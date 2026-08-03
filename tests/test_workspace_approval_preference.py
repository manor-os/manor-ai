"""Regression: "Always approve" must actually stick in a workspace chat.

Creating an automation (`create_scheduled_job` → action ``workspace.automation.create``,
capability ``automation.manage``, risk ``high``) in a workspace whose governance
policy HITL-gates automation used to loop forever: the approval card's
"Always approve" wrote a user *preference* that the guard's workspace branch
never read — every retry re-paused and minted a brand-new card.

Under the unified approval core there is ONE standing store for workspace
conversations: the workspace policy auto-approve set. "Always approve" on the
card writes it (``grant_approval(standing=True)``), and both the runtime guard
and the dispatcher step gate honor it through ``resolve_approval``. Hard
governance blocks (``never_allow`` / risk ceiling / budget caps) return
``deny`` without minting a request and can never be approved past.
"""

import json

import pytest

from packages.core import database
from packages.core.ai.runtime.approval_service import (
    guard_runtime_tool_action as guard_workspace_tool_action,
    resolve_runtime_approval_message,
)
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.models.workspace import Workspace


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


_CREATE_ARGS = {
    "name": "hourly X post runner health check",
    "cron": "0 * * * *",
    "prompt": "Check the runner is healthy.",
}


async def _setup_workspace(
    db_session,
    *,
    policy: WorkspacePolicy,
) -> dict[str, str]:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    workspace_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        User(
            id=user_id,
            entity_id=entity_id,
            email=f"{user_id}@example.com",
            password_hash="x",
            role="owner",
        )
    )
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Automation Workspace",
            operating_model={},
        )
    )
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            title="Workspace chat",
        )
    )
    await update_policy(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=policy,
        changed_by=user_id,
        change_summary="test policy",
    )
    await db_session.commit()
    return {
        "entity_id": entity_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
    }


async def _guard(ids, arguments):
    return await guard_workspace_tool_action(
        name="create_scheduled_job",
        arguments=arguments,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
    )


@pytest.mark.asyncio
async def test_workspace_hitl_without_grant_still_prompts(db_session, monkeypatch):
    """Loop precondition: a workspace that HITL-gates automation returns a card."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(hitl_required_capabilities=["automation.manage"]),
    )

    blocked = await _guard(ids, dict(_CREATE_ARGS))
    payload = json.loads(blocked or "{}")
    assert payload["__hitl__"] is True
    assert payload["hitl"]["action"] == "workspace.automation.create"
    assert payload["hitl"]["capability_id"] == "automation.manage"


@pytest.mark.asyncio
async def test_workspace_always_on_card_breaks_hitl_loop(db_session, monkeypatch):
    """THE loop fix, end to end: Always approve on the card writes the
    workspace policy standing grant — the next call sails through with no
    token and no new card."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(hitl_required_capabilities=["automation.manage"]),
    )

    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    message = await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=blocked["approval_token"],
        action="always_approve",
    )
    assert message and "Retry the exact same tool call" in message

    assert await _guard(ids, dict(_CREATE_ARGS)) is None
    # ... and a DIFFERENT payload for the same action is standing-approved too.
    assert await _guard(ids, {**_CREATE_ARGS, "name": "daily digest"}) is None


@pytest.mark.asyncio
async def test_workspace_approve_once_covers_one_retry_only(db_session, monkeypatch):
    """Plain approve is a one-time grant: token retry passes, third ask re-prompts."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(hitl_required_capabilities=["automation.manage"]),
    )

    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    token = blocked["approval_token"]
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=token,
        action="approve",
    )

    assert await _guard(ids, {**_CREATE_ARGS, "approval_token": token}) is None
    reask = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    assert reask["__hitl__"] is True
    assert reask["approval_token"] != token


@pytest.mark.asyncio
async def test_workspace_reject_is_per_request_not_standing(db_session, monkeypatch):
    """Rejecting a card denies THAT request; the next attempt asks again with
    a fresh card (rejection is not a standing deny)."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(hitl_required_capabilities=["automation.manage"]),
    )

    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    message = await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=blocked["approval_token"],
        action="reject",
    )
    assert message and "rejected" in message

    reask = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    assert reask["__hitl__"] is True
    assert reask["approval_token"] != blocked["approval_token"]


@pytest.mark.asyncio
async def test_workspace_standing_grant_does_not_override_hard_block(db_session, monkeypatch):
    """never_allow beats auto_approve — a standing grant can never override an
    admin's hard governance block (decide()'s deny precedence)."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(
            never_allow_capabilities=["automation.manage"],
            auto_approve_capabilities=["automation.manage"],
        ),
    )

    blocked = await _guard(ids, dict(_CREATE_ARGS))
    payload = json.loads(blocked or "{}")
    assert payload["error"] == "blocked_by_governance"
    assert payload["capability_id"] == "automation.manage"
