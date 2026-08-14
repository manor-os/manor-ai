from packages.core.constants.models import CATALOG, DEFAULTS, resolve_model_for_role
from packages.core.services.model_resolver import _resolve_entity_scoped_model


def test_media_catalog_restores_the_recent_stable_model_choices():
    """Provider transport must not silently shrink the product catalog."""
    assert DEFAULTS["voice"] == "google/gemini-3.1-flash-tts-preview"
    assert DEFAULTS["audio"] == "google/lyria-3-clip-preview"
    assert DEFAULTS["sfx"] == "openai/gpt-audio-mini"
    assert DEFAULTS["stt"] == "openai/gpt-4o-audio-preview"

    assert {
        "openai/gpt-5-image-mini",
        "google/gemini-3.1-flash-image-preview",
        "openai/gpt-image-2",
    }.issubset({item["id"] for item in CATALOG["image"]})
    assert {
        "google/gemini-3.1-flash-tts-preview",
        "zyphra/zonos-v0.1-hybrid",
        "zyphra/zonos-v0.1-transformer",
        "sesame/csm-1b",
    }.issubset({item["id"] for item in CATALOG["voice"]})
    assert {
        "google/lyria-3-clip-preview",
        "google/lyria-3-pro-preview",
        "openai/gpt-audio-mini",
        "openai/gpt-audio",
    }.issubset({item["id"] for item in CATALOG["audio"]})
    assert {
        "openai/gpt-audio-mini",
        "openai/gpt-audio",
    }.issubset({item["id"] for item in CATALOG["sfx"]})
    assert {
        "groq/whisper-large-v3",
        "openai/gpt-4o-audio-preview",
        "openai/gpt-audio-mini",
        "openai/gpt-audio",
    }.issubset({item["id"] for item in CATALOG["stt"]})
    assert {
        "kwaivgi/kling-v3.0-std",
        "kwaivgi/kling-v3.0-pro",
        "atlascloud/wan-2.2-turbo-spicy",
    }.issubset({item["id"] for item in CATALOG["video"]})
    assert "openai/gpt-4" in {item["id"] for item in CATALOG["worker"]}


def test_audio_roles_keep_the_product_catalog_models():
    assert DEFAULTS["voice"] == "google/gemini-3.1-flash-tts-preview"
    assert DEFAULTS["audio"] == "google/lyria-3-clip-preview"
    assert DEFAULTS["sfx"] == "openai/gpt-audio-mini"

    voice_ids = {item["id"] for item in CATALOG["voice"]}
    audio_ids = {item["id"] for item in CATALOG["audio"]}
    sfx_ids = {item["id"] for item in CATALOG["sfx"]}

    assert DEFAULTS["voice"] in voice_ids
    assert DEFAULTS["audio"] in audio_ids
    assert DEFAULTS["sfx"] in sfx_ids
    assert {"openai/tts-1", "openai/tts-1-hd"}.issubset(voice_ids)
    assert {"google/lyria-3-clip-preview", "google/lyria-3-pro-preview"}.issubset(audio_ids)
    assert {"openai/gpt-audio-mini", "openai/gpt-audio"}.issubset(sfx_ids)


def test_restored_media_preferences_remain_available():
    legacy_preferences = {
        "models": {
            "voice": "zyphra/zonos-v0.1-hybrid",
            "audio": "google/lyria-3-clip-preview",
        }
    }

    assert resolve_model_for_role("voice", legacy_preferences) == "zyphra/zonos-v0.1-hybrid"
    assert resolve_model_for_role("audio", legacy_preferences) == "google/lyria-3-clip-preview"

    assert (
        _resolve_entity_scoped_model(
            "voice",
            entity_settings=legacy_preferences,
            owner_prefs=None,
            platform_settings={},
        )
        == "zyphra/zonos-v0.1-hybrid"
    )


def test_catalog_media_preferences_are_still_honored():
    preferences = {"models": {"voice": "openai/tts-1"}}

    assert resolve_model_for_role("voice", preferences) == "openai/tts-1"


def test_stt_catalog_exposes_vercel_transcription_models():
    stt_models = {item["id"]: item for item in CATALOG["stt"]}

    assert DEFAULTS["stt"] in stt_models
    assert stt_models["openai/whisper-1"]["capabilities"] == {
        "segment_timestamps": True,
        "alignment_compatible": True,
        "route": "audio_transcriptions",
    }
    assert stt_models["openai/gpt-4o-mini-transcribe"]["capabilities"] == {
        "segment_timestamps": False,
        "alignment_compatible": False,
        "route": "transcription_model",
    }
