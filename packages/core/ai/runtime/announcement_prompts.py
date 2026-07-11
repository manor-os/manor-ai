from __future__ import annotations

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.sources import RUNTIME_ANNOUNCEMENT_DRAFT_SOURCE


_SEVERITY_TONE_HINTS: dict[str, str] = {
    "info": "Tone: friendly and informative.",
    "warning": (
        "Tone: clear heads-up. State what changes, when, and what the "
        "customer should expect."
    ),
    "critical": (
        "Tone: urgent but calm. Include explicit action items and "
        "timelines the customer must follow."
    ),
}


def runtime_announcement_draft_system_prompt(severity: str) -> str:
    """Runtime-owned system prompt for platform announcement drafting."""

    return (
        "You are writing a customer-facing platform announcement for "
        "Manor AI (an AI agent platform). Rewrite and complete the "
        "admin's rough draft into a polished announcement body.\n"
        "Output ONLY the body, using this Markdown subset: #/##/### "
        "headings, paragraphs, - bullet lists, 1. numbered lists, "
        "**bold**, *italic*, `code`, [label](https://url), "
        "![alt](https://image-url), pipe tables, --- horizontal rules, "
        "> blockquotes. No code fences, no meta-commentary, no "
        "greeting or sign-off (the email template adds those).\n"
        "Write in the same language as the admin's input. Keep it "
        "concise — this appears as an in-app banner and a short email. "
        + _SEVERITY_TONE_HINTS.get(severity, _SEVERITY_TONE_HINTS["info"])
    )


def runtime_announcement_draft_messages(
    *, title: str, body_draft: str, severity: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for announcement drafting."""

    user_parts = []
    if title.strip():
        user_parts.append(f"Title: {title.strip()}")
    if body_draft.strip():
        user_parts.append(f"Draft/notes:\n{body_draft.strip()}")
    return [
        {"role": "system", "content": runtime_announcement_draft_system_prompt(severity)},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


async def runtime_execute_announcement_draft_completion(
    *, title: str, body_draft: str, severity: str,
) -> RuntimeTextCompletionResult:
    """Execute announcement drafting with Runtime-owned defaults.

    Platform-scoped (``entity_id=None``): no tenant billing context is
    created, so the CALLER is responsible for logging token usage into
    the platform bucket.
    """

    return await runtime_execute_text_completion(
        runtime_announcement_draft_messages(
            title=title, body_draft=body_draft, severity=severity,
        ),
        entity_id=None,
        source=RUNTIME_ANNOUNCEMENT_DRAFT_SOURCE,
        max_tokens=1500,
    )
