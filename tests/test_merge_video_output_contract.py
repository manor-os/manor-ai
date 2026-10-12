"""Merge approval, execution and commit share one prepared output identity."""
from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.authorization_receipts import RuntimeToolAuthorizationWriteError
from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
from packages.core.ai.runtime.media_outputs import runtime_generated_media_target
from packages.core.ai.tools import media_tools
from packages.core.services.generated_media_naming import GeneratedMediaOutputPlan
from tests.test_generated_media_authorization_contract import (
    ENTITY, FOLDER, _approve, _authorized, _consume_media_approval,
    _granted_media_approval,
)
from tests.test_generated_media_authorization_contract import (  # noqa: F401 — pytest fixtures
    approval_store as approval_store,
    media_storage as media_storage,
)


HOME = f"Workspaces/_by_id/{FOLDER}"


def _target(root, name, *, workspace_base=HOME):
    return runtime_generated_media_target(
        kind="video", entity_id=ENTITY, workspace_base_dir=workspace_base,
        ext=".mp4", prompt=name, output_name=name, entity_root=str(root),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "Videos/P1-Final-Merged.mp4",
    "Workspaces/Media QA/Videos/P1-Final-Merged.mp4",
    f"{HOME}/Videos/P1-Final-Merged.mp4",
])
async def test_merge_paths_authorize_the_exact_execution_target(media_storage, name):
    sources = [f"{HOME}/Videos/source.mp4"]
    request, receipt = await _approve(
        name, tool_name="merge_videos", params={"video_paths": sources}, task_id=None,
    )
    assert request.arguments["video_paths"] == sources
    assert receipt.resource_ids == (f"{HOME}/Videos/p1-final-merged.mp4",)
    assert len(receipt.media_output_plan.targets) == 1
    with _authorized(request, receipt):
        target = _target(media_storage, name)
        runtime_write_entity_file_atomic(
            ENTITY, target.rel_path, b"synthetic output", require_missing=True,
        )
    assert target.rel_path == receipt.resource_ids[0]
    assert (media_storage / target.rel_path).read_bytes() == b"synthetic output"


@pytest.mark.asyncio
async def test_merge_late_collision_never_renames_or_overwrites(media_storage):
    name = "Videos/final.mp4"
    request, receipt = await _approve(name, tool_name="merge_videos", task_id=None)
    approved = receipt.media_output_plan.targets[0]
    existing = media_storage / approved.rel_path
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"concurrent owner")
    with _authorized(request, receipt):
        assert _target(media_storage, name).rel_path == approved.rel_path
        with pytest.raises(RuntimeToolAuthorizationWriteError, match="appeared"):
            runtime_write_entity_file_atomic(
                ENTITY, approved.rel_path, b"merge output", require_missing=True,
            )
    assert existing.read_bytes() == b"concurrent owner"
    assert not existing.with_name("final_1.mp4").exists()


@pytest.mark.asyncio
async def test_merge_resume_rebinds_only_trusted_root(media_storage):
    name = "Videos/final.mp4"
    request, receipt = await _approve(name, tool_name="merge_videos", task_id=None)
    restored = GeneratedMediaOutputPlan.from_dict(receipt.media_output_plan.to_dict())
    assert restored.targets[0].abs_path is None
    with _authorized(request, replace(receipt, media_output_plan=restored)):
        target = _target(media_storage, name)
    assert target.rel_path == restored.targets[0].rel_path
    assert target.abs_path == str(media_storage / target.rel_path)


@pytest.mark.asyncio
async def test_merge_resume_collision_invalidates_same_operation_approval(media_storage, approval_store):
    name = "Videos/final.mp4"
    request, receipt = await _approve(name, tool_name="merge_videos", task_id=None)
    row = await _granted_media_approval(approval_store, request, receipt)
    occupied = media_storage / receipt.media_output_plan.targets[0].rel_path
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"new owner after approval")
    resumed_request, resumed_receipt = await _approve(name, tool_name="merge_videos", task_id=None)
    result = json.loads(await _consume_media_approval(
        approval_store, row, resumed_request, resumed_receipt,
    ))
    assert row.status == "expired"
    assert row.consumed_at is None
    assert result["error"] == "blocked_by_governance"
    assert result["matched_rule"] == "approval.operation_changed"
    assert result["status"] == "expired" and result["action_executed"] is False
    assert result["approval_token"] == row.id
    assert "__hitl__" not in result and "hitl" not in result
    assert list(approval_store.requests) == [row.id]
    replay = json.loads(await _consume_media_approval(
        approval_store, row, resumed_request, resumed_receipt,
    ))
    assert replay["error"] == "approval_not_granted" and replay["status"] == "expired"
    assert row.status == "expired" and row.consumed_at is None
    assert list(approval_store.requests) == [row.id]
    assert occupied.read_bytes() == b"new owner after approval"
    assert resumed_receipt.resource_ids != receipt.resource_ids
    assert all(not (media_storage / path).exists() for path in resumed_receipt.resource_ids)
    assert not occupied.with_name("final_1.mp4").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["name", "scope", "missing_plan"])
async def test_merge_refuses_authorization_drift(media_storage, change):
    request, receipt = await _approve("Videos/final.mp4", tool_name="merge_videos", task_id=None)
    if change == "missing_plan":
        receipt = replace(receipt, media_output_plan=None)
    with _authorized(request, receipt):
        with pytest.raises(RuntimeToolAuthorizationWriteError):
            _target(
                media_storage, "Videos/changed.mp4" if change == "name" else "Videos/final.mp4",
                workspace_base="Workspaces/_by_id/other" if change == "scope" else HOME,
            )
    assert not (media_storage / HOME / "Videos/final.mp4").exists()


@pytest.mark.asyncio
async def test_merge_keeps_input_acl_before_processing(media_storage, monkeypatch):
    source = media_storage / HOME / "Videos/private.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"private input")
    deny = AsyncMock(return_value=json.dumps({"error": "file_permission_denied"}))
    render = AsyncMock(side_effect=AssertionError("denied input reached renderer"))
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "runtime_guard_file_read_access", deny)
    monkeypatch.setattr(media_tools, "_merge_video_files", render)
    result = json.loads(await media_tools._merge_videos_handler(
        entity_id=ENTITY,
        workspace_id="ws_media_qa",
        _user_id_from_context="user_media_qa",
        video_paths=[f"{HOME}/Videos/private.mp4"],
        output_name="Videos/final.mp4",
    ))
    assert result["code"] == "merge_failed"
    assert "access denied" in result["error"]
    assert deny.await_args.kwargs["paths"] == [f"{HOME}/Videos/private.mp4"]
    render.assert_not_awaited()
    assert source.read_bytes() == b"private input"


@pytest.mark.parametrize("source", [
    "Workspaces/_by_id/other-folder/Videos/source.mp4",
    "Workspaces/Other Workspace/Videos/source.mp4",
    "Workspaces/Media QA/Videos/source.mp4",
])
def test_explicit_source_identity_is_never_reassigned_to_current_workspace(source):
    assert media_tools._workspace_media_reference(
        source, entity_id=ENTITY, workspace_base_dir=HOME,
    ) == source


@pytest.mark.asyncio
async def test_merge_source_acl_checks_original_workspace_not_same_named_local_file(media_storage, monkeypatch):
    source_path = "Workspaces/_by_id/other-folder/Videos/source.mp4"
    for path, data in ((source_path, b"private other workspace"), (f"{HOME}/Videos/source.mp4", b"local decoy")):
        file = media_storage / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    deny = AsyncMock(return_value=json.dumps({"error": "file_permission_denied"}))
    render = AsyncMock(side_effect=AssertionError("denied source reached renderer"))
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "runtime_guard_file_read_access", deny)
    monkeypatch.setattr(media_tools, "_merge_video_files", render)
    result = json.loads(await media_tools._merge_videos_handler(
        entity_id=ENTITY, workspace_id="ws_media_qa", _user_id_from_context="user_media_qa",
        video_paths=[source_path], output_name="Videos/final.mp4",
    ))
    assert result["code"] == "merge_failed"
    assert deny.await_args.kwargs["paths"] == [source_path]
    render.assert_not_awaited()
