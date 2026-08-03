"""Regression: the bash tool must not read private Knowledge documents.

Local filesystem commands (`cat`, `grep`, `head`, …) run in the entity FS root,
so an agent acting for a member could `cat another-members-private.md`. The bash
tool now resolves the file paths a read command touches and blocks the command
when any maps to a document the caller cannot view.
"""

from __future__ import annotations

import json
import os

import pytest

import packages.core.database as db_module
from packages.core.ai.tools.bash_tool import _bash
from packages.core.config import get_settings
from packages.core.models.user import User
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document

SECRET = "BOARD-COMP-SECRET board comp numbers"


@pytest.fixture
def fs_enabled(tmp_path, monkeypatch):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    # Force local execution (no sandbox) so `cat` runs against the entity FS.
    monkeypatch.delenv("SANDBOX_SERVICE_URL", raising=False)
    try:
        yield settings
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


async def _make_user(entity_id: str, name: str, role: str) -> str:
    async with db_module.async_session() as db:
        u = User(
            entity_id=entity_id, email=f"{name}@test.com", display_name=name,
            password_hash=hash_password("pass123"), role=role, status="active",
        )
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
    return uid


@pytest.mark.asyncio
async def test_bash_cannot_cat_private_doc_of_another_member(fs_enabled, tmp_path):
    entity_id = "ent_bash_vis"
    owner_id = await _make_user(entity_id, "bash_owner", "owner")
    member_id = await _make_user(entity_id, "bash_member", "member")

    root = os.path.join(str(tmp_path), entity_id)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "board-comp.md"), "w", encoding="utf-8") as f:
        f.write(SECRET)
    with open(os.path.join(root, "handbook.md"), "w", encoding="utf-8") as f:
        f.write("team handbook body")

    async with db_module.async_session() as db:
        await create_document(
            db, entity_id, name="board-comp.md", fs_path="board-comp.md",
            file_type="md", source="upload", visibility="private", owner_id=owner_id,
        )
        await create_document(
            db, entity_id, name="handbook.md", fs_path="handbook.md",
            file_type="md", source="upload", visibility="entity", owner_id=owner_id,
        )
        await db.commit()

    # MEMBER: cat of the private doc is blocked and NOT executed.
    r = json.loads(await _bash(entity_id, command="cat board-comp.md", user_id=member_id))
    assert r.get("error"), f"bash leaked private doc: {r}"
    assert "board-comp.md" not in (r.get("stdout") or "")
    assert SECRET not in json.dumps(r)

    # MEMBER: grep + pipe over the private doc is also blocked.
    r = json.loads(await _bash(
        entity_id, command="cat board-comp.md | grep SECRET", user_id=member_id,
    ))
    assert r.get("error"), f"bash pipe leaked private doc: {r}"
    assert SECRET not in json.dumps(r)

    # MEMBER: the entity-visible doc is readable (no over-block).
    r = json.loads(await _bash(entity_id, command="cat handbook.md", user_id=member_id))
    assert "team handbook" in (r.get("stdout") or ""), r

    # OWNER: can read their own private doc.
    r = json.loads(await _bash(entity_id, command="cat board-comp.md", user_id=owner_id))
    assert "BOARD-COMP-SECRET" in (r.get("stdout") or ""), r
