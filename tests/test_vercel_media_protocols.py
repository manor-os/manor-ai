import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from packages.core.services.model_gateway import ModelGatewayRoute


@pytest.mark.asyncio
async def test_oversized_local_image_is_compressed_for_inline_video_reference(
    monkeypatch,
    tmp_path,
):
    from packages.core.tasks import media_tasks
    from packages.core.services import entity_fs

    image_path = tmp_path / "images" / "scene.png"
    image_path.parent.mkdir()
    Image.effect_noise((1024, 1024), 100).convert("RGB").save(image_path, format="PNG")
    assert image_path.stat().st_size > 32 * 1024

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(
        media_tasks,
        "_entity_rel_path_from_reference",
        lambda *_args, **_kwargs: "images/scene.png",
    )

    data_url = await media_tasks._ensure_inline_data_url(
        "/api/v1/fs/entity/images/scene.png",
        "entity",
        max_bytes=32 * 1024,
    )
    header, encoded = data_url.split(",", 1)

    assert header == "data:image/jpeg;base64"
    assert len(base64.b64decode(encoded)) <= 32 * 1024


@pytest.mark.asyncio
async def test_vercel_video_reference_uses_gateway_safe_inline_size_limit():
    from packages.core.tasks import video_adapters

    captured = {}

    async def inline_data_url(value, _entity_id, **kwargs):
        captured.update(value=value, **kwargs)
        return "data:image/jpeg;base64,aW1hZ2U="

    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=None,
        media_api_timeout=None,
        ensure_public_url=None,
        public_url_kwargs=lambda _url: {},
        remember_provider_poll=None,
        poll_openrouter_generation=None,
        poll_volcengine_task=None,
        poll_generic_video_task=None,
        download_and_save=None,
        extract_video_url=None,
        extract_task_id=None,
        provider_error_message=None,
        openrouter_api_url=None,
        normalize_duration=None,
        normalize_resolution=None,
        ensure_inline_data_url=inline_data_url,
    )

    result = await video_adapters.materialize_video_reference(
        "/api/v1/fs/entity/images/scene.png",
        "entity",
        media_kind="image",
        capability=video_adapters.VERCEL_IMAGE_MEDIA_REFERENCE,
        runtime=runtime,
        public_base_url="",
    )

    assert captured == {
        "value": "/api/v1/fs/entity/images/scene.png",
        "max_bytes": video_adapters.VERCEL_VIDEO_INLINE_REFERENCE_MAX_BYTES,
    }
    assert result.as_vercel_file() == {
        "type": "file",
        "data": "aW1hZ2U=",
        "mediaType": "image/jpeg",
    }


@pytest.mark.asyncio
async def test_image_reference_falls_back_to_signed_url_when_inline_encoding_fails():
    from packages.core.tasks import video_adapters

    calls = []

    async def inline_data_url(*_args, **_kwargs):
        raise video_adapters.InlineMediaReferenceLimitError(
            "Local image reference cannot be encoded below the inline limit."
        )

    async def ensure_public_url(value, entity_id, **kwargs):
        calls.append((value, entity_id, kwargs))
        return "https://media.example.test/public/scene.png"

    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=None,
        media_api_timeout=None,
        ensure_public_url=ensure_public_url,
        public_url_kwargs=lambda public_base_url: {"public_base_url": public_base_url},
        remember_provider_poll=None,
        poll_openrouter_generation=None,
        poll_volcengine_task=None,
        poll_generic_video_task=None,
        download_and_save=None,
        extract_video_url=None,
        extract_task_id=None,
        provider_error_message=None,
        openrouter_api_url=None,
        normalize_duration=None,
        normalize_resolution=None,
        ensure_inline_data_url=inline_data_url,
    )

    result = await video_adapters.materialize_video_reference(
        "/api/v1/fs/entity/images/scene.png",
        "entity",
        media_kind="image",
        capability=video_adapters.OPENROUTER_IMAGE_MEDIA_REFERENCE,
        runtime=runtime,
        public_base_url="https://manor.example.test",
    )

    assert result.as_data_url() == "https://media.example.test/public/scene.png"
    assert calls == [
        (
            "/api/v1/fs/entity/images/scene.png",
            "entity",
            {"public_base_url": "https://manor.example.test"},
        )
    ]


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
@pytest.mark.parametrize(
    "model_id,output_format,animated",
    [
        ("openai/gpt-image-2", "png", False),
        ("openai/gpt-image-2.6-sunburst", "png", False),
        ("openai/gpt-image-2.6-sunburst", "gif", True),
        ("openai/gpt-image-2.6-sunburst", "gif", False),
    ],
)
async def test_managed_image_generation_uses_vercel_v4_protocol(monkeypatch, model_id, output_format, animated):
    from packages.core.ai.tools import extended_tools
    from packages.core.services import vercel_ai_gateway

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")

    async def credentials(*_args, **_kwargs):
        # The managed OpenRouter default must not prevent Vercel GIF routing.
        return "sk-or-test", "https://openrouter.ai/api/v1", False

    async def model(*_args, **_kwargs):
        return model_id

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
    encoded = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    if animated:
        frames[0].save(encoded, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    else:
        frames[0].save(encoded, format="PNG")
    image_bytes = encoded.getvalue()

    async def gateway_post(**kwargs):
        captured.update(kwargs)
        return {
            "images": [base64.b64encode(image_bytes).decode("ascii")],
            "usage": {"inputTokens": 100, "outputTokens": 200},
        }

    async def save(**kwargs):
        captured["saved"] = kwargs
        return extended_tools._carry_saved_image_details(
            f"/api/v1/fs/entity/images/vercel.{output_format}", document_id="document-image",
        )

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", model)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", route)
    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(extended_tools, "_save_generated_image_bytes", save)

    result = json.loads(
        await extended_tools._generate_image_handler(
            prompt="A clean blue square",
            aspect_ratio="1:1",
            output_format=output_format,
            size="32x32",
            **({"frame_count": 2, "frame_duration_ms": 200} if output_format == "gif" else {}),
        )
    )

    assert captured["protocol"] == "image"
    assert captured["model"] == model_id
    if output_format == "gif":
        assert captured["payload"]["size"] == "32x32"
        assert captured["payload"]["providerOptions"] == {"openai": {
            "outputFormat": "gif", "frameCount": 2, "frameDurationMs": 200, "quality": "medium",
        }}
        if not animated:
            assert "not an animated GIF" in result["error"]
            assert "saved" not in captured
            return
        assert captured["saved"]["image_bytes"] == image_bytes
        assert captured["saved"]["mime"] == "image/gif"
        assert captured["saved"]["usage"]["input_tokens"] == 100
        assert captured["saved"]["usage"]["output_tokens"] == 200
        assert result["output_format"] == "gif"
        assert result["frame_count"] == 2
        assert result["frame_duration_ms"] == 200
    else:
        assert captured["payload"]["aspectRatio"] == "1:1"
    assert result["model"] == model_id
    assert result["document_id"] == "document-image"


@pytest.mark.asyncio
async def test_managed_gif_with_references_sends_files_and_gif_options(monkeypatch):
    """The managed route carries the same GIF fields and reference images the native edit request does."""
    from packages.core.ai.tools import extended_tools
    from packages.core.services import vercel_ai_gateway

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")

    async def credentials(*_args, **_kwargs):
        return "sk-or-test", "https://openrouter.ai/api/v1", False

    async def model(*_args, **_kwargs):
        return "openai/gpt-image-2.6-sunburst"

    async def route(*_args, **_kwargs):
        return ModelGatewayRoute(
            api_key="vck_test", base_url="https://ai-gateway.vercel.sh/v1", provider="vercel",
            source="official", source_detail="test",
        )

    encoded = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    frames[0].save(encoded, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    captured = {}

    async def gateway_post(**kwargs):
        captured.update(kwargs)
        return {"images": [base64.b64encode(encoded.getvalue()).decode("ascii")], "usage": {}}

    async def refs(_urls, *_args, **_kwargs):
        return [("character.png", b"character", "image/png"), ("style.png", b"style", "image/png")]

    async def save(**kwargs):
        captured["saved"] = kwargs
        return extended_tools._carry_saved_image_details(
            "/api/v1/fs/entity/images/vercel.gif", document_id="document-gif",
        )

    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", model)
    monkeypatch.setattr(extended_tools, "_resolve_official_model_route", route)
    monkeypatch.setattr(extended_tools, "_load_image_references_for_upload", refs)
    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(extended_tools, "_save_generated_image_bytes", save)

    result = json.loads(await extended_tools._generate_image_handler(
        prompt="The character waves in this style.", output_format="gif", frame_count=2,
        frame_duration_ms=200, size="32x32", reference_urls=["characters/cat.png", "styles/ink.png"],
    ))

    assert captured["payload"]["providerOptions"] == {"openai": {
        "outputFormat": "gif", "frameCount": 2, "frameDurationMs": 200, "quality": "medium",
    }}
    assert [file["mediaType"] for file in captured["payload"]["files"]] == ["image/png", "image/png"]
    assert captured["saved"]["mime"] == "image/gif"
    assert result["document_id"] == "document-gif" and result["output_format"] == "gif"




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
@pytest.mark.parametrize("resolution,aspect_ratio,expected", [
    ("720p", "16:9", "1280x720"),
    ("720p", "9:16", "720x1280"),
    ("1080p", "9:16", "1080x1920"),
    ("480p", "9:16", "480x864"),
    ("1080p", "3:4", "1248x1664"),
])
async def test_video_adapter_uses_vercel_start_and_status_protocol(monkeypatch, resolution, aspect_ratio, expected):
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
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
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
    assert calls[0]["payload"]["resolution"] == expected
    assert calls[0]["payload"]["aspectRatio"] == aspect_ratio
    assert remembered["provider"] == "vercel"
    assert json.loads(remembered["generation_id"]) == {"id": "operation-1"}


@pytest.mark.asyncio
async def test_openrouter_video_adapter_inlines_local_first_frame_as_base64():
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

    async def ensure_public_url(*_args, **_kwargs):
        raise AssertionError("OpenRouter local image frames should not require a public URL")

    async def inline_data_url(value, _entity_id, **kwargs):
        assert value == "/api/v1/fs/entity/images/scene-01.png"
        assert kwargs["max_bytes"] == video_adapters.OPENROUTER_VIDEO_INLINE_IMAGE_MAX_BYTES
        return "data:image/png;base64,aW1hZ2U="

    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=Client,
        media_api_timeout=None,
        ensure_public_url=ensure_public_url,
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
    object.__setattr__(runtime, "ensure_inline_data_url", inline_data_url)
    job = SimpleNamespace(
        id="job-1",
        entity_id="entity",
        prompt="A short video",
        model="bytedance/seedance-2.0",
        params={
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "first_frame_url": "/api/v1/fs/entity/images/scene-01.png",
        },
    )

    await video_adapters.OpenRouterVideoAdapter().submit(
        job,
        "sk-or-admin",
        "https://admin-openrouter.example/v1",
        runtime,
    )

    assert captured["url"] == "https://admin-openrouter.example/v1/videos"
    assert captured["kwargs"]["json"]["frame_images"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
            "frame_type": "first_frame",
        }
    ]


@pytest.mark.asyncio
async def test_vercel_video_request_shares_one_inline_budget_across_images(monkeypatch):
    """The gateway caps the whole request, so two near-limit images cannot both go inline."""
    import base64

    from packages.core.services import vercel_ai_gateway
    from packages.core.tasks import video_adapters

    calls = []

    async def gateway_post(**kwargs):
        calls.append(kwargs)
        if kwargs.get("endpoint_suffix") == "start":
            return {"operation": {"id": "operation-1"}}
        return {"status": "completed", "videos": [{"type": "url", "url": "https://cdn.example.test/v.mp4", "mediaType": "video/mp4"}]}

    async def no_sleep(_seconds):
        return None

    async def remember(*_args, **_kwargs):
        return None

    async def download(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/result.mp4"}

    async def ensure_public_url(value, *_args, **_kwargs):
        return f"https://signed.example.test/{value}"

    limits = []

    async def inline_data_url(value, _entity_id, *, max_bytes):
        # Every image compresses to just under the allowance it is given.
        limits.append(max_bytes)
        size = max_bytes - 1024
        if size <= 0:
            raise video_adapters.InlineMediaReferenceLimitError("too small")
        return "data:image/jpeg;base64," + base64.b64encode(b"x" * size).decode()

    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(video_adapters.asyncio, "sleep", no_sleep)
    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=None, media_api_timeout=None, ensure_public_url=ensure_public_url,
        public_url_kwargs=lambda _url: {}, remember_provider_poll=remember,
        poll_openrouter_generation=None, poll_volcengine_task=None, poll_generic_video_task=None,
        download_and_save=download, extract_video_url=lambda _value: "", extract_task_id=lambda _value: "",
        provider_error_message=lambda _value: "", openrouter_api_url=lambda value: value,
        normalize_duration=lambda value: int(value), normalize_resolution=lambda _model, value: str(value),
        ensure_inline_data_url=inline_data_url,
    )
    job = SimpleNamespace(
        id="job-1", entity_id="entity", prompt="Continue the shot", model="bytedance/seedance-2.0",
        params={
            "duration": 10, "resolution": "1080p", "aspect_ratio": "9:16",
            "first_frame_url": "frames/end.png", "reference_urls": ["Brand/ui.png", "Brand/logo.png"],
        },
    )

    await video_adapters.VercelGatewayVideoAdapter().submit(job, "vck_test", None, runtime)

    payload = calls[0]["payload"]
    images = [payload["frameImages"][0]["image"], *payload["inputReferences"]]
    inline_bytes = sum(len(image["data"]) * 3 // 4 for image in images if image["type"] == "file")
    assert inline_bytes <= video_adapters.VERCEL_VIDEO_INLINE_REFERENCE_MAX_BYTES
    # The start frame goes inline; what no longer fits travels as a signed URL.
    assert images[0]["type"] == "file"
    assert any(image["type"] == "url" for image in images[1:])


@pytest.mark.asyncio
async def test_vercel_seedance_audio_reference_rides_provider_options(monkeypatch):
    """Regression: audio references were dropped on the gateway route, so a
    voice-driven (lip-sync) Seedance request silently lost its voice."""
    from packages.core.services import vercel_ai_gateway
    from packages.core.tasks import video_adapters

    calls = []

    async def gateway_post(**kwargs):
        calls.append(kwargs)
        if kwargs.get("endpoint_suffix") == "start":
            return {"operation": {"id": "operation-1"}}
        return {"status": "completed", "videos": [{"type": "url", "url": "https://cdn.example.test/v.mp4", "mediaType": "video/mp4"}]}

    async def no_sleep(_seconds):
        return None

    async def remember(*_args, **_kwargs):
        return None

    async def download(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/result.mp4"}

    async def ensure_public_url(value, *_args, **_kwargs):
        return f"https://signed.example.test/{value}"

    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(video_adapters.asyncio, "sleep", no_sleep)
    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=None, media_api_timeout=None, ensure_public_url=ensure_public_url,
        public_url_kwargs=lambda _url: {}, remember_provider_poll=remember,
        poll_openrouter_generation=None, poll_volcengine_task=None, poll_generic_video_task=None,
        download_and_save=download, extract_video_url=lambda _value: "", extract_task_id=lambda _value: "",
        provider_error_message=lambda _value: "", openrouter_api_url=lambda value: value,
        normalize_duration=lambda value: int(value), normalize_resolution=lambda _model, value: str(value),
    )
    job = SimpleNamespace(
        id="job-1", entity_id="entity", prompt="The host speaks to camera", model="bytedance/seedance-2.0",
        params={
            "duration": 12, "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": True,
            "reference_urls": ["hosts/presenter.png"], "audio_reference_urls": ["vo/presenter.mp3"],
        },
    )

    await video_adapters.VercelGatewayVideoAdapter().submit(job, "vck_test", None, runtime)

    options = calls[0]["payload"]["providerOptions"]["bytedance"]
    assert options["referenceAudio"] == ["https://signed.example.test/vo/presenter.mp3"]
    assert options["generateAudio"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model, first_frame, generate_audio, wire_model, options",
    [
        # Pro with a first frame: image-to-video, pro mode, native sound on.
        ("kwaivgi/kling-v3.0-pro", "hosts/presenter.png", True, "klingai/kling-v3.0-i2v", {"mode": "pro", "sound": "on"}),
        # Std without a frame: text-to-video; sound exists only in pro mode.
        ("kwaivgi/kling-v3.0-std", "", True, "klingai/kling-v3.0-t2v", {"mode": "std"}),
    ],
)
async def test_vercel_kling_routes_image_to_video_and_sends_klingai_options(
    monkeypatch, model, first_frame, generate_audio, wire_model, options,
):
    """Regression: Kling always went to the t2v gateway model (the first frame
    was ignored) and its options were filed under ``kwaivgi``, which the
    gateway does not read — so Pro mode and native sound never applied."""
    from packages.core.services import vercel_ai_gateway
    from packages.core.tasks import video_adapters

    calls = []

    async def gateway_post(**kwargs):
        calls.append(kwargs)
        if kwargs.get("endpoint_suffix") == "start":
            return {"operation": {"id": "operation-1"}}
        return {"status": "completed", "videos": [{"type": "url", "url": "https://cdn.example.test/v.mp4", "mediaType": "video/mp4"}]}

    async def no_sleep(_seconds):
        return None

    async def remember(*_args, **_kwargs):
        return None

    async def download(*_args, **_kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/result.mp4"}

    async def ensure_public_url(value, *_args, **_kwargs):
        return f"https://signed.example.test/{value}"

    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", gateway_post)
    monkeypatch.setattr(video_adapters.asyncio, "sleep", no_sleep)
    runtime = video_adapters.VideoAdapterRuntime(
        http_client_cls=None, media_api_timeout=None, ensure_public_url=ensure_public_url,
        public_url_kwargs=lambda _url: {}, remember_provider_poll=remember,
        poll_openrouter_generation=None, poll_volcengine_task=None, poll_generic_video_task=None,
        download_and_save=download, extract_video_url=lambda _value: "", extract_task_id=lambda _value: "",
        provider_error_message=lambda _value: "", openrouter_api_url=lambda value: value,
        normalize_duration=lambda value: int(value), normalize_resolution=lambda _model, value: str(value),
    )
    params = {"duration": 10, "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": generate_audio}
    if first_frame:
        params["first_frame_url"] = first_frame
    job = SimpleNamespace(id="job-1", entity_id="entity", prompt="The host speaks to camera", model=model, params=params)

    await video_adapters.VercelGatewayVideoAdapter().submit(job, "vck_test", None, runtime)

    assert {call["model"] for call in calls} == {wire_model}
    assert calls[0]["payload"]["providerOptions"] == {"klingai": options}
