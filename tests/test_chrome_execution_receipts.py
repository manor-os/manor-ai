"""Regressions from real Chrome calls whose execution facts were lost in context."""

import json

import pytest

from packages.core.ai.agentic_loop import _compact_tool_result_for_context
from packages.core.ai.tools.mcp_builtin import _mcp_error_result_to_text
from packages.core.ai.runtime.streams import (
    runtime_tool_call_error,
    runtime_tool_path_memory_outcome,
    runtime_tool_status_for_chat,
)


def _receipt(status="upload_unverified"):
    return {
        "ok": False,
        "status": status,
        "action_executed": True,
        "retryable": False,
        "verified": False,
        "website_upload_confirmed": False,
        "selected_files_count": 0,
        "selected_file_names": [],
        "event_receipt": {"kind": "page_state", "status": "timeout"},
        "next_required_tool": "mcp__chrome__read_page",
        "post_action_page_state": {
            "state_verified": True,
            "snapshot_id": "snap-after",
            "page_text": "Unrelated feed content. " * 4000,
            "input_candidates": [{"ref": "e1", "value": "Draft caption"}],
        },
    }


def test_chrome_mcp_error_exposes_execution_receipt_to_runtime():
    payload = _receipt()
    result = json.loads(_mcp_error_result_to_text("chrome", "upload", {
        "isError": True, "content": [{"type": "text", "text": json.dumps(payload)}],
    }))
    for key in ("status", "action_executed", "verified", "event_receipt", "next_required_tool"):
        assert result.get(key) == payload[key]


def test_wrapped_file_error_keeps_actionable_reason_after_compaction():
    reason = "Knowledge source does not resolve to one active document"
    raw = json.dumps({"error": "tool_error", "server": "chrome", "tool": "upload",
                      "detail": json.dumps({"ok": False, "status": "error",
                                            "error": reason, "action_executed": False})})
    for budget in (16000, 1000):
        raw = _compact_tool_result_for_context("mcp__chrome__upload", raw, max_chars=budget)
        result = json.loads(raw)
        assert result["contract"] == "tool_error"
        assert result["error"] == "tool_error"
        assert result["message"] == reason
        assert result["details"].get("reason") == reason
        assert result["details"]["action_executed"] is False


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("status", ["upload_unverified", "event_timeout"])
def test_chrome_receipt_survives_initial_and_pressure_compaction(wrapped, status):
    payload = _receipt(status)
    raw = json.dumps({"server": "chrome", "tool": "upload", "error": "tool_error",
                      "detail": json.dumps(payload)} if wrapped else payload)
    for budget in (16000, 4000, 1800, 1000):
        raw = _compact_tool_result_for_context("mcp__chrome__upload", raw, max_chars=budget)
        assert len(raw) <= budget
        result = json.loads(raw)
        assert result["contract"] == "tool_error"
        result = result["details"]
        for key in ("ok", "status", "action_executed", "retryable", "verified",
                    "website_upload_confirmed", "next_required_tool"):
            assert result.get(key) == payload[key], (budget, key, result)
        assert result["event_receipt"]["status"] == "timeout"


@pytest.mark.parametrize("with_action_receipt", [False, True])
def test_error_contract_keeps_execution_facts_ahead_of_large_action_diagnostics(with_action_receipt):
    payload = {
        "target_resolution": {"selector": "div > " * 4000},
        "state_hint": {"text": "page content " * 4000},
        "action": {"detail": "locator diagnostics " * 4000},
        **_receipt("event_timeout"),
        "do_not_replay": True,
    }
    if with_action_receipt:
        payload["action_receipt"] = {
            "contract": "chrome_action_v1", "action_id": "action-123",
            "execution": "executed", "verification": "failed",
        }
    raw = json.dumps(payload)
    for budget in (16000, 4000, 1800, 1000):
        raw = _compact_tool_result_for_context("mcp__chrome__upload", raw, max_chars=budget)
        assert len(raw) <= budget
        result = json.loads(raw)
        assert result["contract"] == "tool_error"
        for key in ("ok", "status", "action_executed", "retryable", "do_not_replay",
                    "verified", "website_upload_confirmed", "next_required_tool", "event_receipt"):
            assert result["details"][key] == payload[key], (budget, key, result)
        if with_action_receipt:
            assert result["details"]["action_receipt"] == payload["action_receipt"]


def test_successful_upload_compaction_keeps_website_evidence():
    payload = {**_receipt("uploaded"), "ok": True, "verified": True,
               "website_upload_confirmed": True, "selected_files_count": 1,
               "selected_file_names": ["clip.mp4"]}
    result = _compact_tool_result_for_context(
        "mcp__chrome__upload", json.dumps(payload), max_chars=1800,
    )
    assert 'website_upload_confirmed: true' in result
    assert 'selected_file_names[0]: clip.mp4' in result


def test_large_post_action_state_keeps_only_the_changed_field_and_feedback():
    selector = "main > " + "div:nth-child(2) > " * 40 + "textarea"
    refs = [{"ref": f"e{i}", "role": "textbox", "label": f"Field {i}",
             "selector": selector + f"[data-field=\"{i}\"]", "value": "Verified draft"}
            for i in range(1, 25)]
    raw = json.dumps({
        "ok": True, "status": "filled", "tabId": 3, "ref": "e1",
        "snapshot_id": "before",
        "target_resolution": {**refs[0], "snapshot_id": "before", "selector_candidates": [selector] * 4},
        "action": {"ok": True, "ref": "e1", "target_resolution": {**refs[0], "selector_candidates": [selector] * 4}},
        "state_hint": {"action": "fill_or_select", "target": refs[0], "next": "use_post_action_state"},
        "post_action_page_state": {
            "ok": True, "state_verified": True, "tabId": 3, "snapshot_id": "after",
            "semantic_refs": refs, "input_candidates": refs[:4],
            "page_feedback": [{"kind": "status", "text": "Draft saved"}],
        },
    })
    for budget in (12000, 4000):
        raw = _compact_tool_result_for_context("mcp__chrome__fill_or_select", raw, max_chars=budget)
        assert len(raw) <= budget
        assert raw.startswith('Browser action\n')
        assert 'snapshot_id: after' in raw
        assert 'field.ref: e1' in raw and 'field.value: Verified draft' in raw
        assert 'Draft saved' in raw
        assert 'Field 24' not in raw and selector not in raw


def test_later_chrome_success_does_not_hide_a_failure_without_operation_evidence():
    from packages.core.services.assistant_blocks import AssistantBlocksBuilder

    builder = AssistantBlocksBuilder()
    tool = "mcp__chrome__upload"
    builder.end_tool(tool, arguments={"tabId": 3, "document_id": "doc", "ref": "e1", "snapshot_id": "old"},
                     status="error", result=json.dumps(_receipt()))
    builder.end_tool(tool, arguments={"tabId": 3, "document_id": "doc", "ref": "e1", "snapshot_id": "new"},
                     status="success", result='{"ok":true,"verified":true}')
    builder.set_final_text("The latest draft is ready.")
    process = builder.blocks()[0]
    assert process["status"] == "error"
    assert "recovered_by_step_id" not in process["steps"][0]

@pytest.mark.parametrize("status", ["read_page_required", "upload_unverified", "event_timeout"])
def test_negative_execution_receipt_is_not_projected_as_success(status):
    raw = json.dumps({"ok": False, "status": status, "reason": "target_moved"})
    assert runtime_tool_status_for_chat(raw) == "error"
    assert runtime_tool_path_memory_outcome(raw) == "failure"
    assert runtime_tool_call_error(raw) == "target_moved"


def test_read_and_cleanup_do_not_turn_unverified_upload_into_goal_completion():
    from packages.core.ai.runtime.chrome_routing import runtime_record_chrome_tool_result
    from packages.core.ai.runtime.chrome_run import chrome_outcome_for_result

    metadata = {}
    for tool, payload in [
        ("upload", {"error": "tool_error", "detail": json.dumps(_receipt())}),
        ("read_page", {"ok": True, "snapshot_id": "snap-new", "tabId": 42}),
        ("finalize_tabs", {"ok": True}),
    ]:
        runtime_record_chrome_tool_result(tool_name=f"mcp__chrome__{tool}", arguments={"tabId": 42},
                                          result=json.dumps(payload), runtime_metadata=metadata)
    outcome = chrome_outcome_for_result(metadata, skill="chrome", goal="Prepare media", content="Stopped",
                                       stop_reason="completed").to_dict()
    assert outcome["status"] == "completed"  # The child loop ended normally.
    assert outcome["goal_status"] == "unverified"
    receipt = next(item for item in outcome["evidence"] if item["kind"] == "last_browser_action")
    assert receipt["status"] == "upload_unverified"
    assert receipt["action_executed"] is True
    assert receipt["website_upload_confirmed"] is False


def test_wrapped_event_timeout_uses_executed_recovery_path():
    from packages.core.ai.runtime.chrome_routing import runtime_record_chrome_tool_result

    metadata = {}
    runtime_record_chrome_tool_result(
        tool_name="mcp__chrome__fill_or_select", arguments={"tabId": 42},
        result=json.dumps({"error": "tool_error", "detail": json.dumps(_receipt("event_timeout"))}),
        runtime_metadata=metadata,
    )
    state = metadata["chrome_runtime_contract_v1"]
    assert state["pending_observation_after_action"]["reason"] == "expected_event_timeout_after_execution"
