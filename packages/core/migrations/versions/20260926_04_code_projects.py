"""Add code projects and link sites to the project they were published from.

Revision ID: 20260926_04
Revises: 20260926_03
"""

from alembic import op
import sqlalchemy as sa

from packages.core.migrations.helpers import column_exists, index_exists, table_exists


revision = "20260926_04"
down_revision = "20260926_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not table_exists("code_projects"):
        op.create_table(
            "code_projects",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("workspace_id", sa.String(26), nullable=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("root_path", sa.String(1024), nullable=False),
            sa.Column("profile", sa.String(32), nullable=False, server_default="generic"),
            sa.Column("entry", sa.String(255), nullable=True),
            sa.Column("created_by_user_id", sa.String(26), nullable=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if not index_exists("uq_code_projects_entity_root"):
        op.create_index(
            "uq_code_projects_entity_root",
            "code_projects",
            ["entity_id", "root_path"],
            unique=True,
        )
    if not index_exists("ix_code_projects_entity_workspace"):
        op.create_index(
            "ix_code_projects_entity_workspace",
            "code_projects",
            ["entity_id", "workspace_id", "archived_at"],
        )

    if table_exists("sites"):
        if not column_exists("sites", "project_id"):
            op.add_column("sites", sa.Column("project_id", sa.String(26), nullable=True))
        if not index_exists("ix_sites_project"):
            op.create_index("ix_sites_project", "sites", ["project_id"])


def downgrade() -> None:
    if table_exists("sites"):
        if index_exists("ix_sites_project"):
            op.drop_index("ix_sites_project", table_name="sites")
        if column_exists("sites", "project_id"):
            op.drop_column("sites", "project_id")
    if table_exists("code_projects"):
        op.drop_table("code_projects")
