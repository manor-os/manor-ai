"""Seed initial memory MD files for a fresh workspace.

Called once from the workspace setup wizard. Drops a tiny set of
guidance / fact entries so the Strategist + Planner have *something*
to read on first review (otherwise the prompts get an empty memory
block which the LLM tends to ignore).

The seed content is workspace-kind-aware where it can be — a
``social_media`` workspace gets a different "voice" guidance template
than a ``property`` workspace. Anything missing falls back to a
generic note.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.memory.service import record_memory

logger = logging.getLogger(__name__)


async def seed_workspace_memory(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    workspace_name: str,
    workspace_kind: Optional[str],
    services: Optional[list[dict]] = None,
) -> int:
    """Drop the starter memory entries. Returns count written.

    Idempotent at the DB level via the ULID — running twice creates
    duplicates *only* if the file system was wiped between runs (the
    seed_*.md files would still exist from the first run; ``record_memory``
    uses fresh ULIDs so re-runs would create new rows). The wizard
    only calls this once during initial setup, so duplicates aren't a
    real concern in practice.
    """
    written = 0
    services = services or []

    # ── Generic charter ──
    await record_memory(
        db,
        entity_id=entity_id, workspace_id=workspace_id,
        scope="fact",
        title=f"About {workspace_name}",
        body=_build_charter(workspace_name, workspace_kind, services),
        tags=["charter", "context"],
        source="workspace_setup",
        importance=8,
        slug="about",
    )
    written += 1

    # ── Voice / style guidance — kind-aware ──
    voice_body = _voice_template_for_kind(workspace_kind)
    if voice_body:
        await record_memory(
            db,
            entity_id=entity_id, workspace_id=workspace_id,
            scope="guidance",
            title="Voice and tone",
            body=voice_body,
            tags=["voice", "style"],
            source="workspace_setup",
            importance=7,
            slug="voice",
        )
        written += 1

    # ── HITL preferences default — solo founders should be able to
    # tweak how aggressive the Strategist is.
    await record_memory(
        db,
        entity_id=entity_id, workspace_id=workspace_id,
        scope="preference",
        title="Approval defaults",
        body=_DEFAULT_APPROVAL_PREFS,
        tags=["hitl", "approval"],
        source="workspace_setup",
        importance=6,
        slug="approval-defaults",
    )
    written += 1

    return written


# ── Templates ─────────────────────────────────────────────────────────

def _build_charter(name: str, kind: Optional[str], services: list[dict]) -> str:
    service_lines = "\n".join(
        f"- **{s.get('service_key', '?')}** — {s.get('description', '')} "
        f"(autonomy: {s.get('autonomy_level', 'assisted')})"
        for s in services
    ) or "_No services configured yet._"

    kind_line = f"\n**Kind**: {kind}" if kind else ""

    return f"""\
# About {name}

This workspace exists to do work autonomously on the operator's behalf.{kind_line}

## Services

{service_lines}

## How memory is used

Notes in this workspace's memory feed both the Strategist (when it
proposes weekly tasks) and the Planner (when it breaks tasks into
steps). Edit any file under `memory/` to teach the system how you
work — the system will pick it up on the next sync.
"""


def _voice_template_for_kind(kind: Optional[str]) -> str:
    if kind in ("social_media", "social", "content"):
        return _VOICE_SOCIAL
    if kind in ("property", "real_estate"):
        return _VOICE_PROPERTY
    if kind in ("ecommerce", "shop", "shopify"):
        return _VOICE_ECOMMERCE
    return _VOICE_GENERIC


_VOICE_SOCIAL = """\
# Voice and tone

The default voice for content this workspace produces.

## Style
- First person, conversational, never corporate
- One idea per post; cut everything that isn't load-bearing
- Concrete examples beat abstract claims

## Topics
- ✅ tutorials, behind-the-scenes, learnings, hot takes with proof
- ❌ generic motivation, hashtag spam, engagement bait

## Cadence
- Quality over volume — better 3 strong posts/week than 7 weak ones
- Edit yesterday's posts harder than you wrote them

_The Strategist references this when proposing content; replace with
your own voice as you learn what works._
"""

_VOICE_PROPERTY = """\
# Voice and tone

Communication with guests and prospects.

## Style
- Warm, professional, anticipate questions
- Confirm bookings within 1 hour during business hours
- Give specific times and addresses; never vague

## Don't
- Don't quote prices without checking the calendar first
- Don't promise upgrades without confirming availability
"""

_VOICE_ECOMMERCE = """\
# Voice and tone

Customer support + marketing copy default voice.

## Style
- Direct, helpful, lead with the answer
- Use product names exactly as they appear in the catalog
- Refunds are a yes-by-default for any order under $50; flag higher
"""

_VOICE_GENERIC = """\
# Voice and tone

Default communication style for this workspace.

## Style
- Plain language, short sentences
- Be specific — concrete numbers, dates, names
- If unsure, ask one clarifying question rather than guess

_Edit this note to capture your preferred tone — Strategist and
Planner read it when generating drafts on your behalf._
"""


_DEFAULT_APPROVAL_PREFS = """\
# Approval defaults

How aggressive the Strategist should be without asking.

## Auto-approved
- Read-only actions (search, fetch, list)
- Drafting (LLM-only steps with no side effects)

## Always ask first
- Posting to public channels (Twitter, LinkedIn, Slack)
- Sending email or SMS to people you haven't messaged before
- Spending money (Stripe charges, paid API actions)

## Hard limits
- Never delete data without explicit confirmation
- Never DM more than 5 people in one plan without approval

_Edit this note to expand or tighten — Manor turns these preferences into
workspace governance rules, which the runtime enforces before actions run._
"""
