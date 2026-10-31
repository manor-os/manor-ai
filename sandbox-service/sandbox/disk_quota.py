"""Disk usage of a user's shared sandbox: ONE policy, in one place.

A user's general sandbox serves all of their conversations and tasks, so a
disk one of them fills must never cost the others their commands or the
container. Every disk the runner bounds goes through ``enforce_disk_quota``
(called by the runner's disk watchdog, ``SkillRunner._enforce_disk_quotas``):

* **Attribution.** A disk with work areas (the shared ``/workspace`` tmpfs:
  ``/workspace/c/<area>``, one per conversation or task) charges each area
  for the bytes under it; the largest area is the offender. Bytes outside
  every area (``/workspace/shared``) and a disk without areas (a runtime
  install dir every command shares) are charged to the commands writing
  there right now (files open there; for a disk without areas also the
  working directory) -- never to an innocent area.
* **Over the limit** the offending commands are stopped: the watchdog writes
  a verdict naming the area / directory and kills the command's processes,
  and the launcher reports ``disk_killed``. Other areas' commands keep
  running.
* **Refusal.** New commands of the offending work area are refused before
  they start (``SandboxDiskQuotaExceeded``: HTTP 409, code
  ``sandbox_disk_quota``, ``started: false``; a command already waiting for
  a slot ends with outcome ``disk_quota``) until the disk is back under its
  resume level -- except cleanup commands (``rm``, ``du``, ``ls``, ``pip
  uninstall`` ...; see ``is_cleanup_command``), so the area can always free
  its own space. Nothing is ever deleted by the runner.
* **Recycle** only when the container itself is unusable -- a disk marked
  ``recycle_when_idle`` (one with no per-area attribution, like the install
  dir: its bytes cannot be handed back to anyone) is still over its limit --
  AND nothing runs, waits or loads in it. ``/workspace`` is never a reason to
  recycle: its bytes belong to the areas, which can delete them.

The disk-backed install dir (``/opt/sandbox-local``, a volume every
container gets) is one of ``DockerSandbox.disk_roots``: a directory measured
with ``du`` (``filesystem=False``) every 60 s, with no areas (its writers are
charged) and ``recycle_when_idle``. The disk watchdog measures it (one
``docker exec`` that also lists the writers), stops the commands writing into
it, refuses their work areas, and recycles the container only once it is idle
and still over -- the runner never reclaims a sandbox for it otherwise. A
caller that already has a measurement can pass it in: ``await
enforce_disk_quota(sandbox, [DiskUsage(root, used_bytes, writers=...)])``.

This module imports nothing from the Docker backend: the sandbox is used
through the small ``DiskQuotaTarget`` surface (``DockerSandbox`` implements it).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import shlex
import time
from typing import Any, Iterable, Mapping, Protocol

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = "/workspace"
WORK_AREAS_DIR = f"{WORKSPACE_ROOT}/c"
DISK_QUOTA_CODE = "sandbox_disk_quota"
DISK_QUOTA_OUTCOME = "disk_quota"
# /workspace: stop the largest area at this share of the tmpfs, keep refusing
# its new commands until the tmpfs is back under the resume share of it.
WORKSPACE_LIMIT_FRACTION = 0.92
RESUME_FRACTION = 0.85
# A disk with nothing pending is measured this often (a refusal or an
# over-limit disk is measured at its own interval until it clears).
IDLE_INTERVAL_SECONDS = 60.0


def work_area_key(fair_key: str | None) -> str | None:
    """The directory name (under /workspace/c) of a command's work area.

    The control plane names a command's work area by its work key
    (``fair_key``) and places it in ``/workspace/c/<area>``
    (``packages.core.ai.runtime.sandbox.runtime_sandbox_area_key``; the two
    are held together by ``tests/test_sandbox_per_user_layout.py``).
    """

    raw = str(fair_key or "").strip()
    if not raw:
        return None
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)[:64]
    return safe or "default"


def execution_digest(execution_id: str) -> str:
    """The id the in-container watchdog knows a command by (and names its verdict file)."""

    return hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]


@dataclasses.dataclass(frozen=True)
class DiskRoot:
    """One disk (mount or directory) of the container that the runner bounds."""

    path: str
    limit_bytes: int
    # Per-work-area directories under it; None = shared by every area.
    areas_dir: str | None = None
    # True: a mount of its own, usage = the filesystem's (statvfs).
    # False: a directory, usage = bytes under it.
    filesystem: bool = False
    recycle_when_idle: bool = False
    interval_seconds: float = 3.0
    resume_fraction: float = RESUME_FRACTION

    @property
    def resume_bytes(self) -> int:
        return int(self.limit_bytes * self.resume_fraction)


def workspace_root(size_bytes: int, *, interval_seconds: float = 3.0) -> DiskRoot:
    """The shared /workspace tmpfs of ``size_bytes``: per-area, never a reason to recycle."""

    return DiskRoot(
        path=WORKSPACE_ROOT,
        limit_bytes=int(size_bytes * WORKSPACE_LIMIT_FRACTION),
        areas_dir=WORK_AREAS_DIR,
        filesystem=True,
        interval_seconds=interval_seconds,
    )


@dataclasses.dataclass(frozen=True)
class DiskUsage:
    """One measurement of a ``DiskRoot``."""

    root: DiskRoot
    used_bytes: int
    # Bytes per work area (directory name under ``root.areas_dir``); measured
    # only when the disk is at or over its resume level.
    area_bytes: Mapping[str, int] = dataclasses.field(default_factory=dict)
    # Commands (execution digests) with files open, or working, under the root.
    writers: frozenset[str] = frozenset()
    # Command (execution digest) -> the work areas its processes work in.
    cwd_areas: Mapping[str, frozenset[str]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class DiskRefusal:
    """Why new commands of one work area are refused."""

    area: str
    root: str
    message: str


@dataclasses.dataclass(frozen=True)
class DiskKill:
    """One command to stop, with the verdict its launcher reports."""

    execution_id: str
    digest: str
    verdict: dict[str, Any]


@dataclasses.dataclass
class DiskQuotaPlan:
    kills: list[DiskKill] = dataclasses.field(default_factory=list)
    # The complete set of refusals after this measurement, by work area.
    refusals: dict[str, DiskRefusal] = dataclasses.field(default_factory=dict)
    recycle_reason: str | None = None


def _mb(value: int) -> int:
    return int(value) // (1024 * 1024)


def _area_refusal(area: str, usage: DiskUsage) -> DiskRefusal:
    root = usage.root
    held = int(usage.area_bytes.get(area, 0))
    return DiskRefusal(
        area=area,
        root=root.path,
        message=(
            f"Not started: work area {root.areas_dir}/{area} holds {_mb(held)} MB of this user's "
            f"shared {root.path} ({_mb(usage.used_bytes)} MB used, limit {_mb(root.limit_bytes)} MB). "
            "Its new commands are refused until space is freed; other work in the sandbox is not "
            "affected. Cleanup commands still run: delete what you do not need (rm -rf ...), or "
            "save results with save_result, then retry."
        ),
    )


def _writer_refusal(area: str, usage: DiskUsage) -> DiskRefusal:
    root = usage.root
    where = root.path if root.areas_dir is None else f"{root.path} outside the work areas (e.g. {root.path}/shared)"
    return DiskRefusal(
        area=area,
        root=root.path,
        message=(
            f"Not started: this work area's commands were writing to {where}; {root.path} holds "
            f"{_mb(usage.used_bytes)} MB, over its {_mb(root.limit_bytes)} MB limit (shared by all "
            "of this user's work). Its new commands are refused until space is freed there; other "
            f"work is not affected. Cleanup commands still run (rm -rf {root.path}/..., pip "
            "uninstall, npm uninstall, npm cache clean)."
        ),
    )


def plan_disk_quota(
    usages: Iterable[DiskUsage],
    *,
    executions: Mapping[str, str | None],
    refusals: Mapping[str, DiskRefusal],
    idle: bool,
) -> DiskQuotaPlan:
    """THE disk policy (pure): what to stop, refuse and recycle after one measurement.

    ``executions``: running commands, execution id -> work area (None when
    the caller named none). ``refusals``: the refusals in force. ``idle``:
    nothing runs, waits or loads in the sandbox. Refusals of disks not
    measured this time are kept as they are.
    """

    usages = list(usages)
    measured = {usage.root.path for usage in usages}
    plan = DiskQuotaPlan(
        refusals={area: refusal for area, refusal in refusals.items() if refusal.root not in measured},
    )
    kills: dict[str, DiskKill] = {}
    for usage in usages:
        root = usage.root
        if root.limit_bytes <= 0:
            continue
        over = usage.used_bytes >= root.limit_bytes
        if usage.used_bytes < root.resume_bytes:
            continue  # back under the resume level: its refusals are lifted
        largest = max(usage.area_bytes.values(), default=0)
        # Bytes outside every work area (/workspace/shared, say) are nobody's
        # area: when they hold the most, the largest area is not the
        # offender -- the commands writing there are.
        outside = usage.used_bytes - sum(usage.area_bytes.values())
        if root.areas_dir is not None and largest > 0 and largest >= outside:
            offender = max(usage.area_bytes, key=lambda area: (usage.area_bytes[area], area))
            prior = refusals.get(offender)
            if over or (prior is not None and prior.root == root.path):
                plan.refusals[offender] = _area_refusal(offender, usage)
            if not over:
                continue
            for execution_id, area in executions.items():
                digest = execution_digest(execution_id)
                if area == offender or offender in usage.cwd_areas.get(digest, frozenset()):
                    kills[execution_id] = DiskKill(execution_id, digest, {
                        "reason": "disk",
                        "root": root.path,
                        "area": f"{root.areas_dir}/{offender}",
                        "bytes": int(usage.area_bytes.get(offender, 0)),
                        "used": int(usage.used_bytes),
                        "limit": int(root.limit_bytes),
                    })
        else:
            for area, refusal in refusals.items():
                if refusal.root == root.path:
                    plan.refusals[area] = refusal
            if over:
                for execution_id, area in executions.items():
                    digest = execution_digest(execution_id)
                    if digest not in usage.writers:
                        continue
                    kills[execution_id] = DiskKill(execution_id, digest, {
                        "reason": "disk",
                        "root": root.path,
                        "area": None,
                        "bytes": int(outside if root.areas_dir is not None else usage.used_bytes),
                        "used": int(usage.used_bytes),
                        "limit": int(root.limit_bytes),
                    })
                    if area:
                        plan.refusals[area] = _writer_refusal(area, usage)
        if over and root.recycle_when_idle and idle and plan.recycle_reason is None:
            plan.recycle_reason = (
                f"{root.path} holds {_mb(usage.used_bytes)} MB, over its {_mb(root.limit_bytes)} MB "
                "limit, with nothing running in the sandbox"
            )
    plan.kills = list(kills.values())
    return plan


# ── cleanup commands ─────────────────────────────────────────────

_CLEANUP_PROGRAMS = frozenset({"rm", "rmdir", "unlink", "du", "df", "ls", "pwd", "cd", "true", "find"})
_CLEANUP_SUBCOMMANDS = frozenset({
    ("pip", "uninstall"), ("pip", "cache"), ("pip3", "uninstall"), ("pip3", "cache"),
    ("npm", "uninstall"), ("npm", "rm"), ("npm", "prune"), ("npm", "cache"),
})
_FIND_WRITERS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"})
_COMMAND_SEPARATORS = re.compile(r"&&|\|\||;|\n")


def is_cleanup_command(command: str) -> bool:
    """A command that can only free (or inspect) space: allowed while its area is refused.

    Every part of the command line must be one of a few programs that never
    write file contents (``rm``, ``du``, ``ls``, ``find`` without
    ``-exec``/``-fprint``, ``pip``/``npm`` uninstall or cache cleanup), with
    no redirection, pipe, background job or command substitution.
    """

    text = str(command or "")
    if any(token in text for token in (">", "<", "`", "$(")):
        return False
    parts = [part for part in _COMMAND_SEPARATORS.split(text) if part.strip()]
    if not parts:
        return False
    for part in parts:
        if "|" in part or "&" in part:
            return False
        try:
            words = shlex.split(part)
        except ValueError:
            return False
        if not words:
            continue
        program = words[0].rsplit("/", 1)[-1]
        if program == "find":
            if any(word in _FIND_WRITERS for word in words[1:]):
                return False
            continue
        if program in _CLEANUP_PROGRAMS:
            continue
        if len(words) > 1 and (program, words[1]) in _CLEANUP_SUBCOMMANDS:
            continue
        if program in {"python", "python3"} and words[1:3] == ["-m", "pip"] and len(words) > 3 and (
            "pip", words[3]
        ) in _CLEANUP_SUBCOMMANDS:
            continue
        return False
    return True


# ── enforcement ──────────────────────────────────────────────────


class DiskQuotaTarget(Protocol):
    """What ``enforce_disk_quota`` needs from a sandbox (``DockerSandbox``)."""

    sandbox_id: str

    @property
    def has_pending_work(self) -> bool: ...

    @property
    def disk_refusals(self) -> Mapping[str, DiskRefusal]: ...

    def disk_roots(self) -> list[DiskRoot]: ...

    def execution_areas(self) -> dict[str, str | None]: ...

    async def measure_disk(self, roots: list[DiskRoot]) -> list[DiskUsage]: ...

    async def stop_for_disk(self, kills: list[DiskKill]) -> None: ...

    def set_disk_refusals(self, refusals: Mapping[str, DiskRefusal]) -> None: ...

    def mark_disk_unusable(self, reason: str) -> None: ...


def due_disk_roots(sandbox: Any, now: float | None = None) -> list[DiskRoot]:
    """The roots of ``sandbox`` to measure now (its own interval while it matters)."""

    current = time.monotonic() if now is None else now
    measured_at: dict[str, float] = getattr(sandbox, "_disk_measured_at", None) or {}
    watching = bool(getattr(sandbox, "has_pending_work", False)) or bool(getattr(sandbox, "disk_refusals", None))
    over: set[str] = getattr(sandbox, "_disk_over", None) or set()
    due = []
    for root in sandbox.disk_roots():
        interval = root.interval_seconds if (watching or root.path in over) else max(
            root.interval_seconds, IDLE_INTERVAL_SECONDS,
        )
        if current - measured_at.get(root.path, float("-inf")) >= interval:
            due.append(root)
    return due


async def enforce_disk_quota(
    sandbox: DiskQuotaTarget,
    usages: Iterable[DiskUsage] | None = None,
    *,
    roots: list[DiskRoot] | None = None,
) -> DiskQuotaPlan:
    """Measure (unless given ``usages``), decide with ``plan_disk_quota``, act.

    Stops the offending commands, installs the refusals (failing the queued
    commands of a newly refused area as never started), and -- only when the
    container is unusable and idle -- marks it for recycling (the caller,
    the runner, reclaims it: ``plan.recycle_reason`` is set). Never raises
    for a measurement the container could not answer; the next tick retries.
    """

    if usages is None:
        wanted = roots if roots is not None else sandbox.disk_roots()
        if not wanted:
            return DiskQuotaPlan(refusals=dict(sandbox.disk_refusals))
        measured_at = getattr(sandbox, "_disk_measured_at", None)
        if isinstance(measured_at, dict):
            now = time.monotonic()
            for root in wanted:
                measured_at[root.path] = now
        usages = await sandbox.measure_disk(wanted)
    usages = list(usages)
    plan = plan_disk_quota(
        usages,
        executions=sandbox.execution_areas(),
        refusals=sandbox.disk_refusals,
        idle=not sandbox.has_pending_work,
    )
    over = getattr(sandbox, "_disk_over", None)
    if isinstance(over, set):
        for usage in usages:
            if usage.root.limit_bytes > 0 and usage.used_bytes >= usage.root.resume_bytes:
                over.add(usage.root.path)
            else:
                over.discard(usage.root.path)
    # Refuse first, so no new command of the offending area starts while its
    # running ones are being stopped.
    sandbox.set_disk_refusals(plan.refusals)
    if plan.kills:
        logger.warning(
            "Sandbox %s disk quota: stopping %s",
            sandbox.sandbox_id,
            ", ".join(f"{kill.execution_id} ({kill.verdict.get('area') or kill.verdict.get('root')})"
                      for kill in plan.kills),
        )
        await sandbox.stop_for_disk(plan.kills)
    if plan.recycle_reason:
        logger.warning("Sandbox %s is unusable: %s; recycling it", sandbox.sandbox_id, plan.recycle_reason)
        sandbox.mark_disk_unusable(f"disk quota: {plan.recycle_reason}")
    return plan


__all__ = [
    "DISK_QUOTA_CODE",
    "DISK_QUOTA_OUTCOME",
    "DiskKill",
    "DiskQuotaPlan",
    "DiskQuotaTarget",
    "DiskRefusal",
    "DiskRoot",
    "DiskUsage",
    "WORK_AREAS_DIR",
    "WORKSPACE_ROOT",
    "due_disk_roots",
    "enforce_disk_quota",
    "execution_digest",
    "is_cleanup_command",
    "plan_disk_quota",
    "work_area_key",
    "workspace_root",
]
