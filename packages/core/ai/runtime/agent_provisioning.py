"""Runtime-owned facade for agent provisioning actions."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def runtime_provision_agent_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Create a custom agent through the Runtime action boundary."""

    if not entity_id:
        return json.dumps({"ok": False, "error": "entity_id missing from tool context"})

    raw_params = dict(params or {})
    agent_name = (raw_params.get("agent_name") or "").strip()
    system_prompt = (raw_params.get("system_prompt") or "").strip()
    if not agent_name or not system_prompt:
        return json.dumps({"ok": False, "error": "agent_name and system_prompt are required"})

    try:
        from packages.core.database import async_session
        from packages.core.services.agent_provisioning_service import (
            CustomAgentSpec,
            provision_custom_agent,
        )

        spec = CustomAgentSpec(
            agent_name=agent_name,
            system_prompt=system_prompt,
            description=(raw_params.get("description") or "").strip(),
            category=raw_params.get("category"),
            tags=list(raw_params.get("tags") or []),
            tool_bindings=list(raw_params.get("tool_bindings") or []),
            skill_bindings=list(raw_params.get("skill_bindings") or []),
            mcp_bindings=list(raw_params.get("mcp_bindings") or []),
            missing_skill_specs=list(raw_params.get("missing_skill_specs") or []),
            source="chat_tool",
        )

        async with async_session() as db:
            try:
                result = await provision_custom_agent(db, entity_id=entity_id, spec=spec)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                raise exc

        return json.dumps({
            "ok": True,
            "agent_id": result.agent_id,
            "agent_name": result.agent_name,
            "bound_tools": result.bound_tools,
            "bound_skills": result.bound_skills,
            "created_skills": result.created_skills,
            "bound_mcp_servers": result.bound_mcp_servers,
            "warnings": result.warnings,
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("provision_agent failed")
        return json.dumps({"ok": False, "error": f"failed to provision agent: {exc}"})
