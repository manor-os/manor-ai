"""Several Workspace agents work at the same time from one delegation call.

delegate_to_agent takes a tasks list and delegate_service takes params.tasks;
independent tasks for different agents run concurrently (up to 20). Dependent
work goes in a later call, so no dependency graph is needed here.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from packages.core.ai.runtime import workspace_delegation_actions as delegation
from packages.core.ai.runtime.agentic_loop_prompts import runtime_is_turn_context_message
from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from packages.core.models.base import generate_ulid
from tests.test_delegate_to_agent import _entity, _workspace_with_agents


class _Children:
    """Stands in for one delegated child run per call; records overlap."""

    def __init__(self, replies: dict[str, dict] | None = None, delay: float = 0.05):
        self.replies = replies or {}
        self.delay = delay
        self.running = 0
        self.peak = 0
        self.calls: list[dict] = []
        self.groups: list = []

    async def __call__(self, **kwargs):
        from packages.core.database_runtime import database_admission_group

        params = kwargs["params"]
        self.calls.append(params)
        self.groups.append(database_admission_group.get())
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.running -= 1
        reply = self.replies.get(params["agent_subscription_id"])
        return json.dumps(reply or {
            "delegated": True, "status": "completed", "content": f"done: {params['prompt']}",
            "session_id": f"session-{params['agent_subscription_id']}",
            "run_id": f"run-{params['agent_subscription_id']}",
            "usage": {"prompt": 9000}, "tool_calls_made": ["web_search"] * 30,
        })


@pytest.fixture
def children(monkeypatch):
    def install(**kwargs):
        fake = _Children(**kwargs)
        monkeypatch.setattr(delegation, "runtime_workspace_delegate_service_action", fake)
        monkeypatch.setattr(delegation, "PARALLEL_DELEGATION_STAGGER_SECONDS", 0)
        from packages.core.ai.tools import workspace_agent_tools
        monkeypatch.setattr(workspace_agent_tools, "runtime_workspace_delegate_service_action", fake)
        return fake
    return install


async def _call(workspace, **kwargs):
    from packages.core.ai.tools import workspace_agent_tools

    return json.loads(await workspace_agent_tools._delegate_to_agent_handler(
        entity_id=workspace.entity_id, user_id="user-1", workspace_id=workspace.id,
        conversation_id="conv-1", **kwargs,
    ))


def test_the_schema_takes_a_list_of_tasks_naming_the_workspace_agents():
    schema = delegation.delegate_to_agent_schema([
        {"label": "Writer", "summary": ""}, {"label": "Analyst", "summary": "Numbers."},
    ])["function"]
    tasks = schema["parameters"]["properties"]["tasks"]
    assert schema["parameters"]["required"] == ["tasks"]
    assert tasks["maxItems"] == delegation.MAX_PARALLEL_DELEGATIONS == 20
    assert tasks["items"]["properties"]["agent"]["enum"] == ["Writer", "Analyst"]
    assert tasks["items"]["required"] == ["agent", "task"]
    assert "at the same time" in schema["description"]


@pytest.mark.asyncio
async def test_tasks_for_different_agents_run_at_the_same_time_and_return_in_order(client, db_session, children):
    entity_id = await _entity(client, "parallel_run")
    workspace, _agents, subs = await _workspace_with_agents(db_session, entity_id)
    fake = children()

    result = await _call(workspace, tasks=[
        {"agent": "Writer", "task": "draft"},
        {"agent": "Video Operator (douyin)", "task": "plan douyin"},
        {"agent": "Video Operator (xhs)", "task": "plan xhs"},
    ], _agent_id_from_context="")

    assert fake.peak == 3
    # Their database work queued as one caller of the chat process.
    assert len({id(group) for group in fake.groups}) == 1 and None not in fake.groups
    assert result["parallel"] is True and result["completed"] == 3 and result["total"] == 3
    assert [entry["agent"] for entry in result["results"]] == ["Writer", "Video Operator (douyin)", "Video Operator (xhs)"]
    assert [entry["content"] for entry in result["results"]] == ["done: draft", "done: plan douyin", "done: plan xhs"]
    # Follow-ups need each agent's session; per-child bookkeeping stays out.
    assert all(entry["session_id"].startswith("session-") for entry in result["results"])
    assert not any("usage" in entry for entry in result["results"])
    assert all(entry["tool_calls_made"] == ["web_search"] for entry in result["results"])
    assert {call["agent_subscription_id"] for call in fake.calls} == {
        subs["writer"].id, subs["video_a"].id, subs["video_b"].id,
    }


@pytest.mark.asyncio
async def test_a_bad_batch_is_refused_before_any_agent_runs(client, db_session, children):
    entity_id = await _entity(client, "parallel_refuse")
    workspace, _agents, _subs = await _workspace_with_agents(db_session, entity_id)
    fake = children()

    duplicate = await _call(workspace, tasks=[
        {"agent": "Writer", "task": "a"}, {"agent": "Writer", "task": "b"},
    ])
    assert duplicate["error"] == "duplicate_delegation_target"
    unknown = await _call(workspace, tasks=[
        {"agent": "Writer", "task": "a"}, {"agent": "Nobody", "task": "b"},
    ])
    assert unknown["error"] == "unknown_agent" and "Writer" in unknown["available_agents"]
    empty_task = await _call(workspace, tasks=[
        {"agent": "Writer", "task": "a"}, {"agent": "Video Operator (xhs)", "task": " "},
    ])
    assert empty_task["error"] == "task is required"
    too_many = await _call(workspace, tasks=[{"agent": "Writer", "task": str(i)} for i in range(21)])
    assert too_many["error"] == "invalid_tasks"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_one_task_and_the_old_single_form_run_exactly_as_before(client, db_session, children):
    entity_id = await _entity(client, "parallel_single")
    workspace, _agents, subs = await _workspace_with_agents(db_session, entity_id)
    fake = children()

    one = await _call(workspace, tasks=[{"agent": "Writer", "task": "draft"}])
    legacy = await _call(workspace, agent="Writer", task="draft again")
    assert one["content"] == "done: draft" and one["delegated"] is True and "usage" not in one
    assert legacy["content"] == "done: draft again"
    assert [call["agent_subscription_id"] for call in fake.calls] == [subs["writer"].id] * 2


@pytest.mark.asyncio
async def test_a_child_waiting_for_approval_pauses_the_parent_with_the_others_results(client, db_session, children):
    from packages.core.constants.hitl_envelope import parse_hitl_envelope

    entity_id = await _entity(client, "parallel_hitl")
    workspace, _agents, subs = await _workspace_with_agents(db_session, entity_id)
    children(replies={
        subs["video_b"].id: {"__hitl__": True, "error": "approval_required", "approval_token": "hitl-1", "hitl": {"prompt": "Publish?"}},
        subs["video_a"].id: {"stop_parent": True, "status": "waiting_resource", "content": "Which account?"},
    })

    raw = await __import__("packages.core.ai.tools.workspace_agent_tools", fromlist=["x"])._delegate_to_agent_handler(
        entity_id=entity_id, user_id="user-1", workspace_id=workspace.id, conversation_id="conv-1",
        tasks=[
            {"agent": "Writer", "task": "draft"},
            {"agent": "Video Operator (douyin)", "task": "plan"},
            {"agent": "Video Operator (xhs)", "task": "publish"},
        ],
    )
    envelope = parse_hitl_envelope(raw)
    assert envelope is not None and envelope["approval_token"] == "hitl-1"
    statuses = {entry["agent"]: entry["delegation_status"] for entry in envelope["parallel_results"]}
    assert statuses == {
        "Writer": "completed",
        "Video Operator (douyin)": "waiting_for_input",
        "Video Operator (xhs)": "awaiting_approval",
    }


@pytest.mark.asyncio
async def test_a_child_asking_for_input_stops_the_parent(client, db_session, children):
    entity_id = await _entity(client, "parallel_input")
    workspace, _agents, subs = await _workspace_with_agents(db_session, entity_id)
    children(replies={subs["writer"].id: {"stop_parent": True, "content": "Which tone?"}})

    result = await _call(workspace, tasks=[
        {"agent": "Writer", "task": "draft"}, {"agent": "Video Operator (xhs)", "task": "plan"},
    ])
    assert result["stop_parent"] is True and result["content"] == "Which tone?"
    assert [entry["delegation_status"] for entry in result["parallel_results"]] == ["waiting_for_input", "completed"]


@pytest.mark.asyncio
async def test_delegate_service_runs_several_services_at_once(client, db_session, children):
    from packages.core.ai.tools import workspace_agent_tools

    entity_id = await _entity(client, "parallel_service")
    workspace, _agents, subs = await _workspace_with_agents(db_session, entity_id)
    fake = children()

    result = json.loads(await workspace_agent_tools._workspace_delegate_service_handler(
        entity_id=entity_id, user_id="user-1", workspace_id=workspace.id, conversation_id="conv-1",
        tasks=[
            {"service_key": "douyin", "prompt": "plan douyin"},
            {"agent_subscription_id": subs["video_b"].id, "prompt": "plan xhs"},
        ],
    ))
    assert fake.peak == 2 and result["completed"] == 2
    assert [entry["content"] for entry in result["results"]] == ["done: plan douyin", "done: plan xhs"]
    duplicate = json.loads(await workspace_agent_tools._workspace_delegate_service_handler(
        entity_id=entity_id, user_id="user-1", workspace_id=workspace.id, conversation_id="conv-1",
        tasks=[
            {"service_key": "xhs", "prompt": "a"},
            {"agent_subscription_id": subs["video_b"].id, "prompt": "b"},
        ],
    ))
    assert duplicate["error"] == "duplicate_delegation_target" and fake.peak == 2 and len(fake.calls) == 2


def test_parallel_delegation_respects_the_depth_limit(monkeypatch):
    impl = []
    monkeypatch.setattr(delegation, "_runtime_workspace_delegate_service_action", lambda **kw: impl.append(kw))
    token = delegation._delegation_depth.set(delegation.MAX_DELEGATION_DEPTH)
    try:
        result = json.loads(asyncio.run(delegation.runtime_workspace_delegate_parallel(
            entity_id="e", workspace_id="w", user_id="u", conversation_id="c",
            items=[{"agent": "A", "params": {}}, {"agent": "B", "params": {}}],
        )))
    finally:
        delegation._delegation_depth.reset(token)
    assert result["error"] == "delegation_depth_limit" and impl == []


def test_a_tasks_list_is_governed_like_delegate_service():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory

    args = {"tasks": [{"agent": "Writer", "task": "draft"}, {"agent": "Analyst", "task": "numbers"}]}
    call = RuntimeCompositeToolCallFactory.create("delegate_to_agent", args)
    assert call.tool_name == "workspace_agent" and call.arguments["action"] == "delegate_service"
    assert [item["prompt"] for item in call.arguments["params"]["tasks"]] == ["draft", "numbers"]
    assert classify_runtime_tool("delegate_to_agent", args).effect == classify_runtime_tool(
        "workspace_agent", {"action": "delegate_service", "params": {"prompt": "draft"}},
    ).effect


# -- Keeping the 40k tool-result budget useful --------------------------------


def test_each_agent_is_told_its_share_of_the_tool_result_budget():
    from packages.core.services.conversation_transcript import TRANSCRIPT_TOOL_RESULT_MAX_CHARS

    assert delegation.DELEGATION_TOOL_RESULT_CHARS == TRANSCRIPT_TOOL_RESULT_MAX_CHARS
    budgets = {n: delegation.runtime_delegation_answer_budget(n) for n in (1, 2, 5, 20)}
    assert budgets[1] > 35_000 and budgets[20] == 1_250
    for n, budget in budgets.items():
        # n answers plus their measured fields (~700 each) and the envelope fit.
        assert n * (budget + 700) + 1_000 <= TRANSCRIPT_TOOL_RESULT_MAX_CHARS
    note = delegation._answer_budget_note({"answer_max_chars": 1_250})
    assert "limited to 1250 characters" in note
    assert "even if the task asks for more" in note and "file" in note
    assert delegation._answer_budget_note({}) is None
    # The parent writing the tasks knows the limits and the read-before-quoting rule.
    description = delegation.delegate_to_agent_schema()["function"]["description"]
    assert "1,250 each" in description and "read_delegation_result before quoting" in description


@pytest.mark.asyncio
async def test_single_delegations_tell_the_agent_the_whole_budget(client, db_session, children):
    entity_id = await _entity(client, "budget_single")
    workspace, _agents, _subs = await _workspace_with_agents(db_session, entity_id)
    fake = children()
    await _call(workspace, tasks=[{"agent": "Writer", "task": "draft"}])
    assert fake.calls[0]["answer_max_chars"] == delegation.runtime_delegation_answer_budget(1)


def test_the_model_copy_drops_the_repeated_answer_and_bookkeeping():
    content = "x" * 5_000
    payload = {
        "delegated": True, "run_id": "run-1", "session_id": "s-1", "status": "completed",
        "content": content, "result": {"text": content}, "usage": {"prompt": 1},
        "tool_calls_made": ["web_search"], "artifacts": [{"path": "/a.md"}],
    }
    out = delegation.runtime_delegation_result_for_model(payload)
    assert "result" not in out and "usage" not in out and out["tool_calls_made"] == ["web_search"]
    assert out["artifacts"] == [{"path": "/a.md"}] and out["content"] == content
    # A typed result is the contract output, not a repeat: it stays.
    assert delegation.runtime_delegation_result_for_model({**payload, "result": {"rows": 3}})["result"] == {"rows": 3}


def test_a_single_delegation_result_fits_the_budget_and_pauses_pass_through():
    big = json.dumps({"delegated": True, "run_id": "r", "session_id": "s", "content": "y" * 90_000,
                      "result": {"text": "y" * 90_000}})
    bounded = delegation.runtime_delegation_tool_result(big)
    payload = json.loads(bounded)
    assert 39_000 < len(bounded) <= 40_000
    assert payload["content_truncated"]["total_chars"] == 90_000
    assert "Cut here" in payload["content"] and "run_id r, offset" in payload["content"]
    small = json.dumps({"delegated": True, "run_id": "r", "session_id": "s", "content": "fits"})
    assert json.loads(delegation.runtime_delegation_tool_result(small))["content"] == "fits"
    pause = json.dumps({"stop_parent": True, "delegated": True, "content": "z" * 90_000, "_pending_action": {"k": 1}})
    assert delegation.runtime_delegation_tool_result(pause) == pause


@pytest.mark.asyncio
async def test_twenty_answers_always_fit_one_result_and_only_overruns_are_cut(client, db_session, children, monkeypatch):
    entity_id = await _entity(client, "parallel_budget")
    workspace, _agents, _subs = await _workspace_with_agents(db_session, entity_id)
    fake = children()

    async def reply(**kwargs):
        params = kwargs["params"]
        fake.calls.append(params)
        index = len(fake.calls)
        # Like staging: every agent overran its share except one short answer.
        answer = "short answer" if index == 1 else "z" * (1_600 + 40 * index)
        return json.dumps({
            "delegated": True, "status": "completed", "content": answer, "result": {"text": answer},
            "run_id": f"run-{index:02d}-01KXYZABCDEFGHJKMNPQRS", "session_id": "01KXYZABCDEFGHJKMNPQRSTUVW",
            "service": {"agent_subscription_id": "01KXYZABCDEFGHJKMNPQRSTUVW", "service_key": f"probe{index:02d}",
                        "agent_id": "01KXYZABCDEFGHJKMNPQRSTUVW", "agent_name": "Tour Scheduling Agent (sandbox)"},
            "evidence_refs": [], "artifacts": [], "knowledge_refs": [], "stop_reason": "completed",
            "error": None, "error_detail": None, "usage": {"prompt": 9000}, "tool_calls_made": ["a"] * 9,
        })

    monkeypatch.setattr(delegation, "runtime_workspace_delegate_service_action", reply)
    items = [{"agent": f"Agent {i}", "params": {"agent_subscription_id": f"sub-{i}", "prompt": "go"}} for i in range(20)]

    async def resolve(db, *, entity_id, workspace_id, raw_params):
        from types import SimpleNamespace
        return SimpleNamespace(id=raw_params["agent_subscription_id"]), None, [], None

    monkeypatch.setattr(delegation, "_resolve_workspace_service_agent", resolve)
    raw = await delegation.runtime_workspace_delegate_parallel(
        entity_id=entity_id, workspace_id=workspace.id, user_id="u", conversation_id="c", items=items,
    )
    assert len(raw) <= 40_000
    entries = json.loads(raw)["results"]
    budget = delegation.runtime_delegation_answer_budget(20)
    assert all(call["answer_max_chars"] == budget for call in fake.calls)
    by_run = {entry["run_id"][:6]: entry for entry in entries}
    assert by_run["run-01"]["content"] == "short answer" and "content_truncated" not in by_run["run-01"]
    cut = [entry for entry in entries if entry.get("content_truncated")]
    # Only what does not fit is cut, from the longest answers, never below the stated share.
    assert cut and all(entry["content_truncated"]["shown_chars"] >= budget for entry in cut)
    for entry in cut:
        assert f"run_id {entry['run_id']}, offset {entry['content_truncated']['shown_chars']}" in entry["content"]
    assert not any("result" in entry or "usage" in entry for entry in entries)


@pytest.mark.asyncio
async def test_finished_runs_are_readable_by_run_id_only_in_their_own_chat(db_session):
    from datetime import datetime, timedelta, timezone

    from packages.core.models.agent_delegation_session import AgentDelegationSession
    from packages.core.services.agent_delegation_session import DELEGATION_RESULTS_KEPT, finish_delegation_session

    scope = {"entity_id": generate_ulid(), "workspace_id": generate_ulid(),
             "conversation_id": generate_ulid(), "user_id": generate_ulid()}
    session = AgentDelegationSession(
        id=generate_ulid(), agent_subscription_id=generate_ulid(), agent_id=generate_ulid(),
        messages=[], context_usage={}, context_state={}, turn_count=0, status="idle", **scope,
    )
    db_session.add(session)
    await db_session.commit()
    answer = "".join(f"line {i}\n" for i in range(5_000))
    for index in range(DELEGATION_RESULTS_KEPT + 1):
        run_id = f"run-{index:02d}"
        session.status, session.active_run_id = "running", run_id
        session.active_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        await db_session.commit()
        await finish_delegation_session(
            db_session, session_id=session.id, run_id=run_id, messages=[], context_usage={},
            run_result={"status": "completed", "content": answer, "agent_name": "Writer"},
        )

    async def read(run_id, params=None, **overrides):
        return json.loads(await delegation.runtime_read_delegation_result_action(
            **{**scope, **overrides}, params={"run_id": run_id, **(params or {})},
        ))

    pages, offset = [], 0
    while offset is not None:
        page = await read(f"run-{DELEGATION_RESULTS_KEPT:02d}", params={"offset": offset})
        pages.append(page["content"])
        offset = page["next_offset"]
    assert "".join(pages) == answer and page["agent"] == "Writer" and page["total_chars"] == len(answer)
    assert (await read("run-00"))["error"] == "delegation_result_not_found"  # oldest dropped
    # A loop with the default 4,000-character budget (task runs, delegated
    # agents) gets a shorter page whose next_offset continues exactly there.
    from packages.core.ai.agentic_loop import TOOL_RESULT_MAX_CHARS, _compact_tool_result_for_context

    raw = await delegation.runtime_read_delegation_result_action(
        **scope, params={"run_id": f"run-{DELEGATION_RESULTS_KEPT:02d}"},
    )
    assert len(json.loads(raw)["content"]) == delegation.DELEGATION_READ_PAGE_CHARS
    seen = json.loads(_compact_tool_result_for_context("workspace_agent", raw, TOOL_RESULT_MAX_CHARS))
    assert len(json.dumps(seen, ensure_ascii=False)) <= TOOL_RESULT_MAX_CHARS
    assert seen["next_offset"] == len(seen["content"]) and answer.startswith(seen["content"])
    assert (await read("run-05", user_id=generate_ulid()))["error"] == "delegation_result_not_found"
    assert (await read("run-05", conversation_id=generate_ulid()))["error"] == "delegation_result_not_found"


def test_reading_a_delegated_result_is_a_read():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    for name, args in (
        ("workspace_agent", {"action": "read_delegation_result", "params": {"run_id": "r"}}),
        ("manor", {"action": "workspace", "params": {"action": "read_delegation_result", "params": {"run_id": "r"}}}),
    ):
        assert classify_runtime_tool(name, args).effect is RuntimeToolEffect.READ_ONLY, name


# -- Follow-up turns answered in prose ---------------------------------------


@pytest.mark.asyncio
async def test_a_prose_follow_up_without_tools_is_accepted_as_the_answer():
    contract = ChatEvidenceContract(accept_prose=True)
    decision = await contract.validate("第一条是每月 $1,450，来自 Bay Area RV Park。")
    assert decision.action == FinalResponseAction.ACCEPT
    assert decision.content == "第一条是每月 $1,450，来自 Bay Area RV Park。"
    assert decision.outcome["status"] == "answered"


@pytest.mark.asyncio
async def test_chat_accepts_a_plain_answer_on_the_first_turn_too():
    from types import SimpleNamespace

    from packages.core.ai.runtime.chat_evidence import ChatStopGate, chat_evidence_contract
    from packages.core.ai.runtime.surfaces import ChatSurface

    def contract(history):
        return chat_evidence_contract(SimpleNamespace(
            runtime_envelope=SimpleNamespace(surface=ChatSurface.GLOBAL_OWNER_CHAT, metadata={}),
            turn_execution_plan=None, transcript_initial_messages=history,
        ))

    # Chat replies are plain text whether or not the conversation has an
    # earlier reply; format is never a reason to send the model back.
    for history in ([], [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]):
        gate = contract(history)
        assert isinstance(gate, ChatStopGate)
        decision = await gate.validate("Plain answer.")
        assert decision.action == FinalResponseAction.ACCEPT
        assert decision.content == "Plain answer."


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["research", "tools_used", "broken_json", "first_turn"])
async def test_prose_still_needs_the_contract_when_evidence_is_at_stake(case):
    contract = ChatEvidenceContract(requires_execution=case == "research", accept_prose=case != "first_turn")
    content = "Done, the post is published."
    if case == "tools_used":
        contract.observe_tool_result("web_fetch", "call_1", '{"ok":true}')
    if case == "broken_json":
        content = '{"kind":"answer","text":"cut off'
    decision = await contract.validate(content)
    assert decision.action == FinalResponseAction.REVISE_REPLY
    assert decision.diagnostic["error"] == "response_contract_invalid"


def test_the_turn_context_reminds_the_model_of_the_reply_contract(monkeypatch):
    loop = importlib.import_module("packages.core.ai.agentic_loop")
    sent: list[list[dict]] = []

    async def complete(messages, tools, **kwargs):
        sent.append(messages)
        return "Plain answer.", [], {"total": 1}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    result = asyncio.run(loop.agentic_loop(
        system_prompt="stable system", user_message="and the second one?",
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        tool_executor=None, max_rounds=2, turn_context="memories",
        final_response_contract=ChatEvidenceContract(accept_prose=True),
    ))
    context = next(m for m in sent[0] if runtime_is_turn_context_message(m))["content"]
    assert loop.FINAL_RESPONSE_CONTRACT_REMINDER in context
    assert loop.FINAL_RESPONSE_CONTRACT_REMINDER not in sent[0][0]["content"]
    assert result.content == "Plain answer." and len(sent) == 1


# -- The delegated agent fits its own answer ----------------------------------


def test_an_agent_over_its_limit_gets_one_chance_to_fit_its_answer(monkeypatch):
    loop = importlib.import_module("packages.core.ai.agentic_loop")
    sent: list[list[dict]] = []
    replies = iter(["x" * 3_000, "A short answer within the limit."])

    async def complete(messages, tools, **kwargs):
        sent.append(list(messages))
        return next(replies), [], {"total": 1}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    result = asyncio.run(loop.agentic_loop(
        system_prompt="stable system", user_message="Introduce yourself in 400 words.",
        tools=[{"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}}],
        tool_executor=None, max_rounds=4, turn_context=delegation._answer_budget_note({"answer_max_chars": 1_250}),
        final_response_contract=delegation.DelegatedAnswerLengthContract(1_250),
    ))
    assert result.content == "A short answer within the limit." and not result.error
    # The length check has no system-prompt instructions, so no reply-contract reminder.
    assert any("limited to 1250 characters" in str(m.get("content")) for m in sent[0])
    assert not any(loop.FINAL_RESPONSE_CONTRACT_REMINDER in str(m.get("content")) for m in sent[0])
    # One retry, and the check adds nothing to the system prompt (its cache holds).
    assert len(sent) == 2 and sent[0][0]["content"] == sent[1][0]["content"]
    assert sent[0][0]["content"].startswith("stable system")
    feedback = json.loads(sent[1][-1]["content"]) if sent[1][-1]["content"].startswith("{") else sent[1][-1]["content"]
    assert "the limit is 1250" in json.dumps(feedback)


@pytest.mark.asyncio
async def test_a_second_overrun_is_kept_rather_than_lost():
    from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseContext

    contract = delegation.DelegatedAnswerLengthContract(100)
    assert (await contract.validate("ok")).action is FinalResponseAction.ACCEPT
    assert (await contract.validate("y" * 500)).action is FinalResponseAction.CONTINUE_EXECUTION
    second = await contract.validate("y" * 400)
    assert second.action is FinalResponseAction.ACCEPT and second.content == "y" * 400
    final_round = delegation.DelegatedAnswerLengthContract(100)
    last = await final_round.validate_with_context("z" * 500, FinalResponseContext(allow_execution=False))
    assert last.action is FinalResponseAction.ACCEPT
