"""E2E tests: workspace CRUD."""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, username: str = "wsuser") -> tuple[str, dict]:
    """Register and return (token, headers)."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "WS Corp",
        },
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_create_workspace(client: AsyncClient):
    _, headers = await _register(client)
    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "My Project",
            "description": "A test workspace",
            "category": "development",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Project"
    assert data["description"] == "A test workspace"
    assert data["category"] == "development"
    assert data["id"]
    assert data["heartbeat_enabled"] is False


@pytest.mark.asyncio
async def test_create_workspace_matches_business_ledgers_and_exposes_overview(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    from packages.core import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )
    _, headers = await _register(client, "wsrecruiting")
    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Talent Operations",
            "operating_context": "Recruit candidates and manage employee onboarding.",
            "primary_work": "Run interviews and the hiring pipeline.",
        },
    )
    assert resp.status_code == 201, resp.text
    workspace = resp.json()
    assert workspace["settings"]["ledger_contracts"] == [{
        "contract_id": "manor.recruiting_ledger/v1",
        "schema_version": 1,
        "directory": "recruiting-ledger",
    }]

    overview = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/ledgers/overview",
        headers=headers,
    )
    assert overview.status_code == 200, overview.text
    assert overview.json()["ledgers"][0]["projection_kind"] == "current"

    configured = await client.put(
        f"/api/v1/workspaces/{workspace['id']}/ledgers/configuration",
        headers=headers,
        json={"ledger_contracts": ["finance_ledger"]},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["ledger_contracts"][0]["contract_id"] == (
        "manor.finance_ledger/v1"
    )

    refreshed_overview = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/ledgers/overview",
        headers=headers,
    )
    assert refreshed_overview.status_code == 200, refreshed_overview.text
    assert refreshed_overview.json()["ledger_count"] == 1
    assert refreshed_overview.json()["ledgers"][0]["contract_id"] == (
        "manor.finance_ledger/v1"
    )


@pytest.mark.asyncio
async def test_workspace_ledger_configuration_preserves_legacy_storage_config(
    client: AsyncClient,
):
    _, headers = await _register(client, "wslegacyledger")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Legacy workspace"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
        json={
            "settings": {
                "content_ledger": {
                    "contract_id": "manor.content_ledger/v1",
                    "directory": "topic-ledger",
                    "legacy_directories": ["content-ledger"],
                    "legacy_identity_fields": ["selected_topic"],
                },
            },
        },
    )
    assert updated.status_code == 200, updated.text

    configured = await client.put(
        f"/api/v1/workspaces/{workspace_id}/ledgers/configuration",
        headers=headers,
        json={"ledger_contracts": ["content_ledger", "finance_ledger"]},
    )
    assert configured.status_code == 200, configured.text
    contracts = {
        item["contract_id"]: item
        for item in configured.json()["ledger_contracts"]
    }
    assert contracts["manor.content_ledger/v1"] == {
        "contract_id": "manor.content_ledger/v1",
        "schema_version": 1,
        "directory": "topic-ledger",
        "legacy_directories": ["content-ledger"],
        "legacy_identity_fields": ["selected_topic"],
    }


@pytest.mark.asyncio
async def test_workspace_ledger_overview_returns_503_when_storage_is_unavailable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import workspace_ledger_overview as overview_module

    _, headers = await _register(client, "wsledgeroutage")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Recruiting Operations",
            "primary_work": "Manage candidates and interviews.",
        },
    )
    assert created.status_code == 201, created.text

    async def unavailable(**_kwargs):
        raise overview_module.RecruitingLedgerError(
            "filesystem_unavailable",
            "Workspace filesystem is unavailable",
        )

    monkeypatch.setattr(overview_module, "read_recruiting_ledger", unavailable)
    response = await client.get(
        f"/api/v1/workspaces/{created.json()['id']}/ledgers/overview",
        headers=headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "filesystem_unavailable"


@pytest.mark.asyncio
async def test_create_autonomous_workspace_without_goal_installs_runtime(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import select

    from apps.api.routers import workspaces as workspaces_router
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    sync_calls: list[str] = []
    original_sync = workspaces_router.sync_workspace_runtime_schedules

    async def tracked_sync(db, workspace):
        sync_calls.append(workspace.id)
        await original_sync(db, workspace)

    monkeypatch.setattr(
        workspaces_router,
        "sync_workspace_runtime_schedules",
        tracked_sync,
    )

    _, headers = await _register(client, "wsautonomous")
    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Autonomous, No Goal",
            "heartbeat_enabled": True,
            "heartbeat_cadence": "daily",
        },
    )

    assert resp.status_code == 201
    workspace = resp.json()
    assert workspace["heartbeat_enabled"] is True
    assert (await db_session.execute(
        select(Goal).where(Goal.workspace_id == workspace["id"])
    )).scalars().all() == []
    strategist_job = (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == f"sr:{workspace['id']}")
    )).scalar_one_or_none()
    assert strategist_job is not None
    assert strategist_job.enabled is True
    assert sync_calls == [workspace["id"]]


@pytest.mark.asyncio
async def test_create_workspace_enforces_plan_limit_without_stale_cache(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Free tenants should not be able to create a second workspace by
    reusing a cached positive plan-gate result from the first request."""
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    plan_gate.invalidate_gate_cache()

    _, headers = await _register(client, "workspace_limit_cache")

    first = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Allowed Workspace"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Blocked Workspace"},
    )
    assert second.status_code == 402, second.text
    assert second.json()["detail"]["kind"] == "workspaces"


@pytest.mark.asyncio
async def test_delete_workspace_immediately_frees_plan_slot(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """A delete response must make the freed workspace slot visible to the
    very next create request; clients should not need to wait for the
    dependency cleanup commit."""
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    plan_gate.invalidate_gate_cache()

    _, headers = await _register(client, "workspace_delete_frees_slot")

    original = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Original"},
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]

    deleted = await client.delete(f"/api/v1/workspaces/{original_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    replacement = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Replacement"},
    )
    assert replacement.status_code == 201, replacement.text


@pytest.mark.asyncio
async def test_restore_workspace_enforces_plan_limit(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Restoring a trashed workspace should not bypass the active
    workspace cap."""
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    plan_gate.invalidate_gate_cache()

    _, headers = await _register(client, "workspace_restore_limit")

    original = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Original"},
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]

    deleted = await client.delete(f"/api/v1/workspaces/{original_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    replacement = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Replacement"},
    )
    assert replacement.status_code == 201, replacement.text

    restored = await client.post(f"/api/v1/workspaces/{original_id}/restore", headers=headers)
    assert restored.status_code == 402, restored.text
    assert restored.json()["detail"]["kind"] == "workspaces"


@pytest.mark.asyncio
async def test_list_workspaces(client: AsyncClient):
    _, headers = await _register(client)
    # Create 3 workspaces
    for name in ["Alpha", "Beta", "Gamma"]:
        await client.post("/api/v1/workspaces", headers=headers, json={"name": name})

    resp = await client.get("/api/v1/workspaces", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    names = {ws["name"] for ws in data}
    assert names == {"Alpha", "Beta", "Gamma"}


@pytest.mark.asyncio
async def test_get_workspace(client: AsyncClient):
    _, headers = await _register(client)
    create_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "GetMe"})
    ws_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "GetMe"


@pytest.mark.asyncio
async def test_generic_workspace_update_cannot_forge_blueprint_setup_contract(
    client: AsyncClient,
):
    _, headers = await _register(client, "workspace_setup_status")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Setup status"},
    )
    workspace_id = created.json()["id"]

    ready = await client.get(
        f"/api/v1/workspaces/{workspace_id}/setup-status",
        headers=headers,
    )
    assert ready.status_code == 200, ready.text
    assert ready.json()["ready"] is True

    rejected = await client.put(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
        json={
            "settings": {
                "_blueprint": {
                    "install_todos": [{
                        "kind": "note",
                        "detail": "Complete the Blueprint-specific setup.",
                        "payload": {},
                        "blocking": True,
                    }],
                },
            },
        },
    )
    assert rejected.status_code == 400, rejected.text

    still_ready = await client.get(
        f"/api/v1/workspaces/{workspace_id}/setup-status",
        headers=headers,
    )
    assert still_ready.status_code == 200, still_ready.text
    assert still_ready.json()["ready"] is True


@pytest.mark.asyncio
async def test_update_workspace(client: AsyncClient):
    _, headers = await _register(client)
    create_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Old Name"})
    ws_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=headers,
        json={
            "name": "New Name",
            "address": "456 Oak Ave",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["address"] == "456 Oak Ave"


@pytest.mark.asyncio
async def test_update_workspace_explicit_null_clears_optional_field(client: AsyncClient):
    _, headers = await _register(client, "workspace_null_clear")
    create_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Nullable", "identity_label": "keep-me"},
    )
    assert create_resp.status_code == 201, create_resp.text
    ws_id = create_resp.json()["id"]
    assert create_resp.json()["identity_label"] == "keep-me"

    cleared = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=headers,
        json={"identity_label": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["identity_label"] is None

    refreshed = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["identity_label"] is None


@pytest.mark.asyncio
async def test_update_workspace_omitted_optional_field_is_preserved(client: AsyncClient):
    _, headers = await _register(client, "workspace_omitted_preserved")
    create_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Preserve", "identity_label": "keep-me"},
    )
    assert create_resp.status_code == 201, create_resp.text
    ws_id = create_resp.json()["id"]

    updated = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=headers,
        json={"name": "Preserved name"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["identity_label"] == "keep-me"


@pytest.mark.asyncio
async def test_delete_workspace(client: AsyncClient):
    _, headers = await _register(client)
    create_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "ToDelete"})
    ws_id = create_resp.json()["id"]

    # Delete
    resp = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp.status_code == 204

    # Verify gone
    resp2 = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_workspace_isolation(client: AsyncClient):
    """User A can't see User B's workspaces."""
    _, headers_a = await _register(client, "user_a")
    _, headers_b = await _register(client, "user_b")

    # A creates a workspace
    create_resp = await client.post("/api/v1/workspaces", headers=headers_a, json={"name": "A's Project"})
    ws_id = create_resp.json()["id"]

    # B can't see it
    resp = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers_b)
    assert resp.status_code == 404

    # B's list is empty
    resp2 = await client.get("/api/v1/workspaces", headers=headers_b)
    assert len(resp2.json()) == 0
