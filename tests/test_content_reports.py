"""Content reports: a visible Workspace message is emailed to support and acknowledged only once delivered.

Only the SMTP transport is replaced. The real email service, the Workspace
visibility check, and the coded delivery error run unchanged.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services import smtp_transport

SUPPORT_INBOX = "support@manorai.xyz"


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture messages handed to SMTP; email is enabled with the default support sender."""
    sent: list = []

    async def send_message_async(*, message, **_kwargs):
        sent.append(message)

    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("SMTP_FROM_EMAIL", SUPPORT_INBOX)
    monkeypatch.setattr(smtp_transport, "send_message_async", send_message_async)
    return sent


async def _signed_in(client: AsyncClient, username: str) -> tuple[dict[str, str], dict]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "test-password-123",
            "entity_name": f"{username} Co",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return headers, me


async def _seed_workspace_message(
    client: AsyncClient, db_session, headers: dict[str, str], me: dict, *, content: str,
) -> str:
    """Create a workspace the caller owns, with one chat message they authored."""
    resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Reported Workspace"})
    assert resp.status_code in (200, 201), resp.text
    conversation_id, message_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=conversation_id, entity_id=me["entity_id"], workspace_id=resp.json()["id"],
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Message(
        id=message_id, conversation_id=conversation_id, role="user", content=content,
        author_kind="user", message_kind="text", meta={"author_user_id": me["id"]},
    ))
    await db_session.commit()
    return message_id


def _plain_text(message) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode("utf-8")
    raise AssertionError("report email has no plain-text part")


@pytest.mark.asyncio
async def test_report_is_emailed_to_support_and_acknowledged(client: AsyncClient, db_session, sent_emails):
    headers, me = await _signed_in(client, "report_author")
    message_id = await _seed_workspace_message(client, db_session, headers, me, content="x" * 600)

    response = await client.post(
        "/api/v1/content-reports", headers=headers,
        json={"message_id": message_id, "reason": "offensive", "note": "Abusive wording"},
    )

    assert response.status_code == 201, response.text
    assert response.json() == {"received": True}
    [report] = sent_emails
    assert report["To"] == SUPPORT_INBOX
    text = _plain_text(report)
    assert f"Reporter user id: {me['id']}" in text
    assert "Reporter email: report_author@example.com" in text
    assert f"Message id: {message_id}" in text
    assert f"Author user id: {me['id']}" in text
    assert "Reason: offensive" in text
    assert "Note: Abusive wording" in text
    # The excerpt is the last line and is capped at 500 characters.
    assert text.splitlines()[-1] == "x" * 500


@pytest.mark.asyncio
async def test_message_the_caller_cannot_see_looks_the_same_as_a_missing_one(
    client: AsyncClient, db_session, sent_emails,
):
    owner_headers, owner = await _signed_in(client, "report_owner")
    message_id = await _seed_workspace_message(
        client, db_session, owner_headers, owner, content="Private workspace message",
    )
    outsider_headers, _ = await _signed_in(client, "report_outsider")

    foreign = await client.post(
        "/api/v1/content-reports", headers=outsider_headers,
        json={"message_id": message_id, "reason": "spam"},
    )
    missing = await client.post(
        "/api/v1/content-reports", headers=outsider_headers,
        json={"message_id": generate_ulid(), "reason": "spam"},
    )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"]
    assert sent_emails == []


@pytest.mark.asyncio
async def test_report_requires_a_signed_in_user(client: AsyncClient, sent_emails):
    response = await client.post(
        "/api/v1/content-reports",
        json={"message_id": generate_ulid(), "reason": "spam"},
    )

    assert response.status_code == 401
    assert sent_emails == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider_raises", "email_disabled"])
async def test_undelivered_report_is_not_acknowledged(
    client: AsyncClient, db_session, monkeypatch, sent_emails, failure,
):
    headers, me = await _signed_in(client, f"report_{failure}")
    message_id = await _seed_workspace_message(client, db_session, headers, me, content="Reported message")
    if failure == "provider_raises":
        async def unreachable(**_kwargs):
            raise OSError("smtp unreachable")

        monkeypatch.setattr(smtp_transport, "send_message_async", unreachable)
    else:
        monkeypatch.setenv("EMAIL_ENABLED", "false")

    response = await client.post(
        "/api/v1/content-reports", headers=headers,
        json={"message_id": message_id, "reason": "other", "note": None},
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "content_report.delivery_unavailable"
    assert sent_emails == []
