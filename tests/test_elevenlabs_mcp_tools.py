from packages.core.ai.mcp import elevenlabs
from packages.core.ai.tools import mcp_builtin


def test_elevenlabs_exposes_dedicated_audio_tools():
    names = {tool["name"] for tool in elevenlabs.list_tools()}

    assert {
        "text_to_speech",
        "text_to_dialogue",
        "generate_sound_effect",
        "compose_music",
        "list_voices",
    } <= names


def test_mcp_builtin_catalog_exposes_elevenlabs_audio_tools():
    names = {tool["name"] for tool in mcp_builtin._SERVER_TOOL_SCHEMAS["elevenlabs"]}

    assert {
        "text_to_dialogue",
        "generate_sound_effect",
        "compose_music",
    } <= names
