"""Outbound email over SMTP must pick the right TLS mode for the port.

Prod incident (2026-08-04): every outbound email for an entity failed
with ``[SSL: WRONG_VERSION_NUMBER]`` on smtp.gmail.com:587. Cause:
``_send_email`` passed ``use_tls=True`` to ``aiosmtplib.send``, which
means *implicit* TLS from the first byte (port 465 semantics). Port 587
speaks plaintext until STARTTLS, so the handshake never completed —
17 sends failed over two weeks and no send ever succeeded.

Second defect found alongside it: the Integration→ChannelConfig bridge
writes ``smtp_host``/``smtp_port`` into ``credentials``, but this sender
only read them from ``config``, silently falling back to the *platform*
env SMTP_HOST. An entity's configured mail server was ignored.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.services.channel_service as cs


def _config(*, config: dict | None = None, credentials: dict | None = None):
    return SimpleNamespace(
        id="cc_1",
        channel_type="email",
        config=config if config is not None else {},
        credentials=credentials if credentials is not None else {},
    )


@pytest.fixture()
def fake_smtp(monkeypatch):
    mock_send = AsyncMock()
    monkeypatch.setattr(cs, "aiosmtplib", SimpleNamespace(send=mock_send))

    async def lease_credentials(config, *, reason: str):
        assert reason == "channel_service.email.send"
        return config.credentials

    monkeypatch.setattr(cs, "lease_channel_config_credentials", lease_credentials)
    return mock_send


async def _send(cc):
    return await cs._send_email(
        cc, to="bob@example.com", subject="hi", content="body",
        html_content=None, attachments=None,
    )


@pytest.mark.asyncio
async def test_port_587_uses_starttls_not_implicit_tls(fake_smtp):
    cc = _config(credentials={
        "smtp_host": "smtp.gmail.com", "smtp_port": 587,
        "username": "alice@example.com", "password": "pw",
    })
    result = await _send(cc)

    assert not result.get("error")
    kwargs = fake_smtp.call_args.kwargs
    assert kwargs["port"] == 587
    assert kwargs.get("start_tls") is True
    assert kwargs.get("use_tls") is False


@pytest.mark.asyncio
async def test_port_465_uses_implicit_tls(fake_smtp):
    cc = _config(credentials={
        "smtp_host": "smtp.example.com", "smtp_port": 465,
        "username": "alice@example.com", "password": "pw",
    })
    await _send(cc)

    kwargs = fake_smtp.call_args.kwargs
    assert kwargs["port"] == 465
    assert kwargs.get("use_tls") is True
    assert kwargs.get("start_tls") is not True


@pytest.mark.asyncio
async def test_host_and_port_read_from_credentials_bundle(fake_smtp):
    """The Integration bridge stores the bundle in ``credentials``."""
    cc = _config(credentials={
        "smtp_host": "smtp.fastmail.com", "smtp_port": 587,
        "username": "alice@example.com", "password": "pw",
    })
    await _send(cc)

    assert fake_smtp.call_args.kwargs["hostname"] == "smtp.fastmail.com"


@pytest.mark.asyncio
async def test_config_still_wins_for_legacy_rows(fake_smtp):
    """Older ChannelConfig rows put the host in ``config`` — keep them working."""
    cc = _config(
        config={"smtp_host": "legacy.example.com", "smtp_port": 465},
        credentials={"username": "alice@example.com", "password": "pw"},
    )
    await _send(cc)

    kwargs = fake_smtp.call_args.kwargs
    assert kwargs["hostname"] == "legacy.example.com"
    assert kwargs["port"] == 465


@pytest.mark.asyncio
async def test_explicit_ssl_flag_overrides_port_default(fake_smtp):
    cc = _config(credentials={
        "smtp_host": "smtp.example.com", "smtp_port": 2525,
        "username": "alice@example.com", "password": "pw",
        "use_ssl_smtp": True,
    })
    await _send(cc)

    kwargs = fake_smtp.call_args.kwargs
    assert kwargs.get("use_tls") is True
    assert kwargs.get("start_tls") is not True


@pytest.mark.asyncio
async def test_from_address_falls_back_to_username(fake_smtp):
    cc = _config(credentials={
        "smtp_host": "smtp.example.com", "smtp_port": 587,
        "username": "alice@example.com", "password": "pw",
    })
    result = await _send(cc)

    assert result.get("from_address") == "alice@example.com"
