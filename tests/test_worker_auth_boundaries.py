import pytest


@pytest.mark.asyncio
async def test_user_jwt_and_worker_secret_are_not_interchangeable(client):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "worker_auth_boundary",
            "email": "worker-auth-boundary@example.com",
            "password": "pass123",
        },
    )
    assert register.status_code == 200, register.text
    user_token = register.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    worker_register = await client.post(
        "/api/v1/workers/register",
        headers=user_headers,
        json={
            "kind": "custom_http",
            "display_name": "Boundary Worker",
            "capabilities": {
                "supported_kinds": ["action"],
                "supported_providers": ["browser_mcp"],
                "supported_capabilities": [],
                "max_concurrent_leases": 1,
                "max_risk_level": "low",
                "uses_manor_credentials": False,
                "deployment": "local",
                "protocol_version": 1,
            },
        },
    )
    assert worker_register.status_code == 201, worker_register.text
    worker_payload = worker_register.json()
    worker_headers = {
        "Authorization": f"Bearer {worker_payload['worker_secret']}",
        "Manor-Worker-Id": worker_payload["worker_id"],
    }

    user_catalog = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=user_headers,
    )
    assert user_catalog.status_code == 200, user_catalog.text

    worker_on_user_endpoint = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=worker_headers,
    )
    assert worker_on_user_endpoint.status_code == 401

    user_on_worker_endpoint = await client.post(
        "/api/v1/workers/heartbeat",
        headers=user_headers,
        json={"state": "idle", "capacity": {"can_accept_leases": 0}},
    )
    assert user_on_worker_endpoint.status_code == 401

    worker_heartbeat = await client.post(
        "/api/v1/workers/heartbeat",
        headers=worker_headers,
        json={"state": "idle", "capacity": {"can_accept_leases": 0}},
    )
    assert worker_heartbeat.status_code == 200, worker_heartbeat.text


@pytest.mark.asyncio
async def test_user_can_rename_owned_local_computer_with_unique_name(client):
    register_user = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "worker_rename_owner",
            "email": "worker-rename-owner@example.com",
            "password": "pass123",
        },
    )
    assert register_user.status_code == 200, register_user.text
    headers = {
        "Authorization": f"Bearer {register_user.json()['access_token']}"
    }

    worker_ids = []
    for display_name in ("CLI · Office MacBook", "Home PC"):
        registered = await client.post(
            "/api/v1/workers/register",
            headers=headers,
            json={
                "kind": "custom_http",
                "display_name": display_name,
                "capabilities": {
                    "supported_kinds": ["action"],
                    "max_concurrent_leases": 1,
                    "max_risk_level": "low",
                    "uses_manor_credentials": False,
                    "deployment": "local",
                    "protocol_version": 1,
                },
            },
        )
        assert registered.status_code == 201, registered.text
        worker_ids.append(registered.json()["worker_id"])

    renamed = await client.patch(
        f"/api/v1/workers/{worker_ids[0]}",
        headers=headers,
        json={"display_name": "Studio Mac"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "Studio Mac"

    duplicate = await client.patch(
        f"/api/v1/workers/{worker_ids[0]}",
        headers=headers,
        json={"display_name": "  home   pc  "},
    )
    assert duplicate.status_code == 409, duplicate.text
