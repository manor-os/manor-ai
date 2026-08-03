"""Regression: the `manor` composite tool must scope document reads by user_id.

The dispatcher had `list_documents` / `search_documents` / `get_document` /
`list_workspace_artifacts` call the runtime document boundary WITHOUT
forwarding the caller's user_id, even though user_id was in scope. Since
`list_visible_documents` / `get_visible_document` treat user_id=None as
"read everything" (background-caller allowance), an authenticated member could
ask an agent to list/search/open documents and receive another member's
PRIVATE documents.

These tests drive `_dispatch_action` with a member user_id and assert a private
doc owned by someone else is not returned; with the owner's user_id it is.
"""

from __future__ import annotations

import json

import pytest

import packages.core.database as db_module
from packages.core.ai.tools.manor_tool import _dispatch_action
from packages.core.models.user import User
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document


async def _make_user(entity_id: str, name: str, role: str = "member") -> str:
    async with db_module.async_session() as db:
        user = User(
            entity_id=entity_id,
            email=f"{name}@test.com",
            display_name=name,
            password_hash=hash_password("pass123"),
            role=role,
            status="active",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
    return uid


@pytest.mark.asyncio
async def test_manor_list_search_get_scope_private_docs_by_user(client):
    entity_id = "ent_manor_vis"
    owner_id = await _make_user(entity_id, "manorvis_owner", role="owner")
    member_id = await _make_user(entity_id, "manorvis_member", role="member")

    async with db_module.async_session() as db:
        priv = await create_document(
            db, entity_id,
            name="board-comp.md", file_type="md", source="upload",
            visibility="private", owner_id=owner_id,
        )
        await create_document(
            db, entity_id,
            name="handbook.md", file_type="md", source="upload",
            visibility="entity", owner_id=owner_id,
        )
        await db.commit()
        priv_id = priv.id

    # list_documents as the MEMBER: private doc must be absent, shared present.
    listed = json.loads(await _dispatch_action(
        "list_documents", {"limit": 50}, entity_id, user_id=member_id
    ))
    names = {d.get("name") for d in listed.get("documents", [])}
    assert "board-comp.md" not in names, f"private doc leaked to member: {names}"
    assert "handbook.md" in names, f"entity doc wrongly hidden: {names}"

    # list_documents as the OWNER: sees both (control).
    listed_owner = json.loads(await _dispatch_action(
        "list_documents", {"limit": 50}, entity_id, user_id=owner_id
    ))
    owner_names = {d.get("name") for d in listed_owner.get("documents", [])}
    assert {"board-comp.md", "handbook.md"} <= owner_names

    # search_documents as the MEMBER must not surface the private doc.
    searched = json.loads(await _dispatch_action(
        "search_documents", {"query": "board-comp", "limit": 50}, entity_id,
        user_id=member_id,
    ))
    s_names = {d.get("name") for d in searched.get("documents", [])}
    assert "board-comp.md" not in s_names, f"search leaked private doc: {searched}"

    # get_document by id as the MEMBER must be denied.
    got = json.loads(await _dispatch_action(
        "get_document", {"document_id": priv_id}, entity_id, user_id=member_id
    ))
    assert got.get("document") is None, f"get_document leaked private doc: {got}"

    # get_document by id as the OWNER succeeds (control).
    got_owner = json.loads(await _dispatch_action(
        "get_document", {"document_id": priv_id}, entity_id, user_id=owner_id
    ))
    assert got_owner.get("document"), "owner cannot read own private doc"
