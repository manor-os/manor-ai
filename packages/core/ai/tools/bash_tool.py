"""Bash tool — execute shell commands via sandbox service or local subprocess.

Commands are scoped to the entity's filesystem directory when available.
Security: blocked patterns and allowed command prefixes are enforced.
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import secrets
from typing import Any

from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.bash_command_specs import BashCommandSpecFactory
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler

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
            "/tmp/sandbox-output; save user-visible files "
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
    "sort", "uniq", "cut", "tr", "diff",
    "mkdir", "cp", "mv", "touch", "chmod", "rm",
]

# Commands that must run locally (on the API container with JuiceFS),
# never routed to the sandbox which has no filesystem mount.
_LOCAL_ONLY_CMDS: set[str] = {
    "mkdir", "cp", "mv", "touch", "chmod", "rm",
    "ls", "cat", "head", "tail", "find", "tree", "file", "wc",
    "grep", "rg", "sort", "uniq", "cut", "tr", "diff",
    "echo", "date", "pwd", "which", "whoami",
}
# Local commands that inspect or mutate paths must never fall back to the API
# container's /tmp when an entity mount is unavailable. Pure informational
# shell builtins remain usable without a mount (unless they use redirection).
_ENTITY_FS_LOCAL_CMDS = _LOCAL_ONLY_CMDS - {
    "echo", "date", "pwd", "which", "whoami",
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
_PROJECTION_RECOVERY_REL = ".ai/bash-projection-recovery.json"


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


def _segment_validation_error(segment: list[str]) -> str | None:
    if not segment:
        return None

    base_cmd = segment[0].rsplit("/", 1)[-1]
    if base_cmd not in ALLOWED_PREFIXES:
        return (
            f"Command '{base_cmd}' not in allowed list. "
            f"Allowed: {', '.join(sorted(ALLOWED_PREFIXES))}"
        )

    if base_cmd in {"echo", "date", "pwd", "which", "whoami"} and any(
        glob.has_magic(token) or token.startswith("~")
        for token in segment[1:]
    ):
        return (
            "Shell path expansion in informational commands is blocked by "
            "permission checking"
        )

    if base_cmd == "xargs":
        target_cmd = _xargs_target_command(segment)
        if target_cmd:
            return "xargs with an explicit command is blocked by security policy"

    if base_cmd == "find":
        if any(token in {"-exec", "-execdir", "-ok", "-okdir"} for token in segment):
            return "find command execution actions are blocked by security policy"
        if any(
            token in {"-delete", "-fprint", "-fprint0", "-fprintf", "-files0-from"}
            or token.startswith("-files0-from=")
            for token in segment
        ):
            return "find write and indirect-input actions are blocked by security policy"
    if base_cmd == "sort" and any(
        token in {"-o", "--output", "--files0-from"}
        or token.startswith(("--output=", "--files0-from="))
        for token in segment[1:]
    ):
        return "sort output and indirect-input options are blocked by security policy"
    if base_cmd == "file" and any(
        token in {"-f", "-m", "-M", "--files-from", "--magic-file"}
        or token.startswith(("--files-from=", "--magic-file="))
        or (
            len(token) > 2
            and not token.startswith("--")
            and token.startswith(("-f", "-m", "-M"))
        )
        for token in segment[1:]
    ):
        return "file indirect-input options are blocked by security policy"
    if base_cmd == "wc" and any(
        token == "--files0-from" or token.startswith("--files0-from=")
        for token in segment[1:]
    ):
        return "wc indirect-input options are blocked by security policy"
    if base_cmd == "diff" and any(
        token in {"-X", "--exclude-from", "--from-file", "--to-file"}
        or token.startswith(("--exclude-from=", "--from-file=", "--to-file="))
        or (len(token) > 2 and token.startswith("-X"))
        for token in segment[1:]
    ):
        return "diff indirect-input options are blocked by security policy"
    if base_cmd in {"mv", "cp"} and _has_advanced_move_destination_option(segment):
        return f"Advanced {base_cmd} destination options are blocked by security policy"
    if base_cmd == "chmod" and any(
        token == "--reference" or token.startswith("--reference=")
        for token in segment[1:]
    ):
        return "chmod reference files are blocked by security policy"
    if base_cmd == "touch" and any(
        token in {"-r", "--reference"}
        or token.startswith(("--reference=", "-r"))
        for token in segment[1:]
    ):
        return "touch reference files are blocked by security policy"
    return None


def _validate_command(command: str) -> str | None:
    """Return an error message if the command is blocked, else None."""
    stripped = command.strip()
    if not stripped:
        return "Empty command"
    # The local executor necessarily invokes a shell for pipes/redirections.
    # Reject every construct that asks that shell to discover or execute a
    # second command which the policy parser cannot authorize independently.
    if any(token in command for token in ("$", "`", "\r", "\n")):
        return "Shell expansion and multi-line commands are blocked by security policy"
    if "<(" in command or ">(" in command:
        return "Process substitution is blocked by security policy"
    if "<<" in command or "<>" in command:
        return "Here-doc and read/write redirections are blocked by security policy"
    if ">&" in command:
        return "Descriptor redirections are blocked by security policy"

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

    # Routing is chosen once for the entire shell expression.  Do not let a
    # local read command pull a code executor into the API container via a
    # later pipeline/chain segment (for example `rg x | python3 ...`).
    route_classes = {
        segment[0].rsplit("/", 1)[-1] in _LOCAL_ONLY_CMDS
        for segment in segments
        if segment
    }
    if len(route_classes) > 1:
        return "Mixing local filesystem and sandbox commands is blocked by security policy"

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


# Local filesystem commands that read file *contents* (as opposed to just
# listing names). Their non-flag arguments are candidate file paths.
_READ_CONTENT_CMDS = {
    "cat", "head", "tail", "wc", "file", "nl", "tac", "od", "xxd",
    "strings", "base64", "less", "more", "sort", "uniq", "cut", "tr",
    "diff", "column", "fold", "fmt", "rev",
}
# Commands whose first non-flag argument is a pattern/script, not a path.
_READ_PATTERN_CMDS = {"grep", "rg", "egrep", "fgrep"}
_READ_REDIRECTION_PATTERN = re.compile(r"(?<![<>])(?:\d?<)\s*([^\s;&|]+)")

_SEARCH_PATTERN_OPTIONS = {"-e", "--regexp"}
_SEARCH_PATTERN_FILE_OPTIONS = {"-f", "--file"}
_SEARCH_VALUE_OPTIONS = {
    "-A", "-B", "-C", "-m",
    "--after-context", "--before-context", "--context", "--encoding",
    "--max-count",
}
_GREP_VALUE_OPTIONS = {
    "-D", "-d",
    "--binary-files", "--directories", "--devices", "--exclude",
    "--exclude-dir", "--include", "--label",
}
_RG_VALUE_OPTIONS = {
    "-g", "-j", "-M", "-r", "-t", "-T",
    "--dfa-size-limit", "--engine", "--glob", "--iglob", "--max-columns",
    "--max-depth", "--max-filesize", "--path-separator", "--regex-size-limit",
    "--replace", "--sort", "--sortr", "--threads", "--type", "--type-add",
    "--type-not",
}
_SEARCH_FLAG_OPTIONS = {
    "-F", "-H", "-I", "-N", "-P", "-S", "-U", "-a", "-b",
    "-c", "-h", "-i", "-l", "-n", "-o", "-p", "-q", "-s", "-u",
    "-v", "-w", "-x", "-z", "-0",
    "--binary", "--byte-offset", "--case-sensitive", "--column", "--count",
    "--count-matches", "--crlf", "--files-with-matches",
    "--files-without-match", "--fixed-strings", "--heading",
    "--ignore-case", "--invert-match", "--json", "--line-buffered",
    "--line-number", "--line-regexp", "--multiline", "--multiline-dotall",
    "--no-filename", "--no-follow", "--no-heading", "--no-ignore",
    "--no-ignore-global", "--no-ignore-parent", "--no-ignore-vcs",
    "--no-line-number", "--no-messages", "--no-require-git", "--null",
    "--null-data", "--one-file-system", "--only-matching", "--passthru",
    "--pretty", "--quiet", "--smart-case", "--stats",
    "--color", "--colour", "--text", "--trim", "--with-filename",
    "--word-regexp",
}
_GREP_FLAG_OPTIONS = {
    "-E", "-G", "-L", "-T", "-V", "-Z",
    "--basic-regexp", "--extended-regexp", "--initial-tab", "--perl-regexp",
    "--unix-byte-offsets",
}
_RG_FLAG_OPTIONS = {"--no-unicode", "--search-zip"}
_SEARCH_METADATA_MODES = {
    "--files", "--help", "--pcre2-version", "--type-list", "--version",
}


def _search_read_paths(base_cmd: str, args: list[str]) -> list[str]:
    """Return every file whose content a supported grep/rg form may read."""
    family = "rg" if base_cmd == "rg" else "grep"
    value_options = _SEARCH_VALUE_OPTIONS | (
        _RG_VALUE_OPTIONS if family == "rg" else _GREP_VALUE_OPTIONS
    )
    flag_options = _SEARCH_FLAG_OPTIONS | (
        _RG_FLAG_OPTIONS if family == "rg" else _GREP_FLAG_OPTIONS
    )
    short_flags = {
        option[1]
        for option in flag_options
        if len(option) == 2 and option.startswith("-")
    }
    positional: list[str] = []
    read_files: list[str] = []
    explicit_pattern = False
    metadata_mode = False
    rg_unrestricted_count = 0
    options_ended = False
    index = 0

    def validate_value(option: str, value: str) -> None:
        if family != "grep":
            return
        if option in {"-d", "--directories"} and value != "skip":
            raise ValueError(
                "grep directory traversal is blocked by permission checking"
            )
        if option in {"-D", "--devices"} and value != "skip":
            raise ValueError(
                "grep device reads are blocked by permission checking"
            )

    while index < len(args):
        arg = args[index]
        if options_ended or not arg.startswith("-"):
            positional.append(arg)
            index += 1
            continue
        if arg == "--":
            options_ended = True
            index += 1
            continue
        if arg in _SEARCH_METADATA_MODES:
            metadata_mode = True
            index += 1
            continue

        option, separator, attached = arg.partition("=")
        if option in _SEARCH_PATTERN_OPTIONS and separator:
            if not attached:
                raise ValueError(f"{base_cmd} requires a pattern after {option}")
            explicit_pattern = True
            index += 1
            continue
        if option in _SEARCH_PATTERN_FILE_OPTIONS and separator:
            if not attached:
                raise ValueError(f"{base_cmd} requires a file after {option}")
            explicit_pattern = True
            read_files.append(attached)
            index += 1
            continue
        if option in value_options and separator:
            if not attached:
                raise ValueError(f"{base_cmd} requires a value after {option}")
            validate_value(option, attached)
            index += 1
            continue
        if option in flag_options and separator:
            index += 1
            continue

        option_kind = None
        if arg in _SEARCH_PATTERN_OPTIONS:
            option_kind = "pattern"
        elif arg in _SEARCH_PATTERN_FILE_OPTIONS:
            option_kind = "pattern_file"
        elif arg in value_options:
            option_kind = "value"
        if option_kind:
            if index + 1 >= len(args):
                raise ValueError(f"{base_cmd} requires a value after {arg}")
            value = args[index + 1]
            if option_kind == "pattern":
                explicit_pattern = True
            elif option_kind == "pattern_file":
                explicit_pattern = True
                read_files.append(value)
            else:
                validate_value(arg, value)
            index += 2
            continue

        attached_option = next(
            (
                candidate
                for candidate in _SEARCH_PATTERN_OPTIONS
                | _SEARCH_PATTERN_FILE_OPTIONS
                | value_options
                if len(candidate) == 2
                and arg.startswith(candidate)
                and len(arg) > len(candidate)
            ),
            None,
        )
        if attached_option:
            if attached_option in _SEARCH_PATTERN_OPTIONS:
                explicit_pattern = True
            elif attached_option in _SEARCH_PATTERN_FILE_OPTIONS:
                explicit_pattern = True
                read_files.append(arg[len(attached_option):])
            else:
                validate_value(attached_option, arg[len(attached_option):])
            index += 1
            continue
        bundled_short_flags = (
            len(arg) > 2
            and not arg.startswith("--")
            and all(char in short_flags for char in arg[1:])
        )
        if arg in flag_options or bundled_short_flags:
            if family == "rg" and not arg.startswith("--"):
                rg_unrestricted_count += arg[1:].count("u")
            if rg_unrestricted_count >= 2:
                raise ValueError(
                    "rg options that include hidden files are blocked by "
                    "permission checking"
                )
            index += 1
            continue
        raise ValueError(f"Unsupported {base_cmd} option for permission checking: {arg}")

    if metadata_mode:
        return read_files + (positional or ["."])
    input_paths = positional if explicit_pattern else positional[1:]
    if family == "rg" and (explicit_pattern or positional) and not input_paths:
        input_paths = ["."]
    return read_files + input_paths


def _metadata_read_paths(base_cmd: str, args: list[str]) -> list[str]:
    """Return bounded metadata roots or reject commands with hidden traversal."""
    if base_cmd in {"find", "tree"}:
        raise ValueError(
            f"{base_cmd} traversal is blocked by permission checking; "
            "use list_files or glob_files"
        )
    if base_cmd != "ls":
        return []

    paths: list[str] = []
    options_ended = False
    for arg in args:
        if options_ended:
            paths.append(arg)
        elif arg == "--":
            options_ended = True
        elif arg.startswith("-"):
            raise ValueError(
                "ls options are blocked by permission checking; use list_files"
            )
        else:
            paths.append(arg)
    return paths or ["."]


def _visible_read_paths(command: str) -> list[str]:
    """Detect paths a supported local bash command reads the contents of.

    Over-extraction is safe: the caller resolves each candidate against the
    ``documents`` table, so flag values / non-document paths never match and are
    never blocked. Covers the common ``cat priv.md`` / ``grep x priv.md`` /
    ``cat priv.md | ...`` forms. Search options with unknown argument semantics
    fail closed instead of being guessed; shell expansions are rejected earlier.
    """
    paths: list[str] = []
    for segment in _shell_command_segments(command):
        if not segment:
            continue
        base_cmd = segment[0].rsplit("/", 1)[-1]
        args = segment[1:]
        non_flags = [a for a in args if a and not a.startswith("-")]
        if base_cmd in _READ_CONTENT_CMDS:
            paths.extend(non_flags)
        elif base_cmd in _READ_PATTERN_CMDS:
            paths.extend(_search_read_paths(base_cmd, args))
        elif base_cmd in {"ls", "find", "tree"}:
            paths.extend(_metadata_read_paths(base_cmd, args))
    for match in _READ_REDIRECTION_PATTERN.finditer(command):
        paths.append(match.group(1).strip("'\""))
    return list(dict.fromkeys(paths))


def _expanded_read_paths(*, entity_root: str, cwd: str, paths: list[str]) -> list[str]:
    """Resolve read globs/directories to exact paths before ACL evaluation."""
    from packages.core.services.knowledge_visibility import (
        is_user_visible_path,
        normalize_rel_path,
    )

    root = os.path.realpath(entity_root)
    resolved: list[str] = []
    for raw_path in paths:
        if raw_path.startswith("~") or "{" in raw_path or "}" in raw_path:
            raise ValueError(
                "Bash content read paths cannot use shell expansion"
            )
        candidates = glob.glob(os.path.join(cwd, raw_path)) if glob.has_magic(raw_path) else [
            os.path.join(cwd, raw_path)
        ]
        for candidate in candidates:
            full_path = os.path.realpath(candidate)
            try:
                if os.path.commonpath([root, full_path]) != root:
                    raise ValueError("Bash content reads must stay inside the entity filesystem")
            except ValueError:
                raise ValueError("Bash content reads must stay inside the entity filesystem")
            rel_path = normalize_rel_path(os.path.relpath(full_path, root))
            if rel_path and not is_user_visible_path(rel_path):
                raise ValueError(
                    "Bash content reads cannot access internal filesystem paths"
                )
            if os.path.isdir(full_path):
                for dirpath, dirnames, filenames in os.walk(full_path):
                    dirnames[:] = [
                        name
                        for name in dirnames
                        if is_user_visible_path(
                            normalize_rel_path(os.path.relpath(os.path.join(dirpath, name), root))
                        )
                    ]
                    for filename in filenames:
                        nested_rel = normalize_rel_path(
                            os.path.relpath(os.path.join(dirpath, filename), root)
                        )
                        if is_user_visible_path(nested_rel):
                            resolved.append(nested_rel)
                continue
            resolved.append(rel_path)
    return list(dict.fromkeys(resolved))


def _get_entity_cwd(entity_id: str) -> str:
    """Get the entity root, or an owner-only host runtime directory."""
    from packages.core.config import get_settings
    from packages.core.services.runtime_paths import private_runtime_dir

    settings = get_settings()
    if settings.MANOR_FS_ENABLED and entity_id:
        entity_dir = os.path.join(settings.MANOR_FS_ROOT, entity_id)
        if os.path.isdir(entity_dir):
            return entity_dir
    return str(private_runtime_dir("bash"))


def _projection_recovery_path(entity_root: str) -> str:
    return os.path.join(entity_root, _PROJECTION_RECOVERY_REL)


def _normalized_projection_paths(
    *,
    entity_root: str,
    cwd: str,
    paths: list[str],
) -> list[str]:
    from packages.core.services.knowledge_visibility import (
        is_user_visible_path,
        normalize_rel_path,
    )

    root = os.path.realpath(entity_root)
    normalized: list[str] = []
    for raw_path in paths:
        full_path = os.path.realpath(os.path.join(cwd, raw_path))
        try:
            if os.path.commonpath([root, full_path]) != root:
                continue
        except ValueError:
            continue
        rel_path = normalize_rel_path(os.path.relpath(full_path, root))
        if (
            rel_path
            and rel_path not in {".", "/"}
            and is_user_visible_path(rel_path)
        ):
            normalized.append(rel_path)
    return list(dict.fromkeys(normalized))


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
) -> int:
    """Make the Knowledge projection match a bounded set of filesystem paths."""
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


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _bash_impl(
    entity_id: str,
    *,
    user_id: str = "",
    _projection_repaired: bool = False,
    **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_handler(
        kwargs,
        user_id=user_id,
    )
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

    command_segments = _shell_command_segments(command)
    requires_entity_fs = any(
        segment
        and segment[0].rsplit("/", 1)[-1] in _ENTITY_FS_LOCAL_CMDS
        for segment in command_segments
    )
    if not cwd_in_entity_fs and (
        requires_entity_fs
        or bool(_READ_REDIRECTION_PATTERN.search(command))
        or bool(_visible_mutation_paths(command))
    ):
        return json.dumps({
            "error": "entity_filesystem_unavailable",
            "reason": (
                "Bash filesystem commands require a mounted entity filesystem."
            ),
            "command_executed": False,
        })

    if cwd_in_entity_fs and not _projection_repaired:
        repair_error = await _repair_pending_projection_for_bash(entity_id, entity_root)
        if repair_error:
            return repair_error

    if cwd_in_entity_fs:
        # Reads: a local `cat`/`grep`/`head` … must not surface a private
        # Knowledge document the caller cannot view. Content-executing commands
        # route to the mountless sandbox, so only these local commands can read
        # entity files. User-less agents fail closed in the same ACL service.
        try:
            read_paths = _expanded_read_paths(
                entity_root=entity_root,
                cwd=cwd,
                paths=_visible_read_paths(command),
            )
        except ValueError as exc:
            return json.dumps({
                "error": "file_permission_denied",
                "reason": str(exc),
                "command_executed": False,
            })
        if read_paths:
            from packages.core.database import async_session
            from packages.core.services.document_access import (
                unreadable_document_paths,
            )

            async with async_session() as _db:
                blocked_reads = await unreadable_document_paths(
                    _db,
                    entity_id=entity_id,
                    rel_paths=read_paths,
                    user_id=runtime_context.user_id,
                    workspace_id=runtime_context.workspace_id,
                    actor_type="agent",
                )
            if blocked_reads:
                return json.dumps({
                    "error": (
                        "Access denied: this command reads a document you do "
                        "not have permission to view "
                        f"({', '.join(sorted(blocked_reads))}). It was not executed."
                    ),
                    "command_executed": False,
                })

        mutation_paths = _visible_mutation_paths(command)
        may_mutate_entity_fs = bool(mutation_paths) or _may_create_files(command)
        if mutation_paths:
            from packages.core.services.ai_file_permissions import guard_ai_file_mutation
            mutation_action = _bash_mutation_action(command)
            blocked = await guard_ai_file_mutation(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                conversation_id=runtime_context.conversation_id,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                runtime_envelope=runtime_context.runtime_envelope,
                tool_name="bash",
                action=mutation_action,
                paths=mutation_paths,
                approval_token=kwargs.get("approval_token"),
                content_preview={"command": command, "paths": mutation_paths},
            )
            if blocked:
                return blocked
            access_error = await _bash_resource_mutation_error(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                workspace_id=runtime_context.workspace_id,
                cwd=cwd,
                command=command,
                paths=mutation_paths,
            )
            if access_error:
                return access_error

    # Route: sandbox for code execution (python3, node, curl, etc.),
    # local for filesystem ops that need JuiceFS access.
    base_cmd = command.strip().split()[0].rsplit("/", 1)[-1]
    recovery_paths: list[str] = []
    recovery_operations: list[dict[str, Any]] = []
    if cwd_in_entity_fs and may_mutate_entity_fs and base_cmd in _LOCAL_ONLY_CMDS:
        recovery_paths = _normalized_projection_paths(
            entity_root=entity_root,
            cwd=cwd,
            paths=mutation_paths,
        )
        if not recovery_paths:
            return json.dumps({
                "error": "knowledge_sync_plan_required",
                "reason": "Filesystem mutations require explicit recoverable paths",
                "command_executed": False,
            })
        try:
            recovery_operations = await _projection_operations(
                command=command,
                entity_id=entity_id,
                entity_root=entity_root,
                cwd=cwd,
            )
            # Write intent before touching disk.  If the process dies during
            # execution, the next invocation still repairs these paths first.
            _write_projection_recovery_marker(
                entity_root,
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                paths=recovery_paths,
                operations=recovery_operations,
            )
        except Exception as exc:
            logger.error("Could not prepare Bash projection recovery", exc_info=True)
            return json.dumps({
                "error": "knowledge_sync_recovery_unavailable",
                "reason": str(exc),
                "command_executed": False,
            })

    async def execute_and_project() -> str:
        ran_against_entity_fs = False
        if sandbox_url and base_cmd not in _LOCAL_ONLY_CMDS:
            result = await _execute_via_sandbox(sandbox_url, command, timeout, cwd)
        elif base_cmd in _LOCAL_ONLY_CMDS:
            result = await _execute_local(command, timeout, cwd)
            ran_against_entity_fs = cwd_in_entity_fs
        else:
            result = json.dumps({
                "error": "sandbox_required",
                "reason": "Executable and network commands require the sandbox service",
                "command_executed": False,
            })

        # After a successful filesystem command, a Knowledge projection failure is
        # still an operation failure. Persist the bounded paths for deterministic
        # retry before another Bash mutation is allowed to run.
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return result
        if (
            data.get("exit_code") == 0
            and entity_id
            and ran_against_entity_fs
            and may_mutate_entity_fs
        ):
            try:
                if recovery_operations:
                    await _apply_projection_operations(
                        entity_id=entity_id,
                        entity_root=entity_root,
                        operations=recovery_operations,
                    )
                else:
                    await _sync_documents_after_bash(
                        command,
                        entity_id,
                        cwd,
                        user_id=runtime_context.user_id,
                    )
                precise_synced = await _repair_projection_paths(
                    entity_id=entity_id,
                    entity_root=entity_root,
                    paths=recovery_paths,
                    user_id=runtime_context.user_id,
                )
                if precise_synced:
                    data["synced_files"] = max(
                        int(data.get("synced_files") or 0),
                        precise_synced,
                    )
                marker_path = _projection_recovery_path(entity_root)
                if os.path.isfile(marker_path):
                    os.remove(marker_path)
            except Exception as exc:
                try:
                    _write_projection_recovery_marker(
                        entity_root,
                        entity_id=entity_id,
                        user_id=runtime_context.user_id,
                        paths=recovery_paths,
                        operations=recovery_operations,
                    )
                except Exception:
                    logger.critical("Could not persist Bash projection recovery", exc_info=True)
                logger.error("Bash filesystem projection failed", exc_info=True)
                data.update({
                    "exit_code": 1,
                    "error": "knowledge_sync_failed",
                    "reason": str(exc),
                    "filesystem_changed": True,
                    "knowledge_sync_pending": True,
                })
        elif entity_id and ran_against_entity_fs and may_mutate_entity_fs:
            # Shell commands can partially mutate files before returning non-zero.
            # Keep the pre-written marker so the next invocation repairs reality.
            data["filesystem_may_have_changed"] = True
            data["knowledge_sync_pending"] = True

        return json.dumps(data)

    operation = execute_and_project()
    if cwd_in_entity_fs and may_mutate_entity_fs and base_cmd in _LOCAL_ONLY_CMDS:
        from packages.core.services.entity_fs import finish_entity_filesystem_mutation

        return await finish_entity_filesystem_mutation(operation)
    return await operation


async def _bash(entity_id: str, **kwargs: Any) -> str:
    """Serialize entity filesystem mutations across conversations and API pods."""
    command = str(kwargs.get("command") or "")
    error = _validate_command(command)
    if error:
        return json.dumps({"error": error})

    cwd = _get_entity_cwd(entity_id)
    entity_root = ""
    if entity_id:
        from packages.core.config import get_settings

        settings = get_settings()
        if settings.MANOR_FS_ENABLED:
            entity_root = os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))
    if not entity_root or os.path.realpath(cwd) != entity_root:
        return await _bash_impl(entity_id, **kwargs)

    base_cmd = command.strip().split()[0].rsplit("/", 1)[-1]
    mutates_local_entity_fs = (
        base_cmd in _LOCAL_ONLY_CMDS
        and (bool(_visible_mutation_paths(command)) or _may_create_files(command))
    )

    from sqlalchemy import text

    from packages.core.database import async_session
    from packages.core.services.entity_fs import (
        entity_filesystem_mutation_lock,
        entity_filesystem_read_lock,
    )

    try:
        if mutates_local_entity_fs:
            async with entity_filesystem_mutation_lock(entity_root):
                async with async_session() as lock_db:
                    async with lock_db.begin():
                        await lock_db.execute(
                            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                            {"lock_key": f"bash-projection:{entity_id}"},
                        )
                        return await _bash_impl(entity_id, **kwargs)

        while True:
            async with entity_filesystem_read_lock(entity_root):
                if not os.path.exists(_projection_recovery_path(entity_root)):
                    return await _bash_impl(
                        entity_id,
                        _projection_repaired=True,
                        **kwargs,
                    )
            async with entity_filesystem_mutation_lock(entity_root):
                async with async_session() as lock_db:
                    async with lock_db.begin():
                        await lock_db.execute(
                            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                            {"lock_key": f"bash-projection:{entity_id}"},
                        )
                        repair_error = await _repair_pending_projection_for_bash(
                            entity_id,
                            entity_root,
                        )
                        if repair_error:
                            return repair_error
    except Exception as exc:
        logger.error("Could not acquire Bash entity filesystem lock", exc_info=True)
        return json.dumps({
            "error": "filesystem_lock_unavailable",
            "reason": str(exc),
            "command_executed": False,
        })


async def _execute_via_sandbox(
    sandbox_url: str, command: str, timeout: int, cwd: str,
) -> str:
    """Execute via the sandbox FastAPI service."""
    sandbox_id: str | None = None
    client = None
    try:
        from packages.core.config import get_settings
        from packages.core.services.sandbox_sdk import SandboxClient
        from packages.core.services.sandbox_sdk.exceptions import SandboxCapacityError, SandboxError

        settings = get_settings()
        client = SandboxClient(
            base_url=sandbox_url,
            timeout=float(timeout) + 60.0,
            api_token=settings.SANDBOX_API_TOKEN,
        )
        created = await client.create_from_files(
            skill_name="bash-tool",
            files={
                "SKILL.md": (
                    "# Bash Tool Sandbox\n"
                    "Ephemeral sandbox for a single Manor bash tool command.\n"
                )
            },
            env={},
            allowed_sensitive_keys=[],
            auto_install=False,
            config={
                "network": "none",
                "memory": "512m",
                "cpus": 1.0,
                "pids_limit": 128,
            },
        )
        sandbox_id = created.sandbox_id
        from packages.core.services.runtime_paths import private_runtime_dir
        sandbox_workdir = (
            "/tmp"  # nosec B108 -- path is inside the ephemeral sandbox container
            if cwd == str(private_runtime_dir("bash"))
            else created.workdir
        )
        executed = await client.exec(
            sandbox_id=sandbox_id,
            command=command,
            timeout=timeout,
            workdir=sandbox_workdir,
        )
        result = {
            "exit_code": executed.exit_code,
            "timed_out": False,
        }
        result.update(_stream_output_fields("stdout", executed.stdout or ""))
        result.update(_stream_output_fields("stderr", executed.stderr or ""))
        return json.dumps(result)
    except SandboxCapacityError as exc:
        return json.dumps({
            "error": "Sandbox capacity is full. Please retry later.",
            "details": str(exc),
        })
    except SandboxError as exc:
        return json.dumps({"error": f"Sandbox execution failed: {exc}"})
    except Exception as e:
        logger.warning("Sandbox service unavailable: %s", e)
        return json.dumps({"error": f"Sandbox service unavailable: {e}"})
    finally:
        if client is not None:
            try:
                if sandbox_id:
                    try:
                        await client.destroy(sandbox_id)
                    except Exception:
                        logger.warning(
                            "bash tool sandbox cleanup failed sandbox_id=%s",
                            sandbox_id,
                            exc_info=True,
                        )
            finally:
                await client.close()


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

_WRITE_REDIRECTION_PATTERN = BashCommandSpecFactory.WRITE_REDIRECTION_PATTERN


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
    command: str,
    entity_id: str,
    cwd: str,
    *,
    user_id: str | None = None,
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
            await _sync_documents_after_bash(
                segment,
                entity_id,
                cwd,
                user_id=user_id,
            )
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
        await _handle_mkdir(
            args,
            entity_id,
            entity_root,
            _resolve,
            cwd,
            owner_id=user_id,
        )
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
    *,
    owner_id: str | None = None,
) -> None:
    """Mirror mkdir paths into DocumentFolder hierarchy."""
    from packages.core.services.knowledge_sync import ensure_folder_path

    count = 0
    for arg in args:
        rel_path = resolve(arg)
        full_path = os.path.realpath(os.path.join(cwd, arg))
        if (
            rel_path
            and os.path.isdir(full_path)
            and await ensure_folder_path(entity_id, rel_path, owner_id=owner_id)
        ):
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
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (BASH_SCHEMA, _bash),
    ]
