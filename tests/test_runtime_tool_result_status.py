"""The persisted tool_end status must not label a sandbox handoff as failed."""

from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.events import RuntimeToolResultStatus
from packages.core.ai.runtime.tool_execution import _tool_end_event_data


def test_sandbox_ready_parses_as_a_successful_completion():
    assert RuntimeToolResultStatus.parse("sandbox_ready") is RuntimeToolResultStatus.COMPLETED
    assert RuntimeToolResultStatus.parse("failed") is RuntimeToolResultStatus.FAILED
    assert RuntimeToolResultStatus.parse("something_else") is None


@pytest.mark.parametrize("tool_name", ["invoke_skill", "generate_file"])
def test_sandbox_handoff_is_recorded_as_completed(tool_name):
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    handoff = runtime_format_invoke_skill_result(
        "xlsx",
        {
            "skill": "xlsx",
            "content": "Sandbox ready",
            "usage": {},
            "tools_used": [],
            "rounds": 0,
            "stop_reason": "sandbox_ready",
            "sandbox_id": "sb_1",
        },
    )
    assert json.loads(handoff)["status"] == "sandbox_ready"

    data = _tool_end_event_data(tool_name, handoff, {"kind": "spreadsheet"})
    assert data["status"] == RuntimeToolResultStatus.COMPLETED.value


def test_unknown_domain_status_is_not_assumed_to_be_a_failure():
    data = _tool_end_event_data("invoke_skill", json.dumps({"status": "mystery"}), {})
    assert data["status"] == RuntimeToolResultStatus.COMPLETED.value


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("status,expected", [
    ("approval_required", "success"),
    ("hitl_required", "success"),
    ("cancelled", "error"),
    ("rejected", "error"),
])
def test_approval_waits_are_not_execution_failures(status, expected, encoded):
    from packages.core.ai.runtime.streams import (
        runtime_tool_call_error,
        runtime_tool_path_memory_outcome,
        runtime_tool_status_for_chat,
    )

    payload = {"status": status, "approval_required": True, "ok": False}
    result = json.dumps(payload) if encoded else payload
    assert runtime_tool_status_for_chat(result) == expected
    assert runtime_tool_path_memory_outcome(result) is None
    assert runtime_tool_call_error(result) is None
