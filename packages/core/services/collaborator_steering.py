"""Which external Workspace collaborators steered a Task.

A collaborator's influence on a Task outlives the request that applied it: a
Task the collaborator created, edited, started, answered, approved or
commented on later runs as its customer requester, a system worker or a
delegated agent. ``Task.collaborator_ids`` records those collaborators, and
the runtime authorization gate narrows every run of the Task to their live
grants (runtime_authorization.collaborator).

Recording is one rule rather than a list of call sites: while a collaborator
acts -- their authenticated HTTP request (apps/api/deps), or a runtime tool
call whose acting humans include a collaborator -- every flush that creates
or changes a Task, adds a TaskLog to one (comments, status changes, answers,
plan events), or resolves a HitlRequest of one marks that Task. Reads never
mark.

The same flush records where a Workspace's integrations came from. A
collaborator may edit the Workspace configuration, but a binding they add
must not unlock the customer's company resources:

* every ``operating_model.capability_bindings`` entry a collaborator adds or
  changes carries ``added_by_collaborator`` (their user id); entries they
  leave unchanged keep the provenance they had, so it can be neither forged
  nor stripped;
* an integration/MCP binding they add, a provider they remove from the
  Workspace's ``integration_scope.disabled_providers``, or an Agent MCP
  binding they create or change is refused unless the collaborator owns a
  connection for that provider (their OAuth account, or an Integration they
  own in the company) -- ``CollaboratorBindingRejected``, a 403.

The runtime gate then counts a stamped binding only while its collaborator
still owns such a connection (runtime_authorization.collaborator).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

# (entity_id, collaborator user ids) of the collaborators acting right now.
_ACTING: ContextVar[tuple[str, frozenset[str]] | None] = ContextVar("collaborator_steering", default=None)
# True only inside ``collaborator_steering_scope``: outside a bounded unit of
# work (a dispatcher loop, a test) the gate's marking would outlive the call.
_SCOPED: ContextVar[bool] = ContextVar("collaborator_steering_scoped", default=False)
_INSTALLED: set[type] = set()
_SESSION_KEY = "collaborators_acting"
BINDING_PROVENANCE_KEY = "added_by_collaborator"


class CollaboratorBindingRejected(HTTPException):
    def __init__(self, provider: str) -> None:
        super().__init__(
            403,
            f"External collaborators can bind only integrations they connected themselves; "
            f"'{provider}' is a company integration. Ask a member of this company to add it.",
        )


def _merged(current, entity_id, user_ids) -> tuple[str, frozenset[str]] | None:
    ids = frozenset(str(value) for value in user_ids if value)
    if not entity_id or not ids:
        return current
    if current is not None and current[0] == entity_id:
        ids |= current[1]
    return str(entity_id), ids


def collaborators_acting(entity_id: str | None, user_ids: Iterable[str | None]) -> None:
    """Mark Tasks this context writes in ``entity_id`` as steered by ``user_ids``.

    A no-op outside ``collaborator_steering_scope``, so the marking always
    ends with the unit of work.
    """
    if _SCOPED.get():
        _ACTING.set(_merged(_ACTING.get(), entity_id, user_ids))


def collaborators_acting_now(entity_id: str | None) -> frozenset[str]:
    """Collaborators behind the current tool call in ``entity_id`` (set by the runtime gate).

    Handlers whose listing or creation is not keyed on an id use it to keep a
    collaborator-involved call inside the run Workspace.
    """
    current = _ACTING.get()
    return current[1] if current is not None and entity_id and current[0] == str(entity_id) else frozenset()


def collaborator_request_acting(db, entity_id: str | None, user_id: str | None) -> None:
    """A collaborator's HTTP request: its Session, and any Session the request opens.

    The context-wide marking ends with the request's task, which never outlives
    the request.
    """
    session = getattr(db, "sync_session", db)
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[_SESSION_KEY] = _merged(info.get(_SESSION_KEY), entity_id, (user_id,))
    _SCOPED.set(True)
    _ACTING.set(_merged(_ACTING.get(), entity_id, (user_id,)))


@contextmanager
def collaborator_steering_scope() -> Iterator[None]:
    """Bound ``collaborators_acting`` to one unit of work (a tool call)."""
    token = _ACTING.set(_ACTING.get())
    scoped = _SCOPED.set(True)
    try:
        yield
    finally:
        _SCOPED.reset(scoped)
        _ACTING.reset(token)


def collaborator_steering_scoped(fn):
    """Run an async unit of work (a tool call: gate, then handler) in its own scope."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with collaborator_steering_scope():
            return await fn(*args, **kwargs)

    return wrapper


def _steer(task, user_ids: frozenset[str]) -> None:
    current = set(task.collaborator_ids or ())
    if not user_ids <= current:
        task.collaborator_ids = sorted(current | user_ids)


def _mark_steered_tasks(session, flush_context, instances) -> None:
    for acting in {_ACTING.get(), session.info.get(_SESSION_KEY)} - {None}:
        _mark(session, *acting)


def _mark(session, entity_id: str, user_ids: frozenset[str]) -> None:
    from sqlalchemy import inspect

    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.mcp import AgentMCPBinding
    from packages.core.models.task import Task, TaskLog
    from packages.core.models.workspace import Workspace

    linked: set[str] = set()
    for obj in (*session.new, *session.dirty):
        if isinstance(obj, Task):
            if obj.entity_id == entity_id and (obj in session.new or session.is_modified(obj)):
                _steer(obj, user_ids)
        elif isinstance(obj, TaskLog):
            if obj in session.new and obj.task_id:
                linked.add(str(obj.task_id))
        elif isinstance(obj, HitlRequest):
            # Resolving the card (its status), not viewing or reminding.
            if (
                obj.entity_id == entity_id
                and obj.origin_task_id
                and obj not in session.new
                and inspect(obj).attrs.status.history.has_changes()
            ):
                linked.add(str(obj.origin_task_id))
        elif isinstance(obj, Workspace):
            if (
                obj.entity_id == entity_id
                and obj not in session.new
                and inspect(obj).attrs.operating_model.history.has_changes()
            ):
                _record_workspace_bindings(session, obj, entity_id, user_ids)
        elif isinstance(obj, AgentMCPBinding):
            if obj in session.new or session.is_modified(obj):
                _check_agent_mcp_binding(session, obj, entity_id, user_ids)
    for task_id in sorted(linked):
        task = session.get(Task, task_id)
        if task is not None and task.entity_id == entity_id:
            _steer(task, user_ids)


def ownership_statements(entity_id: str, user_ids: Iterable[str]):
    """Queries for the providers each user holds a connection for in the company."""
    from sqlalchemy import select

    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount

    ids = sorted({str(value) for value in user_ids if value})
    return (
        select(OAuthAccount.user_id, OAuthAccount.provider).where(OAuthAccount.user_id.in_(ids)),
        select(Integration.owner_user_id, Integration.provider).where(
            Integration.entity_id == entity_id,
            Integration.owner_user_id.in_(ids),
            Integration.status == "active",
        ),
    )


def owned_providers(rows) -> dict[str, set[str]]:
    from packages.core.services.provider_keys import canonical_provider_key

    owned: dict[str, set[str]] = {}
    for user_id, provider in rows:
        if user_id and (key := canonical_provider_key(provider)):
            owned.setdefault(str(user_id), set()).add(key)
    return owned


def binding_providers(binding: dict) -> set[str]:
    """Providers an operating-model capability binding grants (empty for non-integrations)."""
    from packages.core.ai.runtime.capability_bindings import expand_runtime_capability_binding
    from packages.core.services.provider_keys import canonical_provider_key
    from packages.core.services.workspace_integration_scope import integration_provider_for_tool

    providers: set[str] = set()
    if str(binding.get("capability_type") or "").strip().lower() in {"mcp", "integration"}:
        for key in ("mcp_server_key", "integration_key", "capability"):
            if provider := canonical_provider_key(binding.get(key)):
                providers.add(provider)
                break
    try:
        tools = expand_runtime_capability_binding(binding).mcp_tool_names
    except Exception:
        tools = set()
    providers.update(provider for tool in tools if (provider := integration_provider_for_tool(tool)))
    return providers


def _owner(session, entity_id: str, user_ids: frozenset[str], providers: set[str]) -> str | None:
    """A collaborator among ``user_ids`` who holds a connection for every provider."""
    owned = owned_providers(
        row for statement in ownership_statements(entity_id, user_ids) for row in session.execute(statement).all()
    )
    return next((user_id for user_id in sorted(user_ids) if providers <= owned.get(user_id, set())), None)


def _entry_key(entry: dict) -> str:
    return json.dumps(
        {key: value for key, value in entry.items() if key != BINDING_PROVENANCE_KEY}, sort_keys=True, default=str,
    )


def _record_workspace_bindings(session, workspace, entity_id: str, user_ids: frozenset[str]) -> None:
    from sqlalchemy import select

    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_integration_scope import disabled_workspace_providers

    stored = session.execute(select(Workspace.operating_model).where(Workspace.id == workspace.id)).scalar()
    old = stored if isinstance(stored, dict) else {}
    new = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    old_entries = {
        _entry_key(entry): entry for entry in old.get("capability_bindings") or [] if isinstance(entry, dict)
    }
    for entry in new.get("capability_bindings") or []:
        if not isinstance(entry, dict):
            continue
        prior = old_entries.get(_entry_key(entry))
        if prior is not None:
            if prior.get(BINDING_PROVENANCE_KEY):
                entry[BINDING_PROVENANCE_KEY] = prior[BINDING_PROVENANCE_KEY]
            else:
                entry.pop(BINDING_PROVENANCE_KEY, None)
            continue
        providers = binding_providers(entry)
        owner = _owner(session, entity_id, user_ids, providers) if providers else sorted(user_ids)[0]
        if owner is None:
            raise CollaboratorBindingRejected(sorted(providers)[0])
        entry[BINDING_PROVENANCE_KEY] = owner
    for provider in sorted(disabled_workspace_providers(old) - disabled_workspace_providers(new)):
        if _owner(session, entity_id, user_ids, {provider}) is None:
            raise CollaboratorBindingRejected(provider)


def _check_agent_mcp_binding(session, binding, entity_id: str, user_ids: frozenset[str]) -> None:
    from packages.core.models.mcp import MCPServer
    from packages.core.models.workspace import Agent
    from packages.core.services.provider_keys import canonical_provider_key

    if binding.status != "active":
        return  # removing a binding only narrows the Agent
    agent = session.get(Agent, binding.agent_id)
    if agent is not None and agent.entity_id not in (None, entity_id):
        return
    server = session.get(MCPServer, binding.mcp_server_id)
    provider = canonical_provider_key(server.server_key if server is not None else binding.mcp_server_id)
    if _owner(session, entity_id, user_ids, {provider}) is None:
        raise CollaboratorBindingRejected(provider)


def install(session_class) -> None:
    """Register the steering recorder on ``session_class`` (idempotent)."""
    from sqlalchemy import event

    if session_class in _INSTALLED:
        return
    _INSTALLED.add(session_class)
    event.listen(session_class, "before_flush", _mark_steered_tasks)
