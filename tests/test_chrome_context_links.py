"""Browser observation compaction must retain bounded, exact link evidence."""
import json

import pytest

from packages.core.ai.agentic_loop import _compact_tool_result_for_context


def _observation(href="https://app.example.test/items/receipt-42"):
    controls = [
        {
            "ref": f"e{i}", "role": "button", "label": f"Control {i}",
            "selector": f"main > section:nth-child({i + 1}) > button",
            "description": "Control description " * 30,
            "bounds": {"x": 10, "y": 10, "width": 100, "height": 20},
            "disabled": False, "clickable": True, "in_viewport": True,
        }
        for i in range(45)
    ]
    link = {
        "ref": "e46", "role": "link", "label": "View created item", "href": href,
        "frame_id": 7, "dialog_label": "Item details", "in_viewport": True,
    }
    return {
        "ok": True, "status": "read_page", "driver": "chrome-extension",
        "tabId": 17, "snapshot_id": "snapshot-current",
        "pageContent": "Control details " * 1500 + href,
        "semantic_refs": [*controls, link], "refs_count": 46,
        "input_candidates": [], "next_actions": [],
    }


@pytest.mark.parametrize("budget", [12000, 6000])
def test_late_visible_link_survives_control_heavy_observation(budget):
    observation = _observation()
    raw = _compact_tool_result_for_context(
        "mcp__chrome__read_page", json.dumps(observation), max_chars=budget,
    )
    result = json.loads(raw)
    assert len(raw) <= budget
    assert result["snapshot_id"] == observation["snapshot_id"]
    link = next(item for item in result["content_links"] if item["ref"] == "e46")
    assert link["href"] == observation["semantic_refs"][-1]["href"]
    assert link["frame_id"] == 7
    assert link["dialog_label"] == "Item details"


def test_link_identity_is_not_shortened_or_lost_on_recompaction():
    href = "https://app.example.test/items/receipt?key=" + "a" * 300
    raw = json.dumps(_observation(href))
    for budget in (12000, 6000):
        raw = _compact_tool_result_for_context("mcp__chrome__read_page", raw, max_chars=budget)
        assert len(raw) <= budget
        assert any(item["href"] == href for item in json.loads(raw)["content_links"])


def test_links_too_large_for_budget_are_explicitly_omitted_not_truncated():
    observation = _observation("https://app.example.test/" + "a" * 12000)
    raw = _compact_tool_result_for_context(
        "mcp__chrome__read_page", json.dumps(observation), max_chars=6000,
    )
    result = json.loads(raw)
    assert len(raw) <= 6000
    assert result["_tool_result_truncated"]["omitted_content_links"] == 1
    assert result["content_links"] == []


def test_hidden_and_duplicate_links_do_not_consume_evidence_budget():
    observation = _observation()
    link = observation["semantic_refs"][-1]
    observation["content_links"] = [dict(link)]
    observation["semantic_refs"].append({
        "ref": "e47", "role": "link", "href": "https://hidden.example.test/",
        "hidden": True,
    })
    result = json.loads(_compact_tool_result_for_context(
        "mcp__chrome__read_page", json.dumps(observation), max_chars=12000,
    ))
    assert len(result["content_links"]) == 1
    assert result["content_links"][0]["href"] == link["href"]


def test_retained_ref_does_not_expose_a_shortened_link_identity():
    href = "https://app.example.test/items/" + "a" * 300
    observation = _observation(href)
    observation["semantic_refs"].insert(0, observation["semantic_refs"].pop())
    result = json.loads(_compact_tool_result_for_context(
        "mcp__chrome__read_page", json.dumps(observation), max_chars=12000,
    ))
    assert next(item for item in result["semantic_refs"] if item["ref"] == "e46")["href"] == href


def test_link_evidence_never_overrides_executed_action_facts():
    observation = {
        **_observation(), "ok": False, "status": "upload_unverified",
        "action_executed": True, "verified": False, "retryable": False,
        "do_not_replay": True, "next_required_tool": "mcp__chrome__read_page",
    }
    raw = json.dumps(observation)
    for budget in (12000, 4000, 1000):
        raw = _compact_tool_result_for_context("mcp__chrome__upload", raw, max_chars=budget)
        assert len(raw) <= budget
        result = json.loads(raw)
        assert result["contract"] == "tool_error"
        facts = result["details"]
        for key in ("ok", "status", "action_executed", "verified", "retryable", "do_not_replay"):
            assert facts[key] == observation[key]


def test_existing_link_content_evidence_is_preserved():
    observation = _observation()
    observation["content_links"] = [{
        **observation["semantic_refs"][-1], "snippet": "The created item is saved.",
        "source": "Result card", "published_time": "2026-09-26",
    }]
    result = json.loads(_compact_tool_result_for_context(
        "mcp__chrome__read_page", json.dumps(observation), max_chars=12000,
    ))
    link = result["content_links"][0]
    assert link["snippet"] == "The created item is saved."
    assert link["source"] == "Result card"
    assert link["published_time"] == "2026-09-26"
