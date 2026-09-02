from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import logging
import os
from typing import Any

from packages.core.ai.runtime.completions import runtime_execute_text_completion
from packages.core.ai.runtime.sources import RUNTIME_INTERNAL_WORKER_SOURCE


logger = logging.getLogger(__name__)

GENERAL_CHAT_INTENT_TIMEOUT_SECONDS = float(
    os.getenv("GENERAL_CHAT_INTENT_TIMEOUT_SECONDS", "12")
)
GENERAL_CHAT_INTENT_MAX_TOKENS = 512
WORKSPACE_RECOMMENDATION_CONFIDENCE_THRESHOLD = 0.82
_MAX_MESSAGE_CHARS = 12_000
_MAX_CONTEXT_CHARS = 12_000
_MAX_REASON_CHARS = 600


class GeneralChatIntentKind(str, Enum):
    AGENT_CHAT = "agent_chat"
    WORKSPACE_RECOMMENDATION = "workspace_recommendation"


class WorkspaceRecommendationAction(str, Enum):
    CREATE_NEW = "create_new"
    OPEN_EXISTING = "open_existing"
    ADD_TO_EXISTING = "add_to_existing"


@dataclass(frozen=True)
class WorkspaceIntentCandidate:
    workspace_id: str
    name: str
    summary: str
    can_add: bool

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "summary": self.summary,
            "can_add": self.can_add,
        }


@dataclass(frozen=True)
class WorkspaceRecommendation:
    action: WorkspaceRecommendationAction
    reason: str
    workspace_id: str | None = None
    workspace_name: str | None = None

    def to_metadata(self, *, request: str) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "request": str(request or "").strip()[:_MAX_MESSAGE_CHARS],
        }


@dataclass(frozen=True)
class GeneralChatIntentDecision:
    kind: GeneralChatIntentKind
    confidence: float
    recommendation: WorkspaceRecommendation | None = None

    @classmethod
    def agent_chat(cls, confidence: float = 0.0) -> "GeneralChatIntentDecision":
        return cls(
            kind=GeneralChatIntentKind.AGENT_CHAT,
            confidence=max(0.0, min(float(confidence), 1.0)),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        candidates: list[WorkspaceIntentCandidate],
    ) -> "GeneralChatIntentDecision":
        if not isinstance(payload, dict) or set(payload) != {
            "kind",
            "confidence",
            "reason",
            "action",
            "workspace_id",
        }:
            raise ValueError("General Chat intent response has an invalid shape")

        raw_confidence = payload["confidence"]
        if isinstance(raw_confidence, bool):
            raise ValueError("General Chat intent confidence must be numeric")
        confidence = float(raw_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("General Chat intent confidence must be between 0 and 1")

        kind = GeneralChatIntentKind(str(payload["kind"]).strip())
        if kind is GeneralChatIntentKind.AGENT_CHAT:
            if payload["action"] is not None or payload["workspace_id"] is not None:
                raise ValueError("agent_chat cannot include a Workspace action or id")
            if str(payload["reason"] or "").strip():
                raise ValueError("agent_chat cannot include a recommendation reason")
            return cls.agent_chat(confidence)
        if confidence < WORKSPACE_RECOMMENDATION_CONFIDENCE_THRESHOLD:
            return cls.agent_chat(confidence)

        reason = str(payload["reason"] or "").strip()[:_MAX_REASON_CHARS]
        if not reason:
            raise ValueError("Workspace recommendation reason is required")
        action = WorkspaceRecommendationAction(str(payload["action"] or "").strip())
        workspace_id = str(payload["workspace_id"] or "").strip() or None
        candidates_by_id = {candidate.workspace_id: candidate for candidate in candidates}

        if action is WorkspaceRecommendationAction.CREATE_NEW:
            if workspace_id is not None:
                raise ValueError("create_new cannot target an existing Workspace")
            recommendation = WorkspaceRecommendation(action=action, reason=reason)
        else:
            candidate = candidates_by_id.get(workspace_id or "")
            if candidate is None:
                raise ValueError("Workspace recommendation must use an exact candidate id")
            if action is WorkspaceRecommendationAction.ADD_TO_EXISTING and not candidate.can_add:
                raise ValueError("The selected Workspace is read-only for this user")
            recommendation = WorkspaceRecommendation(
                action=action,
                reason=reason,
                workspace_id=candidate.workspace_id,
                workspace_name=candidate.name,
            )

        return cls(
            kind=GeneralChatIntentKind.WORKSPACE_RECOMMENDATION,
            confidence=confidence,
            recommendation=recommendation,
        )


def _classifier_messages(
    *,
    message_text: str,
    recent_context_text: str,
    candidates: list[WorkspaceIntentCandidate],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are WorkspaceSuitabilityClassifier. Decide only which operating "
                "surface is the best fit for the work requested by the latest user query: "
                "ordinary Chat or a Workspace. Do not decide whether the user authorized "
                "Workspace creation, and do not initiate, approve, or imply any write. "
                "Return JSON only with exactly these keys: "
                '{"kind":"agent_chat|workspace_recommendation",'
                '"confidence":0.0,'
                '"reason":"","action":"create_new|open_existing|add_to_existing|null",'
                '"workspace_id":"exact candidate id or null"}. '
                "Ordinary Chat is a conversation-scoped assistant surface. It can preserve the "
                "conversation, use tools, and create or edit artifacts, so complexity, multiple "
                "steps, files, research, or a substantial one-time deliverable do not by themselves "
                "justify a Workspace. It is the better fit for questions, explanations, advice, "
                "brainstorming, research, analysis, troubleshooting, bounded execution, and single "
                "deliverables that can be completed in this conversation. "
                "A Workspace is a durable named operating boundary for sustained work across "
                "sessions or people. It can scope shared Knowledge and artifacts; define primary "
                "work, goals, measurements, and operating rules; deploy service Agents and tools; "
                "manage Proposals, Tasks, Plans, Workflows, approvals, and governance; run scheduled "
                "automations or recurring Strategist reviews; and retain outcomes as evidence for "
                "later work. Recommend a Workspace only when those persistent operating capabilities "
                "are material to the requested outcome, not merely compatible with it. Strong fits "
                "include an ongoing function, a recurring operating loop, a sustained initiative "
                "with accountable state and progress, or collaboration that needs a shared scoped "
                "system of record. A long or complex one-off request, a request mentioning a project "
                "or business, and a question about Workspaces should still stay in Chat unless the "
                "outcome actually depends on that durable operating boundary. "
                "Judge the latest query. Use recent conversation only to resolve references and "
                "continuations; do not let unrelated history turn a bounded query into a Workspace. "
                "When Workspace is clearly the best surface, use add_to_existing when the current "
                "request should be handed into a "
                "matching writable Workspace, open_existing when navigation or existing status "
                "is the useful next step, and create_new only when no listed Workspace is a "
                "good match. Never invent or partially match a workspace_id: copy one exact "
                "candidate id. Read-only candidates may only use open_existing. Write a short, "
                "specific, user-facing reason in the user's language. When uncertain, choose "
                "agent_chat. For agent_chat, action and workspace_id must be null and reason "
                "must be an empty string."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "latest_user_message": str(message_text or "")[:_MAX_MESSAGE_CHARS],
                    "recent_conversation": str(recent_context_text or "")[-_MAX_CONTEXT_CHARS:],
                    "workspace_candidates": [
                        candidate.to_prompt_dict() for candidate in candidates
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]


async def classify_general_chat_intent(
    *,
    message_text: str,
    recent_context_text: str = "",
    candidates: list[WorkspaceIntentCandidate] | None = None,
    entity_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> GeneralChatIntentDecision:
    """Classify whether Chat or a Workspace is the better operating surface."""

    workspace_candidates = list(candidates or [])
    try:
        result = await asyncio.wait_for(
            runtime_execute_text_completion(
                _classifier_messages(
                    message_text=message_text,
                    recent_context_text=recent_context_text,
                    candidates=workspace_candidates,
                ),
                entity_id=entity_id,
                user_id=user_id,
                conversation_id=conversation_id,
                source=RUNTIME_INTERNAL_WORKER_SOURCE,
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=GENERAL_CHAT_INTENT_MAX_TOKENS,
                reasoning_effort="none",
            ),
            timeout=GENERAL_CHAT_INTENT_TIMEOUT_SECONDS,
        )
        payload = json.loads(str(result.content or ""))
        return GeneralChatIntentDecision.from_payload(
            payload,
            candidates=workspace_candidates,
        )
    except Exception:
        logger.info(
            "General Chat intent classifier fell back to agent_chat",
            exc_info=True,
        )
        return GeneralChatIntentDecision.agent_chat()
