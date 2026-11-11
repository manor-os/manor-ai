"""Real configuration parity and fail-closed, durable creative deliveries."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import shutil

from PIL import Image
import pytest
from sqlalchemy import select

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


@pytest.mark.parametrize("text", [
    "Bienvenido a Clayhouse. En este breve recorrido te mostraré cómo reservar tu primera clase. Primero, abre el sitio.",
    "欢迎来到 Cedar Cup。检查订单后，点击 Accept。准备好后点击 Ready，交给顾客。",
])
def test_estimated_dub_subtitles_never_join_two_sentences_in_one_cue(text):
    cues = [" ".join(block.splitlines()[2:]) for block in studio._estimated_dub_srt(text, 30.0).strip().split("\n\n")]
    # A sentence end may only close a cue, never sit inside it.
    assert not any(re.search(r"[.。][^.。]", cue) for cue in cues)


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


def test_every_publication_layout_executes_in_the_native_engine():
    import hashlib
    from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
    from packages.core.contracts.file_engine import normalize_file_patch_operation

    def png(color):
        data = io.BytesIO()
        Image.new("RGB", (600, 400), color).save(data, "PNG")
        return data.getvalue(), hashlib.sha256(data.getvalue()).hexdigest()

    (hero, hero_digest), (portrait, portrait_digest) = png("#D5B296"), png("#6F8F7B")
    pages = [
        {"layout": "cover", "title": "Clayhouse Studio", "bullets": ["Pottery for beginners"], "notes": "n"},
        {"layout": "image", "image": "portrait.png", "title": "Why now", "bullets": ["Calm hobbies"], "notes": "n"},
        {"layout": "stats", "title": "Traction", "stats": [{"value": "120", "label": "students"},
                                                          {"value": "4.8", "label": "rating"}], "notes": "n"},
        {"layout": "statement", "title": "Classes repeat", "bullets": ["Guided making"], "notes": "n"},
        {"layout": "closing", "title": "The ask: $50k", "bullets": ["A second kiln"], "notes": "n"},
    ]
    operations = studio._publication_operations(
        pages, label="Clayhouse", theme="sage", illustration={"path": "hero.png", "expected_sha256": hero_digest},
        pictures={"portrait.png": {"path": "portrait.png", "expected_sha256": portrait_digest}})
    result = _generate_office_operations_sync("pptx", [normalize_file_patch_operation(op) for op in operations],
        resources={("hero.png", hero_digest): hero, ("portrait.png", portrait_digest): portrait})
    assert not result.get("error"), result
    from pptx import Presentation
    deck = Presentation(io.BytesIO(result["_persisted_bytes"]))
    texts = ["\n".join(shape.text for shape in slide.shapes if shape.has_text_frame) for slide in deck.slides]
    assert "120" in texts[2] and "4.8" in texts[2] and "The ask: $50k" in texts[4]
    pictures = [[shape.image.sha1 for shape in slide.shapes if shape.shape_type == 13] for slide in deck.slides]
    # The cover and closing use the hero, the image page its own picture; stats and statement have none.
    assert pictures[0] == pictures[4] and pictures[1] and pictures[1] != pictures[0]
    assert pictures[2] == pictures[3] == []
    # A picture named on a page that shows none is never opened (a live run named a bad path there).
    assert studio._page_pictures([{**pages[2], "image": "missing.png"}, pages[1]]) == ["portrait.png"]
    # Text never silently disappears: a cover keeps one subtitle line, stats need real numbers.
    for page in ({**pages[0], "bullets": ["one", "two"]}, {**pages[2], "stats": []}):
        with pytest.raises(ValueError, match="layout-density"):
            studio._validate_publication_layout([page, pages[1], pages[3]], label="Clayhouse")


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_brief_is_multiline_and_sources_are_picked_from_knowledge(spec):
    start = workflow_steps(spec)[0]["config"]
    controls = {field["key"]: field["schema"]["x-ui"]["control"] for field in start["run_inputs"]
                if "x-ui" in field["schema"]}
    # Only series studios pick the series a next episode continues (asked by that entry alone).
    assert controls == {"brief": "textarea", "sources": "documents",
                        **({"series_id": "workflow_project_select", "episode_id": "workflow_project_select"}
                           if spec.series else {})}
    # Only the presentation studio asks for a style (and is told the kind of deck), and only a series
    # studio is told when an entry starts or continues a series; see their own tests.
    assert ({field["key"] for field in start["run_inputs"]} - set(controls)) == (
        {"style", "deck_type"} if spec.key == "presentation" else {"series", "episode_code"} if spec.series else set())
    for key, control in controls.items():
        assert start["input_schema"]["properties"][key]["x-ui"]["control"] == control
    sources = next(field for field in start["run_inputs"] if field["key"] == "sources")
    # Only dubbing needs a file; its help is not shared with the other studios.
    assert sources["required"] == (spec.key == "dubbing")
    assert ("MP4" in sources["description"]) == (spec.key == "dubbing")


def test_a_deck_is_made_in_the_style_the_person_picks():
    """Every deck looked the same: the studio fixed one look and three accent colors.
    The person now picks one of the deck engine's designed styles, each deck type
    suggests the one that suits it, and production renders exactly that style."""
    from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS

    spec = next(spec for spec in STUDIOS if spec.key == "presentation")
    steps = workflow_steps(spec)
    style = next(field for field in steps[0]["config"]["run_inputs"] if field["key"] == "style")
    choices = style["schema"]
    assert style["required"] and style["default"] in choices["enum"]
    assert set(choices["enum"]) == set(studio.deck_styles())
    assert len(choices["x-enum-labels"]) == len(choices["x-enum-descriptions"]) == len(choices["enum"])
    for key, picture in zip(choices["enum"], choices["x-enum-images"]):
        # A picture of real slides in that style, shipped with the web app.
        assert picture == f"/blueprints/styles/deck/{key}.webp"
        with Image.open(ROOT / "apps/web/public" / picture.lstrip("/")) as image:
            assert image.width >= 640
    produce = next(step for step in steps if step["id"] == "produce")["config"]
    assert "compose_creative_deck" in produce["tools"] and "compose_creative_publication" not in produce["tools"]
    assert "{{style}}" in produce["input"]
    presets = BUILTIN_APP_VIEWS[spec.slug]["presets"]
    assert {preset["style"] for preset in presets.values()} <= set(choices["enum"])
    assert len({preset["style"] for preset in presets.values()}) == len(presets)


def test_each_kind_of_deck_offers_the_styles_that_suit_it_with_slides_of_that_kind():
    """A pitch deck's style choices showed a tea machine's product slides, all nine alike.
    Each deck entry now tells the flow its kind: the styles that suit it come first,
    pictured with sample slides of that kind, and the preselected style is the best of them."""
    from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS, DECK_TYPE_STYLES

    spec = next(spec for spec in STUDIOS if spec.key == "presentation")
    inputs = {field["key"]: field for field in workflow_steps(spec)[0]["config"]["run_inputs"]}
    kind = inputs["deck_type"]
    assert kind["hidden"] and not kind["required"] and kind["default"] == ""
    cases = inputs["style"]["schema"]["x-enum-cases"]
    assert cases["field"] == "deck_type"
    presets = BUILTIN_APP_VIEWS[spec.slug]["presets"]
    for variant, (deck_type, recommended) in DECK_TYPE_STYLES.items():
        preset = presets[f"action:{variant}"]
        assert preset["deck_type"] == deck_type and preset["style"] == recommended[0]
        assert cases["cases"][deck_type]["recommended"] == list(recommended)
        assert set(kind["schema"]["enum"]) >= {deck_type}
    assert set(cases["cases"]["pitch"]["images"]) == set(DECK_TYPE_STYLES["deck-pitch"][1])
    assert all(url.startswith("/blueprints/styles/deck/pitch/") for url in cases["cases"]["pitch"]["images"].values())


def test_a_podcast_series_is_planned_first_then_made_one_episode_at_a_time():
    """A series is one plan and its episodes in order, not unrelated one-offs: New series plans every
    episode and makes the first; Next episode reads the plan, makes the next and extends it on request.
    The series is a project holding the voice every episode reuses; each episode is a project in its folder."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS

    spec = next(spec for spec in STUDIOS if spec.key == "podcast")
    steps = {step["id"]: step for step in workflow_steps(spec)}
    inputs = {field["key"]: field for field in steps["start"]["config"]["run_inputs"]}
    series = inputs["series"]
    assert series["hidden"] and not series["required"] and series["default"] == ""
    assert steps["start"]["config"]["input_schema"]["properties"]["series"]["enum"] == ["", "new", "next"]
    presets = BUILTIN_APP_VIEWS[spec.slug]["presets"]
    assert presets["action:series-new"]["series"] == "new"
    assert presets["action:series-next"]["series"] == "next"
    assert all("series" not in preset for key, preset in presets.items() if not key.startswith("action:series-"))
    produce = steps["produce"]["config"]["input"]
    assert "{{series_context}}" in produce and "fixed_per_workspace" in produce

    # The next episode is what the series plan says: no brief check or Knowledge question first.
    assert steps["start"]["next"] == ["brief_route"]
    assert steps["brief_route"]["true_next"] == ["series_route"] and steps["brief_route"]["false_next"] == ["check_brief"]
    route = steps["series_route"]
    assert route["true_next"] == ["plan_series"] and steps["series_next_route"]["false_next"] == ["prepare"]
    series_project = steps["create_series"]["config"]
    assert series_project["project_type"] == "podcast_series" and series_project["folder"]["name"] == "{{series_plan.title}}"
    assert series_project["state"]["references"] == {"voices": {"narrator": {}}, "images": {}}
    # The Flow makes the series' key visual once (later episodes reuse it); every cover is made from it.
    key_art = steps["series_key_art"]["config"]
    assert key_art["args"]["workspace_asset_key"] == "series-key-art" and key_art["args"]["reuse_if_exists"] is True
    assert key_art["on_error"] == "continue" and steps["episode_output"]["next"] == ["series_key_art"]
    assert "reference_assets:['series-key-art']" in produce and "never make or name another" in produce
    assert steps["pick_series"]["config"]["input_schema"]["properties"]["series_id"]["x-ui"]["project_type"] == "podcast_series"
    # A series picked in the entry's form opens directly; a run started elsewhere asks once.
    assert inputs["series_id"]["schema"]["x-ui"]["control"] == "workflow_project_select" and inputs["series_id"]["hidden"]
    assert list(inputs)[0] == "series_id"
    assert steps["series_picked"]["true_next"] == ["get_series"] and steps["series_picked"]["false_next"] == ["pick_series"]
    assert steps["get_series"]["config"]["project_id"] == "{{series_id}}"
    assert BUILTIN_APP_VIEWS[spec.slug]["asks"] == {
        "action:series-next": ["series_id"], "action:series-remake": ["episode_id"],
        "action:series-make": ["series_id", "episode_code"],
    }
    # Remake names an episode already started or made; the run finds its series and code from it.
    assert inputs["episode_id"]["hidden"] and inputs["episode_id"]["schema"]["x-ui"] == {
        "control": "workflow_project_select", "project_type": "podcast_episode",
        "allowed_stages": ["in_production", "made"]}
    assert presets["action:series-remake"]["series"] == "next"
    # A planned episode's card opens Make this episode with its series and code;
    # neither it nor Remake, which each episode card opens, is a tile of its own.
    assert presets["action:series-make"]["series"] == "next"
    assert {"action:series-make", "action:series-remake"} <= set(BUILTIN_APP_VIEWS[spec.slug]["hidden"])
    assert inputs["episode_code"]["hidden"] and inputs["episode_code"]["schema"]["pattern"] == "^(E[0-9]{2,3})?$"
    assert steps["series_next_route"]["true_next"] == ["remake_route"]
    assert steps["remake_route"]["true_next"] == ["get_remake_episode"]
    assert steps["remake_route"]["false_next"] == ["series_picked"]
    assert steps["use_remake_episode"]["config"]["set"] == {
        "series_id": "{{remake_episode.state.parent_project_id}}", "episode_code": "{{remake_episode.state.code}}"}
    # The episode a run makes is a rule over the plan, not the model's pick: a
    # failed episode is made again before any later one, and asking for more
    # episodes never skips it.
    runner = WorkflowRunner.__new__(WorkflowRunner)
    plan = [{"code": "E01", "status": "made"}, {"code": "E02"}, {"code": "E03"}]
    picked = {code: runner._execute_filter_step(steps["pick_episode"], {
        "series_project": {"state": {"episodes": plan}}, "episode_code": code})["output"]
        for code in ("", "E01", "E09")}
    assert picked == {"": plan[1:], "E01": plan[:1], "E09": []}
    assert steps["remake_check"]["true_next"] == ["planned_route"]
    assert steps["extend_series"]["next"] == ["planned_route"]
    assert steps["planned_route"]["true_next"] == ["next_spec"]
    assert steps["planned_route"]["false_next"] == ["next_spec_new"]
    assert steps["next_spec"]["config"]["set"]["episode_spec"] == "{{planned_episodes.0}}"
    assert steps["next_spec_new"]["config"]["set"]["episode_spec"] == "{{next_episode.new_episodes.0}}"
    # Adding episodes keeps the premise in step: one that counts episodes says the new count.
    assert "premise" in steps["plan_next"]["config"]["output_schema"]["required"]
    assert steps["extend_series"]["config"]["patch"] == {"premise": "{{next_episode.premise}}"}
    episode = steps["create_episode"]["config"]
    assert episode["folder"]["under"] == "{{series_project.state.project_root}}"
    assert episode["state"]["parent_project_id"] == "{{series_project.project_id}}"
    # Making an episode again (after a failed run) reuses its project and folder.
    assert episode["operation"] == "claim"
    assert episode["project_key"] == "{{series_project.project_id}}:{{episode_spec.code}}"
    # A remake is made in a take of its own inside the episode's folder; the episode records it.
    assert steps["get_remake_episode"]["config"]["production"] == "take"
    assert steps["episode_output"]["config"]["set"]["output_root"] == "{{episode_project.production_root}}"
    assert steps["finish_episode"]["config"]["patch"] == {"production_root": "{{episode_project.production_root}}"}
    assert steps["prepare"]["config"]["args"]["output_directory"] == "{{output_root}}"
    # Accepting an episode marks it made in the series; a single episode records nothing.
    assert steps["accepted"]["true_next"] == ["is_series"] and steps["is_series"]["false_next"] == ["done"]
    assert steps["record_episode"]["config"]["list_upserts"][0]["item"]["status"] == "made"
    assert validate_workflow_steps(workflow_steps(spec))["valid"]


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_information_nobody_gave_is_left_out_never_a_placeholder(spec):
    """A Manor pitch deck stopped three times asking for company facts nobody had to hand.
    What the person does not fill in is left out of the work, not invented or marked as a gap."""
    steps = {step["id"]: step for step in workflow_steps(spec)}
    form = steps["needs_input"]["config"]["retry_state"]["editable_input_schema"]["properties"]
    assert set(form) == {"brief", "sources"}
    produce = steps["produce"]["config"]["input"]
    assert "{{brief_details}}" in produce and "left out" in produce and "never a placeholder" in produce
    assert "allow_placeholders" not in json.dumps(workflow_steps(spec))


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_a_brief_the_form_accepts_is_never_refused_when_the_run_freezes_it(spec):
    """'鬼吹灯' passed the form and failed at prepare_creative_studio, whose own schema wanted ten characters."""
    tool_brief = studio.PREPARE_SCHEMA["function"]["parameters"]["properties"]["brief"]
    steps = {step["id"]: step for step in workflow_steps(spec)}
    run_brief = steps["start"]["config"]["input_schema"]["properties"]["brief"]
    retry_brief = steps["needs_input"]["config"]["retry_state"]["editable_input_schema"]["properties"]["brief"]
    for schema in (run_brief, retry_brief):
        assert (schema["minLength"], schema["maxLength"]) == (tool_brief["minLength"], tool_brief["maxLength"])
    assert tool_brief["minLength"] <= len("鬼吹灯")


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_productions_that_state_facts_research_public_subjects_themselves(spec):
    """A podcast about a book series stopped asking for source files: production looks public subjects up."""
    produce = next(step for step in workflow_steps(spec) if step["id"] == "produce")["config"]
    researches = spec.key != "dubbing"  # a dub only translates its uploaded recording
    assert ({"search_documents", "web_search", "web_fetch"} <= set(produce["tools"])) is researches
    assert ("look it up yourself" in produce["input"]) is researches
    assert "never a reason to stop or ask" in produce["input"] or not researches


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


@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_gif_studio_configure_install_roundtrip(db_session, isolated_test_redis, mode):
    from scripts.blueprints import gif_studio

    entities = [Entity(id=generate_ulid(), name=name, plan_id="plan_enterprise",
                       settings={"approval_defaults": account_defaults()}) for name in ("GIF source", "GIF installer")]
    db_session.add_all(entities)
    await db_session.flush()
    source, payload = await gif_studio.configure_and_export(db_session, entity_id=entities[0].id)
    published = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{gif_studio.SLUG}.json").read_text())
    assert payload == published
    assert payload["embedded"]["agents"][0]["tool_bindings"] == list(gif_studio.AGENT_TOOLS)
    assert "generate_file" in payload["contract"]["requires"]["tools"]
    assert _checker("check_blueprint").check(payload) == []
    assert source.id not in json.dumps(payload) and entities[0].id not in json.dumps(payload)
    assert not payload["recipe"]["scheduled_jobs"] and not payload["recipe"].get("task_templates")
    assert not payload["recipe"]["workflows"]

    installed = await install_blueprint(db_session, entity_id=entities[1].id, payload=payload,
                                        mode=mode, blueprint_slug=gif_studio.SLUG)
    assert not installed.todos
    assert not installed.workflow_binding_ids
    target = await db_session.get(Workspace, installed.workspace_id)
    assert not target.heartbeat_enabled
    assert bool(target.settings.get("sandbox")) == (mode == InstallMode.SIMULATE)
    assert target.operating_model["app_view"]["hero"] == f"service:{gif_studio.SERVICE_KEY}"
    exported = await export_workspace(db_session, target.id, title=gif_studio.TITLE,
                                      context=ExportContext(include_knowledge_packs=False))
    assert _checker("compare_roundtrip").compare(payload, exported) == []
    checks = [item["payload"]["check"] for item in target.settings["_blueprint"]["live_setup_requirements"]
              if item["kind"] == "post_install_check"]
    assert checks == payload["policy"]["post_install_checks"]


def test_gif_style_cards_have_their_pictures_in_the_web_assets():
    from packages.core.blueprints.builtin_app_views import GIF_STYLES

    for _, _, picture, _ in GIF_STYLES:
        assert (ROOT / "apps/web/public" / picture.lstrip("/")).is_file(), picture


async def test_installed_gif_studio_agent_generates_retained_gifs_and_serves_original_bytes(
    client, db_session, isolated_test_redis, monkeypatch, tmp_path,
):
    """Only provider I/O is faked; the generate_file call the Agent makes, file writes, Documents and delivery are real."""
    import base64
    import httpx
    from auth_helpers import register_user_and_get_token
    from approval_settings_test_utils import action_rule, set_workspace_test_approval
    from packages.core.ai.tools import extended_tools, generate_file_tool
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from scripts.blueprints import gif_studio

    settings = get_settings()
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    account = (await register_user_and_get_token(client, {
        "username": "gif-studio-test", "email": "gif-studio@example.test",
        "password": "TestPassword123!", "entity_name": "GIF Studio test",
    })).json()
    entity_id, user_id = account["entity_id"], account["user_id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}
    payload = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{gif_studio.SLUG}.json").read_text())
    installed = await install_blueprint(db_session, entity_id=entity_id, user_id=user_id,
                                        payload=payload, mode=InstallMode.LIVE, blueprint_slug=gif_studio.SLUG)
    assert not installed.todos
    await set_workspace_test_approval(db_session, entity_id=entity_id,
        workspace_id=installed.workspace_id, actor_id=user_id,
        user_rules=[action_rule("workspace.file.create")])
    await db_session.commit()

    gif = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), color) for color in ("coral", "gold", "teal")]
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    gif_bytes = gif.getvalue()
    requests = []
    original_post = httpx.AsyncClient.post

    async def provider_post(self, url, *args, **kwargs):
        if str(url) != "https://api.openai.com/v1/images/generations":
            return await original_post(self, url, *args, **kwargs)
        requests.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "data": [{"b64_json": base64.b64encode(gif_bytes).decode()}],
            "usage": {"input_tokens": 20, "output_tokens": 100,
                      "input_tokens_details": {"text_tokens": 20, "image_tokens": 0}},
        })

    async def credentials(*args, **kwargs):
        return "sk-" + "test" * 10, "https://api.openai.com/v1", True

    monkeypatch.setattr(httpx.AsyncClient, "post", provider_post)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)

    async def agent_generates(version: int) -> str:
        # The arguments the Agent sends for one version, as its prompt specifies them.
        return await generate_file_tool._generate_file_handler(
            entity_id=entity_id, user_id=user_id, conversation_id="conversation",
            workspace_id=installed.workspace_id, kind="image",
            prompt="A coral cat waves and returns to its starting pose.",
            name=f"gifs/waving-cat-v{version}.gif", storage_scope="workspace",
            params={"model": gif_studio.MODEL, "output_format": "gif", "frame_count": 3,
                    "frame_duration_ms": 200, "size": "512x512", "quality": "medium",
                    "save_to_knowledge": True})

    receipts = []
    for version in (1, 2):
        result = json.loads(await agent_generates(version))
        doc = await db_session.get(Document, result["document_id"])
        assert result["output_format"] == "gif" and result["frame_count"] == 3, result
        assert doc.mime_type == "image/gif" and doc.file_type == "gif"
        assert doc.metadata_["origin"]["workspace_id"] == installed.workspace_id
        assert (tmp_path / entity_id / doc.fs_path).read_bytes() == gif_bytes
        for endpoint in ("preview/content", "download"):
            response = await client.get(f"/api/v1/documents/{doc.id}/{endpoint}", headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("image/gif")
            assert response.content == gif_bytes
        receipts.append((doc.id, doc.fs_path))
        await db_session.commit()

    assert len(requests) == 2
    assert all(r["model"] == gif_studio.MODEL.split("/", 1)[1] and r["output_format"] == "gif" for r in requests)
    assert receipts[0][0] != receipts[1][0] and receipts[0][1] != receipts[1][1]
    assert all((tmp_path / entity_id / path).read_bytes() == gif_bytes for _, path in receipts)


async def test_gif_studio_tool_refuses_a_static_image_as_a_gif(client, db_session, isolated_test_redis, monkeypatch, tmp_path):
    """A provider answer with one frame is not an animation: no Document is kept for the Studio."""
    import base64
    import httpx
    from auth_helpers import register_user_and_get_token
    from packages.core.ai.tools import extended_tools, generate_file_tool
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from scripts.blueprints import gif_studio

    settings = get_settings()
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    account = (await register_user_and_get_token(client, {
        "username": "gif-static-test", "email": "gif-static@example.test",
        "password": "TestPassword123!", "entity_name": "GIF static test",
    })).json()
    entity_id, user_id = account["entity_id"], account["user_id"]
    payload = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{gif_studio.SLUG}.json").read_text())
    installed = await install_blueprint(db_session, entity_id=entity_id, user_id=user_id,
                                        payload=payload, mode=InstallMode.LIVE, blueprint_slug=gif_studio.SLUG)
    await db_session.commit()

    still = io.BytesIO()
    Image.new("RGB", (32, 32), "coral").save(still, format="GIF")
    original_post = httpx.AsyncClient.post

    async def provider_post(self, url, *args, **kwargs):
        if str(url) != "https://api.openai.com/v1/images/generations":
            return await original_post(self, url, *args, **kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "data": [{"b64_json": base64.b64encode(still.getvalue()).decode()}],
            "usage": {"input_tokens": 20, "output_tokens": 100,
                      "input_tokens_details": {"text_tokens": 20, "image_tokens": 0}},
        })

    async def credentials(*args, **kwargs):
        return "sk-" + "test" * 10, "https://api.openai.com/v1", True

    monkeypatch.setattr(httpx.AsyncClient, "post", provider_post)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    result = json.loads(await generate_file_tool._generate_file_handler(
        entity_id=entity_id, user_id=user_id, conversation_id="conversation",
        workspace_id=installed.workspace_id, kind="image", prompt="A coral cat waves.",
        name="gifs/waving-cat-v1.gif", storage_scope="workspace",
        params={"model": gif_studio.MODEL, "output_format": "gif", "frame_count": 3, "frame_duration_ms": 200,
                "size": "512x512", "quality": "medium", "save_to_knowledge": True}))
    assert "error" in result and "document_id" not in result
    assert (await db_session.scalars(select(Document).where(
        Document.entity_id == entity_id))).all() == []


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


async def test_details_are_gathered_and_shown_for_checking_before_any_work():
    """A Manor pitch deck passed the brief check and stopped after production started,
    asking for company facts the person then had to type. The facts are now looked up
    first (files, Knowledge, the web) and shown filled in, one field each, for the
    person to check or complete before anything is produced."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    spec = next(spec for spec in STUDIOS if spec.key == "presentation")
    steps = {step["id"]: step for step in workflow_steps(spec)}
    check = steps["check_brief"]["config"]
    assert {"search_documents", "web_search", "web_fetch"} <= set(check["tools"])
    assert "{label, hint, value, source}" in check["input"] and "never guess" in check["input"]
    assert "left blank is left out" in steps["complete_brief"]["config"]["message"]
    picked = [{"document_id": "doc-1"}]
    runner = WorkflowRunner()
    run = WorkflowRun(id="run", entity_id="entity", workflow_id="flow", step_results={}, variables={
        "brief": "Pitch deck for Manor.", "sources": picked, "brief_check": {"status": "confirm", "details": [
            {"label": "Market size and source", "hint": "TAM/SAM with where the numbers come from"},
            {"label": "Team", "value": "Ada (CEO)", "source": "manor.example"}]}})
    paused = await runner._execute_step_safe(steps["complete_brief"], run, None)
    form = paused["input_schema"]["properties"]
    assert paused["input_schema"]["x-ui"]["order"] == ["details", "files"]
    asked = form["details"]["properties"]
    assert list(asked) == ["Market size and source", "Team"]
    assert asked["Market size and source"]["x-ui"]["placeholder"].startswith("TAM/SAM")
    assert asked["Team"]["default"] == "Ada (CEO)" and asked["Team"]["x-ui"]["source"] == "manor.example"
    assert set(form) == {"details", "files"} and form["files"]["default"] == picked
    # The person corrects what was found; the blank item is not passed on.
    run.variables = {**run.variables, "brief_answer": {"details": {"Team": "Ada (CEO), Lin (CTO)",
                                                                   "Market size and source": " "}}}
    answered = await runner._execute_step_safe(steps["complete_brief"], run, None)
    assert answered["status"] == "completed", answered
    run.variables = {**run.variables, "brief_answer": answered["output"]}
    await runner._execute_step_safe(steps["use_brief"], run, None)
    assert run.variables["brief_details"] == {"Team": "Ada (CEO), Lin (CTO)"}
    assert run.variables["sources"] == picked
    assert "{{brief_details}}" in steps["produce"]["config"]["input"]
    # Only the items asked about are accepted.
    run.variables = {**run.variables, "brief_answer": {"details": {"Pricing": "$20"}}}
    assert (await runner._execute_step_safe(steps["complete_brief"], run, None))["status"] == "paused"
    # Nothing filled in still goes on: what is missing is left out.
    run.variables = {**run.variables, "brief_answer": {"details": {}, "files": []}}
    assert (await runner._execute_step_safe(steps["complete_brief"], run, None))["status"] == "completed"
    await runner._execute_step_safe(steps["use_brief"], run, None)
    assert run.variables["brief_details"] == {} and run.variables["sources"] == []
    assert steps["use_brief"]["next"] == ["has_sources"]


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
    # A change request re-freezes and produces a new version through the shared
    # approval revision contract; the graph itself stays acyclic.
    review = steps["review"]["config"]
    assert (review["revise_from"], review["revision_history_variable"]) == ("prepare", "revision_notes")
    assert "{{revision_notes}}" in steps["produce"]["config"]["input"]
    from packages.core.ai.workflow_runner import apply_workflow_approval_revision
    completed = {"status": "completed"}
    run.variables = {"acceptance": {"choice": "revise", "note": "Brighter cover"}, "revision_notes": []}
    run.step_results = {step_id: completed for step_id in ("start", "prepare", "produce", "ready", "verify", "review")}
    assert apply_workflow_approval_revision(list(steps.values()), run) == "review"
    assert {"prepare", "produce", "verify", "review"}.isdisjoint(run.step_results)
    assert run.variables["revision_notes"] == ["Brighter cover"]
    assert _review_variable(steps["review"]["config"]) is None
    assert "publish_site" not in steps["produce"]["config"]["tools"]


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
async def test_missing_essentials_are_asked_for_before_any_work(spec):
    """Blanks such as [event] are gathered or asked about right after the brief, not after files are made."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    steps = {step["id"]: step for step in workflow_steps(spec)}
    after_brief = "prepare" if spec.key == "dubbing" else "has_sources"
    # A podcast's next series episode skips this (its plan is the brief); every other run checks.
    first = steps["start"]["next"][0]
    assert first == "check_brief" or steps[first]["false_next"] == ["check_brief"]
    check = steps["check_brief"]["config"]
    assert "[event]" in check["input"] and "sensible defaults" in check["input"]
    runner = WorkflowRunner()
    run = WorkflowRun(id="run", entity_id="entity", workflow_id="flow", step_results={}, variables={
        "brief": "Event announcement carousel for [event], for [audience].",
        "brief_check": {"status": "confirm", "details": [{"label": "Event"}, {"label": "Audience"}]}})
    assert (await runner._execute_step_safe(steps["brief_ready"], run, None))["next_override"] == ["complete_brief"]
    paused = await runner._execute_step_safe(steps["complete_brief"], run, None)
    assert paused["status"] == "paused"
    assert list(paused["input_schema"]["properties"]["details"]["properties"]) == ["Event", "Audience"]
    answer = {"Event": "Open Studio Night", "Audience": "Beginners"}
    run.variables = {**run.variables, "brief_answer": {"details": answer}}
    assert (await runner._execute_step_safe(steps["complete_brief"], run, None))["status"] == "completed"
    await runner._execute_step_safe(steps["use_brief"], run, None)
    assert run.variables["brief_details"] == answer
    assert steps["use_brief"]["next"] == [after_brief]
    # A complete brief goes straight on to the work.
    run.variables = {**run.variables, "brief_check": {"status": "ready", "details": []}}
    assert (await runner._execute_step_safe(steps["brief_ready"], run, None))["next_override"] == [after_brief]


@pytest.mark.parametrize("spec", [spec for spec in STUDIOS if spec.key != "dubbing"], ids=lambda spec: spec.key)
async def test_without_picked_files_the_flow_offers_what_knowledge_found(spec):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    steps = {step["id"]: step for step in workflow_steps(spec)}
    runner = WorkflowRunner()
    found = [{"document_id": "doc-1", "name": "notes.md"}, {"document_id": "doc-2", "name": "report.pdf"}]
    run = WorkflowRun(id="run", entity_id="entity", workflow_id="flow", step_results={},
                      variables={"brief": "A short guide to the workshop", "sources": [], "found": {"sources": found}})
    assert (await runner._execute_step_safe(steps["has_sources"], run, None))["next_override"] == ["find_sources"]
    assert (await runner._execute_step_safe(steps["found_sources"], run, None))["next_override"] == ["confirm_sources"]
    paused = await runner._execute_step_safe(steps["confirm_sources"], run, None)
    assert paused["status"] == "paused"
    assert paused["input_schema"]["properties"]["picked"]["default"] == found
    run.variables = {**run.variables, "source_choice": {"picked": found[:1]}}
    assert (await runner._execute_step_safe(steps["confirm_sources"], run, None))["status"] == "completed"
    await runner._execute_step_safe(steps["use_sources"], run, None)
    assert run.variables["sources"] == found[:1]
    # Removing every file is an answer: work from the brief alone.
    run.variables = {**run.variables, "source_choice": {"picked": []}}
    assert (await runner._execute_step_safe(steps["confirm_sources"], run, None))["status"] == "completed"
    # A series studio first settles which series, if any, the episode belongs to.
    work = "series_route" if spec.series else "prepare"
    run.variables = {**run.variables, "found": {"sources": []}}
    assert (await runner._execute_step_safe(steps["found_sources"], run, None))["next_override"] == [work]
    run.variables = {**run.variables, "sources": found}
    assert (await runner._execute_step_safe(steps["has_sources"], run, None))["next_override"] == [work]


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


def _deck(picture: str, *, layout: str = "icon_grid") -> dict:
    return {"title": "Fresh tea in 60 seconds", "brand": "Cedar Cup", "slides": [
        {"layout": "cover", "title": "Fresh tea in {60 seconds}", "subtitle": "A fictional test deck",
         "image": picture, "notes": "Open with the promise."},
        {"layout": layout, "title": "Built for the lunch rush", "items": [
            {"icon": "clock", "title": "Fast", "text": "One cup a minute"},
            {"icon": "shield", "title": "Consistent", "text": "Same recipe every cup"},
            {"icon": "settings", "title": "Simple", "text": "Three buttons"}], "notes": "Three reasons."},
        {"layout": "process", "title": "How it works", "steps": [
            {"title": "Load tea"}, {"title": "Pick a recipe"}, {"title": "Serve"}], "notes": "Three steps."},
        {"layout": "closing", "title": "Book a {free trial}", "subtitle": "Two weeks in your kitchen",
         "notes": "The ask."},
    ]}


async def _deck_production(db_session, monkeypatch, tmp_path, mode="presentation"):
    """A prepared run folder holding one real picture. Preparing a presentation needs the Office renderer."""
    entity, workspace = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity, workspace)
    kwargs = dict(entity_id=entity, workspace_id=workspace)
    prepared = json.loads(await studio._prepare_creative_studio_handler(
        **kwargs, mode=mode, brief="A fictional four-slide product introduction.",
        output_directory="production/runs"))
    assert prepared["status"] == "completed", prepared
    ctx, runtime = studio._context(entity, "", workspace, None, None, None, {})
    data = io.BytesIO()
    Image.new("RGB", (900, 600), "#3A5A7A").save(data, format="PNG")
    picture = await studio._save(ctx, runtime, prepared["output_directory"] + "/cover.png", data.getvalue())
    return kwargs, prepared, picture["fs_path"]


@pytest.mark.skipif(not (shutil.which("soffice") and shutil.which("pdftoppm")), reason="needs the Office renderer")
async def test_a_styled_deck_is_built_by_the_deck_engine_and_passes_verification(db_session, fs_enabled, monkeypatch, tmp_path):
    kwargs, prepared, picture = await _deck_production(db_session, monkeypatch, tmp_path)
    result = json.loads(await studio._compose_creative_deck_handler(**kwargs,
        output_directory=prepared["output_directory"], style="consulting", deck=_deck(picture),
        source_notes="Fictional unit-test deck; no real product claims."))
    assert result["status"] == "completed", result
    assert result["style"] == "consulting" and result["pages"] == 4
    assert [Path(path).name for path in result["files"]] == ["deck.pptx", "deck.pdf", "notes.md"]
    # The Studio's own gate opens the files: one PPTX and a PDF of the same pages, with a real picture.
    delivery = json.loads(await studio._verify_creative_studio_handler(**kwargs,
        snapshot_path=prepared["snapshot"]["fs_path"], snapshot_sha256=prepared["snapshot"]["sha256"],
        files=result["files"], output_directory="production/reviews"))
    assert delivery["status"] == "completed", delivery


async def test_a_deck_the_engine_cannot_lay_out_returns_its_problems_and_writes_nothing(db_session, fs_enabled, monkeypatch, tmp_path):
    # Any run folder will do: the engine rejects the deck before anything is converted or written.
    kwargs, prepared, picture = await _deck_production(db_session, monkeypatch, tmp_path, mode="carousel")
    saved = []
    real_save = studio._save

    async def recording_save(*args, **kwargs_):
        saved.append(args[2])
        return await real_save(*args, **kwargs_)

    monkeypatch.setattr(studio, "_save", recording_save)
    result = json.loads(await studio._compose_creative_deck_handler(**kwargs,
        output_directory=prepared["output_directory"], style="mono", deck=_deck(picture, layout="no_such_layout"),
        source_notes="Fictional unit-test deck."))
    assert result["status"] == "failed"
    assert result["problems"], result
    assert saved == []


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


@pytest.mark.asyncio
async def test_verified_files_are_the_results_and_the_rest_how_they_were_made(db_session, monkeypatch):
    """The App lists results by artifact role: a brief snapshot or raw narration is never shown as an output."""
    from contextlib import asynccontextmanager
    from packages.core.models.document import Document
    from packages.core.services.generated_artifact_service import mark_production_results
    import packages.core.database as database

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(database, "async_session", session)
    entity = generate_ulid()  # Other tests commit Documents too.
    folder = "Workspaces/_by_id/f1/projects/ghost/e01/v1"
    names = ["episode.mp3", "script.md", "narration.mp3", "brief.json"]
    # Another version's files are not this production's, nor are the takes
    # made again inside its folder.
    for path in [*(f"{folder}/{name}" for name in names), "Workspaces/_by_id/f1/projects/ghost/e01/v2/narration.mp3",
                 f"{folder}/takes/01TAKE/episode.mp3"]:
        db_session.add(Document(entity_id=entity, name=path.rsplit("/", 1)[1], file_type="x", source="generated",
                                fs_path=path, metadata_={"artifact": {"role": "final"}}))
    await db_session.commit()

    await mark_production_results(entity, folder, {f"{folder}/episode.mp3", f"{folder}/script.md"})

    roles = {doc.fs_path.removeprefix("Workspaces/_by_id/f1/projects/ghost/e01/"): doc.metadata_["artifact"]["role"]
             for doc in (await db_session.execute(select(Document))).scalars() if doc.entity_id == entity}
    delivered = {doc.fs_path.rsplit("/", 2)[-2] + "/" + doc.name for doc in (await db_session.execute(select(Document))).scalars()
                 if doc.entity_id == entity and doc.metadata_["artifact"].get("delivered")}
    assert delivered == {"v1/episode.mp3", "v1/script.md"}
    assert roles == {"v1/episode.mp3": "final", "v1/script.md": "final", "v1/narration.mp3": "intermediate",
                     "v1/brief.json": "intermediate", "v2/narration.mp3": "final",
                     "v1/takes/01TAKE/episode.mp3": "final"}


def test_vertical_episodes_get_larger_captions_set_higher():
    vertical = studio._estimated_dub_srt("先揉面。再静置。", 4.0, subtitle_format="ass", width=1080, height=1920)
    assert "Style: Default,Arial,60," in vertical
    landscape = studio._estimated_dub_srt("先揉面。再静置。", 4.0, subtitle_format="ass", width=1920, height=1080)
    assert "Style: Default,Arial,42," in landscape


def test_each_scene_shows_for_its_share_of_the_narration():
    assert studio._scene_seconds(["一二三", "四五六七八九"], 9.0) == [3.0, 6.0]
    with pytest.raises(ValueError, match="under a second"):
        studio._scene_seconds(["短", "这一段很长很长很长很长很长很长很长很长很长很长很长很长"], 10.0)


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs FFmpeg")
async def test_the_picture_track_shows_every_still_for_its_seconds(tmp_path):
    pictures = []
    for color in ("red", "blue"):
        path = tmp_path / f"{color}.png"
        Image.new("RGB", (400, 300), color).save(path)
        pictures.append(str(path))
    output = str(tmp_path / "picture.mp4")
    await studio.media._run_process(
        studio._picture_master_args(shutil.which("ffmpeg"), pictures, [1.0, 1.5], 180, 320, output), timeout_seconds=120)
    report = await studio.media._probe_media_report(shutil.which("ffprobe"), output)
    assert report["has_video"] and not report["has_audio"]
    assert report["video_stream"]["width"] == 180 and report["video_stream"]["height"] == 320
    assert abs(report["duration_seconds"] - 2.5) < 0.1


async def test_video_component_narrates_the_scenes_in_one_subtitled_episode(db_session, fs_enabled, monkeypatch, tmp_path):
    kwargs, prepared, ctx, runtime = await _prepared(db_session, monkeypatch, tmp_path)
    directory = prepared["output_directory"]
    scenes = []
    for index, color in enumerate(("red", "green", "blue")):
        data = io.BytesIO()
        Image.new("RGB", (540, 960), color).save(data, format="PNG")
        saved = await studio._save(ctx, runtime, f"{directory}/scene-{index}.png", data.getvalue())
        scenes.append({"image_path": saved["fs_path"], "text": f"第{index + 1}幕：先揉面，再静置，最后烘烤。"})
    narration = await studio._save(ctx, runtime, directory + "/narration.mp3", b"mock narration bytes")
    monkeypatch.setattr(studio.shutil, "which", lambda _: "/test/ffmpeg")
    rendered_master: dict = {}

    async def probe(_binary, path):
        return {"has_audio": True, "has_video": False, "duration_seconds": 30.4}

    async def normalize(**arguments):
        assert "target_duration_seconds" not in arguments  # The narration sets the length.
        return json.dumps({"status": "completed", **await studio._save(ctx, runtime, arguments["output_name"], b"wav")})

    async def run(args, *, timeout_seconds):
        rendered_master["inputs"] = [args[index + 1] for index, value in enumerate(args) if value == "-i"]
        Path(args[-1]).write_bytes(b"mock picture track")
        return "", ""

    async def subtitles(**_arguments):
        return json.dumps({"verdict": "pass"})

    async def compose(**arguments):
        assert arguments["include_source_audio"] is False and arguments["burn_subtitles"] is True
        assert arguments["clean_video_path"].endswith("/picture.mp4")
        _, absolute = await studio._source_path(arguments["timeline_path"], ctx)
        timeline = json.loads(Path(absolute).read_text())
        assert timeline["duration"] == 30.4 and timeline["audio_tracks"][0]["path"].endswith("/narration.wav")
        return json.dumps({"status": "completed", **await studio._save(ctx, runtime, arguments["output_name"], b"mp4")})

    monkeypatch.setattr(studio.media, "_probe_media_report", probe)
    monkeypatch.setattr(studio.media, "_normalize_audio_loudness_handler", normalize)
    monkeypatch.setattr(studio.media, "_run_process", run)
    monkeypatch.setattr(studio.media, "_validate_subtitles_handler", subtitles)
    monkeypatch.setattr(studio.media, "_compose_video_timeline_handler", compose)
    result = json.loads(await studio._compose_creative_video_handler(**kwargs,
        output_directory=directory, aspect_ratio="9:16", narration_path=narration["fs_path"], scenes=scenes))

    assert result["status"] == "completed", result
    assert [Path(path).name for path in result["files"]] == ["episode.mp4", "subtitles.srt"]
    assert len(rendered_master["inputs"]) == 3 and result["scenes"] == 3


async def test_a_series_intro_plays_before_the_episode_and_its_subtitles_follow(db_session, fs_enabled, monkeypatch, tmp_path):
    kwargs, prepared, ctx, runtime = await _prepared(db_session, monkeypatch, tmp_path)
    directory = prepared["output_directory"]
    scenes = []
    for index, color in enumerate(("red", "blue")):
        data = io.BytesIO()
        Image.new("RGB", (540, 960), color).save(data, format="PNG")
        saved = await studio._save(ctx, runtime, f"{directory}/scene-{index}.png", data.getvalue())
        scenes.append({"image_path": saved["fs_path"], "text": f"第{index + 1}幕：先揉面，再静置。"})
    narration = await studio._save(ctx, runtime, directory + "/narration.mp3", b"mock narration bytes")
    intro = await studio._save(ctx, runtime, directory + "/series-intro.mp4", b"mock intro")
    monkeypatch.setattr(studio.shutil, "which", lambda _: "/test/ffmpeg")
    calls: dict = {}

    async def probe(_binary, path):
        return {"has_audio": True, "duration_seconds": 3.0 if path.endswith("series-intro.mp4") else 20.0}

    async def save_output(**arguments):
        return json.dumps({"status": "completed", **await studio._save(ctx, runtime, arguments["output_name"], b"x")})

    async def run(args, *, timeout_seconds):
        Path(args[-1]).write_bytes(b"mock picture track")
        return "", ""

    async def merge(**arguments):
        calls["merge"] = arguments
        return await save_output(**arguments)

    async def subtitles(**arguments):
        _, absolute = await studio._source_path(arguments["subtitle_path"], ctx)
        calls["srt"], calls["checked_against"] = Path(absolute).read_text(), arguments["media_path"]
        return json.dumps({"verdict": "pass"})

    monkeypatch.setattr(studio.media, "_probe_media_report", probe)
    monkeypatch.setattr(studio.media, "_normalize_audio_loudness_handler", save_output)
    monkeypatch.setattr(studio.media, "_run_process", run)
    monkeypatch.setattr(studio.media, "_compose_video_timeline_handler", save_output)
    monkeypatch.setattr(studio.media, "_merge_videos_handler", merge)
    monkeypatch.setattr(studio.media, "_validate_subtitles_handler", subtitles)
    result = json.loads(await studio._compose_creative_video_handler(**kwargs, output_directory=directory,
        aspect_ratio="9:16", narration_path=narration["fs_path"], scenes=scenes, intro_path=intro["fs_path"]))

    assert result["status"] == "completed", result
    assert [Path(path).name for path in result["files"]] == ["episode.mp4", "subtitles.srt"]
    # The narrated part is composed first; the intro plays before it, keeping the narration's audio.
    assert [Path(path).name for path in calls["merge"]["paths"]] == ["series-intro.mp4", "episode-body.mp4"]
    assert calls["merge"]["include_source_audio"] is True
    assert result["duration_seconds"] == 23.0 and result["intro_seconds"] == 3.0
    # The delivered subtitles start after the intro, checked against the finished episode.
    assert "00:00:03,000 -->" in calls["srt"] and calls["checked_against"].endswith("/episode.mp4")


def test_a_video_series_keeps_one_voice_look_and_host_for_every_episode():
    """Episodes of a video series share the series' narrator, key visual and host picture."""
    spec = next(spec for spec in STUDIOS if spec.key == "video")
    steps = {step["id"]: step for step in workflow_steps(spec)}
    assert spec.series.series_type == "video_series" and "compose_creative_video" in spec.tools
    series_state = steps["create_series"]["config"]["state"]
    assert series_state["references"] == {"voices": {"narrator": {}}, "images": {}}
    assert series_state["host"] == "{{series_plan.host}}" and "host" in steps["plan_series"]["config"]["output_schema"]["properties"]
    # The Flow keeps the host picture once, only when the series has a host, and the key visual shows that host;
    # scenes reference both by name.
    assert steps["episode_output"]["next"] == ["has_host"]
    assert steps["has_host"]["true_next"] == ["series_host"] and steps["has_host"]["false_next"] == ["series_key_art"]
    host = steps["series_host"]["config"]
    assert host["args"]["workspace_asset_key"] == "series-host" and host["args"]["reuse_if_exists"] is True
    assert steps["series_host"]["next"] == ["series_key_art_with_host"]
    with_host = steps["series_key_art_with_host"]["config"]["args"]
    assert with_host["reference_assets"] == ["series-host"] and with_host["workspace_asset_key"] == "series-key-art"
    assert with_host["name"].endswith("/references/series-key-art.png")
    produce = steps["produce"]["config"]["input"]
    assert "'series-host'" in produce and "compose_creative_video" in produce
    # Every episode opens with the series intro, made once from the key visual and title and kept by the series.
    assert steps["series_key_art"]["next"] == steps["series_key_art_with_host"]["next"] == ["has_intro"]
    assert steps["has_intro"]["true_next"] == ["use_intro"] and steps["has_intro"]["false_next"] == ["series_intro"]
    intro = steps["series_intro"]["config"]
    assert intro["tool"] == "still_to_video" and intro["on_error"] == "continue"
    assert intro["args"] | {} == {**intro["args"], "input_path": "{{series_key_art.fs_path}}", "aspect_ratio": "9:16",
                                  "motion": "push_in", "title": "{{series_project.state.title}}"}
    assert steps["keep_intro"]["config"]["patch"] == {"intro_path": "{{series_intro.fs_path}}"}
    assert "intro_path" in produce and "{{series_intro_path}}" in produce
    # Keeping the intro made the series the run's project; production goes back to the episode's folder.
    assert steps["use_intro"]["next"] == ["reopen_episode"] and steps["reopen_episode"]["next"] == ["prepare"]
    assert steps["reopen_episode"]["config"]["project_id"] == "{{episode_project.project_id}}"
    # A podcast has no picture, so no intro.
    podcast = {step["id"] for step in workflow_steps(next(spec for spec in STUDIOS if spec.key == "podcast"))}
    assert "series_intro" not in podcast
