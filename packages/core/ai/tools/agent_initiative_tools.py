"""Conversation-scoped tools for managing proactive Agent Initiatives."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent
from packages.core.contracts.agent_initiative import (
    ConditionSpec,
    DecisionPolicy,
    DeliveryPolicy,
    DeliverySurface,
    InterruptionPolicy,
    NotificationPolicy,
    QuietHours,
    TriggerSpec,
)
from packages.core.database import async_session
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.models.workspace import AgentSubscription
from packages.core.revisions import StaleRevisionError
from packages.core.schemas.agent_initiative import AgentInitiativePatch
from packages.core.services.agent_initiative_service import (
    AgentInitiativeServiceError,
    create_initiative,
    delete_initiative,
    get_initiative,
    initiative_driver,
    initiative_state,
    list_initiative_runs,
    list_initiatives,
    transition_initiative,
    update_initiative,
)
from packages.core.services.agent_subscription_service import (
    ensure_personal_subscription,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
)


_INTERNAL_SURFACES = {
    ChatSurface.GLOBAL_OWNER_CHAT.value,
    ChatSurface.AGENT_DM.value,
}
_INTERNAL_PROFILES = {
    RuntimeProfile.OWNER_COPILOT.value,
    RuntimeProfile.AGENT_DELEGATE.value,
}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _error(code: str, message: str, **extra: Any) -> str:
    return _json({"ok": False, "error": code, "message": message, **extra})


def _surface_value(context) -> str:
    value = getattr(getattr(context.runtime_envelope, "surface", None), "value", None)
    return str(value or "")


def _profile_value(context) -> str:
    value = getattr(getattr(context.runtime_envelope, "profile", None), "value", None)
    return str(value or "")


async def _actor(db, context) -> User:
    if not context.entity_id or not context.user_id:
        raise AgentInitiativeServiceError(
            "initiative_actor_required",
            "An authenticated user is required",
            status_code=403,
        )
    if (
        _surface_value(context) not in _INTERNAL_SURFACES
        or _profile_value(context) not in _INTERNAL_PROFILES
    ):
        raise AgentInitiativeServiceError(
            "initiative_surface_forbidden",
            "Agent Initiatives are available only in private Agent or Manor Chat; use a Workspace automation in a Workspace",
            status_code=403,
        )
    user = (await db.execute(select(User).where(
        User.id == context.user_id,
        User.entity_id == context.entity_id,
        User.status == "active",
        User.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if user is None:
        raise AgentInitiativeServiceError(
            "initiative_actor_required", "User is not active", status_code=403,
        )
    return user


async def _current_conversation(db, *, context, user: User) -> Conversation:
    if not context.conversation_id:
        raise AgentInitiativeServiceError(
            "initiative_conversation_invalid",
            "Manage Agent Initiatives from the Chat that should receive updates",
            status_code=409,
        )
    conversation = (await db.execute(select(Conversation).where(
        Conversation.id == context.conversation_id,
        Conversation.entity_id == user.entity_id,
        Conversation.status == "active",
    ))).scalar_one_or_none()
    if conversation is None:
        raise AgentInitiativeServiceError(
            "initiative_conversation_invalid",
            "Current conversation is unavailable",
            status_code=409,
        )
    return conversation


def _validate_subscription_scope(
    *,
    context,
    user: User,
    conversation: Conversation,
    subscription: AgentSubscription,
) -> None:
    """Prove that a model-selected Subscription is the current Chat scope."""

    surface = _surface_value(context)
    if (
        subscription.entity_id != user.entity_id
        or subscription.status != "active"
    ):
        raise AgentInitiativeServiceError(
            "initiative_subscription_inactive",
            "Agent subscription is unavailable",
            status_code=404,
        )

    if surface == ChatSurface.WORKSPACE_CHAT.value:
        if (
            not conversation.workspace_id
            or conversation.workspace_id != context.workspace_id
            or conversation.scope not in {"workspace_main", "workspace_thread"}
            or subscription.workspace_id != conversation.workspace_id
            or (
                conversation.agent_subscription_id
                and conversation.agent_subscription_id != subscription.id
            )
            or (
                context.agent_id
                and not is_master_agent(context.agent_id)
                and subscription.agent_id != context.agent_id
            )
        ):
            raise AgentInitiativeServiceError(
                "initiative_subscription_ambiguous",
                "The selected deployment is not bound to this Workspace Chat",
                status_code=409,
            )
        return

    expected_agent_id = str(
        context.agent_id or conversation.agent_id or MANOR_AGENT_ID
    )
    if (
        conversation.workspace_id is not None
        or conversation.user_id != user.id
        or subscription.workspace_id is not None
        or subscription.owner_user_id != user.id
        or subscription.agent_id != expected_agent_id
        or (
            conversation.agent_id is not None
            and conversation.agent_id != subscription.agent_id
        )
        or (
            conversation.agent_subscription_id
            and conversation.agent_subscription_id != subscription.id
        )
    ):
        raise AgentInitiativeServiceError(
            "initiative_subscription_ambiguous",
            "The selected deployment is not bound to this personal Agent Chat",
            status_code=409,
        )


async def _current_subscription(
    db,
    *,
    context,
    user: User,
    requested_subscription_id: str | None = None,
    create_if_missing: bool = False,
) -> AgentSubscription | None:
    conversation = await _current_conversation(db, context=context, user=user)

    if requested_subscription_id:
        subscription = (await db.execute(select(AgentSubscription).where(
            AgentSubscription.id == requested_subscription_id,
            AgentSubscription.entity_id == user.entity_id,
            AgentSubscription.status == "active",
        ))).scalar_one_or_none()
        if subscription is None:
            raise AgentInitiativeServiceError(
                "initiative_subscription_inactive",
                "Agent subscription is unavailable",
                status_code=404,
            )
        _validate_subscription_scope(
            context=context,
            user=user,
            conversation=conversation,
            subscription=subscription,
        )
        return subscription

    if conversation.agent_subscription_id:
        subscription = await db.get(
            AgentSubscription, conversation.agent_subscription_id
        )
        if subscription is None:
            raise AgentInitiativeServiceError(
                "initiative_subscription_inactive",
                "The Agent deployment bound to this Chat is unavailable",
                status_code=404,
            )
        _validate_subscription_scope(
            context=context,
            user=user,
            conversation=conversation,
            subscription=subscription,
        )
        return subscription

    if conversation.workspace_id:
        query = select(AgentSubscription).where(
            AgentSubscription.entity_id == user.entity_id,
            AgentSubscription.workspace_id == conversation.workspace_id,
            AgentSubscription.status == "active",
        )
        if context.agent_id and not is_master_agent(context.agent_id):
            query = query.where(AgentSubscription.agent_id == context.agent_id)
        candidates = list((await db.execute(query.limit(2))).scalars().all())
        if not candidates and not create_if_missing:
            return None
        if len(candidates) != 1:
            raise AgentInitiativeServiceError(
                "initiative_subscription_ambiguous",
                "Choose the Workspace Agent instance before creating an Initiative",
                status_code=409,
            )
        _validate_subscription_scope(
            context=context,
            user=user,
            conversation=conversation,
            subscription=candidates[0],
        )
        return candidates[0]

    agent_id = str(
        context.agent_id
        or (conversation.agent_id if conversation else None)
        or MANOR_AGENT_ID
    )
    subscription = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.entity_id == user.entity_id,
        AgentSubscription.agent_id == agent_id,
        AgentSubscription.owner_user_id == user.id,
        AgentSubscription.workspace_id.is_(None),
        AgentSubscription.status == "active",
    ).limit(1))).scalar_one_or_none()
    if subscription is None:
        if not create_if_missing:
            return None
        subscription = await ensure_personal_subscription(
            db,
            entity_id=user.entity_id,
            agent_id=agent_id,
            owner_user_id=user.id,
            name="Personal Manor AI" if agent_id == MANOR_AGENT_ID else "Personal",
        )
    _validate_subscription_scope(
        context=context,
        user=user,
        conversation=conversation,
        subscription=subscription,
    )
    if create_if_missing:
        conversation.agent_subscription_id = subscription.id
        await db.flush()
    return subscription


def _assert_current_initiative_scope(*, row, subscription, context) -> None:
    expected_kind = "workspace_chat" if subscription.workspace_id else "conversation"
    try:
        delivery = DeliveryPolicy.model_validate(row.delivery_policy or {})
    except ValidationError as exc:
        raise AgentInitiativeServiceError(
            "initiative_config_invalid",
            "Initiative delivery configuration is invalid",
            status_code=409,
        ) from exc
    if (
        row.agent_subscription_id != subscription.id
        or row.workspace_id != subscription.workspace_id
        or delivery.surface.kind != expected_kind
        or delivery.surface.conversation_id != context.conversation_id
    ):
        raise AgentInitiativeServiceError(
            "initiative_not_available_in_chat",
            "This Initiative does not belong to the current Agent Chat",
            status_code=404,
        )


async def _current_initiative(
    db,
    *,
    context,
    user: User,
    initiative_id: str,
    manage: bool = False,
):
    row, _ = await get_initiative(
        db,
        initiative_id=initiative_id,
        entity_id=user.entity_id,
        user_id=user.id,
        user_role=user.role,
        manage=manage,
    )
    subscription = await _current_subscription(
        db,
        context=context,
        user=user,
        requested_subscription_id=row.agent_subscription_id,
    )
    if subscription is None:  # Defensive: requested subscriptions never return None.
        raise AgentInitiativeServiceError(
            "initiative_subscription_inactive",
            "Agent subscription is unavailable",
            status_code=404,
        )
    _assert_current_initiative_scope(
        row=row,
        subscription=subscription,
        context=context,
    )
    return row, subscription


async def _with_errors(operation):
    try:
        return await operation()
    except StaleRevisionError as exc:
        return _error(
            "initiative_revision_conflict",
            "The Initiative changed; list it again before updating",
            expected=exc.expected,
            actual=exc.actual,
        )
    except AgentInitiativeServiceError as exc:
        return _error(exc.code, str(exc))
    except ValidationError as exc:
        return _error("initiative_config_invalid", str(exc))
    except ValueError as exc:
        return _error("initiative_config_invalid", str(exc))


async def _create_handler(
    name: str = "",
    instruction: str = "",
    schedule_kind: str = "cron",
    cron: str = "",
    every_seconds: int = 0,
    run_at: str = "",
    timezone: str = "UTC",
    condition: str = "",
    decision_mode: str = "on_condition",
    cooldown_seconds: int = 0,
    notification_enabled: bool = False,
    notification_channels: list[str] | None = None,
    quiet_hours_mode: str = "inherit_user",
    quiet_hours_start: str = "",
    quiet_hours_end: str = "",
    quiet_hours_timezone: str = "",
    subscription_id: str = "",
    **kwargs,
):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
            subscription = await _current_subscription(
                db,
                context=context,
                user=user,
                requested_subscription_id=subscription_id or None,
                create_if_missing=True,
            )
            if subscription is None:  # Workspace deployments are never implicit.
                raise AgentInitiativeServiceError(
                    "initiative_subscription_ambiguous",
                    "Choose the Agent deployment for this Workspace Chat",
                    status_code=409,
                )
            trigger = TriggerSpec(
                kind=schedule_kind,
                cron=cron or None,
                every_seconds=every_seconds or None,
                run_at=run_at or None,
                timezone=timezone,
            )
            quiet = QuietHours(
                mode=quiet_hours_mode,
                start=quiet_hours_start or None,
                end=quiet_hours_end or None,
                timezone=quiet_hours_timezone or None,
            )
            row = await create_initiative(
                db,
                subscription_id=subscription.id,
                entity_id=user.entity_id,
                user_id=user.id,
                user_role=user.role,
                name=name,
                instruction=instruction,
                trigger=trigger,
                condition=ConditionSpec(semantic_condition=condition or None),
                decision_policy=DecisionPolicy(
                    mode=decision_mode,
                    cooldown_seconds=cooldown_seconds,
                ),
                delivery_policy=DeliveryPolicy(
                    surface=DeliverySurface(
                        kind="workspace_chat" if subscription.workspace_id else "conversation",
                        conversation_id=context.conversation_id,
                    ),
                    notification=NotificationPolicy(
                        enabled=notification_enabled,
                        channels=(
                            notification_channels
                            if notification_channels is not None
                            else ["inapp"]
                        ),
                    ),
                ),
                interruption_policy=InterruptionPolicy(quiet_hours=quiet),
            )
            await db.commit()
            return _json({
                "ok": True,
                "created": True,
                "initiative": _initiative_receipt(row),
                "message": f"Created Initiative '{row.name}'.",
            })

    return await _with_errors(operation)


def _initiative_receipt(row, state=None) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "revision": row.revision,
        "agent_subscription_id": row.agent_subscription_id,
        "workspace_id": row.workspace_id,
        "instruction": row.instruction,
        "trigger": row.trigger_config,
        "condition": row.condition_config,
        "decision_policy": row.decision_policy,
        "delivery": row.delivery_policy,
        "interruption_policy": row.interruption_policy,
        "last_checked_at": getattr(state, "last_checked_at", None),
        "last_notified_at": getattr(state, "last_notified_at", None),
        "last_silent_reason": getattr(state, "last_silent_reason", None),
    }


def _initiative_list_receipt(row) -> dict[str, Any]:
    """Return only the identity and revision needed for a follow-up mutation."""
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "revision": row.revision,
    }


async def _list_handler(subscription_id: str = "", **kwargs):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            subscription = await _current_subscription(
                db,
                context=context,
                user=user,
                requested_subscription_id=subscription_id or None,
            )
            if subscription is None:
                return _json({"ok": True, "initiatives": [], "count": 0})
            rows, _ = await list_initiatives(
                db,
                subscription_id=subscription.id,
                entity_id=user.entity_id,
                user_id=user.id,
                user_role=user.role,
            )
            receipts = []
            for row in rows:
                try:
                    _assert_current_initiative_scope(
                        row=row,
                        subscription=subscription,
                        context=context,
                    )
                except AgentInitiativeServiceError:
                    continue
                receipts.append(_initiative_list_receipt(row))
            return _json({"ok": True, "initiatives": receipts, "count": len(receipts)})

    return await _with_errors(operation)


async def _get_handler(initiative_id: str = "", **kwargs):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            row, _ = await _current_initiative(
                db,
                context=context,
                user=user,
                initiative_id=initiative_id,
            )
            return _json({
                "ok": True,
                "initiative": _initiative_receipt(row, await initiative_state(db, row.id)),
            })

    return await _with_errors(operation)


async def _update_handler(
    initiative_id: str = "",
    expected_revision: int = 0,
    patch: dict | None = None,
    **kwargs,
):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
            current, subscription = await _current_initiative(
                db,
                context=context,
                user=user,
                initiative_id=initiative_id,
                manage=True,
            )
            validated_patch = AgentInitiativePatch.model_validate(patch or {})
            if validated_patch.delivery_policy is not None:
                proposed = validated_patch.delivery_policy
                expected_kind = (
                    "workspace_chat" if subscription.workspace_id else "conversation"
                )
                if (
                    proposed.surface.kind != expected_kind
                    or proposed.surface.conversation_id != context.conversation_id
                ):
                    raise AgentInitiativeServiceError(
                        "initiative_conversation_invalid",
                        "An Initiative managed in Chat must keep this exact delivery conversation",
                        status_code=409,
                    )
            if current.revision != expected_revision:
                raise StaleRevisionError(
                    target_kind="agent_initiative",
                    target_id=current.id,
                    expected=expected_revision,
                    actual=current.revision,
                )
            row = await update_initiative(
                db,
                initiative_id=initiative_id,
                entity_id=user.entity_id,
                user_id=user.id,
                user_role=user.role,
                expected_revision=expected_revision,
                patch=validated_patch.model_dump(exclude_unset=True),
            )
            await db.commit()
            return _json({"ok": True, "updated": True, "initiative": _initiative_receipt(row)})

    return await _with_errors(operation)


def _transition_handler(action: str):
    async def handler(initiative_id: str = "", expected_revision: int = 0, **kwargs):
        context = runtime_tool_call_context_from_kwargs(kwargs)

        async def operation():
            async with async_session() as db:
                user = await _actor(db, context)
                await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
                await _current_initiative(
                    db,
                    context=context,
                    user=user,
                    initiative_id=initiative_id,
                    manage=True,
                )
                row = await transition_initiative(
                    db,
                    initiative_id=initiative_id,
                    entity_id=user.entity_id,
                    user_id=user.id,
                    user_role=user.role,
                    expected_revision=expected_revision,
                    action=action,
                )
                await db.commit()
                return _json({"ok": True, "action": action, "initiative": _initiative_receipt(row)})

        return await _with_errors(operation)

    return handler


async def _delete_handler(initiative_id: str = "", expected_revision: int = 0, **kwargs):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
            await _current_initiative(
                db,
                context=context,
                user=user,
                initiative_id=initiative_id,
                manage=True,
            )
            await delete_initiative(
                db,
                initiative_id=initiative_id,
                entity_id=user.entity_id,
                user_id=user.id,
                user_role=user.role,
                expected_revision=expected_revision,
            )
            await db.commit()
            return _json({"ok": True, "deleted": True, "initiative_id": initiative_id})

    return await _with_errors(operation)


async def _run_handler(
    initiative_id: str = "",
    idempotency_key: str = "",
    confirm_cost: bool = False,
    **kwargs,
):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            row, _ = await _current_initiative(
                db,
                context=context,
                user=user,
                initiative_id=initiative_id,
                manage=True,
            )
            paused_confirmed = row.status == "paused" and confirm_cost
            if row.status not in {"active", "muted"} and not paused_confirmed:
                raise AgentInitiativeServiceError(
                    "initiative_paused",
                    "Resume the Initiative or explicitly confirm a one-off paid run",
                    status_code=409,
                )
            job = await initiative_driver(db, row.id)
            if job is None or (not job.enabled and not paused_confirmed):
                raise AgentInitiativeServiceError(
                    "initiative_driver_unavailable", "Initiative driver is unavailable", status_code=409,
                )
            key = idempotency_key or f"chat-{context.runtime_tool_call_id or context.runtime_run_id or int(datetime.now(timezone.utc).timestamp())}"
            from packages.core.tasks.scheduler_tasks import (
                dispatch_agent_initiative_now,
            )

            dispatch = await dispatch_agent_initiative_now(
                job_db_id=job.id,
                initiative_id=row.id,
                occurrence_key=f"manual:{key}",
                trigger_detail=(
                    "owner_confirmed_paused_chat_tool"
                    if paused_confirmed
                    else "chat_tool"
                ),
            )
            if dispatch is None:
                raise AgentInitiativeServiceError(
                    "initiative_driver_unavailable",
                    "Initiative driver is unavailable",
                    status_code=409,
                )
            return _json({
                "ok": True,
                "queued": True,
                "published": dispatch.published,
                "run_id": dispatch.run_id,
                "initiative_id": row.id,
                "idempotency_key": key,
            })

    return await _with_errors(operation)


async def _runs_handler(initiative_id: str = "", limit: int = 10, **kwargs):
    context = runtime_tool_call_context_from_kwargs(kwargs)

    async def operation():
        async with async_session() as db:
            user = await _actor(db, context)
            await _current_initiative(
                db,
                context=context,
                user=user,
                initiative_id=initiative_id,
            )
            rows = await list_initiative_runs(
                db,
                initiative_id=initiative_id,
                entity_id=user.entity_id,
                user_id=user.id,
                user_role=user.role,
                limit=max(1, min(int(limit), 50)),
            )
            return _json({
                "ok": True,
                "runs": [
                    {
                        "id": row.id,
                        "status": row.status,
                        "decision": row.final_decision,
                        "reason_code": row.reason_code,
                        "reason": row.reason,
                        "started_at": row.started_at,
                        "completed_at": row.completed_at,
                    }
                    for row in rows
                ],
            })

    return await _with_errors(operation)


def _id_revision_schema(description: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "initiative_id": {"type": "string", "description": "Exact Initiative ID"},
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["initiative_id", "expected_revision"],
        "additionalProperties": False,
        "description": description,
    }


def _trigger_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": ["1"]},
            "kind": {"type": "string", "enum": ["cron", "interval", "at"]},
            "cron": {"type": ["string", "null"], "maxLength": 100},
            "every_seconds": {
                "type": ["integer", "null"],
                "minimum": 60,
                "maximum": 31536000,
            },
            "run_at": {"type": ["string", "null"], "format": "date-time"},
            "timezone": {"type": "string", "minLength": 1, "maxLength": 50},
        },
        "required": ["kind"],
        "additionalProperties": False,
    }


def _condition_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": ["1"]},
            "semantic_condition": {
                "type": ["string", "null"],
                "maxLength": 4000,
            },
        },
        "additionalProperties": False,
    }


def _decision_policy_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": ["1"]},
            "mode": {
                "type": "string",
                "enum": ["always", "on_condition", "on_change"],
            },
            "cooldown_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 31536000,
            },
            "max_notifications_per_day": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
            },
        },
        "additionalProperties": False,
    }


def _delivery_policy_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": ["1"]},
            "surface": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["conversation", "workspace_chat"],
                    },
                    "conversation_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                },
                "required": ["kind", "conversation_id"],
                "additionalProperties": False,
            },
            "notification": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "recipient": {"type": "string", "enum": ["owner"]},
                    "channels": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "inapp",
                                "email",
                                "telegram",
                                "wechat",
                                "slack",
                                "discord",
                                "whatsapp",
                            ],
                        },
                        "minItems": 1,
                        "maxItems": 7,
                        "uniqueItems": True,
                    },
                    "content_mode": {
                        "type": "string",
                        "enum": ["full", "summary_and_link"],
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["surface"],
        "additionalProperties": False,
    }


def _interruption_policy_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": ["1"]},
            "quiet_hours": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["inherit_user", "custom", "none"],
                    },
                    "start": {
                        "type": ["string", "null"],
                        "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    },
                    "end": {
                        "type": ["string", "null"],
                        "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    },
                    "timezone": {
                        "type": ["string", "null"],
                        "maxLength": 50,
                    },
                },
                "additionalProperties": False,
            },
            "during_quiet_hours": {
                "type": "string",
                "enum": ["suppress_external"],
            },
        },
        "additionalProperties": False,
    }


def get_tools():
    subscription_id_description = (
        "Omit when managing the Agent deployment already bound to this Chat. "
        "Pass only an exact AgentSubscription ID returned by the system when "
        "this Chat has multiple valid deployments; never use placeholders such "
        "as 'current' or 'default'."
    )
    create_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 255},
            "instruction": {"type": "string", "minLength": 1, "maxLength": 20000},
            "schedule_kind": {"type": "string", "enum": ["cron", "interval", "at"]},
            "cron": {"type": "string"},
            "every_seconds": {"type": "integer", "minimum": 60},
            "run_at": {"type": "string", "format": "date-time"},
            "timezone": {"type": "string"},
            "condition": {"type": "string", "maxLength": 4000},
            "decision_mode": {"type": "string", "enum": ["always", "on_condition", "on_change"]},
            "cooldown_seconds": {"type": "integer", "minimum": 0},
            "notification_enabled": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Optional secondary notification fan-out in addition to the "
                    "exact Chat delivery, which always happens. Keep false unless "
                    "the user explicitly asks for a separate notification. Set "
                    "false for Chat-only delivery or when the user says no in-app "
                    "notification, email, or other channel."
                ),
            },
            "notification_channels": {
                "type": "array",
                "items": {"type": "string", "enum": ["inapp", "email", "telegram", "wechat", "slack", "discord", "whatsapp"]},
                "minItems": 1,
                "maxItems": 7,
                "uniqueItems": True,
                "description": (
                    "Secondary notification routes only; these do not choose the "
                    "Chat destination. Omit when notification_enabled is false."
                ),
            },
            "quiet_hours_mode": {"type": "string", "enum": ["inherit_user", "custom", "none"]},
            "quiet_hours_start": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
            "quiet_hours_end": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
            "quiet_hours_timezone": {"type": "string"},
            "subscription_id": {
                "type": "string",
                "description": subscription_id_description,
            },
        },
        "required": ["name", "instruction", "schedule_kind"],
        "additionalProperties": False,
    }
    tools = [
        ({"type": "function", "function": {
            "name": "create_agent_initiative",
            "description": (
                "Create a proactive rule in a private Agent or Manor Chat and deliver updates "
                "back to this exact Chat. This is the route for private scheduled "
                "or conditional requests that ask to tell, notify, remind, or update "
                "the user here, including 'tell me every day/hour', 'if X happens "
                "let me know', '每天搜新闻发给我', and '如果达到条件告诉我'. "
                "Workspace requests must use a Workspace automation instead. Exact Chat "
                "delivery is always enabled; notification_enabled controls only an "
                "additional in-app/email/external notification and must remain false "
                "unless the user explicitly requests that extra fan-out."
            ),
            "parameters": create_schema,
        }}, _create_handler),
        ({"type": "function", "function": {
            "name": "list_agent_initiatives",
            "description": (
                "List proactive rules for the current Agent deployment with the exact "
                "ID and revision needed for follow-up actions. Use get_agent_initiative "
                "for one rule's full configuration and recent state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "description": subscription_id_description,
                    }
                },
                "additionalProperties": False,
            },
        }}, _list_handler),
        ({"type": "function", "function": {
            "name": "get_agent_initiative",
            "description": "Get one proactive Agent rule by exact ID.",
            "parameters": {"type": "object", "properties": {"initiative_id": {"type": "string"}}, "required": ["initiative_id"], "additionalProperties": False},
        }}, _get_handler),
        ({"type": "function", "function": {
            "name": "update_agent_initiative",
            "description": "Update an existing proactive rule. Reuse its ID; never create a duplicate to change time or condition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "initiative_id": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "patch": {"type": "object", "additionalProperties": False, "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 255},
                        "instruction": {"type": "string", "minLength": 1, "maxLength": 20000},
                        "trigger": _trigger_schema(),
                        "condition": _condition_schema(),
                        "decision_policy": _decision_policy_schema(),
                        "delivery_policy": _delivery_policy_schema(),
                        "interruption_policy": _interruption_policy_schema(),
                    }, "minProperties": 1},
                },
                "required": ["initiative_id", "expected_revision", "patch"],
                "additionalProperties": False,
            },
        }}, _update_handler),
        ({"type": "function", "function": {"name": "mute_agent_initiative", "description": "Keep checking but stop all user delivery.", "parameters": _id_revision_schema("Mute this rule.")}}, _transition_handler("mute")),
        ({"type": "function", "function": {"name": "unmute_agent_initiative", "description": "Resume delivery for a muted rule.", "parameters": _id_revision_schema("Unmute this rule.")}}, _transition_handler("unmute")),
        ({"type": "function", "function": {"name": "pause_agent_initiative", "description": "Stop checks and model/tool cost until resumed.", "parameters": _id_revision_schema("Pause this rule.")}}, _transition_handler("pause")),
        ({"type": "function", "function": {"name": "resume_agent_initiative", "description": "Resume checks for a paused rule.", "parameters": _id_revision_schema("Resume this rule.")}}, _transition_handler("resume")),
        ({"type": "function", "function": {"name": "delete_agent_initiative", "description": "Delete a proactive rule.", "parameters": _id_revision_schema("Delete this rule.")}}, _delete_handler),
        ({"type": "function", "function": {
            "name": "run_agent_initiative_now",
            "description": "Run a proactive rule now. A paused rule requires confirm_cost=true and remains paused afterward.",
            "parameters": {"type": "object", "properties": {"initiative_id": {"type": "string"}, "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 120, "pattern": "^[A-Za-z0-9._:-]+$"}, "confirm_cost": {"type": "boolean", "description": "Required as true for a one-off run while paused; this confirms model/tool cost."}}, "required": ["initiative_id"], "additionalProperties": False},
        }}, _run_handler),
        ({"type": "function", "function": {
            "name": "list_agent_initiative_runs",
            "description": "List recent check decisions, including successful silent checks.",
            "parameters": {"type": "object", "properties": {"initiative_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["initiative_id"], "additionalProperties": False},
        }}, _runs_handler),
    ]
    return tools


__all__ = ["get_tools"]
