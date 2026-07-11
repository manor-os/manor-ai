"""Notification action-callback registry + matcher.

When a notification is sent to an external channel with ``actions=[...]``,
the dispatcher records a ``NotificationDelivery`` row carrying the action
keys and a callback descriptor (kind + payload). When the user replies
through the channel, ``dispatch_inbound`` matches the inbound text against
the open delivery's action keys and — on a hit — fires the registered
callback to resolve the underlying pending state (workspace HITL approval,
SLA reminder dismiss, etc.).

Callbacks are async functions registered at import time by the producer
module that owns the pending state. Keeping the registry in-process (vs.
a DB lookup) means handlers can carry rich Python context — but does mean
the producer module must be imported before its callback can fire. The
gateway's ``dispatch_inbound`` already imports every channel adapter, so
producers that ship with the core package register their callbacks at
adapter-import time.

Matching is intentionally forgiving: case-insensitive, whitespace-trimmed,
and supports per-action ``synonyms`` so "approve" / "Approve" / "ok" /
"yes" all match the same key when the producer lists them.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


CallbackHandler = Callable[
    [dict, str, dict], Awaitable[dict[str, Any]]
]
# Signature: handler(callback_payload, action_key, dispatch_context) -> dict


_REGISTRY: dict[str, CallbackHandler] = {}


def register_callback(kind: str, handler: CallbackHandler) -> None:
    """Register an async handler for a callback kind.

    Producers call this at module import time. Re-registering the same
    kind overwrites the previous handler — useful for tests that swap in
    a recording stub but should be avoided in production code paths.
    """
    if not kind:
        raise ValueError("callback kind must be a non-empty string")
    _REGISTRY[kind] = handler


def get_callback(kind: str) -> Optional[CallbackHandler]:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY.keys())


async def dispatch_callback(
    kind: str,
    *,
    payload: dict | None,
    action_key: str,
    context: dict | None = None,
) -> dict[str, Any]:
    """Invoke the registered handler for ``kind``.

    Returns the handler's dict result. Unknown kinds return ``{"ok":
    False, "error": "unknown_callback_kind"}`` instead of raising so the
    inbound flow can record + ack the user with a sensible error rather
    than 500ing the webhook.
    """
    handler = _REGISTRY.get(kind)
    if handler is None:
        logger.warning(
            "notification_callbacks: no handler for kind=%s — registered=%s",
            kind, sorted(_REGISTRY.keys()),
        )
        return {"ok": False, "error": "unknown_callback_kind", "kind": kind}
    try:
        return await handler(payload or {}, action_key, context or {})
    except Exception as exc:
        logger.exception(
            "notification_callbacks: handler for kind=%s raised", kind,
        )
        return {"ok": False, "error": str(exc), "kind": kind}


# ── Action-key matching ────────────────────────────────────────────────────

def _normalise(value: str) -> str:
    return (value or "").strip().casefold()


def match_action(
    inbound_text: str,
    actions: Iterable[dict] | None,
) -> Optional[str]:
    """Return the canonical action key the user replied with, or None.

    ``actions`` is a list of ``{"key": "...", "label": "...",
    "synonyms": [...]}`` dicts. The match strategy:

      1. Strict equality (case-insensitive, trimmed) against ``key``
         and ``label`` and any listed ``synonyms``.
      2. If the inbound text is a single digit and falls within the
         actions list bounds, treat it as a 1-indexed pick (so a phone
         user can type ``1`` for the first action even if the rendered
         text only suggested word replies).
    """
    if not actions:
        return None

    text = _normalise(inbound_text)
    if not text:
        return None

    actions_list = list(actions)

    # Numeric shortcut: "1" → first action.
    if text.isdigit():
        try:
            idx = int(text)
        except ValueError:
            idx = -1
        if 1 <= idx <= len(actions_list):
            picked = actions_list[idx - 1]
            if isinstance(picked, dict):
                key = picked.get("key")
                if isinstance(key, str) and key:
                    return key

    for action in actions_list:
        if not isinstance(action, dict):
            continue
        candidates: list[str] = []
        for field in ("key", "label"):
            value = action.get(field)
            if isinstance(value, str) and value:
                candidates.append(value)
        synonyms = action.get("synonyms")
        if isinstance(synonyms, list):
            candidates.extend(s for s in synonyms if isinstance(s, str) and s)
        for candidate in candidates:
            if _normalise(candidate) == text:
                key = action.get("key")
                if isinstance(key, str) and key:
                    return key
    return None
