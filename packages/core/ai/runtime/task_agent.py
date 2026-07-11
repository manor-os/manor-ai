from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from packages.core.ai.engine import ChatMessage
from packages.core.ai.runtime.artifacts import (
    runtime_artifact_tracking_scope,
    runtime_extract_artifact_urls_from_tool_result,
    runtime_record_tool_result_artifacts,
)
from packages.core.ai.runtime.billing import runtime_llm_billing_context
from packages.core.ai.runtime.envelope import RuntimeEnvelope
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


def runtime_task_user_prompt(
    *,
    base_prompt: str,
    task_logs: Iterable[Any] | None = None,
    hitl_response: Any | None = None,
    hitl_responded_by: Any | None = None,
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
            "comment",
            "ai_execution_completed",
            "ai_hitl_requested",
        }
    ]
    if relevant_logs:
        convo_lines = ["## Conversation History"]
        for log in relevant_logs[-max_history_entries:]:
            log_type = str(getattr(log, "log_type", "") or "")
            who = getattr(log, "created_by", None) or ("AI Agent" if log_type.startswith("ai_") else "System")
            content = str(getattr(log, "content", "") or "")[:max_log_chars]
            convo_lines.append(f"**{who}:** {content}")
        prompt = f"{prompt}\n\n" + "\n\n".join(convo_lines)

    response = str(hitl_response or "").strip()
    hitl_by = str(hitl_responded_by or "Human").strip() or "Human"
    if response:
        prompt = f"{prompt}\n\n## Human Response (from {hitl_by})\n{response}"

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


def _supervisor_tool_names_for_call(tool_name: str, tool_args: Mapping[str, Any]) -> list[str]:
    """Return supervisor-visible tool evidence for gateway-style tool calls."""

    names = [tool_name]
    if tool_name in {"workspace_agent", "manor"}:
        action = str(tool_args.get("action") or "").strip()
        if action:
            names.append(f"{tool_name}:{action}")
    return names


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
    for match in search_result.get("matches", []):
        if not isinstance(match, dict):
            continue
        schema = match.get("schema")
        if not isinstance(schema, dict):
            continue
        match_name = str(match.get("name") or "").strip()
        schema_name = _schema_name(schema)
        if match_name:
            match_schemas[match_name] = schema
        if schema_name:
            match_schemas[schema_name] = schema

    def _search_schema(name: str) -> dict[str, Any] | None:
        return runtime_tool_schema(name) or match_schemas.get(name)

    search_tool_schema_resolver = runtime_tool_schema_resolver(
        get_schema=_search_schema,
        allowed_tool_names=allowed_tool_names,
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
    legacy_tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimeTaskAgentTurnResult:
    """Run one scheduled-task agent turn through Runtime-owned tool plumbing."""

    response = await runtime_execute_task_agent_chat(
        engine=engine,
        messages=messages,
        tools=tools,
        system_prompt=system_prompt,
        metadata=metadata,
    )
    usage = dict(response.usage or {})
    tool_calls = list(response.tool_calls or [])
    if not tool_calls:
        return RuntimeTaskAgentTurnResult(
            messages=messages,
            tools=tools,
            loaded_tool_names=loaded_tool_names,
            response_text=response.content or "",
            tool_names=[],
            usage=usage,
            had_tool_calls=False,
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
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_name, raw_args, tool_call_id = _tool_call_parts(tool_call)
            if not tool_name:
                continue
            tool_args = _parse_tool_args(raw_args)
            tool_names.extend(_supervisor_tool_names_for_call(tool_name, tool_args))
            tool_result = await runtime_execute_tool(
                tool_name,
                tool_args,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                active_user_message=active_user_message,
                legacy_tool_profile=legacy_tool_profile,
                allowed_tool_names=allowed_tool_names,
                runtime_envelope=runtime_envelope,
            )
            runtime_record_tool_result_artifacts(tool_result)
            messages.append(
                ChatMessage(
                    role="tool",
                    content=tool_result,
                    tool_call_id=tool_call_id,
                )
            )
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
                except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                    pass

        return RuntimeTaskAgentTurnResult(
            messages=messages,
            tools=tools,
            loaded_tool_names=loaded_tool_names,
            response_text="",
            tool_names=tool_names,
            usage=usage,
            had_tool_calls=True,
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
) -> str:
    """Render the scheduled-task supervisor prompt."""

    response_preview = agent_response[:4000] + ("..." if len(agent_response) > 4000 else "")
    tools_summary = ", ".join(tools_called) if tools_called else "(none -- agent did not invoke any tool)"
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
    worker_model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the scheduled-task supervisor review through Runtime helpers."""

    prompt = runtime_task_supervisor_prompt(
        task_title=task_title,
        agent_response=agent_response,
        done_when=done_when,
        turns_used=turns_used,
        max_turns=max_turns,
        tools_called=tools_called,
    )
    try:
        response = await runtime_execute_task_supervisor_chat(
            engine=engine,
            prompt=prompt,
            worker_model=worker_model,
            metadata=metadata,
        )
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
    )
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
