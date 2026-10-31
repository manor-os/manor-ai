"""Remove review gates that Blueprint import generated from template grants.

Import used to turn a template's historical automatic grants
(``auto_approve_*``, ``automated_runs_always_allow`` or the old default) into
an ask rule for every category. Those rules outranked the mode the installer
chose, so "Run automatically" still asked before every file write. Import now
drops such grants. A Workspace loses the generated gates only when its source
Blueprint, imported today, no longer produces them; gates that stand for an
unrepresentable restriction, and every other rule, stay.
"""
import json
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from sqlalchemy import MetaData, Table, inspect, select, text

from packages.core.governance.approval_policy_migration import (
    LegacyApprovalConversionError, _rule_id, normalize_legacy_workspace_policy,
)
from packages.core.models.base import generate_ulid

REVISION = '20261019_01'
# The import's own gate identities, plus the read gate older imports added.
GENERATED_GATE_IDS = frozenset({_rule_id('template_review', 'read')} | {
    rule['id'] for rule in normalize_legacy_workspace_policy(
        {'hitl_required_capabilities': ['probe']}, safe_import=True)['approval']['rules']})


def _binding(policy):
    approval = policy.get('approval') if isinstance(policy, dict) else None
    if not isinstance(approval, dict):
        return None
    binding = approval.get('policy', approval)
    return binding if isinstance(binding, dict) and isinstance(binding.get('rules'), list) else None


def _generated_gate(rule) -> bool:
    return (isinstance(rule, dict) and rule.get('id') in GENERATED_GATE_IDS and rule.get('effect') == 'ask'
            and (rule.get('selector') or {}).get('kind') == 'category')


def plan_repairs(policies, workspaces, blueprints, history=None):
    """Pure plan over plain rows; ``blueprints`` maps id to stored payload."""
    history = history or {}
    operations, skipped = [], []
    for row in policies:
        binding = _binding(row.get('policy'))
        if not binding or not any(_generated_gate(rule) for rule in binding['rules']):
            continue
        source = ((workspaces.get(row['workspace_id']) or {}).get('settings') or {}).get('_blueprint') or {}
        payload = blueprints.get(source.get('blueprint_id'))
        if not isinstance(payload, dict):
            skipped.append({'workspace_id': row['workspace_id'], 'reason': 'source Blueprint unavailable'})
            continue
        # The installer imports exactly this section.
        governance = (payload.get('policy') or {}).get('governance') or {}
        try:
            regenerated = _binding(normalize_legacy_workspace_policy(governance, safe_import=True))
        except LegacyApprovalConversionError:
            skipped.append({'workspace_id': row['workspace_id'], 'reason': 'source governance requires review'})
            continue
        if any(_generated_gate(rule) for rule in regenerated['rules']):
            skipped.append({'workspace_id': row['workspace_id'], 'reason': 'restriction review retained'})
            continue
        policy = deepcopy(row['policy'])
        target = _binding(policy)
        removed = [rule['id'] for rule in target['rules'] if _generated_gate(rule)]
        target['rules'] = [rule for rule in target['rules'] if not _generated_gate(rule)]
        revision = max(int(row.get('revision') or 0), int(history.get(row['workspace_id']) or 0)) + 1
        operations.append({'workspace_id': row['workspace_id'], 'entity_id': row.get('entity_id'),
                           'before': deepcopy(row), 'policy': policy, 'revision': revision, 'removed': removed})
    return {'operations': operations, 'skipped': skipped}


def _read(connection):
    metadata = MetaData()
    policies_table = Table('governance_policies', metadata, autoload_with=connection)
    workspaces_table = Table('workspaces', metadata, autoload_with=connection)
    policies = [dict(row) for row in connection.execute(select(policies_table)).mappings()
                if any(_generated_gate(rule) for rule in (_binding(row['policy']) or {}).get('rules', []))]
    ids = [row['workspace_id'] for row in policies]
    workspaces = {row['id']: dict(row) for row in connection.execute(
        select(workspaces_table.c.id, workspaces_table.c.settings).where(workspaces_table.c.id.in_(ids))).mappings()} if ids else {}
    blueprint_ids = {((row.get('settings') or {}).get('_blueprint') or {}).get('blueprint_id') for row in workspaces.values()} - {None}
    blueprints = {}
    if blueprint_ids:
        table = Table('workspace_blueprints', metadata, autoload_with=connection)
        blueprints = {row['id']: row['payload'] for row in connection.execute(
            select(table.c.id, table.c.payload).where(table.c.id.in_(blueprint_ids))).mappings()}
    history = defaultdict(int)
    if ids:
        revisions = Table('governance_revisions', metadata, autoload_with=connection)
        for row in connection.execute(select(revisions.c.workspace_id, revisions.c.revision)
                                      .where(revisions.c.workspace_id.in_(ids))).mappings():
            history[row['workspace_id']] = max(history[row['workspace_id']], int(row['revision'] or 0))
    return policies, workspaces, blueprints, history


def apply(connection):
    """Run inside Alembic's transaction, with per-Workspace audit receipts."""
    existing = set(inspect(connection).get_table_names())
    required = {'governance_policies', 'governance_revisions', 'workspaces', 'workspace_blueprints', 'audit_log'}
    if not required <= existing:
        return {'operations': [], 'skipped': [], 'applied': 0}
    if connection.dialect.name == 'postgresql':
        for name in sorted(required):
            connection.execute(text(f'LOCK TABLE {name} IN SHARE ROW EXCLUSIVE MODE'))
    metadata = MetaData()
    audit = Table('audit_log', metadata, autoload_with=connection)
    if connection.execute(select(audit.c.id).where(audit.c.action == 'approval_policy.template_grant_repair_checked',
                                                   audit.c.resource_id == REVISION)).first():
        return {'operations': [], 'skipped': [], 'applied': 0}
    plan = plan_repairs(*_read(connection))
    policies = Table('governance_policies', metadata, autoload_with=connection)
    revisions = Table('governance_revisions', metadata, autoload_with=connection)
    now = datetime.now(timezone.utc)
    for operation in plan['operations']:
        connection.execute(policies.update().where(policies.c.workspace_id == operation['workspace_id'])
                           .values(policy=operation['policy'], revision=operation['revision']))
        connection.execute(revisions.insert().values(id=generate_ulid(), workspace_id=operation['workspace_id'],
            revision=operation['revision'], policy=operation['policy'], created_at=now,
            change_summary="Remove review gates generated from Blueprint grants; the Workspace's chosen mode decides"))
        connection.execute(audit.insert().values(id=generate_ulid(), action='approval_policy.template_grant_repaired',
            entity_id=operation['entity_id'], resource_type='governance_policies', resource_id=operation['workspace_id'],
            details={'revision': REVISION, 'removed': operation['removed'],
                     'source': json.loads(json.dumps(operation['before'], default=str)),
                     'after': {'policy': operation['policy'], 'revision': operation['revision']}}, created_at=now))
    connection.execute(audit.insert().values(id=generate_ulid(), action='approval_policy.template_grant_repair_checked',
        resource_type='approval_migration', resource_id=REVISION,
        details={'skipped': plan['skipped'], 'repaired_rows': len(plan['operations'])}, created_at=now))
    return {**plan, 'applied': len(plan['operations'])}
