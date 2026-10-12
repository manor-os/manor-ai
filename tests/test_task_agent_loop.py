"""Task agent loop (Phase A1): one durable AgentLoop owns a Workspace Task.

These tests run the real AgentLoop, the real wake/claim/checkpoint storage and
the real Task state machine against the test database. Only the model round,
the Workspace/factory admission lookups and the agent context are faked.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth_helpers import register_user_and_get_token

import packages.core.ai.agentic_loop as loop_module
from packages.core.constants.task import TaskRuntimeKind
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.services import feature_flags
from packages.core.services import task_requester_identity
from packages.core.services.task_coordinator_wakeup import (
    ensure_task_coordinator_run,
    stage_task_coordinator_wake,
    task_coordinator_outbox_dedupe_key,
)
from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
)
from packages.core.workers import task_agent_loop as worker
from packages.core.workers import task_run_host


NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_note",
        "description": "Record a note.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}
DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_to_agent",
        "description": "Hand tasks to other agents.",
        "parameters": {"type": "object", "properties": {"tasks": {"type": "array"}}},
    },
}
PLAN_ARGS = {
    "items": [
        {"title": "Draft the note", "status": "in_progress", "owner": "self"},
        {"title": "Verify the note", "status": "pending"},
    ],
    "acceptance_criteria": ["The note names the launch date."],
}
COMPLETE_ARGS = {
    "outcome": "completed",
    "summary": "Recorded the launch note.",
    "deliverables": [],
    "acceptance": [{
        "criterion": "The note names the launch date.",
        "met": True,
        "evidence": "record_note returned the note with 2026-10-10.",
    }],
}


def _selection() -> CoordinatorRuntimeSelection:
    root = TaskPlanServiceBinding(
        subscription_id=generate_ulid(), service_key="owner", agent_id=generate_ulid(),
    )
    return CoordinatorRuntimeSelection(root=root, bindings=(root,))


async def _task(db) -> Task:
    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        title="Write the launch note",
        description="Record a short note that names the launch date.",
        status="in_progress",
        task_type="general",
        details={},
    )
    db.add(task)
    await db.flush()
    return task


def _flag(monkeypatch, enabled: bool, calls: list | None = None) -> None:
    async def is_enabled(_db, key, *, entity_id=None, user_id=None, fallback=False):
        if calls is not None:
            calls.append((key, entity_id, user_id))
        return enabled

    monkeypatch.setattr(feature_flags, "is_enabled", is_enabled)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value="user-1"),
    )


async def _agent_loop_run(db_session, monkeypatch) -> tuple[Task, TaskCoordinatorRun, CoordinatorRuntimeSelection, str, str]:
    _flag(monkeypatch, True)
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    assert run.engine == TaskCoordinatorEngine.AGENT_LOOP.value
    return task, run, selection, wake.event_id, wake.wake_generation


async def _event(db, task_id: str) -> RuntimeOutboxEvent:
    return (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task_id)
        ).execution_options(populate_existing=True)
    )).scalar_one()


async def _fresh(db, model, row_id):
    return (await db.execute(
        select(model).where(model.id == row_id).execution_options(populate_existing=True)
    )).scalar_one()


def _install_host(monkeypatch, db_session, selection, *, conversation_hook=None):
    """Real storage and loop; fake Workspace lookups, factory, context and model."""

    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(worker, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(
        task_run_host, "lock_workspace_access_boundary",
        AsyncMock(return_value=SimpleNamespace(status="active")),
    )
    monkeypatch.setattr(worker, "lock_workspace_access_boundary", AsyncMock())
    monkeypatch.setattr(task_run_host, "workspace_allows_manual_work", lambda _workspace: True)
    monkeypatch.setattr(task_run_host, "resolve_task_execution", AsyncMock(return_value=SimpleNamespace(
        runtime_kind=TaskRuntimeKind.COORDINATED_PLAN, coordinator=selection,
    )))

    async def never_reevaluate(*_args, **_kwargs):
        raise AssertionError("the engine flag is never re-read at claim time")

    monkeypatch.setattr(feature_flags, "is_enabled", never_reevaluate)
    monkeypatch.setattr(worker, "resolve_task_execution_user_id", AsyncMock(return_value="user-1"))
    conversation_id = generate_ulid()

    async def ensure_conversation(_db, **kwargs):
        if conversation_hook is not None:
            await conversation_hook()
        return SimpleNamespace(id=conversation_id)

    from packages.core.services import workspace_runtime

    monkeypatch.setattr(workspace_runtime, "ensure_workspace_task_conversation", ensure_conversation)
    context = AsyncMock(return_value=SimpleNamespace(
        tools=[NOTE_TOOL],
        allowed_tool_names={"record_note"},
        runtime_envelope=None,
        system_prompt="AGENT PROMPT",
        tool_profile=None,
        model=None,
        llm_metadata={},
    ))
    monkeypatch.setattr(worker, "build_agent_context", context)
    monkeypatch.setattr(worker, "runtime_tool_schemas_for_names", lambda names: [DELEGATE_SCHEMA])

    async def specialize(_db, tools, **_kwargs):
        return tools

    monkeypatch.setattr(worker, "runtime_specialize_delegate_to_agent", specialize)
    loop_calls: list[dict] = []
    tool_runs: list[tuple[str, dict]] = []
    extra_tools: dict = {}

    async def real_loop(**kwargs):
        assert "tool_executor" not in kwargs, "the registry runs the real tools"
        loop_calls.append(kwargs)
        handlers = kwargs["dynamic_tool_handlers"]

        async def executor(name, args):
            tool_runs.append((name, args))
            if name in handlers:
                return await handlers[name](args)
            if name == "record_note":
                return json.dumps({"ok": True, "note": args.get("text")})
            if name in extra_tools:
                return await extra_tools[name](args)
            raise AssertionError(f"unexpected tool {name}")

        keys = (
            "system_prompt", "user_message", "tools", "max_rounds",
            "on_round_end_strict", "on_llm_call_before", "runtime_cancel_checker",
            "terminal_tool_result_policy", "initial_messages", "initial_usage",
            "initial_rounds", "initial_tool_calls_made", "on_tool_end",
            "human_input_suspension", "hitl_resume_checkpoint",
        )
        resume = {"resume_checkpoint": kwargs["runtime_checkpoint"]} if kwargs.get("runtime_checkpoint") else {}
        # The harness restores the checkpointed ledger into the host's live one.
        evidence = kwargs.get("tool_evidence")
        assert evidence is not None, "the loop runs on the host's live ledger"
        initial_tool_evidence = kwargs.get("initial_tool_evidence")
        run_state = kwargs.get("run_state")
        if run_state is not None:
            # As the harness does: a resumed run's state replaces initial_*.
            assert not any(key in kwargs for key in ("initial_messages", "initial_tool_evidence"))
            resume.update(run_state.resume_loop_kwargs())
            initial_tool_evidence = run_state.tool_evidence
        if initial_tool_evidence:
            evidence.restore_checkpoint_state(initial_tool_evidence)
        from packages.core.ai.runtime.harness import runtime_run_state_round_observer

        # As the harness does: each round hands over the run state snapshot
        # with the host's registered slots captured.
        resume["on_round_end"] = runtime_run_state_round_observer(
            kwargs["on_round_end"], run_state_slots=kwargs.get("run_state_slots"),
        )
        from packages.core.ai.runtime.execution_nesting import runtime_nested_execution_scope

        # As the harness does: every agent loop is one nesting level.
        with runtime_nested_execution_scope():
            return await loop_module.agentic_loop(
                tool_executor=executor, tool_evidence=evidence, **resume,
                # As the harness does: the Task id is the performance run, so
                # every call carries the runtime's own call id.
                performance_run_id=kwargs.get("task_id"),
                **{key: kwargs[key] for key in keys if key in kwargs},
            )

    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", real_loop)
    return SimpleNamespace(
        context=context, loop_calls=loop_calls, tool_runs=tool_runs, extra_tools=extra_tools,
        conversation_id=conversation_id, session_factory=session_factory,
    )


def _model(monkeypatch, script):
    """Each call returns the next scripted (content, tool_calls)."""

    seen: list[list[dict]] = []

    async def model(messages, tools, **_kwargs):
        seen.append([dict(message) for message in messages])
        step = script[min(len(seen), len(script)) - 1]
        outcome = step(messages, tools) if callable(step) else step
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        content, calls = outcome
        return content, calls, {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    return seen


def _resumed(loop_call: dict) -> dict:
    """The initial_* arguments the harness derives from a resumed run state."""

    return loop_call["run_state"].resume_loop_kwargs()


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


# ── (a) the flag freezes the engine at run creation ────────────────────


@pytest.mark.asyncio
async def test_flag_decides_engine_once_when_the_run_is_created(db_session, monkeypatch):
    calls: list = []
    _flag(monkeypatch, True, calls)
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    assert run.engine == TaskCoordinatorEngine.AGENT_LOOP.value
    assert calls == [("task_agent_loop", task.entity_id, "user-1")]

    # Flipping the flag never moves an existing run, and is not even read.
    _flag(monkeypatch, False, calls)
    again = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    assert again.id == run.id and again.engine == TaskCoordinatorEngine.AGENT_LOOP.value
    # A retried lifecycle reopens the same row with the same engine.
    run.phase = TaskCoordinatorPhase.FAILED.value
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await db_session.flush()
    retried = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    assert retried.phase == TaskCoordinatorPhase.READY.value
    assert retried.engine == TaskCoordinatorEngine.AGENT_LOOP.value
    assert len(calls) == 1

    other = await _task(db_session)
    dag = await ensure_task_coordinator_run(db_session, task=other, selection=_selection())
    assert dag.engine == TaskCoordinatorEngine.PLAN_DAG.value
    assert calls[-1] == ("task_agent_loop", other.entity_id, "user-1")
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", ["none", "unresolvable"])
async def test_userless_task_stays_on_plan_dag_with_the_flag_on(db_session, monkeypatch, requester):
    """Strategist/scheduler Tasks have no executing member; the loop needs one."""

    calls: list = []
    _flag(monkeypatch, True, calls)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=None) if requester == "none" else AsyncMock(
            side_effect=task_requester_identity.TaskRequesterIdentityError("t", "none"),
        ),
    )
    task = await _task(db_session)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    assert run.engine == TaskCoordinatorEngine.PLAN_DAG.value
    assert calls == [("task_agent_loop", task.entity_id, None)]
    await db_session.rollback()


# ── (b) the consumer routes on the frozen engine ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_consumer_dispatches_by_run_engine(db_session, monkeypatch, enabled):
    from packages.core import database
    from packages.core.tasks import runtime_tasks

    _flag(monkeypatch, enabled)
    task = await _task(db_session)
    await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    sent: dict[str, list] = {"agent_loop": [], "plan_dag": []}
    monkeypatch.setattr(
        runtime_tasks.run_task_agent_loop_task, "apply_async",
        lambda **kwargs: sent["agent_loop"].append(kwargs),
    )
    monkeypatch.setattr(
        runtime_tasks.run_task_coordinator_task, "apply_async",
        lambda **kwargs: sent["plan_dag"].append(kwargs),
    )

    outcome = await runtime_tasks.consume_task_coordinator_intent_once(
        event_id=wake.event_id, task_id=task.id,
        wake_generation=wake.wake_generation, reason="task_start",
    )

    assert outcome == {"status": "dispatched", "task_id": task.id}
    expected = {"kwargs": {
        "event_id": wake.event_id, "task_id": task.id,
        "wake_generation": wake.wake_generation, "reason": "task_start",
    }}
    routed, other = ("agent_loop", "plan_dag") if enabled else ("plan_dag", "agent_loop")
    assert sent[routed] == [expected]
    assert sent[other] == []
    # Same handoff protocol: the wake stays pending until the worker ACKs it.
    assert (await _event(db_session, task.id)).delivered_at is None


# ── (c) happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_work_and_complete_task_finishes_the_task(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    def finish(messages, _tools):
        note = json.loads(next(m["content"] for m in messages if m.get("tool_call_id") == "note"))
        assert note == {"ok": True, "note": "Launch on 2026-10-10"}
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("note", "record_note", {"text": "Launch on 2026-10-10"})]),
        finish,
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome == {"status": "completed", "task_id": task.id}
    assert len(seen) == 3
    task = await _fresh(db_session, Task, task.id)
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert task.status == "completed"
    assert task.conversation_id == host.conversation_id
    result = {
        "summary": COMPLETE_ARGS["summary"],
        "deliverables": COMPLETE_ARGS["deliverables"],
        "acceptance": COMPLETE_ARGS["acceptance"],
    }
    assert task.actual_output == {
        **result, "response": COMPLETE_ARGS["summary"], "result": result, "engine": "agent_loop",
        # A2: the host's outcome decision, the receipt ledger and verified files.
        "outcome": {
            "kind": "outcome", "outcome": "completed", "effect_state": "confirmed",
            "retryable": False, "do_not_replay": False, "downgraded_success": False,
        },
        "receipts": [],
        "files": [],
        # Confirmed non-file side effects (record_note is not classified).
        "effects": [],
    }
    # Scheduled occurrences settle with the loop's summary as their response.
    from packages.core.tasks.ai_tasks import _task_terminal_result

    settled = _task_terminal_result(task)
    assert settled["status"] == "completed"
    assert settled["response"] == COMPLETE_ARGS["summary"]
    assert run.phase == TaskCoordinatorPhase.COMPLETED.value
    assert run.claim_token is None and run.wake_generation is None
    assert run.checkpoint["task_plan"]["acceptance_criteria"] == PLAN_ARGS["acceptance_criteria"]
    assert run.checkpoint["task_plan"]["revision"] == 1
    assert run.checkpoint["task_submission"]["outcome"] == "completed"
    event = await _event(db_session, task.id)
    assert event.delivered_at is not None
    logs = (await db_session.execute(
        select(TaskLog.log_type).where(TaskLog.task_id == task.id)
    )).scalars().all()
    assert "ai_agent_turn" in logs and "ai_execution_completed" in logs

    # Context: the plan-step worker surface, the Task brief as the query.
    context_kwargs = host.context.await_args.kwargs
    assert context_kwargs["runtime_surface"].value == "scheduled_agent_run"
    assert context_kwargs["user_id"] == "user-1"
    assert context_kwargs["agent_id"] == selection.root.agent_id
    assert context_kwargs["agent_subscription_id"] == selection.root.subscription_id
    assert context_kwargs["conversation_id"] == host.conversation_id
    assert "Write the launch note" in context_kwargs["active_user_message"]
    loop = host.loop_calls[0]
    assert [tool["function"]["name"] for tool in loop["tools"]] == [
        "record_note", "request_input", "delegate_to_agent", "wait_until",
        "update_task_plan", "complete_task",
    ]
    assert set(loop["allowed_tool_names"]) == {
        "record_note", "request_input", "delegate_to_agent", "wait_until",
        "update_task_plan", "complete_task",
    }
    # Phase B: a question parks the loop on the person's answer.
    assert loop["human_input_suspension"] is True
    assert "call request_input with one precise question" in loop["system_prompt"]
    assert loop["user_id"] == "user-1"
    assert loop["conversation_id"] == host.conversation_id
    assert loop["system_prompt"].startswith("AGENT PROMPT")
    assert "complete_task" in loop["system_prompt"]
    assert "delegate_to_agent" in loop["system_prompt"]
    assert set(loop["dynamic_tool_handlers"]) == {
        "wait_until", "wait_media_jobs", "update_task_plan", "complete_task",
    }


@pytest.mark.asyncio
async def test_invalid_completion_is_returned_to_the_loop(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)

    def corrected(messages, _tools):
        error = json.loads(messages[-1]["content"])
        assert error["error"] == "invalid_arguments"
        assert "blocked_reason" in error["message"]
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [_call("bad", "complete_task", {**COMPLETE_ARGS, "outcome": "blocked"})]),
        corrected,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["status"] == "completed" and len(seen) == 2


@pytest.mark.asyncio
async def test_oversize_submission_is_truncated_everywhere_it_is_copied(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    # Capping, not deliverable verification, is under test here.
    monkeypatch.setattr(worker, "resolve_deliverables", AsyncMock(return_value=([], [])))
    huge = {
        "outcome": "completed",
        "summary": "s" * 9000,
        "deliverables": [{"title": "t" * 600, "reference": "r" * 600} for _ in range(60)],
        "acceptance": [
            {"criterion": "c" * 600, "met": True, "evidence": "e" * 1200} for _ in range(60)
        ],
    }
    _model(monkeypatch, [("", [_call("done", "complete_task", huge)])])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "completed"

    task = await _fresh(db_session, Task, task.id)
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    log = (await db_session.execute(
        select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.log_type == "ai_execution_completed")
    )).scalar_one()
    output = task.actual_output
    for copy_ in (output, output["result"], run.checkpoint["task_submission"]):
        assert len(copy_["summary"]) == 8000
        assert len(copy_["deliverables"]) == 50
        assert {len(item["title"]) for item in copy_["deliverables"]} == {500}
        assert {len(item["reference"]) for item in copy_["deliverables"]} == {500}
        assert len(copy_["acceptance"]) == 50
        assert {len(item["criterion"]) for item in copy_["acceptance"]} == {500}
        assert {len(item["evidence"]) for item in copy_["acceptance"]} == {1000}
    assert len(output["response"]) == 8000
    assert len(log.content) == 8000
    meta = log.meta
    assert len(meta["deliverables"]) == 50 and len(meta["acceptance"]) == 50
    assert {len(item["evidence"]) for item in meta["acceptance"]} == {1000}


@pytest.mark.asyncio
async def test_checkpointed_submission_finishes_without_another_model_call(db_session, monkeypatch):
    """A crash after the submission round but before the terminal write."""

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    run.checkpoint = {
        "messages": [{"role": "user", "content": "brief"}], "rounds": 3,
        "task_submission": {
            "outcome": "completed", "summary": "Done.", "deliverables": [], "acceptance": [],
        },
    }
    await db_session.commit()
    _install_host(monkeypatch, db_session, selection)
    seen = _model(monkeypatch, [("must not run", [])])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "completed", "task_id": task.id}
    assert seen == []
    assert (await _fresh(db_session, Task, task.id)).actual_output["summary"] == "Done."


# ── (d) no complete_task: bounded continuation, then BLOCKED ────────────


@pytest.mark.asyncio
async def test_missing_completion_continues_then_blocks(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    seen = _model(monkeypatch, [("I think the note is done.", [])])

    outcomes = []
    for attempt in range(worker._NON_SUBMIT_RETRIES + 1):
        outcomes.append(await worker.run_task_agent_loop_once(
            event_id=event_id, task_id=task.id, wake_generation=generation,
        ))
        run = await _fresh(db_session, TaskCoordinatorRun, run.id)
        if attempt < worker._NON_SUBMIT_RETRIES:
            assert run.checkpoint["non_submit_retries"] == attempt + 1
            assert run.claim_token is None
            event = await _event(db_session, task.id)
            assert event.delivered_at is None
            assert event.payload["reason"] == "task_agent_loop_completion_missing"
            assert event.available_at > datetime.now(timezone.utc)
            assert event.payload["wake_generation"] == run.wake_generation != generation
            event_id, generation = event.id, run.wake_generation

    assert [item["status"] for item in outcomes] == [
        "retry_scheduled", "retry_scheduled", "blocked",
    ]
    assert len(seen) == worker._NON_SUBMIT_RETRIES + 1
    # Each continuation resumed the same transcript with the host reminder.
    assert host.loop_calls[1]["user_message"] == worker._CONTINUE_MESSAGE
    assert _resumed(host.loop_calls[1])["initial_messages"][0]["role"] == "user"
    assert all(m["role"] != "system" for m in _resumed(host.loop_calls[1])["initial_messages"])
    task = await _fresh(db_session, Task, task.id)
    assert task.status == "blocked"
    assert run.phase == TaskCoordinatorPhase.FAILED.value
    outcome = run.checkpoint["coordinator_outcome"]
    assert outcome["error"] == "agent_loop_completion_missing"
    assert outcome["details"]["continuation_attempts"] == worker._NON_SUBMIT_RETRIES
    assert (await _event(db_session, task.id)).delivered_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [True, False])
async def test_request_input_is_offered_and_parks_the_loop(db_session, monkeypatch, bound):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    if bound:
        request_input = {"type": "function", "function": {"name": "request_input", "parameters": {"type": "object"}}}
        host.context.return_value.tools = [NOTE_TOOL, request_input]
        host.context.return_value.allowed_tool_names = {"record_note", "request_input"}
    _model(monkeypatch, [("", [_call("done", "complete_task", COMPLETE_ARGS)])])
    await worker.run_task_agent_loop_once(event_id=event_id, task_id=task.id, wake_generation=generation)
    loop = host.loop_calls[0]
    # Asking is always allowed, exactly once, whether or not the Agent's
    # binding listed it.
    assert [tool["function"]["name"] for tool in loop["tools"]].count("request_input") == 1
    assert "request_input" in loop["allowed_tool_names"]
    assert loop["human_input_suspension"] is True
    assert loop["hitl_resume_checkpoint"] is True
    assert "call request_input with one precise question" in loop["system_prompt"]
    assert "You cannot ask the user questions" not in loop["system_prompt"]


def _approval_loop(hitl_id: str):
    envelope = json.dumps({
        "__hitl__": True, "error": "approval_required", "approval_token": hitl_id,
        "hitl": {"id": hitl_id, "type": "approval", "prompt": "Publish the launch post to TikTok?",
                 "action": "external.social.publish", "tool": "tiktok_publish"},
    })

    async def loop(**kwargs):
        return loop_module.AgenticResult(
            content="", messages=[{"role": "user", "content": "brief"}], usage={}, rounds=1,
            stop_reason="hitl_required",
            control={"kind": "approval_required", "tool_name": "tiktok_publish", "blocking_result": envelope},
        )

    return loop


@pytest.mark.asyncio
@pytest.mark.parametrize("card", ["pending", "missing"])
async def test_unresumable_approval_blocks_and_withdraws_its_card(db_session, monkeypatch, card):
    """An approval stop without its exact pre-call continuation cannot park."""

    from packages.core.models.hitl_request import HitlRequest

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    hitl_id = generate_ulid()
    if card == "pending":
        db_session.add(HitlRequest(
            id=hitl_id, entity_id=task.entity_id, workspace_id=task.workspace_id,
            action_key="external.social.publish", resource_kind="workspace", risk_level="high",
            origin_kind="tool_call", origin_task_id=task.id,
            origin_conversation_id=host.conversation_id, status="pending",
            dedup_key=f"tool:{generate_ulid()}",
        ))
        await db_session.commit()
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", _approval_loop(hitl_id))

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome["status"] == "blocked" and outcome["code"] == "agent_loop_approval_unresumable"
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert run.phase == TaskCoordinatorPhase.FAILED.value != TaskCoordinatorPhase.WAITING_CHILD.value
    failure = run.checkpoint["coordinator_outcome"]
    message = failure["message"]
    assert "`tiktok_publish` needs approval: Publish the launch post to TikTok?" in message
    assert "cannot be resumed at the exact call that asked for it" in message
    assert "standing Workspace rule that auto-approves" in message
    withdrawn = card == "pending"
    assert ("request was withdrawn" in message) is withdrawn
    assert failure["details"]["approval_withdrawn"] is withdrawn
    assert failure["details"]["approval"]["hitl_id"] == hitl_id
    log = (await db_session.execute(
        select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.log_type == "ai_execution_failed")
    )).scalar_one()
    assert log.content == message
    if withdrawn:
        # Approving it later cannot reach fail_task_runtime_approval_origin.
        hitl = await _fresh(db_session, HitlRequest, hitl_id)
        assert hitl.status == "expired" and hitl.resolved_reason == "agent_loop_approval_unresumable"
    assert (await _fresh(db_session, Task, task.id)).status == "blocked"


@pytest.mark.asyncio
async def test_resource_wait_parks_the_pending_call_and_resumes_without_a_model_call(db_session, monkeypatch):
    from packages.core.ai.runtime.control import RuntimeToolSuspension

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    sandbox_tool = {"type": "function", "function": {
        "name": "run_in_sandbox", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    }}
    host.context.return_value.tools = [NOTE_TOOL, sandbox_tool]
    host.context.return_value.allowed_tool_names = {"record_note", "run_in_sandbox"}
    monkeypatch.setattr(
        worker, "_withdraw_approval_card",
        AsyncMock(side_effect=AssertionError("a capacity wait is not an approval")),
    )
    model_calls_at_tool: list[int] = []
    attempts = 0

    async def run_in_sandbox(args):
        nonlocal attempts
        attempts += 1
        model_calls_at_tool.append(len(seen))
        if attempts == 1:
            return RuntimeToolSuspension(
                kind="waiting_resource", reservation_id="reservation-1",
                poll_after_seconds=7, deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        return json.dumps({"ok": True, "stdout": "built"})

    host.extra_tools["run_in_sandbox"] = run_in_sandbox

    def finish(messages, _tools):
        result = next(m["content"] for m in messages if m.get("tool_call_id") == "sandbox")
        assert json.loads(result) == {"ok": True, "stdout": "built"}
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [_call("sandbox", "run_in_sandbox", {"cmd": "make"})]),
        finish,
    ])
    before = datetime.now(timezone.utc)

    first = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert first == {"status": "waiting_resource", "task_id": task.id}
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.claim_token is None and row.phase != TaskCoordinatorPhase.FAILED.value
    assert row.checkpoint["runtime_checkpoint"]["pending_tool_call"]["name"] == "run_in_sandbox"
    assert row.checkpoint["resource_wait"]["reservation_id"] == "reservation-1"
    assert row.checkpoint["resource_wait"]["poll_after_seconds"] == 7
    assert row.checkpoint["non_submit_retries"] == 0
    assert "wake_failures" not in row.checkpoint
    event = await _event(db_session, task.id)
    assert event.payload["reason"] == "task_agent_loop_resource_wait"
    assert event.payload["wake_generation"] == row.wake_generation != generation
    assert before + timedelta(seconds=6) < event.available_at < before + timedelta(seconds=30)
    assert (await _fresh(db_session, Task, task.id)).status == "in_progress"

    second = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=row.wake_generation,
    )

    assert second == {"status": "completed", "task_id": task.id}
    assert host.loop_calls[-1]["runtime_checkpoint"]["pending_tool_call"]["name"] == "run_in_sandbox"
    assert "initial_messages" not in host.loop_calls[-1] and "run_state" not in host.loop_calls[-1]
    # The parked call ran again before any new model round.
    assert model_calls_at_tool == [1, 1]
    assert len(seen) == 2
    final = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint
    assert "runtime_checkpoint" not in final


@pytest.mark.parametrize(("value", "expected"), [(None, 30), (1, 5), (7, 7), (5000, 600), ("x", 30)])
def test_resource_poll_delay_is_clamped(value, expected):
    assert worker._resource_poll_delay(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "code"), [
    # A question without its payload cannot be shown to anyone.
    ("waiting_human", "agent_loop_suspension_unsupported"),
    ("capability_approval", "agent_loop_suspension_unsupported"),
    # An unknown wait, or a timer/job wait without its terms, fails closed.
    ("waiting_event", "agent_loop_suspension_unsupported"),
    ("waiting_timer", "agent_loop_suspension_unsupported"),
    ("waiting_job", "agent_loop_suspension_unsupported"),
])
async def test_other_suspensions_block_with_distinct_codes(db_session, monkeypatch, kind, code):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(
        worker, "_withdraw_approval_card",
        AsyncMock(side_effect=AssertionError("not an approval")),
    )

    async def suspended(**kwargs):
        return loop_module.AgenticResult(
            content="", messages=[{"role": "user", "content": "brief"}], usage={}, rounds=1,
            stop_reason="waiting_resource", control={"kind": kind, "checkpoint": {"messages": []}},
        )

    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", suspended)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": code}
    failure = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["coordinator_outcome"]
    assert "withdrawn" not in failure["message"]
    assert "needs approval" not in failure["message"]


@pytest.mark.asyncio
async def test_retry_continues_the_transcript_with_a_recheck_notice(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(worker, "_NON_SUBMIT_RETRIES", 0)
    _model(monkeypatch, [
        ("", [_call("note", "record_note", {"text": "Launch on 2026-10-10"})]),
        ("Done, I think.", []),
    ])
    blocked = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert blocked["code"] == "agent_loop_completion_missing"
    stopped = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint
    assert stopped["rounds"] == 2

    # Manual retry: the Task re-enters IN_PROGRESS under a new lifecycle.
    task = await _fresh(db_session, Task, task.id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.commit()
    assert run.phase == TaskCoordinatorPhase.READY.value
    assert run.checkpoint["messages"] == stopped["messages"]
    assert run.checkpoint["retried_after"]["after"] == "agent_loop_completion_missing"
    assert run.checkpoint["rounds"] == 0
    assert "coordinator_outcome" not in run.checkpoint
    event = await _event(db_session, task.id)

    _model(monkeypatch, [("", [_call("done", "complete_task", COMPLETE_ARGS)])])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=run.wake_generation,
    )

    assert outcome == {"status": "completed", "task_id": task.id}
    resumed = host.loop_calls[-1]
    assert _resumed(resumed)["initial_messages"] == [m for m in stopped["messages"] if m["role"] != "system"]
    assert resumed["user_message"].startswith("Task retried at ")
    assert "after agent_loop_completion_missing: re-check external state before continuing" in resumed["user_message"]
    final = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint
    assert "retried_after" not in final


# ── Honest stop codes ──────────────────────────────────────────────────


def _stopped_loop(stop_reason: str, *, rounds: int = 1, error: str | None = None):
    async def loop(**kwargs):
        return loop_module.AgenticResult(
            content="", messages=[{"role": "user", "content": "brief"}], usage={},
            rounds=rounds, stop_reason=stop_reason, error=error,
            error_detail={"provider": "x"} if error else None,
        )

    return loop


@pytest.mark.asyncio
@pytest.mark.parametrize(("stop_reason", "rounds", "code", "continues"), [
    ("credit_exhausted", 1, "agent_loop_credit_exhausted", False),
    ("max_rounds", 384, "agent_loop_round_budget_exhausted", False),
    ("error", 1, "agent_loop_error", True),
    ("context_budget", 1, "agent_loop_context_budget_exhausted", False),
    ("completed", 1, "agent_loop_completion_missing", True),
])
async def test_stop_reasons_block_with_honest_codes(
    db_session, monkeypatch, stop_reason, rounds, code, continues,
):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    error = {"error": "provider_failed", "context_budget": "context_budget_exceeded"}.get(stop_reason)
    if stop_reason == "context_budget":
        stop_reason = "error"
    monkeypatch.setattr(
        worker, "runtime_execute_agentic_loop",
        _stopped_loop(stop_reason, rounds=rounds, error=error),
    )
    first = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    if continues:
        assert first["status"] == "retry_scheduled"
        monkeypatch.setattr(worker, "_NON_SUBMIT_RETRIES", 1)
        row = await _fresh(db_session, TaskCoordinatorRun, run.id)
        first = await worker.run_task_agent_loop_once(
            event_id=(await _event(db_session, task.id)).id, task_id=task.id,
            wake_generation=row.wake_generation,
        )
    assert first == {"status": "blocked", "task_id": task.id, "code": code}
    failure = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["coordinator_outcome"]
    assert failure["error"] == code
    assert failure["details"]["stop_reason"] == stop_reason
    if stop_reason == "error":
        assert failure["details"]["runtime_error"] == error
        assert failure["details"]["error_detail"] == {"provider": "x"}


@pytest.mark.asyncio
async def test_flag_evaluation_failure_falls_back_to_plan_dag(db_session, monkeypatch):
    async def broken(db, *_args, **_kwargs):
        from sqlalchemy import text

        await db.execute(text("SELECT * FROM no_such_flag_table"))

    monkeypatch.setattr(feature_flags, "is_enabled", broken)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id", AsyncMock(return_value=None),
    )
    task = await _task(db_session)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    # The failed lookup was confined to its savepoint; admission continues.
    assert run.engine == TaskCoordinatorEngine.PLAN_DAG.value
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    assert wake is not None


def test_empty_envelope_allowlist_is_never_widened():
    import asyncio

    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    surface = ChatSurface.SCHEDULED_AGENT_RUN
    envelope = RuntimeEnvelope(
        surface=surface,
        principal=resolve_runtime_principal(surface=surface, entity_id="e", user_id="u", workspace_id="w"),
        profile=RuntimeProfile.BACKGROUND_WORKER,
    )
    ctx = SimpleNamespace(
        tools=[NOTE_TOOL, DELEGATE_SCHEMA], allowed_tool_names={"record_note", "delegate_to_agent"},
        runtime_envelope=envelope,
    )

    async def keep(_db, tools, **_kwargs):
        return tools

    original = worker.runtime_specialize_delegate_to_agent
    worker.runtime_specialize_delegate_to_agent = keep
    try:
        tools, allowed, loop_envelope, can_delegate = asyncio.run(worker._loop_tools(
            None, ctx, entity_id="e", workspace_id="w", agent_id="a",
        ))
    finally:
        worker.runtime_specialize_delegate_to_agent = original
    assert not can_delegate
    assert loop_envelope is envelope and loop_envelope.allowed_tool_names == ()
    assert "delegate_to_agent" not in [tool["function"]["name"] for tool in tools]
    assert "delegate_to_agent" not in allowed


# ── A crashing wake is bounded ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_crash_on_one_wake_blocks_for_repair(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    host.context.side_effect = RuntimeError("context exploded " + "x" * 2000)

    for attempt in (1, 2):
        with pytest.raises(RuntimeError, match="context exploded"):
            await worker.run_task_agent_loop_once(
                event_id=event_id, task_id=task.id, wake_generation=generation,
            )
        row = await _fresh(db_session, TaskCoordinatorRun, run.id)
        assert row.checkpoint["wake_failures"]["count"] == attempt
        assert row.checkpoint["wake_failures"]["wake_generation"] == generation
        assert row.claim_token is None
        # Not ACKed: the same wake is redelivered.
        assert (await _event(db_session, task.id)).delivered_at is None

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome["status"] == "blocked" and outcome["code"] == "agent_loop_repair_required"
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    failure = row.checkpoint["coordinator_outcome"]
    assert failure["details"]["exception"] == "RuntimeError"
    assert failure["details"]["error"].startswith("context exploded")
    assert len(failure["details"]["error"]) == 1000
    assert failure["details"]["attempts"] == 3
    assert "wake_failures" not in row.checkpoint
    assert row.phase == TaskCoordinatorPhase.FAILED.value and row.claim_token is None
    assert (await _event(db_session, task.id)).delivered_at is not None
    assert (await _fresh(db_session, Task, task.id)).status == "blocked"


@pytest.mark.asyncio
async def test_a_successful_round_resets_the_crash_count(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    real_context = host.context.return_value
    host.context.side_effect = [RuntimeError("transient"), real_context, real_context]
    with pytest.raises(RuntimeError, match="transient"):
        await worker.run_task_agent_loop_once(event_id=event_id, task_id=task.id, wake_generation=generation)
    assert (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["wake_failures"]["count"] == 1

    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)
    _model(monkeypatch, [("", [_call("note", "record_note", {"text": "x"})])])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "yielded"
    assert "wake_failures" not in (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint


# ── (e) time-slice yield and resume ─────────────────────────────────────


@pytest.mark.asyncio
async def test_time_slice_yields_restages_and_resumes_same_transcript(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)
    seen = _model(monkeypatch, [
        ("", [_call("note", "record_note", {"text": "Launch on 2026-10-10"})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
    ])

    first = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert first == {"status": "yielded", "task_id": task.id}
    assert len(seen) == 1
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert run.phase == TaskCoordinatorPhase.RUNNING.value
    assert run.claim_token is None
    # A yield is not a failed attempt.
    assert run.checkpoint["non_submit_retries"] == 0
    parked_messages = run.checkpoint["messages"]
    assert any(m.get("tool_call_id") == "note" for m in parked_messages)
    event = await _event(db_session, task.id)
    assert event.delivered_at is None
    assert event.payload["reason"] == "task_agent_loop_yield"
    assert event.payload["wake_generation"] == run.wake_generation != generation
    assert (await _fresh(db_session, Task, task.id)).status == "in_progress"

    # The old wake can no longer be claimed; the successor resumes.
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "deferred"
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 3600)
    second = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=run.wake_generation,
    )

    assert second == {"status": "completed", "task_id": task.id}
    resumed = host.loop_calls[-1]
    assert _resumed(resumed)["initial_messages"] == [m for m in parked_messages if m["role"] != "system"]
    assert _resumed(resumed)["initial_rounds"] == 1
    # The model saw the first slice's tool result without re-running it.
    assert any(m.get("tool_call_id") == "note" for m in seen[1])
    assert [name for name, _args in host.tool_runs] == ["record_note", "complete_task"]
    assert (await _fresh(db_session, Task, task.id)).status == "completed"


# ── (f) a lost claim aborts without writing ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_model", "during_round", "during_plan"])
async def test_stale_claim_aborts_without_writing(db_session, monkeypatch, when):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    async def steal_claim():
        async with session_factory() as db:
            row = await _fresh(db, TaskCoordinatorRun, run.id)
            row.claim_token = "successor"
            row.state_version += 1
            await db.commit()

    _install_host(
        monkeypatch, db_session, selection,
        conversation_hook=steal_claim if when == "before_model" else None,
    )

    async def note_then_steal(_messages, _tools):
        await steal_claim()
        if when == "during_plan":
            return "", [_call("plan", "update_task_plan", PLAN_ARGS)]
        return "", [_call("note", "record_note", {"text": "x"})]

    calls = 0

    async def model(messages, tools, **_kwargs):
        nonlocal calls
        calls += 1
        content, tool_calls = await note_then_steal(messages, tools)
        return content, tool_calls, {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome == {"status": "superseded", "task_id": task.id}
    assert calls == (0 if when == "before_model" else 1)
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert run.claim_token == "successor"
    assert run.checkpoint is None
    assert run.phase == TaskCoordinatorPhase.READY.value
    task = await _fresh(db_session, Task, task.id)
    assert task.status == "in_progress"
    # A superseded owner binds no conversation and writes no plan log.
    assert (task.conversation_id is None) == (when == "before_model")
    assert (await db_session.execute(
        select(TaskLog).where(TaskLog.task_id == task.id, TaskLog.log_type == "ai_agent_turn")
    )).scalars().all() == []
    assert (await _event(db_session, task.id)).delivered_at is None


# ── (g) engines never run each other's runs ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["plan_dag", "agent_loop"])
async def test_each_worker_admits_only_its_engine(db_session, monkeypatch, engine):
    from packages.core.workers import task_coordinator

    _flag(monkeypatch, engine == "agent_loop")
    task = await _task(db_session)
    selection = _selection()
    await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(task_coordinator, "create_worker_session", worker.create_worker_session)
    wrong = task_coordinator if engine == "agent_loop" else worker
    right = worker if engine == "agent_loop" else task_coordinator

    claim, reason = await wrong._admit_and_claim(
        event_id=wake.event_id, task_id=task.id, wake_generation=wake.wake_generation,
    )
    assert claim is None and reason == "engine_mismatch"
    claim, reason = await right._admit_and_claim(
        event_id=wake.event_id, task_id=task.id, wake_generation=wake.wake_generation,
    )
    assert reason == "claimed" and claim.task_id == task.id


# ── Loop tools and governance ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegation_is_added_to_this_loop_only(monkeypatch):
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    surface = ChatSurface.SCHEDULED_AGENT_RUN
    envelope = RuntimeEnvelope(
        surface=surface,
        principal=resolve_runtime_principal(
            surface=surface, entity_id="entity-1", user_id="user-1",
            agent_id="agent-1", workspace_id="workspace-1",
        ),
        profile=RuntimeProfile.BACKGROUND_WORKER,
        tool_names=("record_note",),
        allowed_tool_names=("record_note",),
    )
    ctx = SimpleNamespace(tools=[NOTE_TOOL], allowed_tool_names={"record_note"}, runtime_envelope=envelope)
    monkeypatch.setattr(worker, "runtime_tool_schemas_for_names", lambda names: [DELEGATE_SCHEMA])
    specialized = {**DELEGATE_SCHEMA, "function": {**DELEGATE_SCHEMA["function"], "description": "Agents: Writer"}}

    async def specialize(_db, tools, **kwargs):
        assert kwargs == {"entity_id": "entity-1", "workspace_id": "workspace-1", "agent_id": "agent-1"}
        return [specialized if tool["function"]["name"] == "delegate_to_agent" else tool for tool in tools]

    monkeypatch.setattr(worker, "runtime_specialize_delegate_to_agent", specialize)
    authorized = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_delegation_authorized", authorized)

    tools, allowed, loop_envelope, can_delegate = await worker._loop_tools(
        None, ctx, entity_id="entity-1", workspace_id="workspace-1", agent_id="agent-1",
        user_id="user-1", conversation_id="conversation-1", task_id="task-1",
    )
    # The pre-check runs against the loop's widened envelope and principal.
    assert authorized.await_args.args[1] is loop_envelope
    assert authorized.await_args.kwargs == {
        "entity_id": "entity-1", "user_id": "user-1", "workspace_id": "workspace-1",
        "conversation_id": "conversation-1", "task_id": "task-1",
    }

    assert can_delegate
    assert specialized in tools
    assert {"delegate_to_agent", "update_task_plan", "complete_task"} <= allowed
    assert "delegate_to_agent" in loop_envelope.effective_allowed_tool_names()
    # The context's own envelope and the global profile are untouched.
    assert "delegate_to_agent" not in envelope.effective_allowed_tool_names()

    async def no_targets(_db, tools, **_kwargs):
        return [tool for tool in tools if tool["function"]["name"] != "delegate_to_agent"]

    monkeypatch.setattr(worker, "runtime_specialize_delegate_to_agent", no_targets)
    tools, allowed, loop_envelope, can_delegate = await worker._loop_tools(
        None, ctx, entity_id="entity-1", workspace_id="workspace-1", agent_id="agent-1",
    )
    assert not can_delegate
    assert "delegate_to_agent" not in allowed
    assert loop_envelope is envelope


def test_loop_controls_are_runtime_owned_control_tools():
    from packages.core.ai.runtime.approval_classifier import (
        RUNTIME_LOOP_CONTROL_TOOL_NAMES,
        classify_runtime_tool,
    )
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    for name in (worker.UPDATE_TASK_PLAN_TOOL_NAME, worker.COMPLETE_TASK_TOOL_NAME):
        assert name in RUNTIME_LOOP_CONTROL_TOOL_NAMES
        classification = classify_runtime_tool(name, {})
        assert classification.effect is RuntimeToolEffect.CONTROL
        assert classification.authorization.action_key == f"runtime.control.{name}"



def test_delegation_requires_its_canonical_action_rule():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from tests.approval_policy_test_utils import evaluate_runtime_action
    from tests.approval_settings_test_utils import action_rule

    delegation = classify_runtime_tool("delegate_to_agent", {
        "tasks": [{"agent": "Writer", "task": "Draft the launch note."}],
    })
    assert delegation.effect is RuntimeToolEffect.ACTION
    assert delegation.action.action_key == "workspace.service.delegate"
    assert evaluate_runtime_action(delegation.action).outcome == "ask"
    rules = [{"id": "fixture-delegation", **action_rule("workspace.service.delegate")}]
    assert evaluate_runtime_action(delegation.action, rules=rules).outcome == "allow"
    unrelated = classify_runtime_tool("workspace_agent", {"action": "create_task"})
    assert evaluate_runtime_action(unrelated.action, rules=rules).outcome == "ask"


def test_engine_migration_matches_the_model():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / (
        "packages/core/migrations/versions/20261003_02_task_coordinator_engine.py"
    )
    spec = importlib.util.spec_from_file_location("task_coordinator_engine_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    added = {}
    module.table_exists = lambda table: True
    module.column_exists = lambda table, column: column in added
    module.op = SimpleNamespace(
        add_column=lambda table, column: added.update({column.name: column}),
        drop_column=lambda table, column: added.pop(column),
    )
    module.upgrade()
    module.upgrade()
    column = added["engine"]
    model = TaskCoordinatorRun.__table__.c.engine
    assert not column.nullable and not model.nullable
    assert column.type.length == model.type.length == 20
    assert column.server_default.arg == model.server_default.arg == "plan_dag"
    module.downgrade()
    assert not added


# ── Real harness and authorization (only the model round is faked) ─────


async def _real_workspace(client, db, name: str):
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    response = await register_user_and_get_token(client, json={
        "username": name, "email": f"{name}@example.com",
        "password": "securepass123", "entity_name": f"{name} Co",
    })
    assert response.status_code == 200, response.text
    entity_id, user_id = response.json()["entity_id"], response.json()["user_id"]
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Launch", status="active")
    db.add(workspace)
    writer = Agent(id=generate_ulid(), entity_id=entity_id, name="Writer", description="Drafts posts.", status="active")
    editor = Agent(id=generate_ulid(), entity_id=entity_id, name="Editor", description="Owns launches.", status="active")
    db.add_all([writer, editor])
    if await db.get(Agent, MANOR_AGENT_ID) is None:
        db.add(Agent(id=MANOR_AGENT_ID, name="Manor AI", status="active"))
    await db.flush()
    from packages.core.governance.approval_policy_service import initialize_workspace_approval
    await initialize_workspace_approval(db, workspace)
    subs = {
        "writer": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=writer.id, service_key="writer", status="active"),
        "editor": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=editor.id, service_key="editor", status="active"),
        "manor": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=MANOR_AGENT_ID, status="active"),
    }
    db.add_all(subs.values())
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Write the launch note", description="Get the Writer to draft a note.",
        status="in_progress", task_type="general", details={}, creator_id=user_id,
    )
    db.add(task)
    await db.commit()
    return SimpleNamespace(entity_id=entity_id, user_id=user_id, workspace=workspace, task=task, subs=subs)


def _spy_authorization(monkeypatch):
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization import binding_verifier

    authorizations: list[tuple] = []
    verified: list[str] = []
    authorize = runtime_authorization.authorize_runtime_action
    verify = binding_verifier.CurrentRuntimeToolBindingVerifier.verify

    async def spy_authorize(db, **kwargs):
        decision = await authorize(db, **kwargs)
        access = kwargs.get("access")
        authorizations.append((
            kwargs.get("tool_name"), kwargs.get("principal_kind"),
            getattr(access, "value", access), decision.allowed,
        ))
        return decision

    async def spy_verify(self, db, request):
        verified.append(request.tool_name)
        return await verify(self, db, request)

    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", spy_authorize)
    monkeypatch.setattr(binding_verifier.CurrentRuntimeToolBindingVerifier, "verify", spy_verify)
    return authorizations, verified


def _install_real_harness(monkeypatch, db_session, selection):
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker, "create_worker_session", lambda: session_factory)
    # Factory selection is the only admission lookup faked here.
    monkeypatch.setattr(task_run_host, "resolve_task_execution", AsyncMock(return_value=SimpleNamespace(
        runtime_kind=TaskRuntimeKind.COORDINATED_PLAN, coordinator=selection,
    )))
    from packages.core.ai.tools import workspace_agent_tools

    delegated = AsyncMock(return_value=json.dumps({"status": "completed", "answer": "Draft: launch on 2026-10-10"}))
    monkeypatch.setattr(workspace_agent_tools, "runtime_workspace_delegate_service_action", delegated)
    return delegated


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["editor", "manor"])
async def test_real_harness_delegates_and_completes(client, db_session, monkeypatch, root):
    from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval

    env = await _real_workspace(client, db_session, f"agent_loop_real_{root}")
    # This completion path has consent to delegate; Workspace member authority
    # is still checked by the real harness independently of this exact rule.
    await set_workspace_test_approval(
        db_session, workspace_id=env.workspace.id, entity_id=env.entity_id,
        actor_id=env.user_id, user_rules=[action_rule("workspace.service.delegate")],
    )
    _flag(monkeypatch, True)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=env.user_id),
    )
    sub = env.subs[root]
    binding = TaskPlanServiceBinding(subscription_id=sub.id, service_key=sub.service_key or "owner", agent_id=sub.agent_id)
    selection = CoordinatorRuntimeSelection(root=binding, bindings=(binding,))
    task = await _fresh(db_session, Task, env.task.id)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    delegated = _install_real_harness(monkeypatch, db_session, selection)
    authorizations, verified = _spy_authorization(monkeypatch)

    def after_delegation(messages, tools):
        assert "delegate_to_agent" in {tool["function"]["name"] for tool in tools}
        result = next(m["content"] for m in messages if m.get("tool_call_id") == "delegate")
        assert "blocked_by_permission" not in result, result
        assert "not available in the" not in result, result
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [_call("delegate", "delegate_to_agent", {"tasks": [{"agent": "Writer", "task": "Draft the launch note."}]})]),
        after_delegation,
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=wake.event_id, task_id=task.id, wake_generation=wake.wake_generation,
    )

    assert outcome == {"status": "completed", "task_id": task.id}, outcome
    assert len(seen) == 2
    delegated.assert_awaited_once()
    assert delegated.await_args.kwargs["params"]["agent_subscription_id"] == env.subs["writer"].id
    task = await _fresh(db_session, Task, task.id)
    assert task.status == "completed" and task.actual_output["engine"] == "agent_loop"
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert run.phase == TaskCoordinatorPhase.COMPLETED.value
    # The loop runs as its executing workspace member, so delegation is
    # authorized by that member's Workspace role (governance unchanged), not
    # by a durable Agent tool binding: no binding exemption is involved.
    assert ("delegate_to_agent", "workspace_member", "action", True) in authorizations
    assert ("complete_task", "workspace_member", "control", True) in authorizations
    assert "delegate_to_agent" not in verified


@pytest.mark.asyncio
async def test_real_harness_task_created_in_the_loop_is_linked_and_listed(client, db_session, monkeypatch):
    """create_task through the real tools: provenance from the run's envelope."""

    from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval

    env = await _real_workspace(client, db_session, "agent_loop_real_create")
    # Consent to create Tasks without a per-call approval, as the delegation
    # test grants delegation.
    await set_workspace_test_approval(
        db_session, workspace_id=env.workspace.id, entity_id=env.entity_id,
        actor_id=env.user_id, user_rules=[action_rule("workspace.task.create")],
    )
    _flag(monkeypatch, True)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=env.user_id),
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
    )
    sub = env.subs["editor"]
    binding = TaskPlanServiceBinding(subscription_id=sub.id, service_key=sub.service_key, agent_id=sub.agent_id)
    selection = CoordinatorRuntimeSelection(root=binding, bindings=(binding,))
    task = await _fresh(db_session, Task, env.task.id)
    await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    _install_real_harness(monkeypatch, db_session, selection)

    def after_create(messages, _tools):
        result = json.loads(next(m["content"] for m in messages if m.get("tool_call_id") == "mk"))
        assert result.get("created") is True, result
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    _model(monkeypatch, [
        # The Task loop surface's own create_task tool.
        ("", [_call("mk", "workspace_create_task", {
            "title": "Follow-up review", "owner_service_key": "writer",
        })]),
        after_create,
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=wake.event_id, task_id=task.id, wake_generation=wake.wake_generation,
    )

    assert outcome == {"status": "completed", "task_id": task.id}, outcome
    [child] = (await db_session.execute(
        select(Task).where(Task.entity_id == env.entity_id, Task.title == "Follow-up review")
        .execution_options(populate_existing=True)
    )).scalars().all()
    assert child.parent_task_id == task.id
    assert child.details["origin_task_id"] == task.id
    output = (await _fresh(db_session, Task, task.id)).actual_output
    assert [(e["action_key"], e["count"]) for e in output["effects"]] == [("workspace.task.create", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["editor", "manor"])
async def test_workspace_task_surface_already_carries_delegation(client, db_session, root):
    """A Workspace Task's own scheduled surface grants delegate_to_agent.

    _loop_tools therefore leaves the real envelope untouched (no widening) and
    the tool appears exactly once; its add-for-this-loop branch only applies
    when a surface lacks it.
    """

    from packages.core.ai.context import build_agent_context
    from packages.core.ai.runtime.policies import check_runtime_tool_policy
    from packages.core.ai.runtime.surfaces import ChatSurface

    env = await _real_workspace(client, db_session, f"agent_loop_scope_{root}")
    sub = env.subs[root]
    ctx = await build_agent_context(
        db_session, entity_id=env.entity_id, user_id=env.user_id, agent_id=sub.agent_id,
        agent_subscription_id=sub.id, workspace_id=env.workspace.id, task_id=env.task.id,
        active_user_message="Write the launch note", runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
    )
    assert check_runtime_tool_policy(envelope=ctx.runtime_envelope, tool_name="delegate_to_agent").allowed
    tools, allowed, envelope, can_delegate = await worker._loop_tools(
        db_session, ctx, entity_id=env.entity_id, workspace_id=env.workspace.id, agent_id=sub.agent_id,
        user_id=env.user_id, task_id=env.task.id,
    )
    assert can_delegate and "delegate_to_agent" in allowed
    assert envelope is ctx.runtime_envelope
    names = [tool["function"]["name"] for tool in tools]
    assert names.count("delegate_to_agent") == 1
    assert names[-2:] == ["update_task_plan", "complete_task"]
    schema = next(tool for tool in tools if tool["function"]["name"] == "delegate_to_agent")
    assert "Writer" in schema["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"]
    await db_session.rollback()


@pytest.mark.asyncio
async def test_delegation_is_not_offered_when_the_member_may_not_delegate(client, db_session):
    """Delegation needs Workspace manage authority; a plain member is refused."""

    from packages.core.ai.context import build_agent_context
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.models.user import UserMembership

    env = await _real_workspace(client, db_session, "agent_loop_member_owner")
    response = await register_user_and_get_token(client, json={
        "username": "agent_loop_member", "email": "agent_loop_member@example.com",
        "password": "securepass123", "entity_name": "agent_loop_member Co",
    })
    member_id = response.json()["user_id"]
    db_session.add(UserMembership(user_id=member_id, entity_id=env.entity_id, role="member", status="active"))
    await db_session.commit()
    sub = env.subs["editor"]
    offered = {}
    for name, user_id in (("owner", env.user_id), ("member", member_id)):
        ctx = await build_agent_context(
            db_session, entity_id=env.entity_id, user_id=user_id, agent_id=sub.agent_id,
            agent_subscription_id=sub.id, workspace_id=env.workspace.id, task_id=env.task.id,
            active_user_message="Write the launch note", runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
        )
        tools, allowed, _envelope, can_delegate = await worker._loop_tools(
            db_session, ctx, entity_id=env.entity_id, workspace_id=env.workspace.id,
            agent_id=sub.agent_id, user_id=user_id, task_id=env.task.id,
        )
        names = [tool["function"]["name"] for tool in tools]
        offered[name] = can_delegate
        assert ("delegate_to_agent" in names) is can_delegate
        assert ("delegate_to_agent" in allowed) is can_delegate
        assert (
            "delegate_to_agent" in worker._task_section("brief", can_delegate=can_delegate)
        ) is can_delegate
    assert offered == {"owner": True, "member": False}
    await db_session.rollback()


@pytest.mark.parametrize(("field", "value"), [("deliverables", 5), ("acceptance", "x")])
def test_malformed_submission_lists_get_a_preflight_message(field, value):
    submission, error = worker._parse_submission({**COMPLETE_ARGS, field: value})
    assert submission is None
    assert error == f"{field} must be a list"


@pytest.mark.asyncio
async def test_malformed_submission_is_corrected_in_the_loop(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    errors = []

    def corrected(nxt):
        def step(messages, _tools):
            error = json.loads(messages[-1]["content"])
            assert error["status"] == "error"
            errors.append(error["error"])
            return "", [nxt]
        return step

    _model(monkeypatch, [
        ("", [_call("a", "complete_task", {**COMPLETE_ARGS, "deliverables": 5})]),
        corrected(_call("b", "complete_task", {**COMPLETE_ARGS, "acceptance": "x"})),
        corrected(_call("c", "complete_task", COMPLETE_ARGS)),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["status"] == "completed"
    assert len(errors) == 2


@pytest.mark.parametrize("submission", [None, "x", {"summary": "s", "deliverables": 5, "acceptance": "x"}])
def test_cap_submission_tolerates_malformed_input(submission):
    capped = worker.cap_submission(submission)
    assert capped["deliverables"] == [] and capped["acceptance"] == []


@pytest.mark.asyncio
async def test_task_is_locked_before_the_run_when_binding_the_conversation(db_session, monkeypatch):
    from sqlalchemy import event

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    claim = (await task_run_host.admit_and_claim(
        event_id=event_id, task_id=task.id, wake_generation=generation,
        engine="agent_loop", lease_seconds=60, session_factory=host.session_factory,
    ))[0]
    locked: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        if "FOR UPDATE" in statement:
            locked.append(statement.split("FROM", 1)[1].split()[0])

    engine = db_session.bind.sync_engine if hasattr(db_session.bind, "sync_engine") else db_session.bind
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await worker._ensure_task_conversation(host.session_factory, claim) == host.conversation_id
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert locked[-2:] == ["tasks", "task_coordinator_runs"]


@pytest.mark.asyncio
async def test_retry_after_repair_starts_a_fresh_transcript_with_the_plan(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    plan = {"items": [{"title": "Draft", "status": "pending"}], "acceptance_criteria": ["Names the date."], "revision": 2}
    run.checkpoint = {
        "messages": [{"role": "user", "content": "poisoned transcript"}], "rounds": 9,
        "task_plan": plan,
        "coordinator_outcome": {
            "error": "agent_loop_repair_required",
            "details": {"exception": "RuntimeError", "error": "boom", "attempts": 3},
        },
    }
    run.phase = TaskCoordinatorPhase.FAILED.value
    task = await _fresh(db_session, Task, task.id)
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await db_session.commit()

    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.commit()
    from packages.core.ai.runtime.run_state import RuntimeRunState, runtime_run_state_persisted_keys

    # Only the declared carried slot survives; every core field starts empty.
    assert set(run.checkpoint) == {"retried_after", "task_plan"} | runtime_run_state_persisted_keys()
    assert RuntimeRunState.restore(run.checkpoint).snapshot() == RuntimeRunState().snapshot()
    assert run.checkpoint["task_plan"] == plan
    assert run.checkpoint["retried_after"]["fresh_transcript"] is True

    host = _install_host(monkeypatch, db_session, selection)
    # The kept plan's criteria still bind the submission.
    seen = _model(monkeypatch, [("", [_call("done", "complete_task", {
        **COMPLETE_ARGS, "acceptance": [{"criterion": "Names the date.", "met": True, "evidence": "Read it."}],
    })])])
    event = await _event(db_session, task.id)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=run.wake_generation,
    )

    assert outcome["status"] == "completed"
    loop = host.loop_calls[-1]
    assert "initial_messages" not in loop and "run_state" not in loop and "runtime_checkpoint" not in loop
    assert loop["user_message"].startswith("# Task\nWrite the launch note")
    assert "after agent_loop_repair_required (RuntimeError: boom)" in loop["user_message"]
    assert "fresh transcript" in loop["user_message"]
    # The kept plan is rendered, so the model can see what it is told about.
    assert "## Plan kept from the previous attempt (revision 2)" in loop["user_message"]
    assert "- [pending] Draft" in loop["user_message"]
    assert "Acceptance criteria:\n- Names the date." in loop["user_message"]
    assert all("poisoned transcript" not in str(m.get("content")) for m in seen[0])
    final = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint
    assert final["task_plan"] == plan



@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", [
    lambda: sa_exc.OperationalError("SELECT 1", {}, Exception("connection reset")),
    lambda: sa_exc.InterfaceError("SELECT 1", {}, Exception("connection closed")),
    lambda: sa_exc.DBAPIError("SELECT 1", {}, Exception("server closed"), connection_invalidated=True),
    lambda: asyncio.TimeoutError("pool timeout"),
])
async def test_transient_infrastructure_errors_never_count_toward_repair(db_session, monkeypatch, make_error):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    host.context.side_effect = lambda *_args, **_kwargs: (_ for _ in ()).throw(make_error())

    for _ in range(worker._MAX_WAKE_FAILURES + 1):
        with pytest.raises(type(make_error())):
            await worker.run_task_agent_loop_once(
                event_id=event_id, task_id=task.id, wake_generation=generation,
            )
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert "wake_failures" not in (row.checkpoint or {})
    assert row.phase != TaskCoordinatorPhase.FAILED.value and row.claim_token is None
    assert (await _event(db_session, task.id)).delivered_at is None
    assert (await _fresh(db_session, Task, task.id)).status == "in_progress"


@pytest.mark.asyncio
async def test_a_checkpoint_from_a_newer_runtime_is_left_for_a_current_worker(db_session, monkeypatch, caplog):
    """A rolling deploy's older worker logs it distinctly and never counts it as a failure."""
    from packages.core.ai.runtime.run_state import RUN_STATE_VERSION_KEY, RuntimeRunState, RuntimeRunStateVersionError

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    newer = {RUN_STATE_VERSION_KEY: RuntimeRunState.VERSION + 1, "messages": [{"role": "user", "content": "x"}]}
    run.checkpoint = newer
    await db_session.commit()
    host = _install_host(monkeypatch, db_session, selection)

    with caplog.at_level("WARNING"), pytest.raises(RuntimeRunStateVersionError):
        await worker.run_task_agent_loop_once(event_id=event_id, task_id=task.id, wake_generation=generation)

    assert "saved by a newer runtime" in caplog.text
    assert host.loop_calls == []
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.checkpoint == newer, "never rewritten in an older shape"
    assert row.phase != TaskCoordinatorPhase.FAILED.value and row.claim_token is None
    assert (await _event(db_session, task.id)).delivered_at is None


@pytest.mark.asyncio
async def test_retrying_a_task_a_newer_runtime_saved_is_a_clear_conflict(db_session, monkeypatch):
    from packages.core.ai.runtime.run_state import RUN_STATE_NEWER_RUNTIME_DETAIL, RUN_STATE_VERSION_KEY, RuntimeRunState
    from packages.core.services import workspace_access, workspace_autonomy
    from packages.core.services.task_retry_service import TaskRetryError, prepare_task_retry

    task, run, _selection, _event_id, _generation = await _agent_loop_run(db_session, monkeypatch)
    run.checkpoint = {RUN_STATE_VERSION_KEY: RuntimeRunState.VERSION + 1}
    run.phase = TaskCoordinatorPhase.FAILED.value
    task = await _fresh(db_session, Task, task.id)
    task.status = "blocked"
    await db_session.commit()
    monkeypatch.setattr(workspace_access, "lock_workspace_access_boundary", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(workspace_autonomy, "workspace_allows_manual_work", lambda _workspace: True)

    with pytest.raises(TaskRetryError) as raised:
        await prepare_task_retry(db_session, task=task, user_id="user-1", user_label="Ana")

    assert raised.value.status_code == 409 and raised.value.detail == RUN_STATE_NEWER_RUNTIME_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", [
    lambda: sa_exc.DataError("INSERT", {}, Exception("unsupported Unicode escape sequence \\u0000")),
    lambda: sa_exc.IntegrityError("INSERT", {}, Exception("duplicate key")),
    lambda: sa_exc.ProgrammingError("SELECT", {}, Exception("syntax error")),
    lambda: sa_exc.DBAPIError("SELECT", {}, Exception("plain DBAPI failure")),
])
async def test_deterministic_database_errors_count_and_block(db_session, monkeypatch, make_error):
    """Data/integrity/programming errors repeat on every redelivery; bound them."""

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    host.context.side_effect = lambda *_args, **_kwargs: (_ for _ in ()).throw(make_error())

    for attempt in range(1, worker._MAX_WAKE_FAILURES):
        with pytest.raises(type(make_error())):
            await worker.run_task_agent_loop_once(
                event_id=event_id, task_id=task.id, wake_generation=generation,
            )
        row = await _fresh(db_session, TaskCoordinatorRun, run.id)
        assert row.checkpoint["wake_failures"]["count"] == attempt
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["status"] == "blocked" and outcome["code"] == "agent_loop_repair_required"
    failure = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["coordinator_outcome"]
    assert failure["details"]["exception"] == type(make_error()).__name__
    assert (await _event(db_session, task.id)).delivered_at is not None


@pytest.mark.asyncio
async def test_resource_wait_past_its_deadline_blocks_instead_of_parking(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    async def waiting(**kwargs):
        return loop_module.AgenticResult(
            content="", messages=[{"role": "user", "content": "brief"}], usage={}, rounds=1,
            stop_reason="waiting_resource",
            control={"kind": "waiting_resource", "reason": "sandbox_capacity",
                     "reservation_id": "r-1", "poll_after_seconds": 30, "deadline_at": expired,
                     "checkpoint": {"messages": [], "pending_tool_call": {"id": "x"}}},
        )

    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", waiting)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": "agent_loop_resource_wait_expired"}
    failure = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["coordinator_outcome"]
    assert "sandbox_capacity" in failure["message"] and "approval" not in failure["message"]
    assert failure["details"]["deadline_at"] == expired


@pytest.mark.asyncio
async def test_parked_resource_wait_past_its_deadline_blocks_without_resuming(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    run.checkpoint = {
        "messages": [{"role": "user", "content": "brief"}], "rounds": 1,
        "runtime_checkpoint": {"messages": [], "pending_tool_call": {"id": "x"}},
        "resource_wait": {"reason": "sandbox_capacity", "reservation_id": "r-1",
                          "deadline_at": expired, "poll_after_seconds": 30},
    }
    await db_session.commit()
    _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(
        worker, "runtime_execute_agentic_loop",
        AsyncMock(side_effect=AssertionError("an expired wait must not resume")),
    )
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["code"] == "agent_loop_resource_wait_expired"
    assert (await _fresh(db_session, Task, task.id)).status == "blocked"



def test_kept_plan_rendering_is_capped():
    plan = {
        "revision": 1,
        "items": [{"title": "t" * 600, "status": "pending", "owner": "o" * 600} for _ in range(80)],
        "acceptance_criteria": ["c" * 600 for _ in range(40)],
    }
    rendered = worker._render_kept_plan(plan)
    assert len(rendered) <= worker._KEPT_PLAN_MAX_CHARS
    assert rendered.startswith("## Plan kept from the previous attempt (revision 1)")
    assert worker._render_kept_plan(None) == ""
    assert worker._render_kept_plan({"items": "x"}) == ""


@pytest.mark.asyncio
async def test_delegation_precheck_failure_fails_closed(monkeypatch):
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import runtime_authorization

    surface = ChatSurface.SCHEDULED_AGENT_RUN
    envelope = RuntimeEnvelope(
        surface=surface,
        principal=resolve_runtime_principal(surface=surface, entity_id="e", user_id="u", workspace_id="w"),
        profile=RuntimeProfile.BACKGROUND_WORKER,
        tool_names=("record_note", "delegate_to_agent"),
        allowed_tool_names=("record_note", "delegate_to_agent"),
    )
    monkeypatch.setattr(
        runtime_authorization, "authorize_runtime_action",
        AsyncMock(side_effect=RuntimeError("authorization store unavailable")),
    )

    async def keep(_db, tools, **_kwargs):
        return tools

    monkeypatch.setattr(worker, "runtime_specialize_delegate_to_agent", keep)
    ctx = SimpleNamespace(
        tools=[NOTE_TOOL, DELEGATE_SCHEMA], allowed_tool_names={"record_note", "delegate_to_agent"},
        runtime_envelope=envelope,
    )
    tools, allowed, _envelope, can_delegate = await worker._loop_tools(
        None, ctx, entity_id="e", workspace_id="w", agent_id="a", user_id="u", task_id="t",
    )
    assert not can_delegate
    assert "delegate_to_agent" not in [tool["function"]["name"] for tool in tools]
    assert "delegate_to_agent" not in allowed
    assert {"update_task_plan", "complete_task"} <= allowed


# ── Phase A2: the loop's live receipt ledger ───────────────────────────


def test_harness_runs_on_a_host_owned_tool_evidence_ledger():
    """A host passes its own ledger; the loop observes into that instance."""

    from unittest.mock import patch

    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    seen = {}

    async def fake_agentic_loop(**kwargs):
        seen["tool_evidence"] = kwargs["tool_evidence"]
        return SimpleNamespace(content="ok", usage={}, rounds=1, tool_calls_made=[])

    restored = RuntimeToolEvidence()
    restored.observe_tool_result("generate_file", "c1", json.dumps({
        "document": {"document_id": "doc1", "fs_path": "Reports/a.md"},
    }), {"kind": "md"})
    ledger = RuntimeToolEvidence()
    with (
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
        patch("packages.core.ai.llm_client.ensure_billing_context", new=lambda *a, **k: None),
    ):
        asyncio.run(runtime_execute_agentic_loop(
            runtime_envelope=None, system_prompt="s", user_message="u", tools=[],
            entity_id="e", agent_id=None, tool_evidence=ledger,
            initial_tool_evidence=restored.checkpoint_state(),
        ))
    assert seen["tool_evidence"] is ledger
    assert [r["handle"] for r in ledger.receipts] == ["r1"]

    other = RuntimeToolEvidence()
    with pytest.raises(ValueError, match="one tool evidence ledger"):
        asyncio.run(runtime_execute_agentic_loop(
            runtime_envelope=None, system_prompt="s", user_message="u", tools=[],
            entity_id="e", agent_id=None, tool_evidence=ledger, final_response_contract=other,
        ))


# ── Phase A2: complete_task integrity checks ───────────────────────────


TWO_CRITERIA_PLAN = {
    "items": [{"title": "Write the note", "status": "in_progress"}],
    "acceptance_criteria": ["The note names the launch date.", "The note is under 50 words."],
}


def _accept(criterion: str, met: bool = True) -> dict:
    return {"criterion": criterion, "met": met, "evidence": f"Checked: {criterion}"}


def _completion(*, deliverables=(), acceptance=None, outcome="completed", **extra) -> dict:
    return {
        "outcome": outcome,
        "summary": "Recorded the launch note.",
        "deliverables": list(deliverables),
        "acceptance": acceptance if acceptance is not None else [_accept("The note names the launch date.")],
        **extra,
    }


def _tool_error(messages) -> dict:
    payload = json.loads(messages[-1]["content"])
    assert payload["status"] == "error", payload
    return payload


@pytest.fixture
def workspace_files(monkeypatch, tmp_path):
    """Real Knowledge files: a Document row plus bytes under MANOR_FS_ROOT."""

    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.models.user import Entity, User, UserMembership

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")

    async def create(db, task, name: str, text: str = "Launch on 2026-10-10.\n") -> SimpleNamespace:
        root = tmp_path / task.entity_id
        root.mkdir(exist_ok=True)
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
        user_id = (await db.execute(
            select(User.id).where(User.entity_id == task.entity_id)
        )).scalar_one_or_none()
        if user_id is None:
            from decimal import Decimal

            from packages.core.models.workspace import Workspace

            user_id = generate_ulid()
            db.add_all([
                Entity(id=task.entity_id, name="Agent loop entity"),
                User(id=user_id, entity_id=task.entity_id, email=f"{user_id}@test.invalid",
                     password_hash="unused", role="owner", status="active"),
                UserMembership(entity_id=task.entity_id, user_id=user_id, role="owner", status="active"),
                Workspace(
                    id=task.workspace_id, entity_id=task.entity_id, name="Launch", status="active",
                    operating_model={}, settings={}, monthly_spent_usd=Decimal("0"),
                    auto_pause_on_budget=True,
                ),
            ])
        document = Document(
            id=generate_ulid(), entity_id=task.entity_id, name=name.rsplit("/", 1)[-1], fs_path=name,
            owner_id=user_id, file_type=name.rsplit(".", 1)[-1], visibility="entity",
            metadata_={"origin": {"task_id": task.id, "workspace_id": task.workspace_id}},
        )
        db.add(document)
        await db.commit()
        monkeypatch.setattr(worker, "resolve_task_execution_user_id", AsyncMock(return_value=user_id))
        return SimpleNamespace(user_id=user_id, document_id=document.id, fs_path=name)

    return create


NON_FILE_ADVICE = (
    "If this is not a Workspace file (a sent message, an external link, a record), remove it "
    "from deliverables and describe it in summary or in the acceptance evidence."
)


def _read_tool(host, reads: list | None = None):
    async def read_file(args):
        if reads is not None:
            reads.append(args)
        return json.dumps({"path": args.get("path"), "content": "Launch on 2026-10-10."})

    host.extra_tools["read_file"] = read_file


@pytest.mark.asyncio
async def test_missing_acceptance_criterion_is_refused_then_completes(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    first = [_accept("The note names the launch date.")]

    def refused(messages, _tools):
        error = _tool_error(messages)
        assert error["error"] == "completion_check_failed"
        assert "'The note is under 50 words.'" in error["message"]
        assert "names the launch date" not in error["message"]
        return "", [_call("done", "complete_task", _completion(acceptance=first + [
            # Matched by normalized text: case, spacing and the final period.
            _accept("the note is   under 50 words"),
        ]))]

    seen = _model(monkeypatch, [
        ("", [_call("plan", "update_task_plan", TWO_CRITERIA_PLAN)]),
        ("", [_call("early", "complete_task", _completion(acceptance=first))]),
        refused,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "completed", "task_id": task.id}
    assert len(seen) == 3
    # A refused check is corrected in the loop; no continuation was spent.
    run = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert run.checkpoint["non_submit_retries"] == 0


@pytest.mark.asyncio
async def test_unmet_criterion_cannot_be_reported_completed(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)

    def refused(messages, _tools):
        error = _tool_error(messages)
        assert "is met=false, but outcome=completed needs every criterion met" in error["message"]
        return "", [_call("done", "complete_task", _completion())]

    seen = _model(monkeypatch, [
        ("", [_call("early", "complete_task", _completion(
            acceptance=[_accept("The note names the launch date.", met=False)],
        ))]),
        refused,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["status"] == "completed" and len(seen) == 2


@pytest.mark.asyncio
async def test_without_a_plan_self_verification_must_state_a_criterion(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)

    def refused(messages, _tools):
        assert "acceptance is empty" in _tool_error(messages)["message"]
        return "", [_call("done", "complete_task", _completion())]

    seen = _model(monkeypatch, [("", [_call("early", "complete_task", _completion(acceptance=[]))]), refused])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "completed"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_deliverable_paths_and_handles_must_be_real(db_session, monkeypatch, workspace_files):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    file = await workspace_files(db_session, task, "Reports/launch-note.md")
    _read_tool(host)

    def refused(messages, _tools):
        message = _tool_error(messages)["message"]
        assert "deliverable 1 'Reports/missing.md' is not an existing, readable file in this Workspace" in message
        # Handles are host-only until W2: an unknown one is just an unknown file.
        assert "deliverable 2 'r9' is not an existing, readable file in this Workspace" in message
        assert "deliverable 3 'Launch note' has no reference" in message
        assert message.count(NON_FILE_ADVICE) == 3
        assert "handle" not in message and "receipt" not in message
        return "", [_call("read", "read_file", {"path": file.fs_path})]

    _model(monkeypatch, [
        ("", [_call("bad", "complete_task", _completion(deliverables=[
            {"title": "Missing", "reference": "Reports/missing.md"},
            {"title": "Forged", "reference": "r9"},
            {"title": "Launch note"},
        ]))]),
        refused,
        ("", [_call("done", "complete_task", _completion(deliverables=[
            {"title": "Launch note", "reference": file.fs_path},
        ]))]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "completed", "task_id": task.id}
    files = (await _fresh(db_session, Task, task.id)).actual_output["files"]
    assert [(f["document_id"], f["fs_path"]) for f in files] == [(file.document_id, file.fs_path)]


@pytest.mark.asyncio
async def test_existing_file_must_be_inspected_before_completion(db_session, monkeypatch, workspace_files):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    file = await workspace_files(db_session, task, "Reports/launch-note.md")
    _read_tool(host)
    by_document = {"title": "Launch note", "reference": file.document_id}

    def refused(messages, _tools):
        message = _tool_error(messages)["message"]
        assert message.startswith(
            "Inspect Reports/launch-note.md before completing (e.g. read_file/probe_media)"
        ), message
        # Read by path, deliver by Document id: the same verified file.
        return "", [_call("read", "read_file", {"path": f"/api/v1/fs/{task.entity_id}/{file.fs_path}"})]

    seen = _model(monkeypatch, [
        ("", [_call("early", "complete_task", _completion(deliverables=[by_document]))]),
        refused,
        ("", [_call("done", "complete_task", _completion(deliverables=[by_document]))]),
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "completed"
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_receipt_handle_is_a_deliverable_once_read_after_it_was_written(
    db_session, monkeypatch, workspace_files,
):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    _read_tool(host)
    document_id = (await workspace_files(db_session, task, "Reports/plan.md", "# Plan\n")).document_id

    async def generate_file(args):
        return json.dumps({"document": {
            "document_id": document_id, "fs_path": "Reports/plan.md", "name": "plan.md",
        }})

    host.extra_tools["generate_file"] = generate_file
    deliverable = {"title": "Launch plan", "reference": "r1"}

    def unread(messages, _tools):
        assert _tool_error(messages)["message"].startswith("Inspect Reports/plan.md before completing")
        return "", [_call("read", "read_file", {"document_id": document_id})]

    _model(monkeypatch, [
        # Read before the write does not count: it must come after.
        ("", [_call("pre", "read_file", {"path": "Reports/plan.md"})]),
        ("", [_call("gen", "generate_file", {"kind": "md", "content": "# Plan"})]),
        ("", [_call("early", "complete_task", _completion(deliverables=[deliverable]))]),
        unread,
        ("", [_call("done", "complete_task", _completion(deliverables=[deliverable]))]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "completed", "task_id": task.id}
    output = (await _fresh(db_session, Task, task.id)).actual_output
    assert [f["document_id"] for f in output["files"]] == [document_id]
    assert output["receipts"][0]["handle"] == "r1"
    assert output["receipts"][0]["fs_path"] == "Reports/plan.md"


@pytest.mark.asyncio
async def test_unconfirmed_effect_is_refused_once_then_blocked_is_accepted(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish(args):
        # Not a loop error (the loop keeps its tools), but not confirmed either.
        return json.dumps({"status": "partial", "message": "2 of 3 channels confirmed"})

    host.extra_tools["publish_post"] = publish

    def refused(messages, _tools):
        error = _tool_error(messages)
        assert error["error"] == "unconfirmed_effects"
        assert "publish_post" in error["message"]
        assert error["details"]["unconfirmed_calls"] == [
            {"call_id": "pub", "tool": "publish_post", "operation": "publish_post"},
        ]
        return "", [_call("done", "complete_task", _completion(
            outcome="blocked", blocked_reason="The post may or may not be live; please check.",
            acceptance=[_accept("The note names the launch date.", met=False)],
        ))]

    _model(monkeypatch, [
        ("", [_call("pub", "publish_post", {"text": "Launch!"})]),
        ("", [_call("early", "complete_task", _completion())]),
        refused,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": "agent_loop_reported_blocked"}
    task = await _fresh(db_session, Task, task.id)
    assert task.status == "blocked"
    assert task.actual_output["outcome"]["outcome"] == "completed_non_success"
    assert task.actual_output["outcome"]["effect_state"] == "unconfirmed"
    assert task.actual_output["outcome"]["downgraded_success"] is False


@pytest.mark.asyncio
async def test_insisting_on_completed_over_an_unconfirmed_effect_is_non_success(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish(args):
        # Not a loop error (the loop keeps its tools), but not confirmed either.
        return json.dumps({"status": "partial", "message": "2 of 3 channels confirmed"})

    host.extra_tools["publish_post"] = publish
    _read_tool(host)
    acceptance = [
        _accept("The note names the launch date."), _accept("The note is under 50 words."),
    ]
    _model(monkeypatch, [
        ("", [_call("plan", "update_task_plan", TWO_CRITERIA_PLAN)]),
        ("", [_call("pub", "publish_post", {"text": "Launch!"})]),
        ("", [_call("early", "complete_task", _completion(acceptance=acceptance))]),
        # The model re-reads, then insists: accepted, but never as success.
        ("", [_call("check", "read_file", {"path": "Posts/launch.md"})]),
        ("", [_call("done", "complete_task", _completion(acceptance=acceptance))]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": "agent_loop_completed_non_success"}
    task = await _fresh(db_session, Task, task.id)
    assert task.status == "blocked"
    decision = task.actual_output["outcome"]
    assert decision["outcome"] == "completed_non_success" and decision["downgraded_success"] is True
    failure = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["coordinator_outcome"]
    assert failure["error"] == "agent_loop_completed_non_success"
    message = failure["message"]
    assert "Recorded the launch note." in message
    assert "publish_post" in message
    # One list, in the message and the details alike.
    claimed = ["The note names the launch date.", "The note is under 50 words."]
    assert failure["details"]["criteria_claimed_not_confirmed"] == claimed
    assert "unmet_criteria" not in failure["details"]
    assert "Acceptance criteria claimed met, not confirmed: " + "; ".join(claimed) in message
    assert [c["tool"] for c in failure["details"]["unconfirmed_calls"]] == ["publish_post"]


@pytest.mark.asyncio
async def test_new_unconfirmed_effect_after_a_refusal_is_refused_again(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish(args):
        return json.dumps({"status": "unknown"})

    host.extra_tools["publish_post"] = publish
    refusals = []

    def refused(nxt):
        def step(messages, _tools):
            refusals.append(_tool_error(messages)["details"]["unconfirmed_calls"])
            return "", nxt
        return step

    _model(monkeypatch, [
        ("", [_call("p1", "publish_post", {"text": "a"})]),
        ("", [_call("c1", "complete_task", _completion())]),
        refused([_call("p2", "publish_post", {"text": "b"})]),
        ("", [_call("c2", "complete_task", _completion())]),
        refused([_call("c3", "complete_task", _completion())]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["code"] == "agent_loop_completed_non_success"
    assert [[c["call_id"] for c in calls] for calls in refusals] == [["p1"], ["p1", "p2"]]


@pytest.mark.asyncio
async def test_pending_media_job_for_the_task_is_refused(db_session, monkeypatch):
    from packages.core.models.media_job import MediaJob

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    job = MediaJob(
        id=generate_ulid(), entity_id=task.entity_id, kind="video", status="processing",
        prompt="launch teaser", params={"task_id": task.id},
    )
    other = MediaJob(
        id=generate_ulid(), entity_id=task.entity_id, kind="video", status="pending",
        prompt="other task", params={"task_id": generate_ulid()},
    )
    db_session.add_all([job, other])
    await db_session.commit()

    from packages.core import database

    # The loop's own wait_media_jobs reads the real job rows.
    monkeypatch.setattr(database, "async_session", host.session_factory)

    async def refused(messages, _tools):
        message = _tool_error(messages)["message"]
        assert f"Wait for job(s) {job.id} (wait_media_jobs) before completing" in message
        assert other.id not in message
        async with host.session_factory() as db:
            row = await db.get(MediaJob, job.id)
            row.status = "completed"
            await db.commit()
        return "", [_call("wait", "wait_media_jobs", {"job_ids": [job.id]})]

    seen = _model(monkeypatch, [
        ("", [_call("early", "complete_task", _completion())]),
        refused,
        ("", [_call("done", "complete_task", _completion())]),
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "completed"
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_running_delegation_session_is_refused(db_session, monkeypatch):
    from packages.core.models.agent_delegation_session import AgentDelegationSession

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    session = AgentDelegationSession(
        id=generate_ulid(), entity_id=task.entity_id, workspace_id=task.workspace_id,
        conversation_id=host.conversation_id, user_id="user-1",
        agent_subscription_id=generate_ulid(), agent_id=generate_ulid(), service_key="writer",
        messages=[], context_usage={}, context_state={}, status="running",
        active_run_id=generate_ulid(), active_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(session)
    await db_session.commit()

    async def check_writer(args):
        async with host.session_factory() as db:
            row = await db.get(AgentDelegationSession, session.id)
            row.status = "idle"
            await db.commit()
        return json.dumps({"ok": True})

    host.extra_tools["check_writer"] = check_writer

    def refused(messages, _tools):
        assert "A delegated agent is still working on this Task (writer)" in _tool_error(messages)["message"]
        return "", [_call("check", "check_writer", {})]

    seen = _model(monkeypatch, [
        ("", [_call("early", "complete_task", _completion())]),
        refused,
        ("", [_call("done", "complete_task", _completion())]),
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "completed"
    assert len(seen) == 3


def test_inspection_tools_are_read_only_by_classification():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.workers import task_agent_loop_completion as completion

    for name, arguments in completion.INSPECTION_TOOL_ARGUMENTS.items():
        args = {argument: "Reports/a.md" for argument in arguments}
        assert classify_runtime_tool(name, args).effect is RuntimeToolEffect.READ_ONLY, name
    # The action-tool inspections are explicit exceptions, not read-only tools.
    for name in completion.ACTION_INSPECTIONS:
        assert classify_runtime_tool(name, {}).effect is not RuntimeToolEffect.READ_ONLY, name


def test_inspection_tracker_orders_reads_after_writes_and_covers_review_directories():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
    from packages.core.workers.task_agent_loop_completion import InspectionTracker, file_keys

    ledger = RuntimeToolEvidence()
    tracker = InspectionTracker()

    def run(name, args, result):
        ledger.observe_tool_result(name, generate_ulid(), result, args)
        tracker.observe(name, result, args, ledger)

    keys = file_keys(fs_path="Videos/launch/renders/final.mp4")
    run("probe_media", {"input_path": "Videos/launch/renders/final.mp4"}, json.dumps({"duration": 3}))
    assert tracker.inspected_after_write(keys)  # never written by this run
    run("video_edit", {"operation": "render", "project_path": "Videos/launch"}, json.dumps({
        "status": "completed", "video": {"fs_path": "Videos/launch/renders/final.mp4"},
    }))
    assert not tracker.inspected_after_write(keys)
    run("probe_media", {"input_path": "Videos/launch/renders/final.mp4"}, json.dumps({"error": "x"}))
    assert not tracker.inspected_after_write(keys), "a failed read is not an inspection"
    run("video_edit", {"operation": "review", "project_path": "Videos/launch"}, json.dumps({
        "status": "ready_for_review", "review_sha256": "a" * 64,
    }))
    assert tracker.inspected_after_write(keys)
    # Composite gateway calls are unwrapped; state survives a checkpoint.
    restored = InspectionTracker(json.loads(json.dumps(tracker.state())))
    other = file_keys(fs_path="Reports/b.md")
    ledger.observe_tool_result("call_tool", "x", "{}", {"name": "read_file", "arguments": {"path": "Reports/b.md"}})
    restored.observe("call_tool", "{}", {"name": "read_file", "arguments": {"path": "Reports/b.md"}}, ledger)
    assert restored.inspected_after_write(other)


@pytest.mark.asyncio
async def test_a_display_path_read_satisfies_the_inspection_rule(db_session, monkeypatch, workspace_files):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    file = await workspace_files(db_session, task, "Reports/launch-taglines.md")
    display = "Workspaces/Launch/Reports/launch-taglines.md"

    async def generate_file(args):
        return json.dumps({"document": {"document_id": file.document_id, "fs_path": file.fs_path}})

    async def read_file(args):
        # As read_file reports a display path it resolved to the stored file.
        return json.dumps({"path": args.get("path"), "resolved_path": file.fs_path, "content": "Taglines."})

    host.extra_tools.update(generate_file=generate_file, read_file=read_file)
    seen = _model(monkeypatch, [
        ("", [_call("gen", "generate_file", {"kind": "md"})]),
        ("", [_call("read", "read_file", {"path": display})]),
        ("", [_call("done", "complete_task", _completion(deliverables=[
            {"title": "Taglines", "reference": file.fs_path},
        ]))]),
        lambda messages, _tools: pytest.fail(_tool_error(messages)["message"]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "completed", "task_id": task.id} and len(seen) == 3


def test_inspection_is_keyed_by_the_file_the_read_resolved_to():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
    from packages.core.workers.task_agent_loop_completion import InspectionTracker, file_keys

    storage = "Workspaces/_by_id/f1/launch-taglines.md"
    display = "Workspaces/Video Studio Eval R1/launch-taglines.md"
    ledger = RuntimeToolEvidence()
    tracker = InspectionTracker()

    def run(name, args, result):
        ledger.observe_tool_result(name, generate_ulid(), result, args)
        tracker.observe(name, result, args, ledger)

    run("generate_file", {"kind": "md"}, json.dumps({"document": {"document_id": "d1", "fs_path": storage}}))
    receipt_keys = file_keys(document_id="d1", fs_path=storage)
    assert not tracker.inspected_after_write(receipt_keys)
    # A display path the read resolved to the storage file inspects it.
    run("read_file", {"path": display}, json.dumps({
        "path": display, "resolved_path": storage, "content": "x",
    }))
    assert tracker.inspected_after_write(file_keys(fs_path=storage))
    # A document_id read with a different path argument inspects the
    # document it read, never the path it did not read.
    run("read_file", {"document_id": "d2", "path": "Reports/other.md"}, json.dumps({
        "path": "Reports/d2.md", "resolved_path": "Reports/d2.md", "document_id": "d2",
        "requested_path": "Reports/other.md", "content": "y",
    }))
    assert tracker.inspected_after_write(file_keys(document_id="d2"))
    assert tracker.inspected_after_write(file_keys(fs_path="Reports/d2.md"))
    assert "path:Reports/other.md" not in tracker.inspected


@pytest.mark.asyncio
async def test_failed_write_reports_blocked_in_the_same_wake(db_session, monkeypatch):
    """A write error reaches the model with its tools: it can report blocked
    through complete_task in the same wake, so no continuation is spent."""

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish(args):
        return json.dumps({"error": "gateway timeout after upload started"})

    host.extra_tools["publish_post"] = publish
    offered = []

    def report(messages, tools):
        offered.append([tool["function"]["name"] for tool in tools])
        assert "gateway timeout after upload started" in messages[-1]["content"]
        return "", [_call("done", "complete_task", _completion(
            outcome="blocked", blocked_reason="The post may or may not be live; please check.",
            acceptance=[_accept("The note names the launch date.", met=False)],
        ))]

    seen = _model(monkeypatch, [("", [_call("pub", "publish_post", {"text": "Launch!"})]), report])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": "agent_loop_reported_blocked"}
    assert "complete_task" in offered[0] and len(offered[0]) > 1 and len(seen) == 2
    assert [name for name, _args in host.tool_runs] == ["publish_post", "complete_task"]
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.checkpoint["non_submit_retries"] == 0
    assert row.checkpoint["tool_evidence"]["effect_state"] == "unconfirmed"
    assert (await _fresh(db_session, Task, task.id)).status == "blocked"


@pytest.mark.asyncio
async def test_failed_write_then_completed_claim_is_refused_in_the_lane(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish(args):
        return json.dumps({"error": "gateway timeout after upload started"})

    host.extra_tools["publish_post"] = publish
    refusals = []

    def refused(messages, _tools):
        refusals.append(_tool_error(messages)["error"])
        return "", [_call("again", "complete_task", _completion())]

    seen = _model(monkeypatch, [
        ("", [_call("pub", "publish_post", {"text": "Launch!"})]),
        ("", [_call("early", "complete_task", _completion())]),
        refused,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": "agent_loop_completed_non_success"}
    assert refusals == ["unconfirmed_effects"] and len(seen) == 3
    assert (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["non_submit_retries"] == 0


@pytest.mark.asyncio
async def test_unconfirmed_ledger_and_refusal_survive_a_restage(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish(args):
        return json.dumps({"status": "partial"})

    host.extra_tools["publish_post"] = publish
    refusals = []

    def refused(messages, _tools):
        refusals.append(_tool_error(messages)["error"])
        return "I will check the post first.", []

    seen = _model(monkeypatch, [
        ("", [_call("pub", "publish_post", {"text": "Launch!"})]),
        ("", [_call("early", "complete_task", _completion())]),
        refused,
        ("", [_call("again", "complete_task", _completion())]),
    ])
    first = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert first["status"] == "retry_scheduled"
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.checkpoint["tool_evidence"]["effect_state"] == "unconfirmed"
    assert row.checkpoint["unconfirmed_refusal"] == ["pub"]
    second = await worker.run_task_agent_loop_once(
        event_id=(await _event(db_session, task.id)).id, task_id=task.id,
        wake_generation=row.wake_generation,
    )
    # The refusal was already shown in the earlier wake: not refused again.
    assert second == {"status": "blocked", "task_id": task.id, "code": "agent_loop_completed_non_success"}
    assert refusals == ["unconfirmed_effects"] and len(seen) == 4


# ── Phase A2: the plan-free terminal writer ────────────────────────────


def _terminal_spies(monkeypatch, host):
    """Capture the post-commit task event and the in-transaction side effects."""

    from packages.core.ledger import adapters
    from packages.core.plans.executor import PlanExecutor
    from packages.core.services import workspace_operation_service

    spies = SimpleNamespace(events=[], artifacts=[], batches=[])

    async def emit(task_event):
        # Emitted after the terminal commit: a fresh session already sees it.
        async with host.session_factory() as db:
            status = (await db.execute(
                select(Task.status).where(Task.id == task_event["payload"]["task_id"])
            )).scalar_one()
        spies.events.append((task_event, status))

    real_record = adapters.record_task_artifacts

    async def record_task_artifacts(db, task, refs):
        spies.artifacts.append([dict(ref) for ref in refs or []])
        return await real_record(db, task, refs)

    async def check_work_batch_completion(db, task, *, trigger_source):
        spies.batches.append((task.id, task.status, trigger_source))

    monkeypatch.setattr(PlanExecutor, "_emit_task_event", staticmethod(emit))
    monkeypatch.setattr(adapters, "record_task_artifacts", record_task_artifacts)
    monkeypatch.setattr(
        workspace_operation_service, "check_work_batch_completion", check_work_batch_completion,
    )
    return spies


@pytest.mark.asyncio
async def test_completed_task_writes_every_terminal_side_effect(db_session, monkeypatch, workspace_files):
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.services.scheduled_task_terminal_delivery import scheduled_task_terminal_dedupe_key

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    scheduled_run_id = generate_ulid()
    row = await _fresh(db_session, Task, task.id)
    row.details = {"scheduled_run_id": scheduled_run_id}
    await db_session.commit()
    host = _install_host(monkeypatch, db_session, selection)
    file = await workspace_files(db_session, task, "Reports/launch-note.md")
    brief = await workspace_files(db_session, task, "Briefs/launch-brief.md", "The brief.\n")
    _read_tool(host)

    async def generate_file(args):
        return json.dumps({"document": {"document_id": file.document_id, "fs_path": file.fs_path}})

    host.extra_tools["generate_file"] = generate_file
    spies = _terminal_spies(monkeypatch, host)
    _model(monkeypatch, [
        ("", [_call("gen", "generate_file", {"kind": "md"})]),
        ("", [_call("read", "read_file", {"path": file.fs_path})]),
        ("", [_call("read2", "read_file", {"path": brief.fs_path})]),
        ("", [_call("done", "complete_task", _completion(deliverables=[
            {"title": "Launch note", "reference": file.fs_path},
            # Existed before this run: delivered by reference, not created.
            {"title": "Brief", "reference": brief.fs_path},
        ]))]),
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome == {"status": "completed", "task_id": task.id}
    task = await _fresh(db_session, Task, task.id)
    assert task.status == "completed"
    files = task.actual_output["files"]
    assert [(f["document_id"], f["fs_path"], f["open_url"], f["origin"]) for f in files] == [
        (file.document_id, file.fs_path, f"/viewer/{file.document_id}", "created"),
        (brief.document_id, brief.fs_path, f"/viewer/{brief.document_id}", "referenced"),
    ]
    # Only what this run produced is an artifact_created of this Task.
    assert spies.artifacts == [[files[0]]]
    logs = {
        log.log_type: log for log in (await db_session.execute(
            select(TaskLog).where(TaskLog.task_id == task.id)
        )).scalars().all()
    }
    terminal = logs["ai_execution_completed"]
    assert terminal.content == "Recorded the launch note."
    assert terminal.meta["files"] == files and terminal.meta["duration_s"] >= 0
    comment = logs["comment"]
    assert comment.content.startswith("## Task Completed\n\nRecorded the launch note.")
    assert f"/viewer/{file.document_id}" in comment.content
    assert comment.meta["auto_summary"] is True
    [(event, committed_status)] = spies.events
    assert event["event_type"] == "task.succeeded" and committed_status == "completed"
    assert event["payload"]["task_id"] == task.id
    assert event["payload"]["task_status"] == "completed"
    assert event["payload"]["engine"] == "agent_loop"
    assert "plan_id" not in event["payload"]
    assert spies.batches == [(task.id, "completed", "workers.task_agent_loop")]
    staged = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.dedupe_key == scheduled_task_terminal_dedupe_key(scheduled_run_id, task.id),
    ))).scalar_one()
    assert staged.payload["task_id"] == task.id
    evidence = (await db_session.execute(select(RuntimeEvidence).where(
        RuntimeEvidence.task_id == task.id, RuntimeEvidence.evidence_type == "task_run",
    ))).scalar_one()
    assert evidence.status == "succeeded"
    assert evidence.trace_id is None, "no Plan is faked"
    assert evidence.metrics["artifact_count"] == 2


@pytest.mark.asyncio
async def test_non_file_effects_are_listed_in_the_output_and_the_comment(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def manor(args):
        return json.dumps({"created": True, "task": {"id": "t-stray", "title": "Stray"}})

    async def invoke_skill(args):
        return json.dumps({"ok": True, "result": "skill done"})

    async def delegate(args):
        return json.dumps({"delegated": True, "status": "completed"})

    async def publish_site(args):
        return json.dumps({"published": True, "url": "https://launch.example.test", "site_id": "s1"})

    host.extra_tools.update(
        manor=manor, invoke_skill=invoke_skill, delegate_to_agent=delegate, publish_site=publish_site,
    )
    def create(title):
        return {"action": "workspace", "params": {"action": "create_task", "title": title}}

    _model(monkeypatch, [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("mk1", "manor", create("Stray")), _call("mk2", "manor", create("Other"))]),
        ("", [_call("skill", "invoke_skill", {"skill": "x"})]),
        ("", [_call("dl", "delegate_to_agent", {"tasks": [{"agent": "writer", "goal": "x"}]})]),
        ("", [_call("pub", "publish_site", {"path": "Sites/launch"})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome == {"status": "completed", "task_id": task.id}
    output = (await _fresh(db_session, Task, task.id)).actual_output
    effects = {effect["action_key"]: effect for effect in output["effects"]}
    # Plan/complete (control), the skill (orchestrator) and the delegation
    # (work inside this Task) are not side effects of their own.
    assert set(effects) == {"workspace.task.create", "workspace.site.publish"}
    assert effects["workspace.task.create"]["count"] == 2
    assert effects["workspace.task.create"]["title"] == "create workspace task"
    assert effects["workspace.site.publish"]["targets"] == ["https://launch.example.test"]
    assert "effects_omitted" not in output
    comment = (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "comment",
    ))).scalar_one()
    assert comment.content.startswith("## Task Completed\n\nRecorded the launch note.")
    other = comment.content.split("### Other changes\n", 1)[1]
    assert "- Created a Task (2)" in other
    assert "https://launch.example.test" in other
    for absent in ("update_task_plan", "complete_task", "invoke_skill", "delegate"):
        assert absent not in other


@pytest.mark.asyncio
async def test_delegated_child_confirmed_action_completes_and_is_listed_once(db_session, monkeypatch):
    """A delegation hands its child's ledger through the real run-prefixed publisher."""

    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
    from packages.core.ai.runtime.workspace_delegation_actions import _publish_delegated_child_ledger

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def delegate(args):
        child = RuntimeToolEvidence()
        child.observe_tool_result(
            "manor", "c1", json.dumps({"created": True, "task": {"id": "t-c", "title": "Child made"}}),
            {"action": "workspace", "params": {"action": "create_task", "title": "Child made"}},
        )
        assert _publish_delegated_child_ledger("run-1", SimpleNamespace(tool_evidence=child.checkpoint_state()))
        return json.dumps({"status": "completed", "answer": "Done."})

    host.extra_tools["delegate_to_agent"] = delegate
    seen = _model(monkeypatch, [
        ("", [_call("dl", "delegate_to_agent", {"tasks": [{"agent": "writer", "goal": "x"}]})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
        lambda messages, _tools: pytest.fail(_tool_error(messages)["message"]),
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    # Same as before the effects ledger: a confirmed child effect never
    # leaves the parent unconfirmed.
    assert outcome == {"status": "completed", "task_id": task.id} and len(seen) == 2
    output = (await _fresh(db_session, Task, task.id)).actual_output
    assert output["outcome"]["effect_state"] == "confirmed"
    assert [(e["action_key"], e["count"]) for e in output["effects"]] == [("workspace.task.create", 1)]


def test_output_receipts_keep_external_identity_and_revision():
    from packages.core.workers.task_agent_loop_completion import output_receipts

    [receipt] = output_receipts({"receipts": [{
        "handle": "r1", "tool": "code_project", "kind": "code_project", "operation": "commit",
        "external_id": "proj-1", "revision": "abc123", "effect_state": "confirmed",
    }]})
    assert receipt["external_id"] == "proj-1" and receipt["revision"] == "abc123"


@pytest.mark.parametrize(("locale", "created", "heading"), [
    ("en", "- Created a Task (2): Stray", "### Other changes"),
    ("zh", "- 创建了任务 (2): Stray", "### 其他变更"),
    ("de", "- Aufgabe erstellt (2): Stray", "### Weitere Änderungen"),
])
def test_comment_lists_people_facing_effects_in_the_reader_s_language(locale, created, heading):
    body = worker._comment_body({
        "summary": "Done.", "accepted": True,
        "effects": [
            {"action_key": "cli.exec", "title": "exec cli", "resource_kind": "cli", "count": 300},
            {"action_key": "sandbox.file.modify", "title": "modify sandbox file",
             "resource_kind": "sandbox_file", "count": 4},
            {"action_key": "workspace.task.create", "title": "create workspace task",
             "resource_kind": "workspace_task", "count": 2, "targets": ["Stray"]},
            {"action_key": "github.create_issue", "title": "create issue github",
             "resource_kind": "external_account", "count": 1},
        ],
    }, [], locale=locale)
    changes = body.split(f"{heading}\n", 1)[1].splitlines()
    # Workspace-internal compute stays in actual_output, out of the comment.
    assert changes == [created, "- Create issue github"]


def test_ledger_work_so_far_lists_each_confirmed_file_once_and_is_empty_without_work():
    from packages.core.workers.task_agent_loop_completion import ledger_work_so_far

    assert ledger_work_so_far({"receipts": [], "effects": []}) == {}
    assert ledger_work_so_far(None) == {}
    work = ledger_work_so_far({"receipts": [
        {"handle": "r1", "document_id": "d1", "fs_path": "a.md", "effect_state": "confirmed"},
        {"handle": "r2", "fs_path": "b.md", "effect_state": "unconfirmed"},
        {"handle": "r3", "url": "https://x.test", "effect_state": "confirmed"},
        {"handle": "r4", "document_id": "d1", "fs_path": "a.md", "name": "A", "effect_state": "confirmed"},
    ]})
    assert work["ledger_files"] == [{"document_id": "d1", "fs_path": "a.md", "name": "A"}]
    assert work["effects"] == [] and work["effects_omitted"] == 0


def test_output_effects_group_by_action_and_cap():
    from packages.core.workers.task_agent_loop_completion import output_effects

    state = {"effects": [
        {"action_key": "cli.exec", "resource_kind": "cli", "operation": "execute", "count": 300},
        {"action_key": "sandbox.file.modify", "resource_kind": "sandbox_file", "operation": "modify", "count": 4},
        {"action_key": "workspace.task.create", "resource_kind": "workspace_task", "operation": "create",
         "count": 1, "targets": ["Stray"]},
    ] + [
        {"action_key": f"svc.k{i}.send", "resource_kind": "external_account", "operation": "send", "count": 2}
        for i in range(22)
    ] + [{"call_id": "bad"}, "junk"], "effects_omitted": 3}
    effects, omitted = output_effects(state)
    assert len(effects) == 20
    # Workspace-internal compute (cli/sandbox) never pushes a created Task or a
    # sent message out of the shown effects: they come last.
    keys = [effect["action_key"] for effect in effects]
    assert keys[:3] == ["workspace.task.create", "svc.k0.send", "svc.k1.send"]
    assert "cli.exec" not in keys and "sandbox.file.modify" not in keys
    assert len([k for k in keys if k.startswith("svc.")]) == 19
    created = next(effect for effect in effects if effect["action_key"] == "workspace.task.create")
    assert created == {
        "action_key": "workspace.task.create", "title": "create workspace task",
        "resource_kind": "workspace_task", "count": 1, "targets": ["Stray"],
    }
    # Dropped: 3 svc groups (6 calls), cli (300) and sandbox (4), plus 3 never kept.
    assert omitted == 6 + 300 + 4 + 3
    assert output_effects(None) == ([], 0)


def test_low_signal_effects_are_shown_last_when_there_is_room():
    from packages.core.workers.task_agent_loop_completion import output_effects

    effects, omitted = output_effects({"effects": [
        {"action_key": "cli.exec", "resource_kind": "cli", "count": 9},
        {"action_key": "external_message.send", "resource_kind": "external_account", "count": 1},
    ]})
    assert [(e["action_key"], e["title"], e["count"]) for e in effects] == [
        ("external_message.send", "send external message", 1), ("cli.exec", "exec cli", 9),
    ]
    assert omitted == 0


@pytest.mark.asyncio
async def test_completed_non_success_and_internal_stops_emit_task_failed(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    spies = _terminal_spies(monkeypatch, host)

    async def publish(args):
        return json.dumps({"status": "partial"})

    host.extra_tools["publish_post"] = publish
    _model(monkeypatch, [
        ("", [_call("pub", "publish_post", {"text": "Launch!"})]),
        ("", [_call("c1", "complete_task", _completion())]),
        ("", [_call("c2", "complete_task", _completion())]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["code"] == "agent_loop_completed_non_success"
    [(event, committed_status)] = spies.events
    assert event["event_type"] == "task.failed" and committed_status == "blocked"
    assert event["payload"]["error_type"] == "agent_loop_completed_non_success"
    assert "publish_post" in event["payload"]["issue"]
    comment = (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "comment",
    ))).scalar_one()
    assert comment.content.startswith("## Work so far (not accepted as finished)")
    # BLOCKED is not terminal: no work batch evaluation, as in _finalize.
    assert spies.batches == []

    other, _run, other_selection, other_event, other_generation = await _agent_loop_run(db_session, monkeypatch)
    other_host = _install_host(monkeypatch, db_session, other_selection)
    other_spies = _terminal_spies(monkeypatch, other_host)
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", _stopped_loop("credit_exhausted"))
    stopped = await worker.run_task_agent_loop_once(
        event_id=other_event, task_id=other.id, wake_generation=other_generation,
    )
    assert stopped["code"] == "agent_loop_credit_exhausted"
    [(event, _status)] = other_spies.events
    assert event["event_type"] == "task.failed"
    assert event["payload"]["error_type"] == "agent_loop_credit_exhausted"
    assert not (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == other.id, TaskLog.log_type == "comment",
    ))).scalars().all()


@pytest.mark.asyncio
async def test_stale_terminal_write_emits_nothing(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    spies = _terminal_spies(monkeypatch, host)

    def superseded(_messages, _tools):
        return "", [_call("done", "complete_task", _completion())]

    async def steal(*_args, **_kwargs):
        async with host.session_factory() as db:
            row = await db.get(TaskCoordinatorRun, run.id)
            row.claim_token = "someone-else"
            await db.commit()
        return True

    _model(monkeypatch, [superseded])
    real_checkpoint = worker.checkpoint_task_coordinator_run

    async def checkpoint_then_steal(db, **kwargs):
        version = await real_checkpoint(db, **kwargs)
        if (kwargs.get("checkpoint") or {}).get("task_submission"):
            await db.commit()
            await steal()
        return version

    monkeypatch.setattr(worker, "checkpoint_task_coordinator_run", checkpoint_then_steal)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["status"] == "stale"
    assert spies.events == [] and spies.artifacts == [] and spies.batches == []
    assert (await _fresh(db_session, Task, task.id)).status == "in_progress"


@pytest.mark.asyncio
async def test_guessed_handle_of_a_non_file_receipt_is_not_a_deliverable(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)

    async def publish_site(args):
        return json.dumps({"published": True, "url": "https://launch.example.test", "site_id": "s1"})

    host.extra_tools["publish_site"] = publish_site

    def refused(messages, _tools):
        message = _tool_error(messages)["message"]
        assert message.startswith("deliverable 1 'r1' is not an existing, readable file"), message
        assert NON_FILE_ADVICE in message
        # The observed URL itself is still a real (non-file) deliverable.
        return "", [_call("done", "complete_task", _completion(deliverables=[
            {"title": "Launch site", "reference": "https://launch.example.test"},
        ]))]

    seen = _model(monkeypatch, [
        ("", [_call("pub", "publish_site", {"path": "Sites/launch"})]),
        ("", [_call("early", "complete_task", _completion(deliverables=[
            {"title": "Launch site", "reference": "r1"},
        ]))]),
        refused,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "completed", "task_id": task.id} and len(seen) == 3
    assert (await _fresh(db_session, Task, task.id)).actual_output["files"] == []


def test_handles_are_not_advertised_until_they_are_model_visible():
    schema = json.dumps(worker.COMPLETE_TASK_TOOL)
    prompt = worker._task_section("brief", can_delegate=False)
    for text in (schema, prompt):
        assert "handle" not in text and "r<n>" not in text and "receipt" not in text
    assert (
        "A sent message, an external link or a record is not a deliverable file: describe it "
        "in summary or in the acceptance evidence."
    ) in prompt


@pytest.mark.asyncio
async def test_deliverable_wording_asks_for_the_document_id_or_the_returned_fs_path(db_session, monkeypatch):
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
    from packages.core.services import artifact_delivery
    from packages.core.workers.task_agent_loop_completion import resolve_deliverables

    real_verify = artifact_delivery.verify_artifact_delivery_refs

    async def verify(db, **kwargs):
        if kwargs["refs"][0].get("fs_path") == "Reports/gone.md":
            # The receipt's file was deleted (not merely being rewritten).
            raise artifact_delivery.ArtifactDeliveryError("artifact_not_found", "The artifact is gone.")
        return await real_verify(db, **kwargs)

    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)

    wording = "Document id, or the exact fs_path a tool result returned"
    reference = worker.COMPLETE_TASK_TOOL["function"]["parameters"]["properties"]["deliverables"]
    assert wording in reference["items"]["properties"]["reference"]["description"]
    assert wording in " ".join(worker._task_section("brief", can_delegate=False).split())
    # The refusals the host actually returns: no reference, a receipt whose
    # file is gone, and a path that is not a readable file.
    evidence = RuntimeToolEvidence()
    evidence.observe_tool_result(
        "generate_file", "gen",
        json.dumps({"document": {"document_id": generate_ulid(), "fs_path": "Reports/gone.md"}}),
        {"kind": "md"},
    )
    resolved, unresolved = await resolve_deliverables(
        db_session,
        deliverables=[
            {"title": "No reference"},
            {"title": "Gone", "reference": "Reports/gone.md"},
            {"title": "Display path", "reference": "Workspaces/Launch/missing.md"},
        ],
        evidence=evidence, entity_id=generate_ulid(), workspace_id=generate_ulid(), user_id=None,
    )
    assert resolved == []
    reasons = [item["reason"] for item in unresolved]
    assert [item["index"] for item in unresolved] == [1, 2, 3]
    for reason in reasons:
        assert wording in reason, reason
        assert "Workspace path or Document id" not in reason


@pytest.mark.parametrize("can_delegate", [True, False])
def test_prompt_explains_create_task_without_banning_it(can_delegate):
    prompt = worker._task_section("brief", can_delegate=can_delegate)
    line = (
        "- create_task creates a separate Task that is not part of this run and does no "
        "work here"
    )
    assert line in prompt
    assert ("delegate_to_agent runs work inside this Task" in prompt) is can_delegate
    lowered = prompt.lower()
    assert "never call create_task" not in lowered and "do not call create_task" not in lowered


@pytest.mark.asyncio
async def test_blocked_report_in_the_error_lane_accepts_partial_unread_files(db_session, monkeypatch, workspace_files):
    """Reads are refused in the lane, so a blocked report needs no inspection."""

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    file = await workspace_files(db_session, task, "Reports/draft.md", "Half a draft.\n")

    async def generate_file(args):
        return json.dumps({"document": {"document_id": file.document_id, "fs_path": file.fs_path}})

    async def publish(args):
        return json.dumps({"error": "gateway timeout after upload started"})

    host.extra_tools.update(generate_file=generate_file, publish_post=publish)
    seen = _model(monkeypatch, [
        ("", [_call("gen", "generate_file", {"kind": "md", "content": "Half a draft."})]),
        ("", [_call("pub", "publish_post", {"text": "Launch!"})]),
        ("", [_call("done", "complete_task", _completion(
            outcome="blocked", blocked_reason="Publishing failed midway; check the post.",
            acceptance=[_accept("The note names the launch date.", met=False)],
            deliverables=[
                {"title": "Draft", "reference": file.fs_path},
                {"title": "Final", "reference": "Reports/final.md"},
            ],
        ))]),
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome == {"status": "blocked", "task_id": task.id, "code": "agent_loop_reported_blocked"}
    assert len(seen) == 3
    task = await _fresh(db_session, Task, task.id)
    assert [f["fs_path"] for f in task.actual_output["files"]] == [file.fs_path]
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.checkpoint["non_submit_retries"] == 0
    [unverified] = row.checkpoint["coordinator_outcome"]["details"]["unverified_deliverables"]
    assert unverified["index"] == 2 and unverified["reference"] == "Reports/final.md"
    assert "not an existing, readable file" in unverified["reason"]
    # Dropped deliverables are listed only as unverified, nowhere as delivered.
    output = task.actual_output
    for listed in (output["deliverables"], output["result"]["deliverables"],
                   row.checkpoint["coordinator_outcome"]["details"]["submission"]["deliverables"]):
        assert [d["reference"] for d in listed] == [file.fs_path]
    # The person sees why it stopped and what exists, never as completed.
    [comment] = (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "comment",
    ))).scalars().all()
    assert comment.content.startswith(
        "## Work so far (not accepted as finished)\n\n"
        "Publishing failed midway; check the post.\n\nRecorded the launch note."
    )
    assert f"/viewer/{file.document_id}" in comment.content
    assert "Reports/final.md" not in comment.content


@pytest.mark.asyncio
async def test_host_forced_block_posts_the_confirmed_work_so_far(db_session, monkeypatch, workspace_files):
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    file = await workspace_files(db_session, task, "Reports/draft.md", "Half a draft.\n")

    async def loop(**kwargs):
        evidence = kwargs["tool_evidence"]
        assert isinstance(evidence, RuntimeToolEvidence)
        evidence.observe_tool_result(
            "generate_file", "gen",
            json.dumps({"document": {"document_id": file.document_id, "fs_path": file.fs_path}}),
            {"kind": "md"},
        )
        evidence.observe_tool_result(
            "generate_file", "failed", json.dumps({"error": "disk full"}), {"kind": "md"},
        )
        evidence.observe_tool_result(
            "manor", "mk", json.dumps({"created": True, "task": {"id": "t-stray"}}),
            {"action": "workspace", "params": {"action": "create_task", "title": "Stray"}},
        )
        return await _stopped_loop("credit_exhausted")(**kwargs)

    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", loop)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome["code"] == "agent_loop_credit_exhausted"
    [comment] = (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "comment",
    ))).scalars().all()
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    message = row.checkpoint["coordinator_outcome"]["message"]
    # The internal stop message is in the failure log, not the comment.
    assert message not in comment.content
    assert comment.content.startswith(
        "## Work so far (not accepted as finished)\n\n"
        "The Task stopped before it was finished. What it produced so far:"
    )
    assert f"/viewer/{file.document_id}" in comment.content
    assert "### Other changes\n- Created a Task" in comment.content
    # A host-forced stop records no delivery: the Task's output is untouched.
    assert (await _fresh(db_session, Task, task.id)).actual_output in (None, {})
    # Re-blocking the preserved checkpoint with the same work posts nothing new.
    assert worker._host_stop_comment(row.checkpoint) is None


def test_host_stop_with_only_workspace_internal_compute_posts_nothing():
    cli = {"action_key": "cli.exec", "resource_kind": "cli", "operation": "execute", "count": 9}
    sandbox = {"action_key": "sandbox.exec", "resource_kind": "sandbox", "count": 2}
    assert worker._host_stop_comment({"tool_evidence": {"receipts": [], "effects": [cli, sandbox]}}) is None
    # Anything a person would see still posts.
    shown = worker._host_stop_comment({"tool_evidence": {"receipts": [], "effects": [
        cli, {"action_key": "workspace.task.create", "resource_kind": "workspace_task", "count": 1},
    ]}})
    assert shown is not None
    assert worker._host_stop_comment({"tool_evidence": {
        "receipts": [], "effects": [cli], "effects_omitted": 2,
    }}) is not None


@pytest.mark.asyncio
async def test_sandbox_only_host_stop_posts_no_comment(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)

    async def loop(**kwargs):
        kwargs["tool_evidence"].observe_tool_result(
            "bash", "sh", json.dumps({"ok": True, "stdout": "built"}), {"command": "make"},
        )
        return await _stopped_loop("credit_exhausted")(**kwargs)

    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", loop)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome["code"] == "agent_loop_credit_exhausted"
    assert not (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "comment",
    ))).scalars().all()
    output_effects = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["tool_evidence"]["effects"]
    assert [effect["action_key"] for effect in output_effects] == ["cli.exec"]


@pytest.mark.asyncio
async def test_retried_run_re_blocked_with_no_new_work_posts_one_comment(db_session, monkeypatch, workspace_files):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(worker, "_NON_SUBMIT_RETRIES", 0)
    file = await workspace_files(db_session, task, "Reports/draft.md", "Half a draft.\n")

    async def generate_file(args):
        return json.dumps({"document": {"document_id": file.document_id, "fs_path": file.fs_path}})

    host.extra_tools["generate_file"] = generate_file
    _read_tool(host)
    _model(monkeypatch, [
        ("", [_call("gen", "generate_file", {"kind": "md"})]),
        ("I will stop here.", []),
    ])
    first = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert first["code"] == "agent_loop_completion_missing"

    # Manual retry: read-only rounds, then the same stop with no new work.
    task = await _fresh(db_session, Task, task.id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.commit()
    event = await _event(db_session, task.id)
    _model(monkeypatch, [
        ("", [_call("r1", "read_file", {"path": file.fs_path})]),
        ("", [_call("r2", "read_file", {"path": file.fs_path})]),
        ("Still stopping.", []),
    ])
    second = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=run.wake_generation,
    )
    assert second["code"] == "agent_loop_completion_missing"

    comments = (await db_session.execute(select(TaskLog).where(
        TaskLog.task_id == task.id, TaskLog.log_type == "comment",
    ))).scalars().all()
    assert len(comments) == 1 and f"/viewer/{file.document_id}" in comments[0].content


def test_host_stop_comment_is_posted_once_per_distinct_work_list():
    evidence = {"receipts": [
        {"handle": "r1", "document_id": "d1", "fs_path": "a.md", "effect_state": "confirmed"},
    ], "effects": []}
    first = worker._host_stop_comment({"tool_evidence": evidence})
    assert first is not None
    assert first["comment"]["ledger_files"] == [{"document_id": "d1", "fs_path": "a.md"}]
    assert first["comment"]["accepted"] is False and "summary" not in first["comment"]
    posted = {"tool_evidence": evidence, **first["checkpoint"]}
    assert worker._host_stop_comment(posted) is None
    more = {**posted, "tool_evidence": {**evidence, "receipts": evidence["receipts"] + [
        {"handle": "r2", "document_id": "d2", "fs_path": "b.md", "effect_state": "confirmed"},
    ]}}
    assert worker._host_stop_comment(more) is not None
    assert worker._host_stop_comment({"tool_evidence": {"receipts": [], "effects": []}}) is None


@pytest.mark.asyncio
async def test_receipt_for_a_file_that_is_not_available_is_refused(db_session, monkeypatch, workspace_files):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    # A real Workspace whose Knowledge does not contain the receipt's Document.
    await workspace_files(db_session, task, "Reports/other.md")
    _read_tool(host)

    async def generate_file(args):
        # A receipt whose Document does not exist (or is no longer readable).
        return json.dumps({"document": {"document_id": generate_ulid(), "fs_path": "Reports/gone.md"}})

    host.extra_tools["generate_file"] = generate_file

    def refused(messages, _tools):
        message = _tool_error(messages)["message"]
        assert message.startswith("deliverable 1 'Reports/gone.md' is not an existing, readable file"), message
        return "", [_call("done", "complete_task", _completion())]

    seen = _model(monkeypatch, [
        ("", [_call("gen", "generate_file", {"kind": "md"})]),
        ("", [_call("read", "read_file", {"path": "Reports/gone.md"})]),
        ("", [_call("early", "complete_task", _completion(deliverables=[
            {"title": "Gone", "reference": "Reports/gone.md"},
        ]))]),
        refused,
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "completed"
    assert len(seen) == 4


@pytest.mark.parametrize(("code", "accepted"), [
    ("artifact_busy", True), ("artifact_changed", False), ("artifact_unavailable", False),
    ("artifact_identity_invalid", False),
])
def test_receipt_route_falls_back_only_while_the_file_is_in_flux(monkeypatch, code, accepted):
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
    from packages.core.services import artifact_delivery
    from packages.core.workers.task_agent_loop_completion import resolve_deliverables

    ledger = RuntimeToolEvidence()
    ledger.observe_tool_result("generate_file", "c1", json.dumps({
        "document": {"document_id": "doc1", "fs_path": "Reports/a.md"},
    }), {"kind": "md"})

    async def verify(*_args, **_kwargs):
        raise artifact_delivery.ArtifactDeliveryError(code, "nope")

    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    resolved, unresolved = asyncio.run(resolve_deliverables(
        None, deliverables=[{"title": "A", "reference": "Reports/a.md"}], evidence=ledger,
        entity_id="e", workspace_id="w", user_id="u",
    ))
    if accepted:
        assert unresolved == [] and resolved[0]["file_ref"]["document_id"] == "doc1"
    elif code == "artifact_changed":
        # Can be permanent (bytes gone, not a regular file): never a fallback.
        assert resolved == [] and unresolved[0]["reason"] == (
            "The file Reports/a.md changed or could not be read just now; read it again "
            "and call complete_task again."
        )
    else:
        assert resolved == [] and "not an existing, readable file" in unresolved[0]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "blocked"])
async def test_changed_receipt_file_is_refused_for_completed_and_dropped_for_blocked(
    db_session, monkeypatch, outcome,
):
    from packages.core.services import artifact_delivery

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    _read_tool(host)

    async def generate_file(args):
        return json.dumps({"document": {"document_id": generate_ulid(), "fs_path": "Reports/a.md"}})

    async def verify(*_args, **_kwargs):
        raise artifact_delivery.ArtifactDeliveryError("artifact_changed", "Artifact bytes are unavailable.")

    host.extra_tools["generate_file"] = generate_file
    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    deliverables = [{"title": "A", "reference": "Reports/a.md"}]
    extra = (
        {"blocked_reason": "Stopped early.", "acceptance": [_accept("The note names the launch date.", met=False)]}
        if outcome == "blocked" else {}
    )
    refusals = []

    def refused(messages, _tools):
        refusals.append(_tool_error(messages)["message"])
        return "", [_call("done", "complete_task", _completion())]

    _model(monkeypatch, [
        ("", [_call("gen", "generate_file", {"kind": "md"})]),
        ("", [_call("read", "read_file", {"path": "Reports/a.md"})]),
        ("", [_call("early", "complete_task", _completion(outcome=outcome, deliverables=deliverables, **extra))]),
        refused,
    ])
    result = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    task = await _fresh(db_session, Task, task.id)
    if outcome == "completed":
        assert refusals == [
            "The file Reports/a.md changed or could not be read just now; read it again and "
            "call complete_task again."
        ]
        assert result["status"] == "completed"
    else:
        assert refusals == [] and result["code"] == "agent_loop_reported_blocked"
        assert task.actual_output["files"] == []
        details = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["coordinator_outcome"]["details"]
        assert [u["reference"] for u in details["unverified_deliverables"]] == ["Reports/a.md"]


# ── B2: long-running Tasks park instead of holding a worker ─────────────


@pytest.mark.asyncio
async def test_media_jobs_started_in_the_loop_dispatch_through_celery(db_session, monkeypatch):
    from packages.core.ai.runtime.media_job_scope import runtime_media_jobs_detached
    from packages.core.tasks import media_tasks

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    queued: list[str] = []
    scopes: list = []
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr(media_tasks.process_video_job_task, "delay", lambda job_id: queued.append(job_id))

    async def must_not_run_inline(_job_id):
        raise AssertionError("an in-loop job would hold the worker after the Task parks")

    monkeypatch.setattr(media_tasks, "process_video_job", must_not_run_inline)

    async def start_video(_args):
        scopes.append(runtime_media_jobs_detached())
        media_tasks.schedule_video_job("job-in-task")
        return json.dumps({"status": "pending", "job_id": "job-in-task"})

    host.extra_tools["start_video"] = start_video
    host.context.return_value.tools = [NOTE_TOOL, {"type": "function", "function": {
        "name": "start_video", "parameters": {"type": "object", "properties": {}},
    }}]
    host.context.return_value.allowed_tool_names = {"record_note", "start_video"}
    _model(monkeypatch, [
        ("", [_call("video", "start_video", {})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome["status"] == "completed"
    assert scopes == [{"task_id": task.id, "run_id": run.id}]
    assert queued == ["job-in-task"]
    assert runtime_media_jobs_detached() is None


def _wait_calls(host, name):
    return [args for tool, args in host.tool_runs if tool == name]


async def _deliver(db_session, task_id):
    row = await _fresh(db_session, TaskCoordinatorRun, (await db_session.execute(
        select(TaskCoordinatorRun.id).where(TaskCoordinatorRun.task_id == task_id)
    )).scalar_one())
    event = await _event(db_session, task_id)
    outcome = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task_id, wake_generation=row.wake_generation,
    )
    return outcome, await _fresh(db_session, TaskCoordinatorRun, row.id)


@pytest.mark.asyncio
async def test_wait_until_parks_rewakes_early_and_resumes_at_its_time(db_session, monkeypatch):
    from packages.core.workers import task_agent_loop_waits as waits

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    clock = {"now": None}
    real_now = waits.utcnow
    monkeypatch.setattr(waits, "utcnow", lambda: clock["now"] or real_now())

    def after_wake(messages, tools):
        assert "wait_until" in {tool["function"]["name"] for tool in tools}
        woke = json.loads(next(m["content"] for m in messages if m.get("tool_call_id") == "wait"))
        assert woke["status"] == "woke" and woke["reason"] == "the supplier's reply"
        assert woke["late_seconds"] == 5
        assert 3600 <= woke["slept_seconds"] <= 3610
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [_call("wait", "wait_until", {"seconds": 3600, "reason": "the supplier's reply"})]),
        after_wake,
    ])
    before = datetime.now(timezone.utc)

    first = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert first["status"] == "waiting_timer"
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    wait = row.checkpoint["task_loop_wait"]
    wake_at = datetime.fromisoformat(wait["wake_at"])
    assert row.phase == TaskCoordinatorPhase.WAITING_TIMER.value
    assert row.claim_token is None
    assert wait["kind"] == "waiting_timer" and wait["tool_call_id"] == "wait"
    assert before + timedelta(seconds=3599) < wake_at < before + timedelta(seconds=3610)
    pending = row.checkpoint["runtime_checkpoint"]["pending_tool_call"]
    assert pending["name"] == "wait_until" and pending["arguments"]["at"] == wait["wake_at"]
    assert row.checkpoint["non_submit_retries"] == 0 and "wake_failures" not in row.checkpoint
    event = await _event(db_session, task.id)
    assert abs((event.available_at - wake_at).total_seconds()) < 1
    assert event.payload["reason"] == "task_agent_loop_waiting_timer"
    assert (await _fresh(db_session, Task, task.id)).status == "in_progress"
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id))).scalars().all()
    assert [log.content for log in logs if log.content.startswith("Waiting until")] == [
        f"Waiting until {wait['wake_at']}: the supplier's reply."
    ]

    # Another stage replaced the single wake row with an immediate one: the
    # wait re-parks at the same absolute time with no model call.
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="unrelated_stage")
    await db_session.commit()
    early, row = await _deliver(db_session, task.id)
    assert early["status"] == "waiting_timer" and len(seen) == 1
    assert row.phase == TaskCoordinatorPhase.WAITING_TIMER.value
    assert row.checkpoint["task_loop_wait"] == wait
    event = await _event(db_session, task.id)
    assert abs((event.available_at - wake_at).total_seconds()) < 1
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id))).scalars().all()
    assert len([log for log in logs if log.content.startswith("Waiting until")]) == 1

    # At (or after) its time the parked call returns before any model round.
    clock["now"] = wake_at + timedelta(seconds=5)
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="due")
    await db_session.commit()
    done, row = await _deliver(db_session, task.id)

    assert done == {"status": "completed", "task_id": task.id}
    assert len(seen) == 2
    assert host.loop_calls[-1]["runtime_checkpoint"]["pending_tool_call"]["name"] == "wait_until"
    assert "task_loop_wait" not in row.checkpoint and "runtime_checkpoint" not in row.checkpoint
    assert row.phase == TaskCoordinatorPhase.COMPLETED.value


@pytest.mark.asyncio
async def test_resumed_wait_returns_to_running_at_its_next_round(db_session, monkeypatch):
    from packages.core.workers import task_agent_loop_waits as waits

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    phases: list[str] = []

    async def record_note(args):
        row = await _fresh(db_session, TaskCoordinatorRun, run.id)
        phases.append((row.phase, "task_loop_wait" in row.checkpoint, "runtime_checkpoint" in row.checkpoint))
        return json.dumps({"ok": True})

    host.extra_tools["probe"] = record_note
    host.context.return_value.tools = [NOTE_TOOL, {"type": "function", "function": {
        "name": "probe", "parameters": {"type": "object", "properties": {}},
    }}]
    host.context.return_value.allowed_tool_names = {"record_note", "probe"}
    _model(monkeypatch, [
        ("", [_call("wait", "wait_until", {"seconds": 120, "reason": "later"})]),
        ("", [_call("p1", "probe", {})]),
        ("", [_call("p2", "probe", {})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_timer"
    monkeypatch.setattr(waits, "utcnow", lambda: datetime.now(timezone.utc) + timedelta(minutes=5))
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="due")
    await db_session.commit()
    done, row = await _deliver(db_session, task.id)
    assert done["status"] == "completed"
    # The resumed call's result is checkpointed before the next model round:
    # that checkpoint clears the wait and returns the run to RUNNING.
    assert phases == [(TaskCoordinatorPhase.RUNNING.value, False, False)] * 2


@pytest.mark.parametrize(("arguments", "error"), [
    ({"reason": "x"}, "Give at"),
    ({"at": "next tuesday", "reason": "x"}, "ISO 8601"),
    ({"at": "2026-10-12T09:00:00Z", "seconds": 5, "reason": "x"}, "not both"),
    ({"seconds": float("nan"), "reason": "x"}, "finite"),
])
def test_wait_until_rejects_unusable_times(arguments, error):
    from packages.core.workers import task_agent_loop_waits as waits

    target, message = waits.wait_until_target(arguments, datetime.now(timezone.utc))
    assert target is None and error in message


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"seconds": 1}, timedelta(seconds=60)),
    ({"seconds": 10 ** 12}, timedelta(days=90)),
    ({"at": "2020-01-01T00:00:00Z"}, timedelta(seconds=60)),
    ({"at": "2026-10-04T12:00:00"}, timedelta(hours=2)),
])
def test_wait_until_target_is_clamped_and_absolute(arguments, expected):
    from packages.core.workers import task_agent_loop_waits as waits

    now = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    target, error = waits.wait_until_target(arguments, now)
    assert error is None and target - now == expected


def test_wait_until_is_read_only_and_task_loop_only():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.ai.runtime.tool_registry import runtime_tool_schema
    from packages.core.ai.tool_pool import ToolPool

    assert classify_runtime_tool("wait_until", {"seconds": 60}).effect is RuntimeToolEffect.READ_ONLY
    pool = ToolPool()
    pool.initialize()
    # Chat never sees it: it is not a registered tool, only this loop's handler.
    assert "wait_until" not in dict(pool.registered_tool_schemas(include_undiscoverable=True))
    assert runtime_tool_schema("wait_until") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["editor", "manor"])
async def test_real_harness_authorizes_and_parks_wait_until(client, db_session, monkeypatch, root):
    env = await _real_workspace(client, db_session, f"agent_loop_wait_{root}")
    _flag(monkeypatch, True)
    monkeypatch.setattr(
        task_requester_identity, "resolve_task_execution_user_id",
        AsyncMock(return_value=env.user_id),
    )
    sub = env.subs[root]
    binding = TaskPlanServiceBinding(subscription_id=sub.id, service_key=sub.service_key or "owner", agent_id=sub.agent_id)
    selection = CoordinatorRuntimeSelection(root=binding, bindings=(binding,))
    task = await _fresh(db_session, Task, env.task.id)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db_session, task_id=task.id, reason="task_start")
    await db_session.commit()
    _install_real_harness(monkeypatch, db_session, selection)
    authorizations, _verified = _spy_authorization(monkeypatch)
    seen = _model(monkeypatch, [
        ("", [_call("wait", "wait_until", {"seconds": 600, "reason": "the review window"})]),
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=wake.event_id, task_id=task.id, wake_generation=wake.wake_generation,
    )

    assert outcome["status"] == "waiting_timer", outcome
    assert len(seen) == 1
    assert ("wait_until", "workspace_member", "read", True) in authorizations
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.phase == TaskCoordinatorPhase.WAITING_TIMER.value
    assert row.checkpoint["task_loop_wait"]["tool_call_id"] == "wait"


GENERATE_FILE_TOOL = {"type": "function", "function": {
    "name": "generate_file",
    "parameters": {"type": "object", "properties": {"kind": {"type": "string"}, "prompt": {"type": "string"}}},
}}


def _media_host(monkeypatch, db_session, selection, job_id):
    """A Task whose generate_file starts one video job (a real MediaJob row)."""

    from packages.core import database
    from packages.core.tasks import media_tasks

    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(database, "async_session", host.session_factory)
    for name in ("_bill_usage", "_push_notification", "_project_group_media_if_ready"):
        monkeypatch.setattr(media_tasks, name, AsyncMock(return_value=True))
    host.context.return_value.tools = [NOTE_TOOL, GENERATE_FILE_TOOL]
    host.context.return_value.allowed_tool_names = {"record_note", "generate_file", "wait_media_jobs"}

    async def generate_file(_args):
        return json.dumps({"kind": "video", "status": "pending", "job_id": job_id})

    host.extra_tools["generate_file"] = generate_file
    return host


async def _video_job(db_session, task, *, status="processing", task_id=None):
    from packages.core.models.media_job import MediaJob

    job = MediaJob(
        id=generate_ulid(), entity_id=task.entity_id, kind="video", status=status,
        prompt="launch teaser", params={"task_id": task_id or task.id},
    )
    db_session.add(job)
    await db_session.commit()
    return job


async def _finish_video(job_id, entity_id):
    from packages.core.tasks import media_tasks

    assert await media_tasks._finalize_video_job(job_id, {
        "result_url": f"/api/v1/fs/{entity_id}/videos/teaser.mp4",
        "source_url": "https://provider.test/teaser.mp4",
        "file_size": 2048,
    })


def _media_result(messages):
    return json.loads(next(
        m["content"] for m in messages
        if m.get("role") == "tool" and '"kind": "media_jobs"' in str(m.get("content"))
    ))


@pytest.mark.asyncio
async def test_pending_video_parks_on_its_job_and_the_job_wakes_the_task(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    host = _media_host(monkeypatch, db_session, selection, job.id)

    def after_job(messages, _tools):
        result = _media_result(messages)
        assert result["status"] == "completed" and result["timed_out"] is False
        assert result["jobs"][0]["job_id"] == job.id
        assert result["jobs"][0]["fs_path"] == "videos/teaser.mp4"
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [_call("video", "generate_file", {"kind": "video", "prompt": "teaser"})]),
        after_job,
    ])
    before = datetime.now(timezone.utc)

    parked = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert parked["status"] == "waiting_job"
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    wait = row.checkpoint["task_loop_wait"]
    assert row.phase == TaskCoordinatorPhase.WAITING_JOB.value and row.claim_token is None
    assert wait["kind"] == "waiting_job" and wait["job_ids"] == [job.id]
    deadline = datetime.fromisoformat(wait["deadline_at"])
    assert before + timedelta(minutes=59) < deadline < before + timedelta(minutes=61)
    # The forced wait after the pending generate_file is the parked call.
    assert row.checkpoint["runtime_checkpoint"]["pending_tool_call"]["name"] == "wait_media_jobs"
    event = await _event(db_session, task.id)
    # Fallback poll while the job runs; the job's completion wakes it sooner.
    assert before + timedelta(seconds=290) < event.available_at < before + timedelta(seconds=310)
    assert (await _fresh(db_session, Task, task.id)).status == "in_progress"
    logs = (await db_session.execute(select(TaskLog.content).where(TaskLog.task_id == task.id))).scalars().all()
    assert any(line.startswith("Waiting for 1 media job(s)") for line in logs)

    await _finish_video(job.id, task.entity_id)

    event = await _event(db_session, task.id)
    assert event.payload["reason"] == "media_jobs_settled"
    assert event.available_at <= datetime.now(timezone.utc)
    done, row = await _deliver(db_session, task.id)
    assert done == {"status": "completed", "task_id": task.id}
    assert len(seen) == 2
    final_task = await _fresh(db_session, Task, task.id)
    assert any(
        receipt.get("fs_path") == "videos/teaser.mp4" for receipt in final_task.actual_output["receipts"]
    ), final_task.actual_output["receipts"]
    assert "task_loop_wait" not in row.checkpoint


@pytest.mark.asyncio
async def test_job_finishing_before_the_park_commits_wakes_at_once(db_session, monkeypatch):
    from packages.core.ai.tools import media_tools

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    _media_host(monkeypatch, db_session, selection, job.id)
    real_read = media_tools._wait_media_jobs_handler

    async def read_then_job_finishes(**kwargs):
        raw = await real_read(**kwargs)
        # The job settles after the loop's read and before its park commits:
        # its hook finds no parked run and stages nothing.
        await _finish_video(job.id, task.entity_id)
        return raw

    monkeypatch.setattr(media_tools, "_wait_media_jobs_handler", read_then_job_finishes)
    _model(monkeypatch, [
        ("", [_call("video", "generate_file", {"kind": "video", "prompt": "teaser"})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
    ])
    parked = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert parked["status"] == "waiting_job"
    event = await _event(db_session, task.id)
    assert event.available_at <= datetime.now(timezone.utc)
    assert event.payload["reason"] == "task_agent_loop_waiting_job"

    monkeypatch.setattr(media_tools, "_wait_media_jobs_handler", real_read)
    done, _row = await _deliver(db_session, task.id)
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_job_wait_past_its_deadline_returns_timed_out(db_session, monkeypatch):
    from packages.core.workers import task_agent_loop_waits as waits

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    _media_host(monkeypatch, db_session, selection, job.id)

    def timed_out(messages, _tools):
        result = _media_result(messages)
        assert result["status"] == "pending" and result["timed_out"] is True
        assert result["pending_job_ids"] == [job.id]
        assert result["timeout_seconds"] == 3600
        return "", [_call("done", "complete_task", _completion(
            outcome="blocked", blocked_reason="The video job did not finish in time.",
        ))]

    seen = _model(monkeypatch, [
        ("", [_call("video", "generate_file", {"kind": "video", "prompt": "teaser"})]),
        timed_out,
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_job"
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    monkeypatch.setattr(waits, "utcnow", lambda: later)
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="poll")
    await db_session.commit()

    done, _row = await _deliver(db_session, task.id)

    assert done["status"] == "blocked" and len(seen) == 2


@pytest.mark.asyncio
async def test_loop_waits_only_on_its_own_tasks_jobs(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    mine = await _video_job(db_session, task, status="completed")
    other = await _video_job(db_session, task, task_id=generate_ulid())
    host = _media_host(monkeypatch, db_session, selection, mine.id)

    def checked(messages, _tools):
        result = _media_result(messages)
        assert [item["job_id"] for item in result["jobs"]] == [mine.id]
        assert result["missing_job_ids"] == [other.id] and result["total_count"] == 2
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    _model(monkeypatch, [
        ("", [_call("wait", "wait_media_jobs", {"job_ids": [mine.id, other.id]})]),
        checked,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    # The other Task's pending job neither parked this Task nor was read.
    assert outcome["status"] == "completed"
    assert [name for name, _ in host.tool_runs].count("wait_media_jobs") == 1


@pytest.mark.asyncio
async def test_media_job_hook_ignores_runs_not_parked_on_it(db_session, monkeypatch):
    from packages.core.services.task_coordinator_wakeup import wake_task_coordinator_for_media_job

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task, status="completed")
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    before = row.wake_generation
    # Not parked: no wake.
    assert await wake_task_coordinator_for_media_job(db_session, job) is None
    # Parked on another job: no wake.
    row.phase = TaskCoordinatorPhase.WAITING_JOB.value
    row.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_job", "job_ids": [generate_ulid()]}}
    await db_session.flush()
    assert await wake_task_coordinator_for_media_job(db_session, job) is None
    # Parked on this job and a still-running one: no wake yet.
    running = await _video_job(db_session, task)
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    row.phase = TaskCoordinatorPhase.WAITING_JOB.value
    row.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_job", "job_ids": [job.id, running.id]}}
    await db_session.flush()
    assert await wake_task_coordinator_for_media_job(db_session, job) is None
    assert (await _fresh(db_session, TaskCoordinatorRun, run.id)).wake_generation == before
    # Every listed job terminal: wake, and again idempotently.
    running.status = "failed"
    await db_session.flush()
    first = await wake_task_coordinator_for_media_job(db_session, job)
    second = await wake_task_coordinator_for_media_job(db_session, running)
    assert first is not None and second is not None
    await db_session.commit()
    assert (await _event(db_session, task.id)).payload["reason"] == "media_jobs_settled"


def test_chat_keeps_the_registered_wait_media_jobs():
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools import media_tools

    pool = ToolPool()
    pool.initialize()
    entry = pool.get("wait_media_jobs")
    assert entry is not None
    handler = entry.get("handler") if isinstance(entry, dict) else getattr(entry, "handler", None)
    assert handler is media_tools._wait_media_jobs_handler


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", ["succeeded", "failed"])
async def test_suno_job_terminal_paths_wake_the_parked_task(db_session, monkeypatch, finished):
    from packages.core import database
    from packages.core.models.media_job import MediaJob
    from packages.core.tasks import media_tasks

    task, run, selection, _event_id, _generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(database, "async_session", host.session_factory)
    monkeypatch.setattr(media_tasks, "_project_group_media_if_ready", AsyncMock())
    job = MediaJob(
        id=generate_ulid(), entity_id=task.entity_id, kind="audio", status="processing",
        prompt="theme", byok=True, params={"task_id": task.id, "provider": "suno"},
    )
    db_session.add(job)
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    row.phase = TaskCoordinatorPhase.WAITING_JOB.value
    row.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_job", "job_ids": [job.id]}}
    await db_session.commit()

    if finished == "succeeded":
        await media_tasks._finalize_suno_audio_success(job.id, {"result_url": "https://cdn.test/a.mp3"})
    else:
        await media_tasks._finalize_suno_audio_failure(job.id, "provider failed", release_reservation=False)

    assert (await _event(db_session, task.id)).payload["reason"] == "media_jobs_settled"
    assert (await _fresh(db_session, MediaJob, job.id)).status == (
        "completed" if finished == "succeeded" else "failed"
    )


async def _lose_wake(db_session, task_id):
    event = await _event(db_session, task_id)
    event.delivered_at = datetime.now(timezone.utc)
    await db_session.commit()


@pytest.mark.asyncio
async def test_recovery_sweep_restages_a_lost_timer_at_its_remaining_delay(db_session, monkeypatch):
    from packages.core import database
    from packages.core.tasks.runtime_tasks import recover_stalled_task_coordinator_wakes_once

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    _model(monkeypatch, [("", [_call("wait", "wait_until", {"seconds": 7200, "reason": "later"})])])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_timer"
    wake_at = datetime.fromisoformat(
        (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["task_loop_wait"]["wake_at"]
    )
    await _lose_wake(db_session, task.id)

    await recover_stalled_task_coordinator_wakes_once()

    event = await _event(db_session, task.id)
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert event.delivered_at is None and event.payload["wake_generation"] == row.wake_generation
    assert abs((event.available_at - wake_at).total_seconds()) < 2
    assert row.phase == TaskCoordinatorPhase.WAITING_TIMER.value
    # A pending (future) wake row is live: the sweep leaves it alone.
    generation_before = row.wake_generation
    await recover_stalled_task_coordinator_wakes_once()
    assert (await _fresh(db_session, TaskCoordinatorRun, run.id)).wake_generation == generation_before


@pytest.mark.asyncio
async def test_recovery_sweep_wakes_a_job_wait_only_when_its_jobs_settled(db_session, monkeypatch):
    from packages.core import database
    from packages.core.models.media_job import MediaJob
    from packages.core.tasks.runtime_tasks import recover_stalled_task_coordinator_wakes_once

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    host = _media_host(monkeypatch, db_session, selection, job.id)
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    _model(monkeypatch, [("", [_call("video", "generate_file", {"kind": "video"})])])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_job"

    await _lose_wake(db_session, task.id)
    before = datetime.now(timezone.utc)
    await recover_stalled_task_coordinator_wakes_once()
    event = await _event(db_session, task.id)
    assert event.delivered_at is None
    assert before + timedelta(seconds=290) < event.available_at < before + timedelta(seconds=310)

    # The job settled but its completion wake was lost too.
    await _lose_wake(db_session, task.id)
    async with host.session_factory() as db:
        (await db.get(MediaJob, job.id)).status = "completed"
        await db.commit()
    await recover_stalled_task_coordinator_wakes_once()
    event = await _event(db_session, task.id)
    assert event.delivered_at is None and event.available_at <= datetime.now(timezone.utc)
    assert (await _fresh(db_session, TaskCoordinatorRun, run.id)).phase == TaskCoordinatorPhase.WAITING_JOB.value


async def _pause_and_resume(db_session, task_id, selection):
    """ON_HOLD, then back IN_PROGRESS: a new lifecycle generation."""

    task = await _fresh(db_session, Task, task_id)
    task.status = "on_hold"
    task.status_changed_at = datetime.now(timezone.utc)
    await db_session.commit()
    task = await _fresh(db_session, Task, task_id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.commit()
    return run


@pytest.mark.asyncio
async def test_pause_and_resume_rebinds_a_parked_wait_and_it_reparks(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    seen = _model(monkeypatch, [("", [_call("wait", "wait_until", {"seconds": 86400, "reason": "tomorrow"})])])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_timer"
    parked = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint
    old_lifecycle = run.lifecycle_generation

    rebound = await _pause_and_resume(db_session, task.id, selection)

    assert rebound.id == run.id and rebound.lifecycle_generation != old_lifecycle
    assert rebound.phase == TaskCoordinatorPhase.READY.value
    assert rebound.checkpoint == parked  # transcript, parked call and wait kept
    event = await _event(db_session, task.id)
    assert event.payload["reason"] == "task_coordinator_lifecycle_retry"
    assert event.available_at <= datetime.now(timezone.utc)

    again = await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=rebound.wake_generation,
    )

    assert again["status"] == "waiting_timer" and len(seen) == 1
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert row.phase == TaskCoordinatorPhase.WAITING_TIMER.value
    assert row.checkpoint["task_loop_wait"] == parked["task_loop_wait"]
    event = await _event(db_session, task.id)
    wake_at = datetime.fromisoformat(parked["task_loop_wait"]["wake_at"])
    assert abs((event.available_at - wake_at).total_seconds()) < 2
    assert host.loop_calls[-1]["runtime_checkpoint"]["pending_tool_call"]["name"] == "wait_until"


@pytest.mark.asyncio
async def test_pause_and_resume_of_a_running_loop_keeps_its_transcript(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    host = _install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)
    _model(monkeypatch, [
        ("", [_call("note", "record_note", {"text": "Launch on 2026-10-10"})]),
        ("", [_call("done", "complete_task", COMPLETE_ARGS)]),
    ])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "yielded"
    running = (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint
    assert (await _fresh(db_session, TaskCoordinatorRun, run.id)).phase == TaskCoordinatorPhase.RUNNING.value

    rebound = await _pause_and_resume(db_session, task.id, selection)

    assert rebound.phase == TaskCoordinatorPhase.READY.value
    assert rebound.checkpoint["messages"] == running["messages"]
    assert rebound.checkpoint["rounds"] == running["rounds"]
    assert rebound.checkpoint["retried_after"]["after"] == "task_resumed"
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 3600)
    done = await worker.run_task_agent_loop_once(
        event_id=(await _event(db_session, task.id)).id, task_id=task.id,
        wake_generation=rebound.wake_generation,
    )
    assert done["status"] == "completed"
    resumed = host.loop_calls[-1]
    assert _resumed(resumed)["initial_messages"] == [m for m in running["messages"] if m["role"] != "system"]
    assert resumed["user_message"].startswith("Task resumed at ")
    assert "re-check external state before continuing" in resumed["user_message"]


@pytest.mark.asyncio
async def test_plan_dag_runs_are_not_rebound_while_running(db_session, monkeypatch):
    _flag(monkeypatch, False)
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    run.phase = TaskCoordinatorPhase.RUNNING.value
    await db_session.commit()
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(ValueError, match="not stopped for retry"):
        await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_kind", ["timer", "job"])
async def test_cancelling_a_parked_task_closes_its_future_wake(db_session, monkeypatch, wait_kind):
    from packages.core import database
    from packages.core.services.task_coordinator_wakeup import wake_task_coordinator_for_media_job
    from packages.core.tasks.runtime_tasks import reconcile_terminal_task_coordinator_runs_once

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    host = _media_host(monkeypatch, db_session, selection, job.id)
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    _model(monkeypatch, [(
        "",
        [_call("wait", "wait_until", {"seconds": 86400, "reason": "tomorrow"})]
        if wait_kind == "timer"
        else [_call("video", "generate_file", {"kind": "video"})],
    )])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"].startswith("waiting_")
    row = await _fresh(db_session, Task, task.id)
    row.status = "cancelled"
    await db_session.commit()

    assert await reconcile_terminal_task_coordinator_runs_once() >= 1

    run_row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    assert run_row.phase == TaskCoordinatorPhase.CANCELLED.value
    assert run_row.wake_generation is None
    event = await _event(db_session, task.id)
    assert event.delivered_at is not None
    assert event.last_error == "permanent:task_coordinator_intent_closed:terminal_task"
    # The job keeps running; its completion no longer wakes anything.
    finished = await _fresh(db_session, type(job), job.id)
    finished.status = "completed"
    await db_session.flush()
    assert await wake_task_coordinator_for_media_job(db_session, finished) is None
    await db_session.rollback()


# ── B2 review fixes ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_waits_in_one_batch_park_one_and_keep_the_other(db_session, monkeypatch):
    from packages.core.workers import task_agent_loop_waits as waits

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    clock = {"now": None}
    real_now = waits.utcnow
    monkeypatch.setattr(waits, "utcnow", lambda: clock["now"] or real_now())

    def both_woke(messages, _tools):
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m.get("role") == "tool"}
        assert results["w1"]["status"] == "woke" and results["w2"]["status"] == "woke"
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    seen = _model(monkeypatch, [
        ("", [
            _call("w1", "wait_until", {"seconds": 3600, "reason": "first"}),
            _call("w2", "wait_until", {"seconds": 7200, "reason": "second"}),
        ]),
        both_woke,
    ])
    start = datetime.now(timezone.utc)
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_timer"
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    parked = row.checkpoint["runtime_checkpoint"]
    assert parked["pending_tool_call"]["id"] == "w1"
    assert [call["id"] for call in parked["remaining_tool_calls"]] == ["w2"]
    transcript = json.dumps(parked["messages"]) + json.dumps(row.checkpoint.get("messages"))
    assert "RuntimeToolSuspension" not in transcript
    assert not any(m.get("role") == "tool" for m in parked["messages"])

    # The first wait is due: it returns, then the second parks at its own time.
    clock["now"] = start + timedelta(seconds=3605)
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="due")
    await db_session.commit()
    second, row = await _deliver(db_session, task.id)
    assert second["status"] == "waiting_timer" and len(seen) == 1
    wait = row.checkpoint["task_loop_wait"]
    assert wait["tool_call_id"] == "w2"
    assert abs((datetime.fromisoformat(wait["wake_at"]) - (start + timedelta(seconds=7200))).total_seconds()) < 5

    clock["now"] = start + timedelta(seconds=7210)
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="due")
    await db_session.commit()
    done, _row = await _deliver(db_session, task.id)
    assert done["status"] == "completed" and len(seen) == 2


async def _block_and_retry(db_session, task_id, selection, *, code="agent_loop_resource_wait_expired"):
    run = (await db_session.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    run.phase = TaskCoordinatorPhase.FAILED.value
    run.checkpoint = {**run.checkpoint, "coordinator_outcome": {"error": code}}
    task = await _fresh(db_session, Task, task_id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.commit()
    return run


def _results_by_call(messages):
    return {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m.get("role") == "tool"}


@pytest.mark.asyncio
async def test_retry_after_a_parked_run_answers_every_unanswered_call(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    _model(monkeypatch, [("", [
        _call("w1", "wait_until", {"seconds": 3600, "reason": "first"}),
        _call("w2", "wait_until", {"seconds": 7200, "reason": "second"}),
    ])])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_timer"

    retried = await _block_and_retry(db_session, task.id, selection)

    messages = retried.checkpoint["messages"]
    results = _results_by_call(messages)
    assert results["w1"] == {"status": "interrupted", "executed": "no"}
    assert results["w2"] == {"status": "interrupted", "executed": "no"}
    # Every declared call of the last assistant turn now has a result after it.
    last = max(i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls"))
    declared = {call["id"] for call in messages[last]["tool_calls"]}
    assert declared == {"w1", "w2"} <= set(results)
    assert "runtime_checkpoint" not in retried.checkpoint


def test_retry_marks_only_unproven_effects_as_unknown():
    from packages.core.services.task_coordinator_wakeup import agent_loop_retry_checkpoint

    def call(call_id, name, args):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    checkpoint = {
        "messages": [
            {"role": "user", "content": "brief"},
            {"role": "assistant", "content": "", "tool_calls": [
                call("send", "send_email", {"to": "a@example.com"}),
                call("read", "read_file", {"path": "notes.md"}),
                call("done", "record_note", {"text": "x"}),
                call("box", "run_in_sandbox", {"cmd": "make"}),
            ]},
            {"role": "tool", "tool_call_id": "done", "content": "{\"ok\": true}"},
        ],
        "runtime_checkpoint": {
            "pending_tool_call": {"id": "box", "name": "run_in_sandbox", "arguments": {"cmd": "make"}},
            "remaining_tool_calls": [],
        },
        "coordinator_outcome": {"error": "agent_loop_resource_wait_expired"},
    }

    results = _results_by_call(agent_loop_retry_checkpoint(checkpoint)["messages"])

    assert results["box"] == {"status": "interrupted", "executed": "no"}
    assert results["send"] == {"status": "interrupted", "executed": "unknown"}
    assert results["read"] == {"status": "interrupted", "executed": "no"}
    assert results["done"] == {"ok": True}


@pytest.mark.asyncio
async def test_job_wait_without_a_call_id_is_unavailable_not_blocking(db_session, monkeypatch):
    from packages.core.ai.tools import media_tools
    from packages.core.workers import task_agent_loop_waits as waits

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    _media_host(monkeypatch, db_session, selection, job.id)
    monkeypatch.setattr(waits, "tool_call_id", lambda _arguments: "")

    real_read = media_tools._wait_media_jobs_handler

    async def must_not_block(**kwargs):
        assert kwargs.get("timeout_seconds") == 0, "the Task loop never waits in process"
        return await real_read(**kwargs)

    monkeypatch.setattr(media_tools, "_wait_media_jobs_handler", must_not_block)

    def unavailable(messages, _tools):
        error = _tool_error(messages)
        assert error["error"] == "wait_unavailable"
        return "", [_call("done", "complete_task", _completion(
            outcome="blocked", blocked_reason="Cannot wait for the video in this run.",
        ))]

    _model(monkeypatch, [
        ("", [_call("wait", "wait_media_jobs", {"job_ids": [job.id]})]),
        unavailable,
    ])
    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )
    assert outcome["status"] == "blocked"


@pytest.mark.asyncio
async def test_a_wake_within_clock_skew_of_its_time_is_due(db_session, monkeypatch):
    from packages.core.workers import task_agent_loop_waits as waits

    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    _install_host(monkeypatch, db_session, selection)
    clock = {"now": None}
    real_now = waits.utcnow
    monkeypatch.setattr(waits, "utcnow", lambda: clock["now"] or real_now())

    def woke(messages, _tools):
        result = json.loads(next(m["content"] for m in messages if m.get("tool_call_id") == "wait"))
        assert result["status"] == "woke" and result["late_seconds"] == 0
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    _model(monkeypatch, [("", [_call("wait", "wait_until", {"seconds": 600, "reason": "x"})]), woke])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_timer"
    wake_at = datetime.fromisoformat(
        (await _fresh(db_session, TaskCoordinatorRun, run.id)).checkpoint["task_loop_wait"]["wake_at"]
    )
    # The delivering worker's clock runs 1.5 s behind the stager's.
    clock["now"] = wake_at - timedelta(seconds=1.5)
    await stage_task_coordinator_wake(db_session, task_id=task.id, reason="due")
    await db_session.commit()
    done, _row = await _deliver(db_session, task.id)
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_job_wait_caps_ids_at_50_and_says_which_were_dropped(db_session, monkeypatch):
    task, run, selection, event_id, generation = await _agent_loop_run(db_session, monkeypatch)
    job = await _video_job(db_session, task)
    _media_host(monkeypatch, db_session, selection, job.id)
    others = [generate_ulid() for _ in range(51)]
    requested = [job.id, *others]

    def told(messages, _tools):
        result = _media_result(messages)
        assert result["status"] == "completed"
        assert result["dropped_job_ids"] == others[49:]
        assert "at most 50" in result["note"]
        assert result["missing_job_ids"] == others[:49] and result["total_count"] == 50
        return "", [_call("done", "complete_task", COMPLETE_ARGS)]

    _model(monkeypatch, [("", [_call("wait", "wait_media_jobs", {"job_ids": requested})]), told])
    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "waiting_job"
    row = await _fresh(db_session, TaskCoordinatorRun, run.id)
    wait = row.checkpoint["task_loop_wait"]
    pinned = row.checkpoint["runtime_checkpoint"]["pending_tool_call"]["arguments"]
    assert wait["job_ids"] == [job.id] == pinned["job_ids"]
    assert len(wait["requested_job_ids"]) == 50 and wait["dropped_job_ids"] == others[49:]

    await _finish_video(job.id, task.entity_id)
    done, _row = await _deliver(db_session, task.id)
    assert done["status"] == "completed"
