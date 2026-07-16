"""Resolve ``${{ steps.<key>.result.<path> }}`` references inside step
params. Pattern + semantics borrowed from GitHub Actions.

Two modes:

  * Bare ref ("${{ steps.foo.result.bar }}") → returns the resolved
    value with its native type (dict / list / number / bool …) so
    downstream JSON Schema validation sees the real shape.
  * Embedded ref ("Tweet text: ${{ steps.draft.result.text }}") →
    string substitution. Non-scalar values are JSON-stringified.

Walks dicts + lists recursively. Strings without refs pass through.
"""
from __future__ import annotations

import json
import re
from typing import Any

_REF_RE = re.compile(
    r"\$\{\{\s*steps\.([A-Za-z_][A-Za-z0-9_]*)\.result(?:\.([A-Za-z_][A-Za-z0-9_.\[\]]*))?\s*\}\}"
)
_BARE_REF_RE = re.compile(r"^\s*" + _REF_RE.pattern + r"\s*$")


class ReferenceError(Exception):
    """Raised when a ${{ steps.X.result.Y }} ref doesn't resolve."""


def extract_step_refs(value: Any) -> list[tuple[str, str | None]]:
    """Return ``(step_key, top_level_field)`` for every ``${{ steps.X.result.Y }}``
    reference found anywhere inside ``value``.

    ``top_level_field`` is the first identifier of the path (``selected_topics``
    for ``selected_topics[0].title``), or ``None`` for a bare ``.result`` ref.
    Used by the plan-time linker to check that referenced keys exist in the
    producing step's output shape — the same parser that resolves refs at
    dispatch, so the linker and the executor never disagree.
    """
    refs: list[tuple[str, str | None]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for m in _REF_RE.finditer(node):
                path = m.group(2)
                field = re.split(r"[.\[]", path, maxsplit=1)[0] if path else None
                refs.append((m.group(1), field))

    walk(value)
    return refs


def resolve_refs(value: Any, prior_results: dict[str, Any]) -> Any:
    """Recursively resolve refs inside arbitrary JSON-shaped data.

    ``prior_results`` is ``{step_key: <result dict-or-value>}``.
    Step results that don't exist raise ReferenceError — fail fast at
    dispatch time instead of giving the underlying adapter garbage."""
    if isinstance(value, dict):
        return {k: resolve_refs(v, prior_results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, prior_results) for v in value]
    if isinstance(value, str):
        return _resolve_string(value, prior_results)
    return value


def _resolve_string(s: str, prior_results: dict[str, Any]) -> Any:
    bare = _BARE_REF_RE.match(s)
    if bare:
        # Whole-string ref — preserve type.
        step_key, path = bare.group(1), bare.group(2)
        return _lookup(prior_results, step_key, path)

    # Embedded — substitute every match with its string form.
    def _sub(m: re.Match) -> str:
        v = _lookup(prior_results, m.group(1), m.group(2))
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False, default=str)

    return _REF_RE.sub(_sub, s)


def _lookup(
    prior_results: dict[str, Any],
    step_key: str,
    path: str | None,
) -> Any:
    if step_key not in prior_results:
        raise ReferenceError(
            f"reference to step {step_key!r} but it is not in prior results"
        )

    cursor: Any = prior_results[step_key]
    if not path:
        return cursor

    # Path is dot-separated; tolerate ``foo.bar.baz`` or
    # ``foo[0].bar`` (list indices are integer parts).
    parts = _tokenize_path(path)
    for i, part in enumerate(parts):
        if isinstance(cursor, list):
            try:
                idx = int(part)
            except (TypeError, ValueError):
                raise ReferenceError(
                    f"step {step_key} path .{path}: expected int index "
                    f"at .{'.'.join(parts[:i + 1])}"
                ) from None
            try:
                cursor = cursor[idx]
            except IndexError:
                raise ReferenceError(
                    f"step {step_key} path .{path}: list index {idx} out of range"
                ) from None
        elif isinstance(cursor, dict):
            if part not in cursor:
                raise ReferenceError(
                    f"step {step_key} path .{path}: key {part!r} missing"
                )
            cursor = cursor[part]
        else:
            raise ReferenceError(
                f"step {step_key} path .{path}: cannot descend into {type(cursor).__name__}"
            )
    return cursor


def _tokenize_path(path: str) -> list[str]:
    """``foo.bar[0].baz`` → ['foo', 'bar', '0', 'baz']."""
    out: list[str] = []
    buf = ""
    i = 0
    while i < len(path):
        c = path[i]
        if c == ".":
            if buf:
                out.append(buf)
                buf = ""
        elif c == "[":
            if buf:
                out.append(buf)
                buf = ""
            j = path.find("]", i)
            if j == -1:
                raise ReferenceError(f"unterminated [ in path {path!r}")
            out.append(path[i + 1:j])
            i = j
        else:
            buf += c
        i += 1
    if buf:
        out.append(buf)
    return out
