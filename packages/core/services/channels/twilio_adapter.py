"""Twilio SMS + Voice channel adapter.

Handles:
- Sending SMS messages via Twilio REST API
- Initiating outbound voice calls
- Receiving inbound SMS and voice webhooks
- Webhook signature validation (HMAC-SHA1)
- Phone number listing and usage reporting

Configuration:
  Credentials are leased from the ChannelConfig's source Integration:
    account_sid  — Twilio Account SID
    auth_token   — Twilio Auth Token
    from_number  — Default outbound phone number (E.164 format)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any, Optional as _Optional

from packages.core.models.channel import ChannelConfig as _CC
from packages.core.services.channels.base import (
    ChannelAdapter,
    ChannelTextSendError,
    ChannelTextSendResultStatus,
    NormalizedInbound,
    channel_text_send_result,
    register_adapter,
)

logger = logging.getLogger(__name__)

# Optional dependency — fail gracefully
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioAdapter:
    """Adapter for Twilio SMS and Voice APIs.

    Uses httpx for async HTTP calls (no Twilio SDK dependency required).
    All methods return normalised dicts compatible with channel_service.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    # ------------------------------------------------------------------
    # Outbound SMS
    # ------------------------------------------------------------------

    async def send_sms(self, to: str, body: str) -> dict[str, Any]:
        """Send SMS via Twilio REST API.

        POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json

        Returns:
            {
                "external_id": str,   # MessageSid
                "status": str,        # queued, sent, etc.
                "from_address": str,
                "to_address": str,
                "raw": dict,
            }
        """
        if httpx is None:
            raise ChannelTextSendError.determinate(
                "httpx is not installed. Run: pip install httpx"
            )
        to = str(to or "").strip()
        if not to:
            raise ValueError("Twilio SMS recipient must not be blank")
        if not str(body or "").strip():
            raise ValueError("Twilio SMS body must not be blank")

        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Messages.json"
        payload = {
            "To": to,
            "From": self.from_number,
            "Body": body,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data=payload,
                auth=(self.account_sid, self.auth_token),
            )
            data = resp.json()

        if resp.status_code >= 400:
            error_msg = data.get("message", resp.text)
            logger.error("Twilio send_sms failed: status=%s error=%s", resp.status_code, error_msg)
            raise ChannelTextSendError.from_http_status(
                f"Twilio API error {resp.status_code}: {error_msg}",
                status_code=resp.status_code,
            )

        return channel_text_send_result(
            ChannelTextSendResultStatus.QUEUED,
            external_id=data.get("sid", ""),
            provider_status=data.get("status", ""),
            from_address=data.get("from", self.from_number),
            to_address=data.get("to", to),
            raw=data,
        )

    # ------------------------------------------------------------------
    # Outbound voice call
    # ------------------------------------------------------------------

    async def make_call(
        self,
        to: str,
        twiml_url: str,
        *,
        status_callback_url: str | None = None,
        status_callback_events: tuple[str, ...] = (
            "initiated",
            "ringing",
            "answered",
            "completed",
        ),
    ) -> dict[str, Any]:
        """Initiate voice call via Twilio REST API.

        POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json

        Returns:
            {
                "external_id": str,   # CallSid
                "status": str,        # queued, ringing, etc.
                "from_address": str,
                "to_address": str,
                "raw": dict,
            }
        """
        if httpx is None:
            raise ChannelTextSendError.determinate(
                "httpx is not installed. Run: pip install httpx"
            )
        to = str(to or "").strip()
        twiml_url = str(twiml_url or "").strip()
        if not to:
            raise ValueError("Twilio call recipient must not be blank")
        if not twiml_url:
            raise ValueError("Twilio TwiML URL must not be blank")

        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Calls.json"
        payload: dict[str, Any] = {
            "To": to,
            "From": self.from_number,
            "Url": twiml_url,
        }
        if status_callback_url:
            payload["StatusCallback"] = str(status_callback_url).strip()
            payload["StatusCallbackMethod"] = "POST"
            payload["StatusCallbackEvent"] = [
                event
                for raw_event in status_callback_events
                if (event := str(raw_event).strip())
            ]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data=payload,
                auth=(self.account_sid, self.auth_token),
            )
            data = resp.json()

        if resp.status_code >= 400:
            error_msg = data.get("message", resp.text)
            logger.error("Twilio make_call failed: status=%s error=%s", resp.status_code, error_msg)
            raise ChannelTextSendError.from_http_status(
                f"Twilio API error {resp.status_code}: {error_msg}",
                status_code=resp.status_code,
            )

        return channel_text_send_result(
            ChannelTextSendResultStatus.QUEUED,
            external_id=data.get("sid", ""),
            provider_status=data.get("status", ""),
            from_address=data.get("from", self.from_number),
            to_address=data.get("to", to),
            raw=data,
        )

    # ------------------------------------------------------------------
    # Inbound webhook handling
    # ------------------------------------------------------------------

    async def handle_sms_webhook(self, form_data: dict[str, str]) -> dict[str, Any]:
        """Parse incoming SMS webhook from Twilio.

        Twilio POSTs form-encoded data with fields:
            From, To, Body, MessageSid, NumMedia, MediaUrl0, etc.

        Returns normalised message dict:
            {
                "sender_id": str,
                "message_type": str,    # text | media
                "content": str,
                "raw": dict,
                "channel": "twilio_sms",
            }
        """
        sender = form_data.get("From", "")
        recipient = form_data.get("To", "")
        body = form_data.get("Body", "")
        message_sid = form_data.get("MessageSid", "")
        num_media = int(form_data.get("NumMedia", "0"))

        # Collect media URLs if present
        media_urls: list[str] = []
        for i in range(num_media):
            media_url = form_data.get(f"MediaUrl{i}", "")
            if media_url:
                media_urls.append(media_url)

        message_type = "media" if num_media > 0 else "text"
        content = body
        if media_urls and not body:
            content = f"[Media: {', '.join(media_urls)}]"

        return {
            "sender_id": sender,
            "recipient_id": recipient,
            "message_type": message_type,
            "content": content,
            "msg_id": message_sid,
            "media_urls": media_urls,
            "raw": dict(form_data),
            "channel": "twilio_sms",
        }

    async def handle_voice_webhook(self, form_data: dict[str, str]) -> str:
        """Handle incoming voice call, return TwiML response.

        Twilio POSTs form-encoded data with fields:
            CallSid, From, To, CallStatus, Direction, etc.

        Returns TwiML XML string for Twilio to execute.
        Default behaviour: announce a greeting and hang up.
        Override this method or configure TwiML in channel config for
        custom IVR menus, call forwarding, etc.
        """
        caller = form_data.get("From", "unknown")
        logger.info("Incoming voice call from %s (CallSid=%s)", caller, form_data.get("CallSid", ""))

        # Default TwiML: greet and hang up
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Say voice=\"alice\">Thank you for calling. "
            "Your call is important to us. Please leave a message after the beep.</Say>"
            "<Record maxLength=\"120\" action=\"/api/v1/channels/twilio/recording\" />"
            "<Say voice=\"alice\">We did not receive a recording. Goodbye.</Say>"
            "</Response>"
        )
        return twiml

    # ------------------------------------------------------------------
    # Webhook signature validation
    # ------------------------------------------------------------------

    async def validate_signature(
        self,
        url: str,
        params: dict[str, str],
        signature: str,
    ) -> bool:
        """Validate Twilio webhook signature.

        Twilio signs requests with HMAC-SHA1 using the auth token.
        The signature is computed over the full URL + sorted POST parameters.

        See: https://www.twilio.com/docs/usage/security#validating-requests
        """
        # Build the data string: URL + sorted key/value pairs
        data_str = url
        for key in sorted(params.keys()):
            data_str += key + params[key]

        # Compute HMAC-SHA1
        expected = base64.b64encode(
            hmac.new(
                self.auth_token.encode("utf-8"),
                data_str.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")

        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Phone number listing
    # ------------------------------------------------------------------

    async def list_phone_numbers(self) -> list[dict[str, Any]]:
        """List purchased phone numbers on this Twilio account.

        GET https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json

        Returns list of dicts with phone_number, sid, capabilities, etc.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/IncomingPhoneNumbers.json"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, auth=(self.account_sid, self.auth_token))
            data = resp.json()

        if resp.status_code >= 400:
            error_msg = data.get("message", resp.text)
            raise RuntimeError(f"Twilio API error {resp.status_code}: {error_msg}")

        numbers = []
        for item in data.get("incoming_phone_numbers", []):
            numbers.append({
                "sid": item.get("sid", ""),
                "phone_number": item.get("phone_number", ""),
                "friendly_name": item.get("friendly_name", ""),
                "capabilities": item.get("capabilities", {}),
                "status": item.get("status", ""),
            })

        return numbers

    # ------------------------------------------------------------------
    # Usage reporting
    # ------------------------------------------------------------------

    async def get_usage(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Get usage records for billing.

        GET https://api.twilio.com/2010-04-01/Accounts/{sid}/Usage/Records.json

        Args:
            start_date: YYYY-MM-DD format
            end_date: YYYY-MM-DD format

        Returns dict with usage categories and amounts.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        url = f"{TWILIO_API_BASE}/Accounts/{self.account_sid}/Usage/Records.json"
        params = {
            "StartDate": start_date,
            "EndDate": end_date,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                params=params,
                auth=(self.account_sid, self.auth_token),
            )
            data = resp.json()

        if resp.status_code >= 400:
            error_msg = data.get("message", resp.text)
            raise RuntimeError(f"Twilio API error {resp.status_code}: {error_msg}")

        records = []
        total_cost = 0.0
        for record in data.get("usage_records", []):
            price = float(record.get("price", 0))
            total_cost += price
            records.append({
                "category": record.get("category", ""),
                "description": record.get("description", ""),
                "count": record.get("count", 0),
                "count_unit": record.get("count_unit", ""),
                "price": price,
                "price_unit": record.get("price_unit", "USD"),
            })

        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_cost": total_cost,
            "currency": "USD",
            "records": records,
        }


# ── Polymorphic ChannelAdapter wrappers ─────────────────────────────────────


async def _twilio(cc: _CC, *, reason: str) -> TwilioAdapter:
    from packages.core.services.channel_credentials import (
        lease_channel_config_credentials,
    )

    try:
        creds = await lease_channel_config_credentials(cc, reason=reason)
    except ValueError as exc:
        raise ChannelTextSendError.determinate(str(exc)) from exc
    sid = creds.get("account_sid")
    auth = creds.get("auth_token")
    from_num = creds.get("phone_number") or creds.get("from_number")
    if not (sid and auth):
        raise ChannelTextSendError.determinate(
            "Twilio credential source is missing account_sid / auth_token"
        )
    return TwilioAdapter(account_sid=sid, auth_token=auth, from_number=from_num or "")


def _parse_form(body: bytes) -> dict[str, str]:
    # Twilio posts application/x-www-form-urlencoded
    from urllib.parse import parse_qsl
    try:
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except Exception:
        return {}


class TwilioSMSChannelAdapter(ChannelAdapter):
    channel_type = "twilio_sms"

    def webhook_path(self, cc: _CC) -> str:
        return f"/api/v1/channels/twilio/sms?config_id={cc.id}"

    async def send_text(self, cc: _CC, to: str, text: str, **kwargs: Any) -> dict[str, Any]:
        adapter = await _twilio(cc, reason="channel.twilio_sms.send_text")
        try:
            return await adapter.send_sms(to, text)
        except ValueError as exc:
            raise ChannelTextSendError.determinate(str(exc)) from exc

    async def parse_inbound(self, cc: _CC, *, headers, query, body) -> _Optional[NormalizedInbound]:
        form = _parse_form(body)
        sender = form.get("From", "")
        if not sender:
            return None
        return NormalizedInbound(
            channel_type="twilio_sms",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender,
            reply_to=sender,
            content=form.get("Body", ""),
            message_type="text",
            external_message_id=form.get("MessageSid"),
            raw=form,
        )


class TwilioVoiceChannelAdapter(ChannelAdapter):
    """Twilio Voice registration and compatibility adapter.

    Live speech is delivered by the Media Streams websocket and therefore
    bypasses ``send_text``. Outbound calls should use the Twilio MCP
    ``make_call`` operation, which creates the authenticated call session and
    Manor-owned TwiML URL before calling Twilio.
    """
    channel_type = "twilio_voice"

    def webhook_path(self, cc: _CC) -> str:
        return f"/api/v1/channels/twilio/voice?config_id={cc.id}"

    async def send_text(self, cc: _CC, to: str, text: str, **kwargs: Any) -> dict[str, Any]:
        # Kept for legacy callers that explicitly provide their own TwiML
        # URL. The Agent-facing path is MCP ``make_call`` above.
        twiml_url = kwargs.get("twiml_url")
        if not twiml_url:
            return channel_text_send_result(
                ChannelTextSendResultStatus.DEFERRED,
                reason="no twiml_url — supply to place call",
                text=text,
            )
        adapter = await _twilio(cc, reason="channel.twilio_voice.send_text")
        try:
            return await adapter.make_call(to, twiml_url)
        except ValueError as exc:
            raise ChannelTextSendError.determinate(str(exc)) from exc

    async def parse_inbound(self, cc: _CC, *, headers, query, body) -> _Optional[NormalizedInbound]:
        form = _parse_form(body)
        caller = form.get("From", "")
        if not caller:
            return None
        # Inbound speech transcript comes from <Gather input="speech">
        transcript = form.get("SpeechResult") or form.get("Body") or ""
        return NormalizedInbound(
            channel_type="twilio_voice",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=caller,
            reply_to=caller,
            content=transcript,
            message_type="voice",
            external_message_id=form.get("CallSid"),
            raw=form,
        )


register_adapter(TwilioSMSChannelAdapter())
register_adapter(TwilioVoiceChannelAdapter())
