"""Stable, non-authoritative speaker labels for shared chat model context."""

from __future__ import annotations

import hashlib
import hmac

from packages.core.config import get_settings


def anonymous_chat_sender_label(
    conversation_id: str,
    sender_kind: str,
    sender_id: str,
) -> str:
    """Identify a speaker across turns without exposing an internal database ID.

    These labels are only prompt context. Runtime authorization must continue to
    use the authenticated user and the persisted sender ID, never this label.
    """

    prefix = {"person": "P", "agent": "A"}.get(sender_kind)
    if not prefix or not conversation_id or not sender_id:
        raise ValueError("A shared-chat speaker needs a conversation and sender")
    material = f"chat-speaker:v1:{conversation_id}:{sender_kind}:{sender_id}"
    digest = hmac.new(
        get_settings().JWT_SECRET_KEY.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"
