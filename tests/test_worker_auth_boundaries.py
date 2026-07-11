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
