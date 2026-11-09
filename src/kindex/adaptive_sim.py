"""Store-local Sim cadence, finite controls and honest advice receipts.

Queue ownership stays in sim_queue. Transaction helpers here never commit: the
caller commits control changes with the corresponding queue transition.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import time
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONTROL_META = "sim.adaptive.control"
PROVIDER_FAILURES = frozenset({
    "invalid_output", "llm_error", "sim_command_error", "sim_command_failed",
    "subscription_provider_failed", "subscription_review_failed",
    "ollama_response_invalid", "ollama_http_failed", "ollama_timeout", "ollama_review_failed",
})
Timestamp = Annotated[float, Field(ge=0, le=4102444800, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, strict=True)]


class AdaptiveStateError(ValueError):
    """Control metadata is unusable; admission must fail closed."""


class PersistentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Override(PersistentModel):
    mode: Literal["attention", "backoff"]
    created_at: Timestamp
    expires_at: Timestamp

    @model_validator(mode="after")
    def finite_lifetime(self):
        if not 0 < self.expires_at - self.created_at <= 86400:
            raise ValueError("invalid_override_lifetime")
        return self


class ConversationControl(PersistentModel):
    next_due_at: Timestamp = 0.0
    last_admitted_at: Timestamp = 0.0
    quiet_streak: Count = 0
    last_outcome_id: str = ""
    override: Override | None = None


class AdviceReceipt(PersistentModel):
    review_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    state: Literal["generated", "emitted", "acknowledged", "expired", "discarded"]
    generated_at: Timestamp
    expires_at: Timestamp
    emitted_at: Timestamp | None = None
    acknowledged_at: Timestamp | None = None
    reason: str = ""


class ControlState(PersistentModel):
    version: Literal[1] = 1
    failures: Count = 0
    circuit_until: Timestamp = 0.0
    probe_id: str = ""
    override: Override | None = None
    conversations: dict[str, ConversationControl] = Field(default_factory=dict)
    receipts: list[AdviceReceipt] = Field(default_factory=list)


def now() -> float:
    return time.time()


def _scope(conversation_id):
    if conversation_id is not None and (not isinstance(conversation_id, str) or
            not conversation_id or len(conversation_id) > 512 or
            any(char.isspace() or unicodedata.category(char).startswith("C") for char in conversation_id)):
        raise ValueError("invalid_conversation_id")
    return conversation_id


def read_control(store) -> ControlState:
    raw = store.get_meta(CONTROL_META)
    if raw is None:
        return ControlState()
    try:
        return ControlState.model_validate_json(raw)
    except (ValueError, TypeError, ValidationError) as error:
        raise AdaptiveStateError("invalid_adaptive_state") from error


def put_control(store, state, settings):
    from .sim_queue import _put
    state.receipts = state.receipts[-settings.max_receipts:]
    _put(store, CONTROL_META, state.model_dump())


def _expire(state, clock):
    for receipt in state.receipts:
        if receipt.state in {"generated", "emitted"} and (
                clock >= receipt.expires_at or receipt.generated_at > clock):
            receipt.state, receipt.reason = "expired", "ttl"


def _active(override, clock):
    return override if override and override.created_at <= clock < override.expires_at else None


def effective_override(state, conversation_id, clock):
    local = state.conversations.get(conversation_id)
    overrides = [_active(state.override, clock), _active(local.override, clock) if local else None]
    active = [item for item in overrides if item is not None]
    return next((item for item in active if item.mode == "backoff"), active[-1] if active else None)


def bound_clock(state, settings, clock):
    """Persist finite deadlines after a backwards jump, without rescheduling jitter."""
    state.circuit_until = min(state.circuit_until, clock + settings.circuit_cooldown_seconds)
    for control in state.conversations.values():
        control.last_admitted_at = min(control.last_admitted_at, clock)
        control.next_due_at = min(control.next_due_at, clock + settings.max_interval_seconds)
    for override in [state.override] + [c.override for c in state.conversations.values()]:
        if override is not None and override.created_at > clock:
            override.created_at = clock
            override.expires_at = min(override.expires_at, clock + settings.max_override_seconds)


def admission_reason(state, settings, conversation_id, *, event="activity", cadence=True, clock=None):
    clock = now() if clock is None else clock
    bound_clock(state, settings, clock)
    override = effective_override(state, conversation_id, clock)
    if override and override.mode == "backoff":
        return "backoff"
    if settings.enabled and state.circuit_until > clock:
        # A backwards clock cannot turn a finite cooldown into an indefinite one.
        if state.circuit_until - clock > settings.circuit_cooldown_seconds:
            state.circuit_until = clock + settings.circuit_cooldown_seconds
        return "circuit_open"
    if not cadence or not settings.enabled:
        return ""
    control = state.conversations.get(conversation_id)
    if control is None:
        return ""
    due = min(control.next_due_at, clock + settings.max_interval_seconds)
    last = min(control.last_admitted_at, clock)
    if event in {"goal_changed", "tool_error", "user_attention"} or (override and override.mode == "attention"):
        due = min(due, last + settings.min_interval_seconds)
        control.next_due_at = due
    return "cadence" if clock < due else ""


def admitted(state, settings, conversation_id, clock):
    control = state.conversations.setdefault(conversation_id, ConversationControl())
    control.last_admitted_at = clock
    control.next_due_at = clock + settings.min_interval_seconds


def apply_outcome(state, settings, store_id, conversation_id, outcome_id, outcome, clock):
    control = state.conversations.setdefault(conversation_id, ConversationControl())
    if control.last_outcome_id == outcome_id:
        return
    control.last_outcome_id = outcome_id
    if state.probe_id == outcome_id:
        state.probe_id = ""
    if not settings.enabled:
        return
    if outcome in {"quiet", "advisory"}:
        state.failures, state.circuit_until, state.probe_id = 0, 0.0, ""
        control.quiet_streak = control.quiet_streak + 1 if outcome == "quiet" else 0
        power = min(30, control.quiet_streak // settings.quiet_hysteresis)
        interval = min(settings.max_interval_seconds, settings.base_interval_seconds * (2 ** power))
        digest = hashlib.sha256(f"{store_id}:{conversation_id}:{outcome_id}".encode()).digest()
        jitter = (int.from_bytes(digest[:8], "big") / (2**64 - 1) * 2 - 1) * settings.jitter_fraction
        interval = min(settings.max_interval_seconds, max(settings.min_interval_seconds, interval * (1 + jitter)))
        control.next_due_at = max(min(control.last_admitted_at, clock) + settings.min_interval_seconds, clock + interval)
    elif outcome == "failure":
        state.failures += 1
        control.quiet_streak = 0
        if state.failures >= settings.failure_threshold:
            state.circuit_until = clock + settings.circuit_cooldown_seconds


def prepare_dispatch(store, config, conversation_id, review_id):
    """Caller owns worker lock; reserve a single cooldown probe under queue lock."""
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        state = read_control(store)
        clock, settings = now(), config.sim.adaptive
        reason = admission_reason(state, settings, conversation_id, cadence=False, clock=clock)
        if reason:
            # Persist a backwards-clock bound once, rather than extending it on each read.
            store.conn.execute("BEGIN IMMEDIATE")
            try:
                put_control(store, state, settings)
                store.conn.commit()
            except BaseException:
                store.conn.rollback()
                raise
            return reason
        if settings.enabled and state.failures >= settings.failure_threshold:
            state.probe_id = review_id
            state.circuit_until = clock + settings.circuit_cooldown_seconds
            store.conn.execute("BEGIN IMMEDIATE")
            try:
                put_control(store, state, settings)
                store.conn.commit()
            except BaseException:
                store.conn.rollback()
                raise
        return ""


def _status(state, config, conversation_id, clock):
    control = state.conversations.get(conversation_id) if conversation_id is not None else None
    due = control.next_due_at if control else (min((c.next_due_at for c in state.conversations.values()), default=None)
                                                if conversation_id is None else None)
    override = effective_override(state, conversation_id, clock)
    return {"enabled": config.sim.adaptive.enabled,
            "next_due_at": min(due, clock + config.sim.adaptive.max_interval_seconds) if due is not None else None,
            "quiet_streak": control.quiet_streak if control else 0, "failures": state.failures,
            "circuit_until": state.circuit_until, "override": override.model_dump() if override else None,
            "receipts": [r.model_dump() for r in state.receipts
                         if conversation_id is None or r.conversation_id == conversation_id]}


def control_status(store, config, *, conversation_id=None):
    _scope(conversation_id)
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        try:
            state = read_control(store)
        except AdaptiveStateError:
            return {"enabled": config.sim.adaptive.enabled, "ok": False, "reason": "invalid_adaptive_state",
                    "next_due_at": None, "quiet_streak": 0, "failures": 0, "circuit_until": None,
                    "override": None, "receipts": []}
        clock = now()
        _expire(state, clock)
        bound_clock(state, config.sim.adaptive, clock)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            put_control(store, state, config.sim.adaptive)
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        return _status(state, config, conversation_id, clock)


def set_override(store, config, *, mode, duration_seconds, conversation_id=None):
    _scope(conversation_id)
    if mode not in {"attention", "backoff"}:
        raise ValueError("invalid_control_mode")
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= config.sim.adaptive.max_override_seconds:
        raise ValueError("invalid_duration_seconds")
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        state, clock = read_control(store), now()
        override = Override(mode=mode, created_at=clock, expires_at=clock + duration_seconds)
        if conversation_id is None:
            state.override = override
        else:
            state.conversations.setdefault(conversation_id, ConversationControl()).override = override
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            put_control(store, state, config.sim.adaptive)
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        return {"ok": True, **_status(state, config, conversation_id, clock)}


def clear_override(store, *, conversation_id=None):
    _scope(conversation_id)
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        state = read_control(store)
        if conversation_id is None:
            state.override = None
        elif conversation_id in state.conversations:
            state.conversations[conversation_id].override = None
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            put_control(store, state, store.config.sim.adaptive)
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        return {"ok": True, "override": None}


def generation_time(item, clock, ttl):
    generated = item.get("generated_at")
    if generated is None:
        try:
            parsed = datetime.fromisoformat(str(item.get("at", "")).replace("Z", "+00:00"))
            generated = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            return None
    if (type(generated) not in {int, float} or not math.isfinite(generated) or generated < 0 or generated > clock):
        return None
    expires = item.get("expires_at", generated + ttl)
    if (type(expires) not in {int, float} or not math.isfinite(expires) or expires < generated):
        return None
    return float(generated), float(min(expires, generated + ttl))


def record_advice(state, settings, item, *, status="generated", reason="", clock=None):
    clock = now() if clock is None else clock
    review, conversation = item.get("review_id"), item.get("conversation_id")
    if not isinstance(review, str) or not review or not isinstance(conversation, str) or not conversation:
        return None
    timing = generation_time(item, clock, settings.advice_ttl_seconds)
    generated, expires = timing or (clock, clock)
    if status in {"generated", "emitted"} and (timing is None or clock >= expires):
        status, reason = "expired", "ttl"
    receipt = next((r for r in state.receipts if r.review_id == review and r.conversation_id == conversation), None)
    if receipt is None:
        receipt = AdviceReceipt(review_id=review, conversation_id=conversation, state=status,
                                generated_at=generated, expires_at=expires, reason=reason)
        state.receipts.append(receipt)
    else:
        receipt.state, receipt.reason = status, reason
    if status == "emitted":
        receipt.emitted_at = clock
    state.receipts = state.receipts[-settings.max_receipts:]
    return receipt


def acknowledge_advice(store, *, conversation_id, review_id):
    _scope(conversation_id)
    if conversation_id is None or not isinstance(review_id, str) or not review_id.strip() or review_id != review_id.strip():
        raise ValueError("invalid_advice_identity")
    from .supervisor import store_lock
    with store_lock(store, "queue"):
        state, clock = read_control(store), now()
        _expire(state, clock)
        receipt = next((r for r in state.receipts if r.conversation_id == conversation_id and r.review_id == review_id), None)
        if receipt is None:
            return {"ok": False, "reason": "unknown_review"}
        if receipt.state == "acknowledged":
            return {"ok": True, "state": "acknowledged"}
        valid = receipt.state == "emitted"
        if valid:
            receipt.state, receipt.acknowledged_at = "acknowledged", clock
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            put_control(store, state, store.config.sim.adaptive)
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        return {"ok": True, "state": "acknowledged"} if valid else {"ok": False, "reason": receipt.state}
