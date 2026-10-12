from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from packages.worker_sdk import client as sdk
from packages.worker_sdk.types import HeartbeatRequest, LeaseResult


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, sleeps=[])

    async def sleep(delay):
        state.sleeps.append(delay)
        state.now += delay

    monkeypatch.setattr(sdk, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(sdk, "random", SimpleNamespace(uniform=lambda low, high: high))
    monkeypatch.setattr(sdk.asyncio, "sleep", sleep)
    return state


@pytest.mark.asyncio
async def test_retry_after_is_a_floor_and_heartbeat_retries_keep_the_same_payload(clock):
    calls = []

    def respond(request):
        calls.append((clock.now, request.content))
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "5"}, json={"detail": {"code": "database_busy"}})
        return httpx.Response(200, json={
            "server_time": datetime.now(timezone.utc).isoformat(),
            "next_heartbeat_in_seconds": 10, "new_leases": [], "instructions": [],
        })

    async with sdk.ManorClient("http://test", worker_id="worker", secret="secret", transport=httpx.MockTransport(respond)) as client:
        await client.heartbeat(HeartbeatRequest(state="idle"))
    assert len(calls) == 2
    assert calls[1][0] >= 5
    assert calls[0][1] == calls[1][1]


@pytest.mark.asyncio
async def test_terminal_503_is_not_replayed_and_next_request_obeys_shared_backpressure(clock):
    calls = []

    def respond(request):
        calls.append((clock.now, request.url.path))
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "30"}, json={"detail": "busy"})
        return httpx.Response(200, json={})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(sdk.WorkerClientError) as rejected:
            await client.complete_lease("lease", LeaseResult(result={"receipt": "done"}), attempt_token="attempt")
        assert rejected.value.retry_after_seconds == 30
        assert len(calls) == 1
        await client._post("/api/v1/workers/heartbeat", json={})
    assert len(calls) == 2
    assert calls[1][0] >= 30


@pytest.mark.asyncio
async def test_last_transport_failure_does_not_sleep_after_exhausting_attempts(clock):
    def unavailable(request):
        raise httpx.ConnectError("Unavailable", request=request)

    async with sdk.ManorClient("http://test", worker_id="worker", secret="secret", transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(sdk.WorkerClientError):
            await client._post("/heartbeat", json={}, max_attempts=1)
    assert clock.sleeps == []


@pytest.mark.parametrize("header,minimum,maximum", [
    ("2", 2, 2), ("invalid", 2, 2), ("NaN", 2, 2),
    pytest.param("Thu, 01 Jan 1970 00:00:00 GMT", 0, 0, id="expired-http-date"),
])
def test_retry_after_header_has_a_safe_fallback(header, minimum, maximum):
    delay = sdk._retry_after_seconds(httpx.Response(503, headers={"Retry-After": header}))
    assert minimum <= delay <= maximum


@pytest.mark.asyncio
@pytest.mark.parametrize("same_worker", [True, False])
async def test_cooldown_refreshes_rotated_secret_but_never_replays_as_a_different_worker(clock, same_worker):
    calls = []

    def respond(request):
        calls.append(request.headers["Authorization"])
        if len(calls) == 1:
            client.configure_credentials(worker_id="worker" if same_worker else "other-worker", secret="new-secret")
            return httpx.Response(503, headers={"Retry-After": "5"}, json={"detail": "busy"})
        return httpx.Response(200, json={})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        if same_worker:
            await client._post("/heartbeat", json={})
            assert calls == ["Bearer old-secret", "Bearer new-secret"]
        else:
            with pytest.raises(sdk.WorkerClientError, match="identity changed"):
                await client._post("/heartbeat", json={})
            assert calls == ["Bearer old-secret"]


@pytest.mark.asyncio
async def test_inflight_401_uses_new_same_worker_secret_once_even_with_one_attempt(clock):
    calls = []

    def respond(request):
        calls.append((request.headers["Authorization"], request.content))
        if len(calls) == 1:
            client.configure_credentials(worker_id="worker", secret="new-secret")
            return httpx.Response(401, json={"detail": "Invalid worker secret"})
        return httpx.Response(200, json={
            "server_time": datetime.now(timezone.utc).isoformat(),
            "next_heartbeat_in_seconds": 10, "new_leases": [], "instructions": [],
        })

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        await client._post("/api/v1/workers/heartbeat", json={"active_leases": [{"lease_id": "lease", "attempt_token": "attempt"}]}, max_attempts=1)
    assert [header for header, _ in calls] == ["Bearer old-secret", "Bearer new-secret"]
    assert calls[0][1] == calls[1][1]
    assert clock.sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,rotate", [(401, False), (403, True), (426, True)])
async def test_rotation_does_not_retry_unchanged_credentials_or_policy_rejections(clock, status, rotate):
    calls = []

    def respond(request):
        calls.append(request)
        if rotate:
            client.configure_credentials(worker_id="worker", secret="new-secret")
        return httpx.Response(status, json={"detail": "rejected"})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(sdk.WorkerClientError) as rejected:
            await client.heartbeat(HeartbeatRequest(state="idle"))
        assert rejected.value.status_code == status
        assert rejected.value.requires_operator_action
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_401_credential_refresh_is_bounded_to_one_retry(clock):
    calls = []

    def respond(request):
        calls.append(request)
        client.configure_credentials(worker_id="worker", secret=f"secret-{len(calls)}")
        return httpx.Response(401, json={"detail": "revoked"})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(sdk.WorkerClientError) as rejected:
            await client.heartbeat(HeartbeatRequest(state="idle"))
        assert rejected.value.status_code == 401
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_rotation_never_replays_lifecycle_write_on_401(clock):
    calls = []

    def respond(request):
        calls.append(request)
        client.configure_credentials(worker_id="worker", secret="new-secret")
        return httpx.Response(401, json={"detail": "rejected"})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(sdk.WorkerClientError):
            await client.complete_lease("lease", LeaseResult(result={"receipt": "done"}), attempt_token="attempt")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401])
async def test_inflight_identity_change_rejects_old_response_without_replay(clock, status):
    calls = []

    def respond(request):
        calls.append(request)
        client.configure_credentials(worker_id="other-worker", secret="other-secret")
        return httpx.Response(status, json={})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(sdk.WorkerClientError, match="identity changed"):
            await client.heartbeat(HeartbeatRequest(state="idle"))
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_call_deadline_includes_backoff_and_cooldown_without_early_retry(monkeypatch, status):
    calls = []
    monkeypatch.setattr(sdk.random, "uniform", lambda low, high: high if status == 500 else 0)

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "60"} if status == 503 else {}, json={"detail": "busy"})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="secret", timeout_seconds=0.03, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(sdk.WorkerClientError, match="deadline exceeded") as rejected:
            await client.heartbeat(HeartbeatRequest(state="idle"))
        if status == 503:
            assert rejected.value.retry_after_seconds > 59
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_result", [False, True])
async def test_shutdown_interrupts_cooldown_and_final_flush_keeps_unacknowledged_results(monkeypatch, pending_result):
    import asyncio
    from packages.worker_sdk.types import HeartbeatCompletedLease
    from packages.worker_sdk.worker import ManorWorker

    cooling = asyncio.Event()
    original_sleep = asyncio.sleep

    async def observe_sleep(delay):
        if delay > 50:
            cooling.set()
        await original_sleep(delay)

    monkeypatch.setattr(sdk.asyncio, "sleep", observe_sleep)
    monkeypatch.setattr(sdk.random, "uniform", lambda low, high: 0)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "60"}, json={"detail": "busy"})

    client = sdk.ManorClient("http://test", worker_id="worker", secret="secret", timeout_seconds=0.05, transport=httpx.MockTransport(respond))
    worker = ManorWorker(endpoint="http://test", worker_id="worker", secret="secret", client=client)
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda: None)
    completion = HeartbeatCompletedLease(lease_id="lease", status="done", result={"receipt": "done"}, attempt_token="attempt", task_output_value_kind="task_payload")
    if pending_result:
        worker._queue_completion(completion)
    running = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.wait_for(cooling.wait(), 0.5)
        await worker.stop()
        await asyncio.wait_for(running, 0.5)
        assert client._http is None
        assert len(calls) == 1, "neither cancellation nor final flush can bypass Retry-After"
        assert worker._completions == ([completion] if pending_result else [])
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_failed_heartbeat_keeps_last_successful_cadence_without_accumulating_error_delays(monkeypatch):
    from packages.worker_sdk import worker as runtime

    worker = runtime.ManorWorker(endpoint="http://test", worker_id="worker", secret="secret")
    responses = iter([60, sdk.WorkerClientError("busy", status_code=503, retry_after_seconds=180), sdk.WorkerClientError("busy", status_code=503, retry_after_seconds=2), RuntimeError("unexpected")])
    delays = []

    async def tick():
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    async def observe_wait(awaitable, timeout):
        awaitable.close()
        delays.append(timeout)
        if len(delays) == 4:
            worker._stop.set()
        raise TimeoutError

    monkeypatch.setattr(worker, "_tick", tick)
    monkeypatch.setattr(runtime.asyncio, "wait_for", observe_wait)
    monkeypatch.setattr(runtime.random, "uniform", lambda low, high: 0)
    await worker._loop()
    assert delays == [60, 180, 60, 60]


@pytest.mark.asyncio
async def test_worker_recovers_when_rotation_wins_race_with_fresh_server_auth(monkeypatch):
    import asyncio
    from copy import copy
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from unittest.mock import AsyncMock
    from packages.core.workers import authentication as auth
    from packages.worker_sdk.worker import ManorWorker

    row = SimpleNamespace(id="worker", entity_id="entity", secret_hash="old-hash", status="active", expires_at=None)
    entered, resume = asyncio.Event(), asyncio.Event()
    rejected, release_rotation = asyncio.Event(), asyncio.Event()
    calls = []

    async def get_worker(*args, **kwargs):
        return copy(row)

    async def verify(secret_hash, presented_secret):
        if presented_secret == "old-secret" and not entered.is_set():
            assert secret_hash == "old-hash"
            entered.set()
            await resume.wait()
        elif presented_secret == "new-secret":
            assert secret_hash == "new-hash" and presented_secret == "new-secret"
        return True

    app = FastAPI()

    @app.post("/api/v1/workers/heartbeat")
    async def heartbeat(request: Request):
        calls.append(request.headers["Authorization"])
        current = await auth.authenticate_worker_secret(None, "worker", request.headers["Authorization"].split(" ", 1)[1])
        if current is None:
            rejected.set()
            return JSONResponse(status_code=401, content={"detail": "Invalid worker secret"})
        return {
            "server_time": datetime.now(timezone.utc).isoformat(), "next_heartbeat_in_seconds": 10,
            "new_leases": [], "instructions": [{"type": "shutdown"}],
        }

    @app.post("/api/v1/workers/me/rotate-secret")
    async def rotate(request: Request):
        current = await auth.authenticate_worker_secret(None, "worker", request.headers["Authorization"].split(" ", 1)[1])
        assert current is not None
        row.secret_hash = "new-hash"
        resume.set()
        await release_rotation.wait()
        return {"worker_secret": "new-secret"}

    monkeypatch.setattr(auth, "get_worker", get_worker)
    monkeypatch.setattr(auth, "release_read_transaction", AsyncMock())
    monkeypatch.setattr(auth, "_verifier", lambda: SimpleNamespace(verify=verify))
    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.ASGITransport(app=app)) as client:
        worker = ManorWorker(endpoint="http://test", worker_id="worker", secret="old-secret", client=client)
        running = asyncio.create_task(worker._loop())
        rotating = None
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            rotating = asyncio.create_task(client.rotate_secret())
            await asyncio.wait_for(rejected.wait(), 0.5)
            assert not running.done(), "old 401 must wait for the committed rotation's response"
            release_rotation.set()
            assert await rotating == "new-secret"
            await asyncio.wait_for(running, 0.5)
        finally:
            release_rotation.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if rotating is not None:
                await asyncio.gather(rotating, return_exceptions=True)
    assert calls == ["Bearer old-secret", "Bearer new-secret"]


@pytest.mark.asyncio
async def test_shutdown_cancels_an_injected_client_heartbeat(monkeypatch):
    import asyncio
    from packages.worker_sdk.worker import ManorWorker

    entered, cancelled = asyncio.Event(), asyncio.Event()

    class InjectedClient:
        closed = False

        async def start(self):
            pass

        async def heartbeat(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def close(self):
            self.closed = True

    client = InjectedClient()
    worker = ManorWorker(endpoint="http://test", worker_id="worker", secret="secret", client=client)
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda: None)
    running = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        await worker.stop()
        await worker.stop()
        await asyncio.wait_for(running, 0.5)
        assert cancelled.is_set() and client.closed
        assert worker._heartbeat_task is None
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled", "identity_changed", "heartbeat_stopped"])
async def test_pending_rotation_wait_preserves_auth_and_cancellation_boundaries(outcome):
    import asyncio

    entered, release, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    heartbeat_calls = []

    async def respond(request):
        if request.url.path.endswith("rotate-secret"):
            entered.set()
            await release.wait()
            return httpx.Response(403 if outcome == "failed" else 200, json={"worker_secret": "new-secret"})
        heartbeat_calls.append((request.headers["Authorization"], request.content))
        if len(heartbeat_calls) == 1:
            rejected.set()
            return httpx.Response(401, json={"detail": "Invalid worker secret"})
        return httpx.Response(200, json={"server_time": datetime.now(timezone.utc).isoformat(), "next_heartbeat_in_seconds": 10, "new_leases": [], "instructions": []})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        rotating = asyncio.create_task(client.rotate_secret())
        heartbeat = None
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            heartbeat = asyncio.create_task(client.heartbeat(HeartbeatRequest(state="idle")))
            await asyncio.wait_for(rejected.wait(), 0.5)
            assert not heartbeat.done()
            if outcome == "identity_changed":
                client.configure_credentials(worker_id="other-worker", secret="other-secret")
            elif outcome == "cancelled":
                rotating.cancel()
            elif outcome == "heartbeat_stopped":
                heartbeat.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(heartbeat, 0.5)
                assert not rotating.done(), "cancelling a heartbeat cannot cancel another caller's rotation"
            release.set()
            rotation_result = (await asyncio.gather(rotating, return_exceptions=True))[0]
            if outcome in {"success", "heartbeat_stopped"}:
                assert rotation_result == "new-secret"
            if outcome == "success":
                await asyncio.wait_for(heartbeat, 0.5)
                assert [header for header, _ in heartbeat_calls] == ["Bearer old-secret", "Bearer new-secret"]
                assert heartbeat_calls[0][1] == heartbeat_calls[1][1]
            elif outcome != "heartbeat_stopped":
                with pytest.raises(sdk.WorkerClientError) as error:
                    await asyncio.wait_for(heartbeat, 0.5)
                assert error.value.status_code == 401
                assert len(heartbeat_calls) == 1
        finally:
            release.set()
            rotating.cancel()
            if heartbeat is not None:
                heartbeat.cancel()
            await asyncio.gather(rotating, *([heartbeat] if heartbeat is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_same_worker_rotations_share_one_request():
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"worker_secret": "new-secret"})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", transport=httpx.MockTransport(respond)) as client:
        first = asyncio.create_task(client.rotate_secret())
        await asyncio.wait_for(entered.wait(), 0.5)
        second = asyncio.create_task(client.rotate_secret())
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == ["new-secret", "new-secret"]
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_pending_rotation_does_not_extend_heartbeat_deadline_or_replay_late_success():
    import asyncio

    entered, committed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    heartbeat_calls = []

    async def respond(request):
        if request.url.path.endswith("rotate-secret"):
            committed.set()
            await release.wait()
            return httpx.Response(200, json={"worker_secret": "new-secret"})
        heartbeat_calls.append(request)
        entered.set()
        await committed.wait()
        return httpx.Response(401, json={"detail": "Invalid worker secret"})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="old-secret", timeout_seconds=0.03, transport=httpx.MockTransport(respond)) as client:
        heartbeat = asyncio.create_task(client.heartbeat(HeartbeatRequest(state="idle")))
        await entered.wait()
        rotating = asyncio.create_task(client.rotate_secret())
        try:
            with pytest.raises(sdk.WorkerClientError, match="deadline exceeded"):
                await heartbeat
        finally:
            release.set()
            await asyncio.gather(rotating, return_exceptions=True)
        assert len(heartbeat_calls) == 1


@pytest.mark.asyncio
async def test_pre_stopped_worker_never_sends_a_request():
    import asyncio
    from packages.worker_sdk.worker import ManorWorker

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async with sdk.ManorClient("http://test", worker_id="worker", secret="secret", transport=httpx.MockTransport(respond)) as client:
        worker = ManorWorker(endpoint="http://test", worker_id="worker", secret="secret", client=client)
        await worker.stop()
        with pytest.raises(asyncio.CancelledError):
            await worker._tick()
    assert calls == []
