"""Facebook channel adapter — webhook receiver + outbound for Pages.

Inbound (Meta → Manor):
  Facebook Webhooks pushes a single envelope per change to
  ``POST /api/v1/channels/facebook/webhook?config_id=<cc.id>``.
  Two relevant ``object`` types:
    * ``page`` — feed events (new comment, new post mention)
    * ``messenger`` (object=page, field=messages) — DMs to the Page

  Verification: HMAC-SHA256 of the raw body using the Facebook App
  Secret, sent in ``X-Hub-Signature-256``. Without it we reject.

Outbound (Manor → Meta):
  ``send_text`` interpretation depends on what the inbound was:
    * Comment → reply by posting to ``/{comment_id}/comments``
    * Messenger DM → ``/{page_id}/me/messages``
  We keep both paths in this adapter; the channel_gateway picks
  ``reply_to`` from the parsed inbound and the adapter routes from there.

Credentials in ``ChannelConfig.credentials``::

    {
      "access_token":   "<user-or-page access token>",   # required
      "page_id":        "<numeric>",                      # default page
      "app_secret":     "<App Secret>",                   # webhook HMAC
      "verify_token":   "<arbitrary string>"              # webhook GET handshake
    }
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from packages.core.config import get_settings
from packages.core.models.channel import ChannelConfig
from packages.core.services.meta_graph import (
    MetaGraphClient, MetaGraphError, graph as _graph,
)
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)

logger = logging.getLogger(__name__)


# Adapter wants snappier 15s timeouts than the MCP wrapper's 30s
# (webhooks are latency-sensitive). One per-module client makes that
# explicit; the version still comes from the central pin.
_graph_quick = MetaGraphClient(timeout=15.0)
# Back-compat aliases so the existing send_text path stays readable.
_GraphError = MetaGraphError


class FacebookChannelAdapter(ChannelAdapter):
    channel_type = "facebook"

    # ── Helpers ────────────────────────────────────────────────────────

    def _creds(self, cc: ChannelConfig) -> dict:
        return cc.credentials or {}

    def _token(self, cc: ChannelConfig) -> str:
        c = self._creds(cc)
        token = c.get("page_access_token") or c.get("access_token")
        if not token:
            raise RuntimeError(
                "Facebook ChannelConfig missing access_token. Connect "
                "Facebook in Integrations first."
            )
        return token

    # ── Inbound ────────────────────────────────────────────────────────

    async def verify_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> bool:
        """Verify Meta's HMAC-SHA256 signature on the raw POST body.

        When ``app_secret`` isn't configured we still accept (dev mode);
        production deployments should always set it. The signature
        header is ``X-Hub-Signature-256: sha256=<hex>``.
        """
        secret = self._creds(cc).get("app_secret", "")
        if not secret:
            logger.debug("facebook: app_secret missing — skipping HMAC check")
            return True
        sig = (headers.get("x-hub-signature-256") or
               headers.get("X-Hub-Signature-256") or "")
        if not sig.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", sig)

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        """Pick the first interesting event out of the webhook envelope.

        Meta delivers a batch ``{object, entry: [{id, changes/messaging}]}``.
        We extract:
          * ``feed`` change with ``item == "comment"`` and
            ``verb == "add"`` → user-comment-on-page-post
          * ``messaging`` array with ``message.text`` → Messenger DM
        Status events / reactions / our own outbound echoes are ignored.
        """
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None

        for entry in payload.get("entry") or []:
            page_id = str(entry.get("id") or "")
            # 1) Messenger DMs come as ``messaging`` arrays
            for msg_event in entry.get("messaging") or []:
                if "message" not in msg_event:
                    continue
                if msg_event.get("message", {}).get("is_echo"):
                    continue
                sender = msg_event.get("sender", {}).get("id", "")
                text = msg_event.get("message", {}).get("text") or ""
                mid = msg_event.get("message", {}).get("mid")
                if not sender:
                    continue
                return NormalizedInbound(
                    channel_type="facebook",
                    channel_config_id=cc.id,
                    entity_id=cc.entity_id,
                    source_id=sender,
                    sender_name="",
                    reply_to=f"messenger:{page_id}:{sender}",
                    content=text,
                    message_type="text",
                    external_message_id=mid,
                    raw={"page_id": page_id, **msg_event},
                )

            # 2) Page feed events — comments on Page posts
            for change in entry.get("changes") or []:
                if change.get("field") != "feed":
                    continue
                value = change.get("value") or {}
                item = value.get("item")
                verb = value.get("verb")
                if item != "comment" or verb != "add":
                    continue
                # Skip the Page replying to itself.
                from_id = (value.get("from") or {}).get("id", "")
                if from_id and from_id == page_id:
                    continue
                comment_id = value.get("comment_id") or ""
                post_id = value.get("post_id") or ""
                content = value.get("message") or ""
                from_name = (value.get("from") or {}).get("name", "")
                if not comment_id:
                    continue
                return NormalizedInbound(
                    channel_type="facebook",
                    channel_config_id=cc.id,
                    entity_id=cc.entity_id,
                    source_id=from_id or comment_id,
                    sender_name=from_name,
                    # reply_to encodes the comment id for outbound; the
                    # adapter parses the prefix to decide which Graph
                    # endpoint to post to.
                    reply_to=f"comment:{page_id}:{comment_id}:{post_id}",
                    content=content,
                    message_type="text",
                    external_message_id=comment_id,
                    raw={"page_id": page_id, **value},
                )

        return None

    # ── Outbound ───────────────────────────────────────────────────────

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> dict:
        """Route reply based on the prefix encoded in ``to`` by parse_inbound.

        ``to`` shapes:
          * ``comment:{page_id}:{comment_id}:{post_id}`` — POST a child comment
          * ``messenger:{page_id}:{psid}`` — Messenger DM
          * a bare numeric id is treated as a Messenger PSID for the
            default page (legacy / direct invocation)
        """
        token = self._token(cc)
        creds = self._creds(cc)
        default_page = creds.get("page_id", "")

        if to.startswith("comment:"):
            _, _page_id, comment_id, _post_id = (to.split(":", 3) + ["", "", ""])[:4]
            return await _post(
                f"/{comment_id}/comments",
                {"message": text},
                token=token,
            )

        if to.startswith("messenger:"):
            _, page_id, psid = (to.split(":", 2) + ["", ""])[:3]
            return await _post(
                f"/{page_id or default_page}/messages",
                {
                    "recipient": {"id": psid},
                    "message": {"text": text},
                    "messaging_type": "RESPONSE",
                },
                token=token,
                json_body=True,
            )

        # Bare PSID fallback — assume Messenger to the default page.
        if not default_page:
            raise RuntimeError(
                f"Facebook send_text called with bare target {to!r} but "
                "no default page_id on the ChannelConfig."
            )
        return await _post(
            f"/{default_page}/messages",
            {
                "recipient": {"id": to},
                "message": {"text": text},
                "messaging_type": "RESPONSE",
            },
            token=token,
            json_body=True,
        )

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> dict:
        # Comments don't take attachments via the API; Messenger DMs do
        # but only in the 24h window. Punt for v1.
        raise NotImplementedError(
            "Facebook send_attachment isn't implemented yet. "
            "Inline image URLs in the post body via mcp__facebook__create_post.",
        )

    # ── Provisioning ───────────────────────────────────────────────────

    async def register_webhook(self, cc: ChannelConfig) -> dict:
        """Tell Meta which fields to push for our subscription.

        We don't manage the App-level webhook here (that's a one-time
        admin action in developers.facebook.com → Webhooks). What we
        DO is subscribe THIS Page to the App's webhook so the App's
        callback URL receives feed/messages events for this page.

        Idempotent: re-running just refreshes the subscription.
        """
        creds = self._creds(cc)
        page_id = creds.get("page_id")
        token = self._token(cc)
        if not page_id:
            return {"registered": False, "reason": "page_id missing on ChannelConfig"}

        s = get_settings()
        base = (s.PUBLIC_BASE_URL or "").rstrip("/")
        callback_url = f"{base}{self.webhook_path(cc)}" if base else None

        try:
            await _post(
                f"/{page_id}/subscribed_apps",
                {"subscribed_fields": "feed,messages,messaging_postbacks"},
                token=token,
            )
        except _GraphError as exc:
            return {"registered": False, "reason": str(exc)}

        return {
            "registered": True,
            "url": callback_url,
            "page_id": page_id,
            "fields": "feed,messages,messaging_postbacks",
        }


# ── HTTP helpers — thin alias over the shared MetaGraphClient ─────────

async def _post(path: str, body: dict, *, token: str, json_body: bool = False) -> dict:
    return await _graph_quick.post(path, body, token=token, json_body=json_body)


register_adapter(FacebookChannelAdapter())
