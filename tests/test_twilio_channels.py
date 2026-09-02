"""Twilio channel regression tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog


async def _auth(client: AsyncClient, username: str = "twilio_user") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _allow_unsigned_local(monkeypatch) -> None:
    monkeypatch.setenv("MANOR_ENV", "local")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_LOCAL", "true")


async def _load_twilio_configs(db_session, *, entity_id: str, integration_id: str) -> list[ChannelConfig]:
    rows = (
        (
            await db_session.execute(
                select(ChannelConfig)
                .where(
                    ChannelConfig.entity_id == entity_id,
                    ChannelConfig.config["integration_id"].astext == integration_id,
                    ChannelConfig.channel_type.in_(("twilio_sms", "twilio_voice")),
                )
                .order_by(ChannelConfig.channel_type.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _create_twilio(
    client: AsyncClient,
    db_session,
    username: str = "twilio_webhook",
) -> tuple[dict, ChannelConfig]:
    headers = await _auth(client, username)
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {
                "account_sid": "AC-WEBHOOK",
                "auth_token": "webhook-token",
                "phone_number": "+14155550110",
            },
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    configs = await _load_twilio_configs(
        db_session,
        entity_id=body["entity_id"],
        integration_id=body["id"],
    )
    return body, next(c for c in configs if c.channel_type == "twilio_sms")


@pytest.mark.asyncio
async def test_twilio_integration_creates_sms_and_voice_channel_configs(client: AsyncClient, db_session):
    headers = await _auth(client, "twilio_create")
    resp = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {
                "account_sid": "AC123",
                "auth_token": "secret-token",
                "phone_number": "+14155550100",
            },
        },
    )
    assert resp.status_code == 201
    body = resp.json()

    configs = await _load_twilio_configs(
        db_session,
        entity_id=body["entity_id"],
        integration_id=body["id"],
    )
    assert {c.channel_type for c in configs} == {"twilio_sms", "twilio_voice"}
    assert all(c.status == "active" for c in configs)
    assert all(c.credential_source_kind == "integration" for c in configs)
    assert all(c.credential_source_id == body["id"] for c in configs)
    assert all(c.credentials == {} for c in configs)


@pytest.mark.asyncio
async def test_twilio_webhook_rejects_inactive_channel_config(client, db_session, monkeypatch):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    _body, config = await _create_twilio(client, db_session, "twilio_inactive_webhook")
    config.status = "inactive"
    await db_session.commit()
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    response = await client.post(
        "/api/v1/channels/twilio/sms",
        params={"config_id": config.id},
        data={"From": "+14155550199", "To": "+14155550110", "Body": "hello"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_twilio_sms_webhook_rejects_voice_channel_config(client, db_session, monkeypatch):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    _allow_unsigned_local(monkeypatch)
    body, _sms_cc = await _create_twilio(client, db_session, "twilio_sms_route_type")
    voice_cc = next(
        config
        for config in await _load_twilio_configs(
            db_session,
            entity_id=body["entity_id"],
            integration_id=body["id"],
        )
        if config.channel_type == "twilio_voice"
    )

    response = await client.post(
        "/api/v1/channels/twilio/sms",
        params={"config_id": voice_cc.id},
        data={
            "From": "+14155550199",
            "To": "+14155550110",
            "Body": "wrong route",
            "MessageSid": "SM-wrong-route",
        },
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_twilio_update_keeps_both_channel_configs_source_linked(client: AsyncClient, db_session):
    from packages.core.services.channel_credentials import lease_channel_credentials

    headers = await _auth(client, "twilio_update")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {
                "account_sid": "AC999",
                "auth_token": "old-token",
                "phone_number": "+14155550101",
            },
        },
    )
    assert create.status_code == 201
    body = create.json()

    update = await client.put(
        f"/api/v1/integrations/{body['id']}",
        headers=headers,
        json={
            "credentials": {
                "account_sid": "AC999",
                "auth_token": "new-token",
                "phone_number": "+14155550101",
            },
        },
    )
    assert update.status_code == 200

    configs = await _load_twilio_configs(
        db_session,
        entity_id=body["entity_id"],
        integration_id=body["id"],
    )
    assert len(configs) == 2
    assert all(c.credentials == {} for c in configs)
    assert all(c.credential_source_kind == "integration" for c in configs)
    leased = [
        await lease_channel_credentials(
            db_session,
            config,
            reason="test.twilio.updated_source",
        )
        for config in configs
    ]
    assert all(credentials["auth_token"] == "new-token" for credentials in leased)


@pytest.mark.asyncio
async def test_twilio_voice_fallback_stays_with_signed_owner_and_credential_source(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    body, sms_config = await _create_twilio(client, db_session, "twilio_voice_owner")
    legitimate_voice = next(
        config
        for config in await _load_twilio_configs(
            db_session,
            entity_id=body["entity_id"],
            integration_id=body["id"],
        )
        if config.channel_type == "twilio_voice"
    )
    attacker_voice = ChannelConfig(
        id=generate_ulid(),
        entity_id=body["entity_id"],
        owner_user_id=generate_ulid(),
        credential_source_kind="integration",
        credential_source_id=generate_ulid(),
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={"account_sid": "AC-ATTACKER", "auth_token": "attacker"},
        status="active",
    )
    db_session.add(attacker_voice)
    await db_session.commit()
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    resolved = await twilio_router._resolve_voice_channel_config(
        sms_config,
        to_number="+14155550110",
    )

    assert resolved.id == legitimate_voice.id
    assert resolved.id != attacker_voice.id


@pytest.mark.asyncio
async def test_twilio_voice_fallback_rejects_ambiguous_source_match(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from fastapi import HTTPException

    body, sms_config = await _create_twilio(client, db_session, "twilio_voice_ambiguous")
    duplicate_voice = ChannelConfig(
        id=generate_ulid(),
        entity_id=body["entity_id"],
        owner_user_id=sms_config.owner_user_id,
        credential_source_kind=sms_config.credential_source_kind,
        credential_source_id=sms_config.credential_source_id,
        channel_type="twilio_voice",
        provider="twilio",
        config={"phone_number": "+14155550110"},
        credentials={},
        status="active",
    )
    db_session.add(duplicate_voice)
    await db_session.commit()
    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

    with pytest.raises(HTTPException, match="Ambiguous Twilio voice") as exc_info:
        await twilio_router._resolve_voice_channel_config(
            sms_config,
            to_number="+14155550110",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_twilio_sms_adapter_leases_source_credentials_for_outbound_send(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.services.channels import get_adapter
    from packages.core.services.channels.twilio_adapter import TwilioAdapter
    import packages.core.database as db_module
    import packages.core.services.channel_credentials as channel_credentials

    monkeypatch.setattr(channel_credentials, "async_session", db_module.async_session)

    headers = await _auth(client, "twilio_outbound_source")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {
                "account_sid": "AC-OUTBOUND",
                "auth_token": "outbound-token",
                "phone_number": "+14155550103",
            },
        },
    )
    assert create.status_code == 201
    body = create.json()
    configs = await _load_twilio_configs(
        db_session,
        entity_id=body["entity_id"],
        integration_id=body["id"],
    )
    sms_cc = next(c for c in configs if c.channel_type == "twilio_sms")
    assert sms_cc.credentials == {}

    captured: dict[str, str] = {}

    async def fake_send_sms(self, to: str, message: str):
        captured.update(
            account_sid=self.account_sid,
            auth_token=self.auth_token,
            from_number=self.from_number,
            to=to,
            message=message,
        )
        return {"external_id": "SM-outbound", "status": "queued"}

    monkeypatch.setattr(TwilioAdapter, "send_sms", fake_send_sms)

    result = await get_adapter("twilio_sms").send_text(
        sms_cc,
        "+14155550199",
        "hello source lease",
    )

    assert result["external_id"] == "SM-outbound"
    assert captured == {
        "account_sid": "AC-OUTBOUND",
        "auth_token": "outbound-token",
        "from_number": "+14155550103",
        "to": "+14155550199",
        "message": "hello source lease",
    }


@pytest.mark.asyncio
async def test_twilio_status_callback_updates_message_log(client: AsyncClient, db_session, monkeypatch):
    # The router imports `async_session` by symbol; in tests we need to
    # repoint it at the fixture-overridden session factory.
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    _allow_unsigned_local(monkeypatch)

    headers = await _auth(client, "twilio_status")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {
                "account_sid": "AC321",
                "auth_token": "status-token",
                "phone_number": "+14155550102",
            },
        },
    )
    assert create.status_code == 201
    body = create.json()

    configs = await _load_twilio_configs(
        db_session,
        entity_id=body["entity_id"],
        integration_id=body["id"],
    )
    sms_cc = next(c for c in configs if c.channel_type == "twilio_sms")
    voice_cc = next(c for c in configs if c.channel_type == "twilio_voice")

    sms_log = MessageLog(
        id=generate_ulid(),
        entity_id=body["entity_id"],
        channel_config_id=sms_cc.id,
        direction="outbound",
        channel_type="twilio_sms",
        to_address="+14155550199",
        content="hi",
        external_id="SM123",
        status="queued",
    )
    voice_log = MessageLog(
        id=generate_ulid(),
        entity_id=body["entity_id"],
        channel_config_id=voice_cc.id,
        direction="outbound",
        channel_type="twilio_voice",
        to_address="+14155550198",
        content="call",
        external_id="CA123",
        status="queued",
    )
    db_session.add(sms_log)
    db_session.add(voice_log)
    await db_session.commit()

    sms_cb = await client.post(
        f"/api/v1/channels/twilio/status?config_id={sms_cc.id}",
        data={"MessageSid": "SM123", "MessageStatus": "delivered"},
    )
    assert sms_cb.status_code == 200

    voice_cb = await client.post(
        f"/api/v1/channels/twilio/status?config_id={voice_cc.id}",
        data={"CallSid": "CA123", "CallStatus": "completed", "CallDuration": "37"},
    )
    assert voice_cb.status_code == 200

    async with db_module.async_session() as verify_db:
        sms_row = (await verify_db.execute(select(MessageLog).where(MessageLog.id == sms_log.id))).scalar_one()
        voice_row = (await verify_db.execute(select(MessageLog).where(MessageLog.id == voice_log.id))).scalar_one()

    assert sms_row.status == "delivered"
    assert voice_row.status == "delivered"
    assert voice_row.duration_seconds == 37


@pytest.mark.asyncio
async def test_twilio_status_callback_returns_503_when_persistence_fails(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    _allow_unsigned_local(monkeypatch)

    headers = await _auth(client, "twilio_status_persistence_failure")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {
                "account_sid": "AC-PERSIST-FAIL",
                "auth_token": "status-token",
                "phone_number": "+14155550104",
            },
        },
    )
    assert create.status_code == 201
    body = create.json()
    sms_cc = next(
        c
        for c in await _load_twilio_configs(
            db_session,
            entity_id=body["entity_id"],
            integration_id=body["id"],
        )
        if c.channel_type == "twilio_sms"
    )
    db_session.add(
        MessageLog(
            id=generate_ulid(),
            entity_id=body["entity_id"],
            channel_config_id=sms_cc.id,
            direction="outbound",
            channel_type="twilio_sms",
            to_address="+14155550199",
            content="hi",
            external_id="SM-persist-fail",
            status="queued",
        )
    )
    await db_session.commit()

    class _FailingCommitContext:
        def __init__(self, factory):
            self._context = factory()
            self._session = None

        async def __aenter__(self):
            self._session = await self._context.__aenter__()
            return self

        async def __aexit__(self, *exc_info):
            return await self._context.__aexit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def commit(self):
            raise RuntimeError("database unavailable")

    calls = 0

    def _session_factory():
        nonlocal calls
        calls += 1
        if calls == 2:
            return _FailingCommitContext(db_module.async_session)
        return db_module.async_session()

    monkeypatch.setattr(twilio_router, "async_session", _session_factory)

    response = await client.post(
        f"/api/v1/channels/twilio/status?config_id={sms_cc.id}",
        data={"MessageSid": "SM-persist-fail", "MessageStatus": "delivered"},
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_twilio_webhook_fails_closed_without_or_with_invalid_signature(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import twilio as twilio_router
    import packages.core.database as db_module

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    monkeypatch.setenv("MANOR_ENV", "staging")
    _, sms_cc = await _create_twilio(client, db_session, "twilio_signature_gate")
    path = f"/api/v1/channels/twilio/sms?config_id={sms_cc.id}"
    form = {
        "From": "+14155550199",
        "To": "+14155550110",
        "Body": "signed?",
        "MessageSid": "SM-signature-gate",
    }

    missing = await client.post(path, data=form)
    invalid = await client.post(
        path,
        data=form,
        headers={"X-Twilio-Signature": "invalid"},
    )

    assert missing.status_code == 403
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_twilio_webhook_defaults_to_requiring_a_signature(monkeypatch):
    from fastapi import HTTPException

    from apps.api.routers.channels import twilio as twilio_router

    monkeypatch.delenv("MANOR_ENV", raising=False)
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_LOCAL", raising=False)
    request = type("UnsignedRequest", (), {"headers": {}})()

    with pytest.raises(HTTPException, match="Twilio signature is required"):
        await twilio_router._validate_twilio_signature(
            object(),
            request,
            {},
        )


@pytest.mark.asyncio
async def test_twilio_sms_deduplicates_message_sid_and_does_not_reenqueue(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import twilio as twilio_router
    import packages.core.database as db_module

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    _allow_unsigned_local(monkeypatch)
    _, sms_cc = await _create_twilio(client, db_session, "twilio_dedup")
    dispatched: list[dict] = []
    monkeypatch.setattr(
        twilio_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )
    path = f"/api/v1/channels/twilio/sms?config_id={sms_cc.id}"
    form = {
        "From": "+14155550199",
        "To": "+14155550110",
        "Body": "once",
        "MessageSid": "SM-dedup-1",
    }

    first = await client.post(path, data=form)
    second = await client.post(path, data=form)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(dispatched) == 1
    rows = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == sms_cc.id,
                MessageLog.direction == "inbound",
                MessageLog.external_id == "SM-dedup-1",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_twilio_sms_retries_when_receipt_claim_was_not_published(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    """Do not ACK the database-before-broker crash window as a duplicate."""
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.services.channel_inbound_receipts import claim_inbound_dispatch

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    _allow_unsigned_local(monkeypatch)
    owner, sms_cc = await _create_twilio(client, db_session, "twilio_pending_publish")
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        channel_config_id=sms_cc.id,
        direction="inbound",
        channel_type="twilio_sms",
        external_id="SM-publish-pending",
        status="received",
        content="retry the provider",
    )
    db_session.add(receipt)
    await db_session.commit()
    claim = await claim_inbound_dispatch(
        config_id=sms_cc.id,
        channel_type="twilio_sms",
        external_id=receipt.external_id,
    )
    assert claim.acquired

    monkeypatch.setattr(
        twilio_router.dispatch_inbound_task,
        "delay",
        lambda **_kwargs: pytest.fail("an unexpired claim must not be republished"),
    )
    response = await client.post(
        f"/api/v1/channels/twilio/sms?config_id={sms_cc.id}",
        data={
            "From": "+14155550199",
            "To": "+14155550110",
            "Body": "retry the provider",
            "MessageSid": receipt.external_id,
        },
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_twilio_unpublished_receipt_claim_becomes_reclaimable(
    client: AsyncClient,
    db_session,
):
    import packages.core.database as db_module
    from packages.core.services.channel_inbound_receipts import (
        InboundDispatchClaimOutcome,
        claim_inbound_dispatch,
    )

    owner, sms_cc = await _create_twilio(client, db_session, "twilio_stale_publish")
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        channel_config_id=sms_cc.id,
        direction="inbound",
        channel_type="twilio_sms",
        external_id="SM-stale-publish",
        status="received",
        content="recover the handoff",
    )
    db_session.add(receipt)
    await db_session.commit()
    started_at = datetime.now(timezone.utc)

    first = await claim_inbound_dispatch(
        config_id=sms_cc.id,
        channel_type="twilio_sms",
        external_id=receipt.external_id,
        now=started_at,
        session_factory=db_module.async_session,
    )
    pending = await claim_inbound_dispatch(
        config_id=sms_cc.id,
        channel_type="twilio_sms",
        external_id=receipt.external_id,
        now=started_at + timedelta(minutes=14),
        session_factory=db_module.async_session,
    )
    reclaimed = await claim_inbound_dispatch(
        config_id=sms_cc.id,
        channel_type="twilio_sms",
        external_id=receipt.external_id,
        now=started_at + timedelta(minutes=16),
        session_factory=db_module.async_session,
    )

    assert first.acquired is True
    assert pending.outcome is InboundDispatchClaimOutcome.PUBLISH_PENDING
    assert reclaimed.acquired is True
    assert reclaimed.claim_id != first.claim_id


@pytest.mark.asyncio
async def test_twilio_sms_concurrent_duplicate_sid_is_published_once(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router
    from packages.core.services.channel_service import handle_inbound_message as original_handle_inbound_message

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    _allow_unsigned_local(monkeypatch)
    _, sms_cc = await _create_twilio(client, db_session, "twilio_concurrent_dedup")
    dispatched: list[dict] = []
    monkeypatch.setattr(
        twilio_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )

    entered = 0
    both_entered = asyncio.Event()

    async def racing_handle(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=5)
        return await original_handle_inbound_message(*args, **kwargs)

    monkeypatch.setattr(twilio_router, "handle_inbound_message", racing_handle)
    path = f"/api/v1/channels/twilio/sms?config_id={sms_cc.id}"
    form = {
        "From": "+14155550199",
        "To": "+14155550110",
        "Body": "once concurrently",
        "MessageSid": "SM-concurrent-dedup-1",
    }

    responses = await asyncio.gather(client.post(path, data=form), client.post(path, data=form))

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) >= 1
    assert set(statuses) <= {200, 503}
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_twilio_receipt_claim_is_single_use(client: AsyncClient, db_session, monkeypatch):
    """The webhook must claim a received SID before publishing to Celery."""
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="twilio-claim-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="twilio_sms",
        external_id="SM-claim-once",
        status="received",
        content="claim me",
    )
    db_session.add(receipt)
    await db_session.commit()

    first, first_claimed = await twilio_router._claim_received(
        receipt.channel_config_id,
        receipt.external_id,
    )
    second, second_claimed = await twilio_router._claim_received(
        receipt.channel_config_id,
        receipt.external_id,
    )

    assert first is not None and first_claimed is True
    assert second is not None and second_claimed is False
    assert second.status == "queued"


@pytest.mark.asyncio
async def test_twilio_sms_returns_503_when_broker_enqueue_fails(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import twilio as twilio_router
    import packages.core.database as db_module

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)
    _allow_unsigned_local(monkeypatch)
    _, sms_cc = await _create_twilio(client, db_session, "twilio_queue_failure")

    def fail_enqueue(**_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(twilio_router.dispatch_inbound_task, "delay", fail_enqueue)
    response = await client.post(
        f"/api/v1/channels/twilio/sms?config_id={sms_cc.id}",
        data={
            "From": "+14155550199",
            "To": "+14155550110",
            "Body": "retry me",
            "MessageSid": "SM-queue-failure",
        },
    )

    assert response.status_code == 503


def test_twilio_mcp_surface_matches_release_contract():
    from packages.core.ai.mcp import twilio

    assert {tool["name"] for tool in twilio.list_tools()} == {
        "list_phone_numbers",
        "send_sms",
        "make_call",
        "get_usage",
    }


@pytest.mark.asyncio
async def test_twilio_mcp_send_sms_uses_json_blob_credentials(monkeypatch):
    from packages.core.ai.mcp import twilio
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    async def fake_send_sms(self, to: str, body: str):
        assert self.account_sid == "AC-MCP"
        assert self.auth_token == "mcp-token"
        assert self.from_number == "+14155550111"
        return {"external_id": "SM-mcp", "status": "queued", "to": to, "body": body}

    monkeypatch.setattr(TwilioAdapter, "send_sms", fake_send_sms)
    result = await twilio.call_tool(
        "send_sms",
        {"to": "+14155550199", "body": "hello from mcp"},
        '{"account_sid":"AC-MCP","auth_token":"mcp-token","phone_number":"+14155550111"}',
    )

    assert result["isError"] is False
    assert "SM-mcp" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"to": "   ", "body": "hello"},
        {"to": "+14155550199", "body": "   "},
    ],
)
async def test_twilio_mcp_rejects_whitespace_required_arguments_without_provider_call(
    monkeypatch, arguments
):
    from packages.core.ai.mcp import twilio
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    async def unexpected_send_sms(*_args, **_kwargs):
        raise AssertionError("Twilio provider must not be called")

    monkeypatch.setattr(TwilioAdapter, "send_sms", unexpected_send_sms)
    result = await twilio.call_tool(
        "send_sms",
        arguments,
        '{"account_sid":"AC-MCP","auth_token":"mcp-token","phone_number":"+14155550111"}',
    )

    assert result["isError"] is True
    assert "to" in result["content"][0]["text"] or "body" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_twilio_mcp_rejects_non_object_arguments_before_provider_call(monkeypatch):
    from packages.core.ai.mcp import twilio
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    async def unexpected_send_sms(*_args, **_kwargs):
        raise AssertionError("Twilio provider must not be called")

    monkeypatch.setattr(TwilioAdapter, "send_sms", unexpected_send_sms)
    result = await twilio.call_tool(
        "send_sms",
        [],
        '{"account_sid":"AC-MCP","auth_token":"mcp-token","phone_number":"+14155550111"}',
    )

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("send_sms", ("   ", "hello")),
        ("send_sms", ("+14155550199", "   ")),
        ("make_call", ("   ", "https://manor.example/twiml")),
        ("make_call", ("+14155550199", "   ")),
    ],
)
async def test_twilio_adapter_rejects_blank_outbound_values_before_http(
    monkeypatch, method, args
):
    from packages.core.services.channels import twilio_adapter
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("Twilio provider must not be called")

    monkeypatch.setattr(twilio_adapter, "httpx", type("Httpx", (), {"AsyncClient": UnexpectedClient}))
    adapter = TwilioAdapter("AC-MCP", "mcp-token", "+14155550111")

    with pytest.raises(ValueError, match="must not be blank"):
        await getattr(adapter, method)(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args", "provider_status", "external_id"),
    [
        ("send_sms", ("+14155550199", "hello"), "accepted", "SM-accepted"),
        (
            "make_call",
            ("+14155550199", "https://manor.example/twiml"),
            "ringing",
            "CA-ringing",
        ),
    ],
)
async def test_twilio_success_normalizes_provider_status_with_result_factory(
    monkeypatch,
    method,
    args,
    provider_status,
    external_id,
):
    from packages.core.services.channels import twilio_adapter
    from packages.core.services.channels.twilio_adapter import TwilioAdapter

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {
                "sid": external_id,
                "status": provider_status,
                "from": "+14155550111",
                "to": "+14155550199",
            }

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        twilio_adapter,
        "httpx",
        type("Httpx", (), {"AsyncClient": Client}),
    )
    adapter = TwilioAdapter("AC-MCP", "mcp-token", "+14155550111")

    result = await getattr(adapter, method)(*args)

    assert result["status"] == "queued"
    assert result["provider_status"] == provider_status
    assert result["external_id"] == external_id
