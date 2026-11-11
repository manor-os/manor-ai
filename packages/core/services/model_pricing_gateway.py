"""Model pricing gateway facade.

Business code should call this module for price lookup and accounting facades.
The underlying registry lives in ``model_pricing.py`` so pricing data remains
easy to audit and update separately from runtime workflows.
"""
from __future__ import annotations

from typing import Any

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
    audio_input_tokens: int = 0,
    audio_output_tokens: int = 0,
    cached_audio_input_tokens: int = 0,
    require_price: bool = False,
) -> float | None:
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
        audio_input_tokens=audio_input_tokens,
        audio_output_tokens=audio_output_tokens,
        cached_audio_input_tokens=cached_audio_input_tokens,
        require_price=require_price,
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
    audio_input_tokens: int = 0,
    audio_output_tokens: int = 0,
    cached_audio_input_tokens: int = 0,
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


def estimate_video_cost_usd(
    model: str,
    duration_seconds: float,
    resolution: str = "720p",
    *,
    with_audio: bool = False,
    has_video_input: bool = False,
) -> float:
    return model_pricing.estimate_video_cost_usd(
        model,
        duration_seconds,
        resolution,
        with_audio=with_audio,
        has_video_input=has_video_input,
    )


def video_is_priced(model: str | None) -> bool:
    return model_pricing.video_is_priced(model)


def video_output_tokens(model: str, duration_seconds: float, resolution: str = "720p") -> int:
    return model_pricing.video_output_tokens(model, duration_seconds, resolution)


def video_token_price_per_m(model: str | None, resolution: str = "720p", *, has_video_input: bool = False) -> float | None:
    return model_pricing.video_token_price_per_m(model, resolution, has_video_input=has_video_input)


def video_cost_from_tokens(
    model: str | None,
    output_tokens: float | None,
    resolution: str = "720p",
    *,
    has_video_input: bool = False,
) -> float | None:
    return model_pricing.video_cost_from_tokens(
        model, output_tokens, resolution, has_video_input=has_video_input,
    )


def estimate_image_cost_usd(model: str, size: str = "1024x1024") -> float:
    return model_pricing.estimate_image_cost_usd(model, size=size)


def image_call_cost_usd(model: str, usage: Any, size: str = "1024x1024") -> tuple[float | None, str]:
    return model_pricing.image_call_cost_usd(model, usage, size=size)


def estimate_audio_cost_usd(model: str, *, purpose: str = "", characters: int | None = None) -> float:
    return model_pricing.estimate_audio_cost_usd(model, purpose=purpose, characters=characters)


def audio_call_cost_usd(
    model: str,
    *,
    characters: int | None = None,
    seconds: float | None = None,
    input_tokens: int | None = None,
    audio_output_tokens: int | None = None,
) -> tuple[float | None, str]:
    return model_pricing.audio_call_cost_usd(
        model,
        characters=characters,
        seconds=seconds,
        input_tokens=input_tokens,
        audio_output_tokens=audio_output_tokens,
    )


def embedding_cost_usd(model: str, total_tokens: int) -> float:
    return model_pricing.embedding_cost_usd(model, total_tokens)
