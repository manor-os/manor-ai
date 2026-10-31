from copy import deepcopy

from packages.core.governance.approval_policy_migration import normalize_legacy_workspace_policy
from packages.core.governance.approval_projection import import_blueprint_governance, compile_workspace_rules
from tests.fixtures.approval_v1.approval_policy_migration import (
    normalize_legacy_workspace_policy as v1_normalize_legacy_workspace_policy,
)


def fixture_source():
    old = {'never_allow_actions': ['ticket.create', 'billing.*'],
           'auto_approve_actions': ['email.send'], 'max_risk_level': 'medium'}
    # The audited after-image is what the historical conversion wrote.
    converted = v1_normalize_legacy_workspace_policy(old, root_entity_id='e', safe_import=True)
    source = {'governance_policies': [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 8, 'policy': converted}],
        'workspaces': [{'id': 'w', 'entity_id': 'e', 'settings': {}, 'operating_model': {}}],
        'governance_revisions': [{'workspace_id': 'w', 'revision': 8}]}
    audit = {'id': 'a', 'resource_type': 'governance_policies', 'resource_id': 'w', 'details': {
        'source_digest': 'original',
        'source': {'workspace_id': 'w', 'entity_id': 'e', 'revision': 7, 'policy': old},
        'after': deepcopy(source['governance_policies'][0])}}
    return source, [audit]


def test_forward_repair_restores_only_proven_exact_denials():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    source, audits = fixture_source()
    frozen = deepcopy(source)
    plan = plan_repairs(source, audits)
    assert len(plan['operations']) == 1
    values = plan['operations'][0]['values']
    rules = values['policy']['approval']['policy']['rules']
    assert [(r['effect'], r['selector']['action_key']) for r in rules if r['selector']['kind'] == 'action'] == [
        ('deny', 'ticket.create'), ('ask', 'email.send')]
    assert all(r['effect'] == 'ask' for r in rules if r['selector']['kind'] == 'category')
    assert values['policy']['max_risk_level'] == 'medium'
    assert values['revision'] == 9
    assert plan['review_required']
    assert source == frozen


def test_new_operator_revision_and_missing_audit_are_never_overwritten():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    source, audits = fixture_source()
    assert not plan_repairs(source, [])['operations']
    source['governance_policies'][0]['revision'] += 1
    plan = plan_repairs(source, audits)
    assert not plan['operations']
    assert plan['skipped']


def test_explicitly_accepted_conversion_semantics_are_not_reinterpreted():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    for mode in ('new_semantics_conservative_v1', 'future_explicit_plan'):
        source, audits = fixture_source()
        audits[0]['details']['plan_mode'] = mode
        plan = plan_repairs(source, audits)
        assert not plan['operations']
        assert plan['skipped'][0]['reason'] == 'explicit migration plan semantics retained'


def test_unrelated_workspace_settings_do_not_block_or_get_overwritten():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    source, audits = fixture_source()
    audits.append({'id': 'b', 'resource_type': 'workspaces', 'resource_id': 'w', 'details': {
        'source': {'id': 'w', 'entity_id': 'e', 'operating_model': {}, 'settings': {}},
        'after': {'operating_model': {}, 'settings': {'theme': 'old'}}}})
    source['workspaces'][0]['settings'] = {'theme': 'new'}
    plan = plan_repairs(source, audits)
    assert len(plan['operations']) == 1
    assert plan['operations'][0]['table'] == 'governance_policies'
    assert source['workspaces'][0]['settings'] == {'theme': 'new'}


def test_exact_source_denial_and_its_projection_are_repaired_together():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    source, audits = fixture_source()
    original = [{'rule_key': 'block_ticket', 'rule_type': 'deny', 'actions': ['ticket.create'], 'description': 'Keep'}]
    _, imported = import_blueprint_governance({}, original)
    source['workspaces'][0]['operating_model'] = {'rules': imported}
    projected = [r.model_dump() for r in compile_workspace_rules(imported)]
    source['governance_policies'][0]['policy']['approval']['policy']['rules'].extend(projected)
    audits[0]['details']['after'] = deepcopy(source['governance_policies'][0])
    audits.append({'id': 'b', 'resource_type': 'workspaces', 'resource_id': 'w', 'details': {
        'source_digest': 'original', 'source': {'id': 'w', 'entity_id': 'e', 'operating_model': {'rules': original}},
        'after': {'operating_model': deepcopy(source['workspaces'][0]['operating_model'])}}})
    plan = plan_repairs(source, audits)
    by_table = {op['table']: op['values'] for op in plan['operations']}
    restored = by_table['workspaces']['operating_model']['rules']
    assert restored[0]['rule_type'] == 'deny'
    assert restored[0]['description'] == 'Keep'
    assert restored[0]['approval_selectors'] == [{'kind': 'action', 'action_key': 'ticket.create'}]
    actual = [r for r in by_table['governance_policies']['policy']['approval']['policy']['rules'] if r['source_kind'] == 'workspace_rule']
    assert actual == [r.model_dump() for r in compile_workspace_rules(restored)]
    source['workspaces'][0]['operating_model']['rules'][0]['description'] = 'Edited later'
    assert not plan_repairs(source, audits)['operations'], 'protect both source and live policy'


def test_correct_unified_migration_and_intentional_unified_rules_remain_unchanged():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    source, audits = fixture_source()
    old = {'never_allow_actions': ['ticket.create']}
    correct = normalize_legacy_workspace_policy(old, root_entity_id='e')
    source['governance_policies'][0]['policy'] = correct
    audits[0]['details']['source']['policy'] = old
    audits[0]['details']['after'] = deepcopy(source['governance_policies'][0])
    assert not plan_repairs(source, audits)['operations']
    audits[0]['details']['source']['policy'] = deepcopy(correct)
    assert not plan_repairs(source, audits)['operations']


def test_snapshot_rules_are_repaired_and_revision_advances_without_resuming_requests():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    original = {'rules': [{'rule_key': 'block', 'rule_type': 'deny', 'actions': ['email.delete']}]}
    _, converted = import_blueprint_governance({}, original['rules'])
    old = {'configuration': {'entries': {'rules': {'source_content': {'value': original['rules']}}}}}
    after = {'configuration': {'entries': {'rules': {'source_content': {'value': converted}}}}}
    row = {'workspace_id': 'w', 'entity_id': 'e', 'revision': 2, 'baseline': after, 'restore_point': after}
    source = {'workspace_blueprint_upgrade_states': [row]}
    audits = [{'id': 'a', 'resource_type': 'workspace_blueprint_upgrade_states', 'resource_id': 'w', 'details': {
        'source': {**row, 'revision': 1, 'baseline': old, 'restore_point': old}, 'after': deepcopy(row)}}]
    plan = plan_repairs(source, audits)
    values = plan['operations'][0]['values']
    assert values['revision'] == 3
    assert values['baseline']['configuration']['entries']['rules']['source_content']['value'][0]['rule_type'] == 'deny'
    assert all(op['table'] != 'hitl_requests' for op in plan['operations'])


def test_flattened_blueprint_policy_snapshots_restore_exact_denials_too():
    from packages.core.migrations.approval_conversion_repair import plan_repairs
    prefix = ['operating_model', 'governance']
    raw = {'never_allow_actions': ['email.delete', 'billing.*']}
    rules = normalize_legacy_workspace_policy(raw, safe_import=True)['approval']['rules']
    before = {'configuration': {'entries': {'legacy': {'kind': 'workspace_configuration',
        'path': [*prefix, 'never_allow_actions'], 'installed_content': {'value': raw['never_allow_actions']}}}}}
    after = {'configuration': {'entries': {'unified': {'kind': 'workspace_configuration',
        'path': [*prefix, 'approval', 'policy', 'rules'], 'installed_content': {'value': rules}}}}}
    row = {'workspace_id': 'w', 'entity_id': 'e', 'revision': 2, 'baseline': after, 'restore_point': None}
    audits = [{'id': 'a', 'resource_type': 'workspace_blueprint_upgrade_states', 'resource_id': 'w', 'details': {
        'source': {**row, 'baseline': before, 'revision': 1}, 'after': deepcopy(row)}}]
    plan = plan_repairs({'workspace_blueprint_upgrade_states': [row]}, audits)
    values = plan['operations'][0]['values']
    restored = values['baseline']['configuration']['entries']['unified']['installed_content']['value']
    assert restored[0]['effect'] == 'deny'
    assert all(rule['effect'] == 'ask' for rule in restored[1:])
    assert values['revision'] == 3


def test_database_repair_is_atomic_audited_and_replay_safe():
    import pytest
    import sqlalchemy as sa

    from packages.core.migrations.approval_conversion_repair import apply, preflight

    source, audits = fixture_source()
    metadata = sa.MetaData()
    policies = sa.Table('governance_policies', metadata,
        sa.Column('workspace_id', sa.String, primary_key=True), sa.Column('entity_id', sa.String),
        sa.Column('revision', sa.Integer), sa.Column('policy', sa.JSON))
    workspaces = sa.Table('workspaces', metadata,
        sa.Column('id', sa.String, primary_key=True), sa.Column('entity_id', sa.String),
        sa.Column('settings', sa.JSON), sa.Column('operating_model', sa.JSON))
    history = sa.Table('governance_revisions', metadata,
        sa.Column('id', sa.String, primary_key=True), sa.Column('workspace_id', sa.String),
        sa.Column('revision', sa.Integer), sa.Column('policy', sa.JSON),
        sa.Column('change_summary', sa.String), sa.Column('created_at', sa.DateTime))
    audit = sa.Table('audit_log', metadata,
        sa.Column('id', sa.String, primary_key=True), sa.Column('entity_id', sa.String),
        sa.Column('action', sa.String), sa.Column('resource_type', sa.String),
        sa.Column('resource_id', sa.String), sa.Column('details', sa.JSON), sa.Column('created_at', sa.DateTime))
    engine = sa.create_engine('sqlite://')
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(policies.insert(), source['governance_policies'])
            connection.execute(workspaces.insert(), source['workspaces'])
            connection.execute(history.insert(), [{**source['governance_revisions'][0], 'id': 'h'}])
            connection.execute(audit.insert(), [{**audits[0], 'action': 'approval_policy.migrated'}])

        def snapshot():
            with engine.connect() as connection:
                return {table.name: connection.execute(sa.select(table)).mappings().all()
                        for table in metadata.sorted_tables}

        before = snapshot()
        with pytest.raises(RuntimeError, match='injected failure'):
            with engine.begin() as connection:
                assert apply(connection)['applied'] == 1
                raise RuntimeError('injected failure')
        assert snapshot() == before

        with engine.begin() as connection:
            assert apply(connection)['applied'] == 1
        repaired = snapshot()
        assert repaired['governance_policies'][0]['revision'] == 9
        assert len(repaired['governance_revisions']) == 2
        assert repaired['governance_revisions'][-1]['policy'] == repaired['governance_policies'][0]['policy']
        assert repaired['audit_log'][0] == before['audit_log'][0]
        assert [row['action'] for row in repaired['audit_log']] == [
            'approval_policy.migrated', 'approval_policy.repaired', 'approval_policy.repair_checked']
        assert repaired['audit_log'][-1]['details']['review_required']
        with engine.begin() as connection:
            assert preflight(connection)['already_applied']
            assert apply(connection)['applied'] == 0
        assert snapshot() == repaired
    finally:
        engine.dispose()
