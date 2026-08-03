"""Phase 3 §4.3 — "Approve" and "Always" are two different questions.

§4.3 names them: an ``authorize`` request is minted at ``scope=action`` ("may I
do this one thing, with the content you just read?"), and "Always" is a
PROMOTION of it to ``scope=tool`` — a standing capability grant. The promotion
is the existing ``grant_approval(standing=True)``; the scope is what makes the
audit trail say which question was answered.

Scope DESCRIBES the promotion; it does not restrict it. There is no capability
class the system withholds "Always" from — the user is the authority on what
they want standing, and ``never_allow`` (which they can edit) is the only hard
block. See ``test_unified_approvals`` for the publish-class proof.
"""
from __future__ import annotations

import pytest

from packages.core.constants.approvals import AuthorizeScope, HitlType
from packages.core.governance.approvals import validate_hitl_payload
from packages.core.services.hitl_options import (
    approval_options,
    one_time_approval_options,
)


# ── the option vocabulary ──────────────────────────────────


def test_every_approval_card_offers_always():
    """No capability tier withholds the button. One vocabulary, no exceptions."""
    assert approval_options() == ["approve", "always_approve", "reject"]
    assert approval_options(["approve", "always_approve", "reject"]) == [
        "approve", "always_approve", "reject",
    ]


def test_a_card_with_no_standing_subject_offers_approve_once():
    """Not a restriction on the user — the ABSENCE of a standing subject.

    A ``review`` card is a verdict on ONE diff; "always apply whatever the next
    draft says" is not something anyone can consent to. An explicit list is
    filtered too, so a card minted with the three-button vocabulary still
    renders no "Always" here.
    """
    assert one_time_approval_options() == ["approve", "reject"]
    assert one_time_approval_options(
        ["approve", "always_approve", "reject"],
    ) == ["approve", "reject"]


# ── §4.3 scope ─────────────────────────────────────────────────────


def test_an_authorize_payload_is_stamped_action_scope():
    """A request is minted because someone asked to do ONE thing."""
    assert validate_hitl_payload(HitlType.AUTHORIZE.value, {})["scope"] == (
        AuthorizeScope.ACTION.value
    )
    typed = validate_hitl_payload(HitlType.AUTHORIZE.value, {
        "action_description": "Publish the launch post", "why": "policy rule",
    })
    assert typed["scope"] == AuthorizeScope.ACTION.value


def test_scope_is_a_closed_vocabulary():
    with pytest.raises(ValueError, match="unknown scope"):
        validate_hitl_payload(HitlType.AUTHORIZE.value, {
            "action_description": "x", "why": "y", "scope": "everything",
        })
    promoted = validate_hitl_payload(HitlType.AUTHORIZE.value, {
        "action_description": "x", "why": "y",
        "scope": AuthorizeScope.TOOL.value,
    })
    assert promoted["scope"] == AuthorizeScope.TOOL.value


def test_only_authorize_carries_a_scope():
    """The other four types are not permission asks, so "how wide is this
    permission" is not a question they have."""
    for hitl_type, payload in (
        (HitlType.INPUT.value, {"question": "q", "why": "w"}),
        (HitlType.REVIEW.value, {"diff": {"changed_keys": []}, "why": "w"}),
        (HitlType.CHOICE.value, {"question": "q", "options": [{"key": "a"}]}),
        (HitlType.ERROR.value, {
            "what_happened": "w", "why": "y", "action_to_take": "a",
        }),
    ):
        assert "scope" not in validate_hitl_payload(hitl_type, payload)


def test_validation_is_idempotent():
    """It runs twice on the mint path — once in ``mint_approval_request`` and
    again in ``_create_pending_request`` on that function's own output. The
    stamped scope must not be mistaken for producer-supplied copy the second
    time round, or every untyped authorize request fails to mint."""
    once = validate_hitl_payload(HitlType.AUTHORIZE.value, {})
    assert validate_hitl_payload(HitlType.AUTHORIZE.value, once) == once
