"""Runtime boundary for code projects: identity, layout, and atomic writes.

Tool modules (``code_project``, ``generate_file(kind='code')``) and other
runtime callers share this implementation so project rules live in one place.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from packages.core.ai.runtime.file_actions import (
    RuntimeFileCommitError,
    RuntimeFileProjectionError,
    RuntimeFileProjectionTransactionFactory,
    runtime_entity_file_root,
    runtime_entity_filesystem_mutation_lock,
    runtime_guard_file_mutation,
    runtime_guard_file_read_access,
    runtime_normalize_entity_file_path,
    runtime_user_visible_file_path,
)
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.services import code_projects as cp

MAX_LISTED_FILES = 500
MAX_HASHED_BYTES = 5 * 1024 * 1024


def clean_rel(path: str) -> str:
    """Normalize a project-relative path; empty when unsafe or hidden."""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        return ""
    rel = runtime_normalize_entity_file_path(raw)
    parts = [part for part in rel.split("/") if part]
    if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
        return ""
    return "/".join(parts)


def inside_root(root_abs: str, rel: str) -> str | None:
    candidate = os.path.realpath(os.path.join(root_abs, rel))
    root = os.path.realpath(root_abs)
    if candidate == root or not candidate.startswith(root + os.sep):
        return None
    return candidate


def sha256_file(abs_path: str) -> str:
    digest = hashlib.sha256()
    with open(abs_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_files(root_abs: str) -> list[tuple[str, str]]:
    """(project-relative path, absolute path) for every visible file."""
    found: list[tuple[str, str]] = []
    if not os.path.isdir(root_abs):
        return found
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path):
                continue
            found.append((os.path.relpath(abs_path, root_abs).replace(os.sep, "/"), abs_path))
    return sorted(found)


def project_payload(project, *, sites: list[dict] | None = None) -> dict[str, Any]:
    profile = cp.PROFILES.get(project.profile)
    payload = {
        "project_id": project.id,
        "name": project.name,
        "profile": project.profile,
        "root_path": project.root_path,
        "entry": project.entry,
        "workspace_id": project.workspace_id,
        "publish_targets": {
            target: cp.PUBLISH_TARGET_TOOLS[target]
            for target in (profile.publish_targets if profile else ())
        },
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }
    if sites is not None:
        payload["sites"] = sites
    return payload


async def workspace_home(entity_id: str, workspace_id: str | None) -> str | None:
    if not workspace_id:
        return None
    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
        workspace_storage_root,
    )

    base = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    return workspace_storage_root(base) or None


async def load_project(db, entity_id: str, project_id: str):
    from sqlalchemy import select

    from packages.core.models.code_project import CodeProject

    if not project_id:
        return None
    return await db.scalar(
        select(CodeProject).where(
            CodeProject.id == project_id,
            CodeProject.entity_id == entity_id,
            CodeProject.archived_at.is_(None),
        )
    )


async def sites_by_project(db, entity_id: str, project_ids: list[str]) -> dict[str, list[dict]]:
    from sqlalchemy import select

    from packages.core.config import get_settings
    from packages.core.models.site import Site

    if not project_ids:
        return {}
    domain = (get_settings().MANOR_SITES_DOMAIN or "").strip().lower()
    rows = (await db.scalars(
        select(Site).where(Site.entity_id == entity_id, Site.project_id.in_(project_ids))
    )).all()
    grouped: dict[str, list[dict]] = {}
    for site in rows:
        grouped.setdefault(site.project_id, []).append({
            "site_id": site.id,
            "url": f"https://{site.slug}.{domain}" if domain else "",
            "revision": site.revision,
            "status": site.status,
            "workspace_id": site.workspace_id,
        })
    return grouped



# ── write ───────────────────────────────────────────────────────────────────

@dataclass
class PlannedFile:
    rel: str                      # path inside the project / bundle
    entity_rel: str               # entity-relative target
    data: bytes | None = None     # text content or downloaded bytes
    source_abs: str | None = None # entity file to copy
    source_rel: str | None = None
    size: int = 0
    sha256: str = ""
    exists: bool = False


def root_is_lexical(entity_root: str, root_path: str) -> bool:
    """True when no component of the project root is a symlink."""
    real_root = os.path.realpath(entity_root)
    expected = os.path.normpath(os.path.join(real_root, root_path))
    return os.path.realpath(os.path.join(entity_root, root_path)) == expected


def _file_items(raw_files: Any) -> list[dict[str, Any]]:
    items = raw_files if isinstance(raw_files, list) else []
    for item in items:
        if not isinstance(item, dict):
            raise cp.CodeProjectError("each files[] entry must be an object")
    return items


def _item_source(item: dict[str, Any]) -> str:
    return str(item.get("source") or "").strip()


async def prefetch_external_sources(
    raw_files: Any,
    profile: cp.CodeProjectProfile,
) -> dict[str, bytes]:
    """Download public https sources before any filesystem lock is taken.

    Cheap checks (path and file type) run first so a rejected file never
    costs a download.
    """
    fetched: dict[str, bytes] = {}
    for item in _file_items(raw_files):
        source = _item_source(item)
        if not source or not cp.is_external_url(source) or source in fetched:
            continue
        rel = clean_rel(item.get("path"))
        if not rel:
            raise cp.CodeProjectError(f"invalid or hidden path: {item.get('path')!r}")
        cp.check_file_allowed(profile, rel, 0)
        fetched[source] = await cp.download_public_https(source, max_bytes=profile.max_file_bytes)
    return fetched


def _resolve_entity_source(entity_id: str, entity_root: str, source: str) -> tuple[str, str]:
    """Return (real entity-relative path, absolute path) of an importable file.

    The real path is what gets read and what the read ACL is checked against,
    so a symlink cannot launder a restricted file through a readable folder.
    """
    parsed = cp.parse_entity_fs_url(source)
    if parsed is not None:
        source_entity, rel = parsed
        if source_entity != entity_id:
            raise cp.CodeProjectError(f"{source}: links to another entity cannot be imported")
    else:
        rel = source
    rel = runtime_normalize_entity_file_path(rel)
    if not rel or not runtime_user_visible_file_path(rel):
        raise cp.CodeProjectError(f"{source}: not a readable Knowledge file")
    abs_path = inside_root(entity_root, rel)
    if not abs_path or not os.path.isfile(abs_path):
        raise cp.CodeProjectError(f"{source}: file not found")
    real_rel = os.path.relpath(abs_path, os.path.realpath(entity_root)).replace(os.sep, "/")
    if not runtime_user_visible_file_path(real_rel):
        raise cp.CodeProjectError(f"{source}: not a readable Knowledge file")
    return real_rel, abs_path


def plan_files(
    *,
    entity_id: str,
    entity_root: str,
    root_path: str,
    profile: cp.CodeProjectProfile,
    raw_files: Any,
    prefetched: dict[str, bytes],
    allow_content: bool = True,
) -> tuple[list[PlannedFile], list[str]]:
    """Resolve files to write; returns the plan and the source paths to read-guard."""
    root_abs = os.path.join(entity_root, root_path)
    planned: list[PlannedFile] = []
    seen: set[str] = set()
    read_paths: list[str] = []
    for item in _file_items(raw_files):
        rel = clean_rel(item.get("path"))
        if not rel:
            raise cp.CodeProjectError(f"invalid or hidden path: {item.get('path')!r}")
        if rel in seen:
            raise cp.CodeProjectError(f"duplicate path: {rel}")
        seen.add(rel)
        target_abs = inside_root(root_abs, rel)
        if target_abs is None:
            raise cp.CodeProjectError(f"{rel}: path must stay inside the project")
        entity_rel = f"{root_path}/{rel}"
        if not runtime_user_visible_file_path(entity_rel):
            raise cp.CodeProjectError(f"{rel}: cannot create hidden/system path")
        has_content = item.get("content") is not None
        source = _item_source(item)
        if has_content == bool(source) or (has_content and not allow_content):
            raise cp.CodeProjectError(f"{rel}: give exactly one of content or source")
        cp.check_file_allowed(profile, rel, 0)

        entry = PlannedFile(rel=rel, entity_rel=entity_rel)
        if has_content:
            if not cp.is_text_extension(os.path.splitext(rel)[1]):
                raise cp.CodeProjectError(
                    f"{rel}: binary files must be imported with source, not content"
                )
            entry.data = str(item["content"]).encode("utf-8")
        elif cp.is_external_url(source):
            entry.data = prefetched[source]
        else:
            entry.source_rel, entry.source_abs = _resolve_entity_source(entity_id, entity_root, source)
            read_paths.append(entry.source_rel)

        if entry.data is not None:
            entry.size = len(entry.data)
            entry.sha256 = hashlib.sha256(entry.data).hexdigest()
        else:
            entry.size = os.path.getsize(entry.source_abs)
            entry.sha256 = sha256_file(entry.source_abs)
        cp.check_file_allowed(profile, rel, entry.size)

        entry.exists = os.path.isfile(target_abs)
        expected = str(item.get("expected_sha256") or "").strip().lower()
        if expected:
            current = sha256_file(target_abs) if entry.exists else ""
            if current != expected:
                raise cp.CodeProjectError(f"{rel}: expected_sha256 does not match the current file")
        planned.append(entry)
    return planned, read_paths


def _plan_deletions(root_path: str, root_abs: str, raw_delete: Any) -> list[tuple[str, str]]:
    deletions: list[tuple[str, str]] = []
    for raw in raw_delete if isinstance(raw_delete, list) else []:
        rel = clean_rel(raw)
        abs_path = inside_root(root_abs, rel) if rel else None
        if not rel or abs_path is None:
            raise cp.CodeProjectError(f"invalid delete path: {raw!r}")
        if not os.path.isfile(abs_path):
            raise cp.CodeProjectError(f"{rel}: nothing to delete")
        deletions.append((f"{root_path}/{rel}", abs_path))
    return deletions


def _check_project_totals(
    root_abs: str,
    profile: cp.CodeProjectProfile,
    planned: list[PlannedFile],
    deleted_rels: list[str],
) -> None:
    sizes = {rel: os.path.getsize(abs_path) for rel, abs_path in project_files(root_abs)}
    for entry in planned:
        sizes[entry.rel] = entry.size
    for rel in deleted_rels:
        sizes.pop(rel, None)
    if len(sizes) > profile.max_files:
        raise cp.CodeProjectError(
            f"{len(sizes)} files exceeds the {profile.max_files} file limit of a {profile.id} project"
        )
    total = sum(sizes.values())
    if total > profile.max_total_bytes:
        raise cp.CodeProjectError(
            f"{total} bytes exceeds the {profile.max_total_bytes} byte project limit of a "
            f"{profile.id} project; host large media elsewhere and link to it"
        )


def _guard_payload(blocked: str) -> dict[str, Any]:
    """Return a guard refusal unchanged so a HITL envelope reaches the runtime."""
    try:
        payload = json.loads(blocked)
    except ValueError:
        return {"error": blocked}
    return payload if isinstance(payload, dict) else {"error": blocked}


async def guard_source_reads(entity_id: str, ctx, read_paths: list[str]) -> dict[str, Any] | None:
    if not read_paths:
        return None
    blocked = await runtime_guard_file_read_access(
        entity_id=entity_id,
        user_id=ctx.user_id,
        conversation_id=ctx.conversation_id,
        workspace_id=ctx.workspace_id,
        runtime_envelope=ctx.runtime_envelope,
        tool_name="code_project",
        paths=read_paths,
    )
    return _guard_payload(blocked) if blocked else None


async def stage_planned_file(transaction, entry: PlannedFile) -> str:
    if entry.data is not None:
        return await transaction.write_bytes(
            entry.entity_rel,
            entry.data,
            expected_content_sha256=entry.sha256,
            expected_size=entry.size,
            allow_empty=True,
        )
    return await transaction.copy_file(
        entry.entity_rel,
        entry.source_abs,
        expected_content_sha256=entry.sha256,
        expected_size=entry.size,
        allow_empty=True,
    )


def planned_file_payload(entry: PlannedFile, document_id: str | None) -> dict[str, Any]:
    return {
        "path": entry.rel,
        "size": entry.size,
        "sha256": entry.sha256,
        "created": not entry.exists,
        "document_id": document_id,
        **({"copied_from": entry.source_rel} if entry.source_rel else {}),
    }


async def write_code_project(
    entity_id: str,
    ctx,
    project,
    *,
    raw_files: Any,
    raw_delete: Any,
    approval_token: str | None,
) -> dict[str, Any]:
    """Apply one atomic batch behind exactly one approval.

    One tool call maps to one file operation: files (adds and overwrites) or
    deletions, never both, so a single approval token and a single Workspace
    receipt always describe the whole batch.
    """
    from packages.core.services.entity_fs import EntityFilesystemBusyError

    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return {"error": "Entity filesystem is not enabled"}
    if not root_is_lexical(entity_root, project.root_path):
        return {"error": "project folder is a symbolic link; it cannot be written"}
    has_files = bool(raw_files)
    has_deletes = bool(raw_delete)
    if has_files and has_deletes:
        return {"error": "write files and delete files in separate code_project calls"}
    if not has_files and not has_deletes:
        return {"error": "nothing to write: give files or delete"}
    profile = cp.get_profile(project.profile)
    root_abs = os.path.join(entity_root, project.root_path)
    try:
        prefetched = await prefetch_external_sources(raw_files, profile)
    except cp.CodeProjectError as exc:
        return {"error": str(exc)}

    try:
        async with runtime_entity_filesystem_mutation_lock(entity_root):
            try:
                planned, read_paths = plan_files(
                    entity_id=entity_id,
                    entity_root=entity_root,
                    root_path=project.root_path,
                    profile=profile,
                    raw_files=raw_files,
                    prefetched=prefetched,
                )
                deletions = _plan_deletions(project.root_path, root_abs, raw_delete)
                _check_project_totals(
                    root_abs,
                    profile,
                    planned,
                    [entity_rel.removeprefix(f"{project.root_path}/") for entity_rel, _ in deletions],
                )
            except cp.CodeProjectError as exc:
                return {"error": str(exc)}

            blocked = await guard_source_reads(entity_id, ctx, read_paths)
            if blocked:
                return blocked
            if planned:
                action = FileMutationAction.WRITE
                paths = [entry.entity_rel for entry in planned]
                preview = "\n".join(
                    f"{entry.rel} ({entry.size} bytes"
                    + (f", from {entry.source_rel}" if entry.source_rel else "")
                    + ")"
                    for entry in planned[:20]
                )
                approval_payload = {
                    "project_id": project.id,
                    "files": [
                        {"path": entry.entity_rel, "sha256": entry.sha256, "source": entry.source_rel}
                        for entry in planned
                    ],
                }
            else:
                action = FileMutationAction.DELETE
                paths = [entity_rel for entity_rel, _ in deletions]
                preview = "\n".join(paths[:20])
                approval_payload = {"project_id": project.id, "delete": paths}
            blocked = await runtime_guard_file_mutation(
                entity_id=entity_id,
                user_id=ctx.user_id,
                conversation_id=ctx.conversation_id,
                workspace_id=ctx.workspace_id,
                task_id=ctx.task_id,
                runtime_envelope=ctx.runtime_envelope,
                tool_name="code_project",
                action=action,
                paths=paths,
                approval_token=approval_token,
                content_preview=preview,
                approval_payload=approval_payload,
            )
            if blocked:
                return _guard_payload(blocked)

            written: list[dict[str, Any]] = []
            try:
                async with RuntimeFileProjectionTransactionFactory.create(entity_id) as transaction:
                    for entry in planned:
                        abs_target = await stage_planned_file(transaction, entry)
                        sync = await transaction.project_file(
                            abs_path=abs_target,
                            entity_root=entity_root,
                            source="ai_generated",
                            created_by=ctx.user_id or "ai-agent",
                            force=True,
                            workspace_id=ctx.workspace_id,
                            task_id=ctx.task_id,
                            agent_id=ctx.agent_id,
                            conversation_id=ctx.conversation_id,
                            user_id=ctx.user_id,
                            tool_name="code_project",
                            expected_content_sha256=entry.sha256,
                        )
                        written.append(planned_file_payload(entry, sync.document_id))
                    for entity_rel, abs_path in deletions:
                        transaction.delete_path(entity_rel, expected_resolved_path=abs_path)
                        await transaction.project_delete(entity_rel)
                    await transaction.commit()
            except (RuntimeFileCommitError, RuntimeFileProjectionError) as exc:
                return {"error": f"Project write was rolled back: {exc}"}
    except EntityFilesystemBusyError as exc:
        return {"error": f"The file system is busy; retry shortly ({exc})", "retryable": True}

    await touch_project(project.id)
    return {
        "project_id": project.id,
        "root_path": project.root_path,
        "written": written,
        "deleted": [entity_rel.removeprefix(f"{project.root_path}/") for entity_rel, _ in deletions],
        "validation": cp.validate_project(profile, root_abs),
    }


async def touch_project(project_id: str) -> None:
    from sqlalchemy import update

    from packages.core.database import async_session
    from packages.core.models.code_project import CodeProject

    async with async_session() as db:
        await db.execute(
            update(CodeProject)
            .where(CodeProject.id == project_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        await db.commit()



# ── create ──────────────────────────────────────────────────────────────────

def _require_same_workspace(project, workspace_id: str | None) -> None:
    """A folder registered to one Workspace is never silently re-homed."""
    if project is not None and project.workspace_id and project.workspace_id != (workspace_id or None):
        raise cp.CodeProjectError(
            "this folder is already a code project of another Workspace"
        )


async def find_or_adopt_code_project(
    *,
    entity_id: str,
    root_path: str,
    name: str,
    profile_id: str,
    workspace_id: str | None,
    user_id: str | None,
    entry: str | None = None,
):
    """Return the project registered at ``root_path``, registering it if needed."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from packages.core.database import async_session
    from packages.core.models.code_project import CodeProject

    root_path = runtime_normalize_entity_file_path(root_path).strip("/")
    async with async_session() as db:
        existing = await db.scalar(
            select(CodeProject).where(
                CodeProject.entity_id == entity_id,
                CodeProject.root_path == root_path,
            )
        )
        if existing is not None:
            _require_same_workspace(existing, workspace_id)
            if existing.archived_at is not None:
                existing.archived_at = None
                await db.commit()
            return existing, False
        project = CodeProject(
            entity_id=entity_id,
            workspace_id=workspace_id,
            name=name or os.path.basename(root_path) or "project",
            root_path=root_path,
            profile=profile_id,
            entry=entry,
            created_by_user_id=user_id,
        )
        db.add(project)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(
                select(CodeProject).where(
                    CodeProject.entity_id == entity_id,
                    CodeProject.root_path == root_path,
                )
            )
            _require_same_workspace(existing, workspace_id)
            return existing, False
        await db.refresh(project)
        return project, True


async def new_project_root(entity_id: str, entity_root: str, ctx, name: str) -> str:
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.code_project import CodeProject

    parent = cp.project_parent_dir(await workspace_home(entity_id, ctx.workspace_id))
    base = cp.project_slug(name)
    async with async_session() as db:
        taken = set((await db.scalars(
            select(CodeProject.root_path).where(
                CodeProject.entity_id == entity_id,
                CodeProject.root_path.like(f"{parent}/{base}%"),
            )
        )).all())
    for index in range(1, 1000):
        candidate = f"{parent}/{base}" if index == 1 else f"{parent}/{base}-{index}"
        if candidate not in taken and not os.path.exists(os.path.join(entity_root, candidate)):
            return candidate
    raise cp.CodeProjectError("could not allocate a project folder; choose another name")
