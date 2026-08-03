"""Step 3 of the unified approval rewrite: the runtime tool guard runs on the
same HitlRequest store as the dispatcher step gate.

These tests pin the CROSS-PLANE claims that motivated the rewrite:

  * the guard's approval_token IS a HitlRequest id (one store, no blob);
  * "Always approve" on a workspace chat card writes the workspace policy
    auto-approve set — which the DISPATCHER honors too (the two-Always fix);
  * a one-time chat approval is consumed by the retry and a third identical
    call re-asks;
  * re-tripping the guard for the identical call reuses ONE open request
    (badge honesty);
  * legacy blob approvals (paused before the deploy) still resolve/consume
    via the fallback shim;
  * a step-origin request id is invisible to the runtime resolver (foreign
    ids fall through the chat resolver chain).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from packages.core import database
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.models.workspace import Workspace


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
    db.add(User(
        id=user_id, entity_id=entity_id, email=f"{user_id}@example.com",
        password_hash="x", role="owner",
    ))
    if workspace_id:
        db.add(Workspace(
            id=workspace_id, entity_id=entity_id, name="Unified WS", operating_model={},
        ))
    db.add(Conversation(
        id=conversation_id, entity_id=entity_id, user_id=user_id,
        workspace_id=workspace_id, title="chat",
    ))
    if workspace_id and hitl_capability:
        await update_policy(
            db, entity_id=entity_id, workspace_id=workspace_id,
            policy=WorkspacePolicy(hitl_required_capabilities=[hitl_capability]),
            changed_by="t",
        )
    await db.flush()
    return {
        "entity_id": entity_id, "user_id": user_id,
        "conversation_id": conversation_id, "workspace_id": workspace_id,
    }


async def _guard(ids, arguments, *, name="mcp__twitter_x__create_tweet"):
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action

    return await guard_runtime_tool_action(
        name=name,
        arguments=arguments,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
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

    third = await _guard(ids, dict(args))                 # one-time: re-asks
    third_payload = json.loads(third or "{}")
    assert third_payload["__hitl__"] is True
    assert third_payload["approval_token"] != token


@pytest.mark.asyncio
async def test_workspace_always_is_honored_by_the_dispatcher_gate_too(db_session, monkeypatch):
    """THE unification: 'Always approve' on a runtime chat card writes the
    workspace policy auto-approve set, so the DISPATCHER's step gate allows
    the same subject — one Always, both planes.

    The subject is publish-class on purpose. A capability tier that made
    publish/email/message approvable-once-but-never-blanket shipped briefly and
    was rejected: "Always" means the user wants always. This is the end-to-end
    proof that clicking it on a publish card yields a standing grant a later
    identical call sails through on."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_message
    from packages.core.governance.approvals import ApprovalOrigin, ApprovalSubject, resolve_approval

    ids = await _fixture(db_session, hitl_capability="external.social")

    blocked = json.loads(await _guard(ids, {"text": "hello"}) or "{}")
    # OFFERED, not just honored: the publish card has to carry the button the
    # user clicks, or none of the rest of this is reachable from the screen.
    assert blocked["hitl"]["options"] == ["approve", "always_approve", "reject"]
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=blocked["approval_token"],
        action="always_approve",
    )

    # runtime plane: next identical call sails through on the standing grant
    assert await _guard(ids, {"text": "another post"}) is None

    # dispatcher plane: a STEP with the same subject is allowed by the same store
    step_decision = await resolve_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
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
async def test_legacy_blob_card_click_gets_tombstone(db_session, monkeypatch):
    """Clicking a card minted before the upgrade closes it with an upgrade
    notice — the retry then goes through the unified gate for a fresh card."""
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
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=legacy_token, action="approve",
    )
    assert resolution is not None
    assert "predates the approval-system upgrade" in resolution.message
    refreshed = await db_session.get(Conversation, ids["conversation_id"])
    assert refreshed.meta["runtime_approvals"][legacy_token]["status"] == "expired"


@pytest.mark.asyncio
async def test_step_origin_request_is_invisible_to_runtime_resolver(db_session, monkeypatch):
    """A dispatcher card's request id must fall through the runtime resolver
    (returns None) so the chat resolver chain routes it to the right handler."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn
    from packages.core.governance.approvals import ApprovalOrigin, ApprovalSubject, resolve_approval

    ids = await _fixture(db_session)

    step_decision = await resolve_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
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


# ── adversarial-review fixes ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tokenless_identical_retry_consumes_the_grant_once(db_session, monkeypatch):
    """THE review catch: after a one-time Approve, an identical call WITHOUT
    the approval_token must consume the grant — run once, then re-ask.
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

    # second token-less identical call: re-asks with a fresh request
    reask = json.loads(await _guard(ids, dict(args)) or "{}")
    assert reask["__hitl__"] is True
    assert reask["approval_token"] != token


@pytest.mark.asyncio
async def test_standing_grant_scope_is_the_displayed_action_not_the_capability(db_session, monkeypatch):
    """'Always approve' on one action must not silently pre-approve every
    other action in the same capability family."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_message

    ids = await _fixture(db_session, hitl_capability="automation.manage")

    create_args = {"name": "job", "cron": "0 * * * *", "prompt": "run"}
    blocked = json.loads(await _guard(
        ids, dict(create_args), name="create_scheduled_job",
    ) or "{}")
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=blocked["approval_token"],
        action="always_approve",
    )

    # same action → standing-approved
    assert await _guard(ids, dict(create_args), name="create_scheduled_job") is None
    # DIFFERENT action, same capability (automation.manage) → still asks
    cancel_blocked = await _guard(
        ids, {"job_id": "job-1"}, name="cancel_scheduled_job",
    )
    payload = json.loads(cancel_blocked or "{}")
    assert payload["__hitl__"] is True
    assert payload["hitl"]["action"] == "workspace.automation.delete"


@pytest.mark.asyncio
async def test_task_runtime_rules_gate_the_runtime_plane_too(db_session, monkeypatch):
    """tool_call origins carry task_id, so task-scoped runtime rules
    (approval_required / deny) bind chat tool calls, not just plan steps."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
    from packages.core.models.task import Task

    ids = await _fixture(db_session)  # no policy rule — task rule is the only gate
    task_id = generate_ulid()
    db_session.add(Task(
        id=task_id, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        title="rule-bound task",
        details={"runtime_context": {"rules": [{
            "rule_key": "review_social",
            "rule_type": "approval_required",
            "description": "Social publishes need review in this task.",
            "action_patterns": ["social_post.publish"],
        }]}},
    ))
    await db_session.flush()

    blocked = await guard_runtime_tool_action(
        name="mcp__twitter_x__create_tweet",
        arguments={"text": "task-scoped"},
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        task_id=task_id,
    )
    payload = json.loads(blocked or "{}")
    assert payload["__hitl__"] is True
    assert payload["operation"]["matched_rule"] == "review_social"
    row = await db_session.get(HitlRequest, payload["approval_token"])
    assert row.origin_task_id == task_id


@pytest.mark.asyncio
async def test_terminal_origin_revokes_granted_unconsumed_requests(db_session):
    """A grant whose origin died must be revoked, not left findable forever."""
    from packages.core.governance.approvals import (
        grant_approval, resolve_approval, resolve_origin_requests,
        ApprovalOrigin, ApprovalSubject,
    )
    from packages.core.models.workspace import Workspace

    entity_id, workspace_id = generate_ulid(), generate_ulid()
    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="WS", operating_model={}))
    await db_session.flush()
    step_id = generate_ulid()
    d = await resolve_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=entity_id, workspace_id=workspace_id,
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=step_id),
    )
    await grant_approval(db_session, d.request, by_user_id="op", via="chat_card")
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
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], message="approve",
    )
    assert resolution is not None
    assert "predates the approval-system upgrade" in resolution.message
