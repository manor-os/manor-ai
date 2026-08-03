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

Ordering
--------
First match wins. Entries are ordered narrowest-first so a broad pattern can
never answer for a failure a specific one describes better. Two placements
are load-bearing rather than cosmetic:

* credit exhaustion (a cloud-only entry) sits ahead of the upstream-outage
  entry wherever both are present. Both failures
  are minted a few lines apart in ``packages/core/ai/agentic_loop.py``
  (``_credit_exhausted_result`` / ``_llm_call_failed_result``), so a future
  out-of-credits case that surfaces wrapped in the LLM-failure prose must be
  told "top up", not "we're retrying for you".
* the upstream-outage entry is keyed on the HTTP status, never on the prose
  around it. That prose ("check the selected model and API key
  configuration") is emitted for *every* provider failure including a real
  401/403, so matching it would swallow genuine auth errors into a silent
  retry loop. Auth failures are deliberately left unmatched: the raw message
  they fall back to already says to check the API key, which for them is
  correct advice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

#: Where a user pairs / inspects their local worker, and where third-party
#: app connections are (re)authorized. Verified against
#: apps/web/src/router.tsx; the local-runner copy already sends users here.
_INTEGRATIONS_ROUTE = "/integrations"


#: The prose ``_llm_call_failed_result`` wraps around *any* provider error.
#: Used only as an anchor, never as a match on its own — see "Ordering".
_LLM_CALL_FAILED_PROSE = r"failed before the model could respond"


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
    # The single most common real failure in production, and the one whose
    # own message is actively wrong. Every observed occurrence is an upstream
    # 503 / 524 / 502 — the provider was down or the gateway gave up waiting.
    # Not one was an authentication failure. The message the user currently
    # sees sends them to re-check an API key that was never the problem.
    #
    # One entry rather than three: "the provider did not answer" is the same
    # sentence and the same (non-)action for all three statuses, and three
    # copies of it would be three places to drift.
    #
    # Both halves are required: the LLM-failure prose AND an outage status.
    # The prose alone also wraps genuine 401/403s, and the bare status alone
    # would claim any web page a subagent fetched that happened to answer
    # 503. Requiring both keeps this pinned to "Manor's model call died
    # upstream".
    (
        re.compile(
            _LLM_CALL_FAILED_PROSE
            + r"[\s\S]{0,400}?"
            + r"(?:\bHTTP[ :]*(?:502|503|524)\b"
            + r"|service (?:is )?temporarily unavailab)",
            re.I,
        ),
        ExecutionErrorHint(
            what_happened=(
                "The AI provider did not answer — it was temporarily "
                "unavailable or took too long. This is not a problem with "
                "your settings or your API key."
            ),
            # Written for the moment this card is actually read. Because
            # the entry is transient, Manor retries silently first and only
            # surfaces once that budget is spent — so "hang on, we're
            # retrying" would already be out of date on screen.
            action_to_take=(
                "Manor already waited and tried again several times, and the "
                "provider is still not answering. Nothing on your side needs "
                "changing — run the step again later, or cancel it."
            ),
            is_transient=True,
        ),
    ),
    # A connected app refused the call. Reconnecting is the only fix the user
    # has, and it is a real one. Matched on the uppercase error code rather
    # than on prose, because "access denied" in free text means far too many
    # things.
    (
        re.compile(r"\b(?:ACCESS|PERMISSION)_DENIED\b"),
        ExecutionErrorHint(
            what_happened=(
                "A connected app refused the request. Manor's connection to it "
                "does not have permission for what this step needed to do."
            ),
            action_to_take=(
                "Open Integrations, reconnect that app, and accept every "
                "permission it asks for — then retry. If your account there "
                "does not have the access itself, ask an administrator of that "
                "app to grant it."
            ),
            action_link=_INTEGRATIONS_ROUTE,
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
