"""One runtime-owned language snapshot shared by Plan and Step notices."""

from __future__ import annotations

from sqlalchemy import case, cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.ai.response_language import normalize_response_locale
from packages.core.i18n import TRANSLATIONS
from packages.core.models.execution import ExecutionPlan
from packages.core.models.user import User


PLAN_RESPONSE_LOCALE_KEY = "_response_locale"


async def _task_execution_user_id(db, task) -> str | None:
    from packages.core.services.task_requester_identity import (
        TaskRequesterIdentityError,
        resolve_task_execution_user_id,
    )

    try:
        return await resolve_task_execution_user_id(db, task)
    except TaskRequesterIdentityError:
        # Notices can still finalize legacy/retired work. Identity
        # admission remains the execution runtime's responsibility.
        return None


async def _user_response_locale(db, user_id: str | None) -> str:
    return (
        normalize_response_locale(
            await db.scalar(select(User.locale).where(User.id == user_id))
        )
        if user_id
        else None
    ) or "en"


async def resolve_task_response_locale(db, task, *, user_id: str | None = None) -> str:
    """The plan-free counterpart for a Task with no Plan (Task agent loop).

    The Task's execution member's language, like a Plan's; nothing is cached
    because there is no Plan state to cache it in.
    """
    if not user_id:
        user_id = await _task_execution_user_id(db, task)
    return await _user_response_locale(db, user_id)


async def resolve_plan_response_locale(db, plan, *, user_id: str | None = None) -> str:
    """Freeze at execution time, after the runtime has resolved its actor.

    The DAG is model-authored and cannot supply this setting. Legacy Plans
    resolve the existing Task execution identity; taskless manual Plans use
    the server-written receipt. The first writer wins in the caller's ordinary
    transaction, without a second session or an independent commit.
    """
    state = dict(getattr(plan, "dispatcher_state", None) or {})
    cached = normalize_response_locale(state.get(PLAN_RESPONSE_LOCALE_KEY))
    if cached:
        return cached

    if not user_id and getattr(plan, "task_id", None):
        from packages.core.models.task import Task

        task = await db.get(Task, plan.task_id)
        if task is not None:
            user_id = await _task_execution_user_id(db, task)
    if not user_id and not getattr(plan, "task_id", None):
        from packages.core.services.task_execution_origin import MANUAL_EXECUTION_KEY

        receipt = state.get(MANUAL_EXECUTION_KEY)
        if isinstance(receipt, dict) and receipt.get("action"):
            user_id = receipt.get("user_id")
    locale = await _user_response_locale(db, user_id)
    # Another cycle or manual receipt writer may have updated this JSON after
    # the Plan was loaded. Merge only our key, and retain a concurrent winner's
    # locale instead of replacing the whole stale state. Flush the caller's
    # existing mutations before synchronizing the ORM's committed snapshot.
    await db.flush()
    stored_state = (await db.execute(
        update(ExecutionPlan)
        .where(ExecutionPlan.id == plan.id)
        .values(dispatcher_state=case(
            (
                ExecutionPlan.dispatcher_state[PLAN_RESPONSE_LOCALE_KEY].astext.in_(tuple(TRANSLATIONS)),
                ExecutionPlan.dispatcher_state,
            ),
            else_=func.coalesce(ExecutionPlan.dispatcher_state, cast({}, JSONB)).op("||")(
                cast({PLAN_RESPONSE_LOCALE_KEY: locale}, JSONB),
            ),
        ))
        .returning(ExecutionPlan.dispatcher_state)
        .execution_options(synchronize_session=False)
    )).scalar_one()
    set_committed_value(plan, "dispatcher_state", stored_state)
    return normalize_response_locale(stored_state.get(PLAN_RESPONSE_LOCALE_KEY)) or "en"
