"""The approval-request lifecycle, as a closed vocabulary.

``HitlRequest`` was introduced to end the sprawl of five stores that
each held a piece of "does this need a human?" (see the model's docstring).
Its ``status`` column, though, was still compared against bare strings in
the governance layer, the runtime approval service, the strategist and half
a dozen read models — so the one thing that decides whether a gate blocks
was spelled fresh at every site.

The lifecycle is: ``pending`` blocks; ``granted`` unblocks exactly once and
then becomes ``consumed``; ``denied`` refuses; ``expired`` is a stale
request whose origin outlived it (auto-resolved). Only ``pending`` is open
for counting and dedup — the partial unique index that keeps at most one
open request per key is defined on that value.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from packages.core.constants.pending_actions import PendingActionKind


class ApprovalStatus(str, Enum):
    """Every state a HitlRequest row can hold."""

    #: Blocking, waiting on a person. The only "open" state.
    PENDING = "pending"

    #: A person allowed it. Unblocks the gate once, then → CONSUMED.
    GRANTED = "granted"

    #: A person refused it.
    DENIED = "denied"

    #: The grant was spent by the gate that was waiting for it.
    CONSUMED = "consumed"

    #: The origin (conversation/step/channel) reached a terminal state
    #: while the request was still pending, so it was auto-resolved.
    EXPIRED = "expired"

    # A plain ``str, Enum`` stringifies to its QUALIFIED NAME, so ``str(member)``
    # and every f-string built from one silently yield "ApprovalStatus.PENDING"
    # instead of "pending". Not cosmetic: that is exactly how the dispatcher
    # stopped minting requests for fallback cards when PendingActionKind was
    # introduced. Same mixin, same trap.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: Statuses that still block. Membership, not equality, so a second open
#: state (if one is ever added) reaches every read model at once.
APPROVAL_OPEN_STATUSES: tuple[str, ...] = (ApprovalStatus.PENDING.value,)

#: Statuses that no longer block, in any way.
APPROVAL_TERMINAL_STATUSES: tuple[str, ...] = (
    ApprovalStatus.GRANTED.value,
    ApprovalStatus.DENIED.value,
    ApprovalStatus.EXPIRED.value,
    ApprovalStatus.CONSUMED.value,
)

#: A request that is either still blocking or has an unspent grant — what
#: "is there already a decision for this subject?" means.
APPROVAL_LIVE_STATUSES: tuple[ApprovalStatus, ...] = (
    ApprovalStatus.PENDING,
    ApprovalStatus.GRANTED,
)


class ApprovalOriginKind(str, Enum):
    """WHERE a request is blocking — the axis that decides how it resumes.

    ``dedup_key_for`` branches on this to pick the key that keeps at most one
    open request per subject, and the runtime guard filters its entire read
    model on ``origin_kind == "tool_call"``. Both were bare strings, so an
    origin spelled slightly differently would not have raised — it would have
    minted a second card for a subject that already had one, or made a request
    invisible to the plane waiting on it.

    Persisted in ``hitl_requests.origin_kind`` (``String(30)``);
    ``str``-based so the column, the comparisons and the API response shape are
    all unchanged.
    """

    #: An LLM tool call in a conversation (the runtime guard plane).
    TOOL_CALL = "tool_call"
    #: A plan step at the dispatcher gate.
    STEP = "step"
    #: A channel message awaiting sign-off before it leaves the workspace.
    CHANNEL = "channel"
    #: A workspace-structure change drafted by the strategist.
    OPERATION = "operation"
    #: A mid-execution lease pause ("path C") — the worker stopped and asked.
    LEASE = "lease"

    # Render the wire value from ``str()`` / f-strings rather than the mixin
    # enum's qualified name, so a member is interchangeable with the literal it
    # replaced everywhere — including inside the log lines and rule names that
    # interpolate it.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class HitlType(str, Enum):
    """What KIND of human involvement a request needs.

    ``ApprovalStatus`` answers "where in the lifecycle is it"; this answers
    "what is the human being asked for". They are orthogonal: an ``error``
    request still moves pending → granted just like an ``authorize`` one.

    Every type's rendered copy must answer three things — what this is, why,
    and what you should do. The payload fields below are what make that
    possible, which is why they are required rather than advisory.
    """

    INPUT = "input"            # needs information from the user
    REVIEW = "review"          # needs the user to look at specific content
    AUTHORIZE = "authorize"    # needs permission for an action
    CHOICE = "choice"          # needs the user to pick among valid options
    ERROR = "error"            # something broke; the user has a specific fix

    # See ApprovalStatus above — same mixin, same trap. This one is the more
    # dangerous of the two because ``matched_rule`` is built by f-string from a
    # pause kind, and a qualified name written into that column is invisible
    # until someone tries to match on it.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class AuthorizeScope(str, Enum):
    """How WIDE an ``authorize`` request's answer is.

    The approval card's two affirmative buttons have always done two
    materially different jobs, and nothing in the record said so:

      * **Approve** allows *this one instance* — this post, these arguments.
        One-time, tied to concrete content the user just read.
      * **Always** writes a *standing grant* for the capability, forever,
        for every future instance nobody has read yet.

    Naming them makes "Always" what it actually is: a **promotion** of an
    ``action``-scope request into a ``tool``-scope standing grant. The
    promotion is written by ``grant_approval(standing=True)`` — the same
    call that already wrote the workspace auto-approve set. No new
    machinery; the field records which of the two questions was answered.
    """

    #: This one instance, with the content the card displayed.
    ACTION = "action"
    #: A standing capability grant, applying to every future instance.
    TOOL = "tool"

    # See ApprovalStatus above — same mixin, same trap.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: Per-type required payload keys. Enforced at the record layer (the single
#: mint entry point) so a card that cannot answer what/why/what-to-do is
#: never created in the first place.
HITL_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    HitlType.INPUT.value: ("question", "why"),
    HitlType.REVIEW.value: ("diff", "why"),
    HitlType.AUTHORIZE.value: ("action_description", "why"),
    HitlType.CHOICE.value: ("question", "options"),
    HitlType.ERROR.value: ("what_happened", "why", "action_to_take"),
}


#: ``authorize`` payload keys that carry the scope model (§4.3).
#:
#: ``scope`` is stamped by the record layer on every ``authorize`` request, so
#: a card never has to guess which question it is asking. It is deliberately
#: NOT in ``HITL_REQUIRED_PAYLOAD_FIELDS``: a producer must not have to know
#: about scope to mint a valid request, and defaulting it in one place means
#: there is exactly one spelling of "this is a one-time ask".
PAYLOAD_KEY_SCOPE = "scope"
#: Set alongside ``scope=tool`` when the tool scope was reached by PROMOTING
#: an action-scope request (the user pressed "Always"), rather than being
#: asked for as a standing grant up front. Keeps the audit trail honest about
#: what the user was actually shown.
PAYLOAD_KEY_SCOPE_PROMOTED_FROM = "scope_promoted_from"


#: The types where the human's answer is a VERDICT on work the system already
#: produced — "may this proceed?". These, and only these, are governance.
#:
#: ⚠ This is the ONE definition every read surface filters on. Four surfaces
#: (``/human-queue``, the strategist briefing's ``open_approvals``, and the
#: ``human_participation`` / ``risk_governance`` consolidators) used to select
#: on ``status == pending`` and nothing else, so an operator's CAPTCHA pause or
#: a "your local worker is offline" error aged in a *governance* queue, counted
#: as governance friction, and reached the strategist LLM as an approval it
#: could reason about. Each of those four spellings was independent; the point
#: of this object is that they cannot drift apart again.
#:
#: ``input`` / ``choice`` / ``error`` are excluded because the human is
#: SUPPLYING something the run lacks (credentials, an answer, a fix), not
#: granting permission. ``review`` is included: a diff waiting for sign-off is
#: a gate — nothing proceeds until a person blesses it — which is exactly the
#: friction an approval bottleneck measures.
#:
#: Plain ``str`` members: the thing tested against this set is a column value
#: read back off the DB, and a set of plain strings is the one spelling that
#: cannot depend on how a mixin enum hashes.
GOVERNANCE_HITL_TYPES: frozenset[str] = frozenset({
    HitlType.AUTHORIZE.value,
    HitlType.REVIEW.value,
})


#: The complement. Derived, never re-listed — a type added to ``HitlType``
#: without a decision above lands in NEITHER set here and is treated as
#: governance by both the Python and the SQL spelling (see below).
NON_GOVERNANCE_HITL_TYPES: frozenset[str] = frozenset(
    HitlType.values()
) - GOVERNANCE_HITL_TYPES


def is_governance_hitl(hitl_type: Optional[str]) -> bool:
    """Is this request a governance approval (vs. a request for information)?

    The single discriminator for every read surface. ``hitl_type`` — not
    ``matched_rule`` (free text, assembled by f-string) and not ``origin_kind``
    (answers *where* it blocks, not *what* is asked: a lease-origin pause can
    be a genuine ``needs_confirmation`` authorization, and a step-origin row
    can be an ``error``).

    An unknown or missing type counts as governance. The column is NOT NULL and
    backfilled to ``authorize``, so this only bites a type added later without
    a decision here — and the safe failure is the pre-existing behavior
    (counted, shown), never silently hiding work from an operator.
    """
    if not hitl_type:
        return True
    if hitl_type not in HitlType.values():
        return True
    return hitl_type in GOVERNANCE_HITL_TYPES


#: What a mid-execution ("path C") lease pause is actually asking for, keyed by
#: the ``pending_action.kind`` that caused it.
#:
#: ``lease_needs_human`` mints its request through the generic record layer,
#: whose ``hitl_type`` default is ``authorize`` — so before this map every
#: path-C pause was *stored* as an authorization request even though the code
#: minting it says, in as many words, "the human is being asked for
#: information, not permission". That default is what made a CAPTCHA wall
#: indistinguishable from "may I publish this?" on every read surface.
#:
#: ``needs_confirmation`` is the one kind that really is a permission ask
#: (a tool wants to do something destructive and needs an explicit OK), so it
#: stays ``authorize`` and keeps counting as governance.
#:
#: Keys must equal ``LEASE_HITL_CLOSEABLE_KINDS`` — the kinds that path can
#: mint. Guarded by a test, same discipline as the mint/close pair itself.
LEASE_KIND_HITL_TYPES: dict[str, str] = {
    PendingActionKind.HUMAN_INPUT.value: HitlType.INPUT.value,
    PendingActionKind.NEEDS_INPUT.value: HitlType.INPUT.value,
    PendingActionKind.NEEDS_LOGIN.value: HitlType.INPUT.value,
    PendingActionKind.NEEDS_CONFIRMATION.value: HitlType.AUTHORIZE.value,
}
