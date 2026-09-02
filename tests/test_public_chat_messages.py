"""Incremental history and explicit stream refresh share one visitor scope."""
from datetime import datetime, timedelta, timezone

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services.channel_conversations import list_public_webchat_messages


async def _conversation(db):
    conversation = Conversation(id=generate_ulid(), entity_id=generate_ulid(), channel="webchat")
    db.add(conversation)
    await db.flush()
    return conversation


def _message(conversation_id, *, session="visitor", **kwargs):
    return Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant",
        content="Reply", meta={"channel_type": "webchat", "chat_id": session}, **kwargs,
    )


async def test_poll_cursor_does_not_drop_messages_with_the_same_creation_time(db_session):
    conv = await _conversation(db_session)
    timestamp = datetime.now(timezone.utc)
    rows = [_message(conv.id, created_at=timestamp) for _ in range(3)]
    rows.sort(key=lambda row: row.id)
    db_session.add_all(rows)
    await db_session.flush()
    page = await list_public_webchat_messages(db_session, conv.id, session_id="visitor", limit=1)
    assert [message["id"] for message in page] == [rows[0].id]
    next_page = await list_public_webchat_messages(
        db_session, conv.id, session_id="visitor", after=page[-1]["id"],
    )
    assert [message["id"] for message in next_page] == [row.id for row in rows[1:]]


async def test_refresh_can_retrieve_a_completed_placeholder_behind_the_cursor(db_session):
    conv = await _conversation(db_session)
    timestamp = datetime.now(timezone.utc)
    placeholder = _message(conv.id, created_at=timestamp)
    placeholder.meta = {**placeholder.meta, "stream_status": "running"}
    later = _message(conv.id, created_at=timestamp + timedelta(seconds=1))
    db_session.add_all([placeholder, later])
    await db_session.flush()
    page = await list_public_webchat_messages(db_session, conv.id, session_id="visitor")
    assert page[0]["stream_status"] == "running"
    placeholder.content = "The completed reply"
    placeholder.meta = {**placeholder.meta, "stream_status": "completed"}
    await db_session.flush()

    assert await list_public_webchat_messages(db_session, conv.id, session_id="visitor", after=later.id) == []
    updates = await list_public_webchat_messages(
        db_session, conv.id, session_id="visitor", message_ids=[placeholder.id],
    )
    assert [(message["content"], message["stream_status"]) for message in updates] == [
        ("The completed reply", "completed"),
    ]


async def test_refresh_never_returns_other_conversations_visitors_or_internal_messages(db_session):
    conv = await _conversation(db_session)
    other_conv = await _conversation(db_session)
    own = _message(conv.id)
    other_visitor = _message(conv.id, session="another-visitor")
    other_conversation = _message(other_conv.id)
    internal = _message(conv.id)
    internal.meta = {}
    rows = [own, other_visitor, other_conversation, internal]
    db_session.add_all(rows)
    await db_session.flush()
    updates = await list_public_webchat_messages(
        db_session, conv.id, session_id="visitor", message_ids=[row.id for row in rows],
    )
    assert [message["id"] for message in updates] == [own.id]


async def test_foreign_cursor_does_not_control_this_visitors_history(db_session):
    conv = await _conversation(db_session)
    timestamp = datetime.now(timezone.utc)
    own = _message(conv.id, created_at=timestamp)
    other_visitor = _message(conv.id, session="another-visitor", created_at=timestamp + timedelta(days=1))
    db_session.add_all([own, other_visitor])
    await db_session.flush()
    page = await list_public_webchat_messages(db_session, conv.id, session_id="visitor", after=other_visitor.id)
    assert [message["id"] for message in page] == [own.id]


async def test_client_turn_id_survives_checkpoints_and_final_reply(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from packages.core import database
    from packages.core.services.conversation_messages import save_or_update_assistant_stream_message

    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    conv = await _conversation(db_session)
    user = _message(conv.id)
    user.role = "user"
    user.meta = {**user.meta, "client_turn_id": "browser-turn"}
    reply = _message(conv.id)
    reply.meta = {**reply.meta, "client_turn_id": "browser-turn", "stream_status": "running"}
    db_session.add_all([user, reply])
    await db_session.commit()
    conv_id, entity_id, reply_id = conv.id, conv.entity_id, reply.id

    for content, stream_status in (("Partial", "streaming"), ("Recovered answer", "completed")):
        assert await save_or_update_assistant_stream_message(
            conversation_id=conv_id, entity_id=entity_id, workspace_id=None, agent_id=None,
            message_id=reply_id, content=content, meta={"stream_status": stream_status},
        ) == reply_id
        db_session.expire_all()
        messages = await list_public_webchat_messages(db_session, conv_id, session_id="visitor")
        assert len(messages) == 2
        assert {message["client_turn_id"] for message in messages} == {"browser-turn"}
        refreshed = await list_public_webchat_messages(
            db_session, conv_id, session_id="visitor", message_ids=[reply_id],
        )
        assert [(m["id"], m["content"], m["stream_status"], m["client_turn_id"]) for m in refreshed] == [
            (reply_id, content, stream_status, "browser-turn"),
        ]
