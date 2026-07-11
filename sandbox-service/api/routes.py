"""
FastAPI routes for the Sandbox Service.

All endpoints are under /api/v1/.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from sandbox.models import (
    CreateFromBuiltinRequest,
    CreateFromFilesRequest,
    CreateSandboxRequest,
    CreateSandboxResponse,
    ExecRequest,
    ExecResponse,
    FileReadBase64Request,
    FileReadBase64Response,
    FileReadRequest,
    FileReadResponse,
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
from sandbox.security import SecurityError
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
    try:
        result = _get_runner().scan_skill(req.skill_dir)
        logger.info(
            "skill/scan: done skill=%s scripts=%d requirements=%s package_json=%s elapsed=%.2fs",
            result.name, len(result.scripts),
            result.requirements_txt or "none",
            result.package_json or "none",
            time.time() - t0,
        )
        return result
    except FileNotFoundError as exc:
        logger.warning("skill/scan: not found skill_dir=%s error=%s", req.skill_dir, exc)
        raise HTTPException(status_code=404, detail=str(exc))


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
    try:
        result = await _get_runner().create_sandbox(
            skill_dir=req.skill_dir,
            env=req.env,
            allowed_sensitive_keys=set(req.allowed_sensitive_keys),
            config_overrides=req.config_overrides,
            auto_install=req.auto_install,
        )
        logger.info(
            "sandbox/create: ok sandbox_id=%s container=%s status=%s skill=%s elapsed=%.2fs",
            result.sandbox_id, result.container_name, result.status,
            result.skill.name, time.time() - t0,
        )
        return result
    except FileNotFoundError as exc:
        logger.warning("sandbox/create: not found skill_dir=%s error=%s", req.skill_dir, exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except SecurityError as exc:
        logger.warning("sandbox/create: security error skill_dir=%s error=%s", req.skill_dir, exc)
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        logger.error(
            "sandbox/create: failed skill_dir=%s elapsed=%.2fs error=%s",
            req.skill_dir, time.time() - t0, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))


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
    try:
        result = await _get_runner().create_sandbox_from_files(
            skill_name=req.skill_name,
            files=req.files,
            env=req.env,
            allowed_sensitive_keys=set(req.allowed_sensitive_keys),
            config_overrides=req.config_overrides,
            auto_install=req.auto_install,
        )
        logger.info(
            "sandbox/create-from-files: ok sandbox_id=%s container=%s status=%s skill=%s elapsed=%.2fs",
            result.sandbox_id, result.container_name, result.status,
            result.skill.name, time.time() - t0,
        )
        return result
    except ValueError as exc:
        logger.warning("sandbox/create-from-files: bad request skill=%s error=%s", req.skill_name, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except SecurityError as exc:
        logger.warning("sandbox/create-from-files: security error skill=%s error=%s", req.skill_name, exc)
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        logger.error(
            "sandbox/create-from-files: failed skill=%s elapsed=%.2fs error=%s",
            req.skill_name, time.time() - t0, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/sandbox/create-from-builtin", response_model=CreateSandboxResponse, tags=["sandbox"])
async def create_sandbox_from_builtin(req: CreateFromBuiltinRequest):
    """
    Create a sandbox for a builtin (codebase) skill.

    Semantically identical to create-from-files but marks the source as 'builtin'
    for logging and policy purposes.
    """
    logger.info(
        "sandbox/create-from-builtin: skill=%s files=%d auto_install=%s env_keys=%s",
        req.skill_name, len(req.files), req.auto_install, list(req.env.keys()),
    )
    t0 = time.time()
    try:
        result = await _get_runner().create_sandbox_from_files(
            skill_name=req.skill_name,
            files=req.files,
            env=req.env,
            allowed_sensitive_keys=set(req.allowed_sensitive_keys),
            config_overrides=req.config_overrides,
            auto_install=req.auto_install,
        )
        logger.info(
            "sandbox/create-from-builtin: ok sandbox_id=%s container=%s status=%s skill=%s elapsed=%.2fs",
            result.sandbox_id, result.container_name, result.status,
            result.skill.name, time.time() - t0,
        )
        return result
    except ValueError as exc:
        logger.warning("sandbox/create-from-builtin: bad request skill=%s error=%s", req.skill_name, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except SecurityError as exc:
        logger.warning("sandbox/create-from-builtin: security error skill=%s error=%s", req.skill_name, exc)
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        logger.error(
            "sandbox/create-from-builtin: failed skill=%s elapsed=%.2fs error=%s",
            req.skill_name, time.time() - t0, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/sandbox/{sandbox_id}/load-skill", response_model=LoadSkillResponse, tags=["sandbox"])
async def load_skill_into_sandbox(sandbox_id: str, req: LoadSkillRequest):
    """
    Load a new skill into an existing idle sandbox (reuse mode).

    Copies the new skill's files into the running container without recreating it.
    Returns 409 if the sandbox is currently busy (recently used within idle_threshold seconds).
    """
    logger.info(
        "sandbox/load-skill: sandbox_id=%s skill=%s files=%d source=%s auto_install=%s",
        sandbox_id, req.skill_name, len(req.files), req.source, req.auto_install,
    )
    t0 = time.time()
    try:
        result = await _get_runner().load_skill_into_sandbox(
            sandbox_id=sandbox_id,
            skill_name=req.skill_name,
            files=req.files,
            auto_install=req.auto_install,
            idle_threshold=req.idle_threshold,
        )
        logger.info(
            "sandbox/load-skill: ok sandbox_id=%s skill=%s elapsed=%.2fs",
            sandbox_id, req.skill_name, time.time() - t0,
        )
        return result
    except KeyError:
        logger.warning("sandbox/load-skill: not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")
    except RuntimeError as exc:
        msg = str(exc)
        if "busy" in msg.lower() or "installing" in msg.lower():
            logger.warning(
                "sandbox/load-skill: busy sandbox_id=%s skill=%s error=%s",
                sandbox_id, req.skill_name, msg,
            )
            raise HTTPException(status_code=409, detail=msg)
        logger.error(
            "sandbox/load-skill: failed sandbox_id=%s skill=%s elapsed=%.2fs error=%s",
            sandbox_id, req.skill_name, time.time() - t0, msg,
        )
        raise HTTPException(status_code=500, detail=msg)


@router.get("/sandbox", response_model=list[SandboxInfo], tags=["sandbox"])
async def list_sandboxes():
    """List all active sandboxes."""
    sandboxes = _get_runner().list_sandboxes()
    logger.debug("sandbox/list: count=%d", len(sandboxes))
    return sandboxes


@router.get("/sandbox/{sandbox_id}", response_model=SandboxInfo, tags=["sandbox"])
async def get_sandbox(sandbox_id: str):
    """Get sandbox status and info."""
    try:
        result = _get_runner().get_sandbox_status(sandbox_id)
        logger.debug(
            "sandbox/get: sandbox_id=%s status=%s skill=%s",
            sandbox_id, result.status, result.skill_name,
        )
        return result
    except KeyError:
        logger.warning("sandbox/get: not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")


@router.delete("/sandbox/{sandbox_id}", tags=["sandbox"])
async def destroy_sandbox(sandbox_id: str):
    """Destroy a sandbox and remove its container."""
    logger.info("sandbox/destroy: sandbox_id=%s", sandbox_id)
    t0 = time.time()
    try:
        await _get_runner().destroy_sandbox(sandbox_id)
        logger.info(
            "sandbox/destroy: ok sandbox_id=%s elapsed=%.2fs",
            sandbox_id, time.time() - t0,
        )
        return {"sandbox_id": sandbox_id, "destroyed": True}
    except KeyError:
        logger.warning("sandbox/destroy: not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")


# ── Command execution ──


@router.post("/sandbox/{sandbox_id}/exec", response_model=ExecResponse, tags=["exec"])
async def exec_command(sandbox_id: str, req: ExecRequest):
    """Execute a shell command inside a sandbox."""
    logger.info(
        "sandbox/exec: sandbox_id=%s command=%r timeout=%d workdir=%s",
        sandbox_id, req.command[:120], req.timeout, req.workdir or "(default)",
    )
    t0 = time.time()
    try:
        result = await _get_runner().exec_command(
            sandbox_id=sandbox_id,
            command=req.command,
            timeout=req.timeout,
            workdir=req.workdir,
        )
        elapsed = time.time() - t0
        if result.exit_code == 0:
            logger.info(
                "sandbox/exec: ok sandbox_id=%s exit_code=%d elapsed=%.2fs stdout_len=%d",
                sandbox_id, result.exit_code, elapsed, len(result.stdout),
            )
        else:
            logger.warning(
                "sandbox/exec: non-zero sandbox_id=%s exit_code=%d elapsed=%.2fs stderr=%r",
                sandbox_id, result.exit_code, elapsed, result.stderr[:300],
            )
        return result
    except KeyError:
        logger.warning("sandbox/exec: not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")
    except RuntimeError as exc:
        logger.error(
            "sandbox/exec: failed sandbox_id=%s elapsed=%.2fs error=%s",
            sandbox_id, time.time() - t0, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ── File operations ──


@router.post(
    "/sandbox/{sandbox_id}/files/read",
    response_model=FileReadResponse,
    tags=["files"],
)
async def read_file(sandbox_id: str, req: FileReadRequest):
    """Read a file from inside the sandbox."""
    logger.info("sandbox/files/read: sandbox_id=%s path=%s", sandbox_id, req.path)
    try:
        result = await _get_runner().read_file(
            sandbox_id=sandbox_id,
            path=req.path,
            max_size=req.max_size,
        )
        logger.info(
            "sandbox/files/read: ok sandbox_id=%s path=%s size=%d truncated=%s",
            sandbox_id, req.path, result.size, result.truncated,
        )
        return result
    except KeyError:
        logger.warning("sandbox/files/read: sandbox not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")
    except FileNotFoundError as exc:
        logger.warning(
            "sandbox/files/read: file not found sandbox_id=%s path=%s error=%s",
            sandbox_id, req.path, exc,
        )
        raise HTTPException(status_code=404, detail=str(exc))


@router.post(
    "/sandbox/{sandbox_id}/files/read-base64",
    response_model=FileReadBase64Response,
    tags=["files"],
)
async def read_file_base64(sandbox_id: str, req: FileReadBase64Request):
    """Read a binary file from inside the sandbox as base64."""
    logger.info("sandbox/files/read-base64: sandbox_id=%s path=%s", sandbox_id, req.path)
    try:
        result = await _get_runner().read_file_base64(
            sandbox_id=sandbox_id,
            path=req.path,
            max_size=req.max_size,
        )
        logger.info(
            "sandbox/files/read-base64: ok sandbox_id=%s path=%s size=%d",
            sandbox_id, req.path, result.size,
        )
        return result
    except KeyError:
        logger.warning("sandbox/files/read-base64: sandbox not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")
    except FileNotFoundError as exc:
        logger.warning(
            "sandbox/files/read-base64: file not found sandbox_id=%s path=%s error=%s",
            sandbox_id, req.path, exc,
        )
        raise HTTPException(status_code=404, detail=str(exc))


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
    try:
        result = await _get_runner().write_file(
            sandbox_id=sandbox_id,
            path=req.path,
            content=req.content,
            mkdir=req.mkdir,
        )
        logger.info(
            "sandbox/files/write: ok sandbox_id=%s path=%s",
            sandbox_id, req.path,
        )
        return result
    except KeyError:
        logger.warning("sandbox/files/write: sandbox not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")
    except IOError as exc:
        logger.error(
            "sandbox/files/write: failed sandbox_id=%s path=%s error=%s",
            sandbox_id, req.path, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))


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
    try:
        result = await _get_runner().write_file_base64(
            sandbox_id=sandbox_id,
            path=req.path,
            content_base64=req.content_base64,
            mkdir=req.mkdir,
        )
        logger.info(
            "sandbox/files/write-base64: ok sandbox_id=%s path=%s",
            sandbox_id, req.path,
        )
        return result
    except KeyError:
        logger.warning("sandbox/files/write-base64: sandbox not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")
    except IOError as exc:
        logger.error(
            "sandbox/files/write-base64: failed sandbox_id=%s path=%s error=%s",
            sandbox_id, req.path, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))


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
    try:
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
    except KeyError:
        logger.warning("sandbox/context: not found sandbox_id=%s", sandbox_id)
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {sandbox_id}")


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
    try:
        result = await _get_runner().run_skill(
            skill_dir=req.skill_dir,
            env=req.env,
            commands=req.commands,
            allowed_sensitive_keys=set(req.allowed_sensitive_keys),
            config_overrides=req.config_overrides,
            auto_destroy=req.auto_destroy,
        )
        elapsed = time.time() - t0
        step_summary = ", ".join(
            f"exit={s.exit_code}" for s in result.steps
        )
        logger.info(
            "skill/run: %s sandbox_id=%s skill=%s steps=[%s] elapsed=%.2fs destroyed=%s",
            "ok" if result.success else "FAILED",
            result.sandbox_id, result.skill.name,
            step_summary, elapsed, result.destroyed,
        )
        return result
    except FileNotFoundError as exc:
        logger.warning("skill/run: not found skill_dir=%s error=%s", req.skill_dir, exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except SecurityError as exc:
        logger.warning("skill/run: security error skill_dir=%s error=%s", req.skill_dir, exc)
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        logger.error(
            "skill/run: failed skill_dir=%s elapsed=%.2fs error=%s",
            req.skill_dir, time.time() - t0, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))
