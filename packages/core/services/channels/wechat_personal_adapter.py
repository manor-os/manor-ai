"""Personal WeChat channel adapter — talks to a multi-session iLink runner.

Personal WeChat (i.e. a regular human account, not an Official Account)
is driven via Tencent's official iLink Bot API (released 2026, surfaced
inside WeChat as the *ClawBot* plugin). Manor's ``wechat-runner``
sidecar holds the long-lived iLink long-poll for many concurrent
accounts at once — each Integration row owns one ``session_id``.

    POST   /sessions                            → {session_id}
    GET    /sessions/{sid}/status               → {online, qr_pending, ...}
    GET    /sessions/{sid}/qr.png               → image/png
    POST   /sessions/{sid}/messages             → send (text only for v1)
    POST   /sessions/{sid}/config               → register callback URL
    DELETE /sessions/{sid}                      → tear down

The runner pushes inbound messages back to Manor by POSTing JSON to
``/api/v1/channels/wechat_personal/callback?config_id=...`` — the URL
the runner gets via ``/config`` from :py:meth:`register_webhook`.
Inbound payload includes the runner's ``session_id`` so multiple
accounts on the same Manor entity stay attributable.

Credentials in ``ChannelConfig.credentials``::

    {
      "runner_url":   "https://wechat-bot.internal:8800",  # default OK
      "bearer_token": "shared-secret",                       # optional
      "session_id":   "abc123",                              # required
                                                             # — set when
                                                             # the user
                                                             # finishes
                                                             # the QR flow
      "default_target": "<ilink_user_id>"                   # optional
    }

iLink semantics: outbound messages MUST reply to a peer that recently
messaged us (``context_token`` requirement). The runner enforces this
and 409s if there's no cached token. The default model: agents only
speak when spoken to. To unlock proactive sends, add a check-in flow
where the user's WeChat sends a hello first.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from packages.core.config import get_settings
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


class WeChatPersonalChannelAdapter(ChannelAdapter):
    channel_type = "wechat_personal"

    # ── Helpers ─────────────────────────────────────────────────────────

    def _runner(self, cc: ChannelConfig) -> tuple[str, str, str]:
        """Return ``(runner_url, bearer_token, session_id)``. Raises if
        any of the required pieces are missing."""
        creds = cc.credentials or {}
        runner_url = (creds.get("runner_url") or "").rstrip("/")
        if not runner_url:
            raise RuntimeError(
                "WeChat (personal) ChannelConfig is missing runner_url. "
                "Configure the bot runner in Integrations → WeChat (Personal)."
            )
        session_id = (creds.get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError(
                "WeChat (personal) ChannelConfig is missing session_id. "
                "The QR scan flow assigns this — open the connect modal "
                "and scan with your WeChat ClawBot plugin."
            )
        return runner_url, creds.get("bearer_token") or "", session_id

    def _headers(self, token: str) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    # ── Outbound ────────────────────────────────────────────────────────

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        runner_url, token, session_id = self._runner(cc)
        target = to or (cc.credentials or {}).get("default_target")
        if not target:
            raise RuntimeError(
                "WeChat (personal) send_text needs a target ilink_user_id."
            )
        # iLink personal-account API doesn't expose group ids the same
        # way itchat did; ``kind`` here is informational. Keep the
        # field for future migration to group-aware iLink.
        kind = kwargs.get("kind") or "direct"

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{runner_url}/sessions/{session_id}/messages",
                headers={**self._headers(token), "Content-Type": "application/json"},
                json={"kind": kind, "target": target, "body": text},
            )
        if resp.status_code == 401:
            raise RuntimeError("WeChat runner rejected the bearer token.")
        if resp.status_code == 404:
            raise RuntimeError(
                f"WeChat session {session_id!r} not found on the runner — "
                "session was lost (runner restart?). Re-scan the ClawBot QR."
            )
        if resp.status_code == 409:
            # iLink reply-only constraint surfaced from the sidecar.
            raise RuntimeError(
                f"iLink: no recent context_token for {target!r}. "
                "Personal-account bots can only reply, not initiate — "
                "ask the contact to message us first."
            )
        if not resp.is_success:
            raise RuntimeError(
                f"WeChat runner error {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        return {"to": target, "kind": kind, "status": "sent",
                "msg_id": data.get("msg_id")}

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> Dict[str, Any]:
        """Media not yet wired through iLink. The runner will 501; we
        surface that as a typed RuntimeError so callers can pivot to
        text or hold the media for the day media is added."""
        # Keep the signature so ChannelAdapter conformance is happy;
        # raise rather than fire a request we know will 501.
        _ = (data, mime_type, kind)
        runner_url, token, session_id = self._runner(cc)
        target = to or (cc.credentials or {}).get("default_target")
        if not target:
            raise RuntimeError("WeChat (personal) send_attachment needs a target.")
        if not url:
            raise RuntimeError(
                "WeChat (personal) send_attachment currently only supports url-based "
                "uploads — pass a public URL the runner can fetch."
            )
        # When media support lands in the runner, swap this to a real
        # POST /sessions/{sid}/messages with media_kind. For now,
        # explicit error so we don't pretend it worked.
        raise RuntimeError(
            "Media send isn't implemented for the iLink personal-account "
            "runner yet. Send text via send_text() and include the URL inline."
        )

        # Keep the unused params referenced so linters don't strip them.
        _ = (runner_url, token, session_id, caption)

    # ── Inbound ─────────────────────────────────────────────────────────

    async def verify_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> bool:
        """If the runner was given a bearer token, every inbound POST it
        makes back to us must echo it in ``Authorization: Bearer ...``."""
        expected = (cc.credentials or {}).get("bearer_token")
        if not expected:
            return True  # no shared secret configured — accept
        got = (headers.get("authorization") or headers.get("Authorization") or "").strip()
        return got == f"Bearer {expected}"

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        """Parse the JSON envelope the runner POSTs. Shape::

            {
              "kind":         "direct" | "group",
              "from":         "@xxx" | "@@yyy/@xxx",
              "from_name":    "Alice",
              "chat_id":      "@xxx" | "@@yyy",   # where to reply
              "text":         "hi",
              "message_type": "text" | "image" | "file" | ...,
              "msg_id":       "1234567890"
            }
        """
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None
        sender = (payload.get("from") or "").strip()
        if not sender:
            return None
        return NormalizedInbound(
            channel_type="wechat_personal",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender,
            sender_name=payload.get("from_name") or sender,
            reply_to=payload.get("chat_id") or sender,
            content=payload.get("text") or "",
            message_type=payload.get("message_type") or "text",
            external_message_id=payload.get("msg_id"),
            raw=payload,
        )

    # ── Provisioning ────────────────────────────────────────────────────

    async def register_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        """Tell the per-session runner where to push inbound messages
        and what bearer to sign them with. Idempotent — safe to call
        on every save.
        """
        try:
            runner_url, token, session_id = self._runner(cc)
        except RuntimeError as e:
            return {"registered": False, "reason": str(e)}

        s = get_settings()
        base = (s.PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            return {"registered": False, "reason": "PUBLIC_BASE_URL not set"}
        callback_url = f"{base}{self.webhook_path(cc)}"

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{runner_url}/sessions/{session_id}/config",
                    headers={**self._headers(token), "Content-Type": "application/json"},
                    json={"callback_url": callback_url, "bearer_token": token},
                )
        except httpx.ConnectError:
            return {"registered": False,
                    "reason": f"Cannot reach runner at {runner_url}"}
        except httpx.TimeoutException:
            return {"registered": False, "reason": "Runner timed out"}

        if resp.status_code == 404:
            return {"registered": False,
                    "reason": f"Runner has no session {session_id!r} — "
                              "user needs to re-scan the ClawBot QR."}
        if not resp.is_success:
            return {"registered": False,
                    "reason": f"Runner {resp.status_code}: {resp.text[:200]}"}
        return {"registered": True, "url": callback_url}


register_adapter(WeChatPersonalChannelAdapter())
