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
        "IMPORTANT: Output ONLY valid JSON with this shape: "
        "{\"data\":[[cell,...],...],\"charts\":[{\"type\":\"bar|line|pie\","
        "\"title\":\"...\",\"labelColumn\":0,\"valueColumn\":1,\"startRow\":1,"
        "\"endRow\":10}],\"styles\":{\"row:column\":{\"bold\":true,"
        "\"color\":\"FFFFFF\",\"fill\":\"0F766E\",\"align\":\"left|center|right\","
        "\"numberFormat\":\"0.0%\",\"wrapText\":true}},\"settings\":{"
        "\"sheetName\":\"Dashboard\",\"freezePane\":\"A2\","
        "\"showGridLines\":false,\"autoFilter\":\"A1:F20\","
        "\"columnWidths\":{\"A\":24}}}. Use typed JSON numbers and booleans. "
        "Write derived cells as Excel formula strings beginning with =, keep assumptions "
        "in visible cells, format headers and key outputs, and include a chart only when it "
        "clarifies the data. Omit unused optional fields. No markdown fences or explanation."
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
            "Start with one # deck title. Before writing, infer the audience, the "
            "communication job, the desired audience outcome, the central takeaway, "
            "and a cumulative narrative arc. Give every slide one narrative job and "
            "one claim-led title; keep titles short enough for one line (roughly 8-10 "
            "English words or 18-24 CJK characters). Use 2-5 concise audience-facing "
            "points under each heading. Use numbered points for a true sequence and a "
            "Markdown table only when comparison or evidence is clearer as a table. "
            "Vary the information structure across adjacent slides instead of repeating "
            "the same card grid. Open with context or stakes and close with a decision, "
            "recommendation, synthesis, or explicit next action; do not end with a generic "
            "Thank You slide. Keep text concise — these will become presentation slides. "
            "For speaker notes, add an optional ### Notes block inside that slide. For "
            "every externally sourced non-trivial claim or asset, add a ### Sources block "
            "with the exact URLs supplied by the user. Never invent citations or URLs."
        )
    if format_name == "docx":
        return (
            "Write for a polished editable Word document. Do not repeat the document "
            "title; the renderer adds the title block separately. Begin with a concise "
            "lead paragraph or the first ## section. Build a clear heading hierarchy, "
            "short readable paragraphs, and real bullet or numbered lists. Use a table "
            "only for genuinely comparable rows with shared fields; do not package normal "
            "prose in tables. Match the document archetype (memo, proposal, SOP, report, "
            "manual, or brief). When the request matches a photo report, experiment report, "
            "investment committee memo, legal memo, or formal business letter, preserve its "
            "native structure: photo/cover metadata; hypothesis/method/results/limitations; "
            "decision metadata and underwriting; legal question/facts/analysis; or true "
            "letterhead/recipient/salutation/signature fields respectively. Surface the "
            "recommendation or reader action early, and "
            "end with concrete next steps when appropriate. Keep externally sourced "
            "claims attributable using only URLs or sources supplied by the user; never "
            "invent citations. Do not emit placeholders, TODOs, or production notes."
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
