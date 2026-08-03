"""WorkspaceActivity -> workflow trigger auto-dispatch."""
from __future__ import annotations

import pytest

from packages.core.services import workflow_service, workspace_service


def _step():
    return [
        {
            "id": "start",
            "type": "trigger",
            "config": {"trigger_type": "workspace_event"},
            "next": ["s1"],
        },
        {"id": "s1", "type": "transform", "config": {"set": {"x": "1"}}, "next": ["end"]},
        {"id": "end", "type": "end", "config": {}, "next": []},
    ]


@pytest.mark.asyncio
async def test_workspace_activity_fires_subscribed_binding(db_session):
    entity_id, workspace_id = "ent_evt", "ws_evt"
    wf = await workflow_service.create_workflow(
        db_session, entity_id=entity_id, name="On lead", steps=_step(),
    )
    await workflow_service.create_workflow_binding(
        db_session, entity_id=entity_id, workflow_id=wf.id, workspace_id=workspace_id,
        trigger_type="workspace_event", trigger_config={"event": "lead.created"},
    )

    # a matching activity auto-starts a run with the workspace context
    await workspace_service.record_activity(
        db_session, workspace_id, entity_id,
        event_type="lead.created", summary="new lead",
    )
    runs = await workflow_service.list_runs(db_session, entity_id)
    matched = [r for r in runs if r.workflow_id == wf.id and r.workspace_id == workspace_id]
    assert len(matched) == 1
    assert matched[0].trigger_source == "workspace_event"

    # a non-matching event starts nothing new
    await workspace_service.record_activity(
        db_session, workspace_id, entity_id, event_type="other.event", summary="x",
    )
    runs_after = await workflow_service.list_runs(db_session, entity_id)
    assert len([r for r in runs_after if r.workflow_id == wf.id]) == 1


@pytest.mark.asyncio
async def test_activity_without_bindings_is_noop(db_session):
    # recording activity with no subscribed bindings must not error or create runs
    await workspace_service.record_activity(
        db_session, "ws_none", "ent_none", event_type="anything", summary="ok",
    )
    runs = await workflow_service.list_runs(db_session, "ent_none")
    assert runs == []
