"""Token usage tracking service — log and query LLM token consumption.

Primary API for callers:
    record_llm_usage(db, entity_id, usage, source)  — one call records everything
    log_token_usage(db, entity_id, model, tokens...)  — low-level row insert
"""
from __future__ import annotations

import json
import os
import logging
from hashlib import sha256
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import is_cloud
from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.models.base import generate_ulid
from packages.core.services.timezone_utils import user_range_start_utc

logger = logging.getLogger(__name__)
_token_usage_context_breakdown_exists: Optional[bool] = None
_token_usage_column_exists: dict[str, bool] = {}

LLM_RESERVATION_MAX_OUTPUT_TOKENS = int(
    os.getenv("CHAT_LLM_RESERVATION_MAX_OUTPUT_TOKENS", "8192") or 8192
)
_CREDIT_RESERVATION_SOURCE_ID_MAX_LENGTH = 100


def _usage_is_byok(usage: dict) -> bool:
    """Return whether normalized usage was produced by a user-owned LLM key."""

    if bool(usage.get("byok")):
        return True
    for key in ("billing_mode", "llm_billing_mode", "api_key_source", "llm_api_key_source"):
        if str(usage.get(key) or "").lower() == "byok":
            return True
    return False


async def _ledger_payer(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    payer_entity_id: str | None,
) -> str:
    """Return who a ledger write for ``entity_id``'s data charges.

    A pinned ``payer_entity_id`` must be the data owner or the provider of a
    delivery targeting the Workspace; without one the Workspace's current
    payer is resolved (``TrialSponsorUnavailable`` propagates).
    """
    from packages.core.services.billing_payer import (
        payer_entity_id as resolve_payer,
        validate_payer,
    )

    if payer_entity_id is None:
        return await resolve_payer(db, entity_id, workspace_id)
    if payer_entity_id != entity_id:
        await validate_payer(
            db, entity_id=entity_id, workspace_id=workspace_id, payer_entity_id=payer_entity_id,
        )
    return payer_entity_id


async def _ledger_payer_or_uncharged(
    db: AsyncSession,
    *,
    writer: str,
    entity_id: str,
    workspace_id: str | None,
    payer_entity_id: str | None,
    strict: bool,
    operation_id: str | None = None,
) -> str | None:
    """``_ledger_payer`` for best-effort writers: None when nobody may be charged.

    A trial whose provider is gone, or a pin that no delivery backs, leaves
    the spend uncharged. Strict writers raise instead; best-effort ones log
    it distinctly so an uncharged call is never mistaken for a transient
    billing failure.
    """
    from packages.core.services.billing_payer import TrialSponsorUnavailable

    try:
        return await _ledger_payer(
            db, entity_id=entity_id, workspace_id=workspace_id, payer_entity_id=payer_entity_id,
        )
    except (TrialSponsorUnavailable, ValueError) as exc:
        if strict:
            raise
        logger.warning(
            "%s: trial payer unavailable; usage not charged entity=%s workspace=%s payer=%s operation=%s err=%s",
            writer, entity_id, workspace_id, payer_entity_id, operation_id, exc,
        )
        return None


def _billing_task_id(entity_id: str | None, task_id: str | None) -> str | None:
    """``task_id``, else the Task the bound billing context bills ``entity_id``'s calls to."""
    if task_id:
        return task_id
    from packages.core.ai.runtime.billing import runtime_current_billing_task_id

    return runtime_current_billing_task_id(entity_id)


def chat_llm_usage_is_byok(usage: dict | None = None) -> bool:
    """Return whether the active chat call or its normalized usage is BYOK."""
    from packages.core.ai.runtime import runtime_is_byok_call_active

    return runtime_is_byok_call_active() or _usage_is_byok(usage or {})


def _usd(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def llm_call_cost_usd(usage: dict, model: str | None = None) -> tuple[float | None, str]:
    """A finished LLM call's provider cost in USD, and what it was priced on.

    1. ``cost_usd`` already worked out from official tokens by the caller.
    2. The call's tokens at the model's price (official registry, else a
       gateway's synced catalog): ``official_tokens``.
    3. Only when nothing prices the model, the gateway's own reported cost:
       ``gateway_cost_fallback``.
    4. Otherwise ``unpriced``: no cost, so no charge, and a warning.
    A gateway's reported cost is never billed while the tokens can be priced.
    """
    usage = usage or {}
    given = _usd(usage.get("cost_usd"))
    if given is not None and given > 0:
        return given, "official_tokens"
    model_name = str(model or usage.get("model") or "") or None
    try:
        from packages.core.services.billing_service import estimate_provider_cost

        cost = estimate_provider_cost(
            int(usage.get("prompt") or usage.get("prompt_tokens") or 0),
            int(usage.get("completion") or usage.get("completion_tokens") or 0),
            model_name,
            pricing_source=str(usage.get("pricing_source") or usage.get("llm_pricing_source") or ""),
            provider=str(usage.get("provider") or ""),
            cache_read_tokens=int(usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(
                usage.get("cache_creation") or usage.get("cache_creation_input_tokens") or 0
            ),
            audio_input_tokens=int(usage.get("audio_in") or usage.get("audio_input_tokens") or 0),
            audio_output_tokens=int(usage.get("audio_out") or usage.get("audio_output_tokens") or 0),
            cached_audio_input_tokens=int(usage.get("cached_audio_input_tokens") or 0),
            require_price=True,
        )
    except Exception:
        logger.warning("llm_call_cost_usd: pricing failed for %s", model_name, exc_info=True)
        cost = None
    if cost is not None:
        return float(cost), "official_tokens"
    gateway = _usd(usage.get("gateway_cost_usd"))
    if gateway is not None and gateway > 0:
        logger.warning("LLM model %s has no price; billing the gateway's reported cost", model_name)
        return gateway, "gateway_cost_fallback"
    logger.warning("LLM model %s has no price and no reported cost; nothing billed", model_name)
    return None, "unpriced"


def chat_llm_usage_to_credits(usage: dict) -> int:
    """Credits a finished chat LLM call costs, on the same price as the ledger."""
    if not usage or not is_cloud():
        return 0
    from packages.core.services.billing_service import usd_to_credits

    cost, _basis = llm_call_cost_usd(usage)
    return usd_to_credits(cost)


def durable_llm_call_source_id(source_prefix: str, round_num: int) -> str:
    """Return a bounded idempotency key for one provider call.

    ``source_prefix`` identifies one *actual loop invocation*. Callers that
    restart a loop after a crash must use a new invocation nonce: the provider
    request may really be sent again and therefore must be charged again. A
    retry of the settlement callback for that same request keeps this key.
    """

    prefix = str(source_prefix or "").strip()
    if not prefix:
        raise ValueError("source_prefix is required")
    candidate = f"{prefix}:{int(round_num)}"
    if len(candidate) <= _CREDIT_RESERVATION_SOURCE_ID_MAX_LENGTH:
        return candidate
    namespace = prefix.partition(":")[0][:20] or "llm"
    digest = sha256(candidate.encode("utf-8")).hexdigest()[:48]
    return f"{namespace}:{digest}:{int(round_num)}"


def build_durable_llm_billing_callbacks(
    *,
    source_prefix: str,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    fallback_model: str | None,
    payer_entity_id: str | None = None,
    source: str | None = None,
    operation_id: str | None = None,
    task_id: str | None = None,
):
    """Build per-provider-call reserve/settle callbacks for an agentic loop.

    Whether a call is BYOK is read from the route the call actually took (the
    loop marks it from its metadata, and the call's usage carries it). A saved
    key whose provider cannot serve the model routes to the platform, so the
    caller's view of its keys never makes a platform call free.

    ``payer_entity_id`` pins who pays. Without it the first reservation
    resolves the Workspace's payer and every later round reuses it, so a
    delivery phase change mid-loop cannot split one loop across two payers.

    ``source`` labels each usage row (the caller's billing source; chat when
    omitted). ``operation_id`` ties each row to the run that made the call;
    when omitted the row is tied to its own call.

    Reservations are committed before provider I/O. Settlement, ledger and
    reservation consumption share a later transaction. The reservation row is
    the idempotency fence for duplicate settlement callbacks. BYOK calls do
    not reserve Manor credits; without a reservation their analytics remain
    best-effort and cannot use this fence.

    Child-cost projection intentionally remains outside this helper. Coupling
    child execution locks to the reservation/entity/workspace lock chain needs
    an explicit global lock-order design rather than an arbitrary callback.
    """

    # Validate eagerly, while still deriving the final bounded id per round.
    durable_llm_call_source_id(source_prefix, 0)
    source_ids: dict[int, str] = {}
    pinned_payer: dict[str, str | None] = {"entity_id": payer_entity_id}

    def source_id(round_num: int) -> str:
        return durable_llm_call_source_id(source_prefix, round_num)

    async def reserve_llm_call(
        round_num: int,
        model: str | None,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
    ) -> None:
        call_source_id = source_id(round_num)
        source_ids[round_num] = call_source_id
        if not entity_id or not is_cloud() or chat_llm_usage_is_byok():
            return

        from packages.core.ai.agentic_loop import _estimate_tokens
        from packages.core.ai.llm_client import CreditExhaustedError
        from packages.core.database import async_session
        from packages.core.services.billing_payer import TrialSponsorUnavailable
        from packages.core.services.billing_payer import payer_entity_id as resolve_payer
        from packages.core.services.billing_service import tokens_to_credits
        from packages.core.services.credit_reservations import (
            CreditReservationError,
            TrialCreditReservationError,
            reserve_credits,
        )

        output_tokens = int(max_tokens or 0)
        if output_tokens <= 0:
            output_tokens = LLM_RESERVATION_MAX_OUTPUT_TOKENS
        try:
            estimate = max(
                1,
                int(tokens_to_credits(_estimate_tokens(messages), output_tokens, model)),
            )
            async with async_session() as db:
                if pinned_payer["entity_id"] is None:
                    # Share one payer with the bound billing context, whose
                    # credit preflight gates these same calls.
                    from packages.core.ai.runtime.billing import runtime_current_billing_context

                    bound = runtime_current_billing_context()
                    shared = bound if (
                        bound is not None
                        and bound.entity_id == entity_id
                        and bound.workspace_id == workspace_id
                    ) else None
                    if shared is not None and shared.payer_entity_id:
                        pinned_payer["entity_id"] = shared.payer_entity_id
                    else:
                        try:
                            pinned_payer["entity_id"] = await resolve_payer(db, entity_id, workspace_id)
                        except TrialSponsorUnavailable as exc:
                            raise TrialCreditReservationError() from exc
                        if shared is not None:
                            shared.payer_entity_id = pinned_payer["entity_id"]
                await reserve_credits(
                    db,
                    entity_id=entity_id,
                    payer_entity_id=pinned_payer["entity_id"],
                    amount_credits=estimate,
                    source_kind="chat_llm_call",
                    source_id=call_source_id,
                    reason="LLM call estimate",
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    metadata={
                        "round": round_num, "model": model or "",
                        "task_id": _billing_task_id(entity_id, task_id), "operation_id": operation_id,
                    },
                )
                await db.commit()
        except CreditReservationError as exc:
            raise CreditExhaustedError(str(exc), kind=exc.kind) from exc
        except Exception as exc:
            raise CreditExhaustedError(
                "Unable to reserve credits for this request. Please try again shortly."
            ) from exc

    async def settle_llm_call(
        round_num: int,
        duration_ms: float,
        usage: dict[str, Any],
    ) -> None:
        if not entity_id:
            return

        from packages.core.database import async_session
        from packages.core.services.credit_reservations import (
            resize_reservation_by_source,
        )

        call_source_id = source_ids.pop(round_num, source_id(round_num))
        billed_usage = dict(usage or {})
        billed_usage.setdefault("model", fallback_model or "")
        usage_is_byok = chat_llm_usage_is_byok(billed_usage)
        if usage_is_byok:
            billed_usage.setdefault("byok", True)
            billed_usage.setdefault("billing_mode", "byok")
            billed_usage.setdefault("api_key_source", "byok")
        actual_credits = 0 if usage_is_byok else chat_llm_usage_to_credits(billed_usage)

        if not usage_is_byok and actual_credits > 0:
            # Persist actual provider cost before the ledger write. If the
            # latter fails, an active reservation still blocks escaped spend.
            async with async_session() as db:
                await resize_reservation_by_source(
                    db,
                    source_kind="chat_llm_call",
                    source_id=call_source_id,
                    amount_credits=actual_credits,
                    allow_overdraft=True,
                )
                await db.commit()

        async with async_session() as db:
            try:
                await settle_chat_llm_call_usage(
                    db,
                    entity_id=entity_id,
                    source_id=call_source_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    usage=billed_usage,
                    duration_ms=int(duration_ms),
                    consumed_credits=actual_credits,
                    payer_entity_id=pinned_payer["entity_id"],
                    source=source,
                    operation_id=operation_id,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    return reserve_llm_call, settle_llm_call


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
    rounds: Optional[int] = None,
    source: Optional[str] = None,
    billing_mode: Optional[str] = None,
    api_key_source: Optional[str] = None,
    pricing_source: Optional[str] = None,
    operation_id: Optional[str] = None,
    media_job_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> TokenUsageLog:
    """Insert a new token usage log entry."""
    entry_id = generate_ulid()
    has_context_breakdown = await _token_usage_has_context_breakdown(db)
    has_billing_mode = await _token_usage_has_column(db, "billing_mode")
    has_api_key_source = await _token_usage_has_column(db, "api_key_source")
    has_pricing_source = await _token_usage_has_column(db, "pricing_source")
    has_rounds = await _token_usage_has_column(db, "rounds")
    has_operation_id = await _token_usage_has_column(db, "operation_id")
    has_media_job_id = await _token_usage_has_column(db, "media_job_id")
    has_task_id = await _token_usage_has_column(db, "task_id")
    if not (
        has_context_breakdown
        and has_billing_mode
        and has_api_key_source
        and has_pricing_source
        and has_rounds
        and has_operation_id
        and has_media_job_id
        and has_task_id
    ):
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
            # A raw-SQL JSON column takes the encoded text; a dict is refused by the driver.
            params["context_breakdown"] = (
                json.dumps(context_breakdown, default=str) if context_breakdown is not None else None
            )
        if has_rounds:
            columns.append("rounds")
            values.append(":rounds")
            params["rounds"] = rounds
        if has_operation_id:
            columns.append("operation_id")
            values.append(":operation_id")
            params["operation_id"] = operation_id
        if has_media_job_id:
            columns.append("media_job_id")
            values.append(":media_job_id")
            params["media_job_id"] = media_job_id
        if has_task_id:
            columns.append("task_id")
            values.append(":task_id")
            params["task_id"] = task_id
        # Column and placeholder names come only from the fixed lists above.
        await db.execute(
            text(
                "INSERT INTO token_usage_logs "
                f"({', '.join(columns)}) VALUES ({', '.join(values)})"  # nosec B608
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
            rounds=rounds if has_rounds else None,
            operation_id=operation_id if has_operation_id else None,
            media_job_id=media_job_id if has_media_job_id else None,
            task_id=task_id if has_task_id else None,
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
    if has_rounds:
        entry_kwargs["rounds"] = rounds
    if has_operation_id:
        entry_kwargs["operation_id"] = operation_id
    if has_media_job_id:
        entry_kwargs["media_job_id"] = media_job_id
    if has_task_id:
        entry_kwargs["task_id"] = task_id
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
    rounds: int | None = None,
    source: str = "chat",
    operation_id: str | None = None,
    strict: bool = False,
    payer_entity_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Record LLM usage in one call: token log + billing + AI budget.

    ``entity_id`` owns the data and keeps the token log. The credit charge,
    usage counters and AI budget go to the payer (``payer_entity_id``, or the
    Workspace's current payer when omitted).

    ``usage`` dict should contain: prompt_tokens (or prompt),
    completion_tokens (or completion), total_tokens (or total), model,
    and (optionally) ``provider`` — captured at auto-record time so
    fallback routing is visible in slice-by-provider reports.

    ``task_id`` names the Task whose execution made the call; the bound
    billing context's Task when omitted.

    Best-effort by default — failures are logged, never raised. Callers that
    need a durable provider settlement can use ``strict=True`` and commit the
    resulting transaction themselves.
    """
    task_id = _billing_task_id(entity_id, task_id)
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("prompt") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("completion") or 0)
    total_tokens = int(usage.get("total_tokens") or usage.get("total") or 0)
    cache_read = int(usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation") or usage.get("cache_creation_input_tokens") or 0)
    audio_in = int(usage.get("audio_in") or usage.get("audio_input_tokens") or 0)
    audio_out = int(usage.get("audio_out") or usage.get("audio_output_tokens") or 0)
    cached_audio_in = int(usage.get("cached_audio_input_tokens") or 0)
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
    # The provider cost this call is billed on (official tokens; see
    # llm_call_cost_usd). BYOK rows keep the same figure as an estimate.
    estimated_cost, _cost_basis = llm_call_cost_usd(
        {
            **usage,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "audio_in": audio_in,
            "audio_out": audio_out,
            "cached_audio_input_tokens": cached_audio_in,
            "pricing_source": str(pricing_source or ""),
            "provider": str(provider or ""),
        },
        model_name,
    )
    reported_cost = estimated_cost

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
            duration_ms=duration_ms, rounds=rounds, source=source,
            billing_mode=billing_mode,
            api_key_source=api_key_source,
            # A BYOK call no price source knows keeps no made-up estimate: its
            # row says so, rather than carrying a default rate.
            pricing_source=(
                "byok_unpriced" if is_byok and estimated_cost is None else str(pricing_source or "")
            ),
            operation_id=operation_id,
            task_id=task_id,
        )
    except Exception:
        logger.warning("record_llm_usage: token log failed", exc_info=True)
        if strict:
            raise
        await db.rollback()

    # 2. Credit billing (model-aware pricing)
    # BYOK (Bring Your Own Key): log usage for analytics but charge 0 credits
    try:
        from packages.core.services.billing_service import record_token_usage as record_billing

        # An unpriced call (no price and no reported cost) is logged above and
        # warned about in llm_call_cost_usd; it is never charged a guessed rate.
        payer = None if is_byok or reported_cost is None else await _ledger_payer_or_uncharged(
            db, writer="record_llm_usage", entity_id=entity_id, workspace_id=workspace_id,
            payer_entity_id=payer_entity_id, strict=strict, operation_id=operation_id,
        )
        if payer is not None:
            billing_log = await record_billing(
                db, payer,
                input_tokens=prompt_tokens, output_tokens=completion_tokens,
                model=model_name, provider=provider,
                pricing_source=str(pricing_source or ""),
                workspace_id=workspace_id,
                agent_id=agent_id, user_id=user_id,
                conversation_id=conversation_id,
                operation_id=operation_id,
                task_id=task_id,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                audio_input_tokens=audio_in,
                audio_output_tokens=audio_out,
                cached_audio_input_tokens=cached_audio_in,
                cost_usd=reported_cost,
                business_type=source, duration_ms=duration_ms,
            )
            # 4. AI budget tracking (dollar-based metering for plan limits)
            provider_cost = reported_cost
            if workspace_id:
                try:
                    from packages.core.budget import accumulate_workspace_ai_cost
                    from packages.core.services.credit_service import credits_to_usd

                    # Non-strict observers may keep the ledger write when a
                    # workspace projection is temporarily unavailable, but
                    # the spend/state/message projection itself must remain
                    # atomic. Strict Runtime settlement propagates the error
                    # so its caller rolls the whole provider settlement back.
                    async with db.begin_nested():
                        await accumulate_workspace_ai_cost(
                            db,
                            workspace_id=workspace_id,
                            # Cloud billing returns the authoritative, margin-
                            # adjusted credit charge.  The OSS facade deliberately
                            # returns no billing row; local workspaces must still
                            # account for provider-reported spend instead of
                            # silently leaving their budget at zero.
                            cost_usd=(
                                credits_to_usd(int(billing_log.total_credit or 0))
                                if billing_log is not None
                                else provider_cost
                            ),
                        )
                except Exception:
                    logger.warning(
                        "record_llm_usage: workspace budget accumulation failed",
                        exc_info=True,
                    )
                    if strict:
                        raise
            from packages.core.services.plan_enforcement import record_ai_cost
            await record_ai_cost(db, payer, provider_cost, model_name)
    except Exception:
        logger.warning("record_llm_usage: billing failed", exc_info=True)
        if strict:
            raise


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
    rounds: int | None = None,
    operation_id: str | None = None,
    strict: bool = False,
    payer_entity_id: str | None = None,
    source: str | None = None,
) -> None:
    """Best-effort chat LLM usage persistence with chat-specific metadata.

    ``source`` labels the row; chat when omitted.
    """

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
            rounds=rounds,
            source=source or RUNTIME_CHAT_SOURCE,
            operation_id=operation_id,
            strict=strict,
            payer_entity_id=payer_entity_id,
        )
    except Exception:
        logger.warning("record_chat_llm_usage: failed", exc_info=True)
        if strict:
            raise


async def settle_chat_llm_call_usage(
    db: AsyncSession,
    *,
    entity_id: str,
    source_id: str,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    usage: dict,
    duration_ms: int,
    consumed_credits: int | None = None,
    payer_entity_id: str | None = None,
    source: str | None = None,
    operation_id: str | None = None,
) -> bool:
    """Write one completed chat LLM call and consume its durable reservation.

    ``source`` and ``operation_id`` label the row; chat and the call's own
    source id when omitted.

    The charge goes to the reservation's entity, the payer pinned when the
    call was reserved, even if the Workspace's payer changed since.
    ``payer_entity_id`` only matters for a BYOK call without a reservation.

    The caller must commit this transaction. If it fails, the separately
    committed active reservation continues to block further billable work.
    The reservation row is also the idempotency fence: a concurrent retry or
    a retry after a lost commit acknowledgement observes ``consumed`` and
    must not write the ledger/workspace cost a second time.

    Return ``True`` only when this transaction transitions a durable
    reservation from active to consumed. BYOK calls deliberately may have no
    reservation and therefore only retain best-effort analytics semantics;
    they do not consume credits or workspace budget, but their token analytics
    are not deduplicated here.
    """
    if not is_cloud():
        # The public migration graph has no Cloud reservation ledger. OSS
        # records native-provider usage even when no billing metadata was
        # attached, without reading or charging a platform credit balance.
        await record_chat_llm_usage(
            db,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            usage={**usage, "byok": True, "billing_mode": "byok", "api_key_source": "byok"},
            duration_ms=duration_ms,
            operation_id=operation_id or source_id,
            source=source,
            strict=True,
            payer_entity_id=entity_id,
        )
        return False

    from packages.core.models.billing import CreditReservation
    from packages.core.services.credit_reservations import (
        RESERVATION_STATUS_ACTIVE,
        RESERVATION_STATUS_CONSUMED,
        CreditReservationError,
        _lock_entity,
        consume_reservation,
    )

    byok = chat_llm_usage_is_byok(usage)
    reservation_query = (
        select(CreditReservation)
        .where(
            CreditReservation.source_kind == "chat_llm_call",
            CreditReservation.source_id == source_id,
        )
        .order_by(
            (CreditReservation.status == RESERVATION_STATUS_ACTIVE).desc(),
            CreditReservation.created_at.desc(),
            CreditReservation.id.desc(),
        )
    )
    # Keep the same Entity -> reservation order as reserve/resize. Two
    # callbacks for one provider response can otherwise deadlock when one is
    # resizing the active reservation while the other starts settlement.
    # The locked Entity is the payer the reservation is held against, so read
    # the reservation without a lock first.
    reservation = (await db.execute(reservation_query)).scalars().first()
    if reservation is not None:
        locked_payer = reservation.entity_id
        await _lock_entity(db, locked_payer)
        reservation = (
            await db.execute(
                reservation_query.with_for_update().execution_options(populate_existing=True)
            )
        ).scalars().first()
        # The charge must land on the Entity locked above: a row that changed
        # (or vanished) between the two reads would settle unserialized.
        if reservation is None or reservation.entity_id != locked_payer:
            raise CreditReservationError(
                f"chat settlement {source_id!r} reservation changed while settling"
            )
    elif byok:
        if payer_entity_id is None:
            from packages.core.services.billing_payer import TrialSponsorUnavailable

            try:
                payer_entity_id = await _ledger_payer(
                    db, entity_id=entity_id, workspace_id=workspace_id, payer_entity_id=None,
                )
            except TrialSponsorUnavailable:
                # BYOK charges no credits; only the lock order needs an Entity.
                payer_entity_id = entity_id
        await _lock_entity(db, payer_entity_id)
    if reservation is not None:
        data_owner = (reservation.meta or {}).get("data_owner_entity_id")
        if reservation.workspace_id != workspace_id or entity_id not in (
            reservation.entity_id, data_owner,
        ):
            raise CreditReservationError(
                f"chat settlement {source_id!r} crosses entity scope"
            )
        if reservation.status == RESERVATION_STATUS_CONSUMED:
            return False
        if reservation.status != RESERVATION_STATUS_ACTIVE:
            raise CreditReservationError(
                f"chat settlement {source_id!r} reservation is {reservation.status!r}"
            )
    elif not byok:
        raise CreditReservationError(
            f"chat settlement {source_id!r} has no durable reservation"
        )

    await record_chat_llm_usage(
        db,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        usage=usage,
        duration_ms=duration_ms,
        operation_id=operation_id or source_id,
        source=source,
        strict=True,
        payer_entity_id=reservation.entity_id if reservation is not None else payer_entity_id,
    )
    if reservation is not None:
        await consume_reservation(
            db,
            reservation_id=reservation.id,
            consumed_credits=consumed_credits,
        )
        return True
    return False


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
    outcome: str = "success",
) -> None:
    """Append-only insert into ``tool_call_logs``. Best-effort.

    Caller MUST commit. Failure is logged but never raised — this is
    observability, not a gate. ``tool_args`` is stored verbatim for
    audit + debugging; large values are truncated by the chat logger
    before reaching here. ``outcome`` is additive detail on top of
    ``success``/``error`` — see ``ToolCallLog.outcome`` docstring.
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
            outcome=outcome,
        )
        db.add(row)
        await db.flush()
    except Exception:
        logger.debug("record_tool_call failed (best-effort)", exc_info=True)


# ── Media usage (image / video / audio / embedding) ─────────────────

async def _record_byok_media_usage(
    db: AsyncSession,
    *,
    entity_id: str,
    kind: str,
    model: str,
    source: str,
    workspace_id: Optional[str],
    user_id: Optional[str],
    agent_id: Optional[str],
    conversation_id: Optional[str],
    duration_ms: Optional[int],
    operation_id: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    strict: bool,
    media_job_id: Optional[str] = None,
    pricing_source: Optional[str] = None,
    estimate_usd: Optional[float] = None,
    task_id: Optional[str] = None,
) -> None:
    """Write the ``token_usage_logs`` row for a media call on the user's key.

    No credit row and no budget movement: the provider bills the user. The
    row keeps the call's model, source, run and any provider token counts so
    BYOK work stays visible next to platform work. ``estimate_usd`` is the
    approximate cost of those tokens, stored on the row only.
    """
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    try:
        await log_token_usage(
            db,
            entity_id,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=estimate_usd,
            workspace_id=workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            duration_ms=int(duration_ms) if duration_ms else None,
            source=source,
            billing_mode="byok",
            api_key_source="byok",
            operation_id=operation_id,
            media_job_id=media_job_id,
            pricing_source=pricing_source,
            task_id=task_id,
        )
    except Exception:
        logger.warning("record_media_usage: byok token log failed kind=%s", kind, exc_info=True)
        if strict:
            raise


def media_cost_credits(cost_usd: float) -> int:
    """Credits a platform media call costs; at least one credit for a call with cost."""
    from packages.core.services.billing_service import AI_MARGIN, CREDITS_PER_USD
    from packages.core.services.model_pricing_gateway import cost_to_credits

    return max(
        1,
        cost_to_credits(
            float(cost_usd),
            credits_per_usd=CREDITS_PER_USD,
            ai_margin=AI_MARGIN,
        ),
    )


async def record_media_call_unknown_cost(
    db: AsyncSession,
    entity_id: str,
    *,
    kind: str,
    model: str,
    media_job_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    units: Optional[int] = None,
    payer_entity_id: Optional[str] = None,
    pricing_source: str = "unknown",
    strict: bool = False,
    task_id: Optional[str] = None,
) -> bool:
    """Log a platform media call that has no known cost, and charge nothing.

    ``pricing_source`` says why: ``unknown`` for a failed gateway request with no
    reported cost, ``unpriced`` for a completed call whose model has no price.
    The row keeps the call's run, media job and model with ``cost_usd`` NULL and
    no credits, and no budget moves. The media job holds the generation id to
    reconcile against the gateway's billing later. Like ``record_media_usage``,
    an omitted ``operation_id`` is the bound billing context's run.
    """
    if operation_id is None:
        from packages.core.ai.runtime.billing import runtime_current_billing_operation_id

        operation_id = runtime_current_billing_operation_id(entity_id)
    task_id = _billing_task_id(entity_id, task_id)
    try:
        from packages.core.models.billing import CreditUsageLog
        payer = await _ledger_payer_or_uncharged(
            db, writer="record_media_call_unknown_cost", entity_id=entity_id, workspace_id=workspace_id,
            payer_entity_id=payer_entity_id, strict=strict,
        )
        if payer is None:
            return False
        db.add(CreditUsageLog(
            id=generate_ulid(),
            entity_id=payer,
            workspace_id=workspace_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            operation_id=operation_id,
            input_tokens=0, output_tokens=0,
            total_tokens=int(units or 0),
            input_credit=0, output_credit=0,
            direct_credit=0, total_credit=0,
            model=model,
            cost_usd=None,
            business_type=kind,
            duration_ms=0,
            media_job_id=media_job_id,
            pricing_source=pricing_source,
            task_id=task_id,
        ))
        await db.flush()
        return True
    except Exception:
        logger.warning("record_media_call_unknown_cost: log failed kind=%s", kind, exc_info=True)
        if strict:
            raise
        return False


async def record_media_usage(
    db: AsyncSession,
    entity_id: str,
    *,
    kind: str,
    model: str,
    cost_usd: Optional[float],
    units: Optional[int] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    source: str = "tool",
    byok: bool = False,
    strict: bool = False,
    payer_entity_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    media_job_id: Optional[str] = None,
    pricing_source: Optional[str] = None,
    byok_estimate_usd: Optional[float] = None,
    task_id: Optional[str] = None,
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

    ``operation_id`` names the run that made the call. When omitted, the
    bound billing context's run id is used if it belongs to ``entity_id``,
    so a tool call inside a Workflow run is tied to that run. A media job
    passes its owning run here and its own id as ``media_job_id``.

    ``prompt_tokens`` / ``completion_tokens`` are the provider's token counts
    when it reported them (or the estimate's), stored on the log row.
    ``pricing_source`` says where ``cost_usd`` came from: ``official_tokens``
    (the provider's output tokens at the model's token price), ``estimated``
    (the list price for the clip's duration), or ``unknown``.

    BYOK calls (the user's own key) charge nothing. They still get one
    ``token_usage_logs`` row with ``billing_mode='byok'`` and any
    provider-reported ``prompt_tokens`` / ``completion_tokens``, so every
    provider call leaves a record. ``byok_estimate_usd`` is the approximate
    cost of those tokens, kept on that row for reporting; it is never charged,
    since the user pays the provider directly. A BYOK caller that passes no
    estimate writes no cost.

    Caller MUST commit. Failure is logged but not raised by default. Durable
    reservation settlement can pass ``strict=True`` so a caller-owned rollback
    keeps the ledger, workspace budget state, and threshold alert atomic.

    The charge, counters and AI budget go to the payer (``payer_entity_id``,
    or the Workspace's current payer when omitted).
    """
    if operation_id is None:
        from packages.core.ai.runtime.billing import runtime_current_billing_operation_id

        operation_id = runtime_current_billing_operation_id(entity_id)
    task_id = _billing_task_id(entity_id, task_id)

    if byok:
        await _record_byok_media_usage(
            db,
            entity_id=entity_id,
            kind=kind,
            model=model,
            source=source,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            duration_ms=duration_ms,
            operation_id=operation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            strict=strict,
            media_job_id=media_job_id,
            pricing_source=pricing_source,
            estimate_usd=byok_estimate_usd,
            task_id=task_id,
        )
        return False

    if cost_usd is None or cost_usd <= 0:
        return False

    credits = media_cost_credits(float(cost_usd))

    # 1. credit_usage_logs row (analytics + audit)
    try:
        from packages.core.models.billing import CreditUsageLog
        payer = await _ledger_payer_or_uncharged(
            db, writer="record_media_usage", entity_id=entity_id, workspace_id=workspace_id,
            payer_entity_id=payer_entity_id, strict=strict,
        )
        if payer is None:
            return False
        row = CreditUsageLog(
            id=generate_ulid(),
            entity_id=payer,
            workspace_id=workspace_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            operation_id=operation_id,
            input_tokens=int(prompt_tokens or 0), output_tokens=int(completion_tokens or 0),
            total_tokens=int(units or 0),  # repurpose for unit count
            input_credit=0, output_credit=0,
            direct_credit=int(credits),
            total_credit=int(credits),
            model=model,
            cost_usd=float(cost_usd),
            business_type=kind,
            duration_ms=int(duration_ms or 0),
            media_job_id=media_job_id,
            pricing_source=pricing_source,
            task_id=task_id,
        )
        db.add(row)
        await db.flush()
    except Exception:
        logger.warning("record_media_usage: log failed", exc_info=True)
        if strict:
            raise
        return False

    # 2. Mirror credit total onto Entity.settings.used_credits
    try:
        from packages.core.services.entity_settings import (
            increment_entity_settings_counters,
        )
        await increment_entity_settings_counters(
            db, payer, {"used_credits": int(credits)},
        )
    except Exception:
        logger.warning("record_media_usage: used_credits update failed", exc_info=True)
        if strict:
            raise

    # 3. Workspace monthly budget uses the same credit charge as the ledger.
    if workspace_id:
        try:
            from packages.core.budget import accumulate_workspace_ai_cost
            from packages.core.services.credit_service import credits_to_usd

            async with db.begin_nested():
                await accumulate_workspace_ai_cost(
                    db,
                    workspace_id=workspace_id,
                    cost_usd=credits_to_usd(credits),
                )
        except Exception:
            logger.warning(
                "record_media_usage: workspace budget accumulation failed",
                exc_info=True,
            )
            if strict:
                raise

    # 4. Entity AI budget (ai_usage_usd)
    try:
        from packages.core.services.plan_enforcement import record_ai_cost
        await record_ai_cost(db, payer, float(cost_usd), model)
    except Exception:
        logger.warning("record_media_usage: budget update failed", exc_info=True)
        if strict:
            raise

    return True


