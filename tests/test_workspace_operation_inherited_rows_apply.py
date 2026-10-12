"""Apply materializes the rows a draft changed, never the invalid rows it inherited.

Delta validation lets a draft through while the Workspace already holds
invalid rows (an inherited duplicate service_key, a mapping to an undeclared
service). Re-materializing those untouched rows on apply could rewire a live
subscription, so apply acts only on what the draft actually changed.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workspace_service import get_workspace_agent_mappings
from packages.core.services.workspace_operation_service import (
    OperationValidationError,
    apply_operation_draft,
    create_operation_draft,
)

pytestmark = pytest.mark.asyncio


async def _workspace_with_inherited_duplicate(db):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Inherited duplicate", status="active",
        operating_model={"services": [{"key": "a"}, {"key": "b"}]},
    )
    agents = [Agent(id=generate_ulid(), entity_id=entity_id, name=f"Agent {n}", status="active") for n in range(3)]
    db.add_all([workspace, *agents])
    await db.flush()
    subs = [
        AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                          service_key=key, agent_id=agent.id, status="active", custom_prompt=f"prompt {n}")
        for n, (key, agent) in enumerate(zip(("a", "a", "b"), agents))
    ]
    db.add_all(subs)
    await db.flush()
    return workspace, agents, subs


async def _active(db, workspace_id):
    rows = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ).execution_options(populate_existing=True))).scalars().all()
    return sorted((row.service_key, row.agent_id, row.custom_prompt) for row in rows)


async def test_changing_another_service_leaves_the_inherited_duplicate_untouched(db_session):
    workspace, agents, _ = await _workspace_with_inherited_duplicate(db_session)
    before = await _active(db_session, workspace.id)
    draft = await create_operation_draft(db_session, workspace.id, workspace.entity_id, initial_patches=[{
        "op": "agent_mapping.upsert",
        "payload": {"service_key": "b", "agent_id": agents[2].id, "custom_prompt": "new prompt for b"},
    }])
    assert draft.validation["valid"] is True
    assert [issue["path"] for issue in draft.validation["pre_existing_issues"]] == ["agent_mappings[1].service_key"]

    result = await apply_operation_draft(
        db_session, draft.id, workspace.entity_id, workspace.id, user_confirmation=True,
    )

    assert result["agent_mappings"]["updated"] == 1
    assert result["agent_mappings"]["unchanged"] == 2
    assert [issue["path"] for issue in result["pre_existing_issues"]] == ["agent_mappings[1].service_key"]
    after = await _active(db_session, workspace.id)
    # Both deployments on "a" are exactly as they were; only "b" changed.
    assert [row for row in after if row[0] == "a"] == [row for row in before if row[0] == "a"]
    assert ("b", agents[2].id, "new prompt for b") in after


async def test_editing_every_row_of_a_duplicated_service_is_a_new_error(db_session):
    workspace, agents, _ = await _workspace_with_inherited_duplicate(db_session)
    before = await _active(db_session, workspace.id)
    # upsert by service_key rewrites both "a" rows: the duplicate is now the draft's.
    draft = await create_operation_draft(db_session, workspace.id, workspace.entity_id, initial_patches=[{
        "op": "agent_mapping.upsert",
        "payload": {"service_key": "a", "agent_id": agents[0].id, "custom_prompt": "edited"},
    }])
    assert draft.validation["valid"] is False
    assert sorted((error["path"], error.get("code") or "") for error in draft.validation["errors"]) == [
        ("agent_mappings[0].service_key", "duplicate_service_change"),
        ("agent_mappings[1].service_key", ""),
        ("agent_mappings[1].service_key", "duplicate_service_change"),
    ]
    with pytest.raises(OperationValidationError, match=r"agent_mappings\[\d\]\.service_key"):
        await apply_operation_draft(
            db_session, draft.id, workspace.entity_id, workspace.id, user_confirmation=True,
        )
    assert await _active(db_session, workspace.id) == before


async def test_changing_one_row_of_a_duplicated_service_is_refused_at_review(db_session):
    workspace, _, _ = await _workspace_with_inherited_duplicate(db_session)
    before = await _active(db_session, workspace.id)
    mappings = await get_workspace_agent_mappings(db_session, workspace.id, workspace.entity_id)
    mappings[0] = {**mappings[0], "custom_prompt": "edited"}
    draft = await create_operation_draft(db_session, workspace.id, workspace.entity_id, initial_patches=[{
        "op": "agent_mapping.replace", "payload": {"mappings": mappings},
    }])
    # The duplicate itself stays inherited on the untouched second row, but the
    # review already says the change cannot apply: it would have to pick one
    # of two live deployments to rewrite.
    assert draft.validation["valid"] is False
    assert [(error["path"], error["code"]) for error in draft.validation["errors"]] == [
        ("agent_mappings[0].service_key", "duplicate_service_change"),
    ]
    assert [issue["path"] for issue in draft.validation["pre_existing_issues"]] == ["agent_mappings[1].service_key"]
    with pytest.raises(OperationValidationError, match="more than one active Agent"):
        await apply_operation_draft(
            db_session, draft.id, workspace.entity_id, workspace.id, user_confirmation=True,
        )
    assert await _active(db_session, workspace.id) == before


async def test_an_inherited_duplicate_goal_key_does_not_overwrite_the_first_goal(db_session):
    from packages.core.models.goal import Goal

    workspace = Workspace(
        id=generate_ulid(), entity_id=generate_ulid(), name="Inherited goal duplicate", status="active",
        operating_model={"services": [{"key": "a"}], "goals": [
            {"goal_key": "reach", "title": "Reach 100 readers", "target_value": 100},
            {"goal_key": "reach", "title": "Reach 5 readers", "target_value": 5},
        ]},
    )
    db_session.add(workspace)
    await db_session.flush()
    draft = await create_operation_draft(db_session, workspace.id, workspace.entity_id, initial_patches=[{
        "op": "goal.upsert", "payload": {"goal_key": "revenue", "title": "Earn 10", "target_value": 10},
    }])
    assert draft.validation["valid"] is True
    assert [issue["path"] for issue in draft.validation["pre_existing_issues"]] == ["goals[1].goal_key"]

    await apply_operation_draft(db_session, draft.id, workspace.entity_id, workspace.id, user_confirmation=True)

    goals = {goal.goal_key: goal for goal in (await db_session.execute(
        select(Goal).where(Goal.workspace_id == workspace.id))).scalars()}
    assert set(goals) == {"reach", "revenue"}
    assert goals["reach"].title == "Reach 100 readers"
