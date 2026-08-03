"""Per-proposal-type approval matrix API (Settings → Approval automation).

``GET  /workspaces/{id}/governance/approval-matrix`` — any entity member;
one row per distinct Strategist ``workspace.proposal.*`` action_key plus the
non-Strategist standing grants (so ``file.write`` & friends stay visible).

``PUT  …/approval-matrix`` — owner/admin only; toggles exactly one row and
writes a GovernanceRevision audit row per toggle.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.proposals.constants import (
    STRATEGIST_ACTION_KEYS,
    TASK_ACTION_KEY,
)

pytestmark = pytest.mark.asyncio

AUTOMATION_DELETE_KEY = "workspace.proposal.automation_change.delete"


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str]:
    resp = await register_user_and_get_token(
        client,
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, me.json()["entity_id"]


async def _seed_workspace(entity_id: str) -> str:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        workspace = Workspace(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Approval Matrix WS",
            status="active",
            settings={},
        )
        db.add(workspace)
        await db.commit()
    return workspace.id


async def _member_headers(entity_id: str, username: str) -> dict:
    import packages.core.database as dbmod
    from packages.core.services.auth_service import create_access_token, hash_password

    async with dbmod.async_session() as db:
        member = User(
            id=generate_ulid(),
            entity_id=entity_id,
            email=f"{username}@test.com",
            display_name=username,
            password_hash=hash_password("pass123"),
            role="member",
        )
        db.add_all([
            member,
            UserMembership(
                id=generate_ulid(),
                user_id=member.id,
                entity_id=entity_id,
                role="member",
                status="active",
                is_primary=True,
            ),
        ])
        await db.commit()
    return {"Authorization": f"Bearer {create_access_token(member.id, entity_id, 'member')}"}


async def _revision_count(workspace_id: str) -> int:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        return len(list((await db.execute(
            select(GovernanceRevision).where(
                GovernanceRevision.workspace_id == workspace_id
            )
        )).scalars().all()))


async def test_matrix_lists_every_strategist_key_and_other_grants(client: AsyncClient):
    owner, entity_id = await _register_owner(client, "matrix_shape_owner")
    workspace_id = await _seed_workspace(entity_id)

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=owner,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    keys = [row["action_key"] for row in body["rows"]]
    assert keys == list(STRATEGIST_ACTION_KEYS)
    assert len(keys) == len(set(keys)), "pause/resume must collapse to one row"

    by_key = {row["action_key"]: row for row in body["rows"]}
    task_row = by_key[TASK_ACTION_KEY]
    assert task_row["kind"] == "task"
    assert task_row["risk_level"] == "low"
    assert task_row["auto_approved"] is False
    assert task_row["label"]
    assert by_key[AUTOMATION_DELETE_KEY]["risk_level"] == "high"
    assert by_key[AUTOMATION_DELETE_KEY]["operation"] == "delete"
    assert by_key["workspace.proposal.automation_change.pause"]["risk_level"] == "medium"
    assert {row["kind"] for row in body["rows"]} == {
        "task", "automation_change", "workflow_change", "goal_change", "experiment",
    }

    # Nothing existing disappears: the default capability grants ride along.
    others = {(g["kind"], g["value"]) for g in body["other_grants"]}
    assert ("capability", "file.write") in others
    # …and no Strategist key leaks into the "other" bucket.
    assert not [g for g in body["other_grants"] if g["value"] in STRATEGIST_ACTION_KEYS]


async def test_toggle_on_and_off_writes_and_removes_the_grant(client: AsyncClient):
    import packages.core.database as dbmod

    owner, entity_id = await _register_owner(client, "matrix_toggle_owner")
    workspace_id = await _seed_workspace(entity_id)
    before = await _revision_count(workspace_id)

    on = await client.put(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=owner,
        json={"action_key": AUTOMATION_DELETE_KEY, "auto_approved": True},
    )
    assert on.status_code == 200, on.text
    by_key = {row["action_key"]: row for row in on.json()["rows"]}
    assert by_key[AUTOMATION_DELETE_KEY]["auto_approved"] is True
    # One row toggled — the rest stay on human review.
    assert by_key[TASK_ACTION_KEY]["auto_approved"] is False

    async with dbmod.async_session() as db:
        policy_row = (await db.execute(
            select(GovernancePolicy).where(
                GovernancePolicy.workspace_id == workspace_id
            )
        )).scalar_one()
        assert AUTOMATION_DELETE_KEY in policy_row.policy["auto_approve_actions"]
    assert await _revision_count(workspace_id) == before + 1

    off = await client.put(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=owner,
        json={"action_key": AUTOMATION_DELETE_KEY, "auto_approved": False},
    )
    assert off.status_code == 200, off.text
    off_by_key = {row["action_key"]: row for row in off.json()["rows"]}
    assert off_by_key[AUTOMATION_DELETE_KEY]["auto_approved"] is False

    async with dbmod.async_session() as db:
        policy_row = (await db.execute(
            select(GovernancePolicy).where(
                GovernancePolicy.workspace_id == workspace_id
            )
        )).scalar_one()
        assert AUTOMATION_DELETE_KEY not in policy_row.policy["auto_approve_actions"]
        revisions = list((await db.execute(
            select(GovernanceRevision)
            .where(GovernanceRevision.workspace_id == workspace_id)
            .order_by(GovernanceRevision.revision.asc())
        )).scalars().all())
    assert len(revisions) == before + 2
    assert revisions[-1].change_summary == (
        f"revoke always-approve action: {AUTOMATION_DELETE_KEY}"
    )


async def test_member_can_read_but_not_toggle(client: AsyncClient):
    owner, entity_id = await _register_owner(client, "matrix_member_owner")
    workspace_id = await _seed_workspace(entity_id)
    member = await _member_headers(entity_id, "matrix_member_user")

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=member,
    )
    assert listed.status_code == 200, listed.text

    denied = await client.put(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=member,
        json={"action_key": TASK_ACTION_KEY, "auto_approved": True},
    )
    assert denied.status_code == 403, denied.text

    still = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=owner,
    )
    assert not [
        row for row in still.json()["rows"] if row["auto_approved"]
    ], "a 403 must not have granted anything"


async def test_unknown_key_and_cross_entity_are_rejected(client: AsyncClient):
    owner, entity_id = await _register_owner(client, "matrix_edge_owner")
    workspace_id = await _seed_workspace(entity_id)

    unknown = await client.put(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=owner,
        json={"action_key": "workspace.proposal.not_a_kind", "auto_approved": True},
    )
    assert unknown.status_code == 400, unknown.text

    stranger, _ = await _register_owner(client, "matrix_stranger")
    cross_read = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=stranger,
    )
    assert cross_read.status_code == 404, cross_read.text

    cross_write = await client.put(
        f"/api/v1/workspaces/{workspace_id}/governance/approval-matrix",
        headers=stranger,
        json={"action_key": TASK_ACTION_KEY, "auto_approved": True},
    )
    assert cross_write.status_code == 404, cross_write.text
