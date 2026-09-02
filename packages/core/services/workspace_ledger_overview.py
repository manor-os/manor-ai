"""Read-only visual summary of installed Workspace Ledger contracts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from typing import Any, Iterable

from packages.core.cache import cache
from packages.core.services.content_ledger import (
    CONTENT_LEDGER_CONTRACT_ID,
    ContentLedgerError,
    read_content_ledger,
)
from packages.core.services.finance_ledger import (
    FINANCE_LEDGER_CONTRACT_ID,
    FinanceLedgerError,
    effective_finance_entries,
    read_finance_ledger,
)
from packages.core.services.ledger_query_service import workspace_queryable_ledger_configs
from packages.core.services.recruiting_ledger import (
    RECRUITING_LEDGER_CONTRACT_ID,
    RecruitingLedgerError,
    read_recruiting_ledger,
)
from packages.core.services.relationship_ledger import (
    RELATIONSHIP_LEDGER_CONTRACT_ID,
    RelationshipLedgerError,
    read_relationship_ledger,
)
from packages.core.services.tool_cache_version import get_tool_cache_version


_LABELS = {
    CONTENT_LEDGER_CONTRACT_ID: ("content", "Content"),
    FINANCE_LEDGER_CONTRACT_ID: ("finance", "Finance"),
    RECRUITING_LEDGER_CONTRACT_ID: ("recruiting", "Recruiting & HR"),
    RELATIONSHIP_LEDGER_CONTRACT_ID: ("relationship", "Relationships"),
}

_OVERVIEW_CACHE_TTL_SECONDS = max(
    0,
    int(os.getenv("WORKSPACE_LEDGER_OVERVIEW_CACHE_TTL_SECONDS", "300") or 300),
)


class WorkspaceLedgerOverviewUnavailable(RuntimeError):
    """Stable read-side outage surfaced by the Workspace overview API."""

    code = "filesystem_unavailable"


def _value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _counts(rows: Iterable[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counter = Counter(_value(row.get(field)) for row in rows if _value(row.get(field)))
    return [
        {"key": key, "count": count}
        for key, count in sorted(
            counter.items(), key=lambda item: (-item[1], item[0])
        )[:20]
    ]


def _latest_timestamp(rows: Iterable[dict[str, Any]]) -> str | None:
    values: list[str] = []
    for row in rows:
        raw = row.get("recorded_at") or row.get("occurred_at")
        if isinstance(raw, datetime):
            values.append(raw.isoformat())
        elif raw:
            values.append(str(raw))
    return max(values) if values else None


def _base(config: dict[str, Any]) -> dict[str, Any]:
    contract_id = str(config["contract_id"])
    kind, title = _LABELS[contract_id]
    return {
        "contract_id": contract_id,
        "kind": kind,
        "title": title,
        "directory": config.get("directory"),
        "schema_version": 1,
        "projection_kind": (
            "event_stream"
            if contract_id == FINANCE_LEDGER_CONTRACT_ID
            else "current"
        ),
        "record_count": 0,
        "event_count": 0,
        "updated_at": None,
        "status_counts": [],
        "stage_counts": [],
        "totals": [],
    }


def _empty_or_raise(summary: dict[str, Any], exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    if code == "ledger_not_installed":
        return summary
    if code == "filesystem_unavailable":
        raise WorkspaceLedgerOverviewUnavailable(str(exc)) from exc
    raise exc


def _overview_cache_key(
    *,
    entity_id: str,
    workspace_id: str,
    configs: dict[str, dict[str, Any]],
    version: int,
) -> str:
    identity = json.dumps(
        {
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "configs": configs,
            "version": version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"workspace-ledger-overview:v1:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _cached_overview(value: object, *, workspace_id: str) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("workspace_id") != workspace_id:
        return None
    if not isinstance(value.get("ledgers"), list):
        return None
    return value


async def workspace_ledger_overview(
    *,
    entity_id: str,
    workspace_id: str,
    settings: object,
    allowed_contract_ids: Iterable[str] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Build a bounded, aggregate-only overview for the Workspace chat UI."""

    configs = workspace_queryable_ledger_configs(settings)
    if allowed_contract_ids is not None:
        allowed = {str(contract_id) for contract_id in allowed_contract_ids}
        configs = {
            contract_id: config
            for contract_id, config in configs.items()
            if contract_id in allowed
        }
    cache_key = ""
    if use_cache and _OVERVIEW_CACHE_TTL_SECONDS > 0:
        version = await get_tool_cache_version(entity_id, "ledgers")
        cache_key = _overview_cache_key(
            entity_id=entity_id,
            workspace_id=workspace_id,
            configs=configs,
            version=version,
        )
        cached = _cached_overview(
            await cache.get(cache_key),
            workspace_id=workspace_id,
        )
        if cached is not None:
            return cached

    ledgers: list[dict[str, Any]] = []
    for contract_id, config in configs.items():
        summary = _base(config)
        try:
            if contract_id == CONTENT_LEDGER_CONTRACT_ID:
                snapshot = await read_content_ledger(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    directory=str(config["directory"]),
                    legacy_directories=tuple(config.get("legacy_directories") or ()),
                    legacy_identity_fields=tuple(config.get("legacy_identity_fields") or ()),
                    recent_limit=0,
                    create_location=False,
                )
                rows = list(snapshot.get("rows") or [])
                events = list(snapshot.get("recent_entries") or [])
                summary.update({
                    "projection_kind": "current",
                    "record_count": len(rows),
                    "event_count": int(snapshot.get("entry_count") or 0),
                    "updated_at": _latest_timestamp(events),
                    "status_counts": _counts(rows, "status"),
                    "stage_counts": _counts(rows, "content_kind"),
                })
            elif contract_id == FINANCE_LEDGER_CONTRACT_ID:
                snapshot = await read_finance_ledger(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    directory=str(config["directory"]),
                    recent_limit=0,
                    create_location=False,
                )
                entries = list(snapshot.get("entries") or [])
                totals: dict[str, dict[str, Any]] = {}
                for entry in effective_finance_entries(entries):
                    currency = _value(entry.get("currency")) or "UNKNOWN"
                    row = totals.setdefault(currency, {
                        "currency": currency,
                        "inflow_minor": 0,
                        "outflow_minor": 0,
                    })
                    direction = _value(entry.get("direction"))
                    if direction == "inflow":
                        row["inflow_minor"] += int(entry.get("amount_minor") or 0)
                    elif direction == "outflow":
                        row["outflow_minor"] += int(entry.get("amount_minor") or 0)
                summary.update({
                    "record_count": len(entries),
                    "event_count": len(entries),
                    "updated_at": _latest_timestamp(entries),
                    "status_counts": _counts(entries, "status"),
                    "stage_counts": _counts(entries, "entry_type"),
                    "totals": [totals[key] for key in sorted(totals)],
                })
            elif contract_id == RECRUITING_LEDGER_CONTRACT_ID:
                snapshot = await read_recruiting_ledger(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    directory=str(config["directory"]),
                    recent_limit=0,
                    create_location=False,
                )
                rows = list(snapshot.get("rows") or [])
                events = list(snapshot.get("entries") or [])
                summary.update({
                    "projection_kind": "current",
                    "record_count": len(rows),
                    "event_count": len(events),
                    "updated_at": _latest_timestamp(events),
                    "status_counts": _counts(rows, "status"),
                    "stage_counts": _counts(rows, "stage"),
                    "subject_counts": _counts(rows, "subject_type"),
                })
            else:
                snapshot = await read_relationship_ledger(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    directory=str(config["directory"]),
                    recent_limit=0,
                    create_location=False,
                )
                rows = list(snapshot.get("rows") or [])
                events = list(snapshot.get("entries") or [])
                summary.update({
                    "projection_kind": "current",
                    "record_count": len(rows),
                    "event_count": len(events),
                    "updated_at": _latest_timestamp(events),
                    "status_counts": _counts(rows, "status"),
                    "stage_counts": _counts(rows, "stage"),
                    "subject_counts": _counts(rows, "subject_type"),
                })
        except (ContentLedgerError, FinanceLedgerError, RecruitingLedgerError, RelationshipLedgerError) as exc:
            summary = _empty_or_raise(summary, exc)
        ledgers.append(summary)

    result = {
        "workspace_id": workspace_id,
        "ledger_count": len(ledgers),
        "record_count": sum(int(item.get("record_count") or 0) for item in ledgers),
        "event_count": sum(int(item.get("event_count") or 0) for item in ledgers),
        "ledgers": ledgers,
    }
    if cache_key:
        await cache.set(cache_key, result, ttl=_OVERVIEW_CACHE_TTL_SECONDS)
    return result


__all__ = ["WorkspaceLedgerOverviewUnavailable", "workspace_ledger_overview"]
