"""Portable Workspaces and actual FFmpeg/Knowledge delivery, not prompt snapshots."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import recording_tools as recording
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.services.workflow_service import validate_workflow_steps
from scripts.blueprints.recording_workspaces import WORKSPACES, configure_and_export, workflow_steps
from test_practical_workspace_blueprints import _checker


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "packages/core/blueprints/configs/recording"
SEGMENTS = [
    {"id": 0, "start": 0.0, "end": 6.0, "text": "Start a new request and give it a clear title."},
    {"id": 1, "start": 6.0, "end": 12.0, "text": "Select the reviewer and save the draft. Check the draft before submitting."},
    {"id": 2, "start": 12.0, "end": 18.0, "text": "Open the review queue to see your submitted request."},
    {"id": 3, "start": 18.0, "end": 24.0, "text": "The reviewer can accept the request. Check the final status in the queue."},
]
PLAN = {"cuts": [
    {"title": "Prepare a request", "first_segment": 0, "last_segment": 1},
    {"title": "Review the result", "first_segment": 2, "last_segment": 3},
]}


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
def test_recording_showcase_uses_shipped_non_manor_example_media(spec):
    from apps.api.routers.blueprints import BlueprintShowcaseAsset

    manifest = json.loads((CONFIG_DIR / f"{spec.slug}.json").read_text())["manifest"]
    assert len(manifest["showcase_assets"]) == (5 if spec.key == "shorts" else 4)
    assert manifest["showcase_assets"][0]["id"] == "concept-cover"
    evidence = manifest["showcase_assets"][1:]
    assert sum(asset["kind"] == "image" for asset in evidence) == 2
    assert "Example — cafe" in manifest["description"]
    assert "not order-management systems or POS integrations" in manifest["description"]
    assert manifest["cover_image_url"] == manifest["showcase_assets"][0]["url"]
    for raw in evidence:
        asset = BlueprintShowcaseAsset.model_validate(raw)
        assert asset.url.startswith("/blueprints/recording/coffee-")
        assert "Cedar Cup is a fictional cafe" in asset.caption
        assert "AI-generated speech" in asset.caption
        assert "not continuous screen recording or customer footage" in asset.caption
        data = (ROOT / "apps/web/public" / asset.url.lstrip("/")).read_bytes()
        assert len(data) > 10000
        if asset.kind == "video":
            assert asset.mime_type == "video/mp4" and data[4:8] == b"ftyp"
        elif asset.mime_type == "image/png":
            assert asset.kind == "image" and data.startswith(b"\x89PNG\r\n\x1a\n")
        else:
            assert asset.kind == "image" and asset.mime_type == "image/jpeg"
            assert data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")


def test_recording_srt_upload_keeps_text_and_executable_safety_checks():
    from packages.core.services.upload_security import UploadSecurityError, validate_upload_header

    assert validate_upload_header(
        filename="recording.srt", header=b"1\n00:00:00,000 --> 00:00:12,000\nSpoken words.\n",
    ) in {"text/plain", "application/x-subrip"}
    for unsafe in (b"MZ executable", b"\x7fELF executable", b"subtitle\x00binary"):
        with pytest.raises(UploadSecurityError):
            validate_upload_header(filename="recording.srt", header=unsafe)


def test_finished_recording_review_is_read_only_with_real_files_and_revision():
    from packages.core.services.workflow_chat_approvals import _review_variable

    for spec in WORKSPACES:
        config = next(step["config"] for step in workflow_steps(spec) if step["id"] == "review")
        assert config["review_read_only"] is True
        assert config["review"]["artifacts"] == "{{delivery.final_videos}}"
        assert config["options"] == ["accept", "revise"]
        assert _review_variable(config) is None
    assert _review_variable({"review": "{{delivery}}", "review_read_only": True}) is None
    assert _review_variable({"review": "{{draft}}"}) == "draft"


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_real_workspace_export_install_roundtrip(db_session, spec, mode):
    source_entity = Entity(id=generate_ulid(), name="Source", plan_id="plan_enterprise",
        settings={"approval_defaults": account_defaults()})
    target_entity = Entity(id=generate_ulid(), name="Target", plan_id="plan_enterprise",
        settings={"approval_defaults": account_defaults()})
    db_session.add_all([source_entity, target_entity])
    await db_session.flush()
    source, payload = await configure_and_export(db_session, entity_id=source_entity.id, spec=spec)
    expected = json.loads((CONFIG_DIR / f"{spec.slug}.json").read_text())
    # The default policy now follows the account and exports no local consent.
    expected["policy"]["governance"] = {}
    assert payload == expected
    assert _checker("check_blueprint").check(payload) == []
    assert payload["recipe"]["workflows"][0]["steps"] == workflow_steps(spec)
    assert validate_workflow_steps(workflow_steps(spec))["valid"]
    for section in ("scheduled_jobs", "goals", "task_templates", "task_categories", "sla_policies"):
        assert not payload["recipe"].get(section)
    assert payload["contract"]["variables"] == []
    assert payload["contract"]["channels"] == payload["contract"]["sessions"] == []
    assert source.id not in json.dumps(payload) and source_entity.id not in json.dumps(payload)
    installed = await install_blueprint(db_session, entity_id=target_entity.id, payload=payload,
                                        mode=mode, blueprint_slug=spec.slug)
    assert not installed.todos
    assert len(installed.subscription_ids) == len(installed.workflow_binding_ids) == 1
    target = await db_session.get(Workspace, installed.workspace_id)
    assert not target.heartbeat_enabled
    assert bool(target.settings.get("sandbox")) == (mode == InstallMode.SIMULATE)
    checks = [item["payload"]["check"] for item in target.settings["_blueprint"]["live_setup_requirements"]
              if item["kind"] == "post_install_check"]
    assert checks == payload["policy"]["post_install_checks"]
    exported = await export_workspace(db_session, target.id, title=spec.title,
                                      context=ExportContext(include_knowledge_packs=False))
    assert _checker("compare_roundtrip").compare(payload, exported) == []


@pytest.mark.parametrize("raw", [[], [{"start": math.nan, "end": 10, "text": "x"}],
    [{"start": 0, "end": math.inf, "text": "x"}], [{"start": 0, "end": 26, "text": "x"}],
    [{"start": 0, "end": 2, "text": ""}], [SEGMENTS[1], SEGMENTS[0]]])
def test_invalid_transcript_never_becomes_cut_evidence(raw):
    with pytest.raises(ValueError):
        recording._segments(raw, 24)


@pytest.mark.parametrize("cuts", [
    [{"title": "bad", "first_segment": 0, "last_segment": 99}],
    [PLAN["cuts"][1], PLAN["cuts"][0]],
    [PLAN["cuts"][0], PLAN["cuts"][0]],
    [{"title": "too short", "first_segment": 0, "last_segment": 0}],
])
def test_out_of_bounds_duplicate_reordered_or_short_cuts_fail(cuts):
    with pytest.raises(ValueError):
        recording._cuts({"cuts": cuts}, SEGMENTS, "shorts")


def test_tutorial_subtitle_offsets_follow_the_selected_source_not_model_text():
    cuts = recording._cuts(PLAN, SEGMENTS, "tutorial")
    cues, chapters = recording._subtitle_cues(cuts, SEGMENTS)
    assert chapters[1]["start_seconds"] == 12
    assert cues[0].start == 0 and cues[-1].end == 24
    assert " ".join(c.text for c in cues) == " ".join(c["text"] for c in SEGMENTS)


def test_portrait_cjk_captions_use_a_full_width_glyph_budget():
    text = "打开工作区选择已上传的录屏素材然后检查字幕时间是否正确最后确认生成的成片"
    segments = [{"start": 0, "end": 12, "text": text}]
    cuts = recording._cuts({"cuts": [{"title": "检查素材", "first_segment": 0, "last_segment": 0}]}, segments, "shorts")
    cues, _ = recording._subtitle_cues(cuts, segments, line_width=18)
    assert all(len(line) <= 18 for cue in cues for line in recording.media._wrap_subtitle_text(cue.text, 18).splitlines())
    assert "".join(c.text.replace(" ", "") for c in cues) == text


def test_chat_prioritizes_all_five_finished_videos_over_sidecar_files():
    from packages.core.services.workflow_chat_projection import _workflow_final_output_attachments

    videos = [{"kind": "video", "status": "completed", "document_id": f"video-{i}",
               "name": f"{i}.mp4", "fs_path": f"Workspaces/test/production/{i}.mp4", "file_size": 100}
              for i in range(5)]
    outputs = [{"video": v, "timeline": {"document_id": f"timeline-{i}", "name": f"{i}.json",
                "fs_path": f"Workspaces/test/production/{i}.json", "file_size": 100}} for i, v in enumerate(videos)]
    attachments = _workflow_final_output_attachments({"status": "accepted", "delivery": {
        "final_videos": videos, "outputs": outputs,
    }}, entity_id="entity")
    assert [item["document_id"] for item in attachments[:5]] == [v["document_id"] for v in videos]


def test_tools_are_registered_and_have_scoped_write_authorization():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools import register_all_tools

    pool = ToolPool()
    register_all_tools(pool)
    for schema, _ in recording.get_tools():
        name = schema["function"]["name"]
        assert name in pool.registered_tool_names()
        classification = classify_runtime_tool(name, {"output_directory": "production/test"})
        assert classification.effect == RuntimeToolEffect.ACTION
        assert classification.action.action_key == "workspace.file.create"
        from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory
        resources = WorkspaceFileAuthorizationResourceFactory.media_tool_resources(
            name, {"output_directory": "production/test"},
        )
        assert len(resources) == 1 and resources[0].resource_id == "production/test"
        assert not WorkspaceFileAuthorizationResourceFactory.media_tool_resources(name, {})


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
async def test_missing_input_and_fake_delivery_cannot_complete(spec):
    runner = WorkflowRunner()
    steps = {s["id"]: s for s in workflow_steps(spec)}
    run = WorkflowRun(id="run", entity_id="entity", workflow_id="flow", variables={"source_path": " "}, step_results={})
    result = await runner._execute_step_safe(steps["start"], run, None)
    assert result["code"] == "input_schema_validation_failed"
    run.variables = {"edit_plan": {"status": "needs_input", "cuts": [], "reason": "No spoken explanation"}}
    route = await runner._execute_step_safe(steps["has_plan"], run, None)
    assert route["next_override"] == ["needs_input"]


@pytest.mark.media_integration
@pytest.mark.oss_regression
@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
async def test_installed_graph_renders_real_media_and_waits_for_review(client, db_session, monkeypatch, tmp_path, spec):
    from auth_helpers import register_user_and_get_token
    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.runtime import file_actions
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.services.workflow_service import start_workflow_from_binding
    from packages.core.services.generated_media_naming import resolve_workspace_artifact_base_dir

    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "Real FFmpeg is required for the production gate"
    # The gate burns subtitles through libass. The Docker image and CI install
    # Debian/Ubuntu ffmpeg, which links it; Homebrew's ffmpeg formula (8.0.1_4)
    # no longer does, so on such a host the gate cannot run at all.
    filters = subprocess.run(
        [shutil.which("ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True, check=False,
    ).stdout
    if not re.search(r"\bsubtitles\s+V->V", filters):
        pytest.skip("host FFmpeg lacks the libass subtitles filter; the production gate needs an ffmpeg built with libass")
    settings = get_settings()
    live_model = os.getenv("RECORDING_LIVE_MODEL_SMOKE") == "1"
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", os.getenv("DEPLOYMENT_MODE", "oss") if live_model else "oss")
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    if live_model and settings.DEPLOYMENT_MODE == "cloud":
        original_ismount = os.path.ismount
        monkeypatch.setattr(os.path, "ismount", lambda path: os.path.abspath(path) == str(tmp_path) or original_ismount(path))
        (tmp_path / ".accesschk").touch()
    monkeypatch.setattr(file_actions, "runtime_trigger_document_embeddings", lambda _id: None)
    account = (await register_user_and_get_token(client, {
        "username": "recording-test", "email": "recording@example.test", "password": "TestPassword123!",
        "entity_name": "Recording test",
    })).json()
    entity_id, user_id = account["entity_id"], account["user_id"]
    payload = json.loads((CONFIG_DIR / f"{spec.slug}.json").read_text())
    installed = await install_blueprint(db_session, entity_id=entity_id, user_id=user_id, payload=payload,
                                        mode=InstallMode.LIVE, blueprint_slug=spec.slug)
    # Portable templates cannot confer consent. The operator authorizes local
    # production explicitly; the graph's mandatory delivery review still pauses.
    from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval
    await set_workspace_test_approval(db_session, entity_id=entity_id,
        workspace_id=installed.workspace_id, actor_id=user_id,
        user_rules=[action_rule("workspace.file.create")])
    await db_session.commit()
    base = await resolve_workspace_artifact_base_dir(entity_id=entity_id, workspace_id=installed.workspace_id)
    source = tmp_path / entity_id / base / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    # Synthetic picture + tone is an engineering fixture, not product proof or a speech test.
    # A slightly shorter audio track reproduces the browser-discovered AAC-tail
    # regression: -shortest used to truncate video and put final captions outside it.
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=15:duration=24", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=23.9", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(source)], check=True, timeout=60)
    srt = source.with_suffix(".srt")
    srt.write_text("\n\n".join(f"{i+1}\n00:00:{i*6:02d},000 --> 00:00:{(i+1)*6:02d},000\n{s['text']}"
                             for i, s in enumerate(SEGMENTS)))

    async def model_output(**kwargs):
        assert kwargs["tools"] == []
        assert SEGMENTS[0]["text"] in kwargs["user_message"]
        assert spec.editorial_rules in kwargs["system_prompt"]
        return SimpleNamespace(content=json.dumps({"status": "ready", **PLAN, "reason": "Two complete steps"}),
                               error=None, stop_reason="end_turn", usage={}, tool_calls_made=[], rounds=1)

    if not live_model:
        monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", model_output)
    binding = await db_session.get(WorkflowBinding, installed.workflow_binding_ids[0])
    workflow = await db_session.get(WorkflowDefinition, binding.workflow_id)
    run = await start_workflow_from_binding(db_session, binding,
        variables={"source_path": base + "/source.mp4", "subtitle_path": base + "/source.srt",
                   "focus": "Make two standalone clips about preparing and reviewing a request." if spec.key == "shorts"
                            else "Make one tutorial with separate preparation and review chapters."},
        trigger_source="manual", started_by=user_id, user_requested=True)
    await db_session.commit()
    await WorkflowRunner()._run_loop(workflow, run, db_session)
    assert run.status == "paused", (run.error, run.step_results)
    assert run.current_step_id == "review"
    delivery = run.variables["delivery"]
    assert delivery["machine_pass"] and delivery["review_required"]
    assert len(delivery["outputs"]) == (2 if spec.key == "shorts" else 1)
    for output in delivery["outputs"]:
        for artifact in (output["video"], output["subtitles"], output["burned_subtitles"], output["timeline"], delivery["receipt"]):
            doc = await db_session.get(Document, artifact["document_id"])
            assert doc and doc.entity_id == entity_id
            assert doc.metadata_["origin"]["workspace_id"] == installed.workspace_id
            assert (tmp_path / entity_id / doc.fs_path).stat().st_size == artifact["file_size"]
        assert output["qa"]["full_decode"] is True
        assert output["qa"]["probe"]["has_audio"]
        assert output["qa"]["subtitles"]["verdict"] == "pass"
        assert output["video"]["include_source_audio"] is True
        assert output["video"]["burn_subtitles"] is True
        assert output["video"]["subtitle_path"].endswith(".ass")
        ass = (tmp_path / entity_id / output["burned_subtitles"]["fs_path"]).read_text()
        assert f"PlayResX: {720 if spec.key == 'shorts' else 1280}" in ass
        assert f"PlayResY: {1280 if spec.key == 'shorts' else 720}" in ass
        assert output["preview"]["frames"][0]["document_id"]
        recipe = json.loads((tmp_path / entity_id / output["video"]["editor_recipe_path"]).read_text())
        assert recipe["timeline"]["captions"]
        assert recipe["timeline"]["clips"][0]["assetDocumentId"] != output["video"]["document_id"]
        assert all(chapter["title"] in {marker["label"] for marker in recipe["timeline"]["markers"]}
                   for chapter in output["chapters"])
    # Resume through the actual operator boundary, not by setting a run variable.
    # Queue transport is outside this in-process test; never dispatch fixture runs
    # to a real worker connected to another database.
    monkeypatch.setattr(WorkflowRunner, "enqueue_resume", staticmethod(lambda *args, **kwargs: False))
    await db_session.commit()
    choice = "accept" if spec.key == "shorts" else "revise"
    assert await WorkflowRunner.resume(run.id, {"acceptance": {"choice": choice}}, entity_id=entity_id,
                                        resumed_by=user_id, execute=False) == "resumed"
    await db_session.refresh(run)
    await WorkflowRunner()._run_loop(workflow, run, db_session)
    assert run.status == "completed", (run.error, run.step_results)
    assert run.variables["__result"]["status"] == ("accepted" if choice == "accept" else "revision_required")
    # Changed input cannot render using old planning evidence.
    snapshot = run.variables["source"]["snapshot"]
    source.write_bytes(source.read_bytes() + b"changed")
    rejected = json.loads(await recording._render_recording_handler(
        entity_id=entity_id, user_id=user_id, workspace_id=installed.workspace_id,
        snapshot_path=snapshot["fs_path"], snapshot_sha256=snapshot["sha256"],
        mode=spec.key, plan=PLAN, output_directory="production/deliveries",
    ))
    assert rejected["status"] == "failed" and "changed" in rejected["error"]


async def test_runtime_acl_blocks_cross_workspace_and_unreadable_sources(monkeypatch, tmp_path):
    from packages.core.services import entity_fs

    async def base(**_):
        return "Workspaces/one"

    async def preflight():
        return "ffmpeg", "ffprobe"

    async def deny(**kwargs):
        assert kwargs["paths"] == ["Workspaces/one/private.mp4"]
        return {"error": "denied"}

    monkeypatch.setattr(recording.media, "_workspace_media_base_dir", base)
    monkeypatch.setattr(recording, "_runtime_preflight", preflight)
    monkeypatch.setattr(entity_fs, "get_entity_root", lambda _: str(tmp_path))
    monkeypatch.setattr(recording.media, "runtime_guard_file_read_access", deny)
    path = tmp_path / "Workspaces/one/private.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not visible")
    for source in ("Workspaces/two/private.mp4", "Workspaces/one/private.mp4", "https://example.test/private.mp4"):
        result = json.loads(await recording._prepare_recording_handler(
            entity_id="entity", user_id="user", workspace_id="one", source_path=source,
            output_directory="production/sources", _user_id_from_context="user",
        ))
        assert result.get("error")


async def test_missing_subtitle_renderer_blocks_before_billable_transcription(monkeypatch):
    async def filters(*args, **kwargs):
        return " ... scale V->V Scale video", ""

    monkeypatch.setattr(recording.shutil, "which", lambda name: name)
    monkeypatch.setattr(recording.media, "_run_process", filters)
    with pytest.raises(ValueError, match="libass"):
        await recording._runtime_preflight()


@pytest.mark.parametrize("failure", ["credit blocked", "text-only STT"])
async def test_transcription_blocker_never_saves_a_prepared_snapshot(monkeypatch, tmp_path, failure):
    """Exercise the no-SRT branch with provider boundaries stubbed, not bypassed."""
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"fixture")

    async def scope(*args):
        return str(tmp_path), "production/sources/test"

    async def source_path(*args):
        return "recording.mp4", str(source)

    async def preflight():
        return "ffmpeg", "ffprobe"

    async def probe(*args):
        return {"duration_seconds": 24, "has_video": True, "has_audio": True}

    async def process(*args, **kwargs):
        return "", ""

    async def transcribe(**kwargs):
        assert kwargs["user_id"] == "actor" and kwargs["workspace_id"] == "workspace"
        assert kwargs["entity_id"] == "entity"
        if failure == "credit blocked":
            raise ValueError("credit blocked")
        return SimpleNamespace(segments=[])

    async def save(*args):
        pytest.fail("A failed transcription must not become source evidence")

    monkeypatch.setattr(recording, "_scope", scope)
    monkeypatch.setattr(recording, "_source_path", source_path)
    monkeypatch.setattr(recording, "_runtime_preflight", preflight)
    monkeypatch.setattr(recording, "_save", save)
    monkeypatch.setattr(recording.media, "_probe_media_report", probe)
    monkeypatch.setattr(recording.media, "_run_process", process)
    monkeypatch.setattr(recording.media, "_transcribe_narration_audio", transcribe)
    result = json.loads(await recording._prepare_recording_handler(
        entity_id="entity", user_id="actor", workspace_id="workspace", source_path="recording.mp4",
        output_directory="production/sources",
    ))
    assert result["code"] == "recording_preparation_failed"
    assert ("credit blocked" if failure == "credit blocked" else "timestamped transcript") in result["error"]
