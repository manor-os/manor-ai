"""Ops monitoring service — host + Docker container snapshots.

Reads:
  - host metrics via ``psutil`` (CPU, RAM, disk, load avg, uptime)
  - container state via ``docker`` SDK over /var/run/docker.sock

Both dependencies degrade gracefully — if the Docker socket isn't mounted
into the API container, the collector returns an empty container list and
flags the issue in ``snapshot.errors``. The API endpoint stays 200 so the
admin dashboard still shows host stats while ops investigates.

Snapshot payload is intentionally flat / JSON-ready so it can be cached
in Redis (``ops:snapshot``) without further serialization gymnastics.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


def _safe_psutil():
    try:
        import psutil  # noqa: WPS433
        return psutil
    except ImportError:
        return None


def _safe_docker_client():
    try:
        import docker  # noqa: WPS433
        return docker.from_env()
    except Exception as exc:
        logger.debug("docker SDK unavailable: %s", exc)
        return None


def collect_host_metrics() -> dict[str, Any]:
    """Host stats from psutil. When ``HOST_PROC=/host/proc`` is set
    (i.e. /proc bind-mounted into the container), psutil reads the host
    namespace instead of the container's. Otherwise it returns whatever
    the container itself sees — still useful in dev.
    """
    psutil = _safe_psutil()
    if not psutil:
        return {"error": "psutil_unavailable"}

    try:
        # interval=None → uses the time since the last call. First-call
        # returns 0.0; collector running on a 30s tick gives accurate
        # subsequent reads without blocking the request.
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        try:
            load1, load5, load15 = psutil.getloadavg()
        except (AttributeError, OSError):
            load1 = load5 = load15 = 0.0
        try:
            boot_ts = psutil.boot_time()
            uptime_s = int(time.time() - boot_ts)
        except Exception:
            uptime_s = 0
        try:
            net = psutil.net_io_counters()
            net_io = {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv}
        except Exception:
            net_io = {}

        return {
            "cpu_pct": round(cpu_pct, 1),
            "cpu_count": psutil.cpu_count(),
            "mem": {
                "total_mb": mem.total // (1024 * 1024),
                "used_mb": mem.used // (1024 * 1024),
                "available_mb": mem.available // (1024 * 1024),
                "pct": round(mem.percent, 1),
            },
            "disk": {
                "total_gb": round(disk.total / (1024 ** 3), 2),
                "used_gb": round(disk.used / (1024 ** 3), 2),
                "free_gb": round(disk.free / (1024 ** 3), 2),
                "pct": round(disk.percent, 1),
            },
            "load_avg": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "uptime_seconds": uptime_s,
            "net_io": net_io,
        }
    except Exception as exc:
        logger.warning("host_collector failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


def collect_docker_containers() -> list[dict[str, Any]]:
    """Per-container snapshot. Skips ``stats`` calls on exited containers
    (those raise APIError). For running containers, ``stats(stream=False)``
    blocks ~1.3s on first call (Docker engine baseline) — fine on a 30s
    Celery tick, too slow for a synchronous request handler. The endpoint
    reads from Redis ``ops:snapshot`` instead of recomputing.
    """
    client = _safe_docker_client()
    if client is None:
        return []

    out = []
    try:
        containers = client.containers.list(all=True)
    except Exception as exc:
        logger.warning("docker.containers.list failed: %s", exc)
        return [{"error": str(exc)}]

    for c in containers:
        attrs = c.attrs or {}
        state = attrs.get("State") or {}
        host_cfg = attrs.get("HostConfig") or {}
        cpu_pct = 0.0
        mem_mb = 0
        mem_pct = 0.0

        if state.get("Status") == "running":
            try:
                stats = c.stats(stream=False)
                cpu_pct = _calc_cpu_pct(stats)
                mem_usage = (stats.get("memory_stats") or {}).get("usage") or 0
                mem_limit = (stats.get("memory_stats") or {}).get("limit") or 0
                mem_mb = mem_usage // (1024 * 1024)
                mem_pct = round((mem_usage / mem_limit) * 100, 1) if mem_limit else 0.0
            except Exception as exc:
                logger.debug("stats(%s) failed: %s", c.name, exc)

        health = (state.get("Health") or {}).get("Status") if state.get("Health") else None
        # ``c.image`` lazy-fetches the image record — if the image was
        # pruned out from under a stopped container it 404s. The image
        # name is also embedded in attrs.Config.Image, which is cheaper
        # and never raises. Use that and skip the lazy attribute.
        image_name = (attrs.get("Config") or {}).get("Image")
        out.append({
            "id": c.id[:12],
            "name": c.name,
            "image": image_name,
            "status": state.get("Status"),
            "health": health,
            "started_at": state.get("StartedAt"),
            "exit_code": state.get("ExitCode") if state.get("Status") == "exited" else None,
            "oom_killed": state.get("OOMKilled", False),
            "restart_count": attrs.get("RestartCount", 0),
            "cpu_pct": round(cpu_pct, 1),
            "mem_mb": int(mem_mb),
            "mem_pct": mem_pct,
            "mem_limit_mb": (host_cfg.get("Memory") or 0) // (1024 * 1024) or None,
        })
    return out


def _calc_cpu_pct(stats: dict[str, Any]) -> float:
    """Docker CPU% formula — same as ``docker stats`` CLI."""
    try:
        cpu = stats.get("cpu_stats") or {}
        pre = stats.get("precpu_stats") or {}
        cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
        pre_total = (pre.get("cpu_usage") or {}).get("total_usage", 0)
        sys_total = cpu.get("system_cpu_usage", 0)
        pre_sys = pre.get("system_cpu_usage", 0)
        online_cpus = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1

        cpu_delta = cpu_total - pre_total
        sys_delta = sys_total - pre_sys
        if sys_delta > 0 and cpu_delta > 0:
            return (cpu_delta / sys_delta) * online_cpus * 100.0
    except Exception:
        pass
    return 0.0


def collect_celery_queues() -> dict[str, int]:
    """Queue depths from Celery's broker. Best-effort — returns {} if
    the broker is unreachable. Reuses the existing ``celery_app``
    connection rather than opening a new Redis client."""
    try:
        from packages.core.celery_app import celery_app
        with celery_app.connection_or_acquire() as conn:
            depths: dict[str, int] = {}
            for q in ("celery", "default"):
                try:
                    info = conn.default_channel.queue_declare(queue=q, passive=True)
                    depths[q] = info.message_count
                except Exception:
                    pass
            return depths
    except Exception as exc:
        logger.debug("celery queue probe failed: %s", exc)
        return {}


def collect_snapshot() -> dict[str, Any]:
    """Full snapshot — host + containers + queues. Used by the Celery
    collector tick and by the snapshot endpoint as a live fallback."""
    return {
        "ts": int(time.time()),
        "host": collect_host_metrics(),
        "containers": collect_docker_containers(),
        "queues": collect_celery_queues(),
    }


# ── Log scanning ───────────────────────────────────────────────────────

import re

# Match common Python / Node / panic patterns. Greedy ``ERROR`` alone
# would catch "ERROR_CODES" / "no errors" — anchor to a word-ish boundary.
_ERROR_PATTERN = re.compile(
    r"(?i)(\bERROR\b|\bCRITICAL\b|\bFATAL\b|Traceback \(most recent call last\)|panic:|unhandled exception|^\s*Error:)",
    re.MULTILINE,
)


def fetch_container_logs(name: str, since_seconds: int = 60, tail: int = 2000) -> str:
    """Read recent stderr+stdout from a container. Returns "" on any
    error (Docker socket missing, container gone, etc.)."""
    client = _safe_docker_client()
    if client is None:
        return ""
    try:
        c = client.containers.get(name)
        # ``logs`` returns bytes; ``since=`` accepts unix ts or seconds
        # ago via ``datetime``. Pass an int = seconds-ago timestamp.
        from datetime import datetime, timedelta, timezone
        since_dt = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
        raw = c.logs(stdout=True, stderr=True, since=since_dt, tail=tail, timestamps=False)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    except Exception as exc:
        logger.debug("fetch_container_logs(%s) failed: %s", name, exc)
        return ""


def count_errors(text: str) -> int:
    """Cheap regex match count over the log text."""
    if not text:
        return 0
    return len(_ERROR_PATTERN.findall(text))


def collect_log_error_counts(since_seconds: int = 60) -> dict[str, int]:
    """Per-container error count over the recent window. Skipped for
    non-running and init containers — they don't emit live logs."""
    client = _safe_docker_client()
    if client is None:
        return {}
    out: dict[str, int] = {}
    try:
        for c in client.containers.list(all=False):
            name = c.name
            if name.endswith("-init"):
                continue
            text = fetch_container_logs(name, since_seconds=since_seconds, tail=2000)
            if text:
                out[name] = count_errors(text)
    except Exception as exc:
        logger.warning("collect_log_error_counts failed: %s", exc)
    return out


def filter_log_lines(text: str, level: str | None = None, limit: int = 200) -> list[str]:
    """Return up to ``limit`` lines from ``text``, optionally filtered
    to lines matching the error pattern when ``level == 'error'``.
    Returned newest-first."""
    if not text:
        return []
    lines = text.splitlines()
    if level == "error":
        lines = [ln for ln in lines if _ERROR_PATTERN.search(ln)]
    # Newest at top — Docker emits oldest-first.
    lines = list(reversed(lines))
    return lines[:limit]
