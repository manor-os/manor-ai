"""Model-context projection regressions for successful Chrome page reads."""

import json

from packages.core.ai.agentic_loop import _compact_tool_result_for_context


def _read_page(tree: str, **extra):
    return {
        "ok": True, "status": "read_page", "driver": "chrome-extension", "tabId": 17,
        "url": "https://app.example.test/workflows", "snapshot_id": "snapshot-current",
        "pageContent": tree, "page_content_truncated": False,
        "observation_coverage": {"complete": True, "frames": 2},
        "semantic_refs": [{"ref": "e1", "role": "button", "label": "History 1", "selector": "main > button"}],
        "candidate_summary": {"diagnostic": "repeated diagnostic " * 3000}, **extra,
    }


def project(value, budget=None):
    return _compact_tool_result_for_context("mcp__chrome__read_page", json.dumps(value), max_chars=budget)


def test_first_read_page_delivery_gets_the_independent_64k_budget():
    tree = "- main:\n" + "  - paragraph: content\n" * 2300
    text = project(_read_page(tree))
    assert text.endswith(tree)
    assert "snapshot_id: snapshot-current" in text
    assert "\ncoverage:" not in text
    assert len(text) <= 64_000


def test_small_history_budget_cuts_tree_at_line_boundary_with_ancestor_and_truthful_flag():
    tree = '- main:\n  - group "History":\n' + ''.join(
        f'    - button "Entry {number}" [ref=e{number}]\n' for number in range(1, 200)
    )
    text = project(_read_page(tree), 1000)
    assert len(text) <= 1000
    assert '\n\nPage tree:\n- main:\n  - group "History":\n' in text
    assert text.endswith("\n")
    assert "\ncoverage:" not in text
    assert "output_truncated: true" in text


def test_recompaction_preserves_prior_truncation_pending_action_and_exact_link():
    href = "https://app.example.test/items/receipt?token=" + "a" * 300
    tree = f'- main:\n  - link "Receipt" [ref=f7:e99]:\n    - /url: "{href}"\n' + "  - paragraph: content\n" * 600
    text = project(_read_page(tree, page_content_truncated=True,
        pending_verification={"action_id": "action-1", "condition": "attachment_preview"}), 6000)
    text = _compact_tool_result_for_context("mcp__chrome__read_page", text, max_chars=4000)
    assert len(text) <= 4000
    assert "page_content_truncated: true" in text
    assert "pending_verification.action_id: action-1" in text
    assert "attachment_preview" in text
    assert href in text
    assert '[ref=f7:e99]' in text


def test_first_delivery_keeps_a_50k_tree_without_repeating_every_link_identity():
    tree = "- main:\n" + ''.join(f'  - link "Item {n}" [ref=e{n}]\n' for n in range(1, 1300))
    links = [{"ref": f"e{n}", "role": "link", "href": f"https://example.test/items/{n}"} for n in range(1, 1300)]
    text = project(_read_page(tree, semantic_refs=links))
    assert text.endswith(tree)
    assert "\ncoverage:" not in text
    assert "links_omitted:" in text
    assert len(text) <= len(tree) + 3000


def test_small_budget_keeps_execution_facts_ahead_of_verbose_feedback():
    url = "https://example.test/" + "path/" * 150
    value = _read_page('- main:\n  - paragraph: "quoted\\ntext"\n' * 100,
        url=url, page_feedback=[{"severity": "error", "text": '"quoted"\n' * 2000}],
        form_candidates=[{"form_label": "redundant form diagnostic"}] * 100,
        action_executed=True, do_not_replay=True, verified=False,
        event_receipt={"kind": "page_state", "status": "pending"})
    text = project(value, 4000)
    assert len(text) <= 4000
    assert url in text
    for fact in ("action_executed: true", "do_not_replay: true", "verified: false", "event_receipt.status: pending"):
        assert fact in text
    assert "redundant form diagnostic" not in text
    assert "metadata:" in text


def test_recompaction_keeps_omission_notice_without_shortening_oversized_link():
    href = "https://example.test/" + "a" * 7000
    text = project(_read_page('- main:\n  - link "Oversized" [ref=e1]\n',
        semantic_refs=[{"ref": "e1", "role": "link", "href": href}]), 4000)
    text = _compact_tool_result_for_context("mcp__chrome__read_page", text, max_chars=3000)
    assert "links_omitted: 1" in text
    assert href not in text
    assert "https://example.test/aaa" not in text


def test_small_budget_keeps_tree_ahead_of_repeated_form_diagnostics():
    tree = "- main:\n" + "  - paragraph: hello world\n" * 40
    text = project(_read_page(tree, form_candidates=[{"label": "form", "fields": [{"label": "A" * 400}]}] * 8), 4000)
    assert text.endswith(tree)
    assert "form_candidates" not in text


def test_field_description_and_readonly_state_stay_in_tree_without_duplicate_ref_array():
    tree = '- main:\n  - textbox "Caption" [ref=e1] [readonly]: First line\n    - /description: Caption field\n'
    text = project(_read_page(tree, semantic_refs=[{
        "ref": "e1", "role": "textbox", "read_only": True, "description": "Caption field", "in_viewport": False,
    }]), 1000)
    text = _compact_tool_result_for_context("mcp__chrome__read_page", text, max_chars=900)
    assert text.endswith(tree)
    assert text.count("Caption field") == 1
    assert "[readonly]" in text
    assert "semantic_refs" not in text
