"""Only newly generated missing-provider-ID fallbacks receive fresh identities."""
from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from packages.core.ai import llm_client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider_id", [None, "provider-preserved-id"])
async def test_new_response_missing_ids_are_unique_and_provider_ids_survive(monkeypatch, provider, stream, provider_id):
    arguments = {"path": "report.md"}
    id_field = {"id": provider_id} if provider_id else {}
    if provider == "anthropic":
        body = {
            "content": [{"type": "tool_use", "name": "patch_file", "input": arguments, **id_field}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 2, "output_tokens": 2},
        }
        chunks = [
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "name": "patch_file", "input": {}, **id_field,
            }},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"report.md"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}},
        ]
    else:
        body = {
            "choices": [{"message": {"tool_calls": [{**id_field, "function": {
                "name": "patch_file", "arguments": json.dumps(arguments),
            }}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        }
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, **id_field, "function": {
                "name": "patch_file", "arguments": '{"path":',
            }}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"report.md"}'}}]}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}},
        ]

    class Response:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
            yield "data: [DONE]"

    class Client:
        @asynccontextmanager
        async def stream(self, *_args, **_kwargs):
            yield Response()

    async def post(url, **_kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json=copy.deepcopy(body))

    monkeypatch.setattr(llm_client, "get_llm_client", AsyncMock(return_value=Client()))
    monkeypatch.setattr(llm_client, "_llm_stream_transport_enabled", lambda: stream)
    monkeypatch.setattr(llm_client, "_preflight_credit_check", AsyncMock())
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)
    monkeypatch.setattr(llm_client, "_post_chat_with_reasoning_retry", post)
    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", AsyncMock(return_value=SimpleNamespace(
        api_key="sk-test-key-1234567890", base_url="https://api.openai.com/v1", provider="openai", source="byok",
    )))
    tools = [{"type": "function", "function": {"name": "patch_file", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "Update the report"}]

    async def complete():
        if provider == "anthropic":
            return await llm_client._anthropic_messages_completion(
                api_key="sk-ant-test-key-1234567890", base_url="https://api.anthropic.com/v1",
                model="anthropic/claude-sonnet-4.6", messages=messages, tools=tools, temperature=0, max_tokens=32,
            )
        return await llm_client.chat_completion_with_tools(messages, tools, model="gpt-5.5")

    first, second = (await complete())[1], (await complete())[1]
    assert first[0]["name"] == second[0]["name"] == "patch_file"
    assert first[0]["arguments"] == second[0]["arguments"] == arguments
    if provider_id:
        assert first[0]["id"] == second[0]["id"] == provider_id
    else:
        assert first[0]["id"] != second[0]["id"]
        assert all(call[0]["id"].startswith("call_") and len(call[0]["id"]) == 37 for call in (first, second))
