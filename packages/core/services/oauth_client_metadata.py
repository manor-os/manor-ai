"""Client ID Metadata Documents: an https ``client_id`` publishes its own metadata.

A hosted MCP client (ChatGPT, Claude) may skip registration and use the https
URL of a JSON document as its ``client_id``. Manor fetches that document, checks
it names itself and where codes may be sent, and caches it as a public PKCE
client of the MCP resource. The host serving the document is the client's
identity, so a callback on that host is trusted like the client itself.

Fetching only happens when the consent page renders and when the user consents,
never at the token endpoint, which answers from the cached row.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.oauth_provider import OAuthClientApp
from packages.core.services import dashboard_http
from packages.core.services import oauth_provider_service as svc

MAX_DOCUMENT_BYTES = 16 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
MIN_CACHE_SECONDS = 5 * 60
MAX_CACHE_SECONDS = 24 * 60 * 60
MAX_CLIENT_ID_LENGTH = 512

_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)\"?\s*(?:,|$)", re.IGNORECASE)


class MetadataDocumentError(svc.ClientRegistrationError):
    """The document could not be fetched or does not describe a usable client."""

    def __init__(self, description: str):
        super().__init__("invalid_client", description)

    def __str__(self) -> str:
        return self.description


@dataclass(frozen=True)
class FetchedDocument:
    document: dict
    max_age_seconds: int


def is_metadata_document_client_id(client_id: str) -> bool:
    """Whether a client_id is a metadata document URL: https, a path, no fragment or userinfo."""
    if not isinstance(client_id, str) or len(client_id) > MAX_CLIENT_ID_LENGTH:
        return False
    try:
        parts = urlsplit(client_id)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or port not in (None, 443):
        return False
    if parts.fragment or "#" in client_id or parts.username or parts.password:
        return False
    segments = unquote(parts.path).split("/")
    return parts.path not in ("", "/") and not any(seg in (".", "..") for seg in segments)


def cache_seconds(cache_control: Optional[str]) -> int:
    """The document's max-age, clamped to 5 minutes..24 hours (5 minutes when absent)."""
    match = _MAX_AGE.search(cache_control or "")
    seconds = int(match.group(1)) if match else 0
    return min(max(seconds, MIN_CACHE_SECONDS), MAX_CACHE_SECONDS)


async def _public_target(url: str) -> dashboard_http.PublicHttpsEndpoint:
    """Resolve the host once, refusing any non-public address, and pin the connection to it."""
    try:
        target = await dashboard_http.resolve_public_https_target(url)
    except dashboard_http.DashboardHttpPolicyError:
        raise MetadataDocumentError("client metadata document host is not a public address") from None
    except dashboard_http.DashboardHttpError:
        raise MetadataDocumentError("client metadata document host could not be resolved") from None
    if any(ipaddress.ip_address(address).is_multicast for address in target.addresses):
        raise MetadataDocumentError("client metadata document host is not a public address")
    return target


async def fetch_metadata_document(
    url: str,
    *,
    transport_factory: Callable[[dashboard_http.PublicHttpsEndpoint], httpx.AsyncBaseTransport]
    = dashboard_http.create_public_https_transport,
) -> FetchedDocument:
    """GET the document: https, public pinned address, no redirects, 5 s, 16 KB, JSON."""
    target = await _public_target(url)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            transport=transport_factory(target),
            headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "Manor-OAuth/1.0"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise MetadataDocumentError("client metadata document redirects are not followed")
                if response.status_code != 200:
                    raise MetadataDocumentError(f"client metadata document returned HTTP {response.status_code}")
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise MetadataDocumentError("compressed client metadata documents are not accepted")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise MetadataDocumentError("client metadata document is not JSON")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise MetadataDocumentError("client metadata document is larger than 16 KB")
                max_age = cache_seconds(response.headers.get("cache-control"))
    except httpx.HTTPError:
        raise MetadataDocumentError("client metadata document could not be fetched") from None
    try:
        document = json.loads(bytes(body))
    except (UnicodeDecodeError, ValueError):
        raise MetadataDocumentError("client metadata document is not valid JSON") from None
    if not isinstance(document, dict):
        raise MetadataDocumentError("client metadata document must be a JSON object")
    return FetchedDocument(document, max_age)


def _same_origin(uri: str, client_id: str) -> bool:
    try:
        parts, own = urlsplit(uri), urlsplit(client_id)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and port in (None, 443)
            and not (parts.fragment or parts.username or parts.password)
            and (parts.hostname or "").lower() == (own.hostname or "").lower())


@dataclass(frozen=True)
class ClientMetadata:
    name: str
    redirect_uris: list[str]


def validate_metadata_document(client_id: str, document: dict) -> ClientMetadata:
    """Check a fetched document describes this public client and where its codes may go."""
    if document.get("client_id") != client_id:
        raise MetadataDocumentError("client metadata document client_id does not match its URL")
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip():
        raise MetadataDocumentError("client_name must be a non-empty string")
    redirect_uris = document.get("redirect_uris")
    if (not isinstance(redirect_uris, list) or not redirect_uris
            or len(redirect_uris) > svc.MAX_REDIRECT_URIS
            or not all(isinstance(u, str) and len(u) <= 2048 for u in redirect_uris)):
        raise MetadataDocumentError("redirect_uris must list 1-10 URIs")
    for uri in redirect_uris:
        if not (svc.is_loopback_redirect(uri) or _same_origin(uri, client_id) or svc.hosted_redirect_allowed(uri)):
            svc.log_refused_redirect(uri, document.get("client_name"))
            raise MetadataDocumentError(
                "redirect URIs must be http loopback, on the client_id's host, or an allowed hosted callback",
            )
    if document.get("token_endpoint_auth_method", "none") != "none":
        raise MetadataDocumentError("only public clients (token_endpoint_auth_method=none) are supported")
    grant_types = document.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
        raise MetadataDocumentError("unsupported grant_types")
    response_types = document.get("response_types", ["code"])
    if not isinstance(response_types, list) or not set(response_types) <= {"code"}:
        raise MetadataDocumentError("unsupported response_types")
    name = name.strip()[:svc.MAX_CLIENT_NAME]
    return ClientMetadata(name=name, redirect_uris=list(dict.fromkeys(redirect_uris)))


def client_host(client_id: str) -> str:
    """The host serving a metadata document client: the identity shown at consent."""
    return (urlsplit(client_id).hostname or "").lower()


def is_fresh(client: OAuthClientApp, now: Optional[datetime] = None) -> bool:
    expires = client.metadata_expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > (now or datetime.now(timezone.utc))


async def refresh_metadata_document_client(
    db: AsyncSession, client_id: str, existing: Optional[OAuthClientApp],
) -> OAuthClientApp:
    """Fetch and validate the document, then insert or update its cached client row.

    The caller commits. A document that no longer validates raises, and the
    cached row is left as it was: it is not used until a fetch succeeds again.
    """
    fetched = await fetch_metadata_document(client_id)
    metadata = validate_metadata_document(client_id, fetched.document)
    now = datetime.now(timezone.utc)
    values = dict(
        name=metadata.name,
        redirect_uris=metadata.redirect_uris,
        metadata_fetched_at=now,
        metadata_expires_at=now + timedelta(seconds=fetched.max_age_seconds),
    )
    if existing is None:
        client = OAuthClientApp(
            client_id=client_id,
            client_secret_hash=None,
            description=None,
            allowed_scopes=[],
            active=True,
            require_pkce=True,
            require_verified_email=False,
            access_token_ttl_minutes=60,
            registration_source=svc.REGISTRATION_SOURCE_METADATA_DOCUMENT,
            token_audience=svc.MCP_TOKEN_AUDIENCE,
            **values,
        )
        try:
            async with db.begin_nested():
                db.add(client)
        except IntegrityError:
            # Another request cached the same document first; refresh that row.
            existing = await svc.get_client_by_id(db, client_id)
            if existing is None:
                raise
        else:
            return client
    for key, value in values.items():
        setattr(existing, key, value)
    await db.flush()
    return existing
