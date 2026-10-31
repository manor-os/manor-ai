"""Structured reply constraints survive the real provider transport paths.

HTTP is mocked; these tests prove request propagation and unchanged response
assembly, not that a live model accepts or obeys a particular schema.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from packages.core.ai import llm_client


pytestmark = pytest.mark.unit
_MESSAGES = [{"role": "user", "content": "Return a synthetic test reply."}]
_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "transport_test_reply", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "required": ["response"],
            "properties": {"response": {
                "type": "object", "additionalProperties": False,
                "required": ["text"], "properties": {"text": {"type": "string"}},
            }},
        },
    },
}
_REPLY = '{"response":{"text":"Synthetic reply"}}'
_TOOLS = [{"type": "function", "function": {
    "name": "synthetic_lookup", "description": "Synthetic test tool",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
}}]
_CALL = {"id": "synthetic_call", "name": "synthetic_lookup", "arguments": {"query": "probe"}}


def _provider_response(native: bool, with_tools: bool) -> dict:
    if native:
        blocks = [{"type": "text", "text": _REPLY}]
        if with_tools:
            blocks.append({"type": "tool_use", "id": _CALL["id"],
                           "name": _CALL["name"], "input": _CALL["arguments"]})
        return {"content": blocks, "stop_reason": "tool_use" if with_tools else "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5}}
    message = {"content": _REPLY}
    if with_tools:
        message["tool_calls"] = [{"id": _CALL["id"], "type": "function", "function": {
            "name": _CALL["name"], "arguments": json.dumps(_CALL["arguments"]),
        }}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if with_tools else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _stream_response(native: bool, with_tools: bool, *, broken: bool = False) -> str:
    if native:
        frames = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": _REPLY}},
        ]
        if with_tools:
            frames.extend([
                {"type": "content_block_start", "index": 1, "content_block": {
                    "type": "tool_use", "id": _CALL["id"], "name": _CALL["name"], "input": {},
                }},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(_CALL["arguments"])}},
            ])
        frames.append({"type": "message_delta", "delta": {
            "stop_reason": "tool_use" if with_tools else "end_turn",
        }, "usage": {"output_tokens": 5}})
    else:
        delta = {"content": _REPLY}
        if with_tools:
            delta["tool_calls"] = [{"index": 0, "id": _CALL["id"], "type": "function", "function": {
                "name": _CALL["name"], "arguments": json.dumps(_CALL["arguments"]),
            }}]
        frames = [{"choices": [{"delta": delta, "finish_reason": "tool_calls" if with_tools else "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
    return "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + (
        "data: invalid-json\n\n" if broken else "data: [DONE]\n\n"
    )


@pytest_asyncio.fixture
async def transport(monkeypatch):
    # These tests are about format/compatibility retries, not the model
    # catalog: no synced reasoning efforts, so no reasoning_effort is added.
    from packages.core.services import model_pricing

    monkeypatch.setattr(model_pricing, "cached_reasoning_efforts", lambda _model: None)
    monkeypatch.setattr(
        model_pricing, "cached_reasoning_controls",
        lambda _model: {"efforts": None, "toggle": False, "budget": None},
    )
    state = SimpleNamespace(provider="vercel", source="official", requests=[], records=[], events=[])
    clients = []
    routes = {
        "vercel": ("https://ai-gateway.vercel.sh/v1", "sk-test-gateway-" + "x" * 32),
        "openai": ("https://api.openai.com/v1", "sk-proj-synthetic-test-key"),
        "anthropic": ("https://api.anthropic.com/v1", "sk-ant-synthetic-test-key"),
        "openrouter": ("https://openrouter.ai/api/v1", "sk-or-synthetic-test-key"),
    }

    async def resolve(*_args, **_kwargs):
        provider = llm_client._official_gateway_override.get("") or state.provider
        base_url, api_key = routes[provider]
        return SimpleNamespace(api_key=api_key, base_url=base_url, provider=provider, source=state.source)

    def install(handler):
        def capture(request):
            state.requests.append({"url": str(request.url), "payload": json.loads(request.content)})
            return handler(request, state.requests[-1]["payload"])

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        clients.append(client)
        monkeypatch.setattr(llm_client, "get_llm_client", AsyncMock(return_value=client))

    async def emit(event, payload):
        state.events.append((event, payload))

    state.install = install
    state.emit = emit
    state.preflight = AsyncMock()
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", resolve)
    monkeypatch.setattr(llm_client, "_preflight_credit_check", state.preflight)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **record: state.records.append(record))
    monkeypatch.setattr(llm_client, "_MAX_RETRIES", 0)
    monkeypatch.setattr(llm_client, "_PROMPT_CACHE_KEY_REJECTED_BASE_URLS", set(), raising=False)
    monkeypatch.setattr(llm_client, "_REASONING_EFFORT_REJECTED_ROUTES", set(), raising=False)
    monkeypatch.setenv("LLM_STREAM_TRANSPORT", "0")
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    yield state
    for client in clients:
        await client.aclose()


async def _complete(transport, with_tools: bool, *, streaming: bool = False, response_format=_FORMAT):
    kwargs = {"model": "openai/gpt-5.5" if transport.provider == "openai" else "anthropic/claude-sonnet-4.6",
              "max_tokens": 128, "response_format": response_format,
              "stream_handler": transport.emit if streaming else None}
    if with_tools:
        return await llm_client.chat_completion_with_tools(_MESSAGES, _TOOLS, **kwargs)
    content, usage = await llm_client.chat_completion(_MESSAGES, **kwargs)
    return content, None, usage


def _assert_format(request: dict, *, native: bool):
    payload = request["payload"]
    if native:
        assert request["url"].endswith("/messages")
        assert "response_format" not in payload
        assert payload["output_config"] == {"format": {
            "type": "json_schema", "schema": _FORMAT["json_schema"]["schema"],
        }}
    else:
        assert request["url"].endswith("/chat/completions")
        assert payload["response_format"] == _FORMAT
        assert "output_config" not in payload


def _assert_streamed_tool_drafts(events, *, native: bool, expected: bool):
    drafts = [payload for event, payload in events if event == "tool_call_draft"]
    if not expected:
        assert drafts == []
        return
    assert drafts
    for draft in drafts:
        assert draft["index"] == (1 if native else 0)
        assert draft["id"] == _CALL["id"]
        assert draft["name"] == _CALL["name"]
        assert json.dumps(_CALL["arguments"]).startswith(draft["arguments_text"])


@pytest.mark.parametrize("provider", ["vercel", "openai", "anthropic"])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_schema_reaches_provider_and_preserves_results(transport, provider, with_tools, streaming):
    transport.provider = provider
    native = provider == "anthropic"

    def reply(request, payload):
        if payload.get("stream"):
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  text=_stream_response(native, with_tools))
        return httpx.Response(200, request=request, json=_provider_response(native, with_tools))

    transport.install(reply)
    content, calls, usage = await _complete(transport, with_tools, streaming=streaming)

    assert content == _REPLY
    assert calls == ([_CALL] if with_tools else None)
    assert (usage["prompt"], usage["completion"], usage["total"]) == (10, 5, 15)
    assert len(transport.requests) == 1
    _assert_format(transport.requests[0], native=native)
    assert bool(transport.requests[0]["payload"].get("tools")) is with_tools
    assert transport.preflight.await_count == 1
    assert len(transport.records) == 1
    assert transport.records[0]["success"] is True
    assert transport.records[0]["usage"]["total"] == 15
    _assert_streamed_tool_drafts(transport.events, native=native, expected=streaming and with_tools)
    text_events = [event for event in transport.events if event[0] != "tool_call_draft"]
    assert text_events == ([("text_delta", {"content": _REPLY})] if streaming else [])


@pytest.mark.parametrize("provider", ["vercel", "anthropic"])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_buffered_fallback_keeps_schema_and_resets_partial_stream(transport, provider, with_tools):
    transport.provider = provider
    native = provider == "anthropic"

    def reply(request, payload):
        if payload.get("stream"):
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  text=_stream_response(native, with_tools, broken=True))
        return httpx.Response(200, request=request, json=_provider_response(native, with_tools))

    transport.install(reply)
    content, calls, usage = await _complete(transport, with_tools, streaming=True)

    assert content == _REPLY
    assert calls == ([_CALL] if with_tools else None)
    assert usage["total"] == 15
    assert len(transport.requests) == 2
    for request in transport.requests:
        _assert_format(request, native=native)
    _assert_streamed_tool_drafts(transport.events, native=native, expected=with_tools)
    text_events = [event for event in transport.events if event[0] != "tool_call_draft"]
    assert text_events[:2] == [("text_delta", {"content": _REPLY}), ("text_reset", {})]
    assert transport.preflight.await_count == 1
    assert len(transport.records) == 1
    assert transport.records[0]["success"] is True


@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("failure", ["http", "timeout", "other"])
async def test_official_gateway_recursive_retry_keeps_schema(transport, with_tools, failure):
    def reply(request, _payload):
        if request.url.host == "ai-gateway.vercel.sh":
            if failure == "http":
                return httpx.Response(400, request=request, json={"error": "Synthetic gateway failure"})
            if failure == "timeout":
                raise httpx.ReadTimeout("Synthetic timeout", request=request)
            raise ValueError("Synthetic transport failure")
        assert request.url.host == "openrouter.ai"
        return httpx.Response(200, request=request, json=_provider_response(False, with_tools))

    transport.install(reply)
    content, calls, usage = await _complete(transport, with_tools)

    assert content == _REPLY
    assert calls == ([_CALL] if with_tools else None)
    assert usage["total"] == 15
    assert len(transport.requests) == 2
    for request in transport.requests:
        _assert_format(request, native=False)
    assert transport.preflight.await_count == 2
    assert [record["success"] for record in transport.records] == [False, True]
    assert llm_client._official_gateway_override.get("") == ""


@pytest.mark.parametrize("with_tools", [False, True])
async def test_native_json_object_mode_retains_existing_unconstrained_behavior(transport, with_tools):
    transport.provider = "anthropic"
    transport.install(lambda request, _payload: httpx.Response(
        200, request=request, json=_provider_response(True, with_tools),
    ))
    content, _calls, usage = await _complete(transport, with_tools, response_format={"type": "json_object"})
    assert content == _REPLY
    assert usage["total"] == 15
    assert "output_config" not in transport.requests[0]["payload"]
    assert "response_format" not in transport.requests[0]["payload"]


@pytest.mark.parametrize("rejected", ["prompt_cache_key", "reasoning_effort"])
async def test_parameter_compatibility_retry_preserves_schema(transport, rejected):
    capability = "_apply_prompt_cache_key" if rejected == "prompt_cache_key" else "_reasoning_effort_route"
    if not hasattr(llm_client, capability):
        pytest.skip("Parent release predates this unrelated parameter compatibility path")
    def reply(request, payload):
        if rejected in payload:
            return httpx.Response(400, request=request, text=f"Unknown parameter: {rejected}")
        return httpx.Response(200, request=request, json=_provider_response(False, False))

    transport.install(reply)
    await llm_client._post_chat_with_reasoning_retry(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": "Bearer synthetic-test-key"},
        {"model": "openai/gpt-5.5", "messages": _MESSAGES, "response_format": _FORMAT, rejected: "test"},
        call_type="structured_transport_test",
    )
    assert len(transport.requests) == 2
    for request in transport.requests:
        _assert_format(request, native=False)
    assert rejected not in transport.requests[1]["payload"]


async def test_tool_model_fallback_retains_schema(transport, monkeypatch):
    transport.provider = "openai"
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "openai/gpt-5.5-fallback")

    def reply(request, _payload):
        if len(transport.requests) == 1:
            return httpx.Response(500, request=request, text="Synthetic primary model failure")
        return httpx.Response(200, request=request, json=_provider_response(False, True))

    transport.install(reply)
    content, calls, usage = await _complete(transport, True)
    assert content == _REPLY
    assert calls == [_CALL]
    assert usage["total"] == 15
    assert len(transport.requests) == 2
    for request in transport.requests:
        _assert_format(request, native=False)
    assert transport.requests[1]["payload"]["model"] == "openai/gpt-5.5-fallback"


@pytest.mark.parametrize("provider", ["vercel", "anthropic"])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_explicit_unsupported_format_retries_once_with_usage_diagnostic(
    transport, provider, with_tools, streaming,
):
    transport.provider = provider
    native = provider == "anthropic"

    def reply(request, payload):
        if "response_format" in payload or "output_config" in payload:
            parameter = "output_config.format" if native else "response_format"
            return httpx.Response(400, request=request, json={"error": {
                "param": parameter, "message": f"{parameter}: json_schema is not supported by this model",
            }})
        return httpx.Response(200, request=request, json=_provider_response(native, with_tools))

    transport.install(reply)
    content, calls, usage = await _complete(transport, with_tools, streaming=streaming)

    assert content == _REPLY
    assert calls == ([_CALL] if with_tools else None)
    assert (usage["prompt"], usage["completion"], usage["total"]) == (10, 5, 15)
    assert usage["response_format_fallback"] == {"type": "json_schema", "reason": "unsupported_format"}
    assert len(transport.requests) == 2
    _assert_format(transport.requests[0], native=native)
    retry_payload = transport.requests[1]["payload"]
    assert "response_format" not in retry_payload
    assert "output_config" not in retry_payload
    assert bool(retry_payload.get("tools")) is with_tools
    assert transport.preflight.await_count == 1
    assert len(transport.records) == 1
    assert transport.records[0]["success"] is True
    assert transport.records[0]["usage"]["response_format_fallback"] == usage["response_format_fallback"]
    assert transport.events == ([("text_delta", {"content": _REPLY})] if native and streaming else [])


@pytest.mark.parametrize("provider", ["vercel", "anthropic"])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_stream_format_downgrade_keeps_diagnostic_when_buffered_retry_fails(
    transport, provider, with_tools,
):
    transport.provider = provider
    transport.source = "byok"  # Isolate the downgrade from official gateway failover.
    native = provider == "anthropic"

    def reply(request, payload):
        if payload.get("stream"):
            parameter = "output_config.format" if native else "response_format"
            return httpx.Response(400, request=request, json={"error": {
                "param": parameter,
                "message": f"{parameter}: json_schema is not supported by this model",
            }})
        return httpx.Response(403, request=request, json={"error": {
            "type": "permission_error", "message": "Synthetic access denied after downgrade",
        }})

    transport.install(reply)
    _content, calls, usage = await _complete(transport, with_tools, streaming=True)

    assert calls is None and usage.get("error")
    assert usage["response_format_fallback"] == {
        "type": "json_schema", "reason": "unsupported_format",
    }
    assert len(transport.requests) == 2
    _assert_format(transport.requests[0], native=native)
    retry_payload = transport.requests[1]["payload"]
    assert "response_format" not in retry_payload and "output_config" not in retry_payload
    assert bool(retry_payload.get("tools")) is with_tools
    assert not retry_payload.get("stream")
    assert transport.preflight.await_count == 1
    assert len(transport.records) == 1 and transport.records[0]["success"] is False
    assert transport.events == []


@pytest.mark.parametrize("provider", ["vercel", "anthropic"])
@pytest.mark.parametrize("status,message", [
    (400, "Invalid schema for response_format: unsupported additionalProperties value"),
    (422, "json_schema contains an unsupported schema keyword"),
    (400, "output_config.format schema is too complex for compilation"),
    (400, "Malformed schema for response_format"),
    (400, "Authentication failed; response_format is unsupported"),
    (401, "response_format json_schema is not supported"),
    (403, "response_format json_schema is not supported"),
    (400, "Insufficient credits; response_format unsupported"),
    (402, "response_format json_schema is not supported"),
    (400, "Upstream error: 400"),
    (422, "Invalid tool arguments"),
    (500, "response_format json_schema is not supported"),
])
async def test_other_provider_failures_do_not_drop_schema(transport, provider, status, message):
    transport.provider = provider
    transport.source = "byok"  # Isolate parameter fallback from official route failover.
    transport.install(lambda request, _payload: httpx.Response(
        status, request=request, json={"error": {"message": message}},
    ))
    _content, calls, usage = await _complete(transport, True)
    assert calls is None
    assert usage.get("error")
    assert "response_format_fallback" not in usage
    assert len(transport.requests) == 1
    _assert_format(transport.requests[0], native=provider == "anthropic")
    assert transport.records[0]["success"] is False


@pytest.mark.parametrize("provider", ["vercel", "anthropic"])
async def test_unsupported_format_rejection_cannot_start_unbounded_retries(transport, provider):
    transport.provider = provider
    transport.source = "byok"
    transport.install(lambda request, _payload: httpx.Response(
        422, request=request, json={"error": {
            "param": "response_format", "code": "unsupported_value",
            "message": "This response_format value is unavailable",
        }},
    ))
    _content, _calls, usage = await _complete(transport, True)
    assert usage.get("error")
    assert len(transport.requests) == 2
    _assert_format(transport.requests[0], native=provider == "anthropic")
    assert "response_format" not in transport.requests[1]["payload"]
    assert "output_config" not in transport.requests[1]["payload"]
    assert usage["response_format_fallback"] == {"type": "json_schema", "reason": "unsupported_format"}


@pytest.mark.parametrize("provider", ["vercel", "anthropic"])
@pytest.mark.parametrize("error", [
    {"param": "output_config.format.schema", "type": "invalid_request_error",
     "message": "Unsupported keyword oneOf at #/"},
    {"param": "response_format.json_schema.schema.properties.text", "message": "Unsupported property"},
    {"param": "response_format", "type": "authentication_error", "message": "Unsupported response_format setting"},
    {"param": "response_format", "code": "invalid_json_schema", "message": "Unsupported schema option"},
    {"param": "response_format", "code": "access_denied", "message": "Unsupported response_format setting"},
    {"param": "output_config.effort", "message": "Unknown parameter: output_config.effort"},
])
async def test_schema_paths_and_typed_account_errors_cannot_downgrade_format(transport, provider, error):
    transport.provider = provider
    transport.source = "byok"
    transport.install(lambda request, _payload: httpx.Response(400, request=request, json={"error": error}))
    _content, _calls, usage = await _complete(transport, True)
    assert usage.get("error")
    assert "response_format_fallback" not in usage
    assert len(transport.requests) == 1
    _assert_format(transport.requests[0], native=provider == "anthropic")


@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("rejected", ["prompt_cache_key", "reasoning_effort"])
async def test_schema_downgrade_stays_monotonic_through_compatibility_retry(
    transport, monkeypatch, with_tools, streaming, rejected,
):
    capability = "_apply_prompt_cache_key" if rejected == "prompt_cache_key" else "_reasoning_effort_route"
    if not hasattr(llm_client, capability):
        pytest.skip("Parent release predates this unrelated parameter compatibility path")
    compatibility = llm_client._apply_chat_completion_compatibility

    def inject_rejected_parameter(payload, **kwargs):
        compatibility(payload, **kwargs)
        payload[rejected] = "test"

    monkeypatch.setattr(llm_client, "_apply_chat_completion_compatibility", inject_rejected_parameter)

    def reply(request, payload):
        if "response_format" in payload:
            return httpx.Response(400, request=request, json={"error": {
                "param": "response_format", "message": "json_schema is not supported by this model",
            }})
        if rejected in payload:
            return httpx.Response(400, request=request, text=f"Unknown parameter: {rejected}")
        return httpx.Response(200, request=request, json=_provider_response(False, with_tools))

    transport.install(reply)
    content, calls, usage = await _complete(transport, with_tools, streaming=streaming)

    assert content == _REPLY
    assert calls == ([_CALL] if with_tools else None)
    assert usage["total"] == 15
    assert usage["response_format_fallback"] == {"type": "json_schema", "reason": "unsupported_format"}
    # A tool call whose reasoning is refused retries at reasoning none (routes
    # that only refuse reasoning with tools), then without the parameter.
    downgrade = rejected == "reasoning_effort" and with_tools
    assert ["response_format" in request["payload"] for request in transport.requests] == (
        [True, False, False, False] if downgrade else [True, False, False]
    )
    assert [rejected in request["payload"] for request in transport.requests] == (
        [True, True, True, False] if downgrade else [True, True, False]
    )
    assert transport.preflight.await_count == 1
    assert len(transport.records) == 1
    assert transport.records[0]["usage"]["response_format_fallback"] == usage["response_format_fallback"]


async def test_reasoning_retry_keeps_format_diagnostic_when_preserving_original_error(transport):
    def reply(request, payload):
        if "reasoning_effort" in payload:
            return httpx.Response(400, request=request, text="Unknown parameter: reasoning_effort")
        if "response_format" in payload:
            return httpx.Response(400, request=request, json={"error": {
                "param": "response_format", "message": "json_schema is not supported by this model",
            }})
        return httpx.Response(503, request=request, text="Synthetic downstream failure")

    transport.install(reply)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await llm_client._post_chat_with_reasoning_retry(
            "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer synthetic-test-key"},
            {"model": "openai/gpt-5.5", "messages": _MESSAGES,
             "response_format": _FORMAT, "reasoning_effort": "test"},
            call_type="structured_transport_test",
        )
    # Preserve dev's choice of the original parameter failure as the primary error.
    assert caught.value.response.status_code == 400
    assert caught.value.response.text == "Unknown parameter: reasoning_effort"
    assert ["response_format" in item["payload"] for item in transport.requests] == [True, True, False]
    assert ["reasoning_effort" in item["payload"] for item in transport.requests] == [True, False, False]
    diagnostic = {"type": "json_schema", "reason": "unsupported_format"}
    assert caught.value.response_format_fallback == diagnostic
    usage = llm_client._byok_failure_usage(
        SimpleNamespace(source="official"), caught.value,
        model="openai/gpt-5.5", detail="Synthetic failed attempt",
    )
    assert usage["response_format_fallback"] == diagnostic
    assert not llm_client._REASONING_EFFORT_REJECTED_ROUTES
