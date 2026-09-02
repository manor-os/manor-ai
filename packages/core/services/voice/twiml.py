"""Small TwiML builders for Manor-owned Twilio Voice routes."""
from __future__ import annotations

from xml.sax.saxutils import escape


def build_stream_twiml(
    *,
    stream_url: str,
    from_number: str = "",
    to_number: str = "",
    direction: str = "inbound",
) -> str:
    """Return bidirectional Media Streams TwiML with non-secret parameters."""
    stream = escape(str(stream_url or ""), {"\"": "&quot;"})
    caller = escape(str(from_number or ""), {"\"": "&quot;"})
    recipient = escape(str(to_number or ""), {"\"": "&quot;"})
    call_direction = escape(str(direction or "inbound"), {"\"": "&quot;"})
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Connect><Stream url="{stream}">'
        f'<Parameter name="from" value="{caller}"/>'
        f'<Parameter name="to" value="{recipient}"/>'
        f'<Parameter name="direction" value="{call_direction}"/>'
        '</Stream></Connect>'
        '</Response>'
    )


__all__ = ["build_stream_twiml"]
