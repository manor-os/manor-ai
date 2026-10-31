"""
FastAPI routes for the Sandbox Service.

All endpoints are under /api/v1/.

Routes do not translate exceptions: whatever the runner raises reaches the
central handlers in ``api/errors.py``, which map typed runner errors to their
status code and a machine-readable ``code`` (404 gone, 409 busy, 429 capacity,
400/413 invalid request, 504 timeout, 5xx infrastructure).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from sandbox.models import (
    CancelExecutionResponse,
    CreateFromBuiltinRequest,
    CreateFromFilesRequest,
    CreateSandboxRequest,
    CreateSandboxResponse,
    ExecRequest,
    ExecResponse,
    ExecutionResponseAck,
    ExecutionResponseRequest,
    ExecutionStatusResponse,
    FileReadBase64Request,
    FileReadBase64Response,
    FileReadRequest,
    FileReadResponse,
    FileTailRequest,
    FileTailResponse,
    FileWriteBase64Request,
    FileWriteRequest,
    FileWriteResponse,
    LoadSkillRequest,
    LoadSkillResponse,
    SandboxInfo,
    SkillContextResponse,
    SkillManifest,
    SkillRunRequest,
    SkillRunResponse,
    SkillScanRequest,
)
from sandbox.errors import InvalidRequest
from sandbox.skill_runner import SkillRunner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

# The runner is injected by main.py via the module-level reference.
_runner: SkillRunner | None = None


def set_runner(runner: SkillRunner) -> None:
    global _runner
    _runner = runner


def _get_runner() -> SkillRunner:
    if _runner is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _runner




# ── Skill scanning ──


@router.post("/skill/scan", response_model=SkillManifest, tags=["skill"])
async def scan_skill(req: SkillScanRequest):
    """Scan a skill directory and return its manifest (no sandbox created)."""
    logger.info("skill/scan: skill_dir=%s", req.skill_dir)
    t0 = time.time()
    result = await _get_runner().scan_skill(req.skill_dir)
    logger.info(
        "skill/scan: done skill=%s scripts=%d requirements=%s package_json=%s elapsed=%.2fs",
        result.name, len(result.scripts),
        result.requirements_txt or "none",
        result.package_json or "none",
        time.time() - t0,
    )
    return result


# ── Sandbox lifecycle ──


@router.post("/sandbox/create", response_model=CreateSandboxResponse, tags=["sandbox"])
async def create_sandbox(req: CreateSandboxRequest):
    """
    Create a sandbox for a skill.

    Scans the skill directory, creates a Docker container, injects the skill
    files, and optionally installs Python/Node.js dependencies.
    """
    logger.info(
        "sandbox/create: skill_dir=%s auto_install=%s env_keys=%s",
        req.skill_dir, req.auto_install, list(req.env.keys()),
    )
    t0 = time.time()
    result = await _get_runner().create_sandbox(
        skill_dir=req.skill_dir,
        env=req.env,
        allowed_sensitive_keys=set(req.allowed_sensitive_keys),
        config_overrides=req.config_overrides,
        auto_install=req.auto_install,
        idempotency_key=req.idempotency_key,
    )
    logger.info(
        "sandbox/create: ok sandbox_id=%s container=%s status=%s skill=%s elapsed=%.2fs",
        result.sandbox_id, result.container_name, result.status,
        result.skill.name, time.time() - t0,
    )
    return result


@router.post("/sandbox/create-from-files", response_model=CreateSandboxResponse, tags=["sandbox"])
async def create_sandbox_from_files(req: CreateFromFilesRequest):
    """
    Create a sandbox from in-memory file contents.

    Use this when skill files live in remote storage (e.g. MinIO, S3)
    and are not available as a host directory. The service writes files to a
    temp directory, scans, creates the container, then cleans up.
    """
    logger.info(
        "sandbox/create-from-files: skill=%s files=%d source=%s auto_install=%s env_keys=%s",
        req.skill_name, len(req.files), req.source, req.auto_install, list(req.env.keys()),
    )
    t0 = time.time()
    result = await _get_runner().create_sandbox_from_files(
        skill_name=req.skill_name,
        files=req.files,
        env=req.env,
        allowed_sensitive_keys=set(req.allowed_sensitive_keys),
        config_overrides=req.config_overrides,
        auto_install=req.auto_install,
        idempotency_key=req.idempotency_key,
    )
    logger.info(
        "sandbox/create-from-files: ok sandbox_id=%s container=%s status=%s skill=%s elapsed=%.2fs",
        result.sandbox_id, result.container_name, result.status,
        result.skill.name, time.time() - t0,
    )
    return result


@router.post("/sandbox/create-from-builtin", response_model=CreateSandboxResponse, tags=["sandbox"])
async def create_sandbox_from_builtin(req: CreateFromBuiltinRequest):
    """
    Create a sandbox for a builtin (codebase) skill.

    Semantically identical to create-from-files but marks the source as 'builtin'
    for logging and policy purposes.
    """
    logger.info(
        "sandbox/create-from-builtin: skill=%s files=%d baked=%s auto_install=%s env_keys=%s",
        req.skill_name, len(req.files),
        f"{req.baked_skill.name}@{req.baked_skill.digest[:12]}" if req.baked_skill else "no",
        req.auto_install, list(req.env.keys()),
    )
    t0 = time.time()
    if req.baked_skill is not None:
        result = await _get_runner().create_sandbox_from_baked(
            skill_name=req.skill_name,
            baked_name=req.baked_skill.name,
            digest=req.baked_skill.digest,
            env=req.env,
            allowed_sensitive_keys=set(req.allowed_sensitive_keys),
            config_overrides=req.config_overrides,
            auto_install=req.auto_install,
            idempotency_key=req.idempotency_key,
        )
    else:
        result = await _get_runner().create_sandbox_from_files(
            skill_name=req.skill_name,
            files=req.files,
            env=req.env,
            allowed_sensitive_keys=set(req.allowed_sensitive_keys),
            config_overrides=req.config_overrides,
            auto_install=req.auto_install,
            idempotency_key=req.idempotency_key,
        )
    logger.info(
        "sandbox/create-from-builtin: ok sandbox_id=%s container=%s status=%s skill=%s elapsed=%.2fs",
        result.sandbox_id, result.container_name, result.status,
        result.skill.name, time.time() - t0,
    )
    return result


@router.post("/sandbox/{sandbox_id}/load-skill", response_model=LoadSkillResponse, tags=["sandbox"])
async def load_skill_into_sandbox(sandbox_id: str, req: LoadSkillRequest):
    """
    Load a new skill into an existing idle sandbox (reuse mode).

    Copies the new skill's files into the running container without recreating it.
    Returns 409 if the sandbox is currently busy (recently used within idle_threshold seconds).
    A load that fails after touching the container destroys the sandbox.
    """
    logger.info(
        "sandbox/load-skill: sandbox_id=%s skill=%s files=%d baked=%s source=%s auto_install=%s",
        sandbox_id, req.skill_name, len(req.files),
        f"{req.baked_skill.name}@{req.baked_skill.digest[:12]}" if req.baked_skill else "no",
        req.source, req.auto_install,
    )
    t0 = time.time()
    result = await _get_runner().load_skill_into_sandbox(
        sandbox_id=sandbox_id,
        skill_name=req.skill_name,
        files=req.files,
        auto_install=req.auto_install,
        idle_threshold=req.idle_threshold,
        env=req.env,
        allowed_sensitive_keys=set(req.allowed_sensitive_keys),
        baked_skill=(req.baked_skill.name, req.baked_skill.digest) if req.baked_skill else None,
    )
    logger.info(
        "sandbox/load-skill: ok sandbox_id=%s skill=%s elapsed=%.2fs",
        sandbox_id, req.skill_name, time.time() - t0,
    )
    return result


@router.get("/sandbox", response_model=list[SandboxInfo], tags=["sandbox"])
async def list_sandboxes():
    """List all active sandboxes."""
    sandboxes = _get_runner().list_sandboxes()
    logger.debug("sandbox/list: count=%d", len(sandboxes))
    return sandboxes


@router.get("/sandbox/{sandbox_id}", response_model=SandboxInfo, tags=["sandbox"])
async def get_sandbox(sandbox_id: str):
    """Get sandbox status and info (confirmed with Docker: a gone container is 404)."""
    result = await _get_runner().probe_sandbox_status(sandbox_id)
    logger.debug(
        "sandbox/get: sandbox_id=%s status=%s skill=%s",
        sandbox_id, result.status, result.skill_name,
    )
    return result


@router.post("/sandbox/{sandbox_id}/touch", response_model=SandboxInfo, tags=["sandbox"])
async def touch_sandbox(sandbox_id: str):
    """Refresh a sandbox's idle lease without running a command."""

    result = _get_runner().touch_sandbox(sandbox_id)
    logger.debug(
        "sandbox/touch: sandbox_id=%s status=%s expires_at=%s",
        sandbox_id,
        result.status,
        result.expires_at,
    )
    return result


@router.delete("/sandbox/{sandbox_id}", tags=["sandbox"])
async def destroy_sandbox(sandbox_id: str):
    """Destroy a sandbox and remove its container (idempotent).

    Kills any running command with the container; never waits for it.
    """
    logger.info("sandbox/destroy: sandbox_id=%s", sandbox_id)
    t0 = time.time()
    await _get_runner().destroy_sandbox(sandbox_id)
    logger.info(
        "sandbox/destroy: ok sandbox_id=%s elapsed=%.2fs",
        sandbox_id, time.time() - t0,
    )
    return {"sandbox_id": sandbox_id, "destroyed": True}


# ── Command execution ──


@router.post("/sandbox/{sandbox_id}/exec", response_model=ExecResponse, tags=["exec"])
async def exec_command(sandbox_id: str, req: ExecRequest):
    """Execute a shell command inside a sandbox.

    The response's ``exit_code`` is always the command's own: a sandbox
    whose container is gone answers 404, a busy one 409, a timeout 504.
    """
    logger.info(
        "sandbox/exec: sandbox_id=%s command=%r timeout=%d workdir=%s execution_id=%s",
        sandbox_id, req.command[:120], req.timeout, req.workdir or "(default)",
        req.execution_id,
    )
    t0 = time.time()
    if req.side_channel:
        if req.env or req.queue_timeout or req.skill or req.execution_id:
            raise InvalidRequest(
                "A side-channel exec takes no env, Skill view, queue budget or execution id"
            )
        return await _get_runner().side_channel_exec(
            sandbox_id=sandbox_id,
            command=req.command,
            timeout=req.timeout,
            workdir=req.workdir,
        )
    result = await _get_runner().exec_command(
        sandbox_id=sandbox_id,
        command=req.command,
        timeout=req.timeout,
        workdir=req.workdir,
        execution_id=req.execution_id,
        **_exec_options(req),
        **({"env": dict(req.env)} if req.env else {}),
    )
    elapsed = time.time() - t0
    if result.exit_code == 0:
        logger.info(
            "sandbox/exec: ok sandbox_id=%s exit_code=%d elapsed=%.2fs stdout_len=%d",
            sandbox_id, result.exit_code, elapsed, len(result.stdout),
        )
    else:
        # The tail: a command's own diagnosis (and a crash's last words) is
        # printed last, after any noise that preceded it.
        logger.warning(
            "sandbox/exec: non-zero sandbox_id=%s exit_code=%d outcome=%s elapsed=%.2fs stderr_tail=%r",
            sandbox_id, result.exit_code, getattr(result, "outcome", None) or "-", elapsed,
            result.stderr[-600:],
        )
    return result


@router.post(
    "/sandbox/{sandbox_id}/exec/start",
    response_model=ExecutionStatusResponse,
    tags=["exec"],
)
async def start_execution(sandbox_id: str, req: ExecRequest):
    """Start a shell command without blocking until it exits.

    A per-exec env (a short-lived credential such as the manor CLI token of
    a bash command that may wait for a slot) lives only as long as the
    command: the runner hands it to that one docker exec and never stores it;
    the caller collects the command and revokes the credential.
    """
    if req.side_channel:
        raise InvalidRequest("A side-channel exec is synchronous only")
    return await _get_runner().start_execution(
        sandbox_id=sandbox_id,
        command=req.command,
        timeout=req.timeout,
        workdir=req.workdir,
        execution_id=req.execution_id,
        **_exec_options(req),
        **({"env": dict(req.env)} if req.env else {}),
    )


def _exec_options(req: ExecRequest) -> dict:
    """Optional exec fields, passed only when set (older runner fakes take none)."""
    options: dict = {}
    if not req.skill_env:
        options["skill_env"] = False
    if req.skill is not None:
        options["skill"] = req.skill
    if req.fair_key:
        options["fair_key"] = req.fair_key
    if req.queue_timeout:
        options["queue_timeout"] = req.queue_timeout
    return options


def _skill_option(req) -> dict:
    return {} if getattr(req, "skill", None) is None else {"skill": req.skill}


@router.get(
    "/sandbox/{sandbox_id}/executions/{execution_id}",
    response_model=ExecutionStatusResponse,
    tags=["exec"],
)
async def get_execution_status(
    sandbox_id: str,
    execution_id: str,
    after_sequence: int = 0,
    wait_seconds: float = 0,
):
    """Read a background command's current or terminal state.

    ``wait_seconds`` (up to 60) long-polls until the command ends.
    """
    return await _get_runner().get_execution_status(
        sandbox_id,
        execution_id,
        after_sequence=max(0, after_sequence),
        **({"wait_seconds": min(60.0, max(0.0, wait_seconds))} if wait_seconds else {}),
    )


@router.post(
    "/sandbox/{sandbox_id}/executions/{execution_id}/cancel",
    response_model=CancelExecutionResponse,
    tags=["exec"],
)
async def cancel_execution(sandbox_id: str, execution_id: str):
    """Cancel a running execution (background or synchronous) by its id.

    404 means only that no execution with this id is running or recorded.
    """
    cancelled = await _get_runner().cancel_execution(sandbox_id, execution_id)
    return CancelExecutionResponse(
        sandbox_id=sandbox_id,
        execution_id=execution_id,
        cancelled=cancelled,
    )


@router.post(
    "/sandbox/{sandbox_id}/executions/{execution_id}/responses",
    response_model=ExecutionResponseAck,
    tags=["exec"],
)
async def send_execution_response(
    sandbox_id: str,
    execution_id: str,
    req: ExecutionResponseRequest,
):
    """Deliver one idempotent Agent response to a structured Sandbox event."""
    return await _get_runner().send_execution_response(
        sandbox_id,
        execution_id,
        req.event_id,
        payload=req.payload,
        message=req.message,
    )


# ── File operations (side channel: allowed while a command runs) ──


@router.post(
    "/sandbox/{sandbox_id}/files/read",
    response_model=FileReadResponse,
    tags=["files"],
)
async def read_file(sandbox_id: str, req: FileReadRequest):
    """Read a file from inside the sandbox."""
    logger.info("sandbox/files/read: sandbox_id=%s path=%s", sandbox_id, req.path)
    result = await _get_runner().read_file(
        sandbox_id=sandbox_id,
        path=req.path,
        max_size=req.max_size,
        **_skill_option(req),
    )
    logger.info(
        "sandbox/files/read: ok sandbox_id=%s path=%s size=%d truncated=%s",
        sandbox_id, req.path, result.size, result.truncated,
    )
    return result


@router.post(
    "/sandbox/{sandbox_id}/files/read-base64",
    response_model=FileReadBase64Response,
    tags=["files"],
)
async def read_file_base64(sandbox_id: str, req: FileReadBase64Request):
    """Read a binary file from inside the sandbox as base64."""
    logger.info("sandbox/files/read-base64: sandbox_id=%s path=%s", sandbox_id, req.path)
    result = await _get_runner().read_file_base64(
        sandbox_id=sandbox_id,
        path=req.path,
        max_size=req.max_size,
        **_skill_option(req),
    )
    logger.info(
        "sandbox/files/read-base64: ok sandbox_id=%s path=%s size=%d",
        sandbox_id, req.path, result.size,
    )
    return result


@router.post(
    "/sandbox/{sandbox_id}/files/tail",
    response_model=FileTailResponse,
    tags=["files"],
)
async def tail_file(sandbox_id: str, req: FileTailRequest):
    """Byte-range read of an append-only log; does not wait for running commands.

    Errors map centrally (api/errors.py) like every other route.
    """
    return await _get_runner().tail_file(
        sandbox_id=sandbox_id, path=req.path, offset=req.offset, limit=req.limit,
    )


@router.post(
    "/sandbox/{sandbox_id}/files/write",
    response_model=FileWriteResponse,
    tags=["files"],
)
async def write_file(sandbox_id: str, req: FileWriteRequest):
    """Write a file into the sandbox."""
    logger.info(
        "sandbox/files/write: sandbox_id=%s path=%s content_len=%d",
        sandbox_id, req.path, len(req.content),
    )
    result = await _get_runner().write_file(
        sandbox_id=sandbox_id,
        path=req.path,
        content=req.content,
        mkdir=req.mkdir,
        **_skill_option(req),
    )
    logger.info("sandbox/files/write: ok sandbox_id=%s path=%s", sandbox_id, req.path)
    return result


@router.post(
    "/sandbox/{sandbox_id}/files/write-base64",
    response_model=FileWriteResponse,
    tags=["files"],
)
async def write_file_base64(sandbox_id: str, req: FileWriteBase64Request):
    """Write a binary file into the sandbox from base64 content."""
    logger.info(
        "sandbox/files/write-base64: sandbox_id=%s path=%s b64_len=%d",
        sandbox_id, req.path, len(req.content_base64),
    )
    result = await _get_runner().write_file_base64(
        sandbox_id=sandbox_id,
        path=req.path,
        content_base64=req.content_base64,
        mkdir=req.mkdir,
        **_skill_option(req),
    )
    logger.info("sandbox/files/write-base64: ok sandbox_id=%s path=%s", sandbox_id, req.path)
    return result


# ── LLM context ──


@router.get(
    "/sandbox/{sandbox_id}/context",
    response_model=SkillContextResponse,
    tags=["context"],
)
async def get_skill_context(sandbox_id: str, max_files: int = 5, max_file_size: int = 8000):
    """
    Build structured context for the LLM.

    Returns the skill manifest, key file contents, and sandbox constraint
    summary so the LLM can decide how to execute the skill.
    """
    logger.info(
        "sandbox/context: sandbox_id=%s max_files=%d max_file_size=%d",
        sandbox_id, max_files, max_file_size,
    )
    result = await _get_runner().get_skill_context(
        sandbox_id=sandbox_id,
        max_files=max_files,
        max_file_size=max_file_size,
    )
    logger.info(
        "sandbox/context: ok sandbox_id=%s skill=%s files_returned=%d",
        sandbox_id, result.skill.name, len(result.file_contents),
    )
    return result


# ── Full run ──


@router.post("/skill/run", response_model=SkillRunResponse, tags=["skill"])
async def run_skill(req: SkillRunRequest):
    """
    Full end-to-end skill execution.

    Creates a sandbox, executes commands (or auto-detects entry point),
    collects results, and optionally destroys the sandbox.
    """
    logger.info(
        "skill/run: skill_dir=%s commands=%d auto_destroy=%s",
        req.skill_dir, len(req.commands), req.auto_destroy,
    )
    t0 = time.time()
    result = await _get_runner().run_skill(
        skill_dir=req.skill_dir,
        env=req.env,
        commands=req.commands,
        allowed_sensitive_keys=set(req.allowed_sensitive_keys),
        config_overrides=req.config_overrides,
        auto_destroy=req.auto_destroy,
    )
    step_summary = ", ".join(f"exit={s.exit_code}" for s in result.steps)
    logger.info(
        "skill/run: %s sandbox_id=%s skill=%s steps=[%s] elapsed=%.2fs destroyed=%s",
        "ok" if result.success else "FAILED",
        result.sandbox_id, result.skill.name,
        step_summary, time.time() - t0, result.destroyed,
    )
    return result
