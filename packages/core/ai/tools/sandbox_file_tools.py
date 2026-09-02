"""Sandbox file tools — retrieve files created in the sandbox and save to knowledge base.

When the agent runs code in the sandbox (e.g. python3 to generate a .pptx),
the file lives only inside the sandbox container. This tool fetches it and
registers it as a document in the entity's knowledge base.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any

from packages.core.ai.runtime.file_actions import (
    RuntimeFileCommitError,
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_mutation,
)
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler
from packages.core.services.generated_media_naming import (
    collision_safe_artifact_path,
    resolve_workspace_artifact_base_dir,
    scope_workspace_artifact_path,
)
from packages.core.services.workspace_layout import WorkspaceArtifactDir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SAVE_SANDBOX_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "save_sandbox_file",
        "description": (
            "Save a file created in the sandbox to the knowledge base. "
            "Use this after running code that generates files (e.g. python3 scripts "
            "that create .pptx, .xlsx, .pdf, images, etc.). The file will be "
            "retrieved from the sandbox and registered as a document."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "Sandbox ID returned by sandbox_create or invoke_skill.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Path inside the sandbox to read. Defaults to filename for compatibility.",
                },
                "filename": {
                    "type": "string",
                    "description": "Destination filename in Manor Knowledge, e.g. 'report.pptx'.",
                },
                "document_name": {
                    "type": "string",
                    "description": "Display name for the document in knowledge base. Defaults to filename.",
                },
                "approval_token": {
                    "type": "string",
                    "description": "One-time token returned after the user approves saving a user-visible file.",
                },
                "display_as_artifact": {
                    "type": "boolean",
                    "description": "Show chat card only for final deliverables; default false.",
                },
                "artifact_role": {
                    "type": "string",
                    "enum": ["intermediate", "final"],
                    "description": "intermediate hides; final shows as artifact.",
                },
            },
            "required": ["sandbox_id", "filename"],
        },
    },
}

LIST_SANDBOX_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_sandbox_files",
        "description": (
            "List files available in the sandbox output directory. "
            "Use this to check what files were created by sandbox commands."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "Sandbox ID returned by sandbox_create or invoke_skill.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory inside the sandbox to list. Defaults to /skill.",
                },
            },
            "required": ["sandbox_id"],
        },
    },
}

# ---------------------------------------------------------------------------
# Mime type mapping
# ---------------------------------------------------------------------------

_MIME_MAP: dict[str, str] = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "csv": "text/csv",
    "json": "application/json",
    "html": "text/html",
    "txt": "text/plain",
    "md": "text/markdown",
    "zip": "application/zip",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
}


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
    return os.getenv("SANDBOX_SERVICE_URL", "").strip()


def _get_sandbox_api_token() -> str:
    return os.getenv("SANDBOX_API_TOKEN", "").strip()


def _get_client():
    from packages.core.services.sandbox_sdk import SandboxClient

    return SandboxClient(
        base_url=_get_sandbox_service_url(),
        timeout=180.0,
        api_token=_get_sandbox_api_token(),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _save_sandbox_file(entity_id: str, user_id: str = "", **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_handler(kwargs, user_id=user_id)
    sandbox_id = str(kwargs.get("sandbox_id") or "").strip()
    filename = str(kwargs.get("filename") or "").strip()
    file_path = str(kwargs.get("file_path") or kwargs.get("path") or filename).strip()
    if not sandbox_id:
        return json.dumps({
            "error": (
                "sandbox_id is required. Use sandbox_save_result, or provide "
                "sandbox_id plus file_path for save_sandbox_file."
            )
        })
    if not filename:
        return json.dumps({"error": "filename is required"})
    if not file_path:
        return json.dumps({"error": "file_path is required"})

    document_name = kwargs.get("document_name") or filename
    display_as_artifact, artifact_role = _artifact_display_params(kwargs)
    safe_filename = os.path.basename(filename)
    if safe_filename != filename or safe_filename in {"", ".", ".."}:
        return json.dumps({"error": "filename must be a plain file name, not a path"})

    if not _get_sandbox_service_url():
        return json.dumps({"error": "Sandbox service not configured"})

    # Step 1: Retrieve file from sandbox
    try:
        client = _get_client()
        try:
            read_result = await client.read_file_base64(
                sandbox_id=sandbox_id,
                path=file_path,
            )
        finally:
            await client.close()
    except Exception as e:
        logger.error("Failed to retrieve file from sandbox: %s", e)
        return json.dumps({"error": f"Failed to retrieve file from sandbox: {e}"})

    # Step 2: Decode content
    content_bytes = base64.b64decode(read_result.content_base64)

    # Step 3: Save to entity filesystem
    entity_dir = runtime_entity_file_root(entity_id)
    if not entity_dir:
        return json.dumps({"error": "Entity filesystem is not enabled"})

    rel_path = safe_filename
    if runtime_context.workspace_id:
        try:
            workspace_base = await resolve_workspace_artifact_base_dir(
                entity_id=entity_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"Workspace artifact scope is unavailable: {exc}"})
        if not workspace_base:
            return json.dumps({"error": "Workspace artifact scope is unavailable"})
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
            tool_name="save_sandbox_file",
            action=FileMutationAction.SAVE_FILE,
            paths=[rel_path],
            approval_token=kwargs.get("approval_token"),
            content_preview={
                "save_as": rel_path,
                "source": file_path,
                "document_name": document_name,
                "bytes": len(content_bytes),
            },
            approval_payload={
                "save_as": rel_path,
                "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            },
        )
        if blocked:
            return blocked

        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        try:
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
                    tool_name="save_sandbox_file",
                    expected_content_sha256=content_sha256,
                )
                await transaction.commit()
        except RuntimeFileCommitError as exc:
            return json.dumps({
                "error": f"Entity filesystem is not available: {exc}",
                "saved": False,
            })
        except RuntimeFileProjectionError as exc:
            return json.dumps({
                "error": (
                    "Document sync failed and the saved file was rolled back: "
                    f"{exc.reason}"
                ),
                "saved": False,
                "knowledge_sync_reason": exc.reason,
            })
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"File was not committed: {exc}", "saved": False})

    saved_name = getattr(sync, "name", None) or os.path.basename(target)
    file_size = getattr(sync, "file_size", None)
    mime_type = getattr(sync, "mime_type", None)
    if file_size is None:
        file_size = len(content_bytes)
    if not mime_type:
        extension = os.path.splitext(saved_name)[1].lower().lstrip(".")
        mime_type = _MIME_MAP.get(extension, "application/octet-stream")
    result = {
        "saved": True,
        "display_as_artifact": display_as_artifact,
        "artifact_role": artifact_role,
        "document_id": sync.document_id,
        "name": saved_name,
        "file_size": file_size,
        "mime_type": mime_type,
        "message": f"File '{saved_name}' saved to knowledge base successfully.",
    }

    return json.dumps(result)


async def _list_sandbox_files(entity_id: str, **kwargs: Any) -> str:
    sandbox_id = str(kwargs.get("sandbox_id") or "").strip()
    path = str(kwargs.get("path") or "/skill").strip() or "/skill"
    if not sandbox_id:
        return json.dumps({"error": "sandbox_id is required"})
    if not _get_sandbox_service_url():
        return json.dumps({"error": "Sandbox service not configured"})

    try:
        import shlex

        client = _get_client()
        try:
            result = await client.exec(
                sandbox_id=sandbox_id,
                command=f"find {shlex.quote(path)} -maxdepth 3 -type f | sort | head -200",
                timeout=20,
            )
        finally:
            await client.close()
    except Exception as e:
        return json.dumps({"error": f"Failed to list sandbox files: {e}"})

    files = [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]
    return json.dumps({
        "sandbox_id": sandbox_id,
        "path": path,
        "files": files,
        "exit_code": result.exit_code,
        "stderr": result.stderr,
    })


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (SAVE_SANDBOX_FILE_SCHEMA, _save_sandbox_file),
        (LIST_SANDBOX_FILES_SCHEMA, _list_sandbox_files),
    ]
