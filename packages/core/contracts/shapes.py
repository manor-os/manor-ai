"""Canonical output shapes — the single vocabulary for action results.

Every action's output maps to one shape. A shape owns three things:
its JSON Schema (for validation + binding the producer), a deterministic
``normalize`` that folds known aliases onto canonical keys, and an
``extract_from_text`` best-effort prose parser. Producer, normalizer, and
validator all share these key names, so field-name drift (path vs fs_path)
is structurally impossible.
"""
from __future__ import annotations

from typing import Any, Optional


class Shape:
    name: str = "Shape"

    def json_schema(self) -> dict:
        raise NotImplementedError

    def normalize(self, raw: Any) -> Any:
        return raw

    def extract_from_text(self, text: str) -> Optional[dict]:
        return None


def _first(d: dict, keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


class ArtifactResult(Shape):
    name = "ArtifactResult"
    _PATH_ALIASES = ("fs_path", "path", "file_path", "saved_to", "filename")
    _URL_ALIASES = ("url", "file_url", "document_url", "image_url", "video_url", "result_url")

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["files"],
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "fs_path"],
                        "properties": {
                            "name": {"type": "string"},
                            "fs_path": {"type": "string"},
                            "url": {"type": "string"},
                        },
                    },
                }
            },
        }

    def normalize(self, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        files = raw.get("files")
        if not isinstance(files, list):
            return raw
        norm_files = []
        for item in files:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            fs_path = _first(item, self._PATH_ALIASES)
            if fs_path and not entry.get("fs_path"):
                # path-like alias wins for fs_path only if not a URL
                if not fs_path.startswith(("http://", "https://")):
                    entry["fs_path"] = fs_path
            url = _first(item, self._URL_ALIASES)
            if url and not entry.get("url"):
                entry["url"] = url
            if not entry.get("name") and entry.get("fs_path"):
                entry["name"] = entry["fs_path"].rstrip("/").split("/")[-1]
            norm_files.append(entry)
        out = dict(raw)
        out["files"] = norm_files
        return out


class TextResult(Shape):
    name = "TextResult"
    _ALIASES = ("text", "value", "content", "output", "answer", "final", "message")

    def json_schema(self) -> dict:
        return {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}

    def normalize(self, raw: Any) -> Any:
        if isinstance(raw, str):
            return {"text": raw}
        if isinstance(raw, dict) and not raw.get("text"):
            val = _first(raw, self._ALIASES)
            if val:
                out = dict(raw)
                out["text"] = val
                return out
        return raw

    def extract_from_text(self, text: str) -> Optional[dict]:
        return {"text": text} if text and text.strip() else None


class DocumentResult(Shape):
    name = "DocumentResult"

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["fs_path"],
            "properties": {
                "document_id": {"type": "string"},
                "fs_path": {"type": "string"},
                "title": {"type": "string"},
            },
        }

    def normalize(self, raw: Any) -> Any:
        if not isinstance(raw, dict) or raw.get("fs_path"):
            return raw
        fs = _first(raw, ("fs_path", "path", "file_path", "saved_to"))
        if fs and not fs.startswith(("http://", "https://")):
            out = dict(raw)
            out["fs_path"] = fs
            return out
        return raw


class ListResult(Shape):
    name = "ListResult"
    _ALIASES = ("items", "result", "data", "records", "rows")

    def json_schema(self) -> dict:
        return {"type": "object", "required": ["items"], "properties": {"items": {"type": "array"}}}

    def normalize(self, raw: Any) -> Any:
        if isinstance(raw, list):
            return {"items": raw}
        if isinstance(raw, dict) and not isinstance(raw.get("items"), list):
            for k in self._ALIASES:
                if isinstance(raw.get(k), list):
                    out = dict(raw)
                    out["items"] = raw[k]
                    return out
        return raw


class PublishResult(Shape):
    name = "PublishResult"

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string"},
                "published_at": {"type": "string"},
                "status": {"type": "string"},
            },
        }

    def normalize(self, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        out = dict(raw)
        if not out.get("url"):
            u = _first(raw, ("url", "post_url", "tweet_url"))
            if u:
                out["url"] = u
        if not out.get("published_at"):
            ts = _first(raw, ("published_at", "created_at"))
            if ts:
                out["published_at"] = ts
        return out


class CountResult(Shape):
    name = "CountResult"

    def json_schema(self) -> dict:
        return {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}

    def normalize(self, raw: Any) -> Any:
        if not isinstance(raw, dict) or isinstance(raw.get("count"), int):
            return raw
        for k in ("count", "draft_count", "total"):
            v = raw.get(k)
            if isinstance(v, int):
                out = dict(raw)
                out["count"] = v
                return out
        return raw


class EmptyResult(Shape):
    name = "EmptyResult"

    def json_schema(self) -> dict:
        return {"type": "object", "properties": {}}


class DraftPack(Shape):
    name = "DraftPack"
    _LIST_ALIASES = ("drafts", "posts", "items")
    _TEXT_ALIASES = ("text", "draft", "content")

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["drafts"],
            "properties": {
                "drafts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                }
            },
        }

    def normalize(self, raw: Any) -> Any:
        if isinstance(raw, list):
            raw = {"drafts": raw}
        if not isinstance(raw, dict):
            return raw
        source = None
        for k in self._LIST_ALIASES:
            if isinstance(raw.get(k), list):
                source = raw[k]
                break
        if source is None:
            return raw
        drafts = []
        for item in source:
            if isinstance(item, str):
                drafts.append({"text": item})
                continue
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            text = _first(item, self._TEXT_ALIASES)
            if text and not entry.get("text"):
                entry["text"] = text
            drafts.append(entry)
        out = dict(raw)
        out["drafts"] = drafts
        return out


_REGISTRY: dict[str, Shape] = {
    s.name: s
    for s in (
        ArtifactResult(), TextResult(), DocumentResult(),
        ListResult(), PublishResult(), CountResult(), EmptyResult(),
        DraftPack(),
    )
}


def get_shape(name: str) -> Shape:
    if name not in _REGISTRY:
        raise KeyError(f"unknown shape: {name!r}")
    return _REGISTRY[name]


def shape_names() -> list[str]:
    return sorted(_REGISTRY)


def coerce_to_shape(shape_name: str, raw: Any) -> Any:
    """Best-effort: normalize raw onto a shape's canonical keys.

    For string input, first try the shape's prose extractor, then normalize.
    Never fabricates: missing fields stay missing for the validator to catch.
    """
    shape = get_shape(shape_name)
    if isinstance(raw, str):
        extracted = shape.extract_from_text(raw)
        if extracted is not None:
            raw = extracted
    return shape.normalize(raw)
