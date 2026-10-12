from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from packages.core.constants.task import AI_LOG_TYPES, TaskLogType
from packages.core.ai.runtime.human_input import (
    HUMAN_INPUT_MODE_END_TURN,
    REQUEST_INPUT_TOOL_NAME,
    input_request_from_tool_result,
)
from packages.core.ai.engine import ChatMessage
from packages.core.ai.llm_client import CreditExhaustedError
from packages.core.ai.runtime.artifacts import (
    runtime_artifact_tracking_scope,
    runtime_extract_artifact_urls_from_tool_result,
    runtime_record_tool_result_artifacts,
)
from packages.core.ai.runtime.billing import runtime_llm_billing_context
from packages.core.ai.runtime.control import (
    runtime_sequence_child_owner,
    runtime_terminal_uncertainty_control,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.human_gate import runtime_detect_blocking_human_gate
from packages.core.ai.runtime.provider_approvals import (
    followup_tool_names_from_forced_calls,
    pop_tool_continuation,
    restrict_provider_followup_tools,
    retry_call_after_tool_continuation,
)
from packages.core.ai.runtime.sources import RUNTIME_TASK_RUNNER_SOURCE
from packages.core.ai.runtime.task_llm import (
    runtime_execute_task_agent_chat,
    runtime_execute_task_final_chat,
    runtime_execute_task_supervisor_chat,
    runtime_configure_task_engine_model as _runtime_configure_task_engine_model,
    runtime_task_engine as _runtime_task_engine,
    runtime_task_engine_model as _runtime_task_engine_model,
)
from packages.core.ai.runtime.task_requirements import task_runtime_requirements_prompt
from packages.core.ai.runtime.tool_registry import runtime_execute_tool, runtime_tool_schema
from packages.core.ai.runtime.tool_schema import runtime_tool_schema_resolver
from packages.core.services.sensitive_data import sanitize_approval_credentials


RUNTIME_TASK_VERDICT_CONTINUE = "continue"
RUNTIME_TASK_VERDICT_DONE = "done"
RUNTIME_TASK_VERDICT_FAILED = "failed"
RUNTIME_TASK_VERDICT_NEEDS_HITL = "needs_hitl"
RUNTIME_TASK_VERDICT_NEEDS_REPLAN = "needs_replan"
RUNTIME_TASK_VALID_VERDICTS = {
    RUNTIME_TASK_VERDICT_CONTINUE,
    RUNTIME_TASK_VERDICT_DONE,
    RUNTIME_TASK_VERDICT_FAILED,
    RUNTIME_TASK_VERDICT_NEEDS_HITL,
    RUNTIME_TASK_VERDICT_NEEDS_REPLAN,
}
_SUPERVISOR_TOOL_EVIDENCE_MAX_RESULT_CHARS = 1_600
_SUPERVISOR_TOOL_EVIDENCE_MAX_ITEMS = 8


def runtime_task_engine(engine: Any | None = None) -> Any:
    """Return the scheduled-task LLM engine, creating the Runtime default when needed."""

    return _runtime_task_engine(engine)


def runtime_configure_task_engine_model(engine: Any, model: str | None) -> Any:
    """Apply the resolved model to a scheduled-task engine when it has config."""

    return _runtime_configure_task_engine_model(engine, model)


def runtime_task_engine_model(engine: Any) -> str | None:
    """Read a scheduled-task engine model name without exposing config internals."""

    return _runtime_task_engine_model(engine)


def runtime_task_llm_billing_context(
    *,
    entity_id: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    source: str = RUNTIME_TASK_RUNNER_SOURCE,
    byok: bool = False,
) -> Any:
    """Build the LLM billing context for scheduled-task agent execution."""

    return runtime_llm_billing_context(
        entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        source=source,
        byok=byok,
    )


def runtime_classify_task_complexity(task: Any) -> str:
    """Choose the primary or worker model role for a scheduled task."""

    details = getattr(task, "details", None) or {}
    llm_role = details.get("model_role") if isinstance(details, dict) else None
    if llm_role in ("worker", "primary"):
        return llm_role

    if isinstance(details, dict) and details.get("scheduled_job_id") and details.get("execution_script"):
        return "worker"

    priority = int(getattr(task, "priority", 0) or 0)
    if priority >= 4:
        return "primary"

    if getattr(task, "task_type", None) in {
        "notification",
        "status_update",
        "reminder",
        "follow_up",
        "summary",
    }:
        return "worker"

    if isinstance(details, dict) and details.get("scheduled_job_id") and priority <= 2:
        return "worker"

    return "primary"


def runtime_task_system_prompt(agent_prompt: str | None = None) -> str:
    """Render the scheduled-task system prompt."""

    base = (
        "You are an AI agent executing an assigned task ticket.\n\n"
        "Rules:\n"
        "- Produce a CONCRETE deliverable \u2014 not a plan or suggestion.\n"
        "- If the task is too vague or missing critical details, clearly state what "
        "information you need. A supervisor will route it to a human for clarification.\n"
        "- Use your tools to complete the work, then report what you accomplished.\n"
        "- Your output will be reviewed by a supervisor who decides pass/fail.\n"
        "- If you cannot complete the task, explain exactly what is blocking you.\n"
    )
    prompt = str(agent_prompt or "").strip()
    if prompt:
        return f"{prompt}\n\n{base}"
    return base


def runtime_task_ticket_prompt(task: Mapping[str, Any]) -> str:
    """Render the scheduled-task user prompt from a task ticket payload."""

    parts: list[str] = [
        "## Execution Mode: Task Ticket",
        "You are executing an assigned task \u2014 produce a concrete deliverable.",
        "If the task is too vague or underspecified to execute properly, "
        "respond with what information you need so a human can clarify.\n",
    ]

    title = str(task.get("title") or "").strip()
    if title:
        parts.append(f"## Task\n{title}")

    description = str(task.get("description") or "").strip()
    if description and description != title:
        parts.append(f"**Details:** {description}")

    meta_lines: list[str] = []
    priority = task.get("priority")
    if priority:
        priority_labels = {
            5: "Critical",
            4: "High",
            3: "Medium",
            2: "Low",
            1: "Minimal",
        }
        meta_lines.append(f"Priority: {priority_labels.get(priority, priority)}")
    task_type = task.get("task_type")
    if task_type and task_type != "general":
        meta_lines.append(f"Type: {str(task_type).replace('_', ' ')}")
    if meta_lines:
        parts.append("**Context:** " + " | ".join(meta_lines))

    details = task.get("details") or {}
    if isinstance(details, dict):
        exec_script = details.get("execution_script")
        if exec_script:
            parts.append(f"## Execution Procedure (follow these steps exactly)\n{exec_script}")

        payload_msg = details.get("payload_message")
        if payload_msg and payload_msg != description:
            parts.append(f"**Instructions:** {payload_msg}")

        deliverable = str(details.get("deliverable") or "").strip()
        if deliverable:
            parts.append(f"**Expected deliverable:** {deliverable}")
        done_when = str(details.get("done_when") or "").strip()
        if done_when:
            parts.append(f"**Done when:** {done_when}")

        runtime_requirements = task_runtime_requirements_prompt(details.get("runtime_context"))
        if runtime_requirements:
            parts.append(runtime_requirements)

        dep_outputs = details.get("dep_outputs") or []
        if dep_outputs:
            dep_lines = ["## Predecessor Task Outputs"]
            for dep in dep_outputs:
                if not isinstance(dep, dict):
                    continue
                dep_lines.append(f"### {dep.get('task_title', 'unknown')}")
                if dep.get("result_summary"):
                    dep_lines.append(f"**Summary:** {dep['result_summary']}")
                files = dep.get("files")
                if isinstance(files, list) and files:
                    dep_lines.append("**Files:**")
                    for file in files[:10]:
                        if not isinstance(file, dict):
                            continue
                        label = (
                            file.get("name") or file.get("filename") or file.get("fs_path") or file.get("url") or "file"
                        )
                        ref = (
                            file.get("fs_path")
                            or file.get("path")
                            or file.get("file_url")
                            or file.get("url")
                            or file.get("document_id")
                            or ""
                        )
                        dep_lines.append(f"- {label}" + (f" ({ref})" if ref and ref != label else ""))
            parts.append("\n".join(dep_lines))

    return "\n\n".join(parts) if parts else title or "Execute this task."


def runtime_task_initial_messages(user_prompt: str) -> list[ChatMessage]:
    """Build the initial scheduled-task chat messages."""

    return [ChatMessage(role="user", content=user_prompt)]


def runtime_task_supervisor_feedback_message(instruction: Any) -> ChatMessage:
    """Build the next-turn user message from a supervisor continuation."""

    text = str(instruction or "Continue working on the task.").strip()
    if not text:
        text = "Continue working on the task."
    return ChatMessage(role="user", content=f"Supervisor feedback: {text}")


def runtime_task_followup_message(content: Any) -> ChatMessage:
    """Build a Runtime-owned user message for a scheduled-task follow-up."""

    return ChatMessage(role="user", content=str(content or ""))


@dataclass(frozen=True)
class RuntimeTaskAgentTurnResult:
    """Result for one scheduled-task agent turn."""

    messages: list[ChatMessage]
    tools: list[dict[str, Any]]
    loaded_tool_names: set[str]
    response_text: str
    tool_names: list[str]
    usage: dict[str, Any]
    had_tool_calls: bool
    supervisor_evidence: list[dict[str, str]] = field(default_factory=list)
    blocking_control: dict[str, Any] | None = None
    blocking_tool_arguments: dict[str, Any] | None = None
    remaining_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    disable_followup_tools: bool = False
    terminal_control: dict[str, Any] | None = None
    #: Set when the agent called ``request_input`` this turn: the normalized
    #: question. The task loop parks the task on it instead of continuing.
    input_request: dict[str, Any] | None = None


class RuntimeTaskProviderError(RuntimeError):
    """A model-provider failure surfaced through the response usage payload."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


_RETRYABLE_TASK_PROVIDER_ERROR = re.compile(
    r"\bHTTP[ :]*(?:429|500|502|503|504|524)\b"
    r"|\b(?:ConnectError|ConnectTimeout|ReadTimeout|RemoteProtocolError|TimeoutError)\b"
    r"|service (?:is )?temporarily unavailable"
    r"|empty llm response",
    re.I,
)


def runtime_task_provider_error_is_retryable(error: Any) -> bool:
    """Return whether a provider error can reasonably clear on a later run."""
    return bool(_RETRYABLE_TASK_PROVIDER_ERROR.search(str(error or "")))


def _raise_task_provider_error(usage: Mapping[str, Any] | None) -> None:
    error = str((usage or {}).get("error") or "").strip()
    if error:
        raise RuntimeTaskProviderError(
            error,
            retryable=runtime_task_provider_error_is_retryable(error),
        )


@dataclass(frozen=True)
class RuntimeTaskFinalResponseResult:
    """Result for the no-tools final scheduled-task completion."""

    messages: list[ChatMessage]
    response_text: str
    usage: dict[str, Any]
    finalized: bool


@dataclass(frozen=True)
class RuntimeTaskUserPromptResult:
    """Resolved user prompt for a scheduled task agent run."""

    prompt: str
    conversation_entry_count: int = 0
    hitl_response: str | None = None
    hitl_by: str | None = None


def _hitl_question_text(question: Any) -> str:
    """Render the question the agent asked (a request_input payload or text)."""
    if isinstance(question, dict):
        lines = [str(question.get("question") or "").strip()]
        options = question.get("options")
        if isinstance(options, list) and options:
            lines.append("Options offered: " + " / ".join(str(item) for item in options))
        fields = question.get("fields")
        if isinstance(fields, list) and fields:
            labels = [
                str(item.get("label") or item.get("name") or "").strip()
                for item in fields
                if isinstance(item, dict)
            ]
            if any(labels):
                lines.append("Fields requested: " + ", ".join(label for label in labels if label))
        return "\n".join(line for line in lines if line)
    return str(question or "").strip()


def runtime_task_user_prompt(
    *,
    base_prompt: str,
    task_logs: Iterable[Any] | None = None,
    hitl_response: Any | None = None,
    hitl_responded_by: Any | None = None,
    hitl_question: Any | None = None,
    hitl_payload: Any | None = None,
    max_history_entries: int = 10,
    max_log_chars: int = 500,
) -> RuntimeTaskUserPromptResult:
    """Attach Runtime-owned task conversation and HITL context to a user prompt."""

    prompt = str(base_prompt or "")
    relevant_logs = [
        log
        for log in (task_logs or [])
        if getattr(log, "log_type", None)
        in {
            TaskLogType.COMMENT,
            TaskLogType.AI_EXECUTION_COMPLETED,
            TaskLogType.AI_HITL_REQUESTED,
        }
    ]
    if relevant_logs:
        convo_lines = ["## Conversation History"]
        for log in relevant_logs[-max_history_entries:]:
            log_type = str(getattr(log, "log_type", "") or "")
            who = getattr(log, "created_by", None) or (
                "AI Agent" if log_type in AI_LOG_TYPES else "System"
            )
            content = str(getattr(log, "content", "") or "")[:max_log_chars]
            convo_lines.append(f"**{who}:** {content}")
        prompt = f"{prompt}\n\n" + "\n\n".join(convo_lines)

    response = str(hitl_response or "").strip()
    attachments = hitl_payload.get("attachments") if isinstance(hitl_payload, dict) else None
    if isinstance(attachments, list) and attachments:
        response = "\n\n".join(part for part in (
            response,
            "Attached Knowledge documents (read as needed):\n"
            + json.dumps(attachments, ensure_ascii=False),
        ) if part)
    hitl_by = str(hitl_responded_by or "Human").strip() or "Human"
    if response:
        question_text = _hitl_question_text(hitl_question)
        if question_text:
            prompt = f"{prompt}\n\n## Question you asked the user\n{question_text}"
        prompt = (
            f"{prompt}\n\n## Human Response (from {hitl_by})\n{response}\n\n"
            "Continue the task using this answer. Do not ask the same question again."
        )

    return RuntimeTaskUserPromptResult(
        prompt=prompt,
        conversation_entry_count=len(relevant_logs[-max_history_entries:]),
        hitl_response=response or None,
        hitl_by=hitl_by if response else None,
    )


def _tool_call_parts(tool_call: dict[str, Any]) -> tuple[str, Any, str | None]:
    if "function" in tool_call and isinstance(tool_call.get("function"), dict):
        fn = tool_call["function"]
        return str(fn.get("name") or ""), fn.get("arguments", {}), tool_call.get("id")
    return (
        str(tool_call.get("name") or ""),
        tool_call.get("arguments", {}),
        tool_call.get("id"),
    )


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _parse_tool_result_object(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    text = str(result or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _supervisor_tool_names_for_call(tool_name: str, tool_args: Mapping[str, Any]) -> list[str]:
    """Return supervisor-visible tool evidence for gateway-style tool calls."""

    names = [tool_name]
    if tool_name in {"workspace_agent", "manor"}:
        action = str(tool_args.get("action") or "").strip()
        if action:
            names.append(f"{tool_name}:{action}")
        if tool_name == "manor" and action == "workspace":
            raw_params = tool_args.get("params")
            params = raw_params if isinstance(raw_params, Mapping) else {}
            workspace_action = str(params.get("action") or "").strip()
            if workspace_action:
                names.append(f"manor:workspace:{workspace_action}")
    return names


def _supervisor_tool_evidence_for_call(
    tool_name: str,
    tool_result: Any,
) -> dict[str, str]:
    """Keep a bounded receipt for the supervisor's completion review."""

    result_text = str(tool_result or "")
    if len(result_text) > _SUPERVISOR_TOOL_EVIDENCE_MAX_RESULT_CHARS:
        result_text = (
            result_text[:_SUPERVISOR_TOOL_EVIDENCE_MAX_RESULT_CHARS]
            + "... [truncated]"
        )
    return {"tool_name": tool_name, "result": result_text}


def _schema_name(schema: dict[str, Any]) -> str | None:
    if not isinstance(schema, dict):
        return None
    fn = schema.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip() or None
    return str(schema.get("name") or "").strip() or None


def _load_search_result_tool_schemas(
    *,
    tools: list[dict[str, Any]],
    loaded_tool_names: set[str],
    search_result: dict[str, Any],
    allowed_tool_names: Iterable[str] | None,
) -> None:
    match_schemas: dict[str, dict[str, Any]] = {}
    match_manifests: dict[str, dict[str, Any]] = {}
    discovered_names: set[str] = set()
    for match in search_result.get("matches", []):
        if not isinstance(match, dict):
            continue
        match_name = str(match.get("name") or "").strip()
        if match_name:
            match_manifests[match_name] = match
        schema = match.get("schema")
        if not isinstance(schema, dict):
            continue
        schema_name = _schema_name(schema)
        if match_name:
            match_schemas[match_name] = schema
        if schema_name:
            match_schemas[schema_name] = schema
            match_manifests[schema_name] = match
            if match.get("available") is not False:
                discovered_names.add(schema_name)

    def _search_schema(name: str) -> dict[str, Any] | None:
        return runtime_tool_schema(name) or match_schemas.get(name)

    search_tool_schema_resolver = runtime_tool_schema_resolver(
        get_schema=_search_schema,
        allowed_tool_names=(
            None
            if allowed_tool_names is None
            else set(allowed_tool_names) | discovered_names
        ),
    )
    load_names = [str(name) for name in (search_result.get("loaded_tools") or []) if name]
    if not load_names:
        load_names = [
            str(match.get("name"))
            for match in search_result.get("matches", [])
            if isinstance(match, dict) and match.get("name") and match.get("available") is not False
        ]
    for schema in match_schemas.values():
        schema_name = _schema_name(schema)
        if schema_name and schema_name not in load_names:
            load_names.append(schema_name)
    for name in load_names:
        schema = search_tool_schema_resolver(name)
        if not isinstance(schema, dict):
            continue
        manifest = match_manifests.get(name)
        if manifest is not None:
            from packages.core.ai.runtime.tool_discovery import (
                runtime_apply_integration_account_options_to_schema,
            )

            schema = runtime_apply_integration_account_options_to_schema(
                schema,
                manifest.get("account_options"),
                requires_explicit_account=bool(
                    manifest.get("requires_explicit_account")
                ),
            )
        schema_name = _schema_name(schema)
        if schema_name and schema_name not in loaded_tool_names:
            tools.append(schema)
            loaded_tool_names.add(schema_name)


async def runtime_execute_task_agent_turn(
    *,
    engine: Any,
    messages: list[ChatMessage],
    tools: list[dict[str, Any]],
    loaded_tool_names: set[str],
    system_prompt: str,
    runtime_envelope: RuntimeEnvelope | None,
    entity_id: str,
    agent_id: str | None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    metadata: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    forced_tool_calls: list[dict[str, Any]] | None = None,
    performance_tool_call_id: str | None = None,
) -> RuntimeTaskAgentTurnResult:
    """Run one scheduled-task agent turn through Runtime-owned tool plumbing."""

    forced_tool_continuations: dict[str, dict[str, Any]] = {}
    private_provider_tool_call_ids: set[str] = set()
    disable_followup_tools = False
    followup_tool_names = followup_tool_names_from_forced_calls(forced_tool_calls)

    def _followup_tool_surface() -> tuple[list[dict[str, Any]], set[str]]:
        return restrict_provider_followup_tools(
            tools,
            loaded_tool_names,
            disable_followup_tools=disable_followup_tools,
            followup_tool_names=followup_tool_names,
        )

    if forced_tool_calls:
        response_tool_calls: list[dict[str, Any]] = []
        disable_followup_tools = any(
            bool(call.get("disable_followup_tools"))
            for call in forced_tool_calls
            if isinstance(call, dict)
        )
        for index, call in enumerate(forced_tool_calls, start=1):
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            if not name:
                continue
            raw_arguments = call.get("arguments") or call.get("args") or {}
            arguments = (
                dict(raw_arguments)
                if isinstance(raw_arguments, dict)
                else {}
            )
            continuation = pop_tool_continuation(arguments)
            tool_call_id = f"scheduled-skill-{index}"
            if continuation is not None:
                forced_tool_continuations[tool_call_id] = continuation
                private_provider_tool_call_ids.add(tool_call_id)
            response_tool_calls.append({
                "id": tool_call_id,
                "name": name,
                "arguments": arguments,
            })
        response = ChatMessage(
            role="assistant",
            content="",
            tool_calls=response_tool_calls,
            usage={},
        )
    else:
        response = await runtime_execute_task_agent_chat(
            engine=engine,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            metadata=metadata,
            temperature=temperature,
            max_tokens=max_tokens,
            performance_run_id=task_id,
            performance_tool_call_id=performance_tool_call_id,
        )
    usage = dict(response.usage or {})
    _raise_task_provider_error(usage)
    tool_calls = list(response.tool_calls or [])
    if not tool_calls:
        followup_tools, followup_loaded = _followup_tool_surface()
        return RuntimeTaskAgentTurnResult(
            messages=messages,
            tools=followup_tools,
            loaded_tool_names=followup_loaded,
            response_text=response.content or "",
            tool_names=[],
            usage=usage,
            had_tool_calls=False,
            disable_followup_tools=disable_followup_tools,
        )

    prior_artifact_urls: set[str] = set()
    for message in messages:
        if getattr(message, "role", None) == "tool":
            prior_artifact_urls.update(
                runtime_extract_artifact_urls_from_tool_result(getattr(message, "content", None))
            )

    with runtime_artifact_tracking_scope(runtime_artifact_urls=prior_artifact_urls):
        messages.append(response)
        tool_names: list[str] = []
        supervisor_evidence: list[dict[str, str]] = []
        continuation_index = 0
        phase_calls = tool_calls
        current_response = response
        while phase_calls:
            next_phase_calls: list[dict[str, Any]] = []
            executed_assistant_calls: list[dict[str, Any]] = []
            for phase_index, tool_call in enumerate(phase_calls):
                if not isinstance(tool_call, dict):
                    continue
                tool_name, raw_args, tool_call_id = _tool_call_parts(tool_call)
                if not tool_name:
                    continue
                normalized_tool_call_id = str(tool_call_id or "")
                visible_tool_call = (
                    (current_response.tool_calls or [])[phase_index]
                    if phase_index < len(current_response.tool_calls or [])
                    else tool_call
                )
                if normalized_tool_call_id in private_provider_tool_call_ids:
                    visible_tool_call = sanitize_approval_credentials(
                        visible_tool_call
                    )
                executed_assistant_calls.append(dict(visible_tool_call))

                tool_args = _parse_tool_args(raw_args)
                if tool_name == REQUEST_INPUT_TOOL_NAME:
                    # This loop parks the task on the marker result; tell the
                    # tool so it answers in end-turn mode rather than
                    # "unavailable".
                    tool_args = {
                        **tool_args,
                        "_runtime_human_input_from_context": HUMAN_INPUT_MODE_END_TURN,
                    }
                tool_names.extend(
                    _supervisor_tool_names_for_call(tool_name, tool_args)
                )
                runtime_tool_args = dict(tool_args)
                for context_key in ("_runtime_run_id_from_context", "_performance_run_id_from_context", "_runtime_tool_call_id_from_context", "_runtime_tool_attempt_from_context"):
                    runtime_tool_args.pop(context_key, None)
                if task_id:
                    runtime_tool_args.update({
                        "_performance_run_id_from_context": task_id,
                        "_runtime_tool_call_id_from_context": normalized_tool_call_id,
                    })
                tool_result = await runtime_execute_tool(
                    tool_name,
                    runtime_tool_args,
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    active_user_message=active_user_message,
                    tool_profile=tool_profile,
                    allowed_tool_names=allowed_tool_names,
                    runtime_envelope=runtime_envelope,
                )
                runtime_record_tool_result_artifacts(tool_result)
                visible_tool_result = (
                    sanitize_approval_credentials(tool_result)
                    if normalized_tool_call_id in private_provider_tool_call_ids
                    else tool_result
                )
                supervisor_evidence.append(
                    _supervisor_tool_evidence_for_call(
                        tool_name,
                        visible_tool_result,
                    )
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=visible_tool_result,
                        tool_call_id=tool_call_id,
                    )
                )
                input_request = (
                    input_request_from_tool_result(tool_result)
                    if tool_name == REQUEST_INPUT_TOOL_NAME
                    else None
                )
                if input_request is not None:
                    # The agent asked the user something: park here. Calls
                    # issued alongside the question are not executed; each one
                    # already on the assistant message gets a synthetic result
                    # so the transcript stays well-formed, scheduled
                    # continuations are only named, and the resumed run can
                    # reissue them.
                    skipped: list[str] = []
                    later_visible_calls: list[dict[str, Any]] = []
                    for later_index in range(phase_index + 1, len(phase_calls)):
                        later_call = phase_calls[later_index]
                        if not isinstance(later_call, dict):
                            continue
                        later_name, _, later_id = _tool_call_parts(later_call)
                        if not later_name:
                            continue
                        skipped.append(later_name)
                        visible_later_call = (
                            (current_response.tool_calls or [])[later_index]
                            if later_index < len(current_response.tool_calls or [])
                            else later_call
                        )
                        if str(later_id or "") in private_provider_tool_call_ids:
                            visible_later_call = sanitize_approval_credentials(visible_later_call)
                        later_visible_calls.append(dict(visible_later_call))
                        messages.append(
                            ChatMessage(
                                role="tool",
                                content=json.dumps({
                                    "ok": False,
                                    "error": "not_executed",
                                    "reason": "the run paused on request_input before this call; reissue it after the answer",
                                }),
                                tool_call_id=later_id,
                            )
                        )
                    for pending_call in next_phase_calls:
                        if isinstance(pending_call, dict):
                            pending_name, _, _ = _tool_call_parts(pending_call)
                            if pending_name:
                                skipped.append(pending_name)
                    if skipped:
                        input_request = {**input_request, "skipped_tool_calls": skipped}
                    current_response.tool_calls = [*executed_assistant_calls, *later_visible_calls]
                    return RuntimeTaskAgentTurnResult(
                        messages=messages,
                        tools=tools,
                        loaded_tool_names=loaded_tool_names,
                        response_text="",
                        tool_names=tool_names,
                        usage=usage,
                        had_tool_calls=True,
                        supervisor_evidence=supervisor_evidence,
                        input_request=input_request,
                    )
                blocking_control = runtime_detect_blocking_human_gate(
                    tool_name,
                    tool_args,
                    tool_result,
                )
                if blocking_control is not None:
                    # The assistant may have proposed sibling calls in one
                    # batch. Keep only the calls that actually ran so a durable
                    # approval checkpoint never contains unmatched tool calls.
                    current_response.tool_calls = executed_assistant_calls
                    remaining_tool_calls: list[dict[str, Any]] = []
                    for remaining_call in [
                        *phase_calls[phase_index + 1 :],
                        *next_phase_calls,
                    ]:
                        if not isinstance(remaining_call, dict):
                            continue
                        remaining_id = str(remaining_call.get("id") or "")
                        visible_remaining_call = (
                            sanitize_approval_credentials(remaining_call)
                            if remaining_id in private_provider_tool_call_ids
                            else dict(remaining_call)
                        )
                        remaining_tool_calls.append(dict(visible_remaining_call))
                    sequence_owner = runtime_sequence_child_owner(
                        tool_name,
                        tool_result,
                    )
                    blocking_tool_arguments = (
                        sequence_owner["arguments"]
                        if sequence_owner is not None
                        else dict(tool_args)
                    )
                    followup_tools, followup_loaded = _followup_tool_surface()
                    return RuntimeTaskAgentTurnResult(
                        messages=messages,
                        tools=followup_tools,
                        loaded_tool_names=followup_loaded,
                        response_text="",
                        tool_names=tool_names,
                        usage=usage,
                        had_tool_calls=True,
                        supervisor_evidence=supervisor_evidence,
                        blocking_control=blocking_control,
                        blocking_tool_arguments=blocking_tool_arguments,
                        remaining_tool_calls=remaining_tool_calls,
                        disable_followup_tools=disable_followup_tools,
                    )
                terminal_control = runtime_terminal_uncertainty_control(tool_result)
                if terminal_control is not None:
                    current_response.tool_calls = executed_assistant_calls
                    return RuntimeTaskAgentTurnResult(
                        messages=messages,
                        tools=[],
                        loaded_tool_names=set(),
                        response_text=str(
                            terminal_control.get("content")
                            or terminal_control.get("message")
                            or terminal_control.get("error")
                            or terminal_control.get("stop_reason")
                            or ""
                        ),
                        tool_names=tool_names,
                        usage=usage,
                        had_tool_calls=True,
                        supervisor_evidence=supervisor_evidence,
                        disable_followup_tools=True,
                        terminal_control=terminal_control,
                    )
                continuation = forced_tool_continuations.get(
                    normalized_tool_call_id
                )
                parsed_tool_result = _parse_tool_result_object(tool_result)
                retry_call = (
                    retry_call_after_tool_continuation(
                        parsed_tool_result,
                        continuation,
                        set(loaded_tool_names),
                    )
                    if parsed_tool_result is not None
                    else None
                )
                if retry_call is not None:
                    continuation_index += 1
                    generated_call = {
                        "id": f"scheduled-continuation-{continuation_index}",
                        "name": retry_call["name"],
                        "arguments": retry_call["arguments"],
                    }
                    private_provider_tool_call_ids.add(generated_call["id"])
                    next_phase_calls.append(generated_call)
                if tool_name == "search_tools" and str(tool_result).startswith("{"):
                    try:
                        search_result = json.loads(tool_result)
                        if isinstance(search_result, dict):
                            _load_search_result_tool_schemas(
                                tools=tools,
                                loaded_tool_names=loaded_tool_names,
                                search_result=search_result,
                                allowed_tool_names=allowed_tool_names,
                            )
                    except (
                        json.JSONDecodeError,
                        KeyError,
                        TypeError,
                        AttributeError,
                    ):
                        pass

            current_response.tool_calls = executed_assistant_calls
            if not next_phase_calls:
                break
            phase_calls = next_phase_calls
            current_response = ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    dict(sanitize_approval_credentials(call))
                    for call in phase_calls
                ],
                usage={},
            )
            messages.append(current_response)

        followup_tools, followup_loaded = _followup_tool_surface()
        return RuntimeTaskAgentTurnResult(
            messages=messages,
            tools=followup_tools,
            loaded_tool_names=followup_loaded,
            response_text="",
            tool_names=tool_names,
            usage=usage,
            had_tool_calls=True,
            supervisor_evidence=supervisor_evidence,
            disable_followup_tools=disable_followup_tools,
        )


def runtime_parse_task_supervisor_json(raw: str) -> dict[str, Any]:
    """Extract and validate scheduled-task supervisor JSON."""

    text = str(raw or "").strip()
    if not text:
        # An empty supervisor response means the supervisor LLM call itself
        # failed / returned nothing (e.g. a BYOK gateway returning an empty
        # body), NOT that the task spec is broken. Treat review as unavailable
        # and accept the agent's result rather than converting completed work
        # into needs_replan — which the task runner maps to a "blocked" task.
        return {
            "verdict": RUNTIME_TASK_VERDICT_DONE,
            "reason": "supervisor response was empty (review unavailable); accepting agent result",
        }
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            verdict = str(parsed.get("verdict", "")).strip().lower()
            if verdict in RUNTIME_TASK_VALID_VERDICTS:
                parsed["verdict"] = verdict
                return parsed
            return {
                "verdict": RUNTIME_TASK_VERDICT_NEEDS_REPLAN,
                "reason": (f"Supervisor returned unknown verdict={verdict!r}; raw={text[:200]!r}"),
            }
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                verdict = str(parsed.get("verdict", "")).strip().lower()
                if verdict in RUNTIME_TASK_VALID_VERDICTS:
                    parsed["verdict"] = verdict
                    return parsed
        except json.JSONDecodeError:
            pass

    return {
        "verdict": RUNTIME_TASK_VERDICT_NEEDS_REPLAN,
        "reason": f"Could not parse supervisor response: {text[:200]!r}",
    }


def runtime_task_supervisor_prompt(
    *,
    task_title: str,
    agent_response: str,
    done_when: str,
    turns_used: int,
    max_turns: int,
    tools_called: list[str] | None = None,
    tool_evidence: list[dict[str, str]] | None = None,
) -> str:
    """Render the scheduled-task supervisor prompt."""

    response_preview = agent_response[:4000] + ("..." if len(agent_response) > 4000 else "")
    tools_summary = ", ".join(tools_called) if tools_called else "(none -- agent did not invoke any tool)"
    evidence_items = (tool_evidence or [])[-_SUPERVISOR_TOOL_EVIDENCE_MAX_ITEMS:]
    evidence_summary = "\n".join(
        f"- {str(item.get('tool_name') or 'unknown_tool')}: {str(item.get('result') or '')}"
        for item in evidence_items
        if isinstance(item, dict)
    ) or "(none recorded)"
    return (
        "## SYSTEM\n"
        "You are a task execution supervisor. Evaluate the agent's work.\n"
        "Output ONLY valid JSON. No explanation, no markdown fences.\n\n"
        "## TASK\n"
        f"title: {task_title}\n"
        f"done_when: {done_when or '(not specified -- use your judgment)'}\n"
        f"turns_used: {turns_used}/{max_turns}\n\n"
        "## TOOLS THE AGENT ACTUALLY INVOKED\n"
        f"{tools_summary}\n"
        "If the task required a side effect (sending an email, creating "
        "a file, updating a record, etc.) and no relevant tool appears "
        "above, the agent FABRICATED its claim -- return verdict=failed.\n\n"
        "## TOOL RESULT EVIDENCE\n"
        f"{evidence_summary}\n"
        "These are the actual bounded tool receipts for this run, not agent "
        "claims. Preserve their JSON semantics: numeric 0 is a real reported "
        "value, while null is unavailable. Do not reject an output merely "
        "because it reports zero when the receipt shows zero.\n\n"
        "## AGENT OUTPUT\n"
        f"{response_preview}\n\n"
        "## VERDICTS\n"
        "continue    -- agent needs more work (only if turns_used < max_turns)\n"
        "done        -- output satisfies the task requirements\n"
        "failed      -- cannot be completed; explain why\n"
        "needs_hitl  -- requires human judgment before proceeding\n"
        "needs_replan -- task spec is broken\n\n"
        "## OUTPUT (pick ONE)\n"
        '{"verdict":"continue","instruction":"what to do next","reason":"..."}\n'
        '{"verdict":"done","summary":"assessment","reason":"how requirements are met"}\n'
        '{"verdict":"failed","reason":"root cause","retry_strategy":"none|retry_same|retry_different"}\n'
        '{"verdict":"needs_hitl","reason":"why human needed","question":"specific question"}\n'
        '{"verdict":"needs_replan","reason":"what is broken"}\n'
    )


async def runtime_review_task_agent_output(
    *,
    engine: Any,
    task_title: str,
    agent_response: str,
    done_when: str,
    turns_used: int,
    max_turns: int,
    tools_called: list[str] | None = None,
    tool_evidence: list[dict[str, str]] | None = None,
    worker_model: str | None = None,
    metadata: dict[str, Any] | None = None,
    performance_run_id: str | None = None,
    performance_tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Run the scheduled-task supervisor review through Runtime helpers."""

    prompt = runtime_task_supervisor_prompt(
        task_title=task_title,
        agent_response=agent_response,
        done_when=done_when,
        turns_used=turns_used,
        max_turns=max_turns,
        tools_called=tools_called,
        tool_evidence=tool_evidence,
    )
    try:
        response = await runtime_execute_task_supervisor_chat(
            engine=engine,
            prompt=prompt,
            worker_model=worker_model,
            metadata=metadata,
            performance_run_id=performance_run_id,
            performance_tool_call_id=performance_tool_call_id,
        )
        _raise_task_provider_error(response.usage)
    except RuntimeTaskProviderError as exc:
        if exc.retryable:
            raise
        return {
            "verdict": RUNTIME_TASK_VERDICT_FAILED,
            "reason": f"Supervisor provider configuration failed: {exc}",
        }
    except CreditExhaustedError:
        raise
    except Exception as exc:
        return {
            "verdict": RUNTIME_TASK_VERDICT_NEEDS_REPLAN,
            "reason": f"Supervisor unavailable: {exc}",
        }
    return runtime_parse_task_supervisor_json(response.content or "")


async def runtime_execute_task_final_response(
    *,
    engine: Any,
    messages: list[ChatMessage],
    system_prompt: str,
    metadata: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    performance_run_id: str | None = None,
    performance_tool_call_id: str | None = None,
) -> RuntimeTaskFinalResponseResult:
    """Force a final no-tools scheduled-task response after max tool rounds."""

    final_instruction = (
        "You have used all available tool rounds for this scheduled "
        "task. Produce the final deliverable now using the tool "
        "results already in the conversation. Do not call any more "
        "tools. If a requested side effect such as sending email "
        "failed or was unavailable, say that plainly."
    )
    messages.append(ChatMessage(role="user", content=final_instruction))
    response = await runtime_execute_task_final_chat(
        engine=engine,
        messages=messages,
        system_prompt=system_prompt,
        metadata=metadata,
        temperature=temperature,
        max_tokens=max_tokens,
        performance_run_id=performance_run_id,
        performance_tool_call_id=performance_tool_call_id,
    )
    _raise_task_provider_error(response.usage)
    content = response.content or ""
    finalized = bool(content.strip())
    if finalized:
        messages.append(response)
    return RuntimeTaskFinalResponseResult(
        messages=messages,
        response_text=content,
        usage=dict(response.usage or {}),
        finalized=finalized,
    )
