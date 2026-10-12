"""Streaming diagnostics distinguish upstream empties from malformed arguments."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai import llm_client


@pytest.mark.asyncio
@pytest.mark.parametrize("parts,state,expected", [
    ([""], "absent", {}),
    (["{}"], "empty_object", {}),
    (["[]"], "non_object", []),
    (['{"query":'], "invalid_json", {"_raw": '{"query":'}),
    (['{"que', 'ry":"松果', ' private-value"}'], "parsed", {"query": "松果 private-value"}),
])
async def test_stream_keeps_arguments_and_records_only_shape(monkeypatch, caplog, parts, state, expected):
    caplog.set_level(logging.INFO, logger=llm_client.logger.name)
    schema = {"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    }}

    class Response:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for n, part in enumerate(parts):
                # Also interleave a second tool: its arguments must not be
                # accidentally attached to the first call's stream index.
                tc = {"index": 0, "function": {"arguments": part}}
                if n == 0:
                    tc.update(id="call_0")
                    tc["function"]["name"] = "lookup"
                yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [tc]}}]})
                if n == 0:
                    yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{
                        "index": 1, "id": "call_1", "function": {"name": "lookup", "arguments": '{"query":"second"}'},
                    }]}}]})
            yield "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                                        "usage": {"prompt_tokens": 11, "completion_tokens": 12, "total_tokens": 23}})
            yield "data: [DONE]"

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            pass

    class Client:
        def stream(self, *_args, **kwargs):
            assert kwargs["json"]["tools"][0]["function"]["parameters"] == schema["function"]["parameters"]
            return Stream()

    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", AsyncMock(return_value=SimpleNamespace(
        api_key="sk-test-key-1234567890", base_url="https://api.openai.com/v1", provider="openai", source="byok",
    )))
    monkeypatch.setattr(llm_client, "_preflight_credit_check", AsyncMock())
    monkeypatch.setattr(llm_client, "get_llm_client", AsyncMock(return_value=Client()))
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)
    monkeypatch.setenv("LLM_STREAM_TRANSPORT", "1")
    _, calls, usage = await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "Run lookup"}], [schema], model="gpt-test",
    )
    assert calls[0]["arguments"] == expected
    assert calls[1]["arguments"] == {"query": "second"}
    diagnostics = usage["tool_argument_diagnostics"]
    assert diagnostics[0] == {"tool_index": 0, "argument_chars": len("".join(parts)),
                              "argument_fragments": sum(bool(p) for p in parts), "state": state}
    assert diagnostics[1]["state"] == "parsed"
    assert "private-value" not in json.dumps(diagnostics)
    diagnostic_logs = [record.getMessage() for record in caplog.records if "[tool-arguments]" in record.getMessage()]
    assert len(diagnostic_logs) == 1
    assert state in diagnostic_logs[0]
    assert "private-value" not in diagnostic_logs[0]
    assert "sk-test-key" not in diagnostic_logs[0]
    assert usage["total"] == 23
