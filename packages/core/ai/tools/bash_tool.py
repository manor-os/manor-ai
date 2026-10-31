"""Bash tool — run a shell command in the agent's own sandbox.

Every command runs inside its owner's sandbox -- the user's general sandbox
the generic ``sandbox`` tool uses (same scope and admission, so state
persists across commands and with ``sandbox`` tool calls), shared by all of
the user's conversations, tasks and agents. Nothing runs as a local
subprocess in an API or worker process.

The command runs from the acting agent's working copy of the entity
filesystem, inside that agent's directory of its conversation's (or task's)
work area (``bash_mirror.MirrorLocation``). Before a command runs, the entity
files it names are staged into it after the Knowledge read ACL; after it
runs, the files it created,
changed, moved or deleted there (and the ``manor mv/cp/rm`` operations it
queued) are saved back through ``guard_ai_file_mutation``, the filesystem
ACL, approvals and the Knowledge projection, exactly like every other file
change. A command whose file targets are visible in its text (``mv a b``,
``echo x > f``) is checked before it runs, as before; anything else is
checked when it ends, and an approval request then leaves the working copy
as it was so the same command can be rerun with the approval token.

The command gets a short-lived ``manor`` CLI token for its own identity
(entity, user, agent, conversation, Workspace, task, run, sandbox) through
the runner's per-exec env; it is revoked when the command returns.
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.bash_command_specs import BashCommandSpecFactory
from packages.core.ai.runtime.bash_output_paths import (
    OUTPUT_DIR_ENV,
    BashOutputPathError,
    scope_bash_output_command,
    workspace_output_dir,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler
from packages.core.ai.runtime import bash_mirror
from packages.core.services.sandbox_sdk import capabilities as runner_caps
from packages.core.ai.runtime.bash_mirror import (
    MirrorEntry,
    MirrorError,
    MirrorManifest,
    MirrorSnapshot,
    SandboxMirror,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _bash_description() -> str:
    """Tool description; the ``manor`` command list comes from the CLI itself."""

    from packages.core.services.sandbox_cli.manor_cli import COMMANDS

    # Eager schema: every character is paid on every turn (see the schema
    # budget test); prompt guidance carries the longer explanation.
    return (
        "Shell in your sandbox; cwd is your copy of named entity files, "
        "changes saved back; scratch /tmp. New Workspace files: "
        "$MANOR_OUTPUT_DIR. No-copy: manor "
        + "|".join(spec.name for spec in COMMANDS if spec.in_tool_summary)
    )


BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": _bash_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 120).",
                },
                "approval_token": {
                    "type": "string",
                    "description": "Approval token when changing user-visible files.",
                },
            },
            "required": ["command"],
        },
    },
}

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

# The sandbox is the isolation boundary, so there is no command allowlist.
# These patterns stay blocked because they would wreck the agent's own
# persistent sandbox (and with it every staged file) or never succeed there.
BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*)?r[a-zA-Z]*\s+/(?!\S)"),  # rm -rf / (root)
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r":\(\)\s*\{"),  # fork bomb
    re.compile(r"\bsudo\b"),
]

MAX_OUTPUT = 65536
_PROJECTION_RECOVERY_REL = ".ai/bash-projection-recovery.json"
_SHELL_SEPARATORS = {"&&", "||", ";", "|", "&"}
_MAY_CREATE_FILE_CMDS = {"python3", "node", "npm", "touch", "cp", "mv", "curl", "wget"}
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
# Commands whose arguments are never file paths; nothing is staged for them.
_NO_FILE_ARGUMENT_COMMANDS = frozenset({
    "echo", "printf", "date", "pwd", "which", "whoami", "true", "false", "sleep",
    "export", "env", "manor", "pip", "pip3", "npm", "curl", "wget", "set", "unset",
})
# Commands that only need a name/size/mtime view of the paths they name.
_METADATA_COMMANDS = frozenset({"ls", "find", "tree", "du", "stat", "rm", "rmdir", "mv"})
# Changes saved back by one command.
_MAX_CHANGED_FILES = 500
_SNAPSHOT_MAX_ENTRIES = 20000
_MIRROR_LOCK_POLL_SECONDS = 0.5
_WRITE_REDIRECTION_PATTERN = BashCommandSpecFactory.WRITE_REDIRECTION_PATTERN


def _validate_command(command: str) -> str | None:
    """Return an error message if the command is blocked, else None."""
    stripped = command.strip()
    if not stripped:
        return "Empty command"
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(stripped):
            return "Command blocked by security policy"
    return None


def _stream_output_fields(name: str, value: str) -> dict[str, Any]:
    """Return compact truncation metadata only when output was clipped."""
    if len(value) <= MAX_OUTPUT:
        return {name: value}
    clipped = value[:MAX_OUTPUT]
    return {
        name: clipped,
        f"{name}_truncated": True,
        f"{name}_chars": len(value),
        f"{name}_sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
        f"{name}_hint": (
            "Output was clipped at 65536 chars. Re-run with a narrower command, "
            "redirect full output to a file, or use read_file with offsets."
        ),
    }


def _shell_tokens(command: str) -> list[str]:
    import shlex

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []


def _shell_command_segments(command: str) -> list[list[str]]:
    tokens = _shell_tokens(command)
    if not tokens:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _xargs_target_command(segment: list[str]) -> str | None:
    """Return the command xargs will execute, or None for default echo."""
    idx = 1
    options_with_value = {
        "-a", "--arg-file", "-d", "--delimiter", "-E", "--eof",
        "-I", "--replace", "-L", "--max-lines", "-n", "--max-args",
        "-P", "--max-procs", "-s", "--max-chars",
    }
    while idx < len(segment):
        arg = segment[idx]
        if arg == "--":
            idx += 1
            break
        if not arg.startswith("-"):
            return arg.rsplit("/", 1)[-1]
        if arg in options_with_value and idx + 1 < len(segment):
            idx += 2
            continue
        idx += 1
    if idx < len(segment):
        return segment[idx].rsplit("/", 1)[-1]
    return None


def _has_advanced_move_destination_option(segment: list[str]) -> bool:
    return any(
        token in {
            "-t", "-T", "-S", "--target-directory", "--no-target-directory",
            "--suffix", "--backup",
        }
        or token.startswith(("--target-directory=", "--suffix=", "--backup="))
        or (len(token) > 2 and token.startswith(("-t", "-S")))
        for token in segment[1:]
    )


def _segment_may_create_files(segment: list[str]) -> bool:
    if not segment:
        return False
    base_cmd = segment[0].rsplit("/", 1)[-1]
    if base_cmd in _MAY_CREATE_FILE_CMDS:
        return True
    if base_cmd == "tee":
        return True
    if base_cmd == "xargs":
        return _xargs_target_command(segment) in _MAY_CREATE_FILE_CMDS.union({"rm"})
    return False


def _projection_recovery_path(entity_root: str) -> str:
    return os.path.join(entity_root, _PROJECTION_RECOVERY_REL)


async def _projection_operations(
    *,
    command: str,
    entity_id: str,
    entity_root: str,
    cwd: str,
) -> list[dict[str, Any]]:
    """Build durable move/copy intents before the filesystem is changed."""
    import shlex

    segments = _split_simple_shell_commands(command)
    if len(segments) != 1:
        return []
    try:
        args = shlex.split(segments[0])
    except ValueError:
        return []
    if not args:
        return []
    kind = args[0].rsplit("/", 1)[-1]
    if kind not in {"mv", "cp"}:
        return []
    operands = [arg for arg in args[1:] if not arg.startswith("-")]
    if len(operands) < 2:
        return []

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services.knowledge_visibility import (
        is_user_visible_path,
        normalize_rel_path,
    )

    root = os.path.realpath(entity_root)

    def _resolve(raw_path: str) -> tuple[str, str] | None:
        full_path = os.path.realpath(os.path.join(cwd, raw_path))
        try:
            if os.path.commonpath([root, full_path]) != root:
                return None
        except ValueError:
            return None
        rel_path = normalize_rel_path(os.path.relpath(full_path, root))
        if not rel_path or not is_user_visible_path(rel_path):
            return None
        return rel_path, full_path

    async with async_session() as db:
        projected_paths = set((await db.scalars(
            select(Document.fs_path).where(
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
                Document.fs_path.is_not(None),
            )
        )).all())

    sources = operands[:-1]
    destination = _resolve(operands[-1])
    if destination is None:
        return []
    destination_rel, destination_abs = destination
    destination_is_directory = (
        len(sources) > 1
        or operands[-1].rstrip().endswith(("/", "/."))
        or os.path.isdir(destination_abs)
    )

    operations: list[dict[str, Any]] = []
    for raw_source in sources:
        source = _resolve(raw_source)
        if source is None:
            continue
        source_rel, source_abs = source
        source_name = os.path.basename(os.path.normpath(source_rel))
        target_root = (
            normalize_rel_path(os.path.join(destination_rel, source_name))
            if destination_is_directory
            else destination_rel
        )
        source_is_directory = os.path.isdir(source_abs)

        if kind == "cp" and source_is_directory:
            for dirpath, dirnames, filenames in os.walk(source_abs):
                dirnames[:] = [
                    name
                    for name in dirnames
                    if is_user_visible_path(
                        normalize_rel_path(
                            os.path.relpath(os.path.join(dirpath, name), root)
                        )
                    )
                ]
                for filename in filenames:
                    source_file_abs = os.path.join(dirpath, filename)
                    source_file_rel = normalize_rel_path(os.path.relpath(source_file_abs, root))
                    if not is_user_visible_path(source_file_rel):
                        continue
                    nested = os.path.relpath(source_file_abs, source_abs)
                    target_file_rel = normalize_rel_path(os.path.join(target_root, nested))
                    operations.append({
                        "id": secrets.token_hex(16),
                        "kind": "copy",
                        "source": source_file_rel,
                        "destination": target_file_rel,
                        "source_is_directory": False,
                        "requires_projection": source_file_rel in projected_paths,
                    })
            continue

        source_prefix = source_rel.rstrip("/") + "/"
        operations.append({
            "id": secrets.token_hex(16),
            "kind": "move" if kind == "mv" else "copy",
            "source": source_rel,
            "destination": target_root,
            "source_is_directory": source_is_directory,
            "requires_projection": any(
                path == source_rel or str(path).startswith(source_prefix)
                for path in projected_paths
            ),
        })
    return operations


def _validated_projection_operation(operation: Any) -> dict[str, Any]:
    from packages.core.services.knowledge_visibility import (
        is_user_visible_path,
        normalize_rel_path,
    )

    if not isinstance(operation, dict):
        raise ValueError("invalid Bash projection recovery operation")
    kind = operation.get("kind")
    operation_id = operation.get("id")
    source = str(operation.get("source") or "")
    destination = str(operation.get("destination") or "")
    if kind not in {"move", "copy"} or not isinstance(operation_id, str) or not operation_id:
        raise ValueError("invalid Bash projection recovery operation")
    if (
        normalize_rel_path(source) != source
        or normalize_rel_path(destination) != destination
        or not is_user_visible_path(source)
        or not is_user_visible_path(destination)
    ):
        raise ValueError("invalid Bash projection recovery operation path")
    return {
        "id": operation_id,
        "kind": kind,
        "source": source,
        "destination": destination,
        "source_is_directory": bool(operation.get("source_is_directory")),
        "requires_projection": bool(operation.get("requires_projection")),
    }


async def _projection_exists(entity_id: str, rel_path: str, *, is_directory: bool) -> bool:
    from sqlalchemy import or_, select

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services.knowledge_sync import find_folder_path

    prefix = rel_path.rstrip("/") + "/"
    path_predicates = [Document.fs_path == rel_path]
    if is_directory:
        path_predicates.append(Document.fs_path.like(prefix + "%"))
    async with async_session() as db:
        document_exists = await db.scalar(
            select(Document.id).where(
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
                or_(*path_predicates),
            ).limit(1)
        )
    if document_exists:
        return True
    return bool(is_directory and await find_folder_path(entity_id, rel_path))


async def _apply_projection_operations(
    *,
    entity_id: str,
    entity_root: str,
    operations: list[dict[str, Any]],
) -> int:
    """Replay typed projection operations idempotently from filesystem state."""
    from packages.core.services.knowledge_sync import copy_file_projection, move_path

    root = os.path.realpath(entity_root)
    applied = 0
    for raw_operation in operations:
        operation = _validated_projection_operation(raw_operation)
        if operation["source"] == operation["destination"]:
            continue
        source_abs = os.path.realpath(os.path.join(root, operation["source"]))
        destination_abs = os.path.realpath(os.path.join(root, operation["destination"]))
        for full_path in (source_abs, destination_abs):
            if os.path.commonpath([root, full_path]) != root:
                raise ValueError("projection recovery operation escapes the entity root")

        if operation["kind"] == "move":
            # If the source still exists, the filesystem move did not complete.
            # Generic path repair below will make any partial destination safe.
            if os.path.exists(source_abs):
                continue
            if not os.path.exists(destination_abs):
                if operation["requires_projection"]:
                    raise RuntimeError("moved destination is missing during projection recovery")
                continue
            changed = await move_path(
                entity_id,
                operation["source"],
                operation["destination"],
            )
            if not changed and operation["requires_projection"]:
                at_destination = await _projection_exists(
                    entity_id,
                    operation["destination"],
                    is_directory=operation["source_is_directory"],
                )
                at_source = await _projection_exists(
                    entity_id,
                    operation["source"],
                    is_directory=operation["source_is_directory"],
                )
                if not at_destination or at_source:
                    raise RuntimeError("Knowledge move projection could not be replayed")
            applied += int(bool(changed))
            continue

        if not os.path.isfile(destination_abs):
            if operation["requires_projection"] and os.path.exists(source_abs):
                continue
            continue
        changed = await copy_file_projection(
            entity_id,
            operation["source"],
            operation["destination"],
            operation_id=operation["id"],
        )
        if not changed and operation["requires_projection"]:
            raise RuntimeError("Knowledge copy projection could not be replayed")
        applied += int(bool(changed))
    return applied


def _write_projection_recovery_marker(
    entity_root: str,
    *,
    entity_id: str,
    user_id: str | None,
    paths: list[str],
    operations: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Persist an auditable retry plan without retaining the shell command."""
    marker_path = _projection_recovery_path(entity_root)
    marker_dir = os.path.dirname(marker_path)
    os.makedirs(marker_dir, exist_ok=True)
    temp_path = f"{marker_path}.tmp-{os.getpid()}"
    payload = {
        "version": 2,
        "entity_id": entity_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "paths": paths,
        "operations": operations or [],
    }
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, marker_path)


def _read_projection_recovery_marker(entity_root: str) -> dict[str, Any] | None:
    marker_path = _projection_recovery_path(entity_root)
    if not os.path.isfile(marker_path):
        return None
    with open(marker_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("version") not in {1, 2} or not isinstance(payload.get("paths"), list):
        raise ValueError("invalid Bash projection recovery marker")
    if payload.get("version") == 1:
        payload["operations"] = []
    if not isinstance(payload.get("operations"), list):
        raise ValueError("invalid Bash projection recovery marker")
    payload["operations"] = [
        _validated_projection_operation(operation)
        for operation in payload["operations"]
    ]
    return payload


async def _repair_projection_paths(
    *,
    entity_id: str,
    entity_root: str,
    paths: list[str],
    user_id: str | None,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> int:
    """Make the Knowledge projection match a bounded set of filesystem paths.

    ``conversation_id``/``task_id`` identify the producing run, so an
    ordinary Chat's new files are filed in its ``Chat sessions`` folder.
    """
    from packages.core.services.knowledge_sync import (
        ensure_folder_path,
        sync_file_to_knowledge,
        trash_path,
    )
    from packages.core.services.knowledge_visibility import (
        is_user_visible_path,
        normalize_rel_path,
    )

    root = os.path.realpath(entity_root)
    synced = 0
    for rel_path in paths:
        normalized_rel = normalize_rel_path(rel_path)
        if (
            not normalized_rel
            or normalized_rel != rel_path
            or not is_user_visible_path(normalized_rel)
        ):
            raise ValueError("invalid Bash projection recovery path")
        full_path = os.path.realpath(os.path.join(root, rel_path))
        if os.path.commonpath([root, full_path]) != root:
            raise ValueError("projection recovery path escapes the entity root")
        if os.path.isfile(full_path):
            result = await sync_file_to_knowledge(
                entity_id=entity_id,
                abs_path=full_path,
                entity_root=root,
                source="bash",
                created_by=user_id or "ai-agent",
                user_id=user_id,
                conversation_id=conversation_id,
                task_id=task_id,
                force=True,
            )
            if not result.synced:
                raise RuntimeError(result.reason or "Knowledge file projection failed")
            synced += 1
            continue
        if os.path.isdir(full_path):
            await ensure_folder_path(entity_id, rel_path, owner_id=user_id)
            for dirpath, dirnames, filenames in os.walk(full_path):
                dirnames[:] = [
                    name
                    for name in dirnames
                    if is_user_visible_path(os.path.relpath(os.path.join(dirpath, name), root))
                ]
                for filename in filenames:
                    nested_path = os.path.join(dirpath, filename)
                    nested_rel = os.path.relpath(nested_path, root)
                    if not is_user_visible_path(nested_rel):
                        continue
                    result = await sync_file_to_knowledge(
                        entity_id=entity_id,
                        abs_path=nested_path,
                        entity_root=root,
                        source="bash",
                        created_by=user_id or "ai-agent",
                        user_id=user_id,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        force=True,
                    )
                    if not result.synced:
                        raise RuntimeError(
                            result.reason or "Knowledge directory projection failed"
                        )
                    synced += 1
            continue
        await trash_path(entity_id, rel_path, is_directory=None)
    return synced


async def _repair_pending_projection(entity_id: str, entity_root: str) -> int:
    marker = _read_projection_recovery_marker(entity_root)
    if marker is None:
        return 0
    if marker.get("entity_id") != entity_id:
        raise ValueError("Bash projection recovery marker belongs to another entity")
    applied = await _apply_projection_operations(
        entity_id=entity_id,
        entity_root=entity_root,
        operations=marker["operations"],
    )
    synced = await _repair_projection_paths(
        entity_id=entity_id,
        entity_root=entity_root,
        paths=[str(path) for path in marker["paths"]],
        user_id=str(marker.get("user_id") or "") or None,
        conversation_id=str(marker.get("conversation_id") or "") or None,
        task_id=str(marker.get("task_id") or "") or None,
    )
    os.remove(_projection_recovery_path(entity_root))
    return applied + synced


async def _repair_pending_projection_for_bash(
    entity_id: str,
    entity_root: str,
) -> str | None:
    """Finish an already-started projection repair before surfacing cancellation."""
    from packages.core.services.entity_fs import finish_entity_filesystem_mutation

    try:
        await finish_entity_filesystem_mutation(
            _repair_pending_projection(entity_id, entity_root),
        )
    except Exception as exc:
        logger.error("Bash projection recovery remains pending", exc_info=True)
        return json.dumps({
            "error": "knowledge_sync_pending",
            "reason": str(exc),
            "exit_code": 1,
            "command_executed": False,
        })
    return None


async def _workspace_bash_artifact_base(
    *,
    entity_id: str,
    workspace_id: str | None,
    task_id: str | None,
    workflow_run_id: str | None,
    workflow_lineage_root_run_id: str | None,
    entity_root: str,
) -> str:
    """Read the already-provisioned Workspace output root without creating it."""
    normalized_workspace_id = str(workspace_id or "").strip()
    if not normalized_workspace_id:
        return ""

    from packages.core.ai.runtime.authorization_receipts import (
        runtime_current_tool_authorization_receipt,
    )

    receipt = runtime_current_tool_authorization_receipt()
    if (
        receipt is not None
        and receipt.entity_id == entity_id
        and receipt.workspace_id == normalized_workspace_id
        and receipt.workspace_artifact_base_dir
    ):
        return receipt.workspace_artifact_base_dir

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace
    from packages.core.services.generated_media_naming import (
        workspace_execution_artifact_dir,
    )
    from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

    async with async_session() as db:
        artifact_folder_id = (await db.execute(
            select(Workspace.artifact_folder_id).where(
                Workspace.id == normalized_workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
    if not artifact_folder_id:
        raise ValueError("Workspace artifact directory is unavailable")
    return workspace_execution_artifact_dir(
        workspace_artifact_storage_base(str(artifact_folder_id)),
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        workflow_lineage_root_run_id=workflow_lineage_root_run_id,
        entity_root=entity_root,
    )


def _split_simple_shell_commands(command: str) -> list[str]:
    """Split simple `cmd && cmd` / `cmd; cmd` chains for post-run fs sync."""
    import shlex

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []

    if not tokens:
        return []

    separators = {"&&", ";"}
    unsupported = {"|", "||", "&", ">", ">>", "<", "<<", "(", ")"}
    segments: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in unsupported or token.startswith("|"):
            return []
        if token in separators:
            if current:
                segments.append(shlex.join(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.append(shlex.join(current))
    return segments


def _may_create_files(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    if BashCommandSpecFactory.has_write_redirection(stripped):
        return True
    segments = _shell_command_segments(stripped)
    if segments:
        return any(_segment_may_create_files(segment) for segment in segments)
    base_cmd = stripped.split()[0].rsplit("/", 1)[-1]
    if base_cmd in _MAY_CREATE_FILE_CMDS:
        return True
    return any(token in stripped for token in (">", "tee "))


def _segment_may_mutate(segment: list[str]) -> bool:
    if not segment:
        return False

    base_cmd = segment[0].rsplit("/", 1)[-1]
    if _segment_may_create_files(segment):
        return True
    if base_cmd in {"rm", "mv", "cp", "mkdir", "touch", "chmod", "tee"}:
        return True
    if base_cmd == "sed":
        return any(arg.startswith("-") and "i" in arg[1:] for arg in segment[1:])
    if base_cmd == "find":
        return "-delete" in segment or any(
            token == "-exec"
            and idx + 1 < len(segment)
            and segment[idx + 1].rsplit("/", 1)[-1] == "rm"
            for idx, token in enumerate(segment)
        )
    if base_cmd == "xargs":
        return _xargs_target_command(segment) == "rm"
    return False


def _contains_shell_mutation(command: str) -> bool:
    if _WRITE_REDIRECTION_PATTERN.search(command):
        return True

    tokens = _shell_tokens(command)
    if not tokens:
        return any(
            token in command
            for token in (">", "tee ", "rm ", "mv ", "cp ", "mkdir ", "touch ", "chmod ", "sed -i", " -delete")
        )

    segment: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if _segment_may_mutate(segment):
                return True
            segment = []
            continue
        segment.append(token)
    return _segment_may_mutate(segment)


def _visible_mutation_paths(command: str) -> list[str]:
    """Best-effort detection of user-visible paths a local bash command may mutate."""
    stripped = command.strip()
    if not stripped or len(_shell_command_segments(stripped)) > 1:
        # Chained/piped commands are hard to audit precisely; require approval
        # against the visible filesystem root if they include mutation syntax.
        if stripped and _contains_shell_mutation(stripped):
            return ["."]
        return []

    import shlex
    try:
        args = shlex.split(stripped)
    except ValueError:
        return ["."]
    if not args:
        return []

    base_cmd = args[0].rsplit("/", 1)[-1]
    paths: list[str] = []

    def _non_flags(items: list[str]) -> list[str]:
        return [item for item in items if item and not item.startswith("-")]

    if base_cmd in {"rm", "mkdir", "touch", "chmod"}:
        paths.extend(_non_flags(args[1:]))
    elif base_cmd in {"mv", "cp"}:
        if _has_advanced_move_destination_option(args):
            # Structured projection supports only the ordinary operand form.
            # Fail closed even if an internal caller bypasses validation.
            return ["."]
        clean = _non_flags(args[1:])
        # A move changes its sources; a copy only writes its destination.
        paths.extend(clean if base_cmd == "mv" else clean[-1:] if len(clean) >= 2 else clean)
    elif base_cmd == "find" and ("-delete" in args or re.search(r"-exec\s+rm\b", stripped)):
        return ["."]
    elif base_cmd == "xargs" and _segment_may_mutate(args):
        return ["."]
    elif base_cmd == "sed" and any(a.startswith("-i") for a in args[1:]):
        paths.extend(_non_flags(args[1:])[-1:])

    # Redirection writes: echo foo > path, cat > path, etc.
    for op in (">>", ">"):
        if op in args:
            idx = args.index(op)
            if idx + 1 < len(args):
                paths.append(args[idx + 1])
    # Shell also accepts redirection without spaces: echo hi>docs/out.txt.
    for match in _WRITE_REDIRECTION_PATTERN.finditer(stripped):
        paths.append(match.group(1).strip("'\""))

    # tee writes to each non-flag argument after tee.
    if base_cmd == "tee":
        paths.extend(_non_flags(args[1:]))

    # Only user-visible paths need approval; the guard does final filtering.
    return list(dict.fromkeys(paths))


def _bash_mutation_action(command: str) -> FileMutationAction:
    segments = _shell_command_segments(command)
    for segment in segments:
        if not segment:
            continue
        base_cmd = segment[0].rsplit("/", 1)[-1]
        if base_cmd == "rm" or (
            base_cmd == "find"
            and ("-delete" in segment or "rm" in segment)
        ):
            return FileMutationAction.DELETE
    if any(
        segment and segment[0].rsplit("/", 1)[-1] in {"mv", "chmod", "sed"}
        for segment in segments
    ):
        return FileMutationAction.EDIT
    return FileMutationAction.WRITE


def _bash_move_destination_collisions(command: str, cwd: str) -> list[str]:
    """Return exact destinations that a simple ``mv`` would overwrite."""
    collisions: list[str] = []
    for segment in _shell_command_segments(command):
        if not segment or segment[0].rsplit("/", 1)[-1] != "mv":
            continue

        operands: list[str] = []
        target_directory: str | None = None
        no_target_directory = False
        parse_options = True
        idx = 1
        while idx < len(segment):
            arg = segment[idx]
            if parse_options and arg == "--":
                parse_options = False
                idx += 1
                continue
            if parse_options and arg in {"-t", "--target-directory"}:
                if idx + 1 >= len(segment):
                    break
                target_directory = segment[idx + 1]
                idx += 2
                continue
            if parse_options and arg.startswith("--target-directory="):
                target_directory = arg.split("=", 1)[1]
                idx += 1
                continue
            if parse_options and arg.startswith("-t") and len(arg) > 2:
                target_directory = arg[2:]
                idx += 1
                continue
            if parse_options and arg in {"-S", "--suffix"}:
                idx += 2
                continue
            if parse_options and (
                arg.startswith("--suffix=")
                or (arg.startswith("-S") and len(arg) > 2)
            ):
                idx += 1
                continue
            if parse_options and arg == "--no-target-directory":
                no_target_directory = True
                idx += 1
                continue
            if parse_options and arg.startswith("-"):
                no_target_directory = no_target_directory or "T" in arg[1:]
                idx += 1
                continue
            operands.append(arg)
            idx += 1

        if target_directory is not None:
            sources = operands
            destination = target_directory
            destination_is_directory = True
        elif len(operands) >= 2:
            sources = operands[:-1]
            destination = operands[-1]
            destination_is_directory = (
                not no_target_directory
                and os.path.isdir(os.path.join(cwd, destination))
            )
        else:
            continue

        if destination_is_directory:
            candidates = [
                os.path.join(destination, os.path.basename(os.path.normpath(source)))
                for source in sources
            ]
        else:
            candidates = [destination]
        for candidate in candidates:
            if os.path.lexists(os.path.join(cwd, candidate)):
                display_path = os.path.relpath(
                    os.path.abspath(os.path.join(cwd, candidate)),
                    os.path.abspath(cwd),
                ).replace(os.sep, "/")
                collisions.append(display_path)
    return list(dict.fromkeys(collisions))


async def _bash_resource_mutation_error(
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None = None,
    cwd: str,
    command: str,
    paths: list[str],
) -> str | None:
    """Apply the same Document/folder ACLs as the filesystem API."""
    if any(
        glob.has_magic(path)
        or path.startswith("~")
        or "{" in path
        or "}" in path
        for path in paths
    ):
        return json.dumps({
            "error": "file_permission_denied",
            "reason": (
                "Filesystem mutations must name exact paths; globs and shell "
                "expansion cannot be authorized safely"
            ),
            "operation": {
                "tool": "bash",
                "action": _bash_mutation_action(command),
                "paths": paths,
            },
        })
    if any(path in {".", "./", "/"} for path in paths):
        return json.dumps({
            "error": "file_permission_denied",
            "reason": (
                "Broad filesystem mutations cannot be projected safely; "
                "use explicit file or directory paths"
            ),
            "operation": {
                "tool": "bash",
                "action": _bash_mutation_action(command),
                "paths": paths,
            },
        })

    from packages.core.services.knowledge_visibility import (
        is_user_visible_path,
        normalize_rel_path,
    )

    entity_root = os.path.realpath(cwd)
    try:
        for raw_path in paths:
            full_path = os.path.realpath(os.path.join(cwd, raw_path))
            if os.path.commonpath([entity_root, full_path]) != entity_root:
                raise ValueError("Filesystem mutation path escapes the entity root")
            rel_path = normalize_rel_path(os.path.relpath(full_path, entity_root))
            if not is_user_visible_path(rel_path):
                raise ValueError("Hidden system paths cannot be changed through Bash")
    except (ValueError, OSError) as exc:
        return json.dumps({
            "error": "file_permission_denied",
            "reason": str(exc),
            "operation": {
                "tool": "bash",
                "action": _bash_mutation_action(command),
                "paths": paths,
            },
        })
    overwrite_paths = _bash_move_destination_collisions(command, cwd)
    if overwrite_paths:
        return json.dumps({
            "error": "file_destination_exists",
            "reason": (
                "Move destination already exists; delete it explicitly before replacing it"
            ),
            "operation": {
                "tool": "bash",
                "action": "edit",
                "paths": overwrite_paths,
            },
        })
    if not user_id:
        return None

    from types import SimpleNamespace

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.user import User
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.filesystem_access import (
        FilesystemAccessDenied,
        require_path_mutation_access,
        require_path_write_access,
    )
    async with async_session() as db:
        user = await db.scalar(
            select(User).where(
                User.id == user_id,
                User.status == "active",
                User.deleted_at.is_(None),
            )
        )
        if user is None:
            return json.dumps({"error": "Active user context is required for file changes"})
        role = await resolve_effective_user_role_name(
            db,
            user_id=user.id,
            entity_id=entity_id,
            legacy_role=user.role if user.entity_id == entity_id else None,
        )
        if not role:
            return json.dumps({"error": "Active entity membership is required for file changes"})
        from packages.core.services.runtime_authorization.collaborator import is_runtime_collaborator

        if await is_runtime_collaborator(user.id, entity_id, db=db):
            # Bash works in the whole company filesystem, outside the Workspace
            # an external collaborator is limited to.
            return json.dumps({"error": "External collaborators cannot change company files through Bash"})
        actor = SimpleNamespace(
            id=user.id,
            entity_id=entity_id,
            role=role,
            email=user.email,
            display_name=user.display_name,
        )
        mutation_action = _bash_mutation_action(command)
        try:
            for raw_path in paths:
                full_path = os.path.realpath(os.path.join(cwd, raw_path))
                if os.path.commonpath([entity_root, full_path]) != entity_root:
                    raise FilesystemAccessDenied(403, "Filesystem path escapes the entity root")
                rel_path = normalize_rel_path(os.path.relpath(full_path, entity_root))
                if not is_user_visible_path(rel_path):
                    raise FilesystemAccessDenied(
                        403,
                        "Hidden system paths cannot be changed through Bash",
                    )
                if os.path.exists(full_path):
                    if mutation_action == "delete":
                        await require_path_mutation_access(
                            db,
                            user=actor,
                            rel_path=rel_path,
                            full_path=full_path,
                            entity_root=entity_root,
                            action="delete",
                            workspace_id=workspace_id,
                        )
                    elif os.path.isdir(full_path):
                        await require_path_mutation_access(
                            db,
                            user=actor,
                            rel_path=rel_path,
                            full_path=full_path,
                            entity_root=entity_root,
                            action="move",
                            workspace_id=workspace_id,
                        )
                    else:
                        await require_path_write_access(
                            db,
                            user=actor,
                            rel_path=rel_path,
                            exists=True,
                            workspace_id=workspace_id,
                        )
                else:
                    await require_path_write_access(
                        db,
                        user=actor,
                        rel_path=rel_path,
                        exists=False,
                        workspace_id=workspace_id,
                    )
        except FilesystemAccessDenied as exc:
            return json.dumps({
                "error": "file_permission_denied",
                "reason": exc.detail,
                "operation": {
                    "tool": "bash",
                    "action": mutation_action,
                    "paths": paths,
                },
            })
    return None



# ---------------------------------------------------------------------------
# Sandbox layout and admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BashSandboxLayout:
    """Where one acting agent's command runs inside its owner's shared sandbox.

    The working copy lives in the agent's own directory of its conversation's
    (or task's) work area (``runtime_sandbox_layout``): commands of the
    user's other conversations and agents run in the same container at the
    same time, each on its own copy, so a command's file changes are synced
    back from -- and attributed to -- that copy alone. The agent's scratch
    home is a dot-directory beside it (never saved back). The command's cwd
    is the entity root of the working copy so relative entity paths
    (``cat Knowledge/a.md``, ``echo x > report.md``) keep their old meaning.
    """

    location: bash_mirror.MirrorLocation
    work_area_key: str

    @property
    def mirror_root(self) -> str:
        return self.location.root

    @property
    def agent_home(self) -> str:
        return self.location.home

    @property
    def cwd(self) -> str:
        return self.location.root

    @classmethod
    def for_context(cls, runtime_context: Any) -> "BashSandboxLayout":
        from packages.core.ai.runtime.sandbox import runtime_sandbox_layout

        binding_key = str(
            getattr(runtime_context, "sandbox_binding_key", None)
            or getattr(runtime_context, "conversation_id", None)
            or ""
        )
        layout = runtime_sandbox_layout(binding_key or None, getattr(runtime_context, "agent_id", None))
        return cls(location=bash_mirror.MirrorLocation.for_layout(layout), work_area_key=binding_key)

    def exec_queue_params(self) -> dict[str, Any]:
        """Fair-queue parameters: the work area, and no Skill view or Skill env."""

        from packages.core.services.sandbox_session_service import sandbox_exec_queue_params

        return sandbox_exec_queue_params(work_area=self.work_area_key or None, skill="")


def _bash_scope(entity_id: str, runtime_context: Any) -> Any:
    """The caller's sandbox scope for this entity (``primary_sandbox_scope_for``).

    The one resolver every sandbox consumer shares: the user's own scope for
    human work (one general sandbox across all of their conversations and
    tasks), the task's for user-less work, the room run's for a group run.
    """

    from types import SimpleNamespace

    from packages.core.ai.runtime.sandbox import primary_sandbox_scope_for

    return primary_sandbox_scope_for(SimpleNamespace(
        entity_id=entity_id,
        user_id=getattr(runtime_context, "user_id", None),
        runtime_envelope=getattr(runtime_context, "runtime_envelope", None),
        sandbox_binding_key=getattr(runtime_context, "sandbox_binding_key", None),
        conversation_id=getattr(runtime_context, "conversation_id", None),
    ))


async def _bash_sandbox(
    entity_id: str,
    runtime_context: Any,
) -> tuple[str | None, Any]:
    """The sandbox bash runs in: ``(sandbox_id, None)`` or ``(None, result)``.

    The one place that decides bash's sandbox scope: the same binding slot
    and admission as the generic ``sandbox`` tool (``acquire_generic_sandbox``),
    so both see one container. ``result`` is an error payload, or the
    Runtime suspension a durable run parks on while its reservation queues.
    """

    from packages.core.ai.runtime.sandbox import RuntimeSandboxKind, runtime_agent_sandbox
    from packages.core.ai.runtime.bash_sandbox import runtime_acquire_generic_sandbox
    from packages.core.services.sandbox_session_service import (
        SandboxSession,
        SandboxSessionError,
    )

    scope = _bash_scope(entity_id, runtime_context)
    if scope is None:
        return None, json.dumps({
            "error": "sandbox_scope_unavailable",
            "reason": "bash runs in a sandbox, which needs a conversation or task to bind it to.",
            "command_executed": False,
        })
    try:
        bound = await runtime_agent_sandbox(scope, runtime_context.agent_id, RuntimeSandboxKind.GENERIC)
    except Exception:  # noqa: BLE001 - fall through to admission
        bound = None
    if bound is not None and bound[0]:
        return await _bash_sandbox_credentials_gate(scope, bound[0])
    try:
        session = await runtime_acquire_generic_sandbox(scope=scope, runtime_context=runtime_context)
    except ValueError as exc:
        return None, json.dumps({"error": "sandbox_unavailable", "reason": str(exc), "command_executed": False})
    if isinstance(session, SandboxSessionError):
        if session.code == "capacity_full":
            return None, json.dumps({
                "error": "Sandbox capacity is full. Please retry later.",
                "details": session.message,
                "command_executed": False,
            })
        return None, json.dumps({
            "error": "sandbox_unavailable",
            "reason": session.message,
            "command_executed": False,
        })
    if not isinstance(session, SandboxSession):
        return None, session  # durable reservation suspension
    return await _bash_sandbox_credentials_gate(scope, session.sandbox_id)


async def _bash_sandbox_credentials_gate(scope: Any, sandbox_id: str) -> tuple[str | None, Any]:
    """Refuse bash in a sandbox holding a skill's stored credentials while a collaborator acts.

    bash shares the owner's general sandbox with the ``sandbox`` tool, where
    credentialed Skills may coexist; the same provenance rule as every
    ``sandbox`` call (``runtime_sandbox_credentials_refused``) applies.
    """

    from packages.core.ai.runtime.sandbox import (
        runtime_load_sandbox_binding,
        runtime_sandbox_credentials_refused,
    )

    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if await runtime_sandbox_credentials_refused(
        ctx, entity_id=scope.entity_id, user_id=scope.user_id or None,
    ):
        return None, json.dumps({
            "error": "sandbox_access_denied",
            "reason": (
                "This sandbox holds a skill's stored credentials and is not available to "
                "runs involving an external collaborator."
            ),
            "command_executed": False,
        })
    return sandbox_id, None


async def _forget_bash_sandbox(entity_id: str, runtime_context: Any, sandbox_id: str) -> None:
    from packages.core.services.sandbox_session_service import forget_vanished_sandbox

    scope = _bash_scope(entity_id, runtime_context)
    if scope is not None:
        await forget_vanished_sandbox(sandbox_id, scope=scope)
    location = BashSandboxLayout.for_context(runtime_context).location
    await bash_mirror.forget_manifest(location.manifest_id(sandbox_id))


def _sandbox_is_gone_error(exc: BaseException) -> bool:
    from packages.core.services.sandbox_queue_service import SandboxRouteNotFoundError
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    if isinstance(exc, SandboxRouteNotFoundError):
        return True
    return isinstance(exc, SandboxNotFoundError) and "execution" not in str(exc).lower()


def _cli_api_url() -> str:
    from packages.core.config import get_settings

    settings = get_settings()
    for value in (settings.SANDBOX_CLI_API_URL, settings.CLI_PUBLIC_API_URL, settings.APP_URL):
        if str(value or "").strip():
            return str(value).strip().rstrip("/")
    return ""


@asynccontextmanager
async def _mirror_section(manifest_id: str, timeout: float):
    """Short critical section over one working copy's manifest.

    Held while staging and while saving changes back, never while a command
    runs: concurrent commands in one container run in parallel, and only
    the bookkeeping of what is staged in one working copy is serialized.
    """

    from packages.core.cache import cache

    key = f"bash-mirror:lock:{manifest_id}"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    held = False
    while True:
        acquired = await cache.acquire_lease(key, token, ttl=int(timeout) + 60)
        if acquired is None:
            raise MirrorError("The bash working-copy state is unavailable (cache unreachable).")
        if acquired:
            held = True
            break
        if time.monotonic() >= deadline:
            raise MirrorError("Another command is still saving files in this sandbox; retry shortly.")
        await asyncio.sleep(_MIRROR_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        if held:
            await cache.release_lease(key, token)


# ---------------------------------------------------------------------------
# Staging: which entity files a command names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StageRequest:
    rel: str
    is_dir: bool
    content: bool  # False: names, sizes and mtimes are enough
    deep: bool  # directories: the whole tree, else direct children only
    explicit: bool  # named as such, not found by expanding a directory
    children: bool = True  # False: only the directory itself (a write target's parent)


def _command_segments_with_separators(command: str) -> list[tuple[str | None, list[str]]]:
    tokens = _shell_tokens(command)
    if not tokens:
        tokens = command.split()
    segments: list[tuple[str | None, list[str]]] = []
    current: list[str] = []
    separator: str | None = None
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append((separator, current))
            current, separator = [], token
            continue
        current.append(token)
    if current:
        segments.append((separator, current))
    return segments


def _token_rel_path(token: str, cwd_rel: str) -> str | None:
    """Entity-relative path a command word may name, or None."""

    text = token
    if text.startswith("-"):
        if "=" not in text:
            return None
        text = text.split("=", 1)[1]
    if not text or "://" in text or text.startswith("~") or any(ch in text for ch in "$`\n"):
        return None
    mirrored = bash_mirror.mirror_rel_path(text)
    if mirrored is not None:
        return mirrored
    if text.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(cwd_rel, text)) if cwd_rel else posixpath.normpath(text)
    if joined == ".":
        return ""
    if joined.startswith("../") or joined == "..":
        return None
    return joined


def _resolve_existing(entity_root: str, rel: str) -> list[tuple[str, bool]]:
    """``(rel, is_dir)`` for each existing, user-visible path ``rel`` names (globs expanded)."""

    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    root = os.path.realpath(entity_root)
    candidates = (
        sorted(glob.glob(os.path.join(root, rel))) if glob.has_magic(rel) else [os.path.join(root, rel)]
    )
    resolved: list[tuple[str, bool]] = []
    for candidate in candidates[:_MAX_CHANGED_FILES]:
        if os.path.islink(candidate) or not os.path.exists(candidate):
            continue
        full = os.path.realpath(candidate)
        if full != root and not full.startswith(root + os.sep):
            continue
        rel_path = normalize_rel_path(os.path.relpath(full, root))
        if rel_path and not is_user_visible_path(rel_path):
            continue
        resolved.append((rel_path, os.path.isdir(full)))
    return resolved


def _stage_requests(command: str, entity_root: str) -> list[_StageRequest]:
    """The entity paths a command names, and how much of each it needs."""

    requests: list[_StageRequest] = []
    cwd_rel = ""
    for separator, segment in _command_segments_with_separators(command):
        if not segment:
            continue
        base = segment[0].rsplit("/", 1)[-1]
        args = segment[1:]
        if base == "cd":
            target = _token_rel_path(args[0], cwd_rel) if args else ""
            cwd_rel = target or ""
            continue
        names_files = base not in _NO_FILE_ARGUMENT_COMMANDS
        content = base not in _METADATA_COMMANDS
        recursive_ls = base == "ls" and any(
            arg == "--recursive" or (arg.startswith("-") and not arg.startswith("--") and "R" in arg)
            for arg in args
        )
        deep = base != "ls" or recursive_ls
        named = 0
        previous = ""
        for arg in args:
            redirect_target = previous in {">", ">>", "<"}
            previous = arg
            if arg in {">", ">>", "<", ">|", "<<", "2>", "&>"}:
                continue
            if not names_files and not redirect_target:
                continue
            rel = _token_rel_path(arg, cwd_rel)
            if rel is None:
                continue
            existing = _resolve_existing(entity_root, rel)
            if not existing and not glob.has_magic(rel):
                # A path the command may create: its folder must exist in
                # the working copy, as it does in the entity filesystem.
                parent = posixpath.dirname(rel)
                for resolved, is_dir in _resolve_existing(entity_root, parent) if parent else []:
                    if is_dir:
                        requests.append(_StageRequest(resolved, True, False, False, False, children=False))
            for resolved, is_dir in existing:
                named += 1
                requests.append(_StageRequest(
                    rel=resolved,
                    is_dir=is_dir,
                    content=content or redirect_target,
                    deep=deep,
                    explicit=True,
                ))
        if named or not names_files:
            continue
        # Commands that default to the working directory.
        if base == "ls" or base in {"find", "tree", "du"}:
            requests.append(_StageRequest(cwd_rel, True, False, deep, True))
        elif base == "rg" and separator != "|":
            requests.append(_StageRequest(cwd_rel, True, True, True, True))
        elif base in {"grep", "egrep", "fgrep"} and separator != "|" and any(
            arg in {"-r", "-R", "--recursive"} or (arg.startswith("-") and not arg.startswith("--") and ("r" in arg or "R" in arg))
            for arg in args
        ):
            requests.append(_StageRequest(cwd_rel, True, True, True, True))
    return requests


@dataclass
class _StagePlan:
    files: dict[str, bytes] = field(default_factory=dict)
    file_meta: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    placeholders: dict[str, tuple[int, int]] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    deletes: list[str] = field(default_factory=list)
    reset: bool = False


class _StagingRefused(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error"))
        self.payload = payload


def _read_entity_file(full_path: str) -> tuple[bytes, int, int]:
    fd = os.open(full_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
        return data, info.st_size, info.st_mtime_ns
    finally:
        os.close(fd)


def _walk_request(entity_root: str, request: _StageRequest, budget_entries: int) -> tuple[list[str], list[str]]:
    """Files and directories a request covers (bounded by entry count)."""

    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    if not request.is_dir:
        return [request.rel], []
    if not request.children:
        return [], [request.rel] if request.rel else []
    root = os.path.realpath(entity_root)
    base = os.path.join(root, request.rel) if request.rel else root
    files: list[str] = []
    dirs: list[str] = [request.rel] if request.rel else []
    for directory, dirnames, filenames in os.walk(base):
        rel_dir = normalize_rel_path(os.path.relpath(directory, root))
        dirnames[:] = sorted(
            name for name in dirnames
            if not os.path.islink(os.path.join(directory, name))
            and is_user_visible_path(f"{rel_dir}/{name}" if rel_dir else name)
        )
        for name in dirnames:
            dirs.append(f"{rel_dir}/{name}" if rel_dir else name)
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(directory, name)
            if os.path.islink(full) or not os.path.isfile(full) or not is_user_visible_path(rel):
                continue
            files.append(rel)
        if len(files) + len(dirs) > budget_entries:
            break
        if not request.deep:
            dirnames[:] = []
    return files, dirs


def _folder_skeleton(entity_root: str, limit: int) -> list[str]:
    """User-visible folders of the entity, shallowest first, at most ``limit``."""

    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    root = os.path.realpath(entity_root)
    found: list[str] = []
    level = [root]
    while level and len(found) < limit:
        following: list[str] = []
        for directory in level:
            try:
                with os.scandir(directory) as entries:
                    names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
            except OSError:
                continue
            for name in names:
                full = os.path.join(directory, name)
                rel = normalize_rel_path(os.path.relpath(full, root))
                if not is_user_visible_path(rel):
                    continue
                found.append(rel)
                following.append(full)
                if len(found) >= limit:
                    return found
        level = following
    return found


async def _unreadable(entity_id: str, runtime_context: Any, files: list[str], dirs: list[str]) -> set[str]:
    from packages.core.database import async_session
    from packages.core.services.document_access import unreadable_document_paths

    blocked: set[str] = set()
    async with async_session() as db:
        for start in range(0, len(files), 500):
            blocked |= await unreadable_document_paths(
                db, entity_id=entity_id, rel_paths=files[start:start + 500],
                user_id=runtime_context.user_id, workspace_id=runtime_context.workspace_id,
                actor_type="agent",
            )
        for start in range(0, len(dirs), 500):
            blocked |= await unreadable_document_paths(
                db, entity_id=entity_id, rel_paths=[], directory_paths=dirs[start:start + 500],
                user_id=runtime_context.user_id, workspace_id=runtime_context.workspace_id,
                actor_type="agent",
            )
    return blocked


def _staging_limit_error(reason: str) -> dict[str, Any]:
    return {
        "error": "bash_staging_limit",
        "reason": reason,
        "hint": (
            "Name narrower paths, or work on the server without copying: "
            "`manor grep PATTERN PATH`, `manor ls -R PATH`, `manor search QUESTION`, "
            "`manor cat PATH`."
        ),
        "command_executed": False,
    }


async def _plan_staging(
    *,
    entity_id: str,
    entity_root: str,
    runtime_context: Any,
    command: str,
    manifest: MirrorManifest,
) -> _StagePlan:
    from packages.core.config import get_settings

    settings = get_settings()
    max_bytes = max(1, int(settings.BASH_SANDBOX_STAGE_MAX_BYTES))
    max_entries = max(1, int(settings.BASH_SANDBOX_STAGE_MAX_FILES))
    plan = _StagePlan(reset=not manifest.persisted)
    requests = _stage_requests(command, entity_root)
    if not manifest.persisted:
        # A fresh working copy gets the entity's folder skeleton (names
        # only), so a script can write into an existing folder it does not
        # name, as it could in the entity filesystem.
        requests.extend(
            _StageRequest(rel, True, False, False, False, children=False)
            for rel in await asyncio.to_thread(_folder_skeleton, entity_root, max_entries // 2)
        )

    wanted_files: dict[str, bool] = {}  # rel -> needs content
    wanted_dirs: set[str] = set()
    explicit_files: set[str] = set()
    explicit_dirs: set[str] = set()
    for request in requests:
        files, dirs = await asyncio.to_thread(_walk_request, entity_root, request, max_entries)
        if len(files) + len(dirs) > max_entries:
            raise _StagingRefused(_staging_limit_error(
                f"{request.rel or '.'} holds more than {max_entries} files and folders."
            ))
        for rel in files:
            wanted_files[rel] = wanted_files.get(rel, False) or request.content
        wanted_dirs.update(dirs)
        if request.explicit:
            (explicit_dirs if request.is_dir else explicit_files).add(request.rel)
    if len(wanted_files) + len(wanted_dirs) > max_entries:
        raise _StagingRefused(_staging_limit_error(
            f"The command names more than {max_entries} files and folders."
        ))

    # Read ACL, exactly as the old local bash: a document the caller cannot
    # view is never copied in, and naming one refuses the command.
    blocked = await _unreadable(
        entity_id,
        runtime_context,
        sorted(set(wanted_files) | set(manifest.files)),
        sorted(path for path in wanted_dirs if path),
    )
    named_blocked = sorted((explicit_files | explicit_dirs) & blocked)
    if named_blocked:
        raise _StagingRefused({
            "error": (
                "Access denied: this command reads a document you do not have permission to view "
                f"({', '.join(named_blocked)}). It was not executed."
            ),
            "command_executed": False,
        })
    blocked_dirs = {path for path in wanted_dirs if path in blocked}
    for rel in list(wanted_files):
        if rel in blocked or any(rel.startswith(directory + "/") for directory in blocked_dirs):
            wanted_files.pop(rel)
    wanted_dirs = {
        path for path in wanted_dirs
        if path not in blocked_dirs and not any(path.startswith(directory + "/") for directory in blocked_dirs)
    }
    # A document that became unreadable since it was staged leaves the copy.
    plan.deletes.extend(sorted(path for path in manifest.files if path in blocked))

    root = os.path.realpath(entity_root)
    new_bytes = 0
    for rel, needs_content in sorted(wanted_files.items()):
        full = os.path.join(root, rel)
        try:
            info = await asyncio.to_thread(os.lstat, full)
        except OSError:
            continue
        entry = manifest.files.get(rel)
        fresh = entry is not None and entry.size == info.st_size and entry.mtime_ns == info.st_mtime_ns
        if fresh and (entry.kind == "file" or not needs_content):
            continue
        if needs_content:
            new_bytes += info.st_size
            if new_bytes > max_bytes:
                raise _StagingRefused(_staging_limit_error(
                    f"The files this command names exceed {max_bytes} bytes."
                ))
            data, size, mtime_ns = await asyncio.to_thread(_read_entity_file, full)
            plan.files[rel] = data
            plan.file_meta[rel] = (hashlib.sha256(data).hexdigest(), size, mtime_ns)
        else:
            plan.placeholders[rel] = (info.st_size, info.st_mtime_ns)
    for rel in list(plan.files) + list(plan.placeholders):
        parent = posixpath.dirname(rel)
        while parent:
            wanted_dirs.add(parent)
            parent = posixpath.dirname(parent)
    plan.dirs = {path for path in wanted_dirs if path and path not in manifest.dirs}
    # Never let the working copy outgrow the sandbox's tmpfs: start it over.
    if manifest.staged_bytes + new_bytes > 3 * max_bytes:
        plan.reset = True
    return plan


async def _apply_stage_plan(
    mirror: SandboxMirror,
    manifest: MirrorManifest,
    plan: _StagePlan,
) -> bool:
    """Run one staging step and record it; returns whether the copy was reset."""

    if plan.reset:
        manifest.files.clear()
        manifest.dirs.clear()
    if not (plan.files or plan.placeholders or plan.dirs or plan.deletes or plan.reset):
        return False
    reset, stats = await mirror.stage(
        epoch=manifest.epoch,
        reset=plan.reset,
        files=plan.files,
        placeholders={rel: size for rel, (size, _mtime) in plan.placeholders.items()},
        deletes=plan.deletes,
        dirs=sorted(plan.dirs),
    )
    if reset and not plan.reset:
        manifest.files.clear()
        manifest.dirs.clear()
    for rel in plan.deletes:
        manifest.files.pop(rel, None)
    for rel, (sha, size, mtime_ns) in plan.file_meta.items():
        ino, ssize, smtime_ns = stats.get(rel, (0, 0, 0))
        manifest.files[rel] = MirrorEntry("file", sha, size, mtime_ns, ino, ssize, smtime_ns)
    for rel, (size, mtime_ns) in plan.placeholders.items():
        ino, ssize, smtime_ns = stats.get(rel, (0, 0, 0))
        manifest.files[rel] = MirrorEntry("placeholder", "", size, mtime_ns, ino, ssize, smtime_ns)
    manifest.dirs |= plan.dirs
    for rel in list(plan.files) + list(plan.placeholders):
        parent = posixpath.dirname(rel)
        while parent:
            manifest.dirs.add(parent)
            parent = posixpath.dirname(parent)
    return reset and not plan.reset


async def _stage_command(
    *,
    entity_id: str,
    entity_root: str,
    runtime_context: Any,
    command: str,
    mirror: SandboxMirror,
    sandbox_id: str,
    fresh: bool = False,
    output_dirs: tuple[str, ...] = (),
) -> tuple[dict[str, Any] | None, str]:
    """Stage what a command names: ``(refusal payload or None, working-copy epoch)``.

    ``fresh`` discards what the manifest says is staged (the container's
    copy turned out not to match it) and starts the copy over.
    """

    async with _mirror_section(mirror.manifest_id, timeout=60):
        manifest = (
            bash_mirror.MirrorManifest.fresh(mirror.manifest_id)
            if fresh else await bash_mirror.load_manifest(mirror.manifest_id)
        )
        from packages.core.services.entity_fs import entity_filesystem_read_lock

        for _attempt in range(2):
            try:
                # Same read boundary as every raw filesystem reader: the ACL
                # decision and the bytes copied belong to one snapshot.
                async with entity_filesystem_read_lock(entity_root):
                    plan = await _plan_staging(
                        entity_id=entity_id,
                        entity_root=entity_root,
                        runtime_context=runtime_context,
                        command=command,
                        manifest=manifest,
                    )
                # Folders of Workspace-scoped outputs exist before the
                # command writes into them, as the local bash created them.
                for path in output_dirs:
                    while path and path not in manifest.dirs:
                        plan.dirs.add(path)
                        path = posixpath.dirname(path)
            except _StagingRefused as refused:
                return refused.payload, manifest.epoch
            unexpected_reset = await _apply_stage_plan(mirror, manifest, plan)
            if not unexpected_reset:
                await bash_mirror.save_manifest(manifest)
                return None, manifest.epoch
            # The container's copy did not match the manifest (new or wiped
            # copy): everything it was assumed to hold must be staged again.
            manifest = bash_mirror.MirrorManifest.fresh(mirror.manifest_id)
        await bash_mirror.save_manifest(manifest)
        return None, manifest.epoch


# ---------------------------------------------------------------------------
# Changes a command made to the working copy
# ---------------------------------------------------------------------------


@dataclass
class _ChangeSet:
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)
    deleted_dirs: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    overwrites: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)  # queued manor mv/cp/rm

    def file_paths(self) -> list[str]:
        paths = [*self.created, *self.modified, *self.deleted, *self.created_dirs, *self.deleted_dirs]
        for source, destination in self.moved:
            paths.extend((source, destination))
        for operation in self.operations:
            paths.extend(operation["paths"])
        return list(dict.fromkeys(paths))

    def guarded_paths(self) -> list[str]:
        """The changes authorization judges: a new folder that only holds other changes is implied.

        ``manor push f d/e/f`` (or ``mkdir -p d && cp f d/``) creates ``d``
        and ``d/e`` to hold ``d/e/f``; approving the file approves its
        folders. A new folder with nothing else changed in it is judged itself.
        """
        paths = self.file_paths()
        implied = {
            folder for folder in self.created_dirs
            if any(path.startswith(folder.rstrip("/") + "/") for path in paths)
        }
        return [path for path in paths if path not in implied]

    @property
    def empty(self) -> bool:
        return not self.file_paths()

    def action(self) -> FileMutationAction:
        if self.deleted or self.deleted_dirs or any(op["op"] == "rm" for op in self.operations):
            return FileMutationAction.DELETE
        if self.modified or self.moved or any(op["op"] == "mv" for op in self.operations):
            return FileMutationAction.EDIT
        return FileMutationAction.WRITE

    def summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "created": self.created,
            "modified": self.modified,
            "deleted": self.deleted,
            "moved": [list(pair) for pair in self.moved],
            "created_dirs": self.created_dirs,
            "deleted_dirs": self.deleted_dirs,
            "manor_operations": [
                {"op": op["op"], "paths": op["paths"]} for op in self.operations
            ],
        }
        return {key: value for key, value in data.items() if value}


def _compute_changes(manifest: MirrorManifest, snapshot: MirrorSnapshot) -> _ChangeSet:
    from packages.core.services.knowledge_visibility import is_user_visible_path

    changes = _ChangeSet()
    files = snapshot.files
    unmatched: dict[int, list[str]] = {}
    for rel, entry in files.items():
        staged = manifest.files.get(rel)
        if staged is None or staged.ino != entry.ino:
            unmatched.setdefault(entry.ino, []).append(rel)
    consumed: set[str] = set()
    for rel, staged in sorted(manifest.files.items()):
        current = files.get(rel)
        if current is not None and current.ino == staged.ino:
            if staged.kind == "placeholder":
                if (current.size, current.mtime_ns, current.mode & 0o777) != (staged.ssize, staged.smtime_ns, 0):
                    changes.violations.append(rel)
            elif (current.size, current.mtime_ns) != (staged.ssize, staged.smtime_ns) and (
                current.sha is not None and current.sha != staged.sha
            ):
                changes.modified.append(rel)
            continue
        targets = [path for path in unmatched.get(staged.ino, []) if path not in consumed] if staged.ino else []
        if len(targets) == 1:
            destination = targets[0]
            consumed.add(destination)
            if destination in manifest.files:
                changes.overwrites.append(destination)
            changes.moved.append((rel, destination))
            moved_entry = files[destination]
            if staged.kind == "placeholder":
                if (moved_entry.size, moved_entry.mode & 0o777) != (staged.ssize, 0):
                    changes.violations.append(destination)
            elif moved_entry.sha is not None and moved_entry.sha != staged.sha:
                changes.modified.append(destination)
            continue
        if current is None:
            changes.deleted.append(rel)
        elif staged.kind == "placeholder" or current.sha != staged.sha:
            changes.modified.append(rel)
    for rel in sorted(files):
        if rel in manifest.files or rel in consumed:
            continue
        changes.created.append(rel)
    snapshot_dirs = snapshot.dirs
    changes.created_dirs = sorted(path for path in snapshot_dirs if path not in manifest.dirs)
    changes.deleted_dirs = sorted(path for path in manifest.dirs if path not in snapshot_dirs)
    changes.ignored = sorted(set(snapshot.ignored) | snapshot.links)

    def visible(path: str) -> bool:
        return is_user_visible_path(path)

    for name in ("created", "modified", "deleted", "created_dirs", "deleted_dirs"):
        kept = []
        for path in getattr(changes, name):
            (kept if visible(path) else changes.ignored).append(path)
        setattr(changes, name, kept)
    changes.moved = [(s, d) for s, d in changes.moved if visible(s) and visible(d)]
    return changes


async def _adopt_identical_creations(
    entity_root: str,
    changes: _ChangeSet,
    snapshot: MirrorSnapshot,
) -> None:
    """A created file equal to the entity file at its path (``manor pull``) is no change."""

    root = os.path.realpath(entity_root)
    for rel in list(changes.created):
        full = os.path.join(root, rel)
        entry = snapshot.files.get(rel)
        if entry is None or entry.sha is None or not os.path.isfile(full) or os.path.islink(full):
            continue
        info = await asyncio.to_thread(os.stat, full)
        if info.st_size != entry.size:
            continue
        data, _size, _mtime = await asyncio.to_thread(_read_entity_file, full)
        if hashlib.sha256(data).hexdigest() == entry.sha:
            changes.created.remove(rel)
            changes.adopted.append(rel)


def _outputs_outside_workspace(root: str, changes: _ChangeSet, artifact_base_dir: str) -> list[str]:
    """New paths of a Workspace run whose location nobody chose.

    A created file or folder is ambiguous unless it lies in the Workspace's
    own storage root, already exists in the entity filesystem, or sits under
    an existing folder below the entity root (an explicit destination, still
    subject to the guard and ACL). ``changes`` holds user-visible paths only.
    """

    from packages.core.services.generated_media_naming import workspace_storage_root

    home = workspace_storage_root(artifact_base_dir)
    if not home:
        return []
    outside: list[str] = []
    created_dirs = set(changes.created_dirs)
    for rel in [*changes.created, *changes.created_dirs]:
        if rel == home or rel.startswith(home + "/"):
            continue
        if rel in created_dirs and home.startswith(rel + "/"):
            continue  # an ancestor folder of the Workspace storage root
        existing = rel
        while existing and not os.path.lexists(os.path.join(root, existing)):
            existing = posixpath.dirname(existing)
        if not existing:
            outside.append(rel)
    return outside


def _within(path: str, roots: set[str]) -> bool:
    return any(
        path == root or path.startswith(root.rstrip("/") + "/") or root.startswith(path.rstrip("/") + "/")
        for root in roots
    )


# ---------------------------------------------------------------------------
# Saving changes back
# ---------------------------------------------------------------------------


async def _change_acl_error(
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    entity_root: str,
    actions: dict[str, str],
) -> str | None:
    """Filesystem ACL per changed path (``delete``/``move``/``write``), as the /fs API applies it."""

    if not user_id or not actions:
        return None
    from types import SimpleNamespace

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.user import User
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.filesystem_access import (
        FilesystemAccessDenied,
        require_path_mutation_access,
        require_path_write_access,
    )

    root = os.path.realpath(entity_root)
    async with async_session() as db:
        user = await db.scalar(
            select(User).where(User.id == user_id, User.status == "active", User.deleted_at.is_(None))
        )
        if user is None:
            return json.dumps({"error": "Active user context is required for file changes"})
        role = await resolve_effective_user_role_name(
            db, user_id=user.id, entity_id=entity_id,
            legacy_role=user.role if user.entity_id == entity_id else None,
        )
        if not role:
            return json.dumps({"error": "Active entity membership is required for file changes"})
        actor = SimpleNamespace(
            id=user.id, entity_id=entity_id, role=role, email=user.email, display_name=user.display_name,
        )
        try:
            for rel, action in actions.items():
                full = os.path.join(root, rel)
                if os.path.lexists(full) and action in {"delete", "move"}:
                    await require_path_mutation_access(
                        db, user=actor, rel_path=rel, full_path=full, entity_root=root,
                        action=action, workspace_id=workspace_id,
                    )
                else:
                    await require_path_write_access(
                        db, user=actor, rel_path=rel, exists=os.path.exists(full), workspace_id=workspace_id,
                    )
        except FilesystemAccessDenied as exc:
            return json.dumps({
                "error": "file_permission_denied",
                "reason": exc.detail,
                "operation": {"tool": "bash", "paths": sorted(actions)},
            })
    return None


def _change_actions(changes: _ChangeSet) -> dict[str, str]:
    actions: dict[str, str] = {}
    for rel in [*changes.created, *changes.modified, *changes.created_dirs]:
        actions.setdefault(rel, "write")
    for rel in [*changes.deleted, *changes.deleted_dirs]:
        actions[rel] = "delete"
    for source, destination in changes.moved:
        actions[source] = "move"
        actions.setdefault(destination, "write")
    for operation in changes.operations:
        paths = operation["paths"]
        if operation["op"] == "rm":
            for rel in paths:
                actions[rel] = "delete"
        else:
            for rel in paths[:-1]:
                actions[rel] = "move" if operation["op"] == "mv" else actions.get(rel, "write")
            actions.setdefault(paths[-1], "write")
    return actions


def _safe_entity_path(root: str, rel: str) -> str:
    full = os.path.normpath(os.path.join(root, rel))
    parent = os.path.realpath(os.path.dirname(full))
    if not full.startswith(root + os.sep) or not (parent == root or parent.startswith(root + os.sep)):
        raise ValueError(f"{rel} escapes the entity filesystem")
    if os.path.islink(full):
        raise ValueError(f"{rel} is a symbolic link")
    return full


async def _operation_targets(root: str, operation: dict[str, Any]) -> list[tuple[str, str]]:
    sources, destination = operation["paths"][:-1], operation["paths"][-1]
    destination_full = _safe_entity_path(root, destination)
    into_directory = len(sources) > 1 or os.path.isdir(destination_full)
    return [
        (source, posixpath.join(destination, posixpath.basename(source)) if into_directory else destination)
        for source in sources
    ]


async def _apply_queued_operations(
    *,
    entity_id: str,
    root: str,
    operations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply ``manor mv/cp/rm`` on the entity filesystem; returns projection ops and touched paths."""

    projection_ops: list[dict[str, Any]] = []
    touched: list[str] = []
    for operation in operations:
        if operation["op"] == "rm":
            for rel in operation["paths"]:
                full = _safe_entity_path(root, rel)
                if os.path.isdir(full):
                    if not operation.get("recursive"):
                        raise ValueError(f"manor rm {rel}: is a directory (use -r)")
                    await asyncio.to_thread(shutil.rmtree, full)
                elif os.path.lexists(full):
                    await asyncio.to_thread(os.remove, full)
                touched.append(rel)
            continue
        for source, target in await _operation_targets(root, operation):
            source_full = _safe_entity_path(root, source)
            target_full = _safe_entity_path(root, target)
            if os.path.lexists(target_full):
                raise FileExistsError(target)
            os.makedirs(os.path.dirname(target_full), exist_ok=True)
            is_directory = os.path.isdir(source_full)
            if operation["op"] == "mv":
                requires = await _projection_exists(entity_id, source, is_directory=is_directory)
                await asyncio.to_thread(os.rename, source_full, target_full)
                projection_ops.append({
                    "id": secrets.token_hex(16), "kind": "move", "source": source, "destination": target,
                    "source_is_directory": is_directory, "requires_projection": requires,
                })
            elif is_directory:
                await asyncio.to_thread(shutil.copytree, source_full, target_full, symlinks=True)
            else:
                await asyncio.to_thread(shutil.copy2, source_full, target_full)
                projection_ops.append({
                    "id": secrets.token_hex(16), "kind": "copy", "source": source, "destination": target,
                    "source_is_directory": False,
                    "requires_projection": await _projection_exists(entity_id, source, is_directory=False),
                })
            touched.extend((source, target))
    return projection_ops, touched


async def _check_bases(root: str, manifest: MirrorManifest, changes: _ChangeSet) -> list[str]:
    """Staged files whose entity version changed while the command ran."""

    conflicts: list[str] = []
    for rel in [*changes.modified, *changes.deleted, *(source for source, _ in changes.moved)]:
        staged = manifest.files.get(rel)
        if staged is None:
            continue
        full = os.path.join(root, rel)
        try:
            info = await asyncio.to_thread(os.stat, full)
        except FileNotFoundError:
            conflicts.append(rel)
            continue
        if (info.st_size, info.st_mtime_ns) != (staged.size, staged.mtime_ns):
            conflicts.append(rel)
    return conflicts


async def _write_changes_to_entity(
    *,
    entity_id: str,
    root: str,
    changes: _ChangeSet,
    contents: dict[str, bytes],
    manifest: MirrorManifest,
    projection_command_ops: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply working-copy changes to the entity filesystem; returns projection ops."""

    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic

    projection_ops: list[dict[str, Any]] = []
    moved = list(changes.moved)
    created_dirs = list(changes.created_dirs)
    deleted_dirs = list(changes.deleted_dirs)
    for op in projection_command_ops:
        # ``mv dir other`` named a whole directory: move it as one, so its
        # Knowledge folder (and anything the caller could not see in it)
        # keeps its identity, as the local bash did.
        if not (
            op["kind"] == "move" and op["source_is_directory"]
            and op["source"] in deleted_dirs and op["destination"] in created_dirs
        ):
            continue
        source_full = _safe_entity_path(root, op["source"])
        destination_full = _safe_entity_path(root, op["destination"])
        if os.path.lexists(destination_full):
            raise FileExistsError(op["destination"])
        os.makedirs(os.path.dirname(destination_full), exist_ok=True)
        await asyncio.to_thread(os.rename, source_full, destination_full)
        projection_ops.append(dict(op))
        source_prefix, destination_prefix = op["source"] + "/", op["destination"] + "/"
        moved = [
            (s, d) for s, d in moved
            if not (s.startswith(source_prefix) and d.startswith(destination_prefix))
        ]
        created_dirs = [
            path for path in created_dirs
            if path != op["destination"] and not path.startswith(destination_prefix)
        ]
        deleted_dirs = [
            path for path in deleted_dirs
            if path != op["source"] and not path.startswith(source_prefix)
        ]
    for source, destination in moved:
        source_full = _safe_entity_path(root, source)
        destination_full = _safe_entity_path(root, destination)
        if os.path.lexists(destination_full):
            raise FileExistsError(destination)
        requires = await _projection_exists(entity_id, source, is_directory=False)
        os.makedirs(os.path.dirname(destination_full), exist_ok=True)
        await asyncio.to_thread(os.rename, source_full, destination_full)
        projection_ops.append({
            "id": secrets.token_hex(16), "kind": "move", "source": source, "destination": destination,
            "source_is_directory": False, "requires_projection": requires,
        })
    for rel in changes.deleted:
        full = _safe_entity_path(root, rel)
        if os.path.lexists(full):
            await asyncio.to_thread(os.remove, full)
    for rel in created_dirs:
        await asyncio.to_thread(os.makedirs, _safe_entity_path(root, rel), exist_ok=True)
    for rel in [*changes.created, *changes.modified]:
        _safe_entity_path(root, rel)
        await asyncio.to_thread(
            runtime_write_entity_file_atomic, entity_id, rel, contents[rel], allow_empty=True,
        )
    for rel in sorted(deleted_dirs, key=len, reverse=True):
        full = _safe_entity_path(root, rel)
        try:
            os.rmdir(full)  # only when nothing the caller could not see is left in it
        except OSError:
            pass
    return projection_ops


def _unapproved_changes_refusal(paths: list[str]) -> dict[str, Any]:
    return {
        "error": "bash_unapproved_changes",
        "reason": (
            "The command changed files beyond the paths it names; they were not saved: "
            f"{', '.join(paths[:10])}. Run the part that writes them as its own command that "
            "names each output path (for example `manor push /tmp/<file> <entity path>`)."
        ),
        "operation": {"tool": "bash", "paths": paths},
    }


def _paths_beyond_receipt(entity_id: str, paths: list[str]) -> list[str]:
    """Changed paths the approved bash call's receipt does not name.

    A bash receipt is predicted from the command text, which cannot name
    every file a program writes (``mkdir -p out && python3 render.py``).
    Changes outside it are this command's own doing, so they are refused as
    unapproved changes, never reported as an internal authorization error.
    """
    from packages.core.ai.runtime.authorization_receipts import (
        runtime_current_tool_authorization_receipt,
    )

    receipt = runtime_current_tool_authorization_receipt()
    if (
        receipt is None
        or receipt.tool_name != "bash"
        or receipt.capability_id != "file.write"
        or receipt.entity_id != entity_id
    ):
        return []
    return [path for path in paths if not receipt.names_workspace_file(path)]


def _guard_paths_cover(predicted: list[str], changes: _ChangeSet) -> bool:
    from packages.core.services.knowledge_visibility import normalize_rel_path

    roots = {normalize_rel_path(path) for path in predicted if normalize_rel_path(path)}
    return all(_within(path, roots) for path in changes.guarded_paths())


async def _revert_working_copy(
    *,
    mirror: SandboxMirror,
    manifest: MirrorManifest,
    entity_root: str,
    changes: _ChangeSet,
) -> None:
    """Put the touched paths of the working copy back to the entity version."""

    root = os.path.realpath(entity_root)
    restore = sorted(
        {*changes.modified, *changes.deleted, *changes.violations, *(s for s, _ in changes.moved)}
        & set(manifest.files)
    )
    deletes = sorted(
        {*changes.created, *(d for _, d in changes.moved), *changes.created_dirs}
        - set(restore)
    )
    plan = _StagePlan(deletes=deletes, dirs=set(changes.deleted_dirs))
    for rel in restore:
        staged = manifest.files.pop(rel)
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        if staged.kind == "file":
            data, size, mtime_ns = await asyncio.to_thread(_read_entity_file, full)
            plan.files[rel] = data
            plan.file_meta[rel] = (hashlib.sha256(data).hexdigest(), size, mtime_ns)
        else:
            info = os.stat(full)
            plan.placeholders[rel] = (info.st_size, info.st_mtime_ns)
    for rel in deletes:
        manifest.files.pop(rel, None)
    manifest.dirs -= set(changes.created_dirs)
    await _apply_stage_plan(mirror, manifest, plan)


async def _record_saved_changes(
    *,
    manifest: MirrorManifest,
    snapshot: MirrorSnapshot,
    root: str,
    changes: _ChangeSet,
    contents: dict[str, bytes],
    mirror: SandboxMirror,
    operation_paths: list[str],
) -> None:
    """Make the manifest describe the working copy after its changes were saved."""

    for source, destination in changes.moved:
        entry = manifest.files.pop(source, None)
        if entry is not None:
            manifest.files[destination] = entry
    for rel in changes.deleted:
        manifest.files.pop(rel, None)
    for rel in [*changes.created, *changes.modified, *changes.adopted, *(d for _, d in changes.moved)]:
        current = snapshot.files.get(rel)
        full = os.path.join(root, rel)
        if current is None or not os.path.isfile(full):
            manifest.files.pop(rel, None)
            continue
        info = os.stat(full)
        previous = manifest.files.get(rel)
        if rel in contents:
            sha = hashlib.sha256(contents[rel]).hexdigest()
        elif current.sha:
            sha = current.sha
        else:
            sha = previous.sha if previous else ""
        kind = previous.kind if previous is not None and rel not in contents and not current.sha else "file"
        manifest.files[rel] = MirrorEntry(
            kind, sha, info.st_size, info.st_mtime_ns, current.ino, current.size, current.mtime_ns,
        )
    manifest.dirs |= set(changes.created_dirs)
    manifest.dirs -= set(changes.deleted_dirs)
    if operation_paths:
        # Paths changed on the server by manor mv/cp/rm leave the copy; the
        # next command that names them stages the new state.
        evict = sorted(
            path for path in manifest.files
            if any(path == rel or path.startswith(rel + "/") for rel in operation_paths)
        )
        for rel in evict:
            manifest.files.pop(rel, None)
        evict_dirs = [
            path for path in manifest.dirs
            if any(path == rel or path.startswith(rel + "/") for rel in operation_paths)
        ]
        manifest.dirs -= set(evict_dirs)
        await mirror.stage(epoch=manifest.epoch, deletes=sorted(set(evict) | set(evict_dirs)))


async def _save_changes(
    *,
    entity_id: str,
    entity_root: str,
    runtime_context: Any,
    command: str,
    approval_token: str | None,
    mirror: SandboxMirror,
    sandbox_id: str,
    operations: list[dict[str, Any]],
    pre_authorized_paths: list[str] | None,
    projection_command_ops: list[dict[str, Any]],
    artifact_base_dir: str = "",
) -> dict[str, Any]:
    """Compare the working copy with what was staged and save the difference.

    Returns fields to merge into the bash result; an ``__override__`` key
    replaces the result (refusal or approval request). In a Workspace run
    (``artifact_base_dir``) new files whose place was not chosen explicitly
    are refused rather than saved at the entity root.
    """

    from packages.core.config import get_settings
    from packages.core.services.ai_file_permissions import guard_ai_file_mutation

    async with _mirror_section(mirror.manifest_id, timeout=120):
        manifest = await bash_mirror.load_manifest(mirror.manifest_id)
        snapshot = await mirror.snapshot(manifest, max_entries=_SNAPSHOT_MAX_ENTRIES)
        if snapshot.epoch != manifest.epoch:
            await bash_mirror.forget_manifest(mirror.manifest_id)
            if not snapshot.entries and not operations:
                return {}
            return {
                "file_changes_saved": False,
                "file_changes_note": (
                    "The sandbox working copy was reset during this command, so its file "
                    "changes could not be attributed and were not saved."
                ),
            }
        if snapshot.truncated:
            return {"__override__": {
                "error": "bash_too_many_files",
                "reason": "The working copy holds too many files to compare; write scratch output under /tmp.",
            }}
        root = os.path.realpath(entity_root)
        changes = _compute_changes(manifest, snapshot)
        changes.operations = list(operations)
        await _adopt_identical_creations(root, changes, snapshot)

        refusal: dict[str, Any] | None = None
        if changes.violations:
            refusal = {
                "error": "bash_unstaged_content",
                "reason": (
                    "These files were only listed (copied without content), so changes to "
                    f"them cannot be saved: {', '.join(changes.violations[:10])}. Name a file "
                    "in a command (or `manor pull` it) before editing it."
                ),
            }
        elif changes.overwrites:
            refusal = {
                "error": "file_destination_exists",
                "reason": "Move destination already exists; delete it explicitly before replacing it",
                "operation": {"tool": "bash", "action": "edit", "paths": changes.overwrites},
            }
        elif len(changes.file_paths()) > _MAX_CHANGED_FILES:
            refusal = {
                "error": "bash_too_many_changes",
                "reason": (
                    f"The command changed more than {_MAX_CHANGED_FILES} files under /workspace; "
                    "put scratch output under /tmp and save only the results."
                ),
            }
        if refusal is None and changes.empty:
            if changes.adopted:
                await _record_saved_changes(
                    manifest=manifest, snapshot=snapshot, root=root, changes=changes,
                    contents={}, mirror=mirror, operation_paths=[],
                )
                await bash_mirror.save_manifest(manifest)
            return {}

        if refusal is None and artifact_base_dir:
            outside = _outputs_outside_workspace(root, changes, artifact_base_dir)
            if outside:
                output_dir = workspace_output_dir(artifact_base_dir)
                refusal = {
                    "error": "bash_output_outside_workspace",
                    "reason": (
                        "In a Workspace run, new files belong in this Workspace's output folder "
                        f"(${OUTPUT_DIR_ENV} = {output_dir}) or an existing folder you name; "
                        f"these were created elsewhere and nothing was saved: {', '.join(outside[:10])}. "
                        f"Run the command again writing them under ${OUTPUT_DIR_ENV}."
                    ),
                    "operation": {"tool": "bash", "paths": outside},
                }

        if refusal is None:
            conflicts = await _check_bases(root, manifest, changes)
            if conflicts:
                refusal = {
                    "error": "file_changed_since_staged",
                    "reason": (
                        "These files changed in Manor while the command ran, so its edits were not "
                        f"saved: {', '.join(conflicts[:10])}. Run the command again."
                    ),
                }

        guard_paths = sorted(changes.guarded_paths())
        unapproved = _paths_beyond_receipt(entity_id, guard_paths) if refusal is None else []
        if unapproved:
            # The approved call named other paths than these. That is what
            # this command did, not a platform fault: say so, so the model
            # can write them with a command that names them.
            refusal = _unapproved_changes_refusal(unapproved)
        elif refusal is None and pre_authorized_paths is not None:
            if not _guard_paths_cover(pre_authorized_paths, changes):
                refusal = _unapproved_changes_refusal(guard_paths)
        elif refusal is None:
            blocked = await guard_ai_file_mutation(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                conversation_id=runtime_context.conversation_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                runtime_envelope=runtime_context.runtime_envelope,
                tool_name="bash",
                action=changes.action(),
                paths=guard_paths,
                approval_token=approval_token,
                content_preview={"command": command, "paths": guard_paths},
            )
            if blocked:
                refusal = json.loads(blocked)
            else:
                acl = await _change_acl_error(
                    entity_id=entity_id,
                    user_id=runtime_context.user_id,
                    workspace_id=runtime_context.workspace_id,
                    entity_root=root,
                    actions=_change_actions(changes),
                )
                if acl:
                    refusal = json.loads(acl)

        if refusal is not None:
            await _revert_working_copy(mirror=mirror, manifest=manifest, entity_root=root, changes=changes)
            await bash_mirror.save_manifest(manifest)
            refusal.setdefault("file_changes", changes.summary())
            refusal["file_changes_saved"] = False
            return {"__override__": refusal}

        max_bytes = max(1, int(get_settings().BASH_SANDBOX_STAGE_MAX_BYTES))
        if sum(snapshot.files[rel].size for rel in [*changes.created, *changes.modified]) > max_bytes:
            await _revert_working_copy(mirror=mirror, manifest=manifest, entity_root=root, changes=changes)
            await bash_mirror.save_manifest(manifest)
            return {"__override__": {
                "error": "bash_changes_too_large",
                "reason": f"Files written under /workspace exceed {max_bytes} bytes; save large results with sandbox action='save_result'.",
            }}
        contents = await mirror.collect([*changes.created, *changes.modified])
        saved = await _persist_changes(
            entity_id=entity_id,
            root=root,
            runtime_context=runtime_context,
            changes=changes,
            contents=contents,
            manifest=manifest,
            projection_command_ops=projection_command_ops,
        )
        operation_paths = [path for op in changes.operations for path in op["paths"]]
        await _record_saved_changes(
            manifest=manifest, snapshot=snapshot, root=root, changes=changes,
            contents=contents, mirror=mirror, operation_paths=operation_paths,
        )
        await bash_mirror.save_manifest(manifest)
        return saved | {"file_changes": changes.summary()}


@asynccontextmanager
async def _entity_mutation_section(entity_id: str, root: str):
    """Serialize entity filesystem + Knowledge mutations across processes and pods."""

    from sqlalchemy import text

    from packages.core.database import async_session
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock

    async with entity_filesystem_mutation_lock(root):
        async with async_session() as lock_db:
            async with lock_db.begin():
                await lock_db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"bash-projection:{entity_id}"},
                )
                yield


async def _persist_changes(
    *,
    entity_id: str,
    root: str,
    runtime_context: Any,
    changes: _ChangeSet,
    contents: dict[str, bytes],
    manifest: MirrorManifest,
    projection_command_ops: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write approved changes and their Knowledge projection, durably recoverable."""

    from packages.core.services.entity_fs import finish_entity_filesystem_mutation
    from packages.core.services.knowledge_visibility import normalize_rel_path

    recovery_paths = [
        normalize_rel_path(path) for path in changes.file_paths() if normalize_rel_path(path)
    ]

    async def operation() -> dict[str, Any]:
        _write_projection_recovery_marker(
            root, entity_id=entity_id, user_id=runtime_context.user_id,
            paths=recovery_paths, operations=[],
            conversation_id=runtime_context.conversation_id, task_id=runtime_context.task_id,
        )
        queued_ops, _touched = await _apply_queued_operations(
            entity_id=entity_id, root=root, operations=changes.operations,
        )
        file_ops = await _write_changes_to_entity(
            entity_id=entity_id, root=root, changes=changes, contents=contents, manifest=manifest,
            projection_command_ops=projection_command_ops,
        )
        copy_ops = [
            op for op in projection_command_ops
            if op["kind"] == "copy" and op["destination"] in changes.created
        ]
        operations = [*queued_ops, *file_ops, *copy_ops]
        _write_projection_recovery_marker(
            root, entity_id=entity_id, user_id=runtime_context.user_id,
            paths=recovery_paths, operations=operations,
            conversation_id=runtime_context.conversation_id, task_id=runtime_context.task_id,
        )
        try:
            await _apply_projection_operations(entity_id=entity_id, entity_root=root, operations=operations)
            synced = await _repair_projection_paths(
                entity_id=entity_id, entity_root=root, paths=recovery_paths, user_id=runtime_context.user_id,
                conversation_id=runtime_context.conversation_id, task_id=runtime_context.task_id,
            )
        except Exception as exc:
            logger.error("Bash filesystem projection failed", exc_info=True)
            return {
                "exit_code": 1,
                "error": "knowledge_sync_failed",
                "reason": str(exc),
                "filesystem_changed": True,
                "knowledge_sync_pending": True,
            }
        marker = _projection_recovery_path(root)
        if os.path.isfile(marker):
            os.remove(marker)
        return {"synced_files": synced}

    async def locked() -> dict[str, Any]:
        async with _entity_mutation_section(entity_id, root):
            return await operation()

    try:
        return await finish_entity_filesystem_mutation(locked())
    except FileExistsError as exc:
        return {
            "exit_code": 1,
            "error": "file_destination_exists",
            "reason": f"{exc.args[0] if exc.args else exc} already exists; nothing after it was saved.",
            "knowledge_sync_pending": True,
        }
    except (OSError, ValueError) as exc:
        logger.error("Bash could not save working-copy changes", exc_info=True)
        return {
            "exit_code": 1,
            "error": "bash_save_failed",
            "reason": str(exc),
            "knowledge_sync_pending": True,
        }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _entity_root_for(entity_id: str) -> str:
    if not entity_id:
        return ""
    from packages.core.ai.runtime.file_actions import runtime_entity_file_root

    try:
        root = runtime_entity_file_root(entity_id)
    except Exception:  # noqa: BLE001 - an unsafe root is treated as unavailable
        return ""
    return os.path.realpath(root) if root and os.path.isdir(root) else ""


def _is_external_principal(kwargs: dict[str, Any]) -> bool:
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_is_external_customer

    try:
        return runtime_tool_call_context_is_external_customer(kwargs)
    except Exception:  # noqa: BLE001 - conflicting scope is never internal
        return True


def _surface_of(runtime_context: Any) -> str | None:
    surface = getattr(runtime_context.runtime_envelope, "surface", None)
    value = getattr(surface, "value", surface)
    return str(value) if value else None


def _precise_pre_authorization(command: str) -> list[str] | None:
    """Mutation paths named exactly by a single file command, checked before it runs."""

    command = BashCommandSpecFactory.without_manor_cli(command)
    segments = _shell_command_segments(command)
    if len(segments) != 1 or not segments[0]:
        return None
    if segments[0][0].rsplit("/", 1)[-1] not in BashCommandSpecFactory.LOCAL_ONLY_COMMANDS:
        return None
    mapped = (BashCommandSpecFactory.entity_target(path) for path in _visible_mutation_paths(command))
    paths = list(dict.fromkeys(path for path in mapped if path))  # /tmp etc. is sandbox scratch
    if not paths or any(
        path in {".", "./", "/"} or glob.has_magic(path) or path.startswith("~") or "$" in path
        for path in paths
    ):
        return None
    return paths


def _not_executed(payload: dict[str, Any] | str, reason: str) -> str:
    """A refusal before the command ran: host proof that nothing changed.

    Minted only at the branches of ``_bash`` that return before anything is
    staged, marked or started on a sandbox. A command that ran and failed is
    not proof: it ran in the user's shared, persistent sandbox (with network
    and staged copies of their files), so its failure stays an unconfirmed
    effect.
    """
    from packages.core.ai.runtime.tool_results import RuntimeSettledToolResult

    text = payload if isinstance(payload, str) else json.dumps(payload)
    return RuntimeSettledToolResult.not_applied(text, reason)


def _failed_without_manor_change(result: dict[str, Any]) -> str:
    """A command that ran and failed without changing Manor: host proof of no effect.

    Every Manor change a bash command makes goes through its save-back: files
    written under /workspace, and ``manor`` CLI file operations, which the CLI
    only queues for it. A failed command that saved and queued nothing changed
    nothing in Manor, so discovery commands that fail (``ls`` of a missing
    file, an import check) never leave an unconfirmed effect. What a failed
    command did to outside services over the sandbox's network is not
    tracked, as for any command.
    """
    from packages.core.ai.runtime.tool_results import RuntimeSettledToolResult

    return RuntimeSettledToolResult.not_applied(json.dumps(result), "no_manor_change")


def _command_failed(result: dict[str, Any]) -> bool:
    return result.get("exit_code") != 0 or bool(result.get("timed_out"))


async def _bash(entity_id: str, **kwargs: Any) -> str:
    """Run one shell command in the caller's sandbox."""

    runtime_context = runtime_tool_call_context_from_handler(
        kwargs, user_id=str(kwargs.get("user_id") or "") or None,
    )
    command = str(kwargs.get("command") or "")
    try:
        timeout = max(1, min(int(kwargs.get("timeout") or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    approval_token = str(kwargs.get("approval_token") or "").strip() or None

    error = _validate_command(command)
    if error:
        return _not_executed({"error": error, "command_executed": False}, "command_policy")
    if _is_external_principal(kwargs):
        return _not_executed({
            "error": "bash_unavailable",
            "reason": "bash runs in a sandbox, which external principals cannot use.",
            "command_executed": False,
        }, "principal_refused")
    from packages.core.ai.runtime.bash_sandbox import runtime_sandbox_available

    if not entity_id:
        return _not_executed({
            "error": "sandbox_required",
            "reason": "bash runs in the entity's sandbox and needs an entity context.",
            "command_executed": False,
        }, "sandbox_required")
    if not runtime_sandbox_available():
        return _not_executed({
            "error": "sandbox_required",
            "reason": "bash runs every command in a sandbox, and no sandbox service is configured.",
            "command_executed": False,
        }, "sandbox_required")

    entity_root = _entity_root_for(entity_id)
    if entity_root and os.path.exists(_projection_recovery_path(entity_root)):
        from packages.core.services.entity_fs import entity_filesystem_mutation_lock

        async with entity_filesystem_mutation_lock(entity_root):
            repair_error = await _repair_pending_projection_for_bash(entity_id, entity_root)
        if repair_error:
            return repair_error

    parked = await _parked_bash_command(runtime_context)
    if parked is not None:
        # This call parked while its command waited for a slot: collect that
        # command. Nothing is authorized, staged or started again.
        return await _resume_parked_command(
            entity_id=entity_id,
            entity_root=entity_root,
            runtime_context=runtime_context,
            parked=parked,
            approval_token=approval_token,
        )

    pre_authorized: list[str] | None = None
    projection_command_ops: list[dict[str, Any]] = []
    output_dirs: tuple[str, ...] = ()
    artifact_base_dir = ""
    if entity_root:
        may_write = bool(_visible_mutation_paths(command) or _may_create_files(command))
        try:
            # Any program may write files, so a Workspace run always learns
            # its output folder; only a command that visibly writes is
            # refused when the folder is unavailable.
            artifact_base_dir = await _workspace_bash_artifact_base(
                entity_id=entity_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                workflow_run_id=runtime_context.workflow_run_id,
                workflow_lineage_root_run_id=runtime_context.workflow_lineage_root_run_id,
                entity_root=entity_root,
            )
        except ValueError as exc:
            if may_write:
                return _not_executed({
                    "error": "bash_output_scope_invalid",
                    "reason": str(exc),
                    "command_executed": False,
                }, "output_scope_invalid")
        except Exception:
            if may_write:
                raise
            logger.warning("[bash] Workspace output folder unavailable for a read command", exc_info=True)
        if may_write or artifact_base_dir:
            # Same scoping the approval classifier and receipts apply.
            try:
                scoped = scope_bash_output_command(
                    command, artifact_base_dir=artifact_base_dir, entity_root=entity_root,
                )
            except BashOutputPathError as exc:
                return _not_executed({
                    "error": "bash_output_scope_invalid",
                    "reason": str(exc),
                    "command_executed": False,
                }, "output_scope_invalid")
            command = scoped.command
            if artifact_base_dir:
                output_dirs = tuple(
                    posixpath.dirname(path) for path in scoped.output_paths
                    if path.startswith(artifact_base_dir.rstrip("/") + "/") and posixpath.dirname(path)
                )
        if artifact_base_dir:
            output_dirs = tuple(dict.fromkeys((workspace_output_dir(artifact_base_dir), *output_dirs)))
        pre_authorized = _precise_pre_authorization(command)
        if pre_authorized is not None:
            # A file command names every path it changes: check it before it
            # runs, exactly as the local bash did.
            from packages.core.services.ai_file_permissions import guard_ai_file_mutation

            file_command = BashCommandSpecFactory.without_manor_cli(command)
            blocked = await guard_ai_file_mutation(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                conversation_id=runtime_context.conversation_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                runtime_envelope=runtime_context.runtime_envelope,
                tool_name="bash",
                action=_bash_mutation_action(file_command),
                paths=pre_authorized,
                approval_token=approval_token,
                content_preview={"command": command, "paths": pre_authorized},
            )
            if blocked:
                return blocked
            access_error = await _bash_resource_mutation_error(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                workspace_id=runtime_context.workspace_id,
                cwd=entity_root,
                command=file_command,
                paths=pre_authorized,
            )
            if access_error:
                # Path/ACL checks only: nothing has been staged, marked or run.
                return _not_executed(access_error, "mutation_refused")
            try:
                projection_command_ops = await _projection_operations(
                    command=command, entity_id=entity_id, entity_root=entity_root, cwd=entity_root,
                )
            except Exception:  # noqa: BLE001 - copy projection falls back to a plain sync
                projection_command_ops = []

    return await _run_on_users_sandbox(
        entity_id=entity_id,
        entity_root=entity_root,
        runtime_context=runtime_context,
        command=command,
        timeout=timeout,
        approval_token=approval_token,
        pre_authorized=pre_authorized,
        projection_command_ops=projection_command_ops,
        output_dirs=output_dirs,
        artifact_base_dir=artifact_base_dir,
    )


async def _run_on_users_sandbox(
    *,
    entity_id: str,
    entity_root: str,
    runtime_context: Any,
    command: str,
    timeout: int,
    approval_token: str | None,
    pre_authorized: list[str] | None,
    projection_command_ops: list[dict[str, Any]],
    output_dirs: tuple[str, ...],
    artifact_base_dir: str = "",
) -> Any:
    """Run an authorized command on the user's sandbox, acquiring it as needed.

    A command that never started on a sandbox that vanished (reclaimed, or
    its container lost while the command waited for a slot) runs on the
    user's new sandbox, staged afresh into its work area -- no request is
    lost. One that was running when its sandbox was lost is reported, never
    run again (``_run_in_sandbox``).
    """

    for attempt in range(2):
        sandbox_id, outcome = await _bash_sandbox(entity_id, runtime_context)
        if sandbox_id is None:
            return outcome
        outdated = await _bash_runner_outdated(sandbox_id)
        if outdated is not None:
            return outdated
        try:
            return await _run_in_sandbox(
                entity_id=entity_id,
                entity_root=entity_root,
                runtime_context=runtime_context,
                command=command,
                timeout=timeout,
                approval_token=approval_token,
                sandbox_id=sandbox_id,
                pre_authorized=pre_authorized,
                projection_command_ops=projection_command_ops,
                output_dirs=output_dirs,
                artifact_base_dir=artifact_base_dir,
            )
        except _SandboxVanished:
            await _forget_bash_sandbox(entity_id, runtime_context, sandbox_id)
            if attempt:
                break
    return json.dumps({
        "error": "sandbox_unavailable",
        "reason": "The sandbox disappeared before the command could start; retry it.",
        "command_executed": False,
    })


#: What bash in the sandbox needs from its runner: its work area and the
#: working copy staged there over the side channel, a started command (it
#: may wait for a slot) and that command's own env (the manor CLI token).
_BASH_RUNNER_CAPABILITIES = (
    runner_caps.WORK_AREAS,
    runner_caps.EXEC_SIDE_CHANNEL,
    runner_caps.EXEC_START,
    runner_caps.EXEC_START_ENV,
)


async def _bash_runner_outdated(sandbox_id: str) -> str | None:
    """A refusal naming the runner as outdated when it cannot run bash; None when it can.

    Checked before anything is staged or started: an older runner would fail
    the staging or the command in a way that reads like the command's own
    fault (or like a vanished sandbox, and drop the binding).
    """

    from packages.core.services.sandbox_session_service import runner_capabilities_for_sandbox

    capabilities = await runner_capabilities_for_sandbox(sandbox_id)
    missing = capabilities.missing(*_BASH_RUNNER_CAPABILITIES)
    if not missing:
        return None
    logger.warning("[bash] sandbox=%s runner lacks %s; refusing", sandbox_id, ", ".join(missing))
    return _not_executed({
        "error": "sandbox_runner_outdated",
        "reason": runner_caps.outdated_message(missing, build_version=capabilities.build_version, what="bash"),
        "command_executed": False,
    }, "sandbox_runner_outdated")


class _SandboxVanished(Exception):
    """The bound sandbox is gone before the command started."""


# The record a parked command is collected from before its first status call.
_PARKED_FIRST = SimpleNamespace(status="queued", terminal=False)


async def _parked_bash_command(runtime_context: Any) -> dict[str, Any] | None:
    """The ticket of the command this tool call parked on, if it did.

    Only a durable run parks (``runtime_sandbox_parks_on_slots``); its
    resumed call finds the ticket by run and tool call and collects the
    command named there -- the same execution, never a new one.
    """

    from packages.core.ai.runtime.bash_sandbox import (
        runtime_parked_exec_slot,
        runtime_sandbox_parks_on_slots,
    )

    if not runtime_sandbox_parks_on_slots(runtime_context):
        return None
    parked = await runtime_parked_exec_slot(runtime_context)
    if not parked or not isinstance(parked.get("bash"), dict):
        return None
    return parked


async def _resume_parked_command(
    *,
    entity_id: str,
    entity_root: str,
    runtime_context: Any,
    parked: dict[str, Any],
    approval_token: str | None,
) -> Any:
    """Collect a parked command and save its file changes back, exactly once."""

    state = parked["bash"]
    sandbox_id = str(parked.get("sandbox_id") or "")
    try:
        timeout = int(state.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    pre_authorized = state.get("pre_authorized")
    # The Workspace output folder the command was started with; a ticket
    # parked before it was recorded keeps its original (unchecked) save-back.
    artifact_base_dir = str(state.get("artifact_base_dir") or "")
    try:
        return await _run_in_sandbox(
            entity_id=entity_id,
            entity_root=entity_root,
            runtime_context=runtime_context,
            command=str(state.get("command") or ""),
            timeout=timeout,
            approval_token=approval_token,
            sandbox_id=sandbox_id,
            pre_authorized=list(pre_authorized) if isinstance(pre_authorized, list) else None,
            projection_command_ops=[
                op for op in state.get("projection_command_ops") or [] if isinstance(op, dict)
            ],
            output_dirs=tuple(str(path) for path in state.get("output_dirs") or ()),
            artifact_base_dir=artifact_base_dir,
            parked=parked,
        )
    except _SandboxVanished:
        # The sandbox was lost while the command waited for a slot: it never
        # started. Its queued execution died with the container (the ticket
        # is consumed, its CLI token revoked); run it on the user's new
        # sandbox as a new execution, staged afresh into that sandbox's work
        # area. If that sandbox is full too, the run parks again on the same
        # ticket, now naming the new execution -- it is collected and saved
        # back once, and nothing runs twice.
        from packages.core.ai.runtime.bash_sandbox import runtime_consume_exec_slot_wait
        from packages.core.services.sandbox_cli.tokens import revoke_sandbox_cli_token_digest

        await revoke_sandbox_cli_token_digest(state.get("cli_token_digest"))
        await runtime_consume_exec_slot_wait(runtime_context)
        await _forget_bash_sandbox(entity_id, runtime_context, sandbox_id)
        logger.info("[bash] sandbox %s lost while the command waited for a slot; running it anew", sandbox_id)
    return await _run_on_users_sandbox(
        entity_id=entity_id,
        entity_root=entity_root,
        runtime_context=runtime_context,
        command=str(state.get("command") or ""),
        timeout=timeout,
        approval_token=approval_token,
        pre_authorized=list(pre_authorized) if isinstance(pre_authorized, list) else None,
        projection_command_ops=[
            op for op in state.get("projection_command_ops") or [] if isinstance(op, dict)
        ],
        output_dirs=tuple(str(path) for path in state.get("output_dirs") or ()),
        artifact_base_dir=artifact_base_dir,
    )


def _not_started_payload(queue_wait: int) -> dict[str, Any]:
    return {
        "error": "sandbox_queue_timeout",
        "reason": (
            "The command was not started: every command slot of this user's shared sandbox "
            f"stayed taken for {queue_wait}s by other work. Nothing ran; retry it later."
        ),
        "command_executed": False,
    }


async def _mint_cli_env(
    *,
    entity_id: str,
    sandbox_id: str,
    runtime_context: Any,
    ttl_seconds: int,
) -> tuple[str | None, str | None, dict[str, str]]:
    """``(token digest, CLI exec id, env)`` of one command's manor CLI token, or empties.

    The token reaches the command through its per-exec env only; a parked
    call keeps just the digest (to revoke it) and the exec id (to drain the
    file operations it queued).
    """

    from packages.core.services.sandbox_cli.tokens import (
        SandboxCliTokenError,
        mint_sandbox_cli_token,
        token_digest,
    )

    api_url = _cli_api_url()
    if not api_url:
        return None, None, {}
    try:
        token, claims = await mint_sandbox_cli_token(
            entity_id=entity_id,
            sandbox_id=sandbox_id,
            ttl_seconds=ttl_seconds,
            user_id=runtime_context.user_id,
            agent_id=runtime_context.agent_id,
            conversation_id=runtime_context.conversation_id,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            runtime_run_id=runtime_context.runtime_run_id,
            surface=_surface_of(runtime_context),
        )
    except SandboxCliTokenError as exc:
        logger.warning("[bash] manor CLI token unavailable: %s", exc)
        return None, None, {}
    return token_digest(token), claims.exec_id, {"MANOR_API_URL": api_url, "MANOR_CLI_TOKEN": token}


async def _run_in_sandbox(
    *,
    entity_id: str,
    entity_root: str,
    runtime_context: Any,
    command: str,
    timeout: int,
    approval_token: str | None,
    sandbox_id: str,
    pre_authorized: list[str] | None,
    projection_command_ops: list[dict[str, Any]],
    output_dirs: tuple[str, ...] = (),
    artifact_base_dir: str = "",
    parked: dict[str, Any] | None = None,
) -> Any:
    """Stage, run and save back one command; or park the run while it waits for a slot.

    The command waits its turn in the shared sandbox's fair FIFO like any
    ``sandbox`` exec. A durable run parks meanwhile (``waiting_resource`` /
    ``sandbox_exec_slot``, the generic sandbox's ticket and wake sweep) and
    its resumed call (``parked``: the ticket's payload) collects that same
    execution and saves its changes back. Anything that cannot park waits
    in-process, bounded by ``SANDBOX_EXEC_QUEUE_WAIT_SECONDS``. Staging and
    read-back run on the runner's side channel (``SandboxMirror``): never in
    the slot queue, so never behind this (or any) queued command.
    """

    from packages.core.ai.runtime.bash_sandbox import (
        runtime_await_sandbox_execution as _await_execution,
        runtime_clear_sandbox_active_execution as _clear_runtime_active_execution,
        runtime_consume_exec_slot_wait as _consume_exec_slot_wait,
        runtime_exec_queue_wait_seconds as _exec_queue_wait_seconds,
        runtime_park_on_exec_slot as _park_on_exec_slot,
        runtime_sandbox_client_for as _get_client_for_sandbox,
        runtime_sandbox_execution_id as _runtime_sandbox_execution_id,
        runtime_sandbox_parks_on_slots as _sandbox_parks_on_slots,
        runtime_set_sandbox_active_execution as _set_runtime_active_execution,
        runtime_stop_sandbox_execution as _stop_execution,
    )
    from packages.core.services.sandbox_cli.tokens import (
        SandboxCliTokenError,
        drain_sandbox_cli_operations,
        revoke_sandbox_cli_token_digest,
    )
    from packages.core.services.sandbox_sdk.exceptions import (
        SandboxCapacityError,
        SandboxError,
    )

    try:
        layout = BashSandboxLayout.for_context(runtime_context)
    except MirrorError as exc:
        return json.dumps({"error": "bash_staging_failed", "reason": str(exc), "command_executed": False})
    queue_wait = _exec_queue_wait_seconds()
    queue = {**layout.exec_queue_params(), "queue_timeout": queue_wait}
    try:
        client = await _get_client_for_sandbox(sandbox_id)
    except Exception as exc:  # noqa: BLE001 - a missing route is a reclaimed sandbox
        if _sandbox_is_gone_error(exc):
            raise _SandboxVanished() from exc
        return json.dumps({"error": f"Sandbox service unavailable: {exc}", "command_executed": False})
    mirror = SandboxMirror(client, sandbox_id, layout.location)
    durable = _sandbox_parks_on_slots(runtime_context)
    state = dict((parked or {}).get("bash") or {})
    attempt = 1 if state.get("attempt") else 0
    pending: str | None = str((parked or {}).get("execution_id") or "") or None
    base_execution_id = (
        str(state.get("base_execution_id") or "")
        or _runtime_sandbox_execution_id(runtime_context, sandbox_id)
    )
    token_hash: str | None = str(state.get("cli_token_digest") or "") or None
    exec_id: str | None = str(state.get("cli_exec_id") or "") or None
    epoch = str(state.get("epoch") or "")
    keep_active = False  # parked: the run stays on record as executing this command
    uncertain = False  # the command may or may not have run: keep the ticket to collect it again
    try:
        timed_out = False
        while True:
            first: Any = _PARKED_FIRST
            if pending is None:
                if entity_root:
                    try:
                        refusal, epoch = await _stage_command(
                            entity_id=entity_id,
                            entity_root=entity_root,
                            runtime_context=runtime_context,
                            command=command,
                            mirror=mirror,
                            sandbox_id=sandbox_id,
                            fresh=bool(attempt),
                            output_dirs=output_dirs,
                        )
                    except SandboxError as exc:
                        if _sandbox_is_gone_error(exc):
                            raise _SandboxVanished() from exc
                        return json.dumps({"error": f"Sandbox execution failed: {exc}", "command_executed": False})
                    except MirrorError as exc:
                        return json.dumps({"error": "bash_staging_failed", "reason": str(exc), "command_executed": False})
                    if refusal:
                        return json.dumps(refusal)
                shell = _shell_for(layout, command, epoch=epoch) if entity_root else command
                env = {"MANOR_WORKSPACE": layout.mirror_root, "MANOR_AGENT_HOME": layout.agent_home}
                if entity_root and artifact_base_dir:
                    # Staged with ``output_dirs``: the folder exists in the copy.
                    env[OUTPUT_DIR_ENV] = posixpath.join(layout.mirror_root, workspace_output_dir(artifact_base_dir))
                token_hash, exec_id, cli_env = await _mint_cli_env(
                    entity_id=entity_id,
                    sandbox_id=sandbox_id,
                    runtime_context=runtime_context,
                    ttl_seconds=queue_wait + timeout + 60,
                )
                env.update(cli_env)
                pending = base_execution_id if not attempt else f"{base_execution_id}-r"
                await _set_runtime_active_execution(runtime_context, sandbox_id=sandbox_id, execution_id=pending)
                try:
                    first = await client.start_exec(
                        sandbox_id=sandbox_id,
                        command=shell,
                        timeout=timeout,
                        workdir="/tmp",  # nosec B108 -- inside the sandbox; the shell cds to layout.cwd
                        execution_id=pending,
                        env=env,
                        **queue,
                    )
                except SandboxCapacityError as exc:
                    return json.dumps({
                        "error": "Sandbox capacity is full. Please retry later.",
                        "details": str(exc),
                        "command_executed": False,
                    })
                except SandboxError as exc:
                    if _sandbox_is_gone_error(exc):
                        raise _SandboxVanished() from exc
                    return json.dumps({"error": f"Sandbox execution failed: {exc}", "command_executed": False})
                waiting_for_slot = first.status == "queued" and getattr(first, "queue_position", None)
                if waiting_for_slot and durable:
                    suspension = await _park_on_exec_slot(
                        runtime_context,
                        sandbox_id=sandbox_id,
                        execution_id=pending,
                        wait_seconds=queue_wait,
                        continuation={"bash": {
                            "attempt": attempt,
                            "base_execution_id": base_execution_id,
                            "epoch": epoch,
                            "command": command,
                            "timeout": timeout,
                            "pre_authorized": pre_authorized,
                            "projection_command_ops": projection_command_ops,
                            "output_dirs": list(output_dirs),
                            "artifact_base_dir": artifact_base_dir,
                            "cli_exec_id": exec_id,
                            "cli_token_digest": token_hash,
                        }},
                    )
                    if suspension is not None:
                        keep_active = True
                        return suspension
            try:
                final, _last_status = await _await_execution(
                    client,
                    sandbox_id=sandbox_id,
                    execution_id=pending,
                    first=first,
                    budget_seconds=queue_wait + timeout + 30,
                    runtime_context=runtime_context,
                )
            except asyncio.TimeoutError:
                # The runner ends a command at its queue budget or its own
                # timeout first; this is the backstop. Stop it either way.
                stopped = await _stop_execution(sandbox_id, pending)
                uncertain = not stopped
                return json.dumps({
                    "error": "bash_timeout",
                    "reason": (
                        f"The command did not finish within its {queue_wait}s slot wait plus its "
                        f"{timeout}s limit and " + ("was stopped." if stopped else "could not be confirmed stopped.")
                        + " Do not automatically repeat a command with side effects."
                    ),
                    "command_executed": None,
                })
            except SandboxError as exc:
                last_status = str(getattr(exc, "_manor_last_status", "") or "queued")
                if _sandbox_is_gone_error(exc) and last_status == "queued":
                    raise _SandboxVanished() from exc
                if _sandbox_is_gone_error(exc):
                    return json.dumps({
                        "error": "sandbox_lost",
                        "reason": "The sandbox was lost while the command ran; it was not retried.",
                        "command_executed": True,
                    })
                uncertain = True
                return json.dumps({"error": f"Sandbox execution failed: {exc}", "command_executed": None})
            except Exception as exc:  # noqa: BLE001 - the runner stayed unreachable
                uncertain = True
                return json.dumps({
                    "error": "sandbox_unreachable",
                    "reason": (
                        f"The sandbox runner did not answer ({type(exc).__name__}). The command "
                        f"(execution {pending}) may or may not have run: check its effects before "
                        "repeating it."
                    ),
                    "command_executed": None,
                })
            outcome = str(getattr(final, "outcome", "") or "")
            detail = str(getattr(final, "outcome_detail", "") or "")
            error = str(getattr(final, "error", "") or "")
            exit_code = getattr(final, "exit_code", None)
            stdout = str(getattr(final, "stdout", None) or "")
            stderr = str(getattr(final, "stderr", None) or "")
            if outcome == "queue_timeout":
                return json.dumps(_not_started_payload(queue_wait))
            if outcome == "sandbox_lost":
                if detail == "never started":
                    raise _SandboxVanished()
                return json.dumps({
                    "error": "sandbox_lost",
                    "reason": "The sandbox was lost while the command ran; it was not retried.",
                    "command_executed": True,
                })
            if exit_code is None:
                if "timed out" in error.lower():
                    timed_out, exit_code = True, -9
                elif str(getattr(final, "status", "")) == "cancelled":
                    return json.dumps({
                        "error": "bash_cancelled",
                        "reason": f"The command was cancelled ({error or 'by a cancel request'}).",
                        "command_executed": None,
                    })
                else:
                    return json.dumps({
                        "error": f"Sandbox execution failed: {detail or error or outcome or 'no exit code'}",
                        "command_executed": None,
                    })
            if not attempt and exit_code == _STALE_COPY_EXIT and _STALE_COPY_SENTINEL in stderr:
                # The container's working copy is not the one the manifest
                # describes: the command did not run; stage afresh and run it.
                attempt, pending = 1, None
                await revoke_sandbox_cli_token_digest(token_hash)
                token_hash = None
                continue
            break

        await revoke_sandbox_cli_token_digest(token_hash)
        token_hash = None
        result: dict[str, Any] = {"exit_code": exit_code, "timed_out": timed_out}
        if outcome:
            result["outcome"] = detail or outcome
        result.update(_stream_output_fields("stdout", stdout))
        result.update(_stream_output_fields("stderr", stderr))

        operations: list[dict[str, Any]] = []
        if exec_id:
            try:
                operations = await drain_sandbox_cli_operations(exec_id)
            except SandboxCliTokenError as exc:
                result["file_changes_note"] = str(exc)
        if not entity_root:
            if operations:
                result["file_changes_note"] = "The entity filesystem is unavailable; manor file operations were not applied."
            if _command_failed(result):
                # No entity filesystem: nothing it did can have reached Manor.
                return _failed_without_manor_change(result)
            return json.dumps(result)
        try:
            saved = await _save_changes(
                entity_id=entity_id,
                entity_root=entity_root,
                runtime_context=runtime_context,
                command=command,
                approval_token=approval_token,
                mirror=mirror,
                sandbox_id=sandbox_id,
                operations=operations,
                pre_authorized_paths=pre_authorized,
                projection_command_ops=projection_command_ops,
                artifact_base_dir=artifact_base_dir,
            )
        except (MirrorError, SandboxError) as exc:
            logger.error("[bash] could not compare the working copy", exc_info=True)
            result.update({
                "file_changes_saved": False,
                "file_changes_note": f"File changes could not be read back from the sandbox: {exc}",
            })
            return json.dumps(result)
        override = saved.pop("__override__", None)
        if override is not None:
            # Approval requests and refusals keep their own shape (the Runtime
            # reads ``__hitl__``), plus what the command printed.
            return json.dumps({
                **override,
                "command_executed": True,
                "exit_code": exit_code,
                "timed_out": timed_out,
                **_stream_output_fields("stdout", stdout),
                **_stream_output_fields("stderr", stderr),
            })
        if saved.get("error"):
            result.update(saved)
            return json.dumps(result)
        if not saved and not operations and _command_failed(result):
            return _failed_without_manor_change(result)
        result.update({key: value for key, value in saved.items() if value not in (None, 0, {}, [])})
        return json.dumps(result)
    finally:
        if not keep_active:
            if token_hash:
                await revoke_sandbox_cli_token_digest(token_hash)
            if pending:
                await _clear_runtime_active_execution(runtime_context, pending)
            if durable and not uncertain:
                # Collected (and saved back) or certainly not run: a re-executed
                # call never collects this command again.
                await _consume_exec_slot_wait(runtime_context)
        await client.close()


_STALE_COPY_EXIT = 97
_STALE_COPY_SENTINEL = "manor: stale working copy"


def _shell_for(layout: BashSandboxLayout, command: str, *, epoch: str) -> str:
    """The user command, run from the layout's cwd only if the working copy is the staged one."""

    epoch_file = f"{layout.location.state}/epoch"
    return (
        f"mkdir -p {_quote(layout.agent_home)} 2>/dev/null; "
        f"[ \"$(cat {_quote(epoch_file)} 2>/dev/null)\" = {_quote(epoch)} ] "
        f"|| {{ echo {_quote(_STALE_COPY_SENTINEL)} >&2; exit {_STALE_COPY_EXIT}; }}; "
        f"cd {_quote(layout.cwd)} || exit 1\n{command}"
    )


def _quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (BASH_SCHEMA, _bash),
    ]
