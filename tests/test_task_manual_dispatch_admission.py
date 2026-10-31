"""Manual Task attribution and lifecycle-serialized lease admission."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.workspace import Workspace
from packages.core.services.task_execution_origin import task_has_manual_execution
from packages.core.services.task_service import update_task


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["automatic_parent", "manual_parent", "foreground"])
async def test_workspace_task_start_keeps_trusted_execution_origin(db_session, monkeypatch, origin):
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.models.task import Task
    from packages.core.services.task_execution_origin import record_manual_task_execution
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.task.details = {"scheduled_job_id": "automatic-job"}
    if origin == "manual_parent":
        record_manual_task_execution(scenario.task, user_id=scenario.user.id, action="task_retry")
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    result = json.loads(await runtime_workspace_create_task_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        params={
            "title": "Background child", "owner_service_key": "reader", "start": True,
            "_runtime_envelope_from_context": SimpleNamespace(
                profile="workspace_operator" if origin == "foreground" else "background_worker",
                user_id=scenario.user.id, task_id=scenario.task.id,
                workspace_id=scenario.workspace.id,
                principal=resolve_runtime_principal(
                    surface=ChatSurface.SCHEDULED_AGENT_RUN, entity_id=scenario.entity_id,
                    user_id=scenario.user.id, workspace_id=scenario.workspace.id,
                ),
            ),
        },
    ))
    assert result.get("created") is True, result
    task = await db_session.get(Task, result["task"]["id"])
    assert task_has_manual_execution(task) is (origin != "automatic_parent")
    if origin == "automatic_parent":
        assert task.details["automation_origin"]["task_id"] == scenario.task.id
    # Provenance, whatever the admission: the Task run that created it.
    assert task.parent_task_id == scenario.task.id
    # Host-only copy the model can neither set nor clear.
    assert task.details["origin_task_id"] == scenario.task.id


def test_task_origin_defaults_the_parent_and_never_overwrites_it():
    from packages.core.services.task_execution_origin import record_task_origin

    chosen = SimpleNamespace(id="child", parent_task_id="chosen-parent", details={"note": "x"})
    record_task_origin(chosen, "run-task")
    assert chosen.parent_task_id == "chosen-parent"
    assert chosen.details == {"note": "x", "origin_task_id": "run-task"}
    orphan = SimpleNamespace(id="child", parent_task_id=None, details=None)
    record_task_origin(orphan, "run-task")
    assert orphan.parent_task_id == "run-task"
    for origin in (None, "", "child"):
        untouched = SimpleNamespace(id="child", parent_task_id=None, details={})
        record_task_origin(untouched, origin)
        assert untouched.parent_task_id is None and untouched.details == {}


@pytest.mark.asyncio
async def test_workspace_task_created_outside_a_task_run_has_no_parent(db_session, monkeypatch):
    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.models.task import Task
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    result = json.loads(await runtime_workspace_create_task_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        params={
            "title": "Chat child", "owner_service_key": "reader",
            # A model-supplied task_id is a business target, not this run's Task.
            "task_id": scenario.task.id,
            "_runtime_envelope_from_context": SimpleNamespace(
                profile="workspace_operator", user_id=scenario.user.id, task_id=None,
                workspace_id=scenario.workspace.id,
            ),
        },
    ))
    assert result.get("created") is True, result
    task = await db_session.get(Task, result["task"]["id"])
    assert task.parent_task_id is None
    assert "origin_task_id" not in task.details


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "background_worker", "workspace_operator"])
async def test_task_tools_preserve_only_trusted_manual_origin(db_session, monkeypatch, profile):
    from packages.core.ai.runtime.manor_actions import runtime_manor_create_task, runtime_manor_update_task
    from packages.core.ai.runtime.task_actions import runtime_update_task_action
    from packages.core.models.task import Task
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.task.details = {"scheduled_job_id": "automatic-job"}
    await db_session.flush()
    runtime_kwargs = {"_runtime_envelope_from_context": SimpleNamespace(
        profile=profile, entity_id=scenario.entity_id, workspace_id=scenario.workspace.id,
        task_id=scenario.task.id, user_id=scenario.user.id,
    )}
    forged_details = {
        "scheduled_job_id": None,
        "_manual_execution": {"user_id": "forged-user", "action": "forged"},
        "origin_task_id": "forged-origin",
        "note": "keep user content",
    }
    created = json.loads(await runtime_manor_create_task(
        db_session, entity_id=scenario.entity_id, user_id=scenario.user.id,
        params={"title": "Scoped child Task", "workspace_id": scenario.workspace.id, "details": forged_details},
        runtime_tool_kwargs=runtime_kwargs,
    ))
    child = await db_session.get(Task, created["id"])
    assert task_has_manual_execution(child) is (profile == "workspace_operator")
    assert child.parent_task_id == scenario.task.id
    assert child.details["origin_task_id"] == scenario.task.id
    assert child.details["note"] == "keep user content"
    assert child.details.get("_manual_execution", {}).get("user_id") != "forged-user"

    result = json.loads(await runtime_manor_update_task(
        db_session, entity_id=scenario.entity_id, user_id=scenario.user.id,
        params={"task_id": scenario.task.id, "status": "in_progress", "details": forged_details, "manual_execution": True},
        runtime_tool_kwargs=runtime_kwargs,
    ))
    assert result.get("updated") is True, result
    assert task_has_manual_execution(scenario.task) is (profile == "workspace_operator")
    assert scenario.task.details["scheduled_job_id"] == "automatic-job"
    # The model can neither clear nor rewrite the host origin of the child.
    for forged_origin in (None, "forged-origin"):
        result = json.loads(await runtime_manor_update_task(
            db_session, entity_id=scenario.entity_id, user_id=scenario.user.id,
            params={"task_id": child.id, "details": {"origin_task_id": forged_origin}},
            runtime_tool_kwargs=runtime_kwargs,
        ))
        assert result.get("updated") is True, result
        assert child.details["origin_task_id"] == scenario.task.id

    # The direct task tool cannot turn public argument names into trusted
    # update_task control arguments or overwrite durable execution lineage.
    scenario.task.details = {"scheduled_job_id": "automatic-job"}
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    result = json.loads(await runtime_update_task_action(
        entity_id=scenario.entity_id,
        params={
            "task_id": scenario.task.id, "status": "in_progress", "manual_execution": True,
            "user_id": "forged-user", "details": forged_details, **runtime_kwargs,
        },
    ))
    assert result.get("updated") is True, result
    await db_session.refresh(scenario.task)
    assert task_has_manual_execution(scenario.task) is (profile == "workspace_operator")
    assert scenario.task.details["scheduled_job_id"] == "automatic-job"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_execution", [False, True])
async def test_task_start_actor_attribution_requires_manual_intent(
    db_session, manual_execution,
):
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.task.details = {"automation_origin": {"task_id": "scheduled-parent"}}
    await db_session.flush()

    task = await update_task(
        db_session, scenario.task.id, scenario.entity_id,
        user_id=scenario.user.id, manual_execution=manual_execution,
        status="in_progress", dispatch_plan=False,
    )

    assert task_has_manual_execution(task) is manual_execution
    assert task.details["automation_origin"] == {"task_id": "scheduled-parent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["pause", "delete"])
async def test_dispatcher_revalidates_lifecycle_after_candidate_discovery(
    db_session, monkeypatch, transition,
):
    from tests.task_manual_helpers import allow_step_approval, task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.task.details = {"scheduled_job_id": "automatic-job"}
    await db_session.commit()
    approval = allow_step_approval(monkeypatch)
    original_execute = db_session.execute
    transitioned = False

    async def execute_with_lifecycle_change(statement, *args, **kwargs):
        nonlocal transitioned
        result = await original_execute(statement, *args, **kwargs)
        columns = getattr(statement, "selected_columns", None)
        if not transitioned and columns is not None and list(columns.keys()) == ["id", "workspace_id"]:
            transitioned = True
            values = {"status": "paused"}
            if transition == "delete":
                values["deleted_at"] = datetime.now(timezone.utc)
            async with AsyncSession(db_session.bind, expire_on_commit=False) as other:
                await other.execute(update(Workspace).where(
                    Workspace.id == scenario.workspace.id,
                ).values(**values))
                await other.commit()
        return result

    monkeypatch.setattr(db_session, "execute", execute_with_lifecycle_change)
    leased = await Dispatcher().checkout_steps_for_worker(db_session, scenario.worker, max_n=1)

    assert transitioned
    assert leased == []
    assert scenario.step.step_status == "pending"
    assert scenario.step.attempt_count == 0
    approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_keeps_workspace_locked_until_lease_commit(db_session, monkeypatch):
    from tests.task_manual_helpers import allow_step_approval, task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.task.details = {"scheduled_job_id": "automatic-job"}
    await db_session.commit()
    allow_step_approval(monkeypatch)

    leased = await Dispatcher().checkout_steps_for_worker(db_session, scenario.worker, max_n=1)
    assert len(leased) == 1
    started = asyncio.Event()

    async def pause_workspace():
        async with AsyncSession(db_session.bind, expire_on_commit=False) as other:
            started.set()
            await other.execute(update(Workspace).where(
                Workspace.id == scenario.workspace.id,
            ).values(status="paused"))
            await other.commit()

    pausing = asyncio.create_task(pause_workspace())
    try:
        await started.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(pausing), timeout=0.1)
        await db_session.commit()
        await asyncio.wait_for(pausing, timeout=5)
    finally:
        if not pausing.done():
            pausing.cancel()
            await asyncio.gather(pausing, return_exceptions=True)


@pytest.mark.asyncio
async def test_dispatcher_skips_workspace_with_lifecycle_mutation_in_flight(db_session, monkeypatch):
    from tests.task_manual_helpers import allow_step_approval, task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    scenario.task.details = {"scheduled_job_id": "automatic-job"}
    await db_session.commit()
    approval = allow_step_approval(monkeypatch)
    async with AsyncSession(db_session.bind, expire_on_commit=False) as other:
        await other.execute(update(Workspace).where(
            Workspace.id == scenario.workspace.id,
        ).values(status="paused"))
        leased = await asyncio.wait_for(
            Dispatcher().checkout_steps_for_worker(db_session, scenario.worker, max_n=1),
            timeout=5,
        )
        assert leased == []
        await other.commit()
    approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_drafted_workspace_task_starts_its_own_run_from_chat(db_session, monkeypatch):
    """A drafted Task used to be "run" by delegating its work in Chat: it stayed pending."""
    from sqlalchemy import select

    from packages.core.ai.runtime.workspace_task_actions import (
        runtime_workspace_create_task_action,
        runtime_workspace_update_task_runtime_action,
    )
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task import Task
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    chat_context = {"_runtime_envelope_from_context": SimpleNamespace(
        profile="workspace_operator", user_id=scenario.user.id, task_id=None,
        workspace_id=scenario.workspace.id,
    )}
    drafted = json.loads(await runtime_workspace_create_task_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        params={"title": "Drafted report", "owner_service_key": "reader", **chat_context},
    ))
    task_id = drafted["task"]["id"]
    assert drafted["dispatched"] is False

    # start_task is the gateway name for update_task_runtime with start=true.
    from packages.core.ai.tools.workspace_agent_tools import _workspace_start_task_handler

    started = json.loads(await _workspace_start_task_handler(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        task_id=task_id, **chat_context,
    ))
    assert started["started"] is True, started
    assert "Do not do its work in Chat" in started["next"]
    task = await db_session.get(Task, task_id)
    await db_session.refresh(task)
    assert task.status == "in_progress"
    assert task_has_manual_execution(task) is True
    intents = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id == task_id,
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
    ))).scalars().all()
    assert len(intents) == 1

    again = json.loads(await runtime_workspace_update_task_runtime_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        params={"task_id": task_id, "start": True, **chat_context},
    ))
    assert again["error"] == "task_not_startable" and again["status"] == "in_progress"


@pytest.mark.asyncio
async def test_the_turn_that_starts_a_task_reports_it_with_server_status(db_session, monkeypatch):
    """Only created Tasks got server-written status; a start was reported by model prose alone."""
    from packages.core.ai.runtime.workspace_task_actions import (
        runtime_workspace_create_task_action,
        runtime_workspace_update_task_runtime_action,
    )
    from packages.core.services.workspace_execution_evidence import (
        ExecutionEvidenceScope, read_current_turn_task_references,
    )
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )

    def turn(message_id):
        return {"_runtime_envelope_from_context": SimpleNamespace(
            profile="workspace_operator", surface="workspace_chat", entity_id=scenario.entity_id,
            principal=SimpleNamespace(kind="owner", execution_user_id=scenario.user.id),
            user_id=scenario.user.id, task_id=None, workspace_id=scenario.workspace.id,
            conversation_id="chat-9", metadata={"origin_user_message_id": message_id},
        )}

    def scope(message_id):
        return ExecutionEvidenceScope(
            scenario.entity_id, scenario.workspace.id, scenario.user.id, "chat-9",
            origin_user_message_id=message_id,
        )

    drafted = json.loads(await runtime_workspace_create_task_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        conversation_id="chat-9",
        params={"title": "Drafted report", "owner_service_key": "reader", **turn("m1")},
    ))
    assert "start_task" in drafted["next"]
    task_id = drafted["task"]["id"]
    started = json.loads(await runtime_workspace_update_task_runtime_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        conversation_id="chat-9", params={"task_id": task_id, "start": True, **turn("m2")},
    ))
    assert started["started"] is True, started

    assert await read_current_turn_task_references(scope("m1")) == [
        {"kind": "task", "id": task_id, "scope": "current_turn", "effect": "created"},
    ]
    assert await read_current_turn_task_references(scope("m2")) == [
        {"kind": "task", "id": task_id, "scope": "current_turn", "effect": "started"},
    ]


@pytest.mark.asyncio
async def test_starting_a_drafted_task_by_recreating_it_points_back_to_the_draft(db_session, monkeypatch):
    """A second create_task with start=true used to leave the draft pending beside a copy."""
    from sqlalchemy import func, select

    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.models.task import Task
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    chat_context = {"_runtime_envelope_from_context": SimpleNamespace(
        profile="workspace_operator", user_id=scenario.user.id, task_id=None,
        workspace_id=scenario.workspace.id,
    )}

    async def create(conversation_id, **params):
        return json.loads(await runtime_workspace_create_task_action(
            entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
            conversation_id=conversation_id,
            params={"title": "Drafted report", "owner_service_key": "reader", **params, **chat_context},
        ))

    drafted = await create("chat-1")
    draft = await db_session.get(Task, drafted["task"]["id"])

    again = await create("chat-1", start=True)
    assert again["error"] == "task_already_drafted" and again["task_id"] == draft.id
    count = (await db_session.execute(select(func.count()).select_from(Task).where(
        Task.workspace_id == scenario.workspace.id, Task.title == "Drafted report",
    ))).scalar_one()
    assert count == 1
    # Another conversation's request is its own Task.
    assert (await create("chat-2", start=True)).get("created") is True


async def _apply_task_prepare(db_session, scenario, conversation_id, **task):
    from packages.core.services.workspace_operation_service import (
        apply_operation_draft,
        create_operation_draft,
    )

    draft = await create_operation_draft(
        db_session, scenario.workspace.id, scenario.entity_id, user_id=scenario.user.id,
        initial_patches=[{"op": "task.prepare", "payload": {
            "title": "Drafted report", "owner_service_key": "reader", **task,
        }}],
        origin_conversation_id=conversation_id,
    )
    await db_session.commit()
    return await apply_operation_draft(
        db_session, draft.id, scenario.entity_id, scenario.workspace.id,
        user_id=scenario.user.id, user_confirmation=True,
    )


@pytest.mark.asyncio
async def test_a_task_drafted_through_an_operation_draft_is_started_not_copied(db_session, monkeypatch):
    """task.prepare Tasks had no source conversation, so "start it" made a second Task."""
    from sqlalchemy import func, select

    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.models.task import Task
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    applied = await _apply_task_prepare(db_session, scenario, "chat-1", start=False)
    [drafted_id] = [row["id"] for row in applied["tasks_created"]]
    assert applied["started_task_ids"] == [] and "start_task" in applied["next"]
    drafted = await db_session.get(Task, drafted_id)
    assert drafted.status == "pending" and drafted.conversation_id == "chat-1"

    # Re-drafting the same Task in that conversation reuses it.
    again = await _apply_task_prepare(db_session, scenario, "chat-1", start=False)
    assert [row["id"] for row in again["tasks_created"]] == [drafted_id]

    # The other chat path is pointed back at the draft instead of copying it.
    copied = json.loads(await runtime_workspace_create_task_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        conversation_id="chat-1",
        params={"title": "Drafted report", "owner_service_key": "reader", "start": True,
                "_runtime_envelope_from_context": SimpleNamespace(
                    profile="workspace_operator", user_id=scenario.user.id, task_id=None,
                    workspace_id=scenario.workspace.id)},
    ))
    assert copied["error"] == "task_already_drafted" and copied["task_id"] == drafted_id

    # Applying the applied draft again is no review card: it names its Task and the start.
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_operation_action

    reapplied = json.loads(await runtime_workspace_operation_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        conversation_id="chat-1",
        params={"action": "apply_draft", "draft_id": applied["draft"]["id"]},
    ))
    assert reapplied["status"] == "already_applied"
    assert drafted_id in reapplied["llm_message"] and "start_task" in reapplied["llm_message"]
    count =(await db_session.execute(select(func.count()).select_from(Task).where(
        Task.workspace_id == scenario.workspace.id, Task.title == "Drafted report",
    ))).scalar_one()
    assert count == 1


async def _create_beside_open_draft(db_session, monkeypatch, patches):
    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.services.workspace_operation_service import create_operation_draft
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    draft = await create_operation_draft(
        db_session, scenario.workspace.id, scenario.entity_id, user_id=scenario.user.id,
        initial_patches=[{"op": "task.prepare", "payload": {
            "title": title, "owner_service_key": "reader", "start": False,
        }} for title in patches],
        origin_conversation_id="chat-3",
    )
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    created = json.loads(await runtime_workspace_create_task_action(
        entity_id=scenario.entity_id, workspace_id=scenario.workspace.id, user_id=scenario.user.id,
        conversation_id="chat-3",
        params={"title": "Drafted report", "owner_service_key": "reader",
                "_runtime_envelope_from_context": SimpleNamespace(
                    profile="workspace_operator", user_id=scenario.user.id, task_id=None,
                    workspace_id=scenario.workspace.id)},
    ))
    return scenario, draft, created


@pytest.mark.asyncio
async def test_creating_the_task_of_an_open_draft_supersedes_the_draft(db_session, monkeypatch):
    """The Task was created beside its open draft, whose later approval made a copy."""
    from packages.core.models.workspace import WorkspaceOperationDraft

    _, draft, created = await _create_beside_open_draft(db_session, monkeypatch, ["Drafted report"])
    assert created["created"] is True and created["superseded_draft_id"] == draft.id
    await db_session.refresh(draft)
    assert (await db_session.get(WorkspaceOperationDraft, draft.id)).status == "discarded"


@pytest.mark.asyncio
async def test_a_task_in_a_larger_open_draft_is_not_created_beside_it(db_session, monkeypatch):
    from sqlalchemy import func, select

    from packages.core.models.task import Task

    scenario, draft, created = await _create_beside_open_draft(
        db_session, monkeypatch, ["Drafted report", "Another report"],
    )
    assert created["error"] == "task_in_open_draft" and created["draft_id"] == draft.id
    count = (await db_session.execute(select(func.count()).select_from(Task).where(
        Task.workspace_id == scenario.workspace.id, Task.title == "Drafted report",
    ))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_task_prepare_with_start_starts_the_task_on_apply(db_session):
    """task.prepare accepted start but never read it: the Task stayed pending."""
    from sqlalchemy import select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task import Task
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    applied = await _apply_task_prepare(db_session, scenario, "chat-2", start=True)
    [task_id] = applied["started_task_ids"]
    task = await db_session.get(Task, task_id)
    await db_session.refresh(task)
    assert task.status == "in_progress" and task.conversation_id == "chat-2"
    assert task_has_manual_execution(task) is True
    intents = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id == task_id,
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
    ))).scalars().all()
    assert len(intents) == 1
    assert "Do not do its work in Chat" in applied["next"]


# Chat-reachable Task creators must record the drafting conversation, or a later
# "start it" cannot find the draft and makes a copy (the 2026-10-29 task.prepare
# gap). The entity-level create_task tool has no Workspace or conversation.
_CREATE_TASK_WITHOUT_CONVERSATION = {
    ("packages/core/ai/runtime/task_actions.py", "runtime_create_task_action"),
}


def test_every_chat_reachable_task_creation_records_its_conversation():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    modules = sorted((root / "packages/core/ai").rglob("*.py")) + [
        root / "packages/core/services/workspace_operation_service.py",
    ]
    found, missing = set(), []
    for path in modules:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports_creator = any(
            isinstance(node, ast.ImportFrom) and node.module == "packages.core.services.task_service"
            and any(alias.name == "create_task" for alias in node.names)
            for node in ast.walk(tree)
        )
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(func):
                if not isinstance(call, ast.Call):
                    continue
                target = call.func
                is_service_call = (
                    isinstance(target, ast.Attribute) and target.attr == "create_task"
                    and isinstance(target.value, ast.Name) and target.value.id == "task_service"
                ) or (isinstance(target, ast.Name) and target.id == "create_task" and imports_creator)
                if not is_service_call:
                    continue
                site = (str(path.relative_to(root)), func.name)
                found.add(site)
                keywords = {kw.arg for kw in call.keywords}
                spread = None in keywords and "conversation_id" in ast.get_source_segment(source, func)
                if "conversation_id" not in keywords and not spread and site not in _CREATE_TASK_WITHOUT_CONVERSATION:
                    missing.append(site)
    assert found, "no Task creation sites found: the scan is broken"
    assert not missing, f"Task creation without the drafting conversation: {missing}"
    assert _CREATE_TASK_WITHOUT_CONVERSATION <= found, "an exemption names a site that no longer exists"
