"""DB-backed tests for the ``notify_members`` manor action.

Before this action the agent could only *read* notifications
(``list_notifications``) via the manor tool — there was no way to *send* a
notification to a group. ``notify_members`` fans out through the
multi-channel ``notify()`` primitive (one call per member), resolving
recipients through the same ``User`` / ``UserMembership`` membership source
that ``find_team_members`` uses. ``notify()`` always lands an in-app
``Notification`` row (plus external channels per the recipient's prefs), so
we assert on the in-app rows — mirroring how ``test_notify_multichannel``
asserts notify()'s in-app side effect.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.tools.manor_tool import (
    _IMPLEMENTED_ACTIONS,
    _manor_handler,
)
from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification
from packages.core.models.user import User


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"Org {username}",
        },
    )
    body = resp.json()
    return {"user_id": body["user_id"], "entity_id": body["entity_id"]}


def _make_member(entity_id: str, email: str) -> User:
    return User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=email,
        password_hash="x",
        role="member",
        status="active",
        display_name=email.split("@")[0],
    )


@pytest.mark.asyncio
async def test_notify_members_is_implemented_and_advertised():
    """Platform 'no placeholder tools' rule: the action must be dispatchable
    and surfaced by search (both derive from the same dispatcher source)."""
    from packages.core.ai.tools.manor_tool import _ALL_ACTIONS, _search_actions

    assert "notify_members" in _ALL_ACTIONS
    assert "notify_members" in _IMPLEMENTED_ACTIONS
    surfaced = {hit["action"] for hit in _search_actions("notify members", max_results=50)}
    assert "notify_members" in surfaced


@pytest.mark.asyncio
async def test_notify_members_explicit_ids_creates_one_row_each(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _register(client, "notify_owner")
    entity_id = owner["entity_id"]
    alice = _make_member(entity_id, "alice_notify@test.com")
    bob = _make_member(entity_id, "bob_notify@test.com")
    db_session.add_all([alice, bob])
    await db_session.commit()

    raw = await _manor_handler(
        entity_id=entity_id,
        user_id=owner["user_id"],
        action="notify_members",
        params={
            "title": "Standup moved to 10am",
            "body": "New time starts tomorrow.",
            "link": "/calendar",
            "member_ids": [alice.id, bob.id],
        },
    )
    result = json.loads(raw)
    assert result["ok"] is True, result
    assert result["delivered"] == 2
    assert set(result["member_ids"]) == {alice.id, bob.id}
    assert result["notification_type"] == "system"

    for member in (alice, bob):
        rows = (
            (
                await db_session.execute(
                    select(Notification).where(Notification.user_id == member.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, member.email
        row = rows[0]
        assert row.entity_id == entity_id
        assert row.user_id == member.id
        assert row.title == "Standup moved to 10am"
        assert row.content == "New time starts tomorrow."
        assert row.type == "system"


@pytest.mark.asyncio
async def test_notify_members_without_ids_fans_out_to_all_active_members(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _register(client, "notify_all_owner")
    entity_id = owner["entity_id"]
    teammate = _make_member(entity_id, "teammate_all@test.com")
    # A user in a *different* entity must never receive the broadcast.
    outsider = _make_member("01OUTSIDERNOTIFY000000000", "outsider_all@test.com")
    db_session.add_all([teammate, outsider])
    await db_session.commit()

    raw = await _manor_handler(
        entity_id=entity_id,
        user_id=owner["user_id"],
        action="notify_members",
        params={"title": "All-hands Friday"},
    )
    result = json.loads(raw)
    assert result["ok"] is True, result
    delivered = set(result["member_ids"])
    assert owner["user_id"] in delivered
    assert teammate.id in delivered
    assert outsider.id not in delivered
    assert result["delivered"] == len(delivered)

    # Outsider got no row at all.
    outsider_rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == outsider.id)
            )
        )
        .scalars()
        .all()
    )
    assert outsider_rows == []


@pytest.mark.asyncio
async def test_notify_members_drops_cross_entity_ids(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _register(client, "notify_scope_owner")
    entity_id = owner["entity_id"]
    outsider = _make_member("01OUTSIDERSCOPE0000000000", "outsider_scope@test.com")
    db_session.add(outsider)
    await db_session.commit()

    raw = await _manor_handler(
        entity_id=entity_id,
        user_id=owner["user_id"],
        action="notify_members",
        params={"title": "Hi", "member_ids": [owner["user_id"], outsider.id]},
    )
    result = json.loads(raw)
    assert result["ok"] is True, result
    # Only the in-entity owner is notified; the cross-entity id is dropped.
    assert result["member_ids"] == [owner["user_id"]]
    assert result["delivered"] == 1

    outsider_rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == outsider.id)
            )
        )
        .scalars()
        .all()
    )
    assert outsider_rows == []


@pytest.mark.asyncio
async def test_notify_members_requires_title(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _register(client, "notify_notitle_owner")
    raw = await _manor_handler(
        entity_id=owner["entity_id"],
        user_id=owner["user_id"],
        action="notify_members",
        params={"body": "no title here"},
    )
    result = json.loads(raw)
    assert result["ok"] is False
    assert "title" in result["error"]
