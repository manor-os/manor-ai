"""Provider-level e-sign mail checks; no live OAuth or external delivery."""

import base64
import smtplib
from email import message_from_bytes

import httpx
import pytest

from packages.core.services import email_service
from packages.core.services.smtp_transport import SMTPTransportConfigurationError


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gmail", "outlook"])
async def test_connected_oauth_email_uses_verified_mailbox(monkeypatch, provider):
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "GET":
            return httpx.Response(200, json={
                "emailAddress": "sender@example.com", "mail": "sender@example.com",
            })
        posted.append(request)
        return httpx.Response(200 if provider == "gmail" else 202, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(email_service.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    sent = await email_service.send_connected_oauth_email(
        provider, "test-token", to="signer@partner.com", subject="Sign the agreement",
        content="<p>Sign here</p>", sender_name="Contract Sender",
        expected_sender="sender@example.com",
    )
    assert sent is True
    assert len(posted) == 1
    if provider == "gmail":
        import json

        raw = json.loads(posted[0].content)["raw"]
        message = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert "sender@example.com" in message["From"]
        assert message["To"] == "signer@partner.com"
        assert "Sign here" in message.get_payload(decode=True).decode()
    else:
        assert b'"contentType":"HTML"' in posted[0].content
        assert b"signer@partner.com" in posted[0].content


@pytest.mark.asyncio
async def test_connected_oauth_email_fails_closed_if_mailbox_changes(monkeypatch):
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"emailAddress": "different@example.com"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(email_service.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    sent = await email_service.send_connected_oauth_email(
        "gmail", "test-token", to="signer@partner.com", subject="Sign",
        content="<p>Sign</p>", sender_name="Sender", expected_sender="original@example.com",
    )
    assert sent is False
    assert calls == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gmail", "outlook"])
async def test_connected_oauth_email_carries_safe_list_headers(monkeypatch, provider):
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"emailAddress": "sender@example.com", "mail": "sender@example.com"})
        posted.append(request)
        return httpx.Response(200 if provider == "gmail" else 202, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(email_service.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    sent = await email_service.send_connected_oauth_email(
        provider, "test-token", to="reader@partner.com", subject="Update",
        content="<p>Hi</p>", sender_name="Creator", expected_sender="sender@example.com",
        headers={
            "List-Unsubscribe": "<https://app.example/u/t>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "Bcc": "evil@example.com\r\nX-Injected: 1",
        },
    )
    assert sent is True
    if provider == "gmail":
        import json

        message = message_from_bytes(base64.urlsafe_b64decode(json.loads(posted[0].content)["raw"]))
        assert message["List-Unsubscribe"] == "<https://app.example/u/t>"
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert message["Bcc"] is None and message["X-Injected"] is None
    else:
        # Graph accepts only X- headers; the footer link carries the opt-out.
        assert b"List-Unsubscribe" not in posted[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [("ok", "delivery_failed"), ("unreachable", "delivery_failed"), ("lost", "sender_unavailable")],
)
async def test_oauth_send_result_separates_lost_mailbox_from_failed_delivery(monkeypatch, status, expected):
    from packages.core.services import connected_email_accounts as accounts

    async def lease(**_kwargs):
        return accounts._LeasedSender(
            provider="gmail", address="sender@example.com", kind="oauth_account",
            connection_id="acct", owner_user_id="u1", token="test-token", credentials=None,
        )

    async def refuse(*_args, **_kwargs):
        return False

    async def mailbox_status(_provider, _token, expected_sender):
        assert expected_sender == "sender@example.com"
        return status

    monkeypatch.setattr(accounts, "_lease_sender", lease)
    monkeypatch.setattr(email_service, "send_connected_oauth_email", refuse)
    monkeypatch.setattr(email_service, "connected_oauth_mailbox_status", mailbox_status)
    result = await accounts.send_email_from_account_result(
        sender=("oauth:acct", "sender@example.com", "gmail"), entity_id="e1", user_id="u1",
        to="reader@partner.com", subject="s", content="<p>c</p>", sender_name="Creator",
    )
    assert result.value == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "profile", "expected"),
    [
        (200, {"emailAddress": "Sender@Example.com"}, "ok"),
        (200, {"emailAddress": "other@example.com"}, "lost"),
        (401, {}, "lost"),
        (403, {}, "lost"),
        (403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}, "lost"),
        (403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}, "unreachable"),
        (403, {"error": {"errors": [{"reason": "dailyLimitExceeded"}]}}, "unreachable"),
        (403, {"error": {"status": "RESOURCE_EXHAUSTED"}}, "unreachable"),
        (500, {}, "unreachable"),
        (200, {"unexpected": True}, "unreachable"),
    ],
)
async def test_oauth_mailbox_status_distinguishes_lost_from_unreachable(monkeypatch, status_code, profile, expected):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(email_service.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json=profile)), **kwargs,
    ))
    assert await email_service.connected_oauth_mailbox_status(
        "gmail", "test-token", "sender@example.com",
    ) == expected


@pytest.mark.asyncio
async def test_oauth_mailbox_status_treats_network_failure_as_unreachable(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(email_service.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(boom), **kwargs,
    ))
    assert await email_service.connected_oauth_mailbox_status(
        "outlook", "test-token", "sender@example.com",
    ) == "unreachable"


_SMTP_CREDENTIALS = {
    "smtp_host": "smtp.example.com", "smtp_port": 587, "username": "sender@example.com",
    "password": "app-password", "from_address": "sender@example.com",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login_error", "expected"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "sender_unavailable"),
        (SMTPTransportConfigurationError("both TLS modes"), "sender_unavailable"),
        (OSError("connection reset"), "delivery_failed"),
        (None, "delivery_failed"),
    ],
)
async def test_smtp_send_result_checks_login_to_find_lost_mailbox(monkeypatch, login_error, expected):
    from packages.core.services import connected_email_accounts as accounts

    async def lease(**_kwargs):
        return accounts._LeasedSender(
            provider="email", address="sender@example.com", kind="integration",
            connection_id="integration-1", owner_user_id="u1", token=None,
            credentials=dict(_SMTP_CREDENTIALS),
        )

    async def refuse(*_args, **_kwargs):
        return False

    login_calls = []

    async def check_login(**kwargs):
        login_calls.append(kwargs)
        if login_error:
            raise login_error

    monkeypatch.setattr(accounts, "_lease_sender", lease)
    monkeypatch.setattr(email_service, "send_connected_email", refuse)
    monkeypatch.setattr(email_service.smtp_transport, "check_login_async", check_login)
    result = await accounts.send_email_from_account_result(
        sender=("integration-1", "sender@example.com", "email"), entity_id="e1", user_id="u1",
        to="reader@partner.com", subject="s", content="c", sender_name="Creator",
    )
    assert result.value == expected
    assert [(call["host"], call["use_starttls"]) for call in login_calls] == [("smtp.example.com", True)]


@pytest.mark.asyncio
async def test_smtp_mailbox_status_is_lost_when_identity_no_longer_matches():
    assert await email_service.connected_smtp_mailbox_status(
        dict(_SMTP_CREDENTIALS), "someone-else@example.com",
    ) == "lost"
    assert await email_service.connected_smtp_mailbox_status({}, "sender@example.com") == "lost"


@pytest.mark.asyncio
async def test_send_result_sent_passes_headers_and_records_use(monkeypatch):
    from packages.core.services import connected_email_accounts as accounts

    sent_kwargs, recorded = {}, []

    async def lease(**_kwargs):
        return accounts._LeasedSender(
            provider="gmail", address="sender@example.com", kind="oauth_account",
            connection_id="acct", owner_user_id="owner-1", token="test-token", credentials=None,
        )

    async def deliver(provider, token, **kwargs):
        sent_kwargs.update(kwargs)
        return True

    monkeypatch.setattr(accounts, "_lease_sender", lease)
    monkeypatch.setattr(email_service, "send_connected_oauth_email", deliver)
    monkeypatch.setattr(
        "packages.core.services.connection_usage.record_shared_connection_use",
        lambda **kwargs: recorded.append(kwargs),
    )
    result = await accounts.send_email_from_account_result(
        sender=("oauth:acct", "sender@example.com", "gmail"), entity_id="e1", user_id="u1",
        to="reader@partner.com", subject="s", content="<p>c</p>", sender_name="Creator",
        headers={"List-Unsubscribe": "<https://app.example/u/t>"},
    )
    assert result is accounts.ConnectedEmailSendResult.SENT
    assert sent_kwargs["headers"] == {"List-Unsubscribe": "<https://app.example/u/t>"}
    assert [(r["connection_id"], r["owner_user_id"], r["actor_user_id"]) for r in recorded] == [
        ("acct", "owner-1", "u1"),
    ]


@pytest.mark.asyncio
async def test_connected_smtp_email_passes_headers_to_delivery(monkeypatch):
    captured = {}

    async def fake_deliver(to, subject, html_body, text_body, config, headers=None, attachments=None):
        captured.update(config=config, headers=headers)
        return True, None

    monkeypatch.setattr(email_service, "_deliver_email", fake_deliver)
    sent = await email_service.send_connected_email(
        dict(_SMTP_CREDENTIALS), to="reader@partner.com", subject="s", content="c",
        sender_name="Creator", expected_sender="sender@example.com",
        headers={"List-Unsubscribe": "<https://app.example/u/t>"},
    )
    assert sent is True
    assert captured["headers"] == {"List-Unsubscribe": "<https://app.example/u/t>"}
    assert captured["config"]["hostname"] == "smtp.example.com" and captured["config"]["use_starttls"] is True


@pytest.mark.parametrize(
    ("key", "value", "kept"),
    [
        ("List-Unsubscribe", "<https://app.example/u/t>", True),
        ("Bcc ", "evil@example.com", False),
        ("Bcc:evil@x.com (", "x", False),
        ("X-Note", "line\x0bbreak", False),
        ("X-Note", "a\r\nBcc: evil@example.com", False),
        ("Resent-To", "evil@example.com", False),
    ],
)
def test_safe_extra_headers_drops_unsafe_names_and_values(key, value, kept):
    assert email_service._safe_extra_headers({key: value}) == ({key: value} if kept else {})


@pytest.mark.asyncio
async def test_send_result_reports_unavailable_when_sender_cannot_be_leased(monkeypatch):
    from packages.core.services import connected_email_accounts as accounts

    async def no_lease(**_kwargs):
        return None

    monkeypatch.setattr(accounts, "_lease_sender", no_lease)
    result = await accounts.send_email_from_account_result(
        sender=("integration-1", "sender@example.com", "email"), entity_id="e1", user_id="u1",
        to="reader@partner.com", subject="s", content="c", sender_name="Creator",
    )
    assert result is accounts.ConnectedEmailSendResult.SENDER_UNAVAILABLE
    assert await accounts.send_email_from_account(
        sender=("integration-1", "sender@example.com", "email"), entity_id="e1", user_id="u1",
        to="reader@partner.com", subject="s", content="c", sender_name="Creator",
    ) is False
