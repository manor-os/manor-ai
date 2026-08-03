from __future__ import annotations

from types import SimpleNamespace

import pytest

import packages.core.tasks.media_tasks as media_tasks
from packages.core.tasks import video_adapters


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _job(**overrides):
    base = dict(
        id="job_atlas",
        model="atlascloud/wan-2.2-turbo-spicy",
        prompt="a paper boat drifting down a rainy street",
        entity_id="entity",
        params={"duration": 5, "resolution": "480p"},
        agent_id=None,
        conversation_id=None,
        user_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_atlascloud_submit_and_poll(monkeypatch):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return _FakeResponse(200, {"data": {"id": "pred_123"}})

    async def fake_remember(*args, **kwargs):
        captured["remember"] = args

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        captured["poll_url"] = poll_url
        return "https://cdn.atlas.test/out.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/atlas.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_remember_provider_poll", fake_remember)
    monkeypatch.setattr(media_tasks, "_poll_generic_video_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    result = await media_tasks._call_atlascloud_api(_job(), "atlas-key", None)

    assert result["result_url"].endswith("/atlas.mp4")
    assert captured["url"] == "https://api.atlascloud.ai/api/v1/model/generateVideo"
    assert captured["headers"]["Authorization"] == "Bearer atlas-key"
    assert captured["payload"]["model"] == "wan-2.2-turbo-spicy"
    assert captured["payload"]["prompt"].startswith("a paper boat")
    assert captured["payload"]["resolution"] == "480p"
    assert "image" not in captured["payload"]
    assert captured["poll_url"] == "https://api.atlascloud.ai/api/v1/model/prediction/pred_123"


@pytest.mark.asyncio
async def test_atlascloud_image_to_video_sends_public_image_url(monkeypatch):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            captured["payload"] = json
            return _FakeResponse(200, {"data": {"id": "pred_i2v"}})

    async def fake_public_url(url: str, entity_id: str, **kwargs) -> str:
        captured["source"] = url
        return "https://manor.example.test/public/frame.png"

    async def fake_remember(*args, **kwargs):
        return None

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        return "https://cdn.atlas.test/i2v.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/i2v.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_public_url)
    monkeypatch.setattr(media_tasks, "_remember_provider_poll", fake_remember)
    monkeypatch.setattr(media_tasks, "_poll_generic_video_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = _job(params={"duration": 5, "resolution": "480p", "first_frame_url": "/fs/entity/frame.png"})
    result = await media_tasks._call_atlascloud_api(job, "atlas-key", None)

    assert result["result_url"].endswith("/i2v.mp4")
    assert captured["source"] == "/fs/entity/frame.png"
    assert captured["payload"]["image"] == "https://manor.example.test/public/frame.png"


@pytest.mark.asyncio
async def test_atlascloud_provider_error_is_surfaced(monkeypatch):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            return _FakeResponse(401, {"error": {"message": "invalid api key"}})

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)

    result = await media_tasks._call_atlascloud_api(_job(), "bad-key", None)

    assert "Atlas Cloud generation failed (401)" in result["error"]


def test_atlas_models_never_route_through_openrouter():
    adapter = video_adapters.select_video_generation_adapter(
        model="atlascloud/wan-2.2-turbo-spicy",
        provider="atlascloud",
        # Even with Manor's platform OpenRouter key resolved, Atlas models
        # must not fall back to the OpenRouter adapter (the model only
        # exists on Atlas).
        api_key="sk-or-platform-key",
    )

    assert isinstance(adapter, video_adapters.AtlasCloudVideoAdapter)


def test_atlascloud_is_byok_only_provider():
    from packages.core.services.model_provider_handlers import PROVIDER_HANDLERS

    handler = PROVIDER_HANDLERS["atlascloud"]
    # No platform env vars — official-route resolution can never produce a
    # Manor-side Atlas credential, which is what makes the model BYOK-only.
    assert handler.env_vars == ()
    assert "video" in handler.roles


def test_atlascloud_catalog_pricing_and_capabilities_are_registered():
    from packages.core.constants.models import CATALOG, video_model_capabilities
    from packages.core.services.model_pricing import VIDEO_COST_PER_SECOND

    ids = [m["id"] for m in CATALOG["video"]]
    assert "atlascloud/wan-2.2-turbo-spicy" in ids

    pricing = VIDEO_COST_PER_SECOND["atlascloud/wan-2.2-turbo-spicy"]
    assert pricing["480p"] == 0.004
    assert pricing["720p"] == 0.008

    caps = video_model_capabilities("atlascloud/wan-2.2-turbo-spicy")
    assert caps["first_frame"] is True
    assert caps["native_audio"] is False
