"""Approval settings concurrency runs only against the isolated pytest DB."""
import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.governance.approval_policy import ApprovalScope, PolicyDraft
from packages.core.governance.approval_policy_service import (
    ApprovalPolicyConflict, account_defaults, read_approval_settings, update_approval_policy,
)
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.proposals.constants import TASK_ACTION_KEY, EXTERNAL_TASK_ACTION_KEY


@pytest.mark.asyncio
async def test_concurrent_proposal_rule_writes_conflict_then_preserve_both_on_explicit_retry(
    db_session, isolated_test_redis,
):
    entity_id, workspace_id, actor_id = generate_ulid(), generate_ulid(), generate_ulid()
    actor = User(id=actor_id, entity_id=entity_id, email=f"{actor_id}@example.test", password_hash="x", role="owner")
    db_session.add_all([
        Entity(id=entity_id, name="CAS test", settings={"approval_defaults": account_defaults()}), actor,
        UserMembership(user_id=actor_id, entity_id=entity_id, role="owner", status="active"),
        Workspace(id=workspace_id, entity_id=entity_id, name="Concurrent approvals", status="active"),
    ])
    await db_session.flush()
    await update_policy(db_session, entity_id=entity_id, workspace_id=workspace_id, policy=WorkspacePolicy())
    scope = ApprovalScope(kind="workspace", id=workspace_id, entity_id=entity_id)
    before = await read_approval_settings(db_session, scope, actor)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gate = asyncio.Event()

    async def write(action_key):
        async with sessions() as db:
            await db.execute(text("SET LOCAL lock_timeout = '5s'"))
            user = await db.get(User, actor_id)
            await gate.wait()
            try:
                await update_approval_policy(db, scope, PolicyDraft(mode=None, user_rules=[{
                    "effect": "allow", "selector": {"kind": "action", "action_key": action_key},
                }]), before["version"], user)
                await db.commit()
                return "saved", action_key
            except ApprovalPolicyConflict:
                await db.rollback()
                return "conflict", action_key

    tasks = [asyncio.create_task(write(key)) for key in (TASK_ACTION_KEY, EXTERNAL_TASK_ACTION_KEY)]
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    assert sorted(status for status, _ in outcomes) == ["conflict", "saved"]
    missed = next(key for status, key in outcomes if status == "conflict")
    async with sessions() as db:
        user = await db.get(User, actor_id)
        latest = await read_approval_settings(db, scope, user)
        user_rules = [{key: rule[key] for key in ("id", "effect", "selector")}
                      for rule in latest["policy"]["rules"] if rule["source_kind"] == "user"]
        await update_approval_policy(db, scope, PolicyDraft(mode=None, user_rules=[*user_rules, {
            "effect": "allow", "selector": {"kind": "action", "action_key": missed},
        }]), latest["version"], user)
        await db.commit()
        policy = await db.get(GovernancePolicy, workspace_id)
        assert {rule["selector"]["action_key"] for rule in policy.policy["approval"]["policy"]["rules"]} == {
            TASK_ACTION_KEY, EXTERNAL_TASK_ACTION_KEY,
        }
        revisions = list((await db.execute(select(GovernanceRevision.revision)
            .where(GovernanceRevision.workspace_id == workspace_id).order_by(GovernanceRevision.revision))).scalars())
        assert revisions == [1, 2, 3]
        assert policy.revision == 3
