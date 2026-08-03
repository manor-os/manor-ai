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


#: Approve-once / reject. Not "this subject is too dangerous to blanket-approve"
#: — the user is the authority on that, and ``never_allow`` is the only hard
#: block. This is for cards whose subject has no STANDING VERSION at all: see
#: ``one_time_approval_options``.
ONE_TIME_APPROVAL_OPTIONS: list[ApprovalChoice] = [
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_REJECT,
]


def approval_options(options: list[str] | None = None) -> list[str]:
    """Return canonical approval choices for HITL cards.

    The UI always offers approve-once, always-approve, and reject for
    approval-style requests. "Always" is the user's to give: whatever the
    capability, if they say always, they mean always. Producers can still pass
    a richer explicit list for non-standard cards, but plain approval fallbacks
    should use this helper instead of hand-rolled arrays.
    """

    return (
        list(options)
        if isinstance(options, list) and options
        else list(DEFAULT_APPROVAL_OPTIONS)
    )


def one_time_approval_options(options: list[str] | None = None) -> list[str]:
    """Approve-once / reject, for a card that HAS no standing version.

    A ``review`` card is a verdict on ONE specific diff. "Always apply whatever
    the next draft happens to say" is not a subject a person can consent to —
    there is nothing stable for the grant to be about. That is a property of
    the question, not a judgement about the user: any capability the user
    clicks "Always" on gets a standing grant (see ``approval_options``).

    An explicit ``options`` list is filtered too, so a producer hand-rolling
    the vocabulary cannot put a standing button onto a card whose subject
    cannot carry one.
    """

    chosen = (
        list(options)
        if isinstance(options, list) and options
        else list(ONE_TIME_APPROVAL_OPTIONS)
    )
    return [
        opt for opt in chosen
        if normalize_approval_choice(opt) != APPROVAL_CHOICE_ALWAYS_APPROVE
    ]


#: What a ``hitl_type="error"`` card offers instead of approve/always/reject.
#: An error card is not a "may I?" — the step already ran and failed, so
#: "Approve" would misdescribe what the click does (and was how an operator
#: approved the same steps 15 times). The honest pair is "I fixed it, run it
#: again" and "give up on this step". ``always_approve`` is deliberately
#: absent: a standing grant for a failure pre-authorizes nothing.
ERROR_CHOICE_RETRY = "retry"
ERROR_CHOICE_CANCEL = "cancel"

ERROR_CARD_OPTIONS: list[str] = [ERROR_CHOICE_RETRY, ERROR_CHOICE_CANCEL]


def error_card_options() -> list[str]:
    """Choices for an ``error`` HITL card."""

    return list(ERROR_CARD_OPTIONS)


def approval_notification_actions() -> list[dict[str, object]]:
    return [
        {"key": APPROVAL_CHOICE_APPROVE, "label": "Approve", "synonyms": ["yes", "ok", "y"]},
        {"key": APPROVAL_CHOICE_ALWAYS_APPROVE, "label": "Always approve", "synonyms": ["always", "always approve", "always allow"]},
        {"key": APPROVAL_CHOICE_REJECT, "label": "Reject", "synonyms": ["no", "deny", "n"]},
    ]
