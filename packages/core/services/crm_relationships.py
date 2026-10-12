"""Explicit, Workspace-scoped references from Relationship Ledger to company CRM.

CRM profiles stay authoritative. Only opaque subject fingerprints are persisted;
ledger history is neither copied to company scope nor bulk-matched by email.
"""

from fastapi import HTTPException
from sqlalchemy import select

from packages.core.models.people import Client
from packages.core.models.workspace import Workspace
from packages.core.services import crm, client_delivery
from packages.core.services.ledger_query_service import workspace_queryable_ledger_configs
from packages.core.services.relationship_ledger import RELATIONSHIP_LEDGER_CONTRACT_ID, read_relationship_ledger
from packages.core.services.workspace_access import user_can_read_workspace_id, user_can_write_workspace_artifacts


async def workspace_ledger(db, user, workspace_id, *, write=False):
    if not workspace_id:
        raise HTTPException(422, "A current runtime Workspace is required")
    query = select(Workspace).where(
        Workspace.id == workspace_id, Workspace.entity_id == user.entity_id, Workspace.deleted_at.is_(None)
    )
    if write:
        query = query.with_for_update()
    ws = await db.scalar(query)
    if ws is None:
        raise HTTPException(404, "Workspace not found")
    allowed = await user_can_read_workspace_id(
        db, workspace_id=ws.id, entity_id=user.entity_id, user_id=user.id, role=user.role
    )
    if not allowed or (
        write
        and not await user_can_write_workspace_artifacts(db, workspace_id=ws.id, user_id=user.id, entity_role=user.role)
    ):
        raise HTTPException(403, "Workspace access is required")
    config = workspace_queryable_ledger_configs(ws.settings).get(RELATIONSHIP_LEDGER_CONTRACT_ID)
    if config is None:
        raise HTTPException(409, "Install Relationship Ledger in the current Workspace first")
    return await read_relationship_ledger(
        entity_id=user.entity_id, workspace_id=ws.id, directory=str(config["directory"]), create_location=False
    )


async def link_relationship(db, user, client_id, workspace_id, identity_key, *, unlink=False):
    client = await crm.customer(db, user, client_id, write=True)
    ledger = await workspace_ledger(db, user, workspace_id, write=True)
    subject = next((r for r in ledger["rows"] if r["identity_key"] == identity_key), None)
    if subject is None:
        raise HTTPException(404, "Relationship Ledger subject not found")
    ref = {"workspace_id": workspace_id, "identity_fingerprint": subject["identity_fingerprint"]}
    links = list((client.meta or {}).get("relationship_links", []))
    if unlink:
        links = [r for r in links if r != ref]
    else:
        # Workspace lock serializes association changes even across two clients.
        conflict = await db.scalar(
            select(Client.id).where(
                Client.entity_id == user.entity_id,
                Client.id != client_id,
                Client.deleted_at.is_(None),
                Client.meta["relationship_links"].contains([ref]),
            )
        )
        if conflict:
            raise HTTPException(409, "This subject is already linked to another client; unlink it first")
        if ref not in links:
            links.append(ref)
    client.meta = {**(client.meta or {}), "relationship_links": links}
    await db.flush()
    return {"client_id": client_id, "workspace_id": workspace_id, "identity_key": identity_key, "linked": not unlink}


async def client_relationships(db, user, client_id, workspace_id):
    client = await crm.customer(db, user, client_id)
    ledger = await workspace_ledger(db, user, workspace_id)
    fingerprints = {
        ref["identity_fingerprint"]
        for ref in (client.meta or {}).get("relationship_links", [])
        if ref["workspace_id"] == workspace_id
    }
    return {
        "client_id": client_id,
        "workspace_id": workspace_id,
        "items": [row for row in ledger["rows"] if row["identity_fingerprint"] in fingerprints],
    }


async def enrich_relationships(db, user, workspace_id, ledger):
    # Called only after current actor and Workspace read authorization is verified.
    clients = (
        await db.scalars(
            select(Client).where(
                Client.entity_id == user.entity_id,
                Client.deleted_at.is_(None),
                Client.meta["relationship_links"].contains([{"workspace_id": workspace_id}]),
            )
        )
    ).all()
    linked = {}
    for client in clients:
        for ref in (client.meta or {}).get("relationship_links", []):
            if ref["workspace_id"] == workspace_id:
                linked[ref["identity_fingerprint"]] = client
    for row in ledger["rows"]:
        client = linked.get(row["identity_fingerprint"])
        if client is not None:
            row["crm"] = {
                "client_id": client.id,
                "profile": crm.client_json(client),
                "contacts": await client_delivery.contacts(client.id, user=user, db=db),
                "url": f"/team/clients?client={client.id}",
            }
    return ledger
