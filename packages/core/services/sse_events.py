from __future__ import annotations

import json


def format_sse(event: str, data: dict | str) -> str:
    """Format a single server-sent event frame."""

    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
