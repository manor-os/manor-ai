"""#3 — the user's verbatim task constraints reach the execution layer.

Constraints the user bound to a task (e.g. "只保存表格,不要写 essay") are
captured into task.details.runtime_context. These tests pin the extractor and
the binding block the agent loop renders into the Task brief (the brief
itself is covered by tests/test_workspace_task_instruction_contract.py).
"""
from __future__ import annotations

from packages.core.services.task_constraints import (
    extract_binding_constraints,
    render_constraints_block,
)


# ── extractor ──────────────────────────────────────────────────────


def test_extracts_instructions_and_rule_descriptions_verbatim():
    details = {
        "runtime_context": {
            "instructions": "只保存表格,不要写 essay",
            "rules": [
                {"description": "URLs must not contain spaces"},
                {"rule": "No duplicate investors"},
                "Keep it under 20 rows",
            ],
        }
    }
    out = extract_binding_constraints(details)
    assert out == [
        "只保存表格,不要写 essay",
        "URLs must not contain spaces",
        "No duplicate investors",
        "Keep it under 20 rows",
    ]


def test_instructions_may_be_a_list():
    details = {"runtime_context": {"instructions": ["a", "b", "a"]}}
    assert extract_binding_constraints(details) == ["a", "b"]  # deduped, ordered


def test_tolerates_missing_or_odd_shapes():
    assert extract_binding_constraints(None) == []
    assert extract_binding_constraints({}) == []
    assert extract_binding_constraints({"runtime_context": "nope"}) == []
    assert extract_binding_constraints({"runtime_context": {"rules": "x"}}) == []


def test_render_block_is_empty_when_no_constraints():
    assert render_constraints_block([]) == ""


def test_render_block_frames_constraints_as_binding():
    block = render_constraints_block(["do not write an essay"])
    assert "USER CONSTRAINTS" in block
    assert "binding" in block.lower()
    assert "- do not write an essay" in block
