"""Shared transaction admission, diagnostics and safe read boundaries.

When enabled, admission happens inside SQLAlchemy's async bridge, before pool checkout, not
for the lifetime of an HTTP request, stream, WebSocket or Agent run. Nothing
here commits a write, retries a statement or changes an authorization decision.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import math
import time
import weakref

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.util.concurrency import await_only, in_greenlet

from packages.core.service_role import (
    SERVICE_ROLE_BEAT, SERVICE_ROLE_WORKER, SERVICE_ROLE_WORKER_HEAVY,
    SERVICE_ROLE_WORKER_ROOM, normalize_service_role,
)

logger = logging.getLogger(__name__)
database_request_id: ContextVar[str] = ContextVar("database_request_id", default="")


class DatabaseEngineKind(StrEnum):
    APPLICATION = "application"
    WORKER = "worker"

    @classmethod
    def for_service(cls, role: str) -> DatabaseEngineKind:
        if normalize_service_role(role) in {
            SERVICE_ROLE_WORKER, SERVICE_ROLE_WORKER_ROOM,
            SERVICE_ROLE_WORKER_HEAVY, SERVICE_ROLE_BEAT,
        }:
            return cls.WORKER
        return cls.APPLICATION


class DatabaseBusyError(SQLAlchemyTimeoutError):
    """Admission failed before acquiring a connection or executing SQL."""


class DatabaseFailureKind(StrEnum):
    BUSY = "database_busy"
    UNAVAILABLE = "database_unavailable"
    PROBE_TIMEOUT = "database_probe_timeout"
    PROBE_FAILED = "database_probe_failed"

    @classmethod
    def from_exception(cls, exc: BaseException) -> DatabaseFailureKind | None:
        if isinstance(exc, SQLAlchemyTimeoutError):
            return cls.BUSY
        # Drivers may raise directly while connecting, or through DBAPIError
        # during SQL. Match protocol codes, never sensitive exception text.
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, DBAPIError) and current.connection_invalidated:
                return cls.UNAVAILABLE
            code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
            if isinstance(code, str) and (code.startswith("08") or code in {"57P01", "57P02", "57P03", "53300"}):
                return cls.UNAVAILABLE
            current = getattr(current, "orig", None) or current.__cause__
        return None


@dataclass(frozen=True)
class AdmissionPolicy:
    limit: int
    max_waiters: int
    timeout: float


@dataclass
class DatabaseAdmission:
    policy: AdmissionPolicy
    waiting: int = 0
    active: int = 0
    rejected: int = 0
    queue_full: int = 0
    wait_timeouts: int = 0
    peak_active: int = 0
    peak_waiting: int = 0
    max_wait_seconds: float = 0
    # Sessions admitted on behalf of a task that already holds a permit.
    nested_active: int = 0
    nested_admissions: int = 0
    semaphore: asyncio.Semaphore = field(init=False)
    holders: dict[object, int] = field(init=False)

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(self.policy.limit)
        self.holders = {}

    def holds(self, owner: object | None) -> bool:
        return owner is not None and self.holders.get(owner, 0) > 0

    def enter_nested(self) -> None:
        """Admit a Session whose caller already holds one of this gate's permits.

        Waiting would be hold-and-wait on the caller's own permit: the caller
        cannot finish its transaction until this Session returns. With limit 1
        that is a guaranteed timeout; with N permits, N such callers deadlock.
        The caller still counts once; extra connections are bounded by its own
        nesting/fan-out, and the client pool and PgBouncer remain hard caps.
        """
        self.nested_active += 1
        self.nested_admissions += 1

    def exit_nested(self) -> None:
        self.nested_active -= 1

    async def acquire(self, owner: object | None = None) -> None:
        if self.semaphore.locked() and self.waiting >= self.policy.max_waiters:
            self.rejected += 1
            self.queue_full += 1
            raise DatabaseBusyError("Database admission queue is full")
        start = time.monotonic()
        self.waiting += 1
        if self.semaphore.locked():
            self.peak_waiting = max(self.peak_waiting, self.waiting)
        try:
            async with asyncio.timeout(self.policy.timeout):
                await self.semaphore.acquire()
        except TimeoutError as exc:
            self.rejected += 1
            self.wait_timeouts += 1
            raise DatabaseBusyError("Database admission wait timed out") from exc
        finally:
            self.waiting -= 1
            self.max_wait_seconds = max(self.max_wait_seconds, time.monotonic() - start)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        if owner is not None:
            self.holders[owner] = self.holders.get(owner, 0) + 1

    def release(self, owner: object | None = None) -> None:
        if owner is not None:
            remaining = self.holders.get(owner, 0) - 1
            if remaining > 0:
                self.holders[owner] = remaining
            else:
                self.holders.pop(owner, None)
        self.active -= 1
        self.semaphore.release()


@dataclass(frozen=True)
class _AdmissionLease:
    gate: DatabaseAdmission
    owner: object | None
    nested: bool = False

    def release(self) -> None:
        if self.nested:
            self.gate.exit_nested()
        else:
            self.gate.release(self.owner)


class DatabaseAdmissionGroup:
    """Concurrent tasks that count as one caller at the process gate.

    Members queue among themselves, patiently, and at most ``limit`` of them
    wait for or hold the process gate at once. Many concurrent Agent runs
    started by one request then compete with other requests like that one
    request, instead of filling the admission queue and timing each other out.
    """

    def __init__(self, limit: int = 1, timeout: float = 120.0):
        if limit < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid database admission group")
        self.limit = limit
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(limit)
        self.holders: set[object] = set()
        self.peak_active = 0

    def holds(self, owner: object) -> bool:
        """A member opening a second session while it holds the group's
        permit goes straight to the process gate, as it would without a
        group, instead of waiting on its own permit."""
        return owner in self.holders

    async def acquire(self, owner: object) -> None:
        try:
            async with asyncio.timeout(self.timeout):
                await self.semaphore.acquire()
        except TimeoutError as exc:
            raise DatabaseBusyError("Database admission group wait timed out") from exc
        self.holders.add(owner)
        self.peak_active = max(self.peak_active, len(self.holders))

    def release(self, owner: object) -> None:
        self.holders.discard(owner)
        self.semaphore.release()

    async def wait_for_gate(self, gate: "DatabaseAdmission", owner: object | None = None) -> None:
        """Enter the process gate behind every other caller already waiting.

        The group yields: it queues only when nobody else is waiting, so other
        requests never wait behind more than one of its transactions.
        """
        deadline = time.monotonic() + self.timeout
        while True:
            if gate.waiting == 0:
                try:
                    await gate.acquire(owner)
                    return
                except DatabaseBusyError:
                    pass
            if time.monotonic() >= deadline:
                raise DatabaseBusyError("Database admission group wait timed out")
            await asyncio.sleep(0.02)


database_admission_group: ContextVar[DatabaseAdmissionGroup | None] = ContextVar(
    "database_admission_group", default=None,
)
# Tasks of this caller that took a permit: the current task and the tasks that
# were holding one when they started it (asyncio copies context at creation).
# Siblings started before or by another task are not included.
_admission_lineage: ContextVar[tuple[weakref.ref, ...]] = ContextVar(
    "database_admission_lineage", default=(),
)


def _loop_admission(policy: AdmissionPolicy, *, scope: str = "business") -> DatabaseAdmission:
    # The loop owns its gates, not a process-global map that retains dead Celery
    # asyncio.run() loops. All factories in the same loop share one budget.
    loop = asyncio.get_running_loop()
    gates = getattr(loop, "_manor_database_admission", None)
    if gates is None:
        gates = {}
        loop._manor_database_admission = gates
    key = (scope, policy)
    if key not in gates:
        gates[key] = DatabaseAdmission(policy)
    return gates[key]


def _extend_admission_lineage(*tasks: object | None) -> None:
    lineage = _admission_lineage.get()
    added = tuple(
        weakref.ref(task) for task in dict.fromkeys(tasks)
        if task is not None and not any(ref() is task for ref in lineage)
    )
    if added:
        _admission_lineage.set((*lineage, *added))


class DatabaseSession(Session):
    """Sync half of AsyncSession; get_bind runs before SQL/pool checkout."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # AsyncSession builds this synchronously in the caller's task and
        # context: remember the caller that owns this Session's permits, and
        # let tasks it starts from now on (tool calls) count as that caller.
        try:
            caller = asyncio.current_task()
        except RuntimeError:  # no running loop (sync tooling)
            caller = None
        if caller is not None:
            self.info["database_admission_caller"] = weakref.ref(caller)
            _extend_admission_lineage(caller)

    def get_bind(self, mapper=None, *, clause=None, bind=None, **kwargs):
        resolved = super().get_bind(mapper, clause=clause, bind=bind, **kwargs)
        runtime = getattr(getattr(resolved, "engine", resolved), "_manor_database_runtime", None)
        # Workers behind PgBouncer can bypass both business gates. Retain the
        # managed Session's transaction tracking and the independent probe cap.
        if runtime is not None and not runtime.admission_enabled and not self.info.get("database_probe"):
            return resolved
        # Execution-lease statements (one short UPDATE per claim every 30s)
        # fence the business work that holds this loop's permits. Waiting for
        # those permits made renewals fail under load and owners give up
        # leases nobody else could take; they run outside the business gate.
        if runtime is not None and self.info.get("database_lease_control"):
            return resolved
        # Public AsyncSession.get_bind() is also used outside the bridge just
        # to inspect dialects. That must never acquire a connection/permit.
        if runtime is not None and in_greenlet() and (
            clause is not None or self.info.get("database_non_read_transaction")
        ):
            scope = "business"
            policy = runtime.policy
            if self.info.get("database_probe"):
                scope = "probe"
                policy = AdmissionPolicy(1, 0, min(2, policy.timeout))
            gates = self.info.setdefault("database_admission", {})
            key = (scope, policy)
            if key not in gates:
                group = database_admission_group.get() if scope == "business" else None
                owner = asyncio.current_task()
                if (group is not None and owner is not None
                        and "database_admission_group" not in self.info and not group.holds(owner)):
                    await_only(group.acquire(owner))
                    self.info["database_admission_group"] = (group, owner)
                gate = _loop_admission(policy, scope=scope)
                # A business permit belongs to the caller that created this
                # Session, not to whichever task runs a statement on it:
                # agentic_loop runs tools in child tasks, and a tool that
                # commits and re-locks the caller's Session must not leave the
                # permit owned by a finished child (the caller's next credit
                # preflight would then wait on it).
                caller = owner
                if scope == "business":
                    created_by = self.info.get("database_admission_caller")
                    caller = (created_by() if created_by is not None else None) or owner
                # Only the business gate is reentrant; the probe cap stays strict.
                lineage = _admission_lineage.get() if scope == "business" else ()
                if gate.holds(owner if scope == "business" else None) or any(
                    gate.holds(ref()) for ref in lineage
                ):
                    # This task, or the task that started it and is (usually)
                    # awaiting it, holds a permit of this gate: e.g. the credit
                    # preflight inside a Workspace draft turn's transaction.
                    gate.enter_nested()
                    gates[key] = _AdmissionLease(gate, None, nested=True)
                    logger.debug(
                        "database_admission_nested scope=%s request_id=%s nested_active=%s",
                        scope, database_request_id.get(), gate.nested_active,
                    )
                    return resolved
                held = self.info.get("database_admission_group") if scope == "business" else None
                if held is None:
                    await_only(gate.acquire(caller))
                else:
                    # The group's one waiter keeps its place until the group
                    # deadline rather than failing behind another request's
                    # long transaction. A final timeout leaves the permit in
                    # info: close() releases it with the session's admission.
                    await_only(held[0].wait_for_gate(gate, caller))
                gates[key] = _AdmissionLease(gate, caller)
                if scope == "business":
                    # The greenlet shares the task's context, so later Sessions
                    # of this task and tasks it starts from now on see it.
                    _extend_admission_lineage(owner, caller)
        return resolved

    def connection(self, bind_arguments=None, execution_options=None):
        self.info["database_non_read_transaction"] = True
        return super().connection(
            bind_arguments=bind_arguments, execution_options=execution_options,
        )

    def close(self):
        super().close()
        # A bind may be resolved before autobegin. Close still releases its
        # admission if execution failed before a root transaction was created.
        _release_admission(self, None)


@event.listens_for(DatabaseSession, "do_orm_execute")
def _track_statement(state):
    statement = state.statement
    # Only the catalog-qualified text functions used by Skill slug lookup are
    # audited reads. Unqualified/custom functions may mutate state or lock.
    # Inspect every descendant: an outer read can wrap a locking subquery,
    # a write CTE, raw SQL or an unknown function.
    if (
        not state.is_select
        or any(
            isinstance(node, (TextClause, UpdateBase))
            or (isinstance(node, ColumnClause) and node.is_literal)
            or getattr(node, "_for_update_arg", None) is not None
            or (
                isinstance(node, FunctionElement)
                and not (
                    getattr(node, "packagenames", ()) == ("pg_catalog",)
                    and getattr(node, "name", None) in {"lower", "replace"}
                )
            )
            for node in visitors.iterate(statement)
        )
    ):
        state.session.info["database_non_read_transaction"] = True


@event.listens_for(DatabaseSession, "before_flush")
def _track_flush(session, flush_context, instances):
    if session.new or session.dirty or session.deleted:
        session.info["database_non_read_transaction"] = True


@event.listens_for(DatabaseSession, "after_transaction_end")
def _release_admission(session, transaction):
    if transaction is None or transaction.parent is None:
        for lease in session.info.pop("database_admission", {}).values():
            lease.release()
        _release_admission_group(session)
        session.info.pop("database_non_read_transaction", None)


def _release_admission_group(session) -> None:
    held = session.info.pop("database_admission_group", None)
    if held is not None:
        group, owner = held
        group.release(owner)


async def release_read_transaction(db) -> bool:
    """Explicit snapshot boundary; never commit unknown/flushed writes/locks.

    Loaded attributes stay available (the factory uses expire_on_commit=False).
    Callers must re-read current permission/lifecycle facts before later effects.
    Unmanaged legacy/test Sessions cannot prove safety and are left untouched.
    """
    session = getattr(db, "sync_session", None)
    if not isinstance(session, DatabaseSession) or not db.in_transaction():
        return False
    if (
        db.in_nested_transaction()
        or session.new
        or session.deleted
        or session.info.get("database_non_read_transaction")
        # Session.dirty is conservative: same-value setattr marks an instance
        # dirty without a net change, and flush would not emit UPDATE.
        or any(session.is_modified(obj) for obj in tuple(session.dirty))
    ):
        return False
    await db.commit()
    return True


@dataclass
class DatabaseRuntime:
    kind: DatabaseEngineKind
    policy: AdmissionPolicy
    warn_seconds: float
    timeouts: dict[str, int]
    pool_mode: str
    pool_class: str
    admission_enabled: bool = True
    checked_out: int = 0
    checkout_count: int = 0
    long_holds: int = 0
    max_hold_seconds: float = 0

    def snapshot(self) -> dict:
        gate = _loop_admission(self.policy) if self.admission_enabled else None
        return {
            "kind": self.kind.value,
            "checked_out": self.checked_out,
            "checkout_count": self.checkout_count,
            "long_holds": self.long_holds,
            "max_hold_seconds": round(self.max_hold_seconds, 3),
            "pool_mode": self.pool_mode,
            "admission_enabled": self.admission_enabled,
            # These are gate counters, not all DB work. None means the gate is
            # disabled; checked_out/hold metrics still describe this engine.
            "admission_limit": self.policy.limit if gate is not None else None,
            "max_waiters": self.policy.max_waiters if gate is not None else None,
            "admission_timeout_seconds": self.policy.timeout if gate is not None else None,
            "active_transactions": gate.active if gate is not None else None,
            "waiting": gate.waiting if gate is not None else None,
            "rejected": gate.rejected if gate is not None else None,
            "queue_full": gate.queue_full if gate is not None else None,
            "wait_timeouts": gate.wait_timeouts if gate is not None else None,
            "peak_active_transactions": gate.peak_active if gate is not None else None,
            "peak_waiters": gate.peak_waiting if gate is not None else None,
            "max_wait_seconds": round(gate.max_wait_seconds, 3) if gate is not None else None,
            "active_nested_transactions": gate.nested_active if gate is not None else None,
            "nested_admissions": gate.nested_admissions if gate is not None else None,
        }

    def checkout(self, connection, record, proxy):
        self.checked_out += 1
        self.checkout_count += 1
        record.record_info["database_checkout"] = (time.monotonic(), database_request_id.get())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        record.record_info["database_hold_timer"] = loop.call_later(
            self.warn_seconds, self.warn_hold, record,
        )

    def warn_hold(self, record):
        checkout = record.record_info.get("database_checkout")
        if checkout:
            self.long_holds += 1
            logger.warning(
                "database_connection_long_hold kind=%s request_id=%s held_seconds=%.3f",
                self.kind.value, checkout[1], time.monotonic() - checkout[0],
            )

    def checkin(self, connection, record):
        timer = record.record_info.pop("database_hold_timer", None)
        if timer:
            timer.cancel()
        checkout = record.record_info.pop("database_checkout", None)
        if checkout:
            self.checked_out -= 1
            self.max_hold_seconds = max(self.max_hold_seconds, time.monotonic() - checkout[0])

    def begin(self, connection):
        if connection.dialect.name == "postgresql" and self.timeouts:
            # SET LOCAL semantics work with direct PG and PgBouncer transaction
            # pooling. Never pass unrecognised timeout startup parameters to it.
            expressions = ", ".join(
                f"set_config('{name}', '{value}', true)" for name, value in self.timeouts.items()
            )
            connection.exec_driver_sql(f"SELECT {expressions}")


def configure_database_runtime(engine, settings, kind: DatabaseEngineKind) -> DatabaseRuntime:
    kind = DatabaseEngineKind(kind)
    pool = engine.sync_engine.pool
    pool_mode = "nullpool" if isinstance(pool, NullPool) else "pooled"
    admission_enabled = getattr(settings, "DATABASE_ADMISSION_ENABLED", True)
    if type(admission_enabled) is not bool:
        raise ValueError("DATABASE_ADMISSION_ENABLED must be a boolean")
    configured_limit = getattr(settings, "DATABASE_ADMISSION_LIMIT", 0)
    max_waiters = getattr(settings, "DATABASE_MAX_WAITERS", 14)
    timeout = getattr(settings, "DATABASE_ADMISSION_TIMEOUT", 2.0)
    if (
        type(configured_limit) is not int or configured_limit < 0
        or type(max_waiters) is not int or max_waiters < 0
        or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("Invalid database admission budget")
    # Direct workers historically derive their automatic budget from these
    # settings despite using NullPool. Preserve that default for nested
    # Sessions; it is not a pool constraint on an explicit NullPool budget.
    legacy_worker_default = (
        kind is DatabaseEngineKind.WORKER
        and getattr(settings, "DATABASE_POOL_MODE", "sqlalchemy") != "pgbouncer"
        and not configured_limit
    )
    if pool_mode == "pooled" or legacy_worker_default:
        capacity = settings.DATABASE_POOL_SIZE + settings.DATABASE_MAX_OVERFLOW
        if settings.DATABASE_POOL_SIZE < 1 or settings.DATABASE_MAX_OVERFLOW < 0:
            raise ValueError("Database pool must have a finite positive connection budget")
        limit = configured_limit or max(1, capacity - 1)
        if limit > capacity:
            raise ValueError("Invalid database admission budget")
    elif configured_limit:
        limit = configured_limit
    else:
        limit = 1 if kind is DatabaseEngineKind.WORKER else 4
    policy = AdmissionPolicy(
        limit=limit,
        max_waiters=max_waiters,
        timeout=timeout,
    )
    # Disabled unless explicitly configured: legacy lock-holding business
    # protocols need their own audit before enforcing a global idle deadline.
    timeouts = {
        name: value for name, value in (
            ("statement_timeout", getattr(settings, "DATABASE_STATEMENT_TIMEOUT_MS", 0)),
            ("lock_timeout", getattr(settings, "DATABASE_LOCK_TIMEOUT_MS", 0)),
            ("idle_in_transaction_session_timeout", getattr(settings, "DATABASE_IDLE_TRANSACTION_TIMEOUT_MS", 0)),
        ) if value > 0
    }
    runtime = DatabaseRuntime(
        kind, policy, getattr(settings, "DATABASE_HOLD_WARN_SECONDS", 5.0), timeouts,
        pool_mode, type(pool).__name__,
        admission_enabled=admission_enabled,
    )
    if not math.isfinite(runtime.warn_seconds) or runtime.warn_seconds <= 0 or any(
        getattr(settings, name, 0) < 0 for name in (
            "DATABASE_STATEMENT_TIMEOUT_MS", "DATABASE_LOCK_TIMEOUT_MS",
            "DATABASE_IDLE_TRANSACTION_TIMEOUT_MS",
        )
    ):
        raise ValueError("Database timeout values must be nonnegative and hold warning positive")
    logger.info(
        "database_runtime_admission_configured mode=%s pool_class=%s kind=%s "
        "admission_enabled=%s admission_limit=%s max_waiters=%s timeout_seconds=%s",
        runtime.pool_mode, runtime.pool_class, runtime.kind.value, runtime.admission_enabled,
        runtime.policy.limit if admission_enabled else None,
        runtime.policy.max_waiters if admission_enabled else None,
        runtime.policy.timeout if admission_enabled else None,
    )
    engine.sync_engine._manor_database_runtime = runtime
    event.listen(engine.sync_engine, "checkout", runtime.checkout)
    event.listen(engine.sync_engine, "checkin", runtime.checkin)
    event.listen(engine.sync_engine, "detach", runtime.checkin)
    event.listen(engine.sync_engine, "begin", runtime.begin)
    return runtime
