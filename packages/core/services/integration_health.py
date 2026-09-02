"""Integration health checks — per-provider "does it actually work" tests.

Each provider has a ``test_connection`` function that does ONE cheap API
call to verify the credentials actually reach the upstream:

  - Gmail / Calendar / Drive  → GET /oauth2/v3/userinfo
  - YouTube                  → GET /youtube/v3/channels?part=id&mine=true
  - Outlook                  → GET /graph.microsoft.com/v1.0/me
  - Email (IMAP+SMTP)         → IMAP LOGIN + LOGOUT
  - Telegram                  → getMe
  - Slack                     → auth.test
  - Discord                   → /users/@me
  - WhatsApp                  → GET /{phone_number_id}
  - Twilio                    → GET /Accounts/{sid}.json
  - Stripe                    → GET /v1/balance
  - GitHub                    → GET /user
  - LinkedIn                  → GET /v2/userinfo
  - Notion                    → GET /v1/users/me
  - QuickBooks                → GET /companyinfo
  - WeChat Official           → /cgi-bin/token refresh
  - WeChat Personal           → runner /sessions/{session_id}/status
  - Webhook                   → HEAD the configured URL
  - Twitter / X               → GET /2/users/me

Return shape:
    {
      "ok":          bool,      # green / red
      "detail":      str,        # "fine" | "401 Unauthorized" | …
      "latency_ms":  float,      # round-trip time
      "checked_at":  ISO string,
    }

No exceptions bubble out; a network failure becomes ``{ok: false, detail: "..."}``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict

from packages.core.external_api_versions import META_GRAPH as _META_PIN
from packages.core.integrations.registry import (
    canonical_integration_key,
    get_integration_spec,
    health_checker_for,
    register_health_checker,
    register_integration,
)
from packages.core.services import smtp_transport

# Convenience: every Meta Graph URL in this module pulls its version
# from the central pin so a bump in external_api_versions.py
# propagates without grepping the repo.
_META_BASE = f"https://graph.facebook.com/{_META_PIN.value}"

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


HealthResult = Dict[str, Any]


class WhatsAppAccountReadinessCode(str, Enum):
    READY = "ready"
    OAUTH_MISSING = "oauth_missing"
    ASSET_MISMATCH = "asset_mismatch"
    PHONE_NOT_REGISTERED = "phone_not_registered"
    APP_NOT_SUBSCRIBED = "app_not_subscribed"
    CALLBACK_NOT_READY = "callback_not_ready"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(detail: str, t0: float) -> HealthResult:
    return {"ok": True, "detail": detail, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "checked_at": _now_iso()}


def _fail(detail: str, t0: float) -> HealthResult:
    return {"ok": False, "detail": detail, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "checked_at": _now_iso()}


async def _http_get(
    url: str, *, headers: Dict[str, str] | None = None, timeout: float = 10,
) -> "httpx.Response":
    assert httpx is not None, "httpx required"
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(url, headers=headers or {})


async def _http_post(
    url: str, *, data: Any = None, json_body: Any = None,
    headers: Dict[str, str] | None = None, timeout: float = 10,
) -> "httpx.Response":
    assert httpx is not None, "httpx required"
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, data=data, json=json_body, headers=headers or {})


# ── Provider tests ──────────────────────────────────────────────────────────

async def _test_with_bearer(
    name: str, url: str, token: str, *, timeout: float = 10,
) -> HealthResult:
    """Generic bearer-token GET — used by many providers whose API has a
    cheap `GET /me` style endpoint."""
    t0 = time.monotonic()
    if not token:
        return _fail(f"No {name} token on record.", t0)
    try:
        resp = await _http_get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        return _ok("reachable + authorized", t0)
    if resp.status_code in (401, 403):
        return _fail(f"{resp.status_code} — token rejected; reconnect.", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_google_userinfo(creds: dict) -> HealthResult:
    """Any Google OAuth scope with openid/profile grants /userinfo — good
    sanity check for Gmail, Calendar, Drive, all sharing the same app."""
    return await _test_with_bearer(
        "Google", "https://openidconnect.googleapis.com/v1/userinfo",
        creds.get("access_token", ""),
    )


async def test_youtube(creds: dict) -> HealthResult:
    """Validate that the OAuth token can access the authenticated channel."""
    return await _test_with_bearer(
        "YouTube",
        "https://www.googleapis.com/youtube/v3/channels?part=id&mine=true",
        creds.get("access_token", ""),
    )


async def test_outlook(creds: dict) -> HealthResult:
    """Validate a delegated Microsoft Graph token without mailbox writes."""
    return await _test_with_bearer(
        "Outlook",
        "https://graph.microsoft.com/v1.0/me",
        creds.get("access_token", ""),
    )


async def test_facebook(creds: dict) -> HealthResult:
    """Validate the Meta user token without creating or changing content."""
    return await _test_with_bearer(
        "Facebook",
        f"{_META_BASE}/me?fields=id,name",
        creds.get("access_token", ""),
    )


_APP_PASSWORD_HOSTS = ("gmail", "googlemail", "google", "mail.me.com", "icloud", "yahoo")

# Phrases that mean "the provider looked at these credentials and said
# no" — as opposed to "we could not reach the provider". Only the former
# should ever cost an integration its availability: a DNS blip or a 502
# says nothing about whether the stored secret is good, and disabling a
# working integration over one is worse than letting a call fail.
#
# Keep every marker specific enough that it cannot appear in a transient
# message. A bare "535", for instance, also matches "failed after 535 ms".
_CREDENTIAL_REJECTION_MARKERS = (
    "authenticationfailed",
    "invalid credentials",
    "username and password not accepted",
    "authentication failed",
    "token rejected",          # _test_with_bearer's wording for 401/403
    "5.7.8",                   # SMTP enhanced status code for bad auth
    "(535,",                   # smtplib's repr of a 535 reply
    "535 5.7.8",
)


def is_credential_rejection(detail: str | None) -> bool:
    """True when a failed health check means the credentials were refused.

    Callers use this to decide whether a failure is the user's to fix
    (re-enter the secret) or something that may clear on its own.
    """
    text = str(detail or "").lower()
    return any(marker in text for marker in _CREDENTIAL_REJECTION_MARKERS)


def classify_nango_health(
    *,
    ok: bool | None = None,
    detail: str | None = None,
    provider_config_present: bool = True,
    webhook_configured: bool | None = None,
    permission_denied: bool = False,
    disabled: bool = False,
) -> str:
    """Map Nango/provider checks to stable operator-facing reason codes."""
    if disabled:
        return "disabled"
    if not provider_config_present:
        return "provider_config_missing"
    if webhook_configured is False:
        return "webhook_config_missing"
    if permission_denied or "permission" in str(detail or "").lower():
        return "permission_denied"
    text = str(detail or "").lower()
    if is_credential_rejection(detail) or "401" in text or "invalid credential" in text:
        return "credentials_rejected"
    if ok is True:
        return "healthy"
    return "provider_unavailable"


def _auth_failure_hint(exc: Exception, *, host: str, username: str) -> str:
    """Turn a bare 'Invalid credentials' into something actionable.

    These providers return the same rejection for a wrong password, a
    normal account password used instead of an app password, and a
    username missing its domain — so the raw error can't be acted on.
    """
    if not is_credential_rejection(str(exc)):
        return ""
    if not any(marker in host.lower() for marker in _APP_PASSWORD_HOSTS):
        return ""

    hints = []
    if "@" not in username:
        hints.append(
            f"the username must be the full email address, not '{username}'"
        )
    hints.append(
        "this provider rejects normal account passwords — generate an"
        " app password (2FA must be on) and paste it without spaces"
    )
    return " — " + "; ".join(hints) + "."


async def test_email_login(creds: dict) -> HealthResult:
    """Attempt real logins on BOTH halves of the credential bundle:
    IMAP LOGIN (read) and SMTP AUTH (send).

    ``ok`` is true only when every *configured* protocol authenticates —
    a send-only bundle (no imap_host, e.g. SendGrid) tests SMTP alone,
    a read-only bundle tests IMAP alone. Testing SMTP AUTH for real
    matters: a reachability-only probe hides both bad passwords and the
    classic implicit-SSL-on-port-587 misconfig until the first send.
    """
    t0 = time.monotonic()
    username = creds.get("username")
    password = creds.get("password")
    imap_host = (creds.get("imap_host") or creds.get("host") or "").strip()
    smtp_host = (creds.get("smtp_host") or creds.get("host") or "").strip()

    if not (username and password):
        return _fail("Missing username / password.", t0)
    if not imap_host and not smtp_host:
        return _fail("Missing imap_host / smtp_host.", t0)

    parts: list[str] = []
    ok = True

    if imap_host:
        imap_port = int(creds.get("imap_port") or 993)
        use_ssl_imap = bool(creds.get("use_ssl_imap", imap_port == 993))

        def _imap_login() -> None:
            client = smtp_transport.open_imap_client(
                imap_host, imap_port, use_ssl=use_ssl_imap, timeout=10,
            )
            client.login(username, password)
            client.logout()

        try:
            await asyncio.to_thread(_imap_login)
            parts.append(f"IMAP login OK ({imap_host}:{imap_port})")
        except Exception as e:
            ok = False
            parts.append(
                f"IMAP login failed: {e}"
                + _auth_failure_hint(e, host=imap_host, username=username)
            )
    else:
        parts.append("IMAP not configured (send-only account)")

    if smtp_host:
        smtp_port = int(creds.get("smtp_port") or 587)
        use_tls_smtp = bool(creds.get("use_tls_smtp", smtp_port == 587))
        use_ssl_smtp = bool(creds.get("use_ssl_smtp", smtp_port == 465))

        def _smtp_login() -> None:
            with smtp_transport.open_smtp_client(
                smtp_host, smtp_port, use_ssl=use_ssl_smtp, timeout=10,
            ) as s:
                if not use_ssl_smtp:
                    s.ehlo()
                    if use_tls_smtp:
                        s.starttls()
                        s.ehlo()
                s.login(username, password)

        try:
            await asyncio.to_thread(_smtp_login)
            parts.append(f"SMTP login OK ({smtp_host}:{smtp_port})")
        except Exception as e:
            ok = False
            msg = f"SMTP login failed: {e}"
            if use_ssl_smtp and smtp_port == 587:
                msg += (
                    " — port 587 expects STARTTLS, not implicit SSL;"
                    " disable use_ssl_smtp or switch to port 465."
                )
            msg += _auth_failure_hint(e, host=smtp_host, username=username)
            parts.append(msg)
    else:
        parts.append("SMTP not configured (read-only account)")

    detail = "; ".join(parts)
    return _ok(detail, t0) if ok else _fail(detail, t0)


async def test_telegram(creds: dict, wiring_ctx: dict | None = None) -> HealthResult:
    t0 = time.monotonic()
    token = creds.get("bot_token")
    if not token:
        return _fail("No bot_token on record.", t0)
    try:
        resp = await _http_get(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    data = resp.json() if resp.is_success else {}
    if not (resp.is_success and data.get("ok")):
        return _fail(f"Telegram rejected the token: {data.get('description') or resp.status_code}", t0)

    username = (data.get("result") or {}).get("username", "")
    result = _ok(f"bot active as @{username}" if username else "bot active", t0)

    # Wiring sub-check: does Telegram's getWebhookInfo match the URL we'd
    # register? Surfaces "credentials fine but webhook missing" which is
    # the most common "test OK but replies don't happen" failure.
    if wiring_ctx:
        result["wiring"] = await _test_telegram_webhook(token, wiring_ctx)
    return result


async def _test_telegram_webhook(
    token: str, ctx: dict,
) -> dict:
    """ctx: {expected_url: str, channel_config_id?: str}"""
    # Polling mode: if the poller is alive for this bot, inbound already
    # works via long-poll — don't complain about missing webhook.
    try:
        from packages.core.services.channels.telegram_poller import (
            poller, polling_mode_enabled,
        )
        if polling_mode_enabled():
            cc_id = ctx.get("channel_config_id")
            active = cc_id and poller.is_polling(cc_id)
            return {
                "ok": bool(active),
                "detail": (
                    "long-polling active — inbound via getUpdates loop"
                    if active else
                    "polling mode selected but no poll task running yet "
                    "(restart the API or wait 30s for the supervisor to reconcile)"
                ),
                "mode": "polling",
                "configured_url": None,
                "expected_url": None,
            }
    except Exception:
        pass  # fall through to webhook check

    expected = ctx.get("expected_url") or ""
    try:
        resp = await _http_get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
    except Exception as e:
        return {"ok": False, "detail": f"getWebhookInfo failed: {e}", "mode": "webhook"}
    if not resp.is_success:
        return {"ok": False, "detail": f"HTTP {resp.status_code}", "mode": "webhook"}
    info = (resp.json() or {}).get("result") or {}
    configured_url = info.get("url") or ""
    pending = info.get("pending_update_count", 0)
    last_err = info.get("last_error_message") or ""

    if not configured_url:
        return {
            "ok": False,
            "mode": "webhook",
            "detail": (
                "No webhook registered and polling mode is off. Either set "
                "TELEGRAM_MODE=polling, or use an HTTPS PUBLIC_BASE_URL and "
                "re-save the bot to auto-register a webhook."
            ),
            "configured_url": None,
            "expected_url": expected or None,
        }
    # Normalise trailing slashes for compare
    if expected and configured_url.rstrip("/") != expected.rstrip("/"):
        return {
            "ok": False,
            "mode": "webhook",
            "detail": (
                "Webhook URL mismatch. Telegram will deliver inbound messages "
                "somewhere else. Re-save the bot to register the current URL."
            ),
            "configured_url": configured_url,
            "expected_url": expected,
        }
    detail = "webhook registered"
    if last_err:
        detail = f"webhook registered but last delivery failed: {last_err}"
    if pending > 10:
        detail += f" · {pending} pending updates"
    return {
        "ok": not last_err,
        "mode": "webhook",
        "detail": detail,
        "configured_url": configured_url,
        "expected_url": expected or configured_url,
        "pending_update_count": pending,
        "last_error": last_err or None,
    }


async def test_slack(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    token = creds.get("bot_token") or creds.get("access_token")
    if not token:
        return _fail("No Slack bot token on record.", t0)
    try:
        resp = await _http_post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    data = resp.json() if resp.is_success else {}
    if data.get("ok"):
        return _ok(f"auth.test OK — team={data.get('team')}", t0)
    return _fail(f"Slack error: {data.get('error', resp.status_code)}", t0)


async def test_discord(creds: dict, wiring_ctx: dict | None = None) -> HealthResult:
    token = creds.get("bot_token")
    if not token:
        return _fail("No bot_token on record.", time.monotonic())
    t0 = time.monotonic()
    try:
        resp = await _http_get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code != 200:
        return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)

    user = resp.json() or {}
    guild_name = str(creds.get("guild_name") or "").strip()
    guild_id = str(creds.get("guild_id") or "").strip()
    if guild_id:
        try:
            guild_resp = await _http_get(
                f"https://discord.com/api/v10/guilds/{guild_id}",
                headers={"Authorization": f"Bot {token}"},
            )
        except Exception as e:
            return _fail(f"Guild check failed: {e}", t0)
        if guild_resp.status_code != 200:
            return _fail(
                f"Discord Bot cannot access guild {guild_id}: "
                f"HTTP {guild_resp.status_code}",
                t0,
            )
        guild = guild_resp.json() or {}
        guild_name = str(guild.get("name") or guild_name).strip()

    detail = (
        f"bot active as {user.get('username', '')}#{user.get('discriminator', '')}"
    )
    if guild_name:
        detail += f" in {guild_name}"
    result = _ok(
        detail,
        t0,
    )
    if wiring_ctx:
        result["wiring"] = await _test_discord_interactions(token, wiring_ctx)
    return result


async def _test_discord_interactions(token: str, ctx: dict) -> dict:
    """Check that Discord's Interactions Endpoint URL matches what we
    expect. Uses GET /applications/@me — requires Bot auth."""
    expected = ctx.get("expected_url") or ""
    try:
        resp = await _http_get(
            "https://discord.com/api/v10/applications/@me",
            headers={"Authorization": f"Bot {token}"},
        )
    except Exception as e:
        return {"ok": False, "mode": "webhook", "detail": f"applications/@me failed: {e}"}
    if not resp.is_success:
        return {"ok": False, "mode": "webhook", "detail": f"HTTP {resp.status_code}"}

    app = resp.json() or {}
    configured = app.get("interactions_endpoint_url") or ""
    if not configured:
        return {
            "ok": False,
            "mode": "webhook",
            "detail": (
                "Interactions Endpoint URL is not set in the Discord app. "
                "Set it under General Information in discord.com/developers/applications."
            ),
            "configured_url": None,
            "expected_url": expected or None,
        }
    if expected and configured.rstrip("/") != expected.rstrip("/"):
        return {
            "ok": False,
            "mode": "webhook",
            "detail": (
                "Interactions Endpoint URL mismatch. Discord will send pings "
                "somewhere else — update the URL in the developer portal."
            ),
            "configured_url": configured,
            "expected_url": expected,
        }
    return {
        "ok": True,
        "mode": "webhook",
        "detail": "interactions endpoint registered",
        "configured_url": configured,
        "expected_url": expected or configured,
    }


async def test_whatsapp(creds: dict, wiring_ctx: dict | None = None) -> HealthResult:
    t0 = time.monotonic()
    phone_id = creds.get("phone_number_id") or creds.get("phone_id")
    waba_id = creds.get("waba_id") or creds.get("business_account_id")
    token = creds.get("access_token") or creds.get("api_key")
    if not (phone_id and token):
        return _fail("Missing phone_number_id or access_token.", t0)
    try:
        resp = await _http_get(
            f"{_META_BASE}/{phone_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code != 200:
        return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)

    result = _ok("Graph API reachable", t0)
    if wiring_ctx:
        wiring = await _test_whatsapp_subscriptions(
            waba_id, token, wiring_ctx,
        )
        result["wiring"] = wiring
        if wiring.get("ok") is not True:
            result["ok"] = False
            result["reason_code"] = "webhook_not_ready"
            result["detail"] = str(
                wiring.get("detail")
                or "WhatsApp Business webhook subscription is not ready."
            )
    return result


async def _test_whatsapp_subscriptions(
    waba_id: str | None, token: str, ctx: dict,
) -> dict:
    """Check that the WABA is subscribed to an app so inbound messages
    will actually be delivered. Empty ``subscribed_apps`` means Meta has
    nothing to send inbound events to. The subscription belongs to the WABA,
    not an individual phone number.

    WhatsApp's webhook URL itself isn't queryable via the public API, so
    we compare the callback URL configured on the app only when the
    caller provides it; otherwise we just verify a subscription exists.
    """
    if not waba_id:
        return {
            "ok": None,
            "mode": "webhook",
            "detail": (
                "Webhook subscription status unavailable: WhatsApp Business "
                "Account ID (waba_id) is missing."
            ),
        }

    try:
        resp = await _http_get(
            f"{_META_BASE}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        return {"ok": False, "mode": "webhook", "detail": f"subscribed_apps failed: {e}"}
    if not resp.is_success:
        detail = f"HTTP {resp.status_code}: {resp.text[:120]}"
        try:
            error = (resp.json() or {}).get("error") or {}
        except Exception:
            error = {}
        if resp.status_code in (401, 403) or error.get("code") in {10, 100, 200}:
            return {
                "ok": None,
                "mode": "webhook",
                "detail": (
                    "Webhook subscription status unavailable: Meta denied "
                    "WABA access. Assign the app to this WhatsApp Business "
                    "Account and grant whatsapp_business_management."
                ),
                "last_error": detail,
            }
        return {"ok": False, "mode": "webhook", "detail": detail}

    data = resp.json() or {}
    subs = data.get("data") or []
    if not subs:
        return {
            "ok": False,
            "mode": "webhook",
            "detail": (
                "No app subscribed to this WhatsApp Business Account — Meta has nowhere to "
                "deliver inbound messages. Subscribe your app in the WhatsApp "
                "Business Account > Webhooks panel."
            ),
        }
    return {
        "ok": True,
        "mode": "webhook",
        "detail": f"{len(subs)} app(s) subscribed — Meta will deliver inbound",
    }


def _twilio_voice_wiring_status(ctx: dict) -> dict:
    from urllib.parse import urlparse

    public_base_url = str(ctx.get("public_base_url") or "").strip()
    parsed_base_url = urlparse(public_base_url)
    missing: list[str] = []
    if parsed_base_url.scheme != "https" or not parsed_base_url.netloc:
        missing.append("PUBLIC_BASE_URL must be a public HTTPS URL")
    if not ctx.get("channel_config_id"):
        missing.append("Voice channel configuration is missing")
    if not ctx.get("agent_bound"):
        if ctx.get("binding_state") == "ambiguous":
            missing.append("Agent binding is ambiguous")
        else:
            missing.append("Agent binding is missing")
    if not ctx.get("realtime_configured"):
        missing.append(
            str(ctx.get("realtime_error") or "").strip()
            or "Realtime voice model/credential route is unavailable"
        )

    expected_url = str(ctx.get("expected_url") or "").strip() or None
    if missing:
        return {
            "ok": False,
            "mode": "media_stream",
            "detail": "Voice not ready: " + "; ".join(missing),
            "configured_url": None,
            "expected_url": expected_url,
        }
    return {
        "ok": True,
        "mode": "media_stream",
        "detail": "Voice channel, Agent, and Realtime voice route are ready",
        "configured_url": None,
        "expected_url": expected_url,
    }


async def test_twilio(
    creds: dict,
    wiring_ctx: dict | None = None,
) -> HealthResult:
    t0 = time.monotonic()
    sid = creds.get("account_sid")
    token = creds.get("auth_token")
    if not (sid and token):
        return _fail("Missing account_sid / auth_token.", t0)
    try:
        async with httpx.AsyncClient(timeout=10) as client:  # type: ignore[union-attr]
            resp = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                auth=(sid, token),
            )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        result = _ok("account active", t0)
        if wiring_ctx is not None:
            result["wiring"] = _twilio_voice_wiring_status(wiring_ctx)
        return result
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_stripe(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    secret = creds.get("access_token") or creds.get("secret_key") or creds.get("api_key")
    if not secret:
        return _fail("No Stripe secret key on record.", t0)
    try:
        async with httpx.AsyncClient(timeout=10) as client:  # type: ignore[union-attr]
            resp = await client.get(
                "https://api.stripe.com/v1/balance",
                auth=(secret, ""),
            )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        return _ok("API reachable", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_github(creds: dict) -> HealthResult:
    return await _test_with_bearer("GitHub", "https://api.github.com/user", creds.get("access_token", ""))


async def test_robinhood(creds: dict) -> HealthResult:
    """Check the user's official MCP grant without invoking a trading tool."""
    from packages.core.ai.mcp._remote import RemoteMCPClient, RemoteMCPError
    from packages.core.services.robinhood_oauth import ROBINHOOD_MCP_ENDPOINT

    t0 = time.monotonic()
    token = creds.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return _fail("No Robinhood access token on record; connect your account.", t0)
    try:
        client = RemoteMCPClient(ROBINHOOD_MCP_ENDPOINT, token, timeout=10)
        await asyncio.wait_for(client.list_tools(), timeout=15)
    except RemoteMCPError as exc:
        # Do not surface upstream bodies, which can echo account credentials.
        return _fail(f"Robinhood MCP check failed (code {exc.code}); check account access.", t0)
    except Exception:
        return _fail("Robinhood MCP is unreachable; retry the check later.", t0)
    return _ok("Official MCP reachable and tools/list authorized; no trade executed.", t0)


async def test_linkedin(creds: dict) -> HealthResult:
    return await _test_with_bearer("LinkedIn", "https://api.linkedin.com/v2/userinfo", creds.get("access_token", ""))


async def test_notion(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    token = creds.get("access_token")
    if not token:
        return _fail("No access_token on record.", t0)
    try:
        resp = await _http_get(
            "https://api.notion.com/v1/users/me",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
            },
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        return _ok("API reachable", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_quickbooks(creds: dict) -> HealthResult:
    from urllib.parse import quote

    from packages.core.ai.mcp.quickbooks import quickbooks_base_url

    t0 = time.monotonic()
    token = str(creds.get("access_token") or "").strip()
    realm_id = str(creds.get("realm_id") or "").strip()
    if not token:
        return _fail("No QuickBooks token on record.", t0)
    if not realm_id:
        return _fail("QuickBooks Company/Realm ID is missing; reconnect.", t0)
    encoded_realm = quote(realm_id, safe="")
    url = f"{quickbooks_base_url()}/{encoded_realm}/companyinfo/{encoded_realm}"
    try:
        resp = await _http_get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:
        return _fail(f"Network error: {exc}", t0)
    if resp.status_code == 200:
        return _ok("company reachable + authorized", t0)
    if resp.status_code in (401, 403):
        return _fail(f"{resp.status_code} — token rejected; reconnect.", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_twitter_x(creds: dict) -> HealthResult:
    return await _test_with_bearer("Twitter/X", "https://api.x.com/2/users/me", creds.get("access_token", ""))


async def test_wechat_official(
    creds: dict, wiring_ctx: dict | None = None,
) -> HealthResult:
    t0 = time.monotonic()
    app_id = creds.get("app_id")
    app_secret = creds.get("app_secret")
    callback_token = creds.get("token")
    if not (app_id and app_secret and callback_token):
        return _fail("Missing app_id / app_secret / callback token.", t0)
    try:
        resp = await _http_get(
            "https://api.weixin.qq.com/cgi-bin/token"
            f"?grant_type=client_credential&appid={app_id}&secret={app_secret}"
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    data = resp.json() if resp.is_success else {}
    if "access_token" not in data:
        return _fail(
            f"WeChat error: {data.get('errmsg', data.get('errcode', resp.status_code))}",
            t0,
        )

    result = _ok("token refresh OK", t0)
    if wiring_ctx:
        expected_url = str(wiring_ctx.get("expected_url") or "").strip()
        result["wiring"] = {
            "ok": None if expected_url else False,
            "mode": "webhook",
            "detail": (
                "Cannot verify the WeChat callback URL via its API; "
                "verify the exact URL in the Official Account admin panel."
                if expected_url
                else "No WeChat ChannelConfig callback URL is available."
            ),
            "configured_url": None,
            "expected_url": expected_url or None,
        }
    return result


async def test_wechat_personal(
    creds: dict, wiring_ctx: dict | None = None,
) -> HealthResult:
    t0 = time.monotonic()
    base = (creds.get("runner_url") or "").rstrip("/")
    if not base:
        return _fail("No runner_url on record.", t0)
    headers = {}
    if creds.get("bearer_token"):
        headers["Authorization"] = f"Bearer {creds['bearer_token']}"
    session_id = (creds.get("session_id") or "").strip()

    if not session_id:
        try:
            resp = await _http_get(f"{base}/health", headers=headers, timeout=8)
        except Exception as e:
            return _fail(f"Runner unreachable: {e}", t0)
        if resp.status_code == 200:
            data = resp.json() if resp.text else {}
            if data.get("ok"):
                return _fail(
                    "runner reachable, but this integration has no session_id; "
                    "finish the ClawBot QR scan again.",
                    t0,
                )
        return _fail(f"Runner HTTP {resp.status_code}: {resp.text[:120]}", t0)

    try:
        resp = await _http_get(
            f"{base}/sessions/{session_id}/status",
            headers=headers,
            timeout=8,
        )
    except Exception as e:
        return _fail(f"Runner unreachable: {e}", t0)

    if resp.status_code == 200:
        data = resp.json() if resp.text else {}
        if data.get("online"):
            callback_configured = data.get("callback_configured")
            if callback_configured is False:
                result = _fail(
                    "session online, but callback is not registered; "
                    "set PUBLIC_BASE_URL and register wiring again.",
                    t0,
                )
                result["wiring"] = {
                    "ok": False,
                    "detail": "Runner session has no callback URL configured.",
                    "expected_url": (wiring_ctx or {}).get("expected_url"),
                }
                return result
            result = _ok(
                "session online + callback registered"
                if callback_configured else "session online",
                t0,
            )
            if wiring_ctx or callback_configured is True:
                result["wiring"] = {
                    "ok": True if callback_configured is True else None,
                    "detail": (
                        "Runner callback registered."
                        if callback_configured is True
                        else "Runner did not report callback state."
                    ),
                    "expected_url": (wiring_ctx or {}).get("expected_url"),
                }
            return result

        bits = []
        if data.get("qr_pending"):
            bits.append("QR pending")
        if data.get("last_error"):
            bits.append(str(data["last_error"]))
        detail = "; ".join(bits) or "session not online"
        return _fail(detail, t0)

    if resp.status_code == 404:
        return _fail(
            f"Runner has no session {session_id!r}; runner restarted or the "
            "session was deleted. Re-scan the ClawBot QR.",
            t0,
        )
    if resp.status_code == 410:
        return _fail(
            "Runner endpoint returned 410; backend/runner API versions are mismatched.",
            t0,
        )
    return _fail(f"Runner HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_webhook(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    url = creds.get("url")
    if not url:
        return _fail("No webhook URL on record.", t0)
    try:
        async with httpx.AsyncClient(timeout=8) as client:  # type: ignore[union-attr]
            # HEAD may not be supported; fall back to OPTIONS
            resp = await client.head(url)
            if resp.status_code == 405:
                resp = await client.options(url)
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code in (401, 403):
        result = _fail(f"HTTP {resp.status_code}: webhook credentials rejected", t0)
        result["reason_code"] = "credentials_rejected"
        return result
    if resp.status_code == 404:
        result = _fail("HTTP 404: webhook endpoint not found", t0)
        result["reason_code"] = "webhook_endpoint_not_found"
        return result
    if 400 <= resp.status_code < 500:
        result = _fail(f"HTTP {resp.status_code}: webhook endpoint rejected request", t0)
        result["reason_code"] = "webhook_endpoint_rejected"
        return result
    if resp.status_code < 500:
        return _ok(f"HTTP {resp.status_code}", t0)
    return _fail(f"HTTP {resp.status_code}", t0)


# ── AI generation / research providers ────────────────────────────────────

async def test_replicate(creds: dict) -> HealthResult:
    """Replicate auth = `Authorization: Token <key>`. Hit /v1/account
    which is the cheapest authenticated endpoint."""
    t0 = time.monotonic()
    token = creds.get("api_key", "")
    if not token:
        return _fail("No Replicate API token on record.", t0)
    try:
        resp = await _http_get(
            "https://api.replicate.com/v1/account",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        return _ok("Replicate token valid", t0)
    if resp.status_code in (401, 403):
        # Replicate's body usually says "You did not pass a valid
        # authentication token" — surface that verbatim so the user
        # knows it isn't a typo on our side.
        try:
            api_msg = resp.json().get("detail") or resp.text[:120]
        except Exception:
            api_msg = resp.text[:120]
        return _fail(
            f"{resp.status_code} {api_msg}. Get a fresh token at "
            "replicate.com/account/api-tokens (format: r8_<32 hex chars>).",
            t0,
        )
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_elevenlabs(creds: dict) -> HealthResult:
    """ElevenLabs uses xi-api-key header. /v1/user returns the
    subscription tier — fast + auth-validating."""
    t0 = time.monotonic()
    api_key = creds.get("api_key", "")
    if not api_key:
        return _fail("No ElevenLabs API key on record.", t0)
    try:
        resp = await _http_get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=10,
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        try:
            tier = resp.json().get("subscription", {}).get("tier", "free")
            return _ok(f"ElevenLabs key valid (tier: {tier})", t0)
        except Exception:
            return _ok("ElevenLabs key valid", t0)
    if resp.status_code in (401, 403):
        return _fail(f"{resp.status_code} — key rejected.", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_tavily(creds: dict) -> HealthResult:
    """Validate Tavily credentials without consuming search credits."""
    t0 = time.monotonic()
    raw_api_key = creds.get("api_key", "")
    api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else ""
    if not api_key:
        return _fail("No Tavily API key on record.", t0)
    if not api_key.startswith("tvly-"):
        return _fail("Tavily API key must start with 'tvly-'.", t0)
    try:
        resp = await _http_get(
            "https://api.tavily.com/usage",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except Exception as exc:
        return _fail(f"Network error: {exc}", t0)
    if resp.status_code == 200:
        return _ok("Tavily key valid", t0)
    if resp.status_code in (401, 403):
        return _fail(f"{resp.status_code} - key rejected; reconnect.", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_jimeng(creds: dict) -> HealthResult:
    """Jimeng goes through the iptag/jimeng-api gateway sidecar. We
    can't validate the sessionid without making a real generation
    request, so we just verify the gateway is up and the sessionid
    format looks plausible. Real validation happens on first call."""
    t0 = time.monotonic()
    sessionid = creds.get("api_key", "")
    if not sessionid:
        return _fail("No Jimeng sessionid on record.", t0)
    if len(sessionid) < 20:
        return _fail("sessionid too short — paste the full cookie value.", t0)
    import os
    gateway = os.environ.get("JIMENG_API_URL", "http://jimeng-api:5100").rstrip("/")
    try:
        resp = await _http_get(f"{gateway}/", timeout=5)
    except Exception:
        return _fail(
            "Jimeng gateway unreachable. Start it with "
            "`docker compose --profile jimeng up -d jimeng-api`.",
            t0,
        )
    if resp.status_code < 500:
        return _ok(f"Gateway up ({resp.status_code}); sessionid validation deferred to first call.", t0)
    return _fail(f"Gateway HTTP {resp.status_code}", t0)


# ── Registry ────────────────────────────────────────────────────────────────

_TESTS: Dict[str, Callable[[dict], Awaitable[HealthResult]]] = {
    "gmail":            test_google_userinfo,
    "google_calendar":  test_google_userinfo,
    "google_drive":     test_google_userinfo,
    "youtube":          test_youtube,
    "outlook":          test_outlook,
    "facebook":         test_facebook,
    "email":            test_email_login,
    "telegram":         test_telegram,
    "slack":            test_slack,
    "discord":          test_discord,
    "whatsapp":         test_whatsapp,
    "twilio":           test_twilio,
    "stripe":           test_stripe,
    "robinhood":        test_robinhood,
    "github":           test_github,
    "linkedin":         test_linkedin,
    "notion":           test_notion,
    "quickbooks":       test_quickbooks,
    "twitter_x":        test_twitter_x,
    "wechat_official":  test_wechat_official,
    "wechat_personal":  test_wechat_personal,
    "webhook":          test_webhook,
    "replicate":        test_replicate,
    "elevenlabs":       test_elevenlabs,
    "tavily":           test_tavily,
    "jimeng":           test_jimeng,
}

for _provider_key, _health_checker in _TESTS.items():
    register_health_checker(_provider_key, _health_checker)


async def run_test(
    provider: str,
    credentials: dict,
    *,
    wiring_ctx: dict | None = None,
) -> HealthResult:
    """Dispatch to the right test without conflating unsupported and unknown.

    ``wiring_ctx`` gives providers with an inbound webhook extra context
    (e.g. the expected callback URL) so they can run a second sub-check
    that verifies the upstream actually knows how to reach us.
    """
    fn = health_checker_for(provider)
    if not fn:
        canonical_key = canonical_integration_key(provider)
        known_provider = get_integration_spec(canonical_key) is not None
        return {
            "ok": None,
            "monitoring_status": "unsupported" if known_provider else "unknown_provider",
            "provider_key": canonical_key,
            "detail": (
                f"No health check is available for '{canonical_key}'."
                if known_provider
                else f"Unknown integration provider '{canonical_key}'."
            ),
            "latency_ms": 0.0,
            "checked_at": _now_iso(),
        }
    try:
        # Only providers that opt in accept wiring_ctx; others ignore it.
        try:
            return await fn(credentials or {}, wiring_ctx=wiring_ctx)  # type: ignore[call-arg]
        except TypeError:
            return await fn(credentials or {})
    except Exception as e:
        logger.exception("Health check crashed for %s", provider)
        return {
            "ok": False,
            "detail": f"Test crashed: {e}",
            "latency_ms": 0.0,
            "checked_at": _now_iso(),
        }


def _build_wiring_context(provider: str, integration_id: str) -> dict:
    """Build the extra context a provider needs to validate its inbound
    wiring. Keyed by provider. Returns empty dict for providers that
    don't need it — the test function will simply skip the sub-check.
    """
    from packages.core.config import get_settings
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")

    if provider == "telegram":
        # Expected URL mirrors TelegramChannelAdapter.webhook_path but we
        # compute from the ChannelConfig row in the persist helper. For
        # now callers pass the full expected URL directly.
        return {"public_base_url": base}
    return {}


# ── Persistence helpers ────────────────────────────────────────────────────


def _whatsapp_readiness_checks() -> dict[str, dict[str, object]]:
    return {
        key: {"ok": None, "detail": "Not checked."}
        for key in (
            "oauth",
            "assets",
            "phone_registration",
            "app_subscription",
            "callback",
        )
    }


def _whatsapp_readiness_result(
    *,
    code: WhatsAppAccountReadinessCode,
    detail: str,
    integration_id: str,
    channel_config_id: str | None,
    phone_number_id: str | None,
    waba_id: str | None,
    checks: dict[str, dict[str, object]],
    started_at: float,
) -> HealthResult:
    return {
        "ok": code is WhatsAppAccountReadinessCode.READY,
        "reason_code": code.value,
        "detail": detail,
        "integration_id": integration_id,
        "channel_config_id": channel_config_id,
        "phone_number_id": phone_number_id,
        "waba_id": waba_id,
        "checks": checks,
        "latency_ms": round((time.monotonic() - started_at) * 1000, 1),
        "checked_at": _now_iso(),
    }


async def _test_whatsapp_account_readiness(
    db,
    integration_row,
    leased_creds: dict,
    resolved_creds: dict,
    wiring_ctx: dict | None,
) -> HealthResult:
    """Prove provider account readiness without consulting Agent Binding."""
    started_at = time.monotonic()
    checks = _whatsapp_readiness_checks()
    config = integration_row.config if isinstance(integration_row.config, dict) else {}
    nango = config.get("nango") if isinstance(config.get("nango"), dict) else {}
    whatsapp = (
        config.get("whatsapp")
        if isinstance(config.get("whatsapp"), dict)
        else {}
    )
    channel_config_id = str((wiring_ctx or {}).get("channel_config_id") or "").strip() or None
    phone_number_id = str(whatsapp.get("phone_number_id") or "").strip() or None
    waba_id = str(whatsapp.get("waba_id") or "").strip() or None

    provider_config_key = str(nango.get("provider_config_key") or "").strip()
    connection_id = str(nango.get("connection_id") or "").strip()
    leased_provider_key = str(leased_creds.get("provider_config_key") or "").strip()
    leased_connection_id = str(leased_creds.get("connection_id") or "").strip()
    oauth_exact = (
        leased_creds.get("via") == "nango"
        and bool(provider_config_key and connection_id)
        and leased_provider_key == provider_config_key
        and leased_connection_id == connection_id
        and bool(resolved_creds.get("access_token"))
    )
    if not oauth_exact:
        detail = "The exact WhatsApp Nango connection or OAuth token is unavailable."
        checks["oauth"] = {"ok": False, "detail": detail}
        return _whatsapp_readiness_result(
            code=WhatsAppAccountReadinessCode.OAUTH_MISSING,
            detail=detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )
    checks["oauth"] = {
        "ok": True,
        "detail": "Exact Nango OAuth connection is available.",
    }

    resolved_phone_id = str(resolved_creds.get("phone_number_id") or "").strip()
    resolved_waba_id = str(resolved_creds.get("waba_id") or "").strip()
    channel_phone_id = str(
        (wiring_ctx or {}).get("channel_phone_number_id") or ""
    ).strip()
    assets_exact = (
        bool(channel_config_id and phone_number_id and waba_id)
        and channel_phone_id == phone_number_id
        and (not resolved_phone_id or resolved_phone_id == phone_number_id)
        and (not resolved_waba_id or resolved_waba_id == waba_id)
    )
    if not assets_exact:
        detail = "The connected WABA or phone asset does not match the Manor account."
        checks["assets"] = {"ok": False, "detail": detail}
        return _whatsapp_readiness_result(
            code=WhatsAppAccountReadinessCode.ASSET_MISMATCH,
            detail=detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )
    checks["assets"] = {
        "ok": True,
        "detail": "WABA and phone assets match this account.",
    }

    try:
        from packages.core.ai.mcp.nango import get_nango_secret
        from packages.core.services.whatsapp_business_config import (
            load_whatsapp_business_config,
        )
        from packages.core.services.whatsapp_business_provisioning import (
            WhatsAppPhoneState,
            inspect_whatsapp_app_callback,
            inspect_whatsapp_business_number,
        )

        nango_secret = await get_nango_secret(db, integration_row.entity_id)
        deployment = load_whatsapp_business_config()
        if not nango_secret:
            raise RuntimeError("Nango is unavailable")
        provisioning = await inspect_whatsapp_business_number(
            nango_secret=nango_secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            expected_app_id=deployment.app_id,
        )
    except Exception:
        logger.warning(
            "WhatsApp readiness provider inspection failed for integration=%s",
            integration_row.id,
            exc_info=True,
        )
        detail = "WhatsApp Business provider readiness is temporarily unavailable."
        checks["phone_registration"] = {"ok": False, "detail": detail}
        return _whatsapp_readiness_result(
            code=WhatsAppAccountReadinessCode.PROVIDER_UNAVAILABLE,
            detail=detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )

    if provisioning.phone_state is not WhatsAppPhoneState.CONNECTED:
        if provisioning.phone_state is WhatsAppPhoneState.REGISTRATION_REQUIRED:
            code = WhatsAppAccountReadinessCode.PHONE_NOT_REGISTERED
            detail = "The WhatsApp Business phone number is not registered."
        else:
            code = WhatsAppAccountReadinessCode.PROVIDER_UNAVAILABLE
            detail = "Meta did not return a known phone registration state."
        checks["phone_registration"] = {"ok": False, "detail": detail}
        return _whatsapp_readiness_result(
            code=code,
            detail=detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )
    checks["phone_registration"] = {
        "ok": True,
        "detail": "Phone number is registered with Cloud API.",
    }

    if not provisioning.exact_app_subscribed:
        detail = "This deployment's Meta App is not subscribed to the WABA."
        checks["app_subscription"] = {"ok": False, "detail": detail}
        return _whatsapp_readiness_result(
            code=WhatsAppAccountReadinessCode.APP_NOT_SUBSCRIBED,
            detail=detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )
    checks["app_subscription"] = {
        "ok": True,
        "detail": "This deployment's Meta App is subscribed to the WABA.",
    }

    try:
        callback = await inspect_whatsapp_app_callback(
            app_id=deployment.app_id,
            app_secret=deployment.app_secret,
            expected_callback_url=deployment.callback_url,
        )
    except Exception:
        logger.warning(
            "WhatsApp callback inspection failed for integration=%s",
            integration_row.id,
            exc_info=True,
        )
        detail = "WhatsApp Business callback readiness is temporarily unavailable."
        checks["callback"] = {"ok": False, "detail": detail}
        return _whatsapp_readiness_result(
            code=WhatsAppAccountReadinessCode.PROVIDER_UNAVAILABLE,
            detail=detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )

    checks["callback"] = {
        "ok": callback.ok,
        "detail": callback.detail,
        "configured_url": callback.configured_url,
        "expected_url": callback.expected_url,
    }
    if not callback.ok:
        return _whatsapp_readiness_result(
            code=WhatsAppAccountReadinessCode.CALLBACK_NOT_READY,
            detail=callback.detail,
            integration_id=integration_row.id,
            channel_config_id=channel_config_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            checks=checks,
            started_at=started_at,
        )
    return _whatsapp_readiness_result(
        code=WhatsAppAccountReadinessCode.READY,
        detail="WhatsApp Business account is ready.",
        integration_id=integration_row.id,
        channel_config_id=channel_config_id,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        checks=checks,
        started_at=started_at,
    )


async def _resolve_nango_runtime_credentials(
    db,
    integration_row,
    leased_creds: dict,
) -> dict:
    """Resolve a Nango indirection to a fresh provider token just in time."""
    if (leased_creds or {}).get("via") != "nango":
        return leased_creds or {}

    provider_config_key = (
        (leased_creds or {}).get("provider_config_key")
        or (((integration_row.config or {}).get("nango") or {}).get("provider_config_key"))
        or integration_row.provider
        or ""
    )
    connection_id = (
        (leased_creds or {}).get("connection_id")
        or (((integration_row.config or {}).get("nango") or {}).get("connection_id"))
        or ""
    )
    if not provider_config_key or not connection_id:
        return leased_creds or {}

    try:
        from packages.core.ai.mcp.nango import _NANGO_BASE, get_nango_secret

        secret = await get_nango_secret(db, integration_row.entity_id)
        if not secret:
            return leased_creds or {}

        assert httpx is not None, "httpx required"
        async with httpx.AsyncClient(timeout=15.0) as cx:
            r = await cx.get(
                f"{_NANGO_BASE}/connection/{connection_id}",
                params={"provider_config_key": provider_config_key},
                headers={"Authorization": f"Bearer {secret}"},
            )
            r.raise_for_status()
            body = r.json() or {}

        creds_block = body.get("credentials") or {}
        access_token = (
            creds_block.get("access_token")
            or creds_block.get("api_key")
            or body.get("access_token")
        )
        if not access_token:
            return leased_creds or {}

        resolved = dict(leased_creds or {})
        resolved["access_token"] = access_token
        if str(getattr(integration_row, "provider", "")).lower() in {
            "whatsapp", "whatsapp_cloud",
        }:
            for source in (
                creds_block,
                body,
                body.get("metadata") if isinstance(body, dict) else None,
                body.get("profile") if isinstance(body, dict) else None,
            ):
                if not isinstance(source, dict):
                    continue
                phone_id = source.get("phone_number_id") or source.get("phone_id")
                if phone_id and not resolved.get("phone_number_id"):
                    resolved["phone_number_id"] = phone_id
                waba_id = source.get("waba_id") or source.get("business_account_id")
                if waba_id and not resolved.get("waba_id"):
                    resolved["waba_id"] = waba_id
                if resolved.get("phone_number_id") and resolved.get("waba_id"):
                    break
            if not resolved.get("phone_number_id"):
                config = getattr(integration_row, "config", {})
                whatsapp_config = (
                    config.get("whatsapp")
                    if isinstance(config, dict)
                    else None
                )
                if isinstance(whatsapp_config, dict):
                    phone_id = (
                        whatsapp_config.get("phone_number_id")
                        or whatsapp_config.get("phone_id")
                    )
                    if phone_id:
                        resolved["phone_number_id"] = phone_id
            if not resolved.get("waba_id"):
                config = getattr(integration_row, "config", {})
                whatsapp_config = (
                    config.get("whatsapp")
                    if isinstance(config, dict)
                    else None
                )
                if isinstance(whatsapp_config, dict):
                    waba_id = (
                        whatsapp_config.get("waba_id")
                        or whatsapp_config.get("business_account_id")
                    )
                    if waba_id:
                        resolved["waba_id"] = waba_id
            from packages.core.services.whatsapp_business_config import (
                load_whatsapp_business_config,
            )

            deployment = load_whatsapp_business_config()
            resolved["app_id"] = deployment.app_id
            resolved["app_secret"] = deployment.app_secret
            resolved["verify_token"] = deployment.verify_token
            resolved["callback_url"] = deployment.callback_url
        return resolved
    except Exception:
        logger.debug("Nango runtime credential resolution failed", exc_info=True)
        return leased_creds or {}


async def run_and_persist_integration(db, integration_id: str) -> HealthResult:
    """Run the health check for an ``Integration`` row and stash the
    result into its ``config.last_health_check`` field.

    Credentials route through CredentialService so we transparently
    handle both legacy plaintext rows and the modern vault_transit
    scheme — reading ``row.credentials`` directly would miss every
    Integration created since the Vault rollout.
    """
    from sqlalchemy import select
    from packages.core.models.document import Integration
    from packages.core.credentials import (
        CredentialDecryptError,
        get_credential_service,
        Requester,
    )

    row = (await db.execute(
        select(Integration).where(Integration.id == integration_id)
    )).scalar_one_or_none()
    if not row:
        return {"ok": False, "detail": "integration not found", "latency_ms": 0.0, "checked_at": _now_iso()}

    if str(getattr(row, "status", "active") or "active").lower() != "active":
        return {
            "ok": False,
            "reason_code": "disabled",
            "detail": "Integration is disabled.",
            "latency_ms": 0.0,
            "checked_at": _now_iso(),
        }

    try:
        creds = get_credential_service().lease_integration(
            row,
            requester=Requester(kind="system", id=f"health_check:{integration_id}"),
            reason="integration_health.run_and_persist_integration",
        )
    except CredentialDecryptError as exc:
        # Expected, operator-actionable state: the stored ciphertext can no
        # longer be decrypted (e.g. the credential predates a Vault transit
        # key change). This recurs on every health tick, so log a single line
        # instead of a full traceback and flag the integration for reconnect.
        logger.warning(
            "Health check %s: stored credentials could not be decrypted (%s); needs reconnect",
            integration_id, exc,
        )
        return {
            "ok": False,
            "detail": "Stored credentials could not be decrypted; reconnect this integration.",
            "needs_reconnect": True,
            "latency_ms": 0.0,
            "checked_at": _now_iso(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to lease credentials for health check %s", integration_id)
        return {
            "ok": False,
            "detail": f"Credential lease failed: {exc}",
            "latency_ms": 0.0,
            "checked_at": _now_iso(),
        }

    resolved_creds = await _resolve_nango_runtime_credentials(db, row, creds or {})
    wiring_ctx = await _wiring_ctx_for_integration(db, row)
    provider_key = canonical_integration_key(row.provider)
    # A persisted connection is a known Integration identity even when an
    # extension/Nango provider has no first-party checker.
    register_integration(provider_key)
    if provider_key == "whatsapp":
        result = await _test_whatsapp_account_readiness(
            db,
            row,
            creds or {},
            resolved_creds or {},
            wiring_ctx,
        )
    else:
        result = await run_test(
            row.provider, resolved_creds or {}, wiring_ctx=wiring_ctx,
        )

    nango_meta = (
        (row.config or {}).get("nango")
        if isinstance(row.config, dict)
        else None
    )
    if isinstance(nango_meta, dict) and provider_key != "whatsapp":
        result["reason_code"] = classify_nango_health(
            ok=result.get("ok"),
            detail=result.get("detail"),
            provider_config_present=bool(
                nango_meta.get("provider_config_key")
                and nango_meta.get("connection_id")
            ),
            webhook_configured=nango_meta.get("webhook_configured"),
        )

    cfg = dict(row.config or {})
    if provider_key == "whatsapp":
        whatsapp = dict(cfg.get("whatsapp") or {})
        readiness_code = str(result.get("reason_code") or "provider_unavailable")
        if readiness_code == WhatsAppAccountReadinessCode.READY.value:
            provisioning_status = "ready"
        elif readiness_code == WhatsAppAccountReadinessCode.PHONE_NOT_REGISTERED.value:
            provisioning_status = "registration_required"
        else:
            provisioning_status = "failed"
        whatsapp["readiness_code"] = readiness_code
        whatsapp["provisioning_status"] = provisioning_status
        whatsapp["subscription_status"] = (
            "ready" if provisioning_status == "ready" else "failed"
        )
        if provisioning_status in {"ready", "registration_required"}:
            whatsapp.pop("provisioning_error", None)
            whatsapp.pop("subscription_error", None)
        else:
            whatsapp["provisioning_error"] = str(result.get("detail") or "")
            whatsapp["subscription_error"] = str(result.get("detail") or "")
        cfg["whatsapp"] = whatsapp
    cfg["last_health_check"] = result
    row.config = cfg
    await db.flush()
    return result


_PROVIDERS_WITH_WIRING: set[str] = {
    "telegram", "discord", "whatsapp", "twilio", "wechat_official",
    "wechat_personal",
}


async def _wiring_ctx_for_integration(db, integration_row) -> dict | None:
    """Resolve the expected inbound URL for providers that have one."""
    raw_provider = integration_row.provider
    provider = canonical_integration_key(raw_provider)
    if provider not in _PROVIDERS_WITH_WIRING:
        return None

    from sqlalchemy import select
    from packages.core.models.channel import ChannelConfig
    from packages.core.config import get_settings

    if provider == "wechat_official":
        channel_type = "wechat"
    elif provider == "twilio":
        channel_type = "twilio_voice"
    else:
        channel_type = provider
    cc = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == integration_row.entity_id,
            ChannelConfig.channel_type == channel_type,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_row.id,
        )
    )).scalar_one_or_none()
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    if not cc:
        if provider == "twilio":
            return {
                "public_base_url": base,
                "channel_config_id": None,
                "agent_bound": False,
                "binding_state": "missing",
                "realtime_configured": False,
                "expected_url": "",
            }
        context = {"expected_url": "", "channel_config_id": None}
        if provider == "whatsapp":
            context["channel_phone_number_id"] = None
        return context
    if provider == "wechat_official" and not base:
        return {"expected_url": "", "channel_config_id": cc.id}

    if provider == "twilio":
        from packages.core.services.channel_bindings import (
            load_channel_binding_scopes_for_config,
        )

        binding_scopes = await load_channel_binding_scopes_for_config(db, cc)
        callable_scopes = [scope for scope in binding_scopes if scope.agent_id]
        if len(callable_scopes) == 1:
            binding_state = "ready"
        elif len(callable_scopes) > 1:
            binding_state = "ambiguous"
        elif binding_scopes:
            binding_state = "invalid"
        else:
            binding_state = "missing"

        realtime_error: str | None = None
        try:
            from packages.core.services.voice.realtime import resolve_realtime_route

            await resolve_realtime_route(
                integration_row.entity_id,
                user_id=integration_row.owner_user_id,
            )
            realtime_configured = True
        except Exception as exc:
            logger.debug(
                "Twilio Realtime route lookup failed for entity=%s",
                integration_row.entity_id,
                exc_info=True,
            )
            realtime_configured = False
            realtime_error = str(exc).strip() or None

        expected = (
            f"{base}/api/v1/channels/twilio/voice?config_id={cc.id}"
            if base
            else ""
        )
        context = {
            "public_base_url": base,
            "channel_config_id": cc.id,
            "agent_bound": binding_state == "ready",
            "binding_state": binding_state,
            "realtime_configured": realtime_configured,
            "expected_url": expected,
        }
        if realtime_error:
            context["realtime_error"] = realtime_error
        return context
    if provider == "telegram":
        import hashlib
        from packages.core.services.channel_credentials import lease_channel_credentials

        try:
            credentials = await lease_channel_credentials(
                db, cc, reason="integration_health.telegram_wiring",
            )
        except ValueError:
            return {"expected_url": "", "channel_config_id": cc.id}
        token = credentials.get("bot_token", "")
        bot_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
        expected = f"{base}/api/v1/channels/telegram/webhook/{bot_hash}?config_id={cc.id}"
    elif provider == "discord":
        expected = f"{base}/api/v1/channels/discord/callback?config_id={cc.id}"
    elif provider == "wechat_personal":
        expected = f"{base}/api/v1/channels/wechat_personal/callback?config_id={cc.id}"
    elif provider == "wechat_official":
        expected = f"{base}/api/v1/channels/wechat/callback?config_id={cc.id}"
    elif provider == "whatsapp":
        from packages.core.services.whatsapp_business_config import (
            load_whatsapp_business_config,
        )

        expected = load_whatsapp_business_config().callback_url
    else:
        expected = ""

    context = {"expected_url": expected, "channel_config_id": cc.id}
    if provider == "whatsapp":
        context["channel_phone_number_id"] = (
            str(getattr(cc, "whatsapp_phone_number_id", "") or "").strip()
            or None
        )
    return context


async def run_and_persist_oauth(db, oauth_account_id: str) -> HealthResult:
    """Run the health check for an ``OAuthAccount`` row and stash into
    its ``profile.last_health_check``."""
    from sqlalchemy import select
    from packages.core.models.user import OAuthAccount
    from packages.core.credentials import CredentialDecryptError
    from packages.core.services.oauth_account_credentials import (
        lease_oauth_account_tokens,
        mark_oauth_account_credential_reconnect_required,
    )

    row = (await db.execute(
        select(OAuthAccount).where(OAuthAccount.id == oauth_account_id)
    )).scalar_one_or_none()
    if not row:
        return {"ok": False, "detail": "oauth account not found", "latency_ms": 0.0, "checked_at": _now_iso()}

    # Lease only stored token credentials here. Provider-specific profile
    # metadata is merged explicitly below so arbitrary profile fields never
    # become credential material.
    try:
        creds = lease_oauth_account_tokens(
            row,
            requester_id="integration_health",
            reason="oauth.integration_health",
        )
    except CredentialDecryptError:
        result = mark_oauth_account_credential_reconnect_required(
            row,
            provider=canonical_integration_key(row.provider),
            checked_at=_now_iso(),
        )
        await db.flush()
        logger.warning(
            "OAuth health check %s: stored credentials could not be decrypted; "
            "marked for reconnect",
            oauth_account_id,
        )
        return {**result, "needs_reconnect": True}

    provider_key = canonical_integration_key(row.provider)
    if provider_key == "quickbooks":
        profile = row.profile if isinstance(row.profile, dict) else {}
        realm_id = str(profile.get("realm_id") or "").strip()
        if realm_id:
            creds = {**(creds or {}), "realm_id": realm_id}

    wiring_ctx = None
    if provider_key == "discord":
        from packages.core.config import get_settings
        from packages.core.services.discord_app_config import (
            resolve_discord_app_config,
        )

        app = await resolve_discord_app_config(db)
        profile = row.profile if isinstance(row.profile, dict) else {}
        profile_application_id = str(profile.get("application_id") or "").strip()
        guild_id = str(profile.get("guild_id") or "").strip()
        if app is not None and profile_application_id == app.application_id:
            creds = {
                **(creds or {}),
                "bot_token": app.bot_token,
                "application_id": app.application_id,
                "guild_id": guild_id,
                "guild_name": str(profile.get("guild_name") or "").strip(),
            }
        base = get_settings().PUBLIC_BASE_URL.rstrip("/")
        wiring_ctx = {
            "expected_url": f"{base}/api/v1/channels/discord/interactions",
        }

    register_integration(provider_key)
    result = await run_test(row.provider, creds, wiring_ctx=wiring_ctx)

    profile = dict(row.profile or {})
    profile["last_health_check"] = result
    row.profile = profile
    await db.flush()
    return result


# Unused — re-export for callers that want to serialise the HealthResult
_ = json
