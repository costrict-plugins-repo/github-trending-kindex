"""Independent acceptance tests for retrieval-contract.md (2026-10-15).

Oracle: clauses R1/R2/R3 are numbered 1/2/3 in the Validator-supplied contract.
No implementation source was inspected. Existing tests and Validator-provided
interface facts supply only call shapes, never expected results.

Guard labels and falsifiability ledger are in each test's docstring. All asserts
in a test (including helpers it calls) inherit its cited contract clause. Setup
assertions prove that real vec0, persisted fixtures, and injection were reached.

Mock ledger:
* _embed_document_chunks: deterministic provider records or provider faults;
  real serialization, vec0 writes, metadata, and transaction handling execute.
* _upsert_embedding_outcome in queue tests: one external embedding attempt;
  real queue persistence and reconciliation execute. Explicit .ok/.terminal
  outcomes prevent unset mock defaults from satisfying the oracle.
* is_available: removes optional provider installation dependence only.
* embed_text: deterministic query vector, counted to prohibit re-embedding.
* vectors.time: manually advanced monotonic clock, no sleeps or retries.
* Store.all_nodes: tripwire for forbidden whole-corpus materialization.

No security/auth surface changed; trust behavior remains covered by the existing
trust suites. No tests or verdicts were run by the Tester.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

from kindex import vectors
from kindex.config import Config, EmbeddingConfig
from kindex.store import Store


@pytest.fixture
def vec_store(tmp_path):
    # Missing sqlite-vec is a failed prerequisite, not silently skipped coverage.
    import sqlite_vec  # noqa: F401

    config = Config(data_dir=str(tmp_path), embedding=EmbeddingConfig(
        provider="voyage", dimensions=2,
    ))
    store = Store(config)
    assert vectors.ensure_vec_table(store), "R1/R3 verification requires real sqlite-vec"
    schema = store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='node_vectors'"
    ).fetchone()[0]
    assert "using vec0" in schema.lower(), "R1/R3: ordinary tables are insufficient"
    store.conn.commit()
    yield store
    store.close()


@pytest.fixture
def queue_stores(tmp_path, monkeypatch):
    config = Config(data_dir=str(tmp_path))
    store = Store(config)
    other = Store(config)
    # Keep an accidental write-lock regression deterministic and fast.
    other.conn.execute("PRAGMA busy_timeout=0")
    monkeypatch.setattr(vectors, "is_available", lambda: True)
    yield store, other
    other.close()
    store.close()


def _records(text, embeddings):
    return [dict(index=i, text=f"{text} chunk {i}", embedding=embedding,
                 text_hash=hashlib.sha256(text.encode()).hexdigest(), token_estimate=3)
            for i, embedding in enumerate(embeddings)]


def _provider(monkeypatch, embeddings):
    monkeypatch.setattr(
        vectors, "_embed_document_chunks",
        lambda text, config, *, embed_one: _records(text, embeddings),
    )


def _generation(store):
    return (
        [tuple(row) for row in store.conn.execute(
            "SELECT * FROM node_vectors ORDER BY node_id")],
        [tuple(row) for row in store.conn.execute(
            "SELECT * FROM node_vector_meta ORDER BY vector_id")],
    )


def _seed_generation(store, monkeypatch):
    node_id = store.add_node("Source remains", content="durable old text")
    _provider(monkeypatch, [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]])
    assert vectors.upsert_embedding(store, node_id, "old generation") is True
    return node_id, _generation(store)


def _attempt(store, node_id, method):
    if method == "boolean":
        return vectors.upsert_embedding(store, node_id, "new generation")
    return vectors._upsert_embedding_outcome(store, node_id, "new generation").ok


def _queue(store):
    return json.loads(store.get_meta(vectors.EMBED_QUEUE_META) or "[]")


def _pin_queue(store, ids):
    store.set_meta(vectors.EMBED_QUEUE_META, json.dumps(ids))


def _read_observer(store):
    path = next(row[2] for row in store.conn.execute("PRAGMA database_list")
                if row[1] == "main")
    observer = sqlite3.connect(path)
    observer.execute("PRAGMA busy_timeout=0")
    return observer


@pytest.mark.parametrize("method", ["boolean", "outcome"])
@pytest.mark.parametrize("failure", [
    pytest.param("provider", id="green_provider_failure"),
    pytest.param("dimension", marks=pytest.mark.red_now),
    pytest.param("serialization", marks=pytest.mark.red_now),
    pytest.param("second_metadata_insert", marks=pytest.mark.red_now),
])
def test_failed_replacement_preserves_generation_after_unrelated_commit(
    vec_store, monkeypatch, method, failure,
):
    """R1: provider green guard; storage red targets catch partial replacement."""
    node_id, old = _seed_generation(vec_store, monkeypatch)
    source = tuple(vec_store.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())
    _provider(monkeypatch, [[0.2, 0.8], [0.3, 0.7]])
    reached = []
    if failure == "provider":
        def fail_provider(*args, **kwargs):
            reached.append("provider")
            raise vectors._EmbeddingProviderError(
                kind="transient", http_status=None,
                message="synthetic offline provider failure",
            )
        monkeypatch.setattr(vectors, "_embed_document_chunks", fail_provider)
    elif failure == "dimension":
        _provider(monkeypatch, [[0.2, 0.8], [0.3]])
    elif failure == "serialization":
        _provider(monkeypatch, [[0.2, 0.8], [object(), 0.7]])
    else:
        # Ordinary metadata trigger after a real vec0 insert. This deliberately
        # fails beyond the first new chunk, not at preflight validation.
        vec_store.conn.create_function("test_reached_fault", 0, lambda: reached.append("insert") or 1)
        vec_store.conn.execute("""
            CREATE TEMP TRIGGER fail_second_new_chunk BEFORE INSERT ON node_vector_meta
            WHEN NEW.chunk_index = 1 BEGIN
                SELECT test_reached_fault();
                SELECT RAISE(ABORT, 'synthetic second metadata insert failure');
            END
        """)
        vec_store.conn.commit()

    assert _attempt(vec_store, node_id, method) is False
    assert _generation(vec_store) == old
    vec_store.conn.execute("INSERT INTO meta(key,value) VALUES ('test.unrelated','committed')")
    vec_store.conn.commit()
    assert _generation(vec_store) == old, "R1: unrelated commit must not leak partial replacement"
    assert tuple(vec_store.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()) == source
    if failure in {"provider", "second_metadata_insert"}:
        assert reached, "R1: fault injection must actually execute"


@pytest.mark.red_now
@pytest.mark.parametrize("succeed", [False, True])
@pytest.mark.parametrize("method", ["boolean", "outcome"])
def test_upsert_preserves_caller_transaction_and_nested_savepoint(
    vec_store, monkeypatch, succeed, method,
):
    """R1 red-now: unconditional commit/rollback destroys caller ownership."""
    node_id, old = _seed_generation(vec_store, monkeypatch)
    _provider(monkeypatch, [[0.2, 0.8], [0.3, 0.7]] if succeed else [[0.2, 0.8], [0.3]])
    observer = _read_observer(vec_store)
    try:
        vec_store.conn.execute("BEGIN")
        vec_store.conn.execute("INSERT INTO meta(key,value) VALUES ('test.outer','pending')")
        vec_store.conn.execute("SAVEPOINT caller_nested")
        vec_store.conn.execute("INSERT INTO meta(key,value) VALUES ('test.inner','pending')")
        assert _attempt(vec_store, node_id, method) is succeed
        assert vec_store.conn.in_transaction
        assert vec_store.get_meta("test.outer") == "pending"
        assert vec_store.get_meta("test.inner") == "pending"
        assert observer.execute("SELECT value FROM meta WHERE key='test.outer'").fetchone() is None
        vec_store.conn.execute("ROLLBACK TO caller_nested")
        vec_store.conn.execute("RELEASE caller_nested")
        assert vec_store.get_meta("test.outer") == "pending"
        assert vec_store.get_meta("test.inner") is None
        assert _generation(vec_store) == old
        vec_store.conn.commit()
        assert observer.execute("SELECT value FROM meta WHERE key='test.outer'").fetchone() == ("pending",)
    finally:
        observer.close()


def test_success_replaces_all_chunks_and_is_durable(vec_store, monkeypatch):
    """R1 green-now: append-only replacement or missing commit makes this red."""
    node_id, old = _seed_generation(vec_store, monkeypatch)
    _provider(monkeypatch, [[0.8, 0.2], [0.7, 0.3]])
    assert vectors.upsert_embedding(vec_store, node_id, "replacement") is True
    new = _generation(vec_store)
    assert new != old
    assert len(new[0]) == len(new[1]) == 2
    meta = vec_store.conn.execute(
        "SELECT node_id,chunk_index,chunk_count,text_hash FROM node_vector_meta ORDER BY chunk_index"
    ).fetchall()
    assert [tuple(row) for row in meta] == [
        (node_id, i, 2, hashlib.sha256(b"replacement").hexdigest()) for i in range(2)
    ]
    vec_store.conn.rollback()
    assert _generation(vec_store) == new
    assert vec_store.get_node(node_id)["content"] == "durable old text"


@pytest.mark.red_now
def test_repeated_single_job_drains_do_not_starve_healthy_nodes(queue_stores, monkeypatch):
    """R2 red-now: prepending a failed attempt starves the two healthy jobs."""
    store, other = queue_stores
    ids = [store.add_node(title, content="body") for title in ["flaky", "healthy one", "healthy two"]]
    _pin_queue(store, ids)
    attempts = []
    def attempt(_store, node_id, text):
        attempts.append(node_id)
        return vectors._EmbeddingOutcome(node_id != ids[0])
    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", attempt)
    for _ in range(3):
        vectors.drain_embedding_queue(store, max_jobs=1, report_coverage=False)
    assert attempts == ids
    assert _queue(other) == [ids[0]]


@pytest.mark.red_now
def test_failed_attempt_moves_behind_concurrent_arrival_without_write_lock(queue_stores, monkeypatch):
    """R2 red-now: stale queue overwrite/lost merge or provider write lock fails."""
    store, other = queue_stores
    failed = store.add_node("failing", content="body")
    waiting = store.add_node("waiting", content="body")
    arrival = store.add_node("arrival", content="body")
    _pin_queue(store, [failed, waiting])
    attempts = []
    def attempt(_store, node_id, text):
        attempts.append(node_id)
        other.conn.execute("BEGIN IMMEDIATE")
        other.conn.rollback()
        vectors.enqueue_embedding(other, arrival)
        vectors.enqueue_embedding(other, arrival)
        return vectors._EmbeddingOutcome(False)
    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", attempt)
    result = vectors.drain_embedding_queue(store, max_jobs=1, report_coverage=False)
    assert attempts == [failed]
    assert result["pending"] == 3
    assert _queue(other) == [waiting, arrival, failed]


@pytest.mark.red_now
@pytest.mark.parametrize("terminal", [False, True])
def test_completed_old_input_does_not_remove_concurrent_edit(queue_stores, monkeypatch, terminal):
    """R2 red-now: ID-only completion discards or quarantines a newer version."""
    store, other = queue_stores
    edited = store.add_node("editable", content="old body")
    waiting = store.add_node("waiting", content="body")
    arrival = store.add_node("arrival", content="body")
    _pin_queue(store, [edited, waiting])
    attempts = []
    def attempt(_store, node_id, text):
        attempts.append((node_id, text))
        other.edit_node(edited, content="new body")
        vectors.enqueue_embedding(other, arrival)
        return vectors._EmbeddingOutcome(not terminal, terminal=terminal, kind="input_too_large" if terminal else None)
    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", attempt)
    vectors.drain_embedding_queue(store, max_jobs=1, report_coverage=False)
    assert attempts == [(edited, "editable old body")]
    assert _queue(other) == [waiting, edited, arrival]
    assert store.get_node(edited)["content"] == "new body"
    quarantine = json.loads(other.get_meta("embed.quarantine.v1") or '{"items":{}}')
    assert edited not in quarantine["items"]
    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", lambda s, n, text: attempts.append((n, text)) or vectors._EmbeddingOutcome(True))
    vectors.drain_embedding_queue(store, max_jobs=3, report_coverage=False)
    assert (edited, "editable new body") in attempts
    assert _queue(other) == []


def test_attempt_limits_and_no_same_drain_retry(queue_stores, monkeypatch):
    """R2 green-now: ignoring max_jobs/deadline or retrying in-pass fails."""
    store, other = queue_stores
    ids = [store.add_node(f"job {i}", content="body") for i in range(4)]
    _pin_queue(store, ids)
    clock = [100.0]
    attempts = []
    monkeypatch.setattr(vectors, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def slow_attempt(_store, node_id, text):
        attempts.append(node_id)
        clock[0] += 20.0
        return vectors._EmbeddingOutcome(False)
    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", slow_attempt)
    vectors.drain_embedding_queue(store, max_jobs=3, time_budget=10, report_coverage=False)
    assert attempts == ids[:1]
    assert set(_queue(other)) == set(ids)
    attempts.clear()
    _pin_queue(store, ids)
    vectors.drain_embedding_queue(store, max_jobs=2, time_budget=float("inf"), report_coverage=False)
    assert attempts == ids[:2]
    assert set(_queue(other)) == set(ids)


def test_terminal_same_input_quarantine_remains_terminal(queue_stores, monkeypatch):
    """R2 green-now: treating terminal failures as transient causes retry."""
    store, other = queue_stores
    node_id = store.add_node("terminal", content="same body")
    attempts = []
    def terminal_attempt(*args):
        attempts.append(1)
        return vectors._EmbeddingOutcome(False, terminal=True, kind="input_too_large")
    monkeypatch.setattr(vectors, "_upsert_embedding_outcome", terminal_attempt)
    vectors.drain_embedding_queue(store, max_jobs=1, report_coverage=False)
    assert _queue(other) == []
    assert node_id in json.loads(other.get_meta("embed.quarantine.v1"))["items"]
    # Durable legacy queue entries must still respect same-input quarantine.
    _pin_queue(store, [node_id])
    vectors.drain_embedding_queue(store, max_jobs=1, report_coverage=False)
    assert attempts == [1]
    assert _queue(other) == []


def _index(store, monkeypatch, node_id, embeddings):
    _provider(monkeypatch, embeddings)
    assert vectors.upsert_embedding(store, node_id, f"fixture for {node_id}") is True


def _query_provider(monkeypatch):
    calls = []
    def query(text, config=None, *, input_type="document"):
        calls.append((text, input_type))
        return [1.0, 0.0]
    monkeypatch.setattr(vectors, "embed_text", query)
    return calls


@pytest.mark.red_now
def test_refills_past_one_hundred_duplicate_chunks_with_best_chunk_and_hydration(vec_store, monkeypatch):
    """R3 red-now: fixed 80 candidates or dedup after slicing misses live hits."""
    ids = [vec_store.add_node(f"live {i}", content=f"body {i}", domains=["contract"],
                              extra={"fixture": i}) for i in range(3)]
    # Best chunk is deliberately not chunk zero.
    _index(vec_store, monkeypatch, ids[0], [[0.999, 0.02]] * 100 + [[1.0, 0.0]])
    _index(vec_store, monkeypatch, ids[1], [[0.9, 0.4]])
    _index(vec_store, monkeypatch, ids[2], [[0.7, 0.7]])
    before = {node_id: vec_store.get_node(node_id) for node_id in ids}
    calls = _query_provider(monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("R3 forbids all-node materialization")
    monkeypatch.setattr(vec_store, "all_nodes", forbidden)
    hits = vectors.vector_search(vec_store, "synthetic query", top_k=3)
    assert [hit["id"] for hit in hits] == ids
    assert calls == [("synthetic query", "query")]
    assert hits[0]["vec_chunk_index"] == 100
    assert [hit["vec_distance"] for hit in hits] == sorted(hit["vec_distance"] for hit in hits)
    for hit in hits:
        assert {key: hit[key] for key in before[hit["id"]]} == before[hit["id"]]
        assert vec_store.get_node(hit["id"]) == before[hit["id"]]


@pytest.mark.red_now
def test_refills_past_missing_and_superseded_candidates(vec_store, monkeypatch):
    """R3 red-now: dead rows consuming the initial candidate allowance fails."""
    retired = vec_store.add_node("retired", content="old")
    live = vec_store.add_node("live", content="current")
    _index(vec_store, monkeypatch, "missing-node", [[1.0, 0.0]] * 90)
    _index(vec_store, monkeypatch, retired, [[0.999, 0.01]] * 90)
    _index(vec_store, monkeypatch, live, [[0.9, 0.4]])
    # Stale index rows are deliberately retained, as may happen after interruption.
    vec_store.conn.execute("UPDATE nodes SET status='superseded' WHERE id=?", (retired,))
    vec_store.conn.commit()
    calls = _query_provider(monkeypatch)
    hits = vectors.vector_search(vec_store, "synthetic query", top_k=1)
    assert [hit["id"] for hit in hits] == [live]
    assert len(calls) == 1


def test_floor_and_exhaustion_preserve_eligible_order(vec_store, monkeypatch):
    """R3 green-now: ignoring supplied floor or inventing hits on exhaustion fails."""
    close = vec_store.add_node("exact", content="current")
    far = vec_store.add_node("orthogonal", content="current")
    _index(vec_store, monkeypatch, close, [[1.0, 0.0]])
    _index(vec_store, monkeypatch, far, [[0.0, 1.0]])
    calls = _query_provider(monkeypatch)
    assert [hit["id"] for hit in vectors.vector_search(vec_store, "q", top_k=10, min_similarity=1.0)] == [close]
    assert [hit["id"] for hit in vectors.vector_search(vec_store, "q", top_k=10)] == [close, far]
    assert len(calls) == 2


@pytest.mark.red_now
@pytest.mark.parametrize("top_k", [0, -1, -100])
def test_nonpositive_top_k_does_not_embed(vec_store, monkeypatch, top_k):
    """R3 red-now: embedding before checking requested count makes this red."""
    calls = _query_provider(monkeypatch)
    assert vectors.vector_search(vec_store, "q", top_k=top_k) == []
    assert calls == []


def test_candidate_ceiling_is_finite_even_when_distinct_hits_exist_beyond_it(vec_store, monkeypatch):
    """R3 green-now bound: unbounded refill scans beyond the declared 4096 ceiling."""
    duplicate = vec_store.add_node("duplicate", content="many chunks")
    beyond = vec_store.add_node("beyond ceiling", content="distant")
    _index(vec_store, monkeypatch, duplicate, [[1.0, 0.0]] * 4100)
    _index(vec_store, monkeypatch, beyond, [[0.9, 0.4]])
    calls = _query_provider(monkeypatch)
    statements = []
    vec_store.conn.set_trace_callback(statements.append)
    try:
        hits = vectors.vector_search(vec_store, "q", top_k=2)
    finally:
        vec_store.conn.set_trace_callback(None)
    assert [hit["id"] for hit in hits] == [duplicate]
    assert len(calls) == 1
    scans = [sql for sql in statements if "MATCH" in sql.upper() and "node_vectors" in sql]
    assert scans, "R3: real nearest-neighbor query must be reached"
    bounds = [int(match.group(1) or match.group(2)) for sql in scans
              for match in [re.search(r"\bk\s*=\s*(\d+)|\bLIMIT\s+(\d+)", sql, re.I)]
              if match]
    assert bounds, "R3: nearest-neighbor candidate bound must be observable"
    assert max(bounds) <= 4096
    assert len(scans) <= 16, "R3: geometric expansion must terminate"
