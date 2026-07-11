from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from packages.core.ai.runtime.surfaces import ChatSurface


LEGACY_WORKSPACE_TOOL_PROFILE = "workspace_agent"


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


@dataclass(frozen=True)
class RuntimeProfileSpec:
    profile: RuntimeProfile
    legacy_tool_profile: str | None = None
    allow_subagents_by_default: bool = False
    stream_allowed: bool = True


@dataclass(frozen=True)
class RuntimeTurnProfileNames:
    runtime_profile: str | None
    legacy_tool_profile: str | None


_PROFILE_BY_SURFACE: dict[ChatSurface, RuntimeProfileSpec] = {
    ChatSurface.GLOBAL_OWNER_CHAT: RuntimeProfileSpec(RuntimeProfile.OWNER_COPILOT),
    ChatSurface.AGENT_DM: RuntimeProfileSpec(RuntimeProfile.AGENT_DELEGATE),
    ChatSurface.WORKSPACE_CHAT: RuntimeProfileSpec(
        RuntimeProfile.WORKSPACE_OPERATOR,
        legacy_tool_profile=LEGACY_WORKSPACE_TOOL_PROFILE,
    ),
    ChatSurface.PUBLIC_CUSTOMER_CHAT: RuntimeProfileSpec(RuntimeProfile.EXTERNAL_CUSTOMER_SAFE),
    ChatSurface.EXTERNAL_CHANNEL_CHAT: RuntimeProfileSpec(RuntimeProfile.EXTERNAL_CHANNEL_SAFE),
    ChatSurface.FILE_EDITOR_CHAT: RuntimeProfileSpec(RuntimeProfile.FILE_EDITOR_PATCH),
    ChatSurface.WORKSPACE_DRAFT_ARCHITECT: RuntimeProfileSpec(
        RuntimeProfile.WORKSPACE_ARCHITECT,
        legacy_tool_profile=LEGACY_WORKSPACE_TOOL_PROFILE,
    ),
    ChatSurface.TASK_COMMENT_THREAD: RuntimeProfileSpec(
        RuntimeProfile.TASK_WORKER_FEEDBACK,
        legacy_tool_profile=LEGACY_WORKSPACE_TOOL_PROFILE,
    ),
    ChatSurface.VOICE_CHAT: RuntimeProfileSpec(RuntimeProfile.VOICE_SAFE),
    ChatSurface.WORKFLOW_AGENT_STEP: RuntimeProfileSpec(RuntimeProfile.WORKFLOW_STEP),
    ChatSurface.SCHEDULED_AGENT_RUN: RuntimeProfileSpec(
        RuntimeProfile.BACKGROUND_WORKER,
        stream_allowed=False,
    ),
}


def profile_spec_for_surface(surface: ChatSurface) -> RuntimeProfileSpec:
    return _PROFILE_BY_SURFACE[surface]


def profile_for_surface(surface: ChatSurface) -> RuntimeProfile:
    return profile_spec_for_surface(surface).profile


def legacy_tool_profile_for_surface(surface: ChatSurface) -> str | None:
    return profile_spec_for_surface(surface).legacy_tool_profile


def runtime_workspace_turn_profile_names(workspace_id: str | None) -> RuntimeTurnProfileNames:
    """Return Runtime/legacy profile names for a workspace-scoped turn."""

    if not workspace_id:
        return RuntimeTurnProfileNames(runtime_profile=None, legacy_tool_profile=None)
    spec = profile_spec_for_surface(ChatSurface.WORKSPACE_CHAT)
    return RuntimeTurnProfileNames(
        runtime_profile=spec.profile.value,
        legacy_tool_profile=spec.legacy_tool_profile,
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
