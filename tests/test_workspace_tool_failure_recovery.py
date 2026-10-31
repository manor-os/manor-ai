"""A stopped Task's recovery card projects its current cause without rewriting the stored card."""
from __future__ import annotations

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["same_plan", "other_plan", "other_workspace", "other_entity", "resolved", "specific_error"])
async def test_old_recovery_card_projection_is_scoped_and_does_not_rewrite_approval(db_session, variant):
    from datetime import datetime, timezone
    from apps.api.routers.workspace_chat import _hydrate_messages
    from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Recovery")
    plan_id = generate_ulid()
    reason = "search_tools: permission.agent_tool_binding"
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Diagnostic packet", status="waiting_on_customer",
        actual_output={"plan_id": plan_id, "supervisor_verdict": "needs_human", "supervisor_evidence": reason},
    )
    db_session.add_all([workspace, task])
    await db_session.flush()
    old_reason = "This workspace task needs a saved file/media/document deliverable, but no saved file link or path was recorded."
    if variant == "specific_error":
        old_reason = "Replacement planning could not be queued."
    card = await ensure_task_recovery_hitl(
        db_session, task, plan_id=generate_ulid() if variant == "other_plan" else plan_id,
        prompt="The run stopped.", issue=old_reason,
    )
    assert card is not None
    if variant == "other_workspace":
        task.workspace_id = generate_ulid()
    elif variant == "other_entity":
        task.entity_id = generate_ulid()
    elif variant == "resolved":
        card.resolved_at = datetime.now(timezone.utc)
    await db_session.flush()

    [response] = await _hydrate_messages(
        db_session, [card], entity_id=entity_id, workspace_id=workspace.id,
    )
    assert response.pending_action["payload"]["why"] == (reason if variant == "same_plan" else old_reason)
    assert card.pending_action["payload"]["why"] == old_reason
    await db_session.refresh(card)
    assert card.pending_action["payload"]["why"] == old_reason
