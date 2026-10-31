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
])
def test_out_of_bounds_duplicate_or_reordered_cuts_fail(cuts):
    with pytest.raises(ValueError):
        recording._cuts({"cuts": cuts}, SEGMENTS, "shorts")


def test_cut_length_follows_the_format_not_a_fixed_window():
    # Any length of material is usable: a 6-second clip, or a tutorial longer than ten minutes.
    one_cue = {"cuts": [{"title": "Brief", "first_segment": 0, "last_segment": 0}]}
    assert recording._cuts(one_cue, SEGMENTS, "shorts")[0]["end_seconds"] == 6.0
    long_talk = [{"id": index, "start": index * 60.0, "end": (index + 1) * 60.0, "text": f"Step {index}"}
                 for index in range(40)]
    whole = {"cuts": [{"title": "Everything", "first_segment": 0, "last_segment": 39}]}
    assert recording._cuts(whole, long_talk, "tutorial")[0]["end_seconds"] == 2400.0
    # Only a short has a ceiling, because it is a short.
    with pytest.raises(ValueError, match="at most 90 seconds"):
        recording._cuts(whole, long_talk, "shorts")


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


def test_ai_voice_follows_its_cues_and_never_spills_into_the_next_chapter():
    cuts = recording._cuts(PLAN, SEGMENTS, "tutorial")
    # A long line pushes the next one later; captions move with the voice.
    timing, tempos = recording._place_voice(cuts, SEGMENTS, {0: 7.0, 1: 4.0, 2: 3.0, 3: 3.0})
    assert timing[1] == (7.08, 11.08) and timing[2] == (12.0, 15.0)
    assert set(tempos.values()) == {1.0}
    cues, _ = recording._subtitle_cues(cuts, SEGMENTS, timing=timing)
    second = [cue for cue in cues if 7.08 <= cue.start < 12]
    assert (second[0].start, second[-1].end) == (7.08, 11.08)
    # A chapter that would overrun is read a little faster, and only that chapter.
    timing, tempos = recording._place_voice(cuts, SEGMENTS, {0: 7.0, 1: 6.0, 2: 3.0, 3: 3.0})
    assert 1.09 < tempos[0] == tempos[1] < 1.1 and tempos[2] == tempos[3] == 1.0
    assert timing[1][1] <= 12.0 and timing[2] == (12.0, 15.0)
    with pytest.raises(ValueError, match='"Prepare a request" runs 2.1s past its footage even when read faster'):
        recording._place_voice(cuts, SEGMENTS, {0: 9.0, 1: 8.5, 2: 3.0, 3: 3.0})
    assert recording._voice_language(["检查字幕"]) == "zh"
    assert recording._voice_language(["Check the draft"]) == ""
    with pytest.raises(ValueError, match="voice must be"):
        recording._voice("narrator")


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
    run = WorkflowRun(id="run", entity_id="entity", workflow_id="flow", variables={"recording": []}, step_results={})
    result = await runner._execute_step_safe(steps["start"], run, None)
    assert result["code"] == "input_schema_validation_failed"
    run.variables = {"edit_plan": {"status": "needs_input", "cuts": [], "reason": "No spoken explanation"}}
    route = await runner._execute_step_safe(steps["has_plan"], run, None)
    assert route["next_override"] == ["needs_input"]
    # The run ends asking for input, with the editor's reason as what the person reads.
    from packages.core.services.workflow_run_trace import workflow_result_outcome
    ending = await runner._execute_step_safe(steps["needs_input"], run, None)
    result = ending.get("output", ending)
    result = result.get("input", result) if isinstance(result, dict) else result
    assert result["status"] == "needs_input" and result["summary"] == "No spoken explanation"
    assert workflow_result_outcome({"__result": result}) == "needs_input"
    assert workflow_result_outcome({"__result": {"status": "completed"}}) == ""


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
    # The tutorial is narrated by the AI voice; shorts keep the source audio.
    voice = "ai" if spec.key == "tutorial" else "original"
    spoken = []
    if voice == "ai":
        from packages.core.ai.tools import extended_tools

        async def speak(**kwargs):
            # Stand-in provider bytes; the real Knowledge save path places and registers each line.
            spoken.append(kwargs["prompt"])
            tone = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                                   "sine=frequency=330:duration=2.5", "-f", "mp3", "-"],
                                  capture_output=True, check=True, timeout=30).stdout
            url = await extended_tools._save_generated_audio_bytes(
                entity_id=kwargs["entity_id"], user_id=kwargs["user_id"], prompt=kwargs["prompt"],
                model="test/tts", purpose=kwargs["purpose"], audio_bytes=tone, audio_format="mp3",
                is_byok=False, output_name=kwargs["name"], workspace_id=kwargs.get("workspace_id"),
                task_id=kwargs.get("task_id"), bill_media=False,
            )
            return json.dumps({"status": "completed", "model": "test/tts", "voice": "test",
                               "fs_path": extended_tools._fs_path_from_result_url(url, kwargs["entity_id"])})

        monkeypatch.setattr(extended_tools, "_generate_live_audio", speak)
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
    # Picked Knowledge uploads live outside the Workspace folder; every media
    # step must still read them there.
    source = tmp_path / entity_id / "source.mp4"
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

    plans = []

    async def model_output(**kwargs):
        assert kwargs["tools"] == []
        assert SEGMENTS[0]["text"] in kwargs["user_message"]
        assert spec.editorial_rules in kwargs["system_prompt"]
        plans.append(kwargs["user_message"])
        return SimpleNamespace(content=json.dumps({"status": "ready", **PLAN, "reason": "Two complete steps"}),
                               error=None, stop_reason="end_turn", usage={}, tool_calls_made=[], rounds=1)

    if not live_model:
        monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", model_output)
    binding = await db_session.get(WorkflowBinding, installed.workflow_binding_ids[0])
    workflow = await db_session.get(WorkflowDefinition, binding.workflow_id)
    picked = [Document(entity_id=entity_id, name=name, fs_path=name, owner_id=user_id)
              for name in ("source.mp4", "source.srt")]
    db_session.add_all(picked)
    await db_session.flush()
    run = await start_workflow_from_binding(db_session, binding,
        variables={"recording": [{"document_id": picked[0].id}], "subtitles": [{"document_id": picked[1].id}],
                   "focus": "Make two standalone clips about preparing and reviewing a request." if spec.key == "shorts"
                            else "Make one tutorial with separate preparation and review chapters.",
                   "voiceover": voice},
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
        assert output["video"]["include_source_audio"] is (voice == "original")
        if voice == "ai":
            # Every selected cue is voiced exactly as written, saved beside its video, and starts its caption.
            assert spoken == [segment["text"] for segment in SEGMENTS]
            folder = output["video"]["fs_path"].rsplit("/", 1)[0] + "/voice/"
            assert [line["fs_path"].startswith(folder) for line in output["voice"]] == [True] * len(SEGMENTS)
            timeline = json.loads((tmp_path / entity_id / output["timeline"]["fs_path"]).read_text())
            assert [track["start"] for track in timeline["audio_tracks"]] == [0.0, 6.0, 12.0, 18.0]
            assert [round(c["end"] - c["start"], 1) for c in timeline["captions"]][:1] == [2.5]
        else:
            assert output["voice"] == []
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
    if spec.key == "tutorial":
        # A change request replans with the note and renders a new version; the reviewed one stays.
        first = delivery["receipt"]["fs_path"]
        assert await WorkflowRunner.resume(
            run.id, {"acceptance": {"choice": "revise", "note": "Shorten the review chapter."}},
            entity_id=entity_id, resumed_by=user_id, execute=False) == "resumed"
        await db_session.refresh(run)
        await WorkflowRunner()._run_loop(workflow, run, db_session)
        assert run.status == "paused" and run.current_step_id == "review", (run.error, run.step_results)
        assert "Shorten the review chapter." in plans[-1]
        assert run.variables["delivery"]["receipt"]["fs_path"] != first
        assert (tmp_path / entity_id / first).exists()
    assert await WorkflowRunner.resume(run.id, {"acceptance": {"choice": "accept"}}, entity_id=entity_id,
                                        resumed_by=user_id, execute=False) == "resumed"
    await db_session.refresh(run)
    await WorkflowRunner()._run_loop(workflow, run, db_session)
    assert run.status == "completed", (run.error, run.step_results)
    assert run.variables["__result"]["status"] == "accepted"
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


async def test_picked_recording_uses_its_document_path_and_the_same_acl(db_session, monkeypatch, tmp_path):
    from packages.core.ai.runtime.media import runtime_document_ref_paths
    from packages.core.models.document import Document
    from packages.core.services import entity_fs

    entity = Entity(id=generate_ulid(), name="Picker", plan_id="plan_enterprise")
    db_session.add(entity)
    await db_session.flush()
    private, srt, trashed = (Document(entity_id=entity.id, name=name, fs_path=f"Workspaces/one/{name}", is_trashed=gone)
                             for name, gone in (("private.mp4", False), ("private.srt", False), ("old.mp4", True)))
    unstored = Document(entity_id=entity.id, name="notes.md", fs_path=None)
    db_session.add_all([private, srt, trashed, unstored])
    await db_session.commit()
    # The browser's fs_path is not trusted; the Document row decides the path.
    assert await runtime_document_ref_paths(
        [{"document_id": private.id, "fs_path": "Workspaces/one/public.mp4"}, {"document_id": private.id},
         {"document_id": srt.id}], entity.id, limit=2,
    ) == ["Workspaces/one/private.mp4", "Workspaces/one/private.srt"]
    assert await runtime_document_ref_paths([], entity.id, limit=1) == []
    for refs, limit in (([{"document_id": trashed.id}], 1), ([{"document_id": unstored.id}], 1),
                        ([{"document_id": private.id}], 0),
                        ([{"name": "private.mp4"}], 1), ("Workspaces/one/private.mp4", 1)):
        with pytest.raises(ValueError):
            await runtime_document_ref_paths(refs, entity.id, limit=limit)
    with pytest.raises(ValueError):
        await runtime_document_ref_paths([{"document_id": private.id}], generate_ulid(), limit=1)

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
    (tmp_path / "Workspaces/one").mkdir(parents=True)
    (tmp_path / "Workspaces/one/private.mp4").write_bytes(b"not visible")
    result = json.loads(await recording._prepare_recording_handler(
        entity_id=entity.id, user_id="user", workspace_id="one", output_directory="production/sources",
        source=[{"document_id": private.id, "fs_path": "Workspaces/one/public.mp4"}], _user_id_from_context="user",
    ))
    # deny() saw the Document's path, and the same read check refused it.
    assert result.get("error")

    # An upload outside the Workspace folder resolves to its own path, while a
    # Workspace file of the same name still wins.
    (tmp_path / "talk.mp4").write_bytes(b"upload")
    ctx = {"entity_id": entity.id, "workspace_id": "one", "task_id": None}
    assert (await recording._source_path("talk.mp4", ctx))[0] == "talk.mp4"
    (tmp_path / "Workspaces/one/talk.mp4").write_bytes(b"workspace")
    assert (await recording._source_path("talk.mp4", ctx))[0] == "Workspaces/one/talk.mp4"


async def test_missing_subtitle_renderer_blocks_before_billable_transcription(monkeypatch):
    async def filters(*args, **kwargs):
        return " ... scale V->V Scale video", ""

    monkeypatch.setattr(recording.shutil, "which", lambda name: name)
    monkeypatch.setattr(recording.media, "_run_process", filters)
    with pytest.raises(ValueError, match="libass"):
        await recording._runtime_preflight()


@pytest.mark.parametrize("failure", ["credit blocked", "text-only STT", "no timings"])
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
        if failure == "no timings":
            from packages.core.services.voice.whisper import WhisperTimestampError
            raise WhisperTimestampError("The configured OpenRouter STT route does not provide measured timestamps.")
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
    assert {"credit blocked": "credit blocked", "text-only STT": "No speech was found",
            "no timings": "Add the recording's SRT, or add an OpenAI or Groq key"}[failure] in result["error"]
    assert "OpenRouter" not in result["error"]


@pytest.mark.parametrize("probe_report,expected", [
    ({"duration_seconds": 24, "has_video": False, "has_audio": True}, "This file has no picture."),
    ({"duration_seconds": 0, "has_video": True, "has_audio": True}, "This recording could not be played."),
])
async def test_an_unusable_recording_says_what_is_wrong_with_it(monkeypatch, tmp_path, probe_report, expected):
    source = tmp_path / "recording.mov"
    source.write_bytes(b"fixture")

    async def scope(*args):
        return str(tmp_path), "production/sources/test"

    async def source_path(*args):
        return "recording.mov", str(source)

    async def preflight():
        return "ffmpeg", "ffprobe"

    async def probe(*args):
        return probe_report

    monkeypatch.setattr(recording, "_scope", scope)
    monkeypatch.setattr(recording, "_source_path", source_path)
    monkeypatch.setattr(recording, "_runtime_preflight", preflight)
    monkeypatch.setattr(recording.media, "_probe_media_report", probe)
    result = json.loads(await recording._prepare_recording_handler(
        entity_id="entity", user_id="actor", workspace_id="workspace", source_path="recording.mov",
        output_directory="production/sources",
    ))
    assert result["code"] == "recording_preparation_failed"
    assert result["error"].startswith(expected)


async def test_a_long_recording_is_transcribed_in_pieces_cut_at_pauses(monkeypatch, tmp_path):
    """No length limit: 25 minutes of speech goes to STT in three pieces, timed back onto the recording."""
    source = tmp_path / "recording.mov"
    source.write_bytes(b"fixture")
    pieces, saved = [], {}

    async def scope(*args):
        return str(tmp_path), "production/sources/test"

    async def source_path(*args):
        return "recording.mov", str(source)

    async def preflight():
        return "ffmpeg", "ffprobe"

    async def probe(*args):
        return {"duration_seconds": 1500, "has_video": True, "has_audio": True}

    async def process(args, **kwargs):
        if any("silencedetect" in str(arg) for arg in args):
            # Pauses near each ten-minute mark, where the pieces should be cut.
            return "", ("silence_start: 597.5\nsilence_end: 598.5\n"
                        "silence_start: 1202.6\nsilence_end: 1203.4\n")
        if "-ss" in args:
            pieces.append((float(args[args.index("-ss") + 1]), float(args[args.index("-t") + 1])))
        return "", ""

    async def transcribe(**kwargs):
        return SimpleNamespace(segments=[{"start": 1.0, "end": 5.0, "text": f"Piece {len(pieces)}"}])

    async def save(ctx, rel, content, role):
        saved.update(json.loads(content))
        return {"document_id": "doc", "fs_path": rel, "file_size": 1, "sha256": "0" * 64}

    monkeypatch.setattr(recording, "_scope", scope)
    monkeypatch.setattr(recording, "_source_path", source_path)
    monkeypatch.setattr(recording, "_runtime_preflight", preflight)
    monkeypatch.setattr(recording, "_save", save)
    monkeypatch.setattr(recording.media, "_probe_media_report", probe)
    monkeypatch.setattr(recording.media, "_run_process", process)
    monkeypatch.setattr(recording.media, "_transcribe_narration_audio", transcribe)
    result = json.loads(await recording._prepare_recording_handler(
        entity_id="entity", user_id="actor", workspace_id="workspace", source_path="recording.mov",
        output_directory="production/sources",
    ))
    assert result["status"] == "completed", result
    assert pieces == [(0.0, 598.0), (598.0, 605.0), (1203.0, 297.0)]
    assert [(cue["start"], cue["end"], cue["text"]) for cue in saved["segments"]] == [
        (1.0, 5.0, "Piece 1"), (599.0, 603.0, "Piece 2"), (1204.0, 1208.0, "Piece 3"),
    ]
