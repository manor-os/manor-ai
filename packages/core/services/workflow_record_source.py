"""Workspace Ledger records as a shared, declarative option source.

A ``record_source`` names an installed Workspace Ledger and selects its current
rows with the ``query_ledger`` filter language::

    {"ledger": "relationship_ledger",
     "filter": {"contact_points.kind": "instagram", "status": {"$ne": "paused"}},
     "value_field": "identity_key", "label_field": "display_name",
     "secondary_field": "stage"}

The ledger location always comes from the Workspace's installed ledger
configuration. A Workflow run input uses it through the ``record_select``
control; the stored value is each record's ``value_field`` (a stable key),
never its label::

    {"type": "array", "items": {"type": "string"},
     "x-ui": {"control": "record_select", "record_source": {...}}}

A plain ``{"type": "string"}`` schema picks a single record.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

RECORD_SELECT_CONTROL = "record_select"
MAX_RECORD_CHOICES = 500
# Chat extraction sees the most recently updated records only.
MAX_PREFILL_RECORD_CHOICES = 200
RECORD_READ_TIMEOUT_SECONDS = 10.0
_FIELD_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}")
_SOURCE_KEYS = frozenset({"ledger", "filter", "value_field", "label_field", "secondary_field"})
_UI_KEYS = frozenset({"control", "record_source", "hidden"})


class RecordSourceUnavailable(RuntimeError):
    """The installed Ledger could not be read within the time budget."""


@dataclass(frozen=True)
class RecordSource:
    contract_id: str
    filter: Mapping[str, Any] = field(default_factory=dict)
    value_field: str = "identity_key"
    label_field: str = "display_name"
    secondary_field: str | None = None


def _field_path(value: Any, *, name: str, default: str | None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not _FIELD_PATH.fullmatch(value.strip()):
        raise ValueError(f"record_source.{name} must be a field path such as 'display_name' or 'payload.handle'")
    return value.strip()


def normalize_record_source(raw: Any) -> RecordSource:
    """Validate a ``record_source``; the filter uses ``query_ledger``'s own checks."""
    from packages.core.services.ledger_query import LedgerQueryError, query_ledger_rows
    from packages.core.services.ledger_query_service import queryable_ledger_contract_id

    if not isinstance(raw, Mapping):
        raise ValueError("record_source must be an object")
    unknown = sorted(set(raw) - _SOURCE_KEYS)
    if unknown:
        raise ValueError(f"record_source does not support {unknown[0]!r}")
    contract_id = queryable_ledger_contract_id(raw.get("ledger"))
    if contract_id is None:
        raise ValueError("record_source.ledger must name a queryable Workspace Ledger")
    filters = raw.get("filter") or {}
    if not isinstance(filters, Mapping):
        raise ValueError("record_source.filter must be an object")
    try:
        query_ledger_rows([], contract_id=contract_id, filters=filters)
    except LedgerQueryError as exc:
        raise ValueError(f"record_source.filter is invalid: {exc}") from None
    return RecordSource(
        contract_id=contract_id,
        filter=deepcopy(dict(filters)),
        value_field=_field_path(raw.get("value_field"), name="value_field", default="identity_key"),
        label_field=_field_path(raw.get("label_field"), name="label_field", default="display_name"),
        secondary_field=_field_path(raw.get("secondary_field"), name="secondary_field", default=None),
    )


def record_select_config_error(schema: Any, *, nested: bool = False) -> str | None:
    """Why a run-input schema's ``record_select`` control is unusable, if it is."""
    if not isinstance(schema, dict):
        return None
    ui = schema.get("x-ui")
    if isinstance(ui, dict) and ui.get("control") == RECORD_SELECT_CONTROL:
        if nested:
            return "record_select is only supported on a top-level run input"
        items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        multiple = schema.get("type") == "array" and items.get("type") == "string"
        if not multiple and schema.get("type") != "string":
            return "record_select needs a string schema, or an array schema whose items are strings"
        if "enum" in schema or "enum" in items:
            return "record_select reads its options from the Ledger; remove enum"
        unknown = sorted(set(ui) - _UI_KEYS)
        if unknown:
            return f"record_select does not support x-ui.{unknown[0]}"
        try:
            normalize_record_source(ui.get("record_source"))
        except ValueError as exc:
            return str(exc)
    properties = schema.get("properties")
    for child in (properties.values() if isinstance(properties, dict) else ()):
        error = record_select_config_error(child, nested=True)
        if error:
            return error
    return None


def record_select_source(schema: Any) -> RecordSource | None:
    """The source of a valid top-level ``record_select`` schema, else ``None``."""
    if not isinstance(schema, dict):
        return None
    ui = schema.get("x-ui")
    if not isinstance(ui, dict) or ui.get("control") != RECORD_SELECT_CONTROL:
        return None
    if record_select_config_error(schema):
        return None
    return normalize_record_source(ui["record_source"])


def _scalar_at(row: Mapping[str, Any], path: str | None) -> str:
    current: Any = row
    for part in str(path or "").split("."):
        if not isinstance(current, Mapping) or part not in current:
            return ""
        current = current[part]
    if isinstance(current, bool) or not isinstance(current, (str, int, float)):
        return ""
    return str(current).strip()


def record_choices(rows: Iterable[Mapping[str, Any]], source: RecordSource) -> list[dict[str, str]]:
    """Distinct ``{value, label, description?}`` options in query order."""
    options: dict[str, dict[str, str]] = {}
    for row in rows:
        value = _scalar_at(row, source.value_field)
        if not value or value in options:
            continue
        option = {"value": value, "label": _scalar_at(row, source.label_field) or value}
        description = _scalar_at(row, source.secondary_field)
        if description:
            option["description"] = description
        options[value] = option
        if len(options) >= MAX_RECORD_CHOICES:
            break
    return list(options.values())


async def load_record_choices(
    db: Any,
    source: RecordSource,
    *,
    entity_id: str,
    workspace_id: str | None,
    timeout: float | None = None,
) -> list[dict[str, str]]:
    """Options from the Workspace's installed Ledger, most recently updated first.

    A Ledger that is not installed offers nothing. One that cannot be read in
    ``timeout`` seconds raises ``RecordSourceUnavailable``.
    """
    from sqlalchemy import select

    from packages.core.models.workspace import Workspace
    from packages.core.services.ledger_query import MAX_QUERY_LIMIT
    from packages.core.services.ledger_query_service import (
        query_installed_workspace_ledger,
        workspace_queryable_ledger_configs,
    )

    if not workspace_id:
        return []
    settings = (await db.execute(
        select(Workspace.settings).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    config = workspace_queryable_ledger_configs(settings).get(source.contract_id)
    if config is None:
        return []
    timeout = RECORD_READ_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        result = await asyncio.wait_for(
            query_installed_workspace_ledger(
                config,
                entity_id=entity_id,
                workspace_id=workspace_id,
                filters=source.filter,
                limit=min(MAX_RECORD_CHOICES, MAX_QUERY_LIMIT),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RecordSourceUnavailable(
            f"The Workspace Ledger took longer than {timeout:g}s to read; try again shortly"
        ) from None
    except Exception as exc:
        code = getattr(exc, "code", "")
        if code == "ledger_not_installed":  # no records written yet
            return []
        logger.warning("Record source %s is unavailable", source.contract_id, exc_info=True)
        raise RecordSourceUnavailable("The Workspace Ledger could not be read") from exc
    return record_choices(result.get("rows") or [], source)


def _structured_chat_prefill(item: Mapping[str, Any]) -> bool:
    prefill = item.get("prefill") if isinstance(item.get("prefill"), dict) else {}
    return prefill.get("source") == "chat_message" and prefill.get("mode") == "structured"


async def resolve_record_select_inputs(
    db: Any,
    run_inputs: Iterable[dict[str, Any]],
    *,
    entity_id: str,
    workspace_id: str | None,
) -> tuple[dict[str, Any], ...]:
    """Run inputs whose record choices are pinned as ``enum`` for Chat extraction.

    Only the most recently updated ``MAX_PREFILL_RECORD_CHOICES`` records are
    offered; a name outside them is reported back so the person picks it. The
    stored Workflow keeps the live control.
    """
    resolved: list[dict[str, Any]] = []
    for item in run_inputs:
        source = record_select_source(item.get("schema")) if _structured_chat_prefill(item) else None
        if source is None:
            resolved.append(item)
            continue
        try:
            options = await load_record_choices(db, source, entity_id=entity_id, workspace_id=workspace_id)
        except RecordSourceUnavailable:
            options = []
        options = options[:MAX_PREFILL_RECORD_CHOICES]
        pinned = deepcopy(item)
        schema = pinned["schema"]
        target = schema["items"] if schema.get("type") == "array" else schema
        if options:
            target["enum"] = [option["value"] for option in options]
            target["x-enum-labels"] = [option["label"] for option in options]
        else:
            # Nothing to pick: drop the input from extraction instead of
            # letting the extractor invent keys.
            pinned.pop("prefill", None)
        resolved.append(pinned)
    return tuple(resolved)


def _offered_summary(options: list[dict[str, str]], limit: int = 10) -> str:
    if not options:
        return "this source has no matching records"
    shown = ", ".join(
        option["value"] if option["label"] == option["value"] else f"{option['value']} ({option['label']})"
        for option in options[:limit]
    )
    more = len(options) - limit
    return f"choose from {shown}" + (f" and {more} more" if more > 0 else "")


async def record_selection_errors(
    db: Any,
    run_inputs: Iterable[dict[str, Any]],
    values: Mapping[str, Any],
    *,
    entity_id: str,
    workspace_id: str | None,
) -> dict[str, str]:
    """``{input key: message}`` for selected keys the source does not offer.

    An empty selection is always accepted: an optional input left blank lets
    the Workflow treat it as "all matching records". Workspace records picked
    with ``workflow_project_select`` are checked the same way.
    """
    from packages.core.services.workflow_project_select import project_selection_errors

    run_inputs = list(run_inputs)
    errors: dict[str, str] = await project_selection_errors(
        db, run_inputs, values, entity_id=entity_id, workspace_id=workspace_id,
    )
    for item in run_inputs:
        key = str(item.get("key") or "")
        if item.get("hidden") or key not in values:
            continue
        source = record_select_source(item.get("schema"))
        raw = values[key]
        selected = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        selected = [str(value).strip() for value in selected if str(value or "").strip()]
        if source is None or not selected:
            continue
        try:
            options = await load_record_choices(db, source, entity_id=entity_id, workspace_id=workspace_id)
        except RecordSourceUnavailable as exc:
            errors[key] = f"Records could not be checked: {exc}"
            continue
        offered = {option["value"] for option in options}
        missing = [value for value in selected if value not in offered]
        if missing:
            errors[key] = (
                "Not an available record: " + ", ".join(missing[:5])
                + "; " + _offered_summary(options) + "."
            )
    return errors
