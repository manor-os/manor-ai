"""Typed Strategist review triggers — what asked for a review, and why.

Historically the review carried a single free-text ``trigger`` string
(``"scheduled"``, ``"user_request: replan the quarter"``, …) and behavior
was inferred from it with ``trigger.startswith("user_feedback")``. That
made the chat tool's ``"user_request: …"`` trigger fall on the wrong side
of the branch, so an explicitly requested review was silently suppressed
whenever an unhandled proposal existed.

The fix is to stop deriving behavior from prose. A trigger is now a pair:

* ``ReviewTriggerKind`` — the closed vocabulary behavior branches on;
* ``detail`` — free text for display and audit only. Nothing reads it.

``ReviewTrigger`` bundles the two and owns the wire (de)serialization for
the Celery task, including the legacy single-string form.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ReviewTriggerKind(str, Enum):
    """Who asked for this review, and — the load-bearing part — whether a
    person is waiting for the result *right now*.

    That single question decides whether silent suppression is acceptable:

    * ``SCHEDULED`` — the workspace cadence fired. Nobody asked, nobody is
      waiting. Suppressing a tick to avoid piling proposals on top of
      undecided ones costs nothing; the next tick comes anyway.
    * ``EVENT`` — the system reacted to something that happened (workspace
      created, work batch completed, readiness changed). Still nobody
      waiting: the event is a hint that reviewing *might* be useful, and
      another event will come if it really was.
    * ``HUMAN_REQUESTED`` — a person asked, in chat or on a card, and is
      watching for an answer. Suppressing this silently is a bug: the
      request evaporates with no review, no proposal and no reply. When
      such a review cannot run, it must say so and name what blocks it.

    ``is_human_initiated`` is the only property behavior may branch on;
    call sites must never compare a trigger against a string literal.
    """

    SCHEDULED = "scheduled"
    EVENT = "event"
    HUMAN_REQUESTED = "human_requested"

    @property
    def is_human_initiated(self) -> bool:
        """True when a person is waiting for this review's result."""
        return self is ReviewTriggerKind.HUMAN_REQUESTED

    @property
    def suppressible(self) -> bool:
        """True when a blocked review may be dropped without telling anyone.

        The inverse of :attr:`is_human_initiated`, named for the decision
        it drives so the call site reads as the rule it implements.
        """
        return not self.is_human_initiated


# ── legacy wire compatibility ────────────────────────────────────────
#
# The ONLY place a trigger string is ever interpreted. This is a decoder
# for messages that were enqueued before the typed field existed (and for
# ``review_runs`` rows written back then), not a call-site shortcut: new
# producers pass ``ReviewTriggerKind`` explicitly and never come through
# here. Longest prefix wins so ``manual_retry_after_failure`` cannot be
# shadowed by a shorter neighbour.
_LEGACY_PREFIX_KINDS: dict[str, ReviewTriggerKind] = {
    "scheduled": ReviewTriggerKind.SCHEDULED,
    "workspace_created": ReviewTriggerKind.EVENT,
    "work_batch_completed": ReviewTriggerKind.EVENT,
    "readiness_changed": ReviewTriggerKind.EVENT,
    "user_request": ReviewTriggerKind.HUMAN_REQUESTED,
    "user_feedback": ReviewTriggerKind.HUMAN_REQUESTED,
    "manual": ReviewTriggerKind.HUMAN_REQUESTED,
    "manual_retry_after_failure": ReviewTriggerKind.HUMAN_REQUESTED,
}

# Unrecognised legacy text is treated as an EVENT: it keeps the old
# suppression behavior (the old code only exempted ``user_feedback``) and
# never silently promotes unknown prose to "a human is waiting".
_LEGACY_FALLBACK_KIND = ReviewTriggerKind.EVENT


def classify_legacy_trigger(raw: str) -> ReviewTriggerKind:
    """Map one pre-enum free-text trigger onto its kind."""
    text = (raw or "").strip()
    best: Optional[tuple[int, ReviewTriggerKind]] = None
    for prefix, kind in _LEGACY_PREFIX_KINDS.items():
        if text == prefix or text.startswith(f"{prefix}:"):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), kind)
    return best[1] if best else _LEGACY_FALLBACK_KIND


@dataclass(frozen=True)
class ReviewTrigger:
    """A review trigger: the typed ``kind`` plus opaque ``detail`` prose."""

    kind: ReviewTriggerKind
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewTriggerKind):
            object.__setattr__(self, "kind", ReviewTriggerKind(str(self.kind)))
        object.__setattr__(self, "detail", str(self.detail or "").strip()[:500])

    @property
    def is_human_initiated(self) -> bool:
        return self.kind.is_human_initiated

    @property
    def label(self) -> str:
        """Display/audit string — never parsed, never branched on."""
        return f"{self.kind.value}: {self.detail}" if self.detail else self.kind.value

    # ── wire format ──────────────────────────────────────────────────
    #
    # New producers enqueue ``kwargs={"trigger_kind": ..., "trigger_detail":
    # ...}``; the second positional ``trigger`` argument survives only so a
    # worker rolled out mid-flight still drains messages that were queued
    # with the old bare string.

    def celery_kwargs(self) -> dict[str, str]:
        return {"trigger_kind": self.kind.value, "trigger_detail": self.detail}

    @classmethod
    def coerce(cls, value: Any) -> "ReviewTrigger":
        """Accept a ReviewTrigger, a ReviewTriggerKind, or legacy prose."""
        if isinstance(value, cls):
            return value
        if isinstance(value, ReviewTriggerKind):
            return cls(kind=value)
        if value is None:
            return cls(kind=ReviewTriggerKind.SCHEDULED)
        if isinstance(value, dict):
            return cls(
                kind=ReviewTriggerKind(str(value.get("kind"))),
                detail=str(value.get("detail") or ""),
            )
        text = str(value)
        try:
            return cls(kind=ReviewTriggerKind(text))
        except ValueError:
            pass
        kind = classify_legacy_trigger(text)
        return cls(kind=kind, detail=text)

    @classmethod
    def from_wire(
        cls,
        *,
        trigger: Any = None,
        trigger_kind: Any = None,
        trigger_detail: Any = None,
    ) -> "ReviewTrigger":
        """Decode a Celery payload, tolerating the legacy single string.

        ``trigger_kind`` is the current contract. A message carrying only
        the positional ``trigger`` string was enqueued by a pre-upgrade
        producer: classify it and log a deprecation warning rather than
        crashing the worker mid-deploy.
        """
        if trigger_kind is not None:
            return cls(
                kind=ReviewTriggerKind(str(getattr(trigger_kind, "value", trigger_kind))),
                detail=str(trigger_detail or ""),
            )
        if trigger is None:
            return cls(kind=ReviewTriggerKind.SCHEDULED, detail=str(trigger_detail or ""))
        decoded = cls.coerce(trigger)
        if isinstance(trigger, str) and trigger not in {k.value for k in ReviewTriggerKind}:
            logger.warning(
                "DEPRECATED: run_strategist_review received the legacy free-text "
                "trigger %r; classified as %s. Producers must send trigger_kind.",
                trigger, decoded.kind.value,
            )
        return decoded


__all__ = [
    "ReviewTrigger",
    "ReviewTriggerKind",
    "classify_legacy_trigger",
]
