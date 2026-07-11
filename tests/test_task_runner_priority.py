from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ai.engine import ChatMessage, LLMConfig
from packages.core.ai.runtime import (
    RuntimeTaskAgentTurnResult,
    runtime_classify_task_complexity,
    runtime_planner_task_prompt,
    runtime_task_ticket_prompt,
)
from packages.core.ai.task_runner import TaskRunner
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_learning import RuntimeEvidence
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace


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
async def test_task_runner_records_runtime_evidence_for_legacy_tasks(client, db_session, monkeypatch):
    import packages.core.ai.llm_client as llm_client
    import packages.core.database as db_module
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add_all(
        [
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Founder OS",
                operating_model={},
                settings={},
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Draft founder content brief",
                description="Create a concrete brief for tomorrow's launch post.",
                status="pending",
                priority=4,
                task_type="general",
                details={"done_when": "A concrete brief exists."},
                owner_service_key="content",
                delegate_service_keys=["research"],
            ),
        ]
    )
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

    runner = TaskRunner(engine=_FakeTaskEngine(), session_factory=db_module.async_session)
    result = await runner.run(task_id)

    assert result["status"] == "completed"

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
    workspace_id = generate_ulid()
    owner_user_id = generate_ulid()
    assignee_user_id = generate_ulid()
    task_id = generate_ulid()

    db_session.add_all(
        [
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Owner BYOK Workspace",
                operating_model={},
                settings={},
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
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
            workspace_id=workspace_id,
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
