"""Known missing configuration is terminal, never a JSON/provider retry."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.ai import llm_client
from packages.core.ai.agentic_loop import agentic_loop
from packages.core.ai.runtime import general_chat_intent
from packages.core.ai.runtime import workspace_creation_authorization
from packages.core.ai.runtime.output_policy import (
    runtime_assistant_result_meta,
    runtime_public_assistant_message_content,
)


CODE = "llm_configuration_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_missing_route_returns_typed_failure_before_credit_or_network(monkeypatch, with_tools):
    route = AsyncMock(return_value=SimpleNamespace(
        api_key="", base_url="https://unused.invalid", provider="openai", source="missing",
    ))
    credits = AsyncMock(side_effect=AssertionError("No credit check before configuration"))
    network = AsyncMock(side_effect=AssertionError("No provider call without configuration"))
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", route)
    monkeypatch.setattr(llm_client, "_preflight_credit_check", credits)
    monkeypatch.setattr(llm_client, "_post_with_retry", network)
    kwargs = {"model": "openai/test-model"}
    if with_tools:
        result = await llm_client.chat_completion_with_tools([], [], **kwargs)
    else:
        result = await llm_client.chat_completion([], **kwargs)
    assert result[0] == ""
    assert result[-1]["error_code"] == CODE
    assert result[-1]["total"] == 0
    route.assert_awaited_once()
    credits.assert_not_awaited()
    network.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("module,method,kwargs", [
    (general_chat_intent, "classify_general_chat_intent", {}),
    (workspace_creation_authorization, "classify_workspace_creation_authorization", {}),
])
async def test_optional_classifier_does_not_parse_failed_completion(monkeypatch, module, method, kwargs):
    completion = AsyncMock(return_value=SimpleNamespace(
        content="", usage={"error": "Missing configuration", "error_code": CODE},
    ))
    parse = Mock(side_effect=AssertionError("Failed completion must not be parsed"))
    monkeypatch.setattr(module, "runtime_execute_text_completion", completion)
    monkeypatch.setattr(module.json, "loads", parse)
    result = await getattr(module, method)(message_text="List my properties", **kwargs)
    assert result is not None  # Optional worker failure still permits a configured primary route.
    completion.assert_awaited_once()
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_missing_configuration_stops_first_round_and_survives_history_projection(monkeypatch):
    from packages.core.services.usage_service import build_durable_llm_billing_callbacks

    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    reserve, _ = build_durable_llm_billing_callbacks(
        source_prefix="oss-missing-configuration", entity_id="oss-entity",
        user_id=None, agent_id=None, workspace_id=None, conversation_id=None,
        fallback_model="openai/test-model",
    )
    database = Mock(side_effect=AssertionError("OSS must not reserve Cloud credits"))
    monkeypatch.setattr("packages.core.database.async_session", database)
    completion = AsyncMock(return_value=("", {"error": "Missing configuration", "error_code": CODE}))
    execute = AsyncMock(side_effect=AssertionError("No tool execution without a model response"))
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", completion,
    )
    result = await agentic_loop(
        system_prompt="system", user_message="List my properties", tools=[], tool_executor=execute,
        on_llm_call_before=reserve,
    )
    assert result.stop_reason == "error"
    assert result.error == CODE
    assert result.error_detail["code"] == CODE
    assert result.rounds == 1
    completion.assert_awaited_once()
    execute.assert_not_awaited()
    database.assert_not_called()
    meta = runtime_assistant_result_meta(result)
    assert meta["stream_status"] == "error"
    public = runtime_public_assistant_message_content(result.content, meta)
    assert "configure" in public.lower()
    assert "administrator" in public.lower()
    assert "API key" not in public
