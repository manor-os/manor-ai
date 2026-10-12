"""Project live Workspace capabilities into its app panel."""
from __future__ import annotations

import re
from typing import Any


_ACTION_KEY = re.compile(r"^(workflow|service|automation|action):[A-Za-z0-9_.:-]{1,128}$")
_FIELD_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


async def load_workspace_app_view(
    db: Any, workspace: Any, *, user: Any, include_catalog: bool = True,
) -> dict[str, Any]:
    """Use the same actor-visible target catalog for Chat and Config."""
    from sqlalchemy import exists, or_, select
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.models.workflow import WorkflowDefinition, WorkflowTemplateInstallation
    from packages.core.models.permission import ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, readable_resource_ids
    from packages.core.services.workspace_workflow_router import list_workspace_chat_entrypoints
    from packages.core.services.workspace_workflow_router import workflow_run_inputs

    entrypoints = await list_workspace_chat_entrypoints(
        db, entity_id=user.entity_id, workspace_id=workspace.id,
        user=user, require_control=True, include_unpublished=True,
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
    jobs = [job for job in jobs if (job.execution_target or {}).get("managed_by") != "agent_initiative"]
    view = project_workspace_app_view(workspace, entrypoints, jobs, agents)
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
         "inputs": [item for item in workflow_run_inputs(row) if not item.get("hidden")]}
        for row in workflows if row.id in readable and row.id not in attached_ids
        and validate_workflow_definition(row)["valid"]
    ]
    return view


def normalize_app_view_config(value: Any) -> dict[str, Any]:
    """Validate the small page config before resolving its live targets."""
    if value is None:
        return {"version": 1, "eyebrow": None, "title": None, "description": None, "hero": None,
                "order": [], "hidden": [], "labels": {}, "descriptions": {}, "forms": {}, "targets": {},
                "entries": {}, "prompts": {},
                "visual": "auto", "layout": "feature", "motion_cover_url": None,
                "motion_cover_source_url": None}
    if not isinstance(value, dict):
        raise ValueError("app_view must be an object")
    unknown = set(value) - {"version", "eyebrow", "title", "description", "hero", "order", "hidden", "labels", "descriptions", "forms", "targets", "entries", "prompts", "visual", "layout", "motion_cover_url", "motion_cover_source_url"}
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
    motion_cover_url = value.get("motion_cover_url")
    motion_cover_source_url = value.get("motion_cover_source_url")
    if motion_cover_url == "":
        motion_cover_url = None
    if motion_cover_source_url == "":
        motion_cover_source_url = None
    for field, url in (("motion_cover_url", motion_cover_url), ("motion_cover_source_url", motion_cover_source_url)):
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

    return {
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
        "visual": visual,
        "layout": layout,
        "motion_cover_url": motion_cover_url,
        "motion_cover_source_url": motion_cover_source_url,
    }


def app_target_kind_and_id(key: str, target: str) -> tuple[str, str]:
    """Typed targets work for every entry; retain the original bare-ID config."""
    for prefix, kind in (("agent:", "chat"), ("workflow:", "workflow"), ("automation:", "automation")):
        if target.startswith(prefix):
            return kind, target[len(prefix):]
    source, source_id = key.split(":", 1)
    return {"service": "chat", "action": "chat", "workflow": "workflow", "automation": "automation"}[source], target or source_id


def project_workspace_app_view(
    workspace: Any, entrypoints: list[Any], automations: list[Any] | None = None,
    agents: list[dict[str, str]] | None = None,
    starter_cover_url: str | None = None,
) -> dict[str, Any]:
    """Derive the visible actions afresh, so removed bindings cannot linger."""
    model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    try:
        config = normalize_app_view_config(model.get("app_view"))
    except ValueError:
        config = normalize_app_view_config(None)

    actions: list[dict[str, Any]] = []
    flows_by_id = {entry.binding_id: entry for entry in entrypoints}
    agents = agents or []
    agents_by_id = {agent["id"]: agent for agent in agents}
    agents_by_service = {agent.get("service_key"): agent for agent in agents if agent.get("service_key")}
    automations = automations or []
    def flow_inputs(entry: Any) -> list[dict[str, Any]]:
        return [
            {field: item[field] for field in (
                "key", "label", "type", "required", "placeholder", "description", "default", "schema",
            ) if field in item}
            for item in entry.run_inputs if not item.get("hidden")
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

    automation_options = [automation_target(job) for job in automations]
    jobs_by_id = {job["job_id"]: job for job in automation_options}
    for entry in entrypoints:
        key = f"workflow:{entry.binding_id}"
        actions.append({
            "key": key, "kind": "workflow", "binding_id": entry.binding_id, "workflow_id": entry.workflow_id,
            "title": config["labels"].get(key) or entry.title, "source_title": entry.title,
            "source_description": entry.description,
            "description": config["descriptions"].get(key, entry.description),
            "placeholder": entry.placeholder, "inputs": flow_inputs(entry),
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

    for job in automation_options:
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
                      "cron_expr", "timezone", "instructions", "target_title", "target_workflow_id"):
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
                          placeholder=selected.placeholder if selected else "", inputs=flow_inputs(selected) if selected else [],
                          available=selected is not None)
        else:
            job = jobs_by_id.get(target_id)
            action.update({field: value for field, value in (job or {}).items() if field not in ("title", "description")})
            action.update(available=job is not None,
                          description=config["descriptions"].get(key, job["description"] if job else ""))

    visible = [action for action in actions if action["key"] not in config["hidden"]]
    positions = {key: index for index, key in enumerate(config["order"])}
    visible.sort(key=lambda action: positions.get(action["key"], len(positions) + actions.index(action)))
    hero = next((action["key"] for action in visible if action["key"] == config["hero"]), None)
    default_hero = next((action["key"] for action in visible
                         if action["kind"] == "workflow" and action.get("available") is not False), None)
    workspace_cover_url = getattr(workspace, "cover_image_url", None)
    return {
        "workspace_id": workspace.id,
        "eyebrow": config["eyebrow"],
        "title": config["title"] if config["title"] is not None else workspace.name,
        "description": config["description"] if config["description"] is not None else (workspace.primary_work or workspace.operating_context or ""),
        "status": workspace.status,
        "cover_image_url": workspace_cover_url or starter_cover_url,
        "cover_source": "workspace" if workspace_cover_url else "starter" if starter_cover_url else "none",
        "hero": hero if config["hero"] else default_hero,
        "actions": visible,
        "available_actions": actions,
        "workflow_options": [
            {"binding_id": entry.binding_id, "workflow_id": entry.workflow_id,
             "title": entry.title, "description": entry.description,
             "placeholder": entry.placeholder, "inputs": flow_inputs(entry)}
            for entry in entrypoints
        ],
        "automation_options": automation_options,
        "agent_options": [{"id": agent["id"], "name": agent["name"]} for agent in agents],
        "config": config,
    }
