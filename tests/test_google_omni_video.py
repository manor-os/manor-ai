from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import packages.core.tasks.media_tasks as media_tasks
from packages.core.tasks import video_adapters

MODEL = "google/gemini-omni-1.1-flash"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _fake_client(captured: dict, response: _FakeResponse):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None, headers=None):
            captured.update(url=url, payload=json, headers=headers)
            return response

    return FakeAsyncClient


def _job(**params):
    return SimpleNamespace(
        id="job_omni",
        model=MODEL,
        prompt="A woman plays violin on a rooftop.",
        entity_id="entity",
        params={"duration": 8, "aspect_ratio": "9:16", "credential_source": "byok", **params},
        agent_id=None,
        conversation_id=None,
        user_id=None,
    )


def _runtime(captured: dict, response: _FakeResponse):
    runtime = media_tasks._video_adapter_runtime()
    return video_adapters.VideoAdapterRuntime(
        **{**runtime.__dict__, "http_client_cls": _fake_client(captured, response)},
    )


@pytest.mark.asyncio
async def test_omni_sends_frames_inline_and_saves_the_inline_video(monkeypatch):
    """Frames go inline with the user's key; the clip comes back as base64 and is saved
    from a data URL, so the key never reaches a URL taken from the response."""
    captured: dict = {}

    async def fake_inline(value, entity_id, *, max_bytes):
        return "data:image/png;base64,FRAME" if "first" in value else "data:image/png;base64,LAST"

    async def fake_download(video_url, *args, auth_headers=None, **kwargs):
        captured["download"] = (video_url, auth_headers)
        return {"result_url": "/api/v1/fs/entity/videos/omni.mp4"}

    monkeypatch.setattr(media_tasks, "_ensure_inline_data_url", fake_inline)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)
    response = _FakeResponse(200, {"status": "completed", "steps": [
        {"type": "thought", "content": [{"type": "thought", "text": "..."}]},
        {"type": "model_output", "content": [{"type": "video", "mime_type": "video/mp4", "data": "VIDEO"}]},
    ]})

    result = await video_adapters.GoogleInteractionsVideoAdapter().submit(
        _job(first_frame_url="/fs/first.png", last_frame_url="/fs/last.png"),
        "AIza-user-key",
        "https://generativelanguage.googleapis.com/v1beta/",
        _runtime(captured, response),
    )

    assert result["result_url"].endswith("/omni.mp4")
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert captured["headers"]["x-goog-api-key"] == "AIza-user-key"
    payload = captured["payload"]
    assert payload["model"] == "gemini-omni-1.1-flash"
    assert payload["response_format"] == {"type": "video", "resolution": "720p", "aspect_ratio": "9:16"}
    assert payload["input"][:2] == [
        {"type": "image", "data": "FRAME", "mime_type": "image/png"},
        {"type": "image", "data": "LAST", "mime_type": "image/png"},
    ]
    text = payload["input"][2]["text"]
    assert text.startswith("A woman plays violin on a rooftop.")
    assert "Image 1 is the first frame" in text and "Image 2 is the last frame" in text
    assert "about 8 seconds" in text
    assert captured["download"] == ("data:video/mp4;base64,VIDEO", None)


@pytest.mark.asyncio
async def test_omni_without_a_video_reports_a_blocked_prompt(monkeypatch):
    captured: dict = {}
    response = _FakeResponse(200, {"status": "completed", "steps": [{"type": "model_output", "content": []}]})

    result = await video_adapters.GoogleInteractionsVideoAdapter().submit(
        _job(), "AIza-user-key", "", _runtime(captured, response),
    )

    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert "no video" in result["error"]


def test_google_video_routes_and_capabilities():
    from packages.core.constants.models import video_model_capabilities

    adapter = video_adapters.select_video_generation_adapter(model=MODEL, provider="google", api_key="AIza-key")
    assert isinstance(adapter, video_adapters.GoogleInteractionsVideoAdapter)
    assert video_adapters.video_adapter_by_name(adapter.adapter_name) is not None
    caps = video_model_capabilities(MODEL)
    assert caps["first_frame"] and caps["last_frame"] and caps["native_audio"]
    assert caps["generation_schema"]["duration"]["text_to_video"]["maximum"] == 10


def test_a_bare_video_id_takes_the_provider_of_its_native_endpoint():
    import apps.api.routers.auth as auth_router

    normalize = auth_router._normalize_custom_media_model
    assert normalize("video", "gemini-omni-1.1-flash", "https://generativelanguage.googleapis.com") == MODEL
    assert normalize("video", "gemini-omni-1.1-flash", "https://relay.example.test/v1") == "xai/gemini-omni-1.1-flash"
    assert normalize("video", MODEL, "https://relay.example.test/v1") == MODEL


@pytest.mark.asyncio
async def test_a_model_outside_the_catalog_never_gets_a_platform_key(monkeypatch):
    """No BYOK key bound to the custom model: the worker must not fall back to Manor's Google key."""
    from packages.core.ai.runtime import media as runtime_media
    from packages.core.ai.tools import extended_tools

    async def no_byok(*args, **kwargs):
        return "", "", False

    async def platform_key(provider):
        return "AIza-platform-key", ""

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", no_byok)
    monkeypatch.setattr(extended_tools, "_platform_native_media_credential_async", platform_key)

    credentials = await runtime_media.runtime_resolve_video_generation_credentials(
        user_id="user", entity_id="entity", model=MODEL,
        stored_adapter_name="google_interactions", openrouter_adapter_name="openrouter",
    )

    assert credentials.api_key == ""
    assert credentials.is_byok is False
