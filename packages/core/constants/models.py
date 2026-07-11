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
    "primary":   "anthropic/claude-sonnet-4.6",
    # ``worker`` is used by TaskRunner for tool-using agent tasks. The
    # previous default (``deepseek/deepseek-chat``) only had Novita as
    # a tool-capable provider on OpenRouter, and Novita rejects our
    # payloads with opaque ``invalid_request_error`` 400s. Once we
    # exclude Novita (see ``_openrouter_provider_block`` in
    # llm_client.py) deepseek-chat returns 404 ``no endpoints support
    # tool use``. ``openai/gpt-4o-mini`` is similarly cheap, multi-
    # provider, and reliably tool-capable.
    "worker":    "openai/gpt-4o-mini",
    # Image default matches the only model currently routed by the
    # generate_image handler (OpenRouter chat/completions image format).
    "image":     "openai/gpt-5-image-mini",
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

# ── Model catalog (shown in user settings) ──
CATALOG = {
    # Inclusion rule: only models served by their own lab (Anthropic /
    # OpenAI / Google). Those go through the lab's own infra on
    # OpenRouter, so tool-calling is end-to-end consistent and there's
    # no provider-routing surprise. Third-party-hosted open-weight
    # models (DeepSeek, Moonshot, Mistral via X, etc.) keep failing
    # with opaque provider errors — see scripts/validate_models.py if
    # you want to re-evaluate any of them empirically.
    "primary": [
        {"id": "anthropic/claude-sonnet-4.6",  "name": "Claude Sonnet 4.6",  "tier": "balanced", "quality": "high",    "tag": "Recommended"},
        {"id": "anthropic/claude-fable-5",     "name": "Claude Fable 5",     "tier": "premium",  "quality": "highest", "tag": "Most capable"},
        {"id": "anthropic/claude-opus-4.7",    "name": "Claude Opus 4.7",    "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "openai/gpt-5.5",               "name": "GPT-5.5",            "tier": "premium",  "quality": "highest", "tag": "New"},
        {"id": "openai/gpt-5.5-pro",           "name": "GPT-5.5 Pro",        "tier": "premium",  "quality": "highest", "tag": "Pro"},
        {"id": "anthropic/claude-opus-4.6",    "name": "Claude Opus 4.6",    "tier": "premium",  "quality": "highest", "tag": ""},
        {"id": "anthropic/claude-haiku-4.5",   "name": "Claude Haiku 4.5",   "tier": "budget",   "quality": "good",    "tag": "Fast"},
        {"id": "moonshotai/kimi-k2.6",         "name": "Kimi K2.6",          "tier": "premium",  "quality": "highest", "tag": "New"},
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
        {"id": "openai/gpt-4o-mini",          "name": "GPT-4o Mini",        "tier": "cheap",    "quality": "good",    "tag": "Recommended"},
        {"id": "qwen/qwen3.6-plus",            "name": "Qwen 3.6 Plus",      "tier": "cheap",    "quality": "good",    "tag": "Value"},
        {"id": "deepseek/deepseek-v4-flash",   "name": "DeepSeek 4 Flash",    "tier": "cheap",    "quality": "good",    "tag": "Fast"},
        {"id": "anthropic/claude-haiku-4.5",   "name": "Claude Haiku 4.5",   "tier": "budget",   "quality": "good",    "tag": "Reliable"},
        {"id": "openai/gpt-4.1-mini",         "name": "GPT-4.1 Mini",       "tier": "budget",   "quality": "good",    "tag": ""},
        {"id": "google/gemini-2.5-flash",     "name": "Gemini 2.5 Flash",   "tier": "budget",   "quality": "good",    "tag": ""},
        {"id": "google/gemini-2.5-flash-lite", "name": "Gemini Flash Lite",  "tier": "cheap",    "quality": "basic",   "tag": "Cheapest"},
    ],
    # Image — OpenRouter keys use OpenRouter's chat/completions image
    # format; native OpenAI/Google keys route to their first-party image APIs.
    "image": [
        {"id": "openai/gpt-5-image-mini", "name": "GPT-5 Image Mini", "tier": "balanced", "quality": "high", "tag": "Recommended"},
        {"id": "google/gemini-3.1-flash-image-preview", "name": "Nano Banana 2", "tier": "balanced", "quality": "high", "tag": "New"},
        {"id": "openai/gpt-5.4-image-2", "name": "GPT Image 2", "tier": "premium", "quality": "highest", "tag": "Pro"},
    ],
    # Voice (TTS) — Gemini TTS can route through native Google keys when
    # BYOK/platform Google credentials are available; otherwise models route
    # through Manor's official OpenRouter audio path.
    "voice": [
        {"id": "google/gemini-3.1-flash-tts-preview", "name": "Gemini 3.1 Flash TTS", "tier": "budget", "quality": "high", "tag": "Recommended"},
        {"id": "zyphra/zonos-v0.1-hybrid",            "name": "Zonos v0.1 Hybrid",    "tier": "budget", "quality": "good", "tag": ""},
        {"id": "zyphra/zonos-v0.1-transformer",       "name": "Zonos v0.1 Transformer","tier": "budget", "quality": "good", "tag": ""},
        {"id": "sesame/csm-1b",                        "name": "Sesame CSM 1B",        "tier": "budget", "quality": "good", "tag": "Conversational"},
    ],
    # Audio generation — music and score models. Lyria is deliberately kept
    # out of SFX routing because it tends to turn impacts/explosions into music.
    "audio": [
        {"id": "google/lyria-3-clip-preview", "name": "Lyria 3 Clip", "tier": "balanced", "quality": "high", "tag": "Recommended"},
        {"id": "google/lyria-3-pro-preview",  "name": "Lyria 3 Pro",  "tier": "premium",  "quality": "highest", "tag": "Full song"},
        {"id": "openai/gpt-audio-mini",       "name": "GPT Audio Mini","tier": "budget",  "quality": "good", "tag": "Flexible"},
        {"id": "openai/gpt-audio",            "name": "GPT Audio",     "tier": "balanced","quality": "high", "tag": ""},
    ],
    # Sound design — ambience, Foley, discrete SFX, and transitions. Keep
    # music-specific generators out of the default picker for this role.
    "sfx": [
        {"id": "openai/gpt-audio-mini",       "name": "GPT Audio Mini","tier": "budget",  "quality": "good", "tag": "Recommended"},
        {"id": "openai/gpt-audio",            "name": "GPT Audio",     "tier": "balanced","quality": "high", "tag": ""},
    ],
    # STT — only chat-based audio models that work via OpenRouter.
    "stt": [
        {"id": "openai/gpt-4o-audio-preview",   "name": "GPT-4o Audio",    "tier": "balanced", "quality": "highest", "tag": "Recommended"},
        {"id": "openai/gpt-audio-mini",         "name": "GPT Audio Mini",  "tier": "budget",   "quality": "good",    "tag": "Fast + cheap"},
        {"id": "openai/gpt-audio",              "name": "GPT Audio",       "tier": "balanced", "quality": "high",    "tag": ""},
    ],
    # Video — OpenRouter keys use /api/v1/videos; native Seedance/Kling
    # keys route to their provider task APIs.
    "video": [
        {"id": "bytedance/seedance-2.0",     "name": "Seedance 2.0",     "tier": "balanced", "quality": "high",    "tag": "Recommended"},
        {"id": "bytedance/seedance-2.0-fast", "name": "Seedance 2.0 Fast","tier": "budget",  "quality": "good",    "tag": "Fast"},
        {"id": "kwaivgi/kling-v3.0-std",     "name": "Kling v3.0 Standard","tier": "balanced", "quality": "high",  "tag": "New"},
        {"id": "kwaivgi/kling-v3.0-pro",     "name": "Kling v3.0 Pro",   "tier": "premium",  "quality": "highest", "tag": "HQ"},
    ],
    "embedding": [
        {"id": "mxbai-embed-large",      "name": "MxBAI Embed (Local)",   "tier": "free",     "quality": "high",    "tag": "Bundled"},
    ],
}


VIDEO_MODEL_CAPABILITIES = {
    "bytedance/seedance-2.0": {
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
    "kwaivgi/kling-v3.0-std": {
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


def resolve_model_for_role(
    role: str = "primary",
    user_prefs: dict | None = None,
    entity_settings: dict | None = None,
    platform_settings: dict | None = None,
) -> str:
    """Resolve model for a given role.

    Priority: user_prefs.models.{role} > entity_settings.models.{role}
              > env LLM_MODEL (for primary only)
              > platform default override > DEFAULTS[role]

    ``platform_settings`` is the normalized admin document from
    ``services.model_settings`` — a user/entity preference pointing at
    an admin-disabled model is skipped so the resolution falls through
    to the platform default.
    """
    disabled = set(
        ((platform_settings or {}).get("disabled_models") or {}).get(role) or []
    )

    # User preference
    if user_prefs:
        m = (user_prefs.get("models") or {}).get(role)
        if m and str(m).strip() and str(m).strip() not in disabled:
            return str(m).strip()

    # Entity setting
    if entity_settings:
        m = (entity_settings.get("models") or {}).get(role)
        if m and str(m).strip() and str(m).strip() not in disabled:
            return str(m).strip()

    # Env var (primary only, backwards compat)
    if role == "primary":
        env = (os.getenv("OPENROUTER_MODEL") or os.getenv("LLM_MODEL") or "").strip()
        if env:
            return env

    # Platform admin default override
    override = ((platform_settings or {}).get("default_overrides") or {}).get(role)
    if override and str(override).strip():
        return str(override).strip()

    return DEFAULTS.get(role, DEFAULTS["primary"])
