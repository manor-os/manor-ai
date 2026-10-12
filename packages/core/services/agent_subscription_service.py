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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelContact
from packages.core.models.document import Channel
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.services.reusable_resource_locks import (
    RESOURCE_AGENT,
    lock_reusable_resource_reference,
)

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


async def resolve_exact_binding_subscription(
    db: AsyncSession,
    *,
    subscription_id: str,
    entity_id: str,
    actor_user_id: str,
    actor_role: str | None = None,
) -> AgentSubscription:
    """Resolve one active Workspace deployment writable by the actor."""
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_access import user_can_write_workspace_id

    subscription = (await db.execute(
        select(AgentSubscription)
        .join(Workspace, Workspace.id == AgentSubscription.workspace_id)
        .where(
            AgentSubscription.id == subscription_id,
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.status == "active",
            Workspace.entity_id == entity_id,
            Workspace.status.in_(("active", "paused")),
            Workspace.deleted_at.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if subscription is None or not subscription.workspace_id:
        raise ValueError("Active AgentSubscription and Workspace not found")
    if not await user_can_write_workspace_id(
        db,
        workspace_id=subscription.workspace_id,
        entity_id=entity_id,
        user_id=actor_user_id,
        role=actor_role,
    ):
        raise ValueError("AgentSubscription Workspace is not writable")
    return subscription


# ── Resolve ─────────────────────────────────────────────────────────────────

async def resolve_subscription(
    db: AsyncSession,
    *,
    binding: Channel,
    contact: Optional[ChannelContact] = None,
) -> ResolvedSubscription:
    """Pick the right ``AgentSubscription`` for an inbound.

    Never raises. An invalid contact pin falls through to the channel default;
    an explicitly configured invalid channel default fails closed. The legacy
    synth is used only when no channel subscription was configured.
    """
    # 1. Per-contact pin
    sub_id = getattr(contact, "agent_subscription_id", None) if contact else None
    if sub_id and getattr(contact, "entity_id", None) == binding.entity_id:
        sub = await _load_active(db, sub_id, entity_id=binding.entity_id)
        if sub:
            return _from_row(sub, source="contact")

    # 2. Channel default
    sub_id = getattr(binding, "agent_subscription_id", None)
    if sub_id:
        sub = await _load_active(db, sub_id, entity_id=binding.entity_id)
        if sub and sub.workspace_id == binding.workspace_id:
            return _from_row(sub, source="channel")
        return ResolvedSubscription(
            id=None,
            agent_id=None,
            workspace_id=None,
            custom_prompt=None,
            config={},
            source="invalid",
        )

    # 3. Legacy synth — no AgentSubscription row, just the bare agent
    return ResolvedSubscription(
        id=None,
        agent_id=getattr(binding, "agent_id", None),
        workspace_id=getattr(binding, "workspace_id", None),
        custom_prompt=None,
        config={},
        source="legacy",
    )


async def _load_active(
    db: AsyncSession,
    sub_id: str,
    *,
    entity_id: str,
) -> Optional[AgentSubscription]:
    sub = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.id == sub_id,
            AgentSubscription.entity_id == entity_id,
        )
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


async def _ensure_master_backing_agent(db: AsyncSession) -> None:
    """Materialise the hidden FK identity without creating a deployment."""
    from packages.core.constants.agents import MANOR_AGENT_ID, MANOR_AGENT_NAME

    if await db.get(Agent, MANOR_AGENT_ID) is not None:
        return
    try:
        async with db.begin_nested():
            db.add(Agent(
                id=MANOR_AGENT_ID,
                entity_id=None,
                name=MANOR_AGENT_NAME,
                slug="manor-master-runtime",
                description="Internal backing identity for durable Manor AI deployments",
                config={"virtual_master": True},
                is_template=True,
                is_public=False,
                source="system",
                status="active",
            ))
            await db.flush()
    except IntegrityError:
        pass


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
            # Personal Initiative/DM deployments share workspace_id=NULL but
            # are never candidates for an entity-wide legacy channel binding.
            *(
                (AgentSubscription.owner_user_id.is_(None),)
                if workspace_id is None
                else ()
            ),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        return existing

    from packages.core.constants.agents import is_master_agent

    if is_master_agent(agent_id):
        await _ensure_master_backing_agent(db)
    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_AGENT,
        resource_id=agent_id,
    )
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


async def ensure_personal_subscription(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    owner_user_id: str,
    name: Optional[str] = None,
) -> AgentSubscription:
    """Find or create one owner-scoped, Workspace-free Agent deployment.

    This intentionally does not reuse ``ensure_default_subscription``: its
    historic Workspace-NULL rows are entity-wide and have no owner identity.
    Initiative and personal DM state must never cross that legacy boundary.

    Manor's first-party master is normally virtual.  We materialise one hidden
    platform backing row only when a user asks for durable subscription-owned
    behaviour; runtime still recognizes it through ``is_master_agent``.
    """
    from packages.core.constants.agents import (
        MANOR_AGENT_ID,
        is_master_agent,
    )

    clean_owner = str(owner_user_id or "").strip()
    if not clean_owner:
        raise ValueError("owner_user_id is required for a personal subscription")

    existing = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.agent_id == agent_id,
            AgentSubscription.workspace_id.is_(None),
            AgentSubscription.owner_user_id == clean_owner,
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        if existing.status != "active":
            existing.status = "active"
            await db.flush()
        return existing

    if is_master_agent(agent_id):
        await _ensure_master_backing_agent(db)
    else:
        await lock_reusable_resource_reference(
            db,
            entity_id=entity_id,
            resource_type=RESOURCE_AGENT,
            resource_id=agent_id,
        )
        agent = (await db.execute(
            select(Agent.id).where(
                Agent.id == agent_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if agent is None:
            raise ValueError("Agent not found")

    sub = AgentSubscription(
        entity_id=entity_id,
        agent_id=MANOR_AGENT_ID if is_master_agent(agent_id) else agent_id,
        owner_user_id=clean_owner,
        workspace_id=None,
        name=name or "Personal",
        status="active",
    )
    try:
        async with db.begin_nested():
            db.add(sub)
            await db.flush()
        return sub
    except IntegrityError:
        # The partial unique index is the concurrency authority.
        winner = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.agent_id == sub.agent_id,
                AgentSubscription.workspace_id.is_(None),
                AgentSubscription.owner_user_id == clean_owner,
            ).limit(1)
        )).scalar_one_or_none()
        if winner is None:
            raise
        return winner


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
        existing = await _load_active(
            db,
            binding.agent_subscription_id,
            entity_id=binding.entity_id,
        )
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
    "resolve_exact_binding_subscription",
    "resolve_subscription",
    "ensure_default_subscription",
    "ensure_personal_subscription",
    "materialise_legacy",
]
