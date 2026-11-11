"""An Agent step that cannot do its work without something only a person can give.

A step that declares ``expects`` must save a file or take an action, so an Agent
missing a connection or the source material saved a note asking for it. That
note then reached the approval that followed, worded as the deliverable. This
tool is the honest way out: the host ends the run with the ``needs_input``
outcome the Workflow surfaces already show, and Retry runs the step again once
the person has provided what was missing.
"""
from __future__ import annotations

from typing import Any, Mapping

STEP_NEEDS_PERSON_TOOL_NAME = "stop_step_needs_person"
STEP_NEEDS_PERSON_STOP_REASON = "workflow_step_needs_person"
STEP_NEEDS_PERSON_CODE = "workflow_step_needs_person"
STEP_NEEDS_PERSON_TOOL = {
    "type": "function", "function": {
        "name": STEP_NEEDS_PERSON_TOOL_NAME,
        "description": (
            "End this Workflow step when you cannot do its actual work without something only a person can "
            "provide: an account that is not connected, source material you cannot get, or a decision nobody "
            "has made. The Workflow stops and asks them; nothing later in it runs, including any approval or "
            "delivery. Try what your tools allow first. Do not call it for a platform fault that a retry could "
            "clear, or when you have already sent or published something."
        ),
        "parameters": {
            "type": "object", "properties": {
                "missing": {
                    "type": "string",
                    "description": "What is missing and why the step cannot go on without it, for the person.",
                },
                "ask": {
                    "type": "string",
                    "description": "The one thing the person should do, such as connecting an account.",
                },
            }, "required": ["missing", "ask"], "additionalProperties": False,
        },
    },
}
STEP_NEEDS_PERSON_GUIDANCE = (
    f"If you cannot do this step's actual work without something only a person can provide, call "
    f"{STEP_NEEDS_PERSON_TOOL_NAME}. Do not save a note, a request or a stand-in in place of what the step "
    "should produce: later steps treat what you save as the real result. Saying so in your reply does not stop "
    "the step; only this tool does."
)
STEP_NEEDS_PERSON_TERMINAL_POLICY = {
    "rules": [{
        "tool_names": [STEP_NEEDS_PERSON_TOOL_NAME],
        "stop_reason": STEP_NEEDS_PERSON_STOP_REASON,
    }],
}

_TEXT_LIMIT = 1000


def step_needs_person_request(args: Any) -> dict[str, str] | None:
    """The request a tool call makes, or None when it does not say what is needed."""
    if not isinstance(args, Mapping):
        return None
    missing = str(args.get("missing") or "").strip()[:_TEXT_LIMIT]
    ask = str(args.get("ask") or "").strip()[:_TEXT_LIMIT]
    return {"missing": missing, "ask": ask} if missing and ask else None


def step_needs_person_result(step_id: str, request: Mapping[str, str], **fields: Any) -> dict[str, Any]:
    """A step result that ends the run asking the person; Retry starts at this step."""
    return {
        **fields,
        "status": "completed",
        "code": STEP_NEEDS_PERSON_CODE,
        "output": {"summary": request["missing"]},
        "needs_person": dict(request),
        "retry_state": {
            "retry_from_step_id": step_id,
            "step_id": step_id,
            "observed_problem": request["missing"],
            "required_change": request["ask"],
        },
    }


def step_stopped_for_person(step_results: Any) -> tuple[str, dict[str, Any]] | None:
    """The completed step that asked to stop this run for a person, if any."""
    if not isinstance(step_results, Mapping):
        return None
    for step_id, result in step_results.items():
        if isinstance(result, dict) and result.get("status") == "completed" and result.get("needs_person"):
            return str(step_id), result
    return None


def needs_input_run_result(result: Mapping[str, Any]) -> dict[str, str]:
    """The run's final result, which the Workflow surfaces read as ``needs_input``."""
    request = result.get("needs_person") if isinstance(result.get("needs_person"), Mapping) else {}
    return {
        "status": "needs_input",
        "summary": str(request.get("missing") or ""),
        "next_step": str(request.get("ask") or ""),
    }
