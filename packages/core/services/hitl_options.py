from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from packages.core.constants.approvals import HitlType

ApprovalChoice = Literal["approve", "always_approve", "revise", "reject"]

APPROVAL_CHOICE_APPROVE: ApprovalChoice = "approve"
APPROVAL_CHOICE_ALWAYS_APPROVE: ApprovalChoice = "always_approve"
APPROVAL_CHOICE_REVISE: ApprovalChoice = "revise"
APPROVAL_CHOICE_REJECT: ApprovalChoice = "reject"

APPROVAL_CHOICES: list[ApprovalChoice] = [
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_REVISE,
    APPROVAL_CHOICE_REJECT,
]

DEFAULT_APPROVAL_OPTIONS: list[ApprovalChoice] = [
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_REJECT,
]

def normalize_approval_choice(value: Any) -> ApprovalChoice | None:
    """Normalize an approval choice to the fixed public schema.

    Approval cards and action APIs must pass exactly one of:
    ``approve``, ``always_approve``, ``revise``, or ``reject``. Plain-text replies such as
    "yes" or "可以" should be classified by an edge adapter before they reach
    this schema boundary.
    """

    normalized = str(value or "").strip().lower()
    if normalized in APPROVAL_CHOICES:
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
    filtered = [
        opt for opt in chosen
        if normalize_approval_choice(opt) != APPROVAL_CHOICE_ALWAYS_APPROVE
    ]
    return filtered or list(ONE_TIME_APPROVAL_OPTIONS)


#: What a ``hitl_type="error"`` card offers instead of approve/always/reject.
#: An error card is not a "may I?" — the step already ran and failed, so
#: "Approve" would misdescribe what the click does (and was how an operator
#: approved the same steps 15 times). The honest pair is "I fixed it, run it
#: again" and "give up on this step". ``always_approve`` is deliberately
#: absent: a standing grant for a failure pre-authorizes nothing.
ERROR_CHOICE_RETRY = "retry"
ERROR_CHOICE_CANCEL = "cancel"

ERROR_CARD_OPTIONS: list[str] = [ERROR_CHOICE_RETRY, ERROR_CHOICE_CANCEL]

#: A review is a verdict on material that already exists. It therefore has
#: one more honest outcome than an authorization request: the reviewer can
#: ask for a revised version and explain what should change. ``reject`` is
#: retained as the terminal "do not continue with this material" decision so
#: old clients and persisted cards remain wire-compatible.
REVIEW_CHOICE_REQUEST_CHANGES = "request_changes"
REVIEW_CARD_OPTIONS: list[str] = [
    APPROVAL_CHOICE_APPROVE,
    REVIEW_CHOICE_REQUEST_CHANGES,
    APPROVAL_CHOICE_REJECT,
]


def human_step_hitl_type(params: dict[str, Any] | None) -> str:
    """Classify an inline human Step as review or ordinary input."""

    values = params if isinstance(params, dict) else {}
    pending_action = values.get("pending_action")
    if isinstance(pending_action, dict):
        explicit = str(pending_action.get("hitl_type") or "").strip().lower()
        if explicit in HitlType.values():
            return explicit
    explicit = str(values.get("hitl_type") or "").strip().lower()
    if explicit in HitlType.values():
        return explicit
    if any(
        values.get(key) is not None
        for key in ("review", "review_artifacts", "artifacts_for_review")
    ):
        return HitlType.REVIEW.value
    return HitlType.INPUT.value


class HumanDecisionIntent(str, Enum):
    """Continuation-neutral meaning of a public HITL choice.

    The producer-specific continuation (resume a Step, replan a Task, resume a
    Workflow) stays outside this enum. This layer answers only what the human
    decided, so every surface uses the same semantics.
    """

    APPROVE = "approve"
    APPROVE_STANDING = "approve_standing"
    REQUEST_CHANGES = "request_changes"
    DENY = "deny"
    RETRY = "retry"
    CANCEL = "cancel"
    OTHER = "other"


def error_card_options() -> list[str]:
    """Choices for an ``error`` HITL card."""

    return list(ERROR_CARD_OPTIONS)


def review_card_options(options: list[str] | None = None) -> list[str]:
    """Choices for a concrete diff/file/content review.

    Producers may supply a narrower explicit vocabulary (task approval, for
    example, intentionally has no terminal reject button). A plain review
    gets the shared approve / request changes / reject contract and can never
    carry a standing grant.
    """

    chosen = (
        list(options)
        if isinstance(options, list) and options
        else list(REVIEW_CARD_OPTIONS)
    )
    if any(
        normalize_approval_choice(opt) == APPROVAL_CHOICE_ALWAYS_APPROVE
        for opt in chosen
    ):
        # A standing grant never belonged to a review. Its presence identifies
        # the pre-typed generic approval vocabulary, so migrate the whole list
        # instead of merely deleting "always" and losing request-changes.
        return list(REVIEW_CARD_OPTIONS)
    filtered = [
        opt for opt in chosen
        if normalize_approval_choice(opt) != APPROVAL_CHOICE_ALWAYS_APPROVE
    ]
    return filtered or list(REVIEW_CARD_OPTIONS)


def decision_options_for_hitl(
    hitl_type: str | None,
    options: list[str] | None = None,
    *,
    standing_allowed: bool = True,
) -> list[str]:
    """Project a typed ``HitlRequest`` into its public decisions.

    This is the common semantic boundary for cards. ``pending_action.kind``
    still decides which legacy continuation handler runs; it no longer needs
    to invent button vocabularies independently.
    """

    normalized = str(hitl_type or HitlType.AUTHORIZE.value).strip().lower()
    if normalized == HitlType.ERROR.value:
        return error_card_options()
    if normalized == HitlType.REVIEW.value:
        return review_card_options(options)
    if normalized == HitlType.AUTHORIZE.value:
        return (
            approval_options(options)
            if standing_allowed
            else one_time_approval_options(options)
        )
    return list(options or [])


def is_change_request_choice(value: Any) -> bool:
    """Whether a decision asks for revised material instead of authorizing it."""

    return str(value or "").strip().lower() in {
        APPROVAL_CHOICE_REVISE,
        REVIEW_CHOICE_REQUEST_CHANGES,
    }


def decision_intent_for_hitl(
    hitl_type: str | None,
    value: Any,
) -> HumanDecisionIntent:
    """Classify a wire choice without deciding how its origin resumes.

    Explicit review cards predate the typed vocabulary and may persist
    ``accept`` / ``revise`` / ``cancel``.  They are still valid public wire
    choices: a deployment must not strand an already-rendered card merely
    because the common layer now calls those intentions approve / request
    changes / cancel.
    """

    normalized_type = str(hitl_type or HitlType.AUTHORIZE.value).strip().lower()
    choice = str(value or "").strip().lower()
    if normalized_type == HitlType.ERROR.value:
        if choice in {ERROR_CHOICE_RETRY, "retry_now"}:
            return HumanDecisionIntent.RETRY
        if choice in {ERROR_CHOICE_CANCEL, "skip"}:
            return HumanDecisionIntent.CANCEL
        return HumanDecisionIntent.OTHER
    if normalized_type in {HitlType.AUTHORIZE.value, HitlType.REVIEW.value}:
        if choice in {APPROVAL_CHOICE_APPROVE, "accept", "approved", "yes"}:
            return HumanDecisionIntent.APPROVE
        if (
            normalized_type == HitlType.AUTHORIZE.value
            and choice == APPROVAL_CHOICE_ALWAYS_APPROVE
        ):
            return HumanDecisionIntent.APPROVE_STANDING
        if normalized_type == HitlType.REVIEW.value and is_change_request_choice(choice):
            return HumanDecisionIntent.REQUEST_CHANGES
        if choice in {APPROVAL_CHOICE_REJECT, "deny", "decline", "no"}:
            return HumanDecisionIntent.DENY
        if normalized_type == HitlType.REVIEW.value and choice in {"cancel", "skip"}:
            return HumanDecisionIntent.CANCEL
    return HumanDecisionIntent.OTHER


def external_reply_decision_intent(value: Any) -> HumanDecisionIntent:
    """Map current and legacy external-reply buttons to shared decision intent."""

    choice = str(value or "").strip().lower()
    if choice == "confirm":
        return HumanDecisionIntent.APPROVE
    if choice == "rejected":
        return HumanDecisionIntent.DENY
    if choice == "cancel":
        return HumanDecisionIntent.CANCEL
    return decision_intent_for_hitl(HitlType.AUTHORIZE.value, choice)


def approval_notification_actions() -> list[dict[str, object]]:
    return [
        {"key": APPROVAL_CHOICE_APPROVE, "label": "Approve", "synonyms": ["yes", "ok", "y"]},
        {"key": APPROVAL_CHOICE_ALWAYS_APPROVE, "label": "Always approve", "synonyms": ["always", "always approve", "always allow"]},
        {"key": APPROVAL_CHOICE_REJECT, "label": "Reject", "synonyms": ["no", "deny", "n"]},
    ]
