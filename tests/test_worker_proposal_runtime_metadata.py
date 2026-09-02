from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from packages.core.ai.context import build_agent_context
from packages.core.ai.runtime import (
    ChatSurface,
    RuntimeEnvelope,
    RuntimePrincipal,
    RuntimePrincipalKind,
    RuntimeProfile,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.worker import Worker, WorkLease
from packages.core.workers import internal


def _authorization(*, task_id: str, workspace_id: str) -> dict:
    return {
        "version": 1,
        "authorization_id": f"proposal-item:{task_id}",
        "task_id": task_id,
        "workspace_id": workspace_id,
        "provider": "youtube",
        "action": "publish_video",
        "destination": "studio.youtube.com",
        "visibility": "public",
        "intended_channel": "paired_chrome_signed_in_channel",
        "max_executions": 1,
        "consumed_at": None,
    }


@pytest.mark.asyncio
async def test_lease_snapshot_inherits_task_proposal_external_authorization(
    db_session,
    monkeypatch,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    user_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    authorization = _authorization(task_id=task_id, workspace_id=workspace_id)
    now = datetime.now(UTC)

    db_session.add_all(
        [
            User(
                id=user_id,
                entity_id=entity_id,
                email=f"{user_id.lower()}@example.test",
                password_hash="not-used-by-this-test",
                role="owner",
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Publish the verified video",
                status="in_progress",
                creator_id=user_id,
                details={"proposal_external_authorization": authorization},
            ),
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Internal worker",
                capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
                monthly_spent_usd=Decimal(0),
                auto_pause_on_budget=True,
                status="active",
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="publish_youtube",
                kind="subagent",
                params={"prompt": "Publish one video"},
                depends_on=[],
                step_status="running",
                risk_level="high",
                attempt_count=1,
                max_attempts=3,
                current_lease_id=lease_id,
                started_at=now,
            ),
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                worker_id=worker_id,
                status="active",
                lease_until=now + timedelta(minutes=5),
                last_heartbeat_at=now,
            ),
        ]
    )
    await db_session.commit()

    captured: dict = {}

    async def fake_execute(snapshot):
        captured.update(snapshot)
        return {"result": {"text": "done"}}

    monkeypatch.setattr(internal, "_execute_by_kind", fake_execute)

    outcome = await internal.execute_lease_inproc(lease_id)

    assert outcome["outcome"] == "completed"
    assert captured["runtime_metadata"] == {
        "proposal_external_authorization": authorization,
    }
    assert captured["runtime_metadata"]["proposal_external_authorization"] is not authorization


@pytest.mark.asyncio
async def test_build_agent_context_adds_worker_runtime_metadata_to_envelope(monkeypatch):
    authorization = _authorization(task_id="task", workspace_id="workspace")
    captured: dict = {}

    async def fake_resolve_model(*_args, **_kwargs):
        return "openai/gpt-5.5"

    async def fake_resolve_metadata(*_args, **_kwargs):
        return None

    async def fake_resolve_workspace_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            workspace_id="workspace",
            task_id="task",
            thread_ref_kind="task",
            thread_ref_id="task",
            tool_profile="workspace",
            bound_tool_names=(),
            is_master=True,
            mcp_allowed_names=(),
            extra_context="",
        )

    async def fake_assemble(_db, *, request, **_kwargs):
        captured["request"] = request
        envelope = RuntimeEnvelope(
            surface=ChatSurface.SCHEDULED_AGENT_RUN,
            principal=RuntimePrincipal(kind=RuntimePrincipalKind.SYSTEM_WORKER),
            profile=RuntimeProfile.BACKGROUND_WORKER,
            entity_id="entity",
            workspace_id="workspace",
            task_id="task",
            metadata=dict(request.metadata),
        )
        return SimpleNamespace(
            context=SimpleNamespace(agent=None),
            tool_schemas=[],
            tool_names=(),
            allowed_tool_names=(),
            envelope=envelope,
            prompt="system prompt",
        )

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(
        "packages.core.ai.context.runtime_assemble_prompt_for_turn",
        fake_assemble,
    )

    ctx = await build_agent_context(
        SimpleNamespace(),
        entity_id="entity",
        workspace_id="workspace",
        task_id="task",
        runtime_metadata={"proposal_external_authorization": authorization},
    )

    assert captured["request"].metadata["proposal_external_authorization"] == authorization
    assert ctx.runtime_envelope.metadata["proposal_external_authorization"] == authorization


def test_exec_subagent_forwards_snapshot_runtime_metadata_to_context_builder(monkeypatch):
    authorization = _authorization(task_id="task", workspace_id="workspace")
    authorization.update({
        "started_at": "2026-08-11T11:55:38Z",
        "execution_count": 1,
        "upload_entry_provider_approval_id": "chrome-upload-entry",
    })
    context_kwargs: dict = {}
    captured_envelope: list[RuntimeEnvelope | None] = []
    captured_user_messages: list[str] = []

    async def fake_build_agent_context(*_args, **kwargs):
        context_kwargs.update(kwargs)
        envelope = RuntimeEnvelope(
            surface=ChatSurface.SCHEDULED_AGENT_RUN,
            principal=RuntimePrincipal(kind=RuntimePrincipalKind.SYSTEM_WORKER),
            profile=RuntimeProfile.BACKGROUND_WORKER,
            metadata=dict(kwargs.get("runtime_metadata") or {}),
        )
        return SimpleNamespace(
            system_prompt="sys",
            runtime_envelope=envelope,
            tools=[],
            tool_profile="workspace",
            allowed_tool_names=set(),
            model="openai/gpt-5.5",
            llm_metadata=None,
        )

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_exc):
            return False

    async def fake_worker_loop(**kwargs):
        captured_envelope.append(kwargs["runtime_envelope"])
        captured_user_messages.append(kwargs["user_message"])
        kwargs["dynamic_tool_handlers"]["submit_result"](
            {"status": "succeeded", "summary": "Published"}
        )
        result = SimpleNamespace(
            content="",
            messages=[],
            usage={},
            rounds=1,
            tool_calls_made=[],
            stop_reason="submitted",
        )
        return SimpleNamespace(result=result, run=None)

    async def fake_persist(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(
        "packages.core.ai.context.build_agent_context",
        fake_build_agent_context,
    )
    monkeypatch.setattr("packages.core.database.async_session", lambda: FakeSession())
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist)
    monkeypatch.setattr(internal, "runtime_metadata_from_context", lambda _ctx: {})

    asyncio.run(
        internal._exec_subagent(
            {
                "params": {"prompt": "Publish one video"},
                "entity_id": "entity",
                "resolved_agent_id": "agent",
                "user_id": "user",
                "workspace_id": "workspace",
                "conversation_id": "conversation",
                "task_id": "task",
                "expected_output_schema": None,
                "runtime_metadata": {
                    "proposal_external_authorization": authorization,
                },
            }
        )
    )

    assert context_kwargs["runtime_metadata"] == {
        "proposal_external_authorization": authorization,
    }
    assert captured_envelope[0].metadata["proposal_external_authorization"] == authorization
    assert "PROPOSAL AUTHORIZATION RECOVERY STATE" not in captured_user_messages[0]
    assert (
        "execution_count=1 records the already-executed Upload videos entry"
        not in captured_user_messages[0]
    )
