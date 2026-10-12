"""Read-only resource projections for the chat composer."""

from __future__ import annotations

from sqlalchemy import select

from packages.core.models.permission import Visibility
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_access import user_readable_workspace_ids


async def resource_workspace_names(db, resources, user) -> dict[str, str]:
    """A shared resource does not grant access to its home Workspace's name."""
    workspace_ids = {row.workspace_id for row in resources if row.workspace_id}
    if not workspace_ids:
        return {}
    workspaces = (
        (
            await db.execute(
                select(Workspace).where(
                    Workspace.id.in_(workspace_ids),
                    Workspace.entity_id == user.entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not workspaces:
        return {}
    readable_ids = await user_readable_workspace_ids(
        db,
        workspace_ids={workspace.id for workspace in workspaces},
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    return {
        workspace.id: workspace.name for workspace in workspaces
        if workspace.id in readable_ids
    }


async def list_composer_skills(db, user, *, workspace_id=None, agent_id=None):
    """Project callable candidates, preserving each installed resource's ID.

    Unrouted chat offers shared/personal resources and the current Workspace's
    resources. Selecting an Agent additionally admits its explicit bindings,
    including reusable Skills whose home is a different Workspace.
    """
    from packages.core.ai.runtime.skills import (
        runtime_master_uses_owner_chat_skill_scope,
        runtime_skill_is_eligible,
    )
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.builtin_plugin_service import load_user_builtin_plugin_preferences
    from packages.core.services.skill_service import (
        builtin_skill_copy_sources, list_skills, list_skills_for_agent,
    )

    owner_chat_master_scope = runtime_master_uses_owner_chat_skill_scope(
        agent_id=agent_id,
        user_id=user.id,
        surface=(
            ChatSurface.WORKSPACE_CHAT
            if workspace_id
            else ChatSurface.GLOBAL_OWNER_CHAT
        ),
    )
    uses_bound_agent_scope = bool(agent_id and not owner_chat_master_scope)
    skills = (
        await list_skills_for_agent(db, user.entity_id, agent_id, workspace_id=workspace_id)
        if uses_bound_agent_scope
        else await list_skills(db, user.entity_id)
    )
    preferences = await load_user_builtin_plugin_preferences(db, user.id)
    legacy_copies = builtin_skill_copy_sources(skills)
    visible = []
    seen = set()
    for skill in skills:
        if skill.id in seen or skill.id in legacy_copies:
            continue
        # is_public controls Agent bindings, not user-visible sharing. A home
        # Workspace does not confine an entity/public-visible reusable Skill.
        if (
            not uses_bound_agent_scope
            and skill.workspace_id
            and skill.workspace_id != workspace_id
            and (skill.visibility or Visibility.ENTITY) not in {Visibility.ENTITY, Visibility.PUBLIC}
        ):
            continue
        eligible, _ = await runtime_skill_is_eligible(
            db,
            skill,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            user_id=user.id,
            enforce_user_access=True,
            builtin_plugin_preferences=preferences,
        )
        if eligible:
            seen.add(skill.id)
            visible.append(skill)
    names = await resource_workspace_names(db, visible, user)
    return [
        {
            "id": skill.id,
            "entity_id": skill.entity_id,
            "workspace_id": skill.workspace_id,
            "workspace_name": names.get(skill.workspace_id),
            "visibility": skill.visibility,
            "name": skill.name,
            "slug": skill.slug,
            "display_name": skill.display_name,
            "description": skill.description,
            "category": skill.category,
            "tags": skill.tags or [],
            "env_vars": (skill.config or {}).get("env_vars") or [],
            "credentials_configured": bool((skill.config or {}).get("credentials_configured")),
            "config": {"description_i18n": (skill.config or {}).get("description_i18n")},
        }
        for skill in visible
    ]
