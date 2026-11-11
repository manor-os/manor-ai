"""Default-deny runtime boundary for external Workspace collaborators.

A collaborator (membership role ``collaborator`` in the customer company,
derived from a WorkspaceCollaboration grant) chats with and runs agents inside
the Workspaces their live grants cover. Those runs execute as the collaborator
in the customer company, so every tool call crosses
``DefaultAuthorizationService.authorize_runtime`` with the collaborator as the
acting human (``user_id``), as the execution user of a delegated/agent
principal, or as the requester of the Task a user-less background call runs
for. This module is that call's gate:

* the run must be in a Workspace ``collaborator_workspace_ids`` returns now --
  the live grant, never the derived seat; a collaborator membership that is
  no longer active has no Workspace at all;
* the call's subject (action key + capability id, as
  ``approval_classifier.classify_runtime_tool`` emits it) must be on
  ``COLLABORATOR_RUNTIME_SUBJECTS``; anything else -- CRM, people, company
  settings, integrations and MCP accounts, usage, entity Knowledge, Workspace
  creation, other Workspaces -- is denied;
* a few tools are denied by name whatever subject they classify to, because
  their handlers reach the company beyond the Workspace.

An allowed subject skips the company-permission step of the ordinary strategy:
the live grant makes the collaborator the Workspace's owner for the trial, so
READ/USE/CONTROL and WRITE/MANAGE are all granted inside it.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass

from packages.core.services.runtime_authorization.domain import (
    AuthorizationRule,
    AuthorizationSubject,
    PermissionDecision,
    RuntimeAuthorizationRequest,
)


@dataclass(frozen=True)
class CollaboratorSubjectGroup:
    reason: str
    subjects: frozenset[tuple[str, str]]

    def matches(self, subject: AuthorizationSubject) -> bool:
        return (subject.action_key, subject.capability_id) in self.subjects


def _pairs(*pairs: tuple[str, str | None]) -> frozenset[tuple[str, str]]:
    return frozenset((key, capability or "") for key, capability in pairs)


COLLABORATOR_RUNTIME_SUBJECTS: tuple[CollaboratorSubjectGroup, ...] = (
    CollaboratorSubjectGroup(
        "Run control: plan, finish, ask and discover within this run only; every "
        "nested call is authorized again.",
        subjects=_pairs(
            ("runtime.control.update_task_plan", None),
            ("runtime.control.complete_task", None),
            ("skill.invoke", "skill.invoke"),
            ("workspace.invoke_skill", "manor.composite"),
            ("human.input.request", "human.input"),
            ("tool.search_tools.read", "runtime.discovery"),
            ("runtime.control.call_tool", "runtime.discovery"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Public web and stateless helpers: no company data is read.",
        subjects=_pairs(
            ("tool.get_current_time.read", None),
            ("tool.web_search.read", "web.safe_search"),
            ("tool.web_fetch.read", "web.safe_search"),
            ("tool.browse_web.read", "web.safe_search"),
            ("tool.website_content.read", None),
            ("tool.web_event_search.read", None),
            ("tool.weather_search.read", None),
            ("tool.render_response_surface.read", "response.render"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace runtime reads: handlers read the trusted run Workspace; Knowledge results "
        "pass document_access, which admits a collaborator only to live-granted Workspace documents.",
        subjects=_pairs(
            ("tool.workspace_agent.read", "workspace.operate"),
            ("tool.workspace_search.read", "workspace.search"),
            ("tool.workspace_list_knowledge.read", "workspace.search"),
            ("tool.rag.read", "workspace.search"),
            ("tool.workspace_operation.read", "workspace.governance"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace work: Tasks, goals and metrics, Workspace Knowledge, delegation to this "
        "Workspace's subscribed agents, and its approval cards.",
        subjects=_pairs(
            ("workspace.task.create", "workspace.task"),
            ("workspace.task.update", "workspace.task"),
            ("workspace.knowledge.update", "workspace.knowledge"),
            ("workspace.service.delegate", "manor.composite"),
            ("workspace.hitl.resolve", "manor.composite"),
            ("workspace.goal.update", "manor.composite"),
            ("workspace.goal.create", "manor.composite"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace configuration including rules, governance and budget: the trial lets the "
        "collaborator manage the Workspace (operation drafts, rules, strategist review).",
        subjects=_pairs(
            ("workspace.rule.update", "workspace.governance"),
            ("workspace.strategist.run", "workspace.governance"),
            ("workspace.operation.draft", "workspace.governance"),
            ("workspace.operation.apply", "workspace.governance"),
            ("workspace.operation.discard", "workspace.governance"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace files: ai_file_permissions and document_access check every path, which admit "
        "a collaborator only inside live-granted Workspaces.",
        subjects=_pairs(
            ("workspace.file.create", "file.write"),
            ("workspace.file.modify", "file.write"),
            ("workspace.file.write", "file.write"),
            ("workspace.file.delete", "file.write"),
            ("tool.read_file.read", "file.read"),
            ("tool.list_files.read", "file.read"),
            ("tool.glob_files.read", "file.read"),
            ("tool.grep_files.read", "file.read"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace automations: scheduled Jobs in the trusted run Workspace; scheduler_service "
        "checks the resources a Job names, and its runs execute as the collaborator.",
        subjects=_pairs(
            ("workspace.automation.create", "automation.manage"),
            ("workspace.automation.update", "automation.manage"),
            ("workspace.automation.delete", "automation.manage"),
            ("workspace.automation.run", "automation.manage"),
            ("tool.list_scheduled_jobs.read", "automation.manage"),
            ("tool.query_scheduled_jobs.read", "automation.manage"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace flows: start, follow and stop the flows deployed to the Workspace.",
        subjects=_pairs(
            ("workspace.workflow.run", "workflow.run"),
            ("tool.list_workspace_flows.read", "workflow.run"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Sandbox: an isolated container without the company filesystem; results are saved "
        "through the Workspace file path checks.",
        subjects=_pairs(
            ("runtime.control.sandbox_create", "sandbox.execute"),
            ("sandbox.exec", "sandbox.execute"),
            ("sandbox.cancel", "sandbox.execute"),
            ("runtime.control.sandbox_destroy", "sandbox.execute"),
            ("sandbox.file.modify", "sandbox.execute"),
            ("tool.sandbox_status.read", "sandbox.execute"),
            ("tool.sandbox_read_file.read", "sandbox.execute"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Reply in the run's own channel conversation (a Workspace webchat the collaborator owns).",
        subjects=_pairs(("channel.reply", None)),
    ),
    CollaboratorSubjectGroup(
        "Workspace Task reads and edits: a named Task must be the run Workspace's (reference "
        "check), and search_tasks lists only the run Workspace's Tasks for a collaborator.",
        subjects=_pairs(
            ("tool.get_task_details.read", None),
            ("tool.search_tasks.read", None),
            ("workspace.update_task", "manor.composite"),
            ("workspace.add_task_comment", "manor.composite"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Knowledge listings: list_visible_documents and content search filter through "
        "document_access; a named Workspace must be live-granted.",
        subjects=_pairs(
            ("tool.list_documents.read", None),
            ("tool.search_documents.read", None),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workspace Ledgers: every reader is keyed on the trusted run Workspace.",
        subjects=_pairs(
            ("tool.read_content_ledger.read", None),
            ("tool.read_finance_ledger.read", None),
            ("tool.read_recruiting_ledger.read", None),
            ("tool.read_relationship_ledger.read", None),
            ("tool.query_ledger.read", None),
            ("tool.visualize_workspace_ledgers.read", None),
        ),
    ),
    CollaboratorSubjectGroup(
        "Workflow authoring for Workspace-owned Workflows: the named Workflow must be the run "
        "Workspace's (writes: owned by it and published nowhere else); a created Workflow is "
        "owned by the run Workspace.",
        subjects=_pairs(
            ("tool.get_workflow.read", "workflow.manage"),
            ("tool.validate_workflow.read", "workflow.manage"),
            ("workspace.workflow.create", "workflow.manage"),
            ("workspace.workflow.modify", "workflow.manage"),
            ("workspace.workflow.delete", "workflow.manage"),
            ("workspace.workflow.run", "workflow.manage"),
        ),
    ),
    CollaboratorSubjectGroup(
        "Media helpers: source files pass the media file ACL (document_access), and waited "
        "media jobs must be the run user's own.",
        subjects=_pairs(
            ("tool.probe_media.read", None),
            ("tool.transcribe_video.read", None),
            ("tool.analyze_audio.read", None),
            ("tool.wait_media_jobs.read", None),
        ),
    ),
)

# Denied by name whatever they classify to. Each reaches the company beyond the
# Workspace: a shell in the company filesystem (bash, code, code_project), the
# Workspace architect that drafts new Workspaces (ws_*), a company-level Task
# outside any Workspace (create_task; Workspace Tasks have their own tools),
# Workflows run, deployed or imported outside the Workspace's own flows
# (run_workflow, deploy_workflow, import_workflow; start_workspace_flow runs
# the Workspace's flows), and capability acquisition.
COLLABORATOR_DENIED_TOOLS = frozenset({
    "bash", "code", "code_project", "acquire_capabilities", "create_task",
    "run_workflow", "deploy_workflow", "import_workflow",
})
COLLABORATOR_DENIED_TOOL_PREFIXES = ("ws_",)
# Connected-account and MCP tools: never by subject; only when bound to the
# run Workspace (``workspace_bound_mcp_tool``).
MCP_TOOL_PREFIX = "mcp__"


def collaborator_tool_denied(tool_name: str | None) -> bool:
    name = str(tool_name or "").strip()
    return name in COLLABORATOR_DENIED_TOOLS or name.startswith(COLLABORATOR_DENIED_TOOL_PREFIXES)


def collaborator_subject_allowed(subject: AuthorizationSubject, *, tool_name: str | None = None) -> bool:
    """The static allowlist. MCP tools are never admitted here (see the gate)."""
    if collaborator_tool_denied(tool_name) or str(tool_name or "").startswith(MCP_TOOL_PREFIX):
        return False
    return any(group.matches(subject) for group in COLLABORATOR_RUNTIME_SUBJECTS)


async def workspace_bound_mcp_tool(db, *, entity_id: str, workspace_id: str, tool_name: str) -> bool:
    """Whether an MCP / connected-account tool is bound to the Workspace.

    Bound means the Workspace's own configuration admits it: an enabled entry
    of ``operating_model.capability_bindings`` expands to this exact tool (an
    MCP server or integration binding, or the tool itself), and the provider is
    not in the Workspace's ``integration_scope.disabled_providers``. Agent-level
    MCP bindings, entity-wide MCP servers and the master agent's unrestricted
    MCP scope are company-level and do not count.

    An entry a customer member added counts as is. An entry a collaborator
    added (``added_by_collaborator``, recorded by collaborator_steering) counts
    only while that collaborator still holds their own connection for the
    provider: a collaborator's binding never unlocks the company's accounts.
    """
    from sqlalchemy import select

    from packages.core.ai.runtime.capability_bindings import expand_runtime_capability_binding
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_integration_scope import (
        disabled_workspace_providers,
        integration_provider_for_tool,
    )

    name = str(tool_name or "").strip()
    if not name.startswith(MCP_TOOL_PREFIX):
        return False
    operating_model = await db.scalar(
        select(Workspace.operating_model).where(
            Workspace.id == workspace_id, Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None)
        )
    )
    if not isinstance(operating_model, dict):
        return False
    if integration_provider_for_tool(name) in disabled_workspace_providers(operating_model):
        return False
    from packages.core.services.collaborator_steering import (
        BINDING_PROVENANCE_KEY,
        owned_providers,
        ownership_statements,
    )

    for binding in operating_model.get("capability_bindings") or []:
        if not isinstance(binding, dict) or binding.get("enabled") is False:
            continue
        try:
            if name not in expand_runtime_capability_binding(binding).mcp_tool_names:
                continue
        except Exception:
            continue
        added_by = str(binding.get(BINDING_PROVENANCE_KEY) or "")
        if not added_by:
            return True
        rows = [row for statement in ownership_statements(entity_id, [added_by]) for row in (await db.execute(statement)).all()]
        if integration_provider_for_tool(name) in owned_providers(rows).get(added_by, set()):
            return True
    return False


# ``(is_collaborator, monotonic time)`` per (user, company): the membership
# role, cached briefly per context so a run does not re-read it on every tool
# call. Live grants are never cached; they are re-read on every call.
_ROLE_CACHE: ContextVar[dict | None] = ContextVar("collaborator_role_cache", default=None)
_ROLE_CACHE_SECONDS = 30.0


def _role_cache() -> dict:
    cache = _ROLE_CACHE.get()
    if cache is None:
        cache = {}
        _ROLE_CACHE.set(cache)
    return cache


async def collaborator_user_ids(db, *, entity_id: str, user_ids) -> frozenset[str]:
    """Which of ``user_ids`` hold a collaborator membership in ``entity_id`` (any status)."""
    from sqlalchemy import select

    from packages.core.models.user import UserMembership
    from packages.core.services.external_collaboration_access import COLLABORATOR_MEMBERSHIP_ROLE

    ids = {str(value) for value in user_ids if value}
    cache = _role_cache()
    now = time.monotonic()
    found = {
        user_id for user_id in ids
        if (hit := cache.get((user_id, entity_id))) and hit[0] and now - hit[1] < _ROLE_CACHE_SECONDS
    }
    missing = sorted(
        user_id for user_id in ids
        if not ((hit := cache.get((user_id, entity_id))) and now - hit[1] < _ROLE_CACHE_SECONDS)
    )
    if missing:
        rows = set((await db.execute(
            select(UserMembership.user_id).where(
                UserMembership.entity_id == entity_id,
                UserMembership.user_id.in_(missing),
                UserMembership.role == COLLABORATOR_MEMBERSHIP_ROLE,
            )
        )).scalars())
        for user_id in missing:
            cache[(user_id, entity_id)] = (user_id in rows, now)
        found |= rows
    return frozenset(found)


async def is_runtime_collaborator(user_id: str | None, entity_id: str | None, db=None) -> bool:
    """For tool handlers: whether the runtime gate (or the HTTP boundary) marked this user as acting.

    Read from the call's context only, never the database: every tool call
    crosses the gate first, which marks the collaborators behind it.
    """
    from packages.core.services.collaborator_steering import collaborators_acting_now

    del db
    return bool(user_id) and str(user_id) in collaborators_acting_now(entity_id)


async def runtime_collaborator_scope(
    db,
    *,
    entity_id: str,
    user_ids,
) -> frozenset[str] | None:
    """Live-granted Workspaces when any acting human is a collaborator, else None.

    Every collaborator among them narrows the scope; one whose collaboration
    has ended (or whose grants lapsed) narrows it to nothing.
    """
    from packages.core.services.collaborator_scope import collaborator_live_workspaces

    scope: frozenset[str] | None = None
    for user_id in sorted(await collaborator_user_ids(db, entity_id=entity_id, user_ids=user_ids)):
        workspaces = await collaborator_live_workspaces(db, user_id=user_id, entity_id=entity_id) or frozenset()
        scope = workspaces if scope is None else scope & workspaces
    return scope


async def collaboration_access_granted(db, *, entity_id: str, workspace_id: str | None) -> bool:
    """Whether a collaboration access grant (any status) ever covered the Workspace.

    Without a Workspace, any in the company. One indexed existence check; no
    collaborator can be behind work where this is False.
    """
    from sqlalchemy import exists, select

    from packages.core.models.workspace_collaboration import WorkspaceCollaboration
    from packages.core.services.external_collaboration_access import WORKSPACE_ACCESS_ROLES

    conditions = [
        WorkspaceCollaboration.entity_id == entity_id,
        WorkspaceCollaboration.role.in_(sorted(WORKSPACE_ACCESS_ROLES)),
    ]
    if workspace_id:
        conditions.append(WorkspaceCollaboration.workspace_id == workspace_id)
    return bool(await db.scalar(select(exists().where(*conditions))))


async def workflow_run_collaborators(db, run) -> frozenset[str]:
    """The collaborators behind a Workflow run.

    Who started it, who authored its Workflow (``created_by``; a later edit
    by a collaborator is refused at save when it names outside resources),
    and who steered the Task it runs for.
    """
    from sqlalchemy import select

    from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context
    from packages.core.models.task import Task
    from packages.core.models.workflow import WorkflowDefinition

    humans = {run.started_by}
    if run.workflow_id:
        humans.add(await db.scalar(
            select(WorkflowDefinition.created_by).where(
                WorkflowDefinition.id == run.workflow_id, WorkflowDefinition.entity_id == run.entity_id,
            )
        ))
    task_id = runtime_workflow_run_context(run).get("task_id")
    if task_id:
        humans.update(await db.scalar(
            select(Task.collaborator_ids).where(Task.id == task_id, Task.entity_id == run.entity_id)
        ) or ())
    return await collaborator_user_ids(db, entity_id=run.entity_id, user_ids=humans)


async def task_steering_ended(db, *, entity_id: str, workspace_id: str | None, collaborator_ids) -> bool:
    """Whether a collaborator who steered a Task has no live grant on its Workspace.

    A steering user who is no longer a collaborator (now a customer member)
    no longer narrows the Task.
    """
    from packages.core.services.collaborator_scope import collaborator_live_workspaces

    for user_id in sorted(await collaborator_user_ids(db, entity_id=entity_id, user_ids=collaborator_ids or ())):
        scope = await collaborator_live_workspaces(db, user_id=user_id, entity_id=entity_id) or frozenset()
        if not workspace_id or workspace_id not in scope:
            return True
    return False


async def _task_humans(db, request: RuntimeAuthorizationRequest, *, user_less: bool) -> tuple[set[str], set[str]]:
    """``(steering collaborators, requester)``: the requester only for a user-less call."""
    if not request.task_id:
        return set(), set()
    from sqlalchemy import select

    from packages.core.models.task import Task

    task = (
        await db.execute(
            select(
                Task.id, Task.entity_id, Task.workspace_id, Task.creator_id, Task.details,
                Task.collaborator_ids,
            ).where(Task.id == request.task_id, Task.entity_id == request.entity_id)
        )
    ).one_or_none()
    if task is None:
        return set(), set()
    steering = {str(value) for value in task.collaborator_ids or () if value}
    humans: set[str] = set()
    if user_less:
        # The requester the Task runner executes as; legacy rows naming an
        # Agent as creator resolve through their creation receipts.
        from packages.core.services.task_requester_identity import resolve_task_requesters

        resolution = (await resolve_task_requesters(db, [task])).get(str(task.id))
        if resolution is not None and resolution.user_id:
            humans.add(resolution.user_id)
    return steering, humans


def _plan_step_mcp_tool(subject: AuthorizationSubject) -> str | None:
    """A dispatcher action step's provider action as its MCP tool name."""
    provider, _, action = subject.action_key.partition(".")
    if not provider or not action or "." in action:
        return None
    return f"{MCP_TOOL_PREFIX}{provider}__{action}"


async def _subject_admitted(db, request: RuntimeAuthorizationRequest) -> bool:
    tool_name = str(request.tool_name or "")
    if tool_name.startswith(MCP_TOOL_PREFIX):
        return await workspace_bound_mcp_tool(
            db, entity_id=request.entity_id, workspace_id=str(request.workspace_id), tool_name=tool_name,
        )
    if collaborator_subject_allowed(request.subject, tool_name=request.tool_name):
        return True
    from packages.core.services.runtime_authorization.domain import AuthorizationPrincipalKind

    if request.principal_kind is AuthorizationPrincipalKind.SYSTEM_WORKER and request.task_id and not tool_name:
        # A dispatcher plan step of a steered Task. A step without its own
        # effect (llm, subagent, code) is admitted: its worker's tool calls
        # each cross this gate. A provider action step is admitted only when
        # its provider action is bound to the Workspace.
        if not request.subject.action_key:
            return True
        mcp_tool = _plan_step_mcp_tool(request.subject)
        return bool(mcp_tool) and await workspace_bound_mcp_tool(
            db, entity_id=request.entity_id, workspace_id=str(request.workspace_id), tool_name=mcp_tool,
        )
    return False


async def _run_human_collaborators(
    db, request: RuntimeAuthorizationRequest, run_humans: set[str],
) -> tuple[set[str], dict[str, str | None]]:
    """``(collaborators, resolved roles)`` among the run identity.

    When the strategy will resolve ``user_id``'s role anyway, the gate does it
    with the same role resolution and the service hands the result over, so an
    ordinary member costs no extra query. Collaborator status is keyed on the
    raw membership role that resolution reads in the same queries -- never on
    the resolved (possibly StaffRole) name, so a collaborator membership with a
    Staff row is still one. Run identities no strategy resolves (and a
    resolution that reported no membership role) are looked up by membership.
    """
    from packages.core.services import resource_access
    from packages.core.services.collaborator_steering import collaborators_acting_now
    from packages.core.services.external_collaboration_access import COLLABORATOR_MEMBERSHIP_ROLE
    from packages.core.services.runtime_authorization.strategies import runtime_strategy_resolves_user_role

    # The HTTP boundary already resolved a collaborator request's actor.
    collaborators = set(run_humans & collaborators_acting_now(request.entity_id))
    roles: dict[str, str | None] = {}
    unresolved = set(run_humans) - collaborators
    if runtime_strategy_resolves_user_role(request) and str(request.user_id) in unresolved:
        user_id = str(request.user_id)
        membership: dict = {}
        role = await resource_access.resolve_user_role(
            db, user_id=user_id, entity_id=request.entity_id, membership_role=membership,
        )
        roles[user_id] = role
        if "role" in membership:
            unresolved.discard(user_id)
            if membership["role"] == COLLABORATOR_MEMBERSHIP_ROLE:
                collaborators.add(user_id)
        elif role is not None and role != COLLABORATOR_MEMBERSHIP_ROLE:
            # A resolver that reports no membership role (a stand-in) and an
            # active non-collaborator role.
            unresolved.discard(user_id)
        elif role == COLLABORATOR_MEMBERSHIP_ROLE:
            unresolved.discard(user_id)
            collaborators.add(user_id)
    if unresolved:
        collaborators |= await collaborator_user_ids(db, entity_id=request.entity_id, user_ids=unresolved)
    return collaborators, roles


async def collaborator_runtime_gate(
    db,
    request: RuntimeAuthorizationRequest,
    roles: dict[str, str | None] | None = None,
) -> tuple[frozenset[str] | None, PermissionDecision | None]:
    """``(scope, denial)``: scope is None when no collaborator is involved.

    The acting humans are the run identity (the request's user and its
    principal's execution user, else -- for a user-less background call -- the
    Task's requester) plus every collaborator who steered the Task the call
    runs for. A Task a collaborator touched stays inside their Workspace
    whoever runs it; a customer-only Task is not narrowed. ``roles`` receives
    the run identity's resolved roles for the strategies to reuse.
    """
    from packages.core.services.collaborator_steering import collaborators_acting

    run_humans = {str(value) for value in (request.user_id, request.principal_execution_user_id) if value}
    collaborators, resolved = await _run_human_collaborators(db, request, run_humans)
    if roles is not None:
        roles.update(resolved)
    steering, requester = await _task_humans(db, request, user_less=not run_humans)
    if (steering | requester) - collaborators:
        collaborators |= await collaborator_user_ids(
            db, entity_id=request.entity_id, user_ids=(steering | requester) - collaborators,
        )
    if not collaborators:
        return None, None
    scope = await runtime_collaborator_scope(db, entity_id=request.entity_id, user_ids=collaborators)
    if scope is None:
        return None, None
    if steering & collaborators and await task_steering_ended(
        db, entity_id=request.entity_id, workspace_id=request.workspace_id, collaborator_ids=steering,
    ):
        from packages.core.services.task_requester_identity import TASK_STEERING_ENDED_MESSAGE

        return scope, PermissionDecision.deny(TASK_STEERING_ENDED_MESSAGE, AuthorizationRule.COLLABORATOR_STEERING_ENDED)
    # Whatever this call writes is steered by these collaborators.
    collaborators_acting(request.entity_id, collaborators)
    if request.principal_execution_user_id and request.principal_execution_user_id != request.user_id:
        # Handlers filter by the call's user_id (Knowledge, files); a run whose
        # user is missing or someone else would read with another identity.
        return scope, PermissionDecision.deny(
            "Runtime actor does not match the execution user.",
            AuthorizationRule.RUNTIME_IDENTITY_MISMATCH,
        )
    if not request.workspace_id or request.workspace_id not in scope:
        return scope, PermissionDecision.deny(
            "External collaborators work only inside the Workspace their collaboration covers.",
            AuthorizationRule.COLLABORATOR_WORKSPACE,
        )
    if not await _subject_admitted(db, request):
        return scope, PermissionDecision.deny(
            "This action is not available to external collaborators.",
            AuthorizationRule.COLLABORATOR_SUBJECT,
        )
    return scope, None


# Workflow tools whose ``workflow`` argument (an id or a name) must be the run
# Workspace's: reads may name one published to it, writes only one it owns.
_WORKFLOW_READ_TOOLS = frozenset({"get_workflow", "validate_workflow"})
_WORKFLOW_WRITE_TOOLS = frozenset({"ai_edit_workflow", "update_workflow", "delete_workflow", "test_workflow", "test_workflow_node"})


# Workflow-run tools whose ``run_id`` must be a run in the run Workspace.
_WORKFLOW_RUN_TOOLS = frozenset({"get_workflow_run", "cancel_workflow_run", "resume_workflow_run", "retry_workflow_run"})


async def _workflow_outside(db, *, entity_id: str, workspace_id: str, tool_name: str, arguments) -> bool:
    if tool_name in _WORKFLOW_RUN_TOOLS:
        from sqlalchemy import select

        from packages.core.models.workflow import WorkflowRun

        run_id = str((arguments or {}).get("run_id") or "").strip()
        run_workspace_id = await db.scalar(
            select(WorkflowRun.workspace_id).where(WorkflowRun.id == run_id, WorkflowRun.entity_id == entity_id)
        ) if run_id else None
        return str(run_workspace_id or "") != workspace_id
    if tool_name not in _WORKFLOW_READ_TOOLS | _WORKFLOW_WRITE_TOOLS:
        return False
    target = arguments.get("workflow") if isinstance(arguments, dict) else None
    if not str(target or "").strip():
        return tool_name in _WORKFLOW_WRITE_TOOLS
    from packages.core.ai.tools.workflow_tools import _resolve_workflow
    from packages.core.services.collaborator_scope import workflow_workspace_ids

    workflow = await _resolve_workflow(db, entity_id, target)
    if workflow is None:
        return False  # the handler reports it missing
    workspaces = await workflow_workspace_ids(db, workflow_id=str(workflow.id), entity_id=entity_id)
    if tool_name in _WORKFLOW_READ_TOOLS:
        return workspace_id not in workspaces
    return str(workflow.workspace_id or "") != workspace_id or not workspaces <= {workspace_id}


async def collaborator_call_refusal(
    db,
    *,
    decision: PermissionDecision,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    tool_name: str,
    arguments,
) -> str | None:
    """Why a collaborator's concrete call is refused, or None to continue.

    The authorization request carries only the call's subject; this sees its
    arguments. A gateway (``call_tool``, the Manor Workspace action) is judged
    as the tool it calls, and every id the arguments name must stay inside
    the run's Workspace. ``decision`` is the call's allowed runtime decision.
    """
    scope = decision.collaborator_workspace_ids
    if scope is None:
        return None
    if not workspace_id or workspace_id not in scope:
        return "External collaborators work only inside the Workspace their collaboration covers."
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
    from packages.core.services.collaborator_scope import runtime_references_outside

    canonical = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
    if collaborator_tool_denied(tool_name) or collaborator_tool_denied(canonical.tool_name):
        return "This tool is not available to external collaborators."
    if await runtime_references_outside(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope=scope,
        user_id=user_id,
        arguments=arguments,
    ) or await _workflow_outside(
        db, entity_id=entity_id, workspace_id=workspace_id, tool_name=tool_name, arguments=arguments,
    ):
        return "The call names a resource outside this Workspace."
    return None


async def collaborator_tool_admitted(db, *, entity_id: str, workspace_id: str | None, tool_name: str, arguments) -> bool:
    """Whether the gate would admit this concrete call for a collaborator in ``workspace_id``.

    The subject allowlist (or, for an MCP tool, its binding to the Workspace),
    the tools denied by name, and the Workflow a workflow tool names. Ids in
    the arguments are checked by the caller (collaborator_scope).
    """
    name = str(tool_name or "").strip()
    if not workspace_id or not name or collaborator_tool_denied(name):
        return False
    arguments = arguments if isinstance(arguments, dict) else {}
    if name.startswith(MCP_TOOL_PREFIX):
        return await workspace_bound_mcp_tool(db, entity_id=entity_id, workspace_id=workspace_id, tool_name=name)
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory

    try:
        authorization = classify_runtime_tool(name, arguments).authorization
        canonical = RuntimeCompositeToolCallFactory.create(name, arguments).tool_name
    except Exception:
        return False
    if authorization is None or collaborator_tool_denied(canonical):
        return False
    if not collaborator_subject_allowed(
        AuthorizationSubject.create(authorization.action_key, authorization.capability_id), tool_name=name,
    ):
        return False
    return not await _workflow_outside(
        db, entity_id=entity_id, workspace_id=workspace_id, tool_name=name, arguments=arguments,
    )


# Gateways whose subject depends on the concrete action: discovery offers them
# and each call is judged on its own.
_PER_CALL_TOOLS = frozenset({"manor", "workspace_agent", "workspace_operation", "sandbox", "call_tool"})


def collaborator_tool_offered(tool_name: str) -> bool:
    """Discovery's approximation: the tool's default call passes the static gate.

    MCP tools are offered separately, when bound to the run Workspace.
    """
    name = str(tool_name or "").strip()
    if not name or collaborator_tool_denied(name):
        return False
    if name in _PER_CALL_TOOLS:
        return True
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool

    try:
        authorization = classify_runtime_tool(name, {}).authorization
    except Exception:
        return False
    if authorization is None:
        return False
    return collaborator_subject_allowed(
        AuthorizationSubject.create(authorization.action_key, authorization.capability_id),
        tool_name=name,
    )
