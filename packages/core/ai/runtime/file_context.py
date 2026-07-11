from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Literal

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface


FileContextKind = Literal[
    "current_editor_file",
    "workspace_artifact",
    "draft_model_file",
    "skill_file",
    "chat_upload",
    "knowledge_document",
    "runtime_scratch_file",
]


@dataclass(frozen=True)
class FileContextMount:
    kind: FileContextKind
    path: str
    readable: bool = True
    writable: bool = False
    patch_only: bool = False
    metadata: dict = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "readable": self.readable,
            "writable": self.writable,
            "patch_only": self.patch_only,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class FileContextPolicyDecision:
    allowed: bool
    code: str | None = None
    reason: str | None = None
    tool_name: str | None = None
    paths: tuple[str, ...] = ()


FILE_CONTEXT_METADATA_KEY = "runtime_file_context_mounts"
RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY = "runtime_attachment_context"
EDITOR_CURRENT_DOCUMENT_CONTENT_KEYS = (
    "current_document_content",
    "currentDocumentContent",
    "current_content",
    "currentContent",
    "current_document",
    "currentDocument",
)
EDITOR_UI_ROUTE_PREFIXES = (
    "/viewer/",
    "/editor/",
    "/video-editor/",
    "/diagram-canvas",
    "/doc-editor",
    "/knowledge",
    "/workspaces/",
    "/api/",
)

FILE_READ_TOOLS = {"read_file", "list_files", "glob_files", "grep_files"}
FILE_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "delete_file",
    "generate_file",
    "sandbox_write_file",
    "sandbox_save_result",
}

def editor_file_identity_from_context(
    editor_context: dict | None,
    *,
    fallback: str = "current_editor_file",
) -> str:
    """Resolve a stable Runtime file identity for the active editor document."""

    context = editor_context or {}
    path = _clean_context_text(context.get("path") or context.get("file_path"))
    source_path = _clean_context_text(context.get("sourcePath") or context.get("source_path"))
    document_id = _clean_context_text(context.get("documentId") or context.get("document_id"))

    for candidate in (path, source_path):
        if candidate and not _looks_like_editor_ui_route(candidate):
            return candidate
    if document_id:
        return f"document:{document_id}"
    return fallback


def editor_context_without_inline_document_content(editor_context: dict | None) -> dict:
    """Return editor context metadata that is safe for traces and prompt metadata."""

    return {
        key: value
        for key, value in (editor_context or {}).items()
        if key not in EDITOR_CURRENT_DOCUMENT_CONTENT_KEYS
    }


def editor_current_document_content(editor_context: dict | None) -> str | None:
    for key in EDITOR_CURRENT_DOCUMENT_CONTENT_KEYS:
        if editor_context and key in editor_context and editor_context[key] is not None:
            return str(editor_context[key])
    return None


def runtime_parse_editor_context(value: str | dict | None) -> dict | None:
    if not value:
        return None
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None

    def _get(*keys: str) -> str | None:
        for key in keys:
            raw = data.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        return None

    context = {
        "path": _get("path", "sourcePath", "source_path"),
        "document_id": _get("document_id", "documentId"),
        "document_name": _get("document_name", "documentName"),
        "file_type": _get("file_type", "fileType"),
        "mime_type": _get("mime_type", "mimeType"),
        "editor_type": _get("editor_type", "editorType"),
        "current_document_content": editor_current_document_content(data),
    }
    return {
        key: val
        for key, val in context.items()
        if val or (key == "current_document_content" and val is not None)
    }


def file_context_mounts_for_request(request) -> list[FileContextMount]:
    editor_context = request.editor_context or {}
    mounts: list[FileContextMount] = []
    if request.surface == ChatSurface.FILE_EDITOR_CHAT:
        path = editor_file_identity_from_context(editor_context)
        metadata = {
            key: editor_context[key]
            for key in (
                "documentId",
                "document_id",
                "documentName",
                "document_name",
                "fileType",
                "file_type",
                "mimeType",
                "mime_type",
                "editorType",
                "editor_type",
                "supportsImageGeneration",
                "supports_image_generation",
            )
            if editor_context.get(key) is not None
        }
        mounts.append(
            FileContextMount(
                kind="current_editor_file",
                path=path,
                readable=True,
                writable=False,
                patch_only=True,
                metadata=metadata,
            )
        )
    if request.surface == ChatSurface.WORKSPACE_DRAFT_ARCHITECT:
        mounts.append(
            FileContextMount(
                kind="draft_model_file",
                path=f"workspace_draft:{request.metadata.get('draft_id') or 'active'}",
                readable=True,
                writable=True,
            )
        )
    if request.workspace_id and request.surface not in {
        ChatSurface.PUBLIC_CUSTOMER_CHAT,
        ChatSurface.EXTERNAL_CHANNEL_CHAT,
    }:
        mounts.append(
            FileContextMount(
                kind="workspace_artifact",
                path=f"workspace:{request.workspace_id}:artifacts",
                readable=True,
                writable=request.surface
                in {
                    ChatSurface.WORKSPACE_CHAT,
                    ChatSurface.TASK_COMMENT_THREAD,
                    ChatSurface.SCHEDULED_AGENT_RUN,
                    ChatSurface.WORKFLOW_AGENT_STEP,
                },
            )
        )
    mounts.extend(file_context_mounts_from_attachment_metadata(request.metadata))
    return mounts


def file_context_mounts_from_attachment_metadata(metadata: dict | None) -> list[FileContextMount]:
    raw_context = (
        metadata.get(RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY)
        if isinstance(metadata, dict)
        else None
    )
    raw_refs = raw_context.get("refs") if isinstance(raw_context, dict) else None
    if not isinstance(raw_refs, (list, tuple)):
        return []

    mounts: list[FileContextMount] = []
    for raw in raw_refs:
        if not isinstance(raw, dict):
            continue
        path = str(
            raw.get("path")
            or raw.get("url")
            or (f"document:{raw.get('document_id')}" if raw.get("document_id") else "")
        ).strip()
        if not path:
            continue
        kind = str(raw.get("kind") or "").strip()
        mount_kind: FileContextKind = (
            "knowledge_document"
            if kind == "knowledge_document"
            else "chat_upload"
        )
        mounts.append(
            FileContextMount(
                kind=mount_kind,
                path=path,
                readable=True,
                writable=False,
                metadata={k: v for k, v in raw.items() if v is not None},
            )
        )
    return mounts


def file_context_mounts_to_trace(mounts: list[FileContextMount]) -> tuple[dict[str, Any], ...]:
    return tuple(mount.to_trace_dict() for mount in mounts)


def file_context_mounts_from_envelope(envelope: Any) -> tuple[FileContextMount, ...]:
    metadata = getattr(envelope, "metadata", None)
    raw_mounts = (
        metadata.get(FILE_CONTEXT_METADATA_KEY)
        if isinstance(metadata, dict)
        else None
    )
    mounts: list[FileContextMount] = []
    if isinstance(raw_mounts, (list, tuple)):
        for raw in raw_mounts:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            path = str(raw.get("path") or "").strip()
            if not kind or not path:
                continue
            mounts.append(
                FileContextMount(
                    kind=kind,
                    path=path,
                    readable=bool(raw.get("readable", True)),
                    writable=bool(raw.get("writable", False)),
                    patch_only=bool(raw.get("patch_only", False)),
                    metadata=dict(raw.get("metadata") or {}),
                )
            )
    if mounts:
        return tuple(mounts)

    return tuple(
        FileContextMount(kind="runtime_scratch_file", path=str(path), readable=True)
        for path in getattr(envelope, "file_context_mounts", ()) or ()
        if str(path or "").strip()
    )


def runtime_allows_file_context_reader(
    envelope: Any,
    *,
    tool_name: str = "read_file",
    arguments: dict[str, Any] | None = None,
) -> bool:
    decision = check_file_context_policy(
        envelope=envelope,
        tool_name=tool_name,
        arguments=arguments or {},
    )
    return decision is None or decision.allowed


def runtime_allows_file_context_writer(
    envelope: Any,
    *,
    tool_name: str = "write_file",
    arguments: dict[str, Any] | None = None,
) -> bool:
    decision = check_file_context_policy(
        envelope=envelope,
        tool_name=tool_name,
        arguments=arguments or {},
    )
    return decision is None or decision.allowed


def check_file_context_policy(
    *,
    envelope: Any,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> FileContextPolicyDecision | None:
    """Return a denial when a file tool violates mounted file scope.

    Internal owner/global surfaces historically operate without file mounts, so
    the policy stays passive unless the runtime resolved structured mounts or
    the profile is a strict file-context surface such as file editor chat.
    """

    if envelope is None:
        return None

    name = str(tool_name or "").strip()
    if name not in FILE_READ_TOOLS and name not in FILE_WRITE_TOOLS:
        return None

    mounts = file_context_mounts_from_envelope(envelope)
    profile = getattr(envelope, "profile", None)
    strict_file_context = profile == RuntimeProfile.FILE_EDITOR_PATCH
    if not mounts and not strict_file_context:
        return None

    args = arguments or {}
    if name in FILE_WRITE_TOOLS:
        return _check_file_write_policy(name=name, arguments=args, mounts=mounts)
    return _check_file_read_policy(
        name=name,
        arguments=args,
        mounts=mounts,
        strict_file_context=strict_file_context,
    )


def _check_file_write_policy(
    *,
    name: str,
    arguments: dict[str, Any],
    mounts: tuple[FileContextMount, ...],
) -> FileContextPolicyDecision | None:
    if name == "generate_file":
        return None

    paths = _tool_paths(name, arguments)
    if not mounts:
        return FileContextPolicyDecision(
            allowed=False,
            code="file_context_write_denied",
            reason="This runtime surface has no writable file context mount.",
            tool_name=name,
            paths=paths,
        )

    if any(mount.patch_only for mount in mounts):
        if paths:
            patch_only_paths = tuple(
                path
                for path in paths
                if any(_path_matches_mount(path, mount) and mount.patch_only for mount in mounts)
            )
        else:
            patch_only_paths = ()
        if patch_only_paths or not any(mount.writable for mount in mounts):
            return FileContextPolicyDecision(
                allowed=False,
                code="file_context_patch_only",
                reason=(
                    "This file context is patch-only. The runtime may inspect the "
                    "mounted file and propose changes, but direct file writes are blocked."
                ),
                tool_name=name,
                paths=patch_only_paths or paths,
            )

    if any(_mount_grants_broad_file_scope(mount) and mount.writable for mount in mounts):
        return None

    if not paths:
        if any(mount.writable for mount in mounts):
            return None
        return FileContextPolicyDecision(
            allowed=False,
            code="file_context_write_denied",
            reason="This runtime surface does not have a writable file context mount.",
            tool_name=name,
            paths=paths,
        )

    denied_paths = tuple(
        path
        for path in paths
        if not any(mount.writable and _path_matches_mount(path, mount) for mount in mounts)
    )
    if denied_paths:
        return FileContextPolicyDecision(
            allowed=False,
            code="file_context_write_denied",
            reason="The requested file write is outside the mounted writable file context.",
            tool_name=name,
            paths=denied_paths,
        )
    return None


def _check_file_read_policy(
    *,
    name: str,
    arguments: dict[str, Any],
    mounts: tuple[FileContextMount, ...],
    strict_file_context: bool,
) -> FileContextPolicyDecision | None:
    if any(_mount_grants_broad_file_scope(mount) and mount.readable for mount in mounts):
        return None
    if not strict_file_context:
        return None

    paths = _tool_paths(name, arguments)
    if not paths:
        return FileContextPolicyDecision(
            allowed=False,
            code="file_context_read_denied",
            reason="This file-context surface requires reads to name an explicitly mounted file.",
            tool_name=name,
            paths=paths,
        )

    denied_paths = tuple(
        path
        for path in paths
        if not any(mount.readable and _path_matches_mount(path, mount) for mount in mounts)
    )
    if denied_paths:
        return FileContextPolicyDecision(
            allowed=False,
            code="file_context_read_denied",
            reason="The requested file read is outside the mounted file context.",
            tool_name=name,
            paths=denied_paths,
        )
    return None


def _tool_paths(name: str, arguments: dict[str, Any]) -> tuple[str, ...]:
    if name in {"read_file", "write_file", "edit_file", "delete_file"}:
        return _clean_paths(arguments.get("path") or arguments.get("file_path"))
    if name == "list_files":
        return _clean_paths(arguments.get("path"))
    if name == "grep_files":
        base = _clean_paths(arguments.get("path"))
        file_glob = str(arguments.get("file_glob") or "").strip()
        if base and file_glob:
            return tuple(_join_rel_path(path, file_glob) for path in base)
        return base
    if name == "glob_files":
        return _clean_paths(arguments.get("pattern"))
    if name in {"sandbox_write_file", "sandbox_save_result"}:
        return _clean_paths(
            arguments.get("workspace_path")
            or arguments.get("path")
            or arguments.get("file_path")
            or arguments.get("filename")
        )
    return ()


def _clean_context_text(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_editor_ui_route(path: str) -> bool:
    clean = path.strip().split("?", 1)[0].split("#", 1)[0].lower()
    if clean.startswith(("http://", "https://")):
        return True
    return clean.startswith(EDITOR_UI_ROUTE_PREFIXES)


def _clean_paths(raw: Any) -> tuple[str, ...]:
    values = raw if isinstance(raw, (list, tuple, set)) else (raw,)
    paths: list[str] = []
    for value in values:
        path = _normalize_rel_path(str(value or ""))
        if path:
            paths.append(path)
    return tuple(dict.fromkeys(paths))


def _normalize_rel_path(path: str) -> str:
    clean = path.strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return "/".join(part for part in clean.split("/") if part and part != ".")


def _join_rel_path(base: str, child: str) -> str:
    base = _normalize_rel_path(base)
    child = _normalize_rel_path(child)
    if not base:
        return child
    if not child:
        return base
    return f"{base.rstrip('/')}/{child}"


def _path_matches_mount(path: str, mount: FileContextMount) -> bool:
    if _mount_grants_broad_file_scope(mount):
        return True
    mount_path = _normalize_rel_path(mount.path)
    path = _normalize_rel_path(path)
    if not mount_path or not path:
        return False
    if mount.kind == "current_editor_file":
        return path == mount_path or path == "current_editor_file"
    if any(ch in path for ch in "*?["):
        return fnmatch(mount_path, path)
    return path == mount_path or path.startswith(f"{mount_path.rstrip('/')}/")


def _mount_grants_broad_file_scope(mount: FileContextMount) -> bool:
    return mount.kind == "workspace_artifact" and mount.path.startswith("workspace:")
