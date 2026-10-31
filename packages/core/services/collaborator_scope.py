"""What an external Workspace collaborator may reference.

A collaborator is a person whose membership role in a company is
``collaborator`` (derived from a WorkspaceCollaboration grant). They work
inside the Workspaces their live grants cover -- ``collaborator_workspace_ids``
-- and nothing else in that company. Any write that names another resource by
id (a workflow, skill, agent, subscription, conversation, task, goal, stat
or scheduled job) must name one that belongs to the Workspace being written,
otherwise a collaborator could wire company resources into their Workspace.

The decision is keyed on the membership role, never on a resolved StaffRole
name, and on live grants, never on the derived Workspace seat. HTTP handlers
and runtime tools both go through ``require_collaborator_references``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from fastapi import HTTPException
from sqlalchemy import select

from packages.core.models.user import UserMembership
from packages.core.services.external_collaboration_access import (
    COLLABORATOR_MEMBERSHIP_ROLE,
    collaborator_workspace_ids,
)

_UNSET = object()
# A platform skill (no company) is usable from any Workspace.
ANY_WORKSPACE = "*"


class CollaboratorScopeError(HTTPException):
    """A collaborator named a resource outside the Workspace; 404 hides whether it exists."""

    def __init__(self) -> None:
        super().__init__(404, "Not found in this Workspace")


async def _collaborator_membership(db, *, user_id: str | None, entity_id: str | None):
    if not user_id or not entity_id:
        return None
    return (
        await db.execute(
            select(UserMembership.status, UserMembership.deleted_at).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == entity_id,
                UserMembership.role == COLLABORATOR_MEMBERSHIP_ROLE,
            )
        )
    ).first()


async def is_collaborator_member(db, *, user_id: str | None, entity_id: str | None) -> bool:
    """Whether the person's membership in the company is a collaborator one, active or not."""
    return await _collaborator_membership(db, user_id=user_id, entity_id=entity_id) is not None


async def actor_workspace_scope(db, *, user_id: str | None, entity_id: str | None, user=None) -> frozenset[str] | None:
    """Live-granted Workspaces for a collaborator actor, or None for anyone else.

    Decided from the request or run context only, never by a lookup: an HTTP
    request has resolved the actor in deps.get_current_user (left on the User,
    and marked acting), and a runtime call's gate marked the collaborators
    behind it (``collaborators_acting_now``). Anyone not marked is not a
    collaborator here, so ordinary users cost no query. Only a marked
    collaborator's membership and live grants are read.
    """
    stashed = getattr(user, "_collaborator_workspace_ids", _UNSET) if user is not None else _UNSET
    if stashed is not _UNSET:
        return stashed
    from packages.core.services.collaborator_steering import collaborators_acting_now

    if not user_id or str(user_id) not in collaborators_acting_now(entity_id):
        return None
    return await collaborator_live_workspaces(db, user_id=str(user_id), entity_id=entity_id)


async def collaborator_live_workspaces(db, *, user_id: str | None, entity_id: str | None) -> frozenset[str] | None:
    """A collaborator membership's live-granted Workspaces, read from the database; None for anyone else.

    A collaborator membership that is no longer active scopes to nothing: an
    ended collaboration never falls back to the company-wide rules. For the
    runtime gate, which decides who is a collaborator; everything else reads
    the context through ``actor_workspace_scope``.
    """
    membership = await _collaborator_membership(db, user_id=user_id, entity_id=entity_id)
    if membership is None:
        return None
    if membership.status != "active" or membership.deleted_at is not None:
        return frozenset()
    from packages.core.services.collaborator_session import session_pin

    live = await collaborator_workspace_ids(db, user_id=user_id, entity_id=entity_id)
    # Inside a scoped session's request (or a run it started in-process),
    # only the Workspace that session pins.
    pin = session_pin(entity_id, user_id)
    return live if pin is None else live & pin


def _ids(values: Iterable[str | None]) -> list[str]:
    return sorted({str(value) for value in values if value})


async def agent_workspace_ids_batch(db, agent_ids: Iterable[str | None], *, entity_id: str) -> dict[str, set[str]]:
    """Each Agent's Workspaces: its home Workspace and its active subscriptions."""
    from packages.core.models.workspace import Agent, AgentSubscription

    ids = _ids(agent_ids)
    result: dict[str, set[str]] = {agent_id: set() for agent_id in ids}
    if not ids:
        return result
    homes = await db.execute(
        select(Agent.id, Agent.workspace_id).where(
            Agent.id.in_(ids), Agent.entity_id == entity_id, Agent.deleted_at.is_(None)
        )
    )
    subscriptions = await db.execute(
        select(AgentSubscription.agent_id, AgentSubscription.workspace_id).where(
            AgentSubscription.agent_id.in_(ids),
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.status == "active",
        )
    )
    for agent_id, workspace_id in [*homes.all(), *subscriptions.all()]:
        if workspace_id:
            result[str(agent_id)].add(str(workspace_id))
    return result


async def agent_workspace_ids(db, *, agent_id: str | None, entity_id: str) -> set[str]:
    return (await agent_workspace_ids_batch(db, [agent_id], entity_id=entity_id)).get(str(agent_id or ""), set())


async def workflow_workspace_ids_batch(
    db, workflow_ids: Iterable[str | None], *, entity_id: str
) -> dict[str, set[str]]:
    """Each Workflow's Workspaces: its own Workspace and the Workspaces binding it."""
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    ids = _ids(workflow_ids)
    result: dict[str, set[str]] = {workflow_id: set() for workflow_id in ids}
    if not ids:
        return result
    homes = await db.execute(
        select(WorkflowDefinition.id, WorkflowDefinition.workspace_id).where(
            WorkflowDefinition.id.in_(ids), WorkflowDefinition.entity_id == entity_id
        )
    )
    bound = await db.execute(
        select(WorkflowBinding.workflow_id, WorkflowBinding.workspace_id).where(
            WorkflowBinding.workflow_id.in_(ids), WorkflowBinding.entity_id == entity_id
        )
    )
    for workflow_id, workspace_id in [*homes.all(), *bound.all()]:
        if workspace_id:
            result[str(workflow_id)].add(str(workspace_id))
    return result


async def workflow_workspace_ids(db, *, workflow_id: str | None, entity_id: str) -> set[str]:
    batch = await workflow_workspace_ids_batch(db, [workflow_id], entity_id=entity_id)
    return batch.get(str(workflow_id or ""), set())


async def _skill_workspace_ids(db, skill_ids: list[str], *, entity_id: str) -> dict[str, set[str]]:
    """A skill belongs to its own Workspace and to the Workspaces of Agents it is bound to."""
    from packages.core.models.skill import AgentSkillBinding, Skill

    result: dict[str, set[str]] = {skill_id: set() for skill_id in skill_ids}
    rows = (
        await db.execute(select(Skill.id, Skill.entity_id, Skill.workspace_id).where(Skill.id.in_(skill_ids)))
    ).all()
    for skill_id, skill_entity_id, workspace_id in rows:
        if skill_entity_id is None:
            result[str(skill_id)].add(ANY_WORKSPACE)
        elif skill_entity_id == entity_id and workspace_id:
            result[str(skill_id)].add(str(workspace_id))
    bindings = (
        await db.execute(
            select(AgentSkillBinding.skill_id, AgentSkillBinding.agent_id).where(AgentSkillBinding.skill_id.in_(skill_ids))
        )
    ).all()
    agents = await agent_workspace_ids_batch(db, [agent_id for _, agent_id in bindings], entity_id=entity_id)
    for skill_id, agent_id in bindings:
        result[str(skill_id)] |= agents.get(str(agent_id), set())
    return result


async def skills_invocable_in_workspace(db, skill_ids, *, entity_id: str, workspace_id: str) -> set[str]:
    """The skills a collaborator may invoke in ``workspace_id``: platform skills, the
    Workspace's own, and those bound to its Agents (the reference check's rule)."""
    ids = _ids(skill_ids)
    if not ids:
        return set()
    workspaces = await _skill_workspace_ids(db, ids, entity_id=entity_id)
    return {skill_id for skill_id, found in workspaces.items() if found & {workspace_id, ANY_WORKSPACE}}


def _column_lookup(model_path: str, model_name: str):
    async def lookup(db, ids: list[str], *, entity_id: str) -> dict[str, set[str]]:
        import importlib

        model = getattr(importlib.import_module(model_path), model_name)
        rows = (
            await db.execute(select(model.id, model.workspace_id).where(model.id.in_(ids), model.entity_id == entity_id))
        ).all()
        result: dict[str, set[str]] = {resource_id: set() for resource_id in ids}
        for resource_id, workspace_id in rows:
            if workspace_id:
                result[str(resource_id)].add(str(workspace_id))
        return result
    return lookup


async def scheduled_job_workspace_ids(db, ids: list[str], *, entity_id: str) -> dict[str, set[str]]:
    """Resolve each reference by primary key first, then by job_id, as get_scheduled_job does."""
    from packages.core.models.scheduler import ScheduledJob

    result: dict[str, set[str]] = {resource_id: set() for resource_id in ids}
    found: set[str] = set()
    for column in (ScheduledJob.id, ScheduledJob.job_id):
        pending = [resource_id for resource_id in ids if resource_id not in found]
        if not pending:
            break
        rows = (
            await db.execute(
                select(column, ScheduledJob.workspace_id).where(column.in_(pending), ScheduledJob.entity_id == entity_id)
            )
        ).all()
        for key, workspace_id in rows:
            found.add(str(key))
            if workspace_id:
                result[str(key)].add(str(workspace_id))
    return result


async def _agents(db, ids, *, entity_id):
    return await agent_workspace_ids_batch(db, ids, entity_id=entity_id)


async def _workflows(db, ids, *, entity_id):
    return await workflow_workspace_ids_batch(db, ids, entity_id=entity_id)


_LOOKUPS = {
    "agent": _agents,
    "workflow": _workflows,
    "skill": _skill_workspace_ids,
    "workflow_binding": _column_lookup("packages.core.models.workflow", "WorkflowBinding"),
    "agent_subscription": _column_lookup("packages.core.models.workspace", "AgentSubscription"),
    "conversation": _column_lookup("packages.core.models.task", "Conversation"),
    "task": _column_lookup("packages.core.models.task", "Task"),
    "goal": _column_lookup("packages.core.models.goal", "Goal"),
    "stat": _column_lookup("packages.core.models.workspace_stat", "WorkspaceStat"),
    "scheduled_job": scheduled_job_workspace_ids,
}
KINDS = frozenset(_LOOKUPS)


async def references_outside(
    db, *, entity_id: str, workspace_id: str, refs: Mapping[str, Iterable[str | None]]
) -> list[tuple[str, str]]:
    """The (kind, id) references that do not belong to ``workspace_id``."""
    from packages.core.constants.agents import is_master_agent

    outside: list[tuple[str, str]] = []
    for kind, values in refs.items():
        ids = [value for value in _ids(values) if not (kind == "agent" and is_master_agent(value))]
        if not ids:
            continue
        workspaces = await _LOOKUPS[kind](db, ids, entity_id=entity_id)
        outside.extend(
            (kind, resource_id)
            for resource_id in ids
            if not workspaces.get(resource_id, set()) & {workspace_id, ANY_WORKSPACE}
        )
    return outside


_PAYLOAD_REFERENCE_KEYS = {
    kind: {
        key
        for name in names
        for key in (name, "".join(part.capitalize() if i else part for i, part in enumerate(name.split("_"))))
    }
    for kind, names in {
        "agent": ("agent_id", "agent_ids", "recommended_agent_id"),
        "skill": ("skill", "skill_id", "skill_ids"),
        "workflow": ("workflow_id", "workflow_ids"),
    }.items()
}


def payload_references(payload) -> dict[str, set[str]]:
    """Agent, skill and workflow references anywhere inside a configuration payload.

    Singular and plural keys, snake_case or camelCase, any string value: a
    value that is not an id of the Workspace's resources fails the check.
    """
    refs: dict[str, set[str]] = {kind: set() for kind in _PAYLOAD_REFERENCE_KEYS}

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            for kind, keys in _PAYLOAD_REFERENCE_KEYS.items():
                if key in keys:
                    values = item if isinstance(item, list) else [item]
                    refs[kind].update(str(v).strip() for v in values if isinstance(v, str) and v.strip())
            visit(item)

    visit(payload or {})
    return refs


def _key_variants(*names: str) -> frozenset[str]:
    return frozenset(
        key
        for name in names
        for key in (name, "".join(part.capitalize() if i else part for i, part in enumerate(name.split("_"))))
    )


# Tool argument keys naming a resource. A Skill may be named by id or slug
# (``skill`` is invoke_skill's legacy alias of ``skill_id``); it is resolved
# as invoke_skill resolves it before the check. ``agent`` (delegate_to_agent)
# names a service the handler resolves among the run Workspace's
# subscriptions. tests/test_collaborator_runtime_boundary.py checks every
# admitted tool's schema against this set.
_RUNTIME_REFERENCE_KEYS = {
    kind: _key_variants(*names)
    for kind, names in {
        "workspace": ("workspace_id", "workspace_ids", "target_workspace_id"),
        "agent": ("agent_id", "agent_ids", "recommended_agent_id"),
        "skill": ("skill", "skill_id", "skill_ids"),
        "workflow": ("workflow_id", "workflow_ids"),
        "workflow_binding": ("binding_id",),
        "agent_subscription": ("agent_subscription_id", "subscription_id"),
        "task": (
            "task_id", "task_ids", "parent_task_id",
            "depends_on_task_ids", "dependency_task_ids", "predecessor_task_ids",
        ),
        "conversation": ("conversation_id",),
        "goal": ("goal_id", "goal_ids", "linked_goal_ids"),
        "stat": ("stat_id",),
        "scheduled_job": ("job_id", "scheduled_job_id"),
        "document": ("document_id", "document_ids", "source_document_id"),
        "media_job": ("job_ids", "media_job_id", "media_job_ids"),
    }.items()
}
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)


def runtime_argument_references(arguments) -> dict[str, set[str]]:
    """Every id a tool call names, anywhere in its arguments.

    Keys starting with ``_`` are runtime-injected context, not model input.
    """
    refs: dict[str, set[str]] = {kind: set() for kind in _RUNTIME_REFERENCE_KEYS}

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if not isinstance(key, str) or key.startswith("_"):
                continue
            for kind, keys in _RUNTIME_REFERENCE_KEYS.items():
                if key in keys:
                    values = item if isinstance(item, list) else [item]
                    refs[kind].update(str(v).strip() for v in values if isinstance(v, str) and v.strip())
            visit(item)

    visit(arguments or {})
    # A goal may be named by its key, which the handler resolves in the run's Workspace.
    refs["goal"] = {value for value in refs["goal"] if _ULID.match(value)}
    return refs


async def runtime_references_outside(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    scope: frozenset[str],
    user_id: str | None,
    arguments,
) -> list[tuple[str, str]]:
    """The ids a collaborator's tool call names outside its run Workspace.

    A named Workspace must be live-granted; a Document must be linked to the
    run Workspace or be the collaborator's own; a media job must be the run
    user's own; every other id must belong to the run Workspace.
    """
    return await _named_references_outside(
        db, entity_id=entity_id, workspace_id=workspace_id, scope=scope, user_id=user_id,
        refs=runtime_argument_references(arguments),
    )


async def _resolve_skill_references(db, values: set[str], *, entity_id: str) -> set[str]:
    """Skill ids for values naming a Skill by id or slug, as invoke_skill resolves them.

    A value naming no Skill stays as given and fails the check.
    """
    from packages.core.models.skill import Skill
    from packages.core.services.skill_service import get_skill_by_slug

    ids = set((await db.scalars(select(Skill.id).where(Skill.id.in_(sorted(values))))).all())
    resolved = set(ids)
    for value in sorted(values - ids):
        row = await get_skill_by_slug(db, value, entity_id)
        resolved.add(str(row.id) if row is not None else value)
    return resolved


async def _named_references_outside(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    scope: frozenset[str],
    user_id: str | None,
    refs: dict[str, set[str]],
) -> list[tuple[str, str]]:
    refs = {kind: set(values) for kind, values in refs.items()}
    outside = [("workspace", value) for value in sorted(refs.pop("workspace", set())) if value not in scope]
    net_ids = sorted(refs.pop("knowledge_net", set()))
    if net_ids:
        from packages.core.models.document import DocumentGroup

        nets = dict((await db.execute(
            select(DocumentGroup.id, DocumentGroup.workspace_id).where(
                DocumentGroup.id.in_(net_ids), DocumentGroup.entity_id == entity_id,
            )
        )).all())
        outside.extend(("knowledge_net", value) for value in net_ids if nets.get(value) != workspace_id)
    media_job_ids = sorted(refs.pop("media_job", set()))
    if media_job_ids:
        from packages.core.models.media_job import MediaJob

        own = set((await db.scalars(
            select(MediaJob.id).where(
                MediaJob.id.in_(media_job_ids), MediaJob.entity_id == entity_id, MediaJob.user_id == user_id,
            )
        )).all()) if user_id else set()
        outside.extend(("media_job", value) for value in media_job_ids if value not in own)
    document_ids = sorted(refs.pop("document", set()))
    if document_ids:
        from packages.core.models.document import Document
        from packages.core.services.document_access import document_workspace_ids_batched

        documents = (
            await db.scalars(select(Document).where(Document.id.in_(document_ids), Document.entity_id == entity_id))
        ).all()
        linked = await document_workspace_ids_batched(db, list(documents))
        admitted = {
            str(document.id)
            for document in documents
            if (user_id and document.owner_id == user_id) or workspace_id in linked.get(str(document.id), set())
        }
        outside.extend(("document", value) for value in document_ids if value not in admitted)
    if refs.get("skill"):
        refs["skill"] = await _resolve_skill_references(db, refs["skill"], entity_id=entity_id)
    outside.extend(await references_outside(db, entity_id=entity_id, workspace_id=workspace_id, refs=refs))
    return outside


# Keys of a Workspace operation state (agent mappings, services, immediate
# Tasks, skill bindings, Workflow attachments, automations) naming an Agent,
# Skill or Workflow by id; any spelling, any depth. ``skill_bindings`` (a
# custom Agent draft's Skill ids or slugs) and ``agent_slug`` (which reuses the
# company Agent with that slug) name resources too and are resolved first.
_OPERATION_REFERENCE_KEYS = {
    kind: _key_variants(*names)
    for kind, names in {
        "agent": ("agent_id", "agent_ids", "recommended_agent_id"),
        "skill": ("skill_id", "skill_ids"),
        "workflow": ("workflow_id", "workflow_ids"),
    }.items()
}
_OPERATION_NAMED_KEYS = {key: kind for kind, names in {
    "skill": ("skill_bindings",), "agent": ("agent_slug",),
}.items() for key in _key_variants(*names)}


def _operation_reference_paths(state) -> list[tuple[str, str, str, bool]]:
    """``(path, kind, value, by_name)`` for every resource an operation state names."""
    found: list[tuple[str, str, str, bool]] = []

    def strings(value) -> list[tuple[str, str]]:
        if isinstance(value, str):
            return [("", value.strip())] if value.strip() else []
        if isinstance(value, list):
            return [(f"[{i}]", item.strip()) for i, item in enumerate(value) if isinstance(item, str) and item.strip()]
        return []

    def visit(value, path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            for kind, keys in _OPERATION_REFERENCE_KEYS.items():
                if key in keys:
                    found.extend((child + suffix, kind, text, False) for suffix, text in strings(item))
            if key in _OPERATION_NAMED_KEYS:
                found.extend(
                    (child + suffix, _OPERATION_NAMED_KEYS[key], text, True) for suffix, text in strings(item)
                )
            visit(item, child)

    visit(state or {}, "")
    return found


async def operation_references_outside(
    db, *, entity_id: str, workspace_id: str, state,
) -> list[tuple[str, str, str]]:
    """``(path, kind, value)`` of what an operation state names outside ``workspace_id``.

    An Agent belongs to the Workspace it lives in or is subscribed to; a Skill
    to its Workspace, its Agents' Workspaces, or the platform; a Workflow to
    its Workspace or the Workspaces binding it. A name resolves as the
    provisioning code resolves it (a Skill id or slug, an Agent slug) and is
    outside when any resource it names is.
    """
    from packages.core.models.skill import Skill
    from packages.core.models.workspace import Agent

    paths = _operation_reference_paths(state)
    resolved: dict[tuple[str, str], set[str]] = {}
    for _, kind, value, by_name in paths:
        if not by_name or (kind, value) in resolved:
            continue
        if kind == "skill":
            resolved[(kind, value)] = await _resolve_named(
                db, Skill, Skill.slug, {value}, entity_id=entity_id, platform=True,
            )
        else:
            resolved[(kind, value)] = set((await db.scalars(
                select(Agent.id).where(Agent.entity_id == entity_id, Agent.slug == value, Agent.deleted_at.is_(None))
            )).all())
    refs: dict[str, set[str]] = {}
    for _, kind, value, by_name in paths:
        refs.setdefault(kind, set()).update(resolved[(kind, value)] if by_name else {value})
    outside = set(await references_outside(db, entity_id=entity_id, workspace_id=workspace_id, refs=refs))
    return [
        (path, kind, value)
        for path, kind, value, by_name in paths
        if any((kind, ref) in outside for ref in (resolved[(kind, value)] if by_name else {value}))
    ]


# Workflow step types whose configuration names another resource, and the
# configuration keys that do. The runner renders exactly these keys before
# its reference check (workflow_runner._collaborator_step_refusal).
WORKFLOW_SUBWORKFLOW_STEP_TYPES = frozenset({"subworkflow", "foreach_subworkflow"})
WORKFLOW_AGENT_STEP_TYPES = frozenset({"agent", "llm", "classifier"})
WORKFLOW_TOOL_STEP_TYPES = frozenset({"tool", "connector"})
WORKFLOW_REFERENCE_KEYS = {
    **{step_type: ("workflow_id",) for step_type in WORKFLOW_SUBWORKFLOW_STEP_TYPES},
    **{step_type: ("agent_id", "skill", "reference_document_ids") for step_type in WORKFLOW_AGENT_STEP_TYPES},
    **{step_type: ("tool", "args") for step_type in WORKFLOW_TOOL_STEP_TYPES},
    "rag": ("workspace_id", "net_ids", "group_ids"),
}


def _literal(value) -> str:
    """A configured reference, or '' when it is empty or still a ``{{template}}``."""
    text = str(value).strip() if isinstance(value, str) else ""
    return "" if "{{" in text else text


def _literals(value) -> set[str]:
    values = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    return {text for item in values if (text := _literal(item))}


async def _resolve_named(db, model, name_column, values: set[str], *, entity_id: str, platform: bool = False) -> set[str]:
    """Ids of the rows a value names by id or by name, as the runner and handlers resolve them."""
    from sqlalchemy import or_

    if not values:
        return set()
    owner = or_(model.entity_id == entity_id, model.entity_id.is_(None)) if platform else model.entity_id == entity_id
    rows = await db.execute(
        select(model.id).where(owner, or_(model.id.in_(sorted(values)), name_column.in_(sorted(values))))
    )
    return {str(row_id) for row_id in rows.scalars()}


async def workflow_steps_outside(
    db,
    *,
    entity_id: str,
    workspace_id: str | None,
    steps,
    user_id: str | None = None,
) -> list[tuple[str, str]]:
    """What Workflow steps name outside ``workspace_id``.

    Subworkflow targets (an id or a name, as the runner resolves them),
    Agents, Skills (an id or a slug), reference Documents, a rag step's
    Workspace and Knowledge Nets, the ids a tool step's arguments name, and
    tool steps whose tool the collaborator runtime gate would not admit in the
    Workspace. A value still holding a ``{{template}}`` is checked when the
    step runs, rendered. Used for a Workflow a collaborator saves and for each
    step of a run a collaborator started, steered or authored.
    """
    from packages.core.models.skill import Skill
    from packages.core.models.workflow import WorkflowDefinition

    workspace_id = str(workspace_id or "")
    refs: dict[str, set[str]] = {kind: set() for kind in _RUNTIME_REFERENCE_KEYS}
    refs["knowledge_net"] = set()
    workflow_names: set[str] = set()
    skill_names: set[str] = set()
    tools: list[tuple[str, dict]] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_type = str(step.get("type") or "tool")
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if step_type in WORKFLOW_SUBWORKFLOW_STEP_TYPES and (target := _literal(config.get("workflow_id"))):
            workflow_names.add(target)
        elif step_type in WORKFLOW_AGENT_STEP_TYPES:
            if agent_id := _literal(config.get("agent_id")):
                refs["agent"].add(agent_id)
            if skill := _literal(config.get("skill")):
                skill_names.add(skill)
            refs["document"] |= _literals(config.get("reference_document_ids"))
        elif step_type in WORKFLOW_TOOL_STEP_TYPES:
            args = config.get("args") if isinstance(config.get("args"), dict) else {}
            for kind, values in runtime_argument_references(args).items():
                refs[kind] |= {value for value in values if "{{" not in value}
            if tool := _literal(config.get("tool")):
                tools.append((tool, args))
        elif step_type == "rag":
            if rag_workspace := _literal(config.get("workspace_id")):
                refs["workspace"].add(rag_workspace)
            refs["knowledge_net"] |= _literals(config.get("net_ids") or config.get("group_ids"))
    refs["workflow"] |= await _resolve_named(
        db, WorkflowDefinition, WorkflowDefinition.name, workflow_names, entity_id=entity_id,
    )
    refs["skill"] |= await _resolve_named(db, Skill, Skill.slug, skill_names, entity_id=entity_id, platform=True)
    outside = await _named_references_outside(
        db, entity_id=entity_id, workspace_id=workspace_id, scope=frozenset({workspace_id} - {""}),
        user_id=user_id, refs=refs,
    )
    from packages.core.services.runtime_authorization.collaborator import collaborator_tool_admitted

    for tool, args in tools:
        if not await collaborator_tool_admitted(db, entity_id=entity_id, workspace_id=workspace_id, tool_name=tool, arguments=args):
            outside.append(("tool", tool))
    return outside


async def require_collaborator_workflow_steps(
    db,
    *,
    user_id: str | None,
    entity_id: str,
    workspace_id: str | None,
    steps,
    user=None,
) -> None:
    """Refuse a Workflow a collaborator saves when its steps name anything outside the Workspace.

    No-op for anyone who is not a collaborator in ``entity_id``.
    """
    scope = await actor_workspace_scope(db, user_id=user_id, entity_id=entity_id, user=user)
    if scope is None:
        return
    if not workspace_id or workspace_id not in scope:
        raise CollaboratorScopeError()
    if await workflow_steps_outside(db, entity_id=entity_id, workspace_id=workspace_id, steps=steps, user_id=user_id):
        raise CollaboratorScopeError()


async def customer_bound_providers(db, *, entity_id: str, workspace_id: str) -> set[str]:
    """Integration providers a customer member bound to the Workspace.

    The runtime rule (runtime_authorization.collaborator.workspace_bound_mcp_tool):
    an enabled ``operating_model.capability_bindings`` entry without
    collaborator provenance, for a provider the Workspace has not disabled.
    """
    from packages.core.models.workspace import Workspace
    from packages.core.services.collaborator_steering import BINDING_PROVENANCE_KEY, binding_providers
    from packages.core.services.workspace_integration_scope import disabled_workspace_providers

    operating_model = await db.scalar(
        select(Workspace.operating_model).where(
            Workspace.id == workspace_id, Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None),
        )
    )
    if not isinstance(operating_model, dict):
        return set()
    providers: set[str] = set()
    for binding in operating_model.get("capability_bindings") or []:
        if isinstance(binding, dict) and binding.get("enabled") is not False and not binding.get(BINDING_PROVENANCE_KEY):
            providers |= binding_providers(binding)
    return providers - set(disabled_workspace_providers(operating_model))


async def collaborator_integration_usable(
    db, *, entity_id: str, workspace_id: str, integration, user_ids: Iterable[str],
) -> bool:
    """Whether collaborators may use a company Integration row in the Workspace.

    Their own connection, or one whose provider a customer member bound to
    the Workspace; never any other company connection.
    """
    from packages.core.services.provider_keys import canonical_provider_key

    if integration is None:
        return False
    if integration.owner_user_id and str(integration.owner_user_id) in {str(value) for value in user_ids}:
        return True
    return canonical_provider_key(integration.provider) in await customer_bound_providers(
        db, entity_id=entity_id, workspace_id=workspace_id,
    )


# Workspace.settings keys an external collaborator may change: how the
# Workspace's own runs retry and whether they learn. Every other key -- who can
# see the Workspace, its collaboration and delivery record, and notification
# routing (which picks the company's channel accounts, recipients and
# credentials) -- stays the customer's. An allowlist: a new settings key is
# closed to collaborators until it is reviewed here.
COLLABORATOR_WRITABLE_SETTINGS = frozenset({"execution_policy", "runtime_learning"})


class CollaboratorSettingsRefused(HTTPException):
    def __init__(self, keys: Iterable[str]) -> None:
        super().__init__(
            403,
            "External collaborators cannot change these Workspace settings: " + ", ".join(sorted(keys)),
        )


def collaborator_settings_refusal(current, requested) -> list[str]:
    """The settings keys a change from ``current`` to ``requested`` touches outside the allowlist."""
    before = current if isinstance(current, Mapping) else {}
    after = requested if isinstance(requested, Mapping) else {}
    return sorted(
        key for key in set(before) | set(after)
        if key not in COLLABORATOR_WRITABLE_SETTINGS and before.get(key) != after.get(key)
    )


def refuse_collaborator_settings_change(current, requested, *, entity_id: str | None) -> None:
    """Refuse a Workspace settings write while a collaborator acts (their request or run).

    No-op when no collaborator is acting in ``entity_id``.
    """
    from packages.core.services.collaborator_steering import collaborators_acting_now

    if not collaborators_acting_now(entity_id):
        return
    if refused := collaborator_settings_refusal(current, requested):
        raise CollaboratorSettingsRefused(refused)


async def require_collaborator_references(
    db,
    *,
    user_id: str | None,
    entity_id: str,
    workspace_id: str | None,
    refs: Mapping[str, Iterable[str | None]] | None = None,
    user=None,
) -> None:
    """Refuse a collaborator write outside a live-granted Workspace or naming outside resources.

    No-op for anyone who is not a collaborator in ``entity_id``.
    """
    scope = await actor_workspace_scope(db, user_id=user_id, entity_id=entity_id, user=user)
    if scope is None:
        return
    if not workspace_id or workspace_id not in scope:
        raise CollaboratorScopeError()
    if refs and await references_outside(db, entity_id=entity_id, workspace_id=workspace_id, refs=refs):
        raise CollaboratorScopeError()
