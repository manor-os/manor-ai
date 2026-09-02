"""Celery control-plane tasks for durable Runtime and Sandbox coordination."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from packages.core.celery_app import celery_app
from packages.core.config import get_settings
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services.sandbox_queue_service import (
    claim_due_sandbox_runner_health_checks,
    claim_next_sandbox_allocation,
    complete_sandbox_allocation,
    configured_sandbox_runners,
    effective_runner_limits,
    expire_sandbox_reservations,
    fail_sandbox_allocation,
    reconciled_runner_active_count,
    reconcile_sandbox_runners,
    recover_stale_sandbox_allocations,
)
from packages.core.services.runtime_event_stream import (
    append_runtime_sse_event,
    parse_sse_frame,
)
from packages.core.tasks._runtime import run_in_worker

logger = logging.getLogger(__name__)


def _runtime_terminal_error_from_frame(frame: str) -> dict[str, str] | None:
    """Extract a terminal Chat error so the durable run cannot report success."""

    event, payload = parse_sse_frame(frame)
    if event not in {"error", "stream_end"} or not payload:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    message = data.get("message") if event == "error" else data.get("error")
    if not message:
        return None
    code = str(data.get("stop_reason") or "runtime_execution_failed").strip()
    return {
        "code": (code or "runtime_execution_failed")[:80],
        "message": str(message)[:2000],
    }


def _durable_external_sandbox_enabled() -> bool:
    settings = get_settings()
    return (
        settings.MANOR_RUNTIME_EXECUTION_MODE == "durable"
        and settings.SANDBOX_COORDINATION_MODE == "external-runner"
    )


async def _refresh_runner_health(runner_id: str) -> None:
    from packages.core.database import create_worker_session
    from packages.core.services.sandbox_sdk import SandboxClient

    settings = get_settings()
    session_factory = create_worker_session()
    async with session_factory() as db:
        runner = await db.get(SandboxRunner, runner_id)
        if runner is None or runner.status == SandboxRunnerStatus.DRAINING.value:
            return
        base_url = runner.base_url
    try:
        async with SandboxClient(
            base_url=base_url,
            timeout=10.0,
            api_token=settings.SANDBOX_API_TOKEN,
        ) as client:
            health = await client.health()
        async with session_factory() as db:
            runner = (
                await db.execute(
                    select(SandboxRunner)
                    .where(SandboxRunner.id == runner_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                runner is None
                or runner.status == SandboxRunnerStatus.DRAINING.value
                or runner.base_url.rstrip("/") != base_url.rstrip("/")
            ):
                return
            runner.status = SandboxRunnerStatus.HEALTHY.value
            runner.last_health_at = datetime.now(timezone.utc)
            runner.last_error = None
            observed_active = max(
                int(health.get("active_sandboxes") or 0),
                int(health.get("reserved_active") or 0),
            )
            runner.active_count = await reconciled_runner_active_count(
                db,
                runner_id=runner.id,
                observed_active=observed_active,
            )
            runner.executing_count = max(0, int(health.get("executing") or 0))
            runner_config = dict(runner.config or {})
            has_active_limit = health.get("max_active") is not None
            has_executing_limit = health.get("max_executing") is not None
            if has_active_limit != has_executing_limit:
                raise ValueError("Sandbox runner health returned incomplete capacity")
            if has_active_limit:
                observed_active_limit = int(health["max_active"])
                observed_executing_limit = int(health["max_executing"])
                if observed_active_limit <= 0 or observed_executing_limit <= 0:
                    raise ValueError("Sandbox runner health returned invalid capacity")
                runner.active_limit, runner.executing_limit = effective_runner_limits(
                    observed_active=observed_active_limit,
                    observed_executing=observed_executing_limit,
                    active_cap=int(runner_config.get("active_cap") or 0),
                    executing_cap=int(runner_config.get("executing_cap") or 0),
                )
                runner.config = {
                    **runner_config,
                    "observed_active_limit": observed_active_limit,
                    "observed_executing_limit": observed_executing_limit,
                }
            await db.commit()
    except Exception as exc:
        async with session_factory() as db:
            runner = (
                await db.execute(
                    select(SandboxRunner)
                    .where(SandboxRunner.id == runner_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                runner is not None
                and runner.status != SandboxRunnerStatus.DRAINING.value
                and runner.base_url.rstrip("/") == base_url.rstrip("/")
            ):
                runner.status = SandboxRunnerStatus.UNHEALTHY.value
                runner.last_health_at = datetime.now(timezone.utc)
                runner.last_error = str(exc)[:2000]
                await db.commit()


async def _create_reserved_sandbox(
    *,
    reservation_id: str,
    base_url: str,
    payload: dict[str, Any],
) -> str:
    from packages.core.services.sandbox_sdk import SandboxClient

    settings = get_settings()
    source = str(payload.get("source") or "workspace")
    kwargs = {
        "skill_name": str(payload["skill_name"]),
        "files": dict(payload.get("files") or {}),
        "env": dict(payload.get("env") or {}),
        "allowed_sensitive_keys": list(payload.get("allowed_sensitive_keys") or []),
        "config": payload.get("config"),
        "auto_install": bool(payload.get("auto_install", True)),
        "idempotency_key": reservation_id,
    }
    async with SandboxClient(
        base_url=base_url,
        timeout=180.0,
        api_token=settings.SANDBOX_API_TOKEN,
    ) as client:
        if source == "builtin":
            result = await client.create_from_builtin(**kwargs)
        else:
            result = await client.create_from_files(**kwargs)
    return result.sandbox_id


async def run_sandbox_scheduler_once() -> dict[str, Any]:
    """Reconcile capacity and enqueue at most one external allocation."""

    if not _durable_external_sandbox_enabled():
        return {"enabled": False, "allocated": False}

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        await reconcile_sandbox_runners(db, configured_sandbox_runners())
        runner_ids = await claim_due_sandbox_runner_health_checks(db, now=now)
        await db.commit()
    await asyncio.gather(*(_refresh_runner_health(runner_id) for runner_id in runner_ids))

    async with session_factory() as db:
        expired = await expire_sandbox_reservations(db)
        recovered = await recover_stale_sandbox_allocations(db)
        claim = await claim_next_sandbox_allocation(db)
        if claim is None:
            await db.commit()
            return {
                "enabled": True,
                "allocation_queued": False,
                "expired": expired,
                "recovered": recovered,
            }
        reservation, _runner = claim
        await db.commit()
        return {
            "enabled": True,
            "allocation_queued": True,
            "expired": expired,
            "recovered": recovered,
            "reservation_id": reservation.id,
        }


async def allocate_reserved_sandbox_once(
    *,
    reservation_id: str,
    reservation_version: int,
) -> dict[str, Any]:
    """Create one claimed Sandbox without occupying the control worker."""

    if not _durable_external_sandbox_enabled():
        return {"enabled": False, "claimed": False}
    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    async with session_factory() as db:
        reservation = (
            await db.execute(
                select(SandboxReservation)
                .where(SandboxReservation.id == reservation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            reservation is None
            or reservation.status != SandboxReservationStatus.ALLOCATING.value
            or reservation.version != reservation_version
            or not reservation.runner_id
        ):
            await db.commit()
            return {"enabled": True, "claimed": False, "reservation_id": reservation_id}
        runner = await db.get(SandboxRunner, reservation.runner_id)
        if runner is None:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error="sandbox_runner_missing",
            )
            await db.commit()
            return {"enabled": True, "claimed": False, "reservation_id": reservation_id}
        runner_id = runner.id
        base_url = runner.base_url
        payload = dict(reservation.request_payload or {})
        await db.commit()

    try:
        sandbox_id = await _create_reserved_sandbox(
            reservation_id=reservation_id,
            base_url=base_url,
            payload=payload,
        )
    except Exception as exc:
        async with session_factory() as db:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error=exc.__class__.__name__,
            )
            await db.commit()
        logger.warning(
            "Sandbox reservation allocation failed reservation=%s runner=%s type=%s",
            reservation_id,
            runner_id,
            exc.__class__.__name__,
        )
        return {"enabled": True, "claimed": True, "allocated": False}

    try:
        async with session_factory() as db:
            await complete_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                sandbox_id=sandbox_id,
                runner_id=runner_id,
                expected_reservation_version=reservation_version,
            )
            await db.commit()
    except Exception as exc:
        from packages.core.services.sandbox_sdk import SandboxClient

        try:
            async with SandboxClient(
                base_url=base_url,
                timeout=30.0,
                api_token=get_settings().SANDBOX_API_TOKEN,
            ) as client:
                await client.destroy(sandbox_id)
        except Exception:
            logger.warning(
                "Failed to destroy uncommitted Sandbox allocation reservation=%s",
                reservation_id,
                exc_info=True,
            )
        async with session_factory() as db:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error=exc.__class__.__name__,
            )
            await db.commit()
        return {"enabled": True, "claimed": True, "allocated": False}
    return {
        "enabled": True,
        "claimed": True,
        "allocated": True,
        "reservation_id": reservation_id,
    }


async def dispatch_runtime_outbox_once(*, batch_size: int = 20) -> int:
    """Recover stale runs and publish pending versioned outbox rows."""

    if not _durable_external_sandbox_enabled():
        return 0
    from packages.core.database import create_worker_session

    now = datetime.now(timezone.utc)
    session_factory = create_worker_session()
    async with session_factory() as db:
        from packages.core.services.runtime_run_service import (
            recover_expired_runtime_run_leases,
        )

        recovered, failed = await recover_expired_runtime_run_leases(
            db,
            now=now,
            batch_size=batch_size,
        )
        if recovered or failed:
            logger.warning(
                "Runtime lease recovery sweep recovered=%d failed=%d",
                recovered,
                failed,
            )
        # Commit the recovered run state before its outbox event can reach a
        # worker. If publishing wins that race, the worker could otherwise
        # claim the old expired row and then be overwritten back to queued.
        await db.commit()
        events = list(
            (
                await db.execute(
                    select(RuntimeOutboxEvent)
                    .where(
                        RuntimeOutboxEvent.delivered_at.is_(None),
                        or_(
                            RuntimeOutboxEvent.available_at.is_(None),
                            RuntimeOutboxEvent.available_at <= now,
                        ),
                    )
                    .order_by(RuntimeOutboxEvent.created_at.asc())
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        delivered = 0
        for event in events:
            try:
                celery_app.send_task(
                    event.event_type,
                    kwargs=dict(event.payload or {}),
                    queue="interactive",
                )
                event.delivered_at = now
                event.last_error = None
                delivered += 1
            except Exception as exc:
                event.attempt_count += 1
                event.last_error = str(exc)[:2000]
        await db.commit()
        return delivered


@celery_app.task(name="runtime.sandbox_scheduler", max_retries=0)
def sandbox_scheduler_task() -> dict[str, Any]:
    return run_in_worker(run_sandbox_scheduler_once())


@celery_app.task(name="runtime.outbox_dispatch", max_retries=0)
def runtime_outbox_dispatch_task() -> dict[str, int]:
    return {"delivered": run_in_worker(dispatch_runtime_outbox_once())}


@celery_app.task(
    name="runtime.allocate_sandbox",
    max_retries=0,
    soft_time_limit=210,
    time_limit=240,
)
def allocate_sandbox_task(
    reservation_id: str,
    reservation_version: int,
) -> dict[str, Any]:
    return run_in_worker(
        allocate_reserved_sandbox_once(
            reservation_id=reservation_id,
            reservation_version=reservation_version,
        )
    )


async def _runtime_run_lease_heartbeat(run_id: str, worker_id: str) -> None:
    from packages.core.database import create_worker_session
    from packages.core.services.runtime_run_service import renew_runtime_run_lease

    session_factory = create_worker_session()
    while True:
        await asyncio.sleep(30)
        async with session_factory() as db:
            renewed = await renew_runtime_run_lease(
                db,
                run_id=run_id,
                worker_id=worker_id,
                lease_seconds=90,
            )
            await db.commit()
        if not renewed:
            return


async def execute_runtime_run_once(
    *,
    run_id: str,
    worker_id: str,
    reservation_id: str | None = None,
    reservation_version: int | None = None,
) -> dict[str, Any]:
    """Claim and execute one initial or resumed Chat Runtime run."""

    from packages.core.ai.runtime import runtime_stream_chat_turn
    from packages.core.database import create_worker_session
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
    from packages.core.services.runtime_run_service import (
        claim_runtime_run_execution,
        complete_runtime_run_execution,
    )

    session_factory = create_worker_session()
    async with session_factory() as db:
        run = await claim_runtime_run_execution(
            db,
            run_id=run_id,
            worker_id=worker_id,
            lease_seconds=90,
            expected_reservation_id=reservation_id,
            expected_reservation_version=reservation_version,
        )
        if run is None:
            await db.commit()
            return {"claimed": False, "run_id": run_id}
        payload = dict(run.execution_payload or {})
        checkpoint = dict(run.checkpoint or {}) if run.checkpoint else None
        await db.commit()

    heartbeat = asyncio.create_task(_runtime_run_lease_heartbeat(run_id, worker_id))
    try:
        terminal_error: dict[str, str] | None = None
        async for frame in runtime_stream_chat_turn(
            payload.get("message") or "",
            run.conversation_id,
            surface=str(payload.get("surface") or "main_assistant_chat"),
            entity_id=run.entity_id,
            user_id=run.user_id,
            agent_id=run.agent_id,
            workspace_id=run.workspace_id,
            assistant_message_id=run.assistant_message_id,
            manual_skill_refs=list(payload.get("manual_skill_refs") or []),
            disable_tools=bool(payload.get("disable_tools", False)),
            blocked_tools=list(payload.get("blocked_tools") or []),
            editor_context=payload.get("editor_context"),
            runtime_metadata=payload.get("runtime_metadata"),
            runtime_run_id=run.id,
            runtime_checkpoint=checkpoint,
        ):
            await append_runtime_sse_event(run.id, frame)
            frame_error = _runtime_terminal_error_from_frame(frame)
            if frame_error is not None:
                terminal_error = frame_error

        async with session_factory() as db:
            current = await db.get(RuntimeRun, run.id)
            if current is not None and current.status == RuntimeRunStatus.WAITING_RESOURCE.value:
                await db.commit()
                return {"claimed": True, "waiting": True, "run_id": run.id}
            completed = await complete_runtime_run_execution(
                db,
                run_id=run.id,
                worker_id=worker_id,
                result={"message_id": run.assistant_message_id},
                error=terminal_error,
            )
            await db.commit()
            return {
                "claimed": True,
                "waiting": False,
                "run_id": run.id,
                "status": completed.status,
            }
    except Exception as exc:
        logger.exception("Durable Runtime run failed run=%s", run.id)
        async with session_factory() as db:
            current = await db.get(RuntimeRun, run.id)
            if current is not None and current.status not in RuntimeRunStatus.terminal():
                await complete_runtime_run_execution(
                    db,
                    run_id=run.id,
                    worker_id=worker_id,
                    error={"code": "runtime_execution_failed", "message": str(exc)[:2000]},
                )
                await db.commit()
        raise
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


@celery_app.task(
    bind=True,
    name="runtime.execute_run",
    max_retries=2,
    soft_time_limit=1800,
    time_limit=1860,
)
def execute_runtime_run_task(self, run_id: str) -> dict[str, Any]:
    worker_id = str(self.request.id or f"execute:{run_id}")
    try:
        return run_in_worker(execute_runtime_run_once(run_id=run_id, worker_id=worker_id))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)


@celery_app.task(
    bind=True,
    name="runtime.resume_run",
    max_retries=2,
    soft_time_limit=1800,
    time_limit=1860,
)
def resume_runtime_run_task(
    self,
    run_id: str,
    reservation_id: str,
    reservation_version: int,
) -> dict[str, Any]:
    worker_id = str(self.request.id or f"resume:{run_id}")
    try:
        return run_in_worker(
            execute_runtime_run_once(
                run_id=run_id,
                worker_id=worker_id,
                reservation_id=reservation_id,
                reservation_version=reservation_version,
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)
