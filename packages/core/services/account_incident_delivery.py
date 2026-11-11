"""Notify each open, un-notified connected-account incident once.

A personal OAuthAccount incident goes only to the account's own, active user.
An Integration incident's recipients are:
- the person who connected the account;
- the active owners of every active Workspace in the entity whose agents use
  that provider;
- the entity owners/admins, only when nobody else is found.

Delivery follows each person's notification settings (``notify()``). The
incident is stamped only after its notices were handed off. A retry reuses
the same idempotency key, so a crash between the two cannot double-send.

With the ``attention_delivery`` flag on, attention delivery tells about the
incident (``account_broken``) instead; it stamps ``notified_at`` the same way.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select

from packages.core.attention.delivery import delivery_enabled
from packages.core.database import async_session
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.account_incidents import INCIDENT_KEY, close_incident
from packages.core.services.notify import notify
from packages.core.services.provider_keys import canonical_provider_key

logger = logging.getLogger(__name__)
NOTIFICATION_KIND = "integration_account_issue"
MAX_DELIVERY_ATTEMPTS = 3


def provider_label(provider: str) -> str:
    return str(provider or "account").replace("_", " ").title()


async def _workspace_owner_ids(db, workspace_id: str, now: datetime) -> list[str]:
    rows = (await db.execute(
        select(WorkspaceStaff.user_id).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.status == "active",
            WorkspaceStaff.user_id.is_not(None),
            func.lower(WorkspaceStaff.role) == "owner",
            or_(WorkspaceStaff.expires_at.is_(None), WorkspaceStaff.expires_at > now),
        ).order_by(WorkspaceStaff.created_at.asc(), WorkspaceStaff.id.asc())
    )).scalars().all()
    return list(dict.fromkeys(str(value) for value in rows))


async def _workspace_providers(db, workspace: Workspace, user_id: str) -> set[str]:
    """Canonical provider keys this Workspace's agents and setup depend on."""
    from packages.core.services.workspace_connection_status import WorkspaceConnectionStatusFactory

    status = await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user_id)
    return {
        canonical_provider_key(item.get("provider"))
        for item in status.get("requirements") or []
        if item.get("provider")
    }


WorkspaceScan = list[tuple[str, str, list[str], set[str]]]  # (id, name, owner ids, provider keys)


async def scan_entity_workspaces(db, entity_id: str, now: datetime) -> WorkspaceScan:
    """Each active Workspace's owners and the providers it depends on."""
    scan: WorkspaceScan = []
    workspaces = (await db.execute(
        select(Workspace).where(
            Workspace.entity_id == entity_id,
            Workspace.status == "active",
            Workspace.deleted_at.is_(None),
        )
    )).scalars().all()
    for workspace in workspaces:
        owners = await _workspace_owner_ids(db, workspace.id, now)
        if not owners:
            continue
        try:
            # A savepoint per Workspace: a database error in one scan rolls
            # back only that scan, and the caller's session stays usable.
            async with db.begin_nested():
                providers = await _workspace_providers(db, workspace, owners[0])
        except Exception:
            logger.warning("account incident: provider scan failed for workspace %s", workspace.id, exc_info=True)
            continue
        scan.append((workspace.id, workspace.name, owners, providers))
    return scan


async def _recipients(
    db, integration: Integration, now: datetime, scans: dict[str, WorkspaceScan],
) -> tuple[list[str], list[str]]:
    """(user ids, names of affected Workspaces). ``scans`` caches each entity's scan for one run."""
    provider = canonical_provider_key(integration.provider)
    users: list[str] = [str(integration.owner_user_id)] if integration.owner_user_id else []
    affected: list[str] = []
    if integration.entity_id not in scans:
        scans[integration.entity_id] = await scan_entity_workspaces(db, integration.entity_id, now)
    for _workspace_id, name, owners, providers in scans[integration.entity_id]:
        if provider in providers:
            affected.append(name)
            users.extend(owners)
    if not users:
        users = [str(value) for value in (await db.execute(
            select(User.id).where(
                User.entity_id == integration.entity_id,
                User.status == "active",
                User.role.in_(("owner", "admin")),
            )
        )).scalars().all()]
    return list(dict.fromkeys(users)), affected


def _parse_aware(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _recovered_since_opened(config: dict) -> bool:
    """True only for a passing check that ran after the incident opened.

    Some producers (Nango auth events, reauth markers) open an incident
    without running a check, so an earlier pass says nothing about it.
    """
    health = config.get("last_health_check")
    incident = config.get(INCIDENT_KEY)
    if not isinstance(health, dict) or health.get("ok") is not True or not isinstance(incident, dict):
        return False
    checked_at = _parse_aware(health.get("checked_at"))
    opened_at = _parse_aware(incident.get("opened_at"))
    return checked_at is not None and opened_at is not None and checked_at > opened_at


def _copy(integration: Integration, affected: list[str]) -> tuple[str, str]:
    label = provider_label(integration.provider)
    title = f"{label} needs reconnecting"
    if affected:
        body = (f"{label} stopped working. Agents in {', '.join(affected)} that use it "
                f"may be affected until you reconnect.")
    else:
        body = f"{label} stopped working. Reconnect it so agents can use it again."
    return title, body


async def deliver_account_incidents(*, now: datetime | None = None, limit: int = 100) -> int:
    """Notify open, un-notified Integration incidents. Returns notices sent."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    async with async_session() as db:
        rows = (await db.execute(
            select(Integration).where(
                Integration.status == "active",
                Integration.config[INCIDENT_KEY].is_not(None),
                Integration.config[INCIDENT_KEY]["notified_at"].astext.is_(None),
            ).limit(limit)
        )).scalars().all()
        pending: list[tuple[Integration, list[str], list[str]]] = []
        scans: dict[str, WorkspaceScan] = {}
        told_by_attention: dict[str, bool] = {}
        for row in rows:
            if _recovered_since_opened(row.config or {}):
                # Recovered before anyone was told: close it silently.
                row.config = close_incident(dict(row.config or {}))
                continue
            if row.entity_id not in told_by_attention:
                told_by_attention[row.entity_id] = await delivery_enabled(db, row.entity_id)
            if told_by_attention[row.entity_id]:
                continue
            users, affected = await _recipients(db, row, now, scans)
            pending.append((row, users, affected))
        await db.commit()
        snapshot = [(row.id, row.entity_id, dict(row.config or {}), users, affected, *_copy(row, affected))
                    for row, users, affected in pending]

    for integration_id, entity_id, config, users, affected, title, body in snapshot:
        opened_at = config[INCIDENT_KEY]["opened_at"]
        delivered, failed = await _notify_each(
            users, entity_id=entity_id, title=title, body=body,
            meta={"integration_id": integration_id, "affected_workspaces": affected},
            idempotency_key=f"account-incident:{integration_id}:{opened_at}",
        )
        sent += delivered
        await _stamp_delivery(Integration, "config", integration_id, opened_at, failed=failed, now=now)
    return sent


async def deliver_oauth_account_incidents(*, now: datetime | None = None, limit: int = 100) -> int:
    """Notify the owner of each open, un-notified personal OAuthAccount incident.

    A personal account belongs to one person, so only that person is told,
    and only while their User is active. An incident whose owner is not
    active is stamped ``delivery_failed`` so it is not picked up every tick.
    """
    now = now or datetime.now(timezone.utc)
    sent = 0
    async with async_session() as db:
        rows = (await db.execute(
            select(OAuthAccount, User).outerjoin(User, User.id == OAuthAccount.user_id).where(
                OAuthAccount.profile[INCIDENT_KEY].is_not(None),
                OAuthAccount.profile[INCIDENT_KEY]["notified_at"].astext.is_(None),
            ).limit(limit)
        )).all()
        pending: list[tuple[str, str, str, str, str]] = []
        for account, owner in rows:
            profile = dict(account.profile or {})
            if _recovered_since_opened(profile):
                account.profile = close_incident(profile)
                continue
            if owner is None or owner.status != "active":
                logger.info("oauth incident %s: owner is not active; not notifying", account.id)
                account.profile = {**profile, INCIDENT_KEY: {
                    **profile[INCIDENT_KEY], "notified_at": now.isoformat(), "delivery_failed": True,
                }}
                continue
            if await delivery_enabled(db, owner.entity_id):
                continue
            pending.append((account.id, account.provider, owner.id, owner.entity_id,
                            profile[INCIDENT_KEY]["opened_at"]))
        await db.commit()

    for account_id, provider, user_id, entity_id, opened_at in pending:
        label = provider_label(provider)
        delivered, failed = await _notify_each(
            [user_id], entity_id=entity_id, title=f"{label} needs reconnecting",
            body=(f"Your {label} account stopped working. Anything that uses it "
                  f"may be affected until you reconnect."),
            meta={"oauth_account_id": account_id, "provider": provider},
            idempotency_key=f"oauth-incident:{account_id}:{opened_at}",
        )
        sent += delivered
        await _stamp_delivery(OAuthAccount, "profile", account_id, opened_at, failed=failed, now=now)
    return sent


async def _notify_each(
    users: list[str], *, entity_id: str, title: str, body: str, meta: dict, idempotency_key: str,
) -> tuple[int, bool]:
    """Hand one incident's notice to each user. Returns (notices sent, any failed)."""
    sent, failed = 0, False
    for user_id in users:
        try:
            await notify(
                entity_id=entity_id, user_id=user_id, type=NOTIFICATION_KIND,
                title=title, body=body, link="/integrations", severity="warn",
                meta=meta, idempotency_key=idempotency_key,
            )
            sent += 1
        except ValueError:
            # Same key, different copy: this incident was already delivered.
            logger.info("account incident %s already notified to %s", idempotency_key, user_id)
        except Exception:
            failed = True
            logger.warning("account incident notify failed for %s", user_id, exc_info=True)
    return sent, failed


async def stamp_incident_notified(db, subject_type: str, row_id: str, *, now: datetime) -> None:
    """Mark an open incident told, in the caller's transaction (attention delivery)."""
    model, column = (Integration, "config") if subject_type == "integration" else (OAuthAccount, "profile")
    row = (await db.execute(select(model).where(model.id == row_id).with_for_update())).scalar_one_or_none()
    state = dict(getattr(row, column) or {}) if row is not None else {}
    incident = state.get(INCIDENT_KEY)
    if isinstance(incident, dict) and not incident.get("notified_at"):
        setattr(row, column, {**state, INCIDENT_KEY: {**incident, "notified_at": now.isoformat()}})


async def _stamp_delivery(model, column: str, row_id: str, opened_at: str, *, failed: bool, now: datetime) -> None:
    """Record one delivery attempt on ``row.<column>`` under a row lock.

    Only the incident that was sent is stamped: if it closed or reopened in
    the meantime (``opened_at`` changed), nothing is written.
    """
    async with async_session() as db:
        row = (await db.execute(
            select(model).where(model.id == row_id).with_for_update()
        )).scalar_one_or_none()
        state = dict(getattr(row, column) or {}) if row is not None else {}
        incident = state.get(INCIDENT_KEY)
        if not isinstance(incident, dict) or incident.get("opened_at") != opened_at:
            return
        updated = dict(incident)
        if failed:
            # Leave it un-notified so the next tick retries under the same
            # key, but give up after a few failed attempts.
            updated["delivery_attempts"] = int(incident.get("delivery_attempts") or 0) + 1
            if updated["delivery_attempts"] >= MAX_DELIVERY_ATTEMPTS:
                updated["notified_at"] = now.isoformat()
                updated["delivery_failed"] = True
                logger.warning(
                    "account incident %s: giving up after %d failed deliveries",
                    row_id, updated["delivery_attempts"],
                )
        else:
            updated["notified_at"] = now.isoformat()
        setattr(row, column, {**state, INCIDENT_KEY: updated})
        await db.commit()
