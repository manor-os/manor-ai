"""Completed validation lets the Agent edit; uncertain effects are observed, never replayed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.streams import runtime_tool_call_error
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.tools import video_edit_tools as video


def _check_report():
    report = {"ok": False, "strict": True}
    for section in ("lint", "runtime", "layout", "motion", "contrast"):
        report[section] = {"ok": True, "findings": []}
    report["lint"] = {"ok": False, "findings": [
        {"code": "media_missing_data_start", "sourceFile": "index.html",
         "severity": "error", "message": "Video media requires data-start."},
        {"code": "multiple_root_compositions", "sourceFile": "project.html",
         "severity": "error", "message": "Keep one root composition."},
    ]}
    return report


@pytest.fixture
def runner():
    path = Path(video.__file__).resolve().parents[1] / "skills/video-edit/scripts/video_runtime.py"
    spec = importlib.util.spec_from_file_location("video_review_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path):
    example = video._video_authoring_contract()["minimal_example"]
    for name in video._video_authoring_contract()["required_files"]:
        value = example.get(name, "project metadata")
        (tmp_path / name).write_text(value if isinstance(value, str) else json.dumps(value))
    return tmp_path


def _completed_review(report=None):
    return {"status": "ok", "operation": "review", "review_passed": False,
            "check_report": report if report is not None else _check_report()}


def test_runner_emits_completed_negative_review_only_after_full_check(runner, monkeypatch, capsys):
    report = _check_report()
    # Lint names the file it read by absolute path inside the sandbox.
    report["lint"]["findings"][0]["sourceFile"] = str(runner.PROJECT / "index.html")
    monkeypatch.setattr(runner, "_check", lambda **_: CompletedProcess([], 1, json.dumps(report), ""))
    with pytest.raises(SystemExit) as stopped:
        runner._review("0,2")
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out) == _completed_review()


@pytest.mark.parametrize("code,stdout", [
    (1, "process crashed"),
    (1, json.dumps({"ok": False, "error": "Browser crashed"})),
    (1, json.dumps({"ok": False, "lint": {"findings": []}})),
    (137, json.dumps(_check_report())),
])
def test_runner_crash_or_incomplete_report_remains_error(runner, monkeypatch, capsys, code, stdout):
    monkeypatch.setattr(runner, "_check", lambda **_: CompletedProcess([], code, stdout, ""))
    with pytest.raises(SystemExit) as stopped:
        runner._review("0")
    assert stopped.value.code == code
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_render_quality_failure_is_not_promoted_by_review_contract(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "_check", lambda **_: CompletedProcess([], 1, json.dumps(_check_report()), ""))
    with pytest.raises(SystemExit) as stopped:
        runner._render("renders/final.mp4", "high", 4)
    assert stopped.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error" and "review_passed" not in result


@pytest.mark.asyncio
async def test_agent_edits_after_failed_review_then_reviews_again(project, monkeypatch):
    from packages.core.services import entity_fs

    state = video.new_video_edit_session(
        sandbox_id="sandbox", entity_id="entity", user_id="user", conversation_id="chat",
        agent_id=None, workspace_id=None, project_path="project",
    )
    state["last_review_sha256"] = "previous-review"
    state["last_review_artifacts"] = [{"id": "previous-frame"}]
    (project / "project.html").write_text("duplicate root")
    edits, operations = [], []
    calls = 0
    monkeypatch.setattr(video, "_ensure_video_session_sandbox", AsyncMock())
    monkeypatch.setattr(video, "_sync_video_session_to_entity", AsyncMock())
    monkeypatch.setattr(video, "save_video_edit_session", AsyncMock())
    sandbox = SimpleNamespace(write_file=AsyncMock(), exec=AsyncMock(return_value=SimpleNamespace(exit_code=0)), close=AsyncMock())
    async def routed_client(_sandbox_id, **_kwargs):
        return sandbox

    monkeypatch.setattr(video, "_video_sandbox_client", routed_client)

    def write(_entity, path, content, **_kwargs):
        name = path.removeprefix("project/")
        edits.append(name)
        (project / name).write_bytes(content)

    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", write)
    review = AsyncMock(side_effect=[
        (0, _completed_review(), {}, ""),
        (0, {"status": "ok", "operation": "review", "review_passed": True}, {}, ""),
    ])
    monkeypatch.setattr(video, "_run_video_sandbox", review)

    async def model(messages, tools, **_kwargs):
        nonlocal calls
        calls += 1
        assert tools[0]["function"]["name"] == "video_edit"
        if calls == 1:
            arguments = {"operation": "review"}
        elif calls == 2:
            observed = json.loads(messages[-1]["content"])
            assert observed["status"] == "completed" and observed["contract_valid"] is False
            assert {item["code"] for item in observed["diagnostics"]} == {
                "media_missing_data_start", "multiple_root_compositions",
            }
            assert state["last_review_sha256"] == "" and state["last_review_artifacts"] == []
            arguments = {"operation": "write_files", "files": {"index.html":
                (project / "index.html").read_text() + '<video id="source" class="clip" src="assets/source.mp4" data-start="0" data-duration="4" data-track-index="1"></video>'},
                "delete_paths": ["project.html"]}
        elif calls == 3:
            assert "source_updated" in messages[-1]["content"]
            arguments = {"operation": "review"}
        else:
            observed = json.loads(messages[-1]["content"])
            assert observed["status"] == "ready_for_review"
            assert observed["review_sha256"] == state["last_review_sha256"]
            assert observed["approval_required"] is True
            return "The revised video is ready for approval.", [], {}
        return "", [{"id": str(calls), "name": "video_edit", "arguments": arguments}], {}

    async def execute(name, arguments):
        assert name == "video_edit"
        operation = arguments["operation"]
        operations.append(operation)
        assert classify_runtime_tool(name, arguments).effect is RuntimeToolEffect.ACTION
        if operation == "write_files":
            return await video._write_video_session_files(
                state=state, entity_id="entity", project_dir=project,
                files=arguments["files"], delete_paths=arguments["delete_paths"], context=None,
            )
        return await video._review_project(
            project_dir=project, project_rel="project", entity_root=project.parent,
            entity_id="entity", context=None, snapshot_times=[0], session_state=state,
        )

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop.agentic_loop(
        system_prompt="Edit and review the video.", user_message="Combine the previous clips.",
        tools=[video.VIDEO_EDIT_SCHEMA], tool_executor=execute, max_rounds=5,
    )
    assert result.stop_reason == "completed" and result.error is None
    assert operations == ["review", "write_files", "review"]
    assert edits == ["index.html"] and not (project / "project.html").exists()
    assert 'data-start="0"' in (project / "index.html").read_text()
    assert review.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["review", "render", "write_files"])
async def test_uncertain_video_operations_reach_the_model_without_replay(operation, monkeypatch):
    calls = []
    model_calls = 0

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [{"id": "failed", "name": "video_edit", "arguments": {"operation": operation}},
                        {"id": "must-not-run", "name": "video_edit", "arguments": {"operation": "write_files"}}], {}
        # The uncertain error is an observation with tools kept; the dependent
        # suffix was discarded and nothing is replayed for the model.
        assert [tool["function"]["name"] for tool in tools] == ["video_edit"]
        tool_results = [item for item in messages if item.get("role") == "tool"]
        assert [item["tool_call_id"] for item in tool_results] == ["failed"]
        assert "runtime_unavailable" in tool_results[0]["content"]
        assert "must-not-run" not in json.dumps(messages)
        return "The operation outcome is unknown; I will inspect it before retrying.", [], {}

    async def execute(name, arguments):
        calls.append(arguments["operation"])
        return video._json_error("Sandbox connection lost after command submission.", code="runtime_unavailable")

    summary = AsyncMock(side_effect=AssertionError("an uncertain error must not force a tool-free summary"))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", summary)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="edit", tools=[video.VIDEO_EDIT_SCHEMA],
        tool_executor=execute, max_rounds=3,
    )
    assert calls == [operation]
    assert model_calls == 2
    assert result.stop_reason == "completed" and result.error is None
    assert result.content == "The operation outcome is unknown; I will inspect it before retrying."
    summary.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status": "error", "code": "quality_gate_failed", "stdout": json.dumps(_check_report())},
    {"status": "ok", "operation": "review", "review_passed": False},
    {"status": "ok", "operation": "render", "review_passed": False, "check_report": _check_report()},
    _completed_review({"ok": False, "error": "checker crashed"}),
])
async def test_review_needs_completed_authoritative_report(project, monkeypatch, payload):
    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(0, payload, {}, "")))
    result = await video._review_project(
        project_dir=project, project_rel="project", entity_root=project.parent,
        entity_id="entity", context=None, snapshot_times=None,
    )
    assert runtime_tool_call_error(result) is not None
    assert "review_sha256" not in json.loads(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1000, 4000])
async def test_negative_review_diagnostics_survive_context_compaction(project, monkeypatch, budget):
    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(0, _completed_review(), {}, "")))
    result = await video._review_project(
        project_dir=project, project_rel="project", entity_root=project.parent,
        entity_id="entity", context=None, snapshot_times=None,
    )
    assert runtime_tool_call_error(result) is None
    payload = json.loads(result)
    assert payload["review_passed"] is False and payload["static_contract_valid"] is True
    payload["extra_log"] = "verbose output " * 10000
    compact = loop._compact_tool_result_for_context("video_edit", json.dumps(payload), budget)
    observed = json.loads(compact)
    assert len(compact) <= budget
    assert observed["status"] == "completed" and observed["contract_valid"] is False
    assert {item["code"] for item in observed["diagnostics"]} == {
        "media_missing_data_start", "multiple_root_compositions",
    }


def _strict_report():
    """A strict check failing only on warnings, plus non-blocking info."""
    report = _check_report()
    report["lint"]["findings"] = [
        {"code": "system_font_will_alias", "severity": "info", "sourceFile": "index.html",
         "message": "Font family will be substituted at render time: Inter."},
        {"code": "duplicate_media_discovery_risk", "severity": "warning",
         "sourceFile": "compositions/scene-2.html", "selector": "[data-composition-id]",
         "message": "Detected 2 matching img entries with the same source/start/duration.",
         "fixHint": "Avoid duplicated media nodes that can be discovered twice during compilation.",
         "dataAttributes": {}, "bbox": {"x": 0, "y": 0, "width": 0, "height": 0}, "time": 0},
    ]
    # Browser findings carry no fixHint of their own.
    report["contrast"]["findings"] = [
        {"code": "contrast_aa_failure", "severity": "error", "sourceFile": "index.html",
         "message": "Contrast is 2.1:1; WCAG AA requires 4.5:1.", "selector": "#caption",
         "suggestedColor": "rgb(20,20,20)", "requiredRatio": 4.5, "time": 1.5,
         "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}, "dataAttributes": {"data-start": "0"}},
    ]
    return report


@pytest.mark.asyncio
async def test_negative_review_returns_each_strict_failure_with_its_fix(project, monkeypatch):
    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(0, _completed_review(_strict_report()), {}, "")))
    result = await video._review_project(
        project_dir=project, project_rel="project", entity_root=project.parent,
        entity_id="entity", context=None, snapshot_times=None,
    )
    payload = json.loads(result)
    assert payload["review_passed"] is False
    # Errors first; the info finding does not fail strict review.
    assert [item["code"] for item in payload["diagnostics"]] == [
        "contrast_aa_failure", "duplicate_media_discovery_risk",
    ]
    assert [item["code"] for item in payload["advisories"]] == ["system_font_will_alias"]
    contrast, duplicate = payload["diagnostics"]
    assert duplicate["file"] == "compositions/scene-2.html"
    assert duplicate["fix_hint"].startswith("Avoid duplicated media nodes")
    assert "background-image" in duplicate["manor_hint"]
    assert contrast["suggestedColor"] == "rgb(20,20,20)" and contrast["time"] == 1.5
    assert "suggestedColor" in contrast["manor_hint"]
    for item in payload["diagnostics"]:
        assert item["message"] and item["severity"] in {"error", "warning"}
        assert not {"bbox", "dataAttributes", "sourceFile", "fixHint"} & set(item)

    # Under context pressure the fix outlives the finding's other details.
    compact = json.loads(loop._compact_tool_result_for_context("video_edit", result, 1200))
    kept = compact["diagnostics"][-1]
    assert kept["code"] == "duplicate_media_discovery_risk" and kept["file"] and kept["manor_hint"]


def test_strict_rule_list_matches_the_pinned_cli():
    """Regenerate with scripts/hyperframes_check_rules.py when HYPERFRAMES_VERSION moves."""
    rules = json.loads(video._CHECK_RULES_PATH.read_text())
    assert rules["hyperframes_version"] == video.HYPERFRAMES_VERSION
    by_code = {rule["code"]: rule for rule in rules["rules"]}
    # Manor guidance must name rules the pinned CLI can still fail review with.
    stale = [code for code in video._REVIEW_FINDING_GUIDANCE if not by_code.get(code, {}).get("strict_fails")]
    assert stale == []


@pytest.mark.asyncio
async def test_completed_negative_review_settles_operation_without_delivery_receipt(project, monkeypatch):
    from packages.core.ai.runtime.workspace_delegation_actions import (
        _delegation_effect_intent, _delegation_receipts,
    )

    monkeypatch.setattr(video, "_run_video_sandbox", AsyncMock(return_value=(0, _completed_review(), {}, "")))
    result = await video._review_project(
        project_dir=project, project_rel="project", entity_root=project.parent,
        entity_id="entity", context=None, snapshot_times=None,
    )
    arguments = {"operation": "review", "project_path": "project"}
    intent = _delegation_effect_intent(
        "video_edit", arguments, entity_id="entity", workspace_id="workspace",
        task_id=None, tool_call_id="review-call",
    )
    evidence, artifacts, knowledge = _delegation_receipts([
        {"role": "assistant", "tool_calls": [{"id": "review-call", "function": {
            "name": "video_edit", "arguments": json.dumps(arguments),
        }}]},
        {"role": "tool", "tool_call_id": "review-call", "content": result},
    ], entity_id="entity", workspace_id="workspace", task_id=None, authoritative_intents=[intent])
    assert len(evidence) == 1 and evidence[0]["effect"] == "action"
    assert evidence[0]["fields"]["tool_call_id"] == "review-call"
    assert evidence[0]["fields"]["delivery_confirmed"] is False
    assert artifacts == [] and knowledge == []
