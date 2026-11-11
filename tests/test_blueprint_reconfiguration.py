"""Protocol 3 must upgrade the original Workspace, or leave it untouched."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.blueprints import reconfigure
from packages.core.blueprints.exporter import export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint, resolve_install_variables
from packages.core.blueprints.upgrade import BlueprintUpgradePlanChangedError
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState
from packages.core.models.document import DocumentGroup
from packages.core.models.skill import Skill
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace


def payload():
    return {
        "manifest": {"blueprint_version": "1.1", "title": "Test studio", "slug": "upgrade-test", "kind": "custom"},
        "contract": {"variables": [], "channels": [], "sessions": [],
                     "requires": {"manor_min_version": None, "tools": [], "mcp_servers": [], "skills": [], "agents": []}},
        "embedded": {"skills": [{"slug": "writer", "system_prompt": "Old writing prompt"}], "agents": [], "knowledge_packs": []},
        "recipe": {"operating_model": {"context": "Old context", "heartbeat_enabled": False},
                   "strategist": None, "prompts": [], "subscriptions": [], "scheduled_jobs": [], "workflows": [],
                   "goals": [], "task_categories": [], "custom_fields": [], "sla_policies": [], "escalation_rules": []},
        "policy": {"governance": {}, "post_install_checks": [], "expected_baseline": None},
    }


async def installed(db):
    source = payload()
    result = await install_blueprint(db, entity_id=generate_ulid(), payload=source,
                                     mode=InstallMode.LIVE, blueprint_id="builtin:upgrade-test")
    workspace = await db.get(Workspace, result.workspace_id)
    return workspace, source


async def preview(db, workspace, source):
    return await reconfigure.plan(db, workspace=workspace, payload=source, source_payload=source,
                                   current_version="2", source_blueprint_id="builtin:upgrade-test")


async def apply_reviewed(db, workspace, source, reviewed=None, *, keep=()):
    reviewed = reviewed or await preview(db, workspace, source)
    return await reconfigure.apply(
        db, workspace=workspace, payload=source, source_payload=source, by_user_id=None, actor=None,
        current_version="2", source_blueprint_id="builtin:upgrade-test",
        expected_blueprint_fingerprint=reviewed["blueprint_fingerprint"],
        expected_plan_fingerprint=reviewed["plan_fingerprint"],
        configuration_resolutions=[{"key": item["key"], "resolution": "keep_yours" if item["key"] in keep else "use_blueprint"}
                                   for item in reviewed["items"]],
    )


def _mcp_requirement(source, *, required, install_blocking):
    source["contract"]["requires"]["mcp_servers"] = [{
        "slug": "optional-reconfigure-mcp",
        "required": required,
        "install_blocking": install_blocking,
    }]


async def _export_mcp_requirement(db, workspace):
    exported = await export_workspace(db, workspace.id, title="Runtime receipt")
    return exported["contract"]["requires"]["mcp_servers"]


def test_only_consumed_or_materialized_variables_are_requested():
    source = payload()
    source["contract"]["variables"] = [
        {"key": "dead", "required": True}, {"key": "business_name", "default": "Company"},
        {"key": "operator_timezone", "default": "PST"},
        {"key": "title", "default": "My studio"}, {"key": "profile", "materialize": True, "default": "local"},
    ]
    source["recipe"]["operating_model"]["context"] = "For {{title}}"
    filtered = reconfigure.used_variable_payload(source)
    assert [v["key"] for v in filtered["contract"]["variables"]] == ["title", "profile"]
    effective, saved = resolve_install_variables(filtered, {})
    assert effective["recipe"]["operating_model"]["context"] == "For My studio"
    assert saved == {"profile": "local"}
    assert len(source["contract"]["variables"]) == 5


@pytest.mark.oss_regression
async def test_runtime_payload_stays_structurally_valid(
    monkeypatch,
) -> None:
    from packages.core.blueprints.payload import validate_payload

    source = payload()
    workspace = SimpleNamespace(settings={"_blueprint": {}})
    monkeypatch.setattr(reconfigure, "_state", AsyncMock(return_value=None))

    retained = await reconfigure.retained_runtime_payload(
        None,
        workspace=workspace,
        payload=source,
    )

    validate_payload(retained)




def test_custom_runtime_acknowledgment_does_not_repeat_upstream_change():
    source = payload()
    target = reconfigure.runtime_contract(source)
    workspace = SimpleNamespace(settings={"_blueprint": {"upgrade_runtime_contract": {"mine": True}}})
    item = reconfigure._runtime_item(workspace, source, {"runtime": {"source": target, "installed": {"mine": True}}})
    assert item["action"] == "unchanged"
    assert item["current_content"] != item["new_content"]


@pytest.mark.parametrize("resolutions", [[], [{"key": "bad", "resolution": "use_blueprint"}],
    [{"key": "a", "resolution": "use_blueprint"}, {"key": "a", "resolution": "keep_yours"}]])
def test_choices_are_complete_and_unambiguous(resolutions):
    with pytest.raises(BlueprintUpgradePlanChangedError):
        reconfigure._choices([{"key": "a", "action": "update"}], resolutions)


async def test_upgrade_and_undo_preserve_identity_history_and_new_local_assets(db_session):
    workspace, source = await installed(db_session)
    original_id = workspace.id
    original_name = workspace.name
    task = Task(entity_id=workspace.entity_id, workspace_id=workspace.id, title="Previous successful video", status="completed")
    db_session.add(task)
    await db_session.flush()
    source["recipe"]["operating_model"]["context"] = "New context"
    source["embedded"]["skills"][0]["system_prompt"] = "New writing prompt"
    reviewed = await preview(db_session, workspace, source)
    assert reviewed["baseline_known"] is True
    result = await apply_reviewed(db_session, workspace, source, reviewed)
    assert result["synchronization_status"] == "current", [(i["key"], i["action"], i["current_content"], i["new_content"]) for i in reviewed["items"] if i["current_content"] != i["new_content"]]
    assert (workspace.id, workspace.name, workspace.operating_context) == (original_id, original_name, "New context")
    assert (await preview(db_session, workspace, source))["items"]
    assert all(i["action"] == "unchanged" for i in (await preview(db_session, workspace, source))["items"])
    workspace.settings = {**workspace.settings, "workspace_shared_assets": {"narrator": "created-after-upgrade.wav"}}
    await db_session.flush()
    undo = await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    assert undo["can_revert"] is False
    assert workspace.operating_context == "Old context"
    assert workspace.settings["workspace_shared_assets"]["narrator"] == "created-after-upgrade.wav"
    assert (await db_session.get(Task, task.id)).status == "completed"
    assert workspace.id == original_id
    with pytest.raises(BlueprintUpgradePlanChangedError):
        await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)


@pytest.mark.parametrize("legacy_receipt", [False, True])
async def test_reviewed_upgrade_refreshes_guide_and_undo_tracks_only_owned_metadata(db_session, legacy_receipt):
    workspace, source = await installed(db_session)
    old_guide = workspace.settings["_blueprint"].get("usage_guide")
    source["manifest"]["usage_guide"] = "Review the product announcement before publishing."
    reviewed = await preview(db_session, workspace, source)
    original_guide = source["manifest"]["usage_guide"]
    source["manifest"]["usage_guide"] = "Changed after preview."
    with pytest.raises(BlueprintUpgradePlanChangedError):
        await apply_reviewed(db_session, workspace, source, reviewed)
    source["manifest"]["usage_guide"] = original_guide
    await apply_reviewed(db_session, workspace, source, reviewed)
    assert workspace.settings["_blueprint"]["usage_guide"] == source["manifest"]["usage_guide"]
    upgraded = copy.deepcopy(workspace.settings)
    changed = copy.deepcopy(upgraded)
    changed["_blueprint"]["usage_guide"] = "Later operator guide."
    workspace.settings = changed
    await db_session.flush()
    if legacy_receipt:
        state = await db_session.get(WorkspaceBlueprintUpgradeState, workspace.id)
        receipt = copy.deepcopy(state.restore_point)
        for side in ("metadata_before", "metadata_after"):
            receipt[side].pop("usage_guide", None)
        state.restore_point = receipt
        await db_session.flush()
    else:
        with pytest.raises(BlueprintUpgradePlanChangedError):
            await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
        workspace.settings = upgraded
        await db_session.flush()
    await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    assert workspace.settings["_blueprint"].get("usage_guide") == ("Later operator guide." if legacy_receipt else old_guide)


async def test_active_task_blocks_apply_without_cancelling_it(db_session):
    workspace, source = await installed(db_session)
    task = Task(entity_id=workspace.entity_id, workspace_id=workspace.id, title="Rendering now", status="in_progress")
    db_session.add(task)
    await db_session.flush()
    reviewed = await preview(db_session, workspace, source)
    assert any(item["id"] == task.id for item in reviewed["active_runs"])
    with pytest.raises(BlueprintUpgradePlanChangedError, match="active executions"):
        await apply_reviewed(db_session, workspace, source, reviewed)
    await db_session.refresh(task)
    assert task.status == "in_progress"


async def test_preview_cas_rejects_local_edit_before_any_component_write(db_session):
    workspace, source = await installed(db_session)
    source["embedded"]["skills"][0]["system_prompt"] = "New writing prompt"
    reviewed = await preview(db_session, workspace, source)
    workspace.operating_context = "My later edit"
    await db_session.flush()
    with pytest.raises(BlueprintUpgradePlanChangedError, match="changed"):
        await apply_reviewed(db_session, workspace, source, reviewed)
    skill = (await db_session.execute(select(Skill).where(Skill.slug == "writer"))).scalar_one()
    assert skill.system_prompt == "Old writing prompt"


async def test_failed_configuration_write_rolls_back_components(db_session, monkeypatch):
    from packages.core.blueprints import configuration_upgrade
    workspace, source = await installed(db_session)
    source["embedded"]["skills"][0]["system_prompt"] = "Must roll back"
    monkeypatch.setattr(configuration_upgrade, "apply_configuration", AsyncMock(side_effect=ValueError("injected verifier failure")))
    with pytest.raises(ValueError, match="injected"):
        await apply_reviewed(db_session, workspace, source)
    skill = (await db_session.execute(select(Skill).where(Skill.slug == "writer"))).scalar_one()
    assert skill.system_prompt == "Old writing prompt"


async def test_legacy_keep_choice_establishes_real_baseline_without_replacing_workspace(db_session):
    workspace, source = await installed(db_session)
    state = await db_session.get(WorkspaceBlueprintUpgradeState, workspace.id)
    await db_session.delete(state)
    workspace.operating_context = "My existing production workspace"
    await db_session.flush()
    reviewed = await preview(db_session, workspace, source)
    assert reviewed["baseline_known"] is False
    conflicts = [i["key"] for i in reviewed["items"] if i["action"] == "keep_yours"]
    assert conflicts
    result = await apply_reviewed(db_session, workspace, source, reviewed, keep=conflicts)
    assert result["synchronization_status"] == "customized"
    assert workspace.operating_context == "My existing production workspace"
    again = await preview(db_session, workspace, source)
    assert all(i["action"] == "unchanged" for i in again["items"])


async def test_second_upgrade_does_not_overwrite_previously_kept_runtime(db_session):
    workspace, source = await installed(db_session)
    before = reconfigure.runtime_contract(source)
    source["contract"]["requires"]["tools"] = ["new_optional_for_test"]
    await apply_reviewed(db_session, workspace, source, keep=(reconfigure.RUNTIME_KEY,))
    source["embedded"]["skills"][0]["system_prompt"] = "An unrelated second upgrade"
    reviewed = await preview(db_session, workspace, source)
    assert next(i for i in reviewed["items"] if i["key"] == reconfigure.RUNTIME_KEY)["action"] == "unchanged"
    await apply_reviewed(db_session, workspace, source, reviewed)
    state = await db_session.get(WorkspaceBlueprintUpgradeState, workspace.id)
    assert state.baseline["runtime"]["installed"] == before
    retained = await reconfigure.retained_runtime_payload(db_session, workspace=workspace, payload=source)
    assert retained["contract"]["requires"]["tools"] == []


@pytest.mark.parametrize("keep", [False, True])
async def test_an_applied_runtime_change_is_reported_as_updated(db_session, keep):
    workspace, source = await installed(db_session)
    source["contract"]["requires"]["tools"] = ["new_optional_for_test"]
    result = await apply_reviewed(db_session, workspace, source, keep=(reconfigure.RUNTIME_KEY,) if keep else ())
    updated = [item.get("key") for item in result["updated"]]
    if keep:
        assert reconfigure.RUNTIME_KEY not in updated
        assert reconfigure.RUNTIME_KEY in result["kept_yours"]
    else:
        assert updated == [reconfigure.RUNTIME_KEY]
        assert result["kept_yours"] == []


async def test_v3_reconfigure_keeps_same_title_knowledge_packs_isolated_by_slug(db_session):
    source = payload()
    source["embedded"]["knowledge_packs"] = [
        {
            "slug": "workspace-knowledge-2",
            "title": "Workspace Knowledge",
            "purpose": "First purpose",
            "mode": "skeleton",
            "folder_structure": [],
            "starter_documents": [],
            "external_source": None,
        },
        {
            "slug": "workspace-knowledge-3",
            "title": "Workspace Knowledge",
            "purpose": "Second purpose",
            "mode": "skeleton",
            "folder_structure": [],
            "starter_documents": [],
            "external_source": None,
        },
    ]
    install = await install_blueprint(
        db_session,
        entity_id=generate_ulid(),
        payload=source,
        mode=InstallMode.LIVE,
        blueprint_id="builtin:upgrade-test",
    )
    workspace = await db_session.get(Workspace, install.workspace_id)
    updated = copy.deepcopy(source)
    updated["embedded"]["knowledge_packs"][0]["purpose"] = "Updated first purpose"

    await apply_reviewed(db_session, workspace, updated)

    async def purposes_by_slug():
        groups = list((await db_session.execute(select(DocumentGroup).where(
            DocumentGroup.workspace_id == workspace.id,
        ))).scalars().all())
        return {
            group.settings["installed_from_blueprint_slug"]: group.settings["purpose"]
            for group in groups
        }

    assert await purposes_by_slug() == {
        "workspace-knowledge-2": "Updated first purpose",
        "workspace-knowledge-3": "Second purpose",
    }

    await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    assert await purposes_by_slug() == {
        "workspace-knowledge-2": "First purpose",
        "workspace-knowledge-3": "Second purpose",
    }


async def test_v3_runtime_use_and_revert_export_the_applied_mcp_policy(db_session):
    workspace, source = await installed(db_session)
    _mcp_requirement(source, required=False, install_blocking=False)
    await reconfigure.capture_install_baseline(db_session, workspace=workspace, payload=source)

    updated = payload()
    _mcp_requirement(updated, required=True, install_blocking=True)
    await apply_reviewed(db_session, workspace, updated)
    assert await _export_mcp_requirement(db_session, workspace) == [{
        "slug": "optional-reconfigure-mcp", "purpose": None,
        "config_fields_to_set": [], "required": True, "install_blocking": True,
    }]

    await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    assert await _export_mcp_requirement(db_session, workspace) == [{
        "slug": "optional-reconfigure-mcp", "purpose": None,
        "config_fields_to_set": [], "required": False, "install_blocking": False,
    }]


async def test_v3_runtime_keep_yours_exports_the_installed_mcp_policy(db_session):
    workspace, source = await installed(db_session)
    _mcp_requirement(source, required=False, install_blocking=False)
    await reconfigure.capture_install_baseline(db_session, workspace=workspace, payload=source)

    updated = payload()
    _mcp_requirement(updated, required=True, install_blocking=True)
    await apply_reviewed(db_session, workspace, updated, keep=(reconfigure.RUNTIME_KEY,))

    assert await _export_mcp_requirement(db_session, workspace) == [{
        "slug": "optional-reconfigure-mcp", "purpose": None,
        "config_fields_to_set": [], "required": False, "install_blocking": False,
    }]


async def test_legacy_partial_upgrade_preserves_mcp_policy_after_v3_invalidation(db_session):
    from packages.core.blueprints.upgrade import apply as apply_legacy

    workspace, source = await installed(db_session)
    _mcp_requirement(source, required=False, install_blocking=False)
    await reconfigure.capture_install_baseline(db_session, workspace=workspace, payload=source)

    updated = payload()
    _mcp_requirement(updated, required=True, install_blocking=True)
    result = await apply_legacy(
        db_session, workspace=workspace, payload=updated, source_payload=updated,
        current_version="2",
    )
    assert result["fully_synchronized"] is False
    await reconfigure.invalidate_after_legacy_upgrade(db_session, workspace=workspace)
    assert await _export_mcp_requirement(db_session, workspace) == [{
        "slug": "optional-reconfigure-mcp", "purpose": None,
        "config_fields_to_set": [], "required": False, "install_blocking": False,
    }]


async def test_legacy_full_upgrade_and_revert_preserve_mcp_policy_after_v3_invalidation(db_session):
    from packages.core.blueprints.upgrade import apply as apply_legacy
    from packages.core.blueprints.upgrade import revert as revert_legacy

    source = payload()
    _mcp_requirement(source, required=False, install_blocking=False)
    result = await install_blueprint(
        db_session, entity_id=generate_ulid(), payload=source,
        mode=InstallMode.LIVE, blueprint_id="builtin:upgrade-test",
    )
    workspace = await db_session.get(Workspace, result.workspace_id)
    updated = payload()
    _mcp_requirement(updated, required=False, install_blocking=False)
    updated["embedded"]["skills"][0]["system_prompt"] = "Updated writer prompt"

    result = await apply_legacy(
        db_session, workspace=workspace, payload=updated, source_payload=updated,
        current_version="2",
    )
    assert result["fully_synchronized"] is True
    await reconfigure.invalidate_after_legacy_upgrade(db_session, workspace=workspace)
    assert await _export_mcp_requirement(db_session, workspace) == [{
        "slug": "optional-reconfigure-mcp", "purpose": None,
        "config_fields_to_set": [], "required": False, "install_blocking": False,
    }]

    # The legacy undo shares the state row with v3 receipts: invalidating the
    # v3 receipt keeps it, and only the legacy revert may spend it.
    with pytest.raises(BlueprintUpgradePlanChangedError, match="no applied upgrade"):
        await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    undo = await revert_legacy(db_session, workspace=workspace)
    assert undo["reverted"]
    assert (await db_session.get(WorkspaceBlueprintUpgradeState, workspace.id)).restore_point is None
    assert await _export_mcp_requirement(db_session, workspace) == [{
        "slug": "optional-reconfigure-mcp", "purpose": None,
        "config_fields_to_set": [], "required": False, "install_blocking": False,
    }]


async def test_undo_refuses_later_configuration_edit_without_partial_restore(db_session):
    workspace, source = await installed(db_session)
    source["recipe"]["operating_model"]["context"] = "Upgraded context"
    source["embedded"]["skills"][0]["system_prompt"] = "Upgraded skill"
    await apply_reviewed(db_session, workspace, source)
    workspace.operating_context = "Edited after upgrade"
    await db_session.flush()
    with pytest.raises(ValueError, match="changed"):
        await reconfigure.revert(db_session, workspace=workspace, actor=None, by_user_id=None)
    skill = (await db_session.execute(select(Skill).where(Skill.slug == "writer"))).scalar_one()
    assert skill.system_prompt == "Upgraded skill"
    await db_session.refresh(workspace)
    assert workspace.operating_context == "Edited after upgrade"


async def test_v3_invalid_variable_is_actionable_and_lock_order_is_consistent(monkeypatch):
    from fastapi import HTTPException
    from apps.api.routers import workspaces as routes
    from packages.core.services import reusable_resource_locks

    source = payload()
    source["contract"]["variables"] = [{"key": "label", "default": "Studio", "materialize": True}]
    workspace = SimpleNamespace(id="ws", entity_id="entity", settings={})
    user = SimpleNamespace(id="owner", entity_id="entity")
    calls = []
    async def lifecycle(*args, **kwargs):
        calls.append("lifecycle")
    async def manage(*args, **kwargs):
        calls.append("workspace")
        return workspace
    monkeypatch.setattr(reusable_resource_locks, "lock_reusable_resource_lifecycle", lifecycle)
    monkeypatch.setattr(routes, "_require_workspace_manage", manage)
    monkeypatch.setattr(routes, "_blueprint_payload_for", AsyncMock(return_value=(source, "2", "builtin:upgrade-test")))
    request = routes.BlueprintReconfigurationApplyRequest(
        expected_blueprint_fingerprint="source", expected_plan_fingerprint="plan", variable_values={"label": []},
    )
    with pytest.raises(HTTPException) as error:
        await routes.apply_blueprint_reconfiguration("ws", request, user, SimpleNamespace())
    assert error.value.status_code == 409
    assert "must be string" in error.value.detail
    assert calls == ["lifecycle", "workspace"]


async def test_v3_upgrade_uses_installed_workspace_provenance_without_purchase_lookup(monkeypatch):
    from apps.api.routers import workspaces as routes

    source = payload()
    workspace = SimpleNamespace(id="ws", entity_id="entity", settings={})

    class NoCommerceDatabase:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("an installed Workspace upgrade must not query commerce")

    monkeypatch.setattr(
        routes,
        "_blueprint_payload_for",
        AsyncMock(return_value=(source, "2", "builtin:upgrade-test")),
    )

    result = await routes._reconfiguration_payload(
        NoCommerceDatabase(),
        workspace=workspace,
        variable_values={},
    )

    assert result[1] == source
    assert result[-1] == "builtin:upgrade-test"


async def test_missing_new_variable_preview_retains_saved_and_default_values(monkeypatch):
    from apps.api.routers import workspaces as routes
    from packages.core.services import reusable_resource_locks

    source = payload()
    source["contract"]["variables"] = [
        {"key": "saved", "default": "Default", "materialize": True},
        {"key": "defaulted", "default": "Ready", "materialize": True},
        {"key": "new", "required": True, "materialize": True},
    ]
    workspace = SimpleNamespace(id="ws", entity_id="entity", settings={"blueprint_personalization": {"saved": "Original name"}})
    user = SimpleNamespace(id="owner", entity_id="entity")
    monkeypatch.setattr(reusable_resource_locks, "lock_reusable_resource_lifecycle", AsyncMock())
    monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr(routes, "_blueprint_payload_for", AsyncMock(return_value=(source, "2", "builtin:upgrade-test")))
    monkeypatch.setattr(reconfigure, "_state", AsyncMock(return_value=SimpleNamespace(baseline={"known": True}, restore_point={"applied_at": "2026-09-10T01:00:00Z", "to_version": "1"})))
    monkeypatch.setattr(reconfigure, "active_runs", AsyncMock(return_value=[]))
    monkeypatch.setattr(routes, "_release_choice_fields", AsyncMock(return_value={"available_versions": []}))
    result = await routes._preview_blueprint_reconfiguration(workspace_id="ws", variable_values={}, channel_config_ids={}, user=user, db=None)
    assert result["variables_ready"] is False
    assert result["resolved_variable_values"] == {"saved": "Original name", "defaulted": "Ready"}
    assert result["can_revert"] is True


async def test_api_keep_runtime_preflights_retained_contract(monkeypatch):
    from apps.api.routers import workspaces as routes
    from packages.core.services import reusable_resource_locks

    source = payload()
    retained = payload()
    source["contract"]["channels"] = [{"channel_type": "telegram", "required": True}]
    workspace = SimpleNamespace(id="ws", entity_id="entity", settings={})
    user = SimpleNamespace(id="owner", entity_id="entity")
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(reusable_resource_locks, "lock_reusable_resource_lifecycle", AsyncMock())
    monkeypatch.setattr(routes, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr(routes, "_reconfiguration_payload", AsyncMock(return_value=(source, source, source, {}, {}, "2", "builtin:upgrade-test")))
    monkeypatch.setattr(reconfigure, "retained_runtime_payload", AsyncMock(return_value=retained))
    monkeypatch.setattr(reconfigure, "plan", AsyncMock(return_value={"items": [{"key": reconfigure.RUNTIME_KEY, "action": "update"}]}))
    apply = AsyncMock(return_value={"upgrade_protocol_version": 3, "synchronization_status": "customized"})
    monkeypatch.setattr(reconfigure, "apply", apply)
    preflight = AsyncMock(return_value={"ready": True, "requirements": []})
    monkeypatch.setattr(routes, "_blueprint_upgrade_setup_preflight", preflight)
    from packages.core.services import workspace_update_offer
    monkeypatch.setattr(workspace_update_offer, "close_update_cards", AsyncMock(return_value=0))
    request = routes.BlueprintReconfigurationApplyRequest(
        expected_blueprint_fingerprint="source", expected_plan_fingerprint="plan",
        configuration_resolutions=[{"key": reconfigure.RUNTIME_KEY, "resolution": "keep_yours"}],
    )
    result = await routes.apply_blueprint_reconfiguration("ws", request, user, db)
    assert preflight.call_args.kwargs["payload"] is source
    assert preflight.call_args.kwargs["retained_contract"] is retained["contract"]
    assert result["synchronization_status"] == "customized"
    assert apply.await_count == 1
