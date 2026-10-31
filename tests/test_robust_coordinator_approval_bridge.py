"""An approved delegated Chat call resumes with its exact continuation.

The delegated Chat runtime owns the exact tool-call continuation: on resume it
rebuilds the approved call plus the unexecuted suffix, carrying the runtime
approval token and each call's continuation, never a model-supplied token.
"""
from __future__ import annotations

from packages.core.ai.runtime import workspace_delegation_actions


def test_delegation_resume_rebuilds_approved_call_and_exact_suffix():
    retry_continuation = {
        "kind": "retry_with_result_token",
        "tool": "mcp__chrome__click_element",
        "arguments": {"tabId": 7, "ref": "e1"},
    }
    suffix_continuation = {
        "kind": "retry_with_result_token",
        "tool": "mcp__chrome__type_text",
        "arguments": {"tabId": 7, "ref": "e2"},
    }
    calls = workspace_delegation_actions._delegation_resume_forced_calls(
        {
            "tool_name": "mcp__chrome__confirm_action",
            "tool_arguments": {"approvalId": "provider-approval"},
            "tool_call_id": "call-approved",
            "remaining_tool_calls": [{
                "id": "call-suffix",
                "name": "mcp__example__write",
                "arguments": {"value": "after approval"},
            }],
            "disable_followup_tools": True,
            "tool_continuations": {
                "call-approved": retry_continuation,
                "call-suffix": suffix_continuation,
            },
        },
        "runtime-approval-token",
    )

    assert calls == [
        {
            "name": "mcp__chrome__confirm_action",
            "arguments": {
                "approvalId": "provider-approval",
                "approval_token": "runtime-approval-token",
                "__manor_tool_continuation": retry_continuation,
            },
            "disable_followup_tools": True,
        },
        {
            "name": "mcp__example__write",
            "arguments": {
                "value": "after approval",
                "__manor_tool_continuation": suffix_continuation,
            },
            "disable_followup_tools": True,
        },
    ]
