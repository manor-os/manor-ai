"""Private, email-bound delivery through the canonical Blueprint installer."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import os
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, update

from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.client_delivery import ClientContact, ClientWorkspaceDelivery
from packages.core.models.people import Client
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.permissions import Permission, check_effective_permission
from packages.core.services.user_email_proof import email_is_verified
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    lock_workspace_recipient_authorization,
    user_can_manage_workspace,
)


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def delivery_status(row):
    if row.status == "pending" and row.expires_at <= datetime.now(timezone.utc):
        return "expired"
    return row.status


async def client_record(db, entity_id, client_id, *, lock=False, allow_inactive=False):
    q = select(Client).where(Client.id == client_id, Client.entity_id == entity_id, Client.deleted_at.is_(None))
    if lock:
        q = q.with_for_update()
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None or (row.status != "active" and not allow_inactive):
        raise HTTPException(404, "Client not found")
    return row


async def require_source(db, row):
    # Revoke implicitly when the company/client/source or issuing authority goes away.
    entity = (
        await db.execute(
            select(Entity.id).where(Entity.id == row.entity_id, Entity.deleted_at.is_(None)).with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(410, "The source company is no longer available")
    client = await client_record(db, row.entity_id, row.client_id, lock=True)
    contact = await db.scalar(
        select(ClientContact)
        .where(
            ClientContact.id == row.contact_id,
            ClientContact.entity_id == row.entity_id,
            ClientContact.client_id == row.client_id,
        )
        .with_for_update()
    )
    if (
        contact is None
        or contact.deleted_at is not None
        or contact.email != row.recipient_email
        or contact.invite_version != row.contact_version
        or (client.meta or {}).get("crm_invite_version", 0) != row.client_version
    ):
        raise HTTPException(410, "The client or contact changed; request a new invitation")
    ws = await lock_workspace_access_boundary(db, workspace_id=row.source_workspace_id, entity_id=row.entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(410, "The source Workspace is no longer available")
    await lock_workspace_recipient_authorization(
        db, workspace_id=ws.id, entity_id=row.entity_id, user_id=row.created_by
    )
    member_role = (
        await db.execute(
            select(UserMembership.role).where(
                UserMembership.user_id == row.created_by,
                UserMembership.entity_id == row.entity_id,
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    author = (await db.execute(select(User.status, User.deleted_at).where(User.id == row.created_by))).one_or_none()
    if not member_role or not author or author.status != "active" or author.deleted_at is not None:
        raise HTTPException(410, "The invitation is no longer authorized")
    if not await user_can_manage_workspace(db, workspace_id=ws.id, user_id=row.created_by, entity_role=member_role):
        raise HTTPException(410, "The invitation is no longer authorized")
    return ws


async def can_manage_delivery(db, user, row):
    return row.entity_id == user.entity_id and await user_can_manage_workspace(
        db, workspace_id=row.source_workspace_id, user_id=user.id, entity_role=user.role
    )


async def require_delivery_management(db, user, row):
    ws = await lock_workspace_access_boundary(db, workspace_id=row.source_workspace_id, entity_id=user.entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    await lock_workspace_recipient_authorization(db, workspace_id=ws.id, entity_id=user.entity_id, user_id=user.id)
    if not await can_manage_delivery(db, user, row):
        raise HTTPException(403, "Only a Workspace manager can change its invitation")


async def create_delivery(
    db, *, user, client_id, contact_id, workspace_id, phase="trial", knowledge_document_ids=(),
):
    if phase not in {"trial", "delivered"}:
        raise HTTPException(422, "Choose trial or direct delivery")
    client = await client_record(db, user.entity_id, client_id, lock=True)
    contact = (
        await db.execute(
            select(ClientContact)
            .where(
                ClientContact.id == contact_id,
                ClientContact.deleted_at.is_(None),
                ClientContact.entity_id == user.entity_id,
                ClientContact.client_id == client_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(404, "Contact not found")
    ws = await lock_workspace_access_boundary(db, workspace_id=workspace_id, entity_id=user.entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    if not await user_can_manage_workspace(db, workspace_id=ws.id, user_id=user.id, entity_role=user.role):
        raise HTTPException(403, "Only the Workspace owner can prepare its delivery")
    # Content is opt-in by exact document identity; never infer selection from
    # workflow dependencies or from everything currently stored in Knowledge.
    selected_document_ids = frozenset(knowledge_document_ids)
    payload = await export_workspace(
        db,
        ws.id,
        title=ws.name,
        context=ExportContext(
            knowledge_pack_mode="inline_text" if selected_document_ids else "skeleton",
            knowledge_document_ids=selected_document_ids,
        ),
    )
    # Pin the configuration. No Marketplace record, public share token, or live grants.
    token = secrets.token_urlsafe(32)
    row = ClientWorkspaceDelivery(
        entity_id=user.entity_id,
        client_id=client_id,
        contact_id=contact_id,
        contact_version=contact.invite_version,
        client_version=(client.meta or {}).get("crm_invite_version", 0),
        source_workspace_id=ws.id,
        created_by=user.id,
        recipient_email=contact.email,
        token_hash=token_digest(token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="pending",
        phase=phase,
        acceptance={},
        email_delivery={"status": "pending"},
        payload=payload,
    )
    db.add(row)
    await db.flush()
    return row, token


async def prepare_delivery(db, **kwargs):
    row, token = await create_delivery(db, **kwargs)
    # SMTP must never expose an invitation that can still be rolled back.
    await db.commit()
    return await send_delivery_invitation(db, row.id, token)


async def send_delivery_invitation(db, delivery_id, token):
    from packages.core.services import email_service

    row = await db.scalar(select(ClientWorkspaceDelivery).where(
        ClientWorkspaceDelivery.id == delivery_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None or row.token_hash != token_digest(token) or delivery_status(row) != "pending":
        raise HTTPException(409, "The invitation has changed; refresh the delivery record")
    # Serialize with contact changes, revocation and source deletion while sending.
    await require_source(db, row)
    # Read the customer's current account language on every send/resend.
    # Contact settings and the sender's UI language never override it.
    locale = await db.scalar(select(User.locale).where(
        func.lower(User.email) == row.recipient_email.casefold(),
        User.status == "active", User.deleted_at.is_(None),
    ))
    locale = email_service.normalize_announcement_email_locale(locale)
    attempted_at = datetime.now(timezone.utc).isoformat()
    result = {"status": "failed", "attempted_at": attempted_at, "locale": locale}
    try:
        enabled = email_service.is_email_delivery_enabled()
    except (ValueError, TypeError):
        enabled = False
        result["status"] = "unconfigured"
    if not enabled:
        if result["status"] != "unconfigured":
            result["status"] = "disabled"
    else:
        app_url = os.getenv("APP_URL", "").strip().rstrip("/")
        try:
            url = urlsplit(app_url)
            valid_url = url.scheme in {"http", "https"} and bool(url.netloc) and not (url.username or url.password or url.query or url.fragment)
        except ValueError:
            valid_url = False
        if not valid_url:
            result["status"] = "unconfigured"
        else:
            entity = await db.get(Entity, row.entity_id)
            try:
                sent = await email_service.send_client_delivery_email(
                    row.recipient_email, entity_name=entity.name,
                    workspace_name=row.payload["manifest"]["title"], phase=row.phase,
                    invite_url=f"{app_url}/client-delivery/{token}", locale=locale,
                )
            except Exception:
                # Do not return SMTP credentials, provider errors or the token.
                sent = False
            if sent:
                result = {"status": "sent", "sent_at": datetime.now(timezone.utc).isoformat(), "attempted_at": attempted_at, "locale": locale}
    row.email_delivery = result
    await db.commit()
    return {**receipt(row, can_manage=True), "invite_path": f"/client-delivery/{token}"}


async def recipient_delivery(db, token, user, *, lock=False):
    q = select(ClientWorkspaceDelivery).where(ClientWorkspaceDelivery.token_hash == token_digest(token))
    if lock:
        q = q.with_for_update()
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None or user.email.casefold() != row.recipient_email.casefold():
        raise HTTPException(404, "Invitation not found")
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
    if (
        current is None
        or current.email != user.email
        or current.status != "active"
        or current.deleted_at is not None
        or current.token_version != user.token_version
    ):
        raise HTTPException(401, "Your sign-in has changed")
    if not email_is_verified(user) or current.verified_email != current.email or current.email_verified_at is None:
        raise HTTPException(403, "Verify your current email before accepting this invitation")
    if row.status in ("delivered", "needs_setup"):
        if row.accepted_by != user.id:
            raise HTTPException(404, "Invitation not found")
        return row
    if delivery_status(row) != "pending":
        raise HTTPException(410, "Invitation expired or revoked")
    await require_source(db, row)
    return row


async def accept_delivery(db, *, token, user, variable_values=None, channel_config_ids=None):
    row = await recipient_delivery(db, token, user, lock=True)
    if row.target_workspace_id:
        if user.entity_id != row.target_entity_id:
            raise HTTPException(409, "Switch to the company where you accepted this Workspace")
        return row
    if user.entity_id == row.entity_id:
        raise HTTPException(409, "Choose the customer's own company before accepting")
    membership = (
        await db.execute(
            select(UserMembership)
            .where(
                UserMembership.user_id == user.id,
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if membership is None or membership.role not in ("owner", "admin"):
        raise HTTPException(403, "A company owner or admin must accept the delivery")
    await check_effective_permission(db, user.id, user.entity_id, user.role, Permission.WORKSPACES_CREATE)
    entity = await db.get(Entity, user.entity_id)
    if entity is None or entity.deleted_at is not None:
        raise HTTPException(404, "Company not found")
    result = await install_blueprint(
        db,
        entity_id=user.entity_id,
        payload=deepcopy(row.payload),
        mode=InstallMode.LIVE,
        user_id=user.id,
        create_missing_agents=True,
        variable_values=variable_values or {},
        channel_config_ids=channel_config_ids or {},
    )
    ws = await db.get(Workspace, result.workspace_id)
    # Paused before the enclosing transaction commits: no job can run on delivery.
    ws.status = "paused"
    ws.settings = {**ws.settings, "client_delivery": {"delivery_id": row.id, "provider_access": "none"}}
    row.target_entity_id = user.entity_id
    row.target_workspace_id = ws.id
    row.accepted_by = user.id
    row.accepted_at = datetime.now(timezone.utc)
    row.status = "needs_setup" if any(t.blocking for t in result.todos) else "delivered"
    contact = await db.get(ClientContact, row.contact_id)
    if contact:
        contact.user_id = user.id
    await db.flush()
    return row


class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255, pattern="^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")
    phone: str = Field(default="", max_length=50)
    job_title: str = Field(default="", max_length=255)
    is_primary: bool = False


def receipt(row, *, can_manage=False, status=None):
    return {
        "id": row.id,
        "client_id": row.client_id,
        "contact_id": row.contact_id,
        "source_workspace_id": row.source_workspace_id,
        "title": row.payload["manifest"]["title"],
        "recipient_email": row.recipient_email,
        "status": status or delivery_status(row),
        "expires_at": row.expires_at,
        "target_workspace_id": row.target_workspace_id,
        "target_entity_id": row.target_entity_id,
        "accepted_at": row.accepted_at,
        "provider_access": "none",
        "phase": row.phase,
        "acceptance": row.acceptance or {},
        "email_delivery": row.email_delivery or {},
        "can_manage": can_manage,
    }


def contact_json(row):
    return {key: getattr(row, key) for key in ("id", "name", "email", "user_id", "phone", "job_title", "is_primary")}


async def contacts(client_id: str, user: User = None, db: AsyncSession = None):
    await client_record(db, user.entity_id, client_id, allow_inactive=True)
    rows = (
        await db.scalars(
            select(ClientContact)
            .where(
                ClientContact.entity_id == user.entity_id,
                ClientContact.client_id == client_id,
                ClientContact.deleted_at.is_(None),
            )
            .order_by(ClientContact.is_primary.desc(), ClientContact.created_at)
        )
    ).all()
    return [contact_json(r) for r in rows]


async def save_contact(db, user, client_id, req, contact_id=None):
    await client_record(db, user.entity_id, client_id, lock=True)
    email = req.email.casefold()
    duplicate = await db.scalar(
        select(ClientContact).where(
            ClientContact.entity_id == user.entity_id,
            ClientContact.client_id == client_id,
            ClientContact.email == email,
        )
    )
    if contact_id:
        row = await db.scalar(
            select(ClientContact)
            .where(
                ClientContact.id == contact_id,
                ClientContact.entity_id == user.entity_id,
                ClientContact.client_id == client_id,
                ClientContact.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(404, "Contact not found")
        if duplicate and duplicate.id != row.id:
            raise HTTPException(409, "A contact already uses this email")
        if row.email != email:
            row.invite_version += 1
            row.user_id = None
    else:
        row = duplicate
        if row is None:
            row = ClientContact(entity_id=user.entity_id, client_id=client_id)
            db.add(row)
        row.deleted_at = None
    if req.is_primary:
        with db.no_autoflush:
            await db.execute(
                update(ClientContact)
                .where(ClientContact.entity_id == user.entity_id, ClientContact.client_id == client_id)
                .values(is_primary=False)
            )
    for key, value in req.model_dump().items():
        setattr(row, key, email if key == "email" else value)
    await db.flush()
    return contact_json(row)


async def remove_contact(client_id: str, contact_id: str, user: User = None, db: AsyncSession = None):
    await client_record(db, user.entity_id, client_id, lock=True)
    row = await db.scalar(
        select(ClientContact)
        .where(
            ClientContact.id == contact_id,
            ClientContact.entity_id == user.entity_id,
            ClientContact.client_id == client_id,
            ClientContact.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Contact not found")
    row.deleted_at = datetime.now(timezone.utc)
    row.invite_version += 1
    await db.flush()


async def deliveries(client_id: str, user: User = None, db: AsyncSession = None):
    client = await client_record(db, user.entity_id, client_id, allow_inactive=True)
    rows = (
        await db.scalars(
            select(ClientWorkspaceDelivery)
            .where(ClientWorkspaceDelivery.entity_id == user.entity_id, ClientWorkspaceDelivery.client_id == client_id)
            .order_by(ClientWorkspaceDelivery.created_at.desc())
        )
    ).all()
    contacts = {
        r.id: r
        for r in (
            await db.scalars(
                select(ClientContact).where(
                    ClientContact.entity_id == user.entity_id, ClientContact.client_id == client_id
                )
            )
        ).all()
    }
    from packages.core.models.workspace_collaboration import WorkspaceCollaboration
    from packages.core.services.workspace_collaboration import public_grant
    maintenance_rows = (await db.scalars(select(WorkspaceCollaboration).where(
        WorkspaceCollaboration.delivery_id.in_([row.id for row in rows]), WorkspaceCollaboration.requested_by == user.id,
    ).order_by(WorkspaceCollaboration.created_at))).all() if rows else []
    maintenance = {grant.delivery_id: public_grant(grant) for grant in maintenance_rows}
    result = []
    for row in rows:
        contact = contacts.get(row.contact_id)
        changed = (
            client.status != "active"
            or (client.meta or {}).get("crm_invite_version", 0) != row.client_version
            or contact is None
            or (contact.deleted_at is not None)
            or (contact.invite_version != row.contact_version)
            or (contact.email != row.recipient_email)
        )
        result.append(
            {**receipt(
                row,
                can_manage=client.status == "active" and await can_manage_delivery(db, user, row),
                status="revoked" if row.status == "pending" and changed else None,
            ), "maintenance": maintenance.get(row.id)}
        )
    return result


async def change_invite(client_id: str, delivery_id: str, action: str, user: User = None, db: AsyncSession = None):
    row = (
        await db.execute(
            select(ClientWorkspaceDelivery)
            .where(
                ClientWorkspaceDelivery.id == delivery_id,
                ClientWorkspaceDelivery.entity_id == user.entity_id,
                ClientWorkspaceDelivery.client_id == client_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Delivery not found")
    await client_record(db, user.entity_id, client_id, lock=True)
    await require_delivery_management(db, user, row)
    if row.status != "pending":
        raise HTTPException(409, "Only a pending invitation can be changed")
    if action == "revoke":
        row.status = "revoked"
        return receipt(row, can_manage=True)
    if action != "rotate":
        raise HTTPException(404, "Action not found")
    await require_source(db, row)
    token = secrets.token_urlsafe(32)
    row.token_hash = token_digest(token)
    row.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    row.email_delivery = {"status": "pending"}
    await db.commit()
    return await send_delivery_invitation(db, row.id, token)
