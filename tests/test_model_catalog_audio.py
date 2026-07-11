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
