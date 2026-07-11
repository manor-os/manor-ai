"""WeChat Personal sidecar runner — iLink Bot API, multi-session.

Manor Integrations expose this sidecar to drive personal WeChat
accounts via Tencent's official iLink Bot API (the *ClawBot* plugin
shipped 2026). One sidecar can hold many concurrent bot sessions —
each Manor Integration row owns one ``session_id`` and the runner
maps it to its own ``bot_token`` + long-poll task + callback URL.

This is a full rewrite of the original single-session runner. Both
shapes are kept around in the docstring for context, but the old
process-wide endpoints (``/status`` / ``/qr.png`` / ``/messages``
without a session prefix) now 410 with a clear migration message.

HTTP surface
────────────
Health (no auth, no session):
  GET  /health                                → {ok, protocol, sessions: N}

Lifecycle (per session):
  POST   /sessions                            → {session_id}
                                                  (spawns login + long-poll
                                                  in the background)
  GET    /sessions                            → [{sid, online, account, ...}]
  GET    /sessions/{sid}/status               → status dict
  GET    /sessions/{sid}/qr.png               → image/png (no auth)
  POST   /sessions/{sid}/messages             → send (text now; media later)
  POST   /sessions/{sid}/config               → register callback URL
  DELETE /sessions/{sid}                      → cancel + tear down

Inbound envelope POSTed to the configured callback (unchanged):

    {
      "kind":         "direct" | "group",
      "from":         "<ilink_user_id>",
      "from_name":    "",
      "chat_id":      "<ilink_user_id>",
      "text":         "hi",
      "message_type": "text",
      "msg_id":       "<context_token>",
      "session_id":   "<sid>"        # NEW — lets Manor's gateway
                                     #       attribute when one Manor
                                     #       entity has many WeChat
                                     #       accounts.
    }

Persistence
───────────
Sessions live in process memory. On runner restart they're gone and
the user has to scan again. Persisting tokens to Vault is a future
exercise — keeping the v1 surface narrow.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .ilink_client import (
    ILinkClient,
    ILinkError,
    ILinkSessionExpired,
    InboundMessage,
)

logger = logging.getLogger("wechat_runner")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


_DATA_ROOT = Path(os.getenv("WECHAT_RUNNER_DATA", "/data"))
_SESSIONS_DIR = _DATA_ROOT / "sessions"
RUNNER_BEARER_TOKEN = os.getenv("RUNNER_BEARER_TOKEN", "")
PUBLIC_QR_BASE = (os.getenv("PUBLIC_QR_BASE") or "").rstrip("/")

_QRCODE_POLL_INTERVAL = 2.0
_SESSION_EXPIRED_BACKOFF_SEC = 60 * 60
_QRCODE_LOGIN_DEADLINE_SEC = 300.0


# ── Per-session state ──────────────────────────────────────────────────────

class Session:
    """One iLink bot session = one personal WeChat account.

    Holds the protocol client, the asyncio task driving its login +
    long-poll loop, and the bookkeeping needed for outbound /messages
    (callback URL + per-peer context_token cache).
    """

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.client = ILinkClient()
        self.online: bool = False
        self.qr_pending: bool = True
        self.account: Optional[Dict[str, Any]] = None
        self.callback_url: Optional[str] = None
        self.callback_bearer: Optional[str] = None
        self.last_error: Optional[str] = None
        self.cursor: str = ""
        # Per-peer cache of the most recent context_token. Outbound
        # /messages looks this up; iLink rejects unsolicited push.
        self.context_tokens: Dict[str, str] = {}
        self.last_inbound_at: Optional[float] = None
        self.last_callback_at: Optional[float] = None
        self.last_callback_status: Optional[int] = None
        self.last_callback_error: Optional[str] = None
        self.last_update_msg_count: int = 0
        self.task: Optional[asyncio.Task] = None
        self.closed: bool = False
        self.created_at: float = time.time()

    @property
    def qr_path(self) -> Path:
        return _SESSIONS_DIR / self.sid / "qr.png"

    def status_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.sid,
            "online": self.online,
            "qr_pending": self.qr_pending,
            "account": self.account,
            "last_error": self.last_error,
            "callback_configured": bool(self.callback_url),
            "last_inbound_at": self.last_inbound_at,
            "last_callback_at": self.last_callback_at,
            "last_callback_status": self.last_callback_status,
            "last_callback_error": self.last_callback_error,
            "last_update_msg_count": self.last_update_msg_count,
            # Peers that have sent us a message recently enough for iLink
            # to expose a context_token. These are the only targets the
            # bot can reply to without iLink returning 409.
            "known_peers": sorted(self.context_tokens.keys()),
        }

    async def shutdown(self) -> None:
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Best-effort: drop the QR PNG so a reconnect doesn't show a
        # stale image while the new login flow is starting.
        try:
            self.qr_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


_sessions: Dict[str, Session] = {}


# ── Auth dep ────────────────────────────────────────────────────────────────

async def require_bearer(authorization: Optional[str] = Header(None)) -> None:
    """Bearer-token gate for every non-public endpoint. Disabled when
    ``RUNNER_BEARER_TOKEN`` is empty (useful for first-run testing)."""
    if not RUNNER_BEARER_TOKEN:
        return
    expected = f"Bearer {RUNNER_BEARER_TOKEN}"
    if (authorization or "").strip() != expected:
        raise HTTPException(401, "Bad bearer token")


def _require_session(sid: str) -> Session:
    sess = _sessions.get(sid)
    if not sess or sess.closed:
        raise HTTPException(404, f"session {sid!r} not found")
    return sess


# ── Login + long-poll loop ────────────────────────────────────────────────

async def _initialize_qr(sess: Session) -> Optional[str]:
    """Generate the login QR (one round-trip to Tencent) and write the
    PNG to disk. Returns the qrcode key the poll loop uses, or None on
    failure. Pulled out of ``_login_with_qrcode`` so ``POST /sessions``
    can await it synchronously — without that, the frontend sees 404
    on the first qr.png fetch.
    """
    sess.qr_pending = True
    sess.last_error = None
    try:
        kick = await sess.client.get_bot_qrcode()
    except ILinkError as exc:
        sess.qr_pending = False
        sess.last_error = f"get_bot_qrcode failed: {exc}"
        logger.error("[sid=%s] %s", sess.sid, sess.last_error)
        return None

    qrcode_key = kick.get("qrcode") or ""
    qrcode_url = kick.get("qrcode_img_content") or ""
    if not qrcode_key or not qrcode_url:
        sess.qr_pending = False
        sess.last_error = f"unexpected get_bot_qrcode payload: {kick}"
        logger.error("[sid=%s] %s", sess.sid, sess.last_error)
        return None

    _render_qr_png(qrcode_url, sess.qr_path)
    logger.info(
        "[sid=%s] login QR ready at %s", sess.sid, sess.qr_path,
    )
    return qrcode_key


async def _login_with_qrcode(sess: Session, qrcode_key: Optional[str] = None) -> bool:
    """Run one full QR-login attempt for ``sess``. If ``qrcode_key`` is
    None, kicks off a fresh QR (used by the long-poll re-login path);
    otherwise reuses the qrcode already initialized.
    """
    if qrcode_key is None:
        qrcode_key = await _initialize_qr(sess)
        if not qrcode_key:
            return False

    deadline = time.monotonic() + _QRCODE_LOGIN_DEADLINE_SEC
    consecutive_errors = 0
    while time.monotonic() < deadline and not sess.closed:
        await asyncio.sleep(_QRCODE_POLL_INTERVAL)
        try:
            await sess.client.get_qrcode_status(qrcode_key)
        except ILinkError as exc:
            consecutive_errors += 1
            sess.last_error = f"get_qrcode_status: {exc}"
            logger.warning("[sid=%s] status poll failed (%d consecutive): %s",
                           sess.sid, consecutive_errors, exc)
            if consecutive_errors >= 5:
                await asyncio.sleep(min(2 ** (consecutive_errors - 5), 30))
            continue
        consecutive_errors = 0
        if sess.client.bot_token:
            sess.online = True
            sess.qr_pending = False
            sess.last_error = None
            sess.account = {
                # iLink doesn't return a friendly name on login —
                # leave ``nick_name`` None until/unless Tencent
                # exposes it. Field shape kept for adapter compat.
                "user_name": "self",
                "nick_name": None,
            }
            logger.info("[sid=%s] WeChat login complete via iLink", sess.sid)
            return True
    sess.qr_pending = False
    if not sess.last_error:
        sess.last_error = "QR scan timed out (no bot_token within 5 min)"
    logger.warning("[sid=%s] %s", sess.sid, sess.last_error)
    return False


async def _long_poll_loop(sess: Session) -> None:
    backoff = 2.0
    while not sess.closed:
        if not sess.online or not sess.client.bot_token:
            await asyncio.sleep(2.0)
            continue
        try:
            data = await sess.client.get_updates(sess.cursor)
        except ILinkSessionExpired as exc:
            logger.warning("[sid=%s] session expired (%s) — backing off %ds",
                           sess.sid, exc, _SESSION_EXPIRED_BACKOFF_SEC)
            sess.online = False
            sess.client.bot_token = None
            sess.last_error = "session expired — re-login required"
            await asyncio.sleep(_SESSION_EXPIRED_BACKOFF_SEC)
            await _login_with_qrcode(sess)
            backoff = 2.0
            continue
        except ILinkError as exc:
            logger.warning("[sid=%s] getupdates failed: %s — retry in %.0fs",
                           sess.sid, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("[sid=%s] getupdates crashed unexpectedly", sess.sid)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        backoff = 2.0

        sess.cursor = data.get("get_updates_buf") or sess.cursor
        messages = data.get("msgs") or []
        sess.last_update_msg_count = len(messages)
        if messages:
            logger.info("[sid=%s] getupdates returned %d message(s)", sess.sid, len(messages))
        for raw in messages:
            parsed = ILinkClient.parse_message(raw)
            if not parsed:
                logger.info("[sid=%s] skipped unparseable iLink message keys=%s",
                            sess.sid, sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__)
                continue
            if parsed.is_from_bot:
                continue
            sess.last_inbound_at = time.time()
            if parsed.context_token:
                sess.context_tokens[parsed.ilink_user_id] = parsed.context_token
            await _dispatch_inbound(sess, parsed)


async def _dispatch_inbound(sess: Session, msg: InboundMessage) -> None:
    if not sess.callback_url:
        logger.debug("[sid=%s] inbound msg but no callback configured", sess.sid)
        return
    if not msg.text:
        return
    envelope = {
        "kind": "direct",
        "from": msg.ilink_user_id,
        "from_name": "",
        "chat_id": msg.ilink_user_id,
        "text": msg.text,
        "message_type": "text",
        "msg_id": msg.context_token,
        "session_id": sess.sid,
    }
    headers = {"Content-Type": "application/json"}
    if sess.callback_bearer:
        headers["Authorization"] = f"Bearer {sess.callback_bearer}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as cx:
            r = await cx.post(sess.callback_url, json=envelope, headers=headers)
        sess.last_callback_at = time.time()
        sess.last_callback_status = r.status_code
        sess.last_callback_error = None if r.status_code < 400 else r.text[:200]
        if r.status_code >= 400:
            logger.warning("[sid=%s] callback %d: %s",
                           sess.sid, r.status_code, r.text[:200])
        else:
            logger.info("[sid=%s] callback delivered for peer=%s status=%d",
                        sess.sid, msg.ilink_user_id, r.status_code)
    except Exception as exc:  # noqa: BLE001
        sess.last_callback_at = time.time()
        sess.last_callback_status = None
        sess.last_callback_error = str(exc)
        logger.warning("[sid=%s] callback POST failed: %s", sess.sid, exc)


async def _orchestrator(sess: Session, initial_qrcode_key: Optional[str]) -> None:
    """Per-session top-level task: poll the QR until scanned, then
    long-poll forever. ``POST /sessions`` pre-generates the first QR
    so the frontend can fetch it without a race; we just inherit that
    key on entry. On failure we kick a fresh QR after a brief pause.
    """
    if not await _login_with_qrcode(sess, initial_qrcode_key):
        if sess.closed:
            return
        await asyncio.sleep(10.0)
        if sess.closed:
            return
        await _login_with_qrcode(sess)  # fresh QR
    await _long_poll_loop(sess)


# ── QR rendering ───────────────────────────────────────────────────────────

def _render_qr_png(content: str, out_path: Path) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        logger.error("`qrcode` package not installed — cannot render login QR")
        return
    img = qrcode.make(content)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out_path.write_bytes(buf.getvalue())


# ── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(title="Manor WeChat Personal Runner (iLink, multi-session)")


@app.on_event("startup")
async def _startup() -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("shutdown")
async def _shutdown() -> None:
    for sess in list(_sessions.values()):
        await sess.shutdown()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "protocol": "ilink",
        "sessions": sum(1 for s in _sessions.values() if not s.closed),
    }


# ── /sessions/* — session lifecycle ────────────────────────────────────────

class StartSessionResponse(BaseModel):
    session_id: str
    qr_path: str   # path under this runner — frontend stitches host


@app.post("/sessions",
          response_model=StartSessionResponse,
          dependencies=[Depends(require_bearer)])
async def start_session() -> StartSessionResponse:
    sid = secrets.token_urlsafe(12)
    sess = Session(sid)
    _sessions[sid] = sess
    # Block the response until the first QR is on disk — otherwise
    # the frontend's <img> fires before the file exists and 404s.
    qrcode_key = await _initialize_qr(sess)
    if not qrcode_key:
        _sessions.pop(sid, None)
        await sess.shutdown()
        raise HTTPException(
            502,
            f"Failed to obtain login QR from Tencent: {sess.last_error}",
        )
    sess.task = asyncio.create_task(
        _orchestrator(sess, qrcode_key), name=f"ilink-{sid}",
    )
    logger.info("[sid=%s] session created", sid)
    return StartSessionResponse(
        session_id=sid,
        qr_path=f"/sessions/{sid}/qr.png",
    )


@app.get("/sessions", dependencies=[Depends(require_bearer)])
async def list_sessions() -> List[Dict[str, Any]]:
    return [s.status_dict() for s in _sessions.values() if not s.closed]


@app.get("/sessions/{sid}/status", dependencies=[Depends(require_bearer)])
async def session_status(sid: str) -> Dict[str, Any]:
    return _require_session(sid).status_dict()


@app.get("/sessions/{sid}/qr.png")
async def session_qr_png(sid: str):
    """Public — no bearer. Image is short-lived and only useful to the
    human scanning it from inside their WeChat ClawBot plugin."""
    sess = _sessions.get(sid)
    if not sess or sess.closed:
        raise HTTPException(404, f"session {sid!r} not found")
    if not sess.qr_path.exists():
        raise HTTPException(404, "No QR available yet")
    return FileResponse(str(sess.qr_path), media_type="image/png")


@app.delete("/sessions/{sid}", dependencies=[Depends(require_bearer)])
async def delete_session(sid: str) -> Dict[str, Any]:
    sess = _sessions.pop(sid, None)
    if sess:
        await sess.shutdown()
        logger.info("[sid=%s] session deleted", sid)
    return {"ok": True}


# ── Per-session messages + config ──────────────────────────────────────────

class SendMessageBody(BaseModel):
    kind: str = "direct"
    target: str          # ilink_user_id
    body: Optional[str] = None
    media_kind: Optional[str] = None  # not yet supported
    url: Optional[str] = None
    caption: Optional[str] = None


@app.post("/sessions/{sid}/messages", dependencies=[Depends(require_bearer)])
async def send_message(sid: str, payload: SendMessageBody) -> Dict[str, Any]:
    sess = _require_session(sid)
    if not sess.online or not sess.client.bot_token:
        raise HTTPException(503, "WeChat session not online — scan QR first.")
    if payload.media_kind:
        raise HTTPException(
            501,
            "Media send not yet wired for iLink. Text-only for v1.",
        )
    if not payload.body:
        raise HTTPException(400, "body required for text messages")

    context_token = sess.context_tokens.get(payload.target)
    if not context_token:
        raise HTTPException(
            409,
            f"No recent context_token for peer {payload.target!r}. "
            "iLink only allows replies — the peer has to message us "
            "first (or message us again after their last message aged out).",
        )
    try:
        await sess.client.send_text(payload.target, payload.body, context_token)
    except ILinkError as exc:
        raise HTTPException(502, f"iLink sendmessage failed: {exc}")
    return {"success": True}


class ConfigBody(BaseModel):
    callback_url: str
    bearer_token: Optional[str] = None


@app.post("/sessions/{sid}/config", dependencies=[Depends(require_bearer)])
async def set_session_config(sid: str, payload: ConfigBody) -> Dict[str, Any]:
    sess = _require_session(sid)
    sess.callback_url = payload.callback_url
    sess.callback_bearer = payload.bearer_token or None
    logger.info("[sid=%s] callback set to %s", sid, payload.callback_url)
    return {"ok": True}


# ── Legacy single-session endpoints — 410 Gone with migration hint ─────────

_LEGACY_410 = (
    "This endpoint was removed in the multi-session iLink rewrite. "
    "Use /sessions/{session_id}/... — start a session with POST /sessions."
)


@app.get("/status", dependencies=[Depends(require_bearer)])
async def legacy_status() -> Dict[str, Any]:
    raise HTTPException(410, _LEGACY_410)


@app.get("/qr.png")
async def legacy_qr() -> Dict[str, Any]:
    raise HTTPException(410, _LEGACY_410)


@app.get("/groups", dependencies=[Depends(require_bearer)])
async def legacy_groups() -> List[Dict[str, Any]]:
    raise HTTPException(410, _LEGACY_410)


@app.get("/contacts", dependencies=[Depends(require_bearer)])
async def legacy_contacts() -> List[Dict[str, Any]]:
    raise HTTPException(410, _LEGACY_410)


@app.post("/messages", dependencies=[Depends(require_bearer)])
async def legacy_messages() -> Dict[str, Any]:
    raise HTTPException(410, _LEGACY_410)


@app.post("/config", dependencies=[Depends(require_bearer)])
async def legacy_config() -> Dict[str, Any]:
    raise HTTPException(410, _LEGACY_410)
