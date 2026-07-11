"""Bash tool — execute shell commands via sandbox service or local subprocess.

Commands are scoped to the entity's filesystem directory when available.
Security: blocked patterns and allowed command prefixes are enforced.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a shell command in the entity filesystem when available. "
            "Use for search/read/scripts. Put temporary outputs under "
            "$SANDBOX_OUTPUT_DIR (/tmp/sandbox-output); save user-visible files "
            "with a dedicated save/write tool."
        ),
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

ALLOWED_PREFIXES: list[str] = [
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc",
    "tree", "file", "echo", "date", "pwd", "which", "whoami",
    "curl", "wget", "jq", "python3", "pip", "node", "npm",
    "sort", "uniq", "cut", "awk", "sed", "tr", "diff", "xargs",
    "mkdir", "cp", "mv", "touch", "chmod", "rm",
]

# Commands that must run locally (on the API container with JuiceFS),
# never routed to the sandbox which has no filesystem mount.
_LOCAL_ONLY_CMDS: set[str] = {
    "mkdir", "cp", "mv", "touch", "chmod", "rm",
    "ls", "cat", "head", "tail", "find", "tree", "file", "wc",
    "grep", "rg", "sort", "uniq", "cut", "awk", "sed", "tr", "diff",
    "echo", "date", "pwd", "which", "whoami", "xargs",
}
_SHELL_SEPARATORS = {"&&", "||", ";", "|", "&"}
_MAY_CREATE_FILE_CMDS = {"python3", "node", "npm", "touch", "cp", "mv", "curl", "wget"}

BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*)?r[a-zA-Z]*\s+/(?!\S)"),  # rm -rf / (root)
    re.compile(r"\brm\s+(-[a-zA-Z]*)?r[a-zA-Z]*\s+\.\./"),    # rm -rf ../
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r":\(\)\{"),  # fork bomb
    re.compile(r"\bsudo\b"),
    re.compile(r"\bsu\s"),
]

MAX_OUTPUT = 65536


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


def _segment_validation_error(segment: list[str]) -> str | None:
    if not segment:
        return None

    base_cmd = segment[0].rsplit("/", 1)[-1]
    if base_cmd not in ALLOWED_PREFIXES:
        return (
            f"Command '{base_cmd}' not in allowed list. "
            f"Allowed: {', '.join(sorted(ALLOWED_PREFIXES))}"
        )

    if base_cmd == "xargs":
        target_cmd = _xargs_target_command(segment)
        if target_cmd and target_cmd not in ALLOWED_PREFIXES:
            return (
                f"Command '{target_cmd}' not in allowed list for xargs. "
                f"Allowed: {', '.join(sorted(ALLOWED_PREFIXES))}"
            )

    if base_cmd == "find":
        for idx, token in enumerate(segment):
            if token != "-exec":
                continue
            if idx + 1 >= len(segment):
                return "Invalid find -exec command"
            exec_cmd = segment[idx + 1].rsplit("/", 1)[-1]
            if exec_cmd not in ALLOWED_PREFIXES:
                return (
                    f"Command '{exec_cmd}' not in allowed list for find -exec. "
                    f"Allowed: {', '.join(sorted(ALLOWED_PREFIXES))}"
                )
    return None


def _validate_command(command: str) -> str | None:
    """Return an error message if the command is blocked, else None."""
    stripped = command.strip()
    if not stripped:
        return "Empty command"

    for pattern in BLOCKED_PATTERNS:
        if pattern.search(stripped):
            return "Command blocked by security policy"

    segments = _shell_command_segments(stripped)
    if not segments:
        segments = [[stripped.split()[0]]]
    for segment in segments:
        error = _segment_validation_error(segment)
        if error:
            return error

    return None


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


def _get_entity_cwd(entity_id: str) -> str:
    """Get entity filesystem root, falling back to /tmp."""
    from packages.core.config import get_settings

    settings = get_settings()
    if settings.MANOR_FS_ENABLED and entity_id:
        entity_dir = os.path.join(settings.MANOR_FS_ROOT, entity_id)
        if os.path.isdir(entity_dir):
            return entity_dir
    return "/tmp"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _bash(entity_id: str, **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    command = kwargs.get("command", "")
    timeout = min(int(kwargs.get("timeout") or 30), 120)

    # Validate
    error = _validate_command(command)
    if error:
        return json.dumps({"error": error})

    cwd = _get_entity_cwd(entity_id)
    sandbox_url = os.getenv("SANDBOX_SERVICE_URL", "")
    entity_root = ""
    if entity_id:
        from packages.core.config import get_settings
        settings = get_settings()
        if settings.MANOR_FS_ENABLED:
            entity_root = os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))
    cwd_in_entity_fs = bool(entity_root) and os.path.realpath(cwd) == entity_root
    may_mutate_entity_fs = False

    if cwd_in_entity_fs:
        mutation_paths = _visible_mutation_paths(command)
        may_mutate_entity_fs = bool(mutation_paths) or _may_create_files(command)
        if mutation_paths:
            from packages.core.services.ai_file_permissions import guard_ai_file_mutation
            blocked = await guard_ai_file_mutation(
                entity_id=entity_id,
                user_id=kwargs.get("user_id") or runtime_context.user_id,
                conversation_id=runtime_context.conversation_id,
                tool_name="bash",
                action="shell_modify",
                paths=mutation_paths,
                approval_token=kwargs.get("approval_token"),
                content_preview={"command": command, "paths": mutation_paths},
            )
            if blocked:
                return blocked

    # Route: sandbox for code execution (python3, node, curl, etc.),
    # local for filesystem ops that need JuiceFS access.
    base_cmd = command.strip().split()[0].rsplit("/", 1)[-1]
    ran_against_entity_fs = False
    if sandbox_url and base_cmd not in _LOCAL_ONLY_CMDS:
        result = await _execute_via_sandbox(sandbox_url, command, timeout, cwd)
    elif base_cmd in _LOCAL_ONLY_CMDS:
        result = await _execute_local(command, timeout, cwd)
        ran_against_entity_fs = cwd_in_entity_fs
    else:
        result = await _execute_local(command, timeout, "/tmp")

    # After successful filesystem commands, sync documents in DB
    try:
        data = json.loads(result)
        if data.get("exit_code") == 0 and entity_id and ran_against_entity_fs and may_mutate_entity_fs:
            await _sync_documents_after_bash(command, entity_id, cwd)

            from packages.core.services.knowledge_sync import reconcile_entity_filesystem

            reconciled = await reconcile_entity_filesystem(
                entity_id=entity_id,
                entity_root=entity_root,
                source="bash",
                created_by=kwargs.get("user_id") or runtime_context.user_id or "ai-agent",
                sync_files=True,
                trash_missing=True,
            )
            if reconciled.synced_files:
                data["synced_files"] = reconciled.synced_files
            if reconciled.trashed_missing_documents:
                data["trashed_missing_documents"] = reconciled.trashed_missing_documents
            if reconciled.limited:
                data["sync_limited"] = True
            result = json.dumps(data)
    except Exception as e:
        logger.debug("post-bash sync skipped: %s", e)

    return result


async def _execute_via_sandbox(
    sandbox_url: str, command: str, timeout: int, cwd: str,
) -> str:
    """Execute via the sandbox FastAPI service."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            resp = await client.post(
                f"{sandbox_url.rstrip('/')}/execute",
                json={"command": command, "timeout": timeout, "cwd": cwd},
            )
            # cwd not found in sandbox (no JuiceFS mount) — retry with /tmp
            if resp.status_code == 400 and cwd != "/tmp":
                logger.debug("Sandbox cwd %s unavailable, retrying with /tmp", cwd)
                resp = await client.post(
                    f"{sandbox_url.rstrip('/')}/execute",
                    json={"command": command, "timeout": timeout, "cwd": "/tmp"},
                )
            if resp.status_code == 403:
                return json.dumps({"error": resp.json().get("detail", "Command blocked")})
            resp.raise_for_status()
            data = resp.json()

        result = {
            "exit_code": data.get("exit_code", -1),
            "timed_out": data.get("timed_out", False),
        }
        result.update(_stream_output_fields("stdout", str(data.get("stdout") or "")))
        result.update(_stream_output_fields("stderr", str(data.get("stderr") or "")))
        # Include output files if any were created in sandbox
        if data.get("output_files"):
            result["output_files"] = data["output_files"]
        return json.dumps(result)
    except Exception as e:
        logger.warning("Sandbox service unavailable, falling back to local: %s", e)
        return await _execute_local(command, timeout, "/tmp")


async def _execute_local(command: str, timeout: int, cwd: str) -> str:
    """Execute locally via asyncio subprocess."""
    timed_out = False
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout_bytes, stderr_bytes = await proc.communicate()
    except OSError as e:
        return json.dumps({"error": str(e)})

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")

    result = {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "timed_out": timed_out,
    }
    result.update(_stream_output_fields("stdout", stdout))
    result.update(_stream_output_fields("stderr", stderr))
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Document sync — keep DB in sync after filesystem-mutating bash commands
# ---------------------------------------------------------------------------

_CMD_ARGS_PATTERN = re.compile(
    r"""^\s*(mv|cp|rm|mkdir)\s+(?:-[a-zA-Z]*\s+)*(.+)""", re.DOTALL,
)

_WRITE_REDIRECTION_PATTERN = re.compile(r"(?<![<>=])(?:\d?>>|>>|>)\s*([^\s;&|]+)")


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


async def _sync_documents_after_bash(
    command: str, entity_id: str, cwd: str,
) -> None:
    """After a successful bash command, sync Document records with filesystem.

    Handles:
      mkdir — create DocumentFolder records so user-created folders appear in Knowledge
      mv  — update fs_path for moved/renamed files and directories
      cp  — duplicate Document records for copied files
      rm  — delete Document records for removed files
    """
    stripped = command.strip()
    segments = _split_simple_shell_commands(stripped)
    if not segments:
        return
    if len(segments) > 1:
        for segment in segments:
            await _sync_documents_after_bash(segment, entity_id, cwd)
        return

    base_cmd = stripped.split()[0].rsplit("/", 1)[-1]
    if base_cmd not in ("mv", "cp", "rm", "mkdir"):
        return

    m = _CMD_ARGS_PATTERN.match(stripped)
    if not m:
        return
    cmd = m.group(1)
    args_str = m.group(2)

    import shlex
    try:
        args = shlex.split(args_str)
    except ValueError:
        return

    # Filter out flags
    args = [a for a in args if not a.startswith("-")]
    if not args:
        return

    from packages.core.config import get_settings
    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        return

    entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
    if not os.path.isdir(entity_root):
        return

    def _resolve(p: str) -> str | None:
        full = os.path.realpath(os.path.join(cwd, p))
        root = os.path.realpath(entity_root)
        if os.path.commonpath([root, full]) != root:
            return None
        return os.path.relpath(full, root)

    if cmd == "mkdir":
        await _handle_mkdir(args, entity_id, entity_root, _resolve, cwd)
    elif cmd == "rm":
        # rm file1 file2 ... — mark matching Document records as trashed
        await _handle_rm(args, entity_id, entity_root, _resolve)
    elif cmd in ("mv", "cp"):
        if len(args) < 2:
            return
        await _handle_mv_cp(cmd, args, entity_id, entity_root, _resolve, cwd)


async def _handle_rm(
    args: list[str], entity_id: str, entity_root: str,
    resolve: callable,
) -> None:
    """Soft-delete Document records for removed files."""
    from packages.core.services.knowledge_sync import trash_path

    count = 0
    for rel_path in (resolve(a) for a in args):
        if rel_path and await trash_path(entity_id, rel_path):
            count += 1
    logger.info("Trashed documents for %d rm path(s) in entity %s", count, entity_id)


async def _handle_mkdir(
    args: list[str], entity_id: str, entity_root: str, resolve: callable,
    cwd: str,
) -> None:
    """Mirror mkdir paths into DocumentFolder hierarchy."""
    from packages.core.services.knowledge_sync import ensure_folder_path

    count = 0
    for arg in args:
        rel_path = resolve(arg)
        full_path = os.path.realpath(os.path.join(cwd, arg))
        if rel_path and os.path.isdir(full_path) and await ensure_folder_path(entity_id, rel_path):
            count += 1
    logger.info("Synced %d mkdir path(s) to knowledge folders in entity %s", count, entity_id)


async def _handle_mv_cp(
    cmd: str, args: list[str], entity_id: str, entity_root: str,
    resolve: callable, cwd: str,
) -> None:
    """Handle mv (update fs_path) and cp (duplicate Document record)."""
    from packages.core.services.knowledge_sync import copy_file_projection, move_path

    sources = args[:-1]
    dest = args[-1]

    dest_rel = resolve(dest)
    if not dest_rel:
        return
    dest_abs = os.path.normpath(os.path.join(cwd, dest))

    def _arg_basename(path: str) -> str:
        return os.path.basename(os.path.normpath(path))

    def _dest_arg_forces_directory(path: str) -> bool:
        stripped = path.rstrip()
        return stripped.endswith("/") or stripped.endswith("/.")

    pairs: list[tuple[str, str]] = []  # (old_fs_path, new_fs_path)
    for src in sources:
        old_rel = resolve(src)
        if not old_rel:
            continue
        src_name = _arg_basename(src)
        candidate_abs = os.path.join(dest_abs, src_name) if src_name else dest_abs
        dest_is_dir = (
            len(sources) > 1
            or _dest_arg_forces_directory(dest)
            or (src_name and os.path.exists(candidate_abs))
        )
        new_rel = os.path.join(dest_rel, src_name) if dest_is_dir and src_name else dest_rel
        pairs.append((old_rel, new_rel))

    if not pairs:
        return

    count = 0
    for old_path, new_path in pairs:
        if cmd == "mv":
            changed = await move_path(entity_id, old_path, new_path)
        else:
            changed = await copy_file_projection(entity_id, old_path, new_path)
        if changed:
            count += 1
    logger.info("Synced %d %s operation(s) for entity %s", count, cmd, entity_id)


def _may_create_files(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
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
    if not stripped or re.search(r"(?:^|\s)(?:&&|\|\||;|\|)(?:\s|$)", stripped):
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
        clean = _non_flags(args[1:])
        paths.extend(clean)
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


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (BASH_SCHEMA, _bash),
    ]
