"""Workspace records (Workflow projects) picked in a Workflow form.

A run input or an input wait field picks records a Workflow of the same
Workspace created, by the ``workflow_project_select`` control. A string schema
picks one; an array of strings picks several, with ``minItems``/``maxItems``::

    {"type": "string", "x-ui": {"control": "workflow_project_select", "project_type": "dish"}}
    {"type": "array", "items": {"type": "string"}, "minItems": 1,
     "x-ui": {"control": "workflow_project_select", "project_type": "dish"}}

``allowed_stages`` optionally limits the offer to projects in those stages. The
value is each project's id. Every id a person submits must name a project of
that type in the run's Workspace.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping

PROJECT_SELECT_CONTROL = "workflow_project_select"
MAX_PROJECT_CHOICES = 500


@dataclass(frozen=True)
class ProjectSelect:
    project_type: str
    stages: tuple[str, ...] = ()
    multiple: bool = False


def project_select(schema: Any) -> ProjectSelect | None:
    """The picker a schema declares, else ``None``."""
    if not isinstance(schema, Mapping):
        return None
    ui = schema.get("x-ui")
    if not isinstance(ui, Mapping) or ui.get("control") != PROJECT_SELECT_CONTROL:
        return None
    project_type = ui.get("project_type")
    if not isinstance(project_type, str) or not project_type.strip() or "{{" in project_type:
        return None
    items = schema.get("items") if isinstance(schema.get("items"), Mapping) else {}
    multiple = schema.get("type") == "array" and items.get("type") == "string"
    if not multiple and schema.get("type") not in {None, "string"}:
        return None
    stages = ui.get("allowed_stages")
    stages = tuple(dict.fromkeys(
        str(stage).strip() for stage in (stages if isinstance(stages, list) else []) if str(stage or "").strip()
    ))
    return ProjectSelect(project_type=project_type.strip(), stages=stages, multiple=multiple)


def project_title(project: Any) -> str:
    """How a project reads in a picker or an App ``record`` field: its publication title, topic,
    title, name or the title its Workflow displays, else its key."""
    from packages.core.contracts.project_display import project_display

    state = project.state if isinstance(project.state, dict) else {}
    publication = state.get("publication")
    topic = state.get("topic")
    title = str(publication.get("title") or "").strip() if isinstance(publication, dict) else ""
    if not title and isinstance(topic, dict):
        title = str(topic.get("selected_topic") or "").strip()
    title = (title or str(state.get("title") or state.get("name") or "").strip()
             or project_display(state).get("title") or str(project.project_key or "").strip() or project.id)
    # A numbered project (a series episode) reads as "E01 · title".
    code = str(state.get("code") or "").strip()
    if code and not title.startswith(code):
        title = f"{code} · {title}"
    return title[:200]


async def list_projects(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str,
    project_type: str,
    stages: Iterable[str] = (),
    limit: int = MAX_PROJECT_CHOICES,
) -> list[Any]:
    """Projects of one type in a Workspace, most recently updated first."""
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowProject

    query = select(WorkflowProject).where(
        WorkflowProject.entity_id == entity_id,
        WorkflowProject.workspace_id == workspace_id,
        WorkflowProject.project_type == project_type,
    )
    stages = [stage for stage in stages if stage]
    if stages:
        query = query.where(WorkflowProject.current_stage.in_(stages))
    query = query.order_by(WorkflowProject.updated_at.desc(), WorkflowProject.id.desc()).limit(limit)
    return list((await db.execute(query)).scalars().all())


async def load_project_choices(
    db: Any, picker: ProjectSelect, *, entity_id: str, workspace_id: str | None,
) -> list[dict[str, str]]:
    """``{value, label, description}`` for each project the picker offers."""
    if not workspace_id:
        return []
    projects = await list_projects(
        db, entity_id=entity_id, workspace_id=workspace_id,
        project_type=picker.project_type, stages=picker.stages,
    )
    return [{"value": project.id, "label": project_title(project), "description": project.current_stage}
            for project in projects]


def selected_project_ids(raw: Any) -> list[str]:
    """The project ids a picked value holds, in order: one id, a list of them, or a list's JSON text."""
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:  # a json run input may arrive as its JSON text
            raw = json.loads(raw)
        except ValueError:
            pass
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


async def project_selection_errors(
    db: Any,
    fields: Iterable[Mapping[str, Any]],
    values: Mapping[str, Any],
    *,
    entity_id: str,
    workspace_id: str | None,
) -> dict[str, str]:
    """``{field key: message}`` for picked ids that are not projects of the field's type.

    ``fields`` are ``{key, schema, hidden?}`` rows: run inputs or the
    properties of an input wait. Hidden fields are filled by the Workflow
    itself, never picked, so they are not checked. An empty selection is left
    to the schema's own ``required``/``minItems``.
    """
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowProject

    errors: dict[str, str] = {}
    for field in fields:
        key = str(field.get("key") or "")
        if field.get("hidden") or key not in values:
            continue
        picker = project_select(field.get("schema"))
        selected = selected_project_ids(values[key]) if picker else []
        if picker is None or not selected:
            continue
        found: set[str] = set()
        if workspace_id:
            found = set((await db.execute(select(WorkflowProject.id).where(
                WorkflowProject.id.in_(selected),
                WorkflowProject.entity_id == entity_id,
                WorkflowProject.workspace_id == workspace_id,
                WorkflowProject.project_type == picker.project_type,
            ))).scalars().all())
        missing = [value for value in selected if value not in found]
        if missing:
            errors[key] = (f"Not an existing {picker.project_type} of this Workspace: "
                           + ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
                           + ". Pick from the records it offers.")
    return errors


async def input_wait_project_selection_errors(
    db: Any,
    input_schema: Any,
    value: Any,
    *,
    entity_id: str,
    workspace_id: str | None,
) -> dict[str, str]:
    """The same check over the fields of an answered input wait."""
    properties = input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    if not isinstance(properties, Mapping) or not isinstance(value, Mapping):
        return {}
    fields = [{"key": key, "schema": child} for key, child in properties.items()]
    return await project_selection_errors(db, fields, value, entity_id=entity_id, workspace_id=workspace_id)
