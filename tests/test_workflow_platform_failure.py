"""The platform, not the model, owns a Workflow run's failure headline.

An Agent step's model may paste a tool's raw error JSON into its summary, and a
Stop node may interpolate that summary. A Manor platform fault observed in the
run's tool evidence must surface as the localized platform message, with the
raw payload kept only in details; no failed step headline is a JSON blob.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from packages.core.ai import workflow_runner as workflow_runner_module
from packages.core.ai.runtime.tool_evidence import (
    RuntimeToolEvidence,
    runtime_model_view_of_observation,
    runtime_unresolved_platform_error,
)
from packages.core.ai.workflow_failure import settle_workflow_failure, user_facing_run_error
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.i18n import t
from packages.core.models.workflow import WorkflowRun
from packages.core.services.ai_file_permissions import _receipt_mismatch_payload


def _receipt_mismatch() -> str:
    return _receipt_mismatch_payload(
        action="create",
        tool_name="generate_file",
        paths=["videos/ep01.mp4"],
        authorized_action_key="file.write:videos/ep01.mp4",
        authorized_paths=("videos/ep01.mp4",),
        attempted_action_key="file.write:videos/other.mp4",
    )


class _Db:
    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


@asynccontextmanager
async def _session():
    yield _Db()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary_template",
    ["保存视频失败，完整错误：{raw}", "没有文件写入权限，请授予权限后重试"],
    ids=["pasted-raw-json", "misleading-prose"],
)
async def test_platform_fault_owns_the_run_failure_the_model_summarized(monkeypatch, summary_template):
    raw = _receipt_mismatch()
    seen_by_model: list[dict] = []

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    async def loop(**_kwargs):
        # The real ledger observes the real tool payload; only the LLM is faked.
        evidence = RuntimeToolEvidence()
        evidence.observe_tool_result("generate_file", "call-1", raw, {"kind": "video", "name": "videos/ep01.mp4"})
        seen_by_model.extend(runtime_model_view_of_observation(item) for item in evidence.observations.values())
        summary = summary_template.format(raw=raw)
        return SimpleNamespace(
            content=json.dumps({"ready": False, "summary": summary}, ensure_ascii=False),
            usage={}, tool_calls_made=["generate_file"], rounds=2, stop_reason="completed",
            error=None, error_detail=None, control=None, tool_evidence=evidence.checkpoint_state(),
        )

    async def locale(_user_id, **_kwargs):
        return "zh"

    async def no_projection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow_runner_module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(workflow_runner_module, "runtime_execute_workflow_agent_loop", loop)
    monkeypatch.setattr(workflow_runner_module, "release_read_transaction", no_projection)
    monkeypatch.setattr(workflow_runner_module, "_project_workflow_chat_safely", no_projection)
    monkeypatch.setattr("packages.core.ai.response_language.resolve_response_locale", locale)
    monkeypatch.setattr(
        "packages.core.services.workflow_chat_projection.project_workflow_run_status", no_projection,
    )

    run = WorkflowRun(
        id="run1", workflow_id="wf1", entity_id="ent1", started_by="user-1",
        variables={"episode": "EP01"}, step_results={}, execution_trace=[], status="running",
        definition_snapshot={"nodes": [{"id": "produce"}, {"id": "stop"}]},
    )
    workflow = SimpleNamespace(steps=[
        {
            "id": "produce", "name": "制作视频", "type": "agent",
            "config": {"prompt": "Produce {{episode}}", "output_format": "json", "output_var": "production"},
        },
        {
            "id": "stop", "name": "停止", "type": "stop", "depends_on": ["produce"],
            "config": {"message": "{{episode}} 制作失败：{{production.summary}}"},
        },
    ])

    await WorkflowRunner(session_factory=_session)._run_loop(workflow, run, _Db())

    platform_message = t("workflow.failure.platform_error", "zh", step="制作视频")
    assert run.status == "failed"
    assert run.error == platform_message
    stop = run.step_results["stop"]
    assert stop["error"] == platform_message
    failure = stop["platform_failure"]
    assert failure["code"] == "internal_authorization_error"
    # Observed by the host on the tool call, not parsed from the model's text.
    assert failure["details"]["tool"] == "generate_file" and failure["details"]["call_id"] == "call-1"
    assert failure["messages"]["en"] == t("workflow.failure.platform_error", "en", step="制作视频")
    # The raw tool payload and the model's own text live only in details.
    assert failure["details"]["tool_error"]["mode"] == "authorization_receipt_mismatch"
    assert failure["details"]["step_error"].startswith("EP01 制作失败：")
    assert "{" not in run.error and "{" not in stop["error"]
    assert run.execution_trace[-1]["error"] == platform_message
    # The raw payload is operator detail, never model context.
    assert seen_by_model and all("platform_error_details" not in item for item in seen_by_model)


@pytest.mark.asyncio
async def test_context_overflow_is_reported_as_such_with_its_budget(monkeypatch, caplog):
    budget = {
        "estimated_input_tokens": 131_204, "reserved_output_tokens": 8192,
        "context_limit_tokens": 128_000, "compaction_error": "protected_input_exceeds_window",
    }

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    async def loop(**_kwargs):
        # What agentic_loop's context_budget_failure returns: no provider call happened.
        return SimpleNamespace(
            content="This request is too large to fit safely in the conversation context.",
            usage={}, tool_calls_made=[], rounds=3, stop_reason="error", error="context_budget_exceeded",
            error_detail=budget, control=None, tool_evidence={},
        )

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow_runner_module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(workflow_runner_module, "runtime_execute_workflow_agent_loop", loop)
    monkeypatch.setattr(workflow_runner_module, "release_read_transaction", nothing)
    monkeypatch.setattr(workflow_runner_module, "_project_workflow_chat_safely", nothing)
    monkeypatch.setattr("packages.core.services.workflow_chat_projection.project_workflow_run_status", nothing)

    run = WorkflowRun(
        id="run2", workflow_id="wf1", entity_id="ent1", variables={}, step_results={},
        execution_trace=[], status="running", definition_snapshot={"nodes": [{"id": "start"}, {"id": "produce"}]},
    )
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger"},
        {"id": "produce", "name": "Produce", "type": "agent", "depends_on": ["start"], "config": {"prompt": "Go"}},
    ])
    with caplog.at_level("WARNING", logger=workflow_runner_module.logger.name):
        await WorkflowRunner(session_factory=_session)._run_loop(workflow, run, _Db())

    assert run.status == "failed"
    assert run.error == t("workflow.failure.context_budget_exceeded", "en", step="Produce")
    assert "provider request failed" not in run.error
    failure = run.step_results["produce"]["platform_failure"]
    assert failure["code"] == "context_budget_exceeded"
    assert {key: failure["details"][key] for key in budget} == budget
    assert "protected_input_exceeds_window" in caplog.text


def test_a_later_successful_call_resolves_the_platform_fault():
    evidence = RuntimeToolEvidence()
    evidence.observe_tool_result("generate_file", "call-1", _receipt_mismatch(), {"kind": "video"})
    assert runtime_unresolved_platform_error(evidence.checkpoint_state())["code"] == "internal_authorization_error"

    evidence.observe_tool_result("generate_file", "call-2", json.dumps({"status": "completed"}), {"kind": "video"})
    assert runtime_unresolved_platform_error(evidence.checkpoint_state()) is None


@pytest.mark.parametrize(
    ("code", "kwargs", "platform_fault"),
    [
        ("DOCUMENT_SYNC_FAILED", {}, True),
        ("FILESYSTEM_UNAVAILABLE", {}, True),
        ("WORKSPACE_SCOPE_UNAVAILABLE", {}, True),
        ("QUALITY_GATE_BLOCKED", {"internal_error": True}, True),
        # A file that fails its quality gate is the agent's to repair.
        ("QUALITY_GATE_BLOCKED", {}, False),
        ("READ_FAILED", {}, False),
    ],
)
def test_save_failures_manor_caused_are_platform_faults(code, kwargs, platform_fault):
    """A save the platform failed must reach step resilience as a platform
    fault (2026-10-27: a Word deliverable the gate passed was "not saved", the
    step ended unmet and nothing retried it)."""
    from packages.core.ai.runtime.composite_tools import SandboxToolErrorCode
    from packages.core.ai.tools.sandbox_tools import _sandbox_error

    evidence = RuntimeToolEvidence()
    error = _sandbox_error(SandboxToolErrorCode[code], "x", **kwargs)
    evidence.observe_tool_result("sandbox", "call-1", error, {"action": "save_result"})
    fault = runtime_unresolved_platform_error(evidence.checkpoint_state())
    if platform_fault:
        assert fault["code"] == SandboxToolErrorCode[code].value and fault["tool"] == "sandbox"
    else:
        assert fault is None


@pytest.mark.parametrize(
    "result",
    [
        # retryable:false alone is a blocker the user can resolve, not a platform fault.
        {"error": "chrome_cli_worker_not_paired", "retryable": False, "terminal_failure": True},
        {"error": "blocked_by_governance", "failure": {"retryable": False, "requires_human": True}},
        # A provider cannot declare a Manor platform fault for itself.
        {"error": "boom", "internal_error": True, "retryable": False},
    ],
    ids=["retryable-false-blocker", "governance-block", "external-mcp"],
)
def test_only_manor_internal_errors_are_platform_faults(result):
    tool = "mcp__acme__render" if result.get("internal_error") else "browse_web"
    evidence = RuntimeToolEvidence()
    evidence.observe_tool_result(tool, "call-1", json.dumps(result), {})
    assert runtime_unresolved_platform_error(evidence.checkpoint_state()) is None


_RAW_TOOL_ERRORS = [
    pytest.param(
        "EP01 制作失败：" + _receipt_mismatch(), True, id="internal-error-json",
    ),
    pytest.param(
        'EP01 制作失败：{"contract":"tool_error","status":"error","tool":"merge_videos",'
        '"error":"clip_not_found","message":"Clip 3 was not found.","details":{"clip":3}}',
        False, id="tool-error-envelope",
    ),
    pytest.param(
        "EP01 制作失败：```json\n{\"error\": \"render_failed\", \"reason\": \"Encoder crashed.\"}\n```",
        False, id="fenced-json",
    ),
    pytest.param(
        'EP01 制作失败：{"error": "upload_failed", "message": "Upload timed out", "details": {"bytes',
        False, id="truncated-json",
    ),
    pytest.param(
        'EP01 制作失败：{\\"error\\": \\"render_failed\\", \\"message\\": \\"Encoder crashed\\"}',
        False, id="escaped-json",
    ),
    pytest.param(
        'Retry failed: [{"error": "quota_exceeded", "message": "Daily quota used"}]',
        False, id="json-array",
    ),
]


@pytest.mark.parametrize("step_type", ["stop", "agent", "tool"])
@pytest.mark.parametrize(("error", "platform"), _RAW_TOOL_ERRORS)
def test_no_failed_step_headline_is_a_raw_tool_error_blob(step_type, error, platform):
    step = {"id": "produce", "name": "Produce", "type": step_type}
    settled = settle_workflow_failure(
        step, {"status": "failed", "error": error}, step_results={}, locale="en",
    )

    headline = settled["error"]
    assert headline and '{"' not in headline and '{\\"' not in headline and "```" not in headline
    if platform:
        assert headline == t("workflow.failure.platform_error", "en", step="Produce")
        assert settled["platform_failure"]["details"]["step_error"] == error
    else:
        assert headline.startswith(("EP01 制作失败", "Retry failed"))
        assert settled["error_details"] == error


def test_readable_failure_text_is_unchanged():
    result = {"status": "failed", "error": "EP01 制作失败：素材不足，请补充第 3 段镜头"}
    assert settle_workflow_failure({"id": "stop", "type": "stop"}, result, step_results={}, locale="zh") is result


@pytest.mark.parametrize(("result", "key"), [
    # A host-coded failure reads as its platform message, whatever its raw text.
    ({"status": "failed", "code": "workflow_project_conflict",
      "error": "Workflow project 01M6261Y01G3GP2CFX2ZHSE9JE revision is 3, expected 0"},
     "workflow.failure.workflow_project_conflict"),
    # Any headline naming an internal record ID becomes a plain "stopped" line.
    ({"status": "failed", "error": "Document 3f2b8c1e-9a4d-4c2e-8f1a-2b3c4d5e6f70 is locked"},
     "workflow.failure.technical"),
])
@pytest.mark.parametrize("locale", ["en", "zh"])
def test_failure_headline_never_shows_internal_ids(result, key, locale):
    settled = settle_workflow_failure(
        {"id": "finish_episode", "name": "Finish episode", "type": "workflow_project"},
        result, step_results={}, locale=locale,
    )

    assert settled["error"] == t(key, locale, step="Finish episode")
    assert settled["error_details"] == result["error"]


def test_a_run_recorded_before_settling_reads_without_internal_ids():
    raw = "Workflow project 01M6261Y01G3GP2CFX2ZHSE9JE revision is 3, expected 0"
    run = SimpleNamespace(
        status="failed", error=raw, current_step_id="finish_episode",
        step_results={"finish_episode": {"status": "failed", "code": "workflow_project_conflict", "error": raw}},
        definition_snapshot={"nodes": [{"id": "finish_episode", "name": "Finish episode"}]},
    )
    assert user_facing_run_error(run, "zh") == t("workflow.failure.workflow_project_conflict", "zh", step="Finish episode")
    # A run closed as replaced keeps its own message.
    replaced = SimpleNamespace(**{**vars(run), "status": "cancelled", "error": "Replaced by a newer version"})
    assert user_facing_run_error(replaced, "zh") == "Replaced by a newer version"


@pytest.mark.parametrize("outcome", ["failed_effects_unknown", "failed_recovery_limit"])
def test_a_run_stopped_by_a_platform_restart_reads_as_interrupted(outcome):
    """The recovery's raw text is about execution claims and step types; a person reads what to do."""
    run = SimpleNamespace(
        status="failed", current_step_id="produce", step_results={},
        error="Workflow execution was interrupted while step 'Generate the shots' (agent) was running. It may ...",
        trigger_data={"_execution_recovery": {"outcome": outcome, "attempts": 1}},
        definition_snapshot={"nodes": [{"id": "produce", "name": "Generate the shots"}]},
    )
    assert user_facing_run_error(run, "zh") == t("workflow.failure.interrupted", "zh", step="Generate the shots")
    assert "(agent)" not in user_facing_run_error(run, "en")


def test_a_long_failure_explanation_reads_as_its_first_sentence():
    """An Agent's long explanation is the details; its first sentence is what a person reads."""
    long = ("The platform reports no configured credential for music generation. It also selected a clip model "
            "rather than the requested full-song model. Enable credentials and select the full-song model to "
            "proceed. No audio was generated, published, or scheduled.")
    settled = settle_workflow_failure({"id": "not_created", "type": "stop"},
                                      {"status": "failed", "error": long}, step_results={}, locale="en")
    assert settled["error"] == "The platform reports no configured credential for music generation."
    assert settled["error_details"] == long
    zh = "音乐生成没有配置凭据。" + "请先在设置里添加密钥，然后重试本次运行。" * 8
    assert settle_workflow_failure({"id": "s"}, {"status": "failed", "error": zh}, step_results={},
                                   locale="zh")["error"] == "音乐生成没有配置凭据。"
