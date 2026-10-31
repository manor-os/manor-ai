from __future__ import annotations

import json
import logging
from typing import Any

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
from packages.core.ai.runtime.surfaces import infer_chat_surface, normalize_surface
from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text
from packages.core.ai.runtime.turn_policy import (
    TurnExecutionPlan,
    runtime_turn_visible_tool_names,
)
from packages.core.constants.agents import is_master_agent
from packages.core.constants.conversation import ConversationSurfaceKind
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.services.conversation_history import load_conversation_history
from packages.core.services.conversation_surfaces import (
    ConversationSurfaceMetadataFactory,
)

logger = logging.getLogger(__name__)

def _append_extra_context(base: str | None, extra: str | None) -> str | None:
    extra = (extra or "").strip()
    if not extra:
        return base
    base = (base or "").strip()
    if not base:
        return extra
    return f"{base}\n\n{extra}"


async def _recent_chat_capability_hints(
    db: AsyncSession | None,
    *,
    conversation_id: str | None,
    scope: dict[str, str | None],
) -> list[dict[str, str]]:
    """Read persisted identity hints; an active placeholder has no such record."""
    if db is None or not conversation_id:
        return []
    from packages.core.ai.runtime.capability_continuation import (
        CAPABILITY_HINT_LIMIT,
        runtime_capability_hint_state,
        runtime_known_capability_hints,
    )

    rows = (await db.execute(
        select(Message.meta).where(
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
        ).order_by(Message.created_at.desc()).limit(CAPABILITY_HINT_LIMIT)
    )).scalars()
    hints: list[dict[str, str]] = []
    for meta in rows:
        state = meta.get("capability_hints") if isinstance(meta, dict) else None
        hints.extend(runtime_known_capability_hints(state, scope=scope))
    # Rank across the bounded recent rows before trimming. A later discovery-
    # only answer must not hide the capability that produced actual evidence.
    return runtime_capability_hint_state(scope=scope, hints=hints)["hints"]


async def _restore_chat_capability_schemas(
    *,
    hints: list[dict[str, str]],
    envelope: Any,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    active_user_message: str,
) -> tuple[list[dict], tuple[str, ...]]:
    """Run the shared exact discovery restore under this turn's fresh envelope."""
    from packages.core.ai.runtime.capability_continuation import (
        runtime_restore_gateway_tools,
    )

    return await runtime_restore_gateway_tools(
        hints,
        envelope=envelope,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        active_user_message=active_user_message,
    )


async def _personal_agent_subscription_for_chat(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    user_id: str | None,
    conversation_id: str | None,
    requested_agent_id: str | None,
    requested_workspace_id: str | None,
    runtime_surface: ChatSurface | str | None,
    channel_context: ChannelRuntimeContext | dict | None,
    editor_context: dict | None,
) -> AgentSubscription | None:
    """Resolve an exact owner-bound deployment only for first-party web Chat.

    External/public channel conversations also carry AgentSubscription ids, but
    their sender and credential authority is established by the channel
    gateway.  Keeping this resolver internal-only prevents a personal Chat
    convenience path from changing those established runtime semantics.
    """

    if not db or not entity_id or not user_id or not conversation_id:
        return None
    if channel_context is not None or requested_workspace_id or editor_context:
        return None
    normalized_surface = normalize_surface(runtime_surface)
    if normalized_surface not in {
        None,
        ChatSurface.GLOBAL_OWNER_CHAT,
        ChatSurface.AGENT_DM,
    }:
        return None

    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if conversation is None:
        return None
    first_party_personal = bool(
        conversation.workspace_id is None
        and conversation.channel == "web"
        and conversation.scope == "channel"
        and ConversationSurfaceMetadataFactory.matches(
            conversation.meta,
            ConversationSurfaceKind.ORDINARY_CHAT,
        )
    )
    if not first_party_personal:
        return None
    if conversation.user_id != user_id or conversation.status != "active":
        raise ValueError("Runtime Agent conversation is unavailable")
    if not conversation.agent_subscription_id:
        return None

    subscription = (await db.execute(
        select(AgentSubscription)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(
            AgentSubscription.id == conversation.agent_subscription_id,
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.owner_user_id == user_id,
            AgentSubscription.workspace_id.is_(None),
            AgentSubscription.status == "active",
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if subscription is None:
        raise ValueError("Runtime Agent subscription is unavailable")

    subscription_is_master = is_master_agent(subscription.agent_id)
    if subscription_is_master:
        conversation_agent_matches = (
            conversation.agent_id is None
            or is_master_agent(conversation.agent_id)
        )
        requested_agent_matches = (
            not requested_agent_id or is_master_agent(requested_agent_id)
        )
    else:
        conversation_agent_matches = conversation.agent_id == subscription.agent_id
        requested_agent_matches = (
            not requested_agent_id
            or requested_agent_id == subscription.agent_id
        )
    if not conversation_agent_matches or not requested_agent_matches:
        raise ValueError("Runtime Agent subscription does not match this conversation")
    return subscription


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
    history_token_budget: int | None = None,
    split_turn_context: bool = False,
) -> tuple[str, list[dict], list[dict], ChatContext]:
    """Resolve prompt, tools, history, and runtime envelope for a chat turn.

    With ``split_turn_context`` the returned system prompt holds only what is
    the same every turn of the conversation; ``ctx.runtime_turn_context`` holds
    the rest, for the agentic loop to send after the history.
    """

    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    metadata = dict(runtime_metadata or {})
    mark = trace.mark if trace else (lambda _phase: None)
    mark("before_context")

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
    personal_subscription = None
    if not task_session_host:
        personal_subscription = await _personal_agent_subscription_for_chat(
            db,
            entity_id=entity_id,
            user_id=user_id,
            conversation_id=conversation_id,
            requested_agent_id=agent_id,
            requested_workspace_id=workspace_id,
            runtime_surface=runtime_surface,
            channel_context=channel_context,
            editor_context=editor_context,
        )
    if personal_subscription is not None:
        requested_subscription_id = metadata.get("agent_subscription_id")
        if (
            isinstance(requested_subscription_id, str)
            and requested_subscription_id.strip()
            and requested_subscription_id != personal_subscription.id
        ):
            raise ValueError(
                "Runtime Agent subscription does not match this conversation"
            )
        metadata["agent_subscription_id"] = personal_subscription.id
        if is_master_agent(personal_subscription.agent_id):
            agent_id = None
            is_master = True
            runtime_surface = ChatSurface.GLOBAL_OWNER_CHAT
        else:
            agent_id = personal_subscription.agent_id
            is_master = False
            runtime_surface = ChatSurface.AGENT_DM
    mark("agent_subscription")
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
        source_subscription = personal_subscription
        if source_subscription is None:
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
                (
                    "## Agent Workspace Instructions\n"
                    if runtime.workspace_id
                    else "## Agent Instance Instructions\n"
                )
                + source_subscription.custom_prompt,
            )
    mark("workspace_runtime")
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
    if (
        db and entity_id and user_id and runtime.task_id
        and runtime.thread_ref_kind == "task"
        and runtime.thread_ref_id == runtime.task_id
        and surface == ChatSurface.WORKSPACE_CHAT
        and not channel_context and not editor_context
        and not metadata.get("system_worker")
    ):
        from packages.core.constants.task import TaskType
        from packages.core.models.task import Task

        # Card continuations arrive through ordinary Workspace Chat. Derive
        # their foreground surface from the durable thread, never metadata.
        general_task_thread = (await db.execute(
            select(Task.id).join(Conversation, Conversation.thread_ref_id == Task.id).where(
                Task.id == runtime.task_id, Task.entity_id == entity_id,
                Task.workspace_id == workspace_id, Task.task_type == TaskType.GENERAL.value,
                Conversation.id == conversation_id, Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id, Conversation.scope == "workspace_thread",
                Conversation.thread_ref_kind == "task", Conversation.status == "active",
            )
        )).scalar_one_or_none()
        if general_task_thread:
            surface = ChatSurface.TASK_COMMENT_THREAD
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
    mark("agent_workspace_fs")

    # Tool-path memory remains advisory; do not inject keyword-derived hints
    # or grant their tool names while constructing the Chat context.
    extra_context = _append_extra_context(
        runtime.extra_context,
        (
            metadata.get("approval_resume_guidance")
            if isinstance(metadata.get("approval_resume_guidance"), str)
            else None
        ),
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
    active_user_message = runtime_message_text_for_intent(message)
    turn_tool_focus = runtime_turn_visible_tool_names(
        turn_plan,
        manual_skill_selected=bool(manual_skill_refs),
        available_tool_names=runtime.bound_tool_names,
    )

    async def assemble(extra_schemas: list[dict], extra_names: set[str]):
        return await runtime_assemble_prompt_for_turn(
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
            active_user_message=active_user_message,
            manual_skill_selected=bool(manual_skill_refs),
            legacy_extra_context=extra_context,
            initial_extra_context=extra_context,
            # Split turns keep the tool list stable and say the focus in the
            # turn context; narrowing the schemas would change the prefix.
            visible_tool_names=None if split_turn_context else turn_tool_focus,
            tool_schemas=[] if (disable_tools_for_turn or not db) else None,
            allowed_tool_names=set() if (disable_tools_for_turn or not db) else None,
            extra_tool_schemas=extra_schemas,
            extra_allowed_tool_names=extra_names,
            blocked_tool_names=blocked_tool_names,
            skill_refs=manual_skill_refs,
            disable_tools=disable_tools_for_turn or not db,
            split_turn_context=split_turn_context,
        )

    assembled = await assemble(extra_tool_schemas, extra_tool_names)
    mark("assemble_prompt")
    if not disable_tools_for_turn and db and conversation_id:
        principal = assembled.envelope.principal
        from packages.core.ai.runtime.capability_continuation import runtime_capability_scope

        hints = await _recent_chat_capability_hints(
            db,
            conversation_id=conversation_id,
            scope=runtime_capability_scope(
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=principal.actor_user_id or user_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
            ),
        )
        current_tool_names = {
            str((schema.get("function") or {}).get("name") or "")
            for schema in assembled.tool_schemas
            if isinstance(schema, dict)
        }
        hints = [hint for hint in hints if hint["name"] not in current_tool_names]
        restored_schemas, restored_names = await _restore_chat_capability_schemas(
            hints=hints,
            envelope=assembled.envelope,
            entity_id=entity_id,
            user_id=principal.actor_user_id or user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            active_user_message=active_user_message,
        )
        gateway = "call_tool" in current_tool_names
        if restored_schemas:
            # With call_tool, restored tools stay reachable through it: their
            # names join the allowlist, their schemas never enter the tool
            # list, so the cached request prefix is the same as last turn.
            assembled = await assemble(
                extra_tool_schemas if gateway else [*extra_tool_schemas, *restored_schemas],
                extra_tool_names | set(restored_names),
            )
            if gateway:
                assembled.context.gateway_tool_schemas = list(restored_schemas)
    mark("capability_restore")
    ctx = assembled.context
    tools = assembled.tool_schemas
    system_prompt = assembled.prompt
    ctx.turn_execution_plan = turn_plan
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
                    workspace_id=workspace_id,
                )
            resolved_metadata = await resolve_llm_metadata_for_user(
                model_role,
                user_id=user_id,
                entity_id=entity_id,
                db=db,
                workspace_id=workspace_id,
            )
            if resolved_metadata:
                ctx.llm_metadata = {
                    **resolved_metadata,
                    "_resolved_model": ctx.model,
                }
        except Exception:
            logger.debug("Tenant LLM route resolution failed for chat context", exc_info=True)
    mark("model_route")
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
    mark("auto_skill")
    chat_mode_prompt = metadata.get("chat_mode_prompt")
    if isinstance(chat_mode_prompt, str) and chat_mode_prompt.strip():
        system_prompt = f"{chat_mode_prompt.strip()}\n\n{system_prompt}"
    general_intent_prompt = metadata.get("general_intent_prompt")
    if isinstance(general_intent_prompt, str) and general_intent_prompt.strip():
        if split_turn_context:
            # Re-classified every turn: it belongs with the turn, not the prefix.
            ctx.runtime_turn_context = _append_extra_context(
                general_intent_prompt.strip(), ctx.runtime_turn_context,
            ) or ""
        else:
            system_prompt = f"{general_intent_prompt.strip()}\n\n{system_prompt}"
    if split_turn_context and turn_tool_focus and tools:
        offered = {str((tool.get("function") or {}).get("name") or "") for tool in tools}
        focus = [name for name in turn_tool_focus if name in offered]
        if focus:
            ctx.runtime_turn_context = _append_extra_context(
                ctx.runtime_turn_context,
                "## Tools for this request\n"
                f"This request is a {turn_plan.tool_catalog_mode.value.replace('_', ' ')} request. "
                f"Use {', '.join(f'`{name}`' for name in focus)}. The other tools stay "
                "available; use one only when the request clearly needs it.",
            ) or ""

    initial_messages: list[dict] = []
    if db and conversation_id:
        from packages.core.services.model_context_window import effective_context_window_tokens
        from packages.core.ai.runtime.agentic_llm import runtime_agentic_output_tokens

        message_preview = runtime_message_text_for_intent(message)
        overhead_tokens = (
            runtime_estimate_tokens_for_text(system_prompt)
            + runtime_estimate_tokens_for_text(json.dumps(tools))
            + runtime_estimate_tokens_for_text(message_preview)
        )
        loop_model = getattr(ctx, "model", None)
        loop_metadata = getattr(ctx, "llm_metadata", None)
        output_reserve = runtime_agentic_output_tokens(loop_metadata, model=loop_model, tools=bool(tools))
        history_budget = max(
            effective_context_window_tokens(loop_model, loop_metadata) - overhead_tokens - output_reserve,
            0,
        )
        if turn_plan.history_token_budget is not None:
            history_budget = min(
                history_budget,
                turn_plan.history_token_budget,
            )
        if history_token_budget is not None:
            history_budget = min(
                history_budget,
                max(0, history_token_budget),
            )

        shared_workspace_senders = bool(
            user_id
            and workspace_id
            and surface in {ChatSurface.WORKSPACE_CHAT, ChatSurface.TASK_COMMENT_THREAD}
        )
        origin_message_id = metadata.get("origin_user_message_id")

        async def rendered_history() -> list[dict]:
            history = await load_conversation_history(
                db,
                conversation_id,
                token_budget=history_budget,
                latest_user_message=message_preview,
                include_workspace_senders=shared_workspace_senders,
                current_sender_user_id=user_id if shared_workspace_senders else None,
                exclude_message_id=(
                    origin_message_id
                    if shared_workspace_senders and isinstance(origin_message_id, str)
                    else None
                ),
                runtime_metadata=assembled.envelope.metadata,
            )
            if (
                history
                and history[-1].get("role") == "user"
                and isinstance(message, str)
                and history[-1].get("content") == message
            ):
                history = history[:-1]
            return history

        from packages.core.ai.runtime.file_context import RUNTIME_HISTORY_ATTACHMENT_REFS_KEY
        from packages.core.services.conversation_history import history_attachment_refs
        from packages.core.services.conversation_transcript import (
            load_or_import_transcript,
            transcript_current_sender_note,
            transcript_scope_for_turn,
        )

        ctx.conversation_transcript = transcript_scope_for_turn(
            surface, entity_id=entity_id, conversation_id=conversation_id, user_id=user_id,
            agent_id=agent_id, workspace_id=getattr(ctx, "workspace_id", None),
        )
        if ctx.conversation_transcript is not None:
            initial_messages = await load_or_import_transcript(
                db, ctx.conversation_transcript,
                legacy_history=rendered_history,
                exclude_message_ids={
                    value for value in (origin_message_id, metadata.get("assistant_message_id"))
                    if isinstance(value, str) and value
                },
            )
            # Older runs stored tool-round replies in the JSON reply envelope;
            # plain-text replies must not learn to imitate it.
            from packages.core.ai.runtime.chat_evidence import plain_text_history

            initial_messages = plain_text_history(initial_messages)
            ctx.transcript_initial_messages = [dict(m) for m in initial_messages]
            # Rendered history ends with this note; transcript history is
            # append-only, so the per-turn note belongs in the system prompt.
            sender_note = await transcript_current_sender_note(db, ctx.conversation_transcript)
            if sender_note:
                system_prompt = f"{system_prompt}\n\n{sender_note}"
            # Rendered history records these as it renders; child skills need them either way.
            assembled.envelope.metadata[RUNTIME_HISTORY_ATTACHMENT_REFS_KEY] = (
                await history_attachment_refs(db, conversation_id)
            )
        else:
            initial_messages = await rendered_history()

    mark("history")
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
