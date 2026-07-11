"""Official model pricing registry and credit-cost helpers.

All prices are normalized to USD. Token prices are per 1M tokens.
OpenRouter can still override prices through its runtime cache when the
request is routed through OpenRouter; official native routes use this table.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PRICING_CACHE_MTIME = 0.0
_PRICING_CACHE_MODELS: dict[str, dict] = {}


@dataclass(frozen=True)
class TokenModelPrice:
    provider: str
    input_per_m: float
    output_per_m: float
    source: str
    note: str = ""
    cache_read_multiplier: float | None = None
    cache_write_multiplier: float | None = None


@dataclass(frozen=True)
class FlatModelPrice:
    provider: str
    unit: str
    usd: float
    source: str
    note: str = ""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


_CNY_USD = _env_float("MODEL_PRICING_CNY_USD", 0.138)


OFFICIAL_TOKEN_PRICES: dict[str, TokenModelPrice] = {
    # OpenAI
    "openai/gpt-4o-mini": TokenModelPrice("openai", 0.15, 0.60, "official"),
    "openai/gpt-4o": TokenModelPrice("openai", 2.50, 10.00, "official"),
    "openai/gpt-4.1": TokenModelPrice("openai", 2.00, 8.00, "official"),
    "openai/gpt-4.1-mini": TokenModelPrice("openai", 0.40, 1.60, "official"),
    "openai/gpt-5.5": TokenModelPrice("openai", 5.00, 30.00, "official"),
    "openai/gpt-5.5-pro": TokenModelPrice("openai", 30.00, 180.00, "official"),
    "openai/gpt-audio-mini": TokenModelPrice("openai", 0.60, 2.40, "official"),
    "openai/gpt-audio": TokenModelPrice("openai", 2.50, 10.00, "official"),
    "openai/gpt-4o-audio-preview": TokenModelPrice("openai", 2.50, 10.00, "official"),
    "openai/gpt-5-image-mini": TokenModelPrice("openai", 2.50, 2.00, "official"),
    "openai/gpt-5.4-image-2": TokenModelPrice("openai", 8.00, 15.00, "official"),
    "openai/gpt-image-2": TokenModelPrice("openai", 8.00, 15.00, "official"),
    "text-embedding-3-small": TokenModelPrice("openai", 0.02, 0.0, "official"),
    "text-embedding-3-large": TokenModelPrice("openai", 0.13, 0.0, "official"),
    "text-embedding-ada-002": TokenModelPrice("openai", 0.10, 0.0, "official"),

    # Anthropic
    "anthropic/claude-fable-5": TokenModelPrice("anthropic", 10.00, 50.00, "official"),
    "anthropic/claude-haiku-4.5": TokenModelPrice("anthropic", 1.00, 5.00, "official"),
    "anthropic/claude-sonnet-4.6": TokenModelPrice("anthropic", 3.00, 15.00, "official"),
    "anthropic/claude-opus-4.6": TokenModelPrice("anthropic", 5.00, 25.00, "official"),
    "anthropic/claude-opus-4.7": TokenModelPrice("anthropic", 5.00, 25.00, "official"),

    # Google Gemini
    "google/gemini-2.5-flash-lite": TokenModelPrice("google", 0.10, 0.40, "official"),
    "google/gemini-2.5-flash": TokenModelPrice("google", 0.30, 2.50, "official"),
    "google/gemini-2.5-pro": TokenModelPrice("google", 1.25, 10.00, "official"),
    "google/gemini-3.1-flash-image": TokenModelPrice("google", 0.50, 3.00, "official"),
    "google/gemini-3.1-flash-image-preview": TokenModelPrice("google", 0.50, 3.00, "official"),

    # DeepSeek
    "deepseek/deepseek-v4-flash": TokenModelPrice("deepseek", 0.14, 0.28, "official"),
    "deepseek/deepseek-v4-pro": TokenModelPrice("deepseek", 0.435, 0.87, "official"),
    "deepseek/deepseek-chat": TokenModelPrice("deepseek", 0.32, 0.89, "official"),
    "deepseek/deepseek-v3.2": TokenModelPrice("deepseek", 0.25, 0.70, "official"),

    # Qwen / DashScope. Official CNY rates are converted with MODEL_PRICING_CNY_USD.
    "qwen/qwen3.6-plus": TokenModelPrice(
        "qwen",
        round(2.0 * _CNY_USD, 6),
        round(12.0 * _CNY_USD, 6),
        "official",
        note="DashScope mainland China rate converted from CNY.",
    ),

    # Moonshot / Kimi. Keep explicit until Moonshot exposes a stable machine-readable price feed.
    "moonshotai/kimi-k2.6": TokenModelPrice("moonshotai", 0.95, 4.00, "official"),

    # Local embeddings
    "mxbai-embed-large": TokenModelPrice("ollama", 0.0, 0.0, "local"),
    "nomic-embed-text": TokenModelPrice("ollama", 0.0, 0.0, "local"),
}


OFFICIAL_IMAGE_PRICES: dict[str, FlatModelPrice] = {
    "openai/gpt-5-image-mini": FlatModelPrice("openai", "image", 0.04, "official"),
    "gpt-5-image-mini": FlatModelPrice("openai", "image", 0.04, "official"),
    "openai/gpt-image-1": FlatModelPrice("openai", "image", 0.04, "official"),
    "gpt-image-1": FlatModelPrice("openai", "image", 0.04, "official"),
    "openai/gpt-5.4-image-2": FlatModelPrice("openai", "image", 0.08, "official"),
    "openai/gpt-image-2": FlatModelPrice("openai", "image", 0.08, "official"),
    "google/gemini-3.1-flash-image-preview": FlatModelPrice("google", "image", 0.04, "official"),
}


OFFICIAL_AUDIO_PRICES: dict[str, FlatModelPrice] = {
    "google/gemini-3.1-flash-tts-preview": FlatModelPrice("google", "audio_asset", 0.01, "official"),
    "zyphra/zonos-v0.1-hybrid": FlatModelPrice("zyphra", "audio_asset", 0.01, "official"),
    "zyphra/zonos-v0.1-transformer": FlatModelPrice("zyphra", "audio_asset", 0.01, "official"),
    "sesame/csm-1b": FlatModelPrice("openrouter", "audio_asset", 0.01, "openrouter_fallback"),
    "google/lyria-3-clip-preview": FlatModelPrice("google", "audio_asset", 0.04, "official"),
    "google/lyria-3-pro-preview": FlatModelPrice("google", "audio_asset", 0.08, "official"),
    "openai/gpt-audio-mini": FlatModelPrice("openai", "audio_asset", 0.02, "official"),
    "openai/gpt-audio": FlatModelPrice("openai", "audio_asset", 0.04, "official"),
}


VIDEO_COST_PER_SECOND: dict[str, dict[str, float]] = {
    "bytedance/seedance-2.0": {
        "480p": 0.067,
        "720p": 0.134,
        "1080p": 0.268,
        "1440p": 0.335,
    },
    "bytedance/seedance-2.0-fast": {
        "480p": 0.054,
        "720p": 0.107,
        "1080p": 0.214,
        "1440p": 0.268,
    },
    "kwaivgi/kling-v3.0": {
        "480p": 0.126,
        "720p": 0.126,
        "1080p": 0.126,
        "1440p": 0.126,
    },
    "kwaivgi/kling-v3.0-std": {
        "480p": 0.126,
        "720p": 0.126,
        "1080p": 0.126,
        "1440p": 0.126,
    },
    "kwaivgi/kling-v3.0-pro": {
        "480p": 0.168,
        "720p": 0.168,
        "1080p": 0.168,
        "1440p": 0.168,
    },
}

DEFAULT_INPUT_COST_PER_M = 1.50
DEFAULT_OUTPUT_COST_PER_M = 5.00
DEFAULT_MODEL_MULTIPLIER = 5.0
BASELINE_INPUT_COST_PER_M = 0.32


def openrouter_pricing_cache_path() -> str:
    return (os.getenv("OPENROUTER_PRICING_CACHE_PATH") or "/tmp/manor_openrouter_pricing_cache.json").strip()


def _load_openrouter_cache_if_needed() -> None:
    global _PRICING_CACHE_MTIME, _PRICING_CACHE_MODELS
    path = Path(openrouter_pricing_cache_path())
    try:
        st = path.stat()
    except Exception:
        return
    if st.st_mtime <= _PRICING_CACHE_MTIME:
        return
    try:
        data = json.loads(path.read_text())
        models = data.get("models") or {}
        if isinstance(models, dict):
            _PRICING_CACHE_MODELS = models
            _PRICING_CACHE_MTIME = st.st_mtime
    except Exception:
        logger.debug("model pricing: failed to load OpenRouter pricing cache", exc_info=True)


def model_pricing_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model, price in OFFICIAL_TOKEN_PRICES.items():
        rows.append({
            "model": model,
            "provider": price.provider,
            "unit": "1m_tokens",
            "input_per_m": price.input_per_m,
            "output_per_m": price.output_per_m,
            "source": price.source,
            "note": price.note,
        })
    for model, price in OFFICIAL_IMAGE_PRICES.items():
        rows.append({
            "model": model,
            "provider": price.provider,
            "unit": price.unit,
            "usd": price.usd,
            "source": price.source,
            "note": price.note,
        })
    for model, price in OFFICIAL_AUDIO_PRICES.items():
        rows.append({
            "model": model,
            "provider": price.provider,
            "unit": price.unit,
            "usd": price.usd,
            "source": price.source,
            "note": price.note,
        })
    for model, by_resolution in VIDEO_COST_PER_SECOND.items():
        rows.append({
            "model": model,
            "provider": model.split("/", 1)[0] if "/" in model else "",
            "unit": "video_second",
            "usd_by_resolution": dict(by_resolution),
            "source": "official",
        })
    return rows


def _lookup_token_price(model: str | None) -> TokenModelPrice | None:
    if not model:
        return None
    model_id = str(model).strip()
    if model_id in OFFICIAL_TOKEN_PRICES:
        return OFFICIAL_TOKEN_PRICES[model_id]
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    if bare in OFFICIAL_TOKEN_PRICES:
        return OFFICIAL_TOKEN_PRICES[bare]
    lowered = model_id.lower()
    for key, price in OFFICIAL_TOKEN_PRICES.items():
        if lowered.startswith(key.lower().rsplit("-", 1)[0]):
            return price
    return None


def _openrouter_cached_price(model: str | None) -> tuple[float | None, float | None]:
    if not model:
        return None, None
    _load_openrouter_cache_if_needed()
    cached = _PRICING_CACHE_MODELS.get(str(model))
    if not isinstance(cached, dict):
        return None, None
    input_per_m = cached.get("input_per_m")
    output_per_m = cached.get("output_per_m")
    return (
        float(input_per_m) if input_per_m is not None else None,
        float(output_per_m) if output_per_m is not None else None,
    )


def token_unit_prices(
    model: str | None,
    *,
    pricing_source: str | None = None,
    provider: str | None = None,
) -> tuple[float, float]:
    source = str(pricing_source or "").lower()
    route_provider = str(provider or "").lower()
    use_openrouter = source == "openrouter" or route_provider == "openrouter"

    if use_openrouter:
        cached_input, cached_output = _openrouter_cached_price(model)
        if cached_input is not None or cached_output is not None:
            return (
                cached_input if cached_input is not None else DEFAULT_INPUT_COST_PER_M,
                cached_output if cached_output is not None else DEFAULT_OUTPUT_COST_PER_M,
            )

    official = _lookup_token_price(model)
    if official:
        return official.input_per_m, official.output_per_m

    if not use_openrouter:
        cached_input, cached_output = _openrouter_cached_price(model)
        if cached_input is not None or cached_output is not None:
            return (
                cached_input if cached_input is not None else DEFAULT_INPUT_COST_PER_M,
                cached_output if cached_output is not None else DEFAULT_OUTPUT_COST_PER_M,
            )

    return DEFAULT_INPUT_COST_PER_M, DEFAULT_OUTPUT_COST_PER_M


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
    input_per_m, output_per_m = token_unit_prices(
        model,
        pricing_source=pricing_source,
        provider=provider,
    )
    base_input = max(0, int(input_tokens or 0) - int(cache_read_tokens or 0) - int(cache_creation_tokens or 0))
    return (
        base_input * input_per_m
        + int(cache_read_tokens or 0) * input_per_m * cache_read_multiplier
        + int(cache_creation_tokens or 0) * input_per_m * cache_write_multiplier
        + int(output_tokens or 0) * output_per_m
    ) / 1_000_000


def model_cost_multiplier(model: str | None) -> float:
    price = _lookup_token_price(model)
    if not price:
        return DEFAULT_MODEL_MULTIPLIER
    if price.input_per_m <= 0:
        return 0.0
    return max(0.0, price.input_per_m / BASELINE_INPUT_COST_PER_M)


def estimate_video_cost_usd(model: str, duration_seconds: float, resolution: str = "720p") -> float:
    pricing = VIDEO_COST_PER_SECOND.get(model)
    if not pricing:
        return 0.20 * max(0.0, float(duration_seconds or 0))
    rate = pricing.get(resolution, pricing.get("720p", 0.134))
    return float(rate) * max(0.0, float(duration_seconds or 0))


def estimate_image_cost_usd(model: str, size: str = "1024x1024") -> float:
    del size
    model_id = str(model or "").strip()
    price = OFFICIAL_IMAGE_PRICES.get(model_id)
    if not price and "/" in model_id:
        price = OFFICIAL_IMAGE_PRICES.get(model_id.split("/", 1)[1])
    return float(price.usd) if price else 0.04


def estimate_audio_cost_usd(model: str, *, purpose: str = "") -> float:
    model_id = str(model or "").strip()
    price = OFFICIAL_AUDIO_PRICES.get(model_id)
    if price:
        return float(price.usd)
    lowered = model_id.lower()
    if purpose in {"music", "score", "bgm"}:
        return 0.04
    if "gpt-audio" in lowered:
        return 0.02
    if "tts" in lowered or "zonos" in lowered or "sesame" in lowered:
        return 0.01
    return 0.01


def embedding_cost_usd(model: str, total_tokens: int) -> float:
    price = _lookup_token_price(model)
    rate = price.input_per_m if price else 0.13
    return max(0, int(total_tokens or 0)) / 1_000_000 * rate
