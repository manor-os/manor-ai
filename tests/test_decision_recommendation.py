"""Every question carries exactly one recommended option with a one-line reason."""
from __future__ import annotations

import pytest

from packages.core.governance.always_allow import (
    authorization_card_fields,
    is_irreversible,
    recommend_authorization,
)
from packages.core.services.decision_recommendation import (
    RecommendationError,
    channel_actions,
    ensure_recommendation,
    project_recommendation,
    recommendation,
)


def test_a_card_without_a_chosen_answer_gets_the_shared_default_first():
    card = ensure_recommendation({"kind": "workflow_retry", "options": ["cancel", "retry"]})
    assert card["options"] == ["retry", "cancel"]
    assert card["recommendation"]["option"] == "retry" and card["recommendation"]["reason"]
    assert card["allow_other"] is True
    # A review recommends approving with the review's own reason.
    review = ensure_recommendation({"kind": "task_approval", "options": ["approve", "request_changes"]})
    assert review["recommendation"]["reason_code"] == "review_ready"


@pytest.mark.parametrize("bad", [
    {"option": "send", "reason": "Not one of the options."},
    {"option": "approve", "reason": "  "},
    {"option": "approve", "reason": "Two lines.\nAre not one sentence."},
    {"option": "approve", "reason": "x" * 281},
])
def test_a_producer_recommendation_that_breaks_the_contract_is_refused(bad):
    with pytest.raises(RecommendationError):
        ensure_recommendation({"kind": "governance_approval", "options": ["approve", "reject"], "recommendation": bad})


def test_option_details_may_only_describe_offered_options():
    with pytest.raises(RecommendationError):
        ensure_recommendation({
            "kind": "approve_proposals", "options": ["approve", "reject"],
            "option_details": {"approve_selected": {"label": "Only the first"}},
        })


def test_a_stored_card_with_a_broken_recommendation_still_reads():
    stored = {"kind": "workflow_approval", "options": ["approve", "cancel"],
              "recommendation": {"option": "gone", "reason": "old"}}
    assert project_recommendation(stored)["recommendation"]["option"] == "approve"


def test_irreversible_actions_recommend_once_and_repeated_approvals_recommend_always():
    assert is_irreversible(action_key="external_message.send")
    assert not is_irreversible(action_key="workspace.automation.create")
    once = recommend_authorization(irreversible=True, approved=9, declined=0, threshold=3, standing=True)
    assert once["option"] == "approve" and once["reason_code"] == "irreversible_once"
    always = recommend_authorization(irreversible=False, approved=3, declined=0, threshold=3, standing=True)
    assert always["option"] == "approve_and_save_rule"
    assert recommend_authorization(irreversible=False, approved=3, declined=1, threshold=3, standing=True)["option"] == "approve"
    assert recommend_authorization(irreversible=False, approved=2, declined=0, threshold=3, standing=True)["option"] == "approve"


def test_a_card_that_cannot_stand_never_offers_or_recommends_always():
    decision = {"standing": False, "recommendation": recommendation("approve_and_save_rule", reason_code="repeated_always")}
    fields = authorization_card_fields(decision, action_label="post a tweet")
    assert fields["options"] == ["approve", "reject"]
    assert fields["recommendation"]["option"] == "approve"


def test_channel_buttons_mirror_the_card_recommended_first():
    buttons = channel_actions({
        "kind": "external_message_approval", "options": ["reject", "approve", "approve_and_save_rule"],
        "option_details": {"approve_and_save_rule": {"label": "Always send replies without asking"}},
        "recommendation": recommendation("approve_and_save_rule", reason_code="repeated_always", reason_params={"count": 4}),
    })
    assert [button["key"] for button in buttons] == ["approve_and_save_rule", "reject", "approve"]
    assert buttons[0]["label"] == "Always send replies without asking · Recommended"


def test_a_proposal_is_decided_as_whole_combinations():
    from packages.core.strategist.service import _proposal_combination_options

    action = {"tasks": [
        {"task_id": "t1", "title": "Invite two creators", "rationale": "No budget needed. Last week it worked."},
        {"task_id": "t2", "title": "Raise the quote"},
    ], "items": [{"item_id": "i1", "summary": "Add a goal"}]}
    card = ensure_recommendation({"kind": "approve_proposals", **action, **_proposal_combination_options(action)})
    assert card["options"] == ["approve", "approve_selected", "reject"]
    assert card["recommendation"] == {"option": "approve", "reason": "No budget needed."}
    only_first = card["option_details"]["approve_selected"]
    assert only_first["payload"] == {"selected_task_ids": ["t1"], "selected_item_ids": []}
    single = _proposal_combination_options({"tasks": [{"task_id": "t1", "title": "Invite"}], "items": []})
    assert single["options"] == ["approve", "reject"]


@pytest.mark.asyncio
async def test_message_writes_enforce_the_contract(db_session):
    """Every card written to a conversation leaves with its recommendation;
    a producer bug (an answer that is not offered) fails the write."""
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import Entity, User
    from packages.core.services.conversation_messages import add_message

    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Recommendation"),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="x", role="owner"),
    ])
    conversation = Conversation(id=generate_ulid(), entity_id=entity_id, user_id=user_id, title="chat")
    db_session.add(conversation)
    await db_session.flush()

    message = await add_message(db_session, conversation.id, role="system", content="Run it?",
                                pending_action={"kind": "workflow_starter_input", "options": ["cancel", "run"]})
    assert message.pending_action["options"] == ["run", "cancel"]
    assert message.pending_action["recommendation"]["reason_code"] == "run_flow"
    with pytest.raises(RecommendationError):
        await add_message(db_session, conversation.id, role="system", content="Run it?", pending_action={
            "kind": "workflow_starter_input", "options": ["run", "cancel"],
            "recommendation": {"option": "skip", "reason": "Not offered."},
        })


@pytest.mark.asyncio
async def test_a_workspace_draft_question_rides_on_the_architect_reply(db_session):
    """Create Workspace: the architect's follow-up question carries answer
    buttons (recommended first, at most 3) on its reply; a question from a
    turn that never finished does not leak onto the next reply."""
    import json

    from packages.core.ai.tools.workspace_arch_tools import HANDLERS, PENDING_QUESTION_FIELD
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_draft_service import _record_visible_messages

    entity_id, user_id = generate_ulid(), generate_ulid()
    draft = WorkspaceDraft(entity_id=entity_id, user_id=user_id, fields={}, messages=[],
                           missing=[], ready=False, status="active")
    db_session.add(draft)
    await db_session.commit()

    refused = json.loads(await HANDLERS["ws_ask_user"](
        db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id,
        question="What should the popup workspace handle?", options=["Promote it"], recommended="",
    ))
    assert refused["ok"] is False

    asked = json.loads(await HANDLERS["ws_ask_user"](
        db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id,
        question="What should the Weekend Coffee Popup workspace handle?",
        options=["Promote it", "Take orders", "Coordinate staff", "Plan the event"],
        recommended="Plan and promote the event",
        recommendation_reason="A weekend popup mostly needs a plan and people showing up.",
    ))
    assert asked["ok"] is True
    await db_session.commit()
    await db_session.refresh(draft)
    _record_visible_messages(draft, "Weekend coffee popup", "What should it handle?")
    reply = draft.messages[-1]
    assert reply["question"]["options"] == ["Plan and promote the event", "Promote it", "Take orders"]
    assert reply["question"]["recommendation"]["option"] == "Plan and promote the event"
    assert reply["question"]["answer_as"] == "message"
    assert PENDING_QUESTION_FIELD not in draft.fields
    # Create Workspace inside an ordinary chat: the draft tool result carries
    # the same question so the chat shows its buttons under the reply.
    from packages.core.ai.runtime.workspace_drafts import _draft_artifact_payload

    assert _draft_artifact_payload(draft, "What should it handle?")["question"] == reply["question"]

    _record_visible_messages(draft, "Plan and promote the event", "Got it — drafting the services.")
    assert "question" not in draft.messages[-1]
    assert "question" not in _draft_artifact_payload(draft, "Got it — drafting the services.")


@pytest.mark.asyncio
async def test_the_draft_api_returns_the_architect_question_with_its_reply(client, db_session):
    """GET /workspace-drafts/{id} keeps a reply's answer buttons; the pending
    working field never leaks into the draft's public fields."""
    from tests.auth_helpers import register_user_and_get_token
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.decision_recommendation import reply_question

    token = (await register_user_and_get_token(client, json={
        "username": "draftquestion", "email": "draftquestion@test.com",
        "password": "DraftQuestionTest123!", "entity_name": "Draft Question Co",
    })).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    question = reply_question(
        "What should the popup workspace handle?", ["Promote it"], "Plan and promote the event",
        "A weekend popup mostly needs a plan and people showing up.",
    )
    draft = WorkspaceDraft(
        entity_id=me["entity_id"], user_id=me["id"], status="active", missing=[], ready=False,
        fields={"_pending_question": question},
        messages=[
            {"role": "user", "content": "Weekend coffee popup"},
            {"role": "assistant", "content": "What should it handle?", "question": question},
        ],
    )
    db_session.add(draft)
    await db_session.commit()

    response = await client.get(f"/api/v1/workspace-drafts/{draft.id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    reply = body["messages"][-1]
    assert reply["question"]["options"] == ["Plan and promote the event", "Promote it"]
    assert reply["question"]["recommendation"]["option"] == "Plan and promote the event"
    assert "question" not in body["messages"][0]
    assert "_pending_question" not in body["fields"]


@pytest.mark.asyncio
async def test_a_card_closes_on_other_clients_only_when_the_answer_commits(db_session, monkeypatch):
    """A resolve that fails afterwards (the Task stopped waiting) rolls back;
    the realtime "resolved" event must not have closed the card meanwhile."""
    import asyncio

    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace
    from packages.core.workspace_chat import service as chat_service

    published: list[dict] = []

    async def capture(entity_id, data):
        published.append(data)

    monkeypatch.setattr(chat_service, "_publish_workspace_chat_event", capture)
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    db_session.add_all([Entity(id=entity_id, name="Realtime"),
                        Workspace(id=workspace_id, entity_id=entity_id, name="W", status="active")])
    await db_session.flush()

    async def answer() -> None:
        card = await chat_service.post_message(
            db_session, entity_id=entity_id, workspace_id=workspace_id, body="Should the FAQ mention pricing?",
            author_kind="agent", pending_action={"kind": "human_input", "options": ["Link to the pricing page"]},
        )
        await db_session.commit()
        published.clear()
        await chat_service.resolve_pending_action(
            db_session, message_id=card.id, user_id=generate_ulid(),
            resolution={"choice": "respond", "note": "Link to the pricing page"},
        )

    await answer()
    await db_session.rollback()
    await asyncio.sleep(0)
    assert not [event for event in published if event.get("action_resolved")]

    await answer()
    await db_session.commit()
    await asyncio.sleep(0)
    assert [event["resolution"]["note"] for event in published if event.get("action_resolved")] == [
        "Link to the pricing page",
    ]


@pytest.mark.asyncio
async def test_an_expired_session_asks_the_account_admins_to_reconnect(db_session, monkeypatch):
    """The session belongs to the account, so its reconnect question goes to
    owners/admins where they see it, with Reconnect recommended."""
    from packages.core import database
    from packages.core.integrations.sessions import service as sessions
    from packages.core.models.base import generate_ulid
    from packages.core.models.integration_session import IntegrationSession
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.services import notify as notify_module

    class _Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return False

    sent: list[dict] = []

    async def capture(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(notify_module, "notify", capture)
    entity_id, owner_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Reconnect"),
        User(id=owner_id, entity_id=entity_id, email=f"{owner_id}@example.test", password_hash="x",
             role="owner", status="active"),
        UserMembership(user_id=owner_id, entity_id=entity_id, role="owner", status="active"),
    ])
    row = IntegrationSession(entity_id=entity_id, provider="wechat", label="shop", status="active")
    db_session.add(row)
    await db_session.flush()

    await sessions.expire_session(db_session, session_id=row.id, reason="cookies_expired")

    assert [item["user_id"] for item in sent] == [owner_id]
    assert sent[0]["link"] == "/integrations?provider=wechat"
    question = sent[0]["meta"]["pending_action"]
    assert question["recommendation"]["option"] == "sign_in"
    assert question["option_details"]["sign_in"]["label"] == "Reconnect"


@pytest.mark.asyncio
async def test_a_card_answered_with_always_allow_says_so(db_session):
    """The rule is saved first, then the card's own approve runs: the card
    records "Always allow", not the approve that followed."""
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import Entity, User
    from packages.core.services.hitl_requests import mark_hitl_request_resolved

    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([Entity(id=entity_id, name="Always"),
                        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="x")])
    conversation = Conversation(id=generate_ulid(), entity_id=entity_id, user_id=user_id, title="chat")
    db_session.add(conversation)
    await db_session.flush()
    saved, plain = (HitlRequest(
        id=generate_ulid(), entity_id=entity_id, action_key="workspace.automation.create", risk_level="medium",
        origin_kind="tool_call", origin_conversation_id=conversation.id, status="granted", hitl_type="authorize",
        dedup_key=f"always:{generate_ulid()}", payload={}, context=context,
    ) for context in ({"rule_command": {"result": {}}}, {}))
    message = Message(conversation_id=conversation.id, role="assistant", content="", meta={"hitl_requests": [
        {"id": saved.id, "type": "approval"}, {"id": plain.id, "type": "approval"},
    ]})
    db_session.add_all([saved, plain, message])
    await db_session.flush()

    for request in (saved, plain):
        await mark_hitl_request_resolved(db_session, conversation_id=conversation.id, hitl_id=request.id, choice="approve")
    await db_session.flush()
    await db_session.refresh(message)
    assert [entry["resolution"] for entry in message.meta["hitl_requests"]] == ["approve_and_save_rule", "approve"]


def test_an_approval_click_line_stays_out_of_the_chat_list():
    """The model keeps "Approved the requested action."; people see the card's choice."""
    from types import SimpleNamespace

    from apps.api.routers.chat import _approval_receipt_meta, _visible_chat_messages

    receipt = SimpleNamespace(id="r", role="user", content="Approved the requested action.",
                              meta={"author_user_id": "u", **_approval_receipt_meta("Approved the requested action.")})
    typed = SimpleNamespace(id="t", role="user", content="Send it", meta={"author_user_id": "u", **_approval_receipt_meta(None)})
    assert [message.id for message in _visible_chat_messages([typed, receipt])] == ["t"]


@pytest.mark.parametrize("card_type", ["approval", "authorize"])
@pytest.mark.parametrize("prompt", ["approval.mode", "workspace.agent.capabilities.modify", ""])
def test_every_authorization_is_the_authorization_card_titled_in_words(card_type, prompt):
    from packages.core.services.decision_recommendation import project_hitl_entries

    [card] = project_hitl_entries([{
        "id": "req", "type": card_type, "prompt": prompt,
        "action": "workspace.agent.capabilities.modify", "options": ["approve", "reject"],
    }])
    assert card["type"] == "approval"
    assert card["prompt"] == "Modify workspace agent capabilities"
    assert card["recommendation"]["option"] == "approve"


def test_a_readable_authorization_title_and_questions_are_left_alone():
    from packages.core.services.decision_recommendation import project_hitl_entries

    readable, question = project_hitl_entries([
        {"id": "a", "type": "approval", "prompt": "Add capabilities to this Workspace Agent",
         "action": "workspace.agent.capabilities.modify", "options": ["approve", "reject"]},
        {"id": "q", "type": "input", "prompt": "Which unit?"},
    ])
    assert readable["prompt"] == "Add capabilities to this Workspace Agent"
    assert question["type"] == "input" and question["prompt"] == "Which unit?"


@pytest.mark.asyncio
@pytest.mark.parametrize("hitl_type", ["authorize", "review"])
async def test_a_governance_card_says_what_needs_approval_without_internal_names(monkeypatch, hitl_type):
    """No rule ids, step keys or action keys in what people read."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from packages.core.governance import service
    from packages.core.workspace_chat import service as chat_service

    posted = AsyncMock()
    monkeypatch.setattr(chat_service, "post_message", posted)
    await service.post_hitl_card(
        entity_id="e", workspace_id="w", plan_id="p", step_id="s", step_key="publish_site_step",
        kind="action", action_key="site.publish", matched_rule="approval.mode", reason="approval.mode",
        hitl_type=hitl_type, db=SimpleNamespace(),
    )
    kwargs = posted.await_args.kwargs
    shown = f"{kwargs['body']}\n{kwargs['pending_action']['prompt']}"
    for internal in ("approval.mode", "publish_site_step", "site.publish", "rule"):
        assert internal not in shown, shown


def test_stored_text_never_shows_a_rule_id():
    from types import SimpleNamespace

    from packages.core.services.decision_recommendation import (
        is_internal_key_only_message,
        project_recommendation,
        strip_internal_rule_mentions,
        user_facing_reason,
    )

    stored = "Approval needed before sending.\n\nRecipient: `Ana`\nRule: `approval.mode`\n\nDraft reply:\nHi"
    assert "approval.mode" not in strip_internal_rule_mentions(stored)
    assert "Draft reply:\nHi" in strip_internal_rule_mentions(stored)
    assert strip_internal_rule_mentions("Paused by your policy (rule: `approval.mode`).") == "Paused by your policy."
    assert strip_internal_rule_mentions("approval.mode") == ""
    assert user_facing_reason("approval.mode") == ""
    assert user_facing_reason("This can't be undone.") == "This can't be undone."
    card = project_recommendation({"kind": "governance_approval", "prompt": "approval.mode",
                                   "action": "site.publish", "options": ["approve", "reject"]})
    assert card["prompt"] == "Publish site"
    assert is_internal_key_only_message(SimpleNamespace(role="assistant", content="approval.mode", meta={}, pending_action=None))
    assert not is_internal_key_only_message(SimpleNamespace(role="user", content="approval.mode", meta={}, pending_action=None))
    assert not is_internal_key_only_message(SimpleNamespace(role="assistant", content="Done.", meta={}, pending_action=None))
