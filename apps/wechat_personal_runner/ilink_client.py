"""Async client for Tencent's iLink Bot API (`ilinkai.weixin.qq.com`).

iLink is the official Bot protocol Tencent shipped in 2026 for personal
WeChat accounts. It replaces all the dead/grey-market options:

  - itchat / itchat-uos          → web protocol, killed
  - Wechaty puppet-padlocal      → paid grey-market iPad protocol
  - wxauto / wechaty puppet-xp   → requires user's Windows PC

iLink is plain HTTPS + JSON, polling-based (no public webhook needed),
and Tencent-signed. The user activates the *WeChat ClawBot* plugin from
inside the WeChat App → 我 → 设置 → 插件, scans a QR shown by us,
and a long-lived ``bot_token`` is returned.

Contract is intentionally narrow — this module only knows the wire
protocol; it does NOT know about Manor entities, agents, or
conversations. The sidecar runner.py wraps this client and forwards
inbound messages to Manor's channel_gateway via callback POST.

Endpoints
─────────
Login (no auth):
  GET  /ilink/bot/get_bot_qrcode?bot_type=3
  GET  /ilink/bot/get_qrcode_status?qrcode={qrcode_key}

Once a ``bot_token`` is in hand, every other call carries
``Authorization: Bearer {bot_token}`` and the body wraps a
``base_info`` block:

  POST /ilink/bot/getupdates       {get_updates_buf, base_info}
  POST /ilink/bot/sendmessage      {msg, base_info}
  POST /ilink/bot/getconfig        {ilink_user_id, context_token, base_info}
  POST /ilink/bot/sendtyping       {ilink_user_id, typing_ticket, status, base_info}
  POST /ilink/bot/getuploadurl     {ilink_user_id, context_token, base_info}

Caveats
───────
- ``context_token`` is per-incoming-message; outbound MUST echo back
  the token of the message it's replying to. Reply-only — no
  unsolicited push.
- Group messages are gated by server-side policy (default disabled).
- Error code -14 means session expired; back off for 60 minutes.
- Each request includes ``X-WECHAT-UIN`` (random uint32, base64'd) for
  Tencent's anti-replay check.
"""
from __future__ import annotations

import base64
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("ilink_client")


_BASE_URL = "https://ilinkai.weixin.qq.com"
_CHANNEL_VERSION = "1.0.2"
_LONG_POLL_TIMEOUT = 40.0    # server holds 35s; give it 5s slack
_DEFAULT_TIMEOUT = 15.0
_BOT_TYPE = 3
_TYPING_TICKET_TTL = 23 * 3600   # cache typing tickets just under 24h


# ── Public exceptions ──────────────────────────────────────────────────────

class ILinkError(Exception):
    """Base class for protocol-level errors from iLink."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"iLink error {code}: {message}")


class ILinkSessionExpired(ILinkError):
    """Returned for Tencent error code -14 — re-login required."""


# ── Data classes for incoming messages ─────────────────────────────────────

@dataclass
class InboundMessage:
    """One message as parsed from a getupdates response.

    The raw payload is kept on ``raw`` for forward compatibility — Tencent
    keeps adding item types and we don't want to lose them.
    """
    ilink_user_id: str
    context_token: str
    text: str
    message_type: int                    # 1 = peer→bot, 2 = bot→peer (skip)
    is_from_bot: bool
    raw: Dict[str, Any] = field(default_factory=dict)


# ── Client ─────────────────────────────────────────────────────────────────

class ILinkClient:
    """Async HTTP/JSON client for iLink. Stateless w.r.t. Manor.

    Holds: ``bot_token`` (after login) + ``base_url`` (overridable from
    qrcode_status response) + a small cache of ``typing_ticket``s.
    Everything else is per-call.
    """

    def __init__(
        self,
        *,
        bot_token: Optional[str] = None,
        base_url: Optional[str] = None,
        bot_type: int = _BOT_TYPE,
        channel_version: str = _CHANNEL_VERSION,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.bot_token = bot_token
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.bot_type = bot_type
        self.channel_version = channel_version
        self._timeout = timeout
        # typing_ticket cache: ilink_user_id → (ticket, expires_at)
        self._typing_tickets: Dict[str, tuple[str, float]] = {}

    # ── Headers / body helpers ─────────────────────────────────────────

    def _wechat_uin(self) -> str:
        """Random uint32 → DECIMAL STRING → base64. Tencent's anti-
        replay nonce. (NOT raw 4 bytes; the reference impl in
        ``@tencent-weixin/openclaw-weixin@1.0.2`` formats the uint32
        as a decimal string before base64-ing — Tencent's server
        rejects the raw-bytes variant with -14.)"""
        return base64.b64encode(str(random.getrandbits(32)).encode()).decode()

    def _headers(self, *, with_auth: bool = True) -> Dict[str, str]:
        h: Dict[str, str] = {
            "Content-Type": "application/json",
            # Required by Tencent — without it ``getupdates`` returns
            # error_code -14 immediately after login. Sniffed from the
            # 1.0.2 OpenClaw reference SDK.
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._wechat_uin(),
            "User-Agent": f"manor-ilink/{self.channel_version}",
        }
        if with_auth and self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    def _base_info(self) -> Dict[str, str]:
        return {"channel_version": self.channel_version}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        with_auth: bool = True,
    ) -> Dict[str, Any]:
        """Single source of truth for every HTTP call. Raises
        ``ILinkError`` (or subclass) on non-2xx and on JSON-level errors."""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout or self._timeout) as cx:
                r = await cx.request(
                    method, url,
                    headers=self._headers(with_auth=with_auth),
                    json=json_body, params=params,
                )
        except httpx.HTTPError as exc:
            # Wrap transport-level errors (timeout / connection reset /
            # DNS) as ILinkError so callers see a single exception
            # hierarchy and can retry uniformly.
            raise ILinkError(0, f"{type(exc).__name__}: {exc}") from exc
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}

        if r.status_code >= 400:
            raise ILinkError(r.status_code, str(data)[:500])

        # iLink uses negative ``error_code`` inside the JSON envelope to
        # indicate logical failures (-14 is the well-known one for
        # expired sessions). Wrap them in typed exceptions.
        ec = data.get("error_code") or data.get("errcode")
        if ec is not None and ec != 0:
            msg = data.get("error_msg") or data.get("errmsg") or str(data)
            if ec == -14:
                raise ILinkSessionExpired(ec, msg)
            raise ILinkError(int(ec), msg)
        return data

    # ── Login ──────────────────────────────────────────────────────────

    async def get_bot_qrcode(self) -> Dict[str, str]:
        """Start the login flow.

        Returns ``{qrcode, qrcode_img_content}`` where:
          * ``qrcode`` is the polling key for ``get_qrcode_status``
          * ``qrcode_img_content`` is a URL the user needs to scan (we
            render it as a QR PNG ourselves before showing).
        """
        return await self._request(
            "GET", "/ilink/bot/get_bot_qrcode",
            params={"bot_type": self.bot_type}, with_auth=False,
        )

    async def get_qrcode_status(self, qrcode_key: str) -> Dict[str, Any]:
        """Poll once. Pre-scan: ``{status: "wait"}``. Post-scan:
        ``{bot_token, baseurl}``. We update ``self.bot_token`` /
        ``self.base_url`` automatically so subsequent calls authenticate.

        Empirically Tencent holds this connection open for ~30s on the
        first call to a fresh qrcode key (server-side push) before
        returning ``status: "wait"`` — so the timeout has to be
        comfortably above 30s.
        """
        data = await self._request(
            "GET", "/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode_key}, with_auth=False,
            timeout=45.0,
        )
        if data.get("bot_token"):
            self.bot_token = data["bot_token"]
            if data.get("baseurl"):
                self.base_url = data["baseurl"].rstrip("/")
        return data

    # ── Long-polling ───────────────────────────────────────────────────

    async def get_updates(self, cursor: str = "") -> Dict[str, Any]:
        """Long-poll for new messages. Server holds the response up to
        35s; we time out at 40s. Pass back the prior ``get_updates_buf``
        as ``cursor``; advance it from the response on every call,
        even when ``msgs`` is empty.
        """
        data = await self._request(
            "POST", "/ilink/bot/getupdates",
            json_body={
                "get_updates_buf": cursor,
                "base_info": self._base_info(),
            },
            timeout=_LONG_POLL_TIMEOUT,
        )
        return data

    @staticmethod
    def parse_message(raw: Dict[str, Any]) -> Optional[InboundMessage]:
        """Flatten a single raw message envelope into ``InboundMessage``.

        Returns None for unsupported / outbound items so callers can
        ignore them with a single ``if not parsed`` guard.
        """
        if not isinstance(raw, dict):
            return None
        message_type = int(raw.get("message_type") or 0)
        is_from_bot = (message_type == 2)
        ilink_user_id = (
            raw.get("ilink_user_id")
            or raw.get("from_user_id")
            or raw.get("to_user_id")
            or ""
        )
        context_token = raw.get("context_token") or ""
        items = raw.get("item_list") or []
        text_parts: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ti = it.get("text_item") or {}
            t = ti.get("text") if isinstance(ti, dict) else None
            if t:
                text_parts.append(str(t))
        text = "".join(text_parts).strip()
        if not ilink_user_id:
            return None
        return InboundMessage(
            ilink_user_id=ilink_user_id,
            context_token=context_token,
            text=text,
            message_type=message_type,
            is_from_bot=is_from_bot,
            raw=raw,
        )

    # ── Sending ────────────────────────────────────────────────────────

    async def send_text(
        self,
        ilink_user_id: str,
        text: str,
        context_token: str,
    ) -> Dict[str, Any]:
        """Reply to a peer with text. ``context_token`` MUST come from a
        message that peer recently sent us — iLink rejects unsolicited
        pushes.
        """
        if not context_token:
            raise ILinkError(
                400,
                "context_token required (iLink only allows replies to "
                "incoming messages — no unsolicited push).",
            )
        return await self._request(
            "POST", "/ilink/bot/sendmessage",
            json_body={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": ilink_user_id,
                    "client_id": f"manor-{uuid.uuid4()}",
                    "message_type": 2,        # 2 = bot→peer
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [
                        {"type": 1, "text_item": {"text": text}},
                    ],
                },
                "base_info": self._base_info(),
            },
        )

    async def send_typing(
        self,
        ilink_user_id: str,
        context_token: str,
        *,
        is_typing: bool = True,
    ) -> None:
        """Show / hide the typing indicator. Tencent gates this behind a
        ``typing_ticket`` we have to fetch via ``getconfig`` first; we
        cache it for ~24h per peer to avoid the round-trip on every
        keystroke."""
        ticket = await self._typing_ticket(ilink_user_id, context_token)
        if not ticket:
            return
        try:
            await self._request(
                "POST", "/ilink/bot/sendtyping",
                json_body={
                    "ilink_user_id": ilink_user_id,
                    "typing_ticket": ticket,
                    "status": 1 if is_typing else 2,
                    "base_info": self._base_info(),
                },
                timeout=10.0,
            )
        except ILinkError as exc:
            # Ticket may have expired earlier than 24h; drop the cache
            # so next call re-fetches.
            self._typing_tickets.pop(ilink_user_id, None)
            logger.debug("sendtyping failed (will refresh ticket): %s", exc)

    async def _typing_ticket(
        self, ilink_user_id: str, context_token: str,
    ) -> Optional[str]:
        cached = self._typing_tickets.get(ilink_user_id)
        if cached and cached[1] > time.time():
            return cached[0]
        try:
            data = await self._request(
                "POST", "/ilink/bot/getconfig",
                json_body={
                    "ilink_user_id": ilink_user_id,
                    "context_token": context_token,
                    "base_info": self._base_info(),
                },
                timeout=10.0,
            )
        except ILinkError as exc:
            logger.debug("getconfig failed: %s", exc)
            return None
        ticket = data.get("typing_ticket")
        if ticket:
            self._typing_tickets[ilink_user_id] = (
                ticket, time.time() + _TYPING_TICKET_TTL,
            )
        return ticket
