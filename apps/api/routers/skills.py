"""Skill endpoints — CRUD, invocation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import runtime_invoke_skill
from packages.core.database import get_db
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import User
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.skill_service import (
    list_skills, get_skill, create_skill,
    update_skill, delete_skill,
    list_agent_skill_bindings, list_available_skills_for_agent,
    bind_skill_to_agent, unbind_skill_from_agent,
)
from packages.core.services.skill_generator import (
    generate_skill as ai_generate_skill,
    generate_skill_streaming as ai_generate_skill_streaming,
    update_skill as ai_update_skill,
)
from packages.core.services.sse_events import format_sse
from packages.core.services.github_skill_installer import install_from_github
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


# ── Schemas ──

class SkillResponse(BaseModel):
    id: str
    entity_id: str | None = None
    name: str
    slug: str | None = None
    display_name: str | None = None
    description: str | None = None
    system_prompt: str
    tools: list[str] = []
    input_schema: dict = {}
    output_format: str = "text"
    category: str | None = None
    tags: list[str] = []
    is_public: bool = False
    version: str = "1.0.0"
    config: dict = {}
    status: str = "active"
    env_vars: list[dict] = []
    credentials_configured: bool = False
    type: str = ""
    scripts: dict[str, str] = {}
    requirements: str = ""
    usage_summary: str = ""
    example_scenarios: list[str] = []
    examples: list[dict] = []
    bindings: list[dict] = []


class SkillCreateRequest(BaseModel):
    name: str
    system_prompt: str
    slug: str = ""
    display_name: str = ""
    description: str = ""
    tools: list[str] = []
    input_schema: dict = {}
    output_format: str = "text"
    category: str = ""
    tags: list[str] = []
    is_public: bool = False
    version: str = "1.0.0"
    config: dict = {}
    type: str = ""
    scripts: dict[str, str] = {}
    requirements: str = ""


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    slug: str | None = None
    display_name: str | None = None
    description: str | None = None
    tools: list[str] | None = None
    input_schema: dict | None = None
    output_format: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    is_public: bool | None = None
    version: str | None = None
    config: dict | None = None
    type: str | None = None
    scripts: dict[str, str] | None = None
    requirements: str | None = None


class SkillGenerateRequest(BaseModel):
    prompt: str
    category: str | None = None


class GitHubInstallRequest(BaseModel):
    github_url: str


class SkillAIUpdateRequest(BaseModel):
    prompt: str


class InvokeRequest(BaseModel):
    # Accept either a free-form string OR a structured dict matching the
    # skill's input_schema. Dicts are JSON-serialized before being passed
    # to the LLM as the user message — most skills' system prompts are
    # written to expect JSON anyway.
    input: str | dict


class InvokeResponse(BaseModel):
    skill: str
    content: str
    usage: dict = {}
    tools_used: list = []
    rounds: int = 0
    stop_reason: str = ""


class BatchImportSkillItem(BaseModel):
    name: str
    prompt: str
    description: str = ""
    tags: list[str] = []
    is_public: bool = False
    version: str = "1.0.0"
    id: str | None = None


class BatchImportRequest(BaseModel):
    skills: list[BatchImportSkillItem]


class BatchImportResponse(BaseModel):
    imported: int
    skipped: int
    failed: int


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _clean_dict(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if v not in (None, "", [], {})}


def _to_response(skill, *, bindings: list[dict] | None = None) -> dict:
    cfg = skill.config or {}
    env_vars = cfg.get("env_vars") or []
    credentials_configured = bool(cfg.get("credentials_configured", False))
    return {
        "id": skill.id,
        "entity_id": skill.entity_id,
        "name": skill.name,
        "slug": skill.slug,
        "display_name": skill.display_name,
        "description": skill.description,
        "system_prompt": skill.system_prompt,
        "tools": skill.tools or [],
        "input_schema": skill.input_schema or {},
        "output_format": skill.output_format or "text",
        "category": skill.category,
        "tags": skill.tags or [],
        "is_public": skill.is_public,
        "version": skill.version or "1.0.0",
        "config": cfg,
        "status": skill.status,
        "env_vars": env_vars,
        "credentials_configured": credentials_configured,
        "type": cfg.get("type") or "",
        "scripts": cfg.get("scripts") or {},
        "requirements": cfg.get("requirements") or "",
        "usage_summary": cfg.get("usage_summary") or "",
        "example_scenarios": cfg.get("example_scenarios") or [],
        "examples": cfg.get("examples") or [],
        "bindings": bindings or [],
    }


async def _binding_contexts_for_skills(
    db: AsyncSession,
    skills: list[Skill],
    *,
    entity_id: str,
) -> dict[str, list[dict]]:
    """Return display-ready provenance for agent/workspace/automation skill use."""
    skill_ids = [skill.id for skill in skills if getattr(skill, "id", None)]
    contexts: dict[str, list[dict]] = {skill_id: [] for skill_id in skill_ids}
    if not skill_ids:
        return contexts

    rows = (await db.execute(
        select(AgentSkillBinding, Agent)
        .join(Agent, Agent.id == AgentSkillBinding.agent_id)
        .where(
            AgentSkillBinding.skill_id.in_(skill_ids),
            AgentSkillBinding.status == "active",
            Agent.entity_id == entity_id,
            Agent.status == "active",
        )
    )).all()

    agent_ids = {agent.id for _binding, agent in rows if agent and agent.id}
    automation_agent_ids = {
        str((skill.config or {}).get("agent_id"))
        for skill in skills
        if (skill.config or {}).get("agent_id")
    }
    all_agent_ids = {agent_id for agent_id in (agent_ids | automation_agent_ids) if agent_id}
    agent_by_id: dict[str, Agent] = {
        agent.id: agent
        for _binding, agent in rows
        if agent and agent.id
    }
    if automation_agent_ids - set(agent_by_id):
        extra_agents = (await db.execute(
            select(Agent).where(
                Agent.id.in_(automation_agent_ids - set(agent_by_id)),
                Agent.entity_id == entity_id,
            )
        )).scalars().all()
        agent_by_id.update({agent.id: agent for agent in extra_agents})

    subscription_rows = []
    if all_agent_ids:
        subscription_rows = (await db.execute(
            select(AgentSubscription, Workspace)
            .join(Workspace, Workspace.id == AgentSubscription.workspace_id)
            .where(
                AgentSubscription.agent_id.in_(all_agent_ids),
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.status == "active",
                Workspace.entity_id == entity_id,
                Workspace.status == "active",
            )
        )).all()
    subscriptions_by_agent: dict[str, list[tuple[AgentSubscription, Workspace]]] = {}
    workspace_by_id: dict[str, Workspace] = {}
    for subscription, workspace in subscription_rows:
        subscriptions_by_agent.setdefault(subscription.agent_id, []).append((subscription, workspace))
        workspace_by_id[workspace.id] = workspace

    config_workspace_ids = {
        str(value)
        for binding, _agent in rows
        for raw in _binding_config_contexts(binding.config)
        for value in [raw.get("workspace_id")]
        if value
    } | {
        str((skill.config or {}).get("workspace_id"))
        for skill in skills
        if (skill.config or {}).get("workspace_id")
    }
    missing_workspace_ids = config_workspace_ids - set(workspace_by_id)
    if missing_workspace_ids:
        extra_workspaces = (await db.execute(
            select(Workspace).where(
                Workspace.id.in_(missing_workspace_ids),
                Workspace.entity_id == entity_id,
            )
        )).scalars().all()
        workspace_by_id.update({workspace.id: workspace for workspace in extra_workspaces})

    job_refs = {
        str(value)
        for skill in skills
        for value in [
            (skill.config or {}).get("scheduled_job_id"),
            (skill.config or {}).get("scheduled_job_pk"),
        ]
        if value
    }
    jobs_by_ref: dict[str, ScheduledJob] = {}
    if job_refs:
        jobs = (await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.entity_id == entity_id,
                or_(ScheduledJob.job_id.in_(job_refs), ScheduledJob.id.in_(job_refs)),
            )
        )).scalars().all()
        for job in jobs:
            jobs_by_ref[job.job_id] = job
            jobs_by_ref[job.id] = job

    for binding, agent in rows:
        raw_contexts = _binding_config_contexts(binding.config)
        if not raw_contexts:
            raw_contexts = [{}]
        for raw_context in raw_contexts:
            context = _display_binding_context(
                binding=binding,
                agent=agent,
                raw_context=raw_context,
                workspace_by_id=workspace_by_id,
            )
            contexts.setdefault(binding.skill_id, []).append(context)

        if not any(raw.get("workspace_id") for raw in raw_contexts):
            for subscription, workspace in subscriptions_by_agent.get(agent.id, [])[:5]:
                contexts.setdefault(binding.skill_id, []).append(_clean_dict({
                    "binding_id": binding.id,
                    "binding_type": "agent_skill_binding",
                    "source": "agent_subscription",
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "workspace_id": workspace.id,
                    "workspace_name": workspace.name,
                    "service_key": subscription.service_key,
                    "created_at": _iso(binding.created_at),
                }))

    for skill in skills:
        automation_context = _skill_automation_context(
            skill,
            agent_by_id=agent_by_id,
            workspace_by_id=workspace_by_id,
            jobs_by_ref=jobs_by_ref,
        )
        if automation_context:
            contexts.setdefault(skill.id, []).append(automation_context)

    return {skill_id: _dedupe_contexts(items) for skill_id, items in contexts.items()}


def _binding_config_contexts(config: dict | None) -> list[dict]:
    if not isinstance(config, dict):
        return []
    raw_contexts = config.get("contexts")
    if isinstance(raw_contexts, list):
        return [dict(item) for item in raw_contexts if isinstance(item, dict)]
    if isinstance(raw_contexts, dict):
        return [dict(raw_contexts)]
    return [dict(config)] if config else []


def _display_binding_context(
    *,
    binding: AgentSkillBinding,
    agent: Agent,
    raw_context: dict,
    workspace_by_id: dict[str, Workspace],
) -> dict:
    workspace_id = str(raw_context.get("workspace_id") or "") or None
    workspace = workspace_by_id.get(workspace_id or "")
    return _clean_dict({
        "binding_id": binding.id,
        "binding_type": raw_context.get("binding_type") or "agent_skill_binding",
        "source": raw_context.get("source") or "agent_skill_binding",
        "agent_id": agent.id,
        "agent_name": raw_context.get("agent_name") or agent.name,
        "workspace_id": workspace_id,
        "workspace_name": raw_context.get("workspace_name") or (workspace.name if workspace else None),
        "service_key": raw_context.get("service_key"),
        "automation_id": raw_context.get("automation_id"),
        "automation_name": raw_context.get("automation_name"),
        "match": raw_context.get("match"),
        "requested_skill": raw_context.get("requested_skill"),
        "created_at": _iso(binding.created_at),
    })


def _skill_automation_context(
    skill: Skill,
    *,
    agent_by_id: dict[str, Agent],
    workspace_by_id: dict[str, Workspace],
    jobs_by_ref: dict[str, ScheduledJob],
) -> dict | None:
    cfg = skill.config or {}
    job_ref = str(cfg.get("scheduled_job_id") or cfg.get("scheduled_job_pk") or "")
    if cfg.get("source") != "scheduled_job" and not job_ref:
        return None
    job = jobs_by_ref.get(job_ref)
    agent_id = str(cfg.get("agent_id") or (job.agent_id if job else "") or "")
    workspace_id = str(cfg.get("workspace_id") or (job.workspace_id if job else "") or "")
    agent = agent_by_id.get(agent_id)
    workspace = workspace_by_id.get(workspace_id)
    return _clean_dict({
        "binding_type": "automation_skill",
        "source": "scheduled_job",
        "automation_id": cfg.get("scheduled_job_id") or (job.job_id if job else None),
        "automation_pk": cfg.get("scheduled_job_pk") or (job.id if job else None),
        "automation_name": cfg.get("automation_name") or (job.name if job else None),
        "agent_id": agent_id,
        "agent_name": agent.name if agent else None,
        "workspace_id": workspace_id,
        "workspace_name": workspace.name if workspace else None,
        "match": {"type": cfg.get("generation_source") or "automation_generated"},
    })


def _dedupe_contexts(items: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in items:
        key = "|".join(str(item.get(part) or "") for part in (
            "binding_id",
            "source",
            "agent_id",
            "workspace_id",
            "automation_id",
            "service_key",
        ))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


# ── Endpoints ──

@router.get("", response_model=list[SkillResponse])
async def list_skills_endpoint(
    category: str = Query(None),
    include_platform: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List skills for the current entity + platform skills."""
    skills = await list_skills(db, user.entity_id, category=category)
    if not include_platform:
        skills = [s for s in skills if s.entity_id == user.entity_id]
    bindings = await _binding_contexts_for_skills(db, list(skills), entity_id=user.entity_id)
    return [_to_response(s, bindings=bindings.get(s.id, [])) for s in skills]


@router.post("", response_model=SkillResponse, status_code=201)
async def create_skill_endpoint(
    body: SkillCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new skill."""
    config = dict(body.config)
    if body.type:
        config["type"] = body.type
    if body.scripts:
        config["scripts"] = body.scripts
    if body.requirements:
        config["requirements"] = body.requirements

    skill = await create_skill(
        db,
        entity_id=user.entity_id,
        name=body.name,
        system_prompt=body.system_prompt,
        slug=body.slug or None,
        display_name=body.display_name or None,
        description=body.description or None,
        tools=body.tools,
        input_schema=body.input_schema,
        output_format=body.output_format,
        category=body.category or None,
        tags=body.tags,
        is_public=body.is_public,
        version=body.version,
        config=config,
    )
    return _to_response(skill)


@router.post("/generate", response_model=SkillResponse, status_code=201)
async def generate_skill_endpoint(
    body: SkillGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI-generate a skill from a natural-language prompt."""
    try:
        skill = await ai_generate_skill(
            prompt=body.prompt,
            entity_id=user.entity_id,
            db=db,
            category=body.category,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _to_response(skill)


@router.post("/generate-stream")
async def generate_skill_stream_endpoint(
    body: SkillGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI-generate a skill, streaming progress as Server-Sent Events.

    Generation can take well over a minute; a plain request hits Cloudflare's
    100s origin timeout (524). Streaming ``step`` frames keeps the connection
    alive and lets the UI narrate what the AI is doing. Emits ``step`` events
    while working, then a terminal ``done`` (the skill) or ``error`` frame.
    """
    async def event_stream():
        try:
            skill = None
            async for kind, payload in ai_generate_skill_streaming(
                prompt=body.prompt,
                entity_id=user.entity_id,
                db=db,
                category=body.category,
            ):
                if kind == "step":
                    yield format_sse("step", {"label": payload})
                elif kind == "skill":
                    skill = payload
            if skill is None:
                yield format_sse("error", {"message": "Skill generation did not produce a skill"})
                return
            yield format_sse("done", _to_response(skill))
        except Exception as exc:  # noqa: BLE001 — surface any failure to the client
            yield format_sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class DraftQuestionsResponse(BaseModel):
    questions: list[str]
    ready: bool


@router.post("/draft-questions", response_model=DraftQuestionsResponse)
async def draft_skill_questions_endpoint(
    body: SkillGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clarifying questions for a skill request — powers conversational create.

    Returns up to 3 questions (``ready=true`` / empty list means the request is
    already specific enough to generate). Clarification is a nicety, so any
    failure degrades to ``ready`` rather than blocking creation.
    """
    from packages.core.ai.runtime import runtime_skill_clarifying_questions

    try:
        questions = await runtime_skill_clarifying_questions(
            body.prompt, entity_id=user.entity_id,
        )
    except Exception:
        questions = []
    return DraftQuestionsResponse(questions=questions, ready=not questions)


@router.post("/install-github", response_model=SkillResponse, status_code=201)
async def install_github_skill_endpoint(
    body: GitHubInstallRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Install a skill from a GitHub repository URL."""
    try:
        skill = await install_from_github(
            url=body.github_url,
            entity_id=user.entity_id,
            db=db,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _to_response(skill)


@router.post("/{skill_id}/ai-update", response_model=SkillResponse)
async def ai_update_skill_endpoint(
    skill_id: str,
    body: SkillAIUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI-powered skill update from a natural-language change description."""
    try:
        skill = await ai_update_skill(
            skill_id=skill_id,
            change_description=body.prompt,
            entity_id=user.entity_id,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    return _to_response(skill)


# ── Skill Credential Endpoints ──

class SkillCredentialRequest(BaseModel):
    values: dict[str, str]


@router.put("/{skill_id}/credentials", response_model=SkillResponse)
async def save_skill_credentials(
    skill_id: str,
    body: SkillCredentialRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save credential values for a skill's env_vars.

    Values are stored in MinIO (skills/{entity_id}/{skill_id}/credentials.json)
    and the DB config is updated with credential status flags only (no raw values).
    """
    skill = await get_skill(db, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if skill.entity_id and skill.entity_id != user.entity_id:
        raise HTTPException(403, "Access denied")

    cfg = dict(skill.config or {})
    env_vars = cfg.get("env_vars") or []

    if env_vars:
        required_names = {v["name"] for v in env_vars if isinstance(v, dict) and v.get("required", True)}
        credentials_configured = all(
            body.values.get(name, "").strip() for name in required_names
        )
    else:
        credentials_configured = True

    # Save credentials to MinIO (best-effort — not secret-safe for production but
    # mirrors the old manor-multi-agent pattern; upgrade to vault when needed).
    if skill.entity_id:
        try:
            from packages.core.services.skill_file_storage import save_skill_credentials as _minio_save_creds
            _cfg = skill.config or {}
            _minio_save_creds(
                skill.entity_id, skill_id, body.values,
                skill_dir=_cfg.get("minio_dir") or None,
                config=_cfg,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "MinIO credential save failed skill=%s: %s", skill_id, exc
            )

    # Keep DB config in sync — store configured status but NOT raw values
    cfg["credentials_configured"] = credentials_configured
    # Legacy: also write env_var_values for backward-compat with skill invocation
    # that still reads from config (until all callers use MinIO)
    cfg["env_var_values"] = body.values
    skill.config = cfg
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return _to_response(skill)


# ── Single skill by ID (must come after all fixed-path routes) ──

@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill_endpoint(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a skill by ID."""
    skill = await get_skill(db, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    # Allow access if platform skill or owned by entity
    if skill.entity_id is not None and skill.entity_id != user.entity_id:
        raise HTTPException(404, "Skill not found")
    bindings = await _binding_contexts_for_skills(db, [skill], entity_id=user.entity_id)
    return _to_response(skill, bindings=bindings.get(skill.id, []))


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill_endpoint(
    skill_id: str,
    body: SkillUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a skill."""
    updates = body.model_dump(exclude_none=True)

    # Merge type/scripts/requirements into config so they are persisted
    extra_config_keys = ("type", "scripts", "requirements")
    extra_config: dict = {}
    for key in extra_config_keys:
        if key in updates:
            extra_config[key] = updates.pop(key)

    if extra_config:
        # Load current config and patch only the supplied keys
        existing = await get_skill(db, skill_id)
        if existing and (existing.entity_id is None or existing.entity_id == user.entity_id):
            merged = dict(existing.config or {})
            merged.update(extra_config)
            updates["config"] = merged

    skill = await update_skill(db, skill_id, user.entity_id, **updates)
    if not skill:
        raise HTTPException(404, "Skill not found or not owned by your entity")
    bindings = await _binding_contexts_for_skills(db, [skill], entity_id=user.entity_id)
    return _to_response(skill, bindings=bindings.get(skill.id, []))


@router.get("/{skill_id}/files")
async def get_skill_files(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the full skill bundle (prompt + extra files) from MinIO.

    Useful for editors that need the raw script content without re-loading
    it from the config JSONB field.
    """
    skill = await get_skill(db, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    if skill.entity_id is not None and skill.entity_id != user.entity_id:
        raise HTTPException(404, "Skill not found")

    from packages.core.services.skill_file_storage import (
        load_skill_prompt as _load_prompt,
        load_skill_extra_files as _load_extras,
        load_skill_requirements as _load_reqs,
    )

    entity_id = skill.entity_id or ""
    cfg = skill.config or {}
    _kw = {"skill_dir": cfg.get("minio_dir") or None, "config": cfg}
    prompt = (_load_prompt(entity_id, skill_id, **_kw) if entity_id else None) or skill.system_prompt
    extra_files = _load_extras(entity_id, skill_id, **_kw) if entity_id else {}
    requirements = (_load_reqs(entity_id, skill_id, **_kw) if entity_id else "") or cfg.get("requirements") or ""

    return {
        "skill_id": skill_id,
        "prompt": prompt,
        "extra_files": extra_files,
        "requirements": requirements,
    }


@router.delete("/{skill_id}", status_code=204)
async def delete_skill_endpoint(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a skill."""
    deleted = await delete_skill(db, skill_id, user.entity_id)
    if not deleted:
        raise HTTPException(404, "Skill not found or not owned by your entity")


@router.post("/{skill_id}/invoke", response_model=InvokeResponse)
async def invoke_skill_endpoint(
    skill_id: str,
    body: InvokeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invoke a skill."""
    import json as _json
    # Stringify dict inputs into JSON so the skill's LLM gets a clean,
    # parseable user message. Skills with structured input_schema
    # (like ph_launch_studio) explicitly tell the LLM to expect a
    # JSON object.
    input_payload = (
        body.input if isinstance(body.input, str)
        else _json.dumps(body.input, ensure_ascii=False)
    )
    result = await runtime_invoke_skill(
        db, skill_id, user.entity_id, input_payload, user_id=user.id,
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.post("/batch-import", response_model=BatchImportResponse)
async def batch_import_skills(
    body: BatchImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch import skills from local folder parse results."""
    imported = skipped = failed = 0
    for item in body.skills:
        try:
            # Check if a skill with the same name already exists for this entity
            from sqlalchemy import select as sa_select
            from packages.core.models.skill import Skill as SkillModel
            existing = await db.execute(
                sa_select(SkillModel).where(
                    SkillModel.entity_id == user.entity_id,
                    SkillModel.name == item.name,
                    SkillModel.status == "active",
                )
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            await create_skill(
                db,
                entity_id=user.entity_id,
                name=item.name,
                system_prompt=item.prompt,
                description=item.description or None,
                tags=item.tags,
                is_public=item.is_public,
                version=item.version,
            )
            imported += 1
        except Exception:
            failed += 1

    await db.commit()
    return {"imported": imported, "skipped": skipped, "failed": failed}


# ── Agent Skill Binding Endpoints ──

class BindingResponse(BaseModel):
    agent_id: str
    skill_id: str
    status: str


@router.get("/agents/{agent_id}/bindings", response_model=list[SkillResponse])
async def list_agent_bindings(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List skills bound to a specific agent."""
    skills = await list_agent_skill_bindings(db, agent_id, user.entity_id)
    bindings = await _binding_contexts_for_skills(db, list(skills), entity_id=user.entity_id)
    return [_to_response(s, bindings=bindings.get(s.id, [])) for s in skills]


@router.get("/agents/{agent_id}/available", response_model=list[SkillResponse])
async def list_agent_available_skills(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List entity skills not yet bound to the agent."""
    skills = await list_available_skills_for_agent(db, agent_id, user.entity_id)
    bindings = await _binding_contexts_for_skills(db, list(skills), entity_id=user.entity_id)
    return [_to_response(s, bindings=bindings.get(s.id, [])) for s in skills]


@router.post("/agents/{agent_id}/bindings/{skill_id}", status_code=201)
async def bind_skill(
    agent_id: str,
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind a skill to an agent."""
    binding = await bind_skill_to_agent(
        db,
        agent_id,
        skill_id,
        user.entity_id,
        config={
            "binding_type": "agent_skill_binding",
            "source": "manual_agent_binding",
            "agent_id": agent_id,
            "match": {"type": "manual_selection"},
        },
    )
    if not binding:
        raise HTTPException(404, "Skill not found or not accessible")
    await db.commit()
    return {"agent_id": agent_id, "skill_id": skill_id, "status": binding.status}


@router.delete("/agents/{agent_id}/bindings/{skill_id}", status_code=204)
async def unbind_skill(
    agent_id: str,
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a skill binding from an agent."""
    removed = await unbind_skill_from_agent(db, agent_id, skill_id)
    if not removed:
        raise HTTPException(404, "Binding not found")
    await db.commit()
