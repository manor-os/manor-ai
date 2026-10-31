"""Bounded external portal. Guests never acquire a customer company actor."""

from datetime import timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.chat_room import ChatRoom, ChatRoomMember
from packages.core.models.workspace import Workspace, WorkspaceActivity, WorkspaceStaff
from packages.core.models.workspace_collaboration import WorkspaceCollaboration
from packages.core.services.external_collaboration_access import (
    COLLABORATOR_MEMBERSHIP_ROLE,
    MAINTAINER_ROLE,
    PROVIDER_TRIAL_ROLE,
    REMOVED_PROVIDER,
    TRIAL_PHASES,
    WORKSPACE_ACCESS_ROLES,
    grant_expired,
    grant_is_live,
    grantee_manages_source,
    now,
    sync_external_access,
)
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    lock_workspace_recipient_authorization,
    user_can_manage_workspace,
)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=255, pattern="^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")
    role: Literal["tester", "maintainer"] = "tester"
    # None keeps a grant that has no expiry (a kept maintainer) without one; only on update.
    days: int | None = Field(default=14, ge=1, le=90)
    share_configuration: bool = False


class GroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str | None = Field(default=None, min_length=1, max_length=26)


GROUP_SETTING = "collaboration_group_id"


def group_view(room):
    return {"id": room.id, "title": room.title, "path": f"/chat?room={room.id}"}


def live_groups(user_id):
    # Same live room/member/owner boundary as native Group chat. Linking is
    # navigation only: it never creates membership or makes the room an actor.
    return (
        select(ChatRoom)
        .join(ChatRoomMember, ChatRoomMember.room_id == ChatRoom.id)
        .where(
            ChatRoom.kind == "group",
            ChatRoom.status == "active",
            ChatRoomMember.user_id == user_id,
            ChatRoomMember.status == "active",
            or_(
                select(Entity.id).where(Entity.id == ChatRoom.owner_entity_id, Entity.deleted_at.is_(None)).exists(),
                select(User.id)
                .where(User.id == ChatRoom.owner_user_id, User.deleted_at.is_(None), User.status == "active")
                .exists(),
            ),
        )
    )


async def communication(db, ws, user):
    group_id = (ws.settings or {}).get(GROUP_SETTING)
    room = await db.scalar(live_groups(user.id).where(ChatRoom.id == group_id)) if group_id else None
    # Do not expose a private room id/title to a nonmember or after closure.
    return {"linked": bool(group_id), "group": group_view(room) if room else None}


async def link_group(db, user, workspace_id, req):
    # Native Group binding changes lock room/member before Workspace. Preserve
    # that order so linking cannot deadlock with a concurrent Agent binding.
    room = None
    if req.group_id:
        room = await db.scalar(
            live_groups(user.id)
            .where(ChatRoom.id == req.group_id)
            .with_for_update(read=True, of=(ChatRoom, ChatRoomMember))
        )
        if room is None:
            raise HTTPException(404, "Join an active Group before linking it")
    ws = await managed_workspace(db, user, workspace_id)
    settings = dict(ws.settings or {})
    previous = settings.pop(GROUP_SETTING, None)
    if room:
        settings[GROUP_SETTING] = room.id
    ws.settings = settings
    if previous != req.group_id:
        audit(db, ws, user, "Collaboration Group changed", {"group_id": req.group_id})
    return {"linked": room is not None, "group": group_view(room) if room else None}


def public_grant(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "workspace_id",
            "delivery_id",
            "email",
            "role",
            "status",
            "expires_at",
            "accepted_at",
            "share_configuration",
        )
    } | {
        "status": "expired" if row.status != "revoked" and grant_expired(row) else row.status,
        "path": f"/workspace-collaboration/{row.id}",
        # One of the provider team's grants: the customer decides on the team as a whole.
        "team": bool(row.delivery_id) and row.role in WORKSPACE_ACCESS_ROLES,
    }


def team_grants(delivery_id, ws, *roles):
    """The delivery's active access grants in this Workspace, oldest first, locked."""
    return (
        select(WorkspaceCollaboration)
        .where(
            WorkspaceCollaboration.delivery_id == delivery_id,
            WorkspaceCollaboration.workspace_id == ws.id,
            WorkspaceCollaboration.entity_id == ws.entity_id,
            WorkspaceCollaboration.role.in_(roles or WORKSPACE_ACCESS_ROLES),
            WorkspaceCollaboration.status == "active",
        )
        .order_by(WorkspaceCollaboration.created_at, WorkspaceCollaboration.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def target_delivery(db, ws, *, delivery_id=None, lock=False):
    """The delivery that installed this Workspace; locked after the Workspace."""
    query = select(ClientWorkspaceDelivery).where(
        ClientWorkspaceDelivery.target_workspace_id == ws.id,
        ClientWorkspaceDelivery.target_entity_id == ws.entity_id,
    )
    if delivery_id is not None:
        query = query.where(ClientWorkspaceDelivery.id == delivery_id)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


def audit(db, ws, user, event, details):
    db.add(
        WorkspaceActivity(
            workspace_id=ws.id,
            entity_id=ws.entity_id,
            user_id=user.id,
            event_type="external_collaboration",
            summary=event,
            details=details,
        )
    )


async def managed_workspace(db, user, workspace_id):
    ws = await lock_workspace_access_boundary(db, workspace_id=workspace_id, entity_id=user.entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    await lock_workspace_recipient_authorization(db, workspace_id=ws.id, entity_id=ws.entity_id, user_id=user.id)
    await fresh_actor(db, user)
    # Read the membership itself: a derived owner seat must never let a
    # collaborator decide who else works in the customer's Workspace.
    role = await db.scalar(
        select(UserMembership.role).where(
            UserMembership.user_id == user.id,
            UserMembership.entity_id == ws.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )
    if role == COLLABORATOR_MEMBERSHIP_ROLE:
        raise HTTPException(403, "External collaborators cannot change this Workspace's collaboration")
    if not await user_can_manage_workspace(db, workspace_id=ws.id, user_id=user.id, entity_role=user.role):
        raise HTTPException(403, "A customer Workspace manager must approve this action")
    return ws


async def live_boundary(db, row, *, lock=True):
    # lock=False runs the same checks with plain reads, for per-request lookups
    # that must not serialize against Workspace lifecycle writes.
    if lock:
        ws = await lock_workspace_access_boundary(db, workspace_id=row.workspace_id, entity_id=row.entity_id)
    else:
        ws = await db.scalar(
            select(Workspace)
            .where(Workspace.id == row.workspace_id, Workspace.entity_id == row.entity_id)
            .execution_options(populate_existing=True)
        )
    entity = await db.scalar(select(Entity.id).where(Entity.id == row.entity_id, Entity.deleted_at.is_(None)))
    if ws is None or ws.deleted_at is not None or entity is None:
        raise HTTPException(410, "Workspace is no longer available")
    if row.status == "revoked" or grant_expired(row):
        raise HTTPException(410, "Collaboration expired or revoked")
    if row.authorized_by:
        if lock:
            await lock_workspace_recipient_authorization(
                db, workspace_id=ws.id, entity_id=ws.entity_id, user_id=row.authorized_by
            )
        # Resolve the issuing identity in the customer company; never use the guest's company role.
        # Ordinary same-company authorization below also fences revoked/deleted issuers.
        issuer = await db.scalar(
            select(User).where(User.id == row.authorized_by, User.status == "active", User.deleted_at.is_(None))
        )
        role = await db.scalar(
            select(UserMembership.role).where(
                UserMembership.user_id == row.authorized_by,
                UserMembership.entity_id == ws.entity_id,
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )
        if row.authority_fingerprint != await issuer_fingerprint(db, ws, row.authorized_by):
            raise HTTPException(410, "The issuing authority changed; request a new invitation")
        if (
            issuer is None
            or role is None
            or not await user_can_manage_workspace(db, workspace_id=ws.id, user_id=row.authorized_by, entity_role=role)
        ):
            raise HTTPException(410, "The issuing manager is no longer authorized")
    return ws


async def guest_grant(db, user, grant_id, *, active=True, for_update=False):
    # Workspace-first lock order is shared by invite, revoke and portal access.
    # for_update: the caller writes the guest's derived access (User FOR UPDATE).
    stub = await db.get(WorkspaceCollaboration, grant_id)
    if stub is None or user.email.casefold() != stub.email:
        raise HTTPException(404, "Collaboration not found")
    ws = await live_boundary(db, stub)
    # Freeze the guest identity before the child grant, matching user deletion's
    # User -> grant order. This prevents a stale proof and a revoke/read deadlock.
    current = await fresh_actor(db, user, verified=True, for_update=for_update)
    row = await db.scalar(
        select(WorkspaceCollaboration)
        .where(WorkspaceCollaboration.id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.email != current.email.casefold():
        raise HTTPException(404, "Collaboration not found")
    if row.status == "revoked" or grant_expired(row):
        raise HTTPException(410, "Collaboration expired or revoked")
    if row.user_id and row.user_id != user.id:
        raise HTTPException(404, "Collaboration not found")
    await live_boundary(db, row)
    if active and row.status != "active":
        raise HTTPException(403, "Accept an approved invitation before continuing")
    return ws, row


async def share_snapshot(db, ws):
    # The canonical exporter scrubs runtime references/secrets. Share only explicitly
    # approved editable operating sections; never credentials, Knowledge or histories.
    from packages.core.blueprints.exporter import export_workspace, ExportContext

    payload = await export_workspace(
        db,
        ws.id,
        title=ws.name,
        context=ExportContext(knowledge_pack_mode="skeleton", knowledge_document_ids=frozenset()),
    )
    model = payload["recipe"].get("operating_model", {})
    keys = {"service_key", "process_key", "key", "name", "title", "description", "role"}

    def descriptions(section):
        return [
            {key: value for key, value in item.items() if key in keys and isinstance(value, str)}
            for item in model.get(section, [])
            if isinstance(item, dict)
        ]

    return {
        "revision": ws.operation_revision,
        "services": descriptions("services"),
        "processes": descriptions("processes"),
    }


async def invite(db, user, workspace_id, req):
    ws = await managed_workspace(db, user, workspace_id)
    if req.email.casefold() == user.email.casefold():
        raise HTTPException(422, "Invite an external collaborator rather than yourself")
    if req.days is None:
        raise HTTPException(422, "Choose how long the invitation lasts")
    duplicate = await db.scalar(
        select(WorkspaceCollaboration.id)
        .where(
            WorkspaceCollaboration.workspace_id == ws.id,
            WorkspaceCollaboration.email == req.email.casefold(),
            WorkspaceCollaboration.status.in_(["requested", "invited", "active"]),
            or_(WorkspaceCollaboration.expires_at.is_(None), WorkspaceCollaboration.expires_at > now()),
        )
        .limit(1)
    )
    if duplicate:
        raise HTTPException(
            409, "An invitation or active collaboration already exists; authorize it or revoke it first"
        )
    row = WorkspaceCollaboration(
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        email=req.email.casefold(),
        role=req.role,
        status="invited",
        authorized_by=user.id,
        authority_fingerprint=await issuer_fingerprint(db, ws, user.id),
        expires_at=now() + timedelta(days=req.days),
        share_configuration=req.share_configuration,
        snapshot=await share_snapshot(db, ws) if req.share_configuration else {},
    )
    db.add(row)
    await db.flush()
    audit(db, ws, user, "External collaboration invited", {"collaboration_id": row.id, "role": row.role})
    return public_grant(row)


async def request_maintenance(db, user, client_id, delivery_id):
    from packages.core.services.client_delivery import client_record, can_manage_delivery

    await client_record(db, user.entity_id, client_id, lock=True)
    row = await db.scalar(
        select(ClientWorkspaceDelivery).where(
            ClientWorkspaceDelivery.id == delivery_id,
            ClientWorkspaceDelivery.entity_id == user.entity_id,
            ClientWorkspaceDelivery.client_id == client_id,
        )
    )
    if row is None or not row.target_workspace_id:
        raise HTTPException(404, "Accepted delivery not found")
    if not await can_manage_delivery(db, user, row):
        raise HTTPException(403, "Only a source Workspace manager may request maintenance")
    # The target Workspace before the requester's User row, as in open_client_trial and guest_grant.
    ws = await lock_workspace_access_boundary(db, workspace_id=row.target_workspace_id, entity_id=row.target_entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(410, "Customer Workspace is no longer available")
    await fresh_actor(db, user, verified=True)
    # The phase moves only under the Workspace lock held above.
    await db.refresh(row)
    if row.phase in TRIAL_PHASES:
        raise HTTPException(
            409, "During the trial every manager of the Workspace opens it from Customer trials; no request is needed"
        )
    existing = await db.scalar(
        select(WorkspaceCollaboration)
        .where(
            WorkspaceCollaboration.delivery_id == row.id,
            WorkspaceCollaboration.email == user.email.casefold(),
            WorkspaceCollaboration.status.in_(["requested", "invited", "active"]),
            or_(WorkspaceCollaboration.expires_at.is_(None), WorkspaceCollaboration.expires_at > now()),
        )
        .order_by(WorkspaceCollaboration.created_at.desc())
        .limit(1)
    )
    if existing:
        return public_grant(existing)
    grant = WorkspaceCollaboration(
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        delivery_id=row.id,
        email=user.email.casefold(),
        requested_by=user.id,
        role="maintainer",
        status="requested",
        expires_at=now() + timedelta(days=14),
        snapshot={},
    )
    db.add(grant)
    await db.flush()
    audit(db, ws, user, "Provider requested maintenance authorization", {"collaboration_id": grant.id})
    return public_grant(grant)


async def management(db, user, workspace_id):
    ws = await managed_workspace(db, user, workspace_id)
    grants = (
        await db.scalars(
            select(WorkspaceCollaboration)
            .where(WorkspaceCollaboration.workspace_id == ws.id, WorkspaceCollaboration.entity_id == ws.entity_id)
            .order_by(WorkspaceCollaboration.created_at.desc())
        )
    ).all()
    delivery = await target_delivery(db, ws)
    return {
        "workspace_id": ws.id,
        "title": ws.name,
        "status": ws.status,
        "grants": [public_grant(r) for r in grants],
        "communication": await communication(db, ws, user),
        "group_options": [
            group_view(room)
            for room in (
                await db.scalars(
                    live_groups(user.id).order_by(ChatRoom.last_message_at.desc().nullslast(), ChatRoom.id).limit(100)
                )
            ).all()
        ],
        "phase": delivery.phase if delivery else None,
        "acceptance": delivery.acceptance if delivery else {},
        "provider_team": await provider_team(db, delivery, grants),
    }


async def provider_team(db, delivery, grants):
    """Who of the provider company works in the Workspace now, decided on as one team.

    Only live members: a teammate who no longer manages the source Workspace
    is not in the team, though their derived seat may briefly remain.
    """
    if delivery is None:
        return None
    source_side = {}
    members = [
        g for g in reversed(grants)
        if g.delivery_id == delivery.id
        and g.role in WORKSPACE_ACCESS_ROLES
        and await grant_is_live(db, g, source_side=source_side)
    ]
    roles = {g.role for g in members}
    return {
        "delivery_id": delivery.id,
        "access": "trial" if PROVIDER_TRIAL_ROLE in roles else "maintainer" if roles else "none",
        "members": [
            {"grant_id": g.id, "email": g.email, "user_id": g.user_id, "role": g.role, "accepted_at": g.accepted_at}
            for g in members
        ],
    }


async def reauthorize_provider(db, ws, row):
    """The customer authorizes the provider again after removing the team.

    Clears the removal marker so the authorized grant can be accepted; a
    grant authorized before the removal was revoked with the team and can
    never be accepted.
    """
    delivery = await target_delivery(db, ws, delivery_id=row.delivery_id, lock=True)
    acceptance = (delivery.acceptance or {}) if delivery else {}
    if acceptance.get("provider") in REMOVED_PROVIDER:
        delivery.acceptance = {**acceptance, "provider": "reauthorized", "reauthorized_at": now().isoformat()}


async def change_grant(db, user, workspace_id, grant_id, action, req=None):
    ws = await managed_workspace(db, user, workspace_id)
    row = await db.scalar(
        select(WorkspaceCollaboration)
        .where(
            WorkspaceCollaboration.id == grant_id,
            WorkspaceCollaboration.workspace_id == ws.id,
            WorkspaceCollaboration.entity_id == ws.entity_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Collaboration not found")
    # Workspace, grant, then the delivery: the same order as phase() and accept().
    delivery = (
        await target_delivery(db, ws, delivery_id=row.delivery_id, lock=True)
        if row.delivery_id and row.role in WORKSPACE_ACCESS_ROLES
        else None
    )
    if row.role == PROVIDER_TRIAL_ROLE or (delivery is not None and delivery.phase in TRIAL_PHASES):
        # Every grant of the delivery is the provider team's until acceptance decides on it.
        raise HTTPException(
            409, "The provider stays during the trial; choose keep or remove when accepting the delivery"
        )
    if delivery is not None and row.status != "active":
        delivery = None
    # A live maintainer grant of a delivery is one member of the provider team:
    # the customer removes or re-scopes the whole team at once.
    team = (await db.scalars(team_grants(delivery.id, ws, MAINTAINER_ROLE))).all() if delivery else [row]
    if action == "revoke" and delivery is not None:
        from packages.core.services.client_trial_access import revoke_provider_team

        await revoke_provider_team(db, ws, delivery, user)
        audit(db, ws, user, "External collaboration revoke", {"collaboration_id": row.id})
        return public_grant(row)
    if action == "revoke":
        row.status = "revoked"
        row.snapshot = {}
    elif (
        action in {"authorize", "update"}
        and row.status in ({"requested"} if action == "authorize" else {"invited", "active"})
        and not grant_expired(row)
    ):
        if req.email.casefold() != row.email or req.role != row.role:
            raise HTTPException(422, "Approve the exact requested identity and role")
        if req.days is None and row.expires_at is not None:
            raise HTTPException(422, "Only an authorization without an expiry can stay without one")
        if action == "authorize":
            row.status = "invited"
            if row.delivery_id:
                await reauthorize_provider(db, ws, row)
        fingerprint = await issuer_fingerprint(db, ws, user.id)
        snapshot = await share_snapshot(db, ws) if req.share_configuration else {}
        expires_at = None if req.days is None else now() + timedelta(days=req.days)
        for grant in team if action == "update" else [row]:
            grant.authorized_by = user.id
            grant.authority_fingerprint = fingerprint
            grant.share_configuration = req.share_configuration
            grant.snapshot = snapshot
            grant.expires_at = expires_at
    else:
        raise HTTPException(409, "This authorization can no longer be changed; issue a new invitation")
    # Status and expiry both shape the derived seat.
    for grant in team if action == "update" else [row]:
        await sync_external_access(db, grant)
    audit(db, ws, user, f"External collaboration {action}", {"collaboration_id": row.id})
    return public_grant(row)


async def portal(db, user, grant_id):
    ws, row = await guest_grant(db, user, grant_id, active=False)
    return {
        "grant": public_grant(row),
        "title": "Workspace collaboration" if row.status == "requested" else ws.name,
        "configuration": row.snapshot if row.status == "active" and row.share_configuration else None,
        "communication": await communication(db, ws, user)
        if row.status == "active"
        else {"linked": False, "group": None},
    }


async def accept(db, user, grant_id):
    # Lock the guest exclusively from the start: holding SHARE and upgrading in
    # sync_external_access deadlocks two concurrent accepts by the same person.
    ws, row = await guest_grant(db, user, grant_id, active=False, for_update=True)
    if row.status == "requested":
        raise HTTPException(403, "Wait for the customer to authorize this request")
    if row.status == "invited":
        if row.delivery_id and await db.scalar(
            select(WorkspaceCollaboration.id).where(
                WorkspaceCollaboration.delivery_id == row.delivery_id,
                WorkspaceCollaboration.user_id == user.id,
                WorkspaceCollaboration.status == "active",
                WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
            )
        ):
            # One live access grant per person and delivery (a database invariant).
            raise HTTPException(409, "You already work in this customer Workspace")
        delivery = (
            await target_delivery(db, ws, delivery_id=row.delivery_id, lock=True)
            if row.delivery_id and row.role == MAINTAINER_ROLE
            else None
        )
        if delivery is not None and delivery.phase in TRIAL_PHASES:
            # During the trial the provider team opens the Workspace from its own company.
            raise HTTPException(409, "The provider team already works in this Workspace during the trial")
        if delivery is not None and (delivery.acceptance or {}).get("provider") in REMOVED_PROVIDER:
            # Only an authorization the customer gives after removing the team counts.
            raise HTTPException(409, "The customer removed the provider team; ask them to authorize you again")
        row.status = "active"
        row.user_id = user.id
        row.accepted_at = now()
        await sync_external_access(db, row)
        if delivery is not None and delivery.phase == "delivered":
            # The customer re-authorized the provider: the whole team may open it again.
            delivery.acceptance = {**(delivery.acceptance or {}), "provider": "keep", "restored_at": now().isoformat()}
        audit(db, ws, user, "External collaborator accepted", {"collaboration_id": row.id})
    return public_grant(row)


async def phase(db, user, workspace_id, action, note, provider=None):
    ws = await managed_workspace(db, user, workspace_id)
    delivery = await target_delivery(db, ws, lock=True)
    if delivery is None:
        raise HTTPException(404, "Delivery not found")
    if action == "request_acceptance" and delivery.phase == "trial":
        delivery.phase = "awaiting_acceptance"
    elif action == "accept" and delivery.phase in TRIAL_PHASES:
        # Workspace, then delivery, then every grant of the team, then the
        # derived rows in sync_external_access. The decision is the team's.
        team = (await db.scalars(team_grants(delivery.id, ws))).all()
        if not team:
            # A delivery from before trial access has no provider to decide on.
            provider = None
        elif provider not in {"keep", "remove"}:
            raise HTTPException(422, "Choose whether to keep the provider as maintainer or remove them")
        elif provider == "keep":
            # Every live grant of the team becomes a maintainer, checked against
            # the accepting manager's authority.
            fingerprint = await issuer_fingerprint(db, ws, user.id)
            for grant in team:
                if not await grantee_manages_source(db, grant):
                    # A teammate who no longer manages the source Workspace is not kept.
                    grant.status = "revoked"
                    grant.snapshot = {}
                    await sync_external_access(db, grant)
                    continue
                grant.role = MAINTAINER_ROLE
                grant.authorized_by = user.id
                grant.authority_fingerprint = fingerprint
                grant.expires_at = None
                await sync_external_access(db, grant)
        else:
            from packages.core.services.client_trial_access import revoke_provider_team

            # Every grant of the team, pending ones included, so none can restore it later.
            await revoke_provider_team(db, ws, delivery, user)
        delivery.phase = "delivered"
        delivery.acceptance = {
            "confirmed_by": user.id,
            "confirmed_at": now().isoformat(),
            "note": note,
            "operation_revision": ws.operation_revision,
            "provider": provider,
            **({"removed_at": now().isoformat(), "removed_by": user.id} if provider == "remove" else {}),
        }
    elif action == "accept" and delivery.phase == "delivered":
        return {"phase": delivery.phase, "acceptance": delivery.acceptance}
    else:
        raise HTTPException(409, "Delivery is not in a reviewable phase")
    details = {"phase": delivery.phase, "note": note}
    if action == "accept":
        details["provider"] = provider
    audit(db, ws, user, "Customer delivery phase changed", details)
    return {"phase": delivery.phase, "acceptance": delivery.acceptance}


async def issuer_fingerprint(db, ws, user_id):
    import hashlib
    import json

    membership = (
        await db.execute(
            select(
                UserMembership.id,
                UserMembership.role,
                UserMembership.status,
                UserMembership.updated_at,
                UserMembership.deleted_at,
            ).where(UserMembership.user_id == user_id, UserMembership.entity_id == ws.entity_id)
        )
    ).one_or_none()
    workspace_member = (
        await db.execute(
            select(
                WorkspaceStaff.id,
                WorkspaceStaff.role,
                WorkspaceStaff.status,
                WorkspaceStaff.expires_at,
                WorkspaceStaff.updated_at,
            ).where(WorkspaceStaff.workspace_id == ws.id, WorkspaceStaff.user_id == user_id)
        )
    ).one_or_none()
    return hashlib.sha256(
        json.dumps(
            [list(membership) if membership else None, list(workspace_member) if workspace_member else None],
            default=str,
        ).encode()
    ).hexdigest()


async def fresh_actor(db, user, *, verified=False, for_update=False):
    current = (
        await db.execute(
            select(
                User.email,
                User.verified_email,
                User.email_verified_at,
                User.status,
                User.deleted_at,
                User.token_version,
            )
            .where(User.id == user.id)
            .with_for_update(read=not for_update)
        )
    ).one_or_none()
    membership = await db.scalar(
        select(UserMembership.id)
        .where(
            UserMembership.user_id == user.id,
            UserMembership.entity_id == user.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
        .with_for_update(read=True)
    )
    if (
        current is None
        or current.status != "active"
        or current.deleted_at is not None
        or not membership
        or current.email != user.email
        or current.token_version != user.token_version
    ):
        raise HTTPException(401, "Your current sign-in or company membership has changed")
    if verified and (current.verified_email != current.email or current.email_verified_at is None):
        raise HTTPException(403, "Verify your current email to use this collaboration")
    return current
