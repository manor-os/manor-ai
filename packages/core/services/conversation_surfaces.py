"""Conversation metadata contract for host-owned chat surfaces."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from packages.core.constants.conversation import (
    HIDDEN_CONVERSATION_SURFACES,
    AiEditTargetKind,
    ConversationSurfaceKind,
)


@dataclass(frozen=True)
class AiEditTargetIdentity:
    kind: AiEditTargetKind
    id: str


class ConversationSurfaceMetadataFactory:
    """Own the persisted metadata shape and backwards-compatible default."""

    META_KEY = "surface"
    AI_EDIT_TARGET_KEY = "ai_edit_target"
    AI_EDIT_TARGET_ID_MAX_LENGTH = 512

    @classmethod
    def resolve(cls, metadata: Mapping[str, Any] | None) -> ConversationSurfaceKind | None:
        raw = str((metadata or {}).get(cls.META_KEY) or "").strip()
        if not raw:
            return ConversationSurfaceKind.ORDINARY_CHAT
        try:
            return ConversationSurfaceKind(raw)
        except ValueError:
            return None

    @classmethod
    def matches(
        cls,
        metadata: Mapping[str, Any] | None,
        expected: ConversationSurfaceKind,
    ) -> bool:
        return cls.resolve(metadata) is expected

    @classmethod
    def is_host_owned(cls, metadata: Mapping[str, Any] | None) -> bool:
        return cls.resolve(metadata) in HIDDEN_CONVERSATION_SURFACES

    @classmethod
    def is_deletable_session(cls, metadata: Mapping[str, Any] | None) -> bool:
        """Allow user chat deletion plus explicit AI Edit session teardown."""

        return cls.resolve(metadata) in {
            ConversationSurfaceKind.ORDINARY_CHAT,
            ConversationSurfaceKind.AI_EDIT,
        }

    @classmethod
    def ai_edit_target(
        cls,
        kind: AiEditTargetKind | str,
        target_id: str,
    ) -> AiEditTargetIdentity:
        try:
            resolved_kind = kind if isinstance(kind, AiEditTargetKind) else AiEditTargetKind(kind)
        except ValueError as exc:
            raise ValueError("Unsupported AI Edit target kind") from exc
        resolved_id = str(target_id or "").strip()
        if not resolved_id:
            raise ValueError("AI Edit requires a stable target id")
        if len(resolved_id) > cls.AI_EDIT_TARGET_ID_MAX_LENGTH:
            raise ValueError("AI Edit target id is too long")
        return AiEditTargetIdentity(kind=resolved_kind, id=resolved_id)

    @classmethod
    def resolve_ai_edit_target(
        cls,
        metadata: Mapping[str, Any] | None,
    ) -> AiEditTargetIdentity | None:
        raw = (metadata or {}).get(cls.AI_EDIT_TARGET_KEY)
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls.ai_edit_target(
                str(raw.get("kind") or ""),
                str(raw.get("id") or ""),
            )
        except ValueError:
            return None

    @classmethod
    def matches_ai_edit_target(
        cls,
        metadata: Mapping[str, Any] | None,
        expected: AiEditTargetIdentity,
    ) -> bool:
        return cls.resolve_ai_edit_target(metadata) == expected

    @classmethod
    def build(
        cls,
        surface: ConversationSurfaceKind,
        *,
        current: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        ai_edit_target: AiEditTargetIdentity | None = None,
    ) -> dict[str, Any]:
        metadata = dict(current or {})
        metadata.update(extra or {})
        if surface is ConversationSurfaceKind.ORDINARY_CHAT:
            metadata.pop(cls.META_KEY, None)
        else:
            metadata[cls.META_KEY] = surface.value
        if surface is ConversationSurfaceKind.AI_EDIT:
            if ai_edit_target is not None:
                metadata[cls.AI_EDIT_TARGET_KEY] = {
                    "kind": ai_edit_target.kind.value,
                    "id": ai_edit_target.id,
                }
        else:
            metadata.pop(cls.AI_EDIT_TARGET_KEY, None)
        return metadata
