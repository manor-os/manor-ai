"""Estimated context occupancy, distinct from cumulative billed usage."""

from __future__ import annotations

import json
import hashlib
from typing import Any

from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text


def context_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def context_section(name: str, content: str, *, category: str | None = None, source: str | None = None) -> dict[str, Any]:
    """Classify from the source that rendered a section, never from its prose."""
    if category is None:
        category = {
            "agent_identity": "agent_instructions",
            "agent_memories": "memory_files",
            "workspace_operating_memory": "memory_files",
            "available_skills_section": "skills",
            "extra_context_section": "runtime_context",
            "runtime_context": "runtime_context",
        }.get(name, "system_prompt")
    return {
        "key": name, "category": category, "source": source or f"prompt_sections:{name}",
        "content": content, "source_digest": context_digest(content),
        "source_tokens": runtime_estimate_tokens_for_text(content),
        "policy": "reload_and_bound" if category in {"memory_files", "skills"} else "protect",
        "storage": "source",
    }


def manage_prompt_sections(
    sections: list[dict[str, Any]], *, memory_tokens: int = 8_000, skill_tokens: int = 6_000,
) -> list[dict[str, Any]]:
    """Bound reloadable context while leaving authoritative instructions intact.

    Source documents remain in their existing stores. The bounded text is only
    a per-turn working copy and must never be written back as durable memory.
    """
    remaining = {"memory_files": memory_tokens, "skills": skill_tokens}
    # Allocate small sources first, then divide the remaining budget fairly.
    # Rendering order must not decide which Workspace memories disappear.
    allocations = {}
    for category, total in remaining.items():
        pending = sorted(
            ((i, runtime_estimate_tokens_for_text(s["content"]))
             for i, s in enumerate(sections) if s["category"] == category),
            key=lambda item: item[1],
        )
        available = max(0, total)
        for offset, (index, requested) in enumerate(pending):
            share = min(requested, available // (len(pending) - offset))
            allocations[index] = share
            available -= share
    managed = []
    for index, original in enumerate(sections):
        section = dict(original)
        content = section["content"]
        category = section["category"]
        if category in remaining:
            budget = allocations[index]
            if runtime_estimate_tokens_for_text(content) > budget:
                marker = "\n[Context excerpt; full content remains at its source.]\n"
                lo, hi = 0, len(content)
                while lo < hi:
                    size = (lo + hi + 1) // 2
                    head = (size * 3) // 4
                    tail = size - head
                    preview = content[:head] + marker + (content[-tail:] if tail else "")
                    if runtime_estimate_tokens_for_text(preview) <= budget:
                        lo = size
                    else:
                        hi = size - 1
                head, tail = (lo * 3) // 4, lo - (lo * 3) // 4
                content = content[:head] + marker + (content[-tail:] if tail else "") if lo else ""
                section["compacted"] = True
        section["content"] = content
        section["loaded_tokens"] = runtime_estimate_tokens_for_text(content) if content else 0
        section["loaded"] = bool(content)
        managed.append(section)
    return managed


def context_source_manifest(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store source identities, not stale copies of prompts, skills or memory."""
    return [{key: value for key, value in section.items() if key != "content"} for section in sections]


def classify_context_messages(messages: list[dict[str, Any]], *, storage: str = "session.messages") -> list[dict[str, Any]]:
    from packages.core.ai.runtime.agentic_loop_prompts import (
        RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX,
        RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX,
    )

    calls = {}
    result = []
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            from packages.core.ai.runtime.tool_gateway import runtime_tool_call_target

            function = call.get("function") or call
            calls[call.get("id")] = runtime_tool_call_target(function.get("name", ""), function.get("arguments"))[0]
        name = calls.get(message.get("tool_call_id"), "")
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            category, policy = "system_prompt", "rebuild"
        elif role == "tool":
            category = "skill_content" if name == "invoke_skill" else "tool_results"
            policy = "archive_then_summarize"
        elif role == "user" and content.startswith((
            "[Earlier conversation compacted", "[Earlier conversation summary]",
            RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX, RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX,
        )):
            category, policy = "summary", "merge"
        else:
            category, policy = "messages", "preserve_current_and_recent"
        result.append({
            "index": index, "category": category, "policy": policy,
            "tool_name": name or None, "digest": context_digest(message),
            "storage": storage,
        })
    return result


def context_tool_manifest(tools: list[dict], allowed_tool_names: set[str]) -> list[dict]:
    loaded = {str((tool.get("function") or {}).get("name") or ""): tool for tool in tools}
    return [{
        "key": name, "category": "mcp_tools" if name.startswith("mcp__") else "system_tools",
        "source": "runtime.tool_registry", "storage": "source", "policy": "resolve_current_scope",
        "loaded": name in loaded, "source_digest": context_digest(loaded[name]) if name in loaded else None,
    } for name in sorted((set(loaded) | allowed_tool_names) - {""})]


def runtime_context_window_usage(
    *,
    system_prompt: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    context_window_tokens: int,
    output_reserve_tokens: int,
    compaction_ratio: float = 0.75,
) -> dict[str, Any]:
    """Approximate the actual prompt categories; deferred tools are excluded."""

    def estimate(value: Any) -> int:
        return runtime_estimate_tokens_for_text(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )

    from packages.core.ai.runtime.context_compaction import request_tokens
    message_tokens = request_tokens(messages)
    mcp_tools = sum(
        estimate(tool) for tool in tools
        if str((tool.get("function") or {}).get("name") or "").startswith("mcp__")
    )
    system_tools = sum(
        estimate(tool) for tool in tools
        if not str((tool.get("function") or {}).get("name") or "").startswith("mcp__")
    )
    system_tokens = estimate(system_prompt)
    occupied = message_tokens + mcp_tools + system_tools + system_tokens
    buffer = max(0, context_window_tokens - int(context_window_tokens * compaction_ratio))
    return {
        "estimated": True,
        "context_window_tokens": context_window_tokens,
        "messages_tokens": message_tokens,
        "mcp_tools_tokens": mcp_tools,
        "system_tools_tokens": system_tools,
        "system_prompt_tokens": system_tokens,
        "system_prompt_includes": ["skills", "memory_files", "agent_instructions"],
        "autocompact_buffer_tokens": buffer,
        "output_reserve_tokens": output_reserve_tokens,
        "occupied_tokens": occupied,
        "free_tokens": max(0, context_window_tokens - occupied - buffer - output_reserve_tokens),
    }
