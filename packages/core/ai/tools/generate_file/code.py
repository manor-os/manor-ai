from __future__ import annotations

import json
import os
import re
from typing import Any

from packages.core.ai.runtime import runtime_generated_file_metadata
from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_guard_file_mutation,
    runtime_normalize_entity_file_path,
    runtime_sync_entity_file_to_knowledge,
    runtime_user_visible_file_path,
    runtime_write_entity_file_atomic,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

from . import common


_DEFAULT_BUNDLE_NAME = "code-project"


def _slugify_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", value.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned[:80] or _DEFAULT_BUNDLE_NAME


def _bundle_name(name: str, prompt: str) -> str:
    requested = str(name or "").strip()
    if not requested:
        requested = _slugify_name(prompt) if prompt else _DEFAULT_BUNDLE_NAME
    requested = requested.replace("\\", "/").strip("/")
    basename = os.path.basename(requested)
    if os.path.splitext(basename)[1]:
        requested = os.path.dirname(requested) or os.path.splitext(basename)[0]
    return requested or _DEFAULT_BUNDLE_NAME


def _coerce_content(value: Any, rel_path: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        if rel_path.lower().endswith(".json"):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _extract_files(params: dict[str, Any], kwargs: dict[str, Any]) -> list[dict[str, str]]:
    raw_files = kwargs.get("files")
    if raw_files is None:
        raw_files = params.get("files")
    if isinstance(raw_files, dict):
        raw_files = [
            {"path": path, "content": content}
            for path, content in raw_files.items()
        ]
    if not isinstance(raw_files, list):
        return []

    files: list[dict[str, str]] = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path") or item.get("name") or item.get("filename")
        rel_path = _clean_file_path(str(raw_path or ""))
        if not rel_path:
            continue
        content = item.get("content")
        if content is None:
            content = item.get("text")
        if content is None:
            content = item.get("body")
        files.append({"path": rel_path, "content": _coerce_content(content, rel_path)})
    return files


def _clean_file_path(path: str) -> str:
    rel = runtime_normalize_entity_file_path(path)
    if not rel or rel == "." or rel.startswith("../") or os.path.isabs(path):
        return ""
    parts = [part for part in rel.split("/") if part]
    if not parts:
        return ""
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        return ""
    return "/".join(parts)


def _safe_join(base_abs: str, rel_path: str) -> str | None:
    target = os.path.normpath(os.path.join(base_abs, rel_path))
    base = os.path.normpath(base_abs)
    if target == base or not target.startswith(base + os.sep):
        return None
    return target


async def handle_code(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    kwargs: dict[str, Any],
    agent_id: str | None,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)

    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return json.dumps({"error": "Entity filesystem is not enabled"}, ensure_ascii=False)
    if not entity_id:
        return json.dumps({"error": "entity_id is required"}, ensure_ascii=False)

    files = _extract_files(params, kwargs)
    if not files:
        return json.dumps(
            {
                "error": "kind=code requires params.files with {path, content} entries",
                "example": {
                    "name": "rental-website",
                    "params": {
                        "files": [
                            {"path": "index.html", "content": "<!doctype html>..."},
                            {"path": "styles.css", "content": "body {...}"},
                            {"path": "app.js", "content": "console.log('ready')"},
                        ]
                    },
                },
            },
            ensure_ascii=False,
        )

    bundle = await common._scope_workspace_output_name(
        entity_id=entity_id,
        workspace_id=kwargs.get("workspace_id"),
        name=_bundle_name(name or str(params.get("name") or ""), prompt),
        default_subdir="code",
    )
    bundle = runtime_normalize_entity_file_path(bundle)
    if not bundle:
        bundle = _DEFAULT_BUNDLE_NAME
    if kwargs.get("workspace_id") is None and "/" not in bundle:
        bundle = f"code/{bundle}"

    entry = _clean_file_path(str(params.get("entry") or kwargs.get("entry") or ""))
    file_paths = {file["path"] for file in files}
    if not entry or entry not in file_paths:
        entry = "index.html" if any(file["path"] == "index.html" for file in files) else files[0]["path"]

    entity_root = os.path.normpath(entity_root)
    base_abs = os.path.normpath(os.path.join(entity_root, bundle))
    if base_abs == entity_root or not base_abs.startswith(entity_root + os.sep):
        return json.dumps({"error": "Path traversal detected"}, ensure_ascii=False)

    targets: list[tuple[str, str, str]] = []
    for file in files:
        rel_file = file["path"]
        rel_target = runtime_normalize_entity_file_path(f"{bundle}/{rel_file}")
        if not runtime_user_visible_file_path(rel_target):
            return json.dumps({"error": f"Cannot create hidden/system path: {rel_file}"}, ensure_ascii=False)
        abs_target = _safe_join(base_abs, rel_file)
        if not abs_target:
            return json.dumps({"error": f"Path traversal detected: {rel_file}"}, ensure_ascii=False)
        targets.append((rel_file, rel_target, abs_target))

    content_preview = "\n".join(f"{file['path']} ({len(file['content'])} chars)" for file in files[:20])
    blocked = await runtime_guard_file_mutation(
        entity_id=entity_id,
        user_id=user_id or runtime_context.user_id,
        conversation_id=conversation_id or runtime_context.conversation_id,
        tool_name="generate_file",
        action="create_code_bundle",
        paths=[rel_target for _, rel_target, _ in targets],
        approval_token=kwargs.get("approval_token") or params.get("approval_token"),
        content_preview=content_preview,
    )
    if blocked:
        return blocked

    written: list[dict[str, Any]] = []
    for file, (_, rel_target, abs_target) in zip(files, targets):
        content = file["content"]
        data = content.encode("utf-8")
        try:
            abs_target = runtime_write_entity_file_atomic(
                entity_id,
                rel_target,
                data,
                expected_size=len(data),
                allow_empty=True,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"Entity filesystem is not available: {exc}"}, ensure_ascii=False)

        sync = await runtime_sync_entity_file_to_knowledge(
            entity_id=entity_id,
            abs_path=abs_target,
            entity_root=entity_root,
            source="ai_generated",
            created_by=user_id or runtime_context.user_id or "ai-agent",
            force=True,
            workspace_id=runtime_context.workspace_id,
            task_id=runtime_context.task_id,
            agent_id=agent_id or runtime_context.agent_id,
            conversation_id=conversation_id or runtime_context.conversation_id,
            user_id=user_id or runtime_context.user_id,
            tool_name="generate_file",
        )
        meta = await runtime_generated_file_metadata(abs_target)
        written.append(
            {
                "path": rel_target,
                "url": f"/api/v1/fs/{entity_id}/{rel_target}",
                "size": os.path.getsize(abs_target),
                "source_sha256": meta["source_sha256"],
                "mtime_ns": meta["mtime_ns"],
                "knowledge_synced": sync.synced,
                "document_id": sync.document_id,
                "knowledge_sync_reason": sync.reason,
            }
        )

    entry_path = runtime_normalize_entity_file_path(f"{bundle}/{entry}")
    return json.dumps(
        {
            "created": True,
            "kind": "code",
            "bundle_path": bundle,
            "entry": entry_path,
            "entry_url": f"/api/v1/fs/{entity_id}/{entry_path}",
            "files": written,
        },
        ensure_ascii=False,
    )
