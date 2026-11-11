"""Asking again in a later turn closes the earlier turn's card for the same action."""
import pytest


async def _conversation(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message

    entity_id, user_id, conversation_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id, title="cards"))
    await db_session.flush()
    return entity_id, conversation_id, Message


async def _ask(db_session, entity_id, conversation_id, *, run, call, tool="generate_file",
               action_key="workspace.file.create", task_id=None):
    from packages.core.governance.approvals import (
        ApprovalOrigin, ApprovalSubject, _create_pending_request,
    )

    return await _create_pending_request(
        db_session,
        subject=ApprovalSubject(entity_id=entity_id, action_key=action_key),
        origin=ApprovalOrigin(
            kind="tool_call", conversation_id=conversation_id, task_id=task_id,
            operation_id=f"runtime:{run}:call:{call}", context={"tool": tool},
        ),
        dedup_key=f"tool:{conversation_id}:{run}:{call}", reason=None, matched_rule=None,
    )


@pytest.mark.asyncio
async def test_a_later_turn_asking_again_closes_the_earlier_card(db_session):
    """Live: a write held in turn 1 and tried again in turn 3 left two cards; rejecting
    the newer one left the stale one open, still approvable."""
    from packages.core.models.base import generate_ulid

    entity_id, conversation_id, Message = await _conversation(db_session)
    first = await _ask(db_session, entity_id, conversation_id, run="R1", call="c1")
    card = Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant", content="",
        message_kind="hitl_request", meta={"hitl_requests": [{"id": first.id, "type": "approval"}]},
    )
    db_session.add(card)
    await db_session.flush()

    second = await _ask(db_session, entity_id, conversation_id, run="R3", call="c9")

    await db_session.refresh(first)
    await db_session.refresh(card)
    assert first.status == "expired" and first.resolved_reason == "superseded"
    assert card.meta["hitl_requests"][0]["resolved"] is True
    assert second.status == "pending"


@pytest.mark.asyncio
async def test_cards_of_one_turn_and_other_actions_stay(db_session):
    entity_id, conversation_id, _ = await _conversation(db_session)
    a = await _ask(db_session, entity_id, conversation_id, run="R1", call="c1")
    b = await _ask(db_session, entity_id, conversation_id, run="R1", call="c2")
    other_action = await _ask(db_session, entity_id, conversation_id, run="R2", call="c1",
                              action_key="workspace.knowledge.update")
    other_tool = await _ask(db_session, entity_id, conversation_id, run="R2", call="c2", tool="write_file")
    for row in (a, b, other_action, other_tool):
        await db_session.refresh(row)
        assert row.status == "pending"


@pytest.mark.asyncio
async def test_task_owned_cards_are_not_superseded_from_chat(db_session):
    """A Task approval has its own lifecycle; a Chat re-ask must not revoke it."""
    from packages.core.models.base import generate_ulid

    entity_id, conversation_id, _ = await _conversation(db_session)
    task_card = await _ask(db_session, entity_id, conversation_id, run="R1", call="c1", task_id=generate_ulid())
    await _ask(db_session, entity_id, conversation_id, run="R2", call="c1")
    await db_session.refresh(task_card)
    assert task_card.status == "pending"
