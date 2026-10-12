"""The chat banner and the sidebar badge must be the SAME number.

Production report: the sidebar showed 10, the banner showed 18. The user
answered a card and only the left-hand number moved.

Two independent causes, both fixed here:

1. The banner counted the cards the client happened to be holding in memory.
   That list only grows — ``unresolved_pending_messages`` pins cards from far
   outside the page window, and the moment one is answered it leaves the pinned
   set rather than coming back marked resolved, so the client's merge-by-id has
   nothing to overwrite. The badge re-counted in the DB and dropped. Hence one
   answer, two numbers. The endpoint now returns the authoritative count.

2. The badge's own ``if kind in {...} / elif / elif`` chain had no ``else``, so
   any kind added later counted as zero — ``external_message_approval``
   (approve a post/tweet) and ``needs_login`` were rendered by the chat as
   actionable cards while being invisible to the badge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.hitl_request import HitlRequest

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{username}@example.com",
            "password": "test-password-123",
            "entity_name": f"{username} Co",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Content Engine"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _badge_total(stats: dict) -> int:
    """What the sidebar renders — see workspaceChatActionCount in the web app."""
    return (
        (stats.get("chat_pending_actions") or 0)
        + (stats.get("proposal_actions") or 0)
        + (stats.get("failed_actions") or 0)
    )


async def _sidebar_stats(client: AsyncClient, headers: dict, workspace_id: str) -> dict:
    resp = await client.get("/api/v1/workspaces", headers=headers)
    assert resp.status_code == 200, resp.text
    row = next(w for w in resp.json() if w["id"] == workspace_id)
    return row.get("stats") or {}


async def _page(client: AsyncClient, headers: dict, workspace_id: str, **params):
    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 75, **params},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed(db_session, entity_id: str, workspace_id: str):
    main_id, thread_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=main_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Conversation(
        id=thread_id, entity_id=entity_id, workspace_id=workspace_id,
        title="plan 01PLAN", channel="workspace", scope="workspace_thread",
        thread_ref_kind="plan", thread_ref_id="01PLAN",
    ))
    return main_id, thread_id


def _card(conversation_id: str, kind: str, *, created_at: datetime, **extra) -> Message:
    return Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant",
        content=f"Action needed — {kind}", author_kind="system",
        message_kind="hitl_request",
        pending_action={"kind": kind, "step_id": generate_ulid()},
        created_at=created_at, **extra,
    )


@pytest.mark.asyncio
async def test_banner_count_equals_badge_across_every_kind(
    client: AsyncClient, db_session,
):
    """The number the chat shows and the number the sidebar shows are one number.

    Includes the two kinds the badge used to drop on the floor.
    """
    headers = await _register(client, "ws_count_kinds")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)

    kinds = [
        "human_input",
        "governance_approval",
        "workspace_operation_review",
        "needs_input",
        "needs_confirmation",
        "approve_proposals",
        "retry_strategist_review",
        "external_message_approval",   # was invisible to the badge
        "needs_login",                 # was invisible to the badge
    ]
    for index, kind in enumerate(kinds):
        db_session.add(_card(
            thread_id, kind, created_at=BASE + timedelta(minutes=index),
        ))
    await db_session.commit()

    body = await _page(client, headers, workspace_id)
    stats = await _sidebar_stats(client, headers, workspace_id)

    assert body["open_action_count"] == len(kinds)
    assert _badge_total(stats) == len(kinds)
    assert body["open_action_count"] == _badge_total(stats)
    # Every card is reachable from the chat, so the count is never a dead end.
    assert body["open_actions_complete"] is True
    assert len([r for r in body["items"] if r.get("pending_action")]) == len(kinds)


@pytest.mark.asyncio
async def test_answering_a_pinned_card_moves_both_numbers(
    client: AsyncClient, db_session,
):
    """The reported symptom: answer a card, only the sidebar moved.

    The card is older than the whole page window, so it is present ONLY because
    it was pinned. Once answered it leaves the pinned set and the page stops
    returning it — which is exactly why a client-side count cannot notice.
    """
    headers = await _register(client, "ws_count_answer")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    main_id, thread_id = await _seed(db_session, me["entity_id"], workspace_id)

    old_card = _card(thread_id, "governance_approval", created_at=BASE)
    db_session.add(old_card)
    db_session.add(_card(
        thread_id, "human_input", created_at=BASE + timedelta(minutes=1),
    ))
    # Bury the card under a full page of newer chatter.
    for index in range(90):
        db_session.add(Message(
            id=generate_ulid(), conversation_id=main_id, role="assistant",
            content=f"activity-{index}", author_kind="agent",
            message_kind="step_event",
            created_at=BASE + timedelta(hours=1, minutes=index),
        ))
    await db_session.commit()

    before = await _page(client, headers, workspace_id)
    assert before["open_action_count"] == 2
    assert old_card.id in {r["id"] for r in before["items"]}
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 2

    old_card.resolved_at = BASE + timedelta(hours=2)
    old_card.resolution = {"choice": "approve"}
    await db_session.commit()

    after = await _page(client, headers, workspace_id)
    # The page drops it — nothing comes back for the client to overwrite.
    assert old_card.id not in {r["id"] for r in after["items"]}
    # ...so the count has to come from the server, and it does.
    assert after["open_action_count"] == 1
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 1
    # The flag that lets the client retire its stale copy.
    assert after["open_actions_complete"] is True


@pytest.mark.asyncio
async def test_open_actions_complete_is_false_when_pins_are_capped(
    client: AsyncClient, db_session,
):
    """Past the pin cap the client is NOT holding the whole open set, so it must
    not treat an absent card as answered."""
    from apps.api.routers.workspace_chat import _PINNED_ACTION_LIMIT

    headers = await _register(client, "ws_count_capped")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)

    total = _PINNED_ACTION_LIMIT + 3
    for index in range(total):
        db_session.add(_card(
            thread_id, "human_input", created_at=BASE + timedelta(minutes=index),
        ))
    await db_session.commit()

    body = await _page(client, headers, workspace_id)
    # The count is still exact even though the card list is capped.
    assert body["open_action_count"] == total
    assert body["open_actions_complete"] is False
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == total


@pytest.mark.asyncio
async def test_thread_view_does_not_claim_a_complete_open_set(
    client: AsyncClient, db_session,
):
    """Only the main view pins. A thread view shows one plan's messages, so it
    must not license the client to retire cards it cannot see."""
    headers = await _register(client, "ws_count_thread")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    db_session.add(_card(thread_id, "human_input", created_at=BASE))
    await db_session.commit()

    body = await _page(
        client, headers, workspace_id,
        thread_ref_kind="plan", thread_ref_id="01PLAN",
    )
    assert body["open_actions_complete"] is False
    # The workspace-wide count stays honest regardless of which view asked.
    assert body["open_action_count"] == 1


@pytest.mark.asyncio
async def test_distinct_live_requests_include_metadata_and_ignore_stale_mirrors(client, db_session):
    headers = await _register(client, "ws_distinct_requests")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    main_id, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    requests = []
    for status in ("pending", "pending", "consumed", "expired", "granted", "denied"):
        request = HitlRequest(
            id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
            origin_kind="tool_call", origin_conversation_id=thread_id,
            status=status, dedup_key=generate_ulid(),
        )
        db_session.add(request)
        requests.append(request)
    # Two independent decisions on one old metadata-only message.
    tool_card = Message(
        id=generate_ulid(), conversation_id=thread_id, role="assistant",
        content="Review tools", author_kind="agent", message_kind="message",
        meta={"hitl_requests": [{"id": r.id, "prompt": "Approve tool?"} for r in requests]},
        created_at=BASE,
    )
    db_session.add(tool_card)
    for request in requests:
        for index in range(2):
            card = _card(thread_id, "governance_approval", created_at=BASE + timedelta(minutes=index))
            card.pending_action = {"kind": "governance_approval", "approval_request_id": request.id}
            db_session.add(card)
    for index in range(90):
        db_session.add(Message(
            id=generate_ulid(), conversation_id=main_id, role="assistant",
            content="New activity", author_kind="agent", message_kind="message",
            created_at=BASE + timedelta(hours=1, minutes=index),
        ))
    await db_session.commit()

    page = await _page(client, headers, workspace_id)
    stats = await _sidebar_stats(client, headers, workspace_id)
    assert page["open_action_count"] == _badge_total(stats) == 2
    # Counting does not add response fields or rewrite card/history state.
    assert "open_actions" not in page
    fetched = (await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{tool_card.id}", headers=headers,
    )).json()
    assert [r["id"] for r in fetched["hitl_requests"] if not r.get("resolved")] == [r.id for r in requests]
    await db_session.refresh(tool_card)
    assert not any(r.get("resolved") for r in tool_card.meta["hitl_requests"])


@pytest.mark.asyncio
async def test_metadata_only_requests_are_counted_individually(client, db_session):
    headers = await _register(client, "ws_tool_only_count")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    ids = [generate_ulid() for _ in range(3)]
    for request_id in ids:
        db_session.add(HitlRequest(
            id=request_id, entity_id=me["entity_id"], workspace_id=workspace_id,
            origin_kind="tool_call", status="pending", dedup_key=request_id,
        ))
    card = Message(
        id=generate_ulid(), conversation_id=thread_id, role="assistant",
        content="Tools", author_kind="agent", message_kind="message",
        meta={"hitl_requests": [{"id": rid, "prompt": "Approve?"} for rid in ids]},
        created_at=BASE,
    )
    db_session.add(card)
    await db_session.commit()
    page = await _page(client, headers, workspace_id)
    assert page["open_action_count"] == _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 3


@pytest.mark.asyncio
async def test_legacy_business_identity_deduplicates_without_merging_distinct_inputs(client, db_session):
    headers = await _register(client, "ws_legacy_identity")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    actions = [
        {"kind": "approve_proposals", "review_id": "review-a"},
        {"kind": "workflow_input", "workflow_run_id": "run-a", "step_id": "step-a"},
        {"kind": "workflow_input", "workflow_run_id": "run-a", "step_id": "step-b"},
        {"kind": "retry_strategist_review", "review_id": "review-b"},
    ]
    for action in actions:
        for index in range(2):
            card = _card(thread_id, action["kind"], created_at=BASE + timedelta(minutes=index))
            card.pending_action = action
            db_session.add(card)
    await db_session.commit()
    page = await _page(client, headers, workspace_id)
    assert page["open_action_count"] == _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 4


@pytest.mark.asyncio
async def test_owner_lifecycle_and_workspace_pause_keep_only_actionable_decisions(client, db_session):
    from packages.core.models.proposal import ProposalRecord
    from packages.core.models.workflow import WorkflowRun
    from packages.core.models.workspace import Workspace, WorkspaceOperationDraft
    from packages.core.workspace_chat.pending_actions import count_pending_actions_by_workspace

    headers = await _register(client, "ws_pending_owners")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    proposal = ProposalRecord(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        review_id=generate_ulid(), status="open", summary="Review cohort",
    )
    draft = WorkspaceOperationDraft(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        status="open",
    )
    run = WorkflowRun(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        workflow_id=generate_ulid(), status="paused", current_step_id="input-a",
    )
    db_session.add_all([proposal, draft, run])
    for action in (
        {"kind": "approve_proposals", "review_id": proposal.review_id},
        {"kind": "workspace_operation_review", "draft_id": draft.id},
        {"kind": "workflow_input", "workflow_run_id": run.id, "step_id": "input-a"},
        {"kind": "workflow_input", "workflow_run_id": run.id, "step_id": "obsolete-step"},
    ):
        card = _card(thread_id, action["kind"], created_at=BASE)
        card.pending_action = action
        db_session.add(card)
    workspace = await db_session.get(Workspace, workspace_id)
    workspace.status = "paused"
    await db_session.flush()
    before = (await count_pending_actions_by_workspace(db_session, entity_id=me["entity_id"], workspace_ids=[workspace_id]))[workspace_id]
    assert sum(before.values()) == 3
    proposal.status = "resolved"
    draft.status = "applied"
    run.status = "completed"
    await db_session.flush()
    after = (await count_pending_actions_by_workspace(db_session, entity_id=me["entity_id"], workspace_ids=[workspace_id]))[workspace_id]
    assert sum(after.values()) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_retry", [False, True])
async def test_retried_workflow_and_nested_or_foreign_requests_do_not_add_actions(client, db_session, legacy_retry):
    from packages.core.constants.execution import COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY
    from packages.core.models.workflow import WorkflowRun
    from packages.core.workspace_chat.pending_actions import count_pending_actions_by_workspace

    headers = await _register(client, "ws_pending_scope")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    other_workspace = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    parent = HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        origin_kind="lease", status="pending", dedup_key=generate_ulid(),
    )
    nested = HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        origin_kind="tool_call", status="pending", dedup_key=generate_ulid(),
        context={COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY: {"request_id": parent.id}},
    )
    foreign = HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=other_workspace,
        origin_kind="tool_call", status="pending", dedup_key=generate_ulid(),
    )
    db_session.add_all([parent, nested, foreign])
    for request in (parent, nested, foreign):
        card = _card(thread_id, "governance_approval", created_at=BASE)
        card.pending_action = {"kind": "governance_approval", "approval_request_id": request.id}
        db_session.add(card)
    run = WorkflowRun(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        workflow_id=generate_ulid(), status="failed",
    )
    retry = WorkflowRun(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        workflow_id=run.workflow_id, status="running",
        retry_of_run_id=None if legacy_retry else run.id,
        trigger_data={"retry_of_run_id": run.id} if legacy_retry else {},
    )
    db_session.add_all([run, retry])
    card = _card(thread_id, "workflow_retry", created_at=BASE)
    card.pending_action = {"kind": "workflow_retry", "workflow_run_id": run.id}
    db_session.add(card)
    await db_session.flush()
    counts = (await count_pending_actions_by_workspace(db_session, entity_id=me["entity_id"], workspace_ids=[workspace_id]))[workspace_id]
    assert sum(counts.values()) == 1


@pytest.mark.asyncio
async def test_pending_request_without_message_is_counted_and_deduplicates_later_mirror(client, db_session):
    """A lost chat projection cannot hide a durable human decision."""
    from packages.core.constants.execution import COORDINATOR_CHILD_PARAM_KEY
    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    headers = await _register(client, "ws_missing_mirror")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        status="paused", plan_dag={},
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=me["entity_id"], workspace_id=workspace_id,
        step_key="publish", kind="subagent", step_status="waiting_human",
        human_input_prompt="Which screenshot?", params={},
    )
    request = HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        origin_kind="lease", origin_plan_id=plan.id, origin_step_id=step.id,
        status="pending", dedup_key=generate_ulid(), hitl_type="input",
        reason="Which screenshot?", context={"pending_kind": "human_input"},
        payload={"question": "Which screenshot?"},
    )
    child_step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=me["entity_id"], workspace_id=workspace_id,
        step_key="child", kind="subagent", step_status="waiting_human",
        params={COORDINATOR_CHILD_PARAM_KEY: True},
    )
    nested_request = HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        origin_kind="tool_call", origin_step_id=child_step.id,
        status="pending", dedup_key=generate_ulid(),
    )
    db_session.add_all([plan, step, request, child_step, nested_request])
    await db_session.commit()
    page = await _page(client, headers, workspace_id)
    assert page["open_action_count"] == 1
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 1

    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    card = _card(thread_id, "human_input", created_at=BASE)
    card.pending_action = {"kind": "human_input", "approval_request_id": request.id}
    db_session.add(card)
    await db_session.commit()
    mirrored = await _page(client, headers, workspace_id)
    assert mirrored["open_action_count"] == 1

    request.status = "consumed"
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["open_action_count"] == 0


@pytest.mark.asyncio
async def test_proposal_cohort_and_its_item_requests_are_one_review(client, db_session):
    from packages.core.models.proposal import ProposalRecord

    headers = await _register(client, "ws_proposal_cohort_count")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    proposal = ProposalRecord(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        review_id=generate_ulid(), status="open", summary="Review the campaign",
    )
    requests = [HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        origin_kind="operation", status="pending", dedup_key=generate_ulid(),
        context={"plane": "strategist_proposal", "review_id": proposal.review_id, "proposal_id": proposal.id},
    ) for _ in range(3)]
    db_session.add_all([proposal, *requests])
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["open_action_count"] == 1

    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    card = _card(thread_id, "approve_proposals", created_at=BASE)
    card.pending_action = {
        "kind": "approve_proposals", "review_id": proposal.review_id,
        "approval_request_id": requests[0].id,
    }
    db_session.add(card)
    await db_session.commit()
    page = await _page(client, headers, workspace_id)
    assert page["open_action_count"] == _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 1
    proposal.status = "resolved"
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["open_action_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "variables", "expected"), [
    ("failed", {"project": "campaign-a"}, 1),
    ("failed", {"project": {"state": "draft"}}, 1),
    ("completed", {"project": "campaign-a"}, 0),
    ("completed", {"project": {"state": "draft"}}, 0),
    ("completed", {"project": {"state": {"business_outcome": " NEEDS_INPUT "}}}, 1),
    ("completed", {"project": {"state": {"business_outcome": "succeeded"}}}, 0),
])
async def test_workflow_retry_count_accepts_user_variable_shapes(
    client, db_session, status, variables, expected,
):
    from apps.api.routers.workflows import RunStartRequest
    from packages.core.models.workflow import WorkflowRun

    headers = await _register(client, "ws_retry_variables")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    run = WorkflowRun(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        workflow_id=generate_ulid(), status=status,
        variables=RunStartRequest(variables=variables).variables,
    )
    card = _card(thread_id, "workflow_retry", created_at=BASE)
    card.pending_action = {"kind": "workflow_retry", "workflow_run_id": run.id}
    db_session.add_all([run, card])
    await db_session.commit()

    page = await _page(client, headers, workspace_id)
    stats = await _sidebar_stats(client, headers, workspace_id)
    assert page["open_action_count"] == _badge_total(stats) == expected


@pytest.mark.asyncio
async def test_legacy_pending_actions_preserves_missing_task_and_step_fallback(client, db_session):
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    headers = await _register(client, "ws_legacy_pending_fallback")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    tasks = [Task(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        title=f"Waiting task {index}", status="waiting_on_customer",
    ) for index in range(2)]
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        task_id=tasks[0].id, status="paused",
    )
    step = ExecutionStep(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        plan_id=plan.id, step_key="input", kind="human", step_status="waiting_human",
    )
    db_session.add_all([*tasks, plan, step])
    await db_session.commit()

    stats = await _sidebar_stats(client, headers, workspace_id)
    assert stats["pending_actions"] == stats["hitl_tasks"] == 2
    assert _badge_total(stats) == 0

    for expected, action in enumerate((
        {"kind": "human_input", "step_id": step.id, "task_id": tasks[0].id},
        {"kind": "human_input", "task_id": tasks[1].id},
    ), start=1):
        card = _card(thread_id, "human_input", created_at=BASE)
        card.pending_action = action
        db_session.add(card)
        await db_session.commit()
        stats = await _sidebar_stats(client, headers, workspace_id)
        assert stats["pending_actions"] == stats["hitl_tasks"] == 2
        assert _badge_total(stats) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("wait_type", "choice"), [
    ("input", "respond"), ("approval", "approve"),
    ("input", "cancel"), ("approval", "cancel"),
])
async def test_real_workflow_wait_counts_once_and_clears_after_resolution(
    client, monkeypatch, wait_type, choice,
):
    import packages.core.ai.workflow_runner as runner
    import packages.core.database as database
    from test_workspace_workflow_entrypoints import _seed_workspace_entrypoint

    monkeypatch.setattr(runner.WorkflowRunner, "enqueue", staticmethod(lambda *_args, **_kwargs: True))
    monkeypatch.setattr(runner, "async_session", database.async_session)
    wait_config = {
        "wait_type": wait_type,
        "message": "Please respond.",
        "response_variable": "reply",
        "options": ["respond" if wait_type == "input" else "approve", "cancel"],
    }
    if wait_type == "input":
        wait_config["input_schema"] = {
            "type": "object", "properties": {"choice": {"type": "string"}},
            "required": ["choice"],
        }
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client, "count_wait", variables={"request": "", "attachments": [], "reply": None},
        steps=[
            {"id": "start", "type": "trigger", "name": "Start", "config": {}, "next": ["wait"]},
            {"id": "wait", "type": "wait", "name": "Review", "config": wait_config, "next": ["end"]},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    )
    workspace_id = workspace["id"]
    launched = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/entrypoints/{binding['id']}/stream",
        headers=headers, data={"message": "Run this workflow."},
    )
    assert launched.status_code == 200, launched.text
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace_id}", headers=headers,
    )).json()[0]
    await runner.WorkflowRunner().run(run["id"])

    before = await _page(client, headers, workspace_id)
    cards = [item for item in before["items"] if item.get("pending_action")]
    assert len(cards) == 1
    assert cards[0]["hitl_requests"][0]["id"] == cards[0]["id"]
    assert before["open_action_count"] == _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 1

    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{cards[0]['id']}/resolve",
        headers=headers, json={"choice": choice, "note": "Reviewed"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved_at"]
    after = await _page(client, headers, workspace_id)
    assert after["open_action_count"] == _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 0
    updated_run = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert updated_run["status"] == ("cancelled" if choice == "cancel" else "running")


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["approval_token", "message_id"])
async def test_linked_mirror_shares_resolution_without_hiding_independent_requests(client, db_session, link):
    headers = await _register(client, "ws_linked_mirror")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    # A new action kind must obey the same linkage contract as existing kinds.
    card = _card(thread_id, "future_review", created_at=BASE)
    mirror_id = generate_ulid() if link == "approval_token" else card.id
    if link == "approval_token":
        card.pending_action = {**card.pending_action, "approval_token": mirror_id}
    requests = [HitlRequest(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
        origin_kind="tool_call", status="pending", dedup_key=generate_ulid(),
    ) for _ in range(2)]
    card.meta = {"hitl_requests": [
        {"id": mirror_id, "resolved": False},
        *[{"id": request.id, "resolved": False} for request in requests],
    ]}
    db_session.add_all([card, *requests])
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["open_action_count"] == 3

    # The card can be resolved independently of its stale metadata mirror.
    card.resolved_at = BASE + timedelta(minutes=1)
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["open_action_count"] == 2
    await db_session.refresh(card)
    assert card.meta["hitl_requests"][0]["resolved"] is False

    # The other surface can mark the mirror resolved before the card catches up.
    card.resolved_at = None
    card.meta = {"hitl_requests": [
        {"id": mirror_id, "resolved": True},
        *[{"id": request.id, "resolved": False} for request in requests],
    ]}
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["open_action_count"] == 2
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 2
