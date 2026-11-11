"""Readiness ticks never resurrect legacy fallback proposals."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.tasks.monitor_tasks import _async_readiness_check


def _setup_tick(monkeypatch, *, reason_code=None, changed=False, snapshot=None):
    from packages.core.models.goal import Goal
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Task
    from packages.core.models.workspace import AgentSubscription, Workspace
    from packages.core.tasks.ai_tasks import run_strategist_review

    workspace = SimpleNamespace(
        id="workspace-1", entity_id="entity-1", name="Workspace", deleted_at=None,
        status="active", heartbeat_enabled=True,
        settings={
            "created_by_user_id": "user-1",
            "_readiness_snapshot": snapshot or {
                "agents": 1, "goals": 0 if changed else 1, "integration_providers": 1,
            },
            "_strategist_llm_retry": {"retried_review_ids": ["original-fallback"]},
            "user_setting": "preserved",
        },
    )
    evidence = SimpleNamespace(
        summary="Strategist proposed 1 task(s): [fallback] Strategist could not reach the LLM",
        details={
            "review_id": "retry-fallback", "trigger_kind": "event",
            "trigger_detail": "retry after strategist llm unavailable",
        },
        trace_id="retry-fallback",
    )
    rejected_item = SimpleNamespace(
        status="rejected", decision={"decision": "rejected", "reason_code": reason_code},
    )
    history = [evidence, rejected_item] if reason_code else [evidence]
    queried_tables = []

    def scalar_result(value):
        return SimpleNamespace(scalar_one=lambda: value, scalar_one_or_none=lambda: value)

    def rows_result(rows):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def execute(statement):
        table = statement.get_final_froms()[0].name
        queried_tables.append(table)
        if table == Workspace.__tablename__:
            return rows_result([workspace])
        if table == RuntimeEvidence.__tablename__:
            return scalar_result(evidence)
        if table == Goal.__tablename__ and statement.column_descriptions[0]["entity"] is Goal:
            return rows_result([SimpleNamespace(current_value=0, target_value=10)])
        if table == AgentSubscription.__tablename__:
            return scalar_result(1)
        if table in {Task.__tablename__, ReviewRun.__tablename__}:
            return scalar_result(0)
        raise AssertionError(f"Unexpected readiness query: {statement}")

    db = SimpleNamespace(execute=AsyncMock(side_effect=execute), commit=AsyncMock(), rollback=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    dispatch = Mock()
    startup = AsyncMock()
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: session)
    monkeypatch.setattr("packages.core.services.workspace_access.lock_workspace_access_boundary", AsyncMock(return_value=workspace))
    monkeypatch.setattr("packages.core.tasks.monitor_tasks._reconcile_blueprint_startup_after_monitor", startup)
    monkeypatch.setattr(run_strategist_review, "apply_async", dispatch)
    monkeypatch.setattr(
        "packages.core.strategist.context.workspace_configured_provider_keys",
        AsyncMock(return_value=["gmail"]),
    )
    return workspace, history, queried_tables, db, dispatch, startup


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", [None, "WRONG_DIRECTION", "BAD_TIMING"])
async def test_two_idle_ticks_do_not_retry_legacy_fallback_or_rejected_cohort(monkeypatch, reason_code):
    workspace, history, queried, db, dispatch, startup = _setup_tick(monkeypatch, reason_code=reason_code)
    original_settings = deepcopy(workspace.settings)
    original_history = deepcopy(history)

    assert await _async_readiness_check() == 0
    assert await _async_readiness_check() == 0

    dispatch.assert_not_called()
    assert workspace.settings == original_settings
    assert history == original_history
    assert workspace.status == "active" and workspace.heartbeat_enabled is True
    assert queried == ["workspaces", "agent_subscriptions", "goals"] * 2
    assert db.commit.await_count == 2
    assert startup.await_count == 2


@pytest.mark.asyncio
async def test_readiness_change_still_dispatches_once_without_fallback_retry(monkeypatch):
    workspace, history, queried, db, dispatch, startup = _setup_tick(
        monkeypatch, reason_code="WRONG_DIRECTION", changed=True,
    )
    original_retry_state = deepcopy(workspace.settings["_strategist_llm_retry"])

    assert await _async_readiness_check() == 1
    assert await _async_readiness_check() == 0

    dispatch.assert_called_once_with(
        args=[workspace.id],
        kwargs={
            "trigger_kind": "event", "trigger_detail": "readiness changed: goals: 0->1",
            "user_id": "user-1",
        },
        countdown=5,
    )
    assert workspace.settings["_strategist_llm_retry"] == original_retry_state
    assert workspace.settings["_readiness_snapshot"] == {"agents": 1, "goals": 1, "integration_providers": 1}
    assert workspace.status == "active" and workspace.heartbeat_enabled is True
    assert queried == ["workspaces", "agent_subscriptions", "goals"] * 2
    assert db.commit.await_count == startup.await_count == 2


@pytest.mark.asyncio
async def test_provider_baseline_is_recorded_without_a_review(monkeypatch):
    # A snapshot from before the Workspace-scoped provider count (or none):
    # the first tick records the baseline; only a later increase is news.
    workspace, _, _, _, dispatch, _ = _setup_tick(
        monkeypatch, snapshot={"agents": 1, "goals": 1, "integrations": 7},
    )

    assert await _async_readiness_check() == 0
    dispatch.assert_not_called()
    assert workspace.settings["_readiness_snapshot"]["integration_providers"] == 1

    workspace.settings["_readiness_snapshot"]["integration_providers"] = 0
    assert await _async_readiness_check() == 1
    assert dispatch.call_args.kwargs["kwargs"]["trigger_detail"] == (
        "readiness changed: integration_providers: 0->1"
    )
