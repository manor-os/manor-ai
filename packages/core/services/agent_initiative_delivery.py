"""Idempotent Chat and Notification projection for Agent Initiatives."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update

from packages.core.contracts.agent_initiative import (
    DecisionPolicy,
    DeliveryPolicy,
    InterruptionPolicy,
)
from packages.core.models.agent_initiative import (
    AgentInitiative,
    AgentInitiativeDelivery,
    AgentInitiativeRun,
    AgentInitiativeState,
)
from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import AgentSubscription
from packages.core.services.agent_initiative_decision import (
    InitiativeAdmissionRejected,
    _exact_runtime_authority,
    initiative_decision_fingerprint,
    initiative_result_revision_matches,
    initiative_task_marker,
    parse_initiative_decision,
)


logger = logging.getLogger(__name__)


_TERMINAL_DELIVERY_STATUSES = frozenset({
    "accepted", "delivered", "quarantined", "suppressed",
})
_RETRYABLE_DELIVERY_STATUSES = frozenset({"pending", "error"})
_NOTIFICATION_SUMMARY_MAX_CHARS = 500
_DELIVERY_LEASE = timedelta(minutes=5)
_MAX_DELIVERY_ATTEMPTS = 5
_DELIVERY_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)


class _DeliveryClaimLost(RuntimeError):
    """A stale delivery worker no longer owns the durable intent."""


class _DeliveryDeferred(RuntimeError):
    """The delivery exists but is not due or claimed by this worker."""


class _PermanentDeliveryError(RuntimeError):
    """Persisted delivery context is invalid and cannot heal on retry."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code


@dataclass(frozen=True)
class InitiativeDeliveryClaim:
    run_id: str
    kind: str
    claim_token: str


@dataclass(frozen=True)
class _DeliveryContext:
    initiative: AgentInitiative
    state: AgentInitiativeState
    run: AgentInitiativeRun
    delivery: AgentInitiativeDelivery
    decision_policy: DecisionPolicy
    delivery_policy: DeliveryPolicy
    interruption_policy: InterruptionPolicy


def _eligible_delivery(when: datetime):
    return or_(
        and_(
            AgentInitiativeDelivery.status.in_(
                tuple(_RETRYABLE_DELIVERY_STATUSES)
            ),
            AgentInitiativeDelivery.next_attempt_at <= when,
        ),
        and_(
            AgentInitiativeDelivery.status == "processing",
            or_(
                AgentInitiativeDelivery.locked_until.is_(None),
                AgentInitiativeDelivery.locked_until <= when,
            ),
        ),
    )


def _delivery_retry_at(when: datetime, attempt_count: int) -> datetime:
    index = max(
        0,
        min(attempt_count - 1, len(_DELIVERY_RETRY_DELAYS) - 1),
    )
    return when + _DELIVERY_RETRY_DELAYS[index]


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clock_in_window(current: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _validate_notification_lineage(
    notification: Notification,
    *,
    initiative_id: str,
    run_id: str,
    message_id: str | None,
    agent_subscription_id: str,
    task_id: str | None,
    entity_id: str,
    owner_user_id: str,
    workspace_id: str | None,
    idempotency_key: str,
    agent_id: str | None = None,
) -> None:
    """Reject a reused Notification unless every persisted owner key matches."""

    meta = notification.meta if isinstance(notification.meta, dict) else {}
    if (
        notification.type != "agent_initiative_update"
        or str(notification.entity_id or "") != str(entity_id or "")
        or str(notification.user_id or "") != str(owner_user_id or "")
        or str(notification.workspace_id or "") != str(workspace_id or "")
        or str(notification.idempotency_key or "") != str(idempotency_key or "")
        or str(meta.get("initiative_id") or "") != str(initiative_id or "")
        or str(meta.get("initiative_run_id") or "") != str(run_id or "")
        or str(meta.get("message_id") or "") != str(message_id or "")
        or str(meta.get("agent_subscription_id") or "")
        != str(agent_subscription_id or "")
        or str(meta.get("task_id") or "") != str(task_id or "")
        or (
            agent_id is not None
            and str(meta.get("agent_id") or "") != str(agent_id or "")
        )
    ):
        raise _PermanentDeliveryError(
            "notification_idempotency_conflict",
            "Existing Notification does not match the Initiative delivery lineage",
        )


async def _reconcile_exhausted_notification(
    db,
    *,
    run_id: str,
    candidate: AgentInitiativeDelivery,
    rows: list[AgentInitiativeDelivery],
    when: datetime,
) -> bool:
    """Accept a durable Notification parent left by a final-attempt crash."""

    lineage = (await db.execute(
        select(
            AgentInitiativeRun.initiative_id,
            AgentInitiativeRun.task_id,
            AgentInitiative.entity_id,
            AgentInitiative.owner_user_id,
            AgentInitiative.workspace_id,
            AgentInitiative.agent_subscription_id,
            AgentSubscription.agent_id,
        )
        .join(
            AgentInitiative,
            AgentInitiative.id == AgentInitiativeRun.initiative_id,
        )
        .join(
            AgentSubscription,
            AgentSubscription.id == AgentInitiative.agent_subscription_id,
        )
        .where(AgentInitiativeRun.id == run_id)
    )).one_or_none()
    if lineage is None:
        return False
    (
        initiative_id,
        task_id,
        entity_id,
        owner_user_id,
        workspace_id,
        agent_subscription_id,
        agent_id,
    ) = lineage
    if (
        candidate.kind != "notification"
        or str(candidate.recipient_user_id or "") != str(owner_user_id or "")
    ):
        raise _PermanentDeliveryError(
            "notification_idempotency_conflict",
            "Notification delivery recipient does not match its Initiative owner",
        )
    notification = (await db.execute(
        select(Notification).where(
            Notification.entity_id == entity_id,
            Notification.user_id == owner_user_id,
            Notification.idempotency_key == candidate.idempotency_key,
        ).with_for_update()
    )).scalar_one_or_none()
    if notification is None:
        return False

    surface_rows = [row for row in rows if row.kind != "notification"]
    expected_message_id = (
        surface_rows[0].message_id
        if len(surface_rows) == 1 and surface_rows[0].status == "delivered"
        else None
    )
    _validate_notification_lineage(
        notification,
        initiative_id=initiative_id,
        run_id=run_id,
        message_id=expected_message_id,
        agent_subscription_id=agent_subscription_id,
        task_id=task_id,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        idempotency_key=candidate.idempotency_key,
        agent_id=agent_id,
    )
    if expected_message_id is None:
        raise _PermanentDeliveryError(
            "notification_idempotency_conflict",
            "Existing Notification has no delivered Chat lineage",
        )
    outbox_exists = (await db.execute(
        select(NotificationOutboxEvent.id).where(
            NotificationOutboxEvent.notification_id == notification.id,
        ).with_for_update()
    )).scalar_one_or_none()
    if outbox_exists is None:
        raise _PermanentDeliveryError(
            "notification_idempotency_conflict",
            "Existing Notification is missing its durable outbox intent",
        )

    candidate.status = "accepted"
    candidate.notification_id = notification.id
    candidate.error = None
    candidate.suppressed_reason = None
    candidate.locked_until = None
    candidate.claim_token = None
    candidate.delivered_at = when
    return True


async def _claim_locked_delivery_run(
    db,
    *,
    run_id: str,
    when: datetime,
) -> InitiativeDeliveryClaim | None:
    rows = list((await db.execute(
        select(AgentInitiativeDelivery)
        .where(AgentInitiativeDelivery.initiative_run_id == run_id)
        .order_by(AgentInitiativeDelivery.created_at.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    if not rows:
        return None
    surface_rows = [row for row in rows if row.kind != "notification"]
    surface_incomplete = next(
        (
            row
            for row in surface_rows
            if row.status not in _TERMINAL_DELIVERY_STATUSES
        ),
        None,
    )
    candidate = surface_incomplete
    if candidate is None:
        candidate = next(
            (
                row
                for row in rows
                if row.kind == "notification"
                and row.status not in _TERMINAL_DELIVERY_STATUSES
            ),
            None,
        )
    if candidate is None:
        return None
    next_attempt_at = _aware_utc(candidate.next_attempt_at)
    locked_until = _aware_utc(candidate.locked_until)
    due = (
        candidate.status in _RETRYABLE_DELIVERY_STATUSES
        and next_attempt_at is not None
        and next_attempt_at <= when
    ) or (
        candidate.status == "processing"
        and (locked_until is None or locked_until <= when)
    )
    if not due:
        return None
    if int(candidate.attempt_count or 0) >= _MAX_DELIVERY_ATTEMPTS:
        terminal_reason = "delivery_retry_exhausted_after_worker_interruption"
        terminal_error = None
        if candidate.kind == "notification":
            try:
                reconciled = await _reconcile_exhausted_notification(
                    db,
                    run_id=run_id,
                    candidate=candidate,
                    rows=rows,
                    when=when,
                )
            except _PermanentDeliveryError as exc:
                terminal_reason = exc.reason_code
                terminal_error = str(exc)[:2_000]
            else:
                if reconciled:
                    return None
        # The parent Run is already locked, so terminalizing the child here
        # preserves the Run -> Delivery lock order used by normal claims and
        # bounds each sweep to its selected run batch.
        candidate.status = "quarantined"
        candidate.suppressed_reason = terminal_reason
        candidate.error = terminal_error
        candidate.locked_until = None
        candidate.claim_token = None
        candidate.delivered_at = when
        return None
    claim_token = generate_ulid()
    candidate.status = "processing"
    candidate.attempt_count = int(candidate.attempt_count or 0) + 1
    candidate.locked_until = when + _DELIVERY_LEASE
    candidate.claim_token = claim_token
    return InitiativeDeliveryClaim(
        run_id=run_id,
        kind=candidate.kind,
        claim_token=claim_token,
    )


async def claim_initiative_delivery_run(
    session_factory,
    *,
    run_id: str,
    now: datetime | None = None,
) -> InitiativeDeliveryClaim | None:
    """Claim one run's due delivery intents for an initial broker message."""

    when = _aware_utc(now) or datetime.now(timezone.utc)
    async with session_factory() as db:
        run = (await db.execute(
            select(AgentInitiativeRun)
            .where(AgentInitiativeRun.id == run_id)
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        claim = None
        if run is not None:
            claim = await _claim_locked_delivery_run(
                db,
                run_id=run.id,
                when=when,
            )
        await db.commit()
        return claim


def _due_delivery_run_clause(when: datetime) -> tuple[Any, Any]:
    surface_incomplete = (
        select(AgentInitiativeDelivery.id)
        .where(
            AgentInitiativeDelivery.initiative_run_id == AgentInitiativeRun.id,
            AgentInitiativeDelivery.kind != "notification",
            AgentInitiativeDelivery.status.not_in(
                tuple(_TERMINAL_DELIVERY_STATUSES)
            ),
        )
        .exists()
    )
    surface_eligible = (
        select(AgentInitiativeDelivery.id)
        .where(
            AgentInitiativeDelivery.initiative_run_id == AgentInitiativeRun.id,
            AgentInitiativeDelivery.kind != "notification",
            _eligible_delivery(when),
        )
        .exists()
    )
    notification_eligible = (
        select(AgentInitiativeDelivery.id)
        .where(
            AgentInitiativeDelivery.initiative_run_id == AgentInitiativeRun.id,
            AgentInitiativeDelivery.kind == "notification",
            _eligible_delivery(when),
        )
        .exists()
    )
    earliest_due = (
        select(func.min(func.coalesce(
            AgentInitiativeDelivery.locked_until,
            AgentInitiativeDelivery.next_attempt_at,
        )))
        .where(
            AgentInitiativeDelivery.initiative_run_id == AgentInitiativeRun.id,
            _eligible_delivery(when),
        )
        .scalar_subquery()
    )
    return (
        or_(surface_eligible, and_(~surface_incomplete, notification_eligible)),
        earliest_due,
    )


async def pending_initiative_delivery_run_ids(
    session_factory,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> list[str]:
    """List due runs for broker handoff without spending delivery attempts.

    Workers claim the exact delivery step after the broker accepts the task.
    Duplicate handoffs are harmless because the durable claim is fenced.
    """

    when = _aware_utc(now) or datetime.now(timezone.utc)
    batch_size = max(1, min(int(limit), 500))
    due_run, earliest_due = _due_delivery_run_clause(when)
    async with session_factory() as db:
        return list((await db.execute(
            select(AgentInitiativeRun.id)
            .where(due_run)
            .order_by(earliest_due.asc(), AgentInitiativeRun.id.asc())
            .limit(batch_size)
        )).scalars().all())


def initiative_notification_body(message: str | None, *, content_mode: str) -> str:
    """Render a bounded external-safe summary or the configured full body."""

    body = str(message or "").strip()
    if content_mode == "full":
        return body[:20_000]
    if len(body) <= _NOTIFICATION_SUMMARY_MAX_CHARS:
        return body
    return body[: _NOTIFICATION_SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def initiative_notification_link(
    workspace_id: str | None,
    conversation_id: str | None = None,
) -> str:
    """Link a Notification to the Chat surface that owns the durable fact."""

    if conversation_id:
        return f"/chat?conversation={quote(conversation_id, safe='')}"
    if workspace_id:
        return f"/chat?workspace={quote(workspace_id, safe='')}"
    return "/chat/history"


def initiative_quiet_hours_active(
    interruption_policy: InterruptionPolicy,
    *,
    user_preferences: dict | None,
    now: datetime | None = None,
) -> bool:
    """Evaluate custom or inherited quiet hours without severity bypass."""

    quiet = interruption_policy.quiet_hours
    if quiet.mode == "none":
        return False
    if quiet.mode == "custom":
        start_raw, end_raw, timezone_name = quiet.start, quiet.end, quiet.timezone
    else:
        prefs = user_preferences if isinstance(user_preferences, dict) else {}
        notifications = prefs.get("notifications")
        notifications = notifications if isinstance(notifications, dict) else {}
        inherited = notifications.get("quiet_hours")
        inherited = inherited if isinstance(inherited, dict) else {}
        start_raw = inherited.get("from")
        end_raw = inherited.get("to")
        timezone_name = inherited.get("tz") or "UTC"
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return False
    try:
        start = time.fromisoformat(start_raw)
        end = time.fromisoformat(end_raw)
        zone = ZoneInfo(str(timezone_name or "UTC"))
    except (ValueError, ZoneInfoNotFoundError):
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(zone).time()
    return _clock_in_window(current, start, end)


async def _load_locked_context(
    db,
    *,
    run_id: str,
    kind: str,
    claim_token: str,
) -> _DeliveryContext | None:
    initiative_id = (await db.execute(
        select(AgentInitiativeRun.initiative_id).where(
            AgentInitiativeRun.id == run_id,
        )
    )).scalar_one_or_none()
    if initiative_id is None:
        return None
    initiative = (await db.execute(
        select(AgentInitiative)
        .where(AgentInitiative.id == initiative_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if initiative is None:
        return None
    state = (await db.execute(
        select(AgentInitiativeState)
        .where(AgentInitiativeState.initiative_id == initiative.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    run = (await db.execute(
        select(AgentInitiativeRun)
        .where(
            AgentInitiativeRun.id == run_id,
            AgentInitiativeRun.initiative_id == initiative.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    delivery = (await db.execute(
        select(AgentInitiativeDelivery)
        .where(
            AgentInitiativeDelivery.initiative_run_id == run_id,
            AgentInitiativeDelivery.kind == kind,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if delivery is None:
        return None
    if delivery.status not in _TERMINAL_DELIVERY_STATUSES:
        if delivery.status in _RETRYABLE_DELIVERY_STATUSES:
            raise _DeliveryDeferred("delivery is not currently claimed")
        if delivery.status != "processing" or delivery.claim_token != claim_token:
            raise _DeliveryClaimLost("delivery claim is no longer current")
    if state is None or run is None:
        raise _PermanentDeliveryError(
            "invalid_delivery_context",
            "Initiative delivery is missing its state or run",
        )
    try:
        decision_policy = DecisionPolicy.model_validate(
            initiative.decision_policy or {}
        )
        delivery_policy = DeliveryPolicy.model_validate(
            initiative.delivery_policy or {}
        )
        interruption_policy = InterruptionPolicy.model_validate(
            initiative.interruption_policy or {}
        )
    except ValidationError as exc:
        raise _PermanentDeliveryError(
            "invalid_delivery_contract",
            str(exc),
        ) from exc
    return _DeliveryContext(
        initiative=initiative,
        state=state,
        run=run,
        delivery=delivery,
        decision_policy=decision_policy,
        delivery_policy=delivery_policy,
        interruption_policy=interruption_policy,
    )


def _suppress(delivery: AgentInitiativeDelivery, reason: str, now: datetime) -> None:
    delivery.status = "suppressed"
    delivery.suppressed_reason = reason[:2_000]
    delivery.error = None
    delivery.delivered_at = now
    delivery.locked_until = None
    delivery.claim_token = None


def _quarantine(delivery: AgentInitiativeDelivery, reason: str, now: datetime) -> None:
    delivery.status = "quarantined"
    delivery.suppressed_reason = reason[:2_000]
    delivery.error = None
    delivery.delivered_at = now
    delivery.locked_until = None
    delivery.claim_token = None


def _advance_delivery_state(context: _DeliveryContext, now: datetime) -> None:
    decision, error = parse_initiative_decision(context.run.candidate_decision)
    if decision is None or decision.decision != "notify":
        raise _PermanentDeliveryError(
            "invalid_delivery_decision",
            error or "Initiative delivery has no notify decision",
        )
    fingerprint = initiative_decision_fingerprint(decision)
    state = context.state
    window_started = _aware_utc(state.window_started_at)
    if window_started is None or window_started <= now - timedelta(days=1):
        state.window_started_at = now
        state.notification_count = 0
    state.notification_count = int(state.notification_count or 0) + 1
    state.last_fingerprint = fingerprint
    state.last_notified_at = now
    state.cooldown_until = now + timedelta(
        seconds=context.decision_policy.cooldown_seconds
    )
    state.version = int(state.version or 0) + 1


async def _publish_surface_event(
    *,
    entity_id: str,
    owner_user_id: str,
    workspace_id: str | None,
    message: Message,
) -> None:
    """Refresh the exact Chat surface after its durable Message commits."""

    if workspace_id:
        from packages.core.workspace_chat.service import (
            publish_workspace_chat_message_event,
        )

        await publish_workspace_chat_message_event(
            entity_id,
            workspace_id=workspace_id,
            message=message,
        )
        return

    from packages.core.services.realtime import push_conversation_message

    await push_conversation_message(
        owner_user_id,
        {
            "conversation_id": message.conversation_id,
            "message_id": message.id,
            "message_kind": message.message_kind,
            "author_kind": message.author_kind,
            "has_pending_action": False,
        },
        entity_id=entity_id,
    )


async def _validate_context_authority(db, context: _DeliveryContext):
    initiative = context.initiative
    delivery_active = initiative.status == "active" or (
        initiative.status == "paused" and context.run.trigger_type == "manual"
    ) or (
        initiative.status == "completed"
        and initiative.trigger_kind == "at"
        and context.run.status == "succeeded"
    )
    if not delivery_active:
        raise InitiativeAdmissionRejected(
            "initiative_muted" if initiative.status == "muted" else "initiative_not_active"
        )
    surface = context.delivery_policy.surface
    task = (await db.execute(
        select(Task).where(Task.id == context.run.task_id)
    )).scalar_one_or_none()
    marker = initiative_task_marker(task.details if task is not None else None)
    if (
        task is None
        or marker is None
        or marker.initiative_id != initiative.id
        or marker.initiative_run_id != context.run.id
        or marker.agent_subscription_id != initiative.agent_subscription_id
        or marker.owner_user_id != initiative.owner_user_id
        or str(task.creator_id or "") != str(initiative.owner_user_id)
        or str(task.owner_subscription_id or "")
        != str(initiative.agent_subscription_id)
        or str(task.workspace_id or "") != str(initiative.workspace_id or "")
        or str(task.conversation_id or "") != str(surface.conversation_id or "")
    ):
        raise InitiativeAdmissionRejected("initiative_task_lineage_mismatch")
    if not initiative_result_revision_matches(
        initiative=initiative,
        marker=marker,
        run=context.run,
        allow_one_shot_completion=True,
    ):
        raise InitiativeAdmissionRejected("initiative_revision_changed")
    agent_id = (await db.execute(
        select(AgentSubscription.agent_id).where(
            AgentSubscription.id == initiative.agent_subscription_id,
        )
    )).scalar_one_or_none()
    return await _exact_runtime_authority(
        db,
        initiative=initiative,
        subscription_id=initiative.agent_subscription_id,
        owner_user_id=initiative.owner_user_id,
        agent_id=agent_id,
        conversation_id=surface.conversation_id,
        allow_paused_workspace=context.run.trigger_type == "manual",
    )


async def lock_agent_initiative_notification_delivery_authority(
    db,
    *,
    notification_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str | None,
    idempotency_key: str | None,
    meta: dict | None,
    require_manual_run: bool = False,
) -> tuple[bool, str | None]:
    """Lock and revalidate one Initiative immediately before external I/O.

    Notification metadata is only a lookup hint. Every owner, revision, run,
    Chat receipt, and delivery edge is re-read from the database while the
    Initiative lifecycle lock is held. Expected lifecycle failures are
    deterministic suppression; database failures deliberately propagate so
    the Notification outbox keeps its normal retry semantics.
    """

    metadata = meta if isinstance(meta, dict) else {}
    initiative_id = str(metadata.get("initiative_id") or "").strip()
    run_id = str(metadata.get("initiative_run_id") or "").strip()
    message_id = str(metadata.get("message_id") or "").strip()
    subscription_id = str(metadata.get("agent_subscription_id") or "").strip()
    agent_id = str(metadata.get("agent_id") or "").strip()
    task_id = str(metadata.get("task_id") or "").strip()
    delivery_key = str(idempotency_key or "").strip()
    if not all(
        (
            initiative_id,
            run_id,
            message_id,
            subscription_id,
            agent_id,
            task_id,
            delivery_key,
        )
    ):
        return False, "invalid_agent_initiative_notification_metadata"

    initiative = (await db.execute(
        select(AgentInitiative)
        .where(AgentInitiative.id == initiative_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if initiative is None:
        return False, "initiative_not_found"
    state = (await db.execute(
        select(AgentInitiativeState)
        .where(AgentInitiativeState.initiative_id == initiative.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    run = (await db.execute(
        select(AgentInitiativeRun)
        .where(
            AgentInitiativeRun.id == run_id,
            AgentInitiativeRun.initiative_id == initiative.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    delivery = (await db.execute(
        select(AgentInitiativeDelivery)
        .where(
            AgentInitiativeDelivery.initiative_run_id == run_id,
            AgentInitiativeDelivery.kind == "notification",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if state is None or run is None or delivery is None:
        return False, "invalid_delivery_context"
    if require_manual_run and run.trigger_type != "manual":
        return False, "initiative_manual_run_required"
    notification = (await db.execute(
        select(Notification)
        .where(Notification.id == notification_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if notification is None:
        return False, "initiative_notification_lineage_mismatch"
    try:
        decision_policy = DecisionPolicy.model_validate(
            initiative.decision_policy or {}
        )
        delivery_policy = DeliveryPolicy.model_validate(
            initiative.delivery_policy or {}
        )
        interruption_policy = InterruptionPolicy.model_validate(
            initiative.interruption_policy or {}
        )
    except ValidationError:
        return False, "invalid_delivery_contract"
    context = _DeliveryContext(
        initiative=initiative,
        state=state,
        run=run,
        delivery=delivery,
        decision_policy=decision_policy,
        delivery_policy=delivery_policy,
        interruption_policy=interruption_policy,
    )
    if (
        str(initiative.entity_id or "") != str(entity_id or "")
        or str(initiative.owner_user_id or "") != str(user_id or "")
        or str(initiative.workspace_id or "") != str(workspace_id or "")
        or str(initiative.agent_subscription_id or "") != subscription_id
        or str(run.task_id or "") != task_id
        or run.status != "succeeded"
        or run.final_decision != "notify"
        or str(delivery.recipient_user_id or "") != str(user_id or "")
        or str(delivery.idempotency_key or "") != delivery_key
        or delivery.status not in {"processing", "accepted"}
        or (
            delivery.status == "accepted"
            and str(delivery.notification_id or "") != str(notification_id)
        )
        or (
            delivery.status == "processing"
            and (
                not str(delivery.claim_token or "")
                or (
                    delivery.notification_id is not None
                    and str(delivery.notification_id) != str(notification_id)
                )
            )
        )
    ):
        return False, "initiative_notification_lineage_mismatch"
    surface_delivery = (await db.execute(
        select(AgentInitiativeDelivery).where(
            AgentInitiativeDelivery.initiative_run_id == run.id,
            AgentInitiativeDelivery.kind == delivery_policy.surface.kind,
        )
    )).scalar_one_or_none()
    if (
        surface_delivery is None
        or surface_delivery.status != "delivered"
        or str(surface_delivery.message_id or "") != message_id
    ):
        return False, "initiative_notification_lineage_mismatch"
    try:
        subscription, _owner, conversation = await _validate_context_authority(
            db,
            context,
        )
    except InitiativeAdmissionRejected as exc:
        return False, exc.reason_code
    if str(subscription.agent_id or "") != agent_id:
        return False, "initiative_notification_lineage_mismatch"
    surface_message = (await db.execute(
        select(Message)
        .where(
            Message.id == message_id,
            Message.conversation_id == conversation.id,
            Message.response_surface_event_id == f"agent-initiative-run:{run.id}",
            Message.role == "assistant",
            Message.author_kind == "agent",
            Message.author_subscription_id == subscription.id,
            Message.message_kind == "agent_update",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    message_meta = (
        surface_message.meta
        if surface_message is not None and isinstance(surface_message.meta, dict)
        else {}
    )
    if (
        surface_message is None
        or str(surface_message.content or "") != str(run.message or "")
        or message_meta.get("agent_initiative") is not True
        or str(message_meta.get("initiative_id") or "") != str(initiative.id)
        or str(message_meta.get("initiative_run_id") or "") != str(run.id)
        or str(message_meta.get("task_id") or "") != str(run.task_id or "")
        or str(message_meta.get("decision") or "") != "notify"
        or str(message_meta.get("trigger_type") or "")
        != str(run.trigger_type or "")
    ):
        return False, "initiative_notification_lineage_mismatch"
    try:
        _validate_notification_lineage(
            notification,
            initiative_id=initiative.id,
            run_id=run.id,
            message_id=message_id,
            agent_subscription_id=initiative.agent_subscription_id,
            task_id=run.task_id,
            entity_id=initiative.entity_id,
            owner_user_id=initiative.owner_user_id,
            workspace_id=initiative.workspace_id,
            idempotency_key=delivery.idempotency_key,
            agent_id=subscription.agent_id,
        )
    except _PermanentDeliveryError as exc:
        return False, exc.reason_code
    return True, None


async def _deliver_surface(
    session_factory,
    *,
    run_id: str,
    claim_token: str,
) -> tuple[str, Message | None, str | None]:
    message = None
    workspace_id = None
    entity_id = None
    owner_user_id = None
    async with session_factory() as db:
        surface_kinds = list((await db.execute(
            select(AgentInitiativeDelivery.kind).where(
                AgentInitiativeDelivery.initiative_run_id == run_id,
                AgentInitiativeDelivery.kind != "notification",
            ).limit(2)
        )).scalars().all())
        if len(surface_kinds) != 1:
            await db.rollback()
            raise _PermanentDeliveryError(
                "invalid_surface_delivery",
                "Initiative run must have exactly one durable Chat delivery intent",
            )
        surface_kind = surface_kinds[0]
        try:
            context = await _load_locked_context(
                db,
                run_id=run_id,
                kind=surface_kind,
                claim_token=claim_token,
            )
        except _DeliveryDeferred:
            await db.rollback()
            return "deferred", None, None
        if context is None:
            await db.rollback()
            raise _PermanentDeliveryError(
                "missing_surface_delivery",
                "Initiative run has no durable Chat delivery intent",
            )
        delivery = context.delivery
        if delivery.status in _TERMINAL_DELIVERY_STATUSES:
            await db.rollback()
            return delivery.status, None, None
        now = datetime.now(timezone.utc)
        if context.run.status != "succeeded" or context.run.final_decision != "notify":
            _suppress(delivery, "initiative_run_not_deliverable", now)
            await db.commit()
            return delivery.status, None, None
        try:
            subscription, _owner, conversation = await _validate_context_authority(db, context)
        except InitiativeAdmissionRejected as exc:
            if exc.reason_code in {"initiative_muted", "initiative_not_active"}:
                _suppress(delivery, exc.reason_code, now)
            else:
                _quarantine(delivery, exc.reason_code, now)
            await db.commit()
            return delivery.status, None, None

        surface = context.delivery_policy.surface
        if delivery.kind != surface.kind:
            _quarantine(delivery, "initiative_delivery_kind_mismatch", now)
            await db.commit()
            return delivery.status, None, None
        event_id = f"agent-initiative-run:{context.run.id}"
        message = (await db.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.response_surface_event_id == event_id,
            )
        )).scalar_one_or_none()
        if message is None:
            message = Message(
                id=generate_ulid(),
                conversation_id=conversation.id,
                role="assistant",
                content=context.run.message,
                meta={
                    "agent_initiative": True,
                    "initiative_id": context.initiative.id,
                    "initiative_run_id": context.run.id,
                    "decision": "notify",
                    "trigger_type": context.run.trigger_type,
                    "task_id": context.run.task_id,
                },
                response_surface_event_id=event_id,
                author_kind="agent",
                author_subscription_id=subscription.id,
                message_kind="agent_update",
                refs=[
                    {"type": "agent_initiative", "id": context.initiative.id},
                    {"type": "agent_initiative_run", "id": context.run.id},
                    *(
                        [{"type": "task", "id": context.run.task_id}]
                        if context.run.task_id else []
                    ),
                ],
            )
            db.add(message)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == conversation.id)
                .values(updated_at=func.now())
            )
            await db.flush()
        delivery.status = "delivered"
        delivery.message_id = message.id
        delivery.error = None
        delivery.suppressed_reason = None
        delivery.delivered_at = now
        delivery.locked_until = None
        delivery.claim_token = None
        _advance_delivery_state(context, now)
        entity_id = context.initiative.entity_id
        workspace_id = context.initiative.workspace_id
        owner_user_id = context.initiative.owner_user_id
        await db.commit()

    if message is not None and entity_id and owner_user_id:
        try:
            await _publish_surface_event(
                entity_id=entity_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                message=message,
            )
        except Exception:
            # The durable Message is the delivery fact. Realtime refresh is
            # best-effort and must not block the Notification handoff.
            logger.warning(
                "Initiative Chat realtime refresh failed run=%s",
                run_id,
                exc_info=True,
            )
    return "delivered", message, workspace_id


async def _notification_already_exists(db, context: _DeliveryContext) -> Notification | None:
    return (await db.execute(
        select(Notification).where(
            Notification.entity_id == context.initiative.entity_id,
            Notification.user_id == context.initiative.owner_user_id,
            Notification.idempotency_key == context.delivery.idempotency_key,
        )
    )).scalar_one_or_none()


def _validate_existing_notification(
    notification: Notification,
    context: _DeliveryContext,
    *,
    expected_message_id: str | None,
    expected_agent_id: str | None,
) -> None:
    _validate_notification_lineage(
        notification,
        initiative_id=context.initiative.id,
        run_id=context.run.id,
        message_id=expected_message_id,
        agent_subscription_id=context.initiative.agent_subscription_id,
        task_id=context.run.task_id,
        entity_id=context.initiative.entity_id,
        owner_user_id=context.initiative.owner_user_id,
        workspace_id=context.initiative.workspace_id,
        idempotency_key=context.delivery.idempotency_key,
        agent_id=expected_agent_id,
    )


async def _deliver_notification(
    session_factory,
    *,
    run_id: str,
    claim_token: str,
) -> str:
    notify_payload: dict[str, Any] | None = None
    delivery_key = ""
    async with session_factory() as db:
        try:
            context = await _load_locked_context(
                db,
                run_id=run_id,
                kind="notification",
                claim_token=claim_token,
            )
        except _DeliveryDeferred:
            await db.rollback()
            return "deferred"
        if context is None:
            await db.rollback()
            return "not_configured"
        delivery = context.delivery
        if delivery.status in _TERMINAL_DELIVERY_STATUSES:
            await db.rollback()
            return delivery.status
        now = datetime.now(timezone.utc)
        surface_delivery = (await db.execute(
            select(AgentInitiativeDelivery).where(
                AgentInitiativeDelivery.initiative_run_id == run_id,
                AgentInitiativeDelivery.kind == context.delivery_policy.surface.kind,
            )
        )).scalar_one_or_none()
        if surface_delivery is None or surface_delivery.status != "delivered":
            reason = (
                "surface_delivery_unavailable"
                if surface_delivery is None
                or surface_delivery.status in {"quarantined", "suppressed"}
                else "surface_delivery_pending"
            )
            if reason == "surface_delivery_pending":
                await db.rollback()
                raise RuntimeError(reason)
            _suppress(delivery, reason, now)
            await db.commit()
            return delivery.status
        try:
            subscription, owner, _conversation = await _validate_context_authority(db, context)
        except InitiativeAdmissionRejected as exc:
            if exc.reason_code in {"initiative_muted", "initiative_not_active"}:
                _suppress(delivery, exc.reason_code, now)
            else:
                _quarantine(delivery, exc.reason_code, now)
            await db.commit()
            return delivery.status

        existing = await _notification_already_exists(db, context)
        if existing is not None:
            _validate_existing_notification(
                existing,
                context,
                expected_message_id=surface_delivery.message_id,
                expected_agent_id=subscription.agent_id,
            )
            delivery.status = "accepted"
            delivery.notification_id = existing.id
            delivery.error = None
            delivery.delivered_at = now
            delivery.locked_until = None
            delivery.claim_token = None
            await db.commit()
            return delivery.status

        notification_policy = context.delivery_policy.notification
        channels = list(notification_policy.channels)
        quiet = initiative_quiet_hours_active(
            context.interruption_policy,
            user_preferences=owner.preferences,
            now=now,
        )
        if quiet:
            channels = ["inapp"]
        delivery_key = delivery.idempotency_key
        link = initiative_notification_link(
            context.initiative.workspace_id,
            context.delivery_policy.surface.conversation_id,
        )
        notify_payload = {
            "entity_id": context.initiative.entity_id,
            "user_id": context.initiative.owner_user_id,
            "type": "agent_initiative_update",
            "title": context.initiative.name,
            "body": initiative_notification_body(
                context.run.message,
                content_mode=notification_policy.content_mode,
            ),
            "link": link,
            "meta": {
                "agent_id": subscription.agent_id,
                "agent_subscription_id": context.initiative.agent_subscription_id,
                "initiative_id": context.initiative.id,
                "initiative_run_id": context.run.id,
                "message_id": surface_delivery.message_id,
                "task_id": context.run.task_id,
                "quiet_hours_external_suppressed": quiet,
            },
            "channels": channels,
            # Model severity cannot bypass quiet hours. The candidate remains
            # in Run.candidate_decision for audit, but routing is platform-owned.
            "severity": "info",
            "workspace_id": context.initiative.workspace_id,
            "idempotency_key": delivery.idempotency_key,
        }
        await db.rollback()

    if notify_payload is None:
        return "not_configured"
    from packages.core.services.notify import notify

    await notify(**notify_payload)

    async with session_factory() as db:
        context = await _load_locked_context(
            db,
            run_id=run_id,
            kind="notification",
            claim_token=claim_token,
        )
        if context is None:
            await db.rollback()
            return "missing"
        delivery = context.delivery
        if delivery.status in _TERMINAL_DELIVERY_STATUSES:
            # The lease may have expired while the external adapter was in
            # flight. A newer worker's terminal decision is authoritative;
            # this stale worker must never revive it as accepted.
            await db.rollback()
            return delivery.status
        existing = await _notification_already_exists(db, context)
        if existing is None:
            raise RuntimeError("durable notification was not accepted")
        _validate_existing_notification(
            existing,
            context,
            expected_message_id=(notify_payload.get("meta") or {}).get("message_id"),
            expected_agent_id=(notify_payload.get("meta") or {}).get("agent_id"),
        )
        if delivery.idempotency_key != delivery_key:
            raise RuntimeError("Initiative notification delivery identity changed")
        delivery.status = "accepted"
        delivery.notification_id = existing.id
        delivery.error = None
        delivery.suppressed_reason = None
        delivery.delivered_at = datetime.now(timezone.utc)
        delivery.locked_until = None
        delivery.claim_token = None
        await db.commit()
        return delivery.status


async def record_initiative_delivery_claim_failure(
    session_factory,
    *,
    run_id: str,
    claim_token: str,
    error: BaseException,
    permanent_reason: str | None = None,
    now: datetime | None = None,
) -> str:
    """Release or quarantine only the delivery rows owned by this claim."""

    when = _aware_utc(now) or datetime.now(timezone.utc)
    error_text = str(error or type(error).__name__)[:2_000]
    async with session_factory() as db:
        rows = list((await db.execute(
            select(AgentInitiativeDelivery)
            .where(
                AgentInitiativeDelivery.initiative_run_id == run_id,
                AgentInitiativeDelivery.status == "processing",
                AgentInitiativeDelivery.claim_token == claim_token,
            )
            .with_for_update()
        )).scalars().all())
        if not rows:
            await db.rollback()
            return "claim_lost"
        retry_scheduled = False
        for row in rows:
            row.error = error_text
            row.locked_until = None
            row.claim_token = None
            if (
                permanent_reason is not None
                or int(row.attempt_count or 0) >= _MAX_DELIVERY_ATTEMPTS
            ):
                row.status = "quarantined"
                row.suppressed_reason = (
                    permanent_reason or "delivery_retry_exhausted"
                )[:2_000]
                row.delivered_at = when
                continue
            row.status = "error"
            row.suppressed_reason = None
            row.next_attempt_at = _delivery_retry_at(
                when,
                int(row.attempt_count or 0),
            )
            row.delivered_at = None
            retry_scheduled = True
        await db.commit()
        return "retry_scheduled" if retry_scheduled else "quarantined"


async def deliver_initiative_run(
    session_factory,
    *,
    run_id: str,
    kind: str | None = None,
    claim_token: str | None = None,
) -> dict[str, str]:
    """Project one settled run in Chat-before-Notification order."""

    if claim_token is None:
        claim = await claim_initiative_delivery_run(
            session_factory,
            run_id=run_id,
        )
        if claim is None:
            return {
                "run_id": run_id,
                "surface": "not_due_or_claimed",
                "notification": "not_due_or_claimed",
            }
        kind = claim.kind
        claim_token = claim.claim_token
    if kind is None:
        return {
            "run_id": run_id,
            "surface": "invalid_claim",
            "notification": "invalid_claim",
        }
    surface_status = "not_attempted"
    notification_status = "not_attempted"
    current_claim_token = claim_token
    try:
        if kind == "notification":
            notification_status = await _deliver_notification(
                session_factory,
                run_id=run_id,
                claim_token=claim_token,
            )
        else:
            surface_status, _message, _workspace_id = await _deliver_surface(
                session_factory,
                run_id=run_id,
                claim_token=claim_token,
            )
            if surface_status not in _TERMINAL_DELIVERY_STATUSES:
                return {
                    "run_id": run_id,
                    "surface": surface_status,
                    "notification": notification_status,
                }
            next_claim = await claim_initiative_delivery_run(
                session_factory,
                run_id=run_id,
            )
            if next_claim is not None:
                current_claim_token = next_claim.claim_token
                if next_claim.kind != "notification":
                    raise _PermanentDeliveryError(
                        "invalid_delivery_sequence",
                        "Chat delivery did not reach a terminal state",
                    )
                notification_status = await _deliver_notification(
                    session_factory,
                    run_id=run_id,
                    claim_token=next_claim.claim_token,
                )
        return {
            "run_id": run_id,
            "surface": surface_status,
            "notification": notification_status,
        }
    except _DeliveryClaimLost:
        return {
            "run_id": run_id,
            "surface": "claim_lost",
            "notification": "claim_lost",
        }
    except _PermanentDeliveryError as exc:
        recovery = await record_initiative_delivery_claim_failure(
            session_factory,
            run_id=run_id,
            claim_token=current_claim_token,
            error=exc,
            permanent_reason=exc.reason_code,
        )
        return {
            "run_id": run_id,
            "surface": surface_status,
            "notification": notification_status,
            "recovery": recovery,
        }
    except Exception as exc:
        recovery = await record_initiative_delivery_claim_failure(
            session_factory,
            run_id=run_id,
            claim_token=current_claim_token,
            error=exc,
        )
        return {
            "run_id": run_id,
            "surface": surface_status,
            "notification": notification_status,
            "recovery": recovery,
        }


__all__ = [
    "InitiativeDeliveryClaim",
    "claim_initiative_delivery_run",
    "deliver_initiative_run",
    "initiative_notification_body",
    "initiative_notification_link",
    "initiative_quiet_hours_active",
    "lock_agent_initiative_notification_delivery_authority",
    "pending_initiative_delivery_run_ids",
    "record_initiative_delivery_claim_failure",
]
