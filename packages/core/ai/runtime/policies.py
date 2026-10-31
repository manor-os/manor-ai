from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from packages.core.ai.runtime.capabilities import (
    AGENT_INITIATIVE_TOOL_NAMES,
    allowed_tools_for_profile,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.file_context import check_file_context_policy
from packages.core.ai.runtime.profiles import EXTERNAL_RUNTIME_PROFILES, RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface


_ALWAYS_DENIED_FOR_EXTERNAL = {
    "render_response_surface",
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "patch_file",
    "replace_file",
    "delete_file",
    "inspect_file_engine",
    "list_files",
    "glob_files",
    "grep_files",
    "generate_file",
    "code_project",
    "manor",
    "workspace_agent",
    "workspace_update_task_runtime",
    "workspace_list_knowledge",
    "workspace_create_knowledge_folder",
    "workspace_add_knowledge_documents",
    "workspace_remove_knowledge_document",
    "workspace_update_knowledge_policy",
    "workspace_operation",
    "workspace_add_rule",
    "workspace_request_strategist_review",
    "create_scheduled_job",
    "cancel_scheduled_job",
    "toggle_scheduled_job",
    "run_scheduled_job_now",
    "sandbox_create",
    "sandbox",
    "sandbox_exec",
    "sandbox_status",
    "sandbox_respond",
    "sandbox_cancel",
    "sandbox_write_file",
    "sandbox_save_result",
    "sandbox_destroy",
} | set(AGENT_INITIATIVE_TOOL_NAMES)


class RuntimeInjectedTool(str, Enum):
    """Tools the RUNTIME hands a run, not capabilities a model discovers.

    They exist only when the caller armed them for that specific run (as a
    dynamic tool handler), so the surface allowlist — which describes which
    *capabilities* a surface may reach — must not gate them.

    A model cannot escalate by naming one of these: if the runtime did not
    arm it, no handler is registered and the call fails as an unknown tool.
    """

    SEND_CHANNEL_ATTACHMENT = "send_channel_attachment"

    @classmethod
    def names(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


class RuntimeToolPolicyCode(str, Enum):
    """Why a tool call was refused. Consumers compare against these members
    instead of matching on the message text."""

    INVALID_TOOL = "invalid_tool"
    TOOL_NOT_IN_RUNTIME_SURFACE = "tool_not_in_runtime_surface"
    EXTERNAL_PRINCIPAL_TOOL_DENIED = "external_principal_tool_denied"
    FILE_EDITOR_TOOL_DENIED = "file_editor_tool_denied"
    FILE_CONTEXT_DENIED = "file_context_denied"
    BLOCKED_BY_RUNTIME_POLICY = "blocked_by_runtime_policy"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


def is_runtime_policy_denial(result: Any) -> bool:
    """True when a tool result is this module's refusal payload.

    Structural: the payload's ``error`` must be a member of
    ``RuntimeToolPolicyCode``. Callers use this to tell "the tool ran and
    reported an error" apart from "the call never happened".
    """
    if isinstance(result, str):
        text = result.strip()
        if not (text.startswith("{") and text.endswith("}")):
            return False
        try:
            result = json.loads(text)
        except (ValueError, TypeError):
            return False
    if not isinstance(result, dict):
        return False
    return str(result.get("error") or "") in RuntimeToolPolicyCode.values()


@dataclass(frozen=True)
class RuntimeToolPolicyDecision:
    allowed: bool
    reason: str | None = None
    code: str | None = None
    tool_name: str | None = None

    def to_tool_result(self) -> str:
        return json.dumps(
            {
                "error": self.code or RuntimeToolPolicyCode.BLOCKED_BY_RUNTIME_POLICY.value,
                "message": self.reason or "This tool call is not allowed in this Manor AI surface.",
                "tool": self.tool_name,
            },
            ensure_ascii=False,
        )


def tool_name_from_schema(schema: dict[str, Any]) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip()
    return str(schema.get("name") or "").strip() if isinstance(schema, dict) else ""


def filter_runtime_tools(
    *,
    surface: ChatSurface,
    profile: RuntimeProfile,
    tools: Iterable[dict[str, Any]],
    allowed_tool_names: Iterable[str],
    profile_extra_allowed_tool_names: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Apply Manor surface/profile tool visibility limits.

    Internal surfaces currently preserve legacy behavior. External/customer and
    file-editor surfaces get a hard schema filter so the model never sees tools
    that the runtime would later deny.
    """

    initiative_blocked = (
        set(AGENT_INITIATIVE_TOOL_NAMES)
        if surface not in {ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM}
        else set()
    )
    profile_allowed = allowed_tools_for_profile(profile)
    if profile_allowed is not None:
        profile_allowed = (set(profile_allowed) | {
            str(name).strip()
            for name in profile_extra_allowed_tool_names
            if str(name or "").strip()
        }) - initiative_blocked
    incoming_allowed = {str(name) for name in allowed_tool_names if str(name or "").strip()}
    if profile_allowed is None:
        filtered_tools = [
            tool
            for tool in (tools or [])
            if tool_name_from_schema(tool) not in initiative_blocked
            and (not incoming_allowed or tool_name_from_schema(tool) in incoming_allowed)
        ]
        visible_names = {
            name
            for name in (tool_name_from_schema(tool) for tool in filtered_tools)
            if name
        }
        return filtered_tools, (incoming_allowed - initiative_blocked) or visible_names, incoming_allowed & initiative_blocked

    filtered: list[dict[str, Any]] = []
    visible_names: set[str] = set()
    blocked_names: set[str] = set()
    for tool in tools or []:
        name = tool_name_from_schema(tool)
        if not name:
            continue
        if name in profile_allowed:
            filtered.append(tool)
            visible_names.add(name)
        else:
            blocked_names.add(name)

    if incoming_allowed:
        filtered = [
            tool
            for tool in filtered
            if tool_name_from_schema(tool) in incoming_allowed
        ]
        visible_names = {
            name
            for name in (tool_name_from_schema(tool) for tool in filtered)
            if name
        }

    # Keep discovered allowed names aligned with what the schema exposes. When
    # a legacy caller only passed schemas, use the filtered schema names as the
    # hard allowlist so dynamic tool discovery cannot reopen the surface.
    effective_allowed = (incoming_allowed & profile_allowed) if incoming_allowed else visible_names
    return filtered, effective_allowed, blocked_names | (incoming_allowed - profile_allowed)


def check_runtime_tool_policy(
    *,
    envelope: RuntimeEnvelope | None,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> RuntimeToolPolicyDecision:
    """Hard runtime policy check before any tool handler executes."""

    if envelope is None:
        return RuntimeToolPolicyDecision(True, tool_name=tool_name)

    name = str(tool_name or "").strip()
    if not name:
        return RuntimeToolPolicyDecision(
            False,
            code=RuntimeToolPolicyCode.INVALID_TOOL.value,
            reason="The model attempted to call an empty tool name.",
            tool_name=tool_name,
        )

    file_context_decision = check_file_context_policy(
        envelope=envelope,
        tool_name=name,
        arguments=arguments or {},
    )
    if file_context_decision is not None and not file_context_decision.allowed:
        return RuntimeToolPolicyDecision(
            False,
            code=file_context_decision.code or RuntimeToolPolicyCode.FILE_CONTEXT_DENIED.value,
            reason=file_context_decision.reason,
            tool_name=name,
        )

    if name in RuntimeInjectedTool.names():
        # Runtime-injected, not a discoverable capability — see the enum's
        # docstring. Gating these on the surface allowlist silently destroyed
        # the deliverable of every subagent step on some surfaces.
        return RuntimeToolPolicyDecision(True, tool_name=name)

    if name in set(getattr(envelope, "blocked_tool_names", None) or ()):
        return RuntimeToolPolicyDecision(
            False,
            code=RuntimeToolPolicyCode.BLOCKED_BY_RUNTIME_POLICY.value,
            reason=f"`{name}` is blocked for this Runtime turn.",
            tool_name=name,
        )

    effective_allowed = getattr(envelope, "effective_allowed_tool_names", None)
    allowed = (
        effective_allowed()
        if callable(effective_allowed)
        else set(getattr(envelope, "allowed_tool_names", None) or ())
    )
    if allowed and name not in allowed:
        return RuntimeToolPolicyDecision(
            False,
            code=RuntimeToolPolicyCode.TOOL_NOT_IN_RUNTIME_SURFACE.value,
            reason=(
                f"`{name}` is not available in the `{envelope.surface.value}` "
                f"surface with `{envelope.profile.value}` profile."
            ),
            tool_name=name,
        )

    if envelope.profile in EXTERNAL_RUNTIME_PROFILES:
        group_file_grant = (
            envelope.surface == ChatSurface.PUBLIC_CUSTOMER_CHAT
            and name in {"generate_file", "sandbox"}
            and name in getattr(envelope, "room_file_tool_names", ())
            and (
                name != "sandbox"
                or envelope.discovered_tool_grants.allows("sandbox")
            )
            and bool((envelope.metadata or {}).get("room_id"))
            and bool((envelope.metadata or {}).get("room_agent_run_id"))
        )
        if (name in _ALWAYS_DENIED_FOR_EXTERNAL or name.startswith("mcp__")) and not group_file_grant:
            return RuntimeToolPolicyDecision(
                False,
                code=RuntimeToolPolicyCode.EXTERNAL_PRINCIPAL_TOOL_DENIED.value,
                reason=(
                    "External customer/channel conversations cannot execute "
                    "owner-private, internal mutation, file, sandbox, or MCP tools."
                ),
                tool_name=name,
            )

    if envelope.profile == RuntimeProfile.FILE_EDITOR_PATCH:
        allowed_file_tools = allowed_tools_for_profile(RuntimeProfile.FILE_EDITOR_PATCH) or set()
        editor_image_generation = (
            name == "generate_file"
            and bool((envelope.metadata or {}).get("editor_image_generation"))
            and str((arguments or {}).get("kind") or "").strip().lower() == "image"
        )
        if name not in allowed_file_tools and not editor_image_generation:
            return RuntimeToolPolicyDecision(
                False,
                code=RuntimeToolPolicyCode.FILE_EDITOR_TOOL_DENIED.value,
                reason=(
                    "File editor chat can only inspect its mounted context or use the "
                    "approval-gated native patch tool, plus the isolated sandbox; "
                    "business side effects, shell, and unrestricted write tools are blocked."
                ),
                tool_name=name,
            )

    if (
        name in AGENT_INITIATIVE_TOOL_NAMES
        and envelope.surface not in {ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM}
    ):
        return RuntimeToolPolicyDecision(
            False,
            code=RuntimeToolPolicyCode.TOOL_NOT_IN_RUNTIME_SURFACE.value,
            reason=(
                "Agent Initiatives are available only in private Agent or Manor Chat. "
                "Use a Workspace automation for scheduled Workspace work."
            ),
            tool_name=name,
        )

    return RuntimeToolPolicyDecision(True, tool_name=name)
