from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
    runtime_one_shot_messages,
    runtime_prompt_with_output_schema,
)
from packages.core.ai.runtime.sources import RUNTIME_WORKER_SOURCE


async def runtime_execute_internal_worker_llm_step(
    *,
    prompt: Any,
    expected_output_schema: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    byok: bool = False,
    metadata: dict[str, Any] | None = None,
) -> RuntimeTextCompletionResult:
    """Execute an InternalWorker LLM step with Runtime-owned defaults."""

    user_prompt = runtime_prompt_with_output_schema(prompt, expected_output_schema)
    runtime_overrides: dict[str, Any] = {}
    if temperature is not None:
        runtime_overrides["temperature"] = temperature
    return await runtime_execute_text_completion(
        runtime_one_shot_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        source=RUNTIME_WORKER_SOURCE,
        model=model,
        max_tokens=4096 if max_tokens is None else max_tokens,
        byok=byok,
        metadata=metadata,
        **runtime_overrides,
    )
