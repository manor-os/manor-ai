"""Transaction-boundary regressions for nightly hard-deletion sweeps."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid
from packages.core.tasks.deletion_tasks import (
    _async_purge_users,
    _async_purge_workspaces,
)


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_workspace_purge_batch_commits_each_success(monkeypatch):
    from packages.core import database
    from packages.core.services import entity_service
    from packages.core.services import workspace_artifact_purge

    fake_db = _FakeSession()
    deleted_at = datetime.now(timezone.utc)
    candidates = [
        SimpleNamespace(
            id=generate_ulid(),
            entity_id=generate_ulid(),
            deleted_at=deleted_at,
        )
        for _ in range(3)
    ]
    purge_calls: list[tuple[str, datetime]] = []
    drain_calls = 0

    async def fake_list(_db):
        return candidates

    async def fake_purge(_db, workspace_id, *, expected_deleted_at):
        purge_calls.append((workspace_id, expected_deleted_at))
        if workspace_id == candidates[1].id:
            raise RuntimeError("isolated candidate failure")
        return True

    async def fake_drain(_db):
        nonlocal drain_calls
        drain_calls += 1
        return 2, 1

    monkeypatch.setattr(database, "create_worker_session", lambda: lambda: fake_db)
    monkeypatch.setattr(entity_service, "list_workspaces_due_for_purge", fake_list)
    monkeypatch.setattr(entity_service, "purge_workspace", fake_purge)
    monkeypatch.setattr(
        workspace_artifact_purge,
        "drain_workspace_artifact_purge_jobs",
        fake_drain,
    )

    await _async_purge_workspaces()

    assert purge_calls == [
        (candidate.id, deleted_at) for candidate in candidates
    ]
    assert fake_db.commits == 2
    assert fake_db.rollbacks == 1
    assert drain_calls == 1


@pytest.mark.asyncio
async def test_user_purge_batch_commits_each_success(monkeypatch):
    from packages.core import database
    from packages.core.services import user_lifecycle

    fake_db = _FakeSession()
    deleted_at = datetime.now(timezone.utc)
    candidates = [
        SimpleNamespace(
            id=generate_ulid(),
            entity_id=generate_ulid(),
            deleted_at=deleted_at,
        )
        for _ in range(3)
    ]
    purge_calls: list[str] = []

    async def fake_list(_db):
        return candidates

    async def fake_purge(_db, user_id):
        purge_calls.append(user_id)
        if user_id == candidates[1].id:
            raise RuntimeError("isolated candidate failure")
        return True

    monkeypatch.setattr(database, "create_worker_session", lambda: lambda: fake_db)
    monkeypatch.setattr(user_lifecycle, "list_users_due_for_purge", fake_list)
    monkeypatch.setattr(user_lifecycle, "purge_user", fake_purge)

    await _async_purge_users()

    assert purge_calls == [candidate.id for candidate in candidates]
    assert fake_db.commits == 2
    assert fake_db.rollbacks == 1
