"""Blueprints carry review requirements; operator consent stays with its owner."""
from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import select

from packages.core.blueprints.exporter import _portable_governance, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.promote import promote_workspace
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.governance.approval_projection import policy_override
from packages.core.governance.service import list_revisions
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernancePolicy
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.proposals.constants import EXTERNAL_TASK_ACTION_KEY, TASK_ACTION_KEY
from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval


def _payload():
    return {
        "manifest": {"blueprint_version": "1.1", "title": "Proposal approvals", "kind": "operations"},
        "contract": {"variables": [], "channels": [], "sessions": [], "requires": {}},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"operating_model": {}, "subscriptions": [], "workflows": [], "scheduled_jobs": []},
        "policy": {"governance": {}, "post_install_checks": []},
    }


def _rule(rule_id, action, effect="allow"):
    return {"id": rule_id, **action_rule(action, effect=effect)}


async def _policy_row(db, workspace_id):
    return (await db.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == workspace_id,
    ).execution_options(populate_existing=True))).scalar_one()


async def _entity_id(db):
    entity_id = generate_ulid()
    db.add_all([
        Entity(id=entity_id, name="Blueprint approvals", plan_id="plan_enterprise",
               settings={"approval_defaults": account_defaults()}),
        User(id=entity_id, entity_id=entity_id, email=f"{entity_id}@blueprint.example",
             password_hash="test-only", role="owner", status="active"),
        UserMembership(user_id=entity_id, entity_id=entity_id, role="owner", status="active"),
    ])
    await db.flush()
    return entity_id


@pytest.mark.parametrize("simulation_owned", [False, True])
def test_portable_governance_strips_consent_regardless_of_legacy_provenance(simulation_owned):
    raw = {"approval": {"mode": "automatic", "rules": [
        _rule("task", TASK_ACTION_KEY), _rule("external", EXTERNAL_TASK_ACTION_KEY),
        _rule("review", "external_message.send", "ask"),
    ]}}
    original = deepcopy(raw)
    settings = {"strategist": {
        "proposal_task_approval_source": "blueprint_simulation" if simulation_owned else "operator",
    }}
    portable = _portable_governance(raw, settings)
    assert portable["approval"]["mode"] is None
    assert [(r["effect"], r["selector"]["action_key"]) for r in portable["approval"]["rules"]] == [
        ("ask", "external_message.send"),
    ]
    assert raw == original


@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_install_neither_mode_grants_internal_proposal_task(db_session, mode):
    payload = _payload()
    payload["policy"]["governance"] = {"approval": {
        "mode": "automatic", "rules": [_rule("author-task", TASK_ACTION_KEY)],
    }}
    result = await install_blueprint(db_session, entity_id=await _entity_id(db_session),
        payload=payload, mode=mode, governance_preset="follow_default")
    workspace = await db_session.get(Workspace, result.workspace_id)
    row = await _policy_row(db_session, workspace.id)
    assert row.policy["approval"]["root_entity_id"] == workspace.entity_id
    assert policy_override(row.policy).mode is None
    assert policy_override(row.policy).rules == []
    strategist = workspace.settings.get("strategist") or {}
    for field in ("auto_approve_proposals", "auto_approve_proposals_source", "proposal_task_approval_source"):
        assert field not in strategist


@pytest.mark.parametrize("install_author_hard_blocks", [False, True])
async def test_simulation_does_not_import_author_task_grant(db_session, install_author_hard_blocks):
    payload = _payload()
    payload["policy"]["governance"] = {"approval": {"rules": [_rule("author-task", TASK_ACTION_KEY)]}}
    # Runtime provenance from a stale source must not manufacture portable consent.
    payload["recipe"]["operating_model"]["settings"] = {
        "strategist": {"proposal_task_approval_source": "blueprint_simulation"},
    }
    result = await install_blueprint(
        db_session, entity_id=await _entity_id(db_session), payload=payload,
        mode=InstallMode.SIMULATE, install_author_hard_blocks=install_author_hard_blocks,
    )
    workspace = await db_session.get(Workspace, result.workspace_id)
    row = await _policy_row(db_session, workspace.id)
    assert policy_override(row.policy).rules == []
    assert "proposal_task_approval_source" not in (workspace.settings.get("strategist") or {})
    exported = await export_workspace(db_session, workspace.id, title="Portable task approval")
    assert not policy_override(exported["policy"].get("governance") or {}).rules
    before_revision = row.revision
    await promote_workspace(db_session, workspace.id, force=True)
    row = await _policy_row(db_session, workspace.id)
    assert policy_override(row.policy).rules == []
    assert row.revision == before_revision


async def test_promotion_preserves_operator_mode_and_rules_with_existing_audit(db_session):
    result = await install_blueprint(db_session, entity_id=await _entity_id(db_session), payload=_payload())
    workspace = await db_session.get(Workspace, result.workspace_id)
    await set_workspace_test_approval(
        db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
        actor_id=workspace.entity_id, mode="automatic",
        user_rules=[action_rule(TASK_ACTION_KEY), action_rule(EXTERNAL_TASK_ACTION_KEY)],
    )
    row = await _policy_row(db_session, workspace.id)
    before_policy, before_revision = deepcopy(row.policy), row.revision
    promoted = await promote_workspace(db_session, workspace.id, user_id=workspace.entity_id, force=True)
    assert promoted.promoted is True
    row = await _policy_row(db_session, workspace.id)
    assert row.policy == before_policy
    assert row.revision == before_revision
    revisions = await list_revisions(db_session, workspace.id)
    assert revisions[0].policy == row.policy
    assert revisions[0].changed_by == workspace.entity_id
    assert "proposal_task_approval_source" not in (workspace.settings.get("strategist") or {})


@pytest.mark.parametrize("enabled", [False, True])
async def test_explicit_task_choice_survives_simulation_promotion(db_session, enabled):
    result = await install_blueprint(db_session, entity_id=await _entity_id(db_session), payload=_payload())
    workspace = await db_session.get(Workspace, result.workspace_id)
    await set_workspace_test_approval(
        db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
        actor_id=workspace.entity_id, user_rules=[action_rule(TASK_ACTION_KEY)] if enabled else [],
    )
    row = await _policy_row(db_session, workspace.id)
    before_policy, before_revision = deepcopy(row.policy), row.revision
    await promote_workspace(db_session, workspace.id, force=True)
    row = await _policy_row(db_session, workspace.id)
    assert bool(policy_override(row.policy).rules) is enabled
    assert row.policy == before_policy
    assert row.revision == before_revision


@pytest.mark.parametrize("explicit_choice", [False, True])
async def test_simulation_export_never_transfers_operator_task_consent(db_session, explicit_choice):
    result = await install_blueprint(db_session, entity_id=await _entity_id(db_session), payload=_payload())
    workspace = await db_session.get(Workspace, result.workspace_id)
    if explicit_choice:
        await set_workspace_test_approval(
            db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
            actor_id=workspace.entity_id, user_rules=[action_rule(TASK_ACTION_KEY)],
        )
    row = await _policy_row(db_session, workspace.id)
    before_policy = deepcopy(row.policy)
    workspace.operating_model = {**workspace.operating_model, "governance": deepcopy(row.policy)}
    await db_session.flush()
    exported = await export_workspace(db_session, workspace.id, title="Portable proposal approvals")
    for governance in (exported["policy"].get("governance"), exported["recipe"]["operating_model"]["governance"]):
        assert not policy_override(governance or {}).rules
        assert policy_override(governance or {}).mode is None
    assert "proposal_task_approval_source" not in str(exported)
    assert row.policy == before_policy
    assert bool(policy_override(row.policy).rules) is explicit_choice
    installed = await install_blueprint(
        db_session, entity_id=await _entity_id(db_session), payload=exported, mode=InstallMode.LIVE,
    )
    installed_policy = await _policy_row(db_session, installed.workspace_id)
    assert not policy_override(installed_policy.policy).rules
