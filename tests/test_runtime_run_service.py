from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from packages.core.models.runtime_run import (
    RuntimeRun,
    RuntimeRunStatus,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services.runtime_run_service import (
    cancel_runtime_run_resources,
    create_runtime_run,
    project_runtime_run_status,
    request_runtime_run_cancel,
)


@pytest.mark.integration
async def test_only_one_active_root_run_is_allowed_per_conversation(db_session) -> None:
    first = await create_runtime_run(
        db_session,
        conversation_id="conv_runtime_unique",
        entity_id="entity_runtime_unique",
        user_id="user_runtime_unique",
        execution_payload={"message": "first"},
    )
    await db_session.commit()

    assert first.root_run_id == first.id

    await create_runtime_run(
        db_session,
        conversation_id="conv_runtime_unique",
        entity_id="entity_runtime_unique",
        user_id="user_runtime_unique",
        execution_payload={"message": "second"},
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.integration
async def test_waiting_projection_reports_queue_position_and_poll_interval(db_session) -> None:
    now = datetime.now(timezone.utc)
    earlier = RuntimeRun(
        id="01RUNTIMEQUEUEEARLIER00001",
        root_run_id="01RUNTIMEQUEUEEARLIER00001",
        conversation_id="conv_queue_earlier",
        entity_id="entity_queue",
        user_id="user_queue_other",
        status=RuntimeRunStatus.WAITING_RESOURCE.value,
    )
    current = RuntimeRun(
        id="01RUNTIMEQUEUECURRENT00001",
        root_run_id="01RUNTIMEQUEUECURRENT00001",
        conversation_id="conv_queue_current",
        entity_id="entity_queue",
        user_id="user_queue_current",
        status=RuntimeRunStatus.WAITING_RESOURCE.value,
    )
    db_session.add_all([earlier, current])
    await db_session.flush()
    db_session.add_all(
        [
            SandboxReservation(
                id="01RESERVATIONEARLIER000001",
                runtime_run_id=earlier.id,
                root_run_id=earlier.id,
                tool_call_id="tool-earlier",
                entity_id=earlier.entity_id,
                user_id=earlier.user_id,
                status=SandboxReservationStatus.QUEUED.value,
                priority=100,
                enqueued_at=now - timedelta(seconds=10),
                deadline_at=now + timedelta(minutes=5),
            ),
            SandboxReservation(
                id="01RESERVATIONCURRENT000001",
                runtime_run_id=current.id,
                root_run_id=current.id,
                tool_call_id="tool-current",
                entity_id=current.entity_id,
                user_id=current.user_id,
                status=SandboxReservationStatus.QUEUED.value,
                priority=100,
                enqueued_at=now,
                deadline_at=now + timedelta(minutes=5),
            ),
        ]
    )
    current.reservation_id = "01RESERVATIONCURRENT000001"
    await db_session.flush()

    projection = await project_runtime_run_status(db_session, current, poll_after_seconds=5)

    assert projection["status"] == RuntimeRunStatus.WAITING_RESOURCE.value
    assert projection["queue"] == {
        "ticket": "01RESERVATIONCURRENT000001",
        "position": 2,
        "eta_seconds": None,
        "poll_after_seconds": 5,
        "deadline_at": projection["queue"]["deadline_at"],
    }


@pytest.mark.integration
async def test_cancel_cascades_but_never_rewrites_terminal_children(db_session) -> None:
    now = datetime.now(timezone.utc)
    root = RuntimeRun(
        id="01RUNTIMECANCELROOT000001",
        root_run_id="01RUNTIMECANCELROOT000001",
        conversation_id="conv_cancel",
        entity_id="entity_cancel",
        user_id="user_cancel",
        status=RuntimeRunStatus.WAITING_RESOURCE.value,
    )
    active_child = RuntimeRun(
        id="01RUNTIMECANCELCHILD00001",
        root_run_id=root.id,
        parent_run_id=root.id,
        conversation_id=root.conversation_id,
        entity_id=root.entity_id,
        user_id=root.user_id,
        status=RuntimeRunStatus.RUNNING.value,
    )
    completed_child = RuntimeRun(
        id="01RUNTIMECANCELDONE000001",
        root_run_id=root.id,
        parent_run_id=root.id,
        conversation_id=root.conversation_id,
        entity_id=root.entity_id,
        user_id=root.user_id,
        status=RuntimeRunStatus.COMPLETED.value,
        completed_at=now,
    )
    reservation = SandboxReservation(
        id="01RESERVATIONCANCEL000001",
        runtime_run_id=root.id,
        root_run_id=root.id,
        tool_call_id="tool-cancel",
        entity_id=root.entity_id,
        user_id=root.user_id,
        status=SandboxReservationStatus.ALLOCATING.value,
        deadline_at=now + timedelta(minutes=5),
        runner_id="runner-cancel",
    )
    runner = SandboxRunner(
        id="runner-cancel",
        base_url="http://10.0.0.19:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_limit=2,
        executing_limit=1,
        active_count=1,
    )
    root.reservation_id = reservation.id
    db_session.add_all([root, active_child, completed_child, reservation, runner])
    await db_session.flush()

    cancelled = await request_runtime_run_cancel(
        db_session,
        run_id=root.id,
        entity_id=root.entity_id,
        user_id=root.user_id,
    )
    await db_session.flush()

    assert cancelled.id == root.id
    assert root.status == RuntimeRunStatus.CANCELLED.value
    assert root.cancel_requested_at is not None
    assert root.completed_at is not None
    assert active_child.status == RuntimeRunStatus.CANCEL_REQUESTED.value
    assert completed_child.status == RuntimeRunStatus.COMPLETED.value
    assert reservation.status == SandboxReservationStatus.CANCELLED.value
    assert reservation.completed_at is not None
    assert reservation.runner_id is None
    assert runner.active_count == 0

    replacement = await create_runtime_run(
        db_session,
        conversation_id=root.conversation_id,
        entity_id=root.entity_id,
        user_id=root.user_id,
        execution_payload={"message": "after cancellation"},
    )
    await db_session.flush()
    assert replacement.status == RuntimeRunStatus.QUEUED.value


@pytest.mark.unit
async def test_cancel_releases_local_sandbox_lease_when_runner_already_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    run = SimpleNamespace(
        id="run_missing_remote_sandbox",
        root_run_id="root_missing_remote_sandbox",
        active_sandbox_id=None,
        active_execution_id=None,
    )
    instance = SimpleNamespace(sandbox_id="sandbox_already_removed")
    runner = SimpleNamespace(base_url="http://runner.internal:8000")

    class QuerySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [(instance, runner)])

    class ReleaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    sessions = iter([QuerySession(), ReleaseSession()])

    class MissingSandboxClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def destroy(self, _sandbox_id):
            from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

            raise SandboxNotFoundError("sandbox no longer exists", status_code=404)

    released: list[str] = []

    async def release_local_lease(_db, *, sandbox_id: str):
        released.append(sandbox_id)
        return True

    # Import and patch the database module before stubbing get_settings: the
    # string target imports packages.core.database, whose module-level
    # settings read would otherwise run against the stub.
    monkeypatch.setattr("packages.core.database.async_session", lambda: next(sessions))
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(
            SANDBOX_COORDINATION_MODE="external-runner",
            SANDBOX_API_TOKEN="token",
        ),
    )
    monkeypatch.setattr(
        "packages.core.services.sandbox_sdk.SandboxClient",
        MissingSandboxClient,
    )
    monkeypatch.setattr(
        "packages.core.services.sandbox_queue_service.release_sandbox_instance",
        release_local_lease,
    )

    await cancel_runtime_run_resources(run)

    assert released == ["sandbox_already_removed"]


@pytest.mark.asyncio
async def test_cancel_leaves_a_warm_registered_sandbox_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container the owner registry keeps warm is reused by later turns; a
    run cancel (a completed run's included) must release the run's
    accounting but not destroy it."""
    run = SimpleNamespace(
        id="run_warm",
        root_run_id="root_warm",
        active_sandbox_id="sandbox_warm",
        active_execution_id="exec_1",
    )
    instance = SimpleNamespace(sandbox_id="sandbox_warm")
    runner = SimpleNamespace(base_url="http://runner.internal:8000")

    class QuerySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [(instance, runner)])

    class ReleaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    sessions = iter([QuerySession(), ReleaseSession()])
    calls: list[str] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def cancel_execution(self, sandbox_id, execution_id):
            calls.append(f"cancel:{sandbox_id}:{execution_id}")

        async def destroy(self, sandbox_id):
            calls.append(f"destroy:{sandbox_id}")

    released: list[str] = []

    async def release_local_lease(_db, *, sandbox_id: str):
        released.append(sandbox_id)
        return True

    async def kept_warm(sandbox_id: str) -> bool:
        return sandbox_id == "sandbox_warm"

    restored: list[str] = []

    async def restore(_db, *, sandbox_id: str, now=None):
        restored.append(sandbox_id)
        return True

    monkeypatch.setattr(
        "packages.core.services.sandbox_queue_service.restore_consumed_reservation", restore,
    )
    monkeypatch.setattr("packages.core.database.async_session", lambda: next(sessions))
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(SANDBOX_COORDINATION_MODE="external-runner", SANDBOX_API_TOKEN="token"),
    )
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    monkeypatch.setattr("packages.core.services.sandbox_queue_service.release_sandbox_instance", release_local_lease)
    monkeypatch.setattr("packages.core.services.sandbox_session_service.is_sandbox_kept_warm", kept_warm)

    async def runner_url(sandbox_id: str) -> str:
        return "http://runner.internal:8000"

    monkeypatch.setattr("packages.core.services.sandbox_session_service.sandbox_runner_base_url", runner_url)

    await cancel_runtime_run_resources(run)

    assert calls == ["cancel:sandbox_warm:exec_1"], "the running execution is cancelled, the container is not destroyed"
    # The instance row is the container's routing record; releasing it would
    # make every later exec in the reused container unroutable.
    assert released == []
    # The cancel had marked the ticket release_pending (a slot-pinning state);
    # a kept container is a consumed ticket again.
    assert restored == ["sandbox_warm"]
