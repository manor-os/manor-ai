"""Skill edit version.

``skills.edit_version`` moves on every saved change, cosmetic ones included,
so editors can refuse to overwrite a save made since they loaded the Skill.
``revision`` keeps counting behaviour changes only.

Revision ID: 20261030_75
Revises: 20261030_60
"""

import sqlalchemy as sa

from packages.core.migrations.helpers import add_column_if_not_exists, drop_column_if_exists


revision = "20261030_75"
down_revision = "20261030_60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists(
        "skills", sa.Column("edit_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    drop_column_if_exists("skills", "edit_version")
