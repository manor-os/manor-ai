"""Unified LLM execution context builder.

Used by: Chat Service, TaskRunner, InternalWorker, Planner.
Resolves model, system prompt, tools, and history in one call.

Usage:
    ctx = await build_agent_context(db, entity_id="...", agent_id="...")
    result = await runtime_execute_agentic_loop(
        runtime_envelope=ctx.runtime_envelope,
        system_prompt=ctx.system_prompt,
        user_message="...",
        tools=ctx.tools,
        entity_id=ctx.entity_id,
        agent_id=ctx.agent_id,
        legacy_tool_profile=ctx.legacy_runtime_profile,
        allowed_tool_names=ctx.allowed_tool_names,
        model=ctx.model,
        ...
    )
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    ChatSurface,
    RuntimeEnvelope,
    runtime_assemble_prompt_for_turn,
    runtime_merge_prompt_appendix,
    runtime_prepare_agent_tool_surface_for_turn,
    runtime_request_for_chat_turn,
)
from packages.core.ai.runtime.profiles import runtime_profile_name_for_surface

logger = logging.getLogger(__name__)


@dataclass
class AgentExecutionContext:
    """Everything needed to call the Runtime Harness or engine.chat."""
    system_prompt: str = ""
    tools: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    allowed_tool_names: set[str] = field(default_factory=set)
    model: str = ""
    initial_messages: list[dict] = field(default_factory=list)
    entity_id: str = ""
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: str = ""
    workspace_id: Optional[str] = None
    task_id: Optional[str] = None
    runtime_profile: Optional[str] = None
    legacy_runtime_profile: Optional[str] = None
    runtime_surface: str | None = None
    runtime_envelope: RuntimeEnvelope | None = None
    llm_metadata: Optional[dict] = None
    byok: bool = False


async def build_agent_context(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    active_user_message: Optional[str] = None,
    model_role: str = "primary",
    mode: str = "full",
    extra_system_prompt: Optional[str] = None,
    runtime_surface: ChatSurface | str = ChatSurface.SCHEDULED_AGENT_RUN,
) -> AgentExecutionContext:
    """Build a complete execution context for any LLM call.

    Args:
        entity_id: The entity (tenant) this execution belongs to.
        user_id: Optional user who triggered it (for model prefs).
        agent_id: Which agent to use. None = master agent.
        workspace_id: Optional workspace scope (loads workspace context).
        conversation_id: Optional conversation (loads history).
        active_user_message: Latest task/message text for skill routing hints.
        model_role: Model tier — "primary", "worker", "image", etc.
        mode: Prompt detail — "full" (chat), "minimal" (quick), "task" (execution).
        extra_system_prompt: Appended to the resolved system prompt.
    """
    from packages.core.constants.agents import is_master_agent, MANOR_AGENT_ID

    ctx = AgentExecutionContext(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
    )

    # ── Resolve model + role-specific BYOK metadata ──
    try:
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )
        ctx.model = await resolve_model_for_user(
            model_role, user_id=user_id, entity_id=entity_id, db=db,
        )
        ctx.llm_metadata = await resolve_llm_metadata_for_user(
            model_role,
            user_id=user_id,
            entity_id=entity_id,
            db=db,
        )
        ctx.byok = bool(ctx.llm_metadata)
    except Exception:
        from packages.core.services.model_resolver import default_llm_model
        ctx.model = default_llm_model()
        logger.warning("build_agent_context: model resolution failed, using default %s", ctx.model)

    # ── Determine if master agent ──
    is_master = (
        agent_id is None
        or agent_id == MANOR_AGENT_ID
        or is_master_agent(agent_id, None)
    )

    from packages.core.services.workspace_runtime import resolve_workspace_runtime
    runtime = await resolve_workspace_runtime(
        db,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        is_master=is_master,
        runtime_surface=runtime_surface,
    )
    ctx.workspace_id = runtime.workspace_id
    ctx.task_id = runtime.task_id
    surface = runtime_surface if isinstance(runtime_surface, ChatSurface) else ChatSurface(str(runtime_surface))
    ctx.runtime_profile = runtime_profile_name_for_surface(surface)
    ctx.legacy_runtime_profile = runtime.legacy_tool_profile
    ctx.runtime_surface = surface.value

    # ── Resolve system prompt + tools ──
    # Tool discovery must happen before PromptBuilder runs. The available
    # skills section only advertises skills when ``invoke_skill`` is visible in
    # the current runtime scope, so building the prompt first silently hides
    # bound skills from worker/subagent execution.
    try:
        runtime_request = runtime_request_for_chat_turn(
            surface=surface,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=runtime.workspace_id,
            conversation_id=conversation_id,
            task_id=runtime.task_id,
            thread_ref_kind=runtime.thread_ref_kind,
            thread_ref_id=runtime.thread_ref_id,
            message=active_user_message or "",
            legacy_path="ai.context.build_agent_context",
        )
        assembled = await runtime_assemble_prompt_for_turn(
            db,
            request=runtime_request,
            legacy_runtime_profile=runtime.legacy_tool_profile,
            agent_id=agent_id,
            bound_tool_names=runtime.bound_tool_names,
            is_master=runtime.is_master,
            mcp_allowed_names=runtime.mcp_allowed_names,
            mode=mode if mode != "task" else "full",
            active_user_message=active_user_message,
            legacy_extra_context=runtime.extra_context,
            initial_extra_context=runtime.extra_context,
        )
        chat_ctx = assembled.context
        ctx.tools = assembled.tool_schemas
        ctx.tool_names = list(assembled.tool_names)
        ctx.allowed_tool_names = set(assembled.allowed_tool_names)
        ctx.runtime_envelope = assembled.envelope
        ctx.runtime_profile = assembled.envelope.profile.value
        ctx.system_prompt = assembled.prompt
        if is_master:
            ctx.agent_name = "Manor AI"
        else:
            ctx.agent_name = getattr(chat_ctx.agent, "name", None) or "Agent"
    except Exception:
        logger.warning("build_agent_context: prompt/tool resolution failed", exc_info=True)
        ctx.system_prompt = (
            "You are Manor AI, a helpful assistant."
            if is_master
            else "You are a helpful assistant."
        )
        try:
            fallback_request = runtime_request_for_chat_turn(
                surface=surface,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=runtime.workspace_id,
                conversation_id=conversation_id,
                task_id=runtime.task_id,
                thread_ref_kind=runtime.thread_ref_kind,
                thread_ref_id=runtime.thread_ref_id,
                message=active_user_message or "",
                legacy_path="ai.context.build_agent_context.fallback",
            )
            surface_result = runtime_prepare_agent_tool_surface_for_turn(
                fallback_request,
                agent_id=agent_id,
                bound_tool_names=runtime.bound_tool_names,
                is_master=runtime.is_master,
                mcp_allowed_names=runtime.mcp_allowed_names,
                legacy_runtime_profile=runtime.legacy_tool_profile,
            )
            ctx.tools = surface_result.tool_schemas
            ctx.tool_names = [
                t.get("function", {}).get("name", "") for t in ctx.tools
            ]
            ctx.allowed_tool_names = surface_result.allowed_tool_names
            ctx.runtime_envelope = surface_result.envelope
            ctx.runtime_profile = surface_result.envelope.profile.value
        except Exception:
            logger.warning("build_agent_context: fallback tool loading failed", exc_info=True)

    # ── Append caller-provided prompt override ──
    if extra_system_prompt:
        ctx.system_prompt = runtime_merge_prompt_appendix(ctx.system_prompt, extra_system_prompt)

    # ── Load conversation history (if available) ──
    if conversation_id:
        try:
            from packages.core.services.conversation_history import load_conversation_history
            ctx.initial_messages = await load_conversation_history(
                db,
                conversation_id,
                token_budget=80_000,
                latest_user_message=active_user_message,
            )
        except Exception:
            logger.debug("build_agent_context: history loading failed for conv %s", conversation_id)

    return ctx
