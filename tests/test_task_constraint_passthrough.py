"""#3 — the user's verbatim task constraints reach the execution layer.

Constraints the user bound to a task (e.g. "只保存表格,不要写 essay") are
captured into task.details.runtime_context but historically reached neither
the planner (except as opaque Details JSON) nor the executing subagent (the
worker snapshot dropped them). These tests pin the transmission chain:
extractor → planner prompt block → subagent prompt injection.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.plans.task_constraints import (
    binding_constraints_forbid_artifact_writes,
    extract_binding_constraints,
    plan_requires_artifact_write,
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


def test_explicit_artifact_write_prohibition_is_structured_for_runtime_gates():
    details = {
        "runtime_context": {
            "instructions": (
                "Keep this inspection read-only. Do not create or write files, "
                "including artifact generation."
            ),
        },
    }

    assert binding_constraints_forbid_artifact_writes(details) is True
    assert plan_requires_artifact_write({
        "steps": [{"key": "save", "output_shape": "ArtifactResult"}],
    }) is True


def test_artifact_write_prohibition_matches_failed_step_wording():
    details = {
        "runtime_context": {
            "rules": [
                {
                    "description": (
                        "The binding constraints explicitly prohibit creating or "
                        "writing files, including artifact generation."
                    ),
                },
            ],
        },
    }

    assert binding_constraints_forbid_artifact_writes(details) is True


def test_source_file_prohibition_does_not_block_separate_generated_artifact():
    details = {
        "runtime_context": {
            "instructions": "Do not write source files; generate a separate PDF artifact.",
        },
    }

    assert binding_constraints_forbid_artifact_writes(details) is False


@pytest.mark.parametrize(
    "instruction",
    [
        "No files should be deleted; create report.pdf.",
        "No files were deleted. Save the final result as report.pdf.",
        "No artifacts may be removed; generate a new PDF artifact.",
    ],
)
def test_file_retention_wording_does_not_forbid_new_artifacts(instruction):
    details = {"runtime_context": {"instructions": instruction}}

    assert binding_constraints_forbid_artifact_writes(details) is False


@pytest.mark.parametrize(
    "instruction",
    [
        "No files should be created.",
        "No artifacts may be generated.",
        "No files; reply inline only.",
    ],
)
def test_explicit_no_output_wording_still_forbids_artifacts(instruction):
    details = {"runtime_context": {"instructions": instruction}}

    assert binding_constraints_forbid_artifact_writes(details) is True


def test_optional_envelope_file_slot_does_not_make_text_step_an_artifact_write():
    assert plan_requires_artifact_write({
        "steps": [
            {
                "key": "summarize",
                "expected_output_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {
                        "summary": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
        ],
    }) is False


def test_planner_rejects_artifact_step_that_violates_binding_constraint():
    from packages.core.plans.planner import _required_plan_step_errors
    from packages.core.plans.schema import Plan, PlanStep

    task = SimpleNamespace(
        details={
            "runtime_context": {
                "instructions": "Do not create or write files.",
            },
        },
    )
    plan = Plan(steps=[
        PlanStep(
            key="materialize",
            kind="subagent",
            service_key="knowledge",
            params={"prompt": "Save a closeout artifact."},
            output_shape="ArtifactResult",
        ),
    ])

    assert _required_plan_step_errors(task, plan) == [
        "step 'materialize' requires a saved artifact but USER CONSTRAINTS prohibit file/artifact writes",
    ]
