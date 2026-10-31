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

A tenant BYOK credential may declare its model's limits (``llm_model_limits``
in the resolved LLM metadata, bound to one model id). A model no catalog knows
is otherwise budgeted at the 128K fallback with a 16K output ceiling. The
declared window replaces the catalog/cap chain because the tenant's own key
pays for the resend volume the cap exists to bound; the declared output
ceiling is clamped so a request can still compact into the window.
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

# Resolved LLM metadata key carrying a BYOK credential's declared limits:
# {"model": str, "context_window_tokens": int?, "max_output_tokens": int?}.
MODEL_LIMITS_METADATA_KEY = "llm_model_limits"
MODEL_LIMIT_FIELDS = ("context_window_tokens", "max_output_tokens")
# A window below this cannot hold the loop's system prompt and tool schemas.
MIN_DECLARED_CONTEXT_WINDOW_TOKENS = 16_384
MAX_DECLARED_CONTEXT_WINDOW_TOKENS = 10_000_000
MIN_DECLARED_MAX_OUTPUT_TOKENS = 1_024
MAX_DECLARED_MAX_OUTPUT_TOKENS = 1_000_000
# Compaction triggers at 75% and targets 50% of the window minus the output
# reserve; a reserve above a quarter of the window leaves no room to compact to.
MAX_OUTPUT_WINDOW_SHARE = 4


def normalize_model_limits(
    context_window_tokens: object = None,
    max_output_tokens: object = None,
) -> dict[str, int]:
    """Validate declared limits; ``{}`` when neither is set. Raises ValueError."""

    bounds = {
        "context_window_tokens": (MIN_DECLARED_CONTEXT_WINDOW_TOKENS, MAX_DECLARED_CONTEXT_WINDOW_TOKENS),
        "max_output_tokens": (MIN_DECLARED_MAX_OUTPUT_TOKENS, MAX_DECLARED_MAX_OUTPUT_TOKENS),
    }
    limits: dict[str, int] = {}
    for field, raw in (("context_window_tokens", context_window_tokens), ("max_output_tokens", max_output_tokens)):
        if raw is None or raw == "":
            continue
        if isinstance(raw, bool):
            raise ValueError(f"{field} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
        if isinstance(raw, float) and raw != value:
            raise ValueError(f"{field} must be an integer")
        low, high = bounds[field]
        if not low <= value <= high:
            raise ValueError(f"{field} must be between {low} and {high}")
        limits[field] = value
    window = limits.get("context_window_tokens")
    if window and limits.get("max_output_tokens", 0) >= window:
        raise ValueError("max_output_tokens must be smaller than context_window_tokens")
    return limits


def declared_model_limits(metadata: dict | None, model: str | None = None) -> dict[str, int]:
    """Limits a BYOK credential declared for ``model`` (or the routed model)."""

    raw = (metadata or {}).get(MODEL_LIMITS_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return {}
    bound = str(raw.get("model") or "").strip()
    called = str(model or (metadata or {}).get("_resolved_model") or "").strip()
    # A credential's limits describe its bound model only; another route
    # (fallback, per-round override) keeps its own catalog limits.
    if not bound or (called and called != bound):
        return {}
    try:
        return normalize_model_limits(raw.get("context_window_tokens"), raw.get("max_output_tokens"))
    except ValueError:
        logger.warning("Ignoring invalid declared model limits for %s: %r", bound, raw)
        return {}


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
    from packages.core.services.model_pricing import cached_context_windows, catalog_model_ids

    model_id = str(model).strip()
    known = [*cached_context_windows(model_id)]
    known.extend(MODEL_CONTEXT_WINDOWS[key] for key in catalog_model_ids(model_id) if key in MODEL_CONTEXT_WINDOWS)
    return min(known) if known else None


def effective_context_window_tokens(model: str | None, metadata: dict | None = None) -> int:
    """The window the agent loop budgets, compacts and validates against."""
    from packages.core.services.model_pricing import long_context_threshold

    declared = declared_model_limits(metadata, model).get("context_window_tokens")
    if declared:
        return declared
    window = min(
        model_context_window_tokens(model) or DEFAULT_CONTEXT_WINDOW_TOKENS,
        context_window_cap_tokens(),
    )
    tier = long_context_threshold(model)
    if tier and tier >= MIN_PRICE_TIER_CAP_TOKENS:
        window = min(window, tier)
    return window


def effective_output_tokens(
    default: int,
    *,
    model: str | None,
    metadata: dict | None,
    window: int | None = None,
) -> int:
    """Per-call output ceiling: a declared BYOK limit, else ``default``.

    A declared ceiling is both the ``max_tokens`` sent and the reserve the
    loop compacts against, so it is clamped to a quarter of the window.
    """

    declared = declared_model_limits(metadata, model).get("max_output_tokens")
    if not declared:
        return default
    if window is None:
        window = effective_context_window_tokens(model, metadata)
    return max(1, min(declared, window // MAX_OUTPUT_WINDOW_SHARE))
