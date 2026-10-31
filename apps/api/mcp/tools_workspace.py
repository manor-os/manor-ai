"""Inspect and edit a live Workspace: identity, operation drafts, workflows, skills, knowledge."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from jsonschema import Draft202012Validator

from apps.api.mcp import transfers
from apps.api.mcp.manor_api import ManorApiError
from apps.api.mcp.registry import Tool, ToolContext, ToolError, boolean, obj, require, string
from apps.api.mcp.workspace_operation_schemas import OPERATION_PAYLOAD_SCHEMAS
from packages.core.ai.runtime.capability_search import SKILL_DESCRIPTION_MAX_CHARS
from packages.core.services.workspace_operation_service import (
    OPERATION_PATCH_OP_CATALOG,
    operation_draft_review_fingerprint,
)

_WS = "/api/v1/workspaces"

# Supplement only MCP's public descriptors; the in-product catalog and runtime
# validation retain their own contracts. Undescribed internal operations cannot
# silently become MCP-callable, and existing canonical schemas take precedence.
PUBLISHED_OPERATIONS = tuple(
    replace(entry, payload_schema=entry.payload_schema if entry.payload_schema is not None
            else OPERATION_PAYLOAD_SCHEMAS[entry.canonical])
    for entry in OPERATION_PATCH_OP_CATALOG
    if entry.payload_schema is not None or entry.canonical in OPERATION_PAYLOAD_SCHEMAS
)
_PUBLISHED_NAMES = [entry.canonical for entry in PUBLISHED_OPERATIONS]
_CANONICAL_NAMES = {name: entry.canonical for entry in PUBLISHED_OPERATIONS
                    for name in (entry.canonical, *entry.aliases)}
_PAYLOAD_VALIDATORS = {entry.canonical: Draft202012Validator(entry.payload_schema) for entry in PUBLISHED_OPERATIONS}
_PATCH_SCHEMA = {"oneOf": [
    {"type": "object", "properties": {
        "op": {"type": "string", "enum": [entry.canonical, *entry.aliases]},
        "payload": entry.payload_schema,
    }, "required": ["op", "payload"], "additionalProperties": False}
    for entry in PUBLISHED_OPERATIONS
]}


def operation_tool_name(operation: str) -> str:
    return "stage_workspace_" + operation.replace(".", "_")

_LIST_KEYS = ("id", "name", "description", "kind", "category", "status", "can_manage",
              "blueprint_update", "created_at", "updated_at")


async def whoami(ctx: ToolContext, args: dict) -> Any:
    me = await ctx.api.get("/api/v1/auth/me")
    return {key: me.get(key) for key in (
        "id", "email", "role", "entity_id", "staff_role_name", "memberships", "permissions",
    )}


async def list_workspaces(ctx: ToolContext, args: dict) -> Any:
    rows = await ctx.api.get(_WS, initialize=False)
    return [{key: row.get(key) for key in _LIST_KEYS} for row in rows]


async def get_workspace(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    workspace = await ctx.api.get(f"{_WS}/{ws_id}", initialize=False)
    result: dict[str, Any] = {"workspace": workspace}
    sections = set(args.get("include") or ["app_view"])
    if "app_view" in sections:
        result["app_view"] = await ctx.api.get(f"{_WS}/{ws_id}/app-view", include_catalog=False, initialize=False)
    if "agents" in sections:
        result["agents"] = await ctx.api.get(f"{_WS}/{ws_id}/agents")
    if "operation_state" in sections:
        result["operation_state"] = (await ctx.api.get(f"{_WS}/{ws_id}/operation/current")).get("state")
    return result


async def describe_operations(ctx: ToolContext, args: dict) -> Any:
    return {
        "summary": "Optional reference for operations already exposed as individual stage tools. "
                   "Each stage returns validation, diff and review_fingerprint. "
                   "apply_workspace_change commits the reviewed draft atomically. "
                   "stage_workspace_change batches these same public operations; no additional operations are enabled.",
        "patch_schema": _PATCH_SCHEMA,  # Retain the existing optional catalog response key.
        "operations": [{"operation": entry.canonical, "tool": operation_tool_name(entry.canonical),
                        "description": entry.summary, "payload_schema": entry.payload_schema}
                       for entry in PUBLISHED_OPERATIONS],
    }


async def stage_change(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    patches = require(args, "patches")
    if not isinstance(patches, list):
        raise ToolError("'patches' must be a list of {op, payload} objects")
    for index, patch in enumerate(patches):
        if (not isinstance(patch, dict) or set(patch) != {"op", "payload"}
                or not isinstance(patch.get("op"), str) or patch["op"] not in _CANONICAL_NAMES):
            raise ToolError(f"patches[{index}] requires a published operation name and payload")
        error = next(_PAYLOAD_VALIDATORS[_CANONICAL_NAMES[patch["op"]]].iter_errors(patch["payload"]), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)
            raise ToolError(f"patches[{index}].payload{'.' + path if path else ''} must match the published operation's "
                            f"{error.validator} constraint: {error.validator_value}")
    patches = [{"op": _CANONICAL_NAMES[patch["op"]], "payload": patch["payload"]} for patch in patches]
    body = {"patches": patches, "allowed_patch_operations": _PUBLISHED_NAMES}
    draft_id = args.get("draft_id")
    if draft_id:
        draft = await ctx.api.patch(f"{_WS}/{ws_id}/operation/drafts/{draft_id}", body)
    else:
        draft = await ctx.api.post(f"{_WS}/{ws_id}/operation/drafts", body)
    draft_id = draft["id"]
    # Create/patch already validates and returns one consistent review snapshot.
    # Separate validate/diff requests could bind a fingerprint to another edit.
    return {
        "draft_id": draft_id,
        "base_revision": draft.get("base_revision"),
        "patches": draft.get("patches"),
        "validation": draft.get("validation"),
        "diff": draft.get("diff"),
        "review_fingerprint": operation_draft_review_fingerprint(draft),
    }


async def design_app_view(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    body = {"brief": require(args, "brief")}
    for key in ("feedback", "draft_id"):
        if args.get(key):
            body[key] = args[key]
    result = await ctx.api.post(f"{_WS}/{ws_id}/app-view/design", body)
    draft = result["draft"]
    return {
        "draft_id": draft["id"],
        "candidate": result.get("candidate"),
        "recipes": result.get("recipes"),
        "warnings": result.get("warnings"),
        "design_notes": result.get("design_notes"),
        "validation": draft.get("validation"),
        "diff": draft.get("diff"),
        "review_fingerprint": operation_draft_review_fingerprint(draft),
    }


async def apply_change(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    draft_id = require(args, "draft_id")
    fingerprint = require(args, "review_fingerprint")
    if not Draft202012Validator({"type": "string", "pattern": "^[0-9a-f]{64}$"}).is_valid(fingerprint):
        raise ToolError("review_fingerprint must come from the reviewed stage result; refresh the draft with patches: [] if needed")
    return await ctx.api.post(
        f"{_WS}/{ws_id}/operation/drafts/{draft_id}/apply", {
            "user_confirmation": True, "expected_review_fingerprint": fingerprint,
            "allowed_patch_operations": _PUBLISHED_NAMES,
        },
    )


async def discard_change(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    draft_id = require(args, "draft_id")
    draft = await ctx.api.post(f"{_WS}/{ws_id}/operation/drafts/{draft_id}/discard")
    return {"draft_id": draft.get("id"), "status": draft.get("status")}


_WORKFLOW_FIELDS = ("name", "description", "steps", "trigger_type", "trigger_config",
                    "variables", "category", "tags", "workspace_id", "visibility", "icon")


async def upsert_workflow(ctx: ToolContext, args: dict) -> Any:
    from packages.core.services.workflow_service import validate_workflow_steps

    workflow_id = args.get("workflow_id")
    steps = args.get("steps")
    if steps is not None:
        report = validate_workflow_steps(steps)
        if args.get("dry_run") or not report.get("valid"):
            return {"saved": False, "validation": report}
    elif args.get("dry_run"):
        raise ToolError("dry_run validates 'steps'; pass them")
    body = {key: args[key] for key in _WORKFLOW_FIELDS if key in args}
    if workflow_id:
        body.pop("workspace_id", None)
        workflow = await ctx.api.put(f"/api/v1/workflows/{workflow_id}", {**body, "validate_steps": True})
    else:
        require(args, "name")
        require(args, "steps")
        workflow = await ctx.api.post("/api/v1/workflows", body)
    return {"saved": True, "workflow": workflow}


async def run_workflow(ctx: ToolContext, args: dict) -> Any:
    workflow_id = require(args, "workflow_id")
    return await ctx.api.post(f"/api/v1/workflows/{workflow_id}/run", {
        "variables": args.get("variables") or {},
        "trigger_data": args.get("trigger_data") or {},
        "execute": args.get("wait", True),
    })


async def list_run_input_choices(ctx: ToolContext, args: dict) -> Any:
    workflow_id = require(args, "workflow_id")
    input_key = require(args, "input_key")
    return await ctx.api.get(
        f"/api/v1/workflows/{workflow_id}/run-inputs/{input_key}/choices",
        workspace_id=require(args, "workspace_id"),
    )


async def get_workflow_run(ctx: ToolContext, args: dict) -> Any:
    run_id = require(args, "run_id")
    return await ctx.api.get(f"/api/v1/workflows/runs/{run_id}", detail=bool(args.get("detail", True)))


_SKILL_FIELDS = ("name", "display_name", "description", "system_prompt", "slug", "tools",
                 "input_schema", "output_format", "category", "tags", "version", "config",
                 "type", "scripts", "requirements", "extra_files", "workspace_id", "visibility")


async def upsert_skill(ctx: ToolContext, args: dict) -> Any:
    skill_id = args.get("skill_id")
    body = {key: args[key] for key in _SKILL_FIELDS if key in args}
    if skill_id:
        body.pop("workspace_id", None)
        return await ctx.api.put(f"/api/v1/skills/{skill_id}", body)
    require(args, "name")
    require(args, "system_prompt")
    return await ctx.api.post("/api/v1/skills", body)


async def create_upload(ctx: ToolContext, args: dict) -> Any:
    filename = str(require(args, "filename"))[:255]
    slot = await transfers.create(
        kind=transfers.UPLOAD, user_id=ctx.user_id, entity_id=ctx.entity_id,
        filename=filename, content_type=args.get("content_type") or "application/octet-stream",
    )
    url = ctx.transfer_url(slot.id)
    return {
        "upload_id": slot.id,
        "upload_url": url,
        "command": f"curl -sS --fail -X PUT --data-binary @{_shell_quote(filename)} {_shell_quote(url)}",
        "expires_in_seconds": transfers.TRANSFER_TTL_SECONDS,
        "max_bytes": transfers.MAX_TRANSFER_BYTES,
    }


_STAT_FIELDS = ("name", "description", "unit", "window", "collector_type", "collector_config",
                "collection_cadence", "freshness_limit_seconds", "goal_eligible")
_STAT_CREATE_ONLY_FIELDS = ("library_key", "key", "value_type")


async def list_stats(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    stats = await ctx.api.get(f"{_WS}/{ws_id}/stats")
    templates = await ctx.api.get(f"{_WS}/stats/assessment-templates")
    return {"items": stats.get("items"), "assessment_templates": templates.get("items")}


async def upsert_stat(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    stat_id = args.get("stat_id")
    body = {key: args[key] for key in _STAT_FIELDS if key in args}
    if stat_id:
        extra = [key for key in _STAT_CREATE_ONLY_FIELDS if key in args]
        if extra:
            raise ToolError(f"{', '.join(extra)} can only be set when creating a stat")
        if "status" in args:
            body["status"] = args["status"]
        return await ctx.api.patch(f"{_WS}/{ws_id}/stats/{stat_id}", body)
    if "status" in args:
        raise ToolError("'status' only applies to an existing stat; pass stat_id")
    body.update({key: args[key] for key in _STAT_CREATE_ONLY_FIELDS if key in args})
    if not body.get("library_key"):
        require(args, "key")
        require(args, "name")
    return await ctx.api.post(f"{_WS}/{ws_id}/stats", body)


async def add_assessed_metrics(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    body = {"templates": require(args, "templates"),
            **{key: args[key] for key in ("service_key", "cadence") if key in args}}
    return await ctx.api.post(f"{_WS}/{ws_id}/stats/assessed-metrics", body)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


async def add_knowledge_document(ctx: ToolContext, args: dict) -> Any:
    ws_id = require(args, "workspace_id")
    content = args.get("content")
    upload_id = args.get("upload_id")
    if (content is None) == (upload_id is None):
        raise ToolError("pass exactly one of 'content' (text) or 'upload_id' (from create_upload)")
    if upload_id:
        try:
            upload = await transfers.load_upload_for(upload_id, user_id=ctx.user_id, entity_id=ctx.entity_id)
        except ValueError as e:
            raise ToolError(str(e)) from None
        filename = args.get("filename") or upload.filename
        data, content_type = upload.data, upload.content_type
    else:
        filename = require(args, "filename")
        data, content_type = str(content).encode("utf-8"), "text/markdown"
    folder_id = args.get("folder_id")
    if not folder_id:
        workspace = await ctx.api.get(f"{_WS}/{ws_id}")
        folder_id = workspace.get("artifact_folder_id")
        if not folder_id:
            raise ToolError("this Workspace has no Knowledge folder yet; pass folder_id")
    document = await ctx.api.request(
        "POST", "/api/v1/documents/upload",
        params={"folder_id": folder_id},
        files={"file": (filename, data, content_type)},
    )
    return {key: document.get(key) for key in ("id", "name", "folder_id", "file_type", "size", "status")
            if isinstance(document, dict)}


_WORKSPACE_ID = string("Workspace id (from list_workspaces).")
_DRAFT_ID = string("Operation draft id returned by a Workspace stage tool.")

TOOLS = [
    Tool("whoami", "Who am I", "The signed-in Manor user, their current team (entity) and role.",
         whoami, read_only=True, idempotent=True),
    Tool("list_workspaces", "List Workspaces",
         "Workspaces in the current team that this user can read.",
         list_workspaces, read_only=True, idempotent=True),
    Tool("get_workspace", "Get Workspace",
         "One Workspace: its configuration and operating model, plus optional sections "
         "(app_view = the customer-facing App, agents = agent/service mappings, "
         "operation_state = the editable configuration that operation patches change).",
         get_workspace,
         properties={"workspace_id": _WORKSPACE_ID, "include": {
             "type": "array", "items": {"enum": ["app_view", "agents", "operation_state"]},
             "description": "Extra sections; default [\"app_view\"]."}},
         required=("workspace_id",), read_only=True, idempotent=True),
    Tool("describe_workspace_operations", "Describe Workspace operations",
         "Optional reference listing the operations, descriptions and schemas already published "
         "as individual stage_workspace_* tools. This read-only reference enables no additional operations.",
         describe_operations, read_only=True, idempotent=True),
    Tool("stage_workspace_change", "Stage Workspace change",
         "Batch the explicitly enumerated Workspace configuration operations into one draft, or append "
         "them to an open draft. These same operations each have an individual stage_workspace_* tool. "
         "Return validation, diff and review_fingerprint; live configuration is unchanged until the reviewed "
         "draft is committed with apply_workspace_change. An empty patches list refreshes an open draft's review.",
         stage_change,
         properties={"workspace_id": _WORKSPACE_ID,
                     "patches": {"type": "array", "items": _PATCH_SCHEMA,
                                 "description": "Explicitly typed public operation patches, applied in order to this draft."},
                     "draft_id": string("Add to this open draft instead of starting a new one.")},
         required=("workspace_id", "patches")),
    Tool("design_workspace_app_view", "Design Workspace App page",
         "Command Manor's App design sub-agent to design the Workspace's App page from what it runs (Workflows, "
         "services, Agents, Stats, Automations), using the App module schematic and recipes: entries, card actions "
         "such as Make this episode and Remake, data views and sections. Returns the checked candidate, its recipes "
         "and design notes, staged as an app_view.update draft with validation, diff and review_fingerprint. "
         "Nothing changes until apply_workspace_change commits it; to send it back call again with feedback and "
         "its draft_id (the earlier draft is discarded). Requires management access to this Workspace; "
         "a collaborator may design only within their live-granted Workspace. Runs a model using the "
         "deployment's configured provider and billing policy.",
         design_app_view,
         properties={"workspace_id": _WORKSPACE_ID,
                     "brief": string("What the person wants to do from the App, in their words.", maxLength=4000),
                     "feedback": string("Why the last design was sent back and what to change.", maxLength=4000),
                     "draft_id": string("The earlier design draft this one replaces.")},
         required=("workspace_id", "brief")),
    Tool("apply_workspace_change", "Apply Workspace change",
         "Atomically commit the exact reviewed draft containing only the individually published Workspace "
         "operations. Pass its latest review_fingerprint after reviewing the validation and diff with the user. "
         "May replace configuration, remove bindings or rules, change budgets, start schedules, or create tasks "
         "as explicitly shown in that draft. A changed or stale draft is rejected before any effects.",
         apply_change, properties={"workspace_id": _WORKSPACE_ID, "draft_id": _DRAFT_ID,
                                  "review_fingerprint": string("Fingerprint from the exact stage result reviewed by the user.",
                                                               pattern="^[0-9a-f]{64}$")},
         required=("workspace_id", "draft_id", "review_fingerprint"), destructive=True, open_world=True),
    Tool("discard_workspace_change", "Discard Workspace change",
         "Cancel a staged operation draft permanently; it can no longer be edited or applied. Live configuration stays unchanged.",
         discard_change, properties={"workspace_id": _WORKSPACE_ID, "draft_id": _DRAFT_ID},
         required=("workspace_id", "draft_id"), destructive=True, idempotent=True),
    Tool("upsert_workflow", "Create or update workflow",
         "Validate workflow steps and create (no workflow_id) or update a workflow. Invalid steps "
         "are never saved; dry_run=true only validates. Updating replaces supplied fields, including the steps graph. "
         "A workflow that makes something (a video scene by scene, a song, slides) can declare config.production "
         "on its trigger step so its runs show as stages, units and the finished result: {media: video|audio|image "
         "(default video; what the run makes and how its result plays), unit: {name}, "
         "units_from: dotted path to the planned "
         "units in run variables, final_from: dotted path to the finished file ({document_id}), "
         "unit_fields: {id, title, duration}, lanes: [{key, label, from}], stages: "
         "[{label, steps: [step ids], counts_units, weight}], revise: {from_step, notes_input, versions_input} "
         "(a finished result can be changed: its next version starts again from from_step with the person's notes "
         "and chosen unit versions in those declared run inputs; earlier versions are kept)}. "
         "Attach a saved workflow with stage_workspace_workflow_binding_attach, then review and apply the draft.",
         upsert_workflow,
         properties={"workflow_id": string("Update this workflow; omit to create."),
                     "name": string("Workflow name (required to create)."),
                     "description": string("What the workflow does."),
                     "steps": {"type": "array", "items": obj("Workflow step"),
                               "description": "Workflow graph steps (required to create)."},
                     "trigger_type": string("manual, schedule, webhook, event …"),
                     "trigger_config": obj("Trigger configuration."),
                     "variables": {"type": "array", "items": obj("Run input variable")},
                     "workspace_id": string("Own the workflow by this Workspace (create only)."),
                     "category": string("Category label."),
                     "tags": {"type": "array", "items": {"type": "string"}},
                     "dry_run": boolean("Only validate the steps.", False)}, destructive=True),
    Tool("run_workflow", "Run workflow",
         "Execute a workflow with run inputs. Its configured steps may modify data, spend credits, send messages "
         "or call external services; this is not a dry run. wait=true (default) runs to completion or its first pause.",
         run_workflow,
         properties={"workflow_id": string("Workflow id."),
                     "variables": obj("Run input values by variable name."),
                     "trigger_data": obj("Trigger payload, for event/webhook workflows."),
                     "wait": boolean("Execute now and wait for the result.", True)},
         required=("workflow_id",), destructive=True, open_world=True),
    Tool("list_run_input_choices", "List run input choices",
         "The records a record_select run input currently offers, as {value, label, description}. "
         "Pass the values (record keys, never labels) to run_workflow; a run with a key the source "
         "does not offer is rejected.",
         list_run_input_choices,
         properties={"workflow_id": string("Workflow id."),
                     "input_key": string("The run input's key."),
                     "workspace_id": string("Workspace the run belongs to.")},
         required=("workflow_id", "input_key", "workspace_id"), read_only=True, idempotent=True),
    Tool("get_workflow_run", "Get workflow run",
         "A workflow run's status, step results, errors and pending approvals.",
         get_workflow_run,
         properties={"run_id": string("Run id from run_workflow."),
                     "detail": boolean("Include per-step detail.", True)},
         required=("run_id",), read_only=True, idempotent=True),
    Tool("upsert_skill", "Create or update skill",
         "Create (no skill_id) or update a Skill: system_prompt, tools, input schema and bundled "
         "files. On update, 'scripts' and 'extra_files' replace the whole file set; files you leave "
         "out are deleted.",
         upsert_skill,
         properties={"skill_id": string("Update this skill; omit to create."),
                     "name": string("Skill name (required to create)."),
                     "display_name": string("Display name."),
                     "description": string("When an agent should use the skill, and any params callers must pass. "
                                            "Callers read only this.", maxLength=SKILL_DESCRIPTION_MAX_CHARS),
                     "system_prompt": string("The skill's instructions (required to create)."),
                     "tools": {"type": "array", "items": {"type": "string"}},
                     "input_schema": obj("JSON schema of the skill's inputs."),
                     "scripts": obj("Script files by relative path → content."),
                     "extra_files": obj("Reference files by relative path → content."),
                     "requirements": {"type": "array", "items": {"type": "string"}},
                     "workspace_id": string("Own the skill by this Workspace (create only)."),
                     "visibility": string("Visibility, e.g. private or entity.")},
         destructive=True),
    Tool("list_stats", "List Workspace Stats",
         "A Workspace's Stats (metrics) with collector, current value, freshness and, for agent_review "
         "Stats, the assessment Workflow's runtime phase (configuration_error means the Stat's "
         "workflow_slug matches no single attached Workflow with one schedule). Also lists the "
         "assessment templates add_assessed_metrics accepts.",
         list_stats, properties={"workspace_id": _WORKSPACE_ID},
         required=("workspace_id",), read_only=True, idempotent=True),
    Tool("upsert_stat", "Create or update Workspace Stat",
         "Create (no stat_id) or update a Workspace Stat. Create from library_key, or a custom Stat "
         "with key and name. collector_type manual | workspace_internal | integration | public_post | "
         "agent_review. An agent_review Stat takes collector_config {workflow_slug, assessment: "
         "{subject: \"workspace\", rubric, scale: {minimum, maximum?}, missing_evidence: \"unknown\"}} "
         "and no collection_cadence: its assessment Workflow's single schedule sets the cadence. "
         "Changing the collector starts a new revision, so earlier assessments no longer count as current. "
         "On update only the fields you pass change; status pauses or resumes the Stat.",
         upsert_stat,
         properties={"workspace_id": _WORKSPACE_ID,
                     "stat_id": string("Update this Stat; omit to create."),
                     "library_key": string("Create from this Stats library entry (create only)."),
                     "key": string("Stable Stat key, e.g. weekly_new_guests (create only)."),
                     "name": string("Display name."),
                     "description": string("What the number means."),
                     "value_type": {"enum": ["number", "percent", "currency", "duration"], "default": "number",
                                    "description": "Create only."},
                     "unit": string("Unit label, e.g. guests."),
                     "window": string("latest (default), lifetime, rolling_24h, rolling_7d, rolling_30d, "
                                     "calendar_week, calendar_month …"),
                     "collector_type": {"enum": ["manual", "workspace_internal", "integration",
                                                 "public_post", "agent_review"]},
                     "collector_config": obj("Collector configuration; see the description for agent_review."),
                     "collection_cadence": string("Collection cadence for collectors that poll; never for agent_review."),
                     "freshness_limit_seconds": {"type": "integer", "minimum": 60},
                     "goal_eligible": boolean("Goals may measure this Stat."),
                     "status": {"enum": ["active", "paused"], "description": "Update only."}},
         required=("workspace_id",), destructive=True),
    Tool("add_assessed_metrics", "Add assessed metrics",
         "Track audience metrics from list_stats' assessment_templates (e.g. latest_video_views, "
         "followers) that an agent assesses from Workspace evidence on a schedule. Creates the Stats, "
         "the Workspace's one assessment Workflow and its enabled schedule (repeat calls add metrics "
         "to the same Workflow). Scheduled runs spend credits. For custom rubrics build the "
         "assessment Workflow yourself and use upsert_stat.",
         add_assessed_metrics,
         properties={"workspace_id": _WORKSPACE_ID,
                     "templates": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
                     "service_key": string("Service whose agent assesses; defaults to the Workspace's only one."),
                     "cadence": {"enum": ["daily", "weekly"], "default": "daily"}},
         required=("workspace_id", "templates"), destructive=True),
    Tool("create_upload", "Create file upload",
         "Get a 30-minute upload URL for a local file (a Blueprint payload or a Knowledge "
         "document). Run the returned curl command from the file's directory, then pass "
         "upload_id to validate_blueprint, install_blueprint, compare_blueprints or "
         "add_knowledge_document.",
         create_upload,
         properties={"filename": string("Local file name, e.g. blueprint.json."),
                     "content_type": string("MIME type, e.g. application/json or application/pdf.")},
         required=("filename",)),
    Tool("add_knowledge_document", "Add Knowledge document",
         "Add a document to a Workspace's Knowledge (its Knowledge folder unless folder_id is "
         "given). Pass text as 'content' with a filename, or a file uploaded via create_upload.",
         add_knowledge_document,
         properties={"workspace_id": _WORKSPACE_ID,
                     "filename": string("Document file name, e.g. pricing.md."),
                     "content": string("Text/Markdown content."),
                     "upload_id": string("A completed create_upload upload."),
                     "folder_id": string("Target folder instead of the Workspace Knowledge folder.")},
         required=("workspace_id",)),
]


def _operation_tool(entry) -> Tool:
    async def stage_operation(ctx: ToolContext, args: dict) -> Any:
        if set(args) - {"workspace_id", "draft_id", "payload"}:
            raise ToolError("pass workspace_id, payload, and optionally draft_id; this tool's operation is fixed")
        return await stage_change(ctx, {
            "workspace_id": require(args, "workspace_id"), "draft_id": args.get("draft_id"),
            "patches": [{"op": entry.canonical, "payload": require(args, "payload")}],
        })

    return Tool(
        operation_tool_name(entry.canonical), f"Stage {entry.canonical.replace('.', ' ')}",
        f"Stage this specific Workspace change: {entry.summary}. "
        "Creates or appends to an open operation draft and returns validation, diff and review_fingerprint. "
        "No live configuration changes until the user reviews it and apply_workspace_change commits that exact draft.",
        stage_operation,
        properties={"workspace_id": _WORKSPACE_ID, "payload": entry.payload_schema,
                    "draft_id": string("Append this change to an existing open draft; omit for a new draft.")},
        required=("workspace_id", "payload"),
    )


TOOLS.extend(_operation_tool(entry) for entry in PUBLISHED_OPERATIONS)

__all__ = ["TOOLS", "ManorApiError"]
