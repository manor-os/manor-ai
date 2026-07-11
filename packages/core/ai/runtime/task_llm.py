from __future__ import annotations

from typing import Any

from packages.core.ai.engine import AIEngine, ChatMessage, LLMConfig


RUNTIME_TASK_FINAL_TEMPERATURE = 0.3
RUNTIME_TASK_FINAL_MAX_TOKENS = 2048
RUNTIME_TASK_SUPERVISOR_SYSTEM_PROMPT = (
    "You are a task supervisor. Output only valid JSON."
)


def runtime_task_engine(engine: Any | None = None) -> Any:
    """Return the scheduled-task LLM engine, creating the Runtime default when needed."""

    if engine is not None:
        return engine
    return AIEngine(LLMConfig.from_env())


def runtime_configure_task_engine_model(engine: Any, model: str | None) -> Any:
    """Apply the resolved model to a scheduled-task engine when it has config."""

    if model and hasattr(engine, "config"):
        engine.config.model = model
    return engine


def runtime_task_engine_model(engine: Any) -> str | None:
    """Read a scheduled-task engine model name without exposing config internals."""

    return getattr(getattr(engine, "config", None), "model", None)


async def runtime_execute_task_agent_chat(
    *,
    engine: Any,
    messages: list[ChatMessage],
    tools: list[dict[str, Any]],
    system_prompt: str,
    metadata: dict[str, Any] | None = None,
) -> ChatMessage:
    """Run one scheduled-task agent chat call through the Runtime LLM gateway."""

    return await engine.chat(
        messages,
        tools=tools,
        system_prompt=system_prompt,
        metadata=metadata,
    )


async def runtime_execute_task_supervisor_chat(
    *,
    engine: Any,
    prompt: str,
    worker_model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChatMessage:
    """Run the scheduled-task supervisor chat call with bounded model swapping."""

    saved_model = runtime_task_engine_model(engine)
    if worker_model:
        runtime_configure_task_engine_model(engine, worker_model)
    try:
        return await engine.chat(
            [ChatMessage(role="user", content=prompt)],
            system_prompt=RUNTIME_TASK_SUPERVISOR_SYSTEM_PROMPT,
            metadata=metadata,
        )
    finally:
        if worker_model and hasattr(engine, "config"):
            engine.config.model = saved_model


async def runtime_execute_task_final_chat(
    *,
    engine: Any,
    messages: list[ChatMessage],
    system_prompt: str,
    metadata: dict[str, Any] | None = None,
) -> ChatMessage:
    """Run the scheduled-task final no-tools chat call."""

    return await engine.chat(
        messages,
        tools=None,
        system_prompt=system_prompt,
        temperature=RUNTIME_TASK_FINAL_TEMPERATURE,
        max_tokens=RUNTIME_TASK_FINAL_MAX_TOKENS,
        metadata=metadata,
    )
