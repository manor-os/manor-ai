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

WORKSPACE_CREATION_AUTHORIZATION_TIMEOUT_SECONDS = float(
    os.getenv("WORKSPACE_CREATION_AUTHORIZATION_TIMEOUT_SECONDS", "8")
)
WORKSPACE_CREATION_AUTHORIZATION_MAX_TOKENS = 128
WORKSPACE_CREATION_AUTHORIZATION_CONFIDENCE_THRESHOLD = 0.92
_MAX_MESSAGE_CHARS = 12_000
_MAX_CONTEXT_CHARS = 12_000


class WorkspaceCreationAuthorizationStatus(str, Enum):
    NOT_AUTHORIZED = "not_authorized"
    AUTHORIZED = "authorized"


@dataclass(frozen=True)
class WorkspaceCreationAuthorizationDecision:
    status: WorkspaceCreationAuthorizationStatus
    confidence: float

    @classmethod
    def not_authorized(
        cls,
        confidence: float = 0.0,
    ) -> "WorkspaceCreationAuthorizationDecision":
        return cls(
            status=WorkspaceCreationAuthorizationStatus.NOT_AUTHORIZED,
            confidence=max(0.0, min(float(confidence), 1.0)),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "WorkspaceCreationAuthorizationDecision":
        if not isinstance(payload, dict) or set(payload) != {"status", "confidence"}:
            raise ValueError("Workspace creation authorization has an invalid shape")
        raw_confidence = payload["confidence"]
        if isinstance(raw_confidence, bool):
            raise ValueError("Workspace creation authorization confidence must be numeric")
        confidence = float(raw_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                "Workspace creation authorization confidence must be between 0 and 1"
            )
        status = WorkspaceCreationAuthorizationStatus(str(payload["status"]).strip())
        if (
            status is WorkspaceCreationAuthorizationStatus.AUTHORIZED
            and confidence < WORKSPACE_CREATION_AUTHORIZATION_CONFIDENCE_THRESHOLD
        ):
            return cls.not_authorized(confidence)
        return cls(status=status, confidence=confidence)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "confidence": self.confidence,
        }


def _authorization_messages(
    *,
    message_text: str,
    recent_context_text: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are WorkspaceCreationAuthorizationClassifier. Decide only whether "
                "the latest user message explicitly authorizes starting and saving a new "
                "Workspace draft now. Return JSON only with exactly this shape: "
                '{"status":"authorized|not_authorized","confidence":0.0}. '
                "Choose authorized when the latest message directly asks to create, start, "
                "set up, or configure a new Workspace, or clearly confirms an immediately "
                "preceding explicit question asking permission to create one. Requests to "
                "explain, recommend, design, preview, or discuss a Workspace are not "
                "authorization. Describing work that might suit a Workspace, including "
                "phrases such as 'manage my properties', is not authorization. A vague yes "
                "without a clear preceding creation question is not authorization. This "
                "decision is only permission to save a draft; it never authorizes creation "
                "of the final Workspace. When uncertain, choose not_authorized."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "latest_user_message": str(message_text or "")[:_MAX_MESSAGE_CHARS],
                    "recent_conversation": str(recent_context_text or "")[
                        -_MAX_CONTEXT_CHARS:
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]


async def classify_workspace_creation_authorization(
    *,
    message_text: str,
    recent_context_text: str = "",
    entity_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> WorkspaceCreationAuthorizationDecision:
    """Classify permission to persist a new Workspace draft, without writing it."""

    try:
        result = await asyncio.wait_for(
            runtime_execute_text_completion(
                _authorization_messages(
                    message_text=message_text,
                    recent_context_text=recent_context_text,
                ),
                entity_id=entity_id,
                user_id=user_id,
                conversation_id=conversation_id,
                source=RUNTIME_INTERNAL_WORKER_SOURCE,
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=WORKSPACE_CREATION_AUTHORIZATION_MAX_TOKENS,
                reasoning_effort="none",
            ),
            timeout=WORKSPACE_CREATION_AUTHORIZATION_TIMEOUT_SECONDS,
        )
        payload = json.loads(str(result.content or ""))
        return WorkspaceCreationAuthorizationDecision.from_payload(payload)
    except Exception:
        logger.info(
            "Workspace creation authorization fell back to not_authorized",
            exc_info=True,
        )
        return WorkspaceCreationAuthorizationDecision.not_authorized()
