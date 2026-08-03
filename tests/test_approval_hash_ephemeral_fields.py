"""A page-snapshot token must not invalidate an approval.

Production incident (2026-07-27, Chrome publishing path): the operator
approved a `Chrome Fill Or Select` gate five times and the post was never
written. The three recorded payloads were byte-identical apart from
``snapshot_id`` — the browser's page-snapshot version token, which changes
every time the agent re-reads the page to locate the element again after the
approval. Because it was part of the approval args hash, each retry read as
"the content changed after approval": the grant was superseded, a new card
minted, and the loop never terminated.

The exact payloads below are copied from the production records.
"""
from __future__ import annotations

from packages.core.ai.runtime.approval_messages import (
    approval_args_hash,
    approval_preview_arguments,
)

_VALUE = (
    "Manor AI runs product work in your own Chrome with visible tabs, "
    "grounded page reads, and action-time approvals before external posts "
    "or submits. Built for safer browser automation you can inspect."
)


def _payload(snapshot_id: str, **overrides):
    base = {
        "ref": "e1",
        "tabId": 895206222,
        "value": _VALUE,
        "snapshot_id": snapshot_id,
    }
    base.update(overrides)
    return base


def test_snapshot_id_change_does_not_invalidate_the_approval():
    """The prod loop: same element, same text, re-read page → new snapshot."""
    approved = _payload("snap-tab-gc7kd6")   # what the user approved, 20:12:41
    retried = _payload("snap-tab-9qjlt7")    # what came back, 20:13:27
    assert approval_args_hash(approved) == approval_args_hash(retried)


def test_changing_the_typed_text_still_invalidates():
    """The guarantee that must survive: approving one message never
    authorizes posting a different one."""
    approved = _payload("snap-tab-gc7kd6")
    tampered = _payload("snap-tab-gc7kd6", value="Totally different post text")
    assert approval_args_hash(approved) != approval_args_hash(tampered)


def test_changing_the_target_element_still_invalidates():
    """`ref` stays hashed: approving a fill on one element must not authorize
    filling another."""
    approved = _payload("snap-tab-gc7kd6")
    other_element = _payload("snap-tab-gc7kd6", ref="e9")
    assert approval_args_hash(approved) != approval_args_hash(other_element)


def test_changing_the_tab_still_invalidates():
    """`tabId` stays hashed for the same reason — a different tab is a
    different destination."""
    approved = _payload("snap-tab-gc7kd6")
    other_tab = _payload("snap-tab-gc7kd6", tabId=111222333)
    assert approval_args_hash(approved) != approval_args_hash(other_tab)


def test_snapshot_id_is_dropped_from_the_card_preview():
    """It is machine bookkeeping; showing it to a human reviewing a post is
    noise."""
    preview = approval_preview_arguments(_payload("snap-tab-gc7kd6"))
    assert "snapshot_id" not in preview
    assert preview["value"] == _VALUE
    assert preview["ref"] == "e1"
