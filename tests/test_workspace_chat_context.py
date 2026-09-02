from __future__ import annotations

import pytest
from sqlalchemy import select, text

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.workspace_chat.context import _build_summary


@pytest.mark.asyncio
async def test_optional_channel_summary_failure_does_not_poison_chat_session(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Transaction isolation test",
        kind="project",
        status="active",
    )
    db_session.add(workspace)
    await db_session.flush()

    async def fail_channel_read(db, _workspace):
        await db.execute(text("SELECT missing_prompt_column FROM channel_configs LIMIT 1"))

    monkeypatch.setattr(
        "packages.core.services.workspace_readiness.list_configured_workspace_channels",
        fail_channel_read,
    )

    summary = await _build_summary(db_session, workspace_id, entity_id)

    assert 'Workspace: "Transaction isolation test"' in summary
    assert (await db_session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id)
    )).scalar_one() == workspace_id
