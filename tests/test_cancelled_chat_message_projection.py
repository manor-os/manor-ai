from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.models.runtime_run import RuntimeRun
from packages.core.models.base import generate_ulid
from packages.core.models.task import Message
from packages.core.services.conversation_messages import assistant_stream_started_content
from packages.core.services.runtime_message_projection import (
    cancelled_runtime_message_snapshot,
    cancelled_runtime_message_snapshots,
)


def stale_message(**overrides):
    return Message(**{
        "id": "message-cancelled", "conversation_id": "conversation-cancelled",
        "role": "assistant", "content": assistant_stream_started_content(),
        "meta": {"stream_status": "running", "origin_user_message_id": "question"},
        "created_at": datetime.now(timezone.utc), **overrides,
    })


def cancelled_run(**overrides):
    return RuntimeRun(**{
        "id": "run-cancelled", "root_run_id": "run-cancelled",
        "entity_id": "entity-owner", "user_id": "user-owner",
        "conversation_id": "conversation-cancelled", "assistant_message_id": "message-cancelled",
        "status": "cancelled", **overrides,
    })


def persisted_cancelled_pair(**run_overrides):
    """Committed integration rows must not reuse another test's identities."""
    message = stale_message(id=generate_ulid(), conversation_id=generate_ulid())
    run_id = generate_ulid()
    run = cancelled_run(
        id=run_id, root_run_id=run_id, conversation_id=message.conversation_id,
        assistant_message_id=message.id, **run_overrides,
    )
    return message, run


@pytest.mark.unit
def test_cancelled_placeholder_is_terminal_without_mutating_history_row():
    message = stale_message()
    snapshot = cancelled_runtime_message_snapshot(message, cancelled_run())
    assert snapshot.meta["stream_status"] == "interrupted"
    assert snapshot.meta["stop_reason"] == "cancelled"
    assert snapshot.meta["origin_user_message_id"] == "question"
    assert "still working" not in snapshot.content
    assert message.meta["stream_status"] == "running"
    assert message.content == assistant_stream_started_content()


@pytest.mark.unit
def test_cancellation_preserves_partial_reply_files_usage_and_tool_history():
    message = stale_message(
        content="已生成第一张图片。", attachments=[{"id": "image"}],
        tool_calls=[{"name": "read_file", "status": "success"}],
        token_usage={"total_tokens": 100},
        meta={"stream_status": "streaming", "assistant_blocks": [{
            "id": "process", "type": "process", "lifecycle_status": "running",
            "started_at_ms": 1000, "ended_at_ms": 3000, "duration_ms": 2000,
            "steps": [{"id": "read", "name": "read_file", "status": "success"}],
        }]},
    )
    snapshot = cancelled_runtime_message_snapshot(message, cancelled_run())
    assert snapshot.content.startswith(message.content)
    assert snapshot.attachments == message.attachments
    assert snapshot.tool_calls == message.tool_calls
    assert snapshot.token_usage == message.token_usage
    process = snapshot.meta["assistant_blocks"][0]
    assert process["lifecycle_status"] == "cancelled"
    assert process["steps"][0]["status"] == "success"
    assert process["duration_ms"] == 2000
    assert message.meta["assistant_blocks"][0]["lifecycle_status"] == "running"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["queued", "running", "waiting_resource", "resuming", "cancel_requested", "cancelling", "completed", "failed"])
def test_projection_never_reports_non_cancelled_run_as_stopped(status):
    message = stale_message()
    assert cancelled_runtime_message_snapshot(message, cancelled_run(status=status)) is message


@pytest.mark.unit
@pytest.mark.parametrize("overrides", [
    {"assistant_message_id": "another-message"}, {"conversation_id": "another-conversation"},
])
def test_projection_requires_exact_message_and_conversation_identity(overrides):
    message = stale_message()
    assert cancelled_runtime_message_snapshot(message, cancelled_run(**overrides)) is message


@pytest.mark.unit
def test_finished_reply_is_preserved_even_if_a_cancel_arrives_later():
    message = stale_message(content="测试完成", meta={"stream_status": "completed"})
    assert cancelled_runtime_message_snapshot(message, cancelled_run()) is message


@pytest.mark.unit
async def test_history_recovery_is_read_only_and_uses_one_scoped_query():
    message = stale_message()
    final = stale_message(id="final", content="完成", meta={})
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [cancelled_run()]))
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    snapshots = await cancelled_runtime_message_snapshots(db, [message, final], entity_id="entity-owner")
    assert snapshots[0].meta["stream_status"] == "interrupted"
    assert snapshots[1] is final
    assert message.meta["stream_status"] == "running"
    db.execute.assert_awaited_once()
    assert "entity-owner" in db.execute.call_args.args[0].compile().params.values()


@pytest.mark.unit
async def test_finished_history_does_not_query_runtime_runs():
    db = SimpleNamespace(execute=AsyncMock())
    messages = [stale_message(content="完成", meta={})]
    assert await cancelled_runtime_message_snapshots(db, messages, entity_id="owner") is messages
    db.execute.assert_not_awaited()


@pytest.mark.integration
async def test_cancel_persists_message_terminal_atomically_and_is_idempotent(db_session):
    from packages.core.services.runtime_run_service import request_runtime_run_cancel

    message, run = persisted_cancelled_pair(status="queued")
    db_session.add_all([message, run])
    await db_session.flush()
    await request_runtime_run_cancel(
        db_session, run_id=run.id, entity_id=run.entity_id, user_id=run.user_id,
    )
    await db_session.commit()
    await db_session.refresh(message)
    terminal_content = message.content
    assert message.meta["stream_status"] == "interrupted"
    assert run.status == "cancelled"
    assert "still working" not in terminal_content
    await request_runtime_run_cancel(
        db_session, run_id=run.id, entity_id=run.entity_id, user_id=run.user_id,
    )
    await db_session.commit()
    await db_session.refresh(message)
    assert message.content == terminal_content


@pytest.mark.integration
async def test_history_recovers_legacy_cancellation_without_writes_or_cross_entity_projection(db_session):
    message, run = persisted_cancelled_pair()
    other = stale_message(id=generate_ulid(), conversation_id=message.conversation_id)
    other_run_id = generate_ulid()
    db_session.add_all([
        message, other, run,
        cancelled_run(
            id=other_run_id, root_run_id=other_run_id, entity_id="another-entity",
            conversation_id=other.conversation_id, assistant_message_id=other.id,
        ),
    ])
    await db_session.flush()
    snapshots = await cancelled_runtime_message_snapshots(db_session, [message, other], entity_id="entity-owner")
    assert snapshots[0].meta["stream_status"] == "interrupted"
    assert snapshots[1] is other
    await db_session.commit()
    await db_session.refresh(message)
    assert message.meta["stream_status"] == "running"
