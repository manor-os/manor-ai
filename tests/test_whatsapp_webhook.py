"""WhatsApp Cloud API webhook integrity and delivery receipt regressions."""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from packages.core.credentials import get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.models.document import Integration
from tests.test_document_permissions import _auth


_APP_SECRET = "whatsapp-webhook-app-secret"


@pytest.fixture(autouse=True)
def _whatsapp_deployment_config(monkeypatch):
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", _APP_SECRET)
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "whatsapp-verify-token")


async def _current_user(client, headers: dict[str, str]) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _seed_whatsapp_connection(
    client,
    db_session,
    *,
    username: str,
    phone_number_id: str = "whatsapp-phone-id",
) -> ChannelConfig:
    headers = await _auth(client, username)
    user = await _current_user(client, headers)
    integration = Integration(
        id=generate_ulid(),
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        created_by_user_id=user["id"],
        provider="whatsapp",
        credentials={},
        status="active",
    )
    get_credential_service().store_integration(integration, {
        "phone_number_id": phone_number_id,
        "access_token": "whatsapp-access-token",
        "verify_token": "whatsapp-verify-token",
        "app_secret": _APP_SECRET,
    })
    config = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        whatsapp_phone_number_id=phone_number_id,
        credentials={},
        status="active",
    )
    db_session.add_all([integration, config])
    await db_session.commit()
    return config


def _message_payload(*, message_id: str = "wamid.inbound.1") -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "waba-1",
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "whatsapp-phone-id"},
                    "contacts": [{
                        "wa_id": "15550001111",
                        "profile": {"name": "WhatsApp Sender"},
                    }],
                    "messages": [{
                        "from": "15550001111",
                        "id": message_id,
                        "timestamp": "1724457500",
                        "type": "text",
                        "text": {"body": "Please help"},
                    }],
                },
            }],
        }],
    }, separators=(",", ":")).encode()


def _status_payload(*, message_id: str, status: str) -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "waba-1",
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "whatsapp-phone-id"},
                    "statuses": [{
                        "id": message_id,
                        "recipient_id": "15550001111",
                        "status": status,
                        "timestamp": "1724457501",
                    }],
                },
            }],
        }],
    }, separators=(",", ":")).encode()


def _signed_headers(body: bytes) -> dict[str, str]:
    signature = hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={signature}",
    }


@pytest.mark.asyncio
async def test_whatsapp_webhook_rejects_unsigned_payload(client, db_session, monkeypatch):
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    config = await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_unsigned_owner",
    )
    queued: list[dict] = []
    monkeypatch.setattr(
        whatsapp_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )

    async def mark_as_read(self, message_id: str) -> bool:
        return True

    monkeypatch.setattr(WhatsAppAdapter, "mark_as_read", mark_as_read)
    response = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        params={"config_id": config.id},
        content=_message_payload(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 403
    assert queued == []
    count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
        )
    )
    assert count == 0


@pytest.mark.asyncio
async def test_whatsapp_webhook_deduplicates_inbound_message_and_queues_receipt(
    client,
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    config = await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_dedup_owner",
    )
    queued: list[dict] = []
    monkeypatch.setattr(
        whatsapp_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )

    async def mark_as_read(self, message_id: str) -> bool:
        return True

    monkeypatch.setattr(WhatsAppAdapter, "mark_as_read", mark_as_read)
    body = _message_payload(message_id="wamid.inbound.dedup")

    first = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        params={"config_id": config.id},
        content=body,
        headers=_signed_headers(body),
    )
    duplicate = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        params={"config_id": config.id},
        content=body,
        headers=_signed_headers(body),
    )

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert len(queued) == 1
    assert queued[0]["inbound_message_log_id"]
    count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
            MessageLog.external_id == "wamid.inbound.dedup",
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_whatsapp_receipt_claim_is_single_use(db_session, monkeypatch):
    """A repeated WhatsApp message ID must have one dispatch claim."""
    import packages.core.database as db_module
    from apps.api.routers.channels import whatsapp as whatsapp_router

    monkeypatch.setattr(whatsapp_router, "async_session", db_module.async_session)
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="whatsapp-claim-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="whatsapp",
        external_id="wamid.claim-once",
        status="received",
        content="claim me",
    )
    db_session.add(receipt)
    await db_session.commit()

    first, first_claimed = await whatsapp_router._claim_received(
        receipt.channel_config_id,
        receipt.external_id,
    )
    second, second_claimed = await whatsapp_router._claim_received(
        receipt.channel_config_id,
        receipt.external_id,
    )

    assert first is not None and first_claimed is True
    assert second is not None and second_claimed is False
    assert second.status == "queued"


@pytest.mark.asyncio
async def test_whatsapp_fixed_webhook_routes_by_phone_number_id(
    client, db_session, monkeypatch,
):
    """Meta's app-level callback must not require a ChannelConfig query id."""
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    config = await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_fixed_webhook_owner",
    )
    queued: list[dict] = []
    monkeypatch.setattr(
        whatsapp_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    monkeypatch.setattr(WhatsAppAdapter, "mark_as_read", lambda *_args, **_kwargs: True)

    body = _message_payload(message_id="wamid.fixed-route")
    response = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200, response.text
    assert queued and queued[0]["channel_config_id"] == config.id


@pytest.mark.asyncio
async def test_whatsapp_fixed_route_leases_only_the_stored_phone_match(
    client, db_session, monkeypatch,
):
    from apps.api.routers.channels import whatsapp as whatsapp_router

    target = await _seed_whatsapp_connection(
        client,
        db_session,
        username="whatsapp_routing_target",
        phone_number_id="phone-target",
    )
    other = await _seed_whatsapp_connection(
        client,
        db_session,
        username="whatsapp_routing_other",
        phone_number_id="phone-other",
    )
    leased: list[str] = []

    async def lease_once(db, cc, *, reason: str, resolve_nango: bool = False):
        leased.append(cc.id)
        assert resolve_nango is True
        return {
            "phone_number_id": cc.whatsapp_phone_number_id,
            "access_token": "token",
            "app_secret": _APP_SECRET,
            "verify_token": "whatsapp-verify-token",
        }

    monkeypatch.setattr(whatsapp_router, "lease_channel_credentials", lease_once)
    _adapter, resolved = await whatsapp_router._get_adapter_and_config(
        phone_number_id="phone-target",
    )

    assert resolved.id == target.id
    assert resolved.id != other.id
    assert leased == [target.id]


@pytest.mark.asyncio
async def test_whatsapp_fixed_route_uses_central_meta_graph_version(
    client, db_session, monkeypatch,
):
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.external_api_versions import META_GRAPH

    await _seed_whatsapp_connection(
        client,
        db_session,
        username="whatsapp_central_graph_version",
        phone_number_id="phone-central-version",
    )

    adapter, _config = await whatsapp_router._get_adapter_and_config(
        phone_number_id="phone-central-version",
    )

    assert adapter.api_version == META_GRAPH.value


@pytest.mark.asyncio
async def test_whatsapp_adapter_registers_waba_subscription(monkeypatch):
    from packages.core.external_api_versions import META_GRAPH
    from packages.core.services.channels import whatsapp_adapter as adapter_module

    requests: list[tuple[str, dict]] = []

    async def lease(*_args, **_kwargs):
        return {
            "phone_number_id": "phone-1",
            "waba_id": "waba-1",
            "access_token": "token-1",
        }

    class Response:
        is_success = True
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    monkeypatch.setattr(adapter_module, "lease_channel_config_credentials", lease)
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", Client)

    result = await adapter_module.WhatsAppChannelAdapter().register_webhook(
        SimpleNamespace(id="channel-config-1"),
    )

    assert result["registered"] is True
    assert requests[0][0] == (
        f"https://graph.facebook.com/{META_GRAPH.value}/waba-1/subscribed_apps"
    )
    assert requests[0][1]["headers"]["Authorization"] == "Bearer token-1"


@pytest.mark.asyncio
async def test_whatsapp_fixed_webhook_verifies_by_verify_token(
    client, db_session, monkeypatch,
):
    await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_fixed_verify_owner",
    )
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", "meta-app-secret")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "whatsapp-verify-token")
    response = await client.get(
        "/api/v1/channels/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "whatsapp-verify-token",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 200, response.text
    assert response.text == "challenge-123"


@pytest.mark.asyncio
async def test_legacy_whatsapp_verify_does_not_require_app_secret(
    client, db_session, monkeypatch,
):
    from apps.api.routers.channels import whatsapp as whatsapp_router

    config = await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_legacy_verify_owner",
    )

    async def lease_without_webhook_secret(db, cc, *, reason, resolve_nango):
        assert cc.id == config.id
        return {
            "phone_number_id": config.whatsapp_phone_number_id,
            "access_token": "whatsapp-access-token",
            "verify_token": "whatsapp-verify-token",
        }

    monkeypatch.setattr(
        whatsapp_router, "lease_channel_credentials", lease_without_webhook_secret,
    )
    response = await client.get(
        "/api/v1/channels/whatsapp/webhook",
        params={
            "config_id": config.id,
            "hub.mode": "subscribe",
            "hub.verify_token": "whatsapp-verify-token",
            "hub.challenge": "legacy-challenge",
        },
    )

    assert response.status_code == 200, response.text
    assert response.text == "legacy-challenge"


@pytest.mark.asyncio
async def test_whatsapp_webhook_retries_provider_delivery_after_queue_failure(
    client,
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import whatsapp as whatsapp_router
    from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter

    config = await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_queue_retry_owner",
    )
    attempts = 0
    queued: list[dict] = []

    def enqueue(**kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("broker unavailable")
        queued.append(kwargs)

    monkeypatch.setattr(whatsapp_router.dispatch_inbound_task, "delay", enqueue)

    async def mark_as_read(self, message_id: str) -> bool:
        return True

    monkeypatch.setattr(WhatsAppAdapter, "mark_as_read", mark_as_read)
    body = _message_payload(message_id="wamid.inbound.queue-retry")

    failed = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        params={"config_id": config.id},
        content=body,
        headers=_signed_headers(body),
    )
    retried = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        params={"config_id": config.id},
        content=body,
        headers=_signed_headers(body),
    )

    assert failed.status_code == 503
    assert retried.status_code == 200, retried.text
    assert len(queued) == 1
    count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
            MessageLog.external_id == "wamid.inbound.queue-retry",
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_whatsapp_status_webhook_updates_outbound_receipt(client, db_session):
    config = await _seed_whatsapp_connection(
        client, db_session, username="whatsapp_status_owner",
    )
    outbound = MessageLog(
        entity_id=config.entity_id,
        channel_config_id=config.id,
        direction="outbound",
        channel_type="whatsapp",
        from_address="whatsapp-phone-id",
        to_address="15550001111",
        content="Hello",
        external_id="wamid.outbound.status",
        status="sent",
    )
    db_session.add(outbound)
    await db_session.commit()
    body = _status_payload(message_id=outbound.external_id, status="delivered")

    response = await client.post(
        "/api/v1/channels/whatsapp/webhook",
        params={"config_id": config.id},
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(outbound)
    assert outbound.status == "delivered"


def test_whatsapp_connection_requires_app_secret_for_inbound_webhooks(monkeypatch):
    from fastapi import HTTPException
    from apps.api.routers.integrations import _prepare_channel_credentials

    monkeypatch.delenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET")
    with pytest.raises(HTTPException, match="app_secret"):
        _prepare_channel_credentials("whatsapp", {
            "phone_number_id": "whatsapp-phone-id",
            "access_token": "whatsapp-access-token",
            "verify_token": "whatsapp-verify-token",
        })


def test_whatsapp_connection_uses_central_deployment_app_secret(monkeypatch):
    from apps.api.routers.integrations import _prepare_channel_credentials

    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv(
        "NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET",
        "deployment-app-secret",
    )
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "verify-token")
    prepared = _prepare_channel_credentials("whatsapp", {
        "phone_number_id": "whatsapp-phone-id",
        "access_token": "whatsapp-access-token",
    })

    assert prepared["phone_number_id"] == "whatsapp-phone-id"
