"""Forward repair of audited approval conversions; never an online interpreter.

Only exact denials accidentally changed into review requirements are restored.
Ambiguous selectors and historical authorizations remain unchanged and are
reported in the durable repair receipt. A completed revision is not a claim
that every historical policy has an equivalent representation.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, inspect, select, text

from packages.core.governance.approval_policy_migration import (
    LEGACY_POLICY_KEYS, normalize_legacy_workspace_policy, source_digest,
)
from packages.core.governance.approval_projection import compile_workspace_rules, import_blueprint_governance
from packages.core.migrations.unified_approval import _convert_task_rules, read_sources
from packages.core.models.base import generate_ulid

REVISION = '20261006_01'
_FIELDS = {
    'governance_policies': ('policy',), 'workspaces': ('operating_model',),
    'tasks': ('details',), 'workspace_drafts': ('fields',),
    'workspace_blueprint_upgrade_states': ('baseline', 'restore_point'),
}


def _repair_flattened(before, after, notes, path):
    groups = defaultdict(list)
    for entry in before.values():
        if not isinstance(entry, dict):
            continue
        parts = entry.get('path') or []
        if (entry.get('kind') == 'workspace_configuration' and len(parts) == 3
                and parts[:2] == ['operating_model', 'governance'] and parts[-1] in LEGACY_POLICY_KEYS):
            groups[tuple(parts[:-1])].append(entry)
    for key, entry in after.items():
        if not isinstance(entry, dict) or entry.get('kind') != 'workspace_configuration':
            continue
        parts = entry.get('path') or []
        prefix = tuple(parts[:2])
        if prefix not in groups or parts[2:] not in (['approval', 'rules'], ['approval', 'policy', 'rules']):
            continue
        for slot in ('source_content', 'installed_content', 'blueprint_content'):
            content = entry.get(slot)
            if not isinstance(content, dict) or not isinstance(content.get('value'), list):
                continue
            raw = {old['path'][-1]: old[slot]['value'] for old in groups[prefix]
                   if isinstance(old.get(slot), dict) and 'value' in old[slot]}
            repaired = _repair_value(raw, {'approval': {'rules': content['value']}}, notes, (*path, key, slot))
            content['value'] = repaired['approval']['rules']


def _repair_value(before, after, notes, path=()):
    result = deepcopy(after)
    if isinstance(before, dict) and isinstance(after, dict):
        if isinstance(before.get('entries'), dict) and isinstance(after.get('entries'), dict):
            _repair_flattened(before['entries'], result['entries'], notes, (*path, 'entries'))
        if set(before) & LEGACY_POLICY_KEYS and isinstance(after.get('approval'), dict):
            approval = result['approval']
            rules = approval.get('policy', approval).get('rules', [])
            for field in sorted(LEGACY_POLICY_KEYS):
                values = before.get(field) or []
                if not values:
                    continue
                if not isinstance(values, list):
                    notes.append({'path': list(path), 'field': field, 'reason': 'legacy configuration requires review'})
                    continue
                for value in values:
                    try:
                        exact = normalize_legacy_workspace_policy({field: [value]})['approval']['rules'][0]
                    except (ValueError, IndexError, TypeError):
                        notes.append({'path': list(path), 'field': field, 'selector': value,
                                      'reason': 'no proven equivalent selector; existing policy retained'})
                        continue
                    imported = {**exact, 'effect': 'ask'}
                    if exact['effect'] == 'deny':
                        for rule in rules:
                            if rule == imported:
                                rule['effect'] = 'deny'
                    elif exact['effect'] == 'allow' and imported in rules:
                        notes.append({'path': list(path), 'field': field, 'selector': value,
                                      'reason': 'automatic permission is not restored without review'})
        if str(before.get('rule_type') or '').strip().lower() in ('deny', 'never_allow', 'block', 'draft_only') and after.get('rule_type') == 'approval_required':
            try:
                _, imported = import_blueprint_governance({}, [before])
                exact = _convert_task_rules([before])[0]
                if after == imported[0]:
                    return exact
            except (ValueError, TypeError):
                notes.append({'path': list(path), 'reason': 'source denial requires explicit selectors; existing source retained'})
        for key in before.keys() & after.keys():
            result[key] = _repair_value(before[key], result[key], notes, (*path, key))
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        result = [_repair_value(old, new, notes, (*path, index)) for index, (old, new) in enumerate(zip(before, after))]
    return result


def plan_repairs(source, audits):
    """Compare audit after-images before making any deterministic repair."""
    rows = {name: {r.get('id', r.get('workspace_id')): r for r in source.get(name, [])} for name in _FIELDS}
    groups = defaultdict(list)
    for audit in audits:
        groups[(audit['resource_type'], audit['resource_id'])].append(audit)
    eligible, candidates, review, skipped = {}, {}, [], []
    for identity, receipts in groups.items():
        table, rid = identity
        if table not in _FIELDS:
            if table == 'hitl_requests':
                for receipt in receipts:
                    old = receipt['details'].get('source') or {}
                    context = old.get('context') or {}
                    if context.get('kind') == 'provider' or 'provider_action_claim' in context:
                        review.append({'table': table, 'id': rid, 'reason': 'historical provider decision requires reconciliation; never revive authorization'})
            continue
        if any(receipt['details'].get('plan_mode') is not None for receipt in receipts):
            skipped.append({'table': table, 'id': rid, 'reason': 'explicit migration plan semantics retained'})
            continue
        row = rows[table].get(rid)
        receipt = receipts[0]
        before = receipt['details'].get('source')
        after = receipt['details'].get('after')
        guarded = (*_FIELDS[table], 'revision')
        if (len(receipts) != 1 or not row or not isinstance(before, dict) or not isinstance(after, dict)
                or before.get('entity_id') != row.get('entity_id')
                or not all(row.get(key) == after.get(key, before.get(key))
                           for key in guarded if key in after or key in before)):
            skipped.append({'table': table, 'id': rid, 'reason': 'missing, ambiguous or subsequently changed audit target'})
            continue
        eligible[identity] = receipt
        values = {}
        for field in _FIELDS[table]:
            if field not in before or field not in after:
                continue
            notes = []
            repaired = _repair_value(before[field], after[field], notes, (field,))
            review.extend({'table': table, 'id': rid, **note} for note in notes)
            if repaired != row[field]:
                values[field] = repaired
        if values:
            candidates[identity] = values

    # Source rules and their live projection form one repair unit. Do not
    # restore a restriction behind a source or policy edited after conversion.
    workspace_ids = {rid for table, rid in candidates if table in ('workspaces', 'governance_policies')}
    for wid in workspace_ids:
        policy_key, source_key = ('governance_policies', wid), ('workspaces', wid)
        if policy_key not in eligible or (source_key in groups and source_key not in eligible):
            candidates.pop(policy_key, None)
            candidates.pop(source_key, None)
            skipped.append({'table': 'workspaces', 'id': wid, 'reason': 'source and policy must both retain their audited state'})
            continue
        if source_key in candidates:
            row = rows['governance_policies'][wid]
            original = rows['workspaces'][wid]['operating_model'] or {}
            policy = deepcopy(candidates.get(policy_key, {}).get('policy', row['policy']))
            binding = policy['approval']['policy']
            projected = [r for r in row['policy']['approval']['policy']['rules'] if r.get('source_kind') == 'workspace_rule']
            if projected != [r.model_dump() for r in compile_workspace_rules(original.get('rules') or [])]:
                candidates.pop(policy_key, None)
                candidates.pop(source_key, None)
                skipped.append({'table': 'workspaces', 'id': wid, 'reason': 'existing source projection mismatch'})
                continue
            repaired = candidates[source_key]['operating_model']
            binding['rules'] = [r for r in binding['rules'] if r.get('source_kind') != 'workspace_rule'] + [
                r.model_dump() for r in compile_workspace_rules(repaired.get('rules') or [])]
            if policy != row['policy']:
                candidates.setdefault(policy_key, {})['policy'] = policy

    history = defaultdict(int)
    for row in source.get('governance_revisions', []):
        history[row['workspace_id']] = max(history[row['workspace_id']], int(row['revision'] or 0))
    operations = []
    for (table, rid), values in sorted(candidates.items()):
        row = rows[table][rid]
        if table in ('governance_policies', 'workspace_blueprint_upgrade_states'):
            values['revision'] = max(int(row['revision'] or 0), history[rid] if table == 'governance_policies' else 0) + 1
        operations.append({'table': table, 'key': 'workspace_id' if table in ('governance_policies', 'workspace_blueprint_upgrade_states') else 'id',
            'id': rid, 'values': values, 'before': deepcopy(row), 'audit_id': eligible[(table, rid)]['id']})
    return {'operations': operations, 'review_required': review, 'skipped': skipped}


def preflight(connection):
    source = read_sources(connection)
    if 'audit_log' not in inspect(connection).get_table_names():
        audits, checked = [], None
    else:
        audit = Table('audit_log', MetaData(), autoload_with=connection)
        checked = connection.execute(select(audit.c.details).where(
            audit.c.action == 'approval_policy.repair_checked', audit.c.resource_id == REVISION)).scalar_one_or_none()
        audits = connection.execute(select(audit).where(audit.c.action == 'approval_policy.migrated')).mappings().all()
    plan = plan_repairs(source, audits) if checked is None else {
        'operations': [], 'review_required': checked['review_required'], 'skipped': checked['skipped']}
    return {**plan, 'source_digest': source_digest(source), 'blockers': [],
            'already_applied': checked is not None, 'audits_present': bool(audits)}


def apply(connection):
    """Run inside Alembic's transaction, including the repair/audit receipts."""
    existing = set(inspect(connection).get_table_names())
    if connection.dialect.name == 'postgresql':
        for name in ('workspaces', 'governance_policies', 'governance_revisions', 'tasks',
                     'workspace_drafts', 'workspace_blueprint_upgrade_states', 'audit_log'):
            if name in existing:
                connection.execute(text(f'LOCK TABLE {name} IN SHARE ROW EXCLUSIVE MODE'))
    plan = preflight(connection)
    if plan['already_applied'] or not plan['audits_present']:
        return {**plan, 'applied': 0}
    metadata = MetaData()
    audit = Table('audit_log', metadata, autoload_with=connection)
    for operation in plan['operations']:
        table = Table(operation['table'], metadata, autoload_with=connection, extend_existing=True)
        connection.execute(table.update().where(table.c[operation['key']] == operation['id']).values(**operation['values']))
        connection.execute(audit.insert().values(id=generate_ulid(), action='approval_policy.repaired',
            entity_id=operation['before'].get('entity_id'), resource_type=operation['table'], resource_id=operation['id'],
            details={'revision': REVISION, 'migration_audit_id': operation['audit_id'],
                     'source': operation['before'], 'after': operation['values']}, created_at=datetime.now(timezone.utc)))
        if operation['table'] == 'governance_policies':
            history = Table('governance_revisions', metadata, autoload_with=connection, extend_existing=True)
            connection.execute(history.insert().values(id=generate_ulid(), workspace_id=operation['id'],
                revision=operation['values']['revision'], policy=operation['values']['policy'],
                change_summary='Restore audited exact denial rules; ambiguous conversions remain under review',
                created_at=datetime.now(timezone.utc)))
    connection.execute(audit.insert().values(id=generate_ulid(), action='approval_policy.repair_checked',
        resource_type='approval_migration', resource_id=REVISION,
        details={'review_required': plan['review_required'], 'skipped': plan['skipped'], 'repaired_rows': len(plan['operations'])},
        created_at=datetime.now(timezone.utc)))
    return {**plan, 'applied': len(plan['operations'])}
