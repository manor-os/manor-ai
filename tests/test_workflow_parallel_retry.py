"""Recovery contracts for arbitrary parallel graphs, without paid producers."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_service import _retry_variables


def graph():
    return [
        {"id": "start", "type": "trigger", "next": ["a", "b", "c"]},
        *[
            {"id": key, "type": "agent", "config": {"output_var": f"result_{key}"}, "next": [f"check_{key}"]}
            for key in ("a", "b", "c")
        ],
        *[
            {"id": f"check_{key}", "type": "transform", "next": ["join"]}
            for key in ("a", "b", "c")
        ],
        {"id": "join", "type": "merge", "next": ["review"]},
        {"id": "review", "type": "agent", "next": ["end"]},
        {"id": "end", "type": "end", "next": []},
    ]


def receipts():
    return {
        "start": {"status": "completed"},
        "a": {"status": "completed", "output": {"document_id": "doc-a"}},
        "b": {"status": "completed", "output": {"document_id": "doc-b"}},
        "c": {"status": "failed", "error": "timed out"},
    }


def recovered_run(steps, results, target="c", **kwargs):
    from packages.core.services.workflow_retry import build_retry_context

    context = build_retry_context(steps, results, target, **kwargs)
    inherited = set(context["inherited_step_ids"])
    workflow = SimpleNamespace(steps=steps, variables={})
    prior = SimpleNamespace(step_results=results, variables={})
    run = WorkflowRun(
        id="retry", entity_id="entity", workflow_id="workflow", status="running",
        retry_from_step_id=target, current_step_id=target,
        step_results={key: deepcopy(results[key]) for key in inherited},
        variables=_retry_variables(workflow, prior, inherited, None, target),
        trigger_data={"_workflow_retry_context": context}, execution_trace=[],
    )
    return workflow, run


def test_parallel_retry_retains_siblings_and_waits_for_every_check():
    workflow, run = recovered_run(graph(), receipts())
    runner = WorkflowRunner()
    assert run.variables["result_a"] == {"document_id": "doc-a"}
    assert run.variables["result_b"] == {"document_id": "doc-b"}
    assert "result_c" not in run.variables
    assert {s["id"] for s in runner._find_runnable_steps(workflow, run)} == {"c", "check_a", "check_b"}
    assert not runner._all_steps_done(workflow, run)
    for key in ("check_a", "check_b"):
        run.step_results[key] = {"status": "completed"}
    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["c"]
    run.step_results["c"] = {"status": "completed"}
    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["check_c"]
    run.step_results["check_c"] = {"status": "completed"}
    assert [s["id"] for s in runner._find_runnable_steps(workflow, run)] == ["join"]


def test_parallel_retry_invalidates_old_downstream_and_retries_multiple_failures():
    results = receipts()
    results["b"] = {"status": "failed", "error": "network"}
    results["join"] = {"status": "completed", "output": "stale"}
    results["review"] = {"status": "completed", "output": "stale"}
    workflow, run = recovered_run(graph(), results)
    assert "join" not in run.step_results
    assert "review" not in run.step_results
    assert {s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, run)} == {"b", "c", "check_a"}


def test_explicit_checkpoint_does_not_reenter_ambiguous_condition():
    steps = [
        {"id": "start", "type": "trigger", "next": ["condition"]},
        {"id": "condition", "type": "condition", "true_next": ["publish"], "false_next": ["stop"]},
        {"id": "publish", "type": "agent", "next": []},
        {"id": "stop", "type": "stop", "next": []},
    ]
    results = {"start": {"status": "completed"}, "condition": {"status": "completed", "output": {"legacy": True}}}
    workflow, run = recovered_run(steps, results, "publish", explicit_checkpoint=True)
    assert [s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, run)] == ["publish"]


def test_retry_does_not_activate_unselected_switch_branch():
    steps = graph()
    steps[0] = {"id": "start", "type": "switch", "config": {"cases": [{"next": ["a", "c"]}], "default_next": ["b"]}}
    steps.insert(0, {"id": "trigger", "type": "trigger", "next": ["start"]})
    results = receipts()
    results["trigger"] = {"status": "completed"}
    results["start"]["next_override"] = ["a", "c"]
    results.pop("b")
    workflow, run = recovered_run(steps, results)
    assert {s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, run)} == {"c", "check_a"}
    assert "b" not in run.step_results


def test_retry_handles_dependency_only_graph():
    steps = graph()
    edges = [(s["id"], target) for s in steps for target in s.pop("next", [])]
    for step in steps:
        step["depends_on"] = [source for source, target in edges if target == step["id"]]
    workflow, run = recovered_run(steps, receipts())
    assert {s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, run)} == {"c", "check_a", "check_b"}


class RunDb:
    async def flush(self):
        pass

    async def commit(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["a", "c"])
async def test_failure_pointer_survives_successful_batch_siblings(monkeypatch, failed):
    runner = WorkflowRunner()
    run = WorkflowRun(id="initial", entity_id="entity", workflow_id="workflow", status="running",
                      variables={}, step_results={}, execution_trace=[], trigger_data={})

    async def execute(step, run, db):
        if step["id"] == failed:
            return {"status": "failed", "error": "timed out"}
        return {"status": "completed", "output": step["id"]}

    monkeypatch.setattr(runner, "_execute_step", execute)
    await runner._run_loop(SimpleNamespace(steps=graph()), run, RunDb())
    assert run.status == "failed"
    assert run.current_step_id == failed


@pytest.mark.asyncio
async def test_failed_then_retried_graph_never_repeats_successful_producers(monkeypatch):
    runner = WorkflowRunner()
    calls = []
    initial = WorkflowRun(id="initial", entity_id="entity", workflow_id="workflow", status="running",
                          variables={}, step_results={}, execution_trace=[], trigger_data={})

    async def execute(step, run, db):
        calls.append(step["id"])
        if step["id"] == "c" and calls.count("c") == 1:
            return {"status": "failed", "error": "timed out"}
        if step["id"] == "review":
            assert all(run.variables[f"result_{key}"] == {"document_id": f"doc-{key}"} for key in ("a", "b", "c"))
        return {"status": "completed", "output": {"document_id": f"doc-{step['id']}"},
                "output_var": (step.get("config") or {}).get("output_var")}

    monkeypatch.setattr(runner, "_execute_step", execute)
    await runner._run_loop(SimpleNamespace(steps=graph()), initial, RunDb())
    workflow, retry = recovered_run(graph(), initial.step_results)
    await runner._run_loop(workflow, retry, RunDb())
    assert retry.status == "completed"
    assert calls.count("a") == calls.count("b") == 1
    assert calls.count("c") == 2
    assert all(calls.index(f"check_{key}") < calls.index("join") for key in ("a", "b", "c"))


def test_retry_after_a_stop_redoes_the_work_that_came_up_short_not_the_stop():
    from packages.core.services.workflow_retry import retry_target

    steps = [
        {"id": "start", "type": "trigger", "next": ["export"]},
        {"id": "export", "type": "tool", "next": ["measure"]},
        {"id": "measure", "type": "code", "next": ["usable"]},
        {"id": "usable", "type": "condition", "true_next": ["done"], "false_next": ["blocked"]},
        {"id": "done", "type": "end"},
        {"id": "blocked", "type": "stop"},
    ]
    results = {
        "start": {"status": "completed"},
        "export": {"status": "completed"},
        "measure": {"status": "completed"},
        "usable": {"status": "completed", "condition_result": False},
        "blocked": {"status": "failed", "error": "The export came out empty."},
    }
    prior = SimpleNamespace(step_results=results, current_step_id="blocked")
    # The stop only reports the outcome; the condition and code before it would decide the same again.
    assert retry_target(steps, prior, {}, None) == "export"
    # A card drawn before this rule still names the stop; it does not override the work before it.
    assert retry_target(steps, prior, {}, "blocked") == "export"
    # An end that fails the run (production_blocked) is a terminal like a stop.
    failing_end = [*steps[:-1], {"id": "blocked", "type": "end", "config": {"fail_when": "true"}}]
    assert retry_target(failing_end, prior, {}, "blocked") == "export"
    # The Retry the person is offered points at the same work, also on a run that was already retried from the stop.
    from packages.core.services.workflow_chat_projection import workflow_retry_from_step_id
    for retry_of, retried_from in (("", ""), ("earlier-run", "blocked")):
        run = SimpleNamespace(status="failed", current_step_id="blocked", step_results=results,
                              execution_snapshot={"steps": steps}, effective_retry_of_run_id=retry_of,
                              effective_retry_from_step_id=retried_from)
        assert workflow_retry_from_step_id(run, {}) == "export"
    # A Flow that names its own recovery point keeps it.
    steps[-1]["config"] = {"retry_from_step_id": "measure"}
    assert retry_target(steps, prior, {}, None) == "measure"
    # With no work before it, the stop is all there is to retry.
    bare = [{"id": "start", "type": "trigger", "next": ["blocked"]}, {"id": "blocked", "type": "stop"}]
    alone = SimpleNamespace(step_results={"start": {"status": "completed"}, "blocked": {"status": "failed"}}, current_step_id="blocked")
    assert retry_target(bare, alone, {}, None) == "blocked"


def test_default_retry_uses_failed_receipt_not_stale_success_pointer():
    from packages.core.services.workflow_retry import retry_target

    prior = SimpleNamespace(step_results=receipts(), current_step_id="b", retry_from_step_id="a")
    assert retry_target(graph(), prior, {}, None) == "c"
    assert retry_target(graph(), prior, {"retry_from_step_id": "review"}, None) == "c"
    assert retry_target(graph(), prior, {}, "a") == "a"
    steps = graph()
    steps[3]["config"]["retry_from_step_id"] = "start"
    assert retry_target(steps, prior, {}, None) == "start"


def test_default_retry_of_a_failed_error_route_resumes_at_the_failed_step_not_its_handler():
    from packages.core.services.workflow_chat_projection import workflow_retry_from_step_id
    from packages.core.services.workflow_retry import retry_target

    # A continue_error sub-flow fails and is handled: the run takes its error route to read, then hold, which fails too.
    steps = [
        {"id": "start", "type": "trigger", "next": ["load"]},
        {"id": "load", "type": "workflow_project", "next": ["make"]},
        {"id": "make", "type": "subworkflow", "next": ["probe"],
         "config": {"on_error": "continue_error", "error_next": ["read"]}},
        {"id": "probe", "type": "tool", "next": ["done"]},
        {"id": "read", "type": "workflow_project", "next": ["hold"]},
        {"id": "hold", "type": "workflow_project", "next": ["held"]},
        {"id": "done", "type": "end"},
        {"id": "held", "type": "stop"},
    ]
    results = {
        "start": {"status": "completed"},
        "load": {"status": "completed"},
        "make": {"status": "failed", "continued": True, "next_override": ["read"], "subrun_id": "child"},
        "read": {"status": "completed"},
        "hold": {"status": "failed", "error": "another run changed this project"},
    }
    prior = SimpleNamespace(step_results=results, current_step_id="hold")
    # The retry redoes the sub-flow that failed; the handler's own conflict is only its consequence.
    assert retry_target(steps, prior, {}, None) == "make"
    run = SimpleNamespace(status="failed", current_step_id="hold", step_results=results,
                          execution_snapshot={"steps": steps}, effective_retry_of_run_id="earlier-run",
                          effective_retry_from_step_id="hold")
    assert workflow_retry_from_step_id(run, {}) == "make"
    # When the normal path also reaches the failing step, that step is not just the handler's consequence.
    shared = [dict(step) for step in steps]
    shared[2] = {**steps[2], "next": ["probe", "hold"]}
    assert retry_target(shared, prior, {}, None) == "hold"


def test_legacy_retry_recovers_missing_sibling_from_lineage_without_reviving_stale_downstream():
    from packages.core.services.workflow_retry import merge_retry_evidence

    first = SimpleNamespace(id="first", step_results=receipts(), variables={}, trigger_data={}, execution_trace=[])
    second = SimpleNamespace(
        id="second", step_results={"start": {"status": "completed"}, "b": {"status": "completed", "output": "new-b"},
                                   "join": {"status": "completed", "output": "incomplete"}, "review": {"status": "failed"}},
        variables={}, trigger_data={}, execution_trace=[{"node_id": "b", "status": "running"}],
    )
    evidence = merge_retry_evidence(graph(), [first, second])
    assert evidence.step_results["a"] == first.step_results["a"]
    assert evidence.step_results["b"]["output"] == "new-b"
    workflow, retry = recovered_run(graph(), evidence.step_results, "review")
    assert "join" not in retry.step_results
    assert {s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, retry)} == {"c", "check_a", "check_b"}


def test_lineage_rewind_does_not_revive_outputs_invalidated_by_newer_attempt():
    from packages.core.services.workflow_retry import merge_retry_evidence

    first = SimpleNamespace(id="first", step_results=receipts(), variables={"result_a": "old"}, trigger_data={}, execution_trace=[])
    second = SimpleNamespace(id="second", step_results={"start": {"status": "completed"}}, variables={},
                             trigger_data={}, execution_trace=[{"node_id": "start", "status": "running"}])
    evidence = merge_retry_evidence(graph(), [first, second])
    assert evidence.step_results["a"]["status"] == "invalidated"
    assert "result_a" not in _retry_variables(SimpleNamespace(steps=graph(), variables={}), evidence, {"start"}, None)


def test_lineage_rewind_clears_transform_values_but_keeps_agent_effect_evidence():
    from packages.core.services.workflow_retry import merge_retry_evidence

    steps = graph()
    steps[4]["config"] = {"set": {"verified_a": "{{result_a}}"}}
    results = receipts()
    results["check_a"] = {"status": "completed", "output": {"verified_a": "old"}}
    first = SimpleNamespace(id="first", step_results=results, variables={"verified_a": "old"}, trigger_data={}, execution_trace=[])
    second = SimpleNamespace(id="second", step_results={"start": {"status": "completed"}}, variables={},
                             trigger_data={}, execution_trace=[{"node_id": "start", "status": "running"}])
    evidence = merge_retry_evidence(steps, [first, second])
    restored = _retry_variables(SimpleNamespace(steps=steps, variables={}), evidence, {"start"}, None)
    assert "verified_a" not in restored
    assert evidence.executed_results["a"]["output"] == {"document_id": "doc-a"}


def test_lineage_recovery_requires_a_known_retry_entry():
    from packages.core.services.workflow_retry import merge_retry_evidence

    first = SimpleNamespace(id="first", step_results=receipts(), variables={}, trigger_data={}, execution_trace=[])
    second = SimpleNamespace(id="second", step_results={}, variables={}, trigger_data={}, execution_trace=[])
    with pytest.raises(ValueError, match="checkpoint"):
        merge_retry_evidence(graph(), [first, second])


@pytest.mark.asyncio
async def test_retry_service_recovers_legacy_parallel_chain(client, db_session):
    from test_workflows import _auth

    headers = await _auth(client, "parallelrecovery")
    response = await client.post("/api/v1/workflows", headers=headers, json={"name": "Three independent producers", "steps": graph()})
    assert response.status_code == 201, response.text
    workflow_id = response.json()["id"]
    response = await client.post(f"/api/v1/workflows/{workflow_id}/run", headers=headers,
                                 json={"execute": False, "trigger_data": {"_workflow_retry_context": {"entry_step_ids": ["review"]}}})
    first = await db_session.get(WorkflowRun, response.json()["id"])
    assert "_workflow_retry_context" not in first.trigger_data
    first.status = "failed"
    first.current_step_id = "b"  # historical batch pointer bug
    first.step_results = receipts()
    first.error = "c timed out"
    await db_session.commit()
    first_id = first.id
    response = await client.post(f"/api/v1/workflows/runs/{first_id}/retry", headers=headers,
                                 json={"from_step_id": "b", "execute": False})
    assert response.status_code == 201, response.text
    second = await db_session.get(WorkflowRun, response.json()["id"])
    second.status = "failed"
    second.current_step_id = "review"
    second.retry_from_step_id = "review"  # overwritten at the second failure
    second.step_results = {"start": {"status": "completed"}, "b": receipts()["b"],
                           "check_b": {"status": "completed"}, "join": {"status": "completed"},
                           "review": {"status": "failed", "error": "missing outputs"}}
    second.trigger_data = {key: value for key, value in second.trigger_data.items() if key != "_workflow_retry_context"}
    second.execution_trace = [{"node_id": "b", "status": "running"}]
    await db_session.commit()
    response = await client.post(f"/api/v1/workflows/runs/{second.id}/retry", headers=headers, json={"execute": False})
    assert response.status_code == 201, response.text
    third = await db_session.get(WorkflowRun, response.json()["id"])
    assert third.variables["result_a"] == {"document_id": "doc-a"}
    assert third.trigger_data["_workflow_retry_context"]["source_run_ids"]["a"] == first_id
    assert third.execution_snapshot == first.execution_snapshot
    assert third.retry_of_run_id == second.id
    assert {s["id"] for s in WorkflowRunner()._find_runnable_steps(SimpleNamespace(steps=graph()), third)} == {"c", "check_a"}
    assert third.step_results["c"]["agent_recovery"]["attempts"][0]["run_id"] == first_id

    # Rewinding farther upstream must still tell every executed Agent about
    # prior partial effects, even though it is no longer a recovery entry.
    third.status = "failed"
    await db_session.commit()
    response = await client.post(f"/api/v1/workflows/runs/{third.id}/retry", headers=headers,
                                 json={"from_step_id": "start", "execute": False})
    assert response.status_code == 201, response.text
    fourth = await db_session.get(WorkflowRun, response.json()["id"])
    assert fourth.step_results["c"]["agent_recovery"]["attempts"][0]["run_id"] == first_id
    assert "result_a" not in fourth.variables


def test_approval_revision_updates_retry_frontier_without_losing_siblings():
    from packages.core.ai.workflow_runner import apply_workflow_approval_revision

    steps = graph()
    steps[8] = {"id": "review", "type": "wait", "config": {"revise_from": "a", "response_variable": "decision"}, "next": ["end"]}
    results = receipts()
    results["c"] = {"status": "completed"}
    results.update({key: {"status": "completed"} for key in ("check_a", "check_b", "check_c", "join", "review")})
    workflow, run = recovered_run(steps, results, "review", explicit_checkpoint=True)
    run.step_results["review"] = {"status": "completed", "decision": "revise"}
    run.variables["decision"] = {"choice": "revise", "note": "change first output"}
    assert apply_workflow_approval_revision(steps, run) == "review"
    assert [s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, run)] == ["a"]


def test_retry_preserves_partial_checkpoints_for_each_failed_stage():
    steps = graph()
    state = {}
    variables = {}
    for step in steps[1:4]:
        step["type"] = "stage"
        operation = f"{step['id']}_accepted"
        step["config"]["operations"] = [{"id": operation, "type": "tool"}]
        state[step["id"]] = {"status": "failed", "operation_results": {operation: {"status": "completed"}}}
        variables[operation] = {"receipt": operation}
    variables["__stage_execution"] = state
    results = receipts()
    results["b"] = {"status": "failed"}
    prior = SimpleNamespace(step_results=results, variables=variables)
    restored = _retry_variables(SimpleNamespace(steps=steps, variables={}), prior, {"start", "a"}, None, "c",
                               retry_step_ids={"b", "c", "check_a"})
    assert set(restored["__stage_execution"]) == {"b", "c"}
    assert restored["b_accepted"] == {"receipt": "b_accepted"}
    assert restored["c_accepted"] == {"receipt": "c_accepted"}


def test_retry_preserves_handled_error_output_without_replaying_it():
    results = receipts()
    results["a"] = {"status": "failed", "continued": True, "next_override": ["check_a"], "output": {"fallback": True}}
    workflow, run = recovered_run(graph(), results)
    assert run.variables["result_a"] == {"fallback": True}
    assert {s["id"] for s in WorkflowRunner()._find_runnable_steps(workflow, run)} == {"check_a", "check_b", "c"}


def test_retry_invalidates_explicit_dependencies_even_when_source_has_other_routes():
    steps = graph()
    steps[5]["depends_on"] = ["c"]  # check_b consumes c as well as b
    results = receipts()
    results["check_b"] = {"status": "completed", "output": "old-c"}
    workflow, run = recovered_run(steps, results)
    assert "check_b" not in run.step_results
    assert "check_b" not in run.variables


def test_retry_dag_checkpoint_does_not_execute_unrelated_unreached_ancestors():
    steps = [{"id": "root", "type": "trigger"},
             {"id": "gate", "type": "transform", "depends_on": ["root"]},
             {"id": "target", "type": "agent", "depends_on": ["gate"]}]
    with pytest.raises(ValueError, match="checkpoint.*gate"):
        recovered_run(steps, {"root": {"status": "completed"}}, "target", explicit_checkpoint=True)


@pytest.mark.asyncio
async def test_retry_refuses_a_second_attempt_while_the_first_retry_runs(client, db_session):
    from sqlalchemy import select

    from test_workflows import _auth, _simple_steps

    headers = await _auth(client, "retryrunningguard")
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers,
        json={"name": "Guarded retry", "steps": _simple_steps(["work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={"execute": False},
    )).json()
    original = await db_session.get(WorkflowRun, failed["id"])
    original.status = "failed"
    original.current_step_id = "work"
    original.error = "work timed out"
    await db_session.commit()
    original_id = original.id

    first = await client.post(
        f"/api/v1/workflows/runs/{original_id}/retry", headers=headers,
        json={"from_step_id": "work", "execute": False},
    )
    assert first.status_code == 201, first.text
    retry_id = first.json()["id"]

    # Both the original run (a double click) and the running retry itself are refused.
    for clicked in (original_id, retry_id):
        second = await client.post(
            f"/api/v1/workflows/runs/{clicked}/retry", headers=headers,
            json={"from_step_id": "work", "execute": False},
        )
        assert second.status_code == 409, second.text
        assert retry_id in second.json()["detail"]

    attempts = (await db_session.scalars(
        select(WorkflowRun.id).where(WorkflowRun.retry_of_run_id == original_id)
    )).all()
    assert attempts == [retry_id]
