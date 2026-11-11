"""Limits on what a workflow step may keep in run variables.

Every step output is stored in the run's variables and step results, and every
App read loads them. File contents therefore never belong there: an image read
inline arrives as a ``data:...;base64,`` URL of several megabytes, and a run
that stored one could make each later read of the Workspace slow. Such a value
is refused with a message that says what to do instead. Large structured state
(a project snapshot, a plan) is legitimate and stays under ``MAX_VARIABLE_BYTES``.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_VARIABLE_BYTES = 8 * 1024 * 1024
INLINE_BINARY_MIN_CHARS = 64 * 1024
# The refusal text names this; a step failure carries it, so the App can say what happened.
INLINE_FILE_DATA_MARK = "contains inline file data"

_BASE64_BODY = re.compile(r"[A-Za-z0-9+/]+=*")


def _is_inline_binary(text: str) -> bool:
    if len(text) < INLINE_BINARY_MIN_CHARS:
        return False
    head = text[:160]
    if head.startswith("data:") and ";base64," in head:
        return True
    return _BASE64_BODY.fullmatch(text) is not None


def find_inline_binary(value: Any, path: str = "") -> str | None:
    """The dotted path of the first inline binary string in ``value``, or ``None``."""
    if isinstance(value, str):
        return path if _is_inline_binary(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            found = find_inline_binary(item, f"{path}.{key}" if path else str(key))
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = find_inline_binary(item, f"{path}.{index}" if path else str(index))
            if found:
                return found
    return None


def variable_limit_error(name: str, value: Any) -> str | None:
    """Why ``value`` may not be stored under ``name``, or ``None`` when it may."""
    path = find_inline_binary(value)
    if path:
        return (f"'{name}' {INLINE_FILE_DATA_MARK} at {path}. Save the file as a Document and keep its "
                "document_id; workflow variables cannot hold file contents.")
    size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    if size > MAX_VARIABLE_BYTES:
        return (f"'{name}' is {size // 1024} KB, over the {MAX_VARIABLE_BYTES // (1024 * 1024)} MB limit for a "
                "workflow variable. Keep only the fields the next step needs.")
    return None


def trim_inline_binary(value: Any) -> tuple[Any, int, int]:
    """``value`` with each inline binary string replaced by a note; also the count and bytes removed."""
    if isinstance(value, str):
        if _is_inline_binary(value):
            return f"[removed inline file data: {len(value)} characters]", 1, len(value)
        return value, 0, 0
    if isinstance(value, dict):
        out, count, removed = {}, 0, 0
        for key, item in value.items():
            new, c, r = trim_inline_binary(item)
            out[key] = new
            count, removed = count + c, removed + r
        return out, count, removed
    if isinstance(value, list):
        out, count, removed = [], 0, 0
        for item in value:
            new, c, r = trim_inline_binary(item)
            out.append(new)
            count, removed = count + c, removed + r
        return out, count, removed
    return value, 0, 0
