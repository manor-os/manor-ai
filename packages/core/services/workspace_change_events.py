"""Tell everyone viewing a Workspace that its configuration changed.

Configuration (agents, skills, workflows, goals, rules, channels, the
Workspace row itself) is written from many places: REST routes, the
in-product builder tools, MCP, Blueprint upgrades. None of them pushed a
realtime event, so a second viewer -- a teammate, the customer, or a
provider in the customer-trial frame -- kept the old configuration until a
cache expired or the page reloaded.

Every committed write to a tracked row queues its Workspace; after the root
commit one ``workspace_changed`` event per Workspace goes out on the
``workspace`` relay target, which re-checks who may read that Workspace
(collaborators included) before delivery. The browser treats it as "refetch
this Workspace", so the payload carries ids only.

Columns that runtime churns (heartbeat clocks, spend, measured Goal values)
do not count as a configuration change.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

EVENT = "workspace_changed"

# Tracked table -> the column naming its Workspace.
_WORKSPACE_COLUMNS = {
    "workspaces": "id",
    "workspace_staff": "workspace_id",
    "agents": "workspace_id",
    "agent_subscriptions": "workspace_id",
    "skills": "workspace_id",
    "workflow_definitions": "workspace_id",
    "workflow_bindings": "workspace_id",
    "goals": "workspace_id",
    "governance_policies": "workspace_id",
    "channel_configs": "workspace_id",
    "workspace_operation_drafts": "workspace_id",
}
_IGNORED_COLUMNS = frozenset({"updated_at"})
# Runtime writes to these columns are not configuration changes.
_RUNTIME_COLUMNS = {
    "workspaces": frozenset({"last_heartbeat_at", "monthly_spent_usd", "budget_reset_at", "budget_alert_state"}),
    "goals": frozenset({"current_value", "current_value_updated_at", "pace_status", "pace_computed_at", "achieved_at"}),
}
_PENDING_KEY = "workspace_changes_pending"
_COMMITTED_KEY = "workspace_changes_committed"
_PUBLISH_TASKS: set[asyncio.Task[None]] = set()
_INSTALLED: set[type] = set()


def _configuration_changed(table: str, changed: frozenset[str] | None) -> bool:
    if changed is None:
        return True
    return bool(changed - _IGNORED_COLUMNS - _RUNTIME_COLUMNS.get(table, frozenset()))


def _flush_changes(session) -> dict[str, str]:
    """Workspace id -> its entity id, for tracked rows this flush changes."""
    from sqlalchemy import inspect

    found: dict[str, str] = {}
    for state_name, objects in (("new", session.new), ("dirty", session.dirty), ("deleted", session.deleted)):
        for obj in objects:
            table = getattr(obj, "__tablename__", None)
            column = _WORKSPACE_COLUMNS.get(table)
            if column is None:
                continue
            changed = None
            if state_name == "dirty":
                changed = frozenset(attr.key for attr in inspect(obj).attrs if attr.history.has_changes())
            if not _configuration_changed(table, changed):
                continue
            workspace_id, entity_id = getattr(obj, column, None), getattr(obj, "entity_id", None)
            if workspace_id and entity_id:
                found[str(workspace_id)] = str(entity_id)
    return found


def _statement_changes(statement) -> dict[str, str]:
    """A bulk update/delete scoped by ``WHERE <workspace column> = x AND entity_id = y``.

    Anything else names no single Workspace and is left to the views'
    reconnect resync; broadcasting it to every Workspace would be noise.
    """
    from sqlalchemy.sql import operators, visitors
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter, ColumnClause

    table = getattr(getattr(statement, "table", None), "name", None)
    column = _WORKSPACE_COLUMNS.get(table)
    where = getattr(statement, "whereclause", None)
    if column is None or where is None:
        return {}
    values = getattr(statement, "_values", None) or {}
    changed = frozenset(getattr(key, "key", None) or str(key) for key in values) or None
    if not _configuration_changed(table, changed):
        return {}
    equals: dict[str, object] = {}
    for node in visitors.iterate(where):
        if (
            isinstance(node, BinaryExpression) and node.operator is operators.eq
            and isinstance(node.left, ColumnClause) and isinstance(node.right, BindParameter)
            and getattr(getattr(node.left, "table", None), "name", None) == table
        ):
            equals[node.left.key] = node.right.effective_value
    workspace_id, entity_id = equals.get(column), equals.get("entity_id")
    return {str(workspace_id): str(entity_id)} if workspace_id and entity_id else {}


async def publish(changes: dict[str, str]) -> None:
    """One best-effort ``workspace_changed`` per Workspace."""
    import json

    from packages.core.cache import _get_redis

    redis = await _get_redis()
    if not redis:
        return
    for workspace_id, entity_id in changes.items():
        await redis.publish("manor:ws_broadcast", json.dumps({
            "target": "workspace",
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "event": EVENT,
            "data": {"workspace_id": workspace_id},
        }))


def _schedule(changes: dict[str, str]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # a synchronous session: views catch up on their next refetch
    task = loop.create_task(publish(changes))
    _PUBLISH_TASKS.add(task)

    def finished(done: asyncio.Task[None]) -> None:
        _PUBLISH_TASKS.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.debug("Could not publish Workspace change", exc_info=done.exception())

    task.add_done_callback(finished)


def install(session_class) -> None:
    """Register the commit hooks on ``session_class`` (idempotent).

    Changes collect per session; a root commit publishes them, a rollback
    discards them. A rolled back savepoint still publishes: an extra
    refetch, never a missed one.
    """
    from sqlalchemy import event
    from sqlalchemy.sql.dml import UpdateBase

    if session_class in _INSTALLED:
        return
    _INSTALLED.add(session_class)

    @event.listens_for(session_class, "before_flush")
    def _before_flush(session, flush_context, instances):
        found = _flush_changes(session)
        if found:
            session.info.setdefault(_PENDING_KEY, {}).update(found)

    @event.listens_for(session_class, "do_orm_execute")
    def _bulk_write(state):
        if not isinstance(state.statement, UpdateBase):
            return
        try:
            found = _statement_changes(state.statement)
        except Exception:
            found = {}
        if found:
            state.session.info.setdefault(_PENDING_KEY, {}).update(found)

    @event.listens_for(session_class, "after_commit")
    def _after_commit(session):
        # Also fires when a savepoint is released: merge, never overwrite.
        pending = session.info.pop(_PENDING_KEY, None)
        if pending:
            session.info.setdefault(_COMMITTED_KEY, {}).update(pending)

    @event.listens_for(session_class, "after_rollback")
    def _after_rollback(session):
        session.info.pop(_PENDING_KEY, None)

    @event.listens_for(session_class, "after_transaction_end")
    def _after_end(session, transaction):
        if transaction.parent is not None:
            return
        committed = session.info.pop(_COMMITTED_KEY, None)
        if committed:
            _schedule(committed)
