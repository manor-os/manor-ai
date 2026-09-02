"""Shared SMTP and IMAP connection selection for direct and SOCKS egress modes."""

from __future__ import annotations

import asyncio
import imaplib
import os
import smtplib
import socket
import ssl
from email.message import Message
from typing import Iterable


class SMTPTransportConfigurationError(RuntimeError):
    """Raised when the configured SMTP egress transport is unusable."""


def proxy_settings() -> tuple[str, int] | None:
    """Return the internal SOCKS endpoint, or ``None`` for direct SMTP."""
    mode = os.getenv("SMTP_EGRESS_MODE", "direct").strip().lower()
    if mode == "direct":
        return None
    if mode != "socks":
        raise SMTPTransportConfigurationError(
            "SMTP_EGRESS_MODE must be either 'direct' or 'socks'",
        )

    host = os.getenv("SMTP_SOCKS_HOST", "").strip()
    if not host:
        raise SMTPTransportConfigurationError(
            "SMTP_SOCKS_HOST is required when SMTP_EGRESS_MODE=socks",
        )

    raw_port = os.getenv("SMTP_SOCKS_PORT", "1080").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SMTPTransportConfigurationError(
            "SMTP_SOCKS_PORT must be an integer",
        ) from exc
    if not 1 <= port <= 65535:
        raise SMTPTransportConfigurationError(
            "SMTP_SOCKS_PORT must be between 1 and 65535",
        )
    return host, port


def open_tcp_socket(host: str, port: int, *, timeout: float) -> socket.socket:
    """Open a TCP socket directly or through Manor's internal SOCKS service."""
    proxy = proxy_settings()
    if proxy is None:
        return socket.create_connection((host, port), timeout=timeout)

    # PySocks is imported only for the opt-in cloud egress mode so the direct
    # path remains a plain standard-library socket connection.
    import socks

    sock = socks.socksocket()
    # Dante applies its destination rules to socket addresses. Resolve here so
    # its SMTP-only allowlist sees the public IP rather than a SOCKS domain name.
    sock.set_proxy(socks.SOCKS5, proxy[0], proxy[1], rdns=False)
    sock.settimeout(timeout)
    sock.connect((host, port))
    return sock


class _SMTP(smtplib.SMTP):
    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return open_tcp_socket(host, port, timeout=timeout)


class _SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = open_tcp_socket(host, port, timeout=timeout)
        return self.context.wrap_socket(sock, server_hostname=host)


class _IMAP4(imaplib.IMAP4):
    def _create_socket(self, timeout: float) -> socket.socket:
        return open_tcp_socket(self.host, self.port, timeout=timeout)


class _IMAP4SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout: float) -> socket.socket:
        sock = open_tcp_socket(self.host, self.port, timeout=timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def open_smtp_client(
    host: str,
    port: int,
    *,
    use_ssl: bool,
    timeout: float,
) -> smtplib.SMTP:
    """Open an SMTP client through the configured egress transport."""
    if proxy_settings() is None:
        client_type = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    else:
        client_type = _SMTP_SSL if use_ssl else _SMTP
    return client_type(host=host, port=port, timeout=timeout)


def open_imap_client(
    host: str,
    port: int,
    *,
    use_ssl: bool,
    timeout: float,
) -> imaplib.IMAP4:
    """Open an IMAP client through the configured egress transport."""
    if proxy_settings() is None:
        client_type = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
    else:
        client_type = _IMAP4SSL if use_ssl else _IMAP4
    return client_type(host=host, port=port, timeout=timeout)


def send_message(
    *,
    message: Message,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_starttls: bool,
    use_ssl: bool,
    timeout: float,
    from_addr: str | None = None,
    to_addrs: Iterable[str] | None = None,
) -> None:
    """Send one MIME message with the configured SMTP connection mode."""
    if use_ssl and use_starttls:
        raise SMTPTransportConfigurationError(
            "SMTP implicit TLS and STARTTLS cannot both be enabled",
        )

    with open_smtp_client(host, port, use_ssl=use_ssl, timeout=timeout) as client:
        client.ehlo()
        if use_starttls:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if username:
            client.login(username, password or "")
        client.send_message(message, from_addr=from_addr, to_addrs=to_addrs)


async def send_message_async(**kwargs: object) -> None:
    """Run the blocking SMTP session outside the caller's event loop."""
    await asyncio.to_thread(send_message, **kwargs)
