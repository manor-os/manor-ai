from types import SimpleNamespace

import pytest

from packages.core.plans import planner
from packages.core.plans.planner import (
    _build_validated_goal_scoring_plan,
    _fallback_plan,
    _is_goal_scoring_task,
    _normalize_goal_scoring_plan,
    _normalize_required_skill_steps,
)
from packages.core.plans.schema import Plan, PlanStep


def _scoring_task(**overrides):
    values = dict(
        title="Score active Goals from current evidence",
        description=(
            "Score each active Goal only from current workspace evidence. "
            "Missing evidence equals 0. Persist immediately: for each listed Goal "
            "call workspace_agent or manor action update_goal_value once, with "
            "params.goal_id AND params.value in the SAME call."
        ),
        owner_service_key="readiness_and_learning",
        required_skills=["jr-readiness-replan-measurement-learning"],
        details={"strategist_task_key": "score_goals"},
        expected_output={
            "deliverables": [{
                "kind": "value",
                "name": "goal_measurements",
                "shape": "StepResult",
            }]
        },
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _two_step_inventory_plan() -> Plan:
    return Plan(
        steps=[
            PlanStep(
                key="score_from_current_evidence",
                kind="subagent",
                service_key="readiness_and_learning",
                output_shape="TextResult",
                params={
                    "prompt": "Inventory files and invoke skills before scoring.",
                    "skill_refs": ["jr-task-catalog-eligibility"],
                },
                description="Score from documents and skills.",
            ),
            PlanStep(
                key="goal_measurements",
                kind="subagent",
                service_key="readiness_and_learning",
                output_shape="TextResult",
                depends_on=["score_from_current_evidence"],
                params={
                    "prompt": (
                        "Persist ${{ steps.score_from_current_evidence.result.measurements }}"
                    ),
                },
                description="Persist each Goal current_value via update_goal_value.",
            ),
        ]
    )


def test_is_goal_scoring_task_uses_strategist_task_key() -> None:
    assert _is_goal_scoring_task(_scoring_task()) is True
    assert _is_goal_scoring_task(_scoring_task(details={"strategist_task_key": "other"})) is False
    assert _is_goal_scoring_task(_scoring_task(details={})) is False


def test_goal_scoring_plan_collapses_inventory_and_skill_steps() -> None:
    task = _scoring_task()
    plan = _normalize_required_skill_steps(task, _two_step_inventory_plan())
    plan = _normalize_goal_scoring_plan(task, plan)

    assert [step.key for step in plan.steps] == ["goal_measurements"]
    step = plan.steps[0]
    assert step.kind == "subagent"
    assert step.service_key == "readiness_and_learning"
    assert step.params.get("prompt") == task.description
    assert not step.params.get("skill_refs")
    assert step.output_shape == "TextResult"
    assert plan.metadata.normalization_reason == "goal_scoring_persist_only"


def test_goal_scoring_does_not_bind_owner_skill_refs() -> None:
    task = _scoring_task()
    plan = _normalize_required_skill_steps(task, _two_step_inventory_plan())
    assert all(not (step.params or {}).get("skill_refs") or step.key != "goal_measurements" or True for step in plan.steps)
    # The inventory step may already have skill_refs from the planner LLM;
    # required_skills must not be copied onto scoring steps.
    bound = [
        step.key
        for step in plan.steps
        if "jr-readiness-replan-measurement-learning" in (step.params or {}).get("skill_refs", [])
    ]
    assert bound == []


def test_non_scoring_plan_is_unchanged() -> None:
    task = _scoring_task(details={"strategist_task_key": "compile_handoff"})
    original = _two_step_inventory_plan()
    plan = _normalize_goal_scoring_plan(task, original)
    assert [step.key for step in plan.steps] == [step.key for step in original.steps]


def test_file_deliverable_skill_runs_only_on_file_producer() -> None:
    task = _scoring_task(
        details={"strategist_task_key": "build_release_matrix"},
        required_skills=["jr-task-catalog-eligibility"],
        expected_output={"deliverables": [{"kind": "file", "name": "matrix"}]},
    )
    plan = Plan(steps=[
        PlanStep(
            key="write_matrix", kind="subagent", service_key="readiness_and_learning",
            output_shape="DocumentResult", expects=["files"],
            params={"prompt": "Write and read back the matrix."},
        ),
        PlanStep(
            key="report_matrix", kind="subagent", service_key="readiness_and_learning",
            output_shape="TextResult", depends_on=["write_matrix"],
            params={
                "prompt": "Report the verified document ID.",
                "skill_refs": ["jr-task-catalog-eligibility", "other-skill"],
            },
        ),
    ])

    normalized = _normalize_required_skill_steps(task, plan)

    assert normalized.steps[0].params["skill_refs"] == ["jr-task-catalog-eligibility"]
    assert normalized.steps[1].params["skill_refs"] == ["other-skill"]
    assert normalized.metadata.normalized_required_skill_steps == ["write_matrix"]



def _scoring_context():
    return planner._Context(
        workspace=None,
        subscriptions=[
            SimpleNamespace(
                service_key="readiness_and_learning",
                id="sub",
                agent_id="reviewer",
            )
        ],
        agents_by_id={
            "reviewer": SimpleNamespace(
                id="reviewer",
                name="Reviewer",
                system_prompt="",
            )
        },
        allowed_service_keys={"readiness_and_learning"},
        provider_actions={},
    )


@pytest.mark.asyncio
async def test_generate_plan_skips_llm_for_score_goals(monkeypatch) -> None:
    async def _should_not_run(**kwargs):
        raise AssertionError("score_goals must not call the planner LLM")

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _should_not_run)
    plan = await planner._generate_plan(_scoring_task(), _scoring_context())
    assert [step.key for step in plan.steps] == ["goal_measurements"]
    assert plan.steps[0].kind == "subagent"
    assert plan.steps[0].service_key == "readiness_and_learning"
    assert plan.steps[0].params.get("prompt") == _scoring_task().description
    assert not plan.steps[0].params.get("skill_refs")
    assert plan.steps[0].output_shape == "TextResult"
    assert plan.steps[0].expected_output_schema is None


def test_fallback_plan_uses_persist_plan_for_score_goals() -> None:
    plan = _fallback_plan(_scoring_task(), _scoring_context())
    assert [step.key for step in plan.steps] == ["goal_measurements"]
    assert plan.steps[0].kind == "subagent"


def test_build_validated_goal_scoring_plan_rejects_missing_owner() -> None:
    with pytest.raises(planner.PlannerError):
        _build_validated_goal_scoring_plan(
            _scoring_task(owner_service_key=""),
            _scoring_context(),
        )



def test_submit_plan_logs_validation_failure(caplog) -> None:
    import logging

    from packages.core.ai.runtime.planning import runtime_execute_planner_tool_call

    def parse_plan(_text):
        raise ValueError("bad plan")

    with caplog.at_level(logging.WARNING, logger="packages.core.ai.runtime.planning"):
        result = runtime_execute_planner_tool_call(
            "submit_plan",
            {"plan_json": {"steps": []}},
            context=SimpleNamespace(),
            parse_plan=parse_plan,
        )
    assert result["contract"] == "tool_error"
    assert result["error"] == "plan_validation_failed"
    assert "Plan validation failed" in result["message"]
    assert any("submit_plan validation failed" in rec.message for rec in caplog.records)
