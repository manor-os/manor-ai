from __future__ import annotations

from enum import Enum


class ChatSurface(str, Enum):
    """Product entrypoint for one Manor AI turn."""

    GLOBAL_OWNER_CHAT = "global_owner_chat"
    AGENT_DM = "agent_dm"
    WORKSPACE_CHAT = "workspace_chat"
    PUBLIC_CUSTOMER_CHAT = "public_customer_chat"
    EXTERNAL_CHANNEL_CHAT = "external_channel_chat"
    FILE_EDITOR_CHAT = "file_editor_chat"
    WORKSPACE_DRAFT_ARCHITECT = "workspace_draft_architect"
    TASK_COMMENT_THREAD = "task_comment_thread"
    VOICE_CHAT = "voice_chat"
    WORKFLOW_AGENT_STEP = "workflow_agent_step"
    SCHEDULED_AGENT_RUN = "scheduled_agent_run"


def normalize_surface(value: ChatSurface | str | None) -> ChatSurface | None:
    if value is None or isinstance(value, ChatSurface):
        return value
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return ChatSurface(normalized)
    except ValueError as exc:
        valid = ", ".join(surface.value for surface in ChatSurface)
        raise ValueError(f"Unknown Manor AI surface: {value!r}. Valid surfaces: {valid}") from exc


def infer_chat_surface(
    *,
    surface: ChatSurface | str | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    ephemeral: bool = False,
) -> ChatSurface:
    """Infer the legacy chat route's surface during the migration window."""

    explicit = normalize_surface(surface)
    if explicit is not None:
        return explicit
    if workspace_id:
        return ChatSurface.WORKSPACE_CHAT
    if agent_id:
        return ChatSurface.AGENT_DM
    if ephemeral:
        return ChatSurface.FILE_EDITOR_CHAT
    return ChatSurface.GLOBAL_OWNER_CHAT


def surface_for_channel_type(channel_type: str | None) -> ChatSurface:
    """Map an inbound channel to the public/customer runtime surface."""

    if str(channel_type or "").lower() == "webchat":
        return ChatSurface.PUBLIC_CUSTOMER_CHAT
    return ChatSurface.EXTERNAL_CHANNEL_CHAT


def surface_for_channel_context(
    *,
    channel_type: str | None,
    is_verified: bool = False,
    role: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None,
) -> ChatSurface:
    """Map an inbound channel turn to the runtime surface it should use.

    Public webchat stays customer-safe. Other unverified/external senders stay
    external-safe. A claimed channel contact with an internal role should use
    the same internal surface the user would get in the web UI.
    """

    if str(channel_type or "").lower() == "webchat":
        return ChatSurface.PUBLIC_CUSTOMER_CHAT

    clean_role = str(role or "").strip().lower()
    verified_internal_user = bool(
        is_verified
        and user_id
        and clean_role
        and clean_role != "external"
    )
    if not verified_internal_user:
        return ChatSurface.EXTERNAL_CHANNEL_CHAT
    if workspace_id:
        return ChatSurface.WORKSPACE_CHAT
    if agent_id:
        return ChatSurface.AGENT_DM
    return ChatSurface.GLOBAL_OWNER_CHAT
