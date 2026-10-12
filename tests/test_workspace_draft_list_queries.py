"""Draft list query bounds, suggestion visibility, and response compatibility."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from apps.api.routers.workspace_drafts import list_my_drafts
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.workspace_draft import WorkspaceDraft


def _capture_sql(db, statements):
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())
    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    return record


@pytest.mark.parametrize("count", [0, 1, 105])
async def test_draft_list_reads_at_most_two_queries_without_blueprint_payload(db_session, count):
    actor = SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid())
    ids = []
    for index in range(count):
        blueprint_id, draft_id = generate_ulid(), generate_ulid()
        ids.append(draft_id)
        db_session.add(WorkspaceBlueprint(
            id=blueprint_id, entity_id=actor.entity_id, slug=blueprint_id,
            title=f"Template {index}", summary="Public summary", tags=["test"],
            status="published", install_count=index, payload={"large_bundle": "x" * 4096},
        ))
        db_session.add(WorkspaceDraft(
            id=draft_id, entity_id=actor.entity_id, user_id=actor.id,
            status="active", suggested_blueprint_id=blueprint_id,
            fields={
                "name": f"Draft {index}",
                "sources": [{"id": "source", "text": "private source text", "chars": 19}],
                "_blueprint_source_payload": {"private": True},
            },
            messages=[
                {"role": "user", "content": "begin"},
                {"role": "system", "content": "private system message"},
                {"role": "user", "content": "My brief"},
                {"role": "assistant", "content": "Ready <workspace_setup>{}</workspace_setup>"},
            ],
            missing=["goal"], ready=False,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
        ))
    await db_session.flush()
    db_session.expunge_all()  # Cold identity map, as in a new HTTP request.
    statements = []
    listener = _capture_sql(db_session, statements)
    try:
        drafts = await list_my_drafts(user=actor, db=db_session, status="active")
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", listener)
    assert len(statements) == (2 if count else 1)
    assert [draft.id for draft in drafts] == list(reversed(ids))[:100]
    for index, draft in zip(reversed(range(count)), drafts):
        assert draft.fields["name"] == f"Draft {index}"
        assert draft.fields["sources"] == [{"id": "source", "chars": 19}]
        assert "_blueprint_source_payload" not in draft.fields
        assert draft.messages == [
            {"role": "user", "content": "My brief"},
            {"role": "assistant", "content": "Ready"},
        ]
        assert draft.missing == ["goal"]
        assert draft.ready is False
        assert draft.suggested_blueprint.title == f"Template {index}"
        assert draft.suggested_blueprint.install_count == index
    if count:
        assert "workspace_blueprints.payload" not in statements[1]
        assert "workspace_blueprints.description" not in statements[1]


async def test_draft_list_keeps_scope_status_and_suggestion_visibility(db_session):
    actor = SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid())
    published_id, archived_id = generate_ulid(), generate_ulid()
    for blueprint_id, status in [(published_id, "published"), (archived_id, "archived")]:
        db_session.add(WorkspaceBlueprint(
            id=blueprint_id, entity_id=actor.entity_id, slug=blueprint_id,
            title=status, payload={}, status=status,
        ))
    expected = {}
    for name, suggestion, applied in [
        ("visible", published_id, None),
        ("duplicate", published_id, None),
        ("archived", archived_id, None),
        ("missing", generate_ulid(), None),
        ("applied", published_id, published_id),
        ("none", None, None),
    ]:
        draft_id = generate_ulid()
        expected[draft_id] = published_id if name in {"visible", "duplicate"} else None
        db_session.add(WorkspaceDraft(
            id=draft_id, entity_id=actor.entity_id, user_id=actor.id, status="active",
            fields={}, messages=[], missing=[], ready=False,
            suggested_blueprint_id=suggestion, applied_blueprint_id=applied,
        ))
    for entity_id, user_id, status in [
        (generate_ulid(), actor.id, "active"),
        (actor.entity_id, generate_ulid(), "active"),
        (actor.entity_id, actor.id, "ready"),
    ]:
        db_session.add(WorkspaceDraft(
            entity_id=entity_id, user_id=user_id, status=status,
            fields={}, messages=[], missing=[], ready=False,
            suggested_blueprint_id=published_id,
        ))
    await db_session.flush()
    db_session.expunge_all()
    statements = []
    listener = _capture_sql(db_session, statements)
    try:
        drafts = await list_my_drafts(user=actor, db=db_session, status="active")
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", listener)
    assert len(statements) == 2  # Missing/archived hints must not trigger per-row fallback.
    assert {d.id: d.suggested_blueprint.id if d.suggested_blueprint else None for d in drafts} == expected


async def test_draft_list_without_suggestions_skips_blueprint_query(db_session):
    actor = SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid())
    db_session.add(WorkspaceDraft(
        entity_id=actor.entity_id, user_id=actor.id, status="ready",
        fields={}, messages=[], missing=[], ready=True,
    ))
    await db_session.flush()
    db_session.expunge_all()
    statements = []
    listener = _capture_sql(db_session, statements)
    try:
        drafts = await list_my_drafts(user=actor, db=db_session)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", listener)
    assert len(drafts) == 1
    assert drafts[0].status == "ready"
    assert len(statements) == 1
