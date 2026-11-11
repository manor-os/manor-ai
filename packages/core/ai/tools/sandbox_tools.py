"""
Sandbox tools — expose Docker-based sandbox lifecycle to the LLM.

One ``sandbox`` tool is registered when a local or coordinated runner is
configured. Its action enum covers lifecycle, background execution status,
file access, result projection, and cancellation. The former tool names remain
execution-only compatibility aliases.

Sandbox bindings (which sandbox each caller may operate, one slot per owner,
conversation or task binding key, agent, and sandbox kind) are tracked via
Redis so sessions survive across chat turns. Admission fails closed when a
binding cannot be persisted, and a call without a binding scope is denied.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import shlex
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Any

from packages.core.ai.runtime.artifact_save import project_bytes_into_knowledge
from packages.core.ai.runtime.file_images import FILE_IMAGE_DELIVERY_CONTRACT
from packages.core.ai.runtime.file_actions import (
    RuntimeFileCommitError,
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_mutation,
    runtime_write_entity_file_atomic,
)
from packages.core.ai.runtime.workspace_reads import runtime_read_workspace_file
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.composite_tools import (
    RuntimeCompositeToolCallFactory,
    SandboxToolAction,
    SandboxToolErrorCode,
)
from packages.core.ai.runtime.sandbox_toolset import sandbox_toolset_summary
from packages.core.services.runtime_activity import sandbox_activity_recorder
from packages.core.ai.runtime.sandbox import (
    RUNTIME_SANDBOX_LAYOUT_NOTE,
    RuntimeSandboxKind,
    RuntimeSandboxScope,
    primary_sandbox_scope_for,
    runtime_agent_sandbox,
    runtime_agent_skill_in,
    runtime_agent_viewed_skill_in,
    runtime_binding_is_live,
    runtime_binding_skills,
    runtime_binding_was_released,
    runtime_load_sandbox_binding,
    runtime_sandbox_agent_workdir,
    runtime_sandbox_credentials_refused,
)
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_tool_call_context_from_handler,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.services.sandbox_session_service import (
    GENERIC_SANDBOX_REUSE_SCOPE,
    GENERIC_SANDBOX_SKILL_NAME,
    SandboxBinding,
    SandboxBundle,
    SandboxOwner,
    SandboxSession,
    SandboxSessionError,
    acquire_sandbox_session,
    attach_durable_reservation,
    forget_vanished_sandbox,
    release_sandbox_session,
)
from packages.core.services import sandbox_session_service as _sessions
from packages.core.services.sandbox_sdk.capabilities import (
    EXEC_START,
    SKILL_SLOTS,
    WORK_AREAS,
    RunnerCapabilities,
    outdated_message,
)
from packages.core.services.sandbox_sdk.exceptions import SandboxRunnerOutdatedError
from packages.core.services.generated_media_naming import (
    collision_safe_artifact_path,
    resolve_workspace_artifact_base_dir,
    scope_workspace_artifact_path,
)
from packages.core.services.workspace_layout import WorkspaceArtifactDir
from packages.core.services.file_refs import file_ref
from packages.core.services.generated_file_refs import entity_fs_open_url

logger = logging.getLogger(__name__)


_GENERIC_SANDBOX_NAME = GENERIC_SANDBOX_SKILL_NAME
# What the model needs to know about sharing, kept short (tool result and
# schema): one sandbox per user, each conversation or task in its own area.
_SHARED_SANDBOX_NOTE = RUNTIME_SANDBOX_LAYOUT_NOTE
_GENERIC_SANDBOX_FILES = {
    "README.md": (
        "# Generic sandbox\n\n"
        "Temporary isolated workspace for ad hoc command execution.\n"
    ),
}


# Manor's own storage failed the save: no call the agent makes repairs it.
_PLATFORM_FAULT_ERROR_CODES = frozenset({
    SandboxToolErrorCode.FILESYSTEM_UNAVAILABLE,
    SandboxToolErrorCode.WORKSPACE_SCOPE_UNAVAILABLE,
    SandboxToolErrorCode.DOCUMENT_SYNC_FAILED,
})


def _sandbox_error(code: SandboxToolErrorCode, message: str, *, internal_error: bool = False) -> str:
    """Encode every sandbox failure with the shared machine-readable contract.

    A platform fault carries the shared ``internal_error`` marker
    (``tool_error_payload``): the run records it, and a Workflow step it
    stopped is retried by the shared step resilience instead of asking anyone.
    """

    error: dict[str, Any] = {"code": code.value, "message": message}
    if internal_error or code in _PLATFORM_FAULT_ERROR_CODES:
        error.update(internal_error=True, requires_human=False, retryable=False)
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def _save_not_applied(error: str) -> str:
    """A save refused before it wrote anything to Manor: host proof of no effect.

    Minted only where ``save_result`` returns before its file mutation. Gate
    commands write only their own report inside the sandbox. A failure after
    the write started stays unconfirmed.
    """
    from packages.core.ai.runtime.tool_results import RuntimeSettledToolResult

    return RuntimeSettledToolResult.not_applied(error, "before_save")


def _runner_outdated_error(exc: BaseException) -> str:
    """The runner lacks what this call needs: nothing ran, and retrying will not help."""

    logger.warning("[sandbox] runner outdated: %s", exc)
    return _sandbox_error(SandboxToolErrorCode.RUNNER_OUTDATED, str(exc))


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


# ────────────────────────────────────────────────────────────────
# Sandbox bindings (Redis-backed, owned by owner x conversation)
# ────────────────────────────────────────────────────────────────

def _caller_scope(runtime_context: Any, entity_id: str | None = None) -> RuntimeSandboxScope | None:
    """The owner x conversation scope this call operates sandboxes in.

    Built from the sandbox *binding key* -- the root conversation id for a
    human run (delegates and sub-agents run in their parent's conversation),
    the synthetic task key for user-less and conversation-less runs -- never
    from a handler's ``conversation_id`` argument alone: that is empty for a
    worker run. Everything in a handler that is not sandbox scope
    (file-mutation guards, knowledge-sync provenance) keeps the real
    conversation id. The acting agent is NOT part of the scope: every agent
    of the conversation may operate its sandboxes.
    """

    if entity_id and not getattr(runtime_context, "entity_id", None):
        # A handler whose context came from ``runtime_tool_call_context_from_handler``
        # carries the entity only as its explicit argument.
        from dataclasses import replace

        runtime_context = replace(runtime_context, entity_id=entity_id)
    return primary_sandbox_scope_for(runtime_context)


async def _load_binding(scope: RuntimeSandboxScope, sandbox_id: str) -> dict | None:
    """The conversation's binding record for ``sandbox_id`` (None: not this scope's)."""

    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    return ctx if scope.owns(ctx) else None


def _scope_owner(scope: RuntimeSandboxScope) -> SandboxOwner:
    return SandboxOwner(entity_id=scope.entity_id, user_id=scope.user_id, workspace_id=scope.workspace_id)


_NO_SANDBOX_SCOPE_MESSAGE = (
    "Sandbox access denied: this run has no sandbox binding scope (it has neither "
    "a conversation nor a task to bind a sandbox to, or no entity). Sandboxes can "
    "only be opened and operated from a conversation or task run."
)


async def _sandbox_instance_access_error(
    *,
    sandbox_id: str,
    scope: RuntimeSandboxScope | None,
) -> str | None:
    """Allow a model-supplied sandbox id only if it belongs to the caller's scope.

    The sandbox must be bound to the same owner (user, or the workspace of
    user-less work) in the same conversation (or task) the acting principal
    runs in. It does NOT have to be the acting agent's own: any agent of the
    conversation may operate the conversation's sandboxes, while another
    user, or the same user in another conversation, never can. There is no
    unscoped path: a call without a scope is denied, never waved through.
    """

    if scope is None:
        return _sandbox_error(SandboxToolErrorCode.ACCESS_DENIED, _NO_SANDBOX_SCOPE_MESSAGE)
    ctx = await _load_binding(scope, sandbox_id)
    if runtime_binding_is_live(ctx, sandbox_id) and scope.owns(ctx):
        if await runtime_sandbox_credentials_refused(
            ctx, entity_id=scope.entity_id, user_id=scope.user_id or None,
        ):
            # Every sandbox tool call names its sandbox by id and passes here, so
            # a collaborator's run neither drives nor reattaches to it.
            return _sandbox_error(
                SandboxToolErrorCode.ACCESS_DENIED,
                "This sandbox holds a skill's stored credentials and is not available to "
                "runs involving an external collaborator.",
            )
        return None
    if _expired_binding(ctx, sandbox_id):
        return _sandbox_expired_error(
            sandbox_id,
            str(ctx.get("expired_skill") or ""),
            bool(ctx.get("skill_id")),
        )
    return _sandbox_error(
        SandboxToolErrorCode.ACCESS_DENIED,
        f"Sandbox access denied: sandbox {sandbox_id} does not belong to this conversation. "
        "Use the sandbox_id returned by sandbox action='create' or invoke_skill in this conversation.",
    )


def _expired_binding(ctx: Any, sandbox_id: str) -> bool:
    """True when this scope's binding records ``sandbox_id`` as reclaimed."""

    return (
        isinstance(ctx, dict)
        and not str(ctx.get("sandbox_id") or "")
        and str(ctx.get("expired_sandbox_id") or "") == sandbox_id
    )


def _sandbox_expired_error(sandbox_id: str, skill: str, governed: bool) -> str:
    # The generic sandbox is registered under a placeholder skill name; only
    # a real Skill can be re-opened through invoke_skill.
    if skill == _GENERIC_SANDBOX_NAME:
        skill, governed = "", False
    if skill:
        reopen = (
            f"invoke_skill(skill='{skill}') (or repeat the generate_file call that opened it)"
        )
    elif governed:
        reopen = "invoke_skill with the same skill (or repeat the generate_file call that opened it)"
    else:
        reopen = "sandbox action='create'"
    return _sandbox_error(
        SandboxToolErrorCode.EXPIRED,
        f"Sandbox {sandbox_id} no longer exists: it was reclaimed after sitting idle, "
        "and every file that was only inside it is gone. Do not retry with this "
        f"sandbox_id. Open a fresh sandbox with {reopen}, use the new sandbox_id it "
        "returns, and redo the setup steps (for example project init) before continuing.",
    )


async def _sandbox_is_gone(sandbox_id: str) -> bool:
    """Ask the runner whether the sandbox itself exists; only a definite no counts."""

    from packages.core.services.sandbox_queue_service import SandboxRouteNotFoundError
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    try:
        client = await _get_client_for_sandbox(sandbox_id)
    except SandboxRouteNotFoundError:
        return True
    except Exception:  # noqa: BLE001 - unknown is not gone
        return False
    try:
        await client.status(sandbox_id)
    except SandboxNotFoundError:
        return True
    except Exception:  # noqa: BLE001 - a blip is not proof of death
        return False
    finally:
        await client.close()
    return False


async def _runner_failure_error(
    exc: Exception,
    *,
    sandbox_id: str,
    scope: RuntimeSandboxScope | None,
) -> str | None:
    """A runner failure with its own answer: an outdated runner, or a vanished sandbox."""

    if isinstance(exc, SandboxRunnerOutdatedError):
        return _runner_outdated_error(exc)
    return await _vanished_sandbox_error(exc, sandbox_id=sandbox_id, scope=scope)


async def _vanished_sandbox_error(
    exc: Exception,
    *,
    sandbox_id: str,
    scope: RuntimeSandboxScope | None,
) -> str | None:
    """Map a failure caused by a reclaimed container to ``sandbox_expired``.

    A 404 from the runner may concern an execution or a file inside a live
    sandbox, so the sandbox itself is probed before anything is unbound. A
    missing runner route means its instance row was released and no warm
    record points at it, which only happens to destroyed containers.
    """

    from packages.core.services.sandbox_queue_service import SandboxRouteNotFoundError
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    if isinstance(exc, SandboxRouteNotFoundError):
        gone = True
    elif isinstance(exc, SandboxNotFoundError):
        gone = await _sandbox_is_gone(sandbox_id)
    else:
        return None
    if not gone:
        return None
    ctx = await _load_binding(scope, sandbox_id) if scope is not None else None
    governed = runtime_binding_is_live(ctx, sandbox_id) and bool(str(ctx.get("skill_id") or ""))
    skill = await forget_vanished_sandbox(sandbox_id, scope=scope)
    logger.warning("[sandbox] sandbox=%s vanished from its runner; binding dropped", sandbox_id)
    return _sandbox_expired_error(sandbox_id, skill or "", governed)


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


def _sandbox_uses_durable_admission() -> bool:
    """Durable runs on external runners admit sandboxes through the reservation queue."""

    try:
        from packages.core.config import get_settings

        settings = get_settings()
        return (
            settings.MANOR_RUNTIME_EXECUTION_MODE == "durable"
            and settings.SANDBOX_COORDINATION_MODE == "external-runner"
        )
    except Exception:
        return False


@asynccontextmanager
async def _sandbox_admission_db():
    from packages.core.database import async_session

    async with async_session() as db:
        yield db


def _get_client():
    from packages.core.services.sandbox_session_service import sandbox_runner_client

    return sandbox_runner_client(_get_sandbox_service_url(), timeout=180.0)


async def _get_client_for_sandbox(sandbox_id: str):
    from packages.core.config import get_settings
    from packages.core.services.sandbox_session_service import sandbox_client_for

    if get_settings().SANDBOX_COORDINATION_MODE != "external-runner":
        return _get_client()
    return await sandbox_client_for(sandbox_id, timeout=180.0)


async def _agent_exec_placement(
    scope: RuntimeSandboxScope | None,
    agent_id: str | None,
    sandbox_id: str,
    *,
    capabilities: RunnerCapabilities | None = None,
) -> dict[str, Any]:
    """Where one agent's command runs in its owner's shared sandbox, and with which env.

    The agent that invoked a Skill loaded in this sandbox (in this
    conversation or task) sees that Skill as /skill: it starts in the Skill's
    directory with the Skill's credentials. Every other agent -- and every
    other conversation or task of the user -- runs in its own work-area
    directory (``/workspace/c/<work>/agents/<agent>``, created on demand) with
    no Skill view and none of a Skill's credentials: credentials reach only
    the agents that invoked the Skill, per command, never persisted in the
    container.

    An older runner (no ``work_areas`` / ``skill_slots``) has neither the
    work areas nor per-command Skill views: every command runs in its
    default workdir, as before those existed. It cannot keep a Skill's
    credentials from another agent either, so another agent's command in a
    container that holds credentials is refused (``SandboxRunnerOutdatedError``).
    """

    held = await runtime_agent_skill_in(scope, agent_id, sandbox_id) if scope is not None else None
    if capabilities is None:
        capabilities = await _sessions.runner_capabilities_for_sandbox(sandbox_id)
    missing = capabilities.missing(WORK_AREAS, SKILL_SLOTS)
    if missing:
        if held is None and await _sessions.sandbox_holds_credentials(sandbox_id) is not False:
            raise SandboxRunnerOutdatedError(
                outdated_message(
                    [SKILL_SLOTS],
                    build_version=capabilities.build_version,
                    what="Running a command next to another agent's credentialed Skill",
                ),
                missing=[SKILL_SLOTS],
                build_version=capabilities.build_version,
            )
        # The runner's own default workdir and its single /skill view.
        return {}
    if held is not None and held.get("skill_key"):
        return {"skill": held["skill_key"]}
    if held is not None:
        return {}
    return {
        "workdir": runtime_sandbox_agent_workdir(agent_id, scope.binding_key if scope is not None else None),
        "skill_env": False,
        "skill": "",
    }


def runner_skill_slot(skill_name: str | None) -> str:
    """The runner's directory name for a Skill (``sandbox/docker_backend.skill_slot``).

    Kept identical to the runner's; ``tests/test_sandbox_per_user_layout.py``
    holds them together.
    """

    raw = str(skill_name or "").strip().lower()
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in raw).strip(".-")
    return safe[:64] or "skill"


def _skill_view_path(path: str, skill_view: str | None) -> str:
    """Map the viewed Skill's real directory (``/skills/<slot>``) back to ``/skill``."""

    if not path or not skill_view:
        return path
    real = f"/skills/{runner_skill_slot(skill_view)}"
    if path == real:
        return "/skill"
    if path.startswith(real + "/"):
        return "/skill" + path[len(real):]
    return path


def _view_kwargs(skill_view: str | None) -> dict[str, Any]:
    """The ``skill`` argument of a runner call (omitted: the runner's legacy single view)."""

    return {} if skill_view is None else {"skill": skill_view}


async def _agent_file_view(
    scope: RuntimeSandboxScope | None,
    agent_id: str | None,
    sandbox_id: str,
) -> tuple[str | None, str]:
    """(Skill view, base directory for relative paths) of one agent's file operations.

    The agent that invoked a Skill reads and writes through that Skill's
    view. Any other agent's relative paths land in its own work-area
    directory; an absolute ``/skill/...`` path it names is read through the
    runner's default view (the Skill loaded last), as before Skills coexisted
    -- file operations carry no credentials.
    """

    placement = await _agent_exec_placement(scope, agent_id, sandbox_id)
    if "workdir" in placement:
        return None, str(placement["workdir"])
    return placement.get("skill"), "/skill"


def _is_runner_busy(exc: BaseException) -> bool:
    """The runner's typed ``sandbox_busy`` 409 (too many concurrent commands, or a Skill load)."""

    return getattr(exc, "status_code", None) == 409 and getattr(exc, "code", None) == "sandbox_busy"


def _runtime_sandbox_execution_id(runtime_context: Any, sandbox_id: str) -> str:
    """Name every Sandbox command, so the runner can always stop it.

    The runner kills a timed-out or cancelled command by its execution id;
    a command sent without one only loses its ``docker exec`` client and keeps
    running inside the container. A call that a checkpoint may replay keeps a
    deterministic id (run, call, attempt) so the runner deduplicates it; any
    other call gets a fresh one.
    """

    run_id = str(getattr(runtime_context, "runtime_run_id", None) or "")
    tool_call_id = str(getattr(runtime_context, "runtime_tool_call_id", None) or "")
    if not tool_call_id:
        return f"manor-{uuid.uuid4().hex}"
    attempt = max(1, int(getattr(runtime_context, "runtime_tool_attempt", 1) or 1))
    scope = run_id or f"sandbox:{sandbox_id}"
    digest = hashlib.sha256(f"{scope}:{tool_call_id}:{attempt}".encode("utf-8")).hexdigest()[:32]
    return f"manor-{digest}"


async def _stop_execution(sandbox_id: str, execution_id: str) -> bool:
    """Stop a command by name after its call gave up; True when confirmed.

    The runner kills a command that hits its own limit, but the HTTP call can
    give up first, and only an explicit cancel stops the command then.
    Cancelling one that already ended is harmless.
    """

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            await client.cancel_execution(sandbox_id, execution_id)
        finally:
            await client.close()
        return True
    except Exception as exc:
        if type(exc).__name__ == "SandboxNotFoundError" and "execution" in str(exc).lower():
            return True  # never started, or already ended and pruned
        logger.warning(
            "[sandbox] could not stop timed-out execution sandbox=%s execution=%s: %s",
            sandbox_id, execution_id, exc,
        )
        return False


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


# ────────────────────────────────────────────────────────────────
# Workspace input collection
# ────────────────────────────────────────────────────────────────

async def _read_workspace_bytes(
    entity_id: str,
    workspace_path: str,
    *,
    conversation_id: str | None,
    user_id: str | None,
    workspace_id: str | None,
    runtime_envelope: Any | None,
) -> tuple[bytes | None, str | None]:
    """``(bytes, None)``, ``(None, blocked_json)``, or ``(None, None)`` when unreadable."""

    read = await runtime_read_workspace_file(
        entity_id=entity_id,
        workspace_path=workspace_path,
        tool_name="sandbox",
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        runtime_envelope=runtime_envelope,
    )
    if read.status == "ok":
        return read.content, None
    if read.status == "denied":
        return None, read.blocked
    return None, None


# ────────────────────────────────────────────────────────────────
# sandbox_create
# ────────────────────────────────────────────────────────────────

def generic_sandbox_bundle() -> SandboxBundle:
    """The bundle every generic sandbox (``sandbox`` tool and ``bash``) is admitted with."""

    return SandboxBundle(
        skill_name=_GENERIC_SANDBOX_NAME,
        files=dict(_GENERIC_SANDBOX_FILES),
        env={},
        allowed_sensitive_keys=(),
        source="workspace",
        config=None,
        auto_install=False,
        reuse_scope=GENERIC_SANDBOX_REUSE_SCOPE,
    )


async def acquire_generic_sandbox(
    *,
    scope: RuntimeSandboxScope,
    runtime_context: Any,
    tool_call_id: str | None = None,
) -> "SandboxSession | SandboxSessionError | Any":
    """Admit (or reuse) the caller's generic sandbox: the owner's general sandbox.

    One path for the ``sandbox`` tool and ``bash``: same owner, binding scope
    and sandbox kind, so a command run by either sees the other's state. A
    durable run under external-runner coordination queues a reservation and
    gets back a suspension (the call is re-executed on resume and attaches
    the allocated container); a task worker waits on its task reservation.
    ``tool_call_id`` overrides the call's reservation key (a replacement
    admission inside a call that already has a ticket). Raises
    ``ValueError`` for an invalid owner.
    """

    owner = _scope_owner(scope)
    bundle = generic_sandbox_bundle()
    call_id = runtime_context.runtime_tool_call_id if tool_call_id is None else tool_call_id
    binding = SandboxBinding(
        conversation_id=scope.binding_key,
        agent_id=runtime_context.agent_id or "",
        skill_id="",
        task_id=runtime_context.task_id or None,
        workflow_run_id=runtime_context.workflow_run_id or None,
        runtime_run_id=runtime_context.runtime_run_id or None,
        runtime_tool_call_id=call_id or None,
        parent_skill=runtime_context.parent_skill or None,
    )
    if _sandbox_uses_durable_admission() and binding.runtime_run_id and binding.runtime_tool_call_id:
        # Same admission as a Skill sandbox: a durable run queues a runner
        # reservation and is resumed by re-executing this call, which then
        # attaches the container the scheduler allocated.
        async with _sandbox_admission_db() as db:
            session = await attach_durable_reservation(
                owner=owner, bundle=bundle, binding=binding, db=db,
            )
            if session is None:
                session = await acquire_sandbox_session(
                    owner=owner, bundle=bundle, binding=binding, db=db,
                )
        return session
    return await acquire_sandbox_session(owner=owner, bundle=bundle, binding=binding)


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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
    skill_id = str(skill_id or "").strip()
    if skill_id:
        return _sandbox_error(
            SandboxToolErrorCode.SKILL_REQUIRES_INVOKE,
            "Stored Skills must be opened with invoke_skill so Skill permissions "
            "and bindings are enforced. Omit skill_id to create a generic sandbox.",
        )
    if not entity_id:
        return _sandbox_error(
            SandboxToolErrorCode.ACCESS_DENIED,
            "Sandbox creation requires an entity context.",
        )
    scope = _caller_scope(runtime_context, entity_id)
    if scope is None:
        return _sandbox_error(SandboxToolErrorCode.ACCESS_DENIED, _NO_SANDBOX_SCOPE_MESSAGE)
    # Governance: while this agent drives a governed Skill in this
    # conversation, a blank sandbox would let the model step around the
    # Skill's workflow. Only this agent's own Skill counts; another agent
    # driving a Skill in the shared sandbox never blocks it.
    try:
        driving = await runtime_agent_sandbox(scope, runtime_context.agent_id, RuntimeSandboxKind.SKILL)
    except Exception as exc:
        return _sandbox_error(
            SandboxToolErrorCode.CREATE_FAILED,
            f"Sandbox owner context could not be loaded: {exc}",
        )
    active_skill_sandbox = driving[0] if driving is not None else ""
    if active_skill_sandbox:
        return _sandbox_error(
            SandboxToolErrorCode.ALREADY_ACTIVE,
            f"This agent already has an active governed Skill sandbox ({active_skill_sandbox}) "
            "in this conversation. Continue the Skill's workflow in that sandbox. A generic "
            "sandbox can be created only after that sandbox is released with sandbox "
            f"action='destroy' sandbox_id='{active_skill_sandbox}'; finishing or saving a "
            "result does not release it.",
        )

    try:
        session = await acquire_generic_sandbox(scope=scope, runtime_context=runtime_context)
    except ValueError as exc:
        return _sandbox_error(SandboxToolErrorCode.ACCESS_DENIED, f"Invalid sandbox owner: {exc}")
    if isinstance(session, SandboxSessionError):
        if session.code == "capacity_full":
            logger.warning("[sandbox] capacity full: kind=generic error=%s", session.message)
            return _sandbox_error(
                SandboxToolErrorCode.CAPACITY_FULL,
                f"Sandbox capacity is full. Please retry later. Details: {session.message}",
            )
        logger.warning("[sandbox] create failed: kind=generic code=%s", session.code)
        return _sandbox_error(
            SandboxToolErrorCode.CREATE_FAILED,
            f"Sandbox creation failed: {session.message}",
        )
    if not isinstance(session, SandboxSession):
        return session

    # An older runner has no work areas: commands run in its own workdir.
    legacy_layout = (await _sessions.runner_capabilities(session.base_url)).lacks(WORK_AREAS)
    parts = [
        f"sandbox_id: {session.sandbox_id}",
        "status: ready" if session.reused else "status: running",
        "workdir: "
        + (
            session.workdir
            if legacy_layout
            else runtime_sandbox_agent_workdir(runtime_context.agent_id, scope.binding_key)
        ),
        "sandbox_kind: generic",
    ]
    if not legacy_layout:
        parts.append(f"shared: {_SHARED_SANDBOX_NOTE}")
    if session.reused:
        parts.append("reused: true")
    if session.entry_hint:
        parts.append(f"entry_hint: {session.entry_hint}")
    if session.scripts:
        parts.append(f"scripts: {', '.join(session.scripts)}")
    if session.requirements_installed:
        parts.append("dependencies: installed from requirements.txt")
    parts.append("credentials_injected: (none)")
    if session.env_blocked:
        parts.append(f"env_blocked: {', '.join(session.env_blocked)}")
    parts.append(
        "workspace_mount: (none; use sandbox action='write_file' with "
        "workspace_path or direct content to inject inputs). Your bash working "
        "copy (workspace/ in your working directory) holds the entity files bash "
        "commands named; files changed there are saved by the next bash command"
    )
    parts.append(
        "NOTE: This is this user's isolated sandbox; other users never see it. Proceed with "
        "sandbox action='exec' or action='write_file'."
    )
    # Generated from the image's toolset manifest, which the release verifies.
    parts.append(sandbox_toolset_summary())
    logger.info(
        "[sandbox] %s: kind=generic sandbox=%s entity=%s",
        "reused" if session.reused else "created",
        session.sandbox_id,
        entity_id,
    )
    return "\n".join(parts)


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
    """Run one command in the owner's shared sandbox; never "busy", never silently lost.

    The command takes a slot of the sandbox -- or waits its turn in the
    runner's fair FIFO: a durable run parks meanwhile (``sandbox_exec_slot``)
    and resumes to collect it; anything else waits in-process up to
    ``SANDBOX_EXEC_QUEUE_WAIT_SECONDS``. A command that never started on a
    sandbox that was lost (or reclaimed) runs on the owner's new general
    sandbox; one that was running when it was lost is reported, never re-run.
    """

    replacement_of = str(kwargs.pop("_sandbox_replacement_of", "") or "")
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
    retry = {
        "entity_id": entity_id, "user_id": user_id, "conversation_id": conversation_id,
        "command": command, "timeout": timeout, "background": background, "kwargs": kwargs,
        "replacement_of": replacement_of,
    }

    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
    if access_error:
        if replacement_of or SandboxToolErrorCode.EXPIRED.value not in access_error:
            return access_error
        if _sandbox_parks_on_slots(runtime_context):
            # A run that parked on this sandbox's slot queue is done with that ticket.
            await _consume_exec_slot_wait(runtime_context)
        # A command for a reclaimed sandbox has not started anywhere yet.
        return await _retry_not_started(
            runtime_context=runtime_context, scope=scope, sandbox_id=sandbox_id,
            fallback=access_error, **retry,
        )
    background = _coerce_bool(background, False)
    capabilities = await _sessions.runner_capabilities_for_sandbox(sandbox_id)
    try:
        placement = await _agent_exec_placement(
            scope, runtime_context.agent_id, sandbox_id, capabilities=capabilities,
        )
    except SandboxRunnerOutdatedError as exc:
        return _runner_outdated_error(exc)
    # A runner without /exec/start runs a command synchronously, as before it
    # had one; a background command cannot run there at all.
    synchronous = capabilities.lacks(EXEC_START)
    if synchronous and background:
        return _runner_outdated_error(
            SandboxRunnerOutdatedError(
                outdated_message(
                    [EXEC_START], build_version=capabilities.build_version, what="A background command",
                )
                + " Run it without background=true instead.",
                missing=[EXEC_START],
            )
        )

    durable = _sandbox_parks_on_slots(runtime_context)
    parked = await _exec_slot_ticket(runtime_context, sandbox_id) if durable else None
    execution_id = (parked or {}).get("execution_id") or _runtime_sandbox_execution_id(runtime_context, sandbox_id)
    queue_wait = _exec_queue_wait_seconds()
    fair_key = scope.binding_key if scope is not None else None
    started = None
    last_status = "queued" if parked else None
    keep_active = False
    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            if not background:
                await _set_runtime_active_execution(
                    runtime_context, sandbox_id=sandbox_id, execution_id=execution_id,
                )
            if synchronous:
                result = await client.exec(
                    sandbox_id=sandbox_id,
                    command=command,
                    timeout=timeout,
                    execution_id=execution_id,
                    **placement,
                )
                started = final = _finished_exec_status(result, sandbox_id=sandbox_id, execution_id=execution_id)
                last_status = "running"
                return _exec_result_text(final, sandbox_id=sandbox_id, command=command, timeout=timeout)
            started = await client.start_exec(
                sandbox_id=sandbox_id,
                command=command,
                timeout=timeout,
                execution_id=execution_id,
                fair_key=fair_key,
                queue_timeout=queue_wait,
                **placement,
            )
            if background:
                try:
                    await _set_runtime_active_execution(
                        runtime_context,
                        sandbox_id=sandbox_id,
                        execution_id=started.execution_id,
                    )
                except Exception:
                    await client.cancel_execution(sandbox_id, started.execution_id)
                    raise
                return _background_started_text(started)

            # "queued" with a queue position: waiting for a slot (a record
            # whose slot was granted at once is "queued" until its task starts).
            waiting_for_slot = started.status == "queued" and getattr(started, "queue_position", None)
            last_status = "queued" if waiting_for_slot else "running"
            if waiting_for_slot and durable:
                suspension = await _park_on_exec_slot(
                    runtime_context, sandbox_id=sandbox_id, execution_id=execution_id, wait_seconds=queue_wait,
                )
                if suspension is not None:
                    # The run stays on record as executing this command, so
                    # cancelling the parked run withdraws it from the queue.
                    keep_active = True
                    return suspension
            final, last_status = await _await_execution(
                client,
                sandbox_id=sandbox_id,
                execution_id=execution_id,
                first=started,
                budget_seconds=queue_wait + timeout + 30,
                on_status=sandbox_activity_recorder(runtime_context),
            )
        except asyncio.CancelledError:
            # This call's run (or the delegate running inside it) was
            # cancelled: stop -- or withdraw from the slot queue -- exactly
            # this command in the shared sandbox, never another's, never the
            # container.
            await asyncio.shield(_stop_execution(sandbox_id, execution_id))
            raise
        finally:
            await client.close()
            if not background and not keep_active:
                await _clear_runtime_active_execution(runtime_context, execution_id)
    except Exception as exc:
        if parked and last_status == "queued":
            await _consume_exec_slot_wait(runtime_context)
        if isinstance(exc, SandboxRunnerOutdatedError):
            # Refused before it was sent: nothing ran, and no retry helps.
            return _runner_outdated_error(exc)
        expired = await _vanished_sandbox_error(exc, sandbox_id=sandbox_id, scope=scope)
        if expired:
            if last_status in (None, "queued") and not replacement_of:
                return await _retry_not_started(
                    runtime_context=runtime_context, scope=scope, sandbox_id=sandbox_id,
                    fallback=expired, forget=False, **retry,
                )
            if last_status in (None, "queued"):
                return expired
            return _sandbox_lost_while_running_error(sandbox_id)
        return await _exec_failure_text(
            exc, sandbox_id=sandbox_id, execution_id=execution_id, timeout=timeout, admitted=started is not None,
        )
    if durable:
        await _consume_exec_slot_wait(runtime_context)
    outcome = str(getattr(final, "outcome", "") or "")
    if outcome == "sandbox_lost" and str(getattr(final, "outcome_detail", "") or "") == "never started":
        if replacement_of:
            return _sandbox_error(
                SandboxToolErrorCode.EXEC_NOT_STARTED,
                f"Sandbox {sandbox_id} was lost before your command started, so it did not run.",
            )
        return await _retry_not_started(
            runtime_context=runtime_context, scope=scope, sandbox_id=sandbox_id, fallback=None, **retry,
        )
    if outcome == "sandbox_lost":
        if scope is not None:
            await forget_vanished_sandbox(sandbox_id, scope=scope)
        return _sandbox_lost_while_running_error(sandbox_id)
    return _exec_result_text(final, sandbox_id=sandbox_id, command=command, timeout=timeout)


def _finished_exec_status(result: Any, *, sandbox_id: str, execution_id: str) -> Any:
    """A synchronous exec's result in the shape of a finished execution record."""

    from types import SimpleNamespace

    exit_code = getattr(result, "exit_code", None)
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        execution_id=getattr(result, "execution_id", None) or execution_id,
        status="completed" if exit_code == 0 else "failed",
        terminal=True,
        stdout=getattr(result, "stdout", ""),
        stderr=getattr(result, "stderr", ""),
        exit_code=exit_code,
        error=None,
        outcome=getattr(result, "outcome", None),
        outcome_detail=getattr(result, "outcome_detail", None),
    )


def _background_started_text(started: Any) -> str:
    payload = {
        "sandbox_id": started.sandbox_id,
        "execution_id": started.execution_id,
        "status": started.status,
        "created_at": started.created_at,
        "poll_after_seconds": 1,
        "hint": (
            "Poll with sandbox action='status' and the same sandbox_id/"
            "execution_id; use action='cancel' to stop it."
        ),
    }
    if started.status == "queued" and getattr(started, "queue_position", None):
        payload["queue_position"] = started.queue_position
        payload["hint"] = (
            "Every command slot of the shared sandbox is taken: this command waits its "
            "turn and starts automatically. " + payload["hint"]
        )
    return json.dumps(payload, ensure_ascii=False)


async def _exec_failure_text(
    exc: Exception,
    *,
    sandbox_id: str,
    execution_id: str,
    timeout: int,
    admitted: bool,
) -> str:
    if getattr(exc, "code", None) == _DISK_QUOTA_CODE:
        return _disk_quota_error(str(exc))
    if _is_runner_busy(exc):
        # Only a runner without a slot queue, or with a full one, answers busy.
        return _sandbox_error(
            SandboxToolErrorCode.SANDBOX_BUSY,
            f"Sandbox {sandbox_id} is busy: {exc}. All of this user's work shares it. "
            "Wait for a running command to finish (or poll a background one) and retry; "
            "do not start a second copy of the same command.",
        )
    if _is_transport_error(exc):
        never_sent = not admitted and _transport_error_before_send(exc)
        return _sandbox_error(
            SandboxToolErrorCode.RUNNER_UNREACHABLE,
            f"The sandbox runner did not answer ({type(exc).__name__}). "
            + (
                "The command was never sent, so it did not run; retry it shortly."
                if never_sent
                else f"The command (execution {execution_id}) may or may not have run: check its "
                "effects, or its status with action='status' once the runner answers, before "
                "repeating it."
            ),
        )
    err_msg = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    is_timeout = (
        isinstance(exc, asyncio.TimeoutError)
        or "timed out" in err_msg
        or "timeout" in err_msg
        or "readtimeout" in exc_type
    )
    if is_timeout:
        stopped = await _stop_execution(sandbox_id, execution_id)
        outcome = (
            "was stopped" if stopped
            else "could not be confirmed stopped and may still be running; check "
            "with a short command before starting more work in this sandbox"
        )
        return _sandbox_error(
            SandboxToolErrorCode.EXEC_TIMEOUT,
            f"Command exceeded its {timeout}s limit and {outcome}. Do not "
            "automatically repeat a command with side effects. For long-running "
            "work, start it with background=true and poll action='status'.",
        )
    logger.exception("[sandbox] exec failed: sandbox=%s error=%s", sandbox_id, exc)
    return _sandbox_error(
        SandboxToolErrorCode.EXEC_FAILED,
        f"Sandbox exec failed: {exc}",
    )


def _exec_queue_wait_seconds() -> int:
    try:
        from packages.core.config import get_settings

        return max(1, int(get_settings().SANDBOX_EXEC_QUEUE_WAIT_SECONDS))
    except Exception:
        return 600


def _sandbox_parks_on_slots(runtime_context: Any) -> bool:
    """A durable run parks while its command waits for a slot; anything else waits in-process."""

    return bool(
        _sandbox_uses_durable_admission()
        and getattr(runtime_context, "runtime_run_id", None)
        and getattr(runtime_context, "runtime_tool_call_id", None)
        and not getattr(runtime_context, "parent_skill", None)
    )


async def _exec_slot_ticket(runtime_context: Any, sandbox_id: str | None) -> dict[str, Any] | None:
    """The command a resumed call parked on: it is collected, never started twice.

    A resume re-executes the tool call with a new attempt number, which would
    name a new execution; the parked ticket remembers the original one.
    ``sandbox_id`` None: whichever sandbox the call parked on (``bash`` learns
    it from the ticket).
    """

    from packages.core.models.runtime_run import SandboxReservationStatus
    from packages.core.services.sandbox_queue_service import find_exec_slot_wait

    try:
        async with _sandbox_admission_db() as db:
            ticket = await find_exec_slot_wait(
                db,
                run_id=str(runtime_context.runtime_run_id),
                tool_call_id=str(runtime_context.runtime_tool_call_id),
            )
    except Exception:  # noqa: BLE001 - no ticket is the normal first call
        logger.warning("[sandbox] exec-slot ticket lookup failed", exc_info=True)
        return None
    if ticket is None or ticket.status == SandboxReservationStatus.CONSUMED.value:
        return None
    payload = ticket.request_payload if isinstance(ticket.request_payload, dict) else {}
    if not payload.get("execution_id") or not payload.get("sandbox_id"):
        return None
    if sandbox_id is not None and str(payload.get("sandbox_id") or "") != sandbox_id:
        return None
    return dict(payload)


async def _park_on_exec_slot(
    runtime_context: Any,
    *,
    sandbox_id: str,
    execution_id: str,
    wait_seconds: int,
    continuation: dict[str, Any] | None = None,
) -> Any | None:
    """Park the durable run while its command waits for a slot (``sandbox_exec_slot``).

    ``continuation`` rides on the ticket for the resumed call (see
    ``enqueue_exec_slot_wait``).
    """

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.services.sandbox_queue_service import (
        SANDBOX_EXEC_WAIT_REASON,
        enqueue_exec_slot_wait,
    )

    try:
        async with _sandbox_admission_db() as db:
            run = await db.get(RuntimeRun, str(runtime_context.runtime_run_id))
            if run is None:
                return None
            ticket = await enqueue_exec_slot_wait(
                db,
                run=run,
                tool_call_id=str(runtime_context.runtime_tool_call_id),
                sandbox_id=sandbox_id,
                execution_id=execution_id,
                # The runner gives up on the command at the same budget; the
                # ticket outlives it a little so the run always wakes to its outcome.
                wait_seconds=wait_seconds + 60,
                continuation=continuation,
            )
            await db.commit()
            return RuntimeToolSuspension(
                kind="waiting_resource",
                reservation_id=ticket.id,
                poll_after_seconds=_settings_poll_seconds(),
                deadline_at=ticket.deadline_at,
                reason=SANDBOX_EXEC_WAIT_REASON,
            )
    except Exception:  # noqa: BLE001 - waiting in-process is the safe fallback
        logger.warning("[sandbox] could not park on the exec-slot queue; waiting in-process", exc_info=True)
        return None


async def _consume_exec_slot_wait(runtime_context: Any) -> None:
    """Latch the call's exec-slot ticket once its command was collected."""

    from datetime import datetime, timezone

    from packages.core.models.runtime_run import SandboxReservationStatus
    from packages.core.services.sandbox_queue_service import find_exec_slot_wait

    try:
        async with _sandbox_admission_db() as db:
            ticket = await find_exec_slot_wait(
                db,
                run_id=str(runtime_context.runtime_run_id),
                tool_call_id=str(runtime_context.runtime_tool_call_id),
            )
            if ticket is None or ticket.status == SandboxReservationStatus.CONSUMED.value:
                return
            ticket.status = SandboxReservationStatus.CONSUMED.value
            ticket.consumed_at = datetime.now(timezone.utc)
            ticket.version += 1
            await db.commit()
    except Exception:  # noqa: BLE001 - a stale ticket is cancelled by the wake sweep
        logger.warning("[sandbox] exec-slot ticket could not be consumed", exc_info=True)


def _settings_poll_seconds() -> int:
    try:
        from packages.core.config import get_settings

        return max(1, int(get_settings().SANDBOX_QUEUE_POLL_SECONDS))
    except Exception:
        return 5


# A runner restart drops in-flight HTTP calls; a long-poll rides out this much
# of it before it reports the runner unreachable.
_RUNNER_BLIP_SECONDS = 20.0
# How often a command someone is watching is asked what it is doing.
_WATCHED_POLL_SECONDS = 4.0


async def _await_execution(
    client: Any,
    *,
    sandbox_id: str,
    execution_id: str,
    first: Any,
    budget_seconds: float,
    on_status: Callable[[Any], Awaitable[None]] | None = None,
) -> tuple[Any, str]:
    """Long-poll one execution until it ends. Returns (final status, last seen status).

    Never hangs: ``asyncio.TimeoutError`` once the budget runs out; the
    transport error once the runner stays unreachable for
    ``_RUNNER_BLIP_SECONDS``; the runner's own error (404 when the sandbox is
    gone) otherwise. The last status seen tells the caller whether the
    command had started.

    ``on_status`` sees every status with only the events new since the last
    one; the runner's long-poll wakes only when the command ends, so a
    watched command is polled every few seconds instead.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(budget_seconds))
    status = first
    last_status = str(getattr(first, "status", "") or "")
    unreachable_since: float | None = None
    after_sequence = 0
    poll_ceiling = _WATCHED_POLL_SECONDS if on_status is not None else 30.0
    if on_status is not None:
        after_sequence = int(getattr(first, "next_sequence", 0) or 0)
        await on_status(first)
    while not getattr(status, "terminal", False):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"execution {execution_id} did not end within {budget_seconds:.0f}s")
        try:
            status = await client.execution_status(
                sandbox_id, execution_id,
                **({"after_sequence": after_sequence} if after_sequence else {}),
                wait_seconds=min(poll_ceiling, max(1.0, remaining)),
            )
            last_status = str(getattr(status, "status", "") or last_status)
            unreachable_since = None
            if on_status is not None:
                after_sequence = max(after_sequence, int(getattr(status, "next_sequence", 0) or 0))
                await on_status(status)
        except Exception as exc:
            if not _is_transport_error(exc):
                setattr(exc, "_manor_last_status", last_status)
                raise
            now = loop.time()
            unreachable_since = unreachable_since or now
            if now - unreachable_since >= _RUNNER_BLIP_SECONDS:
                raise
            await asyncio.sleep(1.0)
    return status, last_status


def _is_transport_error(exc: BaseException) -> bool:
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx ships with the SDK
        return False
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException)


def _transport_error_before_send(exc: BaseException) -> bool:
    """True when the request certainly never reached the runner."""

    import httpx

    return isinstance(exc, httpx.ConnectError)


def _sandbox_lost_while_running_error(sandbox_id: str) -> str:
    return _sandbox_error(
        SandboxToolErrorCode.SANDBOX_LOST,
        f"Sandbox {sandbox_id} was lost while your command was running (its container stopped "
        "or was replaced). The command was NOT retried because it may not be idempotent, and "
        "files that were only inside the sandbox are gone. Check what it already changed, then "
        "open a fresh sandbox (sandbox action='create', or invoke_skill for a Skill) and continue.",
    )


# The runner's disk-quota refusal (``sandbox.disk_quota``): HTTP 409 code for
# a command refused at admission, outcome for one refused while it waited.
_DISK_QUOTA_CODE = "sandbox_disk_quota"
_DISK_QUOTA_OUTCOME = "disk_quota"


def _disk_quota_error(detail: str) -> str:
    return _sandbox_error(
        SandboxToolErrorCode.DISK_QUOTA,
        (detail.strip() or "Not started: this work area is over its share of the sandbox's disk.")
        + " Nothing ran. Do not retry the same command before freeing space.",
    )


def _exec_result_text(final: Any, *, sandbox_id: str, command: str, timeout: int) -> str:
    """The model-facing outcome of a finished command, with accurate kill reasons."""

    outcome = str(getattr(final, "outcome", "") or "")
    detail = str(getattr(final, "outcome_detail", "") or "")
    error = str(getattr(final, "error", "") or "")
    exit_code = getattr(final, "exit_code", None)
    if outcome == _DISK_QUOTA_OUTCOME:
        return _disk_quota_error(detail or error)
    if outcome == "queue_timeout":
        return _sandbox_error(
            SandboxToolErrorCode.EXEC_NOT_STARTED,
            f"The command was not started: every command slot of sandbox {sandbox_id} stayed taken "
            f"for {_exec_queue_wait_seconds()}s by other work of this user. Nothing ran. Retry it "
            "later, or start it with background=true so it waits its turn without blocking you.",
        )
    if str(getattr(final, "status", "")) == "cancelled" and exit_code is None:
        return _sandbox_error(
            SandboxToolErrorCode.EXEC_FAILED,
            f"The command was cancelled ({error or 'by a cancel request'}).",
        )
    if exit_code is None and "timed out" in error.lower():
        return _sandbox_error(
            SandboxToolErrorCode.EXEC_TIMEOUT,
            f"Command exceeded its {timeout}s limit and was stopped. Do not automatically repeat "
            "a command with side effects. For long-running work, start it with background=true "
            "and poll action='status'.",
        )
    parts = []
    if getattr(final, "stdout", None):
        parts.append(final.stdout)
    if getattr(final, "stderr", None):
        parts.append(f"[stderr]\n{final.stderr}")
    if outcome in {"oom_killed", "pids_killed", "disk_killed"}:
        parts.append(f"[{detail or outcome}]")
    elif exit_code is None and error:
        parts.append(f"[error: {error}]")
    parts.append(f"[exit_code: {exit_code if exit_code is not None else 'unknown'}]")
    logger.info(
        "[sandbox] exec: sandbox=%s exit=%s outcome=%s cmd=%s",
        sandbox_id, exit_code, outcome or "-", command[:80],
    )
    return "\n".join(parts)


async def _acquire_general_sandbox(runtime_context: Any, scope: RuntimeSandboxScope) -> Any:
    """The owner's live general sandbox id (created on demand), a suspension, or None."""

    tool_call_id = str(getattr(runtime_context, "runtime_tool_call_id", "") or "")
    try:
        session = await acquire_generic_sandbox(
            scope=scope,
            runtime_context=runtime_context,
            tool_call_id=f"{tool_call_id}:replacement" if tool_call_id else "",
        )
    except ValueError:
        return None
    if isinstance(session, SandboxSession):
        return session.sandbox_id
    if isinstance(session, SandboxSessionError):
        logger.warning("[sandbox] no replacement sandbox: %s", session.message)
        return None
    return session


async def _retry_not_started(
    *,
    runtime_context: Any,
    scope: RuntimeSandboxScope | None,
    sandbox_id: str,
    fallback: str | None,
    forget: bool = True,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    command: str,
    timeout: int,
    background: Any,
    kwargs: dict[str, Any],
    replacement_of: str,
) -> Any:
    """A command that never started on a lost (or reclaimed) sandbox runs on the owner's new one.

    Only for the owner's GENERAL sandbox: a Skill's container is re-opened
    through the Skill, so its command is answered "not started" instead.
    """

    # A command that saw a Skill as /skill needs that Skill re-opened first;
    # only a plain command (the agent's own work area) moves on its own.
    # A sandbox that held a Skill (any agent's) is re-opened through the
    # Skill, never replaced behind the agents' backs.
    skill_container = scope is not None and (
        await runtime_agent_viewed_skill_in(scope, runtime_context.agent_id, sandbox_id)
        or bool(runtime_binding_skills(await _load_binding(scope, sandbox_id)))
    )
    if scope is not None and forget:
        await forget_vanished_sandbox(sandbox_id, scope=scope)
    replacement = None
    if not skill_container and scope is not None and not replacement_of:
        replacement = await _acquire_general_sandbox(runtime_context, scope)
    if replacement is None:
        return fallback or _sandbox_error(
            SandboxToolErrorCode.EXEC_NOT_STARTED,
            f"Sandbox {sandbox_id} was lost before your command started, so it did not run. "
            "Open a fresh sandbox (sandbox action='create', or invoke_skill for a Skill) and run it there.",
        )
    if not isinstance(replacement, str):
        return replacement  # a durable run waits for the owner's new container
    logger.info("[sandbox] running a not-started command of %s in replacement %s", sandbox_id, replacement)
    result = await _sandbox_exec(
        entity_id=entity_id,
        user_id=user_id,
        sandbox_id=replacement,
        command=command,
        timeout=timeout,
        background=background,
        conversation_id=conversation_id,
        _sandbox_replacement_of=sandbox_id,
        **kwargs,
    )
    if not isinstance(result, str):
        return result
    return (
        f"[sandbox {sandbox_id} was gone before this command started, so it ran in this user's "
        f"new sandbox {replacement}. Use sandbox_id {replacement} from now on; files that were "
        "only in the old sandbox are gone.]\n" + result
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
    if getattr(result, "outcome", None):
        # Why it ended when the runner (not the command) ended it.
        payload["outcome"] = result.outcome
        payload["outcome_detail"] = getattr(result, "outcome_detail", None)
    if result.status == "queued":
        payload["queue_position"] = getattr(result, "queue_position", None)
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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
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
    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
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
        expired = await _runner_failure_error(exc, sandbox_id=sandbox_id, scope=scope)
        if expired:
            return expired
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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
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
    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
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
        expired = await _runner_failure_error(exc, sandbox_id=sandbox_id, scope=scope)
        if expired:
            return expired
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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
    sandbox_id = sandbox_id.strip()
    execution_id = execution_id.strip()
    if not sandbox_id or not execution_id:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and execution_id are required.",
        )
    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
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
        expired = await _runner_failure_error(exc, sandbox_id=sandbox_id, scope=scope)
        if expired:
            return expired
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

#: Largest ``read_file`` page. Sized so the JSON envelope fits
#: ``agentic_loop.PAGED_TEXT_RESULT_MAX_CHARS`` (12 000) without compaction.
READ_FILE_PAGE_MAX_CHARS = 10_000


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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
    sandbox_id = sandbox_id.strip()
    path = (path or kwargs.get("file_path") or "").strip()
    if not sandbox_id or not path:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and path are required.",
        )

    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
    if access_error:
        return access_error

    try:
        skill_view, base_dir = await _agent_file_view(scope, runtime_context.agent_id, sandbox_id)
    except SandboxRunnerOutdatedError as exc:
        return _runner_outdated_error(exc)
    if not path.startswith("/"):
        path = f"{base_dir}/{path}"

    try:
        offset = max(0, int(kwargs.get("offset") or 0))
    except (TypeError, ValueError):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "offset must be a non-negative integer.",
        )
    raw_limit = kwargs.get("limit")
    try:
        # One page must survive the agentic loop's context budget intact,
        # otherwise the pagination metadata describes text the model never
        # saw. Default and cap are the same page size; a caller asking for
        # the whole file gets the first page plus an honest next_offset.
        limit = (
            READ_FILE_PAGE_MAX_CHARS
            if raw_limit is None
            else max(1, min(int(raw_limit), READ_FILE_PAGE_MAX_CHARS))
        )
    except (TypeError, ValueError):
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "limit must be a positive integer.",
        )

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            image_mime_type = {
                ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
            }.get(PurePosixPath(path).suffix.lower())
            if image_mime_type:
                from .file_tools import READ_FILE_MAX_IMAGE_BYTES

                image = await client.read_file_base64(
                    sandbox_id=sandbox_id, path=path, max_size=READ_FILE_MAX_IMAGE_BYTES,
                    **_view_kwargs(skill_view),
                )
                if (
                    image.size > READ_FILE_MAX_IMAGE_BYTES
                    or len(image.content_base64) > 4 * ((READ_FILE_MAX_IMAGE_BYTES + 2) // 3)
                ):
                    return _sandbox_error(SandboxToolErrorCode.READ_FAILED, "Image exceeds the read size limit.")
                image_bytes = base64.b64decode(image.content_base64, validate=True)
                return json.dumps({
                    "path": image.path, "size": len(image_bytes), "read_mode": "image",
                    "content_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    "image": {
                        "data_url": f"data:{image_mime_type};base64,{image.content_base64}",
                        "mime_type": image_mime_type, "bytes": len(image_bytes),
                        "delivery": FILE_IMAGE_DELIVERY_CONTRACT,
                    },
                })
            result = await client.read_file(sandbox_id=sandbox_id, path=path, **_view_kwargs(skill_view))
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
        expired = await _runner_failure_error(exc, sandbox_id=sandbox_id, scope=scope)
        if expired:
            return expired
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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
    sandbox_id = sandbox_id.strip()
    path = (path or kwargs.get("file_path") or "").strip()
    workspace_path = (workspace_path or "").strip()

    if not sandbox_id or not path:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id and path are required.",
        )
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

    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
    if access_error:
        return access_error
    try:
        skill_view, base_dir = await _agent_file_view(scope, runtime_context.agent_id, sandbox_id)
    except SandboxRunnerOutdatedError as exc:
        return _runner_outdated_error(exc)
    if not path.startswith("/"):
        path = f"{base_dir}/{path}"

    try:
        client = await _get_client_for_sandbox(sandbox_id)
        try:
            if workspace_path:
                workspace_bytes, read_blocked = await _read_workspace_bytes(
                    entity_id,
                    workspace_path,
                    conversation_id=runtime_context.conversation_id,
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
                    **_view_kwargs(skill_view),
                )
            else:
                result = await client.write_file(
                    sandbox_id=sandbox_id, path=path, content=str(content), mkdir=True,
                    **_view_kwargs(skill_view),
                )
        finally:
            await client.close()
        source = f"workspace:{workspace_path}" if workspace_path else "direct content"
        return f"Written to {result.path} (source: {source})"
    except Exception as exc:
        expired = await _runner_failure_error(exc, sandbox_id=sandbox_id, scope=scope)
        if expired:
            return expired
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
# The server picks the gated file and the report path itself, so a report
# naming another file is a platform fault the agent cannot repair.
_GATE_REPORT_PATH_MISMATCH = "the report points to a different {kind} path"


def _normalized_sandbox_path(path: str) -> PurePosixPath:
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/skill/{normalized.lstrip('/')}"
    return PurePosixPath(normalized)


def _gate_reported_path(path: str, skill_view: str | None) -> PurePosixPath:
    """The /skill-view path of a file a server-run gate named in its report.

    The runner maps ``/skill`` onto the Skill's own ``/skills/<slot>`` in every
    command, so the gate sees, and records, the real directory. ``skill_view``
    is the Skill the gate ran as; without one the runner used its default view
    (the Skill loaded last), whose slot is not known here.
    """
    normalized = _normalized_sandbox_path(path)
    parts = normalized.parts
    if len(parts) >= 3 and parts[1] == "skills" and (
        skill_view is None or parts[2] == runner_skill_slot(skill_view)
    ):
        return PurePosixPath("/skill", *parts[3:])
    return normalized


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
    skill_view: str | None = None,
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
    if metrics.get("pipeline") == "template":
        # Template decks are proven by re-planning deck.json shape by shape
        # instead of a raster render (see deck_engine/verify.py).
        if metrics.get("template_verified_slide_count") != slide_count:
            return "not every slide was verified against its deck.json template plan", payload
    elif metrics.get("rendered_slide_count") != slide_count:
        return "not every final slide has verified render evidence", payload

    reported_path = str(payload.get("pptx") or "")
    if reported_path and _gate_reported_path(reported_path, skill_view) != _normalized_sandbox_path(file_path):
        return _GATE_REPORT_PATH_MISMATCH.format(kind="PPTX"), payload
    return None, payload


def _docx_quality_evidence_error(
    *,
    file_path: str,
    content_bytes: bytes,
    gate_exit_code: int,
    report_bytes: bytes | None,
    skill_view: str | None = None,
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
    if reported_path and _gate_reported_path(reported_path, skill_view) != _normalized_sandbox_path(file_path):
        return _GATE_REPORT_PATH_MISMATCH.format(kind="DOCX"), payload
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
    # Only the sandbox access check below is scoped by the binding key; the
    # knowledge-sync provenance further down keeps the real conversation.
    binding_key = runtime_context.sandbox_binding_key or runtime_context.conversation_id or ""
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

    scope = _caller_scope(runtime_context, entity_id)
    skill_view: str | None = None
    if sandbox_id:
        access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
        if access_error:
            return access_error
        try:
            skill_view, base_dir = await _agent_file_view(scope, runtime_context.agent_id, sandbox_id)
        except SandboxRunnerOutdatedError as exc:
            return _runner_outdated_error(exc)
        if file_path and not file_path.startswith("/"):
            file_path = f"{base_dir}/{file_path}"
        # Scripts print the Skill's real directory (/skills/<name>); the
        # agent's view of it is /skill, which the export gates check.
        file_path = _skill_view_path(file_path, skill_view)

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
                        execution_id=f"manor-{uuid.uuid4().hex}",
                        # A gate never fails "busy": it waits its turn.
                        queue_timeout=_exec_queue_wait_seconds(),
                        fair_key=scope.binding_key if scope is not None else None,
                        **_view_kwargs(skill_view),
                    )
                result = await client.read_file_base64(
                    sandbox_id=sandbox_id,
                    path=file_path,
                    **_view_kwargs(skill_view),
                )
                if gate_context is not None:
                    try:
                        report_result = await client.read_file_base64(
                            sandbox_id=sandbox_id,
                            path=gate_context[2],
                            **_view_kwargs(skill_view),
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
                    skill_view=skill_view,
                )
                gate_platform_fault = evidence_error == _GATE_REPORT_PATH_MISMATCH.format(
                    kind=gate_kind.upper()
                )
                if (
                    evidence_error
                    and gate_result is not None
                    and "No such file or directory" in str(getattr(gate_result, "stderr", "") or "")
                    and "_quality_gate.py" in str(getattr(gate_result, "stderr", "") or "")
                ):
                    # The bundle does not ship the gate script at all: say so
                    # instead of asking the agent to "produce the report".
                    evidence_error = (
                        f"the {gate_kind} skill bundle in this sandbox has no "
                        f"scripts/{gate_kind}_quality_gate.py, so no machine quality report "
                        "can be produced; reload the skill or report the missing script"
                    )
                    gate_platform_fault = True
                if evidence_error and gate_platform_fault:
                    logger.error(
                        "[sandbox] %s quality gate platform fault: sandbox=%s file=%s reason=%s",
                        gate_kind.upper(),
                        sandbox_id,
                        file_path,
                        evidence_error,
                    )
                    return _save_not_applied(_sandbox_error(
                        SandboxToolErrorCode.QUALITY_GATE_BLOCKED,
                        f"{gate_kind.upper()}_FINAL_QUALITY_GATE_BLOCKED: {evidence_error}. "
                        "This is a Manor platform fault, not a defect in the file: do not "
                        f"change or re-export it. The current {gate_kind.upper()} was not saved.",
                        internal_error=True,
                    ))
                if evidence_error:
                    label = gate_kind.upper()
                    logger.warning(
                        "[sandbox] blocked %s delivery: sandbox=%s file=%s reason=%s",
                        label,
                        sandbox_id,
                        file_path,
                        evidence_error,
                    )
                    evidence_metrics = (quality_evidence or {}).get("metrics")
                    if isinstance(evidence_metrics, dict) and evidence_metrics.get("pipeline") == "template":
                        repair = (
                            "Fix deck.json, rerun `python3 scripts/build_deck.py "
                            "projects/<project>/deck.json` without editing the built PPTX, "
                            "and call sandbox action='save_result' again."
                        )
                    else:
                        repair = (
                            "Fix the source, re-export it, render every "
                            f"{'slide' if gate_kind == 'pptx' else 'page'}, and call "
                            "sandbox action='save_result' again."
                        )
                    return _save_not_applied(_sandbox_error(
                        SandboxToolErrorCode.QUALITY_GATE_BLOCKED,
                        f"{label}_FINAL_QUALITY_GATE_BLOCKED: {evidence_error}. {repair} "
                        f"The current {label} was not saved.",
                    ))
        except Exception as exc:
            expired = await _runner_failure_error(exc, sandbox_id=sandbox_id, scope=scope)
            if expired:
                return expired
            logger.exception("[sandbox] save_result read failed: %s", exc)
            return _save_not_applied(_sandbox_error(
                SandboxToolErrorCode.READ_FAILED,
                f"Failed to read file from sandbox: {exc}. No file was saved. "
                "Check that the prior sandbox action='exec' command succeeded and that "
                f"'{file_path}' actually exists before calling sandbox action='save_result' again.",
            ))
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

    from packages.core.services.room_generation_scope import current_room_generation_scope

    group_scope = current_room_generation_scope()
    if group_scope is not None:
        # Only the room runtime sets this scope after claiming a file-action
        # receipt. The sandbox cannot select an Entity/Workspace destination.
        if url or not sandbox_id or not file_path:
            return _sandbox_error(
                SandboxToolErrorCode.INVALID_REQUEST,
                "Group skill output requires a file from its bound sandbox.",
            )
        from packages.core.services.room_file_mutation_tools import store_room_generated_bytes

        group_result = await store_room_generated_bytes(
            scope=group_scope, name=safe_filename, content=content_bytes,
            mime_type=mimetypes.guess_type(safe_filename)[0] or "application/octet-stream",
            entity_id=entity_id,
        )
        return json.dumps({
            "saved": True, "saved_to_knowledge": True,
            "document_id": group_result.document_id,
            "pending_group_share": True,
            "name": safe_filename,
            **({"quality_gate": quality_evidence} if quality_evidence else {}),
        })

    # Save to entity filesystem + document store
    entity_dir = runtime_entity_file_root(entity_id)
    if not entity_dir:
        return _save_not_applied(_sandbox_error(
            SandboxToolErrorCode.FILESYSTEM_UNAVAILABLE,
            "Entity filesystem is not enabled.",
        ))

    rel_path = safe_filename
    if runtime_context.workspace_id:
        try:
            workspace_base = await resolve_workspace_artifact_base_dir(
                entity_id=entity_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
            )
        except Exception as exc:  # noqa: BLE001
            return _save_not_applied(_sandbox_error(
                SandboxToolErrorCode.WORKSPACE_SCOPE_UNAVAILABLE,
                f"Workspace artifact scope is unavailable: {exc}",
            ))
        if not workspace_base:
            return _save_not_applied(_sandbox_error(
                SandboxToolErrorCode.WORKSPACE_SCOPE_UNAVAILABLE,
                "Workspace artifact scope is unavailable.",
            ))
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
                "result_url": entity_fs_open_url(entity_id, rel_path),
                "file": file_ref(
                    entity_id=entity_id, fs_path=rel_path, mime=mime_type, size=len(content_bytes),
                ),
                "message": f"File '{_os.path.basename(target)}' saved for this run but not registered in Knowledge.",
                **({"quality_gate": quality_evidence} if quality_evidence else {}),
            })

        try:
            record = await project_bytes_into_knowledge(
                entity_id=entity_id,
                entity_dir=entity_dir,
                rel_path=rel_path,
                content_bytes=content_bytes,
                source="sandbox",
                created_by=runtime_context.user_id or "ai-agent",
                tool_name="sandbox_save_result",
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                agent_id=runtime_context.agent_id,
                conversation_id=runtime_context.conversation_id,
                user_id=runtime_context.user_id,
                transaction_factory=RuntimeFileProjectionTransactionFactory,
            )
            logger.info(
                "[sandbox] save_result: registered %s → doc=%s",
                filename,
                record.document_id,
            )
            return json.dumps({
                "saved": True,
                "saved_to_knowledge": True,
                "display_as_artifact": display_as_artifact,
                "artifact_role": artifact_role,
                "document_id": record.document_id,
                "name": record.name,
                "file_size": record.file_size,
                "mime_type": record.mime_type,
                "fs_path": record.fs_path,
                "result_url": entity_fs_open_url(entity_id, record.fs_path),
                "file": file_ref(
                    entity_id=entity_id, fs_path=record.fs_path, document_id=record.document_id,
                    name=record.name, mime=record.mime_type, size=record.file_size,
                    sha256=hashlib.sha256(content_bytes).hexdigest(),
                ),
                "message": f"File '{record.name}' saved to knowledge base.",
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
    # Sandbox scope is the binding key, not the conversation; see _caller_scope.
    binding_key = runtime_context.sandbox_binding_key or conversation_id
    sandbox_id = sandbox_id.strip()
    if not sandbox_id:
        return _sandbox_error(
            SandboxToolErrorCode.INVALID_REQUEST,
            "sandbox_id is required.",
        )

    scope = _caller_scope(runtime_context, entity_id)
    access_error = await _sandbox_instance_access_error(sandbox_id=sandbox_id, scope=scope)
    if access_error:
        if scope is None:
            return access_error
        # The conversation's own sandbox, released or reclaimed before:
        # releasing twice is benign, so say so instead of reporting a denial.
        # A sandbox that was never the conversation's stays denied.
        ctx = await _load_binding(scope, sandbox_id)
        if _expired_binding(ctx, sandbox_id):
            return f"Sandbox {sandbox_id} was already reclaimed after sitting idle; nothing to release."
        if runtime_binding_was_released(ctx, sandbox_id):
            return f"Sandbox {sandbox_id} was already released."
        return access_error

    try:
        owner = _scope_owner(scope)
    except ValueError:
        owner = None
    outcome = await release_sandbox_session(
        owner=owner,
        sandbox_id=sandbox_id,
        scope=scope,
        agent_id=runtime_context.agent_id,
        db=None,
    )
    logger.info(
        "[sandbox] released: sandbox=%s agent=%s outcome=%s",
        sandbox_id,
        runtime_context.agent_id or "main",
        outcome,
    )
    if outcome == "destroyed":
        return f"Sandbox {sandbox_id} released and destroyed (not reusable)."
    if outcome == "destroy_failed":
        return (
            f"Sandbox {sandbox_id} released; destroying the container failed "
            "and it will be reclaimed by the idle reaper."
        )
    return (
        f"Sandbox {sandbox_id} released for reuse; the container stays warm for this "
        "conversation's other agents and later turns."
    )


_SANDBOX_DESTROY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_destroy",
        "description": (
            "Release your use of this conversation's sandbox when you are done. The "
            "container stays warm for the conversation's other agents and the next "
            "request; call at most once and do not call again after it succeeds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID to release"},
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
            "destroy once when finished to release the sandbox for reuse. "
            "Allowed action values are exactly create, "
            "exec, status, respond, cancel, read_file, write_file, save_result, and destroy; do not substitute "
            "run, shell, execute, exec_command, or command. Create returns the "
            "conversation's shared sandbox; use invoke_skill for stored Skills."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(SandboxToolAction.values()),
                    "description": "Sandbox operation to perform.",
                },
                # Display-only step label; chat guidance says how to fill it.
                "description": {"type": "string"},
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
                        "read_file: sandbox_id, path, offset?/limit? (text); PNG/JPEG/WebP "
                        "are returned as visual observations. write_file: "
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
    # Display only: the step label the user sees, never part of the command.
    kwargs.pop("description", None)
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
