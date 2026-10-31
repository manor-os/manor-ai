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
from packages.core.database_runtime import release_read_transaction
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
from packages.core.services.collaborator_steering import collaborator_request_acting
from packages.core.services.external_collaboration_access import (
    COLLABORATOR_MEMBERSHIP_ROLE,
    collaboration_ended,
    live_collaborator_grants,
)
from packages.core.services.collaborator_session import (
    COLLABORATION_ENDED,
    pin_session,
    session_scope,
    require_collaborator_session,
)
from apps.api.collaborator_boundary import enforce_collaborator_route
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



async def _collaborator_live_grants(request: Request, db: AsyncSession, actor) -> list:
    """Live access grants of this actor, read once per request."""
    key = (actor.user_id, actor.entity_id)
    cache = getattr(request.state, "collaborator_live_grants", None)
    if cache is None:
        cache = request.state.collaborator_live_grants = {}
    if key not in cache:
        cache[key] = await live_collaborator_grants(db, user_id=actor.user_id, entity_id=actor.entity_id)
    return cache[key]


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
    return await authenticate_token_claims(request, claims, db)


async def authenticate_token_claims(request: Request, claims: dict, db: AsyncSession) -> User:
    """Admit decoded access-token claims: revocation, account, membership, suspension.

    Shared by the REST bearer dependency and resource endpoints (e.g. ``/mcp``)
    that decode their own audience-bound token first.
    """
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
        if await collaboration_ended(db, user_id=str(user.id), entity_id=str(token_entity_id)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, COLLABORATION_ENDED)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team membership is inactive or unavailable")
    # Present the selected entity as a request-local ORM view without making
    # an auth read persist User.entity_id/User.role.  Security decisions re-read
    # ResolvedUserActor and never trust these display fields as authority.
    user._authenticated_user_credential = credential
    set_committed_value(user, "entity_id", actor.entity_id)
    set_committed_value(user, "role", actor.display_role)
    # None for every actor except an external collaborator, whose live grants
    # are the only Workspaces any route may reach (apps/api/collaborator_boundary).
    user._collaborator_workspace_ids = None
    collaborator = actor.display_role == COLLABORATOR_MEMBERSHIP_ROLE
    # A collaborator authenticates only with a scoped session, and a scoped
    # session only ever resolves to a collaborator (packages/core/services/collaborator_session).
    require_collaborator_session(claims, collaborator=collaborator)
    pin_session(None, None, ())
    if collaborator:
        # The token reaches only the Workspace it pins, while the grant it names is live there.
        allowed = session_scope(claims, await _collaborator_live_grants(request, db, actor))
        if not allowed:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, COLLABORATION_ENDED)
        user._collaborator_workspace_ids = allowed
        # Knowledge, resource and runtime decisions in this request read the same scope.
        pin_session(actor.entity_id, actor.user_id, allowed)
        await enforce_collaborator_route(request, allowed, db, actor.entity_id, actor.user_id)
        # Tasks this request writes are steered by the collaborator, and the
        # integrations it binds carry the collaborator's provenance.
        collaborator_request_acting(db, actor.entity_id, actor.user_id)

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

    # Authentication is a read snapshot, not a transaction for the subsequent
    # upload, external lookup or Agent run. Support-session bookkeeping and
    # other writes deliberately remain on their original commit/rollback path.
    await release_read_transaction(db)
    return user


def require_role(*roles: str):
    """Factory for role-based access control."""
    async def check(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if await effective_user_role_name(db, user) not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role: {', '.join(roles)}")
        await release_read_transaction(db)
        return user
    return check


async def _primary_llm_byok_configured_for_user(
    db: AsyncSession,
    user: User,
    *,
    payer_entity_id: str | None = None,
) -> bool:
    """Return whether primary chat LLM calls should bypass platform AI credits.

    Only the payer's keys count: when another company pays (a client delivery
    trial), the user's own keys do not exempt the run from its gate.
    """
    entity_id = payer_entity_id or user.entity_id
    user_id = user.id if entity_id == user.entity_id else None

    try:
        from packages.core.ai.llm_client import metadata_has_native_byok
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )

        metadata = await resolve_llm_metadata_for_user(
            "primary",
            user_id=user_id,
            entity_id=entity_id,
        )
        if not metadata:
            return False
        model = await resolve_model_for_user(
            "primary",
            user_id=user_id,
            entity_id=entity_id,
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
    workspace_id: str | None = None,
):
    """Apply the standard plan gate from a conditional request branch.

    ``workspace_id`` is the Workspace the AI work runs in, as already
    resolved and authorized by the caller; its payer is gated (the provider
    during a client delivery trial).
    """
    if resource == "ai_budget_usd":
        # Model resolution uses its own short-lived session. Release the auth
        # transaction first so concurrent streams do not each hold one
        # PgBouncer backend while waiting for another.
        await _commit_open_dependency_transaction(db)
        payer_entity_id = user.entity_id
        if workspace_id:
            from packages.core.services.billing_payer import TrialSponsorUnavailable, payer_entity_id as resolve_payer

            try:
                payer_entity_id = await resolve_payer(db, user.entity_id, workspace_id)
            except TrialSponsorUnavailable:
                payer_entity_id = None  # the gate below refuses it as a trial
            await _commit_open_dependency_transaction(db)
        if payer_entity_id and await _primary_llm_byok_configured_for_user(
            db, user, payer_entity_id=payer_entity_id,
        ):
            return None

    from packages.core.services.plan_gate import check
    if resource == "ai_budget_usd" and workspace_id:
        result = await check(db, user.entity_id, resource, workspace_id=workspace_id)
    else:
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
                "kind": result.kind or _PLAN_LIMIT_KIND.get(resource, "generic"),
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


def require_workspace_ai_plan():
    """Dependency: the AI-credit gate for a route's ``workspace_id`` path parameter.

    The payer lookup only matches deliveries targeting the caller's own
    company, so an unverified id cannot borrow another company's sponsor.
    """
    async def _dep(
        workspace_id: str,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        return await enforce_plan_resource("ai_budget_usd", user=user, db=db, workspace_id=workspace_id)
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
        await release_read_transaction(db)
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
    from packages.core.workers.authentication import (
        WorkerAuthenticationBusy, authenticate_worker_secret,
    )

    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Worker bearer secret required")

    worker_id = request.headers.get("manor-worker-id") or request.headers.get("Manor-Worker-Id")
    if not worker_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Manor-Worker-Id header required",
        )

    try:
        worker = await authenticate_worker_secret(db, worker_id, credentials.credentials)
    except WorkerAuthenticationBusy as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "worker_auth_busy", "message": "Worker authentication is busy; retry shortly"},
            headers={"Retry-After": "2"},
        ) from exc
    if worker is None:
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

    await release_read_transaction(db)
    return worker


async def require_workspace_readable(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> None:
    """404 unless ``user`` may read ``workspace_id`` (when one is supplied).

    Workspace-scoped list endpoints on standalone routers (tasks, goals,
    dashboard) filter only by ``entity_id`` + an attacker-supplied
    ``workspace_id``. Without this gate, an entity member who is not part of a
    ``members_only`` workspace could read its tasks/goals/activity by
    passing its id — bypassing the ``_require_workspace_read`` checks on
    ``/workspaces/{id}/*``. A falsy ``workspace_id`` (entity-wide query) is a
    no-op here, except for an external collaborator: entity-level rows and
    Workspaces outside their live grants are never theirs to read.
    """
    workspace_id = (workspace_id or "").strip()
    collaborator_scope = getattr(user, "_collaborator_workspace_ids", None)
    if collaborator_scope is not None and workspace_id not in collaborator_scope:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
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
    ``workspace_id`` (entity-level row with no workspace) is a no-op, except
    for an external collaborator, who may write only inside live grants.
    """
    workspace_id = (workspace_id or "").strip()
    collaborator_scope = getattr(user, "_collaborator_workspace_ids", None)
    if collaborator_scope is not None and workspace_id not in collaborator_scope:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not have write access to this workspace",
        )
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
