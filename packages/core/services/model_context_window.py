"""Context window of the model a loop is calling.

A model's raw window is the smallest value known for it: the catalog snapshot
in ``MODEL_CONTEXT_WINDOWS`` and the live windows the Vercel/OpenRouter pricing
syncs cache (refreshed by Celery beat; read through mtime-keyed caches).

The window the loop budgets against is capped further. Every round re-sends
the whole prompt, so a 1M window would mean ~750K tokens per round before the
first compaction:

* ``MANOR_CONTEXT_WINDOW_CAP_TOKENS`` (default 200K) bounds every model;
* a provider long-context price tier caps it at the tier threshold, unless the
  threshold is too small to hold the loop's fixed prompt (qwen3.7-flash bills
  above 32K) — then the request pays the tier rather than failing.
"""
from __future__ import annotations

import logging
import os

from packages.core.constants.models import MODEL_CONTEXT_WINDOWS

logger = logging.getLogger(__name__)

# Unknown models (custom BYOK ids, test doubles) keep the historical budget.
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_CONTEXT_WINDOW_CAP_TOKENS = 200_000
# System prompt + eager tool schemas + output reserve already take ~35K.
MIN_PRICE_TIER_CAP_TOKENS = 64_000


def context_window_cap_tokens() -> int:
    raw = os.getenv("MANOR_CONTEXT_WINDOW_CAP_TOKENS", "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_CONTEXT_WINDOW_CAP_TOKENS
    except ValueError:
        logger.warning("Invalid MANOR_CONTEXT_WINDOW_CAP_TOKENS=%r; using %d", raw, DEFAULT_CONTEXT_WINDOW_CAP_TOKENS)
        return DEFAULT_CONTEXT_WINDOW_CAP_TOKENS
    return value if value > 0 else DEFAULT_CONTEXT_WINDOW_CAP_TOKENS


def model_context_window_tokens(model: str | None) -> int | None:
    """The model's own input window, or None when no source knows it."""
    if not model:
        return None
    from packages.core.services.model_pricing import cached_context_windows

    model_id = str(model).strip()
    known = [*cached_context_windows(model_id)]
    if model_id in MODEL_CONTEXT_WINDOWS:
        known.append(MODEL_CONTEXT_WINDOWS[model_id])
    return min(known) if known else None


def effective_context_window_tokens(model: str | None) -> int:
    """The window the agent loop budgets, compacts and validates against."""
    from packages.core.services.model_pricing import long_context_threshold

    window = min(
        model_context_window_tokens(model) or DEFAULT_CONTEXT_WINDOW_TOKENS,
        context_window_cap_tokens(),
    )
    tier = long_context_threshold(model)
    if tier and tier >= MIN_PRICE_TIER_CAP_TOKENS:
        window = min(window, tier)
    return window
