from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from packages.core.ai.runtime.surfaces import ChatSurface


WORKSPACE_AGENT_TOOL_PROFILE = "workspace_agent"


class RuntimeProfile(str, Enum):
    """Manor-level capability boundary template."""

    OWNER_COPILOT = "owner_copilot"
    AGENT_DELEGATE = "agent_delegate"
    WORKSPACE_OPERATOR = "workspace_operator"
    EXTERNAL_CUSTOMER_SAFE = "external_customer_safe"
    EXTERNAL_CHANNEL_SAFE = "external_channel_safe"
    FILE_EDITOR_PATCH = "file_editor_patch"
    WORKSPACE_ARCHITECT = "workspace_architect"
    TASK_WORKER_FEEDBACK = "task_worker_feedback"
    VOICE_SAFE = "voice_safe"
    WORKFLOW_STEP = "workflow_step"
    BACKGROUND_WORKER = "background_worker"


# The only profiles that serve principals outside the account: public customer
# chat visitors and external channel senders. Every other profile (present or
# future) is internal, so internal-only capabilities derive from this set
# instead of enumerating internal profiles by hand.
EXTERNAL_RUNTIME_PROFILES: frozenset[RuntimeProfile] = frozenset({
    RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
    RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
})
INTERNAL_RUNTIME_PROFILES: tuple[RuntimeProfile, ...] = tuple(
    profile for profile in RuntimeProfile if profile not in EXTERNAL_RUNTIME_PROFILES
)


@dataclass(frozen=True)
class RuntimeProfileSpec:
    profile: RuntimeProfile
    tool_profile: str | None = None
    allow_subagents_by_default: bool = False
    stream_allowed: bool = True


@dataclass(frozen=True)
class RuntimeTurnProfileNames:
    runtime_profile: str | None
    tool_profile: str | None


_PROFILE_BY_SURFACE: dict[ChatSurface, RuntimeProfileSpec] = {
    ChatSurface.GLOBAL_OWNER_CHAT: RuntimeProfileSpec(RuntimeProfile.OWNER_COPILOT),
    ChatSurface.AGENT_DM: RuntimeProfileSpec(RuntimeProfile.AGENT_DELEGATE),
    ChatSurface.WORKSPACE_CHAT: RuntimeProfileSpec(
        RuntimeProfile.WORKSPACE_OPERATOR,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    ),
    ChatSurface.PUBLIC_CUSTOMER_CHAT: RuntimeProfileSpec(RuntimeProfile.EXTERNAL_CUSTOMER_SAFE),
    ChatSurface.EXTERNAL_CHANNEL_CHAT: RuntimeProfileSpec(RuntimeProfile.EXTERNAL_CHANNEL_SAFE),
    ChatSurface.FILE_EDITOR_CHAT: RuntimeProfileSpec(RuntimeProfile.FILE_EDITOR_PATCH),
    ChatSurface.WORKSPACE_DRAFT_ARCHITECT: RuntimeProfileSpec(
        RuntimeProfile.WORKSPACE_ARCHITECT,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    ),
    ChatSurface.TASK_COMMENT_THREAD: RuntimeProfileSpec(
        RuntimeProfile.TASK_WORKER_FEEDBACK,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    ),
    ChatSurface.VOICE_CHAT: RuntimeProfileSpec(RuntimeProfile.VOICE_SAFE),
    ChatSurface.WORKFLOW_AGENT_STEP: RuntimeProfileSpec(RuntimeProfile.WORKFLOW_STEP),
    ChatSurface.SCHEDULED_AGENT_RUN: RuntimeProfileSpec(
        RuntimeProfile.BACKGROUND_WORKER,
        stream_allowed=False,
    ),
}


# Surfaces derive their internal/external side from their profile.
EXTERNAL_CHAT_SURFACES: frozenset[ChatSurface] = frozenset(
    surface
    for surface, spec in _PROFILE_BY_SURFACE.items()
    if spec.profile in EXTERNAL_RUNTIME_PROFILES
)
INTERNAL_CHAT_SURFACES: frozenset[ChatSurface] = frozenset(
    surface for surface in _PROFILE_BY_SURFACE if surface not in EXTERNAL_CHAT_SURFACES
)


def runtime_profile_is_external(value: RuntimeProfile | str | None) -> bool:
    return normalize_runtime_profile(value) in EXTERNAL_RUNTIME_PROFILES


def runtime_surface_is_external(value: ChatSurface | str | None) -> bool:
    raw = getattr(value, "value", value)
    try:
        surface = ChatSurface(str(raw or "").strip())
    except ValueError:
        return False
    return surface in EXTERNAL_CHAT_SURFACES


def runtime_surface_is_internal(value: ChatSurface | str | None) -> bool:
    """Whether a runtime surface serves account-internal principals.

    ``None`` is a legacy unscoped internal caller. An unrecognized surface
    name fails closed: it is neither internal nor external.
    """

    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    if not text:
        return True
    try:
        return ChatSurface(text) in INTERNAL_CHAT_SURFACES
    except ValueError:
        return False


def profile_spec_for_surface(surface: ChatSurface) -> RuntimeProfileSpec:
    return _PROFILE_BY_SURFACE[surface]


def profile_for_surface(surface: ChatSurface) -> RuntimeProfile:
    return profile_spec_for_surface(surface).profile


def tool_profile_for_surface(surface: ChatSurface) -> str | None:
    return profile_spec_for_surface(surface).tool_profile


def runtime_workspace_turn_profile_names(workspace_id: str | None) -> RuntimeTurnProfileNames:
    """Return runtime and tool-visibility profile names for a workspace turn."""

    if not workspace_id:
        return RuntimeTurnProfileNames(runtime_profile=None, tool_profile=None)
    spec = profile_spec_for_surface(ChatSurface.WORKSPACE_CHAT)
    return RuntimeTurnProfileNames(
        runtime_profile=spec.profile.value,
        tool_profile=spec.tool_profile,
    )


def normalize_runtime_profile(value: RuntimeProfile | str | None) -> RuntimeProfile | None:
    if isinstance(value, RuntimeProfile):
        return value
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return RuntimeProfile(normalized)
    except ValueError:
        return None


def runtime_profile_name(value: RuntimeProfile | str | None) -> str | None:
    profile = normalize_runtime_profile(value)
    return profile.value if profile is not None else None


def runtime_profile_name_for_surface(
    surface: ChatSurface,
    value: RuntimeProfile | str | None = None,
) -> str:
    return runtime_profile_name(value) or profile_for_surface(surface).value
