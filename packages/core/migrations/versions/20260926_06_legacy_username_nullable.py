"""Keep the public revision graph aligned with the private username repair.

The frozen OSS initial schema was generated from the modern User model and
never included users.username, so no public DDL is needed here.

Revision ID: 20260926_06
Revises: 20260926_05
"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = "20260926_06"
down_revision: Union[str, None] = "20260926_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
