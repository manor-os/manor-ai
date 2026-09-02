from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from packages.core.models.task import Conversation, Message
from packages.core.services.voice.work_queue import (
    VOICE_WORK_META_KEY,
    VoiceWorkReceipt,
    admit_voice_work,
    claim_voice_work,
    finish_voice_work,
    interrupt_voice_work,
    recover_voice_work,
    voice_work_context,
    voice_work_was_interrupted,
)


async def _conversation(db_session, *, channel: str = "web") -> Conversation:
    conversation = Conversation(
        entity_id="entity",
        user_id="user",
        title="Voice test",
        channel=channel,
    )
    db_session.add(conversation)
    await db_session.commit()
    return conversation


async def test_voice_work_uses_one_visible_message_for_queue_and_chat_origin(db_session):
    conversation = await _conversation(db_session)

    receipt = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Run the report",
        user_id="user",
        public_channel=False,
    )
    message = await db_session.get(Message, receipt.message_id)
    assert message.content == "Run the report"
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "pending"

    claimed = await claim_voice_work(
        db_session,
        receipt,
        conversation_id=conversation.id,
    )
    assert claimed.id == message.id
    assert claimed.meta[VOICE_WORK_META_KEY]["state"] == "running"

    await finish_voice_work(
        db_session,
        receipt,
        conversation_id=conversation.id,
        state="completed",
    )
    await db_session.refresh(message)
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "completed"
    assert len(
        (
            await db_session.execute(
                select(Message).where(Message.conversation_id == conversation.id)
            )
        ).scalars().all()
    ) == 1


async def test_recovery_requeues_pending_but_never_replays_running_work(db_session):
    conversation = await _conversation(db_session)
    pending = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Pending instruction",
        user_id="user",
        public_channel=False,
    )
    running = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Possibly executed instruction",
        user_id="user",
        public_channel=False,
    )
    await claim_voice_work(
        db_session,
        running,
        conversation_id=conversation.id,
    )

    recovered = await recover_voice_work(
        db_session,
        conversation_id=conversation.id,
    )

    assert recovered == [
        VoiceWorkReceipt(
            pending.id,
            pending.message_id,
            pending.text,
            recovered=True,
        )
    ]
    running_message = await db_session.get(Message, running.message_id)
    assert running_message.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"


async def test_public_voice_receipt_retains_webchat_visibility_metadata(db_session):
    conversation = await _conversation(db_session, channel="webchat")
    conversation.meta = {
        "sender_id": "visitor",
        "sender_name": "Visitor",
        "chat_id": "visitor",
    }
    await db_session.commit()

    receipt = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Hello",
        user_id=None,
        public_channel=True,
    )

    message = await db_session.get(Message, receipt.message_id)
    assert message.meta["channel_type"] == "webchat"
    assert message.meta["sender_id"] == "visitor"
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "pending"


async def test_recovery_expires_stale_pending_voice_work(db_session):
    conversation = await _conversation(db_session)
    receipt = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Old instruction",
        user_id="user",
        public_channel=False,
    )
    message = await db_session.get(Message, receipt.message_id)
    message.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    await db_session.commit()

    assert await recover_voice_work(
        db_session,
        conversation_id=conversation.id,
    ) == []
    await db_session.refresh(message)
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"


async def test_recovery_finds_pending_work_buried_by_regular_chat(db_session):
    conversation = await _conversation(db_session)
    receipt = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Run after restart",
        user_id="user",
        public_channel=False,
    )
    db_session.add_all(
        Message(
            conversation_id=conversation.id,
            role="user",
            content=f"Regular message {index}",
        )
        for index in range(40)
    )
    await db_session.commit()

    assert await recover_voice_work(
        db_session,
        conversation_id=conversation.id,
    ) == [
        VoiceWorkReceipt(
            receipt.id,
            receipt.message_id,
            receipt.text,
            recovered=True,
        )
    ]


async def test_voice_work_context_excludes_control_replies_and_interrupts_once(db_session):
    conversation = await _conversation(db_session)
    receipt = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Inspect the staging logs",
        user_id="user",
        public_channel=False,
    )
    await claim_voice_work(db_session, receipt, conversation_id=conversation.id)
    db_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content="Still working",
                meta={"voice_control": True},
            ),
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content="Found a WebSocket timeout",
            ),
        ]
    )
    await db_session.commit()

    context = await voice_work_context(
        db_session,
        receipt,
        conversation_id=conversation.id,
    )
    replacement = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="Email Alice instead",
        user_id="user",
        public_channel=False,
    )
    changed = await interrupt_voice_work(
        db_session,
        receipt,
        conversation_id=conversation.id,
        reason="Replaced by voice",
        superseded_by=replacement.id,
    )

    assert context.request == "Inspect the staging logs"
    assert context.assistant_output == "Found a WebSocket timeout"
    assert changed is True
    message = await db_session.get(Message, receipt.message_id)
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"
    assert message.meta[VOICE_WORK_META_KEY]["superseded_by"] == replacement.id
    assert await voice_work_was_interrupted(
        db_session,
        message_id=receipt.message_id,
        conversation_id=conversation.id,
    ) is True
    assert await interrupt_voice_work(
        db_session,
        receipt,
        conversation_id=conversation.id,
        reason="Cancel the queued replacement",
    ) is True
    assert await voice_work_was_interrupted(
        db_session,
        message_id=replacement.message_id,
        conversation_id=conversation.id,
    ) is True
    sibling = await admit_voice_work(
        db_session,
        conversation_id=conversation.id,
        text="A separate text or voice turn",
        user_id="user",
        public_channel=False,
    )
    assert await voice_work_was_interrupted(
        db_session,
        message_id=sibling.message_id,
        conversation_id=conversation.id,
    ) is False
    assert await interrupt_voice_work(
        db_session,
        receipt,
        conversation_id=conversation.id,
        reason="Duplicate",
    ) is False
