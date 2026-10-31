"""The control plane places sandbox tickets by memory, and waits out a full runner.

1. A ticket carries the container memory it needs; the scheduler claims it
   only for a runner whose reported free memory fits it (memory already
   claimed since that report included), clamped to the runner's
   per-container maximum so it is never unschedulable. A runner whose health
   has no memory fields (older version) is placed by slots alone.
2. A typed ``runner_full`` rejection requeues the ticket without spending an
   attempt until its deadline, then expires it as ``sandbox_capacity_timeout``;
   a typed request rejection (bad config, failed dependency install) fails
   the ticket at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.core.models.runtime_run import (
    RuntimeRun,
    RuntimeRunStatus,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services import sandbox_queue_service
from packages.core.services.runtime_run_terminal import runtime_run_failure_message
from packages.core.services.sandbox_queue_service import (
    RUNNER_MEMORY_KEY,
    SANDBOX_MEMORY_KEY,
    PoolGrowth,
    claim_next_sandbox_allocation,
    classify_allocation_error,
    enqueue_sandbox_reservation,
    expire_sandbox_reservations,
    fail_sandbox_allocation,
    runner_memory_from_health,
)
from packages.core.services.sandbox_sdk.exceptions import (
    SandboxCapacityError,
    SandboxConnectionError,
    SandboxRequestError,
    SandboxRunnerOutdatedError,
    SandboxRuntimeError,
    SandboxSecurityError,
)
from tests.sandbox_scheduling_isolation import fence_foreign_scheduling


@pytest.fixture
async def db_session(db_session):
    """Claims here see only this test's runners and tickets (rolled back with it)."""

    await fence_foreign_scheduling(db_session)
    yield db_session


def _run(index: int) -> RuntimeRun:
    run_id = f"01MEMSCHED{index:02d}RUNTIME000000"[:26]
    return RuntimeRun(
        id=run_id,
        root_run_id=run_id,
        conversation_id=f"conv_memory_sched_{index}",
        entity_id="entity_memory_sched",
        user_id=f"user_memory_sched_{index}",
        status=RuntimeRunStatus.RUNNING.value,
    )


def _report(
    available_mb: int,
    *,
    max_container_mb: int = 3840,
    default_mb: int = 512,
    host_cpus: float | None = None,
    at: datetime,
) -> dict:
    report = {
        "available_mb": available_mb,
        "max_container_mb": max_container_mb,
        "default_mb": default_mb,
        "observed_at": at.isoformat(),
    }
    if host_cpus is not None:
        report["host_cpus"] = host_cpus
    return report


def _runner(
    runner_id: str,
    index: int,
    *,
    report: dict | None,
    active_limit: int = 5,
    active_count: int = 0,
    pool: str = "default",
) -> SandboxRunner:
    return SandboxRunner(
        id=runner_id,
        base_url=f"http://10.0.77.{index}:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=active_limit,
        active_count=active_count,
        executing_limit=2,
        config={RUNNER_MEMORY_KEY: report} if report is not None else {},
    )


async def _ticket(
    db,
    index: int,
    *,
    now: datetime,
    config: dict | None = None,
    wait_seconds: int | None = 600,
    pool: str | None = None,
):
    run = _run(index)
    db.add(run)
    await db.flush()
    payload = {"skill_name": f"skill-{index}"}
    if config is not None:
        payload["config"] = config
    if pool is not None:
        payload["pool"] = pool
    reservation = await enqueue_sandbox_reservation(
        db,
        run=run,
        tool_call_id=f"call-memory-{index}",
        request_payload=payload,
        priority=0,
        now=now,
        max_wait_seconds=wait_seconds,
    )
    await db.flush()
    return run, reservation


# ── 1. tickets carry their memory; claims respect it ──


@pytest.mark.integration
async def test_a_ticket_carries_the_memory_its_container_needs(db_session) -> None:
    now = datetime.now(timezone.utc)
    _run_a, explicit = await _ticket(db_session, 1, now=now, config={"memory": "2g", "pids_limit": 512})
    _run_b, default = await _ticket(db_session, 2, now=now)

    assert explicit.request_payload[SANDBOX_MEMORY_KEY] == 2048
    assert SANDBOX_MEMORY_KEY not in default.request_payload, "None = the runner's default size"


@pytest.mark.integration
async def test_the_next_waiting_need_skips_a_follower_like_the_claim_does(db_session) -> None:
    """A follower joins its leader's container (sandbox-user-conversation):
    it needs no runner memory, so warm containers are never freed for it."""

    now = datetime.now(timezone.utc)
    _run_a, follower = await _ticket(db_session, 4, now=now - timedelta(seconds=5), config={"memory": "4g"})
    follower.request_payload = {
        **follower.request_payload, sandbox_queue_service.SANDBOX_FOLLOWS_PAYLOAD_KEY: "leader-ticket",
    }
    _run_b, _plain = await _ticket(db_session, 5, now=now, config={"memory": "1g"})
    await db_session.flush()

    assert await sandbox_queue_service.next_waiting_ticket_need(db_session, now=now) == (1024, None)


@pytest.mark.integration
async def test_the_claim_skips_a_runner_without_the_memory_for_the_ticket(db_session) -> None:
    now = datetime.now(timezone.utc)
    # Ordered first (same load, smaller id) but only 300 MiB free.
    tight = _runner("runner-mem-a-tight", 1, report=_report(300, at=now))
    roomy = _runner("runner-mem-b-roomy", 2, report=_report(3000, at=now))
    db_session.add_all([tight, roomy])
    await db_session.flush()
    _run_a, ticket = await _ticket(db_session, 3, now=now)

    claim = await claim_next_sandbox_allocation(db_session, now=now)

    assert claim is not None
    assert claim[0].id == ticket.id
    assert claim[1].id == roomy.id, "a 512m default container does not fit in 300 MiB"


@pytest.mark.integration
async def test_nothing_is_claimed_while_no_runner_fits_and_the_head_keeps_its_place(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = _runner("runner-mem-head", 4, report=_report(1000, max_container_mb=1792, at=now))
    db_session.add(runner)
    await db_session.flush()
    _run_a, big = await _ticket(db_session, 5, now=now - timedelta(seconds=5), config={"memory": "1500m"})
    _run_b, small = await _ticket(db_session, 6, now=now, config={"memory": "256m"})

    # The head (1500m) does not fit in 1000 MiB; the small ticket behind it
    # does not jump the queue (a big container would starve otherwise).
    assert await claim_next_sandbox_allocation(db_session, now=now) is None
    assert big.status == SandboxReservationStatus.QUEUED.value
    assert small.status == SandboxReservationStatus.QUEUED.value

    runner.config = {RUNNER_MEMORY_KEY: _report(1600, max_container_mb=1792, at=now)}
    await db_session.flush()
    claim = await claim_next_sandbox_allocation(db_session, now=now)
    assert claim is not None and claim[0].id == big.id


@pytest.mark.integration
async def test_an_oversize_ticket_is_clamped_to_the_runner_maximum_never_unschedulable(db_session) -> None:
    now = datetime.now(timezone.utc)
    # A 2 GiB staging runner: 1792 MiB per container at most, all of it free.
    runner = _runner("runner-mem-clamp", 7, report=_report(1792, max_container_mb=1792, at=now))
    db_session.add(runner)
    await db_session.flush()
    _run_a, video = await _ticket(db_session, 8, now=now, config={"memory": "4g", "cpus": 4.0})

    claim = await claim_next_sandbox_allocation(db_session, now=now)

    assert claim is not None and claim[0].id == video.id


@pytest.mark.integration
@pytest.mark.parametrize(
    "config",
    [{"memory": "12g"}, {"memory": "4g", "cpus": 8.0}],
    ids=["memory", "cpus"],
)
async def test_a_ticket_a_bigger_runner_class_holds_never_lands_shrunk_on_a_small_one(db_session, config) -> None:
    """A pool of two machine classes: the ask fits only the big class, which is
    busy. The small runner with a free slot must not take it shrunk; once the
    big class is gone, the small one is the largest and holds it clamped."""

    now = datetime.now(timezone.utc)
    small = _runner(
        "runner-mem-class-small", 16, pool="render",
        report=_report(7424, max_container_mb=7424, host_cpus=4, at=now),
    )
    big = _runner(
        "runner-mem-class-big", 17, pool="render", active_limit=1, active_count=1,
        report=_report(0, max_container_mb=15104, host_cpus=8, at=now),
    )
    db_session.add_all([small, big])
    await db_session.flush()
    _run_a, render = await _ticket(db_session, 18, now=now, config=config, pool="render")

    assert await claim_next_sandbox_allocation(db_session, now=now) is None
    assert render.status == SandboxReservationStatus.QUEUED.value

    big.status = SandboxRunnerStatus.UNHEALTHY.value
    await db_session.flush()
    claim = await claim_next_sandbox_allocation(db_session, now=now)
    assert claim is not None and claim[1].id == small.id




@pytest.mark.integration
async def test_memory_claimed_since_the_last_report_is_counted(db_session) -> None:
    now = datetime.now(timezone.utc)
    runner = _runner("runner-mem-burst", 9, report=_report(1024, at=now - timedelta(seconds=1)))
    db_session.add(runner)
    await db_session.flush()
    for index in (10, 11, 12):
        await _ticket(db_session, index, now=now + timedelta(milliseconds=index))

    first = await claim_next_sandbox_allocation(db_session, now=now + timedelta(seconds=1))
    second = await claim_next_sandbox_allocation(db_session, now=now + timedelta(seconds=1))
    third = await claim_next_sandbox_allocation(db_session, now=now + timedelta(seconds=1))

    # Two 512m containers fill the 1024 MiB the report showed; the report
    # cannot know about them yet, the claims do.
    assert first is not None and second is not None
    assert third is None
    # Allocated after the report: still counted.
    first[0].status = SandboxReservationStatus.ALLOCATED.value
    first[0].allocated_at = now + timedelta(seconds=1)
    await db_session.flush()
    assert await claim_next_sandbox_allocation(db_session, now=now + timedelta(seconds=1)) is None
    # A fresh report that includes both containers frees the count.
    runner.config = {RUNNER_MEMORY_KEY: _report(512, at=now + timedelta(seconds=2))}
    second[0].status = SandboxReservationStatus.ALLOCATED.value
    second[0].allocated_at = now + timedelta(seconds=1)
    await db_session.flush()
    assert await claim_next_sandbox_allocation(db_session, now=now + timedelta(seconds=3)) is not None


@pytest.mark.integration
async def test_a_runner_without_memory_fields_is_placed_by_slots(db_session) -> None:
    """Mixed-version rollout: an older runner reports no memory, so only its slots count."""

    now = datetime.now(timezone.utc)
    runner = _runner("runner-mem-legacy", 13, report=None, active_limit=1)
    db_session.add(runner)
    await db_session.flush()
    await _ticket(db_session, 14, now=now, config={"memory": "4g"})
    await _ticket(db_session, 15, now=now + timedelta(milliseconds=1))

    assert await claim_next_sandbox_allocation(db_session, now=now) is not None
    assert await claim_next_sandbox_allocation(db_session, now=now) is None, "its one slot is taken"


def test_health_without_memory_fields_yields_no_report() -> None:
    now = datetime.now(timezone.utc)
    assert runner_memory_from_health({"active_sandboxes": 1, "max_active": 5}, now=now) is None
    report = runner_memory_from_health(
        {
            "memory_available_mb": 2816,
            "max_container_memory_mb": 1792,
            "memory_budget_mb": 3840,
            "memory_reserved_mb": 1024,
            "largest_allocatable_mb": 1792,
            "container_defaults": {"memory_mb": 512},
        },
        now=now,
    )
    assert report == {
        "available_mb": 2816,
        "max_container_mb": 1792,
        "default_mb": 512,
        "observed_at": now.isoformat(),
        "budget_mb": 3840,
        "reserved_mb": 1024,
        "largest_allocatable_mb": 1792,
    }


# ── 2. what a failed create means for the ticket ──


@pytest.mark.parametrize(
    "exc,outcome,prefix",
    [
        (SandboxCapacityError("memory full", 429, code="runner_full"), "capacity", "runner_full: "),
        (SandboxCapacityError("Max active sandbox limit reached (5)", 429, code="capacity_exceeded"),
         "capacity", "capacity_exceeded: "),
        (SandboxCapacityError("full", 429), "capacity", "http_429: "),
        (SandboxRequestError("dependency install failed (exit 1)", 422, code="dependency_install_failed"),
         "fatal", "dependency_install_failed: "),
        (SandboxRequestError("image is runner-owned", 400, code="config_override_rejected"),
         "fatal", "config_override_rejected: "),
        (SandboxSecurityError("network host is not allowed", 403, code="security_denied"),
         "fatal", "security_denied: "),
        (SandboxRuntimeError("docker unavailable", 503, code="docker_unavailable"),
         "unavailable", "docker_unavailable: "),
        (SandboxConnectionError("connection refused"), "unavailable", "SandboxConnectionError: "),
        # /health unreadable (restarting) vs a runner that reported what it lacks.
        (SandboxRunnerOutdatedError("capabilities could not be read", missing=["egress"], reported=False),
         "unavailable", "runner_outdated: "),
        (SandboxRunnerOutdatedError("runner lacks egress", missing=["egress"]), "fatal", "runner_outdated: "),
        (SandboxRuntimeError("load failed", 500, code="load_skill_failed"), "retry", "SandboxRuntimeError"),
        (TimeoutError(), "retry", "TimeoutError"),
    ],
)
def test_failed_creates_are_sorted_into_capacity_fatal_and_retry(exc, outcome, prefix) -> None:
    got_outcome, error = classify_allocation_error(exc)
    assert got_outcome == outcome
    assert error.startswith(prefix)


async def _claimed(db, index: int, *, now: datetime, wait_seconds: int = 600):
    runner = _runner(f"runner-mem-fail-{index}", 30 + index, report=_report(3000, at=now))
    db.add(runner)
    await db.flush()
    run, ticket = await _ticket(db, index, now=now, wait_seconds=wait_seconds)
    claim = await claim_next_sandbox_allocation(db, now=now)
    assert claim is not None and claim[0].id == ticket.id
    return run, ticket, runner


@pytest.mark.integration
async def test_runner_full_waits_without_spending_attempts_until_the_deadline(db_session, monkeypatch) -> None:
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "SANDBOX_QUEUE_POLL_SECONDS", 5)
    now = datetime.now(timezone.utc)
    run, ticket, runner = await _claimed(db_session, 20, now=now, wait_seconds=120)
    full_report = runner_memory_from_health(
        {"memory_available_mb": 100, "max_container_memory_mb": 3840}, now=now,
    )

    # Far more rejections than max_attempts allows for real failures.
    current = now
    for _ in range(8):
        await fail_sandbox_allocation(
            db_session,
            reservation_id=ticket.id,
            expected_reservation_version=ticket.version,
            error="runner_full: Sandbox runner memory is full",
            outcome="capacity",
            runner_report=full_report,
            now=current,
            max_attempts=5,
        )
        await db_session.flush()
        assert ticket.status == SandboxReservationStatus.REQUEUED.value
        assert ticket.attempt_count == 0, "waiting for room is not a failed attempt"
        assert ticket.next_attempt_at == current + timedelta(seconds=5)
        assert run.status != RuntimeRunStatus.FAILED.value
        assert runner.active_count == 0
        # The rejection's numbers replace the report, keeping the default size.
        assert runner.config[RUNNER_MEMORY_KEY]["available_mb"] == 100
        assert runner.config[RUNNER_MEMORY_KEY]["default_mb"] == 512
        # Nothing fits now, so the scheduler does not hammer the full runner.
        assert await claim_next_sandbox_allocation(db_session, now=ticket.next_attempt_at) is None
        runner.config = {RUNNER_MEMORY_KEY: _report(3000, at=current)}
        await db_session.flush()
        current = ticket.next_attempt_at
        claim = await claim_next_sandbox_allocation(db_session, now=current)
        assert claim is not None and claim[0].id == ticket.id

    # The last rejection arrives after the ticket's deadline: it expires, clearly.
    await fail_sandbox_allocation(
        db_session,
        reservation_id=ticket.id,
        expected_reservation_version=ticket.version,
        error="runner_full: Sandbox runner memory is full",
        outcome="capacity",
        now=ticket.deadline_at + timedelta(seconds=1),
    )
    await db_session.flush()
    assert ticket.status == SandboxReservationStatus.EXPIRED.value
    assert ticket.last_error.startswith("sandbox_capacity_timeout: last runner answer: runner_full")
    assert run.status == RuntimeRunStatus.FAILED.value
    assert run.status_reason == "sandbox_capacity_timeout"


@pytest.mark.integration
async def test_a_requeued_full_runner_ticket_expires_as_a_capacity_timeout(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, ticket, _runner_row = await _claimed(db_session, 21, now=now, wait_seconds=60)
    await fail_sandbox_allocation(
        db_session,
        reservation_id=ticket.id,
        error="runner_full: Sandbox runner memory is full",
        outcome="capacity",
        now=now,
    )
    await db_session.flush()
    assert ticket.status == SandboxReservationStatus.REQUEUED.value

    await expire_sandbox_reservations(db_session, now=now + timedelta(seconds=61))
    await db_session.flush()

    assert ticket.status == SandboxReservationStatus.EXPIRED.value
    assert ticket.last_error.startswith("sandbox_capacity_timeout: last runner answer: runner_full")
    assert run.status_reason == "sandbox_capacity_timeout"
    # The run ends through the out-of-loop terminal transition (#1047): the
    # user reads that no environment became free in time, not a start failure.
    assert run.error == {
        "code": "sandbox_capacity_timeout",
        "message": runtime_run_failure_message("sandbox_queue_timeout"),
    }


@pytest.mark.integration
async def test_a_request_the_runner_rejects_fails_on_the_first_attempt(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, ticket, runner = await _claimed(db_session, 22, now=now)

    await fail_sandbox_allocation(
        db_session,
        reservation_id=ticket.id,
        expected_reservation_version=ticket.version,
        error="dependency_install_failed: dependency install failed (exit 1)",
        outcome="fatal",
        now=now,
        max_attempts=5,
    )
    await db_session.flush()

    assert ticket.attempt_count == 1
    assert ticket.status == SandboxReservationStatus.FAILED.value
    assert ticket.last_error.startswith("dependency_install_failed")
    assert run.status == RuntimeRunStatus.FAILED.value
    assert run.status_reason == "sandbox_allocation_rejected"
    assert run.error["message"] == runtime_run_failure_message("sandbox_allocation_failed")
    assert runner.active_count == 0


@pytest.mark.integration
async def test_other_failures_keep_their_bounded_retries(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, ticket, _runner_row = await _claimed(db_session, 23, now=now)

    await fail_sandbox_allocation(
        db_session,
        reservation_id=ticket.id,
        error="SandboxRuntimeError",
        now=now,
        max_attempts=5,
    )
    await db_session.flush()

    assert ticket.status == SandboxReservationStatus.REQUEUED.value
    assert ticket.attempt_count == 1, "a real failure spends its attempt"
    assert ticket.next_attempt_at == now + timedelta(seconds=2)
    assert run.status != RuntimeRunStatus.FAILED.value


class _RestartingRunner:
    """A runner over HTTP that answers each create from a script.

    ``draining``: the old process refuses new sandboxes before it restarts
    (503 ``sandbox_service_draining``); ``refused``: nothing listens while the
    new image starts; ``dropped``: the port proxy accepts and drops the
    connection before the process listens; ``mismatch``: the exit proxy is
    wrong (422 ``egress_ip_mismatch``); ``up``: the container is created.
    """

    def __init__(self, script: list[str]):
        self.script = list(script)
        self.creates = 0

    def handle(self, request):
        import httpx

        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "build_version": "test"})
        self.creates += 1
        answer = self.script.pop(0)
        if answer == "draining":
            return httpx.Response(503, json={
                "detail": "Sandbox Service is draining for a restart and is not accepting new sandboxes; retry shortly.",
                "code": "sandbox_service_draining",
            })
        if answer == "refused":
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        if answer == "dropped":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        if answer == "mismatch":
            return httpx.Response(422, json={
                "detail": "egress exits from 198.51.100.9, expected 203.0.113.7", "code": "egress_ip_mismatch",
            })
        return httpx.Response(200, json={
            "sandbox_id": "sbx-after-restart", "container_name": "c", "status": "ready", "workdir": "/workspace",
            "skill": {"name": "skill", "skill_dir": "/skill"},
        })


async def _serve_ticket_from(db_session, monkeypatch, runner: _RestartingRunner, *, index: int):
    """Claim and allocate one ticket on a fake HTTP runner until the script runs out."""

    import httpx

    from packages.core.config import get_settings
    from packages.core.services import sandbox_sdk, sandbox_session_service
    from packages.core.tasks import runtime_tasks

    class _Client(sandbox_sdk.SandboxClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **{**kwargs, "transport": httpx.MockTransport(runner.handle)})

    class _SameSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(get_settings(), "SANDBOX_QUEUE_POLL_SECONDS", 5)
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", _Client)
    monkeypatch.setattr(sandbox_session_service, "_RUNNER_CAPABILITIES_CACHE", {})
    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: True)
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: _SameSession)
    monkeypatch.setattr(db_session, "commit", db_session.flush)

    now = datetime.now(timezone.utc)
    db_session.add(_runner(f"runner-restart-{index}", 40 + index, report=None))
    await db_session.flush()
    run, ticket = await _ticket(db_session, index, now=now, wait_seconds=600)
    current = now
    while runner.script:
        claim = await claim_next_sandbox_allocation(db_session, now=current)
        assert claim is not None and claim[0].id == ticket.id
        await runtime_tasks.allocate_reserved_sandbox_once(
            reservation_id=ticket.id, reservation_version=claim[0].version,
        )
        if ticket.status != SandboxReservationStatus.REQUEUED.value:
            break
        assert ticket.attempt_count == 0, "waiting for a restarting runner is not a failed attempt"
        assert ticket.last_error.startswith("sandbox_runner_unavailable: last runner answer: ")
        current = ticket.next_attempt_at
    return run, ticket


@pytest.mark.integration
async def test_a_ticket_waits_out_its_runner_restarting_and_is_served_when_it_returns(
    db_session, monkeypatch,
) -> None:
    # More unanswered creates than max_attempts allows real failures: the
    # self-update restart outlasted the old 5 x 2 s retry budget (staging 10-27).
    runner = _RestartingRunner(["draining", "draining", "refused", "refused", "refused", "dropped", "up"])

    run, ticket = await _serve_ticket_from(db_session, monkeypatch, runner, index=24)

    assert runner.creates == 7
    assert ticket.status == SandboxReservationStatus.ALLOCATED.value
    assert ticket.sandbox_id == "sbx-after-restart"
    assert run.status != RuntimeRunStatus.FAILED.value


@pytest.mark.integration
async def test_a_runner_refusing_the_request_still_fails_the_ticket_at_once(db_session, monkeypatch) -> None:
    runner = _RestartingRunner(["mismatch", "up"])

    run, ticket = await _serve_ticket_from(db_session, monkeypatch, runner, index=25)

    assert runner.creates == 1
    assert ticket.status == SandboxReservationStatus.FAILED.value
    assert ticket.last_error.startswith("egress_ip_mismatch: ")
    assert run.status_reason == "sandbox_allocation_rejected"


@pytest.mark.integration
async def test_a_ticket_whose_runner_never_returns_expires_as_runner_unavailable(db_session) -> None:
    now = datetime.now(timezone.utc)
    run, ticket, _runner_row = await _claimed(db_session, 26, now=now, wait_seconds=60)
    await fail_sandbox_allocation(
        db_session,
        reservation_id=ticket.id,
        error="sandbox_service_draining: Sandbox Service is draining for a restart",
        outcome="unavailable",
        now=now,
    )
    await db_session.flush()
    assert ticket.status == SandboxReservationStatus.REQUEUED.value

    await expire_sandbox_reservations(db_session, now=now + timedelta(seconds=61))
    await db_session.flush()

    assert ticket.status == SandboxReservationStatus.EXPIRED.value
    assert ticket.last_error.startswith("sandbox_runner_unavailable: last runner answer: sandbox_service_draining")
    assert run.status_reason == "sandbox_runner_unavailable"
    assert run.error["message"] == runtime_run_failure_message("sandbox_queue_timeout")


# ── the allocation task wires the classification through ──


class _AllocationSession:
    def __init__(self, reservation, runner):
        self.reservation = reservation
        self.runner = runner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.reservation)

    async def get(self, _model, _id):
        return self.runner

    async def commit(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,outcome",
    [
        (
            SandboxCapacityError(
                "Sandbox runner memory is full",
                429,
                code="runner_full",
                body={"available_memory_mb": 100, "max_container_memory_mb": 1792, "memory_budget_mb": 3840},
            ),
            "capacity",
        ),
        (SandboxRequestError("dependency install failed", 422, code="dependency_install_failed"), "fatal"),
        (SandboxRuntimeError("boom", 500, code="internal_error"), "retry"),
    ],
)
async def test_the_allocation_task_hands_the_outcome_to_the_queue(monkeypatch, exc, outcome) -> None:
    from packages.core.tasks import runtime_tasks

    reservation = SimpleNamespace(
        id="res-1", status=SandboxReservationStatus.ALLOCATING.value, version=3,
        runner_id="runner-1", request_payload={"skill_name": "demo"},
    )
    runner = SimpleNamespace(id="runner-1", base_url="http://runner-1:8000")
    session = _AllocationSession(reservation, runner)
    failures: list[dict] = []

    async def fake_create(**_kwargs):
        raise exc

    async def fake_fail(_db, **kwargs):
        failures.append(kwargs)

    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: True)
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: (lambda: session))
    monkeypatch.setattr(runtime_tasks, "_create_reserved_sandbox", fake_create)
    monkeypatch.setattr(runtime_tasks, "fail_sandbox_allocation", fake_fail)

    result = await runtime_tasks.allocate_reserved_sandbox_once(reservation_id="res-1", reservation_version=3)

    assert result["allocated"] is False and result["outcome"] == outcome
    assert len(failures) == 1
    assert failures[0]["outcome"] == outcome
    assert failures[0]["expected_reservation_version"] == 3
    if outcome == "capacity":
        assert failures[0]["error"].startswith("runner_full: ")
        assert failures[0]["runner_report"]["available_mb"] == 100
        assert failures[0]["runner_report"]["max_container_mb"] == 1792
    else:
        assert failures[0]["runner_report"] is None


def test_queue_service_exports_are_real() -> None:
    for name in sandbox_queue_service.__all__:
        assert hasattr(sandbox_queue_service, name), name
