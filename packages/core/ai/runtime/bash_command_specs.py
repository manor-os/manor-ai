"""Shared shell syntax recognized by Bash execution and approval policy."""

from __future__ import annotations

import re


class BashCommandSpecFactory:
    """Create deterministic permission facts for supported shell syntax."""

    LOCAL_ONLY_COMMANDS = frozenset({
        "mkdir", "cp", "mv", "touch", "chmod", "rm",
        "ls", "cat", "head", "tail", "find", "tree", "file", "wc",
        "grep", "rg", "sort", "uniq", "cut", "tr", "diff",
        "echo", "date", "pwd", "which", "whoami",
    })

    WRITE_REDIRECTION_PATTERN = re.compile(r"(?<![<>=])(?:\d*>>|\d*>\|?)\s*([^\s;&|]+)")

    @classmethod
    def has_write_redirection(cls, command: str) -> bool:
        return bool(cls.WRITE_REDIRECTION_PATTERN.search(command))

    @classmethod
    def write_redirection_targets(cls, command: str) -> list[str]:
        return list(
            dict.fromkeys(
                match.group(1).strip("'\"")
                for match in cls.WRITE_REDIRECTION_PATTERN.finditer(command)
                if match.group(1).strip("'\"")
            )
        )
