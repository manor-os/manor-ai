"""The Task coordinator runtime selection has a canonical, identity-complete fingerprint."""
from __future__ import annotations

import pytest

from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
)


@pytest.mark.unit
def test_coordinator_selection_fingerprint_is_canonical_and_identity_complete():
    writer = TaskPlanServiceBinding(
        subscription_id="subscription-writer",
        service_key="writer",
        agent_id="agent-writer",
    )
    research = TaskPlanServiceBinding(
        subscription_id="subscription-research",
        service_key="research",
        agent_id="agent-research",
    )
    selected = CoordinatorRuntimeSelection(
        root=writer,
        bindings=(writer, research),
    )
    reordered = CoordinatorRuntimeSelection(
        root=writer,
        bindings=(research, writer),
    )
    replacement = CoordinatorRuntimeSelection(
        root=TaskPlanServiceBinding(
            subscription_id="replacement-writer",
            service_key="writer",
            agent_id="agent-writer",
        ),
        bindings=(
            TaskPlanServiceBinding(
                subscription_id="replacement-writer",
                service_key="writer",
                agent_id="agent-writer",
            ),
            research,
        ),
    )

    assert selected.stable_identity() == reordered.stable_identity()
    assert selected.stable_fingerprint() == reordered.stable_fingerprint()
    assert selected.stable_fingerprint() != replacement.stable_fingerprint()
