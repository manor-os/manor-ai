"""OAuth discovery documents for Manor's authorization server and MCP resource.

GET /.well-known/oauth-authorization-server            RFC 8414
GET /.well-known/oauth-protected-resource[/api/v1/mcp] RFC 9728

Native MCP clients (Claude Code, Codex) follow the MCP endpoint's 401 challenge
to the protected-resource document, then to the authorization-server document,
register themselves, and sign the user in through the web consent page.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from packages.core.config import get_settings
from packages.core.services.oauth_provider_service import MCP_SCOPES as _MCP_SCOPES

router = APIRouter(tags=["oauth-provider"])

MCP_PATH = "/api/v1/mcp"

# The scopes the MCP resource understands. Advertised so clients that build
# their scope request from discovery (Codex, Claude Code) send known values.
MCP_SCOPES = list(_MCP_SCOPES)


def public_origin(request: Request) -> str:
    """The user-facing origin: APP_URL (web + proxied API), else this request's."""
    configured = (get_settings().APP_URL or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def mcp_resource_url(request: Request) -> str:
    return public_origin(request) + MCP_PATH


def protected_resource_metadata_url(request: Request) -> str:
    return public_origin(request) + "/.well-known/oauth-protected-resource" + MCP_PATH


def _document(content: dict) -> JSONResponse:
    return JSONResponse(content, headers={"Cache-Control": "public, max-age=300"})


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server_metadata(request: Request):
    origin = public_origin(request)
    return _document({
        "issuer": origin,
        "authorization_endpoint": origin + "/oauth/authorize",
        "token_endpoint": origin + "/api/v1/oauth/token",
        "registration_endpoint": origin + "/api/v1/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": MCP_SCOPES,
        "resource": mcp_resource_url(request),
    })


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource" + MCP_PATH, include_in_schema=False)
async def protected_resource_metadata(request: Request):
    origin = public_origin(request)
    return _document({
        "resource": mcp_resource_url(request),
        "authorization_servers": [origin],
        "bearer_methods_supported": ["header"],
        "scopes_supported": MCP_SCOPES,
        "resource_name": "Manor Workspace Builder",
    })
