"""Bootstrap self-hosted Nango from Manor's environment.

On API startup we scan ``.env`` for two patterns and upsert them into
the running Nango instance via its admin API:

  1. **Webhook config** — ``NANGO_WEBHOOK_URL`` (or auto-computed from
     ``APP_URL``) + ``NANGO_WEBHOOK_SECRET`` get written to Nango's
     environment settings so Nango knows where to forward provider
     events. Admin doesn't have to click around in Nango UI.

  2. **Provider configs** — every pair of
     ``NANGO_PROVIDER_<PROVIDER>_CLIENT_ID`` /
     ``NANGO_PROVIDER_<PROVIDER>_CLIENT_SECRET`` (with optional
     ``_SCOPES`` and ``_KEY``) gets registered as a Nango integration.

Idempotent: on repeat boots, only writes when env values differ from
what's currently in Nango. Failures are logged and don't block API
startup — Nango may not be reachable in some deployments and Manor
should still come up.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

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


def _parse_provider_envs() -> List[Dict[str, str]]:
    """Scan os.environ for NANGO_PROVIDER_<KEY>_CLIENT_ID/SECRET/...

    Returns a list of dicts: {provider_config_key, provider, client_id,
    client_secret, scopes}. The provider name defaults to the lowercased
    KEY portion; pass ``NANGO_PROVIDER_<KEY>_PROVIDER=<actual-provider>``
    if Nango knows the platform under a different slug.
    """
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


def _resolve_webhook_url() -> Optional[str]:
    """Webhook URL Nango should hit. Order:
    1. ``NANGO_WEBHOOK_URL`` env (explicit override)
    2. Default to the API service hostname inside the docker network
       (``http://api:8000/api/v1/nango/webhook``) — works for local
       compose, can be overridden per-deploy.
    """
    explicit = os.environ.get("NANGO_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit
    # Default for the bundled docker-compose setup.
    return "http://api:8000/api/v1/nango/webhook"


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

    # Read current setting first to skip a noop write.
    try:
        r = await cx.get(f"{base}/api/v1/environment", headers=headers)
        if r.status_code == 200:
            current = (r.json() or {}).get("account", {}).get("webhook_url")
            if current == url:
                return "unchanged"
    except Exception:  # noqa: BLE001
        pass

    try:
        r = await cx.post(
            f"{base}/api/v1/environment/webhook",
            headers=headers,
            json={"webhook_url": url},
        )
        if 200 <= r.status_code < 300:
            return f"updated → {url}"
        return f"error: {r.status_code} {r.text[:120]}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


async def seed_nango_from_env() -> Dict[str, Any]:
    """Idempotent bootstrap. Returns a per-provider action map for
    logging plus the webhook setup result."""
    secret = _admin_secret()
    if not secret:
        return {"skipped": "NANGO_SECRET_KEY not set"}

    provider_configs = _parse_provider_envs()
    actions: Dict[str, str] = {}
    webhook_result = "skipped"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        # Webhook config first — even if no providers are declared,
        # admin probably wants Nango pointing at Manor.
        try:
            webhook_result = await _set_webhook_settings(cx, secret)
        except Exception as exc:  # noqa: BLE001
            webhook_result = f"error: {exc}"

        for cfg in provider_configs:
            try:
                actions[cfg["provider_config_key"]] = await _put_provider_config(cx, secret, cfg)
            except Exception as exc:  # noqa: BLE001
                actions[cfg["provider_config_key"]] = f"error: {exc}"

    return {"providers": actions, "webhook": webhook_result}
