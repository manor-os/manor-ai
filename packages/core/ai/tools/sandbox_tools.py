"""
Sandbox tools — expose Docker-based sandbox lifecycle to the LLM.

Tools (only registered when SANDBOX_SERVICE_URL is set):
  sandbox_create      – Create an isolated Docker container for a skill
  sandbox_exec        – Execute a command inside a sandbox
  sandbox_read_file   – Read a file from inside a sandbox
  sandbox_write_file  – Write a file into a sandbox (direct content or from MinIO)
  sandbox_save_result – Save a sandbox output file/URL, optionally registering it in Knowledge
  sandbox_destroy     – Destroy a sandbox and release its resources

Sandbox context (sandbox_id per conversation) is tracked via Redis cache so
that sessions survive across chat turns. Falls back silently to "no tracking"
when Redis is unavailable.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
from typing import Any

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_get_document_for_entity,
    runtime_guard_file_mutation,
    runtime_sync_entity_file_to_knowledge,
    runtime_write_entity_file_atomic,
)
from packages.core.ai.runtime.sandbox import (
    RUNTIME_SANDBOX_CONTEXT_PREFIX,
    RUNTIME_SANDBOX_CONTEXT_TTL,
    RUNTIME_SANDBOX_IDLE_THRESHOLD,
    runtime_delete_sandbox_context,
    runtime_init_sandbox_context,
    runtime_load_sandbox_context,
    runtime_save_sandbox_context,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

logger = logging.getLogger(__name__)


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


async def _init_ctx(conversation_id: str, sandbox_id: str, skill_id: str) -> dict:
    return await runtime_init_sandbox_context(conversation_id, sandbox_id, skill_id)


def _sandbox_available() -> bool:
    return bool(SANDBOX_SERVICE_URL)


def _get_client():
    from packages.core.services.sandbox_sdk import SandboxClient
    return SandboxClient(base_url=SANDBOX_SERVICE_URL, timeout=180.0)


# ────────────────────────────────────────────────────────────────
# File / credential collection from MinIO
# ────────────────────────────────────────────────────────────────

def _collect_skill_files_from_minio(skill_id: str, entity_id: str) -> dict[str, str]:
    """Download all skill files from MinIO into a {rel_path: content} dict."""
    try:
        from packages.core.services.skill_file_storage import (
            load_skill_extra_files,
            load_skill_prompt,
            load_skill_scripts,
            load_skill_requirements,
        )
        files: dict[str, str] = {}
        prompt = load_skill_prompt(entity_id, skill_id)
        if prompt:
            files["SKILL.md"] = prompt
        scripts = load_skill_scripts(entity_id, skill_id) or {}
        files.update(scripts)
        reqs = load_skill_requirements(entity_id, skill_id) or ""
        if reqs:
            files["requirements.txt"] = reqs
        extra = load_skill_extra_files(entity_id, skill_id) or {}
        files.update(extra)
        return files
    except Exception as exc:
        logger.debug("[sandbox] MinIO file collect failed skill=%s: %s", skill_id, exc)
        return {}


def _load_skill_credentials(skill_id: str, entity_id: str) -> dict[str, str]:
    """Load saved credentials for a skill from MinIO credentials.json."""
    try:
        from packages.core.services.skill_file_storage import load_skill_credentials
        return load_skill_credentials(entity_id, skill_id) or {}
    except Exception:
        return {}


# ────────────────────────────────────────────────────────────────
# sandbox_create
# ────────────────────────────────────────────────────────────────

async def _sandbox_create(
    entity_id: str = "",
    skill_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    skill_id = skill_id.strip()
    if not skill_id:
        return "skill_id is required."

    files = _collect_skill_files_from_minio(skill_id, entity_id)
    if not files:
        return f"No files found for skill '{skill_id}' in storage."

    env = _load_skill_credentials(skill_id, entity_id)
    allowed_keys = list(env.keys())

    config_overrides = None
    expected_workspace_volume: str | None = None
    manor_fs_root = os.getenv("MANOR_FS_ROOT", "")
    if manor_fs_root and entity_id and os.getenv("MANOR_FS_ENABLED", "").lower() in ("true", "1"):
        entity_path = os.path.join(manor_fs_root, entity_id)
        if os.path.isdir(entity_path):
            expected_workspace_volume = f"{entity_path}:/workspace:ro"
            config_overrides = {"volumes": [expected_workspace_volume]}

    try:
        client = _get_client()
        try:
            result = await client.create_from_files(
                skill_name=skill_id,
                files=files,
                env=env,
                allowed_sensitive_keys=allowed_keys,
                auto_install=True,
                config=config_overrides,
            )
        finally:
            await client.close()

        skill = result.skill
        parts = [
            f"sandbox_id: {result.sandbox_id}",
            f"status: {result.status}",
            f"workdir: {result.workdir}",
        ]
        if skill.entry_hint:
            parts.append(f"entry_hint: {skill.entry_hint}")
        if skill.scripts:
            parts.append(f"scripts: {', '.join(skill.scripts)}")
        if skill.requirements_txt:
            parts.append("dependencies: installed from requirements.txt")
        parts.append(
            f"credentials_injected: {', '.join(env.keys())}" if env else "credentials_injected: (none)"
        )
        if result.env_blocked:
            parts.append(f"env_blocked: {', '.join(result.env_blocked)}")
        if expected_workspace_volume:
            parts.append(
                "workspace_mount: /workspace (read-only entity filesystem; chat uploads live under /workspace/uploads/chat/...)"
            )
        parts.append(
            "NOTE: All required credentials are pre-injected as environment variables. "
            "Do NOT look for API keys elsewhere. Proceed directly with sandbox_exec."
        )

        if conversation_id:
            await _init_ctx(conversation_id, result.sandbox_id, skill_id)
        logger.info(
            "[sandbox] created: skill=%s sandbox=%s entity=%s",
            skill_id, result.sandbox_id, entity_id or "(none)",
        )
        return "\n".join(parts)
    except Exception as exc:
        logger.exception("[sandbox] create failed: skill=%s error=%s", skill_id, exc)
        return f"Sandbox creation failed: {exc}"


_SANDBOX_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_create",
        "description": (
            "Create a skill sandbox and return sandbox_id, skill info, and entry_hint. "
            "Credentials are injected as env vars; do not search for API keys. "
            "Then run sandbox_exec."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "Skill id, e.g. 'my-org--data-processor'",
                },
            },
            "required": ["skill_id"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_exec
# ────────────────────────────────────────────────────────────────

async def _sandbox_exec(
    entity_id: str = "",
    sandbox_id: str = "",
    command: str = "",
    timeout: Any = 60,
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    sandbox_id = sandbox_id.strip()
    command = command.strip()
    try:
        timeout = min(int(timeout or 60), 300)
    except (ValueError, TypeError):
        timeout = 60

    if not sandbox_id or not command:
        return "sandbox_id and command are required."

    try:
        client = _get_client()
        try:
            result = await client.exec(sandbox_id=sandbox_id, command=command, timeout=timeout)
        finally:
            await client.close()

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
            cmd_lower = command.lower()
            is_poll = any(kw in cmd_lower for kw in ("poll", "wait", "status", "check"))
            retry_timeout = 120 if is_poll else 30
            return (
                f"Command timed out after {timeout}s. The sandbox is still running.\n\n"
                "The task is likely still in progress. You MUST retry:\n"
                f"  sandbox_exec(sandbox_id=\"{sandbox_id}\", "
                f"command=\"<same command>\", timeout={retry_timeout})\n\n"
                "IMPORTANT: Always use `bash` (not `sh`) to run scripts.\n"
                "Do NOT destroy the sandbox. Do NOT give up."
            )
        return f"Sandbox exec failed: {exc}"


_SANDBOX_EXEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_exec",
        "description": (
            "Execute a command inside a sandbox; returns stdout/stderr/exit_code. "
            "Use bash for .sh scripts, timeout=120 for polling, and retry after timeouts. "
            "Prefer running existing skill scripts or explicit helper files. "
            "Use sandbox_write_file for larger new files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string", "description": "Sandbox ID returned by sandbox_create"},
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
            },
            "required": ["sandbox_id", "command"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_read_file
# ────────────────────────────────────────────────────────────────

async def _sandbox_read_file(
    entity_id: str = "",
    sandbox_id: str = "",
    path: str = "",
    **kwargs: Any,
) -> str:
    sandbox_id = sandbox_id.strip()
    path = (path or kwargs.get("file_path") or "").strip()
    if not sandbox_id or not path:
        return "sandbox_id and path are required."

    if not path.startswith("/"):
        path = f"/skill/{path}"

    try:
        client = _get_client()
        try:
            result = await client.read_file(sandbox_id=sandbox_id, path=path)
        finally:
            await client.close()
        return json.dumps(
            {
                "path": result.path,
                "content": result.content,
                "size": result.size,
                "truncated": result.truncated,
                "content_sha256": hashlib.sha256(
                    result.content.encode("utf-8", errors="replace")
                ).hexdigest(),
                "hint": (
                    "Content is truncated by the sandbox read limit; inspect a smaller "
                    "file or save/export the full artifact."
                    if result.truncated
                    else None
                ),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.exception("[sandbox] read_file failed: sandbox=%s error=%s", sandbox_id, exc)
        return f"Sandbox read_file failed: {exc}"


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
    sandbox_id: str = "",
    path: str = "",
    content: Any = None,
    workspace_path: str = "",
    **kwargs: Any,
) -> str:
    sandbox_id = sandbox_id.strip()
    path = (path or kwargs.get("file_path") or "").strip()
    workspace_path = (workspace_path or "").strip()

    if not sandbox_id or not path:
        return "sandbox_id and path are required."
    if not path.startswith("/"):
        path = f"/skill/{path}"
    if content is not None and workspace_path:
        return "Provide either `content` or `workspace_path`, not both."
    if content is None and not workspace_path:
        return "Either `content` or `workspace_path` must be provided."

    try:
        file_content = content
        if workspace_path:
            # Load from MinIO (entity's workspace storage)
            try:
                # workspace_path is arbitrary; try a direct MinIO read
                from packages.core.services.skill_file_storage import _get_client as _mc
                mc = _mc()
                if mc is None:
                    return f"File not found in workspace (MinIO unavailable): {workspace_path}"
                from packages.core.config import get_settings
                bucket = get_settings().MINIO_BUCKET
                obj = mc.get_object(bucket, workspace_path)
                file_content = obj.read().decode("utf-8")
            except Exception as exc:
                return f"File not found in workspace: {workspace_path} ({exc})"

        client = _get_client()
        try:
            result = await client.write_file(
                sandbox_id=sandbox_id, path=path, content=file_content, mkdir=True,
            )
        finally:
            await client.close()
        source = f"workspace:{workspace_path}" if workspace_path else "direct content"
        return f"Written to {result.path} (source: {source})"
    except Exception as exc:
        logger.exception("[sandbox] write_file failed: sandbox=%s error=%s", sandbox_id, exc)
        return f"Sandbox write_file failed: {exc}"


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
                "workspace_path": {"type": "string", "description": "Relative MinIO path to copy from, e.g. 'documents/sales.csv'"},
            },
            "required": ["sandbox_id", "path"],
        },
    },
}


# ────────────────────────────────────────────────────────────────
# sandbox_save_result
# ────────────────────────────────────────────────────────────────

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
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    sandbox_id = sandbox_id.strip()
    file_path = file_path.strip()
    url = url.strip()
    filename = filename.strip()
    display_as_artifact, artifact_role = _artifact_display_params(kwargs)

    if not filename:
        return "filename is required."
    if not sandbox_id and not file_path and not url:
        return "Provide either (sandbox_id + file_path) or url."
    if sandbox_id and file_path and url:
        return "Provide either (sandbox_id + file_path) or url, not both."
    if url and not url.startswith(("http://", "https://")):
        return (
            f"Invalid url '{url}': must start with http:// or https://. "
            "To save a file generated inside the sandbox, use sandbox_id + file_path instead of url."
        )

    content_bytes: bytes | None = None

    if sandbox_id and file_path:
        try:
            client = _get_client()
            try:
                result = await client.read_file_base64(sandbox_id=sandbox_id, path=file_path)
            finally:
                await client.close()
            content_bytes = base64.b64decode(result.content_base64)
        except Exception as exc:
            logger.exception("[sandbox] save_result read failed: %s", exc)
            return (
                f"Failed to read file from sandbox: {exc}. No file was saved. "
                "Check that the prior sandbox_exec command succeeded and that "
                f"'{file_path}' actually exists before calling sandbox_save_result again."
            )
    elif url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
                resp = await http.get(url)
            if resp.status_code != 200:
                return f"Failed to download URL (HTTP {resp.status_code}): {url}"
            content_bytes = resp.content
        except Exception as exc:
            logger.exception("[sandbox] save_result download failed: %s", exc)
            return f"Failed to download URL: {exc}"

    if not content_bytes:
        return "No content to save."

    import os as _os

    safe_filename = _os.path.basename(filename)
    if safe_filename != filename:
        return "filename must be a plain file name, not a path."

    # Save to entity filesystem + document store
    entity_dir = runtime_entity_file_root(entity_id)
    if not entity_dir:
        return "Entity filesystem is not enabled."

    blocked = await runtime_guard_file_mutation(
        entity_id=entity_id,
        user_id=user_id or runtime_context.user_id,
        conversation_id=runtime_context.conversation_id,
        tool_name="sandbox_save_result",
        action="save_file",
        paths=[safe_filename],
        approval_token=kwargs.get("approval_token"),
        content_preview={
            "save_as": safe_filename,
            "source": file_path or url,
            "bytes": len(content_bytes),
        },
    )
    if blocked:
        return blocked

    rel_path = safe_filename
    target = _os.path.join(entity_dir, rel_path)
    if _os.path.exists(target):
        import time as _time
        base, ext_part = _os.path.splitext(safe_filename)
        rel_path = f"{base}_{int(_time.time())}{ext_part}"
    try:
        target = runtime_write_entity_file_atomic(
            entity_id,
            rel_path,
            content_bytes,
            expected_size=len(content_bytes),
            allow_empty=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Entity filesystem is not available: {exc}"

    knowledge_sync_enabled = _coerce_bool(save_to_knowledge, True)
    rel_path = _os.path.relpath(target, entity_dir).replace(_os.sep, "/")
    mime_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    if not knowledge_sync_enabled:
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
        })

    try:
        from packages.core.database import async_session

        sync = await runtime_sync_entity_file_to_knowledge(
            entity_id=entity_id,
            abs_path=target,
            entity_root=entity_dir,
            source="sandbox",
            created_by=user_id or "ai-agent",
            force=True,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
            conversation_id=runtime_context.conversation_id,
            user_id=user_id or runtime_context.user_id,
            tool_name="sandbox_save_result",
        )
        async with async_session() as db:
            doc = (
                await runtime_get_document_for_entity(
                    db,
                    entity_id=entity_id,
                    document_id=sync.document_id,
                )
                if sync.document_id
                else None
            )
        if not doc:
            return f"Document sync failed: {sync.reason}"
        logger.info("[sandbox] save_result: registered %s → doc=%s", filename, doc.id)
        return json.dumps({
            "saved": True,
            "saved_to_knowledge": True,
            "display_as_artifact": display_as_artifact,
            "artifact_role": artifact_role,
            "document_id": doc.id,
            "name": doc.name,
            "file_size": doc.file_size,
            "mime_type": doc.mime_type,
            "fs_path": getattr(doc, "fs_path", rel_path),
            "result_url": f"/api/v1/fs/{entity_id}/{getattr(doc, 'fs_path', rel_path)}",
            "message": f"File '{doc.name}' saved to knowledge base.",
        })
    except Exception as exc:
        logger.exception("[sandbox] save_result doc register failed: %s", exc)
        return f"File downloaded but failed to register in document store: {exc}"


_SANDBOX_SAVE_RESULT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_save_result",
        "description": "Save sandbox output.",
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
    sandbox_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    sandbox_id = sandbox_id.strip()
    if not sandbox_id:
        return "sandbox_id is required."

    try:
        client = _get_client()
        try:
            await client.destroy(sandbox_id=sandbox_id)
        finally:
            await client.close()

        if conversation_id:
            await _delete_ctx(conversation_id)
        logger.info("[sandbox] destroyed: sandbox=%s", sandbox_id)
        return f"Sandbox {sandbox_id} destroyed."
    except Exception as exc:
        from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
        if isinstance(exc, SandboxNotFoundError):
            if conversation_id:
                await _delete_ctx(conversation_id)
            logger.info("[sandbox] destroy ignored; sandbox already gone: sandbox=%s", sandbox_id)
            return f"Sandbox {sandbox_id} was already destroyed."
        logger.exception("[sandbox] destroy failed: sandbox=%s error=%s", sandbox_id, exc)
        if conversation_id:
            await _delete_ctx(conversation_id)
        return f"Sandbox destroy failed: {exc}"


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
    logger.info("[sandbox] loading 6 sandbox tools (service=%s)", SANDBOX_SERVICE_URL)
    return [
        (_SANDBOX_CREATE_SCHEMA, _sandbox_create),
        (_SANDBOX_EXEC_SCHEMA, _sandbox_exec),
        (_SANDBOX_READ_FILE_SCHEMA, _sandbox_read_file),
        (_SANDBOX_WRITE_FILE_SCHEMA, _sandbox_write_file),
        (_SANDBOX_SAVE_RESULT_SCHEMA, _sandbox_save_result),
        (_SANDBOX_DESTROY_SCHEMA, _sandbox_destroy),
    ]
