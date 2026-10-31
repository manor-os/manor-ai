"""OAuth contracts for the independently deployed PMS and Minutes services.

Only deployment configuration chooses endpoints. Saved account metadata binds a
grant to that deployment and its original Manor owner/entity; it never supplies
an endpoint or credentials to a model.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit, urlunsplit

import httpx


MANOR_OAUTH_PROVIDERS = {
    "manor_pms": ("PMS", "https://portfolio.manorai.xyz", "pms:read pms:write"),
    "manor_mcp_minutes": ("MINUTES", "https://minutes.manorai.xyz", "minutes:read minutes:write"),
}


def is_manor_oauth_provider(provider: str) -> bool:
    return provider in MANOR_OAUTH_PROVIDERS


def _deployment_url(raw: str, *, origin: bool = False) -> str:
    parsed = urlsplit(raw.strip())
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query or parsed.fragment
        or parsed.port == 0
        or (origin and parsed.path not in {"", "/"})
    ):
        raise ValueError("OAuth deployment URL must be a valid HTTP(S) URL without credentials, query, or fragment.")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            raise ValueError("OAuth deployment URLs require HTTPS except for local loopback development.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def manor_oauth_endpoints(provider: str) -> dict[str, str]:
    prefix, default_origin, scopes = MANOR_OAUTH_PROVIDERS[provider]
    base = _deployment_url(os.getenv(f"{prefix}_OAUTH_BASE_URL", "").strip() or default_origin, origin=True)
    authorize = _deployment_url(os.getenv(f"{prefix}_OAUTH_AUTHORIZE_URL", "").strip() or f"{base}/oauth/authorize")
    return {
        "issuer": base,
        "authorize_url": authorize,
        "token_url": f"{base}/api/oauth/token",
        "userinfo_url": f"{base}/api/oauth/userinfo",
        "revoke_url": f"{base}/api/oauth/revoke",
        "scopes": scopes,
        "client_id_env": f"{prefix}_OAUTH_CLIENT_ID",
        "client_secret_env": f"{prefix}_OAUTH_CLIENT_SECRET",
    }


def manor_application_url(provider: str) -> str | None:
    """Public application navigation, derived only from trusted web config."""
    if not is_manor_oauth_provider(provider):
        return None
    prefix, default_origin, _ = MANOR_OAUTH_PROVIDERS[provider]
    authorize_url = os.getenv(f"{prefix}_OAUTH_AUTHORIZE_URL", "").strip()
    try:
        api_origin = _deployment_url(os.getenv(f"{prefix}_OAUTH_BASE_URL", "").strip() or default_origin, origin=True)
        if not authorize_url and api_origin != default_origin:
            # A custom API origin need not serve the application's UI.
            # Require its explicit frontend configuration before linking it.
            return None
        web = urlsplit(_deployment_url(authorize_url or f"{default_origin}/oauth/authorize"))
    except ValueError:
        return None
    path = "/management" if provider == "manor_pms" else "/"
    return urlunsplit((web.scheme, web.netloc, path, "", ""))


def manor_mcp_endpoint(provider: str) -> str:
    if provider == "manor_pms":
        from packages.core.ai.mcp.manor_pms import _mcp_url

        return _deployment_url(_mcp_url(os.getenv("PMS_API_URL", "").strip() or "https://portfolio.manorai.xyz/api/mcp"))
    return _deployment_url(os.getenv("MINUTES_MCP_URL", "").strip() or "https://minutes.manorai.xyz/api/mcp/")


def manor_oauth_binding(account) -> dict:
    profile = account.profile if isinstance(account.profile, dict) else {}
    binding = profile.get("manor_oauth")
    return binding if isinstance(binding, dict) else {}


def manor_oauth_account_matches(account, *, entity_id: str | None = None) -> bool:
    if not is_manor_oauth_provider(account.provider):
        return True
    binding = manor_oauth_binding(account)
    try:
        return bool(
            binding.get("version") == 1
            and binding.get("user_id") == account.user_id
            and binding.get("entity_id")
            and (entity_id is None or binding["entity_id"] == entity_id)
            and binding.get("subject") == account.provider_user_id
            and binding.get("issuer") == manor_oauth_endpoints(account.provider)["issuer"]
            and binding.get("mcp_url") == manor_mcp_endpoint(account.provider)
            and not (account.profile or {}).get("oauth_disconnect")
            and not ((account.profile or {}).get("oauth_refresh") or {}).get("reauth_required")
        )
    except (KeyError, TypeError, ValueError):
        return False


async def resolve_manor_oauth_identity(
    provider: str, access_token: str, *, user_id: str, entity_id: str,
) -> tuple[str, dict]:
    """Prove the grant's identity using its fixed provider endpoint."""
    from packages.core.services.oauth_flow import OAuthFlowError

    if not user_id or not entity_id or not access_token:
        raise OAuthFlowError(400, "OAuth connection is missing its owner or Entity context.")
    try:
        endpoints = manor_oauth_endpoints(provider)
        mcp_url = manor_mcp_endpoint(provider)
    except ValueError as exc:
        raise OAuthFlowError(503, "OAuth deployment configuration is invalid; contact the administrator.") from exc
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                endpoints["userinfo_url"],
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if response.status_code != 200:
            raise OAuthFlowError(response.status_code if response.status_code in {401, 403} else 502, f"OAuth identity verification returned HTTP {response.status_code}.")
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OAuthFlowError(502, "OAuth identity verification is unavailable or invalid.") from exc
    if not isinstance(data, dict):
        raise OAuthFlowError(502, "OAuth identity verification returned an invalid account.")
    subject = data.get("sub")
    if (
        not isinstance(subject, str) or not subject.strip() or len(subject) > 255
        or any(ord(char) < 32 for char in subject)
        or data.get("manor_user_id") != user_id
        or data.get("manor_entity_id") != entity_id
    ):
        raise OAuthFlowError(403, "Authorize the same Manor user and Entity that started this connection.")
    profile = {
        "manor_oauth": {
            "version": 1, "user_id": user_id, "entity_id": entity_id,
            "subject": subject, "issuer": endpoints["issuer"],
            "mcp_url": mcp_url,
        },
    }
    # PMS uses an independent integer entity namespace. Preserve only a live,
    # strictly typed value; opaque subject strings are not an entity contract.
    if provider == "manor_pms" and type(data.get("entity_id")) is int and data["entity_id"] != 0:
        profile["manor_oauth"]["remote_entity_id"] = data["entity_id"]
    for source, target in (("email", "email"), ("name", "display_name")):
        value = data.get(source)
        if isinstance(value, str) and value.strip():
            profile[target] = value[:320]
    return subject, profile


async def verify_manor_oauth_account(account, access_token: str) -> None:
    from packages.core.services.oauth_flow import OAuthFlowError

    if not manor_oauth_account_matches(account):
        raise OAuthFlowError(409, "OAuth connection configuration changed or was disconnected; reconnect.")
    subject, _ = await resolve_manor_oauth_identity(
        account.provider, access_token, user_id=account.user_id,
        entity_id=manor_oauth_binding(account)["entity_id"],
    )
    if subject != account.provider_user_id:
        raise OAuthFlowError(403, "OAuth grant identity changed; reconnect.")


async def revoke_manor_oauth_token(db, account, token: str, *, issuer: str | None = None) -> bool:
    """Revoke through current trusted configuration, never a saved URL."""
    from packages.core.credentials import CredentialError
    from packages.core.services.oauth_provider_config import build_token_request_auth, resolve_oauth_config

    binding = manor_oauth_binding(account)
    try:
        endpoints = manor_oauth_endpoints(account.provider)
        if (issuer or binding.get("issuer")) != endpoints["issuer"]:
            return False
        config = await resolve_oauth_config(db, account.provider)
        if config is None or not token:
            return False
        headers, body = build_token_request_auth(config, {
            "token": token, "client_id": config.client_id, "client_secret": config.client_secret,
        })
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False) as client:
            response = await client.post(endpoints["revoke_url"], data=body, headers=headers)
        return 200 <= response.status_code < 300
    except (httpx.HTTPError, ValueError, CredentialError):
        return False


async def _provider_confirms_previous_grants_retired(account, access_token: str) -> bool:
    """Require a fresh, authenticated receipt covering the original identity.

    Optional companion-provider contract: grant_status v1 confirms that the
    presented token's grant is the only live grant for this subject across all
    clients. Absence, changed deployments, and pending cleanup fail closed.
    The receipt is fetched here, never trusted from saved profile metadata.
    """
    binding = manor_oauth_binding(account)
    try:
        endpoints = manor_oauth_endpoints(account.provider)
        if (
            binding.get("version") != 1
            or binding.get("issuer") != endpoints["issuer"]
            or binding.get("user_id") != account.user_id
            or binding.get("subject") != account.provider_user_id
            or not binding.get("entity_id")
            or (account.profile or {}).get("oauth_disconnect")
        ):
            return False
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                endpoints["userinfo_url"],
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if response.status_code != 200:
            return False
        data = response.json()
        receipt = data.get("grant_status") if isinstance(data, dict) else None
        return bool(
            isinstance(receipt, dict)
            and type(receipt.get("version")) is int and receipt["version"] == 1
            and receipt.get("sole_active_grant") is True
            and isinstance(receipt.get("grant_id"), str) and receipt["grant_id"].strip()
            and data.get("sub") == binding["subject"]
            and data.get("manor_user_id") == account.user_id
            and data.get("manor_entity_id") == binding["entity_id"]
        )
    except (httpx.HTTPError, ValueError, KeyError):
        return False


async def prepare_manor_oauth_replacement(db, account, *, access_token: str, refresh_token: str) -> None:
    """Retain both grants durably until retirement of the old grant succeeds.

    The encrypted payload holds pending grant tokens. Profile contains only a
    disabled status; a failed replacement can be cleaned up by Disconnect.
    """
    from packages.core.credentials import CredentialDecryptError, get_credential_service
    from packages.core.services.integration_account_service import lock_runtime_integration_account_scope
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
    from packages.core.services.oauth_flow import OAuthFlowError

    await db.refresh(account, with_for_update=True)
    try:
        old = lease_oauth_account_tokens(account, requester_id=account.user_id, reason="oauth.reconnect.stage")
    except CredentialDecryptError as exc:
        if await _provider_confirms_previous_grants_retired(account, access_token):
            old = {"revocation_confirmed": True}
        else:
            # A failed recovery must not orphan the newly exchanged grant. If
            # revocation fails too, retain both its tokens and the old encrypted
            # envelope durably for cleanup, without making either usable.
            if not await revoke_manor_oauth_token(
                db, account, refresh_token or access_token,
                issuer=manor_oauth_endpoints(account.provider)["issuer"],
            ):
                get_credential_service().store_oauth_account(account, {
                    "unreadable_credential": {
                        "ref": account.credential_ref, "scheme": account.credential_scheme,
                    },
                    "pending_revocations": [{
                        "access_token": access_token, "refresh_token": refresh_token,
                        "issuer": manor_oauth_endpoints(account.provider)["issuer"],
                    }],
                })
                account.profile = {**(account.profile or {}), "oauth_disconnect": {"status": "pending_replacement"}}
                await db.commit()
            raise OAuthFlowError(502, "Previous credentials are unreadable and provider revocation is not confirmed. Connection remains disabled; contact the administrator for recovery.") from exc
    already_pending = bool((account.profile or {}).get("oauth_disconnect"))
    pending = list(old.get("pending_revocations") or [])
    pending.append({
        "access_token": access_token, "refresh_token": refresh_token,
        "issuer": manor_oauth_endpoints(account.provider)["issuer"],
    })
    get_credential_service().store_oauth_account(account, {**old, "pending_revocations": pending})
    account.profile = {**(account.profile or {}), "oauth_disconnect": {"status": "pending_replacement"}}
    staged_ref = account.credential_ref
    await db.commit()
    if already_pending:
        raise OAuthFlowError(409, "Connection replacement is pending cleanup. Retry Disconnect before connecting again.")
    # Keep retirement and activation serialized with health, refresh and
    # Disconnect. Reacquire scope before row lock, matching normal default
    # normalization and avoiding an inverted lock order after the commit.
    await lock_runtime_integration_account_scope(
        db, kind="oauth_account", user_id=account.user_id,
        entity_id=manor_oauth_binding(account)["entity_id"], provider=account.provider,
    )
    await db.refresh(account, with_for_update=True)
    if account.credential_ref != staged_ref:
        raise OAuthFlowError(409, "Connection changed during replacement. Retry Disconnect before connecting again.")
    old_token = old.get("refresh_token") or old.get("access_token") or ""
    if old.get("revocation_confirmed") is not True and not await revoke_manor_oauth_token(db, account, old_token):
        raise OAuthFlowError(502, "Connection is disabled in Manor. Previous grant revocation is not confirmed; retry Disconnect to clean up both grants.")


async def revoke_manor_oauth_grants(db, account, tokens: dict) -> bool:
    """Disconnect also retires grants retained after a failed replacement."""
    from packages.core.credentials import get_credential_service

    grants = [{**tokens, "issuer": manor_oauth_binding(account).get("issuer")}, *tokens.get("pending_revocations", [])]
    confirmed = True
    for grant in grants:
        revoked = grant.get("revocation_confirmed") is True or await revoke_manor_oauth_token(
            db, account, grant.get("refresh_token") or grant.get("access_token") or "",
            issuer=grant.get("issuer"),
        )
        grant["revocation_confirmed"] = revoked
        confirmed = revoked and confirmed
    # Retain confirmed outcomes across retries/configuration restoration. No
    # token is sent to a replacement issuer just to finish local deletion.
    tokens["revocation_confirmed"] = grants[0]["revocation_confirmed"]
    get_credential_service().store_oauth_account(account, tokens)
    return confirmed
