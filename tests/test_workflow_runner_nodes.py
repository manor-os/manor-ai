"""Unit tests for the new canonical node-type handlers in WorkflowRunner.

Covers the deterministic, network-free nodes added so that imported
(ComfyUI/n8n/Dify) workflows execute through the runner:
trigger / end (passthrough), unsupported (graceful skip), switch
(multi-branch), merge (variable aggregation).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ai import workflow_runner as workflow_runner_module
from packages.core.ai.workflow_runner import WorkflowRunner, workflow_input_wait_value
from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun


def test_workflow_effect_deadline_is_scoped_and_uses_strict_time_boundary() -> None:
    from datetime import datetime, timezone

    from packages.core.ai.runtime.approvals import RuntimeApprovalAction
    from packages.core.ai.runtime.effect_deadline import workflow_effect_deadline_expired
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification

    publication = RuntimeToolClassification.action_call(
        RuntimeApprovalAction("action", "submission.send", "low", "submit", "external", "write")
    )
    unrelated = RuntimeToolClassification.action_call(
        RuntimeApprovalAction("action", "document.save", "low", "save", "document", "write")
    )
    boundary = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert workflow_effect_deadline_expired(
        deadline_at="2026-10-14T00:00:00Z", action_keys=["submission.send"],
        classification=publication, now=boundary,
    ) is True
    assert workflow_effect_deadline_expired(
        deadline_at="2026-10-14T00:00:00Z", action_keys=["submission.send"],
        classification=unrelated, now=boundary,
    ) is False
    assert workflow_effect_deadline_expired(
        deadline_at=None, action_keys=["submission.send"], classification=publication, now=boundary,
    ) is False


def test_workflow_effect_deadline_freezes_trusted_context(runner) -> None:
    run = _run({"deadline": "2026-10-14T08:00:00+08:00"})
    step = {
        "type": "workflow_effect_deadline",
        "config": {"deadline_at": "{{deadline}}", "action_keys": ["submission.send"], "output_var": "bound"},
    }
    first = runner._execute_workflow_effect_deadline_step(step, run.variables, run)
    assert first["status"] == "completed"
    assert first["output"] == {"deadline_at": "2026-10-14T00:00:00Z", "action_keys": ["submission.send"]}
    assert run.trigger_data["_workflow_runtime_context"]["workflow_effect_deadline_at"] == "2026-10-14T00:00:00Z"
    rerun = runner._execute_workflow_effect_deadline_step(step, {"deadline": "2026-10-15T00:00:00Z"}, run)
    assert rerun["status"] == "failed"
    child = workflow_runner_module._child_workflow_context(run, {
        "workflow_effect_deadline_at": "2030-01-01T00:00:00Z",
        "workflow_effect_deadline_action_keys": ["document.save"],
    })
    assert child["workflow_effect_deadline_at"] == "2026-10-14T00:00:00Z"
    assert child["workflow_effect_deadline_action_keys"] == ["submission.send"]


@pytest.mark.asyncio
async def test_runtime_handler_blocks_expired_declared_external_action_before_handler():
    from packages.core.ai.runtime.tool_context import (
        runtime_injected_tool_context_args,
        runtime_tool_call_context_from_kwargs,
    )
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationLeaseFactory,
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    from packages.core.ai.runtime.approvals import RuntimeApprovalAction

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        "action", "submission.send", "low", "submit", "external", "write",
    ))
    calls: list[str] = []

    def handler(**_kwargs):
        calls.append("called")
        return "submitted"

    def prepared(deadline: str) -> RuntimePreparedToolExecution:
        context = runtime_tool_call_context_from_kwargs({
            "_workflow_effect_deadline_at_from_context": deadline,
            "_workflow_effect_deadline_action_keys_from_context": ["submission.send"],
        })
        arguments = runtime_injected_tool_context_args(
            workflow_effect_deadline_at=context.workflow_effect_deadline_at,
            workflow_effect_deadline_action_keys=context.workflow_effect_deadline_action_keys,
        )
        receipt = RuntimeToolAuthorizationReceiptFactory.create(
            decision=RuntimeApprovalDecision.allow(classification),
            request=RuntimeApprovalRequest(
                tool_name="submit_application", arguments=arguments,
                entity_id="entity-1", user_id="user-1",
            ),
        )
        assert receipt is not None
        return RuntimePreparedToolExecution(
            arguments=arguments, classification=classification,
            authorization_lease=RuntimeToolAuthorizationLeaseFactory.create(receipt),
        )

    expired = prepared("2020-01-01T00:00:00Z")
    blocked = await runtime_execute_prepared_tool_handler(
        tool_name="submit_application", handler=handler, prepared=expired, entity_id="entity-1",
    )
    assert json.loads(blocked) == {"error": "workflow_effect_deadline_expired", "action_executed": False}
    assert calls == []

    allowed = prepared("2999-01-01T00:00:00Z")
    assert await runtime_execute_prepared_tool_handler(
        tool_name="submit_application", handler=handler, prepared=allowed, entity_id="entity-1",
    ) == "submitted"
    assert calls == ["called"]


def test_runtime_social_publication_action_keys_match_provider_classification():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action

    keys = {
        tool_name: classify_runtime_tool_action(tool_name, {}).action_key
        for tool_name in (
            "mcp__facebook__create_instagram_media",
            "mcp__facebook__publish_instagram_media",
            "mcp__tiktok__post_video",
            "mcp__tiktok__post_photo",
        )
    }
    assert keys == {
        "mcp__facebook__create_instagram_media": "facebook.create_instagram_media",
        "mcp__facebook__publish_instagram_media": "social_post.publish",
        "mcp__tiktok__post_video": "social_post.publish",
        "mcp__tiktok__post_photo": "social_post.publish",
    }


@pytest.mark.asyncio
async def test_chrome_mutation_family_deadline_blocks_unlabelled_publish_click():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_context import (
        runtime_injected_tool_context_args,
        runtime_tool_call_context_from_kwargs,
    )
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    context = runtime_tool_call_context_from_kwargs({
        "_workflow_effect_deadline_at_from_context": "2020-01-01T00:00:00Z",
        "_workflow_effect_deadline_action_keys_from_context": ["chrome.browser.interact"],
    })
    forwarded = runtime_injected_tool_context_args(
        workflow_effect_deadline_at=context.workflow_effect_deadline_at,
        workflow_effect_deadline_action_keys=context.workflow_effect_deadline_action_keys,
    )
    arguments = {
        **forwarded,
        "target": {"role": "button", "name": "Publish"},
        "operation_intent": {"effect": "prepare"},
    }
    classification = classify_runtime_tool("mcp__chrome__click_element", arguments)
    assert classification.action is not None
    assert classification.action.action_key == "chrome.representational_communication.confirm"
    writes: list[bool] = []

    def browser_handler(**_kwargs):
        writes.append(True)
        return "unexpected"

    result = await runtime_execute_prepared_tool_handler(
        tool_name="mcp__chrome__click_element", handler=browser_handler,
        prepared=RuntimePreparedToolExecution(arguments=arguments, classification=classification),
        entity_id="entity-1",
    )
    assert json.loads(result)["error"] == "workflow_effect_deadline_expired"
    assert writes == []

    read_arguments = forwarded
    read_classification = classify_runtime_tool("mcp__chrome__read_page", read_arguments)
    assert await runtime_execute_prepared_tool_handler(
        tool_name="mcp__chrome__read_page", handler=lambda **_kwargs: "observed",
        prepared=RuntimePreparedToolExecution(arguments=read_arguments, classification=read_classification),
        entity_id="entity-1",
    ) == "observed"


@pytest.mark.asyncio
async def test_expired_deadline_settles_claimed_provider_approval(monkeypatch):
    from packages.core.ai.runtime import tool_execution
    from packages.core.ai.runtime.approvals import RuntimeApprovalAction
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        "action", "submission.send", "low", "submit", "external", "write",
    ))
    settled: dict[str, object] = {}

    async def settle(**kwargs):
        settled.update(kwargs)
        return "settled-before-provider-write"

    monkeypatch.setattr(tool_execution, "runtime_settle_provider_approval_preflight_failure", settle)
    result = await runtime_execute_prepared_tool_handler(
        tool_name="submit_application",
        handler=lambda **_kwargs: pytest.fail("handler must not run"),
        prepared=RuntimePreparedToolExecution(
            arguments={
                "_workflow_effect_deadline_at_from_context": "2020-01-01T00:00:00Z",
                "_workflow_effect_deadline_action_keys_from_context": ["submission.send"],
            },
            classification=classification,
        ),
        entity_id="entity-1",
    )
    assert result == "settled-before-provider-write"
    assert settled["result"] == json.dumps({"error": "workflow_effect_deadline_expired", "action_executed": False})


@pytest.mark.asyncio
async def test_tiktok_review_resume_cannot_begin_provider_write_after_deadline():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationLeaseFactory,
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    arguments = {
        "video_url": "https://media.example/video.mp4",
        "privacy_level": "SELF_ONLY",
        "_workflow_effect_deadline_at_from_context": "2020-01-01T00:00:00Z",
        "_workflow_effect_deadline_action_keys_from_context": ["social_post.publish"],
    }
    classification = classify_runtime_tool("mcp__tiktok__post_video", arguments)
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=RuntimeApprovalRequest(
            tool_name="mcp__tiktok__post_video", arguments=arguments,
            entity_id="entity-1", user_id="operator-1",
        ),
    )
    assert receipt is not None
    provider_writes: list[bool] = []

    def provider_handler(**_kwargs):
        provider_writes.append(True)
        return "unexpected post"

    result = await runtime_execute_prepared_tool_handler(
        tool_name="mcp__tiktok__post_video",
        handler=provider_handler,
        prepared=RuntimePreparedToolExecution(
            arguments=arguments, classification=classification,
            authorization_lease=RuntimeToolAuthorizationLeaseFactory.create(receipt),
        ),
        entity_id="entity-1", user_id="operator-1",
    )
    assert json.loads(result)["error"] == "workflow_effect_deadline_expired"
    assert provider_writes == []


def _run(variables: dict | None = None) -> WorkflowRun:
    return WorkflowRun(
        id="run1", workflow_id="wf1", entity_id="ent1",
        variables=variables or {}, step_results={},
    )


def test_scheduled_workflow_context_uses_legacy_fallback_only_without_trusted_context() -> None:
    from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context

    legacy = SimpleNamespace(
        trigger_source="schedule",
        trigger_data={
            "conversation_id": "legacy-conversation",
            "task_id": "legacy-task",
            "workflow_action_grant_id": "untrusted-grant",
        },
        variables={"context": {"conversation_id": "variable-conversation", "task_id": "variable-task"}},
    )
    authoritative = SimpleNamespace(
        trigger_source="schedule",
        trigger_data={
            "conversation_id": "legacy-conversation",
            "task_id": "legacy-task",
            "_workflow_runtime_context": {
                "conversation_id": "trusted-conversation",
                "task_id": "trusted-task",
                "workflow_action_grant_id": "trusted-grant",
            },
        },
        variables={"context": {"conversation_id": "variable-conversation", "task_id": "variable-task"}},
    )
    empty_authoritative = SimpleNamespace(
        trigger_source="schedule",
        trigger_data={
            "conversation_id": "legacy-conversation",
            "task_id": "legacy-task",
            "_workflow_runtime_context": {},
        },
        variables={"context": {"conversation_id": "variable-conversation", "task_id": "variable-task"}},
    )

    legacy_context = runtime_workflow_run_context(legacy)
    assert legacy_context["conversation_id"] == "legacy-conversation"
    assert legacy_context["task_id"] == "legacy-task"
    assert legacy_context["workflow_action_grant_id"] is None

    authoritative_context = runtime_workflow_run_context(authoritative)
    assert authoritative_context["conversation_id"] == "trusted-conversation"
    assert authoritative_context["task_id"] == "trusted-task"
    assert authoritative_context["workflow_action_grant_id"] == "trusted-grant"

    empty_context = runtime_workflow_run_context(empty_authoritative)
    assert empty_context["conversation_id"] is None
    assert empty_context["task_id"] is None


@pytest.fixture
def runner() -> WorkflowRunner:
    return WorkflowRunner()


def test_datetime_node_resolves_bound_amount_and_rejects_invalid_amount(runner):
    step = {
        "type": "datetime",
        "config": {
            "operation": "add",
            "value": "2026-01-01T00:00:00+00:00",
            "amount": "{{invoice.payment_terms_days}}",
            "unit": "days",
        },
    }
    result = runner._execute_datetime_step(step, {"invoice": {"payment_terms_days": 30}})
    assert result["status"] == "completed"
    assert result["output"].startswith("2026-01-31")
    assert runner._execute_datetime_step(
        step, {"invoice": {"payment_terms_days": "later"}}
    )["code"] == "datetime_amount_invalid"
    assert runner._execute_datetime_step(step, {"invoice": {}})["code"] == "datetime_amount_invalid"
    assert runner._execute_datetime_step(
        step, {"invoice": {"payment_terms_days": ""}}
    )["code"] == "datetime_amount_invalid"
    assert runner._execute_datetime_step(
        step, {"invoice": {"payment_terms_days": "NaN"}}
    )["code"] == "datetime_amount_invalid"


class _CheckpointDb:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


class _RunLoopDb:
    """Minimal AsyncSession protocol used by run-loop unit tests."""

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stage_executes_operations_checkpoints_and_exits_through_route(runner):
    run = _run({"request": "demo"})
    run.current_step_id = "prepare"
    run.execution_trace = []
    db = _CheckpointDb()
    step = {
        "id": "prepare",
        "type": "stage",
        "config": {
            "entry_operation_id": "normalize",
            "operations": [
                {
                    "id": "normalize",
                    "type": "transform",
                    "config": {"set": {"normalized": "{{request}}"}},
                    "next": ["summarize"],
                },
                {
                    "id": "summarize",
                    "type": "transform",
                    "config": {
                        "set": {"summary": "{{normalized}} ready"},
                        "output_var": "summary_output",
                    },
                    "next": ["continue"],
                },
            ],
            "routes": {"continue": "next_stage"},
        },
        "next": ["next_stage"],
    }

    result = await runner._execute_step_safe(step, run, db)

    assert result["status"] == "completed"
    assert result["next_override"] == ["next_stage"]
    assert run.current_step_id == "prepare"
    assert run.variables["normalized"] == "demo"
    assert run.variables["summary"] == "demo ready"
    state = run.variables["__stage_execution"]["prepare"]
    assert state["status"] == "completed"
    assert state["current_operation_id"] is None
    assert state["pending_operation_ids"] == []
    assert list(state["operation_results"]) == ["normalize", "summarize"]
    assert "output" not in state["operation_results"]["normalize"]
    assert run.variables["normalize"]["normalized"] == "demo"
    assert db.commit_count == 2
    assert [entry["node_id"] for entry in run.execution_trace] == [
        "prepare.normalize",
        "prepare.normalize",
        "prepare.summarize",
        "prepare.summarize",
    ]
    assert [entry["status"] for entry in run.execution_trace] == [
        "running",
        "completed",
        "running",
        "completed",
    ]


@pytest.mark.asyncio
async def test_stage_condition_can_select_a_terminal_route(runner):
    run = _run({"approved": False})
    db = _CheckpointDb()
    result = await runner._execute_step_safe(
        {
            "id": "review",
            "type": "stage",
            "config": {
                "entry_operation_id": "check",
                "operations": [{
                    "id": "check",
                    "type": "condition",
                    "config": {"expression": "approved == true"},
                    "true_next": ["continue"],
                    "false_next": ["needs_input"],
                }],
                "routes": {"continue": "publish", "needs_input": None},
            },
            "next": ["publish"],
        },
        run,
        db,
    )

    assert result["status"] == "completed"
    assert result["next_override"] == []
    assert result["selected_route"] == "needs_input"
    assert run.variables["__stage_execution"]["review"]["selected_route"] == (
        "needs_input"
    )
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_stage_failure_checkpoints_only_the_failed_operation_as_pending(runner):
    run = _run()
    db = _CheckpointDb()
    result = await runner._execute_step_safe(
        {
            "id": "produce",
            "type": "stage",
            "config": {
                "entry_operation_id": "prepare",
                "operations": [
                    {
                        "id": "prepare",
                        "type": "transform",
                        "config": {"set": {"prepared": True}},
                        "next": ["render"],
                    },
                    {
                        "id": "render",
                        "type": "stop",
                        "config": {"message": "renderer unavailable"},
                        "next": ["done"],
                    },
                ],
                "routes": {"done": "review"},
            },
            "next": ["review"],
        },
        run,
        db,
    )

    assert result["status"] == "failed"
    assert result["failed_operation_id"] == "render"
    assert result["error"] == "renderer unavailable"
    state = run.variables["__stage_execution"]["produce"]
    assert state["status"] == "failed"
    assert state["current_operation_id"] == "render"
    assert state["pending_operation_ids"] == ["render"]
    assert state["operation_results"]["prepare"]["status"] == "completed"
    assert state["operation_results"]["render"]["status"] == "failed"
    assert db.commit_count == 2


@pytest.mark.asyncio
async def test_foreach_subworkflow_cancelled_child_fails_parent_barrier(
    runner,
    monkeypatch,
):
    scenes = [{"scene_id": "scene-1"}]
    fingerprint = hashlib.sha256(
        json.dumps(scenes, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    progress_key = "__foreach_subworkflow__capture"
    run = _run(
        {
            "scenes": scenes,
            progress_key: {
                "input_fingerprint": fingerprint,
                "concurrency": 1,
                "items": [
                    {
                        "key": "scene-1",
                        "index": 0,
                        "item": scenes[0],
                        "status": "cancelled",
                        "subrun_id": "child-run-1",
                        "attempts": 1,
                        "output": None,
                        "error": "foreach_subworkflow child cancelled",
                    }
                ],
            },
        }
    )
    target = SimpleNamespace(
        id="child-workflow",
        entity_id=run.entity_id,
        steps=[{"id": "start", "type": "trigger", "next": []}],
    )

    class Result:
        def scalar_one_or_none(self):
            return target

    class FakeDB:
        async def execute(self, _statement):
            return Result()

    async def lock_reference(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.reusable_resource_locks."
        "lock_reusable_resource_reference",
        lock_reference,
    )

    result = await runner._execute_foreach_subworkflow_step(
        {
            "id": "capture",
            "type": "foreach_subworkflow",
            "config": {
                "workflow_id": target.id,
                "over": "scenes",
                "item_key": "scene_id",
            },
        },
        run.variables,
        run,
        FakeDB(),
    )

    assert result["status"] == "failed"
    assert result["error"] == "foreach_subworkflow child cancelled"
    assert result["items"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_trigger_and_end_are_passthrough(runner):
    trigger = await runner._execute_step(
        {"id": "trigger", "type": "trigger", "name": "trigger"}, _run(), None,
    )
    assert trigger == {"status": "completed", "output": "trigger"}

    end = await runner._execute_step(
        {"id": "end", "type": "end", "name": "Done"},
        _run({"upstream": "final answer"}),
        None,
    )
    assert end == {"status": "completed", "output": "final answer"}


@pytest.mark.asyncio
async def test_needs_input_end_carries_its_repair_hint(runner):
    end = await runner._execute_step(
        {"id": "needs_input", "type": "end", "name": "Explain", "config": {
            "retry_from_step_id": "prepare",
            "retry_state": {"observed_problem": "{{result.summary}}",
                            "editable_input_schema": {"type": "object", "properties": {"brief": {"type": "string"}}}},
        }},
        _run({"result": {"status": "needs_input", "summary": "No episode topic"}}),
        None,
    )
    assert end["status"] == "completed"
    assert end["retry_state"] == {
        "observed_problem": "No episode topic",
        "editable_input_schema": {"type": "object", "properties": {"brief": {"type": "string"}}},
        "retry_from_step_id": "prepare",
    }


@pytest.mark.asyncio
async def test_trigger_emits_runtime_payload(runner):
    run = _run()
    run.trigger_data = {"chatInput": "Explain Manor in one sentence"}
    result = await runner._execute_step(
        {"id": "chat", "type": "trigger", "name": "Chat"}, run, None,
    )
    assert result["output"] == {"chatInput": "Explain Manor in one sentence"}


@pytest.mark.asyncio
async def test_wait_node_honors_standing_always_approval(runner, monkeypatch):
    async def standing(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "packages.core.services.workflow_chat_approvals.workflow_wait_has_standing_approval",
        standing,
    )
    run = _run()
    run.started_by = "user-1"
    result = await runner._execute_step(
        {
            "id": "approve_publish",
            "type": "wait",
            "name": "Approve publish",
            "config": {
                "wait_type": "approval",
                "allow_always": True,
                "approval_action_key": "social_post.publish",
                "response_variable": "publish_decision",
            },
        },
        run,
        object(),
    )
    assert result["status"] == "completed"
    assert result["standing_approval"] is True
    assert result["output"] == {"choice": "always_approve", "standing": True}
    assert result["output_var"] == "publish_decision"


@pytest.mark.asyncio
async def test_run_loop_appends_trace_before_best_effort_chat_projection(
    runner,
    monkeypatch,
):
    run = _run({"prompt": "hello", "api_token": "never-store"})
    run.status = "running"
    run.trigger_source = "workspace_chat"
    run.trigger_data = {
        "attempt_number": 2,
        "_workspace_chat_entrypoint": {
            "enabled": True,
            "conversation_id": "conversation-1",
            "activity_message_id": "message-1",
        },
    }
    run.definition_snapshot = {"nodes": [{"id": "start"}]}
    run.execution_trace = []
    workflow = SimpleNamespace(steps=[{
        "id": "start",
        "name": "Start",
        "type": "trigger",
        "next": [],
    }])

    async def execute_step(_step, _run, _db):
        return {
            "status": "completed",
            "duration_ms": 4.25,
            "inputs": {"prompt": "hello", "api_token": "never-store"},
            "output": {"artifact_refs": [{"artifact_id": "artifact-1"}]},
        }

    trace_seen_by_projection: list[list[str]] = []

    async def project(_db, projector, **_kwargs):
        if projector.__name__ == "project_workflow_step":
            trace_seen_by_projection.append([
                entry["status"] for entry in run.execution_trace
            ])

    monkeypatch.setattr(runner, "_execute_step_safe", execute_step)
    monkeypatch.setattr(workflow_runner_module, "_project_workflow_chat_safely", project)

    await runner._run_loop(workflow, run, _RunLoopDb())

    assert trace_seen_by_projection == [["running"], ["running", "completed"]]
    assert [entry["status"] for entry in run.execution_trace] == [
        "running",
        "completed",
    ]
    assert run.execution_trace[1]["input_summary"]["api_token"] == "[REDACTED]"
    assert run.execution_trace[1]["artifact_refs"] == [{"id": "artifact-1"}]


@pytest.mark.asyncio
async def test_run_loop_persists_current_step_before_long_running_execution(
    runner,
    monkeypatch,
):
    run = _run()
    run.status = "running"
    run.current_step_id = "validate"
    run.step_results = {"validate": {"status": "completed", "output": True}}
    run.definition_snapshot = {
        "nodes": [
            {"id": "validate", "name": "Validate", "type": "condition"},
            {"id": "generate", "name": "Generate video", "type": "tool"},
        ]
    }
    run.execution_trace = []
    workflow = SimpleNamespace(steps=[
        {
            "id": "validate",
            "name": "Validate",
            "type": "condition",
            "depends_on": [],
        },
        {
            "id": "generate",
            "name": "Generate video",
            "type": "tool",
            "depends_on": ["validate"],
        },
    ])
    committed_current_step_ids: list[str | None] = []

    class Db(_RunLoopDb):
        async def commit(self):
            committed_current_step_ids.append(run.current_step_id)

    async def execute_step(step, _run, _db):
        assert committed_current_step_ids == ["generate"]
        return {"status": "completed", "output": step["id"]}

    monkeypatch.setattr(runner, "_execute_step_safe", execute_step)

    await runner._run_loop(workflow, run, Db())

    assert committed_current_step_ids[0] == "generate"


@pytest.mark.asyncio
async def test_run_loop_fences_and_closes_its_terminal_commit(runner):
    run = _run()
    run.status = "running"
    run.step_results = {"start": {"status": "completed"}}
    workflow = SimpleNamespace(steps=[{
        "id": "start",
        "type": "trigger",
        "next": [],
    }])
    events: list[str] = []

    class Db:
        async def flush(self):
            return None

        async def commit(self):
            events.append("commit")

    db = Db()

    async def terminal_commit():
        events.append("before")
        await db.commit()
        events.append("after")

    await runner._run_loop(
        workflow,
        run,
        db,
        terminal_commit=terminal_commit,
    )

    assert run.status == "completed"
    assert events == ["before", "commit", "after"]


@pytest.mark.asyncio
async def test_terminal_run_redelivery_drains_post_commit_effects(monkeypatch):
    run = _run()
    run.status = "failed"
    events = []

    class Result:
        def scalar_one_or_none(self):
            return run

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class Factory:
        def __call__(self):
            return Session()

    terminal_runner = WorkflowRunner(session_factory=Factory())

    async def finalize(settled, _db):
        events.append(("effects", settled.id))

    monkeypatch.setattr(terminal_runner, "_finalize_run_effects", finalize)

    await terminal_runner._run_claimed(
        run.id,
        after_terminal_commit=lambda: events.append(("marker", run.id)),
    )

    assert events == [("marker", run.id), ("effects", run.id)]


@pytest.mark.asyncio
async def test_terminal_effects_complete_only_after_every_effect_succeeds(
    runner,
    monkeypatch,
):
    from packages.core.ledger import adapters as ledger_adapters

    run = _run()
    run.status = "failed"
    events: list[str] = []

    class FakeDB:
        async def commit(self):
            events.append("commit")

    async def record(_db, _run):
        events.append("ledger")

    async def dispatch(_run, _db):
        events.append("error_handlers")

    async def propagate(_run, _db):
        events.append("propagate")

    monkeypatch.setattr(ledger_adapters, "record_workflow_run_status", record)
    monkeypatch.setattr(runner, "_dispatch_error_handlers", dispatch)
    monkeypatch.setattr(runner, "_propagate_subworkflow_result", propagate)

    await runner._finalize_run_effects(run, FakeDB())

    assert events == [
        "commit",
        "ledger",
        "commit",
        "error_handlers",
        "propagate",
        "commit",
    ]
    assert run.terminal_effects_completed_at is not None
    assert run.terminal_effects_next_attempt_at is None


@pytest.mark.asyncio
async def test_terminal_effect_failure_keeps_recovery_intent(
    runner,
    monkeypatch,
):
    from packages.core.ledger import adapters as ledger_adapters

    run = _run()
    run.status = "failed"

    class FakeDB:
        async def commit(self):
            return None

    async def record(_db, _run):
        return None

    async def fail_dispatch(_run, _db):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ledger_adapters, "record_workflow_run_status", record)
    monkeypatch.setattr(runner, "_dispatch_error_handlers", fail_dispatch)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await runner._finalize_run_effects(run, FakeDB())

    assert run.terminal_effects_completed_at is None
    assert run.terminal_effects_next_attempt_at is not None


@pytest.mark.asyncio
async def test_interactive_terminal_effect_failure_returns_committed_outcome(
    runner,
    monkeypatch,
):
    run = _run()
    run.status = "cancelled"
    workflow_runner_module.mark_workflow_terminal_effects_pending(run)
    rollbacks: list[bool] = []

    class FakeDB:
        async def rollback(self):
            rollbacks.append(True)

    async def fail_finalize(_run, _db):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(runner, "_finalize_run_effects", fail_finalize)

    completed = await (
        workflow_runner_module.finalize_workflow_terminal_effects_best_effort(
            run,
            FakeDB(),
            runner=runner,
            context="test cancellation",
        )
    )

    assert completed is False
    assert rollbacks == [True]
    assert run.status == "cancelled"
    assert run.terminal_effects_completed_at is None
    assert run.terminal_effects_next_attempt_at is not None


@pytest.mark.asyncio
async def test_workflow_tool_cancel_returns_state_when_effects_are_deferred(
    monkeypatch,
):
    from packages.core.ai.tools import workflow_tools
    from packages.core.services import workflow_chat_projection
    from packages.core.services import workflow_run_control
    from packages.core.services import workflow_service

    run = _run()
    run.status = "running"
    run.workspace_id = None
    run.binding_id = None
    run.trigger_source = "tool"
    run.current_step_id = "work"
    run.error = None
    run.started_at = None
    run.completed_at = None
    run.trigger_data = {}
    rollbacks: list[bool] = []
    cascade_calls: list[tuple[str, str]] = []
    discovery_calls: list[str] = []
    revoked_claims: list[list[str]] = []

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            return None

        async def rollback(self):
            rollbacks.append(True)

    async def get_run(*_args):
        return run

    async def can_control(*_args, **_kwargs):
        return True

    async def no_project(*_args, **_kwargs):
        return None

    async def discover(_db, parent):
        discovery_calls.append(parent.id)
        return []

    async def cascade(_db, parent, *, actor_id, descendant_run_ids=None):
        cascade_calls.append((parent.id, actor_id))
        assert descendant_run_ids == []
        return []

    async def revoke(_db, run_ids):
        revoked_claims.append(list(run_ids))
        return len(run_ids)

    async def fail_effects(_runner, _run, _db):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(workflow_tools, "async_session", lambda: FakeDB())
    monkeypatch.setattr(workflow_service, "get_run", get_run)
    monkeypatch.setattr(
        workflow_tools,
        "_user_can_control_workflow_run",
        can_control,
    )
    monkeypatch.setattr(
        workflow_chat_projection,
        "project_workflow_run_status",
        no_project,
    )
    monkeypatch.setattr(
        workflow_run_control,
        "active_subworkflow_descendant_ids",
        discover,
        raising=False,
    )
    monkeypatch.setattr(
        workflow_run_control,
        "cancel_active_subworkflow_descendants",
        cascade,
    )
    monkeypatch.setattr(
        "packages.core.services.workflow_run_execution_claim."
        "revoke_workflow_run_execution_claims",
        revoke,
        raising=False,
    )
    monkeypatch.setattr(WorkflowRunner, "_finalize_run_effects", fail_effects)

    response = json.loads(await workflow_tools._cancel_workflow_run(
        entity_id="ent1",
        user_id="user1",
        run_id=run.id,
    ))

    assert response["ok"] is True
    assert response["run"]["status"] == "cancelled"
    assert run.status == "cancelled"
    assert run.terminal_effects_next_attempt_at is not None
    assert cascade_calls == [(run.id, "user1")]
    assert discovery_calls == [run.id]
    assert revoked_claims == [[run.id]]
    assert rollbacks == [True]


@pytest.mark.asyncio
async def test_parallel_frontier_does_not_share_db_session_concurrently(
    runner,
    monkeypatch,
):
    run = _run()
    run.status = "running"
    run.trigger_data = {"attempt_number": 1}
    run.definition_snapshot = {
        "nodes": [{"id": node_id} for node_id in ("start", "slow", "fast")]
    }
    run.execution_trace = []
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["slow", "fast"]},
        {"id": "slow", "type": "tool", "next": []},
        {"id": "fast", "type": "tool", "next": []},
    ])

    active = 0
    max_active = 0

    async def execute(step, _run, _db):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "completed", "output": step["id"]}

    monkeypatch.setattr(runner, "_execute_step", execute)

    await runner._run_loop(workflow, run, _RunLoopDb())

    terminal_ids = [
        entry["node_id"]
        for entry in run.execution_trace
        if entry["status"] == "completed"
    ]
    assert terminal_ids == ["start", "slow", "fast"]
    assert list(run.step_results) == ["start", "slow", "fast"]
    assert max_active == 1


@pytest.mark.asyncio
async def test_run_loop_routes_handled_failure_only_to_error_output(
    runner,
    monkeypatch,
):
    run = _run()
    run.status = "running"
    run.trigger_data = {"attempt_number": 1}
    run.definition_snapshot = {
        "nodes": [
            {"id": node_id}
            for node_id in ("start", "request", "success", "recover")
        ]
    }
    run.execution_trace = []
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["request"]},
        {
            "id": "request",
            "type": "http",
            "config": {
                "on_error": "continue_error",
                "error_next": ["recover"],
            },
            "next": ["success"],
        },
        {"id": "success", "type": "transform", "next": []},
        {"id": "recover", "type": "transform", "next": []},
    ])

    executed: list[str] = []

    async def execute(step, _run, _db):
        executed.append(step["id"])
        if step["id"] == "request":
            return {"status": "failed", "error": "upstream unavailable"}
        return {"status": "completed", "output": step["id"]}

    monkeypatch.setattr(runner, "_execute_step_safe", execute)

    await runner._run_loop(workflow, run, _RunLoopDb())

    assert run.status == "completed"
    assert executed == ["start", "request", "recover"]
    assert run.step_results["request"]["continued"] is True
    assert run.step_results["request"]["next_override"] == ["recover"]
    assert run.step_results["request"]["output"] == {
        "error": {"message": "upstream unavailable"}
    }
    assert "success" not in run.step_results


@pytest.mark.asyncio
async def test_run_loop_executes_item_routes_with_branch_local_inputs(runner):
    items = [
        {"tier": "vip", "id": 1},
        {"tier": "free", "id": 2},
        {"tier": "vip", "id": 3},
    ]
    run = _run()
    run.status = "running"
    run.trigger_data = {"items": items}
    run.definition_snapshot = {
        "nodes": [
            {"id": node_id}
            for node_id in ("start", "switch", "vip", "free")
        ]
    }
    run.execution_trace = []
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["switch"]},
        {
            "id": "switch",
            "type": "switch",
            "config": {
                "cases": [{"expression": 'tier == "vip"', "next": ["vip"]}],
                "default_next": ["free"],
                "inputs": [{"key": "input", "value": "{{start.items}}"}],
                "pass_input": True,
            },
            "next": ["vip", "free"],
        },
        {
            "id": "vip",
            "type": "transform",
            "config": {
                "inputs": [{"key": "input", "value": "{{switch}}"}],
                "pass_input": True,
            },
            "next": [],
        },
        {
            "id": "free",
            "type": "transform",
            "config": {
                "inputs": [{"key": "input", "value": "{{switch}}"}],
                "pass_input": True,
            },
            "next": [],
        },
    ])

    await runner._run_loop(workflow, run, _RunLoopDb())

    assert run.status == "completed"
    assert run.step_results["vip"]["output"] == [items[0], items[2]]
    assert run.step_results["free"]["output"] == [items[1]]


def test_continue_regular_output_forwards_last_valid_input(runner):
    run = _run({"source": {"id": 7, "name": "Ada"}})
    step = {
        "id": "request",
        "type": "http",
        "config": {
            "on_error": "continue",
            "inputs": [{"key": "input", "value": "{{source}}"}],
        },
        "next": ["after"],
    }
    result = {"status": "failed", "error": "upstream unavailable"}

    assert workflow_runner_module._continue_failed_result(step, result, run) is True
    runner._record_step_result(step, result, run)

    assert result["output"] == {"id": 7, "name": "Ada"}
    assert run.variables["request"] == {"id": 7, "name": "Ada"}


def test_continue_error_output_merges_error_with_input_item(runner):
    run = _run({"source": {"id": 7, "name": "Ada"}})
    step = {
        "id": "request",
        "type": "http",
        "config": {
            "on_error": "continue_error",
            "error_next": ["recover"],
            "inputs": [{"key": "input", "value": "{{source}}"}],
        },
        "next": ["success"],
    }
    result = {
        "status": "failed",
        "error": {"message": "upstream unavailable", "code": "EHOSTUNREACH"},
    }

    assert workflow_runner_module._continue_failed_result(step, result, run) is True
    runner._record_step_result(step, result, run)

    assert result["next_override"] == ["recover"]
    assert result["output"] == {
        "id": 7,
        "name": "Ada",
        "error": {"message": "upstream unavailable", "code": "EHOSTUNREACH"},
    }
    assert run.variables["request"] == result["output"]


@pytest.mark.asyncio
async def test_end_prefers_explicit_input_binding(runner):
    step = {
        "id": "end", "type": "end", "name": "Done",
        "config": {"inputs": [{"key": "input", "value": "{{summary}}"}]},
    }
    result = await runner._execute_step(step, _run({"summary": {"text": "digest"}}), None)
    assert result["output"] == {"text": "digest"}


@pytest.mark.asyncio
async def test_end_returns_its_declared_output_and_never_runtime_bookkeeping(runner):
    """An end's declared ``output`` is the result; a launch key recorded last is never shown as one."""
    variables = {"qa": {"summary": "Ready to watch.", "passed": True},
                 "_workspace_flow_launch_key": "manual:01M6370XY6Y65ZK4K70GTZB3JH:01M62TVP9KCPHKZ6N5NKMD7ZPG"}
    declared = {"id": "done", "type": "end", "name": "Done",
                "config": {"output": {"summary": "{{qa.summary}}", "qa_passed": "{{qa.passed}}"}}}
    result = await runner._execute_step(declared, _run(dict(variables)), None)
    assert result["output"] == {"summary": "Ready to watch.", "qa_passed": True}
    # Without a declaration it passes the latest business value through, not the launch key.
    result = await runner._execute_step({"id": "done", "type": "end", "name": "Done"}, _run(dict(variables)), None)
    assert result["output"] == {"summary": "Ready to watch.", "passed": True}


def test_final_run_output_prefers_end_then_latest_business_step():
    steps = [
        {"id": "start", "type": "trigger", "name": "Start"},
        {"id": "llm", "type": "llm", "name": "Summarize"},
        {"id": "done", "type": "end", "name": "Done"},
    ]
    helper = workflow_runner_module._final_run_output
    assert helper(steps, {
        "start": {"status": "completed", "output": "Start"},
        "llm": {"status": "completed", "output": "digest"},
        "done": {"status": "completed", "output": "digest"},
    }) == "digest"
    # Compatibility with runs created before End passed data through.
    assert helper(steps, {
        "start": {"status": "completed", "output": "Start"},
        "llm": {"status": "completed", "output": "legacy digest"},
        "done": {"status": "completed", "output": "Done"},
    }) == "legacy digest"


def test_transform_recursively_resolves_nested_bindings(runner):
    run = _run({
        "project": {
            "project_id": "project-1",
            "state": {
                "project_root": "Product Videos/project-1",
                "retry_state": {
                    "observed_problem": ["Workspace route was not observed"],
                },
            },
        },
    })
    result = runner._execute_transform_step({
        "id": "build_handoff",
        "type": "transform",
        "config": {
            "set": {
                "input": {
                    "project_id": "{{project.project_id}}",
                    "project_root": "{{project.state.project_root}}",
                    "observed_problem": "{{project.state.retry_state.observed_problem}}",
                },
            },
        },
    }, run.variables, run)

    assert result["output"]["input"] == {
        "project_id": "project-1",
        "project_root": "Product Videos/project-1",
        "observed_problem": ["Workspace route was not observed"],
    }


@pytest.mark.asyncio
async def test_unsupported_node_skips_by_default(runner):
    run = _run()
    run.execution_snapshot = {"import_source": "comfyui"}
    step = {
        "id": "ks", "type": "unsupported", "name": "KSampler",
        "meta": {
            "source_tool": "comfyui",
            "original_type": "KSampler",
            "unmapped": True,
        },
    }
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "completed"
    assert res["skipped"] is True
    assert "KSampler" in res["output"]


@pytest.mark.asyncio
async def test_unsupported_node_can_fail_when_configured(runner):
    run = _run()
    run.execution_snapshot = {"import_source": "comfyui"}
    step = {
        "id": "ks", "type": "unsupported", "name": "KSampler",
        "config": {"on_unsupported": "fail"},
        "meta": {
            "source_tool": "comfyui",
            "original_type": "KSampler",
            "unmapped": True,
        },
    }
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "failed"


@pytest.mark.asyncio
async def test_switch_routes_to_first_matching_case(runner):
    run = _run({"tier": "vip"})
    step = {
        "id": "sw", "type": "switch",
        "config": {
            "cases": [
                {"expression": 'tier == "free"', "next": ["a"]},
                {"expression": 'tier == "vip"', "next": ["b"]},
            ],
            "default_next": ["c"],
        },
    }
    res = await runner._execute_step(step, run, None)
    assert res["next_override"] == ["b"]


@pytest.mark.asyncio
async def test_switch_falls_back_to_default(runner):
    run = _run({"tier": "unknown"})
    step = {
        "id": "sw", "type": "switch",
        "config": {
            "cases": [{"expression": 'tier == "vip"', "next": ["b"]}],
            "default_next": ["c"],
        },
    }
    res = await runner._execute_step(step, run, None)
    assert res["next_override"] == ["c"]


@pytest.mark.asyncio
async def test_switch_expression_mode_routes_index_and_passes_input(runner):
    item = {"route": 1, "payload": "keep me"}
    run = _run({"source": item})
    step = {
        "id": "sw",
        "type": "switch",
        "config": {
            "switch_mode": "expression",
            "output_index": "{{input.route}}",
            "output_next": [["zero"], ["one"], ["two"]],
            "inputs": [{"key": "input", "value": "{{source}}"}],
            "pass_input": True,
        },
    }

    res = await runner._execute_step(step, run, None)

    assert res["status"] == "completed"
    assert res["next_override"] == ["one"]
    assert res["output"] == item


@pytest.mark.asyncio
async def test_switch_expression_mode_allows_an_unconnected_output(runner):
    run = _run({"input": {"route": 2}})
    step = {
        "id": "sw",
        "type": "switch",
        "config": {
            "switch_mode": "expression",
            "output_index": "{{input.route}}",
            "output_next": [["zero"], [], []],
            "pass_input": True,
        },
    }

    res = await runner._execute_step(step, run, None)

    assert res["status"] == "completed"
    assert res["next_override"] == []
    assert res["output"] == {"route": 2}


@pytest.mark.asyncio
async def test_switch_partitions_item_stream_into_branch_local_inputs(runner):
    items = [
        {"tier": "vip", "id": 1},
        {"tier": "free", "id": 2},
        {"tier": "vip", "id": 3},
    ]
    run = _run({"source": items})
    switch = {
        "id": "sw",
        "type": "switch",
        "config": {
            "cases": [{"expression": 'tier == "vip"', "next": ["vip"]}],
            "default_next": ["free"],
            "inputs": [{"key": "input", "value": "{{source}}"}],
            "pass_input": True,
        },
        "next": ["vip", "free"],
    }

    result = await runner._execute_step(switch, run, None)
    runner._record_step_result(switch, result, run)

    assert result["next_override"] == ["vip", "free"]
    assert result["route_outputs"] == {
        "vip": [{"tier": "vip", "id": 1}, {"tier": "vip", "id": 3}],
        "free": [{"tier": "free", "id": 2}],
    }

    vip = await runner._execute_step({
        "id": "vip",
        "type": "transform",
        "config": {
            "inputs": [{"key": "input", "value": "{{sw}}"}],
            "pass_input": True,
        },
    }, run, None)
    free = await runner._execute_step({
        "id": "free",
        "type": "transform",
        "config": {
            "inputs": [{"key": "input", "value": "{{sw}}"}],
            "pass_input": True,
        },
    }, run, None)

    assert vip["output"] == [items[0], items[2]]
    assert free["output"] == [items[1]]


@pytest.mark.asyncio
async def test_switch_can_send_one_item_to_all_matching_outputs(runner):
    item = {"score": 10}
    result = await runner._execute_step({
        "id": "sw",
        "type": "switch",
        "config": {
            "cases": [
                {"expression": "score > 5", "next": ["high"]},
                {"expression": "score > 0", "next": ["positive"]},
            ],
            "all_matching_outputs": True,
            "inputs": [{"key": "input", "value": "{{source}}"}],
            "pass_input": True,
        },
    }, _run({"source": item}), None)

    assert result["next_override"] == ["high", "positive"]
    assert result["route_outputs"] == {"high": item, "positive": item}


@pytest.mark.asyncio
async def test_condition_partitions_item_stream_into_both_outputs(runner):
    items = [{"active": True, "id": 1}, {"active": False, "id": 2}]
    run = _run({"source": items})
    condition = {
        "id": "if",
        "type": "condition",
        "config": {
            "expression": "active == true",
            "inputs": [{"key": "input", "value": "{{source}}"}],
            "pass_input": True,
        },
        "true_next": ["active"],
        "false_next": ["inactive"],
        "next": ["active", "inactive"],
    }

    result = await runner._execute_step(condition, run, None)
    runner._record_step_result(condition, result, run)

    assert result["next_override"] == ["active", "inactive"]
    assert result["route_outputs"] == {
        "active": [items[0]],
        "inactive": [items[1]],
    }
    inactive = await runner._execute_step({
        "id": "inactive",
        "type": "transform",
        "config": {
            "inputs": [{"key": "input", "value": "{{if}}"}],
            "pass_input": True,
        },
    }, run, None)
    assert inactive["output"] == [items[1]]


@pytest.mark.asyncio
async def test_execute_once_limits_an_item_stream_to_the_first_item(runner):
    run = _run({"source": [{"value": 1}, {"value": 2}]})
    step = {
        "id": "set",
        "type": "transform",
        "config": {
            "execute_once": True,
            "items": "{{source}}",
            "set": {"copied": "{{value}}"},
        },
    }

    res = await runner._execute_step(step, run, None)

    assert res["output"] == [{"copied": 1}]


@pytest.mark.asyncio
async def test_execute_once_limits_each_merge_input_stream(runner):
    run = _run({
        "left": [{"left": 1}, {"left": 2}],
        "right": [{"right": "a"}, {"right": "b"}],
    })
    step = {
        "id": "merge",
        "type": "merge",
        "config": {
            "execute_once": True,
            "mode": "combine_by_position",
            "sources": ["left", "right"],
        },
    }

    res = await runner._execute_step(step, run, None)

    assert res["output"] == [{"left": 1, "right": "a"}]


@pytest.mark.asyncio
async def test_always_output_data_emits_empty_item_for_empty_output(runner, monkeypatch):
    async def empty(_step, _run, _db):
        return {"status": "completed", "output": []}

    monkeypatch.setattr(runner, "_execute_step", empty)
    result = await runner._execute_step_safe(
        {
            "id": "empty",
            "type": "transform",
            "config": {"always_output_data": True},
        },
        _run(),
        None,
    )

    assert result["status"] == "completed"
    assert result["output"] == [{}]


@pytest.mark.asyncio
async def test_merge_aggregates_variables_as_list(runner):
    run = _run({"a": 1, "b": 2})
    step = {
        "id": "m", "type": "merge",
        "config": {"sources": ["a", "b"], "output_var": "combined"},
    }
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "completed"
    assert res["output"] == [1, 2]
    assert res["output_var"] == "combined"


@pytest.mark.asyncio
async def test_merge_dict_mode(runner):
    run = _run({"a": 1, "b": 2})
    step = {
        "id": "m", "type": "merge",
        "config": {"sources": ["a", "b"], "mode": "dict", "output_var": "combined"},
    }
    res = await runner._execute_step(step, run, None)
    assert res["output"] == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_merge_combines_item_streams_by_position(runner):
    run = _run({
        "titles": [{"title": "A"}, {"title": "B"}],
        "summaries": [{"response": {"text": "one"}}, {"response": {"text": "two"}}],
    })
    res = await runner._execute_step({
        "id": "merge", "type": "merge",
        "config": {"sources": ["titles", "summaries"], "mode": "combine_by_position"},
    }, run, None)
    assert res["output"] == [
        {"title": "A", "response": {"text": "one"}},
        {"title": "B", "response": {"text": "two"}},
    ]


@pytest.mark.asyncio
async def test_unknown_type_still_fails(runner):
    run = _run()
    res = await runner._execute_step({"id": "x", "type": "totally-bogus"}, run, None)
    assert res["status"] == "failed"


# ── External-call nodes: http (mocked) + llm/connector/rag routing ──

class _FakeResp:
    def __init__(self, status=200, payload=None, text="ok", content=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = content if content is not None else text.encode()
        self.headers = headers or {}
        self.is_success = 200 <= status < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


async def _allow_public_http(_url: str) -> None:
    return None


@pytest.mark.asyncio
async def test_http_node_success(runner, monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        assert method == "POST"
        return _FakeResp(200, {"echo": url, "method": method})

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    monkeypatch.setattr(workflow_runner_module, "_resolve_workflow_http_host", _allow_public_http)
    run = _run({"host": "example.com"})
    step = {
        "id": "h", "type": "http",
        "config": {"url": "https://{{host}}/api", "method": "post", "output_var": "resp"},
    }
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "completed"
    assert res["output"]["status_code"] == 200
    assert res["output"]["body"]["echo"] == "https://example.com/api"
    assert res["output"]["data"] == res["output"]["body"]
    assert res["output_var"] == "resp"


@pytest.mark.asyncio
async def test_http_node_can_return_binary_for_extract_from_file(runner, monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        return _FakeResp(
            200, content=b"spreadsheet-bytes",
            headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        )

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    monkeypatch.setattr(workflow_runner_module, "_resolve_workflow_http_host", _allow_public_http)
    res = await runner._execute_step({
        "id": "download", "type": "http",
        "config": {"url": "https://example.com/leads.xlsx", "response_format": "binary"},
    }, _run(), None)
    assert res["status"] == "completed"
    assert res["output"]["content_type"].startswith("application/vnd.openxmlformats")
    import base64
    assert base64.b64decode(res["output"]["body_base64"]) == b"spreadsheet-bytes"


@pytest.mark.asyncio
async def test_http_node_missing_url_fails(runner):
    run = _run()
    res = await runner._execute_step({"id": "h", "type": "http", "config": {}}, run, None)
    assert res["status"] == "failed"


@pytest.mark.asyncio
async def test_http_node_4xx_marks_failed(runner, monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        return _FakeResp(404, {"error": "nope"})

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    monkeypatch.setattr(workflow_runner_module, "_resolve_workflow_http_host", _allow_public_http)
    run = _run()
    step = {"id": "h", "type": "http", "config": {"url": "https://example.com/y"}}
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "failed"
    assert "404" in res["error"]


@pytest.mark.asyncio
async def test_http_batch_renders_each_item_and_query(runner, monkeypatch):
    seen = []

    async def fake_request(self, method, url, **kwargs):
        seen.append((url, kwargs.get("params")))
        return _FakeResp(200, {"url": url})

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    monkeypatch.setattr(workflow_runner_module, "_resolve_workflow_http_host", _allow_public_http)
    res = await runner._execute_step({
        "id": "pages", "type": "http", "config": {
            "url": "https://example.com/{{slug}}", "batch": True,
            "items": "{{rows}}", "query": {"page": "{{page}}"},
        },
    }, _run({"rows": [{"slug": "one", "page": 1}, {"slug": "two", "page": 2}]}), None)
    assert res["status"] == "completed"
    assert seen == [
        ("https://example.com/one", {"page": "1"}),
        ("https://example.com/two", {"page": "2"}),
    ]
    assert len(res["output"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://localhost/admin",
        "http://metadata.internal/latest",
        "file:///etc/passwd",
    ],
)
async def test_http_node_blocks_non_public_destinations_before_request(
    runner,
    monkeypatch,
    url,
):
    called = False

    async def fake_request(*_args, **_kwargs):
        nonlocal called
        called = True
        return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    result = await runner._execute_step(
        {"id": "h", "type": "http", "config": {"url": url}},
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert "public" in result["error"].lower()
    assert called is False


@pytest.mark.asyncio
async def test_http_node_blocks_redirect_to_private_network(runner, monkeypatch):
    calls: list[str] = []

    async def fake_request(_self, _method, url, **_kwargs):
        calls.append(url)
        return _FakeResp(302, headers={"location": "http://127.0.0.1/admin"})

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
    monkeypatch.setattr(workflow_runner_module, "_resolve_workflow_http_host", _allow_public_http)

    result = await runner._execute_step(
        {"id": "h", "type": "http", "config": {"url": "https://example.com/start"}},
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert "public" in result["error"].lower()
    assert calls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_http_node_blocks_hostname_resolving_to_private_network(runner, monkeypatch):
    monkeypatch.setattr(
        workflow_runner_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.0.0.4", 443))],
    )

    result = await runner._execute_step(
        {"id": "h", "type": "http", "config": {"url": "https://example.test/data"}},
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert "private network" in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme,port,url_host,tls_host", [
    ("https", 443, "example.test", "example.test"),
    ("http", 80, "example.test", None),
    ("https", 443, "[2606:4700:4700:0000:0000:0000:0000:1111]", "2606:4700:4700::1111"),
])
async def test_http_node_connects_to_validated_ip_without_resolving_again(runner, monkeypatch, scheme, port, url_host, tls_host):
    import httpcore
    from httpcore._backends.auto import AutoBackend

    dns_calls = []
    connections = []
    writes = []
    tls_hosts = []

    def rebinding_dns(hostname, resolved_port, **_kwargs):
        dns_calls.append((hostname, resolved_port))
        address = "93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1"
        return [(2, 1, 6, "", (address, resolved_port))]

    class Stream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            writes.append(buffer)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls_hosts.append(server_hostname)
            return self

    class NetworkBoundary:
        async def connect_tcp(self, host, connected_port, **_kwargs):
            connections.append((host, connected_port))
            return Stream([b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{"ok":true}'])

    async def forbid_unpinned_connection(*_args, **_kwargs):
        raise AssertionError("Workflow HTTP must use the validated-IP transport")

    monkeypatch.setattr(workflow_runner_module.socket, "getaddrinfo", rebinding_dns)
    monkeypatch.setattr(httpcore, "AnyIOBackend", NetworkBoundary)
    monkeypatch.setattr(AutoBackend, "connect_tcp", forbid_unpinned_connection)

    result = await runner._execute_http_step(
        {"id": "h", "type": "http", "config": {"url": f"{scheme}://{url_host}/data"}}, {},
    )

    assert result["status"] == "completed"
    assert result["output"]["status_code"] == 200
    assert result["output"]["data"] == {"ok": True}
    assert dns_calls == [(tls_host or "example.test", port)]
    assert connections == [("93.184.216.34", port)]
    assert f"Host: {url_host}\r\n".encode() in b"".join(writes)
    # TLS verifies the original URL identity; canonicalization is only for the
    # DNS/connection target comparison, including equivalent IPv6 spellings.
    assert tls_hosts == ([url_host.strip("[]")] if scheme == "https" else [])


@pytest.mark.asyncio
async def test_http_node_preserves_cookies_across_pinned_redirect_hops(runner, monkeypatch):
    import httpcore

    requests = []
    responses = iter([
        b'HTTP/1.1 302 Found\r\nLocation: /logout\r\nSet-Cookie: fixture=ok; Path=/\r\nContent-Length: 0\r\n\r\n',
        b'HTTP/1.1 302 Found\r\nLocation: /final\r\nSet-Cookie: fixture=; Max-Age=0; Path=/\r\nContent-Length: 0\r\n\r\n',
        b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{"ok":true}',
    ])
    class Stream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            requests.append(buffer)
    class NetworkBoundary:
        async def connect_tcp(self, host, port, **_kwargs):
            assert host == "93.184.216.34"
            return Stream([next(responses)])
    monkeypatch.setattr(workflow_runner_module.socket, "getaddrinfo", lambda hostname, port, **kwargs: [(2, 1, 6, "", ("93.184.216.34", port))])
    monkeypatch.setattr(httpcore, "AnyIOBackend", NetworkBoundary)
    result = await runner._execute_http_step(
        {"id": "h", "type": "http", "config": {"url": "https://example.test/start"}}, {},
    )
    assert result["status"] == "completed"
    logout_request = next(value for value in requests if value.startswith(b"GET /logout "))
    final_request = next(value for value in requests if value.startswith(b"GET /final "))
    assert b"Cookie: fixture=ok\r\n" in logout_request
    assert b"Cookie:" not in final_request


@pytest.mark.asyncio
async def test_parallel_node_does_not_share_db_session_concurrently(runner, monkeypatch):
    active = 0
    max_active = 0

    async def execute(_step, _run, _db):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "completed", "output": "ok"}

    monkeypatch.setattr(runner, "_execute_step_safe", execute)
    result = await runner._execute_parallel_step(
        {
            "id": "parallel",
            "config": {"steps": [{"id": "one"}, {"id": "two"}]},
        },
        {},
        "ent1",
        _run(),
        object(),
    )

    assert result["status"] == "completed"
    assert max_active == 1


@pytest.mark.asyncio
async def test_llm_node_routes_to_agent_step(runner, monkeypatch):
    seen = {}

    async def fake_agent(step, variables, entity_id, user_id, runtime_context, db):
        seen["called"] = True
        return {"status": "completed", "output": "drafted"}

    monkeypatch.setattr(runner, "_execute_agent_step", fake_agent)
    res = await runner._execute_step({"id": "l", "type": "llm", "config": {}}, _run(), None)
    assert seen.get("called") and res["output"] == "drafted"


def test_agent_contract_failure_names_the_tool_error_not_the_provider():
    """A step that spent its rounds on a failing tool reports that tool's error."""
    import json

    from packages.core.ai.runtime.tool_error_contract import tool_error_payload
    from packages.core.ai.workflow_runner import _agent_loop_failure_message

    rejected = tool_error_payload(
        tool_name="record_stat_assessment", code="stat_assessment_invalid",
        message="idempotency key already records a different assessment",
    )
    result = SimpleNamespace(
        content="", control=None, stop_reason="error", error="final_response_validation_failed",
        error_detail=tool_error_payload(
            tool_name="stat_assessment", code="measurement_receipt_missing",
            message="Required assessments have not been persisted for this run.",
        ),
        messages=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "manor", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": json.dumps(rejected)},
        ],
    )

    message = _agent_loop_failure_message(result)

    assert "Model provider" not in message
    assert message == (
        "Agent step ended without meeting its completion requirement. "
        "Required assessments have not been persisted for this run. Last tool error: "
        "manor: stat_assessment_invalid: idempotency key already records a different assessment"
    )


@pytest.mark.asyncio
async def test_agent_provider_failure_is_not_reparsed_as_invalid_json(runner, monkeypatch):
    async def fake_appendix(*args, **kwargs):
        return SimpleNamespace(
            prompt_appendix="",
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=None,
        )

    async def fake_loop(**kwargs):
        return SimpleNamespace(
            content=(
                "Sorry, the request failed before the model could respond. "
                "Please check the selected model and API key configuration.\n\n"
                "Error detail: ConnectError:"
            ),
            usage={},
            tool_calls_made=[],
            rounds=1,
            stop_reason="error",
            error="llm_call_failed",
            error_detail=None,
        )

    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_prepare_prompt_appendix_for_turn",
        fake_appendix,
    )
    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_execute_workflow_agent_loop",
        fake_loop,
    )

    result = await runner._execute_agent_step(
        {
            "id": "prepare",
            "type": "agent",
            "config": {
                "prompt": "Prepare the project.",
                "output_format": "json",
                "output_schema": {"type": "object"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
        None,
    )

    assert result["status"] == "failed"
    assert result["error"] == (
        "Model provider request failed before producing a response. ConnectError:"
    )
    assert "valid JSON" not in result["error"]


@pytest.mark.asyncio
async def test_agent_catalog_refresh_releases_writes_before_nested_tools(db_session, monkeypatch):
    """A refreshed Skill must be visible and unlocked to a nested invocation."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.models.skill import Skill

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    skill = Skill(
        id=generate_ulid(), name="event-planning-probe", slug="event-planning-probe",
        description="original", system_prompt="Plan an event.", status="active",
    )
    db_session.add(skill)
    await db_session.commit()
    skill_id = skill.id
    pending_id = generate_ulid()
    db_session.add(Skill(
        id=pending_id, name="uncommitted-event", slug="uncommitted-event",
        system_prompt="Caller-owned change.", status="active",
    ))

    async def refresh_catalog(db, **_kwargs):
        row = await db.get(Skill, skill_id)
        row.description = "refreshed"
        await db.flush()
        return SimpleNamespace(
            prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None,
        )

    async def nested_tool(**_kwargs):
        async with factory() as nested:
            row = (await nested.execute(
                select(Skill).where(Skill.id == skill_id).with_for_update(nowait=True)
            )).scalar_one()
            assert row.description == "refreshed"
            assert await nested.get(Skill, pending_id) is None
        return SimpleNamespace(
            content="Event plan ready", usage={}, tool_calls_made=[], rounds=1,
            stop_reason="completed", error=None, error_detail=None,
        )

    monkeypatch.setattr(workflow_runner_module, "runtime_prepare_prompt_appendix_for_turn", refresh_catalog)
    monkeypatch.setattr(workflow_runner_module, "runtime_execute_workflow_agent_loop", nested_tool)
    result = await WorkflowRunner(session_factory=factory)._execute_agent_step(
        {"id": "plan_event", "type": "agent", "config": {"prompt": "Plan an event."}},
        {}, "entity-1", "user-1", {}, db_session,
    )
    assert result["status"] == "completed"
    assert result["output"] == "Event plan ready"


@pytest.mark.asyncio
async def test_agent_step_releases_read_transaction_before_workflow_agent_loop(
    runner,
    monkeypatch,
):
    order = []

    async def fake_appendix(*args, **kwargs):
        order.append("appendix")
        return SimpleNamespace(
            prompt_appendix="",
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=None,
        )

    async def fake_release(_db):
        order.append("release")
        return True

    async def fake_loop(**kwargs):
        order.append("loop")
        return SimpleNamespace(
            content="ok",
            usage={},
            tool_calls_made=[],
            rounds=1,
            stop_reason="completed",
            error=None,
            error_detail=None,
        )

    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_prepare_prompt_appendix_for_turn",
        fake_appendix,
    )
    monkeypatch.setattr(
        workflow_runner_module,
        "release_read_transaction",
        fake_release,
    )
    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_execute_workflow_agent_loop",
        fake_loop,
    )

    result = await runner._execute_agent_step(
        {
            "id": "prepare",
            "type": "agent",
            "config": {"prompt": "Collect release materials."},
        },
        {},
        "entity-1",
        "user-1",
        {},
        object(),
    )

    assert order == ["release", "appendix", "release", "loop"]
    assert result["status"] == "completed"
    assert result["output"] == "ok"


@pytest.mark.asyncio
async def test_workflow_skill_captures_approval_gates_and_only_resumes_saved_checkpoints(
    runner,
    monkeypatch,
):
    """All calls capture gates; only approval resume carries a saved checkpoint."""

    seen: list[dict] = []
    approval_resume = None

    async def fake_resume(*_args, **_kwargs):
        return approval_resume

    async def fake_get_skill(*_args, **_kwargs):
        return SimpleNamespace(id="chrome", slug="chrome", name="chrome")

    async def fake_release(_db):
        return None

    async def fake_invoke_skill(*_args, **kwargs):
        seen.append(kwargs)
        return {
            "content": '{"saved":true}',
            "usage": {},
            "tools_used": [],
            "stop_reason": "completed",
            "control": {},
        }

    monkeypatch.setattr(runner, "_native_approval_resume", fake_resume)
    monkeypatch.setattr("packages.core.services.skill_service.get_skill", fake_get_skill)
    monkeypatch.setattr("packages.core.services.skill_service._determine_skill_type", lambda _skill: "prompt")
    monkeypatch.setattr(workflow_runner_module, "release_read_transaction", fake_release)
    monkeypatch.setattr(workflow_runner_module, "runtime_invoke_skill", fake_invoke_skill)

    step = {
        "id": "upload",
        "type": "agent",
        "config": {"skill": "chrome", "input": "Upload the approved video", "output_format": "json"},
    }
    for _ in range(2):
        result = await runner._execute_agent_step(
            step, {}, "entity-1", "user-1", {}, object(),
        )
        assert result["status"] == "completed"

    approval_resume = {
        "agent_id": None,
        "checkpoint": {"messages": [], "pending_tool_call": {"id": "resume", "name": "invoke_skill", "arguments": {}}},
    }
    result = await runner._execute_agent_step(
        step, {}, "entity-1", "user-1", {}, object(),
    )

    assert result["status"] == "completed"
    assert [call["hitl_resume_checkpoint"] for call in seen] == [True, True, True]
    assert seen[0]["runtime_checkpoint"] is None
    assert seen[1]["runtime_checkpoint"] is None
    assert seen[2]["runtime_checkpoint"] == approval_resume["checkpoint"]




_UNCONFIRMED_CONTROL = {
    "kind": "tool_error",
    "error_reason": "tool_outcome_unconfirmed",
    "terminal_failure": True,
    "retryable": False,
    "do_not_replay": True,
    "effect_state": "unconfirmed",
    "child_skill_results": [{"skill_id": "delivery-skill", "result": {"content": "Receipt exists: report-42"}}],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "control", "tool_evidence"),
    [
        # The unconfirmed tool error carries the replay fields on its control.
        ("tool_error", _UNCONFIRMED_CONTROL, {}),
        # A stop with no outcome control: only the receipt ledger knows.
        ("error", None, {"effect_state": "unconfirmed", "unconfirmed_calls": [{"call_id": "c1"}]}),
    ],
)
async def test_agent_step_failure_carries_replay_fields(
    runner,
    monkeypatch,
    stop_reason,
    control,
    tool_evidence,
):
    async def fake_appendix(*args, **kwargs):
        return SimpleNamespace(
            prompt_appendix="",
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=None,
        )

    async def fake_loop(**kwargs):
        return SimpleNamespace(
            content="The publish request timed out; its outcome is unknown.",
            usage={},
            tool_calls_made=["publish_post"],
            rounds=1,
            stop_reason=stop_reason,
            error="publish timed out" if control is None else None,
            error_detail=None,
            control=control,
            tool_evidence=tool_evidence,
        )

    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_prepare_prompt_appendix_for_turn",
        fake_appendix,
    )
    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_execute_workflow_agent_loop",
        fake_loop,
    )

    result = await runner._execute_agent_step(
        {
            "id": "publish",
            "type": "agent",
            "config": {
                "prompt": "Publish the post.",
                "output_format": "json",
                "output_schema": {"type": "object"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
        None,
    )

    assert result["status"] == "failed"
    assert result["do_not_replay"] is True
    if control is not None:
        assert result["retryable"] is False
        assert result["child_skill_results"] == control["child_skill_results"]
    assert result["effect_state"] == "unconfirmed"


@pytest.mark.asyncio
async def test_llm_batch_runs_once_per_item_and_wraps_output(runner, monkeypatch):
    prompts = []

    async def fake_agent(step, variables, entity_id, user_id, runtime_context, db):
        prompts.append(workflow_runner_module._render_template(step["config"]["prompt"], variables))
        return {"status": "completed", "output": f"summary:{variables['input']['data']}"}

    monkeypatch.setattr(runner, "_execute_agent_step", fake_agent)
    result = await runner._execute_step({
        "id": "summaries", "type": "llm", "config": {
            "batch": True, "items": "{{documents}}", "prompt": "Summarize {{input.data}}",
            "response_wrapper": "response.text",
        },
    }, _run({"documents": [{"data": "A"}, {"data": "B"}]}), None)
    assert prompts == ["Summarize A", "Summarize B"]
    assert result["output"] == [
        {"response": {"text": "summary:A"}},
        {"response": {"text": "summary:B"}},
    ]


@pytest.mark.asyncio
async def test_connector_node_routes_to_tool_step(runner, monkeypatch):
    seen = {}

    async def fake_tool(step, variables, entity_id, user_id, runtime_context):
        seen["called"] = True
        return {"status": "completed", "output": "sent"}

    monkeypatch.setattr(runner, "_execute_tool_step", fake_tool)
    res = await runner._execute_step({"id": "c", "type": "connector", "config": {}}, _run(), None)
    assert seen.get("called") and res["output"] == "sent"


@pytest.mark.asyncio
async def test_rag_node_calls_rag_tool(runner, monkeypatch):
    captured = {}

    async def fake_tool(step, variables, entity_id, user_id, runtime_context):
        captured["config"] = step["config"]
        return {"status": "completed", "output": "hits"}

    monkeypatch.setattr(runner, "_execute_tool_step", fake_tool)
    step = {"id": "r", "type": "rag", "config": {"query": "refund policy", "output_var": "kb"}}
    res = await runner._execute_step(step, _run(), None)
    assert res["output"] == "hits"
    assert captured["config"]["tool"] == "rag"
    assert captured["config"]["args"]["question"] == "refund policy"
    assert captured["config"]["output_var"] == "kb"
    assert "output_format" not in captured["config"]
    # JSON keeps the result's sources readable by later steps.
    await runner._execute_step({**step, "config": {**step["config"], "output_format": "json"}}, _run(), None)
    assert captured["config"]["output_format"] == "json"


# ── Compound and/or conditions ──

@pytest.mark.asyncio
async def test_condition_compound_and(runner):
    step = {"id": "c", "type": "condition",
            "config": {"expression": 'score > 0.7 and status == "ok"'},
            "true_next": ["a"], "false_next": ["b"]}
    yes = await runner._execute_step(step, _run({"score": 0.9, "status": "ok"}), None)
    assert yes["next_override"] == ["a"]
    no = await runner._execute_step(step, _run({"score": 0.9, "status": "bad"}), None)
    assert no["next_override"] == ["b"]


@pytest.mark.asyncio
async def test_condition_compound_or(runner):
    step = {"id": "c", "type": "condition",
            "config": {"expression": 'tier == "vip" or score > 0.9'},
            "true_next": ["a"], "false_next": ["b"]}
    yes = await runner._execute_step(step, _run({"tier": "free", "score": 0.95}), None)
    assert yes["next_override"] == ["a"]
    no = await runner._execute_step(step, _run({"tier": "free", "score": 0.1}), None)
    assert no["next_override"] == ["b"]


@pytest.mark.asyncio
async def test_condition_compound_parentheses_preserve_precedence(runner):
    step = {
        "id": "c",
        "type": "condition",
        "config": {
            "expression": (
                'upload.success == true and '
                '(upload.dialog == "details" or upload.dialog == "Details")'
            )
        },
        "true_next": ["metadata"],
        "false_next": ["blocked"],
    }

    yes = await runner._execute_step(
        step,
        _run({"upload": {"success": True, "dialog": "Details"}}),
        None,
    )
    assert yes["next_override"] == ["metadata"]

    no = await runner._execute_step(
        step,
        _run({"upload": {"success": False, "dialog": "Details"}}),
        None,
    )
    assert no["next_override"] == ["blocked"]


# ── ComfyUI-style fingerprint caching / incremental re-execution ──

from packages.core.ai.workflow_runner import (  # noqa: E402
    _is_cacheable, _step_fingerprint,
)


def test_fingerprint_stable_and_input_sensitive():
    step = {"id": "m", "type": "merge", "config": {"sources": ["a"], "output_var": "o"}}
    fp1 = _step_fingerprint(step, {"a": 1})
    fp2 = _step_fingerprint(step, {"a": 1})
    fp3 = _step_fingerprint(step, {"a": 2})
    assert fp1 == fp2          # deterministic for same inputs
    assert fp1 != fp3          # changes when inputs change


def test_cacheable_predicate_and_policy_override():
    assert _is_cacheable({"type": "merge", "config": {}})
    assert _is_cacheable({"type": "condition", "config": {}})
    assert not _is_cacheable({"type": "llm", "config": {}})          # external, default off
    assert _is_cacheable({"type": "llm", "config": {"cache_policy": "cache"}})  # opt-in
    assert not _is_cacheable({"type": "merge", "config": {"cache_policy": "never"}})  # opt-out


@pytest.mark.asyncio
async def test_cache_hit_reuses_result_without_executing():
    step = {"id": "m", "type": "merge",
            "config": {"sources": ["a", "b"], "output_var": "combined"}}
    run1 = _run({"a": 1, "b": 2})
    miss_runner = WorkflowRunner()
    first = await miss_runner._execute_step_safe(step, run1, None)
    assert first.get("cached") is not True
    assert "fingerprint" in first

    # prime a second runner's cache from the first run's results
    index = WorkflowRunner.prime_cache_from_results({"m": first})
    cached_runner = WorkflowRunner(cache_index=index)

    # sentinel: if it executes, output would differ; on cache hit it reuses
    async def _boom(*a, **k):
        raise AssertionError("should not execute on cache hit")
    cached_runner._execute_step = _boom  # type: ignore

    run2 = _run({"a": 1, "b": 2})
    second = await cached_runner._execute_step_safe(step, run2, None)
    assert second["cached"] is True
    assert second["output"] == [1, 2]
    assert second["output_var"] == "combined"


@pytest.mark.asyncio
async def test_cache_miss_on_changed_inputs():
    step = {"id": "m", "type": "merge", "config": {"sources": ["a"], "output_var": "o"}}
    first = await WorkflowRunner()._execute_step_safe(step, _run({"a": 1}), None)
    index = WorkflowRunner.prime_cache_from_results({"m": first})
    runner = WorkflowRunner(cache_index=index)
    # different input -> different fingerprint -> executes (not cached)
    res = await runner._execute_step_safe(step, _run({"a": 99}), None)
    assert res.get("cached") is not True
    assert res["output"] == [99]


@pytest.fixture
def workflow_runner_sandbox(monkeypatch):
    from packages.core.config import get_settings
    from sandbox_entry_fakes import FakeRunner

    fake = FakeRunner()
    settings = get_settings()
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "http://sandbox")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "local")
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", fake.client_class)
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_network", [False, True])
async def test_code_node_executes_in_ephemeral_sandbox(runner, workflow_runner_sandbox, allow_network):
    fake = workflow_runner_sandbox
    fake.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        stdout='{"answer": 42}', stderr="", exit_code=0,
    )
    step = {"id": "c", "type": "code",
            "config": {"code": "print(inputs['value'])", "language": "python", "output_var": "r", "allow_network": allow_network}}
    res = await runner._execute_step(step, _run({"value": 42}), None)
    assert res["status"] == "completed"
    assert res["output"] == {"answer": 42}
    assert res["output_var"] == "r"
    assert res["sandboxed"] is True
    [created] = fake.created
    assert created["skill_name"] == "workflow-code-c"
    assert json.loads(created["files"]["inputs.json"]) == {"value": 42}
    if allow_network:
        assert "network" not in created["config"]
    else:
        assert created["config"]["network"] == "none"
    [execution] = [call for name, call in fake.calls if name == "exec"]
    assert execution["command"] == "python run.py"
    assert execution["sandbox_id"] == created["sandbox_id"]
    assert fake.destroyed == [created["sandbox_id"]]
    assert fake.live() == set()


@pytest.mark.asyncio
async def test_code_node_with_network_uses_the_runner_default_network(runner, workflow_runner_sandbox):
    """External runners only allow their dedicated bridge (``manor-sandbox``);
    a hardcoded ``"bridge"`` override was rejected there. Opting into network
    access must leave the network to the runner instead of naming one."""

    fake = workflow_runner_sandbox
    fake.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        stdout='{"ok": true}', stderr="", exit_code=0,
    )
    step = {"id": "c", "type": "code",
            "config": {"code": "print('{}')", "language": "python", "allow_network": True}}
    res = await runner._execute_step(step, _run({}), None)
    assert res["status"] == "completed"
    assert "network" not in fake.created[0]["config"]


@pytest.mark.asyncio
async def test_code_node_requests_bounded_limits_and_names_the_runner_kill(runner, workflow_runner_sandbox):
    """A headless browser needs more pids (threads count) than a plain script;
    when the runner kills a command, the step says which limit was hit."""

    fake = workflow_runner_sandbox
    fake.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        stdout="", stderr="", exit_code=137, outcome="pids_killed",
        outcome_detail="killed: the command's processes exhausted the sandbox's process limit",
    )
    step = {"id": "c", "type": "code", "config": {
        "code": "node collect.js", "language": "bash", "allow_network": True,
        "memory": "1.5g", "pids_limit": 5000, "cpus": "2",
    }}
    res = await runner._execute_step(step, _run({}), None)
    assert res["status"] == "failed"
    assert res["error"].startswith("Code was stopped by the sandbox: killed: the command's processes")
    config = fake.created[0]["config"]
    assert (config["memory"], config["pids_limit"], config["cpus"]) == ("1536m", 1024, 2.0)

    await runner._execute_step(
        {"id": "d", "type": "code", "config": {"code": "true", "language": "bash", "memory": "lots"}},
        _run({}), None,
    )
    config = fake.created[1]["config"]
    assert (config["memory"], config["pids_limit"], config["cpus"]) == ("512m", 512, 1.0)


@pytest.mark.asyncio
async def test_code_node_requires_sandbox_service(runner, monkeypatch):
    monkeypatch.setattr(workflow_runner_module, "_workflow_sandbox_configured", lambda: False)
    res = await runner._execute_step(
        {"id": "c", "type": "code", "config": {"code": "print(1)", "language": "python"}},
        _run(),
        None,
    )
    assert res["status"] == "failed"
    assert "SANDBOX_SERVICE_URL" in res["error"]


# ── Media generation nodes (image / video / audio -> generate_file) ──

@pytest.mark.asyncio
async def test_video_node_calls_generate_file(runner, monkeypatch):
    captured = {}

    async def fake_tool(step, variables, entity_id, user_id, runtime_context):
        captured["config"] = step["config"]
        return {"status": "completed", "output": "<video url>"}

    monkeypatch.setattr(runner, "_execute_tool_step", fake_tool)
    step = {"id": "v", "type": "video", "config": {
        "prompt": "a cat surfing on {{topic}}", "duration": 6, "resolution": "1080p",
        "aspect_ratio": "16:9", "output_var": "clip",
    }}
    res = await runner._execute_step(step, _run({"topic": "mars"}), None)
    assert res["output"] == "<video url>"
    cfg = captured["config"]
    assert cfg["tool"] == "generate_file"
    assert cfg["args"]["kind"] == "video"
    assert cfg["args"]["prompt"] == "a cat surfing on mars"   # template rendered
    assert cfg["args"]["duration"] == 6 and cfg["args"]["resolution"] == "1080p"
    assert cfg["output_var"] == "clip"


@pytest.mark.asyncio
async def test_image_and_audio_and_generic_media(runner, monkeypatch):
    kinds = []

    async def fake_tool(step, variables, entity_id, user_id, runtime_context):
        kinds.append(step["config"]["args"]["kind"])
        return {"status": "completed", "output": "ok"}

    monkeypatch.setattr(runner, "_execute_tool_step", fake_tool)
    await runner._execute_step({"id": "i", "type": "image", "config": {"prompt": "x"}}, _run(), None)
    await runner._execute_step({"id": "a", "type": "audio", "config": {"prompt": "x"}}, _run(), None)
    await runner._execute_step({"id": "m", "type": "media", "config": {"kind": "presentation", "prompt": "x"}}, _run(), None)
    assert kinds == ["image", "audio", "presentation"]


@pytest.mark.asyncio
async def test_image_node_passes_size_quality_reference(runner, monkeypatch):
    """Image node forwards size / quality / reference (templated) to generate_file
    so the tool's _merge_params hoists them into the image params."""
    captured = {}

    async def fake_tool(step, variables, entity_id, user_id, runtime_context):
        captured["args"] = step["config"]["args"]
        return {"status": "completed", "output": "<img url>"}

    monkeypatch.setattr(runner, "_execute_tool_step", fake_tool)
    step = {"id": "img", "type": "image", "config": {
        "prompt": "a hero {{topic}}", "size": "1536x1024", "quality": "high",
        "reference_url": "{{ref}}",
    }}
    await runner._execute_step(step, _run({"topic": "cat", "ref": "kb://style.png"}), None)
    args = captured["args"]
    assert args["kind"] == "image"
    assert args["prompt"] == "a hero cat"          # template rendered
    assert args["size"] == "1536x1024"
    assert args["quality"] == "high"
    assert args["reference_url"] == "kb://style.png"  # templated reference resolved


@pytest.mark.asyncio
async def test_media_node_forwards_model_override(runner, monkeypatch):
    """A per-node model (picked from the catalog) reaches generate_file's args;
    a blank model is omitted so the handler falls back to the account default."""
    captured = {}

    async def fake_tool(step, variables, entity_id, user_id, runtime_context):
        captured.setdefault("args", []).append(step["config"]["args"])
        return {"status": "completed", "output": "ok"}

    monkeypatch.setattr(runner, "_execute_tool_step", fake_tool)
    await runner._execute_step({"id": "i", "type": "image", "config": {"prompt": "x", "model": "openai/gpt-image-1"}}, _run(), None)
    await runner._execute_step({"id": "v", "type": "video", "config": {"prompt": "x", "model": "google/veo-3"}}, _run(), None)
    await runner._execute_step({"id": "a", "type": "audio", "config": {"prompt": "x"}}, _run(), None)  # blank
    img_args, vid_args, aud_args = captured["args"]
    assert img_args["model"] == "openai/gpt-image-1"
    assert vid_args["model"] == "google/veo-3"
    assert "model" not in aud_args  # blank → omitted → account default


# ── Node settings: retry on fail + continue on error ──

@pytest.mark.asyncio
async def test_retry_on_fail_retries_until_success(runner, monkeypatch):
    """retry_on_fail re-runs a failing step up to max_tries until it succeeds."""
    calls = {"n": 0}

    async def flaky(step, run, db):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"status": "failed", "error": "boom"}
        return {"status": "completed", "output": "ok"}

    monkeypatch.setattr(runner, "_execute_step", flaky)
    step = {"id": "s", "type": "tool", "config": {"retry_on_fail": True, "max_tries": 3, "retry_wait_ms": 0}}
    res = await runner._execute_step_safe(step, _run(), None)
    assert res["status"] == "completed"
    assert calls["n"] == 3
    assert res["attempts"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect_state", "error"),
    [
        (None, "agent execution stopped before it returned a receipt"),
        ("unknown", "provider returned an unknown effect state"),
        ("confirmed", "weekly report delivery failed after the delivery was accepted"),
        ("unconfirmed", "publication outcome is unknown"),
    ],
)
async def test_retry_on_fail_never_replays_an_attempt_with_an_effect_receipt(
    runner,
    monkeypatch,
    effect_state,
    error,
):
    calls = {"n": 0}

    async def unsafe(step, run, db):
        calls["n"] += 1
        result = {"status": "failed", "error": error}
        if effect_state is not None:
            result["effect_state"] = effect_state
        return result

    monkeypatch.setattr(runner, "_execute_step", unsafe)
    step = {"id": "s", "type": "agent", "config": {"retry_on_fail": True, "max_tries": 3, "retry_wait_ms": 0}}
    res = await runner._execute_step_safe(step, _run(), None)

    assert res["status"] == "failed"
    assert calls["n"] == 1
    assert res["attempts"] == 1


@pytest.mark.asyncio
async def test_retry_on_fail_still_retries_an_exhausted_in_loop_budget(runner, monkeypatch):
    """retryable=False alone means "the loop's own budget is spent", not "effect unknown"."""
    calls = {"n": 0}

    async def transient(step, run, db):
        calls["n"] += 1
        if calls["n"] < 3:
            return {
                "status": "failed",
                "error": "provider kept failing",
                "retryable": False,
                "effect_state": "none",
            }
        return {"status": "completed", "output": "ok"}

    monkeypatch.setattr(runner, "_execute_step", transient)
    step = {"id": "s", "type": "agent", "config": {"retry_on_fail": True, "max_tries": 3, "retry_wait_ms": 0}}
    res = await runner._execute_step_safe(step, _run(), None)

    assert res["status"] == "completed"
    assert calls["n"] == 3
    assert res["attempts"] == 3


@pytest.mark.asyncio
async def test_imported_failure_with_spent_loop_budget_is_still_skipped(runner, monkeypatch):
    from packages.core.ai import workflow_runner as wr

    async def spent(step, run, db):
        return {"status": "failed", "error": "provider kept failing", "retryable": False}

    monkeypatch.setattr(runner, "_execute_step", spent)
    monkeypatch.setattr(wr, "_is_trusted_imported_workflow_step", lambda run, step: True)
    res = await runner._execute_step_safe({"id": "s", "type": "agent", "config": {}}, _run(), None)

    assert res["status"] == "completed"
    assert res["skipped"] is True


@pytest.mark.asyncio
async def test_imported_failure_with_unconfirmed_effect_is_not_skipped(runner, monkeypatch):
    from packages.core.ai import workflow_runner as wr

    async def unsafe(step, run, db):
        return {"status": "failed", "error": "outcome unknown", "do_not_replay": True}

    monkeypatch.setattr(runner, "_execute_step", unsafe)
    monkeypatch.setattr(wr, "_is_trusted_imported_workflow_step", lambda run, step: True)
    res = await runner._execute_step_safe({"id": "s", "type": "agent", "config": {}}, _run(), None)

    assert res["status"] == "failed"
    assert res.get("skipped") is not True


@pytest.mark.asyncio
async def test_output_contract_failure_does_not_repeat_completed_effect(
    runner,
    monkeypatch,
):
    calls = 0

    async def published(step, run, db):
        nonlocal calls
        calls += 1
        return {"status": "completed", "output": {"published": "yes"}}

    monkeypatch.setattr(runner, "_execute_step", published)
    result = await runner._execute_step_safe(
        {
            "id": "publish",
            "type": "tool",
            "config": {
                "retry_on_fail": True,
                "max_tries": 3,
                "retry_wait_ms": 0,
                "output_schema": {
                    "type": "object",
                    "properties": {"published": {"type": "boolean"}},
                    "required": ["published"],
                    "additionalProperties": False,
                },
            },
        },
        _run(),
        None,
    )

    assert calls == 1
    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert result["attempts"] == 1
    assert result["producer_completed"] is True
    assert result["retry_blocked"] is True
    assert result["output"] == {"published": "yes"}


@pytest.mark.asyncio
async def test_imported_output_contract_failure_is_not_converted_to_skip(
    runner,
    monkeypatch,
):
    async def imported_effect(step, run, db):
        return {"status": "completed", "output": "wrong shape"}

    monkeypatch.setattr(runner, "_execute_step", imported_effect)
    result = await runner._execute_step_safe(
        {
            "id": "imported",
            "type": "tool",
            "config": {
                "n8n": {"type": "vendor.node"},
                "output_schema": {"type": "object"},
            },
        },
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert result.get("skipped") is not True


@pytest.mark.asyncio
async def test_no_retry_by_default(runner, monkeypatch):
    """Without retry_on_fail a step runs exactly once."""
    calls = {"n": 0}

    async def always_fail(step, run, db):
        calls["n"] += 1
        return {"status": "failed", "error": "boom"}

    monkeypatch.setattr(runner, "_execute_step", always_fail)
    res = await runner._execute_step_safe({"id": "s", "type": "tool", "config": {}}, _run(), None)
    assert res["status"] == "failed"
    assert calls["n"] == 1
    assert "attempts" not in res


@pytest.mark.asyncio
async def test_step_input_schema_rejects_before_execution(runner, monkeypatch):
    executed = False

    async def fake_execute(step, run, db):
        nonlocal executed
        executed = True
        return {"status": "completed", "output": {"ready": True}}

    monkeypatch.setattr(runner, "_execute_step", fake_execute)
    result = await runner._execute_step_safe(
        {
            "id": "checked",
            "type": "tool",
            "config": {
                "input_schema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                }
            },
        },
        _run({"count": "not-an-integer"}),
        None,
    )

    assert executed is False
    assert result["status"] == "failed"
    assert result["code"] == "input_schema_validation_failed"
    assert "count" in result["error"]


@pytest.mark.asyncio
async def test_step_output_schema_rejects_invalid_completed_output(runner, monkeypatch):
    async def fake_execute(step, run, db):
        return {"status": "completed", "output": {"ready": "yes"}}

    monkeypatch.setattr(runner, "_execute_step", fake_execute)
    result = await runner._execute_step_safe(
        {
            "id": "checked",
            "type": "tool",
            "config": {
                "output_schema": {
                    "type": "object",
                    "properties": {"ready": {"type": "boolean"}},
                    "required": ["ready"],
                    "additionalProperties": False,
                }
            },
        },
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert "ready" in result["error"]
    assert "fingerprint" not in result


@pytest.mark.asyncio
async def test_step_output_schema_enforces_registered_formats(runner, monkeypatch):
    async def fake_execute(step, run, db):
        return {"status": "completed", "output": "not a uri"}

    monkeypatch.setattr(runner, "_execute_step", fake_execute)
    result = await runner._execute_step_safe(
        {
            "id": "checked",
            "type": "tool",
            "config": {
                "output_schema": {"type": "string", "format": "uri"},
            },
        },
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert "uri" in result["error"]


@pytest.mark.asyncio
async def test_step_output_schema_rejects_unsupported_dialect(runner, monkeypatch):
    calls = 0

    async def fake_execute(step, run, db):
        nonlocal calls
        calls += 1
        return {"status": "completed", "output": {}}

    monkeypatch.setattr(runner, "_execute_step", fake_execute)
    result = await runner._execute_step_safe(
        {
            "id": "checked",
            "type": "tool",
            "config": {
                "output_schema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                },
            },
        },
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert "unsupported JSON Schema dialect" in result["error"]
    assert calls == 0


@pytest.mark.asyncio
async def test_step_output_schema_rejects_non_object_before_execution(
    runner,
    monkeypatch,
):
    calls = 0

    async def fake_execute(step, run, db):
        nonlocal calls
        calls += 1
        return {"status": "completed", "output": {}}

    monkeypatch.setattr(runner, "_execute_step", fake_execute)
    result = await runner._execute_step_safe(
        {
            "id": "checked",
            "type": "tool",
            "config": {"output_schema": False},
        },
        _run(),
        None,
    )

    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert "must be an object" in result["error"]
    assert calls == 0


def test_workflow_validation_checks_nested_step_schema_contracts() -> None:
    from packages.core.services.workflow_service import validate_workflow_steps

    validation = validate_workflow_steps(
        [
            {
                "id": "start",
                "type": "trigger",
                "config": {},
                "next": ["loop"],
            },
            {
                "id": "loop",
                "type": "loop",
                "config": {
                    "steps": [
                        {
                            "id": "effect",
                            "type": "tool",
                            "config": {"output_schema": False},
                        }
                    ]
                },
                "next": [],
            },
        ]
    )

    assert validation["valid"] is False
    assert any(
        error["code"] == "invalid_output_schema"
        and "loop.effect" in error["message"]
        for error in validation["errors"]
    )


def test_workflow_validation_rejects_condition_routes_inside_config() -> None:
    from packages.core.services.workflow_service import validate_workflow_steps

    validation = validate_workflow_steps(
        [
            {"id": "start", "type": "trigger", "next": ["upload_complete"]},
            {
                "id": "upload_complete",
                "type": "condition",
                "config": {
                    "expression": "upload.succeeded",
                    "true_next": ["publish"],
                    "false_next": ["blocked"],
                },
            },
            {"id": "publish", "type": "end", "next": []},
            {"id": "blocked", "type": "end", "next": []},
        ]
    )

    assert validation["valid"] is False
    errors = [
        error for error in validation["errors"]
        if error["code"] == "misplaced_condition_route"
    ]
    assert {error["message"] for error in errors} == {
        "Node 'upload_complete' config.true_next is ignored; move it to top-level 'true_next'.",
        "Node 'upload_complete' config.false_next is ignored; move it to top-level 'false_next'.",
    }


def test_workflow_validation_checks_entry_run_input_schema_contracts() -> None:
    from packages.core.services.workflow_service import validate_workflow_steps

    validation = validate_workflow_steps(
        [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "run_inputs": [
                        {"key": "request", "type": "json", "schema": False}
                    ]
                },
                "next": [],
            }
        ]
    )

    assert validation["valid"] is False
    assert any(
        error["code"] == "invalid_input_schema"
        and "run input 'request'" in error["message"]
        for error in validation["errors"]
    )


_RECORDS_UI = {"control": "record_select", "record_source": {
    "ledger": "relationship_ledger",
    "filter": {"contact_points.kind": "instagram", "status": {"$ne": "paused"}},
}}


@pytest.mark.parametrize(("schema", "valid"), [
    ({"type": "array", "items": {"type": "string"}, "x-ui": _RECORDS_UI}, True),
    ({"type": "string", "x-ui": {"control": "record_select", "record_source": {
        "ledger": "manor.relationship_ledger/v1", "label_field": "display_name", "secondary_field": "stage",
    }}}, True),
    ({"type": "array", "items": {"type": "string"}, "x-ui": {"control": "record_select", "record_source": {
        "ledger": "not_a_ledger",
    }}}, False),
    # The filter goes through query_ledger's own validation.
    ({"type": "array", "items": {"type": "string"}, "x-ui": {"control": "record_select", "record_source": {
        "ledger": "relationship_ledger", "filter": {"status": {"$regex": "x"}},
    }}}, False),
    ({"type": "array", "items": {"type": "string", "enum": ["lead:1"]}, "x-ui": _RECORDS_UI}, False),
    ({"type": "number", "x-ui": _RECORDS_UI}, False),
    ({"type": "object", "properties": {"creators": {"type": "string", "x-ui": _RECORDS_UI}}}, False),
])
def test_workflow_validation_checks_record_select_run_inputs(schema, valid) -> None:
    from packages.core.services.workflow_service import validate_workflow_steps

    validation = validate_workflow_steps([{
        "id": "start",
        "type": "trigger",
        "config": {"run_inputs": [{"key": "creators", "type": "json", "schema": schema}]},
        "next": [],
    }])

    errors = [error for error in validation["errors"] if error["code"] == "invalid_input_schema"]
    assert bool(errors) is not valid, errors


def test_runtime_contract_preflight_preserves_imported_graph_compatibility() -> None:
    from packages.core.services.workflow_service import (
        _require_runtime_workflow,
        validate_workflow_definition,
    )
    from packages.core.services.workflow_run_trace import (
        build_execution_snapshot,
        workflow_import_provenance_tags,
    )

    imported = SimpleNamespace(
        id="wf-imported",
        version=1,
        tags=[],
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "config": {},
                "next": ["legacy"],
            },
            {
                "id": "legacy",
                "type": "unsupported",
                "config": {},
                "meta": {
                    "source_tool": "n8n",
                    "original_type": "vendor.node",
                    "unmapped": True,
                },
                "next": [],
            },
        ],
    )
    imported.tags = workflow_import_provenance_tags(imported, "n8n")
    _require_runtime_workflow(imported)
    assert validate_workflow_definition(imported)["valid"] is True
    assert build_execution_snapshot(imported)["import_source"] == "n8n"

    ambiguous = SimpleNamespace(
        id=imported.id,
        version=imported.version,
        steps=imported.steps,
        tags=[
            *imported.tags,
            *workflow_import_provenance_tags(imported, "dify"),
        ],
    )
    with pytest.raises(ValueError, match="must be replaced"):
        _require_runtime_workflow(ambiguous)
    assert "import_source" not in build_execution_snapshot(ambiguous)

    edited = SimpleNamespace(
        id=imported.id,
        version=imported.version,
        tags=imported.tags,
        steps=[*imported.steps, {"id": "edited", "type": "end", "next": []}],
    )
    with pytest.raises(ValueError, match="must be replaced"):
        _require_runtime_workflow(edited)
    assert validate_workflow_definition(edited)["valid"] is False
    assert "import_source" not in build_execution_snapshot(edited)
    _require_runtime_workflow(
        edited,
        steps=build_execution_snapshot(imported)["steps"],
        imported_source="n8n",
    )

    with pytest.raises(ValueError, match="must be replaced"):
        _require_runtime_workflow(
            SimpleNamespace(tags=[], steps=imported.steps)
        )

    with pytest.raises(ValueError, match="non-consuming recursive"):
        _require_runtime_workflow(
            SimpleNamespace(tags=[], steps=[
                {
                    "id": "start",
                    "type": "trigger",
                    "config": {},
                    "next": ["effect"],
                },
                {
                    "id": "effect",
                    "type": "tool",
                    "config": {"output_schema": {"$ref": "#"}},
                    "next": [],
                },
            ])
        )

    with pytest.raises(ValueError, match="missing node 'missing'"):
        _require_runtime_workflow(
            SimpleNamespace(tags=[], steps=[
                {
                    "id": "start",
                    "type": "trigger",
                    "config": {},
                    "next": ["missing"],
                }
            ])
        )


@pytest.mark.asyncio
async def test_step_output_schema_accepts_valid_output(runner, monkeypatch):
    async def fake_execute(step, run, db):
        return {"status": "completed", "output": {"ready": True}}

    monkeypatch.setattr(runner, "_execute_step", fake_execute)
    result = await runner._execute_step_safe(
        {
            "id": "checked",
            "type": "tool",
            "config": {
                "output_schema": {
                    "type": "object",
                    "properties": {"ready": {"type": "boolean"}},
                    "required": ["ready"],
                    "additionalProperties": False,
                }
            },
        },
        _run(),
        None,
    )

    assert result["status"] == "completed"
    assert result["output"] == {"ready": True}


@pytest.mark.asyncio
async def test_cache_reuses_unchanged_step(monkeypatch):
    """ComfyUI-style incremental re-execution: a cache-eligible step whose
    fingerprint matches a primed prior result is reused, not re-executed;
    a changed input (different vars) re-runs it."""
    from packages.core.ai.workflow_runner import WorkflowRunner, _step_fingerprint
    step = {"id": "x", "type": "llm", "next": [], "config": {"cache_policy": "cache", "prompt": "hi {{a}}"}}

    run = _run({"a": 1})
    fp = _step_fingerprint(step, dict(run.variables or {}))
    prior = {"x": {"status": "completed", "output": "cached-out", "fingerprint": fp}}
    runner = WorkflowRunner(cache_index=WorkflowRunner.prime_cache_from_results(prior))

    calls = {"n": 0}

    async def fake_exec(s, r, d):
        calls["n"] += 1
        return {"status": "completed", "output": "fresh"}

    monkeypatch.setattr(runner, "_execute_step", fake_exec)

    res = await runner._execute_step_safe(step, run, None)
    assert res["output"] == "cached-out" and res.get("cached") is True
    assert calls["n"] == 0                       # reused — not executed

    res2 = await runner._execute_step_safe(step, _run({"a": 2}), None)
    assert res2["output"] == "fresh" and calls["n"] == 1   # inputs changed → re-ran


@pytest.mark.asyncio
async def test_invalid_cached_output_fails_without_reexecuting(monkeypatch):
    step = {
        "id": "x",
        "type": "llm",
        "next": [],
        "config": {
            "cache_policy": "cache",
            "prompt": "hi {{a}}",
            "output_schema": {"type": "object"},
        },
    }
    run = _run({"a": 1})
    fingerprint = _step_fingerprint(step, dict(run.variables or {}))
    prior = {
        "x": {
            "status": "completed",
            "output": "invalid cached output",
            "fingerprint": fingerprint,
        }
    }
    runner = WorkflowRunner(
        cache_index=WorkflowRunner.prime_cache_from_results(prior)
    )
    calls = 0

    async def should_not_execute(step, run, db):
        nonlocal calls
        calls += 1
        return {"status": "completed", "output": {"fresh": True}}

    monkeypatch.setattr(runner, "_execute_step", should_not_execute)

    result = await runner._execute_step_safe(step, run, None)

    assert calls == 0
    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    assert result["cached"] is True
    assert result["producer_completed"] is True
    assert result["retry_blocked"] is True
    assert result["output"] == "invalid cached output"


@pytest.mark.asyncio
async def test_error_trigger_dispatch_and_recursion_guard(runner, monkeypatch):
    """A failed run dispatches error-handler bindings; an error-sourced run does
    not (so a failing handler can't recurse)."""
    import packages.core.services.workflow_service as svc

    calls = []

    class FakeDB:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    db = FakeDB()

    async def fake_dispatch(db, entity_id, **kw):
        calls.append({"entity_id": entity_id, **kw})
        return []

    monkeypatch.setattr(svc, "dispatch_trigger", fake_dispatch)

    failed = _run()
    failed.status = "failed"
    failed.error = "boom"
    failed.trigger_source = None
    failed.workspace_id = "ws-1"
    await runner._dispatch_error_handlers(failed, db=db)
    assert len(calls) == 1
    assert calls[0]["trigger_type"] == "error"
    assert calls[0]["workspace_id"] == "ws-1"
    assert calls[0]["require_workspace_match"] is True
    assert calls[0]["trigger_data"]["error"] == "boom"
    assert failed.trigger_data["_workflow_terminal_effects"] == {
        "error_handler_run_ids": [],
        "error_handlers_enqueued": True,
    }

    calls.clear()
    handler = _run()
    handler.status = "failed"
    handler.trigger_source = "error"          # an error-handler that itself failed
    await runner._dispatch_error_handlers(handler, db=db)
    assert calls == []                         # no recursion


@pytest.mark.asyncio
async def test_error_trigger_retries_same_handler_runs_after_enqueue_failure(
    runner,
    monkeypatch,
):
    import packages.core.services.workflow_service as svc

    dispatches = []
    enqueues = []

    async def fake_dispatch(*_args, **_kwargs):
        dispatches.append(True)
        return [SimpleNamespace(id="handler-run-1")]

    class FakeDB:
        async def commit(self):
            return None

    outcomes = iter([False, True])

    def fake_enqueue(run_id):
        enqueues.append(run_id)
        return next(outcomes)

    monkeypatch.setattr(svc, "dispatch_trigger", fake_dispatch)
    monkeypatch.setattr(runner, "enqueue", fake_enqueue)

    failed = _run()
    failed.status = "failed"
    failed.workspace_id = "ws-1"
    with pytest.raises(RuntimeError, match="failed to enqueue"):
        await runner._dispatch_error_handlers(failed, FakeDB())

    await runner._dispatch_error_handlers(failed, FakeDB())

    assert dispatches == [True]
    assert enqueues == ["handler-run-1", "handler-run-1"]
    assert failed.trigger_data["_workflow_terminal_effects"][
        "error_handlers_enqueued"
    ] is True


@pytest.mark.asyncio
async def test_subworkflow_terminal_redelivery_repairs_committed_parent_resume(
    runner,
    monkeypatch,
):
    child = _run()
    child.id = "child-run"
    child.status = "completed"
    child.trigger_data = {
        "parent_run_id": "parent-run",
        "parent_step_id": "nested-step",
    }
    parent = _run()
    parent.id = "parent-run"
    parent.status = "running"
    parent.current_step_id = "nested-step"
    parent.step_results = {
        "nested-step": {
            "status": "completed",
            "subrun_id": child.id,
            "resumed": True,
        }
    }
    resumed = []

    class Result:
        def scalar_one_or_none(self):
            return parent

    class FakeDB:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    async def fake_run(self, run_id, progress=None):
        resumed.append(run_id)
        return "executed"

    monkeypatch.setattr(WorkflowRunner, "run", fake_run)

    await runner._propagate_subworkflow_result(child, FakeDB())

    assert resumed == [parent.id]


def test_workflow_tools_exposed():
    """Manor agents can discover the complete Workflow authoring package."""
    from packages.core.ai.tools.workflow_tools import get_tools
    tools = get_tools()
    names = {s["function"]["name"] for s, _ in tools}
    assert names == {
        "expand_schedule_occurrences",
        "query_workflow_projects",
        "list_workspace_flows",
        "start_workspace_flow",
        "list_workflows",
        "run_workflow",
        "list_workflow_definitions",
        "get_workflow",
        "create_workflow",
        "ai_edit_workflow",
        "update_workflow",
        "validate_workflow",
        "deploy_workflow",
        "delete_workflow",
        "test_workflow",
        "test_workflow_node",
        "list_workflow_runs",
        "get_workflow_run",
        "cancel_workflow_run",
        "resume_workflow_run",
        "import_workflow",
    }
    run = next(s for s, _ in tools if s["function"]["name"] == "run_workflow")
    assert "workflow" in run["function"]["parameters"]["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["invalid_input", "wait_changed"])
async def test_resume_workflow_tool_reports_unaccepted_resume_outcomes(monkeypatch, outcome):
    """A native input or changed wait must not be presented as a resumed run."""
    import packages.core.ai.tools.workflow_tools as workflow_tools

    run = SimpleNamespace(id="run-1", status="paused")

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def get_run(*_args):
        return run

    async def can_control(*_args, **_kwargs):
        return True

    async def resume(*_args, **_kwargs):
        return outcome

    monkeypatch.setattr(workflow_tools, "async_session", lambda: FakeDB())
    monkeypatch.setattr("packages.core.services.workflow_service.get_run", get_run)
    monkeypatch.setattr(workflow_tools, "_user_can_control_workflow_run", can_control)
    monkeypatch.setattr(WorkflowRunner, "resume", resume)

    response = json.loads(await workflow_tools._resume_workflow_run(
        entity_id="ent1",
        user_id="user1",
        run_id=run.id,
        inputs={"required_field": "invalid-for-current-wait"},
    ))

    assert response == {
        "ok": False,
        "error": f"Could not resume workflow run: {outcome}",
    }


def test_workflow_service_validation_catches_graph_errors():
    from packages.core.services.workflow_service import validate_workflow_steps

    valid = validate_workflow_steps([
        {"id": "start", "type": "trigger", "next": ["set"]},
        {"id": "set", "type": "transform", "config": {"set": {"ok": True}}, "next": ["end"]},
        {"id": "end", "type": "end", "next": []},
    ])
    assert valid["valid"] is True
    assert valid["entry_step_id"] == "start"
    assert valid["edge_count"] == 2

    invalid = validate_workflow_steps([
        {"id": "same", "type": "transform", "next": ["missing"]},
        {"id": "same", "type": "made_up", "next": []},
    ])
    assert invalid["valid"] is False
    codes = {item["code"] for item in invalid["errors"]}
    assert {"missing_entry", "duplicate_node_id", "unsupported_node_type", "missing_edge_target"} <= codes


def test_filter_node_keeps_matching_items(runner):
    """Filter keeps list items for which the per-item condition holds."""
    leads = [{"score": 90}, {"score": 40}, {"score": 75}]
    res = runner._execute_filter_step(
        {"id": "f", "config": {"items": "{{leads}}", "item_var": "lead", "condition": "lead.score >= 70"}},
        {"leads": leads},
    )
    assert res["status"] == "completed"
    assert res["output"] == [{"score": 90}, {"score": 75}]


def test_aggregate_node_operations(runner):
    items = [{"amt": 10}, {"amt": 5}, {"amt": 20}]
    vars_ = {"rows": items}
    def agg(op, **extra):
        return runner._execute_aggregate_step(
            {"id": "a", "config": {"items": "{{rows}}", "operation": op, "field": "amt", **extra}}, vars_,
        )["output"]
    assert agg("count") == 3
    assert agg("sum") == 35
    assert agg("max") == 20
    assert agg("avg") == pytest.approx(35 / 3)
    assert agg("join", separator="-") == "10-5-20"
    assert agg("collect") == [10, 5, 20]


def test_split_node(runner):
    # list of objects + field -> flattened list
    r1 = runner._execute_split_step(
        {"id": "s", "config": {"items": "{{rows}}", "field": "tags"}},
        {"rows": [{"tags": ["a", "b"]}, {"tags": ["c"]}]},
    )
    assert r1["output"] == ["a", "b", "c"]
    # delimited string + separator
    r2 = runner._execute_split_step(
        {"id": "s", "config": {"items": "{{csv}}", "separator": ","}}, {"csv": "x, y ,z"},
    )
    assert r2["output"] == ["x", "y", "z"]

    # n8n Split Out preserves the configured field name for later $json refs.
    r3 = runner._execute_split_step(
        {"id": "s", "config": {"items": "{{page}}", "field": "links", "preserve_field": True}},
        {"page": {"links": ["a.html", "b.html"]}},
    )
    assert r3["output"] == [{"links": "a.html"}, {"links": "b.html"}]


def test_limit_node(runner):
    items = [1, 2, 3, 4, 5]
    first = runner._execute_limit_step({"id": "l", "config": {"items": "{{xs}}", "max": 2}}, {"xs": items})
    assert first["output"] == [1, 2]
    last = runner._execute_limit_step(
        {"id": "l", "config": {"items": "{{xs}}", "max": 2, "keep": "last"}}, {"xs": items},
    )
    assert last["output"] == [4, 5]


def test_respond_node_captures_response(runner):
    res = runner._execute_respond_step(
        {"id": "r", "config": {"body": '{"ok": true, "name": "{{who}}"}', "status_code": 201}},
        {"who": "Ada"},
    )
    assert res["output_var"] == "__response"           # webhook endpoint reads this
    assert res["output"]["status"] == 201
    assert res["output"]["body"] == {"ok": True, "name": "Ada"}   # JSON body parsed


def test_sort_dedupe_stop_extract(runner):
    rows = [{"n": 3}, {"n": 1}, {"n": 2}, {"n": 1}]
    s = runner._execute_sort_step({"id": "s", "config": {"items": "{{r}}", "field": "n", "order": "desc"}}, {"r": rows})
    assert [x["n"] for x in s["output"]] == [3, 2, 1, 1]
    d = runner._execute_dedupe_step({"id": "d", "config": {"items": "{{r}}", "field": "n"}}, {"r": rows})
    assert [x["n"] for x in d["output"]] == [3, 1, 2]
    stop = runner._execute_stop_step(
        {
            "id": "x",
            "config": {
                "message": "halt {{why}}",
                "retry_from_step_id": "producer",
                "retry_state": {
                    "required_change": "Replace {{field}}",
                    "editable_input_schema": {
                        "type": "object",
                        "properties": {"source_id": {"type": "string"}},
                        "required": ["source_id"],
                    },
                },
            },
        },
        {"why": "now", "field": "the source"},
    )
    assert stop == {
        "status": "failed",
        "error": "halt now",
        "retry_from_step_id": "producer",
        "retry_state": {
            "retry_from_step_id": "producer",
            "required_change": "Replace the source",
            "editable_input_schema": {
                "type": "object",
                "properties": {"source_id": {"type": "string"}},
                "required": ["source_id"],
            },
        },
    }
    blocked_stop = runner._execute_stop_step(
        {
            "id": "blocked",
            "config": {
                "message": "reconcile before retry",
                "retryable": False,
            },
        },
        {},
    )
    assert blocked_stop == {
        "status": "failed",
        "error": "reconcile before retry",
        "retryable": False,
        "retry_blocked": True,
    }
    j = runner._execute_extractfromfile_step({"id": "e", "config": {"input": '{{body}}'}}, {"body": '{"a": 1}'})
    assert j["output"] == {"a": 1}
    csv = runner._execute_extractfromfile_step({"id": "e", "config": {"input": "{{c}}", "format": "csv"}}, {"c": "a,b\n1,2\n3,4"})
    assert csv["output"] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_failed_stop_retry_state_drives_editable_chat_retry():
    from packages.core.services.workflow_chat_projection import (
        _workflow_business_state,
        _workflow_retry_input_schema,
    )

    editable_schema = {
        "type": "object",
        "properties": {"workflow_project_id": {"type": "string"}},
        "required": ["workflow_project_id"],
    }
    run = SimpleNamespace(
        variables={"workflow_project_id": "published-project"},
        current_step_id="video_project_not_ready",
        step_results={
            "video_project_not_ready": {
                "status": "failed",
                "retry_state": {
                    "retry_from_step_id": "load_video_project",
                    "editable_input_schema": editable_schema,
                },
            }
        },
    )

    outcome, retry_state = _workflow_business_state(run)

    assert outcome == "in_progress"
    assert retry_state["retry_from_step_id"] == "load_video_project"
    assert _workflow_retry_input_schema(run, retry_state) == editable_schema


def test_failed_retry_blocked_step_has_no_chat_retry_checkpoint():
    from packages.core.services.workflow_chat_projection import (
        workflow_retry_from_step_id,
    )

    run = SimpleNamespace(
        status="failed",
        current_step_id="upload_youtube_video",
        execution_snapshot={
            "steps": [{
                "id": "upload_youtube_video",
                "type": "agent",
                "config": {"skill": "chrome"},
            }],
        },
        step_results={
            "upload_youtube_video": {
                "status": "failed",
                "retry_blocked": True,
            }
        },
        retry_from_step_id=None,
        retry_of_run_id=None,
    )

    assert workflow_retry_from_step_id(run, {}) == ""


def test_failed_unconfirmed_effect_has_no_chat_retry_checkpoint():
    from packages.core.services.workflow_chat_projection import (
        workflow_retry_from_step_id,
    )

    run = SimpleNamespace(
        status="failed",
        current_step_id="upload_youtube_video",
        execution_snapshot={
            "steps": [{
                "id": "upload_youtube_video",
                "type": "agent",
                "config": {},
            }],
        },
        step_results={
            "upload_youtube_video": {
                "status": "failed",
                "effect_state": "unconfirmed",
                "do_not_replay": True,
            }
        },
        retry_from_step_id=None,
        retry_of_run_id=None,
    )

    assert workflow_retry_from_step_id(run, {}) == ""


@pytest.mark.parametrize("skill", ["chrome", "another-skill", None])
def test_failed_chrome_retry_blocked_step_keeps_retry_checkpoint(skill):
    from packages.core.services.workflow_chat_projection import (
        workflow_retry_from_step_id,
    )

    run = SimpleNamespace(
        status="failed",
        current_step_id="upload_youtube_video",
        execution_snapshot={
            "steps": [{
                "id": "upload_youtube_video",
                "type": "agent",
                "config": {"skill": skill} if skill else {},
            }],
        },
        step_results={
            "upload_youtube_video": {
                "status": "failed",
                "retry_blocked": True,
                "chrome_outcome": {"status": "failed", "do_not_replay": True},
            }
        },
        retry_from_step_id=None,
        retry_of_run_id=None,
    )

    assert workflow_retry_from_step_id(run, {}) == "upload_youtube_video"


def test_chrome_retry_prompt_retains_bounded_prior_agent_evidence():
    from packages.core.ai.workflow_runner import _workflow_agent_resume_prompt

    prompt = _workflow_agent_resume_prompt(
        {
            "status": "failed",
            "resume_strategy": "reexecute",
            "output": {
                "video_id": "video-1",
                "watch_url": "https://www.youtube.com/watch?v=video-1",
                "upload_occurred": True,
                "final_action_state": "uncertain",
                "draft_tab_id": 17,
                "unrelated": "discarded",
            },
            "chrome_outcome": {
                "status": "failed",
                "action_executed": "unknown",
                "do_not_replay": True,
                "receipts": [{"operation_id": "operation-1"}],
                "chrome_connection": {"id": "connection-old"},
                "unbounded": "discarded",
            },
        },
        runtime_context={"workflow_run_id": "retry-run"},
        step_id="upload_youtube_video",
    )

    assert '"same_workflow_run": false' in prompt
    assert '"operation_id": "operation-1"' in prompt
    assert '"video_id": "video-1"' in prompt
    assert "Historical tab, browser-group, and node-handle references" in prompt
    assert '"unbounded": "discarded"' in prompt
    assert '"unrelated": "discarded"' in prompt


def test_chrome_retry_prompt_retains_completed_agent_output():
    from packages.core.ai.workflow_runner import _workflow_agent_resume_prompt

    prompt = _workflow_agent_resume_prompt(
        {
            "status": "completed",
            "resume_strategy": "reexecute",
            "chrome_retry_recovery": True,
            "output": {
                "video_id": "video-1",
                "upload_occurred": True,
                "draft_tab_id": 17,
            },
        },
        runtime_context={"workflow_run_id": "retry-run"},
        step_id="upload_youtube_video",
    )

    assert '"same_workflow_run": false' in prompt
    assert '"upload_occurred": true' in prompt


def test_extract_from_file_reads_downloaded_xlsx_and_can_limit_rows(runner):
    import base64
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Email", "Feedback"])
    sheet.append(["ada@example.com", "Loved it"])
    sheet.append(["grace@example.com", "Arrived late"])
    buffer = BytesIO()
    workbook.save(buffer)

    res = runner._execute_extractfromfile_step({
        "id": "extract",
        "config": {"input": "{{download}}", "format": "xlsx", "max_rows": 1},
    }, {"download": {"body_base64": base64.b64encode(buffer.getvalue()).decode()}})
    assert res["status"] == "completed"
    assert res["output"] == [{"Email": "ada@example.com", "Feedback": "Loved it"}]


def test_marketing_templates_render_html_and_markdown(runner):
    html_result = runner._execute_transform_step({
        "id": "email", "config": {
            "items": "{{customer}}",
            "html_template": "<h1>{{input.Headline}}</h1><p>{{input.Body}}</p>",
        },
    }, {"customer": {"Headline": "Welcome", "Body": "Thanks, Ada"}}, _run())
    assert html_result["output"] == {"html": "<h1>Welcome</h1><p>Thanks, Ada</p>"}

    markdown_result = runner._execute_transform_step({
        "id": "report", "config": {
            "items": "{{audit}}", "markdown": "# {{input.title}}\n\n{{input.body}}",
            "markdown_to_html": True,
        },
    }, {"audit": {"title": "SEO audit", "body": "**Fix titles**"}}, _run())
    assert "<h1>SEO audit</h1>" in markdown_result["output"]["data"]
    assert "<strong>Fix titles</strong>" in markdown_result["output"]["data"]


def test_set_include_other_fields_preserves_marketing_customer_data(runner):
    result = runner._execute_transform_step({
        "id": "campaign", "config": {
            "items": "{{rows}}", "include_other_fields": True,
            "set": {"Campaign Target": "Retention"},
        },
    }, {"rows": [{"Email": "ada@example.com", "Feedback": "Great"}]}, _run())
    assert result["output"] == [{
        "Email": "ada@example.com", "Feedback": "Great", "Campaign Target": "Retention",
    }]


def test_set_selected_and_excluded_fields_match_n8n_include_modes(runner):
    source = [{
        "id": 7,
        "secret": "remove",
        "profile": {"name": "Before", "city": "Paris"},
    }]
    selected = runner._execute_transform_step({
        "id": "selected",
        "config": {
            "items": "{{rows}}",
            "include_mode": "selected",
            "include_fields": ["id"],
            "set": {"status": "ready"},
        },
    }, {"rows": source}, _run())
    excluded = runner._execute_transform_step({
        "id": "excluded",
        "config": {
            "items": "{{rows}}",
            "include_mode": "except",
            "exclude_fields": ["secret"],
            "set": {"profile.name": "After"},
            "dot_notation": True,
        },
    }, {"rows": source}, _run())

    assert selected["output"] == [{"id": 7, "status": "ready"}]
    assert excluded["output"] == [{
        "id": 7,
        "profile": {"name": "After", "city": "Paris"},
    }]


def test_set_raw_json_mode_resolves_each_item(runner):
    result = runner._execute_transform_step({
        "id": "raw",
        "config": {
            "items": "{{rows}}",
            "include_mode": "none",
            "raw_json": '{"fullName":"{{first}} {{last}}","active":true}',
        },
    }, {
        "rows": [
            {"first": "Ada", "last": "Lovelace"},
            {"first": "Grace", "last": "Hopper"},
        ]
    }, _run())

    assert result["output"] == [
        {"fullName": "Ada Lovelace", "active": True},
        {"fullName": "Grace Hopper", "active": True},
    ]


def test_datetime_node_now_and_offset(runner):
    iso = runner._execute_datetime_step({"id": "d", "config": {"operation": "now"}}, {})["output"]
    assert "T" in iso and iso[:4].isdigit()                     # ISO datetime

    shifted = runner._execute_datetime_step(
        {"id": "d", "config": {"operation": "add", "value": "2026-01-01T00:00:00+00:00",
                               "amount": 3, "unit": "days", "format": "%Y-%m-%d"}}, {},
    )["output"]
    assert shifted == "2026-01-04"


@pytest.mark.asyncio
async def test_extract_node_parses_json(runner, monkeypatch):
    """Information Extractor runs a tool-less LLM and parses the reply as JSON."""
    async def fake_llm(step, variables, entity_id, user_id, runtime_context, db):
        # the extractor builds an llm step with a JSON-forcing system prompt
        assert "extraction engine" in step["config"]["system_prompt"]
        return {"status": "completed", "output": '{"name": "Ada", "amount": 42}'}

    monkeypatch.setattr(runner, "_execute_agent_step", fake_llm)
    res = await runner._execute_extract_step(
        {"id": "x", "config": {"input": "Ada paid 42", "schema": "name, amount"}},
        {}, "ent1", "user1", {}, None,
    )
    assert res["status"] == "completed"
    assert res["output"] == {"name": "Ada", "amount": 42}


@pytest.mark.asyncio
async def test_information_extractor_batches_items_keeps_campaign_instruction(runner, monkeypatch):
    seen = []

    async def fake_llm(step, variables, entity_id, user_id, runtime_context, db):
        seen.append((step["config"]["system_prompt"], step["config"]["prompt"], variables["input"]))
        return {
            "status": "completed",
            "output": '{"Headline":"Thanks","Body":"We heard you","SendCoupon":false}',
        }

    monkeypatch.setattr(runner, "_execute_agent_step", fake_llm)
    result = await runner._execute_batch_extract_step({
        "id": "draft", "config": {
            "batch": True,
            "input": "Feedback: {{input.Feedback}}",
            "system_prompt": "Use a friendly retention-campaign voice.",
            "schema": {"Headline": "string", "Body": "string", "SendCoupon": "boolean"},
            "response_wrapper": "output",
        },
    }, {"input": [{"Feedback": "Great"}, {"Feedback": "Late"}]}, "ent", "user", {}, None)
    assert result["status"] == "completed"
    assert len(result["output"]) == 2
    assert result["output"][0]["output"]["Headline"] == "Thanks"
    assert all("friendly retention-campaign" in system for system, _prompt, _item in seen)
    assert [prompt for _system, prompt, _item in seen] == ["Feedback: {{input.Feedback}}"] * 2


@pytest.mark.asyncio
async def test_imported_condition_routes_but_passes_current_item(runner):
    step = {
        "id": "coupon", "type": "condition",
        "config": {"expression": "output.SendCoupon == true", "pass_input": True},
        "true_next": ["with_coupon"], "false_next": ["without_coupon"],
    }
    current = {"Email": "ada@example.com", "output": {"SendCoupon": True}}
    result = await runner._execute_step(step, _run({"input": current}), None)
    assert result["condition_result"] is True
    assert result["next_override"] == ["with_coupon"]
    assert result["output"] == current


@pytest.mark.asyncio
async def test_html_extract_uses_css_fields_without_llm(runner):
    html = """<html><body><nav>menu</nav><h1 id='firstHeading'>Workflow</h1>
      <table><tr><td><a href='one.html'>One</a><a href='two.html'>Two</a></td></tr></table>
      <p>Useful body</p></body></html>"""
    result = await runner._execute_extract_step({
        "id": "extract", "config": {"html_extract": [
            {"key": "title", "selector": "#firstHeading", "return_value": "text"},
            {"key": "links", "selector": "table a", "return_value": "attribute",
             "attribute": "href", "return_array": True},
            {"key": "body", "selector": "body", "return_value": "text",
             "skip_selectors": "nav"},
            {"key": "document", "selector": "html", "return_value": "text"},
        ]},
    }, {"input": {"body": html}}, "ent1", "user1", {}, None)
    assert result["status"] == "completed"
    assert result["output"]["title"] == "Workflow"
    assert result["output"]["links"] == ["one.html", "two.html"]
    assert "menu" not in result["output"]["body"]
    assert "Useful body" in result["output"]["body"]
    assert "Workflow" in result["output"]["document"]


@pytest.mark.asyncio
async def test_subworkflow_guards(runner):
    """Sub-workflow node refuses an empty target, a self-call, and runaway depth
    (all before touching the DB)."""
    run = _run()
    run.workflow_id = "wf_self"

    r1 = await runner._execute_subworkflow_step({"id": "s", "config": {}}, {}, run, db=None)
    assert r1["status"] == "failed" and "workflow_id" in r1["error"]

    r2 = await runner._execute_subworkflow_step(
        {"id": "s", "config": {"workflow_id": "wf_self"}}, {}, run, db=None,
    )
    assert r2["status"] == "failed" and "itself" in r2["error"]

    deep = _run()
    deep.workflow_id = "wf_parent"
    deep.trigger_data = {"_subworkflow_depth": 5}
    r3 = await runner._execute_subworkflow_step(
        {"id": "s", "config": {"workflow_id": "wf_child"}}, {}, deep, db=None,
    )
    assert r3["status"] == "failed" and "depth limit" in r3["error"]


@pytest.mark.asyncio
async def test_subworkflow_safe_execution_has_no_default_wall_clock_timeout(
    runner,
    monkeypatch,
):
    async def delayed_step(step, run, db):
        await asyncio.sleep(0.02)
        return {"status": "completed", "output": "child completed"}

    monkeypatch.setattr(runner, "_execute_step", delayed_step)

    async def unexpected_wait_for(*_args, **_kwargs):
        raise AssertionError("unbounded node must not call asyncio.wait_for")

    monkeypatch.setattr(workflow_runner_module.asyncio, "wait_for", unexpected_wait_for)

    result = await runner._execute_step_safe(
        {"id": "child", "type": "subworkflow", "config": {}},
        _run(),
        db=None,
    )

    assert result["status"] == "completed"
    assert result["output"] == "child completed"


@pytest.mark.asyncio
async def test_subworkflow_safe_execution_honors_explicit_wall_clock_timeout(
    runner,
    monkeypatch,
):
    async def delayed_step(step, run, db):
        await asyncio.sleep(0.02)
        return {"status": "completed", "output": "child completed"}

    monkeypatch.setattr(runner, "_execute_step", delayed_step)

    result = await runner._execute_step_safe(
        {"id": "child", "type": "subworkflow", "config": {"timeout": 0.001}},
        _run(),
        db=None,
    )

    assert result["status"] == "failed"
    assert result["error"] == "Step child timed out"


@pytest.mark.asyncio
async def test_completed_subworkflow_preserves_configured_output_alias(
    runner,
    monkeypatch,
):
    target = SimpleNamespace(
        id="wf-child",
        variables={},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {"trigger_type": "internal"},
            "next": [],
        }],
    )

    class Result:
        def scalar_one_or_none(self):
            return target

    class Db:
        child = None

        async def execute(self, _query):
            return Result()

        def add(self, child):
            self.child = child

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _child):
            return None

    db = Db()

    async def complete_child(_runner, _run_id):
        db.child.status = "completed"
        db.child.variables = {"project": {"revision": 2}}

    monkeypatch.setattr(WorkflowRunner, "run", complete_child)
    result = await runner._execute_subworkflow_step(
        {
            "id": "child",
            "type": "subworkflow",
            "config": {"workflow_id": "wf-child", "output_var": "planning"},
        },
        {},
        _run(),
        db,
    )

    assert result["status"] == "completed"
    assert result["output"] == {"project": {"revision": 2}}
    assert result["output_var"] == "planning"
    assert db.child.lineage_root_run_id == db.child.id
    assert db.child.lineage_is_legacy is False


@pytest.mark.asyncio
async def test_subworkflow_child_inherits_nested_chat_origin_without_reusing_root_activity(
    runner,
    monkeypatch,
):
    target = SimpleNamespace(
        id="wf-child",
        variables={},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {"trigger_type": "internal"},
            "next": [],
        }],
    )

    class Result:
        def scalar_one_or_none(self):
            return target

    class Db:
        child = None

        async def execute(self, _query):
            return Result()

        def add(self, child):
            self.child = child

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _child):
            return None

    db = Db()
    parent = _run()
    parent.id = "root-run"
    parent.trigger_data = {
        "_workflow_chat_origin": {
            "enabled": True,
            "conversation_id": "conversation-1",
            "activity_message_id": "activity-1",
            "projection": {"progress": True},
            "wait_bridge": True,
        }
    }

    async def leave_child_running(_runner, _run_id):
        return None

    monkeypatch.setattr(WorkflowRunner, "run", leave_child_running)
    result = await runner._execute_subworkflow_step(
        {
            "id": "child",
            "type": "subworkflow",
            "config": {"workflow_id": target.id},
        },
        {},
        parent,
        db,
    )

    assert result["status"] == "paused"
    assert db.child.trigger_data["_workflow_chat_origin"] == {
        "enabled": True,
        "conversation_id": "conversation-1",
        "activity_message_id": "activity-1",
        "projection": {"progress": True},
        "wait_bridge": True,
        "host_run_id": parent.id,
        "nested": True,
    }


@pytest.mark.asyncio
async def test_nested_chat_cancel_cascades_cancel_to_parent_subworkflow_barrier(
    runner,
    db_session,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    step = {
        "id": "child",
        "type": "subworkflow",
        "config": {"workflow_id": generate_ulid()},
        "next": [],
    }
    parent_workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        created_by=user_id,
        name="Nested approval parent",
        trigger_type="manual",
        steps=[step],
        variables={},
        tags=[],
    )
    child = WorkflowRun(
        id=generate_ulid(),
        workflow_id=step["config"]["workflow_id"],
        entity_id=entity_id,
        status="cancelled",
        variables={},
        step_results={},
        trigger_data={
            "parent_run_id": "pending-parent-id",
            "parent_step_id": step["id"],
            "_workflow_chat_cancel_ancestors": {
                "host_run_id": "pending-parent-id",
                "cancelled_by": user_id,
            },
        },
        execution_trace=[],
        started_by=user_id,
    )
    parent = WorkflowRun(
        id=generate_ulid(),
        workflow_id=parent_workflow.id,
        entity_id=entity_id,
        status="paused",
        current_step_id=step["id"],
        variables={},
        step_results={
            step["id"]: {
                "status": "paused",
                "subrun_id": child.id,
            }
        },
        trigger_data={},
        execution_trace=[],
        started_by=user_id,
    )
    child.trigger_data["parent_run_id"] = parent.id
    child.trigger_data["_workflow_chat_cancel_ancestors"]["host_run_id"] = parent.id
    db_session.add_all([parent_workflow, parent, child])
    await db_session.commit()

    await runner._propagate_subworkflow_result(child, db_session)
    await db_session.refresh(parent)

    assert parent.status == "cancelled"
    assert parent.error is None
    assert parent.trigger_data["_workflow_run_control"]["cancelled_by"] == user_id


@pytest.mark.asyncio
async def test_subworkflow_uncertain_effect_blocks_parent_retry(
    runner,
    db_session,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    step = {
        "id": "publish_video",
        "type": "subworkflow",
        "config": {"workflow_id": generate_ulid()},
        "next": [],
    }
    parent_workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        created_by=user_id,
        name="Publication parent",
        trigger_type="manual",
        steps=[step],
        variables={},
        tags=[],
    )
    child = WorkflowRun(
        id=generate_ulid(),
        workflow_id=step["config"]["workflow_id"],
        entity_id=entity_id,
        status="failed",
        current_step_id="upload_youtube_video",
        error="Upload outcome is uncertain",
        variables={},
        step_results={
            "upload_youtube_video": {
                "status": "failed",
                "retry_blocked": True,
                "do_not_replay": True,
            }
        },
        trigger_data={
            "parent_run_id": "pending-parent-id",
            "parent_step_id": step["id"],
        },
        execution_trace=[],
        started_by=user_id,
    )
    parent = WorkflowRun(
        id=generate_ulid(),
        workflow_id=parent_workflow.id,
        entity_id=entity_id,
        status="paused",
        current_step_id=step["id"],
        variables={},
        step_results={
            step["id"]: {
                "status": "paused",
                "subrun_id": child.id,
            }
        },
        trigger_data={},
        execution_trace=[],
        started_by=user_id,
    )
    child.trigger_data["parent_run_id"] = parent.id
    db_session.add_all([parent_workflow, parent, child])
    await db_session.commit()

    await runner._propagate_subworkflow_result(child, db_session)
    await db_session.refresh(parent)

    assert parent.status == "failed"
    assert parent.step_results[step["id"]]["retry_blocked"] is True


@pytest.mark.asyncio
async def test_subworkflow_recovery_reuses_child_committed_before_parent_result(
    runner,
    monkeypatch,
    db_session,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    child_workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        created_by=user_id,
        name="Crash-safe child",
        trigger_type="manual",
        steps=[
            {"id": "start", "type": "trigger", "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
        variables={},
        tags=[],
    )
    parent_workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        created_by=user_id,
        name="Crash-safe parent",
        trigger_type="manual",
        steps=[],
        variables={},
        tags=[],
    )
    parent_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=parent_workflow.id,
        entity_id=entity_id,
        status="running",
        current_step_id="child",
        variables={},
        step_results={},
        trigger_data={},
        execution_trace=[],
        started_by=user_id,
    )
    db_session.add_all([child_workflow, parent_workflow, parent_run])
    await db_session.commit()

    class SimulatedProcessExit(RuntimeError):
        pass

    async def crash_after_child_commit(_runner, _run_id):
        raise SimulatedProcessExit("process exited after child commit")

    monkeypatch.setattr(WorkflowRunner, "run", crash_after_child_commit)
    step = {
        "id": "child",
        "type": "subworkflow",
        "config": {"workflow_id": child_workflow.id},
        "next": [],
    }
    with pytest.raises(SimulatedProcessExit):
        await runner._execute_subworkflow_step(
            step,
            parent_run.variables,
            parent_run,
            db_session,
        )

    await db_session.refresh(parent_run)
    checkpoint = parent_run.step_results[step["id"]]
    child_run_id = checkpoint["subrun_id"]
    assert checkpoint["resume_strategy"] == "reexecute"

    resumed_child_ids: list[str] = []

    async def child_still_running(_runner, run_id):
        resumed_child_ids.append(run_id)

    monkeypatch.setattr(WorkflowRunner, "run", child_still_running)
    result = await runner._execute_subworkflow_step(
        step,
        parent_run.variables,
        parent_run,
        db_session,
    )

    child_runs = (await db_session.execute(
        select(WorkflowRun).where(WorkflowRun.workflow_id == child_workflow.id)
    )).scalars().all()
    assert [child.id for child in child_runs] == [child_run_id]
    assert resumed_child_ids == [child_run_id]
    assert result["status"] == "paused"
    assert result["subrun_id"] == child_run_id


@pytest.mark.asyncio
async def test_subworkflow_grant_failure_does_not_commit_uncheckpointed_child(
    runner,
    monkeypatch,
    db_session,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    user_id = generate_ulid()
    child_workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        created_by=user_id,
        name="Grant-protected child",
        trigger_type="manual",
        steps=[
            {"id": "start", "type": "trigger", "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
        variables={},
        tags=[],
    )
    parent_workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        created_by=user_id,
        name="Grant-protected parent",
        trigger_type="manual",
        steps=[],
        variables={},
        tags=[],
    )
    parent_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=parent_workflow.id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        status="running",
        current_step_id="publish",
        variables={},
        step_results={},
        trigger_data={},
        execution_trace=[],
        started_by=user_id,
    )
    db_session.add_all([child_workflow, parent_workflow, parent_run])
    await db_session.commit()

    async def deny_grant(*_args, **_kwargs):
        raise PermissionError("grant denied")

    monkeypatch.setattr(
        "packages.core.services.workflow_action_grant_service."
        "create_workflow_wait_youtube_publication_grant",
        deny_grant,
    )
    with pytest.raises(PermissionError, match="grant denied"):
        await runner._execute_subworkflow_step(
            {
                "id": "publish",
                "type": "subworkflow",
                "config": {
                    "workflow_id": child_workflow.id,
                    "workflow_wait_authorization": {},
                },
                "next": [],
            },
            parent_run.variables,
            parent_run,
            db_session,
        )

    # The run loop records the failed parent step and commits afterward.
    await db_session.commit()
    child_runs = (await db_session.execute(
        select(WorkflowRun).where(WorkflowRun.workflow_id == child_workflow.id)
    )).scalars().all()
    assert child_runs == []
    assert "publish" not in (parent_run.step_results or {})


@pytest.mark.asyncio
async def test_subworkflow_creates_workflow_wait_grant_before_executor_starts(
    runner,
    monkeypatch,
):
    target = SimpleNamespace(
        id="wf-youtube-executor",
        variables={},
        steps=[
            {"id": "start", "type": "trigger", "next": ["upload"]},
            {"id": "upload", "type": "agent", "next": ["publish"]},
            {"id": "publish", "type": "agent", "next": []},
        ],
    )

    class Result:
        def scalar_one_or_none(self):
            return target

    class Db:
        child = None

        class NestedTransaction:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_exc):
                return False

        async def execute(self, _query):
            return Result()

        def add(self, child):
            self.child = child

        def begin_nested(self):
            return self.NestedTransaction()

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _child):
            return None

    db = Db()
    parent = _run({"youtube_title": "Approved Stickman title"})
    parent.workspace_id = "workspace-1"
    parent.started_by = "user-1"
    parent.step_results = {
        "approve_video": {
            "status": "completed",
            "approved": True,
            "approved_by": "user-1",
        }
    }
    calls = []

    async def create_grant(_db, **kwargs):
        calls.append(kwargs)
        trigger_data = dict(kwargs["executor_run"].trigger_data or {})
        trusted = dict(trigger_data.get("_workflow_runtime_context") or {})
        trusted["workflow_action_grant_id"] = "grant-1"
        trigger_data["_workflow_runtime_context"] = trusted
        kwargs["executor_run"].trigger_data = trigger_data
        return SimpleNamespace(id="grant-1")

    async def complete_child(_runner, _run_id):
        assert (
            db.child.trigger_data["_workflow_runtime_context"]
            ["workflow_action_grant_id"]
            == "grant-1"
        )
        db.child.status = "completed"

    monkeypatch.setattr(
        "packages.core.services.workflow_action_grant_service."
        "create_workflow_wait_youtube_publication_grant",
        create_grant,
    )
    monkeypatch.setattr(WorkflowRunner, "run", complete_child)
    result = await runner._execute_subworkflow_step(
        {
            "id": "publish",
            "type": "subworkflow",
            "config": {
                "workflow_id": target.id,
                "workflow_wait_authorization": {
                    "kind": "youtube_publication_v1",
                    "source": "workflow_wait",
                    "approved_wait_step_id": "approve_video",
                    "destination": "studio.youtube.com",
                    "upload_step_id": "upload",
                    "publish_step_id": "publish",
                    "artifact": {
                        "workflow_project_id": "project-1",
                        "document_id": "document-1",
                        "fs_path": "Knowledge/video.mp4",
                        "upload_path": "/workspace/Knowledge/video.mp4",
                        "sha256": "a" * 64,
                    },
                    "publication": {
                        "title": "{{youtube_title}}",
                        "description": "Approved description",
                        "audience": "not_made_for_kids",
                        "visibility": "public",
                    },
                    "ttl_seconds": 3600,
                },
            },
        },
        parent.variables,
        parent,
        db,
    )

    assert result["status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["root_run"] is parent
    assert calls[0]["executor_run"] is db.child
    assert calls[0]["executor_workflow"] is target
    assert (
        calls[0]["declaration"]["publication"]["title"]
        == "Approved Stickman title"
    )


def test_continues_on_error_flag():
    """on_error=continue lets a failed step advance the run instead of halting."""
    from packages.core.ai.workflow_runner import _continues_on_error
    assert _continues_on_error({"config": {"on_error": "continue"}}) is True
    assert _continues_on_error({"config": {"on_error": "stop"}}) is False
    assert _continues_on_error({"config": {}}) is False
    assert _continues_on_error({}) is False


# ── loop / classifier / webhook (previously unhandled -> crashed) ──


def test_foreach_subworkflow_is_a_canonical_node_type():
    from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES

    assert "foreach_subworkflow" in CANONICAL_NODE_TYPES


@pytest.mark.parametrize(
    ("step_type", "config", "missing_field"),
    [
        ("workflow_project", {"operation": "create"}, "project_type"),
        (
            "workflow_project",
            {
                "operation": "claim",
                "project_type": "daily_content_batch",
                "schema_version": 1,
                "state_schema": {"type": "object"},
            },
            "project_key",
        ),
        (
            "workflow_project",
            {
                "operation": "patch",
                "project_type": "product_video",
                "schema_version": 1,
                "state_schema": {"type": "object"},
            },
            "project_id",
        ),
        ("workflow_project", {"operation": "lease_due", "project_type": "queue", "schema_version": 1, "state_schema": {"type": "object"}, "due_path": "due_at"}, "pending_stages"),
        ("workflow_project", {"operation": "complete_lease", "project_id": "project", "project_type": "queue", "schema_version": 1, "state_schema": {"type": "object"}}, "current_stage"),
        ("workflow_action_grant", {"operation": "create"}, "approval_step_id"),
        ("browser_effect", {"operation": "transition", "record": {}}, "target_status"),
    ],
)
def test_durable_orchestration_node_contracts_reject_missing_config(
    step_type,
    config,
    missing_field,
):
    from packages.core.services.workflow_service import validate_workflow_steps

    result = validate_workflow_steps(
        [
            {"id": "start", "type": "trigger", "next": ["durable"]},
            {"id": "durable", "type": step_type, "config": config, "next": []},
        ]
    )

    assert result["valid"] is False
    assert any(
        error["code"] == "invalid_node_config" and missing_field in error["message"]
        for error in result["errors"]
    )

@pytest.mark.asyncio
async def test_webhook_node_passthrough(runner):
    res = await runner._execute_step({"id": "w", "type": "webhook", "name": "Hook"}, _run(), None)
    assert res["status"] == "completed"


@pytest.mark.asyncio
async def test_classifier_routes_to_agent_step(runner, monkeypatch):
    seen = {}
    async def fake_agent(step, variables, entity_id, user_id, runtime_context, db):
        seen["t"] = step["type"]
        return {"status": "completed", "output": "billing"}
    monkeypatch.setattr(runner, "_execute_agent_step", fake_agent)
    res = await runner._execute_step({"id": "c", "type": "classifier", "config": {}}, _run(), None)
    assert seen["t"] == "classifier" and res["output"] == "billing"


@pytest.mark.asyncio
async def test_loop_runs_sub_steps_per_item(runner):
    step = {
        "id": "lp", "type": "loop",
        "config": {
            "over": "names", "item_var": "name", "output_var": "results",
            "steps": [{"type": "transform", "config": {"set": {"greeting": "hi {{name}}"}}}],
        },
    }
    run = _run({"names": ["Ana", "Bo"]})
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "completed"
    assert len(res["output"]) == 2          # one iteration per item
    assert res["output_var"] == "results"


@pytest.mark.asyncio
async def test_loop_inline_list_and_cap(runner):
    step = {"id": "lp", "type": "loop", "config": {
        "items": [1, 2, 3, 4, 5], "max_iterations": 3,
        "steps": [{"type": "transform", "config": {"set": {"x": "{{index}}"}}}],
    }}
    res = await runner._execute_step(step, _run(), None)
    assert len(res["output"]) == 3          # capped at 3


# ── wait (timer / approval) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_timer_completes_inline(runner):
    """A short timer wait sleeps inline and continues — does not pause."""
    step = {"id": "w", "type": "wait", "name": "Pause",
            "config": {"wait_type": "timer", "duration_seconds": 0.05}}
    res = await runner._execute_step(step, _run(), None)
    assert res["status"] == "completed"
    assert res["wait_type"] == "timer"


@pytest.mark.asyncio
async def test_wait_long_timer_pauses_and_schedules_resume(runner, monkeypatch):
    """A long timer records durable resume intent without broker I/O."""
    monkeypatch.setattr(
        runner,
        "enqueue_resume",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("timer intent must commit before broker publication")
        ),
    )
    step = {"id": "w", "type": "wait", "name": "Pause",
            "config": {"wait_type": "timer", "duration_seconds": 100000}}
    run = _run()
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "paused"
    assert res["auto_resume_scheduled"] is True
    assert res["resume_at"]
    assert run.continuation_token
    assert run.continuation_due_at.isoformat() == res["resume_at"]
    assert run.continuation_next_attempt_at == run.continuation_due_at


@pytest.mark.asyncio
async def test_wait_approval_pauses(runner):
    """Approval waits always pause the run."""
    step = {"id": "w", "type": "wait", "name": "Approve",
            "config": {"wait_type": "approval", "message": "Need sign-off"}}
    run = _run()
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "paused"
    assert run.status == "paused"


@pytest.mark.asyncio
async def test_wait_input_auto_completes_from_existing_declared_values(runner):
    run = _run({"cover_art": [{"document_id": "cover-doc"}]})
    step = {
        "id": "cover_input",
        "type": "wait",
        "config": {
            "wait_type": "input",
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
        },
    }

    result = await runner._execute_step(step, run, None)

    assert result["status"] == "completed"
    assert result["output"] == {"cover_art": [{"document_id": "cover-doc"}]}
    assert result["output_var"] == "release_inputs"


@pytest.mark.asyncio
async def test_wait_input_pauses_with_schema_when_required_value_is_missing(runner):
    run = _run()
    schema = {"type": "object", "properties": {"cover_art": {"type": "array", "minItems": 1}}, "required": ["cover_art"]}
    step = {"id": "cover_input", "type": "wait", "config": {"wait_type": "input", "input_schema": schema}}

    result = await runner._execute_step(step, run, None)

    assert result == {
        "status": "paused",
        "output": "Waiting for input",
        "wait_type": "input",
        "response_variable": "inputs",
        "input_schema": schema,
    }


@pytest.mark.asyncio
async def test_wait_input_safe_dispatch_pauses_before_required_submission(runner):
    schema = {"type": "object", "properties": {"cover_art": {"type": "array", "minItems": 1}}, "required": ["cover_art"]}
    step = {"id": "cover_input", "type": "wait", "config": {"wait_type": "input", "input_schema": schema}}

    result = await runner._execute_step_safe(step, _run(), None)

    assert result["status"] == "paused"
    assert result["input_schema"] == schema


@pytest.mark.asyncio
async def test_wait_input_form_opens_with_defaults_bound_to_the_run(runner):
    schema = {"type": "object", "required": ["title"], "properties": {
        "title": {"type": "string", "default": "{{draft.title}}"},
        "summary": {"type": "string", "default": "From {{draft.author}}"},
        "notes": {"type": "string", "default": "{{missing.value}}"},
    }}
    step = {"id": "confirm", "type": "wait", "config": {"wait_type": "input", "input_schema": schema}}

    result = await runner._execute_step(step, _run({"draft": {"title": "Q3 plan", "author": "Ana"}}), None)

    assert result["status"] == "paused"
    properties = result["input_schema"]["properties"]
    assert properties["title"]["default"] == "Q3 plan"
    assert properties["summary"]["default"] == "From Ana"
    # An unresolved binding is never shown to the person as {{...}} text.
    assert "default" not in properties["notes"]
    # The configured form keeps its bindings for the next run.
    assert schema["properties"]["title"]["default"] == "{{draft.title}}"


def test_input_wait_rejects_refresh_invalid_submission():
    config = {
        "wait_type": "input",
        "response_variable": "release_inputs",
        "input_schema": {
            "type": "object",
            "properties": {"cover_art": {"type": "array", "minItems": 1}},
            "required": ["cover_art"],
        },
    }

    with pytest.raises(ValueError, match="cover_art"):
        workflow_input_wait_value(config, {"release_inputs": {}})


def test_input_wait_normalizes_flat_values_without_unrelated_submission_data():
    config = {
        "response_variable": "release_inputs",
        "input_schema": {
            "type": "object",
            "properties": {"cover_art": {"type": "array", "minItems": 1}},
            "required": ["cover_art"],
        },
    }

    response_variable, value, _schema = workflow_input_wait_value(config, {
        "cover_art": [{"document_id": "cover-doc"}],
        "unrelated_override": "must not persist",
    })

    assert response_variable == "release_inputs"
    assert value == {"cover_art": [{"document_id": "cover-doc"}]}


def test_input_wait_drops_undeclared_nested_values_before_persisting():
    config = {
        "response_variable": "release_inputs",
        "input_schema": {
            "type": "object",
            "properties": {"cover_art": {"type": "array", "minItems": 1}},
            "required": ["cover_art"],
        },
    }

    _response_variable, value, _schema = workflow_input_wait_value(config, {
        "release_inputs": {
            "cover_art": [{"document_id": "cover-doc"}],
            "unrelated_override": "must not persist",
        },
    })

    assert value == {"cover_art": [{"document_id": "cover-doc"}]}


def test_input_wait_lists_one_field_per_item_the_run_found_missing():
    """A form asks for each missing item in its own field, from a list an earlier step produced."""
    from packages.core.ai.workflow_runner import _bind_schema_defaults

    config = {
        "response_variable": "supplier_answer",
        "input_schema": {"type": "object", "properties": {
            "answers": {"type": "object", "default": {}, "x-fields-from": "{{review.gaps}}"},
        }},
    }
    run_variables = {"review": {"gaps": [
        {"label": "Tax ID", "hint": "As on the invoice"}, "Bank country", "Tax ID",
        {"label": "Address", "value": "1 Main St", "source": "invoice.pdf"},
    ]}}

    shown = _bind_schema_defaults(config["input_schema"], run_variables)["properties"]["answers"]
    assert list(shown["properties"]) == shown["x-ui"]["order"] == ["Tax ID", "Bank country", "Address"]
    assert shown["x-ui"]["layout"] == "list"
    assert shown["properties"]["Tax ID"]["x-ui"]["placeholder"] == "As on the invoice"
    # What an earlier step found is filled in for the person to check, with its source.
    assert shown["properties"]["Address"]["default"] == "1 Main St"
    assert shown["properties"]["Address"]["x-ui"]["source"] == "invoice.pdf"
    assert "default" not in shown["properties"]["Tax ID"]
    assert "x-fields-from" not in shown
    # The submission alone is validated against the form the run showed.
    # Items left blank are not answers.
    submitted = {"supplier_answer": {"answers": {"Bank country": "DE", "Tax ID": "  "}}}
    assert workflow_input_wait_value(config, submitted, context=run_variables)[1] == {
        "answers": {"Bank country": "DE"},
    }
    with pytest.raises(ValueError, match="Website"):
        workflow_input_wait_value(config, {"supplier_answer": {"answers": {"Website": "x"}}},
                                  context=run_variables)


def test_input_wait_rejects_non_object_schema():
    with pytest.raises(ValueError, match="object input_schema"):
        workflow_input_wait_value({"input_schema": {"type": "array"}}, {})


def test_paused_tool_continuation_becomes_runnable_without_false_completion(runner):
    workflow = SimpleNamespace(steps=[{
        "id": "fanout",
        "type": "connector",
        "config": {},
    }])
    run = _run()
    run.current_step_id = "fanout"
    run.step_results = {
        "fanout": {
            "status": "paused",
            "resume_strategy": "reexecute",
        }
    }

    assert [
        step["id"] for step in runner._find_runnable_steps(workflow, run)
    ] == ["fanout"]
    assert runner._all_steps_done(workflow, run) is False


# ── notify ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_dispatches(runner, monkeypatch):
    """Notify with a recipient calls the notification dispatcher."""
    calls = {}

    async def fake_notify(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("packages.core.services.notify.notify", fake_notify)
    run = _run({"who": "Sam"})
    run.started_by = "user1"
    step = {"id": "n", "type": "notify", "name": "Ping",
            "config": {"channel": "slack", "message": "Hi {{who}}"}}
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "completed"
    assert calls["user_id"] == "user1"
    assert calls["body"] == "Hi Sam"          # templated
    assert calls["channels"] == ["slack"]


@pytest.mark.asyncio
async def test_notify_skips_without_recipient(runner):
    """No triggering user → degrade gracefully, never fail the run."""
    step = {"id": "n", "type": "notify", "config": {"message": "hello"}}
    res = await runner._execute_step(step, _run(), None)
    assert res["status"] == "completed"


# ── data flow: auto variables + dotted templates ─────────────────────────


def test_render_template_dotted():
    from packages.core.ai.workflow_runner import _render_template
    v = {"l": "hi", "r": {"context": "doc text"}}
    assert _render_template("{{l}}", v) == "hi"
    assert _render_template("got {{r.context}}", v) == "got doc text"
    assert _render_template("{{missing}}", v) == "{{missing}}"   # visible, not blanked
    assert _render_template("{{r.nope}}", v) == "{{r.nope}}"


def test_render_template_reads_lists_and_structures_as_text_not_python_repr():
    from packages.core.ai.workflow_runner import _render_template
    v = {"episodes": ["EP.05", "EP.06"], "refs": [{"name": "店面.jpg"}], "none": []}
    # Review titles and Chat lines interpolate these; ['EP.05'] leaked before.
    assert _render_template("审批 {{episodes}} 逐字剧本", v) == "审批 EP.05, EP.06 逐字剧本"
    assert _render_template("refs: {{refs}}", v) == 'refs: [{"name": "店面.jpg"}]'
    assert _render_template("[{{none}}]", v) == "[]"
    # A list of sentences (blockers) must not read "platform., The next".
    blockers = ["Connect the account first.", "Confirm the recipient."]
    assert _render_template("Fix: {{b}}", {"b": blockers}) == "Fix: Connect the account first. Confirm the recipient."


def test_stop_step_lists_items_as_separate_lines_under_the_message():
    from packages.core.ai.workflow_runner import WorkflowRunner
    step = {"config": {"message": "Fix these, then retry:", "items": "{{setup.blockers}}"}}
    variables = {"setup": {"blockers": ["Connect Instagram.", "Confirm the recipient.", "Connect Instagram."]}}
    result = WorkflowRunner()._execute_stop_step(step, variables)
    assert result["status"] == "failed"
    # Duplicates collapse; each item keeps its own line for the run page.
    assert result["error"] == "Fix these, then retry:\n- Connect Instagram.\n- Confirm the recipient."
    # Without items the message is unchanged.
    plain = WorkflowRunner()._execute_stop_step({"config": {"message": "Stop here."}}, {})
    assert plain["error"] == "Stop here."


def test_resolve_binding_preserves_type_for_single_ref():
    """A bare {{ref}} resolves to the raw value (dict/list/number preserved);
    mixed text interpolates to a string; unknown refs stay visible."""
    from packages.core.ai.workflow_runner import _resolve_binding
    v = {"http": {"status_code": 200, "body": "ok"}, "n": 3}
    assert _resolve_binding("{{http}}", v) == {"status_code": 200, "body": "ok"}  # whole object
    assert _resolve_binding("{{http.body}}", v) == "ok"
    assert _resolve_binding("{{n}}", v) == 3                          # number, not "3"
    assert _resolve_binding("code is {{http.status_code}}", v) == "code is 200"  # interpolated
    assert _resolve_binding("{{missing}}", v) == "{{missing}}"        # visible
    assert _resolve_binding("plain", v) == "plain"


@pytest.mark.asyncio
async def test_unconfigured_connector_skips_only_with_trusted_import_provenance(runner):
    """Only a connector proven to belong to an imported snapshot may skip."""
    imported = {
        "id": "c",
        "type": "connector",
        "config": {"n8n": {"type": "n8n-nodes-base.slack"}},
        "meta": {"source_tool": "n8n"},
    }
    imported_run = _run({})
    imported_run.execution_snapshot = {"import_source": "n8n"}
    res = await runner._execute_step(imported, imported_run, None)
    assert res["status"] == "completed" and res.get("skipped") is True

    spoofed = await runner._execute_step(imported, _run({}), None)
    assert spoofed["status"] == "failed"
    assert spoofed.get("skipped") is not True

    native_connector = {"id": "c2", "type": "connector", "config": {}}
    native = await runner._execute_step(native_connector, _run({}), None)
    assert native["status"] == "failed"

    bare_tool = {"id": "t", "type": "tool", "config": {}}
    res2 = await runner._execute_tool_step(bare_tool, {}, "e", "u", {})
    assert res2["status"] == "failed"


@pytest.mark.asyncio
async def test_connector_output_contract_parses_structured_tool_result(runner, monkeypatch):
    async def fake_execute_tool_step(**_kwargs):
        return SimpleNamespace(
            output=json.dumps({"updated": True}),
            envelope=None,
        )

    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_execute_workflow_tool_step",
        fake_execute_tool_step,
    )

    result = await runner._execute_tool_step(
        {
            "id": "update_record",
            "type": "connector",
            "config": {
                "tool": "mcp__records__update_record",
                "args": {"record_id": "rec-1"},
                "output_schema": {
                    "type": "object",
                    "required": ["updated"],
                    "properties": {"updated": {"type": "boolean"}},
                },
            },
        },
        {},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )

    assert result["status"] == "completed"
    assert result["output"] == {"updated": True}


@pytest.mark.asyncio
async def test_json_tool_output_with_error_key_fails_the_workflow_step(runner, monkeypatch):
    async def fake_execute_tool_step(**_kwargs):
        return SimpleNamespace(
            output=json.dumps({"error": "Workspace storage is unavailable"}),
            envelope=None,
        )

    monkeypatch.setattr(
        workflow_runner_module,
        "runtime_execute_workflow_tool_step",
        fake_execute_tool_step,
    )

    result = await runner._execute_tool_step(
        {
            "id": "materialize_project_storage",
            "type": "tool",
            "config": {
                "tool": "generate_file",
                "args": {"kind": "code"},
                "output_format": "json",
                "output_var": "project_storage_receipt",
            },
        },
        {},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )

    assert result["status"] == "failed"
    assert result["code"] == "tool_step_failed"
    assert result["error"] == "Workspace storage is unavailable"


@pytest.mark.asyncio
async def test_imported_node_failure_skips_and_continues(runner, monkeypatch):
    """An *imported* node that fails (live endpoint/credential absent here) is
    converted to a skip so the whole workflow still runs through. A user-built
    node still fails; an imported ``stop`` keeps its deliberate halt."""
    async def boom(step, _run, _db):
        return {"status": "failed", "error": "boom"}
    monkeypatch.setattr(runner, "_execute_step", boom)

    n8n_run = _run({})
    n8n_run.execution_snapshot = {"import_source": "n8n"}
    imported = {
        "id": "a",
        "type": "http",
        "config": {"n8n": {"type": "n8n-nodes-base.httpRequest"}},
        "meta": {"source_tool": "n8n"},
    }
    res = await runner._execute_step_safe(imported, n8n_run, None)
    assert res["status"] == "completed" and res.get("skipped") is True and res.get("error") == "boom"

    spoofed = await runner._execute_step_safe(imported, _run({}), None)
    assert spoofed["status"] == "failed"
    assert spoofed.get("skipped") is not True

    # ComfyUI/Dify imports mark provenance via meta.source_tool (no config.n8n)
    comfy = {"id": "d", "type": "image", "config": {}, "meta": {"source_tool": "comfyui"}}
    comfy_run = _run({})
    comfy_run.execution_snapshot = {"import_source": "comfyui"}
    res_c = await runner._execute_step_safe(comfy, comfy_run, None)
    assert res_c["status"] == "completed" and res_c.get("skipped") is True

    user_built = {"id": "b", "type": "http", "config": {}}
    res2 = await runner._execute_step_safe(user_built, _run({}), None)
    assert res2["status"] == "failed"

    imported_stop = {"id": "c", "type": "stop", "config": {"n8n": {"type": "x"}}}
    res3 = await runner._execute_step_safe(imported_stop, n8n_run, None)
    assert res3["status"] == "failed"  # stop still halts

    strict = {"id": "s", "type": "http", "config": {
        "n8n": {"type": "n8n-nodes-base.httpRequest"}, "strict_execution": True,
    }}
    res4 = await runner._execute_step_safe(strict, n8n_run, None)
    assert res4["status"] == "failed" and res4.get("skipped") is not True

    async def partial_fanout(step, _run, _db):
        return {
            "status": "paused",
            "code": "tool_fanout_partial",
            "error": "Connector account fan-out returned partial results",
            "resume_strategy": "reexecute",
        }

    monkeypatch.setattr(runner, "_execute_step", partial_fanout)
    partial = await runner._execute_step_safe(imported, n8n_run, None)
    assert partial["status"] == "paused"
    assert partial.get("skipped") is not True


def test_template_resolves_refs_with_spaces():
    """n8n uses the node *name* as its id, so refs contain spaces/punctuation.
    These must resolve as a whole key, while non-variable expressions
    (``{{ $env.X }}``) are left visible, unchanged."""
    from packages.core.ai.workflow_runner import _render_template, _resolve_binding
    v = {"When User Completes Form": {"company": "Acme"}, "Person's company": "Globex"}
    assert _render_template("Co={{When User Completes Form.company}}", v) == "Co=Acme"
    assert _render_template("{{Person's company}}", v) == "Globex"
    # n8n expressions are not variables → passed through untouched
    expr = "{{ $env.WEBHOOK_URL }}{{ encodeURIComponent(x.trim()) }}"
    assert _render_template(expr, v) == expr
    # single bare ref keeps the raw type even with spaces in the key
    assert _resolve_binding("{{When User Completes Form}}", v) == {"company": "Acme"}


def test_template_resolves_longest_imported_node_prefix_and_array_path(runner):
    from packages.core.ai.workflow_runner import _render_template, _resolve_binding
    values = {
        "Limit to first 3": [{"essay": "one.html"}],
        "HTTP Request - Get my Stars": {"body": []},
    }
    assert _resolve_binding("{{Limit to first 3.0.essay}}", values) == "one.html"
    assert _render_template(
        "url={{Limit to first 3.0.essay}}", values,
    ) == "url=one.html"
    assert runner._eval_atom("len(HTTP Request - Get my Stars.body) == 0", values) is True


def test_coerce_typed_widget_types():
    """Typed bindings coerce like ComfyUI widgets: number→numeric, json→parsed,
    text→string; unparseable values pass through; any/image are untouched."""
    from packages.core.ai.workflow_runner import _coerce_typed
    assert _coerce_typed("42", "number") == 42 and isinstance(_coerce_typed("42", "number"), int)
    assert _coerce_typed("3.5", "number") == 3.5
    assert _coerce_typed("nope", "number") == "nope"          # unparseable → passthrough
    assert _coerce_typed('{"a": 1}', "json") == {"a": 1}
    assert _coerce_typed("not json", "json") == "not json"
    assert _coerce_typed(7, "text") == "7"
    assert _coerce_typed({"x": 1}, "any") == {"x": 1}         # any → untouched
    assert _coerce_typed("kb://a.png", "image") == "kb://a.png"


def test_typed_input_binding_coerces():
    """A typed input coerces its resolved value before the step sees it."""
    from packages.core.ai.workflow_runner import _bind_inputs
    variables = {"http": {"body": "5"}}
    config = {"inputs": [{"key": "count", "value": "{{http.body}}", "type": "number"}]}
    _bind_inputs(config, variables)
    assert variables["count"] == 5 and isinstance(variables["count"], int)


def test_typed_output_binding_coerces(runner):
    """A typed named output coerces the exposed value."""
    run = _run()
    runner._record_step_result(
        {"id": "c", "next": [], "config": {"outputs": [
            {"key": "score", "value": "{{c.raw}}", "type": "number"},
        ]}},
        {"status": "completed", "output": {"raw": "0.8"}},
        run,
    )
    assert run.variables["score"] == 0.8


def test_bind_inputs_maps_into_scope():
    """config.inputs binds named values into the variable scope before the step
    runs; later rows can read earlier ones."""
    from packages.core.ai.workflow_runner import _bind_inputs
    variables = {"fetch": {"body": "hello world"}}
    config = {"inputs": [
        {"key": "page", "value": "{{fetch.body}}"},
        {"key": "greeting", "value": "say: {{page}}"},
    ]}
    _bind_inputs(config, variables)
    assert variables["page"] == "hello world"
    assert variables["greeting"] == "say: hello world"  # reads the earlier binding


def test_named_outputs_expose_result_fields(runner):
    """config.outputs exposes chosen fields of a step's result under names;
    blank value defaults to the whole result."""
    run = _run()
    runner._record_step_result(
        {"id": "h", "next": [], "config": {"outputs": [
            {"key": "status", "value": "{{h.status_code}}"},
            {"key": "raw", "value": ""},  # blank → whole result
        ]}},
        {"status": "completed", "output": {"status_code": 200, "body": "ok"}},
        run,
    )
    assert run.variables["h"] == {"status_code": 200, "body": "ok"}  # auto var
    assert run.variables["status"] == 200  # single ref preserves the int type
    assert run.variables["raw"] == {"status_code": 200, "body": "ok"}


def test_trigger_named_outputs_resolve_from_trigger_payload(runner):
    """Run-input outputs must read from the trigger node's recorded payload.

    A self-reference such as ``{{source}}`` leaves the literal placeholder in
    scope because run inputs initially live under the trigger output object.
    """
    run = _run()
    runner._record_step_result(
        {
            "id": "start",
            "next": [],
            "config": {
                "outputs": [
                    {"key": "source", "value": "{{start.source}}", "type": "text"},
                ],
            },
        },
        {"status": "completed", "output": {"source": "approved topic"}},
        run,
    )

    assert run.variables["source"] == "approved topic"


@pytest.mark.asyncio
async def test_trigger_projects_declared_manual_run_inputs_into_its_output(runner):
    run = _run({
        "youtube_visibility": "public",
        "request": {"target_duration": "120s"},
    })
    run.trigger_data = {"source": "manual"}
    step = {
        "id": "start",
        "type": "trigger",
        "config": {
            "run_inputs": [
                {"key": "youtube_visibility", "type": "string"},
                {
                    "key": "target_duration",
                    "target": "request.target_duration",
                    "type": "string",
                },
            ],
            "outputs": [
                {
                    "key": "youtube_visibility",
                    "value": "{{start.youtube_visibility}}",
                    "type": "text",
                },
            ],
        },
    }

    result = await runner._execute_step(step, run, db=None)
    assert result["output"] == {
        "source": "manual",
        "youtube_visibility": "public",
        "target_duration": "120s",
    }

    runner._record_step_result(step, result, run)
    assert run.variables["youtube_visibility"] == "public"


def test_step_output_stored_as_auto_var(runner):
    """Every completed step's output is stored under its id (the auto var),
    plus an optional explicit output_var alias."""
    run = _run()
    runner._record_step_result({"id": "a", "next": []}, {"status": "completed", "output": "hello"}, run)
    assert run.variables.get("a") == "hello"
    runner._record_step_result(
        {"id": "b", "next": []},
        {"status": "completed", "output": "world", "output_var": "greeting"},
        run,
    )
    assert run.variables.get("b") == "world"        # auto var by id
    assert run.variables.get("greeting") == "world"  # explicit alias


def test_graph_runner_preserves_fanout_and_waits_for_fanin(runner):
    """Both branches after a fan-out run, and their shared successor waits for both."""
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["left", "right"]},
        {"id": "left", "type": "transform", "next": ["join"]},
        {"id": "right", "type": "transform", "next": ["join"]},
        {"id": "join", "type": "merge", "next": []},
    ])
    run = _run()
    run.current_step_id = "start"

    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["start"]
    runner._record_step_result(workflow.steps[0], {"status": "completed"}, run)
    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["left", "right"]

    runner._record_step_result(workflow.steps[1], {"status": "completed"}, run)
    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["right"]
    runner._record_step_result(workflow.steps[2], {"status": "completed"}, run)
    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["join"]


def test_explicit_empty_condition_branch_completes_without_true_path(runner):
    """An empty false branch is terminal and must not fall back to step.next."""
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["check"]},
        {"id": "check", "type": "condition", "next": ["yes"], "false_next": []},
        {"id": "yes", "type": "transform", "next": []},
    ])
    run = _run()
    run.current_step_id = "start"
    runner._record_step_result(workflow.steps[0], {"status": "completed"}, run)
    runner._record_step_result(
        workflow.steps[1],
        {"status": "completed", "next_override": []},
        run,
    )
    assert runner._find_runnable_steps(workflow, run) == []
    assert runner._all_steps_done(workflow, run) is True


def test_graph_routes_win_when_steps_also_include_dependency_metadata(runner):
    """Frozen/imported workflows may carry depends_on alongside graph edges.

    Dependency metadata must not disable condition routing and make both the
    success and stop branches runnable.
    """
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["check"]},
        {
            "id": "check",
            "type": "condition",
            "depends_on": ["start"],
            "true_next": ["produce"],
            "false_next": ["stop"],
        },
        {
            "id": "produce",
            "type": "agent",
            "depends_on": ["check"],
            "next": ["done"],
        },
        {
            "id": "stop",
            "type": "stop",
            "depends_on": ["check"],
        },
        {
            "id": "done",
            "type": "end",
            "depends_on": ["produce"],
            "next": [],
        },
    ])
    run = _run()
    run.step_results = {
        "start": {"status": "completed"},
        "check": {"status": "completed", "next_override": ["produce"]},
    }

    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["produce"]
    run.step_results["produce"] = {"status": "completed"}
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["done"]


def test_graph_recovers_a_missing_condition_receipt_from_completed_inputs(runner):
    """A legacy condition receipt must not activate both normalized branches."""
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["produce"]},
        {"id": "produce", "type": "agent", "next": ["check"]},
        {
            "id": "check",
            "type": "condition",
            "config": {"expression": 'produce.status == "completed"'},
            # Frozen workflow graphs can materialize both branches in ``next``.
            "next": ["blocked", "publish"],
            "true_next": ["publish"],
            "false_next": ["blocked"],
        },
        {"id": "blocked", "type": "stop", "next": []},
        {"id": "publish", "type": "agent", "next": []},
    ])
    run = _run()
    run.step_results = {
        "start": {"status": "completed"},
        "produce": {"status": "completed", "output": {"status": "completed"}},
        # A historical recovery wrote output but dropped the branch receipt.
        "check": {"status": "completed", "output": {"status": "completed"}},
    }

    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["publish"]


def test_checkpoint_retry_starts_at_the_requested_step(runner):
    """A retry must not re-enter an ambiguous upstream condition branch."""
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["check"]},
        {
            "id": "check",
            "type": "condition",
            "config": {"expression": "ready == true"},
            "next": ["blocked", "publish"],
            "true_next": ["publish"],
            "false_next": ["blocked"],
        },
        {"id": "blocked", "type": "stop", "next": []},
        {"id": "publish", "type": "agent", "next": []},
    ])
    run = _run({"ready": False})
    run.current_step_id = "publish"
    run.retry_from_step_id = "publish"
    run.step_results = {
        "start": {"status": "completed"},
        "check": {"status": "completed", "output": {"legacy": True}},
    }

    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["publish"]


def test_hybrid_workflow_uses_dependency_edges_until_an_explicit_branch(runner):
    """Blueprint workflows use dependency edges for linear setup and graph routes for gates."""
    workflow = SimpleNamespace(steps=[
        {"id": "extract", "type": "extract"},
        {"id": "prepare", "type": "transform", "depends_on": ["extract"]},
        {
            "id": "check",
            "type": "condition",
            "depends_on": ["prepare"],
            "true_next": ["produce"],
            "false_next": ["stop"],
        },
        {"id": "produce", "type": "agent", "depends_on": ["check"]},
        {"id": "stop", "type": "stop", "depends_on": ["check"]},
        {"id": "done", "type": "end", "depends_on": ["produce"]},
    ])
    run = _run()

    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["extract"]
    run.step_results = {"extract": {"status": "completed"}}
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["prepare"]
    run.step_results["prepare"] = {"status": "completed"}
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["check"]
    run.step_results["check"] = {"status": "completed", "next_override": ["produce"]}
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["produce"]
    run.step_results["produce"] = {"status": "completed"}
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["done"]


def test_n8n_fan_in_waits_for_indirect_active_branch(runner):
    """A condition can feed Merge directly on one input port while its other
    input passes through transform nodes. Merge must not race the transform."""
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["choose"]},
        {
            "id": "choose", "type": "condition", "next": ["coupon", "merge"],
            "true_next": ["coupon", "merge"], "false_next": ["plain", "merge"],
        },
        {"id": "coupon", "type": "transform", "next": ["html"]},
        {"id": "plain", "type": "transform", "next": ["html"]},
        {"id": "html", "type": "transform", "next": ["merge"]},
        {"id": "merge", "type": "merge", "next": ["done"]},
        {"id": "done", "type": "end", "next": []},
    ])
    run = _run()
    run.step_results = {
        "start": {"status": "completed"},
        "choose": {"status": "completed", "next_override": ["coupon", "merge"]},
        "coupon": {"status": "completed"},
    }
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["html"]
    run.step_results["html"] = {"status": "completed"}
    assert [step["id"] for step in runner._find_runnable_steps(workflow, run)] == ["merge"]


def test_entry_step_id_requires_explicit_trigger():
    """Execution never guesses an entry from array order or graph indegree."""
    from packages.core.services.workflow_service import entry_step_id
    steps = [
        {"id": "mid", "type": "transform", "next": ["end"]},     # steps[0], mid-graph
        {"id": "trig", "type": "trigger", "next": ["mid"]},
        {"id": "end", "type": "end", "next": []},
    ]
    assert entry_step_id(steps) == "trig"
    no_trigger = [
        {"id": "b", "type": "transform", "next": ["c"]},
        {"id": "a", "type": "http", "next": ["b"]},              # indegree 0
        {"id": "c", "type": "end", "next": []},
    ]
    assert entry_step_id(no_trigger) is None
    assert entry_step_id([]) is None


def test_note_is_never_an_entry_point():
    """A free-floating note never replaces the required trigger entry."""
    from packages.core.services.workflow_service import entry_step_id
    steps = [
        {"id": "note1", "type": "note", "config": {"text": "reminder"}, "next": []},
        {"id": "start", "type": "trigger", "next": ["a"]},
        {"id": "a", "type": "http", "next": ["b"]},
        {"id": "b", "type": "end", "next": []},
    ]
    assert entry_step_id(steps) == "start"
    assert entry_step_id([{"id": "n", "type": "note", "next": []}]) is None


@pytest.mark.asyncio
async def test_input_snapshot_captured_for_node_panel(runner, monkeypatch):
    """Each step result carries a capped snapshot of the data it received, so the
    node config panel can show a per-node Input view (n8n-style)."""
    async def fake(step, run, db):
        return {"status": "completed", "output": "ok"}
    monkeypatch.setattr(runner, "_execute_step", fake)
    run = _run({"name": "Ada", "score": 85, "obj": {"a": 1}})
    res = await runner._execute_step_safe({"id": "s", "type": "tool", "config": {}}, run, None)
    assert res["inputs"]["name"] == "Ada"
    assert res["inputs"]["score"] == 85
    assert res["inputs"]["obj"] == '{"a": 1}'      # complex values stringified


def test_rss_item_pipeline_maps_merges_filters_limits_and_aggregates(runner, monkeypatch):
    xml = """<rss><channel><item><title>Fresh</title>
      <pubDate>Sun, 19 Jul 2026 12:00:00 GMT</pubDate>
      <description><![CDATA[<p>Useful story</p>]]></description>
      <content:encoded xmlns:content="urn:content"><![CDATA[<b>Full story</b>]]></content:encoded>
    </item></channel></rss>"""
    articles = workflow_runner_module._parse_rss_feed(xml)
    assert articles[0]["title"] == "Fresh"
    assert articles[0]["contentSnippet"] == "Useful story"
    assert articles[0]["content:encoded"] == "<b>Full story</b>"

    run = _run({"RSS": articles})
    mapped = runner._execute_transform_step({
        "id": "Fields", "config": {
            "items": "{{RSS}}",
            "set": {"title": "{{title}}", "body": "{{content:encoded}}"},
        },
    }, dict(run.variables), run)
    assert mapped["output"] == [{"title": "Fresh", "body": "<b>Full story</b>"}]

    merged = runner._execute_merge_step({
        "id": "Merge", "config": {"sources": ["one", "two"], "flatten": True},
    }, {"one": [1, 2], "two": [3]}, run)
    assert merged["output"] == [1, 2, 3]

    monkeypatch.setattr(
        workflow_runner_module,
        "_utc_now",
        lambda: workflow_runner_module.datetime(2026, 7, 19, 18, tzinfo=workflow_runner_module.timezone.utc),
    )
    filtered = runner._execute_filter_step({
        "id": "Filter", "config": {
            "items": [{"pubDate": "Sun, 19 Jul 2026 12:00:00 GMT"},
                      {"pubDate": "Fri, 17 Jul 2026 12:00:00 GMT"}],
            "field": "pubDate", "operation": "after", "relative_days": -1,
        },
    }, {})
    assert len(filtered["output"]) == 1

    aggregated = runner._execute_aggregate_step({
        "id": "Aggregate", "config": {
            "items": [{"title": "A", "body": "12345", "ignored": True}],
            "operation": "collect", "fields": ["title", "body"],
            "max_field_chars": 3, "wrap_key": "data",
        },
    }, {})
    assert aggregated["output"] == {"data": [{"title": "A", "body": "123"}]}


def test_template_resolves_imported_array_paths():
    variables = {"input": {"data": [{"title": "Top story"}]}}
    assert workflow_runner_module._render_template(
        "News: {{input.data.0.title}}", variables,
    ) == "News: Top story"


@pytest.mark.asyncio
async def test_media_node_keeps_referenced_parameter_types(monkeypatch):
    # generate_file validates `duration` as an integer enum; rendering every
    # template to a string made "{{duration_seconds}}" fail as "5".
    captured: dict = {}

    async def fake_tool_step(self, step, variables, entity_id, user_id="", runtime_context=None):
        captured.update(step["config"]["args"])
        return {"status": "completed", "output": {"video_url": "/api/v1/fs/e/v.mp4"}}

    monkeypatch.setattr(WorkflowRunner, "_execute_tool_step", fake_tool_step)
    runner = WorkflowRunner.__new__(WorkflowRunner)
    step = {
        "id": "video",
        "type": "video",
        "config": {
            "prompt": "A {{style}} clip",
            "duration": "{{duration_seconds}}",
            "aspect_ratio": "{{ratio}}",
            "reference_urls": "{{refs}}",
            "save_to_knowledge": "{{save}}",
            "output_var": "video_asset",
        },
    }
    variables = {"style": "calm", "duration_seconds": 5, "ratio": "9:16", "refs": ["/a.png", "/b.png"], "save": True}
    await runner._execute_media_step(step, variables, "entity-1")
    assert captured["duration"] == 5
    assert captured["aspect_ratio"] == "9:16"
    assert captured["reference_urls"] == ["/a.png", "/b.png"]
    assert captured["save_to_knowledge"] is True
    assert captured["prompt"] == "A calm clip"

    captured.clear()
    await runner._execute_media_step(step, {**variables, "duration_seconds": "7"}, "entity-1")
    assert captured["duration"] == 7


@pytest.mark.asyncio
async def test_video_node_waits_for_its_async_job_without_regenerating(monkeypatch):
    # generate_file starts video as an async job ("pending" + job_id). The
    # node must not complete with no video, and a re-check must never start a
    # second (paid) generation.
    calls = {"tool": 0}

    async def fake_tool_step(self, step, variables, entity_id, user_id="", runtime_context=None):
        calls["tool"] += 1
        return {
            "status": "completed",
            "output": {"kind": "video", "status": "pending", "job_id": "job-1", "name": "clip.mp4", "duration": 5},
            "output_var": "video_asset",
        }

    job_state = {"status": "processing", "result_url": None, "error": None}

    async def fake_load_job(job_id, entity_id):
        assert (job_id, entity_id) == ("job-1", "entity-1")
        return dict(job_state)

    monkeypatch.setattr(WorkflowRunner, "_execute_tool_step", fake_tool_step)
    monkeypatch.setattr(workflow_runner_module, "_load_media_job_state", fake_load_job)
    runner = WorkflowRunner.__new__(WorkflowRunner)
    step = {"id": "video", "type": "video", "config": {"prompt": "clip", "output_var": "video_asset"}}
    run = SimpleNamespace(step_results={}, status="running", continuation_token=None,
                          continuation_due_at=None, continuation_next_attempt_at=None)

    first = await runner._execute_media_step(step, {}, "entity-1", run=run)
    assert first["status"] == "paused"
    assert first["resume_strategy"] == "reexecute"
    assert first["media_job_id"] == "job-1"
    assert run.continuation_token and run.continuation_due_at is not None
    assert calls["tool"] == 1

    run.step_results = {"video": first}
    still = await runner._execute_media_step(step, {}, "entity-1", run=run)
    assert still["status"] == "paused" and still["media_job_id"] == "job-1"
    assert calls["tool"] == 1  # re-check only, no second generation

    job_state.update(status="completed", result_url="/api/v1/fs/entity-1/clip.mp4")
    run.step_results = {"video": still}
    done = await runner._execute_media_step(step, {}, "entity-1", run=run)
    assert done["status"] == "completed"
    assert done["output"]["video_url"] == "/api/v1/fs/entity-1/clip.mp4"
    assert done["output"]["status"] == "completed"
    assert done["output_var"] == "video_asset"
    assert calls["tool"] == 1

    job_state.update(status="failed", result_url=None, error="provider rejected the prompt")
    failed = await runner._execute_media_step(step, {}, "entity-1", run=SimpleNamespace(
        step_results={"video": still}, status="running", continuation_token=None,
        continuation_due_at=None, continuation_next_attempt_at=None,
    ))
    assert failed["status"] == "failed"
    assert "provider rejected the prompt" in failed["error"]


@pytest.mark.asyncio
async def test_media_job_state_loader_reads_the_entity_scoped_job(db_session):
    from packages.core.models.media_job import MediaJob

    entity_id = generate_ulid()
    job = MediaJob(entity_id=entity_id, kind="video", status="completed", result_url="/api/v1/fs/e/clip.mp4", model="m", prompt="clip")
    db_session.add(job)
    await db_session.commit()

    state = await workflow_runner_module._load_media_job_state(job.id, entity_id)
    assert state == {"status": "completed", "result_url": "/api/v1/fs/e/clip.mp4", "error": None}
    assert await workflow_runner_module._load_media_job_state(job.id, generate_ulid()) is None


@pytest.mark.asyncio
async def test_media_node_output_is_structured_so_fields_can_be_referenced(monkeypatch):
    # generate_file returns its JSON as a string; downstream nodes read
    # {{image_asset.image_url}}, which cannot walk into a string.
    async def fake_tool_step(self, step, variables, entity_id, user_id="", runtime_context=None):
        return {
            "status": "completed",
            "output": json.dumps({"kind": "image", "image_url": "/api/v1/fs/e/post.png", "model": "m"}),
            "output_var": "image_asset",
        }

    monkeypatch.setattr(WorkflowRunner, "_execute_tool_step", fake_tool_step)
    runner = WorkflowRunner.__new__(WorkflowRunner)
    step = {"id": "image", "type": "image", "config": {"prompt": "p", "output_var": "image_asset"}}
    result = await runner._execute_media_step(step, {}, "entity-1", run=SimpleNamespace(step_results={}))
    assert result["output"] == {"kind": "image", "image_url": "/api/v1/fs/e/post.png", "model": "m"}
    assert workflow_runner_module._lookup_reference("image_asset.image_url", {"image_asset": result["output"]}) == "/api/v1/fs/e/post.png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected_tools", "expected_system"),
    [
        (
            {"standalone_tools": ["mcp__chrome__open_or_reuse", "mcp__chrome__upload"],
             "standalone_system_prompt": "You publish through Chrome."},
            ["mcp__chrome__open_or_reuse", "mcp__chrome__upload"],
            "You publish through Chrome.",
        ),
        ({}, [], "You are a helpful assistant completing a workflow step."),
    ],
)
async def test_agent_step_without_an_agent_uses_only_its_declared_standalone_tools(
    runner,
    monkeypatch,
    config,
    expected_tools,
    expected_system,
):
    """A service step run with no Workspace has no agent; it gets the tools and
    instructions the node declares for that case, and nothing otherwise."""
    seen: dict = {}

    async def fake_appendix(*args, **kwargs):
        seen["tools"] = kwargs.get("configured_tool_names")
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    async def fake_loop(**kwargs):
        seen["system"] = kwargs.get("system_prompt")
        return SimpleNamespace(
            content="done", usage={}, tool_calls_made=[], rounds=1,
            stop_reason=None, error=None, error_detail=None, control=None,
        )

    monkeypatch.setattr(workflow_runner_module, "runtime_prepare_prompt_appendix_for_turn", fake_appendix)
    monkeypatch.setattr(workflow_runner_module, "runtime_execute_workflow_agent_loop", fake_loop)

    await runner._execute_agent_step(
        {"id": "publish", "type": "agent", "config": {"prompt": "Publish.", "service_key": "distribution.x.chrome", **config}},
        {},
        "entity-1",
        "user-1",
        {},
        None,
    )

    assert seen["tools"] == expected_tools
    assert seen["system"].startswith(expected_system)
