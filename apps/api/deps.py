"""
FastAPI dependencies — auth, database, current user.

Usage:
    @router.get("/tasks")
    async def list_tasks(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        ...
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.permissions import Permission, check_effective_permission
from packages.core.services.auth_service import (
    activate_user_membership,
    decode_token,
    get_user_by_id,
    get_user_membership,
)

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)



async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate JWT from Authorization header. Returns authenticated User."""
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    claims = decode_token(credentials.credentials)
    if not claims:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    request.state.auth_claims = claims
    request.state.impersonation = None

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token payload")


    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")

    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    token_entity_id = claims.get("entity_id") or user.entity_id
    membership = await get_user_membership(db, user=user, entity_id=token_entity_id)
    if not membership or membership.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team membership is inactive or unavailable")
    if user.entity_id != membership.entity_id or user.role != membership.role:
        await activate_user_membership(db, user=user, membership=membership)

    # Check entity-level suspension (set by platform admin)
    from packages.core.constants.plans import is_cloud
    if is_cloud() and user.entity_id:
        from packages.core.models.user import Entity
        entity = (await db.execute(
            select(Entity).where(Entity.id == user.entity_id)
        )).scalar_one_or_none()
        if entity and (entity.settings or {}).get("platform_suspended_at"):
            reason = (entity.settings or {}).get("platform_suspended_reason", "")
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your organization has been suspended. {reason}".strip(),
            )

    return user


def require_role(*roles: str):
    """Factory for role-based access control."""
    async def check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role: {', '.join(roles)}")
        return user
    return check


async def _primary_llm_byok_configured_for_user(
    db: AsyncSession,
    user: User,
) -> bool:
    """Return whether primary chat LLM calls should bypass platform AI credits."""

    try:
        from packages.core.ai.llm_client import metadata_has_native_byok
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )

        metadata = await resolve_llm_metadata_for_user(
            "primary",
            user_id=user.id,
            entity_id=user.entity_id,
        )
        if not metadata:
            return False
        model = await resolve_model_for_user(
            "primary",
            user_id=user.id,
            entity_id=user.entity_id,
        )
        routed_metadata = {**metadata, "_resolved_model": model}
        return metadata_has_native_byok(routed_metadata)
    except Exception:
        logger.debug(
            "Failed to resolve primary BYOK metadata for ai_budget gate",
            exc_info=True,
        )
        return False


def require_plan(resource: str):
    """Dependency factory: 402 if plan limit exceeded.

    Usage:
        @router.post("/workspaces")
        async def create_workspace(
            _gate=Depends(require_plan("workspaces")),
            user=Depends(get_current_user),
            db=Depends(get_db),
        ): ...
    """
    async def _dep(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        if resource == "ai_budget_usd" and await _primary_llm_byok_configured_for_user(db, user):
            return None

        from packages.core.services.plan_gate import check
        result = await check(db, user.entity_id, resource)
        if not result.allowed:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": result.message,
                    "limit": result.limit,
                    "current": result.current,
                    "plan": result.plan,
                    # Drives which unified limit reminder the UI shows.
                    "kind": _PLAN_LIMIT_KIND.get(resource, "generic"),
                },
            )
        return result
    return _dep


# Maps a plan-gate resource to the reminder "kind" the frontend renders.
_PLAN_LIMIT_KIND = {
    "ai_budget_usd": "credit",
    "storage_mb": "storage",
    "workspaces": "workspaces",
    "users": "users",
}


# Backward compat alias
require_plan_limit = require_plan


def require_permission(permission: Permission):
    """FastAPI dependency that checks the current user has a specific permission."""
    async def _check(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        await check_effective_permission(
            db,
            user.id,
            user.entity_id,
            user.role,
            permission,
        )
        return user
    return _check


# Convenience shortcuts
require_admin = require_permission(Permission.ADMIN_SETTINGS)
require_owner = require_permission(Permission.USERS_MANAGE)


# ── Worker auth ──────────────────────────────────────────────────────

async def get_current_worker(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Validate a worker via Bearer secret + ``Manor-Worker-Id`` header.

    Mirrors get_current_user but for the M3 heartbeat-driven worker
    surface. Internal workers (``kind='internal'``) reach the dispatcher
    in-process and never traverse this dependency.

    Checks:
      * Bearer secret matches worker.secret_hash (bcrypt)
      * Worker secret is usable (revoked / expired workers are rejected;
        paused and quarantined workers may still heartbeat and receive a
        pause instruction)
      * Optional IP allowlist (``worker.allowed_ips``) honoured
    """
    from packages.core.workers import get_worker, verify_worker_secret

    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Worker bearer secret required")

    worker_id = request.headers.get("manor-worker-id") or request.headers.get("Manor-Worker-Id")
    if not worker_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Manor-Worker-Id header required",
        )

    worker = await get_worker(db, worker_id)
    if worker is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Worker not found")

    if not verify_worker_secret(worker, credentials.credentials):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid worker secret")

    # IP allowlist check — only enforced when the operator opts in.
    allowed = worker.allowed_ips or []
    if allowed:
        client_ip = request.client.host if request.client else None
        if client_ip not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Source IP {client_ip!r} not in worker allowlist",
            )

    return worker
