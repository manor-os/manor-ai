"""Retired grant endpoints and their versioned, scoped approval replacements."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.auth_helpers import register_user_and_get_token
from tests.approval_settings_test_utils import action_rule, room_reply_rule
from packages.core.governance.approval_policy_service import initialize_personal_approval
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import WorkspaceStaff

ACTION_KEY = "workspace.proposal.task"
ROOM_ID = "approval-test-room"


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    resp = await register_user_and_get_token(client, json={
        "username": username, "email": f"{username}@test.com", "password": "pass123",
        "entity_name": f"{username} Corp",
    })
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, data["user_id"], me.json()["entity_id"]


def _draft_rules(snapshot):
    return [{key: rule[key] for key in ("id", "effect", "selector")}
            for rule in snapshot["saved_rules"] if rule["editable"]]


async def _seed_workspace_with_rules(client, owner):
    created = await client.post("/api/v1/workspaces", headers=owner,
        json={"name": "Approval rules WS", "heartbeat_enabled": False})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    path = f"/api/v1/approval-settings/workspaces/{workspace_id}"
    current = await client.get(path, headers=owner)
    assert current.status_code == 200, current.text
    saved = await client.put(path, headers=owner, json={
        "expected_version": current.json()["version"],
        "draft": {"mode": None, "user_rules": [action_rule(ACTION_KEY), room_reply_rule(ROOM_ID)]},
    })
    assert saved.status_code == 200, saved.text
    return workspace_id, saved.json()


async def _member_headers(entity_id: str, username: str, workspace_id: str) -> dict:
    import packages.core.database as dbmod
    from packages.core.services.auth_service import create_access_token, hash_password
    async with dbmod.async_session() as db:
        member = User(id=generate_ulid(), entity_id=entity_id, email=f"{username}@test.com",
            display_name=username, password_hash=hash_password("pass123"), role="member")
        db.add_all([member, UserMembership(user_id=member.id, entity_id=entity_id,
            role="member", status="active", is_primary=True),
            WorkspaceStaff(workspace_id=workspace_id, user_id=member.id, role="viewer", status="active")])
        await db.flush()
        await initialize_personal_approval(db, user_id=member.id, entity_id=entity_id)
        await db.commit()
    return {"Authorization": f"Bearer {create_access_token(member.id, entity_id, 'member')}"}


async def _revisions(workspace_id):
    import packages.core.database as dbmod
    async with dbmod.async_session() as db:
        return list((await db.execute(select(GovernanceRevision).where(
            GovernanceRevision.workspace_id == workspace_id).order_by(GovernanceRevision.revision))).scalars())


@pytest.mark.asyncio
async def test_member_can_read_but_not_revoke(client: AsyncClient):
    owner, _, entity_id = await _register_owner(client, "grants_member_owner")
    workspace_id, saved = await _seed_workspace_with_rules(client, owner)
    member = await _member_headers(entity_id, "grants_member_user", workspace_id)
    path = f"/api/v1/approval-settings/workspaces/{workspace_id}"
    listed = await client.get(path, headers=member)
    assert listed.status_code == 200, listed.text
    assert _draft_rules(listed.json()) == _draft_rules(saved)
    assert listed.json()["capabilities"]["can_save_rule"] is False
    denied = await client.put(path, headers=member, json={
        "expected_version": listed.json()["version"], "draft": {"mode": None, "user_rules": []},
    })
    assert denied.status_code == 403, denied.text
    still = await client.get(path, headers=owner)
    assert still.json()["version"] == saved["version"]
    assert _draft_rules(still.json()) == _draft_rules(saved)


@pytest.mark.asyncio
async def test_owner_revoke_preserves_other_resource_rule_and_writes_revision(client: AsyncClient):
    import packages.core.database as dbmod
    from packages.core.governance.service import check_step_policy
    owner, owner_id, entity_id = await _register_owner(client, "grants_admin_owner")
    workspace_id, saved = await _seed_workspace_with_rules(client, owner)
    path = f"/api/v1/approval-settings/workspaces/{workspace_id}"
    revisions_before = len(await _revisions(workspace_id))
    kept = [rule for rule in _draft_rules(saved) if rule["selector"]["kind"] == "resource"]
    removed = await client.put(path, headers=owner, json={
        "expected_version": saved["version"], "draft": {"mode": None, "user_rules": kept},
    })
    assert removed.status_code == 200, removed.text
    assert _draft_rules(removed.json()) == kept
    stale = await client.put(path, headers=owner, json={
        "expected_version": saved["version"], "draft": {"mode": None, "user_rules": []},
    })
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["current"]["version"] == removed.json()["version"]
    revisions = await _revisions(workspace_id)
    assert len(revisions) == revisions_before + 1
    assert revisions[-1].changed_by == owner_id
    assert revisions[-1].change_summary == "Updated execution and approval settings"
    async with dbmod.async_session() as db:
        row = (await db.execute(select(GovernancePolicy).where(
            GovernancePolicy.workspace_id == workspace_id))).scalar_one()
        assert all(rule["selector"]["action_key"] != ACTION_KEY for rule in row.policy["approval"]["policy"]["rules"])
        for resource_id, allowed in ((ROOM_ID, True), ("another-room", False)):
            decision = await check_step_policy(db, entity_id=entity_id, user_id=owner_id,
                workspace_id=workspace_id, action_key="external_message.send", kind="action",
                resource_kind="resource", resource_id=resource_id, risk_level="high")
            assert decision.allowed is allowed
            assert decision.pause_for_hitl is (not allowed)
    final = await client.put(path, headers=owner, json={
        "expected_version": removed.json()["version"], "draft": {"mode": None, "user_rules": []},
    })
    assert final.status_code == 200, final.text
    assert final.json()["saved_rules"] == []
    assert final.json()["inheritance"] == "following"
    assert len(await _revisions(workspace_id)) == revisions_before + 2


@pytest.mark.asyncio
async def test_retired_grant_routes_never_mutate_and_keep_entity_boundary(client: AsyncClient):
    owner, _, _ = await _register_owner(client, "grants_edge_owner")
    workspace_id, saved = await _seed_workspace_with_rules(client, owner)
    base = f"/api/v1/workspaces/{workspace_id}/governance/standing-grants"
    before = len(await _revisions(workspace_id))
    for method, suffix in (("GET", ""), ("DELETE", f"/action/{ACTION_KEY}"),
                           ("DELETE", "/capability/browser.use"), ("DELETE", "/action/never.granted"),
                           ("DELETE", f"/scope/{ACTION_KEY}")):
        response = await client.request(method, base + suffix, headers=owner)
        assert response.status_code == 410, response.text
        assert response.json()["detail"]["code"] == "approval_configuration_retired"
    assert len(await _revisions(workspace_id)) == before
    path = f"/api/v1/approval-settings/workspaces/{workspace_id}"
    assert (await client.get(path, headers=owner)).json()["version"] == saved["version"]
    stranger, _, _ = await _register_owner(client, "grants_stranger")
    assert (await client.get(base, headers=stranger)).status_code == 404
    assert (await client.get(path, headers=stranger)).status_code == 403
