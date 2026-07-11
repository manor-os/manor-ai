from __future__ import annotations

import pytest

import packages.core.tasks.media_tasks as media_tasks


class _FakeResponse:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def json(self) -> dict:
        return self._data


class _FakeAsyncClient:
    next_response: _FakeResponse
    requested_url: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, headers):
        self.__class__.requested_url = url
        return self.next_response


@pytest.mark.asyncio
async def test_provider_poll_once_completed_unsigned_url(monkeypatch):
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requested_url = None
    _FakeAsyncClient.next_response = _FakeResponse(
        200,
        {"status": "completed", "unsigned_urls": ["https://cdn.test/out.mp4"]},
    )

    result = await media_tasks._check_provider_poll_once(
        "https://openrouter.test/videos/job",
        {"Authorization": "Bearer test"},
        provider="openrouter",
    )

    assert result == {"video_url": "https://cdn.test/out.mp4"}


@pytest.mark.asyncio
async def test_provider_poll_once_openrouter_completed_uses_content_endpoint(monkeypatch):
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requested_url = None
    _FakeAsyncClient.next_response = _FakeResponse(200, {"status": "completed"})

    result = await media_tasks._check_provider_poll_once(
        "/api/v1/videos/job-content-only",
        {"Authorization": "Bearer test"},
        provider="openrouter",
    )

    assert _FakeAsyncClient.requested_url == "https://openrouter.ai/api/v1/videos/job-content-only"
    assert result == {"video_url": "https://openrouter.ai/api/v1/videos/job-content-only/content?index=0"}


@pytest.mark.asyncio
async def test_provider_poll_once_failed_error_message(monkeypatch):
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(
        200,
        {
            "status": "failed",
            "error": {
                "message": "The request failed because the output video may be related to copyright restrictions."
            },
        },
    )

    result = await media_tasks._check_provider_poll_once(
        "https://openrouter.test/videos/job",
        {"Authorization": "Bearer test"},
        provider="openrouter",
    )

    assert "copyright restrictions" in result["error"]


@pytest.mark.asyncio
async def test_provider_poll_once_pending(monkeypatch):
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.next_response = _FakeResponse(200, {"status": "pending"})

    result = await media_tasks._check_provider_poll_once(
        "https://openrouter.test/videos/job",
        {"Authorization": "Bearer test"},
        provider="openrouter",
    )

    assert result == {"status": "pending"}


@pytest.mark.asyncio
async def test_openrouter_long_poll_completed_uses_content_endpoint(monkeypatch):
    monkeypatch.setattr(media_tasks.httpx, "AsyncClient", _FakeAsyncClient)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(media_tasks.asyncio, "sleep", no_sleep)
    _FakeAsyncClient.requested_url = None
    _FakeAsyncClient.next_response = _FakeResponse(200, {"status": "completed"})

    result = await media_tasks._poll_video_generation(
        "/api/v1/videos/job-without-unsigned-url",
        {"Authorization": "Bearer test"},
        timeout=1,
    )

    assert _FakeAsyncClient.requested_url == "https://openrouter.ai/api/v1/videos/job-without-unsigned-url"
    assert result == "https://openrouter.ai/api/v1/videos/job-without-unsigned-url/content?index=0"
