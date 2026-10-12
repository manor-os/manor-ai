"""Durable multi-VAD sources keep original Chat rows and one execution receipt."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import chat_voice
from apps.api.chat_audio import ChatAudioScope
from packages.core.models.task import Conversation, Message
from packages.core.services.voice.work_queue import (
    VOICE_WORK_META_KEY,
    VoiceWorkNotPendingError,
    admit_voice_work,
    claim_voice_work,
    finish_voice_work,
    interrupt_voice_work,
    recover_voice_work,
    validate_voice_origin_message,
    voice_work_context,
)


async def sources(db, texts=("查一下西雅图的", "独立 studio，预算一千以内"), *, public=False):
    conversation = Conversation(entity_id="entity", user_id="owner", channel="webchat" if public else "web",
                                meta={"sender_id": "visitor", "chat_id": "visitor"} if public else {})
    db.add(conversation)
    await db.flush()
    now = datetime.now(timezone.utc)
    rows = [Message(
        conversation_id=conversation.id, role="user", content=text,
        created_at=now + timedelta(microseconds=index),
        meta={"voice_control": True, **({"channel_type": "webchat", "sender_id": "visitor", "chat_id": "visitor"}
                                       if public else {"author_user_id": "owner"})},
    ) for index, text in enumerate(texts)]
    db.add_all(rows)
    await db.commit()
    return conversation, rows


async def admit(db, conversation, rows, *, text=None, public=False, scope_id=None):
    return await admit_voice_work(
        db, conversation_id=conversation.id, text=text if text is not None else "\n".join(row.content for row in rows),
        user_id=None if public else "owner", public_channel=public, scope_id=scope_id,
        source_message_ids=tuple(row.id for row in rows),
    )


@pytest.mark.parametrize("public", [False, True])
async def test_promotion_keeps_original_rows_and_reuses_one_durable_work(db_session, public):
    conversation, rows = await sources(db_session, public=public)
    original = [(row.id, row.content, dict(row.meta)) for row in rows]
    receipt = await admit(db_session, conversation, rows, public=public)
    retry = await admit(db_session, conversation, rows, public=public)
    assert retry == receipt
    assert receipt.message_id == rows[-1].id
    assert receipt.text == "\n".join(row.content for row in rows)
    saved = list((await db_session.scalars(select(Message).where(Message.conversation_id == conversation.id)
                                         .order_by(Message.created_at, Message.id))).all())
    assert [(row.id, row.content) for row in saved] == [(item[0], item[1]) for item in original]
    assert saved[0].meta["voice_work_source_id"] == receipt.id
    assert saved[-1].meta[VOICE_WORK_META_KEY]["source_message_ids"] == [row.id for row in rows]
    for row, (_, _, meta) in zip(saved, original):
        assert all(row.meta[key] == value for key, value in meta.items())
    assert (await recover_voice_work(db_session, conversation_id=conversation.id))[0].text == receipt.text
    assert (await voice_work_context(db_session, receipt, conversation_id=conversation.id)).request == receipt.text
    anchor = await claim_voice_work(db_session, receipt, conversation_id=conversation.id)
    assert validate_voice_origin_message(anchor, conversation_id=conversation.id, content=receipt.text) is anchor
    with pytest.raises(RuntimeError, match="origin"):
        validate_voice_origin_message(anchor, conversation_id=conversation.id, content=anchor.content)


async def test_capture_order_is_preserved_when_asr_persisted_in_reverse_order(db_session):
    conversation, rows = await sources(db_session, texts=("second capture", "first capture"))
    receipt = await admit(db_session, conversation, list(reversed(rows)))
    assert receipt.text == "first capture\nsecond capture"
    assert receipt.message_id == rows[-1].id  # Most recent DB row anchors output history.
    assert rows[-1].meta[VOICE_WORK_META_KEY]["source_message_ids"] == [rows[1].id, rows[0].id]


async def test_source_retry_refreshes_previously_loaded_rows_before_idempotence_check(db_session):
    conversation, rows = await sources(db_session)
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as other:
        cached = list((await other.scalars(select(Message).where(Message.conversation_id == conversation.id))).all())
        assert all(VOICE_WORK_META_KEY not in row.meta for row in cached)
        first = await admit(db_session, conversation, rows)
        replay = await admit(other, conversation, rows)
        assert replay == first


async def test_source_bound_accepts_32_and_combination_omits_empty_parts(db_session):
    conversation, rows = await sources(db_session, texts=tuple("" if index == 15 else str(index) for index in range(32)))
    text = "\n".join(row.content for row in rows if row.content)
    receipt = await admit(db_session, conversation, rows, text=text)
    assert receipt.text == text
    assert len(rows[-1].meta[VOICE_WORK_META_KEY]["source_message_ids"]) == 32


@pytest.mark.parametrize("mutation", ["different_author", "unattributed", "not_voice", "assistant", "foreign", "missing"])
async def test_source_admission_rejects_unowned_or_unrelated_rows(db_session, mutation):
    conversation, rows = await sources(db_session)
    if mutation == "different_author":
        rows[0].meta = {**rows[0].meta, "author_user_id": "other-owner"}
    elif mutation == "unattributed":
        rows[0].meta = {"voice_control": True}
    elif mutation == "not_voice":
        rows[0].meta = {"author_user_id": "owner"}
    elif mutation == "assistant":
        rows[0].role = "assistant"
    elif mutation == "foreign":
        other, _ = await sources(db_session, texts=())
        rows[0].conversation_id = other.id
    else:
        await db_session.delete(rows[0])
    await db_session.commit()
    with pytest.raises(ValueError):
        await admit(db_session, conversation, rows)
    assert VOICE_WORK_META_KEY not in rows[-1].meta


@pytest.mark.parametrize("field,value", [
    ("sender_id", "another-visitor"), ("chat_id", "another-session"),
    ("channel_type", "email"), ("voice_control", False),
])
async def test_public_sources_require_same_authorized_visitor_metadata(db_session, field, value):
    conversation, rows = await sources(db_session, public=True)
    rows[0].meta = {**rows[0].meta, field: value}
    await db_session.commit()
    with pytest.raises(ValueError):
        await admit(db_session, conversation, rows, public=True)
    assert VOICE_WORK_META_KEY not in rows[-1].meta


@pytest.mark.parametrize("invalid", ["duplicate", "too_many", "empty_id", "paraphrase", "reordered_text"])
async def test_source_identity_and_authoritative_text_are_strict(db_session, invalid):
    conversation, rows = await sources(db_session)
    ids = tuple(row.id for row in rows)
    text = "\n".join(row.content for row in rows)
    if invalid == "duplicate":
        ids = (rows[0].id, rows[0].id)
    elif invalid == "too_many":
        ids = tuple(f"fake-{index}" for index in range(33))
    elif invalid == "empty_id":
        ids = ("",)
    elif invalid == "paraphrase":
        text = "Find something cheap"
    else:
        text = "\n".join(row.content for row in reversed(rows))
    with pytest.raises(ValueError):
        await admit_voice_work(db_session, conversation_id=conversation.id, text=text, user_id="owner",
                               public_channel=False, source_message_ids=ids)
    assert all(VOICE_WORK_META_KEY not in row.meta for row in rows)


@pytest.mark.parametrize("state", ["pending", "completed", "interrupted"])
async def test_sources_cannot_overlap_or_restart_terminal_work(db_session, state):
    conversation, rows = await sources(db_session, texts=("first", "second", "third"))
    receipt = await admit(db_session, conversation, rows[:2])
    if state == "interrupted":
        assert await interrupt_voice_work(db_session, receipt, conversation_id=conversation.id, reason="Explicit cancel")
    elif state == "completed":
        await finish_voice_work(db_session, receipt, conversation_id=conversation.id, state="completed")
    assert await admit(db_session, conversation, rows[:2]) == receipt
    if state != "pending":
        with pytest.raises(VoiceWorkNotPendingError):
            await claim_voice_work(db_session, receipt, conversation_id=conversation.id)
    for selection in (rows[1:], [rows[0], rows[2]], rows[:1]):
        with pytest.raises(ValueError, match="different work"):
            await admit(db_session, conversation, selection)
    assert VOICE_WORK_META_KEY not in rows[-1].meta


async def test_exact_source_retry_cannot_change_scope_id(db_session):
    conversation, rows = await sources(db_session)
    await admit(db_session, conversation, rows, scope_id="call-one")
    with pytest.raises(ValueError, match="different work"):
        await admit(db_session, conversation, rows, scope_id="call-two")


@pytest.mark.parametrize("public", [False, True])
async def test_api_recording_returns_original_user_id_then_promotes_it(monkeypatch, db_session, public):
    conversation, _ = await sources(db_session, texts=(), public=public)
    scope = ChatAudioScope("entity", "owner", conversation_id=conversation.id)
    start = (chat_voice.CallStart(public_token="public", session_id="visitor") if public
             else chat_voice.CallStart(token="test-token", conversation_id=conversation.id))

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(chat_voice, "async_session", session)
    monkeypatch.setattr(chat_voice, "resolve_call_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(chat_voice, "enforce_public_audio_budget", AsyncMock())
    request = SimpleNamespace()
    first = await chat_voice.record_call_control_turn(request, start, scope, "first", "")
    second = await chat_voice.record_call_control_turn(request, start, scope, "second", "")
    assert first and second and first != second
    receipt = await chat_voice.admit_call_work(request, start, scope, "first\nsecond", source_message_ids=(first, second))
    assert receipt.text == "first\nsecond"
    assert receipt.message_id in (first, second)
    assert await chat_voice.record_call_control_turn(request, start, scope, "", "Control reply") is None
    users = list((await db_session.scalars(select(Message).where(Message.conversation_id == conversation.id,
                                                                Message.role == "user"))).all())
    assert {row.id for row in users} == {first, second}


async def test_api_rechecks_scope_before_source_admission(monkeypatch):
    @asynccontextmanager
    async def session():
        yield AsyncMock()

    monkeypatch.setattr(chat_voice, "async_session", session)
    monkeypatch.setattr(chat_voice, "resolve_call_scope", AsyncMock(return_value=ChatAudioScope("other", "owner")))
    admission = AsyncMock()
    monkeypatch.setattr(chat_voice, "admit_voice_work", admission)
    with pytest.raises(HTTPException) as error:
        await chat_voice.admit_call_work(SimpleNamespace(), chat_voice.CallStart(token="test-token"),
                                        ChatAudioScope("entity", "owner"), "input", source_message_ids=("source",))
    assert error.value.status_code == 409
    admission.assert_not_awaited()
