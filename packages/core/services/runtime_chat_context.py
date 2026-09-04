from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.chat_logger import ChatTrace
from packages.core.ai.runtime import (
    ChannelRuntimeContext,
    ChatContext,
    ChatSurface,
    runtime_assemble_prompt_for_turn,
    runtime_request_for_chat_turn,
)
from packages.core.ai.runtime.prompt_tools import runtime_normalize_tool_name_set
from packages.core.ai.runtime.skill_forcing import (
    runtime_auto_skill_forced_tool_calls,
    runtime_message_text_for_intent,
)
from packages.core.ai.runtime.surfaces import infer_chat_surface
from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text
from packages.core.ai.runtime.turn_policy import (
    TurnExecutionPlan,
    TurnToolCatalogMode,
    build_turn_execution_plan,
    runtime_turn_visible_tool_names,
)
from packages.core.constants.agents import is_master_agent
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.services.conversation_history import load_conversation_history

logger = logging.getLogger(__name__)

def _append_extra_context(base: str | None, extra: str | None) -> str | None:
    extra = (extra or "").strip()
    if not extra:
        return base
    base = (base or "").strip()
    if not base:
        return extra
    return f"{base}\n\n{extra}"


async def resolve_runtime_chat_context(
    db: AsyncSession | None,
    message: str | list[dict],
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    is_master: bool | None = None,
    trace: ChatTrace | None = None,
    manual_skill_refs: list[dict] | None = None,
    disable_tools: bool = False,
    blocked_tools: list[str] | tuple[str, ...] | set[str] | str | None = None,
    runtime_surface: ChatSurface | str | None = None,
    channel_context: ChannelRuntimeContext | dict | None = None,
    editor_context: dict | None = None,
    runtime_metadata: dict | None = None,
) -> tuple[str, list[dict], list[dict], ChatContext]:
    """Resolve prompt, tools, history, and runtime envelope for a chat turn."""

    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    metadata = dict(runtime_metadata or {})

    # An interactive Task owns the identity of its Host Agent. Resolve that
    # before any prompt, tool, Skill, billing, or author context is assembled;
    # a request-scoped agent_id (including an @mention) cannot override it.
    task_session_host = None
    if db and conversation_id:
        from packages.core.services.task_session import (
            task_session_host_for_conversation,
        )

        task_session_host = await task_session_host_for_conversation(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        if task_session_host:
            agent_id = task_session_host.agent_id
            is_master = is_master_agent(agent_id)

    if task_session_host and task_session_host.agent_subscription_id:
        metadata["agent_subscription_id"] = (
            task_session_host.agent_subscription_id
        )
    persisted_turn_plan = metadata.get("turn_execution_plan")
    turn_plan = TurnExecutionPlan.from_metadata(persisted_turn_plan)
    disable_tools_for_turn = disable_tools or turn_plan.disable_tools
    blocked_tool_names = runtime_normalize_tool_name_set(blocked_tools)
    extra_tool_names = runtime_normalize_tool_name_set(
        metadata.get("extra_tool_names")
    )
    if extra_tool_names and not disable_tools_for_turn:
        from packages.core.ai.runtime.tool_registry import (
            runtime_tool_schemas_for_names,
        )

        extra_tool_schemas = runtime_tool_schemas_for_names(
            sorted(extra_tool_names)
        )
    else:
        extra_tool_schemas = []

    requested_agent_subscription_id = metadata.get("agent_subscription_id")
    agent_subscription_id = (
        task_session_host.agent_subscription_id
        if task_session_host
        else (
            str(requested_agent_subscription_id)
            if isinstance(requested_agent_subscription_id, str)
            and requested_agent_subscription_id.strip()
            else None
        )
    )
    runtime = await resolve_workspace_runtime(
        db,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        agent_subscription_id=agent_subscription_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        is_master=is_master,
        runtime_surface=runtime_surface,
    )
    if task_session_host:
        if task_session_host.custom_prompt:
            runtime.extra_context = _append_extra_context(
                runtime.extra_context,
                "## Host Agent Workspace Instructions\n"
                + task_session_host.custom_prompt,
            )
    elif agent_subscription_id and db:
        source_subscription = (await db.execute(
            select(AgentSubscription)
            .join(Agent, Agent.id == AgentSubscription.agent_id)
            .where(
                AgentSubscription.id == agent_subscription_id,
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.workspace_id == runtime.workspace_id,
                AgentSubscription.agent_id == agent_id,
                AgentSubscription.status == "active",
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if source_subscription is None:
            raise ValueError("Runtime Agent subscription is unavailable")
        if source_subscription.custom_prompt:
            runtime.extra_context = _append_extra_context(
                runtime.extra_context,
                "## Agent Workspace Instructions\n"
                + source_subscription.custom_prompt,
            )
    workspace_id = runtime.workspace_id
    tool_profile = runtime.tool_profile
    explicit_surface = (
        ChatSurface.FILE_EDITOR_CHAT
        if editor_context and runtime_surface is None
        else runtime_surface
    )
    surface = infer_chat_surface(
        surface=explicit_surface,
        workspace_id=workspace_id,
        agent_id=agent_id,
        ephemeral=conversation_id is None,
    )
    active_user_message_text = runtime_message_text_for_intent(message)

    # Workspace Chat does not use the global owner-chat intent classifier. For
    # explicit read-only Ledger queries, derive only the prompt-visibility plan
    # here so the parent model receives the small Ledger schema set instead of
    # every tool authorized to the Workspace master. Execution authorization,
    # model routing, billing, and Skill-injected tools remain unchanged.
    if (
        surface is ChatSurface.WORKSPACE_CHAT
        and not isinstance(persisted_turn_plan, dict)
    ):
        workspace_turn_plan = build_turn_execution_plan(
            enabled=True,
            surface=surface,
            message=message,
            message_text=active_user_message_text,
            disable_tools=disable_tools,
            workspace_id=workspace_id,
            agent_id=agent_id,
            manual_skill_selected=bool(manual_skill_refs),
            editor_context=editor_context,
            runtime_metadata=metadata,
        )
        if (
            workspace_turn_plan.tool_catalog_mode
            is TurnToolCatalogMode.LEDGER_QUERY
        ):
            turn_plan = workspace_turn_plan
            metadata["turn_execution_plan"] = turn_plan.to_metadata()

    if is_master is None:
        is_master = runtime.is_master

    if entity_id:
        try:
            from packages.core.services.agent_files import (
                effective_agent_id,
                ensure_agent_workspace,
            )

            ensure_agent_workspace(entity_id, effective_agent_id(agent_id))
        except Exception:
            logger.debug("Agent workspace provisioning failed", exc_info=True)

    # tool_discovery_v2 (A3 intent-path memory, spec §A3): cache-first
    # lookup + hint, flag-gated. Reuses the SAME extra_context channel
    # approval_resume_guidance already merges into just above (mirrors that
    # seam) rather than threading a brand-new kwarg through
    # runtime_assemble_prompt_for_turn's call chain. Recording
    # (agentic_loop._maybe_record_tool_path) runs unconditionally elsewhere;
    # only this lookup/hint/boost surface is flag-gated. Any failure
    # degrades to "no hint" — never blocks context resolution.
    path_hint: str | None = None
    hinted_tool_names: set[str] = set()
    if (
        db
        and entity_id
        and user_id
        and active_user_message_text
        and not disable_tools_for_turn
    ):
        try:
            from packages.core.services.feature_flags import is_enabled
            if await is_enabled(
                db, "tool_discovery_v2",
                entity_id=entity_id, user_id=user_id, fallback=False,
            ):
                from packages.core.services import tool_path_memory as tpm
                paths = await tpm.lookup_paths(
                    entity_id=entity_id, user_id=user_id,
                    user_message=active_user_message_text,
                )
                if paths:
                    from packages.core.services.agent_permission_service import (
                        resolve_usable_mcp_providers,
                    )
                    usable = await resolve_usable_mcp_providers(
                        db, user_id=user_id, entity_id=entity_id,
                        provider_keys=sorted({p.provider for p in paths if p.provider}),
                    )
                    paths = [p for p in paths if p.provider in usable]
                if paths:
                    path_hint = tpm.format_hint(paths)
                    hinted_tool_names = {p.tool_name for p in paths}
        except Exception:
            logger.debug("tool_path_memory hint resolution failed", exc_info=True)
            path_hint = None
            hinted_tool_names = set()

    extra_context = _append_extra_context(
        _append_extra_context(
            runtime.extra_context,
            (
                metadata.get("approval_resume_guidance")
                if isinstance(metadata.get("approval_resume_guidance"), str)
                else None
            ),
        ),
        path_hint,
    )
    runtime_request = runtime_request_for_chat_turn(
        surface=surface,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=runtime.task_id,
        thread_ref_kind=runtime.thread_ref_kind,
        thread_ref_id=runtime.thread_ref_id,
        message=message,
        channel_context=channel_context,
        editor_context=editor_context,
        manual_skill_refs=manual_skill_refs,
        ephemeral=conversation_id is None,
        legacy_path="runtime_chat_context.resolve_runtime_chat_context",
        metadata={**metadata, "disable_tools": disable_tools_for_turn},
    )
    assembled = await runtime_assemble_prompt_for_turn(
        db,
        request=runtime_request,
        tool_profile=tool_profile,
        agent_id=agent_id,
        bound_tool_names=runtime.bound_tool_names,
        is_master=runtime.is_master,
        mcp_allowed_names=runtime.mcp_allowed_names,
        mcp_provider_scopes=getattr(runtime, "mcp_provider_scopes", ()),
        mcp_scope_unrestricted=bool(
            getattr(runtime, "mcp_scope_unrestricted", False)
        ),
        mode=turn_plan.prompt_mode.value,
        active_user_message=runtime_message_text_for_intent(message),
        manual_skill_selected=bool(manual_skill_refs),
        legacy_extra_context=extra_context,
        initial_extra_context=extra_context,
        visible_tool_names=runtime_turn_visible_tool_names(
            turn_plan,
            manual_skill_selected=bool(manual_skill_refs),
            available_tool_names=runtime.bound_tool_names,
        ),
        tool_schemas=[] if (disable_tools_for_turn or not db) else None,
        allowed_tool_names=set() if (disable_tools_for_turn or not db) else None,
        extra_tool_schemas=extra_tool_schemas,
        extra_allowed_tool_names=extra_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=manual_skill_refs,
        disable_tools=disable_tools_for_turn or not db,
    )
    ctx = assembled.context
    tools = assembled.tool_schemas
    system_prompt = assembled.prompt
    ctx.turn_execution_plan = turn_plan
    ctx.hinted_tool_names = hinted_tool_names
    if entity_id:
        try:
            from packages.core.services.agent_runtime_config import (
                agent_runtime_config_for,
            )
            from packages.core.services.model_resolver import (
                resolve_llm_metadata_for_user,
                resolve_model_for_user,
            )

            agent_config = agent_runtime_config_for(ctx.agent)
            ctx.temperature = agent_config.temperature
            ctx.max_tokens = agent_config.max_tokens
            model_role = "worker" if turn_plan.use_fast_model else "primary"
            if agent_config.model:
                ctx.model = agent_config.model
            else:
                ctx.model = await resolve_model_for_user(
                    model_role,
                    user_id=user_id,
                    entity_id=entity_id,
                    db=db,
                )
            resolved_metadata = await resolve_llm_metadata_for_user(
                model_role,
                user_id=user_id,
                entity_id=entity_id,
                db=db,
            )
            if resolved_metadata:
                ctx.llm_metadata = {
                    **resolved_metadata,
                    "_resolved_model": ctx.model,
                }
        except Exception:
            logger.debug("Tenant LLM route resolution failed for chat context", exc_info=True)
    forced_tool_calls = metadata.get("forced_tool_calls")
    if disable_tools_for_turn:
        ctx.auto_forced_tool_calls = []
    elif isinstance(forced_tool_calls, list) and not manual_skill_refs:
        ctx.auto_forced_tool_calls = [
            call for call in forced_tool_calls if isinstance(call, dict)
        ]
    else:
        ctx.auto_forced_tool_calls = await runtime_auto_skill_forced_tool_calls(
            ctx,
            message,
        )
    chat_mode_prompt = metadata.get("chat_mode_prompt")
    if isinstance(chat_mode_prompt, str) and chat_mode_prompt.strip():
        system_prompt = f"{chat_mode_prompt.strip()}\n\n{system_prompt}"
    general_intent_prompt = metadata.get("general_intent_prompt")
    if isinstance(general_intent_prompt, str) and general_intent_prompt.strip():
        system_prompt = f"{general_intent_prompt.strip()}\n\n{system_prompt}"

    initial_messages: list[dict] = []
    if db and conversation_id:
        from packages.core.ai.agentic_loop import MAX_CONTEXT_TOKENS

        message_preview = runtime_message_text_for_intent(message)
        overhead_tokens = (
            runtime_estimate_tokens_for_text(system_prompt)
            + runtime_estimate_tokens_for_text(json.dumps(tools))
            + runtime_estimate_tokens_for_text(message_preview)
        )
        output_reserve = 4_000
        history_budget = max(
            MAX_CONTEXT_TOKENS - overhead_tokens - output_reserve,
            8_000,
        )
        if turn_plan.history_token_budget is not None:
            history_budget = min(
                history_budget,
                turn_plan.history_token_budget,
            )

        history = await load_conversation_history(
            db,
            conversation_id,
            token_budget=history_budget,
            latest_user_message=message_preview,
        )
        if (
            history
            and history[-1].get("role") == "user"
            and isinstance(message, str)
            and history[-1].get("content") == message
        ):
            history = history[:-1]
        initial_messages = history

    if trace:
        trace.log_context(
            agent_name=ctx.agent.name if ctx.agent else None,
            user_name=(ctx.user.display_name or ctx.user.email) if ctx.user else None,
            entity_name=ctx.entity.name if ctx.entity else None,
            model=ctx.entity.llm_model if ctx.entity else None,
            user_timezone=ctx.user.timezone if ctx.user else None,
            tool_count=len(tools),
            tool_names=ctx.tool_names,
            history_count=len(initial_messages),
            prompt_length=len(system_prompt),
            prompt_source=ctx.prompt_source,
            agent_files_loaded=ctx.agent_files_loaded or None,
        )

    return system_prompt, tools, initial_messages, ctx
