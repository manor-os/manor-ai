"""Manual Task attribution and lifecycle-serialized lease admission."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

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
    monkeypatch.setattr("packages.core.tasks.ai_tasks.plan_and_run_task.delay", Mock())
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
