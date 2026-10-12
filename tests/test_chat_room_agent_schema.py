"""Group Agent receipts retain their source and identity after binding removal."""
from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint

from packages.core.models.chat_room import ChatRoomAgentRun, ChatRoomMessage, ChatRoomToolApproval


def test_group_agent_run_model_has_unique_source_and_binding_snapshot():
    table = ChatRoomAgentRun.__table__
    assert table.c.binding_id.foreign_keys == set()
    assert table.c.audience_fingerprint.nullable is False
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns} == {"source_message_id", "binding_id"}
        for constraint in table.constraints
    )
    assert any(
        isinstance(constraint, ForeignKeyConstraint)
        and {column.name for column in constraint.columns} == {"source_message_id"}
        for constraint in table.constraints
    )


def test_group_agent_message_attribution_survives_binding_deletion():
    table = ChatRoomMessage.__table__
    assert table.c.sender_agent_binding_id.foreign_keys == set()
    assert table.c.reply_to_message_id.foreign_keys
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_chat_room_messages_agent_sender"
        for constraint in table.constraints
    )


def test_group_agent_migration_matches_model_columns(monkeypatch):
    config = Config("alembic.ini")
    config.set_main_option("script_location", "packages/core/migrations")
    revision = ScriptDirectory.from_config(config).get_revision("20260926_08").module
    added_columns = []
    created_tables = {}
    monkeypatch.setattr(revision.op, "add_column", lambda table, column: added_columns.append((table, column)))
    monkeypatch.setattr(revision.op, "create_check_constraint", lambda *args: None)
    monkeypatch.setattr(revision.op, "create_table", lambda name, *columns: created_tables.update({name: columns}))
    monkeypatch.setattr(revision.op, "create_index", lambda *args: None)
    revision.upgrade()

    assert {column.name for table, column in added_columns if table == "chat_room_messages"} == {
        "sender_agent_binding_id", "sender_agent_name", "sender_agent_avatar_url",
        "reply_to_message_id", "agent_run_status",
    }
    migration_columns = {
        column.name for column in created_tables["chat_room_agent_runs"]
        if isinstance(column, Column)
    }
    assert set(ChatRoomAgentRun.__table__.columns.keys()) - {
        "ai_tool_execution_revision", "checkpoint", "last_enqueued_at", "assistant_blocks",
    } == migration_columns


def test_room_tool_approval_migration_matches_additive_model(monkeypatch):
    config = Config("alembic.ini")
    config.set_main_option("script_location", "packages/core/migrations")
    revision = ScriptDirectory.from_config(config).get_revision("20260926_10").module
    added_columns = []
    created_tables = {}
    monkeypatch.setattr(revision.op, "add_column", lambda table, column: added_columns.append((table, column)))
    monkeypatch.setattr(revision.op, "drop_constraint", lambda *args, **kwargs: None)
    monkeypatch.setattr(revision.op, "create_check_constraint", lambda *args, **kwargs: None)
    monkeypatch.setattr(revision.op, "create_table", lambda name, *columns: created_tables.update({name: columns}))
    monkeypatch.setattr(revision.op, "create_index", lambda *args: None)
    revision.upgrade()
    assert {column.name for table, column in added_columns if table == "chat_room_agent_runs"} == {
        "ai_tool_execution_revision", "checkpoint",
    }
    migration_columns = {
        column.name for column in created_tables["chat_room_tool_approvals"]
        if isinstance(column, Column)
    }
    # The unified policy migration adds the frozen per-action requirements.
    assert set(ChatRoomToolApproval.__table__.columns.keys()) - {"approval_requirements"} == migration_columns
