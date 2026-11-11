"""A draft applied after someone else's change keeps both changes.

Two people stage changes to the same Workspace; the second to apply no longer
loses its draft unless both changed the same setting, and never undoes the
first person's change.
"""

import pytest
from httpx import AsyncClient

from packages.core.services.operation_draft_rebase import rebase_operation_state
from tests.auth_helpers import register_user_and_get_token


def test_changes_to_different_fields_and_list_items_merge():
    base = {"app_view": {"title": "Studio", "theme": {"accent": "#111111"}},
            "rules": [{"rule_key": "a", "text": "A"}, {"rule_key": "b", "text": "B"}]}
    live = {"app_view": {"title": "Studio", "theme": {"accent": "#222222"}},
            "rules": [{"rule_key": "a", "text": "A"}, {"rule_key": "b", "text": "B2"}, {"rule_key": "c", "text": "C"}]}
    draft = {"app_view": {"title": "Music Studio", "theme": {"accent": "#111111"}},
             "rules": [{"rule_key": "b", "text": "B"}, {"rule_key": "d", "text": "D"}]}
    merged, conflicts = rebase_operation_state(base, live, draft)
    assert conflicts == []
    assert merged["app_view"] == {"title": "Music Studio", "theme": {"accent": "#222222"}}
    # The draft removed "a" and added "d"; the other editor's edit to "b" and new "c" stay.
    assert merged["rules"] == [{"rule_key": "b", "text": "B2"}, {"rule_key": "c", "text": "C"}, {"rule_key": "d", "text": "D"}]


def test_the_same_setting_changed_twice_is_a_conflict_unless_equal():
    base = {"app_view": {"title": "Studio"}, "rules": [{"rule_key": "a", "text": "A"}], "tags": ["x"]}
    live = {"app_view": {"title": "Studio One"}, "rules": [], "tags": ["x", "y"]}
    draft = {"app_view": {"title": "Studio Two"}, "rules": [{"rule_key": "a", "text": "A2"}], "tags": ["z"]}
    _, conflicts = rebase_operation_state(base, live, draft)
    assert conflicts == ["app_view.title", "rules[rule_key=a]", "tags"]
    _, none = rebase_operation_state(base, {**base, "app_view": {"title": "Same"}}, {**base, "app_view": {"title": "Same"}})
    assert none == []


def test_operating_model_mirror_reports_a_conflict_once():
    base = {"services": [{"service_key": "s", "name": "S"}],
            "operating_model": {"services": [{"service_key": "s", "name": "S"}]}}
    live = {"services": [{"service_key": "s", "name": "S1"}],
            "operating_model": {"services": [{"service_key": "s", "name": "S1"}]}}
    draft = {"services": [{"service_key": "s", "name": "S2"}],
             "operating_model": {"services": [{"service_key": "s", "name": "S2"}]}}
    assert rebase_operation_state(base, live, draft)[1] == ["services[service_key=s].name"]


async def _workspace(client: AsyncClient) -> tuple[dict, str]:
    resp = await register_user_and_get_token(client, json={
        "username": "draftrebase", "email": "draftrebase@test.com",
        "password": "DraftRebaseTest123!", "entity_name": "Draft Rebase Corp",
    })
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    workspace = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Shared Studio"})
    assert workspace.status_code == 201, workspace.text
    return headers, workspace.json()["id"]


async def _stage(client, headers, ws, patches):
    draft = await client.post(f"/api/v1/workspaces/{ws}/operation/drafts", headers=headers, json={"patches": patches})
    assert draft.status_code == 200, draft.text
    return draft.json()["id"]


async def _apply(client, headers, ws, draft_id):
    return await client.post(f"/api/v1/workspaces/{ws}/operation/drafts/{draft_id}/apply",
                             headers=headers, json={"user_confirmation": True})


@pytest.mark.asyncio
async def test_second_draft_applies_on_top_of_the_first(client: AsyncClient):
    headers, ws = await _workspace(client)
    looks = await _stage(client, headers, ws, [{"op": "app_view.update", "payload": {"title": "Release desk"}}])
    rules = await _stage(client, headers, ws, [{"op": "rule.add", "payload": {
        "rule_key": "reply_in_a_day", "description": "Reply to every artist within a day."}}])

    assert (await _apply(client, headers, ws, rules)).status_code == 200
    applied = await _apply(client, headers, ws, looks)
    assert applied.status_code == 200, applied.text

    state = (await client.get(f"/api/v1/workspaces/{ws}/operation/current", headers=headers)).json()["state"]
    assert state["operating_model"]["app_view"]["title"] == "Release desk"
    assert "reply_in_a_day" in [rule["rule_key"] for rule in state["rules"]]


@pytest.mark.asyncio
async def test_both_changing_the_same_setting_applies_nothing_and_names_it(client: AsyncClient):
    headers, ws = await _workspace(client)
    mine = await _stage(client, headers, ws, [{"op": "app_view.update", "payload": {"title": "Release desk"}}])
    theirs = await _stage(client, headers, ws, [{"op": "app_view.update", "payload": {"title": "Launch room"}}])

    assert (await _apply(client, headers, ws, theirs)).status_code == 200
    refused = await _apply(client, headers, ws, mine)
    assert refused.status_code == 409
    assert "operating_model.app_view.title" in refused.json()["detail"]

    state = (await client.get(f"/api/v1/workspaces/{ws}/operation/current", headers=headers)).json()["state"]
    assert state["operating_model"]["app_view"]["title"] == "Launch room"
