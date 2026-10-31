"""Project live Workspace capabilities into its app panel."""
from __future__ import annotations

import json
import re
from typing import Any

from packages.core.services.viewer_language import localized, sanitize_i18n

_ACTION_KEY = re.compile(r"^(workflow|service|automation|action):[A-Za-z0-9_.:-]{1,128}$")
_FIELD_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_DATA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_DATA_SOURCE_TYPES = {
    "workflow_projects", "related_projects", "project_records", "project_documents",
    "publication_stats", "workspace_stats", "owned_automations",
    # A project's own folder (``state.project_root``) and the references it
    # holds or inherits (e.g. the voices a series pins for its episodes).
    "project_files", "project_references",
    # What a Flow's runs made (a song, a music video), read from each run's own
    # results: the latest version of each, never a failed or cancelled attempt.
    "workflow_results",
}
_DATA_BLOCK_TYPES = {"progress", "records", "documents", "metrics", "summary", "project_rollup", "references", "packs",
                     "gallery"}
PUBLICATION_STAT_COLLECTOR_TYPES = ("public_post", "manual")
DEFAULT_PUBLICATION_STAT_COLLECTOR_TYPES = ["public_post"]
_DATA_CONTEXT_KINDS = {"project", "launch_run", "workspace"}
# The options a data_view's context and each of its blocks accept.
_DATA_CONTEXT_OPTIONS = ("kind", "source", "label", "display", "cover", "progress")
_DATA_CONTEXT_DISPLAYS = ("select", "cards")
_DATA_DISPLAY_LAYOUTS = ("stack", "tabs")
_DATA_BLOCK_OPTIONS = (
    "key", "type", "source", "title", "fields", "metric_keys", "limit", "metrics_source", "metrics_display",
    "record_display", "count_by_stage", "first", "empty_message", "completion", "stage_labels", "plan",
    "item_action", "plan_action",
)


async def load_workspace_app_data_catalog(db: Any, workspace: Any, *, user: Any) -> dict[str, Any]:
    """Return the actor-readable native catalog used to validate App reads.

    Internal producer definitions are retained here because they describe a
    read contract; callers must not use this catalog as an executable target
    catalog.  The normal App runner projection remains separate.
    """
    from sqlalchemy import select
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.models.permission import ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, readable_resource_ids

    definitions = list((await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.entity_id == user.entity_id,
        WorkflowDefinition.workspace_id == workspace.id,
    ))).scalars())
    readable = await readable_resource_ids(
        db, descriptors=[ResourceDescriptor.from_row(row, ResourceType.WORKFLOW) for row in definitions],
        entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    definitions = [row for row in definitions if row.id in readable]
    bindings = set((await db.execute(select(WorkflowBinding.id).where(
        WorkflowBinding.entity_id == user.entity_id, WorkflowBinding.workspace_id == workspace.id,
    ))).scalars())
    stats = set((await db.execute(select(WorkspaceStat.key).where(
        WorkspaceStat.entity_id == user.entity_id, WorkspaceStat.workspace_id == workspace.id,
    ))).scalars())
    return {"workflow_definitions": {row.id for row in definitions}, "workflow_bindings": bindings,
            "stat_keys": stats,
            "workflows": [{"id": row.id, "slug": row.id, "steps": row.steps or []} for row in definitions]}


def _short_text(value: Any, *, field: str, limit: int, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not (1 <= len(value.strip()) <= limit):
        raise ValueError(f"{field} must be {'1–' if required else 'at most '}{limit} characters")
    return value.strip()


def _path(value: Any, *, field: str) -> list[str]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 8
            or any(not isinstance(part, str) or not _DATA_KEY.fullmatch(part) for part in value)):
        raise ValueError(f"{field} must be a bounded field path")
    return list(value)


def _result_path(value: Any, *, field: str) -> list[str]:
    """A run-variable path; it may start at the run's final result (``__result``)."""
    if isinstance(value, list) and value[:1] == ["__result"]:
        return ["__result", *(_path(value[1:], field=field) if len(value) > 1 else [])]
    return _path(value, field=field)


def _secondary_paths(item: dict[str, Any], *, field: str) -> dict[str, list[list[str]]]:
    """Optional fallback paths shown beside a field value; the first non-empty one wins."""
    if "secondary" not in item:
        return {}
    paths = item["secondary"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 4:
        raise ValueError(f"{field} must be 1–4 field paths")
    return {"secondary": [_path(path, field=field) for path in paths]}


def app_data_project_sources(sources: dict[str, Any], name: str) -> list[str]:
    """Declared project owner and ancestors of a record, including project rows."""
    source = sources[name]
    current = source["parent"] if source["type"] == "project_records" else name
    result: list[str] = []
    while current not in result:
        source = sources[current]
        if source["type"] not in {"workflow_projects", "related_projects"}:
            return []
        result.append(current)
        if source["type"] == "workflow_projects":
            return result
        current = source["parent"]
    return []


def normalize_app_view_data_contract(
    data_sources: Any, components: Any, *, catalog: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Normalize the declarative, non-executable App result-view contract.

    ``catalog`` is intentionally optional: Blueprint and Draft callers provide
    their portable/live resource catalog to validate producer references, while
    persisted legacy App configs still normalize without a catalog.
    """
    sources = {} if data_sources is None else data_sources
    if not isinstance(sources, dict) or len(sources) > 32:
        raise ValueError("app_view.data_sources must be an object with at most 32 entries")
    normalized_sources: dict[str, dict[str, Any]] = {}
    for key, raw in sources.items():
        if not isinstance(key, str) or not _DATA_KEY.fullmatch(key) or not isinstance(raw, dict):
            raise ValueError("app_view.data_sources contains an invalid source")
        source_type = raw.get("type")
        if source_type not in _DATA_SOURCE_TYPES:
            raise ValueError("app_view.data_sources contains an unsupported source type")
        allowed = {
            "type", "project_type", "schema_versions", "producer_workflows", "launch_workflows",
            "parent", "parent_state_path", "record_path", "record_schema", "document_path",
            "document_role", "subject", "metric_keys", "collector_types", "stat_keys", "fields", "label_fields",
            "stages", "order", "execution_run_id_path", "reference_kind", "results_only", "media",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"app_view.data_sources.{key} contains unsupported fields")
        source_allowed = {
            "workflow_projects": {"type", "project_type", "schema_versions", "producer_workflows", "launch_workflows", "fields", "label_fields", "stages", "order", "execution_run_id_path"},
            "related_projects": {"type", "project_type", "schema_versions", "producer_workflows", "parent", "parent_state_path", "fields", "label_fields", "stages", "order", "execution_run_id_path"},
            "project_records": {"type", "producer_workflows", "parent", "record_path", "record_schema", "fields"},
            "project_documents": {"type", "producer_workflows", "parent", "document_path", "document_role", "fields"},
            "publication_stats": {"type", "producer_workflows", "parent", "subject", "metric_keys", "collector_types", "fields"},
            "workspace_stats": {"type", "producer_workflows", "stat_keys", "fields"},
            "owned_automations": {"type", "producer_workflows", "parent", "fields"},
            "project_files": {"type", "producer_workflows", "parent", "fields", "results_only"},
            "project_references": {"type", "producer_workflows", "parent", "reference_kind", "fields"},
            "workflow_results": {"type", "producer_workflows", "fields", "label_fields", "media"},
        }[source_type]
        if set(raw) - source_allowed:
            raise ValueError(f"app_view.data_sources.{key} uses fields incompatible with its source type")
        item: dict[str, Any] = {"type": source_type}
        if source_type in {"workflow_projects", "related_projects"}:
            item["project_type"] = _short_text(raw.get("project_type"), field=f"app_view.data_sources.{key}.project_type", limit=80, required=True)
            versions = raw.get("schema_versions", [1])
            if not isinstance(versions, list) or not versions or len(versions) > 16 or any(type(v) is not int or not 1 <= v <= 999 for v in versions):
                raise ValueError("app_view data source schema_versions must be bounded positive integers")
            item["schema_versions"] = sorted(set(versions))
            if source_type == "related_projects":
                parent = raw.get("parent")
                if not isinstance(parent, str) or parent not in sources:
                    raise ValueError("related_projects requires a declared parent source")
                item["parent"] = parent
                item["parent_state_path"] = _path(raw.get("parent_state_path", ["parent_project_id"]), field="related_projects.parent_state_path")
            else:
                launch = raw.get("launch_workflows", [])
                if not isinstance(launch, list) or len(launch) > 16:
                    raise ValueError("launch_workflows must be a bounded array")
                item["launch_workflows"] = []
                for entry in launch:
                    if not isinstance(entry, dict) or set(entry) - {"workflow", "project_steps"}:
                        raise ValueError("launch_workflows contains an invalid entry")
                    workflow = entry.get("workflow")
                    steps = entry.get("project_steps")
                    if not isinstance(workflow, str) or not workflow.startswith("workflow:") or not isinstance(steps, list) or not steps or len(steps) > 32 or any(not isinstance(step, str) or not _DATA_KEY.fullmatch(step) for step in steps):
                        raise ValueError("launch_workflows requires a workflow reference and step ids")
                    item["launch_workflows"].append({"workflow": workflow, "project_steps": list(steps)})
            if "execution_run_id_path" in raw:
                item["execution_run_id_path"] = _path(
                    raw["execution_run_id_path"], field="project execution run id path",
                )
        if source_type in {"project_records", "project_documents", "publication_stats", "owned_automations",
                           "project_files", "project_references"}:
            parent = raw.get("parent")
            if not isinstance(parent, str) or parent not in sources:
                raise ValueError(f"{source_type} requires a declared parent source")
            item["parent"] = parent
        if source_type == "project_records":
            item["record_path"] = _path(raw.get("record_path"), field="project_records.record_path")
            # A versioned record (a publication receipt) names its schema; a plain
            # list kept in the project (a series' episode plan) names none.
            record_schema = _short_text(raw.get("record_schema"), field="project_records.record_schema", limit=120)
            if record_schema is not None:
                item["record_schema"] = record_schema
        if source_type == "project_files" and "results_only" in raw:
            # Only what was delivered: a file whose artifact role marks how it
            # was made (a brief snapshot, a raw take) is left out.
            if type(raw["results_only"]) is not bool:
                raise ValueError("project_files.results_only must be boolean")
            item["results_only"] = raw["results_only"]
        if source_type == "project_references":
            kind = raw.get("reference_kind", "voices")
            if not isinstance(kind, str) or not _DATA_KEY.fullmatch(kind):
                raise ValueError("project_references.reference_kind must be an identifier")
            item["reference_kind"] = kind
        if source_type == "project_documents":
            item["document_path"] = _path(raw.get("document_path"), field="project_documents.document_path")
            item["document_role"] = _short_text(raw.get("document_role"), field="project_documents.document_role", limit=80)
        if source_type == "publication_stats":
            subject = raw.get("subject")
            if not isinstance(subject, str) or subject not in sources:
                raise ValueError("publication_stats requires a declared subject source")
            metric_keys = raw.get("metric_keys", [])
            if not isinstance(metric_keys, list) or not metric_keys or len(metric_keys) > 20 or any(not isinstance(metric, str) or not _DATA_KEY.fullmatch(metric) for metric in metric_keys):
                raise ValueError("publication_stats requires bounded metric_keys")
            item.update(subject=subject, metric_keys=list(dict.fromkeys(metric_keys)))
            # Publication metrics may come from the automatic public-post
            # collector or from evidence-backed manual assessments bound to the
            # same receipt identity. Omitted keeps the automatic collector only.
            if "collector_types" in raw:
                collector_types = raw["collector_types"]
                if (not isinstance(collector_types, list) or not collector_types
                        or any(value not in PUBLICATION_STAT_COLLECTOR_TYPES for value in collector_types)):
                    raise ValueError("publication_stats.collector_types must list public_post and/or manual")
                item["collector_types"] = list(dict.fromkeys(collector_types))
        if source_type == "workspace_stats":
            stat_keys = raw.get("stat_keys", [])
            if not isinstance(stat_keys, list) or not stat_keys or len(stat_keys) > 50 or any(not isinstance(stat, str) or not 1 <= len(stat) <= 120 for stat in stat_keys):
                raise ValueError("workspace_stats requires bounded stat_keys")
            item["stat_keys"] = list(dict.fromkeys(stat_keys))
        fields = raw.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 24:
            raise ValueError("data source fields must be a bounded array")
        path_of = _result_path if source_type == "workflow_results" else _path
        item["fields"] = [path_of(path, field="data_sources.fields") for path in fields]
        if source_type in {"workflow_projects", "related_projects"}:
            stages = raw.get("stages")
            if stages is not None:
                if (not isinstance(stages, list) or not stages or len(stages) > 32
                        or any(not isinstance(stage, str) or not stage.strip() or len(stage) > 80 for stage in stages)):
                    raise ValueError("project data source stages must be a bounded array of stage names")
                item["stages"] = list(dict.fromkeys(stage.strip() for stage in stages))
            order = raw.get("order")
            if order is not None:
                if not isinstance(order, dict) or set(order) != {"field", "type", "direction"}:
                    raise ValueError("project data source order must declare field, type, and direction")
                field = _path(order.get("field"), field="project data source order field")
                if field not in item["fields"] or order.get("type") != "datetime" or order.get("direction") not in {"asc", "desc"}:
                    raise ValueError("project data source order requires a declared datetime field and direction")
                item["order"] = {"field": field, "type": "datetime", "direction": order["direction"]}
        if "label_fields" in raw:
            label_fields = raw["label_fields"]
            if not isinstance(label_fields, list) or not 1 <= len(label_fields) <= 3:
                raise ValueError("data source label_fields must contain 1–3 field paths")
            item["label_fields"] = [(_result_path if source_type == "workflow_results" else _path)(
                path, field="data_sources.label_fields") for path in label_fields]
            if any(path not in item["fields"] for path in item["label_fields"]):
                raise ValueError("data source label_fields must be declared in fields")
        producers = raw.get("producer_workflows", [])
        if not isinstance(producers, list) or len(producers) > 32 or any(not isinstance(ref, str) or not ref.startswith("workflow_definition:") for ref in producers):
            raise ValueError("producer_workflows must contain workflow definition references")
        if source_type in {"workflow_projects", "related_projects", "workflow_results"} and not producers:
            raise ValueError(f"{source_type} data sources require producer_workflows")
        if source_type == "workflow_results":
            # The files a result is: each a run-variable path to a Knowledge document id.
            media = raw.get("media", [])
            if not isinstance(media, list) or len(media) > 2:
                raise ValueError("workflow_results media must be at most 2 entries")
            item["media"] = []
            for entry in media:
                if not isinstance(entry, dict) or set(entry) - {"field", "label"}:
                    raise ValueError("workflow_results media entries have a field and an optional label")
                label = _short_text(entry.get("label"), field="workflow_results media label", limit=80)
                item["media"].append({"field": _result_path(entry.get("field"), field="workflow_results media field"),
                                      **({"label": label} if label else {})})
        item["producer_workflows"] = list(dict.fromkeys(producers))
        normalized_sources[key] = item

    def source_dependencies(name: str, trail: set[str]) -> None:
        if name in trail:
            raise ValueError("app_view.data_sources must not contain dependency cycles")
        source = normalized_sources[name]
        for dependency in (source.get("parent"), source.get("subject")):
            if dependency:
                source_dependencies(dependency, trail | {name})
    for source_name in normalized_sources:
        source_dependencies(source_name, set())
    for source in normalized_sources.values():
        if (source["type"] in {"related_projects", "project_files", "project_references"}
                and normalized_sources[source["parent"]]["type"] not in {"workflow_projects", "related_projects"}):
            raise ValueError(f"{source['type']} requires a project source parent")

    raw_components = [] if components is None else components
    if not isinstance(raw_components, list) or len(raw_components) > 16:
        raise ValueError("app_view.components must be an array of at most 16 components")
    normalized_components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_components:
        if not isinstance(raw, dict) or set(raw) - {"type", "schema_version", "key", "title", "description", "context", "blocks", "display", "visibility"}:
            raise ValueError("app_view.components contains an invalid component")
        key = raw.get("key")
        if not isinstance(key, str) or not _DATA_KEY.fullmatch(key) or key in seen:
            raise ValueError("app_view component keys must be unique identifiers")
        if raw.get("type") != "data_view" or raw.get("schema_version", 1) != 1:
            raise ValueError("app_view supports data_view components at schema version 1")
        context = raw.get("context")
        if not isinstance(context, dict) or set(context) - set(_DATA_CONTEXT_OPTIONS) or context.get("kind") not in _DATA_CONTEXT_KINDS:
            raise ValueError("app_view component context is invalid")
        source = context.get("source")
        project_source_types = {"workflow_projects"} if context["kind"] == "launch_run" else {"workflow_projects", "related_projects"}
        if context["kind"] in {"project", "launch_run"} and (not isinstance(source, str) or source not in normalized_sources or normalized_sources[source]["type"] not in project_source_types):
            raise ValueError("project and launch-run component contexts require a project source")
        if source is not None and (not isinstance(source, str) or source not in normalized_sources):
            raise ValueError("app_view component context references an unknown source")
        blocks = raw.get("blocks")
        if not isinstance(blocks, list) or not blocks or len(blocks) > 12:
            raise ValueError("app_view components require bounded blocks")
        normalized_blocks = []
        block_keys: set[str] = set()
        for block in blocks:
            if not isinstance(block, dict) or set(block) - set(_DATA_BLOCK_OPTIONS):
                raise ValueError("app_view component contains an invalid block")
            bkey, btype, bsource = block.get("key"), block.get("type"), block.get("source")
            if not isinstance(bkey, str) or not _DATA_KEY.fullmatch(bkey) or bkey in block_keys or btype not in _DATA_BLOCK_TYPES or not isinstance(bsource, str) or bsource not in normalized_sources:
                raise ValueError("app_view component block references are invalid")
            limit = block.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("app_view component block limit must be 1–100")
            block_keys.add(bkey)
            metrics_source = block.get("metrics_source")
            if metrics_source is not None and (btype != "records" or not isinstance(metrics_source, str) or metrics_source not in normalized_sources):
                raise ValueError("app_view records metrics_source is invalid")
            metrics_display = block.get("metrics_display")
            if metrics_display is not None and (metrics_source is None or not isinstance(metrics_display, str) or metrics_display not in {"declared", "configured"}):
                raise ValueError("app_view records metrics_display requires a metrics source and a supported mode")
            block_fields = block.get("fields", [])
            count_by_stage = block.get("count_by_stage", False)
            first = block.get("first")
            empty_message = _short_text(block.get("empty_message"), field="component block empty_message", limit=300)
            stage_labels = block.get("stage_labels")
            if stage_labels is not None:
                if btype not in {"project_rollup", "progress", "packs"}:
                    raise ValueError("app_view stage_labels require a project_rollup, progress or packs block")
                if not isinstance(stage_labels, dict) or len(stage_labels) > 32:
                    raise ValueError("app_view stage_labels must be a bounded object")
                normalized_stage_labels: dict[str, str] = {}
                for stage, label in stage_labels.items():
                    if (not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80
                            or not isinstance(label, str) or not label.strip() or len(label.strip()) > 80):
                        raise ValueError("app_view stage_labels entries must be bounded strings")
                    normalized_stage_labels[stage.strip()] = label.strip()
                if len(normalized_stage_labels) != len(stage_labels):
                    raise ValueError("app_view stage_labels must have unique stage names")
                stage_labels = normalized_stage_labels
            # A packs block can list a plan (project records) in its order,
            # each planned child a card before its run starts, and offer one
            # App action per card that opens with the card's project chosen.
            # A project_rollup counts the same plan as its total.
            plan = block.get("plan")
            if plan is not None and (btype not in {"packs", "project_rollup"} or not isinstance(plan, str)
                                     or plan not in normalized_sources
                                     or normalized_sources[plan]["type"] != "project_records"):
                raise ValueError("app_view packs or project_rollup plan must name a project_records source")
            item_action = block.get("item_action")
            if item_action is not None:
                if (btype != "packs" or not isinstance(item_action, dict)
                        or set(item_action) - {"action", "label", "input", "stages"}):
                    raise ValueError("app_view packs item_action is invalid")
                action_key, action_input = item_action.get("action"), item_action.get("input")
                stages = item_action.get("stages", [])
                if (not isinstance(action_key, str) or not _ACTION_KEY.fullmatch(action_key)
                        or not isinstance(action_input, str) or not _DATA_KEY.fullmatch(action_input)
                        or not isinstance(stages, list) or len(stages) > 32
                        or any(not isinstance(stage, str) or not stage.strip() or len(stage) > 80 for stage in stages)):
                    raise ValueError("app_view packs item_action needs an action, an input and bounded stages")
                item_action = {"action": action_key, "input": action_input,
                               "label": _short_text(item_action.get("label"), field="item_action label", limit=40, required=True),
                               **({"stages": [stage.strip() for stage in stages]} if stages else {})}
            # A planned card (no project yet) opens its own App action, its
            # inputs filled from the card: its parent project or its plan code.
            plan_action = block.get("plan_action")
            if plan_action is not None:
                if (btype != "packs" or plan is None or not isinstance(plan_action, dict)
                        or set(plan_action) - {"action", "label", "inputs"}):
                    raise ValueError("app_view packs plan_action needs a plan")
                action_key, action_inputs = plan_action.get("action"), plan_action.get("inputs")
                if (not isinstance(action_key, str) or not _ACTION_KEY.fullmatch(action_key)
                        or not isinstance(action_inputs, dict) or not 1 <= len(action_inputs) <= 4
                        or any(not isinstance(key, str) or not _DATA_KEY.fullmatch(key) or value not in {"parent", "code"}
                               for key, value in action_inputs.items())):
                    raise ValueError("app_view packs plan_action needs an action and inputs from parent or code")
                plan_action = {"action": action_key, "inputs": dict(action_inputs),
                               "label": _short_text(plan_action.get("label"), field="plan_action label", limit=40, required=True)}
            completion = block.get("completion")
            if completion is not None:
                if btype != "project_rollup":
                    raise ValueError("app_view completion requires a project_rollup block")
                if not isinstance(completion, dict) or set(completion) != {"stages", "label"}:
                    raise ValueError("app_view completion is invalid")
                completion_stages = completion.get("stages")
                if (not isinstance(completion_stages, list) or not completion_stages or len(completion_stages) > 32
                        or any(not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80
                               for stage in completion_stages)):
                    raise ValueError("app_view completion requires bounded stage names")
                completion_stages = [stage.strip() for stage in completion_stages]
                if len(set(completion_stages)) != len(completion_stages):
                    raise ValueError("app_view completion stages must be unique")
                completion = {
                    "stages": completion_stages,
                    "label": _short_text(completion.get("label"), field="app_view completion label", limit=100, required=True),
                }
            if btype == "project_rollup":
                if normalized_sources[bsource]["type"] not in {"workflow_projects", "related_projects"}:
                    raise ValueError("project_rollup requires a project source")
                if type(count_by_stage) is not bool:
                    raise ValueError("project_rollup count_by_stage must be boolean")
                if completion is not None and not count_by_stage:
                    raise ValueError("app_view completion requires count_by_stage")
                if first is not None:
                    if (not isinstance(first, dict) or set(first) != {"stages"} or not isinstance(first.get("stages"), list)
                            or not first["stages"] or len(first["stages"]) > 32
                            or any(not isinstance(stage, str) or not stage.strip() or len(stage) > 80 for stage in first["stages"])):
                        raise ValueError("project_rollup first requires bounded stage names")
                    if "order" not in normalized_sources[bsource]:
                        raise ValueError("project_rollup first requires an ordered project source")
                    first = {"stages": list(dict.fromkeys(stage.strip() for stage in first["stages"]))}
                if not count_by_stage and first is None:
                    raise ValueError("project_rollup requires count_by_stage or first")
            # A gallery shows only what a Flow's runs made, and only a gallery shows that.
            if (btype == "gallery") != (normalized_sources[bsource]["type"] == "workflow_results"):
                raise ValueError("app_view gallery blocks read workflow_results sources, and only they do")
            structured_fields = btype == "summary" or (
                btype in {"progress", "gallery"} and isinstance(block_fields, list)
                and any(isinstance(field, dict) for field in block_fields)
            )
            if structured_fields:
                if (not isinstance(block_fields, list) or not block_fields or len(block_fields) > 12
                        or any(not isinstance(item, dict) for item in block_fields)):
                    raise ValueError(f"app_view {btype} fields must be a bounded non-empty array of field definitions")
                summary_fields = []
                for item in block_fields:
                    if not isinstance(item, dict) or set(item) - {"label", "field", "format", "hide_when_empty", "labels", "secondary"}:
                        raise ValueError(f"app_view {btype} field is invalid")
                    field_format = item.get("format", "text")
                    if field_format not in {"text", "status", "date", "link", "link_list", "document", "provider"}:
                        raise ValueError(f"app_view {btype} field format is invalid")
                    labels = item.get("labels", {})
                    if (not isinstance(labels, dict) or len(labels) > 24
                            or any(not isinstance(key, str) or not isinstance(value, str)
                                   or not key.strip() or not value.strip() or len(value.strip()) > 80
                                   for key, value in labels.items())):
                        raise ValueError(f"app_view {btype} field labels are invalid")
                    hide_when_empty = item.get("hide_when_empty", False)
                    if type(hide_when_empty) is not bool:
                        raise ValueError(f"app_view {btype} field hide_when_empty must be boolean")
                    summary_fields.append({
                        "label": _short_text(item.get("label"), field=f"app_view {btype} field label", limit=80, required=True),
                        "field": (_result_path if btype == "gallery" else _path)(
                            item.get("field"), field=f"app_view {btype} field path"),
                        "format": field_format,
                        "hide_when_empty": hide_when_empty,
                        **({"labels": {key.strip(): value.strip() for key, value in labels.items()}} if labels else {}),
                        **_secondary_paths(item, field=f"app_view {btype} field secondary"),
                    })
            else:
                if not isinstance(block_fields, list):
                    raise ValueError("app_view component block fields must be an array")
                summary_fields = None
            raw_display = block.get("record_display")
            if raw_display is not None and (btype != "records" or not isinstance(raw_display, dict)
                                            or set(raw_display) - {"title", "fields", "media"}):
                raise ValueError("app_view records record_display is invalid")
            display: dict[str, Any] | None = None
            if raw_display is not None:
                display = {}
                title = raw_display.get("title")
                if title is not None:
                    if not isinstance(title, dict) or set(title) != {"source", "field"} or not isinstance(title.get("source"), str) or title["source"] not in normalized_sources:
                        raise ValueError("app_view records title display is invalid")
                    display["title"] = {"source": title["source"], "field": _path(title.get("field"), field="record display title field")}
                raw_fields = raw_display.get("fields", [])
                if not isinstance(raw_fields, list) or len(raw_fields) > 12:
                    raise ValueError("app_view records display fields must be bounded")
                display_fields = []
                for item in raw_fields:
                    if not isinstance(item, dict) or set(item) - {"label", "source", "field", "labels", "format", "secondary"} or not isinstance(item.get("source"), str) or item["source"] not in normalized_sources:
                        raise ValueError("app_view records display field is invalid")
                    display_format = item.get("format", "text")
                    if display_format not in {"text", "provider"}:
                        raise ValueError("app_view records display field format is invalid")
                    labels = item.get("labels", {})
                    if (not isinstance(labels, dict) or len(labels) > 24
                            or any(not isinstance(key, str) or not isinstance(value, str)
                                   or not key.strip() or not value.strip() or len(key) > 80 or len(value.strip()) > 80
                                   for key, value in labels.items())):
                        raise ValueError("app_view records display field labels are invalid")
                    display_fields.append({"label": _short_text(item.get("label"), field="record display field label", limit=80, required=True),
                                           "source": item["source"], "field": _path(item.get("field"), field="record display field path"),
                                           **({"labels": {key: value.strip() for key, value in labels.items()}} if labels else {}),
                                           **({"format": display_format} if display_format != "text" else {}),
                                           **_secondary_paths(item, field="record display field secondary")})
                if display_fields:
                    display["fields"] = display_fields
                raw_media = raw_display.get("media", [])
                if not isinstance(raw_media, list) or len(raw_media) > 4:
                    raise ValueError("app_view records media must be a bounded array")
                media = []
                for entry in raw_media:
                    if (not isinstance(entry, dict) or set(entry) - {"source", "label"}
                            or not isinstance(entry.get("source"), str)
                            or normalized_sources.get(entry["source"], {}).get("type") != "project_documents"
                            or any(item["source"] == entry["source"] for item in media)):
                        raise ValueError("app_view records media requires unique document sources")
                    label = _short_text(entry.get("label"), field="record media label", limit=80)
                    media.append({"source": entry["source"], **({"label": label} if label else {})})
                if media:
                    display["media"] = media
            normalized_blocks.append({"key": bkey, "type": btype, "source": bsource,
                                      "title": _short_text(block.get("title"), field="component block title", limit=100),
                                      "fields": summary_fields if btype in {"summary", "progress", "gallery"} and summary_fields is not None else [_path(path, field="component block fields") for path in block_fields],
                                      "metric_keys": list(block.get("metric_keys", [])), "limit": limit,
                                      **({"metrics_source": metrics_source} if metrics_source is not None else {}),
                                      **({"metrics_display": metrics_display} if metrics_display is not None else {}),
                                      **({"record_display": display} if display is not None else {}),
                                      **({"empty_message": empty_message} if empty_message is not None else {}),
                                      **({"stage_labels": stage_labels} if stage_labels is not None else {}),
                                      **({"plan": plan} if plan is not None else {}),
                                      **({"item_action": item_action} if item_action is not None else {}),
                                      **({"plan_action": plan_action} if plan_action is not None else {}),
                                      **({"completion": completion} if completion is not None else {})})
            if btype == "project_rollup":
                normalized_blocks[-1].update(count_by_stage=count_by_stage, **({"first": first} if first is not None else {}))
        visibility = raw.get("visibility")
        if visibility is not None:
            if (not isinstance(visibility, dict) or set(visibility) != {"block", "when"}
                    or visibility.get("when") not in {"has_records", "has_observations"} or context["kind"] != "project"):
                raise ValueError("app_view component visibility requires a project context and a supported gate")
            gate = next((block for block in normalized_blocks if block["key"] == visibility.get("block")), None)
            if (gate is None or gate["type"] != "records"
                    or not app_data_project_sources(normalized_sources, gate["source"])):
                raise ValueError("app_view component visibility requires a project records block")
            if (visibility["when"] == "has_observations" and (not gate.get("metrics_source")
                    or normalized_sources[gate["source"]].get("record_schema") != "publication-receipt/v1")):
                raise ValueError("app_view component visibility requires a publication records block with metrics_source")
            visibility = {"block": gate["key"], "when": visibility["when"]}
        display = raw.get("display", {})
        if not isinstance(display, dict) or set(display) - {"layout", "groups", "hidden", "hidden_blocks", "calendar"}:
            raise ValueError("app_view component display is invalid")
        display_layout = display.get("layout", "stack")
        if display_layout not in _DATA_DISPLAY_LAYOUTS:
            raise ValueError("app_view component display layout is invalid")
        component_hidden = display.get("hidden", False)
        if type(component_hidden) is not bool:
            raise ValueError("app_view component display hidden must be boolean")
        hidden_blocks = display.get("hidden_blocks", [])
        if (not isinstance(hidden_blocks, list) or len(hidden_blocks) > len(normalized_blocks)
                or any(not isinstance(item, str) or not _DATA_KEY.fullmatch(item) for item in hidden_blocks)
                or len(set(hidden_blocks)) != len(hidden_blocks)
                or not set(hidden_blocks).issubset(block_keys)):
            raise ValueError("app_view component display hidden_blocks is invalid")
        groups = display.get("groups", [])
        if not isinstance(groups, list) or len(groups) > 12:
            raise ValueError("app_view component display groups is invalid")
        normalized_groups = []
        group_keys: set[str] = set()
        grouped_blocks: set[str] = set()
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"key", "title", "blocks"}:
                raise ValueError("app_view component display group is invalid")
            group_key = group.get("key")
            group_blocks = group.get("blocks")
            if (not isinstance(group_key, str) or not _DATA_KEY.fullmatch(group_key) or group_key in group_keys
                    or not isinstance(group_blocks, list) or not group_blocks or len(group_blocks) > len(normalized_blocks)
                    or any(not isinstance(item, str) or not _DATA_KEY.fullmatch(item) for item in group_blocks)
                    or len(set(group_blocks)) != len(group_blocks)
                    or not set(group_blocks).issubset(block_keys)
                    or grouped_blocks.intersection(group_blocks)):
                raise ValueError("app_view component display group references are invalid")
            group_keys.add(group_key)
            grouped_blocks.update(group_blocks)
            normalized_groups.append({
                "key": group_key,
                "title": _short_text(group.get("title"), field="component display group title", limit=100, required=True),
                "blocks": list(group_blocks),
            })
        if groups and grouped_blocks != block_keys:
            raise ValueError("app_view component display groups must cover every block exactly once")
        # Projects can be picked from cards instead of a list: each card shows
        # the project's first image reference (``cover``, a references block)
        # and how many of its children are done (``progress``, a rollup block
        # with completion); one opens the component for that project.
        context_display = context.get("display", "select")
        if context_display not in _DATA_CONTEXT_DISPLAYS or (context_display == "cards" and context["kind"] != "project"):
            raise ValueError("app_view component context display must be select, or cards for projects")
        card_options: dict[str, str] = {}
        for option, block_type in (("cover", "references"), ("progress", "project_rollup")):
            value = context.get(option)
            if value is None:
                continue
            target = next((item for item in normalized_blocks if item["key"] == value), None)
            if context_display != "cards" or target is None or target["type"] != block_type or (
                    option == "progress" and not target.get("completion")):
                raise ValueError(f"app_view component context {option} requires cards and a {block_type} block"
                                 + (" with completion" if option == "progress" else ""))
            card_options[option] = value
        if display_layout == "tabs" and not groups:
            raise ValueError("app_view component display tabs require groups")
        calendar = display.get("calendar")
        if calendar is not None:
            if context["kind"] != "project" or not isinstance(calendar, dict) or set(calendar) != {
                "block", "date_field", "rollup_block", "context_block", "attention_stages", "collection_title",
            }:
                raise ValueError("app_view component calendar display is invalid")
            calendar_block = next((block for block in normalized_blocks if block["key"] == calendar.get("block")), None)
            rollup_block = next((block for block in normalized_blocks if block["key"] == calendar.get("rollup_block")), None)
            context_block = next((block for block in normalized_blocks if block["key"] == calendar.get("context_block")), None)
            date_field = _path(calendar.get("date_field"), field="app_view calendar date_field")
            if (
                calendar_block is None or calendar_block["type"] != "progress"
                or normalized_sources[calendar_block["source"]]["type"] not in {"workflow_projects", "related_projects"}
                or bool(normalized_sources[calendar_block["source"]].get("stages"))
                or normalized_sources[calendar_block["source"]].get("order", {}).get("field") != date_field
                or rollup_block is None or rollup_block["type"] != "project_rollup"
                or not rollup_block.get("count_by_stage") or rollup_block["source"] != calendar_block["source"]
                or context_block is None or context_block["type"] != "summary"
                or context_block["source"] != context.get("source")
                or app_data_project_sources(normalized_sources, calendar_block["source"])[-1:] != [context.get("source")]
            ):
                raise ValueError("app_view calendar display references are invalid")
            attention_stages = calendar.get("attention_stages")
            if (not isinstance(attention_stages, list) or len(attention_stages) > 32
                    or any(not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80
                           for stage in attention_stages)):
                raise ValueError("app_view calendar attention_stages must be bounded stage names")
            collection_title = _short_text(calendar.get("collection_title"), field="app_view calendar collection_title", limit=100, required=True)
            calendar = {
                "block": calendar_block["key"], "date_field": date_field,
                "rollup_block": rollup_block["key"], "context_block": context_block["key"],
                "attention_stages": list(dict.fromkeys(stage.strip() for stage in attention_stages)),
                "collection_title": collection_title,
            }
        seen.add(key)
        normalized_components.append({"type": "data_view", "schema_version": 1, "key": key,
                                      "title": _short_text(raw.get("title"), field="component title", limit=100),
                                      "description": _short_text(raw.get("description"), field="component description", limit=300),
                                      "context": {"kind": context["kind"], **({"source": source} if source else {}),
                                                  **({"label": _short_text(context.get("label"), field="component context label", limit=80)} if context.get("label") else {}),
                                                  **({"display": "cards", **card_options} if context_display == "cards" else {})},
                                      "blocks": normalized_blocks,
                                      **({"visibility": visibility} if visibility is not None else {}),
                                      "display": {"layout": display_layout, "groups": normalized_groups,
                                                  "hidden": component_hidden, "hidden_blocks": list(hidden_blocks),
                                                  **({"calendar": calendar} if calendar is not None else {})}})
    def record_ancestors(record_source: str) -> set[str]:
        return set(app_data_project_sources(normalized_sources, record_source))

    for component in normalized_components:
        if component.get("visibility"):
            gate = next(block for block in component["blocks"] if block["key"] == component["visibility"]["block"])
            if component["context"].get("source") not in record_ancestors(gate["source"]):
                raise ValueError("app_view component visibility must use the selected project's records")
        for block in component["blocks"]:
            if block["type"] in {"summary", "progress"} and block["fields"] and isinstance(block["fields"][0], dict):
                source_is_project = normalized_sources[block["source"]]["type"] in {"workflow_projects", "related_projects"}
                if block["type"] == "summary" and (component["context"]["kind"] != "project"
                                                      or component["context"].get("source") != block["source"]
                                                      or not source_is_project):
                    raise ValueError("app_view summary blocks require the selected project source")
                # Progress fields are presented per project row, so any context
                # may list projects with them; summary describes one selection.
                if block["type"] == "progress" and not source_is_project:
                    raise ValueError("app_view progress structured fields require a project source")
                declared = {tuple(path) for path in normalized_sources[block["source"]].get("fields", [])}
                if any(tuple(path) not in declared
                       for field in block["fields"] for path in [field["field"], *field.get("secondary", [])]):
                    raise ValueError(f"app_view {block['type']} fields must be declared by their project source")
                continue
            if block["type"] != "records":
                continue
            ancestors = record_ancestors(block["source"])
            metrics_source = block.get("metrics_source")
            if metrics_source is not None:
                metrics = normalized_sources[metrics_source]
                records = normalized_sources[block["source"]]
                if (records["type"] != "project_records" or records.get("record_schema") != "publication-receipt/v1"
                        or metrics["type"] != "publication_stats" or metrics.get("parent") != block["source"]
                        or metrics.get("subject") not in ancestors):
                    raise ValueError(f"app_view records block {block['key']!r} has an incompatible metrics_source")
            display = block.get("record_display") or {}
            for media in display.get("media", []):
                if normalized_sources[media["source"]]["parent"] not in ancestors:
                    raise ValueError("app_view records media source is not owned by a record ancestor")
            entries = ([display["title"]] if isinstance(display.get("title"), dict) else []) + list(display.get("fields", []))
            for entry in entries:
                if entry["source"] not in ancestors:
                    raise ValueError(f"app_view records block {block['key']!r} display source is not a record ancestor")
                declared = {tuple(path) for path in normalized_sources[entry["source"]].get("fields", [])}
                if any(tuple(path) not in declared for path in [entry["field"], *entry.get("secondary", [])]):
                    raise ValueError(f"app_view records block {block['key']!r} display field is not declared by its source")
    if catalog is not None:
        validate_app_view_data_contract(normalized_sources, normalized_components, catalog=catalog)
    return normalized_sources, normalized_components


def workflow_result_paths(workflows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """What a Flow's runs record: each step's output name, with its output schema when it declares one.

    A run input (``start``'s) and the final result (``__result``) are readable too.
    """
    roots: dict[str, dict[str, Any] | None] = {"__result": None}
    for workflow in workflows:
        for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
            config = step.get("config") if isinstance(step, dict) else None
            if not isinstance(config, dict):
                continue
            for run_input in config.get("run_inputs") or []:
                if isinstance(run_input, dict) and isinstance(run_input.get("key"), str):
                    roots.setdefault(run_input["key"], None)
            name = config.get("output_var")
            if isinstance(name, str) and name:
                schema = config.get("output_schema")
                roots[name] = schema if isinstance(schema, dict) else roots.get(name)
    return roots


def _result_path_declared(roots: dict[str, dict[str, Any] | None], path: list[str]) -> bool:
    """A path starts at an output a step records; under a declared schema it must be declared there."""
    if not path or path[0] not in roots:
        return False
    node = roots[path[0]]
    for part in path[1:]:
        if not isinstance(node, dict) or not isinstance(node.get("properties"), dict):
            return True  # An output without a schema is read as it comes.
        node = node["properties"].get(part)
        if node is None:
            return False
    return True


def validate_app_view_data_contract(data_sources: dict[str, Any], components: list[dict[str, Any]], *, catalog: dict[str, Any]) -> None:
    """Validate portable/live producer references without making them runners."""
    definitions_present = "workflow_definitions" in catalog
    bindings_present = "workflow_bindings" in catalog
    stats_present = "stat_keys" in catalog
    definitions = set(catalog.get("workflow_definitions", ()))
    bindings = set(catalog.get("workflow_bindings", ()))
    stats = set(catalog.get("stat_keys", ()))
    workflows = {
        str(row.get("slug") or row.get("id") or ""): row
        for row in catalog.get("workflows", ()) if isinstance(row, dict)
    }

    def declared_paths(source_name: str, seen: set[str] | None = None) -> set[tuple[str, ...]]:
        seen = set() if seen is None else seen
        if source_name in seen:
            raise ValueError("app_view data sources must not contain dependency cycles")
        seen.add(source_name)
        source = data_sources[source_name]
        refs = list(source.get("producer_workflows", []))
        parent = source.get("parent")
        if parent:
            refs += list(data_sources[parent].get("producer_workflows", []))
        paths: set[tuple[str, ...]] = set()
        for ref in refs:
            workflow = workflows.get(ref.removeprefix("workflow_definition:"))
            if workflow is None:
                continue
            for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
                config = step.get("config") if isinstance(step, dict) else None
                if not isinstance(config, dict) or config.get("project_type") != source.get("project_type"):
                    continue
                schema = config.get("state_schema")
                if not isinstance(schema, dict):
                    continue
                properties = schema.get("properties")
                if not isinstance(properties, dict):
                    continue
                def walk(node: Any, prefix: tuple[str, ...] = ()) -> None:
                    if not isinstance(node, dict):
                        return
                    props = node.get("properties")
                    if not isinstance(props, dict):
                        paths.add(prefix)
                        return
                    # Object-valued receipt fields are declared values too;
                    # derived record sources may select the object itself.
                    if prefix:
                        paths.add(prefix)
                    for name, child in props.items():
                        if isinstance(name, str):
                            walk(child, prefix + (name,))
                for key, child in properties.items():
                    walk(child, (key,))
        return paths

    def declared_types(source_name: str) -> dict[tuple[str, ...], set[str]]:
        source = data_sources[source_name]
        refs = list(source.get("producer_workflows", []))
        if source.get("parent"):
            refs += list(data_sources[source["parent"]].get("producer_workflows", []))
        result: dict[tuple[str, ...], set[str]] = {}
        for ref in refs:
            workflow = workflows.get(ref.removeprefix("workflow_definition:"))
            if workflow is None:
                continue
            for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
                config = step.get("config") if isinstance(step, dict) else None
                if not isinstance(config, dict) or config.get("project_type") != source.get("project_type"):
                    continue
                schema = config.get("state_schema")
                if not isinstance(schema, dict):
                    continue
                def walk(node: Any, prefix: tuple[str, ...] = ()) -> None:
                    if not isinstance(node, dict):
                        return
                    raw_type = node.get("type")
                    kinds = set(raw_type if isinstance(raw_type, list) else [raw_type]) - {None, "null"}
                    if prefix and kinds:
                        result.setdefault(prefix, set()).update(str(kind) for kind in kinds)
                    for name, child in (node.get("properties") or {}).items():
                        if isinstance(name, str):
                            walk(child, prefix + (name,))
                walk(schema)
        return result

    for source_name, source in data_sources.items():
        for ref in source.get("producer_workflows", []):
            if definitions_present and ref.removeprefix("workflow_definition:") not in definitions:
                raise ValueError(f"app_view references an unavailable workflow definition: {ref}")
        for launch in source.get("launch_workflows", []):
            ref = launch["workflow"].removeprefix("workflow:")
            if bindings_present and ref not in bindings:
                raise ValueError(f"app_view references an unavailable workflow binding: {launch['workflow']}")
        if source["type"] == "workspace_stats" and stats_present:
            missing = set(source["stat_keys"]) - stats
            if missing:
                raise ValueError(f"app_view references unavailable stats: {', '.join(sorted(missing))}")
        # Configured state paths are only safe when a producer schema declares
        # them. A schema's additionalProperties never authorizes an App read.
        if workflows and source["type"] in {"workflow_projects", "related_projects"}:
            allowed = declared_paths(source_name)
            missing_paths = [path for path in source.get("fields", []) if tuple(path) not in allowed]
            if missing_paths:
                raise ValueError(f"app_view source {source_name!r} reads fields absent from its producer schema")
            field_types = declared_types(source_name)
            execution_run_id_path = source.get("execution_run_id_path")
            if execution_run_id_path and field_types.get(tuple(execution_run_id_path)) != {"string"}:
                raise ValueError(f"app_view source {source_name!r} execution_run_id_path requires a string-typed producer field")
            if any(field_types.get(tuple(path)) != {"string"} for path in source.get("label_fields", [])):
                raise ValueError(f"app_view source {source_name!r} label_fields require string-typed producer fields")
            order = source.get("order")
            if order and field_types.get(tuple(order["field"])) != {"string"}:
                raise ValueError(f"app_view source {source_name!r} order field requires a string-typed producer field")
        if source["type"] == "workflow_results" and workflows:
            allowed_roots = workflow_result_paths(
                [workflows[ref.removeprefix("workflow_definition:")] for ref in source.get("producer_workflows", [])
                 if ref.removeprefix("workflow_definition:") in workflows])
            requested = [*source.get("fields", []), *(entry["field"] for entry in source.get("media", []))]
            if any(not _result_path_declared(allowed_roots, path) for path in requested):
                raise ValueError(f"app_view source {source_name!r} reads results its Flow does not produce")
        if source["type"] in {"project_records", "project_documents"} and workflows:
            allowed = declared_paths(source["parent"])
            requested = source.get("record_path") or source.get("document_path")
            if tuple(requested) not in allowed:
                raise ValueError(f"app_view source {source_name!r} reads a field absent from its producer schema")
            actual = declared_types(source["parent"]).get(tuple(requested), set())
            expected = ("string" if source["type"] == "project_documents"
                        else "object" if source.get("record_schema") else "array")
            if expected not in actual:
                raise ValueError(f"app_view source {source_name!r} requires a {expected}-typed producer field")
        if source["type"] == "project_files" and workflows:
            # Only a folder the producer declares (and the runtime names) is listed.
            if "string" not in declared_types(source["parent"]).get(("project_root",), set()):
                raise ValueError(f"app_view source {source_name!r} requires a string project_root in its producer schema")
        if source["type"] == "related_projects" and workflows:
            relation_types = declared_types(source_name).get(tuple(source["parent_state_path"]), set())
            if "string" not in relation_types:
                raise ValueError(f"app_view related source {source_name!r} requires a string parent relation")
    # Blocks can only present their configured source category. This prevents a
    # valid-looking block from silently turning a document source into metrics.
    compatible = {
        "progress": {"workflow_projects", "related_projects", "owned_automations"},
        "records": {"project_records", "workflow_projects", "related_projects", "owned_automations"},
        "documents": {"project_documents", "project_files"},
        # One card per child project: its latest version's results together.
        "packs": {"project_files"},
        "references": {"project_references"},
        "metrics": {"publication_stats", "workspace_stats"},
        "summary": {"workflow_projects", "related_projects"},
        "project_rollup": {"workflow_projects", "related_projects"},
        # One card per result a Flow made: its latest version.
        "gallery": {"workflow_results"},
    }

    def record_project_ancestors(record_source: str) -> set[str]:
        return set(app_data_project_sources(data_sources, record_source))

    for component in components:
        visibility = component.get("visibility")
        if visibility:
            gate = next(block for block in component["blocks"] if block["key"] == visibility["block"])
            if component["context"].get("source") not in record_project_ancestors(gate["source"]):
                raise ValueError("app_view component visibility must use the selected project's records")
        for block in component.get("blocks", []):
            source = data_sources[block["source"]]
            if source["type"] not in compatible[block["type"]]:
                raise ValueError(f"app_view block {block['key']!r} has an incompatible source type")
            if block["type"] == "summary":
                if component["context"].get("kind") != "project" or component["context"].get("source") != block["source"]:
                    raise ValueError("app_view summary blocks require the selected project source")
                requested_fields = [field["field"] for field in block.get("fields", [])]
            elif block["type"] == "project_rollup":
                if component["context"].get("kind") != "project":
                    raise ValueError("project_rollup blocks require a selected project context")
                requested_fields = []
            else:
                requested_fields = [field["field"] if isinstance(field, dict) else field for field in block.get("fields", [])]
            allowed_fields = {tuple(path) for path in source.get("fields", [])}
            if any(tuple(path) not in allowed_fields for path in requested_fields):
                raise ValueError(f"app_view block {block['key']!r} reads fields not declared by its source")
            if block["type"] == "summary":
                continue
            if block["type"] != "records":
                continue
            ancestors = record_project_ancestors(block["source"])
            metrics_source = block.get("metrics_source")
            if metrics_source is not None:
                metrics = data_sources[metrics_source]
                if (source["type"] != "project_records" or source.get("record_schema") != "publication-receipt/v1"
                        or metrics["type"] != "publication_stats" or metrics.get("parent") != block["source"]
                        or metrics.get("subject") not in ancestors):
                    raise ValueError(f"app_view records block {block['key']!r} has an incompatible metrics_source")
            display = block.get("record_display") or {}
            for media in display.get("media", []):
                media_source = data_sources.get(media["source"], {})
                if media_source.get("type") != "project_documents" or media_source.get("parent") not in ancestors:
                    raise ValueError("app_view records media source is not owned by a record ancestor")
            entries = ([display["title"]] if isinstance(display.get("title"), dict) else []) + list(display.get("fields", []))
            for entry in entries:
                display_source = entry["source"]
                field = tuple(entry["field"])
                if display_source not in ancestors:
                    raise ValueError(f"app_view records block {block['key']!r} display source is not a record ancestor")
                declared = {tuple(path) for path in data_sources[display_source].get("fields", [])}
                if any(tuple(path) not in declared for path in [entry["field"], *entry.get("secondary", [])]):
                    raise ValueError(f"app_view records block {block['key']!r} display field is not declared by its source")
                if entry is display.get("title") and workflows and "string" not in declared_types(display_source).get(field, set()):
                    raise ValueError(f"app_view records block {block['key']!r} title display field must be string-typed")
_SECTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# A tracked number a metrics section names: a Stat by its key, or a Goal by its key.
_METRIC_REF = re.compile(r"^(stat|goal):[A-Za-z0-9_.:-]{1,120}$")
# The glyphs an App entry may name; the web maps each to one icon. An entry
# without one gets the glyph its name suggests.
APP_ENTRY_ICONS: tuple[str, ...] = (
    "flow", "chat", "clock", "play", "music", "audio", "microphone", "image", "palette", "sparkles",
    "document", "report", "presentation", "checklist", "clipboard", "search", "megaphone", "send",
    "comment", "mail", "globe", "calendar", "trending", "rocket", "academic", "box", "cart",
    "refresh", "bolt", "bookmark", "scale", "code", "users", "dollar", "heart", "star",
)

# The App page is a short list of blocks: the one contract shared by Blueprints,
# settings, the AI and the web renderer (which registers exactly these blocks
# and reads this catalog for views, sources and limits). Each block reads one
# typed source; the first view is the default; ``limit`` is how many items the
# block shows at once (default, max).
# The page is four zones in a fixed order; a block belongs to one zone and only
# moves within it: what needs the person, what they can start, how things
# stand, and what was made.
APP_ZONES: tuple[str, ...] = ("notices", "functions", "data", "outputs")

APP_SECTION_BLOCKS: dict[str, dict[str, Any]] = {
    # Connections the Workspace's Blueprint needs that are not set up yet.
    "connections": {"zone": "notices", "sources": None, "default_source": None, "source_kinds": (), "views": ("default",),
                    "limit": None, "multiple": False, "removable": True},
    # The page's entries; every page keeps them.
    "actions": {"zone": "functions", "sources": None, "default_source": None, "source_kinds": (), "views": ("default",),
                "limit": None, "multiple": False, "removable": False},
    "queue": {"zone": "notices", "sources": re.compile(r"^approvals:pending$"), "default_source": "approvals:pending",
              "source_kinds": ("approvals",), "views": ("list",), "limit": None, "multiple": False, "removable": True},
    # Tracked Workspace stats and goals: the ones its ``items`` name, in that
    # order, or else all of them in the viewer's own stat order.
    "metrics": {"zone": "data", "sources": re.compile(r"^workspace$"), "default_source": "workspace", "source_kinds": ("workspace",),
                "views": ("tiles",), "limit": (4, 12), "multiple": False, "removable": True},
    # 'auto' shows a gallery when most recent files are images or video.
    "outputs": {"zone": "outputs", "sources": re.compile(r"^(workspace|workflow:[A-Za-z0-9_.:-]{1,128})$"), "default_source": "workspace",
                "source_kinds": ("workspace", "workflow"), "views": ("auto", "list", "gallery"),
                "limit": (8, 24), "multiple": True, "removable": True},
}
DEFAULT_APP_SECTIONS: tuple[dict[str, Any], ...] = (
    {"id": "connections", "block": "connections", "source": None, "view": "default", "limit": None},
    {"id": "queue", "block": "queue", "source": "approvals:pending", "view": "list", "limit": None},
    {"id": "actions", "block": "actions", "source": None, "view": "default", "limit": None},
    {"id": "outputs", "block": "outputs", "source": "workspace", "view": "auto", "limit": 8},
)


def app_block_catalog() -> list[dict[str, Any]]:
    """The block contract as data, so clients never hard-code it."""
    return [{
        "block": block, "zone": spec["zone"], "views": list(spec["views"]), "default_source": spec["default_source"],
        "source_kinds": list(spec["source_kinds"]), "multiple": spec["multiple"], "removable": spec["removable"],
        "limit": {"default": spec["limit"][0], "max": spec["limit"][1]} if spec["limit"] else None,
    } for block, spec in APP_SECTION_BLOCKS.items()]


# Every page-level field an App config accepts (normalize_app_view_config).
APP_VIEW_FIELDS: tuple[str, ...] = (
    "version", "eyebrow", "title", "description", "hero", "order", "hidden", "labels", "descriptions", "forms",
    "targets", "entries", "prompts", "presets", "asks", "choices", "icons", "visual", "layout", "cover_image_url",
    "motion_cover_url", "motion_cover_source_url", "data_sources", "components", "sections", "open_as", "i18n",
    "theme",
)

# What a Workspace shows when someone opens it: the App beside the chat, the App alone, or the chat.
APP_OPEN_AS: tuple[str, ...] = ("split", "app", "chat")

# How an App looks within the platform's one layout (see normalize_app_theme).
APP_THEME_SURFACES: tuple[str, ...] = ("neutral", "tinted", "deep")
APP_THEME_RADII: tuple[str, ...] = ("sharp", "soft", "round")
_APP_THEME_ACCENT = re.compile(r"#[0-9a-fA-F]{6}")


def normalize_app_theme(value: Any) -> dict[str, str] | None:
    """An App's own look: a seed accent colour, a surface and a corner style.

    The App renders these as tokens; the platform derives hover and soft
    shades from the accent and keeps text on it readable, so no setting can
    make the page unreadable. None or empty means the platform's look.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"accent", "surface", "radius"}:
        raise ValueError("app_view.theme has only accent, surface and radius")
    theme: dict[str, str] = {}
    accent = value.get("accent")
    if accent is not None:
        if not isinstance(accent, str) or not _APP_THEME_ACCENT.fullmatch(accent):
            raise ValueError("app_view.theme.accent is a #rrggbb colour")
        theme["accent"] = accent.lower()
    for field, allowed in (("surface", APP_THEME_SURFACES), ("radius", APP_THEME_RADII)):
        if value.get(field) is not None:
            if value[field] not in allowed:
                raise ValueError(f"app_view.theme.{field} must be one of: {', '.join(allowed)}")
            theme[field] = value[field]
    return theme or None


def app_module_catalog() -> dict[str, Any]:
    """Every module an App page can be composed of, as data.

    The App design guide explains when to use each; this is the part read from
    the normalizer itself, so the guide never offers a module the page rejects.
    """
    return {
        "zones": list(APP_ZONES),
        "sections": app_block_catalog(),
        "page_fields": list(APP_VIEW_FIELDS),
        "layouts": ["feature", "cover", "list"],
        "visuals": ["auto", "image", "motion", "plain"],
        "form_field_types": ["text", "long_text", "file", "number", "select"],
        "data_sources": sorted(_DATA_SOURCE_TYPES),
        "data_blocks": sorted(_DATA_BLOCK_TYPES),
        "data_block_options": list(_DATA_BLOCK_OPTIONS),
        "entry_icons": list(APP_ENTRY_ICONS),
        "data_contexts": sorted(_DATA_CONTEXT_KINDS),
        "data_context_options": list(_DATA_CONTEXT_OPTIONS),
        "data_context_displays": list(_DATA_CONTEXT_DISPLAYS),
        "data_display_layouts": list(_DATA_DISPLAY_LAYOUTS),
    }


def _normalize_sections(value: Any) -> list[dict[str, Any]] | None:
    """``None`` keeps the default page; a list replaces it in order."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("app_view.sections must be an array of at most 8 sections")
    sections: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"id", "block", "source", "view", "limit", "items", "title"}:
            raise ValueError("app_view.sections contains an invalid section")
        block = item.get("block")
        spec = APP_SECTION_BLOCKS.get(block) if isinstance(block, str) else None
        if spec is None:
            raise ValueError(f"app_view section block must be one of: {', '.join(APP_SECTION_BLOCKS)}")
        section_id = item.get("id", block)
        if not isinstance(section_id, str) or not _SECTION_ID.fullmatch(section_id):
            raise ValueError("app_view section ids must be short lowercase identifiers")
        source = item.get("source", spec["default_source"])
        if spec["sources"] is None:
            if source is not None:
                raise ValueError(f"app_view {block} sections take no source")
        elif not isinstance(source, str) or not spec["sources"].fullmatch(source):
            raise ValueError(f"app_view {block} section source is invalid")
        view = item.get("view", spec["views"][0])
        if view not in spec["views"]:
            raise ValueError(f"app_view {block} section view must be one of: {', '.join(spec['views'])}")
        limit = item.get("limit")
        if spec["limit"]:
            default_limit, max_limit = spec["limit"]
            limit = default_limit if limit is None else limit
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_limit:
                raise ValueError(f"app_view {block} limit must be 1–{max_limit}")
        elif limit is not None:
            raise ValueError(f"app_view {block} sections take no limit")
        normalized = {"id": section_id, "block": block, "source": source, "view": view, "limit": limit}
        if item.get("items") is not None:
            items = item["items"]
            if block != "metrics":
                raise ValueError(f"app_view {block} sections take no items")
            if (not isinstance(items, list) or len(items) > APP_SECTION_BLOCKS["metrics"]["limit"][1]
                    or any(not isinstance(ref, str) or not _METRIC_REF.fullmatch(ref) for ref in items)
                    or len(set(items)) != len(items)):
                raise ValueError("app_view metrics items must be up to 12 distinct stat:<key> or goal:<key> references")
            # Only a section that names its numbers carries the list, so older
            # configs keep their normalized form.
            normalized["items"] = list(items)
        if item.get("title") is not None:
            title = item["title"].strip() if isinstance(item["title"], str) else ""
            if not 1 <= len(title) <= 60:
                raise ValueError("app_view section titles must be 1–60 characters")
            # Present only when set, like items: the heading over this section
            # instead of its source's name (a Flow's outputs as "Decks").
            normalized["title"] = title
        sections.append(normalized)
    ids = [section["id"] for section in sections]
    if len(set(ids)) != len(ids):
        raise ValueError("app_view section ids must be unique")
    for block, spec in APP_SECTION_BLOCKS.items():
        if not spec["multiple"] and sum(section["block"] == block for section in sections) > 1:
            raise ValueError(f"app_view may contain at most one {block} section")
    # Zones keep their order whatever order the blocks were written in.
    return sorted(sections, key=lambda section: APP_ZONES.index(APP_SECTION_BLOCKS[section["block"]]["zone"]))


def workflow_service_keys(steps_by_workflow: list[Any]) -> set[str]:
    """Services some Workflow already runs as one of its steps."""
    keys: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "service_key" and isinstance(item, str) and item:
                    keys.add(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(steps_by_workflow)
    return keys


def suggested_hidden_actions(
    actions: list[dict[str, Any]], *, flow_service_keys: set[str], plumbing_keys: set[str],
    named: set[str] | None = None, hidden: list[str] | None = None,
) -> list[str]:
    """Entries the page leaves out unless its order or hidden list names them.

    A service a Workflow already runs is reached through that Workflow; an
    Automation that only schedules a listed Workflow repeats it; plumbing such
    as the metric assessment shows up on the tracking tiles instead. Standalone
    entries and everything else stay.
    """
    named = named or set()
    explicit = set(hidden or [])
    suggested = [
        action["key"] for action in actions
        if action["key"] not in named and (
            action["key"] in plumbing_keys
            or (action["key"].startswith("service:") and action["key"].removeprefix("service:") in flow_service_keys)
            or (action["key"].startswith("automation:") and action.get("target_workflow_id")))
    ]
    # Never hide the whole page.
    if len(explicit | set(suggested)) >= len(actions):
        return []
    return suggested


def app_view_sections(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's sections, falling back to the default page."""
    sections = config.get("sections")
    return [dict(section) for section in (DEFAULT_APP_SECTIONS if sections is None else sections)]


async def load_workspace_app_view(
    db: Any, workspace: Any, *, user: Any, include_catalog: bool = True, locale: str | None = None,
) -> dict[str, Any]:
    """Use the same actor-visible target catalog for Chat and Config.

    ``locale`` is the viewer's language for the page text (see viewer_language).
    """
    from sqlalchemy import exists, or_, select
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowTemplateInstallation
    from packages.core.stats.review_setup import REVIEW_WORKFLOW_SLUG
    from packages.core.models.permission import ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, readable_resource_ids
    from packages.core.services.workspace_workflow_router import list_workspace_chat_entrypoints
    from packages.core.services.workspace_workflow_router import workflow_run_inputs

    entrypoints = await list_workspace_chat_entrypoints(
        db, entity_id=user.entity_id, workspace_id=workspace.id,
        user=user, require_control=True, include_unpublished=True, locale=locale,
    )
    agent_rows = (await db.execute(select(AgentSubscription, Agent).join(
        Agent, Agent.id == AgentSubscription.agent_id,
    ).where(
        AgentSubscription.entity_id == user.entity_id,
        AgentSubscription.workspace_id == workspace.id,
        AgentSubscription.status == "active", Agent.deleted_at.is_(None),
    ))).all()
    agents = [{"id": agent.id, "name": getattr(agent, "display_name", None) or agent.name,
               "service_key": subscription.service_key or ""} for subscription, agent in agent_rows]
    jobs = (await db.execute(select(ScheduledJob).where(
        ScheduledJob.entity_id == user.entity_id, ScheduledJob.workspace_id == workspace.id,
        ScheduledJob.agent_initiative_id.is_(None),
    ))).scalars().all()
    jobs = [job for job in jobs if app_lists_scheduled_job(job)]
    flow_steps = (await db.execute(select(WorkflowDefinition.steps).where(
        WorkflowDefinition.entity_id == user.entity_id,
        WorkflowDefinition.id.in_({entry.workflow_id for entry in entrypoints}),
    ))).scalars().all() if entrypoints else []
    # The metric assessment feeds the tracking tiles; its schedule is not an entry.
    assessments = (await db.execute(select(WorkflowBinding.id, WorkflowBinding.workflow_id).where(
        WorkflowBinding.entity_id == user.entity_id, WorkflowBinding.workspace_id == workspace.id,
        WorkflowBinding.config["workspace_blueprint_workflow_slug"].as_string() == REVIEW_WORKFLOW_SLUG,
    ))).all()
    assessment_refs = {ref for row in assessments for ref in row}

    def assesses(job: Any) -> bool:
        target = job.execution_target or {}
        return target.get("workflow_slug") == REVIEW_WORKFLOW_SLUG or bool(
            {target.get("binding_id"), target.get("workflow_id")} & assessment_refs)

    view = project_workspace_app_view(
        workspace, entrypoints, jobs, agents,
        flow_service_keys=workflow_service_keys(list(flow_steps)),
        plumbing_keys={f"automation:{job.job_id}" for job in jobs if assesses(job)}
        | {f"workflow:{binding_id}" for binding_id, _ in assessments},
        locale=locale,
    )
    if not include_catalog:
        return view
    workflows = (await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.entity_id == user.entity_id, WorkflowDefinition.status == "active",
        WorkflowDefinition.is_active.is_(True), WorkflowDefinition.trigger_type != "internal",
        or_(WorkflowDefinition.workspace_id.is_(None), exists(select(Workspace.id).where(
            Workspace.id == WorkflowDefinition.workspace_id, Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        ))),
        ~exists(select(WorkflowTemplateInstallation.id).where(
            WorkflowTemplateInstallation.entity_id == user.entity_id,
            WorkflowTemplateInstallation.workflow_id == WorkflowDefinition.id,
            WorkflowTemplateInstallation.source_type == "workspace_blueprint",
            WorkflowTemplateInstallation.installation_metadata["internal"].as_boolean().is_(True),
        )),
    ).order_by(WorkflowDefinition.name, WorkflowDefinition.id))).scalars().all()
    readable = await readable_resource_ids(
        db, descriptors=[ResourceDescriptor.from_row(row, ResourceType.WORKFLOW) for row in workflows],
        entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    attached_ids = {entry.workflow_id for entry in entrypoints}
    from packages.core.services.workflow_service import validate_workflow_definition
    view["attachable_workflow_options"] = [
        {"workflow_id": row.id, "title": row.name, "description": row.description or "",
         "inputs": [item for item in workflow_run_inputs(row, locale=locale) if not item.get("hidden")]}
        for row in workflows if row.id in readable and row.id not in attached_ids
        and validate_workflow_definition(row)["valid"]
    ]
    return view


_CHOICE_FIELDS = {"value", "label", "description", "group", "image", "sample", "presets"}
# A catalog of templates (song styles, deck types) is browsed by group and search.
MAX_CHOICES_PER_INPUT = 60


def _normalize_app_view_choices(value: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """A Workspace's own templates for an entry's free-text inputs (a house style,
    a recurring character): offered first among the input's suggestions, each able
    to set other inputs of the entry the way ``presets`` do. A template may name
    its ``group`` (browsed by group) and a ``sample`` to hear (an audio URL)."""
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("app_view.choices must be an object with at most 50 entries")
    normalized: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for key, fields in value.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.choices contains an invalid action key")
        if not isinstance(fields, dict) or len(fields) > 10:
            raise ValueError("app_view.choices maps an entry to at most 10 inputs")
        for field, options in fields.items():
            if not isinstance(field, str) or not _FIELD_KEY.fullmatch(field):
                raise ValueError("app_view.choices contains an invalid input key")
            if not isinstance(options, list) or not 1 <= len(options) <= MAX_CHOICES_PER_INPUT:
                raise ValueError(f"app_view.choices offers 1 to {MAX_CHOICES_PER_INPUT} choices per input")
            for option in options:
                if not isinstance(option, dict) or set(option) - _CHOICE_FIELDS:
                    raise ValueError("an app_view choice has only value, label, description, group, image, sample "
                                     "and presets")
                text = option.get("value")
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise ValueError("an app_view choice needs a value of at most 4000 characters")
                for name, limit in (("label", 80), ("description", 300), ("group", 40), ("image", 2000),
                                    ("sample", 2000)):
                    if option.get(name) is not None and (not isinstance(option[name], str) or len(option[name]) > limit):
                        raise ValueError(f"an app_view choice {name} is text of at most {limit} characters")
                for name in ("image", "sample"):
                    url = (option.get(name) or "").strip()
                    if url and not (re.match(r"^/(?!/)", url) or url.lower().startswith("https://")):
                        raise ValueError(f"an app_view choice {name} is a site path or an https URL")
                presets = option.get("presets") or {}
                if not isinstance(presets, dict) or len(presets) > 10 or any(
                    not isinstance(name, str) or not _FIELD_KEY.fullmatch(name)
                    or not isinstance(text, str) or len(text) > 4000
                    for name, text in presets.items()
                ):
                    raise ValueError("app_view choice presets map input keys to text of at most 4000 characters")
            normalized.setdefault(key, {})[field] = [
                {name: option[name] for name in ("value", "label", "description", "group", "image", "sample", "presets")
                 if option.get(name)}
                for option in options
            ]
    return normalized


def _input_with_choices(item: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
    """Put a Workspace's own choices first among a free-text input's suggestions.

    A fixed list (``enum``) is checked against the Flow's definition when the run
    starts, so only free text, where any value is valid, takes them."""
    schema = dict(item.get("schema") or {"type": item.get("type") or "string"})
    if schema.get("enum") is not None or schema.get("type", "string") != "string":
        return item
    examples = list(schema.get("examples") or [])

    def column(name: str, fill: Any) -> list[Any]:
        values = list(schema.get(name) or [])
        return (values + [fill] * len(examples))[:len(examples)]

    schema["examples"] = [option["value"] for option in options] + examples
    schema["x-example-labels"] = [option.get("label", "") for option in options] + column("x-example-labels", "")
    for field, name in (("image", "x-example-images"), ("group", "x-example-groups"), ("sample", "x-example-samples")):
        if any(option.get(field) for option in options) or schema.get(name):
            schema[name] = [option.get(field, "") for option in options] + column(name, "")
    if any(option.get("presets") for option in options) or schema.get("x-example-presets"):
        schema["x-example-presets"] = [option.get("presets") for option in options] + column("x-example-presets", None)
    return {**item, "schema": schema}


def normalize_app_view_config(value: Any) -> dict[str, Any]:
    """Validate the small page config before resolving its live targets."""
    if value is None:
        return {"version": 1, "eyebrow": None, "title": None, "description": None, "hero": None,
                "order": [], "hidden": [], "labels": {}, "descriptions": {}, "forms": {}, "targets": {},
                "entries": {}, "prompts": {}, "presets": {}, "asks": {}, "choices": {},
                "visual": "auto", "layout": "feature", "motion_cover_url": None,
                "motion_cover_source_url": None, "data_sources": {}, "components": [], "sections": None}
    if not isinstance(value, dict):
        raise ValueError("app_view must be an object")
    unknown = set(value) - set(APP_VIEW_FIELDS)
    if unknown:
        raise ValueError(f"unsupported app_view fields: {', '.join(sorted(unknown))}")
    if value.get("version", 1) != 1:
        raise ValueError("app_view.version must be 1")
    visual = value.get("visual", "auto")
    if visual not in ("auto", "image", "motion", "plain"):
        raise ValueError("app_view.visual must be auto, image, motion, or plain")
    layout = value.get("layout", "feature")
    if layout not in ("feature", "cover", "list"):
        raise ValueError("app_view.layout must be feature, cover, or list")
    open_as = value.get("open_as")
    if open_as is not None and open_as not in APP_OPEN_AS:
        raise ValueError(f"app_view.open_as must be one of: {', '.join(APP_OPEN_AS)}")
    cover_image_url = value.get("cover_image_url") or None
    theme = normalize_app_theme(value.get("theme"))
    motion_cover_url = value.get("motion_cover_url")
    motion_cover_source_url = value.get("motion_cover_source_url")
    if motion_cover_url == "":
        motion_cover_url = None
    if motion_cover_source_url == "":
        motion_cover_source_url = None
    for field, url in (("cover_image_url", cover_image_url), ("motion_cover_url", motion_cover_url),
                       ("motion_cover_source_url", motion_cover_source_url)):
        if url is not None and (not isinstance(url, str) or len(url) > 2048 or not ((url.startswith("/") and not url.startswith("//")) or url.startswith("https://"))):
            raise ValueError(f"app_view.{field} must be a relative or HTTPS URL")
    if motion_cover_url and not motion_cover_source_url:
        raise ValueError("app_view.motion_cover_source_url is required with a motion cover")

    eyebrow = value.get("eyebrow")
    if eyebrow is not None and (not isinstance(eyebrow, str) or len(eyebrow.strip()) > 60):
        raise ValueError("app_view.eyebrow must be at most 60 characters")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100):
        raise ValueError("app_view.title must be 1–100 characters")
    description = value.get("description")
    if description is not None and (not isinstance(description, str) or len(description.strip()) > 600):
        raise ValueError("app_view.description must be at most 600 characters")

    hero = value.get("hero")
    if hero is not None and (not isinstance(hero, str) or not _ACTION_KEY.fullmatch(hero)):
        raise ValueError("app_view.hero must be a valid action key")

    def keys(field: str) -> list[str]:
        items = value.get(field, [])
        if not isinstance(items, list) or len(items) > 50:
            raise ValueError(f"app_view.{field} must be an array of at most 50 action keys")
        if any(not isinstance(item, str) or not _ACTION_KEY.fullmatch(item) for item in items):
            raise ValueError(f"app_view.{field} contains an invalid action key")
        if len(set(items)) != len(items):
            raise ValueError(f"app_view.{field} contains duplicate action keys")
        return items

    labels = value.get("labels", {})
    if not isinstance(labels, dict) or len(labels) > 50:
        raise ValueError("app_view.labels must be an object with at most 50 entries")
    for key, label in labels.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.labels contains an invalid action key")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError("app_view labels must be 1–80 characters")
    descriptions = value.get("descriptions", {})
    if not isinstance(descriptions, dict) or len(descriptions) > 50:
        raise ValueError("app_view.descriptions must be an object with at most 50 entries")
    for key, text in descriptions.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.descriptions contains an invalid action key")
        if not isinstance(text, str) or len(text.strip()) > 240:
            raise ValueError("app_view action descriptions must be at most 240 characters")

    forms = value.get("forms", {})
    if not isinstance(forms, dict) or len(forms) > 50:
        raise ValueError("app_view.forms must be an object with at most 50 entries")
    normalized_forms: dict[str, list[dict[str, Any]]] = {}
    for key, fields in forms.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.forms must reference a valid action")
        if not isinstance(fields, list) or len(fields) > 10:
            raise ValueError("app_view form must contain at most 10 fields")
        normalized_fields = []
        seen: set[str] = set()
        for field in fields:
            if not isinstance(field, dict) or set(field) - {"key", "label", "type", "required", "placeholder", "options"}:
                raise ValueError("app_view form contains an invalid field")
            field_key = field.get("key")
            label = field.get("label")
            field_type = field.get("type", "text")
            placeholder = field.get("placeholder", "")
            options = field.get("options", [])
            if not isinstance(field_key, str) or not _FIELD_KEY.fullmatch(field_key) or field_key in seen:
                raise ValueError("app_view form field keys must be unique identifiers")
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
                raise ValueError("app_view form labels must be 1–80 characters")
            if field_type not in ("text", "long_text", "file", "number", "select"):
                raise ValueError("app_view form field type is invalid")
            if not isinstance(field.get("required", False), bool):
                raise ValueError("app_view form required must be boolean")
            if not isinstance(placeholder, str) or len(placeholder) > 160:
                raise ValueError("app_view form placeholder must be at most 160 characters")
            if not isinstance(options, list) or len(options) > 20 or any(
                not isinstance(option, str) or not 1 <= len(option.strip()) <= 80 for option in options
            ):
                raise ValueError("app_view form options must be short labels")
            if field_type == "select" and not options:
                raise ValueError("app_view select fields need options")
            seen.add(field_key)
            normalized_fields.append({
                "key": field_key, "label": label.strip(), "type": field_type,
                "required": field.get("required", False), "placeholder": placeholder.strip(),
                "options": [option.strip() for option in options] if field_type == "select" else [],
            })
        normalized_forms[key] = normalized_fields

    targets = value.get("targets", {})
    if not isinstance(targets, dict) or len(targets) > 50:
        raise ValueError("app_view.targets must be an object with at most 50 entries")
    for key, target in targets.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.targets contains an invalid action key")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", target):
            raise ValueError("app_view.targets contains an invalid target")

    entries = value.get("entries", {})
    if not isinstance(entries, dict) or len(entries) > 50:
        raise ValueError("app_view.entries must be an object with at most 50 entries")
    for key, entry in entries.items():
        if not isinstance(key, str) or not key.startswith("action:") or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.entries keys must be action:<entry id>")
        if (not isinstance(entry, dict) or set(entry) != {"title"}
                or not isinstance(entry["title"], str) or not 1 <= len(entry["title"].strip()) <= 80):
            raise ValueError("each app_view.entries value must contain a title of 1–80 characters")
        if key in targets and not targets[key].startswith(("agent:", "workflow:", "automation:")):
            raise ValueError("custom App entries require a typed Agent, Workflow, or Automation target")
    prompts = value.get("prompts", {})
    if not isinstance(prompts, dict) or len(prompts) > 50:
        raise ValueError("app_view.prompts must be an object with at most 50 commands")
    for key, prompt in prompts.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.prompts contains an invalid action key")
        if not isinstance(prompt, str) or len(prompt) > 8000:
            raise ValueError("App Agent commands must be strings of at most 8000 characters")
    # Presets fill an entry's Flow inputs before the person edits them, so one
    # Flow can appear as several entries (a pitch deck, a training deck, ...).
    presets = value.get("presets", {})
    if not isinstance(presets, dict) or len(presets) > 50:
        raise ValueError("app_view.presets must be an object with at most 50 entries")
    for key, values in presets.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.presets contains an invalid action key")
        if not isinstance(values, dict) or len(values) > 10 or any(
            not isinstance(field, str) or not _FIELD_KEY.fullmatch(field)
            or not isinstance(text, str) or len(text) > 4000
            for field, text in values.items()
        ):
            raise ValueError("app_view presets map input keys to text of at most 4000 characters")
    # An entry can ask for a Flow input the Flow keeps hidden elsewhere (the
    # series a "next episode" entry continues), so one Flow serves both.
    asks = value.get("asks", {})
    if not isinstance(asks, dict) or len(asks) > 50:
        raise ValueError("app_view.asks must be an object with at most 50 entries")
    for key, fields in asks.items():
        if (not isinstance(key, str) or not _ACTION_KEY.fullmatch(key) or not isinstance(fields, list)
                or not 1 <= len(fields) <= 10
                or any(not isinstance(field, str) or not _FIELD_KEY.fullmatch(field) for field in fields)):
            raise ValueError("app_view.asks maps an entry to the hidden Flow inputs it asks for")

    icons = value.get("icons", {})
    if not isinstance(icons, dict) or len(icons) > 50:
        raise ValueError("app_view.icons must be an object with at most 50 entries")
    for key, icon in icons.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.icons contains an invalid action key")
        if icon not in APP_ENTRY_ICONS:
            raise ValueError(f"app_view icons must be one of: {', '.join(APP_ENTRY_ICONS)}")
    choices = _normalize_app_view_choices(value.get("choices", {}))

    data_sources, components = normalize_app_view_data_contract(
        value.get("data_sources"), value.get("components"),
    )

    normalized = {
        "version": 1,
        "eyebrow": eyebrow.strip() if eyebrow is not None else None,
        "title": title.strip() if title is not None else None,
        "description": description.strip() if description is not None else None,
        "hero": hero,
        "order": keys("order"),
        "hidden": keys("hidden"),
        "labels": {key: label.strip() for key, label in labels.items()},
        "descriptions": {key: text.strip() for key, text in descriptions.items()},
        "forms": normalized_forms,
        "targets": targets,
        "entries": {key: {"title": entry["title"].strip()} for key, entry in entries.items()},
        "prompts": prompts,
        "presets": {key: dict(values) for key, values in presets.items() if values},
        "asks": {key: list(dict.fromkeys(fields)) for key, fields in asks.items()},
        "choices": choices,
        "visual": visual,
        "layout": layout,
        "motion_cover_url": motion_cover_url,
        "motion_cover_source_url": motion_cover_source_url,
        "data_sources": data_sources,
        "components": components,
        "sections": _normalize_sections(value.get("sections")),
    }
    # Like i18n, present only when set, so configs without icons keep their form.
    if icons:
        normalized["icons"] = dict(icons)
    if open_as is not None:
        normalized["open_as"] = open_as
    if cover_image_url:
        normalized["cover_image_url"] = cover_image_url
    if theme:
        normalized["theme"] = theme
    # The page's text in other languages: same-typed replacements of text it
    # already has, never of keys, targets or data (see viewer_language).
    if value.get("i18n") is not None:
        if not isinstance(value["i18n"], dict):
            raise ValueError("app_view.i18n must map a language to translated page text")
        overlays = sanitize_i18n({**normalized, "i18n": value["i18n"]}).get("i18n")
        if overlays:
            if len(json.dumps(overlays, ensure_ascii=False)) > 100_000:
                raise ValueError("app_view.i18n is too large")
            normalized["i18n"] = overlays
    return normalized


def app_lists_scheduled_job(job: Any) -> bool:
    """Whether a Workspace schedule is something its people run from the App.

    Agent initiatives and the schedules the platform derives at runtime
    (Strategist review, outcome evaluation, chat insight extraction, goal
    measurement) are how the Workspace runs itself, not entries of its page.
    """
    from packages.core.constants.blueprints import is_runtime_derived_scheduled_job

    return ((getattr(job, "execution_target", None) or {}).get("managed_by") != "agent_initiative"
            and not is_runtime_derived_scheduled_job(job_id=job.job_id, execution_type=getattr(job, "execution_type", None)))


def app_target_kind_and_id(key: str, target: str) -> tuple[str, str]:
    """Typed targets work for every entry; retain the original bare-ID config."""
    for prefix, kind in (("agent:", "chat"), ("workflow:", "workflow"), ("automation:", "automation")):
        if target.startswith(prefix):
            return kind, target[len(prefix):]
    source, source_id = key.split(":", 1)
    return {"service": "chat", "action": "chat", "workflow": "workflow", "automation": "automation"}[source], target or source_id


def app_cover(workspace_cover_url: str | None, config: dict[str, Any],
              starter_cover_url: str | None) -> tuple[str | None, str]:
    """The picture an App opens under, and whose it is.

    The Workspace's own cover wins; then the one its App config names, which
    travels with the Blueprint (a motion cover is made for it); then the
    Blueprint's Marketplace artwork.
    """
    if workspace_cover_url:
        return workspace_cover_url, "workspace"
    if config.get("cover_image_url"):
        return config["cover_image_url"], "app"
    if starter_cover_url:
        return starter_cover_url, "starter"
    return None, "none"


def project_workspace_app_view(
    workspace: Any, entrypoints: list[Any], automations: list[Any] | None = None,
    agents: list[dict[str, str]] | None = None,
    starter_cover_url: str | None = None,
    *,
    flow_service_keys: set[str] | None = None,
    plumbing_keys: set[str] | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Derive the visible actions afresh, so removed bindings cannot linger.

    Entries that ``order`` and ``hidden`` do not name follow the suggested
    default (see ``suggested_hidden_actions``). The effective list is reported
    as ``hidden`` so the settings page shows it and saving keeps it.

    The page reads in the viewer's ``locale``; the returned ``config`` stays
    as authored, translations included, because the settings page saves it.
    """
    model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    try:
        stored = normalize_app_view_config(model.get("app_view"))
    except ValueError:
        stored = normalize_app_view_config(None)
    config = localized(stored, locale)

    actions: list[dict[str, Any]] = []
    flows_by_id = {entry.binding_id: entry for entry in entrypoints}
    agents = agents or []
    agents_by_id = {agent["id"]: agent for agent in agents}
    agents_by_service = {agent.get("service_key"): agent for agent in agents if agent.get("service_key")}
    automations = automations or []
    def flow_inputs(entry: Any, *, with_hidden: bool = False) -> list[dict[str, Any]]:
        # Hidden inputs are never asked, but an entry's preset can still set them
        # (e.g. which kind of deck an entry makes), so entries carry them flagged.
        return [
            {field: item[field] for field in (
                "key", "label", "type", "required", "placeholder", "description", "default", "schema", "hidden",
                "step", "step_label",
            ) if field in item}
            for item in entry.run_inputs if with_hidden or not item.get("hidden")
        ]
    def automation_target(job: Any) -> dict[str, Any]:
        target = getattr(job, "execution_target", None) or {}
        if not isinstance(target, dict):
            target = {}
        binding_id = target.get("binding_id")
        target_flow = flows_by_id.get(binding_id) if isinstance(binding_id, str) else None
        if target_flow is None and isinstance(target.get("workflow_id"), str):
            target_flow = next((entry for entry in entrypoints if entry.workflow_id == target["workflow_id"]), None)
        return {
            "job_id": job.job_id, "title": job.name or job.job_id,
            "description": target_flow.description if target_flow else "",
            "enabled": bool(job.enabled), "last_status": job.last_status,
            **{field: getattr(job, field, None) for field in (
                "job_type", "schedule_kind", "every_seconds", "run_at", "cron_expr", "timezone",
            )},
            "instructions": getattr(job, "payload_message", None),
            "target_title": target_flow.title if target_flow else None,
            "target_workflow_id": target_flow.workflow_id if target_flow else None,
        }

    automation_targets = [(job, automation_target(job)) for job in automations]
    automation_options = [target for _, target in automation_targets]
    jobs_by_id = {job["job_id"]: job for job in automation_options}
    has_owned_automation_component = any(
        config["data_sources"].get(block["source"], {}).get("type") == "owned_automations"
        for component in config["components"]
        for block in component["blocks"]
    )
    automatic_automation_options = [
        target for job, target in automation_targets
        if not has_owned_automation_component or getattr(job, "owner_project_id", None) is None
    ]
    for entry in entrypoints:
        key = f"workflow:{entry.binding_id}"
        actions.append({
            "key": key, "kind": "workflow", "binding_id": entry.binding_id, "workflow_id": entry.workflow_id,
            "title": config["labels"].get(key) or entry.title, "source_title": entry.title,
            "source_description": entry.description,
            "description": config["descriptions"].get(key, entry.description),
            "placeholder": entry.placeholder, "inputs": flow_inputs(entry, with_hidden=True),
            # Present only when the Flow declares rules, so other actions keep their form.
            **({"input_rules": [dict(rule) for rule in entry.input_rules]} if getattr(entry, "input_rules", ()) else {}),
        })

    for service in model.get("services") or []:
        if not isinstance(service, dict):
            continue
        service_key = str(service.get("key") or service.get("service_key") or "").strip()
        name = str(service.get("name") or "").strip()
        if not service_key or not name or not _ACTION_KEY.fullmatch(f"service:{service_key}"):
            continue
        key = f"service:{service_key}"
        default_agent = agents_by_service.get(service_key)
        action = {
            "key": key, "kind": "chat", "title": config["labels"].get(key) or name, "source_title": name,
            "source_description": str(service.get("description") or "").strip(),
            "description": config["descriptions"].get(key, str(service.get("description") or "").strip()),
            "prompt": config["prompts"].get(key) or name, "inputs": config["forms"].get(key, []),
        }
        if default_agent:
            action.update(agent_id=default_agent["id"], agent_name=default_agent["name"],
                          default_agent_id=default_agent["id"], default_agent_name=default_agent["name"])
        actions.append(action)

    for job in automatic_automation_options:
        key = f"automation:{job['job_id']}"
        actions.append({**job, "key": key, "kind": "automation", "source_title": job["title"],
                        "source_description": job["description"],
                        "title": config["labels"].get(key) or job["title"],
                        "description": config["descriptions"].get(key, job["description"])})

    # Standalone App entries own only presentation and a runner reference.
    # They never create a Service, Agent, scheduled job, or Workflow definition.
    for key, entry in config["entries"].items():
        actions.append({
            "key": key, "kind": "chat", "source_title": entry["title"], "source_description": "",
            "title": config["labels"].get(key) or entry["title"],
            "description": config["descriptions"].get(key, ""),
            "prompt": config["prompts"].get(key) or entry["title"],
            "inputs": config["forms"].get(key, []), "available": False,
        })

    for action in actions:
        key = action["key"]
        target = config["targets"].get(key)
        if not target:
            continue
        kind, target_id = app_target_kind_and_id(key, target)
        # Remove the previous runner's fields so previews and execution cannot
        # accidentally use a stale Agent, binding, or scheduled job.
        for field in ("agent_id", "agent_name", "binding_id", "workflow_id", "job_id", "prompt", "placeholder",
                      "enabled", "last_status", "job_type", "schedule_kind", "every_seconds", "run_at",
                      "cron_expr", "timezone", "instructions", "target_title", "target_workflow_id", "input_rules"):
            action.pop(field, None)
        action["kind"] = kind
        action["inputs"] = []
        if kind == "chat":
            agent = agents_by_id.get(action.get("default_agent_id") if target_id == "default" else target_id)
            action.update(prompt=config["prompts"].get(key) or action["source_title"], inputs=config["forms"].get(key, []),
                          description=config["descriptions"].get(key, action["source_description"]),
                          available=target_id == "default" or agent is not None)
            if agent:
                action.update(agent_id=agent["id"], agent_name=agent["name"])
        elif kind == "workflow":
            selected = flows_by_id.get(target_id)
            action.update(binding_id=selected.binding_id if selected else None,
                          workflow_id=selected.workflow_id if selected else None,
                          description=config["descriptions"].get(key, selected.description if selected else ""),
                          placeholder=selected.placeholder if selected else "",
                          inputs=flow_inputs(selected, with_hidden=True) if selected else [],
                          available=selected is not None)
            if getattr(selected, "input_rules", ()):
                action["input_rules"] = [dict(rule) for rule in selected.input_rules]
        else:
            job = jobs_by_id.get(target_id)
            action.update({field: value for field, value in (job or {}).items() if field not in ("title", "description")})
            action.update(available=job is not None,
                          description=config["descriptions"].get(key, job["description"] if job else ""))

    config["hidden"] = (config["hidden"] + suggested_hidden_actions(
        actions, flow_service_keys=flow_service_keys or set(), plumbing_keys=plumbing_keys or set(),
        named=set(config["order"]) | set(config["hidden"]), hidden=config["hidden"],
    ))[:50]
    for action in actions:
        preset = config["presets"].get(action["key"]) or {}
        asked = set(config["asks"].get(action["key"]) or [])
        if action.get("kind") == "workflow":
            # The preset wins over the Flow's own default wherever the form reads one.
            # A hidden input travels only where a preset sets it, and is asked
            # only where the entry asks for it.
            action["inputs"] = [{
                **item, "default": preset[item["key"]],
                **({"schema": {**item["schema"], "default": preset[item["key"]]}} if isinstance(item.get("schema"), dict) else {}),
            } if item.get("key") in preset else {**item, "hidden": False, "required": True} if item.get("key") in asked else item
                for item in action.get("inputs") or []
                if not item.get("hidden") or item.get("key") in preset or item.get("key") in asked]
            choices = config["choices"].get(action["key"]) or {}
            action["inputs"] = [_input_with_choices(item, choices[item["key"]]) if item.get("key") in choices else item
                                for item in action["inputs"]]
    # An entry carries an icon only when the page chose one; the web guesses the rest.
    for action in actions:
        if action["key"] in (config.get("icons") or {}):
            action["icon"] = config["icons"][action["key"]]
    visible = [action for action in actions if action["key"] not in config["hidden"]]
    positions = {key: index for index, key in enumerate(config["order"])}
    visible.sort(key=lambda action: positions.get(action["key"], len(positions) + actions.index(action)))
    hero = next((action["key"] for action in visible if action["key"] == config["hero"]), None)
    default_hero = next((action["key"] for action in visible
                         if action["kind"] == "workflow" and action.get("available") is not False), None)
    sections = []
    for section in app_view_sections(config):
        section["implicit"] = config["sections"] is None
        source = section["source"] or ""
        if source.startswith("workflow:"):
            # Like action targets, a removed binding leaves an unavailable
            # section instead of silently widening to every Workspace run.
            flow = flows_by_id.get(source.removeprefix("workflow:"))
            section.update(available=flow is not None, source_title=flow.title if flow else None)
        else:
            section.update(available=True, source_title=None)
        sections.append(section)
    cover_image_url, cover_source = app_cover(getattr(workspace, "cover_image_url", None), config, starter_cover_url)
    return {
        "workspace_id": workspace.id,
        "eyebrow": config["eyebrow"],
        "title": config["title"] if config["title"] is not None else workspace.name,
        "description": config["description"] if config["description"] is not None else (workspace.primary_work or workspace.operating_context or ""),
        "status": workspace.status,
        "cover_image_url": cover_image_url,
        "cover_source": cover_source,
        "hero": hero if config["hero"] else default_hero,
        "actions": visible,
        "available_actions": actions,
        "sections": sections,
        "blocks": app_block_catalog(),
        "workflow_options": [
            {"binding_id": entry.binding_id, "workflow_id": entry.workflow_id,
             "title": entry.title, "description": entry.description,
             "placeholder": entry.placeholder, "inputs": flow_inputs(entry)}
            for entry in entrypoints
        ],
        "automation_options": automation_options,
        "agent_options": [{"id": agent["id"], "name": agent["name"]} for agent in agents],
        # Components intentionally retain only their declaration here. Their
        # workspace-scoped rows are fetched through the bounded read endpoints.
        "components": config["components"],
        "config": {**stored, "hidden": config["hidden"]},
    }
