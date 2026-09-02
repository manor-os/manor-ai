"""Bootstrap self-hosted Nango from Manor's environment.

On API startup we scan ``.env`` for two patterns and upsert them into
the running Nango instance via its admin API:

  1. **Provider configs** — every pair of
     ``NANGO_PROVIDER_<PROVIDER>_CLIENT_ID`` /
     ``NANGO_PROVIDER_<PROVIDER>_CLIENT_SECRET`` (with optional
     ``_SCOPES`` and ``_KEY``) gets registered as a Nango integration.

  2. **Webhook config** — ``NANGO_WEBHOOK_URL`` (or the local API
     service default) is written to Nango with a derived query token so
     Nango can forward provider events without a signature header.

Idempotent: on repeat boots, only writes when env values differ from
what's currently in Nango. Failures are logged and don't block API
startup — Nango may not be reachable in some deployments and Manor
should still come up.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)


# Reserved env names that should NOT be parsed as provider configs.
_RESERVED = {
    "NANGO_BASE_URL", "NANGO_PUBLIC_URL", "NANGO_SECRET_KEY",
    "NANGO_WEBHOOK_SECRET", "NANGO_WEBHOOK_URL", "NANGO_DB_PASSWORD",
    "NANGO_ADMIN_INVITE_TOKEN", "NANGO_ENCRYPTION_KEY",
    "NANGO_PROVIDERS",  # historical list-style, ignored
}

_TIMEOUT = 10.0


def _nango_base() -> str:
    return os.environ.get("NANGO_BASE_URL", "http://nango-server:3003").rstrip("/")


def _admin_secret() -> Optional[str]:
    s = os.environ.get("NANGO_SECRET_KEY", "").strip()
    return s or None


def _connect_hmac_key() -> Optional[str]:
    key = os.environ.get("NANGO_CONNECT_HMAC_KEY", "").strip()
    return key or None


def _derive_webhook_token() -> Optional[str]:
    """Derive the URL credential shared by bootstrap and the API receiver."""
    key = _connect_hmac_key() or _admin_secret()
    if not key:
        return None
    return hmac.new(
        key.encode("utf-8"),
        b"manor-nango-webhook-v1",
        hashlib.sha256,
    ).hexdigest()


def _provider_env_values() -> Dict[str, Dict[str, str]]:
    """Group declared Nango provider environment fields by provider key."""
    prefix = "NANGO_PROVIDER_"
    by_key: Dict[str, Dict[str, str]] = {}

    for env_name, value in os.environ.items():
        if not env_name.startswith(prefix):
            continue
        if env_name in _RESERVED:
            continue
        rest = env_name[len(prefix):]
        # Expect NANGO_PROVIDER_<KEY>_<FIELD> where FIELD is one of:
        # CLIENT_ID, CLIENT_SECRET, SCOPES, PROVIDER, KEY
        for field in ("_CLIENT_ID", "_CLIENT_SECRET", "_SCOPES", "_PROVIDER", "_KEY"):
            if rest.endswith(field):
                key = rest[: -len(field)].lower()
                if not key:
                    continue
                by_key.setdefault(key, {})[field.lstrip("_").lower()] = value.strip()
                break

    return by_key


def _parse_provider_envs() -> List[Dict[str, str]]:
    """Scan complete NANGO_PROVIDER_<KEY>_CLIENT_ID/SECRET pairs.

    Returns a list of dicts: {provider_config_key, provider, client_id,
    client_secret, scopes}. The provider name defaults to the lowercased
    KEY portion; pass ``NANGO_PROVIDER_<KEY>_PROVIDER=<actual-provider>``
    if Nango knows the platform under a different slug.
    """
    by_key = _provider_env_values()

    out: List[Dict[str, str]] = []
    for key, cfg in by_key.items():
        client_id = cfg.get("client_id")
        client_secret = cfg.get("client_secret")
        if not client_id or not client_secret:
            logger.debug("nango_bootstrap: skipping %s — missing client_id or client_secret", key)
            continue
        out.append({
            "provider_config_key": cfg.get("key") or key,
            "provider": cfg.get("provider") or key,
            "oauth_client_id": client_id,
            "oauth_client_secret": client_secret,
            "oauth_scopes": cfg.get("scopes") or "",
        })
    return out


def _incomplete_provider_envs() -> Dict[str, str]:
    """Return bootstrap errors for provider bundles with a missing credential."""
    errors: Dict[str, str] = {}
    for key, cfg in _provider_env_values().items():
        missing = [field for field in ("client_id", "client_secret") if not cfg.get(field)]
        if missing:
            errors[cfg.get("key") or key] = f"error: missing {', '.join(missing)}"
    return errors


def _resolve_webhook_url() -> Optional[str]:
    """Webhook URL Nango should hit. Order:
    1. ``NANGO_WEBHOOK_URL`` env (explicit override)
    2. Default to the API service hostname inside the docker network
       (``http://manor-api:8000/api/v1/nango/webhook``) — works for local
       compose, can be overridden per-deploy.
    """
    token = _derive_webhook_token()
    if not token:
        return None
    explicit = os.environ.get("NANGO_WEBHOOK_URL", "").strip()
    base_url = explicit or "http://manor-api:8000/api/v1/nango/webhook"
    parsed = urlsplit(base_url)
    query = [
        pair
        for pair in parse_qsl(parsed.query, keep_blank_values=True)
        if pair[0] != "nango_webhook_token"
    ]
    query.append(("nango_webhook_token", token))
    return urlunsplit(parsed._replace(query=urlencode(query)))


async def _put_provider_config(
    cx: httpx.AsyncClient, secret: str, cfg: Dict[str, str],
) -> str:
    """Idempotent upsert via Nango 0.36 ``/config`` API.

      POST   /config            — create (409 if exists → fall to PUT)
      PUT    /config            — update (body shape same as POST)

    Returns one of: "created" | "updated" | "error: <reason>"
    """
    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    base = _nango_base()

    try:
        r = await cx.post(f"{base}/config", headers=headers, json=cfg)
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    if 200 <= r.status_code < 300:
        return "created"
    if r.status_code == 409 or "duplicate" in (r.text or "").lower():
        try:
            r2 = await cx.put(f"{base}/config", headers=headers, json=cfg)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        if 200 <= r2.status_code < 300:
            return "updated"
        return f"error: PUT {r2.status_code} {r2.text[:120]}"
    return f"error: POST {r.status_code} {r.text[:120]}"


async def _set_webhook_settings(
    cx: httpx.AsyncClient, secret: str,
) -> str:
    """Tell Nango which URL to POST webhooks to.

    Nango 0.36 exposes ``POST /api/v1/environment/webhook`` taking
    ``{webhook_url}``. Idempotent — skips when the value already
    matches.
    """
    url = _resolve_webhook_url()
    if not url:
        return "skipped: no webhook url"

    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    base = _nango_base()

    # The authenticated read is also the database-backed release gate.
    try:
        r = await cx.get(f"{base}/api/v1/environment", headers=headers)
    except Exception as exc:  # noqa: BLE001
        return f"error: environment lookup {exc}"
    if r.status_code != 200:
        return f"error: environment lookup {r.status_code} {r.text[:120]}"
    current = (r.json() or {}).get("account", {}).get("webhook_url")
    if current == url:
        return "unchanged"

    try:
        r = await cx.post(
            f"{base}/api/v1/environment/webhook",
            headers=headers,
            json={"webhook_url": url},
        )
        if 200 <= r.status_code < 300:
            return "updated"
        return f"error: {r.status_code} {r.text[:120]}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


async def _set_connect_hmac_settings(
    cx: httpx.AsyncClient, secret: str, hmac_key: str,
) -> str:
    if not hmac_key:
        return "error: NANGO_CONNECT_HMAC_KEY not set"

    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    base = _nango_base()
    settings = (
        ("hmac key", "/api/v1/environment/hmac-key", {"hmac_key": hmac_key}),
        ("hmac enabled", "/api/v1/environment/hmac-enabled", {"hmac_enabled": True}),
    )
    for label, path, payload in settings:
        try:
            response = await cx.post(f"{base}{path}", headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001
            return f"error: {label} {exc}"
        if not 200 <= response.status_code < 300:
            return f"error: {label} {response.status_code} {response.text[:120]}"
    return "updated"


async def seed_nango_from_env() -> Dict[str, Any]:
    """Idempotent bootstrap. Returns a per-provider action map for
    logging plus the webhook setup result."""
    secret = _admin_secret()
    if not secret:
        return {"skipped": "NANGO_SECRET_KEY not set"}

    provider_configs = _parse_provider_envs()
    actions = _incomplete_provider_envs()
    webhook_result = "skipped"
    hmac_result = "skipped"
    hmac_key = _connect_hmac_key()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        # Create provider configs before changing environment settings. On a
        # fresh Nango environment, settings updates can briefly race provider
        # creation and turn the POST/PUT upsert into a false duplicate/missing
        # sequence.
        for cfg in provider_configs:
            try:
                actions[cfg["provider_config_key"]] = await _put_provider_config(cx, secret, cfg)
            except Exception as exc:  # noqa: BLE001
                actions[cfg["provider_config_key"]] = f"error: {exc}"

        try:
            hmac_result = await _set_connect_hmac_settings(cx, secret, hmac_key or "")
        except Exception as exc:  # noqa: BLE001
            hmac_result = f"error: {exc}"

        try:
            webhook_result = await _set_webhook_settings(cx, secret)
        except Exception as exc:  # noqa: BLE001
            webhook_result = f"error: {exc}"

    return {"providers": actions, "webhook": webhook_result, "hmac": hmac_result}
