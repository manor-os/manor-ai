from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterable
import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPAccountRegistrySnapshot,
    )
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationRegistry,
    )

from packages.core.ai.runtime.approvals import (
    RuntimeApprovalMiddleware,
    RuntimeApprovalRequest,
)
from packages.core.ai.runtime.authorization_receipts import (
    RuntimeToolAuthorizationReceipt,
    RuntimeToolAuthorizationReceiptFactory,
    RuntimeToolAuthorizationLease,
    RuntimeToolAuthorizationLeaseFactory,
    runtime_begin_tool_authorization_scope,
    runtime_end_tool_authorization_scope,
    RuntimeAuthorizationScopeFailure,
)
from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrant
from packages.core.ai.runtime.dynamic_mcp import runtime_dynamic_mcp_result_is_stale
from packages.core.ai.runtime.streams import runtime_tool_error_result
from packages.core.ai.runtime.harness import RuntimeHarness
from packages.core.ai.runtime.chrome_routing import (
    runtime_blocked_chrome_action_shortcut,
    runtime_blocked_chrome_open_shortcut,
    runtime_blocked_chrome_upload_knowledge_source,
    runtime_blocked_chrome_workflow_contract,
    runtime_blocked_generic_web_for_chrome_local_browser,
    runtime_record_chrome_tool_result,
    runtime_record_chrome_workflow_tool,
)
from packages.core.ai.runtime.tool_availability import runtime_preflight_mcp_call
from packages.core.ai.runtime.tool_context import (
    RuntimeToolCallContext,
    RuntimeToolContextConflictError,
    runtime_injected_tool_context_args,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.constants.integrations import (
    RUNTIME_MCP_ACCOUNT_REGISTRY_SNAPSHOT_ARGUMENT,
    RUNTIME_MCP_INTEGRATION_REGISTRY_ARGUMENT,
)


RUNTIME_LLM_METADATA_AWARE_TOOLS = frozenset(
    {
        "invoke_skill",
        "list_skills",
        "get_skill_details",
        "manor",
        "rag",
        "workspace_search",
        "workspace_create_task",
        "start_workspace_flow",
        "run_workflow",
    }
)

RUNTIME_ENVELOPE_AWARE_TOOLS = RUNTIME_LLM_METADATA_AWARE_TOOLS | frozenset(
    {
        "search_tools",
        "inspect_file_engine",
        "write_file",
        "edit_file",
        "patch_file",
        "delete_file",
        "generate_file",
        "generate_document_file",
        "save_sandbox_file",
        "sandbox_save_result",
        "bash",
        "record_content_ledger",
        "record_finance_ledger",
        "record_recruiting_ledger",
        "record_relationship_ledger",
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
class RuntimeToolResolutionPreflight:
    """Context and policy proof required before resolving a live handler."""

    context: RuntimeToolCallContext | None = None
    harness: RuntimeHarness | None = None
    blocked_result: str | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_result is not None


def runtime_preflight_tool_resolution(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    runtime_envelope: Any | None = None,
) -> RuntimeToolResolutionPreflight:
    """Fail closed before a handler lookup can access actor credentials."""

    harness = RuntimeHarness(runtime_envelope) if runtime_envelope is not None else None
    try:
        context = runtime_tool_call_context_from_kwargs(
            arguments,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            runtime_envelope=runtime_envelope,
        )
    except RuntimeToolContextConflictError as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        return RuntimeToolResolutionPreflight(
            harness=harness,
            blocked_result=runtime_tool_error_result(str(exc)),
        )

    if harness is not None:
        runtime_decision = harness.check_tool_call(tool_name, arguments)
        if not runtime_decision.allowed:
            return RuntimeToolResolutionPreflight(
                context=context,
                harness=harness,
                blocked_result=runtime_decision.to_tool_result(),
            )

    return RuntimeToolResolutionPreflight(context=context, harness=harness)


@dataclass(frozen=True)
class RuntimePreparedToolExecution:
    arguments: dict[str, Any]
    harness: RuntimeHarness | None = None
    blocked_result: str | None = None
    authorization_lease: RuntimeToolAuthorizationLease | None = None
    mcp_integration_registry: RuntimeIntegrationRegistry | None = None
    mcp_account_registry_snapshot: (
        RuntimeDynamicMCPAccountRegistrySnapshot | None
    ) = None

    @property
    def blocked(self) -> bool:
        return self.blocked_result is not None

    @property
    def authorization_receipt(self) -> RuntimeToolAuthorizationReceipt | None:
        return (
            self.authorization_lease.receipt
            if self.authorization_lease is not None
            else None
        )


class RuntimeToolHandlerInvocationFactory:
    """Build one signature-compatible invocation without exception retries."""

    @staticmethod
    def keyword_arguments(
        handler: Callable[..., Any],
        *,
        arguments: dict[str, Any],
        entity_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        try:
            parameters = inspect.signature(handler).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_extra = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        invocation = dict(arguments)
        if accepts_extra or "entity_id" in parameters or not parameters:
            invocation["entity_id"] = entity_id
        if accepts_extra or "user_id" in parameters or not parameters:
            invocation["user_id"] = user_id
        return invocation


def _authorization_scope_error_message(
    failure: RuntimeAuthorizationScopeFailure | None,
) -> str:
    if failure is RuntimeAuthorizationScopeFailure.ARGUMENTS_MISMATCH:
        return "Runtime tool arguments changed after approval."
    return "Runtime tool authorization lease was already consumed."


def _record_successful_tool_workflow_step(
    *,
    harness: RuntimeHarness | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    if harness is None:
        return
    runtime_record_chrome_workflow_tool(
        tool_name=tool_name,
        arguments=arguments,
        runtime_metadata=harness.envelope.metadata,
    )


def _record_tool_result_for_workflow_recovery(
    *,
    harness: RuntimeHarness | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
) -> None:
    if harness is None:
        return
    runtime_record_chrome_tool_result(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        runtime_metadata=harness.envelope.metadata,
    )


async def runtime_execute_prepared_tool_handler(
    *,
    tool_name: str,
    handler: Callable[..., Any],
    prepared: RuntimePreparedToolExecution,
    entity_id: str | None = None,
    user_id: str | None = None,
    logger: logging.Logger | None = None,
) -> Any:
    """Call a prepared tool handler and record standard runtime events."""
    harness = prepared.harness
    arguments = prepared.arguments
    try:
        context = runtime_tool_call_context_from_kwargs(
            arguments,
            entity_id=entity_id,
            user_id=user_id,
            runtime_envelope=(harness.envelope if harness is not None else None),
        )
    except RuntimeToolContextConflictError as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        if logger is not None:
            logger.error("Tool %s failed: %s", tool_name, exc)
        return runtime_tool_error_result(str(exc))
    entity = context.entity_id or ""
    user = context.user_id or ""
    if harness is not None:
        harness.record_event("tool_start", tool_name=tool_name)
    authorization_token = runtime_begin_tool_authorization_scope(
        prepared.authorization_lease,
        arguments=arguments,
    )
    if not authorization_token.valid:
        runtime_end_tool_authorization_scope(authorization_token)
        message = _authorization_scope_error_message(authorization_token.failure)
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=message)
        return runtime_tool_error_result(message)
    try:
        invocation = RuntimeToolHandlerInvocationFactory.keyword_arguments(
            handler,
            arguments=arguments,
            entity_id=entity,
            user_id=user,
        )
        if (
            tool_name.startswith("mcp__")
            and prepared.mcp_integration_registry is not None
        ):
            invocation[RUNTIME_MCP_INTEGRATION_REGISTRY_ARGUMENT] = (
                prepared.mcp_integration_registry
            )
        if (
            tool_name.startswith("mcp__")
            and prepared.mcp_account_registry_snapshot is not None
        ):
            invocation[RUNTIME_MCP_ACCOUNT_REGISTRY_SNAPSHOT_ARGUMENT] = (
                prepared.mcp_account_registry_snapshot
            )
        result = handler(**invocation)
        if hasattr(result, "__await__"):
            result = await result
        from packages.core.ai.runtime.control import is_runtime_tool_suspension

        if is_runtime_tool_suspension(result):
            if harness is not None:
                harness.record_event("tool_suspended", tool_name=tool_name)
            return result
        if harness is not None:
            harness.record_event("tool_end", tool_name=tool_name)
        _record_successful_tool_workflow_step(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
        )
        _record_tool_result_for_workflow_recovery(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
        )
        return result if isinstance(result, str) else str(result)
    except Exception as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        if logger is not None:
            logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
        return runtime_tool_error_result(str(exc))
    finally:
        runtime_end_tool_authorization_scope(authorization_token)


async def runtime_execute_scoped_dynamic_tool_handler(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    handler: Callable[[dict[str, Any]], Any],
    runtime_envelope: Any | None = None,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    allowed_tool_names: set[str] | None = None,
) -> str:
    """Execute a runtime-scoped dynamic handler outside the global registry.

    Some entrypoints add per-run tools whose handlers are closures rather than
    registered ToolPool handlers. They still traverse the same hard
    authorization, governance, and HITL boundary as registered tools, but do
    not receive hidden registry context kwargs.
    """

    def envelope_value(name: str, explicit: str | None) -> str | None:
        if explicit is not None:
            return explicit
        return getattr(runtime_envelope, name, None) if runtime_envelope is not None else None

    prepared = await runtime_prepare_tool_execution(
        tool_name=tool_name,
        arguments=arguments,
        entity_id=envelope_value("entity_id", entity_id),
        user_id=envelope_value("user_id", user_id),
        agent_id=envelope_value("agent_id", agent_id),
        workspace_id=envelope_value("workspace_id", workspace_id),
        conversation_id=envelope_value("conversation_id", conversation_id),
        task_id=envelope_value("task_id", task_id),
        active_user_message=active_user_message,
        allowed_tool_names=(
            allowed_tool_names
            if allowed_tool_names is not None
            else set(getattr(runtime_envelope, "allowed_tool_names", ()) or ())
        ),
        runtime_envelope=runtime_envelope,
        inject_runtime_context=False,
    )
    if prepared.blocked_result is not None:
        return prepared.blocked_result

    harness = prepared.harness
    prepared_arguments = prepared.arguments
    if harness is not None:
        harness.record_event("tool_start", tool_name=tool_name)
    authorization_token = runtime_begin_tool_authorization_scope(
        prepared.authorization_lease,
        arguments=prepared_arguments,
    )
    if not authorization_token.valid:
        runtime_end_tool_authorization_scope(authorization_token)
        message = _authorization_scope_error_message(authorization_token.failure)
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=message)
        return runtime_tool_error_result(message)
    try:
        result = handler(prepared_arguments)
        if hasattr(result, "__await__"):
            result = await result
        if harness is not None:
            harness.record_event("tool_end", tool_name=tool_name)
        return result if isinstance(result, str) else str(result)
    except Exception as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        raise
    finally:
        runtime_end_tool_authorization_scope(authorization_token)


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
    manual_skill_ids: list[str] | None = None,
    manual_skill_slugs: list[str] | None = None,
    tool_profile: str | None = None,
    allowed_tool_names: set[str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
    llm_model: str | None = None,
    runtime_envelope: Any | None = None,
    discovered_tool_grant: RuntimeDiscoveredToolGrant | None = None,
    inject_runtime_context: bool = True,
) -> RuntimePreparedToolExecution:
    """Apply Runtime Harness gates and inject hidden tool context."""
    resolution_preflight = runtime_preflight_tool_resolution(
        tool_name=tool_name,
        arguments=arguments,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        runtime_envelope=runtime_envelope,
    )
    harness = resolution_preflight.harness
    if resolution_preflight.blocked_result is not None:
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=resolution_preflight.blocked_result,
        )
    context = resolution_preflight.context
    if context is None:
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=runtime_tool_error_result(
                "Runtime tool context could not be resolved."
            ),
        )
    entity = context.entity_id or ""
    user = context.user_id or ""

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

    blocked_chrome_upload = runtime_blocked_chrome_upload_knowledge_source(
        tool_name=tool_name,
        arguments=arguments,
    )
    if blocked_chrome_upload:
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_chrome_upload)
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=blocked_chrome_upload,
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

    blocked_chrome_workflow = runtime_blocked_chrome_workflow_contract(
        tool_name=tool_name,
        arguments=arguments,
        runtime_metadata=(runtime_envelope.metadata if runtime_envelope is not None else None),
        active_user_message=active_user_message,
    )
    if blocked_chrome_workflow:
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_chrome_workflow)
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=blocked_chrome_workflow,
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

    resolved_arguments = dict(arguments)
    mcp_integration_registry = None
    mcp_account_registry_snapshot = None
    if tool_name.startswith("mcp__"):
        discovered_grants = getattr(
            runtime_envelope,
            "discovered_tool_grants",
            None,
        )
        discovered_tool = discovered_tool_grant or (
            discovered_grants.metadata(tool_name)
            if discovered_grants is not None
            else None
        )
        mcp_preflight = await runtime_preflight_mcp_call(
            name=tool_name,
            entity_id=entity,
            user_id=user,
            arguments=arguments,
            declared_effect=(
                discovered_tool.effect if discovered_tool is not None else None
            ),
            allowed_account_ids=(
                discovered_tool.account_ids if discovered_tool is not None else None
            ),
            requires_explicit_account=bool(
                discovered_tool and discovered_tool.requires_explicit_account
            ),
            supports_all_accounts=(
                discovered_tool.supports_all_accounts
                if discovered_tool is not None
                else None
            ),
            expected_account_registry_snapshot=(
                discovered_tool.account_registry_snapshot
                if discovered_tool is not None
                else None
            ),
        )
        blocked_mcp = mcp_preflight.blocked_result
        if blocked_mcp:
            if harness is not None and not runtime_dynamic_mcp_result_is_stale(
                blocked_mcp
            ):
                harness.record_tool_block_result(tool_name, blocked_mcp)
            return RuntimePreparedToolExecution(
                arguments=arguments,
                harness=harness,
                blocked_result=blocked_mcp,
            )
        if mcp_preflight.integration_account_id:
            resolved_arguments["integration_account_id"] = (
                mcp_preflight.integration_account_id
            )
        mcp_integration_registry = getattr(
            mcp_preflight,
            "integration_registry",
            None,
        )
        mcp_account_registry_snapshot = (
            discovered_tool.account_registry_snapshot
            if discovered_tool is not None
            else None
        )

    policy_arguments = _runtime_policy_arguments(
        tool_name,
        resolved_arguments,
        active_user_message=active_user_message,
    )
    approval_middleware = (
        harness.approval_middleware
        if harness is not None
        else RuntimeApprovalMiddleware()
    )
    approval_request = RuntimeApprovalRequest(
        tool_name=tool_name,
        arguments=policy_arguments,
        entity_id=entity,
        user_id=user,
        workspace_id=context.workspace_id,
        conversation_id=context.conversation_id,
        task_id=context.task_id,
        envelope=(harness.envelope if harness is not None else runtime_envelope),
        declared_effect=(
            discovered_tool_grant.effect
            if discovered_tool_grant is not None
            else None
        ),
    )
    if harness is not None:
        approval_decision = await harness.guard_tool_request(approval_request)
    else:
        approval_decision = await approval_middleware.guard_request(approval_request)
    policy_blocked = approval_decision.blocked_result
    if policy_blocked:
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=policy_blocked,
        )

    authorization_receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=approval_decision,
        request=approval_request,
    )

    prepared_arguments = dict(resolved_arguments)
    if tool_name.startswith("mcp__") or not inject_runtime_context:
        # Runtime approvals are Manor-only control data. Do not leak the token
        # into third-party MCP payloads after the gate has consumed it.
        prepared_arguments.pop("approval_token", None)

    if inject_runtime_context:
        envelope_aware = tool_name in RUNTIME_ENVELOPE_AWARE_TOOLS
        llm_metadata_aware = tool_name in RUNTIME_LLM_METADATA_AWARE_TOOLS
        injected_runtime_envelope = runtime_envelope if envelope_aware else None
        prepared_arguments.update(
            runtime_injected_tool_context_args(
                agent_id=context.agent_id,
                user_id=context.user_id,
                active_user_message=active_user_message,
                runtime_artifact_urls=runtime_artifact_urls,
                dependency_artifact_urls=dependency_artifact_urls,
                manual_skill_selected=manual_skill_selected,
                manual_skill_ids=manual_skill_ids,
                manual_skill_slugs=manual_skill_slugs,
                tool_profile=tool_profile,
                runtime_envelope=injected_runtime_envelope,
                allowed_tool_names=allowed_tool_names,
                llm_metadata=llm_metadata if llm_metadata_aware else None,
                llm_model=llm_model if llm_metadata_aware else None,
                workspace_id=context.workspace_id,
                conversation_id=context.conversation_id,
                task_id=context.task_id,
            )
        )
    return RuntimePreparedToolExecution(
        arguments=prepared_arguments,
        harness=harness,
        authorization_lease=RuntimeToolAuthorizationLeaseFactory.create(
            authorization_receipt
        ),
        mcp_integration_registry=mcp_integration_registry,
        mcp_account_registry_snapshot=mcp_account_registry_snapshot,
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
    step_id: str | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    manual_skill_ids: list[str] | None = None,
    manual_skill_slugs: list[str] | None = None,
    tool_profile: str | None = None,
    allowed_tool_names: set[str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
    llm_model: str | None = None,
    runtime_envelope: Any | None = None,
    discovered_tool_grant: RuntimeDiscoveredToolGrant | None = None,
    logger: logging.Logger | None = None,
) -> Any:
    """Execute a registered tool through the Runtime Harness boundary."""

    # Planned-step callers already provide this identifier. Runtime approval
    # origins currently use the task/conversation envelope, so keep the
    # argument at this boundary without leaking it into tool payloads until
    # step-scoped approval records are wired through end to end.
    del step_id

    handler = handler_resolver(tool_name)
    if handler is None:
        return runtime_tool_error_result(f"unknown tool '{tool_name}'")

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
        manual_skill_ids=manual_skill_ids,
        manual_skill_slugs=manual_skill_slugs,
        tool_profile=tool_profile,
        allowed_tool_names=allowed_tool_names,
        llm_metadata=llm_metadata,
        llm_model=llm_model,
        runtime_envelope=runtime_envelope,
        discovered_tool_grant=discovered_tool_grant,
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
