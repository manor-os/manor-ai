from __future__ import annotations

from types import SimpleNamespace

import pytest

import packages.core.tasks.media_tasks as media_tasks
from packages.core.services import entity_fs


def _enable_entity_fs(monkeypatch, root):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(root.parent))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_seedance_native_payload_uses_volcengine_content_shape(monkeypatch):
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
            return _FakeResponse(202, {"data": {"task_id": "task_123"}})

    async def fake_public_url(url: str, entity_id: str, **kwargs) -> str:
        assert kwargs == {
            "allow_data_uri": False,
            "expires_in_seconds": media_tasks.MEDIA_REFERENCE_URL_EXPIRES_SECONDS,
        }
        return f"https://public.test/{url.rstrip('/').rsplit('/', 1)[-1]}"

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        captured["poll_url"] = poll_url
        return "https://video.test/out.mp4"

    async def fake_download(video_url, prompt, model, job_id, entity_id, duration, resolution, **kwargs):
        captured["download"] = {
            "video_url": video_url,
            "duration": duration,
            "resolution": resolution,
        }
        return {"result_url": "/api/v1/fs/entity/videos/out.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_public_url)
    monkeypatch.setattr(media_tasks, "_poll_volcengine_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = SimpleNamespace(
        id="job_1",
        model="bytedance/seedance-2.0",
        prompt="make a cinematic mountain storm",
        entity_id="entity",
        params={
            "duration": 99,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "first_frame_url": "/api/v1/fs/entity/first.png",
            "last_frame_url": "/api/v1/fs/entity/last.png",
            "reference_urls": [
                "/api/v1/fs/entity/ref1.png",
                "/api/v1/fs/entity/ref2.png",
                "/api/v1/fs/entity/ref3.png",
                "/api/v1/fs/entity/ref4.png",
                "/api/v1/fs/entity/ref5.png",
            ],
            "seed": 42,
            "generate_audio": True,
            "return_last_frame": "true",
            "camera_fixed": "false",
            "watermark": "true",
            "draft": False,
        },
    )

    result = await media_tasks._call_volcengine_seedance_api(job, "volc-native-key-1234567890", None)

    assert result["result_url"].endswith("/out.mp4")
    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    assert captured["headers"]["Authorization"] == "Bearer volc-native-key-1234567890"
    assert captured["poll_url"].endswith("/contents/generations/tasks/task_123")

    payload = captured["payload"]
    assert payload["model"] == "doubao-seedance-2-0-260128"
    assert payload["duration"] == 15
    assert payload["seed"] == 42
    assert payload["generate_audio"] is True
    assert payload["return_last_frame"] is True
    assert payload["camera_fixed"] is False
    assert payload["watermark"] is True
    assert payload["draft"] is False

    content = payload["content"]
    assert content[0] == {"type": "text", "text": "make a cinematic mountain storm"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://public.test/first.png"},
        "role": "first_frame",
    }
    assert content[2]["role"] == "last_frame"
    assert content[2]["image_url"] == {"url": "https://public.test/last.png"}
    assert len(content) == 3


@pytest.mark.asyncio
async def test_seedance_native_payload_uses_frames_instead_of_duration(monkeypatch):
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
            return _FakeResponse(202, {"id": "task_frames"})

    async def fake_poll(*args, **kwargs) -> str:
        return "https://video.test/out.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/out.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_poll_volcengine_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = SimpleNamespace(
        id="job_frames",
        model="bytedance/seedance-2.0",
        prompt="animate a logo",
        entity_id="entity",
        params={"frames": 144, "duration": 5, "resolution": "720p", "aspect_ratio": "1:1"},
    )

    await media_tasks._call_volcengine_seedance_api(job, "volc-native-key-1234567890", None)

    assert captured["payload"]["frames"] == 144
    assert "duration" not in captured["payload"]
    assert captured["payload"]["generate_audio"] is False


@pytest.mark.asyncio
async def test_seedance_native_retries_las_base_when_default_ark_rejects(monkeypatch):
    captured_posts: list[str] = []
    captured_payloads: list[dict] = []
    responses = [
        _FakeResponse(401, {"error": {"message": "wrong key scope"}}),
        _FakeResponse(202, {"id": "task_456"}),
    ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            captured_posts.append(url)
            captured_payloads.append(json)
            return responses.pop(0)

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        return "https://video.test/out.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/out.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_poll_volcengine_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = SimpleNamespace(
        id="job_2",
        model="bytedance/seedance-2.0-fast",
        prompt="text to video",
        entity_id="entity",
        params={"duration": 5, "resolution": "720p", "aspect_ratio": "16:9"},
    )

    result = await media_tasks._call_volcengine_seedance_api(job, "las-native-key-1234567890", None)

    assert result["credits"] == 0
    assert captured_posts == [
        "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
        "https://operator.las.cn-beijing.volces.com/api/v1/contents/generations/tasks",
    ]
    assert captured_payloads[0]["model"] == "doubao-seedance-2-0-fast-260128"
    assert captured_payloads[0]["generate_audio"] is False


@pytest.mark.asyncio
async def test_seedance_fast_downgrades_unsupported_1080p_resolution(monkeypatch):
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
            return _FakeResponse(202, {"id": "task_fast"})

    async def fake_poll(*args, **kwargs) -> str:
        return "https://video.test/fast.mp4"

    async def fake_download(video_url, prompt, model, job_id, entity_id, duration, resolution, **kwargs):
        captured["download"] = {"resolution": resolution}
        return {"result_url": "/api/v1/fs/entity/videos/fast.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_poll_volcengine_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = SimpleNamespace(
        id="job_fast_1080",
        model="bytedance/seedance-2.0-fast",
        prompt="text to video",
        entity_id="entity",
        params={"duration": 10, "resolution": "1080p", "aspect_ratio": "16:9"},
    )

    await media_tasks._call_volcengine_seedance_api(job, "volc-native-key-1234567890", None)

    assert captured["payload"]["model"] == "doubao-seedance-2-0-fast-260128"
    assert captured["payload"]["resolution"] == "720p"
    assert captured["download"]["resolution"] == "720p"


def test_seedance_helpers_normalize_inputs():
    assert (
        media_tasks._normalize_volcengine_base_url(
            "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
        )
        == "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert media_tasks._seedance_duration(1) == 4
    assert media_tasks._provider_safe_reference_filename("frames/hero.jpeg") == "reference.jpg"
    assert media_tasks._provider_safe_reference_filename("frames/hero.png") == "reference.png"
    assert media_tasks._provider_safe_reference_filename("frames/hero") == "reference.png"
    assert media_tasks._seedance_duration("10") == 10
    assert media_tasks._seedance_duration(30) == 15
    assert media_tasks.normalize_video_resolution("bytedance/seedance-2.0-fast", "1080p") == "720p"
    assert media_tasks.normalize_video_resolution("bytedance/seedance-2.0", "1080p") == "1080p"
    assert media_tasks.normalize_video_resolution("bytedance/seedance-2.0", "720") == "720p"
    assert media_tasks._extract_task_id({"data": {"taskId": "task_nested"}}) == "task_nested"


def test_entity_rel_path_rehydrates_valid_signed_public_url(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    entity_root.mkdir()

    from packages.core import config as core_config
    from packages.core.services.file_access_tokens import create_file_access_token

    class _Settings:
        JWT_SECRET_KEY = "test-secret"

    monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())

    token = create_file_access_token(entity_id="entity", rel_path="images/frame.png")
    result = media_tasks._entity_rel_path_from_reference(
        f"https://app.manorai.xyz/api/v1/fs/public/{token}/reference.png",
        "entity",
        str(entity_root),
    )

    assert result == "images/frame.png"


def test_snapshot_video_reference_urls_copies_transient_local_refs(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    source = entity_root / "uploads" / "chat" / "frame.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x89PNG\r\n\x1a\nsnapshot")

    _enable_entity_fs(monkeypatch, entity_root)
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    result = media_tasks.snapshot_video_reference_urls(
        entity_id="entity",
        job_id="job123",
        first_frame_url="/api/v1/fs/entity/uploads/chat/frame.png",
        reference_urls=["uploads/chat/frame.png"],
    )

    first = result["first_frame_url"]
    ref = result["reference_urls"][0]
    assert first.startswith("/api/v1/fs/entity/uploads/media-references/job123/00-first-frame-")
    assert ref.startswith("/api/v1/fs/entity/uploads/media-references/job123/01-reference-1-")

    first_rel = first.split("/api/v1/fs/entity/", 1)[1]
    ref_rel = ref.split("/api/v1/fs/entity/", 1)[1]
    assert (entity_root / first_rel).read_bytes() == source.read_bytes()
    assert (entity_root / ref_rel).read_bytes() == source.read_bytes()


def test_snapshot_video_reference_urls_rejects_missing_local_ref(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    _enable_entity_fs(monkeypatch, entity_root)
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    with pytest.raises(FileNotFoundError, match="Media reference not found"):
        media_tasks.snapshot_video_reference_urls(
            entity_id="entity",
            job_id="job123",
            first_frame_url="/api/v1/fs/entity/uploads/chat/missing.png",
        )


def test_snapshot_video_reference_urls_rejects_malformed_entity_fs_ref(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    _enable_entity_fs(monkeypatch, entity_root)
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    with pytest.raises(ValueError, match="entity segment"):
        media_tasks.snapshot_video_reference_urls(
            entity_id="entity",
            job_id="job123",
            first_frame_url="/api/v1/fs/project/storyboards/frame.png",
        )


def test_snapshot_video_reference_urls_saves_data_uri_as_file(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    _enable_entity_fs(monkeypatch, entity_root)
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    result = media_tasks.snapshot_video_reference_urls(
        entity_id="entity",
        job_id="job123",
        first_frame_url="data:image/png;base64,iVBORw0KGgpjYXQ=",
    )

    first = result["first_frame_url"]
    assert first.startswith("/api/v1/fs/entity/uploads/media-references/job123/00-first-frame-")
    first_rel = first.split("/api/v1/fs/entity/", 1)[1]
    assert (entity_root / first_rel).read_bytes().startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_kling_native_defaults_to_official_singapore_api(monkeypatch):
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
            return _FakeResponse(202, {"data": {"task_id": "kling_task_1"}})

    async def fake_remember(*args, **kwargs):
        captured["remember"] = args

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        captured["poll_url"] = poll_url
        return "https://video.test/kling.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/kling.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_remember_provider_poll", fake_remember)
    monkeypatch.setattr(media_tasks, "_poll_generic_video_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = SimpleNamespace(
        id="job_kling",
        model="kwaivgi/kling-v3.0-pro",
        prompt="a cat reading a newspaper",
        entity_id="entity",
        params={"duration": 10, "resolution": "1080p", "aspect_ratio": "1:1"},
        agent_id=None,
        conversation_id=None,
        user_id=None,
    )

    result = await media_tasks._call_kling_api(job, "jwt-token", None)

    assert result["result_url"].endswith("/kling.mp4")
    assert captured["url"] == "https://api-singapore.klingai.com/v1/videos/text2video"
    assert captured["headers"]["Authorization"] == "Bearer jwt-token"
    assert captured["payload"]["model_name"] == "kling-v3"
    assert captured["payload"]["mode"] == "pro"
    assert captured["payload"]["duration"] == "10"
    assert "model" not in captured["payload"]
    assert captured["poll_url"] == "https://api-singapore.klingai.com/v1/videos/text2video/kling_task_1"


@pytest.mark.asyncio
async def test_kling_official_image_to_video_uses_image_field(monkeypatch):
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
            captured["payload"] = json
            return _FakeResponse(202, {"data": {"task_id": "kling_i2v_1"}})

    async def fake_public_url(url: str, entity_id: str, **kwargs) -> str:
        assert kwargs == {
            "allow_data_uri": False,
            "expires_in_seconds": media_tasks.MEDIA_REFERENCE_URL_EXPIRES_SECONDS,
        }
        return "https://manor.example.test/public/frame.png"

    async def fake_remember(*args, **kwargs):
        return None

    async def fake_poll(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
        captured["poll_url"] = poll_url
        return "https://video.test/kling-i2v.mp4"

    async def fake_download(*args, **kwargs):
        return {"result_url": "/api/v1/fs/entity/videos/kling-i2v.mp4", "credits": 0, "cost_usd": 0}

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "_ensure_public_url", fake_public_url)
    monkeypatch.setattr(media_tasks, "_remember_provider_poll", fake_remember)
    monkeypatch.setattr(media_tasks, "_poll_generic_video_task", fake_poll)
    monkeypatch.setattr(media_tasks, "_download_and_save", fake_download)

    job = SimpleNamespace(
        id="job_kling_i2v",
        model="kwaivgi/kling-v3.0-std",
        prompt="camera slowly pushes in",
        entity_id="entity",
        params={
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "first_frame_url": "/api/v1/fs/entity/images/frame.png",
        },
        agent_id=None,
        conversation_id=None,
        user_id=None,
    )

    await media_tasks._call_kling_api(job, "jwt-token", None)

    assert captured["url"] == "https://api-singapore.klingai.com/v1/videos/image2video"
    assert captured["payload"]["model_name"] == "kling-v3"
    assert captured["payload"]["mode"] == "std"
    assert captured["payload"]["image"] == "https://manor.example.test/public/frame.png"
    assert "image_url" not in captured["payload"]
    assert captured["poll_url"] == "https://api-singapore.klingai.com/v1/videos/image2video/kling_i2v_1"


def test_kling_base_url_candidates_use_official_domain_first():
    assert media_tasks._kling_base_url_candidates(None)[:2] == [
        "https://api-singapore.klingai.com",
        "https://api.klingai.com",
    ]
    assert media_tasks._kling_base_url_candidates("https://api.klingai.com/v1/videos/text2video") == [
        "https://api.klingai.com",
    ]


@pytest.mark.asyncio
async def test_ensure_public_url_reads_nested_entity_fs_paths(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    image_path = entity_root / "images" / "frame.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-bytes")

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    result = await media_tasks._ensure_public_url(
        "/api/v1/fs/entity/images/frame.png",
        "entity",
    )

    assert result.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_ensure_public_url_reads_knowledge_relative_paths(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    image_path = entity_root / "猫咪打工人动漫" / "images" / "场景_拉面店_傍晚.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-bytes")

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    result = await media_tasks._ensure_public_url(
        "猫咪打工人动漫/images/场景_拉面店_傍晚.png",
        "entity",
    )

    assert result.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_ensure_public_url_rejects_malformed_entity_fs_path(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    with pytest.raises(RuntimeError, match="entity segment"):
        await media_tasks._ensure_public_url(
            "/api/v1/fs/project/storyboards/frame.png",
            "entity",
            allow_data_uri=False,
        )


@pytest.mark.asyncio
async def test_ensure_public_url_rejects_data_uri_when_url_only(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    image_path = entity_root / "猫咪打工人动漫" / "images" / "场景_拉面店_傍晚.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-bytes")

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        await media_tasks._ensure_public_url(
            "猫咪打工人动漫/images/场景_拉面店_傍晚.png",
            "entity",
            allow_data_uri=False,
        )


@pytest.mark.asyncio
async def test_ensure_public_url_returns_signed_https_url_when_public_base_exists(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    image_path = entity_root / "images" / "frame.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-bytes")

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    from packages.core import config as core_config

    class _Settings:
        JWT_SECRET_KEY = "test-secret"
        PUBLIC_BASE_URL = "https://manor.example.test"

    monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())
    monkeypatch.setattr(media_tasks, "get_settings", lambda: _Settings(), raising=False)

    result = await media_tasks._ensure_public_url(
        "/api/v1/fs/entity/images/frame.png",
        "entity",
        allow_data_uri=False,
    )

    assert result.startswith("https://manor.example.test/api/v1/fs/public/")
    assert result.endswith("/reference.png")


@pytest.mark.asyncio
async def test_ensure_public_url_uses_job_public_base_override(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    image_path = entity_root / "images" / "frame.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-bytes")

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    from packages.core import config as core_config

    class _Settings:
        JWT_SECRET_KEY = "test-secret"
        PUBLIC_BASE_URL = "http://localhost:8000"

    monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())
    monkeypatch.setattr(media_tasks, "get_settings", lambda: _Settings(), raising=False)

    result = await media_tasks._ensure_public_url(
        "/api/v1/fs/entity/images/frame.png",
        "entity",
        allow_data_uri=False,
        public_base_url="https://api.example.test",
    )

    assert result.startswith("https://api.example.test/api/v1/fs/public/")
    assert result.endswith("/reference.png")


@pytest.mark.asyncio
async def test_ensure_public_url_preflights_real_public_base(monkeypatch, tmp_path):
    entity_root = tmp_path / "entity"
    image_path = entity_root / "images" / "frame.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png-bytes")

    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    from packages.core import config as core_config

    class _Settings:
        JWT_SECRET_KEY = "test-secret"
        PUBLIC_BASE_URL = "https://app.manorai.xyz"

    async def fake_preflight(url: str):
        assert url.startswith("https://app.manorai.xyz/api/v1/fs/public/")
        return False, "HTTP 404"

    monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())
    monkeypatch.setattr(media_tasks, "_preflight_public_media_url", fake_preflight)

    with pytest.raises(RuntimeError, match="not fetchable"):
        await media_tasks._ensure_public_url(
            "/api/v1/fs/entity/images/frame.png",
            "entity",
            allow_data_uri=False,
        )


@pytest.mark.asyncio
async def test_preflight_public_media_url_retries_transient_502(monkeypatch):
    statuses = [502, 200]
    calls: list[str] = []

    class _PreflightResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.request = SimpleNamespace(method="HEAD")
            self.text = ""

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def head(self, url):
            calls.append(url)
            return _PreflightResponse(statuses.pop(0))

        async def get(self, url, headers=None):
            raise AssertionError("GET fallback should not be used")

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "MEDIA_REFERENCE_URL_PREFLIGHT_ATTEMPTS", 3)
    monkeypatch.setattr(media_tasks, "MEDIA_REFERENCE_URL_PREFLIGHT_RETRY_DELAY_SECONDS", 0)

    ok, reason = await media_tasks._preflight_public_media_url(
        "https://app.manorai.xyz/api/v1/fs/public/token/reference.jpg"
    )

    assert ok is True
    assert reason == ""
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_preflight_public_media_url_does_not_retry_missing_file(monkeypatch):
    calls: list[str] = []

    class _PreflightResponse:
        status_code = 404
        request = SimpleNamespace(method="HEAD")
        text = ""

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def head(self, url):
            calls.append(url)
            return _PreflightResponse()

        async def get(self, url, headers=None):
            raise AssertionError("GET fallback should not be used")

    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(media_tasks, "MEDIA_REFERENCE_URL_PREFLIGHT_ATTEMPTS", 4)
    monkeypatch.setattr(media_tasks, "MEDIA_REFERENCE_URL_PREFLIGHT_RETRY_DELAY_SECONDS", 0)

    ok, reason = await media_tasks._preflight_public_media_url(
        "https://app.manorai.xyz/api/v1/fs/public/token/reference.jpg"
    )

    assert ok is False
    assert reason == "HTTP 404"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ensure_public_url_signs_even_when_worker_file_missing(monkeypatch, tmp_path):
    entity_root = tmp_path / "worker-without-upload"
    entity_root.mkdir()
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda entity_id: str(entity_root))

    from packages.core import config as core_config

    class _Settings:
        JWT_SECRET_KEY = "test-secret"
        PUBLIC_BASE_URL = "https://manor.example.test"

    monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())
    monkeypatch.setattr(media_tasks, "get_settings", lambda: _Settings(), raising=False)

    result = await media_tasks._ensure_public_url(
        "/api/v1/fs/entity/uploads/chat/frame.png",
        "entity",
        allow_data_uri=False,
    )

    assert result.startswith("https://manor.example.test/api/v1/fs/public/")
    assert result.endswith("/reference.png")
