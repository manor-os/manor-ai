"""Unified defaults, sparse inheritance, target matching and CAS contracts."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import JSON, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from packages.core.governance.approval_policy import (
    ActionFacts, ApprovalScope, ApprovalTarget, PolicyDraft,
    RuleDraft, evaluate_policy,
)
from packages.core.governance import approval_policy_service as service
from packages.core.governance.approval_policy_migration import (
    LegacyApprovalConversionError, normalize_legacy_workspace_policy,
)
from packages.core.models.audit import AuditLog
from packages.core.models.user import Entity, User


@compiles(JSONB, 'sqlite')
def compile_jsonb_sqlite(type_, compiler, **kw):
    return compiler.visit_JSON(JSON(), **kw)


def chain(root_mode='risk_based', local_mode=None, root_rules=(), local_rules=(), root_revision=1, local_revision=1):
    return service.build_policy_chain(scope=ApprovalScope(kind='personal', id='u', entity_id='e'),
        root={'revision': root_revision, 'policy': {'mode': root_mode, 'rules': list(root_rules)}},
        local={'revision': local_revision, 'root_entity_id': 'e', 'policy': {'mode': local_mode, 'rules': list(local_rules)}})


def rule(id, effect, selector):
    return {'id': id, 'effect': effect, 'selector': selector, 'source_kind': 'user'}


@pytest.mark.parametrize('mode,impact,expected', [
    ('confirm_changes', 'read', 'allow'), ('confirm_changes', 'reversible', 'ask'),
    ('risk_based', 'reversible', 'allow'), ('risk_based', 'external', 'ask'),
    ('automatic', 'external', 'allow'), ('automatic', 'destructive', 'allow'),
    ('automatic', 'unknown', 'allow'), ('confirm_changes', 'unknown', 'ask'),
    ('risk_based', 'unknown', 'ask'),
])
def test_mode_baseline(mode, impact, expected):
    assert evaluate_policy(chain(root_mode=mode), ActionFacts(action_key='test', impact=impact)).outcome == expected


def test_root_updates_followers_but_not_custom_mode():
    facts = ActionFacts(action_key='test', impact='external')
    assert evaluate_policy(chain(root_mode='automatic'), facts).outcome == 'allow'
    assert evaluate_policy(chain(root_mode='automatic', local_mode='confirm_changes'), facts).outcome == 'ask'
    assert chain(root_revision=2).local.version != chain(root_revision=1).local.version
    assert chain().local.policy.mode is None


@pytest.mark.parametrize('kind', ['personal', 'workspace', 'room'])
@pytest.mark.parametrize('root_mode', ['confirm_changes', 'risk_based', 'automatic'])
def test_local_snapshot_exposes_root_mode_before_resetting_saved_override(kind, root_mode):
    current = service.build_policy_chain(
        scope=ApprovalScope(kind=kind, id='local', entity_id='e'),
        root={'revision': 3, 'policy': {'mode': root_mode, 'rules': []}},
        local={'revision': 7, 'root_entity_id': 'e', 'policy': {'mode': 'confirm_changes', 'rules': []}},
    )
    before = service._response(current, {})
    assert before['effective_policy']['mode'] == 'confirm_changes'
    assert before['inherited_mode'] == root_mode
    reset = service.apply_draft(current, PolicyDraft(mode=None, user_rules=[]))
    after = service._response(reset, {})
    assert after['policy']['mode'] is None
    assert after['effective_policy']['mode'] == after['inherited_mode'] == root_mode
    assert service._response(current, {}) == before, 'preview must not mutate the saved snapshot'


def test_account_snapshot_has_no_inherited_mode():
    current = service.build_policy_chain(
        scope=ApprovalScope(kind='account', id='e', entity_id='e'),
        root=service.account_defaults(),
    )
    assert service._response(current, {})['inherited_mode'] is None


def test_root_deny_cannot_be_overridden_by_more_specific_local_allow():
    selector = {'kind': 'resource', 'action_key': 'send', 'resource_kind': 'recipient', 'account_id': 'mail-1', 'resource_ids': ['alice']}
    result = evaluate_policy(chain(root_rules=[rule('root-deny', 'deny', {'kind': 'category', 'category': 'communication'})],
        local_rules=[rule('local-allow', 'allow', selector)]), ActionFacts(action_key='send', category='communication', impact='external',
        targets=[ApprovalTarget(account_id='mail-1', resource_kind='recipient', resource_id='alice')]))
    assert (result.outcome, result.rule_id, result.scope.kind) == ('deny', 'root-deny', 'account')


def test_resource_rules_match_accounts_and_every_target():
    resource = {'kind': 'resource', 'action_key': 'send', 'resource_kind': 'recipient', 'account_id': 'mail-1', 'resource_ids': ['alice']}
    policy = chain(local_rules=[rule('alice-only', 'allow', resource)])
    for targets in [
        [ApprovalTarget(account_id='mail-2', resource_kind='recipient', resource_id='alice')],
        [ApprovalTarget(account_id='mail-1', resource_kind='recipient', resource_id='alice'), ApprovalTarget(account_id='mail-1', resource_kind='recipient', resource_id='bob')],
    ]:
        result = evaluate_policy(policy, ActionFacts(action_key='send', impact='external', targets=targets))
        assert result.outcome == 'ask'
    assert evaluate_policy(policy, ActionFacts(action_key='send', impact='external', targets=[ApprovalTarget(account_id='mail-1', resource_kind='recipient', resource_id='alice')])).outcome == 'allow'


def test_specificity_then_locality_then_ask():
    category = {'kind': 'category', 'category': 'communication'}
    action = {'kind': 'action', 'action_key': 'send'}
    facts = ActionFacts(action_key='send', category='communication', impact='external')
    assert evaluate_policy(chain(root_rules=[rule('specific', 'ask', action)], local_rules=[rule('general', 'allow', category)]), facts).outcome == 'ask'
    assert evaluate_policy(chain(root_rules=[rule('root', 'ask', action)], local_rules=[rule('local', 'allow', action)]), facts).outcome == 'allow'
    assert evaluate_policy(chain(local_rules=[rule('a', 'allow', action), rule('b', 'ask', action)]), facts).outcome == 'ask'


def test_following_reset_preserves_derived_rules_and_cannot_spoof_sources():
    derived = {'id': 'source-1', 'effect': 'ask', 'selector': {'kind': 'action', 'action_key': 'send'}, 'source_kind': 'workspace_rule', 'source_id': 'operating-rule'}
    current = chain(local_mode='automatic', local_rules=[derived, rule('user', 'allow', {'kind': 'action', 'action_key': 'edit'})])
    reset = service.apply_draft(current, PolicyDraft(mode=None, user_rules=[]))
    assert [row.id for row in reset.local.policy.rules] == ['source-1']
    with pytest.raises(ValueError, match='editable'):
        service.apply_draft(current, PolicyDraft(mode=None, user_rules=[RuleDraft(id='source-1', effect='allow', selector={'kind': 'action', 'action_key': 'send'})]))
    with pytest.raises(ValidationError):
        RuleDraft(effect='allow', selector={'kind': 'action', 'action_key': 'send'}, source_kind='workspace_rule')


def test_missing_configuration_never_falls_back():
    with pytest.raises(service.ApprovalPolicyIntegrityError):
        service.build_policy_chain(scope=ApprovalScope(kind='personal', id='u', entity_id='e'), root=service.account_defaults())
    with pytest.raises(service.ApprovalPolicyIntegrityError):
        service.build_policy_chain(scope=ApprovalScope(kind='personal', id='u', entity_id='e'), root=service.account_defaults(), local=service.local_defaults('other'))


@pytest.mark.parametrize('value', ['file.*', 'file?edit', '*', 'send#resource:recipient'])
def test_live_migration_never_silently_weakens_legacy_patterns(value):
    with pytest.raises(LegacyApprovalConversionError):
        normalize_legacy_workspace_policy({'never_allow_actions': [value]}, root_entity_id='e')


def test_import_is_explicit_review_projection():
    converted = normalize_legacy_workspace_policy(None, safe_import=True)
    assert all(row['effect'] == 'ask' for row in converted['approval']['rules'])
    assert 'unknown' in {row['selector']['category'] for row in converted['approval']['rules']}
    with pytest.raises(LegacyApprovalConversionError):
        normalize_legacy_workspace_policy(None, root_entity_id='e')


@pytest.mark.asyncio
async def test_update_preserves_other_preferences_and_checks_root_version(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Entity.__table__.create(sync))
        await connection.run_sync(lambda sync: User.__table__.create(sync))
        await connection.run_sync(lambda sync: AuditLog.__table__.create(sync))
    capabilities = {'can_view': True, 'can_edit_root': True, 'can_edit_local': True, 'can_approve_once': False, 'can_grant_run': False, 'can_save_rule': True}
    monkeypatch.setattr(service, '_capabilities', AsyncMock(return_value=capabilities))
    async with AsyncSession(engine) as db:
        db.add(Entity(id='e', name='Account', settings={'approval_defaults': service.account_defaults(), 'unrelated': 'keep'}))
        db.add(User(id='u', entity_id='e', email='u@example.test', password_hash='none', preferences={'color': 'teal', 'approval_policy': {'e': service.local_defaults('e'), 'other': service.local_defaults('other')}}))
        await db.commit()
        actor = SimpleNamespace(id='u', entity_id='e')
        local_scope = ApprovalScope(kind='personal', id='u', entity_id='e')
        root_scope = ApprovalScope(kind='account', id='e', entity_id='e')
        local_before = await service.read_approval_settings(db, local_scope, actor)
        root_before = await service.read_approval_settings(db, root_scope, actor)
        await service.update_approval_policy(db, root_scope, PolicyDraft(mode='automatic'), root_before['version'], actor)
        with pytest.raises(service.ApprovalPolicyConflict):
            await service.update_approval_policy(db, local_scope, PolicyDraft(mode='confirm_changes'), local_before['version'], actor)
        local_fresh = await service.read_approval_settings(db, local_scope, actor)
        assert local_fresh['effective_policy']['mode'] == 'automatic'
        await service.update_approval_policy(db, local_scope, PolicyDraft(mode='confirm_changes'), local_fresh['version'], actor)
        preferences = (await db.execute(select(User.preferences).where(User.id == 'u'))).scalar_one()
        assert preferences['color'] == 'teal'
        assert preferences['approval_policy']['other'] == service.local_defaults('other')
        assert preferences['approval_policy']['e']['revision'] == 2
        assert len((await db.execute(select(AuditLog))).scalars().all()) == 2
        assert (await db.execute(select(Entity.settings))).scalar_one()['unrelated'] == 'keep'
    await engine.dispose()


@pytest.mark.asyncio
async def test_generic_settings_cannot_write_approval_branches():
    from packages.core.services.settings_service import update_entity_settings, update_user_preferences
    db = SimpleNamespace()
    with pytest.raises(service.ProtectedApprovalSettingsError):
        await update_entity_settings(db, 'e', {'approval_defaults': {}})
    for key in ['approval_policy', *service.LEGACY_APPROVAL_PREFERENCES]:
        with pytest.raises(service.ProtectedApprovalSettingsError):
            await update_user_preferences(db, 'u', {key: {}})


@pytest.mark.asyncio
async def test_api_conflict_keeps_typed_current_snapshot(monkeypatch):
    from apps.api.routers import approval_settings as api
    from fastapi import HTTPException
    snapshot = {'version': 'current'}
    monkeypatch.setattr(api, 'update_approval_policy', AsyncMock(side_effect=service.ApprovalPolicyConflict(snapshot)))
    with pytest.raises(HTTPException) as caught:
        await api._call(object(), SimpleNamespace(entity_id='e', id='u'), kind='account', scope_id='e',
            request=api.PolicyWriteRequest(expected_version='stale', draft={'mode': 'automatic', 'user_rules': []}))
    assert caught.value.status_code == 409
    assert caught.value.detail == {'code': 'approval_policy_conflict', 'current': snapshot}
    with pytest.raises(ValidationError):
        api.PolicyWriteRequest(expected_version='stale', draft={'mode': 'automatic', 'user_rules': []}, expected_revision=1)


def migration_source():
    return {
        'entities': [{'id': 'e', 'settings': {'color': 'teal'}}],
        'users': [{'id': 'u', 'entity_id': 'e', 'preferences': {'locale': 'zh'}}],
        'user_memberships': [{'user_id': 'u', 'entity_id': 'e'}],
        'workspaces': [{'id': 'w', 'entity_id': 'e', 'settings': {}, 'operating_model': {}}],
        'governance_policies': [{'workspace_id': 'w', 'entity_id': 'e', 'policy': {'hitl_required_actions': ['communication.send'], 'max_risk_level': 'high'}, 'revision': 7}],
        'chat_rooms': [{'id': 'r', 'owner_entity_id': None, 'owner_user_id': 'u', 'approval_policy': None, 'ai_file_actions_direct_enabled': False}],
    }


def test_migration_preserves_owners_defaults_and_legacy_file_review():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    plan = preflight_sources(source)
    assert not plan['blockers']
    operations = {row['table']: row for row in plan['operations']}
    assert operations['entities']['values']['settings']['color'] == 'teal'
    personal = operations['users']['values']['preferences']['approval_policy']['e']['policy']
    assert personal['mode'] is None
    assert {row['effect'] for row in personal['rules']} == {'ask'}
    assert operations['governance_policies']['values']['revision'] == 8
    assert operations['chat_rooms']['values']['approval_policy']['root_entity_id'] == 'e'
    assert operations['chat_rooms']['values']['approval_policy']['policy']['mode'] == 'confirm_changes'
    assert all('ai_tool_execution_revision' not in row['values'] for row in plan['operations'])
    assert source == migration_source(), 'preflight must be read-only'


def test_migration_includes_missing_rows_and_stale_rule_sources():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['governance_policies'] = []
    assert preflight_sources(source)['blockers'], 'missing old row had DEFAULT_POLICY capability grants'
    source = migration_source()
    source['workspaces'][0]['operating_model'] = {'rules': [{'rule_key': 'publish', 'action_patterns': ['*.publish']}]}
    assert any('approval_selectors' in row['reason'] for row in preflight_sources(source)['blockers'])


def test_migration_reviewed_replacement_removes_all_legacy_runtime_fields():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['governance_policies'] = []
    source['chat_rooms'][0]['ai_file_actions_direct_enabled'] = True
    resolutions = {
        'workspace:w': {'policy': {'approval': {'root_entity_id': 'e', 'policy': {'mode': None, 'rules': []}}}, 'operating_model': {}},
        'room:r': {'mode': None, 'rules': []},
    }
    plan = preflight_sources(source, resolutions=resolutions)
    assert not plan['blockers']
    assert next(row for row in plan['operations'] if row['table'] == 'governance_policies')['insert']
    assert next(row for row in plan['operations'] if row['table'] == 'chat_rooms')['values']['ai_file_actions_direct_enabled'] is False


@pytest.mark.asyncio
async def test_root_realtime_event_is_after_commit_and_dropped_on_rollback(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    publish = AsyncMock()
    monkeypatch.setattr(service, '_publish_root_changed', publish)
    async with AsyncSession(engine) as db:
        await db.begin()
        service._queue_root_changed(db, 'e', 1)
        assert not publish.called
        await db.rollback()
        assert not publish.called
        await db.begin()
        service._queue_root_changed(db, 'e', 2)
        await db.commit()
        import asyncio
        await asyncio.sleep(0)
        publish.assert_awaited_once_with('e', 2)
    await engine.dispose()


@pytest.mark.asyncio
async def test_permission_uses_current_membership_role_not_stale_auth_overlay(monkeypatch):
    from packages.core.models.user import UserMembership
    from packages.core import permissions
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: UserMembership.__table__.create(sync))
    seen = []
    async def admin(db, actor):
        seen.append(actor.role)
        return actor.role == 'owner'
    monkeypatch.setattr(permissions, 'user_is_effective_entity_admin', admin)
    async with AsyncSession(engine) as db:
        db.add(UserMembership(id='m', user_id='u', entity_id='e', role='member', status='active'))
        await db.flush()
        caps = await service._capabilities(db, ApprovalScope(kind='account', id='e', entity_id='e'), SimpleNamespace(id='u', entity_id='e', role='owner'))
        assert seen == ['member']
        assert caps['can_edit_root'] is False
        assert caps['can_save_rule'] is False
    await engine.dispose()


def test_migration_apply_is_atomic_fenced_and_repeatable():
    from sqlalchemy import create_engine, update
    from packages.core.models.chat_room import ChatRoom
    from packages.core.models.governance import GovernancePolicy, GovernanceRevision
    from packages.core.models.user import UserMembership
    from packages.core.migrations.unified_approval import apply, preflight
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        for model in (Entity, User, UserMembership, GovernancePolicy, GovernanceRevision, ChatRoom, AuditLog):
            model.__table__.create(connection)
        from sqlalchemy import Table, Column, MetaData, String
        workspace_table = Table('workspaces', MetaData(), Column('id', String, primary_key=True), Column('entity_id', String),
            Column('name', String), Column('settings', JSON), Column('operating_model', JSON))
        workspace_table.create(connection)
        connection.execute(Entity.__table__.insert().values(id='e', name='Account', settings={'color': 'teal'}))
        connection.execute(User.__table__.insert().values(id='u', entity_id='e', email='u@example.test', password_hash='x', preferences={}))
        connection.execute(UserMembership.__table__.insert().values(id='m', user_id='u', entity_id='e', role='owner'))
        connection.execute(workspace_table.insert().values(id='w', entity_id='e', name='Work', settings={}, operating_model={}))
        connection.execute(GovernancePolicy.__table__.insert().values(workspace_id='w', entity_id='e', revision=1, policy={'hitl_required_actions': ['communication.send']}))
        connection.execute(ChatRoom.__table__.insert().values(id='r', kind='group', owner_user_id='u'))
        plan = preflight(connection)
        assert not plan['blockers']
        connection.execute(update(Entity).where(Entity.id == 'e').values(settings={'color': 'changed'}))
        with pytest.raises(ValueError, match='source changed'):
            apply(connection, plan)
        assert connection.execute(select(GovernancePolicy.policy)).scalar_one() == {'hitl_required_actions': ['communication.send']}
        reviewed = preflight(connection)
        assert apply(connection, reviewed)['applied'] == len(reviewed['operations'])
        assert apply(connection, reviewed)['applied'] == 0
        assert not preflight(connection)['operations']
        assert len(connection.execute(select(AuditLog)).all()) == len(reviewed['operations'])
        assert connection.execute(select(ChatRoom.ai_tool_execution_revision)).scalar_one() == 0
    engine.dispose()


@pytest.mark.parametrize('rule_type', [
    'auto_approve', 'allow', 'exception', 'approval_required', 'hitl_required',
    'require_approval', 'review_required', 'deny', 'never_allow', 'block', 'draft_only',
])
@pytest.mark.parametrize('alias', ['action_patterns', 'actions', 'patterns'])
def test_exact_source_conversion_preserves_compiled_effect_identity_and_selectors(rule_type, alias):
    from copy import deepcopy
    from packages.core.governance.approval_projection import compile_workspace_rules
    from packages.core.migrations.unified_approval import _convert_source_rules
    source = {'rules': [{'rule_key': 'operator-choice', 'rule_type': rule_type,
        alias: ['external_message.send', 'workspace.file.modify'], 'description': 'Preserve my choice'}]}
    original = deepcopy(source)
    converted = _convert_source_rules(source)
    assert compile_workspace_rules(converted['rules']) == compile_workspace_rules(source['rules'])
    assert converted['rules'][0]['rule_type'] == rule_type
    assert alias not in converted['rules'][0]
    assert source == original
    assert _convert_source_rules(converted) == converted


@pytest.mark.parametrize('effect', ['allow', 'approval_required', 'deny'])
@pytest.mark.parametrize('selector', [
    {'actions': ['external.*']}, {'actions': ['send#resource:private']},
    {'capabilities': ['file.write']}, {'actions': ['send'], 'capabilities': ['file.write']},
])
def test_source_conversion_never_guesses_capability_or_glob_equivalence(effect, selector):
    from packages.core.migrations.unified_approval import _convert_source_rules
    with pytest.raises(LegacyApprovalConversionError):
        _convert_source_rules({'rules': [{'rule_type': effect, **selector}]})


@pytest.mark.parametrize('location', ['workspace', 'draft', 'baseline'])
def test_exact_source_rules_convert_in_all_restorable_configuration_sources(location):
    from copy import deepcopy
    from packages.core.migrations.unified_approval import preflight_sources
    from packages.core.governance.approval_projection import compile_workspace_rules
    source = migration_source()
    rules = [{'rule_key': 'keep-review', 'rule_type': 'approval_required', 'actions': ['external_message.send']}]
    if location == 'workspace':
        source['workspaces'][0]['operating_model'] = {'rules': rules, 'other': {'keep': True}}
        table = 'workspaces'
    elif location == 'draft':
        source['workspace_drafts'] = [{'id': 'd', 'entity_id': 'e', 'status': 'ready',
            'fields': {'_blueprint_operating_model': {'rules': rules}, 'other': {'keep': True}}}]
        table = 'workspace_drafts'
    else:
        source['workspace_blueprint_upgrade_states'] = [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 1, 'restore_point': None,
            'baseline': {'configuration': {'entries': {'rules': {'kind': 'workspace_configuration',
                'path': ['operating_model', 'rules'], 'source_content': {'value': rules}}}}, 'other': {'keep': True}}}]
        table = 'workspace_blueprint_upgrade_states'
    original = deepcopy(source)
    plan = preflight_sources(source)
    assert not plan['blockers']
    values = next(item['values'] for item in plan['operations'] if item['table'] == table)
    if location == 'workspace':
        converted = values['operating_model']['rules']
        assert values['operating_model']['other'] == {'keep': True}
    elif location == 'draft':
        converted = values['fields']['_blueprint_operating_model']['rules']
        assert values['fields']['other'] == {'keep': True}
    else:
        converted = values['baseline']['configuration']['entries']['rules']['source_content']['value']
        assert values['baseline']['other'] == {'keep': True}
        assert values['revision'] == 2
    assert compile_workspace_rules(converted) == compile_workspace_rules(rules)
    assert source == original


def test_source_conversion_preserves_only_previously_verified_draft_ban_fingerprints():
    from packages.core.migrations.unified_approval import _convert_source_rules
    from packages.core.services.workspace_setup_service import normalize_workspace_draft_governance, workspace_rule_fingerprint
    verified = {'rule_key': 'human-ban', 'rule_type': 'deny', 'actions': ['external_message.send'], 'description': '禁止发送'}
    unverified = {'rule_key': 'unverified', 'rule_type': 'deny', 'actions': ['workspace.file.modify']}
    source = {'rules': [verified, unverified], '_explicit_hard_block_rules': {
        'human-ban': workspace_rule_fingerprint(verified), 'unverified': 'not-an-exact-receipt'}}
    converted = _convert_source_rules(source)
    assert converted['_explicit_hard_block_rules']['human-ban'] == workspace_rule_fingerprint(converted['rules'][0])
    assert converted['_explicit_hard_block_rules']['unverified'] == 'not-an-exact-receipt'
    normalized = normalize_workspace_draft_governance(converted)
    assert normalized['rules'][0]['rule_type'] == 'deny'
    assert normalized['rules'][1]['rule_type'] == 'approval_required'


def test_source_conversion_does_not_rewrite_historical_restore_points():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['workspace_blueprint_upgrade_states'] = [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 1, 'baseline': {},
        'restore_point': {'rules': [{'rule_key': 'old', 'rule_type': 'deny', 'actions': ['send']}]}}]
    assert any(item['scope'] == 'upgrade_state:w' for item in preflight_sources(source)['blockers'])


@pytest.mark.parametrize('status', ['pending', 'approved'])
def test_receipt_reconciliation_requires_explicit_review_and_preserves_source(status):
    from copy import deepcopy
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['chat_room_tool_approvals'] = [{'id': 'receipt', 'room_id': 'r', 'status': status, 'approval_requirements': None}]
    receipt = {'status': status, 'paths': ['/docs/report.md'], 'continuation': {'tool': 'write'}, 'decided_by': 'human'}
    source['conversations'] = [{'id': 'c', 'entity_id': 'e', 'metadata': {'file_approvals': {'f': receipt}, 'other': 'keep'}}]
    original = deepcopy(source)
    assert len(preflight_sources(source)['blockers']) == 2
    resolutions = {
        'room_request:receipt': {'status': 'invalidated', 'resolved_reason': 'approval_migration_unverifiable'},
        'file_request:c:f': {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'},
    }
    plan = preflight_sources(source, resolutions=resolutions)
    assert not plan['blockers']
    room = next(item for item in plan['operations'] if item['table'] == 'chat_room_tool_approvals')
    assert room['values'] == {'status': 'invalidated'}
    assert room['reconciliation'] == resolutions['room_request:receipt']
    conversation = next(item for item in plan['operations'] if item['table'] == 'conversations')
    assert conversation['values']['metadata'] == {'other': 'keep', 'file_approvals': {'f': {**receipt, **resolutions['file_request:c:f']}}}
    assert source == original


@pytest.mark.parametrize('kind', ['room', 'file'])
@pytest.mark.parametrize('invalid', ['verified', 'terminal', 'claimed', 'wrong_status', 'extra_fields', 'unknown'])
def test_receipt_resolution_rejects_unreviewed_changes_and_verified_or_used_receipts(kind, invalid):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    room = {'id': 'receipt', 'room_id': 'r', 'status': 'approved', 'approval_requirements': None}
    file = {'status': 'approved'}
    source['chat_room_tool_approvals'] = [room]
    source['conversations'] = [{'id': 'c', 'entity_id': 'e', 'metadata': {'file_approvals': {'f': file}}}]
    scope = 'room_request:receipt' if kind == 'room' else 'file_request:c:f'
    request = room if kind == 'room' else file
    resolution = {'status': 'invalidated' if kind == 'room' else 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    if invalid == 'verified':
        request.update(approval_requirements=[], operation_id='stable', approval_action_key='workspace.file.modify')
    elif invalid == 'terminal':
        request['status'] = 'succeeded' if kind == 'room' else 'consumed'
    elif invalid == 'claimed':
        request['claimed_at' if kind == 'room' else 'consumed_at'] = '2026-10-01'
    elif invalid == 'wrong_status':
        resolution['status'] = 'approved'
    elif invalid == 'extra_fields':
        resolution['force'] = True
    elif invalid == 'unknown':
        scope += '-missing'
    plan = preflight_sources(source, resolutions={scope: resolution})
    assert any(item['scope'] == scope for item in plan['blockers'])
    assert not any(item['table'] in ('chat_room_tool_approvals', 'conversations') for item in plan['operations'])


def test_room_and_file_reconciliation_is_atomic_fenced_audited_and_repeatable():
    from copy import deepcopy
    from datetime import datetime
    from sqlalchemy import create_engine, update
    from packages.core.models.chat_room import ChatRoom, ChatRoomAgentRun, ChatRoomMessage, ChatRoomToolApproval
    from packages.core.models.governance import GovernanceRevision
    from packages.core.models.task import Conversation
    from packages.core.migrations.unified_approval import apply, preflight
    engine = create_engine('sqlite:///:memory:')
    file_receipt = {'status': 'approved', 'paths': ['/docs/report.md'], 'decided_by_user_id': 'human',
        'continuation': {'tool': 'write', 'arguments': {'content': 'original'}}, 'consumed_at': None}
    metadata = {'file_approvals': {'f': file_receipt, 'peer': {'status': 'consumed'}}, 'other': 'keep'}
    with engine.begin() as connection:
        for model in (Entity, User, ChatRoom, ChatRoomMessage, ChatRoomAgentRun, ChatRoomToolApproval, Conversation, GovernanceRevision, AuditLog):
            model.__table__.create(connection)
        connection.execute(Entity.__table__.insert().values(id='e', name='Account', settings={'approval_defaults': service.account_defaults()}))
        connection.execute(ChatRoom.__table__.insert().values(id='r', kind='group', owner_entity_id='e',
            approval_policy=service.local_defaults('e')))
        connection.execute(ChatRoomToolApproval.__table__.insert().values(id='receipt', room_id='r', run_id='run',
            requester_user_id='u', binding_id='b', audience_fingerprint='audience', ai_tool_execution_revision=1,
            tool_call_id='call', tool_name='write', arguments_hash='original', display_summary='Original mutation',
            status='approved', decided_by_user_id='human', decided_at=datetime(2026, 10, 1)))
        connection.execute(Conversation.__table__.insert().values(id='c', entity_id='e', metadata=metadata))
    resolutions = {
        'room_request:receipt': {'status': 'invalidated', 'resolved_reason': 'approval_migration_unverifiable'},
        'file_request:c:f': {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'},
    }
    with engine.connect() as connection:
        with connection.begin():
            plan = preflight(connection, resolutions=resolutions)
            assert not plan['blockers']
        with pytest.raises(RuntimeError, match='rollback'):
            with connection.begin():
                apply(connection, plan)
                raise RuntimeError('rollback both receipt updates')
        with connection.begin():
            assert connection.execute(select(ChatRoomToolApproval.status)).scalar_one() == 'approved'
            assert connection.execute(select(Conversation.meta)).scalar_one() == metadata
            assert not connection.execute(select(AuditLog.id)).all()
            connection.execute(update(ChatRoomToolApproval).values(arguments_hash='changed'))
            with pytest.raises(ValueError, match='source changed'):
                apply(connection, plan)
            connection.execute(update(ChatRoomToolApproval).values(arguments_hash='original'))
            stale_metadata = deepcopy(metadata)
            stale_metadata['file_approvals']['f']['continuation']['arguments']['content'] = 'changed'
            connection.execute(update(Conversation).values(meta=stale_metadata))
            with pytest.raises(ValueError, match='source changed'):
                apply(connection, plan)
            connection.execute(update(Conversation).values(meta=metadata))
            assert apply(connection, plan)['applied'] == 2
            assert apply(connection, plan)['applied'] == 0
            audits = {item['resource_type']: item['details'] for item in connection.execute(select(AuditLog.__table__)).mappings()}
            room = audits['chat_room_tool_approvals']
            assert room['source']['status'] == 'approved'
            assert room['after'] == {**room['source'], 'status': 'invalidated'}
            assert room['reconciliation'] == resolutions['room_request:receipt']
            conversation = audits['conversations']
            expected_metadata = deepcopy(metadata)
            expected_metadata['file_approvals']['f'].update(resolutions['file_request:c:f'])
            assert conversation['after'] == {**conversation['source'], 'metadata': expected_metadata}
            assert conversation['source']['metadata'] == metadata
    engine.dispose()


def failed_task_approval_source():
    source = migration_source()
    source['tasks'] = [{'id': 't', 'entity_id': 'e', 'workspace_id': 'w', 'status': 'failed', 'details': {}}]
    source['execution_plans'] = [{'id': 'p', 'entity_id': 'e', 'workspace_id': 'w', 'task_id': 't', 'status': 'running'}]
    source['execution_steps'] = [{'id': 's', 'entity_id': 'e', 'workspace_id': 'w', 'plan_id': 'p',
        'action_key': 'external_message.send', 'step_status': 'waiting_human', 'params': {}}]
    source['hitl_requests'] = [{'id': 'h', 'entity_id': 'e', 'workspace_id': 'w', 'origin_kind': 'step',
        'origin_task_id': 't', 'origin_plan_id': 'p', 'origin_step_id': 's', 'action_key': 'external_message.send',
        'status': 'pending', 'hitl_type': 'authorize', 'context': {'continuation': {'keep': True}}}]
    return source


def test_failed_task_request_retirement_needs_specific_review_and_never_resumes():
    from copy import deepcopy
    from packages.core.migrations.unified_approval import preflight_sources
    source = failed_task_approval_source()
    original = deepcopy(source)
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source)['blockers'])
    ordinary_resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source, resolutions={'request:h': ordinary_resolution})['blockers'])
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_failed_task_request'}
    plan = preflight_sources(source, resolutions={'request:h': resolution})
    assert not plan['blockers']
    operation = next(item for item in plan['operations'] if item['table'] == 'hitl_requests')
    assert operation['values'] == operation['reconciliation'] == resolution
    assert not any(item['table'] in ('execution_plans', 'execution_steps', 'tasks') for item in plan['operations'])
    assert source == original


@pytest.mark.parametrize('table,key,value', [
    ('tasks', 'status', 'running'), ('tasks', 'status', 'completed'), ('tasks', 'entity_id', 'other'),
    ('tasks', 'workspace_id', 'other'), ('execution_plans', 'task_id', 'other'),
    ('execution_plans', 'entity_id', 'other'), ('execution_plans', 'workspace_id', 'other'),
    ('execution_plans', 'status', 'completed'), ('execution_steps', 'plan_id', 'other'),
    ('execution_steps', 'entity_id', 'other'), ('execution_steps', 'workspace_id', 'other'),
    ('execution_steps', 'action_key', 'other'), ('execution_steps', 'step_status', 'running'),
    ('hitl_requests', 'origin_task_id', None), ('hitl_requests', 'origin_plan_id', None),
    ('hitl_requests', 'origin_step_id', None), ('hitl_requests', 'origin_kind', 'tool_call'),
    ('hitl_requests', 'status', 'granted'), ('hitl_requests', 'hitl_type', 'input'),
    ('hitl_requests', 'context', {'operation_id': 'stable'}), ('hitl_requests', 'context', {'args_hash': 'known'}),
    ('hitl_requests', 'context', {'kind': 'provider'}), ('hitl_requests', 'consumed_at', '2026-10-01'),
])
def test_failed_task_request_retirement_rejects_bound_active_or_mismatched_lifecycle(table, key, value):
    from packages.core.migrations.unified_approval import preflight_sources
    source = failed_task_approval_source()
    source[table][0][key] = value
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_failed_task_request'}
    plan = preflight_sources(source, resolutions={'request:h': resolution})
    assert any(item['scope'] == 'request:h' for item in plan['blockers'])
    assert not any(item['table'] == 'hitl_requests' for item in plan['operations'])


@pytest.mark.parametrize('status', ['pending', 'granted'])
def test_unverifiable_request_resolution_cannot_retire_an_already_consumed_decision(status):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['hitl_requests'] = [{'id': 'h', 'status': status, 'hitl_type': 'authorize',
        'origin_kind': 'tool_call', 'context': {}, 'consumed_at': '2026-10-01'}]
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    plan = preflight_sources(source, resolutions={'request:h': resolution})
    assert any(item['scope'] == 'request:h' for item in plan['blockers'])
    assert not any(item['table'] == 'hitl_requests' for item in plan['operations'])


def test_active_approval_migration_requires_persisted_semantic_binding():
    import hashlib
    import json
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['execution_steps'] = [{'id': 's', 'entity_id': 'e', 'workspace_id': 'w', 'plan_id': 'p', 'action_key': 'send', 'params': {'to': 'alice'}}]
    source['hitl_requests'] = [{'id': 'h', 'entity_id': 'e', 'workspace_id': 'w', 'action_key': 'send', 'origin_kind': 'step', 'origin_step_id': 's', 'origin_plan_id': 'p', 'status': 'granted', 'hitl_type': 'authorize', 'dedup_key': 'step:s', 'context': {}}]
    blocked = preflight_sources(source)
    assert any(item['scope'] == 'request:h' for item in blocked['blockers'])
    source['hitl_requests'][0]['context']['approval_requirements'] = []
    source['hitl_requests'][0]['context']['args_hash'] = hashlib.sha256(json.dumps({'to': 'alice'}, sort_keys=True, default=str).encode()).hexdigest()
    plan = preflight_sources(source)
    assert not plan['blockers']
    update = next(item for item in plan['operations'] if item['table'] == 'hitl_requests')
    assert update['values']['dedup_key'] == 'operation:step:p:s'
    assert update['values']['context']['operation_id'] == 'step:p:s'
    assert 'status' not in update['values'], 'migration cannot approve or consume a request'
    source['execution_steps'][0]['params'] = {'to': 'bob'}
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source)['blockers'])


def test_upgrade_baseline_and_active_draft_are_converted_before_cutover():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['workspace_drafts'] = [{'id': 'd', 'entity_id': 'e', 'status': 'ready', 'fields': {'name': 'Draft', 'governance_policy': {'hitl_required_actions': ['send']}}}]
    source['workspace_blueprint_upgrade_states'] = [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 2,
        'baseline': {'version': 1, 'entries': {'governance:policy': {'kind': 'governance', 'source_content': {'hitl_required_actions': ['send']}, 'installed_content': {'hitl_required_actions': ['send']}}}},
        'restore_point': None}]
    plan = preflight_sources(source)
    assert not plan['blockers']
    assert next(item for item in plan['operations'] if item['table'] == 'workspace_drafts')['values']['fields']['governance_policy']['approval']['rules']
    state = next(item for item in plan['operations'] if item['table'] == 'workspace_blueprint_upgrade_states')['values']
    assert state['revision'] == 3
    assert 'hitl_required_actions' not in state['baseline']['entries']['governance:policy']['source_content']
    source['workspace_blueprint_upgrade_states'][0]['restore_point'] = {'items': [{'before': {'auto_approve_capabilities': ['file.write']}}]}
    assert any(item['scope'] == 'upgrade_state:w' for item in preflight_sources(source)['blockers'])


def test_real_nested_configuration_baseline_preserves_siblings_and_invalidates_review():
    from copy import deepcopy
    from packages.core.blueprints.configuration_upgrade import configuration_fingerprint, _governance_content
    from packages.core.migrations.unified_approval import preflight_sources
    old = {'max_risk_level': 'high', 'budget_caps_per_kind': {}, 'hitl_required_actions': ['send']}
    entry = {'key': 'governance:policy', 'kind': 'governance', 'identity': 'policy',
        'source_content': deepcopy(old), 'installed_content': deepcopy(old), 'blueprint_content': deepcopy(old)}
    baseline = {'components': {'version': 1, 'entries': {'agent:writer': {'source': {'name': 'Writer'}}}},
        'configuration': {'version': 1, 'entries': {'governance:policy': entry}},
        'runtime': {'source': {'contract': {}}, 'installed': {'contract': {}}}}
    source = migration_source()
    source['workspace_blueprint_upgrade_states'] = [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 4,
        'baseline': baseline, 'restore_point': None}]
    plan = preflight_sources(source)
    assert not plan['blockers']
    values = next(item['values'] for item in plan['operations'] if item['table'] == 'workspace_blueprint_upgrade_states')
    assert values['revision'] == 5
    assert values['baseline']['components'] == baseline['components']
    assert values['baseline']['runtime'] == baseline['runtime']
    converted = values['baseline']['configuration']['entries']['governance:policy']
    for key in ('source_content', 'installed_content', 'blueprint_content'):
        assert converted[key] == _governance_content(converted[key])
        assert configuration_fingerprint(converted[key]) != configuration_fingerprint(old)
    assert baseline['configuration']['entries']['governance:policy']['source_content'] == old


def test_operating_governance_is_converted_alongside_live_policy():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['workspaces'][0]['operating_model'] = {'governance': source['governance_policies'][0]['policy']}
    plan = preflight_sources(source)
    assert not plan['blockers']
    workspace = next(item for item in plan['operations'] if item['table'] == 'workspaces')
    governance = workspace['values']['operating_model']['governance']
    assert governance['approval']['root_entity_id'] == 'e'
    assert 'hitl_required_actions' not in governance


@pytest.mark.parametrize('alias', ['patterns', 'capability_ids'])
@pytest.mark.parametrize('location', ['workspace', 'draft', 'baseline'])
def test_source_rule_aliases_require_reviewed_conversion(alias, location):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    rules = [{'rule_key': 'legacy', 'rule_type': 'approval_required', alias: ['legacy.*']}]
    if location == 'workspace':
        source['workspaces'][0]['operating_model'] = {'rules': rules}
        scope = 'workspace:w'
    elif location == 'draft':
        source['workspace_drafts'] = [{'id': 'd', 'entity_id': 'e', 'status': 'ready', 'fields': {'_blueprint_operating_model': {'rules': rules}}}]
        scope = 'draft:d'
    else:
        source['workspace_blueprint_upgrade_states'] = [{'workspace_id': 'w', 'entity_id': 'e', 'revision': 1, 'restore_point': None,
            'baseline': {'configuration': {'version': 1, 'entries': {'workspace_configuration:operating_model/rules': {
                'kind': 'workspace_configuration', 'path': ['operating_model', 'rules'], 'source_content': {'value': rules}}}}}}]
        scope = 'upgrade_state:w'
    assert any(item['scope'] == scope and 'no proven equivalent canonical selector' in item['reason'] for item in preflight_sources(source)['blockers'])


@pytest.mark.parametrize('kind', ['personal', 'workspace', 'room'])
def test_existing_binding_must_match_persisted_owner(kind):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    if kind == 'personal':
        source['users'][0]['preferences']['approval_policy'] = {'e': service.local_defaults('another-root')}
        scope = 'personal:u:e'
    elif kind == 'workspace':
        source['governance_policies'][0]['policy'] = {'approval': {'root_entity_id': 'another-root', 'policy': {'mode': None, 'rules': []}}}
        scope = 'workspace:w'
    else:
        source['chat_rooms'][0]['approval_policy'] = service.local_defaults('another-root')
        scope = 'room:r'
    assert any(item['scope'] == scope and 'root binding' in item['reason'] for item in preflight_sources(source)['blockers'])


def test_provider_migration_preserves_provider_transaction_dedup_identity():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    context = {'kind': 'provider', 'provider': 'stripe', 'provider_approval_id': 'transaction-1',
        'operation_id': 'provider:stripe:transaction-1', 'approval_requirements': []}
    source['hitl_requests'] = [{'id': 'h', 'entity_id': 'e', 'workspace_id': 'w', 'action_key': 'stripe.action',
        'origin_kind': 'tool_call', 'status': 'pending', 'hitl_type': 'authorize',
        'dedup_key': 'provider:stripe:transaction-1', 'context': context}]
    plan = preflight_sources(source)
    assert not plan['blockers']
    assert not [item for item in plan['operations'] if item['table'] == 'hitl_requests']
    del context['operation_id']
    plan = preflight_sources(source)
    operation = next(item for item in plan['operations'] if item['table'] == 'hitl_requests')
    assert operation['values']['dedup_key'] == 'provider:stripe:transaction-1'
    assert operation['values']['context']['operation_id'] == 'provider:stripe:transaction-1'


@pytest.mark.parametrize('status', ['pending', 'approved'])
def test_active_room_receipts_without_historical_requirements_block_upgrade(status):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['chat_room_tool_approvals'] = [{'id': 'receipt', 'room_id': 'r', 'status': status, 'approval_requirements': None}]
    assert any(item['scope'] == 'room_request:receipt' for item in preflight_sources(source)['blockers'])
    source['chat_room_tool_approvals'][0]['approval_requirements'] = []
    assert not preflight_sources(source)['blockers']


def test_task_runtime_exact_rules_convert_and_ambiguous_rules_require_resolution():
    from copy import deepcopy
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    details = {'unrelated': {'keep': True}, 'runtime_context': {'rules': [
        {'rule_key': 'r', 'rule_type': 'approval_required', 'actions': ['external_message.send'], 'description': 'Review messages'},
        {'rule_key': 'guidance', 'rule_type': 'guidance', 'description': 'Use a friendly tone'},
    ]}}
    source['tasks'] = [{'id': 't', 'entity_id': 'e', 'details': deepcopy(details)}]
    plan = preflight_sources(source)
    assert not plan['blockers']
    converted = next(item['values']['details'] for item in plan['operations'] if item['table'] == 'tasks')
    assert converted['unrelated'] == details['unrelated']
    assert converted['runtime_context']['rules'][0]['approval_selectors'] == [{'kind': 'action', 'action_key': 'external_message.send'}]
    assert 'actions' not in converted['runtime_context']['rules'][0]
    assert converted['runtime_context']['rules'][1] == details['runtime_context']['rules'][1]
    source['tasks'][0]['details']['runtime_context']['rules'][0]['actions'] = ['external_message.*']
    assert any(item['scope'] == 'task:t' for item in preflight_sources(source)['blockers'])
    resolved = preflight_sources(source, resolutions={'task:t': converted})
    assert not resolved['blockers']
    assert next(item['values']['details'] for item in resolved['operations'] if item['table'] == 'tasks') == converted


def test_step_intent_column_default_does_not_invalidate_preupgrade_source_digest():
    from sqlalchemy import Column, MetaData, String, Table, create_engine, text
    from packages.core.migrations.unified_approval import read_sources
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        table = Table('execution_steps', MetaData(), Column('id', String, primary_key=True), Column('params', JSON))
        table.create(connection)
        connection.execute(table.insert().values(id='step', params={}))
        before = read_sources(connection)
        connection.execute(text('ALTER TABLE execution_steps ADD COLUMN approval_operation_revision INTEGER NOT NULL DEFAULT 0'))
        assert before == read_sources(connection)
        assert before['execution_steps'][0]['approval_operation_revision'] == 0
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner_kind', ['entity', 'personal'])
async def test_room_load_rechecks_owner_after_scope_resolution(owner_kind):
    from sqlalchemy import update
    from packages.core.models.chat_room import ChatRoom
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        for model in (Entity, User, ChatRoom):
            await connection.run_sync(lambda sync, model=model: model.__table__.create(sync))
    async with AsyncSession(engine) as db:
        db.add_all([Entity(id=key, name=key, settings={'approval_defaults': service.account_defaults()}) for key in ('e', 'other')])
        db.add(User(id='owner', entity_id='e', email='owner@test', password_hash='x'))
        db.add(ChatRoom(id='r', kind='group', owner_entity_id='e' if owner_kind == 'entity' else None,
            owner_user_id='owner' if owner_kind == 'personal' else None, approval_policy=service.local_defaults('e')))
        await db.flush()
        scope = await service.resolve_scope(db, entity_id='e', room_id='r')
        if owner_kind == 'entity':
            await db.execute(update(ChatRoom).where(ChatRoom.id == 'r').values(owner_entity_id='other'))
        else:
            await db.execute(update(User).where(User.id == 'owner').values(entity_id='other'))
        for lock in (False, True):
            with pytest.raises(service.ApprovalPolicyIntegrityError, match='owner binding'):
                await service._load(db, scope, lock=lock)
        assert (await db.execute(select(ChatRoom.approval_policy))).scalar_one() == service.local_defaults('e')
    await engine.dispose()


@pytest.mark.asyncio
async def test_external_room_member_can_only_read_shared_effective_policy():
    from datetime import datetime, timezone
    from packages.core.models.chat_room import ChatRoom, ChatRoomMember
    from packages.core.models.user import UserMembership
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        for model in (Entity, User, UserMembership, ChatRoom, ChatRoomMember):
            await connection.run_sync(lambda sync, model=model: model.__table__.create(sync))
    async with AsyncSession(engine) as db:
        db.add(Entity(id='e', name='Shared owner', settings={'private_setting': 'never-return', 'approval_defaults': service.account_defaults()}))
        db.add(ChatRoom(id='r', kind='group', owner_entity_id='e', approval_policy=service.local_defaults('e')))
        db.add(ChatRoomMember(id='m', room_id='r', user_id='external', role='member', status='active', visible_from=datetime.now(timezone.utc)))
        await db.flush()
        actor = SimpleNamespace(id='external', entity_id='outside', role='owner')
        scope = await service.resolve_scope(db, entity_id='outside', room_id='r')
        response = await service.read_approval_settings(db, scope, actor)
        assert response['scope'] == {'kind': 'room', 'id': 'r', 'entity_id': 'e'}
        assert response['effective_policy']['mode'] == service.account_defaults()['policy']['mode']
        assert not any(response['capabilities'][key] for key in ('can_edit_root', 'can_edit_local', 'can_save_rule'))
        assert 'never-return' not in str(response)
        for method in (service.preview_approval_policy, service.update_approval_policy):
            with pytest.raises(service.ApprovalPolicyAccessError):
                await method(db, scope, PolicyDraft(mode='automatic'), response['version'], actor)
        with pytest.raises(service.ApprovalPolicyAccessError):
            await service.read_approval_settings(db, ApprovalScope(kind='account', id='e', entity_id='e'), actor)
        with pytest.raises(service.ApprovalPolicyAccessError):
            await service.read_approval_settings(db, scope, SimpleNamespace(id='stranger', entity_id='outside', role='owner'))
    await engine.dispose()


@pytest.mark.parametrize('status', ['pending', 'approved'])
def test_active_file_approval_needs_existing_operation_and_consent_evidence(status):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    receipt = {'status': status, 'paths': ['/docs/report.md']}
    source['conversations'] = [{'id': 'c', 'entity_id': 'e', 'metadata': {'file_approvals': {'file-r': receipt}, 'unrelated': 'keep'}}]
    assert any(item['scope'] == 'file_request:c:file-r' for item in preflight_sources(source)['blockers'])
    receipt.update(operation_id='file:execution-1', approval_action_key='workspace.file.modify', approval_requirements=[])
    plan = preflight_sources(source)
    assert not plan['blockers']
    assert not any(item['table'] == 'conversations' for item in plan['operations'])
    source['conversations'][0]['metadata']['file_approvals']['duplicate'] = dict(receipt)
    assert any('multiple active file' in item['reason'] for item in preflight_sources(source)['blockers'])


@pytest.mark.parametrize('invalid', [
    {'auto_approve_actions': []}, {'max_risk_level': 'invalid'},
    {'budget_caps_per_kind': {'action': -1}}, {'unexpected_policy_field': True},
])
def test_workspace_resolution_validates_complete_policy_contract(invalid):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    replacement = {'policy': {'approval': {'root_entity_id': 'e', 'policy': {'mode': None, 'rules': []}}, **invalid}, 'operating_model': {}}
    plan = preflight_sources(source, resolutions={'workspace:w': replacement})
    assert any(item['scope'] == 'workspace:w' for item in plan['blockers'])
    assert not any(item['table'] == 'governance_policies' for item in plan['operations'])


@pytest.mark.parametrize('invalid', [
    {'max_risk_level': 'invalid'}, {'budget_caps_per_kind': {'action': True}}, {'unexpected_policy_field': 'retired'},
    [],
])
def test_operating_governance_copy_validates_complete_policy_contract(invalid):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['workspaces'][0]['operating_model'] = {'governance': invalid}
    plan = preflight_sources(source)
    assert any(item['scope'] == 'workspace:w' for item in plan['blockers'])
    assert not any(item['table'] in ('governance_policies', 'workspaces') for item in plan['operations'])


@pytest.mark.parametrize('history_revision,blocked', [(6, False), (7, False), (8, True)])
def test_governance_history_ahead_of_live_policy_blocks_preflight(history_revision, blocked):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['governance_revisions'] = [{'id': 'history', 'workspace_id': 'w', 'revision': history_revision}]
    plan = preflight_sources(source)
    assert any(item['scope'] == 'workspace:w' and 'history revision is ahead' in item['reason'] for item in plan['blockers']) is blocked
    if blocked:
        assert not any(item['table'] == 'governance_policies' for item in plan['operations'])


def test_missing_live_policy_with_existing_history_requires_reconciliation():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['governance_policies'] = []
    source['governance_revisions'] = [{'id': 'history', 'workspace_id': 'w', 'revision': 1}]
    replacement = {'policy': {'approval': {'root_entity_id': 'e', 'policy': {'mode': None, 'rules': []}}}, 'operating_model': {}}
    plan = preflight_sources(source, resolutions={'workspace:w': replacement})
    assert any(item['scope'] == 'workspace:w' and 'history revision is ahead' in item['reason'] for item in plan['blockers'])


def test_history_reconciliation_requires_explicit_true_and_preserves_history():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['governance_revisions'] = [{'id': 'history', 'workspace_id': 'w', 'revision': 12}]
    replacement = {'policy': {'approval': {'root_entity_id': 'e', 'policy': {'mode': None, 'rules': []}}}, 'operating_model': {}}
    for value in (None, False, 1, 'true'):
        candidate = {**replacement, **({'reconcile_history': value} if value is not None else {})}
        assert preflight_sources(source, resolutions={'workspace:w': candidate})['blockers']
    plan = preflight_sources(source, resolutions={'workspace:w': {**replacement, 'reconcile_history': True}})
    assert not plan['blockers']
    operation = next(item for item in plan['operations'] if item['table'] == 'governance_policies')
    assert operation['values']['revision'] == 13
    assert operation['reconciliation'] == {'reconcile_history': True, 'live_revision': 7, 'history_revision': 12}
    assert not any(item['table'] == 'governance_revisions' for item in plan['operations'])
    assert preflight_sources(source, resolutions={'workspace:w': {**replacement, 'reconcile_history': True, 'force': True}})['blockers']


@pytest.mark.parametrize('status', ['denied', 'consumed', 'expired'])
def test_expiry_resolution_cannot_change_other_request_states(status):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['hitl_requests'] = [{'id': 'h', 'status': status, 'hitl_type': 'authorize', 'context': {}}]
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source, resolutions={'request:h': resolution})['blockers'])


@pytest.mark.parametrize('resolution', [
    {'status': 'granted', 'resolved_reason': 'approval_migration_unverifiable'},
    {'status': 'expired', 'resolved_reason': 'other'},
    {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable', 'context': {}},
])
def test_expiry_resolution_rejects_unknown_fields_or_other_decisions(resolution):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['hitl_requests'] = [{'id': 'h', 'status': 'granted', 'hitl_type': 'authorize', 'context': {}}]
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source, resolutions={'request:h': resolution})['blockers'])


def test_expiry_resolution_cannot_discard_a_verified_grant_or_unknown_scope():
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['hitl_requests'] = [{'id': 'h', 'status': 'granted', 'hitl_type': 'authorize', 'context': {'approval_requirements': []}}]
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source, resolutions={'request:h': resolution})['blockers'])
    assert any(item['reason'].startswith('Unknown') for item in preflight_sources(migration_source(), resolutions={'request:missing': resolution})['blockers'])


@pytest.mark.parametrize('origin_kind', ['tool_call', 'operation'])
def test_unbound_pending_authorization_requires_explicit_expiry(origin_kind):
    from copy import deepcopy
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    request = {'id': 'h', 'status': 'pending', 'hitl_type': 'authorize', 'origin_kind': origin_kind,
        'context': {'continuation': {'tool': 'send', 'arguments': {'recipient': 'alice'}}, 'args_hash': 'old'},
        'decided_by_user_id': None, 'consumed_at': None}
    source['hitl_requests'] = [request]
    original = deepcopy(source)
    assert any(item['scope'] == 'request:h' for item in preflight_sources(source)['blockers'])
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    plan = preflight_sources(source, resolutions={'request:h': resolution})
    assert not plan['blockers']
    operation = next(item for item in plan['operations'] if item['table'] == 'hitl_requests')
    assert operation['values'] == resolution
    assert operation['reconciliation'] == resolution
    assert source == original, 'preflight cannot change a decision, continuation or resume state'


@pytest.mark.parametrize('request_fields', [
    {'origin_kind': 'tool_call', 'context': {'operation_id': 'tool:stable'}},
    {'origin_kind': 'operation', 'context': {'operation_id': 'operation:stable'}},
    {'origin_kind': 'tool_call', 'context': {'kind': 'provider'}},
    {'origin_kind': 'step', 'context': {}},
    {'origin_kind': 'tool_call', 'hitl_type': 'input', 'context': {}},
    {'origin_kind': 'tool_call', 'hitl_type': 'choice', 'context': {}},
    {'origin_kind': 'tool_call', 'hitl_type': 'error', 'context': {}},
])
def test_expiry_resolution_rejects_bound_pending_requests_and_other_hitl_lanes(request_fields):
    from packages.core.migrations.unified_approval import preflight_sources
    source = migration_source()
    source['hitl_requests'] = [{'id': 'h', 'status': 'pending', 'hitl_type': 'authorize', **request_fields}]
    resolution = {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}
    plan = preflight_sources(source, resolutions={'request:h': resolution})
    assert any(item['scope'] == 'request:h' for item in plan['blockers'])
    assert not any(item['table'] == 'hitl_requests' for item in plan['operations'])


@pytest.mark.parametrize('request_status', ['pending', 'granted'])
def test_reviewed_reconciliation_is_atomic_fenced_audited_and_repeatable(request_status):
    from datetime import datetime
    from sqlalchemy import Column, MetaData, String, Table, create_engine, update
    from packages.core.models.governance import GovernancePolicy, GovernanceRevision
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.migrations.unified_approval import apply, preflight
    engine = create_engine('sqlite:///:memory:')
    workspace_table = Table('workspaces', MetaData(), Column('id', String, primary_key=True),
        Column('entity_id', String), Column('settings', JSON), Column('operating_model', JSON))
    policy = {'approval': {'root_entity_id': 'e', 'policy': {'mode': None, 'rules': []}}, 'max_risk_level': 'medium', 'budget_caps_per_kind': {'action': 100}}
    continuation = {'continuation': {'tool': 'send', 'arguments': {'recipient': 'alice'}}, 'args_hash': 'original'}
    approver = 'human' if request_status == 'granted' else None
    decided_via = 'chat_card' if request_status == 'granted' else None
    with engine.begin() as connection:
        for model in (Entity, GovernancePolicy, GovernanceRevision, HitlRequest, AuditLog):
            model.__table__.create(connection)
        workspace_table.create(connection)
        connection.execute(Entity.__table__.insert().values(id='e', name='Account', settings={'approval_defaults': service.account_defaults()}))
        connection.execute(workspace_table.insert().values(id='w', entity_id='e', settings={}, operating_model={}))
        connection.execute(GovernancePolicy.__table__.insert().values(workspace_id='w', entity_id='e', revision=2, policy=policy))
        connection.execute(GovernanceRevision.__table__.insert().values(id='old', workspace_id='w', revision=5, policy=policy))
        connection.execute(HitlRequest.__table__.insert().values(id='h', entity_id='e', workspace_id='w', action_key='send',
            origin_kind='tool_call', status=request_status, hitl_type='authorize', dedup_key='old', context=continuation,
            decided_by_user_id=approver, decided_at=datetime(2026, 10, 1) if approver else None,
            decided_via=decided_via, resolved_reason='approved' if approver else None))
    resolutions = {'workspace:w': {'policy': policy, 'operating_model': {}, 'reconcile_history': True},
        'request:h': {'status': 'expired', 'resolved_reason': 'approval_migration_unverifiable'}}
    with engine.connect() as connection:
        with connection.begin():
            assert len(preflight(connection)['blockers']) == 2
            plan = preflight(connection, resolutions=resolutions)
            assert not plan['blockers']
        with pytest.raises(RuntimeError, match='rollback'):
            with connection.begin():
                apply(connection, plan)
                raise RuntimeError('rollback the entire cutover')
        with connection.begin():
            assert connection.execute(select(HitlRequest.status)).scalar_one() == request_status
            assert connection.execute(select(GovernancePolicy.revision)).scalar_one() == 2
            assert not connection.execute(select(AuditLog.id)).all()
            connection.execute(update(HitlRequest).values(decided_by_user_id='changed'))
            with pytest.raises(ValueError, match='source changed'):
                apply(connection, plan)
            connection.execute(update(HitlRequest).values(decided_by_user_id=approver))
            result = apply(connection, plan)
            assert result['applied'] == 2
            assert apply(connection, plan)['applied'] == 0
            request = connection.execute(select(HitlRequest.__table__)).mappings().one()
            assert request['status'] == 'expired'
            assert request['context'] == continuation
            assert (request['decided_by_user_id'], request['decided_via']) == (approver, decided_via)
            assert request['consumed_at'] is None
            assert connection.execute(select(GovernancePolicy.revision)).scalar_one() == 6
            assert sorted(connection.execute(select(GovernanceRevision.revision)).scalars()) == [5, 6]
            receipt = connection.execute(select(AuditLog.details).where(AuditLog.resource_type == 'hitl_requests')).scalar_one()
            assert receipt['source']['status'] == request_status
            assert receipt['after']['status'] == 'expired'
            for key in ('context', 'decided_by_user_id', 'decided_at', 'decided_via', 'dedup_key'):
                assert receipt['source'][key] == receipt['after'][key]
            assert receipt['reconciliation'] == resolutions['request:h']
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', [None, {'required': True, 'plan_mode': 'new_semantics_conservative_v2', 'reasons': [
    {'code': 'legacy_selector_unrepresentable', 'field': 'auto_approve_capabilities', 'selector': 'manor.composite',
     'handling': 'follows_mode'}]}])
async def test_workspace_settings_surface_the_conversion_review_marker(db_session, marker):
    """settings.approval_review written by the approval conversion is shown to the owner."""
    from tests.approval_settings_test_utils import initialize_test_approval_owners
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import UserMembership
    from packages.core.models.workspace import Workspace

    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name='Review WS', status='active',
                          settings={'theme': 'dark', **({'approval_review': marker} if marker else {})}, operating_model={})
    db_session.add_all([
        User(id=entity_id, entity_id=entity_id, email=f'{entity_id}@review.example', password_hash='test-only',
             role='owner', status='active'),
        UserMembership(user_id=entity_id, entity_id=entity_id, role='owner', status='active'), workspace,
    ])
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=entity_id)
    actor = await db_session.get(User, entity_id)
    scope = ApprovalScope(kind='workspace', id=workspace.id, entity_id=entity_id)
    current = await service.read_approval_settings(db_session, scope, actor)
    assert current['approval_review'] == marker
    # Reading or saving another scope keeps it.
    personal_scope = ApprovalScope(kind='personal', id=entity_id, entity_id=entity_id)
    personal = await service.read_approval_settings(db_session, personal_scope, actor)
    assert personal['approval_review'] is None
    await service.update_approval_policy(db_session, personal_scope, PolicyDraft(mode='automatic'), personal['version'], actor)
    assert (await service.read_approval_settings(db_session, scope, actor))['approval_review'] == marker
    await db_session.refresh(workspace)
    assert workspace.settings.get('approval_review') == marker and workspace.settings['theme'] == 'dark'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [True, False])
async def test_explicit_workspace_save_clears_the_review_marker(db_session, changed):
    """The owner saving this Workspace's approval settings is the review; other settings stay."""
    from tests.approval_settings_test_utils import initialize_test_approval_owners
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import UserMembership
    from packages.core.models.workspace import Workspace

    marker = {'required': True, 'plan_mode': 'new_semantics_conservative_v2', 'reasons': [{'code': 'correction_failed'}]}
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name='Review WS', status='active',
                          settings={'theme': 'dark', 'approval_review': marker}, operating_model={})
    other = Workspace(id=generate_ulid(), entity_id=entity_id, name='Other WS', status='active',
                      settings={'approval_review': marker}, operating_model={})
    db_session.add_all([
        User(id=entity_id, entity_id=entity_id, email=f'{entity_id}@review.example', password_hash='test-only',
             role='owner', status='active'),
        UserMembership(user_id=entity_id, entity_id=entity_id, role='owner', status='active'), workspace, other,
    ])
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=entity_id)
    await initialize_test_approval_owners(db_session, other, user_id=entity_id)
    actor = await db_session.get(User, entity_id)
    scope = ApprovalScope(kind='workspace', id=workspace.id, entity_id=entity_id)
    current = await service.read_approval_settings(db_session, scope, actor)
    saved = await service.update_approval_policy(db_session, scope,
        PolicyDraft(mode='risk_based' if changed else current['policy']['mode']), current['version'], actor)
    assert saved['approval_review'] is None
    assert (await service.read_approval_settings(db_session, scope, actor))['approval_review'] is None
    await db_session.refresh(workspace)
    await db_session.refresh(other)
    assert workspace.settings == {'theme': 'dark'}
    assert other.settings == {'approval_review': marker}
