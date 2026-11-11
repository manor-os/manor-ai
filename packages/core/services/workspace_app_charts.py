"""What a Workspace App ``chart`` block draws, worked out from the records it reads.

Pure functions: the data layer reads the rows (``{label, stage, values}`` with the
source's fields by dotted path, or a Stat's readings) and this turns them into
categories and series a renderer draws to scale. Nothing here knows a business:
a menu's best sellers, a pipeline's funnel and a channel's posting hours are the
same counts and sums over declared fields.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

# How many records a chart reads before it says it shows only part of them.
CHART_ROW_LIMIT = 2000
_SCATTER_POINT_LIMIT = 500
_SERIES_LIMIT = 5
_TIME_BUCKET_LIMIT = 60
_DEFAULT_LIMITS = {"leaderboard": 10, "donut": 6}
_CATEGORY_LIMIT = 24
OTHER = "__other__"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_WEEKDAYS = {"en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
             "zh": ("周一", "周二", "周三", "周四", "周五", "周六", "周日")}
_WORDS = {"en": {"other": "Other", "none": "None", "count": "Records"},
          "zh": {"other": "其他", "none": "未填写", "count": "记录数"}}


def _language(locale: str | None) -> str:
    return "zh" if (locale or "").lower().startswith("zh") else "en"


def chart_number(value: Any) -> float | None:
    """A number a record holds, also one kept as text ("1,200"); anything else is no number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def chart_time(value: Any) -> datetime | None:
    """A stored date or time as it was written: its own wall clock, so 19:00 in the shop's zone stays 19:00."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if _ISO_DATE.fullmatch(text):
            return datetime.combine(date.fromisoformat(text), datetime.min.time())
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def time_bucket(at: datetime, bucket: str) -> str:
    """The key of the bucket ``at`` falls in: a day, the Monday of its week, a month, a weekday (1–7) or an hour."""
    if bucket == "day":
        return at.date().isoformat()
    if bucket == "week":
        return (at.date() - timedelta(days=at.weekday())).isoformat()
    if bucket == "month":
        return f"{at.year:04d}-{at.month:02d}"
    if bucket == "weekday":
        return str(at.isoweekday())
    return str(at.hour)


def _next_bucket(key: str, bucket: str) -> str:
    if bucket == "month":
        year, month = map(int, key.split("-"))
        return f"{year + month // 12:04d}-{month % 12 + 1:02d}"
    return (date.fromisoformat(key) + timedelta(days=7 if bucket == "week" else 1)).isoformat()


def _filled(keys: Iterable[str], bucket: str) -> list[str]:
    """Every bucket between the first and the last, so a quiet month shows as a gap rather than vanishing."""
    present = sorted(set(keys))
    if bucket == "weekday":
        return [str(day) for day in range(1, 8)]
    if bucket == "hour":
        return [str(hour) for hour in range(24)]
    if not present:
        return []
    filled = [present[0]]
    while filled[-1] < present[-1] and len(filled) < 400:
        filled.append(_next_bucket(filled[-1], bucket))
    return filled[-_TIME_BUCKET_LIMIT:]


def _bucket_label(key: str, bucket: str | None, language: str) -> str:
    if bucket == "weekday":
        return _WEEKDAYS[language][int(key) - 1]
    if bucket == "hour":
        return f"{int(key):02d}:00"
    return key


def _keys(row: dict[str, Any], dimension: dict[str, Any] | None) -> list[str]:
    """The categories one record counts in: its stage, its bucket, its value, or each word of a list of them."""
    if dimension is None:
        return [""]
    if dimension.get("by") == "stage":
        return [str(row.get("stage") or "")]
    value = row["values"].get(".".join(dimension["field"]))
    bucket = dimension.get("bucket")
    if bucket:
        at = chart_time(value)
        return [time_bucket(at, bucket)] if at else []
    parts = value if isinstance(value, list) else [value]
    keys = []
    for part in parts:
        if isinstance(part, bool):
            keys.append("true" if part else "false")
        elif isinstance(part, (str, int, float)) and str(part).strip():
            keys.append(str(part).strip()[:80])
    return list(dict.fromkeys(keys)) or ([""] if not isinstance(value, list) else [])


def _aggregate(values: list[float], count: int, agg: str) -> float | None:
    if agg == "count":
        return float(count)
    if not values:
        return None
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    return values[-1]  # last: the latest reading in the bucket


class _Labeller:
    def __init__(self, chart: dict[str, Any], stage_labels: dict[str, str], language: str) -> None:
        self.labels = chart.get("labels") or {}
        self.stage_labels = stage_labels
        self.language = language

    def __call__(self, key: str, dimension: dict[str, Any] | None) -> tuple[str, bool]:
        """A category's name and whether it was named for people (a label, a stage label, a date)."""
        if key == OTHER:
            return _WORDS[self.language]["other"], True
        if key == "":
            return _WORDS[self.language]["none"], True
        if key in self.labels:
            return self.labels[key], True
        if dimension and dimension.get("by") == "stage" and key in self.stage_labels:
            return self.stage_labels[key], True
        bucket = (dimension or {}).get("bucket")
        if bucket:
            return _bucket_label(key, bucket, self.language), True
        return key, False


def _ordered(keys: list[str], totals: dict[str, float], chart: dict[str, Any], dimension: dict[str, Any] | None,
             stages: list[str]) -> list[str]:
    """Categories in the order a person reads them: time in time, steps in their order, the rest largest first."""
    bucket = (dimension or {}).get("bucket")
    if bucket:
        return _filled(keys, bucket)
    order = chart.get("order") or (stages if dimension and dimension.get("by") == "stage" else [])
    known = [key for key in order if key in keys or chart["kind"] == "funnel"]
    rest = sorted((key for key in keys if key not in known), key=lambda key: (-(totals.get(key) or 0), key))
    # A funnel's steps are its order; a record at a stage outside it (lost) is no step.
    return known + ([] if chart["kind"] == "funnel" else rest)


def build_chart(chart: dict[str, Any], rows: list[dict[str, Any]], *, stage_labels: dict[str, str] | None = None,
                stages: list[str] | None = None, truncated: bool = False, locale: str | None = None) -> dict[str, Any]:
    """The categories and series (or points) a chart block draws from its records.

    ``rows`` are ``{"id", "label", "stage", "values": {"a.b": value}}``. Every
    value comes back a plain number on the chart's own scale (a percent as
    stored, the renderer reads its ``scale``).
    """
    language = _language(locale)
    label = _Labeller(chart, stage_labels or {}, language)
    kind = chart["kind"]
    result: dict[str, Any] = {"kind": kind, "format": chart["format"],
                              **{key: chart[key] for key in ("currency", "unit", "scale") if key in chart},
                              "records": len(rows), **({"truncated": True} if truncated else {})}
    x, series_dim = chart.get("x"), chart.get("series")
    if x and x.get("bucket"):
        result["x_bucket"] = x["bucket"]
    if series_dim and series_dim.get("bucket"):
        result["series_bucket"] = series_dim["bucket"]

    if kind == "scatter":
        points = []
        for row in rows:
            px, py = chart_number(row["values"].get(".".join(x["field"]))), chart_number(row["values"].get(".".join(chart["y"]["field"])))
            if px is None or py is None:
                continue
            group = _keys(row, series_dim)[0] if series_dim and _keys(row, series_dim) else None
            points.append({"x": px, "y": py, "label": row.get("label") or "",
                           **({"series": label(group, series_dim)[0]} if group is not None else {})})
        result["points"] = points[:_SCATTER_POINT_LIMIT]
        if len(points) > _SCATTER_POINT_LIMIT:
            result["truncated"] = True
        result["categories"], result["series"] = [], []
        return result

    value_spec = chart.get("value") or {"agg": "count"}
    if kind == "leaderboard" and not x:
        # Each record by its own number, best first.
        ranked = [(row, chart_number(row["values"].get(".".join(value_spec["field"])))) for row in rows]
        ranked = sorted(((row, number) for row, number in ranked if number is not None), key=lambda pair: -pair[1])
        limit = chart.get("limit") or _DEFAULT_LIMITS["leaderboard"]
        result["categories"] = [{"key": str(row.get("id")), "label": row.get("label") or "", "labelled": True}
                                for row, _ in ranked[:limit]]
        result["series"] = [{"key": "value", "label": "", "values": [number for _, number in ranked[:limit]]}]
        result["hidden_categories"] = max(0, len(ranked) - limit)
        return result

    agg = value_spec.get("agg", "count")
    field = ".".join(value_spec["field"]) if "field" in value_spec else None
    cells: dict[tuple[str, str], list[float]] = {}
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        number = chart_number(row["values"].get(field)) if field else None
        if field and agg != "count" and number is None:
            continue
        for key in _keys(row, x):
            for group in (_keys(row, series_dim) if series_dim else [""]):
                cell = (key, group)
                counts[cell] = counts.get(cell, 0) + 1
                if number is not None:
                    cells.setdefault(cell, []).append(number)
    values = {cell: _aggregate(cells.get(cell, []), counts[cell], agg) for cell in counts}
    totals: dict[str, float] = {}
    for (key, _), number in values.items():
        totals[key] = totals.get(key, 0.0) + (number or 0.0)
    keys = _ordered(sorted({key for key, _ in values}), totals, chart, x, stages or [])
    if kind == "funnel":
        # A record at a later step passed every earlier one; one at a step outside the order is left out.
        steps = keys
        reached = {key: index for index, key in enumerate(steps)}
        cumulative = [0.0] * len(steps)
        for (key, _), number in values.items():
            if key in reached:
                for index in range(reached[key] + 1):
                    cumulative[index] += number or 0.0
        result["categories"] = [dict(zip(("key", "label", "labelled"), (key, *label(key, x)))) for key in steps]
        result["series"] = [{"key": "value", "label": "", "values": cumulative}]
        return result

    if not (x or {}).get("bucket"):
        limit = chart.get("limit") or _DEFAULT_LIMITS.get(kind, _CATEGORY_LIMIT)
        if len(keys) > limit:
            hidden = keys[limit - 1:] if kind == "donut" else keys[limit:]
            keys = keys[:limit - 1] + [OTHER] if kind == "donut" else keys[:limit]
            result["hidden_categories"] = len(hidden)
            if kind == "donut":
                for (key, group), number in list(values.items()):
                    if key in hidden:
                        values[(OTHER, group)] = (values.get((OTHER, group)) or 0.0) + (number or 0.0)
    groups_total: dict[str, float] = {}
    for (key, group), number in values.items():
        if key in keys:
            groups_total[group] = groups_total.get(group, 0.0) + abs(number or 0.0)
    if series_dim and series_dim.get("bucket"):
        groups = _filled(groups_total, series_dim["bucket"])
    else:
        groups = sorted(groups_total, key=lambda group: (-(groups_total[group]), group))
        if kind == "heatmap":
            groups = groups[:_CATEGORY_LIMIT]
        elif len(groups) > _SERIES_LIMIT:
            kept, merged = groups[:_SERIES_LIMIT - 1], groups[_SERIES_LIMIT - 1:]
            for (key, group), number in list(values.items()):
                if group in merged:
                    values[(key, OTHER)] = (values.get((key, OTHER)) or 0.0) + (number or 0.0)
            groups = [*kept, OTHER]
    # A missing count is none at all; a missing sum or average is no reading, drawn as a gap.
    empty = 0.0 if agg in {"count", "sum"} else None
    result["categories"] = [dict(zip(("key", "label", "labelled"), (key, *label(key, x)))) for key in keys]
    result["series"] = [{"key": group, "label": label(group, series_dim)[0] if series_dim else "",
                         "values": [values.get((key, group), empty) for key in keys]} for group in groups]
    if kind == "donut":
        result["total"] = sum(number or 0.0 for number in result["series"][0]["values"]) if result["series"] else 0.0
    return result


def build_stat_chart(chart: dict[str, Any], stats: list[dict[str, Any]], *, locale: str | None = None) -> dict[str, Any]:
    """Each Stat's readings over time, one series per Stat, bucketed by when each value was read."""
    bucket = chart["x"]["bucket"]
    agg = chart["value"]["agg"]
    by_stat: list[tuple[dict[str, Any], dict[str, list[float]]]] = []
    for stat in stats:
        buckets: dict[str, list[float]] = {}
        for reading in sorted(stat.get("observations") or [], key=lambda item: str(item.get("observed_at") or "")):
            at, number = chart_time(reading.get("observed_at")), chart_number(reading.get("value"))
            if at is not None and number is not None:
                buckets.setdefault(time_bucket(at, bucket), []).append(number)
        by_stat.append((stat, buckets))
    keys = _filled((key for _, buckets in by_stat for key in buckets), bucket)
    language = _language(locale)
    return {"kind": chart["kind"], "format": chart["format"],
            **{key: chart[key] for key in ("currency", "unit", "scale") if key in chart},
            **({"unit": next((stat.get("unit") for stat, _ in by_stat if stat.get("unit")), None)}
               if "unit" not in chart and chart["format"] == "number" and any(stat.get("unit") for stat, _ in by_stat) else {}),
            "x_bucket": bucket, "records": len(stats),
            "categories": [{"key": key, "label": _bucket_label(key, bucket, language), "labelled": True} for key in keys],
            "series": [{"key": str(stat.get("key") or stat.get("id")), "label": stat.get("name") or str(stat.get("key") or ""),
                        **({"description": stat["description"]} if stat.get("description") else {}),
                        "values": [_aggregate(buckets.get(key, []), len(buckets.get(key, [])), agg) if buckets.get(key) else None
                                   for key in keys]} for stat, buckets in by_stat]}


def chart_items(block_key: str, drawn: dict[str, Any]) -> list[dict[str, Any]]:
    """The chart as rows, for a client that lists it instead of drawing it (the phone, a self-check)."""
    unit = drawn.get("currency") or drawn.get("unit")
    if drawn.get("points"):
        return [{"id": f"{block_key}:{index}", "title": point["label"] or f"{point['x']}, {point['y']}",
                 "labelled": True, "value": f"{_plain(point['x'])}, {_plain(point['y'])}"}
                for index, point in enumerate(drawn["points"][:100])]
    items = []
    series = drawn.get("series") or []
    for index, category in enumerate(drawn.get("categories") or []):
        numbers = [entry["values"][index] for entry in series]
        known = [number for number in numbers if number is not None]
        items.append({"id": f"{block_key}:{category['key']}", "title": category["label"], "labelled": category["labelled"],
                      "value": _plain(sum(known)) if known else None, **({"unit": unit} if unit else {}),
                      **({"fields": [{"label": entry["label"], "value": _plain(number)} for entry, number in zip(series, numbers)
                                     if number is not None]} if len(series) > 1 else {})})
    return items


def _plain(number: float | None) -> int | float | None:
    if number is None:
        return None
    return int(number) if float(number).is_integer() else round(number, 4)
