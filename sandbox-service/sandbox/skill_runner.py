"""
Skill runner — the top-level orchestrator.

Manages a registry of active sandboxes and provides the high-level API
consumed by the FastAPI routes:
  - create sandbox for a skill
  - exec commands
  - read/write files
  - build LLM context
  - full run (create → exec → collect)
  - destroy / prune idle sandboxes
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import posixpath
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sandbox_config import config as app_config

from .docker_backend import (
    BUSY_MARGIN_SECONDS,
    DockerCommandTimeout,
    DockerSandbox,
    skill_dir as _skill_dir,
    skill_slot as _skill_slot,
    _image_exists_locally,
    docker_host_resources,
    load_baked_skill_index as _load_baked_skill_index,
    list_managed_sandbox_containers,
    remove_dangling_sandbox_volumes,
    list_running_managed_container_names,
    remove_managed_sandbox_container,
    write_skill_files,
)
from .drain import DrainController, SandboxServiceDraining
from .capacity import (
    MemoryBudget,
    MemoryLimits,
    MemoryReservation,
    compute_limits,
    detect_host_resources,
    format_size,
    parse_size,
    to_mib,
)
from .baked_skills import BakedSkill, BakedSkillIndex
from .errors import (
    BakedSkillUnavailable,
    CancelFailed,
    CommandTimeout,
    DockerUnavailable,
    ExecQueueTimeout,
    ExecutionCancelled,
    ExecutionConflict,
    ExecutionEventNotFound,
    ExecutionNotFound,
    ExecutionStateConflict,
    InvalidRequest,
    RunnerError,
    RunnerFull,
    RunnerShuttingDown,
    SandboxDiskQuotaExceeded,
    SandboxGone,
    SandboxLost,
    SandboxUnavailable,
)
from .disk_quota import DISK_QUOTA_OUTCOME, due_disk_roots, enforce_disk_quota
from .fs_bridge import FsBridge
from .concurrency import (
    LocalActiveCapacity,
    SandboxConcurrencyExceeded,
    SandboxConcurrencyGate,
    SandboxGateConfig,
)
from .credential_refs import validate_sandbox_credential_ref
from .overrides import apply_overrides
from .models import (
    ContainerConfig,
    CreateSandboxResponse,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionResponseAck,
    ExecutionStatus,
    ExecutionStatusResponse,
    ExecResponse,
    FileReadBase64Response,
    FileReadResponse,
    FileTailResponse,
    FileWriteResponse,
    LoadSkillResponse,
    SandboxInfo,
    SandboxStatus,
    SkillContextResponse,
    SkillManifest,
    SkillRunResponse,
    SkillRunStepResult,
)
from .scanner import SkillScanner
from .security import (
    SecurityError,
    contains_sensitive_event_key,
    contains_sensitive_event_text,
    sanitize_env_vars,
    validate_container_path,
    validate_host_path,
    validate_skill_name,
)

logger = logging.getLogger(__name__)


def _host_capacity() -> dict[str, int]:
    """CPU count and physical memory (MB) of the host running the containers."""

    try:
        memory_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (AttributeError, OSError, ValueError):
        memory_mb = 0
    return {"host_cpus": os.cpu_count() or 0, "host_memory_mb": int(memory_mb)}

_EXECUTION_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_EXECUTION_EVENTS = 100
_MAX_EXECUTION_EVENT_BYTES = 8192
_CREDENTIAL_RESPONSE_REFERENCE_KEYS = {
    "credential_ref",
    "integration_account_id",
    "provider",
}
# /health answers from a cached image probe; the probe itself is short.
_IMAGE_STATUS_TTL_SECONDS = 30.0
_IMAGE_PROBE_TIMEOUT_SECONDS = 10.0
# Reading the baked Skills index starts one short container per image id.
_BAKED_INDEX_TIMEOUT_SECONDS = 60.0
# After a failed read, wait this long before starting another container.
_BAKED_INDEX_RETRY_SECONDS = 300.0
# After a destroy removed the container, its execution tasks end on their own
# (their docker exec returns); this bounds the wait before they are cancelled.
_DESTROY_TASK_GRACE_SECONDS = 10.0
# The longest a command may legally run (ExecRequest.timeout le=1800).
_MAX_COMMAND_TIMEOUT_SECONDS = 1800
_REAP_INTERVAL_SECONDS = 60
# A container past its max lifetime is recycled only after this much quiet,
# so a command about to follow the previous one is not cut off.
_RECYCLE_QUIET_SECONDS = 30


def _view_slot(sandbox: object, skill: str | None) -> str | None:
    """The Skill slot a request sees (None for a sandbox without Skill slots)."""
    resolve = getattr(sandbox, "resolve_view", None)
    return resolve(skill) if resolve is not None else None


def _claim(
    sandbox: object,
    execution_id: str,
    command: str,
    timeout: float,
    slot: str | None,
    fair_key: str | None = None,
) -> None:
    options = {}
    if slot is not None:
        options["slot"] = slot
    if fair_key:
        options["fair_key"] = fair_key
    sandbox.claim_command(execution_id, command, timeout, **options)


class SkillRunner:
    """
    Central sandbox registry and orchestrator.

    Keeps a dict of active sandboxes keyed by sandbox_id.
    Provides async methods for the full sandbox lifecycle.
    """

    def __init__(self) -> None:
        self._sandboxes: dict[str, DockerSandbox] = {}
        self._fs_bridges: dict[str, FsBridge] = {}
        self._scanner = SkillScanner()
        self._prune_task: asyncio.Task | None = None
        self._disk_task: asyncio.Task | None = None
        self._active_leases: dict[str, object] = {}
        # Executing-slot leases held by in-flight operations. A single exec can
        # run for up to an hour, longer than the gate TTL, so the prune loop
        # renews these alongside the active leases.
        self._executing_leases: set[object] = set()
        self._local_active_capacity = LocalActiveCapacity(app_config.MAX_SANDBOXES)
        # Memory admission: every registered sandbox (including one retained
        # after a failed cleanup) holds its container's memory limit here
        # until ``destroy_sandbox`` removes it from the registry.
        self._memory = MemoryBudget(self._memory_limits())
        self._memory_reservations: dict[str, MemoryReservation] = {}
        self._create_lock = asyncio.Lock()
        self._create_tasks: set[asyncio.Task[object]] = set()
        self._shutting_down = False
        self._idempotency_lock = asyncio.Lock()
        # Each in-flight create: its future and the identity, beyond its key,
        # it was asked for (None for an ordinary sandbox).
        self._idempotency_inflight: dict[
            str, tuple[asyncio.Future[CreateSandboxResponse], tuple[object, ...] | None]
        ] = {}
        self._idempotency_completed: dict[str, CreateSandboxResponse] = {}
        self._sandbox_idempotency_keys: dict[str, str] = {}
        self._execution_records: dict[
            tuple[str, str], ExecutionStatusResponse
        ] = {}
        self._execution_admission_lock = asyncio.Lock()
        self._execution_request_fingerprints: dict[tuple[str, str], str] = {}
        self._execution_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._execution_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._execution_cancel_requested: set[tuple[str, str]] = set()
        self._execution_cancel_confirmed: set[tuple[str, str]] = set()
        self._execution_responses: dict[tuple[str, str, str], bytes] = {}
        self._execution_response_lock = asyncio.Lock()
        self._orphan_cleanup_failures: list[str] = []
        self._drain = DrainController()
        self._started = False
        self._reclaim_tasks: dict[str, asyncio.Task[None]] = {}
        self._image_status: dict[str, object] | None = None
        self._image_status_at: float = 0.0
        self._image_probe: asyncio.Task[None] | None = None
        # (image id, index) of SANDBOX_IMAGE's baked Skills: one entry,
        # replaced when the image id changes.
        self._baked_index: tuple[str, BakedSkillIndex] | None = None
        self._baked_index_failed_at: float | None = None
        self._image_refresh_task: asyncio.Task | None = None
        self._gate = SandboxConcurrencyGate(
            SandboxGateConfig(
                redis_url=app_config.REDIS_URL,
                global_max_active=app_config.GLOBAL_MAX_ACTIVE,
                global_max_executing=app_config.GLOBAL_MAX_EXECUTING,
                instance_max_executing=app_config.INSTANCE_MAX_EXECUTING,
                ttl_seconds=app_config.GATE_TTL_SECONDS,
                wait_timeout_seconds=app_config.GATE_WAIT_TIMEOUT_SECONDS,
                operation_timeout_seconds=app_config.GATE_OPERATION_TIMEOUT_SECONDS,
            )
        )

    # ── lifecycle hooks ──

    async def startup(self) -> None:
        """Called on app startup — begins background prune loop."""
        await self._reconcile_managed_containers()
        await self._refresh_host_resources()
        # Fill the image cache before serving, so /health never waits on it.
        await self.refresh_image_status()
        self._prune_task = asyncio.create_task(self._prune_loop())
        self._image_refresh_task = asyncio.create_task(self._image_refresh_loop())
        if app_config.SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS > 0:
            self._disk_task = asyncio.create_task(self._disk_watchdog_loop())
        self._started = True
        limits = self._memory.limits
        logger.info(
            "SkillRunner started (max sandboxes=%d, host memory=%dMiB cpus=%s from %s, "
            "memory budget=%dMiB = host x %.2f - reserve %dMiB, max per container=%dMiB)",
            app_config.MAX_SANDBOXES,
            to_mib(limits.host_memory),
            limits.host_cpus,
            limits.source,
            to_mib(limits.budget),
            limits.overcommit,
            to_mib(limits.reserve),
            to_mib(limits.max_container),
        )

    async def shutdown(self) -> None:
        """Called on app shutdown — destroy all sandboxes.

        A signal-driven stop drains first (see ``drain``), so by the time this
        runs no caller is still between an exec and reading its result.
        """
        async with self._create_lock:
            self._shutting_down = True
            create_tasks = list(self._create_tasks)
        for attr in ("_prune_task", "_disk_task", "_image_refresh_task"):
            task = getattr(self, attr)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, attr, None)
        if create_tasks:
            await asyncio.gather(*create_tasks, return_exceptions=True)
        reclaims = [task for task in self._reclaim_tasks.values() if not task.done()]
        if reclaims:
            await asyncio.gather(*reclaims, return_exceptions=True)
        if self._image_probe is not None and not self._image_probe.done():
            self._image_probe.cancel()
            await asyncio.gather(self._image_probe, return_exceptions=True)
        tasks = [self.destroy_sandbox(sandbox_id) for sandbox_id in list(self._sandboxes)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._sandboxes:
            logger.warning("SkillRunner shut down with %d sandboxes still registered", len(self._sandboxes))
        else:
            logger.info("SkillRunner shut down, all sandboxes destroyed")

    # ── drain (graceful restart) ──

    @property
    def draining(self) -> bool:
        return self._drain.draining

    def track_request(self):
        """Context manager counting one API request as in-flight work."""

        return self._drain.track()

    def begin_drain(self) -> bool:
        """Stop admitting new sandboxes; existing ones keep working."""

        started = self._drain.begin()
        if started:
            logger.info(
                "Draining: refusing new sandboxes (active=%d inflight_requests=%d "
                "background_executions=%d)",
                len(self._sandboxes),
                self._drain.inflight,
                len(self._execution_tasks),
            )
        return started

    async def drain(self, *, timeout: float, quiet_seconds: float) -> bool:
        """Wait for in-flight requests and background executions to settle.

        Returns False when ``timeout`` elapsed with work still running; the
        caller then proceeds with shutdown, which destroys what is left.
        """

        self.begin_drain()
        started = time.monotonic()
        settled = await self._drain.wait_idle(
            timeout=timeout,
            quiet_seconds=quiet_seconds,
            busy=lambda: len(self._execution_tasks) + len(self._create_tasks),
        )
        elapsed = time.monotonic() - started
        if settled:
            logger.info("Drain complete after %.1fs; shutting down", elapsed)
        else:
            logger.warning(
                "Drain timed out after %.1fs with inflight_requests=%d "
                "background_executions=%d; remaining work will be destroyed",
                elapsed,
                self._drain.inflight,
                len(self._execution_tasks),
            )
        return settled

    def _reject_if_draining(self) -> None:
        if self._drain.draining:
            raise SandboxServiceDraining()

    # ── image probe refresh (cached; never on the request path) ──

    async def _image_refresh_loop(self) -> None:
        """Keep the image cache fresh so /health only ever reads it."""

        interval = max(1.0, float(app_config.RUNTIME_PROBE_INTERVAL_SECONDS))
        while True:
            try:
                await asyncio.sleep(interval)
                await self.refresh_image_status(max_age_seconds=0)
                if self._image_probe is not None:
                    await self._wait_for_task_terminal(self._image_probe)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in image refresh loop")

    def readiness(self) -> tuple[bool, str]:
        """Whether this process can serve sandbox traffic (cached, no I/O).

        Draining stays ready on purpose: a restarting process must remain
        reachable so callers can finish with the sandboxes it still owns.
        """

        if not self._started:
            return False, "starting"
        error = (self._image_status or {}).get("sandbox_image_error")
        if error:
            return False, str(error)
        return True, "ok"

    # ── public API ──

    async def scan_skill(self, skill_dir: str) -> SkillManifest:
        """Scan a skill directory and return its manifest."""
        return await asyncio.to_thread(self._scanner.scan, skill_dir)

    def runtime_status(self) -> dict[str, object]:
        """Return sandbox runtime readiness without creating a container.

        Pure in-memory: it backs the liveness endpoint, so it must never wait
        on Docker, the filesystem or a lock. Image availability comes from the
        cache that ``refresh_image_status`` fills off the event loop.
        """

        image = app_config.SANDBOX_IMAGE
        configured_execute_limit = self._gate.config.instance_max_executing
        status: dict[str, object] = {
            "sandbox_image": image,
            "active_sandboxes": sum(
                not getattr(sandbox, "is_lost", False) for sandbox in self._sandboxes.values()
            ),
            "reserved_active": self._local_active_capacity.reserved,
            "max_active": self._local_active_capacity.limit,
            "executing": self._gate.executing,
            "max_executing": (
                configured_execute_limit
                if configured_execute_limit > 0
                else self._local_active_capacity.limit
            ),
            "orphan_cleanup_failures": list(self._orphan_cleanup_failures),
            "draining": self._drain.draining,
            # Callers size their requests to this host (e.g. video rendering).
            # sysconf/cpu_count only: safe for the liveness path.
            **_host_capacity(),
            # Capabilities the control plane adapts to: Skills coexist in
            # /skills/<name> (per-request /skill view), commands wait in a fair
            # per-sandbox FIFO, containers are recycled after a max lifetime.
            "skill_layout": "slots",
            "exec_queue": True,
            "max_lifetime_seconds": app_config.MAX_LIFETIME_SECONDS,
        }
        status.update(self.memory_status())
        status.update(self._image_status or {"sandbox_image_available": None})
        # Built-in Skills the image holds, name -> content digest. The control
        # plane sends ``baked_skill`` instead of the files only for an exact
        # digest match; an old runner omits this and always gets the files.
        status["baked_skills"] = self._baked_skills().advertised()
        return status

    def memory_status(self) -> dict[str, object]:
        """Memory admission numbers for /health (MiB).

        ``largest_allocatable_mb`` is the biggest container memory limit a
        create would be admitted with right now: 0 while every slot is taken.
        A request above ``max_container_memory_mb`` is clamped to it, so a
        scheduler compares ``min(request, max_container_memory_mb)`` with it.
        """

        status = self._memory.status()
        slots_free = self._local_active_capacity.reserved < self._local_active_capacity.limit
        largest = self._memory.largest_allocatable if slots_free else 0
        status["largest_allocatable_mb"] = to_mib(largest)
        defaults = self._default_config()
        status["container_defaults"] = {
            "memory": defaults.memory,
            "memory_mb": to_mib(self._memory_limit_bytes(defaults)),
            "cpus": defaults.cpus,
            "pids_limit": defaults.pids_limit,
            "workdir_tmpfs_size": defaults.workdir_tmpfs_size,
            "network": defaults.network,
        }
        return status
    def _baked_skills(self) -> BakedSkillIndex:
        return self._baked_index[1] if self._baked_index is not None else BakedSkillIndex()

    def _resolve_baked_skill(self, name: str, digest: str) -> BakedSkill:
        """The image's copy of ``name`` at exactly ``digest``, else BakedSkillUnavailable."""
        return self._baked_skills().resolve(name, digest)

    async def refresh_image_status(
        self,
        *,
        max_age_seconds: float = _IMAGE_STATUS_TTL_SECONDS,
        probe_timeout_seconds: float = _IMAGE_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """Refresh the cached image availability without blocking the event loop.

        The probe (``docker image inspect``) runs in a thread with a short
        timeout and is shared by concurrent callers. While a refresh is in
        flight, callers that already have a (stale) answer use it instead of
        waiting.
        """

        if (
            self._image_status is not None
            and time.monotonic() - self._image_status_at < max_age_seconds
        ):
            return
        if self._image_probe is None or self._image_probe.done():
            self._image_probe = asyncio.create_task(
                self._probe_image(probe_timeout_seconds),
                name="sandbox-image-probe",
            )
        if self._image_status is not None:
            return
        await self._wait_for_task_terminal(self._image_probe)

    async def _probe_image(self, timeout_seconds: float) -> None:
        image = app_config.SANDBOX_IMAGE
        status: dict[str, object]
        try:
            available = await asyncio.to_thread(
                _image_exists_locally, image, timeout=timeout_seconds
            )
            status = {"sandbox_image_available": bool(available)}
        except DockerCommandTimeout as exc:
            # A slow host says nothing about the image: keep the last answer
            # instead of flapping callers (and readiness) into "unavailable".
            logger.warning("Sandbox image probe timed out: %s", exc)
            if self._image_status is not None:
                self._image_status_at = time.monotonic()
                return
            status = {"sandbox_image_available": None}
        except Exception as exc:
            available = False
            status = {"sandbox_image_available": False, "sandbox_image_error": str(exc)}
        self._image_status = status
        self._image_status_at = time.monotonic()
        if available:
            await self._refresh_baked_index(image)


    async def _refresh_baked_index(self, image: str) -> None:
        """Re-read the image's baked Skills index when the image id changed.

        A failure leaves the previous index in place (or none: every built-in
        Skill is then uploaded) and is retried after a back-off.
        """
        failed_at = self._baked_index_failed_at
        if failed_at is not None and time.monotonic() - failed_at < _BAKED_INDEX_RETRY_SECONDS:
            return
        try:
            loaded = await asyncio.to_thread(
                _load_baked_skill_index, image, self._baked_index, timeout=_BAKED_INDEX_TIMEOUT_SECONDS
            )
        except Exception as exc:
            self._baked_index_failed_at = time.monotonic()
            logger.warning("Could not read the baked Skills of %s; uploading built-in Skills: %s", image, exc)
            return
        self._baked_index_failed_at = None
        if loaded is not None and loaded is not self._baked_index:
            logger.info("Sandbox image %s bakes %d built-in Skills", image, len(loaded[1]))
        self._baked_index = loaded

    async def create_sandbox(
        self,
        skill_dir: str,
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_install: bool = True,
        idempotency_key: str | None = None,
        skill_name_override: str | None = None,
        baked: BakedSkill | None = None,
    ) -> CreateSandboxResponse:
        """Create a sandbox for a skill, inject files, install deps.

        With ``baked``, ``skill_dir`` holds only the Skill's root-level texts
        and the container gets the image's copy of the Skill.
        """
        self._reject_if_draining()
        # Validate the host path
        validate_host_path(skill_dir)

        # Scan skill (a filesystem walk: keep it off the event loop)
        if baked is not None:
            skill = await asyncio.to_thread(baked.manifest, skill_name_override or baked.name, skill_dir)
        else:
            skill = await asyncio.to_thread(self._scanner.scan, skill_dir)
        if skill_name_override:
            skill = skill.model_copy(update={"name": skill_name_override})

        key = idempotency_key or None
        if key is None:
            return await self._create_sandbox_once(
                skill,
                env,
                allowed_sensitive_keys,
                config_overrides,
                auto_install,
                sandbox_id=self._sandbox_id_for_create(None),
                baked=baked,
            )

        completed, future, is_leader = await self._prepare_idempotent_create(
            key,
        )
        if completed is not None:
            return completed
        if not is_leader:
            assert future is not None
            return await asyncio.shield(future)

        assert future is not None
        try:
            result = await self._create_sandbox_once(
                skill,
                env,
                allowed_sensitive_keys,
                config_overrides,
                auto_install,
                sandbox_id=self._sandbox_id_for_create(key),
                baked=baked,
            )
        except BaseException as exc:
            finalize_task = asyncio.create_task(
                self._fail_idempotent_create(key, future, exc)
            )
            await self._wait_for_task_terminal(finalize_task)
            raise

        finalize_task = asyncio.create_task(
            self._complete_idempotent_create(key, future, result)
        )
        await self._wait_for_task_terminal(finalize_task)
        return result

    async def _create_sandbox_once(
        self,
        skill: SkillManifest,
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None,
        config_overrides: ContainerConfig | None,
        auto_install: bool,
        *,
        sandbox_id: str,
        baked: BakedSkill | None = None,
    ) -> CreateSandboxResponse:
        """Perform one admitted create attempt without idempotency coordination."""

        create_task = await self._begin_create()
        try:
            # Resolve and size the container before taking any capacity: an
            # invalid or forbidden override is rejected without side effects.
            # Overrides are partial: fields the caller does not send keep the
            # runner's defaults, and runner-owned fields cannot be sent.
            cfg = self._resolve_config(config_overrides)
            reserved_bytes = 0
            warnings = self._fit_to_host(
                cfg,
            )
            memory_ticket = await self._reserve_capacity(self._memory_limit_bytes(cfg) + reserved_bytes)
            sandbox: DockerSandbox | None = None
            active_lease: object | None = None
            setup_task: asyncio.Task[None] | None = None
            try:
                # Sanitize env vars
                safe_env, blocked = sanitize_env_vars(env, allowed_sensitive_keys)

                sandbox = DockerSandbox(
                    sandbox_id,
                    cfg,
                )
                active_lease = await self._gate.acquire_active()
                setup_task = asyncio.create_task(
                    self._setup_sandbox(sandbox, skill, safe_env, auto_install, baked)
                )
                await asyncio.shield(setup_task)
                bridge = FsBridge(sandbox)
                response = CreateSandboxResponse(
                    sandbox_id=sandbox_id,
                    container_name=sandbox.container_name,
                    status=sandbox.status,
                    skill=skill,
                    workdir=cfg.workdir,
                    skill_dir=_skill_dir(_skill_slot(skill.name)),
                    env_blocked=blocked,
                    config=cfg.model_copy(deep=True),
                    warnings=warnings,
                )

                self._sandboxes[sandbox_id] = sandbox
                self._fs_bridges[sandbox_id] = bridge
                self._active_leases[sandbox_id] = active_lease
                self._memory_reservations[sandbox_id] = memory_ticket
                return response
            except BaseException:
                if setup_task is not None:
                    await self._wait_for_task_terminal(setup_task)
                cleanup_task = asyncio.create_task(
                    self._cleanup_failed_create(sandbox, active_lease, memory_ticket)
                )
                await self._wait_for_task_terminal(cleanup_task)
                raise
        finally:
            self._create_tasks.discard(create_task)

    async def create_sandbox_from_files(
        self,
        skill_name: str,
        files: dict[str, str],
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_install: bool = True,
        idempotency_key: str | None = None,
    ) -> CreateSandboxResponse:
        """
        Create a sandbox from in-memory file contents (no host path required).

        Used when skill files live in remote storage (e.g. MinIO) and are not
        available on the sandbox host filesystem. Writes files to a temp
        directory, scans, creates the sandbox, then cleans up the temp dir.
        """
        import shutil
        import tempfile

        validate_skill_name(skill_name)
        if not files:
            raise InvalidRequest("files dict must not be empty")
        self._reject_if_draining()

        # Temp-dir writes are blocking filesystem work: keep them off the loop.
        tmp_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="sbx-files-")
        try:
            await asyncio.to_thread(write_skill_files, tmp_dir, files)
            return await self.create_sandbox(
                skill_dir=tmp_dir,
                env=env,
                allowed_sensitive_keys=allowed_sensitive_keys,
                config_overrides=config_overrides,
                auto_install=auto_install,
                idempotency_key=idempotency_key,
                skill_name_override=skill_name,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)

    async def create_sandbox_from_baked(
        self,
        skill_name: str,
        baked_name: str,
        digest: str,
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_install: bool = True,
        idempotency_key: str | None = None,
    ) -> CreateSandboxResponse:
        """Create a sandbox for a built-in Skill from the image's baked copy.

        Nothing is uploaded: the image already holds the Skill at ``digest``
        (``BakedSkillUnavailable`` otherwise, before any container exists, and
        the caller sends the files instead).
        """
        import shutil
        import tempfile

        validate_skill_name(skill_name)
        baked = self._resolve_baked_skill(baked_name, digest)
        stub_dir = tempfile.mkdtemp(prefix="sbx-baked-")
        try:
            baked.write_root_texts(stub_dir)
            return await self.create_sandbox(
                skill_dir=stub_dir,
                env=env,
                allowed_sensitive_keys=allowed_sensitive_keys,
                config_overrides=config_overrides,
                auto_install=auto_install,
                idempotency_key=idempotency_key,
                skill_name_override=skill_name,
                baked=baked,
            )
        finally:
            shutil.rmtree(stub_dir, ignore_errors=True)

    async def exec_command(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
        execution_id: str | None = None,
        retain_mailbox: bool = False,
        skill_env: bool = True,
        skill: str | None = None,
        fair_key: str | None = None,
        queue_timeout: float = 0,
        env: dict[str, str] | None = None,
    ) -> ExecResponse:
        """Execute a command in an existing sandbox and wait for it.

        The command takes one of the sandbox's slots before anything runs.
        With ``queue_timeout`` it waits for a free slot in the sandbox's fair
        FIFO (``ExecQueueTimeout`` once that runs out: it never started);
        without, a full sandbox answers ``SandboxBusy`` at once. While it
        waits or runs, the command is cancellable by its execution id.
        ``env`` is layered over the skill env for this one command only.
        """
        resolved_execution_id = execution_id or uuid.uuid4().hex
        async with self._sandbox_scope(sandbox_id) as sandbox:
            if (sandbox_id, resolved_execution_id) in self._execution_records:
                raise ExecutionConflict(
                    "execution_id already belongs to a background execution"
                )
            slot = _view_slot(sandbox, skill)
            if queue_timeout and queue_timeout > 0:
                await self._wait_for_slot(
                    sandbox,
                    sandbox.enqueue_command(
                        resolved_execution_id, command, timeout, fair_key=fair_key, slot=slot,
                    ),
                    resolved_execution_id,
                    queue_timeout,
                )
            else:
                _claim(sandbox, resolved_execution_id, command, timeout, slot, fair_key)
            try:
                async with self._operation_guard():
                    return await sandbox.exec(
                        command,
                        timeout=timeout,
                        workdir=workdir,
                        execution_id=resolved_execution_id,
                        interactive=retain_mailbox,
                        **({} if skill_env else {"skill_env": False}),
                        **({} if skill is None else {"skill": skill}),
                        **({"env": env} if env else {}),
                    )
            finally:
                sandbox.release_command(resolved_execution_id)

    @staticmethod
    async def _wait_for_slot(
        sandbox: DockerSandbox,
        granted: asyncio.Future,
        execution_id: str,
        queue_timeout: float,
    ) -> None:
        """Wait until a queued command holds its slot.

        Raises ``ExecQueueTimeout`` (never started) when the wait runs out,
        ``ExecutionCancelled`` when it was cancelled while waiting, and the
        sandbox's own error when it was lost meanwhile. A caller that goes
        away withdraws its place, or frees the slot it was just handed.
        """
        try:
            await asyncio.wait_for(asyncio.shield(granted), timeout=queue_timeout)
        except asyncio.TimeoutError:
            if sandbox.withdraw_waiter(
                execution_id,
                ExecQueueTimeout(
                    f"No slot freed up within {queue_timeout:.0f}s",
                    extra={"started": False},
                ),
            ):
                raise ExecQueueTimeout(
                    f"The command waited {queue_timeout:.0f}s for one of the sandbox's "
                    f"{sandbox.max_concurrent_executions} command slots and was not started.",
                    extra={"started": False, "queue_timeout_seconds": queue_timeout},
                ) from None
            # The slot was granted at the very deadline: run it.
        except asyncio.CancelledError:
            if not sandbox.withdraw_waiter(execution_id) and granted.done() and not granted.cancelled():
                sandbox.release_command(execution_id)
            raise

    async def side_channel_exec(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
    ) -> ExecResponse:
        """Run a short bookkeeping command next to the command slots (``side_exec``).

        The platform's own scripts that move files in and out of a working
        copy (bash staging and read-back) are file operations in substance:
        like a file read or write they run next to whatever commands hold the
        slots, so they never wait behind -- and can never deadlock behind --
        a queued command, including the one they prepare. Bounded by
        ``SIDE_CHANNEL_MAX_TIMEOUT_SECONDS``; never given the Skill env.
        """
        limit = max(1, int(app_config.SIDE_CHANNEL_MAX_TIMEOUT_SECONDS))
        if timeout > limit:
            raise InvalidRequest(
                f"A side-channel exec runs at most {limit}s (asked for {timeout}s)."
            )
        async with self._sandbox_scope(sandbox_id) as sandbox:
            async with self._operation_guard():
                return await sandbox.side_exec(command, timeout=timeout, workdir=workdir or "/")

    async def start_execution(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
        execution_id: str | None = None,
        skill_env: bool = True,
        skill: str | None = None,
        fair_key: str | None = None,
        queue_timeout: float = 0,
        env: dict[str, str] | None = None,
    ) -> ExecutionStatusResponse:
        """Schedule a command and return before its process completes.

        Its slot is taken -- or its place in the sandbox's fair FIFO, with
        ``queue_timeout`` -- synchronously with admission: without a queue
        budget a full sandbox is answered with ``SandboxBusy`` now; with one
        the record stays ``queued`` (with its ``queue_position``) until a
        slot frees, and ends ``failed`` / ``queue_timeout`` if none did.
        ``env`` is layered over the skill env for this one command; it lives
        only in the execution's own task (never on its record, never in its
        request fingerprint) and is gone once the command ends.
        """

        async with self._sandbox_scope(sandbox_id) as sandbox:
            return await self._start_execution(
                sandbox,
                sandbox_id,
                command,
                timeout=timeout,
                workdir=workdir,
                execution_id=execution_id,
                skill_env=skill_env,
                skill=skill,
                fair_key=fair_key,
                queue_timeout=queue_timeout,
                **({"env": dict(env)} if env else {}),
            )

    async def _start_execution(
        self,
        sandbox: DockerSandbox,
        sandbox_id: str,
        command: str,
        *,
        timeout: int,
        workdir: str | None,
        execution_id: str | None,
        skill_env: bool = True,
        skill: str | None = None,
        fair_key: str | None = None,
        queue_timeout: float = 0,
        env: dict[str, str] | None = None,
    ) -> ExecutionStatusResponse:
        sandbox.ensure_available()
        resolved_execution_id = execution_id or uuid.uuid4().hex
        key = (sandbox_id, resolved_execution_id)
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "command": command,
                    "timeout": timeout,
                    "workdir": workdir,
                    **({} if skill_env else {"skill_env": False}),
                    **({} if skill is None else {"skill": skill}),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cleanup_ids: dict[str, list[str]] = {}
        async with self._execution_admission_lock:
            existing = self._execution_records.get(key)
            if existing is not None:
                if self._execution_request_fingerprints.get(key) != request_fingerprint:
                    raise ExecutionConflict(
                        "execution_id already exists with a different request"
                    )
                return self._record_view(sandbox, existing)
            cleanup_ids = await self._prune_execution_history()
            pending_count = sum(
                record.status not in ExecutionStatus.terminal()
                for record in self._execution_records.values()
            )
            if pending_count >= max(1, app_config.MAX_PENDING_EXECUTIONS):
                raise SandboxConcurrencyExceeded(
                    "Max pending sandbox execution limit reached "
                    f"({app_config.MAX_PENDING_EXECUTIONS}). Please poll or cancel existing work."
                )
            slot = _view_slot(sandbox, skill)
            granted: asyncio.Future | None = None
            if queue_timeout and queue_timeout > 0:
                granted = sandbox.enqueue_command(
                    resolved_execution_id, command, timeout, fair_key=fair_key, slot=slot,
                )
            else:
                _claim(sandbox, resolved_execution_id, command, timeout, slot, fair_key)

            record = ExecutionStatusResponse(
                sandbox_id=sandbox_id,
                execution_id=resolved_execution_id,
                status=ExecutionStatus.QUEUED,
                created_at=time.time(),
            )
            self._execution_records[key] = record
            self._execution_request_fingerprints[key] = request_fingerprint
            self._execution_locks[key] = asyncio.Lock()
            task = asyncio.create_task(
                self._run_background_execution(
                    key,
                    command=command,
                    timeout=timeout,
                    workdir=workdir,
                    skill_env=skill_env,
                    skill=skill,
                    granted=granted,
                    queue_timeout=queue_timeout,
                    **({"env": dict(env)} if env else {}),
                ),
                name=f"sandbox-exec-{sandbox_id}-{resolved_execution_id}",
            )
            self._execution_tasks[key] = task
            result = self._record_view(sandbox, record)
        await self._cleanup_execution_mailboxes(cleanup_ids)
        return result

    async def _prune_execution_history(self) -> dict[str, list[str]]:
        now = time.time()
        ttl = max(1, app_config.EXECUTION_HISTORY_TTL_SECONDS)
        terminal = sorted(
            (
                (key, record)
                for key, record in self._execution_records.items()
                if record.status in ExecutionStatus.terminal()
            ),
            key=lambda item: item[1].finished_at or item[1].created_at,
        )
        removed: list[tuple[str, str]] = []
        for key, record in terminal:
            finished_at = record.finished_at or record.created_at
            if now - finished_at > ttl:
                self._execution_records.pop(key, None)
                removed.append(key)
        max_history = max(1, app_config.MAX_EXECUTION_HISTORY)
        terminal_keys = [
            key
            for key, record in terminal
            if key in self._execution_records
            and record.status in ExecutionStatus.terminal()
        ]
        while len(self._execution_records) >= max_history and terminal_keys:
            key = terminal_keys.pop(0)
            self._execution_records.pop(key, None)
            removed.append(key)
        cleanup_ids: dict[str, list[str]] = {}
        for key in removed:
            self._execution_request_fingerprints.pop(key, None)
            self._execution_locks.pop(key, None)
            self._forget_execution_response_state(key)
            cleanup_ids.setdefault(key[0], []).append(key[1])
        return cleanup_ids

    async def _cleanup_execution_mailboxes(
        self,
        cleanup_ids: dict[str, list[str]],
    ) -> None:
        """Run best-effort Docker cleanup outside the admission lock."""

        cleanup_tasks: list[asyncio.Task[None]] = []
        for sandbox_id, execution_ids in cleanup_ids.items():
            sandbox = self._sandboxes.get(sandbox_id)
            cleanup_many = getattr(sandbox, "cleanup_execution_mailboxes", None)
            if cleanup_many is not None:
                cleanup_tasks.append(asyncio.create_task(cleanup_many(execution_ids)))
                continue
            cleanup_one = getattr(sandbox, "cleanup_execution_mailbox", None)
            if cleanup_one is not None:
                cleanup_tasks.extend(
                    asyncio.create_task(cleanup_one(execution_id))
                    for execution_id in execution_ids
                )
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    async def _run_background_execution(
        self,
        key: tuple[str, str],
        *,
        command: str,
        timeout: int,
        workdir: str | None,
        skill_env: bool = True,
        skill: str | None = None,
        granted: asyncio.Future | None = None,
        queue_timeout: float = 0,
        env: dict[str, str] | None = None,
    ) -> None:
        sandbox_id, execution_id = key
        record = self._execution_records[key]
        execution_lock = self._execution_locks[key]
        sandbox = self._sandboxes.get(sandbox_id)
        try:
            if granted is not None and sandbox is not None:
                try:
                    await self._wait_for_slot(sandbox, granted, execution_id, queue_timeout)
                except ExecQueueTimeout as exc:
                    async with execution_lock:
                        if self._settle_record(record, ExecutionStatus.FAILED, error=str(exc)):
                            record.outcome = "queue_timeout"
                            record.outcome_detail = str(exc)
                    return
                except ExecutionCancelled:
                    async with execution_lock:
                        self._settle_record(
                            record, ExecutionStatus.CANCELLED, error="cancelled before it started",
                        )
                    return
                except SandboxDiskQuotaExceeded as exc:
                    # Its work area went over its disk share while it waited.
                    async with execution_lock:
                        if self._settle_record(record, ExecutionStatus.FAILED, error=str(exc)):
                            record.outcome = DISK_QUOTA_OUTCOME
                            record.outcome_detail = str(exc)
                    return
                except (SandboxGone, SandboxUnavailable) as exc:
                    async with execution_lock:
                        if self._settle_record(
                            record,
                            ExecutionStatus.FAILED,
                            error=f"sandbox lost before the command started: {exc}",
                        ):
                            record.outcome = "sandbox_lost"
                            record.outcome_detail = "never started"
                    return
            async with execution_lock:
                if record.status in ExecutionStatus.terminal():
                    # Finalised before it started (its sandbox was lost or destroyed).
                    return
                record.status = ExecutionStatus.RUNNING
                record.started_at = time.time()
                if key in self._execution_cancel_requested:
                    self._settle_record(record, ExecutionStatus.CANCELLED)
                    return

            try:
                async with self._sandbox_scope(sandbox_id, wait_for_reclaim=False) as sandbox:
                    async with self._operation_guard():
                        if key in self._execution_cancel_requested:
                            # Cancelled while waiting for an execute slot: never start it.
                            async with execution_lock:
                                self._settle_record(record, ExecutionStatus.CANCELLED)
                            return
                        result = await sandbox.exec(
                            command,
                            timeout=timeout,
                            workdir=workdir,
                            execution_id=execution_id,
                            interactive=True,
                            **({} if skill_env else {"skill_env": False}),
                            **({} if skill is None else {"skill": skill}),
                            **({"env": env} if env else {}),
                        )
                    # Events emitted just before exit are read once here, so a
                    # terminal record never needs the container again.
                    await self._refresh_events_quietly(sandbox, record)
            except asyncio.CancelledError:
                async with execution_lock:
                    self._settle_record(
                        record, ExecutionStatus.CANCELLED, error="execution task cancelled"
                    )
                raise
            except CommandTimeout as exc:
                async with execution_lock:
                    self._settle_record(
                        record,
                        ExecutionStatus.CANCELLED
                        if key in self._execution_cancel_confirmed
                        else ExecutionStatus.FAILED,
                        error=str(exc),
                        stdout=exc.extra.get("stdout"),
                        stderr=exc.extra.get("stderr"),
                    )
            except Exception as exc:
                async with execution_lock:
                    settled = self._settle_record(
                        record,
                        ExecutionStatus.CANCELLED
                        if key in self._execution_cancel_confirmed
                        else ExecutionStatus.FAILED,
                        error=str(exc),
                    )
                    if settled and isinstance(exc, SandboxGone) and key not in self._execution_cancel_confirmed:
                        record.outcome = "sandbox_lost"
                        record.outcome_detail = "lost while running"
            else:
                async with execution_lock:
                    if key in self._execution_cancel_confirmed:
                        status = ExecutionStatus.CANCELLED
                        error = None
                    elif result.exit_code == 0:
                        status = ExecutionStatus.COMPLETED
                        error = None
                    else:
                        status = ExecutionStatus.FAILED
                        error = f"command exited with code {result.exit_code}"
                    if getattr(result, "outcome", None) and key not in self._execution_cancel_confirmed:
                        error = result.outcome_detail or error
                    if self._settle_record(
                        record,
                        status,
                        error=error,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        exit_code=result.exit_code,
                    ) and key not in self._execution_cancel_confirmed:
                        record.outcome = getattr(result, "outcome", None)
                        record.outcome_detail = getattr(result, "outcome_detail", None)
                        if status == ExecutionStatus.FAILED:
                            # Same record as a synchronous exec's: the stderr
                            # tail carries a command's own diagnosis.
                            logger.warning(
                                "sandbox/execution: non-zero sandbox_id=%s execution_id=%s "
                                "exit_code=%s outcome=%s stderr_tail=%r",
                                sandbox_id, execution_id, result.exit_code,
                                record.outcome or "-", (result.stderr or "")[-600:],
                            )
        finally:
            if sandbox is not None:
                # The claim was taken at admission; release it on every path,
                # including a cancellation before the command started.
                sandbox.release_command(execution_id)
            if self._execution_tasks.get(key) is asyncio.current_task():
                self._execution_tasks.pop(key, None)
            self._execution_cancel_requested.discard(key)
            self._execution_cancel_confirmed.discard(key)
            # The caller still has to poll this result: keep a drain open.
            self._drain.note_activity()

    @staticmethod
    def _record_view(sandbox: object, record: ExecutionStatusResponse) -> ExecutionStatusResponse:
        """A copy of a record with its live place in the slot queue."""
        result = record.model_copy(deep=True)
        if result.status == ExecutionStatus.QUEUED:
            position = getattr(sandbox, "queue_position", None)
            result.queue_position = position(record.execution_id) if position else None
        else:
            result.queue_position = None
        return result

    @staticmethod
    def _settle_record(
        record: ExecutionStatusResponse,
        status: ExecutionStatus,
        *,
        error: str | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
    ) -> bool:
        """Move a record to a terminal state once; later outcomes never overwrite it."""
        if record.status in ExecutionStatus.terminal():
            return False
        record.status = status
        if error is not None:
            record.error = error
        if stdout is not None:
            record.stdout = stdout
        if stderr is not None:
            record.stderr = stderr
        if exit_code is not None:
            record.exit_code = exit_code
        record.finished_at = time.time()
        return True

    async def get_execution_status(
        self,
        sandbox_id: str,
        execution_id: str,
        after_sequence: int = 0,
        wait_seconds: float = 0,
    ) -> ExecutionStatusResponse:
        """Report an execution. Terminal records are served from memory only.

        ``wait_seconds`` long-polls: the answer comes as soon as the command
        ends (or the wait runs out), so a caller waiting for a result does
        not poll in a tight loop.
        """
        key = (sandbox_id, execution_id)
        if wait_seconds and wait_seconds > 0:
            self._get_sandbox(sandbox_id)
            task = self._execution_tasks.get(key)
            record = self._execution_records.get(key)
            if task is not None and record is not None and record.status not in ExecutionStatus.terminal():
                await asyncio.wait({task}, timeout=min(float(wait_seconds), 60.0))
        async with self._sandbox_scope(sandbox_id) as sandbox:
            record = self._execution_records.get(key)
            if record is None:
                raise ExecutionNotFound(f"Execution not found: {execution_id}")
            if record.status not in ExecutionStatus.terminal():
                await self._refresh_events_quietly(sandbox, record)
            result = self._record_view(sandbox, record)
        result.events = [
            event for event in result.events if event.sequence > max(0, after_sequence)
        ]
        return result

    async def _refresh_events_quietly(
        self,
        sandbox: DockerSandbox,
        record: ExecutionStatusResponse,
    ) -> None:
        """Refresh events; a lost container propagates, a transient read failure does not.

        The record's state is authoritative without fresh events, so a Docker
        hiccup while reading the mailbox must not fail a status poll.
        """
        try:
            await self._refresh_execution_events(sandbox, record)
        except SandboxGone:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Could not refresh events for execution %s on sandbox %s",
                record.execution_id,
                record.sandbox_id,
                exc_info=True,
            )

    @staticmethod
    def _contains_secret_event_key(value: object) -> bool:
        return contains_sensitive_event_key(value)

    async def _refresh_execution_events(
        self,
        sandbox: DockerSandbox,
        record: ExecutionStatusResponse,
    ) -> None:
        reader = getattr(sandbox, "read_execution_event_lines", None)
        if reader is None:
            return
        lines, _truncated = await reader(record.execution_id)
        known_ids = {event.event_id for event in record.events}
        for line in lines:
            if len(record.events) >= _MAX_EXECUTION_EVENTS:
                break
            if len(line.encode("utf-8")) > _MAX_EXECUTION_EVENT_BYTES:
                continue
            try:
                raw = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            event_id = str(raw.get("event_id") or "").strip()
            if event_id in known_ids or not _EXECUTION_EVENT_ID_RE.fullmatch(event_id):
                continue
            try:
                event_type = ExecutionEventType(str(raw.get("type") or ""))
            except ValueError:
                continue
            message = str(raw.get("message") or "")
            payload = raw.get("payload") or {}
            if len(message) > 2000 or not isinstance(payload, dict):
                continue
            if self._contains_secret_event_key(payload) or contains_sensitive_event_text(
                {"message": message, "payload": payload}
            ):
                continue
            try:
                payload_size = len(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
            except (TypeError, ValueError):
                continue
            if payload_size > _MAX_EXECUTION_EVENT_BYTES:
                continue
            requires_response = bool(raw.get("requires_response")) or event_type in {
                ExecutionEventType.NEED_INPUT,
                ExecutionEventType.NEED_FILE,
                ExecutionEventType.NEED_TOOL,
                ExecutionEventType.NEED_CREDENTIAL,
            }
            event = ExecutionEvent(
                sequence=len(record.events) + 1,
                event_id=event_id,
                type=event_type,
                message=message,
                payload=payload,
                requires_response=requires_response,
                created_at=time.time(),
            )
            record.events.append(event)
            known_ids.add(event_id)
        record.next_sequence = len(record.events)
        record.waiting_for_response = any(
            event.requires_response and not event.responded for event in record.events
        )

    async def send_execution_response(
        self,
        sandbox_id: str,
        execution_id: str,
        event_id: str,
        *,
        payload: dict,
        message: str,
    ) -> ExecutionResponseAck:
        async with self._sandbox_scope(sandbox_id) as sandbox:
            return await self._send_execution_response(
                sandbox,
                sandbox_id,
                execution_id,
                event_id,
                payload=payload,
                message=message,
            )

    async def _send_execution_response(
        self,
        sandbox: DockerSandbox,
        sandbox_id: str,
        execution_id: str,
        event_id: str,
        *,
        payload: dict,
        message: str,
    ) -> ExecutionResponseAck:
        if len(message) > 2000:
            raise InvalidRequest("Sandbox execution response message exceeds 2000 characters")
        if not isinstance(payload, dict):
            raise InvalidRequest("Sandbox execution response payload must be an object")
        if self._contains_secret_event_key(payload):
            raise InvalidRequest(
                "Sandbox execution responses accept credential references, not secret values"
            )
        if contains_sensitive_event_text({"message": message, "payload": payload}):
            raise InvalidRequest(
                "Sandbox execution responses accept credential references, not secret values"
            )
        key = (sandbox_id, execution_id)
        record = self._execution_records.get(key)
        if record is None:
            raise ExecutionNotFound(f"Execution not found: {execution_id}")
        execution_lock = self._execution_locks.get(key)
        if execution_lock is None:
            raise ExecutionStateConflict("Sandbox execution response state is unavailable")
        if not _EXECUTION_EVENT_ID_RE.fullmatch(event_id):
            raise InvalidRequest("Invalid sandbox execution event_id")
        response_request = {
            "event_id": event_id,
            "message": message,
            "payload": payload,
        }
        canonical_request = json.dumps(
            response_request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = json.dumps(
            {**response_request, "created_at": time.time()},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        response_key = (sandbox_id, execution_id, event_id)
        async with self._execution_response_lock:
            existing = self._execution_responses.get(response_key)
            if existing is not None:
                if existing != canonical_request:
                    raise ExecutionConflict("Execution event already has a different response")
                event = next(
                    (
                        candidate
                        for candidate in record.events
                        if candidate.event_id == event_id
                    ),
                    None,
                )
                if event is not None:
                    event.responded = True
                record.waiting_for_response = any(
                    item.requires_response and not item.responded for item in record.events
                )
                return ExecutionResponseAck(
                    sandbox_id=sandbox_id,
                    execution_id=execution_id,
                    event_id=event_id,
                    accepted=True,
                    duplicate=True,
                )
            await self._refresh_execution_events(sandbox, record)
            async with execution_lock:
                if record.status in ExecutionStatus.terminal():
                    raise ExecutionStateConflict("Cannot respond to a terminal sandbox execution")
                event = next(
                    (
                        candidate
                        for candidate in record.events
                        if candidate.event_id == event_id
                    ),
                    None,
                )
                if event is None:
                    raise ExecutionEventNotFound(f"Execution event not found: {event_id}")
                if not event.requires_response:
                    raise ExecutionConflict("Sandbox execution event does not accept a response")
                if event.type == ExecutionEventType.NEED_CREDENTIAL:
                    credential_ref = payload.get("credential_ref")
                    provider = payload.get("provider")
                    integration_account_id = payload.get("integration_account_id")
                    if (
                        message.strip()
                        or not isinstance(credential_ref, str)
                        or not credential_ref.strip()
                    ):
                        raise InvalidRequest(
                            "need_credential responses require a signed credential_ref and no message"
                        )
                    unexpected_keys = set(payload) - _CREDENTIAL_RESPONSE_REFERENCE_KEYS
                    missing_keys = _CREDENTIAL_RESPONSE_REFERENCE_KEYS - set(payload)
                    invalid_reference_values = any(
                        not isinstance(value, str) or not value.strip()
                        for value in payload.values()
                    )
                    event_provider = event.payload.get("provider")
                    if unexpected_keys or missing_keys or invalid_reference_values:
                        raise InvalidRequest(
                            "need_credential responses accept reference fields only"
                        )
                    if not isinstance(event_provider, str) or provider != event_provider:
                        raise InvalidRequest(
                            "need_credential response provider does not match the event"
                        )
                    validate_sandbox_credential_ref(
                        credential_ref,
                        signing_key=app_config.API_TOKEN,
                        sandbox_id=sandbox_id,
                        execution_id=execution_id,
                        event_id=event_id,
                        provider=provider,
                        integration_account_id=integration_account_id,
                    )
                writer = getattr(sandbox, "write_execution_response", None)
                if writer is None:
                    raise RuntimeError("Sandbox runner does not support execution responses")
                await writer(execution_id, event_id, encoded)
                self._execution_responses[response_key] = canonical_request
                event.responded = True
                record.waiting_for_response = any(
                    item.requires_response and not item.responded for item in record.events
                )
        return ExecutionResponseAck(
            sandbox_id=sandbox_id,
            execution_id=execution_id,
            event_id=event_id,
            accepted=True,
        )

    def _forget_execution_response_state(self, key: tuple[str, str]) -> None:
        for response_key in [
            response_key
            for response_key in self._execution_responses
            if response_key[:2] == key
        ]:
            self._execution_responses.pop(response_key, None)

    async def cancel_execution(self, sandbox_id: str, execution_id: str) -> bool:
        """Cancel a running execution, background or synchronous, by its id.

        Returns True when the execution was running and is now cancelled (or
        its cancellation is recorded), False when it is a recorded execution
        that already ended. ``ExecutionNotFound`` means no execution with
        this id is running or recorded.
        """
        async with self._sandbox_scope(sandbox_id) as sandbox:
            key = (sandbox_id, execution_id)
            record = self._execution_records.get(key)
            execution_lock = self._execution_locks.get(key)
            if record is None or execution_lock is None:
                # A synchronous /exec still waiting for a slot: withdraw it.
                withdraw = getattr(sandbox, "withdraw_waiter", None)
                if withdraw is not None and withdraw(
                    execution_id, ExecutionCancelled("cancelled before it started"),
                ):
                    return True
                # A synchronous /exec: only one running now can be found.
                running = getattr(sandbox, "active_execution_ids", None)
                if running is None:
                    running = [getattr(sandbox, "active_execution_id", None)]
                if execution_id not in running:
                    raise ExecutionNotFound(
                        f"Execution not found: {execution_id} is not running"
                    )
                return await sandbox.cancel_execution(execution_id)

            async with execution_lock:
                if record.status in ExecutionStatus.terminal():
                    return False
                self._execution_cancel_requested.add(key)
                # Cancellation is an accepted state transition once the execution
                # was observed as non-terminal. Mark it before signalling so a
                # fast process exit cannot race the receipt into "completed".
                self._execution_cancel_confirmed.add(key)
                withdraw = getattr(sandbox, "withdraw_waiter", None)
                if withdraw is not None and withdraw(
                    execution_id, ExecutionCancelled("cancelled before it started"),
                ):
                    # Still waiting for a slot: it leaves the queue and never runs.
                    return True

            # The process may not have started yet (queued for an execute
            # slot): the background task then sees the request before it runs
            # anything. If it has started, kill its process group.
            attempts = 3
            for attempt in range(attempts):
                try:
                    if await sandbox.cancel_execution(execution_id):
                        return True
                except CancelFailed:
                    task = self._execution_tasks.get(key)
                    if task is None or task.done():
                        break
                    if attempt == attempts - 1:
                        raise
                    continue
                task = self._execution_tasks.get(key)
                if task is None or task.done():
                    break
                await asyncio.sleep(0.01)
            return True

    async def read_file(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 65536,
        skill: str | None = None,
    ) -> FileReadResponse:
        """Read a file from a sandbox (side channel: works while a command runs)."""
        async with self._sandbox_scope(sandbox_id):
            bridge = self._get_bridge(sandbox_id)
            content, truncated = await bridge.read_file(
                path, max_size=max_size, **({} if skill is None else {"skill": skill})
            )
        return FileReadResponse(
            path=path,
            content=content,
            size=len(content),
            truncated=truncated,
        )

    async def read_file_base64(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 50 * 1024 * 1024,
        skill: str | None = None,
    ) -> FileReadBase64Response:
        """Read a binary file from a sandbox as base64 (side channel)."""
        async with self._sandbox_scope(sandbox_id):
            bridge = self._get_bridge(sandbox_id)
            content_b64, file_size = await bridge.read_file_base64(
                path, max_size=max_size, **({} if skill is None else {"skill": skill})
            )
        return FileReadBase64Response(
            path=path,
            content_base64=content_b64,
            size=file_size,
        )

    async def tail_file(self, sandbox_id: str, path: str, offset: int, limit: int) -> FileTailResponse:
        """Byte-range read for append-only logs, next to any running command.

        Deliberately skips ``_operation_guard()`` and never claims the
        sandbox, so it keeps working while a long background exec holds the
        command claim. Like every runner operation it runs inside
        ``_sandbox_scope``: a container Docker lost is reclaimed and reported
        as a 404, never misread as a missing log.
        """
        if "\x00" in path:
            raise ValueError("Path is not tail-readable: contains a null byte")
        normalized = posixpath.normpath(path)
        if not path.startswith("/") or not any(
            normalized.startswith(p.rstrip("/") + "/") for p in app_config.TAIL_ALLOWED_PREFIXES
        ):
            raise ValueError(f"Path is not tail-readable: {path}")
        async with self._sandbox_scope(sandbox_id) as sandbox:
            data, size = await sandbox.read_file_range(normalized, offset, limit)
        # ``size`` is stat'd before the read, so a growing log may return bytes
        # beyond it: whenever data was read, advance by exactly what was
        # delivered so the next call never re-reads them. Clamp only when
        # nothing was read (offset past the end after truncation/rotation).
        next_offset = offset + len(data) if data else min(offset, size)
        return FileTailResponse(
            path=normalized, offset=offset, data_base64=base64.b64encode(data).decode("ascii"),
            next_offset=next_offset, size=size, eof=next_offset >= size,
        )

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
        mkdir: bool = True,
        skill: str | None = None,
    ) -> FileWriteResponse:
        """Write a file into a sandbox (side channel)."""
        async with self._sandbox_scope(sandbox_id):
            bridge = self._get_bridge(sandbox_id)
            await bridge.write_file(
                path, content, mkdir=mkdir, **({} if skill is None else {"skill": skill})
            )
        return FileWriteResponse(path=path, written=True)

    async def write_file_base64(
        self,
        sandbox_id: str,
        path: str,
        content_base64: str,
        mkdir: bool = True,
        skill: str | None = None,
    ) -> FileWriteResponse:
        """Write a binary file into a sandbox from base64 content (side channel)."""
        async with self._sandbox_scope(sandbox_id):
            bridge = self._get_bridge(sandbox_id)
            await bridge.write_file_base64(
                path, content_base64, mkdir=mkdir, **({} if skill is None else {"skill": skill})
            )
        return FileWriteResponse(path=path, written=True)

    async def get_skill_context(
        self,
        sandbox_id: str,
        max_files: int = 5,
        max_file_size: int = 8000,
    ) -> SkillContextResponse:
        """
        Build structured context for an LLM to understand the skill.
        Returns the skill manifest + key file contents + sandbox info.
        """
        async with self._sandbox_scope(sandbox_id) as sandbox:
            bridge = self._get_bridge(sandbox_id)
            skill = sandbox.skill
            if not skill:
                raise RuntimeError("Sandbox has no skill attached")

            file_contents = await bridge.read_key_skill_files(
                skill.scripts,
                entry_hint=skill.entry_hint,
                max_files=max_files,
                max_size=max_file_size,
            )

        sandbox_info = (
            f"Working directory: {_skill_dir(_skill_slot(skill.name))} (seen as /skill)\n"
            f"Network: {sandbox.config.network}\n"
            f"Memory limit: {sandbox.config.memory}\n"
            f"Image: {sandbox.config.image}\n"
            f"Python available: yes\n"
            "Background commands receive MANOR_SANDBOX_BRIDGE. Use its emit "
            "and wait subcommands for bounded structured Agent interaction.\n"
        )
        if skill.requirements_txt:
            sandbox_info += "Dependencies: installed from requirements.txt\n"

        return SkillContextResponse(
            sandbox_id=sandbox_id,
            skill=skill,
            file_contents=file_contents,
            sandbox_info=sandbox_info,
        )

    async def run_skill(
        self,
        skill_dir: str,
        env: dict[str, str],
        commands: list[str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_destroy: bool = True,
    ) -> SkillRunResponse:
        """
        Full end-to-end: create sandbox → exec commands → return results.

        If ``commands`` is empty, auto-detect entry point from skill manifest.
        """
        resp = await self.create_sandbox(
            skill_dir=skill_dir,
            env=env,
            allowed_sensitive_keys=allowed_sensitive_keys,
            config_overrides=config_overrides,
        )
        sandbox_id = resp.sandbox_id
        skill = resp.skill

        # Auto-detect commands if none provided
        if not commands:
            commands = self._auto_detect_commands(skill)

        steps: list[SkillRunStepResult] = []
        all_ok = True

        for cmd in commands:
            result = await self.exec_command(sandbox_id, cmd)
            steps.append(SkillRunStepResult(
                command=cmd,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            ))
            if result.exit_code != 0:
                all_ok = False
                break

        destroyed = False
        if auto_destroy:
            await self.destroy_sandbox(sandbox_id)
            destroyed = True

        return SkillRunResponse(
            sandbox_id=sandbox_id,
            skill=skill,
            success=all_ok,
            steps=steps,
            destroyed=destroyed,
        )

    async def load_skill_into_sandbox(
        self,
        sandbox_id: str,
        skill_name: str,
        files: dict[str, str],
        auto_install: bool = True,
        idle_threshold: int = 30,
        env: dict[str, str] | None = None,
        allowed_sensitive_keys: set[str] | None = None,
        baked_skill: tuple[str, str] | None = None,
    ) -> LoadSkillResponse:
        """
        Reload a sandbox with a new skill's files (reuse mode). All or nothing.

        ``baked_skill`` (name, digest) loads the image's copy of a built-in
        Skill instead of ``files``; an image without that exact digest answers
        ``BakedSkillUnavailable`` and leaves the sandbox untouched.

        The sandbox container keeps running; only the skill files and the
        skill env are replaced (the same sensitive-key policy as create
        applies). A busy sandbox answers ``SandboxBusy``. A load that failed
        after it touched the container leaves the sandbox quarantined; it is
        destroyed here, before the error reaches the caller, so its capacity
        is free and the next request for the id answers 404.
        """
        validate_skill_name(skill_name)
        baked = self._resolve_baked_skill(*baked_skill) if baked_skill is not None else None
        async with self._sandbox_scope(sandbox_id) as sandbox:
            sandbox.ensure_available()
            safe_env, blocked = sanitize_env_vars(dict(env or {}), allowed_sensitive_keys)
            # A load that fails after touching the container quarantines the
            # sandbox; the scope destroys it before the error is raised.
            async with self._operation_guard():
                new_skill = await sandbox.load_skill(
                    skill_name=skill_name,
                    files=files,
                    auto_install=auto_install,
                    idle_threshold=idle_threshold,
                    env=safe_env,
                    baked=baked,
                )
        return LoadSkillResponse(
            sandbox_id=sandbox_id,
            skill=new_skill,
            skill_dir=_skill_dir(_skill_slot(skill_name)),
            reused=True,
            env_blocked=blocked,
        )

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """Destroy a sandbox and remove it from the registry.

        Removal never waits for a running command and never depends on
        cancelling one: ``docker rm -f`` kills every process in the container.
        Only after the container is gone are execution records settled and
        capacity released; if removal fails the sandbox stays registered as
        ERROR and the reaper retries.
        """
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            return

        await sandbox.destroy()
        if self._sandboxes.get(sandbox_id) is not sandbox:
            return
        await self._settle_sandbox_executions(
            sandbox_id,
            ExecutionStatus.CANCELLED,
            "sandbox was destroyed",
        )

        active_lease = self._active_leases.get(sandbox_id)
        if active_lease:
            await active_lease.release()
        if self._sandboxes.get(sandbox_id) is not sandbox:
            return

        del self._sandboxes[sandbox_id]
        self._fs_bridges.pop(sandbox_id, None)
        for key in [key for key in self._execution_records if key[0] == sandbox_id]:
            self._execution_records.pop(key, None)
            self._execution_request_fingerprints.pop(key, None)
            self._execution_locks.pop(key, None)
            self._execution_tasks.pop(key, None)
            self._execution_cancel_requested.discard(key)
            self._execution_cancel_confirmed.discard(key)
            self._forget_execution_response_state(key)
        if active_lease is not None and self._active_leases.get(sandbox_id) is active_lease:
            del self._active_leases[sandbox_id]
        memory_ticket = self._memory_reservations.pop(sandbox_id, None)
        if memory_ticket is not None:
            memory_ticket.release()
        await self._local_active_capacity.release()
        await self._forget_idempotent_sandbox(sandbox_id)

    async def _settle_sandbox_executions(
        self,
        sandbox_id: str,
        status: ExecutionStatus,
        error: str,
    ) -> None:
        """Finalise every open execution of a sandbox whose container is gone.

        Their tasks end by themselves once Docker reports the exec over; a
        task still running after a short grace is cancelled. The current task
        (a background execution reclaiming its own sandbox) is never awaited.
        """
        current = asyncio.current_task()
        tasks = [
            task
            for (record_sandbox_id, _execution_id), task in list(self._execution_tasks.items())
            if record_sandbox_id == sandbox_id and task is not current and not task.done()
        ]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=_DESTROY_TASK_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=_DESTROY_TASK_GRACE_SECONDS)
        for key, record in list(self._execution_records.items()):
            if key[0] == sandbox_id:
                self._settle_record(record, status, error=error)

    async def _destroy_quietly(self, sandbox_id: str, sandbox: object, *, reason: str) -> None:
        """Destroy a sandbox that can no longer serve; a failure is left to the reaper."""
        if self._sandboxes.get(sandbox_id) is not sandbox:
            return
        logger.warning("Destroying sandbox %s: %s", sandbox_id, reason)
        try:
            await self.destroy_sandbox(sandbox_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Destroying sandbox %s failed; the reaper retries", sandbox_id, exc_info=True
            )

    def _reclaim_lost(self, sandbox_id: str, sandbox: object) -> asyncio.Task[None]:
        """Reclaim a sandbox whose container Docker reports gone (single flight).

        In-flight executions are failed with a clear error, then the sandbox
        is destroyed (``docker rm -f`` of a missing container succeeds), which
        releases its capacity and drops it from the registry. Until that
        completes, ``_get_sandbox`` already answers 404 for a lost sandbox.
        """
        existing = self._reclaim_tasks.get(sandbox_id)
        if existing is not None and not existing.done():
            return existing

        async def _reclaim() -> None:
            try:
                reason = getattr(sandbox, "_lost_reason", "") or "container lost"
                await self._settle_sandbox_executions(
                    sandbox_id,
                    ExecutionStatus.FAILED,
                    f"Sandbox container was lost: {reason}",
                )
                await self._destroy_quietly(sandbox_id, sandbox, reason=f"lost: {reason}")
            finally:
                if self._reclaim_tasks.get(sandbox_id) is asyncio.current_task():
                    self._reclaim_tasks.pop(sandbox_id, None)

        task = asyncio.create_task(_reclaim(), name=f"sandbox-reclaim-{sandbox_id}")
        self._reclaim_tasks[sandbox_id] = task
        return task

    def _visible_sandboxes(self) -> list[tuple[str, DockerSandbox]]:
        return [
            (sid, sbx)
            for sid, sbx in self._sandboxes.items()
            if not getattr(sbx, "is_lost", False)
        ]

    def list_sandboxes(self) -> list[SandboxInfo]:
        """List all active sandboxes (a lost sandbox is already gone)."""
        return [self._sandbox_info(sid, sbx) for sid, sbx in self._visible_sandboxes()]

    @staticmethod
    def _sandbox_info(sandbox_id: str, sbx: DockerSandbox) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id=sandbox_id,
            container_name=sbx.container_name,
            status=sbx.status,
            skill_name=sbx.skill.name if sbx.skill else "unknown",
            workdir=sbx.config.workdir,
            created_at=sbx.created_at,
            last_used_at=sbx.last_used_at,
            config=sbx.config,
            active_command=sbx.active_command,
            active_execution_id=getattr(sbx, "active_execution_id", None),
            active_execution_ids=list(getattr(sbx, "active_execution_ids", None) or []),
            max_concurrent_executions=getattr(sbx, "max_concurrent_executions", None),
            queued_execution_ids=list(getattr(sbx, "queued_execution_ids", None) or []),
            skills=list(getattr(sbx, "skills", None) or []),
            expires_at=sbx.expires_at,
        )

    def get_sandbox_status(self, sandbox_id: str) -> SandboxInfo:
        return self._sandbox_info(sandbox_id, self._get_sandbox(sandbox_id))

    async def probe_sandbox_status(self, sandbox_id: str) -> SandboxInfo:
        """Status for the API: confirms with Docker that the container still runs.

        A container that is gone turns into 404 here (and is reclaimed), so a
        caller probing after a failure learns the truth. A Docker hiccup while
        probing reports the registry state rather than failing the probe.
        """
        async with self._sandbox_scope(sandbox_id) as sandbox:
            try:
                await sandbox.verify_alive()
            except DockerUnavailable:
                logger.warning(
                    "Could not verify sandbox %s with Docker; reporting registry state",
                    sandbox_id,
                    exc_info=True,
                )
            return self.get_sandbox_status(sandbox_id)

    def touch_sandbox(self, sandbox_id: str) -> SandboxInfo:
        """Refresh an active sandbox's idle lease and return its current status."""
        self._get_sandbox(sandbox_id).touch()
        return self.get_sandbox_status(sandbox_id)

    # ── internal helpers ──

    @asynccontextmanager
    async def _sandbox_scope(
        self,
        sandbox_id: str,
        *,
        wait_for_reclaim: bool = True,
    ) -> AsyncIterator[DockerSandbox]:
        """Resolve a sandbox for one operation; reclaim it if Docker lost it.

        Every runner operation on a sandbox runs inside this scope. A
        ``SandboxLost`` from anywhere inside (an exec, a file read, an event
        read) reclaims the sandbox — registry, capacity, open executions —
        and still reaches the caller as a 404. An operation that fails and
        leaves the sandbox quarantined (a load that touched the container, a
        timed-out command that could not be killed) destroys it before the
        error reaches the caller, so no unusable sandbox keeps its capacity.
        """
        sandbox = self._get_sandbox(sandbox_id)
        quarantined_before = getattr(sandbox, "status", None) == SandboxStatus.ERROR
        try:
            yield sandbox
        except SandboxLost:
            task = self._reclaim_lost(sandbox_id, sandbox)
            if wait_for_reclaim:
                await self._wait_for_task_terminal(task)
            raise
        except BaseException as exc:
            if (
                not quarantined_before
                and getattr(sandbox, "status", None) == SandboxStatus.ERROR
                and not getattr(sandbox, "is_lost", False)
            ):
                if isinstance(exc, RunnerError):
                    exc.extra["sandbox_destroyed"] = True
                task = asyncio.create_task(
                    self._destroy_quietly(sandbox_id, sandbox, reason=f"quarantined: {exc}")
                )
                if wait_for_reclaim:
                    await self._wait_for_task_terminal(task)
            raise

    async def _reconcile_managed_containers(self) -> None:
        self._orphan_cleanup_failures = []
        try:
            from .docker_backend import sweep_stale_skill_env_files

            # Startup force-removes every managed container, so no exec can be in
            # flight: every leftover secret file is an orphan, whatever its age.
            swept = await asyncio.to_thread(sweep_stale_skill_env_files, max_age_seconds=0)
            if swept:
                logger.info("Removed %d stale sandbox env files", swept)
        except Exception:
            logger.exception("Failed to sweep stale sandbox env files during startup")
        try:
            container_ids = await asyncio.to_thread(
                list_managed_sandbox_containers, app_config.SANDBOX_SERVICE_SCOPE,
            )
        except Exception:
            self._orphan_cleanup_failures.append("list-managed-containers")
            logger.exception("Failed to list managed sandbox containers during startup")
            return

        for container_id in container_ids:
            try:
                await asyncio.to_thread(remove_managed_sandbox_container, container_id)
                logger.info("Removed orphan sandbox container %s", container_id[:12])
            except Exception:
                self._orphan_cleanup_failures.append(container_id[:12])
                logger.exception(
                    "Failed to remove orphan sandbox container %s",
                    container_id[:12],
                )
        await self._sweep_dangling_volumes()

    @asynccontextmanager
    async def _operation_guard(self) -> AsyncIterator[None]:
        lease = await self._gate.acquire_execute()
        async with lease:
            self._executing_leases.add(lease)
            try:
                yield
            finally:
                self._executing_leases.discard(lease)

    async def _setup_sandbox(
        self,
        sandbox: DockerSandbox,
        skill: SkillManifest,
        env: dict[str, str],
        auto_install: bool,
        baked: BakedSkill | None = None,
    ) -> None:
        async with self._operation_guard():
            await sandbox.setup(skill, env, auto_install=auto_install, baked=baked)

    @staticmethod
    def _sandbox_id_for_create(idempotency_key: str | None) -> str:
        if idempotency_key:
            return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
        return str(uuid.uuid4())[:12]

    async def _prepare_idempotent_create(
        self,
        key: str,
    ) -> tuple[
        CreateSandboxResponse | None,
        asyncio.Future[CreateSandboxResponse] | None,
        bool,
    ]:
        wanted_exit = None

        async with self._idempotency_lock:
            if self._shutting_down:
                raise RunnerShuttingDown("SkillRunner is shutting down")
            completed = self._idempotency_completed.get(key)
            if completed is not None:
                sandbox = self._sandboxes.get(completed.sandbox_id)
                if sandbox is not None:
                    if getattr(sandbox, "is_lost", False):
                        # Its id is derived from the key, so a new create must
                        # wait until the lost one is reclaimed: retry, not 404.
                        raise SandboxUnavailable(
                            "The sandbox for this idempotency key lost its container "
                            "and is being reclaimed; retry shortly."
                        )
                    sandbox.ensure_available()
                    return completed, None, False
                self._idempotency_completed.pop(key, None)
                self._sandbox_idempotency_keys.pop(completed.sandbox_id, None)

            inflight = self._idempotency_inflight.get(key)
            if inflight is not None:
                future, inflight_exit = inflight
                return None, future, False

            future = asyncio.get_running_loop().create_future()
            self._idempotency_inflight[key] = (future, wanted_exit)
            return None, future, True

    async def _complete_idempotent_create(
        self,
        key: str,
        future: asyncio.Future[CreateSandboxResponse],
        result: CreateSandboxResponse,
    ) -> None:
        async with self._idempotency_lock:
            if self._idempotency_inflight.get(key, (None,))[0] is not future:
                return
            self._idempotency_inflight.pop(key, None)
            self._idempotency_completed[key] = result
            self._sandbox_idempotency_keys[result.sandbox_id] = key
            if not future.done():
                future.set_result(result)

    async def _fail_idempotent_create(
        self,
        key: str,
        future: asyncio.Future[CreateSandboxResponse],
        exc: BaseException,
    ) -> None:
        async with self._idempotency_lock:
            if self._idempotency_inflight.get(key, (None,))[0] is not future:
                return
            self._idempotency_inflight.pop(key, None)
            if future.done():
                return
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
                future.exception()

    async def _forget_idempotent_sandbox(self, sandbox_id: str) -> None:
        async with self._idempotency_lock:
            key = self._sandbox_idempotency_keys.pop(sandbox_id, None)
            if key is None:
                return
            completed = self._idempotency_completed.get(key)
            if completed is not None and completed.sandbox_id == sandbox_id:
                self._idempotency_completed.pop(key, None)

    def _get_sandbox(self, sandbox_id: str) -> DockerSandbox:
        sandbox = self._sandboxes.get(sandbox_id)
        if not sandbox:
            raise SandboxGone(f"Sandbox not found: {sandbox_id}")
        if getattr(sandbox, "is_lost", False):
            # Still registered only until its reclaim finishes (or is retried
            # by the reaper); to callers it is already gone.
            raise SandboxLost(f"Sandbox not found: {sandbox_id} (its container is gone)")
        return sandbox

    def _get_bridge(self, sandbox_id: str) -> FsBridge:
        self._get_sandbox(sandbox_id).ensure_available()
        bridge = self._fs_bridges.get(sandbox_id)
        if not bridge:
            raise SandboxGone(f"Sandbox not found: {sandbox_id}")
        return bridge

    async def _begin_create(self) -> asyncio.Task[object]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Sandbox creation requires an active asyncio task")
        async with self._create_lock:
            if self._shutting_down:
                raise RunnerShuttingDown("SkillRunner is shutting down")
            self._reject_if_draining()
            self._create_tasks.add(task)
        return task

    async def _reserve_capacity(self, memory_bytes: int) -> MemoryReservation:
        """Take one sandbox slot and ``memory_bytes`` of the budget, or raise ``RunnerFull``.

        Both are taken together or not at all. When either is short, idle
        sandboxes past their timeout are reaped once and admission retried.
        """

        reason = "max_sandboxes"
        for attempt in range(2):
            if await self._local_active_capacity.try_acquire():
                ticket = self._memory.try_reserve(memory_bytes)
                if ticket is not None:
                    return ticket
                await self._local_active_capacity.release()
                reason = "memory"
            else:
                reason = "max_sandboxes"
            if attempt == 0:
                await self._prune_idle()
        extra = self._capacity_extra(memory_bytes, reason)
        if reason == "memory":
            message = (
                f"Sandbox runner memory is full: the container needs "
                f"{extra['required_memory_mb']}MiB and {extra['available_memory_mb']}MiB of the "
                f"{extra['memory_budget_mb']}MiB budget is free. Please retry later."
            )
        else:
            message = (
                f"Max active sandbox instance limit reached ({app_config.MAX_SANDBOXES}). "
                "Please retry later."
            )
        logger.warning("Sandbox admission refused (%s): %s", reason, message)
        raise RunnerFull(message, extra=extra)

    def _capacity_extra(self, memory_bytes: int, reason: str) -> dict[str, object]:
        memory = self.memory_status()
        return {
            "reason": reason,
            "required_memory_mb": to_mib(memory_bytes),
            "available_memory_mb": memory["memory_available_mb"],
            "largest_allocatable_mb": memory["largest_allocatable_mb"],
            "memory_budget_mb": memory["memory_budget_mb"],
            "max_container_memory_mb": memory["max_container_memory_mb"],
            "active_sandboxes": self._local_active_capacity.reserved,
            "max_active": self._local_active_capacity.limit,
        }

    async def _cleanup_failed_create(
        self,
        sandbox: DockerSandbox | None,
        active_lease: object | None,
        memory_ticket: MemoryReservation | None = None,
    ) -> None:
        if sandbox is not None:
            try:
                await sandbox.destroy()
            except BaseException:
                self._retain_failed_create_cleanup(sandbox, active_lease, memory_ticket)
                logger.warning(
                    "Partial sandbox cleanup failed; retaining sandbox %s",
                    sandbox.sandbox_id,
                    exc_info=True,
                )
                return
        if active_lease is not None:
            try:
                await active_lease.release()
            except BaseException:
                if sandbox is not None:
                    self._retain_failed_create_cleanup(sandbox, active_lease, memory_ticket)
                logger.warning("Sandbox active lease cleanup failed", exc_info=True)
                return
        if memory_ticket is not None:
            memory_ticket.release()
        await self._local_active_capacity.release()

    def _retain_failed_create_cleanup(
        self,
        sandbox: DockerSandbox,
        active_lease: object | None,
        memory_ticket: MemoryReservation | None = None,
    ) -> None:
        # The container may still exist: it keeps its slot and its memory
        # until the reaper's destroy succeeds.
        sandbox.status = SandboxStatus.ERROR
        self._sandboxes[sandbox.sandbox_id] = sandbox
        if active_lease is not None:
            self._active_leases[sandbox.sandbox_id] = active_lease
        if memory_ticket is not None:
            self._memory_reservations[sandbox.sandbox_id] = memory_ticket

    @staticmethod
    async def _wait_for_task_terminal(task: asyncio.Task[object]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                pass
        try:
            task.result()
        except BaseException:
            pass

    def _resolve_config(self, overrides: ContainerConfig | None = None) -> ContainerConfig:
        """Runner defaults plus the caller's allowlisted, bounded overrides."""

        # CPUs and memory are fitted to the host by ``_fit_to_host`` (the
        # measured Docker host, or SANDBOX_HOST_CPUS), which reports what it
        # clamped.
        cfg = apply_overrides(self._default_config(), overrides)
        self._validate_config(cfg)
        return cfg

    def _memory_limits(self, *, docker: tuple[int, float] | None = None) -> MemoryLimits:
        host = detect_host_resources(
            memory_override=app_config.HOST_MEMORY,
            cpus_override=app_config.HOST_CPUS,
            docker=docker,
        )
        return compute_limits(
            host,
            overcommit=app_config.MEMORY_OVERCOMMIT,
            reserve=app_config.MEMORY_RESERVE,
            budget_override=app_config.MEMORY_BUDGET,
            max_container_override=app_config.MAX_CONTAINER_MEMORY,
        )

    async def _refresh_host_resources(self) -> None:
        """Measure the Docker daemon's host (where containers run) off the loop."""

        try:
            measured = await asyncio.to_thread(docker_host_resources)
            if measured is None:
                logger.warning("Docker did not report its host size; keeping the local estimate")
                return
            limits = self._memory_limits(docker=measured)
        except Exception:
            logger.warning("Could not measure the Docker host; keeping the local estimate", exc_info=True)
            return
        self._memory.update_limits(limits)

    def _memory_limit_bytes(self, cfg: ContainerConfig) -> int:
        """The memory a container holds in the budget: its ``--memory`` limit.

        A container without a parsable limit (only possible through the
        runner's own SANDBOX_MEMORY) is charged the per-container maximum.
        """

        try:
            if str(cfg.memory or "").strip():
                return parse_size(cfg.memory)
        except ValueError:
            pass
        return self._memory.limits.max_container

    def _fit_to_host(
        self,
        cfg: ContainerConfig,
    ) -> list[str]:
        """Clamp a container to what this host can give; return the warnings.

        Memory above ``max_container_memory`` (host memory minus the reserve)
        is clamped instead of rejected: callers size a request for the job
        (video asks 4g everywhere), not for one runner, and the workload that
        asks for more (the video runtime) sizes its render workers from the
        cgroup limit it actually gets. Rejecting would make the request
        unschedulable on this runner forever, and waiting cannot help since
        the budget never grows past the host. CPUs above the host's count are
        clamped too: Docker itself refuses ``--cpus`` above it.
        """

        warnings: list[str] = []
        limits = self._memory.limits
        max_container = limits.max_container
        note = ""
        if not str(cfg.memory or "").strip() or self._memory_limit_bytes(cfg) > max_container:
            clamped = format_size(max_container)
            warnings.append(
                f"memory {cfg.memory or '(unlimited)'} exceeds this runner's per-container "
                f"maximum; clamped to {clamped} (host {to_mib(limits.host_memory)}MiB, "
                f"reserve {to_mib(limits.reserve)}MiB{note})"
            )
            cfg.memory = clamped
        host_cpus = float(limits.host_cpus or 0)
        if host_cpus > 0 and float(cfg.cpus or 0) > host_cpus:
            warnings.append(
                f"cpus {cfg.cpus:g} exceeds this runner's {host_cpus:g} CPUs; clamped to {host_cpus:g}"
            )
            cfg.cpus = host_cpus
        for warning in warnings:
            logger.warning("Sandbox config adjusted: %s", warning)
        return warnings

    @staticmethod
    def _validate_config(cfg: ContainerConfig) -> None:
        # Docker's "none" mode removes network access. It is valid even when
        # connected sandboxes are restricted to a dedicated bridge.
        allowed_networks = set(app_config.SANDBOX_ALLOWED_NETWORKS) | {"none"}
        if cfg.network not in allowed_networks:
            raise SecurityError(
                f"Docker network '{cfg.network}' is not allowed; "
                f"expected one of {sorted(allowed_networks)}."
            )
        for volume in cfg.volumes or []:
            parts = volume.split(":")
            if len(parts) not in (2, 3):
                raise SecurityError(
                    "Volume mounts must use 'host_path:container_path[:mode]' syntax."
                )
            host_path, container_path = parts[0], parts[1]
            mode = parts[2] if len(parts) == 3 else ""
            validate_host_path(host_path)
            validate_container_path(container_path)
            if mode and mode not in {"ro", "rw"}:
                raise SecurityError(
                    f"Unsupported volume mode '{mode}'. Use 'ro' or 'rw'."
                )

    def _default_config(self) -> ContainerConfig:
        return ContainerConfig(
            image=app_config.SANDBOX_IMAGE,
            network=app_config.SANDBOX_NETWORK,
            dns=app_config.SANDBOX_DNS_SERVERS,
            memory=app_config.SANDBOX_MEMORY,
            cpus=app_config.SANDBOX_CPUS,
            pids_limit=app_config.SANDBOX_PIDS_LIMIT,
            read_only_root=app_config.SANDBOX_READ_ONLY_ROOT,
            container_prefix=app_config.SANDBOX_CONTAINER_PREFIX,
            service_scope=app_config.SANDBOX_SERVICE_SCOPE,
            workdir=app_config.SANDBOX_WORKDIR,
            workdir_tmpfs_size=app_config.SANDBOX_WORKDIR_TMPFS_SIZE,
            workspace_tmpfs_size=app_config.SANDBOX_WORKSPACE_TMPFS_SIZE,
            tmp_tmpfs_size=app_config.SANDBOX_TMP_TMPFS_SIZE,
            home_tmpfs_size=app_config.SANDBOX_HOME_TMPFS_SIZE,
            max_concurrent_executions=max(1, app_config.MAX_CONCURRENT_EXECUTIONS),
            install_timeout=app_config.INSTALL_TIMEOUT,
            exec_timeout=app_config.EXEC_TIMEOUT,
        )

    @staticmethod
    def _auto_detect_commands(skill: SkillManifest) -> list[str]:
        """Infer which command(s) to run based on the skill manifest."""
        if skill.entry_hint:
            ext = skill.entry_hint.rsplit(".", 1)[-1] if "." in skill.entry_hint else ""
            if ext == "py":
                return [f"python {skill.entry_hint}"]
            if ext in ("sh", "bash"):
                return [f"bash {skill.entry_hint}"]
            if ext in ("js", "ts"):
                return [f"node {skill.entry_hint}"]
            return [f"./{skill.entry_hint}"]

        # Fallback: try common patterns
        for script in skill.scripts:
            if script.endswith(".py"):
                return [f"python {script}"]
        return []

    @staticmethod
    def _default_busy_budget_seconds() -> float:
        """Upper bound for a claim that did not record its own deadline."""
        return (
            max(_MAX_COMMAND_TIMEOUT_SECONDS, 3 * app_config.INSTALL_TIMEOUT + 180)
            + BUSY_MARGIN_SECONDS
            + app_config.GATE_WAIT_TIMEOUT_SECONDS
        )

    def _reap_reason(self, sandbox: object, now: float) -> str | None:
        """Why the reaper must reclaim this sandbox now, or None to keep it.

        Every state is covered, so no sandbox can hold capacity in a state no
        background job reclaims:

        * lost container                                  -> reclaim
        * ERROR (quarantined, or a destroy that failed)    -> destroy, retried every tick
        * DESTROYED but still registered (interrupted)     -> finish the destroy
        * READY idle past IDLE_TIMEOUT                      -> destroy
        * CREATING/RUNNING/INSTALLING/EXECUTING/DESTROYING
          past its deadline (own timeout plus margin)      -> stuck, destroy
        """
        if getattr(sandbox, "is_lost", False):
            return "lost"
        status = getattr(sandbox, "status", None)
        if status == SandboxStatus.ERROR:
            return "error"
        if status == SandboxStatus.DESTROYED:
            return "destroy-incomplete"
        if status == SandboxStatus.READY and getattr(sandbox, "has_pending_work", False):
            # A Skill loading next to no command: busy until its deadline.
            deadline = getattr(sandbox, "busy_deadline", None)
            return "stuck" if deadline is not None and now > deadline else None
        if status == SandboxStatus.READY:
            idle = now - (getattr(sandbox, "last_used_at", 0.0) or 0.0)
            if idle > app_config.IDLE_TIMEOUT_SECONDS:
                return "idle"
            # A user's sandbox may never sit idle long enough to be reaped:
            # recycle it once it is old, between commands (nothing running or
            # waiting, unused for a moment). Its files are ephemeral.
            lifetime = app_config.MAX_LIFETIME_SECONDS
            created = getattr(sandbox, "created_at", 0.0) or 0.0
            if lifetime > 0 and created and now - created > lifetime and idle > _RECYCLE_QUIET_SECONDS:
                return "max-lifetime"
            return None
        if status in (
            SandboxStatus.CREATING,
            SandboxStatus.RUNNING,
            SandboxStatus.INSTALLING,
            SandboxStatus.EXECUTING,
            SandboxStatus.DESTROYING,
        ):
            deadline = getattr(sandbox, "busy_deadline", None)
            if deadline is None:
                deadline = (
                    getattr(sandbox, "last_used_at", 0.0) or 0.0
                ) + self._default_busy_budget_seconds()
            return "stuck" if now > deadline else None
        return None

    async def _prune_idle(self) -> None:
        """Reap every sandbox that can no longer serve (see ``_reap_reason``)."""
        now = time.time()
        candidates = [
            (sid, sbx, reason)
            for sid, sbx in list(self._sandboxes.items())
            if (reason := self._reap_reason(sbx, now)) is not None
        ]
        for sid, sbx, reason in candidates:
            # A previous destroy yields: a later candidate may have been
            # touched, become busy, or been replaced before we reach it.
            if (
                self._sandboxes.get(sid) is not sbx
                or self._reap_reason(sbx, time.time()) is None
            ):
                continue
            logger.info("Reaping sandbox %s (%s)", sid, reason)
            await self._destroy_quietly(sid, sbx, reason=f"reaped: {reason}")

    async def _sweep_lost_containers(self) -> None:
        """Find registered sandboxes whose container Docker no longer runs.

        One ``docker ps`` per tick; a candidate is confirmed with an inspect
        before it is reclaimed, so a container created after the listing is
        never mistaken for a lost one.
        """
        live = [
            (sid, sbx)
            for sid, sbx in list(self._sandboxes.items())
            if getattr(sbx, "status", None) in DockerSandbox._LIVE_STATES
            and getattr(sbx, "container_name", "")
            and not getattr(sbx, "is_lost", False)
        ]
        if not live:
            return
        try:
            running = await asyncio.to_thread(
                list_running_managed_container_names, app_config.SANDBOX_SERVICE_SCOPE
            )
        except Exception:
            logger.warning("Could not list running sandbox containers", exc_info=True)
            return
        for sid, sbx in live:
            # A sandbox of several containers is lost as a whole when one is gone.
            names = getattr(sbx, "container_names", None) or [sbx.container_name]
            if all(name in running for name in names) or self._sandboxes.get(sid) is not sbx:
                continue
            try:
                await sbx.verify_alive()
            except SandboxLost:
                await self._wait_for_task_terminal(self._reclaim_lost(sid, sbx))
            except Exception:
                logger.warning("Could not verify sandbox %s", sid, exc_info=True)

    async def _enforce_disk_quotas(self) -> None:
        """One disk watchdog tick: every live sandbox whose disks are due.

        The policy is ``sandbox.disk_quota.enforce_disk_quota`` (one place,
        for every disk): stop the offending commands, refuse their work
        area's new ones, and -- only when a container is unusable and idle --
        recycle it here.
        """

        due = []
        for sid, sbx in list(self._sandboxes.items()):
            if (
                getattr(sbx, "is_lost", False)
                or getattr(sbx, "status", None) not in DockerSandbox._LIVE_STATES
                or not hasattr(sbx, "disk_roots")
            ):
                continue
            roots = due_disk_roots(sbx)
            if roots:
                due.append((sid, sbx, roots))

        async def one(sid: str, sbx: DockerSandbox, roots) -> None:
            try:
                plan = await enforce_disk_quota(sbx, roots=roots)
            except SandboxLost:
                await self._wait_for_task_terminal(self._reclaim_lost(sid, sbx))
                return
            except Exception:
                logger.warning("Disk quota of sandbox %s not enforced this tick", sid, exc_info=True)
                return
            if plan.recycle_reason and self._sandboxes.get(sid) is sbx:
                await self._wait_for_task_terminal(self._reclaim_lost(sid, sbx))

        if due:
            await asyncio.gather(*(one(sid, sbx, roots) for sid, sbx, roots in due))

    async def _disk_watchdog_loop(self) -> None:
        """Background loop: the disk quota of every sandbox (``_enforce_disk_quotas``)."""
        tick = max(0.2, min(1.0, float(app_config.SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS)))
        while True:
            try:
                await asyncio.sleep(tick)
                await self._enforce_disk_quotas()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in disk watchdog loop")
    async def _sweep_dangling_volumes(self) -> None:
        """Delete local-disk volumes whose container is gone (see docker_backend)."""
        try:
            removed = await asyncio.to_thread(
                remove_dangling_sandbox_volumes, app_config.SANDBOX_SERVICE_SCOPE,
            )
        except Exception:
            logger.warning("Could not sweep dangling sandbox volumes", exc_info=True)
            return
        if removed:
            logger.info("Removed %d dangling sandbox volumes", removed)

    async def _prune_loop(self) -> None:
        """Background loop: renew leases, reclaim lost containers, reap."""
        while True:
            try:
                await asyncio.sleep(_REAP_INTERVAL_SECONDS)
                await self._renew_active_leases()
                await self._sweep_lost_containers()
                await self._renew_executing_leases()
                await self._prune_idle()
                await self._sweep_dangling_volumes()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in prune loop")

    async def _renew_active_leases(self) -> None:
        """Keep Redis active sandbox slots fresh while containers are alive."""
        for sandbox_id, lease in list(self._active_leases.items()):
            renew = getattr(lease, "renew", None)
            if not renew:
                continue
            try:
                renewed = await renew()
                if not renewed:
                    await self._quarantine_lost_active_lease(sandbox_id, lease)
            except Exception:
                logger.debug("Active sandbox lease renew failed for %s", sandbox_id, exc_info=True)

    async def _renew_executing_leases(self) -> None:
        """Keep Redis executing slots counted for the whole life of long execs.

        The gate prunes zset members older than its TTL, so without renewal an
        exec longer than the TTL would silently stop counting against the
        global executing limit. A lost slot cannot be recovered mid-command, so
        it is only logged.
        """
        for lease in list(self._executing_leases):
            renew = getattr(lease, "renew", None)
            if not renew:
                continue
            try:
                if not await renew():
                    logger.warning("Executing sandbox lease was lost while the operation is still running")
            except Exception:
                logger.debug("Executing sandbox lease renew failed", exc_info=True)

    async def _quarantine_lost_active_lease(self, sandbox_id: str, lease: object) -> None:
        if self._active_leases.get(sandbox_id) is not lease:
            return
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            return

        sandbox.status = SandboxStatus.ERROR
        logger.error(
            "Active sandbox lease was lost; quarantining sandbox %s",
            sandbox_id,
        )
        try:
            await self.destroy_sandbox(sandbox_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            sandbox.status = SandboxStatus.ERROR
            logger.exception(
                "Failed to destroy sandbox %s after active lease loss; retaining capacity",
                sandbox_id,
            )
