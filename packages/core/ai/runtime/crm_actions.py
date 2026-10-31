"""Actor-scoped Manor actions over the same CRM services as the web UI."""

from types import SimpleNamespace
import json

from fastapi import HTTPException
from pydantic import Field, ValidationError
from sqlalchemy import select

from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.people import Client
from packages.core.services import crm, client_delivery
from packages.core.services.external_collaboration_access import COLLABORATOR_MEMBERSHIP_ROLE
from packages.core.services.people_service import delete_client
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_is_external_customer

CRM_ACTIONS = {
    "list_clients": "Search company CRM clients. params: search?, lifecycle (lead/prospect/customer/partner)?, owner_id?, status (active/inactive)?, tag?, limit (1-100)?, offset?.",
    "get_client": "Read the current company CRM profile. params: client_id. Returns /team/clients link and authoritative CRM data.",
    "create_client": "Create a company CRM client, never Staff. params: name, email?, phone?, address?, industry?, website?, source?, company_size?, lifecycle?, owner_id?, tags?, notes?.",
    "update_client": "Patch a company CRM profile without clearing omitted fields. params: client_id plus profile fields; status=inactive archives, active restores. Old invitations remain invalid after restore.",
    "delete_client": "Soft-delete a company CRM client. params: client_id. Historical records remain; pending delivery is invalidated.",
    "get_crm_overview": "Read company CRM counts, sales forecast separated by currency, overdue follow-ups. params: {}.",
    "list_client_contacts": "List all current contacts of a company, including primary contact. params: client_id.",
    "create_client_contact": "Add a contact to a company, never Staff. params: client_id, name, email, phone?, job_title?, is_primary?. Existing email is updated, not duplicated.",
    "update_client_contact": "Patch a contact. params: client_id, contact_id, name?, email?, phone?, job_title?, is_primary?. Changing email invalidates old delivery links.",
    "delete_client_contact": "Remove a company contact and invalidate pending delivery. params: client_id, contact_id.",
    "list_client_activities": "Read CRM communication and immutable sales stage history. params: client_id, limit?, offset?.",
    "create_client_activity": "Record a note/call/email/meeting in CRM; does not send a message. params: client_id, subject, kind?, body?, occurred_at? (ISO date-time with timezone).",
    "update_client_activity": "Patch a manual communication record; automatic sales stage history is immutable. params: client_id, record_id, subject?, kind?, body?, occurred_at?.",
    "delete_client_activity": "Soft-delete a manual communication record. params: client_id, record_id. Sales stage history cannot be removed.",
    "list_client_opportunities": "Read a company's sales opportunities. params: client_id, limit?, offset?.",
    "create_client_opportunity": "Create a CRM deal. params: client_id, title, stage? (qualified/discovery/proposal/negotiation/won/lost), amount? (exact decimal string), currency? (3 uppercase letters), probability? (0-100), expected_close? (YYYY-MM-DD), owner_id?, description?, loss_reason?.",
    "update_client_opportunity": "Patch a CRM deal and append immutable stage history on stage change. params: client_id, record_id plus deal fields. Won/lost force probability to 100/0.",
    "delete_client_opportunity": "Soft-delete a deal. params: client_id, record_id.",
    "list_client_followups": "Read human CRM follow-ups, not execution Tasks or scheduled reminders. params: client_id, limit?, offset?.",
    "create_client_followup": "Create a human CRM follow-up. params: client_id, title, due_at (ISO date-time with timezone), body?, owner_id?, priority? (low/normal/high). Does not send notifications.",
    "update_client_followup": "Patch a CRM follow-up. params: client_id, record_id, title?, due_at?, body?, owner_id?, priority?, status? (open/done). Complete with done, reopen with open; omitted fields stay intact.",
    "delete_client_followup": "Soft-delete a follow-up. params: client_id, record_id.",
    "list_crm_opportunities": "Search the company-wide sales pipeline. params: stage?, owner_id?, search?, limit?, offset?.",
    "list_crm_followups": "Read company follow-up queue. params: status? (open/done), owner_id?, overdue? (boolean), limit?, offset?.",
    "list_client_deliveries": "Read private Workspace delivery receipts. params: client_id. Includes invitation email delivery status; no public Marketplace listing.",
    "prepare_client_delivery": "Prepare an email-bound private Workspace configuration invitation. params: client_id, contact_id, workspace_id. Requires source Workspace management and normal approval; saves the invitation then emails the stored contact, returning email_delivery status and invite_path. No customer acceptance, Staff creation or provider access.",
    "rotate_client_delivery": "Resend a pending private invitation by rotating its URL and emailing the stored contact; invalidates the previous URL. params: client_id, delivery_id. Requires source Workspace management and normal approval.",
    "revoke_client_delivery": "Revoke a pending private invitation. params: client_id, delivery_id. Does not remove an already delivered Workspace.",
    "link_client_relationship": "Explicitly link an existing Relationship Ledger subject in the CURRENT runtime Workspace to an existing company CRM client. params: client_id, identity_key. Requires Workspace write access and an installed ledger. Does not merge or copy history; ledger reads return fresh CRM data.",
    "unlink_client_relationship": "Remove a CRM association for a Relationship Ledger subject in the CURRENT Workspace. params: client_id, identity_key. Neither CRM nor ledger history is deleted.",
    "list_client_relationships": "Read a client's associated subjects only in the CURRENT authorized Workspace ledger. params: client_id. Company CRM is authoritative; Workspace event history stays scoped.",
}


class Page(crm.Request):
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Clients(Page):
    search: str = ""
    lifecycle: crm.Lifecycle | None = None
    owner_id: str = ""
    status: str = Field(default="active", pattern="^(active|inactive)$")
    tag: str = ""


class Pipeline(Page):
    stage: crm.Stage | None = None
    owner_id: str = ""
    search: str = ""


class Queue(Page):
    status: str = Field(default="open", pattern="^(open|done)$")
    owner_id: str = ""
    overdue: bool = False


async def crm_actor(db, *, entity_id, user_id):
    """Resolve current company membership without persisting a User overlay."""
    if not user_id:
        raise HTTPException(401, "An authenticated company user is required")
    user = await db.scalar(select(User).where(User.id == user_id, User.status == "active", User.deleted_at.is_(None)))
    entity = await db.scalar(select(Entity.id).where(Entity.id == entity_id, Entity.deleted_at.is_(None)))
    membership = await db.scalar(
        select(UserMembership).where(UserMembership.user_id == user_id, UserMembership.entity_id == entity_id)
    )
    if (
        user is None
        or not entity
        or (membership is not None and (membership.status != "active" or membership.deleted_at is not None))
    ):
        raise HTTPException(403, "Company access is no longer active")
    if membership is None and user.entity_id != entity_id:
        raise HTTPException(403, "Company membership is required")
    if membership is not None and membership.role == COLLABORATOR_MEMBERSHIP_ROLE:
        # An external collaborator works inside one Workspace; the company CRM
        # is outside it whatever the runtime gate admitted.
        raise HTTPException(403, "Company CRM is not available to external collaborators")
    return SimpleNamespace(
        id=user.id, entity_id=entity_id, role=membership.role if membership is not None else user.role, email=user.email
    )


def required(params, key):
    value = params.pop(key, None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(422, f"{key} is required")
    return value.strip()


def no_extra(params):
    if params:
        raise HTTPException(422, "Unsupported parameters: " + ", ".join(sorted(params)))


async def execute_crm_action(db, *, action, params, entity_id, user_id, workspace_id=None, runtime_kwargs=None):
    if runtime_tool_call_context_is_external_customer(runtime_kwargs or {}):
        raise HTTPException(403, "Company CRM is unavailable on external customer surfaces")
    user = await crm_actor(db, entity_id=entity_id, user_id=user_id)
    values = dict(params or {})
    # Runtime may inject its current Workspace. It is not a caller-selected CRM scope.
    if action != "prepare_client_delivery":
        requested_workspace = values.pop("workspace_id", None)
        if requested_workspace and requested_workspace != workspace_id:
            raise HTTPException(422, "Use the current runtime Workspace for CRM relationships")
    if action == "list_clients":
        return await crm.clients(user=user, db=db, **Clients.model_validate(values).model_dump())
    if action == "get_crm_overview":
        no_extra(values)
        return await crm.overview(user=user, db=db)
    if action == "list_crm_opportunities":
        return await crm.pipeline(user=user, db=db, **Pipeline.model_validate(values).model_dump())
    if action == "list_crm_followups":
        return await crm.followup_queue(user=user, db=db, **Queue.model_validate(values).model_dump())
    if action == "create_client":
        metadata = values.pop("metadata", {})
        if not isinstance(metadata, dict) or not isinstance(metadata.get("crm", {}), dict) or set(metadata) - {"crm"}:
            raise HTTPException(422, "Use validated CRM profile fields")
        values = {**metadata.get("crm", {}), **values}
        return await crm.create(req=crm.Profile.model_validate(values), user=user, db=db)
    cid = required(values, "client_id")
    if action == "get_client":
        no_extra(values)
        return crm.client_json(await crm.customer(db, user, cid))
    if action == "update_client":
        row = await db.scalar(
            select(Client)
            .where(Client.id == cid, Client.entity_id == entity_id, Client.deleted_at.is_(None))
            .with_for_update()
        )
        if row is None:
            raise HTTPException(404, "Client not found")
        metadata = values.pop("metadata", {})
        if not isinstance(metadata, dict) or not isinstance(metadata.get("crm", {}), dict) or set(metadata) - {"crm"}:
            raise HTTPException(422, "Use validated CRM profile fields")
        current = {k: getattr(row, k) or "" for k in ("name", "email", "phone", "address", "status")}
        current.update((row.meta or {}).get("crm", {}))
        request = crm.Profile.model_validate({**current, **metadata.get("crm", {}), **values})
        return await crm.profile(client_id=cid, req=request, user=user, db=db)
    if action == "delete_client":
        no_extra(values)
        await crm.customer(db, user, cid)
        return {"deleted": await delete_client(db, cid, entity_id), "client_id": cid}
    if action == "list_client_contacts":
        no_extra(values)
        return await client_delivery.contacts(cid, user=user, db=db)
    if action in {"create_client_contact", "update_client_contact"}:
        contact_id = required(values, "contact_id") if action == "update_client_contact" else None
        current = {}
        if contact_id:
            await crm.customer(db, user, cid, write=True)
            records = await client_delivery.contacts(cid, user=user, db=db)
            current = next((r for r in records if r["id"] == contact_id), None)
            if current is None:
                raise HTTPException(404, "Contact not found")
            current = {
                k: current[k] if current[k] is not None else "" for k in client_delivery.ContactRequest.model_fields
            }
        req = client_delivery.ContactRequest.model_validate({**current, **values})
        return await client_delivery.save_contact(db, user, cid, req, contact_id)
    if action == "delete_client_contact":
        contact_id = required(values, "contact_id")
        no_extra(values)
        await client_delivery.remove_contact(cid, contact_id, user=user, db=db)
        return {"deleted": True, "contact_id": contact_id}
    if action == "list_client_deliveries":
        no_extra(values)
        return await client_delivery.deliveries(cid, user=user, db=db)
    if action == "prepare_client_delivery":
        contact_id = required(values, "contact_id")
        source = required(values, "workspace_id")
        no_extra(values)
        return await client_delivery.prepare_delivery(
            db, user=user, client_id=cid, contact_id=contact_id, workspace_id=source
        )
    if action in {"rotate_client_delivery", "revoke_client_delivery"}:
        delivery_id = required(values, "delivery_id")
        no_extra(values)
        return await client_delivery.change_invite(
            cid, delivery_id, "rotate" if action.startswith("rotate") else "revoke", user=user, db=db
        )
    if action in {"link_client_relationship", "unlink_client_relationship", "list_client_relationships"}:
        from packages.core.services.crm_relationships import client_relationships, link_relationship

        if action == "list_client_relationships":
            no_extra(values)
            return await client_relationships(db, user, cid, workspace_id)
        identity = required(values, "identity_key")
        no_extra(values)
        return await link_relationship(db, user, cid, workspace_id, identity, unlink=action.startswith("unlink"))
    for name, schema in (("activity", crm.Activity), ("opportunity", crm.Opportunity), ("followup", crm.Followup)):
        plural = {"activity": "activities", "opportunity": "opportunities", "followup": "followups"}[name]
        if action == f"list_client_{plural}":
            return await crm.rows(db, user, cid, plural, **Page.model_validate(values).model_dump())
        if action in {f"create_client_{name}", f"update_client_{name}", f"delete_client_{name}"}:
            rid = required(values, "record_id") if not action.startswith("create") else None
            if action.startswith("delete"):
                no_extra(values)
                await crm.remove(db, user, cid, plural, rid)
                return {"deleted": True, "record_id": rid}
            current = {}
            if rid:
                await crm.customer(db, user, cid, write=True)
                model = crm.MODELS[plural]
                row = await db.scalar(
                    select(model).where(
                        model.id == rid,
                        model.client_id == cid,
                        model.entity_id == entity_id,
                        model.deleted_at.is_(None),
                    )
                )
                if row is None:
                    raise HTTPException(404, "CRM record not found")
                current = {key: getattr(row, key) for key in schema.model_fields}
            return await crm.save(db, user, cid, plural, schema.model_validate({**current, **values}), rid)
    raise HTTPException(422, "Unknown CRM action")


async def runtime_crm_action(db, **kwargs):
    try:
        result = await execute_crm_action(db, **kwargs)
        await db.commit()
        if isinstance(result, dict):
            result = {"ok": True, **result}
            if result.get("client_id") or (
                kwargs["action"] in {"create_client", "get_client", "update_client"} and result.get("id")
            ):
                cid = result.get("client_id") or result["id"]
                result["url"] = f"/team/clients?client={cid}"
        return json.dumps(result, ensure_ascii=False, default=str)
    except (HTTPException, ValidationError, ValueError) as exc:
        await db.rollback()
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "crm_request_failed",
                    "status": exc.status_code if isinstance(exc, HTTPException) else 422,
                    "message": message,
                },
            },
            ensure_ascii=False,
        )
