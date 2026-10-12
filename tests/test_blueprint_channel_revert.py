"""Channel ownership and reversible Blueprint routing changes."""
from __future__ import annotations

import copy

import pytest

from packages.core.blueprints.installer import (
    _bind_blueprint_channel_configs,
    sync_workspace_live_setup_requirements,
)
from packages.core.blueprints.upgrade import (
    apply,
    revert,
)
from packages.core.models.channel import TwilioVoiceCallSession
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.services.channel_bindings import (
    load_channel_binding_for_config,
)
from test_blueprint_installer_workflows import _base_payload
from test_blueprint_review_regressions import channel_fixture
from test_blueprint_upgrade_and_revert import _workspace


def _requirement(channel_type="telegram"):
    return {
        "channel_type": channel_type, "linked_service_key": "support",
        "role": "primary_external", "purpose": "Support", "required": True,
    }


async def _scenario(db, *, channel_type="telegram", bound=False):
    fixture_workspace, account, subscription = await channel_fixture(db, channel_type)
    payload = _base_payload(**{
        "contract.channels": [_requirement(channel_type)] if bound else [],
    })
    workspace = await _workspace(db, fixture_workspace.entity_id, installed_from=payload)
    subscription.workspace_id = workspace.id
    await db.flush()
    if bound:
        await _bind_blueprint_channel_configs(
            db, workspace=workspace, user_id=account.owner_user_id,
            channel_requirements=payload["contract"]["channels"],
            selected_channel_config_ids={f"channel:0:{channel_type}": account.id},
        )
    await sync_workspace_live_setup_requirements(db, workspace=workspace, payload=payload)
    return workspace, account, subscription, payload


async def _replacement_subscription(db, workspace):
    agent = Agent(
        entity_id=workspace.entity_id,
        name="Sales",
        status="active",
        config={},
    )
    db.add(agent)
    await db.flush()
    replacement = AgentSubscription(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
        service_key="sales",
        status="active",
    )
    db.add(replacement)
    await db.flush()
    return replacement


async def _upgrade(db, workspace, account, payload):
    return await apply(
        db, workspace=workspace, payload=payload, source_payload=payload,
        by_user_id=account.owner_user_id, current_version="1.0.1",
        channel_config_ids={f"channel:0:{account.channel_type}": account.id},
        require_complete_workspace=True,
    )


async def test_blueprint_twilio_rebind_cancels_unconnected_call(db_session):
    from packages.core.services.voice.call_sessions import create_call_session

    workspace, account, subscription, payload = await _scenario(
        db_session,
        channel_type="twilio_voice",
        bound=True,
    )
    binding = await load_channel_binding_for_config(db_session, account)
    await _replacement_subscription(db_session, workspace)
    pending, _ = await create_call_session(
        db_session,
        entity_id=workspace.entity_id,
        channel_config_id=account.id,
        owner_user_id=account.owner_user_id,
        workspace_id=workspace.id,
        direction="inbound",
        call_sid="CA-blueprint-rebind",
        from_number="+14155550199",
        to_number="+14155550110",
        agent_id=subscription.agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": subscription.id,
        },
    )
    pending_id = pending.id
    payload["contract"]["channels"][0]["linked_service_key"] = "sales"

    await _upgrade(db_session, workspace, account, payload)

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"


@pytest.mark.parametrize("existing_before_upgrade", [False, True])
async def test_blueprint_twilio_revert_cancels_unconnected_call(
    db_session,
    existing_before_upgrade,
):
    from packages.core.services.voice.call_sessions import create_call_session

    workspace, account, subscription, payload = await _scenario(
        db_session,
        channel_type="twilio_voice",
        bound=existing_before_upgrade,
    )
    current = copy.deepcopy(payload)
    if existing_before_upgrade:
        await _replacement_subscription(db_session, workspace)
        current["contract"]["channels"][0]["linked_service_key"] = "sales"
    else:
        current["contract"]["channels"] = [_requirement("twilio_voice")]
    await _upgrade(db_session, workspace, account, current)
    binding = await load_channel_binding_for_config(db_session, account)
    pending, _ = await create_call_session(
        db_session,
        entity_id=workspace.entity_id,
        channel_config_id=account.id,
        owner_user_id=account.owner_user_id,
        workspace_id=workspace.id,
        direction="inbound",
        call_sid=f"CA-blueprint-revert-{existing_before_upgrade}",
        from_number="+14155550199",
        to_number="+14155550110",
        agent_id=binding.agent_id,
        metadata={
            "channel_binding_id": binding.id,
            "agent_subscription_id": binding.agent_subscription_id,
        },
    )
    pending_id = pending.id

    await revert(
        db_session,
        workspace=workspace,
        by_user_id=account.owner_user_id,
    )

    db_session.expire_all()
    saved_pending = await db_session.get(TwilioVoiceCallSession, pending_id)
    assert saved_pending is not None
    assert saved_pending.status == "canceled"
