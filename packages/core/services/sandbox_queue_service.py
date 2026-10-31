"""Durable fair queue and stateful routing for external Sandbox runners."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from packages.core.config import get_settings
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
    SandboxInstance,
    SandboxRequesterKind,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.services.runtime_run_terminal import finish_runtime_run_out_of_loop

logger = logging.getLogger(__name__)

class SandboxQueueFullError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SandboxRouteNotFoundError(LookupError):
    pass


class SandboxAllocationStateError(RuntimeError):
    pass


# Runner pools. A reservation carries ``request_payload["pool"]`` and is only
# placed on a runner of the same pool. ``normalize_runner_pool`` is the one
# rule for reading a pool name; ``claim_next_sandbox_allocation`` mirrors it
# in SQL, so both sides must strip exactly the same characters.
DEFAULT_RUNNER_POOL = "default"
RUNNER_POOL_MAX_LENGTH = 32  # a longer name cannot be stored on the runner row
_RUNNER_POOL_TRIM_CHARS = " \t\r\n\f\v"


def normalize_runner_pool(value: Any) -> str:
    """Return the pool name: non-string or blank means the default pool."""

    if not isinstance(value, str):
        return DEFAULT_RUNNER_POOL
    return value.strip(_RUNNER_POOL_TRIM_CHARS) or DEFAULT_RUNNER_POOL




def _ticket_pool_filters(pool: str | None) -> list[Any]:
    """Filters keeping only tickets of runner pool ``pool`` (None: every pool)."""

    filters: list[Any] = []
    return filters


_PENDING_RESERVATION_STATUSES = (
    SandboxReservationStatus.PENDING_CHECKPOINT.value,
    SandboxReservationStatus.QUEUED.value,
    SandboxReservationStatus.REQUEUED.value,
    SandboxReservationStatus.ALLOCATING.value,
)
# A ticket that owns (or is acquiring) a runner slot. The per-user fairness
# rule in ``claim_next_sandbox_allocation`` counts these.
_IN_FLIGHT_RESERVATION_STATUSES = (
    SandboxReservationStatus.ALLOCATING.value,
    SandboxReservationStatus.ALLOCATED.value,
)

# A CONSUMED reservation no longer pins a runner slot: since warm reuse, the
# container outlives its reservation, so occupancy is driven by the runner's
# observed ``active_sandboxes`` (health check) once allocation has finished.
_RUNNER_SLOT_RESERVATION_STATUSES = (
    SandboxReservationStatus.ALLOCATING.value,
    SandboxReservationStatus.ALLOCATED.value,
    SandboxReservationStatus.RELEASE_PENDING.value,
)
_LEGACY_RUNNER_ACTIVE_LIMIT = 5
_LEGACY_RUNNER_EXECUTING_LIMIT = 2

#: ``SandboxRunner.config`` key marking a runner that a pool manager (the
#: runner autoscaler) created and owns. Static configuration never adopts,
#: drains or rewrites such a row, and the scheduler fills unmarked (static)
#: runners first so a managed runner can go idle and be retired.
POOL_MANAGED_CONFIG_KEY = "pool_managed"


def is_pool_managed_runner(runner: SandboxRunner) -> bool:
    return bool((runner.config or {}).get(POOL_MANAGED_CONFIG_KEY))


# ── Memory-aware placement ──────────────────────────────────────
#
# A runner admits containers by memory (its ``/health`` reports the budget);
# the scheduler mirrors that so it only claims a ticket for a runner whose
# reported free memory fits the ticket's container. A runner whose health has
# no memory fields (an older version mid-rollout) is placed by slots alone.

_MIB = 1024 * 1024
_SIZE_UNITS = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}

#: ``request_payload`` key holding the container memory a ticket needs, in
#: MiB; absent means "the runner's default container size".
SANDBOX_MEMORY_KEY = "memory_mb"
#: ``request_payload`` key holding the CPUs a ticket's container asks for;
#: absent means the runner default. CPUs are a limit, not a reservation (the
#: runner overcommits them like Docker shares), so they decide which machine
#: class a ticket needs, never how many tickets fit on one runner.
SANDBOX_CPUS_KEY = "cpus"
#: ``SandboxRunner.config`` key holding the runner's last memory report.
RUNNER_MEMORY_KEY = "memory"

#: Runner error codes that mean "no room right now" rather than "this request
#: is wrong": the ticket waits for capacity instead of failing.
CAPACITY_ERROR_CODES = frozenset({"runner_full", "capacity_exceeded"})
#: HTTP statuses of a runner rejecting the request itself; retrying the same
#: request can never succeed, so the ticket fails at once.
_FATAL_ALLOCATION_STATUSES = frozenset({400, 401, 403, 404, 413, 422})
#: Runner error codes that mean "this runner is not serving right now": it is
#: draining for a restart (a channel update), shutting down, or cannot reach
#: its Docker daemon. The runner comes back on its own, so the ticket waits
#: for it like for capacity instead of spending its few retry attempts.
RUNNER_UNAVAILABLE_ERROR_CODES = frozenset({"sandbox_service_draining", "shutting_down", "docker_unavailable"})
#: A gateway's answer for a runner that is not listening (no runner code).
_RUNNER_UNAVAILABLE_STATUSES = frozenset({502, 503})
#: ``last_error`` label of a ticket waiting for (or expired waiting for) an
#: unavailable runner.
RUNNER_UNAVAILABLE_ERROR = "sandbox_runner_unavailable"


def parse_memory_mb(value: Any) -> int | None:
    """Docker-style size (``512m``, ``2g``, bytes as int) in whole MiB, or None."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) // _MIB if value > 0 else None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?\s*$", str(value), re.IGNORECASE)
    if not match:
        return None
    number, unit = match.groups()
    size = int(float(number) * (1024 ** _SIZE_UNITS[unit.lower()]))
    return size // _MIB if size > 0 else None


def ticket_memory_mb(payload: dict[str, Any] | None) -> int | None:
    """Container memory a ticket needs (MiB); None means the runner default."""

    payload = payload or {}
    explicit = payload.get(SANDBOX_MEMORY_KEY)
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and explicit > 0:
        return int(explicit)
    config = payload.get("config")
    if isinstance(config, dict):
        return parse_memory_mb(config.get("memory"))
    return parse_memory_mb(getattr(config, "memory", None)) if config is not None else None


def _positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def ticket_cpus(payload: dict[str, Any] | None) -> float | None:
    """CPUs a ticket's container asks for; None means the runner default."""

    payload = payload or {}
    explicit = _positive_float(payload.get(SANDBOX_CPUS_KEY))
    if explicit is not None:
        return explicit
    config = payload.get("config")
    if isinstance(config, dict):
        return _positive_float(config.get("cpus"))
    return _positive_float(getattr(config, "cpus", None)) if config is not None else None


def runner_memory_from_health(health: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    """The memory report a scheduler can place by, or None for an older runner."""

    try:
        available = int(health["memory_available_mb"])
        max_container = int(health["max_container_memory_mb"])
    except (KeyError, TypeError, ValueError):
        return None
    defaults = health.get("container_defaults") if isinstance(health.get("container_defaults"), dict) else {}
    default_mb = defaults.get("memory_mb")
    try:
        default_mb = int(default_mb) if default_mb is not None else None
    except (TypeError, ValueError):
        default_mb = None
    report: dict[str, Any] = {
        "available_mb": max(0, available),
        "max_container_mb": max(0, max_container),
        "default_mb": default_mb,
        "observed_at": now.isoformat(),
    }
    for source, target in (
        ("memory_budget_mb", "budget_mb"),
        ("memory_reserved_mb", "reserved_mb"),
        ("largest_allocatable_mb", "largest_allocatable_mb"),
    ):
        if health.get(source) is not None:
            try:
                report[target] = int(health[source])
            except (TypeError, ValueError):
                pass
    host_cpus = _positive_float(health.get("host_cpus"))
    if host_cpus is not None:
        report["host_cpus"] = host_cpus
    return report


def record_runner_memory(runner: SandboxRunner, report: dict[str, Any] | None) -> None:
    """Store (or, for a runner without memory fields, drop) its memory report."""

    config = dict(runner.config or {})
    if report is None:
        config.pop(RUNNER_MEMORY_KEY, None)
    else:
        previous = config.get(RUNNER_MEMORY_KEY)
        if "host_cpus" not in report and isinstance(previous, dict) and previous.get("host_cpus"):
            # A capacity rejection reports memory only; the machine's CPUs did not change.
            report = {**report, "host_cpus": previous["host_cpus"]}
        config[RUNNER_MEMORY_KEY] = report
    runner.config = config


def runner_memory_report(runner: SandboxRunner) -> dict[str, Any] | None:
    """The runner's last memory report (``record_runner_memory``), or None.

    The one reader of ``SandboxRunner.config["memory"]``: the claim, the
    handover and the pool autoscaler all place by it.
    """

    return _runner_memory(runner)


def _runner_memory(runner: SandboxRunner) -> dict[str, Any] | None:
    report = (runner.config or {}).get(RUNNER_MEMORY_KEY)
    if not isinstance(report, dict):
        return None
    if report.get("available_mb") is None or report.get("max_container_mb") is None:
        return None
    return report


def memory_need_on_runner(need_mb: int | None, report: dict[str, Any]) -> int:
    """What a ticket takes from this runner: its ask (or the runner default), clamped.

    The runner clamps a container above its per-container maximum to that
    maximum, so a request sized for a bigger runner needs at most the
    maximum here and can never become unschedulable.
    """

    need = need_mb if need_mb is not None else report.get("default_mb")
    if need is None:
        need = 0
    max_container = int(report.get("max_container_mb") or 0)
    return min(int(need), max_container) if max_container > 0 else int(need)


def _observed_at(report: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(report.get("observed_at")))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _memory_claimed_since_report(
    db: AsyncSession,
    runners: list[SandboxRunner],
) -> dict[str, int]:
    """Memory of tickets placed on each runner that its last report cannot show yet.

    A ticket still ALLOCATING may not have a container yet, and one
    allocated after the report was taken is missing from it. Counting both
    keeps a burst of claims between two health checks from overcommitting
    a runner; a container that was in fact already counted is only counted
    twice until the next report (conservative, never over-admits).
    """

    reports = {runner.id: _runner_memory(runner) for runner in runners}
    runner_ids = [runner_id for runner_id, report in reports.items() if report is not None]
    if not runner_ids:
        return {}
    rows = (
        await db.execute(
            select(
                SandboxReservation.runner_id,
                SandboxReservation.status,
                SandboxReservation.allocated_at,
                SandboxReservation.request_payload[SANDBOX_MEMORY_KEY].astext,
                SandboxReservation.request_payload["config"]["memory"].astext,
            ).where(
                SandboxReservation.runner_id.in_(runner_ids),
                SandboxReservation.status.in_(_RUNNER_SLOT_RESERVATION_STATUSES + (
                    SandboxReservationStatus.CONSUMED.value,
                )),
            )
        )
    ).all()
    claimed: dict[str, int] = {}
    for runner_id, status, allocated_at, memory_mb, config_memory in rows:
        report = reports.get(runner_id)
        if report is None:
            continue
        if status != SandboxReservationStatus.ALLOCATING.value:
            observed_at = _observed_at(report)
            if allocated_at is None or observed_at is None or allocated_at <= observed_at:
                continue
        need_mb = ticket_memory_mb({
            SANDBOX_MEMORY_KEY: int(memory_mb) if str(memory_mb or "").isdigit() else None,
            "config": {"memory": config_memory} if config_memory else None,
        })
        claimed[runner_id] = claimed.get(runner_id, 0) + memory_need_on_runner(need_mb, report)
    return claimed


@dataclass(frozen=True)
class PoolGrowth:
    """What an autoscaler adds to a runner pool on demand (Cloud only)."""

    #: Container memory (MiB) the largest machine it may create offers; 0 = unknown.
    memory_mb: int = 0
    #: CPUs of that machine; 0 = unknown.
    cpus: float = 0.0
    #: How long a ticket may have to wait until a new machine serves it.
    wait_seconds: int = 0


@dataclass(frozen=True)
class PoolCeiling:
    """The largest container a runner pool can hold: on a runner now, or once it grows."""

    memory_mb: int = 0  # 0 = unknown
    cpus: float = 0.0  # 0 = unknown


def pool_growth(pool: Any) -> PoolGrowth | None:
    """The live autoscale fleet growing ``pool``, if any."""

    return None


def pool_wait_seconds(pool: Any) -> int:
    """How long a ticket for ``pool`` may wait for its container.

    The queue budget, or -- for a pool a live autoscale fleet creates runners
    for on demand -- at least as long as that fleet may take to bring one up
    (sustain + cooldown + provision timeout): from zero runners the first
    ticket waits for a machine to boot before anything can serve it. The
    default pool keeps the queue budget.
    """

    base = int(get_settings().SANDBOX_QUEUE_MAX_WAIT_SECONDS)
    if normalize_runner_pool(pool) == DEFAULT_RUNNER_POOL:
        return base
    growth = pool_growth(pool)
    return max(base, int(growth.wait_seconds)) if growth is not None else base


def pool_ceiling(runners: Iterable[SandboxRunner], growth: PoolGrowth | None) -> PoolCeiling:
    """The pool's ceiling from its healthy runners' reports and its fleet's sizes."""

    memory = int(growth.memory_mb) if growth is not None else 0
    cpus = float(growth.cpus) if growth is not None else 0.0
    for runner in runners:
        report = _runner_memory(runner)
        if report is None:
            continue
        memory = max(memory, int(report.get("max_container_mb") or 0))
        cpus = max(cpus, float(report.get("host_cpus") or 0))
    return PoolCeiling(memory_mb=memory, cpus=cpus)


async def load_pool_ceiling(db: AsyncSession, pool: Any) -> PoolCeiling:
    """``pool_ceiling`` of the pool's healthy runners (busy ones included)."""

    filters = [SandboxRunner.status == SandboxRunnerStatus.HEALTHY.value]
    runners = (await db.execute(select(SandboxRunner).where(*filters))).scalars().all()
    return pool_ceiling(runners, pool_growth(pool))


def _within(need: float, ceiling: float) -> float:
    return min(need, ceiling) if ceiling > 0 else need


def runner_holds_ticket(
    runner: SandboxRunner,
    need_mb: int | None,
    need_cpus: float | None,
    ceiling: PoolCeiling | None,
) -> bool:
    """Is this runner's machine class big enough for the ticket's container?

    A runner shrinks a container above its per-container maximum (memory) or
    its CPU count. Only the pool's largest class may do that: a ticket asking
    more than the pool can ever give gets the largest, but one that a bigger
    runner -- serving now, or one the pool's fleet would create -- can hold
    as asked never lands shrunk on a smaller one. Without a ceiling (or a
    report) every runner holds every ticket, as before.
    """

    report = _runner_memory(runner)
    if report is None or ceiling is None:
        return True
    memory = need_mb if need_mb is not None else report.get("default_mb")
    max_container = int(report.get("max_container_mb") or 0)
    if memory is not None and max_container > 0 and _within(int(memory), ceiling.memory_mb) > max_container:
        return False
    host_cpus = float(report.get("host_cpus") or 0)
    if need_cpus and host_cpus > 0 and _within(float(need_cpus), ceiling.cpus) > host_cpus:
        return False
    return True


def runner_fits_ticket(
    runner: SandboxRunner,
    need_mb: int | None,
    *,
    claimed_mb: int = 0,
    need_cpus: float | None = None,
    ceiling: PoolCeiling | None = None,
) -> bool:
    """Does the runner hold the ticket's class and have the free memory for it? (No report: yes.)"""

    report = _runner_memory(runner)
    if report is None:
        return True
    if not runner_holds_ticket(runner, need_mb, need_cpus, ceiling):
        return False
    need = memory_need_on_runner(need_mb, report)
    return need <= int(report.get("available_mb") or 0) - claimed_mb


def classify_allocation_error(exc: BaseException) -> tuple[str, str]:
    """Sort a failed create into ``capacity`` / ``unavailable`` / ``fatal`` / ``retry`` plus its error text.

    * ``capacity``: the runner is full (429 or a capacity code). Not a failure:
      the ticket waits, without using an attempt, until its deadline.
    * ``unavailable``: the runner is not serving right now -- draining or
      restarting (a self-update), unreachable, or its /health unreadable. Not
      a failure either: the ticket waits for it the same way.
    * ``fatal``: the runner rejected the request itself (bad or forbidden
      config, a dependency install that failed, a wrong exit IP, an isolation
      that cannot be resolved), or reported lacking a capability it needs
      (``runner_outdated``). It fails at once.
    * ``retry``: anything else (other 5xx, timeouts); bounded retries as before.
    """

    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    detail = str(exc).strip()
    label = str(code or (f"http_{status}" if status else exc.__class__.__name__))
    text_ = f"{label}: {detail}" if detail and detail != label else label
    if status == 429 or code in CAPACITY_ERROR_CODES:
        return "capacity", text_[:2000]
    if _runner_unavailable(exc, status=status, code=code):
        return "unavailable", text_[:2000]
    if code == "runner_outdated" or (isinstance(status, int) and status in _FATAL_ALLOCATION_STATUSES):
        # A runner that reported it lacks what the request needs refuses the
        # same request on every attempt, like a request it rejects.
        return "fatal", text_[:2000]
    return "retry", exc.__class__.__name__


def _runner_unavailable(exc: BaseException, *, status: Any, code: Any) -> bool:
    import httpx

    from packages.core.services.sandbox_sdk.exceptions import (
        SandboxConnectionError,
        SandboxRunnerOutdatedError,
    )

    if code in RUNNER_UNAVAILABLE_ERROR_CODES:
        return True
    if code is None and status in _RUNNER_UNAVAILABLE_STATUSES:
        return True
    if isinstance(exc, SandboxRunnerOutdatedError):
        # Refused before sending because /health could not be read: the
        # runner is down, not outdated. A runner that reported what it lacks
        # stays a refusal.
        return not exc.reported
    # Nothing listening (refused, or a port proxy whose container is still
    # starting drops the connection). The create is idempotent per ticket, so
    # asking again once the runner is back is safe.
    return isinstance(
        exc, (SandboxConnectionError, httpx.NetworkError, httpx.ConnectTimeout, httpx.RemoteProtocolError),
    )


def _is_capacity_error_text(error: str | None) -> bool:
    return str(error or "").split(":", 1)[0].strip() in CAPACITY_ERROR_CODES


def _is_runner_unavailable_text(error: str | None) -> bool:
    return str(error or "").split(":", 1)[0].strip() == RUNNER_UNAVAILABLE_ERROR


def effective_runner_limits(
    *,
    observed_active: int,
    observed_executing: int,
    active_cap: int,
    executing_cap: int,
) -> tuple[int, int]:
    """Apply optional coordinator caps to the runner's observed capacity."""

    active = max(1, observed_active)
    if active_cap > 0:
        active = min(active, active_cap)
    executing = min(active, max(1, observed_executing))
    if executing_cap > 0:
        executing = min(executing, executing_cap)
    return active, executing


async def reconciled_runner_active_count(
    db: AsyncSession,
    *,
    runner_id: str,
    observed_active: int,
) -> int:
    """Reconcile runner capacity without dropping in-flight coordinator slots."""

    coordinator_active = await db.scalar(
        select(func.count(SandboxReservation.id)).where(
            SandboxReservation.runner_id == runner_id,
            SandboxReservation.status.in_(_RUNNER_SLOT_RESERVATION_STATUSES),
        )
    )
    return max(int(coordinator_active or 0), max(0, int(observed_active)))


async def claim_due_sandbox_runner_health_checks(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    interval_seconds: int | None = None,
) -> list[str]:
    """Claim due runner probes so scheduler replicas do not duplicate them."""

    current_time = now or datetime.now(timezone.utc)
    interval = max(
        1,
        interval_seconds or get_settings().SANDBOX_RUNNER_HEALTH_INTERVAL_SECONDS,
    )
    runners = list(
        (
            await db.execute(
                select(SandboxRunner)
                .where(
                    SandboxRunner.status != SandboxRunnerStatus.DRAINING.value,
                    or_(
                        SandboxRunner.last_health_at.is_(None),
                        SandboxRunner.last_health_at
                        <= current_time - timedelta(seconds=interval),
                    ),
                )
                .order_by(SandboxRunner.id.asc())
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for runner in runners:
        runner.last_health_at = current_time
    return [runner.id for runner in runners]


def configured_sandbox_runners(
    raw_json: str | None = None,
    *,
    fallback_url: str | None = None,
) -> list[dict[str, Any]]:
    """Parse runner config while retaining the one-runner migration path."""

    settings = get_settings()
    raw = settings.SANDBOX_RUNNERS_JSON if raw_json is None else raw_json
    fallback = settings.SANDBOX_SERVICE_URL if fallback_url is None else fallback_url
    if str(raw or "").strip():
        parsed = json.loads(str(raw))
        if not isinstance(parsed, list):
            raise ValueError("SANDBOX_RUNNERS_JSON must be a JSON list")
    elif str(fallback or "").strip():
        parsed = [{"id": "default", "base_url": str(fallback).strip()}]
    else:
        return []

    runners: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    default_active_cap = max(0, settings.SANDBOX_RUNNER_ACTIVE_LIMIT)
    default_executing_cap = max(0, settings.SANDBOX_RUNNER_EXECUTING_LIMIT)
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("each Sandbox runner must be a JSON object")
        runner_id = str(item.get("id") or "").strip()
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if not runner_id or not base_url.startswith(("http://", "https://")):
            raise ValueError("each Sandbox runner requires id and HTTP(S) base_url")
        if runner_id in seen_ids:
            raise ValueError(f"duplicate Sandbox runner id: {runner_id}")
        seen_ids.add(runner_id)
        active_cap = max(0, int(item.get("active_limit", default_active_cap) or 0))
        executing_cap = max(
            0,
            int(item.get("executing_limit", default_executing_cap) or 0),
        )
        pool = normalize_runner_pool(item.get("pool"))
        if len(pool) > RUNNER_POOL_MAX_LENGTH:
            raise ValueError(
                f"Sandbox runner {runner_id} pool exceeds "
                f"{RUNNER_POOL_MAX_LENGTH} characters"
            )
        runners.append(
            {
                "id": runner_id,
                "base_url": base_url,
                "active_limit": active_cap,
                "executing_limit": executing_cap,
                "config": dict(item.get("config") or {}),
                "pool": pool,
            }
        )
    return runners


async def reconcile_sandbox_runners(
    db: AsyncSession,
    runner_configs: list[dict[str, Any]],
) -> list[SandboxRunner]:
    """Upsert configured runners and drain rows removed from configuration."""

    configured_ids: set[str] = set()
    rows: list[SandboxRunner] = []
    for config in runner_configs:
        runner_id = str(config["id"])
        configured_ids.add(runner_id)
        runner = (
            await db.execute(
                select(SandboxRunner)
                .where(SandboxRunner.id == runner_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if runner is not None and is_pool_managed_runner(runner):
            continue  # a pool-managed id is never taken over by static config
        active_cap = max(0, int(config.get("active_limit") or 0))
        executing_cap = max(0, int(config.get("executing_limit") or 0))
        if runner is None:
            runner = SandboxRunner(
                id=runner_id,
                base_url=str(config["base_url"]),
                active_limit=_LEGACY_RUNNER_ACTIVE_LIMIT,
                executing_limit=_LEGACY_RUNNER_EXECUTING_LIMIT,
            )
            db.add(runner)
        existing_config = dict(runner.config or {})
        observed_active = max(
            1,
            int(existing_config.get("observed_active_limit") or runner.active_limit or 1),
        )
        observed_executing = max(
            1,
            int(
                existing_config.get("observed_executing_limit")
                or runner.executing_limit
                or 1
            ),
        )
        base_url = str(config["base_url"]).rstrip("/")
        url_changed = runner.base_url.rstrip("/") != base_url
        runner.base_url = base_url
        runner.active_limit, runner.executing_limit = effective_runner_limits(
            observed_active=observed_active,
            observed_executing=observed_executing,
            active_cap=active_cap,
            executing_cap=executing_cap,
        )
        runner.config = {
            **dict(config.get("config") or {}),
            "active_cap": active_cap,
            "executing_cap": executing_cap,
            "observed_active_limit": observed_active,
            "observed_executing_limit": observed_executing,
        }
        if url_changed or runner.status == SandboxRunnerStatus.DRAINING.value:
            runner.status = SandboxRunnerStatus.UNKNOWN.value
            runner.last_health_at = None
            runner.last_error = None
        rows.append(runner)

    existing = list(
        (
            await db.execute(select(SandboxRunner).with_for_update())
        ).scalars().all()
    )
    for runner in existing:
        if runner.id not in configured_ids and not is_pool_managed_runner(runner):
            runner.status = SandboxRunnerStatus.DRAINING.value
    return rows


async def enqueue_sandbox_reservation(
    db: AsyncSession,
    *,
    run: RuntimeRun | None = None,
    tool_call_id: str,
    request_payload: dict[str, Any],
    priority: int = 100,
    now: datetime | None = None,
    max_pending: int | None = None,
    max_wait_seconds: int | None = None,
    activate: bool = True,
    entity_id: str | None = None,
    user_id: str | None = None,
    requester_kind: str = SandboxRequesterKind.RUN.value,
    requester_id: str | None = None,
    scope_key: str | None = None,
    follows: str | None = None,
    root_run_id: str | None = None,
) -> SandboxReservation:
    """Create one idempotent ticket and enforce bounded pending admission.

    A durable Runtime run passes ``run`` and is suspended on the ticket. A
    requester with no ``RuntimeRun`` (a task worker, or a delegated agent
    running inside its parent's tool call) passes ``run=None`` plus its own
    identity and polls the ticket instead of being resumed by the outbox. A
    requester that handles its own ticket inside a durable run (a call that
    cannot park) passes that run's ``root_run_id``: the ticket is not the
    run's to resume on, but a cancel of the run still reaches its container.

    ``scope_key`` (polling requesters only) makes admission single-flight per
    sandbox scope (owner x conversation x container role): when a ticket for
    that scope is still in flight -- queued, allocating, or allocated and not
    yet attached -- it is returned instead of a new one, so concurrent
    admissions of one conversation share one ticket and one container. The
    caller recognises a joined ticket by its ``tool_call_id``. ``user_id`` may
    be empty for a user-less owner (system work owned by a workspace).

    ``follows`` (durable runs only) names the scope's in-flight ticket this
    run's ticket waits behind instead of creating a second container (see
    ``SANDBOX_FOLLOWS_PAYLOAD_KEY``).
    """

    if run is not None:
        requester_kind = SandboxRequesterKind.RUN.value
        requester_id = run.id
        owner_entity_id = run.entity_id
        owner_user_id = run.user_id
    else:
        if requester_kind != SandboxRequesterKind.TASK.value:
            raise ValueError("a reservation without a run must be requester_kind='task'")
        if not requester_id:
            raise ValueError("a task reservation requires requester_id")
        if not entity_id:
            raise ValueError("a task reservation requires entity_id")
        owner_entity_id = entity_id
        owner_user_id = str(user_id or "")

    settings = get_settings()
    current_time = now or datetime.now(timezone.utc)
    global_limit = max_pending or settings.SANDBOX_QUEUE_MAX_PENDING
    wait_seconds = max_wait_seconds or pool_wait_seconds((request_payload or {}).get("pool"))

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('manor:sandbox:queue'))"))
    if run is not None:
        idempotency_filters = (
            SandboxReservation.runtime_run_id == run.id,
            SandboxReservation.tool_call_id == tool_call_id,
        )
    else:
        idempotency_filters = (
            SandboxReservation.requester_kind == requester_kind,
            SandboxReservation.requester_id == requester_id,
            SandboxReservation.tool_call_id == tool_call_id,
        )
    existing = (
        await db.execute(select(SandboxReservation).where(*idempotency_filters))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    if scope_key and run is None:
        in_flight = await find_inflight_scope_reservation(db, scope_key=scope_key, now=now)
        if in_flight is not None:
            return in_flight
    if follows and run is not None:
        request_payload = {**dict(request_payload), SANDBOX_FOLLOWS_PAYLOAD_KEY: follows}

    pending_count = await db.scalar(
        select(func.count(SandboxReservation.id)).where(
            SandboxReservation.status.in_(_PENDING_RESERVATION_STATUSES)
        )
    )
    if int(pending_count or 0) >= global_limit:
        raise SandboxQueueFullError("sandbox_queue_full")
    # No per-user admission cap: one user's second ticket is queued behind
    # their first and serialised at claim time (``claim_next_sandbox_allocation``),
    # so the requester never has to wait in-process, holding a worker slot,
    # for its own earlier ticket to drain.

    reservation = SandboxReservation(
        runtime_run_id=run.id if run is not None else None,
        root_run_id=run.root_run_id if run is not None else (root_run_id or None),
        requester_kind=requester_kind,
        requester_id=requester_id,
        tool_call_id=tool_call_id,
        entity_id=owner_entity_id,
        user_id=owner_user_id,
        status=(
            SandboxReservationStatus.QUEUED.value
            if activate
            else SandboxReservationStatus.PENDING_CHECKPOINT.value
        ),
        priority=priority,
        enqueued_at=current_time,
        deadline_at=current_time + timedelta(seconds=wait_seconds),
        request_payload=_with_memory_need(request_payload),
    )
    db.add(reservation)
    await db.flush()
    if run is not None:
        run.reservation_id = reservation.id
        # A pending-checkpoint ticket is taken by the turn that still owns the
        # run: the run is parked (status + version) only when that turn
        # persists its suspension. Bumping the version here revoked the
        # turn's own context writer, so it crashed before it could park and
        # the ticket expired unqueued.
        if activate:
            run.status = RuntimeRunStatus.WAITING_RESOURCE.value
            run.status_reason = "sandbox_capacity"
            run.version += 1
    return reservation


def _with_memory_need(request_payload: dict[str, Any]) -> dict[str, Any]:
    """The ticket's payload carrying the container memory (MiB) and CPUs it needs.

    Each is absent when the request leaves it to the runner's default; the
    scheduler then uses each runner's reported default size.
    """

    payload = dict(request_payload)
    for key, need in (
        (SANDBOX_MEMORY_KEY, ticket_memory_mb(payload)),
        (SANDBOX_CPUS_KEY, ticket_cpus(payload)),
    ):
        if need is not None:
            payload[key] = need
        else:
            payload.pop(key, None)
    return payload


#: ``request_payload`` key naming the sandbox scope a ticket allocates for
#: (written by ``sandbox_session_service``).
SANDBOX_SCOPE_PAYLOAD_KEY = "sandbox_scope"
#: ``request_payload`` key of a FOLLOWER ticket: a durable run that asked for
#: a scope's container while another ticket was already creating it. The
#: scheduler never claims a follower while its leader is pending; once the
#: leader has its container the follower is allocated to that same container
#: (``adopt_follower_reservations``), so one user gets ONE container even when
#: several of their conversations ask at once.
SANDBOX_FOLLOWS_PAYLOAD_KEY = "follows"
#: The leader's container, on an allocated follower (the ``sandbox_id``
#: column is unique to the ticket that created the container).
SANDBOX_ATTACHED_PAYLOAD_KEY = "attached_sandbox_id"
#: ``request_payload`` of an exec-slot wait ticket (``EXEC_WAITING``).
SANDBOX_EXEC_WAIT_KIND = "exec_slot"


def reservation_sandbox_id(reservation: SandboxReservation) -> str:
    """The container an allocated ticket names: its own, or its leader's (a follower)."""

    payload = getattr(reservation, "request_payload", None)
    payload = payload if isinstance(payload, dict) else {}
    return str(getattr(reservation, "sandbox_id", None) or payload.get(SANDBOX_ATTACHED_PAYLOAD_KEY) or "")


def _is_follower():
    """SQL: this ticket follows another ticket's container.

    Never claimed for a container of its own: ``adopt_follower_reservations``
    settles every follower each scheduler tick, before the claim -- allocated
    to its leader's container, or (the leader ended without one) turned into
    an ordinary ticket. Even a follower written between the two is never
    claimed while its leader is still creating, nor after it created.
    """

    return SandboxReservation.request_payload[SANDBOX_FOLLOWS_PAYLOAD_KEY].astext.is_not(None)


_SCOPE_JOINABLE_STATUSES = (
    *_PENDING_RESERVATION_STATUSES,
    SandboxReservationStatus.ALLOCATED.value,
)


async def find_inflight_scope_reservation(
    db: AsyncSession,
    *,
    scope_key: str,
    now: datetime | None = None,
) -> SandboxReservation | None:
    """The oldest ticket still allocating a container for ``scope_key``, if any.

    Joinable: waiting for a runner, being created, or allocated but not yet
    attached. A consumed ticket is not: its container is in the warm
    registry, where every later admission finds it.
    """

    current_time = now or datetime.now(timezone.utc)
    return (
        await db.execute(
            select(SandboxReservation)
            .where(
                SandboxReservation.status.in_(_SCOPE_JOINABLE_STATUSES),
                SandboxReservation.request_payload[SANDBOX_SCOPE_PAYLOAD_KEY].astext == scope_key,
                # A follower creates nothing; join the ticket that does.
                SandboxReservation.request_payload[SANDBOX_FOLLOWS_PAYLOAD_KEY].astext.is_(None),
                or_(
                    SandboxReservation.status == SandboxReservationStatus.ALLOCATED.value,
                    SandboxReservation.deadline_at > current_time,
                ),
            )
            .order_by(SandboxReservation.enqueued_at.asc(), SandboxReservation.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def lock_sandbox_scope(db: AsyncSession, scope_key: str) -> None:
    """Serialise every decision about ``scope_key``'s container until the transaction ends.

    Taken by whatever may create, follow or re-point a ticket of the scope
    (admission, and the scheduler sweeps that settle followers and requeue
    lost tickets), around the scope-head lookup and the write it decides.
    Postgres only (no-op elsewhere, e.g. SQLite unit tests).
    """

    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is None or bind.dialect.name != "postgresql":
        return
    import hashlib

    digest = hashlib.sha256(f"sandbox-scope\0{scope_key}".encode("utf-8")).digest()[:8]
    await db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(digest, "big", signed=True))))


class SandboxScopeContainer:
    """A scope's container according to Postgres: an active instance row.

    Postgres knows WHICH container a scope has; whether it is still alive only
    the runner knows, so an admission probes it before use and retires it
    (``release_sandbox_instance``) when the runner no longer has it.
    """

    __slots__ = ("sandbox_id", "base_url", "creator_id", "created_with")

    def __init__(self, sandbox_id: str, base_url: str, creator_id: str, created_with: dict[str, Any]) -> None:
        self.sandbox_id = sandbox_id
        self.base_url = base_url
        # The ticket that created it, and the request it was created for
        # (plain values: they outlive the session that read them).
        self.creator_id = creator_id
        self.created_with = created_with

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SandboxScopeContainer {self.sandbox_id} via {self.creator_id}>"


async def find_scope_container(
    db: AsyncSession,
    *,
    scope_key: str,
    exclude_sandbox_ids: Iterable[str] = (),
) -> SandboxScopeContainer | None:
    """The newest container of ``scope_key`` whose instance row is still active.

    The instance row exists from the moment the container is allocated
    (``complete_sandbox_allocation``) until it is released, through every
    state of the ticket that created it (allocated, consumed, release
    pending). So unlike a ticket-status lookup it has no gap while the
    creating run attaches its container.
    """

    query = (
        select(
            SandboxInstance.sandbox_id,
            SandboxRunner.base_url,
            SandboxReservation.id,
            SandboxReservation.request_payload,
        )
        .join(SandboxReservation, SandboxReservation.id == SandboxInstance.reservation_id)
        .join(SandboxRunner, SandboxRunner.id == SandboxInstance.runner_id)
        .where(
            SandboxInstance.status == "active",
            SandboxReservation.request_payload[SANDBOX_SCOPE_PAYLOAD_KEY].astext == scope_key,
        )
        .order_by(SandboxInstance.created_at.desc(), SandboxInstance.sandbox_id.desc())
        .limit(1)
    )
    excluded = [str(sandbox_id) for sandbox_id in exclude_sandbox_ids if sandbox_id]
    if excluded:
        query = query.where(SandboxInstance.sandbox_id.notin_(excluded))
    row = (await db.execute(query)).first()
    if row is None:
        return None
    sandbox_id, base_url, creator_id, payload = row
    return SandboxScopeContainer(
        str(sandbox_id), str(base_url or ""), str(creator_id), dict(payload) if isinstance(payload, dict) else {},
    )


async def find_scope_head(
    db: AsyncSession,
    *,
    scope_key: str,
    exclude_sandbox_ids: Iterable[str] = (),
    now: datetime | None = None,
) -> tuple[SandboxReservation | None, SandboxScopeContainer | None]:
    """Postgres's answer to "what does this scope have right now": (leader, container).

    ``leader``: the ticket still creating the scope's container (queued,
    allocating, or allocated and not yet attached); ``container``: the
    scope's container, when no leader is in flight. Called under
    ``lock_sandbox_scope``. The leader is read first: a leader that attaches
    between the two reads still has its active instance row, so the second
    read finds its container -- there is no instant at which a scope with a
    container looks empty.
    """

    leader = await find_inflight_scope_reservation(db, scope_key=scope_key, now=now)
    if leader is not None:
        return leader, None
    return None, await find_scope_container(db, scope_key=scope_key, exclude_sandbox_ids=exclude_sandbox_ids)


async def _settle_scope_ticket(
    db: AsyncSession,
    ticket: SandboxReservation,
    *,
    now: datetime,
) -> str:
    """Re-point a pending ticket that may not create a container of its own.

    For a ticket of a scope (its payload names one) that is about to become
    claimable -- a follower whose leader ended without a container, or a
    run's ticket requeued after its container was lost -- decide under the
    scope lock: follow the scope's in-flight leader, join its live container,
    or (nothing in flight) lead. Returns "follows", "attached" or "leads".
    """

    payload = dict(ticket.request_payload or {})
    scope_key = str(payload.get(SANDBOX_SCOPE_PAYLOAD_KEY) or "")
    payload.pop(SANDBOX_FOLLOWS_PAYLOAD_KEY, None)
    if not scope_key:
        ticket.request_payload = payload
        return "leads"
    await lock_sandbox_scope(db, scope_key)
    leader, container = await find_scope_head(db, scope_key=scope_key, now=now)
    if leader is not None and leader.id != ticket.id:
        payload[SANDBOX_FOLLOWS_PAYLOAD_KEY] = leader.id
        ticket.request_payload = payload
        return "follows"
    if container is not None and container.creator_id != ticket.id:
        await _allocate_follower(db, ticket, payload, container.sandbox_id, now=now)
        return "attached"
    ticket.request_payload = payload
    return "leads"


async def _allocate_follower(
    db: AsyncSession,
    follower: SandboxReservation,
    payload: dict[str, Any],
    sandbox_id: str,
    *,
    now: datetime,
) -> None:
    """Allocate a ticket to another ticket's container (no runner slot, no instance row).

    Its run is resumed; ``attach_durable_reservation`` binds it there.
    """

    payload.pop(SANDBOX_FOLLOWS_PAYLOAD_KEY, None)
    payload[SANDBOX_ATTACHED_PAYLOAD_KEY] = sandbox_id
    follower.request_payload = payload
    follower.status = SandboxReservationStatus.ALLOCATED.value
    follower.allocated_at = now
    follower.next_attempt_at = None
    follower.version += 1
    if follower.runtime_run_id:
        run = await db.get(RuntimeRun, follower.runtime_run_id)
        if run is not None and run.status not in RuntimeRunStatus.terminal():
            run.status = RuntimeRunStatus.WAITING_RESOURCE.value
            run.status_reason = "sandbox_allocated"
            run.version += 1
        db.add(_resume_run_event(
            follower,
            now=now,
            dedupe_key=f"runtime.resume_run:{follower.id}:{follower.version}",
        ))


async def expire_sandbox_reservations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    current_time = now or datetime.now(timezone.utc)
    rows = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.status.in_(
                        [
                            # A ticket whose run never reached its checkpoint
                            # (the worker died, or the acquiring call could
                            # not suspend) is nobody's: nothing activates it.
                            SandboxReservationStatus.PENDING_CHECKPOINT.value,
                            SandboxReservationStatus.QUEUED.value,
                            SandboxReservationStatus.REQUEUED.value,
                        ]
                    ),
                    SandboxReservation.deadline_at <= current_time,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reservation in rows:
        # A ticket that kept being turned away by full runners says so: it
        # waited out its whole budget for capacity, it did not fail. One that
        # waited for its runner to come back already says so in its error.
        waited_for_capacity = _is_capacity_error_text(reservation.last_error)
        waited_for_runner = _is_runner_unavailable_text(reservation.last_error)
        if waited_for_runner:
            reason = RUNNER_UNAVAILABLE_ERROR
        else:
            reason = "sandbox_capacity_timeout" if waited_for_capacity else "sandbox_queue_timeout"
        reservation.status = SandboxReservationStatus.EXPIRED.value
        reservation.completed_at = current_time
        if waited_for_capacity:
            reservation.last_error = f"{reason}: last runner answer: {reservation.last_error}"[:2000]
        elif not waited_for_runner:
            reservation.last_error = reason
        reservation.version += 1
        # Task requesters have no run to fail; the poller observes EXPIRED.
        run = (
            (
                await db.execute(
                    select(RuntimeRun)
                    .where(RuntimeRun.id == reservation.runtime_run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reservation.runtime_run_id
            else None
        )
        # Only the run still parked on this very ticket times out with it; a
        # run that moved on to a later reservation must not be failed by a
        # stale one it abandoned.
        if run is not None and run.reservation_id == reservation.id:
            await finish_runtime_run_out_of_loop(
                db,
                run,
                status=RuntimeRunStatus.FAILED,
                reason=reason,
                now=current_time,
            )
    return len(rows) + await _release_abandoned_task_allocations(db, now=current_time)


async def _release_abandoned_task_allocations(
    db: AsyncSession,
    *,
    now: datetime,
) -> int:
    """Return the runner slots of task tickets nobody ever consumed.

    A task requester polls its own ticket instead of being resumed, so when
    the poller gives up (or its worker dies) between ALLOCATED and CONSUMED,
    nothing moves the ticket again: ALLOCATED is in
    ``_RUNNER_SLOT_RESERVATION_STATUSES`` and neither the expiry sweep nor the
    stale-allocation recovery looks at it, so the slot is pinned forever.

    Once the poller's whole wait budget plus two poll intervals have passed no
    poller can still be waiting on this ticket, so the reservation is released
    and the runner's coordinator slot returned. The *container* is not
    destroyed here: the runner's own idle reaper owns that, and until it runs
    the runner's observed ``active_sandboxes`` keeps the capacity counted, so
    releasing the ticket cannot overcommit the runner.
    """

    settings = get_settings()
    abandoned_after = timedelta(
        seconds=settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS
        + 2 * settings.SANDBOX_QUEUE_POLL_SECONDS
    )
    rows = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.requester_kind
                    == SandboxRequesterKind.TASK.value,
                    SandboxReservation.status
                    == SandboxReservationStatus.ALLOCATED.value,
                    SandboxReservation.sandbox_id.is_not(None),
                    SandboxReservation.allocated_at <= now - abandoned_after,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reservation in rows:
        released = await release_sandbox_instance(
            db,
            sandbox_id=reservation.sandbox_id,
            now=now,
        )
        if not released:
            # No active instance to release (already reaped, or never
            # recorded): retire the ticket here so it is not re-swept forever.
            reservation.status = SandboxReservationStatus.RELEASED.value
            reservation.completed_at = now
            reservation.version += 1
        reservation.last_error = "task_consume_timeout"
    return len(rows)


async def claim_next_sandbox_allocation(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    allocation_lease_seconds: int | None = None,
    max_in_flight_per_user: int | None = None,
) -> tuple[SandboxReservation, SandboxRunner] | None:
    """Claim the fairest ticket and reserve one runner slot atomically.

    Fairness across users is enforced here rather than at admission: a user
    may hold at most ``SANDBOX_QUEUE_MAX_PENDING_PER_USER`` tickets in flight
    (allocating or allocated), and their further tickets stay queued while
    other users' tickets are claimed ahead of them. Queued tickets cost
    nothing but a row, so a requester never waits in-process for its own
    earlier ticket to drain.
    """

    settings = get_settings()
    current_time = now or datetime.now(timezone.utc)
    lease_seconds = max(1, allocation_lease_seconds or settings.SANDBOX_ALLOCATION_LEASE_SECONDS)
    user_limit = max(1, max_in_flight_per_user or settings.SANDBOX_QUEUE_MAX_PENDING_PER_USER)
    in_flight = aliased(SandboxReservation)
    user_in_flight = (
        select(func.count(in_flight.id))
        .where(
            in_flight.user_id == SandboxReservation.user_id,
            in_flight.status.in_(_IN_FLIGHT_RESERVATION_STATUSES),
            # A follower allocated to its leader's container holds no runner slot.
            in_flight.runner_id.is_not(None),
        )
        .correlate(SandboxReservation)
        .scalar_subquery()
    )
    reservation_filters = [
        SandboxReservation.status.in_(
            [
                SandboxReservationStatus.QUEUED.value,
                SandboxReservationStatus.REQUEUED.value,
            ]
        ),
        SandboxReservation.deadline_at > current_time,
        or_(
            SandboxReservation.next_attempt_at.is_(None),
            SandboxReservation.next_attempt_at <= current_time,
        ),
        user_in_flight < user_limit,
        not_(_is_follower()),
    ]
    runner_filters = [
        SandboxRunner.status == SandboxRunnerStatus.HEALTHY.value,
        SandboxRunner.active_count < SandboxRunner.active_limit,
    ]
    # Head-of-line blocking is per pool: when a pool's fairest ticket fits
    # none of that pool's runners, that pool waits (no smaller ticket of it
    # jumps the head, so a big container is never starved) while the other
    # pools' tickets are still claimed this tick.
    blocked_pools: list[str] = []
    while True:
        filters = list(reservation_filters)
        reservation = (
            await db.execute(
                select(SandboxReservation)
                .where(*filters)
                .order_by(
                    SandboxReservation.priority.asc(),
                    SandboxReservation.enqueued_at.asc(),
                    SandboxReservation.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if reservation is None:
            return None

        pool = normalize_runner_pool((reservation.request_payload or {}).get("pool"))
        pool_filters = list(runner_filters)
        candidates = list(
            (
                await db.execute(
                    select(SandboxRunner)
                    .where(*pool_filters)
                    # Static runners first: a pool-managed runner only takes work
                    # the static ones have no room (slots or memory) for, so it can
                    # go idle and be retired. The memory fit below walks this order.
                    .order_by(
                        SandboxRunner.config.has_key(POOL_MANAGED_CONFIG_KEY).asc(),
                        SandboxRunner.active_count.asc(),
                        SandboxRunner.id.asc(),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        # A free slot is not enough: the runner must be of a class that holds
        # the ticket's container as asked (a larger ask is shrunk only to the
        # largest class the pool has or its fleet creates), and it must have
        # the memory for it. So the head always fits once such a runner drains.
        need_mb = ticket_memory_mb(reservation.request_payload)
        need_cpus = ticket_cpus(reservation.request_payload)
        ceiling = await load_pool_ceiling(db, pool)
        claimed = await _memory_claimed_since_report(db, candidates)
        runner = next(
            (
                candidate
                for candidate in candidates
                if runner_fits_ticket(
                    candidate,
                    need_mb,
                    claimed_mb=claimed.get(candidate.id, 0),
                    need_cpus=need_cpus,
                    ceiling=ceiling,
                )
            ),
            None,
        )
        if runner is not None:
            break
        blocked_pools.append(pool)
        return None  # a single pool: nothing else to claim

    reservation.status = SandboxReservationStatus.ALLOCATING.value
    reservation.runner_id = runner.id
    reservation.attempt_count += 1
    reservation.next_attempt_at = current_time + timedelta(seconds=lease_seconds)
    reservation.version += 1
    runner.active_count += 1
    db.add(
        RuntimeOutboxEvent(
            event_type="runtime.allocate_sandbox",
            aggregate_id=reservation.id,
            dedupe_key=f"runtime.allocate_sandbox:{reservation.id}:{reservation.version}",
            payload={
                "reservation_id": reservation.id,
                "reservation_version": reservation.version,
            },
            available_at=current_time,
            created_at=current_time,
        )
    )
    return reservation, runner


def _run_parked_on(run: RuntimeRun | None, reservation: SandboxReservation) -> bool:
    """True only for a live run still waiting on this very ticket.

    A run that moved on to a later reservation, or to a different kind of
    wait, must not be failed or rewritten by a stale ticket it abandoned.
    """

    return (
        run is not None
        and run.status not in RuntimeRunStatus.terminal()
        and run.reservation_id == reservation.id
    )


#: How many tickets queued ahead of an inline requester it claims (for the
#: scheduler to allocate) before giving up on reaching its own.
_INLINE_CLAIM_MAX_AHEAD = 20


async def claim_sandbox_reservation_now(
    db: AsyncSession,
    *,
    reservation_id: str,
    now: datetime | None = None,
) -> SandboxReservation | None:
    """Claim one ticket for its requester to allocate inline, in queue order.

    Tickets are claimed exactly as the scheduler would claim them
    (``claim_next_sandbox_allocation``: fairness, per-user limit, runner
    room), so the requester never jumps the queue: tickets ahead of it are
    claimed for the scheduler's allocation as usual, then its own. Returns
    the claimed ticket with its allocation event withdrawn (the requester
    creates the container itself), or None once no runner has room or the
    ticket is not reached.
    """

    for _ in range(_INLINE_CLAIM_MAX_AHEAD + 1):
        claim = await claim_next_sandbox_allocation(db, now=now)
        if claim is None:
            return None
        reservation, _runner = claim
        if reservation.id != reservation_id:
            continue
        await db.flush()
        await db.execute(
            delete(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.dedupe_key
                == f"runtime.allocate_sandbox:{reservation.id}:{reservation.version}",
                RuntimeOutboxEvent.delivered_at.is_(None),
            )
        )
        return reservation
    return None


async def recover_stale_sandbox_allocations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    allocation_lease_seconds: int | None = None,
    retry_delay_seconds: int = 2,
    max_attempts: int = 5,
) -> int:
    """Return slots held by allocation tasks that exceeded their lease."""

    current_time = now or datetime.now(timezone.utc)
    lease_seconds = max(
        1,
        allocation_lease_seconds or get_settings().SANDBOX_ALLOCATION_LEASE_SECONDS,
    )
    rows = list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(
                    SandboxReservation.status == SandboxReservationStatus.ALLOCATING.value,
                    or_(
                        SandboxReservation.next_attempt_at <= current_time,
                        (
                            SandboxReservation.next_attempt_at.is_(None)
                            & (
                                SandboxReservation.updated_at
                                <= current_time - timedelta(seconds=lease_seconds)
                            )
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reservation in rows:
        will_fail = (
            reservation.deadline_at <= current_time
            or reservation.attempt_count >= max_attempts
        )
        run = None
        if will_fail and reservation.runtime_run_id:
            run = (
                await db.execute(
                    select(RuntimeRun)
                    .where(RuntimeRun.id == reservation.runtime_run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
        if reservation.runner_id:
            runner = (
                await db.execute(
                    select(SandboxRunner)
                    .where(SandboxRunner.id == reservation.runner_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if runner is not None:
                runner.active_count = max(0, runner.active_count - 1)
        reservation.runner_id = None
        reservation.last_error = "sandbox_allocation_lease_expired"
        reservation.version += 1
        if will_fail:
            reservation.status = SandboxReservationStatus.FAILED.value
            reservation.completed_at = current_time
            reservation.next_attempt_at = None
            if _run_parked_on(run, reservation):
                await finish_runtime_run_out_of_loop(
                    db,
                    run,
                    status=RuntimeRunStatus.FAILED,
                    reason="sandbox_allocation_failed",
                    now=current_time,
                )
        else:
            reservation.status = SandboxReservationStatus.REQUEUED.value
            reservation.next_attempt_at = current_time + timedelta(seconds=retry_delay_seconds)
    return len(rows)


#: ``request_payload`` key counting the containers a ticket has lost.
SANDBOX_GENERATION_KEY = "sandbox_generation"


def sandbox_create_idempotency_key(reservation_id: str, payload: dict[str, Any] | None) -> str:
    """The runner's idempotency key for one ticket's container.

    Lease retries of a ticket share it on purpose; after the ticket's
    container was lost the generation changes it, so the runner creates a
    fresh container instead of returning the one it remembers for the key.
    """

    generation = int((payload or {}).get(SANDBOX_GENERATION_KEY) or 0)
    return reservation_id if generation <= 0 else f"{reservation_id}.g{generation}"


#: An allocated ticket whose run has not claimed it for this long gets its
#: resume delivered again. Normal delivery claims within seconds.
_RESUME_REDELIVERY_SECONDS = 60

#: Ticket states that end a run's wait without a container to resume onto.
_RUN_ENDING_RESERVATION_STATUSES = frozenset({
    SandboxReservationStatus.FAILED.value,
    SandboxReservationStatus.EXPIRED.value,
    SandboxReservationStatus.CANCELLED.value,
})


def _resume_redelivery_prefix(reservation: SandboxReservation) -> str:
    """Dedupe-key prefix of every redelivered resume of one ticket version."""

    return f"runtime.resume_run:{reservation.id}:{reservation.version}:redeliver:"


async def _redelivery_due(
    db: AsyncSession,
    *,
    prefix: str,
    now: datetime,
    window: int,
) -> str | None:
    """The dedupe key for a redelivery now, or None if one is not due yet.

    At most one redelivery per *sliding* window: it is due only when no
    outbox event under ``prefix`` was created in the last ``window`` seconds.
    Fixed clock buckets (``timestamp // window``) let two sweeps a second
    apart straddle a bucket boundary and redeliver twice. The returned key is
    per-second, so it never collides with an earlier redelivery.
    """

    if await db.scalar(
        select(RuntimeOutboxEvent.id)
        .where(
            RuntimeOutboxEvent.dedupe_key.startswith(prefix),
            RuntimeOutboxEvent.created_at > now - timedelta(seconds=max(1, window)),
        )
        .limit(1)
    ):
        return None
    return f"{prefix}{int(now.timestamp())}"


def _resume_run_event(
    reservation: SandboxReservation,
    *,
    now: datetime,
    dedupe_key: str,
) -> RuntimeOutboxEvent:
    return RuntimeOutboxEvent(
        event_type="runtime.resume_run",
        aggregate_id=reservation.runtime_run_id,
        dedupe_key=dedupe_key,
        payload={
            "run_id": reservation.runtime_run_id,
            "reservation_id": reservation.id,
            "reservation_version": reservation.version,
        },
        available_at=now,
        created_at=now,
    )


_RUN_ENDING_REASON_BY_RESERVATION_STATUS = {
    SandboxReservationStatus.FAILED.value: "sandbox_allocation_failed",
    SandboxReservationStatus.EXPIRED.value: "sandbox_queue_timeout",
    SandboxReservationStatus.CANCELLED.value: "sandbox_reservation_cancelled",
    SandboxReservationStatus.RELEASED.value: "sandbox_lost_before_resume",
}


async def recover_waiting_sandbox_runs(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    redeliver_after_seconds: int = _RESUME_REDELIVERY_SECONDS,
    max_attempts: int = 5,
) -> int:
    """Give every run parked on a sandbox ticket a way forward.

    A run in ``waiting_resource`` wakes only when a ``runtime.resume_run``
    delivery finds its ticket ALLOCATED. The ticket sweeps above own the
    ticket, not the run, so two failures used to strand the run for good, and
    with it every later turn of its conversation (one active run per
    conversation):

    * the resume was lost after the outbox marked it delivered (broker
      restart, a worker discarding the message): the ticket stays ALLOCATED
      and nobody claims it. Its resume is delivered again.
    * the ticket left ALLOCATED before the run claimed it: the runner reclaimed
      or lost the container and its release marked the ticket RELEASED. The
      same ticket is queued again for a fresh container, a bounded number of
      times.

    A ticket that ended without a container (failed, expired, cancelled)
    fails the run with the ticket's reason, so the conversation is free.
    Runs parked on a question or an approval are never touched: parking
    there clears ``reservation_id``.
    """

    current_time = now or datetime.now(timezone.utc)
    rows = list(
        (
            await db.execute(
                select(RuntimeRun, SandboxReservation)
                .join(SandboxReservation, SandboxReservation.id == RuntimeRun.reservation_id)
                .where(
                    RuntimeRun.status == RuntimeRunStatus.WAITING_RESOURCE.value,
                    RuntimeRun.human_input_request_id.is_(None),
                    SandboxReservation.runtime_run_id == RuntimeRun.id,
                    SandboxReservation.status.in_([
                        SandboxReservationStatus.ALLOCATED.value,
                        SandboxReservationStatus.RELEASED.value,
                        *_RUN_ENDING_RESERVATION_STATUSES,
                    ]),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    moved = 0
    for run, reservation in rows:
        if run.lease_expires_at is not None and run.lease_expires_at > current_time:
            continue  # a worker holds the run right now
        if reservation.status == SandboxReservationStatus.ALLOCATED.value:
            allocated_at = reservation.allocated_at or reservation.updated_at
            if allocated_at is not None and allocated_at > current_time - timedelta(
                seconds=redeliver_after_seconds
            ):
                continue
            # One redelivery per window: a slow claim is not flooded, while a
            # delivery that was lost is retried.
            dedupe_key = await _redelivery_due(
                db,
                prefix=_resume_redelivery_prefix(reservation),
                now=current_time,
                window=redeliver_after_seconds,
            )
            if dedupe_key is None:
                continue
            db.add(_resume_run_event(reservation, now=current_time, dedupe_key=dedupe_key))
            moved += 1
            continue
        if (
            reservation.status == SandboxReservationStatus.RELEASED.value
            and reservation.attempt_count < max_attempts
        ):
            instances = list((await db.execute(
                select(SandboxInstance).where(SandboxInstance.reservation_id == reservation.id)
            )).scalars().all())
            if any(instance.status != "released" for instance in instances):
                continue  # a live container still names this ticket; not ours to judge
            # One instance row per ticket: the lost container's released row is
            # history that holds no capacity, and the fresh allocation needs
            # the ticket's row.
            for instance in instances:
                await db.delete(instance)
            reservation.status = SandboxReservationStatus.REQUEUED.value
            reservation.sandbox_id = None
            reservation.runner_id = None
            reservation.allocated_at = None
            reservation.completed_at = None
            reservation.next_attempt_at = current_time
            reservation.deadline_at = current_time + timedelta(
                seconds=pool_wait_seconds((reservation.request_payload or {}).get("pool"))
            )
            reservation.last_error = "sandbox_lost_before_resume"
            payload = dict(reservation.request_payload or {})
            payload[SANDBOX_GENERATION_KEY] = int(payload.get(SANDBOX_GENERATION_KEY) or 0) + 1
            reservation.request_payload = payload
            reservation.version += 1
            run.status_reason = "sandbox_capacity"
            run.version += 1
            # Single flight: another admission may have given the user a new
            # container (or be creating it) meanwhile; follow that instead of
            # creating a second one.
            await _settle_scope_ticket(db, reservation, now=current_time)
            moved += 1
            continue
        # A stable reason code, never the ticket's raw last_error (which can
        # be an exception class name): the terminal transition shows the
        # user the text for that code.
        reason = _RUN_ENDING_REASON_BY_RESERVATION_STATUS.get(
            reservation.status, "sandbox_allocation_failed"
        )
        await finish_runtime_run_out_of_loop(
            db,
            run,
            status=RuntimeRunStatus.FAILED,
            reason=reason,
            now=current_time,
        )
        moved += 1
    return moved


async def complete_sandbox_allocation(
    db: AsyncSession,
    *,
    reservation_id: str,
    sandbox_id: str,
    runner_id: str,
    expected_reservation_version: int | None = None,
    now: datetime | None = None,
) -> SandboxReservation:
    """Commit allocation, route, and resume outbox exactly once."""

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise SandboxAllocationStateError("reservation_not_found")
    if reservation.status == SandboxReservationStatus.ALLOCATED.value:
        if reservation.sandbox_id == sandbox_id and reservation.runner_id == runner_id:
            return reservation
        raise SandboxAllocationStateError("reservation_already_allocated")
    if (
        expected_reservation_version is not None
        and reservation.version != expected_reservation_version
    ):
        raise SandboxAllocationStateError("reservation_version_mismatch")
    if (
        reservation.status != SandboxReservationStatus.ALLOCATING.value
        or reservation.runner_id != runner_id
    ):
        raise SandboxAllocationStateError("reservation_not_allocating")

    reservation.status = SandboxReservationStatus.ALLOCATED.value
    reservation.sandbox_id = sandbox_id
    reservation.allocated_at = current_time
    reservation.next_attempt_at = None
    reservation.version += 1
    db.add(
        SandboxInstance(
            sandbox_id=sandbox_id,
            reservation_id=reservation.id,
            runtime_run_id=reservation.runtime_run_id,
            root_run_id=reservation.root_run_id,
            runner_id=runner_id,
            status="active",
        )
    )
    # A task requester has no run to resume: its worker polls the reservation,
    # so allocation commits the route and stops there.
    if reservation.runtime_run_id:
        run = await db.get(RuntimeRun, reservation.runtime_run_id)
        if run is not None and run.reservation_id != reservation.id:
            # The run moved on to another wait (or a later ticket) since it
            # took this one: a stale allocation must neither overwrite its
            # status nor wake it. The route stays committed above.
            return reservation
        if _run_parked_on(run, reservation):
            run.status = RuntimeRunStatus.WAITING_RESOURCE.value
            run.status_reason = "sandbox_allocated"
            run.version += 1
        db.add(_resume_run_event(
            reservation,
            now=current_time,
            dedupe_key=f"runtime.resume_run:{reservation.id}:{reservation.version}",
        ))
    await db.flush()
    await adopt_follower_reservations(db, now=current_time, leader_id=reservation.id)
    return reservation


async def adopt_follower_reservations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    leader_id: str | None = None,
) -> int:
    """Settle follower tickets whose leader is no longer pending.

    * leader allocated or consumed, its container still active: the follower
      is allocated to that same container (no runner slot, no instance row)
      and its run resumed -- ``attach_durable_reservation`` binds it there;
    * leader ended without a container, or its container is gone: under the
      scope lock the follower follows the scope's leader now, joins the
      scope's container, or -- nothing in flight -- becomes the one ticket
      that creates the scope's next container (``_settle_scope_ticket``).
    """

    current_time = now or datetime.now(timezone.utc)
    filters = [
        SandboxReservation.status.in_([
            SandboxReservationStatus.QUEUED.value,
            SandboxReservationStatus.REQUEUED.value,
        ]),
        SandboxReservation.request_payload[SANDBOX_FOLLOWS_PAYLOAD_KEY].astext.is_not(None),
    ]
    if leader_id:
        filters.append(SandboxReservation.request_payload[SANDBOX_FOLLOWS_PAYLOAD_KEY].astext == leader_id)
    followers = list(
        (
            await db.execute(
                select(SandboxReservation).where(*filters).with_for_update(skip_locked=True)
            )
        ).scalars().all()
    )
    moved = 0
    for follower in followers:
        payload = dict(follower.request_payload or {})
        leader = await db.get(SandboxReservation, str(payload.get(SANDBOX_FOLLOWS_PAYLOAD_KEY) or ""))
        if leader is not None and leader.status in _PENDING_RESERVATION_STATUSES:
            continue
        live_container = None
        # The leader may itself have joined another ticket's container.
        leader_container = reservation_sandbox_id(leader) if leader is not None else None
        if leader is not None and leader.status in {
            SandboxReservationStatus.ALLOCATED.value,
            SandboxReservationStatus.CONSUMED.value,
        } and leader_container:
            live_container = await db.scalar(
                select(SandboxInstance.sandbox_id).where(
                    SandboxInstance.sandbox_id == leader_container,
                    SandboxInstance.status == "active",
                )
            )
        if live_container:
            await _allocate_follower(db, follower, payload, live_container, now=current_time)
        else:
            # The leader ended without a usable container. Turning every one
            # of its followers into an ordinary ticket would let each of them
            # create a container; under the scope lock the first leads and
            # the others follow it (or join a container the scope has now).
            await _settle_scope_ticket(db, follower, now=current_time)
            if follower.status != SandboxReservationStatus.ALLOCATED.value:
                follower.version += 1
        moved += 1
    return moved


# ── Exec-slot waits ─────────────────────────────────────────────
#
# A durable run whose command found every execution slot of its (shared)
# sandbox taken parks on an EXEC_WAITING ticket instead of seeing "busy": the
# command itself waits in the runner's fair FIFO. A sweep
# (``runtime_tasks._wake_exec_slot_waiters``) asks the runner which commands
# are still queued and wakes the run (EXEC_READY + ``runtime.resume_run``)
# once its command left the queue -- started, finished, cancelled, or lost
# with its container -- or the ticket's deadline passed; the re-executed tool
# call then collects the command by its execution id. No scheduler sweep
# ever claims these tickets: they hold no runner slot.

SANDBOX_EXEC_WAIT_REASON = "sandbox_exec_slot"


async def enqueue_exec_slot_wait(
    db: AsyncSession,
    *,
    run: RuntimeRun,
    tool_call_id: str,
    sandbox_id: str,
    execution_id: str,
    wait_seconds: int,
    now: datetime | None = None,
    continuation: dict[str, Any] | None = None,
) -> SandboxReservation:
    """The run's ticket for one command waiting for a slot (idempotent per call).

    ``continuation``: what the parked tool call needs to carry on with the
    same command once it resumes (kept on the ticket's payload, never what
    decides when it wakes).
    """

    current_time = now or datetime.now(timezone.utc)
    key = f"{tool_call_id}:exec-slot"[:255]
    existing = (
        await db.execute(
            select(SandboxReservation).where(
                SandboxReservation.runtime_run_id == run.id,
                SandboxReservation.tool_call_id == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != SandboxReservationStatus.EXEC_WAITING.value:
            existing.status = SandboxReservationStatus.EXEC_WAITING.value
            existing.deadline_at = current_time + timedelta(seconds=max(1, wait_seconds))
            existing.version += 1
        payload = dict(existing.request_payload or {})
        payload.update({**(continuation or {}), "sandbox_id": sandbox_id, "execution_id": execution_id})
        existing.request_payload = payload
        run.reservation_id = existing.id
        return existing
    reservation = SandboxReservation(
        runtime_run_id=run.id,
        root_run_id=run.root_run_id,
        requester_kind=SandboxRequesterKind.RUN.value,
        requester_id=run.id,
        tool_call_id=key,
        entity_id=run.entity_id,
        user_id=run.user_id,
        status=SandboxReservationStatus.EXEC_WAITING.value,
        enqueued_at=current_time,
        deadline_at=current_time + timedelta(seconds=max(1, wait_seconds)),
        request_payload={
            **(continuation or {}),
            "wait": SANDBOX_EXEC_WAIT_KIND,
            "sandbox_id": sandbox_id,
            "execution_id": execution_id,
        },
    )
    db.add(reservation)
    await db.flush()
    run.reservation_id = reservation.id
    return reservation


async def find_exec_slot_wait(
    db: AsyncSession,
    *,
    run_id: str,
    tool_call_id: str,
) -> SandboxReservation | None:
    """This tool call's exec-slot ticket, if it parked on one."""

    return (
        await db.execute(
            select(SandboxReservation).where(
                SandboxReservation.runtime_run_id == run_id,
                SandboxReservation.tool_call_id == f"{tool_call_id}:exec-slot"[:255],
            )
        )
    ).scalar_one_or_none()


async def list_exec_slot_waits(db: AsyncSession, *, limit: int = 200) -> list[SandboxReservation]:
    """Parked exec-slot tickets, oldest first (for the wake sweep)."""

    return list(
        (
            await db.execute(
                select(SandboxReservation)
                .where(SandboxReservation.status == SandboxReservationStatus.EXEC_WAITING.value)
                .order_by(SandboxReservation.enqueued_at.asc(), SandboxReservation.id.asc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def wake_exec_slot_wait(
    db: AsyncSession,
    *,
    reservation_id: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Wake the run parked on an exec-slot ticket (EXEC_READY + resume). Idempotent.

    A ticket whose run already ended is cancelled instead: nothing resumes.
    """

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation).where(SandboxReservation.id == reservation_id).with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None or reservation.status != SandboxReservationStatus.EXEC_WAITING.value:
        return False
    run = await db.get(RuntimeRun, reservation.runtime_run_id) if reservation.runtime_run_id else None
    if run is None or run.status in RuntimeRunStatus.terminal() or run.reservation_id != reservation.id:
        reservation.status = SandboxReservationStatus.CANCELLED.value
        reservation.completed_at = current_time
        reservation.last_error = "run_no_longer_waiting"
        reservation.version += 1
        return False
    if run.lease_expires_at is not None and run.lease_expires_at > current_time:
        # Still being executed (about to park on this ticket): the next sweep
        # wakes it once parked, so the resume is never consumed by a run that
        # cannot be claimed yet.
        return False
    reservation.status = SandboxReservationStatus.EXEC_READY.value
    reservation.allocated_at = current_time
    reservation.last_error = str(reason)[:200]
    reservation.version += 1
    db.add(_resume_run_event(
        reservation,
        now=current_time,
        dedupe_key=f"runtime.resume_run:{reservation.id}:{reservation.version}",
    ))
    return True


async def redeliver_exec_slot_resumes(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    redeliver_after_seconds: int = _RESUME_REDELIVERY_SECONDS,
) -> int:
    """Re-send the resume of a woken run nobody claimed (a lost delivery)."""

    current_time = now or datetime.now(timezone.utc)
    rows = list(
        (
            await db.execute(
                select(RuntimeRun, SandboxReservation)
                .join(SandboxReservation, SandboxReservation.id == RuntimeRun.reservation_id)
                .where(
                    RuntimeRun.status == RuntimeRunStatus.WAITING_RESOURCE.value,
                    SandboxReservation.status == SandboxReservationStatus.EXEC_READY.value,
                    SandboxReservation.allocated_at <= current_time - timedelta(seconds=redeliver_after_seconds),
                )
            )
        ).all()
    )
    sent = 0
    for run, reservation in rows:
        if run.lease_expires_at is not None and run.lease_expires_at > current_time:
            continue
        dedupe_key = await _redelivery_due(
            db,
            prefix=_resume_redelivery_prefix(reservation),
            now=current_time,
            window=redeliver_after_seconds,
        )
        if dedupe_key is None:
            continue
        db.add(_resume_run_event(reservation, now=current_time, dedupe_key=dedupe_key))
        sent += 1
    return sent


async def fail_sandbox_allocation(
    db: AsyncSession,
    *,
    reservation_id: str,
    error: str,
    expected_reservation_version: int | None = None,
    now: datetime | None = None,
    retry_delay_seconds: int = 2,
    max_attempts: int = 5,
    outcome: str = "retry",
    runner_report: dict[str, Any] | None = None,
) -> SandboxReservation:
    """Return a reserved runner slot and either requeue or fail the ticket.

    ``outcome`` (see ``classify_allocation_error``):

    * ``retry``: requeue after ``retry_delay_seconds`` until ``max_attempts``.
    * ``capacity``: the runner was full. Requeue without spending an attempt,
      polling every ``SANDBOX_QUEUE_POLL_SECONDS``, until the ticket's own
      deadline; then expire it as ``sandbox_capacity_timeout``.
      ``runner_report`` (the rejection's memory numbers) replaces the runner's
      memory report so the scheduler stops placing on it until it has room.
    * ``unavailable``: the runner was restarting or unreachable. Waits like
      ``capacity`` (the runner's health probe takes it out of placement until
      it answers again); past the deadline it expires as
      ``sandbox_runner_unavailable``.
    * ``fatal``: the runner rejected the request itself; fail at once.
    """
    if outcome not in {"retry", "capacity", "unavailable", "fatal"}:
        raise ValueError(f"unknown allocation outcome: {outcome}")

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one()
    if (
        reservation.status == SandboxReservationStatus.ALLOCATING.value
        and expected_reservation_version is not None
        and reservation.version != expected_reservation_version
    ):
        return reservation
    if reservation.status != SandboxReservationStatus.ALLOCATING.value:
        if (
            reservation.runner_id
            and reservation.status
            in {
                SandboxReservationStatus.CANCELLED.value,
                SandboxReservationStatus.RELEASE_PENDING.value,
            }
        ):
            runner = (
                await db.execute(
                    select(SandboxRunner)
                    .where(SandboxRunner.id == reservation.runner_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if runner is not None:
                runner.active_count = max(0, runner.active_count - 1)
            reservation.runner_id = None
            reservation.last_error = str(error)[:2000]
            reservation.version += 1
        return reservation
    expired = reservation.deadline_at <= current_time
    waits = outcome in {"capacity", "unavailable"}
    if waits:
        will_fail = expired
        final_status = SandboxReservationStatus.EXPIRED.value
        run_reason = "sandbox_capacity_timeout" if outcome == "capacity" else RUNNER_UNAVAILABLE_ERROR
    elif outcome == "fatal":
        will_fail = True
        final_status = SandboxReservationStatus.FAILED.value
        run_reason = "sandbox_allocation_rejected"
    else:
        will_fail = expired or reservation.attempt_count >= max_attempts
        final_status = SandboxReservationStatus.FAILED.value
        run_reason = "sandbox_allocation_failed"
    run = None
    if will_fail and reservation.runtime_run_id:
        run = (
            await db.execute(
                select(RuntimeRun)
                .where(RuntimeRun.id == reservation.runtime_run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if reservation.runner_id:
        runner = (
            await db.execute(
                select(SandboxRunner)
                .where(SandboxRunner.id == reservation.runner_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if runner is not None:
            runner.active_count = max(0, runner.active_count - 1)
            if outcome == "capacity" and runner_report is not None:
                # The rejection is the freshest word on this runner's memory;
                # fields it does not carry (the default size) are kept.
                record_runner_memory(runner, {
                    **(_runner_memory(runner) or {}),
                    **{key: value for key, value in runner_report.items() if value is not None},
                })
    if outcome == "capacity" and will_fail:
        last_error = f"sandbox_capacity_timeout: last runner answer: {error}"
    elif outcome == "unavailable":
        # Labelled while it waits too, so the expiry sweep and the requester
        # see what the ticket was waiting for.
        last_error = f"{RUNNER_UNAVAILABLE_ERROR}: last runner answer: {error}"
    else:
        last_error = str(error)
    reservation.last_error = last_error[:2000]
    reservation.version += 1
    reservation.runner_id = None
    if will_fail:
        reservation.status = final_status
        reservation.completed_at = current_time
        reservation.next_attempt_at = None
        if _run_parked_on(run, reservation):
            await finish_runtime_run_out_of_loop(
                db,
                run,
                status=RuntimeRunStatus.FAILED,
                reason=run_reason,
                now=current_time,
            )
    else:
        reservation.status = SandboxReservationStatus.REQUEUED.value
        if waits:
            # Waiting for room (or for the runner to come back) is not a
            # failed attempt: give back the attempt this claim took, so only
            # real failures count toward max_attempts.
            reservation.attempt_count = max(0, reservation.attempt_count - 1)
            delay = max(1, int(get_settings().SANDBOX_QUEUE_POLL_SECONDS))
        else:
            delay = retry_delay_seconds
        reservation.next_attempt_at = current_time + timedelta(seconds=delay)
    return reservation


async def load_sandbox_reservation(
    db: AsyncSession,
    reservation_id: str,
) -> SandboxReservation | None:
    """Read one ticket without locking, for a requester polling its own slot."""

    return (
        await db.execute(
            select(SandboxReservation).where(SandboxReservation.id == reservation_id)
        )
    ).scalar_one_or_none()


async def consume_sandbox_reservation(
    db: AsyncSession,
    *,
    reservation_id: str,
    now: datetime | None = None,
) -> SandboxReservation:
    """Latch an allocated ticket as consumed once its Sandbox is bound.

    Idempotent: a ticket already CONSUMED is returned unchanged, so a retried
    attach does not bump the version twice. Any other status is a caller bug
    (the Sandbox was never allocated to this ticket) and raises.
    """

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise SandboxAllocationStateError("reservation_not_found")
    if reservation.status == SandboxReservationStatus.CONSUMED.value:
        return reservation
    if reservation.status != SandboxReservationStatus.ALLOCATED.value:
        raise SandboxAllocationStateError("reservation_not_allocated")
    reservation.status = SandboxReservationStatus.CONSUMED.value
    reservation.consumed_at = current_time
    reservation.version += 1
    return reservation


async def cancel_queued_sandbox_reservation(
    db: AsyncSession,
    *,
    reservation_id: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Cancel a ticket that has not been claimed yet; return whether it moved."""

    current_time = now or datetime.now(timezone.utc)
    reservation = (
        await db.execute(
            select(SandboxReservation)
            .where(SandboxReservation.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        return False
    if reservation.status not in {
        SandboxReservationStatus.QUEUED.value,
        SandboxReservationStatus.REQUEUED.value,
        SandboxReservationStatus.PENDING_CHECKPOINT.value,
    }:
        # ALLOCATING and later own a runner slot; only the allocation path may
        # release those, so a poller giving up must not touch them here.
        return False
    reservation.status = SandboxReservationStatus.CANCELLED.value
    reservation.completed_at = current_time
    reservation.last_error = str(reason)[:2000]
    reservation.version += 1
    return True


def _waiting_ticket_filters(current_time: datetime, pool: str | None) -> list[Any]:
    return [
        SandboxReservation.status.in_([
            SandboxReservationStatus.QUEUED.value,
            SandboxReservationStatus.REQUEUED.value,
        ]),
        SandboxReservation.deadline_at > current_time,
        or_(
            SandboxReservation.next_attempt_at.is_(None),
            SandboxReservation.next_attempt_at <= current_time,
        ),
        not_(_is_follower()),
        *_ticket_pool_filters(pool),
    ]


async def count_waiting_sandbox_reservations(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    pool: str | None = None,
) -> int:
    """Tickets the scheduler could allocate now if a runner (of ``pool``) had room."""

    current_time = now or datetime.now(timezone.utc)
    return int(await db.scalar(
        select(func.count(SandboxReservation.id)).where(*_waiting_ticket_filters(current_time, pool))
    ) or 0)


async def next_waiting_ticket_need(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    pool: str | None = None,
) -> tuple[int | None, float | None] | None:
    """Memory (MiB) and CPUs the fairest waiting ticket (of ``pool``) needs.

    None when nothing waits; each value is None for the runner default. The
    ticket the next claim would take: a follower never gets a container of
    its own (it joins its leader's), so it needs no runner capacity.
    """

    current_time = now or datetime.now(timezone.utc)
    row = (
        await db.execute(
            select(SandboxReservation.request_payload)
            .where(*_waiting_ticket_filters(current_time, pool))
            .order_by(
                SandboxReservation.priority.asc(),
                SandboxReservation.enqueued_at.asc(),
                SandboxReservation.id.asc(),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    payload = row[0] if isinstance(row[0], dict) else {}
    return ticket_memory_mb(payload), ticket_cpus(payload)


def runner_memory_deficit_mb(
    runner: SandboxRunner,
    need_mb: int | None,
    *,
    need_cpus: float | None = None,
    ceiling: PoolCeiling | None = None,
) -> int:
    """How much memory (MiB) the runner must free before the ticket fits (0 = fits).

    Also 0 for a runner whose class cannot hold the ticket (it waits for a
    bigger one): freeing memory there would only cost warm containers.
    """

    report = _runner_memory(runner)
    if report is None or not runner_holds_ticket(runner, need_mb, need_cpus, ceiling):
        return 0
    need = memory_need_on_runner(need_mb, report)
    return max(0, need - int(report.get("available_mb") or 0))


async def sandboxes_free_for_handover(
    db: AsyncSession,
    *,
    runner_id: str,
    sandbox_ids: Iterable[str],
) -> set[str]:
    """Containers on ``runner_id`` that no live work of their owner still uses.

    A human user's container serves all of the user's conversations and
    tasks, so it is in use while ANY root run of that user (entity + user) is
    not terminal -- running, or parked waiting for a container or a slot --
    and while any of the user's tickets is still being allocated; once all
    have ended it is only a warm convenience that a queued ticket may take.
    The owner is the user the container's ticket was allocated for. A
    user-less container (system work keyed by its task), one a task worker's
    ticket allocated (its worker runs without a RuntimeRun, so its activity
    is invisible here) or one without an instance row is never reported free
    here: the idle reaper owns it.
    """

    wanted = [str(sandbox_id) for sandbox_id in sandbox_ids]
    if not wanted:
        return set()
    rows = (
        await db.execute(
            select(SandboxInstance.sandbox_id, SandboxReservation.entity_id, SandboxReservation.user_id)
            .join(SandboxReservation, SandboxReservation.id == SandboxInstance.reservation_id)
            .where(
                SandboxInstance.runner_id == runner_id,
                SandboxInstance.status == "active",
                SandboxInstance.sandbox_id.in_(wanted),
                SandboxReservation.user_id != "",
                SandboxReservation.requester_kind == SandboxRequesterKind.RUN.value,
            )
        )
    ).all()
    free: set[str] = set()
    for sandbox_id, entity_id, user_id in rows:
        live_run = await db.scalar(
            select(RuntimeRun.id).where(
                RuntimeRun.entity_id == entity_id,
                RuntimeRun.user_id == user_id,
                RuntimeRun.status.notin_(RuntimeRunStatus.terminal()),
            ).limit(1)
        )
        if live_run is not None:
            continue
        live_ticket = await db.scalar(
            select(SandboxReservation.id).where(
                SandboxReservation.entity_id == entity_id,
                SandboxReservation.user_id == user_id,
                # A run parked on a slot (exec tickets) is a live run, counted
                # above; a stale exec ticket of an ended run must not pin it.
                SandboxReservation.status.in_([
                    *_PENDING_RESERVATION_STATUSES,
                    SandboxReservationStatus.ALLOCATED.value,
                ]),
            ).limit(1)
        )
        if live_ticket is None:
            free.add(sandbox_id)
    return free


async def resolve_sandbox_runner(
    db: AsyncSession,
    sandbox_id: str,
) -> SandboxRunner:
    """Resolve a stateful operation to the runner that owns the Sandbox."""

    row = (
        await db.execute(
            select(SandboxRunner)
            .join(SandboxInstance, SandboxInstance.runner_id == SandboxRunner.id)
            .where(
                SandboxInstance.sandbox_id == sandbox_id,
                SandboxInstance.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SandboxRouteNotFoundError(
            f"no active runner route for sandbox {sandbox_id}: its instance row is missing or released"
        )
    return row


async def release_sandbox_instance(
    db: AsyncSession,
    *,
    sandbox_id: str,
    now: datetime | None = None,
) -> bool:
    """Mark a destroyed Sandbox released and return its runner reservation."""

    current_time = now or datetime.now(timezone.utc)
    instance = (
        await db.execute(
            select(SandboxInstance)
            .where(SandboxInstance.sandbox_id == sandbox_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None or instance.status == "released":
        return False
    instance.status = "released"
    instance.released_at = current_time
    reservation = await db.get(SandboxReservation, instance.reservation_id)
    # A ticket requeued after losing this container now names a newer one
    # (or none yet); releasing the old container must not end it.
    if reservation is not None and reservation.sandbox_id == sandbox_id:
        reservation.status = SandboxReservationStatus.RELEASED.value
        reservation.completed_at = current_time
        reservation.version += 1
    runner = (
        await db.execute(
            select(SandboxRunner)
            .where(SandboxRunner.id == instance.runner_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if runner is not None:
        runner.active_count = max(0, runner.active_count - 1)
    return True


#: An active instance row younger than this may describe a container the
#: runner's listing raced with (created after the listing was taken).
_VANISHED_INSTANCE_GRACE_SECONDS = 120


async def release_vanished_sandbox_instances(
    db: AsyncSession,
    *,
    runner_id: str,
    live_sandbox_ids: Iterable[str],
    now: datetime | None = None,
    grace_seconds: int = _VANISHED_INSTANCE_GRACE_SECONDS,
) -> list[str]:
    """Release instance rows for containers the runner no longer has.

    The runner's idle reaper (and a crash) removes containers without telling
    the coordinator, so an instance row stays ``active`` and its reservation
    can sit in ``release_pending`` forever, pinning a runner slot through
    ``reconciled_runner_active_count``. The runner's listing is the truth:
    every active row older than the grace period whose container is not
    listed is released here. Returns the released sandbox ids.
    """

    current_time = now or datetime.now(timezone.utc)
    live = {str(sandbox_id) for sandbox_id in live_sandbox_ids}
    rows = list(
        (
            await db.execute(
                select(SandboxInstance)
                .where(
                    SandboxInstance.runner_id == runner_id,
                    SandboxInstance.status == "active",
                    SandboxInstance.created_at <= current_time - timedelta(seconds=grace_seconds),
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    released: list[str] = []
    for instance in rows:
        if instance.sandbox_id in live:
            continue
        if await release_sandbox_instance(db, sandbox_id=instance.sandbox_id, now=current_time):
            released.append(instance.sandbox_id)
    return released


async def restore_consumed_reservation(
    db: AsyncSession,
    *,
    sandbox_id: str,
    now: datetime | None = None,
) -> bool:
    """Put a warm-kept container's reservation back to ``consumed``.

    A run cancel marks its reservation ``release_pending`` before it knows
    whether the container is kept warm for reuse. A kept container is not
    released, so the ticket must not stay in a slot-pinning state: a consumed
    ticket lets the runner's observed count drive occupancy again.
    """

    instance = (
        await db.execute(
            select(SandboxInstance)
            .where(SandboxInstance.sandbox_id == sandbox_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None:
        return False
    reservation = await db.get(SandboxReservation, instance.reservation_id)
    if reservation is None or reservation.status != SandboxReservationStatus.RELEASE_PENDING.value:
        return False
    reservation.status = SandboxReservationStatus.CONSUMED.value
    reservation.version += 1
    return True


__all__ = [
    "POOL_MANAGED_CONFIG_KEY",
    "SandboxAllocationStateError",
    "SandboxQueueFullError",
    "SandboxRouteNotFoundError",
    "SandboxScopeContainer",
    "cancel_queued_sandbox_reservation",
    "claim_due_sandbox_runner_health_checks",
    "CAPACITY_ERROR_CODES",
    "RUNNER_MEMORY_KEY",
    "RUNNER_UNAVAILABLE_ERROR",
    "RUNNER_UNAVAILABLE_ERROR_CODES",
    "SANDBOX_MEMORY_KEY",
    "claim_next_sandbox_allocation",
    "claim_sandbox_reservation_now",
    "classify_allocation_error",
    "complete_sandbox_allocation",
    "configured_sandbox_runners",
    "consume_sandbox_reservation",
    "count_waiting_sandbox_reservations",
    "effective_runner_limits",
    "enqueue_sandbox_reservation",
    "expire_sandbox_reservations",
    "adopt_follower_reservations",
    "enqueue_exec_slot_wait",
    "find_exec_slot_wait",
    "find_inflight_scope_reservation",
    "find_scope_container",
    "find_scope_head",
    "lock_sandbox_scope",
    "list_exec_slot_waits",
    "redeliver_exec_slot_resumes",
    "reservation_sandbox_id",
    "wake_exec_slot_wait",
    "fail_sandbox_allocation",
    "is_pool_managed_runner",
    "load_sandbox_reservation",
    "memory_need_on_runner",
    "next_waiting_ticket_need",
    "runner_memory_deficit_mb",
    "parse_memory_mb",
    "PoolCeiling",
    "PoolGrowth",
    "SANDBOX_CPUS_KEY",
    "load_pool_ceiling",
    "pool_ceiling",
    "pool_growth",
    "pool_wait_seconds",
    "record_runner_memory",
    "runner_fits_ticket",
    "runner_holds_ticket",
    "runner_memory_from_health",
    "runner_memory_report",
    "ticket_cpus",
    "ticket_memory_mb",
    "reconciled_runner_active_count",
    "recover_stale_sandbox_allocations",
    "recover_waiting_sandbox_runs",
    "sandbox_create_idempotency_key",
    "release_vanished_sandbox_instances",
    "restore_consumed_reservation",
    "reconcile_sandbox_runners",
    "release_sandbox_instance",
    "resolve_sandbox_runner",
    "sandboxes_free_for_handover",
]
