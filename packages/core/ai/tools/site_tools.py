"""publish_site — publish a static website folder/file to a public URL."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from packages.core.database_runtime import release_read_transaction

PUBLISH_SITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "publish_site",
        "description": (
            "Publish a static website to a public URL: a static_site code project "
            "(project_id), a folder whose root contains index.html, or a single .html "
            "file (path). Snapshot semantics: later file edits go live only after "
            "calling publish_site again for the same project or path, which keeps the "
            "same URL. A site is not connected to any Workspace unless workspace_id is "
            "given. Only "
            "browser-ready static files are served (HTML/CSS/JS/images/fonts/media); "
            "JSX/TSX/Vue sources, package.json and other build-tooling files are "
            "excluded from the published snapshot — generate browser-ready code and "
            "reference assets with RELATIVE paths only (never /api/v1/fs/... URLs, "
            "which fail the publish link check). Returns the public URL, the snapshot "
            "revision, and any files excluded from publishing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Code project to publish (from code_project); use instead of path.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Optional: connect the site to this Workspace. Adds the Workspace's "
                        "customer-service chat widget to every page and an intake flow for "
                        "each form kind found (<form data-manor-action=\"lead\"> or "
                        "\"subscription\"); analytics are always on and elements with "
                        "data-manor-event=\"name\" are tracked as interactions. Omit to "
                        "leave the connection unchanged."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Entity-relative path of the folder or .html file to publish, "
                        "e.g. the bundle_path returned by generate_file(kind='code')"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Human-readable site name (display only; the subdomain is "
                        "system-generated and cannot be chosen)"
                    ),
                },
            },
            "required": ["name"],
        },
    },
}


async def _publish_site_handler(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    from packages.core.services import site_publisher as sp

    path = str(kwargs.get("path") or "").strip()
    project_id = str(kwargs.get("project_id") or "").strip()
    workspace_id = str(kwargs.get("workspace_id") or "").strip() or None
    name = str(kwargs.get("name") or "").strip() or "Site"
    if not entity_id:
        return json.dumps({"published": False, "error": "no entity context"})
    if bool(path) == bool(project_id):
        return json.dumps({"published": False, "error": "give exactly one of project_id or path"})
    if not sp.sites_domain():
        # No sites domain means nothing published here is reachable; say so
        # instead of reporting a success with an empty URL.
        return json.dumps({
            "published": False,
            "error": (
                "site hosting is not configured on this deployment "
                "(MANOR_SITES_DOMAIN is unset), so published sites would have no "
                "reachable address. Ask an operator to configure it."
            ),
        })
    try:
        from packages.core.database import async_session
        from packages.core.models.code_project import CodeProject
        from packages.core.models.site import Site
        from packages.core.models.user import User
        from packages.core.permissions import effective_user_role_name
        from packages.core.services.code_projects import get_profile
        from packages.core.services.workspace_access import user_can_manage_workspace
        from packages.core.services.auth_service import get_user_membership
        from packages.core.services.external_collaboration_access import COLLABORATOR_MEMBERSHIP_ROLE
        from packages.core.services.site_access import (
            SitePublishAccessDenied,
            require_site_publish_access,
            user_can_manage_site,
        )
        from sqlalchemy import select

        async with async_session() as session:
            project = None
            if project_id:
                project = await session.scalar(
                    select(CodeProject).where(
                        CodeProject.id == project_id,
                        CodeProject.entity_id == entity_id,
                        CodeProject.archived_at.is_(None),
                    )
                )
                if project is None:
                    raise sp.SitePublishError(f"code project {project_id!r} not found")
                if "site" not in get_profile(project.profile).publish_targets:
                    raise sp.SitePublishError(
                        f"a {project.profile} project cannot be published as a site"
                    )
                path = project.root_path
            target = sp.resolve_publish_target(entity_id, path)
            if target is None:
                raise sp.SitePublishError(
                    f"{path!r} is not publishable: publish a folder whose root contains "
                    "index.html, or a single .html file"
                )
            user = await session.scalar(
                select(User).where(User.id == user_id, User.status == "active")
            )
            if user is None:
                raise SitePublishAccessDenied(
                    "An active user context is required to publish this site"
                )
            membership = await get_user_membership(
                session,
                user=user,
                entity_id=entity_id,
            )
            if membership is None or membership.status != "active":
                raise SitePublishAccessDenied(
                    "An active entity membership is required to publish this site"
                )
            if membership.role == COLLABORATOR_MEMBERSHIP_ROLE:
                # A public site is published under the company's name, outside
                # any Workspace an external collaborator works in.
                raise SitePublishAccessDenied(
                    "External collaborators cannot publish sites for this company"
                )
            actor = SimpleNamespace(
                id=user.id,
                entity_id=membership.entity_id,
                role=membership.role,
                email=user.email,
                display_name=user.display_name,
            )
            # Authorization is re-read against the completed snapshot below.
            await release_read_transaction(session)
            prepared = await asyncio.to_thread(
                sp.prepare_publication,
                entity_id,
                target,
            )
            try:
                await require_site_publish_access(
                    session,
                    user=actor,
                    target=target,
                    included_paths=prepared.included_source_paths,
                )
                existing_site = await session.scalar(
                    select(Site).where(
                        Site.entity_id == entity_id,
                        Site.source_path == target.root_rel,
                    )
                )
                if existing_site is not None and not await user_can_manage_site(
                    session,
                    user=actor,
                    site=existing_site,
                ):
                    raise SitePublishAccessDenied(
                        "Site management access is required to republish this site"
                    )
                if workspace_id is not None:
                    from packages.core.models.workspace import Workspace

                    same_entity = await session.scalar(
                        select(Workspace.id).where(
                            Workspace.id == workspace_id,
                            Workspace.entity_id == entity_id,
                            Workspace.deleted_at.is_(None),
                        )
                    )
                    if same_entity is None:
                        raise SitePublishAccessDenied(
                            "Only a Workspace owner can connect or move this site"
                        )
                    role = await effective_user_role_name(session, actor)
                    current = (existing_site.workspace_id if existing_site else None) or None
                    for needed in {workspace_id, current} - {None}:
                        if not await user_can_manage_workspace(
                            session,
                            workspace_id=needed,
                            user_id=user.id,
                            entity_role=role,
                        ):
                            raise SitePublishAccessDenied(
                                "Only a Workspace owner can connect or move this site"
                            )
                if project is None:
                    project = await session.scalar(
                        select(CodeProject).where(
                            CodeProject.entity_id == entity_id,
                            CodeProject.root_path == target.root_rel,
                        )
                    )
                connections = await _connections_for_publish(
                    session,
                    actor=actor,
                    entity_id=entity_id,
                    workspace_id=workspace_id or (
                        existing_site.workspace_id if existing_site else None
                    ),
                    existing=dict(existing_site.connections or {}) if existing_site else {},
                    prepared=prepared,
                )
                result = await sp.publish(
                    session,
                    entity_id=entity_id,
                    rel_path=target.root_rel,
                    name=name,
                    created_by_user_id=user.id,
                    manage_as_user=actor,
                    prepared=prepared,
                    workspace_id=workspace_id,
                    project_id=project.id if project is not None else None,
                    connections=connections,
                )
                prepared = None
            finally:
                if prepared is not None:
                    await asyncio.to_thread(prepared.cleanup)
    except (OSError, sp.SitePublishError, PermissionError) as e:
        return json.dumps({"published": False, "error": str(e)})

    payload: dict[str, Any] = {
        "published": True,
        "url": sp.site_public_url(result.site),
        "name": result.site.name,
        "site_id": result.site.id,
        "slug": result.site.slug,
        "revision": result.site.revision,
        "excluded_files": [f.rel for f in result.excluded],
        "project_id": result.site.project_id,
        "workspace_id": result.site.workspace_id,
        "connections": _connections_summary(result.site),
    }
    if (
        result.site.workspace_id is None
        and project is not None
        and project.workspace_id
    ):
        # Never connect implicitly: a site's Workspace is independent of where
        # its source lives. Surface the option instead.
        payload["connectable_workspace_id"] = project.workspace_id
    if result.excluded:
        payload["note"] = (
            "excluded_files were not published (not browser-ready static assets); "
            "the live site is a snapshot — call publish_site again after edits"
        )
    return json.dumps(payload)


async def _connections_for_publish(
    session,
    *,
    actor,
    entity_id: str,
    workspace_id: str | None,
    existing: dict[str, Any],
    prepared,
) -> dict[str, Any] | None:
    """Complete a Workspace connection: customer-service chat plus a flow per form kind.

    Only when the site is (or is being) connected to a Workspace the caller
    manages; otherwise the current connections stay exactly as they are.
    Existing chat and flows are reused, so republishing is idempotent.
    """
    if not workspace_id:
        return None
    from sqlalchemy import select

    from packages.core.models.workspace import Workspace
    from packages.core.permissions import effective_user_role_name
    from packages.core.services import site_connections, site_publisher as sp
    from packages.core.services.workspace_access import user_can_manage_workspace

    workspace = await session.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
            Workspace.status.in_(("active", "paused")),
        )
    )
    if workspace is None or not await user_can_manage_workspace(
        session,
        workspace_id=workspace.id,
        user_id=actor.id,
        entity_role=await effective_user_role_name(session, actor),
    ):
        return None
    provisioned = await site_connections.provision_site_connections(
        session,
        user=actor,
        workspace=workspace,
        features=sp.inspect_prepared_site_bridge(prepared),
    )
    # Keep a flow the owner chose earlier when this revision has no such form.
    for key in ("lead_workflow_binding_id", "subscription_workflow_binding_id"):
        if not provisioned.get(key) and existing.get(key):
            provisioned[key] = existing[key]
    provisioned["analytics_enabled"] = bool(existing.get("analytics_enabled", True))
    return provisioned


def _connections_summary(site) -> dict[str, Any]:
    connections = dict(site.connections or {})
    return {
        "analytics": bool(connections.get("analytics_enabled", True)),
        "customer_service_chat": bool(site.workspace_id and connections.get("customer_service_channel_config_id")),
        "lead_form_flow": bool(site.workspace_id and connections.get("lead_workflow_binding_id")),
        "subscription_form_flow": bool(site.workspace_id and connections.get("subscription_workflow_binding_id")),
    }


def get_tools():
    return [(PUBLISH_SITE_SCHEMA, _publish_site_handler)]
