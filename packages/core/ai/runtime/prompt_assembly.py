from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.prompt_adapter import ChatContext, build_default_prompt_builder
from packages.core.ai.runtime.prompt_sections import RuntimePromptMode, available_skills_section
from packages.core.ai.runtime.prompt_tools import (
    runtime_prompt_tool_name,
    runtime_populate_named_tools_for_prompt_context,
    runtime_populate_tools_for_prompt_context,
    runtime_set_tools_for_prompt_context,
)
from packages.core.ai.runtime.skill_forcing import runtime_apply_manual_skill_tool_surface
from packages.core.ai.runtime.profiles import runtime_profile_name_for_surface
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.resolver import RuntimeResolver
from packages.core.ai.runtime.tool_registry import runtime_tool_schema
from packages.core.ai.runtime.context_blocks import (
    RuntimeContextBlock,
    render_context_blocks,
    resolve_runtime_context_blocks,
)
from packages.core.ai.runtime.skills import populate_runtime_skill_descriptors


@dataclass(frozen=True)
class RuntimePromptAssemblyResult:
    prompt: str
    context: ChatContext
    request: AIRuntimeRequest
    envelope: RuntimeEnvelope
    tool_schemas: list[dict]
    tool_names: tuple[str, ...]
    allowed_tool_names: set[str]


@dataclass(frozen=True)
class RuntimePromptAppendixResult:
    prompt_appendix: str
    context_section: str | None
    skill_section: str | None
    context: ChatContext
    request: AIRuntimeRequest
    envelope: RuntimeEnvelope
    tool_schemas: list[dict]
    tool_names: tuple[str, ...]
    allowed_tool_names: set[str]


@dataclass(frozen=True)
class RuntimeBasePromptResult:
    prompt: str
    context: ChatContext
    request: AIRuntimeRequest


@dataclass(frozen=True)
class RuntimeContextAppendixResult:
    context_section: str | None
    context_blocks: tuple[RuntimeContextBlock, ...]
    request: AIRuntimeRequest
    envelope: RuntimeEnvelope


@dataclass(frozen=True)
class RuntimePreparedPromptContext:
    context: ChatContext
    request: AIRuntimeRequest
    envelope: RuntimeEnvelope
    tool_schemas: list[dict]
    tool_names: tuple[str, ...]
    allowed_tool_names: set[str]


def runtime_merge_prompt_appendix(
    base_prompt: str | None,
    appendix: str | RuntimePromptAppendixResult | None,
) -> str:
    """Attach a Runtime prompt appendix to a caller-owned system prompt."""

    prompt = str(base_prompt or "").strip()
    appendix_text = (
        appendix.prompt_appendix
        if isinstance(appendix, RuntimePromptAppendixResult)
        else str(appendix or "")
    ).strip()
    if not appendix_text:
        return prompt
    if appendix_text in prompt:
        return prompt
    if not prompt:
        return appendix_text
    return f"{prompt}\n\n{appendix_text}"


async def runtime_assemble_prompt_for_turn(
    db: AsyncSession | None,
    *,
    request: AIRuntimeRequest,
    legacy_runtime_profile: str | None = None,
    runtime_profile: str | None = None,
    agent_id: str | None = None,
    bound_tool_names: set[str] | None = None,
    is_master: bool = False,
    mcp_allowed_names: set[str] | None = None,
    mode: RuntimePromptMode = "full",
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    legacy_extra_context: str | None = None,
    initial_extra_context: str | None = None,
    configured_tool_names: Iterable[str] | str | None = None,
    tool_schemas: Iterable[dict] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    extra_tool_schemas: Iterable[dict] | None = None,
    extra_allowed_tool_names: Iterable[str] | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict] | None = None,
    disable_tools: bool = False,
) -> RuntimePromptAssemblyResult:
    """Assemble one runtime-owned prompt context for an AI turn.

    This keeps entrypoints from re-creating the same sequence:
    ChatContext -> tool surface -> RuntimeEnvelope -> context blocks -> skills.
    """

    prepared = await runtime_prepare_prompt_context_for_turn(
        db,
        request=request,
        legacy_runtime_profile=legacy_runtime_profile,
        runtime_profile=runtime_profile,
        agent_id=agent_id,
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        mode=mode,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
        legacy_extra_context=legacy_extra_context,
        initial_extra_context=initial_extra_context,
        configured_tool_names=configured_tool_names,
        tool_schemas=tool_schemas,
        allowed_tool_names=allowed_tool_names,
        extra_tool_schemas=extra_tool_schemas,
        extra_allowed_tool_names=extra_allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
        disable_tools=disable_tools,
    )
    prompt = await build_default_prompt_builder().build(prepared.context) or ""
    return RuntimePromptAssemblyResult(
        prompt=prompt,
        context=prepared.context,
        request=prepared.request,
        envelope=prepared.envelope,
        tool_schemas=prepared.tool_schemas,
        tool_names=prepared.tool_names,
        allowed_tool_names=prepared.allowed_tool_names,
    )


async def runtime_build_base_prompt_for_turn(
    db: AsyncSession | None,
    *,
    request: AIRuntimeRequest,
    legacy_runtime_profile: str | None = None,
    runtime_profile: str | None = None,
    agent_id: str | None = None,
    mode: RuntimePromptMode = "full",
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    initial_extra_context: str | None = None,
) -> RuntimeBasePromptResult:
    """Build a runtime-owned base prompt without resolving a tool surface.

    Custom system-prompt entrypoints use this for identity/workspace prompt
    loading before a separate runtime appendix prepares tools, context blocks,
    and available skill descriptors.
    """

    effective_agent_id = agent_id if agent_id is not None else request.agent_id
    effective_runtime_profile = runtime_profile_name_for_surface(
        request.surface,
        runtime_profile,
    )
    ctx = ChatContext(
        db=db,
        entity_id=request.entity_id,
        user_id=request.user_id,
        agent_id=effective_agent_id,
        workspace_id=request.workspace_id,
        conversation_id=request.conversation_id,
        task_id=request.task_id,
        thread_ref_kind=request.thread_ref_kind,
        thread_ref_id=request.thread_ref_id,
        runtime_profile=effective_runtime_profile,
        legacy_runtime_profile=legacy_runtime_profile,
        runtime_surface=request.surface.value,
        runtime_profile_name=effective_runtime_profile,
        active_user_message=active_user_message or request.input_preview,
        manual_skill_selected=manual_skill_selected,
        mode=mode,
    )
    await ctx.resolve()
    if initial_extra_context:
        ctx.extra_context = initial_extra_context
    prompt = await build_default_prompt_builder().build(ctx) or ""
    return RuntimeBasePromptResult(prompt=prompt, context=ctx, request=request)


async def runtime_prepare_context_appendix_for_turn(
    db: AsyncSession | None,
    *,
    request: AIRuntimeRequest,
    legacy_runtime_profile: str | None = None,
    legacy_extra_context: str | None = None,
    tool_schemas: Iterable[dict] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict] | None = None,
) -> RuntimeContextAppendixResult:
    """Render runtime context blocks for custom prompt fallback paths."""

    envelope = RuntimeResolver().resolve_trace_envelope(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=tool_schemas,
        allowed_tool_names=allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )
    context_blocks = await resolve_runtime_context_blocks(
        db,
        request,
        envelope,
        legacy_extra_context=legacy_extra_context,
        manual_skill_refs=skill_refs,
    )
    context_section = render_context_blocks(context_blocks) or None
    return RuntimeContextAppendixResult(
        context_section=context_section,
        context_blocks=tuple(context_blocks),
        request=request,
        envelope=envelope,
    )


async def runtime_prepare_prompt_appendix_for_turn(
    db: AsyncSession | None,
    *,
    request: AIRuntimeRequest,
    legacy_runtime_profile: str | None = None,
    runtime_profile: str | None = None,
    agent_id: str | None = None,
    bound_tool_names: set[str] | None = None,
    is_master: bool = False,
    mcp_allowed_names: set[str] | None = None,
    mode: RuntimePromptMode = "full",
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    legacy_extra_context: str | None = None,
    initial_extra_context: str | None = None,
    configured_tool_names: Iterable[str] | str | None = None,
    tool_schemas: Iterable[dict] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    extra_tool_schemas: Iterable[dict] | None = None,
    extra_allowed_tool_names: Iterable[str] | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict] | None = None,
    disable_tools: bool = False,
) -> RuntimePromptAppendixResult:
    """Prepare runtime-owned context/skill appendix for custom system prompts."""

    prepared = await runtime_prepare_prompt_context_for_turn(
        db,
        request=request,
        legacy_runtime_profile=legacy_runtime_profile,
        runtime_profile=runtime_profile,
        agent_id=agent_id,
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        mode=mode,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
        legacy_extra_context=legacy_extra_context,
        initial_extra_context=initial_extra_context,
        configured_tool_names=configured_tool_names,
        tool_schemas=tool_schemas,
        allowed_tool_names=allowed_tool_names,
        extra_tool_schemas=extra_tool_schemas,
        extra_allowed_tool_names=extra_allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
        disable_tools=disable_tools,
    )
    context_section = prepared.context.extra_context or None
    skill_section = await available_skills_section(prepared.context)
    prompt_appendix = "\n\n".join(
        part for part in (context_section, skill_section) if part
    )
    return RuntimePromptAppendixResult(
        prompt_appendix=prompt_appendix,
        context_section=context_section,
        skill_section=skill_section,
        context=prepared.context,
        request=prepared.request,
        envelope=prepared.envelope,
        tool_schemas=prepared.tool_schemas,
        tool_names=prepared.tool_names,
        allowed_tool_names=prepared.allowed_tool_names,
    )


async def runtime_prepare_prompt_context_for_turn(
    db: AsyncSession | None,
    *,
    request: AIRuntimeRequest,
    legacy_runtime_profile: str | None = None,
    runtime_profile: str | None = None,
    agent_id: str | None = None,
    bound_tool_names: set[str] | None = None,
    is_master: bool = False,
    mcp_allowed_names: set[str] | None = None,
    mode: RuntimePromptMode = "full",
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    legacy_extra_context: str | None = None,
    initial_extra_context: str | None = None,
    configured_tool_names: Iterable[str] | str | None = None,
    tool_schemas: Iterable[dict] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    extra_tool_schemas: Iterable[dict] | None = None,
    extra_allowed_tool_names: Iterable[str] | None = None,
    blocked_tool_names: Iterable[str] | None = None,
    skill_refs: Iterable[dict] | None = None,
    disable_tools: bool = False,
) -> RuntimePreparedPromptContext:
    """Prepare a ChatContext plus runtime envelope without rendering a prompt."""

    effective_agent_id = agent_id if agent_id is not None else request.agent_id
    effective_runtime_profile = runtime_profile_name_for_surface(
        request.surface,
        runtime_profile,
    )
    ctx = ChatContext(
        db=db,
        entity_id=request.entity_id,
        user_id=request.user_id,
        agent_id=effective_agent_id,
        workspace_id=request.workspace_id,
        conversation_id=request.conversation_id,
        task_id=request.task_id,
        thread_ref_kind=request.thread_ref_kind,
        thread_ref_id=request.thread_ref_id,
        runtime_profile=effective_runtime_profile,
        legacy_runtime_profile=legacy_runtime_profile,
        runtime_surface=request.surface.value,
        runtime_profile_name=effective_runtime_profile,
        active_user_message=active_user_message or request.input_preview,
        manual_skill_selected=manual_skill_selected,
        mode=mode,
    )
    await ctx.resolve()
    if initial_extra_context:
        ctx.extra_context = initial_extra_context

    if disable_tools:
        resolved_tools = []
        resolved_allowed_tool_names = set()
        runtime_set_tools_for_prompt_context(
            ctx,
            tools=resolved_tools,
            allowed_tool_names=resolved_allowed_tool_names,
        )
    elif tool_schemas is not None:
        resolved_tools = list(tool_schemas or [])
        resolved_allowed_tool_names = (
            {str(name) for name in allowed_tool_names if str(name or "").strip()}
            if allowed_tool_names is not None
            else None
        )
        runtime_set_tools_for_prompt_context(
            ctx,
            tools=resolved_tools,
            allowed_tool_names=resolved_allowed_tool_names,
        )
        resolved_allowed_tool_names = set(ctx.allowed_tool_names)
    elif configured_tool_names is not None:
        resolved_tools, resolved_allowed_tool_names = runtime_populate_named_tools_for_prompt_context(
            ctx,
            tool_names=configured_tool_names,
        )
    elif tool_schemas is None:
        resolved_tools, resolved_allowed_tool_names = runtime_populate_tools_for_prompt_context(
            ctx,
            agent_id=effective_agent_id,
            bound_tool_names=bound_tool_names,
            is_master=is_master,
            mcp_allowed_names=mcp_allowed_names,
            legacy_tool_profile=legacy_runtime_profile,
        )

    resolved_tools, resolved_allowed_tool_names = _apply_extra_tool_surface(
        tools=resolved_tools,
        allowed_tool_names=resolved_allowed_tool_names,
        extra_tool_schemas=extra_tool_schemas,
        extra_allowed_tool_names=extra_allowed_tool_names,
    )
    runtime_set_tools_for_prompt_context(
        ctx,
        tools=resolved_tools,
        allowed_tool_names=set(resolved_allowed_tool_names or set()),
    )

    runtime_skill_refs = list(skill_refs or [])
    if runtime_skill_refs:
        resolved_tools, resolved_allowed_tool_names = runtime_apply_manual_skill_tool_surface(
            tools=resolved_tools,
            allowed_tool_names=set(resolved_allowed_tool_names or set()),
            manual_skill_refs=runtime_skill_refs,
            message=active_user_message or request.input_preview or "",
            get_schema=runtime_tool_schema,
            disable_tools=disable_tools,
        )
        runtime_set_tools_for_prompt_context(
            ctx,
            tools=resolved_tools,
            allowed_tool_names=set(resolved_allowed_tool_names or set()),
        )

    runtime_surface_result = RuntimeResolver().resolve_tool_surface(
        request,
        legacy_runtime_profile=legacy_runtime_profile,
        tool_schemas=resolved_tools,
        allowed_tool_names=resolved_allowed_tool_names,
        blocked_tool_names=blocked_tool_names,
        skill_refs=skill_refs,
    )
    ctx.tools = runtime_surface_result.tool_schemas
    ctx.tool_names = [
        tool.get("function", {}).get("name") or tool.get("name", "?")
        for tool in ctx.tools
    ]
    ctx.allowed_tool_names = set(runtime_surface_result.allowed_tool_names)
    ctx.runtime_surface = request.surface.value
    ctx.runtime_envelope = runtime_surface_result.envelope
    ctx.runtime_profile_name = ctx.runtime_envelope.profile.value
    ctx.runtime_context_blocks = await resolve_runtime_context_blocks(
        db,
        request,
        ctx.runtime_envelope,
        legacy_extra_context=legacy_extra_context if legacy_extra_context is not None else ctx.extra_context,
        manual_skill_refs=skill_refs,
    )
    ctx.extra_context = render_context_blocks(ctx.runtime_context_blocks)
    await populate_runtime_skill_descriptors(db, ctx, ctx.runtime_envelope)
    envelope = ctx.runtime_envelope
    return RuntimePreparedPromptContext(
        context=ctx,
        request=request,
        envelope=envelope,
        tool_schemas=list(ctx.tools),
        tool_names=tuple(ctx.tool_names),
        allowed_tool_names=set(ctx.allowed_tool_names),
    )


def _apply_extra_tool_surface(
    *,
    tools: list[dict],
    allowed_tool_names: Iterable[str] | None,
    extra_tool_schemas: Iterable[dict] | None,
    extra_allowed_tool_names: Iterable[str] | None,
) -> tuple[list[dict], set[str]]:
    """Merge caller-owned dynamic tool schemas before RuntimeResolver gates them."""
    merged_tools = list(tools or [])
    merged_allowed = {
        str(name)
        for name in (allowed_tool_names or ())
        if str(name or "").strip()
    }
    extras = list(extra_tool_schemas or ())
    if not extras and extra_allowed_tool_names is None:
        return merged_tools, merged_allowed

    existing_names = {
        name
        for name in (runtime_prompt_tool_name(tool) for tool in merged_tools)
        if name
    }
    for tool in extras:
        name = runtime_prompt_tool_name(tool)
        if name and name in existing_names:
            continue
        merged_tools.append(tool)
        if name:
            existing_names.add(name)

    if extra_allowed_tool_names is None:
        merged_allowed.update(
            name for name in (runtime_prompt_tool_name(tool) for tool in extras) if name
        )
    else:
        merged_allowed.update(
            str(name)
            for name in extra_allowed_tool_names
            if str(name or "").strip()
        )

    return merged_tools, merged_allowed
