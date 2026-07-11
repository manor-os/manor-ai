from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.requests import ChannelRuntimeContext
from packages.core.ai.runtime.surfaces import ChatSurface, normalize_surface


async def runtime_run_chat_turn(
    message: str | list[dict],
    conversation_id: str,
    *,
    surface: ChatSurface | str,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    db: AsyncSession | None = None,
    manual_skill_refs: list[dict] | None = None,
    blocked_tools: list[str] | tuple[str, ...] | set[str] | str | None = None,
    editor_context: dict | None = None,
    runtime_metadata: dict | None = None,
) -> dict:
    """Run a non-streaming chat turn through an explicit Runtime surface."""

    resolved_surface = normalize_surface(surface)
    if resolved_surface is None:
        raise ValueError("Runtime chat turn requires an explicit surface")

    from packages.core.services.chat_service import run_chat_message

    return await run_chat_message(
        message,
        conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        db=db,
        manual_skill_refs=manual_skill_refs,
        blocked_tools=blocked_tools,
        editor_context=editor_context,
        runtime_metadata=runtime_metadata,
        runtime_surface=resolved_surface,
    )


async def runtime_stream_chat_turn(
    message: str | list[dict],
    conversation_id: str | None,
    *,
    surface: ChatSurface | str,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    assistant_message_id: str | None = None,
    manual_skill_refs: list[dict] | None = None,
    disable_tools: bool = False,
    blocked_tools: list[str] | tuple[str, ...] | set[str] | str | None = None,
    editor_context: dict | None = None,
    channel_context: ChannelRuntimeContext | dict | None = None,
    runtime_metadata: dict | None = None,
    persist_messages: bool = True,
) -> AsyncGenerator[str, None]:
    """Stream a chat turn through an explicit Runtime surface."""

    resolved_surface = normalize_surface(surface)
    if resolved_surface is None:
        raise ValueError("Runtime chat stream requires an explicit surface")

    from packages.core.services.chat_service import stream_chat_response

    async for event in stream_chat_response(
        message,
        conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        assistant_message_id=assistant_message_id,
        manual_skill_refs=manual_skill_refs,
        disable_tools=disable_tools,
        blocked_tools=blocked_tools,
        editor_context=editor_context,
        channel_context=channel_context,
        runtime_metadata=runtime_metadata,
        persist_messages=persist_messages,
        runtime_surface=resolved_surface,
    ):
        yield event
