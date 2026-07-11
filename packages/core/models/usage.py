"""Token usage log model + tool-call log model.

These two append-only tables are the source of truth for per-call
observability. ``TokenUsageLog`` rolls up LLM token spend; ``ToolCallLog``
records every tool execution (timing, success, error). Both are indexed
on ``(entity_id, workspace_id, created_at)`` so admin slicers can group
by workspace cheaply.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Index, Integer, JSON, Numeric, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class TokenUsageLog(Base):
    """Append-only log of LLM token consumption per request."""
    __tablename__ = "token_usage_logs"
    __table_args__ = (
        Index("ix_token_usage_entity", "entity_id", "created_at"),
        Index("ix_token_usage_agent", "entity_id", "agent_id"),
        Index("ix_token_usage_workspace", "entity_id", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    model: Mapped[Optional[str]] = mapped_column(String(100))
    provider: Mapped[Optional[str]] = mapped_column(String(50))
    """Resolved provider name (anthropic / openai / google / novita / ...).
    Captured from the model-id prefix at log time so fallback routing
    is visible in slice-by-provider reports."""
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    """Tokens served from the prompt cache (billed at ~10% of normal
    input). A non-zero value means the cache hit on this request."""
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    """Tokens written to the prompt cache (billed at ~125% of normal
    input on the default 5-min TTL). Non-zero only on the *first*
    request that primes a new cache key."""
    context_breakdown: Mapped[Optional[Any]] = mapped_column(JSON)
    """Heuristic prompt-source attribution emitted by the agentic loop.

    Keys include system_tokens, history_tokens, file_tokens,
    tool_schema_tokens, tool_result_tokens, and user_input_tokens. This is
    diagnostic only; provider-reported token totals remain authoritative.
    """
    cost_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 6))
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    source: Mapped[Optional[str]] = mapped_column(String(50))
    billing_mode: Mapped[Optional[str]] = mapped_column(String(20))
    """Billing route for the call: platform / byok / mixed."""
    api_key_source: Mapped[Optional[str]] = mapped_column(String(30))
    """Credential source used for the call: platform / byok / legacy."""
    pricing_source: Mapped[Optional[str]] = mapped_column(String(50))
    """Pricing source used when estimating/displaying the row."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolCallLog(Base):
    """Append-only log of every tool execution.

    Sibling to ``TokenUsageLog``. Same dimensions (entity/workspace/agent
    /user/conversation) so a single admin query can stitch together
    "what did this agent spend money on AND what did it call". Written
    fire-and-forget from ``chat_logger.log_tool_exec``.
    """
    __tablename__ = "tool_call_logs"
    __table_args__ = (
        Index("ix_tool_call_entity_created", "entity_id", "created_at"),
        Index("ix_tool_call_workspace", "entity_id", "workspace_id", "created_at"),
        Index("ix_tool_call_tool_name", "entity_id", "tool_name", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    user_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(50))
    round_num: Mapped[Optional[int]] = mapped_column(Integer)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    result_chars: Mapped[Optional[int]] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    tool_args: Mapped[Optional[Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
