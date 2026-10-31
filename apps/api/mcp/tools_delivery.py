"""Deliver a finished Workspace to a customer through the client-delivery contract."""
from __future__ import annotations

from typing import Any

from apps.api.mcp.registry import Tool, ToolContext, require, string


async def list_clients(ctx: ToolContext, args: dict) -> Any:
    result = await ctx.api.get("/api/v1/clients", search=args.get("search"), limit=50, offset=0)
    clients = []
    for client in result.get("items", []):
        contacts = await ctx.api.get(f"/api/v1/clients/{client['id']}/contacts")
        clients.append({
            "id": client.get("id"),
            "name": client.get("name"),
            "email": client.get("email"),
            "status": client.get("status"),
            "contacts": contacts,
        })
    return {"total": result.get("total"), "clients": clients}


async def deliver_to_client(ctx: ToolContext, args: dict) -> Any:
    client_id = require(args, "client_id")
    body = {
        "contact_id": require(args, "contact_id"),
        "workspace_id": require(args, "workspace_id"),
        "phase": args.get("phase") or "trial",
    }
    if args.get("knowledge_document_ids"):
        body["knowledge_document_ids"] = args["knowledge_document_ids"]
    return await ctx.api.post(f"/api/v1/clients/{client_id}/deliveries", body)


async def list_client_deliveries(ctx: ToolContext, args: dict) -> Any:
    return await ctx.api.get(f"/api/v1/clients/{require(args, 'client_id')}/deliveries")


async def publish_delivery_update(ctx: ToolContext, args: dict) -> Any:
    client_id = require(args, "client_id")
    delivery_id = require(args, "delivery_id")
    body = {key: args[key] for key in ("knowledge_document_ids", "release_notes") if args.get(key) is not None}
    return await ctx.api.post(f"/api/v1/clients/{client_id}/deliveries/{delivery_id}/publish-update", body)


_CLIENT_ID = string("Client id from list_clients.")

TOOLS = [
    Tool("list_clients", "List clients",
         "Your team's clients (customers) and their contacts: who a Workspace can be delivered to.",
         list_clients, properties={"search": string("Filter by name or email.")},
         read_only=True, idempotent=True),
    Tool("deliver_to_client", "Deliver Workspace to client",
         "Deliver a copy of a Workspace to a client contact and email them an invitation. "
         "phase=trial (default) installs it paused for the client to try and accept; delivered "
         "hands it over. The client must accept the invite; nothing runs in their team before that.",
         deliver_to_client,
         properties={"client_id": _CLIENT_ID,
                     "contact_id": string("Contact who receives the invitation."),
                     "workspace_id": string("Workspace to deliver."),
                     "phase": {"enum": ["trial", "delivered"], "default": "trial"},
                     "knowledge_document_ids": {"type": "array", "items": {"type": "string"},
                                                "description": "Knowledge documents to include."}},
         required=("client_id", "contact_id", "workspace_id"), destructive=True, open_world=True),
    Tool("list_client_deliveries", "List client deliveries",
         "A client's Workspace deliveries: phase, invitation and acceptance state, maintenance requests.",
         list_client_deliveries, properties={"client_id": _CLIENT_ID},
         required=("client_id",), read_only=True, idempotent=True),
    Tool("publish_delivery_update", "Publish delivery update",
         "Send the source Workspace's current configuration to a delivery as its next release. "
         "Nothing changes in the customer's Workspace until someone there reviews and applies the "
         "update (customer edits are kept as conflicts); a pending invitation installs the new "
         "release when accepted. The version moves only when installable content changed.",
         publish_delivery_update,
         properties={"client_id": _CLIENT_ID,
                     "delivery_id": string("Delivery id from list_client_deliveries."),
                     "knowledge_document_ids": {"type": "array", "items": {"type": "string"},
                                                "description": "Knowledge documents to include; "
                                                               "omit to keep the delivery's current selection."},
                     "release_notes": string("What changed, shown to the customer with the update.")},
         # Replaces the release the delivery held; the customer's Workspace is untouched.
         required=("client_id", "delivery_id"), destructive=True, idempotent=True),
]
