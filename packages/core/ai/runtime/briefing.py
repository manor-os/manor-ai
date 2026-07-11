from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
    runtime_one_shot_messages,
)
from packages.core.ai.runtime.sources import RUNTIME_BRIEFING_SOURCE


RUNTIME_BRIEFING_PROMPT_HINT = {
    "briefing_id": "<provided in user prompt - copy verbatim>",
    "headline": "One sentence. What actually matters today?",
    "items": [
        {
            "source": "gmail|slack|calendar|stripe|manual",
            "source_ref": "<provider id of the message/event>",
            "category": "urgent|respond|fyi|auto_handled|skip",
            "title": "Operator-readable framing.",
            "summary": "What it is + why it matters in 1-2 sentences.",
            "received_at": "ISO-8601 string",
            "sender": "name <email>",
            "draft_reply": "When category=respond. Plain text. Match the workspace voice.",
            "actions": [
                {
                    "kind": "send_reply|schedule|create_task|snooze|archive|manual",
                    "label": "Imperative verb (<=80 chars)",
                    "payload": {"...": "..."},
                }
            ],
        }
    ],
    "notes": "Optional cross-item observations. Keep brief.",
    "metrics_snapshot": {
        "workspace_daily_summary": "Optional compact metrics from the workspace daily summary."
    },
}


async def runtime_execute_briefing_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
) -> RuntimeTextCompletionResult:
    """Execute a morning briefing completion with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_one_shot_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ),
        entity_id=entity_id,
        workspace_id=workspace_id,
        source=RUNTIME_BRIEFING_SOURCE,
    )


def runtime_briefing_system_prompt(workspace_name: str) -> str:
    """Render the Runtime-owned morning briefing system prompt."""

    schema_hint = json.dumps(RUNTIME_BRIEFING_PROMPT_HINT, indent=2)
    return f"""\
You are the morning briefing curator for the Manor workspace
"{workspace_name}". The owner is a solo operator -- treat their
attention as scarce.

Your job:
  1. Use the workspace daily summary as factual database context for
     outcomes, health, human handoff items, and today's focus. Do not
     invent facts that are not in that summary or the inbox signals.
  2. Bucket each inbox signal into one of FIVE categories:
       urgent       needs operator action TODAY
       respond      routine reply expected; draft it for them
       fyi          worth knowing; no action
       auto_handled the system already actioned this
       skip         ignore (newsletters, duplicate transactional)
  3. For ``respond`` items, draft a reply in the workspace voice
     (concrete, brief, no fluff). Default to "yes if reasonable,
     ask one clarifying question otherwise".
  4. Headline = the one thing that matters. <=30 words. NOT a list.
  5. Skip items aggressively -- your value is reducing volume.
  6. Suggest actions the UI can render as buttons. ``send_reply``
     for ``respond``; ``schedule``/``create_task`` for ``urgent``.
  7. If the workspace summary has human action items not tied to an
     inbox signal, create at most 3 ``manual`` source items for them.

Output ONLY valid JSON matching this exact shape -- no prose, no
markdown, no code fences:
{schema_hint}
"""


def runtime_briefing_user_prompt(
    *,
    briefing_id: str,
    signals: list[dict[str, Any]],
    memory_snippets: list[dict[str, Any]],
    goals_snapshot: list[dict[str, Any]],
    workspace_summary: dict[str, Any] | None = None,
) -> str:
    """Render the Runtime-owned morning briefing user prompt."""

    parts = [
        f"# Briefing trigger\nbriefing_id to use: `{briefing_id}`",
        f"# Inbox signals ({len(signals)})",
    ]
    for signal in signals:
        parts.append(runtime_briefing_signal_text(signal))

    if workspace_summary:
        parts.append(
            "# Workspace daily summary (database snapshot)\n"
            + runtime_briefing_workspace_summary_text(workspace_summary)
        )

    if goals_snapshot:
        parts.append("# Active goals (pace context)\n" + "\n".join(
            f"- {goal.get('title')}: {goal.get('current_value')}/{goal.get('target_value')} "
            f"({goal.get('pace_status') or 'unknown'})"
            for goal in goals_snapshot
        ))

    if memory_snippets:
        parts.append("# Workspace memory (most relevant)\n" + "\n".join(
            f"- [{memory.get('scope')}] {memory.get('title')}: "
            f"{((memory.get('content') or '')[:160])}"
            for memory in memory_snippets
        ))

    parts.append(
        f"Now produce the JSON Briefing. ``briefing_id`` MUST equal "
        f"`{briefing_id}` exactly."
    )
    return "\n\n".join(parts)


def runtime_briefing_signal_text(signal: dict[str, Any]) -> str:
    """Compact representation of one inbox signal for the briefing prompt."""

    body = (signal.get("body_text") or signal.get("snippet") or "").strip()
    if len(body) > 800:
        body = body[:800] + "..."
    return (
        f"## {signal.get('source')}:{signal.get('source_ref')}\n"
        f"From: {signal.get('sender', '?')}\n"
        f"Date: {signal.get('received_at', '?')}\n"
        f"Subject: {signal.get('subject', '(none)')}\n"
        f"Body:\n{body}"
    )


def runtime_briefing_workspace_summary_text(summary: dict[str, Any]) -> str:
    """Compact workspace summary for the briefing prompt."""

    compact = {
        "window": summary.get("window"),
        "data_quality": summary.get("data_quality"),
        "yesterday_outcomes": _compact_briefing_summary_section(
            summary.get("yesterday_outcomes") or {},
            ["completed_tasks", "created_tasks", "failed_tasks"],
        ),
        "current_health": _compact_briefing_summary_section(
            summary.get("current_health") or {},
            ["overdue_tasks", "stalled_tasks"],
        ),
        "needs_human_handling": _compact_briefing_summary_section(
            summary.get("needs_human_handling") or {},
            ["proposed_tasks", "waiting_tasks", "blocked_tasks", "failed_tasks"],
        ),
        "today_focus": _compact_briefing_summary_section(
            summary.get("today_focus") or {},
            ["due_today_tasks", "in_progress_tasks", "priority_tasks"],
        ),
        "recommended_action_items": summary.get("recommended_action_items") or [],
    }
    return json.dumps(compact, ensure_ascii=False, indent=2)


def _compact_briefing_summary_section(
    section: dict[str, Any],
    task_keys: list[str],
) -> dict[str, Any]:
    compact = {key: value for key, value in section.items() if key not in task_keys}
    for key in task_keys:
        tasks = section.get(key) or []
        compact[key] = [
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "priority": task.get("priority"),
                "deadline": task.get("deadline"),
            }
            for task in tasks[:5]
            if isinstance(task, dict)
        ]
    return compact
