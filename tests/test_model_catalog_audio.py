from packages.core.constants.models import CATALOG, DEFAULTS


def test_openrouter_audio_roles_are_in_catalog():
    assert DEFAULTS["voice"] == "google/gemini-3.1-flash-tts-preview"
    assert DEFAULTS["audio"] == "google/lyria-3-clip-preview"
    assert DEFAULTS["sfx"] == "openai/gpt-audio-mini"

    voice_ids = {item["id"] for item in CATALOG["voice"]}
    audio_ids = {item["id"] for item in CATALOG["audio"]}
    sfx_ids = {item["id"] for item in CATALOG["sfx"]}

    assert DEFAULTS["voice"] in voice_ids
    assert DEFAULTS["audio"] in audio_ids
    assert "openai/gpt-audio-mini" in audio_ids
    assert DEFAULTS["sfx"] in sfx_ids
    assert "google/lyria-3-clip-preview" not in sfx_ids


def test_stt_catalog_exposes_timestamp_capable_native_whisper_routes():
    stt_models = {item["id"]: item for item in CATALOG["stt"]}

    assert DEFAULTS["stt"] in stt_models
    assert stt_models["openai/whisper-1"]["capabilities"] == {
        "segment_timestamps": True,
        "alignment_compatible": True,
        "route": "audio_transcriptions",
    }
    assert stt_models["groq/whisper-large-v3"]["capabilities"] == {
        "segment_timestamps": True,
        "alignment_compatible": True,
        "route": "audio_transcriptions",
    }
    assert stt_models["openai/gpt-4o-audio-preview"]["capabilities"] == {
        "segment_timestamps": False,
        "alignment_compatible": False,
        "route": "chat_audio",
    }
