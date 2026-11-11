"""Bounded, ACL-aware read projection for declarative Workspace App components.

The App configuration is a portable read contract.  This module deliberately
does not accept field paths or source definitions from HTTP callers: those are
validated at configuration/install time and resolved from the Workspace only.
"""
from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import DateTime, Numeric, Text, and_, case, cast, func, or_, select
from sqlalchemy.orm import aliased, load_only
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.contracts.project_display import display_file, project_display
from packages.core.models.document import Document
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding, WorkflowProject, WorkflowRun
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.services.document_access import DocumentAccessContext
# Signs only inside media_url_scope (browser sessions); every media item below
# is built from a Document this request's DocumentAccessContext admitted.
from packages.core.services.media_urls import with_thumbnail_url
from packages.core.services.workspace_app_media import (
    media_companions, media_details, media_facts, media_file_list, timeline_segments,
)
from packages.core.services.viewer_language import RUN_DISPLAY_I18N_KEY, localized, run_names_for
from packages.core.services.workflow_project_select import project_title, selected_project_ids
from packages.core.services.workspace_layout import PROJECT_TAKES_SEGMENT
from packages.core.stats.service import stat_freshness_status
from packages.core.services.workspace_app_charts import (
    CHART_ROW_LIMIT,
    build_chart,
    build_stat_chart,
    chart_items,
    chart_time,
)
from packages.core.services.workspace_app_view import (
    block_card_actions,
    DEFAULT_PUBLICATION_STAT_COLLECTOR_TYPES,
    app_data_project_sources,
    load_workspace_app_data_catalog,
    normalize_app_view_config,
    preview_platform,
    validate_app_view_data_contract,
)


# What an App read needs from a run: its status, step, error, lineage and start.
# The large JSON columns stay unloaded (raiseload), so a read never pulls a run's
# variables or snapshots. A reader that needs one of them loads it explicitly with
# ``_load_run_columns``; ``_loaded_run_value`` would otherwise see an empty default.
APP_RUN_COLUMNS = (
    "id", "workflow_id", "entity_id", "workspace_id", "binding_id", "trigger_source", "retry_of_run_id",
    "retry_from_step_id", "lineage_root_run_id", "lineage_is_legacy", "attempt_number", "status",
    "current_step_id", "error", "trigger_data", "started_at", "completed_at", "created_at", "updated_at",
)


def _app_run_options() -> tuple[Any, ...]:
    return (load_only(*(getattr(WorkflowRun, name) for name in APP_RUN_COLUMNS), raiseload=True),)


async def _load_run_columns(db: Any, runs: list[WorkflowRun], *columns: str) -> None:
    """Load named large columns of runs read with ``_app_run_options``, in one query.

    The values are set as committed, so the run is not marked changed and the
    request's commit never writes them back.
    """
    runs = [run for run in runs if run is not None]
    if not runs or not columns:
        return
    attributes = [getattr(WorkflowRun, name) for name in columns]
    rows = (await db.execute(select(WorkflowRun.id, *attributes).where(
        WorkflowRun.id.in_({run.id for run in runs}),
    ))).all()
    values = {row[0]: dict(zip(columns, row[1:], strict=True)) for row in rows}
    for run in runs:
        for name, value in values.get(run.id, {}).items():
            set_committed_value(run, name, value)


class AppDataError(ValueError):
    """A structured configuration or bounded-read error safe for App clients."""

    def __init__(self, code: str, message: str, *, source: str | None = None) -> None:
        self.code, self.message, self.source = code, message, source
        super().__init__(message)

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, **({"source": self.source} if self.source else {})}


class _SourceRows(list[dict[str, Any]]):
    """Source rows may carry a cursor after filtered parent rows."""

    next_cursor: str | None = None


_TECHNICAL_SLUG = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)+$")
# A ``record`` field names at most this many records.
_RECORD_FIELD_LIMIT = 50
# How much one field shows: pictures in an ``images`` strip, words in ``tags``, characters of ``code``.
_IMAGES_FIELD_LIMIT = 12
_TAGS_FIELD_LIMIT = 20
# The latest numbers a sparkline draws.
_SPARKLINE_FIELD_LIMIT = 60
_CODE_FIELD_LIMIT = 4000
_PROJECT_EXECUTION_ERROR_LIMIT = 1000
# Cursor values must use the same portable subset accepted by Python and
# PostgreSQL. PostgreSQL accepts 24:00/60 seconds and rounds excess precision,
# while Python has different behavior; neither can enter a keyset cursor.
_ISO_TIMESTAMP_WITH_OFFSET = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:0\d|1[0-5]):[0-5]\d)$"
)
# A plain date (an all-day plan) orders as the start of that day in UTC.
_ISO_DATE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _public_href(value: Any) -> str | None:
    """Return only a navigable public URL from workflow-owned result state."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _value_at(value: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _field_label(path: list[str]) -> str:
    return " ".join(part.replace("_", " ").replace("-", " ").title() for part in path)


def _has_opaque_part(key: str) -> bool:
    """Workflow idempotency keys often contain ULIDs and provider identifiers;
    they are useful to the runtime but are not an App-facing label."""
    return any(len(part) == 26 and part.isalnum() for part in key.split(":"))


def _project(
    row: WorkflowProject, fields: list[list[str]], label_fields: list[list[str]] | None = None,
    execution: dict[str, Any] | None = None, source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = {".".join(path): _value_at(row.state or {}, path) for path in fields}
    raw_key = row.project_key or ""
    has_opaque_part = _has_opaque_part(raw_key)
    display = project_display(row.state)
    # The title its Workflow gave it comes before any key; a page's own label_fields come first.
    fallback = display.get("title") or row.project_type.replace("_", " ").title()
    label = fallback if has_opaque_part or not raw_key or display.get("title") else raw_key
    if label_fields:
        parts = [_value_at(row.state or {}, path) for path in label_fields if path in fields]
        label = " · ".join(value.strip() for value in parts if isinstance(value, str) and value.strip())
        label = label or fallback
    return {"id": row.id, "_cursor": _project_cursor(row, source), "project_type": row.project_type, "schema_version": row.schema_version,
            "project_key": row.project_key, "label": label, "stage": row.current_stage, "updated_at": _iso(row.updated_at),
            "created_at": _iso(row.created_at), "fields": values, "execution": execution, "display": display,
            "parent_id": _parent_id(row, source)}


def _parent_id(row: WorkflowProject, source: dict[str, Any] | None) -> str | None:
    """A related project's parent: the project its state names along the source's parent path."""
    if not source or source.get("type") != "related_projects":
        return None
    parent = _value_at(row.state or {}, source["parent_state_path"])
    return parent if isinstance(parent, str) and parent else None


def _project_cursor(row: WorkflowProject, source: dict[str, Any] | None) -> str:
    order = (source or {}).get("order")
    if not order:
        return row.id
    value = _normalized_order_value(_value_at(row.state or {}, order["field"]))
    payload = json.dumps([value.isoformat() if value else None, row.id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _normalized_order_value(value: Any) -> datetime | None:
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        value = f"{value}T00:00:00+00:00"
    if not isinstance(value, str) or not _ISO_TIMESTAMP_WITH_OFFSET.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _project_order_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value, project_id = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppDataError("invalid_cursor", "The component cursor is invalid.") from exc
    if value is not None and not isinstance(value, str) or not isinstance(project_id, str) or not project_id:
        raise AppDataError("invalid_cursor", "The component cursor is invalid.")
    parsed = _normalized_order_value(value)
    if value is not None and parsed is None:
        raise AppDataError("invalid_cursor", "The component cursor is invalid.")
    return parsed, project_id


def _project_order_expression(row: Any, source: dict[str, Any]) -> Any:
    expression: Any = row.state
    for key in source["order"]["field"]:
        expression = expression[key]
    raw = expression.astext
    # Keep SQL admission aligned with the portable cursor parser: only ISO
    # timestamps carrying an explicit UTC offset are datetimes.
    day = raw.concat("T00:00:00+00:00")
    return case((and_(raw.op("~")(_ISO_TIMESTAMP_WITH_OFFSET.pattern), func.pg_input_is_valid(raw, "timestamp with time zone")), cast(raw, DateTime(timezone=True))),
                (and_(raw.op("~")(_ISO_DATE.pattern), func.pg_input_is_valid(day, "timestamp with time zone")), cast(day, DateTime(timezone=True))),
                else_=None)


def _apply_project_order(query: Any, row: Any, source: dict[str, Any], after_id: str | None) -> Any:
    order = source.get("order")
    if not order:
        if after_id:
            query = query.where(row.id > after_id)
        return query.order_by(row.id.asc())
    value = _project_order_expression(row, source)
    direction = order["direction"]
    if after_id:
        previous, previous_id = _project_order_cursor(after_id)
        if previous is None:
            query = query.where(and_(value.is_(None), row.id > previous_id))
        elif direction == "asc":
            query = query.where(or_(value.is_(None), value > previous, and_(value == previous, row.id > previous_id)))
        else:
            query = query.where(or_(value.is_(None), value < previous, and_(value == previous, row.id > previous_id)))
    ordered = value.asc().nulls_last() if direction == "asc" else value.desc().nulls_last()
    return query.order_by(ordered, row.id.asc())


async def _project_executions(
    db: Any, *, entity_id: str, workspace_id: str, rows: list[WorkflowProject], source: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    execution_run_id_path = (source or {}).get("execution_run_id_path")
    # An explicit business run reference is authoritative; falling back after
    # it is absent would let an unrelated status check conceal that history.
    run_by_project = {
        row.id: (_value_at(row.state or {}, execution_run_id_path) if execution_run_id_path else row.last_run_id)
        for row in rows
    }
    run_ids = {run_id for run_id in run_by_project.values() if isinstance(run_id, str)}
    if not run_ids:
        return {}
    runs = list((await db.execute(select(WorkflowRun).options(*_app_run_options()).where(
        WorkflowRun.id.in_(run_ids), WorkflowRun.entity_id == entity_id, WorkflowRun.workspace_id == workspace_id,
    ))).scalars())
    selected_by_id = {run.id: run for run in runs}
    # A native retry creates another canonical attempt in the same family.  A
    # state path stores the business execution root, while the App should show
    # the latest outcome in that root's lineage. Legacy runs have no trusted
    # family identity and therefore retain the explicitly referenced attempt.
    roots_by_id = {
        run.id: run.lineage_root_run_id or run.id
        for run in runs if execution_run_id_path and run.lineage_status == "canonical"
    }
    if roots_by_id:
        family_runs = list((await db.execute(select(WorkflowRun).options(*_app_run_options()).where(
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.workspace_id == workspace_id,
            or_(
                WorkflowRun.id.in_(set(roots_by_id.values())),
                WorkflowRun.lineage_root_run_id.in_(set(roots_by_id.values())),
            ),
        ))).scalars())
        for selected_id, root_id in roots_by_id.items():
            selected = selected_by_id[selected_id]
            candidates = [
                candidate for candidate in family_runs
                if candidate.lineage_status == "canonical"
                and candidate.workflow_id == selected.workflow_id
                and (candidate.lineage_root_run_id or candidate.id) == root_id
            ]
            if candidates:
                selected_by_id[selected_id] = max(
                    candidates,
                    key=lambda candidate: (candidate.effective_attempt_number, candidate.created_at, candidate.id),
                )
    # The step's name comes from the run's definition, which an App read loads only for runs still going or failed.
    await _load_run_columns(db, [run for run in selected_by_id.values() if run.status in _ACTIVE_RUN_STATUSES], "definition_snapshot")
    by_id = {
        run.id: {"run_id": run.id, "status": run.status, "current_step_id": run.current_step_id,
                 "current_step_label": _step_label(run) if run.status in _ACTIVE_RUN_STATUSES else None,
                 "workflow_name": _workflow_name(run) if run.status in _ACTIVE_RUN_STATUSES else None,
                 "error": run.error[:_PROJECT_EXECUTION_ERROR_LIMIT] if run.error else None,
                 "href": f"/flows/{run.workflow_id}/runs/{run.id}"}
        for run in selected_by_id.values()
    }
    # A run paused on a sub-flow that waits for a person is answered in that sub-flow, so the item names it.
    waiting, working, failed = await _waiting_child_runs(
        db, entity_id=entity_id, workspace_id=workspace_id,
        runs=[run for run in selected_by_id.values() if run.status in {"paused", "failed"}],
    )
    result = {}
    for project_id, run_id in run_by_project.items():
        if not (isinstance(run_id, str) and run_id in selected_by_id):
            continue
        execution = by_id[selected_by_id[run_id].id]
        if run_id in waiting:
            execution = _with_child_run(execution, waiting[run_id])
        elif run_id in working:
            execution = _with_child_run(execution, working[run_id])
        elif run_id in failed:
            execution = _with_child_run(execution, failed[run_id])
        result[project_id] = execution
    return result


_ACTIVE_RUN_STATUSES = frozenset({"running", "paused", "failed"})


def _workflow_name(run: Any) -> str | None:
    """The name of the workflow a run executes, as its definition names it; None when the definition has none."""
    return str((run.definition_snapshot or {}).get("name") or "").strip() or None


def _step_label(run: Any, *, with_workflow: bool = False) -> str | None:
    """The step a run is on, named as its workflow names it; with ``with_workflow`` the workflow leads, as in
    "Make a music video · Produce" for a sub-flow. None when the definition does not name the step."""
    definition = run.definition_snapshot or {}
    step = next((str(node["name"]) for node in definition.get("nodes") or []
                 if isinstance(node, dict) and run.current_step_id and str(node.get("id")) == run.current_step_id
                 and node.get("name")), None)
    if not step:
        return None
    workflow = _workflow_name(run)
    return f"{workflow} · {step}" if with_workflow and workflow else step


def _with_child_run(execution: dict[str, Any], child: Any) -> dict[str, Any]:
    """Where a run's own state is read from its sub-flow: a paused run whose sub-flow waits for a person keeps that
    decision; one whose sub-flow is working reads as running, in the sub-flow's workflow; one whose sub-flow failed
    says why, in the user's words. The step shown is the sub-flow's, since that is the work a person is waiting on."""
    href = f"/flows/{child.workflow_id}/runs/{child.id}"
    step = _step_label(child, with_workflow=True)
    if child.status == "paused":
        return {**execution, "current_step_label": step, "waiting_run": {
            "run_id": child.id, "status": child.status, "current_step_id": child.current_step_id, "href": href}}
    if child.status == "failed":
        return {**execution, "error": _failure_in_words(child), "current_step_id": child.current_step_id,
                "current_step_label": step, "href": href}
    return {**execution, "status": "running", "current_step_id": child.current_step_id, "workflow_name": _workflow_name(child),
            "current_step_label": step, "href": href}


def _failure_in_words(run: Any) -> str:
    """A failed run's reason as its user reads it, cut to the execution's length limit."""
    from packages.core.ai.workflow_failure import user_facing_run_error
    from packages.core.services.workflow_run_trace import STALLED_ERROR_PREFIX

    try:
        text = str(user_facing_run_error(run) or "")
    except Exception:  # a reason that cannot be read is shown as the run stored it
        text = str(run.error or "")
    # The run's stored error is the scheduler's stall text when a step stopped it; the step's own reason is the answer.
    if text.startswith(STALLED_ERROR_PREFIX):
        text = _failed_step_in_words(run) or text
    return text[:_PROJECT_EXECUTION_ERROR_LIMIT] if text else ""


def _failed_step_in_words(run: Any) -> str:
    """The reason of the step that failed in a run, as its user reads it; empty when no step recorded one."""
    from packages.core.ai.workflow_failure import user_facing_step_error
    from packages.core.i18n import t
    from packages.core.services.workflow_variable_limits import INLINE_FILE_DATA_MARK

    names = {str(node.get("id")): node.get("name") for node in ((run.definition_snapshot or {}).get("nodes") or [])
             if isinstance(node, dict)}
    for step_id, result in (run.step_results or {}).items():
        if not (isinstance(result, dict) and result.get("status") == "failed" and isinstance(result.get("error"), str)):
            continue
        step_name = str(names.get(step_id) or step_id)
        if INLINE_FILE_DATA_MARK in result["error"]:
            return t("workflow.failure.inline_file_data", step=step_name)
        return str(user_facing_step_error({"id": step_id, "name": step_name}, {"error": result["error"]}) or "")
    return ""


async def _waiting_child_runs(
    db: Any, *, entity_id: str, workspace_id: str, runs: list[WorkflowRun],
) -> tuple[dict[str, WorkflowRun], dict[str, WorkflowRun], dict[str, WorkflowRun]]:
    """The deepest paused, running and failed sub-flow under each run (up to three levels), by parent_run_id."""
    waiting: dict[str, WorkflowRun] = {}
    working: dict[str, WorkflowRun] = {}
    failed: dict[str, WorkflowRun] = {}
    frontier = {run.id: run.id for run in runs}
    for _ in range(3):
        if not frontier:
            break
        children = list((await db.execute(select(WorkflowRun).where(
            WorkflowRun.entity_id == entity_id, WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.trigger_data["parent_run_id"].astext.in_(list(frontier)),
            WorkflowRun.status.in_(["paused", "running", "failed"]),
        ))).scalars())
        next_frontier: dict[str, str] = {}
        for child in children:
            top = frontier[str((child.trigger_data or {}).get("parent_run_id"))]
            {"paused": waiting, "running": working}.get(child.status, failed)[top] = child
            next_frontier[child.id] = top
        frontier = next_frontier
    return waiting, working, failed


def _record_project_sources(sources: dict[str, Any], record_source: str) -> list[str]:
    """Return the declared project path from a record owner to its root."""
    return app_data_project_sources(sources, record_source)


async def _record_ancestor_states(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], record_source: str,
    records: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Resolve only configured project ancestry for the supplied record page."""
    chain = _record_project_sources(sources, record_source)
    project_row = sources[record_source]["type"] in {"workflow_projects", "related_projects"}
    current = {row["id"]: row["id"] if project_row else row.get("project_id")
               for row in records if isinstance(row.get("id"), str)
               and (project_row or isinstance(row.get("project_id"), str))}
    resolved: dict[str, dict[str, dict[str, Any]]] = {row_id: {} for row_id in current}
    for index, source_name in enumerate(chain):
        project_ids = {project_id for project_id in current.values() if isinstance(project_id, str)}
        rows = list((await db.execute(select(WorkflowProject).where(
            WorkflowProject.id.in_(project_ids), WorkflowProject.entity_id == user.entity_id,
            WorkflowProject.workspace_id == workspace.id,
            WorkflowProject.project_type == sources[source_name]["project_type"],
            WorkflowProject.schema_version.in_(sources[source_name]["schema_versions"]),
        ))).scalars()) if project_ids else []
        by_id = {row.id: row for row in rows}
        for record_id, project_id in current.items():
            row = by_id.get(project_id)
            if row is not None:
                resolved[record_id][source_name] = {"id": row.id, "state": row.state or {}}
        if index + 1 == len(chain):
            break
        child_source = sources[source_name]
        current = {
            record_id: _value_at(by_id[project_id].state or {}, child_source["parent_state_path"])
            for record_id, project_id in current.items() if project_id in by_id
        }
    return resolved


async def _record_media(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], bindings: list[dict[str, Any]],
    ancestors: dict[str, Any], records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Resolve bounded, declared attachments for these exact records, never a source-wide gallery."""
    references: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for row in records:
        for binding in bindings:
            source = sources[binding["source"]]
            owner = ancestors.get(row["id"], {}).get(source["parent"], {})
            document_id = _value_at(owner.get("state", {}), source["document_path"])
            if isinstance(document_id, str) and document_id:
                references.setdefault(row["id"], []).append((binding, document_id))
    document_ids = {doc_id for refs in references.values() for _, doc_id in refs}
    if not document_ids:
        return {}
    docs = list((await db.execute(select(Document).where(
        Document.entity_id == user.entity_id, Document.id.in_(document_ids), Document.is_trashed.is_(False),
    ))).scalars())
    access = await DocumentAccessContext.load(db, entity_id=user.entity_id, user_id=user.id,
                                              role=user.role, document_ids={doc.id for doc in docs})
    await access.preload_documents(db, docs)
    readable = {}
    for doc in docs:
        if await access.can_read_document(db, doc, workspace_id=workspace.id):
            readable[doc.id] = with_thumbnail_url({
                "document_id": doc.id, "title": doc.name, "mime_type": doc.mime_type,
                "file_size": doc.file_size, "updated_at": _iso(doc.updated_at or doc.created_at),
                "viewer_url": f"/viewer/{doc.id}"}, doc)
    return {row_id: [{**readable[doc_id], "label": binding.get("label") or sources[binding["source"]].get("document_role")}
                     for binding, doc_id in refs if doc_id in readable]
            for row_id, refs in references.items()}


async def _readable_media(db: Any, workspace: Any, user: Any, document_ids: set[str]) -> dict[str, dict[str, Any]]:
    """The media item of each named Document that still exists and this viewer may read."""
    docs = list((await db.execute(select(Document).where(
        Document.entity_id == user.entity_id, Document.id.in_(document_ids), Document.is_trashed.is_(False),
    ))).scalars()) if document_ids else []
    access = await DocumentAccessContext.load(db, entity_id=user.entity_id, user_id=user.id, role=user.role,
                                              document_ids={doc.id for doc in docs})
    await access.preload_documents(db, docs)
    # Its pixel size and length where the file records them (a poster's 1080×1350, a short's 9:16).
    return {doc.id: with_thumbnail_url({"document_id": doc.id, "title": doc.name, "mime_type": doc.mime_type,
                                        "viewer_url": f"/viewer/{doc.id}", "file_size": doc.file_size,
                                        **media_facts(doc.metadata_)}, doc)
            for doc in docs if await access.can_read_document(db, doc, workspace_id=workspace.id)}


async def _with_media_details(db: Any, user: Any, items: list[dict[str, Any]]) -> None:
    """How each file a gallery shows was made (source, model, prompt), for a gallery that shows it.

    Only files already on these items are read: each was checked readable when it was picked."""
    files = [media for item in items for media in [*(item.get("media") or []),
                                                    *(entry for version in item.get("versions") or []
                                                      for entry in version.get("media") or [])]]
    ids = {media["document_id"] for media in files if media.get("document_id")}
    if not ids:
        return
    found = {row.id: media_details(row.metadata_, source=row.source, created_at=_iso(row.created_at))
             for row in await db.execute(select(Document.id, Document.metadata_, Document.source, Document.created_at).where(
                 Document.entity_id == user.entity_id, Document.id.in_(ids)))}
    for media in files:
        if found.get(media.get("document_id")):
            media["details"] = found[media["document_id"]]


def _record_ids(value: Any) -> list[str]:
    """The Workspace record (project) ids a ``record`` field holds: one id, or a list of them."""
    return selected_project_ids(value)[:_RECORD_FIELD_LIMIT]


def _record_label(row: WorkflowProject) -> str | None:
    """A record's name as its picker gives it; None when it has nothing but an id or an opaque key."""
    title = project_title(row)
    if title == row.id or (title == (row.project_key or "").strip() and _has_opaque_part(title)):
        return None
    return title


async def _named_records(
    db: Any, workspace: Any, user: Any, fields: Any, rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Each record and member the ``record`` and ``member`` fields of these rows name, by id.

    A record is read as a project source reads its own projects, in this Workspace and
    entity only: an id of another Workspace, or of a record deleted since, is
    not found, so it shows as unavailable and never by its id. A member (keyed
    ``member:<id>``) is read the same way, by ``_named_members``.
    """
    def ids_of(field_format: str, read: Callable[[Any], list[str]]) -> set[str]:
        paths = [".".join(field["field"]) for field in fields or []
                 if isinstance(field, dict) and field.get("format") == field_format]
        return {found for row in rows if paths and isinstance(row.get("fields"), dict)
                for path in paths for found in read(row["fields"].get(path))}

    named: dict[str, dict[str, Any]] = {}
    ids = ids_of("record", _record_ids)
    if ids:
        found = (await db.execute(select(WorkflowProject).where(
            WorkflowProject.id.in_(ids), WorkflowProject.entity_id == user.entity_id,
            WorkflowProject.workspace_id == workspace.id,
        ))).scalars()
        named.update({row.id: {"project_type": row.project_type, "label": _record_label(row)} for row in found})
    member_ids = ids_of("member", _record_ids)
    if member_ids:
        named.update({f"member:{member_id}": member
                      for member_id, member in (await _named_members(db, workspace, user, member_ids)).items()})
    return named


async def _named_members(db: Any, workspace: Any, user: Any, ids: set[str]) -> dict[str, dict[str, Any]]:
    """The people and AI staff of this Workspace's entity these ids name: ``{name, kind, avatar_url?}`` by id.

    A person counts when they belong to the entity or hold a seat in this Workspace;
    AI staff and staff records when they are the entity's own. Anyone else (another
    company's, deleted) is not found, so the page shows them as unavailable, never by id.
    """
    from packages.core.models.staff import Staff
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Agent, WorkspaceStaff

    entity_id = user.entity_id
    members: dict[str, dict[str, Any]] = {}

    def add(member_id: str, name: Any, kind: str, avatar: Any) -> None:
        name = str(name or "").strip()
        if name and member_id not in members:
            members[member_id] = {"name": name[:80], "kind": kind,
                                  **({"avatar_url": avatar.strip()} if isinstance(avatar, str) and avatar.strip() else {})}

    people = await db.execute(select(
        User.id, User.display_name, User.first_name, User.last_name, User.email, User.avatar_url,
    ).where(
        User.id.in_(ids), User.deleted_at.is_(None),
        or_(User.entity_id == entity_id,
            User.id.in_(select(UserMembership.user_id).where(
                UserMembership.entity_id == entity_id, UserMembership.status == "active",
                UserMembership.deleted_at.is_(None))),
            User.id.in_(select(WorkspaceStaff.user_id).where(
                WorkspaceStaff.workspace_id == workspace.id, WorkspaceStaff.status == "active"))),
    ))
    for row in people:
        name = (row.display_name or " ".join(part for part in (row.first_name, row.last_name) if part)
                or str(row.email or "").split("@")[0])
        add(row.id, name, "user", row.avatar_url)
    rest = ids - set(members)
    if rest:
        for row in await db.execute(select(Agent.id, Agent.name, Agent.avatar_url).where(
                Agent.id.in_(rest), Agent.entity_id == entity_id, Agent.deleted_at.is_(None))):
            add(row.id, row.name, "agent", row.avatar_url)
    rest = ids - set(members)
    if rest:
        for row in await db.execute(select(Staff.id, Staff.name, Staff.avatar_url).where(
                Staff.id.in_(rest), Staff.entity_id == entity_id, Staff.deleted_at.is_(None))):
            add(row.id, row.name, "staff", row.avatar_url)
    return members


def _pictured(media: dict[str, Any]) -> bool:
    return str(media.get("mime_type") or "").lower().startswith(("image/", "video/"))


async def _gallery_tiles(
    db: Any, workspace: Any, user: Any, block: dict[str, Any], rows: list[dict[str, Any]],
) -> _SourceRows:
    """One tile per picture a project or record shows, never one per row.

    A project shows its ``display.media``; a record (or a project) also each
    picture its block's ``image`` fields name. A file that is gone, unreadable
    or not an image or video makes no tile.
    """
    image_fields = [".".join(field["field"]) for field in block.get("fields") or []
                    if isinstance(field, dict) and field.get("format") == "image"]
    named: list[tuple[dict[str, Any], list[dict[str, str]]]] = []
    for row in rows:
        values = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        files = list((row.get("display") or {}).get("media") or [])
        for key in image_fields:
            value = values.get(key)
            files.extend(file for file in map(display_file, value if isinstance(value, list) else [value]) if file)
        named.append((row, files))
    readable = await _readable_media(db, workspace, user, {file["document_id"] for _, files in named for file in files})
    tiles = _SourceRows()
    tiles.next_cursor = getattr(rows, "next_cursor", None)
    for row, files in named:
        record = row.get("record") if isinstance(row.get("record"), dict) else {}
        title = record.get("title") if isinstance(record.get("title"), str) else row.get("label")
        seen: set[str] = set()
        for file in files:
            media = readable.get(file["document_id"])
            if media is None or not _pictured(media) or file["document_id"] in seen:
                continue
            seen.add(file["document_id"])
            tiles.append({**row, "id": f"{row.get('id')}:{file['document_id']}", "group": row.get("id"),
                          "label": file.get("label") or title or media["title"],
                          "state": "completed", "media": [{**media, **({"label": file["label"]} if file.get("label") else {})}]})
    return tiles


def _publication_identity(record: dict[str, Any]) -> tuple[tuple[str, str], set[str]] | None:
    from packages.core.services.public_social_metrics import canonicalize_public_post_url

    try:
        identity = canonicalize_public_post_url(str(record.get("platform") or ""), str(record.get("published_url") or ""))
    except ValueError:
        return None
    account = record.get("account")
    values = account.values() if isinstance(account, dict) else (account,)
    return identity, {str(value).strip() for value in values if isinstance(value, str) and value.strip()}


def _publication_collector_types(source: dict[str, Any]) -> list[str]:
    return list(source.get("collector_types") or DEFAULT_PUBLICATION_STAT_COLLECTOR_TYPES)


def _stat_matches_publication(stat: WorkspaceStat, *, subject_id: str, record: dict[str, Any]) -> bool:
    cfg = stat.collector_config or {}
    subject = cfg.get("subject") if isinstance(cfg.get("subject"), dict) else {}
    if subject.get("kind") != "workflow_project" or subject.get("id") != subject_id:
        return False
    receipt = _publication_identity(record)
    candidate = _publication_identity({"platform": cfg.get("platform"), "published_url": cfg.get("public_url")})
    if receipt is None or candidate is None or receipt[0] != candidate[0]:
        return False
    receipt_accounts = receipt[1]
    stat_account = str(cfg.get("account_id") or cfg.get("account_handle") or "").strip()
    return not receipt_accounts or not stat_account or stat_account in receipt_accounts


def _launch_label(run: WorkflowRun, binding_name: str | None = None, locale: str | None = None) -> str:
    """Use durable run metadata rather than exposing an internal run identifier."""
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    trigger = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    name = (run_names_for(trigger.get(RUN_DISPLAY_I18N_KEY), locale).get("name")
            or binding_name or snapshot.get("name") or trigger.get("_workflow_name") or "Workflow")
    started = run.started_at or run.created_at
    started_label = started.strftime("%Y-%m-%d") if started else "not started"
    return f"{name} · {started_label} · {run.status}"


def _page(rows: list[Any], cursor: str | None, limit: int) -> tuple[list[Any], str | None]:
    start = 0
    if cursor:
        for index, item in enumerate(rows):
            item_id = item.get("_cursor", item.get("id")) if isinstance(item, dict) else getattr(item, "id", None)
            if item_id == cursor:
                start = index + 1
                break
    selected = rows[start:start + limit]
    forced_cursor = getattr(rows, "next_cursor", None)
    more = start + limit < len(rows) or forced_cursor is not None
    last = selected[-1] if selected else None
    last_id = last.get("_cursor", last.get("id")) if isinstance(last, dict) else getattr(last, "id", None)
    return selected, (last_id if start + limit < len(rows) else forced_cursor) if more else None


def _flat_record_fields(record: Any) -> dict[str, Any]:
    """A record's own top-level values, for a block's configured ``fields``."""
    if not isinstance(record, dict):
        return {}
    # A list of words (tags) or of files (photos) is a value too; any other structure is not.
    return {key: value for key, value in record.items() if isinstance(value, (str, int, float, bool)) or value is None
            or (isinstance(value, list) and len(value) <= 50
                and all(isinstance(part, (str, int, float)) or display_file(part) for part in value))}


def _parent_cursor(cursor: str | None) -> tuple[str | None, int]:
    """Decode a source-owned cursor without accepting a query expression."""
    if not cursor:
        return None, -1
    parent, separator, offset = cursor.partition("|")
    if not parent or not separator:
        return cursor, -1
    try:
        return parent, max(-1, int(offset))
    except ValueError:
        raise AppDataError("invalid_cursor", "The component cursor is invalid.") from None


def _present_items(block: dict[str, Any], rows: list[dict[str, Any]], *, window: str | None = None,
                   records: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Turn adapter rows into the small, renderer-neutral App item shape.

    ``records`` holds what ``_named_records`` read for the block's ``record`` fields.
    """
    fields = block.get("fields") or []
    presented: list[dict[str, Any]] = []
    for row in rows:
        row_fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
        selected_fields = []
        for configured in fields:
            if block["type"] == "gallery" and isinstance(configured, dict) and configured["format"] == "image":
                continue  # The tile is that picture.
            if isinstance(configured, dict):
                value = row_fields.get(".".join(configured["field"]))
                if list(configured["field"]) == ["held_reason"]:
                    # A held reason outlives its hold: a retry that leases the item again keeps the stored text.
                    # It shows only while the item is held; a failed sub-flow's own failure is the reason instead.
                    if execution.get("status") == "failed" and execution.get("error"):
                        value = execution["error"]
                    elif row.get("stage") != "held":
                        continue
                if configured["format"] == "status" and block.get("stage_labels"):
                    # A status field reads through the block's stage labels, then its own.
                    configured = {**configured, "labels": {**block["stage_labels"], **configured.get("labels", {})}}
                item = _present_configured_field(configured, value, lambda path: row_fields.get(".".join(path)),
                                                 records=records)
                if item is not None:
                    selected_fields.append(item)
            else:
                selected_fields.append({"label": ".".join(configured), "value": row_fields.get(".".join(configured))})
        if block["type"] == "metrics":
            observations = row.get("observations") if isinstance(row.get("observations"), list) else []
            comparison = row.get("comparison") if isinstance(row.get("comparison"), dict) else {}
            value = comparison.get("latest_value")
            if not comparison and row.get("current_value") is not None:
                value = row["current_value"]
            presented.append({"id": row.get("id"), "_cursor": row.get("_cursor", row.get("id")), "label": row.get("name") or row.get("key"), "metric_key": row.get("metric_key"), "value": value,
                              "value_type": row.get("value_type"), "unit": row.get("unit"), "change": comparison.get("exact_delta"),
                              "previous_value": None, "trend": [{"value": item.get("value"), "observed_at": item.get("observed_at")} for item in reversed(observations[:100])],
                              "subtitle": row.get("platform"), "href": _public_href(row.get("public_url")),
                              "updated_at": comparison.get("latest_observed_at"),
                              "status": row.get("freshness") or row.get("collection_status") or row.get("status"),
                              "freshness": row.get("freshness"), "collection_error": row.get("collection_error"),
                              "delta_reason": comparison.get("delta_reason"),
                              # How the number is worked out, from its Stat's own definition.
                              **({"description": row["description"]} if row.get("description") else {}),
                              **({"window": row["window"]} if row.get("window") else {}),
                              "detail": comparison.get("delta_reason") or row.get("collection_error")})
        elif block["type"] == "progress":
            job_state = row.get("state") if "enabled" in row else row.get("last_status")
            display = row.get("display") or {}
            item = {"id": row.get("id"), "_cursor": row.get("_cursor", row.get("id")), "label": row.get("label") or row.get("name") or row.get("job_id") or row.get("id"),
                              "status": row.get("stage") or job_state or ("enabled" if row.get("enabled") else "paused" if "enabled" in row else None),
                              "detail": display.get("summary") or row.get("project_type") or row.get("name") or row.get("last_status"), "updated_at": row.get("updated_at") or row.get("last_run_at"),
                              "fields": selected_fields, "execution": row.get("execution"), "parent_id": row.get("parent_id"),
                              **({"status_label": display["status"]} if display.get("status") else {})}
            for key in ("state", "enabled", "last_status", "last_run_at", "next_run_at", "stop_after"):
                if key in row:
                    item[key] = row[key]
            item.update(_collection_values(block, row, row_fields))
            if "calendar_at" in row:
                item["calendar_at"] = row["calendar_at"]
            if "calendar_lane" in row:
                item["calendar_lane"] = row["calendar_lane"]
                if preview_platform(row["calendar_lane"]):
                    item["calendar_lane_platform"] = preview_platform(row["calendar_lane"])
            presented.append(item)
        elif block["type"] in {"documents", "packs"}:
            if block["type"] == "packs" and not row.get("pack"):
                continue  # A file of the project itself (its references) belongs to no child's pack.
            card = row.get("role") == "pack"  # A child's card before it has files; no Document.
            if card and block["type"] == "documents":
                continue
            presented.append({"id": row.get("id"), "_cursor": row.get("_cursor", row.get("id")),
                              "document_id": None if card else row.get("id"), "title": row.get("name") or ("" if card else row.get("id")),
                              "subtitle": row.get("mime_type") or row.get("role"), "viewer_url": row.get("viewer_url"),
                              "updated_at": row.get("created_at"), "fields": selected_fields,
                              **({"folder": row["folder"]} if "folder" in row else {}),
                              **({"thumbnail_url": row["thumbnail_url"]} if row.get("thumbnail_url") else {}),
                              **({"pack": row["pack"], "delivered": bool(row.get("delivered"))} if row.get("pack") else {})})
        elif block["type"] == "gallery":
            presented.append({"id": row.get("id"), "run_id": row.get("run_id"), "workflow_id": row.get("workflow_id"),
                              "binding_id": row.get("binding_id"),
                              "title": row.get("label"), "status": row.get("state"), "fields": selected_fields,
                              "media": row.get("media") or [], "updated_at": row.get("updated_at"),
                              **({"group": row["group"]} if (block.get("media") or {}).get("group") and row.get("group") else {}),
                              **{key: row[key] for key in ("version", "versions") if key in row},
                              **({"progress_percent": row["progress_percent"]} if "progress_percent" in row else {})})
        elif block["type"] == "references":
            presented.append({"id": row.get("id"), "_cursor": row.get("_cursor", row.get("id")), "kind": row.get("kind"),
                              "role": row.get("role"), "reference": row.get("value") or {}, "inherited": bool(row.get("inherited")),
                              "holder_project_id": row.get("holder_project_id"), "holder_label": row.get("holder_label"),
                              **({"media": row["media"]} if row.get("media") else {})})
        else:
            record = row.get("record") if isinstance(row.get("record"), dict) else {}
            publication = row.get("schema") == "publication-receipt/v1"
            display_title = row.get("display_title")
            record_title = record.get("title")
            if isinstance(record_title, str) and isinstance(record.get("code"), str) and record["code"].strip():
                # A numbered record (a planned episode) reads as "E01 · title".
                record_title = f"{record['code'].strip()} · {record_title}"
            # A project listed as a record (not an item of a project's list) is named by
            # its own label; its type is an identifier, and its stage reads through the block's labels.
            project_row = "record" not in row
            display = row.get("display") or {}
            status = record.get("verification_status") if publication else record.get("status") or row.get("stage")
            stage_labels = block.get("stage_labels") or {}
            if project_row and display.get("status") and status not in stage_labels:
                status = display["status"]
            # An item of a project's list is "<project id>:<index>"; with no title of its own it reads by its fields.
            item = {"id": row.get("id"), "_cursor": row.get("_cursor", row.get("id")), "title": display_title if isinstance(display_title, str) and display_title.strip() else record_title or record.get("published_url") or row.get("label") or row.get("project_key") or (row.get("id") if project_row else ""),
                              "subtitle": record.get("platform") if publication else display.get("summary") if project_row else row.get("project_type"),
                              "status": stage_labels.get(status, status) if isinstance(status, str) else status,
                              "detail": record.get("published_at") if publication else None, "href": _public_href(record.get("published_url")) if publication else None,
                              "updated_at": row.get("updated_at"), "fields": selected_fields + list(row.get("display_fields") or []),
                              "execution": row.get("execution")}
            if publication:
                item.update(platform=record.get("platform"), published_at=record.get("published_at"))
            for key in ("metrics", "published_at", "tracking_status", "conflict", "media"):
                if key in row:
                    item[key] = row[key]
            presented.append(item)
    if len(presented) == len(rows):
        for row, item in zip(rows, presented):
            values = _action_values(block, row.get("fields") if isinstance(row.get("fields"), dict) else {})
            if values:
                item["action_values"] = values
            if block.get("preview"):
                item["preview"] = _present_preview(block["preview"], row)
    return presented


def _plain_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(part for part in (_plain_text(item) for item in value) if part)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()[:120] if isinstance(value, (str, int, float)) else ""


def _collection_values(block: dict[str, Any], row: dict[str, Any], row_fields: dict[str, Any]) -> dict[str, Any]:
    """What a progress block's groupings, timeline, gantt and places read from one record, in people's words."""
    out: dict[str, Any] = {}
    labels_of = {tuple(field["field"]): field.get("labels") or {} for field in block.get("fields") or [] if isinstance(field, dict)}
    if block.get("group_by"):
        groups = []
        for choice in block["group_by"]:
            if choice.get("by") == "stage":
                stage = row.get("stage") or ""
                groups.append((block.get("stage_labels") or {}).get(stage, stage))
                continue
            value = row_fields.get(".".join(choice["field"]))
            labels = labels_of.get(tuple(choice["field"]), {})
            parts = value if isinstance(value, list) else [value]
            groups.append(", ".join(dict.fromkeys(labels.get(text, text) for text in map(_plain_text, parts) if text)))
        out["groups"] = groups
    timeline = block.get("timeline")
    if timeline:
        start, end = (row_fields.get(".".join(timeline[key])) if key in timeline else None for key in ("date_field", "end_field"))
        depends = row_fields.get(".".join(timeline["depends_field"])) if "depends_field" in timeline else None
        out["time"] = {"start": start if isinstance(start, str) and start.strip() else None,
                       **({"end": end if isinstance(end, str) and end.strip() else None} if "end_field" in timeline else {}),
                       **({"depends_on": _record_ids(depends)[:20]} if "depends_field" in timeline else {})}
    places = block.get("places")
    if places:
        address = _plain_text(row_fields.get(".".join(places["address_field"])))
        name = _plain_text(row_fields.get(".".join(places["name_field"]))) if "name_field" in places else ""
        lat = _number(row_fields.get(".".join(places["lat_field"]))) if "lat_field" in places else None
        lng = _number(row_fields.get(".".join(places["lng_field"]))) if "lng_field" in places else None
        coordinates = lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180
        out["place"] = {"address": address or None, **({"name": name} if name else {}),
                        **({"lat": lat, "lng": lng} if coordinates else {})}
    return out


# How much of a post a preview carries: a long caption whole, a thread's posts and a post's collected replies.
_PREVIEW_TEXT_LIMITS = {"title": 300, "caption": 20_000, "account": 80, "summary": 2_000}
_PREVIEW_THREAD_LIMIT = 25
_PREVIEW_COMMENT_LIMIT = 50
_PREVIEW_HASHTAG_LIMIT = 60


def _preview_text(value: Any, limit: int) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _preview_lookup(row: dict[str, Any]) -> Callable[[list[str]], Any]:
    """Where a row's post parts are read: a receipt's post project, a list item itself, else the row's fields."""
    if isinstance(row.get("_preview_state"), dict):
        return lambda path: _value_at(row["_preview_state"], path)
    if isinstance(row.get("record"), dict):
        return lambda path: _value_at(row["record"], path)
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    return lambda path: fields.get(".".join(path))


def _present_preview(preview: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """One record as the post it will be: its frame, words, files, tags, who posts it and when; a receipt adds
    where it went out. Files go as Document ids the viewer opens, or public picture addresses."""
    lookup = _preview_lookup(row)
    paths = preview["fields"]

    def read(role: str) -> Any:
        return lookup(paths[role]) if role in paths else None

    def presented(role: str, field_format: str) -> dict[str, Any] | None:
        if role not in paths:
            return None
        return _present_configured_field({"label": role, "format": field_format, "hide_when_empty": True}, read(role))

    receipt = row.get("record") if row.get("schema") == "publication-receipt/v1" and isinstance(row.get("record"), dict) else {}
    out: dict[str, Any] = {}
    # Where a receipt says the post went wins over where its project planned it.
    named = receipt.get("platform") or read("platform")
    platform = preview_platform(named)
    if platform is None and isinstance(named, str) and named.strip():
        # A platform with no frame of its own shows a plain post under its own name.
        out["platform_label"] = named.strip()[:40]
    elif platform is None:
        platform = preview.get("platform")
    out["platform"] = platform
    for role, limit in _PREVIEW_TEXT_LIMITS.items():
        text = _preview_text(read(role), limit)
        if text:
            out[role] = text
    if not out.get("title") and isinstance(receipt.get("title"), str) and receipt["title"].strip():
        out["title"] = receipt["title"].strip()[:300]
    images = presented("images", "images")
    if images and images.get("images"):
        out["images"] = images["images"]
    for role, field_format in (("video", "video"), ("cover", "image"), ("avatar", "image")):
        item = presented(role, field_format)
        if item and (item.get("document_id") or item.get("image_url")):
            out[role] = {key: item[key] for key in ("document_id", "href", "image_url") if item.get(key)}
    # Hashtags kept as one line of text ("#brunch #coffee") read as the words they are.
    hashtags = read("hashtags")
    if isinstance(hashtags, str):
        hashtags = re.split(r"[\s,，、]+", hashtags)
    words = [str(word).strip().lstrip("#＃").strip()[:80] for word in (hashtags if isinstance(hashtags, list) else [])
             if isinstance(word, (str, int)) and not isinstance(word, bool)]
    if any(words):
        # Every one a platform takes (Instagram 30), so the copied group is the one that goes out.
        out["hashtags"] = list(dict.fromkeys(word for word in words if word))[:_PREVIEW_HASHTAG_LIMIT]
    link = _public_href(read("link"))
    if link:
        out["link"] = link
    when = read("published_at") if "published_at" in paths else None
    if isinstance(when, str) and when.strip():
        out["published_at"] = when.strip()
    thread = read("thread")
    if isinstance(thread, list):
        posts = [_preview_text(part.get("text") if isinstance(part, dict) else part, 4_000) for part in thread]
        posts = [post for post in posts if post][:_PREVIEW_THREAD_LIMIT]
        if posts:
            out["thread"] = posts
    comments = read("comments")
    if isinstance(comments, list):
        replies = []
        for entry in comments:
            text = _preview_text(entry.get("text") or entry.get("body"), 1_000) if isinstance(entry, dict) else None
            if not text:
                continue
            author = _preview_text(entry.get("author") or entry.get("name"), 80)
            at = entry.get("at") or entry.get("created_at")
            likes = _number(entry.get("likes"))
            replies.append({"text": text, **({"author": author} if author else {}),
                            **({"at": at.strip()} if isinstance(at, str) and at.strip() else {}),
                            **({"likes": likes} if likes is not None and likes >= 0 else {})})
            if len(replies) == _PREVIEW_COMMENT_LIMIT:
                break
        if replies:
            out["comments"] = replies
    if "group_field" in preview:
        group = lookup(preview["group_field"])
        if isinstance(group, (str, int)) and not isinstance(group, bool) and str(group).strip():
            out["group"] = str(group).strip()[:120]
    return out


def _action_values(block: dict[str, Any], row_fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What each card action fills its form with from this record (``prefill``), by entry key.
    Only plain values travel: text, numbers, yes/no, lists of text and the files it keeps."""
    def plain(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list) and all(isinstance(part, (str, int, float)) for part in value):
            return value
        # Files the record keeps (a dish photo) go back to the form as the files they are, by id.
        files = [display_file(part) for part in value] if isinstance(value, list) else []
        return files if files and all(files) else None

    filled: dict[str, dict[str, Any]] = {}
    for _, action in block_card_actions(block):
        values = {name: plain(row_fields.get(".".join(path))) for name, path in (action.get("prefill") or {}).items()}
        values = {name: value for name, value in values.items() if value is not None}
        if values:
            filled[action["action"]] = values
    return filled


def _summary_is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _secondary_value(configured: dict[str, Any], lookup: Callable[[list[str]], Any]) -> str | None:
    for path in configured.get("secondary") or []:
        value = lookup(path)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
    return None


def _present_configured_field(
    configured: dict[str, Any], value: Any, lookup: Callable[[list[str]], Any] | None = None,
    *, records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    field_format = configured["format"]
    item: dict[str, Any] = {"label": configured["label"], "format": field_format}
    if field_format == "provider":
        # The renderer resolves the logo from the raw key; the label stays configurable.
        key = value.strip().lower() if isinstance(value, str) else None
        item.update(value=configured.get("labels", {}).get(key, value) if key else None, provider=key)
    elif field_format == "link":
        href = _public_href(value)
        item.update(value=value if href else None, href=href)
    elif field_format == "image" and _public_href(value):
        # A picture given as a public address (a dish photo URL the owner entered).
        item.update(value=None, href=_public_href(value), image_url=_public_href(value))
    elif field_format in {"document", "image", "video", "audio"}:
        # Only a Document ID reaches the viewer; the path is built here, never copied from state.
        # An uploaded picture is kept as a one-file list; the field shows its first file.
        file = display_file(value[0] if isinstance(value, list) and value else value)
        item.update(value=None, href=f"/viewer/{file['document_id']}" if file else None)
        if field_format != "document" and file:
            item["document_id"] = file["document_id"]
    elif field_format == "images":
        # Several pictures: Document ids (or file references) and public addresses, each once.
        images, seen = [], set()
        for part in (value if isinstance(value, list) else [value])[:_IMAGES_FIELD_LIMIT]:
            file, address = display_file(part), _public_href(part)
            key = file["document_id"] if file else address
            if not key or key in seen:
                continue
            seen.add(key)
            images.append({"document_id": file["document_id"], "href": f"/viewer/{file['document_id']}",
                           **({"label": file["label"]} if file.get("label") else {})} if file
                          else {"image_url": address, "href": address})
        item.update(value=None, images=images)
    elif field_format == "clips":
        # A video's shots, one line in several voices: each file once, played on the page.
        item.update(value=None, clips=media_file_list(value))
    elif field_format == "timeline":
        item.update(value=None, segments=timeline_segments(value))
    elif field_format == "sparkline":
        # A list of numbers (or of {value}) the Workflow kept, oldest first: the card draws the latest of them.
        parts = value if isinstance(value, list) else []
        points = [_number(part.get("value") if isinstance(part, dict) else part) for part in parts]
        item.update(value=None, points=[point for point in points if point is not None][-_SPARKLINE_FIELD_LIMIT:])
    elif field_format in {"number", "money", "rating", "percent", "duration", "file_size", "progress"}:
        # A number the Workflow saved as text ("38") still reads as one; anything else shows nothing.
        number = _number(value)
        if field_format == "rating" and number is not None:
            number = min(max(number, 0), 5)
        if field_format in {"duration", "file_size"} and number is not None and number < 0:
            number = None
        if field_format == "percent" and number is not None:
            # The page reads a share as 0–1, whichever scale the Workflow saved it on.
            number = number / configured["scale"]
        item["value"] = number
        if field_format == "money":
            item["currency"] = configured["currency"]
        if configured.get("unit"):
            item["unit"] = configured["unit"]
        if field_format == "progress":
            if configured.get("max_field"):
                bound = _number(lookup(configured["max_field"])) if lookup else None
            else:
                bound = configured.get("max", 100)
            if bound is None or bound <= 0:
                item["value"] = None  # Out of nothing reads as nothing, never as a full bar.
            item["max"] = bound if bound and bound > 0 else None
    elif field_format == "date_range":
        # From its field to its end_field; either end may be open ("from 11-14").
        end = lookup(configured["end_field"]) if lookup else None
        item.update(value=value if isinstance(value, str) and value.strip() else None,
                    end=end if isinstance(end, str) and end.strip() else None)
    elif field_format == "tags":
        words = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        labels = configured.get("labels", {})
        words = [str(word).strip() for word in words if isinstance(word, (str, int)) and not isinstance(word, bool)]
        tags = [labels.get(word, word) for word in words if word]
        item.update(value=None, tags=list(dict.fromkeys(str(tag)[:80] for tag in tags))[:_TAGS_FIELD_LIMIT])
    elif field_format == "member":
        # Each person by name and picture; one that is gone or of another company only counts.
        members, unavailable = [], 0
        for member_id in _record_ids(value):
            member = (records or {}).get(f"member:{member_id}")
            if member is None:
                unavailable += 1
            else:
                members.append(member)
        item.update(value=None, members=members, **({"unavailable": unavailable} if unavailable else {}))
    elif field_format == "color":
        text = value.strip().lower() if isinstance(value, str) else ""
        if re.fullmatch(r"#[0-9a-f]{3}", text):
            text = "#" + "".join(part * 2 for part in text[1:])
        item["value"] = text if re.fullmatch(r"#[0-9a-f]{6}", text) else None
    elif field_format in {"phone", "email", "address", "code"}:
        # A number kept as digits (a phone) reads as its text; a snippet keeps its indentation.
        text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        text = text.strip("\r\n").rstrip() if field_format == "code" else text.strip()
        item["value"] = text[:_CODE_FIELD_LIMIT if field_format == "code" else 500] or None
    elif field_format == "boolean":
        flag = value if isinstance(value, bool) else {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(
            str(value).strip().lower()) if value is not None else None
        item["value"] = None if flag is None else configured.get("labels", {}).get(str(flag).lower(), flag)
    elif field_format == "link_list":
        links = []
        for link in value if isinstance(value, list) else []:
            if not isinstance(link, dict):
                continue
            href = _public_href(link.get("url"))
            label = link.get("label")
            if href:
                links.append({"label": label.strip() if isinstance(label, str) and label.strip() else href, "href": href})
        item.update(links=links, value=None)
    elif field_format == "record":
        # Each record by its name; one that is gone, elsewhere or of another type only counts as unavailable.
        named, unavailable = [], 0
        for project_id in _record_ids(value):
            record = (records or {}).get(project_id)
            if record is None or configured.get("project_type") not in (None, record["project_type"]):
                unavailable += 1
            else:
                named.append({"id": project_id, "label": record["label"]})
        item.update(value=None, records=named, **({"unavailable": unavailable} if unavailable else {}))
    else:
        item["value"] = configured.get("labels", {}).get(str(value), value)
    secondary = _secondary_value(configured, lookup) if lookup else None
    if secondary:
        item["secondary"] = secondary
    if configured.get("relative"):
        item["relative"] = True
    if configured.get("copy"):
        item["copy"] = True
    if configured.get("bar"):
        item["bar"] = True
    if field_format in {"image", "images", "video", "audio", "clips"}:
        item.update(media_companions(configured, lookup or (lambda _path: None), _public_href))
    present = (bool(item.get("href") or item.get("links") or item.get("records") or item.get("members")
                    or item.get("images") or item.get("tags") or item.get("clips") or item.get("segments")
                    or item.get("unavailable") or len(item.get("points") or []) > 1)
               if field_format in {"link", "link_list", "document", "image", "video", "audio", "record", "member", "images",
                                   "tags", "clips", "timeline", "sparkline"}
               else bool(item.get("value") or item.get("end")) if field_format == "date_range"
               else not _summary_is_empty(item.get("value")))
    return None if configured["hide_when_empty"] and not present else item


def _present_summary(block: dict[str, Any], rows: list[dict[str, Any]],
                     records: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Project one selected WorkflowProject into renderer-neutral summary fields."""
    if not rows:
        return []
    row = rows[0]
    values = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    fields = []
    for configured in block["fields"]:
        value = values.get(".".join(configured["field"]))
        item = _present_configured_field(configured, value, lambda path: values.get(".".join(path)), records=records)
        if item is not None:
            fields.append(item)
    values_of_actions = _action_values(block, values)
    return [{"id": row.get("id"), "_cursor": row.get("_cursor", row.get("id")), "fields": fields,
             **({"action_values": values_of_actions} if values_of_actions else {})}] if fields else []


@dataclass(frozen=True)
class _Narrowing:
    """What a person chose to narrow one list block by: a value per filter and search text."""
    key: tuple[Any, ...]
    conditions: list[Any]


def _state_text(path: list[str]) -> Any:
    # The path was validated as declared identifiers; values are bound parameters.
    return func.jsonb_extract_path_text(WorkflowProject.state, *path)


def _block_narrowing(block: dict[str, Any], chosen: dict[str, str] | None, query: str | None) -> _Narrowing | None:
    conditions: list[Any] = []
    for index, configured in enumerate(block.get("filters") or []):
        value = (chosen or {}).get(str(index))
        if not isinstance(value, str) or not value or len(value) > 200:
            continue
        conditions.append(WorkflowProject.current_stage == value if configured.get("by") == "stage"
                          else _state_text(configured["field"]) == value)
    text = (query or "").strip()[:100]
    if text and block.get("search"):
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conditions.append(or_(*(_state_text(path).ilike(pattern, escape="\\") for path in block["search"]["fields"])))
    if not conditions:
        return None
    return _Narrowing(key=(tuple(sorted((chosen or {}).items())), text), conditions=conditions)


async def _filter_choices(db: Any, *, entity_id: str, workspace_id: str, source: dict[str, Any],
                          block: dict[str, Any]) -> list[dict[str, Any]]:
    """Each filter of a list block with the values its records hold (at most 30), in its labels' words."""
    base = [WorkflowProject.entity_id == entity_id, WorkflowProject.workspace_id == workspace_id,
            WorkflowProject.project_type == source["project_type"],
            WorkflowProject.schema_version.in_(source["schema_versions"])]
    if source.get("stages"):
        base.append(WorkflowProject.current_stage.in_(source["stages"]))
    if source.get("exclude_stages"):
        base.append(WorkflowProject.current_stage.not_in(source["exclude_stages"]))
    choices = []
    for index, configured in enumerate(block.get("filters") or []):
        column = WorkflowProject.current_stage if configured.get("by") == "stage" else _state_text(configured["field"])
        values = (await db.execute(select(column).where(*base, column.is_not(None), column != "")
                                   .distinct().order_by(column).limit(30))).scalars().all()
        labels = (block.get("stage_labels") or {}) if configured.get("by") == "stage" else next(
            (field.get("labels") or {} for field in block.get("fields") or []
             if isinstance(field, dict) and field.get("field") == configured.get("field")), {})
        choices.append({"key": str(index), "label": configured["label"],
                        "options": [{"value": value, "label": labels.get(value, value)} for value in values]})
    return choices


async def _projects(
    db: Any, *, entity_id: str, workspace_id: str, source: dict[str, Any], parent_id: str | None = None,
    after_id: str | None = None, limit: int = 101, descending: bool = False, project_ids: set[str] | None = None,
    narrow: list[Any] | None = None,
) -> list[WorkflowProject]:
    filters = _project_filters(entity_id, workspace_id, source, narrow)
    if project_ids is not None:
        if not project_ids:
            return []
        filters.append(WorkflowProject.id.in_(project_ids))
    if parent_id:
        # The path has been structurally validated. PostgreSQL JSON access is
        # intentionally kept server-side and never supplied by the caller.
        path = source.get("parent_state_path", ["parent_project_id"])
        expression: Any = WorkflowProject.state
        for key in path:
            expression = expression[key]
        filters.append(expression.astext == parent_id)
    if source.get("order"):
        query = _apply_project_order(select(WorkflowProject).where(*filters), WorkflowProject, source, after_id)
    else:
        if after_id:
            filters.append(WorkflowProject.id < after_id if descending else WorkflowProject.id > after_id)
        query = select(WorkflowProject).where(*filters).order_by(WorkflowProject.id.desc() if descending else WorkflowProject.id.asc())
    result = await db.execute(query.limit(max(1, min(limit, 101))))
    return list(result.scalars())


async def _related_projects(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], source_name: str, source: dict[str, Any],
    context: dict[str, str], *, after_id: str | None, limit: int, only_id: str | None = None, descending: bool = False,
) -> list[WorkflowProject]:
    """Read a declared project relation chain with a leaf-owned keyset cursor.

    A related source may have another related source as its parent.  Resolving
    every parent in Python made a child cursor affect the parent query, which
    could omit later siblings.  The configured relation paths are safe to join
    server-side, and the cursor remains the leaf project id presented to App
    clients.
    """
    chain_names = [source_name]
    chain = [source]
    while chain[-1]["type"] == "related_projects":
        parent_name = chain[-1]["parent"]
        parent = sources[parent_name]
        if parent["type"] not in {"workflow_projects", "related_projects"}:
            raise AppDataError("invalid_source", "A related project source requires a project parent.")
        chain.append(parent)
        chain_names.append(parent_name)
    chain.reverse()
    chain_names.reverse()
    rows = [aliased(WorkflowProject) for _ in chain]
    query = select(rows[-1])
    for index in range(len(rows) - 1, 0, -1):
        child, parent, child_source = rows[index], rows[index - 1], chain[index]
        expression: Any = child.state
        for key in child_source["parent_state_path"]:
            expression = expression[key]
        query = query.join(parent, expression.astext == parent.id)
    filters = []
    for row, config in zip(rows, chain):
        filters.extend((
            row.entity_id == user.entity_id,
            row.workspace_id == workspace.id,
            row.project_type == config["project_type"],
            row.schema_version.in_(config["schema_versions"]),
        ))
        if config.get("stages"):
            filters.append(row.current_stage.in_(config["stages"]))
        if config.get("exclude_stages"):
            filters.append(row.current_stage.not_in(config["exclude_stages"]))
    root = rows[0]
    if context["kind"] == "project":
        context_source = context.get("source")
        if context_source is None:
            filters.append(root.id == context["id"])
        elif context_source not in chain_names:
            raise AppDataError("invalid_context", "The selected project is outside this component source.")
        else:
            filters.append(rows[chain_names.index(context_source)].id == context["id"])
    elif context["kind"] == "launch_run":
        projects = await _verified_launch_projects(db, workspace, user, chain[0], context["id"])
        filters.append(root.id.in_([project.id for project in projects]))
    if only_id:
        filters.append(rows[-1].id == only_id)
    elif after_id and not source.get("order"):
        filters.append(rows[-1].id < after_id if descending else rows[-1].id > after_id)
    query = query.where(and_(*filters))
    if source.get("order"):
        query = _apply_project_order(query, rows[-1], source, after_id)
    else:
        query = query.order_by(rows[-1].id.desc() if descending else rows[-1].id.asc())
    result = await db.execute(query.limit(max(1, min(limit, 101))))
    return list(result.scalars())


async def _validate_component_context(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], component: dict[str, Any],
    context_kind: str, context_id: str,
) -> None:
    if context_kind == "workspace":
        if context_id != workspace.id:
            raise AppDataError("context_not_found", "The selected workspace is not available.")
        return
    source = sources.get(component["context"].get("source"))
    if source is None:
        raise AppDataError("invalid_component", "The component context is not configured.")
    if context_kind == "launch_run":
        await _verified_launch_projects(db, workspace, user, source, context_id)
    elif source["type"] == "workflow_projects":
        await _resolve_project_context(db, workspace, user, source, context_id)
    elif not await _related_projects(db, workspace, user, sources, component["context"]["source"], source, {"kind": "workspace", "id": workspace.id},
                                     after_id=None, limit=1, only_id=context_id):
        raise AppDataError("context_not_found", "The selected project is not available.")


async def _unmade_producer_runs(db: Any, workspace: Any, user: Any, source: dict[str, Any]) -> list[dict[str, Any]]:
    """Runs of a project source's producer Flows still at work and not yet holding their project.

    Until a Flow's create step succeeds there is no project to list, so the
    run itself stands in for it ("a campaign is being prepared"). A run holds
    its project once a project of the source's type, made since it started,
    is named in its variables.
    """
    definition_ids = [ref.removeprefix("workflow_definition:") for ref in source.get("producer_workflows") or []]
    if source.get("type") != "workflow_projects" or not definition_ids:
        return []
    runs = list((await db.execute(select(WorkflowRun).options(*_app_run_options()).where(
        WorkflowRun.entity_id == user.entity_id, WorkflowRun.workspace_id == workspace.id,
        WorkflowRun.workflow_id.in_(definition_ids),
        WorkflowRun.status.in_(("pending", "running", "paused")),
        WorkflowRun.trigger_data["parent_run_id"].as_string().is_(None),
    ).order_by(WorkflowRun.created_at.desc()).limit(5))).scalars())
    if not runs:
        return []
    oldest = min(run.created_at for run in runs if run.created_at)
    made = [row.id for row in (await db.execute(select(WorkflowProject.id).where(
        WorkflowProject.entity_id == user.entity_id, WorkflowProject.workspace_id == workspace.id,
        WorkflowProject.project_type == source.get("project_type"), WorkflowProject.created_at >= oldest,
    ))).all()] if source.get("project_type") else []
    # Which runs already name a made project is decided in the database: the variables stay there.
    holding = set((await db.execute(select(WorkflowRun.id).where(
        WorkflowRun.id.in_([run.id for run in runs]),
        or_(*(cast(WorkflowRun.variables, Text).contains(project_id) for project_id in made)),
    ))).scalars()) if made else set()
    unmade = [run for run in runs if run.id not in holding]
    progress = await _running_progress(db, user.entity_id, [run.id for run in unmade])
    await _load_run_columns(db, unmade, "definition_snapshot")

    def step_name(run: Any) -> str | None:
        nodes = ((run.definition_snapshot or {}).get("nodes") or []) if isinstance(run.definition_snapshot, dict) else []
        node = next((node for node in nodes if isinstance(node, dict) and node.get("id") == run.current_step_id), None)
        return str(node.get("name") or "").strip() or None if node else None

    return [{"run_id": run.id, "workflow_id": run.workflow_id, "binding_id": run.binding_id, "status": run.status,
             "step": step_name(run), "started_at": _iso(run.created_at),
             **({"progress_percent": progress[run.id]} if run.id in progress else {})} for run in unmade]


async def component_contexts(
    db: Any, workspace: Any, *, user: Any, component_key: str, cursor: str | None = None, limit: int = 50,
    locale: str | None = None,
) -> dict[str, Any]:
    """The contexts one component declares, named in the viewer's ``locale``."""
    config = normalize_app_view_config((workspace.operating_model or {}).get("app_view"))
    try:
        validate_app_view_data_contract(config["data_sources"], config["components"],
                                        catalog=await load_workspace_app_data_catalog(db, workspace))
    except ValueError as exc:
        raise AppDataError("invalid_component", "The component configuration is no longer valid.") from exc
    config = localized(config, locale)
    component = next((item for item in config["components"] if item["key"] == component_key), None)
    if component is None:
        raise AppDataError("component_not_found", "This App component is not available.")
    context = component["context"]
    kind = context["kind"]
    if kind == "workspace":
        return {"component": {"key": component["key"], "title": component.get("title"), "context": context},
                "status": "ready", "items": [{"kind": "workspace", "id": workspace.id, "label": workspace.name,
                                                    "status": workspace.status}], "next_cursor": None, "errors": []}
    source = config["data_sources"].get(context.get("source"))
    if source is None:
        raise AppDataError("invalid_component", "The component context is not configured.")
    stage, _, after = (cursor or "p:").partition(":")
    if stage not in {"p", "l"}:
        raise AppDataError("invalid_cursor", "The component cursor is invalid.")
    eligible_projects: set[str] | None = None
    if component.get("visibility"):
        if stage != "p":
            raise AppDataError("invalid_cursor", "This component only lists eligible projects.")
        gate = next(block for block in component["blocks"] if block["key"] == component["visibility"]["block"])
        if component["visibility"]["when"] == "has_observations":
            eligible, ancestors, _ = await _observed_publications(
                db, workspace, user, config["data_sources"], gate, {"kind": "workspace", "id": workspace.id}, {},
            )
        else:
            eligible, ancestors = await _eligible_records(
                db, workspace, user, config["data_sources"], gate, {"kind": "workspace", "id": workspace.id}, {},
            )
        eligible_projects = {
            ancestor[context["source"]]["id"]
            for row in eligible
            if isinstance((ancestor := ancestors.get(row["id"])), dict)
            and isinstance(ancestor.get(context["source"]), dict)
            and isinstance(ancestor[context["source"]].get("id"), str)
        }
    binding_ids = {item["workflow"].removeprefix("workflow:") for item in source.get("launch_workflows", [])}
    if eligible_projects is not None:
        binding_ids = set()
    async def launch_rows(after_id: str | None, count: int) -> list[WorkflowRun]:
        if not binding_ids:
            return []
        query = select(WorkflowRun).options(*_app_run_options()).where(
            WorkflowRun.entity_id == user.entity_id, WorkflowRun.workspace_id == workspace.id,
            WorkflowRun.binding_id.in_(binding_ids))
        if after_id:
            query = query.where(WorkflowRun.id < after_id)
        return list((await db.execute(query.order_by(WorkflowRun.id.desc()).limit(count))).scalars())
    bindings = {
        binding.id: binding.name
        for binding in (await db.execute(select(WorkflowBinding).where(
            WorkflowBinding.entity_id == user.entity_id,
            WorkflowBinding.workspace_id == workspace.id,
            WorkflowBinding.id.in_(binding_ids),
        ))).scalars()
    } if binding_ids else {}

    async def pending_launches(after_id: str | None, count: int) -> list[WorkflowRun]:
        """Skip materialized runs without letting them consume a result page."""
        pending: list[WorkflowRun] = []
        raw_cursor = after_id
        scanned = 0
        while len(pending) < count:
            batch = await launch_rows(raw_cursor, 101)
            if not batch:
                break
            for run in batch:
                if not await _verified_launch_projects(db, workspace, user, source, run.id):
                    pending.append(run)
                    if len(pending) >= count:
                        break
            raw_cursor = batch[-1].id
            scanned += len(batch)
            if scanned > 10_000:
                raise AppDataError("source_too_large", "The configured launch source exceeds the App read limit.")
            if len(batch) < 101:
                break
        # The label names the Flow from its frozen definition, read only for the runs shown.
        await _load_run_columns(db, pending, "definition_snapshot")
        return pending
    if kind == "project":
        if eligible_projects is not None:
            rows = await _projects(db, entity_id=user.entity_id, workspace_id=workspace.id, source=source,
                                   after_id=after or None, limit=limit + 1, descending=True, project_ids=eligible_projects)
        elif stage == "l":
            rows = []
        elif source["type"] == "related_projects":
            rows = await _related_projects(db, workspace, user, config["data_sources"], context["source"], source,
                                           {"kind": "workspace", "id": workspace.id}, after_id=after or None,
                                           limit=limit + 1, descending=True)
        else:
            rows = await _projects(db, entity_id=user.entity_id, workspace_id=workspace.id, source=source,
                                   after_id=after or None, limit=limit + 1, descending=True)
        executions = await _project_executions(
            db, entity_id=user.entity_id, workspace_id=workspace.id, rows=rows, source=source,
        )
        items = [{"kind": "project", "id": row.id,
                  "label": (shown := _project(row, source["fields"], source.get("label_fields")))["label"],
                  "status": row.current_stage, "project_type": row.project_type,
                  **({"status_label": shown["display"]["status"]} if shown["display"].get("status") else {}),
                  "execution": executions.get(row.id), "parent_id": _parent_id(row, source),
                  "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at)} for row in rows]

        async def present_contexts(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
            summaries = await _calendar_context_summaries(
                db, workspace, user, config["data_sources"], component,
                [row for row in rows if row.id in {item["id"] for item in selected if item["kind"] == "project"}],
            )
            for item in selected:
                if item["kind"] == "project" and item["id"] in summaries:
                    item["summary"] = summaries[item["id"]]
            return selected


        if context.get("display") == "cards":
            await _context_cards(db, workspace, user, config["data_sources"], component, rows[:limit], items)
        # Before a create/claim step succeeds there is no project context. The
        # declared launch binding remains a safe, explicitly typed context.
        if stage == "p" and len(items) > limit:
            selected = await present_contexts(items[:limit])
            return {"component": {"key": component["key"], "title": component.get("title"), "context": context},
                    "status": "ready", "items": selected, "next_cursor": f"p:{selected[-1]['id']}", "errors": [],
                    **({"making": making} if (making := await _unmade_producer_runs(db, workspace, user, source))
                       and not after else {})}
        if binding_ids:
            launches = await pending_launches(after or None if stage == "l" else None, limit - len(items) + 1)
            for run in launches:
                items.append({"kind": "launch_run", "id": run.id,
                              "label": _launch_label(run, bindings.get(run.binding_id), locale), "status": run.status,
                              "updated_at": _iso(run.updated_at), "href": f"/flows/{run.workflow_id}/runs/{run.id}"})
        selected = await present_contexts(items[:limit])
        more = len(items) > limit
        if more and selected and selected[-1]["kind"] == "launch_run":
            next_cursor = f"l:{selected[-1]['id']}"
        elif stage == "p" and binding_ids and selected and selected[-1]["kind"] == "project":
            # A full project page must transition through the launch stage,
            # even when the first eligible launch did not fit this response.
            next_cursor = f"p:{selected[-1]['id']}"
        else:
            next_cursor = None
        # What is being made shows first, until its project exists.
        making = await _unmade_producer_runs(db, workspace, user, source) if stage == "p" and not after else []
        return {"component": {"key": component["key"], "title": component.get("title"), "context": context},
                "status": "ready" if selected or making else "empty", "items": selected, "next_cursor": next_cursor,
                "errors": [], **({"making": making} if making else {})}
    # Launch run contexts are intentionally a limited projection of declared
    # public launch bindings.  They never scan arbitrary workspace runs.
    if not binding_ids:
        return {"component": {"key": component["key"], "title": component.get("title"), "context": context},
                "status": "empty", "items": [], "next_cursor": None, "errors": []}
    rows = await pending_launches(after or None, limit + 1)
    items = [{"kind": "launch_run", "id": row.id, "label": _launch_label(row, bindings.get(row.binding_id), locale), "status": row.status,
              "updated_at": _iso(row.updated_at), "created_at": _iso(row.created_at)} for row in rows]
    selected = items[:limit]
    next_cursor = f"l:{selected[-1]['id']}" if len(items) > limit and selected else None
    return {"component": {"key": component["key"], "title": component.get("title"), "context": context},
            "status": "ready" if selected else "empty", "items": selected, "next_cursor": next_cursor, "errors": []}


async def _resolve_project_context(
    db: Any, workspace: Any, user: Any, source: dict[str, Any], context_id: str,
) -> WorkflowProject:
    row = await db.scalar(select(WorkflowProject).where(
        WorkflowProject.id == context_id, WorkflowProject.entity_id == user.entity_id,
        WorkflowProject.workspace_id == workspace.id, WorkflowProject.project_type == source["project_type"],
        WorkflowProject.schema_version.in_(source["schema_versions"]),
    ))
    if row is None:
        raise AppDataError("context_not_found", "The selected project is not available.")
    return row


async def _verified_launch_projects(
    db: Any, workspace: Any, user: Any, source: dict[str, Any], run_id: str,
) -> list[WorkflowProject]:
    """Project a launch run only from its frozen native creation/claim result."""
    binding_ids = {item["workflow"].removeprefix("workflow:") for item in source.get("launch_workflows", [])}
    run = await db.scalar(select(WorkflowRun).where(
        WorkflowRun.id == run_id, WorkflowRun.entity_id == user.entity_id,
        WorkflowRun.workspace_id == workspace.id, WorkflowRun.binding_id.in_(binding_ids),
    ))
    if run is None:
        raise AppDataError("context_not_found", "The selected launch run is not available.")
    steps = {str(step.get("id")): step for step in (run.definition_snapshot or {}).get("steps", [])
             if isinstance(step, dict) and isinstance(step.get("id"), str)}
    expected_steps = {step for launch in source.get("launch_workflows", []) for step in launch["project_steps"]}
    project_ids: set[str] = set()
    for step_id in expected_steps:
        step = steps.get(step_id)
        result = (run.step_results or {}).get(step_id)
        config = step.get("config") if isinstance(step, dict) else None
        if (not isinstance(step, dict) or step.get("type") != "workflow_project" or not isinstance(config, dict)
                or config.get("operation") not in {"create", "claim"} or config.get("project_type") != source["project_type"]
                or config.get("schema_version") not in source["schema_versions"]
                or not isinstance(result, dict) or result.get("status") != "completed"):
            continue
        output = result.get("output")
        if not isinstance(output, dict) or output.get("project_type") != source["project_type"] or output.get("schema_version") not in source["schema_versions"]:
            continue
        candidate = output.get("project_id")
        if isinstance(candidate, str):
            project_ids.add(candidate)
    return list((await db.execute(select(WorkflowProject).where(
        WorkflowProject.id.in_(project_ids), WorkflowProject.entity_id == user.entity_id,
        WorkflowProject.workspace_id == workspace.id, WorkflowProject.project_type == source["project_type"],
        WorkflowProject.schema_version.in_(source["schema_versions"]),
    ))).scalars()) if project_ids else []


async def _source_rows(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], name: str, context: dict[str, str], cache: dict[str, Any],
    *, after_id: str | None = None, page_limit: int = 101, window: str | None = None,
    narrowing: "_Narrowing | None" = None,
) -> list[dict[str, Any]]:
    cache_key = (name, after_id, page_limit, window, narrowing.key if narrowing else None)
    if cache_key in cache:
        return cache[cache_key]
    source = sources.get(name)
    if source is None:
        raise AppDataError("source_not_found", "The component source is not configured.", source=name)
    kind = source["type"]

    if kind == "workflow_projects":
        if context["kind"] == "project":
            rows = [await _resolve_project_context(db, workspace, user, source, context["id"])]
        elif context["kind"] == "launch_run":
            rows = await _verified_launch_projects(db, workspace, user, source, context["id"])
        else:
            rows = await _projects(db, entity_id=user.entity_id, workspace_id=workspace.id, source=source,
                                   after_id=after_id, limit=page_limit,
                                   narrow=narrowing.conditions if narrowing else None)
        executions = await _project_executions(
            db, entity_id=user.entity_id, workspace_id=workspace.id, rows=rows, source=source,
        )
        result = [_project(row, source["fields"], source.get("label_fields"), executions.get(row.id), source) for row in rows]
    elif kind == "workflow_results":
        result = await _workflow_result_rows(db, workspace, user, source, limit=page_limit)
    elif kind == "related_projects":
        rows = await _related_projects(db, workspace, user, sources, name, source, context,
                                       after_id=after_id, limit=page_limit)
        executions = await _project_executions(
            db, entity_id=user.entity_id, workspace_id=workspace.id, rows=rows, source=source,
        )
        result = [_project(row, source["fields"], source.get("label_fields"), executions.get(row.id), source) for row in rows]
    elif kind == "project_records":
        parent_after, record_offset = _parent_cursor(after_id)
        parents = await _source_rows(db, workspace, user, sources, source["parent"], context, cache,
                                     after_id=parent_after, page_limit=page_limit, window=window)
        result = _SourceRows()
        parent_ids = [row.get("id") for row in parents if isinstance(row.get("id"), str)]
        parent_rows = list((await db.execute(select(WorkflowProject).where(
            WorkflowProject.id.in_(parent_ids), WorkflowProject.entity_id == user.entity_id,
            WorkflowProject.workspace_id == workspace.id,
        ))).scalars()) if parent_ids else []
        states = {row.id: row.state or {} for row in parent_rows}
        for parent in parents:
            parent_id = parent.get("id")
            parent_cursor = parent.get("_cursor", parent_id)
            raw = _value_at(states.get(parent_id, {}), source["record_path"])
            schema = source.get("record_schema")
            # One versioned record, or (without a schema) each item of a plain list.
            records = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) and schema is None else []
            # A plain list item was not made by the project's last run; only a
            # versioned record (a receipt) is that run's result.
            result.extend({"id": f"{parent_id}:{index}", "_cursor": f"{parent_cursor}|{index}", "project_id": parent_id, "schema": schema, "record": record,
                           "fields": _flat_record_fields(record), "execution": parent.get("execution") if schema else None}
                          for index, record in enumerate(records) if isinstance(record, dict)
                          and (schema is None or record.get("schema_version") == schema)
                          and (schema != "publication-receipt/v1" or record.get("verification_status") == "verified")
                          and not (parent_cursor == parent_after and index <= record_offset))
        if len(parents) >= page_limit and len(result) <= page_limit:
            result.next_cursor = f"{parents[-1].get('_cursor', parents[-1]['id'])}|-1"
    elif kind == "project_documents":
        parent_after, _ = _parent_cursor(after_id)
        parents = await _source_rows(db, workspace, user, sources, source["parent"], context, cache,
                                     after_id=parent_after, page_limit=page_limit, window=window)
        parent_ids = [row.get("id") for row in parents if isinstance(row.get("id"), str)]
        parent_rows = list((await db.execute(select(WorkflowProject).where(
            WorkflowProject.id.in_(parent_ids), WorkflowProject.entity_id == user.entity_id,
            WorkflowProject.workspace_id == workspace.id,
        ))).scalars()) if parent_ids else []
        states = {row.id: row.state or {} for row in parent_rows}
        ids = [_value_at(states.get(parent.get("id"), {}), source["document_path"]) for parent in parents]
        docs = list((await db.execute(select(Document).where(Document.entity_id == user.entity_id,
                    Document.id.in_([doc_id for doc_id in ids if isinstance(doc_id, str)]), Document.is_trashed.is_(False)))).scalars()) if any(ids) else []
        access = await DocumentAccessContext.load(db, entity_id=user.entity_id, user_id=user.id, role=user.role, document_ids={doc.id for doc in docs})
        await access.preload_documents(db, docs)
        by_id = {doc.id: doc for doc in docs}
        result = _SourceRows()
        for parent, document_id in zip(parents, ids):
            doc = by_id.get(document_id)
            if doc is not None and await access.can_read_document(db, doc, workspace_id=workspace.id):
                result.append({"id": doc.id, "_cursor": f"{parent.get('_cursor', parent['id'])}|0", "name": doc.name, "mime_type": doc.mime_type,
                               "file_size": doc.file_size, "viewer_url": f"/viewer/{doc.id}",
                               "created_at": _iso(doc.created_at), "role": source.get("document_role")})
        if len(parents) >= page_limit and len(result) <= page_limit:
            result.next_cursor = f"{parents[-1].get('_cursor', parents[-1]['id'])}|-1"
    elif kind in {"project_files", "project_references"}:
        parent_after, offset = _parent_cursor(after_id)
        parents = await _source_rows(db, workspace, user, sources, source["parent"], context, cache,
                                     after_id=parent_after, page_limit=page_limit, window=window)
        parent_ids = [row.get("id") for row in parents if isinstance(row.get("id"), str)]
        projects = {row.id: row for row in (await db.execute(select(WorkflowProject).where(
            WorkflowProject.id.in_(parent_ids), WorkflowProject.entity_id == user.entity_id,
            WorkflowProject.workspace_id == workspace.id,
        ))).scalars()} if parent_ids else {}
        reader = _project_file_rows if kind == "project_files" else _project_reference_rows
        result = await reader(db, workspace, user, source, parents, projects,
                              resume=(parent_after, offset), limit=page_limit)
        if len(parents) >= page_limit and len(result) <= page_limit:
            result.next_cursor = f"{parents[-1].get('_cursor', parents[-1]['id'])}|-1"
    elif kind in {"workspace_stats", "publication_stats"}:
        stat_query = select(WorkspaceStat).where(
            WorkspaceStat.entity_id == user.entity_id,
            WorkspaceStat.workspace_id == workspace.id,
        )
        if kind == "workspace_stats":
            stat_query = stat_query.where(WorkspaceStat.key.in_(source["stat_keys"]))
        if kind == "publication_stats":
            records: list[dict[str, Any]] = []
            receipt_cursor: str | None = None
            # A publication stat may refer to any receipt in this component's
            # declared scope. Do not silently lose posts after the App page.
            while True:
                receipt_page = await _source_rows(
                    db, workspace, user, sources, source["parent"], context, cache,
                    after_id=receipt_cursor, page_limit=101, window=window,
                )
                records.extend(receipt_page)
                next_receipt_cursor = getattr(receipt_page, "next_cursor", None) or (
                    receipt_page[-1].get("_cursor") if receipt_page else None
                )
                if not next_receipt_cursor or next_receipt_cursor == receipt_cursor:
                    break
                receipt_cursor = next_receipt_cursor
                if len(records) > 10_000:
                    raise AppDataError("source_too_large", "The configured publication source exceeds the App read limit.", source=name)
            ancestors = await _record_ancestor_states(db, workspace, user, sources, source["parent"], records)
            published = [
                (row, str(ancestors.get(row.get("id"), {}).get(source["subject"], {}).get("id") or ""))
                for row in records if isinstance(row.get("record"), dict)
            ]
            subject_ids = {subject_id for _, subject_id in published if subject_id}
            def matches_receipt(stat: WorkspaceStat) -> bool:
                return any(_stat_matches_publication(stat, subject_id=subject_id, record=row["record"])
                           for row, subject_id in published if subject_id)
            stat_query = stat_query.where(
                WorkspaceStat.collector_type.in_(_publication_collector_types(source)),
                WorkspaceStat.collector_config["metric_key"].astext.in_(source["metric_keys"]),
                WorkspaceStat.collector_config["subject"]["kind"].astext == "workflow_project",
                WorkspaceStat.collector_config["subject"]["id"].astext.in_(subject_ids),
            )
        stat_cursor = after_id
        stats: list[WorkspaceStat] = []
        scanned = 0
        while len(stats) < page_limit:
            query = stat_query.where(WorkspaceStat.id > stat_cursor) if stat_cursor else stat_query
            batch = list((await db.execute(query.order_by(WorkspaceStat.id).limit(101))).scalars())
            if not batch:
                break
            stats.extend(stat for stat in batch if kind != "publication_stats" or matches_receipt(stat))
            stat_cursor = batch[-1].id
            scanned += len(batch)
            if scanned > 10_000:
                raise AppDataError("source_too_large", "The configured metric source exceeds the App read limit.", source=name)
            if len(batch) < 101:
                break
        stats = stats[:page_limit]
        stat_ids = [stat.id for stat in stats]
        days = {"7d": 7, "30d": 30, "90d": 90}.get(window or "7d", 7)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        by_stat: dict[str, list[Any]] = {stat_id: [] for stat_id in stat_ids}
        # Each Stat receives a bounded current series plus bounded candidate
        # baselines. This is enough for the shared comparison contract without
        # scanning every historical observation in the Workspace.
        for stat_id in stat_ids:
            current = list((await db.execute(select(WorkspaceStatObservation).where(
                WorkspaceStatObservation.entity_id == user.entity_id,
                WorkspaceStatObservation.workspace_id == workspace.id,
                WorkspaceStatObservation.stat_id == stat_id,
                WorkspaceStatObservation.observed_at >= cutoff,
            ).order_by(WorkspaceStatObservation.observed_at.desc(), WorkspaceStatObservation.id.desc()).limit(100))).scalars())
            baseline = list((await db.execute(select(WorkspaceStatObservation).where(
                WorkspaceStatObservation.entity_id == user.entity_id,
                WorkspaceStatObservation.workspace_id == workspace.id,
                WorkspaceStatObservation.stat_id == stat_id,
                WorkspaceStatObservation.observed_at < cutoff,
            ).order_by(WorkspaceStatObservation.observed_at.desc(), WorkspaceStatObservation.id.desc()).limit(100))).scalars())
            by_stat[stat_id] = current + baseline
        result = [{"id": stat.id, "_cursor": stat.id, "key": stat.key, "name": stat.name, "unit": stat.unit, "status": stat.status,
                   "description": stat.description, "window": stat.window,
                   "current_value": float(stat.current_value) if stat.current_value is not None else None,
                   "value_type": stat.value_type, "collection_status": stat.last_collection_status,
                   "collection_error": stat.last_collection_error,
                   "freshness": stat_freshness_status(stat),
                   "metric_key": (stat.collector_config or {}).get("metric_key"),
                   "platform": (stat.collector_config or {}).get("platform"), "public_url": (stat.collector_config or {}).get("public_url"),
                   "updated_at": _iso(stat.current_value_updated_at),
                   "_stat": stat, "_observations": by_stat[stat.id],
                   "observations": [{"id": obs.id, "value": float(obs.value) if obs.value is not None else None,
                                     "observed_at": _iso(obs.observed_at), "window_start": _iso(obs.window_start),
                                     "window_end": _iso(obs.window_end)} for obs in by_stat[stat.id][:100]]}
                  for stat in stats]
    elif kind == "owned_automations":
        parents = await _source_rows(db, workspace, user, sources, source["parent"], context, cache, window=window)
        if len(parents) >= 101:
            raise AppDataError("source_too_large", "The configured automation parent source exceeds the App read limit.", source=name)
        project_ids = [parent["id"] for parent in parents]
        job_query = select(ScheduledJob).where(
            ScheduledJob.entity_id == user.entity_id,
            ScheduledJob.workspace_id == workspace.id,
            ScheduledJob.owner_project_id.in_(project_ids),
        )
        if after_id:
            job_query = job_query.where(ScheduledJob.id > after_id)
        jobs = list((await db.execute(job_query.order_by(ScheduledJob.id).limit(page_limit))).scalars()) if project_ids else []
        binding_ids = {
            target.get("binding_id")
            for job in jobs
            if isinstance((target := job.execution_target), dict) and isinstance(target.get("binding_id"), str)
        }
        workflow_ids = {
            target.get("workflow_id")
            for job in jobs
            if isinstance((target := job.execution_target), dict) and isinstance(target.get("workflow_id"), str)
        }
        binding_references = []
        if binding_ids:
            binding_references.append(WorkflowBinding.id.in_(binding_ids))
        if workflow_ids:
            binding_references.append(WorkflowBinding.workflow_id.in_(workflow_ids))
        bindings = list((await db.execute(select(WorkflowBinding).where(
                WorkflowBinding.entity_id == user.entity_id,
                WorkflowBinding.workspace_id == workspace.id,
                or_(*binding_references),
            ))).scalars()) if binding_references else []
        binding_names = {
            binding.id: binding.name.strip()
            for binding in bindings if isinstance(binding.name, str) and binding.name.strip()
        }
        workflow_binding_name_candidates: dict[str, set[str]] = {}
        for binding in bindings:
            name = binding_names.get(binding.id)
            if not name:
                continue
            workflow_binding_name_candidates.setdefault(binding.workflow_id, set()).add(name)
        binding_names_by_workflow = {
            workflow_id: next(iter(names))
            for workflow_id, names in workflow_binding_name_candidates.items()
            if len(names) == 1
        }
        result = []
        for job in jobs:
            name = job.name.strip() if isinstance(job.name, str) else ""
            target = job.execution_target if isinstance(job.execution_target, dict) else {}
            binding_name = binding_names.get(target.get("binding_id"))
            if not binding_name and not target.get("binding_id"):
                binding_name = binding_names_by_workflow.get(target.get("workflow_id"))
            generated_name = not name or name == job.job_id or bool(_TECHNICAL_SLUG.fullmatch(name))
            label = binding_name if binding_name and generated_name else name or job.job_id
            ended = bool(not job.enabled and job.stop_after and job.stop_after <= datetime.now(UTC))
            result.append({"id": job.id, "_cursor": job.id, "job_id": job.job_id, "name": job.name, "label": label,
                           "enabled": job.enabled, "state": "enabled" if job.enabled else "ended" if ended else "disabled",
                           "last_status": job.last_status, "last_run_at": _iso(job.last_run_at),
                           "next_run_at": _iso(job.next_run_at), "stop_after": _iso(job.stop_after)})
    else:  # guarded by normalization; retain a structured response at runtime.
        raise AppDataError("unsupported_source", "The App source type is not supported.", source=name)
    cache[cache_key] = result
    return result


# How many earlier versions of one result its card keeps (``versions: true``).
_RESULT_VERSION_LIMIT = 9
# How a result's latest attempt reads on its card; a failed or cancelled attempt
# shows the version before it, or nothing when there is none.
_RESULT_STATES = {"pending": "queued", "running": "running", "paused": "needs_you", "completed": "completed"}


async def _running_progress(db: Any, entity_id: str, run_ids: list[str]) -> dict[str, int]:
    """How far each unfinished run is, in whole percent (never 100 while it runs).

    Stages done against the longest route through the Flow, the figure its run
    history shows; the step working now earns its share as its media jobs
    (clips, songs, images) finish, so a long generation step keeps moving.
    """
    if not run_ids:
        return {}
    from packages.core.models.media_job import MediaJob
    from packages.core.services.workflow_run_trace import workflow_execution_steps, workflow_run_stage_counts

    runs = list((await db.execute(select(WorkflowRun).options(*_app_run_options()).where(
        WorkflowRun.entity_id == entity_id, WorkflowRun.id.in_(run_ids),
    ))).scalars())
    # The stage figure reads the route and the trace. A failure to read them is swallowed below,
    # so they are loaded here, explicitly, rather than left to read as empty.
    await _load_run_columns(db, runs, "definition_snapshot", "execution_trace", "step_results")
    run_key = MediaJob.params["workflow_run_id"].as_string()
    jobs: dict[str, list[int]] = {}
    for run_id, status, count in (await db.execute(select(run_key, MediaJob.status, func.count()).where(
        MediaJob.entity_id == entity_id, run_key.in_(run_ids),
    ).group_by(run_key, MediaJob.status))).all():
        done_total = jobs.setdefault(run_id, [0, 0])
        done_total[1] += count
        if status in ("completed", "failed"):
            done_total[0] += count
    progress = {}
    for run in runs:
        try:
            counts = workflow_run_stage_counts(run, workflow_execution_steps(run))
        except Exception:  # a run whose trace cannot be read shows no figure, not an error
            continue
        total = counts.get("total_count") or 0
        if total <= 0:
            continue
        done, made = jobs.get(run.id, [0, 0])
        share = done / made if made else 0.0
        progress[run.id] = max(1, min(99, int(100 * (min(counts.get("processed_count") or 0, total) + share) / (total + 1))))
    return progress


async def _workflow_result_rows(
    db: Any, workspace: Any, user: Any, source: dict[str, Any], *, limit: int,
) -> list[dict[str, Any]]:
    """The latest version of each result a Flow's runs made, newest first.

    One card per run family (a revision or retry is the same result), read
    from the run's own variables at the declared paths only; a file is shown
    when its Document still exists and the viewer may read it.
    """
    definition_ids = [ref.removeprefix("workflow_definition:") for ref in source["producer_workflows"]]
    paths = list(dict.fromkeys(tuple(path) for path in [
        *source.get("fields", []), *source.get("label_fields", []), *(entry["field"] for entry in source.get("media", []))]))
    columns = [WorkflowRun.variables[path].label(f"v{index}") for index, path in enumerate(paths)]
    rows = (await db.execute(select(
        WorkflowRun.id, WorkflowRun.workflow_id, WorkflowRun.binding_id, WorkflowRun.status, WorkflowRun.lineage_root_run_id,
        WorkflowRun.created_at, WorkflowRun.completed_at, *columns,
    ).where(
        WorkflowRun.entity_id == user.entity_id, WorkflowRun.workspace_id == workspace.id,
        WorkflowRun.workflow_id.in_(definition_ids),
        WorkflowRun.trigger_data["parent_run_id"].as_string().is_(None),
    ).order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()).limit(max(limit * 6, 60)))).all()

    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = families.setdefault(row.lineage_root_run_id or row.id, {"latest": row, "done": None, "finished": []})
        if row.status == "completed" and family["done"] is None:
            family["done"] = row
        if row.status == "completed":
            family["finished"].append(row)
    picked = []
    for family_id, family in families.items():
        latest, done = family["latest"], family["done"]
        state = _RESULT_STATES.get(latest.status)
        if state is None:
            if done is None:
                continue
            state = "completed"
        picked.append((family_id, latest, done or latest, state))
        if len(picked) >= limit:
            break
    # Each result's earlier finished versions, newest first, when the page shows its history.
    earlier: dict[str, list[Any]] = {
        family_id: [row for row in families[family_id]["finished"] if row.id != shown.id][:_RESULT_VERSION_LIMIT]
        for family_id, _latest, shown, _state in picked} if source.get("versions") else {}

    def value(row: Any, path: tuple[str, ...]) -> Any:
        return getattr(row, f"v{paths.index(path)}")

    def files(row: Any, entry: dict[str, Any]) -> list[dict[str, str]]:
        """The files one media path holds: a Document id, a ``{document_id}`` or a list of them."""
        held = value(row, tuple(entry["field"]))
        return [file for file in map(display_file, held if isinstance(held, list) else [held]) if file]

    readable = await _readable_media(db, workspace, user, {
        file["document_id"] for family_id, _latest, shown, _state in picked
        for run in [shown, *earlier.get(family_id, [])]
        for entry in source.get("media", []) for file in files(run, entry)})

    progress = await _running_progress(db, user.entity_id, [
        latest.id for _family, latest, _shown, state in picked if state in ("running", "queued")])
    result = []
    for family_id, latest, shown, state in picked:
        label = next((value(shown, tuple(path)) for path in source.get("label_fields", [])
                      if isinstance(value(shown, tuple(path)), str) and value(shown, tuple(path)).strip()), None)
        # A path holding several files (a run's three scene images) makes a
        # result of each; a single file goes with every one of them.
        shared, each = [], []
        for entry in source.get("media", []):
            found = [(file.get("label"), {**readable[file["document_id"]], **(
                {"label": file.get("label") or entry["label"]} if file.get("label") or entry.get("label") else {})})
                     for file in files(shown, entry) if file["document_id"] in readable]
            if len(found) > 1 and not each:
                each = found
            else:
                shared.extend(item for _own, item in found[:1])
        media = [*(item for _own, item in each[:1]), *shared]
        # A finished result whose declared files are all missing made nothing to show.
        if state == "completed" and source.get("media") and not media:
            continue
        fields = {".".join(path): value(shown, tuple(path)) for path in source.get("fields", [])}
        # So did one that ended without any declared value (it stopped to ask for input).
        if state == "completed" and not media and label is None and all(item in (None, "", [], {}) for item in fields.values()):
            continue
        row = {"id": family_id, "_cursor": family_id, "group": family_id, "run_id": shown.id, "workflow_id": shown.workflow_id,
               "binding_id": latest.binding_id,
               "label": label, "state": state, "media": media,
               "fields": fields,
               "updated_at": _iso(latest.completed_at or latest.created_at),
               **({"progress_percent": progress[latest.id]} if latest.id in progress else {})}
        if family_id in earlier:
            # Numbered from the first finished one (v1); the newest finished is the highest number.
            finished = families[family_id]["finished"]
            row["version"] = len(finished) - finished.index(shown) if shown in finished else len(finished) + 1
            row["versions"] = [{
                "run_id": run.id, "number": len(finished) - finished.index(run),
                "updated_at": _iso(run.completed_at or run.created_at),
                "media": [readable[file["document_id"]] for entry in source.get("media", [])
                          for file in files(run, entry) if file["document_id"] in readable][:4],
            } for run in earlier[family_id]]
        if len(each) < 2:
            result.append(row)
            continue
        for own_label, item in each:
            result.append({**row, "id": f"{family_id}:{item['document_id']}", "label": own_label or label,
                           "media": [item, *shared]})
    return result


async def _project_file_rows(
    db: Any, workspace: Any, user: Any, source: dict[str, Any], parents: list[dict[str, Any]],
    projects: dict[str, WorkflowProject], *, resume: tuple[str | None, int], limit: int,
) -> _SourceRows:
    """Readable files in each project's own folder, its subfolders included.

    Only the folder the runtime named for the project (``state.project_root``)
    is listed, and every file passes the normal Document access check. Each
    row carries its folder below the project root, so the App shows the same
    tree as the Workspace files.
    """
    from packages.core.services.generated_artifact_service import WORK_ARTIFACT_ROLES, is_work_artifact
    from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

    home = workspace_artifact_storage_base(getattr(workspace, "artifact_folder_id", None))
    result = _SourceRows()
    if not home:
        return result
    for parent in parents:
        project = projects.get(parent.get("id"))
        root = str(((project.state if project else None) or {}).get("project_root") or "").strip().strip("/")
        if not root or ".." in root.split("/"):
            continue
        prefix = f"{home}/{root}/"
        # The projects inside this one (episodes in a series), by their folder.
        child_root = WorkflowProject.state["project_root"].astext
        children = sorted((
            (str(child.state.get("project_root")).strip("/").removeprefix(root + "/"), child)
            for child in (await db.execute(select(WorkflowProject).where(
                WorkflowProject.entity_id == user.entity_id, WorkflowProject.workspace_id == workspace.id,
                child_root.startswith(root + "/", autoescape=True),
            ))).scalars()
        ), key=lambda pair: -len(pair[0]))
        parent_cursor = parent.get("_cursor", parent["id"])
        skip = resume[1] if parent_cursor == resume[0] else -1
        packs = {child.id: _pack_info(rel, child) for rel, child in children}
        # Where each child's current version lives: a remake records its take
        # (``production_root``) once accepted; until then the earlier one stays.
        current = {child.id: _current_production(root, rel, child) for rel, child in children}
        runs = await _latest_child_runs(db, user.entity_id, [child for _, child in children])
        for child_id, run in runs.items():
            packs[child_id]["run"] = run
        if parent_cursor != resume[0]:
            # Every child is a card from its start, with no file yet, so its
            # progress shows while it is being made.
            for rel, child in sorted(children):
                result.append({"id": f"pack:{child.id}", "_cursor": f"{parent_cursor}|-1", "name": "",
                               "folder": rel, "project_id": project.id, "role": "pack", "pack": packs[child.id]})
        query = select(Document).where(
            Document.entity_id == user.entity_id, Document.is_trashed.is_(False),
            Document.fs_path.startswith(prefix, autoescape=True),
        )
        # Finished files (the default): each project's own files that its state
        # names, or, where it names none, every file but the work files.
        finished = "results_only" not in source

        def owner_of(folder: str) -> str:
            return next((rel for rel, _child in children if folder == rel or folder.startswith(rel + "/")), "")

        named: dict[str, set[str]] = {}
        if finished:
            # A project names a file only when one of its folder's files is that id
            # (its state also holds project and run ids).
            candidates = _named_documents(project.state).union(*(_named_documents(child.state) for _rel, child in children))
            for doc_id, fs_path in (await db.execute(select(Document.id, Document.fs_path).where(
                    Document.entity_id == user.entity_id, Document.is_trashed.is_(False), Document.id.in_(candidates),
                    Document.fs_path.startswith(prefix, autoescape=True)))).all() if candidates else []:
                owner = owner_of(fs_path.removeprefix(prefix).rpartition("/")[0])
                state = project.state if owner == "" else next(child for rel, child in children if rel == owner).state
                if doc_id in _named_documents(state):
                    named.setdefault(owner, set()).add(doc_id)
        if finished and named.get("") and not children:
            query = query.where(Document.id.in_(named[""]))
        elif source.get("results_only") or (finished and not any(named.values())):
            # Leave working files out before paging: a production's frames and
            # clips would otherwise fill the page and push its results off it.
            role = Document.metadata_["artifact"]["role"].astext
            query = query.where(or_(role.is_(None), func.lower(role).not_in(sorted(WORK_ARTIFACT_ROLES))))
        docs = list((await db.execute(
            query.order_by(Document.fs_path).offset(skip + 1).limit(limit + 1))).scalars())
        access = await DocumentAccessContext.load(db, entity_id=user.entity_id, user_id=user.id, role=user.role,
                                                  document_ids={doc.id for doc in docs})
        await access.preload_documents(db, docs)
        for index, doc in enumerate(docs, start=skip + 1):
            if source.get("results_only") and is_work_artifact(doc.metadata_):
                continue
            if not await access.can_read_document(db, doc, workspace_id=workspace.id):
                continue
            folder = doc.fs_path.removeprefix(prefix).rpartition("/")[0]
            owner = next(((rel, child) for rel, child in children if folder == rel or folder.startswith(rel + "/")), None)
            own = named.get(owner[0] if owner else "")
            if finished and (doc.id not in own if own else is_work_artifact(doc.metadata_)):
                continue
            row = with_thumbnail_url({"id": doc.id, "_cursor": f"{parent_cursor}|{index}", "name": doc.name,
                   "folder": folder, "project_id": project.id, "mime_type": doc.mime_type,
                   "file_size": doc.file_size, "viewer_url": f"/viewer/{doc.id}",
                   "created_at": _iso(doc.created_at), "role": "file",
                   "delivered": ((doc.metadata_ or {}).get("artifact") or {}).get("delivered") is True}, doc)
            if owner is not None:
                rel, child = owner
                version = folder.removeprefix(rel).strip("/")
                if source.get("results_only"):
                    # A pack is the child's current version only: an earlier
                    # take, or a later one still being made, stays in Files.
                    here = current[child.id]
                    if not (folder == here or folder.startswith(here + "/")) or (
                            folder + "/").startswith(f"{here}/{PROJECT_TAKES_SEGMENT}/"):
                        continue
                    version = folder.removeprefix(here).strip("/")
                row["pack"] = {**packs[child.id], "version": version}
            result.append(row)
    return result


_DOCUMENT_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _named_documents(state: Any) -> set[str]:
    """The Documents a project's state names as its own: ``display.deliverables``
    and ``display.media`` when it declares them, else every Document id it
    holds outside its history (a draft or reference it never names stays out).
    """
    display = project_display(state)
    declared = {file["document_id"] for name in ("deliverables", "media") for file in display.get(name, [])}
    if display.get("deliverables"):
        return declared
    found: set[str] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if not (depth == 0 and key == "history"):
                    walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node[:500]:
                walk(child, depth + 1)
        elif isinstance(node, str) and _DOCUMENT_ULID.fullmatch(node):
            found.add(node)

    walk(state if isinstance(state, dict) else {}, 0)
    return found


def _pack_info(rel: str, child: WorkflowProject) -> dict[str, Any]:
    state = child.state or {}
    return {"id": child.id, "root": rel, "stage": child.current_stage,
            "label": " · ".join(str(state[key]) for key in ("code", "title") if state.get(key)) or rel.rsplit("/", 1)[-1],
            **{key: str(state[key]) for key in ("code", "synopsis") if isinstance(state.get(key), str) and state[key]}}


def _current_production(root: str, rel: str, child: WorkflowProject) -> str:
    """The child's current version folder, relative to the parent's folder."""
    own = str((child.state or {}).get("project_root") or "").strip("/")
    production = str((child.state or {}).get("production_root") or "").strip("/")
    if own and production.startswith(own + "/") and ".." not in production.split("/"):
        return production.removeprefix(root + "/")
    return rel


def _plan_packs(rows: list[dict[str, Any]], plan: list[dict[str, Any]]) -> None:
    """Order a packs block's cards by its plan; a planned child without a project is a card too."""
    cards = [row for row in rows if row.get("role") == "pack"]
    files = [row for row in rows if row.get("role") != "pack"]
    by_project = {card["pack"]["id"]: card for card in cards}
    by_code = {card["pack"]["code"]: card for card in cards if card["pack"].get("code")}
    ordered: list[dict[str, Any]] = []
    for item in plan:
        record = item.get("record") if isinstance(item.get("record"), dict) else {}
        code, title, synopsis = (str(record.get(key) or "").strip() for key in ("code", "title", "synopsis"))
        card = by_project.get(record.get("project_id")) or by_code.get(code)
        if card is None:
            pack_id = f"plan:{item['id']}"
            card = {"id": pack_id, "_cursor": item.get("_cursor", pack_id), "name": "", "folder": "", "role": "pack",
                    "pack": {"id": pack_id, "root": "", "stage": "planned", "parent_id": item.get("project_id"),
                             "label": " · ".join(value for value in (code, title) if value) or pack_id,
                             **({"code": code} if code else {}), **({"synopsis": synopsis} if synopsis else {})}}
        elif synopsis and not card["pack"].get("synopsis"):
            card["pack"] = {**card["pack"], "synopsis": synopsis}
        if all(card is not seen for seen in ordered):
            ordered.append(card)
    ordered.extend(card for card in cards if all(card is not seen for seen in ordered))
    rows[:] = ordered + files


async def _latest_child_runs(db: Any, entity_id: str, children: list[WorkflowProject]) -> dict[str, dict[str, Any]]:
    """Each child's latest run: how its making is going, and whether it waits for a person."""
    from packages.core.services.workflow_run_control import run_resume_action

    run_ids = {child.last_run_id: child.id for child in children if getattr(child, "last_run_id", None)}
    if not run_ids:
        return {}
    # A retry is a new run in the same lineage; the child's latest attempt says how it is going.
    roots = {
        (run.lineage_root_run_id or run.id): run_ids[run.id]
        for run in (await db.execute(select(WorkflowRun).options(*_app_run_options()).where(
            WorkflowRun.entity_id == entity_id, WorkflowRun.id.in_(list(run_ids)),
        ))).scalars()
    }
    if not roots:
        return {}
    latest: dict[str, WorkflowRun] = {}
    for run in (await db.execute(select(WorkflowRun).options(*_app_run_options()).where(
        WorkflowRun.entity_id == entity_id,
        or_(WorkflowRun.id.in_(list(roots)), WorkflowRun.lineage_root_run_id.in_(list(roots))),
    ).order_by(WorkflowRun.attempt_number.desc(), WorkflowRun.created_at.desc(), WorkflowRun.id.desc()))).scalars():
        latest.setdefault(run.lineage_root_run_id or run.id, run)
    runs = list(latest.values())
    run_ids = {run.id: roots[run.lineage_root_run_id or run.id] for run in runs}
    # Only the runs still going need their route, trace and snapshot: a finished one is shown from its status.
    working = [run for run in runs if run.status in {"running", "paused"}]
    await _load_run_columns(db, working, "definition_snapshot", "execution_trace", "step_results", "execution_snapshot")
    stages = await _production_stages(db, entity_id, working)
    return {run_ids[run.id]: {
        "id": run.id, "workflow_id": run.workflow_id, "binding_id": run.binding_id, "status": run.status,
        # A run paused for background work (an automatic retry) continues on its own.
        "needs_you": run.status == "paused" and run_resume_action(run) is not None,
        **({"stage": stages[run.id]} if stages.get(run.id) else {}),
    } for run in runs}


async def _production_stages(db: Any, entity_id: str, runs: list[WorkflowRun]) -> dict[str, str]:
    """The production stage each run is working in, named as its run page names it."""
    from packages.core.i18n import get_locale
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.workflow_production import production_active_stage, workflow_production_config
    from packages.core.services.workflow_run_trace import workflow_execution_steps
    from packages.core.services.viewer_language import without_i18n

    if not runs:
        return {}
    definitions = dict((await db.execute(select(WorkflowDefinition.id, WorkflowDefinition.steps).where(
        WorkflowDefinition.entity_id == entity_id,
        WorkflowDefinition.id.in_({run.workflow_id for run in runs}),
    ))).all())
    # The App data request carries the viewer's language, as the run page does.
    locale = get_locale()
    configs = {workflow_id: workflow_production_config(localized(steps, locale) if locale else without_i18n(steps))
               for workflow_id, steps in definitions.items()}
    result = {}
    for run in runs:
        config = configs.get(run.workflow_id)
        stage = production_active_stage(config, workflow_execution_steps(run), run.current_step_id) if config else None
        if stage:
            result[run.id] = stage
    return result


async def _project_reference_rows(
    db: Any, workspace: Any, user: Any, source: dict[str, Any], parents: list[dict[str, Any]],
    projects: dict[str, WorkflowProject], *, resume: tuple[str | None, int], limit: int,
) -> _SourceRows:
    """The references each project holds or inherits from its ancestors, nearest first."""
    from packages.core.services.project_references import project_chain

    kind = source.get("reference_kind", "voices")
    result = _SourceRows()
    for parent in parents:
        project = projects.get(parent.get("id"))
        if project is None:
            continue
        chain = await project_chain(db, entity_id=user.entity_id, workspace_id=workspace.id, project_id=project.id)
        seen: set[str] = set()
        parent_cursor = parent.get("_cursor", parent["id"])
        skip = resume[1] if parent_cursor == resume[0] else -1
        index = -1
        for holder in chain:
            slots = ((holder.state or {}).get("references") or {}).get(kind)
            for role, value in (slots.items() if isinstance(slots, dict) else ()):
                if role in seen or not isinstance(value, dict) or not value:
                    continue
                seen.add(role)
                index += 1
                if index <= skip:
                    continue
                result.append({"id": f"{project.id}:{kind}:{role}", "_cursor": f"{parent_cursor}|{index}",
                               "kind": kind, "role": role, "project_id": project.id, "holder_project_id": holder.id,
                               "inherited": holder.id != project.id,
                               "holder_label": str((holder.state or {}).get("title") or ""),
                               "value": {key: value.get(key) for key in ("voice", "provider", "model", "pinned_at")
                                         if value.get(key) is not None},
                               "_fs_path": str(value.get("fs_path") or "").strip().lstrip("/")})
    # A file reference (a key visual, a character sheet) shows its readable Document.
    paths = {row["_fs_path"] for row in result if row["_fs_path"]}
    docs = list((await db.execute(select(Document).where(
        Document.entity_id == user.entity_id, Document.is_trashed.is_(False), Document.fs_path.in_(paths),
    ))).scalars()) if paths else []
    access = await DocumentAccessContext.load(db, entity_id=user.entity_id, user_id=user.id, role=user.role,
                                              document_ids={doc.id for doc in docs})
    await access.preload_documents(db, docs)
    readable = {doc.fs_path: doc for doc in docs if await access.can_read_document(db, doc, workspace_id=workspace.id)}
    for row in result:
        doc = readable.get(row.pop("_fs_path"))
        if doc is not None:
            row["media"] = [with_thumbnail_url({"document_id": doc.id, "title": doc.name, "mime_type": doc.mime_type,
                                                "viewer_url": f"/viewer/{doc.id}"}, doc)]
    return result


async def _context_cards(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], component: dict[str, Any],
    rows: list[WorkflowProject], items: list[dict[str, Any]],
) -> None:
    """What a project's card shows: its first image reference (else the first
    picture its display names) and how many children are done."""
    context = component["context"]
    blocks = {block["key"]: block for block in component["blocks"]}
    cover_block = blocks.get(context.get("cover") or "")
    progress_block = blocks.get(context.get("progress") or "")
    by_id = {item["id"]: item for item in items}
    named = {row.id: [file["document_id"] for file in project_display(row.state).get("media", [])] for row in rows}
    pictures = await _readable_media(db, workspace, user, {doc_id for ids in named.values() for doc_id in ids})
    for row in rows:
        item = by_id.get(row.id)
        if item is None:
            continue
        if cover_block is not None:
            references = await _project_reference_rows(
                db, workspace, user, sources[cover_block["source"]], [{"id": row.id}], {row.id: row},
                resume=(None, -1), limit=20,
            )
            cover = next((reference["media"][0] for reference in references if reference.get("media")), None)
            if cover is not None:
                item["cover"] = cover
        if "cover" not in item:
            cover = next((pictures[doc_id] for doc_id in named[row.id] if doc_id in pictures and _pictured(pictures[doc_id])), None)
            if cover is not None:
                item["cover"] = cover
        if progress_block is not None:
            rollup = await _project_rollup(
                db, workspace, user, sources, progress_block["source"], sources[progress_block["source"]],
                {"kind": "project", "id": row.id, "source": context["source"]},
            )
            completion = progress_block["completion"]
            item["progress"] = {
                "done": sum(rollup["counts_by_stage"].get(stage, 0) for stage in completion["stages"]),
                "total": await _planned_total(db, workspace, user, sources, progress_block, rollup["total"],
                                              {"kind": "project", "id": row.id, "source": context["source"]}),
                "label": completion["label"],
            }


async def _all_source_rows(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], name: str, context: dict[str, str],
    cache: dict[str, Any], *, window: str | None,
) -> list[dict[str, Any]]:
    """Read a bounded derived source before applying its stable public cursor."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await _source_rows(db, workspace, user, sources, name, context, cache,
                                  after_id=cursor, page_limit=101, window=window)
        rows.extend(page)
        next_cursor = getattr(page, "next_cursor", None) or (page[-1].get("_cursor") if page else None)
        if not next_cursor or next_cursor == cursor:
            return rows
        cursor = next_cursor
        if len(rows) > 10_000:
            raise AppDataError("source_too_large", "The configured data source exceeds the App read limit.", source=name)


def _project_filters(entity_id: str, workspace_id: str, source: dict[str, Any], narrow: list[Any] | None = None) -> list[Any]:
    """Which projects a workflow_projects source holds, narrowed by what a person picked."""
    filters = [WorkflowProject.entity_id == entity_id, WorkflowProject.workspace_id == workspace_id,
               WorkflowProject.project_type == source["project_type"],
               WorkflowProject.schema_version.in_(source["schema_versions"]), *(narrow or [])]
    if source.get("stages"):
        filters.append(WorkflowProject.current_stage.in_(source["stages"]))
    if source.get("exclude_stages"):
        filters.append(WorkflowProject.current_stage.not_in(source["exclude_stages"]))
    return filters


async def _chart_rows(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], name: str, context: dict[str, str], cache: dict[str, Any],
    *, window: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Up to CHART_ROW_LIMIT records of a chart's source as ``{id, label, stage, values}``, and whether there were more.

    Projects are read without their runs: a chart counts what records hold, not how their making goes."""
    source = sources[name]
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    lean = source["type"] == "related_projects" or (source["type"] == "workflow_projects" and context["kind"] == "workspace")
    while len(rows) <= CHART_ROW_LIMIT:
        if lean:
            page_rows = (await _related_projects(db, workspace, user, sources, name, source, context, after_id=cursor, limit=101)
                         if source["type"] == "related_projects" else
                         await _projects(db, entity_id=user.entity_id, workspace_id=workspace.id, source=source,
                                         after_id=cursor, limit=101))
            page = [_project(row, source["fields"], source.get("label_fields"), None, source) for row in page_rows]
            next_cursor = page[-1]["_cursor"] if len(page_rows) >= 101 else None
        else:
            page = await _source_rows(db, workspace, user, sources, name, context, cache, after_id=cursor, page_limit=101,
                                      window=window)
            next_cursor = getattr(page, "next_cursor", None) or (page[-1].get("_cursor") if len(page) >= 101 else None)
        rows.extend({"id": row.get("id"), "label": row.get("label") or (row.get("record") or {}).get("title") or row.get("name"),
                     "stage": row.get("stage"), "values": row.get("fields") if isinstance(row.get("fields"), dict) else {},
                     **({"observations": row["observations"], "key": row.get("key"), "name": row.get("name"),
                         "unit": row.get("unit"), "description": row.get("description")} if "observations" in row else {})}
                    for row in page)
        if not next_cursor or next_cursor == cursor:
            return rows, False
        cursor = next_cursor
    return rows[:CHART_ROW_LIMIT], True


async def _chart_data(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], block: dict[str, Any], context: dict[str, str],
    cache: dict[str, Any], *, window: str | None, locale: str | None,
) -> dict[str, Any]:
    chart = block["chart"]
    source = sources[block["source"]]
    rows, truncated = await _chart_rows(db, workspace, user, sources, block["source"], context, cache, window=window)
    if source["type"] == "workspace_stats":
        cutoff = datetime.now(UTC) - timedelta(days={"7d": 7, "30d": 30, "90d": 90}.get(window or "7d", 7))
        for row in rows:
            row["observations"] = [reading for reading in row.get("observations") or []
                                   if (at := chart_time(reading.get("observed_at"))) is not None and at >= cutoff]
        drawn = build_stat_chart(chart, rows, locale=locale)
    else:
        drawn = build_chart(chart, rows, stage_labels=block.get("stage_labels"), stages=source.get("stages"),
                            truncated=truncated, locale=locale)
    target = chart.get("target")
    if target and "goal" in target:
        from packages.core.models.goal import Goal
        goal = (await db.execute(select(Goal).where(
            Goal.entity_id == user.entity_id, Goal.workspace_id == workspace.id, Goal.goal_key == target["goal"],
        ).limit(1))).scalar_one_or_none()
        # A Goal that is gone draws no line rather than a made-up one.
        if goal is not None and goal.target_value is not None:
            drawn["target"] = {"value": float(goal.target_value), "label": target.get("label") or goal.title}
    elif target:
        drawn["target"] = {"value": target["value"], **({"label": target["label"]} if target.get("label") else {})}
    return drawn


def _numeric_state(path: list[str]) -> Any:
    """A number a project's state holds at ``path`` (also one kept as text), else NULL: never a cast that fails."""
    # "8,000" is a number as the cards read it: the thousands separators go before the check and the cast.
    text = func.btrim(func.replace(_state_text(path), ",", ""))
    return case((text.op("~")(r"^-?[0-9]+(\.[0-9]+)?$"), cast(text, Numeric)), else_=None)


async def _block_totals(db: Any, workspace: Any, user: Any, source: dict[str, Any], block: dict[str, Any],
                        narrowing: "_Narrowing | None") -> list[dict[str, Any]]:
    """Each of a table's totals over every record the person's filters keep, not only the page shown."""
    by_path = {tuple(field["field"]): field for field in block.get("fields") or [] if isinstance(field, dict)}
    aggregates = {"sum": func.sum, "avg": func.avg, "min": func.min, "max": func.max}
    columns = [aggregates[total["agg"]](_numeric_state(total["field"])) for total in block["totals"]]
    values = (await db.execute(select(*columns).where(*_project_filters(
        user.entity_id, workspace.id, source, narrowing.conditions if narrowing else None)))).one()
    totals = []
    for total, value in zip(block["totals"], values):
        field = by_path[tuple(total["field"])]
        presented = _present_configured_field({**field, "hide_when_empty": False},
                                              float(value) if value is not None else None)
        totals.append({**(presented or {"label": field["label"]}), "agg": total["agg"]})
    return totals


async def _child_counts(db: Any, user: Any, workspace: Any, source: dict[str, Any], parent_ids: list[str]) -> dict[str, int]:
    """How many projects of a tree's child source sit under each of these parents."""
    if not parent_ids:
        return {}
    parent = WorkflowProject.state
    for key in source["parent_state_path"]:
        parent = parent[key]
    rows = (await db.execute(select(parent.astext, func.count()).where(
        *_project_filters(user.entity_id, workspace.id, source), parent.astext.in_(parent_ids),
    ).group_by(parent.astext))).all()
    return {str(parent_id): int(count) for parent_id, count in rows}


async def _planned_total(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], block: dict[str, Any], started: int,
    context: dict[str, str],
) -> int:
    """A rollup over a plan counts every planned child, started or not."""
    if not block.get("plan"):
        return started
    planned = await _all_source_rows(db, workspace, user, sources, block["plan"], context, {}, window=None)
    return max(started, len(planned))


# An item a run is still working on is "in progress" by stage alone. Counted by
# its run instead, it reads the same on a campaign card and inside the campaign.
_IN_PROGRESS_STAGES = frozenset({"leased", "running", "in_progress", "processing", "producing"})


def _execution_display_stage(stage: str, execution_status: str | None, completed: frozenset[str]) -> str:
    """The stage an item shows, matching the App calendar's own reading of one item."""
    if stage in completed:
        return stage
    if execution_status in {"failed", "cancelled"}:
        return execution_status
    if stage in _IN_PROGRESS_STAGES and execution_status in {"paused", "pending"}:
        return execution_status
    return stage


def _execution_adjusted_counts(
    rows: list[WorkflowProject], counts: dict[str, int], completed: frozenset[str],
    executions: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Move each unfinished item whose run failed, was cancelled or waits on a person to that count."""
    adjusted = dict(counts)
    for row in rows:
        stage = str(row.current_stage or "")
        display = _execution_display_stage(stage, (executions.get(row.id) or {}).get("status"), completed)
        if display != stage and adjusted.get(stage, 0) > 0:
            adjusted[stage] -= 1
            adjusted[display] = adjusted.get(display, 0) + 1
    return {stage: count for stage, count in adjusted.items() if count > 0}


async def _project_rollup(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], source_name: str, source: dict[str, Any], context: dict[str, str],
    completed_stages: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate one declared project relation in SQL for the selected App context.

    With ``completed_stages`` the stage counts also reflect each unfinished
    item's run, the same way the App reads a single item.
    """
    chain_names, chain = [source_name], [source]
    while chain[-1]["type"] == "related_projects":
        parent_name = chain[-1]["parent"]
        chain.append(sources[parent_name])
        chain_names.append(parent_name)
    chain.reverse()
    chain_names.reverse()
    rows = [aliased(WorkflowProject) for _ in chain]
    query = select(rows[-1])
    for index in range(len(rows) - 1, 0, -1):
        child, parent, child_source = rows[index], rows[index - 1], chain[index]
        expression: Any = child.state
        for key in child_source["parent_state_path"]:
            expression = expression[key]
        query = query.join(parent, expression.astext == parent.id)
    filters = []
    for row, config in zip(rows, chain):
        filters.extend((row.entity_id == user.entity_id, row.workspace_id == workspace.id,
                        row.project_type == config["project_type"], row.schema_version.in_(config["schema_versions"])))
        if config.get("stages"):
            filters.append(row.current_stage.in_(config["stages"]))
        if config.get("exclude_stages"):
            filters.append(row.current_stage.not_in(config["exclude_stages"]))
    if context["kind"] == "project":
        selected = context.get("source")
        if selected not in chain_names:
            raise AppDataError("invalid_context", "The selected project is outside this component source.")
        filters.append(rows[chain_names.index(selected)].id == context["id"])
    scoped = query.where(and_(*filters)).subquery()
    total = (await db.execute(select(func.count()).select_from(scoped))).scalar_one()
    counts = dict((await db.execute(select(scoped.c.current_stage, func.count()).group_by(scoped.c.current_stage))).all())
    counts_by_stage = {str(stage): int(count) for stage, count in counts.items() if stage is not None}
    if completed_stages is not None:
        completed = frozenset(completed_stages)
        unfinished = [*filters, rows[-1].current_stage.notin_(completed)] if completed else filters
        open_rows = list((await db.execute(query.where(and_(*unfinished)))).scalars())
        executions = await _project_executions(
            db, entity_id=user.entity_id, workspace_id=workspace.id, rows=open_rows, source=chain[-1],
        )
        counts_by_stage = _execution_adjusted_counts(open_rows, counts_by_stage, completed, executions)
    return {"total": int(total), "counts_by_stage": counts_by_stage,
            "query": query, "filters": filters, "row": rows[-1]}


async def _calendar_context_summaries(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], component: dict[str, Any],
    context_rows: list[WorkflowProject],
) -> dict[str, dict[str, Any]]:
    """Project declared calendar summaries for one contexts page without N+1 reads."""
    calendar = component.get("display", {}).get("calendar")
    if not calendar or not context_rows:
        return {}
    blocks = {block["key"]: block for block in component["blocks"]}
    calendar_block = blocks[calendar["block"]]
    rollup_block = blocks[calendar["rollup_block"]]
    context_block = blocks[calendar["context_block"]]
    source_name = calendar_block["source"]
    source = sources[source_name]
    context_source = component["context"]["source"]
    context_ids = {row.id for row in context_rows}

    summaries: dict[str, dict[str, Any]] = {}
    context_values = {row.id: {".".join(path): _value_at(row.state or {}, path) for path in sources[context_source]["fields"]}
                      for row in context_rows}
    records = await _named_records(db, workspace, user, context_block["fields"],
                                   [{"fields": values} for values in context_values.values()])
    for row in context_rows:
        values = context_values[row.id]
        fields = []
        for configured in context_block["fields"]:
            item = _present_configured_field(configured, values.get(".".join(configured["field"])),
                                              lambda path, values=values: values.get(".".join(path)), records=records)
            if item is not None:
                fields.append(item)
        summaries[row.id] = {"fields": fields, "total": 0, "counts_by_stage": {}}

    chain_names, chain = [source_name], [source]
    while chain[-1]["type"] == "related_projects":
        parent_name = chain[-1]["parent"]
        chain.append(sources[parent_name])
        chain_names.append(parent_name)
    chain.reverse()
    chain_names.reverse()
    if chain_names[0] != context_source:
        raise AppDataError("invalid_component", "The calendar source is outside the selected project source.")
    rows = [aliased(WorkflowProject) for _ in chain]
    root, leaf = rows[0], rows[-1]
    joins = []
    for index in range(len(rows) - 1, 0, -1):
        child, parent, child_source = rows[index], rows[index - 1], chain[index]
        expression: Any = child.state
        for key in child_source["parent_state_path"]:
            expression = expression[key]
        joins.append((parent, expression.astext == parent.id))
    filters = [root.id.in_(context_ids)]
    for row, config in zip(rows, chain):
        filters.extend((row.entity_id == user.entity_id, row.workspace_id == workspace.id,
                        row.project_type == config["project_type"], row.schema_version.in_(config["schema_versions"])))
        if config.get("stages"):
            filters.append(row.current_stage.in_(config["stages"]))
        if config.get("exclude_stages"):
            filters.append(row.current_stage.not_in(config["exclude_stages"]))

    counts_query = select(root.id, leaf.current_stage, func.count()).select_from(leaf)
    for parent, condition in joins:
        counts_query = counts_query.join(parent, condition)
    counts_query = counts_query.where(and_(*filters)).group_by(root.id, leaf.current_stage)
    for context_id, stage, count in (await db.execute(counts_query)).all():
        if stage is None:
            continue
        summary = summaries.get(context_id)
        if summary is None:
            continue
        summary["total"] += int(count)
        summary["counts_by_stage"][str(stage)] = int(count)

    # A card counts each unfinished post by its run, as the open campaign does.
    completed = frozenset((rollup_block.get("completion") or {}).get("stages") or [])
    open_query = select(root.id, leaf).select_from(leaf)
    for parent, condition in joins:
        open_query = open_query.join(parent, condition)
    open_filters = [*filters, leaf.current_stage.notin_(completed)] if completed else filters
    open_by_context: dict[str, list[WorkflowProject]] = {}
    for context_id, row in (await db.execute(open_query.where(and_(*open_filters)))).all():
        open_by_context.setdefault(context_id, []).append(row)
    open_executions = await _project_executions(
        db, entity_id=user.entity_id, workspace_id=workspace.id,
        rows=[row for rows_ in open_by_context.values() for row in rows_], source=source,
    )
    for context_id, summary in summaries.items():
        summary["counts_by_stage"] = _execution_adjusted_counts(
            open_by_context.get(context_id, []), summary["counts_by_stage"], completed, open_executions,
        )
        summary["counts_include_execution"] = True

    first = rollup_block.get("first")
    if not first:
        return summaries
    order_value = _project_order_expression(leaf, source)
    ordered = order_value.asc().nulls_last() if source["order"]["direction"] == "asc" else order_value.desc().nulls_last()
    rank = func.row_number().over(partition_by=root.id, order_by=[ordered, leaf.id.asc()]).label("rank")
    first_query = select(root.id.label("context_id"), leaf.id.label("row_id"), rank).select_from(leaf)
    for parent, condition in joins:
        first_query = first_query.join(parent, condition)
    ranked = first_query.where(and_(*filters), leaf.current_stage.in_(first["stages"])).subquery()
    candidates = (await db.execute(select(ranked.c.context_id, WorkflowProject).join(
        WorkflowProject, WorkflowProject.id == ranked.c.row_id,
    ).where(ranked.c.rank == 1))).all()
    first_rows: dict[str, WorkflowProject] = {context_id: row for context_id, row in candidates}
    executions = await _project_executions(
        db, entity_id=user.entity_id, workspace_id=workspace.id, rows=list(first_rows.values()), source=source,
    )
    next_rows = {context_id: _project(row, source["fields"], source.get("label_fields"), executions.get(row.id), source)
                 for context_id, row in first_rows.items()}
    next_records = await _named_records(db, workspace, user, calendar_block.get("fields"), list(next_rows.values()))
    for context_id, row in first_rows.items():
        next_row = next_rows[context_id]
        next_row["calendar_at"] = _value_at(row.state or {}, calendar["date_field"])
        next_item = _present_items(calendar_block, [next_row], records=next_records)[0]
        next_item.pop("_cursor", None)
        summaries[context_id]["next"] = next_item
    return summaries


def _deduplicate_publications(
    rows: list[dict[str, Any]], *, subject_source: str,
    ancestors: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    representatives: dict[tuple[str, tuple[str, str]], list[dict[str, Any]]] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        record = row.get("record") if isinstance(row.get("record"), dict) else {}
        subject_id = str(ancestors.get(row.get("id"), {}).get(subject_source, {}).get("id") or "")
        identity = _publication_identity(record)
        if not subject_id or identity is None:
            result.append(row)
            continue
        key = (subject_id, identity[0])
        candidates = representatives.setdefault(key, [])
        representative = next((candidate for candidate in candidates if not identity[1]
                               or not _publication_identity(candidate["record"])[1]
                               or identity[1].intersection(_publication_identity(candidate["record"])[1])), None)
        if representative is None:
            candidates.append(row)
            result.append(row)
            continue
        incompatible = any(len({str(value) for value in (representative["record"].get(key), record.get(key))
                                if isinstance(value, str) and value}) > 1
                           for key in ("external_id", "payload_hash"))
        if incompatible:
            representative["conflict"] = "Publication receipts disagree on the published material identity."
    return result


async def _observed_publications(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], block: dict[str, Any],
    context: dict[str, str], cache: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], set[str]]:
    """Qualify receipts by their exact subject/account/post and durable observations."""
    metric_source = sources[block["metrics_source"]]
    rows = await _all_source_rows(db, workspace, user, sources, block["source"], context, cache, window=None)
    ancestors = await _record_ancestor_states(db, workspace, user, sources, block["source"], rows)
    rows = _deduplicate_publications(rows, subject_source=metric_source["subject"], ancestors=ancestors)
    subject_ids = {ancestors.get(row["id"], {}).get(metric_source["subject"], {}).get("id") for row in rows}
    subject_ids.discard(None)
    if not subject_ids:
        return [], ancestors, set()
    observation = select(WorkspaceStatObservation.id).where(
        WorkspaceStatObservation.stat_id == WorkspaceStat.id,
        WorkspaceStatObservation.entity_id == user.entity_id,
        WorkspaceStatObservation.workspace_id == workspace.id,
        WorkspaceStatObservation.collector_revision == WorkspaceStat.revision,
        WorkspaceStatObservation.value.is_not(None),
        WorkspaceStatObservation.observed_at <= datetime.now(UTC),
    ).exists()
    stats = list((await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == user.entity_id, WorkspaceStat.workspace_id == workspace.id,
        WorkspaceStat.status == "active",
        WorkspaceStat.collector_type.in_(_publication_collector_types(metric_source)),
        WorkspaceStat.collector_config["metric_key"].astext.in_(metric_source["metric_keys"]),
        WorkspaceStat.collector_config["subject"]["kind"].astext == "workflow_project",
        WorkspaceStat.collector_config["subject"]["id"].astext.in_(subject_ids), observation,
    ).limit(10_001))).scalars())
    if len(stats) > 10_000:
        raise AppDataError("source_too_large", "The configured tracking source exceeds the App read limit.")
    by_subject: dict[str, list[WorkspaceStat]] = {}
    for stat in stats:
        by_subject.setdefault(stat.collector_config["subject"]["id"], []).append(stat)
    eligible = []
    for row in rows:
        subject_id = ancestors.get(row["id"], {}).get(metric_source["subject"], {}).get("id", "")
        if not row.get("conflict") and any(
            _stat_matches_publication(stat, subject_id=subject_id, record=row["record"])
            for stat in by_subject.get(subject_id, [])
        ):
            eligible.append(row)
    return eligible, ancestors, {stat.id for stat in stats}


async def _eligible_records(
    db: Any, workspace: Any, user: Any, sources: dict[str, Any], block: dict[str, Any],
    context: dict[str, str], cache: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Ordinary records qualify by existence; publication adapters retain identity checks."""
    rows = await _all_source_rows(db, workspace, user, sources, block["source"], context, cache, window=None)
    ancestors = await _record_ancestor_states(db, workspace, user, sources, block["source"], rows)
    if block.get("metrics_source"):
        metric_source = sources[block["metrics_source"]]
        rows = _deduplicate_publications(rows, subject_source=metric_source["subject"], ancestors=ancestors)
    return rows, ancestors


async def _publication_metric_items(
    db: Any, workspace: Any, user: Any, metric_source: dict[str, Any],
    rows: list[dict[str, Any]], ancestors: dict[str, dict[str, dict[str, Any]]], *, window: str | None,
    stat_ids: set[str] | None = None,
    configured_only: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    subject_name = metric_source["subject"]
    subject_ids = {str(ancestors.get(row.get("id"), {}).get(subject_name, {}).get("id") or "") for row in rows}
    subject_ids.discard("")
    stats = list((await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == user.entity_id, WorkspaceStat.workspace_id == workspace.id,
        WorkspaceStat.collector_type.in_(_publication_collector_types(metric_source)),
        WorkspaceStat.collector_config["metric_key"].astext.in_(metric_source["metric_keys"]),
        WorkspaceStat.collector_config["subject"]["kind"].astext == "workflow_project",
        WorkspaceStat.collector_config["subject"]["id"].astext.in_(subject_ids),
    ).order_by(WorkspaceStat.id))).scalars()) if subject_ids else []
    days = {"7d": 7, "30d": 30, "90d": 90}.get(window or "7d", 7)
    by_record: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        record_id = row.get("id")
        record = row.get("record") if isinstance(row.get("record"), dict) else {}
        subject_id = str(ancestors.get(record_id, {}).get(subject_name, {}).get("id") or "")
        matching = [stat for stat in stats if (stat_ids is None or stat.id in stat_ids)
                    and _stat_matches_publication(stat, subject_id=subject_id, record=record)]
        selected: dict[str, WorkspaceStat] = {}
        for stat in matching:
            metric_key = str((stat.collector_config or {}).get("metric_key") or "")
            selected.setdefault(metric_key, stat)
        metric_rows = []
        for metric_key in metric_source["metric_keys"]:
            stat = selected.get(metric_key)
            if stat is None:
                if configured_only:
                    continue
                metric_rows.append({"id": f"{record_id}:{metric_key}", "_cursor": f"{record_id}:{metric_key}",
                    "key": metric_key, "name": metric_key, "unit": None, "status": "no_data",
                    "current_value": None, "value_type": None, "collection_status": None,
                    "collection_error": None, "freshness": "no_data", "metric_key": metric_key,
                    "platform": record.get("platform"), "public_url": record.get("published_url"), "updated_at": None,
                    "observations": [], "comparison": {"latest_value": None, "delta_reason": "no_observation"}})
                continue
            observations = list((await db.execute(select(WorkspaceStatObservation).where(
                WorkspaceStatObservation.entity_id == user.entity_id, WorkspaceStatObservation.workspace_id == workspace.id,
                WorkspaceStatObservation.stat_id == stat.id,
            ).order_by(WorkspaceStatObservation.observed_at.desc(), WorkspaceStatObservation.id.desc()).limit(100))).scalars())
            metric_rows.append({"id": stat.id, "_cursor": stat.id, "key": stat.key, "name": stat.name, "unit": stat.unit,
                "status": stat.status, "current_value": float(stat.current_value) if stat.current_value is not None else None,
                "value_type": stat.value_type, "collection_status": stat.last_collection_status,
                "collection_error": stat.last_collection_error, "freshness": stat_freshness_status(stat),
                "metric_key": metric_key, "platform": (stat.collector_config or {}).get("platform"),
                "public_url": (stat.collector_config or {}).get("public_url"), "updated_at": _iso(stat.current_value_updated_at),
                "_stat": stat, "_observations": observations,
                "observations": [{"id": obs.id, "value": float(obs.value) if obs.value is not None else None,
                    "observed_at": _iso(obs.observed_at), "window_start": _iso(obs.window_start), "window_end": _iso(obs.window_end)} for obs in observations]})
        compared_rows = [item for item in metric_rows if item.get("_stat") is not None]
        if compared_rows:
            from packages.core.stats.publication_reporting import compare_publication_stats
            end = datetime.now(UTC)
            compared = compare_publication_stats([item["_stat"] for item in compared_rows],
                sorted((observation for item in compared_rows for observation in item["_observations"]), key=lambda item: (item.observed_at, item.id)),
                window_start=end - timedelta(days=days), window_end=end)
            comparison = {item["stat_id"]: item for item in compared["stats"]}
            for item in compared_rows:
                item["comparison"] = comparison.get(item["id"], {})
        presented = _present_items({"type": "metrics", "fields": []}, metric_rows, window=window)
        for item in presented:
            item.pop("_cursor", None)
        by_record[str(record_id)] = presented
    return by_record


async def component_data(
    db: Any, workspace: Any, *, user: Any, component_key: str, context_kind: str, context_id: str,
    block_key: str | None = None, cursor: str | None = None, limit: int = 50, window: str | None = None,
    locale: str | None = None, filters: dict[str, str] | None = None, query: str | None = None,
    tree_parent: str | None = None, tree_level: int | None = None,
) -> dict[str, Any]:
    """``filters`` (filter index -> value) and ``query`` narrow the one list block ``block_key`` reads.

    On a tree block, ``tree_parent`` with ``tree_level`` (1 or 2) reads the projects under one record instead."""
    if window not in {None, "7d", "30d", "90d"}:
        raise AppDataError("invalid_window", "This component supports 7d, 30d, or 90d comparison windows.")
    config = normalize_app_view_config((workspace.operating_model or {}).get("app_view"))
    try:
        validate_app_view_data_contract(config["data_sources"], config["components"],
                                        catalog=await load_workspace_app_data_catalog(db, workspace))
    except ValueError as exc:
        raise AppDataError("invalid_component", "The component configuration is no longer valid.") from exc
    # Labels read in the viewer's language; a translation only replaces text,
    # so the keys and paths validated above are unchanged.
    config = localized(config, locale)
    component = next((item for item in config["components"] if item["key"] == component_key), None)
    if component is None:
        raise AppDataError("component_not_found", "This App component is not available.")
    is_launch_context = (context_kind == "launch_run" and component["context"]["kind"] == "project"
                         and not component.get("visibility"))
    if context_kind != component["context"]["kind"] and not is_launch_context:
        raise AppDataError("invalid_context", "The context type does not match this component.")
    await _validate_component_context(db, workspace, user, config["data_sources"], component,
                                      context_kind, context_id)
    selected = [block for block in component["blocks"] if block_key is None or block["key"] == block_key]
    if block_key and not selected:
        raise AppDataError("block_not_found", "This component block is not available.")
    cache: dict[str, Any] = {}
    calendar = component.get("display", {}).get("calendar")
    source_context = {"kind": context_kind, "id": context_id, **(
        {"source": component["context"]["source"]} if component["context"].get("source") else {}
    )}
    visibility = component.get("visibility")
    eligible_rows, eligible_ancestors, eligible_stat_ids = [], {}, set()
    if visibility:
        gate = next(block for block in component["blocks"] if block["key"] == visibility["block"])
        if visibility["when"] == "has_observations":
            eligible_rows, eligible_ancestors, eligible_stat_ids = await _observed_publications(
                db, workspace, user, config["data_sources"], gate, source_context, cache,
            )
        else:
            eligible_rows, eligible_ancestors = await _eligible_records(
                db, workspace, user, config["data_sources"], gate, source_context, cache,
            )
        if not eligible_rows:
            return {"component_key": component_key, "context": {"kind": context_kind, "id": context_id},
                    "status": "ready", "visible": False, "blocks": [], "errors": []}
    blocks = []
    errors = []
    def depends_on(source_name: str, expected: str) -> bool:
        if source_name == expected:
            return True
        source = config["data_sources"][source_name]
        return any(depends_on(dependency, expected) for dependency in (source.get("parent"), source.get("subject")) if dependency)
    for block in selected:
        try:
            block_source = config["data_sources"][block["source"]]
            if context_kind == "project" and block_source["type"] != "workspace_stats" and not depends_on(block["source"], component["context"]["source"]):
                raise AppDataError("invalid_context", "The component block is outside the selected project source.", source=block["source"])
            effective_limit = min(limit, block["limit"])
            extra: dict[str, Any] = {}
            tree_children = (block.get("tree") or {}).get("children") or []
            if block["type"] == "chart":
                drawn = await _chart_data(db, workspace, user, config["data_sources"], block, source_context, cache,
                                          window=window, locale=locale)
                extra["chart"] = drawn
                page, next_cursor = chart_items(block["key"], drawn), None
            elif tree_parent and block_key and tree_children:
                # One record's projects in a tree: its episodes, or an episode's shots.
                if tree_level not in range(1, len(tree_children) + 1):
                    raise AppDataError("invalid_tree_level", "This tree has no such level.")
                child_source = config["data_sources"][tree_children[tree_level - 1]]
                child_rows = await _projects(db, entity_id=user.entity_id, workspace_id=workspace.id, source=child_source,
                                             parent_id=tree_parent, after_id=cursor, limit=effective_limit + 1)
                executions = await _project_executions(db, entity_id=user.entity_id, workspace_id=workspace.id,
                                                       rows=child_rows, source=child_source)
                items = [_project(row, child_source["fields"], child_source.get("label_fields"), executions.get(row.id),
                                  child_source) for row in child_rows]
                page, next_cursor = _page(_present_items({"type": "progress", "fields": [],
                                                          "stage_labels": block.get("stage_labels")}, items),
                                          None, effective_limit)
                if tree_level < len(tree_children):
                    counts = await _child_counts(db, user, workspace, config["data_sources"][tree_children[tree_level]],
                                                 [item["id"] for item in page])
                    for item in page:
                        item["child_count"] = counts.get(item["id"], 0)
                extra["tree_level"] = tree_level
            elif block["type"] == "summary":
                rows = await _source_rows(db, workspace, user, config["data_sources"], block["source"],
                                          source_context, cache, page_limit=1, window=window)
                page, next_cursor = _present_summary(
                    block, rows, await _named_records(db, workspace, user, block["fields"], rows[:1])), None
            elif block["type"] == "project_rollup":
                rollup = await _project_rollup(
                    db, workspace, user, config["data_sources"], block["source"], block_source, source_context,
                    completed_stages=((block.get("completion") or {}).get("stages") or []) if block["count_by_stage"] else None,
                )
                next_item = None
                first = block.get("first")
                if first:
                    first_filters = [*rollup["filters"], rollup["row"].current_stage.in_(first["stages"])]
                    first_query = _apply_project_order(rollup["query"].where(and_(*first_filters)), rollup["row"], block_source, None)
                    next_row = (await db.execute(first_query.limit(1))).scalar_one_or_none()
                    if next_row is not None:
                        executions = await _project_executions(
                            db, entity_id=user.entity_id, workspace_id=workspace.id, rows=[next_row], source=block_source,
                        )
                        next_item = _project(next_row, block_source["fields"], block_source.get("label_fields"), executions.get(next_row.id), block_source)
                        next_item.pop("_cursor", None)
                        order_field = block_source["order"]["field"]
                        next_item["fields"] = [{
                            "label": _field_label(order_field),
                            "format": "date",
                            "value": _value_at(next_row.state or {}, order_field),
                        }]
                        if calendar and block["key"] == calendar["rollup_block"]:
                            next_item["calendar_at"] = _value_at(next_row.state or {}, calendar["date_field"])
                total = await _planned_total(db, workspace, user, config["data_sources"], block, rollup["total"],
                                             source_context)
                item = {"id": f"rollup:{block['source']}", "total": total,
                        "next": next_item,
                        **({"counts_by_stage": rollup["counts_by_stage"], "counts_include_execution": True}
                           if block["count_by_stage"] else {})}
                page, next_cursor = [item], None
            elif block["type"] == "records" and block.get("metrics_source"):
                metric_source = config["data_sources"][block["metrics_source"]]
                gated = visibility and block["key"] == visibility["block"]
                if gated:
                    items, ancestors = eligible_rows, eligible_ancestors
                else:
                    all_rows = await _all_source_rows(db, workspace, user, config["data_sources"], block["source"],
                                                       source_context, cache, window=window)
                    ancestors = await _record_ancestor_states(db, workspace, user, config["data_sources"], block["source"], all_rows)
                    items = _deduplicate_publications(all_rows, subject_source=metric_source["subject"], ancestors=ancestors)
                items, next_cursor = _page(items, cursor, effective_limit)
                configured_only = block.get("metrics_display") == "configured"
                # "zero": every declared counter shows, an unread one as 0, with no tracking notice.
                zero_when_empty = block.get("metrics_display") == "zero"
                metrics = await _publication_metric_items(db, workspace, user, metric_source, items, ancestors, window=window,
                                                         configured_only=configured_only,
                                                         stat_ids=eligible_stat_ids if gated and visibility["when"] == "has_observations" else None)
                for row in items:
                    row_metrics = metrics.get(str(row.get("id")), [])
                    row["metrics"] = row_metrics
                    row["published_at"] = row.get("record", {}).get("published_at")
                    configured_keys = set(metric_source["metric_keys"])
                    found_keys = {metric.get("metric_key") for metric in row_metrics
                                  if metric.get("id") != f"{row.get('id')}:{metric.get('metric_key')}"}
                    row["tracking_status"] = "pending" if not found_keys else "configured" if configured_only or found_keys == configured_keys else "partial"
                    if zero_when_empty:
                        row["tracking_status"] = "configured"
                        for metric in row_metrics:
                            if metric.get("value") is None:
                                metric.update(value=0, status=None, freshness=None, delta_reason=None)
            else:
                narrowing = _block_narrowing(block, filters, query) if block_key and context_kind == "workspace" else None
                items = await _source_rows(db, workspace, user, config["data_sources"], block["source"],
                                           source_context, cache,
                                           after_id=cursor, page_limit=effective_limit + 1, window=window,
                                           narrowing=narrowing)
                next_cursor = None
                if block["type"] == "packs" and block.get("plan") and not cursor:
                    _plan_packs(items, await _source_rows(db, workspace, user, config["data_sources"], block["plan"],
                                                          source_context, cache, page_limit=101, window=window))
                if block["type"] == "gallery" and block_source["type"] != "workflow_results":
                    items = await _gallery_tiles(db, workspace, user, block, items)
                if block.get("totals") and not cursor:
                    extra["totals"] = await _block_totals(db, workspace, user, block_source, block, narrowing)
            if block["type"] == "chart" or extra.get("tree_level"):
                items = []
            if calendar and block["key"] in {calendar["block"], *calendar.get("extra_blocks", [])} and block["type"] == "progress":
                for item in items:
                    fields = item.get("fields", {})
                    item["calendar_at"] = fields.get(".".join(calendar["date_field"])) if isinstance(fields, dict) else None
                    # A schedule's row: the value (a platform) the week is laid out by.
                    lane = fields.get(".".join(calendar["lane_field"])) if calendar.get("lane_field") and isinstance(fields, dict) else None
                    if isinstance(lane, str) and lane.strip():
                        item["calendar_lane"] = lane.strip()[:80]
            if block.get("preview") and block_source.get("record_schema") == "publication-receipt/v1":
                # A receipt's post is its project: the words and files that went out are read there.
                if not block.get("metrics_source"):
                    ancestors = await _record_ancestor_states(db, workspace, user, config["data_sources"], block["source"], items)
                for row in items:
                    state = ancestors.get(row.get("id"), {}).get(block_source["parent"], {}).get("state")
                    if isinstance(state, dict):
                        row["_preview_state"] = state
            if block["type"] == "records" and block.get("record_display"):
                if not block.get("metrics_source"):
                    ancestors = await _record_ancestor_states(db, workspace, user, config["data_sources"], block["source"], items)
                display = block["record_display"]
                media = await _record_media(db, workspace, user, config["data_sources"], display.get("media", []), ancestors, items)
                for row in items:
                    row_ancestors = ancestors.get(row.get("id"), {})
                    title = display.get("title")
                    if isinstance(title, dict):
                        row["display_title"] = _value_at(row_ancestors.get(title["source"], {}).get("state", {}), title["field"])
                    row["display_fields"] = []
                    for entry in display.get("fields", []):
                        state = row_ancestors.get(entry["source"], {}).get("state", {})
                        value = _value_at(state, entry["field"])
                        if entry.get("format") == "provider" or entry.get("secondary"):
                            presented = _present_configured_field(
                                {**entry, "format": entry.get("format", "text"), "hide_when_empty": True},
                                value, lambda path, state=state: _value_at(state, path),
                            )
                            if presented is not None:
                                row["display_fields"].append(presented)
                            continue
                        if isinstance(value, str):
                            value = entry.get("labels", {}).get(value, value)
                        row["display_fields"].append({"label": entry["label"], "value": value})
                    if media.get(row["id"]):
                        row["media"] = media[row["id"]]
            if block["type"] == "metrics":
                from packages.core.stats.publication_reporting import compare_publication_stats
                days = {"7d": 7, "30d": 30, "90d": 90}.get(window or "7d", 7)
                end = datetime.now(UTC)
                stats = [row["_stat"] for row in items if row.get("_stat") is not None]
                observations = sorted((obs for row in items for obs in row.get("_observations", [])),
                                      key=lambda observation: (observation.observed_at, observation.id))
                compared = compare_publication_stats(stats, observations,
                                                     window_start=end - timedelta(days=days), window_end=end)
                by_id = {item["stat_id"]: item for item in compared["stats"]}
                for row in items:
                    if row.get("id") in by_id:
                        row["comparison"] = by_id[row["id"]]
            if block["type"] in {"summary", "project_rollup", "chart"} or extra.get("tree_level"):
                pass
            elif block["type"] == "records" and block.get("metrics_source"):
                records = await _named_records(db, workspace, user, block.get("fields"), items)
                page = _present_items(block, items, window=window, records=records)
            else:
                records = await _named_records(db, workspace, user, block.get("fields"), items)
                page, next_cursor = _page(_present_items(block, items, window=window, records=records),
                                          None, effective_limit)
                if next_cursor is None:
                    next_cursor = getattr(items, "next_cursor", None)
                if block["type"] == "gallery" and (block.get("media") or {}).get("details"):
                    await _with_media_details(db, user, page)
                if tree_children:
                    # Each record says how many projects sit under it, so it opens only where there is something.
                    counts = await _child_counts(db, user, workspace, config["data_sources"][tree_children[0]],
                                                 [item["id"] for item in page if isinstance(item.get("id"), str)])
                    for item in page:
                        item["child_count"] = counts.get(item.get("id"), 0)
            for item in page:
                item.pop("_cursor", None)
            # The values a person can filter by, read once with the first page.
            choices = await _filter_choices(db, entity_id=user.entity_id, workspace_id=workspace.id,
                                            source=block_source, block=block) if block.get("filters") and not cursor else None
            # A board's columns follow the stages its source lists, an empty one included.
            stages = block_source.get("stages") if block.get("view") == "board" else None
            blocks.append({"key": block["key"], "type": block["type"], "source": block["source"],
                           "title": block.get("title"), "status": "ready" if page else "empty", "items": page,
                           "next_cursor": next_cursor, **({"filters": choices} if choices is not None else {}),
                           **({"stages": list(stages)} if stages else {}), **extra})
        except AppDataError as exc:
            errors.append(exc.payload())
            blocks.append({"key": block["key"], "type": block["type"], "source": block["source"],
                           "title": block.get("title"), "status": "error", "items": [], "next_cursor": None,
                           "error": exc.payload()})
    return {"component_key": component_key, "context": {"kind": context_kind, "id": context_id},
            **({"visible": True} if visibility else {}),
            "status": "error" if errors and not any(block["status"] == "ready" for block in blocks) else "ready",
            "blocks": blocks, "errors": errors}
