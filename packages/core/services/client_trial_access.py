"""The provider team's access to a delivered customer Workspace, from its own company.

Everyone who can manage a delivery's SOURCE Workspace may open the customer
Workspace: during the trial, and afterwards while the customer keeps the
provider as maintainer. Each person works through their own
``WorkspaceCollaboration`` grant, copied from a live grant the customer's
decision already stands behind (the anchor), so the customer side of live
authority is unchanged; ``grantee_manages_source`` is the provider side.
Opening never switches the person's company: the caller mints a scoped
token for the customer Workspace instead.

Lock order (shared with delivery acceptance and the customer's decisions):
source Workspace -> target Workspace -> the caller's authorization rows with
their User FOR UPDATE -> grants -> each issuer's authorization rows (shared)
-> the derived membership and seat in ``sync_external_access``.
"""

from fastapi import HTTPException
from sqlalchemy import select

from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.people import Client
from packages.core.models.user import Entity, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_collaboration import WorkspaceCollaboration
from packages.core.services.external_collaboration_access import (
    COLLABORATOR_MEMBERSHIP_ROLE,
    MAINTAINER_ROLE,
    PROVIDER_TRIAL_ROLE,
    REMOVED_PROVIDER,
    TRIAL_PHASES,
    WORKSPACE_ACCESS_ROLES,
    collaborator_workspace_ids,
    grant_gives_access,
    now,
    sync_external_access,
)
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    lock_workspace_recipient_authorization,
    user_can_manage_workspace,
)

MANAGER_REQUIRED = {
    "code": "client_trial_manager_required",
    "message": "Only a manager of this Workspace can open its customer trials",
}
UNAVAILABLE = {
    "code": "client_trial_unavailable",
    "message": "This customer Workspace is not open to the provider team",
}
CUSTOMER_MEMBER = {
    "code": "client_trial_customer_member",
    "message": "You are a member of the customer's company; open the Workspace there",
}


def access_for_phase(delivery):
    """The grant role the provider team holds in the delivery's phase, or None.

    After delivery the team works on only while the customer keeps it: a
    removal at acceptance or later closes it until the customer re-authorizes.
    """
    if delivery.phase in TRIAL_PHASES:
        return PROVIDER_TRIAL_ROLE
    if delivery.phase == "delivered" and (delivery.acceptance or {}).get("provider") not in REMOVED_PROVIDER:
        return MAINTAINER_ROLE
    return None


def access_label(role):
    return "trial" if role == PROVIDER_TRIAL_ROLE else "maintainer"


async def _home_role(db, user):
    return await db.scalar(
        select(UserMembership.role).where(
            UserMembership.user_id == user.id,
            UserMembership.entity_id == user.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )


async def _require_source_manager(db, user, workspace_id):
    role = await _home_role(db, user)
    if role is None or role == COLLABORATOR_MEMBERSHIP_ROLE or not await user_can_manage_workspace(
        db, workspace_id=workspace_id, user_id=user.id, entity_role=role
    ):
        raise HTTPException(403, MANAGER_REQUIRED)


async def _live_anchors(db, delivery, ws, role):
    """Active grants of the team's role the customer's authority still stands behind."""
    from packages.core.services.workspace_collaboration import live_boundary

    rows = (
        await db.scalars(
            select(WorkspaceCollaboration)
            .where(
                WorkspaceCollaboration.delivery_id == delivery.id,
                WorkspaceCollaboration.workspace_id == ws.id,
                WorkspaceCollaboration.entity_id == ws.entity_id,
                WorkspaceCollaboration.role == role,
                WorkspaceCollaboration.status == "active",
            )
            .order_by(WorkspaceCollaboration.created_at, WorkspaceCollaboration.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    anchors = []
    for row in rows:
        if not grant_gives_access(row) or not row.authorized_by:
            continue
        try:
            await live_boundary(db, row, lock=True)
        except HTTPException:
            continue
        anchors.append(row)
    return anchors


async def open_client_trial(db, user, source_workspace_id, delivery_id):
    """Ensure the caller's own grant into the delivery's customer Workspace.

    Returns ``(delivery, workspace, grant)``. The caller owns the transaction
    and must not switch the person's company.
    """
    from packages.core.services.workspace_collaboration import audit, fresh_actor

    delivery = await db.scalar(
        select(ClientWorkspaceDelivery).where(
            ClientWorkspaceDelivery.id == delivery_id,
            ClientWorkspaceDelivery.entity_id == user.entity_id,
            ClientWorkspaceDelivery.source_workspace_id == source_workspace_id,
        )
    )
    if delivery is None:
        raise HTTPException(404, "Delivery not found")
    source = await lock_workspace_access_boundary(db, workspace_id=source_workspace_id, entity_id=user.entity_id)
    if source is None or source.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    if not delivery.target_workspace_id:
        raise HTTPException(409, UNAVAILABLE)
    ws = await lock_workspace_access_boundary(
        db, workspace_id=delivery.target_workspace_id, entity_id=delivery.target_entity_id
    )
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(410, "Customer Workspace is no longer available")
    await lock_workspace_recipient_authorization(
        db, workspace_id=source.id, entity_id=source.entity_id, user_id=user.id, user_lock="update"
    )
    current = await fresh_actor(db, user, for_update=True)
    await _require_source_manager(db, user, source.id)
    customer_role = await db.scalar(
        select(UserMembership.role).where(
            UserMembership.user_id == user.id,
            UserMembership.entity_id == ws.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )
    if customer_role is not None and customer_role != COLLABORATOR_MEMBERSHIP_ROLE:
        raise HTTPException(409, CUSTOMER_MEMBER)
    # The phase moves only under the target Workspace lock held above.
    await db.refresh(delivery)
    role = access_for_phase(delivery)
    anchors = await _live_anchors(db, delivery, ws, role) if role else []
    if not anchors:
        raise HTTPException(409, UNAVAILABLE)
    own = await db.scalar(
        select(WorkspaceCollaboration)
        .where(
            WorkspaceCollaboration.delivery_id == delivery.id,
            WorkspaceCollaboration.user_id == user.id,
            WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
            WorkspaceCollaboration.status == "active",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    anchor = anchors[0]
    if own is None:
        own = WorkspaceCollaboration(
            entity_id=ws.entity_id,
            workspace_id=ws.id,
            delivery_id=delivery.id,
            email=current.email.casefold(),
            role=anchor.role,
            status="active",
            authorized_by=anchor.authorized_by,
            authority_fingerprint=anchor.authority_fingerprint,
            user_id=user.id,
            expires_at=anchor.expires_at,
            accepted_at=now(),
            share_configuration=False,
            snapshot={},
        )
        db.add(own)
        await db.flush()
        audit(db, ws, user, "Provider team member joined the trial", {"collaboration_id": own.id, "role": own.role})
    elif own not in anchors:
        # The person's earlier grant lapsed on the customer side; it follows the team's.
        own.role = anchor.role
        own.authorized_by = anchor.authorized_by
        own.authority_fingerprint = anchor.authority_fingerprint
        own.expires_at = anchor.expires_at
    await sync_external_access(db, own)
    if ws.id not in await collaborator_workspace_ids(db, user_id=user.id, entity_id=ws.entity_id):
        raise HTTPException(409, UNAVAILABLE)
    return delivery, ws, own


async def team_access(db, delivery):
    """The role the provider team can open the delivery's Workspace with now, or None."""
    return (await team_access_many(db, [delivery])).get(delivery.id)


async def team_access_many(db, deliveries):
    """{delivery id: role the provider team can open it with now}; deliveries with none are absent.

    Team-level: some grant of the phase's role is live on the customer side.
    One query for all the deliveries' grants, then the customer-side check
    of all candidates together. Plain reads.
    """
    from packages.core.services.workspace_collaboration import live_grant_ids

    roles = {
        delivery.id: role
        for delivery in deliveries
        if delivery.target_workspace_id and (role := access_for_phase(delivery))
    }
    if not roles:
        return {}
    targets = {d.id: (d.target_workspace_id, d.target_entity_id) for d in deliveries if d.id in roles}
    grants = (
        await db.scalars(
            select(WorkspaceCollaboration)
            .where(
                WorkspaceCollaboration.delivery_id.in_(list(roles)),
                WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
                WorkspaceCollaboration.status == "active",
            )
            .order_by(WorkspaceCollaboration.created_at, WorkspaceCollaboration.id)
        )
    ).all()
    candidates = [
        grant
        for grant in grants
        if grant.role == roles[grant.delivery_id]
        and (grant.workspace_id, grant.entity_id) == targets[grant.delivery_id]
        and grant_gives_access(grant)
        and grant.authorized_by
    ]
    live = await live_grant_ids(db, candidates)
    return {grant.delivery_id: grant.role for grant in candidates if grant.id in live}


async def list_client_trials(db, user, source_workspace_id):
    """Deliveries of one source Workspace that the provider team can open now."""
    source = await db.scalar(
        select(Workspace).where(Workspace.id == source_workspace_id, Workspace.entity_id == user.entity_id)
    )
    if source is None or source.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    await _require_source_manager(db, user, source.id)
    deliveries = (
        await db.scalars(
            select(ClientWorkspaceDelivery)
            .where(
                ClientWorkspaceDelivery.entity_id == user.entity_id,
                ClientWorkspaceDelivery.source_workspace_id == source.id,
                ClientWorkspaceDelivery.target_workspace_id.is_not(None),
            )
            .order_by(ClientWorkspaceDelivery.accepted_at.desc().nullslast(), ClientWorkspaceDelivery.id)
        )
    ).all()
    items = []
    for delivery in deliveries:
        role = await team_access(db, delivery)
        if role is None:
            continue
        client_name = await db.scalar(select(Client.name).where(Client.id == delivery.client_id))
        customer_name = await db.scalar(select(Entity.name).where(Entity.id == delivery.target_entity_id))
        items.append({
            "delivery_id": delivery.id,
            "client_id": delivery.client_id,
            "client_name": client_name,
            "customer_name": customer_name,
            "title": (delivery.payload or {}).get("manifest", {}).get("title"),
            "phase": delivery.phase,
            "access": access_label(role),
            "target_workspace_id": delivery.target_workspace_id,
            "target_entity_id": delivery.target_entity_id,
            "accepted_at": delivery.accepted_at,
        })
    return items


async def revoke_provider_team(db, ws, delivery, user):
    """The customer removes the whole provider team from a delivered Workspace.

    Revokes every access grant of the delivery, pending requests and
    invitations included so none can restore the team later, records the
    removal on the delivery so the team can no longer open it, and audits
    it. The caller
    holds the target Workspace and the delivery locked (Workspace first) and
    owns the transaction.
    """
    from packages.core.services.workspace_collaboration import audit

    grants = (
        await db.scalars(
            select(WorkspaceCollaboration)
            .where(
                WorkspaceCollaboration.delivery_id == delivery.id,
                WorkspaceCollaboration.workspace_id == ws.id,
                WorkspaceCollaboration.entity_id == ws.entity_id,
                WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
                WorkspaceCollaboration.status.in_(("requested", "invited", "active")),
            )
            .order_by(WorkspaceCollaboration.created_at, WorkspaceCollaboration.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for grant in grants:
        grant.status = "revoked"
        grant.snapshot = {}
        await sync_external_access(db, grant)
    delivery.acceptance = {
        **(delivery.acceptance or {}),
        "provider": "removed",
        "removed_at": now().isoformat(),
        "removed_by": user.id,
    }
    audit(
        db, ws, user, "Provider team removed", {"delivery_id": delivery.id, "collaboration_ids": [g.id for g in grants]}
    )
    return grants
