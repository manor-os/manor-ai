"""Microsoft Graph Outlook Mail channel adapter.

Graph change notifications arrive at one fixed Manor endpoint. The
subscription's ``clientState`` and id are the authentication boundary; the
delegated Graph token is leased from the user-owned ChannelConfig source.
"""
from __future__ import annotations

import html
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Optional
from urllib.parse import quote

from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter,
    ChannelTextSendError,
    NormalizedInbound,
    register_adapter,
)

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_GRAPH_API = "https://graph.microsoft.com/v1.0"
_NOTIFICATION_PATH = "/api/v1/channels/outlook/notifications"
_SUBSCRIPTION_LIFETIME = timedelta(days=2)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(content: object, content_type: object = "text") -> str:
    value = html.unescape(str(content or "").strip())
    if str(content_type or "text").lower() != "html":
        return value
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


class OutlookChannelAdapter(ChannelAdapter):
    channel_type = "outlook"

    def webhook_path(self, cc: ChannelConfig) -> str:
        return _NOTIFICATION_PATH

    @staticmethod
    def notification_matches_subscription(
        cc: ChannelConfig, notification: dict,
    ) -> bool:
        """Check the Graph routing secret before any token-backed hydration."""
        config = cc.config if isinstance(cc.config, dict) else {}
        expected_state = str(config.get("outlook_client_state") or "")
        received_state = str(notification.get("clientState") or "")
        expected_subscription = str(config.get("outlook_subscription_id") or "")
        return bool(
            expected_state
            and received_state
            and expected_subscription
            and hmac.compare_digest(expected_state, received_state)
            and str(notification.get("subscriptionId") or "") == expected_subscription
            and str(notification.get("changeType") or "created") == "created"
        )

    async def _request(
        self,
        cc: ChannelConfig,
        credentials: dict,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> dict:
        if httpx is None:
            raise ChannelTextSendError.determinate(
                "httpx is required — pip install httpx"
            )
        path = path.lstrip("/")
        access_token = str(
            credentials.get("access_token") or credentials.get("api_token") or ""
        ).strip()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if access_token:
            url = f"{_GRAPH_API}/{path}"
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            if credentials.get("via") != "nango":
                raise ChannelTextSendError.determinate(
                    "Outlook credential source has no access token"
                )
            from packages.core.ai.mcp.nango import _NANGO_BASE, get_nango_secret
            from packages.core.database import async_session
            from packages.core.models.document import Integration

            async with async_session() as db:
                source = await db.get(Integration, cc.credential_source_id)
                secret = await get_nango_secret(db, cc.entity_id)
            nango_config = (source.config or {}).get("nango", {}) if source else {}
            provider_key = (
                credentials.get("provider_config_key")
                or nango_config.get("provider_config_key")
                or "outlook"
            )
            connection_id = credentials.get("connection_id") or nango_config.get("connection_id")
            if not secret or not connection_id:
                raise ChannelTextSendError.determinate(
                    "Outlook Nango connection is unavailable"
                )
            url = f"{_NANGO_BASE}/proxy/v2/{path}"
            headers.update({
                "Authorization": f"Bearer {secret}",
                "Provider-Config-Key": str(provider_key),
                "Connection-Id": str(connection_id),
                "Nango-Proxy-Accept": "application/json",
            })
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, url, headers=headers, json=body)
        if response.status_code in (401, 403):
            raise ChannelTextSendError.determinate(
                "Outlook authorization failed; reconnect the account"
            )
        if not response.is_success:
            raise ChannelTextSendError.from_http_status(
                f"Microsoft Graph error {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        if not response.text:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        """Reply to the original Graph message, preserving its thread."""
        credentials = await self.credentials(cc, reason="channel.outlook.send_text")
        message_id = str(kwargs.get("message_id") or to or "").strip()
        if not message_id:
            raise ChannelTextSendError.determinate(
                "Outlook reply requires a Graph message id"
            )
        data = await self._request(
            cc,
            credentials,
            "POST",
            f"me/messages/{quote(message_id, safe='')}/reply",
            body={"comment": text},
        )
        return {
            "to": message_id,
            "external_id": str(data.get("id") or ""),
            "status": "sent",
            "raw": data,
        }

    async def verify_inbound(self, cc: ChannelConfig, *, headers, query, body) -> bool:
        # Graph authenticates each notification through clientState; parsing
        # performs the constant-time comparison against the stored state.
        return True

    def parse_notification(
        self, cc: ChannelConfig, notification: dict,
    ) -> Optional[NormalizedInbound]:
        config = cc.config if isinstance(cc.config, dict) else {}
        if not self.notification_matches_subscription(cc, notification):
            return None

        data = notification.get("resourceData") or {}
        sender_block = data.get("from") or {}
        sender_email = sender_block.get("emailAddress") or {}
        sender_id = str(sender_email.get("address") or sender_block.get("user", {}).get("id") or "").strip()
        sender_name = str(sender_email.get("name") or sender_block.get("user", {}).get("displayName") or "").strip()
        self_email = str(config.get("outlook_user_email") or "").strip().lower()
        self_id = str(config.get("outlook_user_id") or "").strip()
        if not sender_id or (self_email and sender_id.lower() == self_email) or (
            self_id and hmac.compare_digest(sender_id, self_id)
        ):
            return None

        message_id = str(data.get("id") or "").strip()
        content_block = data.get("body") or {}
        content = _plain_text(content_block.get("content"), content_block.get("contentType"))
        if not content:
            content = str(data.get("bodyPreview") or "").strip()
        if not message_id or not content:
            return None
        raw = dict(notification)
        # Keep the common thread fields available without forcing downstream
        # callers to know Graph's resourceData envelope shape.
        raw["conversationId"] = data.get("conversationId")
        raw["subject"] = data.get("subject")
        return NormalizedInbound(
            channel_type=self.channel_type,
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender_id,
            sender_name=sender_name or None,
            reply_to=message_id,
            content=content,
            message_type="text",
            external_message_id=message_id,
            raw=raw,
        )

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("value"), list):
            payload = next((item for item in payload["value"] if isinstance(item, dict)), {})
        return self.parse_notification(cc, payload if isinstance(payload, dict) else {})

    async def hydrate_notification(self, cc: ChannelConfig, notification: dict) -> dict:
        """Hydrate a notification whose resourceData omits the message body."""
        resource_data = notification.get("resourceData") or {}
        if resource_data.get("body") or resource_data.get("bodyPreview"):
            return notification
        resource = str(notification.get("resource") or "").strip("/")
        if not resource:
            return notification
        credentials = await self.credentials(cc, reason="channel.outlook.hydrate_notification")
        data = await self._request(cc, credentials, "GET", resource)
        return {**notification, "resourceData": data}

    async def register_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        from packages.core.config import get_settings

        base = (get_settings().PUBLIC_BASE_URL or "").rstrip("/")
        if not base.startswith("https://"):
            return {"registered": False, "reason": "PUBLIC_BASE_URL must be HTTPS"}
        credentials = await self.credentials(cc, reason="channel.outlook.register_webhook")
        current = dict(cc.config or {})
        subscription_id = str(current.get("outlook_subscription_id") or "").strip()
        expiration = datetime.now(timezone.utc) + _SUBSCRIPTION_LIFETIME
        if subscription_id:
            try:
                data = await self._request(
                    cc,
                    credentials,
                    "PATCH",
                    f"subscriptions/{quote(subscription_id, safe='')}",
                    body={"expirationDateTime": expiration.isoformat().replace("+00:00", "Z")},
                )
            except RuntimeError as exc:
                if "Microsoft Graph error 404" not in str(exc):
                    raise
                # The remote subscription may have expired or been deleted.
                # Clear the stale id so the next branch creates a replacement;
                # keep client state so already queued notifications remain
                # bound to the same channel secret.
                cc.config = {
                    **current,
                    "outlook_subscription_id": None,
                    "outlook_subscription_expiration": None,
                }
            else:
                cc.config = {
                    **current,
                    "outlook_subscription_expiration": str(
                        data.get("expirationDateTime") or expiration.isoformat()
                    ),
                    "outlook_registration_pending": False,
                }
                return {"registered": True, "subscription_id": subscription_id, "renewed": True}

        user_id = str(current.get("outlook_user_id") or "").strip()
        if not user_id:
            user_id = str((await self._request(cc, credentials, "GET", "me")).get("id") or "")
        if not user_id:
            raise RuntimeError("Microsoft Graph did not return the connected Outlook user id")
        client_state = str(current.get("outlook_client_state") or secrets.token_urlsafe(32))
        data = await self._request(
            cc,
            credentials,
            "POST",
            "subscriptions",
            body={
                "changeType": "created",
                "notificationUrl": f"{base}{_NOTIFICATION_PATH}?config_id={cc.id}",
                "resource": f"/users/{user_id}/mailFolders('inbox')/messages",
                "expirationDateTime": expiration.isoformat().replace("+00:00", "Z"),
                "clientState": client_state,
            },
        )
        created_id = str(data.get("id") or "").strip()
        if not created_id:
            raise RuntimeError("Microsoft Graph did not return an Outlook subscription id")
        cc.config = {
            **current,
            "outlook_user_id": user_id,
            "outlook_subscription_id": created_id,
            "outlook_client_state": client_state,
            "outlook_subscription_expiration": str(
                data.get("expirationDateTime") or expiration.isoformat()
            ),
            "outlook_registration_pending": False,
        }
        return {"registered": True, "subscription_id": created_id}

    async def unregister_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        subscription_id = str((cc.config or {}).get("outlook_subscription_id") or "").strip()
        if not subscription_id:
            return {"unregistered": True, "reason": "no active subscription"}
        credentials = await self.credentials(cc, reason="channel.outlook.unregister_webhook")
        await self._request(
            cc,
            credentials,
            "DELETE",
            f"subscriptions/{quote(subscription_id, safe='')}",
        )
        cc.config = {
            **(cc.config or {}),
            "outlook_subscription_id": None,
            "outlook_subscription_expiration": None,
            "outlook_registration_pending": False,
        }
        return {"unregistered": True, "subscription_id": subscription_id}


register_adapter(OutlookChannelAdapter())
