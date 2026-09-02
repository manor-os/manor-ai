"""Regressions for channel authorization, stable routing, and setup-only undo."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from packages.core.blueprints.freshness import BLUEPRINT_SETTINGS_KEY
from packages.core.blueprints.installer import InstallError, _bind_blueprint_channel_configs
from packages.core.blueprints.setup_preflight import BlueprintSetupPreflightFactory
from packages.core.blueprints.upgrade import (
    LIVE_SETUP_STATE_KEYS,
    RESTORE_POINT_KEY,
    BlueprintUpgradePlanChangedError,
    apply,
    revert,
)
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services.channel_bindings import (
    load_channel_binding_by_id_for_config,
    load_channel_binding_for_config,
    load_channel_binding_scopes_for_config,
    snapshot_channel_binding,
)
from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup
from test_channel_binding_upgrade_revert import _scenario, _upgrade


async def _binding(db, workspace):
    return (await db.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ))).scalar_one()


def _setup_state(workspace):
    record = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    return {key: copy.deepcopy(record[key]) for key in LIVE_SETUP_STATE_KEYS if key in record}


async def test_requirement_only_upgrade_has_a_durable_idempotent_undo(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    before = _setup_state(workspace)
    version = workspace.settings[BLUEPRINT_SETTINGS_KEY].get("blueprint_version")
    binding = await _binding(db_session, workspace)
    binding_before = snapshot_channel_binding(binding)
    payload["contract"]["channels"][0]["required"] = False

    result = await _upgrade(db_session, workspace, account, payload)
    assert result["updated"] == []
    assert result["can_revert"] is True
    assert _setup_state(workspace) != before
    assert snapshot_channel_binding(binding) == binding_before
    point = copy.deepcopy(workspace.settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY])
    await db_session.refresh(workspace)
    repeated = await _upgrade(db_session, workspace, account, payload)
    assert repeated["can_revert"] is True
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY] == point

    undo = await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert "reason" not in undo
    assert _setup_state(workspace) == before
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY].get("blueprint_version") == version
    assert snapshot_channel_binding(binding) == binding_before
    assert (await revert(db_session, workspace=workspace))["reason"] == "nothing to revert"


async def test_setup_only_undo_rejects_later_operator_edits(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    payload["contract"]["channels"][0]["required"] = False
    await _upgrade(db_session, workspace, account, payload)
    settings = copy.deepcopy(workspace.settings)
    settings[BLUEPRINT_SETTINGS_KEY]["live_setup_requirements"][0]["payload"]["purpose"] = "Local edit"
    workspace.settings = settings
    await db_session.flush()

    with pytest.raises(BlueprintUpgradePlanChangedError):
        await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert workspace.settings == settings


async def test_upgrade_drops_superseded_channel_todo_but_keeps_other_install_failures(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    settings = copy.deepcopy(workspace.settings)
    record = settings[BLUEPRINT_SETTINGS_KEY]
    unrelated = {
        "kind": "missing_integration", "detail": "Connect the separately required account.",
        "payload": {"provider": "github"}, "blocking": True,
    }
    record["install_todos"] = [*copy.deepcopy(record["live_setup_requirements"]), unrelated]
    record["blocking_todo_count"] = 2
    workspace.settings = settings
    await db_session.flush()
    before = _setup_state(workspace)
    payload["contract"]["channels"][0]["required"] = False

    result = await _upgrade(db_session, workspace, account, payload)
    assert result["fully_synchronized"] is True
    state = _setup_state(workspace)
    channels = [item for item in state["live_setup_requirements"] if item["kind"] == "channel"]
    assert len(channels) == 1
    assert channels[0]["blocking"] is False
    assert state["install_todos"] == [unrelated]
    assert unrelated in state["live_setup_requirements"]
    assert state["blocking_todo_count"] == 1

    await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert _setup_state(workspace) == before


async def test_obsolete_required_channel_no_longer_blocks_when_optional_account_disconnects(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    settings = copy.deepcopy(workspace.settings)
    record = settings[BLUEPRINT_SETTINGS_KEY]
    record["install_todos"] = copy.deepcopy(record["live_setup_requirements"])
    record["blocking_todo_count"] = 1
    workspace.settings = settings
    await db_session.flush()
    payload["contract"]["channels"][0]["required"] = False
    await _upgrade(db_session, workspace, account, payload)
    account.status = "inactive"
    await db_session.flush()

    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is None or not readiness.blocks_work


async def test_unchanged_pending_channel_still_blocks_and_does_not_create_undo(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    settings = copy.deepcopy(workspace.settings)
    record = settings[BLUEPRINT_SETTINGS_KEY]
    record["install_todos"] = copy.deepcopy(record["live_setup_requirements"])
    record["blocking_todo_count"] = 1
    workspace.settings = settings
    await db_session.flush()
    before = _setup_state(workspace)

    result = await _upgrade(db_session, workspace, account, payload)
    assert result["can_revert"] is False
    assert _setup_state(workspace) == before
    account.status = "inactive"
    await db_session.flush()
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is not None and readiness.blocks_work


async def test_removing_a_channel_requirement_is_reversible_without_deleting_its_binding(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    before = _setup_state(workspace)
    binding = await _binding(db_session, workspace)
    binding_before = snapshot_channel_binding(binding)
    payload["contract"]["channels"] = []

    result = await apply(
        db_session, workspace=workspace, payload=payload, source_payload=payload,
        by_user_id=account.owner_user_id, require_complete_workspace=True,
    )
    assert result["updated"] == []
    assert result["can_revert"] is True
    assert _setup_state(workspace)["live_setup_requirements"] == []
    await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert _setup_state(workspace) == before
    assert snapshot_channel_binding(binding) == binding_before


@pytest.mark.parametrize("changed", ["purpose", "role", "linked_service_key"])
async def test_workspace_manager_can_upgrade_existing_other_members_channel(db_session, changed):
    workspace, account, subscription, payload = await _scenario(db_session, bound=True)
    manager_id = generate_ulid()
    binding = await _binding(db_session, workspace)
    before = await BlueprintSetupPreflightFactory.from_contract(
        db_session, contract=payload["contract"], entity_id=workspace.entity_id,
        user_id=manager_id, workspace_id=workspace.id,
    )
    assert before.ready
    payload["contract"]["channels"][0][changed] = "Revised" if changed != "role" else "secondary_external"
    if changed == "linked_service_key":
        db_session.add(AgentSubscription(
            entity_id=workspace.entity_id, workspace_id=workspace.id,
            agent_id=subscription.agent_id, service_key="Revised", status="active",
        ))
        await db_session.flush()
    after = await BlueprintSetupPreflightFactory.from_contract(
        db_session, contract=payload["contract"], entity_id=workspace.entity_id,
        user_id=manager_id, workspace_id=workspace.id,
    )
    assert after.ready
    assert after.requirements[0].resource_id == account.id
    assert [option.id for option in after.requirements[0].resource_options] == [account.id]
    await apply(
        db_session, workspace=workspace, payload=payload, source_payload=payload,
        by_user_id=manager_id, channel_config_ids={"channel:0:telegram": account.id},
        require_complete_workspace=True,
    )
    assert binding.config[changed] == payload["contract"]["channels"][0][changed]
    assert binding.user_id == account.owner_user_id


async def test_metadata_matches_are_preferences_not_access_grants(db_session):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    manager_id = generate_ulid()
    other = ChannelConfig(
        entity_id=workspace.entity_id, owner_user_id=generate_ulid(),
        channel_type="telegram", provider="telegram", status="active",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(Channel(
        entity_id=workspace.entity_id, workspace_id=workspace.id,
        user_id=other.owner_user_id, type="telegram", status="active",
        config={
            "channel_config_id": other.id, "role": "primary_external",
            "purpose": "Sales", "linked_service_key": "support",
        },
    ))
    await db_session.flush()

    async def preflight(selected=None):
        return await BlueprintSetupPreflightFactory.from_contract(
            db_session, contract=payload["contract"], entity_id=workspace.entity_id,
            user_id=manager_id, workspace_id=workspace.id,
            selected_channel_config_ids=selected,
        )

    matching = await preflight()
    assert matching.requirements[0].resource_id == account.id
    payload["contract"]["channels"][0]["purpose"] = "New purpose"
    ambiguous = await preflight()
    assert not ambiguous.ready
    assert {option.id for option in ambiguous.requirements[0].resource_options} == {account.id, other.id}
    selected = await preflight({"channel:0:telegram": account.id})
    assert selected.ready
    assert selected.requirements[0].resource_id == account.id


@pytest.mark.parametrize("channel_type", ["slack", "discord", "twilio_voice", "telegram"])
async def test_rename_cannot_reauthorize_transferred_account(db_session, monkeypatch, channel_type):
    from apps.api.routers import workspaces as routes

    workspace, account, _subscription, _payload = await _scenario(db_session, channel_type=channel_type, bound=True)
    binding = await _binding(db_session, workspace)
    previous_owner = account.owner_user_id
    account.owner_user_id = generate_ulid()
    await db_session.flush()
    before = snapshot_channel_binding(binding)
    assert await load_channel_binding_by_id_for_config(db_session, account, binding.id) is None
    monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
    activity = AsyncMock()
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", activity)
    monkeypatch.setattr(db_session, "commit", db_session.flush)

    with pytest.raises(HTTPException) as error:
        await routes.update_workspace_channel(
            workspace.id, binding.id, routes.WorkspaceChannelUpdateRequest(name="Old owner's rename"),
            SimpleNamespace(id=previous_owner, entity_id=workspace.entity_id), db_session,
        )
    assert error.value.status_code == 403
    assert snapshot_channel_binding(binding) == before
    assert await load_channel_binding_by_id_for_config(db_session, account, binding.id) is None
    activity.assert_not_awaited()


@pytest.mark.parametrize("operation", ["update", "upgrade"])
@pytest.mark.parametrize("legacy_owner", [False, True])
async def test_owner_repair_is_available_to_authorized_actors(db_session, monkeypatch, operation, legacy_owner):
    from apps.api.routers import workspaces as routes

    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    binding = await _binding(db_session, workspace)
    if legacy_owner:
        binding.user_id = None
        actor_id = generate_ulid()  # Another Workspace manager repairing old data.
    else:
        account.owner_user_id = generate_ulid()
        actor_id = account.owner_user_id  # Only the new owner may reauthorize a transfer.
    await db_session.flush()
    if operation == "update":
        monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
        monkeypatch.setattr("packages.core.services.workspace_service.record_activity", AsyncMock())
        monkeypatch.setattr(db_session, "commit", db_session.flush)
        await routes.update_workspace_channel(
            workspace.id, binding.id, routes.WorkspaceChannelUpdateRequest(name="Authorized rename"),
            SimpleNamespace(id=actor_id, entity_id=workspace.entity_id), db_session,
        )
    else:
        await apply(
            db_session, workspace=workspace, payload=payload, source_payload=payload,
            by_user_id=actor_id, channel_config_ids={"channel:0:telegram": account.id},
            require_complete_workspace=True,
        )
    assert binding.user_id == account.owner_user_id
    assert await load_channel_binding_by_id_for_config(db_session, account, binding.id) is binding


@pytest.mark.parametrize("invalid", ["transferred", "unbound", "inactive_binding", "foreign_workspace", "inactive_account"])
async def test_upgrade_does_not_expose_or_reauthorize_unavailable_accounts(db_session, invalid):
    workspace, account, _subscription, payload = await _scenario(db_session, bound=True)
    binding = await _binding(db_session, workspace)
    previous_owner = account.owner_user_id
    if invalid == "transferred":
        account.owner_user_id = generate_ulid()
    elif invalid == "unbound":
        await db_session.delete(binding)
    elif invalid == "inactive_binding":
        binding.status = "inactive"
    elif invalid == "foreign_workspace":
        account.workspace_id = generate_ulid()
    else:
        account.status = "inactive"
    await db_session.flush()
    # For a transfer this is the old owner; otherwise a different manager.
    manager_id = previous_owner if invalid == "transferred" else generate_ulid()
    preflight = await BlueprintSetupPreflightFactory.from_contract(
        db_session, contract=payload["contract"], entity_id=workspace.entity_id,
        user_id=manager_id, workspace_id=workspace.id,
        selected_channel_config_ids={"channel:0:telegram": account.id},
    )
    assert not preflight.ready
    assert preflight.requirements[0].resource_options == ()
    with pytest.raises(InstallError, match="unavailable"):
        await _bind_blueprint_channel_configs(
            db_session, workspace=workspace, user_id=manager_id,
            channel_requirements=payload["contract"]["channels"],
            selected_channel_config_ids={"channel:0:telegram": account.id},
        )


@pytest.mark.parametrize("null_timestamp", [False, True])
@pytest.mark.parametrize("edit_default", [False, True])
async def test_tied_default_route_survives_metadata_upgrade_and_revert(db_session, null_timestamp, edit_default):
    workspace, account, subscription, payload = await _scenario(db_session, bound=True)
    binding = await _binding(db_session, workspace)
    other_workspace = Workspace(entity_id=workspace.entity_id, name="Other workspace", status="active")
    db_session.add(other_workspace)
    await db_session.flush()
    other_subscription = AgentSubscription(
        entity_id=workspace.entity_id, workspace_id=other_workspace.id,
        agent_id=subscription.agent_id, service_key="support", status="active",
    )
    db_session.add(other_subscription)
    await db_session.flush()
    # Set the tie-break IDs explicitly, independently of ULID generation order.
    other = Channel(
        id=("0" if edit_default else "Z") * 26,
        entity_id=workspace.entity_id, workspace_id=other_workspace.id,
        user_id=account.owner_user_id, type="telegram", status="active",
        agent_id=subscription.agent_id, agent_subscription_id=other_subscription.id,
        config={"channel_config_id": account.id},
    )
    db_session.add(other)
    await db_session.flush()
    timestamp = None if null_timestamp else datetime(2025, 1, 1, tzinfo=timezone.utc)
    await db_session.execute(update(Channel).where(
        Channel.id.in_([binding.id, other.id]),
    ).values(updated_at=timestamp))
    before = await load_channel_binding_for_config(db_session, account)
    assert before.id == (binding.id if edit_default else other.id)
    scopes = await load_channel_binding_scopes_for_config(db_session, account)
    assert scopes[0].binding.id == before.id

    payload["contract"]["channels"][0]["label"] = "Metadata rename"
    await _upgrade(db_session, workspace, account, payload)
    assert (await load_channel_binding_for_config(db_session, account)).id == before.id
    await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)
    assert (await load_channel_binding_for_config(db_session, account)).id == before.id
