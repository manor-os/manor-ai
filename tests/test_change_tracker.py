"""Tests for field-level change tracking (audit trail diffs)."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "audituser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Audit Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_track_changes(client: AsyncClient):
    """Unit-style: track_changes detects field diffs correctly."""
    from packages.core.services.change_tracker import track_changes

    class FakeTask:
        status = "pending"
        title = "Old Title"
        priority = 3

    changes = track_changes(FakeTask(), {"status": "completed", "title": "New Title", "priority": 3})
    assert len(changes) == 2
    fields = {c["field"] for c in changes}
    assert fields == {"status", "title"}
    status_change = next(c for c in changes if c["field"] == "status")
    assert status_change["old"] == "pending"
    assert status_change["new"] == "completed"
    title_change = next(c for c in changes if c["field"] == "title")
    assert title_change["old"] == "Old Title"
    assert title_change["new"] == "New Title"


@pytest.mark.asyncio
async def test_record_and_get_history(client: AsyncClient):
    """Create a task, update it, then retrieve field-level change history."""
    headers = await _auth(client)

    # Create task
    resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Audit Test Task",
            "priority": 3,
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Update title and priority
    resp2 = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={
            "title": "Updated Audit Task",
            "priority": 1,
        },
    )
    assert resp2.status_code == 200

    # Get change history
    resp3 = await client.get(f"/api/v1/tasks/{task_id}/history", headers=headers)
    assert resp3.status_code == 200
    history = resp3.json()
    assert len(history) >= 1

    # The most recent entry should contain our field changes
    latest = history[0]
    assert latest["action"] == "task.update"
    changed_fields = {c["field"] for c in latest["changes"]}
    assert "title" in changed_fields
    assert "priority" in changed_fields

    title_change = next(c for c in latest["changes"] if c["field"] == "title")
    assert title_change["old"] == "Audit Test Task"
    assert title_change["new"] == "Updated Audit Task"


@pytest.mark.asyncio
async def test_skips_sensitive_fields(client: AsyncClient):
    """Sensitive fields like password_hash should not appear in diffs."""
    from packages.core.services.change_tracker import track_changes

    class FakeUser:
        username = "alice"
        password_hash = "old_hash"
        totp_secret = "old_secret"
        key_hash = "old_key"

    changes = track_changes(
        FakeUser(),
        {
            "username": "bob",
            "password_hash": "new_hash",
            "totp_secret": "new_secret",
            "key_hash": "new_key",
        },
    )
    fields = {c["field"] for c in changes}
    assert "username" in fields
    assert "password_hash" not in fields
    assert "totp_secret" not in fields
    assert "key_hash" not in fields


@pytest.mark.asyncio
async def test_no_changes_when_same(client: AsyncClient):
    """Updating with the same values should produce no change records."""
    from packages.core.services.change_tracker import track_changes

    class FakeTask:
        status = "pending"
        title = "Same Title"
        priority = 3

    changes = track_changes(FakeTask(), {"status": "pending", "title": "Same Title", "priority": 3})
    assert changes == []
