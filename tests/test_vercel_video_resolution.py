"""Vercel video sizes retain gateway presets and short-side defaults."""

import pytest

from packages.core.tasks.video_adapters import _vercel_video_resolution


@pytest.mark.parametrize(
    ("resolution", "aspect_ratio", "size"),
    [
        ("1080p", "16:9", "1920x1080"),
        ("1080p", "9:16", "1080x1920"),
        ("720p", "9:16", "720x1280"),
        ("720p", "16:9", "1280x720"),
        ("480p", "1:1", "480x480"),
        ("720p", "4:3", "960x720"),
        ("unknown", "unknown", "1280x720"),
    ],
)
def test_vercel_video_resolution_uses_the_short_side(resolution: str, aspect_ratio: str, size: str) -> None:
    assert _vercel_video_resolution(resolution, aspect_ratio) == size


@pytest.mark.parametrize(
    ("resolution", "aspect_ratio", "size"),
    [
        ("480p", "9:16", "480x864"),
        ("480p", "3:4", "560x752"),
        ("720p", "3:4", "834x1112"),
        ("1080p", "3:4", "1248x1664"),
        ("unknown", "3:4", "834x1112"),
    ],
)
def test_vercel_video_resolution_preserves_portrait_presets(
    resolution: str, aspect_ratio: str, size: str,
) -> None:
    # Native 3:4 tiers do not mean a 720/1080-pixel short edge. Match the
    # dimensions exercised by the gateway protocol adapter, not an invented
    # 720x960 frame. The official native API documents the 3:4 size table:
    # https://docs.volcengine.com/docs/ark/create-video-generation-task-api
    assert _vercel_video_resolution(resolution, aspect_ratio) == size


@pytest.mark.asyncio
@pytest.mark.parametrize("generate_audio", [False, True])
async def test_vercel_video_sends_audio_choice_in_provider_options(monkeypatch, generate_audio: bool) -> None:
    """The gateway reads provider settings from providerOptions; a top-level
    generateAudio alone is ignored and Seedance then adds an audio track."""
    from types import SimpleNamespace

    from packages.core.services import vercel_ai_gateway
    from packages.core.tasks.video_adapters import VercelGatewayVideoAdapter, VideoAdapterRuntime

    sent: list[dict] = []

    async def fake_post(**kwargs):
        sent.append(kwargs["payload"])
        return {}

    async def unused(*args, **kwargs):
        raise AssertionError("not expected in this test")

    monkeypatch.setattr(vercel_ai_gateway, "vercel_gateway_post", fake_post)
    runtime = VideoAdapterRuntime(
        http_client_cls=None,
        media_api_timeout=None,
        ensure_public_url=unused,
        public_url_kwargs=lambda url: {},
        remember_provider_poll=unused,
        poll_openrouter_generation=unused,
        poll_volcengine_task=unused,
        poll_generic_video_task=unused,
        download_and_save=unused,
        extract_video_url=lambda value: "",
        extract_task_id=lambda value: "",
        provider_error_message=lambda value: "",
        openrouter_api_url=lambda path: path,
        normalize_duration=lambda value, *args, **kwargs: int(value),
        normalize_resolution=lambda model, value: str(value),
    )
    job = SimpleNamespace(
        id="job-1",
        model="bytedance/seedance-2.0",
        prompt="A calm gradient",
        params={"duration": 4, "resolution": "1080p", "aspect_ratio": "9:16", "generate_audio": generate_audio},
    )

    await VercelGatewayVideoAdapter().submit(job, "key", None, runtime)

    assert sent[0]["providerOptions"] == {"bytedance": {"generateAudio": generate_audio}}
    assert sent[0]["resolution"] == "1080x1920"
