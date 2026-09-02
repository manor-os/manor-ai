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
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.permissions import (
    Permission,
    check_effective_permission,
    effective_user_role_name,
)
from packages.core.services.auth_service import (
    decode_token,
    get_user_by_id,
)
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    resolve_current_user_actor,
)
from packages.core.workers.protocol import (
    WORKER_PROTOCOL_HEADER,
    require_current_worker_protocol,
    worker_protocol_header_value,
)

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def _claim_expiry_epoch(claims: dict) -> float | None:
    value = claims.get("exp")
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _validate_impersonation_admission(
    db: AsyncSession,
    credential: AuthenticatedUserCredential,
) -> bool:
    """Recheck the Cloud support-session revocation point for every Gate."""
    if not credential.impersonation_session_id:
        return True
    return False


def authenticated_user_credential_from_claims(
    *,
    user_id: str,
    entity_id: str,
    claims: dict,
) -> AuthenticatedUserCredential:
    """Build the immutable, server-verified credential carried through Gates."""
    session_id = (
        str(claims.get("impersonation_session_id") or "") or None
        if claims.get("typ") == "impersonation"
        else None
    )
    return AuthenticatedUserCredential(
        user_id=str(user_id),
        entity_id=str(entity_id),
        token_version=int(claims.get("token_version", -1)),
        expires_at_epoch=_claim_expiry_epoch(claims),
        impersonation_session_id=session_id,
        admission_validator=_validate_impersonation_admission if session_id else None,
    )



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
    from packages.core.services.auth_context import set_current_mfa_verified
    set_current_mfa_verified("mfa" in {
        str(method).strip().lower() for method in claims.get("amr", [])
    })
    request.state.impersonation = None

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token payload")


    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")

    if int(claims.get("token_version", -1)) != int(user.token_version or 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has been revoked")

    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    token_entity_id = claims.get("entity_id") or user.entity_id
    credential = authenticated_user_credential_from_claims(
        user_id=str(user.id),
        entity_id=str(token_entity_id),
        claims=claims,
    )
    actor = await resolve_current_user_actor(
        db,
        credential,
        enforce_entity_suspension=False,
    )
    if actor is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team membership is inactive or unavailable")
    # Present the selected entity as a request-local ORM view without making
    # an auth read persist User.entity_id/User.role.  Security decisions re-read
    # ResolvedUserActor and never trust these display fields as authority.
    user._authenticated_user_credential = credential
    set_committed_value(user, "entity_id", actor.entity_id)
    set_committed_value(user, "role", actor.display_role)

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
    async def check(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if await effective_user_role_name(db, user) not in roles:
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


async def _commit_open_dependency_transaction(db: AsyncSession) -> None:
    in_transaction = getattr(db, "in_transaction", None)
    if callable(in_transaction) and in_transaction():
        await db.commit()


async def enforce_plan_resource(
    resource: str,
    *,
    user: User,
    db: AsyncSession,
):
    """Apply the standard plan gate from a conditional request branch."""
    if resource == "ai_budget_usd":
        # Model resolution uses its own short-lived session. Release the auth
        # transaction first so concurrent streams do not each hold one
        # PgBouncer backend while waiting for another.
        await _commit_open_dependency_transaction(db)
        if await _primary_llm_byok_configured_for_user(db, user):
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
                "resets_at": result.resets_at,
                # Drives which unified limit reminder the UI shows.
                "kind": _PLAN_LIMIT_KIND.get(resource, "generic"),
            },
        )
    await _commit_open_dependency_transaction(db)
    return result


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
        return await enforce_plan_resource(resource, user=user, db=db)
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

    protocol_version = request.headers.get(WORKER_PROTOCOL_HEADER)
    if protocol_version != worker_protocol_header_value():
        raise HTTPException(
            status.HTTP_426_UPGRADE_REQUIRED,
            (
                f"{WORKER_PROTOCOL_HEADER} must be "
                f"{worker_protocol_header_value()}"
            ),
        )
    try:
        require_current_worker_protocol(worker.capabilities or {})
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_426_UPGRADE_REQUIRED,
            f"Worker registration is incompatible: {exc}; re-register the worker",
        ) from exc

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


async def require_workspace_readable(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> None:
    """404 unless ``user`` may read ``workspace_id`` (when one is supplied).

    Workspace-scoped list endpoints on standalone routers (tasks, goals, plans,
    dashboard) filter only by ``entity_id`` + an attacker-supplied
    ``workspace_id``. Without this gate, an entity member who is not part of a
    ``members_only`` workspace could read its tasks/goals/plans/activity by
    passing its id — bypassing the ``_require_workspace_read`` checks on
    ``/workspaces/{id}/*``. A falsy ``workspace_id`` (entity-wide query) is a
    no-op here.
    """
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return
    from packages.core.services.permission_gate import ResourcePermissionGate

    if await ResourcePermissionGate.authorize_workspace_read(
        db,
        credential=AuthenticatedUserCredential.from_user(user),
        workspace_id=workspace_id,
    ) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")


async def require_workspace_writable(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> None:
    """403 unless ``user`` may create/modify content in ``workspace_id``.

    Writing is stricter than reading: an ``entity_visible`` workspace is
    readable org-wide but only members may write to it, and ``viewer`` members
    are read-only. Entity owner/admin keep the firm-wide override. A falsy
    ``workspace_id`` (entity-level row with no workspace) is a no-op.
    """
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return
    from packages.core.services.workspace_access import user_can_write_workspace_id

    if not await user_can_write_workspace_id(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not have write access to this workspace",
        )


async def require_workspace_authority(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
    permission_key: str,
) -> None:
    """403 unless ``user`` holds one participant authority in a Workspace."""
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return
    from packages.core.humans import participant_can

    if not await participant_can(
        db,
        user=user,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        permission_key=permission_key,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"This action requires '{permission_key}' authority in the workspace",
        )
