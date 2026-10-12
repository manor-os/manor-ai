"""Group invitations use the inviter's exact owned sending account."""
from datetime import datetime, timezone
from email.utils import parseaddr

import pytest
from sqlalchemy import select

from packages.core.models.chat_room import ChatRoomInvite, ChatRoomMember
from packages.core.models.document import Integration
from packages.core.models.notification import Notification
from packages.core.models.permission import Capability, ResourceGrant, ResourceType, SubjectType
from packages.core.models.user import OAuthAccount, User
from packages.core.services import email_service
from test_chat_rooms import _connect_group_mailbox, _register


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient_state", ["unknown", "unverified", "verified"])
@pytest.mark.parametrize("entry", ["create", "invite"])
async def test_every_group_invitee_gets_email_from_inviter_smtp(client, db_session, monkeypatch, recipient_state, entry):
    headers, owner = await _register(client, "smtp_inviter")
    recipient = None
    if recipient_state == "unknown":
        email = "new@example.com"
    else:
        _, recipient = await _register(client, "smtp_recipient")
        email = recipient["email"]
        if recipient_state == "verified":
            saved = await db_session.get(User, recipient["id"])
            saved.verified_email = email
            saved.email_verified_at = datetime.now(timezone.utc)
            await db_session.commit()
    await _connect_group_mailbox(client, headers, "sender@example.com")
    sent = []

    async def capture(*, message, **transport):
        sent.append((message, transport))

    async def no_platform_mail(*args, **kwargs):
        pytest.fail("Group invitations must not use platform SMTP")

    monkeypatch.setattr("packages.core.services.smtp_transport.send_message_async", capture)
    monkeypatch.setattr(email_service, "send_email", no_platform_mail)
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Mail delivery", "owner_scope": "personal",
        "recipient_email": email if entry == "create" else "first@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    if entry == "invite":
        sent.clear()
        result = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=headers, json={"email": email})
        assert result.status_code == 201, result.text
        assert result.json()["email_sent"] is True
        token = result.json()["invite_token"]
        next_step = result.json()["invite_next_step"]
    else:
        assert created.json()["invite_email_sent"] is True
        token = created.json()["invite_token"]
        next_step = created.json()["invite_next_step"]
    assert next_step == {"unknown": "sign_up", "unverified": "verify_email", "verified": "accept"}[recipient_state]
    assert len(sent) == 1
    message, transport = sent[0]
    assert parseaddr(str(message["From"]))[1] == "sender@example.com"
    assert message["To"] == email
    assert transport["username"] == "sender@example.com"
    body = message.get_payload()[0].get_payload(decode=True).decode()
    assert f"/chat?invite={token}" in body
    assert "Mail delivery" in body
    members = (await db_session.execute(select(ChatRoomMember).where(ChatRoomMember.room_id == room_id))).scalars().all()
    assert [member.user_id for member in members] == [owner["id"]]
    if recipient_state == "verified":
        notifications = (await db_session.execute(select(Notification).where(
            Notification.user_id == recipient["id"], Notification.type == "chat_room_invite",
        ))).scalars().all()
        assert len(notifications) == 1
        assert notifications[0].meta["link"] == f"/chat?invite={token}"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gmail", "outlook", "nango"])
async def test_group_invites_send_from_owned_oauth_mailbox(client, db_session, monkeypatch, provider):
    headers, owner = await _register(client, "oauth_inviter")
    canonical = "gmail" if provider == "nango" else provider
    if provider == "nango":
        account = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"], provider="gmail", status="active",
            config={"nango": {"connection_id": f"{owner['entity_id']}--{owner['id']}--gmail--test",
                              "provider_config_key": "gmail"}}, credentials={},
        )

        async def secret(*args, **kwargs):
            return "nango-test-secret"

        async def token(**kwargs):
            return "nango-test-token"

        monkeypatch.setattr("packages.core.ai.mcp.nango.get_nango_secret", secret)
        monkeypatch.setattr("packages.core.services.nango_bridge.fetch_nango_access_token", token)
    else:
        account = OAuthAccount(
            user_id=owner["id"], provider=provider, provider_user_id=f"{provider}-mailbox",
            access_token=f"{provider}-test-token", profile={"email": "mailbox@example.com"},
        )
    db_session.add(account)
    await db_session.commit()
    account_id = account.id if provider == "nango" else f"oauth:{account.id}"
    sent = []

    async def mailbox(actual_provider, access_token):
        assert actual_provider == canonical
        assert access_token == f"{provider}-test-token"
        return "mailbox@example.com"

    async def capture(actual_provider, access_token, **kwargs):
        sent.append((actual_provider, access_token, kwargs))
        return True

    monkeypatch.setattr(email_service, "connected_oauth_sender", mailbox)
    monkeypatch.setattr(email_service, "send_connected_oauth_email", capture)
    # A provider's existing Set as default action also sets the common sender.
    smtp_id = await _connect_group_mailbox(client, headers, "smtp@example.com")
    default_path = (
        f"/api/v1/integrations/mcp-servers/{canonical}/entity-accounts/{account.id}/set-default"
        if provider == "nango" else f"/api/v1/integrations/mcp-servers/{canonical}/connections/{account.id}/set-default"
    )
    selected = await client.post(default_path, headers=headers)
    assert selected.status_code == 204, selected.text
    common = (await client.get("/api/v1/integrations/email-accounts", headers=headers)).json()
    assert common["default_sender_id"] == account_id
    options = await client.get("/api/v1/chat/rooms/invite-senders", headers=headers)
    assert {item["id"] for item in options.json()} == {account_id, smtp_id}
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "OAuth delivery", "owner_scope": "personal", "recipient_email": "new@example.com",
    })
    assert created.status_code == 201, created.text
    assert created.json()["invite_email_sent"] is True
    assert len(sent) == 1

    assert sent[0][:2] == (canonical, f"{provider}-test-token")
    assert sent[0][2]["expected_sender"] == "mailbox@example.com"
    assert sent[0][2]["to"] == "new@example.com"
    # Disconnecting the selected account prevents reuse, including on reissue.
    await db_session.delete(account)
    await db_session.commit()
    result = await client.post(f"/api/v1/chat/rooms/{created.json()['id']}/invites", headers=headers,
                               json={"email": "new@example.com", "sender_integration_id": account_id})
    assert result.status_code == 409
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_multiple_mailboxes_automatically_use_existing_provider_default(client, monkeypatch):
    headers, _ = await _register(client, "legacy_mail_default")
    first = await _connect_group_mailbox(client, headers, "first@example.com")
    await _connect_group_mailbox(client, headers, "second@example.com")
    common = (await client.get("/api/v1/integrations/email-accounts", headers=headers)).json()
    assert common["default_sender_id"] == first
    sent = []

    async def capture(credentials, **kwargs):
        sent.append(kwargs["expected_sender"])
        return True

    monkeypatch.setattr(email_service, "send_connected_email", capture)
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Use default", "owner_scope": "personal", "recipient_email": "new@example.com",
    })
    assert created.status_code == 201, created.text
    assert created.json()["invite_email_sent"] is True
    assert sent == ["first@example.com"]


@pytest.mark.asyncio
async def test_disconnected_common_default_never_silently_switches_mailboxes(client, monkeypatch):
    headers, _ = await _register(client, "disconnected_default")
    first = await _connect_group_mailbox(client, headers, "first@example.com")
    second = await _connect_group_mailbox(client, headers, "second@example.com")
    selected = await client.put("/api/v1/integrations/email-accounts/default", headers=headers,
                                json={"sender_id": first})
    assert selected.status_code == 204
    deleted = await client.delete(f"/api/v1/integrations/{first}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    common = (await client.get("/api/v1/integrations/email-accounts", headers=headers)).json()
    assert common["default_sender_id"] is None
    assert [item["id"] for item in common["accounts"]] == [second]
    assert not any(item["is_default"] for item in common["accounts"])
    sent = []

    async def capture(credentials, **kwargs):
        sent.append(kwargs["expected_sender"])
        return True

    monkeypatch.setattr(email_service, "send_connected_email", capture)
    payload = {"kind": "group", "title": "Default unavailable", "owner_scope": "personal", "recipient_email": "new@example.com"}
    created = await client.post("/api/v1/chat/rooms", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["invite_email_sent"] is False
    assert sent == []
    selected = await client.put("/api/v1/integrations/email-accounts/default", headers=headers,
                                json={"sender_id": second})
    assert selected.status_code == 204
    reissued = await client.post(f"/api/v1/chat/rooms/{created.json()['id']}/invites", headers=headers,
                                 json={"email": "new@example.com"})
    assert reissued.status_code == 201, reissued.text
    assert reissued.json()["email_sent"] is True
    assert sent == ["second@example.com"]


@pytest.mark.asyncio
async def test_group_invite_uses_common_default_and_rejects_shared_or_foreign_senders(client, db_session, monkeypatch):
    headers, owner = await _register(client, "sender_choice")
    first = await _connect_group_mailbox(client, headers, "first@example.com")
    second = await _connect_group_mailbox(client, headers, "second@example.com")
    db_session.add(Integration(
        id="shared-email", entity_id=owner["entity_id"], owner_user_id="another-owner", provider="email", status="active",
        credentials={"smtp_host": "smtp.example.com", "username": "shared@example.com", "password": "test-password"},
    ))
    db_session.add(ResourceGrant(
        entity_id=owner["entity_id"], resource_type=ResourceType.INTEGRATION, resource_id="shared-email",
        subject_type=SubjectType.USER, subject_id=owner["id"], capabilities=[Capability.USE],
        granted_by="another-owner", granted_at=datetime.now(timezone.utc), status="active",
    ))
    await db_session.commit()
    foreign_headers, _ = await _register(client, "foreign_sender")
    foreign = await _connect_group_mailbox(client, foreign_headers, "foreign@example.com")
    sent = []

    async def capture(credentials, **kwargs):
        sent.append(kwargs["expected_sender"])
        return True

    monkeypatch.setattr(email_service, "send_connected_email", capture)
    options = await client.get("/api/v1/chat/rooms/invite-senders", headers=headers)
    assert {item["id"] for item in options.json()} == {first, second}
    payload = {"kind": "group", "title": "Sender choice", "owner_scope": "personal", "recipient_email": "new@example.com"}
    for rejected_id in (foreign, "shared-email"):
        denied_default = await client.put("/api/v1/integrations/email-accounts/default", headers=headers,
                                         json={"sender_id": rejected_id})
        assert denied_default.status_code == 409
        rejected = await client.post("/api/v1/chat/rooms", headers=headers,
                                     json={**payload, "sender_integration_id": rejected_id})
        assert rejected.status_code == 409
    assert sent == []
    assert (await db_session.execute(select(ChatRoomInvite))).scalars().all() == []
    selected = await client.put("/api/v1/integrations/email-accounts/default", headers=headers,
                                json={"sender_id": second})
    assert selected.status_code == 204, selected.text
    common = await client.get("/api/v1/integrations/email-accounts", headers=headers)
    assert common.headers["cache-control"] == "private, no-store"
    assert common.json()["default_sender_id"] == second
    assert sum(item["is_default"] for item in common.json()["accounts"]) == 1
    created = await client.post("/api/v1/chat/rooms", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert sent == ["second@example.com"]
    room_id = created.json()["id"]
    original = (await db_session.execute(select(ChatRoomInvite).where(ChatRoomInvite.room_id == room_id))).scalar_one()
    rejected = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=headers,
                                 json={"email": "new@example.com", "sender_integration_id": foreign})
    assert rejected.status_code == 409
    await db_session.refresh(original)
    assert original.status == "pending"
    assert sent == ["second@example.com"]
    reissued = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=headers,
                                 json={"email": "new@example.com", "sender_integration_id": first})
    assert reissued.status_code == 201, reissued.text
    assert sent == ["second@example.com", "first@example.com"]
    await db_session.refresh(original)
    assert original.status == "revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
async def test_missing_or_failed_sending_account_keeps_invitation_and_in_app_notice(client, db_session, monkeypatch, connected):
    headers, _ = await _register(client, "failed_inviter")
    _, recipient = await _register(client, "failed_recipient")
    saved = await db_session.get(User, recipient["id"])
    saved.verified_email = recipient["email"]
    saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    if connected:
        await _connect_group_mailbox(client, headers, "sender@example.com")
    attempts = []

    async def fail_delivery(credentials, **kwargs):
        attempts.append(kwargs)
        return False

    async def no_platform_mail(*args, **kwargs):
        pytest.fail("Missing/failed inviter accounts must not fall back to platform SMTP")

    monkeypatch.setattr(email_service, "send_connected_email", fail_delivery)
    monkeypatch.setattr(email_service, "send_email", no_platform_mail)
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Delivery failed", "owner_scope": "personal", "recipient_email": recipient["email"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["invite_token"]
    assert created.json()["invite_email_sent"] is False
    assert len(attempts) == int(connected)
    notification = (await db_session.execute(select(Notification).where(
        Notification.user_id == recipient["id"], Notification.type == "chat_room_invite",
    ))).scalar_one()
    assert notification.meta["link"] == f"/chat?invite={created.json()['invite_token']}"


@pytest.mark.asyncio
async def test_member_invitation_uses_members_mailbox_instead_of_group_owners(client, db_session, monkeypatch):
    owner_headers, owner = await _register(client, "mailbox_group_owner")
    member_headers, member = await _register(client, "mailbox_group_member")
    assert owner["entity_id"] != member["entity_id"]
    saved = await db_session.get(User, member["id"])
    saved.verified_email = member["email"]
    saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    await _connect_group_mailbox(client, owner_headers, "owner@example.com")
    await _connect_group_mailbox(client, member_headers, "member@example.com")
    sent = []

    async def capture(credentials, **kwargs):
        sent.append((credentials["username"], kwargs["to"]))
        return True

    monkeypatch.setattr(email_service, "send_connected_email", capture)
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Different companies", "owner_scope": "entity", "recipient_email": member["email"],
    })
    assert created.status_code == 201, created.text
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=member_headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    invited = await client.post(f"/api/v1/chat/rooms/{created.json()['id']}/invites", headers=member_headers,
                                json={"email": "new@example.com"})
    assert invited.status_code == 201, invited.text
    assert invited.json()["email_sent"] is True
    assert sent == [("owner@example.com", member["email"]), ("member@example.com", "new@example.com")]
