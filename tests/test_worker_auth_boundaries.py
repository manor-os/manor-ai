import pytest
from pydantic import ValidationError


def test_worker_registration_schema_only_accepts_protocol_v2():
    from apps.api.routers.workers import WorkerCapabilities
    from packages.core.workers import (
        CURRENT_WORKER_PROTOCOL_VERSION,
        WorkerProtocolVersion,
    )
    from packages.worker_sdk.client import ManorClient

    assert WorkerCapabilities(protocol_version=2).protocol_version is WorkerProtocolVersion.V2
    assert ManorClient.PROTOCOL_VERSION == str(int(CURRENT_WORKER_PROTOCOL_VERSION))
    with pytest.raises(ValidationError):
        WorkerCapabilities(protocol_version=1)
    with pytest.raises(ValidationError):
        WorkerCapabilities()


@pytest.mark.asyncio
async def test_user_jwt_and_worker_secret_are_not_interchangeable(
    client,
    db_session,
):
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
                "protocol_version": 2,
            },
        },
    )
    assert worker_register.status_code == 201, worker_register.text
    worker_payload = worker_register.json()
    worker_headers = {
        "Authorization": f"Bearer {worker_payload['worker_secret']}",
        "Manor-Worker-Id": worker_payload["worker_id"],
        "Manor-Protocol-Version": "2",
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

    from packages.core.models.worker import Worker, WorkerActivityLog
    from packages.core.constants.execution import WorkerStatus
    from sqlalchemy import select

    worker = await db_session.get(Worker, worker_payload["worker_id"])
    assert worker is not None
    worker.status = WorkerStatus.OFFLINE.value
    await db_session.commit()

    reconnected = await client.post(
        "/api/v1/workers/heartbeat",
        headers=worker_headers,
        json={"state": "idle", "capacity": {"can_accept_leases": 0}},
    )
    assert reconnected.status_code == 200, reconnected.text
    await db_session.refresh(worker)
    reconnect_activity = await db_session.scalar(
        select(WorkerActivityLog).where(
            WorkerActivityLog.worker_id == worker.id,
            WorkerActivityLog.event == "reconnected",
        )
    )
    assert worker.status == WorkerStatus.ACTIVE.value
    assert reconnect_activity is not None

    missing_protocol = dict(worker_headers)
    missing_protocol.pop("Manor-Protocol-Version")
    rejected = await client.post(
        "/api/v1/workers/heartbeat",
        headers=missing_protocol,
        json={"state": "idle", "capacity": {"can_accept_leases": 0}},
    )
    assert rejected.status_code == 426

    wrong_protocol = {**worker_headers, "Manor-Protocol-Version": "1"}
    rejected = await client.post(
        "/api/v1/workers/heartbeat",
        headers=wrong_protocol,
        json={"state": "idle", "capacity": {"can_accept_leases": 0}},
    )
    assert rejected.status_code == 426


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
                    "protocol_version": 2,
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
