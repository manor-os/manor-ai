import base64
import json
from types import SimpleNamespace

import pytest

from packages.core.services.model_gateway import ModelGatewayRoute


@pytest.mark.asyncio
async def test_restored_google_image_uses_native_route_before_vercel(monkeypatch):
    from packages.core.ai.tools import extended_tools

    captured = {}

    async def route(model, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return SimpleNamespace(api_key="AIza-native", base_url="https://generativelanguage.googleapis.com/v1beta", provider="google")

    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", route)

    await extended_tools._resolve_managed_media_route(
        "google/gemini-3.1-flash-image-preview",
        role="image",
        provider="google",
    )

    assert captured["model"] == "google/gemini-3.1-flash-image-preview"
    assert captured["provider_chain"] == ("google", "openrouter")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "role", "provider", "expected_chain"),
    [
        ("openai/gpt-5-image-mini", "image", "openai", ("vercel", "openai", "openrouter")),
        ("kwaivgi/kling-v3.0-std", "video", "kwaivgi", ("vercel", "kwaivgi", "openrouter")),
    ],
)
async def test_catalog_models_with_vercel_aliases_prefer_the_gateway(
    monkeypatch,
    model,
    role,
    provider,
    expected_chain,
):
    from packages.core.ai.tools import extended_tools

    captured = {}

    async def route(model, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return SimpleNamespace(
            api_key="vck-test",
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
        )

    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", route)

    await extended_tools._resolve_managed_media_route(
        model,
        role=role,
        provider=provider,
    )

    assert captured["provider_chain"] == expected_chain


@pytest.mark.asyncio
async def test_managed_image_generation_uses_vercel_v4_protocol(monkeypatch):
    from packages.core.ai.tools import extended_tools
    from packages.core.services import vercel_ai_gateway

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")

    async def credentials(*_args, **_kwargs):
        return "", "", False

    async def model(*_args, **_kwargs):
        return "openai/gpt-image-2"

    async def route(*_args, **kwargs):
        assert kwargs.get("provider_chain") == ("vercel", "openai", "openrouter")
        return ModelGatewayRoute(
            api_key="vck_test",
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source="official",
            source_detail="test",
        )

    captured = {}

    async def gateway_post(**kwargs):
        captured.update(kwargs)
        # Minimal valid 1x1 transparent PNG.
        return {
            "images": [
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+3n0Y5QAAAABJRU5ErkJggg=="
            ]
        }

    async def save(**_kwargs):
        return "/api/v1/fs/entity/images/vercel.png"

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", model)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", route)
    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(extended_tools, "_save_generated_image_bytes", save)

    result = json.loads(
        await extended_tools._generate_image_handler(
            prompt="A clean blue square",
            aspect_ratio="1:1",
        )
    )

    assert result["model"] == "openai/gpt-image-2"
    assert captured["protocol"] == "image"
    assert captured["model"] == "openai/gpt-image-2"
    assert captured["payload"]["aspectRatio"] == "1:1"


@pytest.mark.asyncio
async def test_managed_stt_uses_vercel_transcription_protocol(monkeypatch):
    from packages.core.services import model_gateway, vercel_ai_gateway
    from packages.core.services.voice.whisper import transcribe_blob

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")

    async def route(*_args, **kwargs):
        assert kwargs.get("provider_chain") == ("vercel", "openai")
        return ModelGatewayRoute(
            api_key="vck_test",
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source="official",
            source_detail="VERCEL_OIDC_TOKEN",
        )

    captured = {}

    async def gateway_post(**kwargs):
        captured.update(kwargs)
        return {
            "text": "hello",
            "durationInSeconds": 1.25,
            "segments": [{"text": "hello", "startSecond": 0.1, "endSecond": 1.2}],
        }

    monkeypatch.setattr(model_gateway, "resolve_official_model_route", route)
    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)

    result = await transcribe_blob(
        b"audio-bytes",
        mime="audio/webm",
        resolved_model="openai/whisper-1",
        require_timestamps=True,
    )

    assert result.text == "hello"
    assert result.segments == [{"start": 0.1, "end": 1.2, "text": "hello"}]
    assert captured["protocol"] == "transcription"
    assert captured["auth_method"] == "oidc"
    assert base64.b64decode(captured["payload"]["audio"]) == b"audio-bytes"


@pytest.mark.asyncio
async def test_embedding_protocol_uses_values_envelope(monkeypatch):
    from packages.core.services import embedding_service, vercel_ai_gateway

    captured = {}
    expected_dimensions = 1024
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(expected_dimensions))

    async def gateway_post(**kwargs):
        captured.update(kwargs)
        return {"embeddings": [[0.1] * expected_dimensions], "usage": {"tokens": 3}}

    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)

    vectors, usage = await embedding_service._post_embeddings(
        {
            "provider": "vercel",
            "api_key": "vck_test",
            "base_url": "https://ai-gateway.vercel.sh/v1",
            "dimensions": expected_dimensions,
        },
        ["hello"],
        model="openai/text-embedding-3-small",
        timeout=10.0,
    )

    assert len(vectors) == 1
    assert len(vectors[0]) == expected_dimensions
    assert usage["total_tokens"] == 3
    assert captured["protocol"] == "embedding"
    assert captured["payload"] == {
        "values": ["hello"],
        "providerOptions": {"openai": {"dimensions": expected_dimensions}},
    }


@pytest.mark.asyncio
async def test_video_adapter_uses_vercel_start_and_status_protocol(monkeypatch):
    from packages.core.services import vercel_ai_gateway
    from packages.core.tasks import video_adapters

    calls = []

    async def gateway_post(**kwargs):
        calls.append(kwargs)
        if kwargs.get("endpoint_suffix") == "start":
            return {"operation": {"id": "operation-1"}}
        return {
            "status": "completed",
            "videos": [
                {
                    "type": "url",
                    "url": "https://cdn.example.test/video.mp4",
                    "mediaType": "video/mp4",
                }
            ],
        }

    async def no_sleep(_seconds):
        return None

    remembered = {}

    async def remember(job_id, provider, poll_url, generation_id):
        remembered.update(
            job_id=job_id,
            provider=provider,
            poll_url=poll_url,
            generation_id=generation_id,
        )

    async def download(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/result.mp4"}

    async def ensure_public_url(value, *_args, **_kwargs):
        return value

    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(video_adapters.asyncio, "sleep", no_sleep)

    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=None,
        media_api_timeout=None,
        ensure_public_url=ensure_public_url,
        public_url_kwargs=lambda _url: {},
        remember_provider_poll=remember,
        poll_openrouter_generation=None,
        poll_volcengine_task=None,
        poll_generic_video_task=None,
        download_and_save=download,
        extract_video_url=lambda _value: "",
        extract_task_id=lambda _value: "",
        provider_error_message=lambda _value: "",
        openrouter_api_url=lambda value: value,
        normalize_duration=lambda value: int(value),
        normalize_resolution=lambda _model, value: str(value),
    )
    job = SimpleNamespace(
        id="job-1",
        entity_id="entity",
        prompt="A camera move through a studio",
        model="bytedance/seedance-2.0",
        params={
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
        },
    )

    result = await video_adapters.VercelGatewayVideoAdapter().submit(
        job,
        "vck_test",
        "https://ai-gateway.vercel.sh/v1",
        runtime,
    )

    assert result["result_url"].endswith("result.mp4")
    assert [call["endpoint_suffix"] for call in calls] == ["start", "status"]
    assert calls[0]["payload"]["resolution"] == "1280x720"
    assert remembered["provider"] == "vercel"
    assert json.loads(remembered["generation_id"]) == {"id": "operation-1"}


@pytest.mark.asyncio
async def test_openrouter_video_adapter_uses_resolved_base_url(monkeypatch):
    from packages.core.tasks import video_adapters

    captured = {}

    class Response:
        status_code = 202

        @staticmethod
        def json():
            return {"id": "generation-1", "polling_url": "/videos/generation-1"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return Response()

    async def remember(*_args):
        return None

    async def poll(*_args):
        return "https://cdn.example.test/video.mp4"

    async def download(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/result.mp4"}

    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=Client,
        media_api_timeout=None,
        ensure_public_url=lambda value, *_args, **_kwargs: value,
        public_url_kwargs=lambda _url: {},
        remember_provider_poll=remember,
        poll_openrouter_generation=poll,
        poll_volcengine_task=None,
        poll_generic_video_task=None,
        download_and_save=download,
        extract_video_url=lambda _value: "",
        extract_task_id=lambda _value: "",
        provider_error_message=lambda _value: "",
        openrouter_api_url=lambda value: value,
        normalize_duration=lambda value: int(value),
        normalize_resolution=lambda _model, value: str(value),
    )
    job = SimpleNamespace(
        id="job-1",
        entity_id="entity",
        prompt="A short video",
        model="bytedance/seedance-2.0",
        params={"duration": 5, "resolution": "720p", "aspect_ratio": "16:9"},
    )

    await video_adapters.OpenRouterVideoAdapter().submit(
        job,
        "sk-or-admin",
        "https://admin-openrouter.example/v1",
        runtime,
    )

    assert captured["url"] == "https://admin-openrouter.example/v1/videos"
