"""The blocking-HITL tool-result envelope, as a closed vocabulary.

When a tool needs a human before it can proceed, it does not raise — it
*returns* a JSON object carrying ``__hitl__: true`` plus the fields the chat
surface needs to render an approval card. That envelope is a frontend
contract: the browser reads these exact keys.

The keys were spelled as bare strings at seventeen sites across eight
modules, and detection was reinvented per site. One site checked
``payload.get("__hitl__") is True`` (correct); another checked
``text.strip().startswith('{"__hitl__":')`` — which silently stops matching
the moment anything serializes the same payload with ``indent=`` or reorders
its keys, because two of the three producers return a dict and are dumped
somewhere else entirely. A card that never renders is indistinguishable from
a tool that never asked.

So: one enum for the keys, and one function that answers "is this a blocking
HITL result?" — parse, then check the key. Never match on serialized text.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional


class HitlEnvelopeKey(str, Enum):
    """Top-level keys of the blocking-HITL tool result."""

    #: Marker. Presence AND a ``True`` value is what makes a result blocking.
    MARKER = "__hitl__"
    #: Always ``approval_required`` today; kept explicit because the frontend
    #: branches on it.
    ERROR = "error"
    #: The id the resolver grants. For the unified core this is the
    #: ``HitlRequest`` id.
    APPROVAL_TOKEN = "approval_token"
    #: The render payload: prompt, action, tool, options, content previews.
    HITL = "hitl"
    #: What is being gated, when the caller wants it separate from ``hitl``.
    OPERATION = "operation"

    # ``str(member)`` must be the wire key, not "HitlEnvelopeKey.MARKER" —
    # these get used as dict subscripts and in f-strings. See PendingActionKind
    # for the incident this guards against.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


def is_hitl_envelope(payload: Any) -> bool:
    """True when *payload* is a blocking-HITL tool result.

    Takes the parsed object. ``True`` specifically, not truthy: a tool that
    returns ``{"__hitl__": "no"}`` is not asking for a human.
    """
    return isinstance(payload, dict) and payload.get(HitlEnvelopeKey.MARKER.value) is True


def parse_hitl_envelope(result: Any) -> Optional[dict]:
    """Return the HITL envelope carried by a tool result, else ``None``.

    Accepts either the parsed dict or the JSON text a tool returned. Parsing
    is the only correct test: serialization is not stable enough to match on
    (key order, indentation, and separators all vary by producer), and the
    cost of a failed ``json.loads`` on an ordinary tool result is nothing
    compared to an approval card that silently never appears.
    """
    if is_hitl_envelope(result):
        return result
    if not isinstance(result, str) or not result.strip().startswith("{"):
        return None
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return None
    return parsed if is_hitl_envelope(parsed) else None
