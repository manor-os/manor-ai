from __future__ import annotations

import json

import httpx
import pytest

from packages.core.services.sandbox_sdk.client import SandboxClient, _raise_for_status
from packages.core.services.sandbox_sdk.exceptions import SandboxError


def test_sandbox_sdk_maps_429_to_capacity_error() -> None:
    response = httpx.Response(
        429,
        json={"detail": "Max active sandbox limit reached (3)"},
        request=httpx.Request("POST", "http://sandbox-service/api/v1/sandbox/create-from-files"),
    )

    with pytest.raises(SandboxError) as exc_info:
        _raise_for_status(response)

    assert type(exc_info.value).__name__ == "SandboxCapacityError"
    assert exc_info.value.status_code == 429
    assert "Max active sandbox limit reached" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sandbox_client_sends_optional_api_token_header() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Manor-Sandbox-Token", "")
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = SandboxClient(
        base_url="http://sandbox-service",
        timeout=30.0,
        api_token="runner-secret",
        transport=transport,
    )
    try:
        await client.list()
    finally:
        await client.close()

    assert seen["token"] == "runner-secret"


@pytest.mark.asyncio
async def test_sandbox_client_sends_idempotency_key_only_when_requested() -> None:
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        skill_name = bodies[-1]["skill_name"]
        return httpx.Response(
            200,
            json={
                "sandbox_id": f"sandbox-{len(bodies)}",
                "container_name": f"container-{len(bodies)}",
                "status": "ready",
                "skill": {"name": skill_name, "skill_dir": "/skill"},
                "workdir": "/skill",
                "env_blocked": [],
            },
        )

    client = SandboxClient(
        base_url="http://sandbox-service",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.create_from_files(
            "workspace-skill",
            {"SKILL.md": "# Workspace"},
            idempotency_key="reservation-1",
        )
        await client.create_from_builtin(
            "builtin-skill",
            {"SKILL.md": "# Builtin"},
        )
    finally:
        await client.close()

    assert bodies[0]["idempotency_key"] == "reservation-1"
    assert "idempotency_key" not in bodies[1]


@pytest.mark.asyncio
async def test_sandbox_client_propagates_execution_identity_and_cancel() -> None:
    requests: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.path.endswith("/cancel"):
            return httpx.Response(
                200,
                json={
                    "sandbox_id": "sandbox-1",
                    "execution_id": "execution-1",
                    "cancelled": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "execution_id": "execution-1",
            },
        )

    client = SandboxClient(
        base_url="http://sandbox-service",
        transport=httpx.MockTransport(handler),
    )
    try:
        execution = await client.exec(
            "sandbox-1",
            "sleep 60",
            execution_id="execution-1",
        )
        cancelled = await client.cancel_execution("sandbox-1", "execution-1")
    finally:
        await client.close()

    assert execution.execution_id == "execution-1"
    assert cancelled.cancelled is True
    assert requests == [
        (
            "/api/v1/sandbox/sandbox-1/exec",
            {"command": "sleep 60", "timeout": 60, "execution_id": "execution-1"},
        ),
        (
            "/api/v1/sandbox/sandbox-1/executions/execution-1/cancel",
            {},
        ),
    ]


@pytest.mark.asyncio
async def test_sandbox_client_starts_and_polls_background_execution() -> None:
    requests: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "sandbox_id": "sandbox-1",
                    "execution_id": "execution-1",
                    "status": "queued",
                    "created_at": 10.0,
                },
            )
        return httpx.Response(
            200,
            json={
                "sandbox_id": "sandbox-1",
                "execution_id": "execution-1",
                "status": "completed",
                "created_at": 10.0,
                "started_at": 11.0,
                "finished_at": 12.0,
                "stdout": "done\n",
                "stderr": "",
                "exit_code": 0,
            },
        )

    client = SandboxClient(
        base_url="http://sandbox-service",
        transport=httpx.MockTransport(handler),
    )
    try:
        started = await client.start_exec(
            "sandbox-1",
            "sleep 1; echo done",
            execution_id="execution-1",
        )
        completed = await client.execution_status("sandbox-1", "execution-1")
    finally:
        await client.close()

    assert started.status == "queued"
    assert started.terminal is False
    assert completed.status == "completed"
    assert completed.terminal is True
    assert completed.stdout == "done\n"
    assert requests == [
        (
            "POST",
            "/api/v1/sandbox/sandbox-1/exec/start",
            {
                "command": "sleep 1; echo done",
                "timeout": 60,
                "execution_id": "execution-1",
            },
        ),
        (
            "GET",
            "/api/v1/sandbox/sandbox-1/executions/execution-1",
            {},
        ),
    ]


@pytest.mark.asyncio
async def test_sandbox_client_polls_events_and_sends_response() -> None:
    requests: list[tuple[str, str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append(
            (request.method, request.url.path, request.url.query.decode(), body)
        )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sandbox_id": "sandbox-1",
                    "execution_id": "execution-1",
                    "status": "running",
                    "created_at": 10.0,
                    "events": [
                        {
                            "sequence": 4,
                            "event_id": "input-1",
                            "type": "need_input",
                            "message": "Choose a format",
                            "payload": {"choices": ["csv", "json"]},
                            "requires_response": True,
                            "responded": False,
                            "created_at": 11.0,
                        }
                    ],
                    "next_sequence": 4,
                    "waiting_for_response": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "sandbox_id": "sandbox-1",
                "execution_id": "execution-1",
                "event_id": "input-1",
                "accepted": True,
                "duplicate": False,
            },
        )

    client = SandboxClient(
        base_url="http://sandbox-service",
        transport=httpx.MockTransport(handler),
    )
    try:
        status = await client.execution_status(
            "sandbox-1",
            "execution-1",
            after_sequence=3,
        )
        accepted = await client.send_execution_response(
            "sandbox-1",
            "execution-1",
            "input-1",
            payload={"choice": "json"},
            message="Use JSON",
        )
    finally:
        await client.close()

    assert status.waiting_for_response is True
    assert status.next_sequence == 4
    assert status.events[0].type == "need_input"
    assert accepted.accepted is True
    assert requests == [
        (
            "GET",
            "/api/v1/sandbox/sandbox-1/executions/execution-1",
            "after_sequence=3",
            {},
        ),
        (
            "POST",
            "/api/v1/sandbox/sandbox-1/executions/execution-1/responses",
            "",
            {
                "event_id": "input-1",
                "payload": {"choice": "json"},
                "message": "Use JSON",
            },
        ),
    ]
