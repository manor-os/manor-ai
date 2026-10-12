"""Supervisor decisions: an enum, evidence, and one step re-run.

Production task 01KWRR5VGHYHQD3A116TZ8ET0W ended "failed" with seven
task_logs, every one reporting success. The supervisor's whole output was a
single bare word, matched with ``verdict in (...)`` string literals, and the
transition it caused wrote no task_log — the word was the entire record of
why, and it was never stored.

Three rules pinned here:

* verdicts are a closed enum (``SupervisorVerdict``) and travel as a
  ``SupervisorDecision`` that must say why — evidence from the model, a
  deterministic gate's finding, or the stated fact that no review ran;
* every decision is logged on the task with that evidence;
* the supervisor may send exactly ONE step back for a re-run
  (``retry_step``) instead of failing a whole plan over one bad output —
  once per step per plan, because a supervisor retrying the same step
  against the same result forever is a loop, not a review.
"""
from __future__ import annotations

import types

import pytest

from packages.core.constants.supervisor import (
    SUPERVISOR_STEP_RETRY_FLAG,
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.constants.task_actors import TaskActor
from packages.core.plans.executor import PlanExecutor


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Answers step queries; the executor's helpers need nothing else."""

    def __init__(self, steps):
        self._steps = steps

    async def execute(self, query):
        text = str(query)
        if "step_key =" in text:
            wanted = [s for s in self._steps if getattr(s, "_selected", False)]
            return _Query(wanted)
        return _Query(self._steps)


def _step(key, status, *, params=None, selected=False):
    step = types.SimpleNamespace(
        step_key=key, step_status=status, params=dict(params or {}),
        current_lease_id="L", error={"x": 1}, result={"text": "old"},
        finished_at=object(), started_at=object(), attempt_count=3,
        human_input_prompt="p",
    )
    step._selected = selected
    return step


def _plan(plan_id="P1", status="completed"):
    return types.SimpleNamespace(
        id=plan_id, status=status, completed_at=object(), last_error="boom",
        dispatcher_state={"_response_locale": "en"},
    )


def _task(task_id="T1"):
    return types.SimpleNamespace(id=task_id)


def _decision(verdict, evidence="", source=SupervisorDecisionSource.MODEL, step_key=None):
    return SupervisorDecision(
        verdict=verdict, evidence=evidence, source=source, step_key=step_key,
    )


@pytest.fixture(autouse=True)
def _capture_add_task_log(monkeypatch):
    calls: list[dict] = []

    async def _fake_add_task_log(db, task_id, log_type, content, *, actor, created_by, metadata=None):
        calls.append({
            "task_id": task_id, "log_type": log_type, "content": content,
            "actor": actor, "created_by": created_by, "metadata": metadata or {},
        })
        return types.SimpleNamespace(id="L1")

    import packages.core.services.task_service as task_service_module
    monkeypatch.setattr(task_service_module, "add_task_log", _fake_add_task_log)
    return calls


@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    import packages.core.tasks.ai_tasks as ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks, "run_plan",
        types.SimpleNamespace(delay=lambda plan_id: dispatched.append(plan_id)),
    )
    return dispatched


# ── Every decision is logged, with evidence ───────────────────────────


@pytest.mark.asyncio
async def test_the_production_case_an_override_says_so(_capture_add_task_log):
    """Both steps succeeded; the verdict failed the task anyway. The log
    must state the override and carry the evidence — not claim something
    broke, and not stay silent."""
    db = _FakeDB([_step("search", "done"), _step("append", "done")])
    decision = _decision(
        SupervisorVerdict.FAILED,
        evidence="steps produced text but nothing shows the document was written",
    )

    await PlanExecutor._log_supervisor_verdict(db, _task(), _plan(), decision)

    assert len(_capture_add_task_log) == 1
    call = _capture_add_task_log[0]
    assert call["log_type"] == "ai_supervisor_verdict"
    assert call["actor"] is TaskActor.SUPERVISOR
    assert "overrides a mechanically successful run" in call["content"]
    assert "nothing shows the document was written" in call["content"]
    assert call["metadata"]["evidence"]
    assert call["metadata"]["source"] == "model"


@pytest.mark.asyncio
async def test_a_real_step_failure_is_named(_capture_add_task_log):
    db = _FakeDB([_step("generate_video", "failed"), _step("write_script", "done")])

    await PlanExecutor._log_supervisor_verdict(
        db, _task(), _plan(), _decision(SupervisorVerdict.FAILED, evidence="video step errored"),
    )

    content = _capture_add_task_log[0]["content"]
    assert "generate_video" in content
    assert "overrides" not in content, "a real failure is not an override"


@pytest.mark.asyncio
async def test_a_gate_decision_carries_the_gates_finding(_capture_add_task_log):
    db = _FakeDB([_step("s1", "done")])
    await PlanExecutor._log_supervisor_verdict(
        db, _task(), _plan(),
        _decision(
            SupervisorVerdict.NEEDS_REPLAN,
            evidence="task expects a file deliverable; no step produced artifact evidence",
            source=SupervisorDecisionSource.GATE,
        ),
        note="the replan budget for this task is exhausted",
    )
    content = _capture_add_task_log[0]["content"]
    assert "deterministic check" in content
    assert "artifact evidence" in content
    assert "replan budget" in content


@pytest.mark.asyncio
async def test_a_logging_failure_never_raises(monkeypatch):
    import packages.core.services.task_service as task_service_module

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(task_service_module, "add_task_log", _boom)
    db = _FakeDB([_step("s1", "done")])
    await PlanExecutor._log_supervisor_verdict(
        db, _task(), _plan(), _decision(SupervisorVerdict.FAILED),
    )  # must not raise


# ── The one step re-run ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_resets_the_step_and_resumes_the_plan(_capture_add_task_log, _no_celery, monkeypatch):
    from packages.core.plans import wakeup

    staged: list[tuple[str, str]] = []

    async def stage_cycle(_db, plan_id: str, *, reason: str):
        staged.append((plan_id, reason))
        return object()

    monkeypatch.setattr(wakeup, "stage_plan_cycle_wake", stage_cycle)
    step = _step("assemble_final_mp4", "done", selected=True)
    plan = _plan(status="completed")
    db = _FakeDB([step])
    decision = _decision(
        SupervisorVerdict.RETRY_STEP,
        evidence="the step returned text but no file",
        step_key="assemble_final_mp4",
    )

    applied = await PlanExecutor._apply_supervisor_step_retry(db, _task(), plan, decision)

    assert applied is True
    assert step.step_status == "pending"
    assert step.error is None and step.result is None
    assert step.attempt_count == 0
    assert step.params[SUPERVISOR_STEP_RETRY_FLAG] is True
    assert plan.status == "running"
    assert plan.completed_at is None
    assert staged == [("P1", "supervisor_step_retry")], "a fresh executor cycle must be staged"
    assert _no_celery == [], "retry must not publish before the transaction commits"
    assert _capture_add_task_log, "the retry itself is a logged decision"
    assert "re-run" in _capture_add_task_log[0]["content"]


@pytest.mark.asyncio
async def test_the_second_retry_of_a_step_is_refused(_no_celery):
    """Once per step per plan. Without this the supervisor could retry the
    same step against the same result forever."""
    step = _step(
        "assemble_final_mp4", "done",
        params={SUPERVISOR_STEP_RETRY_FLAG: True}, selected=True,
    )
    plan = _plan(status="completed")
    db = _FakeDB([step])

    applied = await PlanExecutor._apply_supervisor_step_retry(
        db, _task(), plan,
        _decision(SupervisorVerdict.RETRY_STEP, step_key="assemble_final_mp4"),
    )

    assert applied is False
    assert step.step_status == "done", "a refused retry must not touch the step"
    assert plan.status == "completed"
    assert _no_celery == []


@pytest.mark.asyncio
async def test_a_retry_naming_no_real_step_is_refused(_no_celery):
    db = _FakeDB([])  # lookup finds nothing
    applied = await PlanExecutor._apply_supervisor_step_retry(
        db, _task(), _plan(),
        _decision(SupervisorVerdict.RETRY_STEP, step_key="ghost_step"),
    )
    assert applied is False


def test_the_frontend_already_renders_this_log_type():
    """The log type is a named constant; the frontend icon map key must be
    the same string, or verdicts silently lose their icon."""
    from pathlib import Path

    from packages.core.constants.supervisor import SUPERVISOR_VERDICT_LOG_TYPE

    body = Path("apps/web/src/components/task/TaskLogItem.tsx").read_text(encoding="utf-8")
    assert SUPERVISOR_VERDICT_LOG_TYPE in body


# ── The supervisor sees the whole picture ─────────────────────────────


def test_step_infos_carry_instruction_result_and_artifacts():
    """The production shape: the plan_dag holds the step's description and
    prompt (what it was ASKED to do); the result holds what it claimed; the
    artifact refs hold what the system verifiably recorded. The old view
    passed 150 characters of result and nothing else."""
    from packages.core.plans.executor import _supervisor_step_infos

    plan = types.SimpleNamespace(plan_dag={
        "steps": [{
            "key": "assemble_final_mp4",
            "description": "Reuse existing scenes and script to assemble the final MP4.",
            "params": {"prompt": "必须尝试产出视频文件,并返回最终 MP4 的文件地址"},
        }],
        "metadata": {"rationale": "one production step suffices"},
    })
    step = types.SimpleNamespace(
        step_key="assemble_final_mp4", kind="subagent", step_status="done",
        service_key="stickman.production", action_key=None, attempt_count=1,
        params={"prompt": "必须尝试产出视频文件,并返回最终 MP4 的文件地址"},
        result={"text": "部分完成:已启动生成任务;最终文件尚未返回可访问 URL"},
        error=None,
    )

    infos = _supervisor_step_infos(plan, [step])

    assert len(infos) == 1
    info = infos[0]
    assert "Reuse existing scenes" in info["instruction"]
    assert "必须尝试产出视频文件" in info["instruction"]
    assert "部分完成" in info["result"]
    assert info["artifacts"] == [], (
        "no artifact refs in the result → the supervisor must SEE that, "
        "instead of taking the step's prose as delivery"
    )
    assert info["owner"] == "stickman.production"


def test_step_infos_list_recorded_artifacts():
    from packages.core.plans.executor import _supervisor_step_infos

    plan = types.SimpleNamespace(plan_dag={})
    step = types.SimpleNamespace(
        step_key="write_report", kind="subagent", step_status="done",
        service_key=None, action_key="generate_file", attempt_count=2,
        params={},
        result={"fs_path": "Workspaces/W/reports/status.md", "text": "written"},
        error=None,
    )

    info = _supervisor_step_infos(plan, [step])[0]
    assert info["artifacts"] == ["Workspaces/W/reports/status.md"]
    assert info["attempts"] == 2


def test_step_infos_read_canonical_envelope_output_text():
    """The StepResult summary is metadata; outputs.text is the deliverable."""
    from packages.core.plans.executor import _supervisor_step_infos

    plan = types.SimpleNamespace(plan_dag={})
    step = types.SimpleNamespace(
        step_key="select_daily_topic",
        kind="subagent",
        step_status="done",
        service_key="stickman.topic",
        action_key=None,
        attempt_count=1,
        params={},
        result={
            "status": "succeeded",
            "summary": "Selected today's topic.",
            "text": "Selected today's topic.",
            "outputs": {
                "text": (
                    "Chosen title\nThe Two-Minute Reset\n\n"
                    "Target viewer\nBusy adults recovering from a difficult morning.\n\n"
                    "Core curiosity hook\nCan two minutes rescue the rest of the day?"
                )
            },
        },
        error=None,
    )

    info = _supervisor_step_infos(plan, [step])[0]

    assert "Chosen title\nThe Two-Minute Reset" in info["result"]
    assert "Core curiosity hook" in info["result"]


def test_step_infos_read_canonical_envelope_output_data():
    """A structured task deliverable must not collapse to its short summary."""
    from packages.core.plans.executor import _supervisor_result_preview

    preview = _supervisor_result_preview({
        "status": "succeeded",
        "summary": "Selected one producer-ready topic.",
        "outputs": {
            "data": {
                "topic_title": "The Two-Minute Reset for a Stuck Task",
                "core_promise": "Turn an avoided task into one visible next move.",
                "intended_viewer": "Busy solo workers",
                "narrative_angle": "Shrink the wall by choosing one corner.",
                "visual_beats": ["Face the wall", "Choose one corner", "Start"],
            },
        },
    }, max_chars=4000)

    assert "Selected one producer-ready topic" in preview
    assert '"topic_title": "The Two-Minute Reset for a Stuck Task"' in preview
    assert '"visual_beats"' in preview


def test_supervisor_contract_gate_rejects_materialized_step_contract_drift():
    """Review must not compare a result against a different contract than Plan."""
    from packages.core.contracts.shapes import get_shape
    from packages.core.contracts.task_output import plan_output_contract_schema
    from packages.core.plans.executor import _execution_output_contract_issue

    plan = types.SimpleNamespace(
        id="plan_1",
        plan_dag={
            "steps": [{
                "key": "draft",
                "kind": "subagent",
                "service_key": "content",
                "output_shape": "TextResult",
                "params": {"prompt": "Draft the final copy."},
            }],
        },
    )
    step = types.SimpleNamespace(
        step_key="draft",
        kind="subagent",
        step_status="done",
        expected_output_schema=plan_output_contract_schema(
            get_shape("ArtifactResult").json_schema()
        ),
        result={"files": [{"name": "draft.md", "fs_path": "draft.md"}]},
    )

    issue = _execution_output_contract_issue(plan, [step])

    assert issue is not None
    assert "draft" in issue
    assert "differs from the Plan contract" in issue


def test_supervisor_contract_gate_revalidates_the_delivered_step_result():
    from packages.core.contracts.shapes import get_shape
    from packages.core.contracts.task_output import plan_output_contract_schema
    from packages.core.plans.executor import _execution_output_contract_issue

    text_contract = plan_output_contract_schema(get_shape("TextResult").json_schema())
    plan = types.SimpleNamespace(
        id="plan_1",
        plan_dag={
            "steps": [{
                "key": "draft",
                "kind": "subagent",
                "service_key": "content",
                "output_shape": "TextResult",
                "params": {"prompt": "Draft the final copy."},
            }],
        },
    )
    step = types.SimpleNamespace(
        step_key="draft",
        kind="subagent",
        step_status="done",
        expected_output_schema=text_contract,
        result={"summary": "claimed success without the required text"},
    )

    issue = _execution_output_contract_issue(plan, [step])

    assert issue is not None
    assert "does not satisfy its output contract" in issue


def test_supervisor_contract_gate_allows_provider_hydrated_action_schema():
    """A provider may add a stronger contract when Plan declared none."""
    from packages.core.plans.executor import _execution_output_contract_issue

    provider_schema = {
        "type": "object",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    }
    plan = types.SimpleNamespace(
        id="plan_1",
        plan_dag={
            "steps": [{
                "key": "send_email",
                "kind": "action",
                "service_key": "communications",
                "provider": "gmail",
                "action_key": "email.send",
                "params": {},
            }],
        },
    )
    step = types.SimpleNamespace(
        step_key="send_email",
        kind="action",
        step_status="done",
        expected_output_schema=provider_schema,
        result={"message_id": "msg_123"},
    )

    assert _execution_output_contract_issue(plan, [step]) is None


def test_coordinator_children_are_not_top_level_output_producers():
    """Durable nested rows must not look like undeclared terminal DAG steps."""

    from packages.core.constants.execution import (
        COORDINATOR_CHILD_PARAM_KEY,
        COORDINATOR_PARENT_STEP_PARAM_KEY,
        COORDINATOR_PARAM_KEY,
    )
    from packages.core.contracts.task_output import task_output_envelope_schema
    from packages.core.plans.executor import (
        _execution_output_contract_issue,
        _task_output_contract_issue,
        _validated_terminal_task_output,
    )
    from packages.core.plans.schema import Plan

    payload_schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
        "additionalProperties": False,
    }
    task = types.SimpleNamespace(expected_output=payload_schema)
    parent_schema = task_output_envelope_schema(payload_schema)
    declared = Plan.model_validate({
        "steps": [{
            "key": "coordinate",
            "kind": "subagent",
            "service_key": "research",
            "params": {"prompt": "Coordinate the deliverable."},
            "expected_output_schema": parent_schema,
        }],
    })
    plan = types.SimpleNamespace(
        id="plan_coordinator",
        plan_dag=declared.model_dump(mode="json", exclude_none=True),
    )
    parent = types.SimpleNamespace(
        step_key="coordinate",
        kind="subagent",
        step_status="done",
        expected_output_schema=parent_schema,
        result={
            "status": "succeeded",
            "summary": "Delivered",
            "outputs": {"data": {"answer": "complete"}},
        },
        params={COORDINATOR_PARAM_KEY: True},
        depends_on=[],
    )
    child = types.SimpleNamespace(
        step_key="coordinate__research",
        kind="subagent",
        step_status="done",
        expected_output_schema=payload_schema,
        # This is intentionally a delegated envelope rather than the child's
        # raw payload schema. Its own handoff validator owns that boundary.
        result={"delegated": True, "result": {"answer": "complete"}},
        params={
            COORDINATOR_CHILD_PARAM_KEY: True,
            COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
        },
        depends_on=[],
    )

    steps = [parent, child]
    assert _execution_output_contract_issue(plan, steps, task) is None
    assert _task_output_contract_issue(task, steps) is None
    assert _validated_terminal_task_output(task, steps) == (
        "coordinate",
        {"answer": "complete"},
    )


def test_coordinator_child_history_does_not_decide_plan_terminal_state():
    """Only the authored parent owns a coordinator subtree's outcome."""
    from packages.core.constants.execution import (
        COORDINATOR_CHILD_PARAM_KEY,
        COORDINATOR_PARENT_STEP_PARAM_KEY,
        ExecutionStepStatus,
    )

    child_params = {
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
    }

    assert PlanExecutor._terminal_summary([
        _step("coordinate", ExecutionStepStatus.DONE),
        _step("old-child", ExecutionStepStatus.FAILED, params=child_params),
        _step("replacement", ExecutionStepStatus.DONE, params=child_params),
    ]) == "completed"
    assert PlanExecutor._terminal_summary([
        _step("coordinate", ExecutionStepStatus.FAILED),
        _step("child", ExecutionStepStatus.DONE, params=child_params),
    ]) == "failed"
    assert PlanExecutor._terminal_summary([
        _step("coordinate", ExecutionStepStatus.RUNNING),
        _step("child", ExecutionStepStatus.WAITING_HUMAN, params=child_params),
    ]) is None


def test_supervisor_step_view_uses_the_materialized_payload_contract():
    from packages.core.contracts.task_output import task_output_envelope_schema
    from packages.core.plans.executor import _supervisor_step_infos

    payload_schema = {
        "type": "object",
        "required": ["leads"],
        "properties": {"leads": {"type": "array", "minItems": 1}},
    }
    plan = types.SimpleNamespace(plan_dag={"steps": [{"key": "deliver"}]})
    step = types.SimpleNamespace(
        step_key="deliver",
        kind="subagent",
        step_status="done",
        service_key="research",
        action_key=None,
        attempt_count=1,
        params={},
        expected_output_schema=task_output_envelope_schema(payload_schema),
        result={
            "status": "succeeded",
            "summary": "one lead",
            "outputs": {"data": {"leads": [{"name": "A"}]}},
        },
        error=None,
    )

    info = _supervisor_step_infos(plan, [step])[0]

    assert info["output_contract"] == payload_schema
    assert info["contract_check"] == "passed"
    assert '"leads"' in info["result"]


def test_the_supervisor_is_never_starved_again():
    """The supervisor reviews one task a handful of times across its whole
    execution. Economising its input saves fractions of a cent; a wrong
    verdict throws away the whole plan's spend — and its output is the
    explanation a person (and its own next review) reads later, so it is
    never asked to be brief. These floors keep anyone from quietly
    re-introducing the 150-character keyhole."""
    from packages.core.ai.runtime import planning
    from packages.core.constants.supervisor import MAX_EVIDENCE_CHARS

    assert planning.SUPERVISOR_INSTRUCTION_CHARS >= 2000
    assert planning.SUPERVISOR_RESULT_CHARS >= 2000
    assert planning.SUPERVISOR_MAX_STEPS >= 25
    assert MAX_EVIDENCE_CHARS >= 2000

# ── The supervisor's scope is the task, not one plan ──────────────────


def test_prior_attempts_summarize_what_was_already_tried():
    from packages.core.plans.executor import _supervisor_attempt_infos

    prior = types.SimpleNamespace(id="P0", status="failed")
    steps_by_plan = {"P0": [
        types.SimpleNamespace(step_key="draft", step_status="done", error=None),
        types.SimpleNamespace(
            step_key="publish", step_status="failed",
            error={"type": "TimeoutError", "message": "provider timed out"},
        ),
    ]}

    infos = _supervisor_attempt_infos([prior], steps_by_plan)

    assert infos == [{
        "status": "failed",
        "steps": [
            {"key": "draft", "status": "done", "error": ""},
            {"key": "publish", "status": "failed", "error": "TimeoutError: provider timed out"},
        ],
    }]


def test_prior_reviews_come_from_its_own_verdict_logs():
    """The supervisor reads back the same record a person reads — so it can
    reason about a retry it already spent, not just be blocked by the
    budget."""
    from packages.core.plans.executor import _supervisor_review_infos

    logs = [
        types.SimpleNamespace(meta={
            "verdict": "retry_step", "evidence": "output was empty",
            "step_key": "assemble_final_mp4", "plan_id": "P0",
        }),
        types.SimpleNamespace(meta={}),          # not a verdict log payload
        types.SimpleNamespace(meta=None),
    ]

    infos = _supervisor_review_infos(logs)

    assert infos == [{
        "verdict": "retry_step", "evidence": "output was empty",
        "step_key": "assemble_final_mp4", "plan_id": "P0",
    }]
