"""
Sandbox tools — expose Docker-based sandbox lifecycle to the LLM.

One ``sandbox`` tool is registered when a local or coordinated runner is
configured. Its action enum covers lifecycle, background execution status,
file access, result projection, and cancellation. The former tool names remain
execution-only compatibility aliases.

Sandbox context (sandbox_id plus exact owner per conversation) is tracked via
Redis so sessions survive across chat turns. Admission fails closed when that
authorization context cannot be persisted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import shlex
from pathlib import PurePosixPath
from typing import Any

from packages.core.ai.runtime.file_actions import (
    RuntimeFileCommitError,
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_read_lock,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_read_access,
    runtime_guard_file_mutation,
    runtime_open_entity_file_snapshot,
    runtime_write_entity_file_atomic,
)
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.composite_tools import (
    RuntimeCompositeToolCallFactory,
    SandboxToolAction,
    SandboxToolErrorCode,
)
from packages.core.ai.runtime.sandbox import (
    RUNTIME_SANDBOX_CONTEXT_PREFIX,
    RUNTIME_SANDBOX_CONTEXT_TTL,
    RUNTIME_SANDBOX_IDLE_THRESHOLD,
    runtime_delete_sandbox_context,
    runtime_init_sandbox_context,
    runtime_load_sandbox_context,
    runtime_sandbox_context_owner_matches,
    runtime_save_sandbox_context,
)
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_tool_call_context_from_handler,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.services.generated_media_naming import (
    collision_safe_artifact_path,
    resolve_workspace_artifact_base_dir,
    scope_workspace_artifact_path,
)
from packages.core.services.workspace_layout import WorkspaceArtifactDir

logger = logging.getLogger(__name__)


_GENERIC_SANDBOX_NAME = "generic-sandbox"
_GENERIC_SANDBOX_FILES = {
    "README.md": (
        "# Generic sandbox\n\n"
        "Temporary isolated workspace for ad hoc command execution.\n"
    ),
}


def _sandbox_error(code: SandboxToolErrorCode, message: str) -> str:
    """Encode every sandbox failure with the shared machine-readable contract."""

    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": code.value,
                "message": message,
            },
        },
        ensure_ascii=False,
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _artifact_display_params(kwargs: dict[str, Any]) -> tuple[bool, str]:
    role = str(kwargs.get("artifact_role") or "").strip().lower()
    display = _coerce_bool(kwargs.get("display_as_artifact"), False)
    if role not in {"final", "intermediate"}:
        role = "final" if display else "intermediate"
    if role == "final":
        display = True
    return display, role


def _get_sandbox_service_url() -> str:
    try:
        from packages.core.config import get_settings
        return get_settings().SANDBOX_SERVICE_URL.strip()
    except Exception:
        return os.getenv("SANDBOX_SERVICE_URL", "").strip()


def _get_sandbox_api_token() -> str:
    try:
        from packages.core.config import get_settings
        return get_settings().SANDBOX_API_TOKEN.strip()
    except Exception:
        return os.getenv("SANDBOX_API_TOKEN", "").strip()


SANDBOX_SERVICE_URL = _get_sandbox_service_url()

_SANDBOX_CTX_PREFIX = RUNTIME_SANDBOX_CONTEXT_PREFIX
_SANDBOX_CTX_TTL = RUNTIME_SANDBOX_CONTEXT_TTL
_SANDBOX_IDLE_THRESHOLD = RUNTIME_SANDBOX_IDLE_THRESHOLD


# ────────────────────────────────────────────────────────────────
# Sandbox context helpers (Redis-backed, async, graceful fallback)
# ────────────────────────────────────────────────────────────────

async def _save_ctx(conversation_id: str, ctx: dict) -> None:
    await runtime_save_sandbox_context(conversation_id, ctx)


async def _load_ctx(conversation_id: str) -> dict | None:
    return await runtime_load_sandbox_context(conversation_id)


async def _delete_ctx(conversation_id: str) -> None:
    await runtime_delete_sandbox_context(conversation_id)


async def _init_ctx(
    conversation_id: str,
    sandbox_id: str,
    skill_id: str,
    *,
    entity_id: str = "",
    user_id: str = "",
    agent_id: str = "",
) -> dict:
    return await runtime_init_sandbox_context(
        conversation_id,
        sandbox_id,
        skill_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
    )


async def _sandbox_instance_access_error(
    *,
    sandbox_id: str,
    entity_id: str,
    user_id: str | None,
    agent_id: str | None,
    conversation_id: str,
) -> str | None:
    """Bind model-supplied Sandbox ids to the current Runtime owner."""

    # Identity-free calls are retained for trusted internal compatibility
    # helpers. Runtime tool calls always carry a conversation and actor scope.
    if not conversation_id and not user_id:
        return None
    ctx = await _load_ctx(conversation_id)
    if (
        not isinstance(ctx, dict)
        or str(ctx.get("sandbox_id") or "") != sandbox_id
        or str(ctx.get("agent_id") or "") != str(agent_id or "")
        or not runtime_sandbox_context_owner_matches(
            ctx,
            entity_id=entity_id,
            user_id=user_id,
        )
    ):
        return _sandbox_error(
            SandboxToolErrorCode.ACCESS_DENIED,
            "Sandbox access denied for the current conversation owner.",
        )
    return None


def _sandbox_available() -> bool:
    if _get_sandbox_service_url():
        return True
    try:
        from packages.core.config import get_settings

        settings = get_settings()
        return (
            settings.SANDBOX_COORDINATION_MODE == "external-runner"
            and bool(settings.SANDBOX_RUNNERS_JSON.strip())
        )
    except Exception:
        return False


def _sandbox_uses_external_runners() -> bool:
    try:
        from packages.core.config import get_settings

        return get_settings().SANDBOX_COORDINATION_MODE == "external-runner"
    except Exception:
        return False


def _get_client():
    from packages.core.services.sandbox_sdk import SandboxClient
    return SandboxClient(
        base_url=_get_sandbox_service_url(),
        timeout=180.0,
        api_token=_get_sandbox_api_token(),
    )


async def _get_client_for_sandbox(sandbox_id: str):
    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk import SandboxClient

    settings = get_settings()
    if settings.SANDBOX_COORDINATION_MODE != "external-runner":
        return _get_client()
    from packages.core.database import async_session
    from packages.core.services.sandbox_queue_service import resolve_sandbox_runner

    async with async_session() as db:
        runner = await resolve_sandbox_runner(db, sandbox_id)
    return SandboxClient(
        base_url=runner.base_url,
        timeout=180.0,
        api_token=_get_sandbox_api_token(),
    )


async def _mark_sandbox_released(sandbox_id: str) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from packages.core.config import get_settings

    if get_settings().SANDBOX_COORDINATION_MODE != "external-runner":
        return
    from packages.core.database import async_session
    from packages.core.models.runtime_run import (
        SandboxInstance,
        SandboxReservation,
        SandboxReservationStatus,
        SandboxRunner,
    )

    async with async_session() as db:
        instance = (
            await db.execute(
                select(SandboxInstance)
                .where(SandboxInstance.sandbox_id == sandbox_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if instance is None or instance.status == "released":
            return
        now = datetime.now(timezone.utc)
        instance.status = "released"
        instance.released_at = now
        reservation = await db.get(SandboxReservation, instance.reservation_id)
        if reservation is not None:
            reservation.status = SandboxReservationStatus.RELEASED.value
            reservation.completed_at = now
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
        await db.commit()


def _runtime_sandbox_execution_id(runtime_context: Any, sandbox_id: str) -> str | None:
    run_id = str(getattr(runtime_context, "runtime_run_id", None) or "")
    tool_call_id = str(getattr(runtime_context, "runtime_tool_call_id", None) or "")
    if not run_id or not tool_call_id:
        return None
    attempt = max(1, int(getattr(runtime_context, "runtime_tool_attempt", 1) or 1))
    digest = hashlib.sha256(f"{run_id}:{tool_call_id}:{attempt}".encode("utf-8")).hexdigest()[:32]
    return f"manor-{digest}"


async def _set_runtime_active_execution(
    runtime_context: Any,
    *,
    sandbox_id: str,
    execution_id: str | None,
) -> None:
    run_id = str(getattr(runtime_context, "runtime_run_id", None) or "")
    if not run_id or not execution_id:
        return
    from packages.core.database import async_session
    from packages.core.services.runtime_run_service import set_runtime_run_active_execution

    async with async_session() as db:
        await set_runtime_run_active_execution(
            db,
            run_id=run_id,
            sandbox_id=sandbox_id,
            execution_id=execution_id,
        )
        await db.commit()


async def _clear_runtime_active_execution(
    runtime_context: Any,
    execution_id: str | None,
) -> None:
    run_id = str(getattr(runtime_context, "runtime_run_id", None) or "")
    if not run_id or not execution_id:
        return
    from packages.core.database import async_session
    from packages.core.services.runtime_run_service import clear_runtime_run_active_execution

    async with async_session() as db:
        await clear_runtime_run_active_execution(
            db,
            run_id=run_id,
            execution_id=execution_id,
        )
        await db.commit()


def _is_sandbox_capacity_error(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 429 or exc.__class__.__name__ == "SandboxCapacityError"


# ────────────────────────────────────────────────────────────────
# Workspace input collection
# ────────────────────────────────────────────────────────────────

def _workspace_path_candidates(workspace_path: str) -> list[str]:
    raw = workspace_path.replace("\\", "/").strip()
    if not raw:
        return []
    candidates: list[str] = []
    normalized = raw
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/"):]
    elif normalized == "/workspace":
        normalized = ""
    elif normalized.startswith("workspace/"):
        normalized = normalized[len("workspace/"):]
    normalized = normalized.lstrip("/")
    for candidate in (normalized, raw.lstrip("/")):
        if candidate and ".." not in candidate:
            candidates.append(candidate)
    return list(dict.fromkeys(candidates))


async def _read_workspace_bytes(
    entity_id: str,
    workspace_path: str,
    *,
    user_id: str | None,
    workspace_id: str | None,
    runtime_envelope: Any | None,
) -> tuple[bytes | None, str | None]:
    candidates = _workspace_path_candidates(workspace_path)
    if not candidates:
        return None, None

    rel_path = candidates[0]
    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return None, None
    try:
        async with runtime_entity_filesystem_read_lock(entity_root):
            blocked = await runtime_guard_file_read_access(
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                runtime_envelope=runtime_envelope,
                tool_name="sandbox",
                paths=[rel_path],
            )
            if blocked:
                return None, blocked
            with runtime_open_entity_file_snapshot(entity_id, rel_path) as snapshot:
                with open(snapshot.descriptor_path, "rb") as handle:
                    return handle.read(), None
    except Exception as exc:
        logger.debug("[sandbox] entity FS workspace read failed path=%s: %s", workspace_path, exc)
        return None, None


# ────────────────────────────────────────────────────────────────
# sandbox_create
# ────────────────────────────────────────────────────────────────

async def _sandbox_create(
    entity_id: str = "",
    user_id: str = "",
    skill_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    skill_id = str(skill_id or "").strip()
    if skill_id:
        return _sandbox_error(
            SandboxToolErrorCode.SKILL_REQUIRES_INVOKE,
            "Stored Skills must be opened with invoke_skill so Skill permissions "
            "and bindings are enforced. Omit skill_id to create a generic sandbox.",
        )
    if user_id and not conversation_id:
        return _sandbox_error(
            SandboxToolErrorCode.ACCESS_DENIED,
            "Authenticated sandbox creation requires a conversation owner context.",
        )
    if _sandbox_uses_external_runners():
        return _sandbox_error(
            SandboxToolErrorCode.GENERIC_UNAVAILABLE,
            "Generic sandbox creation is disabled with external-runner coordination "
            "because it would bypass reservation and runner admission. Use invoke_skill "
            "to allocate a governed sandbox.",
        )

    if conversation_id:
        try:
            existing_ctx = await _load_ctx(conversation_id)
        except Exception as exc:
            return _sandbox_error(
                SandboxToolErrorCode.CREATE_FAILED,
                f"Sandbox owner context could not be loaded: {exc}",
            )
        if existing_ctx is not None:
            existing_sandbox_id = (
                str(existing_ctx.get("sandbox_id") or "")
                if isinstance(existing_ctx, dict)
                else ""
            )
            owner_matches = (
                isinstance(existing_ctx, dict)
                and str(existing_ctx.get("agent_id") or "")
                == str(runtime_context.agent_id or "")
                and runtime_sandbox_context_owner_matches(
                    existing_ctx,
                    entity_id=entity_id,
                    user_id=user_id,
                )
            )
            if not owner_matches or not existing_sandbox_id:
                return _sandbox_error(
                    SandboxToolErrorCode.ACCESS_DENIED,
                    "An existing Sandbox context belongs to another Runtime owner.",
                )
            if str(existing_ctx.get("skill_id") or ""):
                return _sandbox_error(
                    SandboxToolErrorCode.ALREADY_ACTIVE,
                    "This conversation already has a governed Skill sandbox. Destroy "
                    "or finish it before creating a generic sandbox.",
                )

            existing_client = None
            try:
                existing_client = await _get_client_for_sandbox(existing_sandbox_id)
                existing_status = await existing_client.status(existing_sandbox_id)
                status_value = getattr(existing_status.status, "value", existing_status.status)
                return "\n".join(
                    [
                        f"sandbox_id: {existing_sandbox_id}",
                        f"status: {status_value}",
                        f"workdir: {existing_status.workdir}",
                        "sandbox_kind: generic",
                        "reused: true",
                    ]
                )
            except Exception as exc:
                from packages.core.services.sandbox_sdk.exceptions import (
                    SandboxNotFoundError,
                )

                if isinstance(exc, SandboxNotFoundError):
                    await _delete_ctx(conversation_id)
                else:
                    return _sandbox_error(
                        SandboxToolErrorCode.CREATE_FAILED,
                        f"Existing sandbox status could not be verified: {exc}",
                    )
            finally:
                if existing_client is not None:
                    await existing_client.close()
    files = dict(_GENERIC_SANDBOX_FILES)

    client = None
    created_sandbox_id = ""
    try:
        client = _get_client()
        result = await client.create_from_files(
            skill_name=_GENERIC_SANDBOX_NAME,
            files=files,
            env={},
            allowed_sensitive_keys=[],
            auto_install=False,
            config=None,
        )
        created_sandbox_id = result.sandbox_id

        skill = result.skill
        parts = [
            f"sandbox_id: {result.sandbox_id}",
            f"status: {result.status}",
            f"workdir: {result.workdir}",
            "sandbox_kind: generic",
        ]
        if skill.entry_hint:
            parts.append(f"entry_hint: {skill.entry_hint}")
        if skill.scripts:
            parts.append(f"scripts: {', '.join(skill.scripts)}")
        if skill.requirements_txt:
            parts.append("dependencies: installed from requirements.txt")
        parts.append("credentials_injected: (none)")
        if result.env_blocked:
            parts.append(f"env_blocked: {', '.join(result.env_blocked)}")
        parts.append(
            "workspace_mount: (none; use sandbox action='write_file' with "
            "workspace_path or direct content to inject inputs)"
        )
        parts.append(
            "NOTE: This is a blank isolated sandbox. Proceed with sandbox "
            "action='exec' or action='write_file'."
        )

        if conversation_id:
            await _init_ctx(
                conversation_id,
                result.sandbox_id,
                "",
                entity_id=entity_id,
                user_id=user_id,
                agent_id=runtime_context.agent_id or "",
            )
        logger.info(
            "[sandbox] created: kind=generic sandbox=%s entity=%s",
            result.sandbox_id,
            entity_id or "(none)",
        )
        return "\n".join(parts)
    except Exception as exc:
        if created_sandbox_id and client is not None:
            try:
                await client.destroy(sandbox_id=created_sandbox_id)
            except Exception:
                logger.warning(
                    "[sandbox] failed to destroy unadmitted sandbox=%s",
                    created_sandbox_id,
                    exc_info=True,
                )
        if _is_sandbox_capacity_error(exc):
            logger.warning("[sandbox] capacity full: kind=generic error=%s", exc)
            return _sandbox_error(
                SandboxToolErrorCode.CAPACITY_FULL,
                f"Sandbox capacity is full. Please retry later. Details: {exc}",
            )
        logger.exception("[sandbox] create failed: kind=generic error=%s", exc)
        return _sandbox_error(
            SandboxToolErrorCode.CREATE_FAILED,
            f"Sandbox creation failed: {exc}",
        )
    finally:
        if client is not None:
            await client.close()


_SANDBOX_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_create",
        "description": (
            "Create a blank isolated sandbox and return its sandbox_id and workdir. "
            "Use invoke_skill for stored Skills so their permissions and bindings are enforced."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_exec
# ────────────────────────────────────────────────────────────────

async def _sandbox_exec(
    entity_id: str = "",
    user_id: str = "",
    sandbox_id: str = "",
    command: str = "",
    timeout: Any = 60,
    background: Any = False,
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    command = command.strip()
    try:
        timeout = min(int(timeout or 60), 300)
    except (ValueError, TypeError):
        timeout = 60

    if not sandbox_id or not command:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and command are required.",
        )

    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        execution_id = _runtime_sandbox_execution_id(runtime_context, sandbox_id)
        if _coerce_bool(background, False):
            result = None
            try:
                result = await client.start_exec(
                    sandbox_id=sandbox_id,
                    command=command,
                    timeout=timeout,
                    execution_id=execution_id,
                )
                try:
                    await _set_runtime_active_execution(
                        runtime_context,
                        sandbox_id=sandbox_id,
                        execution_id=result.execution_id,
                    )
                except Exception:
                    await client.cancel_execution(sandbox_id, result.execution_id)
                    raise
            finally:
                await client.close()
            assert result is not None
            return json.dumps(
                {
                    "sandbox_id": result.sandbox_id,
                    "execution_id": result.execution_id,
                    "status": result.status,
                    "created_at": result.created_at,
                    "poll_after_seconds": 1,
                    "hint": (
                        "Poll with sandbox action='status' and the same sandbox_id/"
                        "execution_id; use action='cancel' to stop it."
                    ),
                },
                ensure_ascii=False,
            )

        try:
            await _set_runtime_active_execution(
                runtime_context,
                sandbox_id=sandbox_id,
                execution_id=execution_id,
            )
            result = await client.exec(
                sandbox_id=sandbox_id,
                command=command,
                timeout=timeout,
                execution_id=execution_id,
            )
        finally:
            await client.close()
            await _clear_runtime_active_execution(runtime_context, execution_id)

        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        parts.append(f"[exit_code: {result.exit_code}]")

        logger.info("[sandbox] exec: sandbox=%s exit=%s cmd=%s", sandbox_id, result.exit_code, command[:80])
        return "\n".join(parts)

    except Exception as exc:
        logger.exception("[sandbox] exec failed: sandbox=%s error=%s", sandbox_id, exc)
        err_msg = str(exc).lower()
        exc_type = type(exc).__name__.lower()
        is_timeout = "timed out" in err_msg or "timeout" in err_msg or "readtimeout" in exc_type
        if is_timeout:
            return _sandbox_error(
                SandboxToolErrorCode.EXEC_TIMEOUT,
                f"Command exceeded its {timeout}s limit and was cancelled. Do not "
                "automatically repeat a command with side effects. For long-running "
                "work, start it with background=true and poll action='status'.",
            )
        return _sandbox_error(
            SandboxToolErrorCode.EXEC_FAILED,
            f"Sandbox exec failed: {exc}",
        )


_SANDBOX_EXEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_exec",
        "description": (
            "Execute a command inside a sandbox; returns stdout/stderr/exit_code. "
            "Use bash for .sh scripts. For long commands set background=true, then "
            "poll status instead of retrying timed-out commands. "
            "A background script can call `python \"$MANOR_SANDBOX_BRIDGE\" emit` "
            "and `wait` to exchange bounded structured events with the Agent. "
            "Prefer running existing skill scripts or explicit helper files. "
            "Use sandbox_write_file for larger new files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID returned by sandbox_create"},
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
                "background": {
                    "type": "boolean",
                    "description": (
                        "Return immediately with execution_id, then poll sandbox status. "
                        "Use for commands that may take more than a few seconds."
                    ),
                },
            },
            "required": ["sandbox_id", "command"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_status / sandbox_cancel
# ────────────────────────────────────────────────────────────────

def _sandbox_execution_payload(result: Any) -> dict[str, Any]:
    payload = {
        "sandbox_id": result.sandbox_id,
        "execution_id": result.execution_id,
        "status": result.status,
        "created_at": result.created_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "error": result.error,
        "terminal": result.terminal,
        "events": [
            {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "type": event.type,
                "message": event.message,
                "payload": event.payload,
                "requires_response": event.requires_response,
                "responded": event.responded,
                "created_at": event.created_at,
            }
            for event in result.events
        ],
        "next_sequence": result.next_sequence,
        "waiting_for_response": result.waiting_for_response,
    }
    if not result.terminal:
        payload["poll_after_seconds"] = 1
        payload["hint"] = (
            "Handle each new structured event. For need_tool, call the requested "
            "capability only through the normal Runtime permission gate; for "
            "need_input ask the user only when you cannot resolve it. Reply with "
            "action='respond' and the exact event_id, then poll status again. "
            "Never send plaintext credentials; use a governed credential reference."
            if result.waiting_for_response
            else "Poll action='status' again with after_sequence, or use action='cancel'."
        )
    return payload


async def _sandbox_status(
    entity_id: str = "",
    user_id: str = "",
    sandbox_id: str = "",
    execution_id: str = "",
    after_sequence: Any = 0,
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    execution_id = execution_id.strip()
    try:
        after_sequence = max(0, int(after_sequence or 0))
    except (TypeError, ValueError):
        after_sequence = 0
    if not sandbox_id or not execution_id:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and execution_id are required.",
        )
    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    client = None
    try:
        client = await _get_client_for_sandbox(sandbox_id)
        result = await client.execution_status(
            sandbox_id,
            execution_id,
            after_sequence=after_sequence,
        )
        if result.terminal:
            await _clear_runtime_active_execution(runtime_context, execution_id)
        return json.dumps(_sandbox_execution_payload(result), ensure_ascii=False)
    except Exception as exc:
        from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

        code = (
            SandboxToolErrorCode.EXECUTION_NOT_FOUND
            if isinstance(exc, SandboxNotFoundError)
            else SandboxToolErrorCode.EXEC_FAILED
        )
        return _sandbox_error(code, f"Sandbox execution status failed: {exc}")
    finally:
        if client is not None:
            await client.close()


_SANDBOX_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_status",
        "description": (
            "Poll a background sandbox command and receive bounded structured events. "
            "Use after_sequence from the prior result to fetch only newer events."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "execution_id": {"type": "string"},
                "after_sequence": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Return events after this sequence number.",
                },
            },
            "required": ["sandbox_id", "execution_id"],
        },
    },
}


async def _prepare_sandbox_response(
    *,
    client: Any,
    sandbox_id: str,
    execution_id: str,
    event_id: str,
    entity_id: str,
    user_id: str,
    payload: dict[str, Any],
    message: str,
) -> tuple[dict[str, Any], str]:
    """Exchange an actor-scoped account choice for a signed Sandbox reference."""

    status = await client.execution_status(sandbox_id, execution_id)
    event = next(
        (candidate for candidate in status.events if candidate.event_id == event_id),
        None,
    )
    if event is None or str(event.type) != "need_credential":
        return payload, message
    if message.strip():
        raise ValueError("need_credential responses do not accept a message")
    if set(payload) - {"integration_account_id"}:
        raise ValueError(
            "need_credential responses accept only integration_account_id"
        )
    integration_account_id = (
        str(payload.get("integration_account_id") or "").strip() or None
    )
    event_payload = event.payload if isinstance(event.payload, dict) else {}
    provider = str(event_payload.get("provider") or "").strip()
    if not entity_id or not user_id or not provider:
        raise ValueError(
            "need_credential requires an actor-scoped provider"
        )

    from packages.core.database import async_session
    from packages.core.services.agent_permission_service import can_use_integration
    from packages.core.services.provider_keys import canonical_provider_key
    from packages.core.services.sandbox_credential_refs import (
        issue_sandbox_credential_ref,
    )

    provider_key = canonical_provider_key(provider)
    async with async_session() as db:
        decision = await can_use_integration(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider=provider_key,
            integration_account_id=integration_account_id,
            allow_env_fallback=False,
        )
    if not decision.allowed or not decision.account_id:
        raise ValueError(decision.reason or "Integration account access denied")
    resolved_account_id = str(decision.account_id)
    credential_ref = issue_sandbox_credential_ref(
        signing_key=_get_sandbox_api_token(),
        sandbox_id=sandbox_id,
        execution_id=execution_id,
        event_id=event_id,
        provider=provider,
        integration_account_id=resolved_account_id,
    )
    return {
        "credential_ref": credential_ref,
        "integration_account_id": resolved_account_id,
        "provider": provider,
    }, ""


async def _sandbox_respond(
    entity_id: str = "",
    user_id: str = "",
    sandbox_id: str = "",
    execution_id: str = "",
    event_id: str = "",
    payload: Any = None,
    message: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    execution_id = execution_id.strip()
    event_id = event_id.strip()
    if not sandbox_id or not execution_id or not event_id:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id, execution_id, and event_id are required.",
        )
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "payload must be an object.",
        )
    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    client = None
    try:
        client = await _get_client_for_sandbox(sandbox_id)
        payload, message = await _prepare_sandbox_response(
            client=client,
            sandbox_id=sandbox_id,
            execution_id=execution_id,
            event_id=event_id,
            entity_id=str(runtime_context.entity_id or entity_id or ""),
            user_id=str(runtime_context.user_id or user_id or ""),
            payload=payload,
            message=str(message or ""),
        )
        result = await client.send_execution_response(
            sandbox_id,
            execution_id,
            event_id,
            payload=payload,
            message=message,
        )
        return json.dumps(
            {
                "sandbox_id": result.sandbox_id,
                "execution_id": result.execution_id,
                "event_id": result.event_id,
                "accepted": result.accepted,
                "duplicate": result.duplicate,
                "hint": "Poll action='status' with the last next_sequence.",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

        code = (
            SandboxToolErrorCode.EXECUTION_EVENT_NOT_FOUND
            if isinstance(exc, SandboxNotFoundError)
            else SandboxToolErrorCode.RESPOND_FAILED
        )
        return _sandbox_error(code, f"Sandbox execution response failed: {exc}")
    finally:
        if client is not None:
            await client.close()


_SANDBOX_RESPOND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_respond",
        "description": (
            "Reply to one structured event from a running sandbox command. "
            "For need_credential, optionally provide only integration_account_id to "
            "select among the current actor's connections; Core validates access and "
            "creates the governed short-lived reference. Never include plaintext "
            "credentials or a credential_ref."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "execution_id": {"type": "string"},
                "event_id": {"type": "string"},
                "payload": {"type": "object"},
                "message": {"type": "string"},
            },
            "required": ["sandbox_id", "execution_id", "event_id"],
        },
    },
}


async def _sandbox_cancel(
    entity_id: str = "",
    user_id: str = "",
    sandbox_id: str = "",
    execution_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    execution_id = execution_id.strip()
    if not sandbox_id or not execution_id:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and execution_id are required.",
        )
    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    client = None
    try:
        client = await _get_client_for_sandbox(sandbox_id)
        result = await client.cancel_execution(sandbox_id, execution_id)
        return json.dumps(
            {
                "sandbox_id": result.sandbox_id,
                "execution_id": result.execution_id,
                "cancelled": result.cancelled,
                "hint": "Poll action='status' to observe the terminal state.",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return _sandbox_error(
            SandboxToolErrorCode.CANCEL_FAILED,
            f"Sandbox execution cancel failed: {exc}",
        )
    finally:
        if client is not None:
            await client.close()


_SANDBOX_CANCEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_cancel",
        "description": "Cancel one background sandbox command by execution_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "execution_id": {"type": "string"},
            },
            "required": ["sandbox_id", "execution_id"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_read_file
# ────────────────────────────────────────────────────────────────

async def _sandbox_read_file(
    entity_id: str = "",
    user_id: str = "",
    sandbox_id: str = "",
    path: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    path = (path or kwargs.get("file_path") or "").strip()
    if not sandbox_id or not path:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and path are required.",
        )

    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    if not path.startswith("/"):
        path = f"/skill/{path}"

    try:
        offset = max(0, int(kwargs.get("offset") or 0))
    except (TypeError, ValueError):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "offset must be a non-negative integer.",
        )
    raw_limit = kwargs.get("limit")
    try:
        limit = None if raw_limit is None else max(1, min(int(raw_limit), 50_000))
    except (TypeError, ValueError):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "limit must be a positive integer.",
        )

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            result = await client.read_file(sandbox_id=sandbox_id, path=path)
        finally:
            await client.close()
        full_content = result.content
        page_end = len(full_content) if limit is None else offset + limit
        content = full_content[offset:page_end]
        returned_end = offset + len(content)
        page_has_more = returned_end < len(full_content)
        is_truncated = page_has_more or result.truncated
        return json.dumps(
            {
                "path": result.path,
                "content": content,
                "size": result.size,
                "offset": offset,
                "returned_chars": len(content),
                # Only advertise a next page that this endpoint can actually
                # serve.  A service-level truncation means the remainder was
                # never returned to us, so repeating with a larger offset
                # would otherwise produce an empty/misleading page.
                "next_offset": returned_end if page_has_more else None,
                "truncated": is_truncated,
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8", errors="replace")
                ).hexdigest(),
                "hint": (
                    "More content is available; call read_file again with "
                    f"offset={returned_end} and an appropriate limit."
                    if page_has_more and not result.truncated
                    else (
                        "Content is truncated by the sandbox service read limit; inspect "
                        "a smaller file or save/export the full artifact."
                        if result.truncated
                        else None
                    )
                ),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.exception("[sandbox] read_file failed: sandbox=%s error=%s", sandbox_id, exc)
        return _sandbox_error(
            SandboxToolErrorCode.READ_FAILED,
            f"Sandbox read_file failed: {exc}",
        )


_SANDBOX_READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_read_file",
        "description": "Read a file from inside a sandbox container. Use this to retrieve output files generated by skill scripts.",
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID returned by sandbox_create"},
                "path": {"type": "string", "description": "Absolute path inside the sandbox, e.g. '/skill/output.json'"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional zero-based character offset for paginated text reads.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50000,
                    "description": "Optional maximum number of characters to return.",
                },
            },
            "required": ["sandbox_id", "path"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_write_file
# ────────────────────────────────────────────────────────────────

async def _sandbox_write_file(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    sandbox_id: str = "",
    path: str = "",
    content: Any = None,
    workspace_path: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    path = (path or kwargs.get("file_path") or "").strip()
    workspace_path = (workspace_path or "").strip()

    if not sandbox_id or not path:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and path are required.",
        )
    if not path.startswith("/"):
        path = f"/skill/{path}"
    if content is not None and workspace_path:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "Provide either `content` or `workspace_path`, not both.",
        )
    if content is None and not workspace_path:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "Either `content` or `workspace_path` must be provided.",
        )

    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            if workspace_path:
                workspace_bytes, read_blocked = await _read_workspace_bytes(
                    entity_id,
                    workspace_path,
                    user_id=user_id or runtime_context.user_id,
                    workspace_id=workspace_id or runtime_context.workspace_id,
                    runtime_envelope=runtime_context.runtime_envelope,
                )
                if read_blocked:
                    return _sandbox_error(
                        SandboxToolErrorCode.ACCESS_DENIED,
                        "Workspace file read access denied.",
                    )
                if workspace_bytes is None:
                    return _sandbox_error(
                        SandboxToolErrorCode.WORKSPACE_FILE_NOT_FOUND,
                        f"File not found in workspace: {workspace_path}",
                    )
                result = await client.write_file_base64(
                    sandbox_id=sandbox_id,
                    path=path,
                    content_base64=base64.b64encode(workspace_bytes).decode("ascii"),
                    mkdir=True,
                )
            else:
                result = await client.write_file(
                    sandbox_id=sandbox_id, path=path, content=str(content), mkdir=True,
                )
        finally:
            await client.close()
        source = f"workspace:{workspace_path}" if workspace_path else "direct content"
        return f"Written to {result.path} (source: {source})"
    except Exception as exc:
        logger.exception("[sandbox] write_file failed: sandbox=%s error=%s", sandbox_id, exc)
        return _sandbox_error(
            SandboxToolErrorCode.WRITE_FAILED,
            f"Sandbox write_file failed: {exc}",
        )


_SANDBOX_WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_write_file",
        "description": (
            "Write a file into a sandbox from direct content or workspace_path. "
            "Provide exactly one source. Prefer this over sandbox_exec heredocs, "
            "especially for Unicode."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID returned by sandbox_create"},
                "path": {"type": "string", "description": "Absolute destination path inside the sandbox, e.g. '/skill/data.csv'"},
                "content": {"type": "string", "description": "Text content to write directly"},
                "workspace_path": {"type": "string", "description": "Entity FS or MinIO path to inject, e.g. '/workspace/uploads/chat/input.png'"},
            },
            "required": ["sandbox_id", "path"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_save_result
# ────────────────────────────────────────────────────────────────

_PPTX_FINAL_GATE_MIN_SCORE = 90
_DOCX_FINAL_GATE_MIN_SCORE = 90


def _normalized_sandbox_path(path: str) -> PurePosixPath:
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/skill/{normalized.lstrip('/')}"
    return PurePosixPath(normalized)


def _pptx_project_export_context(file_path: str, filename: str) -> tuple[str, str, str] | None:
    """Return canonical project, render, and report paths for a project export."""
    if not file_path.strip():
        return None
    path = _normalized_sandbox_path(file_path)
    if path.parent.name != "exports":
        return None
    project = path.parent.parent
    if project.parent.name != "projects" or str(project.parent.parent) != "/skill":
        return None
    if path.suffix.lower() != ".pptx" and PurePosixPath(filename).suffix.lower() != ".pptx":
        return None
    return (
        str(project),
        str(project / "qa" / "final-render"),
        str(project / "qa" / "pptx-quality.json"),
    )


def _docx_project_export_context(file_path: str, filename: str) -> tuple[str, str, str] | None:
    """Return canonical project, render, and report paths for a DOCX export."""
    if not file_path.strip():
        return None
    path = _normalized_sandbox_path(file_path)
    if path.parent.name != "exports":
        return None
    project = path.parent.parent
    if project.parent.name != "projects" or str(project.parent.parent) != "/skill":
        return None
    if path.suffix.lower() != ".docx" and PurePosixPath(filename).suffix.lower() != ".docx":
        return None
    return (
        str(project),
        str(project / "qa" / "final-render"),
        str(project / "qa" / "docx-quality.json"),
    )


def _pptx_quality_evidence_error(
    *,
    file_path: str,
    content_bytes: bytes,
    gate_exit_code: int,
    report_bytes: bytes | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Verify that the server-run gate passed for these exact PPTX bytes."""
    if not report_bytes:
        return "the machine quality report was not produced", None
    try:
        payload = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"the machine quality report is unreadable ({exc})", None
    if not isinstance(payload, dict):
        return "the machine quality report is not a JSON object", None

    errors = payload.get("errors")
    error_list = [str(item) for item in errors] if isinstance(errors, list) else []
    repair_actions = payload.get("repair_actions")
    repair_list = []
    if isinstance(repair_actions, list):
        for item in repair_actions[:3]:
            if isinstance(item, dict) and item.get("action"):
                repair_list.append(str(item["action"]))

    metrics = payload.get("metrics")
    layout_defects = metrics.get("layout_defects") if isinstance(metrics, dict) else None
    defect_list: list[str] = []
    if isinstance(layout_defects, list):
        for defect in layout_defects[:5]:
            if not isinstance(defect, dict):
                continue
            slide = defect.get("slide")
            kind = str(defect.get("kind") or "layout-defect")
            shapes = defect.get("shapes")
            shape_list = shapes if isinstance(shapes, list) else []
            shape_refs = []
            for shape in shape_list[:2]:
                if not isinstance(shape, dict):
                    continue
                shape_id = shape.get("shape_id")
                text = str(shape.get("text") or "").strip()
                label = f"shape {shape_id}"
                if text:
                    label += f" {text[:40]!r}"
                shape_refs.append(label)
            detail = f"slide {slide}: {kind}"
            if shape_refs:
                detail += " (" + ", ".join(shape_refs) + ")"
            defect_list.append(detail)

    def _with_repairs(details: str) -> str:
        if defect_list:
            details += " Layout evidence: " + "; ".join(defect_list) + "."
        if not repair_list:
            return details
        return details + " Required repair: " + " ".join(repair_list)

    if gate_exit_code != 0:
        details = "; ".join(error_list[:5]) or f"quality command exited with status {gate_exit_code}"
        return _with_repairs(details), payload
    if payload.get("status") != "pass":
        details = "; ".join(error_list[:5]) or "the quality report status is not pass"
        return _with_repairs(details), payload
    try:
        quality_score = int(payload.get("quality_score", -1))
        minimum_score = int(payload.get("minimum_score", -1))
        slide_count = int(payload.get("slide_count", 0))
    except (TypeError, ValueError):
        return "the quality report contains invalid numeric evidence", payload
    if minimum_score < _PPTX_FINAL_GATE_MIN_SCORE or quality_score < _PPTX_FINAL_GATE_MIN_SCORE:
        return (
            (
                f"quality score {quality_score} does not meet the enforced "
                f"minimum {_PPTX_FINAL_GATE_MIN_SCORE}"
            ),
            payload,
        )
    if slide_count <= 0:
        return "the quality report has no verified slides", payload

    if not isinstance(metrics, dict):
        return "the quality report is missing metrics", payload
    expected_hash = hashlib.sha256(content_bytes).hexdigest()
    if metrics.get("pptx_sha256") != expected_hash:
        return "the report is stale or belongs to a different PPTX", payload
    if metrics.get("pptx_size_bytes") != len(content_bytes):
        return "the report file-size evidence does not match the PPTX", payload
    if metrics.get("rendered_slide_count") != slide_count:
        return "not every final slide has verified render evidence", payload

    reported_path = str(payload.get("pptx") or "")
    if reported_path and _normalized_sandbox_path(reported_path) != _normalized_sandbox_path(file_path):
        return "the report points to a different PPTX path", payload
    return None, payload


def _docx_quality_evidence_error(
    *,
    file_path: str,
    content_bytes: bytes,
    gate_exit_code: int,
    report_bytes: bytes | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Verify that the server-run gate passed for these exact DOCX bytes."""
    if not report_bytes:
        return "the machine quality report was not produced", None
    try:
        payload = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"the machine quality report is unreadable ({exc})", None
    if not isinstance(payload, dict):
        return "the machine quality report is not a JSON object", None

    errors = payload.get("errors")
    error_list = [str(item) for item in errors] if isinstance(errors, list) else []
    if gate_exit_code != 0:
        details = "; ".join(error_list[:5]) or f"quality command exited with status {gate_exit_code}"
        return details, payload
    if payload.get("status") != "pass":
        return "; ".join(error_list[:5]) or "the quality report status is not pass", payload
    try:
        quality_score = int(payload.get("quality_score", -1))
        minimum_score = int(payload.get("minimum_score", -1))
        page_count = int(payload.get("page_count", 0))
    except (TypeError, ValueError):
        return "the quality report contains invalid numeric evidence", payload
    if minimum_score < _DOCX_FINAL_GATE_MIN_SCORE or quality_score < _DOCX_FINAL_GATE_MIN_SCORE:
        return (
            (
                f"quality score {quality_score} does not meet the enforced "
                f"minimum {_DOCX_FINAL_GATE_MIN_SCORE}"
            ),
            payload,
        )
    if page_count <= 0:
        return "the quality report has no verified pages", payload

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return "the quality report is missing metrics", payload
    expected_hash = hashlib.sha256(content_bytes).hexdigest()
    if metrics.get("docx_sha256") != expected_hash:
        return "the report is stale or belongs to a different DOCX", payload
    if metrics.get("docx_size_bytes") != len(content_bytes):
        return "the report file-size evidence does not match the DOCX", payload
    if metrics.get("rendered_page_count") != page_count:
        return "not every final page has verified render evidence", payload

    reported_path = str(payload.get("docx") or "")
    if reported_path and _normalized_sandbox_path(reported_path) != _normalized_sandbox_path(file_path):
        return "the report points to a different DOCX path", payload
    return None, payload


async def _sandbox_save_result(
    entity_id: str = "",
    sandbox_id: str = "",
    file_path: str = "",
    url: str = "",
    filename: str = "",
    user_id: str = "",
    save_to_knowledge: bool | str | int | None = None,
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_handler(kwargs, user_id=user_id)
    sandbox_id = sandbox_id.strip()
    file_path = file_path.strip()
    url = url.strip()
    filename = filename.strip()
    display_as_artifact, artifact_role = _artifact_display_params(kwargs)

    if not filename:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "filename is required.",
        )
    if not sandbox_id and not file_path and not url:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "Provide either (sandbox_id + file_path) or url.",
        )
    if sandbox_id and file_path and url:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "Provide either (sandbox_id + file_path) or url, not both.",
        )
    if url and not url.startswith(("http://", "https://")):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            f"Invalid url '{url}': must start with http:// or https://. "
            "To save a file generated inside the sandbox, use sandbox_id + file_path instead of url.",
        )

    if sandbox_id:
        access_error = await _sandbox_instance_access_error(
            sandbox_id=sandbox_id,
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            agent_id=runtime_context.agent_id,
            conversation_id=runtime_context.conversation_id or "",
        )
        if access_error:
            return access_error

    content_bytes: bytes | None = None
    quality_evidence: dict[str, Any] | None = None

    if sandbox_id and file_path:
        pptx_gate_context = _pptx_project_export_context(file_path, filename)
        docx_gate_context = _docx_project_export_context(file_path, filename)
        is_pptx = (
            PurePosixPath(file_path).suffix.lower() == ".pptx"
            or PurePosixPath(filename).suffix.lower() == ".pptx"
        )
        pptx_is_deliverable = is_pptx and (
            artifact_role == "final"
            or display_as_artifact
            or _coerce_bool(save_to_knowledge, True)
        )
        is_final_docx = artifact_role == "final" and (
            PurePosixPath(file_path).suffix.lower() == ".docx"
            or PurePosixPath(filename).suffix.lower() == ".docx"
        )
        if pptx_is_deliverable and pptx_gate_context is None:
            return _sandbox_error(
                SandboxToolErrorCode.QUALITY_GATE_BLOCKED,
                "PPTX_FINAL_QUALITY_GATE_BLOCKED: PPTX deliverables must be exported "
                "from /skill/projects/<project>/exports/ so Manor can verify project, "
                "render, and quality evidence. No file was saved.",
            )
        if is_final_docx and docx_gate_context is None:
            return _sandbox_error(
                SandboxToolErrorCode.QUALITY_GATE_BLOCKED,
                "DOCX_FINAL_QUALITY_GATE_BLOCKED: final DOCX files must be exported "
                "from /skill/projects/<project>/exports/ so Manor can verify project, "
                "render, and quality evidence. No file was saved.",
            )
        gate_context = pptx_gate_context or docx_gate_context
        gate_kind = "pptx" if pptx_gate_context is not None else "docx"
        try:
            client = await _get_client_for_sandbox(sandbox_id)
            try:
                gate_result = None
                report_bytes = None
                if gate_context is not None:
                    project_path, render_path, report_path = gate_context
                    if gate_kind == "pptx":
                        command_parts = [
                            "python3",
                            "/skill/scripts/pptx_quality_gate.py",
                            shlex.quote(file_path),
                            "--mode",
                            "auto",
                            "--project",
                            shlex.quote(project_path),
                            "--render-dir",
                            shlex.quote(render_path),
                            "--min-score",
                            str(_PPTX_FINAL_GATE_MIN_SCORE),
                            "--report",
                            shlex.quote(report_path),
                        ]
                    else:
                        command_parts = [
                            "python3",
                            "/skill/scripts/docx_quality_gate.py",
                            shlex.quote(file_path),
                            "--project",
                            shlex.quote(project_path),
                            "--render-dir",
                            shlex.quote(render_path),
                            "--min-score",
                            str(_DOCX_FINAL_GATE_MIN_SCORE),
                            "--report",
                            shlex.quote(report_path),
                        ]
                    gate_result = await client.exec(
                        sandbox_id=sandbox_id,
                        command=" ".join(command_parts),
                        timeout=240,
                    )
                result = await client.read_file_base64(
                    sandbox_id=sandbox_id,
                    path=file_path,
                )
                if gate_context is not None:
                    try:
                        report_result = await client.read_file_base64(
                            sandbox_id=sandbox_id,
                            path=gate_context[2],
                        )
                        report_bytes = base64.b64decode(report_result.content_base64)
                    except Exception:
                        report_bytes = None
            finally:
                await client.close()
            content_bytes = base64.b64decode(result.content_base64)
            if gate_context is not None:
                evidence_checker = (
                    _pptx_quality_evidence_error
                    if gate_kind == "pptx"
                    else _docx_quality_evidence_error
                )
                evidence_error, quality_evidence = evidence_checker(
                    file_path=file_path,
                    content_bytes=content_bytes,
                    gate_exit_code=gate_result.exit_code if gate_result is not None else -1,
                    report_bytes=report_bytes,
                )
                if evidence_error:
                    label = gate_kind.upper()
                    logger.warning(
                        "[sandbox] blocked %s delivery: sandbox=%s file=%s reason=%s",
                        label,
                        sandbox_id,
                        file_path,
                        evidence_error,
                    )
                    return _sandbox_error(
                        SandboxToolErrorCode.QUALITY_GATE_BLOCKED,
                        f"{label}_FINAL_QUALITY_GATE_BLOCKED: "
                        f"{evidence_error}. Fix the source, re-export it, render every "
                        f"{'slide' if gate_kind == 'pptx' else 'page'}, and call "
                        "sandbox action='save_result' again. "
                        f"The current {label} was not saved.",
                    )
        except Exception as exc:
            logger.exception("[sandbox] save_result read failed: %s", exc)
            return _sandbox_error(
                SandboxToolErrorCode.READ_FAILED,
                f"Failed to read file from sandbox: {exc}. No file was saved. "
                "Check that the prior sandbox action='exec' command succeeded and that "
                f"'{file_path}' actually exists before calling sandbox action='save_result' again.",
            )
    elif url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
                resp = await http.get(url)
            if resp.status_code != 200:
                return _sandbox_error(
                    SandboxToolErrorCode.DOWNLOAD_FAILED,
                    f"Failed to download URL (HTTP {resp.status_code}): {url}",
                )
            content_bytes = resp.content
        except Exception as exc:
            logger.exception("[sandbox] save_result download failed: %s", exc)
            return _sandbox_error(
                SandboxToolErrorCode.DOWNLOAD_FAILED,
                f"Failed to download URL: {exc}",
            )

    if not content_bytes:
        return _sandbox_error(
            SandboxToolErrorCode.EMPTY_RESULT,
            "No content to save.",
        )

    import os as _os

    safe_filename = _os.path.basename(filename)
    if safe_filename != filename or safe_filename in {"", ".", ".."}:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "filename must be a plain file name, not a path.",
        )

    # Save to entity filesystem + document store
    entity_dir = runtime_entity_file_root(entity_id)
    if not entity_dir:
        return _sandbox_error(
            SandboxToolErrorCode.FILESYSTEM_UNAVAILABLE,
            "Entity filesystem is not enabled.",
        )

    rel_path = safe_filename
    if runtime_context.workspace_id:
        try:
            workspace_base = await resolve_workspace_artifact_base_dir(
                entity_id=entity_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
            )
        except Exception as exc:  # noqa: BLE001
            return _sandbox_error(
                SandboxToolErrorCode.WORKSPACE_SCOPE_UNAVAILABLE,
                f"Workspace artifact scope is unavailable: {exc}",
            )
        if not workspace_base:
            return _sandbox_error(
                SandboxToolErrorCode.WORKSPACE_SCOPE_UNAVAILABLE,
                "Workspace artifact scope is unavailable.",
            )
        rel_path = scope_workspace_artifact_path(
            rel_path,
            workspace_base,
            default_subdir=WorkspaceArtifactDir.ARTIFACTS.value,
        )
    async with runtime_entity_filesystem_mutation_lock(entity_dir):
        rel_path = collision_safe_artifact_path(entity_dir, rel_path)

        blocked = await runtime_guard_file_mutation(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name="sandbox_save_result",
            action=FileMutationAction.SAVE_FILE,
            paths=[rel_path],
            approval_token=kwargs.get("approval_token"),
            content_preview={
                "save_as": rel_path,
                "source": file_path or url,
                "bytes": len(content_bytes),
            },
            approval_payload={
                "save_as": rel_path,
                "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            },
        )
        if blocked:
            return blocked

        # Temporary editor previews may remain filesystem-only. Once the caller
        # labels a result as a visible/final Artifact, however, Knowledge
        # registration is part of the delivery contract and cannot be disabled.
        knowledge_sync_enabled = (
            display_as_artifact
            or artifact_role == "final"
            or _coerce_bool(save_to_knowledge, True)
        )
        if not knowledge_sync_enabled:
            try:
                target = runtime_write_entity_file_atomic(
                    entity_id,
                    rel_path,
                    content_bytes,
                    expected_size=len(content_bytes),
                    allow_empty=False,
                    require_missing=True,
                )
            except Exception as exc:  # noqa: BLE001
                return _sandbox_error(
                    SandboxToolErrorCode.FILESYSTEM_UNAVAILABLE,
                    f"Entity filesystem is not available: {exc}",
                )
            rel_path = _os.path.relpath(target, entity_dir).replace(_os.sep, "/")
            mime_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
            return json.dumps({
                "saved": True,
                "saved_to_knowledge": False,
                "display_as_artifact": display_as_artifact,
                "artifact_role": artifact_role,
                "name": _os.path.basename(target),
                "file_size": len(content_bytes),
                "mime_type": mime_type,
                "fs_path": rel_path,
                "result_url": f"/api/v1/fs/{entity_id}/{rel_path}",
                "message": f"File '{_os.path.basename(target)}' saved for this run but not registered in Knowledge.",
                **({"quality_gate": quality_evidence} if quality_evidence else {}),
            })

        try:
            content_sha256 = hashlib.sha256(content_bytes).hexdigest()
            async with RuntimeFileProjectionTransactionFactory.create(
                entity_id,
            ) as transaction:
                target = transaction.write_bytes(
                    rel_path,
                    content_bytes,
                    expected_content_sha256=content_sha256,
                    expected_size=len(content_bytes),
                    allow_empty=False,
                    require_missing=True,
                )
                sync = await transaction.project_file(
                    abs_path=target,
                    entity_root=entity_dir,
                    source="sandbox",
                    created_by=runtime_context.user_id or "ai-agent",
                    force=True,
                    workspace_id=runtime_context.workspace_id,
                    task_id=runtime_context.task_id,
                    agent_id=runtime_context.agent_id,
                    conversation_id=runtime_context.conversation_id,
                    user_id=runtime_context.user_id,
                    tool_name="sandbox_save_result",
                    expected_content_sha256=content_sha256,
                )
                await transaction.commit()
            rel_path = _os.path.relpath(target, entity_dir).replace(_os.sep, "/")
            canonical_path = getattr(sync, "fs_path", None) or rel_path
            saved_name = getattr(sync, "name", None) or _os.path.basename(target)
            file_size = getattr(sync, "file_size", None)
            mime_type = getattr(sync, "mime_type", None)
            if file_size is None:
                file_size = len(content_bytes)
            if not mime_type:
                mime_type = mimetypes.guess_type(saved_name)[0] or "application/octet-stream"
            logger.info(
                "[sandbox] save_result: registered %s → doc=%s",
                filename,
                sync.document_id,
            )
            return json.dumps({
                "saved": True,
                "saved_to_knowledge": True,
                "display_as_artifact": display_as_artifact,
                "artifact_role": artifact_role,
                "document_id": sync.document_id,
                "name": saved_name,
                "file_size": file_size,
                "mime_type": mime_type,
                "fs_path": canonical_path,
                "result_url": f"/api/v1/fs/{entity_id}/{canonical_path}",
                "message": f"File '{saved_name}' saved to knowledge base.",
                **({"quality_gate": quality_evidence} if quality_evidence else {}),
            })
        except RuntimeFileCommitError as exc:
            return _sandbox_error(
                SandboxToolErrorCode.FILESYSTEM_UNAVAILABLE,
                f"Entity filesystem is not available: {exc}",
            )
        except RuntimeFileProjectionError as exc:
            return _sandbox_error(
                SandboxToolErrorCode.DOCUMENT_SYNC_FAILED,
                "Document sync failed and the downloaded file was rolled back: "
                f"{exc.reason}",
            )
        except Exception as exc:
            logger.exception("[sandbox] save_result doc register failed: %s", exc)
            return _sandbox_error(
                SandboxToolErrorCode.DOCUMENT_SYNC_FAILED,
                f"File was not committed to the document store: {exc}",
            )


_SANDBOX_SAVE_RESULT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_save_result",
        "description": "Save sandbox output; PPTX/DOCX final exports are gated.",
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID."},
                "file_path": {"type": "string", "description": "Absolute sandbox path."},
                "url": {"type": "string", "description": "External http(s) URL."},
                "filename": {"type": "string", "description": "Saved filename."},
                "save_to_knowledge": {
                    "type": "boolean",
                    "description": "Register as Knowledge; default true.",
                },
                "display_as_artifact": {
                    "type": "boolean",
                    "description": "Show card for final deliverable.",
                },
                "artifact_role": {
                    "type": "string",
                    "enum": ["intermediate", "final"],
                    "description": "final shows card; default intermediate.",
                },
                "approval_token": {"type": "string", "description": "Approval token when required."},
            },
            "required": ["filename"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_destroy
# ────────────────────────────────────────────────────────────────

async def _sandbox_destroy(
    entity_id: str = "",
    user_id: str = "",
    sandbox_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    sandbox_id = sandbox_id.strip()
    if not sandbox_id:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id is required.",
        )

    access_error = await _sandbox_instance_access_error(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=runtime_context.agent_id,
        conversation_id=conversation_id,
    )
    if access_error:
        return access_error

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            await client.destroy(sandbox_id=sandbox_id)
        finally:
            await client.close()

        if conversation_id:
            await _delete_ctx(conversation_id)
        await _mark_sandbox_released(sandbox_id)
        logger.info("[sandbox] destroyed: sandbox=%s", sandbox_id)
        return f"Sandbox {sandbox_id} destroyed."
    except Exception as exc:
        from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
        if isinstance(exc, SandboxNotFoundError):
            if conversation_id:
                await _delete_ctx(conversation_id)
            await _mark_sandbox_released(sandbox_id)
            logger.info("[sandbox] destroy ignored; sandbox already gone: sandbox=%s", sandbox_id)
            return f"Sandbox {sandbox_id} was already destroyed."
        logger.exception("[sandbox] destroy failed: sandbox=%s error=%s", sandbox_id, exc)
        return _sandbox_error(
            SandboxToolErrorCode.DESTROY_FAILED,
            f"Sandbox destroy failed: {exc}",
        )


_SANDBOX_DESTROY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_destroy",
        "description": (
            "Destroy a sandbox and release its resources. Call at most once when "
            "you are done with a sandbox; do not call again after it succeeds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID to destroy"},
            },
            "required": ["sandbox_id"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────

SANDBOX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox",
        "description": (
            "Run an isolated sandbox lifecycle through one action gateway. "
            "Use create, then exec/status/respond/cancel/read_file/write_file/save_result as needed, and "
            "destroy once when finished. Allowed action values are exactly create, "
            "exec, status, respond, cancel, read_file, write_file, save_result, and destroy; do not substitute "
            "run, shell, execute, exec_command, or command. Create always returns a "
            "blank sandbox; use invoke_skill when a stored Skill is required."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(SandboxToolAction.values()),
                    "description": "Sandbox operation to perform.",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action-specific parameters. create: no params. "
                        "exec: sandbox_id, command, timeout?, background?; background "
                        "scripts may use $MANOR_SANDBOX_BRIDGE emit/wait for "
                        "structured Agent interaction. "
                        "status: sandbox_id, execution_id, after_sequence?. "
                        "respond: sandbox_id, execution_id, event_id, payload?, message?. "
                        "cancel: sandbox_id, execution_id. "
                        "read_file: sandbox_id, path, offset?/limit?. write_file: "
                        "sandbox_id, path plus exactly one of content/workspace_path. "
                        "save_result: filename plus sandbox_id/file_path or url. "
                        "destroy: sandbox_id."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
        },
    },
}


async def _sandbox_handler(
    entity_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    raw_action = str(kwargs.get("action") or "").strip()
    raw_params = kwargs.get("params")
    if raw_params is None:
        raw_params = {}
    if not raw_action:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "action is required",
        )
    if not isinstance(raw_params, dict):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "params must be an object",
        )

    try:
        action = SandboxToolAction(raw_action)
    except ValueError:
        return _sandbox_error(
            SandboxToolErrorCode.UNSUPPORTED_ACTION,
            f"unsupported sandbox action: {raw_action}",
        )
    handlers = {
        SandboxToolAction.CREATE: _sandbox_create,
        SandboxToolAction.EXEC: _sandbox_exec,
        SandboxToolAction.STATUS: _sandbox_status,
        SandboxToolAction.RESPOND: _sandbox_respond,
        SandboxToolAction.CANCEL: _sandbox_cancel,
        SandboxToolAction.READ_FILE: _sandbox_read_file,
        SandboxToolAction.WRITE_FILE: _sandbox_write_file,
        SandboxToolAction.SAVE_RESULT: _sandbox_save_result,
        SandboxToolAction.DESTROY: _sandbox_destroy,
    }
    handler = handlers.get(action)
    if handler is None:
        return _sandbox_error(
            SandboxToolErrorCode.UNSUPPORTED_ACTION,
            f"unsupported sandbox action: {raw_action}",
        )

    # Runtime-injected context is authoritative. Never let nested model params
    # replace actor, scope, approval, or envelope values.
    normalized_params = RuntimeCompositeToolCallFactory.sandbox_params(kwargs)
    handler_kwargs = {
        key: value
        for key, value in normalized_params.items()
        if key not in RUNTIME_TOOL_CONTEXT_KEYS
        and key not in {"entity_id", "user_id"}
    }
    handler_kwargs.update(
        {
            key: value
            for key, value in kwargs.items()
            if key in RUNTIME_TOOL_CONTEXT_KEYS
        }
    )
    return await handler(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        **handler_kwargs,
    )

async def destroy_all_sandboxes() -> None:
    """Destroy all active sandboxes (called on app shutdown as a safety net)."""
    if not _sandbox_available():
        return
    try:
        client = _get_client()
        try:
            sandboxes = await client.list()
            for sbx in sandboxes:
                try:
                    await client.destroy(sbx.sandbox_id)
                    logger.info("[sandbox] shutdown cleanup: destroyed %s", sbx.sandbox_id)
                except Exception as exc:
                    logger.warning("[sandbox] shutdown cleanup failed for %s: %s", sbx.sandbox_id, exc)
        finally:
            await client.close()
    except Exception as exc:
        logger.warning("[sandbox] shutdown cleanup error: %s", exc)


def get_tools() -> list[tuple[dict, Any]]:
    """Return sandbox tool (schema, handler) pairs.

    Returns an empty list when SANDBOX_SERVICE_URL is not set so no
    sandbox tools appear in the pool on non-sandbox deployments.
    """
    if not _sandbox_available():
        logger.debug("[sandbox] SANDBOX_SERVICE_URL not set — sandbox tools disabled")
        return []
    logger.info("[sandbox] loading composite sandbox tool (service=%s)", SANDBOX_SERVICE_URL)
    return [(SANDBOX_SCHEMA, _sandbox_handler)]


def get_legacy_tools() -> list[tuple[dict, Any]]:
    """Return execution-only aliases for persisted calls and tool bindings."""

    if not _sandbox_available():
        return []
    return [
        (_SANDBOX_CREATE_SCHEMA, _sandbox_create),
        (_SANDBOX_EXEC_SCHEMA, _sandbox_exec),
        (_SANDBOX_STATUS_SCHEMA, _sandbox_status),
        (_SANDBOX_RESPOND_SCHEMA, _sandbox_respond),
        (_SANDBOX_CANCEL_SCHEMA, _sandbox_cancel),
        (_SANDBOX_READ_FILE_SCHEMA, _sandbox_read_file),
        (_SANDBOX_WRITE_FILE_SCHEMA, _sandbox_write_file),
        (_SANDBOX_SAVE_RESULT_SCHEMA, _sandbox_save_result),
        (_SANDBOX_DESTROY_SCHEMA, _sandbox_destroy),
    ]
