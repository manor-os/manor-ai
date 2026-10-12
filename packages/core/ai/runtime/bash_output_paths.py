"""Deterministically scope explicit Bash output operands."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

from packages.core.ai.runtime.bash_command_specs import BashCommandSpecFactory
from packages.core.services.generated_media_naming import scope_workspace_artifact_path


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


_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})
_OPERATORS = _SEPARATORS | {">", ">>", ">|", "<", "<<"}
_CANONICAL_WORKSPACE_ROOT = "Workspaces/_by_id/"


def _tokens(command: str) -> list[_Token]:
    """Lex only enough shell syntax to replace operand spans without reformatting."""
    result: list[_Token] = []
    index = 0
    while index < len(command):
        if command[index].isspace():
            index += 1
            continue
        if command[index] == "#":
            break
        start = index
        if command[index] in ";|&<>":
            index += 1
            if index < len(command) and command[start:index] + command[index] in _OPERATORS:
                index += 1
            result.append(_Token(command[start:index], start, index, True))
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
        result.append(_Token(value, start, index))
    return result


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


def _segment(tokens: list[_Token], *, artifact_base_dir: str, entity_root: str) -> tuple[dict[tuple[int, int], str], list[str]]:
    if not tokens:
        return {}, []
    command = tokens[0].value.rsplit("/", 1)[-1]
    args = tokens[1:]
    output_indices: set[int] = set()
    has_redirection = False
    for index, token in enumerate(tokens):
        if token.value in {">", ">>", ">|"} and token.operator:
            has_redirection = True
            if token.value == ">|":
                raise BashOutputPathError("Forced Bash redirection is not supported")
            if index + 1 == len(tokens) or tokens[index + 1].operator:
                raise BashOutputPathError("Bash redirection requires an output path")
            previous = tokens[index - 1] if index else None
            if (
                previous and previous.value.isdigit() and previous.end == token.start
                and previous.end - previous.start == len(previous.value)
                and not previous.operator
            ):
                raise BashOutputPathError("Descriptor Bash redirection is not supported")
            output_indices.add(index + 1)

    if command in {"touch", "mkdir"}:
        if has_redirection:
            raise BashOutputPathError(f"{command} with shell redirection is not supported")
        if any(token.value.startswith("-") and token.value not in {"-p", "--parents"} for token in args):
            raise BashOutputPathError(f"Unsupported {command} options for output routing")
        output_indices.update(
            index
            for index, token in enumerate(tokens[1:], 1)
            if not token.value.startswith("-")
        )
    elif command in {"cp", "mv"}:
        if has_redirection:
            raise BashOutputPathError(f"{command} with shell redirection is not supported")
        if any(
            token.value in {"-t", "-T", "--target-directory", "--no-target-directory"}
            or token.value.startswith("--target-directory=")
            for token in args
        ):
            raise BashOutputPathError(f"Unsupported {command} destination syntax")
        operands = [index for index, token in enumerate(tokens[1:], 1) if not token.value.startswith("-")]
        if len(operands) < 2:
            raise BashOutputPathError(f"{command} requires source and destination paths")
        output_indices.add(operands[-1])

    replacements: dict[tuple[int, int], str] = {}
    paths: list[str] = []
    for index in sorted(output_indices):
        token = tokens[index]
        scoped = _scope_path(token.value, artifact_base_dir=artifact_base_dir, entity_root=entity_root)
        replacements[(token.start, token.end)] = shlex.quote(scoped)
        paths.append(scoped)
    return replacements, list(dict.fromkeys(paths))


def scope_bash_output_command(
    command: str,
    *,
    artifact_base_dir: str,
    entity_root: str,
) -> BashOutputScope:
    """Scope exact output operands while preserving every other command byte."""
    if (
        not artifact_base_dir or not command.strip()
        or command.strip().split()[0].rsplit("/", 1)[-1] not in BashCommandSpecFactory.LOCAL_ONLY_COMMANDS
    ):
        return BashOutputScope(command, ())
    replacements: dict[tuple[int, int], str] = {}
    paths: list[str] = []
    current: list[_Token] = []
    for token in _tokens(command) + [_Token(";", len(command), len(command), True)]:
        if token.operator and token.value in _SEPARATORS:
            segment_replacements, segment_paths = _segment(
                current, artifact_base_dir=artifact_base_dir, entity_root=entity_root,
            )
            replacements.update(segment_replacements)
            paths.extend(segment_paths)
            current = []
        else:
            current.append(token)
    if not replacements:
        return BashOutputScope(command, ())
    pieces: list[str] = []
    position = 0
    for (start, end), value in sorted(replacements.items()):
        pieces.extend((command[position:start], value))
        position = end
    pieces.append(command[position:])
    return BashOutputScope("".join(pieces), tuple(dict.fromkeys(paths)))
