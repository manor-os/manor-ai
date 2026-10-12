from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from packages.core.ai.engine import AIEngine, ChatMessage, LLMConfig
from packages.core.ai.runtime.billing import runtime_current_billing_context
from packages.core.ai.runtime.response_language import (
    resolve_runtime_response_language,
    response_language_messages,
)


RUNTIME_TASK_FINAL_TEMPERATURE = 0.3
RUNTIME_TASK_FINAL_MAX_TOKENS = 2048
RUNTIME_TASK_SUPERVISOR_SYSTEM_PROMPT = (
    "You are a task supervisor. Output only valid JSON."
)


async def _runtime_execute_task_model_round(
    *,
    engine: Any,
    messages: list[ChatMessage],
    call_kwargs: dict[str, Any],
    performance_run_id: str | None,
    performance_tool_call_id: str | None,
) -> ChatMessage:
    from packages.core.ai.runtime.performance import (
        log_model_performance,
        runtime_json_bytes,
    )

    request_bytes = runtime_json_bytes({"messages": messages, "options": call_kwargs})
    started_at = time.perf_counter()
    try:
        response = await engine.chat(messages, **call_kwargs)
    except Exception:
        log_model_performance(
            run_id=performance_run_id,
            tool_call_id=performance_tool_call_id,
            outcome="failure",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            request_bytes=request_bytes,
            response_bytes=0,
        )
        raise
    usage = getattr(response, "usage", None)
    usage_error = (
        str(usage.get("error") or "").strip() if isinstance(usage, Mapping) else ""
    )
    log_model_performance(
        run_id=performance_run_id,
        tool_call_id=performance_tool_call_id,
        outcome="failure" if usage_error else "success",
        duration_ms=(time.perf_counter() - started_at) * 1000,
        request_bytes=request_bytes,
        response_bytes=runtime_json_bytes(response),
    )
    return response


async def _task_response_language_request(
    prompt: str, messages: list[ChatMessage],
) -> tuple[str, list[ChatMessage]]:
    billing = runtime_current_billing_context()
    language = await resolve_runtime_response_language(
        entity_id=getattr(billing, "entity_id", None),
        user_id=getattr(billing, "user_id", None),
    )
    prepared = response_language_messages(
        [{"role": "system", "content": prompt}, *[asdict(message) for message in messages]],
        language,
    )
    # AIEngine prepends this business prompt; the language message stays next
    # to the current user in the history, including resumed tool-call groups.
    system_prompt = ""
    if prepared and prepared[0]["role"] == "system":
        system_prompt = prepared.pop(0)["content"]
    return system_prompt, [ChatMessage(**message) for message in prepared]


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
    temperature: float | None = None,
    max_tokens: int | None = None,
    performance_run_id: str | None = None,
    performance_tool_call_id: str | None = None,
) -> ChatMessage:
    """Run one scheduled-task agent chat call through the Runtime LLM gateway."""

    system_prompt, messages = await _task_response_language_request(system_prompt, messages)
    call_kwargs: dict[str, Any] = {
        "tools": tools,
        "system_prompt": system_prompt,
        "metadata": metadata,
    }
    if temperature is not None:
        call_kwargs["temperature"] = temperature
    if max_tokens is not None:
        call_kwargs["max_tokens"] = max_tokens
    return await _runtime_execute_task_model_round(
        engine=engine,
        messages=messages,
        call_kwargs=call_kwargs,
        performance_run_id=performance_run_id,
        performance_tool_call_id=performance_tool_call_id,
    )


async def runtime_execute_task_supervisor_chat(
    *,
    engine: Any,
    prompt: str,
    worker_model: str | None = None,
    metadata: dict[str, Any] | None = None,
    performance_run_id: str | None = None,
    performance_tool_call_id: str | None = None,
) -> ChatMessage:
    """Run the scheduled-task supervisor chat call with bounded model swapping."""

    saved_model = runtime_task_engine_model(engine)
    if worker_model:
        runtime_configure_task_engine_model(engine, worker_model)
    try:
        system_prompt, messages = await _task_response_language_request(
            RUNTIME_TASK_SUPERVISOR_SYSTEM_PROMPT,
            [ChatMessage(role="user", content=prompt)],
        )
        return await _runtime_execute_task_model_round(
            engine=engine,
            messages=messages,
            call_kwargs={
                "system_prompt": system_prompt,
                "metadata": metadata,
            },
            performance_run_id=performance_run_id,
            performance_tool_call_id=performance_tool_call_id,
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
    temperature: float | None = None,
    max_tokens: int | None = None,
    performance_run_id: str | None = None,
    performance_tool_call_id: str | None = None,
) -> ChatMessage:
    """Run the scheduled-task final no-tools chat call."""

    system_prompt, messages = await _task_response_language_request(
        system_prompt,
        messages,
    )
    return await _runtime_execute_task_model_round(
        engine=engine,
        messages=messages,
        call_kwargs={
            "tools": None,
            "system_prompt": system_prompt,
            "temperature": (
                RUNTIME_TASK_FINAL_TEMPERATURE
                if temperature is None
                else temperature
            ),
            "max_tokens": (
                RUNTIME_TASK_FINAL_MAX_TOKENS
                if max_tokens is None
                else max_tokens
            ),
            "metadata": metadata,
        },
        performance_run_id=performance_run_id,
        performance_tool_call_id=performance_tool_call_id,
    )
