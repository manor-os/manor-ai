"""Merge Group progress and client delivery migration branches.

The existing customer-locale checkpoint stays 20261002_03. This merge adds
no DDL: databases on either parent acquire only the missing branch.
"""

revision = "20261002_client_merge"
down_revision = ("20261002_02", "20261002_03")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
