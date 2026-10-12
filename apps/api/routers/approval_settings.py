"""Execution and approval settings use one owner-aware policy service."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.governance.approval_policy import ApprovalScope, PolicyDraft
from packages.core.governance.approval_policy_service import (
    ApprovalPolicyAccessError, ApprovalPolicyConflict, ApprovalPolicyIntegrityError,
    list_approval_overrides, preview_approval_policy, read_approval_settings,
    resolve_scope, update_approval_policy,
)
from packages.core.models.user import User

router = APIRouter(prefix='/api/v1/approval-settings', tags=['approval-settings'])


class PolicyWriteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: str = Field(min_length=1, max_length=128)
    draft: PolicyDraft


async def _call(db, actor, *, kind, scope_id=None, request=None, preview=False):
    try:
        if kind == 'account':
            scope = ApprovalScope(kind='account', id=scope_id, entity_id=scope_id)
        else:
            scope = await resolve_scope(db, entity_id=actor.entity_id,
                workspace_id=scope_id if kind == 'workspace' else None,
                room_id=scope_id if kind == 'room' else None,
                user_id=actor.id if kind == 'personal' else None)
        if request is None:
            return await read_approval_settings(db, scope, actor)
        method = preview_approval_policy if preview else update_approval_policy
        result = await method(db, scope, request.draft, request.expected_version, actor)
    except ApprovalPolicyConflict as exc:
        raise HTTPException(409, {'code': 'approval_policy_conflict', 'current': exc.current}) from exc
    except ApprovalPolicyAccessError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalPolicyIntegrityError as exc:
        raise HTTPException(409, {'code': 'approval_policy_integrity', 'message': str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not preview:
        # A successful HTTP response must expose a committed version to
        # the next settings read, not wait for yield-dependency cleanup.
        await db.commit()
    return result


@router.get('/accounts/{entity_id}/overrides')
async def overrides(entity_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                    actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await list_approval_overrides(db, entity_id=entity_id, actor=actor, limit=limit, offset=offset)
    except ApprovalPolicyAccessError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalPolicyIntegrityError as exc:
        raise HTTPException(409, {'code': 'approval_policy_integrity', 'message': str(exc)}) from exc


@router.get('/accounts/{entity_id}')
async def get_account(entity_id: str, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='account', scope_id=entity_id)


@router.put('/accounts/{entity_id}')
async def put_account(entity_id: str, request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='account', scope_id=entity_id, request=request)


@router.post('/accounts/{entity_id}/preview')
async def preview_account(entity_id: str, request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='account', scope_id=entity_id, request=request, preview=True)


@router.get('/personal')
async def get_personal(actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='personal')


@router.put('/personal')
async def put_personal(request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='personal', request=request)


@router.post('/personal/preview')
async def preview_personal(request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='personal', request=request, preview=True)


@router.get('/workspaces/{workspace_id}')
async def get_workspace(workspace_id: str, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='workspace', scope_id=workspace_id)


@router.put('/workspaces/{workspace_id}')
async def put_workspace(workspace_id: str, request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='workspace', scope_id=workspace_id, request=request)


@router.post('/workspaces/{workspace_id}/preview')
async def preview_workspace(workspace_id: str, request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='workspace', scope_id=workspace_id, request=request, preview=True)


@router.get('/rooms/{room_id}')
async def get_room(room_id: str, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='room', scope_id=room_id)


@router.put('/rooms/{room_id}')
async def put_room(room_id: str, request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='room', scope_id=room_id, request=request)


@router.post('/rooms/{room_id}/preview')
async def preview_room(room_id: str, request: PolicyWriteRequest, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _call(db, actor, kind='room', scope_id=room_id, request=request, preview=True)
