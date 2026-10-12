from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

from packages.core.services.file_refs import parse_file_reference


# A reference ends at whitespace, a quote or sentence punctuation (ASCII or
# CJK). Brackets such as 《》【】 stay: they are common in Chinese filenames.
_RUNTIME_ARTIFACT_REF_STOP = "\\s\"'<>`,，。；：！？、“”‘’「」『』"
_RUNTIME_ARTIFACT_URL_RE = re.compile(
    rf"(?:https?://[^{_RUNTIME_ARTIFACT_REF_STOP}]+)?/api/v1/fs/[^{_RUNTIME_ARTIFACT_REF_STOP}]+"
)
_RUNTIME_ARTIFACT_TRAILING_CHARS = ".。;；:：)]}>'\""
_RUNTIME_ARTIFACT_EXTENSIONS = (
    "aac",
    "ass",
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
    rf"[^{_RUNTIME_ARTIFACT_REF_STOP}]+)",
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


def _without_unbalanced_close(value: str) -> str:
    """Cut a matched reference at its first unbalanced closing parenthesis.

    ``[sheet](/api/v1/fs/e/a.jpg)。请回复`` must yield ``/api/v1/fs/e/a.jpg``,
    while ``报告（终版）.docx`` keeps its own balanced parentheses.
    """

    depth = 0
    for index, char in enumerate(value):
        if char in "(（":
            depth += 1
        elif char in ")）":
            if depth == 0:
                return value[:index]
            depth -= 1
    return value


def _artifact_refs_from_text(text: str) -> set[str]:
    refs: set[str] = set()
    url_spans: list[tuple[int, int]] = []
    for match in _RUNTIME_ARTIFACT_URL_RE.finditer(text):
        url_spans.append(match.span())
        url = _clean_artifact_ref(_without_unbalanced_close(match.group(0)))
        if url:
            refs.add(url)
    for match in _RUNTIME_ARTIFACT_TEXT_REF_RE.finditer(text):
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        ref = _normalize_runtime_artifact_ref(_without_unbalanced_close(match.group("ref")))
        if ref:
            refs.add(ref)
    return refs


@dataclass
class RuntimeArtifactScope:
    runtime_artifact_urls: set[str] = field(default_factory=set)
    dependency_artifact_urls: set[str] = field(default_factory=set)
    web_source_slices: list["RuntimeWebSourceSlice"] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeWebSourceSlice:
    """Text actually returned by a host web_fetch, not a model-cited URL."""

    url: str
    content: str
    offset: int
    source_sha256: str
    complete_source: bool
    kind: str = "web_fetch"


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


def runtime_current_web_source_slices() -> tuple[RuntimeWebSourceSlice, ...]:
    scope = runtime_current_artifact_scope()
    return tuple(scope.web_source_slices) if scope is not None else ()


# Web source text a round checkpoint carries: one maximal slice. Rounds
# rewrite the whole checkpoint, and the transcript already holds this text;
# parked checkpoints and a run's final state carry every slice (at most 10).
RUNTIME_ROUND_WEB_SOURCE_CHARS = 40_000


def runtime_web_source_checkpoint_state(*, max_chars: int | None = None) -> list[dict[str, Any]]:
    """Preserve host-observed search/fetch text across a parked Agent turn.

    With ``max_chars``, only the most recent slices whose text fits are kept.
    """

    slices = list(runtime_current_web_source_slices())
    if max_chars is not None:
        kept: list[RuntimeWebSourceSlice] = []
        used = 0
        for item in reversed(slices):
            if used + len(item.content) > max_chars:
                break
            kept.append(item)
            used += len(item.content)
        slices = list(reversed(kept))
    return [
        {
            "url": item.url,
            "content": item.content,
            "offset": item.offset,
            "source_sha256": item.source_sha256,
            "complete_source": item.complete_source,
            "kind": item.kind,
            "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
        }
        for item in slices
    ]


def _restore_web_source_slices(state: Iterable[Any] | None) -> list[RuntimeWebSourceSlice]:
    """Reject damaged checkpoint evidence instead of presenting it as a source."""

    if state is None:
        return []
    if not isinstance(state, (list, tuple)) or len(state) > 10:
        raise ValueError("invalid web source checkpoint")
    restored: list[RuntimeWebSourceSlice] = []
    for raw in state:
        if not isinstance(raw, dict):
            raise ValueError("invalid web source checkpoint")
        url, content = raw.get("url"), raw.get("content")
        offset, source_sha256 = raw.get("offset"), raw.get("source_sha256")
        digest = raw.get("content_sha256")
        kind, complete = raw.get("kind"), raw.get("complete_source")
        if (
            not isinstance(url, str) or not url or len(url) > 2048
            or not isinstance(content, str) or not content
            or len(content) > 40_000
            or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
            or not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
            or not isinstance(digest, str)
            or digest != hashlib.sha256(content.encode("utf-8")).hexdigest()
            or kind not in {"web_fetch", "web_search_snippets"}
            or not isinstance(complete, bool)
        ):
            raise ValueError("invalid web source checkpoint")
        restored.append(RuntimeWebSourceSlice(
            url=url, content=content, offset=offset,
            source_sha256=source_sha256, complete_source=complete, kind=kind,
        ))
    return restored


def runtime_record_web_source_slice(
    *, url: str, content: str, offset: int, source_sha256: str,
    complete_source: bool, kind: str = "web_fetch",
) -> None:
    """Retain bounded host-observed web text for a later Skill in this run."""

    scope = runtime_current_artifact_scope()
    if scope is None or not content or len(url) > 2048 or len(content) > 40_000:
        return
    item = RuntimeWebSourceSlice(
        url=url, content=content, offset=offset,
        source_sha256=source_sha256, complete_source=complete_source,
        kind=kind,
    )
    if item not in scope.web_source_slices:
        scope.web_source_slices.append(item)
        del scope.web_source_slices[:-10]


def runtime_record_web_search_result(query: str, result: str) -> None:
    """Retain returned snippets, never claim that their linked pages were read."""

    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return
    content = json.dumps(
        {"query": query, "results": payload["results"]}, ensure_ascii=False,
    )
    if len(content) > 40_000:
        return
    runtime_record_web_source_slice(
        url=query,
        content=content,
        offset=0,
        source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        complete_source=False,
        kind="web_search_snippets",
    )


@contextmanager
def runtime_artifact_tracking_scope(
    *,
    runtime_artifact_urls: Iterable[Any] | None = None,
    dependency_artifact_urls: Iterable[Any] | None = None,
    web_source_slices: Iterable[Any] | None = None,
) -> Iterator[RuntimeArtifactScope]:
    """Track generated file URLs for one long-running agent/tool loop."""

    current = runtime_current_artifact_scope()
    if current is not None:
        current.runtime_artifact_urls.update(_string_set(runtime_artifact_urls))
        current.dependency_artifact_urls.update(_string_set(dependency_artifact_urls))
        for item in _restore_web_source_slices(web_source_slices):
            if item not in current.web_source_slices:
                current.web_source_slices.append(item)
        del current.web_source_slices[:-10]
        yield current
        return

    scope = RuntimeArtifactScope(
        runtime_artifact_urls=_string_set(runtime_artifact_urls),
        dependency_artifact_urls=_string_set(dependency_artifact_urls),
        web_source_slices=_restore_web_source_slices(web_source_slices),
    )
    token = _runtime_artifact_scope_var.set(scope)
    try:
        yield scope
    finally:
        _runtime_artifact_scope_var.reset(token)


def runtime_extract_artifact_urls_from_tool_result(
    result: Any, *, structured_only: bool = False,
) -> set[str]:
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
            for item_key, item in value.items():
                normalized_key = str(item_key).strip().lower()
                visit(
                    item,
                    key=normalized_key,
                    artifact_context=artifact_context or normalized_key in _RUNTIME_ARTIFACT_COLLECTION_KEYS,
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

        # A generic write result can contain unrelated metadata such as a
        # Task title ending in ".md". Only explicit artifact fields/collections
        # (or a producer's free-form result note) may contribute text refs.
        if key in _RUNTIME_ARTIFACT_STRONG_REF_KEYS or artifact_context or (
            not structured_only and key in {"content", "message", "notes"}
        ):
            urls.update(_artifact_refs_from_text(text))
        elif not structured_only:
            # Runtime continuation may reuse an explicit Manor fs URL even
            # when a tool nests it under an arbitrary key. Unlike a filename
            # inferred from a title, the URL identifies the entity fs route.
            urls.update(
                ref for ref in _artifact_refs_from_text(text)
                if "/api/v1/fs/" in ref
            )

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
    file_ref = parse_file_reference(unescape(raw))
    if file_ref is not None and file_ref.entity_id and file_ref.fs_path:
        variants.add(file_ref.fs_path)
        variants.add(f"/workspace/{file_ref.fs_path}")
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
    file_ref = parse_file_reference(unescape(raw))
    if file_ref is not None:
        # An fs URL or the /workspace mount names an entity path; a
        # viewer/document address has none.
        return file_ref.fs_path
    decoded = unescape(raw)
    try:
        decoded = unquote(decoded)
    except Exception:
        pass
    try:
        path = urlsplit(decoded).path or decoded
    except Exception:
        path = decoded
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
