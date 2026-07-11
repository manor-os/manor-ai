from __future__ import annotations

from typing import Any

from .office_common import handle_office_skill


async def handle_spreadsheet(
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
    return await handle_office_skill(
        skill="xlsx",
        default_subdir="spreadsheets",
        kind="spreadsheet",
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        prompt=prompt,
        name=name,
        params=params,
        kwargs=kwargs,
        agent_id=agent_id,
    )
