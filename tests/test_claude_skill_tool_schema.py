"""Send the real registered skill schema verbatim across both Claude transports.

The ``invoke_skill`` contract carries no top-level combinator: Anthropic rejects
``anyOf``/``oneOf``/``allOf`` at the root of ``input_schema`` with HTTP 400, so
``input`` is the only schema-required argument and the ``skill_id`` / legacy
``skill`` alternative is enforced by the runtime action instead. Neither the
native Anthropic transport nor the OpenRouter transport may project, rewrite, or
mutate that schema on the way out.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator

from packages.core.ai import llm_client
from packages.core.ai.runtime.tool_input_validation import validate_runtime_tool_arguments
from packages.core.ai.tools.skill_tools import get_tools


def _skill_tool():
    return next(schema for schema, _handler in get_tools() if schema["function"]["name"] == "invoke_skill")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_claude_skill_schema_is_sent_verbatim_and_requires_input(monkeypatch, native):
    tool = _skill_tool()
    original = deepcopy(tool)
    captured = []
    base_url = "https://api.anthropic.com/v1" if native else "https://openrouter.ai/api/v1"
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", AsyncMock(return_value=SimpleNamespace(
        api_key=("sk-ant-" if native else "sk-or-") + "x" * 40,
        base_url=base_url, provider="anthropic" if native else "openrouter", source="test",
    )))
    monkeypatch.setattr(llm_client, "_preflight_credit_check", AsyncMock())
    monkeypatch.setattr(llm_client, "_llm_stream_transport_enabled", lambda: False)
    monkeypatch.setattr(llm_client, "_apply_prompt_cache", lambda _payload: False)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)

    async def post(url, headers, payload, **_kwargs):
        captured.append(deepcopy(payload))
        schema = payload["tools"][0]["input_schema"] if native else payload["tools"][0]["function"]["parameters"]
        assert not {"anyOf", "allOf", "oneOf"}.intersection(schema)
        body = ({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
                if native else {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return httpx.Response(200, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    content, calls, usage = await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "List properties"}], [tool], model="anthropic/claude-sonnet-4.6",
    )
    assert content == "ok", usage
    assert calls is None
    assert len(captured) == 1
    wire_schema = captured[0]["tools"][0]["input_schema"] if native else captured[0]["tools"][0]["function"]["parameters"]
    assert wire_schema == original["function"]["parameters"]
    validator = Draft202012Validator(wire_schema)
    assert validator.is_valid({"skill_id": "example", "input": "request"})
    assert validator.is_valid({"skill": "example", "input": "request"})
    # The skill_id / skill alternative is the runtime action's job, not the schema's.
    assert validator.is_valid({"input": "request"})
    assert not validator.is_valid({"skill_id": "example"})
    assert tool == original
    assert validate_runtime_tool_arguments(arguments={"skill_id": "example", "input": "request"}, tool_schema=tool) is None
    assert validate_runtime_tool_arguments(arguments={"skill": "example", "input": "request"}, tool_schema=tool) is None
    assert validate_runtime_tool_arguments(arguments={"skill_id": "example"}, tool_schema=tool) is not None


def test_tool_normalization_and_anthropic_conversion_keep_schemas_verbatim():
    tool = _skill_tool()
    assert llm_client._normalize_tools_for_llm([tool]) == [tool]
    converted = llm_client._openai_tools_to_anthropic_tools([tool])
    assert converted[0]["input_schema"] == tool["function"]["parameters"]
