from __future__ import annotations

import os
from typing import Any

from packages.core.ai.llm_client import chat_completion, chat_completion_with_tools
from packages.core.ai.runtime.response_language import current_response_language_messages
from packages.core.constants.models import DEFAULTS


RUNTIME_AGENTIC_MAX_TOKENS = int(os.environ.get("RUNTIME_AGENTIC_MAX_TOKENS", "16384"))
RUNTIME_AGENTIC_TEXT_MAX_TOKENS = int(
    os.environ.get("RUNTIME_AGENTIC_TEXT_MAX_TOKENS", str(RUNTIME_AGENTIC_MAX_TOKENS))
)
RUNTIME_AGENTIC_TOOL_CALL_MAX_TOKENS = int(
    os.environ.get("RUNTIME_AGENTIC_TOOL_CALL_MAX_TOKENS", str(RUNTIME_AGENTIC_MAX_TOKENS))
)
RUNTIME_AGENTIC_COMPACTION_TEMPERATURE = 0.3
RUNTIME_AGENTIC_COMPACTION_MAX_TOKENS = 4096
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}


async def _noop_stream_handler(_event_name: str, _payload: dict[str, Any]) -> None:
    return None


def runtime_agentic_tool_streaming_enabled() -> bool:
    value = os.getenv("RUNTIME_AGENTIC_TOOL_STREAMING", "1").strip().lower()
    return value not in _FALSE_ENV_VALUES


def _agentic_tool_stream_handler(stream_handler: Any = None) -> Any:
    if stream_handler is not None:
        return stream_handler
    if runtime_agentic_tool_streaming_enabled():
        return _noop_stream_handler
    return None


async def runtime_execute_agentic_compaction_completion(
    prompt: str, *, model: str | None = None,
    metadata: dict[str, Any] | None = None,
    max_tokens: int = RUNTIME_AGENTIC_COMPACTION_MAX_TOKENS,
) -> tuple[str, dict[str, Any]]:
    """Use the active route and credentials for a structured continuation checkpoint."""

    return await chat_completion(
        current_response_language_messages([{"role": "user", "content": prompt}]),
        model=model or DEFAULTS["worker"],
        metadata=metadata,
        response_format={"type": "json_object"},
        temperature=RUNTIME_AGENTIC_COMPACTION_TEMPERATURE,
        max_tokens=max_tokens,
    )


async def runtime_execute_agentic_round_tool_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int | None = None,
    model: str | None = None,
    stream_handler: Any = None,
    metadata: dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]] | None, dict[str, Any]]:
    """Run one agentic loop LLM round with Runtime-owned tool-call plumbing."""

    return await chat_completion_with_tools(
        current_response_language_messages(messages),
        list(tools),
        temperature=temperature,
        model=model,
        stream_handler=_agentic_tool_stream_handler(stream_handler),
        metadata=metadata,
        **({"response_format": response_format} if response_format is not None else {}),
        max_tokens=(
            RUNTIME_AGENTIC_TOOL_CALL_MAX_TOKENS
            if max_tokens is None
            else max_tokens
        ),
    )


async def runtime_execute_agentic_round_text_completion(
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int | None = None,
    model: str | None = None,
    stream_handler: Any = None,
    metadata: dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run one non-tool agentic loop LLM round."""

    return await chat_completion(
        current_response_language_messages(messages),
        temperature=temperature,
        model=model,
        max_tokens=(
            RUNTIME_AGENTIC_TEXT_MAX_TOKENS
            if max_tokens is None
            else max_tokens
        ),
        stream_handler=stream_handler,
        metadata=metadata,
        **({"response_format": response_format} if response_format is not None else {}),
    )


async def runtime_execute_agentic_final_completion(
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Produce the final text summary after an agentic loop hits max rounds."""

    return await chat_completion(
        current_response_language_messages(messages),
        temperature=temperature,
        model=model,
        max_tokens=(
            RUNTIME_AGENTIC_TEXT_MAX_TOKENS
            if max_tokens is None
            else max_tokens
        ),
        metadata=metadata,
        **({"response_format": response_format} if response_format is not None else {}),
    )
