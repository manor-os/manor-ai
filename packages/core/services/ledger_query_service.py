"""Application adapters for the generic ledger query envelope.

The query engine is contract-neutral.  These small adapters only load and
validate records owned by installed Content, Finance, Recruiting, and Relationship contracts;
Blueprints choose the directory and contract, while the shared engine handles
filtering, grouping, pagination, and aggregates.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from packages.core.services.content_ledger import (
    CONTENT_LEDGER_CONTRACT_ID,
    DEFAULT_CONTENT_LEDGER_DIRECTORY,
    read_content_ledger,
)
from packages.core.services.finance_ledger import (
    DEFAULT_FINANCE_LEDGER_DIRECTORY,
    FINANCE_LEDGER_CONTRACT_ID,
    effective_finance_entries,
    read_finance_ledger,
)
from packages.core.services.relationship_ledger import (
    DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
    RELATIONSHIP_LEDGER_CONTRACT_ID,
    read_relationship_ledger,
)
from packages.core.services.recruiting_ledger import (
    DEFAULT_RECRUITING_LEDGER_DIRECTORY,
    RECRUITING_LEDGER_CONTRACT_ID,
    read_recruiting_ledger,
)
from packages.core.services.ledger_query import LedgerQueryError, query_ledger_rows


QUERYABLE_LEDGER_CONTRACTS = frozenset({
    CONTENT_LEDGER_CONTRACT_ID,
    FINANCE_LEDGER_CONTRACT_ID,
    RECRUITING_LEDGER_CONTRACT_ID,
    RELATIONSHIP_LEDGER_CONTRACT_ID,
})
_LEDGER_ALIASES = {
    "content_ledger": CONTENT_LEDGER_CONTRACT_ID,
    "finance_ledger": FINANCE_LEDGER_CONTRACT_ID,
    "recruiting_ledger": RECRUITING_LEDGER_CONTRACT_ID,
    "hr_ledger": RECRUITING_LEDGER_CONTRACT_ID,
    "people_ledger": RECRUITING_LEDGER_CONTRACT_ID,
    "relationship_ledger": RELATIONSHIP_LEDGER_CONTRACT_ID,
}
_DEFAULT_DIRECTORIES = {
    CONTENT_LEDGER_CONTRACT_ID: DEFAULT_CONTENT_LEDGER_DIRECTORY,
    FINANCE_LEDGER_CONTRACT_ID: DEFAULT_FINANCE_LEDGER_DIRECTORY,
    RECRUITING_LEDGER_CONTRACT_ID: DEFAULT_RECRUITING_LEDGER_DIRECTORY,
    RELATIONSHIP_LEDGER_CONTRACT_ID: DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
}
_LEDGER_RUNTIME_TOOLS = {
    CONTENT_LEDGER_CONTRACT_ID: ("read_content_ledger", "record_content_ledger"),
    FINANCE_LEDGER_CONTRACT_ID: ("read_finance_ledger", "record_finance_ledger"),
    RECRUITING_LEDGER_CONTRACT_ID: ("read_recruiting_ledger", "record_recruiting_ledger"),
    RELATIONSHIP_LEDGER_CONTRACT_ID: ("read_relationship_ledger", "record_relationship_ledger"),
}


def _query_rows_with_channel_alias(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    projected = []
    for source in rows:
        row = dict(source)
        payload = row.get("payload")
        channel = payload.get("channel") if isinstance(payload, Mapping) else None
        if isinstance(channel, str) and channel.strip():
            row["channel"] = channel.strip()
        projected.append(row)
    return projected


def workspace_queryable_ledger_configs(settings: object) -> dict[str, dict[str, Any]]:
    """Return installed, queryable ledger contracts from Workspace settings.

    Unknown application contracts are intentionally omitted until their
    validated query adapter is registered here. This prevents the runtime from
    advertising a query tool that can only return ``unsupported_contract``.
    """

    configs: dict[str, dict[str, Any]] = {}

    def visit(value: object, hinted_contract: str | None = None) -> None:
        if isinstance(value, str):
            contract_id = _LEDGER_ALIASES.get(value.strip().casefold(), value.strip())
            if contract_id in QUERYABLE_LEDGER_CONTRACTS:
                configs.setdefault(
                    contract_id,
                    {"contract_id": contract_id, "directory": _DEFAULT_DIRECTORIES[contract_id]},
                )
            return
        if isinstance(value, list):
            for item in value:
                visit(item, hinted_contract=hinted_contract)
            return
        if not isinstance(value, dict):
            return

        raw_contract = str(value.get("contract_id") or "").strip()
        contract_id = raw_contract or hinted_contract
        contract_id = _LEDGER_ALIASES.get(contract_id.casefold(), contract_id) if contract_id else ""
        if contract_id in QUERYABLE_LEDGER_CONTRACTS:
            directory = str(
                value.get("directory")
                or value.get("storage_directory")
                or _DEFAULT_DIRECTORIES[contract_id]
            ).strip()
            config = {
                "contract_id": contract_id,
                "directory": directory or _DEFAULT_DIRECTORIES[contract_id],
                "legacy_directories": tuple(
                    str(item).strip()
                    for item in (value.get("legacy_directories") or [])
                    if str(item).strip()
                ),
                "legacy_identity_fields": tuple(
                    str(item).strip()
                    for item in (value.get("legacy_identity_fields") or [])
                    if str(item).strip()
                ),
            }
            configs[contract_id] = config

        for key, child in value.items():
            key_text = str(key or "").strip().casefold()
            child_hint = _LEDGER_ALIASES.get(key_text) if key_text in _LEDGER_ALIASES else None
            if key_text in {"ledger_contracts", "ledger_configs"} or child_hint:
                visit(child, hinted_contract=child_hint)

    def scan_settings(value: object) -> None:
        if isinstance(value, list):
            for child in value:
                scan_settings(child)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            key_text = str(key or "").strip().casefold()
            child_hint = _LEDGER_ALIASES.get(key_text)
            if key_text in {"ledger_contracts", "ledger_configs"} or child_hint:
                visit(child, hinted_contract=child_hint)
            elif isinstance(child, (dict, list)):
                scan_settings(child)

    # The canonical list is authoritative, including an explicit empty list.
    # Legacy aliases are only a fallback for Workspaces not yet migrated.
    if isinstance(settings, dict) and "ledger_contracts" in settings:
        visit(settings.get("ledger_contracts"))
        return configs
    scan_settings(settings)
    return configs


def workspace_ledger_runtime_tools(
    settings: object,
    *,
    include_write: bool = False,
) -> set[str]:
    """Return contract tools implied by installed Workspace Ledger settings."""

    names: set[str] = set()
    for contract_id in workspace_queryable_ledger_configs(settings):
        read_name, write_name = _LEDGER_RUNTIME_TOOLS[contract_id]
        names.add(read_name)
        if include_write:
            names.add(write_name)
    return names


async def query_content_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_CONTENT_LEDGER_DIRECTORY,
    legacy_directories: tuple[str, ...] = (),
    legacy_identity_fields: tuple[str, ...] = (),
    filters: Mapping[str, Any] | None = None,
    date_field: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
    group_by: Iterable[str] = (),
    metrics: Iterable[str] = (),
    date_granularity: str | None = None,
    timezone_name: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    view: str = "current",
) -> dict[str, Any]:
    """Query a current content projection or the complete lifecycle history."""

    snapshot = await read_content_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        legacy_directories=legacy_directories,
        legacy_identity_fields=legacy_identity_fields,
        # Query aggregates must not be limited to the recent read window.
        recent_limit=0,
        create_location=False,
    )
    if view not in {"current", "events"}:
        raise LedgerQueryError("invalid_query", "view must be current or events")
    source_rows = snapshot["rows"] if view == "current" else snapshot["recent_entries"]
    rows = []
    for source in source_rows:
        row = dict(source)
        # Content records use ``status`` as the lifecycle event vocabulary.
        # Expose the generic alias so callers can use the same filter across
        # content and finance contracts without embedding Stickman semantics.
        row.setdefault("event", row.get("status"))
        rows.append(row)
    return query_ledger_rows(
        rows,
        contract_id=CONTENT_LEDGER_CONTRACT_ID,
        revision_rows=snapshot["recent_entries"],
        filters=filters,
        date_field=date_field,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        metrics=metrics,
        date_granularity=date_granularity,
        timezone_name=timezone_name,
        limit=limit,
        cursor=cursor,
    )


async def query_finance_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_FINANCE_LEDGER_DIRECTORY,
    filters: Mapping[str, Any] | None = None,
    date_field: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
    group_by: Iterable[str] = (),
    metrics: Iterable[str] = (),
    date_granularity: str | None = None,
    timezone_name: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Query effective finance totals or explicit lifecycle events."""

    snapshot = await read_finance_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        # Query aggregates must not be limited to the recent read window.
        recent_limit=0,
        create_location=False,
    )
    clean_filters = dict(filters or {})
    clean_group_by = tuple(group_by)
    clean_metrics = tuple(metrics)
    monetary_metrics = {
        "sum_amount_minor",
        "sum_tax_minor",
        "sum_fee_minor",
        "sum_inflow_minor",
        "sum_outflow_minor",
    }
    lifecycle_amount_query = bool(
        monetary_metrics.intersection(clean_metrics)
        and ("status" in clean_filters or "status" in clean_group_by)
    )
    uses_effective_projection = bool(
        monetary_metrics.intersection(clean_metrics)
        and not lifecycle_amount_query
    )
    source_rows = (
        effective_finance_entries(snapshot["entries"])
        if uses_effective_projection
        else snapshot["entries"]
    )
    result = query_ledger_rows(
        source_rows,
        contract_id=FINANCE_LEDGER_CONTRACT_ID,
        revision_rows=snapshot["entries"],
        filters=clean_filters,
        date_field=date_field,
        date_from=date_from,
        date_to=date_to,
        group_by=clean_group_by,
        metrics=clean_metrics,
        date_granularity=date_granularity,
        currency_field="currency",
        timezone_name=timezone_name,
        limit=limit,
        cursor=cursor,
    )
    result["projection_kind"] = (
        "current" if uses_effective_projection else "events"
    )
    return result


async def query_relationship_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_RELATIONSHIP_LEDGER_DIRECTORY,
    filters: Mapping[str, Any] | None = None,
    date_field: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
    group_by: Iterable[str] = (),
    metrics: Iterable[str] = (),
    date_granularity: str | None = None,
    timezone_name: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    view: str = "current",
) -> dict[str, Any]:
    """Query the current relationship projection or immutable interaction events."""

    snapshot = await read_relationship_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        recent_limit=0,
        create_location=False,
    )
    if view not in {"current", "events"}:
        raise LedgerQueryError("invalid_query", "view must be current or events")
    source_rows = _query_rows_with_channel_alias(
        snapshot["rows"] if view == "current" else snapshot["entries"]
    )
    return query_ledger_rows(
        source_rows,
        contract_id=RELATIONSHIP_LEDGER_CONTRACT_ID,
        revision_rows=snapshot["entries"],
        filters=filters,
        date_field=date_field,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        metrics=metrics,
        date_granularity=date_granularity,
        timezone_name=timezone_name,
        limit=limit,
        cursor=cursor,
    )


async def query_recruiting_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_RECRUITING_LEDGER_DIRECTORY,
    filters: Mapping[str, Any] | None = None,
    date_field: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
    group_by: Iterable[str] = (),
    metrics: Iterable[str] = (),
    date_granularity: str | None = None,
    timezone_name: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    view: str = "current",
) -> dict[str, Any]:
    """Query the current people projection or immutable lifecycle events."""

    snapshot = await read_recruiting_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        recent_limit=0,
        create_location=False,
    )
    if view not in {"current", "events"}:
        raise LedgerQueryError("invalid_query", "view must be current or events")
    source_rows = _query_rows_with_channel_alias(
        snapshot["rows"] if view == "current" else snapshot["entries"]
    )
    return query_ledger_rows(
        source_rows,
        contract_id=RECRUITING_LEDGER_CONTRACT_ID,
        revision_rows=snapshot["entries"],
        filters=filters,
        date_field=date_field,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        metrics=metrics,
        date_granularity=date_granularity,
        timezone_name=timezone_name,
        limit=limit,
        cursor=cursor,
    )


__all__ = [
    "LedgerQueryError",
    "QUERYABLE_LEDGER_CONTRACTS",
    "query_content_ledger",
    "query_finance_ledger",
    "query_recruiting_ledger",
    "query_relationship_ledger",
    "workspace_queryable_ledger_configs",
    "workspace_ledger_runtime_tools",
]
