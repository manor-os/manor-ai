"""Admission and strict decision settlement for proactive Agent runs.

The scheduler and the Task agent loop are the execution engine.  This module owns
the Initiative-specific authority checks and turns one strictly validated
model decision into idempotent delivery intents.  It deliberately performs no
Chat or Notification I/O; delivery retries must never re-run the Agent.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.contracts.agent_initiative import (
    DecisionPolicy,
    DeliveryPolicy,
    InitiativeDecision,
    InterruptionPolicy,
    TriggerSpec,
)
from packages.core.models.agent_initiative import (
    AgentInitiative,
    AgentInitiativeDelivery,
    AgentInitiativeRun,
    AgentInitiativeState,
)
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Conversation, Task
from packages.core.models.user import User
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.permissions import resolve_effective_user_role_name
from packages.core.services.workspace_access import user_can_read_workspace_id


INITIATIVE_TASK_MARKER_KEY = "agent_initiative"
INITIATIVE_TASK_MARKER_VERSION = "1"
INITIATIVE_BILLING_SOURCE = "agent_initiative"

_ACTIVE_EXECUTION_STATUSES = frozenset({"active", "muted"})
_ACTIVE_DELIVERY_STATUS = "active"
_DELIVERY_PENDING_STATUSES = frozenset({"pending", "error", "processing"})
_WHITESPACE_RE = re.compile(r"\s+")
_NEEDS_SETUP_REASONS = frozenset({
    "initiative_subscription_inactive",
    "initiative_subscription_owner_mismatch",
    "initiative_owner_inactive",
    "initiative_agent_inactive",
    "initiative_workspace_inactive",
    "initiative_workspace_forbidden",
    "initiative_conversation_invalid",
})


class InitiativeAdmissionRejected(ValueError):
    """A managed scheduler occurrence is no longer authorized to execute."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class InitiativeAdmission:
    run: AgentInitiativeRun
    initiative: AgentInitiative
    subscription: AgentSubscription


@dataclass(frozen=True)
class InitiativeTaskMarker:
    initiative_id: str
    initiative_run_id: str
    initiative_revision: int
    agent_subscription_id: str
    owner_user_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": INITIATIVE_TASK_MARKER_VERSION,
            "initiative_id": self.initiative_id,
            "initiative_run_id": self.initiative_run_id,
            "initiative_revision": self.initiative_revision,
            "agent_subscription_id": self.agent_subscription_id,
            "owner_user_id": self.owner_user_id,
        }


@dataclass(frozen=True)
class InitiativePolicyResult:
    final_decision: str
    reason_code: str
    fingerprint: str | None
    should_deliver: bool


@dataclass(frozen=True)
class InitiativeDeliveryReservations:
    fingerprints: frozenset[str]
    pending_count: int


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def initiative_task_marker(details: Any) -> InitiativeTaskMarker | None:
    """Read the exact scheduler-owned marker; malformed lookalikes fail closed."""

    if not isinstance(details, dict):
        return None
    raw = details.get(INITIATIVE_TASK_MARKER_KEY)
    if not isinstance(raw, dict) or raw.get("version") != INITIATIVE_TASK_MARKER_VERSION:
        return None
    values = {
        key: str(raw.get(key) or "").strip()
        for key in (
            "initiative_id",
            "initiative_run_id",
            "agent_subscription_id",
            "owner_user_id",
        )
    }
    revision = raw.get("initiative_revision")
    if (
        not all(values.values())
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        return None
    return InitiativeTaskMarker(
        initiative_id=values["initiative_id"],
        initiative_run_id=values["initiative_run_id"],
        initiative_revision=revision,
        agent_subscription_id=values["agent_subscription_id"],
        owner_user_id=values["owner_user_id"],
    )


def has_initiative_task_marker(details: Any) -> bool:
    """Return whether a Task claims the reserved Initiative runtime marker."""

    return isinstance(details, dict) and INITIATIVE_TASK_MARKER_KEY in details


def initiative_result_revision_matches(
    *,
    initiative: AgentInitiative,
    marker: InitiativeTaskMarker,
    run: AgentInitiativeRun,
    allow_one_shot_completion: bool = False,
) -> bool:
    """Fence late output while allowing the one system-owned completion bump."""

    current_revision = int(initiative.revision or 1)
    if current_revision == marker.initiative_revision:
        return True
    return bool(
        allow_one_shot_completion
        and initiative.status == "completed"
        and initiative.trigger_kind == "at"
        and run.trigger_type == "at"
        and run.status == "succeeded"
        and current_revision == marker.initiative_revision + 1
    )


def parse_initiative_decision(raw: Any) -> tuple[InitiativeDecision | None, str | None]:
    """Parse one JSON-only Decision.  Markdown/free text is never accepted."""

    try:
        if isinstance(raw, str):
            decision = InitiativeDecision.model_validate_json(raw)
        elif isinstance(raw, dict):
            decision = InitiativeDecision.model_validate(raw)
        else:
            raise TypeError("decision output must be a JSON object")
    except (ValidationError, ValueError, TypeError) as exc:
        return None, str(exc)[:2_000]
    return decision, None


def initiative_decision_repair_prompt(validation_error: str) -> str:
    """Bounded no-tools repair instruction for the single allowed retry."""

    detail = _WHITESPACE_RE.sub(" ", str(validation_error or "invalid output"))[:1_000]
    return (
        "[System: Your previous Agent Initiative decision was invalid: "
        f"{detail}. Return one corrected JSON object only, matching the requested "
        "version-1 notify/silent schema. Do not call tools, repeat side effects, "
        "use markdown fences, or add explanatory prose.]"
    )


def _normalized_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE_RE.sub(" ", normalized).strip().casefold()


def initiative_decision_fingerprint(decision: InitiativeDecision) -> str:
    """Compute the server-owned content fingerprint used for dedupe."""

    refs = sorted(
        (
            {
                "type": _normalized_text(ref.type),
                "id": _normalized_text(ref.id),
                "url": _normalized_text(ref.url),
                "label": _normalized_text(ref.label),
            }
            for ref in decision.evidence_refs
        ),
        key=lambda item: (
            item["type"], item["id"], item["url"], item["label"],
        ),
    )
    payload = {
        "dedupe_key": _normalized_text(decision.dedupe_key),
        "evidence_refs": refs,
        "message": _normalized_text(decision.message),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_initiative_policy(
    *,
    decision: InitiativeDecision,
    initiative_status: str,
    policy: DecisionPolicy,
    last_fingerprint: str | None,
    reserved_fingerprints: set[str] | frozenset[str] = frozenset(),
    cooldown_until: datetime | None,
    window_started_at: datetime | None,
    notification_count: int,
    now: datetime,
    allow_paused_delivery: bool = False,
    pending_delivery_count: int = 0,
) -> InitiativePolicyResult:
    """Apply deterministic mute/dedupe/cooldown/rate gates under State lock."""

    if decision.decision == "silent":
        return InitiativePolicyResult(
            final_decision="silent",
            reason_code=decision.reason_code,
            fingerprint=None,
            should_deliver=False,
        )
    fingerprint = initiative_decision_fingerprint(decision)
    delivery_active = initiative_status == _ACTIVE_DELIVERY_STATUS or (
        initiative_status == "paused" and allow_paused_delivery
    )
    if not delivery_active:
        reason = (
            "initiative_muted"
            if initiative_status == "muted"
            else "initiative_not_active"
        )
        return InitiativePolicyResult("suppressed", reason, fingerprint, False)
    if policy.mode == "on_change" and (
        fingerprint == last_fingerprint or fingerprint in reserved_fingerprints
    ):
        return InitiativePolicyResult("suppressed", "duplicate", fingerprint, False)
    normalized_now = _aware_utc(now) or now
    normalized_cooldown = _aware_utc(cooldown_until)
    reserved_count = max(0, int(pending_delivery_count or 0))
    if (
        normalized_cooldown is not None
        and normalized_cooldown > normalized_now
    ) or (policy.cooldown_seconds > 0 and reserved_count > 0):
        return InitiativePolicyResult("suppressed", "cooldown", fingerprint, False)
    normalized_window = _aware_utc(window_started_at)
    count = max(0, int(notification_count or 0))
    if normalized_window is not None and normalized_window > normalized_now - timedelta(days=1):
        if count + reserved_count >= policy.max_notifications_per_day:
            return InitiativePolicyResult("suppressed", "rate_limited", fingerprint, False)
    elif reserved_count >= policy.max_notifications_per_day:
        return InitiativePolicyResult("suppressed", "rate_limited", fingerprint, False)
    return InitiativePolicyResult("notify", decision.reason_code, fingerprint, True)


async def _exact_runtime_authority(
    db: AsyncSession,
    *,
    initiative: AgentInitiative,
    subscription_id: str,
    owner_user_id: str,
    agent_id: str | None,
    conversation_id: str | None,
    allow_paused_workspace: bool = False,
) -> tuple[AgentSubscription, User, Conversation]:
    """Revalidate the immutable owner/subscription/destination tuple."""

    if (
        str(initiative.agent_subscription_id) != str(subscription_id)
        or str(initiative.owner_user_id) != str(owner_user_id)
    ):
        raise InitiativeAdmissionRejected("initiative_runtime_identity_mismatch")
    subscription = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.id == subscription_id,
            AgentSubscription.entity_id == initiative.entity_id,
            AgentSubscription.status == "active",
        )
    )).scalar_one_or_none()
    if (
        subscription is None
        or str(subscription.agent_id or "") != str(agent_id or "")
        or str(subscription.workspace_id or "") != str(initiative.workspace_id or "")
    ):
        raise InitiativeAdmissionRejected("initiative_subscription_inactive")
    if not subscription.workspace_id and str(subscription.owner_user_id or "") != str(owner_user_id):
        raise InitiativeAdmissionRejected("initiative_subscription_owner_mismatch")

    owner = (await db.execute(
        select(User).where(
            User.id == owner_user_id,
            User.entity_id == initiative.entity_id,
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if owner is None:
        raise InitiativeAdmissionRejected("initiative_owner_inactive")

    if not is_master_agent(subscription.agent_id):
        agent_exists = (await db.execute(
            select(Agent.id).where(
                Agent.id == subscription.agent_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if agent_exists is None:
            raise InitiativeAdmissionRejected("initiative_agent_inactive")

    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == initiative.entity_id,
            Conversation.status == "active",
        )
    )).scalar_one_or_none()
    if conversation is None:
        raise InitiativeAdmissionRejected("initiative_conversation_invalid")
    if subscription.workspace_id:
        from packages.core.services.workspace_autonomy import workspace_allows_manual_work

        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == subscription.workspace_id,
                Workspace.entity_id == initiative.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if (
            not workspace_allows_manual_work(workspace)
            or workspace.status == "paused" and not allow_paused_workspace
        ):
            raise InitiativeAdmissionRejected("initiative_workspace_inactive")
        if (
            conversation.workspace_id != subscription.workspace_id
            or conversation.scope not in {"workspace_main", "workspace_thread"}
        ):
            raise InitiativeAdmissionRejected("initiative_conversation_invalid")
        role = await resolve_effective_user_role_name(
            db,
            user_id=owner_user_id,
            entity_id=initiative.entity_id,
            legacy_role=owner.role,
        )
        if not await user_can_read_workspace_id(
            db,
            workspace_id=subscription.workspace_id,
            entity_id=initiative.entity_id,
            user_id=owner_user_id,
            role=role,
        ):
            raise InitiativeAdmissionRejected("initiative_workspace_forbidden")
    else:
        personal_agent_matches = (
            str(conversation.agent_id or "") == str(subscription.agent_id or "")
            or (
                conversation.agent_id is None
                and is_master_agent(subscription.agent_id)
            )
        )
        if (
            conversation.workspace_id is not None
            or conversation.user_id != owner_user_id
            or not personal_agent_matches
            or str(conversation.agent_subscription_id or "") != str(subscription.id)
        ):
            raise InitiativeAdmissionRejected("initiative_conversation_invalid")
    return subscription, owner, conversation


async def _mark_needs_setup(
    db: AsyncSession,
    initiative: AgentInitiative,
    *,
    reason_code: str,
    causation_id: str | None,
) -> None:
    if reason_code not in _NEEDS_SETUP_REASONS or initiative.status == "needs_setup":
        return
    initiative.status = "needs_setup"
    from packages.core.revisions import bump_revision

    await bump_revision(
        db,
        initiative,
        patch={"status": "needs_setup", "reason_code": reason_code},
        changed_by_kind="system",
        causation_id=causation_id,
    )


async def mark_initiative_driver_needs_setup(
    db: AsyncSession,
    *,
    job: Any,
    reason_code: str,
    causation_id: str | None,
) -> bool:
    """Quarantine an exact managed driver after a deterministic setup failure."""

    target = job.execution_target if isinstance(job.execution_target, dict) else {}
    initiative_id = str(getattr(job, "agent_initiative_id", None) or "").strip()
    if (
        not initiative_id
        or getattr(job, "execution_type", None) != "agent_initiative"
        or target.get("managed_by") != "agent_initiative"
        or str(target.get("initiative_id") or "") != initiative_id
    ):
        return False
    initiative = (await db.execute(
        select(AgentInitiative)
        .where(
            AgentInitiative.id == initiative_id,
            AgentInitiative.entity_id == job.entity_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if initiative is None or initiative.status in {"deleted", "completed"}:
        return False
    await _mark_needs_setup(
        db,
        initiative,
        reason_code=reason_code,
        causation_id=causation_id,
    )
    return initiative.status == "needs_setup"


async def admit_scheduled_initiative(
    db: AsyncSession,
    *,
    job: Any,
    scheduled_run_id: str,
    occurrence_key: str,
    trigger_type: str,
    now: datetime,
    manual: bool,
) -> InitiativeAdmission:
    """Admit one exact managed driver occurrence before Task creation."""

    target = job.execution_target if isinstance(job.execution_target, dict) else {}
    initiative_id = str(getattr(job, "agent_initiative_id", None) or "").strip()
    if (
        not initiative_id
        or str(getattr(job, "execution_type", "") or "") != "agent_initiative"
        or target.get("managed_by") != "agent_initiative"
        or str(target.get("initiative_id") or "") != initiative_id
    ):
        raise InitiativeAdmissionRejected("initiative_driver_invalid")
    initiative = (await db.execute(
        select(AgentInitiative)
        .where(
            AgentInitiative.id == initiative_id,
            AgentInitiative.entity_id == job.entity_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if initiative is None or initiative.status == "deleted":
        raise InitiativeAdmissionRejected("initiative_not_found")
    allowed_statuses = _ACTIVE_EXECUTION_STATUSES | ({"paused"} if manual else set())
    if initiative.status not in allowed_statuses:
        raise InitiativeAdmissionRejected("initiative_not_active")
    target_revision = target.get("initiative_revision")
    if (
        isinstance(target_revision, bool)
        or not isinstance(target_revision, int)
        or target_revision != initiative.revision
    ):
        raise InitiativeAdmissionRejected("initiative_revision_changed")
    if str(target.get("subscription_id") or "") != str(initiative.agent_subscription_id):
        raise InitiativeAdmissionRejected("initiative_subscription_mismatch")
    if str(target.get("owner_user_id") or "") != str(initiative.owner_user_id):
        raise InitiativeAdmissionRejected("initiative_owner_mismatch")
    if str(job.workspace_id or "") != str(initiative.workspace_id or ""):
        raise InitiativeAdmissionRejected("initiative_workspace_mismatch")
    if str(job.user_id or "") != str(initiative.owner_user_id):
        raise InitiativeAdmissionRejected("initiative_owner_mismatch")

    try:
        subscription, _owner, _conversation = await _exact_runtime_authority(
            db,
            initiative=initiative,
            subscription_id=initiative.agent_subscription_id,
            owner_user_id=initiative.owner_user_id,
            agent_id=job.agent_id,
            conversation_id=job.conversation_id,
            allow_paused_workspace=manual,
        )
    except InitiativeAdmissionRejected as exc:
        await _mark_needs_setup(
            db,
            initiative,
            reason_code=exc.reason_code,
            causation_id=scheduled_run_id,
        )
        raise
    existing = (await db.execute(
        select(AgentInitiativeRun).where(
            AgentInitiativeRun.initiative_id == initiative.id,
            AgentInitiativeRun.occurrence_key == occurrence_key,
        )
    )).scalar_one_or_none()
    if existing is not None:
        if str(existing.scheduled_run_id or "") != str(scheduled_run_id):
            raise InitiativeAdmissionRejected("initiative_occurrence_conflict")
        return InitiativeAdmission(existing, initiative, subscription)

    run = AgentInitiativeRun(
        id=generate_ulid(),
        initiative_id=initiative.id,
        scheduled_run_id=scheduled_run_id,
        occurrence_key=occurrence_key,
        trigger_type=str(trigger_type or "scheduled")[:20],
        status="admitted",
        created_at=now,
    )
    db.add(run)
    await db.flush()
    return InitiativeAdmission(run, initiative, subscription)


def initiative_marker_for_admission(admission: InitiativeAdmission) -> InitiativeTaskMarker:
    return InitiativeTaskMarker(
        initiative_id=admission.initiative.id,
        initiative_run_id=admission.run.id,
        initiative_revision=admission.initiative.revision,
        agent_subscription_id=admission.subscription.id,
        owner_user_id=admission.initiative.owner_user_id,
    )


async def mark_initiative_run_started(db: AsyncSession, task: Task) -> None:
    marker = initiative_task_marker(task.details)
    if marker is None:
        return
    initiative = (await db.execute(
        select(AgentInitiative)
        .where(AgentInitiative.id == marker.initiative_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if initiative is None:
        raise InitiativeAdmissionRejected("initiative_not_found")
    run = (await db.execute(
        select(AgentInitiativeRun)
        .where(
            AgentInitiativeRun.id == marker.initiative_run_id,
            AgentInitiativeRun.initiative_id == marker.initiative_id,
            AgentInitiativeRun.task_id == task.id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if run is None:
        raise InitiativeAdmissionRejected("initiative_run_lineage_mismatch")
    allowed_statuses = _ACTIVE_EXECUTION_STATUSES | (
        {"paused"} if run.trigger_type == "manual" else set()
    )
    if initiative.status not in allowed_statuses:
        raise InitiativeAdmissionRejected("initiative_not_active")
    if (
        initiative.revision != marker.initiative_revision
        or str(initiative.agent_subscription_id) != marker.agent_subscription_id
        or str(initiative.owner_user_id) != marker.owner_user_id
        or str(task.creator_id or "") != marker.owner_user_id
        or str(task.owner_subscription_id or "") != marker.agent_subscription_id
        or str(task.entity_id or "") != str(initiative.entity_id or "")
        or str(task.workspace_id or "") != str(initiative.workspace_id or "")
    ):
        raise InitiativeAdmissionRejected("initiative_task_lineage_mismatch")
    try:
        subscription, _owner, conversation = await _exact_runtime_authority(
            db,
            initiative=initiative,
            subscription_id=marker.agent_subscription_id,
            owner_user_id=marker.owner_user_id,
            agent_id=task.agent_id,
            conversation_id=task.conversation_id,
            allow_paused_workspace=run.trigger_type == "manual",
        )
    except InitiativeAdmissionRejected as exc:
        await _mark_needs_setup(
            db,
            initiative,
            reason_code=exc.reason_code,
            causation_id=run.id,
        )
        raise
    if (
        str(subscription.agent_id or "") != str(task.agent_id or "")
        or conversation.id != task.conversation_id
    ):
        raise InitiativeAdmissionRejected("initiative_task_lineage_mismatch")
    if run.status == "admitted":
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        await db.flush()


async def fail_admitted_initiative_run(
    db: AsyncSession,
    run: AgentInitiativeRun | None,
    reason: str,
) -> None:
    if run is None or run.status in {"succeeded", "failed", "filtered"}:
        return
    run.status = "failed"
    run.reason_code = "initiative_dispatch_failed"
    run.error = str(reason or "Initiative dispatch failed")[:2_000]
    run.completed_at = datetime.now(timezone.utc)
    await db.flush()


async def fail_scheduled_initiative_child(
    db: AsyncSession,
    *,
    initiative_id: str,
    scheduled_run_id: str,
    task_id: str,
    reason: str,
) -> bool:
    """Close the exact Initiative run when scheduler admission cancels its child."""

    run = (await db.execute(
        select(AgentInitiativeRun)
        .where(
            AgentInitiativeRun.initiative_id == initiative_id,
            AgentInitiativeRun.scheduled_run_id == scheduled_run_id,
            AgentInitiativeRun.task_id == task_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if run is None or run.status in {"succeeded", "failed", "filtered"}:
        return False
    await fail_admitted_initiative_run(db, run, reason)
    return True


async def _delivery_reservations(
    db: AsyncSession,
    *,
    initiative_id: str,
    current_run_id: str,
) -> InitiativeDeliveryReservations:
    rows = (await db.execute(
        select(AgentInitiativeRun.candidate_decision)
        .join(
            AgentInitiativeDelivery,
            AgentInitiativeDelivery.initiative_run_id == AgentInitiativeRun.id,
        )
        .where(
            AgentInitiativeRun.initiative_id == initiative_id,
            AgentInitiativeRun.id != current_run_id,
            AgentInitiativeRun.final_decision == "notify",
            AgentInitiativeDelivery.kind != "notification",
            AgentInitiativeDelivery.status.in_(
                tuple(_DELIVERY_PENDING_STATUSES)
            ),
        )
    )).scalars().all()
    fingerprints: set[str] = set()
    for raw in rows:
        decision, _error = parse_initiative_decision(raw)
        if decision is not None and decision.decision == "notify":
            fingerprints.add(initiative_decision_fingerprint(decision))
    return InitiativeDeliveryReservations(
        fingerprints=frozenset(fingerprints),
        pending_count=len(rows),
    )


async def _complete_one_shot_initiative(
    db: AsyncSession,
    *,
    initiative: AgentInitiative,
    driver: ScheduledJob | None,
    run: AgentInitiativeRun,
) -> bool:
    """Close a successfully decided one-shot and its managed driver once."""

    trigger = TriggerSpec.model_validate(initiative.trigger_config or {})
    if (
        trigger.kind != "at"
        or run.trigger_type != "at"
        or run.status != "succeeded"
    ):
        return False
    changed = False
    if initiative.status != "completed":
        initiative.status = "completed"
        from packages.core.revisions import bump_revision

        await bump_revision(
            db,
            initiative,
            patch={"status": "completed"},
            changed_by_kind="system",
            causation_id=run.id,
        )
        changed = True
    if driver is not None and (driver.enabled or driver.next_run_at is not None):
        from packages.core.services.scheduler_service import (
            ScheduledJobMutationFactory,
        )

        await ScheduledJobMutationFactory.apply_locked(
            db,
            driver,
            {"enabled": False, "next_run_at": None},
            changed_by_kind="system",
            causation_id=run.id,
            allow_agent_initiative_driver=True,
        )
        changed = True
    return changed


def _delivery_specs(
    *,
    run: AgentInitiativeRun,
    initiative: AgentInitiative,
    delivery_policy: DeliveryPolicy,
) -> list[tuple[str, str, str | None]]:
    specs = [(
        delivery_policy.surface.kind,
        f"agent-initiative:{run.id}:surface",
        initiative.owner_user_id,
    )]
    if delivery_policy.notification.enabled:
        specs.append((
            "notification",
            f"agent-initiative:{run.id}:recipient:{initiative.owner_user_id}",
            initiative.owner_user_id,
        ))
    return specs


async def settle_initiative_task(
    session_factory,
    *,
    task_id: str,
    result: dict[str, Any],
    scheduled_run_id: str | None,
    scheduled_job_id: str | None,
) -> str | None:
    """Settle one Task decision and persist delivery intents atomically.

    Returning a run id means at least one pending projection should be queued.
    The function is replay-safe and is intentionally called even when the
    ScheduledJobRun was already settled by an earlier recovery attempt.
    """

    async with session_factory() as db:
        task = (await db.execute(
            select(Task).where(Task.id == task_id)
        )).scalar_one_or_none()
        if task is None:
            await db.rollback()
            return None
        marker = initiative_task_marker(task.details)
        if marker is None:
            await db.rollback()
            return None
        details = task.details if isinstance(task.details, dict) else {}
        if (
            str(details.get("scheduled_run_id") or "") != str(scheduled_run_id or "")
            or str(details.get("scheduled_job_id") or "") != str(scheduled_job_id or "")
            or str(task.owner_subscription_id or "") != marker.agent_subscription_id
            or str(task.creator_id or "") != marker.owner_user_id
        ):
            raise InitiativeAdmissionRejected("initiative_task_lineage_mismatch")

        initiative = (await db.execute(
            select(AgentInitiative)
            .where(AgentInitiative.id == marker.initiative_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if initiative is None:
            raise InitiativeAdmissionRejected("initiative_not_found")
        driver = (await db.execute(
            select(ScheduledJob)
            .where(
                ScheduledJob.agent_initiative_id == initiative.id,
                ScheduledJob.execution_type == "agent_initiative",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        state = (await db.execute(
            select(AgentInitiativeState)
            .where(AgentInitiativeState.initiative_id == initiative.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if state is None:
            state = AgentInitiativeState(initiative_id=initiative.id, version=1)
            db.add(state)
            await db.flush()
        run = (await db.execute(
            select(AgentInitiativeRun)
            .where(
                AgentInitiativeRun.id == marker.initiative_run_id,
                AgentInitiativeRun.initiative_id == marker.initiative_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if (
            run is None
            or str(run.task_id or "") != task.id
            or str(run.scheduled_run_id or "") != str(scheduled_run_id or "")
        ):
            raise InitiativeAdmissionRejected("initiative_run_lineage_mismatch")
        if run.status in {"succeeded", "failed", "filtered"}:
            one_shot_changed = await _complete_one_shot_initiative(
                db,
                initiative=initiative,
                driver=driver,
                run=run,
            )
            pending = (await db.execute(
                select(AgentInitiativeDelivery.id).where(
                    AgentInitiativeDelivery.initiative_run_id == run.id,
                    AgentInitiativeDelivery.status.in_(tuple(_DELIVERY_PENDING_STATUSES)),
                ).limit(1)
            )).scalar_one_or_none()
            if one_shot_changed:
                await db.commit()
            else:
                await db.rollback()
            return run.id if pending else None

        now = datetime.now(timezone.utc)
        state.last_checked_at = now
        state.version = int(state.version or 0) + 1
        if result.get("status") != "completed":
            run.status = "failed"
            run.reason_code = "initiative_task_failed"
            run.error = str(
                result.get("error")
                or result.get("error_type")
                or result.get("response")
                or "Initiative Task failed"
            )[:2_000]
            run.completed_at = now
            await db.commit()
            return None

        decision, validation_error = parse_initiative_decision(
            result.get("initiative_decision")
        )
        if decision is None:
            run.status = "failed"
            run.reason_code = "invalid_decision_output"
            run.error = str(validation_error or "invalid Initiative decision")[:2_000]
            run.completed_at = now
            await db.commit()
            return None

        run.candidate_decision = decision.model_dump(mode="json", exclude_none=True)
        run.reason = decision.reason
        run.message = decision.message if decision.decision == "notify" else None
        if not initiative_result_revision_matches(
            initiative=initiative,
            marker=marker,
            run=run,
        ):
            reason_code = (
                "initiative_muted"
                if initiative.status == "muted"
                else "initiative_revision_changed"
                if initiative.status == "active"
                else "initiative_not_active"
            )
            run.final_decision = "suppressed"
            run.reason_code = reason_code
            run.status = "succeeded"
            run.completed_at = now
            state.last_silent_reason = reason_code
            await db.commit()
            return None

        # Re-read all persisted JSON at the authority boundary.
        policy = DecisionPolicy.model_validate(initiative.decision_policy or {})
        delivery_policy = DeliveryPolicy.model_validate(initiative.delivery_policy or {})
        InterruptionPolicy.model_validate(initiative.interruption_policy or {})
        reservations = await _delivery_reservations(
            db,
            initiative_id=initiative.id,
            current_run_id=run.id,
        )
        policy_result = evaluate_initiative_policy(
            decision=decision,
            initiative_status=initiative.status,
            policy=policy,
            last_fingerprint=state.last_fingerprint,
            reserved_fingerprints=reservations.fingerprints,
            cooldown_until=state.cooldown_until,
            window_started_at=state.window_started_at,
            notification_count=state.notification_count,
            now=now,
            allow_paused_delivery=run.trigger_type == "manual",
            pending_delivery_count=reservations.pending_count,
        )
        run.final_decision = policy_result.final_decision
        run.reason_code = policy_result.reason_code
        run.status = "succeeded"
        run.completed_at = now

        specs = _delivery_specs(
            run=run,
            initiative=initiative,
            delivery_policy=delivery_policy,
        )
        if not policy_result.should_deliver:
            state.last_silent_reason = policy_result.reason_code
            if decision.decision == "notify":
                for kind, idempotency_key, recipient_user_id in specs:
                    db.add(AgentInitiativeDelivery(
                        id=generate_ulid(),
                        initiative_run_id=run.id,
                        kind=kind,
                        recipient_user_id=recipient_user_id,
                        status="suppressed",
                        idempotency_key=idempotency_key,
                        suppressed_reason=policy_result.reason_code,
                    ))
            await _complete_one_shot_initiative(
                db,
                initiative=initiative,
                driver=driver,
                run=run,
            )
            await db.commit()
            return None

        state.last_silent_reason = None
        for kind, idempotency_key, recipient_user_id in specs:
            db.add(AgentInitiativeDelivery(
                id=generate_ulid(),
                initiative_run_id=run.id,
                kind=kind,
                recipient_user_id=recipient_user_id,
                status="pending",
                idempotency_key=idempotency_key,
            ))
        await _complete_one_shot_initiative(
            db,
            initiative=initiative,
            driver=driver,
            run=run,
        )
        await db.commit()
        return run.id


__all__ = [
    "INITIATIVE_BILLING_SOURCE",
    "INITIATIVE_TASK_MARKER_KEY",
    "InitiativeAdmission",
    "InitiativeAdmissionRejected",
    "InitiativePolicyResult",
    "InitiativeTaskMarker",
    "admit_scheduled_initiative",
    "evaluate_initiative_policy",
    "fail_admitted_initiative_run",
    "has_initiative_task_marker",
    "initiative_decision_fingerprint",
    "initiative_decision_repair_prompt",
    "initiative_marker_for_admission",
    "initiative_result_revision_matches",
    "initiative_task_marker",
    "mark_initiative_driver_needs_setup",
    "mark_initiative_run_started",
    "parse_initiative_decision",
    "settle_initiative_task",
]
