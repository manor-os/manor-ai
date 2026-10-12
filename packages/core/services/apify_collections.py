"""One bounded Apify run per quote, with durable, exactly-once settlement.

Provider I/O is outside transactions. A lost start response never authorizes a
second start. Tokens are leased for a call and are never persisted here.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from sqlalchemy import or_, select

from packages.core.constants.plans import is_cloud
from packages.core.database import create_worker_session
from packages.core.models.apify_collection import ApifyCollection
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)
# NullPool is safe in API requests and across Celery asyncio.run() loops.
async_session = create_worker_session()
SOURCE_KIND = "apify_collection"
QUOTE_TTL_SECONDS = 900
CLAIM_SECONDS = 180
_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_TERMINAL = {"SUCCEEDED": "succeeded", "FAILED": "failed", "TIMED-OUT": "failed", "ABORTED": "canceled"}
_PROVIDER_STATUSES = {*_TERMINAL, "READY", "RUNNING", "ABORTING", "TIMING-OUT"}


class ApifyCollectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _credits(cost: Decimal, pricing: dict) -> int:
    return int((cost * (Decimal("1") + Decimal(pricing["markup"])) * int(pricing["credits_per_usd"])).to_integral_value(rounding=ROUND_CEILING))


def _pricing(scope: str) -> dict:
    if scope == "platform":
        if not is_cloud():
            raise ApifyCollectionError("platform_unavailable", "Platform-funded collection requires Manor Cloud.")
    return {"markup": "0", "credits_per_usd": 0}


def _view(row: ApifyCollection) -> dict:
    return {
        "collection_id": row.id, "quote_id": row.id,
        "status": row.status, "provider_status": row.provider_status,
        "billing_status": row.billing_status,
        "credential_scope": row.credential_scope,
        "mode": row.spec["mode"], "results_limit": row.spec["results_limit"],
        "window": row.spec.get("window"),
        "max_cost_usd": str(row.max_cost_usd), "max_credits": row.max_credits,
        "quote_expires_at": row.quote_expires_at.isoformat(),
        "provider_run_id": row.provider_run_id,
        "cost_usd": str(row.provider_cost_usd) if row.provider_cost_usd is not None else None,
        "charged_credits": row.charged_credits,
        "cancel_requested": row.cancel_requested_at is not None,
        "manual_review_required": row.status == "start_unknown",
        "last_error": row.last_error,
        "pricing": row.pricing,
    }


async def _authorize(db, *, entity_id: str, user_id: str, workspace_id: str | None,
                     credential_scope: str, account_id: str | None, write: bool = True) -> None:
    from packages.core.services.agent_permission_service import can_use_integration
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary, user_can_read_workspace_by_identity, user_can_write_workspace_id,
    )

    # Match entity deletion: entity -> Workspace -> collection/reservation.
    entity = (await db.execute(select(Entity).where(Entity.id == entity_id).with_for_update())).scalar_one_or_none()
    workspace = None
    if workspace_id:
        workspace = await lock_workspace_access_boundary(db, workspace_id=workspace_id, entity_id=entity_id)
        if not workspace or workspace.deleted_at is not None:
            raise ApifyCollectionError("scope_unavailable", "The collection Workspace is unavailable.")
    user = await db.get(User, user_id)
    if not entity or entity.deleted_at is not None or not user or user.deleted_at is not None or user.status != "active":
        raise ApifyCollectionError("scope_unavailable", "The collection owner is unavailable.")
    role = user.role
    if user.entity_id != entity_id:
        membership = (await db.execute(select(UserMembership).where(
            UserMembership.user_id == user_id, UserMembership.entity_id == entity_id,
            UserMembership.status == "active", UserMembership.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if not membership:
            raise ApifyCollectionError("access_denied", "The collection owner is outside this entity.")
        role = membership.role
    if workspace and not await user_can_read_workspace_by_identity(
        db, workspace=workspace, entity_id=entity_id, user_id=user_id, role=role,
    ):
        raise ApifyCollectionError("access_denied", "The collection Workspace is not accessible.")
    if write and workspace and not await user_can_write_workspace_id(
        db, workspace_id=workspace_id, entity_id=entity_id, user_id=user_id, role=role,
    ):
        raise ApifyCollectionError("access_denied", "Collection requires permission to work in this Workspace.")
    if credential_scope not in {"platform", "user", "entity", "team"}:
        raise ApifyCollectionError("credentials_unavailable", "Connect an Apify account or select platform credits.")
    decision = await can_use_integration(db, provider="apify", user_id=user_id, entity_id=entity_id,
                                         integration_account_id=account_id, allow_env_fallback=False)
    if not decision.allowed or decision.scope != credential_scope or (decision.account_id or None) != (account_id or None):
        raise ApifyCollectionError("credentials_unavailable", "The quoted Apify credential is no longer authorized.")


async def _owned(db, collection_id: str, scope: dict) -> ApifyCollection:
    row = (await db.execute(select(ApifyCollection).where(ApifyCollection.id == collection_id).with_for_update())).scalar_one_or_none()
    if row is None or any(getattr(row, key) != value for key, value in scope.items()):
        raise ApifyCollectionError("collection_not_found", "Collection not found in this user, Workspace and account scope.")
    return row


async def _locked(db, collection_id: str) -> ApifyCollection | None:
    entity_id = (await db.execute(select(ApifyCollection.entity_id).where(ApifyCollection.id == collection_id))).scalar_one_or_none()
    if entity_id is None:
        return None
    await db.execute(select(Entity.id).where(Entity.id == entity_id).with_for_update())
    return (await db.execute(select(ApifyCollection).where(ApifyCollection.id == collection_id).with_for_update())).scalar_one_or_none()


async def quote_collection(*, entity_id: str, user_id: str, workspace_id: str | None,
                           credential_scope: str, account_id: str | None, arguments: dict,
                           max_cost_usd: str = "1.00", agent_id: str | None = None,
                           conversation_id: str | None = None) -> dict:
    from packages.core.services.apify_client import prepare_collection
    cap = _decimal(max_cost_usd)
    configured_max = _decimal(os.getenv("APIFY_MAX_RUN_COST_USD", "5.00"))
    if cap is None or cap <= 0 or configured_max is None or configured_max <= 0 or cap > min(configured_max, Decimal("5")):
        raise ApifyCollectionError("invalid_budget", "The collection budget must be positive and within the configured per-run limit.")
    # Numeric(18,9) snapshots must preserve the exact customer-approved limit.
    if cap != cap.quantize(Decimal("0.000000001")):
        raise ApifyCollectionError("invalid_budget", "Use at most nine decimal places for the collection budget.")
    now = _now()
    spec = prepare_collection(arguments, now=now)
    pricing = _pricing(credential_scope)
    scope = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                 credential_scope=credential_scope, account_id=account_id)
    async with async_session() as db:
        await _authorize(db, **scope)
        row = ApifyCollection(id=generate_ulid(), **scope, agent_id=agent_id,
                              conversation_id=conversation_id, spec=spec, pricing=pricing,
                              max_cost_usd=cap, max_credits=_credits(cap, pricing),
                              quote_expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
                              status="quoted", billing_status="not_started")
        db.add(row)
        await db.commit()
        return _view(row)


async def _reserve(db, row: ApifyCollection) -> None:
    if row.credential_scope != "platform":
        return


async def _settle(db, row: ApifyCollection, cost: Decimal) -> None:
    if row.settled_at is not None:
        return
    row.provider_cost_usd = cost
    row.uncovered_cost_usd = max(Decimal("0"), cost - row.max_cost_usd)
    charge = min(row.max_credits, _credits(min(cost, row.max_cost_usd), row.pricing))
    if row.credential_scope == "platform":
        pass
    row.charged_credits = charge
    row.billing_status = "settled" if row.credential_scope == "platform" else "byok"
    row.settled_at = _now()
    row.next_check_at = None
    if row.uncovered_cost_usd:
        row.last_error = "provider_cost_exceeds_quote_platform_absorbs_difference"


async def _apply_run(db, row: ApifyCollection, run: dict) -> None:
    run_id = run.get("id") if isinstance(run, dict) else None
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id) or (row.provider_run_id and row.provider_run_id != run_id):
        raise ApifyCollectionError("provider_identity_mismatch", "Provider returned an invalid collection identity.")
    status = run.get("status")
    dataset = run.get("defaultDatasetId")
    if status not in _PROVIDER_STATUSES or (dataset is not None and (not isinstance(dataset, str) or not _ID.fullmatch(dataset))):
        raise ApifyCollectionError("invalid_provider_response", "Provider returned an invalid collection status or dataset.")
    if row.dataset_id and dataset and row.dataset_id != dataset:
        raise ApifyCollectionError("provider_identity_mismatch", "Provider changed the collection dataset identity.")
    row.provider_run_id = run_id
    row.dataset_id = row.dataset_id or dataset
    if row.settled_at is not None:
        return
    # Once terminal, stale/nonterminal reads cannot regress the persisted run.
    if row.provider_status in _TERMINAL and status not in _TERMINAL:
        return
    if row.provider_status in _TERMINAL and row.provider_status != status:
        raise ApifyCollectionError("provider_identity_mismatch", "Provider changed an already terminal collection status.")
    row.provider_status = status
    row.status = _TERMINAL.get(status, "running")
    row.last_error = None
    row.next_check_at = _now() + timedelta(seconds=60)
    if status in _TERMINAL:
        cost = _decimal(run.get("usageTotalUsd"))
        if cost is None or cost >= Decimal("1000000000"):
            if row.credential_scope == "platform":
                row.billing_status = "pending_cost"
                row.last_error = "provider_cost_not_final"
            else:
                row.billing_status = "byok"
                row.charged_credits = 0
                row.settled_at = _now()
                row.next_check_at = None
            return
        await _settle(db, row, cost)


async def start_collection(*, entity_id: str, user_id: str, workspace_id: str | None,
                           credential_scope: str, account_id: str | None, quote_id: str, token: str) -> dict:
    from packages.core.services.apify_client import ApifyClient, ApifyError
    scope = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                 credential_scope=credential_scope, account_id=account_id)
    async with async_session() as db:
        await _authorize(db, **scope)
        row = await _owned(db, quote_id, scope)
        if row.status != "quoted":
            return _view(row)
        if row.quote_expires_at <= _now():
            raise ApifyCollectionError("quote_expired", "This collection quote expired. Request a new quote.")
        client = ApifyClient(token)
        await _reserve(db, row)
        row.status = "starting"
        row.billing_status = "reserved" if credential_scope == "platform" else "byok_pending"
        row.started_at = _now()
        row.next_check_at = _now() + timedelta(seconds=CLAIM_SECONDS)
        spec, cap = row.spec, str(row.max_cost_usd)
        await db.commit()
    try:
        run = await client.start(spec, max_cost_usd=cap)
    except Exception as exc:
        async with async_session() as db:
            row = await _locked(db, quote_id)
            if row is None:
                raise ApifyCollectionError("collection_not_found", "Collection no longer exists.") from None
            if isinstance(exc, ApifyError) and not exc.uncertain:
                row.status = "failed"
                row.last_error = "provider_rejected_start"
                await _settle(db, row, Decimal("0"))
            else:
                row.status = "start_unknown"
                row.last_error = "start_outcome_unknown_manual_reconciliation_required"
                row.next_check_at = None
            await db.commit()
            return _view(row)
    async with async_session() as db:
        row = await _locked(db, quote_id)
        if row is None:
            raise ApifyCollectionError("collection_not_found", "Collection no longer exists.")
        # Bind a trustworthy returned id even if another response field is bad.
        run_id = run.get("id") if isinstance(run, dict) else None
        if isinstance(run_id, str) and _ID.fullmatch(run_id):
            row.provider_run_id = run_id
            row.status = "running"
            row.next_check_at = _now()
        if not row.provider_run_id:
            row.status = "start_unknown"
            row.last_error = "start_outcome_unknown_manual_reconciliation_required"
            row.next_check_at = None
        await db.commit()
        if not row.provider_run_id:
            return _view(row)
    # Preserve the known provider identity even if the separate ledger write fails.
    try:
        async with async_session() as db:
            row = await _locked(db, quote_id)
            await _apply_run(db, row, run)
            await db.commit()
            return _view(row)
    except Exception:
        async with async_session() as db:
            row = await _locked(db, quote_id)
            row.last_error = "reconciliation_pending"
            row.next_check_at = _now() + timedelta(seconds=60)
            await db.commit()
            return _view(row)


async def _reconcile(collection_id: str, token: str, *, cancel: bool = False) -> dict:
    from packages.core.services.apify_client import ApifyClient
    async with async_session() as db:
        row = await _locked(db, collection_id)
        if row is None:
            raise ApifyCollectionError("collection_not_found", "Collection not found.")
        if cancel:
            row.cancel_requested_at = row.cancel_requested_at or _now()
        if not row.provider_run_id or row.settled_at is not None:
            await db.commit()
            return _view(row)
        if row.claim_token and row.claim_expires_at and row.claim_expires_at > _now():
            await db.commit()
            return _view(row)
        claim = generate_ulid()
        row.claim_token = claim
        row.claim_expires_at = _now() + timedelta(seconds=CLAIM_SECONDS)
        run_id, should_abort = row.provider_run_id, row.cancel_requested_at is not None
        await db.commit()
    try:
        client = ApifyClient(token)
        run = await client.get_run(run_id)
        if should_abort and run.get("status") not in _TERMINAL:
            run = await client.abort_run(run_id)
        async with async_session() as db:
            row = await _locked(db, collection_id)
            if row and row.claim_token == claim:
                await _apply_run(db, row, run)
                row.claim_token = None
                row.claim_expires_at = None
                await db.commit()
                return _view(row)
    except Exception:
        # A transport/settlement failure never proves the provider stopped.
        async with async_session() as db:
            row = await _locked(db, collection_id)
            if row and row.claim_token == claim:
                row.last_error = "reconciliation_pending"
                row.next_check_at = _now() + timedelta(seconds=120)
                row.claim_token = None
                row.claim_expires_at = None
                await db.commit()
                return _view(row)
    async with async_session() as db:
        row = await db.get(ApifyCollection, collection_id)
        if row is None:
            raise ApifyCollectionError("collection_not_found", "Collection not found.")
        return _view(row)


async def get_collection(*, entity_id: str, user_id: str, workspace_id: str | None,
                         credential_scope: str, account_id: str | None, collection_id: str,
                         token: str, include_items: bool = True) -> dict:
    scope = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                 credential_scope=credential_scope, account_id=account_id)
    async with async_session() as db:
        await _authorize(db, **scope, write=False)
        await _owned(db, collection_id, scope)
        await db.commit()
    result = await _reconcile(collection_id, token)
    if include_items:
        from packages.core.services.apify_client import ApifyClient, normalize_items
        async with async_session() as db:
            await _authorize(db, **scope, write=False)
            row = await _owned(db, collection_id, scope)
            if row.result_snapshot is not None:
                result.update(row.result_snapshot)
                return result
            dataset, spec = row.dataset_id, row.spec
            can_fetch = (dataset and row.provider_status in _TERMINAL
                         and (not row.claim_token or not row.claim_expires_at or row.claim_expires_at <= _now()))
            if can_fetch:
                claim = generate_ulid()
                row.claim_token = claim
                row.claim_expires_at = _now() + timedelta(seconds=CLAIM_SECONDS)
            await db.commit()
        if can_fetch:
            try:
                items = await ApifyClient(token).get_items(dataset, limit=spec["results_limit"])
                snapshot = normalize_items(spec, items)
                async with async_session() as db:
                    await _authorize(db, **scope, write=False)
                    row = await _owned(db, collection_id, scope)
                    if row.claim_token == claim:
                        row.result_snapshot = snapshot
                        row.claim_token = row.claim_expires_at = None
                        await db.commit()
                        result.update(snapshot)
            except Exception:
                async with async_session() as db:
                    row = await _locked(db, collection_id)
                    if row and row.claim_token == claim:
                        row.claim_token = row.claim_expires_at = None
                        await db.commit()
                raise
    return result


async def cancel_collection(*, entity_id: str, user_id: str, workspace_id: str | None,
                            credential_scope: str, account_id: str | None, collection_id: str, token: str) -> dict:
    scope = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                 credential_scope=credential_scope, account_id=account_id)
    async with async_session() as db:
        await _authorize(db, **scope)
        row = await _owned(db, collection_id, scope)
        row.cancel_requested_at = row.cancel_requested_at or _now()
        if row.status == "quoted":
            row.status, row.billing_status = "canceled", "not_started"
            row.settled_at = _now()
            row.charged_credits = 0
        await db.commit()
    return await _reconcile(collection_id, token, cancel=True)


async def guard_collection_purge(db, *, entity_id: str, workspace_id: str | None = None,
                                 user_id: str | None = None) -> bool:
    """Defer purge while provider debt is open; the recovery sweep cancels deleted scopes."""
    await db.execute(select(Entity.id).where(Entity.id == entity_id).with_for_update())
    query = select(ApifyCollection).where(ApifyCollection.entity_id == entity_id)
    if workspace_id is not None:
        query = query.where(ApifyCollection.workspace_id == workspace_id)
    if user_id is not None:
        query = query.where(ApifyCollection.user_id == user_id)
    rows = (await db.execute(query.with_for_update())).scalars().all()
    pending = False
    for row in rows:
        if row.started_at is not None and row.settled_at is None:
            pending = True
    if pending:
        await db.flush()
        return False
    # Deleting private collection content must not refund already spent credit.
    # Scope this preservation to Apify; the existing purge policy for other
    # usage types is deliberately unchanged. This also covers receipts whose
    # user (and Collection row) was already purged earlier.
    if workspace_id is not None:
        pass
    for row in rows:
        await db.delete(row)
    return True


async def reconcile_pending_collections(*, limit: int = 25) -> dict:
    """Bounded recovery sweep; never starts an Actor or switches credential source."""
    from packages.core.ai.runtime.dynamic_mcp import runtime_resolve_mcp_bearer_token
    from packages.core.services.agent_permission_service import can_use_integration
    from packages.core.services.workspace_access import user_can_write_workspace_id
    async with async_session() as db:
        ids = list((await db.execute(select(ApifyCollection.id).where(
            ApifyCollection.started_at.is_not(None), ApifyCollection.settled_at.is_(None),
            or_(ApifyCollection.next_check_at.is_(None), ApifyCollection.next_check_at <= _now()),
            ApifyCollection.status != "start_unknown",
        ).order_by(ApifyCollection.next_check_at.asc().nullsfirst(), ApifyCollection.id).limit(max(1, min(limit, 100))))).scalars())
    counts = {"checked": 0, "settled": 0, "pending": 0, "manual_review": 0}
    for collection_id in ids:
        try:
            async with async_session() as db:
                row = await _locked(db, collection_id)
                if row is None or row.settled_at is not None:
                    continue
                counts["checked"] += 1
                if not row.provider_run_id:
                    row.status = "start_unknown"
                    row.last_error = "start_outcome_unknown_manual_reconciliation_required"
                    row.next_check_at = None
                    await db.commit()
                    counts["manual_review"] += 1
                    continue
                entity, user = await db.get(Entity, row.entity_id), await db.get(User, row.user_id)
                workspace = await db.get(Workspace, row.workspace_id) if row.workspace_id else None
                deleted = (not entity or entity.deleted_at is not None or not user or user.deleted_at is not None
                           or user.status != "active" or bool(row.workspace_id and (not workspace or workspace.deleted_at is not None)))
                decision = await can_use_integration(db, provider="apify", entity_id=row.entity_id, user_id=row.user_id,
                    integration_account_id=row.account_id, allow_env_fallback=False)
                authorized = decision.allowed and decision.scope == row.credential_scope and (decision.account_id or None) == row.account_id
                writable = True
                if workspace and user and not deleted:
                    role = user.role
                    if user.entity_id != row.entity_id:
                        membership = (await db.execute(select(UserMembership).where(
                            UserMembership.user_id == row.user_id, UserMembership.entity_id == row.entity_id,
                            UserMembership.status == "active", UserMembership.deleted_at.is_(None),
                        ))).scalar_one_or_none()
                        role = membership.role if membership else None
                    writable = await user_can_write_workspace_id(db, workspace_id=workspace.id,
                        entity_id=row.entity_id, user_id=row.user_id, role=role)
                if deleted or not authorized or not writable:
                    row.cancel_requested_at = row.cancel_requested_at or _now()
                scope = dict(server_key="apify", user_id=row.user_id, entity_id=row.entity_id,
                             scope=row.credential_scope, integration_account_id=row.account_id, allow_env_fallback=False)
                can_resolve = authorized or row.credential_scope == "platform"
                row.next_check_at = _now() + timedelta(seconds=120)
                await db.commit()
            token = await runtime_resolve_mcp_bearer_token(**scope, session_factory=async_session) if can_resolve else None
            if not token:
                async with async_session() as db:
                    row = await _locked(db, collection_id)
                    if row:
                        row.last_error = "original_credentials_unavailable_reconnect_to_reconcile"
                        await db.commit()
                counts["pending"] += 1
                continue
            result = await _reconcile(collection_id, token)
            counts["settled" if result["billing_status"] in {"settled", "byok"} else "pending"] += 1
        except Exception:
            # Log identifiers only: provider exceptions may contain credentials.
            logger.warning("Apify collection reconciliation deferred: %s", collection_id)
            counts["pending"] += 1
    return counts
