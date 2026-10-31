"""Hosted MCP endpoint for coding agents (Claude Code, Codex) building Workspaces.

POST /api/v1/mcp                       MCP Streamable HTTP (stateless, JSON responses)
GET  /api/v1/mcp/transfers/{id}        download an export (capability URL)
PUT  /api/v1/mcp/transfers/{id}        upload a local file (capability URL)

Bearer tokens must carry the MCP audience: they come from the OAuth flow a
self-registered client runs (see oauth_provider / oauth_metadata). A missing or
invalid token gets the RFC 9728 challenge that starts that flow.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import authenticate_token_claims
from apps.api.mcp import (
    tools_blueprint,
    tools_delivery,
    tools_knowledge,
    tools_work,
    tools_workspace,
    transfers,
)
from apps.api.mcp.manor_api import ManorApi
from apps.api.mcp.protocol import PARSE_ERROR, SUPPORTED_PROTOCOL_VERSIONS, McpDispatcher
from apps.api.mcp.registry import ToolContext
from apps.api.routers.oauth_metadata import MCP_PATH, protected_resource_metadata_url, public_origin
from packages.core.database import get_db
from packages.core.services.auth_service import decode_token
from packages.core.services.oauth_provider_service import MCP_TOKEN_AUDIENCE

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

_TOOLS = [
    *tools_workspace.TOOLS, *tools_knowledge.TOOLS, *tools_work.TOOLS,
    *tools_blueprint.TOOLS, *tools_delivery.TOOLS,
]
dispatcher = McpDispatcher(_TOOLS)


def _challenge(request: Request, error: str | None = None) -> JSONResponse:
    # Without a scope hint, MCP clients may request every advertised scope,
    # including optional computer access. Initial MCP access only needs mcp.
    header = f'Bearer resource_metadata="{protected_resource_metadata_url(request)}", scope="mcp"'
    if error:
        header += f', error="{error}"'
    return JSONResponse(
        {"error": error or "unauthorized"},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": header},
    )


def _origin_allowed(request: Request) -> bool:
    """DNS-rebinding guard: browsers send Origin; native clients send none."""
    origin = request.headers.get("origin")
    return origin is None or origin.rstrip("/") == public_origin(request)


async def _admit(request: Request, db: AsyncSession) -> tuple[dict | None, Response | None]:
    """The caller's MCP-audience token claims, or the response that refuses it.

    Every method answers an unauthenticated request with the RFC 9728 challenge,
    so a client's first probe (often a GET) already points at the metadata.
    """
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None, _challenge(request)
    claims = decode_token(token.strip(), audience=MCP_TOKEN_AUDIENCE)
    if not claims:
        return None, _challenge(request, "invalid_token")
    try:
        await authenticate_token_claims(request, claims, db)
    except HTTPException as e:
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            return None, _challenge(request, "invalid_token")
        return None, JSONResponse({"error": e.detail}, status_code=e.status_code)
    return claims, None


@router.post(MCP_PATH, include_in_schema=False)
async def mcp_endpoint(request: Request, db: AsyncSession = Depends(get_db)):
    if not _origin_allowed(request):
        return JSONResponse({"error": "origin not allowed"}, status_code=status.HTTP_403_FORBIDDEN)
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse({"error": f"unsupported MCP-Protocol-Version {version}"},
                            status_code=status.HTTP_400_BAD_REQUEST)

    claims, refusal = await _admit(request, db)
    if refusal is not None:
        return refusal

    try:
        message = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": PARSE_ERROR, "message": "parse error"}},
                            status_code=status.HTTP_400_BAD_REQUEST)

    client = (request.client.host, request.client.port) if request.client else None
    ctx = ToolContext(api=ManorApi(request.app, claims=claims, client=client), claims=claims, request=request)
    if isinstance(message, list):
        responses = [r for r in [await dispatcher.handle(m, ctx) for m in message] if r is not None]
        reply = responses or None
    else:
        reply = await dispatcher.handle(message, ctx)
    if reply is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(reply)


@router.get(MCP_PATH, include_in_schema=False)
@router.delete(MCP_PATH, include_in_schema=False)
async def mcp_no_stream(request: Request, db: AsyncSession = Depends(get_db)):
    _, refusal = await _admit(request, db)
    if refusal is not None:
        return refusal
    # Stateless server: no standalone SSE stream and no sessions to end.
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


def _attachment(filename: str) -> str:
    """Content-Disposition for any file name (Knowledge names are often non-ASCII)."""
    fallback = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{fallback or 'download'}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get(MCP_PATH + "/transfers/{transfer_id}", include_in_schema=False)
async def download_transfer(transfer_id: str):
    try:
        transfer = await transfers.load(transfer_id)
    except transfers.TransferUnavailable:
        return JSONResponse({"error": "transfers unavailable"}, status_code=503)
    if transfer is None or transfer.kind != transfers.DOWNLOAD or transfer.data is None:
        return JSONResponse({"error": "unknown or expired transfer"}, status_code=404)
    return Response(
        content=transfer.data,
        media_type=transfer.content_type,
        headers={"Content-Disposition": _attachment(transfer.filename), "Cache-Control": "no-store"},
    )


@router.put(MCP_PATH + "/transfers/{transfer_id}", include_in_schema=False)
async def upload_transfer(transfer_id: str, request: Request):
    try:
        transfer = await transfers.load(transfer_id)
    except transfers.TransferUnavailable:
        return JSONResponse({"error": "transfers unavailable"}, status_code=503)
    if transfer is None or transfer.kind != transfers.UPLOAD:
        return JSONResponse({"error": "unknown or expired transfer"}, status_code=404)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > transfers.MAX_TRANSFER_BYTES:
            return JSONResponse({"error": "file too large"}, status_code=413)
        chunks.append(chunk)
    await transfers.store_upload(transfer, b"".join(chunks), request.headers.get("content-type"))
    return JSONResponse({"upload_id": transfer.id, "bytes": size})
