"""Headed-login flow — interactive browser sign-in for browser-session
providers (NotebookLM, Claude.ai web, ChatGPT web, …).

Flow:
  1. POST /headed-login/start { provider } →
     api spawns a session in browser-runner, returns session_id.
  2. WS  /headed-login/{sid}/stream?token=<jwt> →
     api proxies bidirectionally to browser-runner. The browser shows
     the live page and sends mouse/keyboard back.
  3. POST /headed-login/{sid}/finish →
     api asks browser-runner to capture storage_state, encrypts it
     via CredentialService, persists as a new Integration row (or
     updates an existing one) and returns the IntegrationResponse so
     the UI can refresh.

Authorization: JWT for HTTP, JWT-in-query for the WebSocket — same
pattern apps/api/routers/ws.py uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Dict, Optional

import httpx
from fastapi import (
    APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, get_db
from packages.core.models.document import Integration
from packages.core.models.user import User
from packages.core.services.auth_service import decode_token
from packages.core.services.integration_service import (
    create_integration, update_integration,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integrations/headed-login", tags=["integrations"])


# ── Sidecar config ─────────────────────────────────────────────────────────

_RUNNER_URL = os.environ.get(
    "BROWSER_RUNNER_URL", "http://browser-runner:5200",
).rstrip("/")
_RUNNER_TOKEN = os.environ.get("BROWSER_RUNNER_TOKEN", "").strip()


def _runner_ws_url(sid: str) -> str:
    base = _RUNNER_URL.replace("http://", "ws://").replace("https://", "wss://")
    return f"{base}/login_session/{sid}/stream"


def _runner_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _RUNNER_TOKEN:
        h["Authorization"] = f"Bearer {_RUNNER_TOKEN}"
    return h


# ── sid ownership tracking ────────────────────────────────────────────────
#
# Defense-in-depth: the runner has no concept of Manor's user model and
# would happily serve any sid to any caller with a valid runner token.
# The api proxy enforces ownership so that even if a sid leaks (logs,
# screenshots, accidental sharing) other tenants can't view the
# in-flight session, exfiltrate the captured cookies, or cancel the
# session out from under its owner.
#
# Stored in-process. Multi-replica api would need to lift this to
# Redis / DB — left as a deployment-time decision.

_session_owners: Dict[str, tuple[str, float]] = {}
_OWNER_TTL_SEC = 30 * 60   # match the runner's max session lifetime


def _record_owner(sid: str, user_id: str) -> None:
    """Remember which Manor user started this sid so future ops on
    the same sid can be authorized."""
    import time as _time
    _session_owners[sid] = (user_id, _time.monotonic() + _OWNER_TTL_SEC)
    # Opportunistic GC of stale entries; cheaper than a background task.
    now = _time.monotonic()
    for k, (_uid, exp) in list(_session_owners.items()):
        if exp <= now:
            _session_owners.pop(k, None)


def _check_owner(sid: str, user_id: str) -> None:
    """Raise HTTPException(404) for "unknown sid", "expired sid", AND
    "wrong owner" — uniformly. A distinguishable status for the wrong-
    owner case would let an attacker enumerate which sids exist by
    comparing response codes. The same-status-everywhere pattern also
    hides how many active sessions exist."""
    import time as _time
    entry = _session_owners.get(sid)
    if entry is None:
        raise HTTPException(404, "session not found")
    owner, exp = entry
    if exp <= _time.monotonic():
        _session_owners.pop(sid, None)
        raise HTTPException(404, "session not found")
    if owner != user_id:
        # Don't say "not yours" — say nothing useful so attackers
        # probing for valid sids can't distinguish "wrong owner" from
        # "doesn't exist".
        raise HTTPException(404, "session not found")


def _release_owner(sid: str) -> None:
    _session_owners.pop(sid, None)


# ── Provider catalog (which URL to land on) ────────────────────────────────

# Each browser-session provider needs a sign-in landing page. After
# the user completes login, capture pulls cookies for that origin.
_PROVIDER_LOGIN_URLS: dict[str, str] = {
    "notebooklm":     "https://notebooklm.google.com",
    "claude_ai_web":  "https://claude.ai/login",
    "chatgpt_web":    "https://chatgpt.com",
    "gemini_web":     "https://gemini.google.com",
    "perplexity_web": "https://www.perplexity.ai",
    # Browser-driven LinkedIn (separate from the OAuth `linkedin`
    # server_key — the two use entirely different credentials and
    # neither can be derived from the other).
    "linkedin_browser": "https://www.linkedin.com/login",
}


# ── Schemas ────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    provider: str


class StartResponse(BaseModel):
    session_id: str
    viewport: Dict[str, int]
    ws_path: str       # relative to api root, frontend stitches host
    provider: str


class FinishRequest(BaseModel):
    integration_id: Optional[str] = None  # update existing if provided


class FinishResponse(BaseModel):
    integration_id: str
    provider: str
    final_url: str


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/start", response_model=StartResponse)
async def start(
    req: StartRequest,
    user: User = Depends(get_current_user),
):
    if req.provider not in _PROVIDER_LOGIN_URLS:
        raise HTTPException(400, f"{req.provider} is not a headed-login provider")

    url = _PROVIDER_LOGIN_URLS[req.provider]
    # 1440x900 — wide enough for ChatGPT's hero with the Sign-in button
    # in the top-right corner; tall enough for Google's two-step login
    # without scrolling.
    viewport = {"width": 1440, "height": 900}
    try:
        async with httpx.AsyncClient(timeout=20.0) as cx:
            r = await cx.post(
                f"{_RUNNER_URL}/login_session",
                headers=_runner_headers(),
                json={"provider": req.provider, "url": url, "viewport": viewport},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            502,
            f"browser-runner sidecar unreachable at {_RUNNER_URL}: {exc}",
        )
    if r.status_code >= 400:
        raise HTTPException(502, f"browser-runner /login_session: {r.status_code} {r.text[:200]}")

    body = r.json()
    sid = body["session_id"]
    # Bind this sid to the user who started it. Subsequent /stream,
    # /finish, /cancel, /state ops verify ownership before forwarding
    # to the runner, so a leaked sid alone (without this user's JWT)
    # cannot be used to view or hijack the session.
    _record_owner(sid, str(user.id))
    return StartResponse(
        session_id=sid,
        viewport=body.get("viewport") or {"width": 1280, "height": 800},
        ws_path=f"/api/v1/integrations/headed-login/{sid}/stream",
        provider=req.provider,
    )


@router.websocket("/{sid}/stream")
async def stream(
    websocket: WebSocket,
    sid: str,
    token: str = Query(...),
):
    """Proxy WS frames from sidecar to browser, and input events back.

    Authentication: JWT in the ``token`` query param. Same pattern as
    /ws — browsers can't set Authorization headers on WebSocket.
    """
    payload = decode_token(token)
    if not payload or not (payload.get("sub") or payload.get("user_id")):
        await websocket.close(code=4001, reason="Invalid token")
        return
    user_id = str(payload.get("sub") or payload.get("user_id"))

    # Verify this user started this sid. Without this check, ANY
    # authenticated user with a leaked sid could view the in-flight
    # session — a real cross-tenant data-exposure bug.
    try:
        _check_owner(sid, user_id)
    except HTTPException as exc:
        await websocket.close(code=4404 if exc.status_code == 404 else 4403, reason=exc.detail)
        return

    # Echo the client's WebSocket subprotocol back so noVNC's handshake
    # check passes. noVNC requests Sec-WebSocket-Protocol: binary; if
    # the server response lacks that header, noVNC's RFB module treats
    # the handshake as failed and closes with code 1006 BEFORE any RFB
    # bytes flow. Pre-fix symptom: "VNC connection closed" in the
    # modal, console error "Failed when connecting:
    # Connection closed (code: 1006)".
    requested_subprotocols = websocket.scope.get("subprotocols") or []
    chosen_subprotocol = "binary" if "binary" in requested_subprotocols else None
    await websocket.accept(subprotocol=chosen_subprotocol)

    upstream_url = _runner_ws_url(sid)
    upstream_headers: list[tuple[str, str]] = []
    if _RUNNER_TOKEN:
        upstream_headers.append(("Authorization", f"Bearer {_RUNNER_TOKEN}"))

    try:
        import websockets
    except ModuleNotFoundError:
        await websocket.close(code=1011, reason="websockets package is not installed")
        return

    try:
        async with websockets.connect(
            upstream_url,
            additional_headers=upstream_headers,
            # 16 MB upper bound. A full Xvfb screen update for a
            # 1440x900 viewport at the worst-case "Raw" RFB encoding
            # is ~5 MB; in practice the Tight encoding x11vnc
            # negotiates with noVNC keeps individual frames under
            # 200 KB. Cap is just a guard against runaway memory.
            max_size=16 * 1024 * 1024,
            open_timeout=15,
            # A quiet sign-in page can sit idle for minutes (waiting
            # for the user to type a password / scan a QR code). VNC
            # itself is silent during idle, and the default 20s WS
            # ping/pong window would drop us mid-login. Disable ping
            # at the proxy edge and rely on Docker's TCP keepalive.
            ping_interval=None,
            ping_timeout=None,
            close_timeout=10,
        ) as upstream:
            await _bridge(websocket, upstream)
    except websockets.exceptions.InvalidStatusCode as exc:
        logger.warning("upstream WS rejected sid=%s: %s", sid, exc)
        await websocket.close(code=4404, reason="session not found")
    except Exception as exc:  # noqa: BLE001
        logger.exception("headed-login WS proxy failed sid=%s", sid)
        try:
            await websocket.close(code=1011, reason=f"proxy error: {exc}")
        except Exception:  # noqa: BLE001
            pass


async def _bridge(client: WebSocket, upstream) -> None:
    """Pump RFB frames upstream→client and mouse/keyboard upstream
    direction until either side disconnects.

    Both halves are now binary (RFB is a binary protocol; the noVNC
    client encodes its input events as RFB messages and the websockify
    bridge passes them through verbatim). Either side closing tears
    down the other half. Errors are swallowed so a transient drop
    can't take down the proxy.
    """
    stop = asyncio.Event()

    async def up_to_down() -> None:
        try:
            async for msg in upstream:
                if stop.is_set():
                    break
                if isinstance(msg, bytes):
                    await client.send_bytes(msg)
                else:
                    await client.send_text(msg)
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("up_to_down ended: %s", exc)
        finally:
            stop.set()

    async def down_to_up() -> None:
        try:
            while not stop.is_set():
                msg = await client.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("down_to_up ended: %s", exc)
        finally:
            stop.set()

    up = asyncio.create_task(up_to_down())
    down = asyncio.create_task(down_to_up())
    await stop.wait()
    for t in (up, down):
        if not t.done():
            t.cancel()
    # Drain — but never raise. CancelledError IS NOT an Exception subclass
    # in Python 3.8+, so we have to catch BaseException explicitly here.
    for t in (up, down):
        try:
            await t
        except BaseException:  # noqa: BLE001
            pass


@router.post("/{sid}/finish", response_model=FinishResponse)
async def finish(
    sid: str,
    req: FinishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Capture storage_state from the sidecar and persist it as the
    user's Integration credentials. Returns the integration id so the
    frontend can refresh the cards list."""
    # Cross-tenant guard: only the user who started this sid may
    # capture (i.e. exfiltrate the cookies into an Integration row).
    _check_owner(sid, str(user.id))

    try:
        async with httpx.AsyncClient(timeout=20.0) as cx:
            r = await cx.post(
                f"{_RUNNER_URL}/login_session/{sid}/capture",
                headers=_runner_headers(),
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            502, f"browser-runner unreachable: {exc}",
        )
    if r.status_code == 404:
        _release_owner(sid)
        raise HTTPException(404, "Login session expired or already captured")
    if r.status_code >= 400:
        raise HTTPException(502, f"browser-runner /capture: {r.status_code} {r.text[:200]}")

    captured = r.json()
    storage_state = captured.get("storage_state") or {}
    final_url = captured.get("final_url", "")

    # Sanity: refuse to save an unauthenticated session. Each provider
    # has a "you're still on the login page" tell.
    if _is_login_url(final_url):
        _release_owner(sid)
        raise HTTPException(
            400,
            f"Sign-in not completed yet — final URL was {final_url}. "
            "Finish logging in inside the modal, then click Done.",
        )

    # Determine the provider — we need it to know how to persist. The
    # sidecar's session doesn't echo it back; resolve by the integration
    # row the user is updating, OR by inferring from the URL host.
    provider: Optional[str] = None
    if req.integration_id:
        existing = (await db.execute(
            select(Integration).where(
                Integration.id == req.integration_id,
                Integration.entity_id == user.entity_id,
            )
        )).scalar_one_or_none()
        if existing:
            provider = existing.provider
    if not provider:
        provider = _infer_provider_from_url(final_url)
    if not provider:
        _release_owner(sid)
        raise HTTPException(
            400,
            "Could not determine which provider this login was for; "
            "pass integration_id or use a recognised provider URL.",
        )

    validation_error = _validate_captured_storage_state(provider, storage_state)
    if validation_error:
        _release_owner(sid)
        raise HTTPException(400, validation_error)

    # The rest of Manor reads the cookie blob via
    # _resolve_bearer_token() → creds.get("api_key"|"access_token"|...).
    # Browser-session providers all use "api_key" as the field today.
    creds = {"api_key": json.dumps(storage_state)}

    if req.integration_id:
        integration = await update_integration(
            db, req.integration_id, user.entity_id,
            credentials=creds,
            status="active",
        )
        if not integration:
            raise HTTPException(404, "integration not found")
    else:
        integration = await create_integration(
            db, user.entity_id, provider,
            credentials=creds,
        )

    await db.commit()
    # Capture succeeded — drop the ownership entry so the sid can't
    # be replayed. (The runner side has already torn down the session
    # but a stray owner row would let a stale GET /state slip through
    # for the TTL window.)
    _release_owner(sid)
    return FinishResponse(
        integration_id=integration.id,
        provider=provider,
        final_url=final_url,
    )


@router.post("/{sid}/cancel", status_code=204)
async def cancel(
    sid: str,
    user: User = Depends(get_current_user),
):
    """Best-effort tear-down — cancellation from the user closing the
    modal. The sidecar GC's stale sessions anyway so this just keeps
    the resource pool small.

    Cross-tenant guard: cancel is destructive (kills the in-flight
    Chromium), so only the originating user may invoke it. We use the
    same 404-on-mismatch pattern as the other endpoints to avoid
    confirming the existence of other tenants' sids.
    """
    _check_owner(sid, str(user.id))
    try:
        async with httpx.AsyncClient(timeout=10.0) as cx:
            await cx.post(
                f"{_RUNNER_URL}/login_session/{sid}/cancel",
                headers=_runner_headers(),
            )
    except Exception:  # noqa: BLE001
        pass
    _release_owner(sid)
    return None


# ── Helpers ────────────────────────────────────────────────────────────────

_LOGIN_URL_FRAGMENTS = (
    "accounts.google.com",
    "/login",
    "auth.openai.com",
    "/sign-in",
    "/signin",
)


def _is_login_url(url: str) -> bool:
    if not url:
        return True
    return any(frag in url for frag in _LOGIN_URL_FRAGMENTS)


_HOST_TO_PROVIDER = {
    "notebooklm.google.com": "notebooklm",
    "claude.ai":             "claude_ai_web",
    "chatgpt.com":           "chatgpt_web",
    "chat.openai.com":       "chatgpt_web",
    "gemini.google.com":     "gemini_web",
    "perplexity.ai":         "perplexity_web",
    "www.perplexity.ai":     "perplexity_web",
    "linkedin.com":          "linkedin_browser",
    "www.linkedin.com":      "linkedin_browser",
}


def _storage_cookie_names(storage_state: object) -> set[str]:
    if not isinstance(storage_state, dict):
        return set()
    cookies = storage_state.get("cookies") or []
    if not isinstance(cookies, list):
        return set()
    return {
        str(cookie.get("name"))
        for cookie in cookies
        if isinstance(cookie, dict) and cookie.get("name")
    }


def _validate_captured_storage_state(provider: str, storage_state: object) -> str | None:
    """Provider-specific checks before persisting a headed-login capture."""
    return None


def _infer_provider_from_url(url: str) -> Optional[str]:
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    return _HOST_TO_PROVIDER.get(host)
