"""Provider waits must leave the single database admission slot usable."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import literal, select, update

from packages.core import database
from packages.core.database_runtime import DatabaseEngineKind
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, ChannelContact, MessageLog
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.models.webhook import WebhookDelivery, WebhookEndpoint


@pytest_asyncio.fixture
async def one_slot_db(db_session):
    from conftest import TEST_DATABASE_URL

    settings = SimpleNamespace(
        DATABASE_URL=TEST_DATABASE_URL, DATABASE_ECHO=False,
        DATABASE_POOL_MODE="sqlalchemy", DATABASE_POOL_SIZE=2,
        DATABASE_MAX_OVERFLOW=0, DATABASE_POOL_TIMEOUT=1,
        DATABASE_POOL_RECYCLE=1800, DATABASE_ADMISSION_LIMIT=1,
        DATABASE_MAX_WAITERS=4, DATABASE_ADMISSION_TIMEOUT=0.1,
        DATABASE_HOLD_WARN_SECONDS=5,
    )
    engine = database.create_database_engine(
        settings, kind=DatabaseEngineKind.APPLICATION, is_test_database=True,
    )
    try:
        yield engine.sync_engine._manor_database_runtime, database.create_session_factory(engine)
    finally:
        await engine.dispose()


async def assert_database_available(runtime, factory):
    assert runtime.checked_out == 0
    async with factory() as other:
        assert await other.scalar(select(literal(1))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_send_email_releases_lookup_and_each_recipient_receipt(
    db_session, one_slot_db, monkeypatch, connected,
):
    from packages.core.ai.runtime.manor_actions import runtime_manor_send_email
    from packages.core.services import channel_service, email_service

    runtime, factory = one_slot_db
    entity_id = generate_ulid()
    if connected:
        db_session.add(ChannelConfig(entity_id=entity_id, channel_type="email", provider="smtp"))
        await db_session.commit()
    seen = []

    async def send_channel(config, **kwargs):
        await assert_database_available(runtime, factory)
        async with factory() as other:
            previous = list((await other.scalars(
                select(MessageLog).where(MessageLog.entity_id == entity_id)
            )).all())
            assert len(previous) == len(seen)
        seen.append(kwargs["to"])
        return {"external_id": str(len(seen))}

    async def send_platform(recipients, subject, body):
        await assert_database_available(runtime, factory)
        seen.extend(recipients)
        return {"sent": len(recipients), "failed": [], "total": len(recipients)}

    monkeypatch.setattr(channel_service, "_send_email", send_channel)
    monkeypatch.setattr(email_service, "send_bulk_email", send_platform)
    async with factory() as db:
        result = json.loads(await runtime_manor_send_email(
            db, entity_id=entity_id,
            params={"to": ["one@test.invalid", "two@test.invalid"], "subject": "Test", "body": "Body"},
        ))
    assert result["delivered"] == 2
    assert seen == ["one@test.invalid", "two@test.invalid"]
    assert runtime.checked_out == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_next", [False, True])
async def test_webhook_releases_between_endpoints_and_rechecks_changes(
    db_session, one_slot_db, monkeypatch, disable_next,
):
    from packages.core.services import webhook_service

    runtime, factory = one_slot_db
    entity_id = generate_ulid()
    endpoints = [WebhookEndpoint(entity_id=entity_id, url=f"https://test.invalid/{n}") for n in range(2)]
    db_session.add_all(endpoints)
    await db_session.commit()
    seen = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            await assert_database_available(runtime, factory)
            async with factory() as other:
                prior = list((await other.scalars(select(WebhookDelivery).where(
                    WebhookDelivery.endpoint_id.in_([endpoint.id for endpoint in endpoints]),
                ))).all())
                assert len(prior) == len(seen)
                if disable_next:
                    await other.execute(update(WebhookEndpoint).where(
                        WebhookEndpoint.entity_id == entity_id, WebhookEndpoint.url != url,
                    ).values(enabled=False))
                    await other.commit()
            seen.append(url)
            return SimpleNamespace(status_code=200 if len(seen) == 1 else 503, text="")

    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(webhook_service.httpx, "AsyncClient", Client)
    result = await webhook_service.deliver_event(entity_id, "task.failed", {})
    assert result == {"attempted": 1 if disable_next else 2, "delivered": 1, "failed": 0 if disable_next else 1}
    assert runtime.checked_out == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("kind", ["integration", "oauth"])
async def test_health_probe_releases_database_and_fences_stale_writeback(
    db_session, one_slot_db, monkeypatch, kind, changed,
):
    from packages.core import credentials
    from packages.core.services import integration_health, oauth_account_credentials

    runtime, factory = one_slot_db
    if kind == "integration":
        row = Integration(entity_id=generate_ulid(), provider="slack", config={"version": "old"})
        model, field = Integration, "config"
    else:
        row = OAuthAccount(user_id=generate_ulid(), provider="gmail", provider_user_id=generate_ulid(), profile={"version": "old"})
        model, field = OAuthAccount, "profile"
    db_session.add(row)
    await db_session.commit()

    def lease(*args, **kwargs):
        assert runtime.checked_out == 0
        return {"access_token": "synthetic-test-token"}

    async def probe(*args, **kwargs):
        await assert_database_available(runtime, factory)
        if changed:
            async with factory() as other:
                current = await other.get(model, row.id)
                setattr(current, field, {"version": "new"})
                await other.commit()
        return {"ok": True, "detail": "reachable", "latency_ms": 1, "checked_at": "2026-10-03T00:00:00+00:00"}

    monkeypatch.setattr(credentials, "get_credential_service", lambda: SimpleNamespace(lease_integration=lease))
    monkeypatch.setattr(oauth_account_credentials, "lease_oauth_account_tokens", lease)
    monkeypatch.setattr(integration_health, "run_test", probe)
    async with factory() as db:
        run = integration_health.run_and_persist_integration if kind == "integration" else integration_health.run_and_persist_oauth
        result = await run(db, row.id)
        await db.commit()
    async with factory() as db:
        current = await db.get(model, row.id)
        saved = getattr(current, field)
        if changed:
            assert result["reason_code"] == "connection_changed"
            assert saved == {"version": "new"}
        else:
            assert result["ok"] is True
            assert saved["last_health_check"]["ok"] is True


@pytest.mark.asyncio
async def test_channel_credential_lease_releases_lookup_before_vault(
    db_session, one_slot_db, monkeypatch,
):
    from packages.core.services import channel_credentials

    runtime, factory = one_slot_db
    row = ChannelConfig(entity_id=generate_ulid(), channel_type="email", provider="smtp")
    db_session.add(row)
    await db_session.commit()

    def lease(*args, **kwargs):
        assert runtime.checked_out == 0
        return {"username": "synthetic-user"}

    monkeypatch.setattr(channel_credentials, "async_session", factory)
    monkeypatch.setattr(channel_credentials, "get_credential_service", lambda: SimpleNamespace(lease_channel_config=lease))
    result = await channel_credentials.lease_channel_config_credentials(row, reason="test")
    assert result == {"username": "synthetic-user"}


@pytest.mark.asyncio
@pytest.mark.parametrize("actionable", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_notification_provider_wait_does_not_flush_queued_receipt(
    db_session, one_slot_db, monkeypatch, actionable, fails,
):
    from packages.core.services import channel_outbound_delivery as outbound

    runtime, factory = one_slot_db
    entity_id = generate_ulid()
    config = ChannelConfig(entity_id=entity_id, channel_type="telegram", provider="telegram")
    db_session.add(config)
    await db_session.flush()
    contact = ChannelContact(entity_id=entity_id, channel_config_id=config.id,
                             channel_type="telegram", source_id="synthetic-chat", user_id=generate_ulid(), status="active")
    db_session.add(contact)
    await db_session.commit()

    async def send(*args, **kwargs):
        await assert_database_available(runtime, factory)
        if fails:
            raise RuntimeError("synthetic provider failure")
        return {"status": "sent", "external_id": "accepted"}

    monkeypatch.setitem(outbound.ADAPTERS, "telegram", SimpleNamespace(send_text=send, send_actionable_message=send))
    async with factory() as db:
        if actionable:
            result = await outbound.send_actionable_outbound_to_contact(db, contact=contact, text="Test", actions=[])
        else:
            result = await outbound.send_outbound_to_contact(db, contact=contact, text="Test")
        await db.commit()
    assert result["sent"] is not fails
    async with factory() as db:
        receipt = await db.get(MessageLog, result["message_log_id"])
        assert receipt.status == ("failed" if fails else "sent")
        assert receipt.error_message == ("synthetic provider failure" if fails else None)


@pytest.mark.asyncio
async def test_task_notification_releases_recipient_lookup_before_checkpoint(one_slot_db, monkeypatch):
    from packages.core.services import email_service, task_event_notifications, task_external_notifications

    runtime, factory = one_slot_db
    completed = []

    async def recipients(db, *args):
        await db.scalar(select(literal(1)))
        return None, [SimpleNamespace(id="recipient", email="recipient@test.invalid")]

    async def send(*args):
        await assert_database_available(runtime, factory)
        return True

    async def checkpoint(key):
        await assert_database_available(runtime, factory)
        completed.append(key)

    monkeypatch.setattr(task_event_notifications, "task_event_recipient_users", recipients)
    monkeypatch.setattr(email_service, "send_notification_email", send)
    async with factory() as db:
        count = await task_external_notifications._deliver_email(
            db, "entity", "task.failed", {}, None, mark_sink_completed=checkpoint,
        )
    assert count == 1
    assert completed == ["task_external.email:recipient@test.invalid"]


@pytest.mark.asyncio
async def test_skill_storage_load_releases_database_and_runs_off_loop(one_slot_db, monkeypatch):
    import threading
    from unittest.mock import AsyncMock
    from packages.core.services import skill_file_storage, skill_service

    runtime, factory = one_slot_db
    owner_thread = threading.get_ident()
    storage_calls = []
    skill = SimpleNamespace(
        id="synthetic-skill", entity_id="entity", slug="synthetic", name="Synthetic",
        status="active", category="general", system_prompt="Stored prompt", tools=[],
        config={"execution_mode": "instructions_only"},
    )

    async def load(db, *args):
        await db.scalar(select(literal(1)))
        return skill

    def storage(*args, **kwargs):
        assert runtime.checked_out == 0
        assert threading.get_ident() != owner_thread
        storage_calls.append(True)
        return "Loaded prompt"

    def extra(*args):
        storage()
        return {}

    monkeypatch.setattr(skill_service, "get_skill", load)
    monkeypatch.setattr("packages.core.ai.runtime.skills.runtime_skill_is_eligible", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(skill_file_storage, "load_skill_prompt", storage)
    monkeypatch.setattr(skill_service, "_load_prompt_skill_extra_files", extra)
    async with factory() as db:
        result = await skill_service.invoke_skill(db, skill.id, "entity", "Load instructions")
    assert result["stop_reason"] == "instructions_loaded"
    assert result["content"] == "Loaded prompt"
    assert len(storage_calls) == 2


@pytest.mark.asyncio
async def test_site_snapshot_preparation_releases_lookup_transaction(one_slot_db, monkeypatch, tmp_path):
    from packages.core.ai.tools import site_tools
    from packages.core.config import get_settings
    from packages.core.services import site_publisher
    from test_site_tools import _create_tool_user

    runtime, factory = one_slot_db
    entity_id = generate_ulid()
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_SITES_DOMAIN", "sites.test.local")
    root = tmp_path / entity_id / "site"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<html>Test</html>")
    owner = await _create_tool_user(factory, entity_id, role="owner")
    original = site_publisher.prepare_publication
    snapshots = []

    def prepare(*args):
        assert runtime.checked_out == 0
        snapshots.append(True)
        return original(*args)

    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(site_publisher, "prepare_publication", prepare)
    result = json.loads(await site_tools._publish_site_handler(entity_id=entity_id, user_id=owner.id, path="site"))
    assert result["published"] is True
    assert snapshots == [True]


@pytest.mark.asyncio
async def test_health_sweep_commits_each_probe_before_next_account(db_session, one_slot_db, monkeypatch):
    from packages.core.services import integration_health
    from packages.core.tasks import channel_tasks

    runtime, factory = one_slot_db
    rows = [Integration(entity_id=generate_ulid(), provider="slack", config={}) for _ in range(2)]
    db_session.add_all(rows)
    await db_session.commit()
    seen = []

    async def probe(db, account_id):
        await assert_database_available(runtime, factory)
        await db.scalar(select(literal(1)))
        seen.append(account_id)
        return {"ok": True}

    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(integration_health, "run_and_persist_integration", probe)
    monkeypatch.setattr(integration_health, "run_and_persist_oauth", probe)
    monkeypatch.setattr(channel_tasks, "_run_async", lambda coroutine: coroutine)
    result = await channel_tasks.health_tick.run()
    assert {row.id for row in rows}.issubset(seen)
    assert result == {"checked": len(seen), "failed": 0}
    assert runtime.checked_out == 0


@pytest.mark.asyncio
async def test_announcement_persists_each_recipient_before_next_send(db_session, one_slot_db, monkeypatch):
    from packages.core.models.channel import Announcement, AnnouncementRecipient
    from packages.core.services import channel_service

    runtime, factory = one_slot_db
    entity_id = generate_ulid()
    config = ChannelConfig(entity_id=entity_id, channel_type="email", provider="smtp")
    db_session.add(config)
    await db_session.flush()
    announcement = Announcement(entity_id=entity_id, channel_type="email", channel_config_id=config.id,
                                title="Synthetic announcement", content="Test")
    db_session.add(announcement)
    await db_session.flush()
    db_session.add_all([
        AnnouncementRecipient(announcement_id=announcement.id, recipient_address=f"{n}@test.invalid")
        for n in range(2)
    ])
    await db_session.commit()
    seen = []

    async def send(config, **kwargs):
        await assert_database_available(runtime, factory)
        async with factory() as other:
            receipts = list((await other.scalars(select(AnnouncementRecipient).where(
                AnnouncementRecipient.announcement_id == announcement.id,
                AnnouncementRecipient.status == "sent",
            ))).all())
            assert len(receipts) == len(seen)
        seen.append(kwargs["to"])
        return {"external_id": str(len(seen))}

    monkeypatch.setattr(channel_service, "_send_email", send)
    async with factory() as db:
        result = await channel_service.send_announcement(db, announcement.id, entity_id)
        await db.commit()
    assert result["sent"] == 2
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_oauth_provider_configuration_releases_before_credential_lease(one_slot_db, monkeypatch):
    from packages.core import credentials
    from packages.core.models.mcp import MCPServer
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    runtime, factory = one_slot_db
    async with factory() as db:
        server = await db.scalar(select(MCPServer).where(MCPServer.server_key == "gmail"))
        server.default_config = {"oauth_client_id": "synthetic-client", "_oauth_source": "ui"}
        server.credential_ref = "synthetic-ref"
        await db.commit()

        def lease(*args, **kwargs):
            assert runtime.checked_out == 0
            return {"oauth_client_secret": "synthetic-secret"}

        monkeypatch.setattr(credentials, "get_credential_service", lambda: SimpleNamespace(lease_mcp_server=lease))
        config = await resolve_oauth_config(db, "gmail")
        assert config.client_id == "synthetic-client"
        assert config.client_secret == "synthetic-secret"
        await assert_database_available(runtime, factory)
