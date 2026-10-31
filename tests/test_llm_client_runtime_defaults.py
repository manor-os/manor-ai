from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest


def test_llm_client_defaults_are_safe_for_user_facing_chat() -> None:
    from packages.core.ai import llm_client

    assert llm_client.DEFAULT_LLM_TIMEOUT <= 300
    assert llm_client.DEFAULT_LLM_STREAM_IDLE_TIMEOUT <= 45
    assert llm_client._DEFAULT_TOOL_CALL_MAX_TOKENS <= 16384


def test_worker_stream_idle_timeout_is_longer_than_chat(monkeypatch) -> None:
    from packages.core.ai import llm_client

    monkeypatch.delenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_STREAM_IDLE_TIMEOUT", raising=False)
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "chat")
    assert llm_client.get_llm_stream_idle_timeout() == llm_client.DEFAULT_LLM_STREAM_IDLE_TIMEOUT

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    assert llm_client.get_llm_stream_idle_timeout() == llm_client.DEFAULT_WORKER_LLM_STREAM_IDLE_TIMEOUT
    assert llm_client.get_llm_stream_idle_timeout() >= 120

    monkeypatch.setenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "12")
    assert llm_client.get_llm_stream_idle_timeout() == 12.0


def test_stream_first_token_deadline_is_role_aware_and_overridable(monkeypatch) -> None:
    from packages.core.ai import llm_client

    monkeypatch.delenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "chat")
    chat = llm_client.get_llm_stream_first_token_timeout()
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    worker = llm_client.get_llm_stream_first_token_timeout()

    assert 0 < chat <= 120 < worker
    # Workers keep at least their previous pre-first-chunk wait (start + idle).
    assert worker >= llm_client.DEFAULT_LLM_STREAM_RESPONSE_TIMEOUT + llm_client.DEFAULT_WORKER_LLM_STREAM_IDLE_TIMEOUT

    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "0")
    assert llm_client.get_llm_stream_first_token_timeout() == 0.0


def test_model_chat_completion_compatibility_defaults(monkeypatch) -> None:
    from packages.core.ai import llm_client
    from packages.core.services import model_pricing

    catalog = {
        "openai/gpt-5.6-sol": ["none", "minimal", "low", "medium", "high"],
        "moonshotai/kimi-k3": ["none", "low", "high", "max"],
    }
    monkeypatch.setattr(model_pricing, "cached_reasoning_efforts", catalog.get)
    gateway = "https://ai-gateway.vercel.sh/v1"

    payload: dict = {}
    llm_client._apply_tool_reasoning_effort(
        payload, model="openai/gpt-5.6-sol", tools=[{"type": "function"}], base_url=gateway,
    )
    # Tool turns reason (staging: with none the model answered from memory).
    assert payload["reasoning_effort"] == llm_client._TOOL_REASONING_EFFORT == "medium"

    text_only_payload: dict = {}
    llm_client._apply_tool_reasoning_effort(
        text_only_payload, model="openai/gpt-5.6-sol", tools=[], base_url=gateway,
    )
    assert "reasoning_effort" not in text_only_payload

    explicit = {"reasoning_effort": "high"}
    llm_client._apply_tool_reasoning_effort(
        explicit, model="openai/gpt-5.6-sol", tools=[{"type": "function"}], base_url=gateway,
    )
    assert explicit["reasoning_effort"] == "high"

    # A provider-native route keeps the provider's own semantics: Kimi K3
    # natively always reasons at max, whatever the gateway catalog lists.
    native: dict = {}
    llm_client._apply_tool_reasoning_effort(
        native, model="moonshotai/kimi-k3", tools=[{"type": "function"}],
        base_url="https://api.moonshot.ai/v1",
    )
    assert "reasoning_effort" not in native

    older_model_payload: dict = {}
    llm_client._apply_chat_completion_compatibility(
        older_model_payload,
        model="openai/gpt-5.5",
        tools=[{"type": "function"}],
    )
    assert "reasoning_effort" not in older_model_payload

    astra_payload = {"temperature": 0.7, "reasoning_effort": "none"}
    llm_client._apply_chat_completion_compatibility(
        astra_payload,
        model="openai/gpt-6-astra",
        tools=[{"type": "function"}],
    )
    assert "temperature" not in astra_payload
    assert astra_payload["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_plain_chat_completion_forwards_explicit_reasoning_effort(monkeypatch) -> None:
    from packages.core.ai import llm_client

    captured: dict = {}

    async def fake_resolve_llm_routing_for_model(*_args, **_kwargs):
        return SimpleNamespace(
            api_key="sk-or-" + "o" * 32,
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            source="official",
        )

    async def fake_preflight_credit_check():
        return None

    async def fake_post(url, headers, payload, *, call_type):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "payload": dict(payload),
                "call_type": call_type,
            }
        )
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_routing_for_model",
        fake_resolve_llm_routing_for_model,
    )
    monkeypatch.setattr(llm_client, "_preflight_credit_check", fake_preflight_credit_check)
    monkeypatch.setattr(llm_client, "_post_chat_with_reasoning_retry", fake_post)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)

    content, usage = await llm_client.chat_completion(
        [{"role": "user", "content": "classify"}],
        model="anthropic/claude-fable-5",
        reasoning_effort="none",
    )

    assert content == "ok"
    assert usage["total"] == 2
    assert captured["payload"]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_kimi_byok_tool_call_uses_native_adapter(monkeypatch) -> None:
    from packages.core.ai import llm_client

    captured: dict = {}

    async def fake_resolve_llm_routing_for_model(*_args, **_kwargs):
        return SimpleNamespace(
            api_key="sk-" + "m" * 32,
            base_url="https://api.moonshot.ai/v1",
            provider="moonshotai",
            source="byok",
        )

    async def fake_preflight_credit_check():
        return None

    async def fake_post(url, headers, payload, *, call_type):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "payload": dict(payload),
                "call_type": call_type,
            }
        )
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_routing_for_model",
        fake_resolve_llm_routing_for_model,
    )
    monkeypatch.setattr(llm_client, "_preflight_credit_check", fake_preflight_credit_check)
    monkeypatch.setattr(llm_client, "_post_chat_with_reasoning_retry", fake_post)

    content, tool_calls, _usage = await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "hello"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look something up",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        model="moonshotai/kimi-k3",
    )

    assert content == "ok"
    assert tool_calls is None
    assert captured["url"] == "https://api.moonshot.ai/v1/chat/completions"
    assert captured["payload"]["model"] == "kimi-k3"
    assert captured["payload"]["max_completion_tokens"] == llm_client._DEFAULT_TOOL_CALL_MAX_TOKENS
    assert "max_tokens" not in captured["payload"]
    assert "temperature" not in captured["payload"]
    assert "reasoning_effort" not in captured["payload"]


@pytest.mark.asyncio
async def test_completion_entrypoints_canonicalize_model_before_vision_routing(monkeypatch) -> None:
    from packages.core.ai import llm_client

    vision_models: list[str] = []

    def fake_resolve_vision(model, _messages):
        vision_models.append(model)
        return model

    async def fake_resolve_routing(model, metadata=None):
        assert model == "openai/gpt-5.5"
        assert metadata == {"trace_id": "trace-1", "_resolved_model": "openai/gpt-5.5"}
        return SimpleNamespace(api_key="", base_url="", provider="openai", source="test")

    monkeypatch.setattr(llm_client, "_resolve_vision_model_if_needed", fake_resolve_vision)
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", fake_resolve_routing)

    await llm_client.chat_completion(
        [{"role": "user", "content": "hello"}],
        model="gpt-5.5",
        metadata={"trace_id": "trace-1"},
    )
    await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "hello"}],
        [],
        model="gpt-5.5",
        metadata={"trace_id": "trace-1"},
    )

    assert vision_models == ["openai/gpt-5.5", "openai/gpt-5.5"]


_STREAM_HEARTBEATS = {
    "comment-keepalive": [": keep-alive", ""],
    "ping-event": ['data: {"type": "ping"}', 'data: {"type": "heartbeat"}'],
    "empty-openai-delta": ['data: {"choices":[{"delta":{}}]}'],
}
_CONTENT_LINE = 'data: {"choices":[{"delta":{"content":"hi"}}]}'


class _ScriptedLines:
    """Yields ``lines`` with a short pause before each; heartbeats keep the socket busy."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    async def aiter_lines(self):
        for line in self.lines:
            await asyncio.sleep(0.003)
            yield line


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", list(_STREAM_HEARTBEATS))
async def test_llm_stream_iterator_heartbeats_are_not_a_first_token(monkeypatch, heartbeat) -> None:
    from packages.core.ai import llm_client

    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "0.02")
    monkeypatch.setenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "60")

    with pytest.raises(TimeoutError, match="stalled") as exc_info:
        async for _line in llm_client._iter_stream_lines_with_idle_timeout(
            _ScriptedLines(_STREAM_HEARTBEATS[heartbeat] * 40)
        ):
            pytest.fail("a heartbeat must not be yielded as model output")

    assert exc_info.value.phase == "first_token"


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", list(_STREAM_HEARTBEATS))
async def test_llm_stream_iterator_heartbeats_do_not_reset_the_idle_deadline(monkeypatch, heartbeat) -> None:
    from packages.core.ai import llm_client

    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "0.02")
    yielded: list[str] = []

    with pytest.raises(TimeoutError, match="stalled") as exc_info:
        async for line in llm_client._iter_stream_lines_with_idle_timeout(
            _ScriptedLines([_CONTENT_LINE, *(_STREAM_HEARTBEATS[heartbeat] * 40)])
        ):
            yielded.append(line)

    assert yielded == [_CONTENT_LINE]
    assert exc_info.value.phase == "idle"


@pytest.mark.asyncio
async def test_llm_stream_iterator_bounds_a_steadily_streaming_response(monkeypatch) -> None:
    from packages.core.ai import llm_client

    monkeypatch.setenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LLM_STREAM_MAX_DURATION_SECONDS", "0.03")

    with pytest.raises(TimeoutError) as exc_info:
        async for _line in llm_client._iter_stream_lines_with_idle_timeout(_ScriptedLines([_CONTENT_LINE] * 100)):
            pass

    assert exc_info.value.phase == "max_duration"


@pytest.mark.asyncio
async def test_llm_post_retries_cloudflare_524(monkeypatch) -> None:
    from packages.core.ai import llm_client

    calls: list[str] = []
    sleeps: list[float] = []

    class FakeClient:
        async def post(self, url, **_kwargs):
            calls.append(url)
            request = httpx.Request("POST", url)
            if len(calls) == 1:
                return httpx.Response(524, text="<html>A timeout occurred</html>", request=request)
            return httpx.Response(200, json={"ok": True}, request=request)

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    async def fake_get_llm_client():
        return FakeClient()

    monkeypatch.setattr(llm_client, "get_llm_client", fake_get_llm_client)
    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)

    response = await llm_client._post_with_retry(
        "https://apitokengate.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        payload={"model": "gpt-5.5", "messages": []},
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert len(sleeps) == 1 and llm_client._BASE_DELAY / 2 <= sleeps[0] <= llm_client._BASE_DELAY


@pytest.mark.asyncio
async def test_llm_post_ignores_retry_after_on_cloudflare_524(monkeypatch) -> None:
    from packages.core.ai import llm_client

    calls: list[str] = []
    sleeps: list[float] = []

    class FakeClient:
        async def post(self, url, **_kwargs):
            calls.append(url)
            request = httpx.Request("POST", url)
            if len(calls) == 1:
                return httpx.Response(
                    524,
                    headers={"Retry-After": "120"},
                    text="<html>A timeout occurred</html>",
                    request=request,
                )
            return httpx.Response(200, json={"ok": True}, request=request)

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    async def fake_get_llm_client():
        return FakeClient()

    monkeypatch.setattr(llm_client, "get_llm_client", fake_get_llm_client)
    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)

    response = await llm_client._post_with_retry(
        "https://apitokengate.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        payload={"model": "gpt-5.5", "messages": []},
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert len(sleeps) == 1 and llm_client._BASE_DELAY / 2 <= sleeps[0] <= llm_client._BASE_DELAY


@pytest.mark.asyncio
async def test_llm_post_retries_cloudflare_524_only_once(monkeypatch) -> None:
    from packages.core.ai import llm_client

    calls: list[str] = []
    sleeps: list[float] = []

    class FakeClient:
        async def post(self, url, **_kwargs):
            calls.append(url)
            request = httpx.Request("POST", url)
            return httpx.Response(
                524,
                headers={"Retry-After": "120"},
                text="<html>A timeout occurred</html>",
                request=request,
            )

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    async def fake_get_llm_client():
        return FakeClient()

    monkeypatch.setattr(llm_client, "get_llm_client", fake_get_llm_client)
    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await llm_client._post_with_retry(
            "https://apitokengate.com/v1/chat/completions",
            headers={"Authorization": "Bearer sk-test"},
            payload={"model": "gpt-5.5", "messages": []},
        )

    assert exc_info.value.response.status_code == 524
    assert len(calls) == 2
    assert len(sleeps) == 1 and llm_client._BASE_DELAY / 2 <= sleeps[0] <= llm_client._BASE_DELAY


@pytest.mark.asyncio
async def test_chat_completion_skips_buffered_fallback_after_stream_provider_error(monkeypatch) -> None:
    from packages.core.ai import llm_client

    buffered_calls: list[str] = []
    stream_calls: list[str] = []
    error_chunk = {
        "error": {
            "message": "Service temporarily unavailable. The model's availability is currently degraded.",
        }
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield f"data: {json.dumps(error_chunk)}"

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_exc):
            return None

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            stream_calls.append("stream")
            return FakeStream()

        async def post(self, *_args, **_kwargs):
            raise AssertionError("buffered POST must not run after a stream provider error")

    async def fake_resolve_llm_routing_for_model(*_args, **_kwargs):
        return SimpleNamespace(
            api_key="sk-test-key-1234567890",
            base_url="https://apitokengate.com/v1",
            provider="openai",
            source="byok",
        )

    async def fake_preflight_credit_check():
        return None

    async def fake_get_llm_client():
        return FakeClient()

    async def fake_buffered(*_args, **_kwargs):
        buffered_calls.append("buffered")
        raise AssertionError("buffered fallback must not run after a stream provider error")

    async def fake_official_retry(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_routing_for_model",
        fake_resolve_llm_routing_for_model,
    )
    monkeypatch.setattr(llm_client, "_preflight_credit_check", fake_preflight_credit_check)
    monkeypatch.setattr(llm_client, "get_llm_client", fake_get_llm_client)
    monkeypatch.setattr(llm_client, "_post_chat_with_reasoning_retry", fake_buffered)
    monkeypatch.setattr(llm_client, "_retry_manor_official_call_via_openrouter", fake_official_retry)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)

    async def fake_stream_handler(_event_name: str, _payload: dict):
        return None

    content, usage = await llm_client.chat_completion(
        [{"role": "user", "content": "hello"}],
        model="grok-4.6",
        stream_handler=fake_stream_handler,
    )

    assert content == ""
    assert buffered_calls == []
    assert usage.get("error")
    # A degraded-availability frame is transient: the call re-streams within
    # the retry budget instead of hanging on a buffered POST.
    assert len(stream_calls) == llm_client._MAX_RETRIES + 1
    assert len(sleeps) == llm_client._MAX_RETRIES
    assert usage["error_transient"] is True


@pytest.mark.asyncio
async def test_chat_completion_fails_over_from_vercel_to_openrouter(monkeypatch) -> None:
    from packages.core.ai import llm_client
    from packages.core.services.model_gateway import ModelGatewayRoute

    calls: list[str] = []
    wire_models: list[str] = []

    async def fake_resolve_routing(*_args, **_kwargs):
        if llm_client._official_gateway_override.get("") == "openrouter":
            return ModelGatewayRoute(
                api_key="sk-or-" + "o" * 32,
                base_url="https://openrouter.ai/api/v1",
                provider="openrouter",
                source="official",
            )
        return ModelGatewayRoute(
            api_key="vck_" + "v" * 32,
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source="official",
        )

    async def fake_preflight():
        return None

    async def fake_post(url, headers, payload, *, call_type):
        calls.append(url)
        wire_models.append(payload["model"])
        request = httpx.Request("POST", url)
        if "ai-gateway.vercel.sh" in url:
            response = httpx.Response(503, text="gateway unavailable", request=request)
            raise httpx.HTTPStatusError(
                "Vercel unavailable",
                request=request,
                response=response,
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", fake_resolve_routing)
    monkeypatch.setattr(llm_client, "_preflight_credit_check", fake_preflight)
    monkeypatch.setattr(llm_client, "_post_chat_with_reasoning_retry", fake_post)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)

    content, usage = await llm_client.chat_completion(
        [{"role": "user", "content": "hello"}],
        model="qwen/qwen3.8-max",
    )

    assert content == "ok"
    assert usage["total"] == 2
    assert calls == [
        "https://ai-gateway.vercel.sh/v1/chat/completions",
        "https://openrouter.ai/api/v1/chat/completions",
    ]
    assert wire_models == ["alibaba/qwen3.8-max", "qwen/qwen3.8-max"]
    assert llm_client._official_gateway_override.get("") == ""


@pytest.mark.asyncio
async def test_tool_completion_fails_over_from_vercel_to_openrouter(monkeypatch) -> None:
    from packages.core.ai import llm_client
    from packages.core.services.model_gateway import ModelGatewayRoute

    calls: list[str] = []

    async def fake_resolve_routing(*_args, **_kwargs):
        if llm_client._official_gateway_override.get("") == "openrouter":
            return ModelGatewayRoute(
                api_key="sk-or-" + "o" * 32,
                base_url="https://openrouter.ai/api/v1",
                provider="openrouter",
                source="official",
            )
        return ModelGatewayRoute(
            api_key="vck_" + "v" * 32,
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source="official",
        )

    async def fake_preflight():
        return None

    async def fake_post(url, headers, payload, *, call_type):
        calls.append(url)
        request = httpx.Request("POST", url)
        if "ai-gateway.vercel.sh" in url:
            response = httpx.Response(503, text="gateway unavailable", request=request)
            raise httpx.HTTPStatusError(
                "Vercel unavailable",
                request=request,
                response=response,
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", fake_resolve_routing)
    monkeypatch.setattr(llm_client, "_preflight_credit_check", fake_preflight)
    monkeypatch.setattr(llm_client, "_post_chat_with_reasoning_retry", fake_post)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)

    content, tool_calls, usage = await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "hello"}],
        [],
        model="anthropic/claude-sonnet-4.6",
    )

    assert content == "ok"
    assert tool_calls is None
    assert usage["total"] == 2
    assert calls == [
        "https://ai-gateway.vercel.sh/v1/chat/completions",
        "https://openrouter.ai/api/v1/chat/completions",
    ]
    assert llm_client._official_gateway_override.get("") == ""


@pytest.mark.asyncio
async def test_vercel_byok_never_fails_over_to_platform_openrouter() -> None:
    from packages.core.ai import llm_client
    from packages.core.services.model_gateway import ModelGatewayRoute

    retry_called = False

    async def retry():
        nonlocal retry_called
        retry_called = True
        return "unexpected"

    result = await llm_client._retry_manor_official_call_via_openrouter(
        ModelGatewayRoute(
            api_key="vck_" + "u" * 32,
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source="byok",
        ),
        model="anthropic/claude-sonnet-4.6",
        call_type="chat_completion",
        retry=retry,
    )

    assert result is None
    assert retry_called is False


@pytest.mark.asyncio
async def test_openai_tool_stream_emits_text_delta_when_chunk_also_contains_tool_call(monkeypatch) -> None:
    from packages.core.ai import llm_client

    events: list[tuple[str, dict]] = []
    tool_chunk = {
        "choices": [
            {
                "delta": {
                    "content": "我先生成简历页面。",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {
                                "name": "generate_file",
                                "arguments": json.dumps({"kind": "code", "name": "personal_resume_html"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "model": "gpt-5.5",
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield f"data: {json.dumps(tool_chunk)}"
            yield "data: [DONE]"

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_exc):
            return None

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            return FakeStream()

    async def fake_stream_handler(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def fake_resolve_llm_routing_for_model(*_args, **_kwargs):
        return SimpleNamespace(
            api_key="sk-test-key-1234567890",
            base_url="https://api.openai.com/v1",
            provider="openai",
            source="byok",
        )

    async def fake_preflight_credit_check():
        return None

    async def fake_get_llm_client():
        return FakeClient()

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_routing_for_model",
        fake_resolve_llm_routing_for_model,
    )
    monkeypatch.setattr(llm_client, "_preflight_credit_check", fake_preflight_credit_check)
    monkeypatch.setattr(llm_client, "get_llm_client", fake_get_llm_client)

    content, tool_calls, usage = await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "生成简历页面"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "generate_file",
                    "description": "Generate a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        model="gpt-5.5",
        stream_handler=fake_stream_handler,
    )

    assert content == "我先生成简历页面。"
    assert tool_calls == [
        {
            "id": "call_1",
            "name": "generate_file",
            "arguments": {"kind": "code", "name": "personal_resume_html"},
        }
    ]
    assert usage["total"] == 15
    assert [event for event in events if event[0] == "text_delta"] == [
        ("text_delta", {"content": "我先生成简历页面。"}),
    ]
    # The tool call being written is announced as a display-only draft.
    assert [payload["name"] for kind, payload in events if kind == "tool_call_draft"] == ["generate_file"]


@pytest.mark.asyncio
async def test_custom_model_probe_checks_tool_call_shape(monkeypatch) -> None:
    from apps.api.routers import auth

    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
            return FakeResponse()

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)

    provider, latency_ms = await auth._probe_custom_model(
        "primary",
        "gpt-5.5",
        "sk-test",
        "https://example-gateway.test/v1",
    )

    assert provider == "openai"
    assert isinstance(latency_ms, int)
    assert len(calls) == 2
    assert calls[0]["json"]["messages"] == [{"role": "user", "content": "ping"}]
    assert "tools" not in calls[0]["json"]
    assert calls[1]["json"]["max_tokens"] == 32
    assert calls[1]["json"]["tool_choice"] == "auto"
    assert calls[1]["json"]["tools"][0]["function"]["name"] == "noop_probe"


@pytest.mark.asyncio
async def test_custom_model_probe_rejects_successful_html_page(monkeypatch) -> None:
    from apps.api.routers import auth
    from fastapi import HTTPException

    class FakeResponse:
        status_code = 200
        text = "<!doctype html><title>API Gateway</title>"

        def json(self):
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, url, *, json, headers):
            return FakeResponse()

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)

    with pytest.raises(HTTPException) as exc_info:
        await auth._probe_custom_model(
            "primary",
            "gpt-5.5",
            "sk-test",
            "https://example-gateway.test",
        )

    assert exc_info.value.status_code == 400
    assert "non-JSON response" in str(exc_info.value.detail)
    assert "/v1" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_custom_model_probe_accepts_anthropic_response_shape(monkeypatch) -> None:
    from apps.api.routers import auth

    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)

    provider, latency_ms = await auth._probe_custom_model(
        "primary",
        "claude-sonnet-4-5",
        "sk-ant-test",
        "https://api.anthropic.com/v1",
    )

    assert provider == "anthropic"
    assert isinstance(latency_ms, int)
    assert len(calls) == 2
    assert all(call["url"].endswith("/messages") for call in calls)
    assert calls[1]["json"]["tools"][0]["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_kimi_byok_probe_uses_native_adapter(monkeypatch) -> None:
    from apps.api.routers import auth

    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)

    provider, latency_ms = await auth._probe_custom_model(
        "primary",
        "moonshotai/kimi-k3",
        "sk-" + "m" * 32,
        "https://api.moonshot.cn/v1",
    )

    assert provider == "moonshotai"
    assert isinstance(latency_ms, int)
    assert len(calls) == 2
    assert all(call["url"] == "https://api.moonshot.cn/v1/chat/completions" for call in calls)
    assert all(call["json"]["model"] == "kimi-k3" for call in calls)
    assert all("max_tokens" not in call["json"] for call in calls)
    assert all("temperature" not in call["json"] for call in calls)
    assert calls[0]["json"]["max_completion_tokens"] == 1
    assert calls[1]["json"]["max_completion_tokens"] == 32
    assert all(call["json"]["reasoning_effort"] == "low" for call in calls)


def test_tool_reasoning_effort_follows_the_catalog_not_model_names(monkeypatch) -> None:
    from packages.core.ai import llm_client
    from packages.core.services import model_pricing

    catalog = {
        "vendor/new-reasoner": ["none", "low", "medium", "high"],
        "vendor/always-thinks": ["low", "medium", "high", "max"],
        "vendor/strong-only": ["high", "max"],
    }
    monkeypatch.setattr(model_pricing, "cached_reasoning_efforts", catalog.get)

    assert llm_client.tool_reasoning_effort("vendor/new-reasoner") == "medium"
    # A model that always reasons keeps its own default (Kimi K3: max).
    assert llm_client.tool_reasoning_effort("vendor/strong-only") is None
    assert llm_client.tool_reasoning_effort("vendor/always-thinks") is None
    # Not in any catalog, or no effort control: the provider default applies.
    assert llm_client.tool_reasoning_effort("vendor/plain-chat") is None
    payload: dict = {}
    llm_client._apply_tool_reasoning_effort(
        payload, model="vendor/plain-chat", tools=[{"type": "function"}],
        base_url="https://ai-gateway.vercel.sh/v1",
    )
    assert "reasoning_effort" not in payload

    monkeypatch.setattr(llm_client, "_TOOL_REASONING_EFFORT", "none")
    assert llm_client.tool_reasoning_effort("vendor/new-reasoner") == "none"
    monkeypatch.setattr(llm_client, "_TOOL_REASONING_EFFORT", "minimal")
    assert llm_client.tool_reasoning_effort("vendor/new-reasoner") == "low"  # nearest listed


def test_route_rejecting_tool_reasoning_is_downgraded_to_none_once(monkeypatch) -> None:
    import asyncio

    import httpx

    from packages.core.ai import llm_client

    base_url = "https://no-tool-reasoning.example/v1"
    request = httpx.Request("POST", f"{base_url}/chat/completions")
    payloads = []

    async def post(url, headers, payload):
        payloads.append(dict(payload))
        if payload.get("reasoning_effort") != "none":
            raise httpx.HTTPStatusError(
                "bad", request=request,
                response=httpx.Response(400, request=request, text='{"error":"reasoning with tools unsupported"}'),
            )
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    tool_payload = {"model": "gpt-5.6-terra", "messages": [], "tools": [{"type": "function"}], "reasoning_effort": "low"}
    response = asyncio.run(llm_client._post_chat_with_reasoning_retry(
        f"{base_url}/chat/completions", {}, dict(tool_payload), call_type="test",
    ))
    assert response.status_code == 200
    assert [p["reasoning_effort"] for p in payloads] == ["low", "none"]
    assert (base_url, "gpt-5.6-terra") in llm_client._TOOL_REASONING_DOWNGRADED_ROUTES
    # Later tool calls on the route go straight to none; text-only calls keep theirs.
    later = dict(tool_payload)
    llm_client._apply_reasoning_effort_route(later, base_url=f"{base_url}/chat/completions")
    assert later["reasoning_effort"] == "none"
    text_only = {"model": "gpt-5.6-terra", "reasoning_effort": "low"}
    llm_client._apply_reasoning_effort_route(text_only, base_url=f"{base_url}/chat/completions")
    assert text_only["reasoning_effort"] == "low"
    llm_client._TOOL_REASONING_DOWNGRADED_ROUTES.discard((base_url, "gpt-5.6-terra"))


def test_model_without_none_falls_back_to_omitting_the_effort(monkeypatch) -> None:
    import asyncio

    import httpx

    from packages.core.ai import llm_client

    base_url = "https://strict-thinker.example/v1"
    request = httpx.Request("POST", f"{base_url}/chat/completions")
    payloads = []

    async def post(url, headers, payload):
        payloads.append(dict(payload))
        if "reasoning_effort" in payload:
            raise httpx.HTTPStatusError(
                "bad", request=request,
                response=httpx.Response(400, request=request, text='{"error":"reasoning_effort not supported"}'),
            )
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    response = asyncio.run(llm_client._post_chat_with_reasoning_retry(
        f"{base_url}/chat/completions", {},
        {"model": "vendor/thinker", "messages": [], "tools": [{"type": "function"}], "reasoning_effort": "low"},
        call_type="test",
    ))
    assert response.status_code == 200
    assert [p.get("reasoning_effort") for p in payloads] == ["low", "none", None]
    assert (base_url, "vendor/thinker") not in llm_client._TOOL_REASONING_DOWNGRADED_ROUTES
    llm_client._REASONING_EFFORT_REJECTED_ROUTES.discard((base_url, "vendor/thinker"))


def test_switch_and_budget_models_get_unified_reasoning_on_gateways(monkeypatch) -> None:
    from packages.core.ai import llm_client
    from packages.core.services import model_pricing

    controls = {
        "vendor/budget-thinker": {"efforts": None, "toggle": True, "budget": {"min": 1, "max": 38912}},
        "vendor/small-budget": {"efforts": None, "toggle": False, "budget": {"min": 1, "max": 1024}},
        "vendor/switch-only": {"efforts": None, "toggle": True, "budget": None},
        "vendor/always-on": {"efforts": ["low", "high"], "toggle": False, "budget": None},
    }
    empty = {"efforts": None, "toggle": False, "budget": None}
    monkeypatch.setattr(model_pricing, "cached_reasoning_efforts", lambda model: (controls.get(model) or empty)["efforts"])
    monkeypatch.setattr(model_pricing, "cached_reasoning_controls", lambda model: controls.get(model, empty))
    tools = [{"type": "function"}]
    vercel, openrouter, direct = "https://ai-gateway.vercel.sh/v1", "https://openrouter.ai/api/v1", "https://api.openai.com/v1"

    def shaped(model, base_url):
        payload: dict = {}
        llm_client._apply_tool_reasoning_effort(payload, model=model, tools=tools, base_url=base_url)
        return payload

    assert shaped("vendor/budget-thinker", vercel) == {"reasoning": {"enabled": True, "max_tokens": 2048}}
    assert shaped("vendor/small-budget", openrouter) == {"reasoning": {"enabled": True, "max_tokens": 1024}}
    assert shaped("vendor/switch-only", vercel) == {"reasoning": {"enabled": True}}
    # A model that always reasons, a direct provider route, or a model no
    # catalog knows: nothing is sent.
    assert shaped("vendor/always-on", vercel) == {}
    assert shaped("vendor/budget-thinker", direct) == {}
    assert shaped("vendor/unknown", vercel) == {}
    monkeypatch.setattr(llm_client, "_TOOL_REASONING_EFFORT", "none")
    assert shaped("vendor/switch-only", vercel) == {}


def test_route_rejecting_the_reasoning_object_drops_it_and_remembers(monkeypatch) -> None:
    import asyncio

    import httpx

    from packages.core.ai import llm_client

    base_url = "https://no-unified-reasoning.example/v1"
    request = httpx.Request("POST", f"{base_url}/chat/completions")
    payloads = []

    async def post(url, headers, payload):
        payloads.append(dict(payload))
        if "reasoning" in payload:
            raise httpx.HTTPStatusError("bad", request=request, response=httpx.Response(
                400, request=request, text='{"error":"Unknown parameter: reasoning"}'))
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    tool_payload = {"model": "vendor/switch", "messages": [], "tools": [{"type": "function"}],
                    "reasoning": {"enabled": True}}
    response = asyncio.run(llm_client._post_chat_with_reasoning_retry(
        f"{base_url}/chat/completions", {}, dict(tool_payload), call_type="test",
    ))
    assert response.status_code == 200
    assert ["reasoning" in p for p in payloads] == [True, False]
    later = dict(tool_payload)
    llm_client._apply_reasoning_effort_route(later, base_url=f"{base_url}/chat/completions")
    assert "reasoning" not in later
    llm_client._REASONING_EFFORT_REJECTED_ROUTES.discard((base_url, "vendor/switch"))
