"""Health check and system status endpoints."""
from __future__ import annotations

import os
import asyncio
import platform
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

import packages.core.database as db_module
from packages.core.database_runtime import DatabaseFailureKind
from packages.core.constants.plans import ai_credit_limits_enabled
from packages.core.service_role import normalize_service_role

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseProbeResult:
    ready: bool
    failure: DatabaseFailureKind | None = None
    exception_type: str | None = None


async def _run_database_probe() -> DatabaseProbeResult:
    started = time.monotonic()
    try:
        async with asyncio.timeout(3), db_module.async_session(info={"database_probe": True}) as db:
            await db.execute(text("SELECT 1"))
        return DatabaseProbeResult(True)
    except Exception as exc:
        kind = DatabaseFailureKind.from_exception(exc)
        failure = kind or (DatabaseFailureKind.PROBE_TIMEOUT if isinstance(exc, TimeoutError) else DatabaseFailureKind.PROBE_FAILED)
        runtime = getattr(db_module.engine.sync_engine, "_manor_database_runtime", None)
        logger.warning(
            "database_probe_failed reason=%s exception_type=%s elapsed_seconds=%.3f pool=%s",
            failure, type(exc).__name__, time.monotonic() - started,
            runtime.snapshot() if runtime is not None else None,
        )
        return DatabaseProbeResult(False, failure, type(exc).__name__)


async def _probe_database() -> DatabaseProbeResult:
    # Readiness and deep-health often overlap. Share only the in-flight probe:
    # otherwise our single reserved probe slot rejects another health request
    # even when the DB is healthy. No success cache masks a later outage.
    loop = asyncio.get_running_loop()
    task = getattr(loop, "_manor_database_probe", None)
    if task is None or task.done():
        task = loop.create_task(_run_database_probe())
        loop._manor_database_probe = task
    return await asyncio.shield(task)

_STARTUP_TIME = time.time()
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
_PRODUCTION_ENVIRONMENTS = {"prod", "production"}


def _feature_available(name: str, environment: str) -> bool:
    """Return an environment default with an explicit deployment override."""
    override = os.getenv(name)
    if override is not None:
        value = override.strip().lower()
        if value in _TRUTHY:
            return True
        if value in _FALSY:
            return False
    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "oss":
        return True
    return environment.strip().lower() not in _PRODUCTION_ENVIRONMENTS


def _is_mountpoint(path: str) -> bool:
    return os.path.ismount(path)


def _ensure_fs_marker(fs_root: str, marker: str) -> tuple[bool, str | None]:
    if not os.path.isdir(fs_root) or not _is_mountpoint(fs_root):
        return False, f"{fs_root} not mounted"
    if not os.path.exists(marker):
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write("ok\n")
        except OSError as exc:
            return False, f"Could not create JuiceFS marker .accesschk: {exc}"
    return True, None


@router.get("/health")
async def health():
    """Quick health check — returns 200 if the API is running."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service_role": normalize_service_role(os.getenv("MANOR_SERVICE_ROLE")),
    }


@router.get("/config")
async def client_config():
    """Public config for the frontend — deployment mode, feature flags."""
    deployment_mode = os.getenv("DEPLOYMENT_MODE", "oss")
    environment = os.getenv("MANOR_ENV", "local" if deployment_mode.lower() == "oss" else "prod")
    config = {
        "deployment_mode": deployment_mode,
        "environment": environment,
        "email_enabled": os.getenv("EMAIL_ENABLED", "false").lower() == "true",
        "fs_enabled": os.getenv("MANOR_FS_ENABLED", "false").lower() in ("true", "1"),
        "flows_available": _feature_available("FLOWS_AVAILABLE", environment),
        "flows_released": _feature_available("FLOWS_RELEASED", environment),
        "ai_credits_unlimited": not ai_credit_limits_enabled(),
    }
    return config


@router.get("/health/deep")
async def deep_health():
    """Deep health check — verifies all dependencies.

    Checks: PostgreSQL, Redis, filesystem, and reports system info.
    Returns 200 if all critical services are reachable, 503 if any fail.
    """
    checks = {}
    all_ok = True

    # PostgreSQL
    probe = await _probe_database()
    if probe.ready:
        checks["postgres"] = {"status": "ok"}
        runtime = getattr(db_module.engine.sync_engine, "_manor_database_runtime", None)
        if runtime is not None:
            checks["postgres"]["pool"] = runtime.snapshot()
    else:
        checks["postgres"] = {"status": "error", "reason": probe.failure, "exception_type": probe.exception_type}
        all_ok = False

    # Redis
    try:
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = aioredis.from_url(redis_url)
        try:
            async with asyncio.timeout(2):
                await r.ping()
        finally:
            await r.aclose()
        checks["redis"] = {"status": "ok"}
    except Exception as e:
        checks["redis"] = {"status": "error", "detail": str(e)[:200]}
        # Redis is not critical for basic operation

    # Filesystem (JuiceFS mount)
    fs_root = os.getenv("MANOR_FS_ROOT", "/mnt/manor")
    fs_enabled = os.getenv("MANOR_FS_ENABLED", "false").lower() == "true"
    if fs_enabled:
        marker = os.path.join(fs_root, ".accesschk")
        marker_ok, marker_detail = _ensure_fs_marker(fs_root, marker)
        if not marker_ok:
            checks["filesystem"] = {"status": "error", "detail": marker_detail, "path": fs_root}
            all_ok = False
        else:
            # Verify the mount is writable
            probe = os.path.join(fs_root, ".health_probe")
            try:
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
                checks["filesystem"] = {"status": "ok", "path": fs_root, "backend": "juicefs"}
            except OSError as e:
                checks["filesystem"] = {
                    "status": "error",
                    "detail": f"Mount exists but not writable: {e}",
                    "path": fs_root,
                }
                all_ok = False
    else:
        checks["filesystem"] = {"status": "disabled"}

    # Credential backend (Vault Transit in cloud/prod, dev provider locally).
    try:
        import packages.core.credentials as credentials

        cred_health = credentials.get_credential_service().health()
        checks["credentials"] = {
            "status": "ok" if cred_health.ok else "error",
            "detail": cred_health.detail,
        }
        if not cred_health.ok:
            all_ok = False
    except Exception as e:
        checks["credentials"] = {"status": "error", "detail": str(e)[:200]}
        all_ok = False

    # System info
    uptime_seconds = time.time() - _STARTUP_TIME
    system = {
        "version": "0.2.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_seconds": round(uptime_seconds),
        "deployment_mode": os.getenv("DEPLOYMENT_MODE", "oss"),
        "service_role": normalize_service_role(os.getenv("MANOR_SERVICE_ROLE")),
        "pid": os.getpid(),
    }

    from fastapi.responses import JSONResponse
    status_code = 200 if all_ok else 503
    content = {
        "status": "ok" if all_ok else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() != "cloud":
        content.update({"checks": checks, "system": system})
    return JSONResponse(status_code=status_code, content=content)


@router.get("/health/ready")
async def readiness():
    """Readiness probe — verifies DB connectivity."""
    probe = await _probe_database()
    if probe.ready:
        return {"ready": True}
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={"ready": False}, headers={"Retry-After": "2"})
