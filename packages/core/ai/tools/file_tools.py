"""File operation tools — read, write, list, glob, grep.

All operations are scoped to the entity's directory: {MANOR_FS_ROOT}/{entity_id}/
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from dataclasses import dataclass
import fnmatch
import hashlib
import io
from itertools import islice
import json
import logging
import os
import re
import stat
import time
from typing import Any

from packages.core.ai.runtime.file_actions import (
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_read_lock,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_mutation,
    runtime_guard_file_resource_access,
    runtime_normalize_entity_file_path,
    runtime_user_visible_file_path,
)
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler
from packages.core.contracts.audio_generation import GenerateFileKind
from packages.core.services.entity_fs import EntityFileVersion
from packages.core.services.workspace_layout import WorkspaceArtifactDir

logger = logging.getLogger(__name__)

READ_FILE_DEFAULT_LINES = 120
READ_FILE_MAX_LINES = 1000
READ_FILE_DEFAULT_CHARS = 12_000
READ_FILE_MAX_CHARS = 40_000
READ_FILE_MAX_IMAGE_BYTES = 12_000_000
LIST_FILES_DEFAULT_LIMIT = 100
LIST_FILES_MAX_LIMIT = 200
GLOB_FILES_DEFAULT_LIMIT = 100
GREP_FILES_DEFAULT_LIMIT = 50
FILE_SCAN_ACL_BATCH_SIZE = LIST_FILES_MAX_LIMIT

# grep_files scan bounds. Production incident: a 1.9 GB / 2410-file workspace
# was 99.7% binary (.mp4/.png/.wav) — the content scan opened and UTF-8-decoded
# every one of those bytes because nothing skipped binaries. These bounds are
# sized off that measurement: the real text in that workspace was 5.2 MB across
# 1593 files (~3.4 KB average), so none of them fire in the common case.
GREP_BINARY_SNIFF_BYTES = 8192  # same head-sniff size git/ripgrep use
GREP_MAX_FILE_READ_BYTES = 8 * 1024 * 1024  # per-file read cap
GREP_MAX_LINE_CHARS = 4096  # cap fed to regex.search per line/chunk
GREP_SCAN_BYTE_BUDGET = 32 * 1024 * 1024  # total bytes read per call
GREP_SCAN_WALL_CLOCK_SECONDS = 6.0  # total wall clock per call
EXTRACT_ONLY_EXTENSIONS = {
    ".doc", ".docx", ".wps", ".pdf", ".xlsx", ".xls", ".et", ".pptx", ".ppt", ".dps",
}

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read text/images.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative entity path.",
                },
                "offset": {
                    "type": "integer",
                    "description": "0-based line offset.",
                },
                "char_offset": {
                    "type": "integer",
                    "description": "Character offset continuation.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max chars.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Previous source_sha256 guard.",
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write/create a user-facing file; use edit_file for small edits.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative entity path.",
                },
                "content": {
                    "type": "string",
                    "description": "Content.",
                },
                "approval_token": {
                    "type": "string",
                    "description": "Approval token.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Previous source_sha256 guard.",
                },
                "storage_scope": {
                    "type": "string",
                    "enum": ["task", "workspace"],
                    "default": "task",
                    "description": (
                        "Use workspace only for durable shared Workspace files; "
                        "normal generated files stay task-scoped."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
}

LIST_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List raw entity filesystem paths.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Recursive.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset.",
                },
            },
            "required": [],
        },
    },
}

GLOB_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "glob_files",
        "description": "Glob raw entity filesystem paths.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern over relative paths. `**` matches zero or "
                        "more directories, so `**/*.pdf` includes PDFs at the "
                        "entity root and below nested directories."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset.",
                },
            },
            "required": ["pattern"],
        },
    },
}

GREP_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep_files",
        "description": "Regex search raw entity file contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative directory.",
                },
                "file_glob": {
                    "type": "string",
                    "description": "File glob filter.",
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Max matches.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Match offset.",
                },
                "after": {
                    "type": "string",
                },
            },
            "required": ["pattern"],
        },
    },
}

EDIT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Edit a file. Text/docx/pptx: old_text/new_text. .xlsx/.xlsm: set_cell, update_row, append_row, add_sheet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                },
                "operation": {
                    "type": "string",
                    "enum": ["replace_text", "set_cell", "update_row", "append_row", "add_sheet"],
                },
                "old_text": {
                    "type": "string",
                },
                "new_text": {
                    "type": "string",
                },
                "replace_all": {
                    "type": "boolean",
                },
                "sheet": {
                    "type": "string",
                },
                "new_sheet": {
                    "type": "string",
                },
                "new_sheet_name": {
                    "type": "string",
                },
                "cell": {
                    "type": "string",
                },
                "value": {
                    "type": ["string", "number", "boolean", "null"],
                },
                "header_row": {
                    "type": "integer",
                },
                "match_column": {
                    "type": "string",
                },
                "match_value": {
                    "type": ["string", "number", "boolean", "null"],
                },
                "match_mode": {
                    "type": "string",
                    "enum": ["exact", "contains"],
                },
                "match_case_sensitive": {
                    "type": "boolean",
                },
                "updates": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                },
                "row": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                },
                "values": {
                    "type": "array",
                    "items": {"type": ["string", "number", "boolean", "null"]},
                },
                "approval_token": {
                    "type": "string",
                    "description": "Approval token.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Previous source_sha256 guard.",
                },
            },
            "required": ["path"],
        },
    },
}

INSPECT_FILE_ENGINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "inspect_file_engine",
        "description": "List unified file engine generate/patch capabilities by file type.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_type": {
                    "type": "string",
                    "description": "Optional extension such as docx, xlsx, pptx, md, json.",
                },
            },
            "required": [],
        },
    },
}

PATCH_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch_file",
        "description": (
            "Patch an existing user-visible file through the same Knowledge save "
            "boundary as generate_file. Use this instead of regenerating a whole "
            "document when the user asks to edit an existing file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative entity/Knowledge file path.",
                },
                "operations": {
                    "type": "array",
                    "description": "Patch operations. The first release accepts exactly one operation.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "description": (
                                    "replace_text/text.replace, set_cell/cell.set, "
                                    "update_row/row.update, append_row/row.append, "
                                    "or add_sheet/sheet.add."
                                ),
                            },
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                            "sheet": {"type": "string"},
                            "new_sheet": {"type": "string"},
                            "new_sheet_name": {"type": "string"},
                            "cell": {"type": "string"},
                            "value": {"type": ["string", "number", "boolean", "null"]},
                            "header_row": {"type": "integer"},
                            "match_column": {"type": "string"},
                            "match_value": {"type": ["string", "number", "boolean", "null"]},
                            "match_mode": {"type": "string", "enum": ["exact", "contains"]},
                            "match_case_sensitive": {"type": "boolean"},
                            "updates": {
                                "type": "object",
                                "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                            },
                            "row": {
                                "type": "object",
                                "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                            },
                            "values": {
                                "type": "array",
                                "items": {"type": ["string", "number", "boolean", "null"]},
                            },
                        },
                        "required": ["op"],
                        "additionalProperties": True,
                    },
                },
                "approval_token": {
                    "type": "string",
                    "description": "Approval token.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Previous source_sha256 guard.",
                },
            },
            "required": ["path", "operations"],
        },
    },
}

DELETE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "Delete a file or empty directory from the entity's filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within entity directory.",
                },
                "approval_token": {
                    "type": "string",
                    "description": "One-time token returned after the user approves deleting a user-visible file.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional previous source_sha256; refuses delete if current file changed.",
                },
            },
            "required": ["path"],
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_entity_root(entity_id: str) -> str | None:
    """Return the entity's filesystem root, or None if FS is disabled."""
    return runtime_entity_file_root(entity_id)


def _runtime_file_tool_context(kwargs: dict[str, Any]):
    """Resolve trusted direct-call identity without trusting model arguments."""
    return runtime_tool_call_context_from_handler(
        kwargs,
        user_id=str(kwargs.get("user_id") or "").strip() or None,
    )


async def _blocked_doc_paths(
    entity_id: str,
    rel_paths: list[str],
    kwargs: dict,
    *,
    directory_paths: list[str] | None = None,
) -> set[str]:
    """Normalized rel paths that map to a Knowledge document this agent turn's
    user may not read.

    Knowledge documents are real files under the entity root, so the raw file
    tools would otherwise read a private document's bytes without honoring
    ``Document.visibility``. We gate on the same guard the ``/documents`` API
    uses. Agent reads fail closed when the turn has no user identity.
    """
    runtime_context = _runtime_file_tool_context(kwargs)
    user_id = runtime_context.user_id
    if not rel_paths and not directory_paths:
        return set()
    from packages.core.database import async_session
    from packages.core.services.document_access import unreadable_document_paths

    async with async_session() as db:
        return await unreadable_document_paths(
            db,
            entity_id=entity_id,
            rel_paths=rel_paths,
            directory_paths=directory_paths,
            user_id=user_id,
            workspace_id=runtime_context.workspace_id,
            actor_type="agent",
        )


async def _blocked_doc_paths_batched(
    entity_id: str,
    rel_paths: list[str],
    kwargs: dict,
    *,
    directory_paths: list[str] | None = None,
) -> set[str]:
    """Resolve large candidate sets without exceeding database bind limits."""
    blocked: set[str] = set()
    for start in range(0, len(rel_paths), 500):
        blocked.update(
            await _blocked_doc_paths(
                entity_id,
                rel_paths[start:start + 500],
                kwargs,
            )
        )
    directories = directory_paths or []
    for start in range(0, len(directories), 500):
        blocked.update(
            await _blocked_doc_paths(
                entity_id,
                [],
                kwargs,
                directory_paths=directories[start:start + 500],
            )
        )
    return blocked


def _safe_path(entity_root: str, relative_path: str) -> str | None:
    """Resolve a path and ensure it stays within entity_root. Returns None if escape detected."""
    root = os.path.realpath(entity_root)
    abs_path = os.path.realpath(os.path.join(root, relative_path))
    if os.path.commonpath([root, abs_path]) != root:
        return None
    return abs_path


def _user_visible_rel_path(entity_root: str, abs_path: str) -> str | None:
    rel_path = runtime_normalize_entity_file_path(os.path.relpath(abs_path, entity_root))
    if not rel_path or not runtime_user_visible_file_path(rel_path):
        return None
    return rel_path


async def _workspace_scoped_new_file_path(
    *,
    entity_id: str,
    entity_root: str,
    workspace_id: str | None,
    task_id: str | None = None,
    path: str,
    expected_sha256: str | None = None,
) -> str:
    """Route new workspace file writes under the workspace artifact folder."""
    if not workspace_id:
        return path
    if str(expected_sha256 or "").strip():
        return path

    original_abs = _safe_path(entity_root, path)
    if not original_abs:
        return path

    rel_path = runtime_normalize_entity_file_path(path)
    if not rel_path or not runtime_user_visible_file_path(rel_path):
        return path
    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
    )

    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    if not workspace_base_dir:
        return rel_path
    scoped = scope_workspace_artifact_path(
        rel_path,
        workspace_base_dir,
        default_subdir=WorkspaceArtifactDir.DOCUMENTS.value,
    )
    return runtime_normalize_entity_file_path(scoped) or rel_path


async def _workspace_scoped_existing_path(
    *,
    entity_id: str,
    entity_root: str,
    workspace_id: str | None,
    task_id: str | None = None,
    path: str,
) -> str | None:
    """Where a prior *write* of this logical name would have been rerouted.

    `_write_file` scopes NEW workspace files under the workspace artifact
    folder; read/edit/delete must consult the SAME location so they address
    the file the write created, not a non-existent literal path (the write-A
    read-B fork). Returns the scoped abs_path if that file exists, else None.
    """
    if not workspace_id:
        return None
    rel = runtime_normalize_entity_file_path(path)
    if not rel or not runtime_user_visible_file_path(rel):
        return None
    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
    )

    base = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
    )
    if not base:
        return None
    scoped = runtime_normalize_entity_file_path(
        scope_workspace_artifact_path(rel, base, default_subdir=WorkspaceArtifactDir.DOCUMENTS.value)
    ) or rel
    scoped_abs = _safe_path(entity_root, scoped)
    if scoped_abs and os.path.isfile(scoped_abs):
        return scoped_abs
    return None


def _same_basename_candidates(entity_root: str, path: str, *, limit: int = 5) -> list[str]:
    """Entity-relative paths of files sharing this basename — so a not-found
    error tells the model where the file actually is instead of dead-ending."""
    target = os.path.basename(str(path or "").replace("\\", "/").rstrip("/"))
    if not target:
        return []
    root = os.path.realpath(entity_root)
    found: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            directory
            for directory in dirnames
            if runtime_user_visible_file_path(
                os.path.relpath(os.path.join(dirpath, directory), root)
            )
        ]
        for fn in filenames:
            scanned += 1
            if scanned > 5000:
                return found
            if fn == target:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                rel = rel.replace("\\", "/")
                if not runtime_user_visible_file_path(rel):
                    continue
                found.append(rel)
                if len(found) >= limit:
                    return found
    return found


async def _locate_entity_file(
    *,
    entity_id: str,
    entity_root: str,
    path: str,
    workspace_id: str | None,
    task_id: str | None = None,
) -> tuple[str | None, list[str]]:
    """Resolve an EXISTING file for read/edit/delete through ONE code path.

    In a Workspace, prefer the scoped location a prior write would have used;
    an already-canonical scoped path resolves to the same file. Outside a
    Workspace, resolve the literal path. On miss, return same-basename
    candidates so the caller can emit a self-correcting error.
    """
    scoped = await _workspace_scoped_existing_path(
        entity_id=entity_id,
        entity_root=entity_root,
        workspace_id=workspace_id,
        task_id=task_id,
        path=path,
    )
    if scoped:
        return scoped, []
    literal = _safe_path(entity_root, path)
    if literal and os.path.isfile(literal):
        return literal, []
    return None, _same_basename_candidates(entity_root, path)


async def _readable_file_candidates(
    entity_id: str,
    candidates: list[str],
    kwargs: dict,
) -> list[str]:
    if not candidates:
        return []
    blocked = await _blocked_doc_paths(entity_id, candidates, kwargs)
    if not blocked:
        return candidates
    from packages.core.services.knowledge_visibility import normalize_rel_path

    return [
        candidate
        for candidate in candidates
        if normalize_rel_path(candidate) not in blocked
    ]


def _not_found_result(path: str, candidates: list[str]) -> str:
    payload: dict[str, Any] = {"error": f"File not found: {path}"}
    if candidates:
        payload["candidates"] = candidates
        payload["hint"] = (
            "No file at that exact path, but a file with the same name exists "
            "elsewhere. Retry with one of `candidates` (these are the real paths)."
        )
    return json.dumps(payload)


_FS_DISABLED_MSG = json.dumps({
    "error": "Entity filesystem is not enabled. Set MANOR_FS_ENABLED=true and MANOR_FS_ROOT.",
})


def _bounded_int(value: Any, default: int, maximum: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _file_meta(abs_path: str, content: str) -> dict[str, Any]:
    stat = os.stat(abs_path)
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "source_sha256": _text_sha256(content),
    }


async def _commit_file_projection(
    *,
    entity_id: str,
    rel_path: str,
    data: bytes,
    entity_root: str,
    runtime_context: Any,
    tool_name: str,
    allow_empty: bool,
    expected_source_version: EntityFileVersion | None,
) -> tuple[str, Any, dict[str, Any]]:
    """Commit bytes and their Knowledge row through the shared transaction."""
    content_sha256 = hashlib.sha256(data).hexdigest()
    async with RuntimeFileProjectionTransactionFactory.create(entity_id) as transaction:
        abs_path = transaction.write_bytes(
            rel_path,
            data,
            expected_content_sha256=content_sha256,
            expected_size=len(data),
            allow_empty=allow_empty,
            expected_source_version=expected_source_version,
        )
        sync = await transaction.project_file(
            abs_path=abs_path,
            entity_root=entity_root,
            source="agent",
            created_by=runtime_context.user_id or "ai-agent",
            force=True,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            agent_id=runtime_context.agent_id,
            conversation_id=runtime_context.conversation_id,
            user_id=runtime_context.user_id,
            tool_name=tool_name,
            expected_content_sha256=content_sha256,
        )
        written_content = await _read_supported_text(
            abs_path,
            file_type_path=rel_path,
        )
        written_meta = _file_meta(abs_path, written_content)
        await transaction.commit()
    return abs_path, sync, written_meta


def _projection_failure_payload(
    exc: RuntimeFileProjectionError,
    *,
    result_flag: str,
) -> dict[str, Any]:
    return {
        "error": (
            "Knowledge projection failed and the file mutation was rolled back: "
            f"{exc.reason}"
        ),
        result_flag: False,
        "knowledge_synced": False,
        "knowledge_sync_reason": exc.reason,
    }


def _extract_docx_text(abs_path: str) -> str:
    """Extract plain text from a .docx file."""
    try:
        from docx import Document
        doc = Document(abs_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        # Also extract text from tables
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    paragraphs.append(" | ".join(cells))
        return "\n".join(paragraphs)
    except ImportError:
        # Fallback: extract raw XML text via zipfile
        import zipfile
        from defusedxml import ElementTree as ET
        with zipfile.ZipFile(abs_path, "r") as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        texts = [t.text for t in tree.iter(f"{{{ns['w']}}}t") if t.text]
        return "\n".join(texts)


async def _read_supported_text(abs_path: str, *, file_type_path: str | None = None) -> str:
    ext = os.path.splitext(file_type_path or abs_path)[1].lower()
    if ext == ".docx":
        return _extract_docx_text(abs_path)
    if ext in {".doc", ".wps", ".pdf", ".xlsx", ".xls", ".et", ".pptx", ".ppt", ".dps"}:
        from packages.core.services.text_extraction import extract_text
        return await extract_text(abs_path, file_type=ext.lstrip("."))
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _spreadsheet_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _normalize_sheet_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _spreadsheet_text(value: Any, *, case_sensitive: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    return text if case_sensitive else text.lower()


def _spreadsheet_values_match(
    actual: Any,
    expected: Any,
    *,
    mode: str,
    case_sensitive: bool,
) -> bool:
    actual_text = _spreadsheet_text(actual, case_sensitive=case_sensitive)
    expected_text = _spreadsheet_text(expected, case_sensitive=case_sensitive)
    if mode == "contains":
        return expected_text in actual_text
    return actual_text == expected_text


def _spreadsheet_sheet(workbook, sheet_name: str | None):
    if sheet_name:
        if sheet_name in workbook.sheetnames:
            return workbook[sheet_name]
        lowered = sheet_name.strip().lower()
        for candidate in workbook.sheetnames:
            if candidate.lower() == lowered:
                return workbook[candidate]
        raise ValueError(f"Sheet not found: {sheet_name}")
    return workbook.active


def _spreadsheet_header_map(ws, header_row: int) -> dict[str, int]:
    if header_row < 1:
        raise ValueError("header_row must be >= 1")
    header_map: dict[str, int] = {}
    for cell in ws[header_row]:
        key = _normalize_sheet_key(cell.value)
        if key and key not in header_map:
            header_map[key] = int(cell.column)
    if not header_map:
        raise ValueError(f"No headers found on row {header_row}")
    return header_map


def _spreadsheet_column_index(header_map: dict[str, int], column_name: str) -> int:
    key = _normalize_sheet_key(column_name)
    if not key:
        raise ValueError("Column name is required")
    if key not in header_map:
        known = ", ".join(sorted(header_map.keys())[:20])
        raise ValueError(f"Column not found: {column_name}. Known columns: {known}")
    return header_map[key]


def _replace_text_limited(
    text: str,
    old_text: str,
    new_text: str,
    remaining: int | None,
) -> tuple[str, int, int | None]:
    if old_text not in text:
        return text, 0, remaining
    available = text.count(old_text)
    if remaining is None:
        return text.replace(old_text, new_text), available, None
    replacement_count = min(available, remaining)
    return text.replace(old_text, new_text, replacement_count), replacement_count, remaining - replacement_count


def _set_rich_paragraph_text(paragraph: Any, text: str) -> None:
    try:
        paragraph.text = text
        return
    except Exception:
        pass
    if hasattr(paragraph, "clear"):
        paragraph.clear()
    if hasattr(paragraph, "add_run"):
        run = paragraph.add_run()
        run.text = text


def _replace_in_rich_paragraph(
    paragraph: Any,
    old_text: str,
    new_text: str,
    remaining: int | None,
) -> tuple[int, int | None]:
    replacements = 0
    for run in getattr(paragraph, "runs", []):
        run_text = str(getattr(run, "text", "") or "")
        updated, count, remaining = _replace_text_limited(run_text, old_text, new_text, remaining)
        if count:
            run.text = updated
            replacements += count
            if remaining == 0:
                return replacements, remaining

    paragraph_text = str(getattr(paragraph, "text", "") or "")
    if old_text in paragraph_text:
        updated, count, remaining = _replace_text_limited(paragraph_text, old_text, new_text, remaining)
        if count:
            _set_rich_paragraph_text(paragraph, updated)
            replacements += count
    return replacements, remaining


def _iter_docx_table_paragraphs(tables: Any):
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
                yield from _iter_docx_table_paragraphs(cell.tables)


def _iter_docx_paragraphs(doc: Any):
    yield from doc.paragraphs
    yield from _iter_docx_table_paragraphs(doc.tables)
    for section in doc.sections:
        for part_name in (
            "header",
            "footer",
            "first_page_header",
            "first_page_footer",
            "even_page_header",
            "even_page_footer",
        ):
            part = getattr(section, part_name, None)
            if part is None:
                continue
            yield from part.paragraphs
            yield from _iter_docx_table_paragraphs(part.tables)


def _replace_docx_sync(abs_path: str, old_text: str, new_text: str, replace_all: bool) -> dict[str, Any]:
    from docx import Document

    doc = Document(abs_path)
    remaining: int | None = None if replace_all else 1
    replacements = 0
    for paragraph in _iter_docx_paragraphs(doc):
        count, remaining = _replace_in_rich_paragraph(paragraph, old_text, new_text, remaining)
        replacements += count
        if remaining == 0:
            break
    if replacements <= 0:
        return {"error": "old_text not found in file. Ensure exact match including whitespace."}
    output = io.BytesIO()
    doc.save(output)
    return {
        "edited": True,
        "operation": "replace_text",
        "file_type": "docx",
        "replacements": replacements,
        "_persisted_bytes": output.getvalue(),
    }


def _iter_pptx_shapes(shapes: Any):
    for shape in shapes:
        yield shape
        child_shapes = getattr(shape, "shapes", None)
        if child_shapes is not None:
            yield from _iter_pptx_shapes(child_shapes)


def _iter_pptx_paragraphs(presentation: Any):
    for slide in presentation.slides:
        for shape in _iter_pptx_shapes(slide.shapes):
            if getattr(shape, "has_text_frame", False):
                yield from shape.text_frame.paragraphs
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        yield from cell.text_frame.paragraphs


def _replace_pptx_sync(abs_path: str, old_text: str, new_text: str, replace_all: bool) -> dict[str, Any]:
    from pptx import Presentation

    presentation = Presentation(abs_path)
    remaining: int | None = None if replace_all else 1
    replacements = 0
    for paragraph in _iter_pptx_paragraphs(presentation):
        count, remaining = _replace_in_rich_paragraph(paragraph, old_text, new_text, remaining)
        replacements += count
        if remaining == 0:
            break
    if replacements <= 0:
        return {"error": "old_text not found in file. Ensure exact match including whitespace."}
    output = io.BytesIO()
    presentation.save(output)
    return {
        "edited": True,
        "operation": "replace_text",
        "file_type": "pptx",
        "replacements": replacements,
        "_persisted_bytes": output.getvalue(),
    }


def _spreadsheet_edit_sync(abs_path: str, params: dict[str, Any]) -> dict[str, Any]:
    from openpyxl import load_workbook
    from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

    ext = os.path.splitext(abs_path)[1].lower()
    if ext in {".xls", ".et"}:
        return {
            "error": "unsupported_spreadsheet_format",
            "hint": "Convert legacy .xls/.et files to .xlsx before editing.",
        }
    if ext not in {".xlsx", ".xlsm"}:
        return {"error": "not_a_spreadsheet", "hint": "Spreadsheet edit operations only support .xlsx/.xlsm files."}

    keep_vba = ext == ".xlsm"
    wb = load_workbook(abs_path, keep_vba=keep_vba)
    try:
        operation = str(params.get("operation") or "").strip().lower()
        sheet_name = str(params.get("sheet") or "").strip() or None
        ws = None if operation == "add_sheet" else _spreadsheet_sheet(wb, sheet_name)
        updated_cells: dict[str, dict[str, Any]] = {}

        if operation == "set_cell":
            cell_ref = str(params.get("cell") or "").strip()
            if not cell_ref:
                return {"error": "cell is required for set_cell"}
            try:
                coordinate_to_tuple(cell_ref)
            except Exception:
                return {"error": f"Invalid cell reference: {cell_ref}"}
            cell = ws[cell_ref]
            old_value = cell.value
            new_value = _spreadsheet_json_value(params.get("value"))
            cell.value = new_value
            updated_cells[cell.coordinate] = {"old": old_value, "new": new_value}
            result: dict[str, Any] = {
                "updated": True,
                "operation": operation,
                "sheet": ws.title,
                "cell": cell.coordinate,
                "updated_cells": updated_cells,
            }

        elif operation == "update_row":
            header_row = _bounded_int(params.get("header_row"), 1, 1000, 1)
            updates = params.get("updates")
            if not isinstance(updates, dict) or not updates:
                return {"error": "updates object is required for update_row"}
            header_map = _spreadsheet_header_map(ws, header_row)
            match_col = _spreadsheet_column_index(header_map, str(params.get("match_column") or ""))
            match_value = params.get("match_value")
            match_mode = str(params.get("match_mode") or "exact").strip().lower()
            if match_mode not in {"exact", "contains"}:
                return {"error": "match_mode must be exact or contains"}
            case_sensitive = bool(params.get("match_case_sensitive"))
            matches: list[int] = []
            for row_idx in range(header_row + 1, ws.max_row + 1):
                if _spreadsheet_values_match(
                    ws.cell(row=row_idx, column=match_col).value,
                    match_value,
                    mode=match_mode,
                    case_sensitive=case_sensitive,
                ):
                    matches.append(row_idx)
            if not matches:
                return {
                    "error": "row_not_found",
                    "match_column": params.get("match_column"),
                    "match_value": match_value,
                }
            if len(matches) > 1:
                return {
                    "error": "multiple_rows_matched",
                    "row_numbers": matches[:50],
                    "hint": "Use a more specific match_value or match_column before updating.",
                }
            row_idx = matches[0]
            for column_name, raw_value in updates.items():
                col_idx = _spreadsheet_column_index(header_map, str(column_name))
                cell = ws.cell(row=row_idx, column=col_idx)
                old_value = cell.value
                new_value = _spreadsheet_json_value(raw_value)
                cell.value = new_value
                updated_cells[cell.coordinate] = {"old": old_value, "new": new_value}
            result = {
                "updated": True,
                "operation": operation,
                "sheet": ws.title,
                "row_number": row_idx,
                "updated_cells": updated_cells,
            }

        elif operation == "append_row":
            row_object = params.get("row")
            values = params.get("values")
            next_row = ws.max_row + 1
            if isinstance(row_object, dict) and row_object:
                header_row = _bounded_int(params.get("header_row"), 1, 1000, 1)
                header_map = _spreadsheet_header_map(ws, header_row)
                for column_name, raw_value in row_object.items():
                    col_idx = _spreadsheet_column_index(header_map, str(column_name))
                    cell = ws.cell(row=next_row, column=col_idx)
                    new_value = _spreadsheet_json_value(raw_value)
                    cell.value = new_value
                    updated_cells[cell.coordinate] = {"old": None, "new": new_value}
            elif isinstance(values, list):
                for col_idx, raw_value in enumerate(values, start=1):
                    cell = ws.cell(row=next_row, column=col_idx)
                    new_value = _spreadsheet_json_value(raw_value)
                    cell.value = new_value
                    updated_cells[cell.coordinate] = {"old": None, "new": new_value}
            else:
                return {"error": "row or values is required for append_row"}
            result = {
                "updated": True,
                "operation": operation,
                "sheet": ws.title,
                "row_number": next_row,
                "updated_cells": updated_cells,
            }

        elif operation == "add_sheet":
            new_sheet_name = str(
                params.get("new_sheet")
                or params.get("new_sheet_name")
                or params.get("sheet")
                or ""
            ).strip()
            if not new_sheet_name:
                return {"error": "sheet is required for add_sheet"}
            if new_sheet_name.lower() in {name.lower() for name in wb.sheetnames}:
                return {"error": "sheet_already_exists", "sheet": new_sheet_name}
            ws = wb.create_sheet(title=new_sheet_name)
            result = {
                "updated": True,
                "operation": operation,
                "sheet": ws.title,
                "sheet_names": list(wb.sheetnames),
                "updated_cells": updated_cells,
            }

        else:
            return {"error": "operation must be set_cell, update_row, append_row, or add_sheet"}

        output = io.BytesIO()
        wb.save(output)
        if updated_cells:
            result["range"] = ", ".join(
                f"{get_column_letter(coordinate_to_tuple(cell)[1])}{coordinate_to_tuple(cell)[0]}"
                for cell in updated_cells
            )
        result["_persisted_bytes"] = output.getvalue()
        return result
    finally:
        wb.close()


@dataclass(frozen=True)
class ExpectedSourceGuardResult:
    error: str | None = None
    version: EntityFileVersion | None = None
    source_sha256: str | None = None

    def __bool__(self) -> bool:
        return self.error is not None


async def _guard_expected_source_sha(
    *,
    abs_path: str,
    path: str,
    expected_sha256: str,
) -> ExpectedSourceGuardResult:
    expected_sha256 = (expected_sha256 or "").strip()
    if not expected_sha256:
        return ExpectedSourceGuardResult()
    try:
        descriptor = os.open(abs_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ExpectedSourceGuardResult(error=json.dumps({
            "error": "source_missing",
            "path": path,
            "expected_sha256": expected_sha256,
            "hint": "The expected source file no longer exists; re-read or recreate intentionally.",
        }))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("source is not a regular file")
        descriptor_root = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
        content = await _read_supported_text(
            os.path.join(descriptor_root, str(descriptor)),
            file_type_path=abs_path,
        )
        after = os.fstat(descriptor)
    except OSError:
        return ExpectedSourceGuardResult(error=json.dumps({
            "error": "source_changed",
            "path": path,
            "expected_sha256": expected_sha256,
            "hint": "The source changed while it was being validated; read it again.",
        }))
    finally:
        os.close(descriptor)
    version = EntityFileVersion.from_stat(before)
    if not version.matches(after):
        return ExpectedSourceGuardResult(error=json.dumps({
            "error": "source_changed",
            "path": path,
            "expected_sha256": expected_sha256,
            "hint": "The source changed while it was being validated; read it again.",
        }))
    source_sha256 = _text_sha256(content)
    if expected_sha256 != source_sha256:
        return ExpectedSourceGuardResult(error=json.dumps({
            "error": "source_changed",
            "path": path,
            "expected_sha256": expected_sha256,
            "source_sha256": source_sha256,
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "hint": "The file changed since it was read; read it again before editing or overwriting.",
        }))
    return ExpectedSourceGuardResult(
        version=version,
        source_sha256=source_sha256,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _read_file_locked(entity_id: str, **kwargs: Any) -> str:
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    requested_path = str(kwargs.get("path", "") or "")
    if _safe_path(root, requested_path) is None:
        return json.dumps({"error": "Path traversal detected"})
    abs_path, candidates = await _locate_entity_file(
        entity_id=entity_id, entity_root=root, path=requested_path,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
    )
    if not abs_path:
        candidates = await _readable_file_candidates(entity_id, candidates, kwargs)
        return _not_found_result(requested_path, candidates)

    resolved_path = _user_visible_rel_path(root, abs_path)
    if not resolved_path:
        return json.dumps({"error": f"File not found: {requested_path}"})
    if await _blocked_doc_paths(entity_id, [resolved_path], kwargs):
        # Private Knowledge document the caller cannot read: report as missing
        # so the tool never confirms that the document exists.
        return json.dumps({"error": f"File not found: {requested_path}"})

    image_mime_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(os.path.splitext(abs_path)[1].lower())
    if image_mime_type:
        try:
            with open(abs_path, "rb") as image_file:
                image_bytes = image_file.read(READ_FILE_MAX_IMAGE_BYTES + 1)
            if len(image_bytes) > READ_FILE_MAX_IMAGE_BYTES:
                return json.dumps({
                    "error": "image_too_large",
                    "path": requested_path,
                    "max_bytes": READ_FILE_MAX_IMAGE_BYTES,
                })
            stat = os.stat(abs_path)
            source_sha256 = hashlib.sha256(image_bytes).hexdigest()
            expected_sha256 = str(kwargs.get("expected_sha256") or "").strip()
            if expected_sha256 and expected_sha256 != source_sha256:
                return json.dumps({
                    "error": "source_changed",
                    "path": requested_path,
                    "expected_sha256": expected_sha256,
                    "source_sha256": source_sha256,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "hint": "The image changed since it was read; read it again before relying on it.",
                })
            data_url = (
                f"data:{image_mime_type};base64,"
                + base64.b64encode(image_bytes).decode("ascii")
            )
            return json.dumps({
                "path": requested_path,
                "resolved_path": resolved_path,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "source_sha256": source_sha256,
                "image": {
                    "data_url": data_url,
                    "mime_type": image_mime_type,
                    "bytes": len(image_bytes),
                    "delivery": "ephemeral_multimodal",
                },
            })
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)})

    offset = _bounded_int(kwargs.get("offset"), 0, 1_000_000, 0)
    char_offset_arg = kwargs.get("char_offset")
    char_offset = (
        _bounded_int(char_offset_arg, 0, 100_000_000, 0)
        if char_offset_arg is not None
        else None
    )
    limit = _bounded_int(kwargs.get("limit"), READ_FILE_DEFAULT_LINES, READ_FILE_MAX_LINES, 1)
    max_chars = _bounded_int(kwargs.get("max_chars"), READ_FILE_DEFAULT_CHARS, READ_FILE_MAX_CHARS, 1_000)

    try:
        content = await _read_supported_text(abs_path)
        meta = _file_meta(abs_path, content)
        expected_sha256 = str(kwargs.get("expected_sha256") or "").strip()
        if expected_sha256 and expected_sha256 != meta["source_sha256"]:
            return json.dumps({
                "error": "source_changed",
                "path": requested_path,
                "expected_sha256": expected_sha256,
                "source_sha256": meta["source_sha256"],
                "size": meta["size"],
                "mtime_ns": meta["mtime_ns"],
                "hint": "The file changed since the previous slice; restart from offset=0.",
            })

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        total_chars = len(content)
        mode = "line"
        start_char_offset = 0
        next_char_offset = None
        partial_line = False
        if char_offset is not None:
            mode = "char"
            start_char_offset = min(char_offset, total_chars)
            content_out = content[start_char_offset : start_char_offset + max_chars]
            selected = content_out.splitlines(keepends=True)
            truncated_by_chars = start_char_offset + len(content_out) < total_chars
            next_offset = None
            next_char_offset = (
                start_char_offset + len(content_out)
                if truncated_by_chars
                else None
            )
        else:
            start_char_offset = sum(len(line) for line in lines[:offset])
            selected = []
            chars_used = 0
            truncated_by_chars = False
            partial = ""
            for line in lines[offset : offset + limit]:
                if chars_used + len(line) <= max_chars:
                    selected.append(line)
                    chars_used += len(line)
                    continue
                truncated_by_chars = True
                if not selected:
                    partial = line[:max_chars]
                    chars_used = len(partial)
                    partial_line = True
                break
            content_out = partial if partial_line else "".join(selected)
            if partial_line:
                selected = []
                next_offset = None
            else:
                next_offset = offset + len(selected) if offset + len(selected) < total_lines else None
            if truncated_by_chars:
                next_char_offset = start_char_offset + len(content_out)
        hint = None
        if truncated_by_chars and next_char_offset is not None:
            hint = (
                "Content hit max_chars; call read_file with char_offset=next_char_offset "
                "and expected_sha256=source_sha256 to continue exactly, or use a "
                "smaller limit/higher max_chars."
            )
        elif truncated_by_chars:
            hint = "Content hit max_chars; use a smaller limit or higher max_chars."
        elif next_offset is not None:
            hint = "Call read_file again with offset=next_offset and expected_sha256=source_sha256 to continue."
        elif next_char_offset is not None:
            hint = "Call read_file again with char_offset=next_char_offset and expected_sha256=source_sha256 to continue."

        return json.dumps({
            "path": requested_path,
            "resolved_path": resolved_path,
            "size": meta["size"],
            "mtime_ns": meta["mtime_ns"],
            "source_sha256": meta["source_sha256"],
            "slice_sha256": _text_sha256(content_out),
            "total_lines": total_lines,
            "total_chars": total_chars,
            "mode": mode,
            "offset": offset,
            "char_offset": start_char_offset,
            "lines_returned": len(selected),
            "next_offset": next_offset,
            "next_char_offset": next_char_offset,
            "partial_line": partial_line,
            "truncated": truncated_by_chars or next_offset is not None,
            "char_truncated": truncated_by_chars,
            "line_truncated": next_offset is not None,
            "max_chars": max_chars,
            "hint": hint,
            "content": content_out,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _read_file(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_read_lock(root):
        return await _read_file_locked(entity_id, **kwargs)


async def _write_file_locked(entity_id: str, **kwargs: Any) -> str:
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    storage_scope = str(kwargs.get("storage_scope") or "task").strip().lower()
    if storage_scope not in {"task", "workspace"}:
        return json.dumps({"error": "storage_scope must be task or workspace"})
    if storage_scope == "workspace" and not runtime_context.workspace_id:
        return json.dumps({
            "error": "storage_scope=workspace requires an active Workspace context",
        })
    artifact_task_id = None if storage_scope == "workspace" else runtime_context.task_id

    requested_path = str(kwargs.get("path", "") or "")
    path = await _workspace_scoped_new_file_path(
        entity_id=entity_id,
        entity_root=root,
        workspace_id=runtime_context.workspace_id,
        task_id=artifact_task_id,
        path=requested_path,
        expected_sha256=str(kwargs.get("expected_sha256") or ""),
    )
    content = kwargs.get("content", "")
    abs_path = _safe_path(root, path)
    if not abs_path:
        return json.dumps({"error": "Path traversal detected"})

    try:
        stale = await _guard_expected_source_sha(
            abs_path=abs_path,
            path=path,
            expected_sha256=str(kwargs.get("expected_sha256") or ""),
        )
        if stale:
            return stale.error or json.dumps({"error": "source_changed"})

        blocked = await runtime_guard_file_mutation(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name="write_file",
            action=FileMutationAction.WRITE,
            paths=[path],
            approval_token=kwargs.get("approval_token"),
            content_preview=content,
            approval_payload={
                "path": path,
                "content_sha256": _text_sha256(str(content)),
                "expected_sha256": str(kwargs.get("expected_sha256") or ""),
            },
        )
        if blocked:
            return blocked

        ext = os.path.splitext(path)[1].lower()
        persisted_bytes: bytes
        # Auto-generate real binary for office formats from text content
        if ext == ".pptx":
            from packages.core.services.docgen_service import generate_pptx
            # Extract title from first heading or filename
            lines = content.strip().split("\n")
            title = os.path.splitext(os.path.basename(path))[0]
            for line in lines:
                if line.startswith("# ") and not line.startswith("## "):
                    title = line[2:].strip()
                    break
            persisted_bytes = await generate_pptx(title, content)
        elif ext == ".docx":
            from packages.core.services.docgen_service import generate_docx
            persisted_bytes = await generate_docx(
                os.path.splitext(os.path.basename(path))[0], content
            )
        else:
            persisted_bytes = str(content).encode("utf-8")
        _abs_path, sync, written_meta = await _commit_file_projection(
            entity_id=entity_id,
            rel_path=path,
            data=persisted_bytes,
            entity_root=root,
            runtime_context=runtime_context,
            tool_name="write_file",
            allow_empty=True,
            expected_source_version=stale.version,
        )

        return json.dumps({
            "written": True,
            "path": path,
            "storage_scope": storage_scope,
            "size": written_meta["size"],
            "source_sha256": written_meta["source_sha256"],
            "mtime_ns": written_meta["mtime_ns"],
            "knowledge_synced": sync.synced,
            "document_id": sync.document_id,
            "viewer_url": f"/viewer/{sync.document_id}",
            "knowledge_sync_reason": sync.reason,
        })
    except RuntimeFileProjectionError as exc:
        return json.dumps(_projection_failure_payload(exc, result_flag="written"))
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _write_file(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_mutation_lock(root):
        return await _write_file_locked(entity_id, **kwargs)


def _scan_list_files(
    root: str,
    abs_path: str,
    recursive: bool,
) -> Iterator[dict[str, Any]]:
    """Blocking directory listing. Runs in a worker thread — see the note on
    ``_scan_grep_files``."""
    if recursive:
        for full in _iter_tree_file_paths(abs_path):
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not runtime_user_visible_file_path(rel):
                continue
            yield {
                "path": rel,
                "type": "file",
                "size": os.path.getsize(full),
            }
    else:
        with os.scandir(abs_path) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                if not runtime_user_visible_file_path(rel):
                    continue
                yield {
                    "path": rel,
                    "type": "dir" if is_dir else "file",
                    "size": entry.stat(follow_symlinks=False).st_size if is_file else None,
                }


def _iter_tree_file_paths(root: str) -> Iterator[str]:
    """Yield a deterministic depth-first walk, materializing one level at a time."""
    pending_directories = [root]
    while pending_directories:
        directory = pending_directories.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            continue
        child_directories: list[str] = []
        for entry in entries:
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield entry.path
            except OSError:
                continue
        pending_directories.extend(reversed(child_directories))


def _next_scan_batch(iterator: Iterator[Any]) -> list[Any]:
    """Consume one bounded filesystem batch in a worker thread."""
    return list(islice(iterator, FILE_SCAN_ACL_BATCH_SIZE))


async def _list_files_locked(entity_id: str, **kwargs: Any) -> str:
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    rel_path = str(kwargs.get("path", "") or "")
    recursive = bool(kwargs.get("recursive", False))
    limit = _bounded_int(kwargs.get("limit"), LIST_FILES_DEFAULT_LIMIT, LIST_FILES_MAX_LIMIT, 1)
    offset = _bounded_int(kwargs.get("offset"), 0, 100_000, 0)

    abs_path = _safe_path(root, rel_path) if rel_path else root
    if not abs_path:
        return json.dumps({"error": "Path traversal detected"})

    # New workspace writes are transparently scoped under that Workspace's
    # artifact root. Listing must resolve the same logical directory or a
    # write-A/list-B fork forces long-running agents to rediscover the entity
    # root after every context compaction.
    if not os.path.isdir(abs_path) and runtime_context.workspace_id:
        from packages.core.services.generated_media_naming import (
            resolve_workspace_artifact_base_dir,
            scope_workspace_artifact_path,
        )

        workspace_base = await resolve_workspace_artifact_base_dir(
            entity_id=entity_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
        )
        if workspace_base:
            scoped_rel = scope_workspace_artifact_path(
                runtime_normalize_entity_file_path(rel_path) or rel_path,
                workspace_base,
                default_subdir=WorkspaceArtifactDir.DOCUMENTS.value,
            )
            scoped_abs = _safe_path(root, scoped_rel)
            if scoped_abs and os.path.isdir(scoped_abs):
                rel_path = runtime_normalize_entity_file_path(scoped_rel) or scoped_rel
                abs_path = scoped_abs

    if not os.path.isdir(abs_path):
        return json.dumps({"error": f"Directory not found: {rel_path}"})

    resolved_directory = os.path.relpath(abs_path, root).replace(os.sep, "/")
    if (
        resolved_directory != "."
        and not runtime_user_visible_file_path(resolved_directory)
    ):
        return json.dumps({"error": f"Directory not found: {rel_path}"})
    if resolved_directory != "." and await _blocked_doc_paths(
        entity_id,
        [],
        kwargs,
        directory_paths=[resolved_directory],
    ):
        return json.dumps({"error": f"Directory not found: {rel_path}"})

    from packages.core.services.knowledge_visibility import normalize_rel_path

    scanner = _scan_list_files(root, abs_path, recursive)
    entries: list[dict[str, Any]] = []
    visible_total = 0
    has_more = False
    try:
        while batch := await asyncio.to_thread(_next_scan_batch, scanner):
            blocked = await _blocked_doc_paths_batched(
                entity_id,
                [entry["path"] for entry in batch if entry.get("type") == "file"],
                kwargs,
                directory_paths=[
                    entry["path"] for entry in batch if entry.get("type") == "dir"
                ],
            )
            for entry in batch:
                if normalize_rel_path(entry["path"]) in blocked:
                    continue
                visible_index = visible_total
                visible_total += 1
                if visible_index < offset:
                    continue
                if len(entries) < limit:
                    entries.append(entry)
                    continue
                has_more = True
                if recursive:
                    break
            if has_more and recursive:
                break
    except Exception as e:
        return json.dumps({"error": str(e)})

    if not recursive:
        has_more = offset + len(entries) < visible_total

    return json.dumps({
        "count": len(entries),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(entries) if has_more else None,
        "has_more": has_more,
        "total": visible_total if not recursive else None,
        "entries": entries,
    })


async def _list_files(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_read_lock(root):
        return await _list_files_locked(entity_id, **kwargs)


def _scan_glob_files(
    root: str,
    pattern: str,
) -> Iterator[str]:
    """Blocking name-only tree walk. Runs in a worker thread — see the note on
    ``_scan_grep_files``."""
    for full in _iter_tree_file_paths(root):
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        if not runtime_user_visible_file_path(rel):
            continue
        if _glob_path_matches(rel, pattern):
            yield rel


def _glob_path_matches(path: str, pattern: str) -> bool:
    """Match a relative path using standard glob directory semantics.

    ``fnmatch`` treats ``/`` as an ordinary character, so ``**/*.pdf`` does
    not match a root-level ``report.pdf`` and ``**/*/*.pdf`` misses files one
    directory deep. Split the pattern into path segments and handle a segment
    equal to ``**`` as zero or more directories while retaining fnmatch's
    wildcard syntax for ordinary segments.
    """
    path_parts = str(path or "").replace("\\", "/").split("/")
    pattern_parts = str(pattern or "").replace("\\", "/").split("/")
    if not path_parts or not pattern_parts or any(not part for part in path_parts):
        return False

    matched_indexes = {0}
    for segment in pattern_parts:
        if segment == "**":
            # A globstar consumes zero or more path segments. The closure is
            # bounded by the already-materialized relative path.
            matched_indexes = {
                candidate
                for start in matched_indexes
                for candidate in range(start, len(path_parts) + 1)
            }
            continue

        matched_indexes = {
            index + 1
            for index in matched_indexes
            if index < len(path_parts)
            and fnmatch.fnmatch(path_parts[index], segment)
        }
        if not matched_indexes:
            return False

    return len(path_parts) in matched_indexes



async def _glob_files_locked(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    pattern = kwargs.get("pattern", "")
    if not pattern:
        return json.dumps({"error": "pattern is required"})

    limit = _bounded_int(kwargs.get("limit"), GLOB_FILES_DEFAULT_LIMIT, LIST_FILES_MAX_LIMIT, 1)
    offset = _bounded_int(kwargs.get("offset"), 0, 100_000, 0)
    from packages.core.services.knowledge_visibility import normalize_rel_path

    scanner = _scan_glob_files(root, pattern)
    matches: list[str] = []
    visible_total = 0
    has_more = False
    try:
        while batch := await asyncio.to_thread(_next_scan_batch, scanner):
            blocked = await _blocked_doc_paths_batched(entity_id, batch, kwargs)
            for candidate in batch:
                if normalize_rel_path(candidate) in blocked:
                    continue
                visible_index = visible_total
                visible_total += 1
                if visible_index < offset:
                    continue
                if len(matches) < limit:
                    matches.append(candidate)
                    continue
                has_more = True
                break
            if has_more:
                break
    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({
        "pattern": pattern,
        "count": len(matches),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(matches) if has_more else None,
        "has_more": has_more,
        "files": matches,
    })


async def _glob_files(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_read_lock(root):
        return await _glob_files_locked(entity_id, **kwargs)


def _grep_is_binary(full: str, sniff_bytes: int = GREP_BINARY_SNIFF_BYTES) -> bool:
    """Same head-sniff git/ripgrep use: a NUL byte in the first chunk means
    binary. An unreadable file is treated as skip, not as a scan failure."""
    try:
        descriptor = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return True
            return b"\x00" in os.read(descriptor, sniff_bytes)
        finally:
            os.close(descriptor)
    except OSError:
        return True


def _collect_grep_candidate_paths(
    root: str,
    search_root: str,
    file_glob: str,
) -> list[str]:
    """Enumerate grep candidates without opening or decoding file content."""
    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [
            name
            for name in dirnames
            if runtime_user_visible_file_path(
                os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            )
        ]
        dirnames.sort()
        for fn in sorted(filenames):
            if file_glob and not fnmatch.fnmatch(fn, file_glob):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if runtime_user_visible_file_path(rel):
                candidates.append(rel)
    return candidates


def _scan_grep_files(
    root: str,
    search_root: str,
    regex: "re.Pattern[str]",
    file_glob: str,
    max_matches: int,
    offset: int,
    after: str | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> dict:
    """Blocking content scan — MUST NOT run on the event loop (see #424).

    Production incident: this walked a 1.9 GB / 2410-file workspace, opening and
    UTF-8 decoding every file (1.4 GB of it .mp4), and took 8m48s. Measured on
    that same workspace, 99.7% of the bytes were binary and the real text was
    5.2 MB across 1593 files — so the binary skip below removes almost the
    entire cost, and the byte/file/wall-clock bounds are sized generously
    around that real text volume, not around the binary noise.

    ``after`` resumes a budget-truncated scan: it is the exact relative path a
    prior call returned as ``resume_cursor`` (the last file it fully scanned).
    We replay the SAME walk (deterministic: sorted dirnames/filenames) and skip
    every file until we see that exact path again — comparing path strings
    directly, not re-deriving a lexicographic order, since os.walk's traversal
    (all of a directory's own files before any subdirectory) is NOT plain
    lexicographic order over full relative paths. Skipping does no file I/O, so
    replaying up to the cursor is cheap regardless of how far in it is.

    Residual risk, stated plainly rather than papered over: capping bytes fed to
    a single regex.search call (``GREP_MAX_LINE_CHARS``) bounds the quadratic
    blowup this incident's pattern shape could produce (``.*x.*y.*``-style), but
    CPython's ``re`` has no timeout and cannot be interrupted mid-match, so a
    genuinely pathological pattern (nested quantifiers, e.g. ``(a+)+$``) is not
    fully bounded by truncation at any practical cap size. What actually
    contains that case is #424: this now runs in a worker thread, so a hung
    match burns one thread instead of freezing the event loop for every user.
    """
    start = time.monotonic()
    matches: list[dict] = []
    matched_seen = 0
    has_more = False
    files_scanned = 0
    files_skipped_binary = 0
    files_truncated = 0
    bytes_scanned = 0
    truncated = False
    resume_cursor: str | None = None
    # The file the budget check last saw fully handled (scanned OR skipped as
    # binary) — NOT the file the budget trips on. If resume_cursor named the
    # file we were about to look at when the budget ran out, resuming with
    # `after` set to it would skip that exact file via the cursor-replay
    # `continue` below and it would never get scanned at all.
    last_covered_rel: str | None = None
    cursor_found = after is None
    stop = False

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [
            name
            for name in dirnames
            if runtime_user_visible_file_path(
                os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            )
        ]
        dirnames.sort()
        for fn in sorted(filenames):
            if file_glob and not fnmatch.fnmatch(fn, file_glob):
                continue

            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")

            # ACL filtering happens before the first binary sniff/open/stat.
            # Consequently matches, cursors, counters, and truncation state
            # are all derived exclusively from authorized inputs.
            if allowed_paths is not None and rel not in allowed_paths:
                continue

            if not cursor_found:
                if rel == after:
                    cursor_found = True
                continue

            if (
                bytes_scanned >= GREP_SCAN_BYTE_BUDGET
                or (time.monotonic() - start) >= GREP_SCAN_WALL_CLOCK_SECONDS
            ):
                truncated = True
                resume_cursor = last_covered_rel
                stop = True
                break

            if _grep_is_binary(full):
                files_skipped_binary += 1
                last_covered_rel = rel
                continue

            try:
                descriptor = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        last_covered_rel = rel
                        continue
                    raw = os.read(descriptor, GREP_MAX_FILE_READ_BYTES + 1)
                finally:
                    os.close(descriptor)
            except OSError:
                last_covered_rel = rel
                continue

            if len(raw) > GREP_MAX_FILE_READ_BYTES:
                raw = raw[:GREP_MAX_FILE_READ_BYTES]
                files_truncated += 1

            files_scanned += 1
            bytes_scanned += len(raw)
            last_covered_rel = rel
            text = raw.decode("utf-8", errors="replace")

            for line_num, line in enumerate(text.split("\n"), 1):
                line = line[:GREP_MAX_LINE_CHARS]
                if regex.search(line):
                    matched_seen += 1
                    if matched_seen <= offset:
                        continue
                    if len(matches) >= max_matches:
                        has_more = True
                        break
                    matches.append({
                        "file": rel,
                        "line": line_num,
                        "text": line.rstrip()[:500],
                    })
            if has_more:
                break
        if has_more or stop:
            break

    return {
        "matches": matches,
        "has_more": has_more,
        "files_scanned": files_scanned,
        "files_skipped_binary": files_skipped_binary,
        "files_truncated": files_truncated,
        "bytes_scanned": bytes_scanned,
        "truncated": truncated,
        "resume_cursor": resume_cursor,
        "cursor_found": cursor_found,
    }


async def _grep_files_locked(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    pattern_str = kwargs.get("pattern", "")
    if not pattern_str:
        return json.dumps({"error": "pattern is required"})

    rel_path = kwargs.get("path", "")
    file_glob = kwargs.get("file_glob", "")
    max_matches = _bounded_int(kwargs.get("max_matches"), GREP_FILES_DEFAULT_LIMIT, LIST_FILES_MAX_LIMIT, 1)
    offset = _bounded_int(kwargs.get("offset"), 0, 100_000, 0)
    after = str(kwargs.get("after") or "").strip().replace("\\", "/") or None

    search_root = _safe_path(root, rel_path) if rel_path else root
    if not search_root or not os.path.isdir(search_root):
        return json.dumps({"error": f"Directory not found: {rel_path}"})

    search_rel = os.path.relpath(search_root, root).replace(os.sep, "/")
    if search_rel != "." and not runtime_user_visible_file_path(search_rel):
        return json.dumps({"error": f"Directory not found: {rel_path}"})
    if search_rel != "." and await _blocked_doc_paths(
        entity_id,
        [],
        kwargs,
        directory_paths=[search_rel],
    ):
        return json.dumps({"error": f"Directory not found: {rel_path}"})

    try:
        regex = re.compile(pattern_str, re.IGNORECASE)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex: {e}"})

    try:
        candidates = await asyncio.to_thread(
            _collect_grep_candidate_paths,
            root,
            search_root,
            file_glob,
        )
        blocked_paths: set[str] = set()
        for start in range(0, len(candidates), 500):
            blocked_paths.update(
                await _blocked_doc_paths(
                    entity_id,
                    candidates[start:start + 500],
                    kwargs,
                )
            )
        allowed_paths = frozenset(
            candidate for candidate in candidates if candidate not in blocked_paths
        )
        result = await asyncio.to_thread(
            _scan_grep_files,
            root,
            search_root,
            regex,
            file_glob,
            max_matches,
            offset,
            after,
            allowed_paths,
        )
        if after and not result["cursor_found"]:
            # The tree changed since the cursor was issued (file moved/deleted)
            # and replay never found it. Skipping the rest of the tree in that
            # state would silently report "no more matches" when we actually
            # never looked — rescan from the start instead of trusting a stale
            # cursor. Rare: a full rescan is cheap once binaries are skipped.
            result = await asyncio.to_thread(
                _scan_grep_files,
                root,
                search_root,
                regex,
                file_glob,
                max_matches,
                offset,
                None,
                allowed_paths,
            )
    except Exception as e:
        return json.dumps({"error": str(e)})

    matches = result["matches"]
    page_match_count = len(matches)
    has_more = result["has_more"]

    payload: dict[str, Any] = {
        "pattern": pattern_str,
        "count": len(matches),
        "limit": max_matches,
        "offset": offset,
        "next_offset": offset + page_match_count if has_more else None,
        "has_more": has_more,
        "matches": matches,
        "files_scanned": result["files_scanned"],
        "files_skipped_binary": result["files_skipped_binary"],
        "files_truncated": result["files_truncated"],
        "bytes_scanned": result["bytes_scanned"],
        "truncated": result["truncated"],
        "resume_cursor": result["resume_cursor"],
    }
    if result["truncated"]:
        # Keeping this out of GREP_FILES_SCHEMA (a fixed per-conversation token
        # cost, budget-tested) and putting it here instead — it only costs
        # anything on the rare truncated response, not on every conversation.
        payload["hint"] = (
            "Scan ran out of time/budget before covering every file. Retry "
            "with after=resume_cursor to continue; do not report no match yet."
        )
    return json.dumps(payload)


async def _grep_files(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_read_lock(root):
        return await _grep_files_locked(entity_id, **kwargs)


async def _edit_file_locked(entity_id: str, **kwargs: Any) -> str:
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    path = kwargs.get("path", "")
    tool_name = str(kwargs.get("_source_tool_name") or "edit_file")
    if _safe_path(root, path) is None:
        return json.dumps({"error": "Path traversal detected"})
    abs_path, candidates = await _locate_entity_file(
        entity_id=entity_id, entity_root=root, path=path,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
    )
    if not abs_path:
        candidates = await _readable_file_candidates(entity_id, candidates, kwargs)
        return _not_found_result(path, candidates)
    # Address the file we actually located (a workspace-scoped write target),
    # so the sync/knowledge write below updates the SAME projection.
    path = os.path.relpath(abs_path, root).replace("\\", "/")

    resource_blocked = await runtime_guard_file_resource_access(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        conversation_id=runtime_context.conversation_id,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
        runtime_envelope=runtime_context.runtime_envelope,
        tool_name=tool_name,
        action=FileMutationAction.EDIT,
        paths=[path],
    )
    if resource_blocked:
        return resource_blocked

    try:
        ext = os.path.splitext(abs_path)[1].lower()
        operation = str(kwargs.get("operation") or "").strip().lower()
        spreadsheet_operations = {"set_cell", "update_row", "append_row", "add_sheet"}
        if ext in {".xlsx", ".xlsm", ".xls", ".et"}:
            if operation in spreadsheet_operations:
                spreadsheet_kwargs = dict(kwargs)
                spreadsheet_kwargs["path"] = path
                spreadsheet_kwargs["_source_tool_name"] = tool_name
                return await _edit_spreadsheet(entity_id, **spreadsheet_kwargs)
            return json.dumps({
                "error": "unsupported_binary_edit",
                "path": path,
                "hint": (
                    "Spreadsheet files cannot be edited with text replacement. For .xlsx/.xlsm, "
                    "call edit_file with operation set_cell, update_row, append_row, or add_sheet. Convert "
                    "legacy .xls/.et files to .xlsx before editing."
                ),
            })
        if ext in {".docx", ".pptx"}:
            if operation and operation != "replace_text":
                return json.dumps({
                    "error": "unsupported_operation_for_file_type",
                    "path": path,
                    "operation": operation,
                    "hint": "Use operation replace_text or omit operation for Word/PowerPoint text edits.",
                })
            old_text = kwargs.get("old_text", "")
            new_text = kwargs.get("new_text", "")
            replace_all = bool(kwargs.get("replace_all", False))
            if not old_text:
                return json.dumps({"error": "old_text is required"})

            stale = await _guard_expected_source_sha(
                abs_path=abs_path,
                path=path,
                expected_sha256=str(kwargs.get("expected_sha256") or ""),
            )
            if stale:
                return stale.error or json.dumps({"error": "source_changed"})

            blocked = await runtime_guard_file_mutation(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                conversation_id=runtime_context.conversation_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                runtime_envelope=runtime_context.runtime_envelope,
                tool_name=tool_name,
                action=FileMutationAction.EDIT,
                paths=[path],
                approval_token=kwargs.get("approval_token"),
                content_preview={
                    "path": path,
                    "operation": "replace_text",
                    "replace_all": replace_all,
                    "old_text": old_text,
                    "new_text": new_text,
                },
            )
            if blocked:
                return blocked

            replace_sync = _replace_docx_sync if ext == ".docx" else _replace_pptx_sync
            result = await asyncio.to_thread(replace_sync, abs_path, old_text, new_text, replace_all)
            if result.get("error"):
                return json.dumps(result, ensure_ascii=False)
            persisted_bytes = result.pop("_persisted_bytes", None)
            if not isinstance(persisted_bytes, bytes):
                return json.dumps({"error": "edited file bytes were not produced"}, ensure_ascii=False)
            _abs_path, sync, meta = await _commit_file_projection(
                entity_id=entity_id,
                rel_path=path,
                data=persisted_bytes,
                entity_root=root,
                runtime_context=runtime_context,
                tool_name=tool_name,
                allow_empty=False,
                expected_source_version=stale.version,
            )
            result.update({
                "path": path,
                "size": meta["size"],
                "source_sha256": meta["source_sha256"],
                "mtime_ns": meta["mtime_ns"],
                "knowledge_synced": sync.synced,
                "document_id": sync.document_id,
                "knowledge_sync_reason": sync.reason,
            })
            return json.dumps(result, ensure_ascii=False)
        if ext in EXTRACT_ONLY_EXTENSIONS:
            return json.dumps({
                "error": "unsupported_binary_edit",
                "path": path,
                "hint": (
                    "This file type is read through structured extraction and cannot be safely "
                    "edited with text replacement. Export it to Markdown/CSV for text updates, or "
                    "use a file-type-specific edit operation when available."
                ),
            })
        if operation and operation != "replace_text":
            return json.dumps({
                "error": "unsupported_operation_for_file_type",
                "path": path,
                "operation": operation,
                "hint": "Use operation replace_text or omit operation for plain text files.",
            })

        old_text = kwargs.get("old_text", "")
        new_text = kwargs.get("new_text", "")
        replace_all = bool(kwargs.get("replace_all", False))
        if not old_text:
            return json.dumps({"error": "old_text is required"})

        stale = await _guard_expected_source_sha(
            abs_path=abs_path,
            path=path,
            expected_sha256=str(kwargs.get("expected_sha256") or ""),
        )
        if stale:
            return stale.error or json.dumps({"error": "source_changed"})

        blocked = await runtime_guard_file_mutation(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name=tool_name,
            action=FileMutationAction.EDIT,
            paths=[path],
            approval_token=kwargs.get("approval_token"),
            content_preview={
                "path": path,
                "operation": operation or "replace_text",
                "replace_all": replace_all,
                "old_text": old_text,
                "new_text": new_text,
            },
        )
        if blocked:
            return blocked

        content = await _read_supported_text(abs_path)

        if old_text not in content:
            return json.dumps({"error": "old_text not found in file. Ensure exact match including whitespace."})

        if replace_all:
            count = content.count(old_text)
            new_content = content.replace(old_text, new_text)
        else:
            count = 1
            new_content = content.replace(old_text, new_text, 1)

        data = new_content.encode("utf-8")
        _abs_path, sync, meta = await _commit_file_projection(
            entity_id=entity_id,
            rel_path=path,
            data=data,
            entity_root=root,
            runtime_context=runtime_context,
            tool_name=tool_name,
            allow_empty=True,
            expected_source_version=stale.version,
        )
        return json.dumps({
            "edited": True,
            "path": path,
            "replacements": count,
            "source_sha256": _text_sha256(new_content),
            "size": meta["size"],
            "mtime_ns": meta["mtime_ns"],
            "knowledge_synced": sync.synced,
            "document_id": sync.document_id,
            "knowledge_sync_reason": sync.reason,
        })
    except RuntimeFileProjectionError as exc:
        return json.dumps(_projection_failure_payload(exc, result_flag="edited"))
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _edit_spreadsheet(entity_id: str, **kwargs: Any) -> str:
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    path = kwargs.get("path", "")
    tool_name = str(kwargs.get("_source_tool_name") or "edit_spreadsheet")
    abs_path = _safe_path(root, path)
    if not abs_path:
        return json.dumps({"error": "Path traversal detected"})
    resource_blocked = await runtime_guard_file_resource_access(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        conversation_id=runtime_context.conversation_id,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
        runtime_envelope=runtime_context.runtime_envelope,
        tool_name=tool_name,
        action=FileMutationAction.EDIT,
        paths=[path],
    )
    if resource_blocked:
        return resource_blocked
    if not os.path.isfile(abs_path):
        return json.dumps({"error": f"File not found: {path}"})

    try:
        stale = await _guard_expected_source_sha(
            abs_path=abs_path,
            path=path,
            expected_sha256=str(kwargs.get("expected_sha256") or ""),
        )
        if stale:
            return stale.error or json.dumps({"error": "source_changed"})

        preview = {
            "path": path,
            "sheet": kwargs.get("sheet"),
            "new_sheet": kwargs.get("new_sheet"),
            "new_sheet_name": kwargs.get("new_sheet_name"),
            "operation": kwargs.get("operation"),
            "cell": kwargs.get("cell"),
            "match_column": kwargs.get("match_column"),
            "match_value": kwargs.get("match_value"),
            "updates": kwargs.get("updates"),
            "row": kwargs.get("row"),
            "values": kwargs.get("values"),
        }
        blocked = await runtime_guard_file_mutation(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name=tool_name,
            action=FileMutationAction.EDIT,
            paths=[path],
            approval_token=kwargs.get("approval_token"),
            content_preview=preview,
        )
        if blocked:
            return blocked

        result = await asyncio.to_thread(_spreadsheet_edit_sync, abs_path, dict(kwargs))
        if result.get("error"):
            return json.dumps(result, ensure_ascii=False)
        persisted_bytes = result.pop("_persisted_bytes", None)
        if not isinstance(persisted_bytes, bytes):
            return json.dumps({"error": "edited spreadsheet bytes were not produced"}, ensure_ascii=False)
        _abs_path, sync, meta = await _commit_file_projection(
            entity_id=entity_id,
            rel_path=path,
            data=persisted_bytes,
            entity_root=root,
            runtime_context=runtime_context,
            tool_name=tool_name,
            allow_empty=False,
            expected_source_version=stale.version,
        )
        result.update({
            "path": path,
            "size": meta["size"],
            "source_sha256": meta["source_sha256"],
            "mtime_ns": meta["mtime_ns"],
            "knowledge_synced": sync.synced,
            "document_id": sync.document_id,
            "knowledge_sync_reason": sync.reason,
        })
        return json.dumps(result, ensure_ascii=False)
    except RuntimeFileProjectionError as exc:
        return json.dumps(
            _projection_failure_payload(exc, result_flag="edited"),
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _edit_file(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_mutation_lock(root):
        return await _edit_file_locked(entity_id, **kwargs)


_TEXT_PATCH_TYPES = {"txt", "md", "markdown", "json", "html", "htm", "csv", "tsv", "xml", "css", "js", "ts", "tsx", "jsx"}
_OFFICE_PATCH_TYPES = {"docx", "pptx", "xlsx", "xlsm"}
_LEGACY_READ_ONLY_TYPES = {"doc", "wps", "xls", "et", "ppt", "dps", "pdf"}


def _file_engine_capabilities(file_type: str | None = None) -> dict[str, Any]:
    all_types = sorted(_TEXT_PATCH_TYPES | _OFFICE_PATCH_TYPES | _LEGACY_READ_ONLY_TYPES)
    patch_operations = {
        "text": ["replace_text", "text.replace"],
        "docx": ["replace_text", "text.replace"],
        "pptx": ["replace_text", "text.replace"],
        "xlsx": ["set_cell", "cell.set", "update_row", "row.update", "append_row", "row.append", "add_sheet", "sheet.add"],
        "xlsm": ["set_cell", "cell.set", "update_row", "row.update", "append_row", "row.append", "add_sheet", "sheet.add"],
    }
    data = {
        "engine": "file_engine",
        "operations": ["generate", "inspect", "patch"],
        "generate_tool": "generate_file",
        "generate_kinds": GenerateFileKind.values(),
        "patch_tool": "patch_file",
        "read_tools": ["read_file", "search_documents", "list_documents", "rag"],
        "save_boundary": "Knowledge projection via RuntimeFileProjectionTransaction",
        "file_types": all_types,
        "patch_operations": patch_operations,
        "limits": {
            "single_operation_per_call": True,
            "office_patch_scope": (
                "docx/pptx support text replacement while preserving editable OOXML packages; "
                "xlsx/xlsm support cell and row operations. Fine-grained layout/shape ops must "
                "be added as explicit adapter operations before agents can rely on them."
            ),
            "legacy_office": "legacy doc/xls/ppt/wps/et/dps are read/convert-only until converted to OOXML.",
            "pdf": "PDF is read/render/edit-specialist only, not a generic text patch target.",
        },
    }
    ext = str(file_type or "").strip().lower().lstrip(".")
    if not ext:
        return data
    if ext in _TEXT_PATCH_TYPES:
        data["selected"] = {"file_type": ext, "can_generate": True, "can_patch": True, "operations": patch_operations["text"]}
    elif ext in _OFFICE_PATCH_TYPES:
        data["selected"] = {"file_type": ext, "can_generate": ext in {"docx", "pptx", "xlsx"}, "can_patch": True, "operations": patch_operations[ext]}
    elif ext in _LEGACY_READ_ONLY_TYPES:
        data["selected"] = {"file_type": ext, "can_generate": ext == "pdf", "can_patch": False, "operations": []}
    else:
        data["selected"] = {"file_type": ext, "can_generate": True, "can_patch": False, "operations": []}
    return data


async def _inspect_file_engine(entity_id: str = "", **kwargs: Any) -> str:
    del entity_id
    return json.dumps(
        _file_engine_capabilities(kwargs.get("file_type")),
        ensure_ascii=False,
    )


def _normalize_patch_operation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each patch operation must be an object")
    op = str(raw.get("op") or raw.get("operation") or "").strip().lower().replace("-", "_")
    aliases = {
        "text.replace": "replace_text",
        "text_replace": "replace_text",
        "replace": "replace_text",
        "cell.set": "set_cell",
        "cell_update": "set_cell",
        "cell.update": "set_cell",
        "row.update": "update_row",
        "row.append": "append_row",
        "sheet.add": "add_sheet",
        "sheet_add": "add_sheet",
        "worksheet.add": "add_sheet",
        "worksheet_add": "add_sheet",
    }
    operation = aliases.get(op, op)
    if operation not in {"replace_text", "set_cell", "update_row", "append_row", "add_sheet"}:
        raise ValueError(f"Unsupported patch op: {raw.get('op') or raw.get('operation') or '(empty)'}")
    normalized = dict(raw)
    normalized["operation"] = operation
    normalized.pop("op", None)
    return normalized


async def _patch_file_locked(entity_id: str, **kwargs: Any) -> str:
    operations = kwargs.get("operations")
    if not isinstance(operations, list) or not operations:
        return json.dumps({
            "error": "operations must contain one patch operation",
            "capabilities": _file_engine_capabilities(),
        }, ensure_ascii=False)
    if len(operations) != 1:
        return json.dumps({
            "error": "multi_operation_patch_not_supported_yet",
            "hint": "Call patch_file once per operation until batched Office patch transactions are implemented.",
            "capabilities": _file_engine_capabilities(),
        }, ensure_ascii=False)
    try:
        patch = _normalize_patch_operation(operations[0])
    except ValueError as exc:
        return json.dumps({
            "error": str(exc),
            "capabilities": _file_engine_capabilities(),
        }, ensure_ascii=False)
    edit_kwargs = dict(kwargs)
    edit_kwargs.update(patch)
    edit_kwargs["_source_tool_name"] = "patch_file"
    return await _edit_file_locked(entity_id, **edit_kwargs)


async def _patch_file(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_mutation_lock(root):
        return await _patch_file_locked(entity_id, **kwargs)


async def _delete_file_locked(entity_id: str, **kwargs: Any) -> str:
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    path = kwargs.get("path", "")
    abs_path = _safe_path(root, path)
    if not abs_path:
        return json.dumps({"error": "Path traversal detected"})
    if not os.path.isdir(abs_path):
        # Files always use the shared resolver so a workspace-scoped file wins
        # over an Entity-root file with the same logical name. Directories only
        # ever match literally.
        located, candidates = await _locate_entity_file(
            entity_id=entity_id, entity_root=root, path=path,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
        )
        if not located:
            candidates = await _readable_file_candidates(entity_id, candidates, kwargs)
            return _not_found_result(path, candidates)
        abs_path = located

    if not _user_visible_rel_path(root, abs_path):
        return json.dumps({"error": f"File not found: {path}"})

    try:
        rel_for_trash = os.path.relpath(abs_path, root)
        if runtime_normalize_entity_file_path(rel_for_trash) in {"", "."}:
            return json.dumps({"error": "Entity filesystem root cannot be deleted"})
        resource_blocked = await runtime_guard_file_resource_access(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name="delete_file",
            action=FileMutationAction.DELETE,
            paths=[rel_for_trash],
        )
        if resource_blocked:
            return resource_blocked

        expected_sha256 = str(kwargs.get("expected_sha256") or "")
        source_version: EntityFileVersion | None = None
        if expected_sha256:
            if not os.path.isfile(abs_path):
                return json.dumps({
                    "error": "source_not_file",
                    "path": rel_for_trash,
                    "hint": "expected_sha256 can only guard file deletes, not directories.",
                })
            stale = await _guard_expected_source_sha(
                abs_path=abs_path,
                path=rel_for_trash,
                expected_sha256=expected_sha256,
            )
            if stale:
                return stale.error or json.dumps({"error": "source_changed"})
            source_version = stale.version

        blocked = await runtime_guard_file_mutation(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name="delete_file",
            action=FileMutationAction.DELETE,
            paths=[rel_for_trash],
            approval_token=kwargs.get("approval_token"),
            content_preview={"delete": rel_for_trash},
        )
        if blocked:
            return blocked

        async with RuntimeFileProjectionTransactionFactory.create(
            entity_id,
        ) as transaction:
            target_kind = transaction.delete_path(
                rel_for_trash,
                expected_resolved_path=abs_path,
                expected_source_version=source_version,
            )
            await transaction.project_delete(rel_for_trash)
            await transaction.commit()
        return json.dumps({
            "deleted": True,
            "path": path,
            "type": target_kind.value,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _delete_file(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_mutation_lock(root):
        return await _delete_file_locked(entity_id, **kwargs)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (READ_FILE_SCHEMA, _read_file),
        (WRITE_FILE_SCHEMA, _write_file),
        (EDIT_FILE_SCHEMA, _edit_file),
        (INSPECT_FILE_ENGINE_SCHEMA, _inspect_file_engine),
        (PATCH_FILE_SCHEMA, _patch_file),
        (DELETE_FILE_SCHEMA, _delete_file),
        (LIST_FILES_SCHEMA, _list_files),
        (GLOB_FILES_SCHEMA, _glob_files),
        (GREP_FILES_SCHEMA, _grep_files),
    ]
