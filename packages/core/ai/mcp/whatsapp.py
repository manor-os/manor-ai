"""WhatsApp Business Cloud API MCP server.

This module exposes the production WhatsApp operations Manor agents need for
customer messaging and Meta App Review:

* verify the connected business phone number and business profile
* send text, template, image, document, audio, and video messages
* mark an inbound message as read
* list, create, and delete WhatsApp message templates

Credentials are stored as one encrypted integration JSON blob and delivered
to ``call_tool`` through ``bearer_token``::

    {
      "access_token": "...",
      "phone_number_id": "...",
      "waba_id": "...",
      "verify_token": "...",       # optional, inbound webhook handshake
      "app_secret": "..."          # optional, inbound signature validation
    }

Legacy ``api_key`` / ``phone_id`` aliases remain readable so existing
integrations do not break when the UI migrates to the native field names.

Meta permissions required for production users:

* ``whatsapp_business_messaging`` — send messages and read receipts
* ``whatsapp_business_management`` — phone/profile and template management
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from packages.core.ai.mcp._http import mcp_err as _err, mcp_ok as _ok
from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter
from packages.core.services.meta_graph import MetaGraphError, graph as _graph

logger = logging.getLogger(__name__)


def _prop(description: str, type_: str = "string", **extra: Any) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": type_, "description": description}
    schema.update(extra)
    return schema


_COMPONENTS = _prop(
    "Meta template components (HEADER, BODY, FOOTER, or BUTTONS).",
    "array",
    items={"type": "object"},
)


_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_phone_number": {
        "description": (
            "Verify the connected WhatsApp Business phone number and return "
            "its display number, verified name, quality rating, and status."
        ),
        "properties": {},
        "required": [],
    },
    "list_phone_numbers": {
        "description": "List phone numbers owned by the configured WhatsApp Business Account.",
        "properties": {
            "limit": _prop("Maximum phone numbers to return (1-100, default 25).", "integer"),
        },
        "required": [],
    },
    "get_business_profile": {
        "description": "Read the public WhatsApp Business profile for the connected number.",
        "properties": {},
        "required": [],
    },
    "update_business_profile": {
        "description": (
            "Update selected public WhatsApp Business profile fields. Only provided "
            "fields are changed. Requires whatsapp_business_management."
        ),
        "properties": {
            "about": _prop("Short profile about text."),
            "address": _prop("Business address."),
            "description": _prop("Longer business description."),
            "email": _prop("Public business email."),
            "websites": _prop(
                "Public business website URLs (maximum two).",
                "array",
                items={"type": "string"},
            ),
            "vertical": _prop("Meta business vertical/category value."),
        },
        "required": [],
    },
    "send_text": {
        "description": (
            "Send a WhatsApp text message. Free-form text is allowed only inside "
            "Meta's customer-service window; use send_template to initiate a conversation."
        ),
        "properties": {
            "to": _prop("Recipient phone number in international format, digits only or E.164."),
            "text": _prop("Message text."),
        },
        "required": ["to", "text"],
    },
    "send_template": {
        "description": "Send an approved WhatsApp message template to a recipient.",
        "properties": {
            "to": _prop("Recipient phone number in international format, digits only or E.164."),
            "template_name": _prop("Approved template name."),
            "language": _prop("Template language code, default en_US."),
            "components": _prop(
                "Runtime template parameters for header, body, or buttons.",
                "array",
                items={"type": "object"},
            ),
        },
        "required": ["to", "template_name"],
    },
    "send_image": {
        "description": "Send an image from a public HTTPS URL, with an optional caption.",
        "properties": {
            "to": _prop("Recipient phone number."),
            "image_url": _prop("Public HTTPS image URL."),
            "caption": _prop("Optional image caption."),
        },
        "required": ["to", "image_url"],
    },
    "send_document": {
        "description": "Send a document from a public HTTPS URL.",
        "properties": {
            "to": _prop("Recipient phone number."),
            "document_url": _prop("Public HTTPS document URL."),
            "filename": _prop("Filename shown to the recipient."),
        },
        "required": ["to", "document_url", "filename"],
    },
    "send_audio": {
        "description": "Send an audio file from a public HTTPS URL.",
        "properties": {
            "to": _prop("Recipient phone number."),
            "audio_url": _prop("Public HTTPS audio URL."),
        },
        "required": ["to", "audio_url"],
    },
    "send_video": {
        "description": "Send a video from a public HTTPS URL, with an optional caption.",
        "properties": {
            "to": _prop("Recipient phone number."),
            "video_url": _prop("Public HTTPS video URL."),
            "caption": _prop("Optional video caption."),
        },
        "required": ["to", "video_url"],
    },
    "mark_as_read": {
        "description": "Mark an inbound WhatsApp message as read using its wamid.",
        "properties": {
            "message_id": _prop("Inbound WhatsApp message id (wamid)."),
        },
        "required": ["message_id"],
    },
    "list_message_templates": {
        "description": (
            "List message templates in the configured WhatsApp Business Account, "
            "including status, category, language, and components."
        ),
        "properties": {
            "status": _prop("Optional Meta template status filter, such as APPROVED or PENDING."),
            "limit": _prop("Maximum templates to return (1-100, default 25).", "integer"),
        },
        "required": [],
    },
    "create_message_template": {
        "description": (
            "Create and submit a WhatsApp message template for Meta review. "
            "Template names must use lowercase letters, numbers, and underscores."
        ),
        "properties": {
            "name": _prop("Template name, for example order_update."),
            "category": _prop(
                "Template category.",
                enum=["AUTHENTICATION", "MARKETING", "UTILITY"],
            ),
            "language": _prop("Template language code, for example en_US or zh_CN."),
            "components": _COMPONENTS,
            "allow_category_change": _prop(
                "Allow Meta to correct the category instead of rejecting it.",
                "boolean",
            ),
        },
        "required": ["name", "category", "language", "components"],
    },
    "delete_message_template": {
        "description": "Delete a WhatsApp message template by name. This cannot be undone.",
        "properties": {
            "name": _prop("Template name to delete."),
        },
        "required": ["name"],
    },
}


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": {
                "type": "object",
                "properties": spec.get("properties", {}),
                "required": spec.get("required", []),
            },
        }
        for name, spec in _TOOLS.items()
    ]


def _credentials(bearer_token: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(bearer_token) if bearer_token else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("WhatsApp credentials are malformed; reconnect the integration.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("WhatsApp credentials must be a JSON object.")
    return parsed


def _ids(creds: Dict[str, Any]) -> tuple[str, str, str]:
    token = str(creds.get("access_token") or creds.get("api_key") or "").strip()
    phone_id = str(creds.get("phone_number_id") or creds.get("phone_id") or "").strip()
    waba_id = str(creds.get("waba_id") or creds.get("business_account_id") or "").strip()
    return token, phone_id, waba_id


def _require(value: str, field: str) -> str:
    if not value:
        raise ValueError(f"WhatsApp not configured — missing {field}.")
    return value


def _adapter(creds: Dict[str, Any]) -> WhatsAppAdapter:
    token, phone_id, _ = _ids(creds)
    return WhatsAppAdapter(
        phone_number_id=_require(phone_id, "phone_number_id"),
        access_token=_require(token, "access_token"),
        verify_token=str(creds.get("verify_token") or ""),
    )


async def _send_link_media(
    token: str,
    phone_id: str,
    *,
    to: str,
    media_type: str,
    url: str,
    caption: str = "",
) -> Dict[str, Any]:
    media: Dict[str, Any] = {"link": url}
    if caption and media_type in {"image", "document", "video"}:
        media["caption"] = caption
    return await _graph.post(
        f"/{phone_id}/messages",
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media_type,
            media_type: media,
        },
        token=token,
        json_body=True,
    )


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    spec = _TOOLS.get(name)
    if spec is None:
        return _err(f"Unknown WhatsApp tool: {name!r}")

    args = arguments or {}
    missing = [field for field in spec.get("required", []) if args.get(field) in (None, "")]
    if missing:
        return _err(f"Missing required params: {', '.join(missing)}")

    try:
        creds = _credentials(bearer_token)
        token, phone_id, waba_id = _ids(creds)
        token = _require(token, "access_token")

        if name == "get_phone_number":
            phone_id = _require(phone_id, "phone_number_id")
            result = await _graph.get(
                f"/{phone_id}",
                token=token,
                params={
                    "fields": (
                        "id,display_phone_number,verified_name,quality_rating,"
                        "code_verification_status,platform_type,throughput"
                    )
                },
            )
        elif name == "list_phone_numbers":
            waba_id = _require(waba_id, "waba_id")
            result = await _graph.get(
                f"/{waba_id}/phone_numbers",
                token=token,
                params={
                    "fields": "id,display_phone_number,verified_name,quality_rating,status",
                    "limit": max(1, min(int(args.get("limit") or 25), 100)),
                },
            )
        elif name == "get_business_profile":
            phone_id = _require(phone_id, "phone_number_id")
            result = await _graph.get(
                f"/{phone_id}/whatsapp_business_profile",
                token=token,
                params={
                    "fields": "about,address,description,email,profile_picture_url,websites,vertical",
                    "messaging_product": "whatsapp",
                },
            )
        elif name == "update_business_profile":
            phone_id = _require(phone_id, "phone_number_id")
            allowed = ("about", "address", "description", "email", "websites", "vertical")
            body = {key: args[key] for key in allowed if args.get(key) not in (None, "")}
            if not body:
                raise ValueError("Provide at least one business profile field to update.")
            body["messaging_product"] = "whatsapp"
            result = await _graph.post(
                f"/{phone_id}/whatsapp_business_profile",
                body,
                token=token,
                json_body=True,
            )
        elif name == "send_text":
            result = await _adapter(creds).send_text(args["to"], args["text"])
        elif name == "send_template":
            result = await _adapter(creds).send_template(
                args["to"],
                args["template_name"],
                args.get("language") or "en_US",
                args.get("components") or [],
            )
        elif name == "send_image":
            result = await _adapter(creds).send_image(
                args["to"],
                args["image_url"],
                args.get("caption") or "",
            )
        elif name == "send_document":
            result = await _adapter(creds).send_document(
                args["to"],
                args["document_url"],
                args["filename"],
            )
        elif name == "send_audio":
            phone_id = _require(phone_id, "phone_number_id")
            result = await _send_link_media(
                token,
                phone_id,
                to=args["to"],
                media_type="audio",
                url=args["audio_url"],
            )
        elif name == "send_video":
            phone_id = _require(phone_id, "phone_number_id")
            result = await _send_link_media(
                token,
                phone_id,
                to=args["to"],
                media_type="video",
                url=args["video_url"],
                caption=args.get("caption") or "",
            )
        elif name == "mark_as_read":
            result = {"success": await _adapter(creds).mark_as_read(args["message_id"])}
        elif name == "list_message_templates":
            waba_id = _require(waba_id, "waba_id")
            params: Dict[str, Any] = {
                "fields": "id,name,status,category,language,components,quality_score,rejected_reason",
                "limit": max(1, min(int(args.get("limit") or 25), 100)),
            }
            if args.get("status"):
                params["status"] = str(args["status"]).upper()
            result = await _graph.get(
                f"/{waba_id}/message_templates",
                token=token,
                params=params,
            )
        elif name == "create_message_template":
            waba_id = _require(waba_id, "waba_id")
            body = {
                "name": args["name"],
                "category": str(args["category"]).upper(),
                "language": args["language"],
                "components": args["components"],
            }
            if args.get("allow_category_change") is not None:
                body["allow_category_change"] = bool(args["allow_category_change"])
            result = await _graph.post(
                f"/{waba_id}/message_templates",
                body,
                token=token,
                json_body=True,
            )
        elif name == "delete_message_template":
            waba_id = _require(waba_id, "waba_id")
            result = await _graph.delete(
                f"/{waba_id}/message_templates",
                token=token,
                params={"name": args["name"]},
            )
        else:  # pragma: no cover - guarded by _TOOLS
            return _err(f"Unhandled WhatsApp tool: {name!r}")
    except (ValueError, MetaGraphError, RuntimeError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("WhatsApp tool %s failed", name)
        return _err(f"{type(exc).__name__}: {exc}")

    return _ok(result)


__all__ = ["call_tool", "list_tools"]
