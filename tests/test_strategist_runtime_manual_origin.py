import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import workspace_task_actions as actions


@pytest.mark.parametrize("status,profile,parent_manual,expected", [
    ("paused", "workspace_operator", None, "human_requested"),
    ("paused", "background_worker", False, None),
    ("paused", "workflow_step", False, None),
    ("paused", "background_worker", True, "human_requested"),
    ("paused", "workflow_step", True, "human_requested"),
    ("paused", None, None, None),
    ("active", "background_worker", False, "event"),
    ("active", None, None, "event"),
    ("draft", "workspace_operator", None, None),
])
async def test_review_tool_preserves_real_runtime_origin(
    monkeypatch, status, profile, parent_manual, expected,
):
    from packages.core import database
    from packages.core.services import workspace_service
    from packages.core.strategist import service
    from packages.core.tasks import ai_tasks

    workspace = SimpleNamespace(id="workspace", entity_id="entity", status=status, deleted_at=None)
    parent = SimpleNamespace(
        id="parent", entity_id="entity", workspace_id="workspace", creator_id="owner",
        details={
            "automation_origin": {"task_id": "scheduled-parent"},
            **({"_manual_execution": {"user_id": "owner", "action": "start"}} if parent_manual else {}),
        },
        started_by="owner",
        trigger_data={"_workspace_user_request": {"user_id": "owner"}} if parent_manual else {},
    )
    db = SimpleNamespace(get=AsyncMock(return_value=parent), commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    enqueued = []

    def enqueue(*args, **kwargs):
        enqueued.append(kwargs)
        return SimpleNamespace(id="queued-review")

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(actions, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(service, "list_open_proposals", AsyncMock(return_value=[]))
    monkeypatch.setattr(workspace_service, "record_activity", AsyncMock())
    monkeypatch.setattr(ai_tasks.run_strategist_review, "apply_async", enqueue)
    params = {
        "reason": "User requested follow-up",  # Prose cannot upgrade the origin.
        "_runtime_envelope_from_context": SimpleNamespace(profile=profile, task_id="parent"),
        "task_id": "parent",
    }
    if profile == "workflow_step":
        params["_workflow_run_id_from_context"] = "parent"
    result = json.loads(await actions.runtime_workspace_request_strategist_review_action(
        entity_id="entity", workspace_id="workspace", user_id="owner", params=params,
    ))
    if expected is None:
        assert result["requested"] is False
        assert result["blocked"] is True
        assert not enqueued
    else:
        assert result["requested"] is True
        assert enqueued[0]["kwargs"]["trigger_kind"] == expected
        assert enqueued[0]["kwargs"]["user_id"] == "owner"
    assert workspace.status == status


async def test_background_review_cannot_supersede_a_human_cohort(monkeypatch):
    from packages.core import database
    from packages.core.services import workspace_service
    from packages.core.strategist import service
    from packages.core.tasks import ai_tasks

    db = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    supersede = AsyncMock()
    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(actions, "_load_workspace", AsyncMock(return_value=SimpleNamespace(
        id="workspace", entity_id="entity", status="active", deleted_at=None,
    )))
    monkeypatch.setattr(service, "list_open_proposals", AsyncMock(return_value=[SimpleNamespace(id="proposal")]))
    monkeypatch.setattr(service, "open_proposal_conflict", lambda _: {"open_count": 1})
    monkeypatch.setattr(service, "supersede_open_proposals", supersede)
    monkeypatch.setattr(workspace_service, "record_activity", AsyncMock())
    monkeypatch.setattr(ai_tasks.run_strategist_review, "apply_async", lambda **_: pytest.fail("must not enqueue"))
    result = json.loads(await actions.runtime_workspace_request_strategist_review_action(
        entity_id="entity", workspace_id="workspace", user_id="owner",
        params={"supersede": True, "_runtime_envelope_from_context": SimpleNamespace(profile="background_worker")},
    ))
    assert result["needs_decision"] is True
    supersede.assert_not_awaited()
