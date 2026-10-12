from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.tasks.video_adapters import (
    KlingVideoAdapter,
    OpenRouterVideoAdapter,
    VolcengineSeedanceAdapter,
    native_video_model,
    select_video_generation_adapter,
    video_adapter_metadata,
)


@pytest.mark.parametrize(
    ("model", "provider", "native_adapter"),
    [
        ("bytedance/seedance-2.0", "bytedance", VolcengineSeedanceAdapter),
        ("bytedance/seedance-2.0-fast", "bytedance", VolcengineSeedanceAdapter),
        ("bytedance/seedance-v1.0-pro-fast", "bytedance", VolcengineSeedanceAdapter),
        ("kwaivgi/kling-v3.0-std", "kwaivgi", KlingVideoAdapter),
        ("kwaivgi/kling-v3.0-pro", "kwaivgi", KlingVideoAdapter),
    ],
)
def test_every_catalog_video_model_has_native_and_openrouter_routes(model, provider, native_adapter):
    assert isinstance(
        select_video_generation_adapter(
            model=model,
            provider=provider,
            api_key="native-provider-key",
        ),
        native_adapter,
    )
    assert isinstance(
        select_video_generation_adapter(
            model=model,
            provider=provider,
            api_key="sk-or-v1-platform-key",
        ),
        OpenRouterVideoAdapter,
    )


def test_video_adapter_selection_prefers_openrouter_for_openrouter_keys():
    adapter = select_video_generation_adapter(
        model="bytedance/seedance-2.0",
        provider="bytedance",
        api_key="sk-or-v1-platform-key",
    )

    assert isinstance(adapter, OpenRouterVideoAdapter)
    assert video_adapter_metadata(
        "bytedance/seedance-2.0",
        "bytedance",
        "sk-or-v1-platform-key",
    ) == {
        "video_provider": "bytedance",
        "video_adapter": "openrouter",
        "video_route": "openrouter",
        "native_model": "bytedance/seedance-2.0",
    }


def test_video_adapter_selection_routes_native_seedance_and_kling():
    seedance = select_video_generation_adapter(
        model="bytedance/seedance-2.0",
        provider="bytedance",
        api_key="volc-native-key",
    )
    kling = select_video_generation_adapter(
        model="kwaivgi/kling-v3.0-pro",
        provider="kwaivgi",
        api_key="kling-native-key",
    )

    assert isinstance(seedance, VolcengineSeedanceAdapter)
    assert isinstance(kling, KlingVideoAdapter)
    assert native_video_model("bytedance/seedance-2.0") == "doubao-seedance-2-0-260128"
    assert native_video_model("bytedance/seedance-2.5") == "doubao-seedance-2-5-260628"
    assert native_video_model("bytedance/seedance-v1.0-pro-fast") == "seedance-1-0-pro-fast-251015"
    assert native_video_model("kwaivgi/kling-v3.0-pro") == "kling-v3.0-pro"


def test_seedance_25_requires_native_route():
    assert isinstance(
        select_video_generation_adapter(model="bytedance/seedance-2.5", provider="bytedance", api_key="ark-native-key"),
        VolcengineSeedanceAdapter,
    )
    assert select_video_generation_adapter(model="bytedance/seedance-2.5", provider="bytedance", api_key="sk-or-key") is None
    assert select_video_generation_adapter(model="bytedance/seedance-2.5", provider="vercel", api_key="vercel-key") is None


def test_seedance_25_pricing_uses_its_own_rate_and_video_input_discount():
    from packages.core.services.model_pricing import estimate_video_cost_usd

    regular = estimate_video_cost_usd("bytedance/seedance-2.5", 30, "720p")
    with_video = estimate_video_cost_usd("bytedance/seedance-2.5", 30, "720p", has_video_input=True)
    assert regular == pytest.approx(6.93)
    assert with_video == pytest.approx(regular * 6.4 / 10.7)


@pytest.mark.asyncio
async def test_seedance_25_native_payload_preserves_limits_and_schema(monkeypatch):
    from packages.core.tasks import media_tasks

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"content": {"video_url": "https://cdn.example.test/out.mp4"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured.update(url=url, payload=json)
            return FakeResponse()

    async def fake_public_url(url, entity_id, **kwargs):
        return url

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/out.mp4"}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_public_url)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)
    job = SimpleNamespace(
        id="job-25", entity_id="entity", prompt="A scene", model="bytedance/seedance-2.5",
        params={
            "duration": 30, "resolution": "720p", "aspect_ratio": "16:9",
            "reference_urls": [f"https://cdn.example.test/image-{i}.png" for i in range(30)],
            "reference_video_urls": [f"https://cdn.example.test/video-{i}.mp4" for i in range(10)],
            "audio_reference_urls": [f"https://cdn.example.test/audio-{i}.wav" for i in range(10)],
        },
    )

    result = await media_tasks._call_volcengine_seedance_api(job, "ark-key", None)

    assert result["result_url"] == "/api/v1/fs/entity/out.mp4"
    assert captured["url"].endswith("/contents/generations/tasks")
    payload = captured["payload"]
    assert payload["model"] == "doubao-seedance-2-5-260628"
    assert payload["duration"] == 30
    assert payload["ratio"] == "16:9"
    assert [item.get("role") for item in payload["content"]].count("reference_image") == 30
    assert [item.get("role") for item in payload["content"]].count("reference_video") == 10
    assert [item.get("role") for item in payload["content"]].count("reference_audio") == 10


@pytest.mark.asyncio
async def test_seedance_25_first_frame_uses_adaptive_ratio(monkeypatch):
    from packages.core.tasks import media_tasks

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"content": {"video_url": "https://cdn.example.test/out.mp4"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured.update(json)
            return FakeResponse()

    async def fake_public_url(url, entity_id, **kwargs):
        return url

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/out.mp4"}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_public_url)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)
    job = SimpleNamespace(
        id="job-frame", entity_id="entity", prompt="A scene", model="bytedance/seedance-2.5",
        params={"duration": 30, "resolution": "720p", "aspect_ratio": "16:9", "first_frame_url": "https://cdn.example.test/frame.png"},
    )

    await media_tasks._call_volcengine_seedance_api(job, "ark-key", None)

    assert captured["ratio"] == "adaptive"


def test_video_adapter_metadata_records_native_route():
    assert video_adapter_metadata(
        "bytedance/seedance-2.0-fast",
        "bytedance",
        "volc-native-key",
    ) == {
        "video_provider": "bytedance",
        "video_adapter": "volcengine_seedance",
        "video_route": "native",
        "native_model": "doubao-seedance-2-0-fast-260128",
    }
