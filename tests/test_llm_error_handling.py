"""LLM client error legibility.

Covers two confirmed audit findings:
- A provider/key mismatch (e.g. an OpenAI key selecting a DeepSeek model) is
  detected by `_validate_llm_key_model_compatibility`, which runs BEFORE the
  main try block in chat_completion / chat_completion_with_tools. Previously the
  raised LLMAuthConfigurationError propagated unhandled (agentic_loop only
  catches CreditExhaustedError). It must now return a clean failure_usage.
- A non-JSON body (e.g. a Cloudflare 524 HTML page) should produce an
  actionable error that names the HTML/gateway condition.
"""

import pytest

import packages.core.ai.llm_client as llm


class _Routing:
    api_key = "sk-" + "x" * 40  # detected as an OpenAI-family key
    base_url = "https://api.openai.com/v1"
    source = "byok"


def _patch_routing(monkeypatch):
    async def _routing(model, metadata):
        return _Routing()

    async def _noop():
        return None

    monkeypatch.setattr(llm, "resolve_llm_routing_for_model", _routing)
    monkeypatch.setattr(llm, "_preflight_credit_check", _noop)


@pytest.mark.asyncio
async def test_chat_completion_returns_failure_on_key_model_mismatch(monkeypatch):
    _patch_routing(monkeypatch)
    # OpenAI key + DeepSeek model → real validator raises; must be caught.
    content, usage = await llm.chat_completion(
        [{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4-flash",
    )
    assert content == ""
    assert usage.get("error"), "mismatch must surface in usage['error'], not raise"


@pytest.mark.asyncio
async def test_chat_completion_with_tools_returns_failure_on_key_model_mismatch(monkeypatch):
    _patch_routing(monkeypatch)
    content, tool_calls, usage = await llm.chat_completion_with_tools(
        [{"role": "user", "content": "hi"}],
        [],
        model="deepseek/deepseek-v4-flash",
    )
    assert content == ""
    assert tool_calls is None
    assert usage.get("error"), "mismatch must surface in usage['error'], not raise"


def test_parse_llm_response_flags_html_error_page():
    class _Resp:
        status_code = 200
        text = "<!DOCTYPE html><html><head><title>524 timeout</title></head></html>"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    with pytest.raises(RuntimeError, match="HTML error page"):
        llm._parse_llm_response_json(_Resp(), call_type="chat_completion")
