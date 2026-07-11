"""Model pricing gateway facade.

Business code should call this module for price lookup and accounting facades.
The underlying registry lives in ``model_pricing.py`` so pricing data remains
easy to audit and update separately from runtime workflows.
"""
from __future__ import annotations

from packages.core.constants.plans import is_cloud
from packages.core.services import model_pricing


def pricing_catalog() -> list[dict]:
    return model_pricing.model_pricing_catalog()


def estimate_token_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
    *,
    pricing_source: str | None = None,
    provider: str | None = None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_multiplier: float = 0.1,
    cache_write_multiplier: float = 1.25,
) -> float:
    return model_pricing.estimate_token_cost_usd(
        input_tokens,
        output_tokens,
        model,
        pricing_source=pricing_source,
        provider=provider,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_multiplier=cache_read_multiplier,
        cache_write_multiplier=cache_write_multiplier,
    )


def token_unit_prices(
    model: str | None,
    *,
    pricing_source: str | None = None,
    provider: str | None = None,
) -> tuple[float, float]:
    return model_pricing.token_unit_prices(
        model,
        pricing_source=pricing_source,
        provider=provider,
    )


def tokens_to_credits(
    input_tokens: int,
    output_tokens: int,
    model: str | None,
    *,
    credits_per_usd: int,
    ai_margin: float,
    pricing_source: str | None = None,
    provider: str | None = None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_multiplier: float = 0.1,
    cache_write_multiplier: float = 1.25,
) -> int:
    if not is_cloud():
        return 0
    return 0


def cost_to_credits(cost_usd: float, *, credits_per_usd: int, ai_margin: float) -> int:
    if not is_cloud():
        return 0
    return 0


def model_cost_multiplier(model: str | None) -> float:
    return model_pricing.model_cost_multiplier(model)


def estimate_video_cost_usd(model: str, duration_seconds: float, resolution: str = "720p") -> float:
    return model_pricing.estimate_video_cost_usd(model, duration_seconds, resolution)


def estimate_image_cost_usd(model: str, size: str = "1024x1024") -> float:
    return model_pricing.estimate_image_cost_usd(model, size=size)


def estimate_audio_cost_usd(model: str, *, purpose: str = "") -> float:
    return model_pricing.estimate_audio_cost_usd(model, purpose=purpose)


def embedding_cost_usd(model: str, total_tokens: int) -> float:
    return model_pricing.embedding_cost_usd(model, total_tokens)
