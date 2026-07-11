"""LLM HTTP responses must fail with a clear error on empty/non-JSON bodies.

Some providers occasionally return an empty 200 (or an HTML/error body),
which makes httpx's ``response.json()`` raise an opaque
``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``. That bubbled
up through the agent loop as a confusing failure. The guard converts it into
a clear, actionable RuntimeError that names the status and body preview.
"""

from __future__ import annotations

import json

import pytest
import httpx

from packages.core.ai.llm_client import _http_error_detail, _parse_llm_response_json


class _EmptyResponse:
    status_code = 200
    text = ""

    def json(self):
        return json.loads("")  # raises JSONDecodeError, like httpx on empty body


class _HtmlErrorResponse:
    status_code = 502
    text = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>apitokengate.com | 520: Web server is returning an unknown error</title>
        <link rel="stylesheet" href="/cdn-cgi/styles/main.css" />
      </head>
      <body>
        <h1>Web server is returning an unknown error</h1>
      </body>
    </html>
    """

    def json(self):
        return json.loads(self.text)  # raises JSONDecodeError


class _OkResponse:
    status_code = 200
    text = '{"choices": []}'

    def json(self):
        return {"choices": []}


def test_empty_body_raises_clear_runtime_error():
    with pytest.raises(RuntimeError, match="non-JSON or empty"):
        _parse_llm_response_json(_EmptyResponse(), call_type="chat_completion_with_tools")


def test_error_includes_status_and_body_preview():
    with pytest.raises(RuntimeError) as exc_info:
        _parse_llm_response_json(_HtmlErrorResponse(), call_type="chat_completion")
    detail = str(exc_info.value)
    assert "502" in detail
    assert "HTML error page" in detail
    assert "Web server is returning an unknown error" in detail
    assert "main.css" not in detail
    assert "<html" not in detail.lower()


def test_http_error_detail_sanitizes_html_error_pages():
    request = httpx.Request("POST", "https://apitokengate.com/v1/chat/completions")
    response = httpx.Response(520, text=_HtmlErrorResponse.text, request=request)
    exc = httpx.HTTPStatusError("server error", request=request, response=response)

    detail = _http_error_detail(exc)

    assert detail.startswith("HTTP 520: HTML error page:")
    assert "Web server is returning an unknown error" in detail
    assert "main.css" not in detail
    assert "<html" not in detail.lower()


def test_valid_json_passes_through():
    assert _parse_llm_response_json(_OkResponse(), call_type="x") == {"choices": []}
