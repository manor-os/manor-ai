from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime import runtime_generate_image_media


async def handle_image(
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
    if not prompt:
        return json.dumps({"error": "kind=image requires prompt"}, ensure_ascii=False)
    return await runtime_generate_image_media(
        entity_id=entity_id,
        user_id=user_id,
        prompt=prompt,
        name=name,
        params=params,
        workspace_id=kwargs.get("workspace_id"),
        task_id=kwargs.get("task_id"),
        agent_id=agent_id,
        conversation_id=conversation_id or kwargs.get("conversation_id"),
    )
