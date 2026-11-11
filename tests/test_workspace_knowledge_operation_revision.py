"""Knowledge configuration and reviewed operation drafts share one revision."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apps.api.routers import workspaces as router
from auth_helpers import register_user_and_get_token
from packages.core.models.document import DocumentGroup
from packages.core.models.user import User
from packages.core.models.workspace import Workspace


async def _workspace(client):
    registered = await register_user_and_get_token(client, {
        "username": "knowledge_revision",
        "email": "knowledge_revision@example.test",
        "password": "KnowledgeRevision2026!",
        "entity_name": "Knowledge Revision",
    })
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    response = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Knowledge Revision"},
    )
    assert response.status_code == 201, response.text
    return headers, response.json()["id"]


async def _integration_draft(client, headers, workspace_id):
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/drafts",
        headers=headers,
        json={"patches": [{"op": "integration.remove", "payload": {"provider": "slack"}}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["validation"]["valid"], response.text
    return response.json()


async def _apply(client, headers, workspace_id, draft_id):
    return await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/drafts/{draft_id}/apply",
        headers=headers, json={"user_confirmation": True},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["initialize", "purpose", "remove"])
async def test_knowledge_write_survives_an_older_integration_draft(
    client, db_session, mutation,
):
    """A Knowledge write bumps the operation revision; a draft made before it
    still applies (#1572 rebases it) and keeps the Knowledge change."""
    headers, workspace_id = await _workspace(client)
    group_id = None
    if mutation != "initialize":
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents/groups", headers=headers,
            json={"name": "Reference"},
        )
        assert response.status_code == 201, response.text
        group_id = response.json()["id"]
        response = await client.put(
            f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}",
            headers=headers, json={"purpose": "Original purpose"},
        )
        assert response.status_code == 200, response.text

    draft = await _integration_draft(client, headers, workspace_id)
    if mutation == "initialize":
        changed = await client.get(
            f"/api/v1/workspaces/{workspace_id}/documents", headers=headers,
        )
        assert changed.status_code == 200, changed.text
        group_id = next(row["id"] for row in changed.json() if row["is_default_collection"])
    elif mutation == "purpose":
        changed = await client.put(
            f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}",
            headers=headers, json={"purpose": "Current purpose"},
        )
        assert changed.status_code == 200, changed.text
    else:
        changed = await client.delete(
            f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}", headers=headers,
        )
        assert changed.status_code == 204, changed.text

    applied = await _apply(client, headers, workspace_id, draft["id"])
    assert applied.status_code == 200, applied.text
    workspace = await db_session.get(Workspace, workspace_id, populate_existing=True)
    assert workspace.operation_revision == draft["base_revision"] + 2
    knowledge = workspace.operating_model["knowledge"]
    if mutation == "initialize":
        assert knowledge["default_group_ids"] == [group_id]
    elif mutation == "purpose":
        assert knowledge["group_purposes"][group_id] == "Current purpose"
    else:
        assert group_id not in knowledge.get("group_purposes", {})
        assert await db_session.get(DocumentGroup, group_id) is None
    # The draft's own change lands beside it.
    assert "slack" in workspace.operating_model.get("integration_scope", {}).get("disabled_providers", [])


@pytest.mark.asyncio
async def test_default_collection_and_purpose_retries_do_not_invalidate_a_draft(client, db_session, monkeypatch):
    headers, workspace_id = await _workspace(client)
    endpoint = f"/api/v1/workspaces/{workspace_id}/documents"
    initialized = await client.get(endpoint, headers=headers)
    assert initialized.status_code == 200, initialized.text
    group_id = next(row["id"] for row in initialized.json() if row["is_default_collection"])
    purpose_url = f"{endpoint}/groups/{group_id}"
    response = await client.put(purpose_url, headers=headers, json={"purpose": "Reference material"})
    assert response.status_code == 200, response.text
    draft = await _integration_draft(client, headers, workspace_id)

    with monkeypatch.context() as patch:
        lock = AsyncMock(side_effect=AssertionError("an initialized list read must not take a write lock"))
        patch.setattr(router, "lock_workspace_access_boundary", lock)
        assert (await client.get(endpoint, headers=headers)).status_code == 200
        lock.assert_not_awaited()
    repeated_create = await client.post(
        f"{endpoint}/groups", headers=headers,
        json={"name": "Ignored default name", "kind": "workspace_collection"},
    )
    assert repeated_create.status_code == 201, repeated_create.text
    assert repeated_create.json()["id"] == group_id
    repeated_purpose = await client.put(purpose_url, headers=headers, json={"purpose": "Reference material"})
    assert repeated_purpose.status_code == 200, repeated_purpose.text
    workspace = await db_session.get(Workspace, workspace_id, populate_existing=True)
    assert workspace.operation_revision == draft["base_revision"]

    applied = await _apply(client, headers, workspace_id, draft["id"])
    assert applied.status_code == 200, applied.text
    await db_session.refresh(workspace)
    assert workspace.operating_model["knowledge"]["default_group_ids"] == [group_id]
    assert workspace.operating_model["knowledge"]["group_purposes"][group_id] == "Reference material"


@pytest.mark.asyncio
async def test_concurrent_lazy_initialization_creates_one_collection(client, db_session, monkeypatch):
    headers, workspace_id = await _workspace(client)
    entered = asyncio.Event()
    second_waiting = asyncio.Event()
    release = asyncio.Event()
    original = router._ensure_default_workspace_collection
    original_lock = router.lock_workspace_access_boundary
    calls = 0
    lock_calls = 0

    async def observed_lock(*args, **kwargs):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            second_waiting.set()
        return await original_lock(*args, **kwargs)

    async def paused_initialize(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(router, "_ensure_default_workspace_collection", paused_initialize)
    monkeypatch.setattr(router, "lock_workspace_access_boundary", observed_lock)
    endpoint = f"/api/v1/workspaces/{workspace_id}/documents"
    first = asyncio.create_task(client.get(endpoint, headers=headers))
    await asyncio.wait_for(entered.wait(), timeout=10)
    second = asyncio.create_task(client.get(endpoint, headers=headers))
    try:
        await asyncio.wait_for(second_waiting.wait(), timeout=10)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), timeout=0.1)
    finally:
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    assert all(response.status_code == 200 for response in responses)
    groups = list((await db_session.execute(
        select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id),
    )).scalars())
    defaults = [group for group in groups if group.settings.get("default_collection")]
    assert len(defaults) == 1
    workspace = await db_session.get(Workspace, workspace_id, populate_existing=True)
    assert workspace.operation_revision == 1
    assert workspace.operating_model["knowledge"]["default_group_ids"] == [defaults[0].id]


@pytest.mark.asyncio
async def test_knowledge_update_refreshes_workspace_loaded_before_another_apply(client, db_session):
    headers, workspace_id = await _workspace(client)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups", headers=headers,
        json={"name": "Reference"},
    )
    assert created.status_code == 201, created.text
    workspace = await db_session.get(Workspace, workspace_id)
    stale_revision = workspace.operation_revision
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = await db_session.get(User, me["id"])
    draft = await _integration_draft(client, headers, workspace_id)
    applied = await _apply(client, headers, workspace_id, draft["id"])
    assert applied.status_code == 200, applied.text

    await router.update_workspace_document_group(
        workspace_id, created.json()["id"],
        router.WorkspaceKnowledgeGroupUpdateRequest(purpose="Current reference"),
        user=user, db=db_session,
    )
    await db_session.refresh(workspace)
    assert workspace.operation_revision == stale_revision + 2
    assert workspace.operating_model["integration_scope"]["disabled_providers"] == ["slack"]
    assert workspace.operating_model["knowledge"]["group_purposes"][created.json()["id"]] == "Current reference"


def test_removing_unreferenced_group_does_not_write_empty_knowledge_config():
    workspace = Workspace(operating_model={"services": []}, operation_revision=3)
    assert router._remove_group_from_operating_model(workspace, "unused") is False
    assert router._set_group_purpose_in_operating_model(workspace, "unused", "") is False
    assert workspace.operating_model == {"services": []}
    assert workspace.operation_revision == 3
