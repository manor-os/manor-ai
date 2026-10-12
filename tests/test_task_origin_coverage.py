"""Every tool call that creates a Task records the Task run that created it.

Guard the class, not the instance: every ToolPool tool (and schema action
variant) the classifier maps to ``workspace.task.create`` is executed with a
Task's Runtime envelope, plus the composite ``manor`` routes to the same
action. Each must yield ``details.origin_task_id`` (host-only) and default
``parent_task_id``, so a Task created inside a run is never an orphan.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.models.task import Task
from tests.test_effect_observation_coverage import _action_calls

_CREATE_KEY = "workspace.task.create"
# Composite routes to the same action that the schema-variant scan cannot
# enumerate (the action lives in params). Each is checked to classify as
# workspace.task.create below, so this list cannot go stale silently.
_COMPOSITE_CALLS = (
    ("manor", {"action": "create_task"}),
    ("manor", {"action": "workspace", "params": {"action": "create_task"}}),
)


def _task_create_calls() -> list[tuple[str, dict]]:
    calls = [
        (name, dict(arguments)) for name, arguments, action in _action_calls()
        if action.action_key == _CREATE_KEY
    ]
    for name, arguments in _COMPOSITE_CALLS:
        assert classify_runtime_tool(name, arguments).action.action_key == _CREATE_KEY, (name, arguments)
        calls.append((name, arguments))
    return calls


def _with_title(name: str, arguments: dict, title: str) -> dict:
    fields = {"title": title, "owner_service_key": "reader"}
    if "action" in arguments:
        # Composite tools (manor, workspace_agent) take the action's fields
        # under params.
        return {**arguments, "params": {**(arguments.get("params") or {}), **fields}}
    return {**arguments, **fields}


@pytest.mark.asyncio
async def test_draft_immediate_tasks_carry_only_the_host_origin(db_session):
    from packages.core.services.workspace_operation_service import _materialize_immediate_tasks
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    forged = {"title": "From the draft", "details": {"origin_task_id": "forged", "note": "kept"}}
    result = await _materialize_immediate_tasks(
        db_session, scenario.workspace, [forged], draft_id="draft-1", user_id=scenario.user.id,
        origin_task_id=scenario.task.id,
    )
    created = await db_session.get(Task, result["task_ids"][0])
    assert created.details["origin_task_id"] == scenario.task.id
    assert created.details["note"] == "kept"
    assert created.parent_task_id == scenario.task.id

    applied_by_a_person = await _materialize_immediate_tasks(
        db_session, scenario.workspace, [{**forged, "title": "Applied in chat"}],
        draft_id="draft-2", user_id=scenario.user.id,
    )
    created = await db_session.get(Task, applied_by_a_person["task_ids"][0])
    # Draft content (model-patched) can never claim an origin.
    assert "origin_task_id" not in created.details and created.parent_task_id is None


def test_the_scan_sees_every_task_creating_tool():
    names = {name for name, _arguments in _task_create_calls()}
    assert {"create_task", "workspace_create_task", "workspace_agent", "manor"} <= names


@pytest.mark.asyncio
async def test_every_task_creating_call_records_the_run_that_created_it(db_session, monkeypatch):
    from packages.core.ai.tool_pool import ToolPool
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.database.async_session",
        async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
    )
    monkeypatch.setattr("packages.core.tasks.ai_tasks.plan_and_run_task.delay", lambda *a, **k: None)
    pool = ToolPool()
    pool.initialize()
    envelope = SimpleNamespace(
        profile="background_worker", entity_id=scenario.entity_id, user_id=scenario.user.id,
        workspace_id=scenario.workspace.id, task_id=scenario.task.id, conversation_id=None,
    )
    missing = []
    for index, (name, arguments) in enumerate(_task_create_calls()):
        title = f"Created by {name} #{index}"
        handler = pool.get(name)["handler"]
        result = await handler(
            entity_id=scenario.entity_id, user_id=scenario.user.id,
            workspace_id=scenario.workspace.id,
            _runtime_envelope_from_context=envelope,
            **_with_title(name, arguments, title),
        )
        payload = json.loads(result) if isinstance(result, str) else result
        created = (await db_session.execute(
            select(Task).where(Task.entity_id == scenario.entity_id, Task.title == title)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if created is None:
            missing.append(f"{name}{arguments}: not created ({str(payload)[:200]})")
            continue
        if (created.details or {}).get("origin_task_id") != scenario.task.id or created.parent_task_id != scenario.task.id:
            missing.append(f"{name}{arguments}: no origin")
    assert missing == [], missing
