"""``messages.pending_action.kind`` — the card vocabulary, as a closed set.

Every interactive chat card is a ``pending_action`` blob whose ``kind`` picks
the branch that renders it and the branch that resolves it. Those two branches
live in different files (``ChatActionCard.tsx`` renders, ``workspace_chat.
resolve_chat_action`` decides), and until now the word joining them was spelled
fresh at each of ~40 sites. That is the same shape as the supervisor-verdict
incident: a card whose kind nothing routes is not an error, it is a card with
no buttons — and a kind minted but never closeable strands its step in
``waiting_human`` forever (see ``LEASE_HITL_CLOSEABLE_KINDS``).

**Wire values are persisted.** These strings live in the ``messages.
pending_action`` JSONB column of rows written months ago and of rows in flight
right now. The enum is ``str``-based precisely so that an existing
``pa["kind"] == "governance_approval"`` comparison, a ``json.dumps`` of a card,
and a JSONB round-trip all keep behaving exactly as before. Renaming a member
is free; changing a *value* is a data migration.
"""
from __future__ import annotations

from enum import Enum


class PendingActionKind(str, Enum):
    """Every ``pending_action.kind`` any producer in this repo writes."""

    # ── MCP-tool-level pauses (packages/core/ai/pending_action.py) ──
    #: Tool hit a login / SSO / CAPTCHA wall.
    NEEDS_LOGIN = "needs_login"
    #: Tool could not fill one or more required fields.
    NEEDS_INPUT = "needs_input"
    #: Tool wants to do a destructive thing and needs an explicit OK.
    NEEDS_CONFIRMATION = "needs_confirmation"

    #: The free-form text-input card the chat notifier builds when a lease
    #: pauses without a structured payload.
    HUMAN_INPUT = "human_input"

    # ── Governance / policy ──
    #: A gated step tripped the approval gate. Backed by a HitlRequest.
    GOVERNANCE_APPROVAL = "governance_approval"

    # ── Strategist review loop ──
    #: A proposal cohort is waiting on approve / reject.
    APPROVE_PROPOSALS = "approve_proposals"
    #: A strategist review failed and offers a retry.
    RETRY_STRATEGIST_REVIEW = "retry_strategist_review"

    # ── Workspace self-modification ──
    #: An agent proposes a change to the workspace's own structure.
    WORKSPACE_OPERATION_REVIEW = "workspace_operation_review"
    #: An outbound message to a real external recipient needs sign-off.
    EXTERNAL_MESSAGE_APPROVAL = "external_message_approval"

    # ── Workflow runs ──
    #: A workflow binding needs its starter inputs before it can run.
    WORKFLOW_STARTER_INPUT = "workflow_starter_input"
    #: A ``wait`` step of type "input" inside a running workflow.
    WORKFLOW_INPUT = "workflow_input"
    #: A ``wait`` step of type "approval" inside a running workflow.
    WORKFLOW_APPROVAL = "workflow_approval"
    #: A failed workflow run offering a corrected retry.
    WORKFLOW_RETRY = "workflow_retry"

    # ``str(member)`` and ``f"{member}"`` must render the wire value, not
    # "PendingActionKind.HUMAN_INPUT". Without these, a mixin enum stringifies
    # to its qualified name — and every ``str(pa.get("kind") or KIND_HUMAN_INPUT)``
    # and every f-string that builds a rule name or a log line silently starts
    # producing a word no reader has ever heard of. That is not a cosmetic
    # difference: it is how the dispatcher stopped minting requests for
    # fallback cards the first time this enum was introduced.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: The kinds whose card is hosted by a workflow run, so the chat surface knows
#: to mount the run host alongside them. Derived here rather than re-listed at
#: the router, which is what let this set and the resolve branches drift.
WORKFLOW_RUN_ACTION_KINDS: frozenset[str] = frozenset({
    PendingActionKind.WORKFLOW_APPROVAL.value,
    PendingActionKind.WORKFLOW_INPUT.value,
    PendingActionKind.WORKFLOW_RETRY.value,
    PendingActionKind.WORKFLOW_STARTER_INPUT.value,
})
