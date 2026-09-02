"""Microsoft Graph chat channel adapter.

Nango supplies the delegated Microsoft token reference. Graph change
notifications still arrive directly at Manor's fixed endpoint, matching the
Slack/Discord channel contract.
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
_NOTIFICATION_PATH = "/api/v1/channels/ms_teams/notifications"
_SUBSCRIPTION_LIFETIME = timedelta(days=2)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(content: object, content_type: object = "text") -> str:
    value = html.unescape(str(content or "")).strip()
    if str(content_type).lower() != "html":
        return value
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def _chat_id_from_resource(resource: object) -> str:
    bits = [part for part in str(resource or "").strip("/").split("/") if part]
    try:
        return bits[bits.index("chats") + 1]
    except (ValueError, IndexError):
        return ""


class TeamsChannelAdapter(ChannelAdapter):
    channel_type = "ms_teams"

    def webhook_path(self, cc: ChannelConfig) -> str:
        return _NOTIFICATION_PATH

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
        access_token = credentials.get("access_token") or credentials.get("api_token")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if access_token:
            url = f"{_GRAPH_API}/{path}"
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            if credentials.get("via") != "nango":
                raise ChannelTextSendError.determinate(
                    "Microsoft Teams credential source has no access token"
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
                or "ms_teams"
            )
            connection_id = credentials.get("connection_id") or nango_config.get("connection_id")
            if not secret or not connection_id:
                raise ChannelTextSendError.determinate(
                    "Microsoft Teams Nango connection is unavailable"
                )
            url = f"{_NANGO_BASE}/proxy/v2/{path}"
            headers.update({
                "Authorization": f"Bearer {secret}",
                "Provider-Config-Key": str(provider_key),
                "Connection-Id": str(connection_id),
                "Nango-Proxy-Accept": "application/json",
            })
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(method, url, headers=headers, json=body)
        if response.status_code in (401, 403):
            raise ChannelTextSendError.determinate(
                "Microsoft Teams authorization failed; reconnect the account"
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

    async def send_text(self, cc: ChannelConfig, to: str, text: str, **kwargs: Any) -> Dict[str, Any]:
        credentials = await self.credentials(cc, reason="channel.ms_teams.send_text")
        data = await self._request(
            cc,
            credentials,
            "POST",
            f"chats/{quote(str(to), safe='')}/messages",
            body={"body": {"contentType": "text", "content": text}},
        )
        return {
            "to": to,
            "external_id": str(data.get("id") or ""),
            "status": "sent",
            "raw": data,
        }

    async def verify_inbound(self, cc: ChannelConfig, *, headers, query, body) -> bool:
        # Graph authenticates the notification through the subscription's
        # clientState; the router performs the constant-time comparison.
        return True

    def parse_notification(
        self, cc: ChannelConfig, notification: dict,
    ) -> Optional[NormalizedInbound]:
        expected_state = str((cc.config or {}).get("teams_client_state") or "")
        received_state = str(notification.get("clientState") or "")
        if not expected_state or not received_state or not hmac.compare_digest(
            expected_state, received_state,
        ):
            return None
        expected_subscription = str((cc.config or {}).get("teams_subscription_id") or "")
        if not expected_subscription or str(notification.get("subscriptionId") or "") != expected_subscription:
            return None
        if str(notification.get("changeType") or "created") != "created":
            return None

        data = notification.get("resourceData") or {}
        sender_block = data.get("from") or {}
        sender_user = sender_block.get("user") or {}
        sender_id = str(sender_user.get("id") or sender_user.get("userPrincipalName") or "").strip()
        self_id = str((cc.config or {}).get("teams_user_id") or "").strip()
        if not sender_id or (self_id and hmac.compare_digest(sender_id, self_id)):
            return None
        chat_id = _chat_id_from_resource(notification.get("resource"))
        if not chat_id:
            chat_id = str(data.get("chatId") or "").strip()
        content_block = data.get("body") or {}
        content = _plain_text(content_block.get("content"), content_block.get("contentType"))
        message_id = str(data.get("id") or "").strip()
        if not chat_id or not message_id or not content:
            return None
        return NormalizedInbound(
            channel_type=self.channel_type,
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender_id,
            sender_name=str(sender_user.get("displayName") or "") or None,
            reply_to=chat_id,
            content=content,
            message_type="text",
            external_message_id=message_id,
            raw=notification,
        )

    async def parse_inbound(self, cc: ChannelConfig, *, headers, query, body) -> Optional[NormalizedInbound]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("value"), list):
            payload = next((item for item in payload["value"] if isinstance(item, dict)), {})
        return self.parse_notification(cc, payload if isinstance(payload, dict) else {})

    async def hydrate_notification(self, cc: ChannelConfig, notification: dict) -> dict:
        """Fetch the message when a subscription omits resource data."""
        if (notification.get("resourceData") or {}).get("body"):
            return notification
        resource = str(notification.get("resource") or "").strip("/")
        if not resource:
            return notification
        credentials = await self.credentials(cc, reason="channel.ms_teams.hydrate_notification")
        data = await self._request(cc, credentials, "GET", resource)
        return {**notification, "resourceData": data}

    async def register_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        from packages.core.config import get_settings

        base = (get_settings().PUBLIC_BASE_URL or "").rstrip("/")
        if not base.startswith("https://"):
            return {"registered": False, "reason": "PUBLIC_BASE_URL must be HTTPS"}
        credentials = await self.credentials(cc, reason="channel.ms_teams.register_webhook")
        current = dict(cc.config or {})
        existing_subscription = str(current.get("teams_subscription_id") or "")
        if existing_subscription:
            expiration = datetime.now(timezone.utc) + _SUBSCRIPTION_LIFETIME
            try:
                data = await self._request(
                    cc,
                    credentials,
                    "PATCH",
                    f"subscriptions/{quote(existing_subscription, safe='')}",
                    body={"expirationDateTime": expiration.isoformat().replace("+00:00", "Z")},
                )
            except RuntimeError as exc:
                if "Microsoft Graph error 404" not in str(exc):
                    raise
                # The remote subscription may have expired or been deleted.
                # Clear the stale id so the create path registers a replacement.
                cc.config = {
                    **current,
                    "teams_subscription_id": None,
                    "teams_subscription_expiration": None,
                }
            else:
                cc.config = {
                    **current,
                    "teams_subscription_expiration": str(
                        data.get("expirationDateTime") or expiration.isoformat()
                    ),
                    "teams_registration_pending": False,
                }
                return {"registered": True, "subscription_id": existing_subscription, "renewed": True}

        user_id = str(current.get("teams_user_id") or "")
        if not user_id:
            user_id = str((await self._request(cc, credentials, "GET", "me")).get("id") or "")
        if not user_id:
            raise RuntimeError("Microsoft Graph did not return the connected user id")
        client_state = str(current.get("teams_client_state") or secrets.token_urlsafe(32))
        expiration = datetime.now(timezone.utc) + _SUBSCRIPTION_LIFETIME
        data = await self._request(
            cc,
            credentials,
            "POST",
            "subscriptions",
            body={
                "changeType": "created",
                "notificationUrl": f"{base}{_NOTIFICATION_PATH}",
                "resource": f"/users/{user_id}/chats/getAllMessages",
                "expirationDateTime": expiration.isoformat().replace("+00:00", "Z"),
                "clientState": client_state,
            },
        )
        cc.config = {
            **current,
            "teams_user_id": user_id,
            "teams_subscription_id": str(data.get("id") or ""),
            "teams_client_state": client_state,
            "teams_subscription_expiration": str(data.get("expirationDateTime") or expiration.isoformat()),
            "teams_registration_pending": False,
        }
        return {"registered": bool(data.get("id")), "subscription_id": data.get("id")}

    async def unregister_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        subscription_id = str((cc.config or {}).get("teams_subscription_id") or "")
        if not subscription_id:
            return {"unregistered": False, "reason": "no subscription"}
        credentials = await self.credentials(cc, reason="channel.ms_teams.unregister_webhook")
        await self._request(cc, credentials, "DELETE", f"subscriptions/{quote(subscription_id, safe='')}")
        cc.config = {key: value for key, value in (cc.config or {}).items() if not key.startswith("teams_subscription_")}
        return {"unregistered": True}


register_adapter(TeamsChannelAdapter())
