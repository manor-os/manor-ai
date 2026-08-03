"""First-party Workflow MCP-style tools for Manor agents.

There are two related surfaces:

* Authoring tools let an authenticated Manor agent create, inspect, validate,
  edit, deploy, test, and delete workflow definitions in its own entity.
* Runtime tools let an agent discover and call workflows explicitly published
  through an enabled ``mcp`` binding.

The visual editor's AI Edit and ``ai_edit_workflow`` both call
``services.workflow_generator``. This module is only the Agent/ToolPool
adapter; workflow semantics stay in the shared service and runner layers.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from packages.core.database import async_session


_WORKFLOW_TOOL_DEPTH: ContextVar[int] = ContextVar("workflow_tool_depth", default=0)


def _schema(
    name: str,
    description: str,
    properties: dict[str, dict] | None = None,
    required: list[str] | None = None,
) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


WORKFLOW_REF = {
    "workflow": {
        "type": "string",
        "description": "Workflow id or exact workflow name in the current Manor entity.",
    },
}

LIST_WORKFLOWS_SCHEMA = _schema(
    "list_workflows",
    "List workflows published as callable Manor Agent tools through an active MCP binding.",
)
RUN_WORKFLOW_SCHEMA = _schema(
    "run_workflow",
    "Run a workflow published as a Manor Agent tool. Use list_workflows first.",
    {
        **WORKFLOW_REF,
        "inputs": {"type": "object", "description": "Trigger input variables."},
    },
    ["workflow"],
)
LIST_DEFINITIONS_SCHEMA = _schema(
    "list_workflow_definitions",
    "List editable workflow definitions in the current Manor entity, including draft and inactive workflows.",
    {
        "query": {"type": "string", "description": "Optional name/description search."},
        "status": {"type": "string", "description": "Optional exact status filter."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    },
)
GET_WORKFLOW_SCHEMA = _schema(
    "get_workflow",
    "Get a complete workflow definition including nodes, edges, variables, metadata, version, and validation.",
    WORKFLOW_REF,
    ["workflow"],
)
CREATE_WORKFLOW_SCHEMA = _schema(
    "create_workflow",
    "Create a complete Manor workflow. Provide either a natural-language prompt (recommended for conversation) or an exact steps graph. The graph is validated before saving.",
    {
        "name": {"type": "string", "description": "Workflow name. Optional when prompt is provided."},
        "prompt": {"type": "string", "description": "Natural-language workflow design request. Uses the same AI engine as canvas AI Edit."},
        "steps": {"type": "array", "items": {"type": "object"}, "description": "Exact canonical node graph; use instead of prompt for deterministic authoring."},
        "description": {"type": "string"},
        "icon": {"type": "string", "default": "flow"},
        "variables": {"type": "object"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "allow_invalid": {"type": "boolean", "default": False, "description": "Save an incomplete draft graph despite validation errors."},
    },
)
AI_EDIT_WORKFLOW_SCHEMA = _schema(
    "ai_edit_workflow",
    "Edit an existing workflow from a natural-language request using the same AI graph editor as the Flow canvas. Returns the full updated graph and saves it by default.",
    {
        **WORKFLOW_REF,
        "prompt": {"type": "string", "description": "Requested change to the workflow."},
        "save": {"type": "boolean", "default": True, "description": "Persist the generated graph. Set false for preview."},
        "update_name": {"type": "boolean", "default": False, "description": "Also replace the current workflow name with the generated name."},
        "expected_version": {"type": "integer", "description": "Reject saving if the workflow has changed since this version."},
        "allow_invalid": {"type": "boolean", "default": False},
    },
    ["workflow", "prompt"],
)
UPDATE_WORKFLOW_SCHEMA = _schema(
    "update_workflow",
    "Update workflow metadata, variables, or the complete node graph. Use expected_version to avoid overwriting another editor.",
    {
        **WORKFLOW_REF,
        "name": {"type": "string"},
        "description": {"type": "string"},
        "icon": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object"}},
        "variables": {"type": "object"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "is_active": {"type": "boolean"},
        "status": {"type": "string"},
        "expected_version": {"type": "integer"},
        "allow_invalid": {"type": "boolean", "default": False},
    },
    ["workflow"],
)
VALIDATE_WORKFLOW_SCHEMA = _schema(
    "validate_workflow",
    "Validate a saved workflow or an unsaved steps graph. Checks entry node, node ids/types, edges, and reachability without executing it.",
    {
        **WORKFLOW_REF,
        "steps": {"type": "array", "items": {"type": "object"}},
    },
)
DEPLOY_WORKFLOW_SCHEMA = _schema(
    "deploy_workflow",
    "Deploy a valid workflow by creating a binding. Use trigger_type='mcp' to publish it as a callable Manor Agent tool.",
    {
        **WORKFLOW_REF,
        "trigger_type": {
            "type": "string",
            "enum": ["mcp", "manual", "webhook", "event", "workspace_event", "schedule"],
            "default": "mcp",
        },
        "target_workspace_id": {"type": "string", "description": "Optional target workspace id."},
        "name": {"type": "string", "description": "Optional deployment label."},
        "description": {"type": "string", "description": "Description exposed by an MCP deployment."},
        "trigger_config": {"type": "object"},
        "variables": {"type": "object"},
        "cron": {"type": "string", "description": "Cron expression when trigger_type is schedule."},
        "timezone": {"type": "string", "default": "UTC"},
    },
    ["workflow"],
)
DELETE_WORKFLOW_SCHEMA = _schema(
    "delete_workflow",
    "Delete a workflow definition. Refuses while deployments exist unless delete_bindings is explicitly true.",
    {
        **WORKFLOW_REF,
        "delete_bindings": {"type": "boolean", "default": False},
    },
    ["workflow"],
)
TEST_WORKFLOW_SCHEMA = _schema(
    "test_workflow",
    "Run any valid workflow definition inline for testing, without requiring an MCP deployment.",
    {
        **WORKFLOW_REF,
        "inputs": {"type": "object"},
    },
    ["workflow"],
)
TEST_NODE_SCHEMA = _schema(
    "test_workflow_node",
    "Execute one canonical workflow node in isolation with test inputs and return output, errors, and duration.",
    {
        "step": {"type": "object", "description": "Canonical workflow step definition."},
        "inputs": {"type": "object", "description": "Test workflow variables available to the node."},
    },
    ["step"],
)
LIST_RUNS_SCHEMA = _schema(
    "list_workflow_runs",
    "List workflow execution history in the current entity.",
    {
        "workflow": {"type": "string", "description": "Optional workflow id or exact name."},
        "target_workspace_id": {"type": "string"},
        "binding_id": {"type": "string"},
        "status": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
)
GET_RUN_SCHEMA = _schema(
    "get_workflow_run",
    "Get a workflow run with per-node results, variables, status, timing, and errors.",
    {"run_id": {"type": "string"}},
    ["run_id"],
)
CANCEL_RUN_SCHEMA = _schema(
    "cancel_workflow_run",
    "Cancel a pending, running, or paused workflow run.",
    {"run_id": {"type": "string"}},
    ["run_id"],
)
RESUME_RUN_SCHEMA = _schema(
    "resume_workflow_run",
    "Resume a paused workflow and optionally supply approval/event variables.",
    {
        "run_id": {"type": "string"},
        "inputs": {"type": "object"},
        "execute": {"type": "boolean", "default": True},
    },
    ["run_id"],
)
IMPORT_WORKFLOW_SCHEMA = _schema(
    "import_workflow",
    "Import an n8n, Dify, or ComfyUI workflow export into Manor. Use dry_run first to inspect mapping coverage.",
    {
        "content": {"type": "string", "description": "Raw exported JSON or YAML."},
        "name": {"type": "string"},
        "dry_run": {"type": "boolean", "default": True},
        "target_workspace_id": {"type": "string"},
        "create_binding": {"type": "boolean", "default": False},
    },
    ["content"],
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(message: str, **extra: Any) -> str:
    return _dump({"ok": False, "error": message, **extra})


def _workflow_dict(wf, *, include_steps: bool = True) -> dict:
    payload = {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description or "",
        "icon": wf.icon or "flow",
        "trigger_type": wf.trigger_type,
        "trigger_config": dict(wf.trigger_config or {}),
        "variables": dict(wf.variables or {}),
        "category": wf.category,
        "tags": list(wf.tags or []),
        "is_active": bool(wf.is_active),
        "status": wf.status,
        "version": int(wf.version or 1),
    }
    if include_steps:
        payload["steps"] = list(wf.steps or [])
    return payload


def _run_dict(run, *, detailed: bool = True) -> dict:
    payload = {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "workspace_id": run.workspace_id,
        "binding_id": run.binding_id,
        "trigger_source": run.trigger_source,
        "status": run.status,
        "current_step_id": run.current_step_id,
        "error": run.error,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }
    if detailed:
        payload["variables"] = dict(run.variables or {})
        payload["step_results"] = dict(run.step_results or {})
        payload["trigger_data"] = dict(run.trigger_data or {})
    return payload


def _binding_dict(binding) -> dict:
    return {
        "id": binding.id,
        "workflow_id": binding.workflow_id,
        "workspace_id": binding.workspace_id,
        "name": binding.name,
        "trigger_type": binding.trigger_type,
        "trigger_config": dict(binding.trigger_config or {}),
        "variables": dict(binding.variables or {}),
        "enabled": bool(binding.enabled),
        "status": binding.status,
    }


async def _resolve_workflow(db, entity_id: str, target: object):
    from packages.core.services import workflow_service as svc

    value = str(target or "").strip()
    if not value:
        return None
    by_id = await svc.get_workflow(db, value, entity_id)
    if by_id is not None:
        return by_id
    workflows = await svc.list_workflows(db, entity_id)
    return next((wf for wf in workflows if wf.name.lower() == value.lower()), None)


async def _exposed_workflows(
    db,
    entity_id: str,
    *,
    workspace_id: str | None = None,
) -> list[tuple[Any, Any]]:
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    rows = (await db.execute(
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowBinding.trigger_type == "mcp",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
    )).all()
    from packages.core.services.workspace_workflow_router import prefer_workspace_bindings

    return prefer_workspace_bindings(rows, workspace_id=workspace_id)


async def _list_workflows(entity_id: str = "", **kwargs: Any) -> str:
    workspace_id = str(kwargs.get("workspace_id") or "").strip() or None
    async with async_session() as db:
        pairs = await _exposed_workflows(db, entity_id, workspace_id=workspace_id)
        return _dump({
            "ok": True,
            "workflows": [
                {
                    "id": wf.id,
                    "name": wf.name,
                    "description": (binding.trigger_config or {}).get("description") or wf.description or "",
                    "inputs": sorted((wf.variables or {}).keys()),
                    "binding_id": binding.id,
                }
                for binding, wf in pairs
            ],
        })


async def _list_workflow_definitions(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    query = str(kwargs.get("query") or "").strip().lower()
    status = str(kwargs.get("status") or "").strip().lower()
    limit = max(1, min(int(kwargs.get("limit") or 25), 100))
    async with async_session() as db:
        workflows = await svc.list_workflows(db, entity_id)
        if query:
            workflows = [wf for wf in workflows if query in f"{wf.name} {wf.description or ''}".lower()]
        if status:
            workflows = [wf for wf in workflows if str(wf.status).lower() == status]
        items = [_workflow_dict(wf, include_steps=False) for wf in workflows[:limit]]
        return _dump({"ok": True, "workflows": items, "count": len(items)})


async def _get_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        payload = _workflow_dict(wf)
        payload["validation"] = svc.validate_workflow_steps(wf.steps)
        payload["deployments"] = [
            _binding_dict(binding)
            for binding in await svc.list_bindings(db, entity_id, workflow_id=wf.id)
        ]
        return _dump({"ok": True, "workflow": payload})


async def _create_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc
    from packages.core.services.workflow_generator import generate_workflow

    steps = kwargs.get("steps")
    variables = kwargs.get("variables")
    generated = None
    prompt = str(kwargs.get("prompt") or "").strip()
    if not isinstance(steps, list):
        if not prompt:
            return _error("Provide either 'prompt' or a complete 'steps' graph")
        try:
            generated = await generate_workflow(prompt, None, entity_id)
        except Exception as exc:
            return _error(f"AI workflow generation failed: {exc}")
        steps = generated.get("steps")
        if not isinstance(variables, dict):
            variables = generated.get("variables")

    validation = svc.validate_workflow_steps(steps)
    if not validation["valid"] and not bool(kwargs.get("allow_invalid")):
        return _error("Workflow graph is invalid", validation=validation)

    name = str(kwargs.get("name") or (generated or {}).get("name") or "Untitled workflow").strip()
    if not name:
        return _error("Workflow name is required")
    async with async_session() as db:
        wf = await svc.create_workflow(
            db,
            entity_id=entity_id,
            created_by=str(kwargs.get("user_id") or "").strip() or None,
            name=name[:255],
            steps=steps or [],
            description=str(kwargs.get("description") or "").strip() or None,
            icon=str(kwargs.get("icon") or "flow").strip() or "flow",
            variables=variables if isinstance(variables, dict) else {},
            category=str(kwargs.get("category") or "").strip() or None,
            tags=[str(tag) for tag in (kwargs.get("tags") or []) if str(tag).strip()],
        )
        await db.commit()
        return _dump({"ok": True, "created": True, "workflow": _workflow_dict(wf), "validation": validation})


async def _ai_edit_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc
    from packages.core.services.workflow_generator import generate_workflow

    prompt = str(kwargs.get("prompt") or "").strip()
    if not prompt:
        return _error("AI Edit requires a prompt")
    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        expected = kwargs.get("expected_version")
        if expected is not None and int(expected) != int(wf.version or 1):
            return _error("Workflow version conflict", expected_version=int(expected), current_version=int(wf.version or 1))
        try:
            generated = await generate_workflow(prompt, list(wf.steps or []), entity_id)
        except Exception as exc:
            return _error(f"AI workflow edit failed: {exc}")
        validation = svc.validate_workflow_steps(generated.get("steps"))
        if not validation["valid"] and not bool(kwargs.get("allow_invalid")):
            return _error("AI Edit produced an invalid graph", validation=validation, draft=generated)
        if not bool(kwargs.get("save", True)):
            return _dump({"ok": True, "saved": False, "draft": generated, "validation": validation, "version": wf.version})
        changes: dict[str, Any] = {
            "steps": generated.get("steps") or [],
            "variables": {
                **dict(wf.variables or {}),
                **(
                    generated.get("variables")
                    if isinstance(generated.get("variables"), dict)
                    else {}
                ),
            },
        }
        if bool(kwargs.get("update_name")) and generated.get("name"):
            changes["name"] = str(generated["name"])[:255]
        wf = await svc.update_workflow(db, wf.id, entity_id, **changes)
        await db.commit()
        return _dump({"ok": True, "saved": True, "workflow": _workflow_dict(wf), "validation": validation})


async def _update_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        expected = kwargs.get("expected_version")
        if expected is not None and int(expected) != int(wf.version or 1):
            return _error("Workflow version conflict", expected_version=int(expected), current_version=int(wf.version or 1))
        changes = {
            key: kwargs[key]
            for key in ("name", "description", "icon", "steps", "variables", "category", "tags", "is_active", "status")
            if key in kwargs
        }
        if not changes:
            return _error("No workflow changes were provided")
        validation = svc.validate_workflow_steps(changes.get("steps", wf.steps))
        if "steps" in changes and not validation["valid"] and not bool(kwargs.get("allow_invalid")):
            return _error("Workflow graph is invalid", validation=validation)
        wf = await svc.update_workflow(db, wf.id, entity_id, **changes)
        await db.commit()
        return _dump({"ok": True, "updated": True, "workflow": _workflow_dict(wf), "validation": validation})


async def _validate_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    steps = kwargs.get("steps")
    workflow = None
    if not isinstance(steps, list):
        async with async_session() as db:
            workflow = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
            if workflow is None:
                return _error("Provide 'workflow' or an unsaved 'steps' graph")
            steps = workflow.steps
    return _dump({
        "ok": True,
        "workflow_id": workflow.id if workflow is not None else None,
        "validation": svc.validate_workflow_steps(steps),
    })


async def _deploy_workflow(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    trigger_type = str(kwargs.get("trigger_type") or "mcp").strip().lower()
    workspace_id = str(kwargs.get("target_workspace_id") or "").strip() or None
    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        validation = svc.validate_workflow_steps(wf.steps)
        if not validation["valid"]:
            return _error("Workflow must be valid before deployment", validation=validation)
        if not wf.is_active or wf.status != "active":
            return _error("Workflow must be active before deployment")
        if trigger_type == "schedule":
            cron = str(kwargs.get("cron") or (kwargs.get("trigger_config") or {}).get("cron") or "").strip()
            if not cron:
                return _error("Scheduled deployment requires cron")
            job = await svc.schedule_workflow(
                db,
                entity_id=entity_id,
                workflow_id=wf.id,
                cron=cron,
                name=str(kwargs.get("name") or wf.name),
                workspace_id=workspace_id,
                timezone_str=str(kwargs.get("timezone") or "UTC"),
                created_by=user_id or None,
            )
            await db.commit()
            return _dump({"ok": True, "kind": "automation", "scheduled_job_id": job.id, "workflow_id": wf.id})

        existing = await svc.list_bindings(db, entity_id, workspace_id=workspace_id, workflow_id=wf.id)
        match = next((binding for binding in existing if binding.trigger_type == trigger_type and binding.status == "active"), None)
        if match is not None:
            return _dump({"ok": True, "created": False, "deployment": _binding_dict(match), "validation": validation})
        config = dict(kwargs.get("trigger_config") or {})
        if trigger_type == "mcp" and kwargs.get("description"):
            config["description"] = str(kwargs["description"])
        binding = await svc.create_workflow_binding(
            db,
            entity_id=entity_id,
            workflow_id=wf.id,
            workspace_id=workspace_id,
            name=str(kwargs.get("name") or wf.name),
            trigger_type=trigger_type,
            trigger_config=config,
            variables=kwargs.get("variables") if isinstance(kwargs.get("variables"), dict) else {},
        )
        await db.commit()
        return _dump({"ok": True, "created": True, "deployment": _binding_dict(binding), "validation": validation})


async def _delete_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        bindings = await svc.list_bindings(db, entity_id, workflow_id=wf.id)
        if bindings and not bool(kwargs.get("delete_bindings")):
            return _error(
                "Workflow has deployments; set delete_bindings=true to remove them with the definition",
                binding_ids=[binding.id for binding in bindings],
            )
        for binding in bindings:
            await svc.delete_binding(db, binding.id, entity_id)
        deleted = await svc.delete_workflow(db, wf.id, entity_id)
        await db.commit()
        return _dump({"ok": deleted, "deleted": deleted, "workflow_id": wf.id, "deleted_bindings": len(bindings)})


async def _execute_run(run_id: str) -> None:
    from packages.core.ai.workflow_runner import WorkflowRunner

    await WorkflowRunner().run(run_id)


async def _run_workflow(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    target = str(kwargs.get("workflow") or "").strip()
    inputs = kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else {}
    workspace_id = str(kwargs.get("workspace_id") or "").strip() or None
    if not target:
        return _error("run_workflow requires a workflow")
    async with async_session() as db:
        pairs = await _exposed_workflows(db, entity_id, workspace_id=workspace_id)
        match = next(((binding, wf) for binding, wf in pairs if wf.id == target or wf.name.lower() == target.lower()), None)
        if match is None:
            return _error("Workflow is not published as an Agent tool", available=[wf.name for _, wf in pairs])
        depth = _WORKFLOW_TOOL_DEPTH.get()
        if depth >= 3:
            return _error("Workflow nesting limit reached")
        binding, _ = match
        runtime_context = {
            key: value
            for key in ("workspace_id", "conversation_id", "task_id")
            if (value := str(kwargs.get(key) or "").strip())
        }
        run = await svc.start_workflow_from_binding(
            db,
            binding,
            variables=inputs,
            trigger_data={
                "_workflow_tool_depth": depth + 1,
                **({"runtime_context": runtime_context} if runtime_context else {}),
            },
            trigger_source="mcp",
            started_by=user_id or None,
            execution_workspace_id=workspace_id,
        )
        await db.commit()
        run_id = run.id
    depth_token = _WORKFLOW_TOOL_DEPTH.set(depth + 1)
    try:
        await _execute_run(run_id)
    finally:
        _WORKFLOW_TOOL_DEPTH.reset(depth_token)
    async with async_session() as db:
        done = await svc.get_run(db, run_id, entity_id)
        return _dump({"ok": done is not None and done.status != "failed", "run": _run_dict(done) if done else None})


async def _test_workflow(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    inputs = kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else {}
    async with async_session() as db:
        wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
        if wf is None:
            return _error("Workflow not found")
        validation = svc.validate_workflow_steps(wf.steps)
        if not validation["valid"]:
            return _error("Workflow is invalid", validation=validation)
        try:
            run = await svc.start_workflow(db, entity_id, wf.id, variables=inputs, started_by=user_id or None, trigger_source="agent_test")
        except ValueError as exc:
            return _error(str(exc))
        await db.commit()
        run_id = run.id
    await _execute_run(run_id)
    async with async_session() as db:
        done = await svc.get_run(db, run_id, entity_id)
        return _dump({"ok": done is not None and done.status != "failed", "run": _run_dict(done) if done else None})


async def _test_workflow_node(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    step = kwargs.get("step")
    if not isinstance(step, dict):
        return _error("step must be an object")
    inputs = kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else {}
    run = WorkflowRun(
        id="adhoc",
        workflow_id="adhoc",
        entity_id=entity_id,
        status="running",
        variables=dict(inputs),
        step_results={},
        trigger_data={},
        started_by=user_id or None,
    )
    started = perf_counter()
    async with async_session() as db:
        try:
            result = await WorkflowRunner()._execute_step_safe(step, run, db)
        except Exception as exc:
            return _error(str(exc), step_id=step.get("id"), duration_ms=round((perf_counter() - started) * 1000, 2))
    return _dump({"ok": result.get("status") != "failed", "step_id": step.get("id"), "result": result, "duration_ms": round((perf_counter() - started) * 1000, 2)})


async def _list_workflow_runs(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    workflow_id = None
    async with async_session() as db:
        if kwargs.get("workflow"):
            wf = await _resolve_workflow(db, entity_id, kwargs.get("workflow"))
            if wf is None:
                return _error("Workflow not found")
            workflow_id = wf.id
        runs = await svc.list_runs(
            db,
            entity_id,
            workflow_id=workflow_id,
            workspace_id=str(kwargs.get("target_workspace_id") or "").strip() or None,
            binding_id=str(kwargs.get("binding_id") or "").strip() or None,
            status=str(kwargs.get("status") or "").strip() or None,
            limit=max(1, min(int(kwargs.get("limit") or 20), 100)),
        )
        return _dump({"ok": True, "runs": [_run_dict(run, detailed=False) for run in runs], "count": len(runs)})


async def _get_workflow_run(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        run = await svc.get_run(db, str(kwargs.get("run_id") or ""), entity_id)
        if run is None:
            return _error("Workflow run not found")
        return _dump({"ok": True, "run": _run_dict(run)})


async def _cancel_workflow_run(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.services import workflow_service as svc

    async with async_session() as db:
        run = await svc.get_run(db, str(kwargs.get("run_id") or ""), entity_id)
        if run is None:
            return _error("Workflow run not found")
        if run.status in {"completed", "cancelled", "failed"}:
            return _error(f"Run is already {run.status}", run=_run_dict(run, detailed=False))
        run.status = "cancelled"
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return _dump({"ok": True, "run": _run_dict(run)})


async def _resume_workflow_run(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services import workflow_service as svc

    run_id = str(kwargs.get("run_id") or "")
    async with async_session() as db:
        run = await svc.get_run(db, run_id, entity_id)
        if run is None:
            return _error("Workflow run not found")
        if run.status != "paused":
            return _error("Workflow run is not paused", status=run.status)
    outcome = await WorkflowRunner.resume(
        run_id,
        kwargs.get("inputs") if isinstance(kwargs.get("inputs"), dict) else None,
        entity_id=entity_id,
        resumed_by=str(kwargs.get("user_id") or "").strip() or None,
        execute=bool(kwargs.get("execute", True)),
    )
    if outcome in {"not_found", "not_paused", "invalid_approval"}:
        return _error(f"Could not resume workflow run: {outcome}")
    async with async_session() as db:
        run = await svc.get_run(db, run_id, entity_id)
        return _dump({"ok": True, "run": _run_dict(run) if run else None})


async def _import_workflow(entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.ai.workflow_import import UnknownWorkflowFormat, import_workflow
    from packages.core.services import workflow_service as svc

    content = str(kwargs.get("content") or "")
    try:
        if bool(kwargs.get("dry_run", True)):
            result = import_workflow(content, name=str(kwargs.get("name") or "").strip() or None)
            return _dump({"ok": True, "saved": False, "report": result.report.to_dict(), "definition": result.definition})
        async with async_session() as db:
            wf, binding, report = await svc.import_workflow_definition(
                db,
                entity_id=entity_id,
                raw=content,
                name=str(kwargs.get("name") or "").strip() or None,
                workspace_id=str(kwargs.get("target_workspace_id") or "").strip() or None,
                create_binding=bool(kwargs.get("create_binding")),
            )
            await db.commit()
            return _dump({"ok": True, "saved": True, "workflow": _workflow_dict(wf), "binding_id": binding.id if binding else None, "report": report})
    except UnknownWorkflowFormat as exc:
        return _error(str(exc))


def get_tools() -> list[tuple[dict, Any]]:
    """Return the complete first-party Workflow tool package."""
    return [
        (LIST_WORKFLOWS_SCHEMA, _list_workflows),
        (RUN_WORKFLOW_SCHEMA, _run_workflow),
        (LIST_DEFINITIONS_SCHEMA, _list_workflow_definitions),
        (GET_WORKFLOW_SCHEMA, _get_workflow),
        (CREATE_WORKFLOW_SCHEMA, _create_workflow),
        (AI_EDIT_WORKFLOW_SCHEMA, _ai_edit_workflow),
        (UPDATE_WORKFLOW_SCHEMA, _update_workflow),
        (VALIDATE_WORKFLOW_SCHEMA, _validate_workflow),
        (DEPLOY_WORKFLOW_SCHEMA, _deploy_workflow),
        (DELETE_WORKFLOW_SCHEMA, _delete_workflow),
        (TEST_WORKFLOW_SCHEMA, _test_workflow),
        (TEST_NODE_SCHEMA, _test_workflow_node),
        (LIST_RUNS_SCHEMA, _list_workflow_runs),
        (GET_RUN_SCHEMA, _get_workflow_run),
        (CANCEL_RUN_SCHEMA, _cancel_workflow_run),
        (RESUME_RUN_SCHEMA, _resume_workflow_run),
        (IMPORT_WORKFLOW_SCHEMA, _import_workflow),
    ]
