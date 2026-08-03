"""Unit tests for the new canonical node-type handlers in WorkflowRunner.

Covers the deterministic, network-free nodes added so that imported
(ComfyUI/n8n/Dify) workflows execute through the runner:
trigger / end (passthrough), unsupported (graceful skip), switch
(multi-branch), merge (variable aggregation).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from packages.core.ai import workflow_runner as workflow_runner_module
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun


def _run(variables: dict | None = None) -> WorkflowRun:
    return WorkflowRun(
        id="run1", workflow_id="wf1", entity_id="ent1",
        variables=variables or {}, step_results={},
    )


@pytest.fixture
def runner() -> WorkflowRunner:
    return WorkflowRunner()


class _CheckpointDb:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


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
async def test_trigger_emits_runtime_payload(runner):
    run = _run()
    run.trigger_data = {"chatInput": "Explain Manor in one sentence"}
    result = await runner._execute_step(
        {"id": "chat", "type": "trigger", "name": "Chat"}, run, None,
    )
    assert result["output"] == {"chatInput": "Explain Manor in one sentence"}


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
        "_workspace_chat_entrypoint": {"enabled": True},
    }
    run.definition_snapshot = {"nodes": [{"id": "start"}]}
    run.execution_trace = []
    workflow = SimpleNamespace(steps=[{
        "id": "start",
        "name": "Start",
        "type": "trigger",
        "next": [],
    }])

    class Db:
        async def commit(self):
            return None

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

    await runner._run_loop(workflow, run, Db())

    assert trace_seen_by_projection == [["running"], ["running", "completed"]]
    assert [entry["status"] for entry in run.execution_trace] == [
        "running",
        "completed",
    ]
    assert run.execution_trace[1]["input_summary"]["api_token"] == "[REDACTED]"
    assert run.execution_trace[1]["artifact_refs"] == [{"id": "artifact-1"}]


@pytest.mark.asyncio
async def test_parallel_terminal_trace_uses_actual_completion_order_without_dataflow_reorder(
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

    class Db:
        async def commit(self):
            return None

    async def execute(step, _run, _db):
        if step["id"] == "slow":
            await asyncio.sleep(0.03)
        return {"status": "completed", "output": step["id"]}

    monkeypatch.setattr(runner, "_execute_step", execute)

    await runner._run_loop(workflow, run, Db())

    terminal_ids = [
        entry["node_id"]
        for entry in run.execution_trace
        if entry["status"] == "completed"
    ]
    assert terminal_ids == ["start", "fast", "slow"]
    assert list(run.step_results) == ["start", "slow", "fast"]
    assert run.step_results["fast"]["completed_at"] < run.step_results["slow"]["completed_at"]


@pytest.mark.asyncio
async def test_end_prefers_explicit_input_binding(runner):
    step = {
        "id": "end", "type": "end", "name": "Done",
        "config": {"inputs": [{"key": "input", "value": "{{summary}}"}]},
    }
    result = await runner._execute_step(step, _run({"summary": {"text": "digest"}}), None)
    assert result["output"] == {"text": "digest"}


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
    step = {
        "id": "ks", "type": "unsupported", "name": "KSampler",
        "meta": {"source_tool": "comfyui", "original_type": "KSampler"},
    }
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "completed"
    assert res["skipped"] is True
    assert "KSampler" in res["output"]


@pytest.mark.asyncio
async def test_unsupported_node_can_fail_when_configured(runner):
    run = _run()
    step = {
        "id": "ks", "type": "unsupported", "name": "KSampler",
        "config": {"on_unsupported": "fail"},
        "meta": {"original_type": "KSampler"},
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


@pytest.mark.asyncio
async def test_http_node_success(runner, monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        assert method == "POST"
        return _FakeResp(200, {"echo": url, "method": method})

    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)
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
    run = _run()
    step = {"id": "h", "type": "http", "config": {"url": "https://x/y"}}
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
async def test_llm_node_routes_to_agent_step(runner, monkeypatch):
    seen = {}

    async def fake_agent(step, variables, entity_id, user_id, runtime_context, db):
        seen["called"] = True
        return {"status": "completed", "output": "drafted"}

    monkeypatch.setattr(runner, "_execute_agent_step", fake_agent)
    res = await runner._execute_step({"id": "l", "type": "llm", "config": {}}, _run(), None)
    assert seen.get("called") and res["output"] == "drafted"


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


@pytest.mark.asyncio
async def test_code_node_executes_in_ephemeral_sandbox(runner, monkeypatch):
    captured = {}

    class FakeSandboxClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def create_from_files(self, **kwargs):
            captured["create"] = kwargs
            return SimpleNamespace(sandbox_id="sbx-1", workdir="/skill")

        async def exec(self, **kwargs):
            captured["exec"] = kwargs
            return SimpleNamespace(stdout='{"answer": 42}', stderr="", exit_code=0)

        async def destroy(self, sandbox_id):
            captured.setdefault("destroy", []).append(sandbox_id)

    monkeypatch.setattr(workflow_runner_module, "_workflow_sandbox_url", lambda: "http://sandbox")
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", FakeSandboxClient)
    step = {"id": "c", "type": "code",
            "config": {"code": "print(inputs['value'])", "language": "python", "output_var": "r"}}
    res = await runner._execute_step(step, _run({"value": 42}), None)
    assert res["status"] == "completed"
    assert res["output"] == {"answer": 42}
    assert res["output_var"] == "r"
    assert res["sandboxed"] is True
    assert json.loads(captured["create"]["files"]["inputs.json"]) == {"value": 42}
    assert captured["create"]["config"]["network"] == "none"
    assert captured["exec"]["command"] == "python run.py"
    assert captured["destroy"] == ["sbx-1"]


@pytest.mark.asyncio
async def test_code_node_requires_sandbox_service(runner, monkeypatch):
    monkeypatch.setattr(workflow_runner_module, "_workflow_sandbox_url", lambda: "")
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
async def test_error_trigger_dispatch_and_recursion_guard(runner, monkeypatch):
    """A failed run dispatches error-handler bindings; an error-sourced run does
    not (so a failing handler can't recurse)."""
    import packages.core.services.workflow_service as svc

    calls = []

    async def fake_dispatch(db, entity_id, **kw):
        calls.append({"entity_id": entity_id, **kw})
        return []

    monkeypatch.setattr(svc, "dispatch_trigger", fake_dispatch)

    failed = _run()
    failed.status = "failed"
    failed.error = "boom"
    failed.trigger_source = None
    await runner._dispatch_error_handlers(failed, db=None)
    assert len(calls) == 1
    assert calls[0]["trigger_type"] == "error"
    assert calls[0]["trigger_data"]["error"] == "boom"

    calls.clear()
    handler = _run()
    handler.status = "failed"
    handler.trigger_source = "error"          # an error-handler that itself failed
    await runner._dispatch_error_handlers(handler, db=None)
    assert calls == []                         # no recursion


def test_workflow_tools_exposed():
    """Manor agents can discover the complete Workflow authoring package."""
    from packages.core.ai.tools.workflow_tools import get_tools
    tools = get_tools()
    names = {s["function"]["name"] for s, _ in tools}
    assert names == {
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
    stop = runner._execute_stop_step({"id": "x", "config": {"message": "halt {{why}}"}}, {"why": "now"})
    assert stop["status"] == "failed" and stop["error"] == "halt now"
    j = runner._execute_extractfromfile_step({"id": "e", "config": {"input": '{{body}}'}}, {"body": '{"a": 1}'})
    assert j["output"] == {"a": 1}
    csv = runner._execute_extractfromfile_step({"id": "e", "config": {"input": "{{c}}", "format": "csv"}}, {"c": "a,b\n1,2\n3,4"})
    assert csv["output"] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


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

    monkeypatch.setattr(workflow_runner_module, "_STEP_TIMEOUT_SECS", 0.001)
    monkeypatch.setattr(runner, "_execute_step", delayed_step)

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
                "operation": "patch",
                "project_type": "product_video",
                "schema_version": 1,
                "state_schema": {"type": "object"},
            },
            "project_id",
        ),
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
    """A timer beyond the inline cap pauses and schedules its own resume."""
    scheduled = {}
    monkeypatch.setattr(
        runner,
        "enqueue_resume",
        lambda run_id, delay: scheduled.update(run_id=run_id, delay=delay) or True,
    )
    step = {"id": "w", "type": "wait", "name": "Pause",
            "config": {"wait_type": "timer", "duration_seconds": 100000}}
    run = _run()
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "paused"
    assert res["auto_resume_scheduled"] is True
    assert res["resume_at"]
    assert scheduled == {"run_id": "run1", "delay": 100000.0}


@pytest.mark.asyncio
async def test_wait_approval_pauses(runner):
    """Approval waits always pause the run."""
    step = {"id": "w", "type": "wait", "name": "Approve",
            "config": {"wait_type": "approval", "message": "Need sign-off"}}
    run = _run()
    res = await runner._execute_step(step, run, None)
    assert res["status"] == "paused"
    assert run.status == "paused"


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
async def test_unconfigured_connector_skips_not_fails(runner):
    """A freshly-imported connector with no integration bound should SKIP so the
    rest of the run completes — not hard-fail the whole workflow. A bare tool
    node the user added with no tool is still a real error."""
    imported = {"id": "c", "type": "connector", "config": {"n8n": {"type": "n8n-nodes-base.slack"}}}
    res = await runner._execute_tool_step(imported, {}, "e", "u", {})
    assert res["status"] == "completed" and res.get("skipped") is True
    bare_tool = {"id": "t", "type": "tool", "config": {}}
    res2 = await runner._execute_tool_step(bare_tool, {}, "e", "u", {})
    assert res2["status"] == "failed"


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
    run = _run({})
    async def boom(step, _run, _db):
        return {"status": "failed", "error": "boom"}
    monkeypatch.setattr(runner, "_execute_step", boom)

    imported = {"id": "a", "type": "http", "config": {"n8n": {"type": "n8n-nodes-base.httpRequest"}}}
    res = await runner._execute_step_safe(imported, run, None)
    assert res["status"] == "completed" and res.get("skipped") is True and res.get("error") == "boom"

    # ComfyUI/Dify imports mark provenance via meta.source_tool (no config.n8n)
    comfy = {"id": "d", "type": "image", "config": {}, "meta": {"source_tool": "comfyui"}}
    res_c = await runner._execute_step_safe(comfy, run, None)
    assert res_c["status"] == "completed" and res_c.get("skipped") is True

    user_built = {"id": "b", "type": "http", "config": {}}
    res2 = await runner._execute_step_safe(user_built, run, None)
    assert res2["status"] == "failed"

    imported_stop = {"id": "c", "type": "stop", "config": {"n8n": {"type": "x"}}}
    res3 = await runner._execute_step_safe(imported_stop, run, None)
    assert res3["status"] == "failed"  # stop still halts

    strict = {"id": "s", "type": "http", "config": {
        "n8n": {"type": "n8n-nodes-base.httpRequest"}, "strict_execution": True,
    }}
    res4 = await runner._execute_step_safe(strict, run, None)
    assert res4["status"] == "failed" and res4.get("skipped") is not True


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
