"""Known execution-failure signatures → what the user should actually do.

An error card is only useful if it answers "what do I do about it". A raw
StepResult failure message answers "what broke" at best. This module is the
lookup that turns the former into the latter, for failures we have seen.

Deliberately a small, explicit table rather than a classifier: a wrong
"here's your fix" is worse than no fix, so an unrecognized failure returns
None and the card falls back to showing the raw message.

The patterns are matched against the failure message the worker actually
reports. The wording they target is the wording Manor itself emits — see
``_cli_worker_terminal_message`` in ``packages/core/ai/mcp/chrome.py``, which
is where the incident's real message came from.

``action_link`` points at ``/integrations``, the route that hosts local
worker pairing (``apps/web/src/router.tsx``). ``/settings/connections`` — the
link this module was first specified with — does not exist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

#: Where a user pairs / inspects their local worker. Verified against
#: apps/web/src/router.tsx; the local-runner copy already sends users here.
_INTEGRATIONS_ROUTE = "/integrations"


@dataclass(frozen=True)
class ExecutionErrorHint:
    what_happened: str
    action_to_take: str
    action_link: Optional[str] = None
    is_transient: bool = False
    """True when the failure is expected to clear on its own once the user
    fixes something external — the retry policy backs off instead of burning
    an attempt and re-asking for approval."""


_SIGNATURES: tuple[tuple[re.Pattern[str], ExecutionErrorHint], ...] = (
    (
        re.compile(r"chrome (extension|control) (is )?unavailable", re.I),
        ExecutionErrorHint(
            what_happened="The Manor Chrome extension is not responding.",
            action_to_take=(
                "Open Chrome and confirm the Manor extension is enabled, then retry."
            ),
            action_link=_INTEGRATIONS_ROUTE,
            is_transient=True,
        ),
    ),
)


def classify_execution_error(error: Any) -> Optional[ExecutionErrorHint]:
    """Return actionable guidance for a known failure, else None."""
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    for pattern, hint in _SIGNATURES:
        if pattern.search(message):
            return hint
    return None
