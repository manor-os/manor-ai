from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.document import Document, VectorStatus
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_history import (
    ATTACHMENT_HISTORY_INSTRUCTION,
    COMPLETED_TOOL_ACTIVITY_FRAME,
    FAILED_ASSISTANT_TURN_HISTORY,
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
    # but framed as historical data so it is not replayed as active work.
    assert COMPLETED_TOOL_ACTIVITY_FRAME in history[1]["content"]
    assert "Do not replay past side effects" in history[1]["content"]
    assert history[-1]["content"] == "Writ a 5 pages hotel industry growth ppt / Slides"


@pytest.mark.asyncio
async def test_failed_workspace_tool_turn_is_not_replayed_as_completed(db_session):
    db_session.add(
        Message(
            id="msg_failed_ws_title",
            conversation_id="conv_failed_ws_title",
            role="assistant",
            content="已完成。",
            meta={
                "error": "stateful_tool_call_failed",
                "stop_reason": "error",
                "stream_status": "error",
            },
            tool_calls=[{
                "name": "manor",
                "arguments": {
                    "action": "workspace",
                    "params": {"action": "create_task", "params": {"name": "Production plans"}},
                },
                "result": '{"error":"title is required"}',
                "status": "error",
            }],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        "conv_failed_ws_title",
        latest_user_message="继续",
    )

    assert FAILED_ASSISTANT_TURN_HISTORY in history[0]["content"]
    assert "title is required" in history[0]["content"]
    assert "已完成。" not in history[0]["content"]
    assert COMPLETED_TOOL_ACTIVITY_FRAME not in history[0]["content"]


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

    runtime_metadata = {}
    history = await load_conversation_history(
        db_session, conversation_id, runtime_metadata=runtime_metadata,
    )

    assert "[Attached file context]" in history[0]["content"]
    assert ATTACHMENT_HISTORY_INSTRUCTION in history[0]["content"]
    assert "reference.pdf" in history[0]["content"]
    assert f"document_id={doc_id}" in history[0]["content"]
    assert "path=uploads/chat/reference.pdf" in history[0]["content"]
    assert f"url=/api/v1/fs/{entity_id}/uploads/chat/reference.pdf" in history[0]["content"]
    refs = runtime_metadata["runtime_history_attachment_refs"]
    assert refs[0]["document_id"] == doc_id
    assert refs[0]["path"] == "uploads/chat/reference.pdf"


@pytest.mark.asyncio
async def test_chat_history_resolves_generated_media_with_only_fs_path(db_session):
    conversation_id = "conv_generated_media_path"
    entity_id = "ent_generated_media_path"
    media_path = "Videos/ad/visuals/shot-01.png"
    db_session.add_all([
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id="user_generated_media_path",
            title="Generated media history",
            channel="web",
            scope="channel",
        ),
        Document(
            id="doc_generated_media_path",
            entity_id=entity_id,
            name="shot-01.png",
            fs_path=media_path,
            file_type="png",
            mime_type="image/png",
            source="ai_generated",
            vector_status=VectorStatus.READY,
        ),
        Message(
            id="msg_generated_media_path",
            conversation_id=conversation_id,
            role="assistant",
            content="The shot is ready.",
            attachments=[
                {"name": "shot-01.png", "type": "knowledge", "fs_path": media_path},
                {"name": "stale.png", "type": "knowledge", "fs_path": "Videos/ad/visuals/stale.png"},
            ],
        ),
    ])
    await db_session.commit()

    runtime_metadata = {}
    await load_conversation_history(
        db_session, conversation_id, runtime_metadata=runtime_metadata,
    )

    assert runtime_metadata["runtime_history_attachment_refs"] == [{
        "kind": "knowledge_document",
        "name": "shot-01.png",
        "document_id": "doc_generated_media_path",
        "path": media_path,
        "url": f"/api/v1/fs/{entity_id}/{media_path}",
        "mime": "image/png",
        "file_type": "png",
    }]


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

    runtime_metadata = {}
    history = await load_conversation_history(
        db_session, conversation_id, runtime_metadata=runtime_metadata,
    )

    assert "[Attached file context]" not in history[0]["content"]
    assert document_id not in history[0]["content"]
    assert "private/private.pdf" not in history[0]["content"]
    assert runtime_metadata.get("runtime_history_attachment_refs", []) == []


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
async def test_new_turn_keeps_three_recent_tool_observations_after_plain_reply(db_session):
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
                id="msg_recent_tool_two",
                conversation_id="conv_two_turn_scope",
                role="assistant",
                content="Observed the second tab.",
                created_at=start + timedelta(seconds=1),
                tool_calls=[
                    {
                        "name": "mcp__chrome__observe",
                        "arguments": {},
                        "result": "second recent tab observation",
                    }
                ],
            ),
            Message(
                id="msg_recent_tool_three",
                conversation_id="conv_two_turn_scope",
                role="assistant",
                content="Observed the third tab.",
                created_at=start + timedelta(seconds=2),
                tool_calls=[
                    {
                        "name": "mcp__chrome__observe",
                        "arguments": {"path": ""},
                        "result": "third recent tab observation",
                    }
                ],
            ),
            Message(
                id="msg_recent_tool_four",
                conversation_id="conv_two_turn_scope",
                role="assistant",
                content="Observed the selected object.",
                created_at=start + timedelta(seconds=3),
                tool_calls=[
                    {
                        "name": "mcp__chrome__observe",
                        "arguments": {"path": ""},
                        "result": "latest selected object observation",
                    }
                ],
            ),
            Message(
                id="msg_plain_after_tools",
                conversation_id="conv_two_turn_scope",
                role="assistant",
                content="I have finished checking the current page.",
                created_at=start + timedelta(seconds=4),
            ),
        ]
    )
    await db_session.commit()

    history = await load_conversation_history(
        db_session,
        "conv_two_turn_scope",
        latest_user_message="move them into folders",
    )

    contents = "\n".join(entry["content"] for entry in history)
    assert "stale search result payload" not in contents
    assert "second recent tab observation" in contents
    assert "third recent tab observation" in contents
    assert "latest selected object observation" in contents
    assert contents.count(COMPLETED_TOOL_ACTIVITY_FRAME) == 3
