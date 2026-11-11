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

_VERCEL_CACHE_MTIME = 0.0
_VERCEL_CACHE_MODELS: dict[str, dict] = {}

# Manor canonical provider prefix → Vercel AI Gateway catalog prefix.
_VERCEL_PROVIDER_ALIASES = {
    "qwen": "alibaba",
    "kwaivgi": "klingai",
    "xai": "spacexai",
}


@dataclass(frozen=True)
class TokenModelPrice:
    provider: str
    input_per_m: float
    output_per_m: float
    source: str
    note: str = ""
    cache_read_multiplier: float | None = None
    cache_write_multiplier: float | None = None
    # Audio-capable chat models bill audio tokens at their own (higher) rate.
    audio_input_per_m: float | None = None
    audio_output_per_m: float | None = None
    cached_audio_input_per_m: float | None = None
    # Long-context tier: when prompt tokens exceed the threshold, providers
    # bill the whole request at the long-context rates.
    long_context_threshold: int | None = None
    long_input_per_m: float | None = None
    long_output_per_m: float | None = None


@dataclass(frozen=True)
class FlatModelPrice:
    provider: str
    unit: str
    usd: float
    source: str
    note: str = ""


OFFICIAL_TOKEN_PRICES: dict[str, TokenModelPrice] = {
    # OpenAI
    "openai/gpt-4": TokenModelPrice("openai", 30.00, 60.00, "official"),
    "openai/gpt-4o-mini": TokenModelPrice("openai", 0.15, 0.60, "official"),
    "openai/gpt-4o": TokenModelPrice("openai", 2.50, 10.00, "official"),
    "openai/gpt-4.1": TokenModelPrice("openai", 2.00, 8.00, "official"),
    "openai/gpt-4.1-mini": TokenModelPrice("openai", 0.40, 1.60, "official"),
    "openai/gpt-6-astra": TokenModelPrice(
        "openai",
        10.00,
        50.00,
        "official",
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
        long_context_threshold=272_000,
        long_input_per_m=20.00,
        long_output_per_m=75.00,
    ),
    "openai/gpt-5.6-sol": TokenModelPrice(
        "openai",
        5.00,
        30.00,
        "official",
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
        long_context_threshold=272_000,
        long_input_per_m=10.00,
        long_output_per_m=60.00,
    ),
    "openai/gpt-5.6-terra": TokenModelPrice(
        "openai",
        2.00,
        12.00,
        "official",
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
        long_context_threshold=272_000,
        long_input_per_m=4.00,
        long_output_per_m=24.00,
    ),
    "openai/gpt-5.6-luna": TokenModelPrice(
        "openai",
        0.20,
        1.20,
        "official",
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
        long_context_threshold=272_000,
        long_input_per_m=0.40,
        long_output_per_m=2.40,
    ),
    "openai/gpt-5.5": TokenModelPrice(
        "openai", 5.00, 30.00, "official",
        long_context_threshold=272_000,
        long_input_per_m=10.00, long_output_per_m=60.00,
    ),
    "openai/gpt-5.5-pro": TokenModelPrice(
        "openai", 30.00, 180.00, "official",
        long_context_threshold=272_000,
        long_input_per_m=60.00, long_output_per_m=240.00,
    ),
    "openai/gpt-audio-mini": TokenModelPrice(
        "openai", 0.60, 2.40, "official",
        audio_input_per_m=10.00, audio_output_per_m=20.00,
    ),
    "openai/gpt-audio": TokenModelPrice(
        "openai", 2.50, 10.00, "official",
        audio_input_per_m=32.00, audio_output_per_m=64.00,
    ),
    "openai/gpt-4o-audio-preview": TokenModelPrice(
        "openai", 2.50, 10.00, "official",
        audio_input_per_m=40.00, audio_output_per_m=80.00,
    ),
    "openai/gpt-realtime-mini": TokenModelPrice(
        "openai",
        0.60,
        2.40,
        "official",
        cache_read_multiplier=0.10,
        audio_input_per_m=10.00,
        audio_output_per_m=20.00,
        cached_audio_input_per_m=0.30,
        note="OpenAI Realtime Mini text, cached-input, and audio token rates.",
    ),
    "openai/gpt-realtime": TokenModelPrice(
        "openai",
        4.00,
        16.00,
        "official",
        cache_read_multiplier=0.10,
        audio_input_per_m=32.00,
        audio_output_per_m=64.00,
        note="OpenAI Realtime GA text, cached-input, and audio token rates.",
    ),
    "openai/gpt-realtime-2025-08-28": TokenModelPrice(
        "openai",
        4.00,
        16.00,
        "official",
        cache_read_multiplier=0.10,
        audio_input_per_m=32.00,
        audio_output_per_m=64.00,
        note="Pinned OpenAI Realtime GA snapshot.",
    ),
    "openai/gpt-realtime-2": TokenModelPrice(
        "openai",
        4.00,
        24.00,
        "official",
        cache_read_multiplier=0.10,
        audio_input_per_m=32.00,
        audio_output_per_m=64.00,
        note="OpenAI GPT-Realtime-2 text and audio rates, verified 2026-08-31.",
    ),
    "openai/gpt-image-1-mini": TokenModelPrice(
        "openai",
        2.00,
        8.00,
        "official",
        note="Text input and image output token rates; image input tokens bill separately.",
    ),
    "openai/gpt-image-2": TokenModelPrice(
        "openai",
        5.00,
        30.00,
        "official",
        note="Text-token rate; image input tokens bill at $8/M and are not represented here.",
    ),
    "openai/gpt-image-2.6-sunburst": TokenModelPrice(
        "openai",
        5.00,
        30.00,
        "vercel",
        cache_read_multiplier=0.25,
        note="Vercel AI Gateway token rates, verified 2026-11-05.",
    ),
    "openai/gpt-image-2.6-flare": TokenModelPrice(
        "openai",
        5.00,
        30.00,
        "official",
        cache_read_multiplier=0.25,
        note="Text input/output token rates; image input is $8/M ($2/M cached).",
    ),
    "text-embedding-3-small": TokenModelPrice("openai", 0.02, 0.0, "official"),
    "text-embedding-3-large": TokenModelPrice("openai", 0.13, 0.0, "official"),
    "text-embedding-ada-002": TokenModelPrice("openai", 0.10, 0.0, "official"),

    # Anthropic
    "anthropic/claude-fable-5.1": TokenModelPrice("anthropic", 10.00, 50.00, "official"),
    "anthropic/claude-fable-5": TokenModelPrice("anthropic", 10.00, 50.00, "official"),
    "anthropic/claude-opus-5.5": TokenModelPrice(
        "anthropic", 4.00, 20.00, "official",
        cache_read_multiplier=0.05,
        cache_write_multiplier=1.25,
    ),
    "anthropic/claude-opus-5": TokenModelPrice(
        "anthropic", 5.00, 25.00, "official",
        cache_read_multiplier=0.10,
        cache_write_multiplier=1.25,
    ),
    "anthropic/claude-haiku-4.5": TokenModelPrice("anthropic", 1.00, 5.00, "official"),
    "anthropic/claude-sonnet-4.6": TokenModelPrice("anthropic", 3.00, 15.00, "official"),
    "anthropic/claude-opus-4.6": TokenModelPrice("anthropic", 5.00, 25.00, "official"),
    "anthropic/claude-opus-4.7": TokenModelPrice("anthropic", 5.00, 25.00, "official"),

    # Google Gemini
    "google/gemini-2.5-flash-lite": TokenModelPrice("google", 0.10, 0.40, "official"),
    "google/gemini-2.5-flash": TokenModelPrice("google", 0.30, 2.50, "official"),
    "google/gemini-2.5-pro": TokenModelPrice(
        "google", 1.25, 10.00, "official",
        long_context_threshold=200_000,
        long_input_per_m=2.50, long_output_per_m=15.00,
    ),
    "google/gemini-3.5-flash-lite": TokenModelPrice(
        "google", 0.30, 2.50, "official",
        cache_read_multiplier=0.10,
    ),
    "google/gemini-3.6-flash": TokenModelPrice(
        "google", 1.50, 7.50, "official",
        cache_read_multiplier=0.10,
    ),
    "google/gemini-3.1-flash-image": TokenModelPrice("google", 0.50, 3.00, "official"),
    "google/gemini-3.1-flash-image-preview": TokenModelPrice("google", 0.50, 3.00, "official"),

    # DeepSeek
    "deepseek/deepseek-v4-flash": TokenModelPrice("deepseek", 0.14, 0.28, "official"),
    "deepseek/deepseek-v4-pro": TokenModelPrice("deepseek", 0.435, 0.87, "official"),
    "deepseek/deepseek-chat": TokenModelPrice(
        "deepseek",
        0.28,
        0.42,
        "official",
        note="Legacy alias retired by DeepSeek on 2026-07-24; kept for historical usage rows, priced at the final v3.2 rate.",
    ),
    "deepseek/deepseek-v3.2": TokenModelPrice("deepseek", 0.28, 0.42, "official"),

    # Qwen / DashScope. Manor's official routes reach Qwen through the
    # international gateways (Vercel/OpenRouter), which bill the DashScope
    # international USD list price — NOT the cheaper mainland CNY rate.
    "qwen/qwen3.6-plus": TokenModelPrice(
        "qwen",
        0.50,
        3.00,
        "official",
        note="DashScope international rate (mainland CNY pricing does not apply to gateway-routed traffic).",
        long_context_threshold=256_000,
        long_input_per_m=2.00,
        long_output_per_m=6.00,
    ),
    "qwen/qwen3.7-flash": TokenModelPrice(
        "qwen",
        0.03,
        0.13,
        "official",
        note=(
            "DashScope international rate. Qwen prices this model in three prompt tiers "
            "(≤32K, 32K–256K, >256K); TokenModelPrice models one, so >256K requests "
            "(list rate $0.20/$0.80) are billed at the 32K–256K rate."
        ),
        cache_read_multiplier=0.20,
        long_context_threshold=32_000,
        long_input_per_m=0.10,
        long_output_per_m=0.40,
    ),
    "qwen/qwen3.8-max": TokenModelPrice(
        "qwen",
        2.00,
        6.00,
        "official",
        note="DashScope international rate (mainland CNY pricing does not apply to gateway-routed traffic).",
        cache_read_multiplier=0.125,
        cache_write_multiplier=1.25,
    ),

    # Moonshot / Kimi. Keep explicit until Moonshot exposes a stable machine-readable price feed.
    "moonshotai/kimi-k3": TokenModelPrice(
        "moonshotai",
        3.00,
        15.00,
        "official",
        cache_read_multiplier=0.10,
    ),
    "moonshotai/kimi-k2.6": TokenModelPrice("moonshotai", 0.95, 4.00, "official"),

    # Local embeddings
    "mxbai-embed-large": TokenModelPrice("ollama", 0.0, 0.0, "local"),
    "nomic-embed-text": TokenModelPrice("ollama", 0.0, 0.0, "local"),
}

# Vercel reports Qwen usage under ``alibaba`` while Manor's fixed Catalog uses
# ``qwen``. Price either wire ID identically so routing never changes billing.
for _legacy_qwen_id, _vercel_qwen_id in (
    ("qwen/qwen3.6-plus", "alibaba/qwen3.6-plus"),
    ("qwen/qwen3.7-flash", "alibaba/qwen3.7-flash"),
    ("qwen/qwen3.8-max", "alibaba/qwen3.8-max"),
):
    OFFICIAL_TOKEN_PRICES[_vercel_qwen_id] = OFFICIAL_TOKEN_PRICES[_legacy_qwen_id]


OFFICIAL_IMAGE_PRICES: dict[str, FlatModelPrice] = {
    "openai/gpt-image-1-mini": FlatModelPrice("openai", "image", 0.011, "official"),
    "gpt-image-1-mini": FlatModelPrice("openai", "image", 0.011, "official"),
    "openai/gpt-image-1": FlatModelPrice("openai", "image", 0.04, "official"),
    "gpt-image-1": FlatModelPrice("openai", "image", 0.04, "official"),
    "openai/gpt-image-2": FlatModelPrice("openai", "image", 0.08, "official"),
    "openai/gpt-image-2.6-sunburst": FlatModelPrice(
        "openai", "image", 0.08, "estimate",
        note="No-usage fallback estimate using the GPT Image 2 baseline; billed per token by Vercel.",
    ),
    # $60/M image-output tokens; the default 1024px image is 1120 tokens ≈ $0.067.
    "google/gemini-3.1-flash-image-preview": FlatModelPrice("google", "image", 0.067, "official"),
    "google/gemini-3.1-flash-image": FlatModelPrice("google", "image", 0.067, "official"),
    # Grok Imagine, per output image at the Vercel AI Gateway's default quality
    # (managed calls route there; 2.0 low quality is $0.04, 2048px $0.08).
    # Edits also bill each source image ($0.002 / $0.01 on 2.0).
    "xai/grok-imagine-image": FlatModelPrice("xai", "image", 0.02, "vercel"),
    "xai/grok-imagine-image-2.0": FlatModelPrice("xai", "image", 0.06, "vercel"),
}


OFFICIAL_AUDIO_PRICES: dict[str, FlatModelPrice] = {
    "zyphra/zonos-v0.1-hybrid": FlatModelPrice("zyphra", "audio_asset", 0.01, "official"),
    "zyphra/zonos-v0.1-transformer": FlatModelPrice("zyphra", "audio_asset", 0.01, "official"),
    "sesame/csm-1b": FlatModelPrice("openrouter", "audio_asset", 0.01, "openrouter_fallback"),
    "google/lyria-3-clip-preview": FlatModelPrice("google", "audio_asset", 0.04, "official"),
    "google/lyria-3-pro-preview": FlatModelPrice("google", "audio_asset", 0.08, "official"),
    # Vercel AI Gateway list price per generated second; the gateway-reported
    # cost is billed when the response carries one.
    "elevenlabs/music-v2.5": FlatModelPrice("vercel", "second", 0.00125, "vercel"),
    "openai/gpt-audio-mini": FlatModelPrice("openai", "audio_asset", 0.02, "official"),
    "openai/gpt-audio": FlatModelPrice("openai", "audio_asset", 0.04, "official"),
}
# Lyria's per-asset price is the same on both of its routes: Google lists
# $0.04 per Clip / $0.08 per Pro song, and OpenRouter passes those through
# (https://openrouter.ai/google/lyria-3-clip-preview). OpenRouter-routed calls
# bill the gateway-reported ``usage.cost`` when the stream carries one.


# Speech models billed per input character (Unicode code points, inline audio
# tags included). ElevenLabs only runs through OpenRouter, which passes
# ElevenLabs list prices through without markup; rates from OpenRouter's model
# list (``pricing.prompt`` per character), checked 2026-10-20.
PER_CHARACTER_AUDIO_PRICES: dict[str, FlatModelPrice] = {
    "elevenlabs/eleven-v4": FlatModelPrice("openrouter", "character", 0.00008, "openrouter"),
    "elevenlabs/eleven-v4-turbo": FlatModelPrice("openrouter", "character", 0.00004, "openrouter"),
    "elevenlabs/eleven-v3": FlatModelPrice("openrouter", "character", 0.00008, "openrouter"),
    "elevenlabs/eleven-multilingual-v2": FlatModelPrice("openrouter", "character", 0.00008, "openrouter"),
    "elevenlabs/eleven-flash-v2.5": FlatModelPrice("openrouter", "character", 0.00004, "openrouter"),
    # OpenAI list prices: $15 / $30 per 1M characters.
    "openai/tts-1": FlatModelPrice("openai", "character", 0.000015, "official"),
    "openai/tts-1-hd": FlatModelPrice("openai", "character", 0.00003, "official"),
}


@dataclass(frozen=True)
class SpeechTokenPrice:
    """A speech model billed on text-input and audio-output tokens.

    ``audio_tokens_per_second`` turns a measured clip length into the audio
    tokens the provider bills when a response carries no token counts.
    """

    input_per_m: float
    audio_output_per_m: float
    audio_tokens_per_second: float


SPEECH_TOKEN_PRICES: dict[str, SpeechTokenPrice] = {
    # $0.60 / 1M text in, $12 / 1M audio out (about $0.015 a minute, so about
    # 20.8 audio tokens a second).
    "openai/gpt-4o-mini-tts": SpeechTokenPrice(0.60, 12.00, 20.8),
    # $1 / 1M text in, $20 / 1M audio out; 25 audio tokens a second.
    "google/gemini-3.1-flash-tts-preview": SpeechTokenPrice(1.00, 20.00, 25.0),
}
# Used when a caller cannot report the synthesized text length.
_DEFAULT_SPEECH_CHARACTERS = 1000
# Estimate for per-second music models: one full-length song.
_DEFAULT_MUSIC_SECONDS = 180


@dataclass(frozen=True)
class VideoOutputTokenPrice:
    """A video model billed per output token instead of per second.

    Clip length times the resolution's output-token rate gives the token count;
    the count times the per-million price gives the cost.
    """

    usd_per_m_output_tokens: float
    tokens_per_second: dict[str, int]


# One table prices every video model. An entry is either per-second USD by
# resolution, or a per-token price. A model missing from the table is unpriced:
# callers must flag it, never record it at zero.
#
# Seedance bills per video token: tokens = W×H×fps(24)×seconds / 1024.
# Per-second rates below derive from BytePlus list prices at 24fps,
# no-video-input; Seedance 2.5 has its own $10.7/M and $11.7/M tiers.
# Official tiers stop at 1080p (+4k); "1440p" keeps the 1080p rate so a
# stale resolution preference can never under-bill.
VIDEO_COST_PER_SECOND: dict[str, dict[str, float] | VideoOutputTokenPrice] = {
    # BytePlus Seedance 2.5: $10.7/M tokens at 480p/720p, $11.7/M at 1080p.
    "bytedance/seedance-2.5": {
        "480p": 0.103,
        "720p": 0.231,
        "1080p": 0.569,
        "1440p": 0.569,
    },
    "bytedance/seedance-2.0": {
        "480p": 0.068,
        "720p": 0.151,
        "1080p": 0.374,
        "1440p": 0.374,
    },
    "bytedance/seedance-2.0-fast": {
        "480p": 0.054,
        "720p": 0.121,
        "1080p": 0.272,
        "1440p": 0.272,
    },
    "bytedance/seedance-v1.0-pro-fast": {
        "480p": 0.0097,
        "720p": 0.0206,
        "1080p": 0.049,
        "1440p": 0.049,
    },
    # Kling official API per-second rates (no-audio tier — Manor's Kling
    # routes generate without native audio; the with-audio tier is +50%).
    "kwaivgi/kling-v3.0": {
        "480p": 0.168,
        "720p": 0.168,
        "1080p": 0.168,
        "1440p": 0.168,
    },
    "kwaivgi/kling-v3.0-std": {
        "480p": 0.168,
        "720p": 0.168,
        "1080p": 0.168,
        "1440p": 0.168,
    },
    "kwaivgi/kling-v3.0-pro": {
        "480p": 0.224,
        "720p": 0.224,
        "1080p": 0.224,
        "1440p": 0.224,
    },
    # Atlas Cloud Wan 2.2 turbo: $0.02 per 5s at 480p, ×2 at 720p, ×3 at
    # "1080p" (VSR upscale). BYOK-only — these rates inform estimates only;
    # BYOK calls bill 0 credits.
    "atlascloud/wan-2.2-turbo-spicy": {
        "480p": 0.004,
        "720p": 0.008,
        "1080p": 0.012,
        "1440p": 0.012,
    },
    # Grok Imagine Video, USD per output second (docs.x.ai and the Vercel AI
    # Gateway catalog agree, 2026-10). Managed calls go through Vercel and bill
    # these rates; BYOK calls bill 0 credits. 1.5 also bills image/video inputs.
    "xai/grok-imagine-video-1.5": {
        "480p": 0.08,
        "720p": 0.14,
        "1080p": 0.25,
        "1440p": 0.25,
    },
    "xai/grok-imagine-video-1.5-lite": {
        "480p": 0.02,
        "720p": 0.03,
        "1080p": 0.14,
        "1440p": 0.14,
    },
    # 720p at most: higher requests render at 720p (Vercel bills 720p at 0.07).
    "xai/grok-imagine-video": {
        "480p": 0.05,
        "720p": 0.07,
        "1080p": 0.07,
        "1440p": 0.07,
    },
    # Gemini Omni 1.2 Flash bills output tokens at $12 per 1M. Per-second
    # rates by resolution would hard-code a product of two numbers, so the
    # token rates are the entry. Source: https://aistudio.google.com/docs/pricing
    "google/gemini-omni-1.2-flash": VideoOutputTokenPrice(
        usd_per_m_output_tokens=12.0,
        tokens_per_second={"360p": 1931, "720p": 5792, "1080p": 8688, "4k": 17376},
    ),
}

DEFAULT_INPUT_COST_PER_M = 1.50
DEFAULT_OUTPUT_COST_PER_M = 5.00
DEFAULT_MODEL_MULTIPLIER = 5.0
BASELINE_INPUT_COST_PER_M = 0.32


def openrouter_pricing_cache_path() -> str:
    configured = (os.getenv("OPENROUTER_PRICING_CACHE_PATH") or "").strip()
    if configured:
        return configured
    from packages.core.services.runtime_paths import private_runtime_dir
    return str(private_runtime_dir("cache") / "openrouter-pricing.json")


def vercel_pricing_cache_path() -> str:
    configured = (os.getenv("VERCEL_PRICING_CACHE_PATH") or "").strip()
    if configured:
        return configured
    from packages.core.services.runtime_paths import private_runtime_dir
    return str(private_runtime_dir("cache") / "vercel-pricing.json")


VERCEL_PRICING_SNAPSHOT_PATH = Path(__file__).parent / "data" / "vercel-pricing-snapshot.json"


def _load_vercel_snapshot() -> dict:
    """The catalog committed with the code, used until the first sync lands."""
    try:
        models = json.loads(VERCEL_PRICING_SNAPSHOT_PATH.read_text()).get("models") or {}
        return models if isinstance(models, dict) else {}
    except Exception:
        logger.warning("model pricing: the committed Vercel pricing snapshot is unreadable", exc_info=True)
        return {}


def _load_vercel_cache_if_needed() -> None:
    global _VERCEL_CACHE_MTIME, _VERCEL_CACHE_MODELS
    path = Path(vercel_pricing_cache_path())
    try:
        st = path.stat()
    except Exception:
        # No sync yet (a fresh deploy, CI): price from the committed snapshot.
        if not _VERCEL_CACHE_MODELS:
            _VERCEL_CACHE_MODELS = _load_vercel_snapshot()
        return
    if st.st_mtime <= _VERCEL_CACHE_MTIME:
        return
    try:
        data = json.loads(path.read_text())
        models = data.get("models") or {}
        if isinstance(models, dict):
            _VERCEL_CACHE_MODELS = models
            _VERCEL_CACHE_MTIME = st.st_mtime
    except Exception:
        logger.debug("model pricing: failed to load Vercel pricing cache", exc_info=True)


def _vercel_cache_candidates(model_id: str) -> list[str]:
    candidates = [model_id]
    # Catalog IDs are intentionally gateway-neutral. Include Vercel's wire ID
    # so a compatibility alias still uses the live Gateway price cache.
    from packages.core.services.model_provider_handlers import vercel_model_id

    wire_id = vercel_model_id(model_id)
    if wire_id and wire_id not in candidates:
        candidates.append(wire_id)
    if "/" in model_id:
        prefix, bare = model_id.split("/", 1)
        alias = _VERCEL_PROVIDER_ALIASES.get(prefix.lower())
        provider_alias = f"{alias}/{bare}" if alias else ""
        if provider_alias and provider_alias not in candidates:
            candidates.append(provider_alias)
    return candidates


def _vercel_cached_price(model: str | None) -> tuple[float | None, float | None]:
    if not model:
        return None, None
    _load_vercel_cache_if_needed()
    for candidate in _vercel_cache_candidates(str(model).strip()):
        cached = _VERCEL_CACHE_MODELS.get(candidate)
        if not isinstance(cached, dict):
            continue
        input_per_m = cached.get("input_per_m")
        output_per_m = cached.get("output_per_m")
        if input_per_m is None and output_per_m is None:
            continue
        return (
            float(input_per_m) if input_per_m is not None else None,
            float(output_per_m) if output_per_m is not None else None,
        )
    return None, None


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
    for model, price in {**OFFICIAL_AUDIO_PRICES, **PER_CHARACTER_AUDIO_PRICES}.items():
        rows.append({
            "model": model,
            "provider": price.provider,
            "unit": price.unit,
            "usd": price.usd,
            "source": price.source,
            "note": price.note,
        })
    for model, price in VIDEO_COST_PER_SECOND.items():
        row = {
            "model": model,
            "provider": model.split("/", 1)[0] if "/" in model else "",
            "source": "official",
        }
        if isinstance(price, VideoOutputTokenPrice):
            row.update(
                unit="video_output_token",
                usd_per_m_output_tokens=price.usd_per_m_output_tokens,
                tokens_per_second=dict(price.tokens_per_second),
            )
        else:
            row.update(unit="video_second", usd_by_resolution=dict(price))
        rows.append(row)
    return rows


def _lookup_token_price(model: str | None) -> TokenModelPrice | None:
    for model_id in catalog_model_ids(model):
        price = _lookup_token_price_for_id(model_id)
        if price is not None:
            return price
    return None


def _lookup_token_price_for_id(model_id: str) -> TokenModelPrice | None:
    if model_id in OFFICIAL_TOKEN_PRICES:
        return OFFICIAL_TOKEN_PRICES[model_id]
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    if bare in OFFICIAL_TOKEN_PRICES:
        return OFFICIAL_TOKEN_PRICES[bare]
    # Suffix fallback for dated/tagged variants of known models
    # (e.g. "openai/gpt-4.1-2025-04-14" → "openai/gpt-4.1"). The variant
    # must extend a full registry key, and the longest key wins, so a
    # genuinely new model (e.g. "openai/gpt-9") never inherits another
    # model's price — it falls through to the route caches/defaults.
    lowered = model_id.lower()
    best: tuple[int, TokenModelPrice] | None = None
    for key, price in OFFICIAL_TOKEN_PRICES.items():
        key_l = key.lower()
        if lowered.startswith(key_l + "-") and (best is None or len(key_l) > best[0]):
            best = (len(key_l), price)
    return best[1] if best else None


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


def catalog_model_ids(model: str | None) -> list[str]:
    """Catalog keys that may describe ``model``.

    Catalogs key models as ``provider/model``. A native BYOK route calls the
    provider's bare id (``gpt-6.1-sol``), so a bare id also tries the id
    qualified with its provider; otherwise every catalog fact about it (context
    window, reasoning controls, long-context tier) silently falls back to a
    default.
    """
    model_id = str(model or "").strip()
    if not model_id:
        return []
    ids = [model_id]
    if "/" not in model_id:
        from packages.core.ai.llm_client import provider_for_model

        provider = provider_for_model(model_id)
        if provider:
            ids.append(f"{provider}/{model_id}")
    return ids


def _cached_catalog_entries(model_id: str) -> list[dict | None]:
    """The Vercel and OpenRouter catalog entries for ``model_id``, if any."""
    from packages.core.services.model_provider_handlers import openrouter_model_id

    _load_vercel_cache_if_needed()
    _load_openrouter_cache_if_needed()
    ids = catalog_model_ids(model_id)
    return [
        next((_VERCEL_CACHE_MODELS[c] for m in ids for c in _vercel_cache_candidates(m)
              if isinstance(_VERCEL_CACHE_MODELS.get(c), dict)), None),
        next((_PRICING_CACHE_MODELS[c] for m in ids for c in dict.fromkeys((m, openrouter_model_id(m) or m))
              if isinstance(_PRICING_CACHE_MODELS.get(c), dict)), None),
    ]


def cached_context_windows(model: str | None) -> list[int]:
    """Live context windows for ``model`` from the synced gateway caches."""
    if not model:
        return []
    entries = _cached_catalog_entries(str(model).strip())
    windows = []
    for entry in entries:
        value = (entry or {}).get("context_window")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            windows.append(value)
    return windows


def cached_reasoning_efforts(model: str | None) -> list[str] | None:
    """Reasoning efforts ``model`` accepts per the synced gateway catalogs.

    None when no catalog lists an effort control for it (unknown or
    non-reasoning model); the gateways can disagree, so Vercel's explicit
    value list wins over OpenRouter's parameter flag.
    """
    if not model:
        return None
    entries = _cached_catalog_entries(str(model).strip())
    for entry in entries:
        values = (entry or {}).get("reasoning_efforts")
        if isinstance(values, list) and values:
            return [str(value) for value in values]
    return None


def cached_reasoning_controls(model: str | None) -> dict[str, Any]:
    """How ``model``'s reasoning is controlled, per the synced gateway catalogs.

    ``efforts`` (effort values or None), ``toggle`` (can be switched on) and
    ``budget`` ({min, max} thinking tokens or None). Vercel's explicit options
    win over OpenRouter's parameter flags.
    """
    controls: dict[str, Any] = {"efforts": None, "toggle": False, "budget": None}
    if not model:
        return controls
    entries = _cached_catalog_entries(str(model).strip())
    for entry in entries:
        entry = entry or {}
        values = entry.get("reasoning_efforts")
        if controls["efforts"] is None and isinstance(values, list) and values:
            controls["efforts"] = [str(value) for value in values]
        if entry.get("reasoning_toggle") is True:
            controls["toggle"] = True
        budget = entry.get("reasoning_budget")
        if controls["budget"] is None and isinstance(budget, dict) and isinstance(budget.get("max"), int):
            controls["budget"] = {"min": int(budget.get("min") or 1), "max": int(budget["max"])}
    return controls


def long_context_threshold(model: str | None) -> int | None:
    price = _lookup_token_price(model)
    return price.long_context_threshold if price else None


def resolve_token_unit_prices(
    model: str | None,
    *,
    pricing_source: str | None = None,
    provider: str | None = None,
) -> tuple[float, float] | None:
    """Per-1M token prices that bill a call, or None when no source prices the model.

    A bare model name (as a provider reports it, e.g. ``gpt-6.1-sol``) is first
    looked up under its provider (``openai/gpt-6.1-sol``), the catalog's id, so
    the more specific match wins over a bare-name guess.
    """
    model_id = str(model or "").strip()
    if model_id and "/" not in model_id and str(provider or "").strip():
        qualified = _resolve_exact_token_prices(
            f"{str(provider).strip().lower()}/{model_id}",
            pricing_source=pricing_source,
            provider=provider,
        )
        if qualified is not None:
            return qualified
    return _resolve_exact_token_prices(model_id, pricing_source=pricing_source, provider=provider)


def _resolve_exact_token_prices(
    model: str | None,
    *,
    pricing_source: str | None = None,
    provider: str | None = None,
) -> tuple[float, float] | None:
    """The registry-then-catalog lookup for one exact model id.

    The official registry always wins. A gateway's synced catalog (the route
    that served the call first, then the other) prices a model the registry
    does not list; a catalog entry missing either side does not count, so a
    half-known price never borrows a guessed default.
    """
    source = str(pricing_source or "").lower()
    route_provider = str(provider or "").lower()
    use_openrouter = source == "openrouter" or route_provider == "openrouter"

    official = _lookup_token_price(model)
    if official:
        return official.input_per_m, official.output_per_m

    catalogs = (
        (_openrouter_cached_price, _vercel_cached_price)
        if use_openrouter
        else (_vercel_cached_price, _openrouter_cached_price)
    )
    for catalog in catalogs:
        cached_input, cached_output = catalog(model)
        if cached_input is not None and cached_output is not None:
            return cached_input, cached_output
    return None


def token_unit_prices(
    model: str | None,
    *,
    pricing_source: str | None = None,
    provider: str | None = None,
) -> tuple[float, float]:
    """Per-1M token prices for estimates made before a call (reservations, quotes).

    Billing a finished call uses ``resolve_token_unit_prices`` and never these
    defaults; an estimate still needs a number for a model nobody prices.
    """
    resolved = resolve_token_unit_prices(model, pricing_source=pricing_source, provider=provider)
    if resolved is not None:
        return resolved
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
    audio_input_tokens: int = 0,
    audio_output_tokens: int = 0,
    cached_audio_input_tokens: int = 0,
    require_price: bool = False,
) -> float | None:
    """USD for a call's tokens at the model's price.

    With ``require_price`` a model no source prices returns None instead of
    the default estimate: a finished call is never billed at a guessed rate.
    """
    if require_price:
        resolved = resolve_token_unit_prices(model, pricing_source=pricing_source, provider=provider)
        if resolved is None:
            return None
        input_per_m, output_per_m = resolved
    else:
        input_per_m, output_per_m = token_unit_prices(
            model,
            pricing_source=pricing_source,
            provider=provider,
        )
    # Per-model cache multipliers from the registry win over the caller's
    # flat defaults (providers differ: Anthropic writes cost 1.25×, OpenAI
    # charges nothing extra for cache writes, etc.).
    official = _lookup_token_price(model)
    if official:
        if official.cache_read_multiplier is not None:
            cache_read_multiplier = official.cache_read_multiplier
        if official.cache_write_multiplier is not None:
            cache_write_multiplier = official.cache_write_multiplier
        # Long-context tier: providers bill the whole request at the higher
        # rate once the prompt crosses the threshold. Gateways pass provider
        # list prices through, so the tier applies on every route.
        if (
            official.long_context_threshold
            and int(input_tokens or 0) > official.long_context_threshold
        ):
            if official.long_input_per_m is not None:
                input_per_m = official.long_input_per_m
            if official.long_output_per_m is not None:
                output_per_m = official.long_output_per_m

    # Audio tokens (OpenAI audio chat models) bill at their own rate. They
    # are included in the provider's prompt/completion totals, so carve
    # them out of the text buckets to avoid double-counting. Without a
    # registry rate they simply stay priced as text.
    audio_in = int(audio_input_tokens or 0)
    audio_out = int(audio_output_tokens or 0)
    cached_audio_in = min(
        max(0, int(cached_audio_input_tokens or 0)),
        max(0, audio_in),
        max(0, int(cache_read_tokens or 0)),
    )
    uncached_audio_in = max(0, audio_in - cached_audio_in)
    cached_text_in = max(0, int(cache_read_tokens or 0) - cached_audio_in)
    audio_cost = 0.0
    cached_audio_cost = cached_audio_in * input_per_m * cache_read_multiplier
    if official and (audio_in or audio_out):
        if official.audio_input_per_m is not None:
            audio_cost += uncached_audio_in * official.audio_input_per_m
        else:
            uncached_audio_in = 0
        if official.audio_output_per_m is not None:
            audio_cost += audio_out * official.audio_output_per_m
        else:
            audio_out = 0
        if cached_audio_in and official.cached_audio_input_per_m is not None:
            cached_audio_cost = cached_audio_in * (
                official.cached_audio_input_per_m
            )
    else:
        uncached_audio_in = audio_out = 0

    base_input = max(
        0,
        int(input_tokens or 0)
        - int(cache_read_tokens or 0)
        - int(cache_creation_tokens or 0)
        - uncached_audio_in,
    )
    base_output = max(0, int(output_tokens or 0) - audio_out)
    return (
        base_input * input_per_m
        + cached_text_in * input_per_m * cache_read_multiplier
        + int(cache_creation_tokens or 0) * input_per_m * cache_write_multiplier
        + base_output * output_per_m
        + audio_cost
        + cached_audio_cost
    ) / 1_000_000


def model_cost_multiplier(model: str | None) -> float:
    price = _lookup_token_price(model)
    if not price:
        return DEFAULT_MODEL_MULTIPLIER
    if price.input_per_m <= 0:
        return 0.0
    return max(0.0, price.input_per_m / BASELINE_INPUT_COST_PER_M)


# Seedance bills all tokens at a reduced rate when the input contains video
# ($4.3/M vs $7/M standard; $3.3/M vs $5.6/M fast).
_SEEDANCE_VIDEO_INPUT_FACTOR = 4.3 / 7.0
_SEEDANCE_FAST_VIDEO_INPUT_FACTOR = 3.3 / 5.6
_SEEDANCE_25_VIDEO_INPUT_FACTOR = 6.4 / 10.7
_SEEDANCE_25_1080_VIDEO_INPUT_FACTOR = 7.0 / 11.7
# Kling charges +50% for native audio generation.
_KLING_AUDIO_FACTOR = 1.5

# Seedance bills the tokens its output takes: W × H × 24 fps × seconds ÷ 1024.
# Its per-second rates are those tokens at a per-million price, so the per-token
# price is the per-second rate over one second of tokens. Pixel counts are the
# 16:9 frame for each tier; 1440p bills at the 1080p rate, as the table does.
_SEEDANCE_TOKEN_MODELS = frozenset({
    "bytedance/seedance-2.5",
    "bytedance/seedance-2.0",
    "bytedance/seedance-2.0-fast",
    "bytedance/seedance-v1.0-pro-fast",
})
_SEEDANCE_TOKEN_FRAME = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (1920, 1080),
}
_SEEDANCE_TOKEN_FPS = 24
_SEEDANCE_TOKEN_DIVISOR = 1024


def _seedance_video_input_factor(model: str, resolution: str) -> float:
    lowered = str(model or "").lower()
    if "seedance-2.5" in lowered:
        return _SEEDANCE_25_1080_VIDEO_INPUT_FACTOR if resolution in {"1080p", "1440p"} else _SEEDANCE_25_VIDEO_INPUT_FACTOR
    if "fast" in lowered:
        return _SEEDANCE_FAST_VIDEO_INPUT_FACTOR
    return _SEEDANCE_VIDEO_INPUT_FACTOR


def video_token_price_per_m(model: str | None, resolution: str = "720p", *, has_video_input: bool = False) -> float | None:
    """USD per million output tokens for a video model, or None when it is billed per second.

    Gemini bills its output tokens directly. Seedance's per-token price is
    derived from its per-second rate, so the same clip costs the same either
    way. Kling, Atlas and Grok bill per second and have no token price here.
    """
    key = str(model or "")
    price = VIDEO_COST_PER_SECOND.get(key)
    if isinstance(price, VideoOutputTokenPrice):
        return price.usd_per_m_output_tokens
    from packages.core.services import catalog_pricing

    catalog_rate = catalog_pricing.video_token_price_per_m(key, resolution, has_video_input=has_video_input)
    if catalog_rate is not None:
        return catalog_rate
    if key not in _SEEDANCE_TOKEN_MODELS or not isinstance(price, dict):
        return None
    width, height = _SEEDANCE_TOKEN_FRAME.get(resolution, _SEEDANCE_TOKEN_FRAME["720p"])
    per_second = float(price.get(resolution, price.get("720p", 0.0)))
    tokens_per_second = width * height * _SEEDANCE_TOKEN_FPS / _SEEDANCE_TOKEN_DIVISOR
    rate = per_second / tokens_per_second * 1_000_000
    if has_video_input:
        rate *= _seedance_video_input_factor(key, resolution)
    return rate


def video_cost_from_tokens(
    model: str | None,
    output_tokens: float | None,
    resolution: str = "720p",
    *,
    has_video_input: bool = False,
) -> float | None:
    """USD for a clip's official output tokens, or None when the model has no token price."""
    if output_tokens is None:
        return None
    rate = video_token_price_per_m(model, resolution, has_video_input=has_video_input)
    if rate is None:
        return None
    return max(0.0, float(output_tokens)) * rate / 1_000_000


def video_is_priced(model: str | None) -> bool:
    """Whether the curated table or the gateway catalog has a rate for this video model."""
    from packages.core.services import catalog_pricing

    model_id = str(model or "")
    return model_id in VIDEO_COST_PER_SECOND or catalog_pricing.has_video_price(model_id)


def video_output_tokens(model: str, duration_seconds: float, resolution: str = "720p") -> int:
    """Output tokens a clip of this length and resolution is billed for.

    Zero for models priced per second. A resolution without its own rate uses
    the 720p rate, as the per-second table does.
    """
    price = VIDEO_COST_PER_SECOND.get(model)
    if not isinstance(price, VideoOutputTokenPrice):
        return 0
    rate = price.tokens_per_second.get(resolution, price.tokens_per_second["720p"])
    return round(rate * max(0.0, float(duration_seconds or 0)))


def priced_video_cost_usd(
    model: str,
    duration_seconds: float,
    resolution: str = "720p",
    *,
    with_audio: bool = False,
    has_video_input: bool = False,
) -> float | None:
    """A clip's list-price cost by length, or None when nothing prices the model.

    The curated table first, then the gateway catalog's per-second rate. Used
    when the provider reports no tokens, and for quotes before a call.
    """
    price = VIDEO_COST_PER_SECOND.get(model)
    if price is None:
        from packages.core.services import catalog_pricing

        per_second = catalog_pricing.video_cost_per_second(model, resolution, with_audio=with_audio)
        if per_second is None:
            return None
        return per_second * max(0.0, float(duration_seconds or 0))
    if isinstance(price, VideoOutputTokenPrice):
        tokens = video_output_tokens(model, duration_seconds, resolution)
        return tokens * price.usd_per_m_output_tokens / 1_000_000
    rate = float(price.get(resolution, price.get("720p", 0.134)))
    lowered = str(model or "").lower()
    if has_video_input and "seedance" in lowered:
        rate *= _seedance_video_input_factor(model, resolution)
    if with_audio and "kling" in lowered:
        rate *= _KLING_AUDIO_FACTOR
    return rate * max(0.0, float(duration_seconds or 0))


def estimate_video_cost_usd(
    model: str,
    duration_seconds: float,
    resolution: str = "720p",
    *,
    with_audio: bool = False,
    has_video_input: bool = False,
) -> float:
    """Pre-run cost used to reserve credits. Unpriced models get a flat guess.

    A finished job never uses this guess: its cost comes from the gateway or
    from ``priced_video_cost_usd``, and an unpriced job records no cost.
    """
    cost = priced_video_cost_usd(
        model, duration_seconds, resolution,
        with_audio=with_audio, has_video_input=has_video_input,
    )
    if cost is None:
        return 0.20 * max(0.0, float(duration_seconds or 0))
    return cost


# Gemini image output bills $60/M image tokens; token count scales with
# output resolution. Ladder keyed by the longest output dimension.
_GEMINI_IMAGE_SIZE_LADDER: tuple[tuple[int, float], ...] = (
    (512, 0.045),
    (1024, 0.067),
    (2048, 0.101),
)
_GEMINI_IMAGE_4K_PRICE = 0.151


def _parse_image_size(size: str) -> tuple[int, int] | None:
    try:
        w, h = str(size or "").strip().lower().split("x", 1)
        return int(w), int(h)
    except Exception:
        return None


def estimate_image_cost_usd(model: str, size: str = "1024x1024") -> float:
    model_id = str(model or "").strip()
    price = OFFICIAL_IMAGE_PRICES.get(model_id)
    if not price and "/" in model_id:
        price = OFFICIAL_IMAGE_PRICES.get(model_id.split("/", 1)[1])
    base = float(price.usd) if price else 0.04

    dims = _parse_image_size(size)
    if not dims:
        return base

    if "gemini-3.1-flash-image" in model_id.lower():
        longest = max(dims)
        for limit, usd in _GEMINI_IMAGE_SIZE_LADDER:
            if longest <= limit:
                return usd
        return _GEMINI_IMAGE_4K_PRICE

    # Other image models (GPT image family): output token count scales
    # roughly with pixel area, so scale the 1024×1024 base price by area.
    # Never scale below base — smaller outputs aren't cheaper in practice.
    area_ratio = (dims[0] * dims[1]) / float(1024 * 1024)
    return base * max(1.0, area_ratio)


@dataclass(frozen=True)
class ImageTokenPrice:
    """An image model's per-1M token rates by token kind.

    Image input and output tokens bill at their own rates, not the text rate.
    ``cached_input_multiplier`` applies to cached text and image input alike.
    """

    text_input_per_m: float
    image_input_per_m: float
    image_output_per_m: float
    text_output_per_m: float = 0.0
    cached_input_multiplier: float = 1.0


_GEMINI_FLASH_IMAGE_TOKENS = ImageTokenPrice(0.50, 0.50, 60.00, text_output_per_m=3.00)
IMAGE_TOKEN_PRICES: dict[str, ImageTokenPrice] = {
    # OpenAI: text $2 / image in $2.50 / image out $8, cached input 10%.
    "openai/gpt-image-1-mini": ImageTokenPrice(2.00, 2.50, 8.00, cached_input_multiplier=0.10),
    # Text $5 ($1.25 cached), image in $8 ($2 cached), image out $30.
    "openai/gpt-image-2": ImageTokenPrice(5.00, 8.00, 30.00, cached_input_multiplier=0.25),
    "openai/gpt-image-2.6-flare": ImageTokenPrice(5.00, 8.00, 30.00, cached_input_multiplier=0.25),
    # Vercel publishes text $5 / out $30 only; image input bills at the text rate.
    "openai/gpt-image-2.6-sunburst": ImageTokenPrice(5.00, 5.00, 30.00, cached_input_multiplier=0.25),
    # Google: input $0.50, text out (thinking included) $3, image out $60.
    "google/gemini-3.1-flash-image": _GEMINI_FLASH_IMAGE_TOKENS,
    "google/gemini-3.1-flash-image-preview": _GEMINI_FLASH_IMAGE_TOKENS,
}


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0)) if not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return 0


def _modality_tokens(details: Any, modality: str) -> int:
    total = 0
    for entry in details or []:
        if isinstance(entry, dict) and str(entry.get("modality") or "").upper() == modality:
            total += _usage_int(entry.get("tokenCount") or entry.get("token_count") or entry.get("tokens"))
    return total


def _image_usage_buckets(usage: Any) -> dict[str, int] | None:
    """Token counts by kind from any image response shape, or None without tokens.

    Shapes: OpenAI Images (``input_tokens`` + details), the AI Gateway's
    camelCase copy of it, OpenRouter chat (``prompt_tokens`` /
    ``completion_tokens``), and Gemini ``usageMetadata``.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        prompt = _usage_int(usage.get("promptTokenCount"))
        image_in = min(prompt, _modality_tokens(usage.get("promptTokensDetails"), "IMAGE"))
        candidates = _usage_int(usage.get("candidatesTokenCount"))
        image_out = _modality_tokens(usage.get("candidatesTokensDetails"), "IMAGE") or candidates
        text_out = max(0, candidates - image_out) + _usage_int(usage.get("thoughtsTokenCount"))
        cached = min(prompt, _usage_int(usage.get("cachedContentTokenCount")))
        return {"text_in": prompt - image_in, "image_in": image_in, "cached_text_in": min(cached, prompt - image_in),
                "cached_image_in": 0, "image_out": image_out, "text_out": text_out}
    input_key = next((k for k in ("input_tokens", "inputTokens") if k in usage), None)
    if input_key is not None or "output_tokens" in usage or "outputTokens" in usage:
        input_tokens = _usage_int(usage.get(input_key)) if input_key else 0
        output_tokens = _usage_int(usage.get("output_tokens", usage.get("outputTokens")))
        details = usage.get("input_tokens_details") or usage.get("inputTokensDetails") or {}
        details = details if isinstance(details, dict) else {}
        image_in = min(input_tokens, _usage_int(details.get("image_tokens", details.get("imageTokens"))))
        text_in = input_tokens - image_in
        cached_image = min(image_in, _usage_int(details.get("cached_image_tokens", details.get("cachedImageTokens"))))
        cached_total = _usage_int(details.get("cached_tokens", details.get("cachedTokens", usage.get("cached_input_tokens"))))
        cached_text = min(text_in, _usage_int(details.get("cached_text_tokens")) or max(0, cached_total - cached_image))
        return {"text_in": text_in, "image_in": image_in, "cached_text_in": cached_text,
                "cached_image_in": cached_image, "image_out": output_tokens, "text_out": 0}
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        prompt = _usage_int(usage.get("prompt_tokens"))
        completion = _usage_int(usage.get("completion_tokens"))
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        image_in = min(prompt, _usage_int(prompt_details.get("image_tokens")) if isinstance(prompt_details, dict) else 0)
        image_out = (_usage_int(completion_details.get("image_tokens")) if isinstance(completion_details, dict) else 0) or completion
        cached = min(prompt - image_in, _usage_int(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else 0)
        return {"text_in": prompt - image_in, "image_in": image_in, "cached_text_in": cached,
                "cached_image_in": 0, "image_out": image_out, "text_out": max(0, completion - image_out)}
    return None


def image_call_cost_usd(model: str, usage: Any, size: str = "1024x1024") -> tuple[float | None, str]:
    """A finished image call's USD cost, and its basis.

    The provider's token counts at the model's per-kind token rates first; a
    response without tokens falls back to the official per-image list price.
    ``(None, "unpriced")`` when the model has neither.
    """
    model_id = str(model or "").strip()
    price = IMAGE_TOKEN_PRICES.get(model_id)
    buckets = _image_usage_buckets(usage) if price else None
    if price and buckets:
        cached_rate = price.cached_input_multiplier
        cost = (
            (buckets["text_in"] - buckets["cached_text_in"]) * price.text_input_per_m
            + buckets["cached_text_in"] * price.text_input_per_m * cached_rate
            + (buckets["image_in"] - buckets["cached_image_in"]) * price.image_input_per_m
            + buckets["cached_image_in"] * price.image_input_per_m * cached_rate
            + buckets["image_out"] * price.image_output_per_m
            + buckets["text_out"] * price.text_output_per_m
        ) / 1_000_000
        return cost, "official_tokens"
    if OFFICIAL_IMAGE_PRICES.get(model_id) or ("/" in model_id and OFFICIAL_IMAGE_PRICES.get(model_id.split("/", 1)[1])):
        return estimate_image_cost_usd(model_id, size=size), "official_per_image"
    from packages.core.services import catalog_pricing

    token_prices = catalog_pricing.image_token_prices(model_id)
    buckets = _image_usage_buckets(usage) if token_prices else None
    if token_prices and buckets:
        input_rate, output_rate, cached_rate = token_prices
        cached_rate = input_rate if cached_rate is None else cached_rate
        cached = buckets["cached_text_in"] + buckets["cached_image_in"]
        uncached = buckets["text_in"] + buckets["image_in"] - cached
        cost = (
            uncached * input_rate + cached * cached_rate
            + (buckets["image_out"] + buckets["text_out"]) * output_rate
        ) / 1_000_000
        return cost, "catalog_tokens"
    per_image = catalog_pricing.image_cost_per_image(model_id, size)
    if per_image is not None:
        return per_image, "catalog_per_image"
    return None, "unpriced"


_TRANSCRIPTION_USD_PER_MINUTE = (
    # (id fragment, official USD per minute)
    ("mini-transcribe", 0.003),
    ("groq/", 0.111 / 60.0),
    ("large-v3", 0.111 / 60.0),
    ("whisper", 0.006),
)


def transcription_usd_per_minute(model: str | None) -> float | None:
    """A transcription model's USD per audio minute: curated, else the gateway catalog."""
    lowered = str(model or "").lower()
    for fragment, rate in _TRANSCRIPTION_USD_PER_MINUTE:
        if fragment in lowered:
            return rate
    from packages.core.services import catalog_pricing

    per_second = catalog_pricing.transcription_cost_per_second(str(model or ""))
    return per_second * 60.0 if per_second is not None else None


def audio_call_cost_usd(
    model: str,
    *,
    characters: int | None = None,
    seconds: float | None = None,
    input_tokens: int | None = None,
    audio_output_tokens: int | None = None,
) -> tuple[float | None, str]:
    """A finished audio call's USD cost on the provider's own billing unit, and its basis.

    Per character, per token, per generated second or per asset, as the
    provider bills that model. ``(None, "unpriced")`` when the model has no
    price or the unit it bills on was not measured: a guess is never billed.
    """
    model_id = str(model or "").strip()
    per_character = PER_CHARACTER_AUDIO_PRICES.get(model_id)
    if per_character:
        if characters is None:
            return None, "unpriced"
        return float(per_character.usd) * max(0, int(characters)), "official_characters"
    speech = SPEECH_TOKEN_PRICES.get(model_id)
    if speech:
        in_tokens = max(0, int(input_tokens if input_tokens is not None else (characters or 0) / 4))
        if audio_output_tokens is not None:
            out_tokens = max(0, int(audio_output_tokens))
            basis = "official_tokens"
        elif seconds is not None:
            out_tokens = round(max(0.0, float(seconds)) * speech.audio_tokens_per_second)
            basis = "official_rate_measured_seconds"
        else:
            return None, "unpriced"
        cost = (in_tokens * speech.input_per_m + out_tokens * speech.audio_output_per_m) / 1_000_000
        return cost, basis
    price = OFFICIAL_AUDIO_PRICES.get(model_id)
    if price and price.unit == "second":
        if seconds is None:
            return None, "unpriced"
        return float(price.usd) * max(0.0, float(seconds)), "official_seconds"
    if price:
        return float(price.usd), "official_asset"
    from packages.core.services import catalog_pricing

    return catalog_pricing.speech_cost(
        model_id,
        characters=characters,
        seconds=seconds,
        input_tokens=input_tokens,
        audio_output_tokens=audio_output_tokens,
    )


def estimate_audio_cost_usd(model: str, *, purpose: str = "", characters: int | None = None) -> float:
    model_id = str(model or "").strip()
    per_character = PER_CHARACTER_AUDIO_PRICES.get(model_id)
    if per_character:
        count = _DEFAULT_SPEECH_CHARACTERS if characters is None else max(0, int(characters))
        return float(per_character.usd) * count
    speech = SPEECH_TOKEN_PRICES.get(model_id)
    if speech:
        # Quote only: about 15 spoken characters a second.
        count = _DEFAULT_SPEECH_CHARACTERS if characters is None else max(0, int(characters))
        cost, _basis = audio_call_cost_usd(model_id, characters=count, seconds=count / 15)
        return float(cost or 0.0)
    price = OFFICIAL_AUDIO_PRICES.get(model_id)
    if price and price.unit == "second":
        return float(price.usd) * _DEFAULT_MUSIC_SECONDS
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
    if price:
        rate = price.input_per_m
    else:
        from packages.core.services import catalog_pricing

        catalog_rate = catalog_pricing.embedding_price_per_m(model)
        # 0.13 is only reached by a model nothing prices: a BYOK estimate.
        rate = catalog_rate if catalog_rate is not None else 0.13
    return max(0, int(total_tokens or 0)) / 1_000_000 * rate
