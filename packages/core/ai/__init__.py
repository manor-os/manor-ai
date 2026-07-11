"""Manor AI engine package -- LLM orchestration, tools, and runners.

Keep package-level exports lazy so lightweight runtime modules can be imported
without constructing database-backed runners or tool pools.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "AIEngine": ("packages.core.ai.engine", "AIEngine"),
    "LLMConfig": ("packages.core.ai.engine", "LLMConfig"),
    "ChatMessage": ("packages.core.ai.engine", "ChatMessage"),
    "StreamChunk": ("packages.core.ai.engine", "StreamChunk"),
    "ToolCall": ("packages.core.ai.engine", "ToolCall"),
    "TaskRunner": ("packages.core.ai.task_runner", "TaskRunner"),
    "chat_completion": ("packages.core.ai.llm_client", "chat_completion"),
    "chat_completion_with_tools": ("packages.core.ai.llm_client", "chat_completion_with_tools"),
    "EMPTY_USAGE": ("packages.core.ai.llm_client", "EMPTY_USAGE"),
    "LLMRateLimited": ("packages.core.ai.llm_client", "LLMRateLimited"),
    "get_llm_client": ("packages.core.ai.llm_client", "get_llm_client"),
    "close_llm_client": ("packages.core.ai.llm_client", "close_llm_client"),
    "bind_llm_call_history": ("packages.core.ai.llm_client", "bind_llm_call_history"),
    "record_llm_response_data": ("packages.core.ai.llm_client", "record_llm_response_data"),
    "record_llm_failure": ("packages.core.ai.llm_client", "record_llm_failure"),
}


def __getattr__(name: str) -> Any:
    exported = _EXPORTS.get(name)
    if not exported:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = exported
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = [
    # Engine (OOP interface)
    "AIEngine",
    "LLMConfig",
    "ChatMessage",
    "StreamChunk",
    "ToolCall",
    # Runners
    "TaskRunner",
    # LLM client (functional interface)
    "chat_completion",
    "chat_completion_with_tools",
    "EMPTY_USAGE",
    "LLMRateLimited",
    "get_llm_client",
    "close_llm_client",
    "bind_llm_call_history",
    "record_llm_response_data",
    "record_llm_failure",
]
