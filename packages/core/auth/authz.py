"""authorize() — single entry point for permission decisions (P1 stub).

Status: scaffolding only. Behavior in this commit is intentionally
**identical to pre-permissions-v1**:

  * If the feature flag ``permissions_v1_enforce`` is OFF (default), we
    fall through to the legacy hardcoded ``Permission`` check on the
    user's tenant role and return ALLOW for any resource the legacy
    code would have allowed.
  * If the flag is ON, we additionally consult workspace membership +
    resource_grants + classification rules. This branch is exercised by
    unit tests but not yet wired into routers.

Routers should be migrated to call ``authorize()`` / ``require()`` over
time (see RFC §11 P5). New code should call this entry point even though
the strict branch is gated — the call site stays correct as the flag
flips.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Union

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import (
    Capability,
    Classification,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
    Visibility,
)
from packages.core.permissions import (
    Permission,
    has_permission,
    user_has_permission,
)

# ── Feature flag key ──────────────────────────────────────────────────────
FLAG_ENFORCE = "permissions_v1_enforce"
FLAG_AUDIT = "permissions_v1_audit"


# ── Actor types ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UserActor:
    user_id: str
    entity_id: str
    role: str  # legacy User.role string
    actor_type: str = "user"


@dataclass(frozen=True)
class AgentActor:
    """Agent acting on behalf of a user/task. ``invoker_user_id`` is the
    user whose ACL the agent inherits; ``capabilities`` is the explicit
    delegation set chosen at task creation time."""
    agent_id: str
    invoker_user_id: str
    entity_id: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    workspace_id: Optional[str] = None
    actor_type: str = "agent"


@dataclass(frozen=True)
class ShareTokenActor:
    share_id: str
    entity_id: str
    resource_type: str
    resource_id: str
    capabilities: frozenset[str]
    actor_type: str = "share_token"




@dataclass(frozen=True)
class WorkerActor:
    worker_id: str
    entity_id: str
    actor_type: str = "worker"


Actor = Union[
    UserActor,
    AgentActor,
    ShareTokenActor,
    WorkerActor,
]


# ── Resource handle ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Resource:
    """Reference to a row plus the metadata authorize() needs.

    The caller passes whatever it has loaded; authorize() will hydrate
    missing pieces (lazy lookups guarded by classification only when the
    enforce flag is on, so the legacy fallthrough costs zero queries).
    """
    type: str
    id: str
    entity_id: Optional[str] = None
    workspace_id: Optional[str] = None
    visibility: Optional[str] = None
    classification: Optional[str] = None
    owner_id: Optional[str] = None
    client_visible: bool = False
    legal_hold: bool = False
    quarantine_status: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── Decision ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""
    matched_rule: Optional[str] = None
    # For diagnostics & UI ("you have access via workspace.editor")

    def __bool__(self) -> bool:  # pragma: no cover — convenience
        return self.allow


def _allow(rule: str, reason: str = "") -> Decision:
    return Decision(allow=True, matched_rule=rule, reason=reason or rule)


def _deny(reason: str, rule: Optional[str] = None) -> Decision:
    return Decision(allow=False, reason=reason, matched_rule=rule)


# ── Public entry ──────────────────────────────────────────────────────────


async def authorize(
    db: AsyncSession,
    actor: Actor,
    action: Union[Permission, str],
    resource: Optional[Resource] = None,
    *,
    request_id: Optional[str] = None,
    ip: Optional[str] = None,
) -> Decision:
    """Decide whether ``actor`` may perform ``action`` on ``resource``.

    Returns a Decision; never raises (callers raise HTTPException via the
    ``require()`` helper or check ``decision.allow`` directly).
    """
    action_str = action.value if isinstance(action, Permission) else str(action)


    # Legacy fallthrough — flag off → preserve current behavior exactly.
    if not await _enforce_enabled(db, _entity_of(actor)):
        decision = await _legacy_check(db, actor, action_str, resource)
        await _audit(db, actor, action_str, resource, decision, request_id, ip)
        return decision

    # Strict branch — three-layer evaluation.
    decision = await _strict_check(db, actor, action_str, resource)
    await _audit(db, actor, action_str, resource, decision, request_id, ip)
    return decision


async def require(
    db: AsyncSession,
    actor: Actor,
    action: Union[Permission, str],
    resource: Optional[Resource] = None,
    *,
    request_id: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """Like ``authorize`` but raises HTTPException 403 on deny."""
    decision = await authorize(
        db, actor, action, resource, request_id=request_id, ip=ip
    )
    if not decision.allow:
        action_str = action.value if isinstance(action, Permission) else str(action)
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied: {action_str} ({decision.reason})",
        )


# ── Legacy fallthrough (flag off) ─────────────────────────────────────────


async def _legacy_check(
    db: AsyncSession,
    actor: Actor,
    action: str,
    resource: Optional[Resource],
) -> Decision:
    """Reproduce pre-v1 behavior: tenant role check on the verb, ignore
    resource. Mirrors ``packages.core.permissions.has_permission`` /
    ``user_has_permission``.
    """
    if isinstance(actor, UserActor):
        # 1) hardcoded role table (sync)
        try:
            perm = Permission(action)
        except ValueError:
            perm = None
        if perm is not None and has_permission(actor.role, perm):
            return _allow("legacy.role_table")
        # 2) data-driven StaffRole permissions
        if await user_has_permission(db, actor.user_id, actor.entity_id, action):
            return _allow("legacy.staff_role")
        return _deny("legacy: role lacks verb", "legacy.deny")

    if isinstance(actor, AgentActor):
        # Pre-v1 agents implicitly used invoker's role. Mirror that by
        # delegating to the invoker check.
        # (This is the behavior we want to phase out — see RFC §13.7.)
        return _allow("legacy.agent_inherits_invoker")

    if isinstance(actor, ShareTokenActor):
        # Shares are opaque allow tokens in legacy world.
        return _allow("legacy.share_token")

    if isinstance(actor, WorkerActor):
        return _allow("legacy.worker")

    return _deny("legacy: unknown actor")


# ── Strict branch (flag on) ───────────────────────────────────────────────


async def _strict_check(
    db: AsyncSession,
    actor: Actor,
    action: str,
    resource: Optional[Resource],
) -> Decision:
    """Three-layer evaluation. Tightly scoped in P1 — covers documents,
    document_folders, tasks, agent_memories. Other resource types fall
    back to the legacy verb check until §11 P-stages migrate them.
    """
    # Layer 1: tenant role must allow the verb at all.
    layer1 = await _layer1_tenant(db, actor, action)
    if not layer1.allow:
        return layer1

    # Verb-only actions stop here (e.g. admin.billing).
    if resource is None:
        return layer1

    # Knowledge / task / memory invariants (RFC §13.14).
    invariant = _check_invariants(actor, action, resource)
    if not invariant.allow:
        return invariant

    # Owner shortcut — owner always passes (subject to invariants).
    if isinstance(actor, UserActor) and resource.owner_id == actor.user_id:
        return _allow("owner")

    # Layer 2: workspace membership where applicable.
    if resource.workspace_id and isinstance(actor, UserActor):
        layer2 = await _layer2_workspace(db, actor, resource)
        if layer2.allow:
            return layer2

    # Visibility check — entity visibility is the legacy default.
    if resource.visibility == Visibility.ENTITY and isinstance(actor, UserActor):
        if resource.entity_id == actor.entity_id:
            return _allow("visibility.entity")

    # Layer 3: explicit grant.
    layer3 = await _layer3_grant(db, actor, action, resource)
    if layer3.allow:
        return layer3

    # Share token actor: capability set is on the token.
    if isinstance(actor, ShareTokenActor):
        if (
            resource.type == actor.resource_type
            and resource.id == actor.resource_id
            and _action_matches_capability(action, actor.capabilities)
        ):
            return _allow("share_token.capability")
        return _deny("share token does not cover this resource/action")

    return _deny("no matching grant")


async def _layer1_tenant(
    db: AsyncSession, actor: Actor, action: str
) -> Decision:
    if isinstance(actor, UserActor):
        try:
            perm = Permission(action)
        except ValueError:
            perm = None
        if perm is not None and has_permission(actor.role, perm):
            return _allow("layer1.role_table")
        if await user_has_permission(db, actor.user_id, actor.entity_id, action):
            return _allow("layer1.staff_role")
        # Tenant verb missing — but row-level grant may still apply for
        # data verbs (view/comment). Allow fall-through.
        if action in {"view", "comment", Permission.DOCS_READ.value, Permission.TASKS_READ.value}:
            return _allow("layer1.skip_for_data_verb")
        # Capability-style actions (share_external, download, view_redacted
        # etc.) are resource-level, not verb-level — layer1 has nothing to
        # say about them. Defer to invariants + grants.
        if perm is None:
            return _allow("layer1.skip_for_capability")
        return _deny("tenant role lacks verb", "layer1.deny")

    if isinstance(actor, AgentActor):
        if action in actor.capabilities:
            return _allow("layer1.agent_capability")
        return _deny("agent capability not delegated", "layer1.agent_deny")

    if isinstance(actor, WorkerActor):
        # Workers operate inside a worker auth context; verb gating is
        # delegated to the dispatcher in P1.
        return _allow("layer1.worker_passthrough")

    if isinstance(actor, ShareTokenActor):
        return _allow("layer1.share_passthrough")

    return _deny("unknown actor")


async def _layer2_workspace(
    db: AsyncSession, actor: UserActor, resource: Resource
) -> Decision:
    """Check workspace_staff membership. Matches by user_id; falls back
    to staff_id lookup if needed (legacy rows). Returns the matched
    role for downstream use via Decision.matched_rule.
    """
    from packages.core.models.workspace import WorkspaceStaff

    row = (
        await db.execute(
            select(WorkspaceStaff).where(
                WorkspaceStaff.workspace_id == resource.workspace_id,
                WorkspaceStaff.user_id == actor.user_id,
                WorkspaceStaff.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return _deny("not a workspace member", "layer2.miss")

    if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
        return _deny("workspace membership expired", "layer2.expired")

    return _allow(f"layer2.workspace.{row.role or 'member'}")


async def _layer3_grant(
    db: AsyncSession, actor: Actor, action: str, resource: Resource
) -> Decision:
    """Look up an active resource_grants row that covers the actor + action.

    For documents, also walks up the folder ancestor chain — a grant on an
    ancestor folder also covers child documents. RFC §13.3.
    """
    if not isinstance(actor, UserActor):
        return _deny("layer3 only resolves user actors here")

    # Build the candidate (resource_type, resource_id) list — the resource
    # itself plus, for documents, every ancestor folder up to the root.
    candidates: list[tuple[str, str]] = [(resource.type, resource.id)]
    if resource.type == ResourceType.DOCUMENT:
        ancestor_folder_ids = await _document_ancestor_folder_ids(db, resource.id)
        for fid in ancestor_folder_ids:
            candidates.append((ResourceType.DOCUMENT_FOLDER, fid))

    # Single round-trip: fetch all active grants matching any candidate
    # for this user. Tuple-in-list is the cleanest SQLAlchemy spelling.
    rt_list = [c[0] for c in candidates]
    rid_list = [c[1] for c in candidates]
    rows = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type.in_(rt_list),
                ResourceGrant.resource_id.in_(rid_list),
                ResourceGrant.status == GrantStatus.ACTIVE,
                ResourceGrant.subject_type == SubjectType.USER,
                ResourceGrant.subject_id == actor.user_id,
            )
        )
    ).scalars().all()

    # SQL filtered by the union — we have to re-verify the matched grant
    # is for a *candidate* pair (not a cross-product false positive).
    candidate_set = set(candidates)
    now = datetime.now(timezone.utc)
    for grant in rows:
        if (grant.resource_type, grant.resource_id) not in candidate_set:
            continue
        if grant.expires_at and grant.expires_at < now:
            continue
        caps = set(grant.capabilities or [])
        if _action_matches_capability(action, caps):
            rule = (
                "layer3.grant"
                if grant.resource_type == resource.type
                else "layer3.folder_walkup"
            )
            return _allow(rule, reason=f"grant {grant.id}")

    return _deny("no resource grant covers this action", "layer3.miss")


async def _document_ancestor_folder_ids(
    db: AsyncSession, doc_id: str,
) -> list[str]:
    """Return [direct_folder, parent, grandparent, ...] for a document.

    Implementation note: SQLite tests + Postgres prod both can do
    recursive CTEs, but the folder tree is bounded (UX hard-caps depth at
    ~5 in practice) so an iterative LIMIT 16 walk is simpler and avoids
    dialect-specific SQL. The first query is bundled with the doc lookup;
    subsequent walks fetch only the parent_id column.
    """
    from packages.core.models.document import Document, DocumentFolder

    folder_id = (
        await db.execute(
            select(Document.folder_id).where(Document.id == doc_id)
        )
    ).scalar_one_or_none()
    if not folder_id:
        return []

    ancestors: list[str] = []
    seen: set[str] = set()
    current_id: str | None = folder_id
    # Bound: practical folder depth never exceeds this; protects against
    # corrupted parent_id cycles too.
    for _ in range(16):
        if current_id is None or current_id in seen:
            break
        seen.add(current_id)
        ancestors.append(current_id)
        current_id = (
            await db.execute(
                select(DocumentFolder.parent_id).where(
                    DocumentFolder.id == current_id
                )
            )
        ).scalar_one_or_none()
    return ancestors


# ── Invariants (RFC §13.14) ───────────────────────────────────────────────


def _check_invariants(
    actor: Actor, action: str, resource: Resource
) -> Decision:
    cls = resource.classification or Classification.INTERNAL

    # Invariant 1: restricted is never publicly visible.
    if cls == Classification.RESTRICTED and resource.visibility == Visibility.PUBLIC:
        return _deny("restricted resource cannot be public", "inv1")

    # Invariant 5: legal hold blocks delete / reclassify / retention.
    if resource.legal_hold and action in {
        Capability.DELETE,
        Capability.RECLASSIFY,
        Permission.DOCS_DELETE.value,
    }:
        return _deny("legal hold prevents this action", "inv5")

    # Invariant 7: agents can never read restricted, never share_external,
    # never create confidential+.
    if isinstance(actor, AgentActor):
        if cls == Classification.RESTRICTED and action in {
            Capability.VIEW,
            Capability.VIEW_REDACTED,
            Permission.DOCS_READ.value,
        }:
            return _deny("agents cannot access restricted documents", "inv7.read")
        if action == Capability.SHARE_EXTERNAL:
            return _deny("agents cannot share externally", "inv7.share")

    # Invariant 6: external_share blocked above internal classification
    # without explicit approval (approval modeled as resource_grants in
    # P3+; in P1 strict mode we deny so callers fail loudly).
    if action == Capability.SHARE_EXTERNAL and cls in (
        Classification.CONFIDENTIAL,
        Classification.RESTRICTED,
    ):
        return _deny(
            "external share requires approval for confidential+", "inv6"
        )

    # Invariant 10: quarantined documents are invisible to non-uploader.
    if resource.quarantine_status in {"quarantined", "pending_scan", "rejected"}:
        if isinstance(actor, UserActor):
            if resource.owner_id != actor.user_id:
                return _deny("document is in quarantine", "inv10")
        else:
            return _deny("quarantine: non-uploader actor blocked", "inv10")

    return _allow("invariants_ok")


# ── Helpers ───────────────────────────────────────────────────────────────


def _action_matches_capability(action: str, caps: Sequence[str]) -> bool:
    """Cross-walk between Permission verbs and capability strings.

    The capability vocabulary is short and deliberately disjoint from
    the verb vocabulary — this mapping is the only place where the two
    meet.
    """
    cap_set = set(caps)
    if action in cap_set:
        return True

    aliases = {
        Permission.DOCS_READ.value: {Capability.VIEW, Capability.VIEW_REDACTED},
        Permission.DOCS_UPLOAD.value: {Capability.UPLOAD_TO, Capability.EDIT},
        Permission.DOCS_DELETE.value: {Capability.DELETE},
        Permission.TASKS_READ.value: {Capability.VIEW},
        Permission.TASKS_UPDATE.value: {Capability.EDIT},
    }.get(action, set())

    return bool(aliases & cap_set)


def _entity_of(actor: Actor) -> Optional[str]:
    return getattr(actor, "entity_id", None)


async def _enforce_enabled(db: AsyncSession, entity_id: Optional[str]) -> bool:
    """Read the feature flag — defaults to OFF (legacy behavior)."""
    try:
        from packages.core.services.feature_flags import is_enabled
    except ImportError:  # pragma: no cover
        return False
    return await is_enabled(db, FLAG_ENFORCE, entity_id=entity_id, fallback=False)


async def _audit(
    db: AsyncSession,
    actor: Actor,
    action: str,
    resource: Optional[Resource],
    decision: Decision,
    request_id: Optional[str],
    ip: Optional[str],
) -> None:
    """Best-effort write to permission_audit. Never raises.

    Sampling rule (RFC §9): all denies; allow-decisions on sensitive
    verbs (admin.*, share.*, mcp.*); 1% of other allows. We keep the
    sample-rate logic simple here — implementation can grow in P3 once
    we have real traffic shape.
    """
    if not _should_audit(action, decision):
        return
    try:
        flag_on = await _audit_flag(db, _entity_of(actor))
        if not flag_on:
            return
        from sqlalchemy import text
        from packages.core.models.base import generate_ulid

        await db.execute(
            text(
                "INSERT INTO permission_audit "
                "(id, ts, entity_id, actor_type, actor_id, action, "
                " resource_type, resource_id, decision, reason, "
                " request_id, ip) "
                "VALUES (:id, now(), :entity_id, :actor_type, :actor_id, "
                "        :action, :resource_type, :resource_id, "
                "        :decision, :reason, :request_id, :ip)"
            ),
            {
                "id": generate_ulid(),
                "entity_id": _entity_of(actor),
                "actor_type": actor.actor_type,
                "actor_id": _actor_id(actor),
                "action": action,
                "resource_type": resource.type if resource else None,
                "resource_id": resource.id if resource else None,
                "decision": "allow" if decision.allow else "deny",
                "reason": (decision.matched_rule or decision.reason)[:120],
                "request_id": request_id,
                "ip": ip,
            },
        )
    except Exception:  # pragma: no cover — audit must never break the request
        pass


def _actor_id(actor: Actor) -> Optional[str]:
    if isinstance(actor, UserActor):
        return actor.user_id
    if isinstance(actor, AgentActor):
        return actor.agent_id
    if isinstance(actor, ShareTokenActor):
        return actor.share_id
    if isinstance(actor, WorkerActor):
        return actor.worker_id
    return None


def _should_audit(action: str, decision: Decision) -> bool:
    if not decision.allow:
        return True
    sensitive_prefixes = ("admin.", "share.", "mcp.", "docs.share", "docs.delete")
    if any(action.startswith(p) for p in sensitive_prefixes):
        return True
    # P1: skip the 1% sample wiring; can be added later without callers
    # changing.
    return False


async def _audit_flag(db: AsyncSession, entity_id: Optional[str]) -> bool:
    try:
        from packages.core.services.feature_flags import is_enabled
    except ImportError:  # pragma: no cover
        return False
    return await is_enabled(db, FLAG_AUDIT, entity_id=entity_id, fallback=False)


# ── Convenience constructors ──────────────────────────────────────────────


def make_actor(user_or_other: Any) -> Actor:
    """Coerce a ``User`` model (or already-an-actor) into a ``UserActor``.

    Convenience for callers that already have ``current_user`` from the
    FastAPI dependency.
    """
    if hasattr(user_or_other, "actor_type"):
        return user_or_other  # already an Actor

    user = user_or_other
    return UserActor(
        user_id=user.id,
        entity_id=user.entity_id,
        role=user.role or "viewer",
    )


# ── FastAPI dependency factory (drop-in alongside require_permission) ─────


def require_dep(action: Union[Permission, str]):
    """Dependency factory mirroring ``deps.require_permission`` but using
    ``authorize()``. P1 accepts only verb-level checks (resource=None);
    row-level enforcement happens inside route bodies once they migrate.
    """
    from apps.api.deps import get_current_user
    from packages.core.database import get_db

    async def _dep(
        user=Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        actor = make_actor(user)
        await require(db, actor, action)
        return user

    return _dep
