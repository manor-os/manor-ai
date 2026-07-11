from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from packages.core.ai.runtime.capabilities import allowed_tools_for_profile
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.file_context import check_file_context_policy
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface


_ALWAYS_DENIED_FOR_EXTERNAL = {
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "list_files",
    "glob_files",
    "grep_files",
    "generate_file",
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
    "sandbox_exec",
    "sandbox_write_file",
    "sandbox_save_result",
    "sandbox_destroy",
}


@dataclass(frozen=True)
class RuntimeToolPolicyDecision:
    allowed: bool
    reason: str | None = None
    code: str | None = None
    tool_name: str | None = None

    def to_tool_result(self) -> str:
        return json.dumps(
            {
                "error": self.code or "blocked_by_runtime_policy",
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
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Apply Manor surface/profile tool visibility limits.

    Internal surfaces currently preserve legacy behavior. External/customer and
    file-editor surfaces get a hard schema filter so the model never sees tools
    that the runtime would later deny.
    """

    del surface
    profile_allowed = allowed_tools_for_profile(profile)
    incoming_allowed = {str(name) for name in allowed_tool_names if str(name or "").strip()}
    if profile_allowed is None:
        filtered_tools = [
            tool
            for tool in (tools or [])
            if not incoming_allowed or tool_name_from_schema(tool) in incoming_allowed
        ]
        visible_names = {
            name
            for name in (tool_name_from_schema(tool) for tool in filtered_tools)
            if name
        }
        return filtered_tools, incoming_allowed or visible_names, set()

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
            code="invalid_tool",
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
            code=file_context_decision.code or "file_context_denied",
            reason=file_context_decision.reason,
            tool_name=name,
        )

    allowed = set(envelope.allowed_tool_names or ())
    if allowed and name not in allowed:
        return RuntimeToolPolicyDecision(
            False,
            code="tool_not_in_runtime_surface",
            reason=(
                f"`{name}` is not available in the `{envelope.surface.value}` "
                f"surface with `{envelope.profile.value}` profile."
            ),
            tool_name=name,
        )

    if envelope.profile in {
        RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
    }:
        if name in _ALWAYS_DENIED_FOR_EXTERNAL or name.startswith("mcp__"):
            return RuntimeToolPolicyDecision(
                False,
                code="external_principal_tool_denied",
                reason=(
                    "External customer/channel conversations cannot execute "
                    "owner-private, internal mutation, file, sandbox, or MCP tools."
                ),
                tool_name=name,
            )

    if envelope.profile == RuntimeProfile.FILE_EDITOR_PATCH:
        allowed_file_tools = allowed_tools_for_profile(RuntimeProfile.FILE_EDITOR_PATCH) or set()
        if name not in allowed_file_tools:
            return RuntimeToolPolicyDecision(
                False,
                code="file_editor_tool_denied",
                reason=(
                    "File editor chat can only inspect context and propose patches; "
                    "it cannot execute business side-effect, shell, sandbox, or write tools."
                ),
                tool_name=name,
            )

    return RuntimeToolPolicyDecision(True, tool_name=name)
