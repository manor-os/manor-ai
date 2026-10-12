"""Real configuration parity and fail-closed, durable creative deliveries."""
from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image
import pytest

from packages.core.ai.tools import creative_studio_tools as studio
from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.workspace import Workspace
from packages.core.services.workflow_service import validate_workflow_steps
from scripts.blueprints.creative_studios import STUDIOS, configure_and_export, workflow_steps
from test_practical_workspace_blueprints import _checker
from test_knowledge_file_consistency import _doc_for, _seed_workspace, fs_enabled as _fs_enabled

fs_enabled = _fs_enabled

ROOT = Path(__file__).resolve().parents[1]


def test_dub_subtitle_end_uses_actual_audio_duration_without_rounding_up():
    assert studio._dub_caption_duration(56.042, 56.0073) == 56.007
    assert studio._dub_caption_duration(56.033, 56.042) == 56.033


def test_dub_burn_subtitles_use_actual_canvas_size():
    rendered = studio._estimated_dub_srt("真实字幕保持底部两行。" * 10, 56, subtitle_format="ass", width=1920, height=1080)
    assert "PlayResX: 1920" in rendered and "PlayResY: 1080" in rendered
    assert "Style: Default,Arial,42," in rendered


@pytest.mark.parametrize("text", [
    "欢迎来到 Cedar Cup。检查订单后，点击 Accept。准备好后点击 Ready，交给顾客。" * 5,
    "Welcome to Cedar Cup. Check the order, accept it, mark it ready and hand it off. " * 6,
])
def test_estimated_dub_subtitles_preserve_text_with_at_most_two_lines(text):
    result = studio._estimated_dub_srt(text, 56.0)
    blocks = result.strip().split("\n\n")
    assert len(blocks) > 1
    lines = [block.splitlines()[2:] for block in blocks]
    assert all(1 <= len(cue) <= 2 for cue in lines)
    assert all(len(line) <= 24 for cue in lines for line in cue)
    assert "".join("".join(line.split()) for cue in lines for line in cue) == "".join(text.split())
    assert blocks[-1].splitlines()[1].endswith("00:00:56,000")


@pytest.mark.parametrize("handler, arguments", [
    (studio._compose_creative_publication_handler, {"pages": [], "theme": "invalid"}),
    (studio._compose_creative_dub_handler, {"translated_text": ""}),
])
async def test_invalid_composition_inputs_fail_before_file_access(monkeypatch, handler, arguments):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid arguments must not read or create files")
    monkeypatch.setattr(studio, "_source_path", unexpected)
    result = json.loads(await handler(**arguments))
    assert result["status"] == "failed"


def test_publication_layout_executes_in_the_native_engine():
    import hashlib
    from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
    from packages.core.contracts.file_engine import normalize_file_patch_operation

    picture = io.BytesIO()
    Image.new("RGB", (400, 500), "#D5B296").save(picture, "PNG")
    digest = hashlib.sha256(picture.getvalue()).hexdigest()
    operations = studio._publication_operations(
        [{"title": f"Page {index}", "bullets": ["A useful first point.", "A second useful point."], "notes": "Notes"}
         for index in range(1, 6)], label="Test club", theme="terracotta",
        illustration={"path": "source.png", "expected_sha256": digest})
    result = _generate_office_operations_sync("pptx", [normalize_file_patch_operation(op) for op in operations],
        resources={("source.png", digest): picture.getvalue()})
    assert not result.get("error"), result
    from pptx import Presentation
    deck = Presentation(io.BytesIO(result["_persisted_bytes"]))
    assert len(deck.slides) == 5
    assert all(len(slide.shapes) == 5 for slide in deck.slides)
    assert all(shape.text_frame.word_wrap is True for slide in deck.slides
               for shape in slide.shapes if shape.has_text_frame)
    assert all("A useful first point." in "\n".join(shape.text for shape in slide.shapes if shape.has_text_frame)
               for slide in deck.slides)


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_brief_and_paths_are_multiline_in_the_live_chat_form(spec):
    start = workflow_steps(spec)[0]["config"]
    for field in start["run_inputs"]:
        assert field["schema"]["x-ui"]["control"] == "textarea"
        assert start["input_schema"]["properties"][field["key"]]["x-ui"]["control"] == "textarea"


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_multistep_production_has_a_bounded_twenty_minute_budget(spec):
    produce = next(step for step in workflow_steps(spec) if step["id"] == "produce")["config"]
    assert produce["timeout"] == 1200
    assert produce["max_rounds"] == 35
    assert produce["tools"] == list(spec.tools)


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_showcase_files_are_bundled_real_media_not_runtime_links(spec):
    payload = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{spec.slug}.json").read_text())
    assets = payload["manifest"]["showcase_assets"]
    assert payload["manifest"]["cover_image_url"] == assets[0]["url"]
    assert 2 <= len(assets) <= 6
    assert assets[0]["id"] == "concept-cover"
    for asset in assets[1:]:
        assert asset["url"].startswith("/blueprints/creative/")
        path = ROOT / "apps/web/public" / asset["url"].lstrip("/")
        assert path.is_file() and path.stat().st_size > 10000
        assert "Fictional non-Manor example" in asset["caption"]
        if asset["kind"] == "image":
            with Image.open(path) as actual:
                expected_size = (660, 998) if asset["url"] == "/blueprints/creative/dubbing-english-playback.jpg" else (1280, 720)
                assert actual.format == "JPEG" and actual.size == expected_size
                assert asset["mime_type"] == Image.MIME[actual.format]
                actual.verify()
        else:
            assert path.suffix == ".mp4" and path.read_bytes()[4:8] == b"ftyp"


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_configured_studio_export_install_roundtrip(db_session, spec, mode):
    source_entity = Entity(id=generate_ulid(), name="Source", plan_id="plan_enterprise",
        settings={"approval_defaults": account_defaults()})
    target_entity = Entity(id=generate_ulid(), name="Target", plan_id="plan_enterprise",
        settings={"approval_defaults": account_defaults()})
    db_session.add_all([source_entity, target_entity])
    await db_session.flush()
    source, payload = await configure_and_export(db_session, entity_id=source_entity.id, spec=spec)
    expected = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{spec.slug}.json").read_text())
    expected["policy"]["governance"] = {}
    assert payload == expected
    assert _checker("check_blueprint").check(payload) == []
    assert payload["recipe"]["workflows"][0]["steps"] == workflow_steps(spec)
    assert validate_workflow_steps(workflow_steps(spec))["valid"]
    for section in ("scheduled_jobs", "goals", "task_templates", "task_categories", "sla_policies"):
        assert not payload["recipe"].get(section)
    assert payload["contract"]["variables"] == payload["contract"]["channels"] == payload["contract"]["sessions"] == []
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


def test_registered_tools_have_scoped_write_authorization():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools import register_all_tools

    pool = ToolPool()
    register_all_tools(pool)
    for schema, _ in studio.get_tools():
        name = schema["function"]["name"]
        assert name in pool.registered_tool_names()
        classification = classify_runtime_tool(name, {"output_directory": "production/reviews"})
        assert classification.effect == RuntimeToolEffect.ACTION
        assert classification.action.action_key == "workspace.file.create"
        resources = WorkspaceFileAuthorizationResourceFactory.media_tool_resources(name, {"output_directory": "production/reviews"})
        assert len(resources) == 1 and resources[0].resource_id == "production/reviews"
        assert not WorkspaceFileAuthorizationResourceFactory.media_tool_resources(name, {})


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
async def test_invalid_brief_and_revision_routes(spec):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_approvals import _review_variable

    steps = {step["id"]: step for step in workflow_steps(spec)}
    run = WorkflowRun(id="run", entity_id="entity", workflow_id="flow", variables={"brief": " "}, step_results={})
    runner = WorkflowRunner()
    result = await runner._execute_step_safe(steps["start"], run, None)
    assert result["code"] == "input_schema_validation_failed"
    run.variables = {"result": {"status": "needs_input", "files": [], "summary": "Missing provider"}}
    assert (await runner._execute_step_safe(steps["ready"], run, None))["next_override"] == ["needs_input"]
    run.variables = {"acceptance": {"choice": "revise"}}
    assert (await runner._execute_step_safe(steps["accepted"], run, None))["next_override"] == ["revision"]
    assert _review_variable(steps["review"]["config"]) is None
    assert "publish_site" not in steps["produce"]["config"]["tools"]


async def _prepared(db_session, monkeypatch, tmp_path):
    entity, workspace = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity, workspace)
    kwargs = dict(entity_id=entity, workspace_id=workspace)
    result = json.loads(await studio._prepare_creative_studio_handler(
        **kwargs, mode="carousel", brief="Create a four-card fictional garden series.", output_directory="production/runs"))
    assert result["status"] == "completed", result
    ctx, runtime = studio._context(entity, "", workspace, None, None, None, {})
    return kwargs, result, ctx, runtime


async def test_dub_component_uses_returned_audio_path_and_actual_duration(db_session, fs_enabled, monkeypatch, tmp_path):
    kwargs, prepared, ctx, runtime = await _prepared(db_session, monkeypatch, tmp_path)
    directory = prepared["output_directory"]
    source = await studio._save(ctx, runtime, directory + "/source.mp4", b"mock media bytes")
    narration = await studio._save(ctx, runtime, directory + "/source.wav", b"mock narration bytes")
    monkeypatch.setattr(studio.shutil, "which", lambda _: "/test/ffmpeg")

    async def probe(_binary, path):
        return {"has_video": path.endswith(".mp4"), "has_audio": True,
                "video_stream": {"width": 1920, "height": 1080} if path.endswith(".mp4") else None,
                "duration_seconds": 56.042 if path.endswith(".mp4") else 56.0073}

    async def normalize(**arguments):
        # Real narration normalization may add the narrator's directory.
        output = str(Path(arguments["output_name"]).parent / "puck" / "narration.wav")
        return json.dumps({"status": "completed", **await studio._save(ctx, runtime, output, b"normalized fixture")})

    async def compose(**arguments):
        assert arguments["include_source_audio"] is False
        assert arguments["require_audio"] is arguments["burn_subtitles"] is True
        _, absolute = await studio._source_path(arguments["timeline_path"], ctx)
        timeline = json.loads(Path(absolute).read_text())
        assert timeline["audio_tracks"][0]["path"].endswith("/puck/narration.wav")
        assert arguments["subtitle_path"].endswith(".ass")
        _, absolute_srt = await studio._source_path(timeline["subtitles"]["srt_path"], ctx)
        assert "00:00:56,007" in Path(absolute_srt).read_text()
        return json.dumps({"status": "completed", **await studio._save(ctx, runtime, arguments["output_name"], b"mock MP4")})

    monkeypatch.setattr(studio.media, "_probe_media_report", probe)
    monkeypatch.setattr(studio.media, "_normalize_audio_loudness_handler", normalize)
    monkeypatch.setattr(studio.media, "_compose_video_timeline_handler", compose)
    result = json.loads(await studio._compose_creative_dub_handler(**kwargs,
        output_directory=directory, source_path=source["fs_path"], narration_path=narration["fs_path"],
        translated_text="欢迎来到 Cedar Cup。先核对订单，再开始准备。" * 5,
        translation_notes="Fictional test only. Original and translated text are test fixtures."))
    assert result["status"] == "completed", result
    assert len(result["files"]) == 3
    assert result["requires_playback_review"] is True
    assert all(path.startswith(directory + "/") for path in result["files"])


async def test_real_images_project_to_knowledge_and_verification_is_durable(db_session, fs_enabled, monkeypatch, tmp_path):
    kwargs, prepared, ctx, runtime = await _prepared(db_session, monkeypatch, tmp_path)
    files = []
    for index, color in enumerate(("red", "green", "blue")):
        data = io.BytesIO()
        Image.new("RGB", (512, 640), color).save(data, format="PNG")
        saved = await studio._save(ctx, runtime, prepared["output_directory"] + f"/page-{index}.png", data.getvalue())
        document = await _doc_for(db_session, kwargs["entity_id"], saved["name"])
        assert document.id == saved["document_id"]
        assert document.metadata_["origin"]["workspace_id"] == kwargs["workspace_id"]
        files.append(saved["fs_path"])
    notes = await studio._save(ctx, runtime, prepared["output_directory"] + "/captions.md", b"Garden cards: fictional unit-test images, not visual quality evidence.")
    files.append(notes["fs_path"])
    delivery = json.loads(await studio._verify_creative_studio_handler(**kwargs,
        snapshot_path=prepared["snapshot"]["fs_path"], snapshot_sha256=prepared["snapshot"]["sha256"],
        files=files, output_directory="production/reviews"))
    assert delivery["status"] == "completed", delivery
    assert delivery["technical_checks_passed"] is True
    receipt = await _doc_for(db_session, kwargs["entity_id"], "verification.json")
    assert receipt.id == delivery["receipt"]["document_id"]
    assert len(delivery["final_files"]) == 4


@pytest.mark.parametrize("failure", ["missing", "outside_run", "changed_brief", "filesystem_only", "duplicate", "bad_image"])
async def test_fake_missing_changed_or_unprojected_outputs_fail(db_session, fs_enabled, monkeypatch, tmp_path, failure):
    kwargs, prepared, ctx, runtime = await _prepared(db_session, monkeypatch, tmp_path)
    root = tmp_path / kwargs["entity_id"]
    path = prepared["output_directory"] + "/fake.png"
    files = [path]
    if failure == "outside_run":
        path = str(Path(prepared["output_directory"]).parent / "outside.png")
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(b"x")
        files = [path]
    elif failure == "changed_brief":
        (root / prepared["snapshot"]["fs_path"]).write_text("{}")
    elif failure in {"filesystem_only", "duplicate"}:
        Image.new("RGB", (512, 640)).save(root / path)
        if failure == "duplicate":
            files = [path, path]
    elif failure == "bad_image":
        await studio._save(ctx, runtime, path, b"not an image")
    result = json.loads(await studio._verify_creative_studio_handler(**kwargs,
        snapshot_path=prepared["snapshot"]["fs_path"], snapshot_sha256=prepared["snapshot"]["sha256"],
        files=files, output_directory="production/reviews"))
    assert result["status"] == "failed", result
    assert not result.get("technical_checks_passed")


async def test_prepare_enforces_runtime_read_acl_before_source_access(db_session, fs_enabled, monkeypatch, tmp_path):
    kwargs, prepared, ctx, runtime = await _prepared(db_session, monkeypatch, tmp_path)
    source = await studio._save(ctx, runtime, prepared["output_directory"] + "/private.md", b"Private source")

    async def deny(**_kwargs):
        return json.dumps({"error": "test read denied"})

    monkeypatch.setattr(studio.media, "runtime_guard_file_read_access", deny)
    result = json.loads(await studio._prepare_creative_studio_handler(**kwargs,
        _user_id_from_context="restricted-user", source_paths=source["fs_path"],
        mode="carousel", brief="Create a series from my sources.", output_directory="production/runs"))
    assert result["status"] == "failed" and "denied" in result["error"]


@pytest.mark.parametrize("mode", [s.key for s in STUDIOS])
def test_text_claim_cannot_meet_binary_contract(mode):
    with pytest.raises(ValueError):
        studio._check_package(mode, [{"fs_path": "notes.md", "kind": "document", "qa": {"characters": 100}}])


@pytest.mark.parametrize("page_count, valid", [(2, False), (3, True), (6, True), (7, False)])
def test_carousel_page_count_matches_the_three_to_six_page_contract(page_count, valid):
    inspected = [{"fs_path": f"page-{index}.png", "kind": "image", "sha256": str(index)}
                 for index in range(page_count)]
    inspected.append({"fs_path": "captions.md", "kind": "document"})
    if valid:
        studio._check_package("carousel", inspected)
    else:
        with pytest.raises(ValueError, match="3–6 distinct finished image pages"):
            studio._check_package("carousel", inspected)
