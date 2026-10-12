from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from packages.core.models.task import Message
from packages.core.services.conversation_history import (
    COMPLETED_TOOL_ACTIVITY_FRAME,
    FAILED_ASSISTANT_TURN_HISTORY,
    UNVERIFIED_TOOL_ACTIVITY_FRAME,
    load_conversation_history,
)


def _tool_message(
    message_id: str,
    conversation_id: str,
    created_at: datetime,
    result: str,
    *,
    failed: bool = False,
) -> Message:
    return Message(
        id=message_id,
        conversation_id=conversation_id,
        role="assistant",
        content="I inspected the current browser state.",
        created_at=created_at,
        meta={"stream_status": "error", "error": "tool failed"} if failed else {},
        tool_calls=[
            {
                "name": "mcp__chrome__observe",
                "arguments": {},
                "result": result,
                "status": "error" if failed else "success",
            }
        ],
    )


@pytest.mark.asyncio
async def test_recent_tool_observation_survives_a_later_plain_assistant_reply(db_session):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    conversation_id = "conv_obs_plain_reply"
    db_session.add_all(
        [
            _tool_message(
                "msg_observe",
                conversation_id,
                start,
                "Active tab: Project board. Selected object: Launch checklist.",
            ),
            Message(
                id="msg_plain_reply",
                conversation_id=conversation_id,
                role="assistant",
                content="I found the project board and am ready for the next step.",
                created_at=start + timedelta(seconds=1),
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        conversation_id,
        latest_user_message="What is the current status?",
    )

    observation_entry = next(
        entry for entry in history if "Active tab: Project board" in entry["content"]
    )
    assert COMPLETED_TOOL_ACTIVITY_FRAME in observation_entry["content"]
    assert "Selected object: Launch checklist" in observation_entry["content"]


@pytest.mark.asyncio
async def test_recent_tool_observations_are_bounded_and_failed_entries_stay_unverified(db_session):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    conversation_id = "conv_bound_obs"
    db_session.add_all(
        [
            _tool_message("msg_observe_1", conversation_id, start, "observation-one"),
            _tool_message(
                "msg_observe_2",
                conversation_id,
                start + timedelta(seconds=1),
                "observation-two",
            ),
            _tool_message(
                "msg_observe_3",
                conversation_id,
                start + timedelta(seconds=2),
                "observation-three",
            ),
            _tool_message(
                "msg_observe_4",
                conversation_id,
                start + timedelta(seconds=3),
                "failed-observation-four",
                failed=True,
            ),
            Message(
                id="msg_plain_after_obs",
                conversation_id=conversation_id,
                role="assistant",
                content="I have finished checking the page.",
                created_at=start + timedelta(seconds=4),
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        conversation_id,
        latest_user_message="Continue with the selected object.",
    )
    contents = "\n".join(entry["content"] for entry in history)

    assert "observation-one" not in contents
    assert "observation-two" in contents
    assert "observation-three" in contents
    assert "failed-observation-four" in contents
    assert FAILED_ASSISTANT_TURN_HISTORY in contents
    assert contents.count(COMPLETED_TOOL_ACTIVITY_FRAME) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "tool_call", "expected_frame"),
    [
        ("queued", {"result": json.dumps({"status": "queued"}), "status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("partial", {"result": json.dumps({"status": "partial"}), "status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("interrupted", {"result": json.dumps({"status": "interrupted"}), "status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("hitl", {"result": json.dumps({"__hitl__": True, "approvalId": "approval_1"}), "status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("error", {"result": json.dumps({"error": "browser request failed"}), "status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("unknown-preview", {"result": "unknown browser observation", "status": "unknown"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("pending-preview", {"result": "pending browser observation", "status": "pending"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("missing", {"status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("empty", {"result": "", "status": "success"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("ui-unverified", {"result": "public preview", "raw_result": json.dumps({"status": "completed", "tab": "Project"}), "status": "unverified"}, UNVERIFIED_TOOL_ACTIVITY_FRAME),
        ("raw-success", {"result": "public preview", "raw_result": json.dumps({"status": "completed", "tab": "Project"}), "status": "success"}, COMPLETED_TOOL_ACTIVITY_FRAME),
    ],
    ids=["queued", "partial", "interrupted", "hitl", "error", "unknown-preview", "pending-preview", "missing", "empty", "ui-unverified", "raw-success"],
)
async def test_tool_activity_frame_uses_raw_outcome_evidence(
    db_session,
    case,
    tool_call,
    expected_frame,
):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    conversation_id = f"conv_frame_{case}"
    db_session.add(
        Message(
            id=f"msg_frame_{case}",
            conversation_id=conversation_id,
            role="assistant",
            content="I started inspecting the browser.",
            created_at=start,
            tool_calls=[
                {
                    "name": "mcp__chrome__observe",
                    "arguments": {},
                    **tool_call,
                }
            ],
        )
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        conversation_id,
        latest_user_message="Continue from the current tab.",
    )

    assert expected_frame in history[0]["content"]
    if expected_frame == UNVERIFIED_TOOL_ACTIVITY_FRAME:
        assert COMPLETED_TOOL_ACTIVITY_FRAME not in history[0]["content"]
