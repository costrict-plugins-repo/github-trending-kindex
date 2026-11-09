"""Durable single-worker review claims and exact completion acknowledgments."""
from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .privacy import redact

CLAIM_META = "sim.claim"
QUEUE_META = "sim.queue"
PENDING_META = "sim.pending"


class SavedSim(BaseModel):
    rating: float
    note: str
    basis: str
    dimension: str = ""
    stakes: str = "low"
    escalate: bool = False
    escalate_reason: str = ""
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


class TerminalOutcome(BaseModel):
    state: str
    reason: str
    health_state: str
    health_reason: str
    outcome: Literal["neutral", "quiet", "advisory", "failure"] = "neutral"


class ReviewPersistenceError(RuntimeError):
    """A durable transition failed; retain its claim instead of reclassifying it."""


class ReviewClaim(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid4().hex)
    # The admitted job format predates claims; preserve its pinned config/scope.
    job: dict[str, Any]
    phase: Literal["claimed", "sim_started", "sim_saved", "advocate_started", "ready"] = "claimed"
    result: SavedSim | None = None
    pending: dict[str, Any] | None = None
    terminal: TerminalOutcome | None = None


class DiscardedReview(BaseModel):
    review_id: str
    scope: dict[str, Any] | None = None


class ReviewReceipt(BaseModel):
    attempt_id: str
    conversation_id: str
    review_id: str | None = None
    scope: dict[str, Any] | None = None
    state: str
    reason: str
    health_state: str
    health_reason: str
    completed_at: str
    result: SavedSim | None = None
    discarded: list[DiscardedReview] = Field(default_factory=list)
    health_pending: bool = True


def _put(store, key: str, value) -> None:
    store.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(redact(value))))


def _list(store, key: str) -> list[dict]:
    value = json.loads(store.get_meta(key) or "[]")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Invalid durable review queue")
    return value


def claim_next(store, *, exclude_review_ids=()) -> tuple[ReviewClaim | None, bool]:
    """Caller holds the worker lock; move only one waiting job atomically."""
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        existing = store.get_meta(CLAIM_META)
        if existing:
            return ReviewClaim.model_validate_json(existing), True
        queue = _list(store, QUEUE_META)
        selected = next((i for i, job in enumerate(queue) if job.get("review_id") not in exclude_review_ids), None)
        if selected is None:
            return None, False
        claim = ReviewClaim(job=queue[selected])
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            _put(store, CLAIM_META, claim.model_dump())
            _put(store, QUEUE_META, queue[:selected] + queue[selected + 1:])
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        return claim, False


def save_claim(store, claim: ReviewClaim) -> None:
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        current = ReviewClaim.model_validate_json(store.get_meta(CLAIM_META) or "{}")
        if current.attempt_id != claim.attempt_id or current.job.get("review_id") != claim.job.get("review_id"):
            raise ValueError("Review claim changed before checkpoint")
        store.set_meta(CLAIM_META, claim.model_dump_json())


def park_claim(store, claim, config):
    """Yield an undispatched claim without releasing its admitted subject.

    A newer waiting window for the same conversation supersedes the parked
    snapshot. Otherwise retain its exact review identity at the queue tail.
    """
    from . import adaptive_sim as adaptive
    from .supervisor import store_lock
    if claim.phase != "claimed" or claim.result is not None or claim.terminal is not None:
        raise ValueError("Only undispatched claims may be parked")
    with store_lock(store, "queue"):
        current = ReviewClaim.model_validate_json(store.get_meta(CLAIM_META) or "{}")
        if current.attempt_id != claim.attempt_id or current.job.get("review_id") != claim.job.get("review_id"):
            raise ValueError("Review claim changed before parking")
        queue = _list(store, QUEUE_META)
        conv = claim.job.get("conversation_id")
        newer = any(job.get("conversation_id") == conv for job in queue)
        if not newer:
            queue.append(claim.job)
        cap = max(1, config.sim.max_queue)
        evicted = ([claim.job] if newer else []) + queue[:-cap]
        queue = queue[-cap:]
        control = adaptive.read_control(store)
        for job in evicted:
            adaptive.record_advice(control, config.sim.adaptive, job, status="discarded",
                                   reason="superseded" if job is claim.job and newer else "saturation")
        retained = {job.get("conversation_id") for job in queue}
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            _put(store, QUEUE_META, queue)
            adaptive.put_control(store, control, config.sim.adaptive)
            for job in evicted:
                conversation = job.get("conversation_id")
                if conversation and conversation not in retained:
                    store.conn.execute("DELETE FROM meta WHERE key=?", ("sim.admission." + conversation,))
            store.conn.execute("DELETE FROM meta WHERE key=?", (CLAIM_META,))
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise


def acknowledge(store, claim: ReviewClaim, *, state: str, reason: str,
                health_state: str, health_reason: str, config=None, outcome: str = "neutral") -> list[dict]:
    """Commit the exact claim's result, advisory and removal in one transaction."""
    from .supervisor import store_lock
    from .sim import _now
    from . import adaptive_sim as adaptive
    config = config or store.config
    settings = config.sim.adaptive
    job = claim.job
    conv = str(job.get("conversation_id") or "")
    receipt = ReviewReceipt(attempt_id=claim.attempt_id, conversation_id=conv,
                            review_id=job.get("review_id"), scope=job.get("scope"),
                            state=state, reason=reason, health_state=health_state,
                            health_reason=health_reason, completed_at=_now(), result=claim.result)
    with store_lock(store, "queue"):
        current = ReviewClaim.model_validate_json(store.get_meta(CLAIM_META) or "{}")
        if (current.attempt_id != claim.attempt_id or current.job.get("conversation_id") != conv
                or current.job.get("review_id") != job.get("review_id")):
            raise ValueError("Review claim changed before acknowledgment")
        pending = _list(store, PENDING_META)
        superseded = []
        if claim.pending is not None:
            superseded = [p for p in pending if p.get("conversation_id") == conv
                          and p.get("review_id") != job.get("review_id")]
            pending = [p for p in pending if p.get("conversation_id") != conv] + [claim.pending]
            receipt.discarded = [DiscardedReview(review_id=p["review_id"], scope=p.get("scope"))
                                 for p in superseded if isinstance(p.get("review_id"), str) and p["review_id"]]
        # All adaptive facts share the exact terminal commit and claim identity.
        control = adaptive.read_control(store)
        clock = adaptive.now()
        adaptive.apply_outcome(control, settings, str(store.db_path), conv,
                               str(job.get("review_id") or claim.attempt_id), outcome, clock)
        evicted = pending[:-max(1, config.sim.max_queue)]
        pending = pending[-max(1, config.sim.max_queue):]
        for previous in superseded + evicted:
            adaptive.record_advice(control, settings, previous, status="discarded",
                                   reason="superseded" if previous in superseded else "saturation", clock=clock)
        if claim.pending is not None:
            adaptive.record_advice(control, settings, claim.pending, clock=clock)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            adaptive.put_control(store, control, settings)
            if claim.pending is not None:
                _put(store, PENDING_META, pending)
            if conv:
                state_key = "supervisor.state." + conv
                previous = json.loads(store.get_meta(state_key) or "{}")
                updated = {**previous, "state": state, "reason": reason, "updated_at": receipt.completed_at}
                if health_state in {"completed", "reviewed_quiet"}:
                    updated["reviewed_at"] = receipt.completed_at
                _put(store, state_key, updated)
            # One latest terminal receipt per conversation, not a history/retry ledger.
            _put(store, "sim.receipt." + conv, receipt.model_dump())
            store.conn.execute("DELETE FROM meta WHERE key=?", (CLAIM_META,))
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
    return superseded


def flush_receipts(store) -> None:
    """Replay terminal health output after an interrupted post-commit handoff."""
    from .supervisor import record_health
    for row in store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'sim.receipt.%'").fetchall():
        receipt = ReviewReceipt.model_validate_json(row["value"])
        if not receipt.health_pending:
            continue
        outcomes = [
            record_health(
                discarded.scope, "review", state="discarded", reason="superseded",
                source="worker", review_id=discarded.review_id,
                event_id=discarded.review_id + ":discarded:superseded")
            for discarded in receipt.discarded
        ]
        outcomes.append(record_health(
            receipt.scope, "review", state=receipt.health_state, reason=receipt.health_reason,
            source="worker", review_id=receipt.review_id,
            event_id=str(receipt.review_id or receipt.attempt_id) + ":result"))
        settled = all(outcome is not False for outcome in outcomes)
        # A receipt whose health record did not land stays pending; the
        # event ids make a later replay idempotent.
        if not settled:
            continue
        receipt.health_pending = False
        store.set_meta(row["key"], receipt.model_dump_json())
