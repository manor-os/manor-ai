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
from packages.core.ai.tool_pool import ToolPool


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
    assert payload["path"] == "$.attachments"
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
            return_value=RuntimeDynamicMCPFailureResultFactory.stale_binding(
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
        assert payload["path"] == "$"
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
async def test_approved_forced_call_continues_with_read_only_alternate_path(
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

    assert executed == ["mcp__email__send_email", "workspace_search"]
    text_completion.assert_not_awaited()
    assert tool_completion.await_count == 2
    first_round_tools = tool_completion.await_args_list[0].args[1]
    assert [tool["function"]["name"] for tool in first_round_tools] == [
        "workspace_search"
    ]
    assert result.stop_reason == "completed"


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
