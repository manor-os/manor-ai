"""An Agent step that declares ``expects`` completes only when the host saw that output."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.services.workflow_step_resilience import plan_step_transient_retry


pytestmark = pytest.mark.unit

NOTHING_SAVED = (
    "Production is not complete. The browsing tool failed, so no research memo "
    "or draft has been saved."
)


def _ledger(*, receipts=(), effects=(), platform_error=False, effect_state="none"):
    observations = [{"call_id": "c1", "tool": "web_fetch", "operation": None, "result": "returned"}]
    if platform_error:
        observations.append({
            "call_id": "c2", "tool": "web_fetch", "operation": None,
            "platform_error": "sandbox_unavailable", "result": "error",
        })
    return {
        "observations": observations, "receipts": list(receipts), "effects": list(effects),
        "effect_state": effect_state,
    }


def _patch_loop(monkeypatch, ledger):
    from packages.core.ai import workflow_runner as module

    async def appendix(*args, **kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    async def loop(**kwargs):
        return SimpleNamespace(content=NOTHING_SAVED, usage={}, tool_calls_made=[], rounds=3,
                               stop_reason="completed", error=None, error_detail=None, tool_evidence=ledger)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", loop)


async def _run(config):
    from packages.core.ai.workflow_runner import WorkflowRunner

    step = {"id": "write_draft", "name": "Write the draft", "type": "agent",
            "config": {"prompt": "Write and save the draft", "tools": ["web_fetch"], **config}}
    return step, await WorkflowRunner()._execute_agent_step(step, {}, "entity")


async def test_a_step_that_saved_nothing_is_not_completed(monkeypatch):
    _patch_loop(monkeypatch, _ledger())
    _step, result = await _run({"expects": ["files"]})
    assert result["status"] == "failed"
    assert result["code"] == "workflow_step_expectation_unmet"
    assert result["unmet_expectations"] == ["files"]
    assert result["error"] == "Write the draft finished without a saved file."
    # The Agent's own account stays readable on the step.
    assert result["output"] == NOTHING_SAVED
    assert "transient_failure" not in result


@pytest.mark.parametrize("expects,ledger", [
    (["files"], _ledger(receipts=[{"handle": "r1", "document_id": "doc-1", "filename": "draft.docx"}],
                        effect_state="confirmed")),
    (["effects"], _ledger(effects=[{"action_key": "external_message.send", "category": "communication", "count": 1}],
                          effect_state="confirmed")),
])
async def test_a_step_whose_declared_output_exists_completes(monkeypatch, expects, ledger):
    _patch_loop(monkeypatch, ledger)
    _step, result = await _run({"expects": expects})
    assert result["status"] == "completed", result


def _observed(*calls):
    """A real host ledger after these (tool, arguments, result) calls."""
    import json

    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    evidence = RuntimeToolEvidence()
    for index, (tool, arguments, result) in enumerate(calls):
        evidence.observe_tool_result(tool, f"c{index}", json.dumps(result), arguments)
    return evidence.checkpoint_state()


# Inspecting account setup does not prove that an outward action happened.
CHECKED_ONLY = (
    ("manor", {"action": "integrations.list"}, {"integrations": []}),
)


@pytest.mark.parametrize("calls,met", [
    (CHECKED_ONLY, False),
    ((*CHECKED_ONLY, ("mcp__email__send_email", {"to": "reader@example.test"},
                      {"status": "sent", "message_id": "sent-1"})), True),
])
async def test_only_an_outward_action_meets_effects(monkeypatch, calls, met):
    _patch_loop(monkeypatch, _observed(*calls))
    _step, result = await _run({"expects": ["effects"]})
    assert result["status"] == ("completed" if met else "failed"), result


async def test_a_completed_step_records_the_files_the_host_saw_it_save(monkeypatch):
    """Reviews and run outputs read what the step saved, not the model's summary."""
    _patch_loop(monkeypatch, _observed(
        ("read_file", {"path": "brief.md"}, {"content": "brief", "document_id": "doc-brief"}),
        ("generate_file", {"kind": "document", "filename": "draft.md"},
         {"document": {"id": "doc-draft", "name": "draft.md", "fs_path": "projects/a/draft.md"}}),
        ("sandbox", {"action": "save_result", "params": {"filename": "final.docx"}},
         {"saved": True, "document_id": "doc-final", "name": "final.docx", "fs_path": "projects/a/final.docx"}),
    ))
    _step, result = await _run({})
    assert result["status"] == "completed"
    assert result["saved_files"] == [
        {"document_id": "doc-draft", "name": "draft.md", "fs_path": "projects/a/draft.md"},
        {"document_id": "doc-final", "name": "final.docx", "fs_path": "projects/a/final.docx"},
    ]


async def test_a_step_without_a_declaration_keeps_the_model_outcome(monkeypatch):
    _patch_loop(monkeypatch, _ledger())
    _step, result = await _run({})
    assert result["status"] == "completed"


async def test_a_platform_fault_behind_the_missing_output_is_retried_first(monkeypatch):
    _patch_loop(monkeypatch, _ledger(platform_error=True))
    step, result = await _run({"expects": ["files"]})
    assert result["status"] == "failed"
    assert result["transient_failure"] == "tool_unavailable"
    assert result["platform_failure"]["code"] == "sandbox_unavailable"
    planned = plan_step_transient_retry(step, result, previous_result=None, now=datetime.now(timezone.utc))
    assert planned is not None and planned["status"] == "paused"
    assert planned["transient_retry"]["reason"] == "tool_unavailable"


async def test_a_platform_fault_after_an_outside_action_is_not_replayed(monkeypatch):
    _patch_loop(monkeypatch, _ledger(platform_error=True, effect_state="confirmed"))
    step, result = await _run({"expects": ["files"]})
    assert result["status"] == "failed"
    assert plan_step_transient_retry(step, result, previous_result=None, now=datetime.now(timezone.utc)) is None


@pytest.mark.parametrize("config,fragment", [
    ({"expects": ["file"]}, "unknown value"),
    ({"expects": []}, "non-empty list"),
    ({"expects": "files"}, "non-empty list"),
])
def test_validation_rejects_a_malformed_declaration(config, fragment):
    from packages.core.services.workflow_service import validate_workflow_steps

    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "config": {}, "next": ["write"]},
        {"id": "write", "type": "agent", "config": {"prompt": "x", **config}, "next": []},
    ])
    assert not result["valid"]
    assert any(fragment in error["message"] for error in result["errors"]), result["errors"]


def test_validation_rejects_the_declaration_outside_agent_steps():
    from packages.core.services.workflow_service import validate_workflow_steps

    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "config": {}, "next": ["send"]},
        {"id": "send", "type": "tool", "config": {"tool": "email", "expects": ["effects"]}, "next": []},
    ])
    assert not result["valid"]
    assert any("only applies to agent nodes" in error["message"] for error in result["errors"])
