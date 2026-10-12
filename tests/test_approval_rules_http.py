"""Exercise the public rules writer through real operation apply and storage."""
import pytest

from tests.auth_helpers import register_user_and_get_token


@pytest.mark.asyncio
async def test_rules_http_apply_and_reset_preserve_each_rule_owner(client):
    registered = await register_user_and_get_token(client, json={
        'email': 'approval-rules-http@example.test', 'password': 'ApprovalRulesTest123!',
        'entity_name': 'Approval rules regression',
    })
    assert registered.status_code == 200, registered.text
    headers = {'Authorization': 'Bearer ' + registered.json()['access_token']}
    created = await client.post('/api/v1/workspaces', headers=headers, json={
        'name': 'Approval rules regression', 'heartbeat_enabled': False,
    })
    assert created.status_code == 201, created.text
    workspace_id = created.json()['id']
    settings = f'/api/v1/approval-settings/workspaces/{workspace_id}'
    rules_path = f'/api/v1/workspaces/{workspace_id}/rules'
    source = {'rule_key': 'review-sends', 'rule_type': 'approval_required',
              'description': 'Review outgoing messages',
              'approval_selectors': [{'kind': 'action', 'action_key': 'external_message.send'}]}

    current = (await client.get(settings, headers=headers)).json()
    custom = await client.put(settings, headers=headers, json={
        'expected_version': current['version'],
        'draft': {'mode': 'automatic', 'user_rules': [
            {'effect': 'deny', 'selector': {'kind': 'action', 'action_key': 'workspace.file.delete'}},
        ]},
    })
    assert custom.status_code == 200, custom.text

    saved = await client.put(rules_path, headers=headers, json={'rules': [source]})
    assert saved.status_code == 200, saved.text
    current = (await client.get(settings, headers=headers)).json()
    assert current['policy']['mode'] == 'automatic'
    assert {rule['source_kind'] for rule in current['saved_rules']} == {'user', 'workspace_rule'}
    derived = next(rule for rule in current['saved_rules'] if rule['source_kind'] == 'workspace_rule')
    assert derived['source_id'] == source['rule_key']
    assert derived['editable'] is False

    reset = await client.put(settings, headers=headers, json={
        'expected_version': current['version'], 'draft': {'mode': None, 'user_rules': []},
    })
    assert reset.status_code == 200, reset.text
    assert reset.json()['policy']['mode'] is None
    assert [rule['id'] for rule in reset.json()['saved_rules']] == [derived['id']]

    removed = await client.put(rules_path, headers=headers, json={'rules': []})
    assert removed.status_code == 200, removed.text
    final = (await client.get(settings, headers=headers)).json()
    assert final['inheritance'] == 'following'
    assert final['saved_rules'] == []
