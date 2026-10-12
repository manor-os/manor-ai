"""Regression coverage for pre-creation gates and personalized upgrade baselines."""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.blueprints.freshness import (
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
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


@pytest.mark.parametrize("status", ["draft", "archived"])
async def test_raw_payload_same_slug_never_adopts_unpublished_platform_source(
    db_session, status,
):
    """Raw JSON has no durable source unless it exactly matches a release."""
    from apps.api.routers.blueprints import _published_platform_blueprint_matching_payload

    payload = _base_payload()
    payload["manifest"]["slug"] = "raw-source-test"
    row = WorkspaceBlueprint(
        id="builtin:raw-source-test", entity_id=None, slug="raw-source-test",
        title="Raw source test", payload=copy.deepcopy(payload), status=status,
    )
    db_session.add(row)
    await db_session.flush()

    assert await _published_platform_blueprint_matching_payload(db_session, payload) is None


async def test_raw_install_stays_unbound_when_a_platform_slug_is_published_later(
    db_session,
):
    """Raw imports must not acquire an upgrade source from a display slug."""
    from apps.api.routers.workspaces import _blueprint_payloads_for

    entity_id = generate_ulid()
    slug = "raw-install-no-source"
    raw_payload = _base_payload()
    raw_payload["manifest"]["slug"] = slug
    raw_payload["recipe"]["operating_model"] = {"context": "Raw local setup"}
    installed = await install_blueprint(
        db_session, entity_id=entity_id, user_id=generate_ulid(),
        payload=raw_payload, mode=InstallMode.SIMULATE, blueprint_slug=slug,
    )
    workspace = await db_session.get(Workspace, installed.workspace_id)
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY]["source"] == "raw_payload"

    published_payload = copy.deepcopy(raw_payload)
    published_payload["recipe"]["operating_model"] = {"context": "Published later"}
    db_session.add(WorkspaceBlueprint(
        id=f"builtin:{slug}", entity_id=None, slug=slug,
        title="Published after raw import", payload=published_payload,
        status="published",
    ))
    await db_session.flush()

    assert await _blueprint_payloads_for(db_session, [workspace]) == {}


@pytest.mark.parametrize("source", [None, "marketplace"])
async def test_legacy_null_id_record_stays_unbound(
    db_session, source,
):
    """No source marker can prove a null-ID record's release provenance."""
    from apps.api.routers.workspaces import (
        _blueprint_payloads_for,
        _workspace_blueprint_id,
    )

    slug = "legacy-raw-source-collision"
    raw_payload = _base_payload()
    raw_payload["manifest"]["slug"] = slug
    workspace = Workspace(
        entity_id=generate_ulid(),
        name="Legacy raw import",
        settings={BLUEPRINT_SETTINGS_KEY: {
            "blueprint_id": None, "blueprint_slug": slug,
            **({"source": source} if source is not None else {}),
        }},
    )
    published = WorkspaceBlueprint(
        id=f"builtin:{slug}", entity_id=None, slug=slug,
        title="Unrelated published release", payload=_base_payload(), status="published",
    )
    db_session.add_all([workspace, published])
    await db_session.flush()

    assert _workspace_blueprint_id(workspace.settings) == ""
    assert await _blueprint_payloads_for(db_session, [workspace]) == {}


@pytest.mark.parametrize("edited_section", ["recipe", "manifest"])
async def test_install_payload_binds_only_an_exact_published_platform_release(
    client: AsyncClient,
    db_session,
    edited_section,
):
    """Raw HTTP imports record provenance only for an exact public release."""
    from packages.core.models.user import Entity
    from test_document_permissions import _create_entity_user

    slug = "exact-public-release"
    release_id = f"builtin:{slug}"
    release_payload = _base_payload(**{
        "manifest.slug": slug,
        "recipe.operating_model": {"context": "Published release"},
    })
    entity = Entity(name="Exact public payload", plan_id="plan_pro")
    db_session.add_all([
        entity,
        WorkspaceBlueprint(
            id=release_id,
            entity_id=None,
            slug=slug,
            title="Exact public release",
            payload=copy.deepcopy(release_payload),
            content_version="2.4.6",
            status="published",
        ),
    ])
    await db_session.commit()

    actor = await _create_entity_user(
        entity.id,
        "exact_public_payload",
        role="owner",
    )
    headers = actor["headers"]
    exact = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": release_payload, "mode": "simulate"},
    )
    assert exact.status_code == 201, exact.text
    assert exact.json()["blueprint_id"] == release_id

    exact_workspace = await db_session.get(Workspace, exact.json()["workspace_id"])
    exact_record = exact_workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert exact_record["blueprint_id"] == release_id
    assert exact_record["source"] == "marketplace"
    assert exact_record[BLUEPRINT_VERSION_KEY] == "2.4.6"

    edited_payload = copy.deepcopy(release_payload)
    if edited_section == "manifest":
        edited_payload["manifest"]["kind"] = "edited-kind"
    else:
        edited_payload["recipe"]["operating_model"] = {"context": "Edited raw import"}
    edited = await client.post(
        "/api/v1/blueprints/install-payload",
        headers=headers,
        json={"payload": edited_payload, "mode": "simulate"},
    )
    assert edited.status_code == 201, edited.text
    assert edited.json()["blueprint_id"] is None

    edited_workspace = await db_session.get(Workspace, edited.json()["workspace_id"])
    assert edited_workspace.kind == edited_payload["manifest"]["kind"]
    edited_record = edited_workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert edited_record["blueprint_id"] is None
    assert edited_record["blueprint_slug"] == slug
    assert edited_record["source"] == "raw_payload"
    assert edited_record[BLUEPRINT_VERSION_KEY] is None


async def test_payload_preflight_validates_typed_setup_without_creating_workspace(
    client: AsyncClient,
):
    """The preview is read-only, and install repeats its account validation."""
    from test_document_permissions import _auth

    headers = await _auth(client, "payload_preflight_contract")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    user = me.json()
    payload = _base_payload(**{
        "contract.variables": [
            {"key": "workspace_name", "label": "Workspace name", "required": True},
            {"key": "team_size", "label": "Team size", "default": 1},
        ],
        "contract.channels": [{
            "channel_type": "telegram", "provider": "telegram", "required": True,
        }],
    })
    async with db_module.async_session() as db:
        account = ChannelConfig(
            entity_id=user["entity_id"], owner_user_id=user["id"],
            channel_type="telegram", provider="telegram", status="active",
        )
        db.add(account)
        await db.commit()
        account_id = account.id

    missing = await client.post(
        "/api/v1/blueprints/install-payload-preflight", headers=headers,
        json={"payload": payload, "mode": "live"},
    )
    assert missing.status_code == 400, missing.text

    selected = {"channel:0:telegram": account_id}
    preview = await client.post(
        "/api/v1/blueprints/install-payload-preflight", headers=headers,
        json={"payload": payload, "mode": "live", "variable_values": {"workspace_name": "Support", "team_size": 2}, "channel_config_ids": selected},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["ready"] is True
    assert preview.json()["requirements"][0]["resource_id"] == account_id

    async with db_module.async_session() as db:
        account = await db.get(ChannelConfig, account_id)
        account.status = "inactive"
        await db.commit()

    install = await client.post(
        "/api/v1/blueprints/install-payload", headers=headers,
        json={"payload": payload, "mode": "live", "variable_values": {"workspace_name": "Support", "team_size": 2}, "channel_config_ids": selected},
    )
    assert install.status_code == 409, install.text
    async with db_module.async_session() as db:
        assert not (await db.execute(select(Workspace.id).where(
            Workspace.entity_id == user["entity_id"],
        ))).scalars().all()


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
