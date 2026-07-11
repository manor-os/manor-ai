from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterable
import logging
from typing import Any

from packages.core.ai.runtime.approvals import RuntimeApprovalMiddleware
from packages.core.ai.runtime.harness import RuntimeHarness
from packages.core.ai.runtime.chrome_routing import (
    runtime_blocked_chrome_action_shortcut,
    runtime_blocked_chrome_open_shortcut,
    runtime_blocked_generic_web_for_chrome_local_browser,
)
from packages.core.ai.runtime.tool_availability import runtime_blocked_mcp_call_result
from packages.core.ai.runtime.tool_context import runtime_injected_tool_context_args


RUNTIME_ENVELOPE_AWARE_TOOLS = frozenset(
    {
        "invoke_skill",
        "list_skills",
        "get_skill_details",
        "manor",
        "rag",
        "workspace_search",
        "workspace_create_task",
    }
)


def _runtime_policy_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    active_user_message: str | None,
) -> dict[str, Any]:
    """Return approval-only context without changing the handler payload."""
    policy_arguments = dict(arguments)
    if tool_name.startswith("mcp__chrome__") and active_user_message:
        policy_arguments.setdefault("active_user_message", active_user_message)
    return policy_arguments


@dataclass(frozen=True)
class RuntimePreparedToolExecution:
    arguments: dict[str, Any]
    harness: RuntimeHarness | None = None
    blocked_result: str | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_result is not None


async def runtime_execute_prepared_tool_handler(
    *,
    tool_name: str,
    handler: Callable[..., Any],
    prepared: RuntimePreparedToolExecution,
    entity_id: str | None = None,
    user_id: str | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """Call a prepared tool handler and record standard runtime events."""
    harness = prepared.harness
    arguments = prepared.arguments
    if harness is not None:
        harness.record_event("tool_start", tool_name=tool_name)
    try:
        result = handler(
            entity_id=entity_id or "",
            user_id=user_id or "",
            **arguments,
        )
        if hasattr(result, "__await__"):
            result = await result
        if harness is not None:
            harness.record_event("tool_end", tool_name=tool_name)
        return str(result)
    except TypeError:
        try:
            result = handler(entity_id=entity_id or "", **arguments)
            if hasattr(result, "__await__"):
                result = await result
            if harness is not None:
                harness.record_event("tool_end", tool_name=tool_name)
            return str(result)
        except Exception as exc:
            if harness is not None:
                harness.record_event("error", tool_name=tool_name, message=str(exc))
            if logger is not None:
                logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
            return f"Error: {exc}"
    except Exception as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        if logger is not None:
            logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
        return f"Error: {exc}"


async def runtime_execute_scoped_dynamic_tool_handler(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    handler: Callable[[dict[str, Any]], Any],
    runtime_envelope: Any | None = None,
) -> str:
    """Execute a runtime-scoped dynamic handler outside the global registry.

    Some entrypoints add per-run tools whose handlers are closures rather than
    registered ToolPool handlers. They still need the same RuntimeHarness
    policy check and event trace, but should not receive hidden registry
    context kwargs.
    """

    harness = RuntimeHarness(runtime_envelope) if runtime_envelope is not None else None
    if harness is not None:
        decision = harness.check_tool_call(tool_name, arguments)
        if not decision.allowed:
            return decision.to_tool_result()
        harness.record_event("tool_start", tool_name=tool_name)
    try:
        result = handler(arguments)
        if hasattr(result, "__await__"):
            result = await result
        if harness is not None:
            harness.record_event("tool_end", tool_name=tool_name)
        return str(result)
    except Exception as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        raise


async def runtime_prepare_tool_execution(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    runtime_artifact_urls: Iterable[str] | None = None,
    dependency_artifact_urls: Iterable[str] | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    manual_skill_slugs: list[str] | None = None,
    legacy_tool_profile: str | None = None,
    allowed_tool_names: set[str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
    llm_model: str | None = None,
    runtime_envelope: Any | None = None,
) -> RuntimePreparedToolExecution:
    """Apply Runtime Harness gates and inject hidden tool context."""
    entity = entity_id or ""
    user = user_id or ""
    harness = RuntimeHarness(runtime_envelope) if runtime_envelope is not None else None

    if harness is not None:
        runtime_decision = harness.check_tool_call(tool_name, arguments)
        if not runtime_decision.allowed:
            return RuntimePreparedToolExecution(
                arguments=arguments,
                harness=harness,
                blocked_result=runtime_decision.to_tool_result(),
            )

    blocked_chrome_web = runtime_blocked_generic_web_for_chrome_local_browser(
        tool_name=tool_name,
        active_user_message=active_user_message,
    )
    if blocked_chrome_web:
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_chrome_web)
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=blocked_chrome_web,
        )

    blocked_chrome_open = runtime_blocked_chrome_open_shortcut(
        tool_name=tool_name,
        arguments=arguments,
        active_user_message=active_user_message,
    )
    if blocked_chrome_open:
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_chrome_open)
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=blocked_chrome_open,
        )

    blocked_chrome_action = runtime_blocked_chrome_action_shortcut(
        tool_name=tool_name,
        arguments=arguments,
        active_user_message=active_user_message,
    )
    if blocked_chrome_action:
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_chrome_action)
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=blocked_chrome_action,
        )

    if tool_name.startswith("mcp__"):
        blocked_mcp = await runtime_blocked_mcp_call_result(
            name=tool_name,
            entity_id=entity,
            user_id=user,
        )
        if blocked_mcp:
            if harness is not None:
                harness.record_tool_block_result(tool_name, blocked_mcp)
            return RuntimePreparedToolExecution(
                arguments=arguments,
                harness=harness,
                blocked_result=blocked_mcp,
            )

    policy_arguments = _runtime_policy_arguments(
        tool_name,
        arguments,
        active_user_message=active_user_message,
    )
    if harness is not None:
        policy_blocked = await harness.guard_tool_action(
            tool_name=tool_name,
            arguments=policy_arguments,
            entity_id=entity,
            user_id=user,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
        )
    else:
        policy_blocked = await RuntimeApprovalMiddleware().guard_tool_action(
            tool_name=tool_name,
            arguments=policy_arguments,
            entity_id=entity,
            user_id=user,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
        )
    if policy_blocked:
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=policy_blocked,
        )

    prepared_arguments = dict(arguments)
    if tool_name.startswith("mcp__"):
        # Runtime approvals are Manor-only control data. Do not leak the token
        # into third-party MCP payloads after the gate has consumed it.
        prepared_arguments.pop("approval_token", None)

    inject_runtime_context = tool_name in RUNTIME_ENVELOPE_AWARE_TOOLS
    injected_runtime_envelope = runtime_envelope if inject_runtime_context else None
    prepared_arguments.update(
        runtime_injected_tool_context_args(
            agent_id=agent_id,
            user_id=user_id,
            active_user_message=active_user_message,
            runtime_artifact_urls=runtime_artifact_urls,
            dependency_artifact_urls=dependency_artifact_urls,
            manual_skill_selected=manual_skill_selected,
            manual_skill_slugs=manual_skill_slugs,
            legacy_tool_profile=legacy_tool_profile,
            runtime_envelope=injected_runtime_envelope,
            allowed_tool_names=allowed_tool_names,
            llm_metadata=llm_metadata if inject_runtime_context else None,
            llm_model=llm_model if inject_runtime_context else None,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
        )
    )
    return RuntimePreparedToolExecution(
        arguments=prepared_arguments,
        harness=harness,
    )


async def runtime_execute_registered_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    handler_resolver: Callable[[str], Callable[..., Any] | None],
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    runtime_artifact_urls: Iterable[str] | None = None,
    dependency_artifact_urls: Iterable[str] | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    manual_skill_slugs: list[str] | None = None,
    legacy_tool_profile: str | None = None,
    allowed_tool_names: set[str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
    llm_model: str | None = None,
    runtime_envelope: Any | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """Execute a registered tool through the Runtime Harness boundary."""

    handler = handler_resolver(tool_name)
    if handler is None:
        return f"Error: unknown tool '{tool_name}'"

    prepared = await runtime_prepare_tool_execution(
        tool_name=tool_name,
        arguments=arguments,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        runtime_artifact_urls=runtime_artifact_urls,
        dependency_artifact_urls=dependency_artifact_urls,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
        manual_skill_slugs=manual_skill_slugs,
        legacy_tool_profile=legacy_tool_profile,
        allowed_tool_names=allowed_tool_names,
        llm_metadata=llm_metadata,
        llm_model=llm_model,
        runtime_envelope=runtime_envelope,
    )
    if prepared.blocked_result is not None:
        return prepared.blocked_result

    return await runtime_execute_prepared_tool_handler(
        tool_name=tool_name,
        handler=handler,
        prepared=prepared,
        entity_id=entity_id,
        user_id=user_id,
        logger=logger,
    )
