from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from collections.abc import Callable, Iterable
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPAccountRegistrySnapshot,
    )
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationRegistry,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

from packages.core.ai.runtime.approvals import (
    RuntimeApprovalMiddleware,
    RuntimeApprovalRequest,
)
from packages.core.ai.runtime.capabilities import AGENT_INITIATIVE_TOOL_NAMES
from packages.core.ai.runtime.authorization_receipts import (
    RuntimeToolAuthorizationReceipt,
    RuntimeToolAuthorizationReceiptFactory,
    RuntimeToolAuthorizationLease,
    RuntimeToolAuthorizationLeaseFactory,
    runtime_begin_tool_authorization_scope,
    runtime_workflow_receipt_is_executable,
    runtime_end_tool_authorization_scope,
    runtime_capture_tiktok_publish_consent,
    runtime_capture_chrome_publication_binding,
    RuntimeAuthorizationScopeFailure,
)
from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrant
from packages.core.ai.runtime.dynamic_mcp import runtime_dynamic_mcp_result_is_stale
from packages.core.ai.runtime.control import RuntimeToolSuspension, RuntimeTurnAborted
from packages.core.ai.runtime.streams import (
    runtime_tool_alternate_path_error_result,
    runtime_tool_error_result,
    runtime_tool_status_for_chat,
)
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
from packages.core.ai.runtime.chrome_fast_mode import (
    runtime_chrome_fast_interaction_mode,
    runtime_strip_model_chrome_interaction_mode,
)
from packages.core.ai.runtime.tool_availability import runtime_preflight_mcp_call
from packages.core.ai.runtime.tool_input_validation import (
    RuntimeToolInputValidationFailure,
    validate_runtime_tool_arguments,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    RuntimeToolCallContext,
    RuntimeToolContextConflictError,
    runtime_authorization_conversation_id,
    runtime_injected_tool_context_args,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.constants.integrations import (
    RUNTIME_MCP_ACCOUNT_REGISTRY_SNAPSHOT_ARGUMENT,
    RUNTIME_MCP_INTEGRATION_REGISTRY_ARGUMENT,
)
from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope


def _output_scope(context: RuntimeToolCallContext, tool_name: str, arguments: dict) -> WorkspaceArtifactScope:
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory

    call = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
    args = call.arguments
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    shared = (
        str(args.get("storage_scope") or "").lower() == "workspace"
        or bool(args.get("workspace_shared"))
        or bool(args.get("workspace_asset_key") or params.get("workspace_asset_key"))
    )
    return WorkspaceArtifactScope(
        context.entity_id, context.workspace_id,
        context.workflow_run_id, context.workflow_lineage_root_run_id, shared,
    )


RUNTIME_LLM_METADATA_AWARE_TOOLS = frozenset(
    {
        "invoke_skill",
        "list_skills",
        "get_skill_details",
        "manor",
        "sandbox",
        "rag",
        "workspace_search",
        "workspace_create_task",
        "start_workspace_flow",
        "run_workflow",
    }
)

RUNTIME_ENVELOPE_AWARE_TOOLS = (
    RUNTIME_LLM_METADATA_AWARE_TOOLS
    | frozenset(AGENT_INITIATIVE_TOOL_NAMES)
    | frozenset(
        {
            "mcp__chrome__run_sequence",
            "check_youtube_publication_setup",
            "search_tools",
            "resident_maintenance_status",
            "resident_maintenance_draft",
            "website_content",
            "workspace_agent",
            "workspace_request_strategist_review",
            "update_task",
            "create_scheduled_job",
            "list_scheduled_jobs",
            "query_scheduled_jobs",
            "cancel_scheduled_job",
            "toggle_scheduled_job",
            "run_scheduled_job_now",
            "render_response_surface",
            "inspect_file_engine",
            "write_file",
            "edit_file",
            "patch_file",
            "replace_file",
            "delete_file",
            "generate_file",
            "generate_document_file",
            "save_sandbox_file",
            "sandbox_save_result",
            "sandbox",
            "bash",
            "record_content_ledger",
            "record_finance_ledger",
            "record_recruiting_ledger",
            "record_relationship_ledger",
        }
    )
)


def _runtime_public_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Strip Runtime-only context before hashing a model-visible tool call."""

    return {
        key: value
        for key, value in arguments.items()
        if key not in RUNTIME_TOOL_CONTEXT_KEYS
    }

_RUNTIME_TOOL_EVIDENCE_FIELDS = (
    "kind",
    "duration_seconds",
    "fs_path",
    "result_url",
    "video_url",
    "file_url",
    "document_id",
    "name",
    "source_sha256",
    "resolved_path",
    "path",
)


def _tool_start_event_data(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"tool_name": str(tool_name or "")}
    if tool_name == "invoke_skill":
        args = arguments if isinstance(arguments, dict) else {}
        skill_ref = str(args.get("skill_id") or args.get("skill") or "").strip()
        if skill_ref:
            data["skill_ref"] = skill_ref
    return data


def _tool_end_event_data(
    tool_name: str,
    result: Any,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _tool_start_event_data(tool_name, arguments)
    payload = result if isinstance(result, dict) else None
    if payload is None and isinstance(result, str):
        try:
            decoded = json.loads(result)
        except (TypeError, ValueError):
            decoded = None
        payload = decoded if isinstance(decoded, dict) else None

    from packages.core.ai.runtime.events import RuntimeToolResultStatus

    status = (
        RuntimeToolResultStatus.FAILED
        if runtime_tool_status_for_chat(result) == "error"
        else RuntimeToolResultStatus.COMPLETED
    )
    data["status"] = status.value
    if payload:
        for field in _RUNTIME_TOOL_EVIDENCE_FIELDS:
            if payload.get(field) is not None:
                data[field] = payload[field]
        if tool_name == "generate_file" and isinstance(payload.get("document"), dict):
            for field in ("document_id", "fs_path", "source_sha256"):
                if payload["document"].get(field) is not None:
                    data[field] = payload["document"][field]
    return data


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




def _runtime_workspace_task_scope_conflicts(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    task_id: str | None,
) -> bool:
    runtime_task_id = str(task_id or "").strip()
    if not runtime_task_id:
        return False

    from packages.core.ai.runtime.composite_tools import (
        RuntimeCompositeToolCallFactory,
        WorkspaceToolAction,
    )

    call = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
    if call.tool_name != "workspace_agent":
        return False
    if str(call.arguments.get("action") or "").strip() != WorkspaceToolAction.UPDATE_TASK_RUNTIME:
        return False
    raw_params = call.arguments.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    requested_task_id = str(params.get("task_id") or "").strip()
    return bool(requested_task_id and requested_task_id != runtime_task_id)


def _runtime_workspace_action_preflight_error(
    tool_name: str,
    arguments: dict[str, Any],
) -> RuntimeToolInputValidationFailure | None:
    """Validate a composite Workspace call before approval or effect intent.

    Manor exposes a flexible ``params`` object, so the public JSON schema
    cannot validate its nested Workspace action enum. A handler-level error
    arrives too late for the serial agent loop to prove that no write occurred.
    """
    if tool_name not in {"manor", "workspace_agent", "workspace_resolve_hitl"}:
        return None

    from packages.core.ai.runtime.composite_tools import (
        RuntimeCompositeToolCallFactory,
        WorkspaceToolAction,
    )

    call = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
    if call.tool_name not in {"workspace_agent", "workspace_resolve_hitl"}:
        return None
    action = str(call.arguments.get("action") or "").strip()
    if call.tool_name == "workspace_resolve_hitl" or action == WorkspaceToolAction.RESOLVE_HITL:
        from packages.core.ai.tools.workspace_agent_tools import WORKSPACE_RESOLVE_HITL_SCHEMA

        if call.tool_name == "workspace_resolve_hitl":
            params, path = call.arguments, "$"
        else:
            params = call.arguments.get("params")
            params = params if isinstance(params, dict) else {}
            path = "$.params.params" if tool_name == "manor" else "$.params"
        failure = validate_runtime_tool_arguments(
            arguments=params, tool_schema=WORKSPACE_RESOLVE_HITL_SCHEMA,
        )
        if failure is not None:
            return replace(
                failure,
                path=path + failure.path[1:],
                message=failure.message.replace(failure.path, path + failure.path[1:]),
            )
        if params["action"] == "respond":
            payload = params.get("payload") or {}

            def has_answer(value: Any) -> bool:
                if isinstance(value, str):
                    return bool(value.strip())
                if isinstance(value, dict):
                    return any(has_answer(item) for item in value.values())
                if isinstance(value, list):
                    return any(has_answer(item) for item in value)
                return value is not None

            has_attachment = any(
                str(item.get("id") or item.get("document_id") or "").strip()
                for item in payload.get("attachments", [])
            )
            if not (
                has_answer(params.get("response"))
                or has_answer(payload.get("fields"))
                or has_answer(payload.get("answers"))
                or has_attachment
            ):
                return RuntimeToolInputValidationFailure(
                    code="tool_input_validation_failed",
                    message=(
                        "Workspace resolve_hitl action='respond' requires a non-empty "
                        f"reply in {path}.response, structured payload.fields/answers, "
                        "or payload.attachments with an id/document_id. "
                        "note is only an approval explanation."
                    ),
                    path=f"{path}.response",
                    schema_rule="required",
                )
        return None
    if action == WorkspaceToolAction.CREATE_TASK:
        params = call.arguments.get("params")
        title = params.get("title") if isinstance(params, dict) else None
        path = "$.params.params.title" if tool_name == "manor" else "$.params.title"
        if not isinstance(title, str) or not title.strip():
            return RuntimeToolInputValidationFailure(
                code="tool_input_validation_failed",
                message=(
                    f"Workspace create_task requires a non-empty string at {path}. "
                    "name is not a title field."
                ),
                path=path,
                schema_rule="type" if title is not None and not isinstance(title, str) else "required",
                expected="non-empty string",
            )
    if action in WorkspaceToolAction.values():
        return None
    return RuntimeToolInputValidationFailure(
        code="tool_input_validation_failed",
        message="Unsupported Workspace action.",
        path="$.params.action" if tool_name == "manor" else "$.action",
        schema_rule="enum",
        expected=sorted(WorkspaceToolAction.values()),
    )


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
        if _runtime_workspace_task_scope_conflicts(
            tool_name,
            arguments,
            task_id=context.task_id,
        ):
            raise RuntimeToolContextConflictError(
                "Conflicting runtime scope for task_id."
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
    artifact_scope: WorkspaceArtifactScope | None = None
    provider_preflight_arguments: dict[str, Any] | None = None
    harness: RuntimeHarness | None = None
    blocked_result: str | None = None
    authorization_lease: RuntimeToolAuthorizationLease | None = None
    classification: "RuntimeToolClassification | None" = None
    consumed_approval_request_id: str | None = None
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
) -> Any:
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


def _provider_approval_execution_for_tool(
    harness: RuntimeHarness | None,
    tool_name: str,
) -> dict[str, Any] | None:
    if harness is None:
        return None
    execution = harness.envelope.metadata.get("provider_approval_execution")
    if not isinstance(execution, dict) or tool_name not in {
        str(execution.get("confirmation_tool") or "").strip(),
        str(execution.get("retry_tool") or "").strip(),
    }:
        return None
    return execution


async def _persist_provider_approval_result(
    *,
    harness: RuntimeHarness,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    entity_id: str,
    conversation_id: str | None,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        settle_provider_runtime_approval_execution,
    )
    from packages.core.database import async_session

    async with async_session() as db:
        changed = await settle_provider_runtime_approval_execution(
            db,
            runtime_metadata=harness.envelope.metadata,
            entity_id=entity_id,
            conversation_id=conversation_id,
            task_id=harness.envelope.task_id,
            runtime_envelope=harness.envelope,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
        )
        if changed:
            await db.commit()


async def runtime_settle_provider_approval_preflight_failure(
    *,
    harness: RuntimeHarness | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    entity_id: str,
    conversation_id: str | None,
) -> Any:
    """Durably close a provider grant when execution stops before provider I/O.

    The returned result is tagged as safe for a different tool path when this
    call owns a provider approval.  Callers must return that value rather than
    the untagged input so the agent loop can continue without replaying the
    approved action.
    """

    execution = _provider_approval_execution_for_tool(harness, tool_name)
    if execution is None or harness is None:
        return result

    from packages.core.ai.runtime.control import is_runtime_tool_suspension

    if is_runtime_tool_suspension(result):
        return result
    # No provider effect has happened yet.  Settlement is part of the
    # fail-closed boundary, so persistence errors must abort the continuation
    # instead of returning a terminal tool result with a live grant.
    try:
        await _persist_provider_approval_result(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            entity_id=entity_id,
            conversation_id=conversation_id,
        )
    except Exception as settlement_error:
        raise RuntimeTurnAborted(
            "The approved provider action could not be durably settled; "
            "use another available path and do not repeat this approval.",
            allow_alternate_path=True,
        ) from settlement_error
    return runtime_tool_alternate_path_error_result(result)


async def runtime_settle_provider_approval_result(
    *,
    harness: RuntimeHarness | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    entity_id: str,
    conversation_id: str | None,
) -> None:
    """Best-effort settlement after a provider result may already exist."""

    execution = _provider_approval_execution_for_tool(harness, tool_name)
    if execution is None or harness is None:
        return

    from packages.core.ai.runtime.control import is_runtime_tool_suspension

    if is_runtime_tool_suspension(result):
        return
    try:
        await _persist_provider_approval_result(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            entity_id=entity_id,
            conversation_id=conversation_id,
        )
    except Exception:
        # The provider result has already happened.  Never turn a successful
        # external action into an apparent failure (and invite a duplicate)
        # merely because audit persistence was temporarily unavailable.
        logging.getLogger(__name__).exception(
            "Failed to settle provider approval %s after %s",
            execution.get("hitl_id"),
            tool_name,
        )


async def _prepare_provider_approval_call(
    *,
    harness: RuntimeHarness | None,
    tool_name: str,
    arguments: dict[str, Any],
    entity_id: str,
    conversation_id: str | None,
    task_id: str | None,
    comparison_arguments: dict[str, Any] | None = None,
) -> str | None:
    """Claim provider execution before I/O or replay a durable confirmation."""

    if harness is None:
        return None
    execution = harness.envelope.metadata.get("provider_approval_execution")
    if not isinstance(execution, dict) or tool_name not in {
        str(execution.get("confirmation_tool") or "").strip(),
        str(execution.get("retry_tool") or "").strip(),
    }:
        return None
    try:
        from packages.core.ai.runtime.approval_service import (
            prepare_provider_runtime_approval_execution,
        )
        from packages.core.database import async_session

        async with async_session() as db:
            preflight = await prepare_provider_runtime_approval_execution(
                db,
                runtime_metadata=harness.envelope.metadata,
                entity_id=entity_id,
                conversation_id=conversation_id,
                task_id=task_id,
                runtime_envelope=harness.envelope,
                tool_name=tool_name,
                arguments=(
                    comparison_arguments
                    if comparison_arguments is not None
                    else arguments
                ),
            )
            if preflight.changed:
                await db.commit()
            if (
                preflight.result is not None
                and runtime_tool_status_for_chat(str(preflight.result)) == "error"
            ):
                return runtime_tool_alternate_path_error_result(preflight.result)
            return preflight.result
    except Exception:
        # This is still before provider I/O. Fail closed unless the execution
        # claim or reusable confirmation receipt is durably readable/writable.
        logging.getLogger(__name__).exception(
            "Failed to prepare provider approval %s before %s",
            execution.get("hitl_id"),
            tool_name,
        )
        error_result = runtime_tool_alternate_path_error_result(
            "The approved provider action could not be durably claimed. "
            "No external action was executed."
        )
        try:
            await _persist_provider_approval_result(
                harness=harness,
                tool_name=tool_name,
                arguments=arguments,
                result=error_result,
                entity_id=entity_id,
                conversation_id=conversation_id,
            )
        except Exception as settlement_error:
            raise RuntimeTurnAborted(
                "The approved provider action could not be durably recovered; "
                "use another available path and do not repeat this approval.",
                allow_alternate_path=True,
            ) from settlement_error
        return error_result


async def runtime_execute_prepared_tool_handler(
    *,
    tool_name: str,
    handler: Callable[..., Any],
    prepared: RuntimePreparedToolExecution,
    entity_id: str | None = None,
    user_id: str | None = None,
    public_arguments: dict[str, Any] | None = None,
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
        error_result = runtime_tool_error_result(str(exc))
        return await runtime_settle_provider_approval_preflight_failure(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=error_result,
            entity_id=(harness.envelope.entity_id or "") if harness else "",
            conversation_id=(harness.envelope.conversation_id if harness else None),
        )
    entity = context.entity_id or ""
    user = context.user_id or ""
    if harness is not None:
        harness.record_event("tool_start", **_tool_start_event_data(tool_name, arguments))
    authorization_token = runtime_begin_tool_authorization_scope(
        prepared.authorization_lease,
        arguments=arguments,
    )
    if not authorization_token.valid:
        runtime_end_tool_authorization_scope(authorization_token)
        message = _authorization_scope_error_message(authorization_token.failure)
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=message)
        error_result = runtime_tool_error_result(message)
        return await runtime_settle_provider_approval_preflight_failure(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=error_result,
            entity_id=entity,
            conversation_id=context.conversation_id,
        )
    try:
        if not await runtime_workflow_receipt_is_executable(prepared.authorization_receipt):
            blocked = json.dumps({"error": "workflow_origin_not_executable", "action_executed": False})
            if harness is not None:
                harness.record_tool_block_result(tool_name, blocked)
            return blocked
        provider_preflight_result = await _prepare_provider_approval_call(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            comparison_arguments=prepared.provider_preflight_arguments,
            entity_id=entity,
            conversation_id=context.conversation_id,
            task_id=context.task_id,
        )
        if provider_preflight_result is not None:
            if harness is not None:
                harness.record_event(
                    "tool_end",
                    **_tool_end_event_data(
                        tool_name,
                        provider_preflight_result,
                        arguments,
                    ),
                )
            return provider_preflight_result
        # This runs after authorization/approval preflight but before the
        # handler can cross external I/O.  Coordinator children install a
        # context-local observer that durably records mutating intent; all
        # other Runtime surfaces pay only this no-op lookup.
        from packages.core.ai.runtime.tool_effect_intents import (
            runtime_record_tool_effect_intent,
            runtime_record_tool_effect_result,
        )

        await runtime_record_tool_effect_intent(
            tool_name,
            _runtime_public_tool_arguments(
                public_arguments if public_arguments is not None else arguments
            ),
            str(arguments.get("_runtime_tool_call_id_from_context") or "") or None,
            prepared.classification,
            approval_request_id=prepared.consumed_approval_request_id,
        )
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
        with workspace_artifact_scope(prepared.artifact_scope or _output_scope(context, tool_name, arguments)):
            result = handler(**invocation)
            if hasattr(result, "__await__"):
                result = await result
        from packages.core.ai.runtime.control import is_runtime_tool_suspension

        if is_runtime_tool_suspension(result):
            if harness is not None:
                harness.record_event("tool_suspended", tool_name=tool_name)
            return result
        if harness is not None and tool_name.startswith("mcp__chrome__"):
            from packages.core.ai.runtime.chrome_action_outcomes import record_action_outcome

            result = record_action_outcome(
                tool_name=tool_name, arguments=arguments, result=result,
                state=harness.envelope.metadata.setdefault("chrome_runtime_contract_v1", {}),
            )
        receipt = prepared.authorization_receipt
        chrome_publication_grant_restored = False
        if (
            receipt is not None and receipt.chrome_publication_binding is not None
            and receipt.chrome_publication_binding.approval_id is not None
        ):
            from packages.core.ai.runtime.approval_service import restore_unexecuted_chrome_publication_approval
            from packages.core.database import async_session

            async with async_session() as db:
                if await restore_unexecuted_chrome_publication_approval(db, receipt=receipt, result=result):
                    await db.commit()
                    chrome_publication_grant_restored = True
        if (
            receipt is not None
            and receipt.chrome_publication_binding is not None
            and (
                receipt.chrome_publication_binding.approval_id is None
                or chrome_publication_grant_restored
            )
            and isinstance(result, str)
        ):
            from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult

            try:
                fresh_read = RuntimeNoEffectToolResult.chrome_publication_fresh_read(result)
            except (TypeError, ValueError):
                pass
            else:
                if fresh_read.matches_call(tool_name, arguments):
                    result = fresh_read
        # Persist the exact observed effect before another frame can discard
        # this result. Durability failures are RuntimeTurnAborted control flow,
        # not tool errors that the model may retry.
        await runtime_record_tool_effect_result(
            tool_name,
            _runtime_public_tool_arguments(
                public_arguments if public_arguments is not None else arguments
            ),
            result,
            str(arguments.get("_runtime_tool_call_id_from_context") or "") or None,
            prepared.classification,
        )
        await runtime_settle_provider_approval_result(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            entity_id=entity,
            conversation_id=context.conversation_id,
        )
        if harness is not None:
            harness.record_event(
                "tool_end",
                **_tool_end_event_data(tool_name, result, arguments),
            )
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
        if isinstance(result, RuntimeToolSuspension):
            return result
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, default=str)
        return result if isinstance(result, str) else str(result)
    except RuntimeTurnAborted:
        raise
    except Exception as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        if logger is not None:
            logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
        error_result = runtime_tool_error_result(str(exc))
        await runtime_settle_provider_approval_result(
            harness=harness,
            tool_name=tool_name,
            arguments=arguments,
            result=error_result,
            entity_id=entity,
            conversation_id=context.conversation_id,
        )
        return error_result
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
    step_id: str | None = None,
    active_user_message: str | None = None,
    allowed_tool_names: set[str] | None = None,
) -> str | RuntimeToolSuspension:
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
        step_id=step_id,
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
        envelope = (
            prepared.harness.envelope if prepared.harness is not None else None
        )
        return await runtime_settle_provider_approval_preflight_failure(
            harness=prepared.harness,
            tool_name=tool_name,
            arguments=arguments,
            result=prepared.blocked_result,
            entity_id=(
                envelope.entity_id or entity_id or ""
                if envelope
                else entity_id or ""
            ),
            conversation_id=(
                envelope.conversation_id if envelope is not None else conversation_id
            ),
        )

    harness = prepared.harness
    prepared_arguments = prepared.arguments
    if harness is not None:
        harness.record_event(
            "tool_start",
            **_tool_start_event_data(tool_name, prepared_arguments),
        )
    authorization_token = runtime_begin_tool_authorization_scope(
        prepared.authorization_lease,
        arguments=prepared_arguments,
    )
    if not authorization_token.valid:
        runtime_end_tool_authorization_scope(authorization_token)
        message = _authorization_scope_error_message(authorization_token.failure)
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=message)
        error_result = runtime_tool_error_result(message)
        return await runtime_settle_provider_approval_preflight_failure(
            harness=harness,
            tool_name=tool_name,
            arguments=prepared_arguments,
            result=error_result,
            entity_id=envelope_value("entity_id", entity_id) or "",
            conversation_id=envelope_value("conversation_id", conversation_id),
        )
    try:
        if not await runtime_workflow_receipt_is_executable(prepared.authorization_receipt):
            blocked = json.dumps({"error": "workflow_origin_not_executable", "action_executed": False})
            if harness is not None:
                harness.record_tool_block_result(tool_name, blocked)
            return blocked
        provider_preflight_result = await _prepare_provider_approval_call(
            harness=harness,
            tool_name=tool_name,
            arguments=prepared_arguments,
            comparison_arguments=prepared.provider_preflight_arguments,
            entity_id=envelope_value("entity_id", entity_id) or "",
            conversation_id=envelope_value("conversation_id", conversation_id),
            task_id=envelope_value("task_id", task_id),
        )
        if provider_preflight_result is not None:
            if harness is not None:
                harness.record_event(
                    "tool_end",
                    **_tool_end_event_data(
                        tool_name,
                        provider_preflight_result,
                        prepared_arguments,
                    ),
                )
            return provider_preflight_result
        from packages.core.ai.runtime.tool_effect_intents import (
            runtime_record_tool_effect_intent,
            runtime_record_tool_effect_result,
        )

        await runtime_record_tool_effect_intent(
            tool_name,
            _runtime_public_tool_arguments(arguments),
            str(
                prepared_arguments.get("_runtime_tool_call_id_from_context") or ""
            ) or None,
            prepared.classification,
            approval_request_id=prepared.consumed_approval_request_id,
        )
        with workspace_artifact_scope(prepared.artifact_scope):
            result = handler(prepared_arguments)
            if hasattr(result, "__await__"):
                result = await result
        if isinstance(result, RuntimeToolSuspension):
            # Match registered handlers: a host-owned wait is control flow,
            # not a successful provider result or a string for the model.
            if harness is not None:
                harness.record_event("tool_suspended", tool_name=tool_name)
            return result
        await runtime_record_tool_effect_result(
            tool_name,
            _runtime_public_tool_arguments(arguments),
            result,
            str(
                prepared_arguments.get("_runtime_tool_call_id_from_context") or ""
            ) or None,
            prepared.classification,
        )
        await runtime_settle_provider_approval_result(
            harness=harness,
            tool_name=tool_name,
            arguments=prepared_arguments,
            result=result,
            entity_id=envelope_value("entity_id", entity_id) or "",
            conversation_id=envelope_value("conversation_id", conversation_id),
        )
        if harness is not None:
            harness.record_event(
                "tool_end",
                **_tool_end_event_data(tool_name, result, prepared_arguments),
            )
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, default=str)
        return result if isinstance(result, str) else str(result)
    except RuntimeTurnAborted:
        raise
    except Exception as exc:
        if harness is not None:
            harness.record_event("error", tool_name=tool_name, message=str(exc))
        await runtime_settle_provider_approval_result(
            harness=harness,
            tool_name=tool_name,
            arguments=prepared_arguments,
            result=runtime_tool_error_result(str(exc)),
            entity_id=envelope_value("entity_id", entity_id) or "",
            conversation_id=envelope_value("conversation_id", conversation_id),
        )
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
    inject_runtime_context: bool = True,
    runtime_validation_duration_ms: float | None = None,
    chrome_sequence_child: bool = False,
) -> RuntimePreparedToolExecution:
    """Apply Runtime Harness gates and inject hidden tool context."""
    if tool_name.startswith("mcp__chrome__"):
        arguments = runtime_strip_model_chrome_interaction_mode(tool_name, arguments)
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

    from packages.core.ai.runtime.tool_gateway import CALL_TOOL_NAME

    rejected_reason = arguments.get("rejected_reason")
    if tool_name == CALL_TOOL_NAME and isinstance(rejected_reason, str) and rejected_reason.strip():
        from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

        blocked_result = RuntimePreflightValidationError(json.dumps(tool_error_payload(
            tool_name=CALL_TOOL_NAME,
            code="gateway_call_rejected",
            message=rejected_reason,
            details={"action_executed": False, "discovery_tool": "search_tools"},
        )))
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_result)
        return RuntimePreparedToolExecution(
            arguments=arguments, harness=harness, blocked_result=blocked_result,
        )

    workspace_action_error = _runtime_workspace_action_preflight_error(
        tool_name, arguments,
    )
    if workspace_action_error is not None:
        from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

        blocked_result = RuntimePreflightValidationError(
            workspace_action_error.to_tool_result(tool_name=tool_name)
        )
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_result)
        return RuntimePreparedToolExecution(
            arguments=arguments, harness=harness, blocked_result=blocked_result,
        )

    from packages.core.contracts.step_execution import (
        step_contract_tool_error, tool_operation_denied_error,
    )

    denied_operation = tool_operation_denied_error(tool_name, arguments)
    if denied_operation is not None:
        from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

        blocked_result = RuntimePreflightValidationError(json.dumps(tool_error_payload(
            tool_name=tool_name, code="tool_operation_denied", message=denied_operation,
        )))
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_result)
        return RuntimePreparedToolExecution(
            arguments=arguments, harness=harness, blocked_result=blocked_result,
        )

    step_declared_grant = discovered_tool_grant
    if step_declared_grant is None and tool_name.startswith("mcp__"):
        step_grants = getattr(runtime_envelope, "discovered_tool_grants", None)
        if step_grants is not None:
            step_declared_grant = step_grants.metadata(tool_name)
    step_error = step_contract_tool_error(
        tool_name, arguments, entity_id=entity,
        workspace_id=context.workspace_id, task_id=context.task_id,
        declared_effect=getattr(step_declared_grant, "effect", None),
    )
    if step_error is not None:
        from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

        blocked_result = RuntimePreflightValidationError(json.dumps(tool_error_payload(
            tool_name=tool_name, code="step_execution_contract_violation", message=step_error,
        )))
        if harness is not None:
            harness.record_tool_block_result(tool_name, blocked_result)
        return RuntimePreparedToolExecution(
            arguments=arguments, harness=harness, blocked_result=blocked_result,
        )

    if tool_name == "generate_file":
        from packages.core.contracts.generate_file_input import generate_file_input_error
        from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

        input_error = generate_file_input_error(arguments)
        if input_error is not None:
            blocked_result = RuntimePreflightValidationError(json.dumps(tool_error_payload(
                tool_name=tool_name, code="tool_input_validation_failed", message=input_error,
            )))
            if harness is not None:
                harness.record_tool_block_result(tool_name, blocked_result)
            return RuntimePreparedToolExecution(
                arguments=arguments, harness=harness, blocked_result=blocked_result,
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
    provider_preflight_arguments = None
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
            runtime_context=context,
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
            if (
                tool_name.startswith("mcp__chrome__")
                and not str(arguments.get("integration_account_id") or "").strip()
            ):
                # Chrome's pinned connection is checked by MCP preflight. Keep
                # its automatically selected account out of the frozen action
                # comparison while still passing it to the handler.
                provider_preflight_arguments = copy.deepcopy(arguments)
        if tool_name.startswith("mcp__chrome__"):
            connection_id = str(
                getattr(mcp_preflight, "chrome_connection_id", "") or ""
            ).strip()
            metadata = getattr(runtime_envelope, "metadata", None)
            if connection_id and isinstance(metadata, dict):
                state = metadata.setdefault("chrome_runtime_contract_v1", {})
                if isinstance(state, dict):
                    # This is produced by the current actor/connection
                    # preflight, never copied from a tool argument.
                    state["chrome_policy_connection_id"] = connection_id
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

    approval_middleware = (
        harness.approval_middleware
        if harness is not None
        else RuntimeApprovalMiddleware()
    )
    # Target binding replaces the sanitized semantic selector with a ref.
    chrome_interaction_mode = runtime_chrome_fast_interaction_mode(
        tool_name,
        resolved_arguments,
        workspace_id=context.workspace_id,
    )

    policy_arguments = _runtime_policy_arguments(
        tool_name,
        resolved_arguments,
        active_user_message=active_user_message,
    )
    call_id = str(context.runtime_tool_call_id or "").strip()
    run_identity = str(context.runtime_run_id or context.performance_run_id or context.task_id or "").strip()
    operation_id = f"runtime:{run_identity}:call:{call_id}" if run_identity and call_id else None
    approval_request = RuntimeApprovalRequest(
        tool_name=tool_name,
        arguments=policy_arguments,
        operation_id=operation_id,
        account_id=(str(mcp_preflight.integration_account_id) if tool_name.startswith("mcp__") and mcp_preflight.integration_account_id else None),
        entity_id=entity,
        user_id=user,
        workspace_id=context.workspace_id,
        # Sandbox-bound tools are authorized against the sandbox binding, so a
        # conversation-less task-worker run presents its binding key here and
        # nowhere else; see runtime_authorization_conversation_id.
        conversation_id=runtime_authorization_conversation_id(context, tool_name),
        task_id=context.task_id,
        step_id=step_id,
        envelope=(harness.envelope if harness is not None else runtime_envelope),
        declared_effect=(
            discovered_tool_grant.effect
            if discovered_tool_grant is not None
            else None
        ),
    )
    output_scope = _output_scope(context, tool_name, resolved_arguments)
    from packages.core.ai.runtime.tool_effect_intents import (
        runtime_consumed_approval_capture_scope,
    )

    with (
        workspace_artifact_scope(output_scope),
        runtime_capture_tiktok_publish_consent() as publish_consents,
        runtime_capture_chrome_publication_binding() as chrome_bindings,
        runtime_consumed_approval_capture_scope() as consumed_approval_ids,
    ):
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
    if len(consumed_approval_ids) > 1:
        return RuntimePreparedToolExecution(
            arguments=arguments,
            harness=harness,
            blocked_result=runtime_tool_error_result(
                "Runtime approval preflight consumed more than one grant. "
                "No external action was executed."
            ),
        )
    consumed_approval_request_id = (
        consumed_approval_ids[0] if consumed_approval_ids else None
    )

    authorization_receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=approval_decision,
        request=approval_request,
    )
    if authorization_receipt is not None:
        authorization_receipt = replace(
            authorization_receipt,
            consumed_approval_request_id=consumed_approval_request_id,
        )
    if tool_name == "mcp__tiktok__post_video":
        if authorization_receipt is None or len(publish_consents) != 1:
            return RuntimePreparedToolExecution(
                arguments=arguments, harness=harness,
                blocked_result=json.dumps({"error": "tiktok_publish_review_required"}),
            )
        authorization_receipt = replace(
            authorization_receipt, tiktok_publish_consent=publish_consents[0],
        )
    if approval_decision.classification.action is not None and approval_decision.classification.action.action_key == "chrome.publication.publish":
        if authorization_receipt is None or len(chrome_bindings) != 1:
            return RuntimePreparedToolExecution(
                arguments=arguments, harness=harness,
                blocked_result=json.dumps({"error": "chrome_publication_review_required"}),
            )
        authorization_receipt = replace(
            authorization_receipt, chrome_publication_binding=chrome_bindings[0],
        )
    chrome_expected_connection_id = (
        str(getattr(mcp_preflight, "chrome_connection_id", "") or "").strip()
        if tool_name.startswith("mcp__chrome__")
        else ""
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
                chrome_interaction_mode=chrome_interaction_mode,
                chrome_sequence_child=chrome_sequence_child,
                chrome_expected_connection_id=chrome_expected_connection_id or None,
                runtime_validation_duration_ms=runtime_validation_duration_ms,
            )
        )
    return RuntimePreparedToolExecution(
        arguments=prepared_arguments,
        artifact_scope=output_scope,
        provider_preflight_arguments=provider_preflight_arguments,
        harness=harness,
        authorization_lease=RuntimeToolAuthorizationLeaseFactory.create(
            authorization_receipt
        ),
        classification=approval_decision.classification,
        consumed_approval_request_id=consumed_approval_request_id,
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
    runtime_validation_duration_ms: float | None = None,
    chrome_sequence_child: bool = False,
    logger: logging.Logger | None = None,
) -> Any:
    """Execute a registered tool through the Runtime Harness boundary."""

    handler = handler_resolver(tool_name)
    if handler is None:
        error_result = runtime_tool_error_result(f"unknown tool '{tool_name}'")
        return await runtime_settle_provider_approval_preflight_failure(
            harness=(RuntimeHarness(runtime_envelope) if runtime_envelope else None),
            tool_name=tool_name,
            arguments=arguments,
            result=error_result,
            entity_id=(
                getattr(runtime_envelope, "entity_id", None) or entity_id or ""
            ),
            conversation_id=(
                getattr(runtime_envelope, "conversation_id", None)
                or conversation_id
            ),
        )

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
        step_id=step_id,
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
        runtime_validation_duration_ms=runtime_validation_duration_ms,
        chrome_sequence_child=chrome_sequence_child,
    )
    if prepared.blocked_result is not None:
        envelope = (
            prepared.harness.envelope if prepared.harness is not None else None
        )
        return await runtime_settle_provider_approval_preflight_failure(
            harness=prepared.harness,
            tool_name=tool_name,
            arguments=arguments,
            result=prepared.blocked_result,
            entity_id=(
                envelope.entity_id or entity_id or ""
                if envelope
                else entity_id or ""
            ),
            conversation_id=(
                envelope.conversation_id if envelope is not None else conversation_id
            ),
        )

    return await runtime_execute_prepared_tool_handler(
        tool_name=tool_name,
        handler=handler,
        prepared=prepared,
        entity_id=entity_id,
        user_id=user_id,
        public_arguments=arguments,
        logger=logger,
    )
