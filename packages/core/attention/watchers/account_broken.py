"""A connected account stopped working (an open credential incident)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.attention.contracts import DesiredItem
from packages.core.constants.attention import AttentionKind, AttentionUrgency
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User
from packages.core.services.account_incidents import INCIDENT_KEY
from packages.core.services.provider_keys import canonical_provider_key


def _opened_at(state: dict, fallback: datetime) -> datetime:
    raw = (state.get(INCIDENT_KEY) or {}).get("opened_at")
    try:
        parsed = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        parsed = None
    if parsed is None:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AccountBrokenWatcher:
    """One item per Workspace that depends on the account; else one entity item.

    Which Workspaces depend on a provider is the same scan the incident
    notices use (account_incident_delivery.scan_entity_workspaces).
    """

    key = "account_broken"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        from packages.core.services.account_incident_delivery import (
            _recovered_since_opened,
            provider_label,
            scan_entity_workspaces,
        )

        broken: list[tuple[str, str, str, str, dict]] = []  # (entity, subject type, id, provider, state)
        for integration in (await db.execute(
            select(Integration).where(
                Integration.status == "active",
                Integration.config[INCIDENT_KEY].is_not(None),
            )
        )).scalars().all():
            config = dict(integration.config or {})
            if not _recovered_since_opened(config):
                broken.append((integration.entity_id, "integration", integration.id, integration.provider, config))
        for account, owner in (await db.execute(
            select(OAuthAccount, User).join(User, User.id == OAuthAccount.user_id).where(
                OAuthAccount.profile[INCIDENT_KEY].is_not(None),
                User.status == "active",
            )
        )).all():
            profile = dict(account.profile or {})
            if not _recovered_since_opened(profile):
                broken.append((owner.entity_id, "oauth_account", account.id, account.provider, profile))

        scans: dict[str, list] = {}
        items: list[DesiredItem] = []
        for entity_id, subject_type, subject_id, provider, state in broken:
            if entity_id not in scans:
                scans[entity_id] = await scan_entity_workspaces(db, entity_id, now)
            key = canonical_provider_key(provider)
            affected = [workspace_id for workspace_id, _name, _owners, providers in scans[entity_id]
                        if key in providers]
            label = provider_label(provider)
            reason = (state.get(INCIDENT_KEY) or {}).get("reason")
            for workspace_id in affected or [None]:
                items.append(DesiredItem(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    kind=AttentionKind.ACCOUNT_BROKEN.value,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    urgency=AttentionUrgency.ACT_TODAY.value,
                    title=f"{label} needs reconnecting",
                    summary=f"{label} stopped working; agents that use it may be affected until it is reconnected.",
                    opened_at=_opened_at(state, now),
                    evidence={"provider": key, "reason": reason, "affected_workspace_ids": affected},
                ))
        return items
