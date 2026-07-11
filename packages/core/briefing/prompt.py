"""Triage prompt + LLM call + response parsing.

One Claude call per briefing: feed in N inbox signals + workspace
memory snippets + recent goal pace, get back a Briefing JSON.

Falls back to a rule-based stub when the LLM is unavailable so dev/CI
runs still produce a non-empty briefing the smoke test can validate.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import ValidationError

from packages.core.ai.runtime import (
    runtime_briefing_system_prompt,
    runtime_briefing_user_prompt,
    runtime_execute_briefing_completion,
    runtime_validation_retry_user_prompt,
)
from packages.core.briefing.schema import (
    Briefing,
    BriefingAction,
    BriefingItem,
)

logger = logging.getLogger(__name__)


BRIEFING_VERSION = "v0.1-demo-b"


# ── Driver ───────────────────────────────────────────────────────────

async def generate_briefing_via_llm(
    *,
    workspace_name: str,
    briefing_id: str,
    signals: list[dict],
    memory_snippets: list[dict],
    goals_snapshot: list[dict],
    entity_id: str | None = None,
    workspace_id: str | None = None,
    workspace_summary: Optional[dict] = None,
) -> Briefing:
    """Single Runtime text completion with one repair retry. Falls back to a
    rule-based stub when the LLM is unreachable."""
    if not signals:
        return _fallback_briefing(
            briefing_id, signals, workspace_summary=workspace_summary,
        )

    system_prompt = runtime_briefing_system_prompt(workspace_name)
    user_prompt = runtime_briefing_user_prompt(
        briefing_id=briefing_id,
        signals=signals,
        memory_snippets=memory_snippets,
        goals_snapshot=goals_snapshot,
        workspace_summary=workspace_summary,
    )

    raw = await _safe_runtime_completion(
        system_prompt,
        user_prompt,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if raw is None:
        logger.warning("briefing: LLM unavailable — falling back to rule-based stub")
        return _fallback_briefing(
            briefing_id, signals, workspace_summary=workspace_summary,
        )

    parsed = _parse_briefing(raw, briefing_id=briefing_id)
    if parsed is not None:
        return parsed

    repair = runtime_validation_retry_user_prompt(
        user_prompt,
        "Return ONLY a valid JSON Briefing matching the schema above.",
    )
    raw2 = await _safe_runtime_completion(
        system_prompt,
        repair,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    parsed2 = _parse_briefing(raw2 or "", briefing_id=briefing_id)
    if parsed2 is not None:
        return parsed2

    logger.warning("briefing: LLM produced invalid JSON twice — using stub")
    return _fallback_briefing(
        briefing_id, signals, workspace_summary=workspace_summary,
    )


async def _safe_runtime_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
) -> Optional[str]:
    try:
        completion = await runtime_execute_briefing_completion(
            system_prompt,
            user_prompt,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        return completion.content
    except Exception as exc:
        logger.warning("briefing LLM call failed: %s", exc)
        return None


def _parse_briefing(text: str, *, briefing_id: str) -> Optional[Briefing]:
    if not text:
        return None
    candidate = _strip_code_fence(text).strip()
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            data.setdefault("briefing_id", briefing_id)
        return Briefing.model_validate(data)
    except (ValidationError, ValueError) as exc:
        logger.debug("briefing parse failed: %s", exc)
        return None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _fallback_briefing(
    briefing_id: str,
    signals: list[dict],
    *,
    workspace_summary: Optional[dict] = None,
) -> Briefing:
    """Rule-of-thumb classifier so dev/CI runs without API key still
    produce a non-empty briefing.

    Heuristic: subject containing "invoice" / "due" / "urgent" → urgent;
    sender from a recognisable transactional source → skip;
    everything else → respond with a placeholder draft.
    """
    items: list[BriefingItem] = []
    for s in signals:
        subj = (s.get("subject") or "").lower()
        sender = (s.get("sender") or "").lower()
        if any(k in subj for k in ("invoice", "due", "urgent", "payment due")):
            cat = "urgent"
            actions = [
                BriefingAction(kind="manual", label="Open in Gmail",
                                payload={"thread_ref": s.get("thread_ref")}),
            ]
            draft = None
        elif any(k in sender for k in ("newsletter", "noreply", "no-reply", "receipts@")):
            cat = "skip"
            actions = []
            draft = None
        elif "?" in (s.get("body_text") or s.get("snippet") or ""):
            cat = "respond"
            draft = (
                "Hi,\n\nThanks for the note. "
                "Re your question — yes that works on my end. "
                "Let me know if you need anything else.\n\nBest"
            )
            actions = [
                BriefingAction(kind="send_reply", label="Send draft",
                                payload={"thread_ref": s.get("thread_ref"), "draft": draft}),
            ]
        else:
            cat = "fyi"
            draft = None
            actions = []

        items.append(BriefingItem(
            source=s["source"],
            source_ref=s["source_ref"],
            category=cat,
            title=s.get("subject") or "(no subject)",
            summary=(s.get("snippet") or s.get("body_text", ""))[:200],
            received_at=s.get("received_at"),
            sender=s.get("sender"),
            draft_reply=draft,
            actions=actions,
        ))

    summary_actions = _summary_action_items(workspace_summary)
    if summary_actions:
        items.append(BriefingItem(
            source="manual",
            source_ref=_summary_source_ref(workspace_summary),
            category="urgent",
            title="Workspace needs attention",
            summary="; ".join(summary_actions[:3])[:600],
            actions=[
                BriefingAction(kind="manual", label="Open workspace",
                                payload={"workspace_id": (workspace_summary or {}).get("workspace", {}).get("id")}),
            ],
        ))

    urgent_n = sum(1 for i in items if i.category == "urgent")
    respond_n = sum(1 for i in items if i.category == "respond")
    if urgent_n or respond_n:
        headline = f"{urgent_n} urgent · {respond_n} reply drafts ready"
    elif workspace_summary:
        headline = "Workspace is clear — no urgent human action detected."
    else:
        headline = "Inbox is clear."
    notes = None
    if workspace_summary and not summary_actions:
        actions = workspace_summary.get("recommended_action_items") or []
        notes = actions[0] if actions else None
    return Briefing(
        briefing_id=briefing_id,
        headline=headline,
        items=items,
        notes=notes,
        metrics_snapshot={"workspace_daily_summary": workspace_summary}
        if workspace_summary else None,
    )


def _summary_action_items(workspace_summary: Optional[dict]) -> list[str]:
    if not workspace_summary:
        return []
    actions = [
        str(item).strip()
        for item in (workspace_summary.get("recommended_action_items") or [])
        if str(item).strip()
    ]
    return [
        item for item in actions
        if "no urgent human action detected" not in item.lower()
    ]


def _summary_source_ref(workspace_summary: Optional[dict]) -> str:
    window = (workspace_summary or {}).get("window") or {}
    date = window.get("date") or "latest"
    return f"workspace_daily_summary:{date}"[:128]
