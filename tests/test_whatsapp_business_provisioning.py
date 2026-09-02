from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest


class _GraphClient:
    responses: list[dict] = []
    requests: list[dict] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params=None,
        json=None,
        headers=None,
    ) -> httpx.Response:
        self.requests.append({
            "method": method,
            "url": url,
            "params": params,
            "json": json,
            "headers": headers,
        })
        payload = self.responses.pop(0)
        request = httpx.Request(method, url, params=params)
        return httpx.Response(200, json=payload, request=request)


@pytest.fixture(autouse=True)
def _reset_graph_client(monkeypatch):
    from packages.core.services import whatsapp_business_provisioning as module

    _GraphClient.responses = []
    _GraphClient.requests = []
    monkeypatch.setattr(module.httpx, "AsyncClient", _GraphClient)


@pytest.mark.asyncio
async def test_inspect_uses_exact_nango_connection_and_exact_app_id():
    from packages.core.external_api_versions import META_GRAPH
    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
        inspect_whatsapp_business_number,
    )

    _GraphClient.responses = [
        {
            "id": "phone/unsafe",
            "status": "CONNECTED",
            "code_verification_status": "VERIFIED",
            "platform_type": "CLOUD_API",
            "is_on_biz_app": False,
        },
        {"data": [{"id": "other-app", "name": "Other"}, {"id": "app-1"}]},
    ]

    result = await inspect_whatsapp_business_number(
        nango_secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="entity--user--whatsapp--connection",
        phone_number_id="phone/unsafe",
        waba_id="waba/unsafe",
        expected_app_id="app-1",
    )

    assert result.ok is True
    assert result.phone_state is WhatsAppPhoneState.CONNECTED
    assert result.exact_app_subscribed is True
    assert result.phone_number_id == "phone/unsafe"
    assert result.waba_id == "waba/unsafe"
    assert result.app_id == "app-1"
    assert [request["method"] for request in _GraphClient.requests] == ["GET", "GET"]
    assert [urlsplit(request["url"]).path for request in _GraphClient.requests] == [
        f"/proxy/{META_GRAPH.value}/phone%2Funsafe",
        f"/proxy/{META_GRAPH.value}/waba%2Funsafe/subscribed_apps",
    ]
    assert _GraphClient.requests[0]["params"] == {
        "fields": "id,status,code_verification_status,platform_type,is_on_biz_app",
    }
    assert _GraphClient.requests[1]["params"] == {"fields": "id,name"}
    assert all(
        request["headers"] == {
            "Authorization": "Bearer nango-secret",
            "Provider-Config-Key": "whatsapp",
            "Connection-Id": "entity--user--whatsapp--connection",
            "Nango-Proxy-Accept": "application/json",
        }
        for request in _GraphClient.requests
    )


@pytest.mark.asyncio
async def test_inspect_rejects_a_different_subscribed_app():
    from packages.core.services.whatsapp_business_provisioning import (
        inspect_whatsapp_business_number,
    )

    _GraphClient.responses = [
        {"id": "phone-1", "status": "CONNECTED"},
        {"data": [{"id": "different-app", "name": "Different App"}]},
    ]

    result = await inspect_whatsapp_business_number(
        nango_secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="connection-1",
        phone_number_id="phone-1",
        waba_id="waba-1",
        expected_app_id="expected-app",
    )

    assert result.ok is False
    assert result.exact_app_subscribed is False


@pytest.mark.asyncio
async def test_inspect_app_callback_requires_exact_whatsapp_callback():
    from packages.core.external_api_versions import META_GRAPH
    from packages.core.services.whatsapp_business_provisioning import (
        inspect_whatsapp_app_callback,
    )

    _GraphClient.responses = [
        {"access_token": "app-access-token", "token_type": "bearer"},
        {
            "data": [
                {
                    "object": "whatsapp_business_account",
                    "callback_url": "https://staging.example/api/v1/channels/whatsapp/webhook",
                }
            ]
        },
    ]

    result = await inspect_whatsapp_app_callback(
        app_id="app-1",
        app_secret="deployment-app-secret",
        expected_callback_url=(
            "https://staging.example/api/v1/channels/whatsapp/webhook"
        ),
    )

    assert result.ok is True
    assert result.configured_url == result.expected_url
    assert [request["method"] for request in _GraphClient.requests] == ["GET", "GET"]
    assert [urlsplit(request["url"]).path for request in _GraphClient.requests] == [
        f"/{META_GRAPH.value}/oauth/access_token",
        f"/{META_GRAPH.value}/app-1/subscriptions",
    ]
    assert _GraphClient.requests[0]["params"] == {
        "client_id": "app-1",
        "client_secret": "deployment-app-secret",
        "grant_type": "client_credentials",
    }
    assert _GraphClient.requests[1]["headers"] == {
        "Authorization": "Bearer app-access-token",
    }
    assert "deployment-app-secret" not in repr(result)
    assert "app-access-token" not in repr(result)


@pytest.mark.asyncio
async def test_inspect_app_callback_rejects_wrong_object_or_url():
    from packages.core.services.whatsapp_business_provisioning import (
        inspect_whatsapp_app_callback,
    )

    _GraphClient.responses = [
        {"access_token": "app-access-token"},
        {
            "data": [
                {
                    "object": "whatsapp_business_account",
                    "callback_url": "https://production.example/api/v1/channels/whatsapp/webhook",
                },
                {
                    "object": "page",
                    "callback_url": "https://staging.example/api/v1/channels/whatsapp/webhook",
                },
            ]
        },
    ]

    result = await inspect_whatsapp_app_callback(
        app_id="app-1",
        app_secret="deployment-app-secret",
        expected_callback_url=(
            "https://staging.example/api/v1/channels/whatsapp/webhook"
        ),
    )

    assert result.ok is False
    assert result.configured_url == (
        "https://production.example/api/v1/channels/whatsapp/webhook"
    )
    assert result.expected_url == (
        "https://staging.example/api/v1/channels/whatsapp/webhook"
    )


@pytest.mark.asyncio
async def test_disconnect_unsubscribes_app_then_deletes_exact_nango_connection():
    from packages.core.external_api_versions import META_GRAPH
    from packages.core.services.whatsapp_business_provisioning import (
        disconnect_whatsapp_business_account,
    )

    _GraphClient.responses = [{"success": True}, {"success": True}]

    await disconnect_whatsapp_business_account(
        nango_secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="entity--user--whatsapp--connection/unsafe",
        waba_id="waba/unsafe",
    )

    assert [request["method"] for request in _GraphClient.requests] == [
        "DELETE",
        "DELETE",
    ]
    assert [urlsplit(request["url"]).path for request in _GraphClient.requests] == [
        f"/proxy/{META_GRAPH.value}/waba%2Funsafe/subscribed_apps",
        "/connection/entity--user--whatsapp--connection%2Funsafe",
    ]
    assert _GraphClient.requests[0]["headers"] == {
        "Authorization": "Bearer nango-secret",
        "Provider-Config-Key": "whatsapp",
        "Connection-Id": "entity--user--whatsapp--connection/unsafe",
        "Nango-Proxy-Accept": "application/json",
    }
    assert _GraphClient.requests[1]["headers"] == {
        "Authorization": "Bearer nango-secret",
    }
    assert _GraphClient.requests[1]["params"] == {
        "provider_config_key": "whatsapp",
    }


@pytest.mark.asyncio
async def test_provision_registers_phone_then_subscribes_exact_app():
    from packages.core.services.whatsapp_business_provisioning import (
        provision_whatsapp_business_number,
    )

    _GraphClient.responses = [
        {"id": "phone-1", "status": "PENDING"},
        {"data": []},
        {"success": True},
        {"success": True},
        {"id": "phone-1", "status": "CONNECTED"},
        {"data": [{"id": "app-1", "name": "Manor"}]},
    ]

    result = await provision_whatsapp_business_number(
        nango_secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="connection-1",
        phone_number_id="phone-1",
        waba_id="waba-1",
        expected_app_id="app-1",
        registration_pin="123456",
    )

    assert result.ok is True
    assert [request["method"] for request in _GraphClient.requests] == [
        "GET", "GET", "POST", "POST", "GET", "GET",
    ]
    assert urlsplit(_GraphClient.requests[2]["url"]).path.endswith(
        "/phone-1/register"
    )
    assert _GraphClient.requests[2]["json"] == {
        "messaging_product": "whatsapp",
        "pin": "123456",
    }
    assert urlsplit(_GraphClient.requests[3]["url"]).path.endswith(
        "/waba-1/subscribed_apps"
    )
    assert _GraphClient.requests[3]["json"] == {}
    assert "123456" not in repr(result)


@pytest.mark.asyncio
async def test_provision_skips_registration_for_connected_phone():
    from packages.core.services.whatsapp_business_provisioning import (
        provision_whatsapp_business_number,
    )

    _GraphClient.responses = [
        {"id": "phone-1", "status": "CONNECTED"},
        {"data": []},
        {"success": True},
        {"id": "phone-1", "status": "CONNECTED"},
        {"data": [{"id": "app-1"}]},
    ]

    result = await provision_whatsapp_business_number(
        nango_secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="connection-1",
        phone_number_id="phone-1",
        waba_id="waba-1",
        expected_app_id="app-1",
    )

    assert result.ok is True
    assert not any(
        urlsplit(request["url"]).path.endswith("/register")
        for request in _GraphClient.requests
    )


@pytest.mark.asyncio
async def test_provision_requires_pin_without_subscribing_unregistered_phone():
    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
        provision_whatsapp_business_number,
    )

    _GraphClient.responses = [
        {"id": "phone-1", "status": "PENDING"},
        {"data": []},
    ]

    result = await provision_whatsapp_business_number(
        nango_secret="nango-secret",
        provider_config_key="whatsapp",
        connection_id="connection-1",
        phone_number_id="phone-1",
        waba_id="waba-1",
        expected_app_id="app-1",
    )

    assert result.ok is False
    assert result.phone_state is WhatsAppPhoneState.REGISTRATION_REQUIRED
    assert [request["method"] for request in _GraphClient.requests] == ["GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pin",
    ["", "12345", "1234567", "abcdef", "12 456", "１２３４５６"],
)
async def test_invalid_registration_pin_fails_before_network(pin):
    from packages.core.services.whatsapp_business_provisioning import (
        provision_whatsapp_business_number,
    )

    with pytest.raises(ValueError, match="exactly six digits"):
        await provision_whatsapp_business_number(
            nango_secret="nango-secret",
            provider_config_key="whatsapp",
            connection_id="connection-1",
            phone_number_id="phone-1",
            waba_id="waba-1",
            expected_app_id="app-1",
            registration_pin=pin,
        )

    assert _GraphClient.requests == []
