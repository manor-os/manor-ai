"""Intent→path memory: cache-first tool routing (spec §A3).

Layering: Redis blob per user (via packages.core.cache) over the
tool_intent_paths table. All writes fire-and-forget from the caller's
perspective; all reads degrade to "no hint" on any failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.models.base import generate_ulid
from packages.core.models.tool_path_memory import ToolIntentPath

logger = logging.getLogger(__name__)

MAX_TERMS = 12
ROW_CAP = 200
REDIS_TTL_SECONDS = 15 * 60
SUPPRESS_FAILURES = 2
MATCH_THRESHOLD = 2          # min overlap score to count as a "strong" match
HINT_MAX_PATHS = 3
DECAY_HALF_LIFE_DAYS = 14


def _cache_key(entity_id: str, user_id: str) -> str:
    return f"tool_paths:{entity_id}:{user_id}"


def intent_signature(user_message: str) -> str:
    from packages.core.ai.runtime.tool_discovery import runtime_search_terms

    terms = runtime_search_terms(str(user_message or ""))
    uniq = sorted({t for t in terms if t and not t.startswith("+")})[:MAX_TERMS]
    return " ".join(uniq)


def overlap_score(sig_a: str, sig_b: str) -> int:
    a, b = set(sig_a.split(" ")), set(sig_b.split(" "))
    a.discard("")
    b.discard("")
    return len(a & b)


def _decay(ts: Optional[datetime]) -> float:
    if ts is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)


@dataclass(frozen=True)
class IntentPath:
    provider: str
    tool_name: str
    success_count: int
    failure_count: int
    last_success_at: Optional[str]
    last_failure_at: Optional[str]
    intent_signature: str

    @property
    def suppressed(self) -> bool:
        if self.failure_count < SUPPRESS_FAILURES:
            return False
        if not self.last_failure_at:
            return False
        return (self.last_success_at or "") < self.last_failure_at

    def rank(self) -> float:
        ls = datetime.fromisoformat(self.last_success_at) if self.last_success_at else None
        lf = datetime.fromisoformat(self.last_failure_at) if self.last_failure_at else None
        return self.success_count * _decay(ls) - 2.0 * self.failure_count * _decay(lf)


def _provider_of(tool_name: str) -> str:
    from packages.core.ai.runtime.tool_discovery import (
        runtime_mcp_provider_from_tool_name,
    )
    return runtime_mcp_provider_from_tool_name(tool_name) or ""


async def record_success(
    db: AsyncSession, *, entity_id: str, user_id: str,
    user_message: str, tool_name: str,
) -> None:
    await _record(db, entity_id=entity_id, user_id=user_id,
                  user_message=user_message, tool_name=tool_name, success=True)


async def record_failure(
    db: AsyncSession, *, entity_id: str, user_id: str,
    user_message: str, tool_name: str,
) -> None:
    await _record(db, entity_id=entity_id, user_id=user_id,
                  user_message=user_message, tool_name=tool_name, success=False)


async def _record(
    db: AsyncSession, *, entity_id: str, user_id: str,
    user_message: str, tool_name: str, success: bool,
) -> None:
    try:
        sig = intent_signature(user_message)
        if not sig:
            return
        now = datetime.now(timezone.utc)
        row = (await db.execute(
            select(ToolIntentPath).where(
                ToolIntentPath.entity_id == entity_id,
                ToolIntentPath.user_id == user_id,
                ToolIntentPath.intent_signature == sig,
                ToolIntentPath.tool_name == tool_name,
            ).limit(1)
        )).scalar_one_or_none()
        if row is None and not success:
            # Review #1: a hinted path's failure almost never repeats the
            # EXACT intent_signature (users paraphrase — "再发一条 x post 吧"
            # vs the seeded "发一条 x post"), while the hint that surfaced
            # this tool in the first place was itself found via fuzzy
            # overlap. Looking up only the exact signature here meant a
            # real failure of a hinted path silently vanished (miss +
            # "never create rows from failures alone" == suppression never
            # engages in practice). Fall back to the user's best-overlap
            # row for this SAME tool_name before giving up.
            candidates = (await db.execute(
                select(ToolIntentPath).where(
                    ToolIntentPath.entity_id == entity_id,
                    ToolIntentPath.user_id == user_id,
                    ToolIntentPath.tool_name == tool_name,
                )
            )).scalars().all()
            best: ToolIntentPath | None = None
            best_score = MATCH_THRESHOLD - 1
            for candidate in candidates:
                score = overlap_score(sig, candidate.intent_signature)
                if score > best_score:
                    best = candidate
                    best_score = score
            row = best
        if row is None:
            if not success:
                return  # never create rows from failures alone
            row = ToolIntentPath(
                id=generate_ulid(), entity_id=entity_id, user_id=user_id,
                intent_signature=sig, provider=_provider_of(tool_name),
                tool_name=tool_name, success_count=0, failure_count=0,
            )
            db.add(row)
        if success:
            row.success_count += 1
            row.failure_count = 0          # fresh success revives
            row.last_success_at = now
        else:
            row.failure_count += 1
            row.last_failure_at = now
        await db.flush()
        await _enforce_row_cap(db, entity_id=entity_id, user_id=user_id)
        await cache.delete(_cache_key(entity_id, user_id))  # write-through-lite: invalidate
    except Exception:
        logger.warning("tool_path_memory record failed", exc_info=True)


async def _enforce_row_cap(db: AsyncSession, *, entity_id: str, user_id: str) -> None:
    rows = (await db.execute(
        select(ToolIntentPath).where(
            ToolIntentPath.entity_id == entity_id,
            ToolIntentPath.user_id == user_id,
        )
    )).scalars().all()
    if len(rows) <= ROW_CAP:
        return
    def _tier(r: ToolIntentPath) -> tuple:
        suppressed = (
            r.failure_count >= SUPPRESS_FAILURES
            and (r.last_success_at or datetime.min.replace(tzinfo=timezone.utc))
            < (r.last_failure_at or datetime.min.replace(tzinfo=timezone.utc))
        )
        anchor = (r.last_failure_at if suppressed else r.last_success_at) \
            or datetime.min.replace(tzinfo=timezone.utc)
        return (0 if suppressed else 1, anchor)  # suppressed-oldest first
    for r in sorted(rows, key=_tier)[: len(rows) - ROW_CAP]:
        await db.delete(r)


async def _load_user_paths(entity_id: str, user_id: str) -> list[IntentPath]:
    key = _cache_key(entity_id, user_id)
    cached = await cache.get(key)
    if isinstance(cached, list):
        try:
            return [IntentPath(**item) for item in cached]
        except Exception:
            await cache.delete(key)
    from packages.core.database import async_session
    try:
        async with async_session() as db:
            rows = (await db.execute(
                select(ToolIntentPath).where(
                    ToolIntentPath.entity_id == entity_id,
                    ToolIntentPath.user_id == user_id,
                )
            )).scalars().all()
    except Exception:
        logger.warning("tool_path_memory load failed", exc_info=True)
        return []
    paths = [
        IntentPath(
            provider=r.provider, tool_name=r.tool_name,
            success_count=r.success_count, failure_count=r.failure_count,
            last_success_at=r.last_success_at.isoformat() if r.last_success_at else None,
            last_failure_at=r.last_failure_at.isoformat() if r.last_failure_at else None,
            intent_signature=r.intent_signature,
        )
        for r in rows
    ]
    await cache.set(key, [p.__dict__ for p in paths], ttl=REDIS_TTL_SECONDS)
    return paths


async def lookup_paths(
    *, entity_id: str, user_id: str, user_message: str,
    limit: int = HINT_MAX_PATHS,
) -> list[IntentPath]:
    """Cache-first lookup: strongest-overlap, non-suppressed, ranked paths."""
    try:
        sig = intent_signature(user_message)
        if not sig:
            return []
        candidates = []
        for p in await _load_user_paths(entity_id, user_id):
            score = overlap_score(sig, p.intent_signature)
            if score >= MATCH_THRESHOLD and not p.suppressed:
                candidates.append((score, p.rank(), p))
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        out, seen = [], set()
        for _, _, p in candidates:
            if p.tool_name in seen:
                continue
            seen.add(p.tool_name)
            out.append(p)
            if len(out) >= limit:
                break
        return out
    except Exception:
        logger.warning("tool_path_memory lookup failed", exc_info=True)
        return []


def fold_path_boosts(paths: list[IntentPath]) -> dict[str, float] | None:
    """Fold matched paths down to one A2 ranking boost per provider (spec
    §A3). Takes the MAX rank across a provider's paths rather than
    last-write-wins (a naive ``{p.provider: p.rank() for p in paths}``
    dict comprehension silently drops all but the last path per provider
    in iteration order) and clamps negative ranks to 0 — a shaky/
    mixed-history path should never actively penalize a provider's
    ranking through this channel, it should just stop helping."""
    if not paths:
        return None
    boosts: dict[str, float] = {}
    for p in paths:
        rank = max(p.rank(), 0.0)
        if rank <= 0.0:
            continue
        if rank > boosts.get(p.provider, 0.0):
            boosts[p.provider] = rank
    return boosts or None


def format_hint(paths: list[IntentPath]) -> str:
    if not paths:
        return ""
    parts = ", ".join(
        f"{p.tool_name} ({p.success_count}×)" for p in paths
    )
    return (
        "Tasks like this previously succeeded via: "
        f"{parts} — try these first (load with search_tools "
        "query='select:<name>')."
    )
