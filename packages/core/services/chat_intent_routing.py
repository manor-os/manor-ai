from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.auto_route_classifier import (
    AutoRouteDecision,
    AutoRouteMode,
    classify_auto_route,
)
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
from packages.core.models.task import Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_access import (
    user_readable_workspace_ids,
    user_writable_workspace_ids,
)


_RECENT_CONTEXT_ROWS = 16
_MAX_WORKSPACE_CANDIDATES = 24
WORKSPACE_RECOMMENDATIONS_ENABLED_PREFERENCE = "workspace_recommendations_enabled"
_VOICE_SOCIAL_UTTERANCE = re.compile(
    r"(?:"
    r"(?:hi|hello|hey|thanks?|thank\s+you|good\s+(?:morning|afternoon|evening)|"
    r"bye|goodbye|are\s+you\s+there|can\s+you\s+hear\s+me)|"
    r"(?:你好|您好|嗨|哈喽|谢谢|感谢|早上好|下午好|晚上好|再见|你在吗|"
    r"听得到吗|能听到吗)"
    r")[\s.!！?？,，。]*",
    re.IGNORECASE,
)


def workspace_recommendations_enabled(user: User) -> bool:
    preferences = getattr(user, "preferences", None)
    if not isinstance(preferences, dict):
        return True
    return preferences.get(WORKSPACE_RECOMMENDATIONS_ENABLED_PREFERENCE) is not False


@dataclass(frozen=True)
class ChatIntentRoutingDecision:
    auto_route: AutoRouteDecision
    general_intent: GeneralChatIntentDecision
    workspace_creation_authorization: WorkspaceCreationAuthorizationDecision
    execution_plan: TurnExecutionPlan

    def runtime_metadata(self, *, request: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "auto_route": {
                "mode": self.auto_route.mode.value,
                "confidence": self.auto_route.confidence,
            },
            "general_chat_intent": {
                "kind": self.general_intent.kind.value,
                "confidence": self.general_intent.confidence,
            },
            "workspace_creation_authorization": (
                self.workspace_creation_authorization.to_metadata()
            ),
            "turn_execution_plan": self.execution_plan.to_metadata(),
        }
        recommendation = self.general_intent.recommendation
        if (
            self.workspace_creation_authorization.status
            is WorkspaceCreationAuthorizationStatus.AUTHORIZED
        ):
            metadata["general_intent_prompt"] = (
                "The user explicitly authorized starting a new Workspace draft. Start and "
                "save a guided Workspace draft in the current Chat, then keep configuration "
                "changes in that draft. Do not create the final Workspace until the user uses "
                "the explicit Create Workspace action."
            )
        elif recommendation is not None:
            metadata["workspace_recommendation"] = recommendation.to_metadata(
                request=request,
            )
            metadata["general_intent_prompt"] = (
                "Answer the user's request normally and helpfully as a standalone "
                "response. You may explain why a Workspace fits the work, but never "
                "mention or allude to internal classification, recommendation delivery, "
                "routing, handoffs, interface controls, cards, buttons, navigation, or "
                "hidden system behavior. Do not predict or announce that any interface "
                "element will appear. Do not tell the user to click, choose, accept, or "
                "wait for another action. Do not start or modify a Workspace, create a "
                "Workspace draft, navigate, or write into a Workspace in this turn."
            )
        else:
            metadata["general_intent_prompt"] = (
                "Answer the user's request normally. Do not start a new Workspace or a new "
                "Workspace draft in this turn because the user has not explicitly asked to "
                "create one. An existing active Workspace draft may still be updated when the "
                "user asks for a configuration change."
            )
        return metadata


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
    runtime_metadata: dict[str, Any] | None = None,
) -> ChatIntentRoutingDecision:
    """Classify one ordinary Auto Chat turn without creating or mutating a Workspace."""

    metadata = dict(runtime_metadata or {})
    if (
        metadata.get("voice_session_mode") == "chat_gateway"
        and _VOICE_SOCIAL_UTTERANCE.fullmatch(str(message or "").strip())
    ):
        local_plan = build_turn_execution_plan(
            enabled=True,
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            message=message,
            message_text=message,
            disable_tools=True,
            runtime_metadata=metadata,
        )
        # Pure call-control and social utterances do not need three remote
        # routing classifiers before the actual reply. Anything that could be
        # a contextual follow-up or action stays on the governed route below.
        return ChatIntentRoutingDecision(
            auto_route=AutoRouteDecision(
                mode=AutoRouteMode.DIRECT_CHAT,
                confidence=1.0,
            ),
            general_intent=GeneralChatIntentDecision.agent_chat(1.0),
            workspace_creation_authorization=(
                WorkspaceCreationAuthorizationDecision.not_authorized(1.0)
            ),
            execution_plan=local_plan,
        )

    recent_context, _active_workspace_draft = await _recent_chat_context(
        db,
        conversation_id,
    )
    recommendations_enabled = workspace_recommendations_enabled(user)
    candidates = (
        await list_workspace_intent_candidates(db, user=user)
        if recommendations_enabled
        else []
    )
    auto_route_call = classify_auto_route(
        message_text=message,
        recent_context_text=recent_context,
        surface=ChatSurface.GLOBAL_OWNER_CHAT.value,
        entity_id=user.entity_id,
        user_id=user.id,
        conversation_id=conversation_id,
    )
    creation_authorization_call = classify_workspace_creation_authorization(
        message_text=message,
        recent_context_text=recent_context,
        entity_id=user.entity_id,
        user_id=user.id,
        conversation_id=conversation_id,
    )
    if recommendations_enabled:
        general_intent_call = classify_general_chat_intent(
            message_text=message,
            recent_context_text=recent_context,
            candidates=candidates,
            entity_id=user.entity_id,
            user_id=user.id,
            conversation_id=conversation_id,
        )
        auto_route, creation_authorization, general_intent = await asyncio.gather(
            auto_route_call,
            creation_authorization_call,
            general_intent_call,
        )
    else:
        auto_route, creation_authorization = await asyncio.gather(
            auto_route_call,
            creation_authorization_call,
        )
        general_intent = GeneralChatIntentDecision.agent_chat(1.0)
    if (
        creation_authorization.status
        is WorkspaceCreationAuthorizationStatus.AUTHORIZED
    ):
        auto_route = AutoRouteDecision(
            mode=AutoRouteMode.GENERAL_CHAT,
            confidence=1.0,
        )
        general_intent = GeneralChatIntentDecision.agent_chat(1.0)

    plan = build_turn_execution_plan(
        enabled=True,
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        message=message,
        message_text=message,
        runtime_metadata=metadata,
        auto_route_decision=auto_route,
    )
    return ChatIntentRoutingDecision(
        auto_route=auto_route,
        general_intent=general_intent,
        workspace_creation_authorization=creation_authorization,
        execution_plan=plan,
    )
