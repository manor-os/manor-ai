"""Atomic settings/decision/outbox behavior for the combined approval command."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from packages.core.governance import approval_commands as commands
from packages.core.governance import approval_policy_service as policies
from packages.core.governance import approvals
from packages.core.models.audit import AuditLog
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.user import Entity, User
from packages.core.ai.runtime import approval_store
from packages.core.services.task_agent_loop_hitl import TASK_AGENT_LOOP_HITL_CONTEXT_KEY

# The existing policy tests register the PostgreSQL JSONB -> SQLite JSON test
# compiler; neither suite starts a database or touches deployment state.
from tests.test_approval_policy_settings import compile_jsonb_sqlite  # noqa: F401


@pytest_asyncio.fixture
async def command_db(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    conversation = Table('conversations', MetaData(), Column('id', String, primary_key=True), Column('user_id', String), Column('entity_id', String))
    async with engine.begin() as connection:
        for model in (Entity, User, AuditLog, HitlRequest, RuntimeOutboxEvent):
            await connection.run_sync(lambda sync, model=model: model.__table__.create(sync))
        await connection.run_sync(conversation.create)
    caps = {'can_view': True, 'can_edit_root': True, 'can_edit_local': True, 'can_save_rule': True, 'can_approve_once': False, 'can_grant_run': False}
    monkeypatch.setattr(policies, '_capabilities', AsyncMock(return_value=caps))
    monkeypatch.setattr(commands, 'authorize_hitl_resolution', AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(commands, 'lock_task_runtime_approval_boundary', AsyncMock())
    # The signed park tag and the live park are the agent loop's own contract
    # (tests/test_task_agent_loop_hitl.py); here a tag's presence stands in.
    monkeypatch.setattr(commands, 'verified_request_tag', lambda request: (request.context or {}).get(TASK_AGENT_LOOP_HITL_CONTEXT_KEY))
    monkeypatch.setattr(commands, 'task_agent_loop_approval_is_current', AsyncMock(return_value=True))
    monkeypatch.setattr(approval_store, 'mark_runtime_hitl_request_resolved', AsyncMock())
    async def grant(db, request, **kwargs):
        assert kwargs['standing'] is False
        request.status = 'granted'
        request.decided_by_user_id = 'u'
        return request
    monkeypatch.setattr(approvals, 'grant_approval', grant)
    async def stage(db, *, request, **kwargs):
        db.add(RuntimeOutboxEvent(event_type='runtime.resume_task', aggregate_id='t', dedupe_key='approval:h', payload={'request_id': request.id}))
        return {'approval_kind': 'task_runtime', 'task_runtime_resume': {'task_id': 't', 'hitl_id': request.id}}
    monkeypatch.setattr(commands, 'stage_task_agent_loop_approval_resolution', stage)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(Entity(id='e', name='Account', settings={'approval_defaults': policies.account_defaults()}))
        db.add(User(id='u', entity_id='e', email='u@example.test', password_hash='none', preferences={'approval_policy': {'e': policies.local_defaults('e')}}))
        await db.execute(conversation.insert().values(id='c', user_id='u', entity_id='e'))
        db.add(HitlRequest(id='h', entity_id='e', action_key='workspace.file.modify', resource_kind='file', resource_id='report.md',
            risk_level='medium', origin_kind='tool_call', origin_task_id='t', origin_conversation_id='c', status='pending',
            hitl_type='authorize', dedup_key='operation:runtime:r:call:x', payload={}, context={
                'operation_id': 'runtime:r:call:x', 'approval_requirements': [], TASK_AGENT_LOOP_HITL_CONTEXT_KEY: {'fixture': True},
                'policy_evaluation': {'target_decisions': [{'target': {'account_id': None, 'resource_kind': 'file', 'resource_id': 'report.md'}}]},
                'tool': 'edit_file', 'args_hash': 'digest', 'continuation': {'version': 1, 'tool': 'edit_file', 'arguments': {'path': 'report.md', 'content': 'new'}}}))
        await db.commit()
        yield db, SimpleNamespace(id='u', entity_id='e')
    await engine.dispose()


async def command_for(db, actor):
    projection = await commands.project_approval_command(db, request_id='h', actor=actor)
    assert 'approve_and_save_rule' in projection['available_decisions']
    assert projection['scope'] == {'kind': 'personal', 'id': 'u', 'entity_id': 'e'}
    return commands.ApproveAndSaveRuleCommand(request={'kind': 'hitl', 'id': 'h'}, choice='approve_and_save_rule',
        expectedDigest=projection['digest'], idempotencyKey='unique-click-1', expectedPolicyVersion=projection['policy_version'], optionId=projection['rule_options'][0]['id'])


@pytest.mark.asyncio
async def test_combo_is_one_rule_one_grant_one_outbox_and_retry_is_idempotent(command_db):
    db, actor = command_db
    command = await command_for(db, actor)
    result = await commands.approve_and_save_rule(db, command=command, actor=actor)
    await db.commit()
    assert result['status'] == 'granted'
    assert result['policy']['policy']['rules'][0]['selector']['resource_ids'] == ['report.md']
    repeated = await commands.approve_and_save_rule(db, command=command, actor=actor)
    assert repeated['idempotent'] is True
    events = (await db.execute(select(RuntimeOutboxEvent))).scalars().all()
    # One resume, plus the one re-decision of other pending asks the rule write stages.
    assert sorted(event.event_type for event in events) == ['approval.reevaluate_pending', 'runtime.resume_task']
    assert len((await db.execute(select(AuditLog))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_policy_conflict_leaves_approval_pending_and_no_outbox(command_db):
    db, actor = command_db
    command = (await command_for(db, actor)).model_copy(update={'expected_policy_version': 'stale'})
    with pytest.raises(policies.ApprovalPolicyConflict):
        await commands.approve_and_save_rule(db, command=command, actor=actor)
    await db.commit()  # Even a caller catching the error cannot commit half a command.
    assert (await db.execute(select(HitlRequest.status))).scalar_one() == 'pending'
    assert not (await db.execute(select(RuntimeOutboxEvent))).scalars().all()
    prefs = (await db.execute(select(User.preferences))).scalar_one()
    assert prefs['approval_policy']['e']['revision'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [PermissionError('Permission revoked'), RuntimeError('Invalid checkpoint')])
async def test_failure_after_rule_save_rolls_back_rule_grant_and_outbox(command_db, monkeypatch, failure):
    db, actor = command_db
    command = await command_for(db, actor)
    async def fail(db, **kwargs):
        db.add(RuntimeOutboxEvent(event_type='runtime.resume_task', aggregate_id='t', dedupe_key='approval:h', payload={}))
        await db.flush()
        raise failure
    monkeypatch.setattr(commands, 'stage_task_agent_loop_approval_resolution', fail)
    with pytest.raises(type(failure)):
        await commands.approve_and_save_rule(db, command=command, actor=actor)
    await db.commit()
    assert (await db.execute(select(HitlRequest.status))).scalar_one() == 'pending'
    assert not (await db.execute(select(RuntimeOutboxEvent))).scalars().all()
    assert not (await db.execute(select(AuditLog))).scalars().all()
    prefs = (await db.execute(select(User.preferences))).scalar_one()
    assert prefs['approval_policy']['e']['policy']['rules'] == []


@pytest.mark.asyncio
async def test_digest_and_option_cannot_be_substituted(command_db):
    db, actor = command_db
    command = await command_for(db, actor)
    for field in ('expected_digest', 'option_id'):
        with pytest.raises(commands.ApprovalCommandConflict):
            await commands.approve_and_save_rule(db, command=command.model_copy(update={field: 'different'}), actor=actor)
    assert (await db.execute(select(HitlRequest.status))).scalar_one() == 'pending'


@pytest.mark.asyncio
async def test_non_task_origin_saves_the_rule_without_the_atomic_combo(command_db):
    """A chat/Workflow tool approval offers "Always allow" as a rule save only;
    its own card transport approves. A provider confirmation offers neither."""
    db, actor = command_db
    request = (await db.execute(select(HitlRequest))).scalar_one()
    request.origin_task_id = None
    await db.flush()
    projection = await commands.project_approval_command(db, request_id='h', actor=actor)
    assert projection['available_decisions'] == ['approve_once', 'reject', 'save_rule']
    assert projection['rule_options'] and projection['capabilities']['can_save_rule'] is True
    request.context = {**request.context, 'kind': 'provider'}
    await db.flush()
    projection = await commands.project_approval_command(db, request_id='h', actor=actor)
    assert projection['available_decisions'] == ['approve_once', 'reject']
    assert not projection['rule_options']
    assert projection['capabilities']['can_save_rule'] is False


@pytest.mark.asyncio
async def test_exact_rule_covers_all_proven_targets_or_is_not_offered(command_db):
    db, actor = command_db
    request = (await db.execute(select(HitlRequest))).scalar_one()
    request.context = {**request.context, '_approval_resource_ids': ['report.md', 'budget.md'], 'policy_evaluation': {'target_decisions': [
        {'target': {'account_id': None, 'resource_kind': 'file', 'resource_id': item}} for item in ['report.md', 'budget.md']]}}
    options = commands._rule_options(request)
    assert options[0]['rule']['selector']['resource_ids'] == ['budget.md', 'report.md']
    request.context = {**request.context, '_approval_resource_ids': ['report.md', 'budget.md', 'unseen.md']}
    assert all(item['coverage'] == 'all_targets_for_action' for item in commands._rule_options(request))


@pytest.mark.asyncio
async def test_lost_resumable_origin_does_not_save_rule(command_db, monkeypatch):
    db, actor = command_db
    command = await command_for(db, actor)
    monkeypatch.setattr(commands, 'task_agent_loop_approval_is_current', AsyncMock(return_value=False))
    with pytest.raises(commands.ApprovalCommandError, match='durable'):
        await commands.approve_and_save_rule(db, command=command, actor=actor)
    assert (await db.execute(select(HitlRequest.status))).scalar_one() == 'pending'
