"""Background LLM calls use streaming transport, so proxies can't kill them.

Production automation task 01KTKVZ6JG1QN3PF14RX9YMDQZ ("[Auto] Daily video
script queue") failed with "HTTP 524: apitokengate.com | A timeout occurred":
the entity's BYOK gateway sits behind Cloudflare, whose default origin
timeout is 100 seconds, and a buffered completion keeps the wire silent
until the whole generation is done. Any generation longer than the wall
524s — deterministically, so the retry loop just fails six times.

The streaming code path (assembly, tool-call stitching, usage capture,
buffered fallback) already existed for the chat UI; it was gated on
``stream_handler is not None``, which background calls never pass. The fix
is that gate: streaming transport is now the default for every call, with
LLM_STREAM_TRANSPORT=0 as the escape hatch.
"""
from __future__ import annotations

import pytest


# ── The gate ──────────────────────────────────────────────────────────


def test_stream_transport_defaults_on(monkeypatch):
    from packages.core.ai.llm_client import _llm_stream_transport_enabled

    monkeypatch.delenv("LLM_STREAM_TRANSPORT", raising=False)
    assert _llm_stream_transport_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", " OFF "])
def test_stream_transport_can_be_disabled(monkeypatch, value):
    from packages.core.ai.llm_client import _llm_stream_transport_enabled

    monkeypatch.setenv("LLM_STREAM_TRANSPORT", value)
    assert _llm_stream_transport_enabled() is False


def test_both_completion_paths_stream_without_a_handler():
    """The gate flip itself: both completion functions must take the stream
    path when no handler is present. A revert to ``stream_handler is not
    None`` would silently put every background call back behind the wall."""
    import inspect

    from packages.core.ai import llm_client

    for fn in (llm_client.chat_completion, llm_client.chat_completion_with_tools):
        source = inspect.getsource(fn)
        assert "stream_handler is not None or _llm_stream_transport_enabled()" in source, (
            f"{fn.__name__} no longer streams for background (handler-less) calls"
        )


# ── Billing survives gateways that ignore include_usage ───────────────


def test_missing_stream_usage_is_estimated_not_zero():
    """We ask for stream_options.include_usage, but an OpenAI-compatible
    gateway is free to ignore it. Recording zero would make every call
    through such a gateway free."""
    from packages.core.ai.llm_client import EMPTY_USAGE, _backfill_streamed_usage

    usage = EMPTY_USAGE.copy()
    messages = [{"role": "user", "content": "write a 500 word script about cats"}]
    _backfill_streamed_usage(
        usage, messages=messages, content="Once upon a time " * 200,
    )
    assert usage["prompt"] > 0
    assert usage["completion"] > 0
    assert usage["total"] == usage["prompt"] + usage["completion"]
    assert usage["usage_estimated"] is True


def test_provider_reported_usage_is_never_overwritten():
    from packages.core.ai.llm_client import _backfill_streamed_usage

    usage = {"prompt": 120, "completion": 340, "total": 460}
    _backfill_streamed_usage(
        usage,
        messages=[{"role": "user", "content": "hi"}],
        content="a real answer",
    )
    assert usage == {"prompt": 120, "completion": 340, "total": 460}
    assert "usage_estimated" not in usage


def test_empty_content_backfills_nothing():
    """No content and no usage is the empty-response case, handled (and
    raised) elsewhere — inventing tokens for it would bill for nothing."""
    from packages.core.ai.llm_client import EMPTY_USAGE, _backfill_streamed_usage

    usage = EMPTY_USAGE.copy()
    _backfill_streamed_usage(
        usage, messages=[{"role": "user", "content": "hi"}], content="",
    )
    assert usage["prompt"] == 0
    assert usage["completion"] == 0


def test_tool_call_arguments_count_toward_the_estimate():
    """A tool-call-only turn has empty text content; the streamed function
    arguments are the completion. The with_tools path passes them in as
    part of the content string — this pins that the estimator counts them."""
    from packages.core.ai.llm_client import EMPTY_USAGE, _backfill_streamed_usage

    usage = EMPTY_USAGE.copy()
    arguments_text = '{"query": "quarterly revenue report", "limit": 20}'
    _backfill_streamed_usage(
        usage,
        messages=[{"role": "user", "content": "find the report"}],
        content="" + arguments_text,
    )
    assert usage["completion"] > 0


def test_with_tools_stream_path_estimates_from_arguments_too():
    import inspect

    from packages.core.ai import llm_client

    source = inspect.getsource(llm_client.chat_completion_with_tools)
    assert "arguments_text" in source.split("_backfill_streamed_usage")[1][:400] or (
        "streamed_completion_text" in source
    ), "tool-call-only streams would record zero tokens"
