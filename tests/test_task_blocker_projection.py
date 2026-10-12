"""Database-free Task blocker/current owner projections, not runtime mutations."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from apps.api.routers import tasks as routes
from packages.core.models.task import Task, TaskLog
from packages.core.services import task_requester_identity
from packages.core.services.task_execution import task_execution_intent_generation


@pytest.fixture(autouse=True)
def _isolate_sandbox_owner_registry():
    """These pure/fake-session tests use neither Redis nor the database."""
    yield


NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def task_row(**changes):
    values = {
        "id": "task-a", "entity_id": "entity-a", "workspace_id": "workspace-a",
        "title": "Research", "status": "blocked", "priority": 3,
        "task_type": "general", "details": {}, "delegate_service_keys": [],
        "status_changed_at": NOW,
    }
    values.update(changes)
    return Task(**values)


def typed_blocker(task, **changes):
    blocker = {
        "kind": "capability_unavailable", "stage": "planning",
        "generation": task_execution_intent_generation(task),
        "missing_capabilities": ["web.safe_search"],
        "affected_agents": [{
            "service_key": "researcher", "subscription_id": "sub-a",
            "agent_id": "agent-a", "agent_name": "市场分析师",
        }],
        "log_id": "log-a", "observed_at": NOW.isoformat(),
    }
    blocker.update(changes)
    return blocker


def legacy_log(task, **changes):
    values = {
        "id": "log-a", "task_id": task.id, "created_at": NOW,
        "log_type": "ai_execution_failed", "created_by": "system",
        "meta": {
            "actor_type": "system", "capability_gap": True,
            "missing_capabilities": ["web.safe_search"],
        },
    }
    values.update(changes)
    return TaskLog(**values)


def scalars_result(rows):
    result = MagicMock()
    result.scalars.return_value = rows
    return result


def owner_lookup(**changes):
    row = {
        "subscription_id": "sub-a", "subscription_entity_id": "entity-a",
        "workspace_id": "workspace-a", "service_key": "researcher",
        "agent_id": "agent-a", "entity_id": "entity-a",
        "name": "市场分析师", "avatar_url": "/researcher.png", "available": True,
    }
    row.update(changes)
    return row


def test_current_typed_details_project_without_rewriting_audit_or_details():
    task = task_row()
    blocker = typed_blocker(task, operation_draft_id="draft-a", request_id="request-a")
    task.details = {"capability_blocker": blocker}
    before = deepcopy(task.details)

    response = routes._to_response(task)

    assert response.current_blocker == blocker
    assert response.agent_id is None
    assert response.agent_name is None
    assert task.details == before


@pytest.mark.parametrize("status", [
    "pending", "scheduled", "in_progress", "waiting_on_customer", "on_hold",
    "completed", "failed", "cancelled",
])
def test_non_blocked_states_never_retain_current_blocker(status):
    task = task_row()
    task.details = {"capability_blocker": typed_blocker(task)}
    task.status = status

    assert routes._to_response(task).current_blocker is None


@pytest.mark.parametrize("changes", [
    {"status_changed_at": NOW + timedelta(seconds=1)},
    {"owner_subscription_id": "replacement-sub"},
    {"workspace_id": "replacement-workspace"},
    {"delegate_service_keys": ["new-delegate"]},
])
def test_generation_fences_retries_and_routing_changes(changes):
    task = task_row()
    task.details = {"capability_blocker": typed_blocker(task)}
    for key, value in changes.items():
        setattr(task, key, value)

    assert routes._current_blocker_for_task(task) is None


@pytest.mark.parametrize("changes", [
    {"generation": 1}, {"generation": None}, {"kind": "retry_everything"},
    {"stage": "unknown"}, {"kind": []}, {"stage": {}},
])
def test_malformed_typed_metadata_does_not_become_current_blocker(changes):
    task = task_row()
    task.details = {"capability_blocker": typed_blocker(task, **changes)}

    assert routes._current_blocker_for_task(task) is None


def test_unknown_fields_are_not_exposed_and_capability_ids_are_not_reinterpreted():
    task = task_row()
    task.details = {"capability_blocker": typed_blocker(
        task, secret="not-a-display-field", missing_capabilities=["web.safe_search", 7, "", "web.safe_search"],
        affected_agents=[{"agent_name": "Analyst", "tool_token": "private"}, None, {}],
    )}

    blocker = routes._current_blocker_for_task(task)

    assert blocker["kind"] == "capability_unavailable"
    assert blocker["missing_capabilities"] == ["web.safe_search"]
    assert blocker["affected_agents"] == [{"agent_name": "Analyst"}]
    assert "secret" not in blocker


def test_latest_typed_log_is_projected_with_original_system_actor_untouched():
    task = task_row()
    log = legacy_log(task, id="current-log", meta={
        "actor_type": "system", "blocker": typed_blocker(task),
    })

    blocker = routes._current_blocker_for_task(task, log)

    assert blocker["log_id"] == "current-log"
    assert blocker["observed_at"] == NOW.isoformat()
    assert log.created_by == "system"
    assert log.meta["actor_type"] == "system"


def test_legacy_latest_capability_gap_is_read_only_and_never_guesses_specific_cause():
    task = task_row()
    log = legacy_log(task)
    before = deepcopy(log.meta)

    blocker = routes._current_blocker_for_task(task, log)

    assert blocker == {
        "kind": "capability_unavailable", "stage": "planning",
        "generation": task_execution_intent_generation(task),
        "missing_capabilities": ["web.safe_search"], "affected_agents": [],
        "log_id": "log-a", "observed_at": NOW.isoformat(),
    }
    assert task.details == {}
    assert log.meta == before


@pytest.mark.parametrize("changes", [
    {"created_at": NOW - timedelta(microseconds=1)},
    {"log_type": "manual_retry"},
    {"log_type": "status_change"},
    {"log_type": "ai_execution_started"},
    {"meta": {"capability_gap": "true", "missing_capabilities": ["web.safe_search"]}},
    {"meta": {"capability_gap": True, "missing_capabilities": []}},
    {"meta": {"capability_gap": True, "missing_capabilities": "web.safe_search"}},
    {"meta": {"error": "Missing capability web.safe_search"}},
])
def test_legacy_stale_or_unproven_log_never_becomes_current(changes):
    task = task_row()

    assert routes._current_blocker_for_task(task, legacy_log(task, **changes)) is None


def test_legacy_missing_lifecycle_clock_is_not_guessed():
    task = task_row(status_changed_at=None)

    assert routes._current_blocker_for_task(task, legacy_log(task)) is None


@pytest.mark.asyncio
async def test_typed_current_state_and_terminal_tasks_need_no_history_query():
    current = task_row()
    current.details = {"capability_blocker": typed_blocker(current)}
    stale = task_row(id="stale")
    stale.details = {"capability_blocker": typed_blocker(stale, generation="old")}
    terminal = task_row(id="done", status="completed")
    db = SimpleNamespace(execute=AsyncMock())

    blockers = await routes._current_task_blockers(db, [current, stale, terminal])

    assert blockers == {current.id: current.details["capability_blocker"]}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_history_lookup_is_one_bounded_latest_recovery_event_query():
    first = task_row(id="first")
    second = task_row(id="second")
    terminal = task_row(id="terminal", status="completed")
    # A newer status event takes precedence over an earlier capability gap.
    rows = [legacy_log(first), legacy_log(second, log_type="status_change")]
    db = SimpleNamespace(execute=AsyncMock(return_value=scalars_result(rows)))

    blockers = await routes._current_task_blockers(db, [first, second, terminal])

    assert set(blockers) == {"first"}
    db.execute.assert_awaited_once()
    query = str(db.execute.call_args.args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "row_number() OVER (PARTITION BY task_logs.task_id" in query
    assert "task_logs.created_at DESC, task_logs.id DESC" in query
    assert "anon_1.position = 1" in query
    assert "'first'" in query and "'second'" in query
    assert "'terminal'" not in query
    assert "'status_change'" in query and "'manual_retry'" in query


def test_ordinary_task_owner_is_real_agent_separate_from_execution_actor():
    task = task_row(owner_subscription_id="sub-a", owner_service_key="researcher", agent_id="executor")
    agents = {
        "sub-a": owner_lookup(),
        "executor": {"name": "Execution Agent", "avatar_url": "/executor.png"},
    }

    response = routes._to_response(task, agents=agents)

    assert response.owner_agent_id == "agent-a"
    assert response.owner_agent_name == "市场分析师"
    assert response.owner_agent_avatar == "/researcher.png"
    assert response.agent_id == "executor"
    assert response.agent_name == "Execution Agent"
    assert response.session_host_agent_id is None


@pytest.mark.parametrize("changes", [
    {"subscription_entity_id": "foreign"}, {"workspace_id": "foreign"},
    {"service_key": "different-service"}, {"entity_id": "foreign"},
    {"available": False},
])
def test_owner_resolution_rejects_foreign_or_inactive_binding(changes):
    task = task_row(owner_subscription_id="sub-a", owner_service_key="researcher")
    agents = {"sub-a": owner_lookup(**changes)}

    assert routes._owner_agent_for_task(task, agents) is None
    assert routes._to_response(task, agents=agents).owner_agent_name is None


def test_missing_pinned_subscription_never_falls_back_to_same_service_replacement():
    task = task_row(owner_subscription_id="removed-sub", owner_service_key="researcher")

    assert routes._owner_agent_for_task(task, {"sub-a": owner_lookup()}) is None


def test_unpinned_service_resolves_only_one_exact_active_deployment():
    task = task_row(owner_service_key="researcher")
    agents = {
        "sub-a": owner_lookup(),
        "foreign": owner_lookup(subscription_id="foreign", workspace_id="other-workspace"),
    }
    assert routes._owner_agent_for_task(task, agents)["name"] == "市场分析师"

    agents["sub-b"] = owner_lookup(subscription_id="sub-b", agent_id="agent-b")
    assert routes._owner_agent_for_task(task, agents) is None


@pytest.mark.asyncio
async def test_owner_agent_lookup_batches_subscription_scope_and_agent_names(monkeypatch):
    tasks = [
        task_row(owner_subscription_id="sub-a", owner_service_key="researcher"),
        task_row(id="unpinned", owner_service_key="researcher"),
    ]
    monkeypatch.setattr(task_requester_identity, "resolve_task_requesters", AsyncMock(return_value={}))
    subscription = SimpleNamespace(
        id="sub-a", entity_id="entity-a", workspace_id="workspace-a",
        service_key="researcher", agent_id="agent-a", name="Research service",
    )
    agent = SimpleNamespace(id="agent-a", entity_id="entity-a", name="市场分析师", status="active", avatar_url="/researcher.png")
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        scalars_result([SimpleNamespace(id="workspace-a", name="Workspace")]),
        scalars_result([subscription]), scalars_result([agent]),
    ]))

    lookups = await routes._resolve_lookups(db, tasks)

    assert len(lookups) == 6
    assert db.execute.await_count == 3
    for task in tasks:
        response = routes._to_response(task, *lookups)
        assert response.owner_agent_id == "agent-a"
        assert response.owner_agent_name == "市场分析师"
    query = str(db.execute.await_args_list[1].args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "agent_subscriptions.status = 'active'" in query
    assert "agent_subscriptions.entity_id = 'entity-a'" in query
    assert "agent_subscriptions.workspace_id = 'workspace-a'" in query
    assert "agent_subscriptions.service_key = 'researcher'" in query


def test_capability_blocker_is_server_owned_runtime_detail():
    assert "capability_blocker" in routes._TASK_RUNTIME_DETAIL_KEYS
    assert routes._without_task_runtime_details({
        "capability_blocker": {"kind": "approval_required"}, "customer_note": "Keep this",
    }) == {"customer_note": "Keep this"}


@pytest.mark.asyncio
async def test_list_board_and_detail_share_legacy_projection(monkeypatch):
    from packages.core.services import task_execution_reconcile, workspace_access

    task = task_row(owner_subscription_id="sub-a", owner_service_key="researcher")
    user = SimpleNamespace(id="user-a", entity_id="entity-a", role="member")
    db = SimpleNamespace(execute=AsyncMock(return_value=scalars_result([legacy_log(task)])))
    monkeypatch.setattr(routes, "require_workspace_readable", AsyncMock())
    monkeypatch.setattr(workspace_access, "readable_workspace_ids_for_user", AsyncMock(return_value={"workspace-a"}))
    monkeypatch.setattr(routes, "_resolve_lookups", AsyncMock(return_value=(
        {}, {"sub-a": owner_lookup()}, {}, {}, {}, {},
    )))
    monkeypatch.setattr(routes, "_active_execution_task_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(routes, "list_tasks", AsyncMock(return_value=([task], 1)))
    monkeypatch.setattr(routes, "get_tasks_by_status", AsyncMock(return_value={
        "blocked": [task], "_counts": {"blocked": 1},
    }))
    monkeypatch.setattr(routes, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(task_execution_reconcile, "reconcile_task_from_latest_completed_plan", AsyncMock())

    listing = await routes.list_my_tasks(
        status=None, workspace_id="workspace-a", workspace_id_alias=None,
        category_id=None, parent_task_id=None, attention=False, limit=50, offset=0,
        user=user, db=db,
    )
    board = await routes.task_board(
        workspace_id="workspace-a", workspace_id_alias=None,
        limit_per_status=50, offset_per_status=0, user=user, db=db,
    )
    detail = await routes.get_one_task(task.id, user=user, db=db)

    for response in (listing.items[0], board["blocked"][0], detail):
        assert response.current_blocker == routes._current_blocker_for_task(task, legacy_log(task))
        assert response.owner_agent_name == "市场分析师"
    assert sum(
        "FROM task_logs" in str(call.args[0])
        for call in db.execute.await_args_list
    ) == 3
