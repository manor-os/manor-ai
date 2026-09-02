"""Health-check credential-lease error handling.

A stored credential whose ciphertext can no longer be decrypted (e.g. it
predates a Vault transit key change) makes ``lease_integration`` raise
``CredentialDecryptError`` on every health tick. That is an expected,
operator-actionable state — not an unexpected crash — so the health check
should mark the integration as needing reconnection and log concisely,
while genuinely unexpected errors stay generic.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import packages.core.credentials as credentials_mod
import packages.core.services.oauth_account_credentials as oauth_credentials_mod
import packages.core.services.integration_health as health_mod
from packages.core.credentials import CredentialDecryptError
from packages.core.services.integration_health import (
    run_and_persist_integration,
    run_and_persist_oauth,
)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row
        self.flush_count = 0

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._row)

    async def flush(self):
        self.flush_count += 1


class _FakeIntegration:
    id = "int_1"
    provider = "slack"
    config: dict = {}


class _FakeOAuthAccount:
    id = "oauth_1"
    provider = "gmail"
    profile: dict = {}
    token_expires_at = object()


def _set_whatsapp_deployment_config(monkeypatch) -> None:
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", "meta-app-secret")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "whatsapp-verify-token")


@pytest.mark.asyncio
async def test_stripe_health_accepts_oauth_access_token(monkeypatch):
    seen: dict = {}

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            seen.update({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr(health_mod.httpx, "AsyncClient", Client)

    result = await health_mod.test_stripe({"access_token": "stripe-oauth-token"})

    assert result["ok"] is True
    assert seen["auth"] == ("stripe-oauth-token", "")


@pytest.mark.asyncio
async def test_quickbooks_health_uses_saved_realm_company_info(monkeypatch):
    seen: dict = {}

    class Response:
        status_code = 200
        text = ""

    async def fake_get(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setenv("QUICKBOOKS_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(health_mod, "_http_get", fake_get)

    result = await health_mod.test_quickbooks(
        {"access_token": "qb-access", "realm_id": "realm/123"}
    )

    assert result["ok"] is True
    assert seen["url"] == (
        "https://sandbox-quickbooks.api.intuit.com/v3/company/"
        "realm%2F123/companyinfo/realm%2F123"
    )
    assert seen["headers"]["Authorization"] == "Bearer qb-access"


@pytest.mark.asyncio
async def test_quickbooks_oauth_health_merges_saved_realm_id(monkeypatch):
    seen: dict = {}

    class Response:
        status_code = 200
        text = ""

    async def fake_get(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    row = _FakeOAuthAccount()
    row.provider = "quickbooks"
    row.profile = {"realm_id": "realm/123"}
    db = _FakeDB(row)
    monkeypatch.setenv("QUICKBOOKS_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(health_mod, "_http_get", fake_get)
    monkeypatch.setattr(
        oauth_credentials_mod,
        "lease_oauth_account_tokens",
        lambda *_args, **_kwargs: {"access_token": "qb-access"},
    )

    result = await run_and_persist_oauth(db, row.id)

    assert result["ok"] is True
    assert seen["url"] == (
        "https://sandbox-quickbooks.api.intuit.com/v3/company/"
        "realm%2F123/companyinfo/realm%2F123"
    )
    assert row.profile["realm_id"] == "realm/123"
    assert row.profile["last_health_check"]["ok"] is True


@pytest.mark.asyncio
async def test_tavily_health_validates_key_with_free_usage_endpoint(monkeypatch):
    seen: dict = {}

    class Response:
        status_code = 200
        text = ""

    async def fake_get(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(health_mod, "_http_get", fake_get)

    result = await health_mod.test_tavily({"api_key": "tvly-test-key"})

    assert result["ok"] is True
    assert seen == {
        "url": "https://api.tavily.com/usage",
        "headers": {"Authorization": "Bearer tvly-test-key"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_tavily_health_rejects_upstream_auth_failure(monkeypatch, status_code):
    class Response:
        text = "invalid key"

        def __init__(self, status):
            self.status_code = status

    async def fake_get(*_args, **_kwargs):
        return Response(status_code)

    monkeypatch.setattr(health_mod, "_http_get", fake_get)

    result = await health_mod.test_tavily({"api_key": "tvly-test-key"})

    assert result["ok"] is False
    assert str(status_code) in result["detail"]


@pytest.mark.asyncio
async def test_tavily_health_rejects_malformed_key_without_http(monkeypatch):
    http_attempts = 0

    async def unexpected_get(*_args, **_kwargs):
        nonlocal http_attempts
        http_attempts += 1
        raise AssertionError("Malformed Tavily keys must fail locally")

    monkeypatch.setattr(health_mod, "_http_get", unexpected_get)

    result = await health_mod.test_tavily({"api_key": "not-a-tavily-key"})

    assert result["ok"] is False
    assert "tvly-" in result["detail"]
    assert http_attempts == 0


def _service_raising(exc: Exception):
    class _Service:
        def lease_integration(self, row, **kwargs):
            raise exc

        def lease_oauth_account(self, row, **kwargs):
            raise exc

    return lambda: _Service()


@pytest.mark.asyncio
async def test_decrypt_failure_marks_integration_needs_reconnect(monkeypatch, caplog):
    monkeypatch.setattr(
        credentials_mod,
        "get_credential_service",
        _service_raising(CredentialDecryptError("cipher: message authentication failed")),
    )

    with caplog.at_level(logging.ERROR):
        result = await run_and_persist_integration(_FakeDB(_FakeIntegration()), "int_1")

    assert result["ok"] is False
    assert result["needs_reconnect"] is True
    assert "reconnect" in result["detail"].lower()
    # Expected, recurring state — must not spam a full traceback at ERROR level.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_unexpected_lease_error_stays_generic(monkeypatch):
    monkeypatch.setattr(
        credentials_mod,
        "get_credential_service",
        _service_raising(RuntimeError("vault unreachable")),
    )

    result = await run_and_persist_integration(_FakeDB(_FakeIntegration()), "int_1")

    assert result["ok"] is False
    assert "needs_reconnect" not in result


@pytest.mark.asyncio
async def test_decrypt_failure_marks_oauth_needs_reconnect(monkeypatch, caplog):
    service_factory = _service_raising(
        CredentialDecryptError("cipher: message authentication failed")
    )
    monkeypatch.setattr(credentials_mod, "get_credential_service", service_factory)
    monkeypatch.setattr(
        oauth_credentials_mod,
        "get_credential_service",
        service_factory,
    )
    row = _FakeOAuthAccount()
    row.profile = {}
    row.token_expires_at = object()
    db = _FakeDB(row)

    with caplog.at_level(logging.ERROR):
        result = await run_and_persist_oauth(db, row.id)

    assert result["ok"] is False
    assert result["needs_reconnect"] is True
    assert row.profile["oauth_refresh"]["error"] == "credential_decrypt_failed"
    assert row.profile["last_health_check"]["ok"] is False
    assert row.token_expires_at is None
    assert db.flush_count == 1
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_whatsapp_nango_health_resolves_phone_number_id(monkeypatch):
    _set_whatsapp_deployment_config(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "credentials": {
                    "access_token": "token-from-nango",
                    "phone_number_id": "phone-from-nango",
                    "waba_id": "waba-from-nango",
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(health_mod.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        "packages.core.ai.mcp.nango.get_nango_secret",
        lambda *_args, **_kwargs: _async_secret("nango-secret"),
    )

    row = SimpleNamespace(
        entity_id="entity-1",
        provider="whatsapp",
        config={
            "nango": {
                "provider_config_key": "whatsapp",
                "connection_id": "entity--user--whatsapp",
            }
        },
    )
    resolved = await health_mod._resolve_nango_runtime_credentials(
        None,
        row,
        {
            "via": "nango",
            "provider_config_key": "whatsapp",
            "connection_id": "entity--user--whatsapp",
        },
    )

    assert resolved["access_token"] == "token-from-nango"
    assert resolved["phone_number_id"] == "phone-from-nango"
    assert resolved["waba_id"] == "waba-from-nango"


@pytest.mark.asyncio
async def test_whatsapp_nango_health_falls_back_to_synced_phone_number_id(monkeypatch):
    _set_whatsapp_deployment_config(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"credentials": {"access_token": "token-from-nango"}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(health_mod.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        "packages.core.ai.mcp.nango.get_nango_secret",
        lambda *_args, **_kwargs: _async_secret("nango-secret"),
    )

    row = SimpleNamespace(
        entity_id="entity-1",
        provider="whatsapp",
        config={
            "nango": {
                "provider_config_key": "whatsapp",
                "connection_id": "entity--user--whatsapp",
            },
            "whatsapp": {"phone_number_id": "phone-from-sync"},
        },
    )
    resolved = await health_mod._resolve_nango_runtime_credentials(
        None,
        row,
        {
            "via": "nango",
            "provider_config_key": "whatsapp",
            "connection_id": "entity--user--whatsapp",
        },
    )

    assert resolved["access_token"] == "token-from-nango"
    assert resolved["phone_number_id"] == "phone-from-sync"


@pytest.mark.asyncio
async def test_whatsapp_health_checks_subscriptions_on_waba(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        is_success = True

        def json(self):
            return {"data": [{"id": "app-1"}]}

    async def fake_http_get(url, *, headers=None, timeout=10):
        calls.append(url)
        return Response()

    monkeypatch.setattr(health_mod, "_http_get", fake_http_get)

    result = await health_mod.test_whatsapp(
        {
            "phone_number_id": "phone-1",
            "waba_id": "waba-1",
            "access_token": "token-1",
        },
        wiring_ctx={"expected_url": ""},
    )

    assert result["ok"] is True
    assert result["wiring"]["ok"] is True
    assert calls == [
        f"{health_mod._META_BASE}/phone-1",
        f"{health_mod._META_BASE}/waba-1/subscribed_apps",
    ]


@pytest.mark.asyncio
async def test_whatsapp_wiring_access_denied_is_reported_as_unavailable(monkeypatch):
    class Response:
        status_code = 400
        is_success = False
        text = '{"error":{"message":"API access blocked.","code":200}}'

        def json(self):
            return {"error": {"message": "API access blocked.", "code": 200}}

    async def fake_http_get(url, *, headers=None, timeout=10):
        if url.endswith("/phone-1"):
            class PhoneResponse:
                status_code = 200
                is_success = True

            return PhoneResponse()
        return Response()

    monkeypatch.setattr(health_mod, "_http_get", fake_http_get)

    result = await health_mod.test_whatsapp(
        {
            "phone_number_id": "phone-1",
            "waba_id": "waba-1",
            "access_token": "token-1",
        },
        wiring_ctx={"expected_url": ""},
    )

    assert result["ok"] is False
    assert result["reason_code"] == "webhook_not_ready"
    assert result["wiring"]["ok"] is None
    assert "Assign the app" in result["wiring"]["detail"]


@pytest.mark.asyncio
async def test_whatsapp_missing_subscription_fails_top_level_health(monkeypatch):
    class Response:
        status_code = 200
        is_success = True
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def fake_http_get(url, *, headers=None, timeout=10):
        if url.endswith("/phone-1"):
            return Response({"id": "phone-1"})
        return Response({"data": []})

    monkeypatch.setattr(health_mod, "_http_get", fake_http_get)

    result = await health_mod.test_whatsapp(
        {
            "phone_number_id": "phone-1",
            "waba_id": "waba-1",
            "access_token": "token-1",
        },
        wiring_ctx={"expected_url": ""},
    )

    assert result["ok"] is False
    assert result["reason_code"] == "webhook_not_ready"
    assert result["wiring"]["ok"] is False


class _WhatsAppHealthDB:
    def __init__(self, integration, channel_config):
        self.integration = integration
        self.channel_config = channel_config
        self.execute_count = 0
        self.flush_count = 0

    async def execute(self, *_args, **_kwargs):
        self.execute_count += 1
        if self.execute_count == 1:
            return _FakeResult(self.integration)
        if self.execute_count == 2:
            return _FakeResult(self.channel_config)
        raise AssertionError("WhatsApp readiness must not query Agent Binding state")

    async def flush(self):
        self.flush_count += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_code", "expected_ok", "expected_status"),
    [
        ("oauth_missing", "oauth_missing", False, "failed"),
        ("asset_mismatch", "asset_mismatch", False, "failed"),
        (
            "phone_not_registered",
            "phone_not_registered",
            False,
            "registration_required",
        ),
        ("app_not_subscribed", "app_not_subscribed", False, "failed"),
        ("callback_not_ready", "callback_not_ready", False, "failed"),
        ("provider_unavailable", "provider_unavailable", False, "failed"),
        ("ready", "ready", True, "ready"),
    ],
)
async def test_whatsapp_account_health_reports_exact_readiness_without_binding(
    monkeypatch,
    scenario,
    expected_code,
    expected_ok,
    expected_status,
):
    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
        WhatsAppProvisioningResult,
    )

    _set_whatsapp_deployment_config(monkeypatch)
    nango_config = {
        "provider_config_key": "whatsapp",
        "connection_id": "entity--user--whatsapp--connection",
    }
    if scenario == "oauth_missing":
        nango_config.pop("connection_id")
    integration = SimpleNamespace(
        id="wa-integration",
        entity_id="entity-1",
        provider="whatsapp",
        status="active",
        config={
            "nango": nango_config,
            "whatsapp": {
                "waba_id": "waba-1",
                "phone_number_id": "phone-1",
                "provisioning_status": "ready",
                "readiness_code": "ready",
            },
        },
    )
    channel_config = SimpleNamespace(
        id="channel-config-1",
        whatsapp_phone_number_id=(
            "different-phone" if scenario == "asset_mismatch" else "phone-1"
        ),
    )
    db = _WhatsAppHealthDB(integration, channel_config)

    class _Service:
        def lease_integration(self, *_args, **_kwargs):
            return {
                "via": "nango",
                "provider_config_key": "whatsapp",
                "connection_id": "entity--user--whatsapp--connection",
            }

    class _NangoResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "credentials": {
                    "access_token": "customer-access-token",
                    "waba_id": "waba-1",
                    "phone_number_id": "phone-1",
                }
            }

    class _NangoClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return _NangoResponse()

    async def fake_inspect(**_kwargs):
        if scenario == "provider_unavailable":
            raise RuntimeError("Meta Graph temporarily unavailable")
        phone_state = (
            WhatsAppPhoneState.REGISTRATION_REQUIRED
            if scenario == "phone_not_registered"
            else WhatsAppPhoneState.CONNECTED
        )
        exact_app_subscribed = scenario != "app_not_subscribed"
        return WhatsAppProvisioningResult(
            ok=(
                phone_state is WhatsAppPhoneState.CONNECTED
                and exact_app_subscribed
            ),
            phone_state=phone_state,
            exact_app_subscribed=exact_app_subscribed,
            phone_number_id="phone-1",
            waba_id="waba-1",
            app_id="meta-app-id",
            detail="provider inspection",
        )

    async def fake_callback(**_kwargs):
        return SimpleNamespace(
            ok=scenario != "callback_not_ready",
            configured_url=(
                "https://wrong.example/api/v1/channels/whatsapp/webhook"
                if scenario == "callback_not_ready"
                else "https://staging.example/api/v1/channels/whatsapp/webhook"
            ),
            expected_url="https://staging.example/api/v1/channels/whatsapp/webhook",
            detail="callback inspection",
        )

    monkeypatch.setattr(credentials_mod, "get_credential_service", lambda: _Service())
    monkeypatch.setattr(health_mod.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(
        "packages.core.ai.mcp.nango.get_nango_secret",
        lambda *_args, **_kwargs: _async_secret("nango-secret"),
    )
    monkeypatch.setattr(
        "packages.core.services.whatsapp_business_provisioning.inspect_whatsapp_business_number",
        fake_inspect,
    )
    monkeypatch.setattr(
        "packages.core.services.whatsapp_business_provisioning.inspect_whatsapp_app_callback",
        fake_callback,
    )
    monkeypatch.setattr(
        "packages.core.services.channel_bindings.resolve_unique_channel_binding_scope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Test connection must not inspect Agent Binding")
        ),
    )
    monkeypatch.setattr(
        "packages.core.services.whatsapp_business_config.load_whatsapp_business_config",
        lambda: SimpleNamespace(
            app_id="meta-app-id",
            app_secret="deployment-app-secret",
            verify_token="verify-token",
            embedded_signup_config_id="embedded-config-id",
            callback_url="https://staging.example/api/v1/channels/whatsapp/webhook",
        ),
    )

    result = await run_and_persist_integration(db, integration.id)

    assert result["ok"] is expected_ok
    assert result["reason_code"] == expected_code
    assert result["integration_id"] == integration.id
    assert result["channel_config_id"] == "channel-config-1"
    assert result["phone_number_id"] == "phone-1"
    assert result["waba_id"] == "waba-1"
    assert set(result["checks"]) == {
        "oauth",
        "assets",
        "phone_registration",
        "app_subscription",
        "callback",
    }
    assert "wiring" not in result
    assert integration.config["last_health_check"] == result
    assert integration.config["whatsapp"]["readiness_code"] == expected_code
    assert integration.config["whatsapp"]["provisioning_status"] == expected_status
    assert "deployment-app-secret" not in repr(result)
    assert "customer-access-token" not in repr(result)
    assert db.flush_count == 1


def test_health_status_preserves_whatsapp_readiness_projection():
    from apps.api.routers.integrations import HealthStatus

    projected = HealthStatus(
        ok=True,
        reason_code="ready",
        detail="WhatsApp Business account is ready.",
        integration_id="wa-integration",
        channel_config_id="channel-config-1",
        phone_number_id="phone-1",
        waba_id="waba-1",
        checks={
            "oauth": {"ok": True, "detail": "OAuth ready."},
            "callback": {
                "ok": True,
                "detail": "Callback ready.",
                "expected_url": "https://staging.example/whatsapp/webhook",
            },
        },
    ).model_dump(exclude_none=True)

    assert projected["integration_id"] == "wa-integration"
    assert projected["channel_config_id"] == "channel-config-1"
    assert projected["phone_number_id"] == "phone-1"
    assert projected["waba_id"] == "waba-1"
    assert projected["checks"]["callback"]["expected_url"] == (
        "https://staging.example/whatsapp/webhook"
    )


async def _async_secret(value: str) -> str:
    return value
