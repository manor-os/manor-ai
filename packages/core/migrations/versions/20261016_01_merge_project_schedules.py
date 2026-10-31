"""Merge project-owned schedules with the public Task run history.

Preserve both deployed revision chains so either existing installation can
upgrade without reapplying its completed migrations.
"""

revision = "20261016_01"
down_revision = ("20261012_02", "20261014_04")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
