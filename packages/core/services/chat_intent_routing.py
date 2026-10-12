from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.general_chat_intent import (
    GeneralChatIntentDecision,
    WorkspaceIntentCandidate,
    classify_general_chat_intent,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.turn_policy import (
    TurnExecutionPlan,
    build_turn_execution_plan,
)
from packages.core.ai.runtime.workspace_creation_authorization import (
    WorkspaceCreationAuthorizationDecision,
    WorkspaceCreationAuthorizationStatus,
    classify_workspace_creation_authorization,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_access import (
    user_readable_workspace_ids,
    user_writable_workspace_ids,
)


logger = logging.getLogger(__name__)

_RECENT_CONTEXT_ROWS = 16
_DEFERRED_INTENT_CACHE_PREFIX = "chat_intent:"
_DEFERRED_INTENT_TTL_SECONDS = 3_600
_DEFERRED_INTENT_POLL_SECONDS = 0.2
_DEFERRED_INTENT_TASKS: set[asyncio.Task] = set()

# The model always sees the same neutral guidance; whether a draft may start is
# decided when it actually calls the draft tool (see workspace_drafts.py).
_NEUTRAL_GENERAL_INTENT_PROMPT = (
    "Answer the user's request normally and helpfully as a standalone response. "
    "Start a new Workspace draft only when the user explicitly asks to create a "
    "new Workspace; then save a guided Workspace draft in the current Chat, keep "
    "configuration changes in that draft, and do not create the final Workspace "
    "until the user uses the explicit Create Workspace action. An existing active "
    "Workspace draft may still be updated when the user asks for a configuration "
    "change. You may explain why a Workspace would fit the work, but never mention "
    "or allude to internal classification, recommendation delivery, routing, "
    "handoffs, cards, or hidden system behavior, and do not predict or announce "
    "that any interface element will appear."
)
_MAX_WORKSPACE_CANDIDATES = 24
WORKSPACE_RECOMMENDATIONS_ENABLED_PREFERENCE = "workspace_recommendations_enabled"
def workspace_recommendations_enabled(user: User) -> bool:
    preferences = getattr(user, "preferences", None)
    if not isinstance(preferences, dict):
        return True
    return preferences.get(WORKSPACE_RECOMMENDATIONS_ENABLED_PREFERENCE) is not False


@dataclass(frozen=True)
class DeferredChatIntentRequest:
    """Inputs for the classifiers that run beside the turn, not before it."""

    key: str
    message: str
    recent_context: str
    entity_id: str
    user_id: str
    conversation_id: str | None
    recommendations_enabled: bool


@dataclass(frozen=True)
class DeferredChatIntent:
    workspace_creation_authorization: WorkspaceCreationAuthorizationDecision
    general_intent: GeneralChatIntentDecision

    def to_metadata(self, *, request: str) -> dict[str, Any]:
        recommendation = self.general_intent.recommendation
        return {
            "workspace_creation_authorization": (
                self.workspace_creation_authorization.to_metadata()
            ),
            "general_chat_intent": {
                "kind": self.general_intent.kind.value,
                "confidence": self.general_intent.confidence,
            },
            "workspace_recommendation": (
                recommendation.to_metadata(request=request)
                if recommendation is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ChatIntentRoutingDecision:
    execution_plan: TurnExecutionPlan
    deferred: DeferredChatIntentRequest

    def runtime_metadata(self, *, request: str) -> dict[str, Any]:
        return {
            "turn_execution_plan": self.execution_plan.to_metadata(),
            "workspace_creation_authorization": {
                "status": WorkspaceCreationAuthorizationStatus.DEFERRED.value,
                "key": self.deferred.key,
            },
            "deferred_chat_intent_key": self.deferred.key,
            "general_intent_prompt": _NEUTRAL_GENERAL_INTENT_PROMPT,
        }


def auto_chat_intent_routing_allowed(
    *,
    surface: ChatSurface,
    chat_mode: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    manual_skill_selected: bool,
    editor_context: dict[str, Any] | None,
    ephemeral: bool,
    disable_tools: bool,
    blocked_tools: bool,
    approval_turn: bool,
    has_forced_tool_calls: bool,
    has_attachments: bool,
) -> bool:
    """Return whether the ordinary Auto Chat classifier owns this turn."""

    return bool(
        surface is ChatSurface.GLOBAL_OWNER_CHAT
        and str(chat_mode or "auto").strip().lower() == "auto"
        and not agent_id
        and not workspace_id
        and not manual_skill_selected
        and not editor_context
        and not ephemeral
        and not disable_tools
        and not blocked_tools
        and not approval_turn
        and not has_forced_tool_calls
        and not has_attachments
    )


def _workspace_draft_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return set()
        return _workspace_draft_ids(parsed)
    if isinstance(value, list):
        return set().union(*(_workspace_draft_ids(item) for item in value))
    if not isinstance(value, dict):
        return set()
    if value.get("artifact_kind") == "workspace_draft" and value.get("draft_id"):
        return {str(value["draft_id"])}
    return set().union(*(_workspace_draft_ids(item) for item in value.values()))


async def _recent_chat_context(
    db: AsyncSession,
    conversation_id: str | None,
) -> tuple[str, bool]:
    if not conversation_id:
        return "", False
    rows = list((await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(_RECENT_CONTEXT_ROWS)
    )).scalars().all())
    rows.reverse()
    context_lines: list[str] = []
    draft_ids: set[str] = set()
    for row in rows:
        content = str(row.content or "").strip()
        if content:
            context_lines.append(f"{row.role}: {content}")
        draft_ids.update(_workspace_draft_ids(row.tool_calls))
    active_draft = False
    if draft_ids:
        active_draft = bool((await db.execute(
            select(WorkspaceDraft.id)
            .where(
                WorkspaceDraft.id.in_(draft_ids),
                WorkspaceDraft.status.in_(("active", "ready")),
            )
            .limit(1)
        )).scalar_one_or_none())
    context = "\n".join(context_lines)
    if active_draft:
        context = (
            f"{context}\n"
            "[An active Workspace Draft exists in this conversation. Treat it as context "
            "only: determine whether the latest user request continues that Draft or is an "
            "independent request that may need a separate Workspace.]"
        )
    return context, active_draft


def _workspace_summary(workspace: Workspace) -> str:
    operating_model = (
        workspace.operating_model
        if isinstance(workspace.operating_model, dict)
        else {}
    )
    services = operating_model.get("services")
    service_names = [
        str(item.get("name") or item.get("service_key") or "").strip()
        for item in services
        if isinstance(item, dict)
    ] if isinstance(services, list) else []
    parts = [
        str(workspace.description or "").strip(),
        str(workspace.operating_context or "").strip(),
        str(workspace.primary_work or "").strip(),
        ", ".join(name for name in service_names if name),
    ]
    return " | ".join(part for part in parts if part)[:1_200]


async def list_workspace_intent_candidates(
    db: AsyncSession,
    *,
    user: User,
) -> list[WorkspaceIntentCandidate]:
    readable_ids = await user_readable_workspace_ids(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not readable_ids:
        return []
    rows = list((await db.execute(
        select(Workspace)
        .where(
            Workspace.id.in_(readable_ids),
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
            Workspace.status == "active",
        )
        .order_by(Workspace.updated_at.desc(), Workspace.created_at.desc())
        .limit(_MAX_WORKSPACE_CANDIDATES)
    )).scalars().all())
    writable_ids = await user_writable_workspace_ids(
        db,
        entity_id=user.entity_id,
        workspace_ids={workspace.id for workspace in rows},
        user_id=user.id,
        role=user.role,
    )
    return [
        WorkspaceIntentCandidate(
            workspace_id=workspace.id,
            name=workspace.name,
            summary=_workspace_summary(workspace),
            can_add=workspace.id in writable_ids,
        )
        for workspace in rows
    ]


async def classify_chat_intent_routing(
    db: AsyncSession,
    *,
    user: User,
    message: str,
    conversation_id: str | None,
    agent_subscription_id: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> ChatIntentRoutingDecision:
    """Plan one ordinary Auto Chat turn without waiting on any model.

    Workspace-creation authorization and Workspace recommendation are model
    classifiers. Neither changes how the turn starts, so they run beside it
    (``start_deferred_chat_intent``) and are read only where they are used:
    the draft tool and the end of the reply.
    """

    metadata = dict(runtime_metadata or {})
    recent_context, _active_workspace_draft = await _recent_chat_context(
        db,
        conversation_id,
    )
    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        message=message,
        message_text=message,
        agent_subscription_id=agent_subscription_id,
        runtime_metadata=metadata,
    )
    return ChatIntentRoutingDecision(
        execution_plan=plan,
        deferred=DeferredChatIntentRequest(
            key=generate_ulid(),
            message=message,
            recent_context=recent_context,
            entity_id=user.entity_id,
            user_id=user.id,
            conversation_id=conversation_id,
            recommendations_enabled=workspace_recommendations_enabled(user),
        ),
    )


async def classify_deferred_chat_intent(
    request: DeferredChatIntentRequest,
    *,
    candidates: list[WorkspaceIntentCandidate],
) -> DeferredChatIntent:
    """Run the creation-authorization and recommendation classifiers."""

    creation_authorization_call = classify_workspace_creation_authorization(
        message_text=request.message,
        recent_context_text=request.recent_context,
        entity_id=request.entity_id,
        user_id=request.user_id,
        conversation_id=request.conversation_id,
    )
    if request.recommendations_enabled:
        creation_authorization, general_intent = await asyncio.gather(
            creation_authorization_call,
            classify_general_chat_intent(
                message_text=request.message,
                recent_context_text=request.recent_context,
                candidates=candidates,
                entity_id=request.entity_id,
                user_id=request.user_id,
                conversation_id=request.conversation_id,
            ),
        )
    else:
        creation_authorization = await creation_authorization_call
        general_intent = GeneralChatIntentDecision.agent_chat(1.0)
    if (
        creation_authorization.status
        is WorkspaceCreationAuthorizationStatus.AUTHORIZED
    ):
        # An explicit create request is answered by the draft, not a card.
        general_intent = GeneralChatIntentDecision.agent_chat(1.0)
    return DeferredChatIntent(
        workspace_creation_authorization=creation_authorization,
        general_intent=general_intent,
    )


def _deferred_intent_cache_key(key: str) -> str:
    return f"{_DEFERRED_INTENT_CACHE_PREFIX}{key}"


async def publish_deferred_chat_intent(request: DeferredChatIntentRequest) -> None:
    """Classify and publish the result for the turn's consumers (Redis)."""

    from packages.core.cache import cache
    from packages.core.database import async_session

    try:
        candidates: list[WorkspaceIntentCandidate] = []
        if request.recommendations_enabled:
            async with async_session() as db:
                user = await db.get(User, request.user_id)
                if user is not None and user.entity_id == request.entity_id:
                    candidates = await list_workspace_intent_candidates(db, user=user)
        result = await classify_deferred_chat_intent(request, candidates=candidates)
        payload = result.to_metadata(request=request.message)
    except Exception:
        logger.warning("Deferred chat intent classification failed key=%s", request.key, exc_info=True)
        payload = {
            "workspace_creation_authorization": (
                WorkspaceCreationAuthorizationDecision.not_authorized(0.0).to_metadata()
            ),
            "workspace_recommendation": None,
        }
    await cache.set(
        _deferred_intent_cache_key(request.key),
        payload,
        ttl=_DEFERRED_INTENT_TTL_SECONDS,
    )


def start_deferred_chat_intent(request: DeferredChatIntentRequest) -> None:
    """Run the deferred classifiers beside the turn in this process."""

    task = asyncio.get_running_loop().create_task(publish_deferred_chat_intent(request))
    _DEFERRED_INTENT_TASKS.add(task)
    task.add_done_callback(_DEFERRED_INTENT_TASKS.discard)


async def wait_for_deferred_chat_intent(
    key: str | None,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Return the published classification, waiting at most ``timeout``."""

    if not isinstance(key, str) or not key:
        return None
    from packages.core.cache import cache

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        value = await cache.get(_deferred_intent_cache_key(key))
        if isinstance(value, dict):
            return value
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_DEFERRED_INTENT_POLL_SECONDS)
