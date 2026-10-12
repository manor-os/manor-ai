"""Reconcile public Knowledge privacy, snapshots, and browse indexes.

Revision ID: 20260924_03_oss
Revises: 20260913_02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import (
    add_column_if_not_exists,
    create_index_if_not_exists,
    column_exists,
    index_exists,
    table_exists,
)


revision = "20260924_03_oss"
down_revision = "20260913_02"
branch_labels = None
depends_on = None


def _append_role_permission(role_name: str, permission: str) -> None:
    op.execute(sa.text("""
        UPDATE staff_roles
        SET permissions = COALESCE(permissions, '[]'::jsonb) || CAST(:permission_json AS jsonb)
        WHERE lower(name) = :role_name
          AND NOT COALESCE(permissions, '[]'::jsonb) @> CAST(:permission_json AS jsonb)
    """).bindparams(
        role_name=role_name,
        permission_json=f'["{permission}"]',
    ))


def upgrade() -> None:
    if table_exists("document_versions"):
        add_column_if_not_exists(
            "document_versions", sa.Column("snapshot_key", sa.String(160))
        )
        add_column_if_not_exists(
            "document_versions", sa.Column("content_sha256", sa.String(64))
        )
        add_column_if_not_exists(
            "document_versions", sa.Column("workspace_id", sa.String(26))
        )
        add_column_if_not_exists(
            "document_versions",
            sa.Column(
                "provenance",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
        if not index_exists("uq_document_versions_snapshot_key"):
            op.create_index(
                "uq_document_versions_snapshot_key",
                "document_versions",
                ["document_id", "snapshot_key"],
                unique=True,
                postgresql_where=sa.text("snapshot_key IS NOT NULL"),
            )

    if table_exists("documents"):
        create_index_if_not_exists(
            "ix_documents_entity_owner",
            "documents",
            ["entity_id", "owner_id"],
        )
        create_index_if_not_exists(
            "ix_documents_browse_date",
            "documents",
            ["entity_id", "is_trashed", "created_at", "id"],
        )
        create_index_if_not_exists(
            "ix_documents_browse_folder_date",
            "documents",
            ["entity_id", "folder_id", "is_trashed", "created_at", "id"],
        )
        op.alter_column(
            "documents",
            "visibility",
            existing_type=sa.String(length=20),
            server_default="private",
            existing_nullable=False,
        )
        if table_exists("users"):
            membership_match = (
                """
                    OR EXISTS (
                        SELECT 1 FROM user_memberships AS membership
                        WHERE membership.user_id = matched.id
                          AND membership.entity_id = document.entity_id
                    )
                """
                if table_exists("user_memberships") else ""
            )
            # Historical created_by values may be a user ID or email. Only a
            # unique user who belonged to this entity may acquire ownership;
            # display names and conflicting evidence are not reliable proof.
            op.execute(sa.text(f"""
                WITH owner_candidates AS (
                    SELECT document.id, MIN(matched.id) AS owner_id
                    FROM documents AS document
                    JOIN users AS matched ON (
                        document.created_by = matched.id
                        OR lower(document.created_by) = lower(matched.email)
                        OR document.metadata -> 'origin' ->> 'user_id' = matched.id
                    )
                    WHERE document.owner_id IS NULL
                      AND document.visibility = 'entity'
                      AND (
                          matched.entity_id = document.entity_id
                          {membership_match}
                      )
                    GROUP BY document.id
                    HAVING COUNT(DISTINCT matched.id) = 1
                )
                UPDATE documents AS document
                SET owner_id = candidate.owner_id
                FROM owner_candidates AS candidate
                WHERE document.id = candidate.id
            """))
        workspace_exclusions = []
        if table_exists("workspaces"):
            workspace_exclusions.append("""
                EXISTS (
                    SELECT 1 FROM workspaces AS workspace
                    WHERE workspace.entity_id = documents.entity_id
                      AND (
                          workspace.artifact_folder_id IS NOT NULL
                          AND documents.fs_path LIKE
                              'Workspaces/_by_id/' || workspace.artifact_folder_id || '/%'
                      )
                )
            """)
            if table_exists("document_folders"):
                workspace_exclusions.append("""
                    EXISTS (
                        WITH RECURSIVE workspace_folder_tree AS (
                            SELECT folder.id
                            FROM document_folders AS folder
                            JOIN workspaces AS workspace
                              ON workspace.artifact_folder_id = folder.id
                            UNION
                            SELECT child.id
                            FROM document_folders AS child
                            JOIN workspace_folder_tree AS tree
                              ON child.parent_id = tree.id
                        )
                        SELECT 1 FROM workspace_folder_tree AS tree
                        WHERE tree.id = documents.folder_id
                    )
                """)
        if table_exists("document_group_members") and table_exists("document_groups"):
            workspace_exclusions.append("""
                EXISTS (
                    SELECT 1 FROM document_group_members AS member
                    JOIN document_groups AS collection ON collection.id = member.group_id
                    WHERE member.document_id = documents.id
                      AND collection.entity_id = documents.entity_id
                      AND collection.workspace_id IS NOT NULL
                )
            """)
        workspace_document_provenance = (
            "(" + " OR ".join(workspace_exclusions) + ")"
            if workspace_exclusions else "FALSE"
        )
        # Keep historical entity-wide shares intact. Only rows with durable
        # Workspace ownership are republished to their Workspace audience.
        op.execute(sa.text(f"""
            UPDATE documents
            SET visibility = 'workspace'
            WHERE visibility IN ('private', 'entity') AND {workspace_document_provenance}
        """))

    if table_exists("document_folders"):
        create_index_if_not_exists(
            "ix_document_folders_entity_owner",
            "document_folders",
            ["entity_id", "owner_id"],
        )
        op.alter_column(
            "document_folders",
            "visibility",
            existing_type=sa.String(length=20),
            server_default="private",
            existing_nullable=False,
        )
        workspace_folder_provenance = "FALSE"
        if table_exists("workspaces"):
            workspace_folder_provenance = """
                EXISTS (
                    WITH RECURSIVE workspace_folder_tree AS (
                        SELECT folder.id
                        FROM document_folders AS folder
                        JOIN workspaces AS workspace
                          ON workspace.artifact_folder_id = folder.id
                        UNION
                        SELECT child.id
                        FROM document_folders AS child
                        JOIN workspace_folder_tree AS tree
                          ON child.parent_id = tree.id
                    )
                    SELECT 1 FROM workspace_folder_tree AS tree
                    WHERE tree.id = document_folders.id
                )
            """
        op.execute(sa.text(f"""
            UPDATE document_folders
            SET visibility = 'workspace'
            WHERE visibility IN ('private', 'entity') AND {workspace_folder_provenance}
        """))
        if index_exists("uq_document_folders_entity_parent_name"):
            op.drop_index(
                "uq_document_folders_entity_parent_name",
                table_name="document_folders",
            )
        op.execute(sa.text("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_document_folders_entity_parent_owner_name
            ON document_folders (
                entity_id,
                COALESCE(parent_id, ''),
                COALESCE(owner_id, ''),
                name
            )
        """))

    if table_exists("staff_roles"):
        _append_role_permission("admin", "docs.transfer_departed")
        for permission in (
            "docs.read.all",
            "docs.manage.all",
            "docs.transfer_departed",
            "workspaces.read.all",
        ):
            _append_role_permission("owner", permission)


def downgrade() -> None:
    # Do not remove permission strings on downgrade: custom roles or existing
    # installations may have granted them independently before this revision.

    if table_exists("documents"):
        for index_name in (
            "ix_documents_browse_folder_date",
            "ix_documents_browse_date",
        ):
            if index_exists(index_name):
                op.drop_index(index_name, table_name="documents")
        op.alter_column(
            "documents",
            "visibility",
            existing_type=sa.String(length=20),
            server_default="entity",
            existing_nullable=False,
        )
    if table_exists("document_folders"):
        # Keep the owner-aware uniqueness index. Reintroducing the legacy
        # entity-wide name index can fail after two users have independently
        # created the same personal folder name.
        op.alter_column(
            "document_folders",
            "visibility",
            existing_type=sa.String(length=20),
            server_default="entity",
            existing_nullable=False,
        )
        if index_exists("ix_document_folders_entity_owner"):
            op.drop_index(
                "ix_document_folders_entity_owner",
                table_name="document_folders",
            )
    if table_exists("documents") and index_exists("ix_documents_entity_owner"):
        op.drop_index("ix_documents_entity_owner", table_name="documents")
    if table_exists("document_versions"):
        if index_exists("uq_document_versions_snapshot_key"):
            op.drop_index(
                "uq_document_versions_snapshot_key", table_name="document_versions"
            )
        for column_name in (
            "provenance",
            "workspace_id",
            "content_sha256",
            "snapshot_key",
        ):
            if column_exists("document_versions", column_name):
                op.drop_column("document_versions", column_name)
    # The Workspace visibility backfill is intentionally not undone here.
