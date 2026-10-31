"""A Task blocked on a recorded capability gap keeps that wait.

The retired Plan/Step planner wrote the capability blocker; the Strategist
still reads it so it does not propose duplicate recovery Tasks.
"""
from types import SimpleNamespace

import pytest

from packages.core.models.task import Task
from packages.core.services import task_capability_recovery as recovery
from packages.core.services.task_execution import task_execution_intent_generation

pytestmark = pytest.mark.oss_regression


def test_strategist_keeps_configuration_wait_on_original_task():
    from packages.core.strategist.service import _suppress_capability_recovery_duplicates

    task = Task(id="task", entity_id="entity", workspace_id="workspace", status="blocked",
                task_type="general", owner_service_key="research", owner_subscription_id="sub",
                delegate_service_keys=[], details={})
    task.details = {recovery.BLOCKER_KEY: {"generation": task_execution_intent_generation(task),
        "missing_capabilities": ["web.safe_search"]}}
    proposal = SimpleNamespace(notes=None, tasks=[
        SimpleNamespace(task_key="recover", owner_service_key="research", required_capabilities=["web.safe_search"], delegate_service_keys=[], depends_on_task_keys=[]),
        SimpleNamespace(task_key="dependent", owner_service_key="analysis", required_capabilities=[], delegate_service_keys=[], depends_on_task_keys=["recover"]),
        SimpleNamespace(task_key="independent", owner_service_key="research", required_capabilities=["file.write"], delegate_service_keys=[], depends_on_task_keys=[]),
    ])
    _suppress_capability_recovery_duplicates(proposal, SimpleNamespace(recent_tasks=[task], service_capabilities={}))
    assert [item.task_key for item in proposal.tasks] == ["independent"]
