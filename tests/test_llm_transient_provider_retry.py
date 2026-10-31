"""Transient model-provider failures are retried once, centrally, in llm_client.

2026-10-14 local eval: workflow run 01M4WH0RQ4G6BAMRAKMPSGKF3H died at step
plan_canvas after ~7 minutes of work. The BYOK gateway had answered HTTP 200,
streamed for 12 s, then sent an SSE error frame ("Upstream HTTP/2 stream
failed"). That frame skipped the buffered fallback and nothing retried it, so
one dropped stream failed the step and told the owner to check a key, model
and base URL that every run before and after used successfully.

These tests drive the real stream/buffered transport with a scripted provider.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from packages.core.ai import llm_client, provider_retry
from packages.core.ai.provider_retry import (
    LLMEmptyResponseError,
    LLMStreamProviderError,
    classify_provider_failure,
)
from packages.core.ai.runtime.output_policy import (
    BYOK_PROVIDER_ERROR_CODE,
    PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE,
    PUBLIC_BYOK_PROVIDER_UNAVAILABLE_MESSAGE,
)

GATEWAY = "https://apitokengate.com/v1"
MODEL = "gpt-6.1-sol"


def _request() -> httpx.Request:
    return httpx.Request("POST", f"{GATEWAY}/chat/completions")


def _sse(*frames: object) -> httpx.Response:
    body = "".join(
        f"data: {frame if isinstance(frame, str) else json.dumps(frame)}\n\n" for frame in frames
    )
    return httpx.Response(200, content=body.encode(), request=_request())


def _status(code: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(code, text="provider body", headers=headers, request=_request())


TEXT_DONE = _sse(
    {"choices": [{"delta": {"content": "canvas planned"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
    "[DONE]",
)
STREAM_DROPPED = {"error": {"message": "Upstream HTTP/2 stream failed"}}


class ScriptedProvider:
    """Replays one outcome per streaming request: a response, or an exception.

    ``buffered`` scripts the non-streaming POSTs; by default there are none,
    because a transient stream failure must re-stream instead.
    """

    def __init__(self, *outcomes: object, buffered: tuple = ()) -> None:
        self.outcomes = list(outcomes)
        self.buffered = list(buffered)
        self.requests = 0
        self.posts = 0

    def stream(self, *_args, **_kwargs):
        provider = self

        class _Stream:
            async def __aenter__(self):
                provider.requests += 1
                outcome = provider.outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            async def __aexit__(self, *_exc):
                return False

        return _Stream()

    async def post(self, *_args, **_kwargs):
        self.posts += 1
        if not self.buffered:
            raise AssertionError("transient stream failures must re-stream, not fall back to a buffered POST")
        outcome = self.buffered.pop(0)
        if outcome is HANG:
            await asyncio.Event().wait()
        return outcome


#: A buffered POST that never completes (a body uploading forever).
HANG = object()


class StalledStream:
    """A 200 stream that sends ``lines`` and then only silence.

    ``advance`` moves the retry budget's clock as if the stream deadline had
    run its full length, so budget arithmetic matches production numbers
    while the test itself waits only for the (tiny) configured deadline.
    """

    status_code = 200

    def __init__(self, *lines: str, clock: list[float], advance: float = 0.0) -> None:
        self.lines = lines
        self.clock = clock
        self.advance = advance

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        self.clock[0] += self.advance
        await asyncio.Event().wait()


@pytest.fixture
def provider(monkeypatch):
    sleeps: list[float] = []
    clock = [1000.0]

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    def install(*outcomes: object, buffered: tuple = (), source: str = "byok") -> ScriptedProvider:
        scripted = ScriptedProvider(*outcomes, buffered=buffered)
        route = SimpleNamespace(api_key="sk-" + "k" * 40, base_url=GATEWAY, provider="openai", source=source)
        monkeypatch.delenv("LLM_STREAM_TRANSPORT", raising=False)
        monkeypatch.delenv("LLM_TRANSIENT_RETRY_BUDGET_SECONDS", raising=False)
        monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", AsyncMock(return_value=route))
        monkeypatch.setattr(llm_client, "_preflight_credit_check", AsyncMock(return_value=None))
        monkeypatch.setattr(llm_client, "_validate_llm_key_model_compatibility", lambda *_args: None)
        monkeypatch.setattr(llm_client, "get_llm_client", AsyncMock(return_value=scripted))
        monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)
        monkeypatch.setattr(provider_retry.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(provider_retry, "_clock", lambda: clock[0])
        # Jitter at its ceiling: backoff waits are 2, 4, 8, 16, 30 s.
        monkeypatch.setattr(provider_retry.random, "uniform", lambda _low, high: high)
        scripted.sleeps = sleeps
        return scripted

    install.clock = clock
    return install


async def _call_with_tools(stream_handler=None):
    return await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "plan the canvas"}], [], model=MODEL, stream_handler=stream_handler,
    )


@pytest.mark.parametrize("failure", [
    pytest.param(_sse(STREAM_DROPPED), id="sse-error-frame-after-200"),
    pytest.param(httpx.ReadTimeout("no response", request=_request()), id="read-timeout"),
    pytest.param(httpx.RemoteProtocolError("peer closed connection"), id="connection-dropped"),
    pytest.param(_status(503), id="http-503"),
    pytest.param(_sse("[DONE]"), id="empty-stream"),
])
async def test_transient_failure_is_retried_then_succeeds(provider, failure):
    scripted = provider(failure, TEXT_DONE)

    content, tool_calls, usage = await _call_with_tools()

    assert (content, tool_calls) == ("canvas planned", None)
    assert not usage.get("error"), usage
    assert scripted.requests == 2
    assert len(scripted.sleeps) == 1


async def test_429_retry_after_is_honoured(provider):
    scripted = provider(_status(429, retry_after="3"), TEXT_DONE)

    content, _tool_calls, usage = await _call_with_tools()

    assert content == "canvas planned" and not usage.get("error")
    assert scripted.sleeps == [3.0]


@pytest.mark.parametrize("status", [401, 404])
async def test_permanent_failure_is_not_retried_and_points_to_settings(provider, status):
    # A stream rejection still gets the one buffered try it always had.
    scripted = provider(_status(status), TEXT_DONE, buffered=(_status(status),))

    _content, _tool_calls, usage = await _call_with_tools()

    assert (scripted.requests, scripted.posts) == (1, 1), "a rejected key or unknown model is not retried"
    assert scripted.sleeps == []
    assert usage["error_code"] == BYOK_PROVIDER_ERROR_CODE
    assert usage["error_public_detail"] == f"HTTP {status} from apitokengate.com (model {MODEL})"
    assert not usage.get("error_transient")


async def test_stream_that_never_starts_still_asks_the_buffered_request(provider):
    """A gateway can hold a rejected stream open; the buffered POST surfaces the real 400."""
    scripted = provider(
        llm_client.LLMStreamStartTimeout("LLM streaming response did not start within 60s"),
        buffered=(_status(400),),
    )

    _content, _tool_calls, usage = await _call_with_tools()

    assert (scripted.requests, scripted.posts) == (1, 1)
    assert usage["error_public_detail"] == f"HTTP 400 from apitokengate.com (model {MODEL})"
    assert not usage.get("error_transient")


async def test_exhausted_budget_reports_a_transient_outage_with_attempts(provider):
    scripted = provider(*[_sse(STREAM_DROPPED) for _ in range(6)])

    _content, _tool_calls, usage = await _call_with_tools()

    assert scripted.requests == 6
    assert usage["error_code"] == BYOK_PROVIDER_ERROR_CODE
    assert usage["error_transient"] is True
    assert usage["error_attempts"] == 6
    assert scripted.sleeps == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert usage["error_public_detail"] == (
        f"no response from apitokengate.com (model {MODEL}) after 6 attempts over 60s"
    )


async def test_time_budget_bounds_retries_before_the_attempt_cap(provider, monkeypatch):
    scripted = provider(*[_status(502) for _ in range(6)])
    monkeypatch.setenv("LLM_TRANSIENT_RETRY_BUDGET_SECONDS", "5")

    _content, _tool_calls, usage = await _call_with_tools()

    # Waits of 2 s then 4 s: the second would end past the 5 s budget.
    assert scripted.requests == 2
    assert scripted.sleeps == [2.0]
    assert usage["error_transient"] is True
    assert usage["error_attempts"] == scripted.requests


async def test_stream_failure_after_visible_output_is_not_replayed(provider):
    scripted = provider(
        _sse({"choices": [{"delta": {"content": "half an ans"}}]}, STREAM_DROPPED),
        TEXT_DONE,
    )
    events: list[tuple[str, dict]] = []

    async def handler(name: str, payload: dict) -> None:
        events.append((name, payload))

    _content, _tool_calls, usage = await _call_with_tools(stream_handler=handler)

    assert scripted.requests == 1, "a re-run would stream the answer to the user a second time"
    assert [name for name, _ in events] == ["text_delta", "text_reset"]
    assert usage["error_code"] == BYOK_PROVIDER_ERROR_CODE


async def test_stream_failure_before_visible_output_is_retried_for_chat(provider):
    scripted = provider(_sse(STREAM_DROPPED), TEXT_DONE)
    events: list[str] = []

    async def handler(name: str, _payload: dict) -> None:
        events.append(name)

    content, _tool_calls, usage = await _call_with_tools(stream_handler=handler)

    assert content == "canvas planned" and not usage.get("error")
    assert scripted.requests == 2
    assert events == ["text_delta"]


# 2026-10-20 local eval, run 01M5BMS75HKZJGDYC0JESX0NFG: an agent step's stream
# dropped 3 s after its first token. Workflow and Task tool rounds stream only
# for the deadlines, into a handler that shows no one anything, yet that token
# counted as committed output and the step failed after one attempt.

def _partial_then(frame: object) -> httpx.Response:
    return _sse({"choices": [{"delta": {"content": "half a pl"}}]}, frame)


async def _agentic_tool_round():
    from packages.core.ai.runtime.agentic_llm import runtime_execute_agentic_round_tool_completion

    return await runtime_execute_agentic_round_tool_completion(
        [{"role": "user", "content": "plan the canvas"}], [], temperature=0.2, model=MODEL,
    )


async def test_agentic_round_retries_a_drop_after_output_nobody_saw(provider):
    scripted = provider(_partial_then(STREAM_DROPPED), TEXT_DONE)

    content, _tool_calls, usage = await _agentic_tool_round()

    assert content == "canvas planned" and not usage.get("error"), usage
    assert (scripted.requests, scripted.posts) == (2, 0)


async def test_agentic_round_does_not_retry_a_permanent_failure_after_unseen_output(provider):
    rejected = {"error": {"message": "This model's maximum context length is 128000 tokens"}}
    scripted = provider(_partial_then(rejected), TEXT_DONE, buffered=(_status(400),))

    _content, _tool_calls, usage = await _agentic_tool_round()

    assert usage.get("error") and not usage.get("error_transient"), usage
    assert scripted.requests == 1 and scripted.sleeps == []


# 2026-10-17 local eval, Task 01M53JABTT5REC52J0MDZNATYY: single model calls
# through this gateway stayed open 10-58 minutes before anything counted as a
# failure, and only then did the retry budget start.

KEEPALIVES = (": keep-alive", "", 'data: {"choices":[{"delta":{}}]}')


async def test_stream_without_a_first_token_is_cut_at_the_deadline_and_restreamed(provider, monkeypatch):
    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "0.05")
    scripted = provider(StalledStream(*KEEPALIVES, clock=provider.clock), TEXT_DONE)

    content, _tool_calls, usage = await _call_with_tools()

    assert content == "canvas planned" and not usage.get("error")
    assert (scripted.requests, scripted.posts) == (2, 0), "a stall re-streams; it never waits on a buffered POST"


async def test_stream_stalled_after_visible_output_surfaces_without_a_replay(provider, monkeypatch):
    monkeypatch.setenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "0.05")
    scripted = provider(
        StalledStream('data: {"choices":[{"delta":{"content":"half an ans"}}]}', *KEEPALIVES, clock=provider.clock),
        TEXT_DONE,
    )
    events: list[str] = []

    async def handler(name: str, _payload: dict) -> None:
        events.append(name)

    _content, _tool_calls, usage = await _call_with_tools(stream_handler=handler)

    assert (scripted.requests, scripted.posts) == (1, 0)
    assert events == ["text_delta", "text_reset"]
    assert usage["error_code"] == BYOK_PROVIDER_ERROR_CODE


async def test_stalled_attempts_spend_the_budget_from_the_first_attempt(provider, monkeypatch):
    """Two 120 s stalls end the call: 242 s in, a third attempt would start past a 150 s budget.

    Measured from the first failure instead, the budget would allow a third stall.
    """
    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "0.05")
    stalls = [StalledStream(*KEEPALIVES, clock=provider.clock, advance=120.0) for _ in range(6)]
    scripted = provider(*stalls)
    monkeypatch.setenv("LLM_TRANSIENT_RETRY_BUDGET_SECONDS", "150")

    _content, _tool_calls, usage = await _call_with_tools()

    assert scripted.requests == 2
    assert scripted.sleeps == [2.0]
    assert usage["error_transient"] is True
    assert usage["error_public_detail"] == (
        f"no response from apitokengate.com (model {MODEL}) after 2 attempts over 242s"
    )


async def test_buffered_request_is_bounded_as_a_whole_and_retried(provider, monkeypatch):
    """The eval's buffered fallback ran 52 minutes before httpx's per-write timeout fired."""
    monkeypatch.setenv("LLM_REQUEST_DEADLINE_SECONDS", "0.05")
    scripted = provider(
        llm_client.LLMStreamStartTimeout("LLM streaming response did not start within 60s"),
        buffered=(HANG, httpx.Response(200, json={
            "choices": [{"message": {"content": "canvas planned"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }, request=_request())),
    )

    content, _tool_calls, usage = await _call_with_tools()

    assert content == "canvas planned" and not usage.get("error")
    assert (scripted.requests, scripted.posts) == (1, 2)


@pytest.mark.parametrize("exc, transient", [
    (httpx.ConnectError("dns"), True),
    (httpx.ReadTimeout("slow"), True),
    (TimeoutError("LLM streaming response stalled for 120s without a chunk"), True),
    (provider_retry.LLMStreamStalled("stalled", phase="first_token", elapsed=120.0, bytes_received=0), True),
    (provider_retry.LLMRequestDeadlineExceeded("did not complete"), True),
    (LLMEmptyResponseError("empty"), True),
    (LLMStreamProviderError("Our servers are currently overloaded. Please try again later."), True),
    (LLMStreamProviderError("Overloaded", error_type="overloaded_error"), True),
    (LLMStreamProviderError("Provider returned error", code=502), True),
    (LLMStreamProviderError("Provider returned error: input_schema does not support anyOf", code=400), False),
    (LLMStreamProviderError("This model's maximum context length is 128000 tokens"), False),
    (LLMStreamProviderError("blocked", error_type="invalid_request_error"), False),
    (httpx.UnsupportedProtocol("Request URL is missing an 'http://' or 'https://' protocol."), False),
    (ValueError("bug in our code"), False),
])
def test_failure_classification(exc, transient):
    assert classify_provider_failure(exc).transient is transient


@pytest.mark.parametrize("status, transient", [
    (429, True), (504, True), (524, True), (529, True),
    (400, False), (403, False), (413, False), (422, False),
])
def test_http_status_classification(status, transient):
    error = httpx.HTTPStatusError("x", request=_request(), response=_status(status))
    assert classify_provider_failure(error).transient is transient


async def test_workflow_llm_step_survives_one_dropped_stream(provider, monkeypatch):
    """The incident's step, end to end through the real Workflow agent loop."""
    from packages.core.ai import workflow_runner as module
    from packages.core.ai.workflow_runner import WorkflowRunner

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    scripted = provider(_sse(STREAM_DROPPED), TEXT_DONE)

    result = await WorkflowRunner()._execute_agent_step(
        {"id": "plan_canvas", "type": "llm", "config": {"prompt": "Plan the canvas"}}, {}, "entity-1",
    )

    assert result["status"] == "completed", result
    assert result["output"] == "canvas planned"
    assert scripted.requests == 2


async def test_workflow_step_outage_is_reported_as_an_outage(provider, monkeypatch):
    from packages.core.ai import workflow_runner as module
    from packages.core.ai.workflow_runner import WorkflowRunner

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    provider(*[_sse(STREAM_DROPPED) for _ in range(6)])

    result = await WorkflowRunner()._execute_agent_step(
        {"id": "plan_canvas", "type": "llm", "config": {"prompt": "Plan the canvas"}}, {}, "entity-1",
    )

    assert result["status"] == "failed"
    assert result["error"].startswith("Model provider outage:")
    assert PUBLIC_BYOK_PROVIDER_UNAVAILABLE_MESSAGE in result["error"]
    assert PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE not in result["error"]
    assert "Error detail: no response from apitokengate.com (model " in result["error"]
    assert result["error"].endswith(") after 6 attempts over 60s")
    assert not result.get("retry_blocked"), "Retry from this step must stay available"
