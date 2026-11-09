"""Independent acceptance tests for adaptive-contract.md (Validator executes).

Clause labels below are exact paragraph anchors in that signed contract:
C1 Shared APIs; C2 Config/status; C3 cadence/events; C4 override precedence;
C5 failures/worker; C6 atomic queue/restart; C7 advice lifecycle; C8 surfaces.
Every test is red-now unless marked green-now. Mutations in docstrings identify
what each oracle rejects. No controller or peer implementation was inspected.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from kindex import adaptive_sim as control
from kindex import sim, supervisor
from kindex.config import Config
from kindex.store import Store

NOW = 1_792_022_400.0
WORK = "User: Repair invoice parsing and preserve reconciliation. Agent: Add regression validation before changing the parser."
GOAL = "Preserve invoice reconciliation while repairing parsing"
NOTE = "Review reconciliation evidence before deleting validation."


class BaselineAdaptiveSettings(BaseModel):
    """Only supplies new input to old Config for functional baseline red tests."""
    enabled: bool = True
    min_interval_seconds: int = 60
    base_interval_seconds: int = 300
    max_interval_seconds: int = 1200
    quiet_hysteresis: int = 2
    jitter_fraction: float = 0
    failure_threshold: int = 3
    circuit_cooldown_seconds: int = 300
    advice_ttl_seconds: int = 900
    max_override_seconds: int = 86400
    max_receipts: int = 200


def config_for(path, **adaptive):
    cfg = Config(data_dir=str(path), sim={"enabled": True, "tick_interval": 1,
        "triage_banter": False, "drain_on_tick": False, "max_stale_ticks": 100,
        "command": "false", "grounding_chars": 0})
    settings = BaselineAdaptiveSettings(**adaptive).model_dump()
    if "adaptive" in type(cfg.sim).model_fields:
        cfg.sim = type(cfg.sim).model_validate({**cfg.sim.model_dump(), "adaptive": settings})
    else:
        cfg.sim = cfg.sim.model_copy(update={"adaptive": BaselineAdaptiveSettings(**adaptive)})
    return cfg


class Box:
    def __init__(self, root, monkeypatch):
        self.root, self.clock = root, NOW
        self.cfg = config_for(root / "data")
        self.store = Store(self.cfg)
        self.calls, self.kind, self.seq = [], "quiet", 0
        monkeypatch.setattr(control, "now", lambda: self.clock)
        monkeypatch.setattr(sim, "call_sim", self.provider)
        monkeypatch.setattr(sim, "build_sim_grounding", lambda *a, **kw: "")
        monkeypatch.setenv("KIN_HEALTH_DIR", str(root / "health"))

    def provider(self, cfg, ledger, window, conv, **kwargs):
        self.calls.append((conv, window, cfg.sim.command))
        if self.kind != "quiet" and self.kind != "advice":
            return None, {"status": self.kind}
        return sim._SimResult(rating=.95 if self.kind == "advice" else .1,
            note=NOTE if self.kind == "advice" else "", basis="synthetic evidence",
            dimension="alignment", stakes="low", escalate=False,
            escalate_reason=""), {"status": "ok"}

    def enqueue(self, conv="alpha", *, text=None, tick=None, event=None):
        self.seq += 1
        kwargs = {} if event is None else {"event": event}
        return sim.enqueue_sim_review(self.store, self.cfg, conv,
            text if text is not None else WORK + f" Synthetic observed revision {self.seq}.",
            tick=tick or self.seq, intent=GOAL,
            scope={"project_path": str(self.root / "project"), "session_id": conv, "agent": "codex"},
            **kwargs)

    def drain(self):
        return sim.drain_sim_queue(self.store, self.cfg, max_jobs=20)

    def status(self, conv="alpha"):
        return control.control_status(self.store, self.cfg, conversation_id=conv)

    def meta(self, key):
        return json.loads(self.store.get_meta(key) or "[]")

    def outcome(self, kind="quiet", conv="alpha"):
        self.kind = kind
        assert self.enqueue(conv), "C3: fresh observed work must be admitted when due"
        self.drain()
        return self.status(conv)

    def reopen(self):
        self.store.close()
        self.store = Store(self.cfg)


@pytest.fixture
def box(tmp_path, monkeypatch):
    value = Box(tmp_path, monkeypatch)
    yield value
    value.store.close()


def test_due_gates_activity_and_time_alone_creates_no_work(box):
    """C3: removing due gating or scheduling idle intervals turns this red."""
    first = box.outcome()
    assert first["quiet_streak"] == 1
    assert first["next_due_at"] == NOW + 300
    box.clock = NOW + 299
    assert not box.enqueue()
    box.clock = NOW + 300
    box.drain()
    assert len(box.calls) == 1
    assert not box.enqueue(text="")
    assert box.enqueue()
    box.drain()
    assert len(box.calls) == 2


def test_duplicate_window_does_not_advance_counters_or_due(box):
    """C3/C6: count an already admitted subject twice and this fails."""
    assert box.enqueue(text=WORK, tick=1)
    box.drain()
    before = box.status()
    box.clock += 1000
    assert not box.enqueue(text=WORK, tick=2)
    box.drain()
    after = box.status()
    assert after["quiet_streak"] == before["quiet_streak"] == 1
    assert after["next_due_at"] == before["next_due_at"]
    assert len(box.calls) == 1


def test_quiet_hysteresis_ceiling_and_advice_reset(box):
    """C3: eager doubling, absent ceiling, or missing advisory reset fails."""
    for index, interval in enumerate([300, 600, 600, 1200, 1200, 1200], 1):
        status = box.outcome()
        assert status["quiet_streak"] == index
        assert status["next_due_at"] == box.clock + interval
        box.clock = status["next_due_at"]
    status = box.outcome("advice")
    assert status["quiet_streak"] == 0
    assert status["next_due_at"] == box.clock + 300


@pytest.mark.parametrize("event", ["goal_changed", "tool_error", "user_attention"])
def test_explicit_events_shorten_to_minimum_but_never_bypass_it(box, event):
    """C3/C4: ignore explicit event or bypass minimum interval and fail."""
    box.outcome()
    box.clock = NOW + 59
    assert not box.enqueue(event=event)
    box.clock = NOW + 60
    assert box.enqueue(event=event)
    box.drain()
    assert len(box.calls) == 2


@pytest.mark.parametrize("event", ["activity", "unrecognized"])
def test_transcript_error_words_and_unknown_events_do_not_accelerate(box, event):
    """C3: transcript keyword inference or treating unknown events as urgent fails."""
    box.outcome()
    box.clock = NOW + 60
    assert not box.enqueue(event=event, text=WORK + " tool_error user_attention goal_changed")
    assert len(box.calls) == 1


def test_attention_and_both_scoped_backoffs_have_finite_precedence(box):
    """C4: attention overriding either backoff, or backoff leaking scope, fails."""
    box.outcome()
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=120)
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=600, conversation_id="alpha")
    box.clock = NOW + 60
    assert not box.enqueue()
    box.clock = NOW + 120
    assert box.enqueue()
    box.drain()
    control.clear_override(box.store, conversation_id="alpha")
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=120, conversation_id="alpha")
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=600)
    box.clock += 60
    assert not box.enqueue()
    assert box.enqueue("beta")
    box.clock += 60
    assert box.enqueue("alpha")


def test_explicit_backoff_applies_when_adaptive_disabled(box):
    """C4: gating backoff on adaptive.enabled fails."""
    box.cfg.sim.adaptive.enabled = False
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=60, conversation_id="alpha")
    assert not box.enqueue()
    assert box.enqueue("beta")
    box.clock += 60
    assert box.enqueue()


@pytest.mark.parametrize("duration", [0, -1, 86401, 1.5, True, "60", float("nan"), float("inf")])
def test_invalid_duration_is_rejected_before_any_control_write(box, duration):
    """C1/C2: coercing or accepting invalid duration mutates state and fails."""
    before = list(box.store.conn.execute("SELECT key,value FROM meta ORDER BY key"))
    try:
        result = control.set_override(box.store, box.cfg, mode="attention", duration_seconds=duration, conversation_id="alpha")
    except (ValueError, TypeError):
        pass
    else:
        assert result.get("ok") is False
    assert list(box.store.conn.execute("SELECT key,value FROM meta ORDER BY key")) == before


@pytest.mark.parametrize("conversation", ["", " ", "bad\nidentity"])
def test_invalid_scope_is_rejected_before_write(box, conversation):
    """C1: treating empty/invalid conversation as store scope fails."""
    before = list(box.store.conn.execute("SELECT key,value FROM meta ORDER BY key"))
    try:
        result = control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=60, conversation_id=conversation)
    except (ValueError, TypeError):
        pass
    else:
        assert result.get("ok") is False
    assert list(box.store.conn.execute("SELECT key,value FROM meta ORDER BY key")) == before


def test_override_is_store_local_persistent_and_clear_is_scope_exact(box):
    """C1/C4: global state, nonpersistent control, or broad clear fails."""
    control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=120, conversation_id="alpha")
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=120, conversation_id="beta")
    box.reopen()
    assert not box.enqueue()
    other = Store(config_for(box.root / "other"))
    try:
        assert control.control_status(other, box.cfg, conversation_id="alpha")["override"] is None
    finally:
        other.close()
    control.clear_override(box.store, conversation_id="beta")
    assert not box.enqueue()
    control.clear_override(box.store, conversation_id="alpha")
    assert box.enqueue()


def test_jitter_is_stable_bounded_and_backward_clock_delay_is_bounded(box):
    """C3/C6: resampling jitter, out-of-range jitter, or clock lockout fails."""
    box.cfg.sim.adaptive.jitter_fraction = .1
    state = box.outcome()
    due = state["next_due_at"]
    assert NOW + 270 <= due <= NOW + 330
    for _ in range(3):
        assert box.status()["next_due_at"] == due
    box.reopen()
    assert box.status()["next_due_at"] == due
    box.clock -= 10000
    assert box.status()["next_due_at"] <= box.clock + 1200


def test_outcome_is_applied_once_across_reopen_and_repeated_drain(box):
    """C6: applying a finished claim twice increments quiet streak and fails."""
    box.outcome()
    before = box.status()
    box.reopen()
    for _ in range(3):
        box.drain()
    after = box.status()
    assert after["quiet_streak"] == before["quiet_streak"] == 1
    assert after["next_due_at"] == before["next_due_at"]
    assert len(box.calls) == 1


def fail_meta_write(store, key):
    """Real SQL faults; no queue or transaction function is mocked."""
    escaped = key.replace("'", "''")
    for operation in ("INSERT", "UPDATE"):
        store.conn.execute(f"CREATE TEMP TRIGGER fault_{operation.lower()} BEFORE {operation} ON meta "
            f"WHEN NEW.key='{escaped}' BEGIN SELECT RAISE(ABORT, 'synthetic atomic write failure'); END")
    store.conn.commit()


def remove_sql_fault(store):
    for name in ("fault_insert", "fault_update"):
        store.conn.execute("DROP TRIGGER " + name)
    store.conn.commit()


def test_queue_write_failure_does_not_consume_admission_or_due(box):
    """C6: split admission/due from queue insertion and rollback oracle fails."""
    before = box.status()
    fail_meta_write(box.store, sim.SIM_QUEUE_META)
    try:
        try:
            box.enqueue(text=WORK, tick=1)
        except sqlite3.DatabaseError:
            pass
        assert box.meta(sim.SIM_QUEUE_META) == []
        assert box.status()["next_due_at"] == before["next_due_at"]
    finally:
        remove_sql_fault(box.store)
    assert box.enqueue(text=WORK, tick=1), "C6: failed transaction cannot reserve dedup subject"
    box.drain()
    assert len(box.calls) == 1


def generate_advice(box, conv="alpha"):
    box.kind = "advice"
    assert box.enqueue(conv, text=WORK, tick=1)
    box.drain()
    receipts = box.status(conv)["receipts"]
    assert len(receipts) == 1 and receipts[0]["state"] == "generated", "C7: generation persists a receipt"
    return receipts[0]


def pickup(box, conv="alpha", **kw):
    return sim.pop_pending_sim_injection(box.store, box.cfg, conv,
        kw.pop("window", WORK), tick=kw.pop("tick", 2), **kw)


def test_advice_exact_ack_lifecycle_and_no_implicit_ack(box):
    """C7: premature, cross-scope, unknown, or inferred acknowledgement fails."""
    generated = generate_advice(box)
    rid = generated["review_id"]
    assert generated["generated_at"] == NOW
    assert generated["expires_at"] == NOW + 900
    rejected = control.acknowledge_advice(box.store, conversation_id="alpha", review_id=rid)
    assert rejected["ok"] is False and isinstance(rejected["reason"], str)
    injection = pickup(box)
    assert injection is not None and injection.message == NOTE
    assert rid in (getattr(injection, "review_id", None), injection.id.rsplit(":", 1)[-1])
    assert box.status()["receipts"][0]["state"] == "emitted"
    assert pickup(box) is None
    assert box.status()["receipts"][0]["acknowledged_at"] is None
    for conv, review in [("beta", rid), ("alpha", "unknown-review")]:
        bad = control.acknowledge_advice(box.store, conversation_id=conv, review_id=review)
        assert bad["ok"] is False and isinstance(bad["reason"], str)
    for _ in range(2):
        good = control.acknowledge_advice(box.store, conversation_id="alpha", review_id=rid)
        assert good == {"ok": True, "state": "acknowledged"}
    assert box.status()["receipts"][0]["acknowledged_at"] == NOW


@pytest.mark.parametrize("emitted", [False, True])
def test_wall_clock_ttl_expires_without_new_ticks(box, emitted):
    """C7: tick-only expiry or allowing ack at expiry fails."""
    generated = generate_advice(box)
    if emitted:
        assert pickup(box) is not None
    box.clock = generated["expires_at"]
    assert pickup(box, tick=2) is None
    state = box.status()["receipts"][0]
    assert state["state"] == "expired"
    assert control.acknowledge_advice(box.store, conversation_id="alpha", review_id=generated["review_id"])["ok"] is False
    assert box.meta(sim.SIM_PENDING_META) == []


def test_receipts_and_pending_are_bounded_and_status_has_no_content(box):
    """C2/C7: unlimited retention or leaking goal/window/note through status fails."""
    box.cfg.sim.adaptive.max_receipts = 3
    box.cfg.sim.max_queue = 2
    for i in range(8):
        box.kind = "advice"
        assert box.enqueue(f"subject-{i}", text=WORK, tick=1)
        box.drain()
    status = box.status(None)
    assert len(status["receipts"]) <= 3
    assert len(box.meta(sim.SIM_PENDING_META)) <= 2
    allowed = {"review_id", "conversation_id", "state", "generated_at", "expires_at", "emitted_at", "acknowledged_at", "reason"}
    for receipt in status["receipts"]:
        assert set(receipt) <= allowed
    serialized = json.dumps(status)
    assert WORK not in serialized and GOAL not in serialized and NOTE not in serialized


def test_identical_windows_in_distinct_sessions_have_distinct_receipts(box):
    """C6/C7: review ID based on window alone conflates sessions and fails."""
    one = generate_advice(box, "alpha")
    two = generate_advice(box, "beta")
    assert one["review_id"] != two["review_id"]
    assert [r["conversation_id"] for r in box.status("alpha")["receipts"]] == ["alpha"]
    assert [r["conversation_id"] for r in box.status("beta")["receipts"]] == ["beta"]


def test_pickup_sql_failure_cannot_emit_without_removing_pending(box):
    """C7: split pending removal from receipt emission and atomicity fails."""
    generated = generate_advice(box)
    fail_meta_write(box.store, sim.SIM_PENDING_META)
    try:
        try:
            result = pickup(box)
        except sqlite3.DatabaseError:
            result = None
        assert result is None
        assert box.status()["receipts"][0]["state"] == "generated"
        assert len(box.meta(sim.SIM_PENDING_META)) == 1
    finally:
        remove_sql_fault(box.store)
    box.reopen()
    injection = pickup(box)
    assert injection is not None
    assert box.status()["receipts"][0]["review_id"] == generated["review_id"]
    assert box.status()["receipts"][0]["state"] == "emitted"


def test_advice_generation_fault_does_not_commit_quiet_or_pending_and_retries_without_provider(box):
    """C6/C7: lost ready claim or provider replay after terminal-write failure fails."""
    box.kind = "advice"
    assert box.enqueue(text=WORK, tick=1)
    fail_meta_write(box.store, sim.SIM_PENDING_META)
    try:
        try:
            box.drain()
        except (sqlite3.DatabaseError, RuntimeError) as error:
            if isinstance(error, RuntimeError):
                assert str(error) == "review_terminal_commit_failed"
        assert box.meta(sim.SIM_PENDING_META) == []
        assert box.status()["quiet_streak"] == 0
        assert box.status()["receipts"] == []
    finally:
        remove_sql_fault(box.store)
    assert len(box.calls) == 1, "C6: fault reached terminal commit after provider result"
    box.reopen()
    box.drain()
    assert len(box.calls) == 1
    assert len(box.meta(sim.SIM_PENDING_META)) == 1
    assert box.status()["receipts"][0]["state"] == "generated"


@pytest.mark.parametrize("adaptive", [
    {"min_interval_seconds": 0}, {"base_interval_seconds": -1},
    {"max_interval_seconds": 0}, {"quiet_hysteresis": 0},
    {"failure_threshold": 0}, {"circuit_cooldown_seconds": 0},
    {"advice_ttl_seconds": 0}, {"max_override_seconds": 0},
    {"max_receipts": 0}, {"jitter_fraction": -.01},
    {"jitter_fraction": .51}, {"jitter_fraction": float("nan")},
    {"jitter_fraction": float("inf")},
    {"min_interval_seconds": 301, "base_interval_seconds": 300},
    {"base_interval_seconds": 4000, "max_interval_seconds": 3600},
])
def test_adaptive_configuration_rejects_invalid_policy(tmp_path, adaptive):
    """C2: missing typed bounded validation accepts an invalid policy and fails."""
    with pytest.raises(ValidationError):
        Config(data_dir=str(tmp_path), sim={"adaptive": adaptive})


def test_provider_failure_threshold_circuit_cooldown_and_success_close(box):
    """C5: missing store circuit, bypass by attention, or failure to close fails."""
    box.kind = "invalid_output"
    for i in range(3):
        assert box.enqueue(f"failed-{i}")
        box.drain()
        assert box.status()["failures"] == i + 1
    until = box.status()["circuit_until"]
    assert until == NOW + 300
    control.set_override(box.store, box.cfg, mode="attention", duration_seconds=600)
    box.clock = until - 1
    assert not box.enqueue("probe")
    box.clock = until
    box.kind = "quiet"
    assert box.enqueue("probe")
    box.drain()
    assert len(box.calls) == 4
    assert box.status()["failures"] == 0
    assert box.status()["circuit_until"] in (None, 0)


def test_failed_cooldown_probe_reopens_circuit_before_other_queued_dispatch(box):
    """C5: allowing all queued jobs through failed probe instead of one fails."""
    box.cfg.sim.adaptive.failure_threshold = 1
    box.kind = "llm_error"
    assert box.enqueue("first")
    box.drain()
    box.clock += 300
    assert box.enqueue("probe-a")
    box.enqueue("probe-b")
    box.drain()
    assert len(box.calls) == 2
    assert box.status()["circuit_until"] == box.clock + 300


@pytest.mark.parametrize("kind", ["over_global_budget", "unavailable", "config_invalid", "interrupted_spend_unknown"])
def test_non_provider_dispositions_are_not_quiet_or_circuit_failures(box, kind):
    """C5: counting suppression or uncertain interruption as a provider failure fails."""
    box.kind = kind
    assert box.enqueue()
    box.drain()
    assert len(box.calls) == 1
    assert box.status()["quiet_streak"] == 0
    assert box.status()["failures"] == 0
    assert box.status()["circuit_until"] in (None, 0)


@pytest.mark.parametrize("change", ["backoff", "disabled"])
def test_worker_rechecks_live_control_after_enqueue(box, change):
    """C5: dispatch using only admitted control snapshot fails."""
    assert box.enqueue()
    if change == "backoff":
        control.set_override(box.store, box.cfg, mode="backoff", duration_seconds=120)
    else:
        sim.set_sim_enabled(box.store, False)
    box.drain()
    assert box.calls == []
    assert box.status()["quiet_streak"] == 0
    assert box.status()["failures"] == 0


def test_corrupt_control_rejects_admission_without_resetting_to_due_now(box):
    """C6: treating corrupt control as an empty/default record fails."""
    box.outcome()
    corrupt = '{"version":1,"conversations":'
    box.store.set_meta("sim.adaptive.control", corrupt)
    box.clock += 1000
    assert not box.enqueue()
    assert box.store.get_meta("sim.adaptive.control") == corrupt
    assert box.meta(sim.SIM_QUEUE_META) == []
    assert len(box.calls) == 1
    report = box.status()
    assert report.get("reason") or report.get("diagnostic") or report.get("error")


def test_superseded_and_stale_advice_is_discarded_not_emitted(box):
    """C7: keeping superseded receipt generated or delivering stale text fails."""
    old = generate_advice(box)
    box.clock += 300
    assert box.enqueue(text=WORK + " Additional observed migration decision", tick=3)
    box.drain()
    receipts = box.status()["receipts"]
    original = next(r for r in receipts if r["review_id"] == old["review_id"])
    assert original["state"] == "discarded"
    assert pickup(box, window="Completely different discussion of garden flowers", tick=4) is None
    assert all(r["state"] == "discarded" for r in box.status()["receipts"])


def test_legacy_pending_without_generation_time_expires_conservatively(box):
    """C7: legacy missing time treated fresh forever fails."""
    generate_advice(box)
    pending = box.meta(sim.SIM_PENDING_META)
    for field in ("at", "generated_at", "expires_at"):
        pending[0].pop(field, None)
    box.store.set_meta(sim.SIM_PENDING_META, json.dumps(pending))
    box.store.conn.execute("DELETE FROM meta WHERE key='sim.adaptive.control'")
    box.store.conn.commit()
    assert pickup(box) is None
    assert box.meta(sim.SIM_PENDING_META) == []


def test_store_status_reports_earliest_conversation_due(box):
    """C2: reporting an arbitrary or latest due time at store scope fails."""
    first = box.outcome(conv="alpha")["next_due_at"]
    box.clock += 50
    second = box.outcome(conv="beta")["next_due_at"]
    assert first < second
    assert box.status(None)["next_due_at"] == first
