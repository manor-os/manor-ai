"""Provider-level e-sign mail checks; no live OAuth or external delivery."""

import base64
from email import message_from_bytes

import httpx
import pytest

from packages.core.services import email_service


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
