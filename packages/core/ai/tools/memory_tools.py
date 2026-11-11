"""remember: save what the person told the AI to the right memory layer."""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

REMEMBER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "Save a lasting fact, preference, decision or correction the person stated, at once. "
            "Not one-off task instructions; update an entry a new statement replaces. "
            "org: company-wide (owners/admins). workspace: this Workspace. my_profile: name, role, contacts. "
            "my_preferences: how to work with them. my_private: health, money, relationships, anything "
            "not for others. Never passwords, keys or codes. Then end the reply with "
            "'Remembered (<target>): <entry>'. undo reverts your last save here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["org", "workspace", "my_profile", "my_preferences", "my_private"]},
                "action": {"type": "string", "enum": ["add", "update", "remove", "undo"]},
                "content": {"type": "string", "description": "One self-contained line."},
                "entry": {"type": "string", "description": "update/remove: part of the existing line."},
                "section": {"type": "string", "description": "add: Markdown heading to file under."},
            },
            "required": ["target", "action"],
        },
    },
}


async def _remember(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.database import async_session
    from packages.core.memory.layer_prompt import speaker_is_collaborator
    from packages.core.memory.layers import memory_scope_has_speaker_in_workspace, memory_turn_scope
    from packages.core.memory.remember import MemoryWriteRefused, remember_memory

    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id)
    entity_id = context.entity_id or entity_id
    if not entity_id:
        return json.dumps({"error": "No entity context available"})
    envelope = context.runtime_envelope
    async with async_session() as db:
        scope = memory_turn_scope(envelope, user_id=context.user_id)
        if memory_scope_has_speaker_in_workspace(scope):
            scope = memory_turn_scope(envelope, user_id=context.user_id, collaborator=await speaker_is_collaborator(
                db, entity_id=entity_id, user_id=scope.speaker_id,
            ))
        try:
            result = await remember_memory(
                db, scope, entity_id=entity_id,
                target=str(kwargs.get("target") or "").strip(), action=str(kwargs.get("action") or "add").strip(),
                content=str(kwargs.get("content") or ""), entry=str(kwargs.get("entry") or ""),
                section=str(kwargs.get("section") or ""),
                conversation_id=context.conversation_id or getattr(envelope, "conversation_id", None),
                run_id=context.runtime_run_id, agent_id=context.agent_id,
            )
        except MemoryWriteRefused as refused:
            return json.dumps({"error": str(refused)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def get_tools() -> list[tuple[dict, Any]]:
    return [(REMEMBER_SCHEMA, _remember)]
