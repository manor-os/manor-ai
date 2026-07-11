from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from packages.core.ai.runtime.channel_tools import RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME
from packages.core.ai.runtime.profiles import RuntimeProfile


RiskLevel = Literal["safe", "write", "external", "destructive", "financial", "admin"]

_INTERNAL_PROFILES = (
    RuntimeProfile.OWNER_COPILOT,
    RuntimeProfile.AGENT_DELEGATE,
    RuntimeProfile.WORKSPACE_OPERATOR,
    RuntimeProfile.WORKSPACE_ARCHITECT,
    RuntimeProfile.TASK_WORKER_FEEDBACK,
    RuntimeProfile.WORKFLOW_STEP,
    RuntimeProfile.BACKGROUND_WORKER,
)
_CUSTOMER_SAFE_PROFILES = (
    RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
    RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
)
_VOICE_AND_EDITOR_PROFILES = (
    RuntimeProfile.FILE_EDITOR_PATCH,
    RuntimeProfile.VOICE_SAFE,
)
_ALL_RUNTIME_PROFILES = _INTERNAL_PROFILES + _CUSTOMER_SAFE_PROFILES + _VOICE_AND_EDITOR_PROFILES


@dataclass(frozen=True)
class BusinessCapability:
    id: str
    name: str
    description: str
    tool_names: tuple[str, ...] = ()
    profiles: tuple[RuntimeProfile, ...] = ()
    risk_level: RiskLevel = "safe"
    required_approval: bool = False
    metadata: dict = field(default_factory=dict)
    output_shape: str | None = None
    """Canonical shape name from packages.core.contracts.shapes; None = unconstrained (legacy)."""


CORE_CAPABILITIES: dict[str, BusinessCapability] = {
    "runtime.discovery": BusinessCapability(
        id="runtime.discovery",
        name="Runtime discovery",
        description="Discover tools and skills that are visible in this runtime surface.",
        tool_names=("search_tools", "list_skills", "get_skill_details"),
        profiles=_ALL_RUNTIME_PROFILES,
    ),
    "web.safe_search": BusinessCapability(
        id="web.safe_search",
        name="Safe web search",
        description="Use read-only web search and fetch tools.",
        tool_names=("web_search", "web_fetch", "browse_web"),
        profiles=_ALL_RUNTIME_PROFILES,
    ),
    "skill.invoke": BusinessCapability(
        id="skill.invoke",
        name="Invoke visible skill",
        description="Invoke a skill that is visible and allowed in this runtime surface.",
        tool_names=("invoke_skill",),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
            RuntimeProfile.VOICE_SAFE,
        ),
    ),
    "knowledge.public_search": BusinessCapability(
        id="knowledge.public_search",
        name="Public knowledge search",
        description="Search information that can be safely used for customer-facing replies.",
        tool_names=("workspace_search", "rag", "web_search", "web_fetch"),
        profiles=(
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        ),
    ),
    "customer_support.ticket_intake": BusinessCapability(
        id="customer_support.ticket_intake",
        name="Customer support ticket intake",
        description=(
            "Create a workspace task/ticket from a customer-facing chat while "
            "staying scoped to the current public conversation."
        ),
        tool_names=("workspace_create_task",),
        profiles=(
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "customer_support.reply": BusinessCapability(
        id="customer_support.reply",
        name="Customer support reply",
        description="Answer customer questions and produce channel-safe replies.",
        tool_names=("workspace_search", "rag", RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME),
        profiles=(RuntimeProfile.EXTERNAL_CHANNEL_SAFE,),
        risk_level="external",
        required_approval=True,
    ),
    "workspace.operate": BusinessCapability(
        id="workspace.operate",
        name="Workspace operation",
        description="Create tasks, update workspace rules, and request strategist review.",
        tool_names=("workspace_agent", "workspace_operation", "workspace_resolve_hitl", "workspace_create_task"),
        profiles=(RuntimeProfile.WORKSPACE_OPERATOR,),
        risk_level="write",
        required_approval=True,
    ),
    "workspace.search": BusinessCapability(
        id="workspace.search",
        name="Workspace search",
        description="Search workspace state, knowledge, tasks, and generated artifacts.",
        tool_names=("workspace_search", "workspace_list_knowledge", "rag"),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
    ),
    "workspace.task": BusinessCapability(
        id="workspace.task",
        name="Workspace task management",
        description="Create or update workspace tasks and runtime requirements.",
        tool_names=("workspace_create_task", "workspace_update_task_runtime", "workspace_agent"),
        profiles=(
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "workspace.knowledge": BusinessCapability(
        id="workspace.knowledge",
        name="Workspace knowledge management",
        description="Manage workspace knowledge folders and document attachments.",
        tool_names=(
            "workspace_create_knowledge_folder",
            "workspace_add_knowledge_documents",
            "workspace_remove_knowledge_document",
            "workspace_update_knowledge_policy",
        ),
        profiles=(
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "workspace.governance": BusinessCapability(
        id="workspace.governance",
        name="Workspace governance",
        description="Update workspace rules and request strategist review.",
        tool_names=("workspace_add_rule", "workspace_request_strategist_review", "workspace_operation"),
        profiles=(
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "external.social": BusinessCapability(
        id="external.social",
        name="External social publishing",
        description="Publish, mutate, or delete social platform content through connected integrations.",
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="external",
        required_approval=True,
        metadata={"action_key_prefixes": ("social_post.",)},
    ),
    "external.email": BusinessCapability(
        id="external.email",
        name="External email sending",
        description="Send or delete email through connected integrations.",
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="external",
        required_approval=True,
        metadata={"action_key_prefixes": ("email.",)},
    ),
    "external.message": BusinessCapability(
        id="external.message",
        name="External message sending",
        description="Send channel replies, DMs, or customer-facing messages.",
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        ),
        risk_level="external",
        required_approval=True,
        metadata={"action_key_prefixes": ("external_message.", "channel.reply")},
    ),
    "communication.notify": BusinessCapability(
        id="communication.notify",
        name="User notification",
        description=(
            "Find team members and send Manor notifications through "
            "user-configured channels."
        ),
        tool_names=("find_team_members", "notify_user"),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
            RuntimeProfile.VOICE_SAFE,
        ),
        risk_level="external",
    ),
    "file.read": BusinessCapability(
        id="file.read",
        name="File read",
        description="Inspect files and locate matching file paths.",
        tool_names=("read_file", "list_files", "glob_files", "grep_files"),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
    ),
    "file.write": BusinessCapability(
        id="file.write",
        name="File write",
        description="Create, edit, or generate workspace files.",
        tool_names=("write_file", "edit_file", "generate_file"),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "sandbox.execute": BusinessCapability(
        id="sandbox.execute",
        name="Sandbox execution",
        description="Run isolated sandbox commands and exchange sandbox files.",
        tool_names=(
            "sandbox_create",
            "sandbox_exec",
            "sandbox_read_file",
            "sandbox_write_file",
            "sandbox_save_result",
            "sandbox_destroy",
        ),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "automation.manage": BusinessCapability(
        id="automation.manage",
        name="Automation management",
        description="Create, list, run, toggle, or cancel scheduled jobs.",
        tool_names=(
            "create_scheduled_job",
            "list_scheduled_jobs",
            "cancel_scheduled_job",
            "toggle_scheduled_job",
            "run_scheduled_job_now",
        ),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="admin",
        required_approval=True,
    ),
    "manor.composite": BusinessCapability(
        id="manor.composite",
        name="Manor composite actions",
        description="Use the Manor composite gateway for first-party business actions.",
        tool_names=("manor",),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.TASK_WORKER_FEEDBACK,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="write",
        required_approval=True,
    ),
    "cli.execute": BusinessCapability(
        id="cli.execute",
        name="CLI execution",
        description="Run local shell commands through the controlled bash tool.",
        tool_names=("bash",),
        profiles=(
            RuntimeProfile.OWNER_COPILOT,
            RuntimeProfile.AGENT_DELEGATE,
            RuntimeProfile.WORKSPACE_OPERATOR,
            RuntimeProfile.WORKFLOW_STEP,
            RuntimeProfile.BACKGROUND_WORKER,
        ),
        risk_level="destructive",
        required_approval=True,
    ),
    "workspace.architect": BusinessCapability(
        id="workspace.architect",
        name="Workspace architect typed tools",
        description="Use typed draft-construction tools for workspace setup and repair.",
        tool_names=(
            "ws_commit_basics",
            "ws_propose_service",
            "ws_propose_goal",
            "ws_propose_agent_mapping",
            "ws_request_custom_agent",
            "ws_propose_channel",
            "ws_propose_rule",
            "ws_propose_automation",
            "ws_set_evaluation",
            "ws_set_budget",
            "ws_search_entity_agents",
            "ws_search_blueprints",
            "ws_get_draft",
            "ws_lint_draft",
            "ws_mark_ready",
        ),
        profiles=(RuntimeProfile.WORKSPACE_ARCHITECT,),
        risk_level="write",
        required_approval=True,
    ),
    "file.patch": BusinessCapability(
        id="file.patch",
        name="File patch proposal",
        description="Inspect the current file context and propose a UI-confirmed patch.",
        tool_names=("read_file", "list_files", "glob_files", "grep_files"),
        profiles=(RuntimeProfile.FILE_EDITOR_PATCH,),
        risk_level="safe",
    ),
}


_LEGACY_INTERNAL_PROFILES = {
    RuntimeProfile.OWNER_COPILOT,
    RuntimeProfile.AGENT_DELEGATE,
    RuntimeProfile.WORKSPACE_OPERATOR,
    RuntimeProfile.WORKSPACE_ARCHITECT,
    RuntimeProfile.TASK_WORKER_FEEDBACK,
    RuntimeProfile.WORKFLOW_STEP,
    RuntimeProfile.BACKGROUND_WORKER,
}


def capabilities_for_tool_names(
    tool_names: set[str],
    *,
    profile: RuntimeProfile | None = None,
) -> list[BusinessCapability]:
    matches: list[BusinessCapability] = []
    for capability in CORE_CAPABILITIES.values():
        if profile is not None and capability.profiles and profile not in capability.profiles:
            continue
        if tool_names.intersection(capability.tool_names):
            matches.append(capability)
    return matches


def capability_for_id(capability_id: str | None) -> BusinessCapability | None:
    key = str(capability_id or "").strip()
    if not key:
        return None
    return CORE_CAPABILITIES.get(key)


def tool_names_for_capability_ids(
    capability_ids: set[str] | list[str] | tuple[str, ...],
    *,
    profile: RuntimeProfile | None = None,
) -> set[str]:
    names: set[str] = set()
    for capability_id in capability_ids:
        capability = capability_for_id(capability_id)
        if capability is None:
            continue
        if profile is not None and capability.profiles and profile not in capability.profiles:
            continue
        names.update(capability.tool_names)
    return names


def classified_tool_names_for_profile(
    profile: RuntimeProfile,
    tool_names: set[str],
) -> set[str]:
    names: set[str] = set()
    for capability in capabilities_for_tool_names(tool_names, profile=profile):
        names.update(name for name in capability.tool_names if name in tool_names)
    return names


def allowed_tools_for_profile(profile: RuntimeProfile) -> set[str] | None:
    """Return hard allowed tools for constrained profiles.

    ``None`` means the profile is still backed by the legacy tool resolver while
    we migrate internal BusinessCapability bindings.
    """

    if profile in _LEGACY_INTERNAL_PROFILES:
        return None
    allowed: set[str] = set()
    for capability in CORE_CAPABILITIES.values():
        if profile in capability.profiles:
            allowed.update(capability.tool_names)
    return allowed


def capability_ids_for_profile_tools(
    profile: RuntimeProfile,
    tool_names: set[str],
) -> tuple[str, ...]:
    return tuple(
        capability.id
        for capability in capabilities_for_tool_names(tool_names, profile=profile)
    )


def unclassified_tool_names_for_profile(
    profile: RuntimeProfile,
    tool_names: set[str],
) -> tuple[str, ...]:
    classified = classified_tool_names_for_profile(profile, tool_names)
    return tuple(sorted(tool_names - classified))
