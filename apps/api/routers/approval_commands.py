"""Typed approval commands, restricted to origins with durable recovery."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.governance.approval_commands import (
    ApprovalCommand, ApprovalCommandConflict, ApprovalCommandError,
    approve_and_save_rule, project_approval_command, save_rule,
)
from packages.core.governance.approval_policy_service import (
    ApprovalPolicyAccessError, ApprovalPolicyConflict, ApprovalPolicyIntegrityError,
)
from packages.core.models.user import User

router = APIRouter(prefix='/api/v1/approval-commands', tags=['approval-commands'])


def _raise_http(exc):
    if isinstance(exc, PermissionError):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, ApprovalPolicyConflict):
        raise HTTPException(409, {'code': 'approval_policy_conflict', 'current': exc.current}) from exc
    if isinstance(exc, (ApprovalCommandConflict, ApprovalPolicyIntegrityError)):
        raise HTTPException(409, {'code': 'approval_command_conflict', 'message': str(exc)}) from exc
    raise HTTPException(422, str(exc)) from exc


@router.get('/hitl/{request_id}')
async def get_command(request_id: str, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await project_approval_command(db, request_id=request_id, actor=actor)
    except (ApprovalPolicyAccessError, ApprovalPolicyConflict, ApprovalPolicyIntegrityError, ApprovalCommandError, RuntimeError, ValueError, PermissionError) as exc:
        _raise_http(exc)


@router.post('')
async def post_command(command: ApprovalCommand, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        if getattr(command, 'choice', None) == 'save_rule':
            result = await save_rule(db, command=command, actor=actor)
        else:
            result = await approve_and_save_rule(db, command=command, actor=actor)
    except (ApprovalPolicyAccessError, ApprovalPolicyConflict, ApprovalPolicyIntegrityError, ApprovalCommandError, RuntimeError, ValueError, PermissionError) as exc:
        _raise_http(exc)
    # Commit the decision, policy and recovery outbox before acknowledging
    # the command; the caller may immediately refresh either surface.
    await db.commit()
    return result
