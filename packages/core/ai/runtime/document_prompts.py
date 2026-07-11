from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.prompt_assembly import runtime_merge_prompt_appendix
from packages.core.ai.runtime.sources import (
    RUNTIME_DOCGEN_SOURCE,
    RUNTIME_DOCUMENT_AI_DRAFT_SOURCE,
)


RUNTIME_DOCUMENT_AI_DRAFT_FORMAT_HINTS: dict[str, str] = {
    "csv": (
        "IMPORTANT: Output ONLY valid CSV (comma-separated values). First row must "
        "be column headers. No markdown, no explanation."
    ),
    "json": (
        "IMPORTANT: Output ONLY valid JSON. No markdown fences, no explanation."
    ),
    "html": (
        "IMPORTANT: Output ONLY valid HTML. No markdown fences, no explanation."
    ),
    "xlsx": (
        "IMPORTANT: Output ONLY valid CSV (comma-separated values). First row must "
        "be column headers. No markdown, no explanation. The data will be "
        "converted to a spreadsheet."
    ),
}


def runtime_document_ai_draft_system_prompt(file_type: str) -> str:
    """Build the Runtime-owned system prompt for document draft generation."""

    base_prompt = (
        "You are a document writer. Generate well-structured content "
        "based on the user's request. Output only the document content, "
        "no meta-commentary."
    )
    return runtime_merge_prompt_appendix(
        base_prompt,
        RUNTIME_DOCUMENT_AI_DRAFT_FORMAT_HINTS.get(file_type, ""),
    )


def runtime_document_ai_draft_messages(
    *,
    prompt: str,
    file_type: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for document draft generation."""

    return [
        {"role": "system", "content": runtime_document_ai_draft_system_prompt(file_type)},
        {"role": "user", "content": prompt},
    ]


async def runtime_execute_document_ai_draft_completion(
    *,
    entity_id: str,
    user_id: str | None,
    prompt: str,
    file_type: str,
    document_id: str | None = None,
) -> RuntimeTextCompletionResult:
    """Execute document AI draft generation with Runtime-owned defaults."""

    metadata: dict[str, Any] = {"file_type": file_type}
    if document_id:
        metadata["document_id"] = document_id

    return await runtime_execute_text_completion(
        runtime_document_ai_draft_messages(prompt=prompt, file_type=file_type),
        entity_id=entity_id,
        user_id=user_id,
        source=RUNTIME_DOCUMENT_AI_DRAFT_SOURCE,
        max_tokens=4000,
        metadata=metadata,
    )


def runtime_docgen_format_hint(format_name: str) -> str:
    """Return the Runtime-owned authoring hint for generated document formats."""

    if format_name == "pptx":
        return (
            "Structure your output with ## headings for each slide. "
            "Use bullet points under each heading. Keep text concise — "
            "these will become presentation slides."
        )
    return (
        "Use markdown formatting: # for title, ## for sections, "
        "bullet lists, numbered lists, and tables where appropriate."
    )


def runtime_docgen_system_prompt(format_name: str) -> str:
    """Build the Runtime-owned system prompt for AI document generation."""

    return runtime_merge_prompt_appendix(
        "You are a professional document writer. Generate well-structured content "
        "in markdown format based on the user's request. "
        "Output ONLY the document content — no meta-commentary.",
        runtime_docgen_format_hint(format_name),
    )


def runtime_docgen_messages(
    *,
    prompt: str,
    format_name: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for AI document generation."""

    return [
        {"role": "system", "content": runtime_docgen_system_prompt(format_name)},
        {"role": "user", "content": prompt},
    ]


async def runtime_execute_docgen_completion(
    *,
    entity_id: str,
    prompt: str,
    format_name: str,
) -> RuntimeTextCompletionResult:
    """Execute AI document content generation with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_docgen_messages(prompt=prompt, format_name=format_name),
        entity_id=entity_id,
        source=RUNTIME_DOCGEN_SOURCE,
        max_tokens=4096,
    )
