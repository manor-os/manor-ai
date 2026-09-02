"""Focused health contracts for the YouTube integration."""
from __future__ import annotations

import pytest

import packages.core.services.integration_health as health_mod


class _Response:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self.text = "" if body is None else "{}"
        self._body = body or {}

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_youtube_health_checks_authenticated_channel_endpoint(monkeypatch):
    calls: list[dict] = []

    async def fake_http_get(url, *, headers=None, timeout=10):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _Response(200, {"items": [{"id": "UC-test"}]})

    monkeypatch.setattr(health_mod, "_http_get", fake_http_get)

    result = await health_mod.test_youtube({"access_token": "youtube-token"})

    assert result["ok"] is True
    assert calls == [{
        "url": "https://www.googleapis.com/youtube/v3/channels?part=id&mine=true",
        "headers": {"Authorization": "Bearer youtube-token"},
        "timeout": 10,
    }]


@pytest.mark.asyncio
async def test_youtube_health_rejects_expired_token(monkeypatch):
    async def fake_http_get(url, *, headers=None, timeout=10):
        return _Response(401, {"error": {"message": "Login Required"}})

    monkeypatch.setattr(health_mod, "_http_get", fake_http_get)

    result = await health_mod.test_youtube({"access_token": "expired"})

    assert result["ok"] is False
    assert "reconnect" in result["detail"].lower()
