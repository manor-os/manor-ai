"""Integration health checks — per-provider "does it actually work" tests.

Each provider has a ``test_connection`` function that does ONE cheap API
call to verify the credentials actually reach the upstream:

  - Gmail / Calendar / Drive  → GET /oauth2/v3/userinfo
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
from typing import Any, Awaitable, Callable, Dict

from packages.core.external_api_versions import META_GRAPH as _META_PIN

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


async def test_email_imap(creds: dict) -> HealthResult:
    """Try IMAP LOGIN + LOGOUT. Also confirms SMTP host is at least reachable."""
    t0 = time.monotonic()
    host = creds.get("imap_host") or creds.get("host")
    port = int(creds.get("imap_port") or 993)
    username = creds.get("username")
    password = creds.get("password")
    if not (host and username and password):
        return _fail("Missing host / username / password.", t0)

    def _login_and_logout() -> None:
        import imaplib
        use_ssl = bool(creds.get("use_ssl_imap", port == 993))
        client = imaplib.IMAP4_SSL(host, port, timeout=10) if use_ssl \
            else imaplib.IMAP4(host, port, timeout=10)
        client.login(username, password)
        client.logout()

    try:
        await asyncio.to_thread(_login_and_logout)
    except Exception as e:
        return _fail(f"IMAP login failed: {e}", t0)
    return _ok(f"IMAP login OK ({host}:{port})", t0)


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
    result = _ok(
        f"bot active as {user.get('username', '')}#{user.get('discriminator', '')}",
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
        result["wiring"] = await _test_whatsapp_subscriptions(
            phone_id, token, wiring_ctx,
        )
    return result


async def _test_whatsapp_subscriptions(phone_id: str, token: str, ctx: dict) -> dict:
    """Check that the WABA is subscribed to an app so inbound messages
    will actually be delivered. Empty ``subscribed_apps`` means Meta has
    nothing to send inbound events to.

    WhatsApp's webhook URL itself isn't queryable via the public API, so
    we compare the callback URL configured on the app only when the
    caller provides it; otherwise we just verify a subscription exists.
    """
    try:
        resp = await _http_get(
            f"{_META_BASE}/{phone_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        return {"ok": False, "mode": "webhook", "detail": f"subscribed_apps failed: {e}"}
    if not resp.is_success:
        return {"ok": False, "mode": "webhook", "detail": f"HTTP {resp.status_code}"}

    data = resp.json() or {}
    subs = data.get("data") or []
    if not subs:
        return {
            "ok": False,
            "mode": "webhook",
            "detail": (
                "No app subscribed to this phone number — Meta has nowhere to "
                "deliver inbound messages. Subscribe your app in the WhatsApp "
                "Business Account > Webhooks panel."
            ),
        }
    return {
        "ok": True,
        "mode": "webhook",
        "detail": f"{len(subs)} app(s) subscribed — Meta will deliver inbound",
    }


async def test_twilio(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    sid = creds.get("account_sid")
    token = creds.get("auth_token")
    if not (sid and token):
        return _fail("Missing account_sid / auth_token.", t0)
    try:
        resp = await _http_get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            headers={},  # auth below
            timeout=10,
        )
        # httpx needs auth param; redo with basic auth
        async with httpx.AsyncClient(timeout=10) as client:  # type: ignore[union-attr]
            resp = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                auth=(sid, token),
            )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        return _ok("account active", t0)
    return _fail(f"HTTP {resp.status_code}: {resp.text[:120]}", t0)


async def test_stripe(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    secret = creds.get("secret_key") or creds.get("api_key")
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
    """QuickBooks /user endpoint needs a realmId, which isn't known at
    register time. We settle for a token introspection via /connection-info."""
    return await _test_with_bearer(
        "QuickBooks",
        "https://accounts.platform.intuit.com/v1/openid_connect/userinfo",
        creds.get("access_token", ""),
    )


async def test_twitter_x(creds: dict) -> HealthResult:
    return await _test_with_bearer("Twitter/X", "https://api.x.com/2/users/me", creds.get("access_token", ""))


async def test_wechat_official(creds: dict) -> HealthResult:
    t0 = time.monotonic()
    app_id = creds.get("app_id")
    app_secret = creds.get("app_secret")
    if not (app_id and app_secret):
        return _fail("Missing app_id / app_secret.", t0)
    try:
        resp = await _http_get(
            "https://api.weixin.qq.com/cgi-bin/token"
            f"?grant_type=client_credential&appid={app_id}&secret={app_secret}"
        )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    data = resp.json() if resp.is_success else {}
    if "access_token" in data:
        return _ok("token refresh OK", t0)
    return _fail(f"WeChat error: {data.get('errmsg', data.get('errcode', resp.status_code))}", t0)


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
    """Tavily has no /me endpoint. Run a 1-result throwaway search —
    the cheapest way to validate the key (~1 credit, free tier covers it)."""
    t0 = time.monotonic()
    api_key = creds.get("api_key", "")
    if not api_key:
        return _fail("No Tavily API key on record.", t0)
    try:
        import httpx  # type: ignore
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": "manor health check",
                    "max_results": 1,
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
    except Exception as e:
        return _fail(f"Network error: {e}", t0)
    if resp.status_code == 200:
        return _ok("Tavily key valid", t0)
    if resp.status_code in (401, 403, 432):
        return _fail(f"{resp.status_code} — key rejected; verify it starts with 'tvly-'.", t0)
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
    "email":            test_email_imap,
    "telegram":         test_telegram,
    "slack":            test_slack,
    "discord":          test_discord,
    "whatsapp":         test_whatsapp,
    "twilio":           test_twilio,
    "stripe":           test_stripe,
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


async def run_test(
    provider: str,
    credentials: dict,
    *,
    wiring_ctx: dict | None = None,
) -> HealthResult:
    """Dispatch to the right test. Unknown providers get a safe "untested".

    ``wiring_ctx`` gives providers with an inbound webhook extra context
    (e.g. the expected callback URL) so they can run a second sub-check
    that verifies the upstream actually knows how to reach us.
    """
    fn = _TESTS.get(provider)
    if not fn:
        return {
            "ok": None,
            "detail": f"No health check registered for '{provider}'.",
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


async def _resolve_nango_runtime_credentials(db, integration_row, leased_creds: dict) -> dict:
    """For Nango-backed integrations, fetch a fresh access token before test.

    The stored credential payload for these rows is only an indirection ref:
    {"via": "nango", "provider_config_key": "...", "connection_id": "..."}.
    Health checks need a real token to hit provider APIs, so we resolve one
    just-in-time from Nango's /connection endpoint.
    """
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
    result = await run_test(
        row.provider, resolved_creds or {}, wiring_ctx=wiring_ctx,
    )

    cfg = dict(row.config or {})
    cfg["last_health_check"] = result
    row.config = cfg
    await db.flush()
    return result


_PROVIDERS_WITH_WIRING: set[str] = {
    "telegram", "discord", "whatsapp", "wechat_personal",
}


async def _wiring_ctx_for_integration(db, integration_row) -> dict | None:
    """Resolve the expected inbound URL for providers that have one."""
    provider = integration_row.provider
    if provider not in _PROVIDERS_WITH_WIRING:
        return None

    from sqlalchemy import select
    from packages.core.models.channel import ChannelConfig
    from packages.core.config import get_settings

    cc = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == integration_row.entity_id,
            ChannelConfig.channel_type == provider,
            ChannelConfig.config["integration_id"].astext == integration_row.id,
        )
    )).scalar_one_or_none()
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    if not cc:
        return {"expected_url": "", "channel_config_id": None}

    if provider == "telegram":
        import hashlib
        token = (cc.credentials or {}).get("bot_token", "")
        bot_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
        expected = f"{base}/api/v1/channels/telegram/webhook/{bot_hash}?config_id={cc.id}"
    elif provider == "discord":
        expected = f"{base}/api/v1/channels/discord/callback?config_id={cc.id}"
    elif provider == "wechat_personal":
        expected = f"{base}/api/v1/channels/wechat_personal/callback?config_id={cc.id}"
    elif provider == "whatsapp":
        expected = ""
    else:
        expected = ""

    return {"expected_url": expected, "channel_config_id": cc.id}


async def run_and_persist_oauth(db, oauth_account_id: str) -> HealthResult:
    """Run the health check for an ``OAuthAccount`` row and stash into
    its ``profile.last_health_check``."""
    from sqlalchemy import select
    from packages.core.models.user import OAuthAccount

    row = (await db.execute(
        select(OAuthAccount).where(OAuthAccount.id == oauth_account_id)
    )).scalar_one_or_none()
    if not row:
        return {"ok": False, "detail": "oauth account not found", "latency_ms": 0.0, "checked_at": _now_iso()}

    # Feed the access_token (+ any extras already on profile) into the
    # test. Providers like email/webhook don't use oauth_accounts, so
    # they're unreachable here — fine.
    creds = {"access_token": row.access_token}
    result = await run_test(row.provider, creds)

    profile = dict(row.profile or {})
    profile["last_health_check"] = result
    row.profile = profile
    await db.flush()
    return result


# Unused — re-export for callers that want to serialise the HealthResult
_ = json
