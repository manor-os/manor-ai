from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import runtime_resolve_voice_chat_instructions


async def resolve_voice_chat_instructions(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Build realtime voice instructions through the Runtime Harness."""

    return await runtime_resolve_voice_chat_instructions(
        db,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
