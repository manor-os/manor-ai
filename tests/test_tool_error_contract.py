"""All execution failures share a diagnostic, non-authorizing model contract."""
import asyncio
import json

import pytest

import packages.core.ai.agentic_loop as loop
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.tool_error_contract import model_tool_error_result
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError, RuntimeStructuredToolResult


@pytest.mark.parametrize("raw", [
    'Error: File missing at /project/reel.md',
    'Tool error (read_file): connection closed',
    'Error invoking skill: missing dependency',
    {"error": "file_not_found", "message": "Read the containing directory", "path": "/project/reel.md"},
    {"status": "failed", "error": {"code": "invalid_argument", "message": "Expected an array", "path": "$.files"}},
    {"ok": False, "reason": "Missing dependency", "package": "renderer"},
    {"success": False, "message": "Connection failed", "provider": "test"},
    {"exit_code": 2, "stderr": "Unknown flag --files"},
    {"isError": True, "content": [{"type": "text", "text": "Service unavailable"}]},
])
def test_every_error_shape_has_one_idempotent_contract(raw):
    result = model_tool_error_result("read_file", raw, max_chars=12000)
    payload = json.loads(result)
    assert set(payload) == {"contract", "status", "tool", "error", "message", "details"}
    assert payload["contract"] == "tool_error"
    assert payload["status"] == "error"
    assert payload["tool"] == "read_file"
    assert payload["message"]
    assert payload["details"] == (raw if isinstance(raw, dict) else {"raw_result": raw})
    assert model_tool_error_result("read_file", result, max_chars=12000) == result


@pytest.mark.parametrize("raw", [
    {"status": "source_updated", "source_saved": True, "contract_valid": False},
    {"status": "waiting_human", "error": "approval required"},
    {"status": "cancelled", "error": "user cancelled"},
    {"status": "blocked", "error": "permission denied"},
    {"status": "success", "text": "example code with an error"},
])
def test_success_and_control_results_are_not_converted_to_retry_errors(raw):
    assert model_tool_error_result("tool", raw, max_chars=12000) is None


@pytest.mark.parametrize("details", [
    {"cause": "Plan criterion criterion:0:delivery lacks evidence from its declared Step", "exception_type": "ValueError"},
    {"validation_errors": [{"path": "capability_bindings[0].owner_scope", "message": "unsupported owner scope"}]},
    {"issues": [{"code": "missing_output_contract", "field_paths": ["plan_json.steps[0].output_shape"]}]},
])
def test_common_validation_diagnostics_survive_context_pressure(details):
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload

    raw = tool_error_payload(
        tool_name="submit_result", code="invalid_assessment", message="Validation failed.",
        details={**details, "stdout": "large provider log " * 5000},
    )
    result = model_tool_error_result("submit_result", raw, max_chars=1000)
    projected = json.loads(result)
    for key, value in details.items():
        assert projected["details"][key] == value
    assert len(result) <= 1000
    assert model_tool_error_result("submit_result", result, max_chars=1000) == result


def test_raw_host_proof_and_secret_fields_are_not_reinterpreted():
    raw = RuntimePreflightValidationError(json.dumps({
        "error": "tool_input_validation_failed", "message": "Expected array",
        "schema_rule": "type", "path": "$.files", "expected": "array",
        "api_key": "must-not-leak", "approval_token": "must-not-leak-either",
    }))
    result = model_tool_error_result("test", raw, max_chars=12000)
    assert "must-not-leak" not in result
    assert type(result) is str
    assert isinstance(raw, RuntimePreflightValidationError)
    assert "must-not-leak" in raw  # presentation never rewrites the receipt
    payload = json.loads(result)
    assert payload["details"]["path"] == "$.files"
    assert payload["details"]["expected"] == "array"


@pytest.mark.parametrize("extra", [
    {"error": "x" * 20000, "truncation": "x" * 20000},
    {"error": "test_error", "truncation": {"original_chars": 10**80, "text": "x" * 20000}},
    {"error": "test_error", "message": "line\n" * 5000},
])
def test_provider_contract_marker_cannot_bypass_bounds_or_required_fields(extra):
    raw = {"contract": "tool_error", "status": "error", "message": "failure",
        "details": {"path": "$.files"}, **extra}
    result = model_tool_error_result("tool", raw, max_chars=1000)
    assert len(result) <= 1000
    payload = json.loads(result)
    assert payload["tool"] == "tool"
    assert isinstance(payload["details"], dict)
    assert len(payload["error"]) <= 128


@pytest.mark.parametrize("budget", [1000, 1500, 4000, 12000])
def test_large_error_retains_structured_repair_evidence_at_every_budget(budget):
    from packages.core.ai.tools.video_edit_tools import _video_authoring_contract

    raw = {"error": "project_contract_incomplete", "message": "Correct the invalid assertion",
        "source_saved": True, "contract_valid": False,
        "contract_violations": [{"file": "index.motion.json", "rule": "motion_assertions",
            "pointer": "/assertions/0", "expected": "appearsBy or keepsMoving",
            "repair_hint": "Replace the unsupported assertion kind"}],
        "authoring_contract": _video_authoring_contract(),
        "stdout": "verbose log " * 5000}
    result = model_tool_error_result("video_edit", raw, max_chars=budget)
    assert len(result) <= budget
    payload = json.loads(result)
    assert payload["error"] == "project_contract_incomplete"
    assert payload["details"]["source_saved"] is True
    violation = payload["details"]["contract_violations"][0]
    assert violation["file"] == "index.motion.json"
    assert violation["pointer"] == "/assertions/0"
    assert "repair_hint" in violation
    assert "preview" not in payload
    assert model_tool_error_result("video_edit", result, max_chars=budget) == result
    smaller = json.loads(model_tool_error_result("video_edit", result, max_chars=1000))
    assert smaller["details"]["contract_violations"][0]["file"] == "index.motion.json"


@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
@pytest.mark.parametrize("result_kind", ["json", "dict", "structured"])
async def test_all_entry_paths_return_diagnostics_and_model_corrects_the_call(monkeypatch, entry, result_kind):
    schema = {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}}
    initial = {"id": "initial", "name": "read_file", "arguments": {"path": "missing.md"}}
    executions, callbacks = [], []
    raw = json.dumps({"error": "file_not_found", "message": "Use the canonical project path",
        "path": "missing.md", "suggested_path": "project/reel.md"})

    async def execute(name, args):
        executions.append(args["path"])
        if args["path"] != "missing.md":
            return "file body"
        if result_kind == "dict":
            return json.loads(raw)
        if result_kind == "structured":
            return RuntimeStructuredToolResult("display-only text", structured_content=json.loads(raw))
        return raw

    async def model(messages, tools, **kwargs):
        observations = [item["content"] for item in messages if item["role"] == "tool"]
        if not observations:
            return "", [initial], {}
        if len(observations) < 3:
            payload = json.loads(observations[-1])
            assert payload["contract"] == "tool_error"
            assert payload["details"]["suggested_path"] == "project/reel.md"
            # A repeated error must retain diagnostics, not a 240-char notice.
            path = "missing.md" if len(observations) == 1 else payload["details"]["suggested_path"]
            return "", [{"id": f"call-{len(observations)}", "name": "read_file", "arguments": {"path": path}}], {}
        return "done", [], {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    options = {}
    if entry == "forced":
        options["forced_tool_calls"] = [initial]
    elif entry == "resume":
        options["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "assistant", "content": "", "tool_calls": [{
                "id": "initial", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps(initial["arguments"]),
                }}]}], usage={}, rounds=1, tool_calls_made=[], pending_tool_call=initial,
        ).to_dict()
    result = await loop.agentic_loop(
        system_prompt="system", user_message="read the script", tools=[schema],
        tool_executor=execute, max_rounds=5,
        on_tool_end=lambda name, result, *args: callbacks.append(result), **options,
    )
    assert result.error is None
    assert result.content == "done"
    assert executions == ["missing.md", "missing.md", "project/reel.md"]
    assert callbacks[0] == raw


def test_structured_result_uses_authoritative_payload_in_model_projection():
    raw = RuntimeStructuredToolResult("display-only text", structured_content={
        "error": "field_missing", "message": "Missing title", "path": "$.title",
    })
    result = json.loads(model_tool_error_result("mcp__test__action", raw, max_chars=1000))
    assert result["details"]["path"] == "$.title"
    assert result["message"] == "Missing title"
    success = RuntimeStructuredToolResult("Error: display-only example", structured_content={"ok": True})
    _, projection = loop._browser_screenshot_observation("mcp__test__read", success)
    assert model_tool_error_result("mcp__test__read", projection, max_chars=1000) is None


async def test_concurrent_error_projection_has_no_shared_state():
    async def project(index):
        await asyncio.sleep(0)
        raw = {"error": "missing", "message": f"file-{index}", "path": f"project-{index}"}
        payload = json.loads(model_tool_error_result(f"tool-{index}", raw, max_chars=1000))
        assert payload["tool"] == f"tool-{index}"
        assert payload["details"]["path"] == f"project-{index}"
    await asyncio.gather(*(project(index) for index in range(64)))
