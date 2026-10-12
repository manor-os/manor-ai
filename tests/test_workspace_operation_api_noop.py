"""Trusted API target-state retries remain idempotent without faking an apply."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from fastapi import HTTPException
import pytest

from apps.api.routers import workspaces as router
from packages.core.models.workspace import Workspace
from packages.core.services import workspace_operation_service as operations


@pytest.fixture
def operation_api(monkeypatch):
    workspace = SimpleNamespace(
        id="workspace-1", entity_id="entity-1", operation_revision=7,
        status="paused", heartbeat_enabled=False,
    )
    draft = SimpleNamespace(id="new-internal-draft", base_revision=3)
    db = SimpleNamespace(
        get=AsyncMock(return_value=workspace),
        delete=AsyncMock(),
    )
    create = AsyncMock(return_value=draft)
    apply = AsyncMock(side_effect=operations.OperationValidationError({
        "valid": False,
        "errors": [{"code": "no_changes"}],
        "warnings": [],
    }))
    sync = AsyncMock()
    monkeypatch.setattr(operations, "create_operation_draft", create)
    monkeypatch.setattr(operations, "apply_operation_draft", apply)
    monkeypatch.setattr(router, "sync_workspace_runtime_schedules", sync)
    timeline = Mock()
    for name, method in (("create", create), ("apply", apply), ("get", db.get),
                         ("delete", db.delete), ("sync", sync)):
        timeline.attach_mock(method, name)
    return SimpleNamespace(
        workspace=workspace, draft=draft, db=db, create=create,
        apply=apply, sync=sync, timeline=timeline,
    )


async def invoke(api):
    return await router._apply_workspace_operation_patches(
        api.db,
        workspace_id="workspace-1",
        entity_id="entity-1",
        user_id="user-1",
        patches=[{"op": "heartbeat_policy.update", "payload": {"enabled": False}}],
        source_event_id="api_workspace_pause",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status,revision", [("paused", 7), ("active", 11), ("active", None)])
async def test_api_noop_revalidates_then_syncs_current_state_without_revision_or_applied_receipt(
    operation_api, status, revision,
):
    api = operation_api
    api.workspace.status = status
    api.workspace.operation_revision = revision
    before = deepcopy(vars(api.workspace))

    result = await invoke(api)

    assert result == {
        "status": "unchanged", "draft": None,
        "workspace_revision": revision or 0, "tasks_created": [],
    }
    assert vars(api.workspace) == before
    assert api.timeline.mock_calls == [
        call.create(
            api.db, "workspace-1", "entity-1", user_id="user-1",
            source_event_id="api_workspace_pause",
            initial_patches=[{"op": "heartbeat_policy.update", "payload": {"enabled": False}}],
        ),
        call.apply(
            api.db, api.draft.id, "entity-1", "workspace-1",
            user_id="user-1", user_confirmation=True,
        ),
        call.get(Workspace, "workspace-1"),
        call.delete(api.draft),
        call.sync(api.db, api.workspace, user_id="user-1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("validation", [
    {"errors": []},
    {"valid": False},
    {"errors": [{"code": "no_effects", "message": "No applicable effects"}]},
    {"errors": [{"code": "invalid_patch"}]},
    {"errors": [{"code": "no_changes"}, {"code": "no_effects"}]},
    {"errors": [{"message": "Missing error code"}]},
])
async def test_api_noop_does_not_swallow_empty_other_or_mixed_validation_errors(
    operation_api, validation,
):
    api = operation_api
    error = operations.OperationValidationError(validation)
    api.apply.side_effect = error

    with pytest.raises(HTTPException) as caught:
        await invoke(api)

    assert caught.value.status_code == 400
    assert caught.value.detail is validation
    assert caught.value.__cause__ is error
    api.apply.assert_awaited_once()
    api.db.get.assert_not_awaited()
    api.db.delete.assert_not_awaited()
    api.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_noop_does_not_swallow_operation_conflicts(operation_api):
    api = operation_api
    error = operations.OperationConflictError("workspace operation draft is stale")
    api.apply.side_effect = error

    with pytest.raises(HTTPException) as caught:
        await invoke(api)

    assert caught.value.status_code == 409
    assert caught.value.detail == str(error)
    api.db.get.assert_not_awaited()
    api.db.delete.assert_not_awaited()
    api.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_noop_keeps_missing_workspace_during_create_as_404(operation_api):
    api = operation_api
    api.create.return_value = None

    with pytest.raises(HTTPException) as caught:
        await invoke(api)

    assert caught.value.status_code == 404
    api.apply.assert_not_awaited()
    api.db.get.assert_not_awaited()
    api.db.delete.assert_not_awaited()
    api.sync.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reloaded", [None, SimpleNamespace(entity_id="other-entity")])
async def test_api_noop_never_syncs_a_missing_or_foreign_workspace(operation_api, reloaded):
    api = operation_api
    api.db.get.return_value = reloaded

    with pytest.raises(HTTPException) as caught:
        await invoke(api)

    assert caught.value.status_code == 404
    api.apply.assert_awaited_once()
    api.db.get.assert_awaited_once_with(Workspace, "workspace-1")
    api.db.delete.assert_not_awaited()
    api.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_successful_apply_retains_the_real_receipt_and_draft(operation_api):
    api = operation_api
    receipt = {"status": "applied", "workspace_revision": 8, "draft": {"id": api.draft.id}}
    api.apply.side_effect = None
    api.apply.return_value = receipt

    assert await invoke(api) is receipt
    api.apply.assert_awaited_once()
    api.db.get.assert_not_awaited()
    api.db.delete.assert_not_awaited()
    api.sync.assert_not_awaited()
