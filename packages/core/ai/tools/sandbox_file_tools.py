"""Sandbox file tools — retrieve files created in the sandbox and save to knowledge base.

When the agent runs code in the sandbox (e.g. python3 to generate a .pptx),
the file lives only inside the sandbox container. This tool fetches it and
registers it as a document in the entity's knowledge base.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_get_document_for_entity,
    runtime_guard_file_mutation,
    runtime_sync_entity_file_to_knowledge,
    runtime_trigger_document_embeddings,
    runtime_write_entity_file_atomic,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

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
                "filename": {
                    "type": "string",
                    "description": "The filename to retrieve from the sandbox (e.g. 'report.pptx').",
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
            "required": ["filename"],
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
            "properties": {},
            "required": [],
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


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _save_sandbox_file(entity_id: str, **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    filename = kwargs.get("filename", "")
    if not filename:
        return json.dumps({"error": "filename is required"})

    document_name = kwargs.get("document_name") or filename
    display_as_artifact, artifact_role = _artifact_display_params(kwargs)
    safe_filename = os.path.basename(filename)
    if safe_filename != filename:
        return json.dumps({"error": "filename must be a plain file name, not a path"})

    sandbox_url = os.getenv("SANDBOX_SERVICE_URL", "")
    if not sandbox_url:
        return json.dumps({"error": "Sandbox service not configured"})

    # Step 1: Retrieve file from sandbox
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{sandbox_url.rstrip('/')}/files/retrieve",
                json={"path": filename},
            )
            if resp.status_code == 404:
                return json.dumps({
                    "error": f"File '{filename}' not found in sandbox. "
                    "Make sure the script saves files to /tmp/sandbox-output/ directory.",
                    "hint": "Modify your script to save output to /tmp/sandbox-output/",
                })
            if resp.status_code == 413:
                return json.dumps({"error": "File too large (max 50MB)"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error("Failed to retrieve file from sandbox: %s", e)
        return json.dumps({"error": f"Failed to retrieve file from sandbox: {e}"})

    # Step 2: Decode content
    content_bytes = base64.b64decode(data["content_base64"])

    # Step 3: Save to entity filesystem
    entity_dir = runtime_entity_file_root(entity_id)
    if not entity_dir:
        return json.dumps({"error": "Entity filesystem is not enabled"})

    blocked = await runtime_guard_file_mutation(
        entity_id=entity_id,
        user_id=kwargs.get("user_id") or runtime_context.user_id,
        conversation_id=runtime_context.conversation_id,
        tool_name="save_sandbox_file",
        action="save_file",
        paths=[safe_filename],
        approval_token=kwargs.get("approval_token"),
        content_preview={
            "save_as": safe_filename,
            "source": filename,
            "document_name": document_name,
            "bytes": len(content_bytes),
        },
    )
    if blocked:
        return blocked

    rel_path = safe_filename
    target = os.path.join(entity_dir, rel_path)

    # Avoid overwriting existing files
    if os.path.exists(target):
        import time as _time
        base, ext_part = os.path.splitext(safe_filename)
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
        return json.dumps({"error": f"Entity filesystem is not available: {exc}"})

    # Step 4: Register in documents database
    from packages.core.database import async_session

    sync = await runtime_sync_entity_file_to_knowledge(
        entity_id=entity_id,
        abs_path=target,
        entity_root=entity_dir,
        source="sandbox",
        created_by=kwargs.get("user_id") or runtime_context.user_id or "ai-agent",
        force=True,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
        agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
        conversation_id=runtime_context.conversation_id,
        user_id=kwargs.get("user_id") or runtime_context.user_id,
        tool_name="save_sandbox_file",
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
            return json.dumps({"error": f"Document sync failed: {sync.reason}"})

        # Trigger embedding for text-based documents
        if doc.file_type in ("txt", "md", "csv", "json", "html", "pdf"):
            runtime_trigger_document_embeddings(doc.id)

        result = {
            "saved": True,
            "display_as_artifact": display_as_artifact,
            "artifact_role": artifact_role,
            "document_id": doc.id,
            "name": doc.name,
            "file_size": doc.file_size,
            "mime_type": doc.mime_type,
            "message": f"File '{doc.name}' saved to knowledge base successfully.",
        }

    # Step 5: Clean up sandbox copy
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(
                f"{sandbox_url.rstrip('/')}/files/{safe_filename}"
            )
    except Exception:
        pass  # Non-critical

    return json.dumps(result)


async def _list_sandbox_files(entity_id: str, **kwargs: Any) -> str:
    sandbox_url = os.getenv("SANDBOX_SERVICE_URL", "")
    if not sandbox_url:
        return json.dumps({"error": "Sandbox service not configured"})

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{sandbox_url.rstrip('/')}/files")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return json.dumps({"error": f"Failed to list sandbox files: {e}"})

    return json.dumps(data)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (SAVE_SANDBOX_FILE_SCHEMA, _save_sandbox_file),
        (LIST_SANDBOX_FILES_SCHEMA, _list_sandbox_files),
    ]
