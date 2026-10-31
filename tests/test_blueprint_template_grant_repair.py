from copy import deepcopy

import pytest
from sqlalchemy import select

from packages.core.governance.approval_policy_migration import _rule_id
from packages.core.migrations.blueprint_template_grant_repair import GENERATED_GATE_IDS, apply, plan_repairs
from tests.fixtures.approval_v1.approval_policy_migration import (
    normalize_legacy_workspace_policy as v1_normalize_legacy_workspace_policy,
)

GRANTS = {'auto_approve_capabilities': ['file.write', 'manor.composite'], 'max_risk_level': 'high'}
RESTRICTION = {'hitl_required_capabilities': ['external.email']}
USER_RULE = {'id': 'mine', 'effect': 'ask', 'selector': {'kind': 'action', 'action_key': 'email.send'},
             'source_kind': 'user', 'source_id': None}


def _installed(governance):
    """What the historical import stored, plus a rule the owner added later."""
    policy = v1_normalize_legacy_workspace_policy(governance, root_entity_id='e', safe_import=True)
    policy['approval']['policy']['mode'] = 'automatic'
    policy['approval']['policy']['rules'].append(deepcopy(USER_RULE))
    return policy


def _rows(governance, blueprint_id='builtin:studio'):
    policies = [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 2, 'policy': _installed(governance)}]
    workspaces = {'w': {'id': 'w', 'settings': {'_blueprint': {'blueprint_id': blueprint_id}}}}
    blueprints = {'builtin:studio': {'policy': {'governance': governance}}}
    return policies, workspaces, blueprints


def test_gates_generated_from_template_grants_are_removed_and_other_rules_kept():
    policies, workspaces, blueprints = _rows(GRANTS)
    assert {rule['id'] for rule in policies[0]['policy']['approval']['policy']['rules']} >= {
        _rule_id('template_review', 'files')}
    plan = plan_repairs(policies, workspaces, blueprints, {'w': 5})
    [operation] = plan['operations']
    binding = operation['policy']['approval']['policy']
    assert binding['rules'] == [USER_RULE]
    assert binding['mode'] == 'automatic'
    assert operation["removed"] and set(operation["removed"]) <= GENERATED_GATE_IDS
    assert operation['revision'] == 6
    assert policies[0]['policy']['approval']['policy']['rules'] != binding['rules'], 'input rows are not mutated'


@pytest.mark.parametrize('governance, blueprint_id, reason', [
    (RESTRICTION, 'builtin:studio', 'restriction review retained'),
    (GRANTS, 'builtin:deleted', 'source Blueprint unavailable'),
])
def test_gates_that_still_stand_for_something_are_kept(governance, blueprint_id, reason):
    plan = plan_repairs(*_rows(governance, blueprint_id))
    assert not plan['operations']
    assert plan['skipped'] == [{'workspace_id': 'w', 'reason': reason}]


def test_policies_without_generated_gates_are_untouched():
    policies, workspaces, blueprints = _rows(GRANTS)
    policies[0]['policy']['approval']['policy']['rules'] = [USER_RULE]
    assert plan_repairs(policies, workspaces, blueprints) == {'operations': [], 'skipped': []}


@pytest.mark.asyncio
async def test_apply_updates_policy_history_and_receipts_once(db_session):
    from packages.core.models.audit import AuditLog
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.models.governance import GovernancePolicy, GovernanceRevision
    from packages.core.models.workspace import Workspace
    from packages.core.models.blueprint import WorkspaceBlueprint

    entity = Entity(id=generate_ulid(), name='Repair fixture', plan_id='plan_enterprise')
    db_session.add(entity)
    await db_session.flush()
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name='Studio',
                          settings={'_blueprint': {'blueprint_id': 'builtin:repair-studio'}})
    db_session.add_all([
        workspace,
        WorkspaceBlueprint(id='builtin:repair-studio', slug='repair-studio', title='Studio',
                           payload={'policy': {'governance': GRANTS}}),
    ])
    await db_session.flush()
    db_session.add(GovernancePolicy(workspace_id=workspace.id, entity_id=entity.id, revision=1,
                                    policy=_installed(GRANTS)))
    await db_session.commit()

    first = await db_session.run_sync(lambda session: apply(session.connection()))
    second = await db_session.run_sync(lambda session: apply(session.connection()))
    await db_session.commit()
    assert first['applied'] == 1 and second['applied'] == 0

    row = await db_session.get(GovernancePolicy, workspace.id, populate_existing=True)
    assert row.revision == 2
    assert row.policy['approval']['policy']['rules'] == [USER_RULE]
    history = (await db_session.execute(select(GovernanceRevision).where(
        GovernanceRevision.workspace_id == workspace.id))).scalars().all()
    assert [item.revision for item in history] == [2]
    actions = (await db_session.execute(select(AuditLog.action).where(
        AuditLog.action.like('approval_policy.template_grant_%')))).scalars().all()
    assert sorted(actions) == ['approval_policy.template_grant_repair_checked', 'approval_policy.template_grant_repaired']
