"""Viewing reminders is personal and cannot resolve a pending decision."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from packages.core.workspace_chat.attention import workspace_attention, mark_workspace_attention_seen
from test_workspace_chat_pending_count import _register, _workspace, _seed, _card, _page, _sidebar_stats, BASE


@pytest.mark.asyncio
async def test_viewing_one_reminder_persists_without_resolving_other_cards(client, db_session):
    headers = await _register(client, "attention_view")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    wid = await _workspace(client, headers)
    _, thread = await _seed(db_session, me["entity_id"], wid)
    cards = [_card(thread, "human_input", created_at=BASE + timedelta(minutes=i)) for i in range(2)]
    db_session.add_all(cards)
    await db_session.commit()
    page = await _page(client, headers, wid)
    keys = page["unseen_action_keys"]
    assert (await _sidebar_stats(client, headers, wid))["unseen_actions"] == 2
    endpoint = f"/api/v1/workspaces/{wid}/chat/attention/seen"
    key = keys[cards[0].id][cards[0].id]
    for _ in range(2):
        response = await client.post(endpoint, headers=headers, json={"keys": [key, "invalid"]})
        assert response.status_code == 200, response.text
        assert response.json()["unseen_actions"] == 1
    # Reload retains unread and outstanding counts independently.
    reloaded = await _page(client, headers, wid)
    assert reloaded["open_action_count"] == 2
    assert cards[0].id not in reloaded["unseen_action_keys"]
    assert cards[1].id in reloaded["unseen_action_keys"]
    for card in cards:
        await db_session.refresh(card)
        assert card.resolved_at is None and card.resolution is None
    assert (await _sidebar_stats(client, headers, wid))["unseen_actions"] == 1
    new_card = _card(thread, "human_input", created_at=BASE + timedelta(minutes=3))
    db_session.add(new_card)
    await db_session.commit()
    assert (await _sidebar_stats(client, headers, wid))["unseen_actions"] == 2


@pytest.mark.asyncio
async def test_viewed_is_personal_and_does_not_change_active_company_identity(client, db_session):
    headers = await _register(client, "attention_company")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    wid = await _workspace(client, headers)
    _, thread = await _seed(db_session, me["entity_id"], wid)
    card = _card(thread, "human_input", created_at=BASE)
    db_session.add(card)
    other = User(id=generate_ulid(), entity_id="personal-company", email="attention-other@example.com",
                 password_hash="unused", preferences={"locale_test": "keep"})
    db_session.add(other)
    await db_session.commit()
    owner = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    from sqlalchemy.orm.attributes import set_committed_value
    set_committed_value(other, "entity_id", me["entity_id"])
    attention = (await workspace_attention(db_session, user=other, workspace_ids=[wid]))[wid]
    key = attention["unseen_action_keys"][card.id][card.id]
    result = await mark_workspace_attention_seen(db_session, user=other, workspace_id=wid, keys=[key])
    assert result["unseen_actions"] == 0
    assert other.entity_id == me["entity_id"]
    assert other.preferences["locale_test"] == "keep"
    await db_session.commit()
    assert (await workspace_attention(db_session, user=owner, workspace_ids=[wid]))[wid]["unseen_actions"] == 1
    from sqlalchemy import text
    assert await db_session.scalar(text("SELECT entity_id FROM users WHERE id = :id"), {"id": other.id}) == "personal-company"


@pytest.mark.asyncio
async def test_receipts_deduplicate_request_mirrors_and_leave_approval_pending(client, db_session):
    from packages.core.models.hitl_request import HitlRequest
    headers = await _register(client, "attention_mirrors")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    wid = await _workspace(client, headers)
    _, thread = await _seed(db_session, me["entity_id"], wid)
    request = HitlRequest(id=generate_ulid(), entity_id=me["entity_id"], workspace_id=wid,
                          origin_kind="tool_call", hitl_type="permission", status="pending", context={}, dedup_key=generate_ulid())
    db_session.add(request)
    await db_session.commit()
    assert (await _page(client, headers, wid))["open_action_count"] == 1
    assert (await _sidebar_stats(client, headers, wid))["unseen_actions"] == 0
    card = _card(thread, "governance_approval", created_at=BASE)
    card.pending_action = {"kind": "governance_approval", "approval_request_id": request.id}
    db_session.add(card)
    await db_session.commit()
    page = await _page(client, headers, wid)
    key = page["unseen_action_keys"][card.id][request.id]
    response = await client.post(f"/api/v1/workspaces/{wid}/chat/attention/seen", headers=headers, json={"keys": [key]})
    assert response.status_code == 200, response.text
    mirror = _card(thread, "governance_approval", created_at=BASE + timedelta(minutes=1))
    mirror.pending_action = card.pending_action
    db_session.add(mirror)
    await db_session.commit()
    assert (await _sidebar_stats(client, headers, wid))["unseen_actions"] == 0
    assert (await _page(client, headers, wid))["open_action_count"] == 1
    await db_session.refresh(request)
    assert request.status == "pending"
    outsider = await _register(client, "attention_outsider")
    denied = await client.post(f"/api/v1/workspaces/{wid}/chat/attention/seen", headers=outsider, json={"keys": [key]})
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_independent_tool_prompts_on_one_message_have_separate_receipts(client, db_session):
    from packages.core.models.task import Message
    headers = await _register(client, "attention_tool_prompts")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    wid = await _workspace(client, headers)
    _, thread = await _seed(db_session, me["entity_id"], wid)
    ids = [generate_ulid(), generate_ulid()]
    card = Message(id=generate_ulid(), conversation_id=thread, role="assistant", content="Two inputs",
                   author_kind="agent", message_kind="message", created_at=BASE,
                   meta={"hitl_requests": [{"id": rid, "type": "input", "prompt": rid, "resolved": False} for rid in ids]})
    db_session.add(card)
    await db_session.commit()
    page = await _page(client, headers, wid)
    keys = page["unseen_action_keys"][card.id]
    response = await client.post(f"/api/v1/workspaces/{wid}/chat/attention/seen", headers=headers, json={"keys": [keys[ids[0]]]})
    assert response.status_code == 200, response.text
    assert response.json()["unseen_actions"] == 1
    assert response.json()["unseen_action_keys"][card.id] == {ids[1]: keys[ids[1]]}
    await db_session.refresh(card)
    assert not any(request["resolved"] for request in card.meta["hitl_requests"])


@pytest.mark.asyncio
async def test_unread_list_is_read_only_scoped_and_excludes_viewed_cards(client, db_session):
    headers = await _register(client, "attention_list")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    wid = await _workspace(client, headers)
    _, thread = await _seed(db_session, me["entity_id"], wid)
    cards = [_card(thread, "human_input", created_at=BASE + timedelta(minutes=i)) for i in range(2)]
    for i, card in enumerate(cards):
        card.pending_action = {**card.pending_action, "title": f"Question {i}"}
    db_session.add_all(cards)
    await db_session.commit()
    # Legacy workflow outcomes may be structured JSON; show their summary.
    import json
    cards[0].pending_action = {"kind": "human_input", "step_id": generate_ulid()}
    cards[0].content = json.dumps({"status": "blocked", "summary": "Question 0"})
    await db_session.commit()
    endpoint = f"/api/v1/workspaces/{wid}/chat/attention"
    first = await client.get(endpoint, headers=headers)
    assert first.status_code == 200, first.text
    items = first.json()["items"]
    assert [item["message_id"] for item in items] == [cards[1].id, cards[0].id]
    assert [item["summary"] for item in items] == ["Question 1", "Question 0"]
    assert len(items) == (await _sidebar_stats(client, headers, wid))["unseen_actions"]
    assert (await client.get(endpoint, headers=headers)).json() == first.json()
    page = await _page(client, headers, wid)
    assert items[0]["key"] == page["unseen_action_keys"][cards[1].id][cards[1].id]
    await client.post(endpoint + "/seen", headers=headers, json={"keys": [items[0]["key"]]})
    unread = (await client.get(endpoint, headers=headers)).json()["items"]
    assert [item["message_id"] for item in unread] == [cards[0].id]
    assert (await _page(client, headers, wid))["open_action_count"] == 2
    outsider = await _register(client, "attention_list_outsider")
    assert (await client.get(endpoint, headers=outsider)).status_code == 404


@pytest.mark.asyncio
async def test_unread_list_deduplicates_mirrors_and_preserves_independent_prompts(client, db_session):
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Message
    headers = await _register(client, "attention_list_mirrors")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    wid = await _workspace(client, headers)
    _, thread = await _seed(db_session, me["entity_id"], wid)
    request = HitlRequest(id=generate_ulid(), entity_id=me["entity_id"], workspace_id=wid,
        origin_kind="tool_call", hitl_type="permission", status="pending", context={}, dedup_key=generate_ulid())
    db_session.add(request)
    cards = [_card(thread, "governance_approval", created_at=BASE + timedelta(minutes=i)) for i in range(2)]
    for i, card in enumerate(cards):
        card.pending_action = {**card.pending_action, "approval_request_id": request.id, "title": f"Approval {i}"}
    db_session.add_all(cards)
    prompt_ids = [generate_ulid(), generate_ulid()]
    prompts = Message(id=generate_ulid(), conversation_id=thread, role="assistant", content="Two inputs",
        author_kind="agent", message_kind="message", created_at=BASE + timedelta(minutes=3),
        meta={"hitl_requests": [{"id": rid, "type": "input", "prompt": f"Input {i}", "resolved": False}
                               for i, rid in enumerate(prompt_ids)]})
    db_session.add(prompts)
    await db_session.commit()
    endpoint = f"/api/v1/workspaces/{wid}/chat/attention"
    response = await client.get(endpoint, headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3
    approvals = [item for item in items if item["kind"] == "governance_approval"]
    assert len(approvals) == 1 and approvals[0]["message_id"] == cards[1].id
    assert approvals[0]["source_id"] == request.id
    inputs = [item for item in items if item["message_id"] == prompts.id]
    assert {item["source_id"] for item in inputs} == set(prompt_ids)
    assert {item["summary"] for item in inputs} == {"Input 0", "Input 1"}
    assert len({item["key"] for item in inputs}) == 2
    await db_session.refresh(request)
    assert request.status == "pending"
    for card in cards:
        await db_session.refresh(card)
        assert card.resolved_at is None
