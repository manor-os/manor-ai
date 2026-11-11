"""Price any model from the AI Gateway's published catalog.

The curated registries in ``model_pricing`` hold corrections and detail the
catalog lacks; they always win. Every other model is priced here, in whatever
unit the gateway bills it (tokens, seconds, characters, images), so a model
the gateway lists can be offered and billed without a code change, and a model
nobody prices is never offered on the platform key (``is_priceable``).

Catalog entries come from ``vercel_pricing_sync`` (the synced cache, or the
committed snapshot before the first sync) and keep the gateway's ``pricing``
object verbatim. Its prices are USD per unit as strings.
"""
from __future__ import annotations

from typing import Any

# The kinds a model can be offered and billed for.
KINDS = ("text", "image", "video", "speech", "music", "transcription", "embedding")


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def catalog_entry(model: str | None) -> dict[str, Any] | None:
    """The catalog entry for a model, matched on its id, gateway wire id or provider alias."""
    if not model:
        return None
    from packages.core.services import model_pricing

    model_pricing._load_vercel_cache_if_needed()
    for candidate in model_pricing._vercel_cache_candidates(str(model).strip()):
        entry = model_pricing._VERCEL_CACHE_MODELS.get(candidate)
        if isinstance(entry, dict):
            return entry
    return None


def catalog_pricing(model: str | None) -> dict[str, Any]:
    entry = catalog_entry(model)
    pricing = (entry or {}).get("pricing")
    return pricing if isinstance(pricing, dict) else {}


# ── Video ─────────────────────────────────────────────────────────────────


def _video_mode(model: str) -> str | None:
    lowered = str(model or "").lower()
    if lowered.endswith("-pro") or "-pro-" in lowered:
        return "pro"
    if lowered.endswith("-std") or "-std-" in lowered:
        return "std"
    return None


def _best_row(rows: list[dict[str, Any]], wanted: dict[str, Any]) -> dict[str, Any] | None:
    """The row matching the most wanted attributes; a row contradicting one is skipped."""
    best, best_score = None, -1
    for row in rows:
        if not isinstance(row, dict):
            continue
        score, ok = 0, True
        for key, value in wanted.items():
            if value is None or key not in row:
                continue
            if row[key] == value or str(row[key]).lower() == str(value).lower():
                score += 1
            else:
                ok = False
                break
        if ok and score > best_score:
            best, best_score = row, score
    return best


def video_token_price_per_m(model: str, resolution: str, *, has_video_input: bool = False) -> float | None:
    """USD per 1M output tokens from the catalog's ``video_token_pricing``."""
    tiers = (catalog_pricing(model).get("video_token_pricing") or {}).get("tiers") or []
    row = _best_row(tiers, {"resolution": resolution}) or _best_row(tiers, {"resolution": "720p"})
    if not row:
        return None
    side = row.get("with_video_input" if has_video_input else "no_video_input") or row.get("no_video_input") or {}
    return _num(side.get("cost_per_million_tokens"))


def video_cost_per_second(
    model: str, resolution: str, *, with_audio: bool = False,
) -> float | None:
    """USD per generated second from the catalog's ``video_duration_pricing``."""
    rows = catalog_pricing(model).get("video_duration_pricing") or []
    wanted = {"resolution": resolution, "mode": _video_mode(model), "audio": bool(with_audio)}
    row = _best_row(rows, wanted) or _best_row(rows, {**wanted, "resolution": None})
    return _num((row or {}).get("cost_per_second"))


def has_video_price(model: str) -> bool:
    pricing = catalog_pricing(model)
    return bool(pricing.get("video_duration_pricing") or (pricing.get("video_token_pricing") or {}).get("tiers"))


# ── Image ─────────────────────────────────────────────────────────────────


def _size_longest_side(size: str) -> int | None:
    text = str(size or "").strip().lower()
    if "x" in text:
        try:
            return max(int(part) for part in text.split("x", 1))
        except ValueError:
            return None
    if text.endswith("k"):
        try:
            return int(float(text[:-1]) * 1024)
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def image_cost_per_image(model: str, size: str = "1024x1024") -> float | None:
    """USD for one image from the catalog's per-size or per-image price."""
    pricing = catalog_pricing(model)
    tiers = [row for row in pricing.get("image_dimension_quality_pricing") or [] if isinstance(row, dict)]
    wanted = _size_longest_side(size)
    if tiers and wanted:
        sized = sorted(
            ((side, _num(row.get("cost"))) for row in tiers
             if (side := _size_longest_side(str(row.get("size") or ""))) and _num(row.get("cost")) is not None),
        )
        for side, cost in sized:
            if wanted <= side:
                return cost
        if sized:
            return sized[-1][1]
    default = next((_num(row.get("cost")) for row in tiers if str(row.get("size")) == "default"), None)
    return default if default is not None else _num(pricing.get("image"))


def image_token_prices(model: str) -> tuple[float, float, float | None] | None:
    """(input, output, cached input) USD per 1M tokens for a token-priced image model."""
    pricing = catalog_pricing(model)
    input_cost, output_cost = _num(pricing.get("input")), _num(pricing.get("output"))
    if input_cost is None or output_cost is None:
        return None
    cached = _num(pricing.get("input_cache_read"))
    return input_cost * 1e6, output_cost * 1e6, cached * 1e6 if cached is not None else None


# ── Speech, music, transcription, embedding ───────────────────────────────


def speech_cost(
    model: str,
    *,
    characters: int | None = None,
    seconds: float | None = None,
    input_tokens: int | None = None,
    audio_output_tokens: int | None = None,
) -> tuple[float | None, str]:
    """A speech or music call's USD cost on the catalog's unit, and its basis."""
    pricing = catalog_pricing(model)
    per_char = _num(pricing.get("speech_input_character_cost"))
    if per_char is not None:
        if characters is None:
            return None, "unpriced"
        return per_char * max(0, int(characters)), "catalog_characters"
    per_second = _num(pricing.get("speech_output_cost_per_second"))
    if per_second is not None:
        if seconds is None:
            return None, "unpriced"
        return per_second * max(0.0, float(seconds)), "catalog_seconds"
    per_audio_token = _num(pricing.get("audio_output_token_cost"))
    if per_audio_token is not None and audio_output_tokens is not None:
        in_rate = _num(pricing.get("input")) or 0.0
        in_tokens = max(0, int(input_tokens if input_tokens is not None else (characters or 0) / 4))
        return in_tokens * in_rate + max(0, int(audio_output_tokens)) * per_audio_token, "catalog_tokens"
    return None, "unpriced"


def transcription_cost_per_second(model: str) -> float | None:
    return _num(catalog_pricing(model).get("transcription_duration_cost_per_second"))


def embedding_price_per_m(model: str) -> float | None:
    entry = catalog_entry(model) or {}
    if entry.get("type") not in ("embedding", ""):
        return None
    input_cost = _num(catalog_pricing(model).get("input"))
    return input_cost * 1e6 if input_cost is not None else None


# ── Can this model be billed? ─────────────────────────────────────────────


def is_priceable(model: str | None, kind: str) -> bool:
    """Whether a platform call to ``model`` for ``kind`` has a price to bill on.

    A model without one is not offered on the platform key: picking it, or
    asking for it at call time, is refused. BYOK calls are not gated.
    """
    from packages.core.services import model_pricing

    model_id = str(model or "").strip()
    if not model_id:
        return False
    pricing = catalog_pricing(model_id)
    if kind == "text":
        return model_pricing.resolve_token_unit_prices(model_id) is not None
    if kind == "video":
        return model_id in model_pricing.VIDEO_COST_PER_SECOND or has_video_price(model_id)
    if kind == "image":
        return (
            model_id in model_pricing.IMAGE_TOKEN_PRICES
            or model_id in model_pricing.OFFICIAL_IMAGE_PRICES
            or image_cost_per_image(model_id) is not None
            or image_token_prices(model_id) is not None
        )
    if kind in ("speech", "music"):
        return (
            model_id in model_pricing.PER_CHARACTER_AUDIO_PRICES
            or model_id in model_pricing.SPEECH_TOKEN_PRICES
            or model_id in model_pricing.OFFICIAL_AUDIO_PRICES
            or any(key in pricing for key in (
                "speech_input_character_cost", "speech_output_cost_per_second", "audio_output_token_cost",
            ))
        )
    if kind == "transcription":
        return (
            model_pricing.transcription_usd_per_minute(model_id) is not None
            # Chat-audio models transcribe on their token price.
            or model_pricing.resolve_token_unit_prices(model_id) is not None
            or transcription_cost_per_second(model_id) is not None
            or _num(pricing.get("audio_input_token_cost")) is not None
        )
    if kind == "embedding":
        return model_pricing._lookup_token_price(model_id) is not None or embedding_price_per_m(model_id) is not None
    return False
