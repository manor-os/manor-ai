"""Skill model — reusable prompt+tool bundles that agents can invoke.

AgentSkillBinding is the per-agent allowlist. Runtime rule for invoke_skill:
  * if Skill.is_public is true -> any agent in the entity may invoke it
  * otherwise -> an active AgentSkillBinding (agent_id, skill_id) is required
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class Skill(Base, TimestampMixin):
    """A reusable prompt+tool chain that agents can invoke."""
    __tablename__ = "skills"
    __table_args__ = (
        Index("ix_skills_entity", "entity_id"),
        Index("ix_skills_slug", "slug"),
        Index("ix_skills_category", "category"),
        Index("ix_skills_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[Optional[str]] = mapped_column(String(26))  # None = platform skill
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(100))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    output_format: Mapped[str] = mapped_column(String(50), default="text")
    category: Mapped[Optional[str]] = mapped_column(String(50))
    tags: Mapped[list] = mapped_column(ARRAY(String), server_default="{}")
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="active")


class AgentSkillBinding(Base, TimestampMixin):
    """Per-agent allowlist for private skills.

    Public skills (``Skill.is_public = true``) do not need a binding; they
    are callable by any agent in the entity. Private skills require a row
    here before ``invoke_skill`` will dispatch them.
    """
    __tablename__ = "agent_skill_bindings"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "skill_id", name="uq_agent_skill_bindings_pair"
        ),
        Index("ix_agent_skill_bindings_agent", "agent_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(26), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
