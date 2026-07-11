from __future__ import annotations

from typing import Any, Literal

ApprovalChoice = Literal["approve", "always_approve", "reject"]

APPROVAL_CHOICE_APPROVE: ApprovalChoice = "approve"
APPROVAL_CHOICE_ALWAYS_APPROVE: ApprovalChoice = "always_approve"
APPROVAL_CHOICE_REJECT: ApprovalChoice = "reject"

DEFAULT_APPROVAL_OPTIONS: list[ApprovalChoice] = [
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_REJECT,
]

def normalize_approval_choice(value: Any) -> ApprovalChoice | None:
    """Normalize an approval choice to the fixed public schema.

    Approval cards and action APIs must pass exactly one of:
    ``approve``, ``always_approve``, or ``reject``. Plain-text replies such as
    "yes" or "可以" should be classified by an edge adapter before they reach
    this schema boundary.
    """

    normalized = str(value or "").strip().lower()
    if normalized in DEFAULT_APPROVAL_OPTIONS:
        return normalized  # type: ignore[return-value]
    return None


def approval_options(options: list[str] | None = None) -> list[str]:
    """Return canonical approval choices for HITL cards.

    The UI should always offer approve-once, always-approve, and reject for
    approval-style requests. Producers can still pass a richer explicit list
    for non-standard cards, but plain approval fallbacks should use this helper
    instead of hand-rolled two-button arrays.
    """

    return options if isinstance(options, list) and options else list(DEFAULT_APPROVAL_OPTIONS)


def approval_notification_actions() -> list[dict[str, object]]:
    return [
        {"key": APPROVAL_CHOICE_APPROVE, "label": "Approve", "synonyms": ["yes", "ok", "y"]},
        {"key": APPROVAL_CHOICE_ALWAYS_APPROVE, "label": "Always approve", "synonyms": ["always", "always approve", "always allow"]},
        {"key": APPROVAL_CHOICE_REJECT, "label": "Reject", "synonyms": ["no", "deny", "n"]},
    ]
