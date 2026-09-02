from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import logging

from packages.core.ai.runtime.completions import runtime_execute_text_completion
from packages.core.ai.runtime.sources import RUNTIME_INTERNAL_WORKER_SOURCE


logger = logging.getLogger(__name__)

AUTO_ROUTE_TIMEOUT_SECONDS = 5.0
AUTO_ROUTE_MAX_TOKENS = 256
DIRECT_CHAT_CONFIDENCE_THRESHOLD = 0.90
_MAX_MESSAGE_CHARS = 12_000
_MAX_CONTEXT_CHARS = 12_000


class AutoRouteMode(str, Enum):
    """The only two decisions made by the Auto Mode classifier."""

    DIRECT_CHAT = "direct_chat"
    GENERAL_CHAT = "general_chat"


@dataclass(frozen=True)
class AutoRouteDecision:
    """Normalized, immutable result from the worker classifier."""

    mode: AutoRouteMode
    confidence: float


def _general_fallback(confidence: float = 0.0) -> AutoRouteDecision:
    return AutoRouteDecision(
        mode=AutoRouteMode.GENERAL_CHAT,
        confidence=max(0.0, min(float(confidence), 1.0)),
    )


def _parse_decision(raw: str) -> AutoRouteDecision:
    parsed = json.loads(str(raw or ""))
    if not isinstance(parsed, dict) or set(parsed) != {"mode", "confidence"}:
        raise ValueError("Auto route response must contain only mode and confidence")

    raw_confidence = parsed["confidence"]
    if isinstance(raw_confidence, bool):
        raise ValueError("Auto route confidence must be numeric")
    confidence = float(raw_confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Auto route confidence must be between 0 and 1")

    mode = AutoRouteMode(str(parsed["mode"]).strip())
    if (
        mode is AutoRouteMode.DIRECT_CHAT
        and confidence < DIRECT_CHAT_CONFIDENCE_THRESHOLD
    ):
        return _general_fallback(confidence)
    return AutoRouteDecision(mode=mode, confidence=confidence)


def _classifier_messages(
    *,
    message_text: str,
    recent_context_text: str,
    surface: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are AutoRouteClassifier. Make exactly one binary routing "
                "decision; do not identify a task type, content type, skill, or "
                "tool. Return JSON only with exactly this shape: "
                '{"mode":"direct_chat|general_chat","confidence":0.0}. '
                "Choose direct_chat only when the assistant can safely complete "
                "the request as a self-contained final text response without "
                "tools, current or external information, workspace or file "
                "retrieval, attachments, actions, artifact creation/editing/export, "
                "or continuation of unfinished work. Short greetings, explanations, "
                "translations, and small inline rewrites may be direct_chat. Choose "
                "general_chat for any ambiguity or possible need for tools, current "
                "information, product/workspace state, external action, file output, "
                "or a substantial deliverable such as an agreement, contract, "
                "report, document, spreadsheet, presentation, website, image, audio, "
                "or video, even when no file format is named. Use recent conversation "
                "context to recognize follow-ups and unfinished deliverables. When "
                "uncertain, choose general_chat."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "surface": str(surface or ""),
                    "latest_user_message": str(message_text or "")[:_MAX_MESSAGE_CHARS],
                    "recent_conversation": str(recent_context_text or "")[
                        -_MAX_CONTEXT_CHARS:
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]


async def classify_auto_route(
    *,
    message_text: str,
    recent_context_text: str = "",
    surface: str,
    entity_id: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
) -> AutoRouteDecision:
    """Classify an Auto Mode turn with the tenant's inexpensive worker model.

    Any timeout, provider error, malformed JSON, or low-confidence direct
    decision degrades to the tool-capable general route.
    """

    try:
        result = await asyncio.wait_for(
            runtime_execute_text_completion(
                _classifier_messages(
                    message_text=message_text,
                    recent_context_text=recent_context_text,
                    surface=surface,
                ),
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                source=RUNTIME_INTERNAL_WORKER_SOURCE,
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=AUTO_ROUTE_MAX_TOKENS,
                reasoning_effort="none",
            ),
            timeout=AUTO_ROUTE_TIMEOUT_SECONDS,
        )
        return _parse_decision(result.content)
    except Exception:
        logger.info(
            "Auto Mode classifier fell back to general_chat",
            exc_info=True,
        )
        return _general_fallback()


def auto_route_metadata(
    decision: AutoRouteDecision,
) -> dict[str, str | float]:
    return {
        "mode": decision.mode.value,
        "confidence": decision.confidence,
    }
