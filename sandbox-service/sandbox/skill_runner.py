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
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sandbox_config import config as app_config

from .docker_backend import (
    DockerSandbox,
    _image_exists_locally,
    list_managed_sandbox_containers,
    remove_managed_sandbox_container,
)
from .fs_bridge import FsBridge
from .concurrency import (
    LocalActiveCapacity,
    SandboxConcurrencyExceeded,
    SandboxConcurrencyGate,
    SandboxGateConfig,
)
from .credential_refs import validate_sandbox_credential_ref
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
)

logger = logging.getLogger(__name__)

_EXECUTION_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_EXECUTION_EVENTS = 100
_MAX_EXECUTION_EVENT_BYTES = 8192
_CREDENTIAL_RESPONSE_REFERENCE_KEYS = {
    "credential_ref",
    "integration_account_id",
    "provider",
}


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
        self._active_leases: dict[str, object] = {}
        self._local_active_capacity = LocalActiveCapacity(app_config.MAX_SANDBOXES)
        self._create_lock = asyncio.Lock()
        self._create_tasks: set[asyncio.Task[object]] = set()
        self._shutting_down = False
        self._idempotency_lock = asyncio.Lock()
        self._idempotency_inflight: dict[
            str, asyncio.Future[CreateSandboxResponse]
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
        self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info("SkillRunner started (max sandboxes=%d)", app_config.MAX_SANDBOXES)

    async def shutdown(self) -> None:
        """Called on app shutdown — destroy all sandboxes."""
        async with self._create_lock:
            self._shutting_down = True
            create_tasks = list(self._create_tasks)
        if self._prune_task:
            self._prune_task.cancel()
            await asyncio.gather(self._prune_task, return_exceptions=True)
            self._prune_task = None
        if create_tasks:
            await asyncio.gather(*create_tasks, return_exceptions=True)
        tasks = [self.destroy_sandbox(sandbox_id) for sandbox_id in list(self._sandboxes)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._sandboxes:
            logger.warning("SkillRunner shut down with %d sandboxes still registered", len(self._sandboxes))
        else:
            logger.info("SkillRunner shut down, all sandboxes destroyed")

    # ── public API ──

    def scan_skill(self, skill_dir: str) -> SkillManifest:
        """Scan a skill directory and return its manifest."""
        return self._scanner.scan(skill_dir)

    def runtime_status(self) -> dict[str, object]:
        """Return sandbox runtime readiness without creating a container."""

        image = app_config.SANDBOX_IMAGE
        configured_execute_limit = self._gate.config.instance_max_executing
        status: dict[str, object] = {
            "sandbox_image": image,
            "active_sandboxes": len(self._sandboxes),
            "reserved_active": self._local_active_capacity.reserved,
            "max_active": self._local_active_capacity.limit,
            "executing": self._gate.executing,
            "max_executing": (
                configured_execute_limit
                if configured_execute_limit > 0
                else self._local_active_capacity.limit
            ),
            "orphan_cleanup_failures": list(self._orphan_cleanup_failures),
        }
        try:
            status["sandbox_image_available"] = _image_exists_locally(image)
        except Exception as exc:
            status["sandbox_image_available"] = False
            status["sandbox_image_error"] = str(exc)
        return status

    async def create_sandbox(
        self,
        skill_dir: str,
        env: dict[str, str],
        allowed_sensitive_keys: set[str] | None = None,
        config_overrides: ContainerConfig | None = None,
        auto_install: bool = True,
        idempotency_key: str | None = None,
        skill_name_override: str | None = None,
    ) -> CreateSandboxResponse:
        """Create a sandbox for a skill, inject files, install deps."""
        # Validate the host path
        validate_host_path(skill_dir)

        # Scan skill
        skill = self._scanner.scan(skill_dir)
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
            )

        completed, future, is_leader = await self._prepare_idempotent_create(key)
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
    ) -> CreateSandboxResponse:
        """Perform one admitted create attempt without idempotency coordination."""

        create_task = await self._begin_create()
        try:
            await self._reserve_local_active()
            sandbox: DockerSandbox | None = None
            active_lease: object | None = None
            setup_task: asyncio.Task[None] | None = None
            try:
                # Sanitize env vars
                safe_env, blocked = sanitize_env_vars(env, allowed_sensitive_keys)

                # Resolve config. Overrides are partial by design: the API caller can
                # add bind mounts without replacing service-owned defaults such as
                # image, memory, workdir, or timeouts.
                cfg = self._resolve_config(config_overrides)

                sandbox = DockerSandbox(sandbox_id, cfg)
                active_lease = await self._gate.acquire_active()
                setup_task = asyncio.create_task(
                    self._setup_sandbox(sandbox, skill, safe_env, auto_install)
                )
                await asyncio.shield(setup_task)
                bridge = FsBridge(sandbox)
                response = CreateSandboxResponse(
                    sandbox_id=sandbox_id,
                    container_name=sandbox.container_name,
                    status=sandbox.status,
                    skill=skill,
                    workdir=cfg.workdir,
                    env_blocked=blocked,
                )

                self._sandboxes[sandbox_id] = sandbox
                self._fs_bridges[sandbox_id] = bridge
                self._active_leases[sandbox_id] = active_lease
                return response
            except BaseException:
                if setup_task is not None:
                    await self._wait_for_task_terminal(setup_task)
                cleanup_task = asyncio.create_task(
                    self._cleanup_failed_create(sandbox, active_lease)
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
        from pathlib import Path

        if not files:
            raise ValueError("files dict must not be empty")

        tmp_dir = tempfile.mkdtemp(prefix=f"sbx-{skill_name}-")
        try:
            for rel_path, content in files.items():
                safe_rel = rel_path.replace("\\", "/").strip().lstrip("/")
                if not safe_rel or ".." in safe_rel:
                    continue
                full_path = Path(tmp_dir) / safe_rel
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")

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
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def exec_command(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
        execution_id: str | None = None,
        retain_mailbox: bool = False,
    ) -> ExecResponse:
        """Execute a command in an existing sandbox."""
        sandbox = self._get_sandbox(sandbox_id)
        sandbox.ensure_available()
        resolved_execution_id = execution_id or uuid.uuid4().hex
        async with self._operation_guard():
            return await sandbox.exec(
                command,
                timeout=timeout,
                workdir=workdir,
                execution_id=resolved_execution_id,
                interactive=retain_mailbox,
            )

    async def start_execution(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: str | None = None,
        execution_id: str | None = None,
    ) -> ExecutionStatusResponse:
        """Schedule a command and return before its process completes."""

        sandbox = self._get_sandbox(sandbox_id)
        sandbox.ensure_available()
        resolved_execution_id = execution_id or uuid.uuid4().hex
        key = (sandbox_id, resolved_execution_id)
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "command": command,
                    "timeout": timeout,
                    "workdir": workdir,
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
                    raise ValueError(
                        "execution_id already exists with a different request"
                    )
                return existing.model_copy(deep=True)
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
                ),
                name=f"sandbox-exec-{sandbox_id}-{resolved_execution_id}",
            )
            self._execution_tasks[key] = task
            result = record.model_copy(deep=True)
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
    ) -> None:
        sandbox_id, execution_id = key
        record = self._execution_records[key]
        execution_lock = self._execution_locks[key]
        async with execution_lock:
            record.status = ExecutionStatus.RUNNING
            record.started_at = time.time()
            if key in self._execution_cancel_requested:
                record.status = ExecutionStatus.CANCELLED
                record.finished_at = time.time()
                self._execution_tasks.pop(key, None)
                self._execution_cancel_requested.discard(key)
                self._execution_cancel_confirmed.discard(key)
                return

        try:
            result = await self.exec_command(
                sandbox_id=sandbox_id,
                command=command,
                timeout=timeout,
                workdir=workdir,
                execution_id=execution_id,
                retain_mailbox=True,
            )
        except asyncio.CancelledError:
            async with execution_lock:
                record.status = ExecutionStatus.CANCELLED
                record.error = "execution task cancelled"
                record.finished_at = time.time()
            raise
        except Exception as exc:
            async with execution_lock:
                record.status = (
                    ExecutionStatus.CANCELLED
                    if key in self._execution_cancel_confirmed
                    else ExecutionStatus.FAILED
                )
                record.error = str(exc)
                record.finished_at = time.time()
        else:
            async with execution_lock:
                record.stdout = result.stdout
                record.stderr = result.stderr
                record.exit_code = result.exit_code
                if key in self._execution_cancel_confirmed:
                    record.status = ExecutionStatus.CANCELLED
                elif result.exit_code == 0:
                    record.status = ExecutionStatus.COMPLETED
                else:
                    record.status = ExecutionStatus.FAILED
                    record.error = f"command exited with code {result.exit_code}"
                record.finished_at = time.time()
        finally:
            self._execution_tasks.pop(key, None)
            self._execution_cancel_requested.discard(key)
            self._execution_cancel_confirmed.discard(key)

    async def get_execution_status(
        self,
        sandbox_id: str,
        execution_id: str,
        after_sequence: int = 0,
    ) -> ExecutionStatusResponse:
        sandbox = self._get_sandbox(sandbox_id)
        key = (sandbox_id, execution_id)
        record = self._execution_records.get(key)
        if record is None:
            raise KeyError(f"Execution not found: {execution_id}")
        await self._refresh_execution_events(sandbox, record)
        result = record.model_copy(deep=True)
        result.events = [
            event for event in result.events if event.sequence > max(0, after_sequence)
        ]
        return result

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
        sandbox = self._get_sandbox(sandbox_id)
        if len(message) > 2000:
            raise ValueError("Sandbox execution response message exceeds 2000 characters")
        if not isinstance(payload, dict):
            raise ValueError("Sandbox execution response payload must be an object")
        if self._contains_secret_event_key(payload):
            raise ValueError(
                "Sandbox execution responses accept credential references, not secret values"
            )
        if contains_sensitive_event_text({"message": message, "payload": payload}):
            raise ValueError(
                "Sandbox execution responses accept credential references, not secret values"
            )
        key = (sandbox_id, execution_id)
        record = self._execution_records.get(key)
        if record is None:
            raise KeyError(f"Execution not found: {execution_id}")
        execution_lock = self._execution_locks.get(key)
        if execution_lock is None:
            raise RuntimeError("Sandbox execution response state is unavailable")
        if not _EXECUTION_EVENT_ID_RE.fullmatch(event_id):
            raise ValueError("Invalid sandbox execution event_id")
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
                    raise ValueError("Execution event already has a different response")
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
                    raise RuntimeError("Cannot respond to a terminal sandbox execution")
                event = next(
                    (
                        candidate
                        for candidate in record.events
                        if candidate.event_id == event_id
                    ),
                    None,
                )
                if event is None:
                    raise KeyError(f"Execution event not found: {event_id}")
                if not event.requires_response:
                    raise ValueError("Sandbox execution event does not accept a response")
                if event.type == ExecutionEventType.NEED_CREDENTIAL:
                    credential_ref = payload.get("credential_ref")
                    provider = payload.get("provider")
                    integration_account_id = payload.get("integration_account_id")
                    if (
                        message.strip()
                        or not isinstance(credential_ref, str)
                        or not credential_ref.strip()
                    ):
                        raise ValueError(
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
                        raise ValueError(
                            "need_credential responses accept reference fields only"
                        )
                    if not isinstance(event_provider, str) or provider != event_provider:
                        raise ValueError(
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
        sandbox = self._get_sandbox(sandbox_id)
        key = (sandbox_id, execution_id)
        record = self._execution_records.get(key)
        execution_lock = self._execution_locks.get(key)
        if record is not None and execution_lock is not None:
            async with execution_lock:
                if record.status in ExecutionStatus.terminal():
                    return False
                self._execution_cancel_requested.add(key)
                # Cancellation is an accepted state transition once the execution
                # was observed as non-terminal. Mark it before signalling so a
                # fast process exit cannot race the receipt into "completed".
                self._execution_cancel_confirmed.add(key)

        # A just-scheduled background task may not have installed its process
        # group marker yet. Give it a short event-loop window so cancellation
        # cannot be lost in that transition.
        attempts = 20 if record is not None else 1
        for _ in range(attempts):
            cancelled = await sandbox.cancel_execution(execution_id)
            if cancelled:
                return True
            task = self._execution_tasks.get(key)
            if task is None or task.done():
                break
            await asyncio.sleep(0.01)
        return record is not None

    async def read_file(
        self,
        sandbox_id: str,
        path: str,
        max_size: int = 65536,
    ) -> FileReadResponse:
        """Read a file from a sandbox."""
        bridge = self._get_bridge(sandbox_id)
        async with self._operation_guard():
            content, truncated = await bridge.read_file(path, max_size=max_size)
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
    ) -> FileReadBase64Response:
        """Read a binary file from a sandbox as base64."""
        bridge = self._get_bridge(sandbox_id)
        async with self._operation_guard():
            content_b64, file_size = await bridge.read_file_base64(path, max_size=max_size)
        return FileReadBase64Response(
            path=path,
            content_base64=content_b64,
            size=file_size,
        )

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
        mkdir: bool = True,
    ) -> FileWriteResponse:
        """Write a file into a sandbox."""
        bridge = self._get_bridge(sandbox_id)
        async with self._operation_guard():
            await bridge.write_file(path, content, mkdir=mkdir)
        return FileWriteResponse(path=path, written=True)

    async def write_file_base64(
        self,
        sandbox_id: str,
        path: str,
        content_base64: str,
        mkdir: bool = True,
    ) -> FileWriteResponse:
        """Write a binary file into a sandbox from base64 content."""
        bridge = self._get_bridge(sandbox_id)
        async with self._operation_guard():
            await bridge.write_file_base64(path, content_base64, mkdir=mkdir)
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
        sandbox = self._get_sandbox(sandbox_id)
        bridge = self._get_bridge(sandbox_id)
        skill = sandbox.skill
        if not skill:
            raise RuntimeError("Sandbox has no skill attached")

        async with self._operation_guard():
            file_contents = await bridge.read_key_skill_files(
                skill.scripts,
                entry_hint=skill.entry_hint,
                max_files=max_files,
                max_size=max_file_size,
            )

        sandbox_info = (
            f"Working directory: {sandbox.config.workdir}\n"
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
    ) -> LoadSkillResponse:
        """
        Reload a sandbox with a new skill's files (reuse mode).

        The sandbox container keeps running; only the skill files are replaced.
        Raises RuntimeError if the sandbox is busy or unavailable.
        """
        sandbox = self._get_sandbox(sandbox_id)
        sandbox.ensure_available()
        async with self._operation_guard():
            new_skill = await sandbox.load_skill(
                skill_name=skill_name,
                files=files,
                auto_install=auto_install,
                idle_threshold=idle_threshold,
            )
        return LoadSkillResponse(sandbox_id=sandbox_id, skill=new_skill, reused=True)

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """Destroy a sandbox and remove from registry."""
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            return

        active_execution_ids = [
            execution_id
            for (record_sandbox_id, execution_id), record in self._execution_records.items()
            if record_sandbox_id == sandbox_id
            and record.status not in ExecutionStatus.terminal()
        ]
        for execution_id in active_execution_ids:
            await self.cancel_execution(sandbox_id, execution_id)
        active_tasks = [
            task
            for (record_sandbox_id, _execution_id), task in self._execution_tasks.items()
            if record_sandbox_id == sandbox_id
        ]
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        await sandbox.destroy()
        if self._sandboxes.get(sandbox_id) is not sandbox:
            return

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
        await self._local_active_capacity.release()
        await self._forget_idempotent_sandbox(sandbox_id)

    def list_sandboxes(self) -> list[SandboxInfo]:
        """List all active sandboxes."""
        result: list[SandboxInfo] = []
        for sid, sbx in self._sandboxes.items():
            result.append(SandboxInfo(
                sandbox_id=sid,
                container_name=sbx.container_name,
                status=sbx.status,
                skill_name=sbx.skill.name if sbx.skill else "unknown",
                workdir=sbx.config.workdir,
                created_at=sbx.created_at,
                last_used_at=sbx.last_used_at,
                config=sbx.config,
                active_command=sbx.active_command,
                active_execution_id=getattr(sbx, "active_execution_id", None),
                expires_at=sbx.expires_at,
            ))
        return result

    def get_sandbox_status(self, sandbox_id: str) -> SandboxInfo:
        sbx = self._get_sandbox(sandbox_id)
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
            expires_at=sbx.expires_at,
        )

    # ── internal helpers ──

    async def _reconcile_managed_containers(self) -> None:
        self._orphan_cleanup_failures = []
        try:
            container_ids = await asyncio.to_thread(list_managed_sandbox_containers)
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

    @asynccontextmanager
    async def _operation_guard(self) -> AsyncIterator[None]:
        async with await self._gate.acquire_execute():
            yield

    async def _setup_sandbox(
        self,
        sandbox: DockerSandbox,
        skill: SkillManifest,
        env: dict[str, str],
        auto_install: bool,
    ) -> None:
        async with self._operation_guard():
            await sandbox.setup(skill, env, auto_install=auto_install)

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
        async with self._idempotency_lock:
            if self._shutting_down:
                raise RuntimeError("SkillRunner is shutting down")
            completed = self._idempotency_completed.get(key)
            if completed is not None:
                sandbox = self._sandboxes.get(completed.sandbox_id)
                if sandbox is not None:
                    sandbox.ensure_available()
                    return completed, None, False
                self._idempotency_completed.pop(key, None)
                self._sandbox_idempotency_keys.pop(completed.sandbox_id, None)

            future = self._idempotency_inflight.get(key)
            if future is not None:
                return None, future, False

            future = asyncio.get_running_loop().create_future()
            self._idempotency_inflight[key] = future
            return None, future, True

    async def _complete_idempotent_create(
        self,
        key: str,
        future: asyncio.Future[CreateSandboxResponse],
        result: CreateSandboxResponse,
    ) -> None:
        async with self._idempotency_lock:
            if self._idempotency_inflight.get(key) is not future:
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
            if self._idempotency_inflight.get(key) is not future:
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
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        return sandbox

    def _get_bridge(self, sandbox_id: str) -> FsBridge:
        self._get_sandbox(sandbox_id).ensure_available()
        bridge = self._fs_bridges.get(sandbox_id)
        if not bridge:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        return bridge

    async def _begin_create(self) -> asyncio.Task[object]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Sandbox creation requires an active asyncio task")
        async with self._create_lock:
            if self._shutting_down:
                raise RuntimeError("SkillRunner is shutting down")
            self._create_tasks.add(task)
        return task

    async def _reserve_local_active(self) -> None:
        if await self._local_active_capacity.try_acquire():
            return
        await self._prune_idle()
        if await self._local_active_capacity.try_acquire():
            return
        raise SandboxConcurrencyExceeded(
            f"Max active sandbox instance limit reached ({app_config.MAX_SANDBOXES}). "
            "Please retry later."
        )

    async def _cleanup_failed_create(
        self,
        sandbox: DockerSandbox | None,
        active_lease: object | None,
    ) -> None:
        if sandbox is not None:
            try:
                await sandbox.destroy()
            except BaseException:
                self._retain_failed_create_cleanup(sandbox, active_lease)
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
                    self._retain_failed_create_cleanup(sandbox, active_lease)
                logger.warning("Sandbox active lease cleanup failed", exc_info=True)
                return
        await self._local_active_capacity.release()

    def _retain_failed_create_cleanup(
        self,
        sandbox: DockerSandbox,
        active_lease: object | None,
    ) -> None:
        sandbox.status = SandboxStatus.ERROR
        self._sandboxes[sandbox.sandbox_id] = sandbox
        if active_lease is not None:
            self._active_leases[sandbox.sandbox_id] = active_lease

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
        cfg = self._default_config()
        if overrides:
            fields = getattr(overrides, "model_fields_set", None)
            if not fields:
                fields = set(type(overrides).model_fields.keys())
            values = overrides.model_dump()
            for field in fields:
                if field in values:
                    setattr(cfg, field, values[field])
        self._validate_config(cfg)
        return cfg

    @staticmethod
    def _validate_config(cfg: ContainerConfig) -> None:
        allowed_networks = set(app_config.SANDBOX_ALLOWED_NETWORKS)
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
            workdir=app_config.SANDBOX_WORKDIR,
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

    async def _prune_idle(self) -> None:
        """Remove ready sandboxes that have been idle beyond the threshold."""
        now = time.time()
        to_remove: list[str] = []
        for sid, sbx in self._sandboxes.items():
            idle = now - sbx.last_used_at
            if sbx.status == SandboxStatus.READY and idle > app_config.IDLE_TIMEOUT_SECONDS:
                to_remove.append(sid)
        for sid in to_remove:
            logger.info("Pruning idle sandbox %s", sid)
            await self.destroy_sandbox(sid)

    async def _prune_loop(self) -> None:
        """Background loop that prunes idle sandboxes every 60s."""
        while True:
            try:
                await asyncio.sleep(60)
                await self._renew_active_leases()
                await self._prune_idle()
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
