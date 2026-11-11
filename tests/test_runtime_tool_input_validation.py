from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import agentic_loop
from packages.core.ai.runtime import (
    AIRuntimeRequest,
    ChatSurface,
    RuntimeResolver,
)
from packages.core.ai.runtime.approval_messages import (
    runtime_approval_continuation,
    runtime_approval_runtime_metadata,
)
from packages.core.ai.runtime.tool_input_validation import (
    validate_runtime_tool_arguments,
)
from packages.core.ai.runtime.tool_search import runtime_search_tools_schema
from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.tools.manor_tool import MANOR_SCHEMA
from packages.core.ai.tools.workspace_agent_tools import (
    WORKSPACE_AGENT_SCHEMA,
    WORKSPACE_OPERATION_SCHEMA,
)


EMAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp__email__send_email",
        "description": "Send email",
        "parameters": {
            "type": "object",
            "required": ["to", "subject", "body"],
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "attachments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"document_id": {"type": "string"}},
                    },
                },
            },
        },
    },
}


def _make_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _email_arguments(attachments):
    return {
        "to": "reviewer@example.test",
        "subject": "Candidate",
        "body": "Please review.",
        "attachments": attachments,
    }


def test_dynamic_mcp_binding_preserves_discovered_schema():
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPToolBindingFactory,
    )

    discovered = SimpleNamespace(
        provider="email",
        action="send_email",
        schema=EMAIL_SCHEMA,
        account_ids=("account-1",),
        effect="write",
        requires_explicit_account=False,
        supports_all_accounts=True,
        incomplete_account_ids=(),
    )

    binding = RuntimeDynamicMCPToolBindingFactory.from_discovered(
        discovered,
        expires_at=100.0,
    )

    assert binding.schema == EMAIL_SCHEMA
    assert binding.schema is not EMAIL_SCHEMA


def test_nested_attachment_array_is_validated_without_coercion():
    attachments = [{"document_id": "document-1"}]
    assert validate_runtime_tool_arguments(
        arguments=_email_arguments(attachments),
        tool_schema=EMAIL_SCHEMA,
    ) is None

    failure = validate_runtime_tool_arguments(
        arguments=_email_arguments(json.dumps(attachments)),
        tool_schema=EMAIL_SCHEMA,
    )
    assert failure is not None
    assert failure.code == "tool_input_validation_failed"
    assert failure.path == "$.attachments"
    assert failure.schema_rule == "type"
    assert "received string" in failure.message
    assert "document-1" not in failure.message


def test_unexpected_fields_are_named_without_echoing_values():
    schema = {
        "type": "function",
        "function": {
            "name": "strict_tool",
            "parameters": {
                "type": "object",
                "properties": {"expected": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    }

    failure = validate_runtime_tool_arguments(
        arguments={
            "expected": "ok",
            "z_extra": "must-not-leak-z",
            "a_extra": "must-not-leak-a",
        },
        tool_schema=schema,
    )

    assert failure is not None
    assert failure.path == "$"
    assert failure.schema_rule == "additionalProperties"
    assert failure.message == (
        "Tool input has unsupported fields 'a_extra', 'z_extra' at $."
    )
    assert "must-not-leak" not in failure.message


@pytest.mark.parametrize(
    ("schema", "arguments", "path"),
    [
        (
            WORKSPACE_OPERATION_SCHEMA,
            {"action": "create_draft", "patches": [{"op": "rule.upsert", "payload": {}}]},
            "$.patches[0].op",
        ),
        (
            WORKSPACE_AGENT_SCHEMA,
            {"action": "operation", "params": {"action": "create_draft", "patches": [{"op": "rule.upsert", "payload": {}}]}},
            "$.params.patches[0].op",
        ),
        (
            MANOR_SCHEMA,
            {"action": "workspace", "params": {"action": "operation", "params": {"action": "create_draft", "patches": [{"op": "rule.upsert", "payload": {}}]}}},
            "$.params.params.patches[0].op",
        ),
    ],
)
def test_workspace_patch_op_rejected_before_approval(schema, arguments, path):
    failure = validate_runtime_tool_arguments(arguments=arguments, tool_schema=schema)

    assert failure is not None
    assert failure.code == "tool_input_validation_failed"
    assert failure.path == path
    assert failure.schema_rule == "enum"
    assert "rule.add" in failure.message
    assert "rule.update" in failure.message


@pytest.mark.parametrize(
    ("schema", "arguments"),
    [
        (WORKSPACE_OPERATION_SCHEMA, {"action": "create_draft", "patches": [{"op": "rule.add", "payload": {}}]}),
        (WORKSPACE_AGENT_SCHEMA, {"action": "operation", "params": {"action": "create_draft", "operations": [{"op": "rule.update", "payload": {}}]}}),
        (MANOR_SCHEMA, {"action": "workspace", "params": {"action": "operation", "params": {"action": "create_draft", "ops": {"rule.add": {"rule": {"description": "Draft only"}}}}}}),
    ],
)
def test_workspace_patch_op_accepts_supported_shapes(schema, arguments):
    assert validate_runtime_tool_arguments(arguments=arguments, tool_schema=schema) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "schema", "arguments"),
    [
        ("workspace_operation", WORKSPACE_OPERATION_SCHEMA, {"action": "create_draft", "patches": [{"op": "rule.upsert", "payload": {}}]}),
        ("workspace_agent", WORKSPACE_AGENT_SCHEMA, {"action": "operation", "params": {"action": "create_draft", "patches": [{"op": "rule.upsert", "payload": {}}]}}),
        ("manor", MANOR_SCHEMA, {"action": "workspace", "params": {"action": "operation", "params": {"action": "create_draft", "patches": [{"op": "rule.upsert", "payload": {}}]}}}),
    ],
)
async def test_invalid_workspace_patch_never_reaches_runtime(monkeypatch, name, schema, arguments):
    pool = ToolPool()
    pool.register(name, schema, lambda **_kwargs: "unused")
    executed = []

    async def execute_registered(**kwargs):
        executed.append(kwargs)
        return "unexpected"

    monkeypatch.setattr("packages.core.ai.tool_pool.runtime_execute_registered_tool", execute_registered)

    result = await pool.execute(name, arguments, entity_id="entity-1", user_id="user-1")

    assert json.loads(result)["error"] == "tool_input_validation_failed"
    assert executed == []


def test_workspace_patch_validation_rechecks_approved_arguments():
    failure = validate_runtime_tool_arguments(
        arguments={
            "action": "create_draft",
            "patches": [{"op": "rule.upsert", "payload": {}}],
            "approval_token": "already-approved",
        },
        tool_schema=WORKSPACE_OPERATION_SCHEMA,
    )
    assert failure is not None
    assert failure.path == "$.patches[0].op"


def test_workspace_patch_validation_rejects_missing_and_malformed_patches():
    for patches in ([], [{"op": "rule.add", "payload": "not-an-object"}], [{"op": "rule.upsert"}]):
        failure = validate_runtime_tool_arguments(
            arguments={"action": "operation", "params": {"action": "create_draft", "patches": patches}},
            tool_schema=WORKSPACE_AGENT_SCHEMA,
        )
        assert failure is not None
        assert failure.code == "tool_input_validation_failed"



def test_runtime_context_fields_are_not_part_of_public_tool_input_validation():
    schema = {
        "type": "function",
        "function": {
            "name": "strict_tool",
            "parameters": {
                "type": "object",
                "properties": {"expected": {"type": "string"}},
                "required": ["expected"],
                "additionalProperties": False,
            },
        },
    }

    assert validate_runtime_tool_arguments(
        arguments={
            "expected": "ok",
            "approval_token": "approval-1",
            "_workflow_run_id_from_context": "run-1",
            "_workflow_step_id_from_context": "step-1",
            "_workflow_lineage_root_run_id_from_context": "lineage-1",
            "_workflow_project_id_from_context": "project-1",
            "_workflow_action_grant_id_from_context": "grant-1",
        },
        tool_schema=schema,
    ) is None


def test_strict_tool_schema_ignores_trusted_runtime_context_only():
    schema = runtime_search_tools_schema()
    runtime_context = {
        "_user_id_from_context": "user-1",
        "_active_user_message_from_context": "Search Gmail",
        "_allowed_tool_names_from_context": ["search_tools"],
        "workspace_id": "workspace-1",
        "conversation_id": "conversation-1",
        "task_id": "task-1",
    }

    assert validate_runtime_tool_arguments(
        arguments={
            "mode": "semantic",
            "query": "Gmail messages",
            "max_results": 5,
            **runtime_context,
        },
        tool_schema=schema,
    ) is None

    failure = validate_runtime_tool_arguments(
        arguments={"query": "Gmail messages", "unexpected": True},
        tool_schema=schema,
    )
    assert failure is not None
    assert failure.schema_rule == "additionalProperties"


@pytest.mark.asyncio
async def test_tool_pool_executes_strict_tool_with_injected_runtime_context(
    monkeypatch,
):
    pool = ToolPool()
    pool.register(
        "search_tools",
        runtime_search_tools_schema(),
        lambda **_kwargs: "unused",
    )
    calls = []

    async def execute_registered(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_execute_registered_tool",
        execute_registered,
    )

    arguments = {
        "mode": "semantic",
        "query": "Gmail messages",
        "max_results": 5,
        "_user_id_from_context": "user-1",
        "_active_user_message_from_context": "Search Gmail",
        "_allowed_tool_names_from_context": ["search_tools"],
    }
    result = await pool.execute(
        "search_tools",
        arguments,
        entity_id="entity-1",
        user_id="user-1",
    )

    assert result == "ok"
    assert calls[0]["arguments"] == arguments


def test_search_tasks_schema_accepts_multi_value_and_range_filters():
    from packages.core.ai.tools.task_tools import SEARCH_TASKS_SCHEMA

    assert validate_runtime_tool_arguments(
        arguments={
            "query": "oauth review",
            "statuses": ["proposed", "in_progress", "completed"],
            "priorities": [3, 4, 5],
            "priority_min": 3,
            "priority_max": 5,
            "assignee_ids": ["staff_1", "staff_2"],
            "created_after": "2026-09-01T00:00:00Z",
            "created_before": "2026-09-02T00:00:00Z",
            "limit": 20,
            "offset": 0,
        },
        tool_schema=SEARCH_TASKS_SCHEMA,
    ) is None

    failure = validate_runtime_tool_arguments(
        arguments={"priorities": [0, 4]},
        tool_schema=SEARCH_TASKS_SCHEMA,
    )
    assert failure is not None
    assert failure.code == "tool_input_validation_failed"
    assert failure.path == "$.priorities[0]"
    assert failure.schema_rule == "minimum"


def test_approval_continuation_preserves_typed_nested_arguments():
    arguments = {
        **_email_arguments([{"document_id": "document-1"}]),
        "confirm": "false",
        "_user_id_from_context": "do-not-persist",
    }
    continuation = runtime_approval_continuation(
        "mcp__email__send_email",
        arguments,
    )
    metadata = runtime_approval_runtime_metadata(
        {
            "tool": "mcp__email__send_email",
            "continuation": continuation,
        },
        "approval-1",
    )

    assert metadata is not None
    assert metadata["extra_tool_names"] == ["mcp__email__send_email"]
    forced = metadata["forced_tool_calls"][0]
    assert forced["arguments"]["attachments"] == [{"document_id": "document-1"}]
    assert forced["arguments"]["approval_token"] == "approval-1"
    assert forced["arguments"]["confirm"] == "false"
    assert "_user_id_from_context" not in forced["arguments"]
    assert forced["disable_followup_tools"] is True
    assert arguments.get("approval_token") is None


def test_approval_continuation_preserves_every_json_value_type():
    arguments = {
        "string": "false",
        "boolean": False,
        "integer": 7,
        "number": 3.5,
        "nothing": None,
        "array": [1, "two", False, None, {"nested": [3]}],
        "object": {"enabled": True, "items": ["a", "b"]},
    }

    continuation = runtime_approval_continuation("mcp__example__act", arguments)

    assert continuation["arguments"] == arguments
    assert continuation["arguments"] is not arguments


def test_approval_continuation_preserves_nested_context_named_business_fields():
    arguments = {
        "workspace_id": "runtime-workspace",
        "operation": {
            "workspace_id": "target-workspace",
            "conversation_id": "target-conversation",
            "snapshot_id": "business-snapshot",
            "approval_token": "business-token",
        },
    }

    continuation = runtime_approval_continuation(
        "mcp__example__act",
        arguments,
    )

    assert continuation["arguments"] == {
        "operation": arguments["operation"],
    }


@pytest.mark.asyncio
async def test_tool_pool_rejects_invalid_input_before_runtime_or_handler(monkeypatch):
    pool = ToolPool()
    called = False

    async def handler(**_kwargs):
        nonlocal called
        called = True
        return "sent"

    async def should_not_execute(**_kwargs):
        raise AssertionError("runtime execution must not start for invalid input")

    pool.register("mcp__email__send_email", EMAIL_SCHEMA, handler, deferred=True)
    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_execute_registered_tool",
        should_not_execute,
    )

    result = await pool.execute(
        "mcp__email__send_email",
        _email_arguments('[{"document_id":"document-1"}]'),
    )
    payload = json.loads(result)
    assert payload["error"] == "tool_input_validation_failed"
    assert payload["details"]["path"] == "$.attachments"
    assert called is False


@pytest.mark.asyncio
async def test_tool_pool_settles_provider_approval_when_schema_validation_blocks(
    monkeypatch,
):
    pool = ToolPool()
    pool.register("mcp__email__send_email", EMAIL_SCHEMA, lambda **_kwargs: "sent")
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            conversation_id="conv_1",
            metadata={
                "provider_approval_execution": {
                    "hitl_id": "hitl_1",
                    "confirmation_tool": "mcp__email__confirm_send",
                    "retry_tool": "mcp__email__send_email",
                },
            },
        ),
        tool_schemas=[EMAIL_SCHEMA],
        allowed_tool_names={"mcp__email__send_email"},
    )
    settled = []

    async def settle_provider_failure(**kwargs):
        settled.append(kwargs)
        return kwargs["result"]

    async def should_not_execute(**_kwargs):
        raise AssertionError("runtime execution must not start for invalid input")

    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_settle_provider_approval_preflight_failure",
        settle_provider_failure,
    )
    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_execute_registered_tool",
        should_not_execute,
    )

    arguments = _email_arguments('[{"document_id":"document-1"}]')
    result = await pool.execute(
        "mcp__email__send_email",
        arguments,
        runtime_envelope=envelope,
    )

    payload = json.loads(result)
    assert payload["error"] == "tool_input_validation_failed"
    assert len(settled) == 1
    assert settled[0]["harness"].envelope is envelope
    assert settled[0]["tool_name"] == "mcp__email__send_email"
    assert settled[0]["arguments"] == arguments
    assert settled[0]["result"] == result
    assert settled[0]["entity_id"] == "ent_1"
    assert settled[0]["conversation_id"] == "conv_1"


@pytest.mark.asyncio
async def test_tool_pool_propagates_provider_approval_preflight_settlement_failure(
    monkeypatch,
):
    pool = ToolPool()
    pool.register("mcp__email__send_email", EMAIL_SCHEMA, lambda **_kwargs: "sent")
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            conversation_id="conv_1",
            metadata={
                "provider_approval_execution": {
                    "hitl_id": "hitl_1",
                    "confirmation_tool": "mcp__email__confirm_send",
                    "retry_tool": "mcp__email__send_email",
                },
            },
        ),
        tool_schemas=[EMAIL_SCHEMA],
        allowed_tool_names={"mcp__email__send_email"},
    )

    async def fail_settlement(**_kwargs):
        raise RuntimeError("approval settlement unavailable")

    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_settle_provider_approval_preflight_failure",
        fail_settlement,
    )

    with pytest.raises(RuntimeError, match="approval settlement unavailable"):
        await pool.execute(
            "mcp__email__send_email",
            _email_arguments('[{"document_id":"document-1"}]'),
            runtime_envelope=envelope,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_mode", ["discovery_failed", "schema_changed"])
async def test_tool_pool_settles_failure_after_stale_dynamic_binding_refresh(
    monkeypatch,
    refresh_mode,
):
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPRehydrationResult,
        RuntimeDynamicMCPRehydrationStatus,
        RuntimeDynamicMCPToolBinding,
        RuntimeDynamicMCPFailureResultFactory,
    )
    from packages.core.ai.runtime.streams import (
        runtime_tool_alternate_path_error_result,
    )

    pool = ToolPool()
    binding = RuntimeDynamicMCPToolBinding(
        provider="email",
        action="send_email",
        expires_at=100.0,
        schema=EMAIL_SCHEMA,
    )
    initial = RuntimeDynamicMCPRehydrationResult(
        status=RuntimeDynamicMCPRehydrationStatus.BOUND,
        tool_name="mcp__email__send_email",
        provider="email",
        binding=binding,
    )
    if refresh_mode == "discovery_failed":
        refreshed = RuntimeDynamicMCPRehydrationResult(
            status=RuntimeDynamicMCPRehydrationStatus.DISCOVERY_FAILED,
            tool_name="mcp__email__send_email",
            provider="email",
            reason="Live discovery failed before provider execution.",
        )
    else:
        changed_schema = {
            **EMAIL_SCHEMA,
            "function": {
                **EMAIL_SCHEMA["function"],
                "parameters": {
                    **EMAIL_SCHEMA["function"]["parameters"],
                    "required": ["to", "subject", "body", "account_id"],
                    "properties": {
                        **EMAIL_SCHEMA["function"]["parameters"]["properties"],
                        "account_id": {"type": "string"},
                    },
                },
            },
        }
        refreshed = RuntimeDynamicMCPRehydrationResult(
            status=RuntimeDynamicMCPRehydrationStatus.BOUND,
            tool_name="mcp__email__send_email",
            provider="email",
            binding=RuntimeDynamicMCPToolBinding(
                provider="email",
                action="send_email",
                expires_at=200.0,
                schema=changed_schema,
            ),
        )
    monkeypatch.setattr(
        pool,
        "_ensure_dynamic_mcp_tool_binding",
        AsyncMock(side_effect=[initial, refreshed]),
    )
    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_execute_registered_tool",
        AsyncMock(
            return_value=RuntimeDynamicMCPFailureResultFactory.stale_preflight_binding(
                provider="email",
                tool_name="mcp__email__send_email",
            )
        ),
    )
    settle = AsyncMock(
        side_effect=lambda **kwargs: runtime_tool_alternate_path_error_result(
            kwargs["result"]
        )
    )
    monkeypatch.setattr(
        "packages.core.ai.tool_pool.runtime_settle_provider_approval_preflight_failure",
        settle,
    )

    result = await pool.execute(
        "mcp__email__send_email",
        _email_arguments([]),
        entity_id="ent_1",
        user_id="user_1",
    )

    payload = json.loads(result)
    if refresh_mode == "discovery_failed":
        assert payload["error"] == "mcp_tool_discovery_failed"
        assert payload["reason"] == "Live discovery failed before provider execution."
    else:
        assert payload["error"] == "tool_input_validation_failed"
        assert payload["details"]["path"] == "$"
    assert payload["retry_policy"] == "read_only_alternate_path"
    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_approval_preflight_failure_allows_read_only_alternate(
    monkeypatch,
):
    from packages.core.ai.runtime.harness import RuntimeHarness
    from packages.core.ai.runtime.tool_execution import (
        runtime_settle_provider_approval_preflight_failure,
    )

    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            conversation_id="conv_1",
            metadata={
                "provider_approval_execution": {
                    "hitl_id": "hitl_1",
                    "confirmation_tool": "mcp__email__confirm_send",
                    "retry_tool": "mcp__email__send_email",
                },
            },
        ),
        tool_schemas=[EMAIL_SCHEMA],
        allowed_tool_names={"mcp__email__send_email"},
    )
    persist = AsyncMock()
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution._persist_provider_approval_result",
        persist,
    )

    result = await runtime_settle_provider_approval_preflight_failure(
        harness=RuntimeHarness(envelope),
        tool_name="mcp__email__send_email",
        arguments=_email_arguments('[{"document_id":"document-1"}]'),
        result=json.dumps(
            {
                "status": "error",
                "error": "tool_input_validation_failed",
            }
        ),
        entity_id="ent_1",
        conversation_id="conv_1",
    )

    assert json.loads(result) == {
        "status": "error",
        "error": "tool_input_validation_failed",
        "retry_policy": "read_only_alternate_path",
    }
    from packages.core.ai.runtime.tool_results import RuntimeAlternatePathToolResult

    assert isinstance(result, RuntimeAlternatePathToolResult)
    persist.assert_awaited_once()


def test_provider_approval_alternate_path_preserves_ambiguous_evidence():
    from packages.core.ai.runtime.streams import (
        runtime_tool_allows_alternate_path,
        runtime_tool_alternate_path_error_result,
    )

    result = runtime_tool_alternate_path_error_result(
        json.dumps(
            {
                "ok": False,
                "error": "provider_approval_execution_blocked",
                "status": "ambiguous",
                "approval_id": "hitl_1",
                "reason": "The earlier provider action must be reconciled.",
            }
        )
    )

    assert json.loads(result) == {
        "ok": False,
        "error": "provider_approval_execution_blocked",
        "status": "ambiguous",
        "approval_id": "hitl_1",
        "reason": "The earlier provider action must be reconciled.",
        "retry_policy": "read_only_alternate_path",
    }
    assert runtime_tool_allows_alternate_path(result) is True


def test_alternate_path_proof_cannot_be_minted_from_provider_json_or_checkpoint():
    from packages.core.ai.runtime.streams import (
        runtime_tool_allows_alternate_path,
        runtime_tool_alternate_path_error_result,
    )

    proven = runtime_tool_alternate_path_error_result(
        {"status": "error", "error": "approved action stopped before I/O"}
    )
    assert runtime_tool_allows_alternate_path(proven)
    assert not runtime_tool_allows_alternate_path(str(proven))
    assert not runtime_tool_allows_alternate_path(json.loads(json.dumps(proven)))
    assert not runtime_tool_allows_alternate_path(
        json.dumps({
            "status": "error",
            "error": "provider failed",
            "retry_policy": "read_only_alternate_path",
        })
    )
    assert not runtime_tool_allows_alternate_path(
        runtime_tool_alternate_path_error_result({"status": "success"})
    )


@pytest.mark.asyncio
async def test_prepared_provider_failure_returns_settlement_result(monkeypatch):
    from packages.core.ai.runtime.harness import RuntimeHarness
    from packages.core.ai.runtime.streams import (
        runtime_tool_alternate_path_error_result,
    )
    from packages.core.ai.runtime.tool_context import RuntimeToolContextConflictError
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            conversation_id="conv_1",
            metadata={
                "provider_approval_execution": {
                    "hitl_id": "hitl_1",
                    "confirmation_tool": "mcp__email__confirm_send",
                    "retry_tool": "mcp__email__send_email",
                },
            },
        ),
        tool_schemas=[EMAIL_SCHEMA],
        allowed_tool_names={"mcp__email__send_email"},
    )
    tagged = runtime_tool_alternate_path_error_result("scope changed")
    settle = AsyncMock(return_value=tagged)
    def context_conflict(*_args, **_kwargs):
        raise RuntimeToolContextConflictError("scope changed")

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution.runtime_tool_call_context_from_kwargs",
        context_conflict,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution.runtime_settle_provider_approval_preflight_failure",
        settle,
    )
    handler = AsyncMock()

    result = await runtime_execute_prepared_tool_handler(
        tool_name="mcp__email__send_email",
        handler=handler,
        prepared=RuntimePreparedToolExecution(
            arguments=_email_arguments([]),
            harness=RuntimeHarness(envelope),
        ),
        entity_id="ent_1",
        user_id="user_1",
    )

    assert result == tagged
    settle.assert_awaited_once()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_approval_preflight_settlement_is_not_best_effort(monkeypatch):
    from packages.core.ai.runtime.control import RuntimeTurnAborted
    from packages.core.ai.runtime.harness import RuntimeHarness
    from packages.core.ai.runtime.tool_execution import (
        runtime_settle_provider_approval_preflight_failure,
    )

    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            conversation_id="conv_1",
            metadata={
                "provider_approval_execution": {
                    "hitl_id": "hitl_1",
                    "confirmation_tool": "mcp__email__confirm_send",
                    "retry_tool": "mcp__email__send_email",
                },
            },
        ),
        tool_schemas=[EMAIL_SCHEMA],
        allowed_tool_names={"mcp__email__send_email"},
    )

    async def fail_persistence(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution._persist_provider_approval_result",
        fail_persistence,
    )

    with pytest.raises(RuntimeTurnAborted, match="could not be durably settled") as exc:
        await runtime_settle_provider_approval_preflight_failure(
            harness=RuntimeHarness(envelope),
            tool_name="mcp__email__send_email",
            arguments=_email_arguments('[{"document_id":"document-1"}]'),
            result=json.dumps({"status": "blocked"}),
            entity_id="ent_1",
            conversation_id="conv_1",
        )
    assert exc.value.allow_alternate_path is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["tagged_result", "ambiguous_preflight", "recoverable_abort"],
)
async def test_approved_forced_call_failure_returns_to_model_with_tools(
    failure_mode: str,
) -> None:
    from packages.core.ai.runtime.control import RuntimeTurnAborted
    from packages.core.ai.runtime.streams import (
        runtime_tool_alternate_path_error_result,
    )

    executed: list[str] = []

    async def execute(name: str, _arguments: dict) -> str:
        executed.append(name)
        if name == "mcp__email__send_email":
            if failure_mode in {"tagged_result", "ambiguous_preflight"}:
                return runtime_tool_alternate_path_error_result(
                    (
                        json.dumps(
                            {
                                "ok": False,
                                "error": "provider_approval_execution_blocked",
                                "status": "ambiguous",
                                "approval_id": "hitl_1",
                                "reason": "The earlier action must be reconciled.",
                            }
                        )
                        if failure_mode == "ambiguous_preflight"
                        else "approved route failed before provider I/O"
                    )
                )
            raise RuntimeTurnAborted(
                "approval settlement unavailable",
                allow_alternate_path=True,
            )
        if name == "workspace_search":
            return json.dumps({"matches": [{"title": "draft email"}]})
        return json.dumps({"error": f"unexpected tool {name}"})

    executor = AsyncMock(side_effect=execute)
    search_schema = _make_schema("workspace_search")
    write_schema = _make_schema("workspace_create_task")
    with (
        patch(
            "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion",
            new_callable=AsyncMock,
        ) as text_completion,
        patch(
            "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
            new_callable=AsyncMock,
        ) as tool_completion,
    ):
        tool_completion.side_effect = [
            (
                "I will inspect a safe alternate source.",
                [
                    {
                        "id": "call_search",
                        "name": "workspace_search",
                        "arguments": {"query": "draft email"},
                    }
                ],
                {"prompt": 10, "completion": 5, "total": 15},
            ),
            (
                "The approved send path is unavailable, but I found the draft.",
                None,
                {"prompt": 10, "completion": 5, "total": 15},
            ),
        ]
        result = await agentic_loop(
            system_prompt="Resume one approved email action.",
            user_message="Approved the requested action.",
            tools=[EMAIL_SCHEMA, search_schema, write_schema],
            tool_executor=executor,
            forced_tool_calls=[
                {
                    "name": "mcp__email__send_email",
                    "arguments": {
                        **_email_arguments([{"document_id": "document-1"}]),
                        "approval_token": "approval-1",
                    },
                    "disable_followup_tools": True,
                }
            ],
        )

    # The failed approved send is never replayed. Its error reaches the model,
    # which keeps its tools; any new write re-enters the ordinary approval gate.
    assert executed == ["mcp__email__send_email", "workspace_search"]
    text_completion.assert_not_awaited()
    assert tool_completion.await_count == 2
    first_round_messages = tool_completion.await_args_list[0].args[0]
    expected_error = {
        "tagged_result": "approved route failed before provider I/O",
        "ambiguous_preflight": "provider_approval_execution_blocked",
        "recoverable_abort": "approval settlement unavailable",
    }[failure_mode]
    assert any(
        item.get("role") == "tool" and expected_error in str(item.get("content"))
        for item in first_round_messages
    )
    first_round_tools = tool_completion.await_args_list[0].args[1]
    assert {tool["function"]["name"] for tool in first_round_tools} == {
        "mcp__email__send_email",
        "workspace_search",
        "workspace_create_task",
    }
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
@pytest.mark.parametrize("host_proof", [True, False])
async def test_failed_approved_call_returns_to_model_in_every_loop_entry(
    monkeypatch, entry, host_proof,
):
    # With or without the host's pre-I/O proof, the failed approved call is an
    # observation for the model: its dependent suffix never runs, nothing is
    # replayed, and the model keeps its tools to choose the next call.
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.ai.runtime.streams import runtime_tool_alternate_path_error_result

    approved_call = {
        "id": "approved_send",
        "name": "mcp__email__send_email",
        "arguments": _email_arguments([]),
    }
    dependent_call = {
        "id": "dependent_write",
        "name": "workspace_create_task",
        "arguments": {"title": "Must not run"},
    }
    serialized_error = runtime_tool_alternate_path_error_result(
        {"status": "error", "error": "approved path failed before I/O"}
    )
    executed: list[str] = []
    model_calls = 0
    summary_calls = 0

    async def execute(name, _arguments):
        executed.append(name)
        if name == approved_call["name"]:
            return serialized_error if host_proof else str(serialized_error)
        assert name == "workspace_search"
        return json.dumps({"matches": [{"title": "Existing draft"}]})

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", [approved_call, dependent_call], {}
        assert {tool["function"]["name"] for tool in tools} == {
            approved_call["name"],
            "workspace_search",
            dependent_call["name"],
        }
        assert any(
            item.get("role") == "tool"
            and "approved path failed before I/O" in str(item.get("content"))
            for item in messages
        )
        if model_calls == (2 if entry == "normal" else 1):
            return "", [{
                "id": "read_alternate",
                "name": "workspace_search",
                "arguments": {"query": "Existing draft"},
            }], {}
        return "The approved send did not run; I found the draft.", [], {}

    async def summary(messages, **_kwargs):
        nonlocal summary_calls
        summary_calls += 1
        return "unexpected forced summary", {}

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        model,
    )
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion",
        summary,
    )
    kwargs = {}
    if entry == "forced":
        kwargs["forced_tool_calls"] = [
            {**approved_call, "disable_followup_tools": True},
            dependent_call,
        ]
    elif entry == "resume":
        kwargs["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    },
                } for call in (approved_call, dependent_call)],
            }],
            usage={},
            rounds=1,
            tool_calls_made=[],
            pending_tool_call=approved_call,
            remaining_tool_calls=[dependent_call],
            disable_followup_tools=True,
        ).to_dict()

    result = await agentic_loop(
        system_prompt="Continue the approved email request.",
        user_message="Approved the requested action.",
        tools=[
            EMAIL_SCHEMA,
            _make_schema("workspace_search"),
            _make_schema("workspace_create_task"),
        ],
        tool_executor=execute,
        max_rounds=5,
        **kwargs,
    )

    # The dependent write and the approved send never run again.
    assert executed == [approved_call["name"], "workspace_search"]
    assert result.stop_reason == "completed"
    assert result.content == "The approved send did not run; I found the draft."
    assert summary_calls == 0
    assert model_calls == (3 if entry == "normal" else 2)


@pytest.mark.asyncio
async def test_serialized_pending_result_is_observed_not_replayed_on_resume(monkeypatch):
    # A JSON round-trip strips the host proof; either way the stored
    # failure is shown to the model and the pending call never re-runs.
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.ai.runtime.streams import runtime_tool_alternate_path_error_result

    pending = {
        "id": "approved_send",
        "name": "mcp__email__send_email",
        "arguments": _email_arguments([]),
    }
    proven = runtime_tool_alternate_path_error_result(
        {"status": "error", "error": "approved path failed before I/O"}
    )
    checkpoint = RuntimeAgentCheckpoint(
        messages=[{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": pending["id"],
                "type": "function",
                "function": {
                    "name": pending["name"],
                    "arguments": json.dumps(pending["arguments"]),
                },
            }],
        }],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call=pending,
        disable_followup_tools=True,
        pending_tool_result={"id": pending["id"], "content": proven},
    )
    serialized_checkpoint = json.loads(json.dumps(checkpoint.to_dict()))
    executor = AsyncMock()
    model = AsyncMock(
        return_value=("The send did not run; reconcile it before retrying.", [], {})
    )
    summary = AsyncMock(return_value=("unexpected forced summary", {}))
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        model,
    )
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion",
        summary,
    )

    result = await agentic_loop(
        system_prompt="Continue the approved email request.",
        user_message="Approved the requested action.",
        tools=[EMAIL_SCHEMA, _make_schema("workspace_search")],
        tool_executor=executor,
        resume_checkpoint=serialized_checkpoint,
        max_rounds=5,
    )

    executor.assert_not_awaited()
    summary.assert_not_awaited()
    model.assert_awaited_once()
    messages, tools = model.await_args.args[:2]
    assert any(
        item.get("role") == "tool"
        and item.get("tool_call_id") == pending["id"]
        and "approved path failed before I/O" in str(item.get("content"))
        for item in messages
    )
    assert {tool["function"]["name"] for tool in tools} == {
        pending["name"],
        "workspace_search",
    }
    assert result.stop_reason == "completed"
    assert result.content == "The send did not run; reconcile it before retrying."


@pytest.mark.asyncio
async def test_nonrecoverable_runtime_abort_still_stops_agent_loop() -> None:
    from packages.core.ai.runtime.control import RuntimeTurnAborted

    executor = AsyncMock(side_effect=RuntimeTurnAborted("stale runtime state"))
    with (
        patch(
            "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion",
            new_callable=AsyncMock,
        ) as text_completion,
        patch(
            "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
            new_callable=AsyncMock,
        ) as tool_completion,
        pytest.raises(RuntimeTurnAborted, match="stale runtime state"),
    ):
        await agentic_loop(
            system_prompt="Resume one approved email action.",
            user_message="Approved the requested action.",
            tools=[EMAIL_SCHEMA],
            tool_executor=executor,
            forced_tool_calls=[
                {
                    "name": "mcp__email__send_email",
                    "arguments": _email_arguments([]),
                    "disable_followup_tools": True,
                }
            ],
        )

    executor.assert_awaited_once()
    text_completion.assert_not_awaited()
    tool_completion.assert_not_awaited()


def _tool_schemas_declaring_context_keys():
    import importlib
    import pkgutil

    import packages.core.ai.tools as tools_package
    from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS

    found = {}
    for module_info in pkgutil.iter_modules(tools_package.__path__):
        module = importlib.import_module(f"packages.core.ai.tools.{module_info.name}")
        for value in vars(module).values():
            function = value.get("function") if isinstance(value, dict) else None
            if not isinstance(function, dict):
                continue
            properties = (function.get("parameters") or {}).get("properties") or {}
            keys = [key for key in properties if key in RUNTIME_TOOL_CONTEXT_KEYS]
            if keys:
                found[function["name"]] = (value, keys)
    return found


def test_a_tool_that_declares_a_context_key_receives_it():
    """task_id/workspace_id are host context keys and were stripped before validation, so
    get_task_details, update_task and others always failed as "missing task_id"."""
    from packages.core.ai.runtime.tool_input_validation import validate_runtime_tool_arguments

    declared = _tool_schemas_declaring_context_keys()
    assert "get_task_details" in declared  # the scan sees the live failure
    for name, (schema, keys) in declared.items():
        required = set((schema["function"].get("parameters") or {}).get("required") or [])
        arguments = {key: "01ABCDEFGHJKMNPQRSTVWXYZ00" for key in keys}
        for key in required - set(arguments):
            arguments[key] = "x"
        failure = validate_runtime_tool_arguments(arguments=arguments, tool_schema=schema)
        assert failure is None or failure.expected not in keys, (name, failure)
