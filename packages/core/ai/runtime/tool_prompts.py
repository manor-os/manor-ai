from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.sources import RUNTIME_EXTRACT_DATA_TOOL_SOURCE


def runtime_extract_data_tool_prompt(
    *,
    task: str | None,
    text: str,
    output_schema: str | None = None,
    max_text_chars: int = 10000,
) -> str:
    """Resolve the one-shot prompt used by the extract_data runtime tool."""

    prompt = f"Extract the following from the text below:\nTask: {task}\n"
    if output_schema:
        prompt += f"Output format: {output_schema}\n"
    prompt += f"\nText:\n{text[:max_text_chars]}\n\nReturn ONLY the extracted data as JSON."
    return prompt


def runtime_extract_data_tool_messages(
    *,
    task: str | None,
    text: str,
    output_schema: str | None = None,
    max_text_chars: int = 10000,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for the extract_data runtime tool."""

    return [{
        "role": "user",
        "content": runtime_extract_data_tool_prompt(
            task=task,
            text=text,
            output_schema=output_schema,
            max_text_chars=max_text_chars,
        ),
    }]


async def runtime_execute_extract_data_tool_completion(
    *,
    entity_id: str | None,
    task: str,
    text: str,
    output_schema: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimeTextCompletionResult:
    """Execute extract_data with Runtime-owned completion defaults."""

    return await runtime_execute_text_completion(
        runtime_extract_data_tool_messages(
            task=task,
            text=text,
            output_schema=output_schema,
        ),
        entity_id=entity_id,
        source=RUNTIME_EXTRACT_DATA_TOOL_SOURCE,
        temperature=0.1,
        metadata=metadata,
    )
