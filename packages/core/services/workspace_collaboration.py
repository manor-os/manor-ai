"""Bounded external portal. Guests never acquire a customer company actor."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.user import Entity, User
from packages.core.models.chat_room import ChatRoom, ChatRoomMember
from packages.core.models.workspace import WorkspaceActivity, WorkspaceStaff
from packages.core.models.workspace_collaboration import WorkspaceCollaboration
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    lock_workspace_recipient_authorization,
    user_can_manage_workspace,
)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=255, pattern="^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")
    role: Literal["tester", "maintainer"] = "tester"
    days: int = Field(default=14, ge=1, le=90)
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


def now():
    return datetime.now(timezone.utc)


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
        "status": "expired" if row.status != "revoked" and row.expires_at <= now() else row.status,
        "path": f"/workspace-collaboration/{row.id}",
    }


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
    if not await user_can_manage_workspace(db, workspace_id=ws.id, user_id=user.id, entity_role=user.role):
        raise HTTPException(403, "A customer Workspace manager must approve this action")
    return ws


async def live_boundary(db, row):
    ws = await lock_workspace_access_boundary(db, workspace_id=row.workspace_id, entity_id=row.entity_id)
    entity = await db.scalar(select(Entity.id).where(Entity.id == row.entity_id, Entity.deleted_at.is_(None)))
    if ws is None or ws.deleted_at is not None or entity is None:
        raise HTTPException(410, "Workspace is no longer available")
    if row.status == "revoked" or row.expires_at <= now():
        raise HTTPException(410, "Collaboration expired or revoked")
    if row.authorized_by:
        await lock_workspace_recipient_authorization(
            db, workspace_id=ws.id, entity_id=ws.entity_id, user_id=row.authorized_by
        )
        # Resolve the issuing identity in the customer company; never use the guest's company role.
        # Ordinary same-company authorization below also fences revoked/deleted issuers.
        issuer = await db.scalar(
            select(User).where(User.id == row.authorized_by, User.status == "active", User.deleted_at.is_(None))
        )
        from packages.core.models.user import UserMembership

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


async def guest_grant(db, user, grant_id, *, active=True):
    # Workspace-first lock order is shared by invite, revoke and portal access.
    stub = await db.get(WorkspaceCollaboration, grant_id)
    if stub is None or user.email.casefold() != stub.email:
        raise HTTPException(404, "Collaboration not found")
    ws = await live_boundary(db, stub)
    # Freeze the guest identity before the child grant, matching user deletion's
    # User -> grant order. This prevents a stale proof and a revoke/read deadlock.
    current = await fresh_actor(db, user, verified=True)
    row = await db.scalar(
        select(WorkspaceCollaboration)
        .where(WorkspaceCollaboration.id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.email != current.email.casefold():
        raise HTTPException(404, "Collaboration not found")
    if row.status == "revoked" or row.expires_at <= now():
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
    duplicate = await db.scalar(
        select(WorkspaceCollaboration.id)
        .where(
            WorkspaceCollaboration.workspace_id == ws.id,
            WorkspaceCollaboration.email == req.email.casefold(),
            WorkspaceCollaboration.status.in_(["requested", "invited", "active"]),
            WorkspaceCollaboration.expires_at > now(),
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
    await fresh_actor(db, user, verified=True)
    ws = await lock_workspace_access_boundary(db, workspace_id=row.target_workspace_id, entity_id=row.target_entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(410, "Customer Workspace is no longer available")
    existing = await db.scalar(
        select(WorkspaceCollaboration)
        .where(
            WorkspaceCollaboration.delivery_id == row.id,
            WorkspaceCollaboration.email == user.email.casefold(),
            WorkspaceCollaboration.status.in_(["requested", "invited", "active"]),
            WorkspaceCollaboration.expires_at > now(),
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
    delivery = await db.scalar(
        select(ClientWorkspaceDelivery).where(
            ClientWorkspaceDelivery.target_workspace_id == ws.id,
            ClientWorkspaceDelivery.target_entity_id == ws.entity_id,
        )
    )
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
    }


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
    if action == "revoke":
        row.status = "revoked"
        row.snapshot = {}
    elif (
        action in {"authorize", "update"}
        and row.status in ({"requested"} if action == "authorize" else {"invited", "active"})
        and row.expires_at > now()
    ):
        if req.email.casefold() != row.email or req.role != row.role:
            raise HTTPException(422, "Approve the exact requested identity and role")
        if action == "authorize":
            row.status = "invited"
        row.authorized_by = user.id
        row.authority_fingerprint = await issuer_fingerprint(db, ws, user.id)
        row.share_configuration = req.share_configuration
        row.snapshot = await share_snapshot(db, ws) if req.share_configuration else {}
        row.expires_at = now() + timedelta(days=req.days)
    else:
        raise HTTPException(409, "This authorization can no longer be changed; issue a new invitation")
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
    ws, row = await guest_grant(db, user, grant_id, active=False)
    if row.status == "requested":
        raise HTTPException(403, "Wait for the customer to authorize this request")
    if row.status == "invited":
        row.status = "active"
        row.user_id = user.id
        row.accepted_at = now()
        audit(db, ws, user, "External collaborator accepted", {"collaboration_id": row.id})
    return public_grant(row)


async def phase(db, user, workspace_id, action, note):
    ws = await managed_workspace(db, user, workspace_id)
    delivery = await db.scalar(
        select(ClientWorkspaceDelivery)
        .where(
            ClientWorkspaceDelivery.target_workspace_id == ws.id,
            ClientWorkspaceDelivery.target_entity_id == ws.entity_id,
        )
        .with_for_update()
    )
    if delivery is None:
        raise HTTPException(404, "Delivery not found")
    if action == "request_acceptance" and delivery.phase == "trial":
        delivery.phase = "awaiting_acceptance"
    elif action == "accept" and delivery.phase in {"trial", "awaiting_acceptance"}:
        delivery.phase = "delivered"
        delivery.acceptance = {
            "confirmed_by": user.id,
            "confirmed_at": now().isoformat(),
            "note": note,
            "operation_revision": ws.operation_revision,
        }
    elif action == "accept" and delivery.phase == "delivered":
        return {"phase": delivery.phase, "acceptance": delivery.acceptance}
    else:
        raise HTTPException(409, "Delivery is not in a reviewable phase")
    audit(db, ws, user, "Customer delivery phase changed", {"phase": delivery.phase, "note": note})
    return {"phase": delivery.phase, "acceptance": delivery.acceptance}


async def issuer_fingerprint(db, ws, user_id):
    import hashlib
    import json
    from packages.core.models.user import UserMembership

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


async def fresh_actor(db, user, *, verified=False):
    from packages.core.models.user import UserMembership

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
            .with_for_update(read=True)
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
