from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.task import Message
from packages.core.services.conversation_history import load_conversation_history


@pytest.mark.asyncio
async def test_chat_mode_markers_are_not_reused_as_runtime_history(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            Message(
                id="msg_mode_user",
                conversation_id="conv_turn_scope",
                role="user",
                content="Generate a product video.\n[Mode: video]",
                created_at=start,
            ),
            Message(
                id="msg_mode_assistant",
                conversation_id="conv_turn_scope",
                role="assistant",
                content="Started the video generation.",
                created_at=start + timedelta(seconds=1),
                tool_calls=[
                    {
                        "name": "generate_file",
                        "arguments": {"kind": "video", "prompt": "Generate a product video."},
                        "result": '{"status":"completed","kind":"video"}',
                    }
                ],
            ),
            Message(
                id="msg_next_user",
                conversation_id="conv_turn_scope",
                role="user",
                content="Now make a PPT about the same product.",
                created_at=start + timedelta(seconds=2),
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(db_session, "conv_turn_scope")

    assert history[0]["content"] == "Generate a product video."
    assert "[Mode: video]" not in history[0]["content"]
    assert "generate_file" not in history[1]["content"]
    assert "kind" not in history[1]["content"]
    assert history[-1]["content"] == "Now make a PPT about the same product."


@pytest.mark.asyncio
async def test_new_hotel_ppt_request_does_not_replay_previous_job_search_tools(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            Message(
                id="msg_job_user",
                conversation_id="conv_ppt_scope",
                role="user",
                content="Find Bay Area Senior SDE jobs.",
                created_at=start,
            ),
            Message(
                id="msg_job_assistant",
                conversation_id="conv_ppt_scope",
                role="assistant",
                content="I found several roles.",
                created_at=start + timedelta(seconds=1),
                tool_calls=[
                    {
                        "name": "web_search",
                        "arguments": {"query": "Meta Senior Software Engineer Bay Area jobs"},
                        "result": "Meta careers Senior Software Engineer, Apple SDE, Google jobs",
                    }
                ],
            ),
            Message(
                id="msg_ppt_user",
                conversation_id="conv_ppt_scope",
                role="user",
                content="Writ a 5 pages hotel industry growth ppt / Slides",
                created_at=start + timedelta(seconds=2),
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        "conv_ppt_scope",
        latest_user_message="Writ a 5 pages hotel industry growth ppt / Slides",
    )

    assert "Previous tool activity" not in history[1]["content"]
    assert "Meta Senior Software Engineer" not in history[1]["content"]
    assert history[-1]["content"] == "Writ a 5 pages hotel industry growth ppt / Slides"


@pytest.mark.asyncio
async def test_tool_activity_can_replay_when_no_new_user_turn_is_active(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add(
        Message(
            id="msg_same_turn_tool",
            conversation_id="conv_same_turn",
            role="assistant",
            content="I checked the source.",
            created_at=start,
            tool_calls=[
                {
                    "name": "web_search",
                    "arguments": {"query": "hotel industry growth statistics"},
                    "result": "Hotel demand recovered with sustained RevPAR growth.",
                }
            ],
        )
    )
    await db_session.commit()

    history = await load_conversation_history(db_session, "conv_same_turn")

    assert "Previous tool activity" in history[0]["content"]
    assert "RevPAR growth" in history[0]["content"]


@pytest.mark.asyncio
async def test_new_turn_does_not_replay_previous_tool_activity_even_for_continue_text(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            Message(
                id="msg_continue_assistant",
                conversation_id="conv_continue_scope",
                role="assistant",
                content="I found several roles.",
                created_at=start,
                tool_calls=[
                    {
                        "name": "web_search",
                        "arguments": {"query": "Meta Senior Software Engineer Bay Area jobs"},
                        "result": "Meta careers Senior Software Engineer",
                    }
                ],
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        "conv_continue_scope",
        latest_user_message="继续上次的 job search",
    )

    assert "Previous tool activity" not in history[0]["content"]
    assert "Meta careers Senior Software Engineer" not in history[0]["content"]
