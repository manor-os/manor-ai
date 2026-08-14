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


def _i2v_params(**extra):
    params = {"duration": 5, "resolution": "480p", "first_frame_url": "/fs/entity/frame.png"}
    params.update(extra)
    return params


def _patch_public_url(monkeypatch, captured):
    async def fake_public_url(url: str, entity_id: str, **kwargs) -> str:
        captured["source"] = url
        return "https://manor.example.test/public/frame.png"

    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_public_url)


@pytest.mark.asyncio
async def test_atlascloud_prompt_only_is_rejected_before_any_http_call(monkeypatch):
    # Verified against the live API: the route is i2v-only and rejects
    # prompt-only requests. The adapter must fail fast with an actionable
    # message instead of submitting a doomed prediction.
    called = {"post": False}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, headers, json):
            called["post"] = True
            return _FakeResponse(200, {})

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)

    result = await media_tasks._call_atlascloud_api(_job(), "atlas-key", None)

    assert "image-to-video model" in result["error"]
    assert called["post"] is False


@pytest.mark.asyncio
async def test_atlascloud_i2v_submit_and_poll(monkeypatch):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return _FakeResponse(200, {"code": 200, "data": {"id": "pred_123"}})

    async def fake_remember(*args, **kwargs):
        captured["remember"] = args

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        captured["poll_url"] = poll_url
        return "https://cdn.atlas.test/out.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/atlas.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    _patch_public_url(monkeypatch, captured)
    monkeypatch.setattr(media_tasks, "_remember_provider_poll", fake_remember)
    monkeypatch.setattr(media_tasks, "_poll_generic_video_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = _job(params=_i2v_params())
    result = await media_tasks._call_atlascloud_api(job, "atlas-key", None)

    assert result["result_url"].endswith("/atlas.mp4")
    assert captured["url"] == "https://api.atlascloud.ai/api/v1/model/generateVideo"
    assert captured["headers"]["Authorization"] == "Bearer atlas-key"
    # Verified against the live API: model ids are three-part paths.
    assert captured["payload"]["model"] == "atlascloud/wan-2.2-turbo-spicy/image-to-video"
    assert captured["payload"]["image"] == "https://manor.example.test/public/frame.png"
    assert captured["payload"]["resolution"] == "480p"
    assert captured["source"] == "/fs/entity/frame.png"
    assert captured["poll_url"] == "https://api.atlascloud.ai/api/v1/model/prediction/pred_123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (401, {"error": {"message": "invalid api key"}}, "Atlas Cloud generation failed (401)"),
        # Atlas wraps errors in HTTP 200 with a non-200 inner code and a
        # "msg" key — the exact shape behind the empty staging error.
        (200, {"code": 400, "msg": "not found"}, "Atlas Cloud generation failed (400): not found"),
    ],
)
async def test_atlascloud_provider_errors_are_surfaced(monkeypatch, status_code, payload, expected):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, headers, json):
            return _FakeResponse(status_code, payload)

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    _patch_public_url(monkeypatch, captured)

    job = _job(params=_i2v_params())
    result = await media_tasks._call_atlascloud_api(job, "atlas-key", None)

    assert expected in result["error"]


def test_atlas_models_never_route_through_openrouter():
    adapter = video_adapters.select_video_generation_adapter(
        model="atlascloud/wan-2.2-turbo-spicy",
        provider="atlascloud",
        api_key="sk-or-platform-key",
    )

    assert isinstance(adapter, video_adapters.AtlasCloudVideoAdapter)


def test_atlascloud_is_byok_only_provider():
    from packages.core.services.model_provider_handlers import PROVIDER_HANDLERS

    handler = PROVIDER_HANDLERS["atlascloud"]
    assert handler.env_vars == ()
    assert "video" in handler.roles


def test_atlascloud_legacy_byok_pricing_and_capabilities_remain_registered():
    from packages.core.constants.models import CATALOG, video_model_capabilities
    from packages.core.services.model_pricing import VIDEO_COST_PER_SECOND

    ids = [m["id"] for m in CATALOG["video"]]
    # Vercel has no Atlas route, so it is no longer offered in the managed
    # Model Catalog. Historical saved BYOK settings still retain their native
    # adapter, pricing, and capability metadata.
    assert "atlascloud/wan-2.2-turbo-spicy" not in ids

    pricing = VIDEO_COST_PER_SECOND["atlascloud/wan-2.2-turbo-spicy"]
    assert pricing["480p"] == 0.004
    assert pricing["720p"] == 0.008

    caps = video_model_capabilities("atlascloud/wan-2.2-turbo-spicy")
    assert caps["text_to_video"] is False
    assert caps["requires_first_frame"] is True
    assert caps["first_frame"] is True
    assert caps["native_audio"] is False
