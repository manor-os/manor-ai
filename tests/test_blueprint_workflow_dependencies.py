from __future__ import annotations

import pytest

from packages.core.blueprints.workflow_dependencies import (
    WorkflowDependencyError,
    WorkflowDependencyFactory,
)
from packages.core.blueprints.upgrade import _desired_content


def test_workflow_dependency_factory_projects_and_materializes_new_identity():
    source_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    installed_id = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    steps = [{
        "id": "child",
        "type": "subworkflow",
        "config": {"workflow_id": source_id, "input": {"topic": "launch"}},
    }]

    portable = WorkflowDependencyFactory.to_portable(
        steps,
        workflow_key_by_id={source_id: "child-flow"},
    )
    assert portable[0]["config"] == {
        "source_workflow_key": "child-flow",
        "input": {"topic": "launch"},
    }
    assert steps[0]["config"]["workflow_id"] == source_id

    runtime = WorkflowDependencyFactory.to_runtime(
        portable,
        workflow_id_by_key={"child-flow": installed_id},
    )
    assert runtime[0]["config"] == {
        "source_workflow_key": "child-flow",
        "workflow_id": installed_id,
        "input": {"topic": "launch"},
    }


def test_workflow_dependency_factory_fails_closed_on_missing_component():
    with pytest.raises(WorkflowDependencyError, match="not part"):
        WorkflowDependencyFactory.to_portable(
            [{
                "id": "child",
                "type": "foreach_subworkflow",
                "config": {"workflow_id": "source-only-id"},
            }],
            workflow_key_by_id={},
        )

    with pytest.raises(WorkflowDependencyError, match="missing Blueprint Flow"):
        WorkflowDependencyFactory.to_runtime(
            [{
                "id": "child",
                "type": "subworkflow",
                "config": {"source_workflow_key": "missing"},
            }],
            workflow_id_by_key={},
        )


def test_missing_source_keys_reports_only_unmaterialized_dependencies():
    steps = [
        {
            "id": "known",
            "type": "subworkflow",
            "config": {"source_workflow_key": "known-flow"},
        },
        {
            "id": "missing",
            "type": "foreach_subworkflow",
            "config": {"workflow_id": "missing-flow"},
        },
    ]

    assert WorkflowDependencyFactory.missing_source_keys(
        steps,
        workflow_id_by_key={"known-flow": "01KNOWNFLOW00000000000000"},
    ) == frozenset({"missing-flow"})


def test_upgrade_projection_uses_installed_subworkflow_identity():
    installed_id = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    desired = _desired_content(
        "workflow",
        {
            "slug": "parent-flow",
            "steps": [{
                "id": "child",
                "type": "subworkflow",
                "config": {"source_workflow_key": "child-flow"},
            }],
        },
        workflow_id_by_key={"child-flow": installed_id},
    )

    assert desired["steps"][1]["config"] == {
        "source_workflow_key": "child-flow",
        "workflow_id": installed_id,
    }
