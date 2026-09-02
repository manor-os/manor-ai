"""Channel ownership and reversible Blueprint routing changes."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from packages.core.blueprints.freshness import BLUEPRINT_SETTINGS_KEY
from packages.core.blueprints.installer import (
    _bind_blueprint_channel_configs,
    sync_workspace_live_setup_requirements,
)
from packages.core.blueprints.upgrade import (
    BlueprintUpgradePlanChangedError,
    RESTORE_POINT_KEY,
    apply,
    revert,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Channel
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.channel_bindings import (
    load_channel_binding_by_id_for_config,
    load_channel_binding_for_config,
)
from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup
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


async def _upgrade(db, workspace, account, payload):
    return await apply(
        db, workspace=workspace, payload=payload, source_payload=payload,
        by_user_id=account.owner_user_id, current_version="1.0.1",
        channel_config_ids={f"channel:0:{account.channel_type}": account.id},
        require_complete_workspace=True,
    )


@pytest.mark.parametrize("channel_type", ["slack", "discord", "twilio_voice", "telegram", "whatsapp"])
async def test_workspace_channel_writers_preserve_source_owner(db_session, monkeypatch, channel_type):
    from apps.api.routers import workspaces as routes

    workspace, account, subscription = await channel_fixture(db_session, channel_type)
    user = SimpleNamespace(id=account.owner_user_id, entity_id=workspace.entity_id)
    monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", AsyncMock())
    monkeypatch.setattr(db_session, "commit", db_session.flush)
    request = routes.WorkspaceChannelRequest(
        channel_config_id=account.id, agent_subscription_id=subscription.id,
    )
    created = await routes.attach_workspace_channel(workspace.id, request, user, db_session)
    binding = await db_session.get(Channel, created["channel_binding_id"])
    assert binding.user_id == account.owner_user_id
    for operation in ("attach", "update"):
        binding.user_id = None
        await db_session.flush()
        if operation == "attach":
            await routes.attach_workspace_channel(workspace.id, request, user, db_session)
        else:
            # A different Workspace manager must retain the account owner,
            # not substitute their own identity while repairing legacy data.
            manager = SimpleNamespace(id=generate_ulid(), entity_id=workspace.entity_id)
            await routes.update_workspace_channel(
                workspace.id, binding.id, routes.WorkspaceChannelUpdateRequest(name="Renamed"),
                manager, db_session,
            )
        assert binding.user_id == account.owner_user_id
        assert await load_channel_binding_by_id_for_config(db_session, account, binding.id) is binding


# WhatsApp is intentionally absent: production now enforces exactly one active
# binding per Business account, so a second active "shared default" route is an
# invalid fixture rather than a Blueprint-repair scenario.
@pytest.mark.parametrize("channel_type", ["telegram", "discord", "twilio_voice"])
@pytest.mark.parametrize("null_priority", [False, True])
async def test_blueprint_owner_repair_keeps_shared_default_route(db_session, channel_type, null_priority):
    workspace, account, subscription, payload = await _scenario(db_session, channel_type=channel_type, bound=True)
    binding = await load_channel_binding_for_config(db_session, account)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    binding.updated_at = old
    binding.user_id = None
    other_workspace = Workspace(entity_id=workspace.entity_id, name="Shared default", status="active")
    db_session.add(other_workspace)
    await db_session.flush()
    other_subscription = AgentSubscription(
        entity_id=workspace.entity_id, workspace_id=other_workspace.id,
        agent_id=subscription.agent_id, service_key="support", status="active",
    )
    db_session.add(other_subscription)
    await db_session.flush()
    other = Channel(
        entity_id=workspace.entity_id, workspace_id=other_workspace.id,
        user_id=account.owner_user_id, type=channel_type, status="active",
        agent_id=subscription.agent_id, agent_subscription_id=other_subscription.id,
        config={"channel_config_id": account.id},
        updated_at=None if null_priority else old + timedelta(days=1),
    )
    db_session.add(other)
    await db_session.flush()
    assert (await load_channel_binding_for_config(db_session, account)).id == other.id

    await _upgrade(db_session, workspace, account, payload)
    await db_session.refresh(binding)

    assert binding.user_id == account.owner_user_id
    assert binding.updated_at == old
    assert (await load_channel_binding_for_config(db_session, account)).id == other.id


@pytest.mark.parametrize("channel_type", ["slack", "telegram", "discord", "twilio_voice", "whatsapp"])
async def test_channel_only_upgrade_and_revert_restore_live_contract(db_session, channel_type):
    workspace, account, _subscription, older = await _scenario(db_session, channel_type=channel_type)
    before = copy.deepcopy(workspace.settings[BLUEPRINT_SETTINGS_KEY])
    current = copy.deepcopy(older)
    current["contract"]["channels"] = [_requirement(channel_type)]

    result = await _upgrade(db_session, workspace, account, current)
    assert result["can_revert"] is True
    assert any(item["kind"] == "channel" for item in result["updated"])
    point = copy.deepcopy(workspace.settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY])
    # The receipt must survive a real JSON round trip and a no-op confirmation.
    await db_session.refresh(workspace)
    repeated = await _upgrade(db_session, workspace, account, current)
    assert repeated["can_revert"] is True
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY] == point

    result = await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert any(item["kind"] == "channel" for item in result["reverted"])
    assert not (await db_session.execute(select(Channel.id).where(Channel.workspace_id == workspace.id))).scalars().all()
    assert await db_session.get(type(account), account.id) is account
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY]["live_setup_requirements"] == before["live_setup_requirements"]
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is None or not readiness.blocks_work
    assert (await revert(db_session, workspace=workspace))["reason"] == "nothing to revert"


@pytest.mark.parametrize("legacy_null_timestamp", [False, True])
async def test_revert_existing_channel_restores_route_and_keeps_repaired_owner(db_session, legacy_null_timestamp):
    workspace, account, subscription, payload = await _scenario(db_session, bound=True)
    binding = await load_channel_binding_for_config(db_session, account)
    old = None if legacy_null_timestamp else datetime.now(timezone.utc) - timedelta(days=1)
    binding.config = {**binding.config, "language": "zh"}
    binding.user_id = None
    binding.updated_at = old
    sales = AgentSubscription(
        entity_id=workspace.entity_id, workspace_id=workspace.id,
        agent_id=subscription.agent_id, service_key="sales", status="active",
    )
    db_session.add(sales)
    await db_session.flush()
    if legacy_null_timestamp:
        # Seed an actual legacy NULL, independently of the ORM's onupdate.
        await db_session.execute(update(Channel).where(Channel.id == binding.id).values(updated_at=None))
        await db_session.refresh(binding)
    before_config = copy.deepcopy(binding.config)
    payload["contract"]["channels"][0]["linked_service_key"] = "sales"
    await _upgrade(db_session, workspace, account, payload)
    assert binding.agent_subscription_id == sales.id
    await db_session.refresh(binding)
    assert binding.updated_at == old

    await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    await db_session.refresh(binding)
    assert binding.agent_subscription_id == subscription.id
    assert binding.config == before_config
    assert binding.updated_at == old
    assert binding.user_id == account.owner_user_id


@pytest.mark.parametrize("changed", [
    "config", "owner", "deleted", "subscription", "service_key", "timestamp",
    "account_disabled", "account_workspace", "agent_disabled", "agent_deleted",
])
async def test_channel_revert_fails_closed_after_later_changes(db_session, changed):
    workspace, account, subscription, payload = await _scenario(db_session, bound=True)
    binding = await load_channel_binding_for_config(db_session, account)
    payload["contract"]["channels"][0]["label"] = "Upgraded channel"
    await _upgrade(db_session, workspace, account, payload)
    point = copy.deepcopy(workspace.settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY])
    if changed == "config":
        binding.config = {**binding.config, "language": "es"}
    elif changed == "owner":
        account.owner_user_id = generate_ulid()
    elif changed == "deleted":
        await db_session.delete(binding)
    elif changed == "subscription":
        subscription.status = "inactive"
    elif changed == "service_key":
        subscription.service_key = "renamed"
    elif changed == "timestamp":
        binding.updated_at = datetime.now(timezone.utc)
    elif changed == "account_disabled":
        account.status = "inactive"
    elif changed == "account_workspace":
        account.workspace_id = generate_ulid()
    else:
        agent = await db_session.get(Agent, subscription.agent_id)
        if changed == "agent_disabled":
            agent.status = "inactive"
        else:
            agent.deleted_at = datetime.now(timezone.utc)
    await db_session.flush()

    with pytest.raises(BlueprintUpgradePlanChangedError):
        await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY] == point
