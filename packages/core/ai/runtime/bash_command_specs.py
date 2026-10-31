"""Shared shell syntax recognized by Bash execution and approval policy."""

from __future__ import annotations

import glob
import os
import posixpath
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BashWriteResource:
    """One entity path a bash command may change.

    ``tree`` marks a directory destination: the command writes children
    under it (``cp a b dir/``, ``cp -r src dir``, ``rm -r dir``).
    """

    path: str
    tree: bool = False


_RECURSIVE_SHORT_FLAGS = frozenset("rRa")
_COPY_TARGET_OPTIONS = frozenset({"-t", "--target-directory"})
_COPY_OPTIONS_WITH_VALUE = frozenset({"-S", "--suffix"})
# Shell operators that open their target file for writing (``2>`` is ``>``
# after its descriptor; ``&>``/``&>>`` redirect stdout and stderr together).
_FILE_REDIRECTIONS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})


def _is_flag(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _recursive(flags: list[str], *, archive: bool) -> bool:
    letters = _RECURSIVE_SHORT_FLAGS if archive else frozenset("rR")
    return any(
        flag in {"--recursive", *({"--archive"} if archive else ())}
        or (not flag.startswith("--") and bool(letters & set(flag[1:])))
        for flag in flags
    )


class BashCommandSpecFactory:
    """Create deterministic permission facts for supported shell syntax."""

    LOCAL_ONLY_COMMANDS = frozenset({
        "mkdir", "cp", "mv", "touch", "chmod", "rm",
        "ls", "cat", "head", "tail", "find", "tree", "file", "wc",
        "grep", "rg", "sort", "uniq", "cut", "tr", "diff",
        "echo", "date", "pwd", "which", "whoami",
    })

    WRITE_REDIRECTION_PATTERN = re.compile(r"(?<![<>=])(?:\d*>>|\d*>\|?)\s*([^\s;&|]+)")

    # Bash runs in its owner's shared sandbox, from the acting agent's working
    # copy of the entity files (``bash_mirror.MirrorLocation``): relative
    # paths are entity paths, and so is an absolute path into a working copy
    # (``/workspace/c/<area>/agents/<agent>/workspace/x``). Any other absolute
    # path (``/tmp/x``, ``/dev/null``, the shared ``/workspace`` tmpfs) is
    # sandbox scratch, never an entity file.

    # ``manor mv/cp/rm`` (and the local ``mkdir/touch/push`` helpers) change
    # entity files exactly like their shell namesakes; approval policy and
    # authorization receipts classify them as those commands.
    MANOR_FILE_VERBS = {"mv": "mv", "cp": "cp", "rm": "rm", "mkdir": "mkdir", "touch": "touch", "push": "cp"}
    _MANOR_FILE_COMMAND = re.compile(r"(^|[;&|]\s*)manor\s+(mv|cp|rm|mkdir|touch|push)(?=\s)")

    @classmethod
    def without_manor_cli(cls, command: str) -> str:
        """The command with ``manor <file verb>`` written as the shell verb it acts as."""

        return cls._MANOR_FILE_COMMAND.sub(
            lambda match: match.group(1) + cls.MANOR_FILE_VERBS[match.group(2)], str(command or ""),
        )

    @classmethod
    def entity_target(cls, path: str) -> str | None:
        """Entity-relative form of a command's file target; None for sandbox scratch."""

        text = str(path or "").strip().strip("'\"")
        if not text:
            return None
        if not text.startswith("/"):
            return text
        from packages.core.ai.runtime.bash_mirror import mirror_rel_path

        rel = mirror_rel_path(text)
        if rel is None:
            return None
        return rel or "."

    @classmethod
    def write_resources(cls, command: str, *, entity_root: str | None = None) -> list[BashWriteResource]:
        """Every entity path one command line may change, segment by segment.

        The authorization receipt of a bash call binds exactly these, and the
        save-back checks each file the command really changed against them, so
        they must name what the shell will produce: a ``cp``/``mv``/``manor
        push`` into a directory writes children of it, ``rm -r`` deletes a
        tree, and every simple command of a chain (``mkdir -p d && cp f d/``)
        contributes its own targets. Sandbox scratch (``/tmp``) is never an
        entity path. ``entity_root`` lets an existing directory destination be
        recognized as one.
        """

        from packages.core.ai.runtime.bash_output_paths import BashOutputPathError, shell_segments

        try:
            segments = shell_segments(cls.without_manor_cli(command))
        except BashOutputPathError:
            return []
        resources: dict[str, BashWriteResource] = {}
        folders: set[str] = set()  # made by an earlier `mkdir` of this command line

        def normalize(raw: str) -> str | None:
            path = cls.entity_target(raw)
            if path is None:
                return None
            return posixpath.normpath(path.replace("\\", "/")).lstrip("/")

        def add(raw: str, *, tree: bool = False) -> None:
            normalized = normalize(raw)
            if normalized is None or normalized in {"", "."} or normalized.startswith(".."):
                return  # scratch, or the entity root, which is never one approvable target
            previous = resources.get(normalized)
            resources[normalized] = BashWriteResource(normalized, tree or bool(previous and previous.tree))

        def is_directory(raw: str) -> bool:
            normalized = normalize(raw)
            if raw.endswith("/") or normalized in {".", ""} or normalized in folders:
                return True
            if not entity_root or normalized is None:
                return False
            return os.path.isdir(os.path.join(entity_root, normalized))

        def into(destination: str, sources: list[str], *, directory: bool, recursive: bool) -> None:
            """``destination``, or what lands in it when it is a directory."""
            if normalize(destination) in {".", ""}:
                # Into the entity root: only the named children, never the whole root.
                for source in sources:
                    name = posixpath.basename(source.rstrip("/"))
                    if name and not glob.has_magic(name) and "$" not in name:
                        add(name, tree=recursive)
                return
            unnamed = any(
                not posixpath.basename(source.rstrip("/"))
                or glob.has_magic(source) or "$" in source
                for source in sources
            )
            add(destination, tree=directory or recursive or unnamed)

        for tokens in segments:
            words: list[str] = []
            index = 0
            while index < len(tokens):
                token = tokens[index]
                if token.operator:
                    target = tokens[index + 1] if index + 1 < len(tokens) else None
                    if (
                        words
                        and words[-1].isdigit()
                        and index
                        and not tokens[index - 1].operator
                        and tokens[index - 1].end == token.start
                    ):
                        words.pop()  # the descriptor of `2>file`
                    if target is not None and not target.operator:
                        if token.value in _FILE_REDIRECTIONS or (
                            # ``>&file`` writes a file; ``>&2`` / ``>&-`` only move descriptors
                            token.value == ">&" and not target.value.isdigit() and target.value != "-"
                        ):
                            add(target.value)
                        index += 2
                        continue
                    index += 1
                    continue
                words.append(token.value)
                index += 1
            if not words:
                continue
            name = words[0].rsplit("/", 1)[-1]
            args = words[1:]
            flags = [arg for arg in args if _is_flag(arg)]
            operands = [arg for arg in args if not _is_flag(arg)]
            if name in {"mkdir", "touch", "tee"}:
                for operand in operands:
                    add(operand)
                    if name == "mkdir" and (folder := normalize(operand)):
                        folders.add(folder)
            elif name == "chmod":
                for operand in operands[1:]:
                    add(operand)
            elif name == "rm":
                for operand in operands:
                    add(operand, tree=_recursive(flags, archive=False))
            elif name == "sed" and any(flag.startswith("-i") or flag == "--in-place" for flag in flags):
                for operand in operands[1:]:
                    add(operand)
            elif name in {"cp", "mv"}:
                target_dir: str | None = None
                plain: list[str] = []
                skip_next = False
                for position, arg in enumerate(args):
                    if skip_next:
                        skip_next = False
                        continue
                    if arg in _COPY_TARGET_OPTIONS and position + 1 < len(args):
                        target_dir = args[position + 1]
                        skip_next = True
                    elif arg.startswith("--target-directory="):
                        target_dir = arg.split("=", 1)[1]
                    elif arg.startswith("-t") and len(arg) > 2 and not arg.startswith("--"):
                        target_dir = arg[2:]
                    elif arg in _COPY_OPTIONS_WITH_VALUE:
                        skip_next = True
                    elif not _is_flag(arg):
                        plain.append(arg)
                if target_dir is None and len(plain) < 2:
                    continue
                destination = target_dir if target_dir is not None else plain[-1]
                sources = plain if target_dir is not None else plain[:-1]
                if name == "mv":
                    # A move changes its sources too; a moved folder, all of it.
                    moved_directory = False
                    for source in sources:
                        source_is_directory = is_directory(source)
                        moved_directory = moved_directory or source_is_directory
                        add(source, tree=source_is_directory)
                    recursive = moved_directory
                else:
                    recursive = _recursive(flags, archive=True)
                if any(flag in {"-T", "--no-target-directory"} for flag in flags):
                    add(destination, tree=recursive)
                    continue
                into(
                    destination,
                    sources,
                    directory=target_dir is not None or len(sources) > 1 or is_directory(destination),
                    recursive=recursive,
                )
        return list(resources.values())

    @classmethod
    def has_write_redirection(cls, command: str) -> bool:
        return bool(cls.write_redirection_targets(command))

    @classmethod
    def write_redirection_targets(cls, command: str) -> list[str]:
        targets = (
            cls.entity_target(match.group(1))
            for match in cls.WRITE_REDIRECTION_PATTERN.finditer(command)
        )
        return list(dict.fromkeys(target for target in targets if target))
