"""Regression: HitlRequest inserts must not violate updated_at NOT NULL.

Migration 20260721_01 builds ``updated_at`` on the request table (named
``approval_requests`` then, ``hitl_requests`` since 20260802_02) as
``NOT NULL DEFAULT now()``. ``TimestampMixin.updated_at`` carries no insert
default, so without a ``server_default`` on the model SQLAlchemy emits an
explicit ``NULL`` on INSERT — which tripped the NOT NULL constraint on the
migration-built (staging/prod) schema and 500'd every approval-request
creation (e.g. any approval-gated tool call, such as the
platform-announcement publish flow).

The create_all-based test schema previously hid this (model column was
nullable), so the approval E2E tests passed while staging failed — the same
model/migration drift the affiliate models already fixed. With the
server_default override, create_all builds ``NOT NULL DEFAULT now()`` and the
insert populates updated_at; without it, updated_at is NULL after insert and
these assertions fail.
"""
import pytest

from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid


@pytest.mark.asyncio
async def test_approval_request_insert_populates_updated_at(db_session):
    req = HitlRequest(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        action_key="external.social.publish",
        resource_kind="platform",
        risk_level="high",
        origin_kind="tool_call",
        status="pending",
        dedup_key=f"tool:{generate_ulid()}",
    )
    db_session.add(req)
    await db_session.flush()
    assert req.updated_at is not None
    assert req.created_at is not None


@pytest.mark.asyncio
async def test_channel_link_token_insert_populates_updated_at(db_session):
    """Same drift class: channel_link_tokens migration (20260520_04) declares
    updated_at NOT NULL DEFAULT now(); the model must override TimestampMixin's
    nullable updated_at so the ORM omits it on INSERT."""
    from datetime import datetime, timedelta, timezone

    from packages.core.models.channel import ChannelLinkToken

    tok = ChannelLinkToken(
        id=generate_ulid(),
        token="abc123def456",
        user_id=generate_ulid(),
        entity_id=generate_ulid(),
        channel_type="telegram",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    db_session.add(tok)
    await db_session.flush()
    assert tok.updated_at is not None
    assert tok.created_at is not None
