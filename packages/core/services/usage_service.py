"""Token usage tracking service — log and query LLM token consumption.

Primary API for callers:
    record_llm_usage(db, entity_id, usage, source)  — one call records everything
    log_token_usage(db, entity_id, model, tokens...)  — low-level row insert
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.models.base import generate_ulid
from packages.core.services.timezone_utils import user_range_start_utc

logger = logging.getLogger(__name__)
_token_usage_context_breakdown_exists: Optional[bool] = None
_token_usage_column_exists: dict[str, bool] = {}


def _usage_is_byok(usage: dict) -> bool:
    """Return whether normalized usage was produced by a user-owned LLM key."""

    if bool(usage.get("byok")):
        return True
    for key in ("billing_mode", "llm_billing_mode", "api_key_source", "llm_api_key_source"):
        if str(usage.get(key) or "").lower() == "byok":
            return True
    return False


async def _token_usage_has_context_breakdown(db: AsyncSession) -> bool:
    global _token_usage_context_breakdown_exists
    if _token_usage_context_breakdown_exists is not None:
        return _token_usage_context_breakdown_exists
    if not hasattr(db, "execute"):
        return True
    try:
        result = await db.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'token_usage_logs' "
            "AND column_name = 'context_breakdown' LIMIT 1"
        ))
        _token_usage_context_breakdown_exists = result.scalar_one_or_none() is not None
        return _token_usage_context_breakdown_exists
    except Exception:
        rollback = getattr(db, "rollback", None)
        if rollback:
            await rollback()
        return False


async def _token_usage_has_column(db: AsyncSession, column_name: str) -> bool:
    if column_name == "context_breakdown":
        return await _token_usage_has_context_breakdown(db)
    if column_name in _token_usage_column_exists:
        return _token_usage_column_exists[column_name]
    if not hasattr(db, "execute"):
        return True
    try:
        result = await db.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'token_usage_logs' "
            "AND column_name = :column_name LIMIT 1"
        ), {"column_name": column_name})
        exists = result.scalar_one_or_none() is not None
        _token_usage_column_exists[column_name] = exists
        return exists
    except Exception:
        rollback = getattr(db, "rollback", None)
        if rollback:
            await rollback()
        return False


async def log_token_usage(
    db: AsyncSession,
    entity_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    *,
    workspace_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    provider: Optional[str] = None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    context_breakdown: Optional[dict[str, Any]] = None,
    cost_usd: Optional[float] = None,
    duration_ms: Optional[int] = None,
    source: Optional[str] = None,
    billing_mode: Optional[str] = None,
    api_key_source: Optional[str] = None,
    pricing_source: Optional[str] = None,
) -> TokenUsageLog:
    """Insert a new token usage log entry."""
    entry_id = generate_ulid()
    has_context_breakdown = await _token_usage_has_context_breakdown(db)
    has_billing_mode = await _token_usage_has_column(db, "billing_mode")
    has_api_key_source = await _token_usage_has_column(db, "api_key_source")
    has_pricing_source = await _token_usage_has_column(db, "pricing_source")
    if not (has_context_breakdown and has_billing_mode and has_api_key_source and has_pricing_source):
        columns = [
            "id", "entity_id", "workspace_id", "agent_id", "user_id", "conversation_id",
            "model", "provider", "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_tokens", "cache_creation_tokens", "cost_usd", "duration_ms", "source",
        ]
        values = [
            ":id", ":entity_id", ":workspace_id", ":agent_id", ":user_id", ":conversation_id",
            ":model", ":provider", ":prompt_tokens", ":completion_tokens", ":total_tokens",
            ":cache_read_tokens", ":cache_creation_tokens", ":cost_usd", ":duration_ms", ":source",
        ]
        params = {
            "id": entry_id,
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "model": model,
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cache_creation_tokens": int(cache_creation_tokens or 0),
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "source": source,
        }
        if has_billing_mode:
            columns.append("billing_mode")
            values.append(":billing_mode")
            params["billing_mode"] = billing_mode
        if has_api_key_source:
            columns.append("api_key_source")
            values.append(":api_key_source")
            params["api_key_source"] = api_key_source
        if has_pricing_source:
            columns.append("pricing_source")
            values.append(":pricing_source")
            params["pricing_source"] = pricing_source
        if has_context_breakdown:
            columns.append("context_breakdown")
            values.append(":context_breakdown")
            params["context_breakdown"] = context_breakdown
        await db.execute(
            text(
                "INSERT INTO token_usage_logs "
                f"({', '.join(columns)}) VALUES ({', '.join(values)})"
            ),
            params,
        )
        return TokenUsageLog(
            id=entry_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            model=model,
            provider=provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=int(cache_read_tokens or 0),
            cache_creation_tokens=int(cache_creation_tokens or 0),
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            source=source,
            billing_mode=billing_mode if has_billing_mode else None,
            api_key_source=api_key_source if has_api_key_source else None,
            pricing_source=pricing_source if has_pricing_source else None,
        )

    entry_kwargs = {
        "id": entry_id,
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "model": model,
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_creation_tokens": int(cache_creation_tokens or 0),
        "context_breakdown": context_breakdown,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "source": source,
    }
    if has_billing_mode:
        entry_kwargs["billing_mode"] = billing_mode
    if has_api_key_source:
        entry_kwargs["api_key_source"] = api_key_source
    if has_pricing_source:
        entry_kwargs["pricing_source"] = pricing_source
    entry = TokenUsageLog(**entry_kwargs)
    db.add(entry)
    await db.flush()
    return entry


async def list_usage(
    db: AsyncSession,
    entity_id: str,
    model: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[TokenUsageLog], int]:
    """Return paginated token usage logs for an entity, plus total count."""
    base = select(TokenUsageLog).where(TokenUsageLog.entity_id == entity_id)
    count_q = select(func.count()).select_from(TokenUsageLog).where(
        TokenUsageLog.entity_id == entity_id
    )

    if model:
        base = base.where(TokenUsageLog.model == model)
        count_q = count_q.where(TokenUsageLog.model == model)
    if source:
        base = base.where(TokenUsageLog.source == source)
        count_q = count_q.where(TokenUsageLog.source == source)

    total = (await db.execute(count_q)).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(TokenUsageLog.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return list(rows), total


async def get_usage_summary(
    db: AsyncSession,
    entity_id: str,
    days: int = 30,
    *,
    timezone_name: str | None = None,
) -> dict:
    """Aggregate token usage for the last N days.

    Returns dict with total_tokens, total_cost, by_model, by_source.
    """
    cutoff = user_range_start_utc(timezone_name, days)
    base_filter = (
        (TokenUsageLog.entity_id == entity_id)
        & (TokenUsageLog.created_at >= cutoff)
    )

    # Totals
    totals_q = select(
        func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("total_cost"),
    ).where(base_filter)
    totals_row = (await db.execute(totals_q)).one()

    # By model
    by_model_q = (
        select(
            TokenUsageLog.model,
            func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("cost"),
        )
        .where(base_filter)
        .group_by(TokenUsageLog.model)
        .order_by(func.sum(TokenUsageLog.total_tokens).desc())
    )
    by_model_rows = (await db.execute(by_model_q)).all()

    # By source
    by_source_q = (
        select(
            TokenUsageLog.source,
            func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("cost"),
        )
        .where(base_filter)
        .group_by(TokenUsageLog.source)
        .order_by(func.sum(TokenUsageLog.total_tokens).desc())
    )
    by_source_rows = (await db.execute(by_source_q)).all()

    return {
        "total_tokens": int(totals_row.total_tokens),
        "total_cost": float(totals_row.total_cost),
        "by_model": [
            {"model": r.model, "tokens": int(r.tokens), "cost": float(r.cost)}
            for r in by_model_rows
        ],
        "by_source": [
            {"source": r.source, "tokens": int(r.tokens), "cost": float(r.cost)}
            for r in by_source_rows
        ],
    }


# ---------------------------------------------------------------------------
# High-level recording — single call does all tracking + billing
# ---------------------------------------------------------------------------

async def record_llm_usage(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    usage: dict,
    duration_ms: int = 0,
    source: str = "chat",
) -> None:
    """Record LLM usage in one call: token log + billing + AI budget.

    ``usage`` dict should contain: prompt_tokens (or prompt),
    completion_tokens (or completion), total_tokens (or total), model,
    and (optionally) ``provider`` — captured at auto-record time so
    fallback routing is visible in slice-by-provider reports.

    Best-effort — failures are logged, never raised.
    """
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("prompt") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("completion") or 0)
    total_tokens = int(usage.get("total_tokens") or usage.get("total") or 0)
    cache_read = int(usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation") or usage.get("cache_creation_input_tokens") or 0)
    context_breakdown = (
        usage.get("context_attribution_total")
        or usage.get("context_attribution")
        or usage.get("context_breakdown")
    )
    if not isinstance(context_breakdown, dict):
        context_breakdown = None
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    if total_tokens == 0:
        return

    model_name = str(usage.get("model") or "unknown")
    provider = usage.get("provider") or None
    pricing_source = (
        usage.get("pricing_source")
        or usage.get("llm_pricing_source")
        or usage.get("route_source")
        or usage.get("api_key_source")
        or usage.get("llm_api_key_source")
        or None
    )
    reported_cost = None
    try:
        reported_cost = float(usage["cost_usd"]) if usage.get("cost_usd") else None
    except Exception:
        reported_cost = None
    estimated_cost = reported_cost
    is_byok = _usage_is_byok(usage)
    billing_mode = str(
        usage.get("billing_mode")
        or usage.get("llm_billing_mode")
        or ("byok" if is_byok else "platform")
    ).lower()
    api_key_source = str(
        usage.get("api_key_source")
        or usage.get("llm_api_key_source")
        or ("byok" if is_byok else "platform")
    ).lower()
    if estimated_cost is None and not is_byok:
        try:
            from packages.core.services.billing_service import estimate_provider_cost

            estimated_cost = estimate_provider_cost(
                prompt_tokens,
                completion_tokens,
                model_name,
                pricing_source=str(pricing_source or ""),
                provider=str(provider or ""),
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
            )
        except Exception:
            logger.debug("record_llm_usage: cost estimate failed", exc_info=True)
            estimated_cost = None

    # 1. Token usage log row
    try:
        await log_token_usage(
            db, entity_id, model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            workspace_id=workspace_id,
            agent_id=agent_id, user_id=user_id,
            conversation_id=conversation_id,
            provider=provider,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            context_breakdown=context_breakdown,
            cost_usd=float(estimated_cost) if estimated_cost is not None else None,
            duration_ms=duration_ms, source=source,
            billing_mode=billing_mode,
            api_key_source=api_key_source,
            pricing_source=str(pricing_source or ""),
        )
    except Exception:
        logger.warning("record_llm_usage: token log failed", exc_info=True)
        await db.rollback()

    # 2. Credit billing (model-aware pricing)
    # BYOK (Bring Your Own Key): log usage for analytics but charge 0 credits
    try:
        from packages.core.services.billing_service import (
            record_token_usage as record_billing,
            estimate_provider_cost,
        )
        if not is_byok:
            billing_log = await record_billing(
                db, entity_id,
                input_tokens=prompt_tokens, output_tokens=completion_tokens,
                model=model_name, provider=provider,
                pricing_source=str(pricing_source or ""),
                workspace_id=workspace_id,
                agent_id=agent_id, user_id=user_id,
                conversation_id=conversation_id,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                cost_usd=reported_cost,
                business_type=source, duration_ms=duration_ms,
            )
            # 4. AI budget tracking (dollar-based metering for plan limits)
            provider_cost = (
                reported_cost
                if reported_cost is not None
                else estimate_provider_cost(
                    prompt_tokens, completion_tokens, model_name,
                    pricing_source=str(pricing_source or ""),
                    provider=str(provider or ""),
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_creation,
                )
            )
            if workspace_id:
                try:
                    from packages.core.budget import accumulate_workspace_ai_cost
                    from packages.core.services.credit_service import credits_to_usd

                    await accumulate_workspace_ai_cost(
                        db,
                        workspace_id=workspace_id,
                        cost_usd=credits_to_usd(int(billing_log.total_credit or 0)),
                    )
                except Exception:
                    logger.warning(
                        "record_llm_usage: workspace budget accumulation failed",
                        exc_info=True,
                    )
            from packages.core.services.plan_enforcement import record_ai_cost
            await record_ai_cost(db, entity_id, provider_cost, model_name)
    except Exception:
        logger.warning("record_llm_usage: billing failed", exc_info=True)


async def record_chat_llm_usage(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None = None,
    conversation_id: str | None,
    usage: dict,
    duration_ms: int | None = None,
    fallback_model: str | None = None,
) -> None:
    """Best-effort chat LLM usage persistence with chat-specific metadata."""

    if not db or not entity_id:
        return
    try:
        from packages.core.ai.runtime import RUNTIME_CHAT_SOURCE, runtime_is_byok_call_active
        from packages.core.services.model_resolver import llm_provider_from_model

        has_explicit_billing_source = any(
            key in usage
            for key in ("byok", "billing_mode", "llm_billing_mode", "api_key_source", "llm_api_key_source")
        )
        if runtime_is_byok_call_active() and not has_explicit_billing_source:
            usage = {
                **usage,
                "byok": True,
                "billing_mode": "byok",
                "llm_billing_mode": "byok",
                "api_key_source": "byok",
                "llm_api_key_source": "byok",
            }
        if fallback_model and not usage.get("model"):
            usage = {**usage, "model": fallback_model}
        if "provider" not in usage:
            usage = {**usage, "provider": llm_provider_from_model(usage.get("model"))}

        await record_llm_usage(
            db,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            usage=usage,
            duration_ms=duration_ms or 0,
            source=RUNTIME_CHAT_SOURCE,
        )
    except Exception:
        logger.warning("record_chat_llm_usage: failed", exc_info=True)


# ── Tool-call logging ────────────────────────────────────────────────


async def record_tool_call(
    db: AsyncSession,
    *,
    entity_id: str,
    tool_name: str,
    workspace_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    source: Optional[str] = None,
    round_num: Optional[int] = None,
    duration_ms: Optional[int] = None,
    result_chars: Optional[int] = None,
    success: bool = True,
    error: Optional[str] = None,
    tool_args: Optional[dict] = None,
) -> None:
    """Append-only insert into ``tool_call_logs``. Best-effort.

    Caller MUST commit. Failure is logged but never raised — this is
    observability, not a gate. ``tool_args`` is stored verbatim for
    audit + debugging; large values are truncated by the chat logger
    before reaching here.
    """
    try:
        row = ToolCallLog(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            source=source,
            round_num=round_num,
            duration_ms=duration_ms,
            result_chars=result_chars,
            success=success,
            error=error,
            tool_args=tool_args,
        )
        db.add(row)
        await db.flush()
    except Exception:
        logger.debug("record_tool_call failed (best-effort)", exc_info=True)


# ── Media usage (image / video / audio / embedding) ─────────────────

async def record_media_usage(
    db: AsyncSession,
    entity_id: str,
    *,
    kind: str,
    model: str,
    cost_usd: float,
    units: Optional[int] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    source: str = "tool",
    byok: bool = False,
) -> bool:
    """Record a non-token-based LLM-style call (image / video / TTS /
    Whisper / embedding).

    For these calls the cost can't be derived from a token count — it's
    set by the API provider (per image, per second of video, per char
    of speech). The caller computes ``cost_usd`` from its own pricing
    table and we handle credit conversion + budget tracking here.

    ``kind`` is a free-form label: ``image`` / ``video`` / ``tts`` /
    ``whisper`` / ``embedding``. Stored in ``credit_usage_logs.business_type``.

    ``units`` is optional metadata (image count, video seconds, char
    count) — written into the log row for analytics but not billed
    again here.

    Caller MUST commit. Failure is logged but not raised — billing is
    a best-effort observer of the underlying API call, never a gate
    that should fail the user-visible operation.
    """
    if cost_usd is None or cost_usd <= 0:
        return False

    # BYOK: log for analytics but skip credit billing and budget tracking
    if byok:
        return False

    from packages.core.services.billing_service import AI_MARGIN, CREDITS_PER_USD
    from packages.core.services.model_pricing_gateway import cost_to_credits

    credits = max(
        1,
        cost_to_credits(
            float(cost_usd),
            credits_per_usd=CREDITS_PER_USD,
            ai_margin=AI_MARGIN,
        ),
    )

    # 1. credit_usage_logs row (analytics + audit)
    try:
        from packages.core.models.billing import CreditUsageLog
        row = CreditUsageLog(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            input_tokens=0, output_tokens=0,
            total_tokens=int(units or 0),  # repurpose for unit count
            input_credit=0, output_credit=0,
            direct_credit=int(credits),
            total_credit=int(credits),
            model=model,
            cost_usd=float(cost_usd),
            business_type=kind,
            duration_ms=int(duration_ms or 0),
        )
        db.add(row)
        await db.flush()
    except Exception:
        logger.warning("record_media_usage: log failed", exc_info=True)
        return False

    # 2. Mirror credit total onto Entity.settings.used_credits
    try:
        from sqlalchemy import select as sa_select
        from packages.core.models.user import Entity
        result = await db.execute(sa_select(Entity).where(Entity.id == entity_id))
        entity = result.scalar_one_or_none()
        if entity:
            settings = dict(entity.settings or {})
            settings["used_credits"] = int(settings.get("used_credits", 0)) + credits
            entity.settings = settings
            await db.flush()
    except Exception:
        logger.warning("record_media_usage: used_credits update failed", exc_info=True)

    # 3. AI budget (ai_usage_usd)
    try:
        from packages.core.services.plan_enforcement import record_ai_cost
        await record_ai_cost(db, entity_id, float(cost_usd), model)
    except Exception:
        logger.warning("record_media_usage: budget update failed", exc_info=True)

    return True
