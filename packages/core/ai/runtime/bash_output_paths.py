"""Deterministically scope explicit Bash output operands."""
from __future__ import annotations

import os
import posixpath
import re
import shlex
from dataclasses import dataclass

from packages.core.ai.runtime.bash_command_specs import BashCommandSpecFactory
from packages.core.services.generated_media_naming import scope_workspace_artifact_path

#: Env var naming the absolute folder a Workspace-scoped command writes new files into.
OUTPUT_DIR_ENV = "MANOR_OUTPUT_DIR"


class BashOutputPathError(ValueError):
    """Raised when a Bash write target cannot be scoped exactly."""


@dataclass(frozen=True)
class BashOutputScope:
    command: str
    output_paths: tuple[str, ...]


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    operator: bool = False
    raw_length: int = 0


_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "|&", "\n"})
_WRITE_REDIRECTIONS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_DUPLICATIONS = frozenset({">&", "<&"})
_HEREDOCS = frozenset({"<<", "<<-"})
_OPERATORS = _SEPARATORS | _WRITE_REDIRECTIONS | _DUPLICATIONS | _HEREDOCS | {"<", "<<<"}
_DIRECTORY_CHANGES = frozenset({"cd", "pushd", "popd"})
_CANONICAL_WORKSPACE_ROOT = "Workspaces/_by_id/"
_OUTPUT_DIR_REFERENCE = re.compile(r"^\$(?:\{" + OUTPUT_DIR_ENV + r"\}|" + OUTPUT_DIR_ENV + r"(?![A-Za-z0-9_]))")


def workspace_output_dir(artifact_base_dir: str) -> str:
    """Entity-relative folder where a Workspace-scoped command's new files belong.

    The folder a bare output name (``> report.txt``) is scoped into, so
    ``$MANOR_OUTPUT_DIR/report.txt`` and ``> report.txt`` name the same file.
    """
    if not artifact_base_dir:
        return ""
    return posixpath.dirname(scope_workspace_artifact_path(
        "_", artifact_base_dir, default_subdir="artifacts",
    ))


def _skip_heredoc_bodies(command: str, index: int, pending: list[tuple[str, bool]]) -> int:
    """Index after the bodies of the here-documents opened on the line that just ended."""
    for delimiter, strip_tabs in pending:
        while index < len(command):
            end = command.find("\n", index)
            line = command[index:] if end < 0 else command[index:end]
            index = len(command) if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    pending.clear()
    return index


def _tokens(command: str) -> list[_Token]:
    """Lex only enough shell syntax to replace operand spans without reformatting.

    Newlines separate commands, comments end at the line end, and the
    bodies of here-documents are skipped: none of them name output paths.
    """
    result: list[_Token] = []
    pending_heredocs: list[tuple[str, bool]] = []
    heredoc_operator: str | None = None
    index = 0
    while index < len(command):
        if command[index] == "\n":
            result.append(_Token("\n", index, index + 1, True))
            index = _skip_heredoc_bodies(command, index + 1, pending_heredocs)
            continue
        if command.startswith("\\\n", index):
            index += 2
            continue
        if command[index].isspace():
            index += 1
            continue
        if command[index] == "#":
            end = command.find("\n", index)
            index = len(command) if end < 0 else end
            continue
        start = index
        if command[index] in ";|&<>":
            index += 1
            while index < len(command) and command[start:index + 1] in _OPERATORS:
                index += 1
            operator = command[start:index]
            heredoc_operator = operator if operator in _HEREDOCS else None
            result.append(_Token(operator, start, index, True))
            continue
        quote = ""
        while index < len(command):
            char = command[index]
            if quote:
                if char == "\\" and quote == '"' and index + 1 < len(command):
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char == "\\" and index + 1 < len(command):
                index += 2
            elif char in "'\"":
                quote = char
                index += 1
            elif char.isspace() or char in ";|&<>":
                break
            else:
                index += 1
        if quote:
            raise BashOutputPathError("Bash output paths require valid shell syntax")
        raw = command[start:index]
        try:
            value = shlex.split(raw)[0]
        except (IndexError, ValueError) as exc:
            raise BashOutputPathError("Bash output paths require valid shell syntax") from exc
        if heredoc_operator is not None:
            pending_heredocs.append((value, heredoc_operator == "<<-"))
            heredoc_operator = None
        result.append(_Token(value, start, index, raw_length=len(raw)))
    return result


def shell_segments(command: str) -> list[list[_Token]]:
    """The simple commands of one command line (split at ``&&``/``||``/``;``/``|``/``|&``/``&``/newline).

    Each token keeps its source span, so a caller can tell ``2>file`` (a
    descriptor redirection) from ``2 > file``. Raises ``BashOutputPathError``
    for invalid shell syntax.
    """
    segments: list[list[_Token]] = []
    current: list[_Token] = []
    for token in _tokens(command):
        if token.operator and token.value in _SEPARATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _is_existing_or_canonical(path: str, *, entity_root: str) -> bool:
    if path.startswith(_CANONICAL_WORKSPACE_ROOT):
        return True
    candidate = path if os.path.isabs(path) else os.path.join(entity_root, path)
    try:
        root = os.path.realpath(entity_root)
        return os.path.commonpath([root, os.path.realpath(candidate)]) == root and os.path.exists(candidate)
    except ValueError:
        return False


def _scope_path(path: str, *, artifact_base_dir: str, entity_root: str) -> str:
    normalized = path.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise BashOutputPathError("Bash output path must be one exact path inside the entity filesystem")
    reference = _OUTPUT_DIR_REFERENCE.match(normalized)
    if reference:
        # ``$MANOR_OUTPUT_DIR/name``: the Workspace output folder, written
        # out so approval and receipts see the exact entity path.
        rest = normalized[reference.end():]
        if (rest and not rest.startswith("/")) or any(char in rest for char in "*?[$`{}~"):
            raise BashOutputPathError(
                f"Bash output path must be one exact path; write to \"${OUTPUT_DIR_ENV}/<name>\""
            )
        return workspace_output_dir(artifact_base_dir) + rest
    if _is_existing_or_canonical(path, entity_root=entity_root):
        return path
    if (
        not normalized
        or os.path.isabs(path)
        or any(char in path for char in "*?[")
        or any(char in path for char in "$`{}")
        or path.startswith("~")
    ):
        raise BashOutputPathError("Bash output path must be one exact path inside the entity filesystem")
    scoped = scope_workspace_artifact_path(
        path,
        artifact_base_dir,
        default_subdir="artifacts",
    )
    return scoped + "/" if path.endswith("/") and not scoped.endswith("/") else scoped


def _is_descriptor(previous: _Token | None, operator: _Token) -> bool:
    """``2`` in ``2> err.txt``: a file-descriptor number glued to its redirection."""
    return (
        previous is not None and not previous.operator and previous.value.isdigit()
        and previous.end == operator.start and previous.raw_length == len(previous.value)
    )


def _segment(
    tokens: list[_Token], *, artifact_base_dir: str, entity_root: str, scope_local: bool,
) -> tuple[dict[tuple[int, int], str], list[str]]:
    """``(operand span replacements, scoped entity output paths)`` of one command.

    In a command line that starts with a local file command
    (``scope_local``), every write redirection and the local file commands'
    destinations are scoped, so ``mkdir -p out && python x.py > out/y`` keeps
    both in one place. In any other line only explicit ``$MANOR_OUTPUT_DIR``
    targets are written out; other targets keep their shell meaning and are
    checked when saved back, so a later read of the same name finds the file.
    """
    if not tokens:
        return {}, []
    command = tokens[0].value.rsplit("/", 1)[-1]
    local_command = scope_local and command in BashCommandSpecFactory.LOCAL_ONLY_COMMANDS
    output_indices: set[int] = set()
    redirection_indices: set[int] = set()
    has_redirection = False
    in_test = False  # ``[[ a > b ]]`` / ``(( a > b ))`` compare, they do not redirect
    for index, token in enumerate(tokens):
        if not token.operator:
            if token.value == "[[" or token.value.startswith("((") or "$((" in token.value:
                in_test = True
            if in_test and (token.value in {"]]", "))"} or token.value.endswith("))")):
                in_test = False
            continue
        if in_test:
            continue
        redirection_indices.add(index)
        if _is_descriptor(tokens[index - 1] if index else None, token):
            redirection_indices.add(index - 1)
        target = tokens[index + 1] if index + 1 < len(tokens) and not tokens[index + 1].operator else None
        if target is None:
            if token.value in _WRITE_REDIRECTIONS | _DUPLICATIONS:
                raise BashOutputPathError("Bash redirection requires an output path")
            continue
        redirection_indices.add(index + 1)
        if any(char in target.value for char in "()`"):
            # Inside ``$(...)`` / backticks the lexer cannot see where the
            # target ends: refuse rather than quote a paren into a path.
            raise BashOutputPathError("Bash redirection inside a command substitution cannot be scoped")
        if token.value in _DUPLICATIONS and (target.value.isdigit() or target.value == "-"):
            continue  # ``2>&1``: descriptor duplication, not a file
        if token.value not in _WRITE_REDIRECTIONS and token.value != ">&":
            continue  # input redirection / here-document delimiter
        if BashCommandSpecFactory.entity_target(target.value) is None:
            continue  # Sandbox scratch (/tmp/x, /dev/null): not an entity file.
        if not scope_local and not _OUTPUT_DIR_REFERENCE.match(target.value):
            continue
        has_redirection = True
        output_indices.add(index + 1)

    args = [
        (index, token) for index, token in enumerate(tokens[1:], 1)
        if index not in redirection_indices
    ]
    if local_command and command in {"touch", "mkdir"}:
        if has_redirection:
            raise BashOutputPathError(f"{command} with shell redirection is not supported")
        if any(token.value.startswith("-") and token.value not in {"-p", "--parents"} for _, token in args):
            raise BashOutputPathError(f"Unsupported {command} options for output routing")
        output_indices.update(index for index, token in args if not token.value.startswith("-"))
    elif local_command and command in {"cp", "mv"}:
        if has_redirection:
            raise BashOutputPathError(f"{command} with shell redirection is not supported")
        if any(
            token.value in {"-t", "-T", "--target-directory", "--no-target-directory"}
            or token.value.startswith("--target-directory=")
            for _, token in args
        ):
            raise BashOutputPathError(f"Unsupported {command} destination syntax")
        operands = [index for index, token in args if not token.value.startswith("-")]
        if len(operands) < 2:
            raise BashOutputPathError(f"{command} requires source and destination paths")
        output_indices.add(operands[-1])

    replacements: dict[tuple[int, int], str] = {}
    paths: list[str] = []
    for index in sorted(output_indices):
        token = tokens[index]
        entity_path = BashCommandSpecFactory.entity_target(token.value)
        if entity_path is None:
            continue  # sandbox scratch is never scoped into a Workspace
        scoped = _scope_path(entity_path, artifact_base_dir=artifact_base_dir, entity_root=entity_root)
        replacements[(token.start, token.end)] = shlex.quote(scoped)
        paths.append(scoped)
    return replacements, list(dict.fromkeys(paths))


def scope_bash_output_command(
    command: str,
    *,
    artifact_base_dir: str,
    entity_root: str,
) -> BashOutputScope:
    """Scope exact output operands while preserving every other command byte.

    Each command of a list or pipeline is scoped on its own (see
    ``_segment``). Scoped paths are relative to the working-copy root, so
    commands after a ``cd`` keep their own meaning. A command line that
    starts with a local file command fails closed on an output it cannot
    scope exactly, as before; any other command line runs unscoped and its
    new files are checked when they are saved back.
    """
    if not artifact_base_dir or not command.strip():
        return BashOutputScope(command, ())
    first_word = command.strip().split()[0].rsplit("/", 1)[-1]
    strict = first_word in BashCommandSpecFactory.LOCAL_ONLY_COMMANDS
    try:
        tokens = _tokens(command)
    except BashOutputPathError:
        if strict:
            raise
        return BashOutputScope(command, ())
    replacements: dict[tuple[int, int], str] = {}
    paths: list[str] = []
    current: list[_Token] = []
    changed_directory = False
    for token in tokens + [_Token(";", len(command), len(command), True)]:
        if not (token.operator and token.value in _SEPARATORS):
            current.append(token)
            continue
        if current and current[0].value.lstrip("(") in _DIRECTORY_CHANGES:
            changed_directory = True
        if not changed_directory:
            try:
                segment_replacements, segment_paths = _segment(
                    current, artifact_base_dir=artifact_base_dir, entity_root=entity_root,
                    scope_local=strict,
                )
            except BashOutputPathError:
                if strict:
                    raise
                segment_replacements, segment_paths = {}, []
            replacements.update(segment_replacements)
            paths.extend(segment_paths)
        current = []
    if not replacements:
        return BashOutputScope(command, ())
    pieces: list[str] = []
    position = 0
    for (start, end), value in sorted(replacements.items()):
        pieces.extend((command[position:start], value))
        position = end
    pieces.append(command[position:])
    return BashOutputScope("".join(pieces), tuple(dict.fromkeys(paths)))
