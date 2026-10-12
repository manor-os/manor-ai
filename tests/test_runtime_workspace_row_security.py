from types import SimpleNamespace
from unittest.mock import AsyncMock

import json
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_allowed", [False, True])
async def test_global_knowledge_read_does_not_bypass_workspace_artifact_acl(
    monkeypatch, workspace_allowed,
):
    from packages.core.services import document_access

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class WorkspaceResult:
        def all(self):
            return [("workspace-1", "artifact-1", None)]

    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(), WorkspaceResult()]))
    ctx = SimpleNamespace(
        entity_available=True,
        can_read_all_documents=True,
        preload_documents=AsyncMock(),
        _workspace_readable=AsyncMock(return_value=workspace_allowed),
    )
    monkeypatch.setattr(
        document_access.DocumentAccessContext,
        "load",
        AsyncMock(return_value=ctx),
    )
    path = "Workspaces/_by_id/artifact-1/unprojected.txt"

    blocked = await document_access.unreadable_document_paths(
        db,
        entity_id="entity-1",
        rel_paths=[path],
        user_id="knowledge-manager",
    )

    assert blocked == (set() if workspace_allowed else {path})


def test_userless_runtime_does_not_trust_model_workspace_argument():
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
        runtime_trusted_workspace_id_from_context,
    )

    forged = runtime_tool_call_context_from_kwargs({"workspace_id": "workspace-forged"})
    assert runtime_trusted_workspace_id_from_context(forged) is None

    envelope = SimpleNamespace(workspace_id="workspace-trusted")
    trusted = runtime_tool_call_context_from_kwargs({
        "workspace_id": "workspace-trusted",
        "_runtime_envelope_from_context": envelope,
    })
    assert runtime_trusted_workspace_id_from_context(trusted) == "workspace-trusted"


@pytest.mark.asyncio
async def test_entity_wide_runtime_reads_fail_closed_without_actor_scope():
    from packages.core.ai.runtime.agent_provisioning import (
        runtime_query_entity_agents_action,
    )
    from packages.core.ai.runtime.scheduling import (
        runtime_list_scheduled_jobs_action,
        runtime_query_scheduled_jobs_action,
    )
    from packages.core.ai.runtime.task_actions import (
        runtime_get_task_details_action,
        runtime_search_tasks_action,
    )
    from packages.core.ai.runtime.manor_actions import (
        runtime_manor_list_document_folders,
        runtime_manor_list_document_groups,
        runtime_manor_list_workspaces,
    )

    assert "context is required" in json.loads(
        await runtime_search_tasks_action(entity_id="entity-1")
    )["error"]
    assert "context is required" in json.loads(
        await runtime_get_task_details_action(
            entity_id="entity-1",
            params={"task_id": "task-1"},
        )
    )["error"]
    assert "context is required" in json.loads(
        await runtime_list_scheduled_jobs_action(entity_id="entity-1")
    )["error"]
    assert "context is required" in json.loads(
        await runtime_query_scheduled_jobs_action(entity_id="entity-1")
    )["error"]
    assert "context is required" in json.loads(
        await runtime_query_entity_agents_action(
            entity_id="entity-1",
            enforce_viewer_scope=True,
        )
    )["error"]
    assert "context is required" in json.loads(
        await runtime_manor_list_workspaces(object(), entity_id="entity-1")
    )["error"]
    assert "context is required" in json.loads(
        await runtime_manor_list_document_folders(
            object(), entity_id="entity-1"
        )
    )["error"]
    assert "context is required" in json.loads(
        await runtime_manor_list_document_groups(
            object(), entity_id="entity-1"
        )
    )["error"]


@pytest.mark.asyncio
async def test_runtime_scheduled_job_mutation_requires_target_workspace_write(
    monkeypatch,
):
    from packages.core.ai.runtime.scheduling import (
        _runtime_can_manage_scheduled_job,
    )
    from packages.core.services import workspace_access

    job = SimpleNamespace(workspace_id="workspace-private")
    can_write = AsyncMock(return_value=False)
    monkeypatch.setattr(workspace_access, "user_can_write_workspace_id", can_write)

    assert not await _runtime_can_manage_scheduled_job(
        object(),
        entity_id="entity-1",
        job=job,
        user_id="admin-user",
        workspace_id=None,
    )
    assert not await _runtime_can_manage_scheduled_job(
        object(),
        entity_id="entity-1",
        job=job,
        user_id=None,
        workspace_id="workspace-other",
    )
    assert await _runtime_can_manage_scheduled_job(
        object(),
        entity_id="entity-1",
        job=job,
        user_id=None,
        workspace_id="workspace-private",
    )


@pytest.mark.asyncio
async def test_workflow_batch_capabilities_do_not_grant_admin_hidden_workspace_control(
    monkeypatch,
):
    from apps.api.routers import workflows

    async def _no_writable_workspaces(*_args, **_kwargs):
        return set()

    async def _admin_role(*_args, **_kwargs):
        return "admin", True

    monkeypatch.setattr(
        workflows,
        "user_writable_workspace_ids",
        _no_writable_workspaces,
    )
    monkeypatch.setattr(
        workflows,
        "user_readable_workspace_ids",
        _no_writable_workspaces,
    )
    monkeypatch.setattr(
        workflows,
        "resolve_workspace_read_access",
        _admin_role,
    )
    runs = [
        SimpleNamespace(
            id="hidden-workspace-run",
            workspace_id="workspace-hidden",
            started_by="other-user",
        ),
        SimpleNamespace(
            id="entity-run",
            workspace_id=None,
            started_by="other-user",
        ),
        SimpleNamespace(
            id="own-run",
            workspace_id="workspace-hidden",
            started_by="admin-user",
        ),
    ]
    user = SimpleNamespace(
        id="admin-user",
        entity_id="entity-1",
        role="admin",
    )

    capabilities = await workflows._run_control_capabilities(
        object(),
        runs,
        user,
    )

    assert capabilities == {
        "hidden-workspace-run": False,
        "entity-run": True,
        "own-run": False,
    }
