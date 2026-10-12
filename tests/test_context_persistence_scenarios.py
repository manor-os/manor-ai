"""Database-backed scope and lease regressions for context persistence."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX
from packages.core.models.agent_delegation_session import AgentContextArchive
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeRun
from packages.core.services.runtime_context_archive import runtime_context_archive_callbacks


def scope():
    return {name: generate_ulid() for name in (
        "entity_id", "user_id", "conversation_id", "workspace_id", "agent_id",
    )}


def checkpoint(label):
    return {"role": "user", "content": CHECKPOINT_PREFIX + "\n" + label}


async def run_row(db, owner_scope, **values):
    rid = generate_ulid()
    row = RuntimeRun(**{
        "id": rid, "root_run_id": rid, **owner_scope, "status": "running",
        "lease_owner": "test-context-worker", "version": 1,
        "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        **values,
    })
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["entity_id", "user_id", "conversation_id", "workspace_id", "agent_id"])
async def test_each_scope_dimension_blocks_archive_and_checkpoint_disclosure(db_session, field):
    owner_scope = scope()
    first = await run_row(db_session, owner_scope)
    archive, _, save, _ = await runtime_context_archive_callbacks(run_id=first.id, **owner_scope)
    archive_id = await archive([{"role": "user", "content": "private original"}])
    await save([checkpoint("private summary")])
    await db_session.execute(update(RuntimeRun).where(RuntimeRun.id == first.id).values(status="completed"))
    await db_session.commit()
    foreign_scope = {**owner_scope, field: generate_ulid()}
    second = await run_row(db_session, foreign_scope)
    _, read, _, previous = await runtime_context_archive_callbacks(run_id=second.id, **foreign_scope)
    assert previous is None
    assert "error" in json.loads(await read({"archive_id": archive_id}))
    with pytest.raises(ValueError, match="scope"):
        await runtime_context_archive_callbacks(run_id=first.id, **foreign_scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_lease", ["version", "owner", "expired", "missing_expiry", "cancelled"])
async def test_stale_main_writer_cannot_append_archive_or_replace_checkpoint(db_session, lost_lease):
    owner_scope = scope()
    pending_tool = {"pending_tool": {"name": "publish", "requires_approval": True}, "rounds": 17}
    row = await run_row(db_session, owner_scope, checkpoint=pending_tool)
    archive, _, save, _ = await runtime_context_archive_callbacks(run_id=row.id, **owner_scope)
    await archive([{"role": "user", "content": "verified original"}])
    saved = checkpoint("verified checkpoint")
    await save([saved])
    changes = {
        "version": {"version": 2}, "owner": {"lease_owner": "replacement-worker"},
        "expired": {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        "missing_expiry": {"lease_expires_at": None}, "cancelled": {"status": "cancelled"},
    }
    await db_session.execute(update(RuntimeRun).where(RuntimeRun.id == row.id).values(**changes[lost_lease]))
    await db_session.commit()
    with pytest.raises(RuntimeError, match="no longer owns"):
        await archive([{"role": "user", "content": "stale extra record"}])
    with pytest.raises(RuntimeError, match="no longer owns"):
        await save([checkpoint("stale summary")])
    await db_session.refresh(row)
    assert row.context_state["checkpoint"] == saved
    assert row.checkpoint == pending_tool
    archives = (await db_session.execute(select(AgentContextArchive).where(AgentContextArchive.runtime_run_id == row.id))).scalars().all()
    assert len(archives) == 1


@pytest.mark.asyncio
async def test_active_sandbox_execution_does_not_revoke_context_writer(db_session):
    from packages.core.services.runtime_run_service import (
        clear_runtime_run_active_execution,
        set_runtime_run_active_execution,
    )

    owner_scope = scope()
    row = await run_row(db_session, owner_scope)
    archive, _, save, _ = await runtime_context_archive_callbacks(run_id=row.id, **owner_scope)

    await set_runtime_run_active_execution(
        db_session, run_id=row.id, sandbox_id="sandbox-1", execution_id="exec-1",
    )
    await db_session.commit()
    await db_session.refresh(row)
    assert (row.active_sandbox_id, row.active_execution_id, row.version) == (
        "sandbox-1", "exec-1", 1,
    )
    await archive([{"role": "user", "content": "source material"}])

    await clear_runtime_run_active_execution(db_session, run_id=row.id, execution_id="exec-1")
    await db_session.commit()
    await db_session.refresh(row)
    assert (row.active_sandbox_id, row.active_execution_id, row.version) == (None, None, 1)
    saved = checkpoint("sandbox work may continue")
    await save([saved])
    await db_session.refresh(row)
    assert row.context_state["checkpoint"] == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("optional_scope", [False, True])
async def test_restore_selects_latest_completed_checkpoint_before_current_run(db_session, optional_scope):
    owner_scope = scope()
    if optional_scope:
        owner_scope.update(workspace_id=None, agent_id=None)
    now = datetime.now(timezone.utc)
    expected = checkpoint("newest verified summary")
    for minutes, status, value in [
        (-6, "completed", checkpoint("older summary")),
        (-5, "completed", expected),
        (-4, "failed", checkpoint("failed summary")),
        (-3, "cancelled", checkpoint("cancelled summary")),
        (-2, "completed", None),
        (10, "completed", checkpoint("future summary")),
    ]:
        await run_row(db_session, owner_scope, status=status, created_at=now + timedelta(minutes=minutes),
                      context_state={"version": 1, "checkpoint": value} if value else None)
    for seconds, state in [(-60, {}), (-30, {"version": 1, "checkpoint": None})]:
        await run_row(db_session, owner_scope, status="completed", created_at=now + timedelta(seconds=seconds),
                      context_state=state)
    current = await run_row(db_session, owner_scope, created_at=now)
    _, _, _, restored = await runtime_context_archive_callbacks(run_id=current.id, **owner_scope)
    assert restored == expected


@pytest.mark.asyncio
async def test_child_checkpoint_survives_interruption_without_counting_completed_turn(db_session):
    from packages.core.services.agent_delegation_session import (
        DelegationSessionInterrupted, checkpoint_delegation_context, claim_delegation_session,
        finish_delegation_session, interrupt_delegation_session,
    )
    owner_scope = scope()
    agent_id = owner_scope.pop("agent_id")
    owner_scope["subscription"] = SimpleNamespace(id=generate_ulid(), agent_id=agent_id, service_key="research")
    child, rid, _ = await claim_delegation_session(db_session, **owner_scope)
    cid = child.id
    transcript = [
        {"role": "system", "content": "Old system policy must be rebuilt"},
        {"role": "developer", "content": "Old developer policy must be rebuilt"},
        checkpoint("pending effect requires verification"),
        {"role": "user", "content": "Continue investigation"},
    ]
    original = deepcopy(transcript)
    await checkpoint_delegation_context(db_session, session_id=cid, run_id=rid, messages=transcript)
    assert transcript == original
    await interrupt_delegation_session(db_session, session_id=cid, run_id=rid)
    with pytest.raises(DelegationSessionInterrupted):
        await claim_delegation_session(db_session, **owner_scope)
    await db_session.rollback()
    resumed, new_rid, restored = await claim_delegation_session(db_session, **owner_scope, continue_interrupted=True)
    assert restored == transcript[2:]
    assert resumed.id == cid and new_rid != rid and resumed.turn_count == 0
    await interrupt_delegation_session(db_session, session_id=cid, run_id=rid)
    await db_session.refresh(resumed)
    assert resumed.status == "running" and resumed.active_run_id == new_rid
    await finish_delegation_session(db_session, session_id=cid, run_id=new_rid,
                                    messages=[*restored, {"role": "assistant", "content": "Verified"}], context_usage={})
    await db_session.refresh(resumed)
    assert resumed.turn_count == 1 and resumed.status == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["archive", "checkpoint", "renew", "finish"])
@pytest.mark.parametrize("stale_reason", ["expired", "wrong_run"])
async def test_child_context_mutations_require_current_active_lease(db_session, operation, stale_reason):
    from packages.core.services.agent_delegation_session import (
        archive_delegation_context, checkpoint_delegation_context, claim_delegation_session,
        finish_delegation_session, renew_delegation_session,
    )
    owner_scope = scope()
    agent_id = owner_scope.pop("agent_id")
    owner_scope["subscription"] = SimpleNamespace(id=generate_ulid(), agent_id=agent_id, service_key="research")
    child, rid, _ = await claim_delegation_session(db_session, **owner_scope)
    if stale_reason == "expired":
        child.active_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
    else:
        rid = generate_ulid()
    args = {"session_id": child.id, "run_id": rid}
    action = {
        "archive": archive_delegation_context, "checkpoint": checkpoint_delegation_context,
        "renew": renew_delegation_session, "finish": finish_delegation_session,
    }[operation]
    if operation != "renew":
        args["messages"] = [checkpoint("unverified replacement")]
    if operation == "finish":
        args["context_usage"] = {}
    with pytest.raises(RuntimeError, match="no longer active"):
        await action(db_session, **args)
    await db_session.refresh(child)
    assert child.messages == [] and child.turn_count == 0
    assert not (await db_session.execute(select(AgentContextArchive).where(AgentContextArchive.session_id == child.id))).scalars().all()


@pytest.mark.asyncio
async def test_sandbox_execution_pointers_preserve_context_writer_claim(db_session):
    from packages.core.services.runtime_run_service import (
        clear_runtime_run_active_execution, set_runtime_run_active_execution,
    )

    owner_scope = scope()
    pending_tool = {"pending_tool": {"name": "sandbox"}, "rounds": 8}
    row = await run_row(db_session, owner_scope, checkpoint=pending_tool)
    archive, _, save, _ = await runtime_context_archive_callbacks(run_id=row.id, **owner_scope)
    version = row.version
    for execution_id in ("inspect-pdf", "edit-pdf"):
        await set_runtime_run_active_execution(
            db_session, run_id=row.id, sandbox_id="pdf-sandbox", execution_id=execution_id,
        )
        await db_session.commit()
        await db_session.refresh(row)
        assert row.active_execution_id == execution_id
        assert row.version == version
        await archive([{"role": "user", "content": execution_id}])
        await clear_runtime_run_active_execution(db_session, run_id=row.id, execution_id="stale-execution")
        await db_session.commit()
        await db_session.refresh(row)
        assert row.active_execution_id == execution_id
        await clear_runtime_run_active_execution(db_session, run_id=row.id, execution_id=execution_id)
        await db_session.commit()
        await save([checkpoint(execution_id)])
        await db_session.refresh(row)
        assert row.active_execution_id is None
        assert row.version == version
        assert row.context_state["checkpoint"] == checkpoint(execution_id)
        assert row.checkpoint == pending_tool
