"""The durable run-row protocol: stage, claim, renew, checkpoint, settle, release.

A durable run is one database row that a worker drives one wake at a time.
Every kind of run (the Task coordinator run and its delegated child runs)
shares the same fencing:

* A wake is an outbox row with one stable dedupe key per run. Staging a wake
  replaces that row and gives the run a fresh opaque ``wake_generation``.
* A worker claims exactly one wake generation for a lease. A newer wake never
  steals a live claim, and a stale delivery can never claim the successor.
* The owner renews its lease during provider I/O, checkpoints with a
  compare-and-swap on ``state_version``, ACKs only the generation it owns and
  releases only its own claim.

This module is the only writer of a run row's claim and wake fields
(``claim_token``, ``claim_generation``, ``claim_expires_at``,
``wake_generation``); ``tests/test_durable_run_protocol.py`` guards that
repo-wide. Each run kind declares a :class:`DurableRunSpec` and keeps its own
admission (Task, Workspace, factory identity) in the ``admit`` callback.

No function commits; the caller owns the transaction and its lock order
(Task -> Run -> outbox event, as admission and the terminal write).

Locking:

* ``stage_run_wake``, ``claim_run_wake``, ``renew_run_claim``,
  ``checkpoint_run``, ``settle_run_wake``, ``release_run_claim``,
  ``holds_run_claim`` and ``record_run_wake_failure`` lock the run row
  themselves (``SELECT ... FOR UPDATE``, ``populate_existing``); the ones that
  touch the wake row then lock the outbox event.
* ``clear_run_claim_and_wake`` and ``reopen_run`` take a run row the caller
  has already locked in this transaction and never re-select it.
* ``close_run_wake`` locks only the run's pending outbox event; the caller
  locks the run first when it also changes it.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent


# The furthest a single wake may be scheduled (a parked wait_until re-parks
# on wake if its own time is further out).
DURABLE_RUN_MAX_WAKE_DELAY = timedelta(days=90)
DEFAULT_WAKE_FAILURES_KEY = "wake_failures"

UNSET: Any = object()


@dataclass(frozen=True)
class DurableRunSpec:
    """How one kind of run row is located, woken and validated."""

    # The ORM model of the run row.
    model: type
    # The unique column a run is addressed by (also the outbox aggregate id).
    key_column: Any
    outbox_event: str
    outbox_version: int
    # The stable outbox dedupe key of a run's single wake row.
    dedupe_key: Callable[[str], str]
    # (key, wake_generation, reason) -> outbox payload.
    payload: Callable[[str, str, str], dict[str, Any]]
    # The payload field carrying the key, checked when a wake is claimed.
    payload_key: str
    # Human-readable name used in error messages ("Task coordinator").
    label: str
    phases: frozenset[str]
    terminal_phases: frozenset[str]
    # ``last_error`` of a staged, undelivered wake row.
    wake_pending_error: str
    # Extra envelope fields read from the locked run row when a wake is
    # staged (a child run's ``task_id``); the spec's own payload wins.
    payload_fields: Callable[[Any], dict[str, Any]] | None = None

    def key_of(self, run: Any) -> str:
        return str(getattr(run, self.key_column.key))


@dataclass(frozen=True)
class DurableRunWake:
    event_id: str
    key: str
    wake_generation: str
    available_at: datetime


@dataclass(frozen=True)
class DurableRunClaim:
    run_id: str
    key: str
    event_id: str
    wake_generation: str
    claim_token: str
    expires_at: datetime
    state_version: int


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def claim_is_live(run: Any, *, now: datetime | None = None) -> bool:
    """Whether some owner holds an unexpired claim on this run."""

    expiry = _aware(getattr(run, "claim_expires_at", None))
    return bool(
        getattr(run, "claim_token", None)
        and expiry is not None
        and expiry > (now or datetime.now(timezone.utc))
    )


def live_claim_matches(run: Any, token: str | None, generation: str | None) -> bool:
    """Whether exactly this owner (token + generation) holds a live claim."""

    return bool(
        token and generation
        and run.claim_token == token
        and run.claim_generation == generation
        and claim_is_live(run)
    )


def holds_current_wake(run: Any, token: str | None, generation: str | None) -> bool:
    """This owner's live claim is on the run's current wake generation."""

    return bool(
        run is not None
        and live_claim_matches(run, token, generation)
        and run.wake_generation == generation
    )


async def lock_run(db: AsyncSession, spec: DurableRunSpec, key: str) -> Any | None:
    return (await db.execute(
        select(spec.model)
        .where(spec.key_column == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


def bounded_wake_time(
    now: datetime,
    *,
    delay_seconds: Any,
    available_at: datetime | None,
) -> datetime:
    """A finite delivery time in [now, now + DURABLE_RUN_MAX_WAKE_DELAY]."""

    latest = now + DURABLE_RUN_MAX_WAKE_DELAY
    if available_at is not None:
        return min(max(_aware(available_at), now), latest)
    try:
        seconds = float(delay_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if math.isnan(seconds) or seconds <= 0:
        return now
    if math.isinf(seconds) or seconds >= DURABLE_RUN_MAX_WAKE_DELAY.total_seconds():
        return latest
    return now + timedelta(seconds=seconds)


async def _pending_wake_event(
    db: AsyncSession, spec: DurableRunSpec, key: str,
) -> RuntimeOutboxEvent | None:
    return (await db.execute(
        select(RuntimeOutboxEvent)
        .where(
            RuntimeOutboxEvent.dedupe_key == spec.dedupe_key(key),
            RuntimeOutboxEvent.event_type == spec.outbox_event,
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _locked_event(db: AsyncSession, event_id: str) -> RuntimeOutboxEvent | None:
    return (await db.execute(
        select(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def stage_run_wake(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    reason: str,
    delay_seconds: float = 0,
    expected_state_version: int | None = None,
    claim_token: str | None = None,
    claim_generation: str | None = None,
    if_pending: bool = False,
    available_at: datetime | None = None,
) -> DurableRunWake | None:
    """Replace the run's single wake inside the caller's transaction.

    Anyone may stage without a claim (a child or job settling). An owner
    staging its own next wake passes both token and generation; a stale owner
    cannot replace a successor wake. Replacing the wake never clears a live
    claim. ``available_at`` is an absolute alternative to ``delay_seconds``;
    either is bounded to [now, now + 90 days]. ``if_pending`` keeps a wake
    that is already staged for the current generation.
    """

    return await _stage_locked_run_wake(
        db, spec, await lock_run(db, spec, key),
        key=key,
        reason=reason,
        delay_seconds=delay_seconds,
        expected_state_version=expected_state_version,
        claim_token=claim_token,
        claim_generation=claim_generation,
        if_pending=if_pending,
        available_at=available_at,
    )


async def _stage_locked_run_wake(
    db: AsyncSession,
    spec: DurableRunSpec,
    run: Any | None,
    *,
    key: str,
    reason: str,
    delay_seconds: float = 0,
    expected_state_version: int | None = None,
    claim_token: str | None = None,
    claim_generation: str | None = None,
    if_pending: bool = False,
    available_at: datetime | None = None,
) -> DurableRunWake | None:
    """``stage_run_wake`` on a run row this transaction already locked."""

    if run is None or run.phase in spec.terminal_phases:
        return None
    if expected_state_version is not None and run.state_version != expected_state_version:
        raise ValueError(f"{spec.label} state version changed")
    if (claim_token is None) != (claim_generation is None):
        raise ValueError(f"{spec.label} claim token and generation must be supplied together")
    if claim_token is not None and not holds_current_wake(run, claim_token, claim_generation):
        raise ValueError(f"{spec.label} execution claim or wake generation was lost")
    if if_pending and run.wake_generation:
        pending = await _pending_wake_event(db, spec, key)
        pending_payload = pending.payload if pending is not None and isinstance(pending.payload, dict) else {}
        if pending is not None and pending_payload.get("wake_generation") == run.wake_generation:
            return DurableRunWake(
                str(pending.id), key, str(run.wake_generation), pending.available_at,
            )
    now = datetime.now(timezone.utc)
    available_at = bounded_wake_time(now, delay_seconds=delay_seconds, available_at=available_at)
    wake_generation = generate_ulid()
    payload = spec.payload(key, wake_generation, str(reason or "state_transition")[:160])
    if spec.payload_fields is not None:
        payload = {**spec.payload_fields(run), **payload}
    insert_statement = pg_insert(RuntimeOutboxEvent).values(
        id=generate_ulid(),
        event_type=spec.outbox_event,
        aggregate_id=key,
        dedupe_key=spec.dedupe_key(key),
        payload=payload,
        attempt_count=0,
        available_at=available_at,
        delivered_at=None,
        last_error=spec.wake_pending_error,
        created_at=now,
    )
    statement = insert_statement.on_conflict_do_update(
        index_elements=[RuntimeOutboxEvent.dedupe_key],
        set_={
            "event_type": spec.outbox_event,
            "aggregate_id": key,
            "payload": payload,
            "attempt_count": 0,
            "available_at": available_at,
            "delivered_at": None,
            "last_error": spec.wake_pending_error,
        },
    ).returning(RuntimeOutboxEvent.id)
    persisted_event_id = str((await db.execute(statement)).scalar_one())
    run.wake_generation = wake_generation
    run.state_version += 1
    return DurableRunWake(persisted_event_id, key, wake_generation, available_at)


def require_claim_request(spec: DurableRunSpec, claim_token: str, lease_seconds: float) -> None:
    if not claim_token or lease_seconds <= 0:
        raise ValueError(f"{spec.label} claim requires a token and positive lease")


async def claim_run_wake(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    event_id: str,
    wake_generation: str,
    claim_token: str,
    lease_seconds: float,
    admit: Callable[[Any], str | None] | None = None,
) -> tuple[DurableRunClaim | None, str]:
    """Claim only the run's current wake generation.

    ``admit`` sees the locked row before any claim check and returns a
    refusal reason (or raises) when this run kind's own admission fails.
    The owner must renew during provider I/O; a failed renewal stops it.
    """

    require_claim_request(spec, claim_token, lease_seconds)
    run = await lock_run(db, spec, key)
    if run is None:
        return None, "missing_run"
    if admit is not None:
        refusal = admit(run)
        if refusal:
            return None, refusal
    if run.phase in spec.terminal_phases:
        return None, "terminal_run"
    event = await _locked_event(db, event_id)
    if event is None or event.delivered_at is not None:
        return None, "missing_or_delivered_event"
    payload = event.payload if isinstance(event.payload, dict) else {}
    if (
        event.event_type != spec.outbox_event
        or event.aggregate_id != key
        or event.dedupe_key != spec.dedupe_key(key)
        or payload.get("version") != spec.outbox_version
        or payload.get(spec.payload_key) != key
        or payload.get("wake_generation") != wake_generation
        or run.wake_generation != wake_generation
    ):
        return None, "stale_generation"
    now = datetime.now(timezone.utc)
    if claim_is_live(run, now=now):
        if run.claim_token != claim_token or run.claim_generation != wake_generation:
            return None, "claim_held"
    expires_at = now + timedelta(seconds=lease_seconds)
    run.claim_token = claim_token
    run.claim_generation = wake_generation
    run.claim_expires_at = expires_at
    run.state_version += 1
    return DurableRunClaim(
        run.id, key, event_id, wake_generation, claim_token,
        expires_at, run.state_version,
    ), "claimed"


async def renew_run_claim(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    claim_token: str,
    claim_generation: str,
    lease_seconds: float,
) -> bool:
    """Heartbeat the exact owner. False means the worker must stop work."""

    if lease_seconds <= 0:
        raise ValueError(f"{spec.label} lease must be positive")
    run = await lock_run(db, spec, key)
    if run is None or not live_claim_matches(run, claim_token, claim_generation):
        return False
    run.claim_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    return True


async def checkpoint_run(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    claim_token: str,
    claim_generation: str,
    expected_state_version: int,
    checkpoint: dict[str, Any] | None | object = UNSET,
    phase: str | object = UNSET,
    apply: Callable[[Any], None] | None = None,
) -> int | None:
    """CAS one completed round, an optional phase, and ``apply``'s own fields.

    Returns the new version, or None after any claim/version loss; the owner
    must stop provider work on None. ``apply`` runs on the locked row after
    the CAS passed and may raise to reject the transition.
    """

    run = await lock_run(db, spec, key)
    if (
        run is None or run.state_version != expected_state_version
        or not live_claim_matches(run, claim_token, claim_generation)
    ):
        return None
    if checkpoint is not UNSET:
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError(f"{spec.label} checkpoint must be a JSON object or null")
        run.checkpoint = checkpoint
    if phase is not UNSET:
        if phase not in spec.phases:
            raise ValueError(f"Unknown {spec.label} phase")
        run.phase = phase
    if apply is not None:
        apply(run)
    run.state_version += 1
    return run.state_version


async def settle_run_wake(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    event_id: str,
    wake_generation: str,
    claim_token: str,
) -> bool:
    """ACK only the generation actually owned by this live execution."""

    run = await lock_run(db, spec, key)
    if not holds_current_wake(run, claim_token, wake_generation):
        return False
    event = await _locked_event(db, event_id)
    payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
    if (
        event is None or event.delivered_at is not None
        or event.event_type != spec.outbox_event
        or event.aggregate_id != key
        or event.dedupe_key != spec.dedupe_key(key)
        or payload.get("wake_generation") != wake_generation
    ):
        return False
    event.delivered_at = datetime.now(timezone.utc)
    event.last_error = None
    run.wake_generation = None
    run.state_version += 1
    return True


def _clear_claim(run: Any) -> None:
    run.claim_token = None
    run.claim_generation = None
    run.claim_expires_at = None


async def release_run_claim(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    claim_token: str,
    claim_generation: str,
) -> bool:
    """Release only this owner's claim; an expired/stolen claim is untouched."""

    run = await lock_run(db, spec, key)
    if run is None or not live_claim_matches(run, claim_token, claim_generation):
        return False
    _clear_claim(run)
    run.state_version += 1
    return True


def clear_run_claim_and_wake(run: Any) -> None:
    """Drop any claim and the current wake of a run the caller has locked.

    For a lifecycle transition decided outside the owner (terminal reconcile,
    a retry or rebind that restages): a previous owner can no longer
    checkpoint, ACK or release, and a queued delivery can no longer claim.
    Bumps ``state_version`` once.
    """

    _clear_claim(run)
    run.wake_generation = None
    run.state_version += 1


async def close_run_wake(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    last_error: str,
) -> bool:
    """Mark the run's undelivered wake row delivered with ``last_error``."""

    pending = await _pending_wake_event(db, spec, key)
    if pending is None:
        return False
    pending.delivered_at = datetime.now(timezone.utc)
    pending.last_error = last_error
    return True


async def reopen_run(
    db: AsyncSession,
    spec: DurableRunSpec,
    run: Any,
    *,
    phase: str,
    reason: str,
) -> DurableRunWake | None:
    """Move a parked run the caller has locked back to ``phase`` and stage its wake.

    The row is used as passed (with any unflushed changes the caller made),
    never re-selected. Any leftover claim is dropped (a parked run has no
    live owner) and the fresh wake generation fences off every earlier
    delivery.
    """

    if phase not in spec.phases or phase in spec.terminal_phases:
        raise ValueError(f"{spec.label} cannot reopen into phase {phase!r}")
    run.phase = phase
    _clear_claim(run)
    return await _stage_locked_run_wake(db, spec, run, key=spec.key_of(run), reason=reason)


async def holds_run_claim(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    claim_token: str,
    claim_generation: str,
) -> bool:
    """Lock the run and confirm this owner holds its current wake's live claim."""

    return holds_current_wake(await lock_run(db, spec, key), claim_token, claim_generation)


async def record_run_wake_failure(
    db: AsyncSession,
    spec: DurableRunSpec,
    *,
    key: str,
    claim_token: str,
    claim_generation: str,
    error: BaseException,
    failures_key: str = DEFAULT_WAKE_FAILURES_KEY,
) -> tuple[int, dict[str, Any]]:
    """Count a crash against this exact wake; (0, {}) when superseded.

    The counter lives in the checkpoint and is keyed by wake generation, so a
    successful round (which rewrites the checkpoint) or a new wake resets it.
    Returns the new count and the checkpoint written.
    """

    run = await lock_run(db, spec, key)
    if not holds_current_wake(run, claim_token, claim_generation):
        return 0, {}
    checkpoint = copy.deepcopy(run.checkpoint or {})
    prior = checkpoint.get(failures_key)
    count = (
        int(prior.get("count") or 0) + 1
        if isinstance(prior, dict) and prior.get("wake_generation") == claim_generation
        else 1
    )
    checkpoint[failures_key] = {
        "wake_generation": claim_generation,
        "count": count,
        "exception": type(error).__name__,
        "error": str(error)[:1000],
    }
    run.checkpoint = checkpoint
    run.state_version += 1
    return count, checkpoint
