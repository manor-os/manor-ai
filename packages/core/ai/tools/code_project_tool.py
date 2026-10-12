"""code_project — durable code projects agents can keep adding files to.

A project is a folder with a stable id. Agents find it again with
``list``, inspect it with ``get``, and add, overwrite, import or delete files
with ``write`` in any later task or conversation. A website is one kind of
project (profile ``static_site``) whose publish target is ``publish_site``.
"""
from __future__ import annotations

import json
import os
from typing import Any

from packages.core.ai.runtime import code_projects as rcp
from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_guard_file_read_access,
    runtime_normalize_entity_file_path,
    runtime_user_visible_file_path,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.services import code_projects as cp

ACTIONS = ("list", "get", "create", "write")

CODE_PROJECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "code_project",
        "description": (
            "Create and keep editing code projects (websites, apps, scripts, data). "
            "A project has a stable project_id: call action='list' first and reuse an "
            "existing project instead of starting a new folder. action='create' makes an "
            "empty project; action='write' then adds or overwrites files in one atomic "
            "batch (or deletes files, in a separate call); a file is either text "
            "{path, content} or an imported asset {path, source}, where source is a "
            "Knowledge path, a /api/v1/fs/... link, or a public https:// URL (images, "
            "fonts, media, PDFs). Reference assets from code with RELATIVE paths. "
            "Profiles: " + "; ".join(
                f"{profile.id} — {profile.description}" for profile in cp.PROFILES.values()
            ) + ". To put generated media in a project, call generate_file(kind='image'|"
            "'audio'|'video') and then write {path, source: <returned fs_path>}. Publish a "
            "static_site project with publish_site(project_id=...)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "project_id": {
                    "type": "string",
                    "description": "Target project for get/write.",
                },
                "name": {"type": "string", "description": "Project name (create)."},
                "profile": {
                    "type": "string",
                    "enum": sorted(cp.PROFILES),
                    "description": "Project profile (create).",
                },
                "entry": {
                    "type": "string",
                    "description": "Entry file, e.g. index.html (create; optional).",
                },
                "adopt_path": {
                    "type": "string",
                    "description": (
                        "create only: register an existing folder as a project in place "
                        "instead of making a new one."
                    ),
                },
                "files": {
                    "type": "array",
                    "description": "Files to add or overwrite (write).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path inside the project."},
                            "content": {"type": "string", "description": "Text content."},
                            "source": {
                                "type": "string",
                                "description": (
                                    "Copy bytes from a Knowledge path, a /api/v1/fs/... "
                                    "link, or a public https:// URL."
                                ),
                            },
                            "expected_sha256": {
                                "type": "string",
                                "description": "Reject the write if the current file differs.",
                            },
                        },
                        "required": ["path"],
                    },
                },
                "delete": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project-relative paths to delete (write; not together with files).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["workspace", "all"],
                    "description": "list: current Workspace only (default in a Workspace) or all.",
                },
            },
            "required": ["action"],
        },
    },
}

def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


# ── list / get ──────────────────────────────────────────────────────────────

async def _handle_list(entity_id: str, ctx, kwargs: dict[str, Any]) -> str:
    from sqlalchemy import or_, select

    from packages.core.database import async_session
    from packages.core.models.code_project import CodeProject

    scope = str(kwargs.get("scope") or "").strip() or ("workspace" if ctx.workspace_id else "all")
    stmt = select(CodeProject).where(
        CodeProject.entity_id == entity_id,
        CodeProject.archived_at.is_(None),
    )
    if ctx.workspace_id and (scope == "workspace" or not ctx.user_id):
        # User-less system runs never see beyond their own Workspace.
        stmt = stmt.where(CodeProject.workspace_id == ctx.workspace_id)
    elif ctx.user_id:
        # Outside a Workspace, only show projects this user made or that live
        # in the Workspace this run belongs to; file ACLs still apply on get.
        visible = [CodeProject.created_by_user_id == ctx.user_id]
        if ctx.workspace_id:
            visible.append(CodeProject.workspace_id == ctx.workspace_id)
        stmt = stmt.where(or_(*visible))
    stmt = stmt.order_by(CodeProject.updated_at.desc()).limit(100)
    async with async_session() as db:
        projects = (await db.scalars(stmt)).all()
        sites = await rcp.sites_by_project(db, entity_id, [project.id for project in projects])
    return json.dumps({
        "projects": [rcp.project_payload(project, sites=sites.get(project.id, [])) for project in projects],
        "scope": scope,
    }, ensure_ascii=False)


async def _attach_site_analytics(db, entity_id: str, sites: list[dict]) -> None:
    """Last 30 days of traffic, form and chat activity for each published site."""
    from packages.core.models.site import Site
    from packages.core.services.site_analytics import site_analytics_summary

    for entry in sites:
        site = await db.get(Site, entry["site_id"])
        if site is not None and site.entity_id == entity_id:
            summary = await site_analytics_summary(db, site, days=30)
            entry["analytics_30d"] = {key: value for key, value in summary.items() if key not in {"site_id", "days"}}


async def _handle_get(entity_id: str, ctx, kwargs: dict[str, Any]) -> str:
    from packages.core.database import async_session

    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return _error("Entity filesystem is not enabled")
    async with async_session() as db:
        project = await rcp.load_project(db, entity_id, str(kwargs.get("project_id") or ""))
        if project is None:
            return _error("project_not_found", hint="Call code_project(action='list') for project ids.")
        sites = await rcp.sites_by_project(db, entity_id, [project.id])
        await _attach_site_analytics(db, entity_id, sites.get(project.id, []))
    root_abs = os.path.join(entity_root, project.root_path)
    files = rcp.project_files(root_abs)
    listed = files[:rcp.MAX_LISTED_FILES]
    if files:
        blocked = await runtime_guard_file_read_access(
            entity_id=entity_id,
            user_id=ctx.user_id,
            conversation_id=ctx.conversation_id,
            workspace_id=ctx.workspace_id,
            runtime_envelope=ctx.runtime_envelope,
            tool_name="code_project",
            paths=[f"{project.root_path}/{rel}" for rel, _ in files],
        )
        if blocked:
            return blocked
    entries = []
    for rel, abs_path in listed:
        size = os.path.getsize(abs_path)
        entries.append({
            "path": rel,
            "size": size,
            "sha256": rcp.sha256_file(abs_path) if size <= rcp.MAX_HASHED_BYTES else None,
        })
    profile = cp.get_profile(project.profile)
    return json.dumps({
        **rcp.project_payload(project, sites=sites.get(project.id, [])),
        "files": entries,
        "file_count": len(files),
        "total_bytes": sum(os.path.getsize(abs_path) for _, abs_path in files),
        "truncated": len(files) > len(listed),
        "validation": cp.validate_project(profile, root_abs),
    }, ensure_ascii=False)


async def _handle_write(entity_id: str, ctx, kwargs: dict[str, Any]) -> str:
    from packages.core.database import async_session

    async with async_session() as db:
        project = await rcp.load_project(db, entity_id, str(kwargs.get("project_id") or ""))
    if project is None:
        return _error("project_not_found", hint="Call code_project(action='list') for project ids.")
    payload = await rcp.write_code_project(
        entity_id,
        ctx,
        project,
        raw_files=kwargs.get("files"),
        raw_delete=kwargs.get("delete"),
        approval_token=kwargs.get("approval_token"),
    )
    return json.dumps(payload, ensure_ascii=False)


async def _handle_create(entity_id: str, ctx, kwargs: dict[str, Any]) -> str:
    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return _error("Entity filesystem is not enabled")
    name = str(kwargs.get("name") or "").strip()
    try:
        profile = cp.get_profile(kwargs.get("profile"))
    except cp.CodeProjectError as exc:
        return _error(str(exc))
    entry = rcp.clean_rel(kwargs.get("entry")) or profile.default_entry
    adopt = str(kwargs.get("adopt_path") or "").strip()
    try:
        if adopt:
            root_path = runtime_normalize_entity_file_path(adopt).strip("/")
            abs_root = rcp.inside_root(entity_root, root_path) if root_path else None
            if not abs_root or not os.path.isdir(abs_root) or not runtime_user_visible_file_path(root_path):
                return _error(f"adopt_path is not an existing visible folder: {adopt!r}")
            if not rcp.root_is_lexical(entity_root, root_path):
                return _error("adopt_path is a symbolic link; adopt the real folder instead")
            files = [f"{root_path}/{rel}" for rel, _ in rcp.project_files(abs_root)]
            if files:
                blocked = await runtime_guard_file_read_access(
                    entity_id=entity_id,
                    user_id=ctx.user_id,
                    conversation_id=ctx.conversation_id,
                    workspace_id=ctx.workspace_id,
                    runtime_envelope=ctx.runtime_envelope,
                    tool_name="code_project",
                    paths=files,
                )
                if blocked:
                    return blocked
        else:
            if not name:
                return _error("name is required to create a project")
            root_path = None
        attempts = 1 if adopt else 3
        for _ in range(attempts):
            if not adopt:
                root_path = await rcp.new_project_root(entity_id, entity_root, ctx, name)
            project, created = await rcp.find_or_adopt_code_project(
                entity_id=entity_id,
                root_path=root_path,
                name=name,
                profile_id=profile.id,
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
                entry=entry,
            )
            if created or adopt:
                break
            # Another create claimed the same new folder first; take the next one.
        else:
            return _error("could not allocate a project folder; retry")
    except cp.CodeProjectError as exc:
        return _error(str(exc))
    return json.dumps({
        "created": created,
        **rcp.project_payload(project),
        "next_step": (
            f"Add files with code_project(action='write', project_id='{project.id}', files=[...])."
        ),
    }, ensure_ascii=False)


# ── entry point ─────────────────────────────────────────────────────────────

_DISPATCH = {
    "list": _handle_list,
    "get": _handle_get,
    "create": _handle_create,
    "write": _handle_write,
}


async def _code_project_handler(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    action = str(kwargs.get("action") or "").strip()
    handler = _DISPATCH.get(action)
    if handler is None:
        return _error(f"unknown action {action!r}", actions=list(ACTIONS))
    if not entity_id:
        return _error("no entity context")
    ctx = runtime_tool_call_context_from_kwargs(
        kwargs,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    return await handler(entity_id, ctx, kwargs)


def get_tools():
    return [(CODE_PROJECT_SCHEMA, _code_project_handler)]
