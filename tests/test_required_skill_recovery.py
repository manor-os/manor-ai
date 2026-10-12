from types import SimpleNamespace
import json

import pytest

from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result
from packages.core.plans.executor import PlanExecutor
from packages.core.workers import internal


def _messages():
    return [
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {
            "name": "invoke_skill", "arguments": json.dumps({"skill_id": "review"}),
        }}]},
        {"role": "tool", "tool_call_id": "call-1", "content": runtime_format_invoke_skill_result("review", {
            "stop_reason": "completed", "content": json.dumps({
                "status": "BLOCKED", "blocking_items": [{"item": "Missing eligibility_matrix_ref"}],
            }),
        })},
    ]


def test_required_skill_block_preserves_reason_without_parent_fallback():
    with pytest.raises(internal.RequiredSkillBlocked) as exc:
        internal._raise_if_required_skill_blocked(_messages(), ["review"])
    assert "eligibility_matrix_ref" in exc.value.summary
    assert exc.value.failure["retryable"] is False
    assert exc.value.failure["requires_human"] is False


def test_unrelated_tool_content_is_not_a_required_skill_block():
    messages = _messages()
    messages[0]["tool_calls"][0]["function"]["name"] = "read_file"
    internal._raise_if_required_skill_blocked(messages, ["review"])


def test_successful_retry_supersedes_old_blocker():
    messages = _messages()
    messages.extend([
        {"role": "assistant", "tool_calls": [{"id": "call-2", "function": {
            "name": "invoke_skill", "arguments": '{"skill_id":"review"}',
        }}]},
        {"role": "tool", "tool_call_id": "call-2", "content": '{"status":"completed"}'},
    ])
    internal._raise_if_required_skill_blocked(messages, ["review"])


def test_strategist_gets_bounded_concrete_failure_evidence():
    from packages.core.ai.runtime.strategist import _format_failed_task_evidence

    ctx = SimpleNamespace(recent_tasks=[SimpleNamespace(
        id="task", title="Rights review", status="failed", actual_output={
            "supervisor_evidence": "Missing required Skill evidence",
            "steps": [{"error": {"type": "RequiredSkillBlocked",
                                  "message": "Missing eligibility_matrix_ref " + "x" * 4000}}],
        },
    )])
    evidence = _format_failed_task_evidence(ctx)
    assert "eligibility_matrix_ref" in evidence
    assert "RequiredSkillBlocked" in evidence
    assert len(evidence) < 2500


@pytest.mark.asyncio
async def test_required_skill_block_returns_to_strategist_without_unchanged_replan():
    plan = SimpleNamespace(id="plan", task_id="task")
    step = SimpleNamespace(step_status="failed", error={
        "type": "RequiredSkillBlocked", "message": "Missing eligibility_matrix_ref",
        "failure": {"retryable": False, "requires_human": False},
    })
    assert await PlanExecutor._maybe_replan(None, plan, [step]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_submits", [False, True])
async def test_worker_cannot_submit_or_fallback_over_required_skill_block(monkeypatch, parent_submits):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session():
        yield None

    async def context(*args, **kwargs):
        return SimpleNamespace(system_prompt="sys", tools=[], allowed_tool_names=set(),
                               tool_profile="workspace", model="test", llm_metadata=None,
                               runtime_envelope=None)

    async def loop(**kwargs):
        if parent_submits:
            kwargs["dynamic_tool_handlers"]["submit_result"]({"status": "succeeded", "summary": "Done"})
        return SimpleNamespace(result=SimpleNamespace(
            messages=_messages(), content="Done", usage={}, rounds=1, tool_calls_made=[], stop_reason="completed",
        ), run=None)

    async def persist(*args, **kwargs):
        return 0

    async def no_fallback(*args, **kwargs):
        pytest.fail("A blocked required Skill must not be replaced by parent fallback")

    monkeypatch.setattr("packages.core.database.async_session", session)
    monkeypatch.setattr("packages.core.ai.context.build_agent_context", context)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", persist)
    monkeypatch.setattr(internal, "_force_submit_result_round", no_fallback)
    with pytest.raises(internal.RequiredSkillBlocked, match="eligibility_matrix_ref"):
        await internal._exec_subagent({
            "params": {"prompt": "Review", "skill_refs": ["review"]}, "entity_id": "entity",
            "resolved_agent_id": "agent", "workspace_id": "workspace", "task_id": "task",
        })


@pytest.mark.asyncio
async def test_partial_plan_success_cannot_hide_required_skill_block(db_session, monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.constants.supervisor import SupervisorVerdict

    async def model(*args, **kwargs):
        return '{"verdict":"completed","evidence":"The first step finished"}', {}

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", model)
    entity_id = generate_ulid()
    task = Task(id=generate_ulid(), entity_id=entity_id, title="Review evidence", status="in_progress")
    plan = ExecutionPlan(id=generate_ulid(), entity_id=entity_id, task_id=task.id, status="failed", plan_dag={})
    db_session.add_all([task, plan,
        ExecutionStep(id=generate_ulid(), entity_id=entity_id, plan_id=plan.id,
                      step_key="inspect", kind="subagent", step_status="done", result={"text": "Inspected"}),
        ExecutionStep(id=generate_ulid(), entity_id=entity_id, plan_id=plan.id,
                      step_key="review", kind="subagent", step_status="failed", error={
                          "type": "RequiredSkillBlocked", "message": "Missing eligibility_matrix_ref",
                          "failure": {"retryable": False, "requires_human": False},
                      }),
    ])
    await db_session.flush()
    decision = await PlanExecutor._supervise_outcome(db_session, plan, "failed")
    assert decision.verdict is SupervisorVerdict.FAILED
    assert "eligibility_matrix_ref" in decision.evidence
