"""Focused tests for the opt-in SMTP SOCKS egress transport."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
import sys
from pathlib import Path
import smtplib
import socket
import ssl
import threading
import tomllib

import pytest


@pytest.fixture(scope="module")
def mail_tls_certificates(tmp_path_factory):
    """Disposable certificates for real TLS over local socket pairs."""
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory = tmp_path_factory.mktemp("mail-tls")
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mail test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail.example.test")]))
                   .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("mail.example.test")]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    cert_path, key_path = directory / "server.pem", directory / "server.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_path, cert_path, key_path


@pytest.mark.parametrize("protocol,mode,case", [
    ("smtp", "direct", "trusted"), ("smtp", "direct", "unknown-ca"), ("smtp", "direct", "wrong-host"),
    ("imap", "direct", "trusted"), ("imap", "direct", "unknown-ca"), ("imap", "direct", "wrong-host"),
    ("smtp", "socks", "trusted"), ("smtp", "socks", "unknown-ca"),
    ("imap", "socks", "trusted"), ("imap", "socks", "unknown-ca"),
    ("smtp-starttls-health", "direct", "trusted"),
    ("smtp-starttls-health", "direct", "unknown-ca"),
    ("smtp-starttls-health", "direct", "wrong-host"),
])
def test_mail_tls_verifies_certificate_and_hostname(monkeypatch, mail_tls_certificates, protocol, mode, case):
    from packages.core.services import smtp_transport

    ca_path, cert_path, key_path = mail_tls_certificates
    monkeypatch.setenv("SMTP_EGRESS_MODE", mode)
    monkeypatch.setenv("SMTP_SOCKS_HOST", "proxy.example.test")
    if case == "unknown-ca":
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    else:
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    host = "wrong.example.test" if case == "wrong-host" else "mail.example.test"
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(3)
    peer_socket.settimeout(3)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    received = []
    errors = []

    def server():
        try:
            if protocol == "smtp-starttls-health":
                peer_socket.sendall(b"220 mail.example.test fixture ready\r\n")
                with peer_socket.makefile("rb") as plain:
                    assert plain.readline().upper().startswith(b"EHLO ")
                    peer_socket.sendall(b"250-fixture\r\n250 STARTTLS\r\n")
                    assert plain.readline().strip().upper() == b"STARTTLS"
                    peer_socket.sendall(b"220 ready for TLS\r\n")
            with server_context.wrap_socket(peer_socket, server_side=True) as secured:
                if protocol == "smtp-starttls-health":
                    with secured.makefile("rb") as stream:
                        assert stream.readline().upper().startswith(b"EHLO ")
                        secured.sendall(b"250-fixture\r\n250 AUTH PLAIN\r\n")
                        authentication = stream.readline()
                        received.append(authentication)
                        assert authentication.upper().startswith(b"AUTH PLAIN ")
                        secured.sendall(b"235 authenticated\r\n")
                        assert stream.readline().strip().upper() == b"QUIT"
                        secured.sendall(b"221 bye\r\n")
                elif protocol == "smtp":
                    secured.sendall(b"220 mail.example.test fixture ready\r\n")
                else:
                    secured.sendall(b"* OK fixture ready\r\n")
                    line = secured.makefile("rb").readline()
                    received.append(line)
                    tag = line.split()[0]
                    secured.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK fixture capabilities\r\n")
        except ssl.SSLError:
            # The certificate-negative client terminates the TLS handshake.
            pass
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    # Only the external TCP/proxy boundary is replaced. Python's real TLS,
    # SMTP/IMAP constructors and certificate/hostname checks execute unchanged.
    if mode == "direct":
        monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: client_socket)
    else:
        monkeypatch.setattr(smtp_transport, "open_tcp_socket", lambda *_args, **_kwargs: client_socket)
    opener = smtp_transport.open_smtp_client if protocol == "smtp" else smtp_transport.open_imap_client
    client = None
    try:
        if protocol == "smtp-starttls-health":
            from packages.core.services.integration_health import test_email_login as check_email
            result = asyncio.run(check_email({"smtp_host": host, "smtp_port": 587,
                                             "use_tls_smtp": True, "use_ssl_smtp": False,
                                             "username": "synthetic-user", "password": "synthetic-password"}))
            assert result["ok"] is (case == "trusted"), result
            if case != "trusted":
                assert "CERTIFICATE_VERIFY_FAILED" in result["detail"]
                assert not received
        elif case == "trusted":
            client = opener(host, 465 if protocol == "smtp" else 993, use_ssl=True, timeout=3)
            context = client.context if protocol == "smtp" else client.ssl_context
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
        else:
            with pytest.raises(ssl.SSLCertVerificationError):
                client = opener(host, 465 if protocol == "smtp" else 993, use_ssl=True, timeout=3)
            assert not received
    finally:
        if client is not None:
            client.close() if protocol == "smtp" else client.shutdown()
        client_socket.close()
        thread.join(timeout=4)
        peer_socket.close()
    assert not thread.is_alive()
    assert not errors


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


def test_check_login_authenticates_like_send_message_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.core.services import smtp_transport

    events: list[tuple] = []

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            events.append(("close",))

        def ehlo(self):
            events.append(("ehlo",))

        def starttls(self, context=None):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, *_args, **_kwargs):
            events.append(("send_message",))

    monkeypatch.setattr(smtp_transport, "open_smtp_client", lambda *_a, **_k: FakeSMTP())
    options = dict(host="smtp.example", port=587, username="u", password="p", use_ssl=False, timeout=5)

    smtp_transport.check_login(use_starttls=True, **options)

    assert events == [("ehlo",), ("starttls",), ("ehlo",), ("login", "u", "p"), ("close",)]


def test_check_login_rejects_implicit_tls_with_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.core.services import smtp_transport

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("must not connect with a contradictory TLS configuration")

    monkeypatch.setattr(smtp_transport, "open_smtp_client", unexpected_connect)

    with pytest.raises(smtp_transport.SMTPTransportConfigurationError):
        smtp_transport.check_login(
            host="smtp.example", port=465, username="u", password="p",
            use_starttls=True, use_ssl=True, timeout=5,
        )


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
