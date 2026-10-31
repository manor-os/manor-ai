"""Composition freezes and saves the MP4 plus its exact editable companion.

Renderer and SQL boundaries are mocked; naming, approval receipts, both atomic
file writers, recipe generation, and model error projection remain real.
"""
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.authorization_receipts import (
    RuntimeToolAuthorizationWriteError,
    WorkspaceFileResourceMatchKind,
)
from packages.core.ai.runtime.media_outputs import (
    runtime_generated_media_target,
    runtime_media_output_approval_scope,
    runtime_media_output_policy_resources,
)
from packages.core.ai.runtime.streams import runtime_tool_call_error
from packages.core.ai.runtime.tool_error_contract import model_tool_error_result
from packages.core.ai.tools import media_tools
from packages.core.services.generated_media_naming import GeneratedMediaOutputPlan
from tests.test_generated_media_authorization_contract import (
    BASE, ENTITY, FOLDER, TASK, WORKSPACE, _approve, _authorized,
    _consume_media_approval, _granted_media_approval,
    approval_store as approval_store, media_storage as media_storage,
)


HOME = f"Workspaces/_by_id/{FOLDER}"


@pytest.fixture
def composition(media_storage, monkeypatch):
    source = media_storage / HOME / "clean.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unchanged clean source")
    timeline = media_storage / HOME / "timeline.json"
    timeline.write_text(json.dumps({
        "delivery": {"clean_picture_master": f"{HOME}/clean.mp4"},
        "spec": {"width": 1280, "height": 720, "fps": 30},
        "audio_tracks": [],
    }))
    rendered = []

    async def render(args, **_kwargs):
        if str(args[0]).endswith("ffprobe"):
            return "", ""  # The final mix's peak check: this synthetic render has no audio.
        rendered.append(args)
        Path(args[-1]).write_bytes(b"synthetic rendered mp4")
        return "", ""

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_run_process", render)
    monkeypatch.setattr(media_tools, "_probe_media", AsyncMock(return_value={
        "duration_seconds": 5.0, "has_audio": False, "width": 1280, "height": 720,
    }))
    monkeypatch.setattr(media_tools, "_video_frame_timing", AsyncMock(return_value=(30.0, 150)))
    monkeypatch.setattr(media_tools, "_lookup_documents_by_rel_paths", AsyncMock(return_value={}))
    monkeypatch.setattr(media_tools, "_register_merged_video", AsyncMock(return_value="video-document"))
    monkeypatch.setattr(media_tools, "_register_video_editor_recipe", AsyncMock(return_value="recipe-document"))
    monkeypatch.setattr(media_tools, "_attach_video_editor_recipe_to_video", AsyncMock())
    monkeypatch.setattr("packages.core.services.knowledge_sync.bind_document_to_workspace", AsyncMock())
    return rendered


async def _compose(request, receipt, name, *, task_id=None):
    with _authorized(request, receipt):
        return json.loads(await media_tools._compose_video_timeline_handler(
            entity_id=ENTITY, user_id="user_media_qa", workspace_id=WORKSPACE,
            task_id=task_id, timeline_path=f"{HOME}/timeline.json", output_name=name,
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("name,task_id,parent", [
    ("sora-demo-with-subs.mp4", None, HOME),
    ("Videos/Final_Output.mp4", None, f"{HOME}/Videos"),
    (f"{HOME}/Videos/Final_Output.mp4", None, f"{HOME}/Videos"),
    ("Final_Output.mp4", TASK, f"{BASE}/videos"),
])
async def test_compose_real_sidecar_write_matches_frozen_exact_pair(
    media_storage, composition, name, task_id, parent,
):
    request, receipt = await _approve(name, tool_name="compose_video_timeline", task_id=task_id)
    plan = receipt.media_output_plan
    assert len(plan.targets) == len(plan.companion_paths) == 1
    assert all(item.match_kind is WorkspaceFileResourceMatchKind.EXACT for item in receipt.file_resources)
    assert tuple(receipt.resource_ids) == plan.all_paths
    assert all(path.startswith(parent + "/") for path in receipt.resource_ids)
    restored = GeneratedMediaOutputPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    with runtime_media_output_approval_scope(restored):
        assert set(plan.all_paths) <= set(runtime_media_output_policy_resources())

    result = await _compose(request, replace(receipt, media_output_plan=restored), name, task_id=task_id)

    assert result["status"] == "completed", result
    assert result["document_id"] == "video-document"
    assert result["fs_path"] == plan.targets[0].rel_path
    assert (media_storage / result["fs_path"]).read_bytes() == b"synthetic rendered mp4"
    assert result["editor_recipe_document_id"] == "recipe-document"
    assert result["editor_recipe_path"] == plan.companion_paths[0]
    recipe = json.loads((media_storage / result["editor_recipe_path"]).read_text())
    assert recipe["kind"] == "manor.video_edit_recipe"
    assert "video-document" in json.dumps(recipe)
    assert len(composition) == 1
    assert (media_storage / HOME / "clean.mp4").read_bytes() == b"unchanged clean source"
    for path in (f"{parent}/other.json", f"{parent}/final-output-2.mp4", "Workspaces/_by_id/foreign/result.mp4"):
        assert not receipt.authorizes_workspace_file_commit(entity_id=ENTITY, path=path, target_exists=False)
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=plan.companion_paths[0], target_exists=True,
    )


@pytest.mark.asyncio
async def test_companion_only_collision_changes_whole_approved_pair(media_storage, composition):
    incumbent = media_storage / HOME / "final.video-edit.json"
    incumbent.write_text("existing recipe")
    request, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    assert receipt.resource_ids == (f"{HOME}/final-2.mp4", f"{HOME}/final-2.video-edit.json")
    result = await _compose(request, receipt, "final.mp4")
    assert result["status"] == "completed", result
    assert incumbent.read_text() == "existing recipe"
    assert not (media_storage / HOME / "final.mp4").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_late_pair_collision_is_rejected_before_render(media_storage, composition, index):
    request, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    incumbent = media_storage / receipt.resource_ids[index]
    incumbent.write_bytes(b"concurrent owner")
    result = await _compose(request, receipt, "final.mp4")
    assert result["status"] == "error"
    assert "appeared" in result["error"]
    assert composition == []
    assert incumbent.read_bytes() == b"concurrent owner"
    assert not (media_storage / receipt.resource_ids[1 - index]).exists()


@pytest.mark.asyncio
async def test_late_companion_collision_expires_persisted_approval(media_storage, composition, approval_store):
    request, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    row = await _granted_media_approval(approval_store, request, receipt)
    incumbent = media_storage / receipt.media_output_plan.companion_paths[0]
    incumbent.write_text("new owner")
    resumed_request, resumed_receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    result = json.loads(await _consume_media_approval(approval_store, row, resumed_request, resumed_receipt))
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
    assert resumed_receipt.resource_ids != receipt.resource_ids
    assert composition == []
    assert incumbent.read_text() == "new owner"
    assert not (media_storage / receipt.resource_ids[0]).exists()
    assert all(not (media_storage / path).exists() for path in resumed_receipt.resource_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["path", "foreign", "duplicate", "version", "kind"])
async def test_forged_companion_snapshot_is_rejected(media_storage, change):
    _, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    snapshot = receipt.media_output_plan.to_dict()
    if change == "path":
        snapshot["companion_paths"] = [f"{HOME}/unrelated.json"]
    elif change == "foreign":
        snapshot["companion_paths"] = ["Workspaces/_by_id/foreign/final.video-edit.json"]
    elif change == "duplicate":
        snapshot["companion_paths"] *= 2
    elif change == "version":
        snapshot["version"] = 1
    else:
        snapshot["kind"] = "image"
    with pytest.raises(ValueError):
        GeneratedMediaOutputPlan.from_dict(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["name", "scope", "entity", "missing_plan", "missing_companion"])
async def test_compose_target_cannot_drift_from_authorized_scope(media_storage, change):
    request, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    if change == "missing_plan":
        receipt = replace(receipt, media_output_plan=None)
    elif change == "missing_companion":
        receipt = replace(receipt, media_output_plan=replace(receipt.media_output_plan, companion_paths=()))
    with _authorized(request, receipt), pytest.raises(RuntimeToolAuthorizationWriteError):
        runtime_generated_media_target(
            kind="video", entity_id="foreign" if change == "entity" else ENTITY,
            workspace_base_dir="Workspaces/_by_id/foreign" if change == "scope" else HOME,
            ext=".mp4", prompt="", output_name="other.mp4" if change == "name" else "final.mp4",
            entity_root=str(media_storage),
        )


@pytest.mark.asyncio
async def test_compose_canonical_foreign_destination_is_not_silently_relocated(media_storage):
    with pytest.raises(ValueError, match="another Workspace"):
        await _approve(
            "Workspaces/_by_id/foreign/final.mp4",
            tool_name="compose_video_timeline", task_id=None,
        )


@pytest.mark.asyncio
async def test_compose_output_symlink_cannot_escape_exact_scope(media_storage, composition):
    foreign = media_storage / "Workspaces/_by_id/foreign"
    foreign.mkdir()
    (media_storage / HOME / "Videos").symlink_to(foreign, target_is_directory=True)
    request, receipt = await _approve("Videos/final.mp4", tool_name="compose_video_timeline", task_id=None)
    result = await _compose(request, receipt, "Videos/final.mp4")
    assert result["status"] == "error"
    assert "filesystem aliases" in result["error"]
    assert composition == []
    assert list(foreign.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sidecar_race", "recipe_projection", "video_projection", "attach"])
async def test_post_render_failure_reports_saved_outputs_without_replay(
    media_storage, composition, monkeypatch, failure,
):
    request, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    recipe_path = media_storage / receipt.media_output_plan.companion_paths[0]
    reason = "injected recipe projection failure: " + "stderr " * 10000
    if failure == "sidecar_race":
        async def register_video(**_kwargs):
            recipe_path.write_bytes(b"concurrent recipe owner")
            return "video-document"
        monkeypatch.setattr(media_tools, "_register_merged_video", register_video)
    else:
        target = {
            "recipe_projection": "_register_video_editor_recipe",
            "video_projection": "_register_merged_video",
            "attach": "_attach_video_editor_recipe_to_video",
        }[failure]
        monkeypatch.setattr(media_tools, target, AsyncMock(side_effect=RuntimeError(reason)))

    result = await _compose(request, receipt, "final.mp4")

    assert result["status"] == "error"
    assert result["error"] == "compose_incomplete"
    assert result["details"]["status"] == "partial"
    assert runtime_tool_call_error(result) is not None
    assert len(composition) == 1
    assert (media_storage / receipt.resource_ids[0]).read_bytes() == b"synthetic rendered mp4"
    projected = json.loads(model_tool_error_result("compose_video_timeline", result, max_chars=4000))
    details = projected["details"]
    assert details["status"] == "partial"
    assert details["action_executed"] is True
    assert details["retryable"] is False
    assert details["do_not_replay"] is True
    outputs = details["execution"]["saved_outputs"]
    assert outputs[0]["fs_path"] == receipt.resource_ids[0]
    assert outputs[0]["document_id"] == (None if failure == "video_projection" else "video-document")
    if failure in {"recipe_projection", "attach"}:
        assert outputs[1]["fs_path"] == receipt.resource_ids[1]
        assert outputs[1]["document_id"] == (None if failure == "recipe_projection" else "recipe-document")
        assert json.loads(recipe_path.read_text())["kind"] == "manor.video_edit_recipe"
        assert "injected recipe projection failure" in details["cause"]["message"]
    else:
        assert len(outputs) == 1
    if failure == "sidecar_race":
        assert "appeared" in details["cause"]["message"]
        assert recipe_path.read_bytes() == b"concurrent recipe owner"
    if failure == "video_projection":
        assert not recipe_path.exists()


@pytest.mark.asyncio
async def test_a_composition_refused_before_render_is_settled_as_no_effect(media_storage, composition):
    """Missing required audio is found while reading the timeline: nothing is
    rendered or written, and the handler proves it (not an uncertain effect)."""
    from packages.core.ai.runtime.receipts import runtime_tool_effect_state

    request, receipt = await _approve("final.mp4", tool_name="compose_video_timeline", task_id=None)
    arguments = {"timeline_path": f"{HOME}/timeline.json", "output_name": "final.mp4", "require_audio": True}
    with _authorized(request, receipt):
        result = await media_tools._compose_video_timeline_handler(
            entity_id=ENTITY, user_id="user_media_qa", workspace_id=WORKSPACE, **arguments,
        )
    assert json.loads(result)["code"] == "audio_track_missing"
    assert composition == [] and not (media_storage / receipt.resource_ids[0]).exists()
    assert runtime_tool_effect_state("compose_video_timeline", arguments, result).value == "none"
    # Identical JSON from anywhere else proves nothing.
    assert runtime_tool_effect_state("compose_video_timeline", arguments, str(result)).value == "unconfirmed"
