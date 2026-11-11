"""Runtime and dispatcher share one approval store and exact operation identity.

Preserve cross-origin permission, pending deduplication, exact continuation,
one-time consumption and terminal-state tests through the unified policy API.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from packages.core import database
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approval_policy_service import account_defaults, initialize_personal_approval, initialize_workspace_approval
from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
)


async def _resolve_step_approval(*args, **kwargs):
    kwargs.setdefault(
        "permission_decision",
        PermissionDecision.allow(AuthorizationRule.DISPATCHER_WORKER),
    )
    from packages.core.governance.approvals import resolve_approval

    return await resolve_approval(*args, **kwargs)


class _SessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _fixture(db, *, with_workspace: bool = True, hitl_capability: str | None = None):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    workspace_id = generate_ulid() if with_workspace else None
    db.add(Entity(id=entity_id, name="Unified approval test", settings={"approval_defaults": account_defaults()}))
    db.add(User(
        id=user_id, entity_id=entity_id, email=f"{user_id}@example.com",
        password_hash="x", role="owner", status="active",
    ))
    db.add(UserMembership(
        user_id=user_id,
        entity_id=entity_id,
        role="owner",
        status="active",
        is_primary=True,
    ))
    if workspace_id:
        db.add(Workspace(
            id=workspace_id, entity_id=entity_id, name="Unified WS", operating_model={},
        ))
    db.add(Conversation(
        id=conversation_id, entity_id=entity_id, user_id=user_id,
        workspace_id=workspace_id, title="chat",
    ))
    await db.flush()
    await initialize_personal_approval(db, user_id=user_id, entity_id=entity_id)
    if workspace_id:
        workspace = await db.get(Workspace, workspace_id)
        await initialize_workspace_approval(db, workspace)
        categories = {"external.social": ("publishing", "communication"),
                      "external.email": ("communication",), "automation.manage": ("automation", "deletion"),
                      "file.write": ("files", "deletion")}.get(hitl_capability, ())
        await update_policy(db, entity_id=entity_id, workspace_id=workspace_id,
            policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(mode="automatic",
                rules=[ApprovalRule(id="review-" + category, effect="ask", selector={"kind": "category", "category": category})
                       for category in categories])), changed_by=user_id)
    await db.flush()
    return {
        "entity_id": entity_id, "user_id": user_id,
        "conversation_id": conversation_id, "workspace_id": workspace_id,
    }


async def _guard(ids, arguments, *, name="mcp__twitter_x__create_tweet", operation="primary"):
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action

    return await guard_runtime_tool_action(
        name=name,
        arguments=arguments,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        runtime_envelope=_runtime_envelope(ids, name=name),
        operation_id=f"test:{ids['conversation_id']}:{operation}",
    )


def _runtime_envelope(ids, *, name: str, task_id: str | None = None):
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.constants.runtime_principal import RuntimePrincipalKind

    return RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER,
            entity_id=ids["entity_id"],
            actor_user_id=ids["user_id"],
            execution_user_id=ids["user_id"],
            agent_id=MANOR_AGENT_ID,
            workspace_id=ids["workspace_id"],
        ),
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        agent_id=MANOR_AGENT_ID,
        workspace_id=ids["workspace_id"],
        conversation_id=ids.get("conversation_id"),
        task_id=task_id,
        allowed_tool_names=(name,),
    )


def _task(ids, *, title: str) -> Task:
    return Task(
        id=generate_ulid(),
        entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        creator_id=ids["user_id"],
        title=title,
        status="in_progress",
        task_type="agent",
        details={},
    )


def _granted_social_request(
    ids,
    *,
    arguments: dict,
    origin_task_id: str | None,
) -> HitlRequest:
    from packages.core.ai.runtime.approval_service import approval_args_hash

    request_id = generate_ulid()
    return HitlRequest(
        id=request_id,
        entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"],
        action_key="social_post.publish",
        capability_id="external.social",
        risk_level="high",
        hitl_type="authorize",
        origin_kind="tool_call",
        origin_conversation_id=ids["conversation_id"],
        origin_task_id=origin_task_id,
        status="granted",
        dedup_key=f"operation:test:{request_id}",
        context={
            "tool": "mcp__twitter_x__create_tweet",
            "args_hash": approval_args_hash(arguments),
            "requested_by": ids["user_id"],
            "operation_id": f"test:{request_id}",
            "approval_requirements": [],
        },
    )


def _social_runtime_action():
    from packages.core.ai.runtime.approvals import RuntimeApprovalAction

    return RuntimeApprovalAction(
        kind="action",
        action_key="social_post.publish",
        risk_level="high",
        title="Publish social post",
        capability_id="external.social",
    )


@pytest.mark.asyncio
async def test_guard_token_is_an_approval_request_row(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _fixture(db_session, hitl_capability="external.social")

    blocked = await _guard(ids, {"text": "hello"})
    payload = json.loads(blocked or "{}")
    token = payload["approval_token"]

    row = await db_session.get(HitlRequest, token)
    assert row is not None
    assert row.origin_kind == "tool_call"
    assert row.origin_conversation_id == ids["conversation_id"]
    assert row.status == "pending"
    assert row.action_key == "social_post.publish"
    assert (row.context or {}).get("tool") == "mcp__twitter_x__create_tweet"


@pytest.mark.asyncio
async def test_workspace_agent_initiative_create_is_denied_before_mutation(
    db_session,
    monkeypatch,
):
    """Workspace Chat cannot create private Agent Initiatives."""
    from packages.core.ai.runtime.tool_execution import (
        runtime_execute_registered_tool,
    )
    from packages.core.ai.tools import agent_initiative_tools
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.models.agent_initiative import AgentInitiative
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import AgentSubscription

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    monkeypatch.setattr(
        agent_initiative_tools,
        "async_session",
        lambda: _SessionContext(db_session),
    )
    ids = await _fixture(db_session)
    subscription = AgentSubscription(
        entity_id=ids["entity_id"],
        agent_id=MANOR_AGENT_ID,
        workspace_id=ids["workspace_id"],
        name="Workspace Manor",
        status="active",
    )
    db_session.add(subscription)
    conversation = await db_session.get(Conversation, ids["conversation_id"])
    assert conversation is not None
    conversation.scope = "workspace_main"
    conversation.agent_subscription_id = subscription.id
    await db_session.flush()

    arguments = {
        "name": "Daily workspace briefing",
        "instruction": "Post the daily briefing back to this Workspace Chat.",
        "schedule_kind": "interval",
        "every_seconds": 3600,
        "notification_enabled": False,
    }
    envelope = _runtime_envelope(ids, name="create_agent_initiative")

    async def execute(call_arguments):
        return await runtime_execute_registered_tool(
            tool_name="create_agent_initiative",
            arguments=dict(call_arguments),
            handler_resolver=lambda _name: agent_initiative_tools._create_handler,
            runtime_envelope=envelope,
        )

    blocked = json.loads(await execute(arguments))
    assert blocked["error"] == "tool_not_in_runtime_surface"
    assert list((await db_session.execute(select(AgentInitiative).where(AgentInitiative.entity_id == ids["entity_id"]))).scalars()) == []
    assert list((await db_session.execute(select(ScheduledJob).where(ScheduledJob.entity_id == ids["entity_id"]))).scalars()) == []
    assert list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.entity_id == ids["entity_id"])
    )).scalars()) == []


@pytest.mark.asyncio
async def test_guard_dedups_identical_call_to_one_open_request(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _fixture(db_session, hitl_capability="external.social")

    first = json.loads(await _guard(ids, {"text": "hello"}) or "{}")
    second = json.loads(await _guard(ids, {"text": "hello"}) or "{}")
    assert first["approval_token"] == second["approval_token"]

    open_rows = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == ids["entity_id"],
            HitlRequest.status == "pending",
        )
    )).scalars().all()
    assert len(open_rows) == 1


@pytest.mark.asyncio
async def test_chat_approve_once_grants_consumes_then_reasks(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_message

    ids = await _fixture(db_session, hitl_capability="external.social")
    args = {"text": "ship it"}

    blocked = json.loads(await _guard(ids, dict(args)) or "{}")
    token = blocked["approval_token"]

    message = await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=token, action="approve",
    )
    assert message and "Retry the exact same tool call" in message
    assert (await db_session.get(HitlRequest, token)).status == "granted"

    retry_args = {**args, "approval_token": token}
    assert await _guard(ids, retry_args) is None          # consumed, runs
    assert "approval_token" not in retry_args
    assert (await db_session.get(HitlRequest, token)).status == "consumed"

    third = await _guard(ids, dict(args), operation="new-intent")                 # one-time: re-asks
    third_payload = json.loads(third or "{}")
    assert third_payload["__hitl__"] is True
    assert third_payload["approval_token"] != token


@pytest.mark.asyncio
async def test_chat_approval_resumes_exact_typed_arguments_without_model_rewrite(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn

    ids = await _fixture(db_session, hitl_capability="external.email")
    args = {
        "to": "reviewer@example.test",
        "subject": "Candidate",
        "body": "Please review.",
        "attachments": [{"filename": "candidate.txt", "data_base64": "YQ=="}],
    }
    blocked = json.loads(
        await _guard(ids, dict(args), name="mcp__email__send_email") or "{}"
    )

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=blocked["approval_token"],
        action="approve",
    )

    assert resolution is not None
    assert resolution.runtime_metadata is not None
    forced = resolution.runtime_metadata["forced_tool_calls"]
    assert len(forced) == 1
    assert forced[0]["name"] == "mcp__email__send_email"
    assert forced[0]["arguments"]["attachments"] == [
        {"filename": "candidate.txt", "data_base64": "YQ=="}
    ]
    assert forced[0]["arguments"]["approval_token"] == blocked["approval_token"]


@pytest.mark.asyncio
async def test_legacy_invalid_email_approval_expires_without_consuming_or_replaying(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn

    ids = await _fixture(db_session, hitl_capability="external.email")
    args = {
        "to": "reviewer@example.test",
        "subject": "Candidate",
        "body": "Please review.",
        "attachments": '[{"document_id":"document-1"}]',
    }
    blocked = json.loads(
        await _guard(ids, dict(args), name="mcp__email__send_email") or "{}"
    )
    token = blocked["approval_token"]

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=token,
        action="approve",
    )

    assert resolution is not None
    assert resolution.runtime_metadata is None
    assert "stored tool input is invalid" in resolution.message
    request = await db_session.get(HitlRequest, token)
    assert request.status == "expired"
    assert request.resolved_reason == "approved_tool_input_invalid"
    assert request.consumed_at is None


@pytest.mark.asyncio
async def test_workspace_exact_setting_is_honored_by_both_execution_planes(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_message
    from packages.core.governance.approvals import ApprovalOrigin, ApprovalSubject
    ids = await _fixture(db_session, hitl_capability="external.social")
    blocked = json.loads(await _guard(ids, {"text": "hello"}) or "{}")
    assert blocked["hitl"]["options"] == ["approve", "approve_and_save_rule", "reject"]
    with pytest.raises(ValueError, match="[Aa]lways|approval.settings"):
        await resolve_runtime_approval_message(db_session,
            conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
            user_id=ids["user_id"], hitl_id=blocked["approval_token"], action="always_approve")
    assert (await db_session.get(HitlRequest, blocked["approval_token"])).status == "pending"
    await set_workspace_test_approval(db_session, workspace_id=ids["workspace_id"],
        entity_id=ids["entity_id"], actor_id=ids["user_id"],
        user_rules=[action_rule("social_post.publish")])
    assert json.loads(await _guard(ids, {"text": "hello"}))["approval_token"] == blocked["approval_token"]
    assert await _guard(ids, {"text": "another post"}, operation="new-intent") is None
    step_decision = await _resolve_step_approval(db_session,
        subject=ApprovalSubject(entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
            action_key="social_post.publish", capability_id="external.social", risk_level="high", kind="action"),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid(), operation_id="test:new-step"))
    assert step_decision.outcome == "allow"


@pytest.mark.asyncio
async def test_legacy_blob_token_is_ignored_and_gate_decides_fresh(db_session, monkeypatch):
    """The pre-upgrade conv.meta blob is no longer a decision store: its token
    is ignored (stripped) and the unified gate decides the call on its own."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_messages import approval_args_hash

    ids = await _fixture(db_session)
    legacy_token = generate_ulid()
    args = {"text": "legacy approved post"}
    conv = await db_session.get(Conversation, ids["conversation_id"])
    conv.meta = {
        "runtime_approvals": {
            legacy_token: {
                "id": legacy_token,
                "status": "approved",
                "tool": "mcp__twitter_x__create_tweet",
                "action_key": "social_post.publish",
                "args_hash": approval_args_hash(args),
                "requested_by": ids["user_id"],
            },
        },
    }
    await db_session.flush()

    retry_args = {**args, "approval_token": legacy_token}
    # No policy gates this workspace → the fresh decision is allow; the stale
    # token is stripped rather than honored.
    assert await _guard(ids, retry_args) is None
    assert "approval_token" not in retry_args


@pytest.mark.asyncio
async def test_legacy_blob_card_click_is_ignored(db_session, monkeypatch):
    """A legacy blob-only card is no longer actionable."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn

    ids = await _fixture(db_session)
    legacy_token = generate_ulid()
    conv = await db_session.get(Conversation, ids["conversation_id"])
    conv.meta = {
        "runtime_approvals": {
            legacy_token: {
                "id": legacy_token,
                "status": "pending",
                "tool": "mcp__twitter_x__create_tweet",
                "action_key": "social_post.publish",
                "requested_by": ids["user_id"],
            },
        },
    }
    await db_session.flush()

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=legacy_token,
        action="approve",
    )
    assert resolution is None
    refreshed = await db_session.get(Conversation, ids["conversation_id"])
    assert refreshed.meta["runtime_approvals"][legacy_token]["status"] == "pending"


@pytest.mark.asyncio
async def test_step_origin_request_is_invisible_to_runtime_resolver(db_session, monkeypatch):
    """A dispatcher card's request id must fall through the runtime resolver
    (returns None) so the chat resolver chain routes it to the right handler."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn
    from packages.core.governance.approvals import ApprovalOrigin, ApprovalSubject

    ids = await _fixture(db_session, hitl_capability="external.social")

    step_decision = await _resolve_step_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid(), operation_id="test:step"),
    )
    assert step_decision.outcome == "needs_human"
    assert step_decision.request is not None

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=step_decision.request.id, action="approve",
    )
    assert resolution is None
    assert (await db_session.get(HitlRequest, step_decision.request.id)).status == "pending"


@pytest.mark.asyncio
async def test_cancel_expires_open_runtime_requests(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import cancel_pending_runtime_approvals

    ids = await _fixture(db_session, hitl_capability="external.social")
    blocked = json.loads(await _guard(ids, {"text": "hello"}) or "{}")
    token = blocked["approval_token"]

    cancelled = await cancel_pending_runtime_approvals(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"],
    )
    assert cancelled == 1
    row = await db_session.get(HitlRequest, token)
    assert row.status == "expired"
    assert row.resolved_reason == "request_stopped"


@pytest.mark.asyncio
async def test_chat_cancel_does_not_revoke_same_conversation_task_request(db_session):
    from packages.core.ai.runtime.approval_service import (
        cancel_pending_runtime_approvals,
    )

    ids = await _fixture(db_session)
    task = _task(ids, title="Scheduled Task")
    direct_request = _granted_social_request(
        ids,
        arguments={"text": "direct pending"},
        origin_task_id=None,
    )
    direct_request.status = "pending"
    task_request = _granted_social_request(
        ids,
        arguments={"text": "task pending"},
        origin_task_id=task.id,
    )
    task_request.status = "pending"
    db_session.add_all([task, direct_request, task_request])
    await db_session.flush()

    cancelled = await cancel_pending_runtime_approvals(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_ids=[direct_request.id, task_request.id],
    )

    assert cancelled == 1
    await db_session.refresh(direct_request)
    await db_session.refresh(task_request)
    await db_session.refresh(task)
    assert direct_request.status == "expired"
    assert direct_request.resolved_reason == "request_stopped"
    assert task_request.status == "pending"
    assert task_request.resolved_reason is None
    assert task.status == "in_progress"



@pytest.mark.asyncio
async def test_explicit_chat_cancel_advances_cross_process_turn_generation(db_session):
    from packages.core.services.chat_approvals import (
        CHAT_TURN_CANCEL_GENERATION_KEY,
        cancel_chat_approvals,
        chat_turn_cancellation_requested,
        get_chat_turn_cancel_generation,
    )

    ids = await _fixture(db_session)
    before = await get_chat_turn_cancel_generation(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
    )
    cancelled = await cancel_chat_approvals(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        cancel_turn=True,
    )

    after = await get_chat_turn_cancel_generation(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
    )
    conversation = await db_session.get(Conversation, ids["conversation_id"])
    assert cancelled["cancel_requested"] == 1
    assert after == before + 1
    assert conversation.meta[CHAT_TURN_CANCEL_GENERATION_KEY] == after
    assert await chat_turn_cancellation_requested(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        generation=before,
    )


# ── adversarial-review fixes ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tokenless_identical_retry_consumes_the_grant_once(db_session, monkeypatch):
    """THE review catch: after a one-time Approve, an identical call WITHOUT
    the approval_token must consume the grant — run once, then reject replay.
    Without consume-on-allow, 'approve once' was approve-forever."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_message

    ids = await _fixture(db_session, hitl_capability="external.social")
    args = {"text": "one-time post"}

    blocked = json.loads(await _guard(ids, dict(args)) or "{}")
    token = blocked["approval_token"]
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=token, action="approve",
    )

    # token-LESS identical retry: allowed exactly once, grant spent
    assert await _guard(ids, dict(args)) is None
    assert (await db_session.get(HitlRequest, token)).status == "consumed"

    # The same operation cannot replay; only a new intent gets a fresh request.
    replay = json.loads(await _guard(ids, dict(args)) or "{}")
    assert replay["error"] == "blocked_by_governance"
    assert replay["matched_rule"] == "approval.operation_terminal"
    reask = json.loads(await _guard(ids, dict(args), operation="new-intent") or "{}")
    assert reask["__hitl__"] is True
    assert reask["approval_token"] != token


@pytest.mark.asyncio
async def test_concurrent_tokenless_retries_have_one_grant_consumer(
    db_session,
    monkeypatch,
):
    """The stale-token compatibility lookup must serialize on the matched row."""

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.ai.runtime.approval_service import (
        consume_runtime_approval,
        resolve_runtime_approval_message,
    )
    from packages.core.ai.runtime.approvals import RuntimeApprovalAction

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _fixture(db_session, hitl_capability="external.social")
    args = {"text": "one concurrent post"}
    blocked = json.loads(await _guard(ids, dict(args)) or "{}")
    token = blocked["approval_token"]
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=token,
        action="approve",
    )
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    action = RuntimeApprovalAction(
        kind="action",
        action_key="social_post.publish",
        risk_level="high",
        title="Publish social post",
        capability_id="external.social",
    )
    permission = PermissionDecision.allow(AuthorizationRule.HITL_REQUEST_OWNER)

    async def consume_once() -> str | None:
        async with session_factory() as worker_db:
            result = await consume_runtime_approval(
                worker_db,
                conversation_id=ids["conversation_id"],
                entity_id=ids["entity_id"],
                user_id=ids["user_id"],
                hitl_id=token,
                tool_name="mcp__twitter_x__create_tweet",
                arguments=dict(args),
                action=action,
                permission_decision=permission,
            )
            await worker_db.commit()
            return result

    import asyncio

    outcomes = await asyncio.gather(consume_once(), consume_once())
    assert outcomes.count(None) == 1
    async with session_factory() as check_db:
        assert (await check_db.get(HitlRequest, token)).status == "consumed"


@pytest.mark.asyncio
async def test_foreign_task_token_cannot_fail_its_origin_on_payload_mismatch(
    db_session,
):
    """A Task-A retry cannot mutate Task B before rejecting B's token."""

    from packages.core.ai.runtime.approval_service import consume_runtime_approval

    ids = await _fixture(db_session)
    task_a = _task(ids, title="Task A")
    task_b = _task(ids, title="Task B")
    request = _granted_social_request(
        ids,
        arguments={"text": "approved for Task B"},
        origin_task_id=task_b.id,
    )
    db_session.add_all([task_a, task_b, request])
    await db_session.flush()

    result = await consume_runtime_approval(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        task_id=task_a.id,
        hitl_id=request.id,
        tool_name="mcp__twitter_x__create_tweet",
        arguments={"text": "changed by Task A"},
        action=_social_runtime_action(),
        permission_decision=PermissionDecision.allow(
            AuthorizationRule.HITL_REQUEST_OWNER
        ),
    )

    assert json.loads(result or "{}")["error"] == "approval_origin_mismatch"
    await db_session.refresh(request)
    await db_session.refresh(task_b)
    assert request.status == "granted"
    assert request.resolved_reason is None
    assert task_b.status == "in_progress"


@pytest.mark.asyncio
async def test_task_cannot_consume_direct_chat_grant_by_exact_token(db_session):
    from packages.core.ai.runtime.approval_service import consume_runtime_approval

    ids = await _fixture(db_session)
    task = _task(ids, title="Task")
    arguments = {"text": "direct-chat approval"}
    request = _granted_social_request(
        ids,
        arguments=arguments,
        origin_task_id=None,
    )
    db_session.add_all([task, request])
    await db_session.flush()

    result = await consume_runtime_approval(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        task_id=task.id,
        hitl_id=request.id,
        tool_name="mcp__twitter_x__create_tweet",
        arguments=dict(arguments),
        action=_social_runtime_action(),
        permission_decision=PermissionDecision.allow(
            AuthorizationRule.HITL_REQUEST_OWNER
        ),
    )

    assert json.loads(result or "{}")["error"] == "approval_origin_mismatch"
    await db_session.refresh(request)
    assert request.status == "granted"
    assert request.consumed_at is None


@pytest.mark.asyncio
async def test_task_unknown_token_cannot_fallback_to_direct_chat_grant(db_session):
    from packages.core.ai.runtime.approval_service import consume_runtime_approval

    ids = await _fixture(db_session)
    task = _task(ids, title="Task")
    arguments = {"text": "same payload, direct approval"}
    direct_request = _granted_social_request(
        ids,
        arguments=arguments,
        origin_task_id=None,
    )
    db_session.add_all([task, direct_request])
    await db_session.flush()

    unknown_token = generate_ulid()
    result = await consume_runtime_approval(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        task_id=task.id,
        hitl_id=unknown_token,
        tool_name="mcp__twitter_x__create_tweet",
        arguments=dict(arguments),
        action=_social_runtime_action(),
        permission_decision=PermissionDecision.allow(
            AuthorizationRule.HITL_REQUEST_OWNER
        ),
    )

    payload = json.loads(result or "{}")
    assert payload == {
        "error": "approval_not_granted",
        "status": "not_found",
        "message": (
            "The Task approval token is missing or no longer valid. "
            "No action was run."
        ),
        "approval_token": unknown_token,
    }
    await db_session.refresh(direct_request)
    assert direct_request.status == "granted"
    assert direct_request.consumed_at is None


@pytest.mark.asyncio
async def test_task_cannot_execute_direct_chat_provider_grant(db_session):
    from packages.core.ai.runtime.approval_service import (
        approval_args_hash,
        prepare_provider_runtime_approval_execution,
        settle_provider_runtime_approval_execution,
    )
    from packages.core.ai.runtime.provider_approvals import (
        normalize_provider_approval,
    )

    ids = await _fixture(db_session)
    task = _task(ids, title="Provider Task")
    retry_tool = "mcp__chrome__click_element"
    retry_arguments = {"tabId": 1, "ref": "e1"}
    provider_approval_id = "direct-provider-origin"
    continuation = normalize_provider_approval(
        retry_tool,
        retry_arguments,
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "action_key": "click_element",
            "approvalId": provider_approval_id,
            "expires_at": "2099-01-01T00:00:00Z",
            "reason": "side_effect_action_requires_confirmation",
            "confirmation_mode": "always_action_time",
            "policy_category": "representational_communication",
            "preapproved": False,
            "retry_action": {
                "name": retry_tool,
                "arguments": retry_arguments,
            },
        },
    )
    assert continuation is not None
    request_id = generate_ulid()
    request = HitlRequest(
        id=request_id,
        entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"],
        action_key="click_element",
        capability_id="chrome.action",
        risk_level="high",
        hitl_type="authorize",
        origin_kind="tool_call",
        origin_conversation_id=ids["conversation_id"],
        origin_task_id=None,
        status="granted",
        dedup_key=f"provider:chrome:{provider_approval_id}",
        context={
            "kind": "provider",
            "provider": "chrome",
            "provider_approval_id": provider_approval_id,
            "tool": retry_tool,
            "args_hash": approval_args_hash(retry_arguments),
            "requested_by": ids["user_id"],
            "continuation": continuation,
        },
    )
    db_session.add_all([task, request])
    await db_session.flush()

    preflight = await prepare_provider_runtime_approval_execution(
        db_session,
        runtime_metadata={
            "provider_approval_execution": {
                "hitl_id": request.id,
                "confirmation_tool": continuation["confirmation_tool"],
                "retry_tool": continuation["retry_tool"],
            }
        },
        entity_id=ids["entity_id"],
        conversation_id=ids["conversation_id"],
        task_id=task.id,
        tool_name=continuation["confirmation_tool"],
        arguments=dict(continuation["confirmation_arguments"]),
    )

    payload = json.loads(preflight.result or "{}")
    assert payload["error"] == "provider_approval_execution_blocked"
    assert payload["status"] == "invalid"
    assert preflight.changed is False
    settled = await settle_provider_runtime_approval_execution(
        db_session,
        runtime_metadata={
            "provider_approval_execution": {
                "hitl_id": request.id,
                "confirmation_tool": continuation["confirmation_tool"],
                "retry_tool": continuation["retry_tool"],
            }
        },
        entity_id=ids["entity_id"],
        conversation_id=ids["conversation_id"],
        task_id=task.id,
        tool_name=continuation["confirmation_tool"],
        arguments=dict(continuation["confirmation_arguments"]),
        result=preflight.result,
    )
    assert settled is False
    await db_session.refresh(request)
    assert request.status == "granted"
    assert "provider_confirmation_receipt" not in request.context
    assert "provider_action_claim" not in request.context


@pytest.mark.asyncio
async def test_approval_expires_when_actor_scoped_schema_is_unavailable(
    db_session,
    monkeypatch,
):
    from packages.core.ai.runtime import tool_registry
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _fixture(db_session, hitl_capability="external.email")
    args = {
        "to": "reviewer@example.test",
        "subject": "Candidate",
        "body": "Please review.",
    }
    blocked = json.loads(
        await _guard(ids, dict(args), name="mcp__email__send_email") or "{}"
    )

    async def unavailable_schema(name: str, *, entity_id: str, user_id: str):
        assert name == "mcp__email__send_email"
        assert entity_id == ids["entity_id"]
        assert user_id == ids["user_id"]
        return None

    monkeypatch.setattr(
        tool_registry,
        "runtime_tool_schema_for_actor",
        unavailable_schema,
    )
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=blocked["approval_token"],
        action="approve",
    )

    assert resolution.runtime_metadata is None
    assert "current tool schema is unavailable" in resolution.message
    request = await db_session.get(HitlRequest, blocked["approval_token"])
    assert request.status == "expired"
    assert request.resolved_reason == "approved_tool_schema_unavailable"


@pytest.mark.asyncio
async def test_saved_exception_scope_is_exact_action_not_the_capability(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _fixture(db_session, hitl_capability="automation.manage")
    await set_workspace_test_approval(db_session, workspace_id=ids["workspace_id"],
        entity_id=ids["entity_id"], actor_id=ids["user_id"],
        user_rules=[action_rule("workspace.automation.create")])
    assert await _guard(ids, {"name": "job", "schedule_kind": "cron", "cron_expr": "0 * * * *", "payload_message": "run"}, name="create_scheduled_job") is None
    blocked = json.loads(await _guard(ids, {"job_id": "job-1"}, name="cancel_scheduled_job", operation="delete-intent"))
    assert blocked["__hitl__"] is True
    assert blocked["hitl"]["action"] == "workspace.automation.delete"


@pytest.mark.asyncio
async def test_task_runtime_rules_gate_the_runtime_plane_too(db_session, monkeypatch):
    """tool_call origins carry task_id, so task-scoped runtime rules
    (approval_required / deny) bind chat tool calls, not just plan steps."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.models.task import Task

    ids = await _fixture(db_session)  # no policy rule — task rule is the only gate
    task_id = generate_ulid()
    db_session.add(Task(
        id=task_id, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        agent_id=MANOR_AGENT_ID,
        title="rule-bound task",
        details={"runtime_context": {"rules": [{
            "rule_key": "review_automation",
            "rule_type": "approval_required",
            "description": "Automation creation needs review in this task.",
            "approval_selectors": [{"kind": "action", "action_key": "workspace.automation.create"}],
        }]}},
    ))
    await db_session.flush()

    blocked = await guard_runtime_tool_action(
        name="create_scheduled_job",
        operation_id=f"task:{task_id}:create-automation",
        arguments={"name": "task-scoped", "cron": "0 * * * *", "prompt": "run"},
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        task_id=task_id,
        runtime_envelope=_runtime_envelope(
            ids,
            name="create_scheduled_job",
            task_id=task_id,
        ),
    )
    payload = json.loads(blocked or "{}")
    assert payload.get("__hitl__") is True, payload
    assert payload["operation"]["matched_rule"] == "review_automation"
    row = await db_session.get(HitlRequest, payload["approval_token"])
    assert row.origin_task_id == task_id


@pytest.mark.asyncio
async def test_terminal_origin_revokes_granted_unconsumed_requests(db_session):
    """A grant whose origin died must be revoked, not left findable forever."""
    from packages.core.governance.approvals import (
        grant_approval, resolve_origin_requests,
        ApprovalOrigin, ApprovalSubject,
    )

    ids = await _fixture(db_session, hitl_capability="external.social")
    entity_id, workspace_id = ids["entity_id"], ids["workspace_id"]
    step_id = generate_ulid()
    d = await _resolve_step_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=entity_id, workspace_id=workspace_id,
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=step_id, operation_id=f"step:{step_id}"),
    )
    await grant_approval(db_session, d.request, by_user_id=ids["user_id"], via="chat_card")
    assert d.request.status == "granted"

    closed = await resolve_origin_requests(db_session, step_id=step_id)
    assert closed == 1
    assert d.request.status == "expired"


@pytest.mark.asyncio
async def test_reply_approve_on_legacy_blob_pending_gets_tombstone(db_session, monkeypatch):
    """Typing 'approve' at a pre-upgrade card must return the tombstone
    notice, not silently fall through as free text."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import (
        resolve_pending_runtime_approval_turn_from_reply,
    )

    ids = await _fixture(db_session)
    legacy_token = generate_ulid()
    conv = await db_session.get(Conversation, ids["conversation_id"])
    conv.meta = {
        "runtime_approvals": {
            legacy_token: {
                "id": legacy_token,
                "status": "pending",
                "tool": "mcp__twitter_x__create_tweet",
                "action_key": "social_post.publish",
                "requested_by": ids["user_id"],
            },
        },
    }
    await db_session.flush()

    resolution = await resolve_pending_runtime_approval_turn_from_reply(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        message="approve",
    )
    assert resolution is None


@pytest.mark.asyncio
async def test_retry_while_still_pending_re_surfaces_the_same_card(
    db_session,
    monkeypatch,
):
    """The second half of the workspace-email incident.

    The model retried the gated tool with the token before the user had
    answered. The guard returned a bare
    ``{"error": "approval_not_granted", "status": "pending"}`` — not a
    ``__hitl__`` envelope, so ``chat_service`` recorded no card — and the model
    then told the user "已重试，新的审批卡已经生成。请在新的 approval card 上点
    Approve". No such card existed, and the one real card was (at the time)
    invisible in workspace chat anyway.

    A retry-while-pending must re-surface THE SAME request as a real envelope:
    the user gets something to click, and the model's sentence stops being a
    lie. Critically it must not mint a second request — two open rows for one
    blocked call would double the badge and let a user "approve" the one that
    is not holding the call.
    """
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _fixture(db_session, hitl_capability="external.social")
    args = {"text": "ship it"}

    blocked = json.loads(await _guard(ids, dict(args)) or "{}")
    token = blocked["approval_token"]
    assert blocked["hitl"]["options"] == ["approve", "approve_and_save_rule", "reject"]
    assert (await db_session.get(HitlRequest, token)).status == "pending"

    # The model jumps the gun and retries with the token, still unanswered.
    retried = json.loads(await _guard(ids, {**args, "approval_token": token}) or "{}")

    # A real card, not a dead error.
    assert retried.get("__hitl__") is True, "a pending retry produced no card"
    assert retried.get("error") == "approval_required"
    # The SAME request — resolving it unblocks the call that is actually stuck.
    assert retried["approval_token"] == token
    assert retried["hitl"]["id"] == token
    assert retried["hitl"]["options"] == blocked["hitl"]["options"]
    # And the card is rendered from the context stored at mint time.
    assert retried["hitl"]["action"] == "social_post.publish"
    assert retried["hitl"]["tool"] == "mcp__twitter_x__create_tweet"

    # No second request was minted.
    open_rows = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == ids["entity_id"],
            HitlRequest.status == "pending",
        )
    )).scalars().all()
    assert [row.id for row in open_rows] == [token]


@pytest.mark.asyncio
async def test_retry_after_rejection_still_reports_the_denial(db_session, monkeypatch):
    """The pending re-surface must not swallow a real verdict: a rejected
    token still returns the terminal error, or a refused action would loop
    back into asking the user again."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_message

    ids = await _fixture(db_session, hitl_capability="external.social")
    args = {"text": "do not ship"}

    token = json.loads(await _guard(ids, dict(args)) or "{}")["approval_token"]
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=token, action="reject",
    )
    assert (await db_session.get(HitlRequest, token)).status == "denied"

    after = json.loads(await _guard(ids, {**args, "approval_token": token}) or "{}")
    assert after["error"] == "approval_not_granted"
    assert after["status"] == "rejected"
    assert "__hitl__" not in after
