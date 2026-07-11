"""Agent subscription resolver — one lookup → (agent, workspace, prompt, config).

Every inbound message through the channel gateway resolves to a single
``ResolvedSubscription`` before the agent runs. The resolver is the
choke point that decides *which* deployment answers a given message,
given the channel binding and the contact that sent it.

Priority (highest wins):
  1. ``ChannelContact.agent_subscription_id`` — per-sender pin. Lets a
     single shared bot route each customer to their own workspace.
  2. ``Channel.agent_subscription_id`` — channel default subscription.
  3. Legacy ``Channel.agent_id`` — synthesises a stub ``ResolvedSubscription``
     with ``id=None`` so old bindings keep working unchanged.

The output is a flat dataclass, not an ORM row, so callers don't care
whether the subscription exists in the DB or was synthesised on the fly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelContact
from packages.core.models.document import Channel
from packages.core.models.workspace import AgentSubscription

logger = logging.getLogger(__name__)


@dataclass
class ResolvedSubscription:
    """The agent-run-ready view of a channel deployment.

    ``id`` is ``None`` when synthesised from a legacy ``Channel.agent_id``
    with no stored ``AgentSubscription``. Memory / analytics scopes that
    want a stable key should prefer ``id`` when present, otherwise fall
    back to ``agent_id``.
    """
    id: Optional[str]
    agent_id: Optional[str]
    workspace_id: Optional[str]
    custom_prompt: Optional[str]
    config: dict = field(default_factory=dict)
    source: str = "channel"
    """Where this resolution came from — ``contact`` | ``channel`` | ``legacy``."""


# ── Resolve ─────────────────────────────────────────────────────────────────

async def resolve_subscription(
    db: AsyncSession,
    *,
    binding: Channel,
    contact: Optional[ChannelContact] = None,
) -> ResolvedSubscription:
    """Pick the right ``AgentSubscription`` for an inbound.

    Never raises — a missing / inactive subscription falls through to the
    next tier, and the legacy synth is the terminal fallback.
    """
    # 1. Per-contact pin
    sub_id = getattr(contact, "agent_subscription_id", None) if contact else None
    if sub_id:
        sub = await _load_active(db, sub_id)
        if sub:
            return _from_row(sub, source="contact")

    # 2. Channel default
    sub_id = getattr(binding, "agent_subscription_id", None)
    if sub_id:
        sub = await _load_active(db, sub_id)
        if sub:
            return _from_row(sub, source="channel")

    # 3. Legacy synth — no AgentSubscription row, just the bare agent
    return ResolvedSubscription(
        id=None,
        agent_id=getattr(binding, "agent_id", None),
        workspace_id=getattr(binding, "workspace_id", None),
        custom_prompt=None,
        config={},
        source="legacy",
    )


async def _load_active(db: AsyncSession, sub_id: str) -> Optional[AgentSubscription]:
    sub = (await db.execute(
        select(AgentSubscription).where(AgentSubscription.id == sub_id)
    )).scalar_one_or_none()
    if sub and sub.status == "active":
        return sub
    return None


def _from_row(sub: AgentSubscription, *, source: str) -> ResolvedSubscription:
    return ResolvedSubscription(
        id=sub.id,
        agent_id=sub.agent_id,
        workspace_id=sub.workspace_id,
        custom_prompt=sub.custom_prompt,
        config=sub.config or {},
        source=source,
    )


# ── Ensure ──────────────────────────────────────────────────────────────────

async def ensure_default_subscription(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    workspace_id: Optional[str] = None,
    name: Optional[str] = None,
) -> AgentSubscription:
    """Find-or-create an ``AgentSubscription`` for ``(agent, workspace)``.

    Used by the channel-binding UI when an admin picks "agent X in
    workspace Y" — we materialise (or reuse) a subscription row so the
    binding can point at it. ``workspace_id`` of ``None`` means the
    agent is deployed entity-wide (legacy-equivalent).
    """
    existing = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.agent_id == agent_id,
            # NOTE: ``workspace_id IS NULL`` comparison needs SQL IS, not ==
            (AgentSubscription.workspace_id == workspace_id)
            if workspace_id is not None
            else AgentSubscription.workspace_id.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return existing

    sub = AgentSubscription(
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        name=name or ("default" if workspace_id is None else None),
        status="active",
    )
    db.add(sub)
    await db.flush()
    return sub


# ── Convenience: force a resolution to be backed by a real row ──────────────

async def materialise_legacy(
    db: AsyncSession,
    *,
    binding: Channel,
) -> Optional[AgentSubscription]:
    """Promote a legacy ``Channel.agent_id`` binding into a real
    ``AgentSubscription`` and point the binding at it.

    Called lazily — e.g. when the Agent → Deployments UI edits a legacy
    row — so we don't mass-create rows we might never need. Returns the
    subscription (or None if the binding has no agent_id yet).
    """
    if not binding.agent_id:
        return None
    if binding.agent_subscription_id:
        existing = await _load_active(db, binding.agent_subscription_id)
        if existing:
            return existing

    sub = await ensure_default_subscription(
        db,
        entity_id=binding.entity_id,
        agent_id=binding.agent_id,
        workspace_id=binding.workspace_id,
        name=binding.name or "default",
    )
    binding.agent_subscription_id = sub.id
    await db.flush()
    return sub


__all__ = [
    "ResolvedSubscription",
    "resolve_subscription",
    "ensure_default_subscription",
    "materialise_legacy",
]
