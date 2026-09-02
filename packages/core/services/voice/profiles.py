"""Provider-safe voice choices shared by browser calls and speech synthesis."""

from __future__ import annotations

from typing import Literal, cast

VoiceProfile = Literal["warm", "clear", "bright", "deep"]
VOICE_PROFILES: tuple[VoiceProfile, ...] = ("warm", "clear", "bright", "deep")
DEFAULT_VOICE_PROFILE: VoiceProfile = "warm"

_OPENAI_VOICES: dict[VoiceProfile, str] = {
    "warm": "marin",
    "clear": "cedar",
    "bright": "coral",
    "deep": "verse",
}
_OPENAI_LEGACY_TTS_VOICES: dict[VoiceProfile, str] = {
    "warm": "nova",
    "clear": "alloy",
    "bright": "shimmer",
    "deep": "onyx",
}
_GOOGLE_VOICES: dict[VoiceProfile, str] = {
    "warm": "Aoede",
    "clear": "Kore",
    "bright": "Zephyr",
    "deep": "Charon",
}


def normalize_voice_profile(value: object) -> VoiceProfile:
    profile = str(value or "").strip().lower()
    if profile not in VOICE_PROFILES:
        raise ValueError("Unsupported voice profile")
    return cast(VoiceProfile, profile)


def openai_voice(profile: VoiceProfile) -> str:
    return _OPENAI_VOICES[normalize_voice_profile(profile)]


def speech_voice(profile: VoiceProfile, model: str) -> str | None:
    """Return a concrete voice only when the selected provider supports it."""

    model_id = str(model or "").strip().lower()
    provider = model_id.split("/", 1)[0]
    normalized = normalize_voice_profile(profile)
    if provider == "google":
        return _GOOGLE_VOICES[normalized]
    if provider == "openai":
        native_model = model_id.split("/", 1)[-1]
        if native_model in {"tts-1", "tts-1-hd"}:
            return _OPENAI_LEGACY_TTS_VOICES[normalized]
        return _OPENAI_VOICES[normalized]
    # Other OpenRouter speech providers expose different, incompatible voice
    # identifiers. Let their adapter use its documented default.
    return None
