"""Which integrations discovery may offer an actor, cached until they change.

``resolve_usable_mcp_providers`` derives this per search from ~10 queries
(accounts, shares, memberships, preferences, platform credentials), each
queued behind the chat pod's single database permit under load. The answer
changes only when one of those rows does, so it is cached per actor under a
key built from version counters that commits bump:

- global: platform MCP credentials (``mcp_servers``) and writes whose rows
  cannot be scoped;
- entity: integrations, resource grants, memberships, staff and their roles;
- user: OAuth accounts, the user row, memberships, platform admins.

An actor using someone else's shared account depends on that owner too: a
fill registers the dependency, and bumping the owner bumps those entities.
Presence-based providers are never cached. This feeds discovery only;
execution re-checks every call. Changes no committed write records (a token
expiring unrefreshed, a synchronous session's write) last at most the TTL.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

SNAPSHOT_TTL = 120
SNAPSHOT_MAX_TTL = 300
_VERSION_TTL = 30 * 24 * 3600
_IO_TIMEOUT = 0.1
# Invalidation runs after commit, outside admission; a missed one leaves
# snapshots stale until their TTL, so it may wait out a reconnect.
_BUMP_TIMEOUT = 1.0
_PREFIX = "manor:integration_avail:v1"

# Rows whose writes can change what discovery offers: table -> the column
# naming the entity / user the row scopes to.
_ENTITY_COLUMNS = {
    "integrations": "entity_id", "resource_grants": "entity_id", "user_memberships": "entity_id",
    "staff": "entity_id", "staff_roles": "entity_id",
}
_USER_COLUMNS = {"oauth_accounts": "user_id", "user_memberships": "user_id", "users": "id"}
_GLOBAL_TABLES = frozenset({"mcp_servers"})
_TRACKED_TABLES = frozenset(_ENTITY_COLUMNS) | frozenset(_USER_COLUMNS) | _GLOBAL_TABLES
# Tables also written for unrelated reasons matter only through these columns.
_RELEVANT_COLUMNS = {
    "users": frozenset({"status", "deleted_at", "preferences", "entity_id"}),
    "mcp_servers": frozenset({"server_key", "status", "credential_ref"}),
}
# A user's own account defaults do not change what the accounts they share
# offer anyone else, so such writes skip the dependent entities.
_SELF_ONLY_COLUMNS = frozenset({"preferences"})
_INSTALLED: set[type] = set()


def _version_key(kind: str, ident: str = "") -> str:
    return f"{_PREFIX}:ver:{kind}:{ident}"


def _dependents_key(owner_user_id: str) -> str:
    return f"{_PREFIX}:dependents:{owner_user_id}"


async def _redis():
    from packages.core.cache import _get_redis

    return await _get_redis()


async def current_versions(entity_id: str, user_id: str) -> tuple[str, str, str] | None:
    """The versions an actor's snapshot is keyed by; None when Redis is unavailable."""
    try:
        async with asyncio.timeout(_IO_TIMEOUT):
            redis = await _redis()
            if redis is None:
                return None
            values = await redis.mget(
                _version_key("global"), _version_key("entity", entity_id), _version_key("user", user_id),
            )
    except Exception:
        return None
    return tuple(str(int(value)) if value else "0" for value in values)  # type: ignore[return-value]


def snapshot_key(entity_id: str, user_id: str, versions: tuple[str, ...], provider_keys: Iterable[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(provider_keys)).encode()).hexdigest()[:16]
    return f"{_PREFIX}:snapshot:{entity_id}:{user_id}:{':'.join(versions)}:{digest}"


async def register_dependents(owner_user_ids: Iterable[str], entity_id: str) -> bool:
    """Record that ``entity_id`` snapshots use these owners' shared accounts."""
    owners = sorted({str(owner) for owner in owner_user_ids if owner})
    if not owners:
        return True
    try:
        async with asyncio.timeout(_IO_TIMEOUT):
            redis = await _redis()
            if redis is None:
                return False
            for owner in owners:
                await redis.sadd(_dependents_key(owner), entity_id)
                await redis.expire(_dependents_key(owner), _VERSION_TTL)
    except Exception:
        return False
    return True


async def bump(
    *, global_: bool = False, entities: Iterable[str] = (), users: Iterable[str] = (),
    users_self: Iterable[str] = (),
) -> None:
    """Invalidate snapshots after a committed write (never raises).

    ``users`` also invalidates the entities using their shared accounts;
    ``users_self`` only their own snapshots.
    """
    entities, users = set(filter(None, entities)), set(filter(None, users))
    users_self = set(filter(None, users_self)) - users
    if not (global_ or entities or users or users_self):
        return
    try:
        async with asyncio.timeout(_BUMP_TIMEOUT):
            redis = await _redis()
            if redis is None:
                return
            for user_id in users:
                entities.update(
                    member.decode() if isinstance(member, bytes) else str(member)
                    for member in await redis.smembers(_dependents_key(user_id))
                )
            keys = ([_version_key("global")] if global_ else []) + [
                _version_key("entity", entity_id) for entity_id in entities
            ] + [_version_key("user", user_id) for user_id in users | users_self]
            for key in keys:
                await redis.incr(key)
                await redis.expire(key, _VERSION_TTL)
    except Exception:
        logger.warning("Integration availability invalidation unavailable; snapshots expire by TTL")


def _empty() -> dict:
    return {"global": False, "entities": set(), "users": set(), "users_self": set()}


def _record(found: dict, table: str, value_of, changed: frozenset[str] | None) -> None:
    """Scope one row write. ``value_of(column)`` is None when unknown;
    ``changed`` is None for inserts/deletes (every column changed)."""
    relevant = _RELEVANT_COLUMNS.get(table)
    if changed is not None and relevant is not None:
        changed = changed & relevant
        if not changed:
            return
    if table in _GLOBAL_TABLES:
        found["global"] = True
        return
    scoped = False
    if table in _ENTITY_COLUMNS and (entity_id := value_of(_ENTITY_COLUMNS[table])):
        found["entities"].add(str(entity_id))
        scoped = True
    if table in _USER_COLUMNS and (user_id := value_of(_USER_COLUMNS[table])):
        self_only = changed is not None and changed <= _SELF_ONLY_COLUMNS
        found["users_self" if self_only else "users"].add(str(user_id))
        scoped = True
    if not scoped:
        found["global"] = True


def _flush_changes(session) -> dict:
    """What this flush changes among tracked rows."""
    from sqlalchemy import inspect

    found = _empty()
    for state_name, objects in (("new", session.new), ("dirty", session.dirty), ("deleted", session.deleted)):
        for obj in objects:
            table = getattr(obj, "__tablename__", None)
            if table not in _TRACKED_TABLES:
                continue
            changed = None
            if state_name == "dirty":
                changed = frozenset(attr.key for attr in inspect(obj).attrs if attr.history.has_changes())
                if not changed:
                    continue
            _record(found, table, lambda column, obj=obj: getattr(obj, column, None), changed)
    return found


def _statement_changes(statement) -> dict | None:
    """Scope a bulk insert/update/delete statement (None: untracked table).

    ``WHERE <scope column> = <value>`` scopes it like a row write; anything
    else (a multi-row write, an insert) invalidates everything.
    """
    from sqlalchemy.sql import operators, visitors
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter, ColumnClause

    table = getattr(getattr(statement, "table", None), "name", None)
    if table not in _TRACKED_TABLES:
        return None
    found = _empty()
    where = getattr(statement, "whereclause", None)
    if where is None:
        found["global"] = True
        return found
    values = getattr(statement, "_values", None) or {}
    changed = frozenset(getattr(column, "key", None) or str(column) for column in values) or None
    equals: dict[str, object] = {}
    for node in visitors.iterate(where):
        if (
            isinstance(node, BinaryExpression) and node.operator is operators.eq
            and isinstance(node.left, ColumnClause) and isinstance(node.right, BindParameter)
            and getattr(getattr(node.left, "table", None), "name", None) == table
        ):
            equals[node.left.key] = node.right.effective_value
    _record(found, table, equals.get, changed)
    return found


def _merge(session, found: dict) -> None:
    pending = session.info.setdefault("integration_avail_pending", _empty())
    pending["global"] = pending["global"] or found["global"]
    for kind in ("entities", "users", "users_self"):
        pending[kind].update(found[kind])


def install(session_class) -> None:
    """Register the commit hooks on ``session_class`` (idempotent).

    Changes collect per session; a root commit applies them after the
    admission permit is released, a root rollback discards them. A rolled
    back savepoint still invalidates: extra misses, never stale hits.
    """
    from sqlalchemy import event
    from sqlalchemy.sql.dml import UpdateBase
    from sqlalchemy.util.concurrency import await_only, in_greenlet

    if session_class in _INSTALLED:
        return
    _INSTALLED.add(session_class)

    @event.listens_for(session_class, "before_flush")
    def _before_flush(session, flush_context, instances):
        found = _flush_changes(session)
        if found["global"] or found["entities"] or found["users"] or found["users_self"]:
            _merge(session, found)

    @event.listens_for(session_class, "do_orm_execute")
    def _bulk_write(state):
        if not isinstance(state.statement, UpdateBase):
            return
        try:
            found = _statement_changes(state.statement)
        except Exception:
            found = {**_empty(), "global": True}
        if found is not None:
            _merge(state.session, found)

    @event.listens_for(session_class, "after_commit")
    def _after_commit(session):
        # Also fires when a savepoint is released: merge, never overwrite.
        pending = session.info.pop("integration_avail_pending", None)
        if pending is not None:
            committed = session.info.setdefault("integration_avail_committed", _empty())
            committed["global"] = committed["global"] or pending["global"]
            for kind in ("entities", "users", "users_self"):
                committed[kind].update(pending[kind])

    @event.listens_for(session_class, "after_rollback")
    def _after_rollback(session):
        session.info.pop("integration_avail_pending", None)

    @event.listens_for(session_class, "after_transaction_end")
    def _after_end(session, transaction):
        if transaction.parent is not None:
            return
        committed = session.info.pop("integration_avail_committed", None)
        if not committed:
            return
        if in_greenlet():
            await_only(bump(
                global_=committed["global"], entities=committed["entities"],
                users=committed["users"], users_self=committed["users_self"],
            ))
        else:
            logger.warning("Synchronous integration write skipped snapshot invalidation; snapshots expire by TTL")
