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


def test_gpt56_chat_completion_tools_disable_reasoning() -> None:
    from packages.core.ai import llm_client

    payload: dict = {}
    llm_client._apply_chat_completion_tool_compatibility(
        payload,
        model="openai/gpt-5.6-sol",
        tools=[{"type": "function"}],
    )
    assert payload["reasoning_effort"] == "none"

    text_only_payload: dict = {}
    llm_client._apply_chat_completion_tool_compatibility(
        text_only_payload,
        model="openai/gpt-5.6-sol",
        tools=[],
    )
    assert "reasoning_effort" not in text_only_payload

    older_model_payload: dict = {}
    llm_client._apply_chat_completion_tool_compatibility(
        older_model_payload,
        model="openai/gpt-5.5",
        tools=[{"type": "function"}],
    )
    assert "reasoning_effort" not in older_model_payload


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


@pytest.mark.asyncio
async def test_llm_stream_iterator_fails_fast_when_provider_stalls(monkeypatch) -> None:
    from packages.core.ai import llm_client

    class SlowResponse:
        async def aiter_lines(self):
            await asyncio.sleep(0.05)
            yield "data: {}"

    monkeypatch.setenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "0.001")

    with pytest.raises(TimeoutError, match="stalled"):
        async for _line in llm_client._iter_stream_lines_with_idle_timeout(SlowResponse()):
            pass


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
    assert sleeps == [llm_client._BASE_DELAY]


@pytest.mark.asyncio
async def test_chat_completion_fails_over_from_vercel_to_openrouter(monkeypatch) -> None:
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

    content, usage = await llm_client.chat_completion(
        [{"role": "user", "content": "hello"}],
        model="anthropic/claude-sonnet-4.6",
    )

    assert content == "ok"
    assert usage["total"] == 2
    assert calls == [
        "https://ai-gateway.vercel.sh/v1/chat/completions",
        "https://openrouter.ai/api/v1/chat/completions",
    ]
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
    assert events == [("text_delta", {"content": "我先生成简历页面。"})]


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
