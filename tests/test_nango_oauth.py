from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import apps.api.routers.nango_oauth as nango_oauth
from apps.api.routers.integrations import EntityAccountConnection
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import User
from packages.core.permissions import Permission
from packages.core.services.whatsapp_business_provisioning import (
    WhatsAppPhoneState,
    WhatsAppProvisioningResult,
)


class _CredentialService:
    def store_integration(self, integration: Integration, payload: dict) -> None:
        integration.credential_ref = f"test-ref:{payload['connection_id']}"
        integration.credential_scheme = "test"
        integration.credentials = {}


class _NangoClient:
    connections: list[dict] = []
    calls: list[tuple[str, dict[str, str]]] = []
    exact_connection: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, url: str, **kwargs) -> httpx.Response:
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        self.calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/connection":
            return httpx.Response(
                200,
                json={"connections": list(self.connections)},
                request=request,
            )
        if request.url.path.startswith("/connection/"):
            connection_id = request.url.path.removeprefix("/connection/")
            connection = self.exact_connection
            if connection is None:
                connection = next(
                    (
                        item
                        for item in self.connections
                        if item.get("connection_id") == connection_id
                    ),
                    None,
                )
            if connection is not None:
                return httpx.Response(200, json=connection, request=request)
        return httpx.Response(404, request=request)


def _whatsapp_provisioning_result(
    *,
    ok: bool,
    phone_state: WhatsAppPhoneState,
    exact_app_subscribed: bool,
    phone_number_id: str,
    waba_id: str,
    detail: str,
) -> WhatsAppProvisioningResult:
    return WhatsAppProvisioningResult(
        ok=ok,
        phone_state=phone_state,
        exact_app_subscribed=exact_app_subscribed,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        app_id="meta-app-id",
        detail=detail,
    )


async def _seed_whatsapp_reconnect_route(db_session, user):
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    old_connection_id = (
        f"{user.entity_id}--{user.id}--whatsapp--old-connection"
    )
    old_phone_number_id = generate_ulid()
    integration = Integration(
        id=generate_ulid(),
        entity_id=user.entity_id,
        owner_user_id=user.id,
        created_by_user_id=user.id,
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": old_connection_id,
                "provider_config_key": "whatsapp",
            },
            "whatsapp": {
                "waba_id": f"old-waba-{user.entity_id}",
                "phone_number_id": old_phone_number_id,
                "provisioning_status": "ready",
                "readiness_code": "ready",
            },
        },
        credentials={},
        credential_ref=f"test-ref:{old_connection_id}",
        credential_scheme="test",
    )
    db_session.add_all([user, integration])
    await db_session.flush()
    channel_config = ChannelConfig(
        entity_id=user.entity_id,
        owner_user_id=user.id,
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        name="WhatsApp",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        whatsapp_phone_number_id=old_phone_number_id,
        config={
            "connection_kind": "integration",
            "connection_id": integration.id,
            "integration_id": integration.id,
            "whatsapp_provisioning_status": "ready",
            "whatsapp_readiness_code": "ready",
        },
        credentials={},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.flush()
    binding = Channel(
        entity_id=user.entity_id,
        user_id=user.id,
        type="whatsapp",
        name="WhatsApp",
        config={
            "integration_id": integration.id,
            "channel_config_id": channel_config.id,
        },
        status="active",
    )
    db_session.add(binding)
    await db_session.commit()
    return (
        integration,
        channel_config,
        binding,
        old_connection_id,
        old_phone_number_id,
    )


@pytest.mark.asyncio
async def test_whatsapp_resource_verification_uses_embedded_waba_without_business_scope(
    monkeypatch,
):
    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        requests: list[tuple[str, dict[str, str], dict[str, str]]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params=None, headers=None):
            self.requests.append((url, params or {}, headers or {}))
            if url.endswith("/waba%2Funsafe/phone_numbers"):
                return Response({
                    "data": [
                        {"id": "other-phone", "verified_name": "Other Number"},
                        {
                            "id": "phone-1",
                            "display_phone_number": "+15550001111",
                            "verified_name": "QA Number",
                        },
                    ]
                })
            raise AssertionError(url)

    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", lambda **_kwargs: Client())

    resource = await nango_oauth._verify_whatsapp_resource_via_nango(
        secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="entity--user--whatsapp--connection",
        waba_id="waba/unsafe",
        phone_number_id="phone-1",
    )

    assert resource == {
        "waba_id": "waba/unsafe",
        "phone_number_id": "phone-1",
        "display_name": "QA Number",
    }
    assert [urlsplit(request[0]).path for request in Client.requests] == [
        "/proxy/v25.0/waba%2Funsafe/phone_numbers",
    ]
    assert all(
        request[2].get("Decompress") == "true"
        and request[2].get("Nango-Proxy-Accept-Encoding") == "identity"
        for request in Client.requests
    )
    assert "access_token" not in resource


@pytest.mark.asyncio
async def test_whatsapp_resource_verification_rejects_phone_outside_embedded_waba(
    monkeypatch,
):
    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"id": "phone-1", "display_phone_number": "+15550001111"},
                {"id": "phone-2", "display_phone_number": "+15550002222"},
            ]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params=None, headers=None):
            if url.endswith("/waba-1/phone_numbers"):
                return Response()
            raise AssertionError(url)

    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", lambda **_kwargs: Client())

    resource = await nango_oauth._verify_whatsapp_resource_via_nango(
        secret="secret",
        provider_config_key="whatsapp",
        connection_id="entity--user--whatsapp--connection",
        waba_id="waba-1",
        phone_number_id="missing-phone",
    )

    assert resource is None


@pytest.mark.asyncio
async def test_whatsapp_waba_subscription_uses_exact_nango_connection(monkeypatch):
    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    class Client:
        requests: list[tuple[str, dict, dict]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers=None, json=None):
            self.requests.append((url, headers or {}, json or {}))
            return Response()

    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", lambda **_kwargs: Client())

    await nango_oauth._subscribe_whatsapp_waba_via_nango(
        secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="entity--user--whatsapp--connection",
        waba_id="waba/unsafe",
    )

    assert len(Client.requests) == 1
    url, headers, body = Client.requests[0]
    assert urlsplit(url).path == "/proxy/v25.0/waba%2Funsafe/subscribed_apps"
    assert headers["Authorization"] == "Bearer nango-secret"
    assert headers["Provider-Config-Key"] == "whatsapp"
    assert headers["Connection-Id"] == "entity--user--whatsapp--connection"
    assert body == {}
    assert "access_token" not in str(Client.requests)


@pytest.mark.asyncio
async def test_whatsapp_waba_subscription_propagates_nango_failure(monkeypatch):
    class Response:
        status_code = 400
        text = "invalid subscription"

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "upstream",
                request=httpx.Request("POST", "https://nango.test/proxy"),
                response=httpx.Response(self.status_code, text=self.text),
            )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(httpx.HTTPStatusError):
        await nango_oauth._subscribe_whatsapp_waba_via_nango(
            secret="nango-secret",
            provider_config_key="whatsapp",
            connection_id="entity--user--whatsapp--connection",
            waba_id="waba-1",
        )


async def _async_result(value):
    return value


@pytest.fixture(autouse=True)
def _reset_nango_client(monkeypatch):
    _NangoClient.connections = []
    _NangoClient.calls = []
    _NangoClient.exact_connection = None
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)


def _user(entity_id: str, *, role: str = "owner") -> User:
    return User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"{generate_ulid()}@example.test",
        password_hash="unused",
        role=role,
    )


async def _stub_secret(*args, **kwargs) -> str:
    return "fresh-secret"


def _set_whatsapp_deployment_config(monkeypatch) -> None:
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", "meta-app-secret")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-1")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "whatsapp-verify-token")


def test_whatsapp_connect_request_is_account_only() -> None:
    fields = nango_oauth.StartConnectRequest.model_fields

    assert "agent_id" not in fields
    assert "agent_subscription_id" not in fields
    assert "workspace_id" not in fields


@pytest.mark.asyncio
async def test_start_connect_requires_a_64_character_hex_hmac_key(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid())
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")

    for key in ("", "not-a-hex-key", "a" * 63):
        monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", key)
        with pytest.raises(HTTPException) as error:
            await nango_oauth.start_connect_session(
                nango_oauth.StartConnectRequest(provider_config_keys=["linkedin"]),
                user=user,
                db=db_session,
            )
        assert error.value.status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["whatsapp", "ms_teams"])
async def test_channel_flavoured_nango_integration_gets_user_owned_channel_config(
    db_session, provider: str,
) -> None:
    """Nango-backed channel accounts must enter the same ChannelConfig path
    as the direct Slack/Discord integrations before they can receive messages.
    """
    from apps.api.routers import integrations as integrations_router
    from packages.core.models.channel import ChannelConfig

    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    integration_id = generate_ulid()

    await integrations_router._sync_channel_config_if_needed(
        db_session,
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        provider=provider,
        integration_id=integration_id,
    )

    row = await db_session.scalar(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.owner_user_id == owner_user_id,
            ChannelConfig.credential_source_id == integration_id,
        )
    )
    assert row is not None
    assert row.credential_source_kind == "integration"
    assert row.channel_type == provider


@pytest.mark.asyncio
async def test_start_connect_returns_provider_and_nango_hmac_url_query(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid())
    hmac_key = "b" * 64
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")
    monkeypatch.setenv("NANGO_PUBLIC_URL", "https://nango.example.test")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", hmac_key)

    result = await nango_oauth.start_connect_session(
        nango_oauth.StartConnectRequest(provider_config_keys=["linkedin"]),
        user=user,
        db=db_session,
    )
    query = parse_qs(urlsplit(result.nango_connect_url).query)
    expected_hmac = hmac.new(
        hmac_key.encode("ascii"),
        f"linkedin:{result.connection_id}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    assert result.provider_config_key == "linkedin"
    assert query["hmac"] == [expected_hmac]


@pytest.mark.asyncio
async def test_start_whatsapp_connect_uses_embedded_signup_authorization_params(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid())
    _set_whatsapp_deployment_config(monkeypatch)
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")
    monkeypatch.setenv("NANGO_PUBLIC_URL", "https://nango.example.test")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "c" * 64)
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-1")

    result = await nango_oauth.start_connect_session(
        nango_oauth.StartConnectRequest(provider_config_keys=["whatsapp"]),
        user=user,
        db=db_session,
    )
    query = parse_qs(urlsplit(result.nango_connect_url).query)

    assert query["authorization_params[config_id]"] == ["embedded-config-1"]
    assert query["authorization_params[response_type]"] == ["code"]
    assert query["authorization_params[override_default_response_type]"] == ["true"]
    assert json.loads(query["authorization_params[extras]"][0]) == {
        "setup": {},
        "featureType": "",
        "sessionInfoVersion": "3",
    }
    assert result.connection_id.startswith(
        f"{user.entity_id}--{user.id}--whatsapp--"
    )


@pytest.mark.asyncio
async def test_start_whatsapp_connect_requires_embedded_signup_config_id(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid())
    _set_whatsapp_deployment_config(monkeypatch)
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "c" * 64)
    monkeypatch.delenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", raising=False)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.start_connect_session(
            nango_oauth.StartConnectRequest(provider_config_keys=["whatsapp"]),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 500
    assert "embedded_signup_config_id" in str(error.value.detail)


def _nango_integration(
    entity_id: str,
    connection_id: str,
    *,
    owner_user_id: str,
) -> Integration:
    return Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        provider="linkedin",
        status="active",
        config={
            "is_default": True,
            "nango": {
                "connection_id": connection_id,
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
    )


@pytest.mark.asyncio
async def test_member_cannot_start_reconnect(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id, role="member")
    integration = _nango_integration(
        entity_id, f"{entity_id}--old", owner_user_id=user.id,
    )
    db_session.add(integration)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")

    with pytest.raises(HTTPException) as error:
        await nango_oauth.start_connect_session(
            nango_oauth.StartConnectRequest(
                provider_config_keys=["linkedin"],
                replace_integration_id=integration.id,
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_member_reconnect_is_denied_before_nango_or_request_validation(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid(), role="member")
    secret_checked = False

    async def _leaking_secret_check(*args, **kwargs) -> str:
        nonlocal secret_checked
        secret_checked = True
        raise HTTPException(400, "Nango is not configured")

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _leaking_secret_check)
    monkeypatch.delenv("NANGO_PUBLIC_KEY", raising=False)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.start_connect_session(
            nango_oauth.StartConnectRequest(
                provider_config_keys=[],
                replace_integration_id=generate_ulid(),
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 403
    assert secret_checked is False


@pytest.mark.asyncio
async def test_member_cannot_sync_reconnect(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id, role="member")
    integration = _nango_integration(
        entity_id, f"{entity_id}--old", owner_user_id=user.id,
    )
    new_connection_id = f"{entity_id}--{user.id}--linkedin--{generate_ulid()}"
    db_session.add(integration)
    await db_session.commit()
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": new_connection_id,
        }
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=new_connection_id,
                replace_integration_id=integration.id,
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_member_can_use_add_account_flow(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id, role="member")
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    started = await nango_oauth.start_connect_session(
        nango_oauth.StartConnectRequest(provider_config_keys=["linkedin"]),
        user=user,
        db=db_session,
    )
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": started.connection_id,
        }
    ]
    synced = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=started.connection_id,
            expected_provider_config_key=started.provider_config_key,
        ),
        user=user,
        db=db_session,
    )
    rows = (await db_session.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )).scalars().all()

    assert started.connection_id.startswith(f"{entity_id}--{user.id}--linkedin--")
    assert synced.upserted == 1
    assert _NangoClient.calls == [
        (
            f"/connection/{started.connection_id}",
            {"provider_config_key": "linkedin"},
        )
    ]
    assert len(rows) == 1
    assert rows[0].config["nango"]["connection_id"] == started.connection_id


@pytest.mark.asyncio
async def test_sync_whatsapp_verifies_embedded_phone_and_stores_no_token(
    db_session, monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    waba_id = generate_ulid()
    phone_number_id = generate_ulid()
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"

    class Response:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = ""

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "upstream", request=httpx.Request("GET", "https://nango.test"), response=httpx.Response(self.status_code),
                )

        def json(self):
            return self._payload

    class Client:
        subscription_requests: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params=None, headers=None):
            if "/connection/" in url:
                return Response({
                    "connection_id": connection_id,
                    "provider_config_key": "whatsapp",
                    "credentials": {"access_token": "must-stay-in-nango"},
                })
            if url.endswith(f"/{waba_id}/phone_numbers"):
                return Response({"data": [{"id": phone_number_id, "verified_name": "QA Number"}]})
            raise AssertionError(url)

    from packages.core.models.channel import ChannelConfig

    provisioning_calls: list[dict] = []

    async def provision(**kwargs):
        pending_channel = (await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.whatsapp_phone_number_id == phone_number_id
            )
        )).scalar_one()
        assert pending_channel.status == "error"
        assert pending_channel.config["whatsapp_provisioning_status"] == "pending"
        provisioning_calls.append(kwargs)
        return _whatsapp_provisioning_result(
            ok=True,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=True,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            detail="WhatsApp Business route is provisioned",
        )

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(
        nango_oauth,
        "provision_whatsapp_business_number",
        provision,
    )
    monkeypatch.setattr(
        nango_oauth,
        "load_whatsapp_business_config",
        lambda: type("Config", (), {"app_id": "meta-app-id"})(),
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=connection_id,
            expected_provider_config_key="whatsapp",
            whatsapp_waba_id=waba_id,
            whatsapp_phone_number_id=phone_number_id,
        ),
        user=user,
        db=db_session,
    )

    row = (await db_session.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "whatsapp",
        )
    )).scalar_one_or_none()
    assert result.upserted == 1
    assert result.providers == ["whatsapp"]
    assert result.integration_id == row.id
    assert result.readiness_code == "ready"
    assert provisioning_calls == [{
        "nango_secret": "fresh-secret",
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
        "phone_number_id": phone_number_id,
        "waba_id": waba_id,
        "expected_app_id": "meta-app-id",
    }]
    assert row is not None
    assert row.config["nango"]["connection_id"] == connection_id
    assert row.config["whatsapp"]["waba_id"] == waba_id
    assert row.config["whatsapp"]["phone_number_id"] == phone_number_id
    assert row.config["whatsapp"]["display_name"] == "QA Number"
    assert row.config["whatsapp"]["provisioning_status"] == "ready"
    assert row.config["whatsapp"]["readiness_code"] == "ready"
    assert result.provisioning_pending is False
    assert "must-stay-in-nango" not in str(row.credentials)

    from packages.core.models.document import Channel

    channel = (await db_session.execute(
        select(ChannelConfig).where(ChannelConfig.credential_source_id == row.id)
    )).scalar_one()
    assert channel.whatsapp_phone_number_id == phone_number_id
    binding = await db_session.scalar(
        select(Channel).where(
            Channel.type == "whatsapp",
            Channel.config["channel_config_id"].as_string() == channel.id,
        )
    )
    assert binding is None


@pytest.mark.asyncio
async def test_sync_whatsapp_uses_and_verifies_embedded_signup_resource(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    waba_id = generate_ulid()
    phone_number_id = generate_ulid()
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
    }]
    verification_calls: list[dict] = []
    provisioning_calls: list[dict] = []

    async def verify(**kwargs):
        verification_calls.append(kwargs)
        return {
            "waba_id": kwargs["waba_id"],
            "phone_number_id": kwargs["phone_number_id"],
            "display_name": "Embedded QA Number",
        }

    async def provision(**kwargs):
        provisioning_calls.append(kwargs)
        return _whatsapp_provisioning_result(
            ok=True,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=True,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            detail="WhatsApp Business route is provisioned",
        )

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", verify)
    monkeypatch.setattr(
        nango_oauth,
        "provision_whatsapp_business_number",
        provision,
    )
    monkeypatch.setattr(
        nango_oauth,
        "load_whatsapp_business_config",
        lambda: type("Config", (), {"app_id": "meta-app-id"})(),
    )
    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=connection_id,
            expected_provider_config_key="whatsapp",
            whatsapp_waba_id=waba_id,
            whatsapp_phone_number_id=phone_number_id,
        ),
        user=user,
        db=db_session,
    )

    stored = (await db_session.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "whatsapp",
        )
    )).scalar_one()
    assert result.upserted == 1
    assert verification_calls == [{
        "secret": "fresh-secret",
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
    }]
    assert provisioning_calls[0]["waba_id"] == waba_id
    assert stored.config["whatsapp"]["waba_id"] == waba_id
    assert stored.config["whatsapp"]["phone_number_id"] == phone_number_id
    assert stored.config["whatsapp"]["display_name"] == "Embedded QA Number"
    assert stored.config["whatsapp"]["provisioning_status"] == "ready"
    assert stored.config["whatsapp"]["readiness_code"] == "ready"


@pytest.mark.asyncio
async def test_sync_whatsapp_requires_embedded_signup_ids_as_a_pair(
    db_session,
    monkeypatch,
) -> None:
    user = _user(generate_ulid())
    secret_loaded = False

    async def load_secret(*_args, **_kwargs):
        nonlocal secret_loaded
        secret_loaded = True
        return "secret"

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", load_secret)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=(
                    f"{user.entity_id}--{user.id}--whatsapp--{generate_ulid()}"
                ),
                expected_provider_config_key="whatsapp",
                whatsapp_waba_id="embedded-waba",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 422
    assert secret_loaded is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_connection_id", "expected_provider_config_key"),
    [
        (None, None),
        ("entity--user--linkedin--connection", "linkedin"),
    ],
)
async def test_sync_embedded_signup_ids_require_exact_whatsapp_connection(
    db_session,
    monkeypatch,
    expected_connection_id,
    expected_provider_config_key,
) -> None:
    user = _user(generate_ulid())
    secret_loaded = False

    async def load_secret(*_args, **_kwargs):
        nonlocal secret_loaded
        secret_loaded = True
        return "secret"

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", load_secret)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=expected_connection_id,
                expected_provider_config_key=expected_provider_config_key,
                whatsapp_waba_id="embedded-waba",
                whatsapp_phone_number_id="embedded-phone",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 400
    assert secret_loaded is False


@pytest.mark.asyncio
async def test_sync_whatsapp_rejects_phone_not_owned_by_embedded_waba(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
    }]

    async def reject_resource(**_kwargs):
        return None

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", reject_resource)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=connection_id,
                expected_provider_config_key="whatsapp",
                whatsapp_waba_id="embedded-waba",
                whatsapp_phone_number_id="foreign-phone",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 422
    row = (await db_session.execute(
        select(Integration).where(Integration.entity_id == entity_id)
    )).scalar_one_or_none()
    assert row is None


@pytest.mark.asyncio
async def test_sync_whatsapp_persists_retryable_state_when_waba_subscription_fails(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.tasks import channel_tasks

    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
        "metadata": {
            "phone_number_id": "phone-subscribe-failure",
            "waba_id": "waba-subscribe-failure",
            "verified_name": "QA Number",
        },
    }]

    async def unavailable_provisioning(**_kwargs):
        raise httpx.ConnectError(
            "provider included a secret-shaped diagnostic that must not persist",
            request=httpx.Request("POST", "https://nango.test/proxy/subscribed_apps"),
        )

    async def verify(**kwargs):
        return {
            "waba_id": kwargs["waba_id"],
            "phone_number_id": kwargs["phone_number_id"],
            "display_name": "QA Number",
        }

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", verify)
    monkeypatch.setattr(
        nango_oauth,
        "provision_whatsapp_business_number",
        unavailable_provisioning,
    )
    monkeypatch.setattr(
        nango_oauth,
        "load_whatsapp_business_config",
        lambda: type("Config", (), {"app_id": "meta-app-id"})(),
    )
    retry_jobs: list[dict] = []
    monkeypatch.setattr(
        channel_tasks.register_integration_webhooks_task,
        "delay",
        lambda **kwargs: retry_jobs.append(kwargs),
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=connection_id,
            expected_provider_config_key="whatsapp",
            whatsapp_waba_id="waba-subscribe-failure",
            whatsapp_phone_number_id="phone-subscribe-failure",
        ),
        user=user,
        db=db_session,
    )

    stored = (await db_session.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "whatsapp",
        )
    )).scalar_one()
    assert result.upserted == 1
    assert result.provisioning_pending is True
    assert result.integration_id == stored.id
    assert result.readiness_code == "provider_unavailable"
    assert stored.config["whatsapp"]["provisioning_status"] == "failed"
    assert stored.config["whatsapp"]["readiness_code"] == "provider_unavailable"
    assert stored.config["whatsapp"]["provisioning_attempt_count"] == 1
    assert stored.config["whatsapp"]["provisioning_last_attempt_at"]
    assert stored.config["whatsapp"]["provisioning_error"] == (
        "WhatsApp Business provider setup is temporarily unavailable."
    )
    assert "secret-shaped" not in str(stored.config)
    assert stored.config["last_health_check"]["ok"] is False
    assert retry_jobs == [{
        "entity_id": entity_id,
        "integration_id": stored.id,
    }]


@pytest.mark.asyncio
async def test_sync_whatsapp_reports_registration_pin_recovery_without_retry(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.tasks import channel_tasks

    entity_id = generate_ulid()
    user = _user(entity_id)
    waba_id = generate_ulid()
    phone_number_id = generate_ulid()
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
    }]

    async def verify(**kwargs):
        return {
            "waba_id": kwargs["waba_id"],
            "phone_number_id": kwargs["phone_number_id"],
            "display_name": "PIN Recovery Number",
        }

    async def registration_required(**kwargs):
        return _whatsapp_provisioning_result(
            ok=False,
            phone_state=WhatsAppPhoneState.REGISTRATION_REQUIRED,
            exact_app_subscribed=False,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            detail="WhatsApp phone registration is required",
        )

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", verify)
    monkeypatch.setattr(
        nango_oauth,
        "provision_whatsapp_business_number",
        registration_required,
    )
    monkeypatch.setattr(
        nango_oauth,
        "load_whatsapp_business_config",
        lambda: type("Config", (), {"app_id": "meta-app-id"})(),
    )
    retry_jobs: list[dict] = []
    monkeypatch.setattr(
        channel_tasks.register_integration_webhooks_task,
        "delay",
        lambda **kwargs: retry_jobs.append(kwargs),
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=connection_id,
            expected_provider_config_key="whatsapp",
            whatsapp_waba_id=waba_id,
            whatsapp_phone_number_id=phone_number_id,
        ),
        user=user,
        db=db_session,
    )

    stored = (await db_session.execute(
        select(Integration).where(Integration.entity_id == entity_id)
    )).scalar_one()
    assert result.integration_id == stored.id
    assert result.provisioning_pending is True
    assert result.readiness_code == "phone_not_registered"
    assert stored.config["whatsapp"]["provisioning_status"] == (
        "registration_required"
    )
    assert stored.config["whatsapp"]["readiness_code"] == "phone_not_registered"
    assert retry_jobs == []


@pytest.mark.asyncio
async def test_sync_whatsapp_rolls_back_rows_when_channel_bridge_fails(
    db_session,
    monkeypatch,
) -> None:
    import apps.api.routers.integrations as integrations_router

    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
    }]

    async def verify(**kwargs):
        return {
            "waba_id": kwargs["waba_id"],
            "phone_number_id": kwargs["phone_number_id"],
            "display_name": "Rollback Number",
        }

    async def fail_bridge(*_args, **_kwargs):
        raise RuntimeError("database bridge failed")

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", verify)
    monkeypatch.setattr(
        integrations_router,
        "_sync_channel_config_if_needed",
        fail_bridge,
    )

    with pytest.raises(RuntimeError, match="database bridge failed"):
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=connection_id,
                expected_provider_config_key="whatsapp",
                whatsapp_waba_id="waba-rollback",
                whatsapp_phone_number_id="phone-rollback",
            ),
            user=user,
            db=db_session,
        )

    integration = (await db_session.execute(
        select(Integration).where(Integration.entity_id == entity_id)
    )).scalar_one_or_none()
    assert integration is None


@pytest.mark.asyncio
async def test_sync_whatsapp_rejects_metadata_with_phone_but_no_waba(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
        "metadata": {
            "phone_number_id": "phone-metadata-only",
            "verified_name": "Metadata Number",
        },
    }]
    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=connection_id,
                expected_provider_config_key="whatsapp",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_sync_whatsapp_rejects_missing_embedded_signup_result(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_id = f"{entity_id}--{user.id}--whatsapp--{generate_ulid()}"
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
    }]

    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=connection_id,
                expected_provider_config_key="whatsapp",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_full_sync_does_not_subscribe_superseded_whatsapp_connection(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    old_connection_id = f"{entity_id}--{user.id}--whatsapp--old"
    current_connection_id = f"{entity_id}--{user.id}--whatsapp--current"
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user.id,
        created_by_user_id=user.id,
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": current_connection_id,
                "provider_config_key": "whatsapp",
                "superseded_connection_ids": [old_connection_id],
            },
            "whatsapp": {
                "waba_id": "waba-current",
                "phone_number_id": "phone-current",
            },
        },
        credentials={},
    )
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": old_connection_id,
        "metadata": {
            "waba_id": "waba-old",
            "phone_number_id": "phone-old",
        },
    }]
    provisioning_calls: list[str] = []

    async def provision(*, connection_id, **_kwargs):
        provisioning_calls.append(connection_id)
        raise AssertionError("Superseded WhatsApp connection was provisioned")

    db_session.add_all([user, integration])
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(
        nango_oauth,
        "provision_whatsapp_business_number",
        provision,
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )

    assert result.upserted == 0
    assert provisioning_calls == []


@pytest.mark.asyncio
async def test_exact_sync_requires_the_expected_provider_before_loading_secret(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid(), role="member")
    secret_loaded = False

    async def _secret_should_not_load(*args, **kwargs) -> str:
        nonlocal secret_loaded
        secret_loaded = True
        return "fresh-secret"

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _secret_should_not_load)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=f"{user.entity_id}--linkedin--missing-provider",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 400
    assert secret_loaded is False
    assert _NangoClient.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_connection_id", "returned_provider"),
    [
        ("wrong-connection", "linkedin"),
        (None, "slack"),
    ],
)
async def test_exact_sync_rejects_mismatched_nango_response(
    db_session,
    monkeypatch,
    returned_connection_id: str | None,
    returned_provider: str,
) -> None:
    user = _user(generate_ulid(), role="member")
    expected_connection_id = f"{user.entity_id}--linkedin--expected-connection"
    _NangoClient.exact_connection = {
        "provider_config_key": returned_provider,
        "connection_id": returned_connection_id or expected_connection_id,
    }
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=expected_connection_id,
                expected_provider_config_key="linkedin",
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 502
    assert _NangoClient.calls == [
        (
            f"/connection/{expected_connection_id}",
            {"provider_config_key": "linkedin"},
        )
    ]


@pytest.mark.asyncio
async def test_member_cannot_start_full_sync_before_nango_or_secret(
    db_session, monkeypatch
) -> None:
    user = _user(generate_ulid(), role="member")
    secret_loaded = False

    async def _secret_should_not_load(*args, **kwargs) -> str:
        nonlocal secret_loaded
        secret_loaded = True
        return "fresh-secret"

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _secret_should_not_load)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)

    with pytest.raises(HTTPException) as error:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 403
    assert secret_loaded is False
    assert _NangoClient.calls == []


@pytest.mark.asyncio
async def test_full_sync_creates_connection_without_existing_candidate(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_id = f"{entity_id}--{user.id}--linkedin--fresh"
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": connection_id,
        }
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )
    rows = (await db_session.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )).scalars().all()

    assert result.upserted == 1
    assert _NangoClient.calls == [("/connection", {})]
    assert len(rows) == 1
    assert rows[0].owner_user_id == user.id
    assert rows[0].config["nango"]["connection_id"] == connection_id


@pytest.mark.asyncio
async def test_custom_integration_manager_can_start_reconnect(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id, role="member")
    role = StaffRole(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Integration manager",
        permissions=[Permission.INTEGRATIONS_MANAGE.value],
    )
    staff = Staff(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Integration manager",
        user_id=user.id,
        role_id=role.id,
        status="active",
    )
    integration = _nango_integration(
        entity_id, f"{entity_id}--old", owner_user_id=user.id,
    )
    db_session.add_all([user, role])
    await db_session.flush()
    db_session.add_all([staff, integration])
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")

    result = await nango_oauth.start_connect_session(
        nango_oauth.StartConnectRequest(
            provider_config_keys=["linkedin"],
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    assert result.connection_id.startswith(f"{entity_id}--{user.id}--linkedin--")


@pytest.mark.asyncio
async def test_custom_integration_manager_can_sync_reconnect(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id, role="member")
    role = StaffRole(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Integration manager",
        permissions=[Permission.INTEGRATIONS_MANAGE.value],
    )
    staff = Staff(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Integration manager",
        user_id=user.id,
        role_id=role.id,
        status="active",
    )
    old_connection_id = f"{entity_id}--{user.id}--linkedin--old"
    new_connection_id = f"{entity_id}--{user.id}--linkedin--new"
    integration = _nango_integration(
        entity_id, old_connection_id, owner_user_id=user.id,
    )
    db_session.add_all([user, role])
    await db_session.flush()
    db_session.add_all([staff, integration])
    await db_session.commit()
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": new_connection_id,
        }
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=new_connection_id,
            expected_provider_config_key="linkedin",
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    assert result.upserted == 1
    assert integration.config["nango"]["connection_id"] == new_connection_id
    assert integration.config["nango"]["superseded_connection_ids"] == [
        old_connection_id
    ]


@pytest.mark.asyncio
async def test_start_reconnect_returns_exact_new_connection_id(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user.id,
        provider="linkedin",
        status="active",
        config={
            "is_default": True,
            "nango": {
                "connection_id": "old-linkedin-connection",
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
    )
    db_session.add(integration)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")
    monkeypatch.setenv("NANGO_PUBLIC_URL", "https://nango.example.test")

    result = await nango_oauth.start_connect_session(
        nango_oauth.StartConnectRequest(
            provider_config_keys=["linkedin"],
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    assert result.connection_id.startswith(f"{entity_id}--{user.id}--linkedin--")
    assert f"connection_id={result.connection_id}" in result.nango_connect_url


@pytest.mark.asyncio
async def test_start_reconnect_refuses_non_nango_and_cross_entity_rows(db_session, monkeypatch) -> None:
    user = _user(generate_ulid())
    rows = (
        Integration(
            id=generate_ulid(),
            entity_id=user.entity_id,
            owner_user_id=user.id,
            provider="linkedin",
            status="active",
            config={"is_default": True},
            credentials={},
        ),
        Integration(
            id=generate_ulid(),
            entity_id=generate_ulid(),
            owner_user_id=generate_ulid(),
            provider="linkedin",
            status="active",
            config={
                "nango": {
                    "connection_id": "other-connection",
                    "provider_config_key": "linkedin",
                }
            },
            credentials={},
        ),
    )
    db_session.add_all(rows)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setenv("NANGO_PUBLIC_KEY", "nango_public_key_test")

    for row, expected_status in ((rows[0], 400), (rows[1], 404)):
        with pytest.raises(HTTPException) as error:
            await nango_oauth.start_connect_session(
                nango_oauth.StartConnectRequest(
                    provider_config_keys=["linkedin"],
                    replace_integration_id=row.id,
                ),
                user=user,
                db=db_session,
            )
        assert error.value.status_code == expected_status


@pytest.mark.asyncio
async def test_reconnect_sync_replaces_selected_row_in_place(db_session, monkeypatch) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    old_connection_id = f"{entity_id}--{user.id}--linkedin--old"
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user.id,
        provider="linkedin",
        status="active",
        config={
            "is_default": True,
            "name": "Manor LinkedIn",
            "custom": {"keep": True},
            "nango": {
                "connection_id": old_connection_id,
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
        credential_ref="old-ref",
        credential_scheme="test",
    )
    db_session.add(integration)
    await db_session.commit()
    original_integration_id = integration.id
    new_connection_id = f"{entity_id}--{user.id}--linkedin--{generate_ulid()}"
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": new_connection_id,
            "created_at": "2026-08-21T08:00:00Z",
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": f"{entity_id}--unrelated",
        },
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=new_connection_id,
            expected_provider_config_key="linkedin",
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    count = await db_session.scalar(
        select(func.count()).select_from(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )
    assert result.upserted == 1
    assert count == 1
    assert integration.id == original_integration_id
    assert integration.config["is_default"] is True
    assert integration.config["name"] == "Manor LinkedIn"
    assert integration.config["custom"] == {"keep": True}
    assert integration.config["nango"]["connection_id"] == new_connection_id
    assert integration.config["nango"]["superseded_connection_ids"] == [
        old_connection_id
    ]
    assert integration.credential_ref == f"test-ref:{new_connection_id}"
    assert integration.credential_ref != "old-ref"

    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": old_connection_id,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": new_connection_id,
        },
    ]
    full_sync = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )
    count_after_full_sync = await db_session.scalar(
        select(func.count()).select_from(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )
    await db_session.refresh(integration)

    assert full_sync.upserted == 1
    assert count_after_full_sync == 1
    assert integration.config["nango"]["connection_id"] == new_connection_id


@pytest.mark.asyncio
async def test_whatsapp_reconnect_keeps_old_route_until_replacement_is_ready(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    integration, channel_config, binding, old_connection_id, old_phone_number_id = (
        await _seed_whatsapp_reconnect_route(db_session, user)
    )
    new_connection_id = f"{entity_id}--{user.id}--whatsapp--replacement"
    replacement_waba_id = f"replacement-waba-{entity_id}"
    replacement_phone_number_id = generate_ulid()
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": new_connection_id,
    }]

    async def verify(**kwargs):
        return {
            "waba_id": kwargs["waba_id"],
            "phone_number_id": kwargs["phone_number_id"],
            "display_name": "Replacement Number",
        }

    async def not_ready(**kwargs):
        return _whatsapp_provisioning_result(
            ok=False,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=False,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            detail="Exact App subscription is pending",
        )

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", verify)
    monkeypatch.setattr(nango_oauth, "provision_whatsapp_business_number", not_ready)
    monkeypatch.setattr(
        nango_oauth,
        "load_whatsapp_business_config",
        lambda: type("Config", (), {"app_id": "meta-app-id"})(),
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=new_connection_id,
            expected_provider_config_key="whatsapp",
            replace_integration_id=integration.id,
            whatsapp_waba_id=replacement_waba_id,
            whatsapp_phone_number_id=replacement_phone_number_id,
        ),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    await db_session.refresh(channel_config)
    await db_session.refresh(binding)
    assert result.upserted == 0
    assert result.provisioning_pending is True
    assert result.readiness_code == "app_not_subscribed"
    assert integration.config["nango"]["connection_id"] == old_connection_id
    assert integration.config["whatsapp"]["phone_number_id"] == old_phone_number_id
    assert channel_config.whatsapp_phone_number_id == old_phone_number_id
    assert channel_config.status == "active"
    assert binding.status == "active"


@pytest.mark.asyncio
async def test_whatsapp_reconnect_swaps_in_place_then_retires_old_connection(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    integration, channel_config, binding, old_connection_id, _old_phone_number_id = (
        await _seed_whatsapp_reconnect_route(db_session, user)
    )
    original_channel_config_id = channel_config.id
    original_binding_id = binding.id
    new_connection_id = f"{entity_id}--{user.id}--whatsapp--replacement"
    replacement_waba_id = f"replacement-waba-{entity_id}"
    replacement_phone_number_id = generate_ulid()
    _NangoClient.connections = [{
        "provider_config_key": "whatsapp",
        "connection_id": new_connection_id,
    }]

    async def verify(**kwargs):
        return {
            "waba_id": kwargs["waba_id"],
            "phone_number_id": kwargs["phone_number_id"],
            "display_name": "Replacement Number",
        }

    async def ready(**kwargs):
        return _whatsapp_provisioning_result(
            ok=True,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=True,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            detail="Replacement is ready",
        )

    retired: list[dict] = []

    async def retire(**kwargs):
        retired.append(kwargs)

    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_verify_whatsapp_resource_via_nango", verify)
    monkeypatch.setattr(nango_oauth, "provision_whatsapp_business_number", ready)
    monkeypatch.setattr(
        nango_oauth,
        "delete_nango_connection",
        retire,
        raising=False,
    )
    monkeypatch.setattr(
        nango_oauth,
        "load_whatsapp_business_config",
        lambda: type("Config", (), {"app_id": "meta-app-id"})(),
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=new_connection_id,
            expected_provider_config_key="whatsapp",
            replace_integration_id=integration.id,
            whatsapp_waba_id=replacement_waba_id,
            whatsapp_phone_number_id=replacement_phone_number_id,
        ),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    await db_session.refresh(channel_config)
    await db_session.refresh(binding)
    assert result.upserted == 1
    assert result.readiness_code == "ready"
    assert integration.config["nango"]["connection_id"] == new_connection_id
    assert (
        integration.config["whatsapp"]["phone_number_id"]
        == replacement_phone_number_id
    )
    assert channel_config.id == original_channel_config_id
    assert channel_config.whatsapp_phone_number_id == replacement_phone_number_id
    assert channel_config.status == "active"
    assert binding.id == original_binding_id
    assert binding.status == "active"
    assert retired == [{
        "nango_secret": "fresh-secret",
        "provider_config_key": "whatsapp",
        "connection_id": old_connection_id,
    }]


@pytest.mark.asyncio
async def test_repeated_reconnect_suppresses_every_old_connection(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    first_connection_id = f"{entity_id}--{user.id}--linkedin--first"
    second_connection_id = f"{entity_id}--{user.id}--linkedin--second"
    final_connection_id = f"{entity_id}--{user.id}--linkedin--final"
    integration = _nango_integration(
        entity_id, first_connection_id, owner_user_id=user.id,
    )
    db_session.add(integration)
    await db_session.commit()
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": second_connection_id,
        }
    ]
    await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=second_connection_id,
            expected_provider_config_key="linkedin",
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": second_connection_id,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": final_connection_id,
        },
    ]
    await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=final_connection_id,
            expected_provider_config_key="linkedin",
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": second_connection_id,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": first_connection_id,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": final_connection_id,
        },
    ]
    full_sync = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    count = await db_session.scalar(
        select(func.count()).select_from(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )
    assert full_sync.upserted == 1
    assert count == 1
    assert integration.config["nango"]["connection_id"] == final_connection_id
    assert integration.config["nango"]["superseded_connection_ids"] == [
        first_connection_id,
        second_connection_id,
    ]


@pytest.mark.asyncio
async def test_full_sync_superseded_id_wins_over_an_exact_candidate(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    incoming_connection_id = f"{entity_id}--{user.id}--linkedin--incoming"
    exact = _nango_integration(
        entity_id, incoming_connection_id, owner_user_id=user.id,
    )
    suppressor = _nango_integration(
        entity_id,
        f"{entity_id}--{user.id}--linkedin--current",
        owner_user_id=user.id,
    )
    suppressor.config["nango"]["superseded_connection_ids"] = [
        incoming_connection_id
    ]
    db_session.add_all([exact, suppressor])
    await db_session.commit()
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": incoming_connection_id,
        }
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )

    count = await db_session.scalar(
        select(func.count()).select_from(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )
    assert result.upserted == 0
    assert count == 2


@pytest.mark.asyncio
async def test_full_sync_candidate_select_locks_and_refreshes_before_update(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    connection_ids = [
        f"{entity_id}--{user.id}--linkedin--first",
        f"{entity_id}--{user.id}--linkedin--second",
    ]
    db_session.add_all(
        [
            _nango_integration(entity_id, value, owner_user_id=user.id)
            for value in connection_ids
        ]
    )
    await db_session.commit()
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": value,
        }
        for value in connection_ids
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())

    events: list[str] = []
    candidate_statements = []
    original_execute = db_session.execute

    async def _record_execute(statement, *args, **kwargs):
        get_final_froms = getattr(statement, "get_final_froms", None)
        if (
            callable(get_final_froms)
            and Integration.__table__ in get_final_froms()
            and statement._for_update_arg is not None
        ):
            events.append("candidate_select")
            candidate_statements.append(statement)
        return await original_execute(statement, *args, **kwargs)

    async def _record_profile(**kwargs):
        events.append("profile_http")
        return None

    monkeypatch.setattr(db_session, "execute", _record_execute)
    monkeypatch.setattr(
        nango_oauth,
        "_fetch_linkedin_profile_via_nango",
        _record_profile,
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )

    assert result.upserted == 2
    assert len(candidate_statements) == 2
    for statement in candidate_statements:
        assert statement._for_update_arg is not None
        assert statement.get_execution_options().get("populate_existing") is True
        order_by = list(statement._order_by_clauses)
        assert len(order_by) == 1
        assert order_by[0].name == "id"
        assert order_by[0].table is Integration.__table__
    assert events == [
        "profile_http",
        "profile_http",
        "candidate_select",
        "candidate_select",
    ]


@pytest.mark.asyncio
async def test_full_sync_locks_candidates_in_stable_provider_connection_order(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    linkedin_first = f"{entity_id}--{user.id}--linkedin--a"
    linkedin_second = f"{entity_id}--{user.id}--linkedin--b"
    slack_connection = f"{entity_id}--{user.id}--slack--z"
    integrations = [
        _nango_integration(
            entity_id, linkedin_first, owner_user_id=user.id,
        ),
        _nango_integration(
            entity_id, linkedin_second, owner_user_id=user.id,
        ),
        Integration(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=user.id,
            provider="slack",
            status="active",
            config={
                "nango": {
                    "connection_id": slack_connection,
                    "provider_config_key": "slack",
                }
            },
            credentials={},
        ),
    ]
    db_session.add_all(integrations)
    await db_session.commit()
    _NangoClient.connections = [
        {
            "provider_config_key": "slack",
            "connection_id": slack_connection,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": linkedin_second,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": linkedin_first,
        },
    ]
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    events: list[object] = []
    original_execute = db_session.execute

    async def _record_execute(statement, *args, **kwargs):
        get_final_froms = getattr(statement, "get_final_froms", None)
        if (
            callable(get_final_froms)
            and Integration.__table__ in get_final_froms()
            and statement._for_update_arg is not None
        ):
            events.append("locked_select")
        return await original_execute(statement, *args, **kwargs)

    class _RecordingCredentialService(_CredentialService):
        def store_integration(self, integration: Integration, payload: dict) -> None:
            events.append(
                (
                    "store",
                    payload["provider_config_key"],
                    payload["connection_id"],
                )
            )
            super().store_integration(integration, payload)

    monkeypatch.setattr(db_session, "execute", _record_execute)
    monkeypatch.setattr(
        nango_oauth,
        "get_credential_service",
        lambda: _RecordingCredentialService(),
    )

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )

    assert result.upserted == 3
    assert events == [
        "locked_select",
        ("store", "linkedin", linkedin_first),
        "locked_select",
        ("store", "linkedin", linkedin_second),
        "locked_select",
        ("store", "slack", slack_connection),
    ]


@pytest.mark.asyncio
async def test_full_sync_refreshes_stale_row_after_reconnect_commit(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    old_connection_id = f"{entity_id}--{user.id}--linkedin--old"
    new_connection_id = f"{entity_id}--{user.id}--linkedin--new"
    integration = _nango_integration(
        entity_id, old_connection_id, owner_user_id=user.id,
    )
    db_session.add(integration)
    await db_session.commit()
    assert integration.config["nango"]["connection_id"] == old_connection_id
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())
    monkeypatch.setattr(nango_oauth, "_fetch_linkedin_profile_via_nango", _no_profile)

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": new_connection_id,
        }
    ]
    async with session_factory() as reconnect_session:
        await nango_oauth.sync_connections(
            nango_oauth.SyncConnectionsRequest(
                expected_connection_id=new_connection_id,
                expected_provider_config_key="linkedin",
                replace_integration_id=integration.id,
            ),
            user=user,
            db=reconnect_session,
        )

    # This session still holds the pre-reconnect ORM state. The candidate
    # SELECT must refresh it after acquiring the row lock.
    assert integration.config["nango"]["connection_id"] == old_connection_id
    _NangoClient.connections = [
        {
            "provider_config_key": "linkedin",
            "connection_id": old_connection_id,
        },
        {
            "provider_config_key": "linkedin",
            "connection_id": new_connection_id,
        },
    ]
    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    count = await db_session.scalar(
        select(func.count()).select_from(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider == "linkedin",
        )
    )
    assert result.upserted == 1
    assert count == 1
    assert integration.config["nango"]["connection_id"] == new_connection_id
    assert integration.config["nango"]["superseded_connection_ids"] == [
        old_connection_id
    ]


@pytest.mark.asyncio
async def test_reconnect_sync_keeps_old_row_when_expected_connection_is_absent(
    db_session, monkeypatch
) -> None:
    entity_id = generate_ulid()
    user = _user(entity_id)
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user.id,
        provider="linkedin",
        status="active",
        config={
            "is_default": True,
            "nango": {
                "connection_id": "old-linkedin-connection",
                "provider_config_key": "linkedin",
            },
        },
        credentials={},
        credential_ref="old-ref",
        credential_scheme="test",
    )
    db_session.add(integration)
    await db_session.commit()
    expected = f"{entity_id}--{user.id}--linkedin--{generate_ulid()}"
    _NangoClient.connections = []
    monkeypatch.setattr(nango_oauth, "_load_nango_secret", _stub_secret)
    monkeypatch.setattr(nango_oauth.httpx, "AsyncClient", _NangoClient)
    monkeypatch.setattr(nango_oauth, "get_credential_service", lambda: _CredentialService())

    result = await nango_oauth.sync_connections(
        nango_oauth.SyncConnectionsRequest(
            expected_connection_id=expected,
            expected_provider_config_key="linkedin",
            replace_integration_id=integration.id,
        ),
        user=user,
        db=db_session,
    )

    await db_session.refresh(integration)
    assert result.upserted == 0
    assert integration.config["nango"]["connection_id"] == "old-linkedin-connection"
    assert integration.credential_ref == "old-ref"


def test_entity_account_response_marks_only_nango_backed_rows() -> None:
    assert EntityAccountConnection(id="nango", nango_backed=True).nango_backed is True
    assert EntityAccountConnection(id="native").nango_backed is False


async def _no_profile(**kwargs):
    return None
