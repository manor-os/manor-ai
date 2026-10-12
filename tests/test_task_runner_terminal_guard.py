"""The first committed terminal Task result is authoritative.

When an Agent worker finishes, a concurrent cancel/failure/recovery path may
already have terminalized the Task.  The losing worker must neither overwrite
that row nor project its locally computed success through events, comments, or
its return payload.  This also applies when both writers chose ``completed``:
status equality does not make stale output safe to publish.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import packages.core.ai.task_runner as task_runner
from packages.core.ai.engine import ChatMessage, LLMConfig
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.services.auth_service import hash_password
from packages.core.services.task_state_machine import TaskStatusTransitionError
from packages.core.services.task_service import update_task


def test_finalize_returns_authoritative_terminal_without_overwriting(monkeypatch):
    calls: list[dict] = []
    authoritative = SimpleNamespace(
        status="cancelled",
        actual_output={"response": "cancelled by user"},
        details={"terminal_owner": "user"},
    )

    async def fake_update_task(db, task_id, entity_id, **fields):
        calls.append(fields)
        assert fields.pop("require_nonterminal") is True
        raise TaskStatusTransitionError("cancelled", "completed", "boom")

    async def fake_get_task(db, task_id, entity_id):
        return authoritative

    monkeypatch.setattr(task_runner, "update_task", fake_update_task)
    monkeypatch.setattr(
        "packages.core.services.task_service.get_task",
        fake_get_task,
    )

    persisted, committed_by_runner = asyncio.run(
        task_runner._finalize_with_terminal_guard(
            None,
            "t1",
            "e1",
            status="completed",
            actual_output="out",
            details={"a": 1},
        )
    )

    assert persisted is authoritative
    assert committed_by_runner is False
    assert len(calls) == 1
    assert calls[0].get("status") == "completed"
    assert calls[0].get("actual_output") == "out"
    assert calls[0].get("details") == {"a": 1}


def test_finalize_same_terminal_status_still_preserves_first_output(monkeypatch):
    authoritative = SimpleNamespace(
        status="completed",
        actual_output={"response": "first committed result"},
        details={"terminal_owner": "first worker"},
    )

    async def fake_update_task(db, task_id, entity_id, **fields):
        assert fields["status"] == "completed"
        assert fields["require_nonterminal"] is True
        raise TaskStatusTransitionError("completed", "completed", "lost terminal CAS")

    async def fake_get_task(db, task_id, entity_id):
        return authoritative

    monkeypatch.setattr(task_runner, "update_task", fake_update_task)
    monkeypatch.setattr(
        "packages.core.services.task_service.get_task",
        fake_get_task,
    )

    persisted, committed_by_runner = asyncio.run(
        task_runner._finalize_with_terminal_guard(
            None,
            "t1",
            "e1",
            status="completed",
            actual_output={"response": "stale second result"},
            details={"terminal_owner": "second worker"},
        )
    )

    assert persisted is authoritative
    assert committed_by_runner is False
    assert persisted.actual_output == {"response": "first committed result"}
    assert persisted.details == {"terminal_owner": "first worker"}


def test_finalize_passthrough_on_normal_success(monkeypatch):
    async def fake_update_task(db, task_id, entity_id, **fields):
        assert fields.pop("require_nonterminal") is True
        return "OK"

    monkeypatch.setattr(task_runner, "update_task", fake_update_task)

    persisted, committed_by_runner = asyncio.run(
        task_runner._finalize_with_terminal_guard(
            None,
            "t1",
            "e1",
            status="completed",
            actual_output="out",
            details={},
        )
    )
    assert persisted == "OK"
    assert committed_by_runner is True


@pytest.mark.asyncio
async def test_update_task_terminal_cas_rejects_same_status_output_overwrite(
    client,
    db_session,
):
    task_id = generate_ulid()
    entity_id = generate_ulid()
    authoritative_output = {"response": "first committed result", "attempt": 1}
    authoritative_details = {
        "terminal_owner": "first worker",
        "ai_result": {"response": "first committed result"},
    }
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Already completed Task",
            status="completed",
            priority=3,
            task_type="general",
            actual_output=authoritative_output,
            details=authoritative_details,
        )
    )
    await db_session.commit()

    with pytest.raises(TaskStatusTransitionError):
        await update_task(
            db_session,
            task_id,
            entity_id,
            status="completed",
            actual_output={"response": "stale second result", "attempt": 2},
            details={
                "terminal_owner": "second worker",
                "ai_result": {"response": "stale second result"},
            },
            require_nonterminal=True,
        )

    await db_session.rollback()
    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    assert persisted.status == "completed"
    assert persisted.actual_output == authoritative_output
    assert persisted.details == authoritative_details


class _SuccessfulTaskEngine:
    def __init__(self):
        self.config = LLMConfig(model="test-model")

    async def chat(self, messages, **kwargs):
        if "task supervisor" in str(kwargs.get("system_prompt") or "").lower():
            return ChatMessage(
                role="assistant",
                content='{"verdict":"done","summary":"ok","reason":"done"}',
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )
        return ChatMessage(
            role="assistant",
            content="STALE WORKER SUCCESS",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authoritative_status", "authoritative_response"),
    [
        ("cancelled", "cancelled by user"),
        ("failed", "failed by recovery owner"),
        ("completed", "first worker result"),
    ],
)
async def test_losing_runner_returns_authoritative_result_without_stale_projection(
    client,
    db_session,
    monkeypatch,
    authoritative_status,
    authoritative_response,
):
    import packages.core.ai.llm_client as llm_client
    import packages.core.database as db_module
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    entity_id = generate_ulid()
    task_id = generate_ulid()
    owner_user_id = generate_ulid()
    db_session.add_all(
        [
            User(
                id=owner_user_id,
                entity_id=entity_id,
                email=f"terminal-cas-{task_id.lower()}@test.local",
                display_name="Terminal CAS Owner",
                password_hash=hash_password("pass123"),
                role="owner",
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                agent_id=MANOR_AGENT_ID,
                title="Race terminal Task",
                description="The second worker must lose the terminal CAS.",
                status="pending",
                priority=3,
                task_type="general",
                details={"done_when": "One authoritative result exists."},
                owner_service_key="content",
                delegate_service_keys=[],
                owner_id=owner_user_id,
                creator_id=owner_user_id,
            ),
        ]
    )
    await db_session.commit()

    monkeypatch.setattr(
        llm_client,
        "llm_billing_context",
        lambda *args, **kwargs: _NoopBillingContext(),
    )

    async def fake_runtime(*_args, **_kwargs):
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id=None,
            task_id=task_id,
            is_master=False,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_finalize(db, requested_task_id, requested_entity_id, **_fields):
        assert requested_task_id == task_id
        assert requested_entity_id == entity_id
        persisted = await db.get(Task, task_id)
        persisted.status = authoritative_status
        persisted.actual_output = {
            "response": authoritative_response,
            "verdict": "done" if authoritative_status == "completed" else "failed",
            "error_type": (
                None
                if authoritative_status == "completed"
                else "AuthoritativeFailure"
            ),
            "error_message": (
                None if authoritative_status == "completed" else authoritative_response
            ),
        }
        persisted.details = {
            "terminal_owner": "authoritative writer",
            "ai_result": {"response": authoritative_response},
        }
        await db.flush()
        return persisted, False

    emitted_events: list[tuple[str, dict]] = []
    evidence_calls: list[dict] = []
    log_calls: list[tuple[str, str]] = []

    async def fake_record_evidence(*_args, **kwargs):
        evidence_calls.append(kwargs)

    async def fake_noop(*_args, **_kwargs):
        return None

    async def fake_log(_runner, _task_id, log_type, content):
        log_calls.append((log_type, content))

    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_runtime)
    monkeypatch.setattr(task_runner, "_finalize_with_terminal_guard", fake_finalize)
    monkeypatch.setattr(
        task_runner,
        "runtime_record_task_runner_execution_evidence",
        fake_record_evidence,
    )
    monkeypatch.setattr(
        task_runner,
        "runtime_persist_task_runner_runtime_events",
        fake_noop,
    )
    monkeypatch.setattr(
        task_runner,
        "runtime_emit_task_runner_status_event",
        lambda _entity_id, event_type, *, payload: emitted_events.append(
            (event_type, payload)
        ),
    )
    monkeypatch.setattr(task_runner.TaskRunner, "_log", fake_log)

    result = await task_runner.TaskRunner(
        engine=_SuccessfulTaskEngine(),
        session_factory=db_module.async_session,
    ).run(task_id)

    assert result["status"] == authoritative_status
    assert result["response"] == authoritative_response
    assert "STALE WORKER SUCCESS" not in str(result)
    assert evidence_calls == []
    assert emitted_events == []
    assert all("STALE WORKER SUCCESS" not in content for _, content in log_calls)
    assert all(log_type != "ai_execution_completed" for log_type, _ in log_calls)

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    assert persisted.status == authoritative_status
    assert persisted.actual_output["response"] == authoritative_response
    assert persisted.details == {
        "terminal_owner": "authoritative writer",
        "ai_result": {"response": authoritative_response},
    }
