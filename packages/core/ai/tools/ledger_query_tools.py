"""Read-only generic ledger query tool.

The active Workspace Blueprint supplies the contract id and directory.  This
surface deliberately exposes no Stickman, YouTube, or provider vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
    runtime_tool_call_context_is_external_customer,
)
from packages.core.services.content_ledger import CONTENT_LEDGER_CONTRACT_ID, ContentLedgerError
from packages.core.services.finance_ledger import FINANCE_LEDGER_CONTRACT_ID, FinanceLedgerError
from packages.core.services.relationship_ledger import RELATIONSHIP_LEDGER_CONTRACT_ID, RelationshipLedgerError
from packages.core.services.recruiting_ledger import RECRUITING_LEDGER_CONTRACT_ID, RecruitingLedgerError
from packages.core.services.ledger_query import (
    LedgerQueryError,
    supported_ledger_query_metrics,
)
from packages.core.services.ledger_query_service import (
    workspace_queryable_ledger_configs,
    query_content_ledger,
    query_finance_ledger,
    query_recruiting_ledger,
    query_relationship_ledger,
)
from packages.core.services.workspace_ledger_overview import (
    WorkspaceLedgerOverviewUnavailable,
    workspace_ledger_overview,
)


logger = logging.getLogger(__name__)

_CONTRACT_ALIASES = {
    "content_ledger": CONTENT_LEDGER_CONTRACT_ID,
    "finance_ledger": FINANCE_LEDGER_CONTRACT_ID,
    "recruiting_ledger": RECRUITING_LEDGER_CONTRACT_ID,
    "hr_ledger": RECRUITING_LEDGER_CONTRACT_ID,
    "people_ledger": RECRUITING_LEDGER_CONTRACT_ID,
    "relationship_ledger": RELATIONSHIP_LEDGER_CONTRACT_ID,
    CONTENT_LEDGER_CONTRACT_ID: CONTENT_LEDGER_CONTRACT_ID,
    FINANCE_LEDGER_CONTRACT_ID: FINANCE_LEDGER_CONTRACT_ID,
    RECRUITING_LEDGER_CONTRACT_ID: RECRUITING_LEDGER_CONTRACT_ID,
    RELATIONSHIP_LEDGER_CONTRACT_ID: RELATIONSHIP_LEDGER_CONTRACT_ID,
}
_READ_TOOL_BY_CONTRACT = {
    CONTENT_LEDGER_CONTRACT_ID: "read_content_ledger",
    FINANCE_LEDGER_CONTRACT_ID: "read_finance_ledger",
    RECRUITING_LEDGER_CONTRACT_ID: "read_recruiting_ledger",
    RELATIONSHIP_LEDGER_CONTRACT_ID: "read_relationship_ledger",
}
_QUERY_VISUALIZATION_COLUMNS = {
    CONTENT_LEDGER_CONTRACT_ID: (
        "recorded_at",
        "identity_key",
        "content_kind",
        "status",
        "event",
    ),
    FINANCE_LEDGER_CONTRACT_ID: (
        "entry_id",
        "occurred_at",
        "entry_type",
        "status",
        "direction",
        "currency",
        "amount_minor",
        "account_ref",
        "counterparty_ref",
        "memo",
    ),
    RECRUITING_LEDGER_CONTRACT_ID: (
        "display_name",
        "record_key",
        "role_title",
        "department",
        "stage",
        "status",
        "event",
        "occurred_at",
        "recorded_at",
        "channel",
    ),
    RELATIONSHIP_LEDGER_CONTRACT_ID: (
        "display_name",
        "identity_key",
        "relationship_type",
        "stage",
        "status",
        "event",
        "occurred_at",
        "recorded_at",
        "channel",
    ),
}
_QUERY_GROUP_BY_FIELDS = {
    CONTENT_LEDGER_CONTRACT_ID: frozenset({
        "recorded_at",
        "identity_key",
        "content_kind",
        "file_type",
        "entry_type",
        "status",
        "event",
    }),
    FINANCE_LEDGER_CONTRACT_ID: frozenset({
        "entry_id",
        "occurred_at",
        "recorded_at",
        "entry_type",
        "status",
        "direction",
        "currency",
        "amount_minor",
        "account_ref",
        "counterparty_ref",
        "invoice_ref",
        "payment_ref",
        "source_system",
        "memo",
    }),
    RECRUITING_LEDGER_CONTRACT_ID: frozenset({
        "display_name",
        "record_key",
        "subject_type",
        "role_title",
        "department",
        "location",
        "manager_ref",
        "stage",
        "status",
        "event",
        "occurred_at",
        "recorded_at",
        "channel",
    }),
    RELATIONSHIP_LEDGER_CONTRACT_ID: frozenset({
        "display_name",
        "identity_key",
        "subject_type",
        "relationship_type",
        "stage",
        "status",
        "event",
        "occurred_at",
        "recorded_at",
        "channel",
    }),
}
_QUERY_GROUP_BY_SCHEMA_FIELDS = sorted(
    {
        field
        for fields in _QUERY_GROUP_BY_FIELDS.values()
        for field in fields
    }
)
_QUERY_VISUALIZATION_METRICS = {
    "count",
    "sum_amount_minor",
    "sum_tax_minor",
    "sum_fee_minor",
    "sum_inflow_minor",
    "sum_outflow_minor",
}
_QUERY_VISUALIZATION_DATE_FIELDS = {"recorded_at", "occurred_at"}
_QUERY_DATE_GRANULARITIES = {"day", "week", "month"}
_QUERY_VISUALIZATION_MAX_GROUPS = 24
_QUERY_VISUALIZATION_MAX_ROWS = 20
_QUERY_VISUALIZATION_MAX_COLUMNS = 10
_QUERY_PRESENTATION_MAX_SECTIONS = 4
_QUERY_PRESENTATION_SECTION_TYPES = {
    "metrics",
    "chart",
    "records",
    "profile",
    "projection",
}
_QUERY_PRESENTATION_CHART_TYPES = {"bar", "line", "area", "pie", "donut"}

QUERY_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_ledger",
        "description": (
            "Query a validated Workspace ledger with exact filters, date ranges, "
            "aggregates, grouping, and cursor pagination. Use this for counts, "
            "totals, trends, and bounded record lookup; use rag for document evidence. "
            "When the user asks what each record or transaction is, omit metrics "
            "and group_by so the host renders the bounded detailed record list. "
            "Finance monetary totals default to realized, non-superseded entries; "
            "filter or group by status to inspect lifecycle amounts. Monetary totals "
            "require filtering to one currency per query. "
            "Successful results are automatically rendered as safe inline HTML in "
            "Workspace Chat, so do not recreate the chart in markdown."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract_id": {
                    "type": "string",
                    "enum": [
                        CONTENT_LEDGER_CONTRACT_ID,
                        FINANCE_LEDGER_CONTRACT_ID,
                        RECRUITING_LEDGER_CONTRACT_ID,
                        RELATIONSHIP_LEDGER_CONTRACT_ID,
                        "content_ledger",
                        "finance_ledger",
                        "recruiting_ledger",
                        "hr_ledger",
                        "people_ledger",
                        "relationship_ledger",
                    ],
                },
                "directory": {"type": "string"},
                "legacy_directories": {"type": "array", "items": {"type": "string"}},
                "legacy_identity_fields": {"type": "array", "items": {"type": "string"}},
                "view": {
                    "type": "string",
                    "enum": ["current", "events"],
                    "description": "Content, recruiting, and relationship ledgers support current projection or full lifecycle events.",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Top-level or dotted-payload filters. Values may be exact scalars, arrays of "
                        "accepted values, or operator objects using $eq, $ne, $in, $not_in, $contains, "
                        "$contains_any, $contains_all, $exists, $gt, $gte, $lt, or $lte."
                    ),
                },
                "date_field": {
                    "type": "string",
                    "description": (
                        "Top-level or dotted-payload ISO date field, such as occurred_at or "
                        "payload.next_action_at."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": "ISO date or timestamp; date-only values use the current user's timezone.",
                },
                "date_to": {
                    "type": "string",
                    "description": "Inclusive ISO date or timestamp; date-only values use the current user's timezone.",
                },
                "group_by": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": _QUERY_GROUP_BY_SCHEMA_FIELDS,
                    },
                    "maxItems": 3,
                },
                "date_granularity": {
                    "type": "string",
                    "enum": sorted(_QUERY_DATE_GRANULARITIES),
                    "description": (
                        "Bucket recorded_at or occurred_at in the current user's "
                        "timezone before grouping. Required for projection reports."
                    ),
                },
                "metrics": {
                    "type": "array",
                    "maxItems": 6,
                    "description": (
                        "All Ledgers support count. Monetary sum metrics are valid "
                        "only for the Finance Ledger contract."
                    ),
                    "items": {
                        "type": "string",
                        "enum": [
                            "count",
                            "sum_amount_minor",
                            "sum_tax_minor",
                            "sum_fee_minor",
                            "sum_inflow_minor",
                            "sum_outflow_minor",
                        ],
                    },
                },
                "presentation": {
                    "type": "object",
                    "description": (
                        "Optional composable inline report. Choose sections that match "
                        "the user's question instead of forcing one default chart. Chart "
                        "sections require group_by. Projection requires group_by to contain "
                        "exactly one field, recorded_at or occurred_at, and is rendered as a clearly labelled "
                        "linear-trend estimate; set date_granularity to day, week, or month. "
                        "Profile is suitable for a filtered person, "
                        "candidate, customer, or other subject. The host safely renders the "
                        "spec; never provide HTML, CSS, or script."
                    ),
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string", "maxLength": 160},
                        "subtitle": {"type": "string", "maxLength": 240},
                        "sections": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": _QUERY_PRESENTATION_MAX_SECTIONS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": sorted(_QUERY_PRESENTATION_SECTION_TYPES),
                                    },
                                    "title": {"type": "string", "maxLength": 120},
                                    "chart": {
                                        "type": "string",
                                        "enum": sorted(_QUERY_PRESENTATION_CHART_TYPES),
                                    },
                                    "metric": {
                                        "type": "string",
                                        "enum": sorted(_QUERY_VISUALIZATION_METRICS),
                                    },
                                    "metrics": {
                                        "type": "array",
                                        "maxItems": 4,
                                        "items": {
                                            "type": "string",
                                            "enum": sorted(_QUERY_VISUALIZATION_METRICS),
                                        },
                                    },
                                    "columns": {
                                        "type": "array",
                                        "maxItems": _QUERY_VISUALIZATION_MAX_COLUMNS,
                                        "items": {"type": "string"},
                                    },
                                    "forecast_periods": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 12,
                                    },
                                },
                                "required": ["type"],
                            },
                        },
                    },
                    "required": ["sections"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _QUERY_VISUALIZATION_MAX_ROWS,
                    "description": "Records per page. Use next_cursor to retrieve later pages.",
                },
                "cursor": {"type": "string"},
                "coalesce_previous_visualization": {
                    "type": "boolean",
                    "description": (
                        "Set true only when this query refines a previous Ledger "
                        "query in the same response and both should share one inline "
                        "visualization. Leave false when the user requested separate "
                        "comparisons, even if one result is a subset of another."
                    ),
                },
            },
            "required": ["contract_id"],
        },
    },
}


def _validated_group_by(contract_id: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise LedgerQueryError("invalid_query", "group_by must be an array")
    if len(value) > 3:
        raise LedgerQueryError("invalid_query", "group_by supports at most 3 fields")

    allowed = _QUERY_GROUP_BY_FIELDS.get(contract_id, frozenset())
    clean: list[str] = []
    for raw_field in value:
        if not isinstance(raw_field, str) or not raw_field.strip():
            raise LedgerQueryError(
                "invalid_query",
                "group_by fields must be non-empty strings",
            )
        field = raw_field.strip()
        if field not in allowed:
            raise LedgerQueryError(
                "invalid_query",
                f"group_by field {field!r} is not allowed for {contract_id}",
            )
        if field not in clean:
            clean.append(field)
    return tuple(clean)


def _validated_date_granularity(
    value: Any,
    *,
    group_by: tuple[str, ...],
) -> str | None:
    if value is None or not str(value).strip():
        return None
    granularity = str(value).strip().casefold()
    if granularity not in _QUERY_DATE_GRANULARITIES:
        raise LedgerQueryError(
            "invalid_query",
            "date_granularity must be day, week, or month",
        )
    if not _QUERY_VISUALIZATION_DATE_FIELDS.intersection(group_by):
        raise LedgerQueryError(
            "invalid_query",
            "date_granularity requires grouping by recorded_at or occurred_at",
        )
    return granularity


def _validated_metrics(contract_id: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise LedgerQueryError("invalid_query", "metrics must be an array")
    if len(value) > 6:
        raise LedgerQueryError("invalid_query", "metrics supports at most 6 items")
    allowed = supported_ledger_query_metrics(contract_id)
    clean: list[str] = []
    for raw_metric in value:
        if not isinstance(raw_metric, str) or not raw_metric.strip():
            raise LedgerQueryError(
                "invalid_query",
                "metrics must contain non-empty strings",
            )
        metric = raw_metric.strip()
        if metric not in allowed:
            raise LedgerQueryError(
                "invalid_query",
                f"metric {metric!r} is not allowed for {contract_id}",
            )
        if metric not in clean:
            clean.append(metric)
    return tuple(clean)


def _visualization_scalar(value: Any) -> str | int | float | bool | None:
    """Return a small JSON scalar suitable for a durable chat block."""

    if value is None or isinstance(value, (str, bool, int)):
        clean = value
    elif isinstance(value, float):
        clean = value if math.isfinite(value) else None
    elif hasattr(value, "isoformat"):
        clean = value.isoformat()
    else:
        return None
    if isinstance(clean, str) and len(clean) > 160:
        return clean[:159] + "…"
    return clean


def _visualization_row_value(row: dict[str, Any], field: str) -> str | int | float | bool | None:
    value = row.get(field)
    if value is None and field == "channel":
        payload = row.get("payload")
        if isinstance(payload, dict):
            value = payload.get("channel")
    return _visualization_scalar(value)


def _presentation_text(value: Any, *, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerQueryError("invalid_query", f"presentation {field} must be a string")
    clean = " ".join(value.split()).strip()
    if not clean:
        return None
    if len(clean) > max_chars:
        raise LedgerQueryError(
            "invalid_query",
            f"presentation {field} must be at most {max_chars} characters",
        )
    return clean


def _validated_query_presentation(
    contract_id: str,
    value: Any,
    *,
    group_by: tuple[str, ...],
    date_granularity: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a bounded, data-only presentation recipe.

    The recipe is persisted in chat history and later rendered by host-owned
    HTML. It deliberately contains no markup, styles, URLs, or executable code.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise LedgerQueryError("invalid_query", "presentation must be an object")
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list) or not (
        1 <= len(raw_sections) <= _QUERY_PRESENTATION_MAX_SECTIONS
    ):
        raise LedgerQueryError(
            "invalid_query",
            f"presentation sections must contain 1 to {_QUERY_PRESENTATION_MAX_SECTIONS} items",
        )

    allowed_columns = set(_QUERY_VISUALIZATION_COLUMNS.get(contract_id, ()))
    allowed_metrics = supported_ledger_query_metrics(contract_id)
    sections: list[dict[str, Any]] = []
    for index, raw_section in enumerate(raw_sections):
        if not isinstance(raw_section, dict):
            raise LedgerQueryError(
                "invalid_query",
                f"presentation section {index + 1} must be an object",
            )
        section_type = str(raw_section.get("type") or "").strip()
        if section_type not in _QUERY_PRESENTATION_SECTION_TYPES:
            raise LedgerQueryError(
                "invalid_query",
                f"unsupported presentation section type {section_type!r}",
            )
        section: dict[str, Any] = {"type": section_type}
        title = _presentation_text(
            raw_section.get("title"),
            field=f"section {index + 1} title",
            max_chars=120,
        )
        if title:
            section["title"] = title

        raw_metric = raw_section.get("metric")
        if raw_metric is not None:
            metric = str(raw_metric).strip()
            if metric not in allowed_metrics:
                raise LedgerQueryError(
                    "invalid_query",
                    f"presentation metric {metric!r} is not allowed for {contract_id}",
                )
            section["metric"] = metric

        raw_metrics = raw_section.get("metrics")
        if raw_metrics is not None:
            if not isinstance(raw_metrics, list) or len(raw_metrics) > 4:
                raise LedgerQueryError(
                    "invalid_query",
                    "presentation section metrics must be an array of at most 4 items",
                )
            metrics = list(dict.fromkeys(str(metric).strip() for metric in raw_metrics))
            if any(metric not in allowed_metrics for metric in metrics):
                raise LedgerQueryError(
                    "invalid_query",
                    f"presentation contains a metric not allowed for {contract_id}",
                )
            if metrics:
                section["metrics"] = metrics

        raw_columns = raw_section.get("columns")
        if raw_columns is not None:
            if not isinstance(raw_columns, list) or len(raw_columns) > _QUERY_VISUALIZATION_MAX_COLUMNS:
                raise LedgerQueryError(
                    "invalid_query",
                    f"presentation section columns must contain at most {_QUERY_VISUALIZATION_MAX_COLUMNS} items",
                )
            columns = list(dict.fromkeys(str(column).strip() for column in raw_columns))
            if any(column not in allowed_columns for column in columns):
                raise LedgerQueryError(
                    "invalid_query",
                    f"presentation contains a column not allowed for {contract_id}",
                )
            if columns:
                section["columns"] = columns

        if section_type == "chart":
            if not group_by:
                raise LedgerQueryError(
                    "invalid_query",
                    "chart presentation requires group_by",
                )
            chart = str(raw_section.get("chart") or (
                "line" if _QUERY_VISUALIZATION_DATE_FIELDS.intersection(group_by) else "bar"
            )).strip()
            if chart not in _QUERY_PRESENTATION_CHART_TYPES:
                raise LedgerQueryError("invalid_query", f"unsupported chart type {chart!r}")
            section["chart"] = chart
        elif section_type == "projection":
            if (
                len(group_by) != 1
                or group_by[0] not in _QUERY_VISUALIZATION_DATE_FIELDS
            ):
                raise LedgerQueryError(
                    "invalid_query",
                    "projection presentation requires exactly one date-based group_by field",
                )
            if date_granularity is None:
                raise LedgerQueryError(
                    "invalid_query",
                    "projection presentation requires date_granularity",
                )
            periods = raw_section.get("forecast_periods", 4)
            if (
                not isinstance(periods, int)
                or isinstance(periods, bool)
                or not 1 <= periods <= 12
            ):
                raise LedgerQueryError(
                    "invalid_query",
                    "forecast_periods must be an integer between 1 and 12",
                )
            section["forecast_periods"] = periods
        sections.append(section)

    title = _presentation_text(value.get("title"), field="title", max_chars=160)
    subtitle = _presentation_text(value.get("subtitle"), field="subtitle", max_chars=240)
    presentation: dict[str, Any] = {"version": 1, "sections": sections}
    if title:
        presentation["title"] = title
    if subtitle:
        presentation["subtitle"] = subtitle
    return presentation


def _presentation_metrics(presentation: dict[str, Any] | None) -> tuple[str, ...]:
    if presentation is None:
        return ()
    requested: list[str] = []
    for section in presentation["sections"]:
        requested.extend(section.get("metrics") or ())
        metric = section.get("metric")
        if metric:
            requested.append(metric)
    return tuple(dict.fromkeys(requested))


def _query_ledger_visualization(
    result: dict[str, Any],
    *,
    contract_id: str,
    view: str,
    group_by: Any,
    metrics: Any,
    filters: dict[str, Any] | None,
    date_field: Any = None,
    date_from: Any = None,
    date_to: Any = None,
    date_granularity: Any = None,
    limit: Any = _QUERY_VISUALIZATION_MAX_ROWS,
    cursor: Any = None,
    presentation: dict[str, Any] | None = None,
    coalesce_previous_visualization: bool = False,
) -> dict[str, Any]:
    """Project a query result into a bounded, host-rendered visualization spec.

    The model chooses the query. This projection controls the persisted shape,
    row/column limits, and supported layouts so tool output can never inject
    executable HTML into Workspace Chat.
    """

    try:
        clean_group_by = list(_validated_group_by(contract_id, group_by))
    except LedgerQueryError:
        clean_group_by = []
    raw_date_granularity = result.get("date_granularity", date_granularity)
    clean_date_granularity = (
        raw_date_granularity.strip().casefold()
        if isinstance(raw_date_granularity, str)
        and raw_date_granularity.strip().casefold() in _QUERY_DATE_GRANULARITIES
        else None
    )
    allowed_metrics = supported_ledger_query_metrics(contract_id)
    clean_metrics = [
        str(metric)
        for metric in (metrics or ())
        if str(metric) in allowed_metrics
    ]
    aggregates = {
        str(key): int(value)
        for key, value in (result.get("aggregates") or {}).items()
        if str(key) in allowed_metrics
        and isinstance(value, int)
        and not isinstance(value, bool)
    }
    groups = []
    if clean_group_by:
        for group in (result.get("groups") or [])[:_QUERY_VISUALIZATION_MAX_GROUPS]:
            if not isinstance(group, dict):
                continue
            key = {
                str(field): _visualization_scalar(value)
                for field, value in (group.get("key") or {}).items()
                if str(field) in clean_group_by
            }
            group_aggregates = {
                str(metric): int(value)
                for metric, value in (group.get("aggregates") or {}).items()
                if str(metric) in allowed_metrics
                and isinstance(value, int)
                and not isinstance(value, bool)
            }
            groups.append({"key": key, "aggregates": group_aggregates})

    source_rows = [
        row for row in (result.get("rows") or [])
        if isinstance(row, dict)
    ][:_QUERY_VISUALIZATION_MAX_ROWS]
    columns = [
        field
        for field in _QUERY_VISUALIZATION_COLUMNS.get(contract_id, ())
        if any(_visualization_row_value(row, field) is not None for row in source_rows)
    ][:_QUERY_VISUALIZATION_MAX_COLUMNS]
    rows = [
        {
            column: _visualization_row_value(row, column)
            for column in columns
        }
        for row in source_rows
    ]

    matched_count = result.get("matched_count")
    matched_count = (
        matched_count
        if isinstance(matched_count, int) and not isinstance(matched_count, bool)
        else len(source_rows)
    )
    group_count = result.get("group_count")
    group_count = (
        group_count
        if clean_group_by
        and isinstance(group_count, int)
        and not isinstance(group_count, bool)
        else len(groups)
    )
    if matched_count <= 0:
        layout = "empty"
    elif clean_group_by:
        layout = (
            "timeline"
            if any(field in _QUERY_VISUALIZATION_DATE_FIELDS for field in clean_group_by)
            else "bar"
        )
    elif clean_metrics:
        layout = "metrics"
    else:
        layout = "table"

    currencies = {
        str(row.get("currency") or "").upper()
        for row in source_rows
        if str(row.get("currency") or "").strip()
    }
    filter_currency = _visualization_scalar((filters or {}).get("currency"))
    currency = (
        str(filter_currency).upper()
        if isinstance(filter_currency, str) and filter_currency
        else next(iter(currencies)) if len(currencies) == 1 else None
    )
    fingerprint_payload = {
        "contract_id": contract_id,
        "view": view,
        "filters": filters or {},
        "date_field": date_field,
        "date_from": date_from,
        "date_to": date_to,
        "group_by": clean_group_by,
        "date_granularity": clean_date_granularity,
        "metrics": clean_metrics,
        "limit": limit,
        "cursor": cursor,
        "ledger_revision": result.get("ledger_revision"),
        "timezone_name": result.get("timezone_name"),
    }
    if presentation is not None:
        fingerprint_payload["presentation"] = presentation
    query_fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    visualization = {
        "contract_id": contract_id,
        "view": view,
        "layout": layout,
        "matched_count": max(0, matched_count),
        "as_of": _visualization_scalar(result.get("as_of")),
        "group_by": clean_group_by,
        "date_granularity": clean_date_granularity,
        "metrics": clean_metrics,
        "aggregates": aggregates,
        "groups": groups,
        "group_count": max(len(groups), group_count),
        "groups_truncated": bool(clean_group_by and result.get("groups_truncated")),
        "columns": columns,
        "rows": rows,
        "has_more": bool(result.get("next_cursor")),
        "currency": currency,
        "query_fingerprint": query_fingerprint,
        "coalesce_previous_visualization": coalesce_previous_visualization,
    }
    if presentation is not None:
        visualization["presentation"] = presentation
    return visualization

VISUALIZE_WORKSPACE_LEDGERS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "visualize_workspace_ledgers",
        "description": (
            "Render the installed business Ledgers as an inline HTML overview in "
            "the current Workspace conversation. Call this when the user asks to "
            "see, visualize, summarize, or understand business, recruiting/HR, "
            "finance, content, or relationship Ledger status, or asks to configure "
            "Ledgers. An empty overview provides the manual configuration entry. "
            "The chat UI renders "
            "the returned aggregate visualization; do not recreate the chart in markdown."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _context_error() -> str:
    return _json(
        {
            "ok": False,
            "error": {
                "code": "missing_workspace_context",
                "message": "Entity and Workspace context are required",
            },
        }
    )


def _query_error(exc: LedgerQueryError) -> str:
    return _json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})


def _service_error(exc: ContentLedgerError | FinanceLedgerError | RecruitingLedgerError | RelationshipLedgerError) -> str:
    return _json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})


def _query_page_limit(value: Any) -> Any:
    if value is None:
        return _QUERY_VISUALIZATION_MAX_ROWS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return value
    return min(parsed, _QUERY_VISUALIZATION_MAX_ROWS) if parsed > 0 else parsed


def _query_ledger_model_envelope(
    result: dict[str, Any],
    visualization: dict[str, Any],
) -> dict[str, Any]:
    """Return only the bounded projection and cursor metadata to the model."""

    payload: dict[str, Any] = {
        "ok": True,
        "contract_id": visualization["contract_id"],
        "schema_version": result.get("schema_version", 1),
        "ledger_revision": _visualization_scalar(result.get("ledger_revision")),
        "matched_count": visualization["matched_count"],
        "returned_row_count": len(visualization["rows"]),
        "group_count": visualization["group_count"],
        "returned_group_count": len(visualization["groups"]),
        "groups_truncated": visualization["groups_truncated"],
        "next_cursor": _visualization_scalar(result.get("next_cursor")),
        "as_of": visualization["as_of"],
        "visualization": {
            "kind": "ledger_query_result",
            "data": visualization,
        },
    }
    projection_kind = result.get("projection_kind")
    if isinstance(projection_kind, str):
        payload["projection_kind"] = projection_kind
    return payload


async def _query_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if runtime_tool_call_context_is_external_customer(kwargs):
        return _json(
            {
                "ok": False,
                "error": {
                    "code": "ledger_query_not_available_on_external_surface",
                    "message": "Workspace ledger queries are restricted to internal Workspace agents",
                },
            }
        )
    contract_id = _CONTRACT_ALIASES.get(str(kwargs.get("contract_id") or "").strip())
    if not contract_id:
        return _json(
            {
                "ok": False,
                "error": {
                    "code": "unsupported_contract",
                    "message": "query_ledger requires a supported ledger contract_id",
                },
            }
        )
    required_read_tool = _READ_TOOL_BY_CONTRACT[contract_id]
    if (
        context.allowed_tool_names is not None
        and required_read_tool not in context.allowed_tool_names
    ):
        return _json(
            {
                "ok": False,
                "error": {
                    "code": "ledger_contract_not_bound",
                    "message": f"{contract_id} is not readable on this agent runtime surface",
                },
            }
        )
    try:
        group_by = _validated_group_by(contract_id, kwargs.get("group_by"))
        date_granularity = _validated_date_granularity(
            kwargs.get("date_granularity"),
            group_by=group_by,
        )
        presentation = _validated_query_presentation(
            contract_id,
            kwargs.get("presentation"),
            group_by=group_by,
            date_granularity=date_granularity,
        )
        requested_metrics = _validated_metrics(contract_id, kwargs.get("metrics"))
    except LedgerQueryError as exc:
        return _query_error(exc)
    query_metrics = tuple(dict.fromkeys([
        *requested_metrics,
        *_presentation_metrics(presentation),
    ]))
    filters = kwargs.get("filters")
    if filters is not None and not isinstance(filters, dict):
        return _json({"ok": False, "error": {"code": "invalid_query", "message": "filters must be an object"}})
    try:
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.user import User
        from packages.core.models.workspace import Workspace

        async with async_session() as db:
            workspace = (await db.execute(
                select(Workspace).where(
                    Workspace.id == context.workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                ).limit(1)
            )).scalar_one_or_none()
            timezone_name = None
            if context.user_id:
                timezone_name = (await db.execute(
                    select(User.timezone).where(
                        User.id == context.user_id,
                        User.deleted_at.is_(None),
                    ).limit(1)
                )).scalar_one_or_none()
        configs = workspace_queryable_ledger_configs(
            getattr(workspace, "settings", None) if workspace is not None else None
        )
        config = configs.get(contract_id)
        if config is None:
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": "ledger_contract_not_installed",
                        "message": f"{contract_id} is not installed in this Workspace",
                    },
                }
            )
        configured_directory = str(config.get("directory") or "").strip()
        requested_directory = str(kwargs.get("directory") or configured_directory).strip()
        if requested_directory != configured_directory:
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": "ledger_directory_not_allowed",
                        "message": "directory must match the installed Workspace ledger configuration",
                    },
                }
            )
        configured_legacy_directories = tuple(config.get("legacy_directories") or ())
        requested_legacy_directories = tuple(
            str(value).strip()
            for value in (kwargs.get("legacy_directories") or [])
            if str(value).strip()
        )
        if requested_legacy_directories and not set(requested_legacy_directories).issubset(
            set(configured_legacy_directories)
        ):
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": "ledger_directory_not_allowed",
                        "message": "legacy_directories must match the installed Workspace ledger configuration",
                    },
                }
            )
        configured_legacy_identity_fields = tuple(config.get("legacy_identity_fields") or ())
        requested_legacy_identity_fields = tuple(
            str(value).strip()
            for value in (kwargs.get("legacy_identity_fields") or [])
            if str(value).strip()
        )
        if requested_legacy_identity_fields and not set(requested_legacy_identity_fields).issubset(
            set(configured_legacy_identity_fields)
        ):
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": "ledger_field_not_allowed",
                        "message": "legacy_identity_fields must match the installed Workspace ledger configuration",
                    },
                }
            )
        common = {
            "entity_id": entity_id,
            "workspace_id": context.workspace_id,
            "filters": filters,
            "date_field": kwargs.get("date_field"),
            "date_from": kwargs.get("date_from"),
            "date_to": kwargs.get("date_to"),
            "group_by": list(group_by),
            "metrics": query_metrics,
            "date_granularity": date_granularity,
            "timezone_name": timezone_name,
            "limit": _query_page_limit(kwargs.get("limit")),
            "cursor": kwargs.get("cursor"),
        }
        if contract_id == CONTENT_LEDGER_CONTRACT_ID:
            result = await query_content_ledger(
                **common,
                directory=configured_directory,
                legacy_directories=tuple(
                    requested_legacy_directories or configured_legacy_directories
                ),
                legacy_identity_fields=tuple(
                    requested_legacy_identity_fields or configured_legacy_identity_fields
                ),
                view=str(kwargs.get("view") or "current"),
            )
        elif contract_id == FINANCE_LEDGER_CONTRACT_ID:
            result = await query_finance_ledger(
                **common,
                directory=configured_directory,
            )
        elif contract_id == RECRUITING_LEDGER_CONTRACT_ID:
            result = await query_recruiting_ledger(
                **common,
                directory=configured_directory,
                view=str(kwargs.get("view") or "current"),
            )
        else:
            result = await query_relationship_ledger(
                **common,
                directory=configured_directory,
                view=str(kwargs.get("view") or "current"),
            )
    except LedgerQueryError as exc:
        return _query_error(exc)
    except (ContentLedgerError, FinanceLedgerError, RecruitingLedgerError, RelationshipLedgerError) as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to query ledger")
        return _json(
            {
                "ok": False,
                "error": {"code": "ledger_query_failed", "message": f"Ledger query failed: {exc}"},
            }
        )
    view = (
        str(result.get("projection_kind") or "events")
        if contract_id == FINANCE_LEDGER_CONTRACT_ID
        else str(kwargs.get("view") or "current")
    )
    visualization = _query_ledger_visualization(
        result,
        contract_id=contract_id,
        view=view,
        group_by=group_by,
        metrics=query_metrics,
        filters=filters,
        date_field=kwargs.get("date_field"),
        date_from=kwargs.get("date_from"),
        date_to=kwargs.get("date_to"),
        date_granularity=date_granularity,
        limit=common["limit"],
        cursor=kwargs.get("cursor"),
        presentation=presentation,
        coalesce_previous_visualization=(
            kwargs.get("coalesce_previous_visualization") is True
        ),
    )
    return _json(_query_ledger_model_envelope(result, visualization))


async def _visualize_workspace_ledgers(entity_id: str = "", **kwargs: Any) -> str:
    """Return aggregate Ledger data in a trusted chat-visualization envelope."""

    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if runtime_tool_call_context_is_external_customer(kwargs):
        return _json(
            {
                "ok": False,
                "error": {
                    "code": "ledger_visualization_not_available_on_external_surface",
                    "message": "Workspace Ledger visualizations are restricted to internal Workspace agents",
                },
            }
        )
    allowed_contract_ids = (
        {
            contract_id
            for contract_id, read_tool in _READ_TOOL_BY_CONTRACT.items()
            if read_tool in context.allowed_tool_names
        }
        if context.allowed_tool_names is not None
        else None
    )
    if (
        allowed_contract_ids is not None
        and not allowed_contract_ids
        and "visualize_workspace_ledgers" not in context.allowed_tool_names
    ):
        return _json(
            {
                "ok": False,
                "error": {
                    "code": "ledger_contract_not_bound",
                    "message": "No installed Ledger is readable on this agent runtime surface",
                },
            }
        )
    try:
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.workspace import Workspace

        async with async_session() as db:
            workspace = (
                await db.execute(
                    select(Workspace)
                    .where(
                        Workspace.id == context.workspace_id,
                        Workspace.entity_id == entity_id,
                        Workspace.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if workspace is None:
            return _json(
                {
                    "ok": False,
                    "error": {
                        "code": "workspace_not_found",
                        "message": "Workspace was not found",
                    },
                }
            )
        overview = await workspace_ledger_overview(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            settings=workspace.settings,
            allowed_contract_ids=allowed_contract_ids,
        )
    except WorkspaceLedgerOverviewUnavailable as exc:
        return _json(
            {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to visualize Workspace Ledgers")
        return _json(
            {
                "ok": False,
                "error": {
                    "code": "ledger_visualization_failed",
                    "message": f"Ledger visualization failed: {exc}",
                },
            }
        )
    return _json(
        {
            "ok": True,
            "visualization": {
                "kind": "workspace_ledger_overview",
                "data": overview,
            },
        }
    )


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (QUERY_LEDGER_SCHEMA, _query_ledger),
        (VISUALIZE_WORKSPACE_LEDGERS_SCHEMA, _visualize_workspace_ledgers),
    ]


__all__ = ["get_tools"]
