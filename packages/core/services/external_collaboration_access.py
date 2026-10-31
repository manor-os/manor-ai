"""Customer-authorized external collaborators working inside one customer Workspace.

A WorkspaceCollaboration grant is the only authority. The membership and
WorkspaceStaff rows derived from it are rewritten by ``sync_external_access``
whenever the grant changes.

Invariant: the derived rows are a superset of live access. They are written
when a grant is live and removed when it is not, but they can briefly outlive
the authority behind it (an expiry passing, the issuing manager losing their
role). ``collaborator_workspace_ids`` re-checks that authority on every call
and is the only gate for an actor whose membership role is
``COLLABORATOR_MEMBERSHIP_ROLE``; HTTP and runtime boundaries must use it and
never treat the derived seat alone as permission.
"""

from datetime import datetime, timezone

from sqlalchemy import exists, or_, select, update

from packages.core.models.staff import Staff
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import WorkspaceStaff
from packages.core.models.workspace_collaboration import WorkspaceCollaboration

PROVIDER_TRIAL_ROLE = "provider_trial"
MAINTAINER_ROLE = "maintainer"
# Roles whose active grant places the person inside the Workspace.
WORKSPACE_ACCESS_ROLES = frozenset({PROVIDER_TRIAL_ROLE, MAINTAINER_ROLE})
# Distinct from "external", which is the home role of public-signup customers.
COLLABORATOR_MEMBERSHIP_ROLE = "collaborator"
TRIAL_PHASES = frozenset({"trial", "awaiting_acceptance"})
# delivery.acceptance["provider"] once the customer removed the provider team.
REMOVED_PROVIDER = frozenset({"remove", "removed"})
# The Workspace role a live grant confers: configuration edits need MANAGE.
SEAT_ROLE = "owner"


def now():
    return datetime.now(timezone.utc)


def grant_expired(row, at=None):
    """A NULL expiry never lapses; revocation is a separate status."""
    return row.expires_at is not None and row.expires_at <= (at or now())


def grant_gives_access(row, at=None):
    return (
        row.role in WORKSPACE_ACCESS_ROLES
        and row.status == "active"
        and row.user_id is not None
        and not grant_expired(row, at)
    )


async def _other_live_grants(db, row):
    rows = (
        await db.scalars(
            select(WorkspaceCollaboration)
            .where(
                WorkspaceCollaboration.user_id == row.user_id,
                WorkspaceCollaboration.entity_id == row.entity_id,
                WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
                WorkspaceCollaboration.status == "active",
                WorkspaceCollaboration.id != row.id,
            )
            .order_by(WorkspaceCollaboration.created_at, WorkspaceCollaboration.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [other for other in rows if grant_gives_access(other)]


async def _seats_owned_by(db, row):
    return (
        await db.scalars(
            select(WorkspaceStaff)
            .where(WorkspaceStaff.collaboration_id == row.id)
            .order_by(WorkspaceStaff.id)
            .with_for_update()
        )
    ).all()


async def home_membership(db, user_id, entity_id):
    """The person's own company membership other than ``entity_id``, primary first."""
    return await db.scalar(
        select(UserMembership)
        .where(
            UserMembership.user_id == user_id,
            UserMembership.entity_id != entity_id,
            UserMembership.role != COLLABORATOR_MEMBERSHIP_ROLE,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
        .order_by(UserMembership.is_primary.desc(), UserMembership.created_at)
        .limit(1)
    )


async def _return_home(db, user_id, entity_id):
    """Move a persisted company switch off the customer company it lost.

    Written with an UPDATE rather than through the ORM User: when the grantee is
    the caller, the identity-mapped User carries the request-local company view
    from deps.get_current_user, which must not be overwritten mid-request.
    """
    from packages.core.services.auth_service import normalize_membership_role

    home = await home_membership(db, user_id, entity_id)
    if home is not None:
        await db.execute(
            update(User)
            .where(User.id == user_id, User.entity_id == entity_id)
            .values(entity_id=home.entity_id, role=normalize_membership_role(home.role))
            .execution_options(synchronize_session=False)
        )


async def _hand_webchats_to_customer(db, row):
    """Give the customer the webchats the collaborator created in this Workspace.

    A public webchat keeps serving its visitors after the provider leaves; its
    owner becomes the person who authorized the grant, or a current owner of
    the Workspace when that person is no longer a member.
    """
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    owner = None
    if row.authorized_by:
        owner = await db.scalar(
            select(UserMembership.user_id).where(
                UserMembership.user_id == row.authorized_by,
                UserMembership.entity_id == row.entity_id,
                UserMembership.role != COLLABORATOR_MEMBERSHIP_ROLE,
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )
    if owner is None:
        owner = await db.scalar(
            select(WorkspaceStaff.user_id)
            .where(
                WorkspaceStaff.workspace_id == row.workspace_id,
                WorkspaceStaff.role == SEAT_ROLE,
                WorkspaceStaff.status == "active",
                WorkspaceStaff.collaboration_id.is_(None),
                WorkspaceStaff.user_id.is_not(None),
            )
            .order_by(WorkspaceStaff.added_at, WorkspaceStaff.id)
            .limit(1)
        )
    if owner is None:
        return
    moved = (
        await db.scalars(
            update(ChannelConfig)
            .where(
                ChannelConfig.entity_id == row.entity_id,
                ChannelConfig.workspace_id == row.workspace_id,
                ChannelConfig.owner_user_id == row.user_id,
                ChannelConfig.channel_type == "webchat",
            )
            .values(owner_user_id=owner)
            .returning(ChannelConfig.id)
            .execution_options(synchronize_session="fetch")
        )
    ).all()
    if moved:
        # Routing admits a binding only when it carries its config's owner
        # (channel_bindings.load_channel_binding_scopes_for_config).
        await db.execute(
            update(Channel)
            .where(
                Channel.entity_id == row.entity_id,
                Channel.config["channel_config_id"].astext.in_(moved),
            )
            .values(user_id=owner)
            .execution_options(synchronize_session="fetch")
        )


async def sync_external_access(db, row):
    """Make the derived membership and Workspace seat match one grant.

    A genuine member of the customer company keeps their membership and any
    customer-managed seat untouched; only rows this grant derived change.
    Lock order: Workspace, User, membership, seat. The caller owns the
    transaction.
    """
    if row.role not in WORKSPACE_ACCESS_ROLES:
        return
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    # Workspace first, matching the lock order of invite, revoke and portal reads.
    await lock_workspace_access_boundary(db, workspace_id=row.workspace_id, entity_id=row.entity_id)
    if row.user_id is None:
        # An anonymized grant has no person left; only its seats remain to clear.
        for seat in await _seats_owned_by(db, row):
            await db.delete(seat)
        await db.flush()
        return
    live = grant_gives_access(row)

    # User before membership, as fresh_actor does. Columns only: loading the
    # ORM User would refresh a caller's request-local company view.
    user = (
        await db.execute(select(User.id, User.entity_id).where(User.id == row.user_id).with_for_update())
    ).one_or_none()
    # Read under the User lock so a concurrent sync for the same person is observed.
    others = await _other_live_grants(db, row)
    membership = await db.scalar(
        select(UserMembership)
        .where(UserMembership.user_id == row.user_id, UserMembership.entity_id == row.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    real_member = (
        membership is not None
        and membership.role != COLLABORATOR_MEMBERSHIP_ROLE
        and membership.deleted_at is None
    )
    if not real_member:
        if live and membership is None:
            # Created directly: a collaborator gets no personal approval setup.
            db.add(UserMembership(
                user_id=row.user_id,
                entity_id=row.entity_id,
                role=COLLABORATOR_MEMBERSHIP_ROLE,
                status="active",
                is_primary=False,
            ))
        elif live:
            membership.role = COLLABORATOR_MEMBERSHIP_ROLE
            membership.status = "active"
            membership.deleted_at = None
            membership.is_primary = False
        elif membership is not None and membership.role == COLLABORATOR_MEMBERSHIP_ROLE and not others:
            membership.status = "inactive"
            if user is not None and user.entity_id == row.entity_id:
                await _return_home(db, row.user_id, row.entity_id)

    # A legacy seat may be keyed by the person's Staff row instead of user_id.
    staff_ids = select(Staff.id).where(Staff.entity_id == row.entity_id, Staff.user_id == row.user_id)
    seats = (
        await db.scalars(
            select(WorkspaceStaff)
            .where(
                WorkspaceStaff.workspace_id == row.workspace_id,
                or_(WorkspaceStaff.user_id == row.user_id, WorkspaceStaff.staff_id.in_(staff_ids)),
            )
            .order_by(WorkspaceStaff.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    derived = next((seat for seat in seats if seat.collaboration_id is not None), None)
    customer_managed = any(seat.collaboration_id is None for seat in seats)
    successor = next((other for other in others if other.workspace_id == row.workspace_id), None)
    source = row if live else successor
    if source is None:
        for seat in await _seats_owned_by(db, row):
            await db.delete(seat)
        if not real_member:
            await _hand_webchats_to_customer(db, row)
    elif derived is not None:
        # One seat per person and Workspace: the live grant owns it.
        derived.collaboration_id = source.id
        derived.role = SEAT_ROLE
        derived.status = "active"
        derived.expires_at = source.expires_at
    elif not customer_managed:
        db.add(WorkspaceStaff(
            workspace_id=row.workspace_id,
            user_id=row.user_id,
            staff_id=None,
            role=SEAT_ROLE,
            added_by=source.authorized_by,
            added_at=now(),
            expires_at=source.expires_at,
            status="active",
            collaboration_id=source.id,
        ))
    await db.flush()


async def lock_access_grant_workspaces(db, *conditions):
    """Lock, Workspace-first, the Workspaces of access grants matching any condition.

    Returns the grant ids so a bulk revoke can re-derive them with
    ``sync_access_grants`` without inverting the Workspace -> grant order.
    """
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    rows = (
        await db.execute(
            select(WorkspaceCollaboration.id, WorkspaceCollaboration.workspace_id, WorkspaceCollaboration.entity_id)
            .where(WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES), or_(*conditions))
            .order_by(WorkspaceCollaboration.id)
        )
    ).all()
    for workspace_id, entity_id in sorted({(r.workspace_id, r.entity_id) for r in rows}):
        await lock_workspace_access_boundary(db, workspace_id=workspace_id, entity_id=entity_id)
    return [r.id for r in rows]


async def sync_access_grants(db, grant_ids):
    for grant_id in grant_ids:
        row = await db.scalar(
            select(WorkspaceCollaboration)
            .where(WorkspaceCollaboration.id == grant_id)
            .execution_options(populate_existing=True)
        )
        if row is not None:
            await sync_external_access(db, row)


async def grantee_manages_source(db, row):
    """Whether a delivery grant's holder still manages the delivery's source Workspace.

    A provider reaches the customer Workspace only on behalf of the Workspace
    they delivered: losing manage on the source in their own company ends the
    access. A grant without a delivery (a customer's own invitation) has no
    source side. Plain reads, for the per-request gate.
    """
    if row.delivery_id is None:
        return True
    from packages.core.models.client_delivery import ClientWorkspaceDelivery
    from packages.core.services.workspace_access import user_can_manage_workspace

    delivery = (
        await db.execute(
            select(ClientWorkspaceDelivery.entity_id, ClientWorkspaceDelivery.source_workspace_id).where(
                ClientWorkspaceDelivery.id == row.delivery_id,
                ClientWorkspaceDelivery.target_workspace_id == row.workspace_id,
                ClientWorkspaceDelivery.target_entity_id == row.entity_id,
            )
        )
    ).one_or_none()
    if delivery is None or row.user_id is None:
        return False
    role = await db.scalar(
        select(UserMembership.role).where(
            UserMembership.user_id == row.user_id,
            UserMembership.entity_id == delivery.entity_id,
            UserMembership.role != COLLABORATOR_MEMBERSHIP_ROLE,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )
    active = await db.scalar(
        select(User.id).where(User.id == row.user_id, User.status == "active", User.deleted_at.is_(None))
    )
    return bool(role and active) and await user_can_manage_workspace(
        db, workspace_id=delivery.source_workspace_id, user_id=row.user_id, entity_role=role
    )


async def collaborator_workspace_ids(db, *, user_id, entity_id):
    """Workspaces a live grant places this person in; reads only, no row locks."""
    return frozenset(row.workspace_id for row in await live_collaborator_grants(db, user_id=user_id, entity_id=entity_id))


async def live_collaborator_grants(db, *, user_id, entity_id):
    """The person's live access grants in a company; reads only, no row locks.

    Live authority has two sides: the customer side (the issuing manager, via
    ``live_boundary``) and, for a delivery grant, the provider side (the
    grantee still manages the delivered source Workspace,
    ``grantee_manages_source``).
    """
    rows = (
        await db.scalars(
            select(WorkspaceCollaboration)
            .where(
                WorkspaceCollaboration.user_id == user_id,
                WorkspaceCollaboration.entity_id == entity_id,
                WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
                WorkspaceCollaboration.status == "active",
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    source_side = {}
    return [row for row in rows if await grant_is_live(db, row, source_side=source_side)]


async def grant_is_live(db, row, *, source_side=None):
    """Whether this access grant places its holder in the Workspace now; plain reads.

    Both sides of live authority: the customer side (``live_boundary``, which
    checks the issuer only when there is one: no issuer, no authority) and,
    for a delivery grant, the provider side (``grantee_manages_source``).
    ``source_side`` caches the provider side per (delivery, user) across calls.
    """
    from fastapi import HTTPException

    from packages.core.services.workspace_collaboration import live_boundary

    if not grant_gives_access(row) or not row.authorized_by:
        return False
    try:
        await live_boundary(db, row, lock=False)
    except HTTPException:
        return False
    cache = {} if source_side is None else source_side
    key = (row.delivery_id, row.user_id)
    if key not in cache:
        cache[key] = await grantee_manages_source(db, row)
    return cache[key]


async def collaboration_ended(db, *, user_id, entity_id):
    """Whether the person's only standing in ``entity_id`` was collaboration that has ended.

    True for a derived collaborator membership that is no longer active, or is
    active but no grant behind it is live (the derived rows outliving the
    authority). A genuine member, or a person with no membership, is never ended.
    """
    row = (
        await db.execute(
            select(UserMembership.role, UserMembership.status, UserMembership.deleted_at).where(
                UserMembership.user_id == user_id, UserMembership.entity_id == entity_id
            )
        )
    ).one_or_none()
    if row is None or row.role != COLLABORATOR_MEMBERSHIP_ROLE:
        return False
    if row.status != "active" or row.deleted_at is not None:
        return True
    return not await collaborator_workspace_ids(db, user_id=user_id, entity_id=entity_id)


async def provider_trial_reconcile_candidates(db):
    """Grants reconcile_provider_trial_access would visit, as (id, needs_fingerprint).

    Live access grants plus any grant that still has derived rows to clear.
    Read-only, so it also serves as the reconcile dry run.
    """
    grant = WorkspaceCollaboration
    owns_seat = exists().where(WorkspaceStaff.collaboration_id == grant.id)
    has_membership = exists().where(
        UserMembership.user_id == grant.user_id,
        UserMembership.entity_id == grant.entity_id,
        UserMembership.role == COLLABORATOR_MEMBERSHIP_ROLE,
        UserMembership.status == "active",
    )
    rows = await db.execute(
        select(
            grant.id,
            (grant.role == PROVIDER_TRIAL_ROLE) & grant.authority_fingerprint.is_(None),
        )
        .where(
            grant.role.in_(WORKSPACE_ACCESS_ROLES),
            or_(grant.status == "active", owns_seat, has_membership),
        )
        .order_by(grant.created_at, grant.id)
    )
    return [(grant_id, bool(needs_fingerprint)) for grant_id, needs_fingerprint in rows.all()]


async def reconcile_provider_trial_access(db):
    """Fill backfilled trial fingerprints and re-derive access, one grant per commit.

    Covers live access grants plus any grant that still has derived rows to
    clear. Returns the number of grants reconciled. Idempotent.
    """
    from packages.core.services.workspace_access import lock_workspace_access_boundary
    from packages.core.services.workspace_collaboration import issuer_fingerprint

    grant = WorkspaceCollaboration
    grant_ids = [grant_id for grant_id, _ in await provider_trial_reconcile_candidates(db)]
    for grant_id in grant_ids:
        stub = await db.get(WorkspaceCollaboration, grant_id)
        if stub is None:
            continue
        ws = await lock_workspace_access_boundary(db, workspace_id=stub.workspace_id, entity_id=stub.entity_id)
        # Re-read under the Workspace lock so a concurrent revoke is observed.
        row = await db.scalar(
            select(grant).where(grant.id == grant_id).execution_options(populate_existing=True)
        )
        await sync_external_access(db, row)
        # The grant row is written last, after User and membership, as guest reads lock it.
        if ws is not None and row.role == PROVIDER_TRIAL_ROLE and row.authority_fingerprint is None and row.authorized_by:
            row.authority_fingerprint = await issuer_fingerprint(db, ws, row.authorized_by)
        await db.commit()
    return len(grant_ids)
