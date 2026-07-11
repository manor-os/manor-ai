from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.approvals import RuntimeApprovalMiddleware, RuntimeApprovalRequest
from packages.core.ai.runtime.artifacts import (
    runtime_artifact_tracking_scope,
    runtime_record_tool_result_artifacts,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.events import RuntimeEventType
from packages.core.ai.runtime.middleware import RuntimeMiddleware, apply_runtime_middleware
from packages.core.ai.runtime.policies import RuntimeToolPolicyDecision, check_runtime_tool_policy
from packages.core.ai.runtime.sources import (
    RUNTIME_AGENTIC_LOOP_SOURCE,
    RUNTIME_CHANNEL_SOURCE,
    RUNTIME_CHAT_SOURCE,
    RUNTIME_SKILL_SOURCE,
    RUNTIME_SUBAGENT_SOURCE,
    RUNTIME_WORKER_SOURCE,
    RUNTIME_WORKFLOW_SOURCE,
    RUNTIME_WORKSPACE_ARCHITECT_SOURCE,
)
from packages.core.ai.runtime.subagents import RuntimeSubAgentDecision, SubAgentSpec, runtime_select_subagent
from packages.core.ai.runtime.traces import RuntimeTrace


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]
RuntimeDynamicToolHandler = Callable[[dict[str, Any]], Awaitable[str] | str]
RUNTIME_CHAT_AGENT_BILLING_SOURCE = RUNTIME_CHAT_SOURCE
RUNTIME_CHANNEL_AGENT_BILLING_SOURCE = RUNTIME_CHANNEL_SOURCE
RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE = RUNTIME_WORKFLOW_SOURCE
RUNTIME_SKILL_AGENT_BILLING_SOURCE = RUNTIME_SKILL_SOURCE
RUNTIME_WORKSPACE_ARCHITECT_BILLING_SOURCE = RUNTIME_WORKSPACE_ARCHITECT_SOURCE
RUNTIME_WORKSPACE_ARCHITECT_TEMPERATURE = 0.3
RUNTIME_WORKER_SUBAGENT_BILLING_SOURCE = RUNTIME_WORKER_SOURCE


class RuntimeHarness:
    """Thin harness facade used while existing agentic_loop remains in place."""

    def __init__(self, envelope: RuntimeEnvelope) -> None:
        self.envelope = envelope
        self.trace = RuntimeTrace(envelope)
        self.approval_middleware = RuntimeApprovalMiddleware()

    @classmethod
    async def create(
        cls,
        envelope: RuntimeEnvelope,
        *,
        middleware: tuple[RuntimeMiddleware, ...] = (),
    ) -> "RuntimeHarness":
        return cls(await apply_runtime_middleware(envelope, middleware))

    def record_event(self, event_type: RuntimeEventType, **data: Any) -> None:
        self.trace.record(event_type, **data)
        events = self.envelope.metadata.setdefault("runtime_events", [])
        events.append({"type": event_type, **data})

    def check_tool_call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> RuntimeToolPolicyDecision:
        decision = check_runtime_tool_policy(
            envelope=self.envelope,
            tool_name=name,
            arguments=args,
        )
        if not decision.allowed:
            self.record_event(
                "tool_denied",
                tool_name=name,
                code=decision.code,
                reason=decision.reason,
            )
        return decision

    def record_tool_block_result(self, name: str, result: str | dict[str, Any]) -> None:
        """Convert a blocking tool payload into a standard runtime event."""
        event = self.approval_middleware.tool_block_event(name, result)
        if event is not None:
            self.record_event(event.type, **event.data)

    def select_subagent(self, name: str | None = None) -> RuntimeSubAgentDecision:
        """Select a subagent spec visible to this runtime envelope."""
        decision = runtime_select_subagent(self.envelope, name=name)
        if not decision.allowed:
            self.record_event(
                "subagent_denied",
                subagent_name=name,
                code=decision.code,
                reason=decision.reason,
            )
        return decision

    async def guard_tool_action(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        entity_id: str,
        user_id: str,
        workspace_id: str | None,
        conversation_id: str | None,
        task_id: str | None = None,
    ) -> str | None:
        """Run approval middleware and record any blocking result."""
        decision = await self.approval_middleware.guard_request(
            RuntimeApprovalRequest(
                tool_name=tool_name,
                arguments=arguments,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                envelope=self.envelope,
            )
        )
        if decision.event is not None:
            self.record_event(decision.event.type, **decision.event.data)
        return decision.blocked_result

    def wrap_tool_executor(self, executor: ToolExecutor) -> ToolExecutor:
        async def _wrapped(name: str, args: dict[str, Any]) -> str:
            decision = self.check_tool_call(name, args)
            if not decision.allowed:
                return decision.to_tool_result()
            self.record_event("tool_start", tool_name=name)
            try:
                result = await executor(name, args)
            except Exception as exc:
                self.record_event("error", tool_name=name, message=str(exc))
                raise
            self.record_event("tool_end", tool_name=name)
            return result

        return _wrapped


@dataclass(frozen=True)
class RuntimePreparedSubAgentRun:
    """Runtime-owned execution guard for one subagent loop."""

    spec: SubAgentSpec | None
    max_rounds: int | None
    harness: RuntimeHarness | None = None

    def record_start(self) -> None:
        if self.harness is None or self.spec is None:
            return
        self.harness.record_event(
            "subagent_start",
            subagent_name=self.spec.name,
            max_steps=self.spec.max_steps,
        )

    def record_end(self, rounds: int | None = None) -> None:
        if self.harness is None or self.spec is None:
            return
        self.harness.record_event(
            "subagent_end",
            subagent_name=self.spec.name,
            rounds=rounds,
        )


def runtime_prepare_subagent_run(
    envelope: RuntimeEnvelope | None,
    *,
    requested_name: str | None = None,
    requested_max_rounds: Any = None,
) -> RuntimePreparedSubAgentRun:
    """Select and bound a runtime-visible subagent execution."""

    if envelope is None:
        max_rounds = int(requested_max_rounds) if requested_max_rounds else None
        return RuntimePreparedSubAgentRun(
            spec=None,
            max_rounds=max_rounds,
            harness=None,
        )

    harness = RuntimeHarness(envelope)
    decision = harness.select_subagent(str(requested_name) if requested_name else None)
    if not decision.allowed or decision.spec is None:
        raise RuntimeError(decision.reason or decision.code or "Subagent execution is not allowed in this runtime.")

    spec = decision.spec
    max_rounds = int(requested_max_rounds) if requested_max_rounds else spec.max_steps
    max_rounds = max(1, min(max_rounds, spec.max_steps))
    return RuntimePreparedSubAgentRun(
        spec=spec,
        max_rounds=max_rounds,
        harness=harness,
    )


@dataclass(frozen=True)
class RuntimeSubAgentLoopResult:
    """Result bundle returned by the runtime subagent loop adapter."""

    result: Any
    run: RuntimePreparedSubAgentRun


async def runtime_execute_agentic_loop(
    *,
    runtime_envelope: RuntimeEnvelope | None,
    system_prompt: str,
    user_message: str | list[dict[str, Any]],
    tools: list[dict[str, Any]],
    entity_id: str,
    agent_id: str | None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    manual_skill_slugs: Iterable[str] | None = None,
    legacy_tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    billing_source: str = RUNTIME_AGENTIC_LOOP_SOURCE,
    max_rounds: int | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    on_tool_start: Callable[[str, dict[str, Any]], Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    on_llm_call: Callable[..., Any] | None = None,
    stream_handler: Callable[..., Any] | None = None,
    metadata: dict[str, Any] | None = None,
    forced_tool_calls: list[dict[str, Any]] | None = None,
    terminal_tool_result_policy: dict[str, Any] | None = None,
    dynamic_tool_handlers: Mapping[str, RuntimeDynamicToolHandler] | None = None,
    tool_executor: ToolExecutor | None = None,
    tool_schema_resolver: Callable[[str], dict[str, Any] | None] | None = None,
) -> Any:
    """Run an agentic loop with Runtime-owned tool execution plumbing."""

    from packages.core.ai.agentic_loop import agentic_loop
    from packages.core.ai.runtime.billing import runtime_ensure_billing_context
    from packages.core.ai.runtime.completions import runtime_resolve_text_completion_route
    from packages.core.ai.runtime.tool_execution import runtime_execute_scoped_dynamic_tool_handler
    from packages.core.ai.runtime.tool_registry import runtime_execute_tool, runtime_tool_schema
    from packages.core.ai.runtime.tool_schema import runtime_tool_schema_resolver

    resolved_model, resolved_metadata, resolved_byok = await runtime_resolve_text_completion_route(
        entity_id=entity_id,
        user_id=user_id,
        source=billing_source,
        model=model,
        metadata=metadata,
    )
    billing_kwargs: dict[str, Any] = {
        "user_id": user_id,
        "agent_id": agent_id,
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
    }
    if resolved_byok is not None:
        billing_kwargs["byok"] = resolved_byok

    runtime_ensure_billing_context(
        entity_id,
        source=billing_source,
        **billing_kwargs,
    )

    allowed_tool_set = (
        {str(tool_name) for tool_name in allowed_tool_names if str(tool_name or "").strip()}
        if allowed_tool_names is not None
        else None
    )
    resolved_tool_schema = tool_schema_resolver or runtime_tool_schema_resolver(
        get_schema=runtime_tool_schema,
        allowed_tool_names=allowed_tool_set,
    )
    tool_context_message = active_user_message
    if tool_context_message is None and isinstance(user_message, str):
        tool_context_message = user_message
    dynamic_handlers = dict(dynamic_tool_handlers or {})

    async def _runtime_tool_executor(name: str, args: dict[str, Any]) -> str:
        tool_name = str(name or "")
        arguments = args if isinstance(args, dict) else {}
        handler = dynamic_handlers.get(tool_name)
        if handler is not None:
            return await runtime_execute_scoped_dynamic_tool_handler(
                tool_name=tool_name,
                arguments=arguments,
                handler=handler,
                runtime_envelope=runtime_envelope,
            )
        return await runtime_execute_tool(
            tool_name,
            arguments,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            active_user_message=tool_context_message,
            manual_skill_selected=manual_skill_selected,
            manual_skill_slugs=list(manual_skill_slugs or []),
            legacy_tool_profile=legacy_tool_profile,
            allowed_tool_names=allowed_tool_set,
            llm_metadata=resolved_metadata,
            llm_model=resolved_model,
            runtime_envelope=runtime_envelope,
        )

    base_tool_executor = tool_executor or _runtime_tool_executor

    async def _artifact_tracking_tool_executor(name: str, args: dict[str, Any]) -> str:
        result = await base_tool_executor(name, args)
        runtime_record_tool_result_artifacts(result)
        return result

    loop_kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_message": user_message,
        "tools": tools,
        "tool_executor": _artifact_tracking_tool_executor,
        "model": resolved_model,
        "initial_messages": initial_messages,
        "on_tool_start": on_tool_start,
        "on_tool_end": on_tool_end,
        "on_llm_call": on_llm_call,
        "stream_handler": stream_handler,
        "metadata": resolved_metadata,
        "tool_schema_resolver": resolved_tool_schema,
        "forced_tool_calls": forced_tool_calls,
        "terminal_tool_result_policy": terminal_tool_result_policy,
    }
    if temperature is not None:
        loop_kwargs["temperature"] = temperature
    if max_rounds is not None:
        loop_kwargs["max_rounds"] = max_rounds
    with runtime_artifact_tracking_scope():
        return await agentic_loop(**loop_kwargs)


async def runtime_execute_channel_agent_loop(**loop_kwargs: Any) -> Any:
    """Run a channel-bound agentic loop with Runtime-owned billing source."""

    return await runtime_execute_agentic_loop(
        **loop_kwargs,
        billing_source=RUNTIME_CHANNEL_AGENT_BILLING_SOURCE,
    )


async def runtime_execute_chat_agent_loop(**loop_kwargs: Any) -> Any:
    """Run a primary chat agentic loop with Runtime-owned billing source."""

    return await runtime_execute_agentic_loop(
        **loop_kwargs,
        billing_source=RUNTIME_CHAT_AGENT_BILLING_SOURCE,
    )


async def runtime_execute_workflow_agent_loop(**loop_kwargs: Any) -> Any:
    """Run a workflow agentic loop with Runtime-owned billing source."""

    return await runtime_execute_agentic_loop(
        **loop_kwargs,
        billing_source=RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE,
    )


async def runtime_execute_skill_agent_loop(**loop_kwargs: Any) -> Any:
    """Run a prompt-skill agentic loop with Runtime-owned billing source."""

    return await runtime_execute_agentic_loop(
        **loop_kwargs,
        billing_source=RUNTIME_SKILL_AGENT_BILLING_SOURCE,
    )


async def runtime_execute_workspace_architect_loop(**loop_kwargs: Any) -> Any:
    """Run Workspace Architect with Runtime-owned LLM defaults."""

    loop_kwargs.setdefault("temperature", RUNTIME_WORKSPACE_ARCHITECT_TEMPERATURE)
    return await runtime_execute_agentic_loop(
        **loop_kwargs,
        billing_source=RUNTIME_WORKSPACE_ARCHITECT_BILLING_SOURCE,
    )


async def runtime_execute_subagent_loop(
    *,
    runtime_envelope: RuntimeEnvelope | None,
    system_prompt: str,
    user_message: str | list[dict[str, Any]],
    tools: list[dict[str, Any]],
    entity_id: str,
    agent_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    legacy_tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
    billing_source: str = RUNTIME_SUBAGENT_SOURCE,
    requested_name: str | None = None,
    requested_max_rounds: Any = None,
    default_max_rounds: int = 20,
) -> RuntimeSubAgentLoopResult:
    """Run a bounded subagent loop through the Runtime Harness adapters."""

    subagent_run = runtime_prepare_subagent_run(
        runtime_envelope,
        requested_name=requested_name,
        requested_max_rounds=requested_max_rounds,
    )
    subagent_run.record_start()
    result = await runtime_execute_agentic_loop(
        runtime_envelope=runtime_envelope,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        active_user_message=active_user_message,
        legacy_tool_profile=legacy_tool_profile,
        allowed_tool_names=allowed_tool_names,
        model=model,
        max_rounds=subagent_run.max_rounds or default_max_rounds,
        metadata=metadata,
        billing_source=billing_source,
    )
    subagent_run.record_end(rounds=getattr(result, "rounds", None))
    return RuntimeSubAgentLoopResult(result=result, run=subagent_run)


async def runtime_execute_worker_subagent_loop(**loop_kwargs: Any) -> RuntimeSubAgentLoopResult:
    """Run an InternalWorker subagent loop with Runtime-owned billing source."""

    return await runtime_execute_subagent_loop(
        **loop_kwargs,
        billing_source=RUNTIME_WORKER_SUBAGENT_BILLING_SOURCE,
    )


def runtime_wrap_tool_executor(
    envelope: RuntimeEnvelope,
    executor: ToolExecutor,
) -> ToolExecutor:
    """Return an executor guarded and traced by a RuntimeEnvelope."""

    return RuntimeHarness(envelope).wrap_tool_executor(executor)
