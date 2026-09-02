"""Email health check must exercise BOTH protocols in the bundle.

Prod incident (2026-08-04): an email integration whose SMTP config was
broken (implicit SSL on the STARTTLS port 587) and whose IMAP password
was rejected by Gmail sat "unchecked" for two weeks — the health check
only attempted IMAP LOGIN and merely assumed SMTP worked. The user
believed SMTP was fine because nothing ever reported otherwise.

These tests pin the contract of ``test_email_login``:
  - IMAP login and SMTP AUTH are both attempted when configured.
  - ``ok`` is true only if every configured protocol authenticates.
  - Send-only bundles (no imap_host — e.g. the SendGrid preset) test
    SMTP alone; read-only bundles test IMAP alone.
  - The classic ssl-on-587 misconfig produces an actionable hint.
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl

import pytest

from packages.core.services import integration_health as ih
from packages.core.services import smtp_transport


# Captured before the fixture monkeypatches imaplib.IMAP4 away.
IMAP_ERROR = imaplib.IMAP4.error

BASE_CREDS = {
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "username": "alice@example.com",
    "password": "app-password",
}


class _FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL / IMAP4."""

    fail_login: Exception | None = None

    def __init__(self, host, port, timeout=None):
        pass

    def login(self, username, password):
        if type(self).fail_login is not None:
            raise type(self).fail_login

    def logout(self):
        pass


class _FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL."""

    fail_connect: Exception | None = None
    fail_login: Exception | None = None
    instances_cls: list[str] = []

    def __init__(self, host, port, timeout=None):
        type(self).instances_cls.append(type(self).__name__)
        if type(self).fail_connect is not None:
            raise type(self).fail_connect

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, username, password):
        if type(self).fail_login is not None:
            raise type(self).fail_login

    def quit(self):
        pass


@pytest.fixture()
def fake_servers(monkeypatch):
    class FakeIMAPSSL(_FakeIMAP):
        pass

    class FakeIMAPPlain(_FakeIMAP):
        pass

    class FakeSMTPPlain(_FakeSMTP):
        instances_cls: list[str] = []

    class FakeSMTPSSL(_FakeSMTP):
        instances_cls: list[str] = []

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAPSSL)
    monkeypatch.setattr(imaplib, "IMAP4", FakeIMAPPlain)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTPPlain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return {
        "imap_ssl": FakeIMAPSSL,
        "imap": FakeIMAPPlain,
        "smtp": FakeSMTPPlain,
        "smtp_ssl": FakeSMTPSSL,
    }


@pytest.mark.asyncio
async def test_both_protocols_ok(fake_servers):
    result = await ih.test_email_login(dict(BASE_CREDS))
    assert result["ok"] is True
    assert "IMAP" in result["detail"] and "SMTP" in result["detail"]


@pytest.mark.asyncio
async def test_smtp_auth_failure_marks_not_ok(fake_servers):
    fake_servers["smtp"].fail_login = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Username and Password not accepted",
    )
    result = await ih.test_email_login(dict(BASE_CREDS))
    assert result["ok"] is False
    assert "SMTP" in result["detail"]
    assert "535" in result["detail"] or "not accepted" in result["detail"]
    # IMAP side still reported so the user sees which half works
    assert "IMAP" in result["detail"]


@pytest.mark.asyncio
async def test_imap_auth_failure_marks_not_ok(fake_servers):
    fake_servers["imap_ssl"].fail_login = IMAP_ERROR(
        b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
    )
    result = await ih.test_email_login(dict(BASE_CREDS))
    assert result["ok"] is False
    assert "IMAP" in result["detail"]


@pytest.mark.asyncio
async def test_ssl_on_starttls_port_gets_hint(fake_servers):
    creds = dict(BASE_CREDS, use_ssl_smtp=True, smtp_port=587)
    fake_servers["smtp_ssl"].fail_connect = ssl.SSLError(
        1, "[SSL: WRONG_VERSION_NUMBER] wrong version number",
    )
    result = await ih.test_email_login(creds)
    assert result["ok"] is False
    # Actionable hint: SSL-on-587 means the config wants STARTTLS
    assert "STARTTLS" in result["detail"]


@pytest.mark.asyncio
async def test_send_only_bundle_tests_smtp_alone(fake_servers):
    creds = dict(BASE_CREDS, imap_host="")
    result = await ih.test_email_login(creds)
    assert result["ok"] is True
    assert "SMTP" in result["detail"]


@pytest.mark.asyncio
async def test_read_only_bundle_tests_imap_alone(fake_servers):
    creds = dict(BASE_CREDS, smtp_host="")
    result = await ih.test_email_login(creds)
    assert result["ok"] is True
    assert "IMAP" in result["detail"]


@pytest.mark.asyncio
async def test_no_hosts_configured_fails(fake_servers):
    creds = dict(BASE_CREDS, imap_host="", smtp_host="")
    result = await ih.test_email_login(creds)
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_bare_username_on_gmail_gets_hint(fake_servers):
    """Gmail wants the full address; a bare local part reads as
    'Invalid credentials' and looks like a wrong password."""
    creds = dict(BASE_CREDS, imap_host="imap.gmail.com", username="alice")
    fake_servers["imap_ssl"].fail_login = IMAP_ERROR(
        b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
    )
    result = await ih.test_email_login(creds)
    assert result["ok"] is False
    assert "full email address" in result["detail"]


@pytest.mark.asyncio
async def test_auth_failure_mentions_app_password(fake_servers):
    """Gmail rejects normal account passwords outright — say so."""
    creds = dict(BASE_CREDS, imap_host="imap.gmail.com")
    fake_servers["imap_ssl"].fail_login = IMAP_ERROR(
        b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
    )
    result = await ih.test_email_login(creds)
    assert result["ok"] is False
    assert "app password" in result["detail"].lower()


@pytest.mark.asyncio
async def test_no_hint_when_auth_succeeds(fake_servers):
    creds = dict(BASE_CREDS, imap_host="imap.gmail.com", username="alice")
    result = await ih.test_email_login(creds)
    assert result["ok"] is True
    assert "full email address" not in result["detail"]


@pytest.mark.asyncio
async def test_non_auth_failure_gets_no_password_hint(fake_servers):
    """A network error is not a credentials problem — don't misdirect."""
    creds = dict(BASE_CREDS, imap_host="imap.gmail.com")
    fake_servers["imap_ssl"].fail_login = OSError("connection reset")
    result = await ih.test_email_login(creds)
    assert result["ok"] is False
    assert "app password" not in result["detail"].lower()


@pytest.mark.asyncio
async def test_registry_maps_email_to_combined_check():
    assert ih._TESTS["email"] is ih.test_email_login


@pytest.mark.asyncio
async def test_email_health_uses_shared_mail_transports(monkeypatch: pytest.MonkeyPatch):
    events: list[tuple[str, str, int, bool]] = []

    class FakeIMAP:
        def login(self, username, password):
            events.append(("imap-login", username, 0, False))

        def logout(self):
            pass

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, username, password):
            events.append(("smtp-login", username, 0, False))

    def open_imap(host: str, port: int, *, use_ssl: bool, timeout: float):
        events.append(("imap-open", host, port, use_ssl))
        return FakeIMAP()

    def open_smtp(host: str, port: int, *, use_ssl: bool, timeout: float):
        events.append(("smtp-open", host, port, use_ssl))
        return FakeSMTP()

    monkeypatch.setattr(smtp_transport, "open_imap_client", open_imap)
    monkeypatch.setattr(smtp_transport, "open_smtp_client", open_smtp)

    result = await ih.test_email_login(dict(BASE_CREDS))

    assert result["ok"] is True
    assert ("imap-open", "imap.example.com", 993, True) in events
    assert ("smtp-open", "smtp.example.com", 587, False) in events
