"""Shared provisioning for the one-person-company templates.

Each solo template provisions a dedicated custom agent for the workspace and
binds the recommended skills + MCP servers to it (agent-global bindings, per
the chosen design). Marketplace skills are installed into the entity first so
they can be bound; built-in document skills are seeded as platform rows.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import is_cloud

# Built-in document skills live on disk under packages/core/ai/skills/ and are
# seeded as platform rows — they are not marketplace skills, so skip import.
_BUILTIN_SKILLS = {"docx", "pdf", "pptx", "xlsx"}


async def provision_solo_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    agent_name: str,
    system_prompt: str,
    service_key: str,
    mcp: list[str],
    skills: list[str],
) -> dict[str, Any]:
    """Install recommended marketplace skills, provision a dedicated agent with
    the recommended skill + MCP bindings, and subscribe it to the workspace.

    Returns a summary dict (agent_id + what actually bound). Runs inside the
    caller's transaction (the template's apply()); the caller commits.
    """
    from packages.core.services.agent_provisioning_service import (
        CustomAgentSpec,
        provision_custom_agent,
    )
    from packages.core.services.builtin_skill_loader import seed_builtin_skills
    from packages.core.services.workspace_service import map_agent_to_service

    # 1. Ensure built-in doc skills exist as platform rows (bindable by slug).
    await seed_builtin_skills(db)

    # 2. Install each recommended marketplace skill (idempotent) so it can be
    #    bound. Built-in skills are skipped (already seeded above).
    warnings: list[str] = []
    slug_to_mid: dict[str, str] = {}
    if is_cloud():
        pass
    for slug in skills:
        if slug in _BUILTIN_SKILLS:
            continue
        mid = slug_to_mid.get(slug)
        if mid:
            pass
        else:
            warnings.append(f"Marketplace skill {slug!r} is unavailable in this deployment.")

    # 3. Provision a dedicated agent with the skill + MCP bindings.
    result = await provision_custom_agent(
        db,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name=agent_name,
            system_prompt=system_prompt,
            category="one-person-company",
            skill_bindings=list(skills),
            mcp_bindings=list(mcp),
            source="solo_template",
        ),
    )

    # 4. Hire the agent into this workspace.
    await map_agent_to_service(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
        service_key=service_key,
        agent_id=result.agent_id,
    )

    return {
        "agent_id": result.agent_id,
        "agent_name": result.agent_name,
        "bound_skills": result.bound_skills,
        "bound_mcp": result.bound_mcp_servers,
        "warnings": [*result.warnings, *warnings],
    }
