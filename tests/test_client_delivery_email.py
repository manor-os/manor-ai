"""Transactional customer invitations: SMTP receipt is separate from acceptance."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.services import email_service, smtp_transport
from test_client_workspace_delivery import prepared
from test_crm_tools import call


@pytest.fixture(autouse=True)
def no_real_email(monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    monkeypatch.setenv("APP_URL", "https://manor.example.test")
    monkeypatch.setattr(smtp_transport, "send_message_async", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("locale, cta, mode, expiry", [
    ("en", "Receive Workspace", "Customer testing", "7 days"),
    ("zh-CN", "接收工作空间", "客户测试", "7 天"),
    ("es", "Recibir espacio de trabajo", "Prueba del cliente", "7 días"),
    ("fr_CA", "Recevoir l’espace de travail", "Test client", "7 jours"),
    ("de", "Arbeitsbereich empfangen", "Kundentest", "7 Tagen"),
    ("unsupported", "Receive Workspace", "Customer testing", "7 days"),
])
async def test_client_invitation_uses_shared_smtp_and_escaped_reception_link(monkeypatch, locale, cta, mode, expiry):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    sent = await email_service.send_client_delivery_email(
        "recipient@acme.test", entity_name="Agency <script>", workspace_name="OLSO & Marketing",
        phase="trial", invite_url="https://manor.example.test/client-delivery/secret-token", locale=locale,
    )
    assert sent
    msg = smtp_transport.send_message_async.call_args.kwargs["message"]
    assert msg["To"] == "recipient@acme.test"
    body = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    from email.header import decode_header, make_header
    assert cta in str(make_header(decode_header(msg["Subject"]))) and cta in body
    resolved = email_service.normalize_announcement_email_locale(locale)
    assert f'<html lang="{resolved}">' in body
    assert "{{" not in body
    assert mode in body and expiry in body
    if resolved == "en":
        assert "接收工作空间" not in body
        assert "your own company" in body and "starts paused" in body
    else:
        assert "Sign in or register" not in body
        assert "View in a web browser" not in body
        assert "All rights reserved" not in body
    if resolved == "zh":
        assert "工作空间所有者" in body and "默认暂停" in body
    assert "Agency &lt;script&gt;" in body and "<script>" not in body
    assert "OLSO &amp; Marketing" in body
    assert "https://manor.example.test/client-delivery/secret-token" in body
    assert "join the team" not in body


@pytest.mark.asyncio
async def test_disabled_and_blocked_mail_never_claims_sent(monkeypatch):
    kwargs = dict(entity_name="Agency", workspace_name="Workspace", phase="delivered", invite_url="https://manor.example.test/client-delivery/token")
    assert not await email_service.send_client_delivery_email("customer@acme.test", **kwargs)
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert not await email_service.send_client_delivery_email("fixture@acme.invalid", **kwargs)
    smtp_transport.send_message_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_saves_before_email_and_rotation_resends_same_receipt(client, db_session, monkeypatch):
    captured = []
    async def send(to, **kwargs):
        # A separate connection sees the invitation before external I/O.
        async with AsyncSession(bind=db_session.bind) as check:
            assert await check.scalar(select(func.count()).select_from(ClientWorkspaceDelivery)) == 1
        captured.append((to, kwargs))
        return len(captured) > 1
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    monkeypatch.setattr(email_service, "send_client_delivery_email", send)
    ph, _p, rh, recipient, cid, _source, receipt, token = await prepared(client, db_session)
    assert receipt["email_delivery"]["status"] == "failed"
    assert captured[0][0] == recipient.email
    assert captured[0][1]["invite_url"].endswith(token)
    assert captured[0][1]["locale"] == receipt["email_delivery"]["locale"] == "en"
    assert (await client.get(f"/api/v1/client-deliveries/invites/{token}", headers=rh)).status_code == 200
    rows = (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()
    assert rows[0]["email_delivery"]["status"] == "failed"
    recipient.locale = "zh-CN"
    await db_session.commit()
    retry = await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/rotate", headers=ph)
    assert retry.status_code == 200, retry.text
    updated = retry.json()
    assert updated["id"] == receipt["id"] and updated["email_delivery"]["status"] == "sent"
    assert updated["email_delivery"]["sent_at"]
    assert captured[1][1]["locale"] == updated["email_delivery"]["locale"] == "zh"
    new_token = updated["invite_path"].rsplit("/", 1)[-1]
    assert new_token != token and captured[1][1]["invite_url"].endswith(new_token)
    assert (await client.get(f"/api/v1/client-deliveries/invites/{token}", headers=rh)).status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(ClientWorkspaceDelivery)) == 1
    # Historical contact preferences cannot override the customer's account.
    from packages.core.models.client_delivery import ClientContact
    contact_id = receipt["contact_id"]
    contact = await db_session.get(ClientContact, contact_id)
    contact.locale = "de"
    recipient.locale = "fr-CA"
    recipient.verified_email = None
    await db_session.commit()
    # Language is an account preference; verification still gates acceptance.
    account = await call(db_session, _p, "rotate_client_delivery", client_id=cid, delivery_id=receipt["id"])
    assert account["email_delivery"]["locale"] == captured[2][1]["locale"] == "fr"
    account_token = account["invite_path"].rsplit("/", 1)[-1]
    assert (await client.post(f"/api/v1/client-deliveries/invites/{account_token}/accept", headers=rh, json={})).status_code == 403
    patched = await call(db_session, _p, "update_client_contact", client_id=cid, contact_id=contact_id, phone="+123")
    assert "locale" not in patched
    forbidden = await call(db_session, _p, "update_client_contact", client_id=cid, contact_id=contact_id, locale="zh")
    assert not forbidden["ok"]
    data = {k: patched[k] for k in ("name", "email", "phone", "job_title", "is_primary")}
    data["job_title"] = data["job_title"] or ""
    assert (await client.put(f"/api/v1/clients/{cid}/contacts/{contact_id}", headers=ph, json={**data, "locale":"zh"})).status_code == 422
    recipient.status = "inactive"
    await db_session.commit()
    fallback = await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/rotate", headers=ph)
    assert fallback.json()["email_delivery"]["locale"] == captured[3][1]["locale"] == "en"
    # Reads, revoke, and failed repeated rotate cannot send another email.
    await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)
    await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/revoke", headers=ph)
    assert (await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/rotate", headers=ph)).status_code == 409
    assert len(captured) == 4


@pytest.mark.asyncio
async def test_tool_prepare_emails_with_same_authority_and_reports_disabled(client, db_session, monkeypatch):
    ph, provider, _rh, recipient, cid, source, receipt, _token = await prepared(client, db_session)
    assert receipt["email_delivery"]["status"] == "disabled"
    recipient.locale = "es-MX"
    await db_session.commit()
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    monkeypatch.setattr(email_service, "send_client_delivery_email", sender)
    created = await call(db_session, provider, "prepare_client_delivery", client_id=cid, contact_id=receipt["contact_id"], workspace_id=source)
    assert created["ok"] and created["email_delivery"]["status"] == "sent"
    sender.assert_awaited_once()
    assert sender.call_args.kwargs["locale"] == created["email_delivery"]["locale"] == "es"
    assert (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()[0]["email_delivery"]["status"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "bad_url"])
async def test_failed_configuration_or_sender_retains_invitation_without_secrets(client, db_session, monkeypatch, failure):
    sender = AsyncMock(side_effect=RuntimeError("private SMTP_PASSWORD=secret"))
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    monkeypatch.setattr(email_service, "send_client_delivery_email", sender)
    if failure == "bad_url":
        monkeypatch.setenv("APP_URL", "https://user:secret@host.test?redirect=elsewhere")
    _ph, _p, _rh, _r, _cid, _source, receipt, _token = await prepared(client, db_session)
    assert receipt["email_delivery"]["status"] == ("unconfigured" if failure == "bad_url" else "failed")
    assert "secret" not in str(receipt["email_delivery"])
    if failure == "bad_url":
        sender.assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(ClientWorkspaceDelivery)) == 1


@pytest.mark.asyncio
async def test_email_receipt_migration_preserves_existing_delivery(db_session):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / "packages/core/migrations/versions/20261002_client_email.py"
    spec = importlib.util.spec_from_file_location("delivery_email_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    def migrate(conn):
        conn.execute(text("CREATE SCHEMA delivery_email_migration_test"))
        conn.execute(text("SET LOCAL search_path TO delivery_email_migration_test"))
        conn.execute(text("CREATE TABLE client_workspace_deliveries (id text PRIMARY KEY)"))
        conn.execute(text("INSERT INTO client_workspace_deliveries VALUES ('existing')"))
        previous = mod.op
        mod.op = Operations(MigrationContext.configure(conn))
        try:
            mod.upgrade()
            assert conn.execute(text("SELECT email_delivery FROM client_workspace_deliveries WHERE id='existing'")).scalar_one() == {}
            mod.downgrade()
            assert conn.execute(text("SELECT id FROM client_workspace_deliveries")).scalar_one() == "existing"
        finally:
            mod.op = previous
            conn.execute(text("SET LOCAL search_path TO public"))
            conn.execute(text("DROP SCHEMA delivery_email_migration_test CASCADE"))
    await (await db_session.connection()).run_sync(migrate)


@pytest.mark.asyncio
async def test_contact_language_migration_preserves_unset_preferences(db_session):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / "packages/core/migrations/versions/20261002_03_client_contact_locale.py"
    spec = importlib.util.spec_from_file_location("contact_locale_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    def migrate(conn):
        conn.execute(text("CREATE SCHEMA contact_locale_migration_test"))
        conn.execute(text("SET LOCAL search_path TO contact_locale_migration_test"))
        conn.execute(text("CREATE TABLE client_contacts (id text PRIMARY KEY, email text)"))
        conn.execute(text("INSERT INTO client_contacts VALUES ('existing', 'recipient@acme.test')"))
        previous = mod.op
        mod.op = Operations(MigrationContext.configure(conn))
        try:
            mod.upgrade()
            assert conn.execute(text("SELECT email, locale FROM client_contacts")).one() == ('recipient@acme.test', None)
            mod.downgrade()
            assert conn.execute(text("SELECT email FROM client_contacts")).scalar_one() == 'recipient@acme.test'
        finally:
            mod.op = previous
            conn.execute(text("SET LOCAL search_path TO public"))
            conn.execute(text("DROP SCHEMA contact_locale_migration_test CASCADE"))
    await (await db_session.connection()).run_sync(migrate)


def test_common_template_keeps_existing_english_defaults():
    rendered = email_service._render("common", content="Existing message")
    assert '<html lang="en">' in rendered and "View in a web browser" in rendered
    assert "All rights reserved" in rendered and "{{" not in rendered
