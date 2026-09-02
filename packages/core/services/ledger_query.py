"""Generic, read-only query primitives for Workspace ledgers.

Ledger contracts own their record schemas.  This module only provides the
common query envelope used by those contracts: exact filters, deterministic
pagination, aggregate metrics, grouping, and a stable revision token.  The
immutable ledger records remain the source of truth; this module never writes
or mutates them.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping

from packages.core.services.timezone_utils import (
    load_user_timezone,
    user_day_bounds_utc,
    user_timezone_name,
)


class LedgerQueryError(ValueError):
    """Stable error returned for malformed or stale ledger queries."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 500
MAX_QUERY_GROUPS = 24
_MISSING = object()
_MONETARY_AGGREGATE_METRICS = frozenset(
    {
        "sum_amount_minor",
        "sum_tax_minor",
        "sum_fee_minor",
        "sum_inflow_minor",
        "sum_outflow_minor",
    }
)
_COUNT_QUERY_METRICS = frozenset({"count"})
_FINANCE_QUERY_METRICS = _COUNT_QUERY_METRICS | _MONETARY_AGGREGATE_METRICS
_QUERY_METRICS_BY_CONTRACT = {
    "manor.content_ledger/v1": _COUNT_QUERY_METRICS,
    "manor.finance_ledger/v1": _FINANCE_QUERY_METRICS,
    "manor.recruiting_ledger/v1": _COUNT_QUERY_METRICS,
    "manor.relationship_ledger/v1": _COUNT_QUERY_METRICS,
}
_DATE_GRANULARITIES = frozenset({"day", "week", "month"})
_DATE_GROUP_FIELDS = frozenset({"recorded_at", "occurred_at"})


def supported_ledger_query_metrics(contract_id: str) -> frozenset[str]:
    """Return aggregate metrics defined by one Ledger contract."""

    return _QUERY_METRICS_BY_CONTRACT.get(contract_id, _COUNT_QUERY_METRICS)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def ledger_revision(rows: Iterable[Mapping[str, Any]], *, contract_id: str) -> str:
    """Return a stable revision for an immutable record snapshot."""

    canonical = json.dumps(
        list(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    digest = hashlib.sha256()
    digest.update(contract_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()[:32]


def _query_scope(
    *,
    contract_id: str,
    rows_revision: str,
    filters: Mapping[str, Any],
    group_by: tuple[str, ...],
    metrics: tuple[str, ...],
    date_granularity: str | None,
    currency_field: str | None,
    timezone_name: str,
) -> str:
    canonical = json.dumps(
        {
            "contract_id": contract_id,
            "rows_revision": rows_revision,
            "filters": filters,
            "group_by": group_by,
            "metrics": metrics,
            "date_granularity": date_granularity,
            "currency_field": currency_field,
            "timezone_name": timezone_name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _decode_cursor(cursor: str | None) -> tuple[int, str, str] | None:
    if not cursor:
        return None
    try:
        padded = str(cursor).encode("ascii") + b"=" * (-len(str(cursor)) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        offset = int(payload["offset"])
        revision = str(payload["revision"])
        scope = str(payload["scope"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError) as exc:
        raise LedgerQueryError("invalid_cursor", "Ledger query cursor is invalid") from exc
    if offset < 0 or not revision or not scope:
        raise LedgerQueryError("invalid_cursor", "Ledger query cursor is invalid")
    return offset, revision, scope


def _encode_cursor(offset: int, revision: str, scope: str) -> str:
    payload = json.dumps(
        {"offset": offset, "revision": revision, "scope": scope},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _value_at(row: Mapping[str, Any], field: str) -> Any:
    """Read a top-level field or a dotted payload field."""

    current: Any = row
    for part in str(field or "").split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _filter_value_at(row: Mapping[str, Any], field: str) -> Any:
    """Read a dotted filter path, traversing arrays of nested objects."""

    parts = str(field or "").split(".")

    def collect(current: Any, remaining: list[str]) -> list[Any]:
        if not remaining:
            return [current]
        if isinstance(current, Mapping):
            part = remaining[0]
            if part not in current:
                return []
            return collect(current[part], remaining[1:])
        if isinstance(current, (list, tuple, set, frozenset)):
            return [value for item in current for value in collect(item, remaining)]
        return []

    values = collect(row, parts)
    if not values:
        return _MISSING
    return values[0] if len(values) == 1 else values


def _as_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError) as exc:
        raise LedgerQueryError("invalid_query", f"{field} must be an ISO-8601 timestamp") from exc


def _is_date_only(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _date_only_value(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value).strip())


def _date_only_bounds(
    value: Any,
    *,
    field: str,
    timezone_name: str | None,
) -> tuple[datetime, datetime]:
    try:
        return user_day_bounds_utc(timezone_name, _date_only_value(value))
    except (OverflowError, ValueError) as exc:
        raise LedgerQueryError(
            "invalid_query",
            f"{field} is outside the supported date range",
        ) from exc


def _date_only_start(
    value: Any,
    *,
    field: str,
    timezone_name: str | None,
) -> datetime:
    try:
        local_start = datetime.combine(
            _date_only_value(value),
            time.min,
            tzinfo=load_user_timezone(timezone_name),
        )
        return local_start.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise LedgerQueryError(
            "invalid_query",
            f"{field} is outside the supported date range",
        ) from exc


def _matches_scalar(actual: Any, expected: Any) -> bool:
    if actual is _MISSING:
        return False
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.casefold() == expected.casefold()
    return actual == expected


def _matches_equality(actual: Any, expected: Any) -> bool:
    """Compare structured filter values while preserving scalar semantics."""

    if actual is _MISSING:
        return False
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return actual.keys() == expected.keys() and all(_matches_equality(actual[key], expected[key]) for key in actual)
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            _matches_equality(left, right) for left, right in zip(actual, expected, strict=True)
        )
    if isinstance(actual, (set, frozenset)) and isinstance(expected, (set, frozenset)):
        unmatched = list(expected)
        for actual_item in actual:
            for index, expected_item in enumerate(unmatched):
                if _matches_equality(actual_item, expected_item):
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    return _matches_scalar(actual, expected)


_FILTER_OPERATORS = frozenset(
    {
        "$contains",
        "$contains_all",
        "$contains_any",
        "$eq",
        "$exists",
        "$gt",
        "$gte",
        "$in",
        "$lt",
        "$lte",
        "$ne",
        "$not_in",
    }
)


def _collection_contains(actual: Any, expected: Any) -> bool:
    if actual is _MISSING or actual is None:
        return False
    if isinstance(actual, Mapping):
        if isinstance(expected, Mapping):
            return all(key in actual and _partial_match(actual[key], value) for key, value in expected.items())
        return any(_matches_scalar(key, expected) for key in actual)
    if isinstance(actual, (list, tuple, set, frozenset)):
        return any(_partial_match(item, expected) for item in actual)
    if isinstance(actual, str) and isinstance(expected, str):
        return expected.casefold() in actual.casefold()
    return False


def _partial_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(key in actual and _partial_match(actual[key], value) for key, value in expected.items())
    return _matches_scalar(actual, expected)


def _ordered_pair(actual: Any, expected: Any) -> tuple[Any, Any] | None:
    if actual is _MISSING or actual is None:
        return None
    if expected is None:
        raise LedgerQueryError(
            "invalid_query",
            "range filter operands must be numbers or ISO-8601 dates",
        )
    if (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        return actual, expected
    if isinstance(actual, (datetime, date)) or isinstance(expected, (datetime, date)):
        try:
            return _as_datetime(actual, field="filter"), _as_datetime(expected, field="filter")
        except LedgerQueryError as exc:
            raise LedgerQueryError(
                "invalid_query",
                "range filters require comparable numbers or ISO-8601 dates",
            ) from exc
    if isinstance(actual, str) and isinstance(expected, str):
        try:
            return _as_datetime(actual, field="filter"), _as_datetime(expected, field="filter")
        except LedgerQueryError as exc:
            raise LedgerQueryError(
                "invalid_query",
                "range filters require comparable numbers or ISO-8601 dates",
            ) from exc
    raise LedgerQueryError(
        "invalid_query",
        "range filters require comparable numbers or ISO-8601 dates",
    )


def _filter_operator_keys(expected: Mapping[str, Any]) -> list[str]:
    operator_keys = [str(key) for key in expected if str(key).startswith("$")]
    if not operator_keys:
        return []
    if len(operator_keys) != len(expected):
        raise LedgerQueryError(
            "invalid_query",
            "filter objects cannot mix operator and ordinary keys",
        )
    unsupported = sorted(set(operator_keys) - _FILTER_OPERATORS)
    if unsupported:
        raise LedgerQueryError(
            "invalid_query",
            f"Unsupported filter operator {unsupported[0]!r}",
        )
    return operator_keys


def _validate_filter_value(expected: Any) -> None:
    if not isinstance(expected, Mapping):
        return
    for operator in _filter_operator_keys(expected):
        operand = expected[operator]
        if operator == "$exists" and not isinstance(operand, bool):
            raise LedgerQueryError("invalid_query", "$exists filter operand must be boolean")
        if operator in {"$in", "$not_in", "$contains_any", "$contains_all"} and not isinstance(operand, list):
            raise LedgerQueryError("invalid_query", f"{operator} filter operand must be an array")
        if operator in {"$gt", "$gte", "$lt", "$lte"}:
            valid_number = isinstance(operand, (int, float)) and not isinstance(operand, bool)
            if valid_number or isinstance(operand, (datetime, date)):
                continue
            if isinstance(operand, str):
                try:
                    _as_datetime(operand, field="filter")
                except LedgerQueryError as exc:
                    raise LedgerQueryError(
                        "invalid_query",
                        f"{operator} filter operand must be a number or ISO-8601 date",
                    ) from exc
                continue
            else:
                raise LedgerQueryError(
                    "invalid_query",
                    f"{operator} filter operand must be a number or ISO-8601 date",
                )


def _matches_operator(actual: Any, operator: str, operand: Any) -> bool:
    if operator == "$exists":
        return (actual is not _MISSING) is operand
    if operator in {"$eq", "$ne"}:
        if isinstance(actual, (list, tuple, set, frozenset)) and not isinstance(operand, (list, tuple, set, frozenset)):
            matched = any(_matches_equality(item, operand) for item in actual)
        else:
            matched = _matches_equality(actual, operand)
        return matched if operator == "$eq" else not matched
    if operator in {"$in", "$not_in"}:
        if isinstance(actual, (list, tuple, set, frozenset)):
            matched = any(_matches_equality(item, candidate) for item in actual for candidate in operand)
        else:
            matched = any(_matches_equality(actual, candidate) for candidate in operand)
        return matched if operator == "$in" else not matched
    if operator == "$contains":
        return _collection_contains(actual, operand)
    if operator in {"$contains_any", "$contains_all"}:
        matches = [_collection_contains(actual, candidate) for candidate in operand]
        return any(matches) if operator == "$contains_any" else all(matches)
    pair = _ordered_pair(actual, operand)
    if pair is None:
        return False
    left, right = pair
    if operator == "$gt":
        return left > right
    if operator == "$gte":
        return left >= right
    if operator == "$lt":
        return left < right
    if operator == "$lte":
        return left <= right
    raise LedgerQueryError("invalid_query", f"Unsupported filter operator {operator!r}")


def _matches_filter_value(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, Mapping):
        if isinstance(actual, (list, tuple, set, frozenset)):
            candidates = expected if isinstance(expected, list) else [expected]
            return any(_matches_scalar(item, candidate) for item in actual for candidate in candidates)
        if isinstance(expected, list):
            return any(_matches_scalar(actual, candidate) for candidate in expected)
        return _matches_scalar(actual, expected)
    operator_keys = _filter_operator_keys(expected)
    if not operator_keys:
        return _matches_equality(actual, expected)
    return all(_matches_operator(actual, operator, expected[operator]) for operator in operator_keys)


def _matches_filters(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    for field, expected in filters.items():
        if expected is None or expected == "":
            continue
        actual = _filter_value_at(row, str(field))
        if not _matches_filter_value(actual, expected):
            return False
    return True


def _recorded_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    recorded = str(row.get("recorded_at") or row.get("occurred_at") or "")
    identifier = str(
        row.get("entry_id") or row.get("event_id") or row.get("reservation_id") or row.get("identity_key") or ""
    )
    return recorded, identifier


def _numeric(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field, 0)
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _aggregate(rows: list[Mapping[str, Any]], metrics: tuple[str, ...]) -> dict[str, int]:
    requested = tuple(dict.fromkeys(metrics or ("count",)))
    result: dict[str, int] = {}
    if "count" in requested:
        result["count"] = len(rows)
    if "sum_amount_minor" in requested:
        result["sum_amount_minor"] = sum(_numeric(row, "amount_minor") for row in rows)
    if "sum_tax_minor" in requested:
        result["sum_tax_minor"] = sum(_numeric(row, "tax_minor") for row in rows)
    if "sum_fee_minor" in requested:
        result["sum_fee_minor"] = sum(_numeric(row, "fee_minor") for row in rows)
    if "sum_inflow_minor" in requested:
        result["sum_inflow_minor"] = sum(
            _numeric(row, "amount_minor") for row in rows if str(row.get("direction") or "").casefold() == "inflow"
        )
    if "sum_outflow_minor" in requested:
        result["sum_outflow_minor"] = sum(
            _numeric(row, "amount_minor") for row in rows if str(row.get("direction") or "").casefold() == "outflow"
        )
    return result


def _validated_metrics(
    contract_id: str,
    metrics: Iterable[str],
) -> tuple[str, ...]:
    clean = tuple(dict.fromkeys(str(metric).strip() for metric in metrics if str(metric).strip()))
    allowed = supported_ledger_query_metrics(contract_id)
    unsupported = [metric for metric in clean if metric not in allowed]
    if unsupported:
        raise LedgerQueryError(
            "invalid_query",
            f"metric {unsupported[0]!r} is not allowed for {contract_id}",
        )
    return clean


def _validated_date_granularity(
    value: str | None,
    *,
    group_by: tuple[str, ...],
) -> str | None:
    if value is None or not str(value).strip():
        return None
    granularity = str(value).strip().casefold()
    if granularity not in _DATE_GRANULARITIES:
        raise LedgerQueryError(
            "invalid_query",
            "date_granularity must be day, week, or month",
        )
    if not _DATE_GROUP_FIELDS.intersection(group_by):
        raise LedgerQueryError(
            "invalid_query",
            "date_granularity requires grouping by recorded_at or occurred_at",
        )
    return granularity


def _date_bucket(
    value: Any,
    *,
    field: str,
    granularity: str,
    timezone_name: str | None,
) -> str:
    local = _as_datetime(value, field=field).astimezone(load_user_timezone(timezone_name))
    local_day = local.date()
    if granularity == "week":
        local_day -= timedelta(days=local_day.weekday())
    elif granularity == "month":
        local_day = local_day.replace(day=1)
    return local_day.isoformat()


def _validate_currency_aggregate(
    rows: list[Mapping[str, Any]],
    *,
    metrics: tuple[str, ...],
    currency_field: str | None,
) -> None:
    if not currency_field or not _MONETARY_AGGREGATE_METRICS.intersection(metrics):
        return
    currencies = {
        str(value).strip().upper()
        for row in rows
        if (value := _value_at(row, currency_field)) is not _MISSING and str(value).strip()
    }
    if len(currencies) > 1:
        raise LedgerQueryError(
            "mixed_currency_aggregate",
            "Monetary totals require a single currency; filter and query one currency at a time",
        )


def _grouped(
    rows: list[Mapping[str, Any]],
    *,
    group_by: tuple[str, ...],
    metrics: tuple[str, ...],
    date_granularity: str | None,
    timezone_name: str | None,
) -> list[dict[str, Any]]:
    if not group_by:
        return []
    groups: dict[str, tuple[dict[str, Any], list[Mapping[str, Any]]]] = {}
    for row in rows:
        key: dict[str, Any] = {}
        for field in group_by:
            value = _value_at(row, field)
            if value is _MISSING:
                key[field] = None
            elif date_granularity and field in _DATE_GROUP_FIELDS:
                key[field] = _date_bucket(
                    value,
                    field=field,
                    granularity=date_granularity,
                    timezone_name=timezone_name,
                )
            else:
                key[field] = value
        encoded = json.dumps(key, ensure_ascii=False, sort_keys=True, default=_json_default)
        if encoded not in groups:
            groups[encoded] = (key, [])
        groups[encoded][1].append(row)
    return [
        {"key": key, "aggregates": _aggregate(group_rows, metrics)}
        for key, group_rows in sorted(
            groups.values(), key=lambda item: json.dumps(item[0], sort_keys=True, default=_json_default)
        )
    ]


def _bounded_groups(
    groups: list[dict[str, Any]],
    *,
    group_by: tuple[str, ...],
    metrics: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep a useful deterministic window when a query has many groups."""

    if len(groups) <= MAX_QUERY_GROUPS:
        return groups
    date_field = next(
        (field for field in group_by if field in {"recorded_at", "occurred_at"}),
        None,
    )
    if date_field:

        def timeline_key(group: dict[str, Any]) -> tuple[float, str]:
            value = (group.get("key") or {}).get(date_field)
            try:
                timestamp = _as_datetime(value, field=date_field).timestamp()
            except LedgerQueryError:
                timestamp = float("-inf")
            return timestamp, json.dumps(
                group.get("key") or {},
                ensure_ascii=False,
                sort_keys=True,
                default=_json_default,
            )

        return sorted(groups, key=timeline_key)[-MAX_QUERY_GROUPS:]

    primary_metric = metrics[0] if metrics else "count"
    return sorted(
        groups,
        key=lambda group: (
            -int((group.get("aggregates") or {}).get(primary_metric) or 0),
            json.dumps(
                group.get("key") or {},
                ensure_ascii=False,
                sort_keys=True,
                default=_json_default,
            ),
        ),
    )[:MAX_QUERY_GROUPS]


def query_ledger_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    contract_id: str,
    revision_rows: Iterable[Mapping[str, Any]] | None = None,
    filters: Mapping[str, Any] | None = None,
    date_field: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
    group_by: Iterable[str] = (),
    metrics: Iterable[str] = (),
    date_granularity: str | None = None,
    currency_field: str | None = None,
    timezone_name: str | None = None,
    limit: int = DEFAULT_QUERY_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Filter, aggregate, and paginate one validated ledger snapshot."""

    if filters is not None and not isinstance(filters, Mapping):
        raise LedgerQueryError("invalid_query", "filters must be an object")
    try:
        clean_limit = int(DEFAULT_QUERY_LIMIT if limit is None else limit)
    except (TypeError, ValueError) as exc:
        raise LedgerQueryError("invalid_query", "limit must be an integer") from exc
    if clean_limit < 1 or clean_limit > MAX_QUERY_LIMIT:
        raise LedgerQueryError("invalid_query", f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    clean_timezone_name = user_timezone_name(timezone_name)

    all_rows = [dict(row) for row in rows]
    revision = ledger_revision(
        [dict(row) for row in (revision_rows if revision_rows is not None else all_rows)],
        contract_id=contract_id,
    )
    clean_filters = {str(field): expected for field, expected in dict(filters or {}).items()}
    for expected in clean_filters.values():
        if expected is None or expected == "":
            continue
        _validate_filter_value(expected)
    if date_from is not None or date_to is not None:
        field = str(date_field or "recorded_at")
        if date_from is not None:
            lower = (
                _date_only_start(
                    date_from,
                    field="date_from",
                    timezone_name=clean_timezone_name,
                )
                if _is_date_only(date_from)
                else _as_datetime(date_from, field="date_from")
            )
            clean_filters[f"__date_from:{field}"] = lower
        if date_to is not None:
            if _is_date_only(date_to):
                upper_day = _date_only_value(date_to)
                if upper_day < date.max:
                    upper = _date_only_bounds(
                        upper_day,
                        field="date_to",
                        timezone_name=clean_timezone_name,
                    )[1]
                    clean_filters[f"__date_to_exclusive:{field}"] = upper
            else:
                upper = _as_datetime(date_to, field="date_to")
                clean_filters[f"__date_to:{field}"] = upper

    clean_group_by = tuple(dict.fromkeys(str(field).strip() for field in group_by if str(field).strip()))
    clean_metrics = _validated_metrics(contract_id, metrics)
    clean_date_granularity = _validated_date_granularity(
        date_granularity,
        group_by=clean_group_by,
    )
    scope = _query_scope(
        contract_id=contract_id,
        rows_revision=ledger_revision(all_rows, contract_id=contract_id),
        filters=clean_filters,
        group_by=clean_group_by,
        metrics=clean_metrics,
        date_granularity=clean_date_granularity,
        currency_field=currency_field,
        timezone_name=clean_timezone_name,
    )
    decoded = _decode_cursor(cursor)
    offset = 0
    if decoded:
        offset, cursor_revision, cursor_scope = decoded
        if cursor_revision != revision:
            raise LedgerQueryError(
                "stale_cursor",
                "Ledger changed; restart the query without the old cursor",
            )
        if cursor_scope != scope:
            raise LedgerQueryError(
                "stale_cursor",
                "Query changed; restart the query without the old cursor",
            )

    def matches(row: Mapping[str, Any]) -> bool:
        ordinary = {
            field: expected for field, expected in clean_filters.items() if not str(field).startswith("__date_")
        }
        if not _matches_filters(row, ordinary):
            return False
        for field, expected in clean_filters.items():
            if not str(field).startswith("__date_"):
                continue
            target_field = str(field).split(":", 1)[1]
            actual = _value_at(row, target_field)
            if actual is _MISSING:
                return False
            actual_dt = _as_datetime(actual, field=target_field)
            if str(field).startswith("__date_from:") and actual_dt < expected:
                return False
            if str(field).startswith("__date_to_exclusive:") and actual_dt >= expected:
                return False
            if str(field).startswith("__date_to:") and actual_dt > expected:
                return False
        return True

    matched = [row for row in all_rows if matches(row)]
    matched.sort(key=_recorded_sort_key, reverse=True)
    _validate_currency_aggregate(
        matched,
        metrics=clean_metrics,
        currency_field=currency_field,
    )
    page = matched[offset : offset + clean_limit]
    next_offset = offset + len(page)
    next_cursor = _encode_cursor(next_offset, revision, scope) if next_offset < len(matched) else None
    max_recorded = max((str(row.get("recorded_at") or row.get("occurred_at") or "") for row in matched), default=None)
    grouped = _grouped(
        matched,
        group_by=clean_group_by,
        metrics=clean_metrics,
        date_granularity=clean_date_granularity,
        timezone_name=clean_timezone_name,
    )
    visible_groups = _bounded_groups(
        grouped,
        group_by=clean_group_by,
        metrics=clean_metrics,
    )
    return {
        "contract_id": contract_id,
        "schema_version": 1,
        "ledger_revision": revision,
        "matched_count": len(matched),
        "rows": page,
        "aggregates": _aggregate(matched, clean_metrics),
        "groups": visible_groups,
        "group_count": len(grouped),
        "groups_truncated": len(grouped) > MAX_QUERY_GROUPS,
        "date_granularity": clean_date_granularity,
        "timezone_name": clean_timezone_name,
        "next_cursor": next_cursor,
        "as_of": max_recorded,
    }


__all__ = [
    "DEFAULT_QUERY_LIMIT",
    "LedgerQueryError",
    "MAX_QUERY_GROUPS",
    "MAX_QUERY_LIMIT",
    "ledger_revision",
    "query_ledger_rows",
    "supported_ledger_query_metrics",
]
