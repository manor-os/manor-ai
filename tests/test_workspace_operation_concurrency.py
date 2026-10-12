"""Real PostgreSQL interleavings for Workspace operation edits and decisions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace, WorkspaceActivity, WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations


def _remove(provider):
    return {"op": "integration.remove", "payload": {"provider": provider}}


async def _seed(db, patches=None, *, entity_id=None):
    workspace = Workspace(
        entity_id=entity_id or generate_ulid(), name="Concurrent configuration",
        status="paused", settings={}, operating_model={}, heartbeat_enabled=False,
    )
    db.add(workspace)
    await db.flush()
    draft = await operations.create_operation_draft(
        db, workspace.id, workspace.entity_id, initial_patches=patches or [],
    )
    await db.commit()
    return workspace.entity_id, workspace.id, draft.id


async def _preload(db, entity_id, workspace_id, draft_id):
    # Retain stale ORM instances, as request authorization and card resolution do.
    return (
        await db.get(Workspace, workspace_id),
        await operations.get_operation_draft(db, draft_id, entity_id, workspace_id),
    )


async def _finish_waiting_write(first, second, observer, write):
    pid = await second.scalar(text("SELECT pg_backend_pid()"))
    pending = asyncio.create_task(write())
    try:
        for _ in range(200):
            blocked = await observer.scalar(text(
                "SELECT cardinality(pg_blocking_pids(:pid)) > 0"
            ), {"pid": pid})
            if blocked:
                break
            if pending.done():
                await pending
                pytest.fail("The competing writer did not wait for the first transaction")
            await asyncio.sleep(0.01)
        else:
            pytest.fail("The competing writer never reached its database lock")
        await first.commit()
        return await asyncio.wait_for(pending, 5)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_patch_successes_preserve_both_edits(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session)
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        stale = await _preload(second, entity_id, workspace_id, draft_id)
        await operations.patch_operation_draft(first, draft_id, entity_id, workspace_id, [_remove("slack")])

        async def patch():
            result = await operations.patch_operation_draft(second, draft_id, entity_id, workspace_id, [_remove("github")])
            await second.commit()
            return result

        result = await _finish_waiting_write(first, second, db_session, patch)
        assert result is stale[1]
        assert len(result.patches) == 2
        assert result.current_state["integration_scope"]["disabled_providers"] == ["github", "slack"]


@pytest.mark.asyncio
async def test_late_patch_cannot_rewrite_applied_receipt(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session, [_remove("slack")])
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        stale = await _preload(second, entity_id, workspace_id, draft_id)
        applied = await operations.apply_operation_draft(first, draft_id, entity_id, workspace_id, user_confirmation=True)

        async def patch():
            return await operations.patch_operation_draft(second, draft_id, entity_id, workspace_id, [_remove("github")])

        with pytest.raises(operations.OperationConflictError, match="cannot patch applied"):
            await _finish_waiting_write(first, second, db_session, patch)
        assert stale[1].status == "applied"
        await second.rollback()
    async with sessions() as check:
        draft = await check.get(WorkspaceOperationDraft, draft_id)
        workspace = await check.get(Workspace, workspace_id)
        assert draft.patches == applied["draft"]["patches"]
        assert draft.diff == applied["draft"]["diff"]
        assert workspace.operating_model["integration_scope"]["disabled_providers"] == ["slack"]


@pytest.mark.asyncio
async def test_apply_reloads_a_draft_changed_while_waiting(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session, [_remove("slack")])
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        stale = await _preload(second, entity_id, workspace_id, draft_id)
        await operations.patch_operation_draft(first, draft_id, entity_id, workspace_id, [_remove("github")])

        async def apply():
            result = await operations.apply_operation_draft(second, draft_id, entity_id, workspace_id, user_confirmation=True)
            await second.commit()
            return result

        result = await _finish_waiting_write(first, second, db_session, apply)
        assert len(stale[1].patches) == 2
        assert result["draft"]["current_state"]["integration_scope"]["disabled_providers"] == ["github", "slack"]
        assert stale[0].operating_model["integration_scope"]["disabled_providers"] == ["github", "slack"]


@pytest.mark.asyncio
async def test_apply_checks_review_fingerprint_after_waiting(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session, [_remove("slack")])
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        stale = await _preload(second, entity_id, workspace_id, draft_id)
        reviewed = operations.operation_draft_review_fingerprint(stale[1])
        await operations.patch_operation_draft(first, draft_id, entity_id, workspace_id, [_remove("github")])

        async def apply():
            return await operations.apply_operation_draft(
                second, draft_id, entity_id, workspace_id,
                user_confirmation=True, expected_review_fingerprint=reviewed,
            )

        with pytest.raises(operations.OperationConflictError, match="changed; review"):
            await _finish_waiting_write(first, second, db_session, apply)
        assert stale[1].status == "open"
        assert stale[0].operation_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("first_action", ["apply", "discard"])
async def test_stale_second_apply_cannot_override_a_terminal_decision(db_session, first_action):
    entity_id, workspace_id, draft_id = await _seed(db_session, [_remove("slack")])
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        stale = await _preload(second, entity_id, workspace_id, draft_id)
        if first_action == "apply":
            await operations.apply_operation_draft(first, draft_id, entity_id, workspace_id, user_confirmation=True)
        else:
            await operations.discard_operation_draft(first, draft_id, entity_id, workspace_id)

        async def apply():
            return await operations.apply_operation_draft(second, draft_id, entity_id, workspace_id, user_confirmation=True)

        terminal = "applied" if first_action == "apply" else "discarded"
        with pytest.raises(operations.OperationConflictError, match=f"cannot apply {terminal}"):
            await _finish_waiting_write(first, second, db_session, apply)
        assert stale[1].status == terminal
        await second.rollback()
    async with sessions() as check:
        workspace = await check.get(Workspace, workspace_id)
        assert workspace.operation_revision == (1 if first_action == "apply" else 0)
        count = await check.scalar(select(func.count()).select_from(WorkspaceActivity).where(
            WorkspaceActivity.workspace_id == workspace_id,
            WorkspaceActivity.event_type == "workspace_operation.applied",
        ))
        assert count == (1 if first_action == "apply" else 0)


@pytest.mark.asyncio
async def test_patch_rechecks_workspace_deletion_after_waiting(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session)
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        stale = await _preload(second, entity_id, workspace_id, draft_id)
        workspace = await first.get(Workspace, workspace_id, with_for_update=True)
        workspace.deleted_at = datetime.now(timezone.utc)
        await first.flush()

        async def patch():
            return await operations.patch_operation_draft(second, draft_id, entity_id, workspace_id, [_remove("slack")])

        assert await _finish_waiting_write(first, second, db_session, patch) is None
        assert stale[1].patches == []


@pytest.mark.asyncio
async def test_refresh_preserves_the_applied_review_receipt(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session, [_remove("slack")])
    applied = await operations.apply_operation_draft(db_session, draft_id, entity_id, workspace_id, user_confirmation=True)
    receipt = deepcopy(applied["draft"]["diff"])
    await db_session.commit()
    draft = await operations.refresh_operation_draft_review(db_session, draft_id, entity_id, workspace_id)
    assert draft.diff == receipt
    assert "integration_scope" in draft.diff["changed_keys"]


@pytest.mark.asyncio
async def test_integration_draft_edits_in_other_workspaces_do_not_wait(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session)
    _, other_workspace_id, other_draft_id = await _seed(db_session, entity_id=entity_id)
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        await operations.patch_operation_draft(first, draft_id, entity_id, workspace_id, [_remove("slack")])
        result = await asyncio.wait_for(operations.patch_operation_draft(
            second, other_draft_id, entity_id, other_workspace_id, [_remove("github")],
        ), 2)
        assert result.current_state["integration_scope"]["disabled_providers"] == ["github"]


@pytest.mark.asyncio
async def test_concurrent_edit_burst_preserves_every_success(db_session):
    entity_id, workspace_id, draft_id = await _seed(db_session)
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    providers = [f"concurrent_{index:02}" for index in range(24)]

    async def edit(provider):
        async with sessions() as db:
            result = await operations.patch_operation_draft(
                db, draft_id, entity_id, workspace_id, [_remove(provider)],
            )
            await db.commit()
            return result.id

    results = await asyncio.wait_for(asyncio.gather(*(edit(provider) for provider in providers)), 20)
    assert results == [draft_id] * len(providers)
    async with sessions() as db:
        draft = await db.get(WorkspaceOperationDraft, draft_id)
        assert len(draft.patches) == len(providers)
        assert draft.current_state["integration_scope"]["disabled_providers"] == providers
