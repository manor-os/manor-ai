"""Memory-based admission for one runner host.

Every container is created with a hard ``--memory`` limit, and that limit is
what the runner admits against: a container is only created when its limit
fits in the host's memory budget. The count cap (``MAX_SANDBOXES``) stays as
an additional hard limit.

Capacity model (all sizes in bytes internally, MiB on the wire)::

    host_memory          = Docker daemon MemTotal (or SANDBOX_HOST_MEMORY)
    budget               = SANDBOX_MEMORY_BUDGET
                           or host_memory * SANDBOX_MEMORY_OVERCOMMIT - SANDBOX_MEMORY_RESERVE
    max_container_memory = SANDBOX_MAX_CONTAINER_MEMORY
                           or host_memory - SANDBOX_MEMORY_RESERVE   (never above budget)
    available            = budget - sum(memory limit of every registered container)
    largest_allocatable  = min(available, max_container_memory), 0 when no slot is free

A container's limit is clamped to ``max_container_memory``: no container can
be promised more memory than the host physically has (minus the reserve), and
a request sized for a bigger runner still runs here instead of waiting for a
budget it can never get.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIB = 1024 * 1024

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?\s*$", re.IGNORECASE)
_UNIT_EXPONENT = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}


def parse_size(value: object) -> int:
    """Parse a Docker-style size (``512m``, ``2g``, ``1.5GiB``, ``1048576``) into bytes.

    Units are binary, like ``docker run --memory``. Raises ``ValueError`` for
    anything else, including an empty value.
    """

    if isinstance(value, bool):
        raise ValueError(f"invalid size: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"invalid size: {value!r}")
        return int(value)
    match = _SIZE_RE.match(str(value or ""))
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    number, unit = match.groups()
    return int(float(number) * (1024 ** _UNIT_EXPONENT[unit.lower()]))


def to_mib(value: int) -> int:
    return int(value // MIB)


def format_size(value: int) -> str:
    """Canonical Docker size string for ``value`` bytes (``2g``, ``512m``, ``4096b``)."""

    for unit, exponent in (("g", 3), ("m", 2), ("k", 1)):
        step = 1024 ** exponent
        if value > 0 and value % step == 0:
            return f"{value // step}{unit}"
    return f"{max(0, int(value))}b"


@dataclass(frozen=True)
class HostResources:
    memory_bytes: int
    cpus: float
    source: str


def _local_resources() -> HostResources | None:
    memory: int | None = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    memory = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        memory = None
    source = "meminfo"
    if memory is None:
        try:
            memory = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
            source = "sysconf"
        except (AttributeError, OSError, ValueError):
            return None
    return HostResources(memory, float(os.cpu_count() or 1), source)


def detect_host_resources(
    *,
    memory_override: str = "",
    cpus_override: float = 0.0,
    docker: tuple[int, float] | None = None,
) -> HostResources:
    """Resolve the host size: explicit overrides, else the Docker daemon's
    report (``docker_backend.docker_host_resources``, measured off the event
    loop by the caller), else this machine's own probes."""

    if docker is not None:
        detected: HostResources | None = HostResources(int(docker[0]), float(docker[1]), "docker")
    else:
        detected = _local_resources()
    if detected is None:
        # Nothing to measure (exotic platform): behave like the smallest
        # supported runner instead of admitting without a bound.
        detected = HostResources(2 * 1024 * MIB, 1.0, "fallback")
    memory_set = bool(str(memory_override or "").strip())
    memory = parse_size(memory_override) if memory_set else detected.memory_bytes
    cpus = float(cpus_override) if cpus_override and cpus_override > 0 else detected.cpus
    return HostResources(memory, cpus, "config" if memory_set else detected.source)


@dataclass(frozen=True)
class MemoryLimits:
    host_memory: int
    host_cpus: float
    budget: int
    max_container: int
    reserve: int
    overcommit: float
    source: str


def compute_limits(
    host: HostResources,
    *,
    overcommit: float,
    reserve: str,
    budget_override: str = "",
    max_container_override: str = "",
) -> MemoryLimits:
    reserve_bytes = parse_size(reserve) if str(reserve or "").strip() else 0
    if str(budget_override or "").strip():
        budget = parse_size(budget_override)
    else:
        budget = int(host.memory_bytes * max(0.0, float(overcommit))) - reserve_bytes
    budget = max(0, budget)
    if str(max_container_override or "").strip():
        max_container = parse_size(max_container_override)
    else:
        max_container = host.memory_bytes - reserve_bytes
    # Whole MiB, so the clamp is a clean Docker size and the wire numbers
    # (MiB) are exact.
    budget = budget // MIB * MIB
    max_container = max(0, min(max_container, budget)) // MIB * MIB
    return MemoryLimits(
        host_memory=host.memory_bytes,
        host_cpus=host.cpus,
        budget=budget,
        max_container=max_container,
        reserve=reserve_bytes,
        overcommit=float(overcommit),
        source=host.source,
    )


class MemoryReservation:
    """One container's share of the budget; ``release`` is idempotent."""

    def __init__(self, budget: "MemoryBudget", nbytes: int) -> None:
        self._budget = budget
        self.nbytes = nbytes
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self._budget._release(self)


class MemoryBudget:
    """Admission ledger of container memory limits on this host.

    Synchronous on purpose: a check-and-reserve never yields to the event
    loop, so two creates can never both take the last free memory.
    """

    def __init__(self, limits: MemoryLimits) -> None:
        self.limits = limits
        self._reservations: set[MemoryReservation] = set()

    @property
    def reserved(self) -> int:
        return sum(item.nbytes for item in self._reservations)

    @property
    def available(self) -> int:
        return max(0, self.limits.budget - self.reserved)

    @property
    def largest_allocatable(self) -> int:
        return min(self.available, self.limits.max_container)

    def update_limits(self, limits: MemoryLimits) -> None:
        """Adopt a better host measurement; existing reservations are kept."""

        self.limits = limits

    def try_reserve(self, nbytes: int) -> MemoryReservation | None:
        # A zero limit would mean "unlimited" to Docker: never admit one.
        if nbytes <= 0 or nbytes > self.available:
            return None
        reservation = MemoryReservation(self, nbytes)
        self._reservations.add(reservation)
        return reservation

    def _release(self, reservation: MemoryReservation) -> None:
        self._reservations.discard(reservation)

    def status(self) -> dict[str, object]:
        limits = self.limits
        return {
            "host_memory_mb": to_mib(limits.host_memory),
            "host_cpus": limits.host_cpus,
            "host_resources_source": limits.source,
            "memory_overcommit": limits.overcommit,
            "memory_reserve_mb": to_mib(limits.reserve),
            "memory_budget_mb": to_mib(limits.budget),
            "memory_reserved_mb": to_mib(self.reserved),
            "memory_available_mb": to_mib(self.available),
            "max_container_memory_mb": to_mib(limits.max_container),
        }
