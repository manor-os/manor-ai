"""Regression: agent file tools must honor Document.visibility.

Knowledge documents are real files under the entity FS root. The agent
``read_file`` / ``list_files`` / ``glob`` / ``grep`` tools read that root with
only path-traversal + hidden-path filtering, so an agent acting for a member
could read another member's PRIVATE document by path. These tools now gate
Knowledge-document paths through ``user_can_read_document``.
"""

from __future__ import annotations

import json
import os

import pytest

import packages.core.database as db_module
from packages.core.ai.tools.file_tools import (
    _grep_files,
    _list_files,
    _read_file,
)
from packages.core.config import get_settings
from packages.core.models.user import User
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document

SECRET = "机密:董事会薪酬明细 BOARD-COMP-SECRET"


@pytest.fixture
def fs_enabled(tmp_path):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
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
async def test_file_tools_hide_private_doc_from_member(fs_enabled, tmp_path):
    entity_id = "ent_ftools_vis"
    owner_id = await _make_user(entity_id, "ft_owner", "owner")
    member_id = await _make_user(entity_id, "ft_member", "member")

    # Write the two files to the entity FS root.
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

    # list_files as MEMBER: private doc absent, shared present.
    listed = json.loads(await _list_files(entity_id, path="", user_id=member_id))
    paths = {e["path"] for e in listed["entries"]}
    assert "board-comp.md" not in paths, f"private doc leaked to member: {paths}"
    assert "handbook.md" in paths

    # list_files as OWNER: sees both.
    listed_owner = json.loads(await _list_files(entity_id, path="", user_id=owner_id))
    owner_paths = {e["path"] for e in listed_owner["entries"]}
    assert {"board-comp.md", "handbook.md"} <= owner_paths

    # read_file the private doc as MEMBER: denied (reported as not found).
    r = json.loads(await _read_file(entity_id, path="board-comp.md", user_id=member_id))
    assert r.get("error"), f"read_file leaked private doc: {r}"
    assert "content" not in r

    # read_file as OWNER: returns content.
    r_owner = json.loads(await _read_file(entity_id, path="board-comp.md", user_id=owner_id))
    assert SECRET.split()[0] in r_owner.get("content", ""), r_owner

    # read_file the entity-visible doc as MEMBER: allowed (no over-block).
    r_shared = json.loads(await _read_file(entity_id, path="handbook.md", user_id=member_id))
    assert "team handbook" in r_shared.get("content", ""), r_shared

    # grep as MEMBER must not surface private-doc content.
    g = json.loads(await _grep_files(entity_id, pattern="董事会薪酬", user_id=member_id))
    hit_files = {m["file"] for m in g.get("matches", [])}
    assert "board-comp.md" not in hit_files, f"grep leaked private content: {g}"
