"""Old approval writers retire explicitly; unified settings retain real ACLs."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.auth_helpers import register_user_and_get_token
from packages.core.governance.approval_policy_service import initialize_personal_approval, initialize_workspace_approval
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernanceRevision
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff

pytestmark = pytest.mark.asyncio


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str]:
    response = await register_user_and_get_token(client, json={'username': username, 'email': f'{username}@test.com', 'password': 'ApprovalMatrixTest123!', 'entity_name': f'{username} Corp'})
    headers = {'Authorization': f"Bearer {response.json()['access_token']}"}
    me = await client.get('/api/v1/auth/me', headers=headers)
    assert me.status_code == 200
    return headers, me.json()['entity_id']


async def _seed_workspace(entity_id: str) -> str:
    import packages.core.database as dbmod
    async with dbmod.async_session() as db:
        workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name='Approval workspace', status='active', settings={})
        db.add(workspace)
        await db.flush()
        await initialize_workspace_approval(db, workspace)
        await db.commit()
    return workspace.id


async def _member_headers(entity_id: str, username: str, workspace_id: str | None = None) -> dict:
    import packages.core.database as dbmod
    from packages.core.services.auth_service import create_access_token, hash_password
    async with dbmod.async_session() as db:
        member = User(id=generate_ulid(), entity_id=entity_id, email=f'{username}@test.com', display_name=username, password_hash=hash_password('pass123'), role='member')
        db.add_all([member, UserMembership(id=generate_ulid(), user_id=member.id, entity_id=entity_id, role='member', status='active', is_primary=True)])
        await db.flush()
        await initialize_personal_approval(db, user_id=member.id, entity_id=entity_id)
        if workspace_id:
            db.add(WorkspaceStaff(id=generate_ulid(), workspace_id=workspace_id, user_id=member.id, role='viewer', status='active'))
        await db.commit()
    return {'Authorization': f"Bearer {create_access_token(member.id, entity_id, 'member')}"}


async def _revision_count(workspace_id: str) -> int:
    import packages.core.database as dbmod
    async with dbmod.async_session() as db:
        return len((await db.execute(select(GovernanceRevision).where(GovernanceRevision.workspace_id == workspace_id))).scalars().all())


async def test_retired_matrix_and_standing_endpoints_return_410_without_mutating(client: AsyncClient):
    owner, entity_id = await _register_owner(client, 'retired_owner')
    workspace_id = await _seed_workspace(entity_id)
    before = await _revision_count(workspace_id)
    base = f'/api/v1/workspaces/{workspace_id}/governance'
    for method, suffix in [('GET', '/approval-matrix'), ('PUT', '/approval-matrix'), ('GET', '/standing-grants'), ('DELETE', '/standing-grants/action/external_message.send')]:
        response = await client.request(method, base + suffix, headers=owner, json={'action_key': 'external_message.send', 'auto_approved': True} if method == 'PUT' else None)
        assert response.status_code == 410, response.text
        assert response.json()['detail']['code'] == 'approval_configuration_retired'
    assert await _revision_count(workspace_id) == before


async def test_generic_governance_only_accepts_hard_constraints(client: AsyncClient):
    owner, entity_id = await _register_owner(client, 'generic_owner')
    workspace_id = await _seed_workspace(entity_id)
    path = f'/api/v1/workspaces/{workspace_id}/governance'
    current = await client.get(path, headers=owner)
    assert current.status_code == 200, current.text
    approval = current.json()['policy']['approval']
    before = await _revision_count(workspace_id)
    rejected = {'approval': {'mode': 'automatic', 'rules': []}, 'automated_runs_always_allow': True,
        'never_allow_actions': [], 'hitl_required_actions': [], 'auto_approve_actions': [],
        'never_allow_capabilities': [], 'hitl_required_capabilities': [], 'auto_approve_capabilities': []}
    for key, value in rejected.items():
        response = await client.put(path, headers=owner, json={'policy': {key: value}})
        assert response.status_code == 422, (key, response.text)
    assert await _revision_count(workspace_id) == before
    response = await client.put(path, headers=owner, json={'policy': {'max_risk_level': 'medium', 'budget_caps_per_kind': {'action': 500}}})
    assert response.status_code == 200, response.text
    assert response.json()['policy']['approval'] == approval
    assert response.json()['policy']['max_risk_level'] == 'medium'
    assert await _revision_count(workspace_id) == before + 1


async def test_member_can_view_but_cannot_change_shared_defaults_or_workspace_policy(client: AsyncClient):
    owner, entity_id = await _register_owner(client, 'permissions_owner')
    workspace_id = await _seed_workspace(entity_id)
    member = await _member_headers(entity_id, 'permissions_member', workspace_id)
    for path in [f'/api/v1/approval-settings/accounts/{entity_id}', f'/api/v1/approval-settings/workspaces/{workspace_id}']:
        read = await client.get(path, headers=member)
        assert read.status_code == 200, read.text
        assert read.json()['capabilities']['can_save_rule'] is False
        denied = await client.put(path, headers=member, json={'expected_version': read.json()['version'], 'draft': {'mode': 'automatic', 'user_rules': []}})
        assert denied.status_code == 403, denied.text
    direct = await client.put(f'/api/v1/workspaces/{workspace_id}/governance', headers=member, json={'policy': {'max_risk_level': 'low'}})
    assert direct.status_code == 403, direct.text
    personal = await client.get('/api/v1/approval-settings/personal', headers=member)
    assert personal.status_code == 200, personal.text
    assert personal.json()['capabilities']['can_edit_local'] is True
    assert personal.json()['capabilities']['can_edit_root'] is False


async def test_cross_entity_access_cannot_disclose_or_modify_scope(client: AsyncClient):
    owner, entity_id = await _register_owner(client, 'isolation_owner')
    workspace_id = await _seed_workspace(entity_id)
    stranger, _ = await _register_owner(client, 'isolation_stranger')
    for suffix in ['/approval-matrix', '/standing-grants']:
        response = await client.get(f'/api/v1/workspaces/{workspace_id}/governance{suffix}', headers=stranger)
        assert response.status_code == 404, response.text
    for path in [f'/api/v1/approval-settings/accounts/{entity_id}', f'/api/v1/approval-settings/workspaces/{workspace_id}']:
        response = await client.get(path, headers=stranger)
        assert response.status_code == 403, response.text
        response = await client.put(path, headers=stranger, json={'expected_version': 'made-up', 'draft': {'mode': 'automatic', 'user_rules': []}})
        assert response.status_code == 403, response.text
