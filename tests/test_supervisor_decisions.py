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
async def test_retry_resets_the_step_and_resumes_the_plan(_capture_add_task_log, _no_celery):
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
    assert _no_celery == ["P1"], "a fresh executor cycle must be scheduled"
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


# ── The wiring holds the rules ────────────────────────────────────────


def test_finalize_compares_enum_members_not_strings():
    """The verdict set is an enum; ``verdict == "completed"`` and
    ``verdict in ("failed", ...)`` are the shape this replaces."""
    import inspect

    from packages.core.plans import executor

    body = inspect.getsource(executor.PlanExecutor._finalize)
    assert "decision.verdict is SupervisorVerdict" in body
    assert 'verdict == "' not in body
    assert 'verdict in ("' not in body


def test_finalize_logs_the_decision_before_acting_on_it():
    import inspect

    from packages.core.plans import executor

    body = inspect.getsource(executor.PlanExecutor._finalize)
    assert body.count("_log_supervisor_verdict(") == 1, (
        "one decision, one log — branches must not each invent their own"
    )
    assert body.index("_log_supervisor_verdict(") < body.index("apply_task_status_transition(")


def test_a_refused_retry_downgrades_instead_of_vanishing():
    import inspect

    from packages.core.plans import executor

    body = inspect.getsource(executor.PlanExecutor._finalize)
    assert "decision.downgraded(" in body
    assert "SupervisorVerdict.NEEDS_REPLAN" in body


def test_the_supervisor_outcome_returns_decisions():
    import inspect

    from packages.core.plans import executor

    body = inspect.getsource(executor.PlanExecutor._supervise_outcome)
    assert "SupervisorDecision" in body
    assert "SupervisorDecisionSource.GATE" in body
    assert "SupervisorDecisionSource.FALLBACK" in body


def test_the_frontend_already_renders_this_log_type():
    """The log type is a named constant; the frontend icon map key must be
    the same string, or verdicts silently lose their icon."""
    from pathlib import Path

    from packages.core.constants.supervisor import SUPERVISOR_VERDICT_LOG_TYPE

    body = Path("apps/web/src/components/task/TaskLogItem.tsx").read_text(encoding="utf-8")
    assert SUPERVISOR_VERDICT_LOG_TYPE in body


def test_the_supervisor_pathway_matches_no_keyword_strings():
    """Statuses and log types are enums/constants — branching on a quoted
    string is how "cancelled" and "blocked" drift apart from the vocabulary
    without anyone noticing. Every supervisor function must compare enum
    members; the raw literals may appear only inside the enum definitions."""
    import inspect

    from packages.core.plans import executor

    for fn in (
        executor.PlanExecutor._supervise_outcome,
        executor.PlanExecutor._apply_supervisor_step_retry,
        executor.PlanExecutor._log_supervisor_verdict,
    ):
        body = inspect.getsource(fn)
        for literal in (
            '"done"', '"failed"', '"skipped"', '"pending"', '"running"',
            '"completed"', '"cancelled"', '"blocked"', '"ai_supervisor_verdict"',
        ):
            assert literal not in body, f"{fn.__name__} matches keyword {literal}"


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


def test_the_supervisor_call_passes_the_full_view():
    import inspect

    from packages.core.plans import executor

    body = inspect.getsource(executor.PlanExecutor._supervise_outcome)
    assert "_supervisor_step_infos(plan, steps)" in body
    assert "plan_rationale" in body
    assert "is_replan" in body
    assert 'str(text)[:150]' not in body, "the 150-character keyhole is gone"


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

    import inspect

    body = inspect.getsource(planning.runtime_execute_plan_supervisor_completion)
    assert "max_tokens=2000" in body


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


def test_the_outcome_call_carries_the_task_history():
    import inspect

    from packages.core.plans import executor

    body = inspect.getsource(executor.PlanExecutor._supervise_outcome)
    assert "_supervisor_attempt_infos(" in body
    assert "_supervisor_review_infos(" in body
    assert "prior_attempts=prior_attempts" in body
    assert "prior_reviews=prior_reviews" in body


# ── The headline follows the verdict, not the plan ────────────────────
#
# Production task 01KZ8A43NZSZCYNTD46A2G8C24 ("Produce today's finished
# stickman video") posted BOTH of these into one thread, from the same
# transaction, microseconds apart:
#
#   Manor AI            ## Task Completed …
#   Manor AI Supervisor This task stopped before it finished.
#                       Missing: the deliverable file …
#
# The plan really had reached `completed` — its one step reported `done`
# while its own text said the MP4 never materialised. The supervisor
# caught exactly that and ruled NEEDS_HUMAN. But the deliverable summary
# was gated on the PLAN's mechanical status, so it announced completion
# over the top of the card that had just withheld it.


def _finalize_summary_source() -> str:
    import inspect

    from packages.core.plans.executor import PlanExecutor

    return inspect.getsource(PlanExecutor._finalize)


def test_the_completion_headline_is_gated_on_the_supervisor_verdict():
    source = _finalize_summary_source()
    assert "supervisor_decision.verdict is SupervisorVerdict.COMPLETED" in source, (
        "the deliverable summary must consult the verdict; gating on the "
        "plan's own status is what let 'Task Completed' contradict a "
        "needs_human card in the same thread"
    )


def test_a_withheld_verdict_gets_a_heading_that_does_not_claim_completion():
    source = _finalize_summary_source()
    assert "## Work so far (not accepted as finished)" in source
    # The work still gets posted — only the claim changes.
    assert "deliverables" in source


def test_the_plan_status_gate_uses_the_enum():
    """`status == "completed"` here is the plan's terminal status as a bare
    local, which the first enum sweep's attribute-anchored scan could not
    see. The guard test now covers this receiver inside executor.py."""
    source = _finalize_summary_source()
    assert 'status == "completed"' not in source
    assert "status == ExecutionPlanStatus.COMPLETED" in source
