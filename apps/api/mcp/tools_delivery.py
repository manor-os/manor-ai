"""Deliver a finished Workspace to a customer through the client-delivery contract."""
from __future__ import annotations

from typing import Any

from apps.api.mcp.registry import Tool, ToolContext, ToolError, require, string
from apps.api.mcp.tools_blueprint import _uploaded_bytes


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
    limit, offset = args.get("limit"), args.get("offset")
    if args.get("client_id"):
        if limit is not None or offset is not None:
            raise ToolError("limit and offset page the list across all clients; one client's list is never cut")
        return await ctx.api.get(f"/api/v1/clients/{args['client_id']}/deliveries")
    rows, headers = await ctx.api.get_with_headers("/api/v1/client-deliveries", limit=limit, offset=offset)
    has_more = headers.get("X-Has-More") == "true"
    return {
        "deliveries": rows,
        "has_more": has_more,
        "next_offset": int(offset or 0) + len(rows) if has_more else None,
    }


async def publish_delivery_update(ctx: ToolContext, args: dict) -> Any:
    client_id = require(args, "client_id")
    delivery_id = require(args, "delivery_id")
    route = f"/api/v1/clients/{client_id}/deliveries/{delivery_id}/publish-update"
    if args.get("upload_id"):
        if args.get("knowledge_document_ids") is not None:
            raise ToolError("knowledge_document_ids selects from the source Workspace; a file carries its own Knowledge")
        data = await _uploaded_bytes(ctx, args["upload_id"])
        form = {"dry_run": (None, "true" if args.get("dry_run") else "false")}
        if args.get("release_notes"):
            form["release_notes"] = (None, args["release_notes"])
        return await ctx.api.request(
            "POST", f"{route}-file", files={"file": ("blueprint", data, "application/octet-stream"), **form},
        )
    body = {key: args[key] for key in ("knowledge_document_ids", "release_notes") if args.get(key) is not None}
    if args.get("dry_run"):
        body["dry_run"] = True
    return await ctx.api.post(route, body)


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
         "Workspace deliveries: phase, invitation and acceptance state, maintenance requests. Pass "
         "client_id for everything delivered to one client, including revoked invitations, as a plain "
         "list. Omit it for live deliveries across all clients, limited to Workspaces you manage, each "
         "with client_name, newest first, one page at a time: the result is {deliveries, has_more, "
         "next_offset}. While has_more is true there are more; call again with offset=next_offset "
         "before treating the list as complete.",
         list_client_deliveries,
         properties={"client_id": _CLIENT_ID,
                     "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100,
                               "description": "Page size. Only without client_id."},
                     "offset": {"type": "integer", "minimum": 0, "default": 0,
                                "description": "Rows to skip: next_offset from the previous page. "
                                               "Only without client_id."}},
         read_only=True, idempotent=True),
    Tool("publish_delivery_update", "Publish delivery update",
         "Send the delivery its next release: the source Workspace's current configuration, or with "
         "upload_id a Blueprint built elsewhere (a .manor package or payload merged in git, or exported "
         "from staging; start it from the delivery's current release so components match). "
         "Nothing changes in the customer's Workspace until someone there applies the update: their "
         "Workspace chat gets a card that applies it in one click (customer edits are kept) and its "
         "owner a notification; a pending invitation installs the new release when accepted. The version moves only when installable content changed. "
         "dry_run=true returns the version, step (patch/minor/major), reasons and changed components "
         "without sending; run it first and show the user.",
         publish_delivery_update,
         properties={"client_id": _CLIENT_ID,
                     "delivery_id": string("Delivery id from list_client_deliveries."),
                     "upload_id": string("Blueprint file (.manor or .json) sent with create_upload; "
                                         "omit to re-export the source Workspace."),
                     "dry_run": {"type": "boolean", "default": False,
                                 "description": "Report what the customer would get; send nothing."},
                     "knowledge_document_ids": {"type": "array", "items": {"type": "string"},
                                                "description": "Knowledge documents to include; "
                                                               "omit to keep the delivery's current selection."},
                     "release_notes": string("What changed, shown to the customer with the update.")},
         # Replaces the release the delivery held; the customer's Workspace is untouched.
         required=("client_id", "delivery_id"), destructive=True, idempotent=True),
]
