"""Health check and system status endpoints."""
from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

import packages.core.database as db_module

router = APIRouter(tags=["health"])

_STARTUP_TIME = time.time()


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
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


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
        "support_tickets_enabled": deployment_mode.lower() == "cloud",
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
    try:
        async with db_module.async_session() as db:
            result = await db.execute(text("SELECT 1"))
            result.scalar()
        checks["postgres"] = {"status": "ok"}
    except Exception as e:
        checks["postgres"] = {"status": "error", "detail": str(e)[:200]}
        all_ok = False

    # Redis
    try:
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = aioredis.from_url(redis_url)
        await r.ping()
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

    # System info
    uptime_seconds = time.time() - _STARTUP_TIME
    system = {
        "version": "0.1.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_seconds": round(uptime_seconds),
        "deployment_mode": os.getenv("DEPLOYMENT_MODE", "oss"),
        "pid": os.getpid(),
    }

    from fastapi.responses import JSONResponse
    status_code = 200 if all_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
            "system": system,
        },
    )


@router.get("/health/ready")
async def readiness():
    """Kubernetes readiness probe — verifies DB connectivity."""
    try:
        async with db_module.async_session() as db:
            await db.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"ready": False})
