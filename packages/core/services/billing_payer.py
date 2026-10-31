"""Who pays for AI spend in a Workspace.

The data owner of a Workspace (``Workspace.entity_id``) normally pays. While a
client delivery of that Workspace is still in a trial phase, the provider
company that sent it pays instead, whoever starts the run.

The payer comes from the accepted delivery row, never from
``Workspace.settings``: many writers replace settings wholesale, and a stale
or missing copy there must not move a charge.

``MANOR_TRIAL_PROVIDER_PAYS`` is the switch and the rollback lever. It is on
when unset; ``0``, ``false``, ``no`` or ``off`` turns it off, and every
Workspace's owner pays again. It is read on every call, so flipping it takes
effect without a restart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace
from packages.core.services.external_collaboration_access import TRIAL_PHASES

TRIAL_PAYER_ENV = "MANOR_TRIAL_PROVIDER_PAYS"


@dataclass(frozen=True)
class BillingPayer:
    entity_id: str
    sponsored: bool
    delivery_id: str | None


class TrialSponsorUnavailable(Exception):
    """The trial's provider company is gone, so nobody may be charged.

    Falling back to the customer would bill them for a trial they were told
    the provider pays for.
    """

    def __init__(self, delivery_id: str, provider_entity_id: str):
        self.delivery_id = delivery_id
        self.provider_entity_id = provider_entity_id
        super().__init__(f"Trial provider {provider_entity_id} for delivery {delivery_id} is unavailable")


def trial_provider_pays_enabled() -> bool:
    return os.getenv(TRIAL_PAYER_ENV, "true").strip().lower() not in {"0", "false", "no", "off"}


async def _accepted_delivery(
    db: AsyncSession, *, entity_id: str, workspace_id: str
) -> ClientWorkspaceDelivery | None:
    # Acceptance creates a fresh Workspace, so at most one row should match.
    # If several ever do, the latest acceptance decides, deterministically.
    return await db.scalar(
        select(ClientWorkspaceDelivery)
        .where(
            ClientWorkspaceDelivery.target_workspace_id == workspace_id,
            ClientWorkspaceDelivery.target_entity_id == entity_id,
            ClientWorkspaceDelivery.accepted_at.is_not(None),
        )
        .order_by(ClientWorkspaceDelivery.accepted_at.desc(), ClientWorkspaceDelivery.id.desc())
        .limit(1)
    )


async def resolve_billing_payer(
    db: AsyncSession, *, entity_id: str, workspace_id: str | None
) -> BillingPayer:
    """Return who pays for spend by ``entity_id`` in ``workspace_id``.

    A plain read with no locks: callers lock the payer they get back.
    Raises ``TrialSponsorUnavailable`` when a trial's provider is deleted.
    """
    owner = BillingPayer(entity_id=entity_id, sponsored=False, delivery_id=None)
    if not workspace_id or not trial_provider_pays_enabled():
        return owner
    delivery = await _accepted_delivery(db, entity_id=entity_id, workspace_id=workspace_id)
    if delivery is None or delivery.phase not in TRIAL_PHASES or delivery.entity_id == entity_id:
        return owner
    provider = await db.get(Entity, delivery.entity_id)
    if provider is None or provider.deleted_at is not None:
        raise TrialSponsorUnavailable(delivery.id, delivery.entity_id)
    return BillingPayer(entity_id=delivery.entity_id, sponsored=True, delivery_id=delivery.id)


async def payer_entity_id(db: AsyncSession, entity_id: str, workspace_id: str | None) -> str:
    return (await resolve_billing_payer(db, entity_id=entity_id, workspace_id=workspace_id)).entity_id


async def resolve_run_payer(
    db: AsyncSession | None, *, entity_id: str | None, workspace_id: str | None
) -> str | None:
    """Resolve once the payer a whole run pins, before its first AI call.

    ``db`` is reused when given; otherwise a short session is opened, and
    none at all when nothing can be sponsored. Returns None when no payer
    can be pinned (no entity, or the trial's provider is gone): every call of
    the run then resolves on its own and fails as a trial credit error.
    """
    if not entity_id:
        return None
    if not workspace_id or not trial_provider_pays_enabled():
        return entity_id
    try:
        if db is not None:
            return await payer_entity_id(db, entity_id, workspace_id)
        from packages.core.database import async_session

        async with async_session() as own:
            return await payer_entity_id(own, entity_id, workspace_id)
    except TrialSponsorUnavailable:
        return None


async def validate_payer(
    db: AsyncSession, *, entity_id: str, workspace_id: str | None, payer_entity_id: str
) -> None:
    """Refuse a pinned payer that is neither the owner nor this Workspace's provider.

    Any delivery phase is accepted: a payer pinned during the trial stays
    valid for the run it was pinned for after the phase moves on.
    """
    if payer_entity_id == entity_id:
        return
    if workspace_id:
        match = await db.scalar(
            select(ClientWorkspaceDelivery.id)
            .where(
                ClientWorkspaceDelivery.target_workspace_id == workspace_id,
                ClientWorkspaceDelivery.target_entity_id == entity_id,
                ClientWorkspaceDelivery.entity_id == payer_entity_id,
                ClientWorkspaceDelivery.accepted_at.is_not(None),
            )
            .limit(1)
        )
        if match is not None:
            return
    raise ValueError(
        f"Entity {payer_entity_id} cannot pay for spend by {entity_id} in Workspace {workspace_id}"
    )


async def sponsored_trial_workspaces(
    db: AsyncSession, provider_entity_id: str, *, include_ended: bool = False
) -> dict[str, str]:
    """Customer Workspaces whose trial this provider currently pays for.

    Maps Workspace id to "<customer company> / <Workspace name>". A purged
    Workspace (or customer) keeps its entry: the provider's charges for it
    outlive the purge and must still read as trial spend.

    ``include_ended`` also returns deliveries whose trial is over. Usage
    reports need them: the provider's charges in such a Workspace are still
    trial spend and must not fold into the provider's own sources.
    """
    conditions = [
        ClientWorkspaceDelivery.entity_id == provider_entity_id,
        ClientWorkspaceDelivery.target_workspace_id.is_not(None),
        ClientWorkspaceDelivery.accepted_at.is_not(None),
        ClientWorkspaceDelivery.target_entity_id != provider_entity_id,
    ]
    if not include_ended:
        conditions.append(ClientWorkspaceDelivery.phase.in_(TRIAL_PHASES))
    rows = (
        await db.execute(
            select(
                ClientWorkspaceDelivery.target_workspace_id,
                func.coalesce(Entity.name, "Deleted company"),
                func.coalesce(Workspace.name, "Deleted Workspace"),
            )
            .outerjoin(Workspace, Workspace.id == ClientWorkspaceDelivery.target_workspace_id)
            .outerjoin(Entity, Entity.id == ClientWorkspaceDelivery.target_entity_id)
            .where(*conditions)
            .order_by(ClientWorkspaceDelivery.accepted_at)
        )
    ).all()
    return {workspace_id: f"{customer} / {workspace}" for workspace_id, customer, workspace in rows}


TRIAL_USAGE_SOURCE = "customer_trial"


def trial_usage_label(name: str) -> str:
    """How a provider's usage report names one customer trial it paid for."""
    return f"Customer trial: {name}"
