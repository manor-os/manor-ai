"""Phase 1 of ``docs/EXECUTION_OBSERVABILITY_DESIGN_ZH.md`` — a failed step
must leave usable evidence behind.

Three confirmed defects (§2.2 A, B, D) share one consequence: the moment an
operator most needs the trace is exactly the moment nothing was written.

* **A** — ``_exec_subagent`` persisted its runtime events *after* every raise
  path, so any failure discarded the whole in-memory event list with the
  process.
* **B** — ``ToolCallLog.success`` was never passed by either chat call site,
  so every row inherited the column default ``true``; the worker path wrote
  no rows at all.
* **D** — worker ``build_agent_context`` calls omitted ``task_id`` (and, for
  the subagent handler, ``conversation_id``), so every ``RuntimeEventLog``
  row from a plan step was unjoinable to the step that produced it.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import (
    ChatSurface,
    RuntimeEnvelope,
    RuntimePrincipal,
    RuntimePrincipalKind,
    RuntimeProfile,
)


# ── Defect A guard: one run never writes its events twice ──────────


def _envelope(**overrides):
    kwargs = dict(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        profile=RuntimeProfile.BACKGROUND_WORKER,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.SYSTEM_WORKER),
        entity_id="ent_1",
        workspace_id="ws_1",
        agent_id="agent_1",
    )
    kwargs.update(overrides)
    return RuntimeEnvelope(**kwargs)


class _RecordingSession:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add_all(self, rows):
        self.sink.extend(rows)

    async def commit(self):
        return None


def test_persisting_the_same_envelope_twice_does_not_duplicate_rows(monkeypatch):
    """``runtime_event_logs`` has no unique constraint and the envelope's
    event list is never cleared, so a second persist would silently double
    every row. The persist path must be idempotent per event."""
    from packages.core.services import runtime_event_service

    envelope = _envelope(task_id="task_1", conversation_id="conv_1")
    envelope.metadata["runtime_events"] = [
        {"type": "tool_start", "tool_name": "web_search"},
        {"type": "tool_end", "tool_name": "web_search"},
    ]

    rows: list = []
    monkeypatch.setattr(
        "packages.core.database.async_session", lambda: _RecordingSession(rows),
    )

    first = asyncio.run(
        runtime_event_service.persist_runtime_events_best_effort(envelope)
    )
    second = asyncio.run(
        runtime_event_service.persist_runtime_events_best_effort(envelope)
    )

    assert first == 2
    assert second == 0
    assert len(rows) == 2


def test_events_appended_after_a_persist_are_still_written(monkeypatch):
    """The guard must skip already-written events, not the whole envelope —
    a second run of the same envelope still has to record its new events."""
    from packages.core.services import runtime_event_service

    envelope = _envelope(task_id="task_1")
    envelope.metadata["runtime_events"] = [{"type": "tool_start", "tool_name": "a"}]

    rows: list = []
    monkeypatch.setattr(
        "packages.core.database.async_session", lambda: _RecordingSession(rows),
    )

    assert asyncio.run(
        runtime_event_service.persist_runtime_events_best_effort(envelope)
    ) == 1
    envelope.metadata["runtime_events"].append({"type": "tool_end", "tool_name": "a"})
    assert asyncio.run(
        runtime_event_service.persist_runtime_events_best_effort(envelope)
    ) == 1
    assert len(rows) == 2
    assert [row.sequence for row in rows] == [0, 1]


# ── Defect D: worker runtime events carry their task ───────────────


def test_runtime_event_rows_inherit_the_envelope_task_id():
    """The envelope is the carrier: once the handler threads task_id in,
    every persisted row is joinable to the task."""
    from packages.core.services.runtime_event_service import (
        runtime_event_records_from_envelope,
    )

    envelope = _envelope(task_id="task_9", conversation_id="conv_9")
    envelope.metadata["runtime_events"] = [{"type": "tool_start", "tool_name": "x"}]

    rows = runtime_event_records_from_envelope(envelope)
    assert rows and rows[0]["task_id"] == "task_9"
    assert rows[0]["conversation_id"] == "conv_9"


# ── Defect B: the tool-result error contract ───────────────────────


@pytest.mark.parametrize(
    "result,expected_error",
    [
        ('{"status": "ok"}', None),
        ('{"ok": true}', None),
        ("plain text result", None),
        ('{"__hitl__": true, "approvalId": "a"}', None),
        ('{"status": "waiting_human"}', None),
        ('{"status": "rejected"}', None),
        ('{"error": "No image generated"}', "No image generated"),
        ('{"status": "failed", "message": "quota exceeded"}', "quota exceeded"),
        ('{"status": "timeout"}', "timeout"),
        ("Error: boom", "boom"),
        ("Tool error (web_search): boom", "Tool error (web_search): boom"),
    ],
)
def test_tool_call_error_decodes_the_result_contract(result, expected_error):
    from packages.core.ai.runtime import runtime_tool_call_error

    assert runtime_tool_call_error(result) == expected_error


def test_runtime_error_envelopes_are_built_from_the_shared_constants():
    """Producer and decoder share one constant so they cannot drift — the
    handler wrapper's ``Error: ...`` form was invisible to every existing
    classifier before this."""
    from packages.core.ai.runtime.streams import (
        RUNTIME_TOOL_ERROR_PREFIX,
        RUNTIME_TOOL_EXECUTOR_ERROR_PREFIX,
        runtime_tool_call_error,
    )

    assert runtime_tool_call_error(f"{RUNTIME_TOOL_ERROR_PREFIX}kaboom") == "kaboom"
    assert runtime_tool_call_error(
        f"{RUNTIME_TOOL_EXECUTOR_ERROR_PREFIX} (t): kaboom"
    ) is not None


def test_tool_call_outcome_is_success_for_a_clean_result():
    from packages.core.ai.runtime.streams import runtime_tool_call_outcome

    assert runtime_tool_call_outcome(
        "generate_image", json.dumps({"status": "completed", "url": "u"}),
    ) == "success"


def test_tool_call_outcome_is_error_when_the_result_carries_an_error():
    from packages.core.ai.runtime.streams import runtime_tool_call_outcome

    assert runtime_tool_call_outcome(
        "generate_image", json.dumps({"error": "No image generated"}),
    ) == "error"


def test_tool_call_outcome_is_empty_result_for_a_zero_match_search():
    from packages.core.ai.runtime.streams import runtime_tool_call_outcome
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(matches=[], query="fly to mars", total_tool_count=40)
    assert runtime_tool_call_outcome("search_tools", json.dumps(payload)) == "empty_result"


def test_tool_call_outcome_is_success_for_a_populated_search():
    from packages.core.ai.runtime.streams import runtime_tool_call_outcome
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[{"name": "send_email", "available": True}], query="email",
    )
    assert runtime_tool_call_outcome("search_tools", json.dumps(payload)) == "success"


def test_tool_call_outcome_is_error_even_for_search_tools_when_search_itself_fails():
    """A real exception from search_tools must still count as an error, not
    get reclassified as empty_result just because it's the search tool."""
    from packages.core.ai.runtime.streams import runtime_tool_call_outcome

    assert runtime_tool_call_outcome(
        "search_tools", json.dumps({"error": "index unavailable"}),
    ) == "error"


def test_tool_call_outcome_ignores_empty_list_fields_on_non_search_tools():
    """Only search_tools' own "matches": [] means empty_result — a
    coincidental empty list on an unrelated tool's payload must not."""
    from packages.core.ai.runtime.streams import runtime_tool_call_outcome

    assert runtime_tool_call_outcome(
        "list_tenants", json.dumps({"matches": [], "tenants": []}),
    ) == "success"


# ── Defect B: chat writes a real success/error ─────────────────────


async def _run_chat_with_tool_result(
    db_session, monkeypatch, tool_result: str, tool_name: str = "generate_image",
):
    """Drive the REAL ``run_chat_message`` -> real ``_on_tool_end`` wiring
    (same pattern as tests/test_tool_path_memory.py) and capture what the
    tool-call log would persist."""
    from unittest.mock import AsyncMock, patch

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services.chat_service import run_chat_message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id, entity_id=entity_id, user_id=user_id,
            title="Failure visibility",
        )
    )
    await db_session.flush()

    recorded: list[dict] = []

    async def fake_record_tool_call(_db, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        "packages.core.services.usage_service.record_tool_call", fake_record_tool_call,
    )

    async def fake_context(*_args, **_kwargs):
        return (
            "system",
            [{"type": "function", "function": {"name": tool_name}}],
            [],
            SimpleNamespace(
                workspace_id=None, task_id=None, runtime_envelope=None,
                tool_profile=None, allowed_tool_names=None,
                user=None, entity=None, hinted_tool_names=set(),
            ),
        )

    async def fake_loop(**kwargs):
        kwargs["on_tool_end"](tool_name, tool_result, 12, {"prompt": "a cat"})
        from packages.core.ai.agentic_loop import AgenticResult
        return AgenticResult(
            content="Done.", messages=[], usage={}, rounds=1,
            tool_calls_made=[tool_name],
        )

    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=fake_context,
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=fake_loop,
        ),
        patch(
            "packages.core.services.model_resolver.resolve_model_for_user",
            new=AsyncMock(return_value="openai/gpt-5.5"),
        ),
        patch("packages.core.services.chat_service.record_chat_llm_usage", new=AsyncMock()),
        patch(
            "packages.core.services.chat_service.resolve_author_subscription_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_runtime_learning",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "packages.core.services.chat_service.schedule_learning_candidate_applies",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_runtime_events",
            new=AsyncMock(),
        ),
    ):
        await run_chat_message(
            "draw a cat", conversation_id,
            entity_id=entity_id, user_id=user_id, db=db_session,
        )
        for _ in range(3):
            await asyncio.sleep(0)

    return recorded


@pytest.mark.asyncio
async def test_chat_tool_error_payload_is_logged_as_a_failure(db_session, monkeypatch):
    """Defect B: a tool that *returns* an error payload is the common failure
    shape. Every row said success=True before this."""
    recorded = await _run_chat_with_tool_result(
        db_session, monkeypatch, json.dumps({"error": "No image generated"}),
    )
    assert len(recorded) == 1
    assert recorded[0]["tool_name"] == "generate_image"
    assert recorded[0]["success"] is False
    assert recorded[0]["error"] == "No image generated"


@pytest.mark.asyncio
async def test_chat_tool_success_is_still_logged_as_success(db_session, monkeypatch):
    """Guard against over-correcting: a healthy call stays success/NULL."""
    recorded = await _run_chat_with_tool_result(
        db_session, monkeypatch, json.dumps({"status": "completed", "url": "u"}),
    )
    assert len(recorded) == 1
    assert recorded[0]["success"] is True
    assert recorded[0]["error"] is None


@pytest.mark.asyncio
async def test_chat_tool_error_payload_is_logged_with_error_outcome(db_session, monkeypatch):
    recorded = await _run_chat_with_tool_result(
        db_session, monkeypatch, json.dumps({"error": "No image generated"}),
    )
    assert recorded[0]["outcome"] == "error"


@pytest.mark.asyncio
async def test_chat_search_tools_empty_match_is_logged_as_empty_result(db_session, monkeypatch):
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(matches=[], query="fly to mars", total_tool_count=40)
    recorded = await _run_chat_with_tool_result(
        db_session, monkeypatch, json.dumps(payload), tool_name="search_tools",
    )
    assert recorded[0]["success"] is True
    assert recorded[0]["outcome"] == "empty_result"


@pytest.mark.asyncio
async def test_chat_tool_success_is_logged_with_success_outcome(db_session, monkeypatch):
    recorded = await _run_chat_with_tool_result(
        db_session, monkeypatch, json.dumps({"status": "completed", "url": "u"}),
    )
    assert recorded[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_chat_message_wires_per_call_billing_callbacks(db_session, monkeypatch):
    """Chat delegates billing to the per-call callbacks, not a final aggregate."""
    from unittest.mock import AsyncMock, patch

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services.chat_service import run_chat_message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id, entity_id=entity_id, user_id=user_id,
            title="Rounds threading",
        )
    )
    await db_session.flush()

    captured = {}

    async def fake_context(*_args, **_kwargs):
        return (
            "system", [], [],
            SimpleNamespace(
                workspace_id=None, task_id=None, runtime_envelope=None,
                tool_profile=None, allowed_tool_names=None,
                user=None, entity=None, hinted_tool_names=set(),
            ),
        )

    async def fake_loop(**kwargs):
        captured["before"] = kwargs.get("on_llm_call_before")
        captured["settled"] = kwargs.get("on_llm_usage_settled")
        from packages.core.ai.agentic_loop import AgenticResult
        return AgenticResult(
            content="Done.", messages=[], usage={"total_tokens": 10}, rounds=7,
            tool_calls_made=[],
        )

    with (
        patch("packages.core.services.chat_service.resolve_runtime_chat_context", new=fake_context),
        patch("packages.core.services.chat_service.runtime_execute_chat_agent_loop", new=fake_loop),
        patch("packages.core.services.model_resolver.resolve_model_for_user", new=AsyncMock(return_value="openai/gpt-5.5")),
        patch("packages.core.services.chat_service.resolve_author_subscription_id", new=AsyncMock(return_value=None)),
        patch("packages.core.services.chat_service.record_chat_runtime_learning", new=AsyncMock(return_value=[])),
        patch("packages.core.services.chat_service.schedule_learning_candidate_applies", new=AsyncMock()),
        patch("packages.core.services.chat_service.runtime_persist_chat_runtime_events", new=AsyncMock()),
    ):
        await run_chat_message(
            "hi", conversation_id,
            entity_id=entity_id, user_id=user_id, db=db_session,
        )

    assert callable(captured["before"])
    assert callable(captured["settled"])


# ── outcome threaded through record_tool_call ───────────────────────

@pytest.mark.asyncio
async def test_record_tool_call_persists_the_outcome_column(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.usage import ToolCallLog
    from packages.core.services.usage_service import record_tool_call
    from sqlalchemy import select

    entity_id = generate_ulid()
    await record_tool_call(
        db_session, entity_id=entity_id, tool_name="search_tools",
        success=True, error=None, outcome="empty_result",
    )
    await db_session.flush()

    row = (await db_session.execute(
        select(ToolCallLog).where(ToolCallLog.entity_id == entity_id)
    )).scalar_one()
    assert row.outcome == "empty_result"


@pytest.mark.asyncio
async def test_record_tool_call_defaults_outcome_to_success(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.usage import ToolCallLog
    from packages.core.services.usage_service import record_tool_call
    from sqlalchemy import select

    entity_id = generate_ulid()
    await record_tool_call(db_session, entity_id=entity_id, tool_name="list_tenants")
    await db_session.flush()

    row = (await db_session.execute(
        select(ToolCallLog).where(ToolCallLog.entity_id == entity_id)
    )).scalar_one()
    assert row.outcome == "success"


@pytest.mark.asyncio
async def test_log_tool_exec_threads_outcome_to_schedule_tool_call_log(monkeypatch):
    from packages.core.ai.chat_logger import ChatTrace

    captured = {}

    def fake_schedule(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "packages.core.ai.chat_logger.schedule_tool_call_log", fake_schedule,
    )

    trace = ChatTrace(entity_id="e1")
    trace.log_tool_exec(
        round_num=1, tool_name="search_tools", success=True, error=None,
        outcome="empty_result",
    )
    assert captured["outcome"] == "empty_result"


# ── rounds threaded through log_token_usage / record_llm_usage ─────


@pytest.mark.asyncio
async def test_log_token_usage_persists_rounds(db_session):
    from packages.core.services.usage_service import log_token_usage

    entry = await log_token_usage(
        db_session, entity_id="e1", model="gpt-5.5",
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        source="chat", rounds=3,
    )
    assert entry.rounds == 3


@pytest.mark.asyncio
async def test_record_chat_llm_usage_threads_rounds_through(db_session, monkeypatch):
    from sqlalchemy import select
    from packages.core.models.usage import TokenUsageLog
    from packages.core.services.usage_service import record_chat_llm_usage

    await record_chat_llm_usage(
        db_session, entity_id="e2", user_id="u1", agent_id=None,
        conversation_id="c1", usage={"total_tokens": 15, "model": "gpt-5.5"},
        rounds=4,
    )
    await db_session.flush()
    row = (await db_session.execute(
        select(TokenUsageLog).where(TokenUsageLog.entity_id == "e2")
    )).scalar_one()
    assert row.rounds == 4
