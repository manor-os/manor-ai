from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import httpx
import pytest

from packages.worker_sdk.client import ManorClient, WorkerClientError
from packages.worker_sdk.types import (
    HeartbeatCompletedLease,
    HeartbeatRequest,
    HeartbeatResponse,
    Lease,
    LeaseResult,
    TaskOutputValueKind,
)
from packages.worker_sdk.worker import ManorWorker


def _lease() -> Lease:
    return Lease(
        lease_id="lease-terminal",
        attempt_token="attempt-terminal",
        step_id="step-terminal",
        plan_id="plan-terminal",
        workspace_id="workspace-terminal",
        kind="action",
        provider="test",
        action_key="write",
        integration_id=None,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        execution_mode="live",
    )


def _heartbeat_response() -> HeartbeatResponse:
    return HeartbeatResponse(
        server_time=datetime.now(timezone.utc),
        next_heartbeat_in_seconds=1,
        new_leases=[],
        instructions=[],
    )


@pytest.mark.asyncio
async def test_python_client_echoes_attempt_token_on_every_lifecycle_call(
    monkeypatch,
) -> None:
    client = ManorClient(
        "https://manor.test",
        worker_id="worker-token",
        secret="secret-token",
    )
    calls: list[tuple[str, dict]] = []

    async def fake_post(path, *, json, **_kwargs):
        calls.append((path, json))
        return {}

    monkeypatch.setattr(client, "_post", fake_post)
    await client.complete_lease(
        "lease-token",
        LeaseResult(result={"ok": True}),
        attempt_token="attempt-token",
    )
    await client.fail_lease(
        "lease-token",
        error={"type": "Failure"},
        attempt_token="attempt-token",
    )
    await client.need_human(
        "lease-token",
        prompt="help",
        attempt_token="attempt-token",
    )
    await client.extend_lease(
        "lease-token",
        attempt_token="attempt-token",
    )

    assert len(calls) == 4
    assert all(payload["attempt_token"] == "attempt-token" for _path, payload in calls)


@pytest.mark.asyncio
async def test_success_report_timeout_retries_same_outcome_without_failing_or_rerunning() -> None:
    class FlakyClient:
        def __init__(self) -> None:
            self.complete_calls = 0
            self.fail_calls = 0
            self.heartbeat_requests: list[HeartbeatRequest] = []
            self.heartbeat_failures = 1

        async def complete_lease(self, *_args, **_kwargs) -> None:
            self.complete_calls += 1
            raise WorkerClientError("completion response timed out")

        async def fail_lease(self, *_args, **_kwargs) -> None:
            self.fail_calls += 1

        async def heartbeat(self, request: HeartbeatRequest) -> HeartbeatResponse:
            self.heartbeat_requests.append(request)
            if self.heartbeat_failures:
                self.heartbeat_failures -= 1
                raise WorkerClientError("heartbeat response timed out")
            return _heartbeat_response()

    client = FlakyClient()
    worker = ManorWorker(
        endpoint="https://manor.test",
        worker_id="worker-terminal",
        secret="secret-terminal",
        client=client,  # type: ignore[arg-type]
    )
    handler_calls = 0
    handler_payload = {"provider_receipt": "receipt-1"}

    @worker.handle(kind="action", provider="test")
    async def handle(_lease, _ctx):
        nonlocal handler_calls
        handler_calls += 1
        return handler_payload

    lease = _lease()
    await worker._run_handler(lease)

    assert handler_calls == 1
    assert client.complete_calls == 1
    assert client.fail_calls == 0
    assert len(worker._completions) == 1
    queued = worker._completions[0]
    assert queued.status == "done"
    assert queued.result == {"provider_receipt": "receipt-1"}

    # The terminal envelope owns a deep snapshot, not the mutable object the
    # application handler returned.
    handler_payload["provider_receipt"] = "mutated"
    assert queued.result == {"provider_receipt": "receipt-1"}

    # A replay received before the terminal report is acknowledged must not
    # invoke application code for the same lease again.
    worker._spawn_handler(lease)
    await asyncio.sleep(0)
    assert handler_calls == 1

    with pytest.raises(WorkerClientError, match="heartbeat response timed out"):
        await worker._tick()
    assert worker._completions == [queued]

    await worker._tick()
    assert worker._completions == []
    assert len(client.heartbeat_requests) == 2
    first = client.heartbeat_requests[0].completed_since_last[0]
    second = client.heartbeat_requests[1].completed_since_last[0]
    assert first.model_dump() == second.model_dump() == queued.model_dump()
    assert client.fail_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("register_handler", "expected_error_type"),
    [(False, "NoHandler"), (True, "RuntimeError")],
)
async def test_failure_report_timeout_keeps_failed_terminal_envelope(
    register_handler: bool,
    expected_error_type: str,
) -> None:
    class FlakyFailureClient:
        def __init__(self) -> None:
            self.fail_calls = 0

        async def fail_lease(self, *_args, **_kwargs) -> None:
            self.fail_calls += 1
            raise WorkerClientError("failure response timed out")

    client = FlakyFailureClient()
    worker = ManorWorker(
        endpoint="https://manor.test",
        worker_id="worker-terminal",
        secret="secret-terminal",
        client=client,  # type: ignore[arg-type]
    )
    if register_handler:

        @worker.handle(kind="action", provider="test")
        async def handle(_lease, _ctx):
            raise RuntimeError("application failed")

    await worker._run_handler(_lease())

    assert client.fail_calls == 1
    assert len(worker._completions) == 1
    completion = worker._completions[0]
    assert completion.status == "failed"
    assert completion.error is not None
    assert completion.error["type"] == expected_error_type


@pytest.mark.asyncio
async def test_heartbeat_ack_removes_only_the_sent_completion_snapshot() -> None:
    class BlockingClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def heartbeat(self, _request: HeartbeatRequest) -> HeartbeatResponse:
            self.started.set()
            await self.release.wait()
            return _heartbeat_response()

    client = BlockingClient()
    worker = ManorWorker(
        endpoint="https://manor.test",
        worker_id="worker-terminal",
        secret="secret-terminal",
        client=client,  # type: ignore[arg-type]
    )
    first = HeartbeatCompletedLease(
        lease_id="lease-first",
        status="done",
        result={"n": 1},
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    second = HeartbeatCompletedLease(
        lease_id="lease-second",
        status="done",
        result={"n": 2},
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    assert worker._queue_completion(first)

    tick = asyncio.create_task(worker._tick())
    await client.started.wait()
    assert worker._queue_completion(second)
    client.release.set()
    await tick

    assert [completion.lease_id for completion in worker._completions] == [
        "lease-second"
    ]


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
