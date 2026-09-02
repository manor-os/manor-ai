"""File operation tools — inspect, read, patch, delete, list, glob, grep.

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
import tempfile
import time
from typing import Any

from packages.core.ai.runtime.file_actions import (
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_read_lock,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_mutation,
    runtime_guard_file_read_access,
    runtime_guard_file_resource_access,
    runtime_normalize_entity_file_path,
    runtime_user_visible_file_path,
)
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler
from packages.core.contracts.audio_generation import GenerateFileKind
from packages.core.contracts.file_engine import (
    FilePatchOperation,
    KNOWN_FILE_TYPES,
    OFFICE_FILE_TYPES,
    OPERATION_GENERATION_TYPES,
    TEMPLATE_OPERATION_GENERATION_TYPES,
    PATCH_OPERATION_ALIASES,
    OfficeChartType,
    PresentationShapePreset,
    TEXT_CONTENT_TYPES,
    file_patch_operations,
    file_type_capability,
    file_type_from_path,
    normalize_file_operation_resources,
    normalize_file_patch_operation as _normalize_patch_operation,
)
from packages.core.services.office_operation_resources import read_office_operation_resources
from packages.core.services.file_engine_patches import (
    append_spreadsheet_row,
    apply_office_structure_patch,
    apply_pdf_patch_sequence,
    apply_text_patch_sequence,
    clear_spreadsheet_merge,
    delete_spreadsheet_conditional_format,
    delete_spreadsheet_chart,
    delete_spreadsheet_picture,
    delete_spreadsheet_sheet,
    delete_spreadsheet_table,
    delete_spreadsheet_validation,
    describe_file_structure,
    format_spreadsheet_chart,
    format_spreadsheet_cell,
    format_spreadsheet_conditional_format,
    format_spreadsheet_picture,
    format_spreadsheet_sheet,
    format_spreadsheet_table,
    format_spreadsheet_validation,
    hydrate_spreadsheet_chart_styles,
    insert_spreadsheet_chart,
    insert_spreadsheet_conditional_format,
    insert_spreadsheet_picture,
    insert_spreadsheet_table,
    insert_spreadsheet_validation,
    replace_office_paragraph_text,
    replace_spreadsheet_picture,
    rename_spreadsheet_package_references,
    rename_spreadsheet_sheet,
    reorder_spreadsheet_sheet,
    set_spreadsheet_merge,
    spreadsheet_cell,
    spreadsheet_cell_value,
    spreadsheet_column_index,
    spreadsheet_header_map,
    spreadsheet_sheet_title,
    update_spreadsheet_chart_data,
    setup_spreadsheet_page,
    word_complex_field_nodes,
)
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
                "include_structure": {
                    "type": "boolean",
                    "description": "DOCX paragraphs, PPTX shape IDs or Excel sheet/table selectors.",
                },
            },
            "required": ["path"],
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
                    "description": "Ordered patches, saved once.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": FilePatchOperation.values() + list(PATCH_OPERATION_ALIASES),
                                "description": "Supported ops: inspect_file_engine.",
                            },
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                            "sheet": {"type": "string"},
                            "new_sheet": {"type": "string"},
                            "new_sheet_name": {"type": "string"},
                            "cell": {"type": "string"},
                            "range": {"type": "string"},
                            "table": {"type": "string"},
                            "table_index": {"type": "integer", "minimum": 0},
                            "section_index": {"type": "integer", "minimum": 0},
                            "story": {"type": "string"},
                            "value": {},
                            "pointer": {"type": "string"},
                            "index": {"type": "integer", "minimum": 0},
                            "rows": {"type": "array"},
                            "text": {"type": "string"},
                            "slide": {"type": "integer", "minimum": 1},
                            "shape_id": {"type": "integer", "minimum": 1},
                            "source": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["path", "expected_sha256"],
                                "additionalProperties": False,
                            },
                            "transform": {"type": "object"},
                            "format": {"type": "object"},
                            "row": {"type": "object"},
                            "match_column": {"type": "string"},
                            "updates": {"type": "object"},
                            "values": {"type": "array"},
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

    `generate_file` scopes new Workspace files under the workspace artifact
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


def _file_meta(abs_path: str, content: str = "") -> dict[str, Any]:
    # content remains accepted for internal callers; versions identify bytes,
    # never extracted text (which omits Office styling and PDF page rotation).
    stat = os.stat(abs_path)
    with open(abs_path, "rb") as source:
        source_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "source_sha256": source_sha256,
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
        written_meta = _file_meta(abs_path)
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
    if ext in {".doc", ".wps", ".pdf", ".xlsx", ".xlsm", ".xls", ".et", ".pptx", ".ppt", ".dps"}:
        from packages.core.services.text_extraction import extract_text
        return await extract_text(abs_path, file_type=ext.lstrip("."))
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _spreadsheet_result_value(value: Any) -> Any:
    """Keep cell-change metadata JSON-safe before committing the workbook."""
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, ArrayFormula):
        return {**dict(value), "text": value.text}
    if isinstance(value, DataTableFormula):
        return dict(value)
    # Dates, times, durations and rich text are represented by their text value.
    return str(value)


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
    from openpyxl.worksheet.worksheet import Worksheet

    if sheet_name is not None:
        if not isinstance(sheet_name, str) or not sheet_name:
            raise ValueError("sheet must be a non-empty name when provided")
        if sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
        else:
            sheet = next((workbook[name] for name in workbook.sheetnames if name.lower() == sheet_name.lower()), None)
        if sheet is None:
            raise ValueError(f"Sheet not found: {sheet_name}")
    else:
        sheet = workbook.active
    if not isinstance(sheet, Worksheet):
        raise ValueError("Target sheet is not a worksheet; specify a worksheet name")
    return sheet


def _iter_docx_container_paragraphs(container: Any):
    from docx.text.paragraph import Paragraph

    seen_cells = set()
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            yield block
        else:
            for row in block.rows:
                for cell in row.cells:
                    # Merged cells expose the same XML at each grid slot.
                    if cell._tc in seen_cells:
                        continue
                    seen_cells.add(cell._tc)
                    yield from _iter_docx_container_paragraphs(cell)


def _iter_docx_paragraphs(doc: Any):
    yield from _iter_docx_container_paragraphs(doc)
    seen_parts = set()
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
            if part is None or part.is_linked_to_previous:
                # Linked parts are visited at their defining section. Reading an
                # undefined part's paragraphs would create a new header/footer.
                continue
            if part.part in seen_parts:
                continue
            seen_parts.add(part.part)
            yield from _iter_docx_container_paragraphs(part)


def _replace_docx_sync(abs_path: str, old_text: str, new_text: str, replace_all: bool) -> dict[str, Any]:
    from docx import Document

    doc = Document(abs_path)
    remaining: int | None = None if replace_all else 1
    replacements = 0
    field_nodes_by_part = {}
    for paragraph in _iter_docx_paragraphs(doc):
        if paragraph.part not in field_nodes_by_part:
            field_nodes_by_part[paragraph.part] = word_complex_field_nodes(paragraph.part.element)
        count, remaining = replace_office_paragraph_text(
            paragraph, old_text, new_text, remaining,
            protected_nodes=field_nodes_by_part[paragraph.part],
        )
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
        count, remaining = replace_office_paragraph_text(paragraph, old_text, new_text, remaining)
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


def _spreadsheet_edit_sync(
    abs_path: str,
    params: dict[str, Any],
    *,
    resources: dict[tuple[str, str], bytes] | None = None,
) -> dict[str, Any]:
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
    wb = load_workbook(abs_path, keep_vba=keep_vba, rich_text=True)
    try:
        hydrate_spreadsheet_chart_styles(abs_path, wb)
        operation = str(params.get("operation") or "").strip().lower()
        if operation not in {"append_row", "table.insert", "table.format", "table.delete"} and any(
            key in params for key in ("table", "inherit_from_row")
        ):
            raise ValueError("table is supported for Excel row.append/table operations; inherit_from_row only for row.append")
        sheet_name = params.get("sheet")
        ws = None if operation == "add_sheet" else _spreadsheet_sheet(wb, sheet_name)
        sheet_rename: tuple[str, str] | None = None
        updated_cells: dict[str, dict[str, Any]] = {}

        if operation == "picture.insert":
            result = insert_spreadsheet_picture(ws, params, resources)

        elif operation == "picture.delete":
            result = delete_spreadsheet_picture(ws, params)

        elif operation == "picture.replace":
            result = replace_spreadsheet_picture(ws, params, resources)

        elif operation == "picture.format":
            result = format_spreadsheet_picture(ws, params)

        elif operation == "chart.insert":
            result = insert_spreadsheet_chart(wb, ws, params)

        elif operation == "chart.delete":
            result = delete_spreadsheet_chart(wb, ws, params)

        elif operation == "chart.data":
            result = update_spreadsheet_chart_data(wb, ws, params)

        elif operation == "chart.format":
            result = format_spreadsheet_chart(ws, params)

        elif operation == "set_cell":
            if "value" not in params:
                return {"error": "value is required for set_cell"}
            cell = spreadsheet_cell(ws, params.get("cell"))
            old_value = _spreadsheet_result_value(cell.value)
            new_value = spreadsheet_cell_value(params["value"])
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
            header_row = params.get("header_row", 1)
            updates = params.get("updates")
            if not isinstance(updates, dict) or not updates:
                return {"error": "updates object is required for update_row"}
            header_map = spreadsheet_header_map(ws, header_row)
            match_col = spreadsheet_column_index(header_map, str(params.get("match_column") or ""))
            if "match_value" not in params:
                return {"error": "match_value is required for update_row"}
            match_value = spreadsheet_cell_value(params["match_value"])
            match_mode = str(params.get("match_mode") or "exact").strip().lower()
            if match_mode not in {"exact", "contains"}:
                return {"error": "match_mode must be exact or contains"}
            case_sensitive = params.get("match_case_sensitive", False)
            if type(case_sensitive) is not bool:
                return {"error": "match_case_sensitive must be boolean"}
            matches: list[int] = []
            for row_idx in range(header_row + 1, ws.max_row + 1):
                existing = ws._cells.get((row_idx, match_col))
                if _spreadsheet_values_match(
                    existing.value if existing is not None else None,
                    match_value,
                    mode=match_mode,
                    case_sensitive=case_sensitive,
                ):
                    matches.append(row_idx)
                    if len(matches) == 50:
                        break
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
                col_idx = spreadsheet_column_index(header_map, str(column_name))
                cell = spreadsheet_cell(ws, f"{get_column_letter(col_idx)}{row_idx}")
                if cell.coordinate in updated_cells:
                    raise ValueError(f"Duplicate update for column: {column_name}")
                old_value = _spreadsheet_result_value(cell.value)
                new_value = spreadsheet_cell_value(raw_value)
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
            result = append_spreadsheet_row(ws, params)
            updated_cells = result["updated_cells"]

        elif operation == "add_sheet":
            if set(params) - {"operation", "sheet", "new_sheet", "new_sheet_name", "index"}:
                raise ValueError("Excel sheet.add accepts a sheet name and optional index")
            new_sheet_name = params.get("new_sheet", params.get("new_sheet_name", params.get("sheet")))
            new_sheet_name = spreadsheet_sheet_title(new_sheet_name)
            if new_sheet_name.lower() in {name.lower() for name in wb.sheetnames}:
                return {"error": "sheet_already_exists", "sheet": new_sheet_name}
            index = params.get("index", len(wb.worksheets))
            if type(index) is not int or not 0 <= index <= len(wb.worksheets):
                raise ValueError(
                    f"index must be an integer between 0 and {len(wb.worksheets)}",
                )
            ws = wb.create_sheet(title=new_sheet_name, index=index)
            result = {
                "updated": True,
                "operation": operation,
                "sheet": ws.title,
                "index": index,
                "sheet_names": list(wb.sheetnames),
                "updated_cells": updated_cells,
            }

        elif operation == "sheet.delete":
            result = delete_spreadsheet_sheet(wb, ws, params)

        elif operation == "sheet.reorder":
            result = reorder_spreadsheet_sheet(wb, ws, params)

        elif operation == "sheet.rename":
            result = rename_spreadsheet_sheet(wb, ws, params)
            sheet_rename = result.pop("_sheet_rename")

        elif operation == "table.insert":
            result = insert_spreadsheet_table(wb, ws, params)

        elif operation == "table.format":
            result = format_spreadsheet_table(ws, params)

        elif operation == "table.delete":
            result = delete_spreadsheet_table(ws, params)

        elif operation == "cell.format":
            result = format_spreadsheet_cell(ws, params)

        elif operation == "sheet.format":
            result = format_spreadsheet_sheet(ws, params)

        elif operation == "merge.set":
            result = set_spreadsheet_merge(ws, params)

        elif operation == "merge.clear":
            result = clear_spreadsheet_merge(ws, params)

        elif operation == "validation.insert":
            result = insert_spreadsheet_validation(ws, params)

        elif operation == "validation.format":
            result = format_spreadsheet_validation(ws, params)

        elif operation == "validation.delete":
            result = delete_spreadsheet_validation(ws, params)

        elif operation == "conditional_format.insert":
            result = insert_spreadsheet_conditional_format(ws, params)

        elif operation == "conditional_format.format":
            result = format_spreadsheet_conditional_format(ws, params)

        elif operation == "conditional_format.delete":
            result = delete_spreadsheet_conditional_format(ws, params)

        elif operation == "page.setup":
            result = setup_spreadsheet_page(ws, params)

        else:
            return {"error": "unsupported_operation_for_file_type"}

        if sheet_rename is not None:
            wb.calculation.fullCalcOnLoad = True
            wb.calculation.forceFullCalc = True
        output = io.BytesIO()
        wb.save(output)
        if updated_cells:
            result["range"] = ", ".join(
                f"{get_column_letter(coordinate_to_tuple(cell)[1])}{coordinate_to_tuple(cell)[0]}"
                for cell in updated_cells
            )
        persisted_bytes = output.getvalue()
        if sheet_rename is not None:
            persisted_bytes = rename_spreadsheet_package_references(
                persisted_bytes, *sheet_rename,
            )
        result["_persisted_bytes"] = persisted_bytes
        return result
    finally:
        wb.close()
        if wb.vba_archive is not None:
            wb.vba_archive.close()


def _with_persisted_bytes(result: dict[str, Any], data: bytes) -> dict[str, Any]:
    updated = dict(result)
    updated["_persisted_bytes"] = data
    return updated


def _apply_office_patch_sequence_sync(
    abs_path: str,
    operations: list[dict[str, Any]],
    *,
    resources: dict[tuple[str, str], bytes] | None = None,
) -> dict[str, Any]:
    ext = os.path.splitext(abs_path)[1].lower()
    fd, temp_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    index = 0
    try:
        with open(abs_path, "rb") as source, open(temp_path, "wb") as target:
            target.write(source.read())

        operation_results: list[dict[str, Any]] = []
        replacements = 0
        for index, operation in enumerate(operations):
            op_name = str(operation.get("operation") or "").strip().lower()
            if op_name not in file_patch_operations(ext.lstrip(".")):
                return {"error": "unsupported_operation_for_file_type", "operation_index": index, "operation": op_name}
            if ext in {".docx", ".pptx"}:
                if op_name != "replace_text":
                    result = apply_office_structure_patch(temp_path, operation, resources=resources)
                else:
                    if not isinstance(operation.get("old_text"), str) or not operation["old_text"]:
                        return {"error": "old_text is required", "operation_index": index}
                    new_text = operation.get("new_text", "")
                    if not isinstance(new_text, str):
                        return {"error": "new_text must be a string", "operation_index": index}
                    replace_sync = _replace_docx_sync if ext == ".docx" else _replace_pptx_sync
                    result = replace_sync(
                        temp_path,
                        operation["old_text"],
                        new_text,
                        bool(operation.get("replace_all", False)),
                    )
            elif ext in {".xlsx", ".xlsm"}:
                result = _spreadsheet_edit_sync(temp_path, operation, resources=resources)
            else:
                return {"error": "unsupported_binary_edit"}

            if result.get("error"):
                result = dict(result)
                result["operation_index"] = index
                return result
            persisted_bytes = result.pop("_persisted_bytes", None)
            if not isinstance(persisted_bytes, bytes):
                return {"error": "patched file bytes were not produced", "operation_index": index}
            with open(temp_path, "wb") as target:
                target.write(persisted_bytes)
            operation_results.append(result)
            replacements += int(result.get("replacements") or 0)

        with open(temp_path, "rb") as target:
            final_bytes = target.read()
        summary: dict[str, Any] = {
            "patched": True,
            "edited": ext in {".docx", ".pptx"},
            "updated": ext in {".xlsx", ".xlsm"},
            "operation": "patch",
            "file_type": ext.lstrip("."),
            "operations_applied": len(operation_results),
            "operation_results": operation_results,
        }
        if replacements:
            summary["replacements"] = replacements
        return _with_persisted_bytes(summary, final_bytes)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return {"error": str(exc), "operation_index": index}
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


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
        with os.fdopen(os.dup(descriptor), "rb") as source:
            source_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
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

    file_type = file_type_from_path(abs_path)
    if file_type not in TEXT_CONTENT_TYPES | OFFICE_FILE_TYPES | {"doc", "wps", "pdf", "xls", "et", "ppt", "dps"}:
        meta = _file_meta(abs_path)
        if kwargs.get("expected_sha256") and kwargs["expected_sha256"] != meta["source_sha256"]:
            return json.dumps({"error": "source_changed", "path": requested_path, **meta})
        return json.dumps({
            "path": requested_path, "resolved_path": resolved_path, **meta,
            "read_mode": "metadata", "capabilities": file_type_capability(file_type),
            "hint": "Binary/unknown content is not decoded as text; use a type-specific reader.",
        })

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
            **({"structure": await asyncio.to_thread(describe_file_structure, abs_path)} if kwargs.get("include_structure") else {}),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _read_file(entity_id: str, **kwargs: Any) -> str:
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG
    async with runtime_entity_filesystem_read_lock(root):
        return await _read_file_locked(entity_id, **kwargs)


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


def _file_engine_capabilities(file_type: str | None = None) -> dict[str, Any]:
    data = {
        "engine": "file_engine",
        "operations": ["generate", "inspect", "patch"],
        "generate_tool": "generate_file",
        "generate_kinds": GenerateFileKind.values(),
        "operation_generation": {
            "file_types": sorted(OPERATION_GENERATION_TYPES),
            "template_file_types": sorted(TEMPLATE_OPERATION_GENERATION_TYPES),
            "operations": "Same ordered operations as patch_file; creates new files only, without content/prompt/options.",
            "template": "Optional {path: Knowledge fs_path, expected_sha256: source_sha256 from read_file}. Uses that exact readable DOCX/PPTX/XLSX/XLSM as input; output must have the same type and a new path. XLSM is template-only so an actual VBA project is never fabricated. Original file/Document stay unchanged. Inspect template structure before choosing operations.",
            "initial_structure": {
                "docx": "No body paragraphs or tables.",
                "pptx": "No slides; default template layout_index 6 is Blank. Textbox geometry requires x/y/width/height in points.",
                "xlsx": "One empty sheet named Sheet.",
            },
        },
        "patch_tool": "patch_file",
        "read_tools": ["read_file", "search_documents", "list_documents", "rag"],
        "save_boundary": "Knowledge projection via RuntimeFileProjectionTransaction",
        "file_types": sorted(KNOWN_FILE_TYPES),
        "patch_operations": {
            ext: file_type_capability(ext)["operations"]
            for ext in ("txt", "json", "diagram.json", "csv", "tsv", "docx", "pptx", "xlsx", "xlsm", "pdf")
        },
        "limits": {
            "multi_operation_per_call": True,
            "source_hash": "SHA-256 of exact file bytes, including Office formatting and PDF page properties.",
            "office_patch_scope": "Only the listed operations are supported; not every Office layout/chart/animation operation.",
            "presentation_shape_presets": PresentationShapePreset.values(),
            "office_chart_types": OfficeChartType.values(),
            "presentation_shapes": "shape.insert: slide + preset + transform{x,y,width,height[,rotation]} in points; optional text/format. Existing top-level and nested group members share the slide-unique slide + shape_id selector for shape/text/paragraph/picture/chart/table edits. Discovery returns group_path, parent_shape_id and coordinate_space; nested transforms use the parent group's local point coordinates. shape.transform also accepts flip_horizontal/flip_vertical booleans. shape.group takes 2–100 unique contiguous sibling shape_ids, preserves their order and returns the new group shape_id; this works at slide or nested-group scope. shape.ungroup bakes group translation/scaling into its members and lifts them back into the parent tree; rotated/flipped groups are rejected rather than changing their appearance. shape.delete removes the selected object; deleting a group's only member is rejected so the caller can explicitly delete the group. picture.delete/chart.delete/table.delete add type safety. Inserts return shape_id.",
            "presentation_slide_duplicate": "slide.duplicate uses a one-based source slide and optional zero-based insertion index (default immediately after the source). It clones the slide XML, background, objects, external links and speaker notes; immutable image/media/theme/layout parts may remain shared, while charts, embedded workbooks, notes and other slide-owned parts are cloned so later edits stay independent. The same operation works during generation, template generation and patching.",
            "presentation_slide_format": "slide.format uses a one-based slide and format containing exactly background_color (six-digit RGB, or null to restore the slide master background) or background_gradient={angle:0–360,stops:[{position:0–1,color:RGB,opacity?:0–1}]}. Gradients require 2–16 ordered stops. The background remains native DrawingML, is reported under read_file structure.slides, and is shared by blank generation, template generation and patching.",
            "presentation_slide_reorder": "slide.reorder moves the selected one-based slide to a zero-based index in the existing deck without recreating it or its relationships. The result reports source_slide, previous_index and final slide/index. The operation is shared by blank generation, template generation and patching.",
            "presentation_shape_format": "format: fill_color/line_color (RGB or null=no fill/line), fill_opacity/line_opacity (0–1, solid color required), line_width (0–72pt), line_dash (DrawingML preset), corner_radius (roundRect only, points <=half shorter side), vertical_alignment (top/middle/bottom), word_wrap boolean, margin_left/right/top/bottom points. gradient_fill={angle:0–360,stops:[{position:0–1,color:RGB,opacity?:0–1}]}: 2–16 ordered stops, exclusive with fill_color/fill_opacity. shadow={color,opacity,blur,distance,angle}, distances in points; null clears outer shadow. Unspecified properties and content are retained; point styles require hundredth-point precision.",
            "presentation_pictures": "picture.insert uses slide + source{path,expected_sha256 from read_file} + transform{x,y,width,height[,rotation]} and optional fit stretch/cover/contain plus format. picture.replace uses slide + picture shape_id + source and preserves geometry/crop/opacity/alt text. picture.format supports crop{left,top,right,bottom} fractions (0–0.95; opposite sums <1), opacity 0–1 and alt_text. picture replace/format/delete accept top-level or grouped pictures through the same slide-unique shape_id and prune unused media relationships. Sources are exact read-authorized Knowledge image snapshots; WebP/AVIF/ICO are embedded as PNG. All operations are shared by generate_file and patch_file.",
            "word_pictures": "The same picture.insert/picture.delete/picture.replace/picture.format operation family supports native Word body, header and footer pictures, including inline and floating anchors. Insert uses optional story + section_index + story-local paragraph index, source, transform{width,height} in points, and format. format supports proportional sizing, alt_text and inline alignment; layout floating adds position_x/position_y, horizontal/vertical reference, none/square/top_bottom wrap, text layering, overlap, table layout and edge distances. Discovery returns a package-wide picture_index, story/shared section indices, layout, dimensions and floating state. Replace/delete use the owning story relationship and preserve shared media still in use. All operations are shared by generation, template generation and patching.",
            "word_text_boxes": "Native Word DrawingML/WPS and legacy VML text-box contents are discovered package-wide across body, header and footer stories. textbox.insert creates an editable floating DrawingML text box from a supported non-line preset, story-local paragraph index, text and transform{x,y,width,height[,rotation]}; shape.transform/shape.format/shape.delete use text_box_index. Formatting supports DrawingML preset, fill/line colors and opacity, line width, text margins, vertical alignment, wrapping, alt text and floating anchor properties; VML supports its native geometry, fill/line/margins/alt text subset. The same text_box_index scopes text.set, paragraph and table/cell operations to content inside the box. Discovery reports group_depth and document or group_local coordinate_space; shape.transform edits explicit DrawingML/VML members in that local space while preserving the outer anchor and siblings. Grouped members support content/member-local formatting and shape.delete removes only the selected member while preserving siblings and cleaning empty groups; shared-frame anchor formatting and ambiguous shared drawings remain rejected. These native operations are shared by generation, template generation and patching without rasterization.",
            "word_paragraph_styles": "paragraph_style.insert creates a native named Word paragraph style with optional base_style, next_style and direct format; omitted base_style defaults to Normal. paragraph_style.format selectively changes an existing custom or built-in paragraph style's base/next/direct format. paragraph_style.delete removes only an unused custom style and rejects built-ins, content use and references from other styles. Direct format uses the same font, spacing, indent, alignment, pagination and line-spacing keys as Word paragraph.format. Discovery returns paragraph_style_definitions with style_id, base/next names, built-in status and direct formatting. All operations are shared by generation, template generation and patching.",
            "presentation_z_order": "PowerPoint shape.reorder moves one top-level or grouped object to zero-based z_index among its current siblings; 0 is the back and sibling_count-1 is the front. It never changes geometry, content, relationships or group membership. Discovery reports z_index and sibling_count for every shape. The operation is shared by generation, template generation and patching.",
            "spreadsheet_pictures": "The same picture.insert/picture.delete/picture.replace/picture.format operation family supports native Excel pictures on XLSX/XLSM sheets. Insert uses sheet, source, an A1 anchor (default A1), optional transform{width,height} in points, and format{alt_text}. Delete uses sheet + zero-based picture_index. Replace preserves native anchor, displayed size, name and alt text. Format moves the anchor, resizes (one dimension preserves displayed aspect for one-cell pictures), or changes alt text. Discovery returns picture_index, anchor type/cells, displayed point size, intrinsic pixels and metadata. All operations are shared by generate_file and patch_file; XLSM macros are preserved.",
            "spreadsheet_sheet_reorder": "Excel sheet.reorder moves an explicit sheet name to a zero-based index while preserving worksheet identity, formulas, drawings, visibility and the active worksheet. Managed hidden chart-data sheets cannot be targeted directly. The operation is shared by blank generation, template generation and patching, including XLSM patching.",
            "spreadsheet_sheet_rename": "Excel sheet.rename requires an explicit sheet and new_sheet. It updates native local worksheet references in formulas, defined names, tables, charts, data validations, conditional formatting and internal hyperlinks while leaving external-workbook references and formula string literals unchanged; calculation is forced on next open. Managed chart-data sheets and invalid/reserved/duplicate names are rejected. The operation is shared by blank generation, template generation and patching, including XLSM patching.",
            "spreadsheet_sheet_add": "Excel sheet.add creates a native worksheet from sheet/new_sheet and accepts optional zero-based index; omitting index appends. It is the same add_sheet canonical operation in generation, template generation and patching.",
            "presentation_charts": "chart.insert creates a native editable PPT chart from chart_type, categories, series{name,values}, transform{x,y,width,height[,rotation]} and optional format. It includes column/bar/line/area/pie/doughnut, marker/line/smooth scatter variants, combo_column_line, stock_hlc, stock_ohlc, stock_vhlc and stock_vohlc; volume stock variants keep volume on the primary axis and prices on a native secondary axis. chart.data replaces categories/series while retaining plot membership and native stock high-low/up-down structure. chart.format changes title/style 1–48, legend, labels, primary/secondary axes, scales/gridlines, series/category colors, and supported indexed trendlines/error bars; stock charts reject vary_colors, trendlines and error bars. chart.delete removes the selected chart and unused native parts. shape.transform moves/resizes/rotates/flips charts. All chart operations are shared by generate_file and patch_file and retain unrelated template parts.",
            "spreadsheet_charts": "The same chart.insert/chart.delete/chart.data/chart.format operations create, delete and edit native XLSX/XLSM charts, including marker/line/smooth scatter variants, combo_column_line and all four stock variants (HLC, OHLC, volume-HLC, volume-OHLC). Volume stock charts use independent native primary-volume and secondary-price axes, including secondary axis title/visibility/scale/gridline formatting. Excel uses sheet + zero-based chart_index, anchor as an A1 cell, and optional width/height in point-based transform or format. Inline categories/series are stored in a very-hidden managed worksheet so the chart remains linked and editable. Discovery reports native chart indices, types, anchors, dimensions, references and bounded data. All operations preserve unrelated workbook parts and XLSM macros.",
            "office_text_set": "DOCX: story paragraph index (body by default; header/footer via story + section_index), or text_box_index plus its local paragraph index; PPTX: slide + shape_id + text-frame paragraph index. Required text string; empty clears, newlines are soft breaks. Retains paragraph properties and first-run/empty typing style. Plain paragraphs only; fields, annotations and embedded content are rejected.",
            "office_paragraph_insert": "DOCX paragraph.insert uses a story-local index, text and optional existing Word style; story defaults to body and header/footer targets use story + section_index, while text_box_index selects content inside a native Word text box. PPTX paragraph.insert uses slide + shape_id + optional zero-based index (default append), text and optional paragraph format; it inherits the nearest existing paragraph's native paragraph and typing style before explicit format overrides. It changes no textbox geometry or other paragraphs.",
            "office_delete": "DOCX paragraph.delete uses a story-local or text-box-local index, rejects body section-break owners and retains the required final header/footer/text-box paragraph; table.delete uses story-local or text-box-local table_index, and shape.delete removes the selected native Word text box by text_box_index, including one explicit DrawingML/VML group member without deleting siblings. PPTX paragraph.delete uses slide + shape_id + index and cannot remove the text frame's only paragraph; slide.delete uses zero-based index. Excel table.delete preserves cells; sheet.delete requires an explicit name, protects the last visible sheet, managed chart-data sheets and detected formula/chart/defined-name references. Every delete is atomic with the surrounding operation batch.",
            "paragraph_format": "Same target as text.set. format: bold, italic, underline, strike, font_size, font_name, font_color (RGB), alignment (left/center/right/justify), space_before/after, indent_left/right, first_line_indent (points), line_spacing (multiple). DOCX also accepts an existing paragraph style and keep_with_next, keep_together, page_break_before, widow_control booleans; PPTX accepts level 0–8. Unspecified properties/text remain unchanged.",
            "page_setup": "format uses points. DOCX requires section_index: width/height, margin_top/bottom/left/right, header_distance/footer_distance. Width/height determine orientation; margins must leave positive content space. PPTX width/height apply to the whole deck (72–4032pt), without resizing shapes. Read current sections/page_size first.",
            "word_sections": "section.insert appends a native Word section at index=section_count with start_type continuous/new_page/even_page/odd_page. Optional format uses the same geometry as page.setup. inherit_headers_footers defaults true; false creates independent empty default/first/even header and footer stories that can immediately be populated through story operations. The operation is shared by generation, template generation and patching.",
            "office_table_cells": "DOCX: story-local table_index + cell, with body as default and optional header/footer story + section_index, or text_box_index plus its local table_index; PPTX: slide + shape_id + cell. A1 is relative to the table grid. Plain text only; merged continuations, fields, nested tables and annotated/embedded content are rejected.",
            "office_cell_format": "Same selectors as cell.set; bold, italic, font_size (1–409pt; DOCX half-points, PPTX hundredth-points), font_name, font_color, fill_color. Only specified properties change; wrap_text/number_format are Excel-only.",
            "office_table_format": "Word/PPT table.format uses the same table selector as table.delete and a non-empty format. Both support partial column_widths {A: points} and row_heights {1: points}. Word also supports style_name (existing table style or null), alignment left/center/right, autofit, width/indent in points, row_height_rules {1: auto/at_least/exact}, and repeat_header_rows. PowerPoint also supports first_row/last_row/first_column/last_column and banded_rows/banded_columns booleans. Discovery reports the effective native layout. The same operation runs in generation, template generation and patching.",
            "spreadsheet_cell_format": "Excel additionally accepts underline/strike/shrink_to_fit/wrap_text booleans, number_format, horizontal (general/left/center/right/fill/justify/centerContinuous/distributed), vertical (top/center/bottom/justify/distributed), indent 0–250, rotation 0–180, borders {left/right/top/bottom: {style: Excel border style, color: RGB} or null to clear}. Unspecified properties remain unchanged.",
            "sheet_format": "Excel sheet.format uses format: column_widths {A: width in Excel character units, 0.1–255}, row_heights {1: points, 0.1–409}, each max 200 entries; freeze_panes (first unfrozen A1 coordinate, null clears); show_gridlines boolean. Grouped widths split without changing other columns. read_file reports bounded layout; grouped column keys such as A:D are descriptive, write widths by individual column.",
            "spreadsheet_merges": "Excel merge.set/merge.clear use sheet + rectangular A1 range. merge.set rejects overlap with another merge or table and refuses to discard content, comments, links or styles from non-anchor cells. merge.clear requires the exact discovered merged range. read_file reports merged_ranges and merge_count. Both operations are native, atomic and shared by generation/patch; XLSM macros are preserved.",
            "spreadsheet_tables": "Excel table.insert creates a native named table over an existing rectangular range whose header cells are non-empty unique strings. It rejects merged/other-table overlap, accepts an optional globally unique table name, and applies a built-in Excel table style (Light1-21, Medium1-28 or Dark1-11; default Medium2 with row stripes). table.format changes only the named table's style and first/last-column or row/column-stripe flags; style_name null clears the table style. table.delete preserves cells, and row.append extends the named table. read_file reports table range, columns, totals and explicit style. The same operations run in generation, template generation and patching, including XLSM macro preservation.",
            "spreadsheet_validations": "Excel validation.insert creates a native data-validation rule over one A1 cell/range; validation.format targets the discovered zero-based validation_index and changes rule fields and optionally its single range; validation.delete removes that rule. Supported types are whole, decimal, list, date, time, textLength and custom, with type-appropriate formula1/formula2/operator, inline list values, blank/dropdown/error/input-message controls. New ranges cannot overlap another data-validation rule. read_file reports bounded selectors, ranges and full supported rule state. The same operations run in generation, template generation and patching and preserve XLSM macros.",
            "spreadsheet_conditional_formats": "Excel conditional_format.insert/format/delete create, partially edit, move and remove native rules by the discovered zero-based conditional_format_index. Supported rule types are cell, formula, two/three-color scale, data bar and all native 3/4/5-icon sets. Cell/formula rules support stop_if_true plus differential font/fill/border styling; visual rules support native thresholds, colors and display options. Multiple rules may target the same range and keep their priority order. Moving or deleting one rule from a shared template range leaves sibling rules and unmodified native properties intact. The same operations run in generation, template generation and patching and preserve XLSM macros.",
            "spreadsheet_page_setup": "Excel page.setup uses sheet and format: orientation portrait/landscape, paper_size A3/A4/A5/letter/legal, margin_left/right/top/bottom and header_distance/footer_distance in points (0–720); fit_width/fit_height in pages (0–32767, zero automatic, switches to fit-to-page); print_area single local A1 range, repeat_rows 1:3, repeat_columns A:C (null clears). No cross-sheet ranges or width/height fields.",
            "legacy_office": "Convert doc/xls/ppt/wps/et/dps to OOXML before patching.",
            "pdf": "Page rotation only; encrypted/signed PDFs and arbitrary PDF text edits are not supported.",
            "media": "Generation uses configured providers. Standalone binary image/audio/video patching is separate; Word/PPT/Excel picture operations edit native package pictures.",
            "text": "UTF-8 only; BOM and line endings are preserved for text.replace.",
        },
    }
    if str(file_type or "").strip():
        data["selected"] = file_type_capability(str(file_type))
    return data

async def _inspect_file_engine(entity_id: str = "", **kwargs: Any) -> str:
    del entity_id
    return json.dumps(
        _file_engine_capabilities(kwargs.get("file_type")),
        ensure_ascii=False,
    )


async def _patch_file_locked(entity_id: str, **kwargs: Any) -> str:
    operations = kwargs.get("operations")
    if not isinstance(operations, list) or not operations:
        return json.dumps({
            "error": "operations must contain at least one patch operation",
            "capabilities": _file_engine_capabilities(),
        }, ensure_ascii=False)
    try:
        patches = [_normalize_patch_operation(operation) for operation in operations]
        patches, operation_resources = normalize_file_operation_resources(
            patches,
            normalize_path=runtime_normalize_entity_file_path,
            visible_path=runtime_user_visible_file_path,
        )
    except ValueError as exc:
        return json.dumps({
            "error": str(exc),
            "capabilities": _file_engine_capabilities(),
        }, ensure_ascii=False)
    runtime_context = _runtime_file_tool_context(kwargs)
    root = _get_entity_root(entity_id)
    if not root:
        return _FS_DISABLED_MSG

    path = kwargs.get("path", "")
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
    path = os.path.relpath(abs_path, root).replace("\\", "/")

    resource_blocked = await runtime_guard_file_resource_access(
        entity_id=entity_id,
        user_id=runtime_context.user_id,
        conversation_id=runtime_context.conversation_id,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
        runtime_envelope=runtime_context.runtime_envelope,
        tool_name="patch_file",
        action=FileMutationAction.EDIT,
        paths=[path],
    )
    if resource_blocked:
        return resource_blocked

    ext = file_type_from_path(abs_path)
    supported = file_patch_operations(ext)
    if not supported:
        return json.dumps({
            "error": "unsupported_binary_edit",
            "path": path,
            "capabilities": file_type_capability(ext),
            "hint": "No native patch operation exists for this format. Do not decode binary files as text.",
        }, ensure_ascii=False)
    for index, patch in enumerate(patches):
        if any(key in patch for key in ("table", "inherit_from_row")) and (
            ext not in {"xlsx", "xlsm"} or patch["operation"] != "append_row"
        ):
            return json.dumps({"error": "table/inherit_from_row are only supported for Excel row.append", "operation_index": index})
        if patch["operation"] not in supported:
            return json.dumps({
                "error": "unsupported_operation_for_file_type",
                "path": path,
                "operation_index": index,
                "operation": patch["operation"],
                "capabilities": file_type_capability(ext),
            }, ensure_ascii=False)

    resource_bytes: dict[tuple[str, str], bytes] = {}
    if operation_resources:
        resource_blocked = await runtime_guard_file_read_access(
            entity_id=entity_id,
            user_id=runtime_context.user_id,
            workspace_id=runtime_context.workspace_id,
            runtime_envelope=runtime_context.runtime_envelope,
            tool_name="patch_file",
            paths=[source["path"] for source in operation_resources],
        )
        if resource_blocked:
            return resource_blocked
        from packages.core.services.entity_fs import EntityFilesystemError, EntityFilesystemStaleWriteError

        try:
            resource_bytes = await asyncio.to_thread(
                read_office_operation_resources, entity_id, operation_resources,
            )
        except EntityFilesystemStaleWriteError:
            return json.dumps({
                "error": "source_changed",
                "paths": [source["path"] for source in operation_resources],
                "hint": "Read the picture source again before patching the Office file.",
            })
        except (EntityFilesystemError, OSError, ValueError) as exc:
            return json.dumps({
                "error": "picture_source_unavailable",
                "detail": str(exc),
                "paths": [source["path"] for source in operation_resources],
            })

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
            tool_name="patch_file",
            action=FileMutationAction.EDIT,
            paths=[path],
            approval_token=kwargs.get("approval_token"),
            content_preview={
                "path": path,
                "operation": "patch",
                "operations": patches,
            },
        )
        if blocked:
            return blocked

        if ext in OFFICE_FILE_TYPES:
            result = await asyncio.to_thread(
                _apply_office_patch_sequence_sync,
                abs_path,
                patches,
                resources=resource_bytes,
            )
            allow_empty = False
        elif ext == "pdf":
            result = await asyncio.to_thread(apply_pdf_patch_sequence, abs_path, patches)
            allow_empty = False
        else:
            result = await asyncio.to_thread(apply_text_patch_sequence, abs_path, patches)
            allow_empty = True

        if result.get("error"):
            return json.dumps(result, ensure_ascii=False)
        persisted_bytes = result.pop("_persisted_bytes", None)
        if not isinstance(persisted_bytes, bytes):
            return json.dumps({"error": "patched file bytes were not produced"}, ensure_ascii=False)
        _abs_path, sync, meta = await _commit_file_projection(
            entity_id=entity_id,
            rel_path=path,
            data=persisted_bytes,
            entity_root=root,
            runtime_context=runtime_context,
            tool_name="patch_file",
            allow_empty=allow_empty,
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
        return json.dumps(_projection_failure_payload(exc, result_flag="patched"), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


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
        (INSPECT_FILE_ENGINE_SCHEMA, _inspect_file_engine),
        (PATCH_FILE_SCHEMA, _patch_file),
        (DELETE_FILE_SCHEMA, _delete_file),
        (LIST_FILES_SCHEMA, _list_files),
        (GLOB_FILES_SCHEMA, _glob_files),
        (GREP_FILES_SCHEMA, _grep_files),
    ]
