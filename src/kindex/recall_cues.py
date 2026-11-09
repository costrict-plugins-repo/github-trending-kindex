"""Offline, source-supported retrieval metadata; never answer evidence.

The extractive generator selects entry points already present in a source. It
does not infer synonyms, anticipate questions, or claim human memory benefits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator

from .privacy import protect_logger

_log = protect_logger(logging.getLogger(__name__))
GENERATOR_VERSION = "extractive-v1"
_SOURCE_DEFAULTS = {
    "id": "", "type": "concept", "title": "", "content": "", "aka": "",
    "intent": "", "prov_who": "", "prov_when": "", "prov_activity": "",
    "prov_why": "", "prov_source": "", "verified_at": None,
    "verified_by": None, "prov_method": None, "valid_at": None,
    "invalid_at": None, "referent": None, "asserted_at": None,
    "true_of": None, "standing": "unruled", "domains": "", "status": "active",
    "audience": "private", "created_at": "", "extra": {},
}
_JSON_FIELDS = {"aka", "domains", "prov_who", "extra", "referent"}
_RETIRED = {"archived", "superseded"}


class RecallCue(BaseModel):
    """Framing plus an exact, separately located supporting source span."""

    kind: Literal["alias", "problem", "context"]
    origin: Literal["source_alias", "generated"]
    text: str = Field(min_length=1, max_length=240)
    support_field: Literal["title", "content", "aka"]
    support_start: int = Field(ge=0, strict=True)
    support_end: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def nonempty_span(self):
        if self.support_end <= self.support_start:
            raise ValueError("Cue support must be a nonempty source span")
        return self


def source_fingerprint(node: dict) -> str:
    """Hash the persisted source view, excluding usage and retrieval fields."""
    view = {}
    for field, default in _SOURCE_DEFAULTS.items():
        value = node.get(field, default)
        if field in _JSON_FIELDS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass  # Store.peek_node preserves a legacy non-JSON string too.
        view[field] = value
    if "scope" in node:
        view["scope"] = node["scope"]
    canonical = json.dumps(view, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_text(node: dict, field: str) -> str:
    value = node.get(field) or ""
    if field == "aka" and isinstance(value, list):
        return "\n".join(value) if all(isinstance(v, str) for v in value) else ""
    return value if isinstance(value, str) else ""


def generate_cues(node: dict) -> list[RecallCue]:
    """Select at most three distinct, intact source spans, without a provider."""
    cues: list[RecallCue] = []
    seen: set[str] = set()

    def add(field: str, start: int, end: int, kind: str, origin: str) -> None:
        span = _source_text(node, field)[start:end]
        key = " ".join(span.casefold().split()).rstrip(".!?")
        prefix = "" if origin == "source_alias" else f"{kind.title()}: "
        if not key or key in seen or len(prefix) + len(span) > 240:
            return
        cues.append(RecallCue(kind=kind, origin=origin, text=prefix + span,
                              support_field=field, support_start=start,
                              support_end=end))
        seen.add(key)

    aliases = _source_text(node, "aka")
    for match in re.finditer(r"[^\n]+", aliases):
        raw = match.group()
        start = match.start() + len(raw) - len(raw.lstrip())
        add("aka", start, match.end() - (len(raw) - len(raw.rstrip())),
            "alias", "source_alias")
        if cues:
            break

    title = _source_text(node, "title")
    add("title", len(title) - len(title.lstrip()), len(title.rstrip()),
        "problem", "generated")
    content = _source_text(node, "content")
    inspected = content[:12000]
    for match in re.finditer(r"[^\n]+", inspected):
        if len(cues) >= 3:
            break
        # Keep passages intact, including decimals, addresses and abbreviations.
        # A content cutoff must not turn a partial passage into a cue. Long
        # passages are omitted by add's text bound rather than summarized.
        if (match.end() == 12000 and len(content) > 12000
                and content[12000] != "\n"):
            continue
        raw = match.group()
        start = match.start() + len(raw) - len(raw.lstrip())
        end = match.end() - (len(raw) - len(raw.rstrip()))
        kind = "context" if any(c.kind == "problem" for c in cues) else "problem"
        add("content", start, end, kind, "generated")
    return cues


def _limit(value: int, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}")
    return value


def _supported_text(node: dict, cues: list[RecallCue]) -> str:
    # Index source wording only. Shared generated labels carry no recall signal.
    return "\n".join(_source_text(node, c.support_field)[c.support_start:c.support_end]
                     for c in cues)


def build_cues(store, *, node_ids: list[str] | None = None, limit: int = 100) -> dict:
    """Explicit bounded build, publishing each source with a fingerprint CAS."""
    _limit(limit, 1000)
    if node_ids is not None:
        if (not isinstance(node_ids, list) or len(node_ids) > 1000
                or any(not isinstance(n, str) or not n.strip() for n in node_ids)):
            raise ValueError("node_ids must be at most 1000 nonempty strings")
        selected = list(dict.fromkeys(node_ids))[:limit]
    else:
        selected = [row[0] for row in store.conn.execute(
            "SELECT id FROM nodes WHERE status NOT IN ('archived', 'superseded') "
            "ORDER BY id LIMIT ?", (limit,))]
    counts = dict.fromkeys(("selected", "built", "unchanged", "missing", "stale", "skipped"), 0)
    counts["selected"] = len(selected)
    for source_id in selected:
        source = store.peek_node(source_id)
        if source is None:
            counts["missing"] += 1
            continue
        if source.get("status") in _RETIRED:
            counts["skipped"] += 1
            continue
        digest = source_fingerprint(source)
        cues = generate_cues(source)
        encoded = json.dumps([c.model_dump() for c in cues], ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        indexed = _supported_text(source, cues)
        conn = store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = store.peek_node(source_id)
            if current is None or source_fingerprint(current) != digest:
                counts["stale"] += 1
                conn.rollback()
                continue
            previous = conn.execute(
                "SELECT source_hash, generator_version, cues_json FROM recall_cue_sets "
                "WHERE source_id = ?", (source_id,)).fetchone()
            fts = conn.execute(
                "SELECT text FROM recall_cues_fts WHERE source_id = ?", (source_id,)).fetchall()
            if (previous and previous["source_hash"] == digest
                    and previous["generator_version"] == GENERATOR_VERSION
                    and previous["cues_json"] == encoded
                    and len(fts) == 1 and fts[0]["text"] == indexed):
                counts["unchanged"] += 1
                conn.rollback()
                continue
            conn.execute(
                "INSERT INTO recall_cue_sets "
                "(source_id, source_hash, generator_version, generated_at, cues_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
                "source_hash=excluded.source_hash, generator_version=excluded.generator_version, "
                "generated_at=excluded.generated_at, cues_json=excluded.cues_json",
                (source_id, digest, GENERATOR_VERSION,
                 datetime.now(timezone.utc).isoformat(), encoded))
            conn.execute("DELETE FROM recall_cues_fts WHERE source_id = ?", (source_id,))
            conn.execute("INSERT INTO recall_cues_fts(source_id, text) VALUES (?, ?)",
                         (source_id, indexed))
            conn.commit()
            counts["built"] += 1
        except BaseException:
            conn.rollback()
            raise
    return counts


def cue_candidates(store, query: str, *, limit: int = 20,
                   node_filter: Callable[[dict], bool] | None = None) -> list[tuple[str, float]]:
    """Rank unique current sources, with bounded refill after source admission."""
    _limit(limit, 100)
    tokens = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))[:32]
    if not tokens:
        return []
    expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    try:
        rows = store.conn.execute(
            "SELECT f.source_id, s.source_hash, s.generator_version, bm25(recall_cues_fts) AS score "
            "FROM recall_cues_fts AS f JOIN recall_cue_sets AS s ON s.source_id=f.source_id "
            "WHERE recall_cues_fts MATCH ? ORDER BY score, f.source_id LIMIT 4096",
            (expression,))
        found: list[tuple[str, float]] = []
        seen: set[str] = set()
        for row in rows:
            source_id = row["source_id"]
            if source_id in seen:
                continue
            seen.add(source_id)
            source = store.peek_node(source_id)
            if (source is None or source.get("status") in _RETIRED
                    or row["generator_version"] != GENERATOR_VERSION
                    or source_fingerprint(source) != row["source_hash"]
                    or (node_filter is not None and not node_filter(source))):
                continue
            # BM25 is used for order; reciprocal accepted rank is finite and positive.
            found.append((source_id, 1.0 / (len(found) + 1)))
            if len(found) >= limit:
                break
        return found
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _log.warning("Recall cue retrieval unavailable; ordinary retrieval retained")
        return []


def cue_status(store) -> dict:
    """Stored set/cue counts; validity is checked against the source on recall."""
    row = store.conn.execute(
        "SELECT COUNT(*) AS sets, COALESCE(SUM(json_array_length(cues_json)), 0) AS cues "
        "FROM recall_cue_sets").fetchone()
    return {"sets": int(row["sets"]), "cues": int(row["cues"])}
