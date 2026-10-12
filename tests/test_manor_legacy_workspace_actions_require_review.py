"""Legacy ``manor`` workspace actions must go through the review gate.

Local E2E, 2026-09-07: asked to soften a hard block, the master agent created
an operation draft (review card raised, correct), could not self-approve it
(correct), then found ``manor(action="update_workspace_rules")`` via search
and used it — which applied a ``rules.replace`` immediately, with no card,
dropping an unrelated rule on the way. The user never approved anything.

Every workspace-configuration mutation an agent can reach must produce the
same review card as the ``operation`` draft flow and apply only after the
person approves it. This pins that for the whole family routed through
``runtime_manor_apply_workspace_operation_patch``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.ai.runtime.manor_actions import runtime_manor_apply_workspace_operation_patch


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace_with_rules(client: AsyncClient, headers: dict) -> tuple[str, str]:
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Review Gate"})
    ws = create.json()
    om = {
        "services": [{"key": "social", "service_key": "social", "name": "Social"}],
        "rules": [
            {"rule_key": "social_draft_only", "description": "帖子只出草稿", "rule_type": "draft_only",
             "action_patterns": ["social_post.publish"]},
            {"rule_key": "tone", "description": "保持友好语气"},
        ],
    }
    put = await client.put(f"/api/v1/workspaces/{ws['id']}/operating-model", headers=headers, json=om)
    assert put.status_code == 200, put.text
    return ws["id"], ws["entity_id"]


async def _rule_keys(db_session, ws_id: str) -> list[str]:
    from packages.core.models.workspace import Workspace

    row = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    await db_session.refresh(row)
    return [r.get("rule_key") for r in (row.operating_model or {}).get("rules") or []]


LEGACY_PATCHES = [
    ("update_workspace_rules", {"op": "rules.replace", "payload": {"rules": [
        {"rule_key": "social_publish_approval", "description": "发帖前审批", "rule_type": "approval_required",
         "action_patterns": ["social_post.publish"]},
    ]}}),
    ("update_operating_model", {"op": "operating_model.replace", "payload": {"operating_model": {"rules": []}}}),
    ("add_workspace_service", {"op": "service_role.upsert", "payload": {"service": {"key": "ads", "name": "Ads"}}}),
    ("remove_workspace_service", {"op": "service_role.remove", "payload": {"key": "social"}}),
    ("update_workspace_goals", {"op": "goals.replace", "payload": {"goals": [{"goal_key": "reach", "title": "Reach"}]}}),
]


@pytest.mark.parametrize("source_action,patch", LEGACY_PATCHES, ids=[p[0] for p in LEGACY_PATCHES])
@pytest.mark.asyncio
async def test_legacy_manor_action_returns_review_card_and_does_not_apply(
    client: AsyncClient, db_session, source_action, patch,
):
    headers = await _register(client, f"ws_review_{source_action[:12]}")
    ws_id, entity_id = await _workspace_with_rules(client, headers)
    before = await _rule_keys(db_session, ws_id)
    assert before == ["social_draft_only", "tone"]

    result = await runtime_manor_apply_workspace_operation_patch(
        db_session,
        entity_id=entity_id,
        user_id=None,
        workspace_id=ws_id,
        params={},
        source_action=source_action,
        patch=patch,
    )
    await db_session.commit()

    assert result is not None
    assert result.get("__hitl__") is True, result
    assert result.get("error") == "approval_required"
    assert result["hitl"]["action"] == "workspace.operation.apply"
    assert result["hitl"]["hitl_type"] == "review"
    assert result["draft"]["status"] == "open"
    assert result["draft"]["source_event_id"] == f"manor_tool.{source_action}"
    # nothing changed until a person approves the card
    assert await _rule_keys(db_session, ws_id) == before


@pytest.mark.asyncio
async def test_legacy_manor_action_review_shows_removed_hard_block(client: AsyncClient, db_session):
    headers = await _register(client, "ws_review_hardblock")
    ws_id, entity_id = await _workspace_with_rules(client, headers)

    result = await runtime_manor_apply_workspace_operation_patch(
        db_session,
        entity_id=entity_id,
        user_id=None,
        workspace_id=ws_id,
        params={},
        source_action="update_workspace_rules",
        patch=LEGACY_PATCHES[0][1],
    )

    review = result["hitl"]["payload"]
    assert "social_post.publish" in review["diff"]["removed_hard_blocks"]
