from __future__ import annotations

import json

import httpx
import pytest


class _Response:
    status_code = 200
    text = ""
    content = b"fake-mp3"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"voices": []}


class _Client:
    calls: list[dict] = []
    response: _Response = _Response()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.response

    async def get(self, url: str, *, headers: dict):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self.response


@pytest.fixture
def elevenlabs_http(monkeypatch):
    from packages.core.ai.mcp import elevenlabs

    _Client.calls = []
    _Client.response = _Response()
    monkeypatch.setattr(elevenlabs.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        elevenlabs,
        "_save_audio_bytes",
        _fake_save_audio_bytes,
    )
    return elevenlabs


async def _fake_save_audio_bytes(_audio_bytes: bytes, _filename_hint: str):
    from pathlib import Path

    return Path("/tmp/elevenlabs-test.mp3"), "elevenlabs-test.mp3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stability", -0.01),
        ("stability", 1.01),
        ("similarity_boost", -0.01),
        ("similarity_boost", 1.01),
    ],
)
async def test_text_to_speech_rejects_out_of_range_voice_settings_without_http(
    elevenlabs_http,
    field,
    value,
):
    result = await elevenlabs_http.call_tool(
        "text_to_speech",
        {"text": "hello", field: value},
        "el-test-key",
    )

    assert result["isError"] is True
    assert field in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_seconds", 0.49),
        ("duration_seconds", 30.01),
        ("prompt_influence", -0.01),
        ("prompt_influence", 1.01),
    ],
)
async def test_sound_effect_rejects_out_of_range_options_without_http(
    elevenlabs_http,
    field,
    value,
):
    result = await elevenlabs_http.call_tool(
        "generate_sound_effect",
        {"text": "rain", field: value},
        "el-test-key",
    )

    assert result["isError"] is True
    assert field in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2999, 600001])
async def test_compose_music_rejects_out_of_range_length_without_http(
    elevenlabs_http,
    value,
):
    result = await elevenlabs_http.call_tool(
        "compose_music",
        {"prompt": "quiet piano", "music_length_ms": value},
        "el-test-key",
    )

    assert result["isError"] is True
    assert "music_length_ms" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_compose_music_rejects_fractional_integer_length_without_http(elevenlabs_http):
    result = await elevenlabs_http.call_tool(
        "compose_music",
        {"prompt": "quiet piano", "music_length_ms": 3000.5},
        "el-test-key",
    )

    assert result["isError"] is True
    assert "music_length_ms" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_text_to_speech_uses_xi_api_key_and_registers_audio_result(elevenlabs_http):
    result = await elevenlabs_http.call_tool(
        "text_to_speech",
        {"text": "hello", "stability": 0.25, "similarity_boost": 0.8},
        "el-test-key",
    )

    assert result["isError"] is False
    call = _Client.calls[0]
    assert call["headers"]["xi-api-key"] == "el-test-key"
    assert call["headers"]["Accept"] == "audio/mpeg"
    assert call["json"]["voice_settings"] == {
        "stability": 0.25,
        "similarity_boost": 0.8,
    }
    payload = json.loads(result["content"][0]["text"])
    assert payload["filename"] == "elevenlabs-test.mp3"
    assert payload["bytes"] == len(b"fake-mp3")


@pytest.mark.asyncio
async def test_text_to_dialogue_rejects_missing_voice_without_http(elevenlabs_http):
    result = await elevenlabs_http.call_tool(
        "text_to_dialogue",
        {"inputs": [{"text": "hello", "voice_id": "  "}]},
        "el-test-key",
    )

    assert result["isError"] is True
    assert "voice_id" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_provider_http_error_is_returned_as_mcp_error(monkeypatch):
    from packages.core.ai.mcp import elevenlabs

    class _ErrorResponse(_Response):
        status_code = 401
        text = "invalid api key"

        def raise_for_status(self) -> None:
            response = httpx.Response(
                401,
                request=httpx.Request("GET", "https://api.elevenlabs.io/v1/voices"),
                text=self.text,
            )
            raise httpx.HTTPStatusError("unauthorized", request=response.request, response=response)

    class _ErrorClient(_Client):
        response = _ErrorResponse()

    monkeypatch.setattr(elevenlabs.httpx, "AsyncClient", _ErrorClient)
    result = await elevenlabs.call_tool("list_voices", {}, "el-test-key")

    assert result["isError"] is True
    assert "401" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_elevenlabs_rejects_blank_api_key_without_http(monkeypatch):
    from packages.core.ai.mcp import elevenlabs

    _Client.calls = []
    monkeypatch.setattr(elevenlabs.httpx, "AsyncClient", _Client)

    result = await elevenlabs.call_tool("list_voices", {}, "   ")

    assert result["isError"] is True
    assert "api key" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_elevenlabs_rejects_non_string_api_key_without_http(monkeypatch):
    from packages.core.ai.mcp import elevenlabs

    _Client.calls = []
    monkeypatch.setattr(elevenlabs.httpx, "AsyncClient", _Client)

    result = await elevenlabs.call_tool("list_voices", {}, {"api_key": "el-test-key"})

    assert result["isError"] is True
    assert "api key" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_elevenlabs_rejects_non_object_arguments_before_http(elevenlabs_http):
    result = await elevenlabs_http.call_tool("list_voices", [], "el-test-key")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("text_to_speech", {"text": 123}, "text"),
        ("generate_sound_effect", {"text": "rain", "loop": "false"}, "loop"),
        (
            "text_to_dialogue",
            {"inputs": [{"text": 123, "voice_id": "voice-1"}]},
            "text",
        ),
    ],
)
async def test_elevenlabs_rejects_invalid_argument_types_before_http(
    elevenlabs_http, tool, arguments, field
):
    result = await elevenlabs_http.call_tool(tool, arguments, "el-test-key")

    assert result["isError"] is True
    assert field in result["content"][0]["text"]
    assert _Client.calls == []
