"""Default-deny HTTP boundary for external Workspace collaborators.

A provider co-working in a customer company holds a derived ``collaborator``
membership there (packages/core/services/external_collaboration_access.py).
Many routers admit any active member of the entity and check nothing else
(clients, memories, activity, reports, usage, people, chat rooms, scheduler,
...), so a collaborator token must not reach routes by default.

``deps.get_current_user`` calls ``enforce_collaborator_route`` for every
collaborator actor, after ``collaborator_workspace_ids`` has resolved the
Workspaces their live grants cover, narrowed to the one Workspace their
scoped session pins (packages/core/services/collaborator_session.py). A
route is reachable only when it is:

* one of ``SELF_ROUTES`` -- the caller's own session, account and preferences;
* one of ``WORKSPACE_FILTERED_ROUTES`` -- a route without a Workspace path
  parameter that the Workspace page needs; the boundary resolves its target
  Workspace itself (query, body or the addressed row) and requires it to be
  live-granted, in addition to the handler's own Workspace check;
* one of ``WORKSPACE_ROUTES_REVIEWED`` under a live-granted Workspace.

Everything else is 403: a new entity route, and a new route under a
Workspace path, stay closed to collaborators until they are classified here.
Opening an HTTP entry does not decide what an agent run may reach once it
starts; that is the runtime's authorization.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from packages.core.models.document import Document
from packages.core.models.goal import Goal
from packages.core.models.runtime_run import RuntimeRun
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workflow import WorkflowBinding, WorkflowRun
from packages.core.models.workspace import AgentSubscription
from packages.core.services.collaborator_scope import (
    agent_workspace_ids,
    scheduled_job_workspace_ids,
    skills_invocable_in_workspace,
    workflow_workspace_ids,
)

# Every Workspace path parameter in the mounted routers is ``workspace_id``.
WORKSPACE_PATH_PARAMS = ("workspace_id",)

NOT_AVAILABLE = "This page is not available to external collaborators"

_WS = "/api/v1/workspaces/{workspace_id}"

# Routes under a live-granted Workspace that remain the customer's decision.
WORKSPACE_ADMIN_DENY = frozenset({
    # Deletion and recovery of the customer's Workspace.
    ("DELETE", _WS),
    ("POST", f"{_WS}/restore"),
    # Who is in the Workspace and with which role.
    ("POST", f"{_WS}/staff"),
    ("DELETE", f"{_WS}/staff/{{staff_id}}"),
    # Collaboration grants, the customer's collaboration group and acceptance.
    ("GET", f"{_WS}/collaboration"),
    ("POST", f"{_WS}/collaboration"),
    ("POST", f"{_WS}/collaboration/{{grant_id}}/{{action}}"),
    ("PUT", f"{_WS}/collaboration-group"),
    ("POST", f"{_WS}/delivery-phase"),
    # The provider team's own trials of a source Workspace: opened from home, never from a scoped token.
    ("GET", f"{_WS}/client-trials"),
    ("POST", f"{_WS}/client-trials/{{delivery_id}}/session"),
    # Exporting or publishing the customer's Workspace as a Blueprint.
    ("GET", f"{_WS}/blueprint-export/knowledge-documents"),
    ("PUT", f"{_WS}/blueprint-export/knowledge-documents/{{document_id}}/initial-template"),
    ("POST", f"{_WS}/export-blueprint-payload"),
    ("POST", f"{_WS}/export-blueprint-package"),
    ("POST", f"{_WS}/export-blueprint"),
})


def _routes(block: str) -> frozenset[tuple[str, str]]:
    rows = (line.split() for line in block.strip().splitlines())
    return frozenset((method, "/api/v1" + path.replace("~", "/workspaces/{workspace_id}")) for method, path in rows)


# Every Workspace-path route a collaborator may use inside a live-granted
# Workspace ("~" is /workspaces/{workspace_id}). During the trial the provider
# sees, runs and configures the Workspace, including approval rules, the
# governance policy, the budget and promotion to live. A route missing from
# both this list and WORKSPACE_ADMIN_DENY is refused until it is reviewed.
WORKSPACE_ROUTES_REVIEWED = _routes("""
    GET /approval-settings~
    PUT /approval-settings~
    POST /approval-settings~/preview
    GET ~
    PUT ~
    GET ~/activity
    GET ~/agents
    POST ~/agents
    GET ~/agents/assignable
    DELETE ~/agents/{service_key}
    GET ~/app-view
    GET ~/app-view/components/{component_key}/contexts
    GET ~/app-view/components/{component_key}/data
    GET ~/app-view/sections/{section_id}
    POST ~/app-view/design
    POST ~/apply-template
    GET ~/automation-health
    POST ~/blueprint-startup/reconcile
    POST ~/blueprint/revert
    GET ~/blueprint/upgrade
    POST ~/blueprint/upgrade
    POST ~/blueprint/upgrade/preview
    POST ~/blueprint/upgrade/v2
    GET ~/blueprint/upgrade/v3
    POST ~/blueprint/upgrade/v3
    POST ~/blueprint/upgrade/v3/preview
    GET ~/budget
    PUT ~/budget
    GET ~/capabilities
    GET ~/channels
    POST ~/channels
    GET ~/channels/available
    DELETE ~/channels/{channel_binding_id}
    PATCH ~/channels/{channel_binding_id}
    GET ~/channels/{channel_binding_id}/webchat/review
    GET ~/chat/attention
    POST ~/chat/attention/seen
    GET ~/chat/entrypoints
    POST ~/chat/entrypoints/{binding_id}/stream
    GET ~/chat/messages
    POST ~/chat/messages
    GET ~/chat/messages/page
    GET ~/chat/messages/{message_id}
    POST ~/chat/messages/{message_id}/feedback
    POST ~/chat/messages/{message_id}/resolve
    GET ~/chat/simulation-run
    POST ~/chat/simulation-run/restart
    POST ~/chat/simulation-run/start
    GET ~/chat/threads
    GET ~/connection-status
    GET ~/credit-usage-summary
    GET ~/dashboard
    GET ~/documents
    POST ~/documents/folders/{folder_id}
    POST ~/documents/groups
    DELETE ~/documents/groups/{group_id}
    PUT ~/documents/groups/{group_id}
    POST ~/documents/groups/{group_id}/members
    DELETE ~/documents/groups/{group_id}/members/{document_id}
    GET ~/evaluation
    PUT ~/goals
    GET ~/governance
    PUT ~/governance
    GET ~/governance/approval-matrix
    PUT ~/governance/approval-matrix
    GET ~/governance/revisions
    GET ~/governance/standing-grants
    DELETE ~/governance/standing-grants/{kind}/{value}
    POST ~/heartbeat/disable
    POST ~/heartbeat/enable
    GET ~/heartbeat/status
    GET ~/human-participation
    GET ~/human-queue
    POST ~/human-queue/commitments/{commitment_id}/respond
    GET ~/learning-candidates
    POST ~/learning-candidates/{candidate_id}/apply
    POST ~/learning-candidates/{candidate_id}/resolve
    PUT ~/ledgers/configuration
    GET ~/ledgers/overview
    GET ~/operating-model
    PUT ~/operating-model
    GET ~/operation/current
    POST ~/operation/drafts
    PATCH ~/operation/drafts/{draft_id}
    POST ~/operation/drafts/{draft_id}/apply
    GET ~/operation/drafts/{draft_id}/diff
    POST ~/operation/drafts/{draft_id}/discard
    POST ~/operation/drafts/{draft_id}/validate
    POST ~/operation/repair
    GET ~/participants/me
    PUT ~/participants/me
    POST ~/pause
    POST ~/promote
    GET ~/promote/preflight
    GET ~/resident-pms/accounts
    POST ~/resolve-integrations
    POST ~/resume
    POST ~/resume/v2
    PUT ~/rules
    GET ~/runtime/evidence
    POST ~/services
    DELETE ~/services/{service_key}
    POST ~/setup-input
    GET ~/setup-status
    POST ~/setup/finalize
    POST ~/setup/turn
    GET ~/simulation-report
    GET ~/staff
    GET ~/stats
    POST ~/stats
    POST ~/stats/assessed-metrics
    GET ~/stats/quick-view
    PUT ~/stats/quick-view
    DELETE ~/stats/{stat_id}
    PATCH ~/stats/{stat_id}
    POST ~/stats/{stat_id}/collect
    GET ~/stats/{stat_id}/observations
    POST ~/stats/{stat_id}/observations
    GET ~/strategy/reviews
    GET ~/strategy/reviews/{review_id}
    GET ~/tasks/{task_id}/provenance
    GET ~/timeline
    GET ~/webchat/resources
    POST ~/webchat/review
""")

# The caller's own account and preferences; nothing in the customer company.
# A scoped collaborator session is held by an embedded frame and re-minted by
# the provider's own app: it cannot renew itself, switch company or sign the
# person out (switch, renew and logout stay closed).
SELF_ROUTES = frozenset({
    # auth.py me: the caller's user row and their own memberships.
    ("GET", "/api/v1/auth/me"),
    # auth.py refresh_browser_session: cookie-based; never returns a collaborator company.
    ("POST", "/api/v1/auth/session/refresh"),
    # admin.py get_preferences / put_preferences: get/update_user_preferences(db, user.id).
    ("GET", "/api/v1/admin/preferences"),
    ("PUT", "/api/v1/admin/preferences"),
    # platform_public.py my_flags: feature-flag booleans evaluated for the caller.
    ("GET", "/api/v1/platform/flags"),
    # tasks.py get_task_constants: module constants (statuses, transitions, priorities, the
    # built-in category and type lists, notification channels); no company data, no auth.
    ("GET", "/api/v1/tasks/constants"),
    # tasks.py get/put_task_board_preferences: the caller's own user.preferences
    # (update_user_preferences(db, user.id)), like admin/preferences.
    ("GET", "/api/v1/tasks/board-preferences"),
    ("PUT", "/api/v1/tasks/board-preferences"),
})


@dataclass(frozen=True)
class CollaboratorScope:
    user_id: str
    entity_id: str
    workspace_ids: frozenset[str]


def collaborator_scope(user) -> frozenset[str] | None:
    """The live-granted Workspaces of a collaborator request, or None for anyone else."""
    return getattr(user, "_collaborator_workspace_ids", None)


async def _fields(request: Request) -> dict:
    """Form or JSON body fields; FastAPI has already read and cached the body."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        return dict(await request.form())
    if "json" in content_type:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    return {}


def _value(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


async def _query(request, db, scope):
    return {request.query_params.get("workspace_id") or request.query_params.get("workspaceId")}


async def _body(request, db, scope):
    return {_value((await _fields(request)).get("workspace_id"))}


async def _handler(request, db, scope):
    # The handler itself narrows its result to collaborator_scope(user).
    return set(scope.workspace_ids)


def _row(model, param):
    async def resolve(request, db, scope):
        return {await db.scalar(
            select(model.workspace_id).where(
                model.id == request.path_params.get(param), model.entity_id == scope.entity_id
            )
        )}
    return resolve


async def _job(request, db, scope):
    # Primary key first, then the stable job_id, as get_scheduled_job does.
    job_id = str(request.path_params.get("job_id") or "")
    return (await scheduled_job_workspace_ids(db, [job_id], entity_id=scope.entity_id)).get(job_id, set())


async def _agent(request, db, scope):
    return await agent_workspace_ids(db, agent_id=request.path_params.get("agent_id"), entity_id=scope.entity_id)


async def _agent_skill(request, db, scope):
    """Binding a Skill: an Agent of a granted Workspace and a Skill invocable there."""
    skill_id = request.path_params.get("skill_id")
    return {
        workspace_id
        for workspace_id in sorted(await _agent(request, db, scope) & scope.workspace_ids)
        if await skills_invocable_in_workspace(db, [skill_id], entity_id=scope.entity_id, workspace_id=workspace_id)
    }


async def _new_task(request, db, scope):
    """A new Task names its Workspace; any Agent, service or assignee it names belongs there."""
    from packages.core.constants.agents import is_master_agent

    fields = await _fields(request)
    workspace_id = _value(fields.get("workspace_id"))
    if workspace_id is None:
        return set()
    agent_id = _value(fields.get("agent_id"))
    if agent_id is not None and not is_master_agent(agent_id) and workspace_id not in await agent_workspace_ids(
        db, agent_id=agent_id, entity_id=scope.entity_id
    ):
        return set()
    subscription_id = _value(fields.get("owner_subscription_id"))
    if subscription_id is not None and await db.scalar(
        select(AgentSubscription.workspace_id).where(
            AgentSubscription.id == subscription_id, AgentSubscription.entity_id == scope.entity_id
        )
    ) != workspace_id:
        return set()
    # An assignee id would let the collaborator notify, and learn the name of,
    # anyone in the company; they may assign themselves or the Workspace's Agents.
    assignee_id = _value(fields.get("assignee_id"))
    if assignee_id is not None and assignee_id != scope.user_id and not is_master_agent(assignee_id) and (
        workspace_id not in await agent_workspace_ids(db, agent_id=assignee_id, entity_id=scope.entity_id)
    ):
        return set()
    # Company task categories are not part of the Workspace.
    if _value(fields.get("category_id")) is not None:
        return set()
    return {workspace_id}


async def _workflow(request, db, scope):
    return await workflow_workspace_ids(
        db, workflow_id=request.path_params.get("workflow_id"), entity_id=scope.entity_id
    )


async def _message(request, db, scope):
    return {await db.scalar(
        select(Conversation.workspace_id)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.id == request.path_params.get("message_id"), Conversation.entity_id == scope.entity_id)
    )}


async def _document(request, db, scope, document_id=None, fs_path=None):
    from packages.core.services.document_access import document_workspace_ids

    match = Document.id == document_id if fs_path is None else Document.fs_path == fs_path
    document = await db.scalar(
        select(Document).where(match, Document.entity_id == scope.entity_id, Document.is_trashed.is_(False)).limit(1)
    )
    # Every Workspace the Document is linked to: group membership, the
    # Workspace artifact folder, or a Workspace-visible record's origin.
    return await document_workspace_ids(db, document) if document is not None else set()


async def _document_path(request, db, scope):
    return await _document(request, db, scope, document_id=request.path_params.get("doc_id"))


async def _fs_read(request, db, scope):
    from packages.core.services.knowledge_visibility import normalize_rel_path

    path = request.query_params.get("path") or ""
    return await _document(request, db, scope, fs_path=normalize_rel_path(path.lstrip("/")))


def _chat_scope(source):
    """Workspace chat: a granted workspace_id, and any conversation or agent inside it."""
    async def resolve(request, db, scope):
        from packages.core.constants.agents import is_master_agent

        fields = await _fields(request) if source == "body" else request.query_params
        workspace_id = _value(fields.get("workspace_id"))
        if workspace_id is None:
            return set()
        conversation_id = _value(fields.get("conversation_id"))
        if conversation_id is not None and await db.scalar(
            select(Conversation.workspace_id).where(
                Conversation.id == conversation_id, Conversation.entity_id == scope.entity_id
            )
        ) != workspace_id:
            return set()
        agent_id = _value(fields.get("agent_id"))
        if agent_id is not None and not is_master_agent(agent_id) and workspace_id not in await agent_workspace_ids(
            db, agent_id=agent_id, entity_id=scope.entity_id
        ):
            return set()
        return {workspace_id}
    return resolve


def _with_body_agent(resolve):
    """A body that names an Agent must name one belonging to the target Workspace."""
    async def wrapped(request, db, scope):
        from packages.core.constants.agents import is_master_agent

        targets = await resolve(request, db, scope)
        agent_id = _value((await _fields(request)).get("agent_id"))
        if agent_id is None or is_master_agent(agent_id):
            return targets
        return targets & await agent_workspace_ids(db, agent_id=agent_id, entity_id=scope.entity_id)
    return wrapped


_TASK = _row(Task, "task_id")
_GOAL = _row(Goal, "goal_id")
_RUN = _row(WorkflowRun, "run_id")
_BINDING = _row(WorkflowBinding, "binding_id")
_CONVERSATION = _row(Conversation, "conversation_id")
_CHAT_RUN = _row(RuntimeRun, "run_id")

# The Workspace page's routes without a Workspace path parameter, each with how
# the boundary finds its target Workspace(s). The request passes only when a
# target is live-granted; rows without a Workspace never do. The comment on
# each group names the handler's own check, which still runs afterwards.
WORKSPACE_FILTERED_ROUTES = {
    # workspaces.py list_my_workspaces: filtered to collaborator_scope(user).
    ("GET", "/api/v1/workspaces"): _handler,
    # tasks.py list_my_tasks / task_board: require_workspace_readable(workspace_id) + the
    # readable_workspace_ids filter. A collaborator must name the Workspace.
    ("GET", "/api/v1/tasks"): _query,
    ("GET", "/api/v1/tasks/board"): _query,
    # tasks.py create_new_task: require_workspace_writable(workspace_id), then
    # ensure_workspace_agent_assignment / ensure_workspace_owner_subscription. A collaborator
    # must name the Workspace; the boundary also keeps assignee and category inside it.
    ("POST", "/api/v1/tasks"): _new_task,
    # tasks.py get_one_task / get_task_history / get_logs: require_workspace_readable(task.workspace_id).
    ("GET", "/api/v1/tasks/{task_id}"): _TASK,
    ("GET", "/api/v1/tasks/{task_id}/history"): _TASK,
    ("GET", "/api/v1/tasks/{task_id}/logs"): _TASK,
    # tasks.py update_one_task / delete_one_task / retry_task_endpoint / respond_to_hitl / add_log /
    # move_task_endpoint: require_workspace_writable(task.workspace_id); decide_approval_task:
    # require_workspace_authority. A reassignment must name an Agent of the task's Workspace.
    ("PUT", "/api/v1/tasks/{task_id}"): _with_body_agent(_TASK),
    ("POST", "/api/v1/tasks/{task_id}/move"): _TASK,
    # tasks.py upload_task_attachment: require_workspace_writable(task.workspace_id), stored under
    # the task's company root (tasks/<task_id>/); download_task_attachment:
    # require_workspace_readable(task.workspace_id), read from the same task folder.
    ("POST", "/api/v1/tasks/{task_id}/attachments"): _TASK,
    ("GET", "/api/v1/tasks/{task_id}/attachments/{filename}"): _TASK,
    ("DELETE", "/api/v1/tasks/{task_id}"): _TASK,
    ("POST", "/api/v1/tasks/{task_id}/retry"): _TASK,
    ("POST", "/api/v1/tasks/{task_id}/hitl-response"): _TASK,
    ("POST", "/api/v1/tasks/{task_id}/approval"): _TASK,
    ("POST", "/api/v1/tasks/{task_id}/logs"): _TASK,
    # goals.py list_goals / get_goal: require_workspace_readable; create_goal / update_goal /
    # delete_goal: require_workspace_writable(goal.workspace_id).
    ("GET", "/api/v1/goals"): _query,
    ("POST", "/api/v1/goals"): _body,
    ("GET", "/api/v1/goals/{goal_id}"): _GOAL,
    ("PUT", "/api/v1/goals/{goal_id}"): _GOAL,
    ("DELETE", "/api/v1/goals/{goal_id}"): _GOAL,
    # scheduler.py list_jobs: require_workspace_readable(workspace_id); create_job / get_job /
    # update_job / delete_job / toggle_job / run_job_now / get_job_runs / get_job_run_detail:
    # _legacy_compatible_workspace_gate, which for a collaborator always requires the Workspace.
    # A job that names an Agent must name one of that Workspace.
    ("GET", "/api/v1/jobs"): _query,
    ("POST", "/api/v1/jobs"): _with_body_agent(_body),
    ("GET", "/api/v1/jobs/{job_id}"): _job,
    ("PUT", "/api/v1/jobs/{job_id}"): _with_body_agent(_job),
    ("DELETE", "/api/v1/jobs/{job_id}"): _job,
    ("POST", "/api/v1/jobs/{job_id}/toggle"): _job,
    ("POST", "/api/v1/jobs/{job_id}/run_now"): _job,
    ("GET", "/api/v1/jobs/{job_id}/runs"): _job,
    ("GET", "/api/v1/jobs/{job_id}/runs/{run_id}"): _job,
    # workflows.py list_workflows: filtered to collaborator_scope(user); get_workflow:
    # _require_workflow(VIEW).
    ("GET", "/api/v1/workflows"): _handler,
    ("GET", "/api/v1/workflows/{workflow_id}"): _workflow,
    # workflows.py list_bindings / create_binding / update_binding / delete_binding / run_binding:
    # _require_binding_workspace_read/_write(binding.workspace_id).
    ("GET", "/api/v1/workflows/bindings"): _query,
    ("POST", "/api/v1/workflows/bindings"): _body,
    ("PUT", "/api/v1/workflows/bindings/{binding_id}"): _BINDING,
    ("DELETE", "/api/v1/workflows/bindings/{binding_id}"): _BINDING,
    ("POST", "/api/v1/workflows/bindings/{binding_id}/run"): _BINDING,
    # workflows.py list_runs: workspace_id must be in user_readable_workspace_ids; get_run /
    # get_run_family: _require_run_read; run controls: _require_run_control (Workspace write).
    ("GET", "/api/v1/workflows/runs"): _query,
    ("GET", "/api/v1/workflows/runs/{run_id}"): _RUN,
    ("GET", "/api/v1/workflows/runs/{run_id}/family"): _RUN,
    ("POST", "/api/v1/workflows/runs/{run_id}/cancel"): _RUN,
    ("POST", "/api/v1/workflows/runs/{run_id}/pause"): _RUN,
    ("POST", "/api/v1/workflows/runs/{run_id}/resume"): _RUN,
    ("POST", "/api/v1/workflows/runs/{run_id}/retry"): _RUN,
    ("POST", "/api/v1/workflows/runs/{run_id}/approvals/{request_id}/resolve"): _RUN,
    # chat.py chat_stream / list_chat_composer_skills: _resolve_chat_workspace_scope
    # (user_can_read_workspace_id, conversation in the same Workspace) and the agent check.
    ("POST", "/api/v1/chat/stream"): _chat_scope("body"),
    ("GET", "/api/v1/chat/composer/skills"): _chat_scope("query"),
    # chat.py get_messages / get_messages_page / record_message_feedback / get_conversation_feedback /
    # cancel_conversation_file_approvals / get_active_chat_runtime_run: _get_accessible_conversation
    # -> user_can_read_workspace_id(conv.workspace_id). Personal and channel-history conversations
    # (no Workspace) stay closed.
    ("GET", "/api/v1/chat/conversations/{conversation_id}/messages"): _CONVERSATION,
    ("GET", "/api/v1/chat/conversations/{conversation_id}/messages/page"): _CONVERSATION,
    ("POST", "/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"): _CONVERSATION,
    ("GET", "/api/v1/chat/conversations/{conversation_id}/feedback"): _CONVERSATION,
    ("POST", "/api/v1/chat/conversations/{conversation_id}/file-approvals/cancel"): _CONVERSATION,
    ("GET", "/api/v1/chat/conversations/{conversation_id}/active-run"): _CONVERSATION,
    # chat.py runtime run routes: _get_owned_runtime_run (the caller's own run).
    ("GET", "/api/v1/chat/runs/{run_id}"): _CHAT_RUN,
    ("GET", "/api/v1/chat/runs/{run_id}/events"): _CHAT_RUN,
    ("POST", "/api/v1/chat/runs/{run_id}/input"): _CHAT_RUN,
    ("POST", "/api/v1/chat/runs/{run_id}/cancel"): _CHAT_RUN,
    # chat.py resolve_global_chat_action: the caller's own conversation (Conversation.user_id).
    ("POST", "/api/v1/chat/messages/{message_id}/resolve"): _message,
    # documents.py list_my_documents: list_visible_documents(workspace_id=...) under the Document ACL.
    ("GET", "/api/v1/documents"): _query,
    # documents.py get_one_document / preview_document_content / get_document_content_endpoint /
    # download_document / document_thumbnail: get_visible_document (Document ACL).
    ("GET", "/api/v1/documents/{doc_id}"): _document_path,
    ("GET", "/api/v1/documents/{doc_id}/preview/content"): _document_path,
    ("GET", "/api/v1/documents/{doc_id}/content"): _document_path,
    ("GET", "/api/v1/documents/{doc_id}/download"): _document_path,
    ("GET", "/api/v1/documents/{doc_id}/thumbnail"): _document_path,
    # filesystem.py read_file: _assert_raw_file_openable, which for a collaborator also
    # requires a Document at that path linked to a granted Workspace.
    ("GET", "/api/v1/fs/read"): _fs_read,
    # agents.py agent_catalog: filtered to collaborator_scope(user); get_one_agent / update_one_agent /
    # agent_tools: _require_agent; create_new_agent: user_can_write_workspace_id(req.workspace_id).
    ("GET", "/api/v1/agents/catalog"): _handler,
    ("POST", "/api/v1/agents"): _body,
    ("GET", "/api/v1/agents/{agent_id}"): _agent,
    ("PUT", "/api/v1/agents/{agent_id}"): _agent,
    ("GET", "/api/v1/agents/{agent_id}/tools"): _agent,
    # skills.py list_agent_bindings / list_agent_available_skills: for a collaborator the handler
    # keeps only Skills invocable in a granted Workspace of the Agent and omits company-wide
    # binding provenance. bind_skill: _require_writable_agent + _require_skill(VIEW), and the
    # boundary requires the Skill to be invocable in the Agent's granted Workspace;
    # unbind_skill: _require_writable_agent.
    ("GET", "/api/v1/skills/agents/{agent_id}/bindings"): _agent,
    ("GET", "/api/v1/skills/agents/{agent_id}/available"): _agent,
    ("POST", "/api/v1/skills/agents/{agent_id}/bindings/{skill_id}"): _agent_skill,
    ("DELETE", "/api/v1/skills/agents/{agent_id}/bindings/{skill_id}"): _agent,
    # agents.py list/bind_subscription_worker_endpoint: _owned_subscription (Workspace read/write).
    ("GET", "/api/v1/agents/subscriptions/{subscription_id}/workers"): _row(AgentSubscription, "subscription_id"),
    ("POST", "/api/v1/agents/subscriptions/{subscription_id}/workers"): _row(AgentSubscription, "subscription_id"),
}


def route_access(key: tuple[str, str], path_params) -> str:
    """How a collaborator may use a route: self, filtered, workspace or deny."""
    if key in SELF_ROUTES:
        return "self"
    if key in WORKSPACE_FILTERED_ROUTES:
        return "filtered"
    if key in WORKSPACE_ROUTES_REVIEWED and any(name in path_params for name in WORKSPACE_PATH_PARAMS):
        return "workspace"
    return "deny"


async def enforce_collaborator_route(request: Request, allowed_workspace_ids, db, entity_id: str, user_id: str) -> None:
    """Admit a collaborator request only to a route this module allows."""
    route = request.scope.get("route")
    path = getattr(route, "path_format", None)
    if not path:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_AVAILABLE)
    key = (request.method.upper(), path)
    access = route_access(key, request.path_params)
    if access == "self":
        return
    if access == "filtered":
        scope = CollaboratorScope(user_id=user_id, entity_id=entity_id, workspace_ids=frozenset(allowed_workspace_ids))
        if not await WORKSPACE_FILTERED_ROUTES[key](request, db, scope) & scope.workspace_ids:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        return
    if access == "deny":
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_AVAILABLE)
    workspace_id = next(request.path_params[name] for name in WORKSPACE_PATH_PARAMS if name in request.path_params)
    if workspace_id not in allowed_workspace_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
