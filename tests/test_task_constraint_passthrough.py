"""#3 — the user's verbatim task constraints reach the execution layer.

Constraints the user bound to a task (e.g. "只保存表格,不要写 essay") are
captured into task.details.runtime_context but historically reached neither
the planner (except as opaque Details JSON) nor the executing subagent (the
worker snapshot dropped them). These tests pin the transmission chain:
extractor → planner prompt block → subagent prompt injection.
"""
from __future__ import annotations

from packages.core.plans.task_constraints import (
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


# ── planner hop ────────────────────────────────────────────────────


def test_planner_prompt_surfaces_constraints_as_a_binding_block():
    from packages.core.ai.runtime.planning import runtime_planner_task_prompt

    task = {
        "title": "Build the investor table",
        "description": "Compile the fundraising tracker",
        "details": {
            "runtime_context": {"instructions": "只保存表格,不要写 essay"},
        },
    }
    prompt = runtime_planner_task_prompt(task)
    assert "USER CONSTRAINTS" in prompt
    assert "只保存表格,不要写 essay" in prompt
    # it must appear as its own block, not only buried inside the Details JSON
    assert prompt.index("USER CONSTRAINTS") < prompt.index("Details JSON")


# ── execution hop ──────────────────────────────────────────────────


def test_subagent_prompt_injection_prepends_constraints():
    from packages.core.workers.internal import _with_binding_constraints

    out = _with_binding_constraints(
        "Do the work.", {"task_binding_constraints": ["只保存表格,不要写 essay"]},
    )
    assert out.startswith("## USER CONSTRAINTS")
    assert "只保存表格,不要写 essay" in out
    assert out.strip().endswith("Do the work.")


def test_subagent_prompt_injection_noop_without_constraints():
    from packages.core.workers.internal import _with_binding_constraints

    assert _with_binding_constraints("Do the work.", {}) == "Do the work."
    assert _with_binding_constraints(
        "Do the work.", {"task_binding_constraints": []},
    ) == "Do the work."
