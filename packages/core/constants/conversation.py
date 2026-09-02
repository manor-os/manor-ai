"""Stable conversation surface identities shared across chat entrypoints."""
from __future__ import annotations

from enum import StrEnum


class ConversationSurfaceKind(StrEnum):
    ORDINARY_CHAT = "chat"
    DASHBOARD_MODULE = "dashboard_module"
    AI_EDIT = "ai_edit"


class AiEditTargetKind(StrEnum):
    """Stable resource domains supported by the shared AI Edit session runtime."""

    DOCUMENT = "document"
    DIAGRAM = "diagram"
    PROJECT = "project"
    WORKFLOW = "workflow"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


HIDDEN_CONVERSATION_SURFACES = frozenset({
    ConversationSurfaceKind.DASHBOARD_MODULE,
    ConversationSurfaceKind.AI_EDIT,
})
