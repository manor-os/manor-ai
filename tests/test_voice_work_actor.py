"""A shared Chat never lends a new caller's authority to old voice input."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from apps.api import chat_voice
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Message
from packages.core.services.voice.work_queue import (
    VOICE_WORK_META_KEY,
    VoiceWorkNotPendingError,
    VoiceWorkReceipt,
    admit_voice_work,
    claim_voice_work,
    recover_voice_work,
    validate_voice_origin_message,
)
from tests.test_voice_hitl_api import main_boundary, voice_api  # noqa: F401
from tests.test_voice_hitl_integration import _effects, _pending, _setup
from tests.test_voice_work_queue import _conversation


async def test_foreign_caller_cannot_recover_or_claim_pending_hitl(db_session, monkeypatch):
    ids = await _setup(db_session, monkeypatch)
    _message, actions = await _pending(db_session, ids, "Only the original caller may confirm")
    receipt = await admit_voice_work(
        db_session, conversation_id=ids["conversation_id"], text="确认",
        user_id=ids["user_id"], public_channel=False, presented_actions=actions,
    )
    assert await recover_voice_work(
        db_session, conversation_id=ids["conversation_id"],
        expected_author_user_id="another-owner",
    ) == []
    with pytest.raises(VoiceWorkNotPendingError, match="different author"):
        await claim_voice_work(
            db_session, receipt, conversation_id=ids["conversation_id"],
            expected_author_user_id="another-owner",
        )
    queued = await db_session.get(Message, receipt.message_id)
    assert queued.meta[VOICE_WORK_META_KEY]["state"] == "pending"
    request = await db_session.get(HitlRequest, actions[0].id)
    assert request.status == "pending"
    assert request.decided_by_user_id is None
    assert request.consumed_at is None
    assert await _effects(db_session, ids) == []

    assert await recover_voice_work(
        db_session, conversation_id=ids["conversation_id"],
        expected_author_user_id=ids["user_id"],
    ) == [replace(receipt, recovered=True)]
    claimed = await claim_voice_work(
        db_session, receipt, conversation_id=ids["conversation_id"],
        expected_author_user_id=ids["user_id"],
    )
    assert claimed.meta[VOICE_WORK_META_KEY]["state"] == "running"


async def test_foreign_running_work_is_not_interrupted_by_recovery(db_session):
    conversation = await _conversation(db_session)
    receipt = await admit_voice_work(
        db_session, conversation_id=conversation.id, text="Original work",
        user_id="speaker", public_channel=False,
    )
    await claim_voice_work(
        db_session, receipt, conversation_id=conversation.id,
        expected_author_user_id="speaker",
    )
    assert await recover_voice_work(
        db_session, conversation_id=conversation.id, expected_author_user_id="other",
    ) == []
    message = await db_session.get(Message, receipt.message_id)
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "running"
    assert await recover_voice_work(
        db_session, conversation_id=conversation.id, expected_author_user_id="speaker",
    ) == []
    await db_session.refresh(message)
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"


@pytest.mark.parametrize("stored_author", [None, "", 123, True, "other"])
async def test_authenticated_claim_rejects_missing_or_wrong_author(db_session, stored_author):
    conversation = await _conversation(db_session)
    receipt = await admit_voice_work(
        db_session, conversation_id=conversation.id, text="确认",
        user_id="speaker", public_channel=False,
    )
    message = await db_session.get(Message, receipt.message_id)
    message.meta = {**message.meta, "author_user_id": stored_author}
    await db_session.commit()
    assert await recover_voice_work(
        db_session, conversation_id=conversation.id, expected_author_user_id="speaker",
    ) == []
    with pytest.raises(VoiceWorkNotPendingError, match="different author"):
        await claim_voice_work(
            db_session, receipt, conversation_id=conversation.id,
            expected_author_user_id="speaker",
        )
    assert message.meta[VOICE_WORK_META_KEY]["state"] == "pending"


@pytest.mark.parametrize("expected_author", ["", " ", " speaker ", 123, True])
async def test_recovery_rejects_malformed_authenticated_author(expected_author):
    db = AsyncMock()
    with pytest.raises(ValueError, match="author"):
        await recover_voice_work(
            db, conversation_id="conversation", expected_author_user_id=expected_author,
        )
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("public", [False, True])
async def test_api_recovery_threads_authenticated_author(voice_api, monkeypatch, public):  # noqa: F811 - imported pytest fixture
    recover = AsyncMock(return_value=[])
    monkeypatch.setattr(chat_voice, "recover_voice_work", recover)
    start = chat_voice.CallStart(public_token="public", session_id="visitor") if public else voice_api.start
    await chat_voice.recover_call_work(voice_api.request, start, voice_api.scope)
    assert recover.await_args.kwargs == {
        "conversation_id": "conversation", "expected_author_user_id": None if public else "owner",
    }


@pytest.mark.parametrize("public", [False, True])
async def test_api_claim_failure_never_delegates_to_main(voice_api, monkeypatch, public):  # noqa: F811 - imported pytest fixture
    claim = AsyncMock(side_effect=VoiceWorkNotPendingError("different author"))
    main = AsyncMock()
    monkeypatch.setattr(chat_voice, "claim_voice_work", claim)
    monkeypatch.setattr(chat_voice, "call_agent", main)
    start = chat_voice.CallStart(public_token="public", session_id="visitor") if public else voice_api.start
    receipt = VoiceWorkReceipt("work", "message", "确认")
    with pytest.raises(VoiceWorkNotPendingError, match="different author"):
        await chat_voice.execute_call_work(voice_api.request, start, voice_api.scope, receipt)
    assert claim.await_args.kwargs == {
        "conversation_id": "conversation", "expected_author_user_id": None if public else "owner",
    }
    main.assert_not_awaited()


@pytest.mark.parametrize("author", [None, "", 123, True, "other"])
async def test_main_origin_rejects_foreign_author_before_approval(main_boundary, author):  # noqa: F811 - imported pytest fixture
    main_boundary.request.state.voice_origin_message_id = "voice-source"
    main_boundary.request.state.voice_approval_action = '{"hitl_id":"approval-1","action":"approve"}'
    main_boundary.db.scalar.return_value = SimpleNamespace(
        conversation_id="conversation", role="user", content="确认",
        meta={"author_user_id": author, VOICE_WORK_META_KEY: {"id": "work", "state": "running"}},
    )
    with pytest.raises(HTTPException) as error:
        await main_boundary.invoke("确认")
    assert error.value.status_code == 409
    main_boundary.resolver.assert_not_awaited()


async def test_origin_actor_check_preserves_existing_public_channel_contract():
    message = SimpleNamespace(
        conversation_id="conversation", role="user", content="Hello",
        meta={VOICE_WORK_META_KEY: {"id": "work", "state": "running"}},
    )
    assert validate_voice_origin_message(
        message, conversation_id="conversation", content="Hello",
    ) is message
    message.meta["author_user_id"] = "speaker"
    assert validate_voice_origin_message(
        message, conversation_id="conversation", content="Hello",
        expected_author_user_id="speaker",
    ) is message
