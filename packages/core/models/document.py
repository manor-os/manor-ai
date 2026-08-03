"""Document, document group, and integration models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class VectorStatus:
    """Canonical vector_status values — single source of truth."""
    PENDING = "pending"
    PROCESSING = "processing"
    GENERATING = "generating"
    READY = "ready"
    INDEXED = "indexed"        # legacy alias for READY
    FAILED = "failed"
    SKIPPED = "skipped"

    # Grouped sets for convenience
    IN_PROGRESS = {PENDING, PROCESSING, GENERATING}
    DONE = {READY, INDEXED, SKIPPED}
    TERMINAL = {READY, INDEXED, FAILED, SKIPPED}


class DocumentGroup(Base, TimestampMixin):
    __tablename__ = "document_groups"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    vector_store_id: Mapped[Optional[str]] = mapped_column(String(255))
    settings: Mapped[dict] = mapped_column(JSONB, server_default="{}")


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_entity", "entity_id"),
        Index("ix_documents_name", "entity_id", "name"),
        Index("ix_documents_fs_path", "fs_path"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    fs_path: Mapped[Optional[str]] = mapped_column(String(1000))
    file_url: Mapped[Optional[str]] = mapped_column(String(1000))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger)
    file_type: Mapped[Optional[str]] = mapped_column(String(20))
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    # pgvector embedding is managed via raw SQL (not in model) to avoid
    # requiring the vector extension at table creation time.
    # The embedding column is added by Alembic migration when pgvector is available.
    vector_status: Mapped[str] = mapped_column(String(20), default="pending")
    source: Mapped[str] = mapped_column(String(20), default="upload")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    folder_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Trash / soft-delete fields
    is_trashed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    trashed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    trashed_by: Mapped[Optional[str]] = mapped_column(String(100))

    # ── Permission-v1 fields (see docs/PERMISSIONS_DESIGN_ZH.md §13) ─────
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default="entity")
    # private | workspace | entity | public
    classification: Mapped[str] = mapped_column(String(20), nullable=False, server_default="internal")
    # public | internal | confidential | restricted
    owner_id: Mapped[Optional[str]] = mapped_column(String(26))
    client_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    pii_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    quarantine_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="clean")
    # clean | pending_scan | quarantined | rejected


class DocumentChunk(Base):
    """A retrieval-sized slice of one document's extracted text.

    RAG used to embed one whole document into a single averaged vector
    (see index_document's history), which dilutes a long document into one
    point — a fact three pages in gets no more representation than the title.
    Chunks are embedded independently so retrieval finds the paragraph, not
    just the document.

    ``embedding`` (pgvector) is added via raw SQL in the creating migration,
    matching ``Document.embedding`` — see the comment there.
    """
    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_document", "document_id", "chunk_index"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    document_id: Mapped[str] = mapped_column(
        String(26), nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class DocumentFolder(Base, TimestampMixin):
    """A folder for organizing documents."""
    __tablename__ = "document_folders"
    __table_args__ = (
        Index("ix_document_folders_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String(26))

    # ── Permission-v1 fields ─────────────────────────────────────────────
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, server_default="entity")
    classification: Mapped[str] = mapped_column(String(20), nullable=False, server_default="internal")
    owner_id: Mapped[Optional[str]] = mapped_column(String(26))
    client_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


Index(
    "uq_document_folders_entity_parent_name",
    DocumentFolder.entity_id,
    func.coalesce(DocumentFolder.parent_id, ""),
    DocumentFolder.name,
    unique=True,
)


class DocumentGroupMember(Base):
    __tablename__ = "document_group_members"

    document_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Integration(Base, TimestampMixin):
    """Entity-scope integration credentials (company-wide systems).

    Used for integrations that live at the tenant level — Stripe, QuickBooks,
    org-level GitHub — as opposed to personal OAuth which lives in
    ``oauth_accounts``. Access is gated by ``required_permission``: before an
    agent can use these credentials via MCP, the acting user's role must
    include that permission.
    """
    __tablename__ = "integrations"
    __table_args__ = (
        Index("ix_integrations_entity_provider", "entity_id", "provider"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # Who connected this integration. Provenance only — an integration stays
    # shared entity-wide, and this never narrows who may use or edit it.
    # Deliberately NOT named ``created_by``/``owner_user_id``: those are the
    # ownership columns ResourceDescriptor.from_row() reads, and picking one
    # up here would silently turn a shared integration into a personal one.
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(26))
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Legacy plaintext credentials. Read via CredentialService.lease_integration
    # which transparently routes legacy_jsonb vs vault-encrypted rows. New
    # writes should call CredentialService.store_integration which clears
    # this field and populates credential_ref.
    credentials: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="legacy_jsonb",
    )
    required_permission: Mapped[Optional[str]] = mapped_column(String(64))
    # e.g. "mcp.quickbooks.use". None = any active staff member may use.


class Channel(Base, TimestampMixin):
    """Channel binding — connects a user's messaging channel to an agent
    (or, preferably, an AgentSubscription so the same agent can run
    against different workspaces with per-workspace prompts / tools /
    memory).

    Resolution order used by the gateway:
      1. ``ChannelContact.agent_subscription_id`` — per-sender pin, wins
         over channel default. Lets a single shared bot route customer A
         to Workspace-A and customer B to Workspace-B.
      2. ``Channel.agent_subscription_id`` — channel default.
      3. ``Channel.agent_id`` — legacy single-agent binding, synthesised
         into a stub subscription at dispatch time. Existing deployments
         keep working until the admin promotes the binding via the UI.
    """
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    config: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    status: Mapped[str] = mapped_column(String(20), default="active")
