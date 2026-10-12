import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ai.engine import ChatMessage, LLMConfig
from packages.core.ai.runtime import (
    RuntimeTaskAgentTurnResult,
    RuntimeTaskFinalResponseResult,
    runtime_classify_task_complexity,
    runtime_planner_task_prompt,
    runtime_task_ticket_prompt,
)
from packages.core.ai.task_runner import TaskRunner
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.constants.approvals import ApprovalStatus
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_learning import RuntimeEvidence
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.workspace import Agent
from packages.core.services.auth_service import hash_password


class _FakeTaskEngine:
    def __init__(self):
        self.config = LLMConfig(model="test-model")

    async def chat(self, messages, **kwargs):
        system_prompt = str(kwargs.get("system_prompt") or "")
        if "task supervisor" in system_prompt.lower():
            return ChatMessage(
                role="assistant",
                content='{"verdict":"done","summary":"ok","reason":"deliverable complete"}',
                usage={"prompt_tokens": 3, "completion_tokens": 2},
            )
        return ChatMessage(
            role="assistant",
            content="Completed the founder content brief and stored the handoff summary.",
            usage={"prompt_tokens": 5, "completion_tokens": 7},
        )


def _orphan_runtime_approval(task: Task) -> HitlRequest:
    """Model a guard commit that survived before its Task marker was written."""

    return HitlRequest(
        id=generate_ulid(),
        entity_id=task.entity_id,
        action_key="scheduled.runtime.action",
        capability_id="scheduled.runtime.action",
        origin_kind="tool_call",
        origin_task_id=task.id,
        origin_conversation_id=task.conversation_id,
        status=ApprovalStatus.PENDING.value,
        dedup_key=f"task-runner-orphan:{task.id}",
        context={},
    )


def test_task_runner_priority_labels_follow_task_ordering():
    urgent_prompt = runtime_task_ticket_prompt(
        {
            "title": "Handle urgent lead",
            "priority": 5,
            "task_type": "general",
            "details": {},
        }
    )
    lowest_prompt = runtime_task_ticket_prompt(
        {
            "title": "Archive old note",
            "priority": 1,
            "task_type": "general",
            "details": {},
        }
    )

    assert "Priority: Critical" in urgent_prompt
    assert "Priority: Minimal" in lowest_prompt


def test_task_complexity_uses_higher_numbers_as_higher_priority():
    critical_summary = SimpleNamespace(
        details={},
        priority=5,
        task_type="summary",
    )
    low_priority_cron = SimpleNamespace(
        details={"scheduled_job_id": "job_123"},
        priority=1,
        task_type="general",
    )

    assert runtime_classify_task_complexity(critical_summary) == "primary"
    assert runtime_classify_task_complexity(low_priority_cron) == "worker"


def test_task_runner_prompt_includes_runtime_knowledge_query():
    prompt = runtime_task_ticket_prompt(
        {
            "title": "Prepare lease options",
            "priority": 2,
            "task_type": "general",
            "details": {
                "runtime_context": {
                    "knowledge_query": "leasing FAQ and approved concessions",
                }
            },
        }
    )

    assert "Knowledge query: leasing FAQ and approved concessions" in prompt


def test_task_runner_prompt_includes_predecessor_files():
    prompt = runtime_task_ticket_prompt(
        {
            "title": "Build calendar from strategy",
            "priority": 2,
            "task_type": "general",
            "details": {
                "dep_outputs": [
                    {
                        "task_title": "Draft strategy",
                        "result_summary": "Strategy doc is ready.",
                        "files": [{"name": "strategy.md", "fs_path": "/workspace/strategy.md"}],
                    }
                ]
            },
        }
    )

    assert "## Predecessor Task Outputs" in prompt
    assert "Strategy doc is ready." in prompt
    assert "strategy.md (/workspace/strategy.md)" in prompt


def test_planner_prompt_formats_predecessor_outputs():
    task = SimpleNamespace(
        title="Build calendar from strategy",
        description="Use upstream strategy.",
        details={
            "dep_outputs": [
                {
                    "task_title": "Draft strategy",
                    "result_summary": "Strategy doc is ready.",
                    "files": [{"name": "strategy.md", "fs_path": "/workspace/strategy.md"}],
                }
            ]
        },
        input_contract=None,
        expected_output=None,
        owner_service_key="content",
        delegate_service_keys=[],
    )

    prompt = runtime_planner_task_prompt(task)

    assert "# Predecessor task outputs" in prompt
    assert "Strategy doc is ready." in prompt
    assert "strategy.md (/workspace/strategy.md)" in prompt


def test_planner_fallback_keeps_runtime_context_in_prompt():
    from packages.core.plans.planner import _fallback_plan

    task = SimpleNamespace(
        title="Prepare lease options",
        description="Match the customer's budget.",
        details={
            "runtime_context": {
                "knowledge_query": "leasing FAQ and approved concessions",
                "required_refs": ["doc:leasing-faq"],
            }
        },
        input_contract=None,
        expected_output=None,
        owner_service_key="leasing_consultant",
        delegate_service_keys=[],
    )
    ctx = SimpleNamespace(allowed_service_keys={"leasing_consultant"})

    plan = _fallback_plan(task, ctx)

    assert "leasing FAQ and approved concessions" in plan.steps[0].params["prompt"]
    assert "doc:leasing-faq" in plan.steps[0].params["prompt"]


@pytest.mark.asyncio
async def test_missing_scheduled_skill_transitions_task_to_failed(
    db_session,
    monkeypatch,
):
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module

    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=generate_ulid(),
        agent_id=MANOR_AGENT_ID,
        title="Run removed scheduled Skill",
        status="pending",
        priority=2,
        task_type="general",
        details={"scheduled_skill_id": generate_ulid()},
    )
    request = _orphan_runtime_approval(task)
    request_id = request.id
    db_session.add_all([task, request])
    await db_session.commit()

    async def fake_resolve_billable_user_id(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        task_runner_module,
        "runtime_resolve_task_billable_user_id",
        fake_resolve_billable_user_id,
    )

    result = await TaskRunner(
        engine=_FakeTaskEngine(),
        session_factory=db_module.async_session,
    ).run(task_id)

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert result == {
        "task_id": task_id,
        "status": "failed",
        "error": "scheduled skill not found",
    }
    assert persisted.status == "failed"
    assert persisted.details["failure_reason"] == "scheduled skill not found"
    assert persisted_request.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_invalid_initiative_marker_expires_orphan_runtime_approval(
    db_session,
):
    import packages.core.database as db_module

    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        agent_id=MANOR_AGENT_ID,
        title="Reject corrupt Initiative lineage",
        status="pending",
        priority=2,
        task_type="ai_generated",
        details={"agent_initiative": {"version": "1"}},
    )
    request = _orphan_runtime_approval(task)
    task_id = task.id
    request_id = request.id
    db_session.add_all([task, request])
    await db_session.commit()

    result = await TaskRunner(
        engine=_FakeTaskEngine(),
        session_factory=db_module.async_session,
    ).run(task_id)

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert result["error"] == "initiative_task_marker_invalid"
    assert persisted.status == "failed"
    assert persisted.details["failure_reason"] == "initiative_task_marker_invalid"
    assert persisted_request.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_initiative_admission_rejection_expires_orphan_runtime_approval(
    db_session,
    monkeypatch,
):
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module
    from packages.core.services import agent_initiative_decision

    owner_user_id = generate_ulid()
    subscription_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        agent_id=MANOR_AGENT_ID,
        title="Reject stale Initiative admission",
        status="pending",
        priority=2,
        task_type="ai_generated",
        details={
            "agent_initiative": {
                "version": "1",
                "initiative_id": generate_ulid(),
                "initiative_run_id": generate_ulid(),
                "initiative_revision": 1,
                "agent_subscription_id": subscription_id,
                "owner_user_id": owner_user_id,
            }
        },
    )
    request = _orphan_runtime_approval(task)
    task_id = task.id
    request_id = request.id
    db_session.add_all([task, request])
    await db_session.commit()

    async def fake_resolve_billable_user_id(*_args, **_kwargs):
        return None

    async def reject_admission(*_args, **_kwargs):
        raise agent_initiative_decision.InitiativeAdmissionRejected(
            "initiative_not_active"
        )

    monkeypatch.setattr(
        task_runner_module,
        "runtime_resolve_task_billable_user_id",
        fake_resolve_billable_user_id,
    )
    monkeypatch.setattr(
        agent_initiative_decision,
        "mark_initiative_run_started",
        reject_admission,
    )

    result = await TaskRunner(
        engine=_FakeTaskEngine(),
        session_factory=db_module.async_session,
    ).run(task_id)

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert result["error"] == "initiative_not_active"
    assert persisted.status == "failed"
    assert persisted.details["failure_reason"] == "initiative_not_active"
    assert persisted_request.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_initiative_json_repair_keeps_task_performance_lineage(
    db_session,
    monkeypatch,
):
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module
    from packages.core.services import agent_initiative_decision
    from packages.core.services import model_resolver
    from packages.core.services import workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    task_id = generate_ulid()
    owner_user_id = generate_ulid()
    subscription_id = generate_ulid()
    db_session.add(Task(
        id=task_id,
        entity_id=generate_ulid(),
        agent_id="manor-master",
        agent_type="manor_agent",
        title="Repair Initiative decision JSON",
        status="pending",
        priority=2,
        task_type="ai_generated",
        creator_id=owner_user_id,
        owner_id=owner_user_id,
        owner_subscription_id=subscription_id,
        delegate_service_keys=[],
        details={
            "done_when": "A strict Initiative decision is returned.",
            "agent_initiative": {
                "version": "1",
                "initiative_id": generate_ulid(),
                "initiative_run_id": generate_ulid(),
                "initiative_revision": 1,
                "agent_subscription_id": subscription_id,
                "owner_user_id": owner_user_id,
            },
        },
    ))
    await db_session.commit()

    async def fake_resolve_billable_user_id(*_args, **_kwargs):
        return owner_user_id

    async def fake_mark_started(*_args, **_kwargs):
        return None

    async def fake_resolve_model(role, **_kwargs):
        return f"{role}-model"

    async def fake_resolve_metadata(*_args, **_kwargs):
        return None

    async def fake_runtime(*_args, **_kwargs):
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            task_id=task_id,
            is_master=True,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_agent_turn(**kwargs):
        provider_call_ids.append(kwargs["performance_tool_call_id"])
        return RuntimeTaskAgentTurnResult(
            messages=list(kwargs["messages"]),
            tools=list(kwargs["tools"]),
            loaded_tool_names=set(kwargs["loaded_tool_names"]),
            response_text="not strict JSON",
            tool_names=[],
            usage={},
            had_tool_calls=False,
        )

    async def fake_supervisor(**kwargs):
        provider_call_ids.append(kwargs["performance_tool_call_id"])
        return {"verdict": "done", "reason": "agent work is complete"}

    async def fake_repair(**kwargs):
        repair_call.update(kwargs)
        return RuntimeTaskFinalResponseResult(
            messages=list(kwargs["messages"]),
            response_text=(
                '{"version":"1","decision":"silent",'
                '"reason_code":"nothing_new","evidence_refs":[]}'
            ),
            usage={},
            finalized=True,
        )

    async def fake_assemble_runtime_prompt(**_kwargs):
        return SimpleNamespace(
            prompt="Initiative system prompt",
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=None,
        )

    async def fake_noop(*_args, **_kwargs):
        return None

    provider_call_ids: list[str] = []
    repair_call: dict = {}
    monkeypatch.setattr(
        task_runner_module,
        "runtime_resolve_task_billable_user_id",
        fake_resolve_billable_user_id,
    )
    monkeypatch.setattr(
        agent_initiative_decision,
        "mark_initiative_run_started",
        fake_mark_started,
    )
    monkeypatch.setattr(model_resolver, "resolve_model_for_user", fake_resolve_model)
    monkeypatch.setattr(
        model_resolver,
        "resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_runtime)
    monkeypatch.setattr(
        task_runner_module,
        "runtime_task_llm_billing_context",
        lambda **_kwargs: _NoopBillingContext(),
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_execute_task_agent_turn",
        fake_agent_turn,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_review_task_agent_output",
        fake_supervisor,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_execute_task_final_response",
        fake_repair,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_record_task_runner_execution_evidence",
        fake_noop,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_persist_task_runner_runtime_events",
        fake_noop,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_emit_task_runner_status_event",
        lambda *_args, **_kwargs: None,
    )

    runner = TaskRunner(
        engine=_FakeTaskEngine(),
        session_factory=db_module.async_session,
    )
    monkeypatch.setattr(runner, "_assemble_runtime_prompt", fake_assemble_runtime_prompt)

    result = await runner.run(task_id)

    assert result["status"] == "completed"
    assert repair_call["performance_run_id"] == task_id
    assert repair_call["performance_tool_call_id"] == "task-initiative-repair-2"
    assert repair_call["performance_tool_call_id"] not in provider_call_ids


def test_task_runner_propagates_proposal_external_authorization_as_runtime_metadata():
    from packages.core.ai.task_runner import _task_runtime_metadata

    authorization = {
        "version": 1,
        "task_id": "publish-task",
        "provider": "youtube",
        "destination": "studio.youtube.com",
        "visibility": "public",
    }
    task = SimpleNamespace(
        details={"proposal_external_authorization": authorization},
    )

    assert _task_runtime_metadata(task) == {
        "proposal_external_authorization": authorization,
    }


@pytest.mark.asyncio
async def test_task_runner_records_runtime_evidence_for_standalone_agent_tasks(client, db_session, monkeypatch):
    import packages.core.ai.llm_client as llm_client
    import packages.core.database as db_module
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    entity_id = generate_ulid()
    workspace_id = None
    task_id = generate_ulid()
    owner_user_id = generate_ulid()
    db_session.add(User(
        id=owner_user_id, entity_id=entity_id, email=f"{owner_user_id}@test.com",
        password_hash=hash_password("pass123"), role="owner", status="active",
    ))
    db_session.add(Task(
        id=task_id,
        entity_id=entity_id,
        agent_id=MANOR_AGENT_ID,
        creator_id=owner_user_id,
        owner_id=owner_user_id,
        title="Draft founder content brief",
        description="Create a concrete brief for tomorrow's launch post.",
        status="pending",
        priority=4,
        task_type="general",
        details={
            "done_when": "A concrete brief exists.",
            "seeded_by": "sandbox_demo",
        },
        owner_service_key="content",
        delegate_service_keys=["research"],
    ))
    await db_session.commit()

    monkeypatch.setattr(llm_client, "llm_billing_context", lambda *args, **kwargs: _NoopBillingContext())

    async def _fake_runtime(*_args, **_kwargs):
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id=workspace_id,
            task_id=task_id,
            is_master=False,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", _fake_runtime)

    terminal_fence_events = []
    runner = TaskRunner(
        engine=_FakeTaskEngine(),
        session_factory=db_module.async_session,
        before_terminal_commit=lambda: terminal_fence_events.append("before"),
        after_terminal_commit=lambda: terminal_fence_events.append("after"),
    )
    result = await runner.run(task_id)

    assert result["status"] == "completed"
    assert terminal_fence_events == ["before", "after"]

    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "completed"
    assert task.actual_output["response"] == ("Completed the founder content brief and stored the handoff summary.")

    evidence = (
        (await db_session.execute(select(RuntimeEvidence).where(RuntimeEvidence.task_id == task_id))).scalars().all()
    )
    assert len(evidence) == 1
    row = evidence[0]
    assert row.evidence_type == "task_run"
    assert row.source == "task_runner"
    assert row.status == "succeeded"
    assert row.workspace_id == workspace_id
    assert row.details["owner_service_key"] == "content"
    assert row.details["delegate_service_keys"] == ["research"]
    assert "Completed the founder content brief" in row.details["actual_output_excerpt"]


@pytest.mark.asyncio
async def test_task_runner_terminal_uncertainty_fails_without_another_turn(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.ai.llm_client as llm_client
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    task_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        agent_id=MANOR_AGENT_ID,
        title="Click one browser control",
        description="Run the bounded Chrome sequence once.",
        status="pending",
        priority=4,
        task_type="general",
        details={"done_when": "The browser action is reconciled.", "max_turns": 3},
        creator_id=owner_user_id,
        owner_id=owner_user_id,
        owner_service_key="browser",
        delegate_service_keys=[],
    )
    db_session.add_all([
        User(
            id=owner_user_id,
            entity_id=entity_id,
            email="chrome-task-owner@test.com",
            display_name="Chrome Task Owner",
            password_hash=hash_password("pass123"),
            role="owner",
            status="active",
        ),
        task,
    ])
    await db_session.commit()

    uncertainty = {
        "status": "error",
        "error": "mutation_timeout_uncertain",
        "content": "Chrome mutation outcome is unknown; observe and reconcile.",
        "stop_parent": True,
        "stop_reason": "uncertain_result",
        "terminal_failure": True,
        "retryable": False,
        "action_executed": "unknown",
        "do_not_replay": True,
        "recommended_next_action": "observe_and_reconcile",
    }
    turn_calls = 0

    async def fake_runtime(*_args, **_kwargs):
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id=None,
            task_id=task_id,
            is_master=False,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_agent_turn(**kwargs):
        nonlocal turn_calls
        turn_calls += 1
        return RuntimeTaskAgentTurnResult(
            messages=list(kwargs["messages"]),
            tools=list(kwargs["tools"]),
            loaded_tool_names=set(kwargs["loaded_tool_names"]),
            response_text=uncertainty["content"],
            tool_names=["mcp__chrome__run_sequence"],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            had_tool_calls=True,
            supervisor_evidence=[{
                "tool_name": "mcp__chrome__run_sequence",
                "result": json.dumps(uncertainty),
            }],
            terminal_control=uncertainty,
        )

    async def forbidden_supervisor(**_kwargs):
        raise AssertionError("terminal uncertainty must not enter supervisor")

    monkeypatch.setattr(
        llm_client,
        "llm_billing_context",
        lambda *args, **kwargs: _NoopBillingContext(),
    )
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_runtime)
    monkeypatch.setattr(
        task_runner_module,
        "runtime_execute_task_agent_turn",
        fake_agent_turn,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_review_task_agent_output",
        forbidden_supervisor,
    )

    result = await TaskRunner(
        engine=_FakeTaskEngine(),
        session_factory=db_module.async_session,
    ).run(task_id)

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    assert turn_calls == 1
    assert result["status"] == "failed"
    assert result["turns_used"] == 1
    assert result["terminal_control"] == uncertainty
    assert persisted.status == "failed"
    assert persisted.actual_output["terminal_control"] == uncertainty
    assert persisted.details["ai_result"]["terminal_control"] == uncertainty


@pytest.mark.asyncio
async def test_task_runner_uses_owner_user_when_creator_is_missing(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.ai.llm_client as llm_client
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module
    import packages.core.services.model_resolver as model_resolver
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    seen: dict[str, list | dict] = {
        "models": [],
        "metadata": [],
    }
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    assignee_user_id = generate_ulid()
    task_id = generate_ulid()

    db_session.add_all(
        [
            User(
                id=owner_user_id,
                entity_id=entity_id,
                email="owner-task-fallback@test.com",
                display_name="Owner Task Fallback",
                password_hash=hash_password("pass123"),
                role="owner",
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                agent_id=MANOR_AGENT_ID,
                title="Run owner-scoped task",
                description="Use the owner's BYOK routing.",
                status="pending",
                priority=4,
                task_type="general",
                details={"done_when": "Owner-scoped run completes."},
                creator_id=None,
                owner_id=owner_user_id,
                assignee_id=assignee_user_id,
                owner_service_key="content",
                delegate_service_keys=[],
            ),
        ]
    )
    await db_session.commit()

    monkeypatch.setattr(llm_client, "llm_billing_context", lambda *args, **kwargs: _NoopBillingContext())

    async def fake_resolve_model_for_user(role, *, user_id=None, entity_id=None, db=None):
        seen["models"].append({"role": role, "user_id": user_id, "entity_id": entity_id, "db": db})
        return f"{role}-owner-model"

    async def fake_resolve_metadata_for_user(role, *, user_id=None, entity_id=None, db=None):
        seen["metadata"].append({"role": role, "user_id": user_id, "entity_id": entity_id, "db": db})
        return {"llm_api_key": f"{role}-owner-key"}

    async def fake_runtime(_db, *, entity_id=None, user_id=None, **kwargs):
        seen["runtime"] = {
            "entity_id": entity_id,
            "user_id": user_id,
            "agent_id": kwargs.get("agent_id"),
            "workspace_id": kwargs.get("workspace_id"),
            "task_id": kwargs.get("task_id"),
        }
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id=None,
            task_id=task_id,
            is_master=False,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    def fake_billing_context(**kwargs):
        seen["billing_context"] = dict(kwargs)
        return _NoopBillingContext()

    async def fake_agent_turn(**kwargs):
        seen["agent_turn"] = dict(kwargs)
        return RuntimeTaskAgentTurnResult(
            messages=list(kwargs["messages"]),
            tools=list(kwargs["tools"]),
            loaded_tool_names=set(kwargs["loaded_tool_names"]),
            response_text="Completed with owner BYOK context.",
            tool_names=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            had_tool_calls=False,
        )

    async def fake_supervisor(**kwargs):
        seen["supervisor"] = dict(kwargs)
        return {"verdict": "done", "summary": "ok", "reason": "owner context preserved"}

    monkeypatch.setattr(model_resolver, "resolve_model_for_user", fake_resolve_model_for_user)
    monkeypatch.setattr(model_resolver, "resolve_llm_metadata_for_user", fake_resolve_metadata_for_user)
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_runtime)
    monkeypatch.setattr(task_runner_module, "runtime_task_llm_billing_context", fake_billing_context)
    monkeypatch.setattr(task_runner_module, "runtime_execute_task_agent_turn", fake_agent_turn)
    monkeypatch.setattr(task_runner_module, "runtime_review_task_agent_output", fake_supervisor)

    runner = TaskRunner(engine=_FakeTaskEngine(), session_factory=db_module.async_session)
    result = await runner.run(task_id)

    assert result["status"] == "completed"
    assert {call["user_id"] for call in seen["models"]} == {owner_user_id}
    assert {call["user_id"] for call in seen["metadata"]} == {owner_user_id}
    assert seen["runtime"]["user_id"] == owner_user_id
    assert seen["billing_context"]["user_id"] == owner_user_id
    assert "byok" not in seen["billing_context"]
    assert seen["agent_turn"]["user_id"] == owner_user_id
    assert seen["agent_turn"]["metadata"] == {"llm_api_key": "primary-owner-key"}
    assert seen["supervisor"]["tool_evidence"] == []


@pytest.mark.asyncio
async def test_task_runner_prefers_fixed_agent_runtime_config(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module
    import packages.core.services.model_resolver as model_resolver
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    entity_id = generate_ulid()
    agent_id = generate_ulid()
    creator_user_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add_all(
        [
            User(
                id=creator_user_id,
                entity_id=entity_id,
                email="fixed-agent-task-creator@test.com",
                display_name="Fixed Agent Task Creator",
                password_hash=hash_password("pass123"),
                role="owner",
                status="active",
            ),
            Agent(
                id=agent_id,
                entity_id=entity_id,
                name="Fixed Runtime Agent",
                config={
                    "model_mode": "fixed",
                    "model": "openai/gpt-5.6-terra",
                    "temperature": 0.1,
                    "max_tokens": 7168,
                },
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                agent_id=agent_id,
                title="Run with a fixed Agent model",
                description="Use the Agent's saved runtime configuration.",
                status="pending",
                priority=4,
                task_type="general",
                details={"done_when": "The fixed Agent run completes."},
                creator_id=creator_user_id,
                owner_id=creator_user_id,
                owner_service_key="content",
                delegate_service_keys=[],
            ),
        ]
    )
    await db_session.commit()

    async def fake_resolve_model_for_user(role, **_kwargs):
        return f"{role}-fallback-model"

    async def fake_resolve_metadata_for_user(*_args, **_kwargs):
        return None

    async def fake_runtime(_db, **_kwargs):
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id=None,
            task_id=task_id,
            is_master=False,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_agent_turn(**kwargs):
        seen.update(kwargs)
        return RuntimeTaskAgentTurnResult(
            messages=list(kwargs["messages"]),
            tools=list(kwargs["tools"]),
            loaded_tool_names=set(kwargs["loaded_tool_names"]),
            response_text="Completed with fixed Agent settings.",
            tool_names=[],
            usage={},
            had_tool_calls=False,
        )

    async def fake_supervisor(**_kwargs):
        return {"verdict": "done", "summary": "ok", "reason": "complete"}

    async def fake_assemble_runtime_prompt(**_kwargs):
        return SimpleNamespace(
            prompt="Fixed Agent prompt",
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=None,
        )

    seen: dict = {}
    monkeypatch.setattr(model_resolver, "resolve_model_for_user", fake_resolve_model_for_user)
    monkeypatch.setattr(
        model_resolver,
        "resolve_llm_metadata_for_user",
        fake_resolve_metadata_for_user,
    )
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_runtime)
    monkeypatch.setattr(
        task_runner_module,
        "runtime_task_llm_billing_context",
        lambda **_kwargs: _NoopBillingContext(),
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_execute_task_agent_turn",
        fake_agent_turn,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_review_task_agent_output",
        fake_supervisor,
    )

    runner = TaskRunner(engine=_FakeTaskEngine(), session_factory=db_module.async_session)
    monkeypatch.setattr(runner, "_assemble_runtime_prompt", fake_assemble_runtime_prompt)

    result = await runner.run(task_id)

    assert result["status"] == "completed"
    assert runner._engine.config.model == "openai/gpt-5.6-terra"
    assert seen["temperature"] == 0.1
    assert seen["max_tokens"] == 7168
