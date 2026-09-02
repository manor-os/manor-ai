from __future__ import annotations

from http import HTTPStatus

import httpx
import pytest

from packages.worker_sdk.client import ManorClient, WorkerClientError
from packages.worker_sdk.types import HeartbeatRequest
from packages.worker_sdk.worker import ManorWorker


@pytest.mark.asyncio
async def test_protocol_upgrade_response_fails_fast_without_retrying() -> None:
    calls = 0

    async def reject_registration(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            HTTPStatus.UPGRADE_REQUIRED,
            request=request,
            json={"detail": "re-register worker"},
        )

    client = ManorClient(
        "https://manor.test",
        worker_id="worker-legacy",
        secret="secret-legacy",
        transport=httpx.MockTransport(reject_registration),
    )
    try:
        with pytest.raises(WorkerClientError) as raised:
            await client.heartbeat(HeartbeatRequest())
    finally:
        await client.close()

    assert raised.value.status_code == HTTPStatus.UPGRADE_REQUIRED
    assert raised.value.requires_operator_action is True
    assert calls == 1


@pytest.mark.parametrize(
    "status_code, expected",
    [
        (HTTPStatus.UNAUTHORIZED, True),
        (HTTPStatus.FORBIDDEN, True),
        (HTTPStatus.UPGRADE_REQUIRED, True),
        (HTTPStatus.TOO_MANY_REQUESTS, False),
        (HTTPStatus.INTERNAL_SERVER_ERROR, False),
    ],
)
def test_operator_action_status_classification(
    status_code: HTTPStatus,
    expected: bool,
) -> None:
    error = WorkerClientError("worker request failed", status_code=status_code)
    assert error.requires_operator_action is expected


@pytest.mark.asyncio
async def test_worker_loop_stops_after_protocol_registration_rejection() -> None:
    class RejectingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def heartbeat(self, _request: HeartbeatRequest) -> None:
            self.calls += 1
            raise WorkerClientError(
                "registration incompatible",
                status_code=HTTPStatus.UPGRADE_REQUIRED,
            )

    client = RejectingClient()
    worker = ManorWorker(
        endpoint="https://manor.test",
        worker_id="worker-legacy",
        secret="secret-legacy",
        client=client,  # type: ignore[arg-type]
    )

    await worker._loop()

    assert worker._stop.is_set()
    assert client.calls == 1
