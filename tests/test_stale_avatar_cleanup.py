"""Self-healing test: stale avatar URLs get cleared after the first 404.

Threat model — UX, not security
───────────────────────────────
When a user's avatar file disappears from disk (dev FS resets, manual
cleanup, etc.) the DB still hands out the broken URL. The frontend
already degrades to an initials circle (#121 added <img onError>
fallback), but the broken URL stays in every staff/user payload,
firing a fresh 404 on every render and polluting devtools.

This test locks in the self-healing behavior added in this PR:
  1. User / Staff has avatar_url pointing at a non-existent file
  2. Request to that URL returns 404
  3. After the response, a background task nulls out avatar_url on
     every matching User / Staff row
  4. Future fetches of that user see avatar_url=None and don't trigger
     another 404

We use ``packages.core.database.async_session`` (which the ``client``
fixture monkey-patches to point at the test database) for both the
seed and the verification, so we share the same engine the FastAPI
app + the cleanup background task are using. The standalone
``db_session`` fixture uses a different engine and would race here.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from packages.core.models.staff import Staff
from packages.core.models.user import Entity, User


async def _drain_avatar_cleanup_tasks() -> None:
    """Wait for any fire-and-forget cleanup tasks scheduled by the
    serve_entity_file handler to actually run. Scheduled via
    ``asyncio.create_task``, they're tracked in a module-level set;
    we await them all before asserting the side effect."""
    from apps.api.routers.filesystem import _PENDING_AVATAR_CLEANUP_TASKS

    # Snapshot — tasks add/discard themselves during iteration
    pending = list(_PENDING_AVATAR_CLEANUP_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_fs(monkeypatch, tmp_path):
    """Re-roots get_entity_root under a tmp dir per test, and forces
    MANOR_FS_ENABLED=True so the serve handler doesn't bail with 503."""
    import apps.api.routers.filesystem as fs_router

    root = tmp_path / "manor-fs"
    root.mkdir()

    def _fake_root(entity_id: str) -> str:
        return str(root / entity_id)

    monkeypatch.setattr(fs_router, "get_entity_root", _fake_root)
    monkeypatch.setattr(fs_router, "is_fs_enabled", lambda: True)
    return root


async def _session():
    """Open a session against the engine the ``client`` fixture installed,
    so we share the same DB / connection pool as the FastAPI app and its
    background tasks."""
    import packages.core.database as db_module

    return db_module.async_session()


async def test_stale_avatar_url_cleared_after_first_404(client, fake_fs):
    """First request: 404 + DB row's avatar_url gets nulled out."""
    entity_id = "01TESTAVATAR0000000000000A"
    stale_path = f"avatars/{('a' * 32)}.jpg"
    stale_url = f"/api/v1/fs/{entity_id}/{stale_path}"

    # Seed via the client's engine.
    async with await _session() as seed:
        seed.add(Entity(id=entity_id, name="Test Entity A"))
        seed.add(
            User(
                entity_id=entity_id,
                email="alice@stale-avatar.test",
                display_name="Alice",
                password_hash="x",
                avatar_url=stale_url,
            )
        )
        await seed.commit()

    # Stale URL → 404
    res = await client.get(stale_url)
    assert res.status_code == 404
    await _drain_avatar_cleanup_tasks()

    # Background task should have cleared avatar_url.
    async with await _session() as verify:
        row = (await verify.execute(select(User).where(User.email == "alice@stale-avatar.test"))).scalar_one()
        assert row.avatar_url is None, (
            f"avatar_url should have been cleared by the background task; still set to {row.avatar_url!r}"
        )


async def test_stale_avatar_url_cleared_on_staff_row_too(client, fake_fs):
    """Same cleanup must run for Staff rows, not just Users — both share
    the avatar_url column and the same broken-image UX bug."""
    entity_id = "01TESTAVATAR0000000000000B"
    stale_path = f"avatars/{('b' * 32)}.png"
    stale_url = f"/api/v1/fs/{entity_id}/{stale_path}"

    async with await _session() as seed:
        seed.add(Entity(id=entity_id, name="Test Entity B"))
        seed.add(
            Staff(
                entity_id=entity_id,
                name="Bob",
                email="bob@stale-avatar.test",
                avatar_url=stale_url,
            )
        )
        await seed.commit()

    res = await client.get(stale_url)
    assert res.status_code == 404
    await _drain_avatar_cleanup_tasks()

    async with await _session() as verify:
        row = (await verify.execute(select(Staff).where(Staff.email == "bob@stale-avatar.test"))).scalar_one()
        assert row.avatar_url is None


async def test_404_for_non_avatar_path_does_not_touch_db(client, fake_fs):
    """Cleanup is scoped to ``avatars/`` paths only. A 404 on, say,
    ``Photos/`` shouldn't accidentally null out unrelated columns."""
    entity_id = "01TESTAVATAR0000000000000C"
    # Nonsense scenario — user's avatar_url points at a non-avatar path.
    # The point is to prove the cleanup task is gated on the ``avatars/``
    # prefix, not on the column name. A request to a non-avatar path
    # must leave avatar_url untouched.
    weird_url = f"/api/v1/fs/{entity_id}/Photos/profile.jpg"

    async with await _session() as seed:
        seed.add(Entity(id=entity_id, name="Test Entity C"))
        seed.add(
            User(
                entity_id=entity_id,
                email="carol@stale-avatar.test",
                display_name="Carol",
                password_hash="x",
                avatar_url=weird_url,
            )
        )
        await seed.commit()

    # The request will 401 (Photos/ is non-public and we have no auth),
    # but either way the prefix guard prevents cleanup. We don't care
    # what status comes back — just that the column is preserved.
    await client.get(weird_url)
    await _drain_avatar_cleanup_tasks()

    async with await _session() as verify:
        row = (await verify.execute(select(User).where(User.email == "carol@stale-avatar.test"))).scalar_one()
        assert row.avatar_url == weird_url, "avatar_url for a non-avatar path must not be cleaned up"
