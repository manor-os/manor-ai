"""Plain-text chat replies: host-derived outcome, ledger gates, live streaming.

The model writes only the answer. The process title comes from the turn's
tool ledger, the only reasons to send the model back are ledger gaps, and the
answer streams live: post-tool text is progress until the host accepts the
reply and re-labels it final.
"""
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.chat_evidence import (
    ChatStopGate, chat_evidence_contract, plain_text_history,
)
from packages.core.ai.runtime.chat_outcome import derive_chat_outcome
from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseContext
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.services.assistant_blocks import AssistantBlocksBuilder
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401


def obs(call_id, operation, *, stage="execution", result="returned", **extra):
    return {"call_id": call_id, "operation": operation, "tool": operation, "stage": stage, "result": result, **extra}


# ── derive_chat_outcome ──────────────────────────────────────────────


@pytest.mark.parametrize("observations,stop_reason,status", [
    ([], None, "answered"),
    ([obs("d1", "search_tools", stage="discovery")], None, "answered"),
    ([], "max_rounds", "not_attempted"),
    ([obs("a", "web_search")], None, "completed"),
    ([obs("a", "workspace_search", stage="inventory")], None, "completed"),
    # Failed then succeeded: recovered, like the timeline's "processed after retry".
    ([obs("a", "web_fetch", result="error"), obs("b", "web_fetch")], None, "completed"),
    ([obs("a", "web_fetch", result="error"), obs("b", "write_file")], None, "partial"),
    ([obs("a", "web_fetch", result="error")], None, "blocked"),
    ([obs("a", "web_fetch"), obs("b", "web_fetch", result="error")], None, "blocked"),
    ([obs("a", "acquire_capabilities.apply", stage="authorization", result="error", authorization_blocked=True)], None, "blocked"),
    ([obs("a", "web_search")], "max_rounds", "partial"),
])
def test_status_comes_from_the_last_attempt_of_each_operation(observations, stop_reason, status):
    assert derive_chat_outcome(observations, stop_reason=stop_reason)["status"] == status


def test_outcome_carries_claims_refs_and_failure_limitations():
    outcome = derive_chat_outcome([
        obs("a", "web_fetch", result="error", message="timeout"), obs("b", "write_file"),
    ])
    assert outcome["evidence_refs"] == ["a", "b"]
    assert outcome["claims"] == [
        {"operation": "web_fetch", "status": "blocked", "evidence_refs": ["a"]},
        {"operation": "write_file", "status": "completed", "evidence_refs": ["b"]},
    ]
    assert outcome["limitations"] == ["web_fetch: timeout"]


def test_open_delivery_and_gate_notes_cap_but_never_raise_status():
    handoff = {"kind": "sandbox_ready", "skill": "pptx", "state": "pending", "failed_call_ids": []}
    assert derive_chat_outcome([obs("a", "invoke_skill", stage="delegation")], pending_handoffs=[handoff])["status"] == "partial"
    assert derive_chat_outcome([obs("a", "web_search")], gate_limitations=["not retrieved"])["status"] == "partial"
    assert derive_chat_outcome([], gate_limitations=["not attempted"])["status"] == "not_attempted"
    blocked = derive_chat_outcome([obs("a", "web_fetch", result="error")], gate_limitations=["x"])
    assert blocked["status"] == "blocked"


def test_unconfirmed_effect_behind_a_recovered_operation_is_reported():
    outcome = derive_chat_outcome(
        [obs("a", "send_email", result="error"), obs("b", "send_email")],
        unconfirmed_calls=[{"call_id": "a", "operation": "send_email"}],
    )
    assert outcome["status"] == "completed"
    assert outcome["limitations"] == ["send_email: an earlier attempt's effect is unconfirmed"]


# ── ChatStopGate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_text_is_the_answer_and_the_outcome_is_derived():
    gate = ChatStopGate()
    decision = await gate.validate("最出名的华人侦探是**狄仁杰**。")
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.content == "最出名的华人侦探是**狄仁杰**。"
    assert decision.outcome["status"] == "answered"
    assert gate.response_format is None and gate.tool_response_format is None
    assert "JSON" not in gate.instructions.replace("Never wrap the\nanswer in JSON", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [
    '{"kind":"answer","text":"Done.","outcome":{"status":"completed","evidence_refs":[],"limitations":[]}}',
    '{"response":{"kind":"answer","text":"Done.","outcome":{"status":"answered","evidence_refs":[],"limitations":[],"claims":[]}}}',
])
async def test_a_reply_imitating_the_old_envelope_is_not_sent_back(legacy):
    decision = await ChatStopGate().validate(legacy)
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.content == "Done."


@pytest.mark.asyncio
async def test_model_claims_cannot_raise_the_derived_status():
    gate = ChatStopGate()
    gate.observe_tool_result("web_fetch", "call-1", '{"error":"timeout","message":"timed out"}')
    decision = await gate.validate("I fetched the page and everything is done.")
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["status"] == "blocked"


def _open_sandbox(gate):
    gate.observe_tool_result("invoke_skill", "skill-1", json.dumps({
        "status": "sandbox_ready", "skill": "pptx", "sandbox_id": "sbx-1",
    }))


@pytest.mark.asyncio
async def test_unfinished_delivery_sends_the_model_back_once_then_accepts_with_limitation():
    gate = ChatStopGate()
    _open_sandbox(gate)
    first = await gate.validate_with_context("Here is your deck!", FinalResponseContext(allow_execution=True))
    assert first.action is FinalResponseAction.CONTINUE_EXECUTION
    assert first.diagnostic["error"] == "sandbox_handoff_undelivered"
    second = await gate.validate_with_context("Here is your deck!", FinalResponseContext(allow_execution=True))
    assert second.action is FinalResponseAction.ACCEPT
    assert second.outcome["status"] == "partial"
    assert "pptx: the requested deliverable was not saved" in second.outcome["limitations"]


@pytest.mark.asyncio
async def test_gate_attempts_survive_a_checkpoint_resume():
    gate = ChatStopGate()
    _open_sandbox(gate)
    assert (await gate.validate_with_context("x", FinalResponseContext())).action is FinalResponseAction.CONTINUE_EXECUTION
    resumed = ChatStopGate()
    resumed.restore_checkpoint_state(gate.checkpoint_state())
    assert (await resumed.validate_with_context("x", FinalResponseContext())).action is FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_all_gates_together_send_the_model_back_at_most_twice():
    gate = ChatStopGate(requires_execution=True, discovery_gate=True)
    gate.observe_tool_result("search_tools", "d1", '{"ok": true}')
    gate._open_handoff({"kind": "sandbox_ready", "call_id": "h", "skill": "pptx", "sandbox_id": "s"})
    codes = []
    for _ in range(4):
        decision = await gate.validate_with_context("answer", FinalResponseContext())
        if decision.action is FinalResponseAction.ACCEPT:
            break
        codes.append(decision.diagnostic["error"])
    # A capped gate never hides the next one; the budget, not the order, stops them.
    assert codes == ["sandbox_handoff_undelivered", "current_research_evidence_required"]
    assert decision.action is FinalResponseAction.ACCEPT
    assert set(decision.outcome["limitations"]) >= {
        "pptx: the requested deliverable was not saved",
        "current external information was not retrieved",
        "suitable capabilities were found but not attempted",
    }


@pytest.mark.asyncio
async def test_a_gap_closed_after_a_nudge_leaves_no_limitation():
    gate = ChatStopGate(requires_execution=True)
    gate._open_handoff({"kind": "skill_continuation", "call_id": "h", "skill": "pptx", "stage": "canvas"})
    first = await gate.validate_with_context("x", FinalResponseContext())
    assert first.diagnostic["error"] == "skill_stage_unfinished"
    # The stage gate is now past its budget (a limitation) while research nudges.
    second = await gate.validate_with_context("x", FinalResponseContext())
    assert second.diagnostic["error"] == "current_research_evidence_required"
    gate.observe_tool_result("invoke_skill", "again", json.dumps({"skill": "pptx", "status": "ok"}))
    gate.observe_tool_result("web_search", "w1", '{"results": [1]}')
    decision = await gate.validate_with_context("Done.", FinalResponseContext())
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["limitations"] == []
    assert decision.outcome["status"] == "completed"


@pytest.mark.asyncio
async def test_gates_never_fire_once_execution_is_closed():
    gate = ChatStopGate(requires_execution=True)
    _open_sandbox(gate)
    decision = await gate.validate_with_context(
        "summary", FinalResponseContext(allow_execution=False, stop_reason="max_rounds", budget_exhausted=True),
    )
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["status"] == "partial"
    assert decision.stop_reason == "max_rounds"


@pytest.mark.asyncio
async def test_an_answer_on_the_last_allowed_round_is_not_an_exhausted_budget():
    gate = ChatStopGate()
    gate.observe_tool_result("web_search", "c1", '{"results": [1]}')
    decision = await gate.validate_with_context(
        "Found it.", FinalResponseContext(allow_execution=False, stop_reason="max_rounds"),
    )
    assert decision.outcome["status"] == "completed"
    assert decision.stop_reason is None


@pytest.mark.asyncio
async def test_an_empty_reply_is_retried_once_then_accepted():
    gate = ChatStopGate()
    first = await gate.validate("   ")
    assert first.action is FinalResponseAction.REVISE_REPLY
    assert first.diagnostic["error"] == "response_empty"
    second = await gate.validate("")
    assert second.action is FinalResponseAction.ACCEPT
    assert second.content == ""


# ── File links: a reply links only files the user can open ──────────

def _deck_resolver():
    from packages.core.services.file_refs import ParsedFileRef, file_ref

    deck = file_ref(entity_id="ent1", fs_path="Decks/AI deck.pptx", document_id="01DECK")

    async def resolve(parsed):
        if parsed in {ParsedFileRef("", "", "01DECK"), ParsedFileRef("ent1", "Decks/AI deck.pptx")}:
            return deck
        return None

    return resolve


@pytest.mark.asyncio
async def test_a_sandbox_link_is_sent_back_once_then_reduced_to_its_words():
    gate = ChatStopGate(file_resolver=_deck_resolver(), entity_id="ent1")
    reply = "PPT 已生成：[AI deck.pptx](sandbox:/skill/projects/p/AI deck.pptx)"
    first = await gate.validate(reply)
    assert first.action is FinalResponseAction.REVISE_REPLY
    assert first.diagnostic["error"] == "file_link_unopenable"
    assert "sandbox:/skill/projects/p/AI deck.pptx" in first.diagnostic["message"]
    second = await gate.validate(reply)
    assert second.action is FinalResponseAction.ACCEPT
    assert second.content == "PPT 已生成：AI deck.pptx"


@pytest.mark.asyncio
async def test_feedback_lists_files_saved_this_turn():
    gate = ChatStopGate(file_resolver=_deck_resolver(), entity_id="ent1")
    gate.receipts.append({"handle": "r1", "document_id": "01DECK", "fs_path": "Decks/AI deck.pptx",
                          "name": "AI deck.pptx"})
    decision = await gate.validate("Here: [deck](/skill/projects/p/deck.pptx)")
    assert "[AI deck.pptx](/viewer/01DECK)" in decision.diagnostic["message"]


@pytest.mark.asyncio
async def test_an_openable_link_is_accepted_in_its_canonical_form():
    gate = ChatStopGate(file_resolver=_deck_resolver(), entity_id="ent1")
    decision = await gate.validate("Saved [AI deck](/api/v1/fs/ent1/Decks/AI%20deck.pptx).")
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.content == "Saved [AI deck](/viewer/01DECK)."
    assert "file_link_unopenable" not in gate.gate_attempts


def test_history_replay_drops_sandbox_links_from_earlier_replies():
    history = plain_text_history([
        {"role": "user", "content": "make a deck from sandbox:/skill/notes.md"},
        {"role": "assistant", "content": "Done: [deck.pptx](sandbox:/skill/projects/p/deck.pptx), [kept](/viewer/01A)"},
    ])
    assert history[0]["content"] == "make a deck from sandbox:/skill/notes.md"
    assert history[1]["content"] == "Done: deck.pptx, [kept](/viewer/01A)"


@pytest.mark.asyncio
async def test_discovery_gate_is_off_by_default():
    gate = ChatStopGate()
    gate.observe_tool_result("search_tools", "d1", '{"ok": true}')
    decision = await gate.validate("No suitable tool is available.")
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["status"] == "answered"


# ── Workspace: verified Task status is appended by the host ──────────


def _workspace_gate(*, references, facts, fail=False):
    async def current_turn(scope):
        if fail:
            raise RuntimeError("db down")
        return references

    async def reader(scope, refs):
        return facts

    return ChatStopGate(
        workspace_scope=SimpleNamespace(), locale="zh-CN",
        task_reader=reader, current_turn_task_reader=current_turn,
    )


@pytest.mark.asyncio
async def test_tasks_created_this_turn_are_appended_with_verified_status():
    fact = {"kind": "task", "id": "T1", "title": "周报", "state": "running", "url": "/tasks/T1",
            "origin": "current_turn", "revision": "r"}
    gate = _workspace_gate(references=[{"kind": "task", "id": "T1", "scope": "current_turn"}], facts=[fact])
    decision = await gate.validate("已为你创建周报任务。")
    assert decision.content == "已为你创建周报任务。\n\n已核实的任务状态：\n\n- [周报](/tasks/T1) — 本轮创建 · 正在执行"
    assert decision.evidence == (fact,)


@pytest.mark.asyncio
async def test_no_created_task_or_a_failed_lookup_leaves_the_answer_as_written():
    assert (await _workspace_gate(references=[], facts=[]).validate("Hi")).content == "Hi"
    assert (await _workspace_gate(references=[{"kind": "task", "id": "T1", "scope": "current_turn"}],
                                  facts=[], fail=True).validate("Hi")).content == "Hi"


def test_workspace_rendering_is_shared_with_the_typed_contract():
    from packages.core.ai.runtime.workspace_response import render_verified_task_status

    fact = {"title": "A", "state": "delivered", "url": "/tasks/A"}
    assert render_verified_task_status(
        [{"id": "A", "scope": "existing_record"}], [fact], locale="en",
    ) == "Verified task status:\n\n- [A](/tasks/A) — Existing task · Accepted and delivered"


# ── Selection and history ────────────────────────────────────────────


def _ctx(*, surface=ChatSurface.GLOBAL_OWNER_CHAT, disable_tools=False):
    return SimpleNamespace(
        runtime_envelope=SimpleNamespace(surface=surface, metadata={}),
        turn_execution_plan=SimpleNamespace(disable_tools=disable_tools, mode=None),
        transcript_initial_messages=[],
    )


@pytest.mark.parametrize("surface", [ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM, ChatSurface.WORKSPACE_CHAT])
def test_every_tool_using_chat_turn_gets_the_plain_text_gate(surface):
    assert isinstance(chat_evidence_contract(_ctx(surface=surface)), ChatStopGate)


def test_turns_without_tools_or_outside_chat_get_no_reply_contract():
    assert chat_evidence_contract(_ctx(disable_tools=True)) is None
    assert chat_evidence_contract(_ctx(surface=ChatSurface.PUBLIC_CUSTOMER_CHAT)) is None


def test_tool_using_chat_turns_share_one_round_budget():
    from packages.core.ai.runtime.turn_policy import CHAT_MAX_ROUNDS

    assert CHAT_MAX_ROUNDS == 1000


def test_history_reduces_old_envelopes_to_their_visible_text():
    messages = [
        {"role": "user", "content": '{"kind":"answer","text":"user text stays"}'},
        {"role": "assistant", "content": '{"response":{"kind":"progress","text":"Checking."}}', "tool_calls": [{"id": "c"}]},
        {"role": "assistant", "content": "Plain reply."},
        {"role": "assistant", "content": '{"kind":"execution_summary","references":[]}'},
    ]
    result = plain_text_history(messages)
    assert [m["content"] for m in result] == [
        '{"kind":"answer","text":"user text stays"}', "Checking.", "Plain reply.",
        '{"kind":"execution_summary","references":[]}',
    ]
    assert result[1]["tool_calls"] == [{"id": "c"}]


# ── Blocks: promotion and withdrawal ─────────────────────────────────


def _builder_with_step():
    builder = AssistantBlocksBuilder(started_at_ms=1)
    builder.append_text("Let me check.", phase="opening")
    builder.start_tool("web_search", {"query": "x"}, now_ms=2)
    builder.end_tool("web_search", arguments={"query": "x"}, result="{}", status="success", duration_ms=1, now_ms=3)
    builder.append_text("The answer ", phase="progress")
    builder.append_text("is 42.", phase="progress")
    return builder


def _texts(builder):
    return [(b["phase"], b["text"]) for b in builder.blocks() if b["type"] == "text"]


def test_trailing_progress_is_promoted_to_final_once():
    builder = _builder_with_step()
    builder.promote_trailing_progress_to_final()
    builder.promote_trailing_progress_to_final()
    assert _texts(builder) == [("opening", "Let me check."), ("final", "The answer is 42.")]


def test_progress_before_a_later_step_is_not_promoted():
    builder = _builder_with_step()
    builder.start_tool("write_file", {"path": "a"}, now_ms=4)
    builder.promote_trailing_progress_to_final()
    assert ("progress", "The answer is 42.") in _texts(builder)
    assert not any(phase == "final" for phase, _ in _texts(builder))


def test_a_withdrawn_candidate_leaves_earlier_text():
    builder = _builder_with_step()
    builder.discard_candidate_text()
    assert _texts(builder) == [("opening", "Let me check.")]


# ── Loop: live text, withdrawal, host boundary ───────────────────────


@pytest.mark.asyncio
async def test_loop_streams_candidates_withdraws_a_gated_one_and_marks_the_boundary(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    events, rounds = [], 0

    async def handler(event_type, payload):
        events.append((event_type, payload))

    async def execute(name, args, **context):
        if name == "invoke_skill":
            return json.dumps({"status": "sandbox_ready", "skill": "pptx", "sandbox_id": "s1"})
        return json.dumps({"saved": True, "document_id": "doc-1", "fs_path": "/k/deck.pptx",
                           "artifact_role": "final", "saved_to_knowledge": True})

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        stream = kwargs["stream_handler"]
        if rounds == 1:
            await stream("text_delta", {"content": "Opening the skill."})
            return "Opening the skill.", [{"id": "c1", "name": "invoke_skill", "arguments": {"skill": "pptx"}}], {"total": 1}
        if rounds == 2:
            await stream("text_delta", {"content": "Your deck is ready!"})
            await stream("final_response_start", {})
            return "Your deck is ready!", [], {"total": 1}
        if rounds == 3:
            assert "sandbox_handoff_undelivered" in json.dumps(messages)
            return "", [{"id": "c2", "name": "sandbox_save_result", "arguments": {}}], {"total": 1}
        await stream("text_delta", {"content": "Saved deck.pptx."})
        return "Saved deck.pptx.", [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("invoke_skill", "sandbox_save_result"), None)
    kwargs["dynamic_tool_handlers"] = {}
    kwargs["capability_refresh_tool_names"] = set()
    kwargs["final_response_contract"] = ChatStopGate()
    kwargs["stream_handler"] = handler
    result = await runtime_execute_agentic_loop(**kwargs)

    assert result.content == "Saved deck.pptx."
    assert result.outcome["status"] == "completed"
    kinds = [event for event, _ in events if event in {"text_delta", "text_reset", "final_response_start"}]
    # Candidates stream live; the gated one is withdrawn; the provider's own
    # boundary marker never passes; the host marks it once, after acceptance.
    assert kinds == ["text_delta", "text_delta", "text_reset", "text_delta", "final_response_start"]
    assert events[[e for e, _ in events].index("text_reset")][1] == {"discard_candidate": True}


# ── chat_service: UI states end to end ───────────────────────────────


async def _stream_turn(fake_loop, *, plain=True):
    from unittest.mock import AsyncMock, patch

    from packages.core.services.chat_service import stream_chat_response

    async def fake_context(*_args, **_kwargs):
        # A tool-using owner turn gets the plain-text gate; without an
        # envelope the turn runs the contract-less lane.
        envelope = SimpleNamespace(surface=ChatSurface.GLOBAL_OWNER_CHAT, metadata={}) if plain else None
        return "system", [], [], SimpleNamespace(
            workspace_id=None, task_id=None, runtime_envelope=envelope,
            tool_profile=None, allowed_tool_names=None, user=None, entity=None,
        )

    events = []
    with (
        patch("packages.core.services.chat_service.resolve_runtime_chat_context", new=fake_context),
        patch("packages.core.services.chat_service.runtime_execute_chat_agent_loop", new=fake_loop),
        patch("packages.core.services.chat_service.save_assistant_stream_interrupted_message", new=AsyncMock(return_value=None)),
        patch("packages.core.services.chat_service.record_chat_llm_usage", new=AsyncMock()),
    ):
        async for raw in stream_chat_response(
            "q", "conv-plain", entity_id="ent", user_id="user",
            persist_messages=False, runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
        ):
            if "\ndata: " in raw:
                events.append((raw.split("\n", 1)[0].removeprefix("event: "), json.loads(raw.split("\ndata: ", 1)[1])))
    return events


def _final_blocks(events):
    terminal = next(payload for event, payload in events if event == "stream_end")
    blocks = terminal["assistant_blocks"]
    process = next(block for block in blocks if block["type"] == "process")
    return process, [(b["phase"], b["text"]) for b in blocks if b["type"] == "text"]


def _result(content, outcome, stop_reason="completed", tools=("web_search",)):
    from packages.core.ai.agentic_loop import AgenticResult

    return AgenticResult(content=content, messages=[], usage={}, rounds=2,
                         tool_calls_made=list(tools), stop_reason=stop_reason, outcome=outcome)


@pytest.mark.asyncio
async def test_post_tool_answer_streams_live_as_progress_then_becomes_the_final_answer():
    async def fake_loop(**kwargs):
        stream = kwargs["stream_handler"]
        await stream("text_delta", {"content": "Searching now. "})
        kwargs["on_tool_start"]("web_search", {"query": "detective"})
        kwargs["on_tool_end"]("web_search", '{"results":[1]}', 10, {"query": "detective"})
        await stream("text_delta", {"content": "Judge Dee is the most famous Chinese detective in fiction."})
        live = [payload for event, payload in captured if event == "text_delta"]
        assert live, "post-tool text must reach the client before the loop ends"
        await stream("final_response_start", {"round": 2})
        return _result("Judge Dee is the most famous Chinese detective in fiction.",
                       derive_chat_outcome([obs("c1", "web_search")]))

    captured = []

    async def tracking_loop(**kwargs):
        original = kwargs["stream_handler"]

        async def stream(event_type, payload):
            await original(event_type, payload)
            captured.append((event_type, payload))
        return await fake_loop(**{**kwargs, "stream_handler": stream})

    events = await _stream_turn(tracking_loop)
    deltas = "".join(p.get("content", "") for e, p in events if e == "text_delta")
    assert "Judge Dee" in deltas
    # The answer is already on screen: no summary_start (it would reset the
    # client's typing queue); the terminal blocks carry the final phase.
    assert not [p for e, p in events if e == "summary_start"]
    process, texts = _final_blocks(events)
    assert process["lifecycle_status"] == "completed"
    assert process["outcome_status"] == "completed"
    assert texts[-1] == ("final", "Judge Dee is the most famous Chinese detective in fiction.")
    assert not any(phase == "progress" and "Judge Dee" in text for phase, text in texts)


@pytest.mark.asyncio
async def test_a_withdrawn_candidate_never_reaches_the_final_blocks():
    async def fake_loop(**kwargs):
        stream = kwargs["stream_handler"]
        kwargs["on_tool_start"]("invoke_skill", {"skill": "pptx"})
        kwargs["on_tool_end"]("invoke_skill", '{"status":"sandbox_ready"}', 10, {"skill": "pptx"})
        candidate = "Your deck is ready! " + "Every quarter has its own slide with charts and speaker notes. " * 4
        await stream("text_delta", {"content": candidate})
        await stream("text_delta", {"content": "One more closing line for the review meeting."})
        await stream("text_reset", {"discard_candidate": True})
        await stream("text_delta", {"content": "The deck could not be saved: render failed."})
        await stream("final_response_start", {"round": 3})
        return _result("The deck could not be saved: render failed.",
                       {"status": "partial", "evidence_refs": [], "limitations": ["pptx"]}, tools=("invoke_skill",))

    events = await _stream_turn(fake_loop)
    reset_index = next(i for i, (e, p) in enumerate(events) if e == "text_reset" and "assistant_blocks" in p)
    # The candidate really was live before it was withdrawn.
    assert any("ready" in p.get("content", "") for e, p in events[:reset_index] if e == "text_delta")
    reset = events[reset_index][1]
    assert not any("ready" in b.get("text", "") for b in reset["assistant_blocks"] if b["type"] == "text")
    process, texts = _final_blocks(events)
    assert process["outcome_status"] == "partial"
    assert all("ready" not in text for _, text in texts)
    assert texts[-1] == ("final", "The deck could not be saved: render failed.")


@pytest.mark.asyncio
async def test_a_reply_accepted_at_the_round_limit_completes_as_partial():
    async def fake_loop(**kwargs):
        kwargs["on_tool_start"]("web_search", {"query": "x"})
        kwargs["on_tool_end"]("web_search", '{"results":[]}', 10, {"query": "x"})
        return _result("Here is what I found so far.", derive_chat_outcome([obs("c", "web_search")], stop_reason="max_rounds"),
                       stop_reason="max_rounds")

    process, texts = _final_blocks(await _stream_turn(fake_loop))
    assert process["lifecycle_status"] == "completed"
    assert process["outcome_status"] == "partial"
    assert texts[-1] == ("final", "Here is what I found so far.")


@pytest.mark.asyncio
async def test_no_tool_answer_streams_as_opening_and_ends_answered():
    async def fake_loop(**kwargs):
        await kwargs["stream_handler"]("text_delta", {"content": "Judge Dee."})
        return _result("Judge Dee.", derive_chat_outcome([]), tools=())

    events = await _stream_turn(fake_loop)
    assert "".join(p.get("content", "") for e, p in events if e == "text_delta") == "Judge Dee."
    process, texts = _final_blocks(events)
    assert process["outcome_status"] == "answered"
    assert texts == [("final", "Judge Dee.")]


@pytest.mark.asyncio
async def test_contract_less_lane_keeps_post_tool_text_out_of_progress():
    async def fake_loop(**kwargs):
        stream = kwargs["stream_handler"]
        kwargs["on_tool_start"]("web_search", {"query": "x"})
        kwargs["on_tool_end"]("web_search", '{"results":[]}', 10, {"query": "x"})
        await stream("text_delta", {"content": "Answer after tool."})
        return _result("Answer after tool.", {"status": "completed", "evidence_refs": [], "limitations": []})

    events = await _stream_turn(fake_loop, plain=False)
    tool_end = max(i for i, (e, _) in enumerate(events) if e == "tool_end")
    # Without a reply contract (public chat) post-tool text waits for the turn end.
    assert not [p for e, p in events[tool_end:] if e == "text_delta" and "summary_start" not in [x for x, _ in events[tool_end:]]]
    process, texts = _final_blocks(events)
    assert texts[-1] == ("final", "Answer after tool.")


# ── History keeps the failure code the client keys retry on ──────────


@pytest.mark.parametrize("code,kept", [
    ("final_response_validation_failed", True), ("byok_provider_error", True),
    ("llm_configuration_missing", True), ("Traceback: secret detail", False),
])
def test_history_returns_known_failure_codes_in_meta(code, kept):
    from apps.api.routers import chat

    message = SimpleNamespace(
        id="m1", conversation_id="c1", role="assistant", content="Sorry.", tool_calls=None,
        meta={"error": code, "stop_reason": "error"},
        token_usage=None, attachments=None, message_kind=None, refs=None,
        pending_action=None, resolved_at=None, resolution=None, created_at=None,
    )
    meta = chat._to_chat_message_response(message).model_dump()["meta"] or {}
    assert (meta.get("error") == code) is kept
    assert "Traceback" not in json.dumps(meta)



# ── Asking the user a question is a read, not an approval ────────────


def test_request_input_is_evaluated_as_a_read_and_needs_no_approval():
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool, runtime_policy_action_for_classification,
    )
    from packages.core.governance.approval_facts import action_facts
    from packages.core.governance.approval_policy import _baseline

    action, impact = runtime_policy_action_for_classification(classify_runtime_tool("request_input", {"fields": []}))
    assert impact == "read"
    facts = action_facts(
        action_key=action.action_key, capability_id=action.capability_id,
        resource_kind=action.resource_kind, operation=action.operation,
        risk_level=action.risk_level, impact=impact,
    )
    for mode in ("confirm_changes", "automatic", "balanced"):
        assert _baseline(mode, facts) == "allow"


def test_a_classified_write_action_keeps_its_impact_undecided():
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool, runtime_policy_action_for_classification,
    )

    _action, impact = runtime_policy_action_for_classification(classify_runtime_tool("generate_image", {"prompt": "x"}))
    assert impact is None


# ── Live view of a tool call the model is still writing ──────────────


@pytest.mark.parametrize("text,expected", [
    ('{"filename": "AI.pptx", "content": "# 第1页\\n## 第3页：渠', {"filename": "AI.pptx", "content": "# 第1页\n## 第3页：渠"}),
    ('{"filename": "AI', {"filename": "AI"}),
    ('{"a": 1, "b"', {"a": 1}),
    ('{"a": [1, {"c": "x', {"a": [1, {"c": "x"}]}),
    ('{"q": "abc\\', {"q": "abc"}),
    ("", {}), ("{", {}), ("not json", {}),
])
def test_partial_tool_arguments_parse_as_far_as_they_go(text, expected):
    from packages.core.ai.runtime.streams import runtime_partial_json_object

    assert runtime_partial_json_object(text) == expected


def test_draft_preview_is_the_line_being_written_of_the_longest_field():
    from packages.core.ai.runtime.streams import runtime_tool_draft_preview

    content = "# 第1页：为什么 AI 产品需要增长黑客\n- 要点一\n## 第3页：增长黑客的 5 个渠道"
    assert runtime_tool_draft_preview({"filename": "AI.pptx", "content": content}) == "第3页：增长黑客的 5 个渠道"
    assert runtime_tool_draft_preview({"query": "short"}) == ""
    # A line just begun keeps the previous whole line on screen.
    assert runtime_tool_draft_preview({"content": content + "\n- 让"}) == "第3页：增长黑客的 5 个渠道"


def test_a_drafted_call_becomes_the_running_step_and_unrun_drafts_vanish():
    builder = AssistantBlocksBuilder(started_at_ms=1)
    builder.draft_tool("call-1", "generate_file", {"filename": "AI"}, now_ms=2)
    builder.draft_tool("call-1", "generate_file", {"filename": "AI.pptx"}, preview="第3页：渠道", now_ms=3)
    steps = builder.blocks()[0]["steps"]
    assert len(steps) == 1 and steps[0]["drafting"] is True and steps[0]["draft_preview"] == "第3页：渠道"
    builder.start_tool("generate_file", {"filename": "AI.pptx", "content": "..."}, now_ms=4)
    steps = builder.blocks()[0]["steps"]
    assert len(steps) == 1
    assert "drafting" not in steps[0] and "draft_preview" not in steps[0]
    assert steps[0]["status"] == "running"
    # A call the model wrote but that never ran leaves no step behind.
    builder.end_tool("generate_file", arguments={"filename": "AI.pptx", "content": "..."}, result="{}",
                     status="success", duration_ms=1, now_ms=5)
    builder.draft_tool("call-2", "web_search", {"query": "x"}, now_ms=6)
    builder.finish("completed", now_ms=7)
    assert [step["name"] for step in builder.blocks()[0]["steps"]] == ["generate_file"]


@pytest.mark.asyncio
async def test_draft_events_are_throttled_but_the_first_one_is_immediate():
    from packages.core.ai.llm_client import _ToolCallDraftEmitter

    seen = []

    async def handler(event_type, payload):
        seen.append((event_type, len(payload["arguments_text"])))

    emitter = _ToolCallDraftEmitter(handler)
    entry = {"id": "c1", "name": "generate_file", "arguments_text": ""}
    await emitter.update(0, {"id": "c1", "name": "", "arguments_text": ""})  # no name yet
    for chunk in ["{", '"filename": "x"'] + ["y" * 10] * 30:
        entry["arguments_text"] += chunk
        await emitter.update(0, entry)
    assert seen[0] == ("tool_call_draft", 1)
    # 300+ characters arrive within half a second: one event per 160 characters.
    assert 2 <= len(seen) <= 3


@pytest.mark.asyncio
async def test_chat_shows_the_call_being_written_then_runs_it_in_the_same_step():
    async def fake_loop(**kwargs):
        stream = kwargs["stream_handler"]
        await stream("tool_call_draft", {"index": 0, "id": "c1", "name": "web_search", "arguments_text": '{"query": "AI marketing'})
        kwargs["on_tool_start"]("web_search", {"query": "AI marketing 2026"})
        kwargs["on_tool_end"]("web_search", '{"results": [1]}', 10, {"query": "AI marketing 2026"})
        return _result("Found it.", derive_chat_outcome([obs("c1", "web_search")]))

    events = await _stream_turn(fake_loop)
    draft = next(p for e, p in events if e == "tool_draft")
    draft_step = next(b for b in draft["assistant_blocks"] if b["type"] == "process")["steps"][0]
    assert draft_step["drafting"] is True
    process, _texts = _final_blocks(events)
    assert len(process["steps"]) == 1
    assert process["steps"][0]["id"] == draft_step["id"]
    assert "drafting" not in process["steps"][0]


# ── Command steps are labelled with the model's own description ──────


def test_a_described_command_step_shows_the_description_not_the_command():
    builder = AssistantBlocksBuilder(started_at_ms=1)
    builder.start_tool("sandbox", {
        "action": "exec", "description": "生成第 3 页 SVG：增长渠道",
        "params": {"sandbox_id": "s1", "command": "python3 << 'PYEOF'\nsvg = '<svg>'\nPYEOF"},
    }, now_ms=2)
    step = builder.blocks()[0]["steps"][0]
    assert step["display_key"] == "component.assistant_process.tool.described"
    assert step["display_params"] == {"target": "生成第 3 页 SVG：增长渠道"}


def test_without_a_description_the_command_label_is_unchanged():
    builder = AssistantBlocksBuilder(started_at_ms=1)
    builder.start_tool("sandbox", {"action": "exec", "params": {"sandbox_id": "s1", "command": "ls"}}, now_ms=2)
    assert builder.blocks()[0]["steps"][0]["display_key"] != "component.assistant_process.tool.described"
    # Only command-style tools take the description as their label.
    other = AssistantBlocksBuilder(started_at_ms=1)
    other.start_tool("web_search", {"query": "x", "description": "ignored"}, now_ms=2)
    assert other.blocks()[0]["steps"][0]["display_key"] != "component.assistant_process.tool.described"


@pytest.mark.parametrize("schema_path", [
    ("packages.core.ai.tools.sandbox_tools", "SANDBOX_SCHEMA"),
])
def test_command_tools_ask_for_a_display_description(schema_path):
    import importlib

    schema = getattr(importlib.import_module(schema_path[0]), schema_path[1])
    properties = schema["function"]["parameters"]["properties"]
    assert properties["description"] == {"type": "string"}
    assert "description" not in schema["function"]["parameters"].get("required", [])
    # How to fill it lives in the chat guidance, outside the tool-schema budget.
    assert "set its description" in ChatStopGate().instructions


@pytest.mark.asyncio
async def test_the_description_never_reaches_the_sandbox_command():
    from packages.core.ai.tools import sandbox_tools

    seen = {}

    async def fake_exec(**kwargs):
        seen.update(kwargs)
        return '{"ok": true}'

    import unittest.mock as mock

    with mock.patch.object(sandbox_tools, "_sandbox_exec", new=fake_exec):
        await sandbox_tools._sandbox_handler(
            entity_id="e", user_id="u", action="exec", description="检查报告",
            params={"sandbox_id": "s1", "command": "ls"},
        )
    assert "description" not in seen
    assert all("检查报告" not in str(value) for value in seen.values())


# ── Prompt cache covers the history inside an agent loop ─────────────


def _loop_payload(last_role="tool"):
    filler = "x" * 20000
    messages = [
        {"role": "system", "content": "system " + filler},
        {"role": "user", "content": "request " + filler},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result " + filler},
    ]
    if last_role == "user":
        messages += [{"role": "assistant", "content": "answer"}, {"role": "user", "content": "next"}]
    return {"model": "anthropic/claude-sonnet-4.6", "messages": messages}


def _marked(messages):
    return [index for index, message in enumerate(messages)
            if isinstance(message.get("content"), list)
            and any(isinstance(block, dict) and block.get("cache_control") for block in message["content"])]


def test_inside_an_agent_loop_the_latest_tool_result_carries_the_history_breakpoint():
    from packages.core.ai.llm_client import _apply_prompt_cache

    payload = _loop_payload()
    assert _apply_prompt_cache(payload) is True
    assert _marked(payload["messages"]) == [0, 3]


def test_a_fresh_user_message_stays_outside_the_cached_history():
    from packages.core.ai.llm_client import _apply_prompt_cache

    payload = _loop_payload("user")
    _apply_prompt_cache(payload)
    assert _marked(payload["messages"]) == [0, 4]


def test_a_cached_tool_result_stays_a_text_block_for_native_anthropic():
    from packages.core.ai.llm_client import _tool_result_content_for_anthropic

    blocks = _tool_result_content_for_anthropic(
        [{"type": "text", "text": "result", "cache_control": {"type": "ephemeral"}}])
    assert blocks == [{"type": "text", "text": "result", "cache_control": {"type": "ephemeral"}}]
    assert _tool_result_content_for_anthropic("plain") == "plain"
    assert _tool_result_content_for_anthropic({"a": 1}) == '{"a": 1}'


# ── A quality gate rejection keeps the agent fixing until it passes ──


def _quality_rejection(gate, call_id):
    gate.observe_tool_result("sandbox", call_id, json.dumps({"ok": False, "error": {
        "code": "sandbox_quality_gate_blocked",
        "message": "PPTX_FINAL_QUALITY_GATE_BLOCKED: Slide 2: title is 31.5 pt; minimum is 32 pt",
    }}), {"action": "save_result"})


@pytest.mark.asyncio
async def test_a_quality_gate_rejection_sends_the_model_back_every_time():
    gate = ChatStopGate()
    _open_sandbox(gate)
    for attempt in range(6):
        _quality_rejection(gate, f"save-{attempt}")
        decision = await gate.validate_with_context("The deck could not be delivered.", FinalResponseContext())
        assert decision.action is FinalResponseAction.CONTINUE_EXECUTION
        assert decision.diagnostic["error"] == "delivery_quality_unmet"
    assert gate.observations["save-0"]["error_code"] == "sandbox_quality_gate_blocked"


@pytest.mark.asyncio
async def test_a_passing_save_after_quality_fixes_ends_the_turn_completed():
    gate = ChatStopGate()
    _open_sandbox(gate)
    _quality_rejection(gate, "save-1")
    assert (await gate.validate_with_context("x", FinalResponseContext())).action is FinalResponseAction.CONTINUE_EXECUTION
    gate.observe_tool_result("sandbox", "save-2", json.dumps({
        "saved": True, "document_id": "doc-1", "fs_path": "/k/deck.pptx",
        "artifact_role": "final", "saved_to_knowledge": True,
    }), {"action": "save_result"})
    decision = await gate.validate_with_context("Here is your deck.", FinalResponseContext())
    assert decision.action is FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_a_non_quality_failure_still_lets_the_reply_report_the_blocker():
    gate = ChatStopGate()
    _open_sandbox(gate)
    gate.observe_tool_result("sandbox", "save-1", json.dumps({"ok": False, "error": {
        "code": "sandbox_capacity_full", "message": "No sandbox capacity"}}), {"action": "save_result"})
    decision = await gate.validate_with_context("The sandbox is full right now.", FinalResponseContext())
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["status"] in {"partial", "blocked"}


@pytest.mark.asyncio
async def test_quality_fixing_stops_only_when_the_run_cannot_execute_any_more():
    gate = ChatStopGate()
    _open_sandbox(gate)
    _quality_rejection(gate, "save-1")
    decision = await gate.validate_with_context(
        "Out of rounds.", FinalResponseContext(allow_execution=False, stop_reason="max_rounds", budget_exhausted=True),
    )
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["status"] == "partial"


@pytest.mark.asyncio
async def test_quality_nudges_do_not_use_up_the_other_gates_budget():
    gate = ChatStopGate(requires_execution=True)
    _open_sandbox(gate)
    for attempt in range(3):
        _quality_rejection(gate, f"save-{attempt}")
        await gate.validate_with_context("x", FinalResponseContext())
    gate.observe_tool_result("sandbox", "save-ok", json.dumps({
        "saved": True, "document_id": "doc-1", "fs_path": "/k/deck.pptx",
        "artifact_role": "final", "saved_to_knowledge": True,
    }), {"action": "save_result"})
    decision = await gate.validate_with_context("Done.", FinalResponseContext())
    # The research gate still has its own nudge after three quality rounds.
    assert decision.action is FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "current_research_evidence_required"


# ── Fixable delivery failures keep the agent going ───────────────────


def _command_failure(gate, call_id, message):
    gate.observe_tool_result("sandbox", call_id, json.dumps({"error": message}), {"action": "exec"})


@pytest.mark.asyncio
async def test_a_wrong_file_name_does_not_end_the_turn():
    gate = ChatStopGate()
    _open_sandbox(gate)
    missing = "Command exited with status 1: ValueError: PPTX file not found: /skill/exports/deck_20206_123721.pptx"
    for attempt in range(2):
        _command_failure(gate, f"render-{attempt}", missing)
        decision = await gate.validate_with_context("Filename mismatch, cannot deliver.", FinalResponseContext())
        assert decision.action is FinalResponseAction.CONTINUE_EXECUTION
        assert decision.diagnostic["error"] == "delivery_failure_fixable"


@pytest.mark.asyncio
async def test_the_same_failure_three_times_in_a_row_may_be_reported():
    gate = ChatStopGate()
    _open_sandbox(gate)
    for attempt in range(3):
        _command_failure(gate, f"render-{attempt}", "Command exited with status 1: renderer crashed")
    decision = await gate.validate_with_context("The renderer keeps crashing.", FinalResponseContext())
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.outcome["status"] in {"partial", "blocked"}


@pytest.mark.asyncio
async def test_changing_failures_are_progress_and_keep_the_agent_going():
    gate = ChatStopGate()
    _open_sandbox(gate)
    for attempt, message in enumerate(["file not found", "font missing", "layout error", "svg invalid"]):
        _command_failure(gate, f"step-{attempt}", f"Command exited with status 1: {message}")
    decision = await gate.validate_with_context("x", FinalResponseContext())
    assert decision.action is FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["sandbox_access_denied", "sandbox_capacity_full", "entity_filesystem_unavailable"])
async def test_failures_the_agent_cannot_fix_may_be_reported(code):
    gate = ChatStopGate()
    _open_sandbox(gate)
    gate.observe_tool_result("sandbox", "step-1", json.dumps({"ok": False, "error": {"code": code, "message": "x"}}),
                             {"action": "exec"})
    assert (await gate.validate_with_context("Blocked.", FinalResponseContext())).action is FinalResponseAction.ACCEPT


def test_a_work_turn_answer_summarizes_the_outcome_not_just_a_link():
    # Staging: a 15-minute PPT turn answered with only "[deck.pptx](/viewer/…)".
    instructions = ChatStopGate().instructions
    assert "short summary of\nthe outcome" in instructions
    assert 'A file link or "done" on its own is not an answer.' in instructions
