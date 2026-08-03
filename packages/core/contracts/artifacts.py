"""Canonical contract for artifacts that tools explicitly persist to Knowledge."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse


_KNOWLEDGE_ARTIFACT_KINDS = frozenset({
    "audio",
    "document",
    "file",
    "image",
    "video",
})


def build_knowledge_artifacts(urls: Iterable[Any], *, kind: str) -> list[dict[str, str]]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in _KNOWLEDGE_ARTIFACT_KINDS:
        return []

    return normalize_knowledge_artifacts(
        {"url": value, "kind": normalized_kind}
        for value in urls
    )


def normalize_knowledge_artifacts(values: Iterable[Any] | None) -> list[dict[str, str]]:
    if values is None:
        return []

    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        kind = str(value.get("kind") or "").strip().lower()
        url = str(value.get("url") or "").strip()
        parsed = urlparse(url)
        if kind not in _KNOWLEDGE_ARTIFACT_KINDS:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
            continue
        seen.add(url)
        artifacts.append({"url": url, "kind": kind})
    return artifacts


def build_mcp_text_result(
    text: str,
    *,
    knowledge_artifacts: Iterable[Any] | None = None,
) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "_manor": {
                "knowledge_artifacts": normalize_knowledge_artifacts(knowledge_artifacts),
            }
        },
        "isError": False,
    }


def extract_mcp_knowledge_artifacts(result: Any) -> list[dict[str, str]]:
    if not isinstance(result, dict):
        return []
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    if not isinstance(structured, dict):
        return []
    manor = structured.get("_manor")
    if not isinstance(manor, dict):
        return []
    values = manor.get("knowledge_artifacts")
    if not isinstance(values, list):
        return []
    return normalize_knowledge_artifacts(values)
