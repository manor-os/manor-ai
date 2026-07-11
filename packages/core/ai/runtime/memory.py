from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.sources import (
    RUNTIME_CHAT_EXTRACTOR_SOURCE,
    RUNTIME_CONVERSATION_SUMMARY_SOURCE,
    RUNTIME_MEMORY_SOURCE,
)
from packages.core.ai.runtime.surfaces import ChatSurface


MemoryScope = Literal[
    "owner",
    "agent",
    "workspace",
    "customer",
    "organization_policy",
    "runtime_scratch",
]


@dataclass(frozen=True)
class MemoryMount:
    scope: MemoryScope
    key: str
    readable: bool = True
    writable: bool = False
    summary: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "readable": self.readable,
            "writable": self.writable,
            "summary": self.summary,
            "metadata": dict(self.metadata or {}),
        }


MEMORY_MOUNTS_METADATA_KEY = "runtime_memory_mounts"

RUNTIME_CHAT_INSIGHT_EXTRACTOR_SYSTEM_PROMPT = """\
You read a workspace's recent chat history and extract durable insights
worth remembering for future planning. Be VERY conservative — most chat
is conversational and should produce zero entries. Only emit an entry
when the operator clearly signaled a long-lived preference, decision,
fact, or piece of guidance.

DO NOT extract:
  - status updates ("done", "looks good", "let me check")
  - one-off requests ("send this email", "create a task")
  - the agent's own messages
  - questions or speculation
  - anything that's already implied by an existing tool / setting

DO extract (each as a separate entry):
  - "always X" / "never Y" / "prefer Z over W"  → guidance
  - "we picked X because Y"                     → decision
  - "our customers are X" / "X is true"         → fact
  - "I don't want to be bothered when X"        → preference

Output valid JSON ONLY (no prose, no markdown):

{
  "entries": [
    {
      "scope": "guidance" | "decision" | "fact" | "preference",
      "title": "Short imperative title (≤80 chars)",
      "body": "1-3 sentence explanation, in the operator's voice",
      "tags": ["..."],
      "source_message_id": "<id of the chat message it came from>",
      "confidence": 0.5 to 0.8
    }
  ]
}

Use empty entries [] when nothing is worth saving. Confidence above 0.8
is reserved for the operator's manual entries — never exceed 0.8.
"""


def memory_mounts_to_trace(mounts: list[MemoryMount]) -> tuple[dict[str, Any], ...]:
    return tuple(mount.to_trace_dict() for mount in mounts)


def runtime_chat_insight_payload(
    messages: list[Any],
    *,
    max_body_chars: int = 800,
) -> str:
    """Build the Runtime-owned payload for chat insight extraction."""

    lines = ["# Recent operator messages\n"]
    for message in messages:
        created_at = getattr(message, "created_at", None)
        ts = created_at.isoformat() if created_at else "?"
        body = str(getattr(message, "content", "") or "").strip()
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "…"
        lines.append(f"## msg id={getattr(message, 'id', '')}  ts={ts}\n{body}\n")
    return "\n".join(lines)


def runtime_chat_insight_extractor_messages(payload: str) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for chat insight extraction."""

    return [
        {"role": "system", "content": RUNTIME_CHAT_INSIGHT_EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": payload},
    ]


async def runtime_execute_chat_insight_extraction_completion(
    *,
    entity_id: str,
    workspace_id: str,
    payload: str,
) -> RuntimeTextCompletionResult:
    """Execute chat insight extraction with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_chat_insight_extractor_messages(payload),
        entity_id=entity_id,
        source=RUNTIME_CHAT_EXTRACTOR_SOURCE,
        workspace_id=workspace_id,
        temperature=0.2,
    )


def runtime_conversation_memory_text(messages: list[Any]) -> str:
    """Build compact conversation text for legacy agent memory extraction."""

    return "\n".join(
        f"{getattr(message, 'role', '')}: {getattr(message, 'content', '')}"
        for message in messages
        if getattr(message, "content", None)
    )


def runtime_conversation_memory_extraction_prompt(
    conversation_text: str,
    *,
    max_chars: int = 8000,
) -> str:
    """Build the Runtime-owned prompt for conversation memory extraction."""

    return (
        "Analyze this conversation and extract key facts worth remembering "
        "for future conversations.\n\n"
        "Rules:\n"
        "- Extract facts about the user, their preferences, business context\n"
        "- Each memory should be a single, concise statement\n"
        "- Skip greetings, small talk, and already-obvious information\n"
        '- Return as JSON array: [{"type": "fact|preference|context|instruction", '
        '"content": "...", "importance": 1-10}]\n'
        "- Return empty array if nothing worth remembering\n\n"
        f"Conversation:\n{conversation_text[:max_chars]}"
    )


def runtime_conversation_memory_extraction_messages(
    conversation_text: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for legacy conversation memory extraction."""

    return [
        {
            "role": "user",
            "content": runtime_conversation_memory_extraction_prompt(conversation_text),
        }
    ]


async def runtime_execute_conversation_memory_extraction_completion(
    *,
    entity_id: str,
    conversation_text: str,
) -> RuntimeTextCompletionResult:
    """Execute legacy conversation memory extraction with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_conversation_memory_extraction_messages(conversation_text),
        entity_id=entity_id,
        source=RUNTIME_MEMORY_SOURCE,
        temperature=0.3,
    )


def runtime_conversation_summary_text(
    dropped_messages: list[Any],
    *,
    max_messages: int = 30,
    max_chars_per_message: int = 500,
) -> str:
    """Build compact dropped-message text for rolling conversation summaries."""

    parts = []
    for message in dropped_messages[-max_messages:]:
        role = str(getattr(message, "role", "") or "").upper()
        content = str(getattr(message, "content", "") or "")[:max_chars_per_message]
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def runtime_conversation_summary_prompt(text_block: str) -> str:
    """Build the Runtime-owned prompt for rolling conversation summaries."""

    return (
        "Summarize this conversation history in 3-5 sentences. "
        "Capture the key topics discussed, decisions made, and any pending items. "
        "Be concise and factual.\n\n"
        f"{text_block}"
    )


def runtime_conversation_summary_messages(text_block: str) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for rolling conversation summaries."""

    return [{"role": "user", "content": runtime_conversation_summary_prompt(text_block)}]


async def runtime_execute_conversation_summary_completion(
    *,
    entity_id: str | None,
    workspace_id: str | None,
    text_block: str,
) -> RuntimeTextCompletionResult:
    """Execute rolling conversation summary with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_conversation_summary_messages(text_block),
        entity_id=entity_id,
        source=RUNTIME_CONVERSATION_SUMMARY_SOURCE,
        workspace_id=workspace_id,
        temperature=0.3,
        max_tokens=300,
    )


def memory_mounts_from_envelope(envelope) -> tuple[MemoryMount, ...]:
    metadata = getattr(envelope, "metadata", None)
    raw_mounts = (
        metadata.get(MEMORY_MOUNTS_METADATA_KEY)
        if isinstance(metadata, dict)
        else None
    )
    mounts: list[MemoryMount] = []
    if isinstance(raw_mounts, (list, tuple)):
        for raw in raw_mounts:
            if not isinstance(raw, dict):
                continue
            scope = raw.get("scope")
            key = str(raw.get("key") or "").strip()
            if not scope or not key:
                continue
            mounts.append(
                MemoryMount(
                    scope=scope,
                    key=key,
                    readable=bool(raw.get("readable", True)),
                    writable=bool(raw.get("writable", False)),
                    summary=raw.get("summary"),
                    metadata=dict(raw.get("metadata") or {}),
                )
            )
    if mounts:
        return tuple(mounts)

    return tuple(
        MemoryMount(scope="runtime_scratch", key=str(key), readable=True)
        for key in getattr(envelope, "memory_mounts", ()) or ()
        if str(key or "").strip()
    )


def runtime_allows_agent_memory_reader(envelope) -> bool:
    """Return whether the legacy AgentMemory prompt reader is mounted.

    The AgentMemory service can only read agent/user scoped memories today.
    Runtime surfaces such as public webchat may still carry a legacy ``user_id``
    for conversation ownership or verified customer identity, so prompt
    injection must follow the resolved memory mounts instead of raw ctx ids.
    """
    if envelope is None:
        return True
    return any(
        mount.readable and str(mount.key or "").startswith(("agent:", "user:"))
        for mount in memory_mounts_from_envelope(envelope)
    )


def runtime_allows_workspace_memory_reader(
    envelope,
    workspace_id: str | None = None,
) -> bool:
    """Return whether workspace-scoped operating memory is mounted."""
    if envelope is None:
        return True
    clean_workspace_id = str(workspace_id or "").strip()
    expected_key = f"workspace:{clean_workspace_id}" if clean_workspace_id else None
    return any(
        mount.readable
        and str(mount.key or "").startswith("workspace:")
        and (expected_key is None or mount.key == expected_key)
        for mount in memory_mounts_from_envelope(envelope)
    )


def runtime_allows_customer_memory_reader(
    envelope,
    channel_key: str | None = None,
) -> bool:
    """Return whether a customer/channel-scoped memory mount is readable."""
    if envelope is None:
        return True
    expected_key = str(channel_key or "").strip() or None
    return any(
        mount.readable
        and mount.scope == "customer"
        and str(mount.key or "").startswith("channel:")
        and (expected_key is None or mount.key == expected_key)
        for mount in memory_mounts_from_envelope(envelope)
    )


def runtime_allows_customer_memory_writer(
    envelope,
    channel_key: str | None = None,
) -> bool:
    """Return whether a customer/channel-scoped memory mount is writable."""
    if envelope is None:
        return True
    expected_key = str(channel_key or "").strip() or None
    return any(
        mount.writable
        and mount.scope == "customer"
        and str(mount.key or "").startswith("channel:")
        and (expected_key is None or mount.key == expected_key)
        for mount in memory_mounts_from_envelope(envelope)
    )


def memory_mounts_for_request(request) -> list[MemoryMount]:
    external_surface = request.surface in {
        ChatSurface.PUBLIC_CUSTOMER_CHAT,
        ChatSurface.EXTERNAL_CHANNEL_CHAT,
    }
    mounts: list[MemoryMount] = [
        MemoryMount(
            scope="runtime_scratch",
            key=f"run:{request.surface.value}",
            writable=True,
        )
    ]
    if request.surface in {
        ChatSurface.GLOBAL_OWNER_CHAT,
        ChatSurface.AGENT_DM,
    } and request.user_id:
        mounts.append(MemoryMount(scope="owner", key=f"user:{request.user_id}"))
    if request.agent_id and not external_surface:
        mounts.append(MemoryMount(scope="agent", key=f"agent:{request.agent_id}"))
    if request.workspace_id and not external_surface:
        mounts.append(MemoryMount(scope="workspace", key=f"workspace:{request.workspace_id}"))
    if request.channel_context and request.channel_context.source_id:
        mounts.append(
            MemoryMount(
                scope="customer",
                key=f"channel:{request.channel_context.channel_type}:{request.channel_context.source_id}",
                writable=request.surface in {
                    ChatSurface.PUBLIC_CUSTOMER_CHAT,
                    ChatSurface.EXTERNAL_CHANNEL_CHAT,
                },
            )
        )
    return mounts
