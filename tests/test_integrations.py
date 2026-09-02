"""E2E tests: integrations and channels CRUD, entity isolation, credential safety."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import (
    ChannelConfig,
    MessageLog,
    TwilioVoiceCallSession,
)
from packages.core.models.document import Channel, Integration
from packages.core.models.user import OAuthAccount, User, UserMembership
from tests.test_document_permissions import _create_entity_user

pytestmark = pytest.mark.oss_regression


async def _auth(client: AsyncClient, username: str = "intuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_whatsapp_catalog_requires_completed_provisioning():
    import apps.api.routers.integrations as integrations_router

    pending = SimpleNamespace(
        provider="whatsapp",
        config={
            "whatsapp": {
                "provisioning_status": "pending",
                "readiness_code": "provider_unavailable",
            }
        },
    )
    ready = SimpleNamespace(
        provider="whatsapp",
        config={
            "whatsapp": {
                "provisioning_status": "ready",
                "readiness_code": "ready",
            }
        },
    )

    assert integrations_router._whatsapp_integration_is_ready(pending) is False
    assert integrations_router._whatsapp_integration_is_ready(ready) is True


@pytest.mark.asyncio
async def test_create_integration(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
            "config": {"team_id": "T12345"},
            "credentials": {"bot_token": "xoxb-secret"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "slack"
    assert data["status"] == "active"
    assert data["config"] == {"team_id": "T12345", "is_default": True}
    assert len(data["id"]) == 26


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["whatsapp", "whatsapp_cloud"])
async def test_whatsapp_direct_credentials_are_rejected(
    db_session,
    monkeypatch,
    provider,
):
    import apps.api.routers.integrations as integrations_router

    created_user = await _create_entity_user(
        generate_ulid(),
        f"whatsapp_business_oauth_only_{provider}",
        role="owner",
    )
    user = await db_session.get(User, created_user["id"])
    assert user is not None

    async def unexpected_create(*_args, **_kwargs):
        raise AssertionError("direct WhatsApp credentials reached persistence")

    monkeypatch.setattr(integrations_router, "create_integration", unexpected_create)

    with pytest.raises(HTTPException) as error:
        await integrations_router.create_new_integration(
            integrations_router.CreateIntegrationRequest(
                provider=provider,
                credentials={
                    "access_token": "must-not-be-stored",
                    "phone_number_id": "phone-direct",
                    "waba_id": "waba-direct",
                    "verify_token": "verify-direct",
                    "app_secret": "app-secret-direct",
                },
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 400
    assert "OAuth" in str(error.value.detail)
    stored = (await db_session.execute(
        select(Integration).where(
            Integration.entity_id == user.entity_id,
            Integration.provider.in_(("whatsapp", "whatsapp_cloud"))
        )
    )).scalars().all()
    assert stored == []


@pytest.mark.asyncio
async def test_whatsapp_nango_credentials_cannot_be_edited(
    db_session,
    monkeypatch,
):
    import apps.api.routers.integrations as integrations_router

    created_user = await _create_entity_user(
        generate_ulid(),
        "whatsapp_nango_edit_rejected",
        role="owner",
    )
    user = await db_session.get(User, created_user["id"])
    assert user is not None
    integration = Integration(
        id=generate_ulid(),
        entity_id=user.entity_id,
        owner_user_id=user.id,
        created_by_user_id=user.id,
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": f"{user.entity_id}--{user.id}--whatsapp--connection",
                "provider_config_key": "whatsapp",
            },
            "whatsapp": {
                "waba_id": "waba-1",
                "phone_number_id": "phone-1",
            },
        },
        credentials={},
    )
    db_session.add(integration)
    await db_session.commit()

    async def unexpected_update(*_args, **_kwargs):
        raise AssertionError("Nango-backed WhatsApp credentials reached persistence")

    monkeypatch.setattr(integrations_router, "update_integration", unexpected_update)

    with pytest.raises(HTTPException) as error:
        await integrations_router.update_one_integration(
            integration.id,
            integrations_router.UpdateIntegrationRequest(
                credentials={
                    "access_token": "must-not-be-stored",
                    "phone_number_id": "replacement-phone",
                },
            ),
            user=user,
            db=db_session,
        )

    assert error.value.status_code == 400
    assert "OAuth" in str(error.value.detail)


async def _seed_whatsapp_delete_account(db_session, owner: dict):
    connection_id = (
        f"{owner['entity_id']}--{owner['id']}--whatsapp--connection"
    )
    integration = Integration(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        created_by_user_id=owner["id"],
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": connection_id,
                "provider_config_key": "whatsapp",
            },
            "whatsapp": {
                "waba_id": "waba-delete",
                "phone_number_id": "phone-delete",
                "provisioning_status": "ready",
                "readiness_code": "ready",
            },
        },
        credentials={},
        credential_ref=f"test-ref:{connection_id}",
        credential_scheme="test",
    )
    db_session.add(integration)
    await db_session.flush()
    channel_config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        name="WhatsApp",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        whatsapp_phone_number_id="phone-delete",
        config={
            "connection_kind": "integration",
            "connection_id": integration.id,
            "integration_id": integration.id,
        },
        credentials={},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.flush()
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
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
    return integration, channel_config, binding, connection_id


@pytest.mark.asyncio
async def test_whatsapp_delete_cleans_provider_before_local_rows(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import apps.api.routers.integrations as integrations_router

    headers = await _auth(client, "whatsapp_delete_success")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    integration, channel_config, binding, connection_id = (
        await _seed_whatsapp_delete_account(db_session, owner)
    )
    integration_id = integration.id
    channel_config_id = channel_config.id
    binding_id = binding.id
    cleanup_calls: list[dict] = []

    async def cleanup(**kwargs):
        cleanup_calls.append(kwargs)

    async def nango_secret(*_args, **_kwargs):
        return "test-nango-secret"

    monkeypatch.setattr(integrations_router, "get_nango_secret", nango_secret)
    monkeypatch.setattr(
        integrations_router,
        "disconnect_whatsapp_business_account",
        cleanup,
        raising=False,
    )

    response = await client.delete(
        "/api/v1/integrations/mcp-servers/whatsapp/entity-accounts/"
        f"{integration_id}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    assert cleanup_calls == [{
        "nango_secret": "test-nango-secret",
        "provider_config_key": "whatsapp",
        "connection_id": connection_id,
        "waba_id": "waba-delete",
    }]
    db_session.expire_all()
    assert await db_session.get(Integration, integration_id) is None
    assert await db_session.get(ChannelConfig, channel_config_id) is None
    assert await db_session.get(Channel, binding_id) is None


@pytest.mark.asyncio
async def test_whatsapp_delete_failure_stops_route_and_enqueues_bounded_retry(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import apps.api.routers.integrations as integrations_router
    from packages.core.tasks import channel_tasks

    headers = await _auth(client, "whatsapp_delete_retry")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    integration, channel_config, binding, _connection_id = (
        await _seed_whatsapp_delete_account(db_session, owner)
    )
    integration_id = integration.id
    channel_config_id = channel_config.id
    binding_id = binding.id

    async def unavailable(**_kwargs):
        raise RuntimeError("secret-shaped provider diagnostic")

    async def nango_secret(*_args, **_kwargs):
        return "test-nango-secret"

    retry_jobs: list[dict] = []
    monkeypatch.setattr(integrations_router, "get_nango_secret", nango_secret)
    monkeypatch.setattr(
        integrations_router,
        "disconnect_whatsapp_business_account",
        unavailable,
        raising=False,
    )
    monkeypatch.setattr(
        channel_tasks,
        "disconnect_whatsapp_business_task",
        SimpleNamespace(delay=lambda **kwargs: retry_jobs.append(kwargs)),
        raising=False,
    )

    response = await client.delete(
        "/api/v1/integrations/mcp-servers/whatsapp/entity-accounts/"
        f"{integration_id}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    db_session.expire_all()
    retained = await db_session.get(Integration, integration_id)
    retained_config = await db_session.get(ChannelConfig, channel_config_id)
    retained_binding = await db_session.get(Channel, binding_id)
    assert retained is not None and retained.status == "disconnecting"
    assert retained_config is not None and retained_config.status == "disconnecting"
    assert retained_binding is not None
    assert retained.config["whatsapp_disconnect"]["status"] == "retry_pending"
    assert "secret-shaped" not in str(retained.config)
    assert retry_jobs == [{
        "entity_id": owner["entity_id"],
        "integration_id": integration_id,
    }]


@pytest.mark.asyncio
async def test_whatsapp_delete_with_incomplete_provider_metadata_fails_closed(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.tasks import channel_tasks

    headers = await _auth(client, "whatsapp_delete_incomplete")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    integration, channel_config, binding, _connection_id = (
        await _seed_whatsapp_delete_account(db_session, owner)
    )
    integration_id = integration.id
    channel_config_id = channel_config.id
    binding_id = binding.id
    integration.config = {
        **integration.config,
        "whatsapp": {"phone_number_id": "phone-delete"},
    }
    await db_session.commit()

    retry_jobs: list[dict] = []
    monkeypatch.setattr(
        channel_tasks,
        "disconnect_whatsapp_business_task",
        SimpleNamespace(delay=lambda **kwargs: retry_jobs.append(kwargs)),
    )

    response = await client.delete(
        "/api/v1/integrations/mcp-servers/whatsapp/entity-accounts/"
        f"{integration_id}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    db_session.expire_all()
    retained = await db_session.get(Integration, integration_id)
    retained_config = await db_session.get(ChannelConfig, channel_config_id)
    retained_binding = await db_session.get(Channel, binding_id)
    assert retained is not None and retained.status == "disconnecting"
    assert retained_config is not None and retained_config.status == "disconnecting"
    assert retained_binding is not None
    assert retained.config["whatsapp_disconnect"]["status"] == "retry_pending"
    assert retry_jobs == [{
        "entity_id": owner["entity_id"],
        "integration_id": integration_id,
    }]


@pytest.mark.asyncio
async def test_whatsapp_broken_wiring_is_not_ready_in_inventory(db_session):
    from packages.core.services.integration_service import get_integration_inventory

    entity_id = generate_ulid()
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": f"{entity_id}--user--whatsapp--connection",
                "provider_config_key": "whatsapp",
            },
            "whatsapp": {
                "waba_id": "waba-1",
                "phone_number_id": "phone-1",
                "subscription_status": "ready",
            },
            "last_health_check": {
                "ok": True,
                "detail": "Graph API reachable",
                "wiring": {"ok": False, "detail": "No subscribed app"},
            },
        },
        credentials={},
    )
    db_session.add(integration)
    await db_session.commit()

    inventory = await get_integration_inventory(db_session, entity_id)
    account = next(
        item for item in inventory["integrations"] if item.get("id") == integration.id
    )
    channel = next(item for item in inventory["channels"] if item["key"] == "whatsapp")

    assert account["ready"] is False
    assert channel["ready"] is False


@pytest.mark.asyncio
async def test_whatsapp_webhook_retry_promotes_failed_connection_to_ready(
    db_session,
    monkeypatch,
):
    import apps.api.routers.integrations as integrations_router

    entity_id = generate_ulid()
    waba_id = generate_ulid()
    phone_number_id = generate_ulid()
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": f"{entity_id}--user--whatsapp--connection",
                "provider_config_key": "whatsapp",
            },
            "whatsapp": {
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "subscription_status": "failed",
                "subscription_error": "Graph unavailable",
            },
            "last_health_check": {
                "ok": False,
                "reason_code": "webhook_not_ready",
                "detail": "Graph unavailable",
                "wiring": {"ok": False, "detail": "Graph unavailable"},
            },
        },
        credentials={},
    )
    channel_config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        whatsapp_phone_number_id=phone_number_id,
        config={
            "integration_id": integration.id,
            "whatsapp_registration_pending": True,
            "whatsapp_last_registration_error": "Graph unavailable",
        },
        credentials={},
        status="active",
    )
    db_session.add_all([integration, channel_config])
    await db_session.commit()

    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
        WhatsAppProvisioningResult,
    )

    provisioning_calls: list[dict] = []

    async def provision(**kwargs):
        provisioning_calls.append(kwargs)
        return WhatsAppProvisioningResult(
            ok=True,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=True,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            app_id="meta-app-id",
            detail="WhatsApp Business route is provisioned",
        )

    async def nango_secret(*_args, **_kwargs):
        return "nango-secret"

    monkeypatch.setattr(integrations_router, "get_nango_secret", nango_secret)
    monkeypatch.setattr(
        integrations_router,
        "load_whatsapp_business_config",
        lambda: SimpleNamespace(app_id="meta-app-id"),
    )
    monkeypatch.setattr(
        integrations_router,
        "provision_whatsapp_business_number",
        provision,
    )

    failures = await integrations_router._register_integration_channel_webhooks(
        db_session,
        entity_id=entity_id,
        integration_id=integration.id,
    )
    await db_session.refresh(integration)
    await db_session.refresh(channel_config)

    assert failures == []
    assert provisioning_calls == [{
        "nango_secret": "nango-secret",
        "provider_config_key": "whatsapp",
        "connection_id": f"{entity_id}--user--whatsapp--connection",
        "phone_number_id": phone_number_id,
        "waba_id": waba_id,
        "expected_app_id": "meta-app-id",
        "registration_pin": None,
    }]
    assert integration.config["whatsapp"]["provisioning_status"] == "ready"
    assert integration.config["whatsapp"]["readiness_code"] == "ready"
    assert integration.config["whatsapp"]["subscription_status"] == "ready"
    assert "subscription_error" not in integration.config["whatsapp"]
    assert integration.config["last_health_check"]["ok"] is True
    assert channel_config.status == "active"
    assert channel_config.config["whatsapp_registration_pending"] is False
    assert channel_config.config["whatsapp_last_registration_error"] is None


@pytest.mark.asyncio
async def test_whatsapp_registration_pin_retry_uses_owner_exact_connection(
    db_session,
    monkeypatch,
):
    import apps.api.routers.integrations as integrations_router
    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
        WhatsAppProvisioningResult,
    )

    created_user = await _create_entity_user(
        generate_ulid(),
        "whatsapp_registration_pin_owner",
        role="owner",
    )
    user = await db_session.get(User, created_user["id"])
    assert user is not None
    waba_id = generate_ulid()
    phone_number_id = generate_ulid()
    connection_id = f"{user.entity_id}--{user.id}--whatsapp--connection"
    integration = Integration(
        id=generate_ulid(),
        entity_id=user.entity_id,
        owner_user_id=user.id,
        created_by_user_id=user.id,
        provider="whatsapp",
        status="active",
        config={
            "nango": {
                "connection_id": connection_id,
                "provider_config_key": "whatsapp",
            },
            "whatsapp": {
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "provisioning_status": "registration_required",
                "readiness_code": "phone_not_registered",
            },
        },
        credentials={},
    )
    channel_config = ChannelConfig(
        entity_id=user.entity_id,
        owner_user_id=user.id,
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        whatsapp_phone_number_id=phone_number_id,
        config={"integration_id": integration.id},
        credentials={},
        status="error",
    )
    db_session.add_all([integration, channel_config])
    await db_session.commit()

    async def nango_secret(*_args, **_kwargs):
        return "nango-secret"

    provisioning_calls: list[dict] = []

    async def provision(**kwargs):
        provisioning_calls.append(kwargs)
        return WhatsAppProvisioningResult(
            ok=True,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=True,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            app_id=kwargs["expected_app_id"],
            detail="WhatsApp Business route is provisioned",
        )

    monkeypatch.setattr(integrations_router, "get_nango_secret", nango_secret)
    monkeypatch.setattr(
        integrations_router,
        "load_whatsapp_business_config",
        lambda: SimpleNamespace(app_id="meta-app-id"),
    )
    monkeypatch.setattr(
        integrations_router,
        "provision_whatsapp_business_number",
        provision,
    )

    response = await integrations_router.retry_whatsapp_business_provisioning(
        integration.id,
        integrations_router.WhatsAppProvisioningRetryRequest(
            registration_pin="654321",
        ),
        user=user,
        db=db_session,
    )

    assert response.ok is True
    assert response.integration_id == integration.id
    assert response.readiness_code == "ready"
    assert provisioning_calls[0]["connection_id"] == connection_id
    assert provisioning_calls[0]["registration_pin"] == "654321"
    await db_session.refresh(integration)
    await db_session.refresh(channel_config)
    assert "654321" not in str(integration.config)
    assert channel_config.status == "active"


@pytest.mark.asyncio
async def test_whatsapp_reconnect_pin_retry_swaps_only_after_ready(
    db_session,
    monkeypatch,
):
    import apps.api.routers.integrations as integrations_router
    from packages.core.services.whatsapp_business_provisioning import (
        WhatsAppPhoneState,
        WhatsAppProvisioningResult,
    )

    created_user = await _create_entity_user(
        generate_ulid(),
        "whatsapp_reconnect_pin_owner",
        role="owner",
    )
    user = await db_session.get(User, created_user["id"])
    assert user is not None
    old_connection_id = f"{user.entity_id}--{user.id}--whatsapp--old"
    new_connection_id = f"{user.entity_id}--{user.id}--whatsapp--replacement"
    old_phone_number_id = generate_ulid()
    new_phone_number_id = generate_ulid()
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
                "waba_id": "old-waba",
                "phone_number_id": old_phone_number_id,
                "provisioning_status": "ready",
                "readiness_code": "ready",
            },
            "whatsapp_reconnect": {
                "status": "registration_required",
                "readiness_code": "phone_not_registered",
                "provider_config_key": "whatsapp",
                "connection_id": new_connection_id,
                "waba_id": "replacement-waba",
                "phone_number_id": new_phone_number_id,
                "display_name": "Replacement Number",
                "connected_by_user_id": user.id,
                "attempt_count": 1,
            },
        },
        credentials={},
    )
    channel_config = ChannelConfig(
        entity_id=user.entity_id,
        owner_user_id=user.id,
        channel_type="whatsapp",
        provider="whatsapp_cloud",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        whatsapp_phone_number_id=old_phone_number_id,
        config={"integration_id": integration.id},
        credentials={},
        status="active",
    )
    db_session.add_all([integration, channel_config])
    await db_session.flush()
    binding = Channel(
        entity_id=user.entity_id,
        user_id=user.id,
        type="whatsapp",
        config={
            "integration_id": integration.id,
            "channel_config_id": channel_config.id,
        },
        status="active",
    )
    db_session.add(binding)
    await db_session.commit()
    binding_id = binding.id

    async def nango_secret(*_args, **_kwargs):
        return "nango-secret"

    provisioning_calls: list[dict] = []

    async def provision(**kwargs):
        provisioning_calls.append(kwargs)
        return WhatsAppProvisioningResult(
            ok=True,
            phone_state=WhatsAppPhoneState.CONNECTED,
            exact_app_subscribed=True,
            phone_number_id=kwargs["phone_number_id"],
            waba_id=kwargs["waba_id"],
            app_id=kwargs["expected_app_id"],
            detail="Replacement is ready",
        )

    class CredentialService:
        @staticmethod
        def store_integration(row, payload):
            row.credentials = {}
            row.credential_ref = f"test-ref:{payload['connection_id']}"
            row.credential_scheme = "test"

    retired: list[dict] = []

    async def retire(**kwargs):
        retired.append(kwargs)
        raise RuntimeError("provider retirement unavailable")

    def unavailable_broker(**_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(integrations_router, "get_nango_secret", nango_secret)
    monkeypatch.setattr(
        integrations_router,
        "load_whatsapp_business_config",
        lambda: SimpleNamespace(app_id="meta-app-id"),
    )
    monkeypatch.setattr(
        integrations_router,
        "provision_whatsapp_business_number",
        provision,
    )
    monkeypatch.setattr(
        integrations_router,
        "get_credential_service",
        lambda: CredentialService(),
    )
    monkeypatch.setattr(integrations_router, "delete_nango_connection", retire)
    from packages.core.tasks import channel_tasks

    monkeypatch.setattr(
        channel_tasks,
        "retire_nango_connection_task",
        SimpleNamespace(delay=unavailable_broker),
    )

    response = await integrations_router.retry_whatsapp_business_provisioning(
        integration.id,
        integrations_router.WhatsAppProvisioningRetryRequest(
            registration_pin="654321",
        ),
        user=user,
        db=db_session,
    )

    assert response.ok is True
    assert provisioning_calls[0]["connection_id"] == new_connection_id
    assert provisioning_calls[0]["registration_pin"] == "654321"
    await db_session.refresh(integration)
    await db_session.refresh(channel_config)
    await db_session.refresh(binding)
    assert integration.config["nango"]["connection_id"] == new_connection_id
    assert "whatsapp_reconnect" not in integration.config
    assert integration.config["whatsapp_retirement"]["connection_id"] == (
        old_connection_id
    )
    assert "unavailable" not in str(integration.config)
    assert "654321" not in str(integration.config)
    assert channel_config.whatsapp_phone_number_id == new_phone_number_id
    assert channel_config.status == "active"
    assert binding.id == binding_id and binding.status == "active"
    assert retired == [{
        "nango_secret": "nango-secret",
        "provider_config_key": "whatsapp",
        "connection_id": old_connection_id,
    }]


@pytest.mark.asyncio
async def test_create_integration_reports_credential_backend_failure(client: AsyncClient, monkeypatch):
    from packages.core.credentials import CredentialError
    import apps.api.routers.integrations as integrations_router

    async def broken_create_integration(*_args, **_kwargs):
        raise CredentialError("vault token is invalid")

    monkeypatch.setattr(integrations_router, "create_integration", broken_create_integration)

    headers = await _auth(client, "int_credential_backend_down")
    resp = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "email",
            "credentials": {"username": "ops@example.test", "password": "secret"},
        },
    )

    assert resp.status_code == 503
    assert "Credential backend" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_integrations(client: AsyncClient):
    headers = await _auth(client)
    await client.post("/api/v1/integrations", headers=headers, json={"provider": "slack"})
    await client.post("/api/v1/integrations", headers=headers, json={"provider": "teams"})

    resp = await client.get("/api/v1/integrations", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_update_integration(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
        },
    )
    iid = create.json()["id"]

    resp = await client.put(
        f"/api/v1/integrations/{iid}",
        headers=headers,
        json={
            "status": "disabled",
            "config": {"updated": True},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert resp.json()["config"] == {"updated": True, "is_default": True}


@pytest.mark.asyncio
async def test_multiple_entity_accounts_keep_one_default(client: AsyncClient):
    """Email and API-key integrations may connect more than one account."""
    headers = await _auth(client, "integration_multi_account")

    first = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "email",
            "config": {"name": "Support", "from_address": "support@example.com"},
            "credentials": {"username": "support@example.com", "password": "one"},
        },
    )
    second = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "email",
            "config": {"name": "Sales", "from_address": "sales@example.com"},
            "credentials": {"username": "sales@example.com", "password": "two"},
        },
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["config"]["is_default"] is True
    assert second.json()["config"]["is_default"] is False

    listed = await client.get("/api/v1/integrations", headers=headers)
    email_rows = [row for row in listed.json() if row["provider"] == "email"]
    assert len(email_rows) == 2
    assert sum(bool(row["config"].get("is_default")) for row in email_rows) == 1


@pytest.mark.asyncio
async def test_create_integration_ignores_a_malformed_default_sibling(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationAccountFactory,
    )

    headers = await _auth(client, "integration_malformed_default_sibling")
    first = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Malformed sibling"},
            "credentials": {"bearer_token": "first-secret"},
        },
    )
    assert first.status_code == 201, first.text

    original_from_entity = RuntimeIntegrationAccountFactory.from_entity

    def _fail_for_existing_sibling(row, *, actor_user_id=None, provider=None):
        if row.id == first.json()["id"]:
            raise ValueError("malformed sibling metadata")
        return original_from_entity(
            row,
            actor_user_id=actor_user_id,
            provider=provider,
        )

    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_entity",
        _fail_for_existing_sibling,
    )
    second = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Healthy sibling"},
            "credentials": {"bearer_token": "second-secret"},
        },
    )

    assert second.status_code == 201, second.text
    listed = await client.get("/api/v1/integrations", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {
        row["id"]
        for row in listed.json()
        if row["provider"] == "webhook"
    } == {first.json()["id"], second.json()["id"]}


@pytest.mark.asyncio
async def test_integration_crud_keeps_default_state_service_owned(
    client: AsyncClient,
):
    headers = await _auth(client, "integration_default_crud")

    first = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Primary", "is_default": False},
        },
    )
    second = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Secondary", "is_default": True},
        },
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["config"]["is_default"] is True
    assert second.json()["config"]["is_default"] is False

    forced = await client.put(
        f"/api/v1/integrations/{second.json()['id']}",
        headers=headers,
        json={"config": {"name": "Secondary edited", "is_default": True}},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["config"]["is_default"] is False

    deleted = await client.delete(
        f"/api/v1/integrations/{first.json()['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    listed = await client.get("/api/v1/integrations", headers=headers)
    webhook_rows = [
        row for row in listed.json() if row["provider"] == "webhook"
    ]
    assert len(webhook_rows) == 1
    assert webhook_rows[0]["id"] == second.json()["id"]
    assert webhook_rows[0]["config"]["is_default"] is True


@pytest.mark.asyncio
async def test_integration_crud_keeps_nango_metadata_server_owned(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "integration_nango_config_guard")
    forged_connection_id = (
        f"{generate_ulid()}--{generate_ulid()}--linkedin--{generate_ulid()}"
    )

    created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "linkedin",
            "config": {
                "name": "Client supplied",
                "nango": {
                    "connection_id": forged_connection_id,
                    "provider_config_key": "linkedin",
                },
            },
        },
    )
    assert created.status_code == 201, created.text
    assert "nango" not in created.json()["config"]

    integration = await db_session.get(Integration, created.json()["id"])
    assert integration is not None
    integration_id = integration.id
    assert "nango" not in integration.config
    trusted_connection_id = (
        f"{integration.entity_id}--{integration.owner_user_id}--linkedin--"
        f"{generate_ulid()}"
    )
    integration.config = {
        "name": "Server managed",
        "is_default": True,
        "nango": {
            "connection_id": trusted_connection_id,
            "provider_config_key": "linkedin",
        },
    }
    await db_session.commit()

    updated = await client.put(
        f"/api/v1/integrations/{integration_id}",
        headers=headers,
        json={
            "config": {
                "name": "Renamed",
                "nango": {
                    "connection_id": forged_connection_id,
                    "provider_config_key": "linkedin",
                },
            },
        },
    )
    assert updated.status_code == 200, updated.text
    assert "nango" not in updated.json()["config"]

    db_session.expire_all()
    stored = await db_session.get(Integration, integration_id)
    assert stored is not None
    assert stored.config["name"] == "Renamed"
    assert stored.config["nango"]["connection_id"] == trusted_connection_id


@pytest.mark.asyncio
async def test_integration_provider_is_immutable_and_credentials_remain_readable(
    client: AsyncClient,
):
    headers = await _auth(client, "integration_provider_immutable")
    created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Immutable webhook"},
            "credentials": {
                "url": "https://immutable.example.test/hook",
                "auth_token": "immutable-secret",
            },
        },
    )
    assert created.status_code == 201, created.text

    changed = await client.put(
        f"/api/v1/integrations/{created.json()['id']}",
        headers=headers,
        json={"provider": "email"},
    )
    assert changed.status_code == 422, changed.text
    assert "cannot be changed" in changed.json()["detail"]

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        row = await db.get(Integration, created.json()["id"])
        assert row is not None
        assert row.provider == "webhook"
        leased = get_credential_service().lease_integration(
            row,
            requester=Requester(kind="system", id="provider-immutable-test"),
            reason="test_integration_provider_is_immutable",
        )
    assert leased["auth_token"] == "immutable-secret"


@pytest.mark.asyncio
async def test_concurrent_integration_creates_keep_one_default(
    client: AsyncClient,
):
    headers = await _auth(client, "integration_default_concurrent_create")

    first, second = await asyncio.gather(
        client.post(
            "/api/v1/integrations",
            headers=headers,
            json={"provider": "webhook", "config": {"name": "First"}},
        ),
        client.post(
            "/api/v1/integrations",
            headers=headers,
            json={"provider": "webhook", "config": {"name": "Second"}},
        ),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    listed = await client.get("/api/v1/integrations", headers=headers)
    webhook_rows = [
        row for row in listed.json() if row["provider"] == "webhook"
    ]
    assert len(webhook_rows) == 2
    assert sum(
        bool(row["config"].get("is_default")) for row in webhook_rows
    ) == 1


@pytest.mark.asyncio
async def test_concurrent_set_default_and_delete_keep_a_default(
    client: AsyncClient,
):
    headers = await _auth(client, "integration_default_concurrent_delete")
    first = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "webhook", "config": {"name": "Primary"}},
    )
    second = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "webhook", "config": {"name": "Secondary"}},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    switched, deleted = await asyncio.gather(
        client.post(
            "/api/v1/integrations/mcp-servers/webhook/"
            f"entity-accounts/{second.json()['id']}/set-default",
            headers=headers,
        ),
        client.delete(
            "/api/v1/integrations/mcp-servers/webhook/"
            f"entity-accounts/{second.json()['id']}",
            headers=headers,
        ),
    )

    assert switched.status_code in {204, 404}, switched.text
    assert deleted.status_code == 204, deleted.text
    listed = await client.get("/api/v1/integrations", headers=headers)
    webhook_rows = [
        row for row in listed.json() if row["provider"] == "webhook"
    ]
    assert len(webhook_rows) == 1
    assert webhook_rows[0]["id"] == first.json()["id"]
    assert webhook_rows[0]["config"]["is_default"] is True


@pytest.mark.asyncio
async def test_concurrent_update_and_delete_do_not_use_a_stale_integration(
    client: AsyncClient,
):
    headers = await _auth(client, "integration_concurrent_update_delete")
    created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "webhook", "config": {"name": "Concurrent"}},
    )
    assert created.status_code == 201, created.text

    updated, deleted = await asyncio.gather(
        client.put(
            f"/api/v1/integrations/{created.json()['id']}",
            headers=headers,
            json={"config": {"name": "Concurrent edited"}},
        ),
        client.delete(
            f"/api/v1/integrations/{created.json()['id']}",
            headers=headers,
        ),
    )

    assert updated.status_code in {200, 404}, updated.text
    assert deleted.status_code == 204, deleted.text
    fetched = await client.get(
        f"/api/v1/integrations/{created.json()['id']}",
        headers=headers,
    )
    assert fetched.status_code == 404


@pytest.mark.asyncio
async def test_delete_integration(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
        },
    )
    iid = create.json()["id"]

    resp = await client.delete(f"/api/v1/integrations/{iid}", headers=headers)
    assert resp.status_code == 204

    resp2 = await client.get(f"/api/v1/integrations/{iid}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_delete_channel_integration_cleans_channel_bridge(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "int_delete_bridge")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "email",
            "credentials": {
                "host": "smtp.example.test",
                "port": "587",
                "username": "bridge@example.test",
                "password": "secret",
            },
        },
    )
    assert create.status_code == 201
    integration = create.json()

    channel_config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == integration["entity_id"],
                ChannelConfig.channel_type == "email",
                ChannelConfig.config["integration_id"].astext == integration["id"],
            )
        )
    ).scalar_one()
    channel = Channel(
        id=generate_ulid(),
        entity_id=integration["entity_id"],
        workspace_id=None,
        type="email",
        name="Bridge binding",
        config={"channel_config_id": channel_config.id},
        status="active",
    )
    db_session.add(channel)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/integrations/{integration['id']}", headers=headers)
    assert resp.status_code == 204

    assert (
        await db_session.execute(select(ChannelConfig).where(ChannelConfig.id == channel_config.id))
    ).scalar_one_or_none() is None
    assert (await db_session.execute(select(Channel).where(Channel.id == channel.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_twilio_integration_cancels_pending_voice_sessions(
    client: AsyncClient,
    db_session,
):
    from packages.core.services.voice.call_sessions import (
        create_call_session,
        finish_call_session,
    )

    headers = await _auth(client, "twilio_disconnect_sessions")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "config": {"phone_number": "+14155550110"},
            "credentials": {
                "account_sid": "AC-disconnect",
                "auth_token": "auth-token",
                "phone_number": "+14155550110",
            },
        },
    )
    assert create.status_code == 201, create.text
    integration = create.json()
    voice_config = (await db_session.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == integration["entity_id"],
            ChannelConfig.channel_type == "twilio_voice",
            ChannelConfig.credential_source_id == integration["id"],
        )
    )).scalar_one()

    pending, _ = await create_call_session(
        db_session,
        entity_id=voice_config.entity_id,
        owner_user_id=voice_config.owner_user_id,
        workspace_id=voice_config.workspace_id,
        channel_config_id=voice_config.id,
        direction="outbound",
        call_sid="CA-disconnect-pending",
        from_number="+14155550110",
        to_number="+14155550199",
    )
    completed, _ = await create_call_session(
        db_session,
        entity_id=voice_config.entity_id,
        owner_user_id=voice_config.owner_user_id,
        workspace_id=voice_config.workspace_id,
        channel_config_id=voice_config.id,
        direction="inbound",
        call_sid="CA-disconnect-completed",
        from_number="+14155550199",
        to_number="+14155550110",
    )
    await finish_call_session(db_session, completed, status="completed")
    await db_session.commit()
    pending_id = pending.id
    completed_id = completed.id
    voice_config_id = voice_config.id
    owner_user_id = voice_config.owner_user_id

    response = await client.delete(
        f"/api/v1/integrations/{integration['id']}",
        headers=headers,
    )
    assert response.status_code == 204, response.text

    db_session.expire_all()
    assert await db_session.get(ChannelConfig, voice_config_id) is None
    pending_after = await db_session.get(TwilioVoiceCallSession, pending_id)
    completed_after = await db_session.get(TwilioVoiceCallSession, completed_id)
    assert pending_after is not None
    assert pending_after.status == "canceled"
    assert pending_after.ended_at is not None
    assert pending_after.owner_user_id == owner_user_id
    assert completed_after is not None
    assert completed_after.status == "completed"
    assert completed_after.owner_user_id == owner_user_id


@pytest.mark.asyncio
async def test_disconnect_oauth_account_cleans_channel_bridge(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.user import OAuthAccount

    headers = await _auth(client, "oauth_disconnect_bridge")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    connection_id = generate_ulid()
    orphaned_connection_id = generate_ulid()
    sibling_connection_id = generate_ulid()
    channel_config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        name="Manor QA",
        credential_source_kind="oauth_account",
        credential_source_id=connection_id,
        config={
            "connection_kind": "oauth_account",
            "connection_id": connection_id,
            "slack_app_id": "A-MANOR",
            "slack_team_id": "T-MANOR",
        },
        credentials={},
        status="active",
    )
    orphaned_channel_config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        name="Deleted Slack connection",
        credential_source_kind="oauth_account",
        credential_source_id=orphaned_connection_id,
        config={
            "connection_kind": "oauth_account",
            "connection_id": orphaned_connection_id,
        },
        credentials={},
        status="active",
    )
    sibling_channel_config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        name="Other Slack workspace",
        credential_source_kind="oauth_account",
        credential_source_id=sibling_connection_id,
        config={
            "connection_kind": "oauth_account",
            "connection_id": sibling_connection_id,
        },
        credentials={},
        status="active",
    )
    db_session.add_all([
        OAuthAccount(
            id=connection_id,
            user_id=owner["id"],
            provider="slack",
            provider_user_id="slack:A-MANOR:team:T-MANOR",
            profile={"app_id": "A-MANOR", "team_id": "T-MANOR"},
        ),
        OAuthAccount(
            id=sibling_connection_id,
            user_id=owner["id"],
            provider="slack",
            provider_user_id="slack:A-MANOR:team:T-OTHER",
            profile={"app_id": "A-MANOR", "team_id": "T-OTHER"},
        ),
        channel_config,
        orphaned_channel_config,
        sibling_channel_config,
    ])
    await db_session.flush()
    channel = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Manor QA",
        config={"channel_config_id": channel_config.id},
        status="active",
    )
    orphaned_channel = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Deleted Slack connection",
        config={"channel_config_id": orphaned_channel_config.id},
        status="active",
    )
    sibling_channel = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Other Slack workspace",
        config={"channel_config_id": sibling_channel_config.id},
        status="active",
    )
    db_session.add_all([channel, orphaned_channel, sibling_channel])
    await db_session.commit()

    response = await client.delete(
        f"/api/v1/integrations/mcp-servers/slack/connections/{connection_id}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    assert (await db_session.execute(
        select(OAuthAccount).where(OAuthAccount.id == connection_id)
    )).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(ChannelConfig).where(
            ChannelConfig.id.in_([channel_config.id, orphaned_channel_config.id])
        )
    )).scalars().all() == []
    assert (await db_session.execute(
        select(Channel).where(Channel.id.in_([channel.id, orphaned_channel.id]))
    )).scalars().all() == []
    assert await db_session.get(OAuthAccount, sibling_connection_id) is not None
    assert await db_session.get(ChannelConfig, sibling_channel_config.id) is not None
    assert await db_session.get(Channel, sibling_channel.id) is not None
    bindings = await client.get(
        "/api/v1/integrations/channel-bindings",
        headers=headers,
    )
    assert bindings.status_code == 200
    assert [row["channel_config_id"] for row in bindings.json()] == [
        sibling_channel_config.id,
    ]


@pytest.mark.asyncio
async def test_disconnect_discord_leaves_guild_and_cleans_binding(
    client: AsyncClient,
    db_session,
    monkeypatch,
) -> None:
    import apps.api.routers.integrations as integrations_router

    headers = await _auth(client, "discord_disconnect")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    account = OAuthAccount(
        id=generate_ulid(),
        user_id=owner["id"],
        provider="discord",
        provider_user_id="discord:discord-app-id:guild:guild-delete",
        profile={
            "application_id": "discord-app-id",
            "guild_id": "guild-delete",
            "guild_name": "Delete QA",
        },
    )
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="discord",
        provider="discord_app",
        name="Delete QA",
        credential_source_kind="oauth_account",
        credential_source_id=account.id,
        discord_application_id="discord-app-id",
        discord_guild_id="guild-delete",
        config={
            "connection_kind": "oauth_account",
            "connection_id": account.id,
        },
        credentials={},
        status="active",
    )
    db_session.add_all([account, config])
    await db_session.flush()
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Delete QA",
        config={"channel_config_id": config.id},
        status="active",
    )
    db_session.add(binding)
    await db_session.commit()
    account_id = account.id
    config_id = config.id
    binding_id = binding.id

    leave_calls: list[tuple[str, str]] = []

    async def _resolve_app(_db):
        return SimpleNamespace(application_id="discord-app-id")

    async def _leave_guild(app, guild_id):
        leave_calls.append((app.application_id, guild_id))

    monkeypatch.setattr(
        integrations_router,
        "resolve_discord_app_config",
        _resolve_app,
    )
    monkeypatch.setattr(
        integrations_router,
        "leave_discord_guild",
        _leave_guild,
    )

    response = await client.delete(
        f"/api/v1/integrations/mcp-servers/discord/connections/{account.id}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    assert leave_calls == [("discord-app-id", "guild-delete")]
    db_session.expire_all()
    assert await db_session.get(OAuthAccount, account_id) is None
    assert await db_session.get(ChannelConfig, config_id) is None
    assert await db_session.get(Channel, binding_id) is None


@pytest.mark.asyncio
async def test_channel_bindings_hide_orphaned_oauth_channel_config(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "orphaned_oauth_channel")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    db_session.add(ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        name="Deleted Slack connection",
        credential_source_kind="oauth_account",
        credential_source_id=generate_ulid(),
        config={},
        credentials={},
        status="active",
    ))
    await db_session.commit()

    bindings = await client.get(
        "/api/v1/integrations/channel-bindings",
        headers=headers,
    )

    assert bindings.status_code == 200
    assert bindings.json() == []


@pytest.mark.asyncio
async def test_integration_isolation(client: AsyncClient):
    headers_a = await _auth(client, "int_a")
    headers_b = await _auth(client, "int_b")

    create = await client.post(
        "/api/v1/integrations",
        headers=headers_a,
        json={
            "provider": "slack",
        },
    )
    iid = create.json()["id"]

    # User B cannot see user A's integration
    resp = await client.get(f"/api/v1/integrations/{iid}", headers=headers_b)
    assert resp.status_code == 404

    # User B's list is empty
    resp2 = await client.get("/api/v1/integrations", headers=headers_b)
    assert len(resp2.json()) == 0


@pytest.mark.asyncio
async def test_create_channel(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/integrations/channels",
        headers=headers,
        json={
            "type": "slack_channel",
            "name": "#general",
            "config": {"channel_id": "C12345"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "slack_channel"
    assert data["name"] == "#general"
    assert data["status"] == "active"
    assert len(data["id"]) == 26


@pytest.mark.asyncio
async def test_channel_crud(client: AsyncClient):
    headers = await _auth(client)

    # Create
    create = await client.post(
        "/api/v1/integrations/channels",
        headers=headers,
        json={
            "type": "email",
            "name": "support@co.com",
        },
    )
    cid = create.json()["id"]

    # Read
    resp = await client.get(f"/api/v1/integrations/channels/{cid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "support@co.com"

    # Update
    resp = await client.put(
        f"/api/v1/integrations/channels/{cid}",
        headers=headers,
        json={
            "name": "help@co.com",
            "status": "disabled",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "help@co.com"
    assert resp.json()["status"] == "disabled"

    # Delete
    resp = await client.delete(f"/api/v1/integrations/channels/{cid}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/integrations/channels/{cid}", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_private_channel_crud_and_logs_are_limited_to_the_owner(
    client: AsyncClient,
    db_session,
):
    owner_headers = await _auth(client, "private_channel_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    member = await _create_entity_user(
        owner["entity_id"],
        "private_channel_member",
    )
    db_session.add(UserMembership(
        user_id=member["id"],
        entity_id=owner["entity_id"],
        role="member",
        status="active",
    ))
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        credential_source_kind="integration",
        credential_source_id="private_source",
        channel_type="telegram",
        provider="telegram_bot",
        config={},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.flush()
    log = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="telegram",
        from_address="private-contact",
        content="private message body",
        status="received",
    )
    db_session.add(log)
    await db_session.commit()

    create = await client.post(
        "/api/v1/integrations/channels",
        headers=owner_headers,
        json={
            "type": "telegram",
            "name": "Owner bot binding",
            "config": {"channel_config_id": config.id},
        },
    )
    assert create.status_code == 201, create.text
    channel_id = create.json()["id"]

    assert (await client.get(
        "/api/v1/integrations/channels", headers=member["headers"],
    )).json() == []
    assert (await client.get(
        f"/api/v1/integrations/channels/{channel_id}", headers=member["headers"],
    )).status_code == 404
    assert (await client.put(
        f"/api/v1/integrations/channels/{channel_id}",
        headers=member["headers"],
        json={"name": "stolen binding"},
    )).status_code == 404
    assert (await client.delete(
        f"/api/v1/integrations/channels/{channel_id}", headers=member["headers"],
    )).status_code == 404

    logs = await client.get("/api/v1/integrations/logs", headers=member["headers"])
    assert logs.status_code == 200
    assert logs.json() == []


@pytest.mark.asyncio
async def test_integration_channel_list_only_returns_its_own_bindings(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "integration_channel_scope")
    integration = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "email"},
    )
    assert integration.status_code == 201, integration.text
    integration_data = integration.json()
    config = (await db_session.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == integration_data["entity_id"],
            ChannelConfig.credential_source_id == integration_data["id"],
        )
    )).scalar_one()
    db_session.add_all([
        Channel(
            entity_id=integration_data["entity_id"],
            user_id=integration_data["owner_user_id"],
            type="email",
            name="Bound to this integration",
            config={"channel_config_id": config.id},
            status="active",
        ),
        Channel(
            entity_id=integration_data["entity_id"],
            user_id=integration_data["owner_user_id"],
            type="webchat",
            name="Unrelated binding",
            config={},
            status="active",
        ),
    ])
    await db_session.commit()

    response = await client.get(
        f"/api/v1/integrations/{integration_data['id']}/channels",
        headers=headers,
    )

    assert response.status_code == 200
    assert [channel["name"] for channel in response.json()] == [
        "Bound to this integration",
    ]


@pytest.mark.asyncio
async def test_credentials_not_exposed(client: AsyncClient, db_session):
    headers = await _auth(client)

    # Create integration with credentials
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
            "credentials": {"bot_token": "xoxb-super-secret"},
        },
    )
    assert create.status_code == 201
    data = create.json()
    assert "credentials" not in data
    assert data["credential_preview"].get("bot_token") in {None, "__unchanged__"}
    assert "xoxb-super-secret" not in str(data)

    # Get single integration — no credentials
    iid = data["id"]
    resp = await client.get(f"/api/v1/integrations/{iid}", headers=headers)
    data = resp.json()
    assert "credentials" not in data
    assert data["credential_preview"].get("bot_token") in {None, "__unchanged__"}
    assert "xoxb-super-secret" not in str(data)

    # List integrations — no credentials
    resp = await client.get("/api/v1/integrations", headers=headers)
    for item in resp.json():
        assert "credentials" not in item
        assert "xoxb-super-secret" not in str(item)

    # Updating with the sentinel must preserve existing encrypted creds.
    update = await client.put(
        f"/api/v1/integrations/{iid}",
        headers=headers,
        json={
            "credentials": {"bot_token": "__unchanged__", "default_channel": "C123"},
        },
    )
    assert update.status_code == 200
    integration = (await db_session.execute(select(Integration).where(Integration.id == iid))).scalar_one()
    plaintext = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="test", id="credentials_not_exposed"),
        reason="assert_update_preserved_secret",
    )
    assert plaintext["bot_token"] == "xoxb-super-secret"
    assert plaintext["default_channel"] == "C123"


@pytest.mark.asyncio
async def test_stale_integration_credential_update_is_rejected(
    client: AsyncClient,
    db_session,
):
    from packages.core.services.integration_service import (
        IntegrationCredentialConflictError,
        update_integration,
    )

    headers = await _auth(client, "integration_credential_cas")
    user = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
            "credentials": {"bot_token": "xoxb-original"},
        },
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["id"]

    await update_integration(
        db_session,
        integration_id,
        user["entity_id"],
        user["id"],
        credentials={"bot_token": "xoxb-newer"},
    )

    with pytest.raises(IntegrationCredentialConflictError):
        await update_integration(
            db_session,
            integration_id,
            user["entity_id"],
            user["id"],
            credentials={"bot_token": "xoxb-stale"},
            expected_credentials={"bot_token": "xoxb-original"},
        )

    integration = await db_session.get(Integration, integration_id)
    plaintext = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="test", id="integration_credential_cas"),
        reason="assert_stale_update_preserved_newer_credentials",
    )
    assert plaintext["bot_token"] == "xoxb-newer"
