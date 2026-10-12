"""Success responses acknowledge durable writes; previews stay read-only."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.routers import approval_commands, approval_settings


@pytest.mark.asyncio
@pytest.mark.parametrize('preview', [False, True])
async def test_settings_response_waits_for_commit_except_preview(monkeypatch, preview):
    events = []
    async def write(*args):
        events.append('evaluated' if preview else 'saved')
        return {'version': 'committed-version'}
    async def commit():
        events.append('committed')
    db = SimpleNamespace(commit=AsyncMock(side_effect=commit))
    monkeypatch.setattr(approval_settings, 'update_approval_policy', write)
    monkeypatch.setattr(approval_settings, 'preview_approval_policy', write)
    body = approval_settings.PolicyWriteRequest(expected_version='old-version', draft={'mode': 'risk_based', 'user_rules': []})
    response = await approval_settings._call(db, SimpleNamespace(), kind='account', scope_id='entity', request=body, preview=preview)
    events.append('response')
    assert response['version'] == 'committed-version'
    assert events == (['evaluated', 'response'] if preview else ['saved', 'committed', 'response'])


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['settings', 'command'])
async def test_commit_failure_cannot_acknowledge_a_saved_policy_or_grant(monkeypatch, surface):
    db = SimpleNamespace(commit=AsyncMock(side_effect=RuntimeError('commit failed')))
    if surface == 'settings':
        monkeypatch.setattr(approval_settings, 'update_approval_policy', AsyncMock(return_value={'version':'uncommitted'}))
        body = approval_settings.PolicyWriteRequest(expected_version='old', draft={'mode':'risk_based', 'user_rules':[]})
        with pytest.raises(RuntimeError, match='commit failed'):
            await approval_settings._call(db, SimpleNamespace(), kind='account', scope_id='entity', request=body)
    else:
        monkeypatch.setattr(approval_commands, 'approve_and_save_rule', AsyncMock(return_value={'status':'granted'}))
        with pytest.raises(RuntimeError, match='commit failed'):
            await approval_commands.post_command(SimpleNamespace(), actor=SimpleNamespace(), db=db)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_combined_command_commits_recovery_before_success(monkeypatch):
    events = []
    async def decide(*args, **kwargs):
        events.append('policy-decision-outbox')
        return {'status':'granted'}
    async def commit():
        events.append('committed')
    monkeypatch.setattr(approval_commands, 'approve_and_save_rule', decide)
    result = await approval_commands.post_command(SimpleNamespace(), actor=SimpleNamespace(), db=SimpleNamespace(commit=commit))
    events.append('response')
    assert result['status'] == 'granted'
    assert events == ['policy-decision-outbox', 'committed', 'response']
