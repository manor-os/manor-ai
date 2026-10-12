from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from packages.core.ai.runtime.approvals import (
    RuntimeApprovalDecision,
    RuntimeApprovalMiddleware,
    RuntimeApprovalRequest,
)
from packages.core.ai.runtime.artifacts import (
    runtime_artifact_tracking_scope,
    runtime_record_tool_result_artifacts,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.final_response import RuntimeFinalResponseContract
from packages.core.ai.runtime.events import RuntimeEventType
from packages.core.ai.runtime.middleware import RuntimeMiddleware, apply_runtime_middleware
from packages.core.ai.runtime.policies import RuntimeToolPolicyDecision, check_runtime_tool_policy
from packages.core.ai.runtime.run_state import (
    LOADED_GATEWAY_TOOLS_LIMIT,
    LOOP_SIGNAL_KEYS,
    RUN_STATE_VERSION_KEY,
    RuntimeRunState,
    RuntimeRunStateSlots,
    runtime_bounded_tool_names,
    runtime_run_state_persisted_keys,
)
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
from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
from packages.core.ai.runtime.traces import RuntimeTrace


logger = logging.getLogger(__name__)
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
        step_id: str | None = None,
    ) -> str | None:
        """Run approval middleware and record any blocking result."""
        decision = await self.guard_tool_request(
            RuntimeApprovalRequest(
                tool_name=tool_name,
                arguments=arguments,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                step_id=step_id,
                envelope=self.envelope,
            )
        )
        return decision.blocked_result

    async def guard_tool_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> RuntimeApprovalDecision:
        """Authorize one request and retain its exact typed classification."""

        decision = await self.approval_middleware.guard_request(request)
        if decision.event is not None:
            self.record_event(decision.event.type, **decision.event.data)
        return decision

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


def _checkpoint_archive_id(message: Any) -> str | None:
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX

    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.startswith(CHECKPOINT_PREFIX):
        return None
    try:
        payload = json.loads(content.split("\n", 2)[2])
    except (ValueError, IndexError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    archive_id = payload.get("archive_id")
    if isinstance(archive_id, str) and archive_id.strip():
        return archive_id.strip()
    return None


def _runtime_has_context_archive_id(*groups: Any) -> bool:
    """True when a window-compaction checkpoint already names an archive."""

    for group in groups:
        if isinstance(group, list):
            if any(_checkpoint_archive_id(message) for message in group):
                return True
            continue
        if isinstance(group, dict):
            if _checkpoint_archive_id(group):
                return True
            messages = group.get("messages")
            if isinstance(messages, list) and _runtime_has_context_archive_id(messages):
                return True
    return False


def _round_observer_arity(on_round_end: Callable[..., Any]) -> int:
    try:
        observer_parameters = inspect.signature(on_round_end).parameters.values()
        return 5 if any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in observer_parameters
        ) else sum(
            parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in observer_parameters
        )
    except (TypeError, ValueError):
        return 2


def runtime_run_state_round_observer(
    on_round_end: Callable[..., Any],
    *,
    run_state_slots: RuntimeRunStateSlots | None = None,
) -> Callable[..., Awaitable[None]]:
    """Wrap a host's round observer so it receives the run state snapshot.

    The fifth argument is ``RuntimeRunState.snapshot()``: every persisted
    field (usage including nested agents' usage) plus the host's registered
    slots captured at this round boundary, and any declared loop signals
    (``LOOP_SIGNAL_KEYS``), which a host strips before persisting.
    """

    from packages.core.ai.runtime.nested_usage import runtime_snapshot_nested_usage_collection

    observer_arity = _round_observer_arity(on_round_end)

    async def _round_end_with_run_state(
        rounds: int,
        messages: list[dict[str, Any]],
        usage: dict[str, Any],
        tool_calls_made: list[str],
        runtime_state: dict[str, Any] | None = None,
    ) -> None:
        from packages.core.ai.agentic_loop import _add_usage

        checkpoint_usage = dict(usage or {})
        for nested_usage in runtime_snapshot_nested_usage_collection():
            _add_usage(checkpoint_usage, nested_usage)
        if observer_arity >= 5:
            loop_state = runtime_state if isinstance(runtime_state, dict) else {}
            if RUN_STATE_VERSION_KEY in loop_state:
                # The loop's snapshot is already a private copy: complete it
                # in place (declared keys only) instead of copying it again.
                round_state = {
                    key: value for key, value in loop_state.items()
                    if key in runtime_run_state_persisted_keys()
                }
                round_state["usage"] = checkpoint_usage
                if run_state_slots is not None:
                    round_state.update(run_state_slots.capture())
            else:
                # A loop without its own snapshot: build one from the round.
                round_state = RuntimeRunState(
                    messages=list(messages or []),
                    usage=checkpoint_usage,
                    rounds=rounds,
                    tool_calls_made=list(tool_calls_made),
                    tool_evidence=dict(loop_state.get("tool_evidence") or {}),
                ).snapshot(run_state_slots)
            # Declared loop signals reach the observer; a host persists the
            # state without them (runtime_run_state_persistable).
            round_state.update({key: loop_state[key] for key in LOOP_SIGNAL_KEYS if key in loop_state})
            callback_result = on_round_end(
                rounds,
                messages,
                checkpoint_usage,
                list(tool_calls_made),
                round_state,
            )
        elif observer_arity >= 4:
            callback_result = on_round_end(
                rounds,
                messages,
                checkpoint_usage,
                list(tool_calls_made),
            )
        else:
            callback_result = on_round_end(rounds, messages)
        if inspect.isawaitable(callback_result):
            await callback_result

    return _round_end_with_run_state


def _runtime_gateway_names_to_restore(
    *,
    run_state: RuntimeRunState | None,
    runtime_checkpoint: Mapping[str, Any] | None,
    tools: list[dict[str, Any]],
    gateway_tool_schemas: list[dict] | None,
) -> list[str]:
    """Tools a resumed run had loaded through discovery, not yet present.

    Only names recorded from real discovery results (``loaded_gateway_tools``)
    are candidates, and only for a surface that can discover tools at all
    (``search_tools`` or ``call_tool``): a name the model merely called is
    never restored, and a host without discovery restores nothing.
    """

    from packages.core.ai.runtime.tool_gateway import CALL_TOOL_NAME, runtime_schema_tool_name

    declared = {runtime_schema_tool_name(schema) for schema in tools}
    if not declared & {"search_tools", CALL_TOOL_NAME}:
        return []
    present = declared | {runtime_schema_tool_name(schema) for schema in gateway_tool_schemas or ()}
    names: list[str] = []
    states = [run_state] if run_state is not None else []
    if isinstance(runtime_checkpoint, Mapping):
        # A parked RuntimeAgentCheckpoint records the same loaded set.
        states.append(RuntimeRunState.restore(runtime_checkpoint))
    for state in states:
        names.extend(name for name in state.loaded_gateway_tools if name not in present)
    return runtime_bounded_tool_names(names)


async def _runtime_execute_agentic_loop(
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
    step_id: str | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    manual_skill_ids: Iterable[str] | None = None,
    manual_skill_slugs: Iterable[str] | None = None,
    tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    model: str | None = None,
    final_model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    billing_source: str = RUNTIME_AGENTIC_LOOP_SOURCE,
    max_rounds: int | None = None,
    hard_max_rounds: int | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    initial_usage: dict[str, Any] | None = None,
    initial_rounds: int = 0,
    initial_tool_calls_made: list[str] | None = None,
    initial_tool_evidence: dict[str, Any] | None = None,
    tool_evidence: Any = None,
    on_round_end: Callable[..., Any] | None = None,
    on_round_end_strict: bool = False,
    carry_previous_checkpoint: bool = True,
    tool_result_max_chars: int | None = None,
    turn_context: str | None = None,
    gateway_tool_schemas: list[dict] | None = None,
    on_tool_start: Callable[[str, dict[str, Any]], Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    on_llm_call: Callable[..., Any] | None = None,
    on_llm_call_before: Callable[..., Any] | None = None,
    on_llm_usage_settled: Callable[..., Any] | None = None,
    on_context_compacted: Callable[..., Any] | None = None,
    on_context_checkpoint: Callable[..., Any] | None = None,
    context_archive_reader: RuntimeDynamicToolHandler | None = None,
    stream_handler: Callable[..., Any] | None = None,
    is_cancelled: Callable[[], Any] | None = None,
    metadata: dict[str, Any] | None = None,
    forced_tool_calls: list[dict[str, Any]] | None = None,
    terminal_tool_result_policy: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    non_success_outcomes: dict[str, Any] | None = None,
    final_response_contract: RuntimeFinalResponseContract | None = None,
    dynamic_tool_handlers: Mapping[str, RuntimeDynamicToolHandler] | None = None,
    capability_refresh_tool_names: Iterable[str] = (),
    runtime_tool_context: Mapping[str, Any] | None = None,
    tool_executor: ToolExecutor | None = None,
    tool_schema_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    performance_run_id: str | None = None,
    runtime_run_id: str | None = None,
    runtime_checkpoint: dict[str, Any] | None = None,
    prompt_context: Any = None,
    runtime_cancel_checker: Callable[[], Any] | None = None,
    human_input_suspension: bool = False,
    hitl_resume_checkpoint: bool = False,
    run_state: RuntimeRunState | None = None,
    run_state_slots: RuntimeRunStateSlots | None = None,
) -> Any:
    """Run an agentic loop with Runtime-owned tool execution plumbing.

    ``run_state`` resumes a durable host's transcript, usage, rounds, tool
    evidence and web sources (in place of the ``initial_*`` arguments).
    ``run_state_slots`` are the host's registered slots: each round observer
    receives a snapshot that already holds them. Tools the resumed run had
    loaded through discovery (from ``run_state`` or a parked
    ``runtime_checkpoint``) are re-authorized here by exact discovery.
    """

    if run_state is not None:
        if (
            initial_messages is not None or initial_usage is not None or initial_rounds
            or initial_tool_calls_made is not None or initial_tool_evidence is not None
        ):
            raise ValueError("A resumed run takes its state from run_state or initial_*, not both")
        resumed = run_state.resume_loop_kwargs()
        initial_messages = resumed["initial_messages"]
        initial_usage = resumed["initial_usage"]
        initial_rounds = resumed["initial_rounds"]
        initial_tool_calls_made = resumed["initial_tool_calls_made"]
        initial_tool_evidence = run_state.tool_evidence or None

    from packages.core.ai.agentic_loop import _add_usage, agentic_loop
    from packages.core.ai.runtime.billing import runtime_ensure_billing_context
    from packages.core.ai.runtime.completions import runtime_resolve_text_completion_route
    from packages.core.ai.runtime.response_language import (
        resolve_runtime_response_language,
        response_language_messages,
        response_language_prompt,
        runtime_response_language_scope,
    )
    from packages.core.ai.runtime.nested_usage import (
        runtime_begin_nested_usage_collection,
        runtime_finish_nested_usage_collection,
    )
    from packages.core.ai.runtime.artifacts import runtime_web_source_checkpoint_state
    from packages.core.ai.runtime.tool_execution import runtime_execute_scoped_dynamic_tool_handler
    from packages.core.ai.runtime.tool_registry import runtime_execute_tool, runtime_tool_schema
    from packages.core.ai.runtime.tool_schema import runtime_tool_schema_resolver

    language = await resolve_runtime_response_language(
        entity_id=entity_id,
        user_id=user_id or getattr(runtime_envelope, "user_id", None),
        profile=getattr(runtime_envelope, "profile", None),
        surface=getattr(runtime_envelope, "surface", None),
        source=billing_source,
        db=getattr(prompt_context, "db", None),
        user=getattr(prompt_context, "user", None),
    )
    system_prompt = response_language_prompt(system_prompt, language)
    if runtime_checkpoint and isinstance(runtime_checkpoint.get("messages"), list):
        runtime_checkpoint = {
            **runtime_checkpoint,
            "messages": response_language_messages(runtime_checkpoint["messages"], language),
        }

    resolved_model, resolved_metadata, resolved_byok = await runtime_resolve_text_completion_route(
        entity_id=entity_id,
        user_id=user_id,
        source=billing_source,
        model=model,
        metadata=metadata,
    )
    resolved_final_model: str | None = None
    resolved_final_metadata: dict[str, Any] | None = None
    if final_model:
        (
            resolved_final_model,
            resolved_final_metadata,
            _,
        ) = await runtime_resolve_text_completion_route(
            entity_id=entity_id,
            user_id=user_id,
            source=billing_source,
            model=final_model,
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
    dynamic_handlers = dict(dynamic_tool_handlers or {})
    previous_checkpoint = None
    if runtime_run_id and user_id and conversation_id and on_context_compacted is None:
        from packages.core.services.runtime_context_archive import runtime_context_archive_callbacks
        on_context_compacted, context_archive_reader, on_context_checkpoint, previous_checkpoint = await runtime_context_archive_callbacks(
            run_id=runtime_run_id, entity_id=entity_id, user_id=user_id,
            conversation_id=conversation_id, workspace_id=workspace_id, agent_id=agent_id,
        )
        if not carry_previous_checkpoint:
            # A transcript-backed chat already holds every earlier run verbatim.
            previous_checkpoint = None
        if previous_checkpoint and not runtime_checkpoint:
            # The carried checkpoint keeps earlier runs' tool calls/results that
            # history no longer renders; history itself is reloaded below it.
            from packages.core.ai.runtime.context_compaction import dedupe_carried_checkpoint
            previous_checkpoint = dedupe_carried_checkpoint(previous_checkpoint, initial_messages or [])
            if previous_checkpoint:
                initial_messages = [previous_checkpoint, *(initial_messages or [])]
    if (
        context_archive_reader is not None
        and tools
        and "read_context_archive" not in set(getattr(runtime_envelope, "blocked_tool_names", ()) or ())
        and _runtime_has_context_archive_id(previous_checkpoint, runtime_checkpoint, initial_messages)
    ):
        from packages.core.services.runtime_context_archive import CONTEXT_ARCHIVE_TOOL
        tools = [*tools, CONTEXT_ARCHIVE_TOOL]
        dynamic_handlers["read_context_archive"] = context_archive_reader
        if allowed_tool_set is not None:
            allowed_tool_set.add("read_context_archive")
    from packages.core.ai.runtime.human_input import (
        HUMAN_INPUT_MODE_END_TURN,
        human_input_mode_from_kwargs,
    )

    # Registration is not a continuation capability. Keep prompt schemas,
    # progressive discovery and execution on the same per-run scope. Task
    # hosts with an explicit end-turn bridge/custom handler retain their tool.
    can_request_input = (
        human_input_suspension
        or human_input_mode_from_kwargs(dict(runtime_tool_context or {})) == HUMAN_INPUT_MODE_END_TURN
        or "request_input" in dynamic_handlers
    )
    if not can_request_input:
        tools = [schema for schema in tools if (schema.get("function") or {}).get("name") != "request_input"]
        if allowed_tool_set is not None:
            allowed_tool_set.discard("request_input")
        if isinstance(runtime_envelope, RuntimeEnvelope):
            runtime_envelope = replace(
                runtime_envelope,
                tool_names=tuple(name for name in runtime_envelope.tool_names if name != "request_input"),
                blocked_tool_names=tuple(dict.fromkeys((*runtime_envelope.blocked_tool_names, "request_input"))),
            )
    refresh_tool_names = frozenset(capability_refresh_tool_names)
    if refresh_tool_names and (
        runtime_envelope is None
        or allowed_tool_set is None
        or not refresh_tool_names <= set(dynamic_handlers) & allowed_tool_set
        or tool_executor is not None
    ):
        raise ValueError("Capability refresh requires explicitly scoped host handlers")
    control_tools = [
        schema for schema in tools
        if str((schema.get("function") or {}).get("name") or "")
        in (set(dynamic_handlers) | {"request_input"}) & (allowed_tool_set or set())
    ]
    registered_tool_context = {
        key: value
        for key, value in dict(runtime_tool_context or {}).items()
        if key in RUNTIME_TOOL_CONTEXT_KEYS
    }
    from packages.core.ai.runtime.performance import runtime_performance_run_id

    performance_run_id = runtime_performance_run_id(
        performance_run_id=performance_run_id,
        runtime_run_id=runtime_run_id,
        workflow_run_id=registered_tool_context.get(
            "_workflow_run_id_from_context"
        ),
        task_id=task_id,
    )
    if runtime_envelope is not None and dynamic_handlers:
        scoped_dynamic_names = set(dynamic_handlers) & (allowed_tool_set if allowed_tool_set is not None else {"read_context_archive"})
        if scoped_dynamic_names:
            runtime_envelope = replace(
                runtime_envelope,
                tool_names=tuple(dict.fromkeys((
                    *runtime_envelope.tool_names,
                    *sorted(scoped_dynamic_names),
                ))),
                allowed_tool_names=tuple(dict.fromkeys((
                    *runtime_envelope.allowed_tool_names,
                    *sorted(scoped_dynamic_names),
                ))),
            )
    resolved_tool_schema = tool_schema_resolver or runtime_tool_schema_resolver(
        get_schema=runtime_tool_schema,
        allowed_tool_names=allowed_tool_set,
    )

    def _current_tool_schema(name: str) -> dict[str, Any] | None:
        # The loop keeps this callable; rebuilding only a local resolver would
        # leave progressive search using the pre-acquisition allowlist.
        if name == "request_input" and not can_request_input:
            return None
        return resolved_tool_schema(name)

    tool_context_message = active_user_message
    if tool_context_message is None and isinstance(user_message, str):
        tool_context_message = user_message
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
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                step_id=step_id,
                active_user_message=tool_context_message,
                allowed_tool_names=allowed_tool_set,
            )
        registered_arguments = dict(arguments)
        registered_arguments.update(registered_tool_context)
        return await runtime_execute_tool(
            tool_name,
            registered_arguments,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            step_id=step_id,
            active_user_message=tool_context_message,
            manual_skill_selected=manual_skill_selected,
            manual_skill_ids=list(manual_skill_ids or []),
            manual_skill_slugs=list(manual_skill_slugs or []),
            tool_profile=tool_profile,
            allowed_tool_names=allowed_tool_set,
            llm_metadata=resolved_metadata,
            llm_model=resolved_model,
            runtime_envelope=runtime_envelope,
        )

    base_tool_executor = tool_executor or _runtime_tool_executor

    async def _artifact_tracking_tool_executor(name: str, args: dict[str, Any]) -> Any:
        nonlocal runtime_envelope, allowed_tool_set, resolved_tool_schema
        executor = _runtime_tool_executor if name == "read_context_archive" and context_archive_reader is not None else base_tool_executor
        result = await executor(name, args)
        from packages.core.ai.runtime.capability_refresh import (
            is_runtime_capability_refresh,
            runtime_applied_capability_schemas,
            runtime_apply_capability_refresh,
        )
        from packages.core.ai.runtime.control import RuntimeTurnAborted, is_runtime_tool_suspension

        if is_runtime_capability_refresh(result):
            if name not in refresh_tool_names:
                raise RuntimeTurnAborted("Capability refresh came from an unregistered host handler")
            runtime_envelope, allowed_tool_set, result = runtime_apply_capability_refresh(
                result,
                current_envelope=runtime_envelope,
                control_tools=control_tools,
            )
            refreshed_schemas = {
                schema["function"]["name"]: schema
                for schema in runtime_applied_capability_schemas(result) or []
            }
            resolved_tool_schema = runtime_tool_schema_resolver(
                get_schema=lambda tool_name: refreshed_schemas.get(tool_name) or runtime_tool_schema(tool_name),
                allowed_tool_names=allowed_tool_set,
            )

        if not is_runtime_tool_suspension(result):
            runtime_record_tool_result_artifacts(result)
        return result

    agentic_loop_parameters = inspect.signature(agentic_loop).parameters
    accepts_agentic_loop_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in agentic_loop_parameters.values()
    )

    def _agentic_loop_accepts_keyword(name: str) -> bool:
        return accepts_agentic_loop_kwargs or name in agentic_loop_parameters

    effective_on_round_end = (
        runtime_run_state_round_observer(on_round_end, run_state_slots=run_state_slots)
        if on_round_end is not None
        else None
    )

    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    if tool_evidence is not None:
        # A host that judges the run itself (e.g. the Task agent loop's
        # complete_task checks) owns the live ledger the loop observes into.
        if not isinstance(tool_evidence, RuntimeToolEvidence) or (
            isinstance(final_response_contract, RuntimeToolEvidence)
            and final_response_contract is not tool_evidence
        ):
            raise ValueError("A run has exactly one tool evidence ledger")
    elif isinstance(final_response_contract, RuntimeToolEvidence):
        tool_evidence = final_response_contract
    else:
        tool_evidence = RuntimeToolEvidence()
    principal = getattr(runtime_envelope, "principal", None)
    capability_scope = {
        "conversation_id": conversation_id,
        "entity_id": entity_id,
        "user_id": getattr(principal, "actor_user_id", None) or user_id,
        "agent_id": agent_id,
        "workspace_id": workspace_id,
    }
    tool_evidence.configure_capability_scope(capability_scope)
    if initial_tool_evidence:
        tool_evidence.restore_checkpoint_state(initial_tool_evidence)
    restore_final_evidence = getattr(final_response_contract, "restore_checkpoint_state", None)
    if (
        run_state is not None
        and run_state.final_response_evidence
        and not runtime_checkpoint
        and callable(restore_final_evidence)
        and final_response_contract is not tool_evidence
    ):
        restore_final_evidence(run_state.final_response_evidence)
    if isinstance(runtime_envelope, RuntimeEnvelope) and (run_state is not None or runtime_checkpoint):
        from packages.core.ai.runtime.capability_continuation import (
            CAPABILITY_SCHEMA_CHAR_BUDGET,
            runtime_restore_gateway_tools,
        )
        from packages.core.ai.runtime.tool_gateway import CALL_TOOL_NAME, runtime_schema_tool_name

        restore_names = _runtime_gateway_names_to_restore(
            run_state=run_state,
            runtime_checkpoint=runtime_checkpoint,
            tools=tools,
            gateway_tool_schemas=gateway_tool_schemas,
        )
        gateway_mode = any(runtime_schema_tool_name(schema) == CALL_TOOL_NAME for schema in tools)
        restored_schemas: list[dict[str, Any]] = []
        restored_names: tuple[str, ...] = ()
        refused: set[str] = set()
        if restore_names:
            try:
                restored_schemas, restored_names = await runtime_restore_gateway_tools(
                    # Most recent first: a bound drops the oldest loads.
                    list(reversed(restore_names)),
                    envelope=runtime_envelope,
                    entity_id=entity_id,
                    user_id=capability_scope["user_id"],
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    active_user_message=tool_context_message,
                    limit=LOADED_GATEWAY_TOOLS_LIMIT,
                    # Gateway tools stay out of the tool list (the cached
                    # prefix); their section in the turn context has its own
                    # budget, so only a tool-list restore is size-bounded.
                    schema_char_budget=None if gateway_mode else CAPABILITY_SCHEMA_CHAR_BUDGET,
                    refused=refused,
                )
            except Exception:  # noqa: BLE001 - a stale provider must not stop the resume
                logger.warning("Resumed run could not restore its loaded tools", exc_info=True)
            # Names, never grants: what this wake could not re-authorize
            # (discovery failed) stays recorded for the next resume; only a
            # name the current scope refused is forgotten.
            tool_evidence.retain_loaded_tools(name for name in restore_names if name not in refused)
        if restored_schemas:
            restored_schemas.reverse()
            if gateway_mode:
                # Reachable through call_tool, as when search_tools loaded
                # them: the tool list (the cached prefix) does not change.
                gateway_tool_schemas = [*(gateway_tool_schemas or []), *restored_schemas]
            else:
                tools = [*tools, *restored_schemas]
            # Loaded again in this run.
            tool_evidence.observe_loaded_tools(reversed(restored_names))
    loop_kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_message": user_message,
        "tools": tools,
        "tool_executor": _artifact_tracking_tool_executor,
        "model": resolved_model,
        "final_model": resolved_final_model,
        "final_metadata": resolved_final_metadata,
        "initial_messages": initial_messages,
        "on_tool_start": on_tool_start,
        "on_tool_end": on_tool_end,
        "on_llm_call": on_llm_call,
        "on_llm_call_before": on_llm_call_before,
        "on_llm_usage_settled": on_llm_usage_settled,
        "stream_handler": stream_handler,
        "is_cancelled": is_cancelled,
        "metadata": resolved_metadata,
        "tool_schema_resolver": _current_tool_schema,
        "forced_tool_calls": forced_tool_calls,
        "terminal_tool_result_policy": terminal_tool_result_policy,
        "output_schema": output_schema,
    }
    # Only transcript turns set a cap; a loop that predates it keeps its default.
    if tool_result_max_chars is not None and _agentic_loop_accepts_keyword("tool_result_max_chars"):
        loop_kwargs["tool_result_max_chars"] = tool_result_max_chars
    if turn_context and _agentic_loop_accepts_keyword("turn_context"):
        loop_kwargs["turn_context"] = turn_context
    if gateway_tool_schemas and _agentic_loop_accepts_keyword("gateway_tool_schemas"):
        loop_kwargs["gateway_tool_schemas"] = gateway_tool_schemas
    if prompt_context is not None and user_id and conversation_id:
        from packages.core.ai.runtime.context_memory import RuntimeContextMemory
        if any(getattr(prompt_context, key, None) != value for key, value in {
            "entity_id": entity_id, "user_id": user_id, "workspace_id": workspace_id,
            "agent_id": agent_id, "conversation_id": conversation_id,
        }.items()):
            raise ValueError("Compaction memory scope differs from the executing runtime")
        if prompt_context.runtime_context_sections and _agentic_loop_accepts_keyword("context_memory"):
            from packages.core.ai.runtime.response_language import _without_response_language_guidance

            normalize = (_without_response_language_guidance
                         if language.external_audience or response_language_prompt("", language) else None)
            bridge = RuntimeContextMemory(prompt_context, normalize_prompt=normalize)
            bridge.ctx.runtime_envelope = runtime_envelope
            if bridge._agent_allowed() or bridge._workspace_allowed():
                loop_kwargs["context_memory"] = bridge
                if runtime_checkpoint and isinstance(runtime_checkpoint.get("messages"), list):
                    # Resume execution state (rounds/tools/approvals) unchanged,
                    # while rebuilding the primary prompt from current sources.
                    resumed = [dict(m) for m in runtime_checkpoint["messages"]]
                    primary = {"role": "system", "content": normalize(system_prompt) if normalize else system_prompt}
                    if resumed and resumed[0].get("role") == "system":
                        resumed[0] = primary
                    else:
                        resumed.insert(0, primary)
                    runtime_checkpoint = {**runtime_checkpoint, "messages": resumed}
    # Keep the runtime boundary compatible with older agentic_loop builds.
    # Runtime-only keywords are optional, but passing an unknown keyword aborts
    # the whole Chat turn before the model gets a chance to respond.
    for keyword, value in {
        "final_response_contract": final_response_contract,
        "non_success_outcomes": non_success_outcomes,
        "tool_evidence": tool_evidence,
        "performance_run_id": performance_run_id,
        "runtime_run_id": runtime_run_id,
        "resume_checkpoint": runtime_checkpoint,
        "runtime_cancel_checker": runtime_cancel_checker,
        "human_input_suspension": human_input_suspension,
        "on_round_end": effective_on_round_end,
        "on_round_end_strict": on_round_end_strict,
        "initial_usage": initial_usage,
        "initial_rounds": initial_rounds,
        "initial_tool_calls_made": initial_tool_calls_made,
        "hard_max_rounds": hard_max_rounds,
        "capability_refresh_tool_names": refresh_tool_names,
    }.items():
        if _agentic_loop_accepts_keyword(keyword):
            loop_kwargs[keyword] = value
    if hitl_resume_checkpoint and _agentic_loop_accepts_keyword("hitl_resume_checkpoint"):
        # Only a durable host that parks approval stops asks for this; every
        # other loop's approval control stays exactly as it was.
        loop_kwargs["hitl_resume_checkpoint"] = True
    if temperature is not None:
        loop_kwargs["temperature"] = temperature
    if max_tokens is not None:
        loop_kwargs["max_tokens"] = max_tokens
    if max_rounds is not None:
        loop_kwargs["max_rounds"] = max_rounds
    if on_context_compacted is not None:
        loop_kwargs["on_context_compacted"] = on_context_compacted
    if on_context_checkpoint is not None:
        loop_kwargs["on_context_checkpoint"] = on_context_checkpoint
    nested_usage_token = runtime_begin_nested_usage_collection()
    from packages.core.ai.llm_client import llm_prompt_cache_key_scope

    try:
        with (
            runtime_artifact_tracking_scope(
                web_source_slices=(
                    (runtime_checkpoint or {}).get("web_source_slices")
                    if runtime_checkpoint or run_state is None
                    else run_state.web_source_slices
                ),
            ),
            runtime_response_language_scope(language),
            llm_prompt_cache_key_scope(_prompt_cache_key(
                entity_id=entity_id, conversation_id=conversation_id,
                user_id=user_id, agent_id=agent_id, workspace_id=workspace_id,
            )),
        ):
            result = await agentic_loop(**loop_kwargs)
            # Every web slice this run saw, for its final run state.
            final_web_source_slices = runtime_web_source_checkpoint_state()
    finally:
        nested_usages = runtime_finish_nested_usage_collection(nested_usage_token)

    if (
        on_context_checkpoint is not None
        and getattr(result, "messages", None)
        and getattr(result, "stop_reason", None) != "cancelled"
    ):
        await on_context_checkpoint(result.messages)
    if nested_usages and isinstance(getattr(result, "usage", None), dict):
        nested_summary = {
            "prompt": 0,
            "completion": 0,
            "total": 0,
            "calls": len(nested_usages),
        }
        for child_usage in nested_usages:
            _add_usage(result.usage, child_usage)
            nested_summary["prompt"] += int(
                child_usage.get("prompt") or child_usage.get("prompt_tokens") or 0
            )
            nested_summary["completion"] += int(
                child_usage.get("completion")
                or child_usage.get("completion_tokens")
                or 0
            )
            nested_summary["total"] += int(
                child_usage.get("total")
                or child_usage.get("total_tokens")
                or 0
            )
        result.usage["nested_usage"] = nested_summary
    result.tool_evidence = tool_evidence.checkpoint_state()
    # The run state a durable host persists when the loop returns: a resume
    # from it (a continuation, a delegated question, a retry) keeps the web
    # sources, reply evidence and loaded tools a host would not rebuild.
    result.run_state = RuntimeRunState(
        messages=list(getattr(result, "messages", None) or []),
        usage=dict(getattr(result, "usage", None) or {}),
        rounds=max(0, int(getattr(result, "rounds", 0) or 0)),
        tool_calls_made=[str(name) for name in getattr(result, "tool_calls_made", None) or []],
        tool_evidence=result.tool_evidence,
        web_source_slices=final_web_source_slices,
        final_response_evidence=(
            final_response_contract.checkpoint_state()
            if callable(getattr(final_response_contract, "checkpoint_state", None))
            and final_response_contract is not tool_evidence
            else {}
        ),
        loaded_gateway_tools=list(tool_evidence.loaded_tool_names),
    )
    if final_response_contract is not None and getattr(result, "error", None):
        # Error exits may contain a provider's partial candidate. Keep its
        # diagnostic transcript private; Chat projects the existing host error.
        result.content = ""
        if result.error == "final_response_validation_failed":
            from packages.core.ai.runtime.output_policy import runtime_response_validation_failure_content

            result.error_detail = {**(result.error_detail or {}), "response_locale": language.locale}
            result.content = runtime_response_validation_failure_content({
                "error": result.error, "limit_detail": result.error_detail,
            }) or ""
    return result


def _prompt_cache_key(
    *, entity_id: str | None, conversation_id: str | None, user_id: str | None,
    agent_id: str | None, workspace_id: str | None,
) -> str | None:
    """One opaque key per conversation stream; None outside a conversation."""
    if not conversation_id:
        return None
    import hashlib

    raw = "|".join(str(value or "") for value in (entity_id, conversation_id, user_id, agent_id, workspace_id))
    return "manor-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


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


@functools.wraps(_runtime_execute_agentic_loop)
async def runtime_execute_agentic_loop(**loop_kwargs: Any) -> Any:
    """Run one agent loop, one execution nesting level deeper.

    Every agent loop (chat, Task, skill, delegated agent, workflow node)
    enters here, so a host can tell its own loop's calls from nested ones.
    """

    from packages.core.ai.runtime.execution_nesting import runtime_nested_execution_scope

    with runtime_nested_execution_scope():
        return await _runtime_execute_agentic_loop(**loop_kwargs)


async def runtime_execute_workflow_agent_loop(**loop_kwargs: Any) -> Any:
    """Run a workflow agentic loop with Runtime-owned billing source."""

    from packages.core.ai.runtime.nested_agent import runtime_execute_with_nested_approval_checkpoints

    return await runtime_execute_with_nested_approval_checkpoints(
        runtime_execute_agentic_loop,
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
    step_id: str | None = None,
    active_user_message: str | None = None,
    tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
    billing_source: str = RUNTIME_SUBAGENT_SOURCE,
    requested_name: str | None = None,
    requested_max_rounds: Any = None,
    default_max_rounds: int = 20,
    initial_messages: list[dict[str, Any]] | None = None,
    initial_usage: dict[str, Any] | None = None,
    initial_rounds: int = 0,
    initial_tool_calls_made: list[str] | None = None,
    on_round_end: Callable[..., Any] | None = None,
    on_round_end_strict: bool = False,
    dynamic_tool_handlers: Mapping[str, RuntimeDynamicToolHandler] | None = None,
    capability_refresh_tool_names: Iterable[str] = (),
    terminal_tool_result_policy: dict[str, Any] | None = None,
    on_tool_start: Callable[[str, dict[str, Any]], Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    forced_tool_calls: list[dict[str, Any]] | None = None,
    runtime_checkpoint: dict[str, Any] | None = None,
    human_input_suspension: bool = False,
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
        step_id=step_id,
        active_user_message=active_user_message,
        tool_profile=tool_profile,
        allowed_tool_names=allowed_tool_names,
        model=model,
        max_rounds=subagent_run.max_rounds or default_max_rounds,
        initial_messages=initial_messages,
        initial_usage=initial_usage,
        initial_rounds=initial_rounds,
        initial_tool_calls_made=initial_tool_calls_made,
        on_round_end=on_round_end,
        on_round_end_strict=on_round_end_strict,
        metadata=metadata,
        billing_source=billing_source,
        dynamic_tool_handlers=dynamic_tool_handlers,
        capability_refresh_tool_names=capability_refresh_tool_names,
        terminal_tool_result_policy=terminal_tool_result_policy,
        forced_tool_calls=forced_tool_calls,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        runtime_checkpoint=runtime_checkpoint,
        human_input_suspension=human_input_suspension,
    )
    subagent_run.record_end(rounds=getattr(result, "rounds", None))
    return RuntimeSubAgentLoopResult(result=result, run=subagent_run)


async def runtime_execute_worker_subagent_loop(**loop_kwargs: Any) -> RuntimeSubAgentLoopResult:
    """Run an InternalWorker subagent loop with Runtime-owned billing source."""

    return await runtime_execute_subagent_loop(
        **loop_kwargs,
        billing_source=RUNTIME_WORKER_SUBAGENT_BILLING_SOURCE,
    )


async def runtime_execute_worker_subagent_followup(**loop_kwargs: Any) -> Any:
    """One bounded follow-up round continuing a worker subagent transcript.

    Used by the forced submit_result finalization: when the main loop ends
    without a submission, the InternalWorker asks for exactly one more round
    whose only tool is submit_result. Runtime-owned so the worker never
    touches the loop layer directly; billing stays on the worker-subagent
    source.
    """

    loop_kwargs.setdefault("max_rounds", 1)
    return await runtime_execute_agentic_loop(
        **loop_kwargs,
        billing_source=RUNTIME_WORKER_SUBAGENT_BILLING_SOURCE,
    )


def runtime_wrap_tool_executor(
    envelope: RuntimeEnvelope,
    executor: ToolExecutor,
) -> ToolExecutor:
    """Return an executor guarded and traced by a RuntimeEnvelope."""

    return RuntimeHarness(envelope).wrap_tool_executor(executor)
