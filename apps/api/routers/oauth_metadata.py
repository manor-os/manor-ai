"""OAuth discovery documents for Manor's authorization server and MCP resource.

GET /.well-known/oauth-authorization-server            RFC 8414
GET /.well-known/oauth-protected-resource[/api/v1/mcp] RFC 9728

Native MCP clients (Claude Code, Codex) follow the MCP endpoint's 401 challenge
to the protected-resource document, then to the authorization-server document,
register themselves (or name a Client ID Metadata Document as their client_id),
and sign the user in through the web consent page.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from packages.core.config import get_settings
from packages.core.services import oauth_provider_service as oauth

router = APIRouter(tags=["oauth-provider"])

MCP_PATH = "/api/v1/mcp"

# Advertised so clients that build their scope request from discovery (Codex,
# Claude Code) send known values.
MCP_SCOPES = list(oauth.MCP_SCOPES)

# Resources this server issues tokens for: token audience → resource path. A
# client's audience is fixed; its RFC 8707 ``resource`` may only name that one.
PROTECTED_RESOURCES = {oauth.MCP_TOKEN_AUDIENCE: MCP_PATH}

_CLIENT_AUTH_METHODS = ["none", "client_secret_post", "client_secret_basic"]


def public_origin(request: Request) -> str:
    """The user-facing origin: APP_URL (web + proxied API), else this request's."""
    configured = (get_settings().APP_URL or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def issuer(request: Request) -> str:
    """The issuer identifier; RFC 9207 ``iss`` must repeat it exactly."""
    return public_origin(request)


def mcp_resource_url(request: Request) -> str:
    return public_origin(request) + MCP_PATH


def protected_resource_metadata_url(request: Request) -> str:
    return public_origin(request) + "/.well-known/oauth-protected-resource" + MCP_PATH


def _origin_and_path(url: str) -> Optional[tuple[str, str]]:
    """Normalized (scheme://host[:port], path) of an absolute URL, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.fragment or parts.query:
        return None
    if parts.username or parts.password:
        return None
    default_port = {"http": 80, "https": 443}[parts.scheme]
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}", parts.path.rstrip("/")


def resource_allowed(request: Request, resource: Optional[str], audience: Optional[str]) -> bool:
    """Whether an RFC 8707 ``resource`` names the resource of this audience.

    Absent means the client's own audience. This server is reachable under its
    public origin and the request's own origin; some clients send just the
    origin, which is accepted as naming the same resource.
    """
    if resource is None:
        return True
    target = _origin_and_path(resource)
    if target is None:
        return False
    origins = {_origin_and_path(public_origin(request)), _origin_and_path(str(request.base_url))}
    own_origins = {o[0] for o in origins if o is not None}
    origin, path = target
    if origin not in own_origins:
        return False
    return path in ("", PROTECTED_RESOURCES.get(audience or "", ""))


def _document(content: dict) -> JSONResponse:
    return JSONResponse(content, headers={"Cache-Control": "public, max-age=300"})


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server_metadata(request: Request):
    origin = public_origin(request)
    return _document({
        "issuer": issuer(request),
        "authorization_endpoint": origin + "/oauth/authorize",
        "token_endpoint": origin + "/api/v1/oauth/token",
        "registration_endpoint": origin + "/api/v1/oauth/register",
        "revocation_endpoint": origin + "/api/v1/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": _CLIENT_AUTH_METHODS,
        "revocation_endpoint_auth_methods_supported": _CLIENT_AUTH_METHODS,
        "authorization_response_iss_parameter_supported": True,
        # An https client_id names its own metadata document (oauth_client_metadata).
        "client_id_metadata_document_supported": True,
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
