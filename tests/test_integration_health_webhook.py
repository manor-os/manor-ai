from __future__ import annotations

import pytest


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Client:
    response: _Response

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def head(self, _url: str):
        return self.response

    async def options(self, _url: str):
        return self.response


@pytest.mark.asyncio
async def test_webhook_health_rejects_auth_failure(monkeypatch):
    from packages.core.services import integration_health

    client = _Client
    client.response = _Response(401)
    monkeypatch.setattr(integration_health.httpx, "AsyncClient", client)

    result = await integration_health.test_webhook({"url": "https://hooks.example.test/events"})

    assert result["ok"] is False
    assert result["reason_code"] == "credentials_rejected"


@pytest.mark.asyncio
async def test_webhook_health_reports_missing_route_for_not_found(monkeypatch):
    from packages.core.services import integration_health

    client = _Client
    client.response = _Response(404)
    monkeypatch.setattr(integration_health.httpx, "AsyncClient", client)

    result = await integration_health.test_webhook({"url": "https://hooks.example.test/events"})

    assert result["ok"] is False
    assert result["reason_code"] == "webhook_endpoint_not_found"
