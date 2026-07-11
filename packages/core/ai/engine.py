"""
AI Engine -- orchestrates LLM calls, tool execution, and streaming.

Wraps the battle-tested llm_client functions with a higher-level OOP interface.
The dataclasses (LLMConfig, ChatMessage, etc.) provide a typed API for callers
that prefer not to work with raw dicts.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from packages.core.ai.llm_client import (
    chat_completion,
    chat_completion_with_tools,
    close_llm_client,
    get_api_key,
    get_llm_base_url,
    get_llm_model,
    EMPTY_USAGE,
)

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    model: str = "anthropic/claude-sonnet-4"
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.7
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            model=get_llm_model(),
            api_key=get_api_key(),
            base_url=get_llm_base_url(),
        )


@dataclass
class ChatMessage:
    role: str  # user, assistant, system, tool
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None  # required for role="tool" messages
    usage: dict | None = None  # token usage from LLM response

    def to_dict(self) -> dict:
        """Convert to OpenAI wire format.

        Two non-trivial conversions happen here so the wire payload is
        valid across providers (OpenAI tolerates a lot; Novita / some
        Anthropic routes through OpenRouter do not):

          * ``role="tool"`` messages need ``tool_call_id`` at the top
            level, not a ``tool_calls`` array. We hoist the id from the
            first entry of ``tool_calls`` if the caller passed it that
            way (legacy shape).
          * ``role="assistant"`` ``tool_calls`` must be in the nested
            ``{id, type:"function", function:{name, arguments:str}}``
            shape. llm_client returns flat ``{id, name, arguments}``
            after parsing, so we re-wrap before sending.
        """
        d: dict[str, Any] = {"role": self.role, "content": self.content}

        if self.role == "tool":
            tcid = self.tool_call_id
            if not tcid and self.tool_calls:
                tcid = (self.tool_calls[0] or {}).get("id")
            if tcid:
                d["tool_call_id"] = tcid
            return d

        if self.tool_calls:
            d["tool_calls"] = [_to_openai_tool_call(tc) for tc in self.tool_calls]
        return d


def _to_openai_tool_call(tc: dict) -> dict:
    """Coerce a tool_call dict to OpenAI's nested function shape.

    Accepts both the flat shape returned by llm_client's parser
    (``{id, name, arguments}``) and the already-nested wire shape
    (``{id, type, function:{name, arguments}}``). Idempotent.
    """
    import json as _json
    if not isinstance(tc, dict):
        return tc
    fn = tc.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        # Already nested. Ensure arguments is a string (wire format).
        args = fn.get("arguments")
        if not isinstance(args, str):
            fn = {**fn, "arguments": _json.dumps(args or {})}
        return {
            "id": tc.get("id"),
            "type": tc.get("type") or "function",
            "function": fn,
        }
    name = tc.get("name") or ""
    args = tc.get("arguments")
    args_str = args if isinstance(args, str) else _json.dumps(args or {})
    return {
        "id": tc.get("id"),
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result: str | None = None


@dataclass
class StreamChunk:
    type: str  # "token", "tool_call", "tool_result", "done", "error"
    content: str = ""
    tool_call: ToolCall | None = None
    usage: dict | None = None


class AIEngine:
    """Stateless AI engine for chat completion and tool execution.

    Wraps :mod:`packages.core.ai.llm_client` functions so callers can use
    either the OOP interface (``engine.chat()``) or the module-level
    functions directly.
    """

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict | None = None,
    ) -> ChatMessage:
        """Single-turn completion (non-streaming).

        If *tools* are provided, uses ``chat_completion_with_tools``; otherwise
        uses plain ``chat_completion``.  Returns a :class:`ChatMessage` with
        the assistant's response (and any tool_calls).
        """
        raw_messages: list[dict] = []
        if system_prompt:
            raw_messages.append({"role": "system", "content": system_prompt})
        for m in messages:
            raw_messages.append(m.to_dict())

        temp = temperature if temperature is not None else self.config.temperature

        if tools:
            content, tool_calls, usage = await chat_completion_with_tools(
                raw_messages,
                tools,
                temperature=temp,
                model=self.config.model,
                metadata=metadata,
            )
            tc_list = None
            if tool_calls:
                tc_list = tool_calls
            return ChatMessage(role="assistant", content=content, tool_calls=tc_list, usage=usage)
        else:
            content, usage = await chat_completion(
                raw_messages,
                temperature=temp,
                max_tokens=max_tokens or self.config.max_tokens,
                model=self.config.model,
                metadata=metadata,
            )
            return ChatMessage(role="assistant", content=content, usage=usage)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion -- yields StreamChunk objects.

        Uses the llm_client streaming path via ``stream_handler`` callback,
        collecting chunks into an async generator.
        """
        import asyncio

        queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()

        def _handler(event_name: str, payload: dict) -> None:
            if event_name == "text_delta":
                queue.put_nowait(StreamChunk(type="token", content=payload.get("content", "")))
            elif event_name == "text_reset":
                # Stream was invalidated (tool calls detected after text); reset
                queue.put_nowait(StreamChunk(type="error", content="stream_reset"))

        raw_messages: list[dict] = []
        if system_prompt:
            raw_messages.append({"role": "system", "content": system_prompt})
        for m in messages:
            raw_messages.append(m.to_dict())

        temp = temperature if temperature is not None else self.config.temperature

        async def _run() -> None:
            try:
                if tools:
                    content, tool_calls, usage = await chat_completion_with_tools(
                        raw_messages,
                        tools,
                        temperature=temp,
                        model=self.config.model,
                        stream_handler=_handler,
                        metadata=metadata,
                    )
                    if tool_calls:
                        for tc in tool_calls:
                            queue.put_nowait(StreamChunk(
                                type="tool_call",
                                tool_call=ToolCall(name=tc["name"], arguments=tc.get("arguments", {})),
                            ))
                    queue.put_nowait(StreamChunk(type="done", content=content or "", usage=usage))
                else:
                    content, usage = await chat_completion(
                        raw_messages,
                        temperature=temp,
                        max_tokens=max_tokens or self.config.max_tokens,
                        model=self.config.model,
                        stream_handler=_handler,
                        metadata=metadata,
                    )
                    queue.put_nowait(StreamChunk(type="done", content=content or "", usage=usage))
            except Exception as e:
                queue.put_nowait(StreamChunk(type="error", content=str(e)))
            finally:
                queue.put_nowait(None)  # sentinel

        task = asyncio.create_task(_run())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            if not task.done():
                task.cancel()

    async def close(self) -> None:
        """Shut down the shared HTTP client."""
        await close_llm_client()
