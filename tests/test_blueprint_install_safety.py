"""Regression coverage for pre-creation gates and personalized upgrade baselines."""
from __future__ import annotations

import copy
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
    InstallError,
    InstallMode,
    install_blueprint,
    resolve_install_variables,
)
from packages.core.blueprints.upgrade import apply, plan, revert
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_draft_service
from packages.core.services.workspace_setup_service import DEFAULT_FIELDS
from test_blueprint_draft_capabilities import _payload
from test_blueprint_installer_workflows import _base_payload

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("materialize", [True, False])
async def test_legacy_partial_upgrade_cannot_adopt_new_values_as_old_baseline(db_session, materialize):
    entity_id = generate_ulid()
    source = _base_payload(**{
        "contract.variables": [{
            "key": "brand", "default": "DefaultCo", "required": True,
            "materialize": materialize,
        }],
        "recipe.operating_model": {"context": "Operate {{brand}}"},
    })
    workspace = Workspace(
        entity_id=entity_id, name="Installed", operating_context="Operate OldCo",
        settings={
            **({"blueprint_personalization": {"brand": "OldCo"}} if materialize else {}),
            BLUEPRINT_SETTINGS_KEY: {
                "blueprint_id": "builtin:baseline-test",
                "variable_declarations": copy.deepcopy(source["contract"]["variables"]),
                CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(source),
                UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: blueprint_upgrade_unsupported_fingerprint(source),
            },
        },
    )
    db_session.add(workspace)
    await db_session.flush()
    resolved, personalization = resolve_install_variables(source, {"brand": "NewCo"})
    first = await plan(db_session, workspace=workspace, payload=resolved, source_payload=source)
    assert first["unsupported_changes"]
    assert first["unsupported_baseline_unknown"] is (not materialize)
    result = await apply(
        db_session, workspace=workspace, payload=resolved, source_payload=source,
        personalization=personalization, current_version="1.0.1",
    )
    assert not result["fully_synchronized"]
    second = await plan(db_session, workspace=workspace, payload=resolved, source_payload=source)
    assert second["unsupported_changes"]
    assert workspace.operating_context == "Operate OldCo"
    if materialize:
        old_resolved, _ = resolve_install_variables(source, {"brand": "OldCo"})
        assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
            MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY
        ] == blueprint_upgrade_unsupported_fingerprint(old_resolved)
        await revert(db_session, workspace=workspace)
        assert workspace.settings["blueprint_personalization"] == {"brand": "OldCo"}
        assert MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY not in workspace.settings[BLUEPRINT_SETTINGS_KEY]


@pytest.mark.parametrize("failure", ["missing", "inactive", "other_user", "wrong_provider", "occupied_slack"])
async def test_channel_rejection_leaves_no_workspace_rows(db_session, failure):
    entity_id, user_id = generate_ulid(), generate_ulid()
    channel_type = "slack" if failure == "occupied_slack" else "telegram"
    account = ChannelConfig(
        entity_id=entity_id, owner_user_id=generate_ulid() if failure == "other_user" else user_id,
        channel_type=channel_type, provider="wrong" if failure == "wrong_provider" else channel_type,
        status="inactive" if failure == "inactive" else "active",
    )
    db_session.add(account)
    await db_session.flush()
    if failure == "occupied_slack":
        db_session.add(Channel(
            entity_id=entity_id, workspace_id=generate_ulid(), type="slack",
            config={"channel_config_id": account.id}, status="active",
        ))
        await db_session.flush()
    payload = _base_payload(**{"contract.channels": [{
        "channel_type": channel_type, "provider": channel_type, "required": True,
    }]})
    selections = {f"channel:0:{channel_type}": account.id}
    if failure == "missing":
        account.status = "inactive"
        await db_session.flush()
        selections = {}
    with pytest.raises(InstallError):
        await install_blueprint(
            db_session, entity_id=entity_id, user_id=user_id, payload=payload,
            mode=InstallMode.LIVE,
            channel_config_ids=selections,
        )
    assert not (await db_session.execute(
        select(Workspace.id).where(Workspace.entity_id == entity_id)
    )).scalars().all()


async def test_draft_binds_selected_account_without_creating_credentialless_duplicate(db_session, monkeypatch):
    entity_id, user_id = generate_ulid(), generate_ulid()
    payload = _payload(agent_slug=f"agent-{entity_id}", skill_slug=f"skill-{entity_id}", server_slug="unused")
    payload["contract"]["requires"]["mcp_servers"] = []
    payload["embedded"]["agents"][0]["mcp_bindings"] = []
    payload["contract"]["channels"] = [{
        "channel_type": "telegram", "provider": "telegram_bot", "required": True,
        "purpose": "Customer support",
    }]
    blueprint = WorkspaceBlueprint(
        entity_id=entity_id, slug=f"test-{entity_id}", title="Test", payload=payload,
        payload_version="1.1", status="published",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, fields=copy.deepcopy(DEFAULT_FIELDS),
        messages=[], missing=[], ready=False, status="active",
    )
    accounts = [ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type="telegram",
        provider="telegram_bot", name=f"Bot {index}", status="active",
    ) for index in range(2)]
    db_session.add_all([blueprint, draft, *accounts])
    await db_session.flush()
    monkeypatch.setattr(workspace_draft_service, "runtime_lint_workspace_draft", AsyncMock(
        return_value={"ok": True, "issues": []},
    ))
    await workspace_draft_service.apply_blueprint(
        db_session, draft_id=draft.id, entity_id=entity_id,
        user_id=user_id, blueprint_id=blueprint.id,
    )
    assert not draft.ready
    workspace_draft_service.apply_public_field_updates(draft, {
        "blueprint_channel_config_ids": {"channel:0:telegram": accounts[1].id},
    })
    await workspace_draft_service._refresh_missing_from_lint(db_session, draft)
    assert draft.ready
    workspace_id, _ = await workspace_draft_service.finalize_draft(
        db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id,
    )
    channels = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace_id, Channel.type == "telegram",
    ))).scalars().all()
    assert len(channels) == 1
    assert channels[0].config["channel_config_id"] == accounts[1].id
    subscription = await db_session.get(AgentSubscription, channels[0].agent_subscription_id)
    assert subscription.workspace_id == workspace_id
    assert not (await db_session.execute(select(ChannelConfig.id).where(
        ChannelConfig.workspace_id == workspace_id, ChannelConfig.channel_type == "telegram",
    ))).scalars().all()
