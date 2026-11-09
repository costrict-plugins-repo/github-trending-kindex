"""Independent C5/C6 recovery regressions; Validator owns execution.

Oracles derive from adaptive-contract.md paragraphs "Failures actualprovider"
and "Atomic enqueue", plus the review's observed failure scenarios. No candidate
implementation has been inspected. This module extends the frozen core fixture.
"""
from __future__ import annotations

import sqlite3

import pytest

from kindex import adaptive_sim as control, sim, supervisor
from test_adaptive_sim_contract import box, WORK, GOAL
from test_adaptive_sim_surfaces_contract import real_call_sim


def test_deferred_head_preserves_alpha_and_allows_beta_without_respawn(box, monkeypatch):
    """C5: stopping at a deferred head strands beta; dropping alpha loses work."""
    alpha_scope = {"project_path": str(box.root), "session_id": "alpha", "agent": "codex"}
    alpha = supervisor.session_key(alpha_scope)
    assert box.enqueue(alpha, text=WORK + " Alpha observed change.", tick=1)
    assert box.enqueue("beta", text=WORK + " Beta observed change.", tick=1)
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=120,
        conversation_id=alpha)

    box.drain()
    assert [call[0] for call in box.calls] == ["beta"], "C5: a deferred alpha cannot block admitted beta"
    assert box.status("beta")["quiet_streak"] == 1
    assert box.status(alpha)["quiet_streak"] == 0

    spawns = []
    monkeypatch.setattr(sim, "spawn_background_drain", lambda *a, **kw: spawns.append(1) or False)
    for index in range(3):
        supervisor.supervisor_tick(box.store, box.cfg, alpha_scope,
            text=WORK + " Alpha observed change.", event_id=f"deferred-observation-{index}", goal=GOAL)
        box.drain()
    assert spawns == [], "C5: retained deferred work cannot cause immediate background respawn"
    assert [call[0] for call in box.calls] == ["beta"]

    # Neither a new alpha enqueue nor a new observed window is supplied here:
    # the previously admitted alpha must remain durable until backoff expires.
    box.reopen()
    box.clock += 120
    box.drain()
    assert [call[0] for call in box.calls] == ["beta", alpha]
    assert box.status(alpha)["quiet_streak"] == 1
    box.drain()
    assert len(box.calls) == 2


def install_terminal_once_fault(box):
    """A Python UDF is deliberately nontransactional: exactly one SQL write fails.

    The trigger is armed only after the fake provider has returned a result.
    Cover INSERT and UPDATE spellings of the real meta upsert without replacing
    Store, sim_queue.acknowledge, or any controller function.
    """
    observed = {"failures": 0}
    def fail_once():
        if box.calls and observed["failures"] == 0:
            observed["failures"] += 1
            return 1
        return 0
    box.store.conn.create_function("adaptive_terminal_fail_once", 0, fail_once)
    for operation in ("INSERT", "UPDATE"):
        box.store.conn.execute(
            f"CREATE TEMP TRIGGER adaptive_terminal_{operation.lower()} BEFORE {operation} ON meta "
            "WHEN NEW.key='sim.adaptive.control' AND adaptive_terminal_fail_once()=1 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic one-shot terminal control failure'); END"
        )
    box.store.conn.commit()
    return observed


@pytest.mark.parametrize("outcome", ["quiet", "advice"])
def test_once_failed_terminal_control_commit_preserves_computed_outcome(box, outcome):
    """C6/C7: generic-error fallback that neutrally finishes/deletes a ready claim fails."""
    box.kind = outcome
    assert box.enqueue(text=WORK, tick=1)
    fault = install_terminal_once_fault(box)
    try:
        box.drain()
    except (sqlite3.DatabaseError, RuntimeError) as error:
        # The public safe persistence boundary may project SQLite's exception.
        # Surfacing the failed commit is permitted; losing the ready result is not.
        if isinstance(error, RuntimeError):
            assert str(error) == "review_terminal_commit_failed"
    assert fault["failures"] == 1, "C6 reachability: real terminal meta write hit the one-shot fault"
    assert len(box.calls) == 1, "C6 reachability: provider result existed before the SQL fault"

    # Retrying inside the first drain or preserving ready work until this retry
    # are both valid. A second provider call or neutral finalization is invalid.
    box.reopen()
    box.drain()
    box.drain()
    assert len(box.calls) == 1, "C6: recovery commits the computed result without provider replay"
    status = box.status()
    assert status["failures"] == 0, "C5/C6: SQLite failure is not an actual provider failure"
    if outcome == "quiet":
        assert status["quiet_streak"] == 1, "C6: successful quiet outcome must survive a failed commit"
        assert status["next_due_at"] == box.clock + 300
        assert box.meta(sim.SIM_PENDING_META) == []
    else:
        assert status["quiet_streak"] == 0
        assert len(status["receipts"]) == 1
        assert status["receipts"][0]["state"] == "generated", "C7: advice generation cannot be neutrally discarded"
        assert len(box.meta(sim.SIM_PENDING_META)) == 1
        injection = sim.pop_pending_sim_injection(box.store, box.cfg, "alpha", WORK, tick=2)
        assert injection is not None
        assert status["receipts"][0]["review_id"] in (
            getattr(injection, "review_id", None), injection.id.rsplit(":", 1)[-1])


@pytest.mark.parametrize("live_enabled", [True, False])
def test_live_api_enable_gate_wins_over_admitted_enabled_snapshot(box, monkeypatch, live_enabled):
    """C5: pinned llm.enabled=True cannot authorize dispatch after live opt-out.

    The enabled parameter is the positive reachability control for the same
    actual call_sim and client-factory path; only provider transport is fake.
    """
    import json
    from kindex import llm

    transports = []
    class Reply:
        def read(self):
            return json.dumps({
                "output": [{"type": "message", "content": [{"type": "output_text",
                    "text": json.dumps({"rating": .1, "note": "", "basis": "synthetic evidence"})}]}],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    def transport(request, timeout=None):
        transports.append(request)
        return Reply()
    monkeypatch.setattr(sim, "call_sim", real_call_sim)
    monkeypatch.setattr(llm.urllib.request, "urlopen", transport)
    monkeypatch.setenv("KIN_ADAPTIVE_SYNTHETIC_API_KEY", "synthetic-not-a-real-credential")
    box.cfg.sim.backend = "api"
    box.cfg.sim.command = ""
    box.cfg.sim.model = "gpt-4o-mini"
    box.cfg.llm.provider = "openai"
    box.cfg.llm.model = "gpt-4o-mini"
    box.cfg.llm.api_key_env = "KIN_ADAPTIVE_SYNTHETIC_API_KEY"
    box.cfg.llm.enabled = True
    assert box.enqueue(text=WORK, tick=1), "C5 reachability: review admitted while API enabled"
    box.cfg.llm.enabled = live_enabled
    box.drain()
    assert len(transports) == int(live_enabled)
    assert box.status()["quiet_streak"] == int(live_enabled)
    assert box.status()["failures"] == 0


def test_once_failed_deferred_parking_preserves_alpha_until_expiry(box):
    """C5/C6: parking persistence failure must not neutrally finalize deferred alpha."""
    import json

    assert box.enqueue("alpha", text=WORK + " Alpha observed change.", tick=1)
    assert box.enqueue("beta", text=WORK + " Beta observed change.", tick=1)
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=120,
        conversation_id="alpha")
    fault = {"failures": 0}
    def fail_alpha_park_once(value):
        # Queue insertion/replacement itself remains real. The UDF only selects
        # the first post-enqueue write returning deferred alpha to the queue.
        entries = json.loads(value)
        if fault["failures"] == 0 and any(entry.get("conversation_id") == "alpha" for entry in entries):
            fault["failures"] += 1
            return 1
        return 0
    box.store.conn.create_function("adaptive_park_fail_once", 1, fail_alpha_park_once)
    for operation in ("INSERT", "UPDATE"):
        box.store.conn.execute(
            f"CREATE TEMP TRIGGER adaptive_park_{operation.lower()} BEFORE {operation} ON meta "
            "WHEN NEW.key='sim.queue' AND adaptive_park_fail_once(NEW.value)=1 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic one-shot deferred parking failure'); END"
        )
    box.store.conn.commit()
    try:
        box.drain()
    except (sqlite3.DatabaseError, RuntimeError) as error:
        assert str(error), "C6: surfaced persistence failure carries a safe diagnostic"
    assert fault["failures"] == 1, "C6 reachability: real queue write hit the one-shot parking fault"
    assert all(call[0] != "alpha" for call in box.calls), "C5: failed parking cannot bypass live backoff"

    box.reopen()
    box.drain()
    assert [call[0] for call in box.calls] == ["beta"], "C5/C6: beta proceeds while deferred alpha remains durable"
    assert box.status("alpha")["quiet_streak"] == 0
    assert box.status("alpha")["failures"] == 0
    box.clock += 120
    box.drain()
    assert [call[0] for call in box.calls] == ["beta", "alpha"], "C6: failed parking cannot discard the admitted alpha review"
    assert box.status("alpha")["quiet_streak"] == 1
    box.drain()
    assert len(box.calls) == 2
