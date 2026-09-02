from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import httpx

from packages.core.services import nango_bootstrap


def test_nango_bootstrap_default_webhook_uses_manor_api_service_with_derived_token(monkeypatch):
    monkeypatch.delenv("NANGO_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NANGO_SECRET_KEY", raising=False)
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)

    url = nango_bootstrap._resolve_webhook_url()

    assert url is not None
    parsed = urlsplit(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "http://manor-api:8000/api/v1/nango/webhook"
    )
    assert parse_qs(parsed.query) == {
        "nango_webhook_token": [nango_bootstrap._derive_webhook_token()]
    }
    assert "a" * 64 not in url


def test_nango_bootstrap_webhook_url_override_preserves_query_and_replaces_token(monkeypatch):
    monkeypatch.setenv(
        "NANGO_WEBHOOK_URL",
        " https://example.test/nango?source=operator&nango_webhook_token=wrong ",
    )
    monkeypatch.delenv("NANGO_CONNECT_HMAC_KEY", raising=False)
    monkeypatch.setenv("NANGO_SECRET_KEY", "compose-secret")

    url = nango_bootstrap._resolve_webhook_url()

    assert url is not None
    assert parse_qs(urlsplit(url).query) == {
        "source": ["operator"],
        "nango_webhook_token": [nango_bootstrap._derive_webhook_token()],
    }
    assert "compose-secret" not in url


def test_nango_bootstrap_webhook_token_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("NANGO_CONNECT_HMAC_KEY", raising=False)
    monkeypatch.delenv("NANGO_SECRET_KEY", raising=False)

    assert nango_bootstrap._derive_webhook_token() is None
    assert nango_bootstrap._resolve_webhook_url() is None


def test_nango_bootstrap_webhook_token_prefers_connect_hmac_key(monkeypatch):
    hmac_key = "c" * 64
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", hmac_key)
    monkeypatch.setenv("NANGO_SECRET_KEY", "compose-secret")

    assert nango_bootstrap._derive_webhook_token() == hmac.new(
        hmac_key.encode("ascii"),
        b"manor-nango-webhook-v1",
        hashlib.sha256,
    ).hexdigest()


def test_nango_bootstrap_requires_authenticated_environment_lookup(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(503, text="database unavailable")

    monkeypatch.setenv("NANGO_BASE_URL", "http://nango-server:3003")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)
    async def exercise() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await nango_bootstrap._set_webhook_settings(client, "secret")

    result = asyncio.run(exercise())

    assert result.startswith("error: environment lookup 503")
    assert calls == ["GET /api/v1/environment"]


def test_nango_bootstrap_webhook_status_does_not_include_derived_url(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json={"account": {}})
        return httpx.Response(204)

    monkeypatch.setenv("NANGO_BASE_URL", "http://nango-server:3003")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)
    async def exercise() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await nango_bootstrap._set_webhook_settings(client, "secret")

    result = asyncio.run(exercise())

    assert result == "updated"
    assert calls == [
        "GET /api/v1/environment",
        "POST /api/v1/environment/webhook",
    ]


def test_nango_bootstrap_configures_connect_hmac_key_and_enables_it(monkeypatch):
    calls: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    monkeypatch.setenv("NANGO_BASE_URL", "http://nango-server:3003")
    async def exercise() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await nango_bootstrap._set_connect_hmac_settings(
                client,
                "secret",
                "a" * 64,
            )

    result = asyncio.run(exercise())

    assert result == "updated"
    assert calls == [
        ("POST", "/api/v1/environment/hmac-key", {"hmac_key": "a" * 64}),
        ("POST", "/api/v1/environment/hmac-enabled", {"hmac_enabled": True}),
    ]


def test_nango_bootstrap_reports_missing_connect_hmac_key_without_request(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(204)

    monkeypatch.setenv("NANGO_BASE_URL", "http://nango-server:3003")
    async def exercise() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await nango_bootstrap._set_connect_hmac_settings(client, "secret", "")

    result = asyncio.run(exercise())

    assert result == "error: NANGO_CONNECT_HMAC_KEY not set"
    assert calls == []


def test_nango_bootstrap_creates_providers_before_environment_settings(monkeypatch):
    calls: list[str] = []
    provider = {
        "provider_config_key": "facebook",
        "provider": "facebook",
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
    }

    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)
    monkeypatch.setattr(nango_bootstrap, "_parse_provider_envs", lambda: [provider])

    async def put_provider(*_args):
        calls.append("provider")
        return "created"

    async def set_hmac(*_args):
        calls.append("hmac")
        return "updated"

    async def set_webhook(*_args):
        calls.append("webhook")
        return "updated"

    monkeypatch.setattr(nango_bootstrap, "_put_provider_config", put_provider)
    monkeypatch.setattr(nango_bootstrap, "_set_connect_hmac_settings", set_hmac)
    monkeypatch.setattr(nango_bootstrap, "_set_webhook_settings", set_webhook)

    result = asyncio.run(nango_bootstrap.seed_nango_from_env())

    assert calls == ["provider", "hmac", "webhook"]
    assert result == {
        "providers": {"facebook": "created"},
        "webhook": "updated",
        "hmac": "updated",
    }


def test_nango_bootstrap_reports_an_incomplete_provider_bundle(monkeypatch):
    monkeypatch.setenv("NANGO_SECRET_KEY", "secret")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)
    monkeypatch.setenv("NANGO_PROVIDER_LINKEDIN_CLIENT_ID", "client-id")
    monkeypatch.delenv("NANGO_PROVIDER_LINKEDIN_CLIENT_SECRET", raising=False)

    async def successful_hmac(*_args, **_kwargs) -> str:
        return "updated"

    async def successful_webhook(*_args, **_kwargs) -> str:
        return "updated"

    monkeypatch.setattr(nango_bootstrap, "_set_connect_hmac_settings", successful_hmac)
    monkeypatch.setattr(nango_bootstrap, "_set_webhook_settings", successful_webhook)

    result = asyncio.run(nango_bootstrap.seed_nango_from_env())

    assert result["providers"] == {
        "linkedin": "error: missing client_secret",
    }


def test_standalone_bootstrap_exits_nonzero_for_any_seed_failure(monkeypatch, capsys):
    from scripts import bootstrap_nango

    async def failed_seed():
        return {
            "providers": {"linkedin": "error: provider rejected"},
            "webhook": "updated",
            "hmac": "updated",
        }

    monkeypatch.setattr(bootstrap_nango, "seed_nango_from_env", failed_seed)

    try:
        bootstrap_nango.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("bootstrap must fail the Job")

    output = capsys.readouterr().out
    assert "linkedin=error" in output
    assert "provider rejected" not in output


def test_standalone_bootstrap_exits_nonzero_for_connect_hmac_failure(monkeypatch, capsys):
    from scripts import bootstrap_nango

    async def failed_seed():
        return {
            "providers": {},
            "webhook": "updated",
            "hmac": "error: hmac key 503 unavailable",
        }

    monkeypatch.setattr(bootstrap_nango, "seed_nango_from_env", failed_seed)

    try:
        bootstrap_nango.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("bootstrap must fail the Job")

    output = capsys.readouterr().out
    assert "hmac=error" in output
    assert "unavailable" not in output
