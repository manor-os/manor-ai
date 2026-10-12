"""Agent Initiative API — proactive behaviour owned by an Agent deployment."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from packages.core.database import get_db
from packages.core.models.agent_initiative import AgentInitiative, AgentInitiativeRun
from packages.core.models.user import User
from packages.core.revisions import StaleRevisionError
from packages.core.schemas.agent_initiative import (
    AgentInitiativeActionRequest,
    AgentInitiativeConversationListResponse,
    AgentInitiativeCreateRequest,
    AgentInitiativeDeleteRequest,
    AgentInitiativeDeliveryResponse,
    AgentInitiativeResponse,
    AgentInitiativeRunListResponse,
    AgentInitiativeRunQueuedResponse,
    AgentInitiativeRunRequest,
    AgentInitiativeRunResponse,
    AgentInitiativeUpdateRequest,
)
from packages.core.services.agent_initiative_service import (
    AgentInitiativeServiceError,
    create_initiative,
    delete_initiative,
    get_initiative,
    get_initiative_run,
    initiative_contracts,
    initiative_driver,
    initiative_run_deliveries,
    initiative_state,
    list_conversation_initiatives,
    list_initiative_runs,
    list_initiatives,
    transition_initiative,
    update_initiative,
)


subscription_router = APIRouter(
    prefix="/api/v1/agent-subscriptions",
    tags=["agent-initiatives"],
)
initiative_router = APIRouter(
    prefix="/api/v1/agent-initiatives",
    tags=["agent-initiatives"],
)
run_router = APIRouter(
    prefix="/api/v1/agent-initiative-runs",
    tags=["agent-initiatives"],
)


def _raise_service_error(exc: AgentInitiativeServiceError) -> None:
    raise CodedError(
        exc.status_code,
        code=f"agent_initiative.error.{exc.code}",
        message=str(exc),
    ) from exc


def _raise_revision_error(exc: StaleRevisionError) -> None:
    raise CodedError(
        409,
        code="agent_initiative.error.initiative_revision_conflict",
        message="Initiative changed since it was loaded; refresh and try again",
        vars={"expected": exc.expected, "actual": exc.actual},
    ) from exc


def _delivery_response(row) -> AgentInitiativeDeliveryResponse:
    return AgentInitiativeDeliveryResponse(
        id=row.id,
        kind=row.kind,
        recipient_user_id=row.recipient_user_id,
        status=row.status,
        message_id=row.message_id,
        notification_id=row.notification_id,
        suppressed_reason=row.suppressed_reason,
        error=row.error,
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
    )


def _run_response(
    row: AgentInitiativeRun,
    deliveries: list | None = None,
) -> AgentInitiativeRunResponse:
    return AgentInitiativeRunResponse(
        id=row.id,
        initiative_id=row.initiative_id,
        scheduled_run_id=row.scheduled_run_id,
        task_id=row.task_id,
        occurrence_key=row.occurrence_key,
        trigger_type=row.trigger_type,
        status=row.status,
        candidate_decision=row.candidate_decision,
        final_decision=row.final_decision,
        reason_code=row.reason_code,
        reason=row.reason,
        message=row.message,
        error=row.error,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        deliveries=[_delivery_response(item) for item in (deliveries or [])],
    )


async def _initiative_response(
    db: AsyncSession,
    row: AgentInitiative,
    *,
    can_manage: bool,
) -> AgentInitiativeResponse:
    trigger, condition, decision, delivery, interruption = initiative_contracts(row)
    state = await initiative_state(db, row.id)
    driver = await initiative_driver(db, row.id)
    return AgentInitiativeResponse(
        id=row.id,
        entity_id=row.entity_id,
        workspace_id=row.workspace_id,
        agent_subscription_id=row.agent_subscription_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        instruction=row.instruction,
        trigger=trigger,
        condition=condition,
        decision_policy=decision,
        delivery_policy=delivery,
        interruption_policy=interruption,
        status=row.status,
        revision=row.revision,
        next_run_at=driver.next_run_at if driver else None,
        last_checked_at=state.last_checked_at if state else None,
        last_notified_at=state.last_notified_at if state else None,
        last_silent_reason=state.last_silent_reason if state else None,
        can_manage=can_manage,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@subscription_router.get(
    "/{subscription_id}/initiatives",
    response_model=list[AgentInitiativeResponse],
)
async def list_for_subscription(
    subscription_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows, can_manage = await list_initiatives(
            db,
            subscription_id=subscription_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
        )
        return [
            await _initiative_response(db, row, can_manage=can_manage)
            for row in rows
        ]
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@subscription_router.post(
    "/{subscription_id}/initiatives",
    response_model=AgentInitiativeResponse,
    status_code=201,
)
async def create_for_subscription(
    subscription_id: str,
    request: AgentInitiativeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await create_initiative(
            db,
            subscription_id=subscription_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
            name=request.name,
            instruction=request.instruction,
            trigger=request.trigger,
            condition=request.condition,
            decision_policy=request.decision_policy,
            delivery_policy=request.delivery_policy,
            interruption_policy=request.interruption_policy,
            status=request.status,
        )
        return await _initiative_response(db, row, can_manage=True)
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@initiative_router.get(
    "/conversation/{conversation_id}",
    response_model=AgentInitiativeConversationListResponse,
)
async def list_for_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return only Initiative rows durably targeting this exact Chat."""

    try:
        conversation, rows = await list_conversation_initiatives(
            db,
            conversation_id=conversation_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
        )
        return AgentInitiativeConversationListResponse(
            conversation={
                "id": conversation.id,
                "title": conversation.title,
                "workspace_id": conversation.workspace_id,
                "scope": conversation.scope,
            },
            items=[
                await _initiative_response(db, row, can_manage=can_manage)
                for row, can_manage in rows
            ],
        )
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@initiative_router.get("/{initiative_id}", response_model=AgentInitiativeResponse)
async def get_one(
    initiative_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row, access = await get_initiative(
            db,
            initiative_id=initiative_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
        )
        return await _initiative_response(db, row, can_manage=access.can_manage)
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@initiative_router.patch("/{initiative_id}", response_model=AgentInitiativeResponse)
async def update_one(
    initiative_id: str,
    request: AgentInitiativeUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await update_initiative(
            db,
            initiative_id=initiative_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
            expected_revision=request.expected_revision,
            patch=request.patch.model_dump(exclude_unset=True),
        )
        return await _initiative_response(db, row, can_manage=True)
    except StaleRevisionError as exc:
        _raise_revision_error(exc)
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


async def _action(
    initiative_id: str,
    request: AgentInitiativeActionRequest,
    user: User,
    db: AsyncSession,
    action: str,
) -> AgentInitiativeResponse:
    try:
        row = await transition_initiative(
            db,
            initiative_id=initiative_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
            expected_revision=request.expected_revision,
            action=action,
        )
        return await _initiative_response(db, row, can_manage=True)
    except StaleRevisionError as exc:
        _raise_revision_error(exc)
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@initiative_router.post("/{initiative_id}/mute", response_model=AgentInitiativeResponse)
async def mute_one(initiative_id: str, request: AgentInitiativeActionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _action(initiative_id, request, user, db, "mute")


@initiative_router.post("/{initiative_id}/unmute", response_model=AgentInitiativeResponse)
async def unmute_one(initiative_id: str, request: AgentInitiativeActionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _action(initiative_id, request, user, db, "unmute")


@initiative_router.post("/{initiative_id}/pause", response_model=AgentInitiativeResponse)
async def pause_one(initiative_id: str, request: AgentInitiativeActionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _action(initiative_id, request, user, db, "pause")


@initiative_router.post("/{initiative_id}/resume", response_model=AgentInitiativeResponse)
async def resume_one(initiative_id: str, request: AgentInitiativeActionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _action(initiative_id, request, user, db, "resume")


@initiative_router.delete("/{initiative_id}", status_code=204)
async def delete_one(
    initiative_id: str,
    request: AgentInitiativeDeleteRequest = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await delete_initiative(
            db,
            initiative_id=initiative_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
            expected_revision=request.expected_revision,
        )
    except StaleRevisionError as exc:
        _raise_revision_error(exc)
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@initiative_router.post(
    "/{initiative_id}/run",
    response_model=AgentInitiativeRunQueuedResponse,
    status_code=202,
)
async def run_now(
    initiative_id: str,
    request: AgentInitiativeRunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        initiative, _ = await get_initiative(
            db,
            initiative_id=initiative_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
            manage=True,
        )
        paused_confirmed = initiative.status == "paused" and request.confirm_cost
        if initiative.status not in {"active", "muted"} and not paused_confirmed:
            raise AgentInitiativeServiceError(
                "initiative_paused",
                "Resume the Initiative or explicitly confirm a one-off paid run",
                status_code=409,
            )
        job = await initiative_driver(db, initiative.id)
        workspace_paused = False
        if initiative.workspace_id:
            from sqlalchemy import select
            from packages.core.models.workspace import Workspace

            workspace_paused = (await db.execute(select(Workspace.id).where(
                Workspace.id == initiative.workspace_id,
                Workspace.entity_id == user.entity_id,
                Workspace.status == "paused",
                Workspace.deleted_at.is_(None),
            ))).scalar_one_or_none() is not None
        if job is None or (not job.enabled and not (paused_confirmed or workspace_paused)):
            raise AgentInitiativeServiceError(
                "initiative_driver_unavailable",
                "Initiative scheduler driver is unavailable",
                status_code=409,
            )
        request_key = request.idempotency_key
        occurrence_key = f"manual:{request_key}"
        initiative_id_value = initiative.id
        job_id_value = job.id
        requesting_user_id = user.id
        await db.rollback()
        from packages.core.tasks.scheduler_tasks import dispatch_agent_initiative_now

        dispatch = await dispatch_agent_initiative_now(
            job_db_id=job_id_value,
            initiative_id=initiative_id_value,
            occurrence_key=occurrence_key,
            trigger_detail="owner_confirmed_paused" if paused_confirmed else None,
            requesting_user_id=requesting_user_id,
        )
        if dispatch is None:
            raise AgentInitiativeServiceError(
                "initiative_driver_unavailable",
                "Initiative scheduler driver is unavailable",
                status_code=409,
            )
        return {
            "initiative_id": initiative_id_value,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "idempotency_key": request_key,
            "scheduled_run_id": dispatch.run_id,
            "published": dispatch.published,
        }
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@initiative_router.get(
    "/{initiative_id}/runs",
    response_model=AgentInitiativeRunListResponse,
)
async def runs(
    initiative_id: str,
    before_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows = await list_initiative_runs(
            db,
            initiative_id=initiative_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
            before_id=before_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        deliveries = await initiative_run_deliveries(db, [row.id for row in rows])
        return AgentInitiativeRunListResponse(
            items=[_run_response(row, deliveries.get(row.id)) for row in rows],
            next_cursor=rows[-1].id if has_more and rows else None,
        )
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


@run_router.get("/{run_id}", response_model=AgentInitiativeRunResponse)
async def run_detail(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await get_initiative_run(
            db,
            run_id=run_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_role=user.role,
        )
        deliveries = await initiative_run_deliveries(db, [row.id])
        return _run_response(row, deliveries.get(row.id))
    except AgentInitiativeServiceError as exc:
        _raise_service_error(exc)


__all__ = ["initiative_router", "run_router", "subscription_router"]
