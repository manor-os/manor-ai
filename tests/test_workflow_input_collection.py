import pytest

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.ai.tools.generate_file.code import _bundle_profile_id
from packages.core.services.code_projects import check_file_allowed, get_profile
from packages.core.services.workflow_chat_projection import _workflow_final_output_attachments


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [False, True, "approval"], ids=["schema", "collection", "approval"])
async def test_native_input_wait_is_actionable_without_a_chat_message(client, collection):
    from tests.test_workflows import _auth, _create_workspace

    headers = await _auth(client, f"native_input_projection_{collection}")
    workspace_id = await _create_workspace(client, headers, "Delivery planning")
    config = {
        "wait_type": "input", "response_variable": "delivery",
        "message": "Choose the delivery date.",
        **({"collection_fields": [{"key": "date", "label": "Delivery date", "required": True}]}
           if collection else {"input_schema": {
               "type": "object", "required": ["date"],
               "properties": {"date": {"type": "string", "minLength": 1}},
           }}),
    }
    approval = collection == "approval"
    if approval:
        config = {
            "wait_type": "approval", "response_variable": "delivery",
            "message": "Review the proposed delivery date.",
            "options": ["approve", "hold"], "approval_values": ["approve"],
        }
    answer = "approve" if approval else {"date": "2026-11-01"}
    response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Plan a delivery", "workspace_id": workspace_id,
        "steps": [
            {"id": "start", "type": "trigger", "config": {}, "next": ["details"]},
            {"id": "details", "type": "wait", "config": config, "next": ["done"]},
            {"id": "done", "type": "end", "config": {}, "next": []},
        ],
    })
    assert response.status_code == 201, response.text
    response = await client.post(f"/api/v1/workflows/{response.json()['id']}/run",
                                 headers=headers, json={})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    path = f"/api/v1/workflows/runs/{run_id}"
    compact = (await client.get(path + "?detail=false", headers=headers)).json()
    action = compact["intervention"]
    assert compact["status"] == "paused"
    assert compact["capabilities"]["can_control"] is True
    assert action["kind"] == ("workflow_approval" if approval else "workflow_input")
    assert action["workflow_run_id"] == run_id
    assert action["step_id"] == "details"
    assert action["options"] == (["approve", "hold"] if approval else ["submit"])
    assert "message_id" not in action
    assert "step_results" not in compact
    detail = (await client.get(path, headers=headers)).json()
    assert detail["resume_action"] == action

    stale = await client.post(path + "/resume", headers=headers, json={
        "expected_step_id": "old_wait", "variables": {"delivery": answer},
    })
    assert stale.status_code == 409
    response = await client.post(path + "/resume", headers=headers, json={
        "expected_step_id": "details", "variables": {"delivery": answer},
    })
    assert response.status_code == 200, response.text
    await WorkflowRunner().run(run_id)
    terminal = (await client.get(path, headers=headers)).json()
    assert terminal["status"] == "completed"
    assert terminal["variables"]["delivery"] == answer
    assert (await client.get(path + "?detail=false", headers=headers)).json()["intervention"] is None


@pytest.mark.asyncio
async def test_run_page_collection_uses_native_message_action_across_rounds(client):
    from tests.test_workflows import _auth, _create_workspace

    headers = await _auth(client, "collection_run_page")
    workspace_id = await _create_workspace(client, headers, "Collection run page")
    response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Collect campaign settings",
        "steps": [
            {"id": "start", "type": "trigger", "config": {}, "next": ["collect"]},
            {"id": "collect", "type": "wait", "config": {
                "wait_type": "input", "response_variable": "answers", "output_var": "settings",
                "fields_per_round": 1,
                "collection_fields": [
                    {"key": "campaign", "label": "Campaign", "required": True},
                    {"key": "timezone", "label": "Timezone", "required": True},
                ],
            }, "next": ["done"]},
            {"id": "done", "type": "end", "config": {}, "next": []},
        ],
    })
    assert response.status_code == 201, response.text
    response = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": response.json()["id"], "workspace_id": workspace_id,
        "trigger_type": "manual", "config": {
            "workspace_attached": True,
            "chat_entrypoint": {"enabled": True, "wait_bridge": True},
        },
    })
    assert response.status_code == 201, response.text
    response = await client.post(f"/api/v1/workflows/bindings/{response.json()['id']}/run", headers=headers,
                                 json={"execute": True, "trigger_data": {"workspace_manual": True}})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    await WorkflowRunner().run(run_id)
    message_ids = []
    for key, value in (("campaign", "Release"), ("timezone", "UTC")):
        detail = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
        action = detail["resume_action"]
        assert action is not None
        assert action["kind"] == "workflow_input"
        assert action["step_id"] == "collect"
        assert action["message_workspace_id"] == workspace_id
        assert [field["key"] for field in action["inputs"]] == [key]
        assert action["message_id"] not in message_ids
        message_ids.append(action["message_id"])
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{action['message_id']}/resolve",
            headers=headers, json={"choice": "respond", "payload": {"inputs": {key: value}}},
        )
        assert response.status_code == 200, response.text
        await WorkflowRunner().run(run_id)
    detail = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert detail["status"] == "completed"
    assert detail["variables"]["settings"] == {"campaign": "Release", "timezone": "UTC"}
    assert detail["resume_action"] is None


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


@pytest.mark.asyncio
async def test_collection_prompt_offers_recommended_answers_in_the_message_language():
    step = {
        "id": "collect",
        "type": "wait",
        "config": {
            "wait_type": "input",
            "message": "Confirm the campaign.",
            "response_variable": "answers",
            "collection_fields": [
                {"key": "work_title", "label": "Work title", "required": True,
                 "default": "{{analysis.work.title}}"},
                {"key": "campaign_name", "label": "Campaign name", "required": True,
                 "default": "{{analysis.work.missing}} campaign",
                 "suggestions": ["{{analysis.work.title}} launch", "{{analysis.work.title}} launch", "{{analysis.work.missing}}"]},
            ],
        },
    }
    run = WorkflowRun(
        id="test-run",
        workflow_id="test-flow",
        entity_id="test-entity",
        status="running",
        variables={"analysis": {"work": {"title": "Night Window"}}},
        step_results={},
        trigger_data={},
    )
    paused = await WorkflowRunner()._execute_wait_step(step, run, None)
    assert paused["status"] == "paused"
    # A resolved default prefills the answer; unresolved bindings and repeats are dropped.
    work, campaign = paused["collection_fields"]
    assert work["default"] == "Night Window" and "suggestions" not in work
    assert "default" not in campaign and campaign["suggestions"] == ["Night Window launch"]
    # Defaults only prefill the form: the field is still asked until someone confirms it.
    assert paused["collection_values"] == {}
    assert paused["output"] == "Confirm the campaign.\nPlease confirm or fill in: Work title, Campaign name"
    # A Chinese request keeps the line in Chinese.
    step["config"]["message"] = "请确认活动信息。"
    zh = await WorkflowRunner()._execute_wait_step(step, run, None)
    assert zh["output"] == "请确认活动信息。\n请确认或补充：Work title、Campaign name"


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
