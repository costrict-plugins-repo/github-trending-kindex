"""Independent acceptance guards for the user-ratified integrity repair.

Contract references (root delegation, ratification confirmed by validator):
T1: Storage insert/upsert rejects non-string, empty, unknown and serialized
    parameter types before mutation; invalid upserts preserve the original.
T2: Graph import, including dry-run, validates types and is atomic.
T3: Restore and derived-copy reject malformed historical types without loss;
    opening a store does not migrate existing type values.
T4: Valid public and internal coordination/conversation types remain supported;
    public add restrictions remain narrower.
A1: Same-ID divergent/unverifiable archived node or edge evidence never gets
    overwritten; source and archive survive, failure is sanitized, batch continues.
A2: Ordinary archiving/restoring, including both endpoints, remains supported.

Only existing tests and the validator-provided public API/schema were consulted.
All SQLite edits below construct legacy/collision inputs in temporary databases.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from kindex.archive import archive_nodes, restore_node
from kindex.config import Config
from kindex.store import Store


BAD_TYPES = [None, 17, False, [], {}, "", " ", "not-a-node-type",
             '{"node_type":"concept","title":"injected"}', '["concept"]']
PUBLIC_TYPES = ["concept", "document", "session", "person", "project", "decision",
                "question", "artifact", "skill", "constraint", "directive",
                "checkpoint", "watch", "task"]


@pytest.fixture
def world(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    yield cfg, store
    store.close()


def rows(conn, table):
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def graph_state(store):
    return {table: rows(store.conn, table) for table in ("nodes", "edges", "activity_log")}


def archive_path(tmp_path):
    paths = list((tmp_path / "data" / "archive").glob("*.db"))
    assert len(paths) == 1, "A2 fixture must create one real archive"
    return paths[0]


def archive_state(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {table: rows(conn, table) for table in ("archived_nodes", "archived_edges")}


def restore_raw_row(conn, table, row):
    """Recreate a pre-existing duplicate exactly; bypass admission only in setup."""
    columns = ",".join(f'"{key}"' for key in row)
    placeholders = ",".join("?" for _ in row)
    conn.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', list(row.values()))
    conn.commit()


def cli(cfg, *args):
    return subprocess.run(
        [sys.executable, "-m", "kindex.cli", *args, "--data-dir", str(cfg.data_path),
         "--config", "/dev/null"], capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("bad_type", BAD_TYPES)
@pytest.mark.parametrize("upsert", [False, True])
def test_invalid_storage_type_preserves_graph(world, bad_type, upsert):
    """T1 red-now: omission of write-boundary validation admits or replaces a row."""
    _, store = world
    store.add_node("Peer", node_id="peer")
    if upsert:
        store.add_node("Original", content="durable evidence", node_id="target")
        store.add_edge("target", "peer", provenance="original evidence")
    before = graph_state(store)
    with pytest.raises(ValueError):  # T1: deliberate validation, not a SQLite binding error.
        store.add_node("Replacement", content="new claim", node_id="target", node_type=bad_type)
    assert graph_state(store) == before  # T1: includes original row, edges and activity.


@pytest.mark.parametrize("node_type", PUBLIC_TYPES + ["coordination", "conversation"])
def test_valid_storage_types_remain_supported(world, node_type):
    """T4 green-now: a public-only storage allowlist breaks internal producers."""
    _, store = world
    store.add_node("Valid type", node_id="valid", node_type=node_type)
    assert rows(store.conn, "nodes")[0]["type"] == node_type  # T4


@pytest.mark.parametrize("bad_type", BAD_TYPES)
@pytest.mark.parametrize("dry_run", [False, True])
def test_import_type_rejection_is_atomic(world, tmp_path, bad_type, dry_run):
    """T2 red-now: dry-run bypass or per-record validation permits malformed imports."""
    cfg, store = world
    store.add_node("Preserved", node_id="existing")
    before = graph_state(store)
    transfer = tmp_path / "invalid.json"
    transfer.write_text(json.dumps([
        {"id": "first", "title": "Valid first record", "type": "concept"},
        {"id": "second", "title": "Malformed second record", "type": bad_type},
    ]))
    result = cli(cfg, "import", str(transfer), *(["--dry-run"] if dry_run else []))
    assert result.returncode != 0, result.stdout  # T2: dry-run must reject too.
    assert graph_state(store) == before  # T2: valid prefix must not be persisted.


@pytest.mark.parametrize("whole_row", [False, True])
@pytest.mark.parametrize("bad_type", ["", "unknown-type", '{"node_type":"concept"}'])
def test_invalid_archive_type_is_retained(world, tmp_path, whole_row, bad_type):
    """T3 red-now: restore cannot recreate malformed historical types."""
    cfg, store = world
    store.add_node("Historical input", node_id="historical")
    assert archive_nodes(cfg, store, ["historical"]) == 1  # A2 fixture reachability.
    path = archive_path(tmp_path)
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute("SELECT node_row FROM archived_nodes").fetchone()[0])
        payload["type"] = bad_type
        conn.execute("UPDATE archived_nodes SET type = ?, node_row = ?",
                     (bad_type, json.dumps(payload) if whole_row else None))
    before_archive = archive_state(path)
    before_graph = graph_state(store)
    with pytest.raises(ValueError):  # T3
        restore_node(cfg, store, "historical")
    assert archive_state(path) == before_archive  # T3: retain rejected evidence.
    assert graph_state(store) == before_graph  # T3: no partial restore.


def test_reopen_preserves_legacy_type_but_supersede_refuses_copy(world):
    """T3 mixed: preserve old values (green); refuse new malformed copies (red)."""
    cfg, store = world
    store.add_node("Legacy claim", node_id="legacy")
    malformed = '{"node_type":"decision"}'
    store.conn.execute("UPDATE nodes SET type = ? WHERE id = 'legacy'", (malformed,))
    store.conn.commit()
    store.close()
    reopened = Store(cfg)
    try:
        assert rows(reopened.conn, "nodes")[0]["type"] == malformed  # T3 no migration.
        before = graph_state(reopened)
        with pytest.raises(ValueError):  # T3 derived-copy rejection.
            reopened.supersede_node("legacy", "Replacement claim")
        # Reads may update last_accessed, but no content/lifecycle/evidence changes.
        after = graph_state(reopened)
        for state in (before, after):
            for row in state["nodes"]:
                row.pop("last_accessed", None)
        assert after == before  # T3
    finally:
        reopened.close()


@pytest.mark.parametrize("rotated", [False, True])
def test_archive_node_collision_preserves_both_and_continues(world, tmp_path, rotated):
    """A1 red-now: INSERT OR REPLACE destroys the first owner's evidence."""
    cfg, store = world
    store.add_node("Archived owner", content="first evidence", node_id="collision")
    assert archive_nodes(cfg, store, ["collision"]) == 1  # A2 fixture.
    path = archive_path(tmp_path)
    if rotated:
        moved = path.with_name("archive_20000101_000000.db")
        path.rename(moved)
        path = moved
    archive_before = archive_state(path)
    store.add_node("Live owner", content="second evidence", node_id="collision")
    store.add_node("Unrelated", node_id="fine")
    live_before = next(row for row in rows(store.conn, "nodes") if row["id"] == "collision")
    assert archive_nodes(cfg, store, ["collision", "fine"]) == 1  # A1 batch continues.
    assert next(row for row in rows(store.conn, "nodes") if row["id"] == "collision") == live_before  # A1
    assert not any(row["id"] == "fine" for row in rows(store.conn, "nodes"))  # A1
    after = archive_state(path)
    assert [row for row in after["archived_nodes"] if row["id"] == "collision"] == archive_before["archived_nodes"]  # A1
    assert after["archived_edges"] == archive_before["archived_edges"]  # A1
    failed = json.loads(store.get_meta("archive_failed_ids"))
    assert store.get_meta("archive_failed_count") == "1"  # A1 visible failure.
    assert [item["id"] for item in failed] == ["collision"]  # A1
    assert failed[0]["error"]  # A1 sanitized signal must exist.
    assert all(text not in failed[0]["error"] for text in ("first evidence", "second evidence", "Archived owner", "Live owner"))  # A1 no evidence disclosure.


@pytest.mark.parametrize("damage", ["divergent", "unverifiable"])
def test_archive_edge_collision_preserves_complete_evidence(world, tmp_path, damage):
    """A1 red-now: a same-ID edge must not overwrite divergent or unverifiable evidence."""
    cfg, store = world
    store.add_node("First endpoint", node_id="first")
    store.add_node("Second endpoint", node_id="second")
    store.add_node("Unrelated", node_id="fine")
    store.add_edge("first", "second", weight=0.7, provenance="original edge", bidirectional=False)
    original_edge = rows(store.conn, "edges")[0]
    assert archive_nodes(cfg, store, ["first"]) == 1  # A2 fixture.
    path = archive_path(tmp_path)
    # The second endpoint survives. Reintroduce an edge left by an interrupted
    # prior move, with the same identity as the archive's edge.
    first = archive_state(path)["archived_nodes"][0]
    restore_raw_row(store.conn, "nodes", json.loads(first["node_row"]))
    restore_raw_row(store.conn, "edges", original_edge)
    if damage == "divergent":
        store.conn.execute("UPDATE edges SET weight = 0.9 WHERE id = ?", (original_edge["id"],))
        store.conn.commit()
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE archived_edges SET edge_row = NULL")
    before_graph = graph_state(store)
    before_archive = archive_state(path)
    assert archive_nodes(cfg, store, ["second", "fine"]) == 1  # A1 edge collision, batch continues.
    after_graph = graph_state(store)
    assert after_graph["edges"] == before_graph["edges"]  # A1 live edge survives.
    assert [row for row in after_graph["nodes"] if row["id"] == "second"] == [row for row in before_graph["nodes"] if row["id"] == "second"]  # A1
    after_archive = archive_state(path)
    assert after_archive["archived_edges"] == before_archive["archived_edges"]  # A1 archive edge survives.
    assert not any(row["id"] == "second" for row in after_archive["archived_nodes"])  # A1 no half-copy.
    assert store.get_meta("archive_failed_count") == "1"  # A1
    assert [item["id"] for item in json.loads(store.get_meta("archive_failed_ids"))] == ["second"]  # A1


def test_archive_two_endpoints_roundtrips_original_edge(world):
    """A2 green-now: conservative collision guards must allow shared edge evidence."""
    cfg, store = world
    for node_id in ("first", "second"):
        store.add_node(node_id, node_id=node_id)
    store.add_edge("first", "second", weight=0.73, provenance="original evidence", bidirectional=False)
    before_edges = rows(store.conn, "edges")
    assert archive_nodes(cfg, store, ["first", "second"]) == 2  # A2
    assert rows(store.conn, "nodes") == []  # A2
    assert restore_node(cfg, store, "first") is True  # A2
    assert restore_node(cfg, store, "second") is True  # A2
    assert rows(store.conn, "edges") == before_edges  # A2 complete evidence.


@pytest.mark.parametrize("node_type", ["session", "project", "task", "coordination", "conversation"])
def test_public_add_keeps_its_narrower_type_policy(world, node_type):
    """T4 green-now: accepting a storage type must not broaden public add."""
    cfg, store = world
    before = graph_state(store)
    result = cli(cfg, "add", "Public boundary claim", "--type", node_type)
    assert result.returncode != 0  # T4 public generic add remains restricted.
    assert graph_state(store) == before  # T4 no hidden admission on failure.


def test_legacy_restore_edge_identity_collision_preserves_both_stores(world, tmp_path):
    """A1 red-now: legacy restore must not renumber evidence to evade an ID collision."""
    cfg, store = world
    for node_id in ("a", "b", "c"):
        store.add_node(node_id, node_id=node_id)
    store.add_edge("a", "b", weight=0.73, provenance="archived edge evidence", bidirectional=False)
    store.conn.execute("UPDATE edges SET id = '7'")
    store.conn.commit()
    assert archive_nodes(cfg, store, ["a"]) == 1  # A2 fixture reaches real archive.
    path = archive_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE archived_edges SET edge_row = NULL WHERE id = '7'")
    store.add_edge("b", "c", weight=0.41, provenance="live edge evidence", bidirectional=False)
    store.conn.execute("UPDATE edges SET id = '7'")
    store.conn.commit()
    before_graph = graph_state(store)
    before_archive = archive_state(path)
    archived_edge = before_archive["archived_edges"][0]
    live_edge = before_graph["edges"][0]
    assert str(archived_edge["id"]) == str(live_edge["id"]) == "7"  # A1 collision reached.
    assert (archived_edge["from_id"], archived_edge["to_id"]) == ("a", "b")  # A1
    assert (live_edge["from_id"], live_edge["to_id"]) == ("b", "c")  # A1
    assert archived_edge["edge_row"] is None  # A1 exercise legacy representation.
    with pytest.raises(ValueError):  # A1 refuse ambiguous identity, never renumber it.
        restore_node(cfg, store, "a")
    assert graph_state(store) == before_graph  # A1 live edge and absent restored node preserved.
    assert archive_state(path) == before_archive  # A1 complete archived node/edge retained.
