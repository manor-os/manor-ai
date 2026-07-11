from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.entrypoints import runtime_request_for_chat_turn
from packages.core.ai.runtime.prompt_assembly import runtime_assemble_prompt_for_turn
from packages.core.ai.runtime.surfaces import ChatSurface


VOICE_DEFAULT_INSTRUCTIONS = "You are a helpful assistant. Be concise and conversational."


async def runtime_resolve_voice_chat_instructions(
    db: AsyncSession | None,
    *,
    entity_id: str,
    user_id: str,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Build no-tools realtime voice instructions through the Runtime Harness."""

    request = runtime_request_for_chat_turn(
        surface=ChatSurface.VOICE_CHAT,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        message="",
        legacy_path="runtime.voice.resolve_voice_chat_instructions",
        metadata={"disable_tools": True},
    )
    assembled = await runtime_assemble_prompt_for_turn(
        db,
        request=request,
        active_user_message="",
        tool_schemas=[],
        allowed_tool_names=set(),
        disable_tools=True,
    )
    system_prompt = (assembled.prompt or "").strip()
    if not system_prompt:
        return VOICE_DEFAULT_INSTRUCTIONS
    return system_prompt
