"""Workspace rule and governance writes stay consistent and race-safe.

Contract: rule-derived policy changes are committed with the source-rule edit;
direct policy writes cannot contradict a surviving source rule; and an older
operation draft preserves unrelated live governance edits made after the draft
was created.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select


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


async def _policy(db_session, ws_id: str) -> dict:
    from packages.core.models.governance import GovernancePolicy

    row = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id))
    ).scalar_one()
    await db_session.refresh(row)
    return dict(row.policy or {})


def _actions(policy, effect, *, source_kind=None):
    return [rule["selector"]["action_key"] for rule in policy["approval"]["policy"]["rules"]
            if rule["effect"] == effect and rule["selector"]["kind"] == "action"
            and (source_kind is None or rule["source_kind"] == source_kind)]


async def _write_user_rules(client, headers, ws_id, user_rules):
    url = f"/api/v1/approval-settings/workspaces/{ws_id}"
    current = await client.get(url, headers=headers)
    assert current.status_code == 200, current.text
    snapshot = current.json()
    response = await client.put(url, headers=headers, json={
        "expected_version": snapshot["version"],
        "draft": {"mode": snapshot["policy"]["mode"], "user_rules": user_rules},
    })
    assert response.status_code == 200, response.text
    return response.json()


def _draft_only_rule() -> dict:
    return {
        "rule_key": "social_draft_only",
        "description": "帖子只出草稿",
        "rule_type": "draft_only",
        "action_patterns": ["social_post.publish"],
    }


async def _seed_hard_block(client: AsyncClient, headers: dict) -> str:
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Live Governance"})
    ws_id = create.json()["id"]
    first = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={"patches": [{"op": "rule.add", "payload": {"rule": _draft_only_rule()}}]},
    )
    assert first.status_code == 200, first.text
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{first.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied.status_code == 200, applied.text
    return ws_id


@pytest.mark.asyncio
async def test_apply_keeps_governance_edits_made_after_draft_creation(client: AsyncClient, db_session):
    headers = await _register(client, "ws_live_gov")
    ws_id = await _seed_hard_block(client, headers)
    assert _actions(await _policy(db_session, ws_id), "deny") == ["social_post.publish"]

    # A draft that retypes the rule (operating_model.replace, as the rules
    # page does) is created FIRST — its snapshot still carries the source prohibition.
    om = (await client.get(f"/api/v1/workspaces/{ws_id}/operating-model", headers=headers)).json()["operating_model"]
    om["rules"] = [dict(_draft_only_rule(), rule_type="approval_required")]
    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={"patches": [{"op": "operating_model.replace", "payload": {"operating_model": om}}]},
    )
    assert draft.status_code == 200, draft.text

    # THEN the user makes an unrelated policy edit. Applying the older rule
    # draft must preserve this live change while deriving the rule retype.
    await _write_user_rules(client, headers, ws_id, [{
        "effect": "deny", "selector": {"kind": "action", "action_key": "credentials.read"},
    }])
    updated = await client.put(
        f"/api/v1/workspaces/{ws_id}/governance", headers=headers,
        json={"policy": {"max_risk_level": "medium"}, "change_summary": "protect credentials"},
    )
    assert updated.status_code == 200, updated.text
    assert "credentials.read" in _actions(await _policy(db_session, ws_id), "deny")

    # Applying the older draft must not bring the gate back.
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied.status_code == 200, applied.text

    final = await _policy(db_session, ws_id)
    assert _actions(final, "deny") == ["credentials.read"]
    assert _actions(final, "ask", source_kind="workspace_rule") == ["social_post.publish"]
    assert final["max_risk_level"] == "medium"


@pytest.mark.asyncio
async def test_direct_governance_edit_cannot_remove_a_source_rule_gate(
    client: AsyncClient, db_session,
):
    headers = await _register(client, "ws_direct_gov_conflict")
    ws_id = await _seed_hard_block(client, headers)

    response = await client.put(
        f"/api/v1/workspaces/{ws_id}/governance", headers=headers,
        json={"policy": {"approval": {"mode": "automatic", "rules": []}}},
    )
    assert response.status_code == 422, response.text
    # The shared CAS endpoint owns user rules only. Saving an empty user list
    # cannot erase a prohibition generated from a surviving Workspace rule.
    await _write_user_rules(client, headers, ws_id, [])
    persisted = await _policy(db_session, ws_id)
    assert _actions(persisted, "deny", source_kind="workspace_rule") == ["social_post.publish"]


@pytest.mark.asyncio
async def test_rules_page_retype_lifts_derived_hard_block_without_separate_policy_edit(
    client: AsyncClient, db_session,
):
    headers = await _register(client, "ws_rules_page_retype")
    ws_id = await _seed_hard_block(client, headers)

    om = (await client.get(
        f"/api/v1/workspaces/{ws_id}/operating-model", headers=headers,
    )).json()["operating_model"]
    om["rules"] = [dict(_draft_only_rule(), rule_type="approval_required")]
    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={"patches": [{
            "op": "operating_model.replace",
            "payload": {"operating_model": om},
        }]},
    )
    assert draft.status_code == 200, draft.text

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied.status_code == 200, applied.text

    final = await _policy(db_session, ws_id)
    assert _actions(final, "deny") == []
    assert _actions(final, "ask", source_kind="workspace_rule") == ["social_post.publish"]


@pytest.mark.asyncio
async def test_unrelated_operating_model_replace_does_not_write_governance_revision(
    client: AsyncClient,
):
    headers = await _register(client, "ws_unrelated_model_edit")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Unrelated model edit"},
    )
    assert created.status_code == 201, created.text
    ws_id = created.json()["id"]
    before = (await client.get(
        f"/api/v1/workspaces/{ws_id}/governance", headers=headers,
    )).json()
    operating_model = (await client.get(
        f"/api/v1/workspaces/{ws_id}/operating-model", headers=headers,
    )).json()["operating_model"]
    operating_model["evaluation"] = {"enabled": True}

    updated = await client.put(
        f"/api/v1/workspaces/{ws_id}/operating-model",
        headers=headers,
        json={"operating_model": operating_model},
    )

    assert updated.status_code == 200, updated.text
    after = (await client.get(
        f"/api/v1/workspaces/{ws_id}/governance", headers=headers,
    )).json()
    assert after["revision"] == before["revision"]


@pytest.mark.asyncio
async def test_apply_still_honours_a_governance_edit_inside_the_draft(client: AsyncClient, db_session):
    headers = await _register(client, "ws_draft_gov")
    ws_id = await _seed_hard_block(client, headers)

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {"op": "rule.remove", "payload": {"rule_key": "social_draft_only"}},
                {"op": "governance_policy.update", "payload": {"max_risk_level": "low"}},
            ]
        },
    )
    assert draft.status_code == 200, draft.text
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied.status_code == 200, applied.text

    final = await _policy(db_session, ws_id)
    assert final["max_risk_level"] == "low"
    assert _actions(final, "deny") == []
