from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from packages.core.database import async_session
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.document import Channel, Integration
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services.channel_outbound_delivery import (
    _acquire_locked_channel_reply_route,
    build_channel_reply_route_snapshot,
    channel_reply_route_is_active,
    send_actionable_outbound_to_contact,
    send_channel_text_reply,
    send_outbound_to_contact,
)
from tests.test_document_permissions import _auth


@pytest.mark.asyncio
async def test_external_reply_governance_is_scoped_to_channel_account(
    monkeypatch,
) -> None:
    from packages.core.governance.policy import PolicyDecision
    from packages.core.services import channel_reply_approvals

    captured: dict[str, object] = {}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def check_policy(_db, **kwargs):
        captured.update(kwargs)
        return PolicyDecision(allowed=True)

    monkeypatch.setattr(channel_reply_approvals, "async_session", SessionContext)
    monkeypatch.setattr(
        "packages.core.governance.check_step_policy",
        check_policy,
    )

    result = await channel_reply_approvals.maybe_hold_external_reply_for_approval(
        entity_id="entity-1",
        workspace_id="workspace-1",
        channel_type="gmail",
        channel_config_id="gmail-account-2",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        conversation_id="conversation-1",
        agent_id="agent-1",
        agent_subscription_id=None,
        route_snapshot={},
        sender_id="sender-1",
        sender_name="Sender",
        chat_id="thread-1",
        reply_text="approved reply",
    )

    assert result is None
    assert captured["action_key"] == "external_message.send"
    assert captured["resource_id"] == "gmail-account-2"


def test_external_reply_standing_grant_does_not_cross_channel_accounts() -> None:
    from packages.core.governance.approval_scope import approval_scope_key
    from packages.core.governance.policy import WorkspacePolicy, decide

    action_key = "external_message.send"
    first_account_scope = approval_scope_key(action_key, "gmail-account-1")
    assert first_account_scope is not None
    policy = WorkspacePolicy(
        auto_approve_actions=[first_account_scope],
        hitl_required_actions=[action_key],
    )

    first = decide(
        policy,
        kind="action",
        action_key=action_key,
        resource_id="gmail-account-1",
        risk_level="high",
    )
    second = decide(
        policy,
        kind="action",
        action_key=action_key,
        resource_id="gmail-account-2",
        risk_level="high",
    )

    assert first.allowed is True
    assert second.allowed is False
    assert second.pause_for_hitl is True


@pytest.mark.asyncio
async def test_locked_reply_route_blocks_workspace_revocation_until_send_finishes(
    db_session,
) -> None:
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Locked reply route",
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="discord",
        provider="discord_bot",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="discord",
        name="Locked binding",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="discord",
        source_id="locked-contact",
        status="active",
    )
    db_session.add_all((workspace, config, binding, contact))
    await db_session.commit()
    snapshot = build_channel_reply_route_snapshot(
        config_workspace_id=workspace.id,
        binding_workspace_id=workspace.id,
        runtime_workspace_id=workspace.id,
    )

    lease = await _acquire_locked_channel_reply_route(
        entity_id=entity_id,
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="manor-master",
        agent_subscription_id=None,
        route_snapshot=snapshot,
        workspace_id=workspace.id,
    )
    assert lease is not None
    revocation_started = asyncio.Event()

    async def revoke_workspace() -> None:
        async with async_session() as db:
            revocation_started.set()
            locked = (await db.execute(
                select(Workspace)
                .where(Workspace.id == workspace.id)
                .with_for_update()
            )).scalar_one()
            locked.status = "paused"
            await db.commit()

    revoke_task = asyncio.create_task(revoke_workspace())
    await revocation_started.wait()
    await asyncio.sleep(0.05)
    assert revoke_task.done() is False
    await lease.release()
    await asyncio.wait_for(revoke_task, timeout=2)

    async with async_session() as cleanup_db:
        for model, row_id in (
            (ChannelContact, contact.id),
            (Channel, binding.id),
            (ChannelConfig, config.id),
            (Workspace, workspace.id),
        ):
            row = await cleanup_db.get(model, row_id)
            if row is not None:
                await cleanup_db.delete(row)
        await cleanup_db.commit()


@pytest.mark.asyncio
async def test_locked_reply_route_acquires_workspace_before_channel_config(
    db_session,
) -> None:
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Canonical route lock order",
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="discord",
        provider="discord_bot",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="discord",
        name="Canonical route binding",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="discord",
        source_id="canonical-lock-contact",
        status="active",
    )
    db_session.add_all((workspace, config, binding, contact))
    await db_session.commit()
    snapshot = build_channel_reply_route_snapshot(
        config_workspace_id=workspace.id,
        binding_workspace_id=workspace.id,
        runtime_workspace_id=workspace.id,
    )

    writer_db = async_session()
    route_task = None
    route_lease = None
    config_lock_error: BaseException | None = None
    try:
        await writer_db.execute(
            select(Workspace.id)
            .where(Workspace.id == workspace.id)
            .with_for_update()
        )
        route_task = asyncio.create_task(_acquire_locked_channel_reply_route(
            entity_id=entity_id,
            cc_id=config.id,
            channel_type="discord",
            channel_binding_id=binding.id,
            channel_contact_id=contact.id,
            agent_id="manor-master",
            agent_subscription_id=None,
            route_snapshot=snapshot,
            workspace_id=workspace.id,
        ))
        await asyncio.sleep(0.05)
        assert route_task.done() is False
        try:
            await writer_db.execute(
                select(ChannelConfig.id)
                .where(ChannelConfig.id == config.id)
                .with_for_update(nowait=True)
            )
        except BaseException as exc:  # assertion below reports the lock inversion
            config_lock_error = exc
    finally:
        await writer_db.rollback()
        await writer_db.close()
        if route_task is not None:
            route_lease = await asyncio.wait_for(route_task, timeout=2)

    if route_lease is not None:
        await route_lease.release()
    assert config_lock_error is None
    assert route_lease is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation_target", ("membership", "user"))
async def test_locked_reply_route_blocks_connection_owner_revocation(
    db_session,
    revocation_target,
) -> None:
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Connection owner authorization lock",
        status="active",
    )
    owner = User(
        id=owner_user_id,
        entity_id=entity_id,
        email=f"{owner_user_id}@example.test",
        password_hash="test-only",
        role="member",
        status="active",
    )
    membership = UserMembership(
        id=generate_ulid(),
        user_id=owner_user_id,
        entity_id=entity_id,
        role="member",
        status="active",
        is_primary=True,
    )
    integration = Integration(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        created_by_user_id=owner_user_id,
        provider="discord",
        status="active",
        credentials={"bot_token": "test-token"},
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="discord",
        provider="discord_bot",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="discord",
        name="Owner-authorized binding",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="discord",
        source_id="owner-auth-contact",
        status="active",
    )
    db_session.add_all((
        workspace,
        owner,
        membership,
        integration,
        config,
        binding,
        contact,
    ))
    await db_session.commit()

    lease = await _acquire_locked_channel_reply_route(
        entity_id=entity_id,
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="manor-master",
        agent_subscription_id=None,
        route_snapshot=build_channel_reply_route_snapshot(
            config_workspace_id=workspace.id,
            binding_workspace_id=workspace.id,
            runtime_workspace_id=workspace.id,
        ),
        workspace_id=workspace.id,
    )
    assert lease is not None
    revocation_started = asyncio.Event()

    async def revoke_owner_authorization() -> None:
        async with async_session() as db:
            revocation_started.set()
            if revocation_target == "membership":
                await db.execute(
                    update(UserMembership)
                    .where(UserMembership.id == membership.id)
                    .values(status="inactive")
                )
            else:
                await db.execute(
                    update(User)
                    .where(User.id == owner.id)
                    .values(status="inactive")
                )
            await db.commit()

    revoke_task = asyncio.create_task(revoke_owner_authorization())
    await revocation_started.wait()
    await asyncio.sleep(0.05)
    assert revoke_task.done() is False
    await lease.release()
    await asyncio.wait_for(revoke_task, timeout=2)

    async with async_session() as verify_db:
        if revocation_target == "membership":
            decision = await verify_db.get(UserMembership, membership.id)
        else:
            decision = await verify_db.get(User, owner.id)
        assert decision is not None
        assert decision.status == "inactive"


@pytest.mark.asyncio
async def test_non_slack_binding_creation_waits_for_active_reply_route(
    client,
    db_session,
) -> None:
    headers = await _auth(client, "non_slack_binding_route_lock")
    me_response = await client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200
    owner = me_response.json()
    first_workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Active provider route"},
    )
    second_workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "New provider route"},
    )
    assert first_workspace_response.status_code == 201
    assert second_workspace_response.status_code == 201
    first_workspace_id = first_workspace_response.json()["id"]
    second_workspace_id = second_workspace_response.json()["id"]

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        workspace_id=None,
        channel_type="discord",
        provider="discord_bot",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        workspace_id=first_workspace_id,
        type="discord",
        name="Existing Discord route",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        source_id="binding-lock-contact",
        status="active",
    )
    db_session.add_all((config, binding, contact))
    await db_session.commit()

    lease = await _acquire_locked_channel_reply_route(
        entity_id=owner["entity_id"],
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="manor-master",
        agent_subscription_id=None,
        route_snapshot=build_channel_reply_route_snapshot(
            config_workspace_id=None,
            binding_workspace_id=first_workspace_id,
            runtime_workspace_id=first_workspace_id,
        ),
        workspace_id=first_workspace_id,
    )
    assert lease is not None

    attach_task = asyncio.create_task(client.post(
        f"/api/v1/workspaces/{second_workspace_id}/channels",
        headers=headers,
        json={"channel_config_id": config.id},
    ))
    await asyncio.sleep(0.05)
    assert attach_task.done() is False
    await lease.release()
    response = await asyncio.wait_for(attach_task, timeout=2)

    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_channel_route_rejects_workspace_from_another_entity(
    db_session,
) -> None:
    entity_id = generate_ulid()
    foreign_workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Foreign Workspace",
        status="active",
    )
    owner_user_id = generate_ulid()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=foreign_workspace.id,
        channel_type="discord",
        provider="discord_bot",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=foreign_workspace.id,
        type="discord",
        name="Foreign workspace route",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="discord",
        source_id="discord-contact",
        status="active",
    )
    db_session.add_all([foreign_workspace, config, binding, contact])
    await db_session.commit()

    assert not await channel_reply_route_is_active(
        entity_id=entity_id,
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="manor-master",
        agent_subscription_id=None,
        route_snapshot=build_channel_reply_route_snapshot(
            config_workspace_id=foreign_workspace.id,
            binding_workspace_id=foreign_workspace.id,
            runtime_workspace_id=foreign_workspace.id,
        ),
        workspace_id=foreign_workspace.id,
    )


@pytest.mark.asyncio
async def test_channel_route_rejects_subscription_from_another_entity(
    db_session,
) -> None:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Local Workspace",
        status="active",
    )
    foreign_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        agent_id="foreign-agent",
        workspace_id=workspace.id,
        status="active",
    )
    owner_user_id = generate_ulid()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace.id,
        channel_type="discord",
        provider="discord_bot",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=workspace.id,
        type="discord",
        name="Foreign subscription route",
        config={"channel_config_id": config.id},
        agent_id=foreign_subscription.agent_id,
        agent_subscription_id=foreign_subscription.id,
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="discord",
        source_id="foreign-subscription-contact",
        status="active",
    )
    db_session.add_all([
        workspace,
        foreign_subscription,
        config,
        binding,
        contact,
    ])
    await db_session.commit()

    assert not await channel_reply_route_is_active(
        entity_id=entity_id,
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id=foreign_subscription.agent_id,
        agent_subscription_id=foreign_subscription.id,
        route_snapshot=build_channel_reply_route_snapshot(
            config_workspace_id=workspace.id,
            binding_workspace_id=workspace.id,
            runtime_workspace_id=workspace.id,
        ),
        workspace_id=workspace.id,
    )


@pytest.mark.asyncio
async def test_channel_route_change_starts_a_new_conversation(db_session) -> None:
    from packages.core.services.channel_conversations import (
        get_or_create_channel_conversation,
    )

    entity_id = generate_ulid()
    first_workspace_id = generate_ulid()
    second_workspace_id = generate_ulid()
    db_session.add_all([
        Workspace(
            id=first_workspace_id,
            entity_id=entity_id,
            name="First route",
            status="active",
        ),
        Workspace(
            id=second_workspace_id,
            entity_id=entity_id,
            name="Second route",
            status="active",
        ),
    ])
    await db_session.flush()
    route = {
        "entity_id": entity_id,
        "channel_type": "slack",
        "channel_config_id": generate_ulid(),
        "channel_contact_id": generate_ulid(),
        "sender_id": "U-ROUTE",
        "sender_name": "Route User",
        "chat_id": "C-ROUTE",
        "agent_id": "manor-master",
        "conversation_key": "slack:C-ROUTE:direct",
    }

    first = await get_or_create_channel_conversation(
        db_session,
        **route,
        workspace_id=first_workspace_id,
    )
    second = await get_or_create_channel_conversation(
        db_session,
        **route,
        workspace_id=second_workspace_id,
    )

    assert second.id != first.id
    assert first.workspace_id == first_workspace_id
    assert second.workspace_id == second_workspace_id


@pytest.mark.asyncio
async def test_channel_attachment_stops_when_exact_route_is_revoked(
    monkeypatch,
) -> None:
    from packages.core.services import channel_agent_runtime, channel_outbound_delivery

    sends: list[str] = []

    class Adapter:
        async def send_attachment(self, *_args, **_kwargs):
            sends.append("attachment")
            return {"status": "sent"}

    async def route_inactive(**_kwargs) -> bool:
        return False

    monkeypatch.setitem(channel_agent_runtime.ADAPTERS, "discord", Adapter())
    monkeypatch.setattr(
        channel_outbound_delivery,
        "channel_reply_route_is_active",
        route_inactive,
    )
    _schema, handler = channel_agent_runtime.build_channel_attachment_tool(
        SimpleNamespace(id="config-1"),
        "discord",
        "channel-1",
        reply_route={"entity_id": "entity-1"},
    )

    result = json.loads(await handler({"url": "https://example.test/report.pdf"}))

    assert result == {"error": "reply route unavailable"}
    assert sends == []


@pytest.mark.asyncio
async def test_channel_attachment_fallback_rechecks_route(monkeypatch) -> None:
    from packages.core.services import channel_agent_runtime, channel_outbound_delivery

    sends: list[str] = []
    route_states = iter([True, False])

    class Adapter:
        async def send_attachment(self, *_args, **_kwargs):
            raise NotImplementedError("attachments unavailable")

        async def send_text(self, *_args, **_kwargs):
            sends.append("fallback")

    async def route_changes(**_kwargs) -> bool:
        return next(route_states)

    monkeypatch.setitem(channel_agent_runtime.ADAPTERS, "discord", Adapter())
    monkeypatch.setattr(
        channel_outbound_delivery,
        "channel_reply_route_is_active",
        route_changes,
    )
    _schema, handler = channel_agent_runtime.build_channel_attachment_tool(
        SimpleNamespace(id="config-1"),
        "discord",
        "channel-1",
        reply_route={"entity_id": "entity-1"},
    )

    result = json.loads(await handler({"url": "https://example.test/report.pdf"}))

    assert result == {"error": "reply route unavailable"}
    assert sends == []


@pytest.mark.asyncio
async def test_contact_subscription_route_can_override_channel_workspace(
    db_session,
) -> None:
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    default_workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Channel default",
        status="active",
    )
    contact_workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Contact override",
        status="active",
    )
    default_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id="default-agent",
        workspace_id=default_workspace.id,
        status="active",
    )
    contact_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id="contact-agent",
        workspace_id=contact_workspace.id,
        status="active",
    )
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=default_workspace.id,
        channel_type="telegram",
        provider="telegram_bot",
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=owner_user_id,
        workspace_id=default_workspace.id,
        type="telegram",
        name="Shared Telegram bot",
        config={"channel_config_id": config.id},
        agent_id=default_subscription.agent_id,
        agent_subscription_id=default_subscription.id,
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="telegram",
        source_id="telegram-contact",
        agent_subscription_id=contact_subscription.id,
        status="active",
    )
    db_session.add_all([
        default_workspace,
        contact_workspace,
        default_subscription,
        contact_subscription,
        config,
        binding,
        contact,
    ])
    await db_session.commit()

    assert await channel_reply_route_is_active(
        entity_id=entity_id,
        cc_id=config.id,
        channel_type="telegram",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id=contact_subscription.agent_id,
        agent_subscription_id=contact_subscription.id,
        route_snapshot=build_channel_reply_route_snapshot(
            config_workspace_id=default_workspace.id,
            binding_workspace_id=default_workspace.id,
            runtime_workspace_id=contact_workspace.id,
        ),
        workspace_id=contact_workspace.id,
    )


@pytest.mark.asyncio
async def test_send_channel_text_reply_rejects_inactive_config(monkeypatch) -> None:
    from packages.core.services import channel_outbound_delivery as delivery

    sends: list[str] = []

    class Adapter:
        async def send_text(self, _cc, _chat_id, text, **_kwargs):
            sends.append(text)
            return {"status": "sent"}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def load_inactive(_db, _cc_id):
        return SimpleNamespace(
            id="config-1",
            channel_type="slack",
            status="inactive",
            workspace_id=None,
        )

    monkeypatch.setitem(delivery.ADAPTERS, "slack", Adapter())
    monkeypatch.setattr(delivery, "async_session", lambda: SessionContext())
    monkeypatch.setattr(delivery, "load_channel_config", load_inactive)

    assert not await send_channel_text_reply(
        cc_id="config-1",
        channel_type="slack",
        chat_id="channel-1",
        text="must not send",
    )
    assert sends == []


@pytest.mark.asyncio
async def test_send_channel_text_reply_propagates_soft_time_limit(
    monkeypatch,
) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    from packages.core.services import channel_outbound_delivery as delivery

    class Adapter:
        async def send_text(self, _cc, _chat_id, _text, **_kwargs):
            raise SoftTimeLimitExceeded()

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    config = SimpleNamespace(
        id="config-1",
        entity_id="entity-1",
        owner_user_id="owner-1",
        channel_type="slack",
        status="active",
        workspace_id=None,
    )

    async def load_config(_db, _cc_id):
        return config

    async def credentials_available(_db, _config):
        return True

    async def workspace_routable(_db, _workspace_id, **_kwargs):
        return True

    async def unexpected_failure_projection(*_args, **_kwargs):
        raise AssertionError("soft timeout must remain owned by the Celery boundary")

    monkeypatch.setitem(delivery.ADAPTERS, "slack", Adapter())
    monkeypatch.setattr(delivery, "async_session", lambda: SessionContext())
    monkeypatch.setattr(delivery, "load_channel_config", load_config)
    monkeypatch.setattr(
        delivery,
        "channel_credential_source_is_available",
        credentials_available,
    )
    monkeypatch.setattr(delivery, "channel_workspace_is_routable", workspace_routable)
    monkeypatch.setattr(
        delivery,
        "mark_last_channel_outbound_failed",
        unexpected_failure_projection,
    )

    with pytest.raises(SoftTimeLimitExceeded):
        await send_channel_text_reply(
            cc_id="config-1",
            channel_type="slack",
            chat_id="channel-1",
            text="provider may have accepted this",
            idempotency_key="receipt-1",
        )


@pytest.mark.asyncio
async def test_send_channel_text_reply_returns_false_for_explicit_provider_failure(
    monkeypatch,
) -> None:
    from packages.core.services import channel_outbound_delivery as delivery

    class Adapter:
        async def send_text(self, _cc, _chat_id, _text, **_kwargs):
            return {"status": "failed", "error": "provider rejected message"}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    projections: list[dict[str, str]] = []

    async def project_result(_cc_id, _chat_id, result):
        projections.append(result)

    monkeypatch.setitem(delivery.ADAPTERS, "slack", Adapter())
    monkeypatch.setattr(delivery, "async_session", lambda: SessionContext())
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "mark_last_channel_outbound_sent", project_result)

    assert not await send_channel_text_reply(
        cc_id="config-1",
        channel_type="slack",
        chat_id="channel-1",
        text="must remain retryable",
    )
    assert projections == [{
        "status": "failed",
        "error": "provider rejected message",
    }]


@pytest.mark.asyncio
async def test_provider_acceptance_survives_outbound_projection_failure(
    monkeypatch,
) -> None:
    from packages.core.services import channel_outbound_delivery as delivery

    sends: list[str] = []

    class Adapter:
        async def send_text(self, _cc, _chat_id, text, **_kwargs):
            sends.append(text)
            return {"status": "sent", "external_id": "provider-message-1"}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    config = SimpleNamespace(
        id="config-1",
        entity_id="entity-1",
        owner_user_id="owner-1",
        channel_type="slack",
        status="active",
        workspace_id=None,
    )
    binding = SimpleNamespace(
        id="binding-1",
        user_id="owner-1",
        workspace_id=None,
    )

    async def load_config(_db, _cc_id):
        return config

    async def credentials_available(_db, _config):
        return True

    async def workspace_routable(_db, _workspace_id, **_kwargs):
        return True

    async def load_bindings(_db, _config):
        return [binding]

    async def projection_failed(*_args, **_kwargs):
        raise RuntimeError("database unavailable after provider accepted")

    async def unexpected_failed_projection(*_args, **_kwargs):
        raise AssertionError("provider acceptance must not be rewritten as send failure")

    monkeypatch.setitem(delivery.ADAPTERS, "slack", Adapter())
    monkeypatch.setattr(delivery, "async_session", lambda: SessionContext())
    monkeypatch.setattr(delivery, "load_channel_config", load_config)
    monkeypatch.setattr(
        delivery,
        "channel_credential_source_is_available",
        credentials_available,
    )
    monkeypatch.setattr(delivery, "channel_workspace_is_routable", workspace_routable)
    monkeypatch.setattr(delivery, "load_slack_channel_bindings_for_config", load_bindings)
    monkeypatch.setattr(delivery, "mark_last_channel_outbound_sent", projection_failed)
    monkeypatch.setattr(
        delivery,
        "mark_last_channel_outbound_failed",
        unexpected_failed_projection,
    )

    assert await send_channel_text_reply(
        cc_id="config-1",
        channel_type="slack",
        chat_id="channel-1",
        text="send once",
        idempotency_key="receipt-1",
    )
    assert sends == ["send once"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actionable", [False, True])
async def test_notification_send_rechecks_contact_revocation(
    db_session,
    monkeypatch,
    actionable: bool,
) -> None:
    from packages.core.services import channel_outbound_delivery as delivery

    sends: list[str] = []

    class Adapter:
        async def send_text(self, *_args, **_kwargs):
            sends.append("text")
            return {"status": "sent"}

        async def send_actionable_message(self, *_args, **_kwargs):
            sends.append("actionable")
            return {"status": "sent"}

    entity_id = generate_ulid()
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=generate_ulid(),
        channel_type="telegram",
        provider="telegram_bot",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=config.id,
        channel_type="telegram",
        source_id="telegram-recipient",
        user_id=generate_ulid(),
        status="active",
    )
    db_session.add_all([config, contact])
    await db_session.commit()
    stale_contact = SimpleNamespace(
        id=contact.id,
        entity_id=contact.entity_id,
        channel_config_id=contact.channel_config_id,
        channel_type=contact.channel_type,
        source_id=contact.source_id,
        user_id=contact.user_id,
    )
    contact.status = "blocked"
    await db_session.commit()
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", Adapter())

    if actionable:
        result = await send_actionable_outbound_to_contact(
            db_session,
            contact=stale_contact,
            text="revoked content",
            actions=[{"key": "approve", "label": "Approve"}],
        )
    else:
        result = await send_outbound_to_contact(
            db_session,
            contact=stale_contact,
            text="revoked content",
        )

    assert result == {"sent": False, "error": "channel_route_unavailable"}
    assert sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actionable", [False, True])
async def test_notification_send_uses_live_config_credential_workspace_gate(
    monkeypatch,
    actionable: bool,
) -> None:
    from packages.core.services import channel_outbound_delivery as delivery

    sends: list[str] = []
    gate_calls: list[tuple[str, str, str]] = []
    contact = SimpleNamespace(
        id="contact-1",
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="slack",
        source_id="U-1",
        user_id="user-1",
        status="active",
    )

    class Result:
        def scalar_one_or_none(self):
            return contact

    class DB:
        async def execute(self, _statement):
            return Result()

    class Adapter:
        async def send_text(self, *_args, **_kwargs):
            sends.append("text")

        async def send_actionable_message(self, *_args, **_kwargs):
            sends.append("actionable")

    async def route_revoked(
        _db,
        *,
        cc_id: str,
        channel_type: str,
        entity_id: str,
    ):
        gate_calls.append((cc_id, channel_type, entity_id))
        return None

    monkeypatch.setitem(delivery.ADAPTERS, "slack", Adapter())
    monkeypatch.setattr(
        delivery,
        "_load_routable_channel_config",
        route_revoked,
    )

    if actionable:
        result = await send_actionable_outbound_to_contact(
            DB(),
            contact=contact,
            text="workspace content",
            actions=[{"key": "open", "label": "Open"}],
        )
    else:
        result = await send_outbound_to_contact(
            DB(),
            contact=contact,
            text="workspace content",
        )

    assert result == {"sent": False, "error": "channel_route_unavailable"}
    assert gate_calls == [("config-1", "slack", "entity-1")]
    assert sends == []
