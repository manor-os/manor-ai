"""Owner-visible record of other members using a shared connection."""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models.base import Base, generate_ulid


class IntegrationConnectionUse(Base):
    """One lease of a connection's credential for someone other than its owner.

    Written when the credential is leased for the call, never storing tool
    arguments or results: the owner sees who used the account and for what.
    """

    __tablename__ = "integration_connection_uses"
    __table_args__ = (
        Index(
            "ix_integration_connection_uses_connection",
            "entity_id", "connection_kind", "connection_id", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    connection_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_id: Mapped[str] = mapped_column(String(26), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    server_key: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
