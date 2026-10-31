"""request_input outside the chat loop: a worker-job Step waiting on a person
keeps its transcript on resume."""
from __future__ import annotations

from types import SimpleNamespace


from packages.core.ai.runtime.control import (
    RuntimeAgentCheckpoint,
)
from packages.core.constants.execution import ExecutionStepStatus
from packages.core.services.step_resume import apply_step_cancel, apply_step_resume
from packages.core.services.task_input_resume import task_input_log_meta


def _checkpoint_dict(call_id: str = "call-ask") -> dict:
    return RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "publish"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "name": "request_input", "arguments": {"question": "Which screenshot?"}}
            ]},
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": call_id, "name": "request_input", "arguments": {"question": "Which screenshot?"}},
    ).to_dict()


# ── step row mutations ────────────────────────────────────────────────────


def test_step_resume_keeps_the_checkpoint_and_cancel_drops_it() -> None:
    step = SimpleNamespace(
        params={"prompt": "publish"},
        step_status=ExecutionStepStatus.WAITING_HUMAN.value,
        human_input_prompt="Which screenshot?",
        human_input_response=None,
        current_lease_id="lease",
        error=None,
        finished_at=None,
        runtime_checkpoint=_checkpoint_dict(),
    )
    apply_step_resume(step, human_input_response={"choice": "respond", "note": "hero"})
    assert step.step_status == ExecutionStepStatus.PENDING.value
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == "call-ask"

    apply_step_cancel(step, "user skipped")
    assert step.step_status == ExecutionStepStatus.FAILED.value
    assert step.runtime_checkpoint is None


# ── the Task page reads the question ─────────────────────────────────────


def test_task_input_log_meta_matches_what_the_task_page_reads() -> None:
    meta = task_input_log_meta({
        "question": "Which unit?",
        "options": ["4C", "5A"],
        "fields": [{"name": "budget", "label": "Budget", "type": "number", "required": True}],
    })
    assert meta["question"] == "Which unit?"
    assert meta["options"] == ["4C", "5A"]
    assert meta["hitl"] == {
        "kind": "human_input",
        "fields": [{"name": "budget", "label": "Budget", "type": "number", "required": True}],
    }
    assert task_input_log_meta({"question": "Q"})["hitl"]["fields"] == [
        {"name": "response", "type": "textarea", "required": True}
    ]


