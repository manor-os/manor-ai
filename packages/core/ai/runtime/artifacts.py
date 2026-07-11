from __future__ import annotations

import json
import re
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit


_RUNTIME_ARTIFACT_URL_RE = re.compile(r"(?:https?://[^\s\"'<>`,，]+)?/api/v1/fs/[^\s\"'<>`,，]+")
_RUNTIME_ARTIFACT_TRAILING_CHARS = ".。;；:：)]}>'\""
_RUNTIME_ARTIFACT_EXTENSIONS = (
    "aac",
    "avi",
    "bmp",
    "csv",
    "css",
    "doc",
    "docx",
    "flac",
    "gif",
    "htm",
    "html",
    "jpeg",
    "jpg",
    "js",
    "json",
    "jsonl",
    "jsx",
    "m4a",
    "m4v",
    "md",
    "mkv",
    "mov",
    "mp3",
    "mp4",
    "ogg",
    "pdf",
    "png",
    "ppt",
    "pptx",
    "py",
    "scss",
    "srt",
    "svg",
    "ts",
    "tsv",
    "tsx",
    "txt",
    "vtt",
    "wav",
    "webm",
    "webp",
    "xls",
    "xlsx",
    "yaml",
    "yml",
    "zip",
)
_RUNTIME_ARTIFACT_PATH_EXT_RE = re.compile(
    rf"\.(?:{'|'.join(re.escape(ext) for ext in _RUNTIME_ARTIFACT_EXTENSIONS)})(?:[?#].*)?$",
    re.IGNORECASE,
)
_RUNTIME_ARTIFACT_PATH_PREFIXES = (
    "Workspaces/",
    "assets/",
    "audio/",
    "audios/",
    "code/",
    "documents/",
    "exports/",
    "files/",
    "generated/",
    "goals/",
    "images/",
    "knowledge/",
    "outputs/",
    "presentations/",
    "runbooks/",
    "spreadsheets/",
    "tasks/",
    "uploads/",
    "videos/",
)
_RUNTIME_ARTIFACT_PATH_PREFIXES_LOWER = tuple(prefix.lower() for prefix in _RUNTIME_ARTIFACT_PATH_PREFIXES)
_RUNTIME_ARTIFACT_TEXT_REF_RE = re.compile(
    r"(?P<ref>(?:/workspace/|\./)?"
    rf"(?:{'|'.join(re.escape(prefix) for prefix in _RUNTIME_ARTIFACT_PATH_PREFIXES)})"
    r"[^\s\"'<>`,，]+)",
    re.IGNORECASE,
)
_RUNTIME_ARTIFACT_STRONG_REF_KEYS = frozenset(
    {
        "audio_url",
        "document_url",
        "download_url",
        "entry_url",
        "file_path",
        "file_url",
        "fs_path",
        "image_url",
        "media_url",
        "output_path",
        "output_url",
        "preview_url",
        "result_url",
        "saved_to",
        "video_url",
    }
)
_RUNTIME_ARTIFACT_WEAK_REF_KEYS = frozenset(
    {
        "input_path",
        "local_path",
        "path",
        "source",
        "target",
        "url",
    }
)
_RUNTIME_ARTIFACT_COLLECTION_KEYS = frozenset(
    {
        "artifacts",
        "assets",
        "attachments",
        "audio",
        "audios",
        "dependencies",
        "documents",
        "files",
        "images",
        "media",
        "outputs",
        "references",
        "videos",
    }
)
_RUNTIME_ARTIFACT_CREATION_FLAGS = frozenset(
    {
        "created",
        "downloaded",
        "exported",
        "generated",
        "saved",
        "uploaded",
        "written",
    }
)
_RUNTIME_ARTIFACT_KINDS = frozenset(
    {
        "audio",
        "code",
        "document",
        "file",
        "image",
        "pdf",
        "presentation",
        "spreadsheet",
        "subtitle",
        "video",
        "word_document",
    }
)


def _string_set(values: Iterable[Any] | None) -> set[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {str(value).strip() for value in values if str(value or "").strip()}


def _clean_artifact_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.strip("<>[](){}'\"`")
    text = text.rstrip(_RUNTIME_ARTIFACT_TRAILING_CHARS)
    text = unescape(text)
    try:
        text = unquote(text)
    except Exception:
        pass
    return text.strip()


def _has_artifact_path_prefix(value: str) -> bool:
    normalized = value.replace("\\", "/").lstrip("./")
    if normalized.startswith("workspace/"):
        normalized = normalized[len("workspace/") :]
    lowered = normalized.lower()
    return lowered.startswith(_RUNTIME_ARTIFACT_PATH_PREFIXES_LOWER)


def _looks_like_runtime_artifact_path(value: str) -> bool:
    normalized = _clean_artifact_ref(value).replace("\\", "/")
    if not normalized or "://" in normalized or normalized.startswith("data:"):
        return False
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/") :]
    elif normalized.startswith("/"):
        return False
    return bool(_RUNTIME_ARTIFACT_PATH_EXT_RE.search(normalized))


def _normalize_runtime_artifact_ref(value: Any, *, allow_generic_path: bool = False) -> str:
    ref = _clean_artifact_ref(value).replace("\\", "/")
    if not ref:
        return ""
    if "/api/v1/fs/" in ref:
        return ref
    if ref.startswith(("http://", "https://", "data:")):
        return ""
    if ref.startswith("/workspace/"):
        ref = ref[len("/workspace/") :]
    elif ref.startswith("/"):
        return ""
    ref = ref.lstrip("./")
    if not ref:
        return ""
    if _has_artifact_path_prefix(ref) or (allow_generic_path and _looks_like_runtime_artifact_path(ref)):
        return ref
    return ""


def _dict_has_artifact_signal(value: dict[str, Any]) -> bool:
    if any(bool(value.get(key)) for key in _RUNTIME_ARTIFACT_CREATION_FLAGS):
        return True
    kind = str(value.get("kind") or value.get("type") or "").strip().lower()
    return kind in _RUNTIME_ARTIFACT_KINDS


def _artifact_refs_from_text(text: str) -> set[str]:
    refs: set[str] = set()
    url_spans: list[tuple[int, int]] = []
    for match in _RUNTIME_ARTIFACT_URL_RE.finditer(text):
        url_spans.append(match.span())
        url = _clean_artifact_ref(match.group(0))
        if url:
            refs.add(url)
    for match in _RUNTIME_ARTIFACT_TEXT_REF_RE.finditer(text):
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        ref = _normalize_runtime_artifact_ref(match.group("ref"))
        if ref:
            refs.add(ref)
    return refs


@dataclass
class RuntimeArtifactScope:
    runtime_artifact_urls: set[str] = field(default_factory=set)
    dependency_artifact_urls: set[str] = field(default_factory=set)


_runtime_artifact_scope_var: ContextVar[RuntimeArtifactScope | None] = ContextVar(
    "runtime_artifact_scope",
    default=None,
)


def runtime_current_artifact_scope() -> RuntimeArtifactScope | None:
    return _runtime_artifact_scope_var.get()


def runtime_current_artifact_urls() -> frozenset[str]:
    scope = runtime_current_artifact_scope()
    return frozenset(scope.runtime_artifact_urls) if scope is not None else frozenset()


def runtime_current_dependency_artifact_urls() -> frozenset[str]:
    scope = runtime_current_artifact_scope()
    return frozenset(scope.dependency_artifact_urls) if scope is not None else frozenset()


@contextmanager
def runtime_artifact_tracking_scope(
    *,
    runtime_artifact_urls: Iterable[Any] | None = None,
    dependency_artifact_urls: Iterable[Any] | None = None,
) -> Iterator[RuntimeArtifactScope]:
    """Track generated file URLs for one long-running agent/tool loop."""

    current = runtime_current_artifact_scope()
    if current is not None:
        current.runtime_artifact_urls.update(_string_set(runtime_artifact_urls))
        current.dependency_artifact_urls.update(_string_set(dependency_artifact_urls))
        yield current
        return

    scope = RuntimeArtifactScope(
        runtime_artifact_urls=_string_set(runtime_artifact_urls),
        dependency_artifact_urls=_string_set(dependency_artifact_urls),
    )
    token = _runtime_artifact_scope_var.set(scope)
    try:
        yield scope
    finally:
        _runtime_artifact_scope_var.reset(token)


def runtime_extract_artifact_urls_from_tool_result(result: Any) -> set[str]:
    """Extract reusable Manor file references from a tool result.

    The runtime artifact ledger is intentionally tool-agnostic. It accepts
    entity file URLs plus common structured file fields such as ``fs_path``,
    ``result_url``, ``output_path``, and entries nested under ``files`` or
    ``artifacts``. External web URLs and arbitrary local absolute paths are
    ignored because later tools cannot reliably reuse them as Manor artifacts.
    """

    urls: set[str] = set()

    def visit(value: Any, *, key: str = "", artifact_context: bool = False) -> None:
        if isinstance(value, dict):
            dict_artifact_context = artifact_context or _dict_has_artifact_signal(value)
            for item_key, item in value.items():
                normalized_key = str(item_key).strip().lower()
                visit(
                    item,
                    key=normalized_key,
                    artifact_context=dict_artifact_context or normalized_key in _RUNTIME_ARTIFACT_COLLECTION_KEYS,
                )
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item, key=key, artifact_context=artifact_context)
            return
        if not isinstance(value, str):
            return

        text = value.strip()
        if not text:
            return

        if text[0] in "{[":
            try:
                visit(json.loads(text), key=key, artifact_context=artifact_context)
            except Exception:
                pass

        urls.update(_artifact_refs_from_text(text))

        if key in _RUNTIME_ARTIFACT_STRONG_REF_KEYS:
            ref = _normalize_runtime_artifact_ref(text, allow_generic_path=True)
            if ref:
                urls.add(ref)
            return

        if key in _RUNTIME_ARTIFACT_WEAK_REF_KEYS or artifact_context:
            ref = _normalize_runtime_artifact_ref(
                text,
                allow_generic_path=artifact_context or key != "url",
            )
            if ref:
                urls.add(ref)

    visit(result)
    return urls


def runtime_record_tool_result_artifacts(result: Any) -> set[str]:
    urls = runtime_extract_artifact_urls_from_tool_result(result)
    scope = runtime_current_artifact_scope()
    if scope is not None:
        scope.runtime_artifact_urls.update(urls)
    return urls


def runtime_reference_artifact_variants(ref: Any) -> set[str]:
    raw = str(ref or "").strip()
    if not raw:
        return set()
    decoded = unescape(raw)
    try:
        decoded = unquote(decoded)
    except Exception:
        pass

    variants = {raw, decoded}
    try:
        path = urlsplit(decoded).path or ""
    except Exception:
        path = ""

    if path:
        variants.add(path)
    marker = "/api/v1/fs/"
    if path.startswith(marker):
        parts = path[len(marker) :].split("/", 1)
        if len(parts) == 2 and parts[1]:
            variants.add(parts[1])
            variants.add(f"/workspace/{parts[1]}")
    workspace_marker = "/workspace/"
    if path.startswith(workspace_marker):
        parts = path[len(workspace_marker) :]
        if parts:
            variants.add(parts)
    if decoded and not decoded.startswith("/") and "://" not in decoded:
        variants.add(f"/workspace/{decoded.lstrip('./')}")

    return {variant.lower() for variant in variants if variant}


def runtime_reference_allowed_by_artifacts(
    allowed_refs: Iterable[Any] | None,
    ref: Any,
) -> bool:
    ref_variants = runtime_reference_artifact_variants(ref)
    if not ref_variants:
        return False
    for allowed_ref in allowed_refs or []:
        if ref_variants & runtime_reference_artifact_variants(allowed_ref):
            return True
    return False


def runtime_artifact_workspace_path(ref: Any) -> str:
    raw = str(ref or "").strip()
    if not raw:
        return ""
    decoded = unescape(raw)
    try:
        decoded = unquote(decoded)
    except Exception:
        pass
    try:
        path = urlsplit(decoded).path or decoded
    except Exception:
        path = decoded

    marker = "/api/v1/fs/"
    if path.startswith(marker):
        parts = path[len(marker) :].split("/", 1)
        if len(parts) == 2 and parts[0] != "public" and parts[1]:
            return parts[1].lstrip("/")
        return ""
    workspace_marker = "/workspace/"
    if path.startswith(workspace_marker):
        return path[len(workspace_marker) :].lstrip("/")
    if path and not path.startswith("/") and "://" not in path:
        return path
    return ""


def runtime_artifact_context_section(
    *,
    runtime_artifact_urls: Iterable[Any] | None = None,
    dependency_artifact_urls: Iterable[Any] | None = None,
    max_items: int = 40,
) -> str:
    runtime_urls = set(runtime_current_artifact_urls()) | _string_set(runtime_artifact_urls)
    dependency_urls = set(runtime_current_dependency_artifact_urls()) | _string_set(dependency_artifact_urls)
    items = sorted(runtime_urls | dependency_urls)
    if not items:
        return ""

    lines = [
        "## Runtime Artifacts Available For This Run",
        (
            "These file references were produced earlier in this same run or supplied "
            "as task dependencies. Reuse them when the user asks to build from "
            "previous outputs; do not regenerate substitutes unless the user "
            "explicitly asks. Inside sandbox skills, entity files are mounted "
            "read-only under `/workspace/<path>`."
        ),
    ]
    for ref in items[:max_items]:
        workspace_path = runtime_artifact_workspace_path(ref)
        suffix = f" (`/workspace/{workspace_path}`)" if workspace_path else ""
        kind = "dependency" if ref in dependency_urls and ref not in runtime_urls else "runtime"
        lines.append(f"- [{kind}] {ref}{suffix}")
    if len(items) > max_items:
        lines.append(f"- ... {len(items) - max_items} more artifact(s) omitted")
    return "\n".join(lines)


def runtime_input_with_artifact_context(
    input_text: Any,
    *,
    runtime_artifact_urls: Iterable[Any] | None = None,
    dependency_artifact_urls: Iterable[Any] | None = None,
) -> str:
    text = str(input_text or "").strip()
    section = runtime_artifact_context_section(
        runtime_artifact_urls=runtime_artifact_urls,
        dependency_artifact_urls=dependency_artifact_urls,
    )
    if not section or "## Runtime Artifacts Available For This Run" in text:
        return text
    if not text:
        return section
    return f"{text.rstrip()}\n\n{section}"
