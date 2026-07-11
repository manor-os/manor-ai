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
