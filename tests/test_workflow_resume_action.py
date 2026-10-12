from types import SimpleNamespace

from packages.core.services.workflow_run_control import run_resume_action
from packages.core.services.workflow_run_trace import build_execution_snapshot


def paused_run(wait_type="approval", step_type="wait"):
    workflow = SimpleNamespace(id="flow", version=1, variables={}, steps=[{
        "id": "confirm", "type": step_type, "name": "Confirm scheme",
        "config": {"wait_type": wait_type, "response_variable": "scheme_decision", "options": ["approve", "reject"]},
    }])
    return SimpleNamespace(
        id="run", workflow_id="flow", status="paused", current_step_id="confirm",
        execution_snapshot=build_execution_snapshot(workflow), trigger_data={},
        step_results={"confirm": {"status": "paused", "output": "Exact saved scheme", "review": {"revision": 3}}},
    )


def test_native_approval_projects_only_the_frozen_wait_and_resolved_review():
    action = run_resume_action(paused_run())
    assert action["options"] == ["approve", "reject"]
    assert action["response_variable"] == "scheme_decision"
    assert action["review"] == {"revision": 3}
    assert action["review_read_only"] is True
    assert action["step_id"] == "confirm"


def test_timer_child_barrier_and_missing_snapshot_have_no_human_response():
    assert run_resume_action(paused_run("timer")) is None
    assert run_resume_action(paused_run(step_type="subworkflow")) is None
    run = paused_run()
    run.execution_snapshot = None
    assert run_resume_action(run) is None


def test_manual_and_event_waits_do_not_fabricate_approval():
    assert run_resume_action(paused_run("event"))["kind"] == "workflow_resume"
    run = paused_run()
    run.trigger_data = {"_workflow_run_control": {"state": "manual_paused"}}
    assert run_resume_action(run)["kind"] == "workflow_resume"


def test_chat_wait_keeps_its_existing_message_approval_path():
    run = paused_run()
    run.trigger_data = {"_workflow_chat_origin": {
        "enabled": True, "conversation_id": "conversation", "activity_message_id": "message",
    }}
    assert run_resume_action(run) is None


def test_input_wait_projects_its_frozen_schema_and_response_variable():
    run = paused_run("input")
    config = {
        "wait_type": "input",
        "message": "Add the cover artwork",
        "response_variable": "release_inputs",
        "input_schema": {
            "type": "object",
            "properties": {
                "cover_art": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["document_id"],
                        "properties": {"document_id": {"type": "string"}},
                    },
                },
            },
            "required": ["cover_art"],
        },
    }
    run.execution_snapshot = build_execution_snapshot(SimpleNamespace(
        id="flow", version=1, variables={}, steps=[{
            "id": "confirm", "type": "wait", "name": "Confirm scheme", "config": config,
        }],
    ))

    action = run_resume_action(run)

    assert action == {
        "kind": "workflow_input",
        "workflow_run_id": "run",
        "step_id": "confirm",
        "title": "Confirm scheme",
        "options": ["submit"],
        "response_variable": "release_inputs",
        "prompt": "Exact saved scheme",
        "input_schema": config["input_schema"],
    }
