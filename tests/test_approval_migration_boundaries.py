"""Migration repairs must use the existing unified approval contract."""
from copy import deepcopy
import hashlib
import json

import pytest

from packages.core.governance.approval_projection import compile_workspace_rules
from packages.core.migrations.unified_approval import preflight_sources


def source():
    return {
        'entities': [{'id': 'e', 'settings': {}}], 'users': [], 'user_memberships': [],
        'workspaces': [{'id': 'w', 'entity_id': 'e', 'settings': {}, 'operating_model': {}}],
        'governance_policies': [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 1, 'policy': {}}],
        'chat_rooms': [],
    }


@pytest.mark.parametrize('kind', ['deny', 'approval_required', 'auto_approve'])
@pytest.mark.parametrize('location', ['workspace', 'draft', 'baseline', 'task'])
def test_exact_source_rules_convert_without_changing_their_projection(kind, location):
    old = source()
    rules = [{'rule_key': 'send', 'rule_type': kind, 'actions': ['communication.send'],
              'description': 'Original business rule'}]
    if location == 'workspace':
        old['workspaces'][0]['operating_model'] = {'rules': rules, 'goals': ['keep']}
        table = 'workspaces'
    elif location == 'draft':
        old['workspace_drafts'] = [{'id': 'd', 'entity_id': 'e', 'status': 'ready',
            'fields': {'_blueprint_operating_model': {'rules': rules}, 'name': 'Keep'}}]
        table = 'workspace_drafts'
    elif location == 'baseline':
        old['workspace_blueprint_upgrade_states'] = [{'workspace_id': 'w', 'entity_id': 'e',
            'revision': 2, 'restore_point': None, 'baseline': {'configuration': {'entries': {
                'workspace_configuration:operating_model/rules': {
                    'kind': 'workspace_configuration', 'path': ['operating_model', 'rules'],
                    'source_content': {'value': rules}, 'installed_content': {'value': rules}}}}}}]
        table = 'workspace_blueprint_upgrade_states'
    else:
        old['tasks'] = [{'id': 't', 'entity_id': 'e', 'details': {'runtime_context': {'rules': rules}}}]
        table = 'tasks'
    frozen = deepcopy(old)
    plan = preflight_sources(old)
    assert not plan['blockers']
    values = next(op['values'] for op in plan['operations'] if op['table'] == table)
    if location == 'workspace':
        converted = values['operating_model']['rules']
        assert values['operating_model']['goals'] == ['keep']
    elif location == 'draft':
        converted = values['fields']['_blueprint_operating_model']['rules']
        assert values['fields']['name'] == 'Keep'
    elif location == 'baseline':
        entries = values['baseline']['configuration']['entries']
        entry = entries['workspace_configuration:operating_model/rules']
        converted = entry['source_content']['value']
        assert entry['installed_content']['value'] == converted
        assert values['revision'] == 3
    else:
        converted = values['details']['runtime_context']['rules']
    assert 'actions' not in converted[0]
    assert converted[0]['approval_selectors'] == [{'kind': 'action', 'action_key': 'communication.send'}]
    assert compile_workspace_rules(converted) == compile_workspace_rules(rules)
    assert old == frozen


@pytest.mark.parametrize('extra', [
    {'patterns': ['other.send']},
    {'approval_selectors': [{'kind': 'action', 'action_key': 'other.send'}]},
])
def test_migration_never_discards_conflicting_source_rule_aliases(extra):
    old = source()
    old['tasks'] = [{'id': 't', 'entity_id': 'e', 'details': {'runtime_context': {'rules': [
        {'rule_type': 'deny', 'actions': ['communication.send'], **extra}]}}}]
    assert preflight_sources(old)['blockers']


def pending_step():
    old = source()
    old['execution_steps'] = [{'id': 's', 'entity_id': 'e', 'workspace_id': 'w', 'plan_id': 'p',
        'action_key': 'communication.send', 'params': {'to': 'alice'}, 'approval_operation_revision': 0}]
    old['hitl_requests'] = [{'id': 'h', 'entity_id': 'e', 'workspace_id': 'w', 'status': 'pending',
        'hitl_type': 'authorize', 'origin_kind': 'step', 'origin_step_id': 's', 'origin_plan_id': 'p',
        'action_key': 'communication.send', 'dedup_key': 'old', 'context': {}}]
    return old


EXPIRY = {'request:h': {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}}


@pytest.mark.parametrize('action_key', ['communication.send', None])
def test_unverifiable_step_can_be_explicitly_expired_without_resuming_or_changing_execution(action_key):
    old = pending_step()
    old['hitl_requests'][0]['action_key'] = action_key
    old['execution_steps'][0]['action_key'] = action_key
    frozen = deepcopy(old)
    assert preflight_sources(old)['blockers']
    plan = preflight_sources(old, resolutions=EXPIRY)
    assert not plan['blockers']
    op = next(op for op in plan['operations'] if op['table'] == 'hitl_requests')
    assert op['values'] == EXPIRY['request:h']
    assert op['reconciliation'] == EXPIRY['request:h']
    assert not any(op['table'] in {'tasks', 'execution_steps', 'execution_plans'} for op in plan['operations'])
    assert old == frozen


@pytest.mark.parametrize('task_link', ['request', 'plan'])
def test_generic_expiry_does_not_bypass_task_lifecycle_reconciliation(task_link):
    old = pending_step()
    if task_link == 'request':
        old['hitl_requests'][0]['origin_task_id'] = 't'
    else:
        old['execution_plans'] = [{'id': 'p', 'entity_id': 'e', 'workspace_id': 'w', 'task_id': 't'}]
    plan = preflight_sources(old, resolutions=EXPIRY)
    assert any(issue['scope'] == 'request:h' for issue in plan['blockers'])
    assert not any(op['table'] == 'hitl_requests' for op in plan['operations'])


@pytest.mark.parametrize('case', ['verified', 'mismatched_digest', 'foreign_owner', 'missing_step', 'provider_claim'])
def test_expiry_cannot_discard_a_verified_or_ambiguous_step(case):
    old = pending_step()
    if case == 'verified':
        old['hitl_requests'][0]['context']['args_hash'] = hashlib.sha256(
            json.dumps({'to': 'alice'}, sort_keys=True, default=str).encode()).hexdigest()
    elif case == 'mismatched_digest':
        old['hitl_requests'][0]['context']['args_hash'] = 'different-intent'
    elif case == 'foreign_owner':
        old['execution_steps'][0]['entity_id'] = 'other'
    elif case == 'missing_step':
        old['execution_steps'] = []
    else:
        old['hitl_requests'][0]['context']['provider_action_claim'] = {'status': 'ambiguous'}
    plan = preflight_sources(old, resolutions=EXPIRY)
    assert plan['blockers']
    assert not any(op['table'] == 'hitl_requests' for op in plan['operations'])


@pytest.mark.parametrize('status', ['pending', 'granted'])
def test_expiry_cannot_hide_a_provider_effect_claim(status):
    old = source()
    old['hitl_requests'] = [{'id': 'h', 'status': status, 'hitl_type': 'authorize',
        'origin_kind': 'tool_call', 'context': {'provider_action_claim': {'status': 'ambiguous'}}}]
    plan = preflight_sources(old, resolutions=EXPIRY)
    assert plan['blockers']
    assert not any(op['table'] == 'hitl_requests' for op in plan['operations'])


def test_verified_entity_step_keeps_its_existing_operation_identity():
    old = pending_step()
    old['execution_steps'][0]['workspace_id'] = None
    request = old['hitl_requests'][0]
    request['workspace_id'] = None
    request['context']['args_hash'] = hashlib.sha256(
        json.dumps({'to': 'alice'}, sort_keys=True, default=str).encode()).hexdigest()
    plan = preflight_sources(old)
    assert not plan['blockers']
    op = next(op for op in plan['operations'] if op['table'] == 'hitl_requests')
    assert op['values']['context']['operation_id'] == 'step:p:s'
    assert 'status' not in op['values']


@pytest.mark.parametrize('with_blocker', [False, True])
def test_preflight_cli_check_is_read_only_and_does_not_print_private_configuration(
    tmp_path, monkeypatch, capsys, with_blocker,
):
    import sqlalchemy as sa
    from scripts.preflight_approval_upgrade import main
    database = tmp_path / 'check.sqlite'
    url = f'sqlite:///{database}'
    engine = sa.create_engine(url)
    entities = sa.Table('entities', sa.MetaData(), sa.Column('id', sa.String, primary_key=True),
                        sa.Column('settings', sa.JSON))
    settings = {'private': 'do-not-print'}
    if with_blocker:
        settings['approval_defaults'] = {'revision': 0, 'policy': {'mode': 'unknown'}}
    with engine.begin() as connection:
        entities.create(connection)
        connection.execute(entities.insert().values(id='private-owner', settings=settings))
    monkeypatch.setenv('DATABASE_URL_SYNC', url)
    monkeypatch.setattr('sys.argv', ['preflight', '--check'])
    if with_blocker:
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
    else:
        main()
    output = capsys.readouterr().out
    assert json.loads(output)['blockers'] == int(with_blocker)
    assert 'do-not-print' not in output and 'private-owner' not in output
    with engine.connect() as connection:
        assert connection.execute(sa.select(entities.c.settings)).scalar_one() == settings
    engine.dispose()


def test_preflight_report_restricts_an_existing_file(tmp_path, monkeypatch):
    from scripts.preflight_approval_upgrade import main
    output = tmp_path / 'plan.json'
    output.touch(mode=0o644)
    output.chmod(0o644)
    monkeypatch.setenv('DATABASE_URL_SYNC', 'sqlite:///:memory:')
    monkeypatch.setattr('sys.argv', ['preflight', '--output', str(output)])
    main()
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())['operations'] == []
