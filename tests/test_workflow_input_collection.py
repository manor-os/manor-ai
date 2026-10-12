import pytest

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.ai.tools.generate_file.code import _bundle_profile_id
from packages.core.services.code_projects import check_file_allowed, get_profile
from packages.core.services.workflow_chat_projection import _workflow_final_output_attachments


@pytest.mark.asyncio
async def test_collection_wait_reasks_only_for_missing_answers_and_preserves_prior_rounds():
    step = {
        "id": "collect",
        "type": "wait",
        "config": {
            "wait_type": "input",
            "message": "Complete release details.",
            "response_variable": "answers",
            "output_var": "release_info",
            "fields_per_round": 1,
            "collection_fields": [
                {"key": "song_title", "label": "Song title", "required": True},
                {"key": "artist_name", "label": "Artist name", "required": True},
                {"key": "genre", "label": "Genre", "required": True},
            ],
        },
    }
    run = WorkflowRun(
        id="test-run",
        workflow_id="test-flow",
        entity_id="test-entity",
        status="running",
        variables={"song_title": "Night Window"},
        step_results={},
        trigger_data={},
    )
    runner = WorkflowRunner()

    first = await runner._execute_wait_step(step, run, None)
    assert first["status"] == "paused"
    assert first["resume_strategy"] == "reexecute"
    assert [field["key"] for field in first["collection_fields"]] == ["artist_name"]

    run.step_results = {"collect": first}
    run.variables = {
        **run.variables,
        "answers": {"payload": {"inputs": {"artist_name": "Demo Artist"}}},
    }
    second = await runner._execute_wait_step(step, run, None)
    assert [field["key"] for field in second["collection_fields"]] == ["genre"]
    assert second["collection_values"] == {
        "song_title": "Night Window",
        "artist_name": "Demo Artist",
    }

    run.step_results = {"collect": second}
    run.variables = {
        **run.variables,
        "answers": {"payload": {"inputs": {"genre": "Indie pop"}}},
    }
    final = await runner._execute_wait_step(step, run, None)
    assert final["status"] == "completed"
    assert final["output_var"] == "release_info"
    assert final["output"] == {
        "song_title": "Night Window",
        "artist_name": "Demo Artist",
        "genre": "Indie pop",
    }


def test_release_package_profile_accepts_master_audio_without_relaxing_generic_bundles():
    assert _bundle_profile_id({"profile": "release_package"}, {}, []) == "release_package"
    check_file_allowed(get_profile("release_package"), "assets/audio/master.wav", 50 * 1024 * 1024)
    with pytest.raises(ValueError, match="not allowed"):
        check_file_allowed(get_profile("generic"), "assets/audio/master.wav", 1024)


def test_release_assets_are_visible_in_final_chat_attachments():
    attachments = _workflow_final_output_attachments(
        {
            "files": [{"path": "bundle/README.md"}],
            "assets": [{"path": "assets/audio/master.wav", "fs_path": "bundle/assets/audio/master.wav"}],
        },
        entity_id="test-entity",
    )
    assert attachments[0]["name"] == "master.wav"
    assert attachments[0]["fsPath"] == "bundle/assets/audio/master.wav"
