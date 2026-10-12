"""An external publish must not run on a partial upstream result.

Prod incident (task FQ2881, 2026-09-14): the content step landed ``partial``
with no post text, the dispatcher recorded it Done ("partial output is usable
output"), and the dependent publish step ran on that empty input, failed, and
only then was the operator asked — with the upstream reason buried.

``partial`` stays usable output for ordinary consumers. The one class that
must not consume it silently is a step whose plan declares an external
publish (``expects: ["publish"]`` or ``output_shape: PublishResult``): the
executor fails that step at the boundary with a human-required failure that
names the partial upstream step, so the task parks for the operator right
away. An operator note on the publish step (a retry with guidance) is the
explicit "publish the partial content anyway" and lifts the gate.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from packages.core.constants.execution import (
    UPSTREAM_PARTIAL_RESULT_ERROR_TYPE,
    ExecutionStepStatus,
)
from packages.core.contracts.envelope import step_result_envelope_schema
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.plans.executor import partial_upstream_publish_issue

_PARTIAL = {"status": "partial", "summary": "research done, no post text yet", "outputs": {"text": "暂缺"}}
_SUCCEEDED = {"status": "succeeded", "summary": "post drafted", "outputs": {"text": "Hello X"}}


def _step(key: str, *, status: str, result=None, depends_on=None, schema=None, human_input_response=None):
    return ExecutionStep(
        id=generate_ulid(),
        plan_id="plan",
        entity_id="entity",
        step_key=key,
        kind="subagent",
        step_status=status,
        result=result,
        depends_on=depends_on or [],
        expected_output_schema=step_result_envelope_schema() if schema is None else schema,
        human_input_response=human_input_response,
    )


def _dag(*, publish_step: dict):
    return {"steps": [{"key": "prepare"}, {"key": "publish", "depends_on": ["prepare"], **publish_step}]}


# ── the pure gate ────────────────────────────────────────────────────


def test_publish_expecting_step_is_held_when_its_dependency_landed_partial():
    prepare = _step("prepare", status="done", result=_PARTIAL)
    publish = _step("publish", status="pending", depends_on=["prepare"])

    issue = partial_upstream_publish_issue(
        publish, _dag(publish_step={"expects": ["publish"]}), {"prepare": prepare, "publish": publish},
    )

    assert issue is not None
    # No publish happened; the incomplete upstream content is a repairable
    # input defect, not a reason to ask the user to debug execution.
    assert "Complete the upstream" in issue["message"]
    assert "prepare" in issue["message"]
    failure = issue["failure"]
    assert failure["requires_human"] is False
    assert failure["retryable"] is False
    assert any("prepare" in b and "no post text yet" in b for b in failure["blockers"])


def test_publish_result_shape_counts_as_an_external_publish():
    prepare = _step("prepare", status="done", result=_PARTIAL)
    publish = _step("publish", status="pending", depends_on=["prepare"])

    assert partial_upstream_publish_issue(
        publish, _dag(publish_step={"output_shape": "PublishResult"}), {"prepare": prepare},
    ) is not None


def test_succeeded_dependency_does_not_trip_the_gate():
    prepare = _step("prepare", status="done", result=_SUCCEEDED)
    publish = _step("publish", status="pending", depends_on=["prepare"])

    assert partial_upstream_publish_issue(
        publish, _dag(publish_step={"expects": ["publish"]}), {"prepare": prepare},
    ) is None


def test_ordinary_consumers_still_accept_partial_output():
    """The gate is for external publishes only — partial output stays usable."""
    prepare = _step("prepare", status="done", result=_PARTIAL)
    summarize = _step("summarize", status="pending", depends_on=["prepare"])
    dag = {"steps": [{"key": "prepare"}, {"key": "summarize", "depends_on": ["prepare"]}]}

    assert partial_upstream_publish_issue(summarize, dag, {"prepare": prepare}) is None


def test_status_as_payload_data_on_a_non_envelope_schema_is_not_a_partial_signal():
    """A provider receipt whose schema carries ``status`` as DATA must not be
    read as the envelope control signal — same rule the dispatcher follows."""
    receipt_schema = {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
    prepare = _step("prepare", status="done", result={"status": "partial"}, schema=receipt_schema)
    publish = _step("publish", status="pending", depends_on=["prepare"])

    assert partial_upstream_publish_issue(
        publish, _dag(publish_step={"expects": ["publish"]}), {"prepare": prepare},
    ) is None


def test_operator_note_on_the_publish_step_lifts_the_gate():
    """A retry with guidance is the operator saying "publish it anyway"."""
    prepare = _step("prepare", status="done", result=_PARTIAL)
    publish = _step(
        "publish", status="pending", depends_on=["prepare"],
        human_input_response={"response": "post the partial draft as is", "user": "op"},
    )

    assert partial_upstream_publish_issue(
        publish, _dag(publish_step={"expects": ["publish"]}), {"prepare": prepare},
    ) is None


# ── the executor applies it at the step boundary ─────────────────────


@pytest.mark.asyncio
async def test_run_cycle_fails_the_publish_step_before_any_post_without_asking_user(
    db_session, monkeypatch,
):
    from packages.core.constants.execution import ExecutionPlanStatus
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    async def fail_llm(*_args, **_kwargs):
        raise AssertionError("the boundary gate is deterministic; no LLM call expected")

    async def quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fail_llm)
    monkeypatch.setattr(PlanExecutor, "_announce", staticmethod(quiet))
    monkeypatch.setattr(PlanExecutor, "_emit_task_event", staticmethod(quiet))

    from packages.core.models.user import User

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    operator = User(
        id=generate_ulid(), entity_id=entity_id, email=f"{generate_ulid()}@gate.example",
        password_hash="test-only", role="owner",
    )
    db_session.add(operator)
    db_session.add(Task(
        id=task_id, entity_id=entity_id, creator_id=operator.id, title="发布一条 X 帖子",
        status="in_progress", priority=3, task_type="general", details={},
    ))
    db_session.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id,
        status=ExecutionPlanStatus.RUNNING.value,
        plan_dag=_dag(publish_step={"expects": ["publish"]}),
    ))
    prepare = ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id, step_key="prepare",
        kind="subagent", step_status="done", result=_PARTIAL,
        expected_output_schema=step_result_envelope_schema(),
    )
    publish = ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id, step_key="publish",
        kind="subagent", step_status="pending", depends_on=["prepare"],
        params={"prompt": "publish it"}, expected_output_schema=step_result_envelope_schema(),
    )
    db_session.add_all([prepare, publish])
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    await PlanExecutor(session_factory=session_factory).run_cycle(plan_id)

    await db_session.refresh(publish)
    assert publish.step_status == ExecutionStepStatus.FAILED
    assert publish.error["type"] == UPSTREAM_PARTIAL_RESULT_ERROR_TYPE
    assert publish.error["failure"]["requires_human"] is False
    assert "prepare" in publish.error["message"]

    task = await db_session.get(Task, task_id)
    await db_session.refresh(task)
    assert task.status != "waiting_on_customer"


# ── retrying the upstream step revives the boundary-failed publish ────


def test_retrying_the_upstream_step_revives_a_boundary_failed_publish_step():
    from apps.api.routers.plans import _reset_skipped_downstream_steps_for_retry
    from packages.core.models.user import User

    user = User(id="u1", entity_id="entity", email="op@example.com", password_hash="x", role="owner")
    prepare = _step("prepare", status="pending")
    publish = _step("publish", status="failed", depends_on=["prepare"])
    publish.error = {"type": UPSTREAM_PARTIAL_RESULT_ERROR_TYPE, "message": "held"}
    unrelated = _step("other", status="failed", depends_on=["prepare"])
    unrelated.error = {"type": "StepResultFailed", "message": "really failed"}

    revived = _reset_skipped_downstream_steps_for_retry(
        [prepare, publish, unrelated], retried_step_keys={"prepare"}, user=user,
    )

    assert [s.step_key for s in revived] == ["publish"]
    assert publish.step_status == ExecutionStepStatus.PENDING
    assert publish.error is None
    # A step that genuinely ran and failed keeps its own retry decision.
    assert unrelated.step_status == ExecutionStepStatus.FAILED
