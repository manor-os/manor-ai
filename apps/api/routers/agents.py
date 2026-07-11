"""Agent endpoints — CRUD, subscriptions, tool bindings."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.skill import AgentSkillBinding
from packages.core.models.user import User
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import AgentSubscription, AgentToolBinding, Workspace
from packages.core.services.agent_service import (
    list_agents, get_agent, create_agent,
    update_agent, delete_agent, subscribe_agent, list_subscriptions,
    unsubscribe_agent, bind_tools, unbind_tools, get_agent_tools,
    list_tool_definitions,
)
from packages.core.services.agent_prompt_preview import preview_agent_prompt
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


class AgentResponse(BaseModel):
    id: str
    entity_id: str | None = None
    name: str
    slug: str | None = None
    description: str | None = None
    description_i18n: dict[str, str] = Field(default_factory=dict)
    avatar_url: str | None = None
    system_prompt: str | None = None
    config: dict = {}
    is_template: bool = False
    is_public: bool = False
    category: str | None = None
    tags: list[str] = []
    source: str = "custom"
    status: str = "active"
    tool_count: int = 0
    skill_count: int = 0


class AgentCreateRequest(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    avatar_url: str = ""
    category: str = ""
    tags: list[str] = []
    config: dict = {}
    source: str = "custom"


class PromptPreviewRequest(BaseModel):
    system_prompt: str
    test_message: str


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    avatar_url: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    config: dict | None = None


class SubscriptionResponse(BaseModel):
    id: str
    entity_id: str
    agent_id: str
    workspace_id: str | None = None
    custom_prompt: str | None = None
    status: str = "active"


class WorkerSummaryResponse(BaseModel):
    id: str
    kind: str
    display_name: str
    description: str | None = None
    version: str | None = None
    capabilities: dict = Field(default_factory=dict)
    trust_level: str
    status: str
    last_heartbeat_at: datetime | None = None
    monthly_budget_usd: float | None = None
    monthly_spent_usd: float = 0
    expires_at: datetime | None = None


class SubscriptionWorkerResponse(BaseModel):
    subscription_id: str
    worker_id: str
    priority: int = 100
    is_preferred: bool = False
    created_at: datetime | None = None
    worker: WorkerSummaryResponse | None = None


class SubscriptionWorkerBindRequest(BaseModel):
    worker_id: str
    priority: int = Field(default=100, ge=0, le=1000)
    is_preferred: bool = False


class AgentDeploymentResponse(BaseModel):
    id: str
    entity_id: str
    agent_id: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    workspace_status: str | None = None
    service_key: str | None = None
    custom_prompt: str | None = None
    config: dict = Field(default_factory=dict)
    status: str
    workers: list[SubscriptionWorkerResponse] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SubscribeRequest(BaseModel):
    agent_id: str
    workspace_id: str | None = None
    custom_prompt: str = ""


class ToolBindRequest(BaseModel):
    tool_ids: list[str]


class ToolResponse(BaseModel):
    id: str
    name: str
    display_name: str | None = None
    description: str | None = None
    category: str | None = None
    status: str = "active"


def _agent_resp(a, tool_count: int = 0, skill_count: int = 0) -> AgentResponse:
    config = a.config or {}
    description_i18n = (
        config.get("description_i18n")
        if isinstance(config.get("description_i18n"), dict)
        else {}
    )
    return AgentResponse(
        id=a.id, entity_id=a.entity_id, name=a.name, slug=a.slug,
        description=a.description, description_i18n=description_i18n,
        avatar_url=a.avatar_url,
        system_prompt=a.system_prompt, config=config,
        is_template=a.is_template, is_public=a.is_public,
        category=a.category, tags=a.tags or [], source=a.source, status=a.status,
        tool_count=tool_count,
        skill_count=skill_count,
    )


def _agent_draft_resp(draft: dict, entity_id: str) -> AgentResponse:
    return AgentResponse(
        id="",
        entity_id=entity_id,
        name=str(draft.get("name") or "New Agent"),
        description=str(draft.get("description") or "") or None,
        avatar_url=None,
        system_prompt=str(draft.get("system_prompt") or "") or None,
        config={},
        is_template=False,
        is_public=False,
        category=str(draft.get("category") or "") or None,
        tags=draft.get("tags") if isinstance(draft.get("tags"), list) else [],
        source="llm-generated",
        status="draft",
        tool_count=0,
        skill_count=0,
    )


def _worker_resp(w: Worker) -> WorkerSummaryResponse:
    return WorkerSummaryResponse(
        id=w.id,
        kind=w.kind,
        display_name=w.display_name,
        description=w.description,
        version=w.version,
        capabilities=w.capabilities or {},
        trust_level=w.trust_level,
        status=w.status,
        last_heartbeat_at=w.last_heartbeat_at,
        monthly_budget_usd=float(w.monthly_budget_usd) if w.monthly_budget_usd is not None else None,
        monthly_spent_usd=float(w.monthly_spent_usd or 0),
        expires_at=w.expires_at,
    )


def _sub_worker_resp(row: SubscriptionWorker, worker: Worker | None) -> SubscriptionWorkerResponse:
    return SubscriptionWorkerResponse(
        subscription_id=row.subscription_id,
        worker_id=row.worker_id,
        priority=row.priority,
        is_preferred=row.is_preferred,
        created_at=row.created_at,
        worker=_worker_resp(worker) if worker else None,
    )


def _user_visible_worker_scope(stmt, user: User):
    return stmt.where(
        Worker.entity_id == user.entity_id,
        (Worker.kind != "custom_http") | (Worker.created_by_user_id == user.id),
    )


async def _owned_subscription(
    db: AsyncSession,
    subscription_id: str,
    entity_id: str,
) -> AgentSubscription:
    sub = (
        await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.id == subscription_id,
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "Subscription not found")
    return sub


async def _subscription_workers(
    db: AsyncSession,
    subscription_ids: list[str],
    user: User | None = None,
) -> dict[str, list[SubscriptionWorkerResponse]]:
    if not subscription_ids:
        return {}
    stmt = (
        select(SubscriptionWorker, Worker)
        .join(Worker, Worker.id == SubscriptionWorker.worker_id)
        .where(SubscriptionWorker.subscription_id.in_(subscription_ids))
        .order_by(SubscriptionWorker.is_preferred.desc(), SubscriptionWorker.priority.asc())
    )
    if user is not None:
        stmt = _user_visible_worker_scope(stmt, user)
    rows = (await db.execute(stmt)).all()
    by_subscription: dict[str, list[SubscriptionWorkerResponse]] = {}
    for binding, worker in rows:
        by_subscription.setdefault(binding.subscription_id, []).append(
            _sub_worker_resp(binding, worker)
        )
    return by_subscription


async def _agent_binding_counts(
    db: AsyncSession,
    agent_ids: list[str],
) -> tuple[dict[str, int], dict[str, int]]:
    if not agent_ids:
        return {}, {}

    tools_result = await db.execute(
        select(AgentToolBinding.agent_id, func.count().label("cnt"))
        .where(AgentToolBinding.agent_id.in_(agent_ids))
        .group_by(AgentToolBinding.agent_id)
    )
    tool_counts: dict[str, int] = {
        str(row.agent_id): int(row.cnt)
        for row in tools_result
    }

    skills_result = await db.execute(
        select(AgentSkillBinding.agent_id, func.count().label("cnt"))
        .where(
            AgentSkillBinding.agent_id.in_(agent_ids),
            AgentSkillBinding.status == "active",
        )
        .group_by(AgentSkillBinding.agent_id)
    )
    skill_counts: dict[str, int] = {
        str(row.agent_id): int(row.cnt)
        for row in skills_result
    }
    return tool_counts, skill_counts


# ── CRUD ──

@router.get("", response_model=list[AgentResponse])
async def list_my_agents(
    include_templates: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agents = await list_agents(db, user.entity_id, include_templates=include_templates)
    agent_ids = [a.id for a in agents]
    tool_counts, skill_counts = await _agent_binding_counts(db, agent_ids)
    return [
        _agent_resp(
            a,
            tool_count=tool_counts.get(a.id, 0),
            skill_count=skill_counts.get(a.id, 0),
        )
        for a in agents
    ]


@router.post("", response_model=AgentResponse, status_code=201)
async def create_new_agent(
    req: AgentCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await create_agent(
        db, user.entity_id,
        name=req.name, description=req.description,
        system_prompt=req.system_prompt, avatar_url=req.avatar_url,
        category=req.category, tags=req.tags,
        config=req.config, source=req.source,
    )
    return _agent_resp(agent)


class AgentGenerateRequest(BaseModel):
    prompt: str


class AgentDraftQuestionsResponse(BaseModel):
    questions: list[str]
    ready: bool


@router.post("/draft-questions", response_model=AgentDraftQuestionsResponse)
async def draft_agent_questions(
    body: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clarifying questions for an agent request — powers conversational create.

    ``ready=true`` / empty list means the request is specific enough to build.
    Clarification is a nicety, so any failure degrades to ``ready``.
    """
    from packages.core.services.agent_generator import agent_clarifying_questions

    try:
        questions = await agent_clarifying_questions(body.prompt, entity_id=user.entity_id)
    except Exception:
        questions = []
    return AgentDraftQuestionsResponse(questions=questions, ready=not questions)


@router.post("/generate", response_model=AgentResponse, status_code=201)
async def generate_new_agent(
    body: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI-generate an agent (name, persona/system_prompt, ...) from a prompt."""
    from packages.core.services.agent_generator import generate_agent

    try:
        agent = await generate_agent(body.prompt, user.entity_id, db)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _agent_resp(agent)


@router.post("/generate-stream")
async def generate_new_agent_stream(
    body: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI-generate an agent, streaming progress as Server-Sent Events.

    Like the skill stream: emitting ``step`` frames keeps the connection alive
    past Cloudflare's 100s 524 timeout and narrates the build. Emits ``step``
    events, then a terminal ``done`` (the agent) or ``error`` frame.
    """
    from fastapi.responses import StreamingResponse

    from packages.core.services.agent_generator import generate_agent_streaming
    from packages.core.services.sse_events import format_sse

    async def event_stream():
        try:
            agent = None
            async for kind, payload in generate_agent_streaming(body.prompt, user.entity_id, db):
                if kind == "step":
                    yield format_sse("step", {"label": payload})
                elif kind == "agent":
                    agent = payload
            if agent is None:
                yield format_sse("error", {"message": "Agent generation did not produce an agent"})
                return
            yield format_sse("done", _agent_resp(agent).model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001 — surface any failure to the client
            yield format_sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/generate-draft-stream")
async def generate_agent_draft_stream(
    body: AgentGenerateRequest,
    user: User = Depends(get_current_user),
):
    """AI-generate an agent draft, streaming progress without persisting it."""
    from fastapi.responses import StreamingResponse

    from packages.core.services.agent_generator import generate_agent_draft_streaming
    from packages.core.services.sse_events import format_sse

    async def event_stream():
        try:
            draft = None
            async for kind, payload in generate_agent_draft_streaming(body.prompt, user.entity_id):
                if kind == "step":
                    yield format_sse("step", {"label": payload})
                elif kind == "draft":
                    draft = payload
            if draft is None:
                yield format_sse("error", {"message": "Agent generation did not produce a draft"})
                return
            yield format_sse("done", _agent_draft_resp(draft, user.entity_id).model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001 — surface any failure to the client
            yield format_sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{agent_id}/ai-update", response_model=AgentResponse)
async def ai_update_agent_endpoint(
    agent_id: str,
    body: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply a natural-language change to an existing agent."""
    from packages.core.services.agent_generator import update_agent_via_ai

    try:
        agent = await update_agent_via_ai(agent_id, body.prompt, user.entity_id, db)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _agent_resp(agent)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_one_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await get_agent(db, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    # Allow access if: own entity, or public template
    if agent.entity_id and agent.entity_id != user.entity_id:
        if not (agent.is_template and agent.is_public):
            raise HTTPException(404, "Agent not found")
    tool_counts, skill_counts = await _agent_binding_counts(db, [agent.id])
    return _agent_resp(
        agent,
        tool_count=tool_counts.get(agent.id, 0),
        skill_count=skill_counts.get(agent.id, 0),
    )


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_one_agent(
    agent_id: str,
    req: AgentUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await update_agent(db, agent_id, user.entity_id, **req.model_dump(exclude_none=True))
    if not agent:
        raise HTTPException(404, "Agent not found")
    tool_counts, skill_counts = await _agent_binding_counts(db, [agent.id])
    return _agent_resp(
        agent,
        tool_count=tool_counts.get(agent.id, 0),
        skill_count=skill_counts.get(agent.id, 0),
    )


@router.delete("/{agent_id}", status_code=204)
async def delete_one_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_agent(db, agent_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Agent not found")


# ── Prompt preview / improve ──

@router.post("/preview")
async def preview_prompt(
    req: PromptPreviewRequest,
    user: User = Depends(get_current_user),
):
    """Test or improve an agent prompt using the configured LLM."""
    try:
        content = await preview_agent_prompt(
            entity_id=user.entity_id,
            system_prompt=req.system_prompt,
            test_message=req.test_message,
        )
        return {"response": content}
    except Exception as e:
        raise HTTPException(500, f"LLM call failed: {e}")


# ── Subscriptions ──

@router.get("/subscriptions/mine", response_model=list[SubscriptionResponse])
async def my_subscriptions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    subs = await list_subscriptions(db, user.entity_id)
    return [SubscriptionResponse(
        id=s.id, entity_id=s.entity_id, agent_id=s.agent_id,
        workspace_id=s.workspace_id, custom_prompt=s.custom_prompt, status=s.status,
    ) for s in subs]


@router.post("/subscriptions", response_model=SubscriptionResponse, status_code=201)
async def subscribe(
    req: SubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sub = await subscribe_agent(
        db, user.entity_id, req.agent_id,
        workspace_id=req.workspace_id, custom_prompt=req.custom_prompt,
    )
    return SubscriptionResponse(
        id=sub.id, entity_id=sub.entity_id, agent_id=sub.agent_id,
        workspace_id=sub.workspace_id, custom_prompt=sub.custom_prompt, status=sub.status,
    )


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def unsubscribe(
    subscription_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await unsubscribe_agent(db, subscription_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Subscription not found")


@router.get("/subscriptions/{subscription_id}/workers", response_model=list[SubscriptionWorkerResponse])
async def list_subscription_workers_endpoint(
    subscription_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _owned_subscription(db, subscription_id, user.entity_id)
    return (await _subscription_workers(db, [subscription_id], user=user)).get(subscription_id, [])


@router.post("/subscriptions/{subscription_id}/workers", response_model=SubscriptionWorkerResponse, status_code=201)
async def bind_subscription_worker_endpoint(
    subscription_id: str,
    req: SubscriptionWorkerBindRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _owned_subscription(db, subscription_id, user.entity_id)
    worker = (
        await db.execute(
            _user_visible_worker_scope(select(Worker), user).where(
                Worker.id == req.worker_id,
                Worker.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if not worker:
        raise HTTPException(404, "Worker not found")

    existing_rows = (
        await db.execute(
            select(SubscriptionWorker).where(
                SubscriptionWorker.subscription_id == subscription_id,
            )
        )
    ).scalars().all()
    if req.is_preferred:
        for existing in existing_rows:
            existing.is_preferred = False

    binding = next((row for row in existing_rows if row.worker_id == req.worker_id), None)
    if binding:
        binding.priority = req.priority
        binding.is_preferred = req.is_preferred
    else:
        binding = SubscriptionWorker(
            subscription_id=subscription_id,
            worker_id=req.worker_id,
            priority=req.priority,
            is_preferred=req.is_preferred,
        )
        db.add(binding)

    await db.flush()
    response = _sub_worker_resp(binding, worker)
    await db.commit()
    return response


@router.delete("/subscriptions/{subscription_id}/workers/{worker_id}", status_code=204)
async def unbind_subscription_worker_endpoint(
    subscription_id: str,
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _owned_subscription(db, subscription_id, user.entity_id)
    binding = (
        await db.execute(
            select(SubscriptionWorker)
            .join(Worker, Worker.id == SubscriptionWorker.worker_id)
            .where(
                SubscriptionWorker.subscription_id == subscription_id,
                SubscriptionWorker.worker_id == worker_id,
            )
        )
    ).scalar_one_or_none()
    if binding is not None:
        visible_worker = (
            await db.execute(
                _user_visible_worker_scope(select(Worker.id), user).where(Worker.id == worker_id)
            )
        ).scalar_one_or_none()
        if visible_worker is None:
            binding = None
    if not binding:
        raise HTTPException(404, "Worker binding not found")
    await db.delete(binding)
    await db.commit()


@router.get("/{agent_id}/deployments", response_model=list[AgentDeploymentResponse])
async def agent_deployments(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await get_agent(db, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent.entity_id and agent.entity_id != user.entity_id:
        if not (agent.is_template and agent.is_public):
            raise HTTPException(404, "Agent not found")

    rows = (
        await db.execute(
            select(AgentSubscription, Workspace)
            .join(Workspace, Workspace.id == AgentSubscription.workspace_id, isouter=True)
            .where(
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.agent_id == agent_id,
                AgentSubscription.status == "active",
            )
            .order_by(AgentSubscription.created_at.desc())
        )
    ).all()
    sub_ids = [sub.id for sub, _workspace in rows]
    workers_by_sub = await _subscription_workers(db, sub_ids, user=user)

    return [
        AgentDeploymentResponse(
            id=sub.id,
            entity_id=sub.entity_id,
            agent_id=sub.agent_id,
            workspace_id=sub.workspace_id,
            workspace_name=workspace.name if workspace else None,
            workspace_status=workspace.status if workspace else None,
            service_key=sub.service_key,
            custom_prompt=sub.custom_prompt,
            config=sub.config or {},
            status=sub.status,
            workers=workers_by_sub.get(sub.id, []),
            created_at=sub.created_at,
            updated_at=sub.updated_at,
        )
        for sub, workspace in rows
    ]


# ── Tool bindings ──

@router.get("/{agent_id}/tools", response_model=list[ToolResponse])
async def agent_tools(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tools = await get_agent_tools(db, agent_id)
    return [ToolResponse(id=t.id, name=t.name, display_name=t.display_name, description=t.description, category=t.category) for t in tools]


@router.post("/{agent_id}/tools", status_code=200)
async def bind_agent_tools(
    agent_id: str,
    req: ToolBindRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await bind_tools(db, agent_id, req.tool_ids)
    return {"bound": count}


@router.delete("/{agent_id}/tools", status_code=200)
async def unbind_agent_tools(
    agent_id: str,
    req: ToolBindRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await unbind_tools(db, agent_id, req.tool_ids)
    return {"unbound": count}


# ── Tool definitions ──

@router.get("/tools/catalog", response_model=list[ToolResponse])
async def tool_catalog(
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    tools = await list_tool_definitions(db, include_inactive=include_inactive)
    return [
        ToolResponse(
            id=t.id,
            name=t.name,
            display_name=t.display_name,
            description=t.description,
            category=t.category,
            status=t.status,
        )
        for t in tools
    ]


@router.get("/tools/all", response_model=list[ToolResponse])
async def all_tools_for_agent_create(db: AsyncSession = Depends(get_db)):
    tools = await list_tool_definitions(db, include_inactive=True)
    return [
        ToolResponse(
            id=t.id,
            name=t.name,
            display_name=t.display_name,
            description=t.description,
            category=t.category,
            status=t.status,
        )
        for t in tools
    ]
