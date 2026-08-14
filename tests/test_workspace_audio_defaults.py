from packages.core.services.workspace_audio_defaults import (
    DEFAULT_WORKSPACE_AUDIO_LANGUAGE,
    normalize_workspace_audio_language,
    normalize_workspace_audio_settings,
    workspace_audio_language,
)


def test_workspace_audio_language_normalizes_common_tags():
    assert normalize_workspace_audio_language("zh") == "zh-CN"
    assert normalize_workspace_audio_language("pt_BR") == "pt-BR"
    assert normalize_workspace_audio_language("ja-JP") == "ja-JP"
    assert normalize_workspace_audio_language("not a language") == DEFAULT_WORKSPACE_AUDIO_LANGUAGE


def test_workspace_audio_settings_preserves_peer_settings():
    settings = {
        "runtime_learning": {"enabled": True},
        "audio_defaults": {"language": "zh", "voice_speed": 1.0},
    }

    normalized = normalize_workspace_audio_settings(settings)

    assert normalized == {
        "runtime_learning": {"enabled": True},
        "audio_defaults": {"language": "zh-CN", "voice_speed": 1.0},
    }
    assert settings["audio_defaults"]["language"] == "zh"
    assert workspace_audio_language(normalized) == "zh-CN"


def test_workspace_audio_language_defaults_without_configuration():
    assert workspace_audio_language(None) == DEFAULT_WORKSPACE_AUDIO_LANGUAGE
    assert normalize_workspace_audio_settings(None) == {}
