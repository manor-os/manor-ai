"""Generated image approval and persistence must agree on one physical target.

Only database/usage boundaries are replaced. Classification, receipt matching,
filename resolution, the image saver and the atomic filesystem writer are real.
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core import database
from packages.core.ai.runtime.approval_classifier import (
    classify_runtime_tool,
    resolve_runtime_workspace_file_scope,
)
from packages.core.ai.runtime.approvals import (
    RuntimeApprovalDecision,
    RuntimeApprovalRequest,
)
from packages.core.ai.runtime.authorization_receipts import (
    RuntimeToolAuthorizationReceiptFactory,
    RuntimeToolAuthorizationWriteError,
    runtime_begin_tool_authorization_scope,
    runtime_end_tool_authorization_scope,
)
from packages.core.ai.tools import extended_tools
from packages.core.config import get_settings
from packages.core.models.document import DocumentFolder
from packages.core.models.workspace import Workspace
from packages.core.services.generated_file_refs import entity_fs_open_url


pytestmark = pytest.mark.oss_smoke


ENTITY = "ent_media_qa"
WORKSPACE = "ws_media_qa"
TASK = "taskmediaqa"
FOLDER = "foldermediaqa"
BASE = f"Workspaces/_by_id/{FOLDER}/tasks/{TASK}"
PROMPT = "A synthetic product illustration for a regression test"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Wl2nWoAAAAASUVORK5CYII="
)


@dataclass
class _Result:
    value: object

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        assert self.value is not None
        return self.value


@pytest.fixture
def media_storage(monkeypatch, tmp_path):
    """Expose one already-provisioned Workspace via the SQL session boundary."""
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    workspace = Workspace(
        id=WORKSPACE, entity_id=ENTITY, name="Media QA", artifact_folder_id=FOLDER,
    )
    root_folder = DocumentFolder(
        id="rootmediaqa", entity_id=ENTITY, name="Workspaces", parent_id=None,
    )
    folder = DocumentFolder(
        id=FOLDER, entity_id=ENTITY, name=workspace.name, parent_id=root_folder.id,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement):
            selected = statement.column_descriptions[0]
            params = statement.compile().params
            if selected["entity"] is Workspace:
                assert params.get("entity_id_1") == ENTITY
                assert params.get("id_1") == WORKSPACE
                return _Result(
                    FOLDER if selected["name"] == "artifact_folder_id" else workspace
                )
            if selected["entity"] is DocumentFolder:
                if selected["name"] == "id":
                    return _Result(None)  # No sibling-name collision.
                return _Result(
                    root_folder
                    if params.get("id_1") == root_folder.id
                    or params.get("name_1") == "Workspaces"
                    else folder
                )
            raise AssertionError(f"Unexpected database query: {statement}")

        async def commit(self):
            return None

        async def flush(self):
            return None

    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(extended_tools, "_bill_media", AsyncMock())
    return tmp_path / ENTITY


async def _approve(
    output_name, *, tool_name="generate_file", params=None, task_id=TASK,
    entity_id=ENTITY, workspace_id=WORKSPACE, user_id="user_media_qa",
):
    arguments = (
        {"kind": "image", "name": output_name, "params": {"prompt": PROMPT, **(params or {})}}
        if tool_name == "generate_file"
        else {"output_name": output_name, "prompt": PROMPT, **(params or {})}
    )
    scope = await resolve_runtime_workspace_file_scope(
        tool_name=tool_name, arguments=arguments,
        entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
    )
    classification = classify_runtime_tool(
        tool_name, arguments,
        entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
        workspace_file_scope=scope,
    )
    request = RuntimeApprovalRequest(
        tool_name=tool_name, arguments=arguments,
        entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        task_id=task_id, workspace_file_scope=scope,
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification), request=request,
    )
    assert receipt is not None
    return request, receipt


@contextmanager
def _authorized(request, receipt):
    scope = runtime_begin_tool_authorization_scope(receipt, arguments=request.arguments)
    assert scope.valid
    try:
        yield
    finally:
        runtime_end_tool_authorization_scope(scope)


async def _save(output_name, **overrides):
    return await extended_tools._save_generated_image_bytes(**{
        "entity_id": ENTITY, "user_id": "user_media_qa", "prompt": PROMPT,
        "model": "gpt-image-1", "size": "1024x1024", "image_bytes": PNG,
        "mime": "image/png", "is_byok": True, "output_name": output_name,
        "workspace_id": WORKSPACE, "task_id": TASK, "save_to_knowledge": False,
        **overrides,
    })


@pytest.mark.asyncio
async def test_workflow_approval_and_save_share_lineage_archive(media_storage):
    from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope

    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "retryrun", "firstrun")):
        request, receipt = await _approve("cover")
        assert receipt.workspace_artifact_base_dir == f"Workspaces/_by_id/{FOLDER}/workflow/firstrun"
        with _authorized(request, receipt):
            url = await _save("cover")
    assert url == entity_fs_open_url(ENTITY, f"Workspaces/_by_id/{FOLDER}/workflow/firstrun/images/cover.png")


RUN = "01M5EY2EGFM4DPCMJECTPJJP0J"


@pytest.mark.asyncio
@pytest.mark.parametrize("injected,user_id,archive", [
    # A user-run Workflow step: no conversation, Workflow binding key.
    ({"_workflow_run_id_from_context": RUN, "_workflow_lineage_root_run_id_from_context": RUN},
     "user_media_qa", f"workflow/{RUN}"),
    # A Task worker: no conversation, Task binding key.
    ({"task_id": TASK}, "user_media_qa", f"tasks/{TASK}"),
    # A user-less Task with a conversation: Task binding key, real conversation.
    ({"task_id": TASK, "conversation_id": "conv_media_qa"}, None, f"tasks/{TASK}"),
])
async def test_sandbox_save_result_receipt_matches_its_inner_write(media_storage, injected, user_id, archive):
    """The sandbox binding is the approval scope, never the receipt's conversation.

    The handler checks its inner save against the run's real conversation, so
    the receipt must record that conversation whatever binding the run uses.
    """
    from packages.core.ai.runtime.tool_context import (
        runtime_authorization_sandbox_binding_key,
        runtime_tool_call_context_from_kwargs,
    )
    from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope

    context = runtime_tool_call_context_from_kwargs(
        injected, entity_id=ENTITY, user_id=user_id, workspace_id=WORKSPACE,
    )
    arguments = {"action": "save_result", "params": {
        "sandbox_id": "sb", "file_path": "/skill/work/cues.json", "filename": "EP.05-n1-cues.json",
    }}
    with workspace_artifact_scope(WorkspaceArtifactScope(
        ENTITY, WORKSPACE, context.workflow_run_id, context.workflow_lineage_root_run_id,
    )):
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="sandbox", arguments=arguments, entity_id=ENTITY, workspace_id=WORKSPACE,
            task_id=context.task_id,
        )
        request = RuntimeApprovalRequest(
            tool_name="sandbox", arguments=arguments, entity_id=ENTITY, user_id=context.user_id or "",
            workspace_id=WORKSPACE, conversation_id=context.conversation_id, task_id=context.task_id,
            sandbox_binding_key=runtime_authorization_sandbox_binding_key(context, "sandbox"),
            workspace_file_scope=scope,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            decision=RuntimeApprovalDecision.allow(classify_runtime_tool(
                "sandbox", arguments, entity_id=ENTITY, workspace_id=WORKSPACE, task_id=context.task_id,
                workspace_file_scope=scope,
            )),
            request=request,
        )

    assert request.approval_scope_id and request.approval_scope_id != context.conversation_id
    assert receipt.authorizes_workspace_file_mutation(
        capability_id="file.write", action_key="workspace.file.create",
        paths=[f"Workspaces/_by_id/{FOLDER}/{archive}/artifacts/EP.05-n1-cues.json"],
        entity_id=ENTITY, user_id=context.user_id, workspace_id=WORKSPACE,
        conversation_id=context.conversation_id, task_id=context.task_id,
    )


@pytest.mark.asyncio
async def test_frame_sample_directory_receipt_matches_scoped_dynamic_outputs(media_storage):
    """A multi-file writer must authorize every derived child in its run archive."""
    from packages.core.services.generated_media_naming import (
        WorkspaceArtifactScope,
        workspace_artifact_scope,
    )

    arguments = {"input_path": "source.mp4", "output_dir": "qa/frames"}
    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "retryrun", "firstrun")):
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="render_frame_samples",
            arguments=arguments,
            entity_id=ENTITY,
            workspace_id=WORKSPACE,
            task_id=TASK,
        )
        classification = classify_runtime_tool(
            "render_frame_samples",
            arguments,
            entity_id=ENTITY,
            workspace_id=WORKSPACE,
            task_id=TASK,
            workspace_file_scope=scope,
        )
        request = RuntimeApprovalRequest(
            tool_name="render_frame_samples",
            arguments=arguments,
            entity_id=ENTITY,
            user_id="user_media_qa",
            workspace_id=WORKSPACE,
            task_id=TASK,
            workspace_file_scope=scope,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            decision=RuntimeApprovalDecision.allow(classification),
            request=request,
        )

    assert receipt is not None
    output_dir = f"Workspaces/_by_id/{FOLDER}/workflow/firstrun/qa/frames"
    assert receipt.resource_ids == (output_dir,)
    for filename in ("frame-001-000000000ms.png", "frame-002-000005000ms.png"):
        assert receipt.authorizes_workspace_file_commit(
            entity_id=ENTITY,
            path=f"{output_dir}/{filename}",
            target_exists=False,
        )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY,
        path=f"Workspaces/_by_id/{FOLDER}/workflow/firstrun/qa/other/frame.png",
        target_exists=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "technical/narration-timeline.json",
    # Agents copy the run-relative path from earlier receipts.
    "workflow/firstrun/technical/narration-timeline.json",
])
async def test_nested_media_output_receipt_matches_the_written_target(media_storage, name):
    from packages.core.ai.tools.media_tools import _build_media_target
    from packages.core.services.generated_media_naming import (
        WorkspaceArtifactScope,
        workspace_artifact_scope,
    )

    arguments = {"timeline_name": name, "cues_name": name.replace("narration-timeline", "subtitle-cues")}
    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "retryrun", "firstrun")):
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="build_narration_timeline", arguments=arguments,
            entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK,
        )
        classification = classify_runtime_tool(
            "build_narration_timeline", arguments,
            entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK, workspace_file_scope=scope,
        )
        request = RuntimeApprovalRequest(
            tool_name="build_narration_timeline", arguments=arguments, entity_id=ENTITY,
            user_id="user_media_qa", workspace_id=WORKSPACE, task_id=TASK, workspace_file_scope=scope,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            decision=RuntimeApprovalDecision.allow(classification), request=request,
        )
        targets = [
            await _build_media_target(
                entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK, output_name=output_name,
                ext=".json", fallback=fallback, default_dir="artifacts",
            )
            for output_name, fallback in (
                ("", "narration-manifest"),
                (arguments["timeline_name"], "narration-timeline"),
                (arguments["cues_name"], "subtitle-cues"),
            )
        ]

    assert receipt is not None
    assert targets[1].rel_path == f"Workspaces/_by_id/{FOLDER}/workflow/firstrun/technical/narration-timeline.json"
    for target in targets:
        assert receipt.authorizes_workspace_file_commit(
            entity_id=ENTITY, path=target.rel_path, target_exists=False,
        ), target.rel_path


@pytest.mark.asyncio
async def test_run_relative_file_name_is_authorized_where_it_is_written(media_storage):
    """Any writer: an approved workflow/<run>/... name matches its run-folder target."""
    from packages.core.services.generated_media_naming import (
        WorkspaceArtifactScope,
        workspace_artifact_scope,
    )

    arguments = {
        "kind": "document", "name": "workflow/firstrun/technical/narration-script.txt",
        "params": {"content": "Narration"},
    }
    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "retryrun", "firstrun")):
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="generate_file", arguments=arguments,
            entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK,
        )
        classification = classify_runtime_tool(
            "generate_file", arguments,
            entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK, workspace_file_scope=scope,
        )
        request = RuntimeApprovalRequest(
            tool_name="generate_file", arguments=arguments, entity_id=ENTITY,
            user_id="user_media_qa", workspace_id=WORKSPACE, task_id=TASK, workspace_file_scope=scope,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            decision=RuntimeApprovalDecision.allow(classification), request=request,
        )

    run_folder = f"Workspaces/_by_id/{FOLDER}/workflow/firstrun"
    assert receipt is not None
    assert receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=f"{run_folder}/technical/narration-script.txt", target_exists=False,
    )
    # Still exactly that file, inside this run only.
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=f"{run_folder}/technical/other.txt", target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=f"Workspaces/_by_id/{FOLDER}/workflow/otherrun/technical/narration-script.txt",
        target_exists=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,leaf", [
    ("write_file", {"path": "report.md", "content": "Report"}, "report.md"),
    ("generate_document_file", {"name": "report", "file_type": "md", "content": "Report"}, "report.md"),
    ("generate_file", {"kind": "document", "name": "report", "params": {"content": "Report"}}, "report.md"),
    ("generate_file", {"kind": "document", "params": {"content": "Report"}}, "generated-document.md"),
    ("sandbox_save_result", {"filename": "report.pdf"}, "report.pdf"),
])
async def test_workspace_root_file_approval_has_no_implicit_bucket(media_storage, tool, args, leaf):
    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory as Factory

    scope = await resolve_runtime_workspace_file_scope(
        tool_name=tool, arguments=args, entity_id=ENTITY, workspace_id=WORKSPACE, task_id=None,
    )
    request = RuntimeApprovalRequest(
        tool_name=tool, arguments=args, entity_id=ENTITY, workspace_id=WORKSPACE,
        workspace_file_scope=scope, user_id="user_media_qa",
    )
    resources = Factory.create(
        request=request, action_key="workspace.file.create",
        resource_id=args.get("name") or args.get("path"),
    )
    assert [item.resource_id for item in resources] == [leaf]
    assert all(item.match_kind.value != "tree" for item in resources)


@pytest.mark.asyncio
async def test_root_narration_freezes_exact_audio_target(media_storage):
    from packages.core.ai.runtime.media_outputs import runtime_generated_media_target

    request, receipt = await _approve(
        "Voice_Intro", tool_name="generate_audio", params={"purpose": "narration"}, task_id=None,
    )
    with _authorized(request, receipt):
        target = runtime_generated_media_target(
            kind="audio", entity_id=ENTITY, workspace_base_dir=f"Workspaces/_by_id/{FOLDER}",
            ext=".wav", prompt=PROMPT, output_name="Voice_Intro", entity_root=str(media_storage),
        )
    assert target.rel_path == f"Workspaces/_by_id/{FOLDER}/voice-intro.wav"
    assert receipt.media_output_plan.target_for_extension(".wav") == target


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "workflow/firstrun/b01-hook.wav",
    f"Workspaces/_by_id/{FOLDER}/workflow/firstrun/b01-hook.wav",
])
async def test_narration_named_at_the_run_folder_root_saves(media_storage, name):
    """Narration authorizes its requested parent; that parent may be the run folder itself.

    Local 2026-10-28 (Stickman run 01M60VW74HZZ1Z3M4FAYQFYK0Q): narration named
    workflow/<run>/b01-hook.wav got a receipt for the workflow/<run> tree, which
    the matcher collapsed to "the artifact base" and rejected, so every TTS save
    failed with "Workspace file target or existence changed after approval".
    """
    import io
    import wave

    from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope

    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00\x80\x01" * 80)
    arguments = {"kind": "audio", "name": name, "prompt": PROMPT, "purpose": "narration"}
    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "firstrun", "firstrun")):
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="generate_file", arguments=arguments,
            entity_id=ENTITY, workspace_id=WORKSPACE, task_id=None,
        )
        request = RuntimeApprovalRequest(
            tool_name="generate_file", arguments=arguments, entity_id=ENTITY,
            user_id="user_media_qa", workspace_id=WORKSPACE, workspace_file_scope=scope,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            decision=RuntimeApprovalDecision.allow(classify_runtime_tool(
                "generate_file", arguments, entity_id=ENTITY, workspace_id=WORKSPACE,
                workspace_file_scope=scope,
            )),
            request=request,
        )
        with _authorized(request, receipt):
            url = await extended_tools._save_generated_audio_bytes(
                entity_id=ENTITY, user_id="user_media_qa", prompt=PROMPT, model="openai/tts-1-hd",
                purpose="narration", audio_bytes=stream.getvalue(), audio_format="wav", is_byok=True,
                output_name=name, workspace_id=WORKSPACE, bill_media=False,
            )

    run_folder = f"Workspaces/_by_id/{FOLDER}/workflow/firstrun"
    assert url == entity_fs_open_url(ENTITY, f"{run_folder}/b01-hook.wav")
    # The tree is still this run's folder only.
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=f"Workspaces/_by_id/{FOLDER}/workflow/otherrun/b01-hook.wav", target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=f"Workspaces/_by_id/{FOLDER}/b01-hook.wav", target_exists=False,
    )


@pytest.mark.asyncio
async def test_root_audio_derivative_approval_is_exact(media_storage):
    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory as Factory

    arguments = {"output_name": "Voice_Normalized", "output_format": "wav"}
    scope = await resolve_runtime_workspace_file_scope(
        tool_name="normalize_audio_loudness", arguments=arguments,
        entity_id=ENTITY, workspace_id=WORKSPACE, task_id=None,
    )
    request = RuntimeApprovalRequest(
        tool_name="normalize_audio_loudness", arguments=arguments,
        entity_id=ENTITY, user_id="user_media_qa", workspace_id=WORKSPACE, workspace_file_scope=scope,
    )
    resources = Factory.create(request=request, action_key="workspace.file.create", resource_id=None)
    assert [(item.resource_id, item.match_kind.value) for item in resources] == [
        (f"Workspaces/_by_id/{FOLDER}/voice-normalized.wav", "exact"),
    ]


@pytest.mark.asyncio
async def test_materialized_legacy_workflow_keeps_approval_and_save_location(media_storage):
    from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope

    legacy = f"Workspaces/_by_id/{FOLDER}/runs/firstrun"
    (media_storage / legacy).mkdir(parents=True)
    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "retryrun", "firstrun")):
        request, receipt = await _approve("cover")
        assert receipt.workspace_artifact_base_dir == legacy
        with _authorized(request, receipt):
            url = await _save("cover")
    assert url == entity_fs_open_url(ENTITY, f"{legacy}/images/cover.png")


@pytest.mark.asyncio
async def test_explicit_workspace_destination_from_task_keeps_exact_plan(media_storage):
    from packages.core.services.generated_media_naming import GeneratedMediaOutputPlan
    path = f"Workspaces/_by_id/{FOLDER}/brand/logo.png"
    request, receipt = await _approve(path)
    assert receipt.workspace_artifact_base_dir == f"Workspaces/_by_id/{FOLDER}"
    assert GeneratedMediaOutputPlan.from_dict(receipt.media_output_plan.to_dict()).to_dict() == receipt.media_output_plan.to_dict()
    with _authorized(request, receipt):
        assert await _save(path) == entity_fs_open_url(ENTITY, f"{path}")


@pytest.mark.asyncio
async def test_a_run_writing_into_a_project_folder_is_authorized_for_every_declared_output(media_storage):
    """A series episode's narration is normalized into its project folder, outside the run's archive.

    Local 2026-10-25: prepare_narration_timeline names its outputs
    normalized_output_directory/manifest_name/..., so the explicit Workspace
    destination was missed, the receipt stayed bound to the run archive, and
    every normalized segment failed at commit.
    """
    from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope

    episode = "projects/daily-calm/e03-kettle"
    arguments = {
        "segments": [{"input_path": f"{episode}/audio/segment-001.wav"}],
        "normalized_output_directory": f"{episode}/audio/normalized",
        "manifest_name": f"{episode}/technical/narration-manifest.json",
        "timeline_name": f"{episode}/technical/narration-timeline.json",
        "cues_name": f"{episode}/technical/subtitle-cues.json",
    }
    with workspace_artifact_scope(WorkspaceArtifactScope(ENTITY, WORKSPACE, "run1", "run1")):
        scope = await resolve_runtime_workspace_file_scope(
            tool_name="prepare_narration_timeline", arguments=arguments,
            entity_id=ENTITY, workspace_id=WORKSPACE, task_id=None,
        )
        request = RuntimeApprovalRequest(
            tool_name="prepare_narration_timeline", arguments=arguments, entity_id=ENTITY,
            user_id="user_media_qa", workspace_id=WORKSPACE, workspace_file_scope=scope,
        )
        classification = classify_runtime_tool(
            "prepare_narration_timeline", arguments, entity_id=ENTITY, workspace_id=WORKSPACE,
            workspace_file_scope=scope,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            request=request, decision=RuntimeApprovalDecision.allow(classification),
        )

    home = f"Workspaces/_by_id/{FOLDER}"
    assert receipt.workspace_artifact_base_dir == home
    for path in (f"{home}/{episode}/audio/normalized/puck/segment-001.wav",
                 f"{home}/{episode}/technical/narration-timeline.json"):
        assert receipt.authorizes_workspace_file_commit(entity_id=ENTITY, path=path, target_exists=False), path


@pytest.mark.asyncio
async def test_bash_approval_execution_and_projection_share_task_destination(media_storage, monkeypatch):
    from packages.core.ai.tools import bash_tool
    from packages.core.services import ai_file_permissions, runtime_authorization

    media_storage.mkdir(parents=True)
    args = {"command": "echo report > report.txt"}
    file_scope = await resolve_runtime_workspace_file_scope(
        tool_name="bash", arguments=args, entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK,
    )
    request = RuntimeApprovalRequest(
        tool_name="bash", arguments=args, entity_id=ENTITY, user_id="user_media_qa",
        workspace_id=WORKSPACE, task_id=TASK, workspace_file_scope=file_scope,
    )
    classification = classify_runtime_tool(
        "bash", args, entity_id=ENTITY, workspace_id=WORKSPACE, task_id=TASK,
        workspace_file_scope=file_scope,
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        request=request, decision=RuntimeApprovalDecision.allow(classification),
    )
    target = f"{BASE}/artifacts/report.txt"
    assert receipt.resource_ids == (target,)
    assert receipt.action_key == "workspace.file.create"
    monkeypatch.setattr(bash_tool, "_bash_resource_mutation_error", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(ai_file_permissions, "_resource_file_acl_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(bash_tool, "_unreadable", AsyncMock(return_value=set()))

    @asynccontextmanager
    async def unlocked(_entity_id, _root):
        yield

    monkeypatch.setattr(bash_tool, "_entity_mutation_section", unlocked)
    repair = AsyncMock(return_value=1)
    monkeypatch.setattr(bash_tool, "_repair_projection_paths", repair)
    from tests.bash_sandbox_fakes import install

    sandbox = install(monkeypatch, media_storage.parent)
    with _authorized(request, receipt):
        result = json.loads(await bash_tool._bash(
            ENTITY, user_id="user_media_qa", workspace_id=WORKSPACE, task_id=TASK, **args,
        ))
    assert result.get("exit_code") == 0, result
    assert "artifacts/report.txt" in sandbox.user_commands()[-1]["command"]
    assert (media_storage / target).read_text() == "report\n"
    assert not (media_storage / "report.txt").exists()
    assert target in repair.await_args.kwargs["paths"]


@pytest.mark.asyncio
async def test_unscoped_media_approval_freezes_a_root_file(media_storage):
    request, receipt = await _approve("cover", workspace_id=None, task_id=None)
    assert receipt.media_output_plan is not None
    assert receipt.media_output_plan.target_for_extension(".png").rel_path == "cover.png"
    with _authorized(request, receipt):
        url = await _save("cover", workspace_id=None, task_id=None)
    assert url == entity_fs_open_url(ENTITY, f"cover.png")


@pytest.mark.asyncio
@pytest.mark.parametrize("requested, expected", [
    ("初夏餐饮海报", "images/初夏餐饮海报.png"),
    ("Launch.HERO.PNG", "images/launch-hero.png"),
    ("hero_v2", "images/hero-v2.png"),
    ("frames/EP01_首帧", "frames/ep01-首帧.png"),
    ("素材:/.草稿/Hero_A", "素材-/草稿/hero-a.png"),
])
async def test_generated_image_approval_matches_normalized_saved_target(
    media_storage, requested, expected,
):
    request, receipt = await _approve(requested)
    with _authorized(request, receipt):
        url = await _save(requested)

    # Saved addresses are percent-encoded once (file_refs contract).
    assert url == entity_fs_open_url(ENTITY, f"{BASE}/{expected}")
    assert (media_storage / BASE / expected).read_bytes() == PNG
    assert receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=f"{BASE}/{expected}", target_exists=False,
    )
    assert receipt.action_key == "workspace.file.create"


@pytest.mark.asyncio
async def test_generated_image_collision_selects_new_approved_file(media_storage):
    existing = media_storage / BASE / "images/hero.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing artifact")
    request, receipt = await _approve("hero.png")
    with _authorized(request, receipt):
        url = await _save("hero.png")

    assert receipt.action_key == "workspace.file.create"
    assert url == entity_fs_open_url(ENTITY, f"{BASE}/images/hero-2.png")
    assert existing.read_bytes() == b"existing artifact"
    assert existing.with_name("hero-2.png").read_bytes() == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["generate_file", "generate_image"])
async def test_generated_image_receipt_rejects_different_output_name(
    media_storage, tool_name,
):
    request, receipt = await _approve("hero.png", tool_name=tool_name)
    with _authorized(request, receipt), pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save("unapproved.png")
    assert not list(media_storage.rglob("*.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["generate_file", "generate_image"])
async def test_generated_image_late_collision_cannot_overwrite_or_retarget(
    media_storage, tool_name,
):
    request, receipt = await _approve("hero.png", tool_name=tool_name)
    # Another writer publishes between authorization and provider completion.
    target = media_storage / BASE / "images/hero.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"concurrent artifact")
    with _authorized(request, receipt), pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save("hero.png")
    assert target.read_bytes() == b"concurrent artifact"
    assert not target.with_name("hero-2.png").exists()


@pytest.mark.asyncio
async def test_generated_image_receipt_cannot_write_to_another_task(media_storage):
    request, receipt = await _approve("hero.png")
    with _authorized(request, receipt), pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save("hero.png", task_id="othertask")
    assert not list(media_storage.rglob("*.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["name", "output_name", "filename"])
async def test_generated_image_nested_output_argument_uses_same_target(
    media_storage, parameter,
):
    request, receipt = await _approve("", params={parameter: "Hero_Output"})
    with _authorized(request, receipt):
        url = await _save("Hero_Output")
    expected = f"{BASE}/images/hero-output.png"
    assert url == entity_fs_open_url(ENTITY, f"{expected}")
    assert (media_storage / expected).read_bytes() == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["generate_file", "generate_image"])
async def test_two_approved_image_calls_cannot_retarget_after_first_commit(
    media_storage, tool_name,
):
    first_request, first_receipt = await _approve("hero.png", tool_name=tool_name)
    second_request, second_receipt = await _approve("hero.png", tool_name=tool_name)
    with _authorized(first_request, first_receipt):
        await _save("hero.png")
    with _authorized(second_request, second_receipt), pytest.raises(
        RuntimeToolAuthorizationWriteError,
    ):
        await _save("hero.png")
    assert (media_storage / BASE / "images/hero.png").read_bytes() == PNG
    assert not (media_storage / BASE / "images/hero-2.png").exists()


@pytest.mark.asyncio
async def test_task_image_receipt_cannot_switch_to_workspace_shared_storage(media_storage):
    request, receipt = await _approve("hero.png")
    with _authorized(request, receipt), pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save("hero.png", workspace_shared=True)
    assert not list(media_storage.rglob("*.png"))


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        return self.scalar_one_or_none()

    def first(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return self.rows


@pytest.fixture
def approval_store(monkeypatch):
    """SQL/cache boundary adapter; approval rendering and lifecycle remain real."""
    from packages.core.governance.policy_cache import GovernancePolicyCacheFactory
    from packages.core.models.base import generate_ulid
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.user import Entity
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation

    cache = AsyncMock()
    cache.get_current.return_value = None
    monkeypatch.setattr(GovernancePolicyCacheFactory, "_default", cache)

    class Store:
        def __init__(self):
            self.requests = {}
            self.events = []

        async def get(self, model, identity):
            assert model is Workspace and identity == WORKSPACE
            return SimpleNamespace(id=WORKSPACE, entity_id=ENTITY, name="Media QA")

        async def execute(self, statement):
            params = statement.compile().params
            from sqlalchemy.sql.dml import Update

            if isinstance(statement, Update):
                assert statement.table.name == HitlRequest.__tablename__
                row = self.requests.get(params["id_1"])
                if row is None or row.status != params["status_1"]:
                    return _Rows([])
                row.status = params["status"]
                row.consumed_at = params["consumed_at"]
                return _Rows([row])
            selected = statement.column_descriptions[0]
            if selected["entity"] is HitlRequest:
                rows = list(self.requests.values())
                for key in ("id", "entity_id", "dedup_key", "status", "action_key"):
                    param = params.get(f"{key}_1")
                    if param is not None:
                        values = param if isinstance(param, list) else [param]
                        rows = [row for row in rows if getattr(row, key) in values]
                return _Rows(rows)
            if selected["entity"] is Conversation:
                return _Rows([Conversation(
                    id="convmediaqa", entity_id=ENTITY, workspace_id=WORKSPACE,
                )])
            if selected["entity"] is Workspace:
                if selected["name"] == "entity_id":
                    return _Rows([ENTITY])
                return _Rows([SimpleNamespace(id=WORKSPACE, entity_id=ENTITY, name="Media QA")])
            if selected["entity"] is Entity:
                assert selected["name"] == "settings"
                return _Rows([{"approval_defaults": {"revision": 1, "policy": {"mode": "risk_based", "rules": []}}}])
            if selected["entity"] is GovernancePolicy:
                return _Rows([SimpleNamespace(
                    policy={"approval": {"root_entity_id": ENTITY, "policy": {"mode": None, "rules": [
                        {"id": "media-review", "effect": "ask", "selector": {"kind": "category", "category": "files"}},
                    ]}}}, revision=1,
                )])
            raise AssertionError(f"Unexpected approval query: {statement}")

        def add(self, row):
            if not row.id:
                row.id = generate_ulid()
            if isinstance(row, HitlRequest):
                # The stored snapshot must survive JSON persistence, without
                # relying on Python object identity or an in-memory plan.
                row.context = json.loads(json.dumps(row.context))
                self.requests[row.id] = row
            else:
                self.events.append(row)

        async def flush(self):
            return None

        @asynccontextmanager
        async def begin_nested(self):
            yield self

    return Store()


async def _granted_media_approval(store, request, receipt):
    from packages.core.ai.runtime.approval_service import _runtime_render_context
    from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_scope
    from packages.core.models.hitl_request import HitlRequest

    action = classify_runtime_tool(
        request.tool_name, request.arguments, entity_id=ENTITY,
        workspace_id=WORKSPACE, workspace_file_scope=request.workspace_file_scope,
    ).action
    with runtime_media_output_approval_scope(receipt.media_output_plan):
        context = await _runtime_render_context(
            store, conversation_id="convmediaqa", entity_id=ENTITY,
            user_id=request.user_id, workspace_id=WORKSPACE,
            tool_name=request.tool_name, arguments=request.arguments, action=action,
        )
    row = HitlRequest(
        entity_id=ENTITY, workspace_id=WORKSPACE, action_key=action.action_key,
        capability_id=action.capability_id, risk_level=action.risk_level,
        resource_kind=action.resource_kind, resource_id=action.resource_id,
        origin_kind="tool_call", origin_conversation_id="convmediaqa",
        status="granted", dedup_key="operation:media-approval-qa", context=context,
    )
    from packages.core.governance.approvals import _consent_requirements
    from packages.core.governance.service import check_step_policy
    from packages.core.ai.runtime.media_outputs import runtime_media_output_policy_resources

    with runtime_media_output_approval_scope(receipt.media_output_plan):
        decision = await check_step_policy(
            store, entity_id=ENTITY, workspace_id=WORKSPACE, user_id=request.user_id,
            kind=action.kind, action_key=action.action_key, capability_id=action.capability_id,
            resource_kind=action.resource_kind, resource_id=action.resource_id,
            resource_ids=runtime_media_output_policy_resources(),
            risk_level=action.risk_level, operation=action.operation,
        )
    assert decision.pause_for_hitl
    row.context = {**context, "operation_id": "media-approval-qa",
                   "approval_requirements": _consent_requirements(decision)}
    store.add(row)
    return row


async def _consume_media_approval(store, row, request, receipt, *, token=None):
    from packages.core.ai.runtime.approval_service import consume_runtime_approval
    from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_scope
    from packages.core.services.runtime_authorization import (
        AuthorizationRule, PermissionDecision,
    )

    action = classify_runtime_tool(
        request.tool_name, request.arguments, entity_id=ENTITY,
        workspace_id=WORKSPACE, workspace_file_scope=request.workspace_file_scope,
    ).action
    with runtime_media_output_approval_scope(receipt.media_output_plan):
        return await consume_runtime_approval(
            store, conversation_id="convmediaqa", entity_id=ENTITY,
            user_id=request.user_id, hitl_id=token or row.id,
            tool_name=request.tool_name, arguments=request.arguments, action=action,
            permission_decision=PermissionDecision.allow(AuthorizationRule.HITL_REQUEST_OWNER),
        )


@pytest.mark.asyncio
async def test_persisted_media_approval_snapshot_consumes_once_then_saves(
    media_storage, approval_store,
):
    request, receipt = await _approve("初夏餐饮海报", task_id=None)
    row = await _granted_media_approval(approval_store, request, receipt)
    assert row.context["media_output_plan"] == receipt.media_output_plan.to_dict()
    assert await _consume_media_approval(approval_store, row, request, receipt) is None
    assert row.status == "consumed"
    assert row.consumed_at is not None
    with _authorized(request, receipt):
        url = await _save("初夏餐饮海报", task_id=None)
    expected = f"Workspaces/_by_id/{FOLDER}/初夏餐饮海报.png"
    assert url == entity_fs_open_url(ENTITY, f"{expected}")
    assert (media_storage / expected).read_bytes() == PNG
    duplicate = await _consume_media_approval(approval_store, row, request, receipt)
    assert json.loads(duplicate)["status"] == "consumed"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_snapshot", [False, True])
async def test_changed_or_missing_media_snapshot_requires_new_intent_after_expiry(
    media_storage, approval_store, legacy_snapshot,
):
    request, receipt = await _approve("hero.png", task_id=None)
    row = await _granted_media_approval(approval_store, request, receipt)
    if legacy_snapshot:
        row.context = {key: value for key, value in row.context.items() if key != "media_output_plan"}
    else:
        target = media_storage / receipt.media_output_plan.targets[0].rel_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"new collision after approval")
    resumed_request, resumed_receipt = await _approve("hero.png", task_id=None)
    assert resumed_request.arguments == request.arguments
    result = await _consume_media_approval(approval_store, row, resumed_request, resumed_receipt)
    payload = json.loads(result)
    assert row.status == "expired"
    assert row.consumed_at is None
    assert payload["error"] == "blocked_by_governance"
    assert payload["matched_rule"] == (
        "approval.operation_terminal" if legacy_snapshot else "approval.operation_changed"
    )
    assert payload["status"] == "expired" and payload["action_executed"] is False
    assert payload["approval_token"] == row.id
    assert "__hitl__" not in payload and "hitl" not in payload
    assert list(approval_store.requests) == [row.id]
    replay = json.loads(await _consume_media_approval(
        approval_store, row, resumed_request, resumed_receipt,
    ))
    assert replay["error"] == "approval_not_granted" and replay["status"] == "expired"
    assert row.consumed_at is None
    assert list(approval_store.requests) == [row.id]

    # A server-owned new intent may request consent for the new physical plan;
    # reusing the terminal call above cannot create that intent implicitly.
    from packages.core.ai.runtime.approval_service import _runtime_block_payload
    from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_scope
    from packages.core.services.runtime_authorization import AuthorizationRule, PermissionDecision

    action = classify_runtime_tool(
        resumed_request.tool_name, resumed_request.arguments, entity_id=ENTITY,
        workspace_id=WORKSPACE, workspace_file_scope=resumed_request.workspace_file_scope,
    ).action
    with runtime_media_output_approval_scope(resumed_receipt.media_output_plan):
        fresh = json.loads(await _runtime_block_payload(
            approval_store, conversation_id="convmediaqa", entity_id=ENTITY,
            user_id=resumed_request.user_id, workspace_id=WORKSPACE,
            tool_name=resumed_request.tool_name, arguments=resumed_request.arguments, action=action,
            permission_decision=PermissionDecision.allow(AuthorizationRule.HITL_REQUEST_OWNER),
            reason="Review the new image request.", matched_rule_hint=None,
            operation_id="media-approval-qa:new-intent",
        ))
    assert fresh["__hitl__"] is True and fresh["approval_token"] != row.id
    replacement = approval_store.requests[fresh["approval_token"]]
    assert replacement.status == "pending"
    assert replacement.context["operation_id"] == "media-approval-qa:new-intent"
    assert replacement.dedup_key == "operation:media-approval-qa:new-intent"
    assert replacement.context["media_output_plan"] == resumed_receipt.media_output_plan.to_dict()
    assert len(approval_store.requests) == 2
    assert row.status == "expired" and row.consumed_at is None
    if not legacy_snapshot:
        assert target.read_bytes() == b"new collision after approval"
    assert all(path.read_bytes() != PNG for path in media_storage.rglob("*.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_plan", [False, True])
async def test_tokenless_media_approval_match_includes_frozen_output_plan(
    media_storage, approval_store, changed_plan,
):
    from packages.core.ai.runtime.approval_service import APPROVAL_TOKEN_IGNORED

    request, receipt = await _approve("hero.png", task_id=None)
    row = await _granted_media_approval(approval_store, request, receipt)
    if changed_plan:
        target = media_storage / receipt.media_output_plan.targets[0].rel_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"new collision")
    resumed_request, resumed_receipt = await _approve("hero.png", task_id=None)
    result = await _consume_media_approval(
        approval_store, row, resumed_request, resumed_receipt, token="unknown-token",
    )
    assert result is (APPROVAL_TOKEN_IGNORED if changed_plan else None)
    assert row.status == ("granted" if changed_plan else "consumed")


@pytest.mark.asyncio
@pytest.mark.parametrize("mime, extension", [("image/png", "png"), ("image/jpeg", "jpg"), ("image/webp", "webp")])
async def test_provider_image_format_selects_only_preapproved_exact_target(
    media_storage, mime, extension,
):
    from io import BytesIO

    from PIL import Image

    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileResourceMatchKind

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "teal").save(buffer, format={"jpg": "JPEG"}.get(extension, extension.upper()))
    provider_bytes = buffer.getvalue()
    request, receipt = await _approve("Hero_Asset")
    assert {target.rel_path for target in receipt.media_output_plan.targets} == {
        f"{BASE}/images/hero-asset.{suffix}" for suffix in ("png", "jpg", "webp")
    }
    assert all(resource.match_kind is WorkspaceFileResourceMatchKind.EXACT for resource in receipt.file_resources)
    with _authorized(request, receipt):
        url = await _save("Hero_Asset", mime=mime, image_bytes=provider_bytes)
    assert url == entity_fs_open_url(ENTITY, f"{BASE}/images/hero-asset.{extension}")
    assert (media_storage / BASE / f"images/hero-asset.{extension}").read_bytes() == provider_bytes
    for path in (f"{BASE}/images/arbitrary.png", f"{BASE}/images/hero-asset.gif"):
        assert not receipt.authorizes_workspace_file_commit(
            entity_id=ENTITY, path=path, target_exists=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["generate_file", "generate_image"])
async def test_model_supplied_output_plan_cannot_replace_server_prepared_targets(media_storage, tool_name):
    forged = {
        "version": 1, "kind": "image", "artifact_base_dir": BASE,
        "paths": [f"{BASE}/images/unapproved.png"],
        "requested_path": f"{BASE}/images/unapproved.png",
    }
    request, receipt = await _approve("hero.png", tool_name=tool_name, params={"media_output_plan": forged})
    assert receipt.media_output_plan.to_dict() != forged
    assert not receipt.authorizes_workspace_file_commit(
        entity_id=ENTITY, path=forged["paths"][0], target_exists=False,
    )
    with _authorized(request, receipt), pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save("unapproved.png")
    assert not list(media_storage.rglob("*.png"))
