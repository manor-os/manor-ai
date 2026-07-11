from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.surfaces import ChatSurface


@dataclass(frozen=True)
class ChannelRuntimeContext:
    channel_type: str | None = None
    source_id: str | None = None
    display_name: str | None = None
    user_id: str | None = None
    role: str | None = None
    is_verified: bool = False
    conversation_id: str | None = None
    channel_contact_id: str | None = None
    channel_language: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "ChannelRuntimeContext | None":
        if not data:
            return None
        return cls(
            channel_type=data.get("channel_type"),
            source_id=data.get("source_id") or data.get("sender_id"),
            display_name=data.get("display_name"),
            user_id=data.get("user_id"),
            role=data.get("role"),
            is_verified=bool(data.get("is_verified")),
            conversation_id=data.get("conversation_id"),
            channel_contact_id=data.get("channel_contact_id"),
            channel_language=data.get("channel_language"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "source_id": self.source_id,
            "display_name": self.display_name,
            "user_id": self.user_id,
            "role": self.role,
            "is_verified": self.is_verified,
            "conversation_id": self.conversation_id,
            "channel_contact_id": self.channel_contact_id,
            "channel_language": self.channel_language,
        }


@dataclass(frozen=True)
class AIRuntimeRequest:
    surface: ChatSurface
    entity_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    thread_ref_kind: str | None = None
    thread_ref_id: str | None = None
    input_preview: str | None = None
    manual_skill_ids: tuple[str, ...] = ()
    channel_context: ChannelRuntimeContext | None = None
    editor_context: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
