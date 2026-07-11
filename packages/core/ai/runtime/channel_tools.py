from __future__ import annotations

from copy import deepcopy

RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME = "send_channel_attachment"

_CHANNEL_ATTACHMENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME,
        "description": (
            "Send a file (image / document / audio / video) to the current "
            "channel user. The URL must be an HTTPS address the channel "
            "provider can fetch. Use this when you've generated a PDF / image "
            "/ report via another tool and want to deliver it to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "HTTPS URL of the file.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["image", "document", "audio", "video"],
                    "description": "What kind of media this is.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption or body text shown with the file.",
                },
            },
            "required": ["url", "kind"],
        },
    },
}


def runtime_channel_attachment_tool_schema() -> dict:
    """Return the runtime-owned schema for channel-local attachment sending."""
    return deepcopy(_CHANNEL_ATTACHMENT_TOOL_SCHEMA)
