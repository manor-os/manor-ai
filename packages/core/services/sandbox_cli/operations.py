"""Server side of the in-sandbox ``manor`` CLI.

Every operation runs as the tool call that minted the CLI token
(``SandboxCliClaims``) and goes through the services the agent's own file
tools use: ``list_files``/``grep_files``/``read_file``/``rag`` handlers, the
Knowledge document ACL (``unreadable_document_paths``) and the read guard
(``guard_ai_file_read_access``). Nothing here decides access on its own.

Reads answer immediately. File changes (``mv``/``cp``/``rm``) are only
validated and queued: the bash tool applies them when the command returns,
in the original tool-call context, through the same mutation guard,
approval and Knowledge projection path as every other bash file change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from packages.core.services.sandbox_cli.tokens import (
    SandboxCliClaims,
    SandboxCliTokenError,
    queue_sandbox_cli_operation,
)

# Raw bytes ``manor pull`` may copy into a sandbox in one request.
PULL_MAX_BYTES = 100 * 1024 * 1024
QUEUED_OPERATION_KINDS = frozenset({"mv", "cp", "rm"})
_MAX_OPERATION_PATHS = 50


class SandboxCliOperationError(Exception):
    """A CLI request the server refuses; carries an HTTP status and code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PulledFile:
    path: str
    data: bytes
    sha256: str


def _entity_root(claims: SandboxCliClaims) -> str:
    from packages.core.ai.tools.file_tools import _get_entity_root

    root = _get_entity_root(claims.entity_id)
    if not root or not os.path.isdir(root):
        raise SandboxCliOperationError(
            503, "filesystem_unavailable", "The entity filesystem is not available."
        )
    return root


def _decode(result: str) -> dict[str, Any]:
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return {"result": result}
    if isinstance(payload, dict) and payload.get("error"):
        message = str(payload.get("error"))
        status = 404 if "not found" in message.lower() else 400
        raise SandboxCliOperationError(status, "operation_failed", message)
    return payload if isinstance(payload, dict) else {"result": payload}


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SandboxCliOperationError(400, "invalid_request", f"Expected an integer, got {value!r}.")


def _rel_path(raw: Any, *, allow_root: bool = True) -> str:
    """Entity-relative path from a CLI argument (``/workspace/x`` == ``x``)."""

    from packages.core.services.knowledge_visibility import normalize_rel_path

    text = str(raw or "").strip().replace("\\", "/")
    for prefix in ("/workspace/", "/skill/workspace/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text in {"/workspace", "/skill/workspace"}:
        text = ""
    if text.startswith("/"):
        raise SandboxCliOperationError(
            400,
            "invalid_path",
            f"{raw!r} is outside the entity filesystem; use a path relative to /workspace.",
        )
    if any(part == ".." for part in text.split("/")):
        raise SandboxCliOperationError(400, "invalid_path", f"{raw!r} escapes the entity filesystem.")
    normalized = normalize_rel_path(text) if text.strip("./") else ""
    if not normalized and not allow_root:
        raise SandboxCliOperationError(400, "invalid_path", "A file path is required.")
    return normalized


async def _ensure_directory_readable(claims: SandboxCliClaims, rel_path: str) -> None:
    from packages.core.ai.runtime.file_actions import runtime_user_visible_file_path
    from packages.core.ai.tools.file_tools import _blocked_doc_paths

    if not rel_path:
        return
    if not runtime_user_visible_file_path(rel_path) or await _blocked_doc_paths(
        claims.entity_id, [], claims.tool_context_kwargs(), directory_paths=[rel_path],
    ):
        raise SandboxCliOperationError(404, "not_found", f"Directory not found: {rel_path}")


async def cli_ls(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    """List one directory (or a tree) with the ``list_files`` ACL filter.

    Paths are entity-root relative like the bash working directory, not
    remapped into a Workspace folder.
    """

    from packages.core.ai.tools.file_tools import (
        _blocked_doc_paths_batched,
        _next_scan_batch,
        _paths_outside_workspace,
        _safe_path,
        _scan_list_files,
        _workspace_scope_fields,
    )
    from packages.core.services.knowledge_visibility import normalize_rel_path

    root = _entity_root(claims)
    rel_path = _rel_path(body.get("path"))
    recursive = bool(body.get("recursive"))
    limit = max(1, min(_int(body.get("limit"), 500) or 500, 5000))
    offset = max(0, _int(body.get("offset"), 0) or 0)
    abs_path = _safe_path(root, rel_path) if rel_path else os.path.realpath(root)
    if not abs_path or not os.path.isdir(abs_path):
        if abs_path and os.path.isfile(abs_path):
            return {"path": rel_path, "entries": [await cli_stat(claims, {"path": rel_path})]}
        raise SandboxCliOperationError(404, "not_found", f"Directory not found: {rel_path or '.'}")
    await _ensure_directory_readable(claims, rel_path)

    kwargs = claims.tool_context_kwargs()
    scanner = _scan_list_files(root, abs_path, recursive)
    entries: list[dict[str, Any]] = []
    visible = 0
    excluded = 0
    has_more = False
    while batch := await asyncio.to_thread(_next_scan_batch, scanner):
        files = [entry["path"] for entry in batch if entry.get("type") == "file"]
        directories = [entry["path"] for entry in batch if entry.get("type") == "dir"]
        blocked = await _blocked_doc_paths_batched(
            claims.entity_id,
            files,
            kwargs,
            directory_paths=directories,
        )
        # Listing the entity root names no location, so it stops at the Workspace.
        outside = await _paths_outside_workspace(
            claims.entity_id, files, kwargs, directory_paths=directories,
        ) if not rel_path else set()
        for entry in batch:
            entry_path = normalize_rel_path(entry["path"])
            if entry_path in blocked:
                continue
            if entry_path in outside:
                excluded += 1
                continue
            visible += 1
            if visible <= offset:
                continue
            if len(entries) < limit:
                entries.append(entry)
            else:
                has_more = True
                break
        if has_more:
            break
    return {
        "path": rel_path or ".",
        "recursive": recursive,
        "entries": entries,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(entries) if has_more else None,
        **_workspace_scope_fields(excluded, kwargs),
    }


async def cli_stat(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    from packages.core.ai.runtime.file_actions import runtime_user_visible_file_path
    from packages.core.ai.tools.file_tools import _blocked_doc_paths, _safe_path

    root = _entity_root(claims)
    rel_path = _rel_path(body.get("path"), allow_root=False)
    abs_path = _safe_path(root, rel_path)
    missing = SandboxCliOperationError(404, "not_found", f"Not found: {rel_path}")
    if not abs_path or not os.path.exists(abs_path) or not runtime_user_visible_file_path(rel_path):
        raise missing
    is_dir = os.path.isdir(abs_path)
    blocked = await _blocked_doc_paths(
        claims.entity_id,
        [] if is_dir else [rel_path],
        claims.tool_context_kwargs(),
        directory_paths=[rel_path] if is_dir else None,
    )
    if blocked:
        raise missing
    info = await asyncio.to_thread(os.stat, abs_path)
    result: dict[str, Any] = {
        "path": rel_path,
        "type": "dir" if is_dir else "file",
        "size": None if is_dir else info.st_size,
        "mtime": int(info.st_mtime),
    }
    return result


async def cli_glob(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    from packages.core.ai.tools.file_tools import _glob_files

    pattern = str(body.get("pattern") or "").strip()
    if not pattern:
        raise SandboxCliOperationError(400, "invalid_request", "A glob pattern is required.")
    return _decode(await _glob_files(
        claims.entity_id,
        pattern=pattern,
        limit=_int(body.get("limit")),
        offset=_int(body.get("offset")),
        **claims.tool_context_kwargs(),
    ))


async def cli_grep(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    from packages.core.ai.tools.file_tools import _grep_files

    pattern = str(body.get("pattern") or "")
    if not pattern:
        raise SandboxCliOperationError(400, "invalid_request", "A search pattern is required.")
    return _decode(await _grep_files(
        claims.entity_id,
        pattern=pattern,
        path=_rel_path(body.get("path")),
        file_glob=str(body.get("glob") or ""),
        max_matches=_int(body.get("max_matches")),
        offset=_int(body.get("offset")),
        after=str(body.get("after") or "") or None,
        **claims.tool_context_kwargs(),
    ))


async def cli_read(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    from packages.core.ai.tools.file_tools import _read_file

    params: dict[str, Any] = {"path": _rel_path(body.get("path"), allow_root=False)}
    for name in ("offset", "limit", "char_offset", "max_chars"):
        value = _int(body.get(name))
        if value is not None:
            params[name] = value
    return _decode(await _read_file(claims.entity_id, **params, **claims.tool_context_kwargs()))


async def cli_search(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    from packages.core.ai.tools.rag_tools import _rag

    question = str(body.get("query") or "").strip()
    if not question:
        raise SandboxCliOperationError(400, "invalid_request", "A search question is required.")
    params: dict[str, Any] = {"question": question}
    limit = _int(body.get("limit"))
    if limit is not None:
        params["limit"] = limit
    return _decode(await _rag(
        claims.entity_id,
        user_id=claims.user_id or "",
        **params,
        **claims.tool_context_kwargs(),
    ))


def _read_bounded(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(PULL_MAX_BYTES + 1)


async def cli_pull(claims: SandboxCliClaims, body: dict[str, Any]) -> PulledFile:
    """Raw bytes of one readable file, for ``manor pull``."""

    from packages.core.ai.runtime.file_actions import (
        runtime_entity_filesystem_read_lock,
        runtime_open_entity_file_snapshot,
    )
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access

    root = _entity_root(claims)
    rel_path = _rel_path(body.get("path"), allow_root=False)
    async with runtime_entity_filesystem_read_lock(root):
        blocked = await guard_ai_file_read_access(
            entity_id=claims.entity_id,
            user_id=claims.user_id,
            conversation_id=claims.conversation_id,
            workspace_id=claims.workspace_id,
            tool_name="bash",
            paths=[rel_path],
        )
        if blocked:
            # Never confirm that an unreadable document exists.
            raise SandboxCliOperationError(404, "not_found", f"File not found: {rel_path}")
        try:
            with runtime_open_entity_file_snapshot(claims.entity_id, rel_path) as snapshot:
                if snapshot.stat.st_size > PULL_MAX_BYTES:
                    raise SandboxCliOperationError(
                        413, "file_too_large",
                        f"{rel_path} is larger than {PULL_MAX_BYTES} bytes; it cannot be pulled.",
                    )
                data = await asyncio.to_thread(_read_bounded, snapshot.descriptor_path)
        except SandboxCliOperationError:
            raise
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise SandboxCliOperationError(404, "not_found", f"File not found: {rel_path}")
        except Exception as exc:  # noqa: BLE001 - entity_fs raises its own typed errors
            raise SandboxCliOperationError(404, "not_found", f"File not found: {rel_path} ({exc})")
    if len(data) > PULL_MAX_BYTES:
        raise SandboxCliOperationError(
            413, "file_too_large",
            f"{rel_path} is larger than {PULL_MAX_BYTES} bytes; it cannot be pulled.",
        )
    return PulledFile(path=rel_path, data=data, sha256=hashlib.sha256(data).hexdigest())


async def cli_queue_operation(claims: SandboxCliClaims, body: dict[str, Any]) -> dict[str, Any]:
    """Validate and queue ``mv``/``cp``/``rm`` for the bash tool to apply."""

    from packages.core.ai.runtime.file_actions import runtime_user_visible_file_path

    op = str(body.get("op") or "").strip()
    if op not in QUEUED_OPERATION_KINDS:
        raise SandboxCliOperationError(
            400, "invalid_request",
            f"Unsupported file operation {op!r}; expected one of {sorted(QUEUED_OPERATION_KINDS)}.",
        )
    raw_paths = body.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths or len(raw_paths) > _MAX_OPERATION_PATHS:
        raise SandboxCliOperationError(400, "invalid_request", "paths must be a non-empty list.")
    paths = [_rel_path(path, allow_root=False) for path in raw_paths]
    if op in {"mv", "cp"} and len(paths) < 2:
        raise SandboxCliOperationError(400, "invalid_request", f"{op} needs a source and a destination.")
    for path in paths:
        if not runtime_user_visible_file_path(path):
            raise SandboxCliOperationError(
                403, "invalid_path", f"{path} is not a user-visible entity path."
            )
    root = _entity_root(claims)
    sources = paths[:-1] if op in {"mv", "cp"} else paths
    for source in sources:
        if not os.path.lexists(os.path.join(root, source)):
            raise SandboxCliOperationError(404, "not_found", f"Not found: {source}")
    recursive = bool(body.get("recursive"))
    operation = {"op": op, "paths": paths, "recursive": recursive}
    try:
        index = await queue_sandbox_cli_operation(claims, operation)
    except SandboxCliTokenError as exc:
        raise SandboxCliOperationError(503, "queue_unavailable", str(exc))
    return {
        "queued": True,
        "index": index,
        **operation,
        "note": (
            "Applied when this bash command finishes, with the same permission "
            "checks and approvals as any bash file change."
        ),
    }


def cli_whoami(claims: SandboxCliClaims) -> dict[str, Any]:
    import time

    return {
        "entity_id": claims.entity_id,
        "agent_id": claims.agent_id,
        "conversation_id": claims.conversation_id,
        "workspace_id": claims.workspace_id,
        "task_id": claims.task_id,
        "sandbox_id": claims.sandbox_id,
        "user": bool(claims.user_id),
        "expires_in_seconds": max(0, int(claims.expires_at - time.time())),
    }
