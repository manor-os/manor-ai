from __future__ import annotations

import datetime
import logging
import platform
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal


RuntimePromptMode = Literal["full", "minimal", "none"]
RuntimePromptSectionFn = Callable[[Any], str | None | Awaitable[str | None]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimePromptSectionSpec:
    name: str
    modes: tuple[RuntimePromptMode, ...] = ("full", "minimal")
    group: str = "runtime"
    description: str = ""


DEFAULT_RUNTIME_PROMPT_SECTIONS: tuple[RuntimePromptSectionSpec, ...] = (
    RuntimePromptSectionSpec(
        "agent_identity",
        ("full", "minimal"),
        "identity",
        "Base agent identity and file-based supplements.",
    ),
    RuntimePromptSectionSpec(
        "response_language_guidance",
        ("full", "minimal"),
        "identity",
        "User-visible response language constraints.",
    ),
    RuntimePromptSectionSpec(
        "output_discipline",
        ("full", "minimal"),
        "identity",
        "One-clean-answer rule: no redundant preamble or stacked versions.",
    ),
    RuntimePromptSectionSpec("user_context", ("full",), "context", "Current user."),
    RuntimePromptSectionSpec("entity_context", ("full",), "context", "Organization."),
    RuntimePromptSectionSpec("workspace_context", ("full",), "context", "Workspace."),
    RuntimePromptSectionSpec(
        "workspace_agent_mode",
        ("full",),
        "routing",
        "Workspace operating contract.",
    ),
    RuntimePromptSectionSpec(
        "workspace_operating_memory",
        ("full",),
        "memory",
        "Workspace and workspace-agent operating memory.",
    ),
    RuntimePromptSectionSpec(
        "available_tools_section",
        ("full", "minimal"),
        "tools",
        "Loaded runtime tool surface summary.",
    ),
    RuntimePromptSectionSpec(
        "runtime_approval_resume_guidance",
        ("full",),
        "approval",
        "Approval resume retry contract.",
    ),
    RuntimePromptSectionSpec(
        "local_coding_cli_routing_guidance",
        ("full",),
        "routing",
        "Local coding CLI route selection.",
    ),
    RuntimePromptSectionSpec(
        "external_integration_routing_guidance",
        ("full",),
        "routing",
        "External integration route selection.",
    ),
    RuntimePromptSectionSpec(
        "external_platform_draft_guidance",
        ("full",),
        "routing",
        "External platform draft bundle route selection.",
    ),
    RuntimePromptSectionSpec(
        "code_artifact_routing_guidance",
        ("full",),
        "routing",
        "Standalone code artifact route selection.",
    ),
    RuntimePromptSectionSpec(
        "workspace_artifact_routing_guidance",
        ("full",),
        "routing",
        "Workspace artifact route selection.",
    ),
    RuntimePromptSectionSpec(
        "workspace_in_flight_task_update_guidance",
        ("full",),
        "routing",
        "Append-only in-flight task update route selection.",
    ),
    RuntimePromptSectionSpec(
        "available_skills_section",
        ("full",),
        "skills",
        "Runtime-visible skill descriptors.",
    ),
    RuntimePromptSectionSpec(
        "tool_usage_guidance",
        ("full",),
        "tools",
        "Generic runtime tool use contract.",
    ),
    RuntimePromptSectionSpec(
        "file_approval_guidance",
        ("full",),
        "approval",
        "Platform-managed file approval contract.",
    ),
    RuntimePromptSectionSpec(
        "agent_memories",
        ("full",),
        "memory",
        "Scoped agent and user memories.",
    ),
    RuntimePromptSectionSpec(
        "runtime_context",
        ("full", "minimal"),
        "runtime",
        "Time, platform, entity, and agent runtime metadata.",
    ),
    RuntimePromptSectionSpec(
        "extra_context_section",
        ("full", "minimal"),
        "context",
        "Resolved runtime context blocks and entrypoint-specific attachments.",
    ),
)


def runtime_prompt_section_specs(
    mode: RuntimePromptMode | None = None,
) -> tuple[RuntimePromptSectionSpec, ...]:
    if mode is None:
        return DEFAULT_RUNTIME_PROMPT_SECTIONS
    return tuple(spec for spec in DEFAULT_RUNTIME_PROMPT_SECTIONS if mode in spec.modes)


def runtime_prompt_section_names(
    mode: RuntimePromptMode | None = None,
) -> tuple[str, ...]:
    return tuple(spec.name for spec in runtime_prompt_section_specs(mode))


def agent_identity(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_context import runtime_agent_identity_prompt
    from packages.core.ai.runtime.profiles import RuntimeProfile

    agent = getattr(ctx, "agent", None)
    runtime_envelope = getattr(ctx, "runtime_envelope", None)
    profile = getattr(runtime_envelope, "profile", None)
    profile_value = getattr(profile, "value", profile) or getattr(ctx, "runtime_profile_name", None)
    allow_agent_files = profile_value not in {
        RuntimeProfile.EXTERNAL_CUSTOMER_SAFE.value,
        RuntimeProfile.EXTERNAL_CHANNEL_SAFE.value,
    }
    rendered = runtime_agent_identity_prompt(
        agent_system_prompt=(
            agent.system_prompt
            if agent and getattr(agent, "system_prompt", None)
            else None
        ),
        entity_id=getattr(ctx, "entity_id", None),
        agent_id=getattr(ctx, "agent_id", None),
        user_id=getattr(ctx, "user_id", None),
        allow_agent_files=allow_agent_files,
    )
    ctx.prompt_source = rendered.source
    ctx.agent_files_loaded = dict(rendered.agent_files_loaded or {})
    return rendered.prompt


def user_context(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_context import runtime_user_context_prompt

    return runtime_user_context_prompt(getattr(ctx, "user", None))


def entity_context(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_context import runtime_entity_context_prompt

    return runtime_entity_context_prompt(getattr(ctx, "entity", None))


def response_language_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import runtime_response_language_guidance

    return runtime_response_language_guidance(getattr(ctx, "active_user_message", None))


def output_discipline(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import runtime_output_discipline_guidance

    return runtime_output_discipline_guidance()


def workspace_context(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_context import runtime_workspace_context_prompt

    return runtime_workspace_context_prompt(getattr(ctx, "workspace", None))


def workspace_agent_mode(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import runtime_workspace_agent_mode_guidance

    return runtime_workspace_agent_mode_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        workspace_id=getattr(ctx, "workspace_id", None),
    )


def workspace_operating_memory(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_context import (
        runtime_workspace_operating_memory_prompt,
    )

    return runtime_workspace_operating_memory_prompt(
        envelope=getattr(ctx, "runtime_envelope", None),
        entity_id=getattr(ctx, "entity_id", None),
        workspace_id=getattr(ctx, "workspace_id", None),
        workspace=getattr(ctx, "workspace", None),
        agent_id=getattr(ctx, "agent_id", None),
    )


def available_tools_section(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import runtime_available_tools_section

    return runtime_available_tools_section(getattr(ctx, "tools", None))


def local_coding_cli_routing_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_local_coding_cli_routing_guidance,
    )

    return runtime_local_coding_cli_routing_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        active_user_message=getattr(ctx, "active_user_message", None),
        tool_names=getattr(ctx, "tool_names", None),
        manual_skill_selected=bool(getattr(ctx, "manual_skill_selected", False)),
    )


def external_integration_routing_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_external_integration_routing_guidance,
    )

    return runtime_external_integration_routing_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        active_user_message=getattr(ctx, "active_user_message", None),
        tool_names=getattr(ctx, "tool_names", None),
        workspace_id=getattr(ctx, "workspace_id", None),
    )


def runtime_approval_resume_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.approval_messages import runtime_approval_resume_guidance

    return runtime_approval_resume_guidance(getattr(ctx, "active_user_message", None))


def external_platform_draft_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_external_platform_draft_guidance,
    )

    return runtime_external_platform_draft_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        active_user_message=getattr(ctx, "active_user_message", None),
        tool_names=getattr(ctx, "tool_names", None),
    )


def code_artifact_routing_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_code_artifact_routing_guidance,
    )

    return runtime_code_artifact_routing_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        active_user_message=getattr(ctx, "active_user_message", None),
        tool_names=getattr(ctx, "tool_names", None),
        manual_skill_selected=bool(getattr(ctx, "manual_skill_selected", False)),
    )


def workspace_artifact_routing_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_workspace_artifact_routing_guidance,
    )

    return runtime_workspace_artifact_routing_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        active_user_message=getattr(ctx, "active_user_message", None),
        tool_names=getattr(ctx, "tool_names", None),
        workspace_id=getattr(ctx, "workspace_id", None),
        manual_skill_selected=bool(getattr(ctx, "manual_skill_selected", False)),
    )


def workspace_in_flight_task_update_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_workspace_in_flight_task_update_guidance,
    )

    return runtime_workspace_in_flight_task_update_guidance(
        envelope=getattr(ctx, "runtime_envelope", None),
        active_user_message=getattr(ctx, "active_user_message", None),
        tool_names=getattr(ctx, "tool_names", None),
        workspace_id=getattr(ctx, "workspace_id", None),
    )


def runtime_context(ctx: Any) -> str | None:
    import zoneinfo

    user = getattr(ctx, "user", None)
    user_tz_name = None
    if user and getattr(user, "timezone", None):
        user_tz_name = user.timezone

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    parts: list[str] = []
    if user_tz_name and user_tz_name != "UTC":
        try:
            user_tz = zoneinfo.ZoneInfo(user_tz_name)
            now_local = now_utc.astimezone(user_tz)
            parts.append(f"current_time={now_local.strftime('%Y-%m-%d %H:00')} ({user_tz_name})")
        except (KeyError, Exception):
            parts.append(f"current_time={now_utc.strftime('%Y-%m-%d %H:00')} UTC")
    else:
        parts.append(f"current_time={now_utc.strftime('%Y-%m-%d %H:00')} UTC")

    parts.append(f"platform={platform.system()}")
    if getattr(ctx, "entity_id", None):
        parts.append(f"entity={ctx.entity_id}")
    if getattr(ctx, "agent_id", None):
        parts.append(f"agent={ctx.agent_id}")
    return "Runtime: " + " | ".join(parts)


def _context_tool_names(ctx: Any) -> set[str]:
    tool_names = set(getattr(ctx, "tool_names", None) or [])
    if not tool_names:
        for tool in getattr(ctx, "tools", None) or []:
            fn = tool.get("function", tool) if isinstance(tool, dict) else {}
            name = fn.get("name")
            if name:
                tool_names.add(name)
    return tool_names


def tool_usage_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance

    return runtime_tool_usage_guidance(
        tool_names=_context_tool_names(ctx),
        has_tools=bool(getattr(ctx, "tools", None)),
        active_user_message=getattr(ctx, "active_user_message", None),
    )


def file_approval_guidance(ctx: Any) -> str | None:
    from packages.core.ai.runtime.approval_messages import runtime_file_approval_guidance

    return runtime_file_approval_guidance(_context_tool_names(ctx))


async def agent_memories(ctx: Any) -> str | None:
    from packages.core.ai.runtime.prompt_context import runtime_agent_memories_prompt

    return await runtime_agent_memories_prompt(
        getattr(ctx, "db", None),
        envelope=getattr(ctx, "runtime_envelope", None),
        entity_id=getattr(ctx, "entity_id", None),
        agent_id=getattr(ctx, "agent_id", None),
        user_id=getattr(ctx, "user_id", None),
    )


async def available_skills_section(ctx: Any) -> str | None:
    db = getattr(ctx, "db", None)
    entity_id = getattr(ctx, "entity_id", None)
    runtime_skill_descriptors = list(getattr(ctx, "runtime_skill_descriptors", None) or [])
    if (not db or not entity_id) and not runtime_skill_descriptors:
        return None
    loaded_tool_names = set(getattr(ctx, "tool_names", None) or [])
    visible_tool_names = loaded_tool_names | set(getattr(ctx, "allowed_tool_names", None) or set())
    if "invoke_skill" not in visible_tool_names:
        return None
    try:
        from packages.core.ai.runtime.skills import (
            render_runtime_available_skills_section,
            runtime_available_skills_omission_section,
        )

        omitted = runtime_available_skills_omission_section(
            active_user_message=getattr(ctx, "active_user_message", None),
            manual_skill_selected=bool(getattr(ctx, "manual_skill_selected", False)),
        )
        if omitted:
            return omitted

        if runtime_skill_descriptors:
            skills = runtime_skill_descriptors
        elif getattr(ctx, "runtime_envelope", None) is not None:
            return None
        elif getattr(ctx, "agent_id", None):
            from packages.core.services.skill_service import list_skills_for_agent
            skills = await list_skills_for_agent(
                db,
                entity_id,
                ctx.agent_id,
                workspace_id=getattr(ctx, "workspace_id", None),
            )
        else:
            from packages.core.services.skill_service import list_skills
            skills = await list_skills(db, entity_id)

        return render_runtime_available_skills_section(
            skills,
            active_user_message=getattr(ctx, "active_user_message", None),
            manual_skill_selected=bool(getattr(ctx, "manual_skill_selected", False)),
            loaded_tool_names=loaded_tool_names,
            available_tool_names=visible_tool_names,
        )
    except Exception:
        logger.debug("Failed to load skills for prompt", exc_info=True)
        return None


def extra_context_section(ctx: Any) -> str | None:
    return getattr(ctx, "extra_context", None)


def runtime_prompt_section_renderers() -> dict[str, RuntimePromptSectionFn]:
    return {
        "agent_identity": agent_identity,
        "response_language_guidance": response_language_guidance,
        "output_discipline": output_discipline,
        "user_context": user_context,
        "entity_context": entity_context,
        "workspace_context": workspace_context,
        "workspace_agent_mode": workspace_agent_mode,
        "workspace_operating_memory": workspace_operating_memory,
        "available_tools_section": available_tools_section,
        "runtime_approval_resume_guidance": runtime_approval_resume_guidance,
        "local_coding_cli_routing_guidance": local_coding_cli_routing_guidance,
        "external_integration_routing_guidance": external_integration_routing_guidance,
        "external_platform_draft_guidance": external_platform_draft_guidance,
        "code_artifact_routing_guidance": code_artifact_routing_guidance,
        "workspace_artifact_routing_guidance": workspace_artifact_routing_guidance,
        "workspace_in_flight_task_update_guidance": workspace_in_flight_task_update_guidance,
        "available_skills_section": available_skills_section,
        "tool_usage_guidance": tool_usage_guidance,
        "file_approval_guidance": file_approval_guidance,
        "agent_memories": agent_memories,
        "runtime_context": runtime_context,
        "extra_context_section": extra_context_section,
    }
