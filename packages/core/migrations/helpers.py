"""Shared helpers for idempotent Alembic migrations.

Every DDL operation should be guarded so migrations are safe to re-run
(e.g. after create_all + stamp on fresh DBs, or after a partial failure).

Usage in a migration:

    from packages.core.migrations.helpers import (
        add_column_if_not_exists,
        create_table_if_not_exists,
        create_index_if_not_exists,
        column_exists,
        table_exists,
    )
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def table_exists(table_name: str) -> bool:
    """Check if a table exists in the current database."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(table_name):
        return False
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in columns


def index_exists(index_name: str) -> bool:
    """Check if an index exists (across all tables)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :name"
        ),
        {"name": index_name},
    )
    return result.fetchone() is not None


def add_column_if_not_exists(
    table_name: str, column: sa.Column
) -> None:
    """Add a column only if it doesn't already exist."""
    if not column_exists(table_name, column.name):
        op.add_column(table_name, column)


def create_index_if_not_exists(
    index_name: str, table_name: str, columns: list[str], **kwargs
) -> None:
    """Create an index only if it doesn't already exist."""
    if not index_exists(index_name):
        op.create_index(index_name, table_name, columns, **kwargs)


def drop_column_if_exists(table_name: str, column_name: str) -> None:
    """Drop a column only if it exists."""
    if column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)
