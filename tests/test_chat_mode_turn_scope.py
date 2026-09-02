from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.document import Document, VectorStatus
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_history import (
    ATTACHMENT_HISTORY_INSTRUCTION,
    COMPLETED_TOOL_ACTIVITY_FRAME,
    load_conversation_history,
)


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

    # The most recent assistant turn's tool trace is kept as reusable context,
    # but framed as completed so it is not replayed as active work.
    assert COMPLETED_TOOL_ACTIVITY_FRAME in history[1]["content"]
    assert "do not re-run" in history[1]["content"]
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

    # Kept (it is the latest assistant turn) but explicitly marked completed
    # so "继续" cannot be read as re-running the finished search.
    assert COMPLETED_TOOL_ACTIVITY_FRAME in history[0]["content"]
    assert "Meta careers Senior Software Engineer" in history[0]["content"]


@pytest.mark.asyncio
async def test_chat_history_does_not_lookup_inline_file_tokens_without_persisted_attachment(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conversation_id = "conv_inline_file_ref"
    doc_id = "doc_inline_video"
    entity_id = "entity_inline_file"
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=entity_id,
                user_id="user_inline_file",
                title="Inline file ref",
                channel="web",
                scope="channel",
            ),
            Document(
                id=doc_id,
                entity_id=entity_id,
                name="daily-stickman-video.mp4",
                fs_path="uploads/chat/daily-stickman-video.mp4",
                file_type="mp4",
                mime_type="video/mp4",
                source="chat_upload",
                vector_status=VectorStatus.READY,
            ),
            Message(
                id="msg_inline_file",
                conversation_id=conversation_id,
                role="user",
                content="使用Chrome将 #daily-stickman-video.mp4 上传到草稿箱",
                created_at=start,
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        conversation_id,
        latest_user_message="上传到youtube",
    )

    assert "[Attached file context]" not in history[0]["content"]
    assert f"document_id={doc_id}" not in history[0]["content"]
    assert "path=uploads/chat/daily-stickman-video.mp4" not in history[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("document_key", ["document_id", "id"])
async def test_chat_history_uses_persisted_attachment_refs_when_available(
    db_session,
    document_key,
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    suffix = "legacy" if document_key == "id" else "canon"
    conversation_id = f"conv_attach_{suffix}"
    doc_id = f"doc_attach_{suffix}"
    entity_id = f"ent_attach_{suffix}"
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=entity_id,
                user_id="user_attachment_refs",
                title="Attachment refs",
                channel="web",
                scope="channel",
            ),
            Document(
                id=doc_id,
                entity_id=entity_id,
                name="reference.pdf",
                fs_path="uploads/chat/reference.pdf",
                file_type="pdf",
                mime_type="application/pdf",
                source="chat_upload",
                vector_status=VectorStatus.READY,
            ),
            Message(
                id=f"msg_attach_{suffix}",
                conversation_id=conversation_id,
                role="user",
                content="请看这份文件",
                attachments=[
                    {
                        "name": "reference.pdf",
                        document_key: doc_id,
                        "type": "knowledge",
                    }
                ],
                created_at=start,
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(db_session, conversation_id)

    assert "[Attached file context]" in history[0]["content"]
    assert ATTACHMENT_HISTORY_INSTRUCTION in history[0]["content"]
    assert "reference.pdf" in history[0]["content"]
    assert f"document_id={doc_id}" in history[0]["content"]
    assert "path=uploads/chat/reference.pdf" in history[0]["content"]
    assert f"url=/api/v1/fs/{entity_id}/uploads/chat/reference.pdf" in history[0]["content"]


@pytest.mark.asyncio
async def test_chat_history_drops_cross_entity_attachment_even_with_persisted_path(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conversation_id = "conv_attach_cross_entity"
    document_id = "doc_attach_other_entity"
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id="entity_attachment_owner",
                user_id="user_attachment_owner",
                title="Attachment tenant boundary",
                channel="web",
                scope="channel",
                created_at=start,
            ),
            Document(
                id=document_id,
                entity_id="entity_attachment_other",
                name="private.pdf",
                fs_path="private/private.pdf",
                file_type="pdf",
                mime_type="application/pdf",
                source="chat_upload",
                vector_status=VectorStatus.READY,
            ),
            Message(
                id="msg_attach_cross_entity",
                conversation_id=conversation_id,
                role="user",
                content="请看这份文件",
                attachments=[
                    {
                        "name": "private.pdf",
                        "id": document_id,
                        "path": "private/private.pdf",
                        "type": "knowledge",
                    }
                ],
                created_at=start,
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(db_session, conversation_id)

    assert "[Attached file context]" not in history[0]["content"]
    assert document_id not in history[0]["content"]
    assert "private/private.pdf" not in history[0]["content"]


@pytest.mark.asyncio
async def test_chat_history_prefers_persisted_attachments_over_inline_name_lookup(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conversation_id = "conv_attach_precedence"
    entity_id = "entity_attach_precedence"
    attached_doc_id = "doc_attached_video"
    unrelated_doc_id = "doc_same_name_wrong"
    db_session.add_all(
        [
            Conversation(
                id=conversation_id,
                entity_id=entity_id,
                user_id="user_attach_precedence",
                title="Attachment precedence",
                channel="web",
                scope="channel",
            ),
            Document(
                id=attached_doc_id,
                entity_id=entity_id,
                name="daily-stickman-video.mp4",
                fs_path="Workspaces/right/daily-stickman-video.mp4",
                file_type="mp4",
                mime_type="video/mp4",
                source="ai_generated",
                vector_status=VectorStatus.READY,
                created_at=start,
            ),
            Document(
                id=unrelated_doc_id,
                entity_id=entity_id,
                name="daily-stickman-video.mp4",
                fs_path="Workspaces/wrong/daily-stickman-video.mp4",
                file_type="mp4",
                mime_type="video/mp4",
                source="ai_generated",
                vector_status=VectorStatus.READY,
                created_at=start + timedelta(seconds=1),
            ),
            Message(
                id="msg_attachment_precedence",
                conversation_id=conversation_id,
                role="user",
                content="使用Chrome将 #daily-stickman-video.mp4 上传到草稿箱",
                attachments=[
                    {
                        "kind": "knowledge_document",
                        "name": "daily-stickman-video.mp4",
                        "document_id": attached_doc_id,
                        "path": "Workspaces/right/daily-stickman-video.mp4",
                        "mime": "video/mp4",
                    }
                ],
                created_at=start + timedelta(seconds=2),
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        conversation_id,
        latest_user_message="上传到youtube",
    )

    assert f"document_id={attached_doc_id}" in history[0]["content"]
    assert "path=Workspaces/right/daily-stickman-video.mp4" in history[0]["content"]
    assert unrelated_doc_id not in history[0]["content"]
    assert "path=Workspaces/wrong/daily-stickman-video.mp4" not in history[0]["content"]


@pytest.mark.asyncio
async def test_older_tool_activity_is_still_dropped_on_new_turn(db_session):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            Message(
                id="msg_old_tool_assistant",
                conversation_id="conv_two_turn_scope",
                role="assistant",
                content="Searched jobs.",
                created_at=start,
                tool_calls=[
                    {
                        "name": "web_search",
                        "arguments": {"query": "old stale search"},
                        "result": "stale search result payload",
                    }
                ],
            ),
            Message(
                id="msg_mid_user",
                conversation_id="conv_two_turn_scope",
                role="user",
                content="thanks",
                created_at=start + timedelta(seconds=1),
            ),
            Message(
                id="msg_new_tool_assistant",
                conversation_id="conv_two_turn_scope",
                role="assistant",
                content="Listed the files.",
                created_at=start + timedelta(seconds=2),
                tool_calls=[
                    {
                        "name": "list_files",
                        "arguments": {"path": ""},
                        "result": "fresh file listing payload",
                    }
                ],
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        "conv_two_turn_scope",
        latest_user_message="move them into folders",
    )

    old_entry = next(h for h in history if "Searched jobs." in h["content"])
    new_entry = next(h for h in history if "Listed the files." in h["content"])
    assert "stale search result payload" not in old_entry["content"]
    assert COMPLETED_TOOL_ACTIVITY_FRAME in new_entry["content"]
