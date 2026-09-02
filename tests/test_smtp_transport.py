"""Focused tests for the opt-in SMTP SOCKS egress transport."""

from __future__ import annotations

from types import SimpleNamespace
import sys
from pathlib import Path
import smtplib
import tomllib

import pytest


def test_socks_egress_runtime_dependency_is_declared() -> None:
    root = Path(__file__).resolve().parents[1]
    dependencies = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]

    assert any(dependency.lower().startswith("pysocks") for dependency in dependencies)


def test_direct_mode_does_not_configure_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_EGRESS_MODE", "direct")

    from packages.core.services import smtp_transport

    assert smtp_transport.proxy_settings() is None


def test_socks_mode_reads_the_internal_proxy_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_EGRESS_MODE", "socks")
    monkeypatch.setenv("SMTP_SOCKS_HOST", "smtp-egress-proxy")
    monkeypatch.setenv("SMTP_SOCKS_PORT", "1080")

    from packages.core.services import smtp_transport

    assert smtp_transport.proxy_settings() == ("smtp-egress-proxy", 1080)


def test_socks_mode_resolves_smtp_host_before_connecting_through_internal_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMTP_EGRESS_MODE", "socks")
    monkeypatch.setenv("SMTP_SOCKS_HOST", "smtp-egress-proxy")

    class FakeSocket:
        proxy: tuple[object, str, int, bool] | None = None
        timeout: float | None = None
        destination: tuple[str, int] | None = None

        def set_proxy(self, proxy_type: object, host: str, port: int, *, rdns: bool) -> None:
            type(self).proxy = (proxy_type, host, port, rdns)

        def settimeout(self, timeout: float) -> None:
            type(self).timeout = timeout

        def connect(self, destination: tuple[str, int]) -> None:
            type(self).destination = destination

    fake_socket = FakeSocket()
    fake_socks = SimpleNamespace(SOCKS5="socks5", socksocket=lambda: fake_socket)
    monkeypatch.setitem(sys.modules, "socks", fake_socks)

    from packages.core.services import smtp_transport

    assert smtp_transport.open_tcp_socket("smtp.example", 587, timeout=10) is fake_socket
    assert FakeSocket.proxy == ("socks5", "smtp-egress-proxy", 1080, False)
    assert FakeSocket.timeout == 10
    assert FakeSocket.destination == ("smtp.example", 587)


def test_socks_imap_ssl_socket_uses_the_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMTP_EGRESS_MODE", "socks")
    monkeypatch.setenv("SMTP_SOCKS_HOST", "smtp-egress-proxy")

    from packages.core.services import smtp_transport

    socket = object()
    opened: list[tuple[str, int, float]] = []
    wrapped: list[tuple[object, str]] = []

    def open_socket(host: str, port: int, *, timeout: float) -> object:
        opened.append((host, port, timeout))
        return socket

    class FakeSSLContext:
        def wrap_socket(self, raw_socket: object, *, server_hostname: str) -> object:
            wrapped.append((raw_socket, server_hostname))
            return "tls-imap-socket"

    monkeypatch.setattr(smtp_transport, "open_tcp_socket", open_socket)
    client = object.__new__(smtp_transport._IMAP4SSL)
    client.host = "imap.example.com"
    client.port = 993
    client.ssl_context = FakeSSLContext()

    assert client._create_socket(10) == "tls-imap-socket"
    assert opened == [("imap.example.com", 993, 10)]
    assert wrapped == [(socket, "imap.example.com")]


def test_email_mcp_opens_imap_through_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.ai.mcp import email as email_mcp

    events: list[tuple[str, int, bool, float]] = []

    class FakeIMAP:
        def login(self, username: str, password: str) -> None:
            pass

    def open_imap(host: str, port: int, *, use_ssl: bool, timeout: float) -> FakeIMAP:
        events.append((host, port, use_ssl, timeout))
        return FakeIMAP()

    monkeypatch.setattr(email_mcp.smtp_transport, "open_imap_client", open_imap)
    client = email_mcp._imap_connect(
        {
            "imap_host": "imap.example.com",
            "username": "alice@example.com",
            "password": "app-password",
        },
    )
    assert isinstance(client, FakeIMAP)
    assert events == [("imap.example.com", 993, True, 20)]


def test_all_smtp_senders_delegate_connection_creation_to_shared_transport() -> None:
    root = Path(__file__).resolve().parents[1]
    senders = (
        root / "packages/core/services/email_service.py",
        root / "packages/core/services/channel_service.py",
        root / "packages/core/services/channels/email_adapter.py",
        root / "packages/core/ai/mcp/email.py",
    )

    for sender in senders:
        source = sender.read_text(encoding="utf-8")
        assert "smtp_transport.send_message" in source, sender
        assert "smtplib.SMTP(" not in source, sender
        assert "smtplib.SMTP_SSL(" not in source, sender

    channel_service = (root / "packages/core/services/channel_service.py").read_text(encoding="utf-8")
    assert "if smtp_transport.proxy_settings() is not None:" in channel_service
    assert "await smtp_transport.send_message_async(" in channel_service


def test_email_mcp_keeps_smtp_authentication_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.ai.mcp import email as email_mcp

    def fail_send(**_kwargs: object) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"credentials rejected")

    monkeypatch.setattr(email_mcp.smtp_transport, "send_message", fail_send)

    with pytest.raises(email_mcp._EmailError, match="SMTP auth failed"):
        email_mcp._send_email(
            {
                "smtp_host": "smtp.example",
                "username": "mailer@example",
                "password": "secret",
            },
            {"to": "user@example", "subject": "Test", "body": "Body"},
        )
