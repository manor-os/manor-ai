"""Real SQLite Blueprint lifecycle with unified, account-bound approvals.

Only Redis is replaced. Export, creation, install, policy writes, upgrade,
restore and promotion run against their production services and ORM rows.
"""
from __future__ import annotations

import copy
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from packages.core.blueprints import InstallMode, export_workspace, install_blueprint, promote_workspace
from packages.core.blueprints import configuration_upgrade as upgrade
from packages.core.blueprints import report as report_module
from packages.core.blueprints.exporter import ExportContext
from packages.core.blueprints.launch_review import OperationsSection, RunOutcomes
from packages.core.blueprints.report import simulate_report
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approval_policy_service import account_defaults, load_policy_chain
from packages.core.governance.approval_projection import policy_override, replace_workspace_rules
from packages.core.governance.policy import WorkspacePolicy
from packages.core.governance.policy_cache import GovernancePolicyCacheFactory, GovernancePolicyMutationFence
from packages.core.governance.service import update_policy
from tests.approval_settings_test_utils import POLICY_WRITE_SIDE_EFFECT_MODELS
from packages.core.models.billing import CreditUsageLog
from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState
from packages.core.models.document import DocumentFolder
from packages.core.models.execution import ExecutionStep
from packages.core.models.goal import Goal, GoalMeasurement
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.runtime_learning import RuntimeEvidence
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import Skill
from packages.core.models.user import Entity
from packages.core.models.workflow import WorkflowTemplateInstallation
from packages.core.models.workspace import Agent, ToolDefinition, Workspace
from packages.core.services.entity_service import create_workspace

pytestmark = pytest.mark.unit


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_, compiler, **kw):
    return compiler.visit_JSON(JSON(), **kw)


@compiles(ARRAY, "sqlite")
def _sqlite_array(type_, compiler, **kw):
    # This fixture never binds array values; do not mutate the shared model
    # metadata as the old smoke did (which broke later PostgreSQL tests).
    return compiler.visit_JSON(JSON(), **kw)


def _source_rule(category="publishing"):
    return {"rule_key": "publication-review", "rule_type": "approval_required",
            "approval_selectors": [{"kind": "category", "category": category}]}


def _export_context():
    # Focus on approval portability; no declared Workflow, Proposal Task,
    # Automation, Agent, integration, or Knowledge component is omitted.
    return ExportContext(include_subscriptions=False, include_goals=False,
        include_stats=False, include_scheduled_jobs=False, include_workflows=False,
        include_custom_fields=False, include_channel_requirements=False,
        include_session_requirements=False, include_embedded_agents=False,
        include_embedded_skills=False, include_knowledge_packs=False)


def _compare_roundtrip(source, installed):
    script = Path(__file__).parents[1] / ".agents/skills/manor-workspace-blueprint/scripts/compare_roundtrip.py"
    spec = importlib.util.spec_from_file_location("approval_blueprint_roundtrip", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._normalize(module._runtime_projection(source)) == module._normalize(module._runtime_projection(installed))
    assert module.compare(source, installed) == []


@pytest.mark.asyncio
async def test_real_export_install_upgrade_revert_and_promote(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    cache = SimpleNamespace(
        get_current=AsyncMock(return_value=None), store_if_current=AsyncMock(return_value=False),
        begin_mutation=AsyncMock(side_effect=lambda workspace_id: GovernancePolicyMutationFence(workspace_id, "isolated", True)),
        commit_mutation=AsyncMock(), rollback_mutation=AsyncMock())
    monkeypatch.setattr(GovernancePolicyCacheFactory, "_default", cache)
    # The report's run outcomes read PostgreSQL-only tables and have their own
    # coverage (test_blueprint_launch_review); this SQLite roundtrip checks the
    # approval counterfactual.
    monkeypatch.setattr(report_module, "build_operations", AsyncMock(return_value=OperationsSection(
        tasks=RunOutcomes(), agents=RunOutcomes(), workflows=RunOutcomes(), automations=RunOutcomes(),
    )))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [Entity, Workspace, DocumentFolder, GovernancePolicy, GovernanceRevision,
              WorkspaceBlueprintUpgradeState, MarketplaceResourceLink, Agent, Skill,
              WorkflowTemplateInstallation, ScheduledJob, Goal, GoalMeasurement,
              ExecutionStep, RuntimeEvidence, ToolDefinition, CreditUsageLog,
              *POLICY_WRITE_SIDE_EFFECT_MODELS]
    try:
        async with engine.begin() as connection:
            for model in tables:
                await connection.run_sync(model.__table__.create)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add_all([Entity(id=key, name=key, settings={"approval_defaults": account_defaults()})
                        for key in ("author", "operator")])
            await db.flush()
            operator = await db.get(Entity, "operator")
            defaults = account_defaults()
            defaults["policy"]["rules"] = [ApprovalRule(id="account-message-ban", effect="deny",
                selector={"kind": "category", "category": "communication"}).model_dump()]
            operator.settings = {"approval_defaults": defaults}
            # A legacy nested mirror is not an independent policy owner.
            # Export the actual ask/deny rules and preserve both across two
            # installs when the operator explicitly opts into author bans.
            mirror_source = await create_workspace(db, "author", name="Mirror", kind="studio",
                operating_model={"governance": {"auto_approve_actions": ["*"],
                    "max_risk_level": "low"}}, match_business_ledgers=False)
            mirror_rules = [ApprovalRule(id=effect, effect=effect, selector={
                "kind": "action", "action_key": action}) for effect, action in (
                ("ask", "workspace.file.modify"), ("deny", "workspace.file.delete"))]
            await update_policy(db, entity_id="author", workspace_id=mirror_source.id,
                policy=WorkspacePolicy(approval=ApprovalPolicyOverride(rules=mirror_rules)),
                change_summary="Current policy replaces stale mirror")
            mirror_payload = await export_workspace(db, mirror_source.id, title="Mirror", context=_export_context())
            for mode in (InstallMode.SIMULATE, InstallMode.LIVE):
                current = copy.deepcopy(mirror_payload)
                for _ in range(2):
                    assert current["recipe"]["operating_model"]["governance"] == current["policy"]["governance"]
                    assert {(rule.effect, rule.selector.action_key) for rule in policy_override(
                        current["policy"]["governance"]).rules} == {
                        ("ask", "workspace.file.modify"), ("deny", "workspace.file.delete")}
                    result = await install_blueprint(db, entity_id="operator", payload=current,
                        mode=mode, governance_preset="follow_default", install_author_hard_blocks=True)
                    assert not [todo for todo in result.todos if todo.blocking]
                    current = await export_workspace(db, result.workspace_id, title="Mirror", context=_export_context())
                    _compare_roundtrip(mirror_payload, current)
                    assert current["recipe"]["operating_model"]["governance"] == current["policy"]["governance"]

            # A restrictive author risk ceiling becomes persistent review
            # rules; following the account must not erase them on reinstall.
            review_source = await create_workspace(db, "author", name="Review", kind="studio",
                match_business_ledgers=False)
            await update_policy(db, entity_id="author", workspace_id=review_source.id,
                policy=WorkspacePolicy(max_risk_level="medium"), change_summary="Author review ceiling")
            review_payload = await export_workspace(db, review_source.id, title="Review", context=_export_context())
            first_review_export = None
            for _ in range(2):
                result = await install_blueprint(db, entity_id="operator", payload=review_payload,
                    mode=InstallMode.LIVE, governance_preset="follow_default")
                assert not [todo for todo in result.todos if todo.blocking]
                review_chain = await load_policy_chain(db, entity_id="operator", workspace_id=result.workspace_id)
                assert review_chain.local.policy.mode is None
                assert len(review_chain.local.policy.rules) == 8
                review_payload = await export_workspace(db, result.workspace_id, title="Review", context=_export_context())
                if first_review_export is None:
                    first_review_export = copy.deepcopy(review_payload)
                _compare_roundtrip(first_review_export, review_payload)

            source = await create_workspace(db, "author", name="Studio", kind="studio",
                operating_context="Make videos", primary_work="Review publication",
                operating_model={"rules": [_source_rule()]}, match_business_ledgers=False)
            author_policy = replace_workspace_rules(WorkspacePolicy(approval=ApprovalPolicyOverride(
                mode="automatic", rules=[ApprovalRule(id="author-local", effect="allow", selector={
                    "kind": "resource", "action_key": "workspace.file.modify", "account_id": "author-account",
                    "resource_kind": "file", "resource_ids": ["author-document"]})])), [_source_rule()])
            await update_policy(db, entity_id=source.entity_id, workspace_id=source.id,
                policy=author_policy, change_summary="Author configuration")
            exported = await export_workspace(db, source.id, title="Studio", context=_export_context())
            assert policy_override(exported["policy"]["governance"]).mode is None
            assert "author-document" not in str(exported)
            assert "author-account" not in str(exported)
            assert "root_entity_id" not in str(exported)

            installed = []
            for mode in (InstallMode.SIMULATE, InstallMode.LIVE):
                result = await install_blueprint(db, entity_id="operator", payload=exported, mode=mode,
                    blueprint_id="blueprint-studio", blueprint_version="1.0.0", governance_preset="follow_default")
                assert not [todo for todo in result.todos if todo.blocking]
                workspace = await db.get(Workspace, result.workspace_id)
                installed.append(workspace)
                chain = await load_policy_chain(db, entity_id="operator", workspace_id=workspace.id)
                assert chain.local.policy.mode is None
                assert chain.local.effective_policy.mode == "risk_based"
                assert chain.local.root_scope.id == "operator"
                again = await export_workspace(db, workspace.id, title="Studio", context=_export_context())
                _compare_roundtrip(exported, again)

            simulation = installed[0]
            state = await db.get(WorkspaceBlueprintUpgradeState, simulation.id)
            plan = await upgrade.plan_configuration(db, workspace=simulation, payload=exported,
                baseline=state.baseline["configuration"])
            assert all(item["action"] == "unchanged" for item in plan["items"])
            policy_row = (await db.execute(select(GovernancePolicy).where(
                GovernancePolicy.workspace_id == simulation.id))).scalar_one()
            local_rule = ApprovalRule(id="operator-exception", effect="allow", selector={
                "kind": "resource", "action_key": "workspace.file.modify", "account_id": "operator-account",
                "resource_kind": "file", "resource_ids": ["operator-document"]})
            local_policy = policy_override(policy_row.policy)
            local_policy.mode = "automatic"
            local_policy.rules.append(local_rule)
            await update_policy(db, entity_id="operator", workspace_id=simulation.id,
                policy=WorkspacePolicy(approval=local_policy), change_summary="Operator customization")
            before = copy.deepcopy(policy_row.policy)

            source.operating_model = {"rules": [_source_rule("communication")]}
            await update_policy(db, entity_id="author", workspace_id=source.id,
                policy=replace_workspace_rules(author_policy, source.operating_model["rules"]),
                change_summary="Author revised business rule")
            upstream = await export_workspace(db, source.id, title="Studio", context=_export_context())
            plan = await upgrade.plan_configuration(db, workspace=simulation, payload=upstream,
                baseline=state.baseline["configuration"])
            assert not any(item["conflict"] for item in plan["items"])
            applied = await upgrade.apply_configuration(db, workspace=simulation, payload=upstream, plan=plan)
            await db.commit()
            await db.refresh(policy_row)
            local = policy_override(policy_row.policy)
            assert local.mode == "automatic" and local_rule in local.rules
            assert policy_row.policy["approval"]["root_entity_id"] == "operator"
            assert [rule.selector.category for rule in local.rules if rule.source_kind == "workspace_rule"] == ["communication"]
            assert simulation.operating_model["rules"] == [_source_rule("communication")]
            again = await export_workspace(db, simulation.id, title="Studio", context=_export_context())
            _compare_roundtrip(upstream, again)
            assert "operator-document" not in str(again) and "operator-account" not in str(again)
            followup = await upgrade.plan_configuration(db, workspace=simulation, payload=upstream, baseline=applied["baseline"])
            assert all(item["action"] == "unchanged" for item in followup["items"])

            await upgrade.revert_configuration(db, workspace=simulation, restore=applied["restore"])
            await db.refresh(policy_row)
            assert upgrade._governance_content(policy_row.policy) == upgrade._governance_content(before)
            assert simulation.operating_model["rules"] == [_source_rule()]
            for index, action_key in enumerate(("workspace.operation.draft", "external_message.send", "social_post.publish")):
                db.add(ExecutionStep(plan_id="simulation-plan", entity_id="operator", workspace_id=simulation.id,
                    step_key=str(index), kind="action", action_key=action_key, risk_level="low",
                    step_status="done", started_at=datetime.now(timezone.utc)))
            await db.flush()
            report = await simulate_report(db, simulation.id)
            assert report.activity.total_steps == 3
            assert {item.preset_key: (item.allowed, item.paused_for_hitl, item.denied)
                    for item in report.counterfactuals} == {
                "automatic": (1, 1, 1), "follow_default": (1, 1, 1), "confirm_changes": (0, 2, 1), "risk_based": (1, 1, 1),
            }
            policy_before_promotion = copy.deepcopy(policy_row.policy)
            revision_before_promotion = policy_row.revision
            promoted = await promote_workspace(db, simulation.id)
            assert promoted.promoted
            assert simulation.settings["sandbox"] is False
            await db.refresh(policy_row)
            assert policy_row.policy == policy_before_promotion
            assert policy_row.revision == revision_before_promotion
    finally:
        await engine.dispose()
