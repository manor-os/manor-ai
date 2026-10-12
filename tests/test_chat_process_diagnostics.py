"""Public attempt diagnostics retain causes, not private execution payloads."""
import json

import pytest

from packages.core.ai.runtime.output_policy import (
    PUBLIC_TOOL_FAILURE_MESSAGE,
    runtime_public_tool_calls,
    runtime_public_tool_result,
)
from packages.core.ai.runtime.streams import runtime_tool_result_for_chat


ATTACHMENT_REASON = "The selected attachment is missing or no longer accessible."
APPROVAL_ATTACHMENT_REASON = "The selected attachment is no longer accessible for approval."
FILE_REASON = "The requested file is missing or no longer accessible."


@pytest.mark.parametrize("payload,expected", [
    ({"error": "approval_continuation_unavailable", "message": (
        "Knowledge document unavailable Save large content as a Workspace document "
        "and retry with its canonical reference."
    ), "tool": "mcp__chrome__click_element", "retryable": True}, APPROVAL_ATTACHMENT_REASON),
    ({"error": "approval_continuation_unavailable", "message": "Knowledge document unavailable"},
     APPROVAL_ATTACHMENT_REASON),
    ({"error": "File not found: /private/workspace/video.mp4"}, FILE_REASON),
    ({"contract": "tool_error", "status": "error", "error": "tool_execution_failed",
      "message": "Knowledge document unavailable", "details": {"api_key": "private-key"}}, ATTACHMENT_REASON),
    ({"error": "tool_error", "detail": json.dumps({"error": "File not found: private.mp4"})}, FILE_REASON),
])
@pytest.mark.parametrize("serialized", [False, True])
def test_known_attempt_causes_survive_public_projection(payload, expected, serialized):
    source = json.dumps(payload) if serialized else payload
    assert runtime_public_tool_result(source) == expected
    preview = runtime_tool_result_for_chat("read_file", json.dumps(payload))
    assert preview == expected
    projected = runtime_public_tool_calls({
        "name": "read_file", "status": "error", "result": preview,
        "raw_result": json.dumps(payload),
    })
    assert projected["result"] == expected
    assert "raw_result" not in projected
    assert runtime_public_tool_calls(projected) == projected


@pytest.mark.parametrize("message", [
    "Tool error (mcp__chrome__click_element): Knowledge document unavailable",
    "Knowledge document unavailable",
])
def test_known_executor_cause_does_not_require_exposing_its_tool_name(message):
    assert runtime_public_tool_result(message) == ATTACHMENT_REASON




@pytest.mark.parametrize("reason", [
    "Chrome connection unavailable: private-host.internal",
    "Knowledge file exceeds the transfer size limit: /private/customer.csv",
    "Knowledge document is outside the current Workspace: workspace-private",
])
def test_extended_domain_diagnostics_do_not_inherit_public_allowlisting(reason):
    assert runtime_public_tool_result({"error": reason}) == PUBLIC_TOOL_FAILURE_MESSAGE


@pytest.mark.parametrize("code,expected", [
    ("approval_continuation_unavailable", APPROVAL_ATTACHMENT_REASON),
    ("tool_execution_failed", ATTACHMENT_REASON),
])
def test_wrapped_attachment_error_retains_only_its_explicit_approval_context(code, expected):
    raw = json.dumps({"error": "tool_error", "detail": json.dumps({
        "error": code, "message": "Knowledge document unavailable",
    })})
    assert runtime_public_tool_result(raw) == expected


@pytest.mark.parametrize("payload", [
    {"error": "unknown_failure", "message": "ValueError('/private/config.yaml')"},
    {"error": "unknown_failure", "message": "Authorization: Bearer private-token"},
    {"error": "unknown_failure", "details": {"message": "Knowledge document unavailable"}},
])
def test_unrecognized_diagnostics_are_not_mistaken_for_public_causes(payload):
    assert runtime_public_tool_result(payload) == PUBLIC_TOOL_FAILURE_MESSAGE


def test_successful_document_content_is_not_reclassified_as_a_failure():
    payload = {"status": "completed", "content": "Knowledge document unavailable"}
    assert runtime_public_tool_result(payload) == payload


def test_typed_authorization_denial_remains_authoritative_over_diagnostic_text():
    from packages.core.ai.runtime.output_policy import PUBLIC_PERMISSION_DENIED_MESSAGE

    assert runtime_public_tool_result({
        "error": "blocked_by_permission", "message": "File not found: private.mp4",
    }) == PUBLIC_PERMISSION_DENIED_MESSAGE
