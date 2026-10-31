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


