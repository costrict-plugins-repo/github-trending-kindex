"""Independent acceptance tests for maintenance-contract.md (2026-10-15).

Oracle: contract items 1--4; baseline tests and Validator-provided interface facts.
No implementation source inspected. Validator owns execution and repair verdict.
Each test's docstring identifies its guard and a falsifying mutation. Assertions
in helpers inherit the contract citation of their calling test.

Mock ledger: queue adapters replace expensive provider work with concrete results;
registry adapter supplies synthetic persisted registrations; cron adapter isolates
profile-pass routing; tracked Store is a real Store with open/close observation;
health and worker adapters record effects without notifications or worker launches.
No permissive MagicMock defaults. No authorization checks are mocked.
"""
from __future__ import annotations

from collections import Counter
import inspect
import json
import math
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

import pytest

from kindex.config import Config, ProfileEntry
from kindex.store import Store


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Contract 4: every mutable graph/config/state root belongs to this test."""
    import kindex.config as config_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("KIN_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    for name in ("KIN_PROFILE", "KIN_PROJECT", "KIN_PROJECT_PATH", "KINDEX_REVIEW_WORKER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_module, "_GLOBAL_PATHS", [home / ".config/kindex/kin.yaml"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def base_config(root, **kwargs):
    return Config(data_dir=str(root / "base"), claude_dir=str(root / "claude"),
                  project_dirs=[str(root / "unrelated-projects")], **kwargs)


def make_project(root, name, *, enabled=True, budget=99.0):
    project = root / name
    (project / ".kin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True,
                   capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                            "GIT_CONFIG_NOSYSTEM": "1"})
    data = project / ".kin/local/kindex"
    (project / ".kin/config").write_text(
        "name: contract-project\n"
        "data_dir: .kin/local/kindex\n"
        f"llm:\n  enabled: {str(enabled).lower()}\n"
        f"attention:\n  enabled: {str(enabled).lower()}\n"
        f"budget:\n  daily: {budget}\n  weekly: {budget}\n  monthly: {budget}\n"
        "embedding:\n  provider: openai\n  model: untrusted-repository-model\n"
    )
    graph = Store(Config(data_dir=str(data)))
    graph.add_node("Project-only canary", node_id="project-canary", queue_embedding=False)
    graph.close()
    return project, data.resolve()


def error_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("error", "errors") and item:
                yield item
            yield from error_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from error_fields(item)


def queue_probes(monkeypatch):
    from kindex import attention, reinforce, vectors

    seen = []
    for module, name, result in (
        (vectors, "drain_embedding_queue", {"status": "ok", "embedded": 1, "pending": 0}),
        (attention, "drain_attention_queue", {"status": "ok", "reviewed": 1, "flagged": 0}),
        (reinforce, "drain_reinforce_queue", {"status": "ok", "graded": 1, "pending": 0}),
    ):
        signature = inspect.signature(getattr(module, name))

        def probe(store, config=None, *args, _name=name, _signature=signature,
                  _result=result, **kwargs):
            effective = _signature.bind(store, config, *args, **kwargs)
            effective.apply_defaults()
            seen.append((_name, store, config, effective.arguments))
            store.set_meta("contract-service:" + _name, "serviced")
            return dict(_result)

        monkeypatch.setattr(module, name, probe)
    return seen


def test_registered_project_queues_are_scoped_bounded_and_canonical_once(world, monkeypatch):
    """Red-now, C1: omit project queues, trust repository execution settings, or omit dedupe => red.

    C1/C2 green guards: reminder sweep first, no unrelated ingest/scan, no stale
    directory creation, and existing local housekeeping remains observable.
    """
    from kindex import daemon, ingest, project_store
    from kindex.locks import lock_node

    project, data = make_project(world, "project", enabled=False)
    alias = world / "data-alias"
    alias.symlink_to(data, target_is_directory=True)
    missing_project = world / "missing-project"
    missing_data = missing_project / ".kin/local/kindex"
    registrations = {str(project): str(data), str(world / "alias-project"): str(alias),
                     str(missing_project): str(missing_data)}
    monkeypatch.setattr(project_store, "registered_project_graphs", lambda: dict(registrations))
    # Repository execution settings are untrusted. The trusted caller's
    # provider policy applies while supported project scope/data roots resolve.
    config = base_config(world, llm={"enabled": True}, attention={"enabled": True},
                         budget={"daily": 0.17, "weekly": 0.17, "monthly": 0.17},
                         embedding={"provider": "openai", "model": "trusted-maintenance-model"})
    graph = Store(config)
    graph.set_meta("project_graph_dirs", json.dumps(registrations))
    base_reminder = graph.add_reminder("base reminder", "2000-01-01T00:00:00")
    graph.close()
    graph = Store(Config(data_dir=str(data)))
    project_reminder = graph.add_reminder("project reminder", "2000-01-01T00:00:00")
    lock_node(graph, "project-canary", "expired-worker", ttl_minutes=-1)
    graph.close()
    monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)

    unrelated = Path(config.claude_dir) / "projects/elsewhere/session-outside.jsonl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(json.dumps({"role": "assistant", "cwd": str(world / "elsewhere"),
                                    "content": "UNRELATED_HOME_TRANSCRIPT_CANARY explains the accounting scheduler."}) + "\n")
    scan_calls = []
    original_scan = ingest.scan_projects

    def scan(config, *args, **kwargs):
        scan_calls.append((config.data_path.resolve(), list(config.project_dirs)))
        return original_scan(config, *args, **kwargs)

    monkeypatch.setattr(ingest, "scan_projects", scan)
    services = queue_probes(monkeypatch)
    original_cron = daemon.cron_run
    reminder_snapshots = []

    def cron(config, store, *args, **kwargs):
        states = []
        for location, rid in ((Path(base_config(world).data_dir), base_reminder),
                              (data, project_reminder)):
            check = Store(Config(data_dir=str(location)))
            try:
                states.append(check.get_reminder(rid)["status"])
            finally:
                check.close()
        reminder_snapshots.append(states)
        return original_cron(config, store, *args, **kwargs)

    monkeypatch.setattr(daemon, "cron_run", cron)
    passes = daemon.cron_run_all(config)

    project_calls = [row for row in services if row[2].data_path.resolve() == data]
    assert Counter(row[0] for row in project_calls) == {
        "drain_embedding_queue": 1, "drain_attention_queue": 1, "drain_reinforce_queue": 1}
    for name, _, resolved, arguments in project_calls:
        assert resolved.llm.enabled and resolved.attention.enabled
        assert resolved.budget.daily == pytest.approx(0.17)
        assert resolved.embedding.model == "trusted-maintenance-model"
        assert Path(resolved.data_dir).resolve() == data
        # A finite existing adapter default is valid; the orchestration need
        # not redundantly pass the same number as an explicit keyword.
        bounds = [arguments[key] for key in ("max_jobs", "time_budget")
                  if key in arguments and arguments[key] is not None]
        if name == "drain_embedding_queue":
            bounds.extend([resolved.embedding.reindex_max_jobs,
                           resolved.embedding.drain_time_budget])
        assert bounds and all(math.isfinite(bound) and bound > 0 for bound in bounds), name
    assert reminder_snapshots and all(states == ["fired", "fired"] for states in reminder_snapshots)
    assert not missing_project.exists()
    for location, roots in scan_calls:
        if location == data:
            assert all(Path(root).resolve().is_relative_to(project.resolve()) for root in roots)
    graph = Store(Config(data_dir=str(data)))
    try:
        assert not graph.all_nodes(node_type="session", limit=100)
        assert "lock" not in (graph.get_node("project-canary").get("extra") or {})
        assert all(graph.get_meta("contract-service:" + name) == "serviced"
                   for name in ("drain_embedding_queue", "drain_attention_queue", "drain_reinforce_queue"))
    finally:
        graph.close()
    assert all(isinstance(entry.get("results"), dict) for entry in passes)


@pytest.mark.parametrize("failure", ["open", "maintenance"])
def test_first_profile_failure_is_sanitized_and_later_profile_runs(world, monkeypatch, failure):
    """Red-now, C2: an uncaught first failure, leaked exception text, or close omission => red."""
    from kindex import daemon
    import kindex.store as store_module

    config = base_config(world, profiles={
        "first": ProfileEntry(data_dir=str(world / "first"), roots=[str(world / "first-root")]),
        "second": ProfileEntry(data_dir=str(world / "second"), roots=[str(world / "second-root")]),
    }, default_profile="second")
    sentinel = "SECRET_API_KEY_AND_PRIVATE_TRANSCRIPT_CANARY"
    opened, closed, serviced, connections = [], [], [], []

    class TrackedStore(Store):
        def __init__(self, config, *args, **kwargs):
            if failure == "open" and config.data_path.resolve() == (world / "first").resolve():
                raise RuntimeError(sentinel)
            super().__init__(config, *args, **kwargs)
            opened.append(self)
            # Store.conn is lazy and may reconnect after close; retain the
            # actual handle so the closure assertion cannot reopen it.
            connections.append(self.conn)

        def close(self):
            closed.append(self)
            return super().close()

    def maintenance(config, store, **kwargs):
        serviced.append(config.active_profile)
        if failure == "maintenance" and config.active_profile == "first":
            raise RuntimeError(sentinel)
        store.set_meta("contract-maintained", "yes")
        return {"sessions": 0, "reminders_fired": 0, "reminders_auto_snoozed": 0}

    monkeypatch.setattr(store_module, "Store", TrackedStore)
    # The public class may be imported at module load or inside the call.
    if hasattr(daemon, "Store"):
        monkeypatch.setattr(daemon, "Store", TrackedStore)
    monkeypatch.setattr(daemon, "cron_run", maintenance)
    passes = daemon.cron_run_all(config)
    by_profile = {entry["profile"]: entry for entry in passes}
    assert "second" in serviced
    assert list(error_fields(by_profile["first"]))
    assert not list(error_fields(by_profile["second"]))
    assert sentinel not in json.dumps(passes)
    assert opened and all(instance in closed for instance in opened)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    check = Store(Config(data_dir=str(world / "second")))
    try:
        assert check.get_meta("contract-maintained") == "yes"
    finally:
        check.close()


@pytest.mark.parametrize("enabled,budget", [(False, 0.17), (True, 0.0)])
def test_project_queue_boundary_preserves_disables_and_budget(world, monkeypatch, enabled, budget):
    """Red-now, C1: repository settings override trusted disables/budget => red.

    Boundary evidence only: existing queue adapters own enforcement; these
    probes prove the newly routed project pass retains trusted execution policy.
    """
    from kindex import daemon, project_store

    project, data = make_project(world, "restricted-project", enabled=not enabled, budget=99)
    monkeypatch.setattr(project_store, "registered_project_graphs", lambda: {str(project): str(data)})
    services = queue_probes(monkeypatch)
    daemon.cron_run_all(base_config(world, llm={"enabled": enabled}, attention={"enabled": enabled},
                                    budget={"daily": budget, "weekly": budget, "monthly": budget}))
    project_calls = [row for row in services if row[2].data_path.resolve() == data]
    assert project_calls, "No project queue boundary was reached"
    for _, _, config, _ in project_calls:
        assert config.llm.enabled is enabled
        assert config.attention.enabled is enabled
        assert config.budget.daily == pytest.approx(budget)
        assert config.budget.weekly == pytest.approx(budget)
        assert config.budget.monthly == pytest.approx(budget)


@pytest.mark.parametrize("failed_store", ["legacy", "project"])
def test_a_failed_store_does_not_suppress_later_registered_projects(world, monkeypatch, failed_store):
    """Red-now, C2: abandon registry iteration after one failure, or silently report it => red."""
    from kindex import daemon, project_store, vectors

    first_project, first_data = make_project(world, "first-project")
    later_project, later_data = make_project(world, "later-project")
    monkeypatch.setattr(project_store, "registered_project_graphs", lambda: {
        str(first_project): str(first_data), str(later_project): str(later_data)})
    config = base_config(world)
    services = queue_probes(monkeypatch)
    probe = vectors.drain_embedding_queue
    sentinel = "SECRET_CONTENT_FROM_FAILED_PROJECT"

    if failed_store == "legacy":
        original_cron = daemon.cron_run

        def fail_legacy(pass_config, store, *args, **kwargs):
            if pass_config.data_path.resolve() == config.data_path.resolve():
                raise RuntimeError(sentinel)
            return original_cron(pass_config, store, *args, **kwargs)
        monkeypatch.setattr(daemon, "cron_run", fail_legacy)
    else:
        def fail_first(store, config=None, *args, **kwargs):
            if store.config.data_path.resolve() == first_data:
                raise RuntimeError(sentinel)
            return probe(store, config, *args, **kwargs)
        monkeypatch.setattr(vectors, "drain_embedding_queue", fail_first)

    passes = daemon.cron_run_all(config)
    assert any(row[2].data_path.resolve() == later_data for row in services)
    failed_profile = None if failed_store == "legacy" else str(first_project)
    failed_entries = [entry for entry in passes if entry["profile"] == failed_profile]
    assert failed_entries and any(list(error_fields(entry)) for entry in failed_entries)
    assert sentinel not in json.dumps(passes)
    check = Store(Config(data_dir=str(later_data)))
    try:
        assert check.get_meta("contract-service:drain_embedding_queue") == "serviced"
    finally:
        check.close()


LOCK_HOLDER = r'''
import fcntl, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('a+') as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    print('HELD', flush=True)
    sys.stdin.readline()
'''

LOCK_PROBE = r'''
import json, sys, time
from pathlib import Path
from unittest.mock import patch
from kindex.config import Config
from kindex.store import Store
from kindex.supervisor import supervisor_tick, read_state, session_key
p = json.load(sys.stdin)
config = Config(data_dir=p['data'], llm={'enabled': True},
                budget={'daily': 1, 'weekly': 5, 'monthly': 10},
                sim={'enabled': True, 'tick_interval': 1, 'triage_banter': False,
                     'command': '/usr/bin/true'})
store = Store(config)
spawned, health = [], []
ledger = Path(config.data_dir) / 'degraded.jsonl'
def diagnostic_rows():
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()] if ledger.exists() else []
def spawn(config):
    spawned.append(str(config.data_path))
    return True
def record(*args, **kwargs):
    health.append({'args': args, 'kwargs': kwargs})
try:
    before = read_state(store, session_key(p['scope']))
    diagnostics_before = diagnostic_rows()
    with patch('kindex.sim.spawn_background_drain', spawn), patch('kindex.supervisor.record_health', record):
        start = time.monotonic()
        result = supervisor_tick(store, config, p['scope'],
            text='User asks to repair invoice validation without deleting ledger rows. '
                 'Assistant proposes deleting reconciliation checks, and validation has not run.',
            event_id='contract-lock-event')
        elapsed = time.monotonic() - start
    after = read_state(store, session_key(p['scope']))
    diagnostics = diagnostic_rows()[len(diagnostics_before):]
    print(json.dumps({'result': result, 'elapsed': elapsed, 'spawned': spawned,
                      'health': health, 'diagnostics': diagnostics, 'before': before, 'after': after}))
finally:
    store.close()
'''


def lock_probe(root, data, scope):
    """C3: cold start has a separate outer deadline; measure only the hook call."""
    env = {"HOME": str(root / "home"), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "XDG_CONFIG_HOME": str(root / "home/.config"),
           "XDG_STATE_HOME": str(root / "state"), "KIN_HEALTH_DIR": str(root / "health"),
           "KIN_NO_SCHEDULER_WRITES": "1"}
    process = subprocess.run([sys.executable, "-c", LOCK_PROBE],
                             input=json.dumps({"data": str(data), "scope": scope}),
                             capture_output=True, text=True, env=env, cwd=root, timeout=5)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize("lock_kind", ["session", "queue"])
def test_actual_held_supervisor_lock_defers_within_bound_and_recovers(world, lock_kind):
    """Red-now, C3: blocking flock, spawn-on-busy, forced unlock, or sticky defer => red.

    Reachability: child acknowledges its actual OS lock before the hook runs;
    same scope reaches normal queued/reviewing work after the child releases it.
    The tick timer excludes imports and Store setup; 5s outer deadline includes
    subprocess cold start and prevents an old blocking implementation hanging CI.
    """
    from kindex.supervisor import session_key

    project, data = make_project(world, "lock-project")
    scope = {"project_path": str(project), "agent": "claude", "session_id": "lock-contract"}
    name = "queue" if lock_kind == "queue" else "session-" + session_key(scope).split(":")[-1]
    lock_path = data / (".supervisor-" + name + ".lock")
    holder = subprocess.Popen([sys.executable, "-c", LOCK_HOLDER, str(lock_path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        readable, _, _ = select.select([holder.stdout], [], [], 5)
        assert readable and holder.stdout.readline().strip() == "HELD"
        inode = lock_path.stat().st_ino
        busy = lock_probe(world, data, scope)
        assert busy["elapsed"] <= 0.250
        assert busy["result"]["ok"] is True
        assert busy["result"]["supervisor"]["state"] in {"skipped", "deferred"}
        assert busy["spawned"] == []
        # C3 permits the existing bounded degraded append when the regular
        # health writer could itself block. Observe only newly appended events.
        contention = [row for row in busy["diagnostics"]
                      if row.get("cmd") == "supervisor_lock_busy"]
        assert busy["health"] or contention, "No bounded contention evidence was emitted"
        assert holder.poll() is None
        assert lock_path.stat().st_ino == inode
        if lock_kind == "session":
            assert busy["after"] == busy["before"]
    finally:
        if holder.poll() is None:
            holder.stdin.write("release\n")
            holder.stdin.flush()
        try:
            holder.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate(timeout=5)
    assert holder.returncode == 0
    recovered = lock_probe(world, data, scope)
    assert recovered["result"]["supervisor"]["state"] in {"queued", "reviewing"}, recovered
    assert recovered["spawned"], "Releasing the real lock did not resume admitted work"
    assert lock_path.stat().st_ino == inode


def test_profile_and_registry_aliases_do_not_repeat_a_store_or_reminder(world, monkeypatch):
    """C1/C2 guard: remove canonical dedupe or merge reminder counts twice => red.

    Expected green for existing reminder accounting; profile/registry maintenance
    canonicalization is a repair target. Validator records those independently.
    """
    from kindex import daemon, project_store

    project, data = make_project(world, "shared-project")
    alias = world / "shared-alias"
    alias.symlink_to(data, target_is_directory=True)
    config = base_config(world, profiles={
        "first": ProfileEntry(data_dir=str(data), roots=[str(project)]),
        "alias": ProfileEntry(data_dir=str(alias), roots=[str(world / "other-root")]),
    }, default_profile="first")
    monkeypatch.setattr(project_store, "registered_project_graphs", lambda: {str(project): str(data)})
    monkeypatch.setattr("kindex.notify.dispatch", lambda *a, **kw: [])
    monkeypatch.setattr("kindex.notify.is_user_idle", lambda c: False)
    graph = Store(Config(data_dir=str(data)))
    graph.add_reminder("one reminder despite three registrations", "2000-01-01T00:00:00")
    graph.close()
    serviced = []

    def maintenance(config, store, *args, **kwargs):
        serviced.append(config.data_path.resolve())
        return {"sessions": 0, "reminders_fired": 0, "reminders_auto_snoozed": 0}

    monkeypatch.setattr(daemon, "cron_run", maintenance)
    queue_calls = queue_probes(monkeypatch)
    passes = daemon.cron_run_all(config)
    assert serviced == [data]
    assert not queue_calls, "Registry repeated a graph already serviced as a profile"
    assert sum(entry["results"].get("reminders_fired", 0)
               + entry["results"].get("project_graph_reminders_fired", 0)
               for entry in passes) == 1


def test_project_embedding_queue_persistence_failure_is_explicit(world, monkeypatch):
    """Red-now, C2: discard a drain's persistence_error status into zero counters => red.

    Reachability: real SQLite TEMP INSERT/UPDATE triggers reject embed.queue
    persistence on the exact project connection used by the real drain. A
    missing-node job requires no provider work, but still requires durable queue
    removal. The real adapter must observe persistence_error, and cron must
    retain explicit failure evidence rather than just embedded/pending counts.

    Mocks: registry selects one hermetic project; availability admits the real
    queue drain; unrelated attention/reinforcement adapters return empty; provider
    outcome is a fail-fast guard which must never be reached for a missing node.
    """
    from kindex import attention, daemon, project_store, reinforce, vectors

    project, data = make_project(world, "persistence-project")
    graph = Store(Config(data_dir=str(data)))
    try:
        vectors.enqueue_embedding(graph, "missing-node-for-persistence-contract")
    finally:
        graph.close()
    monkeypatch.setattr(project_store, "registered_project_graphs", lambda: {str(project): str(data)})
    monkeypatch.setattr(vectors, "is_available", lambda *a, **kw: True)
    monkeypatch.setattr(attention, "drain_attention_queue",
                        lambda *a, **kw: {"status": "empty", "reviewed": 0, "flagged": 0})
    monkeypatch.setattr(reinforce, "drain_reinforce_queue",
                        lambda *a, **kw: {"status": "empty", "graded": 0, "pending": 0})

    def no_provider(*args, **kwargs):
        pytest.fail("A nonexistent node must not reach an embedding provider")

    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", no_provider)
    actual_drain = vectors.drain_embedding_queue
    direct_results = []

    def reject_project_persistence(store, config=None, *args, **kwargs):
        if store.config.data_path.resolve() == data:
            # Both INSERT OR REPLACE and UPDATE must reach the same failure;
            # TEMP triggers affect this connection only and touch no live DB.
            for operation in ("INSERT", "UPDATE"):
                store.conn.execute(
                    f"CREATE TEMP TRIGGER IF NOT EXISTS reject_embed_{operation.lower()} "
                    f"BEFORE {operation} ON main.meta WHEN NEW.key = 'embed.queue' "
                    "BEGIN SELECT RAISE(ABORT, 'PRIVATE_PERSISTENCE_ERROR_CANARY'); END"
                )
            result = actual_drain(store, config, *args, **kwargs)
            direct_results.append(result)
            return result
        return actual_drain(store, config, *args, **kwargs)

    monkeypatch.setattr(vectors, "drain_embedding_queue", reject_project_persistence)
    passes = daemon.cron_run_all(base_config(world))
    assert len(direct_results) == 1
    assert direct_results[0]["status"] == "persistence_error"
    entries = [entry for entry in passes if entry["profile"] == str(project)]
    assert len(entries) == 1

    def failure_status(value):
        if isinstance(value, dict):
            return (value.get("status") in {"persistence_error", "failed", "error"}
                    or any(failure_status(item) for item in value.values()))
        if isinstance(value, list):
            return any(failure_status(item) for item in value)
        return False

    assert list(error_fields(entries[0])) or failure_status(entries[0]), entries[0]
    assert "PRIVATE_PERSISTENCE_ERROR_CANARY" not in json.dumps(entries[0])
    check = Store(Config(data_dir=str(data)))
    try:
        assert json.loads(check.get_meta(vectors.EMBED_QUEUE_META)) == [
            "missing-node-for-persistence-contract"]
    finally:
        check.close()
