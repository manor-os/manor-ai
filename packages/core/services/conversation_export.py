"""Conversation export — generate markdown, text, or JSON exports."""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.output_policy import (
    runtime_public_assistant_message_content,
    runtime_public_tool_calls,
)
from packages.core.services.conversation_records import get_conversation, list_messages
from packages.core.services.conversation_visibility import (
    is_internal_file_permission_marker,
)


def _visible_export_messages(messages):
    return [
        msg for msg in messages
        if not (
            msg.role == "user"
            and is_internal_file_permission_marker(msg.content)
        )
    ]


def _public_export_content(message) -> str | None:
    return runtime_public_assistant_message_content(
        message.content,
        message.meta,
        getattr(message, "tool_calls", None),
    )


async def export_as_markdown(db: AsyncSession, conversation_id: str, entity_id: str) -> str:
    """Export a conversation as Markdown.

    Format:
    # Conversation: {title or "Untitled"}
    _Exported on {date}_

    ---

    **User** _{timestamp}_
    {message content}

    **Assistant** _{timestamp}_
    {message content}

    ---

    **User** _{timestamp}_
    ...
    """
    conv = await get_conversation(db, conversation_id, entity_id)
    if not conv:
        return ""

    messages = _visible_export_messages(await list_messages(db, conversation_id, limit=500))

    lines = [
        f"# Conversation: {conv.title or 'Untitled'}",
        f"_Exported on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = msg.role.capitalize()
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M") if msg.created_at else ""
        lines.append(f"**{role}** _{timestamp}_")
        lines.append("")
        lines.append(_public_export_content(msg) or "_[no content]_")

        public_tool_calls = runtime_public_tool_calls(msg.tool_calls)
        if public_tool_calls:
            lines.append("")
            lines.append("_Tool calls:_")
            if isinstance(public_tool_calls, list):
                for tc in public_tool_calls:
                    lines.append(f"- `{tc.get('name', 'unknown')}`")
            elif isinstance(public_tool_calls, dict):
                for name in public_tool_calls:
                    lines.append(f"- `{name}`")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


async def export_as_json(db: AsyncSession, conversation_id: str, entity_id: str) -> dict:
    """Export a conversation as structured JSON."""
    conv = await get_conversation(db, conversation_id, entity_id)
    if not conv:
        return {}

    messages = _visible_export_messages(await list_messages(db, conversation_id, limit=500))

    return {
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "channel": conv.channel,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        },
        "messages": [
            {
                "id": msg.id,
                "role": msg.role,
                "content": _public_export_content(msg),
                "tool_calls": runtime_public_tool_calls(msg.tool_calls),
                "token_usage": msg.token_usage,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }
            for msg in messages
        ],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "message_count": len(messages),
    }


async def export_as_text(db: AsyncSession, conversation_id: str, entity_id: str) -> str:
    """Export a conversation as plain text."""
    conv = await get_conversation(db, conversation_id, entity_id)
    if not conv:
        return ""

    messages = _visible_export_messages(await list_messages(db, conversation_id, limit=500))

    lines = [f"Conversation: {conv.title or 'Untitled'}", ""]

    for msg in messages:
        role = msg.role.upper()
        lines.append(f"[{role}]")
        lines.append(_public_export_content(msg) or "[no content]")
        lines.append("")

    return "\n".join(lines)
