"""Chat turns list Skills on every turn; built-in seeding must not run each time."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.services import builtin_skill_loader, skill_service


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _FakeDb:
    def __init__(self, engine):
        self._engine = engine
        self.info: dict = {}

    def get_bind(self):
        return SimpleNamespace(engine=self._engine)

    def begin_nested(self):
        return _Nested()


@pytest.mark.asyncio
async def test_built_ins_are_verified_once_per_process_and_database(monkeypatch):
    calls: list[object] = []

    async def seed(db):
        calls.append(db)
        return []

    monkeypatch.setattr(builtin_skill_loader, "seed_builtin_skills", seed)
    engine_a, engine_b = object(), object()

    await skill_service._seed_builtin_skills(_FakeDb(engine_a))
    await skill_service._seed_builtin_skills(_FakeDb(engine_a))
    await skill_service._seed_builtin_skills(_FakeDb(engine_b))

    assert len(calls) == 2  # once per database, never again for engine_a


@pytest.mark.asyncio
async def test_a_seed_that_wrote_rows_is_verified_again(monkeypatch):
    """Rows written inside the caller's transaction are not durable yet."""
    calls: list[object] = []

    async def seed(db):
        calls.append(db)
        if len(calls) == 1:
            db.info[builtin_skill_loader.BUILTIN_SKILL_SEED_WROTE_KEY] = True
        return []

    monkeypatch.setattr(builtin_skill_loader, "seed_builtin_skills", seed)
    engine = object()

    await skill_service._seed_builtin_skills(_FakeDb(engine))
    await skill_service._seed_builtin_skills(_FakeDb(engine))
    await skill_service._seed_builtin_skills(_FakeDb(engine))

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_failed_seed_is_retried(monkeypatch):
    calls: list[object] = []

    async def seed(db):
        calls.append(db)
        if len(calls) == 1:
            raise RuntimeError("database busy")
        return []

    monkeypatch.setattr(builtin_skill_loader, "seed_builtin_skills", seed)
    engine = object()

    await skill_service._seed_builtin_skills(_FakeDb(engine))
    await skill_service._seed_builtin_skills(_FakeDb(engine))
    await skill_service._seed_builtin_skills(_FakeDb(engine))

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_seed_stores_portable_paths_and_converges(db_session):
    """A second seed after the first finds nothing to change, regardless of
    the absolute path the first one ran from."""
    from sqlalchemy import select

    from packages.core.models.skill import Skill

    db = db_session
    db.info.pop(builtin_skill_loader.BUILTIN_SKILL_SEED_WROTE_KEY, None)
    await builtin_skill_loader.seed_builtin_skills(db)
    rows = (await db.execute(
        select(Skill).where(Skill.entity_id.is_(None), Skill.slug == "docx")
    )).scalars().all()
    assert rows and rows[0].config["skill_dir"] == "packages/core/ai/skills/docx"

    # Simulate a row written by a process in another checkout.
    rows[0].config = {**rows[0].config, "skill_dir": "/tmp/pd/packages/core/ai/skills/docx"}
    await db.flush()
    await builtin_skill_loader.seed_builtin_skills(db)  # migrates it once
    assert db.info.pop(builtin_skill_loader.BUILTIN_SKILL_SEED_WROTE_KEY, False) is True
    await builtin_skill_loader.seed_builtin_skills(db)
    assert db.info.pop(builtin_skill_loader.BUILTIN_SKILL_SEED_WROTE_KEY, False) is False
