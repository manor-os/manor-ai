"""Authoritative CRUD, authorization, and scheduler-driver projection.

``AgentInitiative`` is the product object.  Its ``ScheduledJob`` is an
implementation detail and is only written from this module.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.contracts.agent_initiative import (
    ConditionSpec,
    DecisionPolicy,
    DeliveryPolicy,
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
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.revisions import (
    AGENT_INITIATIVE_CONTENT_REVISION_FIELDS,
    assert_revision,
    bump_revision,
    content_patch_for,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
)
from packages.core.services.scheduler_service import (
    ScheduledJobMutationFactory,
    create_scheduled_job,
)
from packages.core.services.workspace_access import (
    user_can_read_workspace_id,
    user_can_write_workspace_id,
)
from packages.core.services.workspace_autonomy import workspace_allows_manual_work


ACTIVE_DRIVER_STATUSES = frozenset({"active", "muted"})
MANAGEABLE_STATUSES = frozenset({
    "draft", "active", "muted", "paused", "needs_setup", "error", "completed",
})


class AgentInitiativeServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class InitiativeAccess:
    subscription: AgentSubscription
    can_manage: bool


def _contract_dict(value: Any) -> dict:
    return value.model_dump(mode="json")


def initiative_contracts(initiative: AgentInitiative) -> tuple[
    TriggerSpec, ConditionSpec, DecisionPolicy, DeliveryPolicy, InterruptionPolicy,
]:
    """Validate persisted JSON again at every authority boundary."""

    return (
        TriggerSpec.model_validate(initiative.trigger_config or {}),
        ConditionSpec.model_validate(initiative.condition_config or {}),
        DecisionPolicy.model_validate(initiative.decision_policy or {}),
        DeliveryPolicy.model_validate(initiative.delivery_policy or {}),
        InterruptionPolicy.model_validate(initiative.interruption_policy or {}),
    )


async def _active_user(db: AsyncSession, user_id: str, entity_id: str) -> User:
    user = (await db.execute(
        select(User).where(
            User.id == user_id,
            User.entity_id == entity_id,
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if user is None:
        raise AgentInitiativeServiceError(
            "initiative_owner_inactive",
            "Initiative owner is not active",
            status_code=409,
        )
    return user


async def _subscription_access(
    db: AsyncSession,
    *,
    subscription_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    manage: bool,
    lock: bool = False,
) -> InitiativeAccess:
    query = select(AgentSubscription).where(
        AgentSubscription.id == subscription_id,
        AgentSubscription.entity_id == entity_id,
        AgentSubscription.status == "active",
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    subscription = (await db.execute(query)).scalar_one_or_none()
    if subscription is None:
        raise AgentInitiativeServiceError(
            "initiative_subscription_inactive",
            "Agent subscription is not active",
            status_code=404,
        )

    if subscription.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == subscription.workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not workspace_allows_manual_work(workspace):
            raise AgentInitiativeServiceError(
                "initiative_workspace_inactive",
                "Agent subscription Workspace is unavailable",
                status_code=409,
            )
        readable = await user_can_read_workspace_id(
            db,
            workspace_id=subscription.workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=user_role,
        )
        if not readable:
            raise AgentInitiativeServiceError(
                "initiative_not_found", "Initiative not found", status_code=404,
            )
        writable = await user_can_write_workspace_id(
            db,
            workspace_id=subscription.workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=user_role,
        )
        if manage and not writable:
            raise AgentInitiativeServiceError(
                "initiative_forbidden",
                "Workspace write access is required",
                status_code=403,
            )
        can_manage = writable
    else:
        if not subscription.owner_user_id:
            raise AgentInitiativeServiceError(
                "initiative_subscription_unowned",
                "Legacy personal subscription must be claimed before it can run initiatives",
                status_code=409,
            )
        if str(subscription.owner_user_id) != str(user_id):
            raise AgentInitiativeServiceError(
                "initiative_not_found", "Initiative not found", status_code=404,
            )
        can_manage = True

    if not is_master_agent(subscription.agent_id):
        agent = (await db.execute(
            select(Agent.id).where(
                Agent.id == subscription.agent_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if agent is None:
            raise AgentInitiativeServiceError(
                "initiative_subscription_inactive",
                "Agent is not active",
                status_code=409,
            )
    return InitiativeAccess(subscription=subscription, can_manage=can_manage)


async def _validate_conversation(
    db: AsyncSession,
    *,
    subscription: AgentSubscription,
    owner_user_id: str,
    delivery: DeliveryPolicy,
    bind_personal: bool,
) -> Conversation:
    surface = delivery.surface
    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == surface.conversation_id,
            Conversation.entity_id == subscription.entity_id,
            Conversation.status == "active",
        )
    )).scalar_one_or_none()
    if conversation is None:
        raise AgentInitiativeServiceError(
            "initiative_conversation_invalid",
            "Delivery conversation is unavailable",
            status_code=409,
        )

    if subscription.workspace_id:
        if (
            conversation.workspace_id != subscription.workspace_id
            or conversation.scope not in {"workspace_main", "workspace_thread"}
        ):
            raise AgentInitiativeServiceError(
                "initiative_conversation_invalid",
                "Delivery conversation does not belong to the Agent Workspace",
                status_code=409,
            )
        if surface.kind != "workspace_chat":
            raise AgentInitiativeServiceError(
                "initiative_conversation_invalid",
                "Workspace initiatives must use the workspace_chat surface",
                status_code=409,
            )
    else:
        personal_agent_matches = (
            str(conversation.agent_id or "") == str(subscription.agent_id or "")
            or (
                conversation.agent_id is None
                and is_master_agent(subscription.agent_id)
            )
        )
        if (
            surface.kind != "conversation"
            or conversation.workspace_id is not None
            or conversation.user_id != owner_user_id
            or not personal_agent_matches
            or (
                conversation.agent_subscription_id
                and conversation.agent_subscription_id != subscription.id
            )
        ):
            raise AgentInitiativeServiceError(
                "initiative_conversation_invalid",
                "Delivery conversation is not the owner's exact Agent conversation",
                status_code=409,
            )
        if bind_personal and conversation.agent_subscription_id is None:
            conversation.agent_subscription_id = subscription.id
            await db.flush()
    return conversation


def _driver_fields(
    initiative: AgentInitiative,
    subscription: AgentSubscription,
) -> dict[str, Any]:
    trigger, condition, decision, delivery, interruption = initiative_contracts(initiative)
    schedule_kind = {
        "cron": "cron",
        "interval": "every",
        "at": "at",
    }[trigger.kind]
    return {
        "name": f"[Initiative] {initiative.name}"[:255],
        "job_type": "agent_initiative",
        "schedule_kind": schedule_kind,
        "cron_expr": trigger.cron if trigger.kind == "cron" else None,
        "every_seconds": (
            float(trigger.every_seconds) if trigger.kind == "interval" else None
        ),
        "run_at": trigger.run_at.isoformat() if trigger.kind == "at" else None,
        "timezone": trigger.timezone,
        "payload_message": None,
        "agent_id": subscription.agent_id,
        "execution_type": "agent_initiative",
        "execution_target": {
            "managed_by": "agent_initiative",
            "initiative_id": initiative.id,
            "initiative_revision": int(initiative.revision or 1),
            "subscription_id": subscription.id,
            "owner_user_id": initiative.owner_user_id,
            "complexity": "primary",
            "max_turns": 8,
            "blocked_tool_names": [
                "create_agent_initiative",
                "update_agent_initiative",
                "delete_agent_initiative",
                "mute_agent_initiative",
                "unmute_agent_initiative",
                "pause_agent_initiative",
                "resume_agent_initiative",
                "run_agent_initiative_now",
                "notify_user",
            ],
        },
        "execution_script": initiative_execution_script(
            initiative,
            condition=condition,
            decision=decision,
            interruption=interruption,
        ),
        "workspace_id": subscription.workspace_id,
        "conversation_id": delivery.surface.conversation_id,
        "default_delivery_mode": None,
        "user_id": initiative.owner_user_id,
        "enabled": initiative.status in ACTIVE_DRIVER_STATUSES,
        "delete_after_run": trigger.kind == "at",
    }


def initiative_execution_script(
    initiative: AgentInitiative,
    *,
    condition: ConditionSpec | None = None,
    decision: DecisionPolicy | None = None,
    interruption: InterruptionPolicy | None = None,
) -> str:
    if condition is None or decision is None or interruption is None:
        _, condition, decision, _, interruption = initiative_contracts(initiative)
    condition_text = condition.semantic_condition or "No additional semantic condition."
    mode_instruction = {
        "always": (
            "Return decision=notify after every successful check, even when the "
            "visible result is unchanged."
        ),
        "on_condition": (
            "Return decision=notify exactly when the notification condition is "
            "satisfied; unchanged results may still notify. Otherwise return silent."
        ),
        "on_change": (
            "Return decision=notify only when the notification condition is "
            "satisfied and the user-visible facts materially changed. Reuse a stable "
            "dedupe_key for the same facts; otherwise return silent."
        ),
    }[decision.mode]
    return f"""Run the proactive Agent Initiative named {initiative.name!r}.

Instruction:
{initiative.instruction}

Notification condition:
{condition_text}

Decision mode: {decision.mode}.
{mode_instruction}

Perform the requested research or checks using authorized read/action tools. Do
not send a message, email, notification, or mutate this Initiative yourself;
the platform owns delivery and lifecycle. Your final response MUST be one JSON
object and nothing else, with exactly this shape:
{{"version":"1","decision":"notify|silent","message":"required only for notify","reason_code":"lowercase_code","reason":"optional explanation","severity":"info|warn","dedupe_key":"optional stable key","evidence_refs":[{{"type":"source","id":"optional","url":"optional","label":"optional"}}]}}

Use decision="silent" and omit message when the condition is not met or there
is nothing new. Free-form output is treated as a failed run and is never sent.
""".strip()


async def _sync_driver(
    db: AsyncSession,
    initiative: AgentInitiative,
    subscription: AgentSubscription,
) -> ScheduledJob:
    fields = _driver_fields(initiative, subscription)
    job = (await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.agent_initiative_id == initiative.id,
        )
    )).scalar_one_or_none()
    if job is None:
        job = await create_scheduled_job(
            db,
            initiative.entity_id,
            f"initiative:{initiative.id}",
            fields["name"],
            job_type=fields["job_type"],
            schedule_kind=fields["schedule_kind"],
            cron_expr=fields["cron_expr"],
            every_seconds=fields["every_seconds"],
            run_at=fields["run_at"],
            timezone_str=fields["timezone"],
            payload_message=None,
            agent_id=fields["agent_id"],
            execution_type=fields["execution_type"],
            execution_target=fields["execution_target"],
            execution_script=fields["execution_script"],
            workspace_id=fields["workspace_id"],
            conversation_id=fields["conversation_id"],
            default_delivery_mode=None,
            user_id=fields["user_id"],
            require_workspace=bool(fields["workspace_id"]),
            publish_realtime=False,
        )
        job.agent_initiative_id = initiative.id
        job.enabled = fields["enabled"]
        job.delete_after_run = fields["delete_after_run"]
        from packages.core.schedule_clock import refresh_scheduled_job_next_run_at

        refresh_scheduled_job_next_run_at(job)
        await db.flush()
        return job

    mutable_fields = {
        key: value
        for key, value in fields.items()
        if key not in {"workspace_id", "agent_id", "user_id"}
    }
    await ScheduledJobMutationFactory.apply(
        db,
        job,
        mutable_fields,
        changed_by_kind="user",
        changed_by_id=initiative.owner_user_id,
        causation_id=initiative.id,
        allow_agent_initiative_driver=True,
    )
    return job


async def prepare_workspace_initiatives_for_driver_teardown(
    db: AsyncSession,
    *,
    initiative_ids: Iterable[str],
    workspace_id: str,
    entity_id: str,
) -> int:
    """Make runnable Workspace Initiatives truthful before driver deletion.

    The caller owns the entity lifecycle fence and has already locked the
    corresponding ``ScheduledJob`` rows.  Workspace restore intentionally does
    not restart these Initiatives; an explicit resume recreates the internal
    driver through :func:`_sync_driver`.
    """

    scoped_ids = sorted({str(value) for value in initiative_ids if value})
    if not scoped_ids:
        return 0

    initiatives = list((await db.execute(
        select(AgentInitiative)
        .where(
            AgentInitiative.id.in_(scoped_ids),
            AgentInitiative.entity_id == entity_id,
            AgentInitiative.workspace_id == workspace_id,
        )
        .order_by(AgentInitiative.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())

    transitioned = 0
    for initiative in initiatives:
        if initiative.status not in ACTIVE_DRIVER_STATUSES:
            continue
        initiative.status = "needs_setup"
        await bump_revision(
            db,
            initiative,
            patch={"status": "needs_setup"},
            changed_by_kind="system",
            causation_id=workspace_id,
        )
        transitioned += 1
    return transitioned


async def create_initiative(
    db: AsyncSession,
    *,
    subscription_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    name: str,
    instruction: str,
    trigger: TriggerSpec,
    condition: ConditionSpec,
    decision_policy: DecisionPolicy,
    delivery_policy: DeliveryPolicy,
    interruption_policy: InterruptionPolicy,
    status: str = "active",
) -> AgentInitiative:
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    access = await _subscription_access(
        db,
        subscription_id=subscription_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        manage=True,
        lock=True,
    )
    subscription = access.subscription
    if subscription.workspace_id:
        raise AgentInitiativeServiceError(
            "initiative_private_chat_only",
            "New Agent initiatives require a private Chat subscription",
            status_code=409,
        )
    await _active_user(db, user_id, entity_id)
    await _validate_conversation(
        db,
        subscription=subscription,
        owner_user_id=user_id,
        delivery=delivery_policy,
        bind_personal=True,
    )
    initiative = AgentInitiative(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=subscription.workspace_id,
        agent_subscription_id=subscription.id,
        owner_user_id=user_id,
        name=name.strip(),
        instruction=instruction.strip(),
        trigger_kind=trigger.kind,
        trigger_source="schedule",
        trigger_config=_contract_dict(trigger),
        condition_config=_contract_dict(condition),
        decision_policy=_contract_dict(decision_policy),
        delivery_policy=_contract_dict(delivery_policy),
        interruption_policy=_contract_dict(interruption_policy),
        status=status,
        revision=1,
    )
    db.add(initiative)
    db.add(AgentInitiativeState(initiative_id=initiative.id, version=1))
    await db.flush()
    await _sync_driver(db, initiative, subscription)
    await db.refresh(initiative)
    return initiative


async def list_initiatives(
    db: AsyncSession,
    *,
    subscription_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
) -> tuple[list[AgentInitiative], bool]:
    access = await _subscription_access(
        db,
        subscription_id=subscription_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        manage=False,
    )
    rows = list((await db.execute(
        select(AgentInitiative).where(
            AgentInitiative.agent_subscription_id == subscription_id,
            AgentInitiative.entity_id == entity_id,
            AgentInitiative.status != "deleted",
        ).order_by(AgentInitiative.created_at.desc(), AgentInitiative.id.desc())
    )).scalars().all())
    return rows, access.can_manage


async def list_conversation_initiatives(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
) -> tuple[Conversation, list[tuple[AgentInitiative, bool]]]:
    """List only Initiatives whose durable surface is this exact Chat.

    Workspace conversations may have several Agent deployments, so this read
    path deliberately does not infer one from the Workspace. The persisted
    delivery surface is the binding authority; subscription access is then
    revalidated for every matching row.
    """

    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
            Conversation.status == "active",
        )
    )).scalar_one_or_none()
    if conversation is None:
        raise AgentInitiativeServiceError(
            "initiative_conversation_invalid",
            "Conversation is unavailable",
            status_code=404,
        )

    if conversation.workspace_id:
        if (
            conversation.channel != "workspace"
            or conversation.scope not in {"workspace_main", "workspace_thread"}
        ):
            raise AgentInitiativeServiceError(
                "initiative_conversation_invalid",
                "Conversation is unavailable",
                status_code=404,
            )
        if not await user_can_read_workspace_id(
            db,
            workspace_id=conversation.workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=user_role,
        ):
            raise AgentInitiativeServiceError(
                "initiative_conversation_invalid",
                "Conversation is unavailable",
                status_code=404,
            )
        scope_filter = AgentInitiative.workspace_id == conversation.workspace_id
    else:
        if (
            conversation.channel != "web"
            or conversation.scope != "channel"
            or conversation.user_id != user_id
        ):
            raise AgentInitiativeServiceError(
                "initiative_conversation_invalid",
                "Conversation is unavailable",
                status_code=404,
            )
        scope_filter = AgentInitiative.workspace_id.is_(None)

    candidates = list((await db.execute(
        select(AgentInitiative).where(
            AgentInitiative.entity_id == entity_id,
            AgentInitiative.status != "deleted",
            scope_filter,
            AgentInitiative.delivery_policy["surface"]["conversation_id"].astext
            == conversation_id,
        ).order_by(AgentInitiative.created_at.desc(), AgentInitiative.id.desc())
    )).scalars().all())

    access_by_subscription: dict[str, InitiativeAccess | None] = {}
    visible: list[tuple[AgentInitiative, bool]] = []
    for initiative in candidates:
        subscription_id = initiative.agent_subscription_id
        if subscription_id not in access_by_subscription:
            try:
                access_by_subscription[subscription_id] = await _subscription_access(
                    db,
                    subscription_id=subscription_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    user_role=user_role,
                    manage=False,
                )
            except AgentInitiativeServiceError:
                # Stale or inaccessible deployments remain invisible without
                # breaking other Initiative rows in the same Conversation.
                access_by_subscription[subscription_id] = None
        access = access_by_subscription[subscription_id]
        if access is None:
            continue

        delivery = DeliveryPolicy.model_validate(initiative.delivery_policy or {})
        surface = delivery.surface
        if surface.conversation_id != conversation.id:
            continue
        subscription = access.subscription
        if conversation.workspace_id:
            if (
                surface.kind != "workspace_chat"
                or subscription.workspace_id != conversation.workspace_id
            ):
                continue
        else:
            conversation_agent_matches = (
                conversation.agent_id == subscription.agent_id
                or (
                    conversation.agent_id is None
                    and is_master_agent(subscription.agent_id)
                )
            )
            if (
                surface.kind != "conversation"
                or subscription.workspace_id is not None
                or subscription.owner_user_id != user_id
                or not conversation_agent_matches
                or (
                    conversation.agent_subscription_id
                    and conversation.agent_subscription_id != subscription.id
                )
            ):
                continue
        visible.append((initiative, access.can_manage))

    return conversation, visible


async def get_initiative(
    db: AsyncSession,
    *,
    initiative_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    manage: bool = False,
    lock: bool = False,
) -> tuple[AgentInitiative, InitiativeAccess]:
    query = select(AgentInitiative).where(
        AgentInitiative.id == initiative_id,
        AgentInitiative.entity_id == entity_id,
        AgentInitiative.status != "deleted",
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    initiative = (await db.execute(query)).scalar_one_or_none()
    if initiative is None:
        raise AgentInitiativeServiceError(
            "initiative_not_found", "Initiative not found", status_code=404,
        )
    access = await _subscription_access(
        db,
        subscription_id=initiative.agent_subscription_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        manage=manage,
    )
    return initiative, access


async def update_initiative(
    db: AsyncSession,
    *,
    initiative_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    expected_revision: int,
    patch: dict[str, Any],
) -> AgentInitiative:
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    initiative, access = await get_initiative(
        db,
        initiative_id=initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        manage=True,
        lock=True,
    )
    await assert_revision(initiative, expected_revision)
    field_map = {
        "trigger": "trigger_config",
        "condition": "condition_config",
        "decision_policy": "decision_policy",
        "delivery_policy": "delivery_policy",
        "interruption_policy": "interruption_policy",
    }
    updates: dict[str, Any] = {}
    for key, value in patch.items():
        target = field_map.get(key, key)
        updates[target] = _contract_dict(value) if hasattr(value, "model_dump") else value
    if "trigger_config" in updates:
        updates["trigger_kind"] = TriggerSpec.model_validate(
            updates["trigger_config"]
        ).kind
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip()
    if "instruction" in updates:
        updates["instruction"] = str(updates["instruction"]).strip()
    if "delivery_policy" in updates:
        await _validate_conversation(
            db,
            subscription=access.subscription,
            owner_user_id=initiative.owner_user_id,
            delivery=DeliveryPolicy.model_validate(updates["delivery_policy"]),
            bind_personal=True,
        )
    content_patch = content_patch_for(
        initiative,
        updates,
        AGENT_INITIATIVE_CONTENT_REVISION_FIELDS,
        include_none=True,
    )
    for key, value in updates.items():
        setattr(initiative, key, value)
    if content_patch:
        await bump_revision(
            db,
            initiative,
            patch=content_patch,
            changed_by_kind="user",
            changed_by_id=user_id,
        )
    await db.flush()
    await _sync_driver(db, initiative, access.subscription)
    # TimestampMixin.updated_at is populated by a server-side SQL expression
    # and is expired after UPDATE.  Refresh inside the async boundary so API
    # response serialization never attempts implicit synchronous I/O.
    await db.refresh(initiative)
    return initiative


async def transition_initiative(
    db: AsyncSession,
    *,
    initiative_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    expected_revision: int,
    action: str,
) -> AgentInitiative:
    transitions = {
        "mute": ({"active"}, "muted"),
        "unmute": ({"muted"}, "active"),
        "pause": (MANAGEABLE_STATUSES - {"paused", "completed"}, "paused"),
        "resume": ({"paused", "error", "needs_setup"}, "active"),
    }
    if action not in transitions:
        raise ValueError(f"unknown initiative action {action!r}")
    initiative, _ = await get_initiative(
        db,
        initiative_id=initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        manage=True,
    )
    allowed, target = transitions[action]
    if initiative.status not in allowed:
        raise AgentInitiativeServiceError(
            "initiative_invalid_transition",
            f"Cannot {action} an Initiative in status {initiative.status!r}",
            status_code=409,
        )
    return await _set_status(
        db,
        initiative_id=initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        expected_revision=expected_revision,
        status=target,
    )


async def _set_status(
    db: AsyncSession,
    *,
    initiative_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    expected_revision: int,
    status: str,
) -> AgentInitiative:
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    initiative, access = await get_initiative(
        db,
        initiative_id=initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        manage=True,
        lock=True,
    )
    await assert_revision(initiative, expected_revision)
    if initiative.status != status:
        initiative.status = status
        await bump_revision(
            db,
            initiative,
            patch={"status": status},
            changed_by_kind="user",
            changed_by_id=user_id,
        )
    await _sync_driver(db, initiative, access.subscription)
    await db.refresh(initiative)
    return initiative


async def delete_initiative(
    db: AsyncSession,
    *,
    initiative_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    expected_revision: int,
) -> None:
    await _set_status(
        db,
        initiative_id=initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
        expected_revision=expected_revision,
        status="deleted",
    )


async def initiative_driver(
    db: AsyncSession,
    initiative_id: str,
) -> ScheduledJob | None:
    return (await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.agent_initiative_id == initiative_id,
        )
    )).scalar_one_or_none()


async def initiative_state(
    db: AsyncSession,
    initiative_id: str,
) -> AgentInitiativeState | None:
    return await db.get(AgentInitiativeState, initiative_id)


async def list_initiative_runs(
    db: AsyncSession,
    *,
    initiative_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
    before_id: str | None = None,
    limit: int = 50,
) -> list[AgentInitiativeRun]:
    await get_initiative(
        db,
        initiative_id=initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
    )
    query = select(AgentInitiativeRun).where(
        AgentInitiativeRun.initiative_id == initiative_id,
    )
    if before_id:
        query = query.where(AgentInitiativeRun.id < before_id)
    return list((await db.execute(
        query.order_by(AgentInitiativeRun.id.desc()).limit(limit)
    )).scalars().all())


async def get_initiative_run(
    db: AsyncSession,
    *,
    run_id: str,
    entity_id: str,
    user_id: str,
    user_role: str | None,
) -> AgentInitiativeRun:
    run = await db.get(AgentInitiativeRun, run_id)
    if run is None:
        raise AgentInitiativeServiceError(
            "initiative_run_not_found", "Initiative run not found", status_code=404,
        )
    await get_initiative(
        db,
        initiative_id=run.initiative_id,
        entity_id=entity_id,
        user_id=user_id,
        user_role=user_role,
    )
    return run


async def initiative_run_deliveries(
    db: AsyncSession,
    run_ids: list[str],
) -> dict[str, list[AgentInitiativeDelivery]]:
    if not run_ids:
        return {}
    rows = list((await db.execute(
        select(AgentInitiativeDelivery).where(
            AgentInitiativeDelivery.initiative_run_id.in_(run_ids),
        ).order_by(AgentInitiativeDelivery.created_at.asc())
    )).scalars().all())
    grouped: dict[str, list[AgentInitiativeDelivery]] = {}
    for row in rows:
        grouped.setdefault(row.initiative_run_id, []).append(row)
    return grouped


__all__ = [
    "ACTIVE_DRIVER_STATUSES",
    "AgentInitiativeServiceError",
    "create_initiative",
    "delete_initiative",
    "get_initiative",
    "get_initiative_run",
    "initiative_contracts",
    "initiative_driver",
    "initiative_execution_script",
    "initiative_run_deliveries",
    "initiative_state",
    "list_conversation_initiatives",
    "list_initiative_runs",
    "list_initiatives",
    "transition_initiative",
    "update_initiative",
]
