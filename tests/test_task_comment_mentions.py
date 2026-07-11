"""Tests for task comment @mention parsing and staff notification fan-out."""
from __future__ import annotations

import pytest

from packages.core.services import task_comment_mentions


def test_parse_mention_items_splits_and_dedupes():
    raw = [
        {"type": "agent", "id": "01AGENTA"},
        {"type": "agent", "id": "01AGENTA"},          # dup dropped
        {"type": "user", "id": "01USERB"},
        {"type": "user", "id": ""},                    # empty id dropped
        {"type": "bogus", "id": "01X"},                # unknown type dropped
        "not-a-dict",                                   # junk dropped
        {"type": "agent", "id": 123},                  # non-str id dropped
    ]
    agent_ids, user_ids = task_comment_mentions.parse_mention_items(raw)
    assert agent_ids == ["01AGENTA"]
    assert user_ids == ["01USERB"]


def test_parse_mention_items_handles_none():
    assert task_comment_mentions.parse_mention_items(None) == ([], [])


def test_parse_mention_items_strips_whitespace_and_dedupes():
    """Ids with surrounding whitespace are normalized and deduplicated."""
    raw = [
        {"type": "agent", "id": " 01AGENTA "},
        {"type": "agent", "id": "01AGENTA"},   # same after strip — dup dropped
    ]
    agent_ids, user_ids = task_comment_mentions.parse_mention_items(raw)
    assert agent_ids == ["01AGENTA"]
    assert user_ids == []


def test_parse_mention_items_truncates_at_50():
    """Items beyond the 50th are silently dropped (denial-of-service guard)."""
    raw = [{"type": "agent", "id": f"AGENT{i:03d}"} for i in range(60)]
    agent_ids, user_ids = task_comment_mentions.parse_mention_items(raw)
    assert len(agent_ids) == 50
    assert agent_ids[0] == "AGENT000"
    assert agent_ids[49] == "AGENT049"
    assert user_ids == []


async def test_notify_mentioned_users_skips_author_and_calls_gateway(monkeypatch):
    calls = []

    async def fake_notify(entity_id, user_id, type, title, **kwargs):
        calls.append({"entity_id": entity_id, "user_id": user_id,
                      "type": type, "title": title, **kwargs})

    monkeypatch.setattr(task_comment_mentions, "_gateway_notify", fake_notify)

    await task_comment_mentions.notify_mentioned_users(
        entity_id="01ENTITY",
        author_user_id="01AUTHOR",
        author_label="Lin Fei",
        mentioned_user_ids=["01AUTHOR", "01USERB", "01USERC"],
        task_id="01TASK",
        task_log_id="01LOG",
        task_title="Fix landing page",
        comment="Please take a look @staff",
        workspace_id="01WS",
    )

    # author excluded, two remaining users notified
    assert [c["user_id"] for c in calls] == ["01USERB", "01USERC"]
    for c in calls:
        assert c["entity_id"] == "01ENTITY"
        assert c["type"] == "task_comment_mention"
        assert "Lin Fei" in c["title"]
        assert c["link"] == "/tasks/01TASK"
        assert c["meta"]["task_id"] == "01TASK"
        assert c["meta"]["task_log_id"] == "01LOG"
        assert c["workspace_id"] == "01WS"
        # channels NOT pinned — routing must honour user preferences
        assert "channels" not in c


async def test_notify_mentioned_users_swallows_gateway_errors(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("adapter down")

    monkeypatch.setattr(task_comment_mentions, "_gateway_notify", boom)
    # must not raise — one broken delivery can't break the comment flow
    await task_comment_mentions.notify_mentioned_users(
        entity_id="01ENTITY", author_user_id=None, author_label="Lin",
        mentioned_user_ids=["01USERB"], task_id="01TASK", task_log_id=None,
        task_title="T", comment="c", workspace_id=None,
    )


@pytest.mark.asyncio
async def test_validate_mentions_entity_scoping(db_session):
    """validate_mentions: entity-scoped agents/users pass; foreign & unknown ids drop; order preserved."""
    from datetime import datetime, timezone

    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent
    from packages.core.models.user import User, Entity

    run_id = generate_ulid()

    # Create two entities
    entity_a_id = generate_ulid()
    entity_b_id = generate_ulid()

    entity_a = Entity(id=entity_a_id, name="Entity A")
    entity_b = Entity(id=entity_b_id, name="Entity B")
    db_session.add_all([entity_a, entity_b])

    # Two agents in entity A, one in entity B
    agent_a1 = Agent(entity_id=entity_a_id, name="Alpha Agent")
    agent_a2 = Agent(entity_id=entity_a_id, name="Beta Agent")
    agent_b = Agent(entity_id=entity_b_id, name="Foreign Agent")
    db_session.add_all([agent_a1, agent_a2, agent_b])

    # A user belonging to entity A (via entity_id)
    user_a = User(
        entity_id=entity_a_id,
        email=f"user_a_{run_id}@test.com",
        password_hash="x",
        status="active",
        display_name="Alice",
    )
    # A soft-deleted user that should be dropped
    deleted_user = User(
        entity_id=entity_a_id,
        email=f"deleted_{run_id}@test.com",
        password_hash="x",
        status="active",
        display_name="Deleted",
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add_all([user_a, deleted_user])
    await db_session.commit()
    await db_session.refresh(agent_a1)
    await db_session.refresh(agent_a2)
    await db_session.refresh(agent_b)
    await db_session.refresh(user_a)
    await db_session.refresh(deleted_user)

    # ── Agent scoping: entity A agents pass; entity B and unknown ids drop; order preserved ──
    raw_agents = [
        {"type": "agent", "id": agent_a2.id},   # entity A — should pass (second)
        {"type": "agent", "id": agent_a1.id},   # entity A — should pass (first by payload order)
        {"type": "agent", "id": agent_b.id},    # entity B — must be dropped
        {"type": "agent", "id": generate_ulid()},  # unknown — must be dropped
    ]
    agent_items, user_items = await task_comment_mentions.validate_mentions(
        db_session, entity_id=entity_a_id, raw=raw_agents,
    )
    assert user_items == []
    assert [item["id"] for item in agent_items] == [agent_a2.id, agent_a1.id]
    assert agent_items[0]["name"] == "Beta Agent"
    assert agent_items[1]["name"] == "Alpha Agent"
    assert all(item["type"] == "agent" for item in agent_items)

    # ── User scoping: entity A user passes; soft-deleted user drops ──
    raw_users = [
        {"type": "user", "id": user_a.id},
        {"type": "user", "id": deleted_user.id},
        {"type": "user", "id": generate_ulid()},  # unknown
    ]
    agent_items2, user_items2 = await task_comment_mentions.validate_mentions(
        db_session, entity_id=entity_a_id, raw=raw_users,
    )
    assert agent_items2 == []
    assert [item["id"] for item in user_items2] == [user_a.id]
    assert user_items2[0]["name"] == "Alice"
    assert user_items2[0]["type"] == "user"
