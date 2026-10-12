"""Model-facing browser observations must be one readable, actionable tree."""

import copy
import importlib
import json

import pytest

from packages.core.ai.agentic_loop import _compact_tool_result_for_context


def observation(tree='- main:\n  - textbox "Name" [ref=e1]\n'):
    return {
        "ok": True, "status": "read_page", "tabId": 17,
        "snapshot_id": "snapshot-current", "url": "https://example.test/form",
        "pageContent": tree, "page_content_truncated": False,
        "frame_summary": {"total": 1, "observed": 1, "skipped": 0},
        "semantic_refs": [{"ref": "e1", "role": "textbox", "label": "Name", "selector": "#name"}],
        "refMap": [{"ref": "e1", "selector": "#name"}],
        "candidate_summary": {"duplicate": "internal diagnostic " * 3000},
    }


def test_final_page_read_is_plain_text_with_one_tree_and_no_duplicate_metadata():
    value = observation()
    before = copy.deepcopy(value)
    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))
    assert text.startswith("Browser page snapshot\n")
    assert text.count(value["pageContent"]) == 1
    assert "snapshot_id: snapshot-current" in text
    assert "tabId: 17" in text
    assert "\ncoverage:" not in text
    assert len(text) < len(value["pageContent"]) + 1000
    for duplicate in ("semantic_refs", "refMap", "candidate_summary", "internal diagnostic", "#name"):
        assert duplicate not in text
    assert value == before


def test_model_projection_retains_pending_execution_and_frame_limits():
    value = observation()
    value.update({
        "page_content_truncated": True,
        "pending_verification": {"action_id": "upload-1", "condition": "attachment_preview"},
        "action_executed": True, "do_not_replay": True, "verified": False,
        "page_blockers": [{"kind": "login", "message": "Sign in required"}],
        "frame_summary": {"total": 2, "observed": 1, "skipped": 1, "unresolved": 1},
        "skipped_frames": [{"frame_id": 7, "reason": "host_url_mismatch"}],
        "observation_quality": "low",
        "visual_fallback_recommended": True,
        "visual_fallback_reasons": ["embedded_content_unreadable"],
    })
    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))
    for fact in ("page_content_truncated: true", "action_executed: true", "do_not_replay: true",
                 "verified: false", "upload-1", "attachment_preview", "Sign in required", "host_url_mismatch",
                 "observation_quality: low", "visual_fallback_recommended: true", "embedded_content_unreadable"):
        assert fact in text


def test_plain_text_history_recompaction_retains_header_and_complete_lines():
    value = observation('- main:\n  - group "History":\n' + ''.join(
        f'    - button "Entry {n}" [ref=e{n}]\n' for n in range(200)
    ))
    value.update(action_executed=True, do_not_replay=True, observation_quality="medium",
                 visual_fallback_recommended=False)
    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))
    for budget in (2000, 1000, 800):
        text = _compact_tool_result_for_context("mcp__chrome__read_page", text, max_chars=budget)
        assert len(text) <= budget
        assert "output_truncated: true" in text
        assert "action_executed: true" in text
        assert "do_not_replay: true" in text
        assert "snapshot_id: snapshot-current" in text
        assert "observation_quality: medium" in text
        assert "visual_fallback_recommended: false" in text
        assert '- main:\n  - group "History":\n' in text
        assert text.endswith("\n")


def test_first_delivery_keeps_a_large_rendered_tree_without_a_second_small_cutoff():
    tree = '- main:\n' + '  - paragraph: Rendered content with useful context\n' * 4200 + '- contentinfo: END OF RENDERED DOCUMENT'
    text = _compact_tool_result_for_context('mcp__chrome__read_page', json.dumps(observation(tree)))
    assert text.endswith(tree)
    assert 'output_truncated: true' not in text


def test_ref_limits_are_distinct_from_document_truncation():
    value = observation()
    value['observation_coverage'] = {'interactive_targets': False}
    text = _compact_tool_result_for_context('mcp__chrome__read_page', json.dumps(value))
    assert '\ncoverage:' not in text
    assert 'observation_coverage.interactive_targets: false' in text
    assert 'page_content_truncated: true' not in text
    value['frame_coverage'] = {'complete': False}
    text = _compact_tool_result_for_context('mcp__chrome__read_page', json.dumps(value))
    assert 'frame_coverage.complete: false' in text


@pytest.mark.parametrize("tag,editable", [("div", False), ("select", False), ("input", True), ("div", True)])
def test_combobox_control_semantics_survive_projection_and_history_compaction(tag, editable):
    tree = '- main:\n  - combobox "Headquarters country" [ref=f181:e31] [required] [invalid]\n'
    tree += '  - paragraph: Additional form instructions\n' * 80
    value = observation(tree)
    value["semantic_refs"] = [{
        "ref": "f181:e31", "role": "combobox", "tag": tag, "editable": editable,
        "selector": '[data-testid="autocomplete-button"]', "frame_id": 181,
    }]
    before = copy.deepcopy(value)

    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))
    text = _compact_tool_result_for_context("mcp__chrome__read_page", text, max_chars=700)

    assert len(text) <= 700 and "output_truncated: true" in text
    line = next(line for line in text.splitlines() if "[ref=f181:e31]" in line)
    assert f"[tag={tag}]" in line
    assert f"[editable={str(editable).lower()}]" in line
    assert '[required] [invalid]' in line
    assert "autocomplete-button" not in text
    assert value == before


def test_combobox_projection_does_not_invent_missing_or_unobserved_control_semantics():
    value = observation('- combobox "Country" [ref=e1]\n')
    value["semantic_refs"] = [
        {"ref": "e1", "role": "combobox"},
        {"ref": "e2", "role": "combobox", "tag": "div", "editable": False},
    ]
    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))
    assert text.endswith(value["pageContent"])
    assert "[tag=" not in text and "[editable=" not in text
    assert "e2" not in text


def test_combobox_annotations_bind_the_tree_ref_not_ref_text_inside_a_label():
    value = observation('- combobox "Country [ref=e2]" [ref=e1]\n')
    value["semantic_refs"] = [
        {"ref": "e1", "role": "combobox", "tag": "div", "editable": False},
        {"ref": "e2", "role": "combobox", "tag": "input", "editable": True},
    ]
    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))
    assert text.endswith('- combobox "Country [ref=e2]" [ref=e1] [tag=div] [editable=false]\n')


@pytest.mark.parametrize("label", [
    "Folder \\",
    "C:\\Archive\\Country",
    "Folder [ref=e2]\\",
    "Folder &quot;[ref=e2]&quot; \\",
    "",
])
def test_combobox_projection_matches_extension_label_format(label):
    quoted_label = f' "{label}"' if label else ""
    tree = f'- combobox{quoted_label} [ref=e1] (x=10,y=20) required invalid\n'
    value = observation(tree)
    value["semantic_refs"] = [
        {"ref": "e1", "role": "combobox", "tag": "div", "editable": False},
        {"ref": "e2", "role": "combobox", "tag": "input", "editable": True},
    ]
    before = copy.deepcopy(value)

    text = _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value))

    assert text.endswith(
        f'- combobox{quoted_label} [ref=e1] [tag=div] [editable=false] (x=10,y=20) required invalid\n'
    )
    assert "[tag=input]" not in text and "[editable=true]" not in text
    assert value == before


@pytest.mark.asyncio
@pytest.mark.parametrize("combobox", [False, True])
async def test_actual_model_message_uses_text_but_executor_keeps_raw_observation(monkeypatch, combobox):
    module = importlib.import_module("packages.core.ai.agentic_loop")
    value = observation()
    value.update(observation_quality="low", visual_fallback_recommended=True,
                 visual_fallback_reasons=["active_dialog_unresolved"])
    if combobox:
        value["pageContent"] = '- combobox "Country" [ref=e1]\n'
        value["semantic_refs"] = [{
            "ref": "e1", "role": "combobox", "tag": "div", "editable": False, "selector": "#name",
        }]
    calls = 0

    async def completion(messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "", [{"id": "read-1", "name": "mcp__chrome__read_page", "arguments": {"tabId": 17}}], {}
        result = next(m["content"] for m in reversed(messages) if m.get("role") == "tool")
        assert result.startswith("Browser page snapshot\n")
        assert "observation_quality: low" in result
        assert "visual_fallback_recommended: true" in result
        assert "active_dialog_unresolved" in result
        if combobox:
            assert '[ref=e1] [tag=div] [editable=false]' in result
        else:
            assert value["pageContent"] in result
        assert "semantic_refs" not in result
        return "done", None, {}

    async def execute(name, args):
        return json.dumps(value)

    monkeypatch.setattr(module, "runtime_execute_agentic_round_tool_completion", completion)
    result = await module.agentic_loop(
        system_prompt="test", user_message="Inspect the form", max_rounds=3,
        tools=[{"type": "function", "function": {"name": "mcp__chrome__read_page", "parameters": {"type": "object"}}}],
        tool_executor=execute,
    )
    assert result.content == "done"
    assert value["semantic_refs"][0]["selector"] == "#name"
