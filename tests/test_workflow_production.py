"""A Workflow's declared production: what its runs show as stages and units."""

import pytest

from packages.core.services.workflow_production import (
    normalize_workflow_production,
    workflow_run_production,
)


PRODUCTION = {
    "unit": {"name": "Scenes", "i18n": {"zh": {"name": "镜头"}}},
    "units_from": "script_packet.blocks",
    "final_from": "__result.state.artifact",
    "unit_fields": {"id": "block_id", "title": "prop", "duration": "duration_seconds"},
    "lanes": [{"key": "narration", "label": "Narration", "from": "narration"}],
    "stages": [
        {"label": "Script", "steps": ["write_script"]},
        {"label": "Scenes", "i18n": {"zh": {"label": "镜头制作"}}, "steps": ["make"], "counts_units": True, "weight": 3},
    ],
}
STEPS = [{"id": "start", "type": "trigger", "config": {"run_inputs": [], "production": PRODUCTION}},
         {"id": "write_script", "type": "agent", "config": {}}]


def test_a_run_shows_its_planned_units_with_their_lanes_in_the_viewers_language():
    variables = {"script_packet": {"blocks": [
        {"block_id": "block_001", "prop": "An hourglass", "duration_seconds": 10, "narration": "Time is gone."},
        {"block_id": "block_002", "prop": "A sign", "duration_seconds": "ten", "narration": "Choose again?"},
    ]}}
    view = workflow_run_production(STEPS, variables, locale="zh")
    assert view["media"] == "video"
    assert view["unit"] == {"name": "镜头"}
    assert [stage["label"] for stage in view["stages"]] == ["Script", "镜头制作"]
    assert view["stages"][1] == {"label": "镜头制作", "steps": ["make"], "counts_units": True, "weight": 3}
    assert view["units"] == [
        {"id": "block_001", "title": "An hourglass", "duration_s": 10, "lanes": {"narration": "Time is gone."}},
        # A duration that is not a number is left out rather than guessed.
        {"id": "block_002", "title": "A sign", "duration_s": None, "lanes": {"narration": "Choose again?"}},
    ]
    # Before the plan exists the stages still show, with no units and no finished video yet.
    assert workflow_run_production(STEPS, {}, locale="en")["units"] == []
    assert "final" not in view
    # Once saved, the finished whole is what the view leads with.
    done = workflow_run_production(STEPS, {**variables, "__result": {"state": {"artifact": {
        "document_id": "01DOC", "filename": "stickman-video.mp4", "fs_path": "Workspaces/x/final.mp4"}}}})
    assert done["final"] == {"document_id": "01DOC", "name": "stickman-video.mp4"}
    # A Workflow without production keeps the plain run view.
    assert workflow_run_production([{"id": "start", "type": "trigger", "config": {}}], variables) is None


def test_a_retry_knows_which_units_earlier_attempts_already_made():
    from packages.core.services.workflow_production import production_progress, production_unit_steps

    config = normalize_workflow_production(PRODUCTION)
    assert production_unit_steps(config) == {"make"}
    variables = {"script_packet": {"blocks": [
        {"block_id": "block_001", "prop": "An hourglass"},
        {"block_id": "block_002", "prop": "A sign"},
        {"block_id": "block_003", "prop": "A door"},
    ]}}
    clip = lambda job_id, name, status="completed", at="1", **extra: {  # noqa: E731
        "id": job_id, "status": status, "kind": "video", "output_name": name, "created_at": at,
        "result_url": f"/api/v1/fs/e/{name}", **extra}
    progress = production_progress(config, variables, [
        clip("j1", "stickman-block_001-r1.mp4", at="1"),
        clip("j2", "stickman-block_001-r2.mp4", at="2"),             # the newest finished clip of a unit wins
        clip("j3", "stickman-block_002.mp4", status="failed"),      # a failed take made nothing
        clip("j4", "anything.mp4", unit_id="block_003", kind="image"),  # another kind of media is not a scene
    ])
    assert [(unit["unit_id"], unit["clip"]["job_id"]) for unit in progress["units_made"]] == [("block_001", "j2")]
    assert progress["units_to_make"] == [{"unit_id": "block_002", "title": "A sign"},
                                         {"unit_id": "block_003", "title": "A door"}]
    # A recorded unit id, a whole id in the saved name, or a bare unit number all name the unit; a take number does not.
    from packages.core.services.workflow_production import clip_unit_id
    shots = ["S01", "S05", "S12"]
    assert clip_unit_id({"output_name": "song-S05-take1.mp4"}, shots) == "S05"
    assert clip_unit_id({"output_name": "shot-12.mp4"}, shots) == "S12"
    assert clip_unit_id({"unit_id": "S01", "output_name": "x.mp4"}, shots) == "S01"
    assert clip_unit_id({"output_name": "S015-take1.mp4"}, shots) is None
    # Without a plan there is nothing to count.
    assert production_progress(config, {}, []) is None
    # The retried step is told what is made and what is left, so it pays only for the rest.
    from packages.core.ai.workflow_runner import _workflow_agent_resume_prompt
    prompt = _workflow_agent_resume_prompt(
        {"status": "failed", "resume_strategy": "reexecute", "agent_retry_recovery": True, "production_progress": progress},
        runtime_context={"workflow_run_id": "run2"}, step_id="make",
    )
    assert "make only units_to_make" in prompt and '"block_002"' in prompt and "stickman-block_001-r2.mp4" in prompt


def test_the_step_that_makes_units_knows_how_much_storage_they_need():
    """The storage precheck sizes what is left to make, not what was already made."""
    from packages.core.services.storage_capacity import estimate_media_bytes
    from packages.core.services.workflow_production import production_storage_bytes

    steps = [STEPS[0], {"id": "make", "type": "agent", "config": {}}]
    variables = {"script_packet": {"blocks": [{"block_id": f"S{n:02d}", "duration_seconds": 10} for n in range(1, 41)]}}
    shot = estimate_media_bytes("video", duration_seconds=10)
    assert production_storage_bytes(steps, variables, "make") == 40 * shot
    retry = {"status": "failed", "production_progress": {"units_to_make": [{"unit_id": "S39"}, {"unit_id": "S40"}]}}
    assert production_storage_bytes(steps, variables, "make", retry) == 2 * shot
    # Planning steps make no units, and nothing is sized before the plan exists.
    assert production_storage_bytes(steps, variables, "write_script") == 0
    assert production_storage_bytes(steps, {}, "make") == 0


def test_a_song_made_in_one_go_shows_its_stages_and_the_track():
    """Units are optional: a production of another medium leads with its finished whole."""
    song = [{"id": "start", "type": "trigger", "config": {"production": {
        "media": "audio", "final_from": "song",
        "stages": [{"label": "Compose", "steps": ["compose"], "weight": 3}, {"label": "Listen", "steps": ["listen"]}],
        "revise": {"from_step": "compose", "notes_input": "revision_notes"},
    }}}]
    view = workflow_run_production(song, {"song": {"document_id": "01SONG", "name": "anthem.mp3"}})
    assert (view["media"], view["units"], view["final"]) == ("audio", [], {"document_id": "01SONG", "name": "anthem.mp3"})


@pytest.mark.parametrize("production", [
    {"stages": []},
    {"stages": [{"label": "A", "steps": []}]},
    {"stages": [{"label": "A", "steps": ["a"], "weight": 0}]},
    {"stages": [{"label": "A", "steps": ["a"]}], "units_from": "a..b"},
    {"stages": [{"label": "A", "steps": ["a"]}], "unit_fields": {"id": "bad field"}},
    {"stages": [{"label": "A", "steps": ["a"]}], "lanes": [{"key": "n", "label": "N", "from": "x", "script": 1}]},
    {"stages": [{"label": "A", "steps": ["a"]}], "layout": "timeline"},
    {"stages": [{"label": "A", "steps": ["a"]}], "media": "3d"},
    {"stages": [{"label": "A", "steps": ["a"]}], "revise": {"from_step": "a"}},
    {"stages": [{"label": "A", "steps": ["a"]}], "revise": {"from_step": "a", "notes_input": "bad key"}},
])
def test_production_is_validated(production):
    with pytest.raises(ValueError):
        normalize_workflow_production(production)


@pytest.mark.parametrize("context", [[], {"__receipt": "made"}, {"cache": "__receipt"}, {"notes": "made"}])
def test_revision_context_cannot_replace_runtime_fields_or_the_new_note(context):
    with pytest.raises(ValueError, match="context_inputs"):
        normalize_workflow_production({"stages": [{"label": "Make", "steps": ["make"]}], "revise": {
            "from_step": "make", "notes_input": "notes", "context_inputs": context,
        }})


@pytest.mark.parametrize("media, target, source", [
    ("video", "picture", "clean_cut"), ("image", "approved_brief", "catalog_brief"),
])
def test_revision_carries_only_declared_context_without_mutating_the_finished_run(media, target, source):
    from packages.core.services.workflow_production import production_revision_inputs

    revise = normalize_workflow_production({"media": media, "stages": [{"label": "Make", "steps": ["make"]}],
        "revise": {"from_step": "make", "notes_input": "notes", "context_inputs": {target: source}},
    })["revise"]
    previous = {source: {"document_id": "saved-document"}, "unrequested": "private", "__receipt": "private"}
    patch = production_revision_inputs(revise, previous, notes="Change the colour", unit_versions={})
    assert patch == {target: {"document_id": "saved-document"}, "notes": "Change the colour"}
    patch[target]["document_id"] = "another-document"
    assert previous[source] == {"document_id": "saved-document"}


def test_saving_a_workflow_refuses_an_invalid_production():
    from packages.core.services.workflow_service import validate_workflow_steps

    bad = [{**STEPS[0], "config": {"production": {"stages": []}}}, *STEPS[1:]]
    result = validate_workflow_steps(bad)
    assert any(error.get("code") == "invalid_production" for error in result["errors"]), result
    # A workflow_project node's own `production` ("take") is another setting, not the run's display.
    take = [*STEPS, {"id": "open_take", "type": "workflow_project", "config": {
        "operation": "get", "project_id": "{{episode_id}}", "project_type": "episode", "production": "take"}}]
    assert not any(error.get("code") == "invalid_production"
                   for error in validate_workflow_steps(take)["errors"])


