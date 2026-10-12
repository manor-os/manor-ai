from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import zipfile
from typing import Any

from packages.core.ai.runtime import runtime_generated_file_metadata
from packages.core.ai.runtime.file_actions import (
    RuntimeFileCommitError,
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_mutation,
    runtime_normalize_entity_file_path,
    runtime_user_visible_file_path,
)
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.contracts.file_engine import TEXT_CONTENT_TYPES, file_type_from_path

from . import common
from packages.core.services.file_refs import file_ref
from packages.core.services.generated_file_refs import entity_fs_open_url
from packages.core.services.workspace_layout import WorkspaceArtifactDir

_FILE_REF_ITEM_KEYS = ("name", "fs_path", "document_id", "open_url", "markdown_link")


def _file_ref_keys(entity_id: str, rel_path: str, document_id: Any) -> dict[str, Any]:
    """The FileRef keys that enrich one ``files[]`` receipt item in place."""
    ref = file_ref(entity_id=entity_id, fs_path=rel_path, document_id=document_id)
    return {key: ref[key] for key in _FILE_REF_ITEM_KEYS if ref.get(key)}


logger = logging.getLogger(__name__)

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
    # An explicit name is kept verbatim on purpose: recipes address their own
    # bundles by literal path ("Product Videos/product-video-<id>" and the
    # ledger/manifest paths beside it), so slugifying here would point the
    # follow-up steps at a directory that does not exist. Spaces are a display
    # problem, and chat fixes them where they are rendered - see
    # normalizeFileMarkdownLink in apps/web/src/lib/fileReferences.ts.
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
        if not isinstance(item, dict) or _asset_source(item):
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


def _single_file_from_name(name: str, content: Any) -> tuple[str, list[dict[str, str]]] | None:
    """``name="folder/file.ext"`` + ``content`` as one file in that folder."""
    if content is None:
        return None
    path = _clean_file_path(str(name or ""))
    folder, _, leaf = path.rpartition("/")
    if not folder or "." not in leaf.strip("."):
        return None
    return folder, [{"path": leaf, "content": _coerce_content(content, leaf)}]


def _asset_source(item: dict[str, Any]) -> str:
    if item.get("content") is not None or item.get("text") is not None or item.get("body") is not None:
        return ""
    return str(item.get("source") or item.get("source_path") or item.get("source_url") or "").strip()


def _extract_assets(params: dict[str, Any], kwargs: dict[str, Any]) -> list[dict[str, str]]:
    """Files imported by reference: ``files`` entries with a source, plus ``params.assets``."""
    entries: list[Any] = []
    for container in (kwargs, params):
        for key in ("files", "assets"):
            value = container.get(key)
            if isinstance(value, list):
                entries.extend(value)
    assets: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        source = _asset_source(item)
        path = str(item.get("path") or item.get("name") or "").strip()
        if source and path and path not in seen:
            seen.add(path)
            assets.append({"path": path, "source": source})
    return assets


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
    from packages.core.services.room_generation_scope import current_room_generation_scope

    group_scope = current_room_generation_scope()
    if group_scope is not None:
        from packages.core.services.room_file_mutation_tools import store_room_generated_bytes

        files = _extract_files(params, kwargs)
        if not files or len(files) > 100 or len({item["path"] for item in files}) != len(files):
            return json.dumps({"error": "invalid_group_code_bundle"})
        if sum(len(item["content"].encode("utf-8")) for item in files) > 20 * 1024 * 1024:
            return json.dumps({"error": "group_code_bundle_too_large"})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.writestr(item["path"], item["content"])
        requested = str(name or "code-project").strip()
        filename = f"{requested.rsplit('.', 1)[0]}.zip" if "." in requested else f"{requested}.zip"
        saved = await store_room_generated_bytes(
            scope=group_scope, name=filename, content=output.getvalue(),
            mime_type="application/zip", entity_id=entity_id,
        )
        return saved.json_result
    if not entity_id:
        return json.dumps({"error": "entity_id is required"}, ensure_ascii=False)
    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return json.dumps({"error": "Entity filesystem is not enabled"}, ensure_ascii=False)
    from packages.core.ai.runtime import code_projects as runtime_code_projects
    from packages.core.services import code_projects as project_rules

    assets = _extract_assets(params, kwargs)
    profile = project_rules.get_profile(_bundle_profile_id(params, kwargs, assets))
    try:
        # Downloads happen before the entity-wide lock is taken.
        prefetched = await runtime_code_projects.prefetch_external_sources(assets, profile)
    except project_rules.CodeProjectError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    async with runtime_entity_filesystem_mutation_lock(entity_root):
        result = await _handle_code_under_entity_lock(
            entity_id=entity_id,
            user_id=user_id,
            conversation_id=conversation_id,
            prompt=prompt,
            name=name,
            params=params,
            kwargs=kwargs,
            agent_id=agent_id,
            assets=assets,
            prefetched=prefetched,
            profile=profile,
        )
    payload = json.loads(result)
    if payload.get("created") is not True:
        return result
    try:
        payload = await _attach_code_project(
            payload,
            entity_id=entity_id,
            user_id=user_id,
            profile=profile,
            kwargs=kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - the bundle itself is already committed
        logger.warning("code bundle %s was written but not registered as a project: %s", payload.get("bundle_path"), exc)
        payload["project_error"] = f"bundle written, but project registration failed: {exc}"
    return json.dumps(payload, ensure_ascii=False)


def _bundle_profile_id(
    params: dict[str, Any],
    kwargs: dict[str, Any],
    assets: list[dict[str, str]],
) -> str:
    """Choose the bounded release profile explicitly or infer the usual code profile."""
    from packages.core.services import code_projects as project_rules

    if str(params.get("profile") or kwargs.get("profile") or "").strip() == "release_package":
        return "release_package"

    paths = [file["path"] for file in _extract_files(params, kwargs)] + [asset["path"] for asset in assets]
    entry = str(params.get("entry") or kwargs.get("entry") or "").strip()
    if not entry:
        entry = "index.html" if "index.html" in paths else (paths[0] if paths else "")
    allowed = project_rules.PROFILES["static_site"].writable_extensions
    if entry.lower().endswith((".html", ".htm")) and all(
        os.path.splitext(path)[1].lower() in allowed for path in paths
    ):
        return "static_site"
    return "generic"


async def _attach_code_project(
    payload: dict[str, Any],
    *,
    entity_id: str,
    user_id: str,
    profile,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Register the committed bundle as a code project and validate it (no writes)."""
    from packages.core.ai.runtime import code_projects as runtime_code_projects
    from packages.core.services import code_projects as project_rules

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    bundle = payload["bundle_path"]
    project, _created = await runtime_code_projects.find_or_adopt_code_project(
        entity_id=entity_id,
        root_path=bundle,
        name=os.path.basename(bundle),
        profile_id=profile.id,
        workspace_id=runtime_context.workspace_id,
        user_id=user_id or runtime_context.user_id,
        entry=payload["entry"].removeprefix(f"{bundle}/"),
    )
    if project is None:
        return payload
    payload["project_id"] = project.id
    payload["project_profile"] = project.profile
    entity_root = runtime_entity_file_root(entity_id)
    if entity_root:
        payload["validation"] = project_rules.validate_project(
            project_rules.get_profile(project.profile),
            os.path.join(entity_root, project.root_path),
        )
    payload["next_step"] = (
        f"Keep adding or editing files with code_project(action='write', project_id='{project.id}')."
    )
    return payload


async def _handle_code_under_entity_lock(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    kwargs: dict[str, Any],
    agent_id: str | None,
    assets: list[dict[str, str]] | None = None,
    prefetched: dict[str, bytes] | None = None,
    profile=None,
) -> str:
    from dataclasses import replace

    from packages.core.ai.runtime import code_projects as runtime_code_projects
    from packages.core.services import code_projects as project_rules

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)

    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return json.dumps({"error": "Entity filesystem is not enabled"}, ensure_ascii=False)
    if not entity_id:
        return json.dumps({"error": "entity_id is required"}, ensure_ascii=False)

    files = _extract_files(params, kwargs)
    if not files:
        # One file given the way kind=document takes it (a name with an
        # extension plus content): write that file in its folder.
        single = _single_file_from_name(name, kwargs.get("content", params.get("content")))
        if single:
            name, files = single
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
        task_id=runtime_context.task_id,
        name=_bundle_name(name or str(params.get("name") or ""), prompt),
        default_subdir=WorkspaceArtifactDir.CODE.value,
    )
    bundle = runtime_normalize_entity_file_path(bundle)
    if not bundle:
        bundle = _DEFAULT_BUNDLE_NAME

    entry = _clean_file_path(str(params.get("entry") or kwargs.get("entry") or ""))
    file_paths = {file["path"] for file in files}
    if not entry or entry not in file_paths:
        entry = "index.html" if any(file["path"] == "index.html" for file in files) else files[0]["path"]

    entity_root = os.path.normpath(entity_root)
    base_abs = os.path.normpath(os.path.join(entity_root, bundle))
    if base_abs == entity_root or not base_abs.startswith(entity_root + os.sep):
        return json.dumps({"error": "Path traversal detected"}, ensure_ascii=False)

    targets: list[tuple[str, str, str]] = []
    target_paths: set[str] = set()
    for file in files:
        rel_file = file["path"]
        if file_type_from_path(rel_file) not in TEXT_CONTENT_TYPES:
            return json.dumps({
                "error": "unsupported_code_file_type",
                "path": rel_file,
                "hint": "Code bundles contain text sources, not binary media or Office files.",
            }, ensure_ascii=False)
        rel_target = runtime_normalize_entity_file_path(f"{bundle}/{rel_file}")
        if not runtime_user_visible_file_path(rel_target):
            return json.dumps({"error": f"Cannot create hidden/system path: {rel_file}"}, ensure_ascii=False)
        abs_target = _safe_join(base_abs, rel_file)
        if not abs_target:
            return json.dumps({"error": f"Path traversal detected: {rel_file}"}, ensure_ascii=False)
        if rel_target in target_paths:
            return json.dumps(
                {"error": f"Duplicate code bundle path: {rel_file}"},
                ensure_ascii=False,
            )
        target_paths.add(rel_target)
        targets.append((rel_file, rel_target, abs_target))

    # Imported assets join the same approval and the same transaction as the
    # text files: one tool call, one file operation.
    asset_entries: list[Any] = []
    if assets:
        try:
            asset_entries, read_paths = runtime_code_projects.plan_files(
                entity_id=entity_id,
                entity_root=entity_root,
                root_path=bundle,
                profile=profile or project_rules.get_profile("generic"),
                raw_files=assets,
                prefetched=prefetched or {},
                allow_content=False,
            )
        except project_rules.CodeProjectError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        clashes = sorted({entry_.entity_rel for entry_ in asset_entries} & target_paths)
        if clashes:
            return json.dumps({"error": f"Duplicate code bundle path: {clashes[0]}"}, ensure_ascii=False)
        read_ctx = replace(
            runtime_context,
            user_id=user_id or runtime_context.user_id,
            conversation_id=conversation_id or runtime_context.conversation_id,
        )
        read_blocked = await runtime_code_projects.guard_source_reads(entity_id, read_ctx, read_paths)
        if read_blocked:
            return json.dumps(read_blocked, ensure_ascii=False)

    content_preview = "\n".join(
        [f"{file['path']} ({len(file['content'])} chars)" for file in files[:20]]
        + [f"{entry_.rel} ({entry_.size} bytes, from {entry_.source_rel or 'url'})" for entry_ in asset_entries[:20]]
    )
    blocked = await runtime_guard_file_mutation(
        entity_id=entity_id,
        user_id=user_id or runtime_context.user_id,
        conversation_id=conversation_id or runtime_context.conversation_id,
        workspace_id=runtime_context.workspace_id,
        task_id=runtime_context.task_id,
        runtime_envelope=runtime_context.runtime_envelope,
        tool_name="generate_file",
        action=FileMutationAction.CREATE_CODE_BUNDLE,
        paths=[rel_target for _, rel_target, _ in targets] + [entry_.entity_rel for entry_ in asset_entries],
        approval_token=kwargs.get("approval_token") or params.get("approval_token"),
        content_preview=content_preview,
        approval_payload={
            "bundle": bundle,
            "entry": entry,
            "files": [
                {
                    "path": file["path"],
                    "sha256": hashlib.sha256(file["content"].encode("utf-8")).hexdigest(),
                }
                for file in files
            ],
            **({"assets": [
                {"path": entry_.rel, "sha256": entry_.sha256, "source": entry_.source_rel}
                for entry_ in asset_entries
            ]} if asset_entries else {}),
        },
    )
    if blocked:
        return blocked

    written: list[dict[str, Any]] = []
    imported_assets: list[dict[str, Any]] = []
    try:
        async with RuntimeFileProjectionTransactionFactory.create(entity_id) as transaction:
            for file, (_, rel_target, _abs_target) in zip(files, targets):
                data = file["content"].encode("utf-8")
                content_sha256 = hashlib.sha256(data).hexdigest()
                abs_target = await transaction.write_bytes(
                    rel_target,
                    data,
                    expected_content_sha256=content_sha256,
                    expected_size=len(data),
                    allow_empty=True,
                )
                sync = await transaction.project_file(
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
                    expected_content_sha256=content_sha256,
                )
                meta = await runtime_generated_file_metadata(abs_target)
                written.append(
                    {
                        "path": rel_target,
                        "url": entity_fs_open_url(entity_id, rel_target),
                        "size": os.path.getsize(abs_target),
                        "source_sha256": meta["source_sha256"],
                        "mtime_ns": meta["mtime_ns"],
                        "knowledge_synced": True,
                        "document_id": sync.document_id,
                        "viewer_url": f"/viewer/{sync.document_id}",
                        "knowledge_sync_reason": sync.reason,
                        **_file_ref_keys(entity_id, rel_target, sync.document_id),
                    }
                )
            for entry_ in asset_entries:
                abs_target = await runtime_code_projects.stage_planned_file(transaction, entry_)
                sync = await transaction.project_file(
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
                    expected_content_sha256=entry_.sha256,
                )
                asset_receipt = runtime_code_projects.planned_file_payload(entry_, sync.document_id)
                asset_receipt["fs_path"] = entry_.entity_rel
                asset_receipt["url"] = entity_fs_open_url(entity_id, entry_.entity_rel)
                asset_receipt["viewer_url"] = f"/viewer/{sync.document_id}"
                asset_receipt.update(
                    _file_ref_keys(entity_id, entry_.entity_rel, sync.document_id)
                )
                imported_assets.append(asset_receipt)
            await transaction.commit()
    except RuntimeFileCommitError as exc:
        return json.dumps(
            {
                "error": f"Entity filesystem is not available: {exc}",
                "created": False,
            },
            ensure_ascii=False,
        )
    except RuntimeFileProjectionError as exc:
        return json.dumps(
            {
                "error": (
                    f"Code artifact '{exc.rel_path}' could not be registered in "
                    f"Knowledge and the bundle write was rolled back: {exc.reason}"
                ),
                "created": False,
                "knowledge_sync_reason": exc.reason,
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {"error": f"Code bundle was not committed: {exc}", "created": False},
            ensure_ascii=False,
        )

    entry_path = runtime_normalize_entity_file_path(f"{bundle}/{entry}")
    return json.dumps(
        {
            "created": True,
            "kind": "code",
            "bundle_path": bundle,
            **(
                {
                    "name": os.path.basename(bundle),
                    "summary": f"Created {len(written) + len(imported_assets)} release files, including supplied audio and cover artwork.",
                }
                if profile.id == "release_package"
                else {}
            ),
            "entry": entry_path,
            "entry_url": entity_fs_open_url(entity_id, entry_path),
            **({"assets": imported_assets} if imported_assets else {}),
            "files": written,
        },
        ensure_ascii=False,
    )
