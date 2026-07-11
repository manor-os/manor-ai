"""Twilio channel regression tests."""

from __future__ import annotations

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
    assert all((c.credentials or {}).get("account_sid") == "AC123" for c in configs)
    assert all((c.credentials or {}).get("auth_token") == "secret-token" for c in configs)


@pytest.mark.asyncio
async def test_twilio_update_syncs_both_channel_config_credentials(client: AsyncClient, db_session):
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
    assert all((c.credentials or {}).get("auth_token") == "new-token" for c in configs)


@pytest.mark.asyncio
async def test_twilio_status_callback_updates_message_log(client: AsyncClient, db_session, monkeypatch):
    # The router imports `async_session` by symbol; in tests we need to
    # repoint it at the fixture-overridden session factory.
    import packages.core.database as db_module
    from apps.api.routers.channels import twilio as twilio_router

    monkeypatch.setattr(twilio_router, "async_session", db_module.async_session)

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
