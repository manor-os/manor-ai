from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from packages.core.ai.runtime.surfaces import ChatSurface


class RuntimePrincipalKind(str, Enum):
    OWNER = "owner"
    WORKSPACE_MEMBER = "workspace_member"
    AGENT = "agent"
    EXTERNAL_CONTACT = "external_contact"
    ANONYMOUS_PUBLIC = "anonymous_public"
    SYSTEM_WORKER = "system_worker"
    DELEGATED = "delegated"


@dataclass(frozen=True)
class RuntimePrincipal:
    """Who is speaking, and which legacy user identity currently executes tools."""

    kind: RuntimePrincipalKind
    entity_id: str | None = None
    actor_user_id: str | None = None
    execution_user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    external_sender_id: str | None = None
    channel_type: str | None = None
    is_verified_external: bool = False
    legacy_owner_fallback: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_external(self) -> bool:
        return self.kind in {
            RuntimePrincipalKind.EXTERNAL_CONTACT,
            RuntimePrincipalKind.ANONYMOUS_PUBLIC,
        }


def resolve_runtime_principal(
    *,
    surface: ChatSurface,
    entity_id: str | None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    channel_context: dict[str, Any] | None = None,
    system_worker: bool = False,
) -> RuntimePrincipal:
    """Resolve the actor/execution identity for a Manor AI runtime surface."""

    channel_context = channel_context or {}
    external_sender_id = channel_context.get("source_id") or channel_context.get("sender_id")
    channel_type = channel_context.get("channel_type")
    verified_user_id = channel_context.get("user_id") if channel_context.get("is_verified") else None
    legacy_owner_fallback = bool(
        surface in {ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT}
        and user_id
        and not verified_user_id
    )

    execution_user_id = user_id

    if system_worker:
        kind = RuntimePrincipalKind.SYSTEM_WORKER
        actor_user_id = user_id
    elif surface in {ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT}:
        kind = RuntimePrincipalKind.EXTERNAL_CONTACT if external_sender_id else RuntimePrincipalKind.ANONYMOUS_PUBLIC
        actor_user_id = verified_user_id
        execution_user_id = verified_user_id
    elif agent_id and not user_id:
        kind = RuntimePrincipalKind.AGENT
        actor_user_id = None
    elif workspace_id:
        kind = RuntimePrincipalKind.WORKSPACE_MEMBER
        actor_user_id = user_id
    else:
        kind = RuntimePrincipalKind.OWNER
        actor_user_id = user_id

    return RuntimePrincipal(
        kind=kind,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        execution_user_id=execution_user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        external_sender_id=external_sender_id,
        channel_type=channel_type,
        is_verified_external=bool(verified_user_id),
        legacy_owner_fallback=legacy_owner_fallback,
        metadata={
            key: value
            for key, value in channel_context.items()
            if key in {"role", "display_name", "conversation_id", "channel_contact_id"}
        },
    )
