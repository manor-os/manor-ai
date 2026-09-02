"""Upgrade compatibility and channel regressions from the final review."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.blueprints.freshness import (
    BLUEPRINT_SETTINGS_KEY,
    CONTENT_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    blueprint_content_fingerprint,
    blueprint_upgrade_unsupported_fingerprint,
)
from packages.core.blueprints.installer import (
    _bind_blueprint_channel_configs,
    resolve_install_variables,
    sync_workspace_live_setup_requirements,
)
from packages.core.blueprints.upgrade import BlueprintUpgradePlanChangedError, apply, plan, revert
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.models.document import Channel
from packages.core.services.channel_bindings import channel_runtime_config
from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup
from test_blueprint_review_regressions import channel_fixture


def _payload():
    return {
        "manifest": {"blueprint_version": "1.1", "name": "Review"},
        "contract": {"channels": [], "variables": []},
        "embedded": {"agents": [], "skills": [], "knowledge_packs": []},
        "recipe": {"workflows": []},
        "policy": {},
    }


def _legacy_fingerprint(payload):
    """Frozen pre-channel-upgrade digest for these component-free fixtures."""
    assert not any(payload["embedded"].values())
    assert not payload["recipe"].get("workflows")
    material = {
        key: payload[key] for key in ("contract", "embedded", "recipe", "policy")
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def _settings(payload, *, legacy=False):
    return {BLUEPRINT_SETTINGS_KEY: {
        "blueprint_slug": "review",
        "blueprint_version": "1.0.0",
        "variable_declarations": copy.deepcopy(payload["contract"]["variables"]),
        CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(payload),
        UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
            _legacy_fingerprint(payload) if legacy
            else blueprint_upgrade_unsupported_fingerprint(payload)
        ),
    }}


def test_legacy_digest_fixture_matches_the_pre_channel_upgrade_format():
    assert _legacy_fingerprint(_payload()) == "6a186e03a648b89c8b324aa66ef0e680"


@pytest.mark.parametrize("channels", [[], [{"channel_type": "telegram", "required": True}]])
@pytest.mark.parametrize("recorded_materialized", [False, True])
async def test_legacy_channel_digest_accepts_identical_content(channels, recorded_materialized):
    source = _payload()
    source["contract"]["channels"] = channels
    settings = _settings(source, legacy=True)
    if recorded_materialized:
        settings[BLUEPRINT_SETTINGS_KEY][MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = (
            _legacy_fingerprint(source)
        )
    before = copy.deepcopy(settings)
    workspace = SimpleNamespace(id="workspace", entity_id="entity", settings=settings)

    preview = await plan(AsyncMock(), workspace=workspace, payload=source)

    assert preview["unsupported_changes"] is False
    assert preview["materialized_unsupported_baseline"] == blueprint_upgrade_unsupported_fingerprint(source)
    assert workspace.settings == before  # Preview must not migrate persisted state.


@pytest.mark.parametrize("recorded_materialized", [False, True])
@pytest.mark.parametrize("changed", ["none", "declaration", "personalization", "shell"])
async def test_legacy_channel_digest_keeps_personalization_boundary(recorded_materialized, changed):
    source = _payload()
    source["contract"]["variables"] = [{
        "key": "brand", "default": "DefaultCo", "required": True, "materialize": True,
    }]
    source["recipe"]["operating_model"] = {"context": "Operate {{brand}}"}
    previous, _ = resolve_install_variables(source, {"brand": "OldCo"})
    settings = _settings(source, legacy=True)
    settings["blueprint_personalization"] = {"brand": "OldCo"}
    if recorded_materialized:
        settings[BLUEPRINT_SETTINGS_KEY][MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = (
            _legacy_fingerprint(previous)
        )
    current = copy.deepcopy(source)
    if changed == "declaration":
        current["contract"]["variables"][0]["default"] = "NewDefaultCo"
    elif changed == "shell":
        current["recipe"]["operating_model"]["context"] = "Different operating model"
    resolved, _ = resolve_install_variables(
        current, {"brand": "NewCo" if changed == "personalization" else "OldCo"},
    )
    workspace = SimpleNamespace(id="workspace", entity_id="entity", settings=settings)

    preview = await plan(AsyncMock(), workspace=workspace, payload=resolved, source_payload=current)

    assert preview["unsupported_changes"] is (changed in {"personalization", "shell"})


async def test_legacy_digest_is_backfilled_only_after_confirmed_upgrade(db_session):
    source = _payload()
    workspace, _account, _subscription = await channel_fixture(db_session, "telegram")
    workspace.settings = _settings(source, legacy=True)
    await db_session.flush()
    result = await apply(db_session, workspace=workspace, payload=source, current_version="1.0.1")
    await db_session.refresh(workspace)

    record = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert result["fully_synchronized"] is True
    assert record["blueprint_version"] == "1.0.1"
    assert record[UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] == blueprint_upgrade_unsupported_fingerprint(source)
    assert record[MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] == blueprint_upgrade_unsupported_fingerprint(source)


@pytest.mark.parametrize("explicit_selection", [False, True])
async def test_upgrade_api_binds_resolved_channel_and_preserves_live_guard(db_session, monkeypatch, explicit_selection):
    from apps.api.routers import workspaces as routes

    older = _payload()
    current = copy.deepcopy(older)
    current["contract"]["channels"] = [{
        "channel_type": "telegram", "required": True, "linked_service_key": "support",
    }]
    workspace, account, subscription = await channel_fixture(db_session, "telegram")
    workspace.settings = _settings(older)
    if explicit_selection:
        db_session.add(ChannelConfig(
            entity_id=workspace.entity_id, owner_user_id=account.owner_user_id,
            channel_type="telegram", provider="telegram", status="active", name="Other account",
        ))
    await db_session.flush()
    monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr(routes, "_blueprint_payloads_for", AsyncMock(return_value={
        workspace.id: (current, "1.0.1", None),
    }))
    user = SimpleNamespace(id=account.owner_user_id, entity_id=workspace.entity_id)
    request = routes.BlueprintUpgradeApplyRequest(
        expected_blueprint_fingerprint=blueprint_content_fingerprint(current),
        **({"channel_config_ids": {"channel:0:telegram": account.id}} if explicit_selection else {}),
    )

    for _ in range(2):  # Repeating confirmation cannot create a second route.
        result = await routes.apply_blueprint_upgrade(workspace.id, request, user, db_session)
        assert result["fully_synchronized"] is True
    binding = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ))).scalar_one()
    assert binding.config["channel_config_id"] == account.id
    assert binding.agent_subscription_id == subscription.id
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is None or not readiness.blocks_work
    account.status = "inactive"
    await db_session.flush()
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is not None and readiness.blocks_work


@pytest.mark.parametrize("channel_type", ["telegram", "slack", "discord"])
async def test_updating_channel_keeps_non_blueprint_runtime_config(db_session, channel_type):
    workspace, account, subscription = await channel_fixture(db_session, channel_type)
    binding = Channel(
        entity_id=workspace.entity_id, workspace_id=workspace.id, user_id=account.owner_user_id,
        type=channel_type, status="active", agent_id=subscription.agent_id,
        agent_subscription_id=subscription.id,
        config={
            "channel_config_id": account.id, "linked_service_key": "support",
            "language": "zh", "role": "primary_external", "purpose": "Old purpose",
            "operator_options": {"labels": ["customer", "support"]},
        },
    )
    db_session.add(binding)
    await db_session.flush()
    original_account_config = copy.deepcopy(account.config)

    await _bind_blueprint_channel_configs(
        db_session, workspace=workspace, user_id=account.owner_user_id,
        channel_requirements=[{
            "channel_type": channel_type, "linked_service_key": "support", "purpose": "New purpose",
        }],
        selected_channel_config_ids={f"channel:0:{channel_type}": account.id},
    )
    await db_session.refresh(binding)

    assert channel_runtime_config(account, binding)["language"] == "zh"
    assert binding.config["operator_options"] == {"labels": ["customer", "support"]}
    assert binding.config["purpose"] == "New purpose"
    assert binding.config["linked_service_key"] == "support"
    assert account.config == original_account_config


async def _prepare_channel_upgrade(db, *, existing, channel_type="telegram"):
    workspace, account, subscription = await channel_fixture(db, channel_type)
    older = _payload()
    requirement = {
        "channel_type": channel_type, "required": True,
        "linked_service_key": "support", "purpose": "Old purpose",
    }
    if existing:
        older["contract"]["channels"] = [requirement]
        db.add(Channel(
            entity_id=workspace.entity_id, workspace_id=workspace.id,
            user_id=account.owner_user_id, type=channel_type, name="Original route",
            status="active", agent_id=subscription.agent_id,
            agent_subscription_id=subscription.id,
            config={"channel_config_id": account.id, "linked_service_key": "support",
                    "purpose": "Old purpose", "role": "primary_external", "language": "zh"},
        ))
    workspace.settings = _settings(older)
    await db.flush()
    await sync_workspace_live_setup_requirements(db, workspace=workspace, payload=older)
    await db.flush()
    before = copy.deepcopy(workspace.settings[BLUEPRINT_SETTINGS_KEY])
    current = copy.deepcopy(older)
    current["contract"]["channels"] = [{**requirement, "purpose": "New purpose", "label": "New route"}]
    return workspace, account, current, before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("channel_type", ["telegram", "slack", "discord"])
async def test_channel_only_upgrade_reverts_binding_and_live_contract(db_session, existing, channel_type):
    workspace, account, current, before = await _prepare_channel_upgrade(
        db_session, existing=existing, channel_type=channel_type,
    )
    account_before = copy.deepcopy(account.config)
    account.credentials = {"test_credential": "retained"}
    history = MessageLog(
        entity_id=workspace.entity_id, channel_config_id=account.id,
        channel_type=channel_type, direction="inbound", content="Existing history",
    )
    db_session.add(history)
    await db_session.flush()
    result = await apply(
        db_session, workspace=workspace, payload=current, current_version="1.0.1",
        by_user_id=account.owner_user_id,
        channel_config_ids={f"channel:0:{channel_type}": account.id},
    )
    assert result["can_revert"] is True
    assert result["updated"][0]["kind"] == "channel"

    result = await revert(db_session, workspace=workspace, by_user_id=account.owner_user_id)

    assert result["reverted"][0]["kind"] == "channel"
    binding = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ))).scalar_one_or_none()
    if existing:
        assert binding.name == "Original route"
        assert binding.config["purpose"] == "Old purpose"
        assert binding.config["language"] == "zh"
    else:
        assert binding is None
    record = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    for key in ("live_setup_requirements", "install_todos", "blocking_todo_count",
                "blueprint_version", CONTENT_FINGERPRINT_KEY):
        assert record[key] == before[key]
    assert await db_session.get(ChannelConfig, account.id) is account
    assert account.config == account_before
    assert account.credentials == {"test_credential": "retained"}
    assert await db_session.get(MessageLog, history.id) is history
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is None or not readiness.blocks_work
    assert (await revert(db_session, workspace=workspace))["reverted"] == []


@pytest.mark.parametrize("edit", ["binding", "setup", "workspace"])
async def test_channel_revert_rejects_later_edits_before_any_mutation(db_session, edit):
    workspace, account, current, _before = await _prepare_channel_upgrade(db_session, existing=True)
    await apply(
        db_session, workspace=workspace, payload=current, by_user_id=account.owner_user_id,
        channel_config_ids={"channel:0:telegram": account.id},
    )
    binding = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ))).scalar_one()
    if edit == "binding":
        binding.config = {**binding.config, "language": "fr"}
    elif edit == "workspace":
        binding.workspace_id = generate_ulid()
    else:
        settings = copy.deepcopy(workspace.settings)
        settings[BLUEPRINT_SETTINGS_KEY]["live_setup_requirements"][0]["payload"]["purpose"] = "Operator purpose"
        workspace.settings = settings
    await db_session.flush()
    settings_before = copy.deepcopy(workspace.settings)
    binding_before = copy.deepcopy(binding.config)

    with pytest.raises(BlueprintUpgradePlanChangedError):
        await revert(db_session, workspace=workspace)

    assert workspace.settings == settings_before
    assert binding.config == binding_before


async def test_channel_revert_preserves_legacy_missing_setup_guard(db_session):
    workspace, account, current, _before = await _prepare_channel_upgrade(db_session, existing=False)
    settings = copy.deepcopy(workspace.settings)
    for key in ("live_setup_requirements", "install_todos", "blocking_todo_count"):
        settings[BLUEPRINT_SETTINGS_KEY].pop(key)
    workspace.settings = settings
    await db_session.flush()
    await apply(
        db_session, workspace=workspace, payload=current, by_user_id=account.owner_user_id,
        channel_config_ids={"channel:0:telegram": account.id},
    )

    await revert(db_session, workspace=workspace)

    for key in ("live_setup_requirements", "install_todos", "blocking_todo_count"):
        assert key not in workspace.settings[BLUEPRINT_SETTINGS_KEY]
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is not None and readiness.blocks_work
    await apply(
        db_session, workspace=workspace, payload=current, by_user_id=account.owner_user_id,
        channel_config_ids={"channel:0:telegram": account.id},
    )
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is None or not readiness.blocks_work


async def test_channel_revert_preflights_all_bindings_before_removing_any(db_session):
    workspace, account, current, _before = await _prepare_channel_upgrade(db_session, existing=False)
    second_account = ChannelConfig(
        entity_id=workspace.entity_id, owner_user_id=account.owner_user_id,
        channel_type="slack", provider="slack", status="active",
    )
    db_session.add(second_account)
    await db_session.flush()
    current["contract"]["channels"].append({
        "channel_type": "slack", "required": True, "linked_service_key": "support",
    })
    await apply(
        db_session, workspace=workspace, payload=current, by_user_id=account.owner_user_id,
        channel_config_ids={"channel:0:telegram": account.id, "channel:1:slack": second_account.id},
    )
    bindings = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ).order_by(Channel.id))).scalars().all()
    edited = next(binding for binding in bindings if binding.type == "slack")
    edited.config = {**edited.config, "language": "fr"}
    await db_session.flush()

    with pytest.raises(BlueprintUpgradePlanChangedError):
        await revert(db_session, workspace=workspace)

    # The first pristine route must not be deleted before detecting the later conflict.
    assert (await db_session.execute(select(Channel.id).where(
        Channel.workspace_id == workspace.id,
    ).order_by(Channel.id))).scalars().all() == [binding.id for binding in bindings]


async def test_repeated_channel_upgrade_keeps_original_undo_point(db_session):
    workspace, account, current, before = await _prepare_channel_upgrade(db_session, existing=False)
    for _ in range(2):
        result = await apply(
            db_session, workspace=workspace, payload=current, current_version="1.0.1",
            by_user_id=account.owner_user_id, channel_config_ids={"channel:0:telegram": account.id},
        )
        assert result["can_revert"] is True
    await revert(db_session, workspace=workspace)
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][CONTENT_FINGERPRINT_KEY] == before[CONTENT_FINGERPRINT_KEY]
    assert (await db_session.execute(select(Channel.id).where(
        Channel.workspace_id == workspace.id,
    ))).scalars().all() == []
