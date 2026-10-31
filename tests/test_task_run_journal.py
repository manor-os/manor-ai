"""Task run journal (W4a): one bounded, model-visible row per run transition.

Every committed transition of a Task-loop run (a round, a plan revision, a
park, a person's answer, the terminal write) appends one ``task_run_journal``
row in the run row's own CAS transaction: a failed CAS or a rolled-back
transaction leaves no row, ``seq`` is the row's ``state_version`` and keeps
growing across wakes and retries. Payloads keep only the model-visible view.

An AST guard checks that every run-row version bump in the Task loop's host
and writers has a matching journal write or a named exemption
(mutation-checked below).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import test_task_agent_loop as base
import test_task_loop_host as host_tests
from packages.core.ai.runtime.run_state import RuntimeRunState
from packages.core.models.task import Task as Base_Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.task_run_journal import TaskRunJournal, TaskRunJournalKind
from packages.core.services import durable_run
from packages.core.services import task_run_journal as journal
from packages.core.services.task_coordinator_wakeup import TASK_COORDINATOR_RUN_SPEC
from packages.core.workers import task_agent_loop as worker
from packages.core.workers import task_loop_host
from packages.core.workers.task_loop_host import DurableRunWrites


REPO_ROOT = Path(__file__).resolve().parents[1]

_HOST_ONLY_KEYS = frozenset({
    "_match", "receipt_records", "receipt_handles", "checkpoint_digest", "claim_token",
    "claim_generation", "wake_generation", "approval_token", "call_id_scoped",
    "_runtime_tool_call_id_from_context", "_runtime_run_id_from_context",
    "_performance_run_id_from_context", "observed_at", "classification",
})


def _keys(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= _keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= _keys(item)
    return found


def _assert_model_visible(payload: dict) -> None:
    assert len(json.dumps(payload).encode("utf-8")) <= journal.PAYLOAD_MAX_BYTES
    assert not _keys(payload) & _HOST_ONLY_KEYS, _keys(payload) & _HOST_ONLY_KEYS
    assert not any(key.startswith("_") for key in _keys(payload))


async def _rows(db, run_id: str) -> list[SimpleNamespace]:
    """The run's committed journal rows in seq order (read before the rollback)."""

    rows = (await db.execute(
        select(TaskRunJournal).where(TaskRunJournal.run_id == run_id)
        .order_by(TaskRunJournal.seq).execution_options(populate_existing=True)
    )).scalars().all()
    columns = [column.key for column in TaskRunJournal.__table__.columns]
    snapshot = [SimpleNamespace(**{name: getattr(row, name) for name in columns}) for row in rows]
    await db.rollback()
    return snapshot


# ── payloads ────────────────────────────────────────────────────────────


def _assistant(text: str, calls: list[tuple[str, str, dict]]) -> dict:
    return {
        "role": "assistant", "content": text,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for call_id, name, args in calls
        ],
    }


def test_round_payload_is_bounded_and_carries_only_the_model_visible_view():
    huge = "x" * 50_000
    calls = [(f"c{i}", "record_note", {"text": huge, "approval_token": "secret"}) for i in range(60)]
    observed = [
        journal.observed_tool_call("record_note", json.dumps({"ok": True}), {
            "text": huge, "approval_token": "secret", "_runtime_tool_call_id_from_context": f"rt:c{i}",
        })
        for i in range(60)
    ]
    evidence = {
        "observations": [
            {"call_id": f"c{i}", "tool": "record_note", "effect_state": "confirmed",
             "observed_at": "2026-10-07T00:00:00+00:00", "receipt_handles": ["r1"]}
            for i in range(60)
        ],
        "receipts": [
            {"handle": f"r{i}", "call_id": "c0", "tool": "generate_file", "effect_state": "confirmed",
             "kind": "file", "document_id": f"doc{i}", "fs_path": f"/Reports/{i}.md", "name": f"{i}.md",
             "url": f"https://example.test/{i}", "content_sha256": "abc", "size": 10}
            for i in range(25)
        ],
    }
    payload = journal.round_payload(
        rounds=3,
        messages=[{"role": "user", "content": "go"}, _assistant(huge, calls)],
        observed=observed,
        run_state={"tool_evidence": evidence, "usage": {"total_tokens": 900, "cost": 0.5, "nested": {"a": 1}}},
        previous_usage={"total_tokens": 600, "cost": 0.25},
    )

    _assert_model_visible(payload)
    assert set(payload) <= journal.ROUND_PAYLOAD_KEYS
    assert payload["round"] == 3
    assert len(payload["assistant_text"]) <= journal.ASSISTANT_TEXT_MAX_CHARS
    assert payload["assistant_text_truncated"] is True
    assert 0 < len(payload["tool_calls"]) <= journal.TOOL_CALLS_MAX
    assert payload["tool_calls_omitted"] == 60 - len(payload["tool_calls"])
    assert payload["usage_delta"] == {"cost": 0.25, "total_tokens": 300}
    first = payload["tool_calls"][0]
    assert set(first) <= journal.TOOL_CALL_KEYS
    assert first["call_id"] == "c0" and first["status"] == "success"
    assert first["effect_state"] == "confirmed"
    assert len(first["args_summary"]) <= journal.ARGS_SUMMARY_MAX_CHARS
    assert "secret" not in json.dumps(payload)
    for receipt in first["receipts"]:
        assert set(receipt) <= set(journal.RECEIPT_KEYS)


def test_round_payload_keeps_receipts_when_they_fit():
    observed = [journal.observed_tool_call("generate_file", json.dumps({"ok": True}), {"name": "a.md"})]
    payload = journal.round_payload(
        rounds=1,
        messages=[_assistant("Saved it.", [("g1", "generate_file", {"name": "a.md"})])],
        observed=observed,
        run_state={"tool_evidence": {"receipts": [
            {"handle": f"r{i}", "call_id": "g1", "tool": "generate_file", "kind": "file",
             "document_id": f"d{i}", "fs_path": f"/a{i}.md", "name": f"a{i}.md", "content_sha256": "x"}
            for i in range(12)
        ]}},
        previous_usage=None,
    )
    call = payload["tool_calls"][0]
    assert call["call_id"] == "g1"
    assert len(call["receipts"]) == journal.RECEIPTS_PER_CALL_MAX
    assert call["receipts_omitted"] == 2
    assert call["receipts"][0] == {
        "handle": "r0", "kind": "file", "document_id": "d0", "fs_path": "/a0.md", "name": "a0.md",
    }
    _assert_model_visible(payload)


def test_a_requested_call_that_did_not_finish_is_listed_pending():
    ask = {"question": "Which date?"}
    messages = [
        {"role": "user", "content": "brief"},
        _assistant("I need a date.", [("ask", "request_input", ask), ("note", "record_note", {"text": "x"})]),
    ]
    payload = journal.round_payload(
        rounds=1, messages=messages,
        observed=[journal.observed_tool_call("record_note", json.dumps({"ok": True}), {"text": "x"})],
        run_state={}, previous_usage=None, previous_messages=messages[:1],
    )
    assert payload["assistant_text"] == "I need a date."
    assert [(c["call_id"], c["name"], c["status"]) for c in payload["tool_calls"]] == [
        ("note", "record_note", "success"), ("ask", "request_input", "pending"),
    ]
    assert json.loads(payload["tool_calls"][1]["args_summary"]) == ask
    # A round that added no model message (a resumed call ran alone) has no text.
    resumed = journal.round_payload(
        rounds=1, messages=messages + [{"role": "tool", "tool_call_id": "ask", "content": "{}"}],
        observed=[journal.observed_tool_call("request_input", "{}", ask)],
        run_state={}, previous_usage=None, previous_messages=messages,
    )
    assert resumed["assistant_text"] == ""
    assert [(c["call_id"], c["status"]) for c in resumed["tool_calls"]] == [("ask", "success")]


def test_a_retry_that_restarts_usage_is_not_a_negative_delta():
    assert journal._usage_delta({"total_tokens": 50}, {"total_tokens": 900}) == {"total_tokens": 50}


def test_wait_summaries_keep_no_host_fields():
    wait = {
        "version": 1, "kind": "waiting_approval", "tool_call_id": "c1", "tool_name": "tweet",
        "hitl_request_id": "h1", "checkpoint_digest": "d" * 64, "summary": "Post it",
        "parked_at": "2026-10-07T00:00:00+00:00", "resolution": None,
    }
    park = journal.park_payload(wait, reparked=True)
    wake = journal.wake_payload(wait, resolution="approved", task_resumed=True)
    for payload in (park, wake):
        _assert_model_visible(payload)
    assert park == {"wait": {
        "kind": "waiting_approval", "tool_call_id": "c1", "tool_name": "tweet",
        "hitl_request_id": "h1", "summary": "Post it", "reparked": True,
    }}
    assert wake["resolution"] == "approved" and wake["task_resumed"] is True


def test_terminal_payload_references_deliverables_only():
    payload = journal.terminal_payload(
        status="blocked", phase="failed", error_code="agent_loop_reported_blocked",
        files=[{"document_id": f"d{i}", "fs_path": f"/f{i}", "name": f"f{i}", "size": 3, "content": "x"}
               for i in range(12)],
    )
    _assert_model_visible(payload)
    assert payload["error_code"] == "agent_loop_reported_blocked"
    assert payload["deliverables"][0] == {"document_id": "d0", "fs_path": "/f0", "name": "f0"}
    assert payload["deliverables_omitted"] == 2


def test_only_declared_payload_keys_are_ever_written():
    with pytest.raises(ValueError, match="undeclared"):
        journal._bounded("round", {"round": 1, "messages": []})
    # Durable delegation's kinds carry only their declared keys too.
    for kind in ("delegate", "child_settled", "batch_settled"):
        with pytest.raises(ValueError, match="undeclared"):
            journal._bounded(kind, {"prompt": "the whole prompt"})


def test_delegation_payloads_are_bounded_and_model_visible():
    from types import SimpleNamespace

    children = [
        SimpleNamespace(
            id=f"c{index}", item_index=index, agent_label="Writer", agent_id="a1",
            prompt="p" * 5000, outcome_status="completed", phase="completed", interrupted=index == 1,
        )
        for index in range(3)
    ]
    delegate = journal.delegate_payload(tool_call_id="call-1", children=children)
    assert delegate["children"][0] == {
        "run_id": "c0", "item_index": 0, "agent": "Writer", "agent_id": "a1",
        "prompt_summary": "p" * journal.PROMPT_SUMMARY_MAX_CHARS,
    }
    settled = journal.batch_settled_payload(tool_call_id="call-1", resolution="children_settled", children=children)
    assert (settled["completed"], settled["total"]) == (3, 3)
    assert settled["children"][1] == {"run_id": "c1", "agent": "Writer", "status": "completed", "interrupted": True}
    child = journal.child_settled_payload(
        status="completed", phase="completed", outcome_status="completed", interrupted=False,
        error_code=None, receipts=[{"handle": f"r{i}", "document_id": f"d{i}", "sha256": "x"} for i in range(12)],
    )
    assert child["receipts"][0] == {"handle": "r0", "document_id": "d0"}
    assert child["receipts_omitted"] == 2
    for kind, payload in (("delegate", delegate), ("batch_settled", settled), ("child_settled", child)):
        assert journal._bounded(kind, payload) == json.loads(json.dumps(payload))


# ── the host writes with its CAS ────────────────────────────────────────


def _round_state(messages: list[dict], **evidence) -> dict:
    return RuntimeRunState(
        messages=messages, usage={"total_tokens": 100 * len(messages)},
        tool_evidence=evidence,
    ).snapshot()


def _round_messages(rounds: int) -> list[dict]:
    """The transcript after ``rounds`` rounds, each one record_note call."""

    messages: list[dict] = [{"role": "user", "content": "brief"}]
    for number in range(1, rounds + 1):
        messages += [
            _assistant(f"note {number}", [(f"n{number}", "record_note", {"text": f"note {number}"})]),
            {"role": "tool", "tool_call_id": f"n{number}", "content": json.dumps({"ok": True})},
        ]
    return messages


async def _checkpoint_round(host, rounds: int, call_id: str, *, observe: bool = True) -> None:
    assert call_id == f"n{rounds}"
    if observe:
        host.observe_tool_end("record_note", json.dumps({"ok": True}), {"text": f"note {rounds}"})
    messages = _round_messages(rounds)
    await host.checkpoint(rounds, messages, {}, [], _round_state(
        messages,
        observations=[{"call_id": call_id, "tool": "record_note", "effect_state": "confirmed"}],
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writes", host_tests._WRITES)
async def test_each_round_writes_one_row_with_its_checkpoint(db_session, make_writes):
    key, claim = await host_tests._claimed_run(db_session)
    host = host_tests._host(db_session, make_writes(key), claim)
    host.start()
    try:
        await _checkpoint_round(host, 1, "n1")
        await _checkpoint_round(host, 2, "n2")
        row = await host_tests._row(db_session, key)
    finally:
        await host.close()

    rows = await _rows(db_session, claim.run_id)
    assert [r.kind for r in rows] == ["round", "round"]
    assert [r.round for r in rows] == [1, 2]
    assert rows[-1].seq == row.state_version == host._state["version"]
    assert rows[0].seq < rows[1].seq
    run = await durable_run.lock_run(db_session, TASK_COORDINATOR_RUN_SPEC, key)
    assert {(r.run_kind, r.run_id, r.root_run_id, r.task_id, r.agent_id, r.depth) for r in rows} == {
        ("root", run.id, run.id, run.task_id, run.root_agent_id, 0),
    }
    assert rows[0].lifecycle_generation == run.lifecycle_generation
    assert rows[0].agent_subscription_id == run.root_subscription_id
    await db_session.rollback()
    assert rows[1].payload == {
        "round": 2,
        "assistant_text": "note 2",
        "tool_calls": [{
            "call_id": "n2", "name": "record_note", "args_summary": json.dumps({"text": "note 2"}),
            "status": "success", "effect_state": "confirmed", "receipts": [],
        }],
        # Two more messages than round 1's snapshot.
        "usage_delta": {"total_tokens": 200},
    }
    assert rows[0].projected_at is None and rows[0].projected_message_id is None
    assert host._state["round_journal"] == []


@pytest.mark.asyncio
async def test_a_failed_cas_writes_no_row(db_session):
    key, claim = await host_tests._claimed_run(db_session)
    host = host_tests._host(db_session, DurableRunWrites(TASK_COORDINATOR_RUN_SPEC, key), claim)
    host.start()
    try:
        # A successor's write moves the version past this owner's.
        row = await durable_run.lock_run(db_session, TASK_COORDINATOR_RUN_SPEC, key)
        row.state_version += 1
        await db_session.commit()
        with pytest.raises(RuntimeError, match="lost its claim"):
            await _checkpoint_round(host, 1, "n1")
        assert not await host.record_plan_revision({"revision": 1, "items": []}, log=AsyncMock())
    finally:
        await host.close()
    assert await _rows(db_session, claim.run_id) == []


@pytest.mark.asyncio
async def test_a_rolled_back_round_leaves_no_row_and_its_replay_writes_one(db_session, monkeypatch, caplog):
    key, claim = await host_tests._claimed_run(db_session)
    writes = DurableRunWrites(TASK_COORDINATOR_RUN_SPEC, key)
    host = host_tests._host(db_session, writes, claim)
    real_journal = writes.journal

    async def journal_then_crash(db, **kwargs):
        await real_journal(db, **kwargs)
        raise ConnectionError("commit never happened")

    host.start()
    try:
        monkeypatch.setattr(writes, "journal", journal_then_crash)
        version = host._state["version"]
        with pytest.raises(ConnectionError):
            await _checkpoint_round(host, 1, "n1")
        assert host._state["version"] == version
        assert await _rows(db_session, claim.run_id) == []

        # The same round's checkpoint, replayed, journals exactly once (the
        # uncommitted round kept its buffered call).
        monkeypatch.setattr(writes, "journal", real_journal)
        await _checkpoint_round(host, 1, "n1", observe=False)
    finally:
        await host.close()
    rows = await _rows(db_session, claim.run_id)
    assert [(r.kind, r.round) for r in rows] == [("round", 1)]
    assert [call["call_id"] for call in rows[0].payload["tool_calls"]] == ["n1"]

    # The idempotency key: re-appending the same (run, seq) inserts nothing. A
    # collision is a journal write without its own bump: tests fail on it,
    # production only logs it.
    run = await durable_run.lock_run(db_session, TASK_COORDINATOR_RUN_SPEC, key)
    run.state_version = rows[0].seq
    with pytest.raises(RuntimeError, match="seq collision"):
        await journal.append_run_journal(
            db_session, run=run, kind=TaskRunJournalKind.ROUND, payload={"round": 1}, round=1,
        )
    monkeypatch.delenv(journal.STRICT_ENV)
    with caplog.at_level("WARNING", logger=journal.__name__):
        await journal.append_run_journal(
            db_session, run=run, kind=TaskRunJournalKind.ROUND, payload={"round": 1}, round=1,
        )
    assert "seq collision" in caplog.text
    count = (await db_session.execute(
        select(func.count()).select_from(TaskRunJournal).where(TaskRunJournal.run_id == run.id)
    )).scalar_one()
    await db_session.rollback()
    assert count == 1


@pytest.mark.asyncio
async def test_seq_keeps_growing_across_a_restage_and_a_retried_round(db_session):
    key, claim = await host_tests._claimed_run(db_session)
    host = host_tests._host(db_session, DurableRunWrites(TASK_COORDINATOR_RUN_SPEC, key), claim)
    host.start()
    try:
        await _checkpoint_round(host, 1, "n1")
        assert await host.restage(
            reason_code="task_agent_loop_completion_missing", delay_seconds=0, checkpoint=None,
        )
    finally:
        await host.close()
    row = await durable_run.lock_run(db_session, TASK_COORDINATOR_RUN_SPEC, key)
    wake_generation = row.wake_generation
    await db_session.rollback()
    event = await durable_run._pending_wake_event(db_session, TASK_COORDINATOR_RUN_SPEC, key)
    retried, reason = await durable_run.claim_run_wake(
        db_session, TASK_COORDINATOR_RUN_SPEC, key=key, event_id=event.id,
        wake_generation=wake_generation, claim_token="retry-owner", lease_seconds=120.0,
    )
    assert reason == "claimed"
    await db_session.commit()
    second = host_tests._host(db_session, DurableRunWrites(TASK_COORDINATOR_RUN_SPEC, key), retried)
    second.start()
    try:
        # A fresh-transcript retry starts its rounds again at 1.
        await _checkpoint_round(second, 1, "n1")
    finally:
        await second.close()

    rows = await _rows(db_session, claim.run_id)
    assert [(r.kind, r.round) for r in rows] == [("round", 1), ("park", None), ("round", 1)]
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert rows[1].payload["wait"]["kind"] == "completion_missing"
    assert rows[1].payload["wait"]["wake_at"]


# ── the loop journals its whole run ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_full_run_journals_its_plan_rounds_and_terminal(db_session, monkeypatch):
    task, run, selection, event_id, generation = await base._agent_loop_run(db_session, monkeypatch)
    base._install_host(monkeypatch, db_session, selection)
    base._model(monkeypatch, [
        ("Planning first.", [base._call("plan", "update_task_plan", base.PLAN_ARGS)]),
        ("", [base._call("note", "record_note", {"text": "Launch on 2026-10-10"})]),
        ("Done.", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    ])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    task_id = task.id
    assert outcome == {"status": "completed", "task_id": task_id}
    run = await base._fresh(db_session, TaskCoordinatorRun, run.id)
    run_id, rounds, version = run.id, run.checkpoint["rounds"], run.state_version
    rows = await _rows(db_session, run_id)
    assert [r.kind for r in rows] == ["plan_revision", "round", "round", "round", "terminal"]
    # As many round rows as the loop ran rounds.
    assert [r.round for r in rows if r.kind == "round"] == list(range(1, rounds + 1))
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[-1] <= version
    assert {(r.task_id, r.agent_id, r.agent_subscription_id) for r in rows} == {
        (task_id, selection.root.agent_id, selection.root.subscription_id),
    }
    assert rows[0].payload == {"revision": 1, "item_count": 2, "acceptance_criteria_count": 1}
    calls = [[(c["call_id"], c["name"]) for c in r.payload["tool_calls"]] for r in rows if r.kind == "round"]
    assert calls == [[("plan", "update_task_plan")], [("note", "record_note")], [("done", "complete_task")]]
    assert rows[1].payload["assistant_text"] == "Planning first."
    note = rows[2].payload["tool_calls"][0]
    assert json.loads(note["args_summary"]) == {"text": "Launch on 2026-10-10"}
    assert note["status"] == "success"
    comment = (await db_session.execute(
        select(base.TaskLog).where(base.TaskLog.task_id == task_id, base.TaskLog.log_type == "comment")
    )).scalar_one()
    comment_id = comment.id
    await db_session.rollback()
    assert rows[-1].payload == {
        "status": "completed", "phase": "completed", "error_code": None, "source": "loop",
        "summary": base.COMPLETE_ARGS["summary"], "comment_log_id": comment_id, "deliverables": [],
    }
    for row in rows:
        _assert_model_visible(row.payload)


@pytest.mark.asyncio
async def test_a_superseded_plan_revision_journals_nothing(db_session, monkeypatch):
    task, run, selection, event_id, generation = await base._agent_loop_run(db_session, monkeypatch)
    base._install_host(monkeypatch, db_session, selection)
    session_factory = base.async_sessionmaker(
        db_session.bind, class_=base.AsyncSession, expire_on_commit=False,
    )

    async def steal_then_plan(_messages, _tools):
        async with session_factory() as db:
            row = await base._fresh(db, TaskCoordinatorRun, run.id)
            row.claim_token = "successor"
            row.state_version += 1
            await db.commit()
        return "", [base._call("plan", "update_task_plan", base.PLAN_ARGS)]

    base._model(monkeypatch, [steal_then_plan])

    outcome = await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    )

    assert outcome == {"status": "superseded", "task_id": task.id}
    assert await _rows(db_session, run.id) == []


@pytest.mark.asyncio
async def test_a_yield_journals_a_park_and_the_next_wake_continues_the_seq(db_session, monkeypatch):
    task, run, selection, event_id, generation = await base._agent_loop_run(db_session, monkeypatch)
    base._install_host(monkeypatch, db_session, selection)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)
    base._model(monkeypatch, [
        ("", [base._call("note", "record_note", {"text": "Launch on 2026-10-10"})]),
        ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    ])

    assert (await worker.run_task_agent_loop_once(
        event_id=event_id, task_id=task.id, wake_generation=generation,
    ))["status"] == "yielded"
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 3600)
    event = await base._event(db_session, task.id)
    assert (await worker.run_task_agent_loop_once(
        event_id=event.id, task_id=task.id, wake_generation=event.payload["wake_generation"],
    ))["status"] == "completed"

    rows = await _rows(db_session, run.id)
    assert [(r.kind, r.round) for r in rows] == [
        ("round", 1), ("park", None), ("round", 2), ("terminal", None),
    ]
    assert rows[1].payload["wait"]["kind"] == "yield"


@pytest.mark.asyncio
async def test_a_question_journals_its_park_the_answer_and_the_answered_call(db_session, monkeypatch):
    import test_task_agent_loop_hitl as hitl

    host, wake = await hitl._start(db_session, monkeypatch)
    hitl._with_request_input(host)
    base._model(monkeypatch, [
        ("", [base._call("ask", "request_input", hitl.ASK)]),
        ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    ])

    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "waiting_human"
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    hitl_id = run.checkpoint["task_loop_wait"]["hitl_request_id"]
    await hitl._answer(db_session, host.task_id, host.envelope.user_id, "2026-10-10")
    assert (await hitl._run(db_session, host.task_id))["status"] == "completed"

    rows = await _rows(db_session, host.run_id)
    assert [r.kind for r in rows] == ["round", "park", "wake", "round", "round", "terminal"]
    asked, park, wake, answered, finished, _terminal = rows
    # The round that asked lists the question as pending; the park names its card.
    assert [(c["call_id"], c["name"], c["status"]) for c in asked.payload["tool_calls"]] == [
        ("ask", "request_input", "pending"),
    ]
    assert park.payload == {"wait": {
        "kind": "waiting_human", "tool_call_id": "ask", "tool_name": "request_input",
        "hitl_request_id": hitl_id, "summary": hitl.ASK["question"],
    }}
    assert wake.payload["resolution"] == "answered" and wake.payload["task_resumed"] is True
    assert wake.payload["wait"]["hitl_request_id"] == hitl_id
    assert wake.payload["decided_by"] == host.envelope.user_id
    # The wake carries the lifecycle the answer rebound the run to.
    assert wake.lifecycle_generation == answered.lifecycle_generation != park.lifecycle_generation
    # The answered call is the resumed wake's first journaled call.
    assert [(c["call_id"], c["status"]) for c in answered.payload["tool_calls"]] == [("ask", "success")]
    assert [c["call_id"] for c in finished.payload["tool_calls"]] == ["done"]
    assert all(r.lifecycle_generation for r in rows)
    for row in rows:
        _assert_model_visible(row.payload)


# ── approvals, and transitions decided outside the loop ────────────────


@pytest.mark.asyncio
async def test_an_approval_stop_is_awaiting_approval_until_the_approved_call_runs(db_session, monkeypatch):
    import test_task_agent_loop_hitl as hitl

    host, executed, _seen, hitl_id = await hitl._park_on_tweet(
        db_session, monkeypatch, ("", [base._call("done", "complete_task", base.COMPLETE_ARGS)]),
    )
    await hitl._decide(db_session, host, hitl_id, "approve")
    assert (await hitl._run(db_session, host.task_id))["status"] == "completed"
    assert len(executed) == 1

    rows = await _rows(db_session, host.run_id)
    kinds = [r.kind for r in rows]
    assert kinds[kinds.index("park") - 1] == "round"
    asked = rows[kinds.index("park") - 1]
    statuses = {c["call_id"]: c["status"] for c in asked.payload["tool_calls"]}
    assert statuses["note"] == "success"
    assert statuses["post"] == "awaiting_approval"
    wake = rows[kinds.index("wake")]
    assert wake.payload["resolution"] == "approved"
    assert wake.payload["decided_by"] == host.envelope.user_id
    # The same call, approved, ran in a later round.
    later = [
        c for r in rows[kinds.index("wake"):] if r.kind == "round"
        for c in r.payload["tool_calls"] if c["call_id"] == "post"
    ]
    assert [c["status"] for c in later] == ["success"]


@pytest.mark.asyncio
async def test_a_cancelled_task_journals_its_runs_terminal_row(db_session, monkeypatch):
    import test_task_agent_loop_hitl as hitl
    from packages.core import database
    from packages.core.services.task_service import update_task
    from packages.core.tasks.runtime_tasks import reconcile_terminal_task_coordinator_runs_once

    host, wake = await hitl._start(db_session, monkeypatch)
    hitl._with_request_input(host)
    base._model(monkeypatch, [("", [base._call("ask", "request_input", hitl.ASK)])])
    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "waiting_human"
    monkeypatch.setattr(database, "create_worker_session", lambda: host.session_factory)
    task = await base._fresh(db_session, Base_Task, host.task_id)
    await update_task(db_session, task.id, task.entity_id, status="cancelled")
    await db_session.commit()

    assert await reconcile_terminal_task_coordinator_runs_once() >= 1

    rows = await _rows(db_session, host.run_id)
    assert [r.kind for r in rows] == ["round", "park", "terminal"]
    assert rows[-1].payload == {
        "status": "cancelled", "phase": "cancelled", "error_code": "task_cancelled",
        "source": "task_status", "summary": None, "comment_log_id": None, "deliverables": [],
    }
    # A second sweep finds nothing to stop and journals nothing.
    await reconcile_terminal_task_coordinator_runs_once()
    assert len(await _rows(db_session, host.run_id)) == 3


@pytest.mark.asyncio
async def test_a_paused_and_resumed_task_journals_a_wake_in_its_new_lifecycle(db_session, monkeypatch):
    import test_task_agent_loop_hitl as hitl
    from datetime import datetime, timedelta, timezone

    from packages.core.services.task_coordinator_wakeup import ensure_task_coordinator_run
    from packages.core.services.task_execution import task_execution_intent_generation

    host, wake = await hitl._start(db_session, monkeypatch)
    hitl._with_request_input(host)
    base._model(monkeypatch, [("", [base._call("ask", "request_input", hitl.ASK)])])
    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "waiting_human"
    task = await base._fresh(db_session, Base_Task, host.task_id)
    task.status, task.status_changed_at = "on_hold", datetime.now(timezone.utc)
    await db_session.commit()
    task = await base._fresh(db_session, Base_Task, host.task_id)
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await ensure_task_coordinator_run(db_session, task=task, selection=host.selection)
    generation = task_execution_intent_generation(task)
    await db_session.commit()

    rows = await _rows(db_session, host.run_id)
    assert [r.kind for r in rows] == ["round", "park", "wake"]
    assert rows[-1].payload["resolution"] == "task_resumed"
    assert rows[-1].payload["wait"]["hitl_request_id"]
    assert rows[-1].lifecycle_generation == generation != rows[1].lifecycle_generation


@pytest.mark.asyncio
async def test_a_retried_run_journals_a_retry_row(db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from packages.core.models.task_coordinator import TaskCoordinatorPhase
    from packages.core.services.task_coordinator_wakeup import ensure_task_coordinator_run

    task, run, selection, _event_id, _generation = await base._agent_loop_run(db_session, monkeypatch)
    row = await base._fresh(db_session, TaskCoordinatorRun, run.id)
    row.phase = TaskCoordinatorPhase.FAILED.value
    row.wake_generation = None
    await db_session.commit()
    task = await base._fresh(db_session, Base_Task, task.id)
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    await db_session.commit()

    rows = await _rows(db_session, run.id)
    assert [(r.kind, r.payload) for r in rows] == [
        ("retry", {"prior_phase": "failed", "reason": "task_retry"}),
    ]


@pytest.mark.asyncio
async def test_a_settled_media_job_and_the_stalled_backstop_journal_wakes(db_session, monkeypatch):
    from packages.core import database
    from packages.core.models.task_coordinator import TaskCoordinatorPhase
    from packages.core.services.task_coordinator_wakeup import wake_task_coordinator_for_media_job
    from packages.core.tasks.runtime_tasks import recover_stalled_task_coordinator_wakes_once

    task, run, _selection, _event_id, _generation = await base._agent_loop_run(db_session, monkeypatch)
    row = await base._fresh(db_session, TaskCoordinatorRun, run.id)
    row.phase = TaskCoordinatorPhase.WAITING_JOB.value
    row.checkpoint = {"task_loop_wait": {
        "version": 1, "kind": "waiting_job", "tool_call_id": "jobs", "job_ids": ["job-1"],
        "deadline_at": "2026-10-09T00:00:00+00:00",
    }}
    await db_session.commit()
    job = SimpleNamespace(id="job-1", entity_id=row.entity_id, status="completed", params={"task_id": task.id})

    assert await wake_task_coordinator_for_media_job(db_session, job) is not None
    await db_session.commit()

    # The wake row is then lost: the backstop restores it.
    event = await base._event(db_session, task.id)
    from datetime import datetime, timezone

    event.delivered_at = datetime.now(timezone.utc)
    row = await base._fresh(db_session, TaskCoordinatorRun, run.id)
    row.phase = TaskCoordinatorPhase.READY.value
    await db_session.commit()
    session_factory = base.async_sessionmaker(db_session.bind, class_=base.AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    # The sweep is global; this run's own rows below are what it must have done.
    assert await recover_stalled_task_coordinator_wakes_once() >= 1

    rows = await _rows(db_session, run.id)
    assert [(r.kind, r.payload["resolution"]) for r in rows] == [
        ("wake", "media_jobs_settled"), ("wake", "recovered"),
    ]
    assert rows[0].payload["wait"] == {"kind": "waiting_job", "tool_call_id": "jobs", "job_count": 1,
                                       "deadline_at": "2026-10-09T00:00:00+00:00"}
    # A READY run's leftover wait is not presented as what it waited on.
    assert rows[1].payload["wait"] == {}


@pytest.mark.asyncio
async def test_a_plan_dag_run_writes_no_journal():
    db = AsyncMock()
    run = TaskCoordinatorRun(engine="plan_dag", state_version=3)
    assert await journal.append_agent_loop_run_journal(
        db, run=run, kind=TaskRunJournalKind.TERMINAL,
        payload=journal.terminal_payload(status="cancelled", phase="cancelled", error_code=None, files=None),
    ) is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_created_at_is_the_inserts_clock_not_the_transactions(db_session):
    key, claim = await host_tests._claimed_run(db_session)
    run = await durable_run.lock_run(db_session, TASK_COORDINATOR_RUN_SPEC, key)
    for _ in range(2):
        run.state_version += 1
        await journal.append_run_journal(
            db_session, run=run, kind=TaskRunJournalKind.PARK, payload=journal.park_payload({"kind": "yield"}),
        )
    stamps = (await db_session.execute(
        select(TaskRunJournal.created_at).where(TaskRunJournal.run_id == claim.run_id)
        .order_by(TaskRunJournal.seq)
    )).scalars().all()
    await db_session.rollback()
    assert stamps[0] < stamps[1]


# ── bounds, matching and redaction ──────────────────────────────────────


def test_an_oversized_terminal_keeps_its_verdict():
    files = [{key: f"{key}-{i}-" + "x" * 500 for key in journal.DELIVERABLE_KEYS} for i in range(10)]
    payload = journal._bounded("terminal", journal.terminal_payload(
        status="blocked", phase="failed", error_code="agent_loop_reported_blocked", files=files,
        summary="s" * 5000, comment_log_id="log-1",
    ))
    _assert_model_visible(payload)
    assert (payload["status"], payload["phase"], payload["error_code"], payload["comment_log_id"]) == (
        "blocked", "failed", "agent_loop_reported_blocked", "log-1",
    )
    assert payload["source"] == "loop"
    assert len(payload["summary"]) <= journal.SUMMARY_MAX_CHARS
    assert len(payload["deliverables"]) + payload.get("deliverables_omitted", 0) == 10


def test_the_backstop_keeps_a_kinds_required_keys():
    payload = journal._bounded("wake", {
        "wait": {"kind": "waiting_human", "summary": "q" * 40_000},
        "resolution": "answered", "task_resumed": True, "decided_by": "user-1",
    })
    _assert_model_visible(payload)
    assert payload["truncated"] is True
    assert (payload["resolution"], payload["task_resumed"], payload["decided_by"]) == ("answered", True, "user-1")
    assert payload["wait"]["kind"] == "waiting_human"


def test_the_match_key_is_a_digest_not_the_arguments():
    entry = journal.observed_tool_call("record_note", json.dumps({"ok": True}), {"text": "private note body"})
    assert len(entry["_match"]) == 64 and "private" not in entry["_match"]


def test_a_rounds_calls_match_its_own_message_first():
    old = _assistant("", [("old", "record_note", {"text": "x"})])
    new = _assistant("", [("new", "record_note", {"text": "y"})])
    messages = [{"role": "user", "content": "brief"}, old, {"role": "tool", "tool_call_id": "old", "content": "{}"}, new]
    # The observed arguments equal the old call's, but the round asked for one record_note.
    observed = [journal.observed_tool_call("record_note", "{}", {"text": "x"})]
    payload = journal.round_payload(
        rounds=2, messages=messages, observed=observed, run_state={}, previous_usage=None,
        previous_messages=messages[:3],
    )
    assert [c["call_id"] for c in payload["tool_calls"]] == ["new"]


def test_a_gateway_call_is_one_row_with_the_inner_tool_s_effect_state():
    inner = {"input_path": "Videos/final.mp4"}
    messages = [_assistant("", [("g1", "call_tool", {"name": "probe_media", "arguments": inner})])]
    # The loop runs and observes a call_tool call as the tool it names, under its id.
    observed = [journal.observed_tool_call("probe_media", json.dumps({"status": "completed"}), inner)]
    payload = journal.round_payload(
        rounds=1, messages=messages, observed=observed, previous_usage=None, previous_messages=[],
        run_state={"tool_evidence": {"observations": [
            {"call_id": "g1", "tool": "probe_media", "effect_state": "none"},
        ]}},
    )
    assert [(c["call_id"], c["name"], c["status"], c["effect_state"]) for c in payload["tool_calls"]] == [
        ("g1", "probe_media", "success", "none"),
    ]


def test_long_tool_names_match_whole_and_are_cut_only_in_the_payload():
    stem = "mcp__" + "a" * 200
    messages = [_assistant("", [("one", stem + "_one", {}), ("two", stem + "_two", {})])]
    observed = [journal.observed_tool_call(stem + "_two", "{}", {}), journal.observed_tool_call(stem + "_one", "{}", {})]
    payload = journal.round_payload(
        rounds=1, messages=messages, observed=observed, run_state={}, previous_usage=None, previous_messages=[],
    )
    assert [c["call_id"] for c in payload["tool_calls"]] == ["two", "one"]
    assert all(len(c["name"]) <= 128 for c in payload["tool_calls"])


def test_a_compacted_transcript_lists_no_stale_pending_calls():
    previous = [{"role": "user", "content": "brief"}, _assistant("old", [("old", "record_note", {"text": "x"})]),
                {"role": "tool", "tool_call_id": "old", "content": "{}"}]
    # Compaction replaced the transcript; the previous checkpoint's last message is gone.
    compacted = [{"role": "user", "content": "summary of earlier work"},
                 _assistant("Continuing.", [("new", "record_note", {"text": "y"})])]
    observed = [journal.observed_tool_call("record_note", "{}", {"text": "y"})]
    payload = journal.round_payload(
        rounds=5, messages=compacted, observed=observed, run_state={}, previous_usage=None,
        previous_messages=previous,
    )
    assert journal.round_start(compacted, previous) is None
    assert payload["assistant_text"] == "Continuing."
    assert [(c["call_id"], c["status"]) for c in payload["tool_calls"]] == [("new", "success")]
    # A compaction that kept the previous last message still finds the round.
    kept = [{"role": "user", "content": "summary"}, previous[-1],
            _assistant("Next.", [("n2", "record_note", {"text": "z"})])]
    assert journal.round_start(kept, previous) == 2


def test_secret_looking_arguments_never_reach_the_journal():
    entry = journal.observed_tool_call("http_request", "{}", {
        "url": "https://api.example.test/v1/items?access_token=tok-abc&page=2",
        "password": "hunter2",
        "api_key": "key-123",
        "headers": {"Authorization": "Bearer bearer-xyz"},
        "body": {"client_secret": "cs-1", "name": "Launch"},
        "max_tokens": 40,
    })
    summary = entry["args_summary"] or ""
    for secret in ("hunter2", "key-123", "bearer-xyz", "cs-1", "tok-abc"):
        assert secret not in summary, secret
    assert "page=2" in summary or "Launch" in summary


# ── guard: every run-row version bump is journaled or exempt ────────────

_HOST_SOURCE = "packages/core/workers/task_loop_host.py"
_LOOP_SOURCE = "packages/core/workers/task_agent_loop.py"
_HITL_SOURCE = "packages/core/services/task_agent_loop_hitl.py"
_CAPABILITIES_SOURCE = "packages/core/workers/task_loop_capabilities.py"
_WAKEUP_SOURCE = "packages/core/services/task_coordinator_wakeup.py"
_RUNTIME_TASKS_SOURCE = "packages/core/tasks/runtime_tasks.py"
_RUN_HOST_SOURCE = "packages/core/workers/task_run_host.py"
_CHILD_WAKEUP_SOURCE = "packages/core/services/task_agent_child_wakeup.py"
_CHILD_LOOP_SOURCE = "packages/core/workers/task_agent_child_loop.py"
_DELEGATION_SOURCE = "packages/core/services/task_agent_delegation.py"
_SETTLEMENT_SOURCE = "packages/core/services/task_agent_child_settlement.py"

# Run-row writes that bump ``state_version`` (durable-run protocol and the
# Task coordinator wrappers), and the host ``writes`` methods that do.
_BUMP_CALLS = frozenset({
    "stage_run_wake", "claim_run_wake", "checkpoint_run", "settle_run_wake",
    "release_run_claim", "record_run_wake_failure", "clear_run_claim_and_wake", "reopen_run",
    "stage_task_coordinator_wake", "claim_task_coordinator_wake",
    "checkpoint_task_coordinator_run", "settle_task_coordinator_wake",
    "release_task_coordinator_claim",
    "stage_task_agent_child_wake",
    # The delegation primitive that stages (bumps) a call's child rows.
    "stage_children",
})
_WRITES_BUMPS = frozenset({"checkpoint", "stage", "settle", "release", "record_wake_failure"})
_JOURNAL_CALLS = frozenset({"append_run_journal", "append_agent_loop_run_journal", "_journal"})

# Outside TaskLoopHost (whose exemptions live in the host module).
_EXEMPT = {
    # The run kind's write primitives: the host journals around them.
    f"{_HOST_SOURCE}::DurableRunWrites": "write primitives",
    f"{_LOOP_SOURCE}::_TaskCoordinatorRunWrites": "write primitives",
    # Cloud-only: a capability acquisition is the round's acquire_capabilities
    # call, journaled with that round.
    f"{_CAPABILITIES_SOURCE}::TaskLoopAcquisitionScope.commit": "journaled as the round's tool call",
    # The Task coordinator's named wrappers of the durable-run writes.
    f"{_WAKEUP_SOURCE}::stage_task_coordinator_wake": "write primitive",
    f"{_WAKEUP_SOURCE}::claim_task_coordinator_wake": "write primitive",
    f"{_WAKEUP_SOURCE}::checkpoint_task_coordinator_run": "write primitive",
    f"{_WAKEUP_SOURCE}::settle_task_coordinator_wake": "write primitive",
    f"{_WAKEUP_SOURCE}::release_task_coordinator_claim": "write primitive",
    # Claiming a wake moves ownership; the run's work does not change.
    f"{_RUN_HOST_SOURCE}::admit_and_claim": "claim only",
    # The child run's named wrapper of the durable-run stage.
    f"{_CHILD_WAKEUP_SOURCE}::stage_task_agent_child_wake": "write primitive",
    # A child wake is claimed as admission's last step: ownership only.
    f"{_CHILD_LOOP_SOURCE}::_admit_and_claim": "claim only",
    # The primitive that stages a call's child wakes and returns the rows it
    # bumped: its callers journal them. A park's own children are named by
    # its delegate row (TaskLoopHost._park_on_children); deeper rows a resume
    # restages get their own wake rows (restage_descendants).
    f"{_DELEGATION_SOURCE}::stage_children": "write primitive: callers journal the rows it returns",
    # A run's very first wake: nothing has happened yet. A retried or resumed
    # run is journaled inside ensure_task_coordinator_run. Follow-up (W4c): a
    # "started" row if the comment thread wants one.
    f"{_RUNTIME_TASKS_SOURCE}::consume_task_execution_intent_once": "first wake of a run (W4c follow-up)",
}


def _bumps(node: ast.AST) -> list[str]:
    found = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.AugAssign)
            and isinstance(child.target, ast.Attribute)
            and child.target.attr == "state_version"
        ):
            found.append(f"state_version:{child.lineno}")
        if not isinstance(child, ast.Call):
            continue
        func_node = child.func
        name = func_node.attr if isinstance(func_node, ast.Attribute) else getattr(func_node, "id", None)
        if name in _BUMP_CALLS:
            found.append(f"{name}:{child.lineno}")
        elif (
            isinstance(func_node, ast.Attribute)
            and name in _WRITES_BUMPS
            and isinstance(func_node.value, ast.Attribute)
            and func_node.value.attr == "writes"
        ):
            found.append(f"writes.{name}:{child.lineno}")
    return found


def _journals(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = child.func.attr if isinstance(child.func, ast.Attribute) else getattr(child.func, "id", None)
            if name in _JOURNAL_CALLS:
                return True
    return False


def _functions(tree: ast.AST, prefix: str = ""):
    """Every function in the module with its qualified name (nested ones too)."""

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from _functions(node, f"{prefix}{node.name}.")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{node.name}", node
            yield from _functions(node, f"{prefix}{node.name}.")


def _unjournaled(sources: dict[str, str], host_exempt: dict[str, str]) -> list[str]:
    exempt = dict(_EXEMPT)
    exempt.update({f"{_HOST_SOURCE}::TaskLoopHost.{name}": why for name, why in host_exempt.items()})
    found = []
    for path, source in sources.items():
        for name, node in _functions(ast.parse(source)):
            qualified = f"{path}::{name}"
            if any(qualified == key or qualified.startswith(f"{key}.") for key in exempt):
                continue
            # Nested functions are checked on their own.
            own = ast.Module(body=[
                stmt for stmt in node.body
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ], type_ignores=[])
            bumps = _bumps(own)
            if bumps and not _journals(own):
                found.append(f"{qualified}: {', '.join(bumps)}")
    return found


def _sources() -> dict[str, str]:
    return {
        path: (REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (
            _HOST_SOURCE, _LOOP_SOURCE, _HITL_SOURCE, _CAPABILITIES_SOURCE,
            _WAKEUP_SOURCE, _RUNTIME_TASKS_SOURCE, _RUN_HOST_SOURCE,
            _CHILD_WAKEUP_SOURCE, _CHILD_LOOP_SOURCE, _DELEGATION_SOURCE, _SETTLEMENT_SOURCE,
        )
        if (REPO_ROOT / path).exists()
    }


def test_every_run_row_version_bump_is_journaled_or_exempt():
    sources = _sources()
    assert _unjournaled(sources, task_loop_host.JOURNAL_EXEMPT_METHODS) == []
    # Every exemption names a definition that still exists.
    defined = {
        f"{path}::{name}" for path, source in sources.items()
        for name, _node in _functions(ast.parse(source))
    } | {
        f"{path}::{node.name}" for path, source in sources.items()
        for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef)
    }
    stale = [key for key in _EXEMPT if key not in defined and key.split("::")[0] in sources]
    assert stale == []
    host_methods = {name.split(".")[-1] for name, _ in _functions(ast.parse(sources[_HOST_SOURCE]))}
    assert set(task_loop_host.JOURNAL_EXEMPT_METHODS) <= host_methods
    # The scan sees what it guards.
    assert any(
        _bumps(node) for name, node in _functions(ast.parse(sources[_HOST_SOURCE]))
        if name == "TaskLoopHost.checkpoint"
    )


@pytest.mark.parametrize(("path", "old", "new"), [
    # The round checkpoint stops journaling.
    (
        _HOST_SOURCE,
        "                await self._journal(db, TaskRunJournalKind.ROUND, entry, round=entry[\"round\"])\n",
        "",
    ),
    # A park stops journaling.
    (
        _HOST_SOURCE,
        "                await self._journal(db, TaskRunJournalKind.PARK, journal.park_payload(wait))\n",
        "",
    ),
    # A new host method bumps the run without a journal row.
    (
        _HOST_SOURCE,
        "    async def human_wait_still_open(",
        "    async def sneaky(self, db):\n"
        "        await self.writes.checkpoint(db, **self._owner(), expected_state_version=0)\n\n"
        "    async def human_wait_still_open(",
    ),
    # The terminal write stops journaling.
    (
        _LOOP_SOURCE,
        "        await append_run_journal(\n            db, run=run, kind=TaskRunJournalKind.TERMINAL,",
        "        _skipped = dict(\n            db=db, run=run, kind=TaskRunJournalKind.TERMINAL,",
    ),
    # A new loop helper bumps the run row directly.
    (
        _LOOP_SOURCE,
        "def _aware(value: datetime) -> datetime:",
        "def _bump(run):\n    run.state_version += 1\n\n\ndef _aware(value: datetime) -> datetime:",
    ),
    # A person's answer reaches the run without a wake row.
    (
        _HITL_SOURCE,
        "    await append_run_journal(\n        db, run=run, kind=TaskRunJournalKind.WAKE,",
        "    _skipped = dict(\n        db=db, run=run, kind=TaskRunJournalKind.WAKE,",
    ),
    # A cancelled Task's run stops without a terminal row.
    (
        _RUNTIME_TASKS_SOURCE,
        "            await append_agent_loop_run_journal(\n                db, run=run, kind=TaskRunJournalKind.TERMINAL,",
        "            _skipped = dict(\n                db=db, run=run, kind=TaskRunJournalKind.TERMINAL,",
    ),
    # A settled media job wakes the run without a wake row.
    (
        _WAKEUP_SOURCE,
        "        await append_agent_loop_run_journal(\n            db, run=run, kind=TaskRunJournalKind.WAKE,\n"
        "            payload=wake_payload(wait, resolution=\"media_jobs_settled\"",
        "        await print(\n            db, run=run, kind=TaskRunJournalKind.WAKE,\n"
        "            payload=wake_payload(wait, resolution=\"media_jobs_settled\"",
    ),
    # A Task retry or resume reopens the run without a row.
    (
        _WAKEUP_SOURCE,
        "    await append_agent_loop_run_journal(db, run=run, kind=kind, payload=payload)\n",
        "    await print(db, run=run, kind=kind, payload=payload)\n",
    ),
    # A restored lost wake is not journaled.
    (
        _RUNTIME_TASKS_SOURCE,
        "                await append_agent_loop_run_journal(\n                    db, run=run, kind=TaskRunJournalKind.WAKE,",
        "                await print(\n                    db, run=run, kind=TaskRunJournalKind.WAKE,",
    ),
    # A restored lost wake of a child run is not journaled.
    (
        _RUNTIME_TASKS_SOURCE,
        "                await append_run_journal(\n                    db, run=child, kind=TaskRunJournalKind.WAKE,",
        "                await print(\n                    db, run=child, kind=TaskRunJournalKind.WAKE,",
    ),
    # A terminal Task cancels its child runs without terminal rows.
    (
        _CHILD_WAKEUP_SOURCE,
        "        await append_run_journal(\n            db, run=child, kind=TaskRunJournalKind.TERMINAL,",
        "        await print(\n            db, run=child, kind=TaskRunJournalKind.TERMINAL,",
    ),
    # A settled media job wakes a child run without a wake row.
    (
        _CHILD_WAKEUP_SOURCE,
        "        await append_run_journal(\n            db, run=child, kind=TaskRunJournalKind.WAKE,",
        "        await print(\n            db, run=child, kind=TaskRunJournalKind.WAKE,",
    ),
    # A child run ends without its child_settled row.
    (
        _SETTLEMENT_SOURCE,
        "    await append_run_journal(\n        db, run=child, kind=TaskRunJournalKind.CHILD_SETTLED,",
        "    await print(\n        db, run=child, kind=TaskRunJournalKind.CHILD_SETTLED,",
    ),
    # A batch settles into its parent without a batch_settled row.
    (
        _SETTLEMENT_SOURCE,
        "    await append_run_journal(\n        db, run=parent, kind=TaskRunJournalKind.BATCH_SETTLED,",
        "    await print(\n        db, run=parent, kind=TaskRunJournalKind.BATCH_SETTLED,",
    ),
    # A parent stops a broken batch's children without their terminal rows.
    (
        _SETTLEMENT_SOURCE,
        "        await append_run_journal(\n            db, run=child, kind=TaskRunJournalKind.TERMINAL,",
        "        await print(\n            db, run=child, kind=TaskRunJournalKind.TERMINAL,",
    ),
    # A delegation parks without its delegate row.
    (
        _HOST_SOURCE,
        "        await self._journal(\n            db, TaskRunJournalKind.DELEGATE,",
        "        await print(\n            db, TaskRunJournalKind.DELEGATE,",
    ),
    # A woken parent settles its own batch without a batch_settled row.
    (
        _HOST_SOURCE,
        "                await self._journal(\n                    db, TaskRunJournalKind.BATCH_SETTLED,",
        "                await print(\n                    db, TaskRunJournalKind.BATCH_SETTLED,",
    ),
    # A resume restages a grandchild without its wake row.
    (
        _DELEGATION_SOURCE,
        "        await append_run_journal(\n            db, run=row, kind=TaskRunJournalKind.WAKE,",
        "        await print(\n            db, run=row, kind=TaskRunJournalKind.WAKE,",
    ),
    # A new delegation helper stages child wakes without any row.
    (
        _DELEGATION_SOURCE,
        "async def restage_descendants(",
        "async def sneaky(db, child_id):\n    await stage_task_agent_child_wake(db, child_id=child_id, reason='x')\n\n\n"
        "async def restage_descendants(",
    ),
])
def test_the_journal_guard_catches_an_unjournaled_bump(path, old, new):
    sources = _sources()
    assert sources[path].count(old) == 1, old
    sources[path] = sources[path].replace(old, new)
    assert _unjournaled(sources, task_loop_host.JOURNAL_EXEMPT_METHODS)


def test_an_exemption_must_be_declared_by_name():
    exempt = dict(task_loop_host.JOURNAL_EXEMPT_METHODS)
    exempt.pop("close")
    assert _unjournaled(_sources(), exempt) == [
        next(
            f"{_HOST_SOURCE}::TaskLoopHost.close: {', '.join(_bumps(node))}"
            for name, node in _functions(ast.parse(_sources()[_HOST_SOURCE]))
            if name == "TaskLoopHost.close"
        )
    ]
