"""Blueprint release history and Workspace upgrade history.

``blueprint_packages`` names each Blueprint by the durable id installs already
record; ``blueprint_versions`` keeps every release made current (append-only);
``blueprint_dist_tags`` points ``latest`` at one of them. The serving payload
columns are unchanged. ``workspace_blueprint_upgrades`` records each applied
in-place upgrade. Rows are filled by scripts/backfill_blueprint_registry.py.

Revision ID: 20261030_60
Revises: 20261029_30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import table_exists


revision = "20261030_60"
down_revision = "20261029_30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not table_exists("blueprint_packages"):
        op.create_table(
            "blueprint_packages",
            sa.Column("ref", sa.String(160), primary_key=True),
            sa.Column("name", sa.String(214), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("owner_entity_id", sa.String(26), nullable=True),
            sa.Column("listing_id", sa.String(160), nullable=True),
            sa.Column("delivery_id", sa.String(26), nullable=True),
            sa.Column("deprecated", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("name", name="uq_blueprint_packages_name"),
        )
        op.create_index("ix_blueprint_packages_owner_entity_id", "blueprint_packages", ["owner_entity_id"])
    if not table_exists("blueprint_versions"):
        op.create_table(
            "blueprint_versions",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column(
                "package_ref", sa.String(160),
                sa.ForeignKey("blueprint_packages.ref", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("format", sa.String(10), nullable=False),
            sa.Column("integrity", sa.String(100), nullable=True),
            sa.Column("content_fingerprint", sa.String(64), nullable=False),
            sa.Column("section_fingerprints", JSONB, nullable=False, server_default="{}"),
            sa.Column("payload", JSONB, nullable=True),
            sa.Column("manifest", JSONB, nullable=False, server_default="{}"),
            sa.Column("lock", JSONB, nullable=True),
            sa.Column("changelog", sa.Text(), nullable=True),
            sa.Column("min_bump", sa.String(10), nullable=True),
            sa.Column("visibility", sa.String(10), nullable=False, server_default="listed"),
            sa.Column("origin", sa.String(20), nullable=False, server_default="published"),
            sa.Column("deprecated", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("published_by", sa.String(26), nullable=True),
            sa.UniqueConstraint("package_ref", "version", name="uq_blueprint_versions_package_version"),
        )
        op.create_index(
            "ix_blueprint_versions_package_fingerprint", "blueprint_versions",
            ["package_ref", "content_fingerprint"],
        )
    if not table_exists("blueprint_dist_tags"):
        op.create_table(
            "blueprint_dist_tags",
            sa.Column(
                "package_ref", sa.String(160),
                sa.ForeignKey("blueprint_packages.ref", ondelete="CASCADE"), primary_key=True,
            ),
            sa.Column("tag", sa.String(64), primary_key=True),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_by", sa.String(26), nullable=True),
        )
    if not table_exists("workspace_blueprint_upgrades"):
        op.create_table(
            "workspace_blueprint_upgrades",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column(
                "workspace_id", sa.String(26),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("package_ref", sa.String(160), nullable=True),
            sa.Column("from_version", sa.String(64), nullable=True),
            sa.Column("to_version", sa.String(64), nullable=True),
            sa.Column("from_integrity", sa.String(100), nullable=True),
            sa.Column("to_integrity", sa.String(100), nullable=True),
            sa.Column("choices", JSONB, nullable=False, server_default="{}"),
            sa.Column("restore", JSONB, nullable=True),
            sa.Column("baseline_before", JSONB, nullable=True),
            sa.Column("applied_by", sa.String(26), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_workspace_blueprint_upgrades_workspace", "workspace_blueprint_upgrades",
            ["workspace_id", "applied_at"],
        )
        op.create_index(
            "ix_workspace_blueprint_upgrades_entity_id", "workspace_blueprint_upgrades", ["entity_id"],
        )


def downgrade() -> None:
    for table in (
        "workspace_blueprint_upgrades", "blueprint_dist_tags", "blueprint_versions", "blueprint_packages",
    ):
        if table_exists(table):
            op.drop_table(table)
