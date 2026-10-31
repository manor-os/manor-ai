"""``request_input`` cards must stay answerable once a worker-job Step parks.

A ``needs_input`` card parks a Dispatcher worker-job Step ``waiting_human``;
the chat bridge answers it through the Task blocker contract. Minting a kind
the chat endpoint cannot close strands the Step forever.
"""
from __future__ import annotations

from packages.core.ai.pending_action import KIND_NEEDS_INPUT, LEASE_HITL_CLOSEABLE_KINDS
from packages.core.services.task_blockers import ANSWERABLE_BLOCKER_KINDS


def test_needs_input_is_closeable_on_the_lease_and_answerable_from_chat():
    """Minting a kind the chat endpoint cannot close strands the step forever
    (see LEASE_HITL_CLOSEABLE_KINDS); needs_input must stay in both sets."""
    assert KIND_NEEDS_INPUT.value in LEASE_HITL_CLOSEABLE_KINDS
    assert KIND_NEEDS_INPUT.value in ANSWERABLE_BLOCKER_KINDS
