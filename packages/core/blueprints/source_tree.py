"""A Blueprint as a directory of small files, so git merges it like code.

A payload is one JSON document. Kept in git as one file, every prompt is a
single line — two people editing different paragraphs of the same prompt
conflict on the whole line, and a diff of an 11,000-character line is
unreadable. The source tree splits the same payload into files git handles
well:

    manor.json                        the payload, components and long text
                                      replaced by references, plus the
                                      package's asset list
    recipe/workflows/<slug>.json      one file per component (every item of
    embedded/skills/<slug>.json       a keyed list under a payload section)
    embedded/skills/<slug>/<path>.md  each multi-line text, as plain text
    assets/<sha256><ext>              files the package carries

A reference is ``{"$ref": "<path from the tree root>"}``. ``join(split(p))``
is ``p`` exactly; the tree is only a layout, never a second format to keep in
sync. Text files end with one newline that is not part of the value, so an
editor adding a final newline changes nothing.
"""
from __future__ import annotations

import json
import re
from typing import Any

FORMAT = "manor-source/1"
ROOT = "manor.json"
REF = "$ref"

# Strings that become their own .md file: prose an author edits. Multi-line
# text merges paragraph by paragraph; a long single line at least leaves the
# JSON readable (git still compares it as one line).
_TEXT_MIN_CHARS = 120
_LINE_MIN_CHARS = 400
_ITEM_KEYS = ("slug", "component_id", "key", "service_key", "goal_key", "rule_key", "job_id", "id", "name")
_SAFE = re.compile(r"[^a-z0-9._-]+")


class SourceTreeError(ValueError):
    """The files are not a Blueprint source tree."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _safe(value: Any) -> str:
    return _SAFE.sub("-", str(value).lower()).strip("-.") or "item"


def _item_key(items: list[Any]) -> str | None:
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for key in _ITEM_KEYS:
        values = [_safe(item.get(key)) if isinstance(item.get(key), str) and item.get(key) else None for item in items]
        if all(values) and len(set(values)) == len(values):
            return key
    return None


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {REF}


class _Writer:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def text_refs(self, value: Any, base: str, trail: list[str]) -> Any:
        """``value`` with each long multi-line string moved to ``base/<trail>.md``."""
        if _is_ref(value):
            return value
        if isinstance(value, str):
            if len(value) >= _LINE_MIN_CHARS or ("\n" in value and len(value) >= _TEXT_MIN_CHARS):
                path = f"{base}/{'.'.join(trail) or 'text'}.md"
                stem, index = path[:-3], 2
                while path in self.files:
                    path, index = f"{stem}-{index}.md", index + 1
                self.files[path] = value.encode("utf-8") + b"\n"
                return {REF: path}
            return value
        if isinstance(value, dict):
            return {name: self.text_refs(item, base, [*trail, _safe(name)]) for name, item in value.items()}
        if isinstance(value, list):
            key = _item_key(value)
            return [
                self.text_refs(item, base, [*trail, _safe(item[key]) if key else str(index)])
                for index, item in enumerate(value)
            ]
        return value

    def component(self, item: dict[str, Any], path: str) -> dict[str, str]:
        stem, index = path[:-5], 2
        while path in self.files:
            path, index = f"{stem}-{index}.json", index + 1
        self.files[path] = b""  # reserve before nested text paths are chosen
        self.files[path] = _canonical(self.text_refs(item, path[:-5], []))
        return {REF: path}


def _refuse_refs(value: Any, trail: list[str]) -> None:
    """A payload value shaped like a reference could not be told apart from one."""
    if _is_ref(value):
        raise SourceTreeError(f"the payload holds a {REF!r} object at {'.'.join(trail) or '(root)'}")
    if isinstance(value, dict):
        for name, item in value.items():
            _refuse_refs(item, [*trail, str(name)])
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_refs(item, [*trail, str(index)])


def split(payload: dict[str, Any], *, assets: list[dict[str, Any]] | None = None) -> dict[str, bytes]:
    """The source tree of ``payload``: relative path → file bytes."""
    if not isinstance(payload, dict):
        raise SourceTreeError("a Blueprint payload is an object")
    _refuse_refs(payload, [])
    writer = _Writer()
    skeleton: dict[str, Any] = {}
    for section, body in payload.items():
        if not isinstance(body, dict):
            skeleton[section] = body
            continue
        out: dict[str, Any] = {}
        for name, value in body.items():
            key = _item_key(value) if isinstance(value, list) else None
            if key is None:
                out[name] = value
                continue
            out[name] = [
                writer.component(item, f"{_safe(section)}/{_safe(name)}/{_safe(item[key])}.json") for item in value
            ]
        skeleton[section] = out
    root = {"format": FORMAT, "assets": list(assets or []), "blueprint": writer.text_refs(skeleton, "text", [])}
    writer.files[ROOT] = _canonical(root)
    return writer.files


def is_source_tree(files: dict[str, bytes]) -> bool:
    return ROOT in files


def join(files: dict[str, bytes]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The payload and the asset entries a source tree describes."""
    try:
        root = json.loads(files[ROOT])
    except (KeyError, ValueError) as exc:
        raise SourceTreeError(f"the tree has no readable {ROOT}") from exc
    if not isinstance(root, dict) or root.get("format") != FORMAT:
        raise SourceTreeError(f"unsupported source format {root.get('format') if isinstance(root, dict) else None!r}")
    seen: set[str] = set()

    def resolve(value: Any) -> Any:
        if _is_ref(value):
            path = str(value[REF])
            if path in seen:
                raise SourceTreeError(f"{path} is referenced twice")
            seen.add(path)
            if path not in files:
                raise SourceTreeError(f"{path} is referenced but missing")
            if path.endswith(".md"):
                text = files[path].decode("utf-8")
                return text[:-1] if text.endswith("\n") else text
            if path.endswith(".json"):
                try:
                    return resolve(json.loads(files[path]))
                except ValueError as exc:
                    raise SourceTreeError(f"{path} is not valid JSON: {exc}") from exc
            raise SourceTreeError(f"{path} is neither .json nor .md")
        if isinstance(value, dict):
            return {name: resolve(item) for name, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    payload = resolve(root.get("blueprint"))
    if not isinstance(payload, dict):
        raise SourceTreeError("the tree's blueprint is not a payload")
    assets = root.get("assets") or []
    if not isinstance(assets, list):
        raise SourceTreeError("the tree's asset list is not a list")
    return payload, assets
