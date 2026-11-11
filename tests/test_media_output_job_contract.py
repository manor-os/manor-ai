"""Real media writes retain prepared targets across synchronous/worker paths."""
from __future__ import annotations

import base64
import copy
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

import pytest

from packages.core import database
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalRequest
from packages.core.ai.runtime.authorization_receipts import (
    RuntimeToolAuthorizationReceiptFactory,
    RuntimeToolAuthorizationWriteError,
    runtime_begin_tool_authorization_scope,
    runtime_end_tool_authorization_scope,
)
from packages.core.ai.runtime.media_outputs import prepare_runtime_media_output_plan
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeWorkspaceFileScope,
    WorkspaceFileExistence,
)
from packages.core.ai.tools import extended_tools
from packages.core.config import get_settings
from packages.core.contracts.media_job_output import (
    MEDIA_JOB_OUTPUT_CONTRACT_KEY,
    MediaJobOutputContract,
    media_job_output_kwargs,
    public_media_job_params,
)
from packages.core.services import document_service, generated_media_naming, knowledge_sync
from packages.core.tasks import media_tasks, video_adapters


pytestmark = pytest.mark.oss_smoke


ENTITY = "entmediaoutput"
USER = "usermediaoutput"
WORKSPACE = "wsmediaoutput"
TASK = "taskmediaoutput"
JOB = "jobmediaoutput"
BASE = "Workspaces/_by_id/foldermediaoutput"
TASK_BASE = f"{BASE}/tasks/{TASK}"
PROMPT = "A media artifact for a contract regression"
VIDEO = b"synthetic-provider-video"
VIDEO_URL = "data:video/mp4;base64," + base64.b64encode(VIDEO).decode()


@pytest.fixture
def media_storage(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")

    async def workspace_base(*, entity_id, workspace_id, task_id=None):
        assert entity_id == ENTITY
        assert workspace_id == WORKSPACE
        return generated_media_naming.workspace_task_artifact_dir(BASE, task_id)

    document = SimpleNamespace(id="docmediaoutput", metadata_={})

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: document)

        async def commit(self):
            return None

    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(database, "create_worker_session", lambda: Session)
    monkeypatch.setattr(generated_media_naming, "resolve_workspace_artifact_base_dir", workspace_base)
    monkeypatch.setattr(extended_tools, "_bill_media", AsyncMock())
    monkeypatch.setattr(extended_tools, "_ensure_generated_media_document_folder", AsyncMock(return_value="foldermediaoutput"))
    monkeypatch.setattr(document_service, "upsert_document_by_fs_path", AsyncMock(return_value=document))
    monkeypatch.setattr(knowledge_sync, "bind_document_to_workspace", AsyncMock())
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", AsyncMock(return_value=SimpleNamespace(
        synced=True, document_id=document.id,
    )))
    return tmp_path / ENTITY


def _approve(kind, name, root, *, base=TASK_BASE):
    arguments = {"kind": kind, "name": name, "prompt": PROMPT}
    if kind == "audio":
        arguments["purpose"] = "speech"
    plan = prepare_runtime_media_output_plan(
        tool_name="generate_file", arguments=arguments,
        artifact_base_dir=base, entity_root=str(root),
    )
    scope = RuntimeWorkspaceFileScope(WorkspaceFileExistence.MISSING, base, plan)
    classification = classify_runtime_tool(
        "generate_file", arguments, entity_id=ENTITY, workspace_id=WORKSPACE,
        task_id=TASK, workspace_file_scope=scope,
    )
    request = RuntimeApprovalRequest(
        tool_name="generate_file", arguments=arguments,
        entity_id=ENTITY, user_id=USER, workspace_id=WORKSPACE,
        task_id=TASK, workspace_file_scope=scope,
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification), request=request,
    )
    assert receipt is not None and receipt.media_output_plan is not None
    return request, receipt


def _contract(root, name="EP03_Test", *, base=TASK_BASE):
    _, receipt = _approve("video", name, root, base=base)
    return MediaJobOutputContract(
        job_id=JOB, entity_id=ENTITY, user_id=USER, workspace_id=WORKSPACE,
        task_id=TASK, plan=receipt.media_output_plan,
    ).to_dict()


async def _save_video(contract, **overrides):
    return await media_tasks._download_and_save(**{
        "video_url": VIDEO_URL, "prompt": PROMPT, "model": "test-video-model",
        "job_id": JOB, "entity_id": ENTITY, "duration": 5, "resolution": "720p",
        "output_name": "EP03_Test", "workspace_id": WORKSPACE, "task_id": TASK,
        "user_id": USER, "output_contract": contract, **overrides,
    })


def _wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00\x80\x01" * 80)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_audio_saver_uses_prepared_name_and_actual_format(media_storage):
    request, receipt = _approve("audio", "Hello_World.MP3", media_storage)
    scope = runtime_begin_tool_authorization_scope(receipt, arguments=request.arguments)
    try:
        payload = _wav()
        url = await extended_tools._save_generated_audio_bytes(
            entity_id=ENTITY, user_id=USER, prompt=PROMPT, model="test-audio-model",
            purpose="speech", audio_bytes=payload, audio_format="wav", is_byok=True,
            output_name="Hello_World.MP3", workspace_id=WORKSPACE, task_id=TASK,
        )
    finally:
        runtime_end_tool_authorization_scope(scope)
    path = f"{TASK_BASE}/audio/hello-world.wav"
    assert url == f"/api/v1/fs/{ENTITY}/{path}"
    assert (media_storage / path).read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [
    BASE, TASK_BASE, f"{BASE}/workflow/runmediaoutput",
    # A run working on a WorkflowProject makes its clips in the project's folder.
    f"{BASE}/projects/launch-film", f"{BASE}/projects/launch-film/takes/runmediaoutput",
])
async def test_video_worker_preserves_frozen_target_without_runtime_lease(media_storage, base):
    contract = _contract(media_storage, base=base)
    result = await _save_video(contract)
    archive = base in {TASK_BASE, f"{BASE}/workflow/runmediaoutput"}
    path = f"{base}/videos/ep03-test.mp4" if archive else f"{base}/ep03-test.mp4"
    assert result["result_url"] == f"/api/v1/fs/{ENTITY}/{path}"
    assert (media_storage / path).read_bytes() == VIDEO
    assert result["document_id"] == "docmediaoutput"


@pytest.mark.asyncio
async def test_in_loop_video_job_keeps_explicit_workspace_destination(media_storage, monkeypatch):
    """A heavy worker runs the job in its loop, inheriting the Workflow's artifact scope.

    Prod 2026-10-28: a series episode's clips were approved for the Workspace
    home (projects/<series>/<episode>/...), but the in-loop job resolved the
    run archive and rejected every clip as "Video output Workspace changed
    after authorization".
    """
    name = "projects/series/e02/clips/clip-01"
    contract = _contract(media_storage, name=name, base=BASE)

    async def run_archive(*, entity_id, workspace_id, task_id=None):
        return f"{BASE}/workflow/runmediaoutput"

    monkeypatch.setattr(generated_media_naming, "resolve_workspace_artifact_base_dir", run_archive)
    result = await _save_video(contract, output_name=name)
    path = f"{BASE}/{name}.mp4"
    assert result["result_url"] == f"/api/v1/fs/{ENTITY}/{path}"
    assert (media_storage / path).read_bytes() == VIDEO


@pytest.mark.asyncio
async def test_video_worker_selects_prepared_webm_encoding(media_storage):
    result = await _save_video(
        _contract(media_storage), video_url=VIDEO_URL.replace("video/mp4", "video/webm"),
    )
    assert result["result_url"].endswith("/videos/ep03-test.webm")


@pytest.mark.asyncio
async def test_unnamed_video_keeps_target_when_provider_prompt_is_augmented(media_storage):
    contract = _contract(media_storage, name="")
    expected = MediaJobOutputContract.from_dict(contract).plan.target_for_extension(".mp4")
    result = await _save_video(contract, output_name="", prompt=PROMPT + " Silent picture only.")
    assert result["result_url"] == f"/api/v1/fs/{ENTITY}/{expected.rel_path}"
    assert (media_storage / expected.rel_path).read_bytes() == VIDEO


@pytest.mark.asyncio
async def test_video_worker_uses_preselected_collision_target(media_storage):
    existing = media_storage / TASK_BASE / "videos/ep03-test.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"prior artifact")
    contract = _contract(media_storage)
    result = await _save_video(contract)
    assert result["result_url"].endswith("/videos/ep03-test-2.mp4")
    assert existing.read_bytes() == b"prior artifact"


@pytest.mark.asyncio
async def test_video_worker_rejects_late_collision_instead_of_renaming(media_storage):
    contract = _contract(media_storage)
    target = media_storage / TASK_BASE / "videos/ep03-test.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"competing write")
    with pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save_video(contract)
    assert target.read_bytes() == b"competing write"
    assert not target.with_name("ep03-test-2.mp4").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["job_id", "entity_id", "user_id", "workspace_id", "task_id"])
async def test_video_worker_rejects_contract_from_other_scope(media_storage, field):
    contract = _contract(media_storage)
    contract[field] = "foreign-identity"
    with pytest.raises(PermissionError):
        await _save_video(contract)
    assert not list(media_storage.rglob("*.mp4"))


@pytest.mark.asyncio
async def test_video_worker_rejects_changed_name_and_foreign_storage_base(media_storage):
    contract = _contract(media_storage)
    with pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save_video(contract, output_name="Unapproved clip")
    foreign = copy.deepcopy(contract)
    foreign["plan"]["artifact_base_dir"] = "Workspaces/_by_id/foreign"
    foreign["plan"]["paths"] = ["Workspaces/_by_id/foreign/videos/ep03-test.mp4"]
    foreign["plan"]["requested_path"] = foreign["plan"]["paths"][0]
    with pytest.raises(RuntimeToolAuthorizationWriteError):
        await _save_video(foreign)
    assert not list(media_storage.rglob("*.mp4"))


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, {}, {"version": 2}, "ignored"])
async def test_present_invalid_video_contract_cannot_use_legacy_writer(media_storage, malformed):
    with pytest.raises(ValueError):
        await _save_video(malformed)
    assert not list(media_storage.rglob("*.mp4"))


@pytest.mark.asyncio
async def test_legacy_video_job_keeps_legacy_resolver(media_storage):
    result = await media_tasks._download_and_save(
        VIDEO_URL, PROMPT, "test-video-model", JOB, ENTITY, 5, "720p",
        output_name="Legacy_Clip", workspace_id=WORKSPACE, task_id=TASK, user_id=USER,
    )
    assert result["result_url"].endswith("/videos/legacy-clip.mp4")


@pytest.mark.asyncio
async def test_video_normal_and_recovery_paths_forward_same_contract(monkeypatch, media_storage):
    from packages.core.ai import runtime

    contract = _contract(media_storage)
    job = SimpleNamespace(
        id=JOB, entity_id=ENTITY, user_id=USER, agent_id=None, conversation_id=None,
        prompt=PROMPT, model="test-video-model", duration_seconds=5,
        params={
            MEDIA_JOB_OUTPUT_CONTRACT_KEY: contract, "output_name": "EP03_Test",
            "workspace_id": WORKSPACE, "task_id": TASK,
            "provider_poll": {"poll_url": "https://provider.example/job", "provider": "google"},
        },
    )
    download = AsyncMock(return_value={"result_url": "saved"})
    await video_adapters._download_adapter_result(
        SimpleNamespace(download_and_save=download), job, VIDEO_URL, job.model, 5, "720p",
    )
    normal = download.call_args
    monkeypatch.setattr(media_tasks, "_download_and_save", download)
    monkeypatch.setattr(media_tasks, "_check_provider_poll_once", AsyncMock(return_value={"video_url": VIDEO_URL}))
    monkeypatch.setattr(runtime, "runtime_resolve_video_recovery_credentials", AsyncMock(return_value=SimpleNamespace(
        provider="google", api_key="test-key", base_url_override="",
    )))
    await media_tasks._resume_video_job_from_provider(job)
    recovered = download.call_args
    assert normal.kwargs["output_contract"] is contract
    assert recovered.kwargs["output_contract"] is contract
    assert normal.kwargs["task_id"] == recovered.kwargs["task_id"] == TASK


def test_contract_is_hidden_from_api_and_present_null_does_not_disappear():
    params = {"duration": 5, MEDIA_JOB_OUTPUT_CONTRACT_KEY: None}
    assert public_media_job_params(params) == {"duration": 5}
    assert media_job_output_kwargs(params) == {"output_contract": None}
    assert media_job_output_kwargs({"duration": 5}) == {}
