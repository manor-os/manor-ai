"""A streaming request that never starts must fail fast, not after the read timeout.

Seen with OpenRouter: an upstream 400 on a streaming request produced no
response headers at all, so the client sat in its 300 s read timeout before
the buffered fallback reported the real error in 4 s.
"""

from __future__ import annotations

import asyncio

import pytest

from packages.core.ai import llm_client


class _NeverStartsStream:
    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self):
        await asyncio.sleep(3600)

    async def __aexit__(self, *_exc):
        self.exited = True
        return False


class _FakeClient:
    def __init__(self, manager) -> None:
        self.manager = manager
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.manager


@pytest.mark.asyncio
async def test_stream_that_never_sends_headers_times_out_and_releases(monkeypatch) -> None:
    monkeypatch.setenv("LLM_STREAM_RESPONSE_TIMEOUT_SECONDS", "0.05")
    manager = _NeverStartsStream()
    client = _FakeClient(manager)

    with pytest.raises(TimeoutError) as exc_info:
        async with llm_client._open_llm_stream(client, "https://x/v1/chat/completions", headers={}, json={}):
            pytest.fail("the body must not run when the response never started")

    assert "did not start" in str(exc_info.value)
    assert manager.exited is True
    assert client.calls[0][:2] == ("POST", "https://x/v1/chat/completions")


class _StartsStream:
    def __init__(self) -> None:
        self.exit_args = None

    async def __aenter__(self):
        return "response"

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_args = (exc_type, exc)
        return False


@pytest.mark.asyncio
async def test_stream_that_starts_yields_the_response_and_propagates_body_errors(monkeypatch) -> None:
    monkeypatch.setenv("LLM_STREAM_RESPONSE_TIMEOUT_SECONDS", "5")
    manager = _StartsStream()
    client = _FakeClient(manager)

    async with llm_client._open_llm_stream(client, "u", headers={}, json={}) as response:
        assert response == "response"
    assert manager.exit_args == (None, None)

    manager = _StartsStream()
    client = _FakeClient(manager)
    with pytest.raises(RuntimeError, match="body failed"):
        async with llm_client._open_llm_stream(client, "u", headers={}, json={}):
            raise RuntimeError("body failed")
    assert manager.exit_args[0] is RuntimeError


def test_stream_response_timeout_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("LLM_STREAM_RESPONSE_TIMEOUT_SECONDS", raising=False)
    assert llm_client.get_llm_stream_response_timeout() == llm_client.DEFAULT_LLM_STREAM_RESPONSE_TIMEOUT
    monkeypatch.setenv("LLM_STREAM_RESPONSE_TIMEOUT_SECONDS", "12.5")
    assert llm_client.get_llm_stream_response_timeout() == 12.5


def test_sse_error_frames_surface_the_provider_reason() -> None:
    openrouter = {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {"raw": '{"type":"error","error":{"message":"input_schema does not support anyOf"}}'},
        }
    }
    with pytest.raises(llm_client.LLMStreamProviderError) as exc_info:
        llm_client._raise_on_stream_error_chunk(openrouter)
    assert "Provider returned error" in str(exc_info.value)
    assert "anyOf" in str(exc_info.value)

    anthropic = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    with pytest.raises(llm_client.LLMStreamProviderError, match="Overloaded"):
        llm_client._raise_on_stream_error_chunk(anthropic)

    # Ordinary chunks pass through untouched.
    llm_client._raise_on_stream_error_chunk({"choices": [{"delta": {"content": "hi"}}]})
    llm_client._raise_on_stream_error_chunk("not a dict")


def test_empty_error_markers_do_not_abort_a_healthy_stream() -> None:
    llm_client._raise_on_stream_error_chunk({"error": None, "choices": [{"delta": {"content": "x"}}]})
    llm_client._raise_on_stream_error_chunk({"error": "", "choices": []})
    llm_client._raise_on_stream_error_chunk({"error": {}, "choices": []})
    with pytest.raises(llm_client.LLMStreamProviderError):
        llm_client._raise_on_stream_error_chunk({"error": "rate limited"})
