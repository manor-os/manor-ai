"""
AI Model configuration — default models and model catalog.

Model roles:
  primary   — main conversational AI (chat, complex reasoning, tool-calling)
  worker    — lightweight tasks (summaries, classification, simple tool calls)
  image     — image generation (DALL-E, Flux, etc.)
  voice     — text-to-speech / narration / dialogue
  audio     — music / score / BGM generation
  sfx       — ambience / Foley / sound effects / transition audio
  stt       — speech-to-text / audio understanding
  video     — video generation
  embedding — text embeddings for RAG

Resolution priority: User preferences > Entity settings > Env vars > Defaults
"""
from __future__ import annotations

import os

# ── Default models ──
DEFAULTS = {
    # Cheapest frontier-lab model in the primary catalog that still does
    # vision + tool-calling ($0.20/$1.20 per 1M vs $3/$15 for Sonnet 4.6).
    # The primary default also backstops ``_resolve_vision_model_if_needed``
    # in llm_client, so it must accept image input — a text-only default
    # would push every image chat onto the hard-coded gpt-4o fallback.
    "primary":   "openai/gpt-5.6-luna",
    # ``worker`` is used by TaskRunner for tool-using agent tasks. Keep the
    # factory default aligned with the first model shown in the Worker picker.
    # Text-only is fine here (unlike ``primary``): a worker task that carries
    # an image is re-routed to the primary default by the vision fallback.
    "worker":    "deepseek/deepseek-v4-flash",
    # Catalog IDs mirror the provider's real model IDs (with a provider
    # namespace), so the default can be sent to OpenAI after stripping only
    # the ``openai/`` namespace.
    "image":     "openai/gpt-image-2",
    "voice":     "google/gemini-3.1-flash-tts-preview",
    "audio":     "google/lyria-3-clip-preview",
    "sfx":       "openai/gpt-audio-mini",
    "stt":       "openai/gpt-4o-audio-preview",
    "video":     "bytedance/seedance-2.0",
    # Local Ollama is bundled with mxbai-embed-large (1024-dim) — that's
    # the only Ollama embedding model present in the container. Switching
    # away means picking an OpenAI / OpenRouter remote model instead.
    "embedding": "mxbai-embed-large",
}


_MODEL_INPUT_MODALITIES = {
    "openai/gpt-4": frozenset({"text"}),
    "openai/gpt-6-astra": frozenset({"text", "image"}),
    "openai/gpt-5.6-sol": frozenset({"text", "image"}),
    "openai/gpt-5.6-terra": frozenset({"text", "image"}),
    "openai/gpt-5.6-luna": frozenset({"text", "image"}),
    "openai/gpt-5.5": frozenset({"text", "image"}),
    "openai/gpt-5.5-pro": frozenset({"text", "image"}),
    "moonshotai/kimi-k3": frozenset({"text", "image", "video"}),
    "moonshotai/kimi-k2.6": frozenset({"text", "image", "video"}),
    "deepseek/deepseek-v4-pro": frozenset({"text"}),
    "deepseek/deepseek-v4-flash": frozenset({"text"}),
    # Product Catalog IDs stay gateway-neutral. Vercel maps these Qwen IDs to
    # its ``alibaba/*`` namespace at the provider boundary.
    "qwen/qwen3.8-max": frozenset({"text", "image", "video"}),
    "qwen/qwen3.7-flash": frozenset({"text", "image", "video"}),
    "qwen/qwen3.6-plus": frozenset({"text", "image", "video"}),
    # Accept settings saved during the short-lived Vercel-canonical migration.
    "alibaba/qwen3.8-max": frozenset({"text", "image", "video"}),
    "alibaba/qwen3.7-flash": frozenset({"text", "image", "video"}),
    "alibaba/qwen3.6-plus": frozenset({"text", "image", "video"}),
    # Gemini chat models accept video for understanding via the native API.
    "google/gemini-3.6-flash": frozenset({"text", "image", "video"}),
    "google/gemini-3.5-flash-lite": frozenset({"text", "image", "video"}),
    "google/gemini-2.5-pro": frozenset({"text", "image", "video"}),
    "google/gemini-2.5-flash": frozenset({"text", "image", "video"}),
    "google/gemini-2.5-flash-lite": frozenset({"text", "image", "video"}),
}
_MODEL_INPUT_MODALITY_PREFIXES = (
    ("openai/gpt-4o", frozenset({"text", "image"})),
    ("openai/gpt-4.1", frozenset({"text", "image"})),
    ("google/gemini-", frozenset({"text", "image"})),
    ("anthropic/claude-", frozenset({"text", "image"})),
)


def model_input_modalities(model_id: str) -> frozenset[str] | None:
    """Return known input modalities for a canonical model ID.

    ``None`` means the model has not been classified. Callers must not treat
    an unknown model as text-only and silently switch its provider.
    """

    canonical = str(model_id or "").strip().lower()
    exact = _MODEL_INPUT_MODALITIES.get(canonical)
    if exact is not None:
        return exact
    for prefix, modalities in _MODEL_INPUT_MODALITY_PREFIXES:
        if canonical.startswith(prefix):
            return modalities
    return None

# ── Model catalog (shown in user settings) ──
CATALOG = {
    # Prefer models that can route through their own lab's native API. Models
    # from providers with generic ``sk-`` keys must have an explicit provider
    # handler so BYOK routing does not silently fall back to OpenAI.
    "primary": [
        # First entry = the factory default in ``DEFAULTS``; keep them in sync.
        {"id": "openai/gpt-5.6-luna",          "name": "GPT-5.6 Luna",       "tier": "budget",   "quality": "good",    "tag": "Recommended"},
        {"id": "openai/gpt-6-astra",           "name": "GPT-6 Astra",        "tier": "premium",  "quality": "highest", "tag": "New"},
        {"id": "anthropic/claude-sonnet-4.6",  "name": "Claude Sonnet 4.6",  "tier": "balanced", "quality": "high",    "tag": "Balanced"},
        {"id": "anthropic/claude-fable-5.1",   "name": "Claude Fable 5.1",   "tier": "premium",  "quality": "highest", "tag": "Most capable"},
        {"id": "anthropic/claude-fable-5",     "name": "Claude Fable 5",     "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "anthropic/claude-opus-5.5",    "name": "Claude Opus 5.5",    "tier": "premium",  "quality": "highest", "tag": "New"},
        {"id": "anthropic/claude-opus-5",      "name": "Claude Opus 5",      "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "anthropic/claude-opus-4.7",    "name": "Claude Opus 4.7",    "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "openai/gpt-5.6-sol",           "name": "GPT-5.6 Sol",        "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "openai/gpt-5.6-terra",         "name": "GPT-5.6 Terra",      "tier": "balanced", "quality": "high",    "tag": "Value"},
        {"id": "openai/gpt-5.5",               "name": "GPT-5.5",            "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "openai/gpt-5.5-pro",           "name": "GPT-5.5 Pro",        "tier": "premium",  "quality": "highest", "tag": "Pro"},
        {"id": "anthropic/claude-opus-4.6",    "name": "Claude Opus 4.6",    "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "anthropic/claude-haiku-4.5",   "name": "Claude Haiku 4.5",   "tier": "budget",   "quality": "good",    "tag": "Fast"},
        {"id": "google/gemini-3.6-flash",      "name": "Gemini 3.6 Flash",   "tier": "balanced", "quality": "high",    "tag": "New"},
        {"id": "google/gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite", "tier": "budget", "quality": "good",   "tag": "New"},
        {"id": "moonshotai/kimi-k3",           "name": "Kimi K3",            "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "moonshotai/kimi-k2.6",         "name": "Kimi K2.6",          "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "qwen/qwen3.8-max",             "name": "Qwen 3.8 Max",       "tier": "balanced", "quality": "highest", "tag": "New"},
        {"id": "qwen/qwen3.7-flash",           "name": "Qwen 3.7 Flash",     "tier": "cheap",    "quality": "good",    "tag": "Cheapest"},
        {"id": "qwen/qwen3.6-plus",            "name": "Qwen 3.6 Plus",      "tier": "balanced", "quality": "high",    "tag": "Coding"},
        {"id": "deepseek/deepseek-v4-pro",     "name": "DeepSeek 4 Pro",      "tier": "premium",  "quality": "highest", "tag": "Reasoning"},
        {"id": "deepseek/deepseek-v4-flash",   "name": "DeepSeek 4 Flash",    "tier": "budget",   "quality": "good",    "tag": "Fast"},
        {"id": "openai/gpt-4.1",              "name": "GPT-4.1",            "tier": "balanced", "quality": "high",    "tag": ""},
        {"id": "openai/gpt-4.1-mini",         "name": "GPT-4.1 Mini",       "tier": "budget",   "quality": "good",    "tag": "Value"},
        {"id": "openai/gpt-4o",               "name": "GPT-4o",             "tier": "balanced", "quality": "high",    "tag": ""},
        {"id": "google/gemini-2.5-pro",       "name": "Gemini 2.5 Pro",     "tier": "balanced", "quality": "high",    "tag": ""},
        {"id": "google/gemini-2.5-flash",     "name": "Gemini 2.5 Flash",   "tier": "budget",   "quality": "good",    "tag": "Fastest"},
    ],
    "worker": [
        # Order = picker order. Models listed here must reliably support
        # tool-calling on OpenRouter — the worker tier runs agent tasks
        # which always require tools. ``deepseek/deepseek-chat`` is
        # deliberately excluded: only Novita serves it with tools, and
        # Novita rejects our payloads with opaque 400s.
        {"id": "deepseek/deepseek-v4-flash",   "name": "DeepSeek 4 Flash",    "tier": "cheap",    "quality": "good",    "tag": "Recommended"},
        {"id": "qwen/qwen3.7-flash",           "name": "Qwen 3.7 Flash",     "tier": "cheap",    "quality": "good",    "tag": "Cheapest"},
        {"id": "qwen/qwen3.6-plus",            "name": "Qwen 3.6 Plus",      "tier": "cheap",    "quality": "good",    "tag": "Value"},
        {"id": "anthropic/claude-haiku-4.5",   "name": "Claude Haiku 4.5",   "tier": "budget",   "quality": "good",    "tag": "Reliable"},
        {"id": "openai/gpt-4.1-mini",         "name": "GPT-4.1 Mini",       "tier": "budget",   "quality": "good",    "tag": ""},
        {"id": "google/gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite", "tier": "budget", "quality": "good",   "tag": "New"},
        {"id": "google/gemini-2.5-flash",     "name": "Gemini 2.5 Flash",   "tier": "budget",   "quality": "good",    "tag": ""},
        {"id": "google/gemini-2.5-flash-lite", "name": "Gemini Flash Lite",  "tier": "cheap",    "quality": "basic",   "tag": ""},
    ],
    # The product catalog includes native/BYOK models alongside the subset
    # that can use Vercel's image protocol.
    "image": [
        {"id": "openai/gpt-5-image-mini", "name": "GPT-5 Image Mini", "tier": "balanced", "quality": "high", "tag": ""},
        {"id": "google/gemini-3.1-flash-image-preview", "name": "Nano Banana 2", "tier": "balanced", "quality": "high", "tag": "New"},
        {"id": "openai/gpt-image-2", "name": "GPT Image 2", "tier": "premium", "quality": "highest", "tag": "Recommended"},
    ],
    # Keep native and OpenRouter voice choices available in addition to the
    # Vercel-compatible OpenAI speech models.
    "voice": [
        {"id": "google/gemini-3.1-flash-tts-preview", "name": "Gemini 3.1 Flash TTS", "tier": "budget", "quality": "high", "tag": "Recommended"},
        {"id": "zyphra/zonos-v0.1-hybrid", "name": "Zonos v0.1 Hybrid", "tier": "budget", "quality": "good", "tag": ""},
        {"id": "zyphra/zonos-v0.1-transformer", "name": "Zonos v0.1 Transformer", "tier": "budget", "quality": "good", "tag": ""},
        {"id": "sesame/csm-1b", "name": "Sesame CSM 1B", "tier": "budget", "quality": "good", "tag": "Conversational"},
        {"id": "openai/tts-1-hd", "name": "OpenAI TTS HD", "tier": "balanced", "quality": "high", "tag": "Recommended"},
        {"id": "openai/tts-1",    "name": "OpenAI TTS",    "tier": "budget",   "quality": "good", "tag": "Fast"},
    ],
    "audio": [
        {"id": "suno/music", "name": "Suno Music", "tier": "premium", "quality": "highest", "tag": "Full song"},
        {"id": "google/lyria-3-clip-preview", "name": "Lyria 3 Clip", "tier": "balanced", "quality": "high", "tag": "Recommended"},
        {"id": "google/lyria-3-pro-preview", "name": "Lyria 3 Pro", "tier": "premium", "quality": "highest", "tag": "Full song"},
        {"id": "openai/gpt-audio-mini", "name": "GPT Audio Mini", "tier": "budget", "quality": "good", "tag": "Flexible"},
        {"id": "openai/gpt-audio", "name": "GPT Audio", "tier": "balanced", "quality": "high", "tag": ""},
    ],
    "sfx": [
        {"id": "openai/gpt-audio-mini", "name": "GPT Audio Mini", "tier": "budget", "quality": "good", "tag": "Recommended"},
        {"id": "openai/gpt-audio", "name": "GPT Audio", "tier": "balanced", "quality": "high", "tag": ""},
    ],
    # Retain the existing Vercel transcription choices and restore the native
    # timestamp/audio entries from the last stable product catalog.
    "stt": [
        {
            "id": "openai/whisper-1",
            "name": "OpenAI Whisper",
            "tier": "budget",
            "quality": "high",
            "tag": "Timestamped",
            "capabilities": {
                "segment_timestamps": True,
                "alignment_compatible": True,
                "route": "audio_transcriptions",
            },
        },
        {
            "id": "groq/whisper-large-v3",
            "name": "Groq Whisper Large v3",
            "tier": "budget",
            "quality": "high",
            "tag": "Timestamped + fast",
            "capabilities": {
                "segment_timestamps": True,
                "alignment_compatible": True,
                "route": "audio_transcriptions",
            },
        },
        {
            "id": "openai/gpt-4o-audio-preview",
            "name": "GPT-4o Audio",
            "tier": "balanced",
            "quality": "highest",
            "tag": "No timestamps",
            "capabilities": {
                "segment_timestamps": False,
                "alignment_compatible": False,
                "route": "chat_audio",
            },
        },
        {
            "id": "openai/gpt-audio-mini",
            "name": "GPT Audio Mini",
            "tier": "budget",
            "quality": "good",
            "tag": "No timestamps",
            "capabilities": {
                "segment_timestamps": False,
                "alignment_compatible": False,
                "route": "chat_audio",
            },
        },
        {
            "id": "openai/gpt-audio",
            "name": "GPT Audio",
            "tier": "balanced",
            "quality": "high",
            "tag": "No timestamps",
            "capabilities": {
                "segment_timestamps": False,
                "alignment_compatible": False,
                "route": "chat_audio",
            },
        },
        {
            "id": "openai/gpt-4o-mini-transcribe",
            "name": "GPT-4o Mini Transcribe",
            "tier": "budget",
            "quality": "good",
            "tag": "Fast",
            "capabilities": {
                "segment_timestamps": False,
                "alignment_compatible": False,
                "route": "transcription_model",
            },
        },
        {
            "id": "openai/gpt-4o-transcribe",
            "name": "GPT-4o Transcribe",
            "tier": "balanced",
            "quality": "highest",
            "tag": "Accurate",
            "capabilities": {
                "segment_timestamps": False,
                "alignment_compatible": False,
                "route": "transcription_model",
            },
        },
    ],
    # Keep native/BYOK video choices in addition to Vercel-compatible Seedance.
    "video": [
        {"id": "bytedance/seedance-2.5",     "name": "Seedance 2.5",     "tier": "premium", "quality": "highest", "tag": "New"},
        {"id": "bytedance/seedance-2.0",     "name": "Seedance 2.0",     "tier": "balanced", "quality": "high",    "tag": "Recommended"},
        {"id": "bytedance/seedance-2.0-fast", "name": "Seedance 2.0 Fast","tier": "budget",  "quality": "good",    "tag": "Fast"},
        {"id": "bytedance/seedance-v1.0-pro-fast", "name": "Seedance v1.0 Pro Fast", "tier": "cheap", "quality": "good", "tag": "Cheapest"},
        {"id": "kwaivgi/kling-v3.0-std", "name": "Kling v3.0 Standard", "tier": "balanced", "quality": "high", "tag": "New"},
        {"id": "kwaivgi/kling-v3.0-pro", "name": "Kling v3.0 Pro", "tier": "premium", "quality": "highest", "tag": "HQ"},
        {"id": "atlascloud/wan-2.2-turbo-spicy", "name": "Wan 2.2 Turbo I2V (Atlas)", "tier": "cheap", "quality": "basic", "tag": "Image required · BYOK"},
    ],
    "embedding": [
        {
            "id": "mxbai-embed-large",
            "name": "MxBAI Embed (Local)",
            "tier": "free",
            "quality": "high",
            "tag": "Bundled",
            "deployment": "local",
            "byok": False,
        },
        {
            "id": "openai/text-embedding-3-small",
            "name": "OpenAI Embedding 3 Small",
            "tier": "budget",
            "quality": "high",
            "tag": "Managed",
        },
    ],
}


# This describes verified native video understanding, not whether Manor has
# attached a video to the current request. None means unverified, not unsupported.
# Video generation is a separate role.
for _chat_role in ("primary", "worker"):
    for _chat_model in CATALOG[_chat_role]:
        _modalities = model_input_modalities(_chat_model["id"])
        _chat_model.setdefault("capabilities", {})["video_input"] = (
            True if _modalities and "video" in _modalities else None
        )


# These roles only accept exact product-catalog selections. Their BYOK
# settings reuse a catalog model ID with different credentials; unlike the
# general LLM/embedding roles, they do not support arbitrary custom model IDs.
# Keeping the constraint here also makes preferences saved by an older catalog
# safely fall through to the current default instead of routing a removed model.
CATALOG_ONLY_ROLES = frozenset({"image", "voice", "audio", "sfx", "stt", "video"})

# Text-completion roles whose models run the agent loop. Every model they
# list must have a context window below (guard-tested).
CONTEXT_WINDOW_ROLES = ("primary", "worker")

# Input context window in tokens per catalog model: the smallest value across
# the gateways a call can route through (Vercel ``context_window``, OpenRouter
# ``context_length`` / ``top_provider.context_length``), snapshot 2026-09-30.
# The pricing syncs refresh live values into their caches; lookups take the
# smaller of this snapshot and the live data
# (``packages/core/services/model_context_window.py``).
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "anthropic/claude-fable-5": 1_000_000,
    "anthropic/claude-fable-5.1": 1_000_000,
    "anthropic/claude-haiku-4.5": 200_000,
    "anthropic/claude-opus-4.6": 1_000_000,
    "anthropic/claude-opus-4.7": 1_000_000,
    "anthropic/claude-opus-5": 1_000_000,
    "anthropic/claude-opus-5.5": 1_000_000,
    "anthropic/claude-sonnet-4.6": 1_000_000,
    "deepseek/deepseek-v4-flash": 1_000_000,
    "deepseek/deepseek-v4-pro": 1_000_000,
    "google/gemini-2.5-flash": 1_000_000,
    "google/gemini-2.5-flash-lite": 1_048_576,
    "google/gemini-2.5-pro": 1_048_576,
    "google/gemini-3.5-flash-lite": 1_000_000,
    "google/gemini-3.6-flash": 1_000_000,
    "moonshotai/kimi-k2.6": 262_000,
    "moonshotai/kimi-k3": 1_000_000,
    "openai/gpt-4.1": 1_047_576,
    "openai/gpt-4.1-mini": 1_047_576,
    "openai/gpt-4o": 128_000,
    "openai/gpt-5.5": 1_000_000,
    "openai/gpt-5.5-pro": 1_000_000,
    "openai/gpt-5.6-luna": 1_050_000,
    "openai/gpt-5.6-sol": 1_050_000,
    "openai/gpt-5.6-terra": 1_050_000,
    "openai/gpt-6-astra": 1_050_000,
    "qwen/qwen3.6-plus": 1_000_000,
    "qwen/qwen3.7-flash": 991_000,
    "qwen/qwen3.8-max": 262_144,
}


def model_preference_is_available(
    role: str,
    model_id: object,
    *,
    disabled_models: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Return whether a stored preference is valid for the current catalog."""

    normalized = str(model_id or "").strip()
    if not normalized or normalized in disabled_models:
        return False
    if role not in CATALOG_ONLY_ROLES:
        return True
    return any(
        str(item.get("id") or "").strip() == normalized
        for item in CATALOG.get(role, [])
    )


VIDEO_MODEL_CAPABILITIES = {
    "bytedance/seedance-2.5": {
        "text_to_video": True,
        "requires_first_frame": False,
        "first_frame": True,
        "last_frame": True,
        "reference_images": True,
        "max_reference_images": 30,
        "reference_videos": True,
        "max_reference_videos": 10,
        "max_reference_video_total_seconds": 30,
        "native_audio": True,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": True,
        "max_audio_references": 10,
        "max_audio_reference_total_seconds": 30,
        "audio_requires_visual_reference": False,
        "generation_schema": {
            "duration": {
                "text_to_video": {"type": "integer", "minimum": 4, "maximum": 30, "default": 5},
                "image_to_video": {"type": "integer", "minimum": 4, "maximum": 30, "default": 5},
            }
        },
    },
    "bytedance/seedance-2.0": {
        "text_to_video": True,
        "requires_first_frame": False,
        "first_frame": True,
        "last_frame": True,
        "reference_images": True,
        "max_reference_images": 9,
        "reference_videos": True,
        "max_reference_videos": 3,
        "native_audio": True,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": True,
        "max_audio_references": 3,
    },
    "bytedance/seedance-2.0-fast": {
        "text_to_video": True,
        "requires_first_frame": False,
        "first_frame": True,
        "last_frame": True,
        "reference_images": True,
        "max_reference_images": 9,
        "reference_videos": True,
        "max_reference_videos": 3,
        "native_audio": True,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": True,
        "max_audio_references": 3,
    },
    "bytedance/seedance-v1.0-pro-fast": {
        "text_to_video": True,
        "requires_first_frame": False,
        "first_frame": True,
        "last_frame": False,
        "reference_images": False,
        "max_reference_images": 0,
        "reference_videos": False,
        "max_reference_videos": 0,
        "native_audio": False,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": False,
        "max_audio_references": 0,
        "generation_schema": {
            "duration": {
                "image_to_video": {
                    "type": "integer",
                    "enum": [4, 5, 6, 7, 8, 9, 10, 11, 12],
                    "default": 5,
                }
            }
        },
    },
    "kwaivgi/kling-v3.0-std": {
        "text_to_video": True,
        "requires_first_frame": False,
        "first_frame": True,
        "last_frame": False,
        "reference_images": False,
        "max_reference_images": 0,
        "reference_videos": False,
        "max_reference_videos": 0,
        "native_audio": False,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": False,
        "max_audio_references": 0,
    },
    "kwaivgi/kling-v3.0-pro": {
        "text_to_video": True,
        "requires_first_frame": False,
        "first_frame": True,
        "last_frame": False,
        "reference_images": False,
        "max_reference_images": 0,
        "reference_videos": False,
        "max_reference_videos": 0,
        "native_audio": False,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": False,
        "max_audio_references": 0,
    },
    "atlascloud/wan-2.2-turbo-spicy": {
        "text_to_video": False,
        "requires_first_frame": True,
        "first_frame": True,
        "last_frame": False,
        "reference_images": False,
        "max_reference_images": 0,
        "reference_videos": False,
        "max_reference_videos": 0,
        "native_audio": False,
        "native_dialogue": False,
        "native_narration": False,
        "native_subtitles": False,
        "audio_reference": False,
        "max_audio_references": 0,
    },
}

DEFAULT_VIDEO_MODEL_CAPABILITIES = {
    "text_to_video": True,
    "requires_first_frame": False,
    "first_frame": True,
    "last_frame": True,
    "reference_images": True,
    "max_reference_images": 5,
    "reference_videos": False,
    "max_reference_videos": 0,
    "native_audio": False,
    "native_dialogue": False,
    "native_narration": False,
    "native_subtitles": False,
    "audio_reference": False,
    "max_audio_references": 0,
}


def video_model_capabilities(model: str | None) -> dict:
    """Return Manor's known feature support for a video generation model."""
    model_id = str(model or "").strip()
    if model_id in VIDEO_MODEL_CAPABILITIES:
        return dict(VIDEO_MODEL_CAPABILITIES[model_id])
    lowered = model_id.lower()
    if lowered.startswith("bytedance/seedance"):
        return dict(VIDEO_MODEL_CAPABILITIES["bytedance/seedance-2.0"])
    if lowered.startswith("kwaivgi/kling"):
        return dict(VIDEO_MODEL_CAPABILITIES["kwaivgi/kling-v3.0-std"])
    return dict(DEFAULT_VIDEO_MODEL_CAPABILITIES)


for _video_model in CATALOG.get("video", []):
    _video_model.setdefault("capabilities", video_model_capabilities(_video_model.get("id")))


def env_configured_primary_model(role: str = "primary") -> str:
    """Model pinned by ``OPENROUTER_MODEL`` / ``LLM_MODEL``, or ``""``.

    Only the ``primary`` role honours these (kept for backwards compat).
    Exposed so admin surfaces can *show* what the deployment has pinned
    instead of silently disagreeing with the picker.
    """
    if role != "primary":
        return ""
    return (os.getenv("OPENROUTER_MODEL") or os.getenv("LLM_MODEL") or "").strip()


def resolve_model_for_role(
    role: str = "primary",
    user_prefs: dict | None = None,
    entity_settings: dict | None = None,
    platform_settings: dict | None = None,
) -> str:
    """Resolve model for a given role.

    Priority: user_prefs.models.{role} > entity_settings.models.{role}
              > platform default override
              > env LLM_MODEL (for primary only) > DEFAULTS[role]

    ``platform_settings`` is the normalized admin document from
    ``services.model_settings`` — a user/entity preference pointing at
    an admin-disabled model is skipped so the resolution falls through
    to the platform default.

    The admin override deliberately outranks the env var: an operator
    picking a default in the admin portal is an explicit, audited act,
    while ``LLM_MODEL`` is a static deployment setting. With the old
    order the portal reported a default it could not actually deliver.
    Deployments that never set an override see unchanged env behavior.
    """
    disabled = set(
        ((platform_settings or {}).get("disabled_models") or {}).get(role) or []
    )

    # User preference
    if user_prefs:
        m = (user_prefs.get("models") or {}).get(role)
        if model_preference_is_available(role, m, disabled_models=disabled):
            return str(m).strip()

    # Entity setting
    if entity_settings:
        m = (entity_settings.get("models") or {}).get(role)
        if model_preference_is_available(role, m, disabled_models=disabled):
            return str(m).strip()

    # Platform admin default override
    override = ((platform_settings or {}).get("default_overrides") or {}).get(role)
    if override and str(override).strip():
        return str(override).strip()

    # Env var (primary only, backwards compat)
    if env := env_configured_primary_model(role):
        return env

    return DEFAULTS.get(role, DEFAULTS["primary"])
