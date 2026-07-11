from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable

from packages.core.ai.runtime.approvals import runtime_capability_id_for_action_key
from packages.core.ai.runtime.billing import runtime_llm_billing_context
from packages.core.ai.runtime.completions import runtime_resolve_text_completion_route
from packages.core.ai.runtime.capabilities import (
    capabilities_for_tool_names,
    capability_for_id,
)
from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.sources import (
    RUNTIME_PLANNER_SOURCE,
    RUNTIME_PLAN_SUPERVISOR_SOURCE,
)


@dataclass(frozen=True)
class RuntimePlannerActionBinding:
    provider: str
    action_key: str
    capability_id: str | None = None
    capability_name: str | None = None
    risk_level: str | None = None
    required_approval: bool | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None

    def to_dict(self, *, include_schema: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "action_key": self.action_key,
        }
        if self.description:
            payload["description"] = self.description
        if self.capability_id:
            payload["capability_id"] = self.capability_id
        if self.capability_name:
            payload["capability_name"] = self.capability_name
        if self.risk_level:
            payload["risk_level"] = self.risk_level
        if self.required_approval is not None:
            payload["required_approval"] = self.required_approval
        if self.input_schema is not None:
            payload["has_input_schema"] = True
            payload["parameters"] = _schema_parameter_names(self.input_schema)
            if include_schema:
                payload["input_schema"] = self.input_schema
        if self.output_schema is not None:
            payload["has_output_schema"] = True
            if include_schema:
                payload["output_schema"] = self.output_schema
        return payload


@dataclass(frozen=True)
class RuntimePlannerCapabilityCatalogEntry:
    capability_id: str
    name: str
    description: str
    risk_level: str
    required_approval: bool
    provider_actions: tuple[RuntimePlannerActionBinding, ...] = ()
    platform_tools: tuple[str, ...] = ()
    skills: tuple[dict[str, Any], ...] = ()
    sources: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level,
            "required_approval": self.required_approval,
        }
        if self.provider_actions:
            payload["provider_actions"] = [binding.to_dict() for binding in self.provider_actions]
        if self.platform_tools:
            payload["platform_tools"] = list(self.platform_tools)
        if self.skills:
            payload["skills"] = list(self.skills)
        if self.sources:
            payload["sources"] = list(self.sources)
        return payload


@dataclass(frozen=True)
class RuntimePlannerChatTurnResult:
    """Result for one Planner LLM turn, including optional tool calls."""

    content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]


RUNTIME_PLAN_JSON_HINT = {
    "steps": [
        {
            "key": "<unique_snake_case>",
            "kind": "<one of llm|action|sleep|human|subagent>",
            "service_key": "<a service from the allowed list>",
            "provider": "<provider key, only if kind=action>",
            "action_key": "<action name, only if kind=action>",
            "capability_id": "<runtime capability id, only if kind=action and known>",
            "params": {"prompt": "<required for llm/subagent steps>"},
            "depends_on": ["<earlier step key>"],
            "expected_output_schema": {"type": "object"},
            "risk_level": "low|medium|high",
            "description": "Human-readable one-liner (<=120 chars).",
        }
    ],
    "metadata": {
        "rationale": "Why this plan shape was chosen.",
        "estimated_cost_usd": 0.05,
    },
}


def _runtime_mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _provider_actions_for_service(
    context: Any,
    service_key: str | None,
) -> dict[str, list[str]]:
    service_provider_actions = _runtime_mapping_get(context, "service_provider_actions", {}) or {}
    if service_provider_actions:
        return dict(service_provider_actions.get(str(service_key or ""), {}) or {})
    return dict(_runtime_mapping_get(context, "provider_actions", {}) or {})


def _provider_action_specs_for_service(
    context: Any,
    service_key: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    service_provider_specs = _runtime_mapping_get(context, "service_provider_action_specs", {}) or {}
    if service_provider_specs:
        return dict(service_provider_specs.get(str(service_key or ""), {}) or {})
    return dict(_runtime_mapping_get(context, "provider_action_specs", {}) or {})


def runtime_planner_system_prompt(
    *,
    subscriptions: Iterable[Any],
    agents_by_id: Mapping[str, Any] | dict[str, Any],
    allowed_service_keys: Iterable[str],
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    document_groups: Iterable[Mapping[str, Any]] | None = None,
    staff: Iterable[Mapping[str, Any]] | None = None,
    agent_tool_names: Mapping[str, Iterable[str]] | None = None,
    agent_skill_names: Mapping[str, Iterable[dict[str, Any]]] | None = None,
) -> str:
    """Render the Runtime-owned Planner system prompt."""

    allowed = set(allowed_service_keys or ())
    services_block = "\n".join(
        f"  - {service_key} (subscription_id={subscription_id}, "
        f"agent={getattr(agents_by_id[agent_id], 'name', 'unknown') if agent_id in agents_by_id else 'unknown'})"
        for subscription in subscriptions
        for service_key, subscription_id, agent_id in [(
            str(getattr(subscription, "service_key", "") or ""),
            getattr(subscription, "id", None),
            getattr(subscription, "agent_id", None),
        )]
        if service_key in allowed
    ) or "  (none \u2014 Planner can only emit llm/sleep/human steps)"

    capabilities_block = runtime_planner_capability_catalog_text(
        provider_actions=provider_actions or {},
        provider_action_specs=provider_action_specs or {},
        platform_tools={
            name
            for names in (agent_tool_names or {}).values()
            for name in names
        },
        skills=[
            skill
            for skills in (agent_skill_names or {}).values()
            for skill in skills
        ],
    )

    schema_hint = json.dumps(RUNTIME_PLAN_JSON_HINT, indent=2)
    docs = list(document_groups or [])
    staff_members = list(staff or [])
    docs_block = "\n".join(
        f"  - {doc['name']} (id={doc['id']})" for doc in docs
    ) if docs else "  (none)"
    staff_block = "\n".join(
        f"  - staff_id={member['staff_id']} role={member.get('role', 'member')}"
        for member in staff_members
    ) if staff_members else "  (none)"

    return (
        "You are the Planner for Manor \u2014 a goal-driven workspace runtime "
        "for solo founders. Given a Task, build a Plan: a DAG of "
        "concrete steps the system will execute.\n\n"
        "IMPORTANT: Use the tools (list_tools, get_tool_schema) to research "
        "available capabilities BEFORE committing to a plan. Then call "
        "submit_plan with the final JSON.\n\n"
        "Allowed services (use for step.service_key \u2014 ONLY these are permitted):\n"
        f"{services_block}\n\n"
        "Allowed runtime capabilities and bindings:\n"
        f"{capabilities_block}\n\n"
        "Workspace documents (only reference these \u2014 no external documents):\n"
        f"{docs_block}\n\n"
        "Assigned staff (for human steps \u2014 only assign to these people):\n"
        f"{staff_block}\n\n"
        "Step kinds:\n"
        "  llm              single LLM call (no side effects)\n"
        "  action           call provider.action_key with capability_id (real side effect)\n"
        "  sleep            wait params.seconds or until params.until\n"
        "  human            pause for user input (params.prompt shown to staff)\n"
        "  subagent         multi-turn LLM with tools\n\n"
        "Constraints:\n"
        "  * step.service_key MUST be from the allowed services list above\n"
        "  * action steps MUST choose capability_id first, then provider+action_key from that capability's actions\n"
        "  * action steps SHOULD include capability_id whenever the capability catalog shows one\n"
        "  * llm/subagent steps MUST put the natural-language work request in params.prompt\n"
        "  * If the task deliverable is a user-visible artifact (file, image, PDF, "
        "document, deck, spreadsheet, video, export, attachment, or domain-specific file), "
        "do not satisfy it with a plain text-only LLM step. Use a subagent step whose "
        "prompt explicitly instructs the agent to call an available file/media tool such as `generate_file`, "
        "and set output_shape to the canonical shape the step produces — one of: "
        "ArtifactResult, TextResult, DocumentResult, ListResult, PublishResult, CountResult, EmptyResult. "
        "Do not invent output field names; the shape owns them (e.g. ArtifactResult provides files[].fs_path). "
        "A legacy expected_output_schema is still accepted but output_shape is preferred.\n"
        "  * For workspace tasks, do not ask the user where to save generated files. "
        "Runtime file/media tools are automatically scoped to the current workspace's "
        "artifact folder. Only skip saving when the task explicitly asks for text-only "
        "output or no saved file.\n"
        "  * Do not use requires_approval to encode approval policy. Approval, "
        "always-allow, and deny rules are inherited from workspace governance "
        "policy. Use a human step only when the task genuinely needs missing "
        "input or a user decision to proceed.\n"
        "  * human steps should only involve assigned staff above\n"
        "  * document references must use workspace documents above\n\n"
        "Reference earlier step output in params with "
        '"${{ steps.<step_key>.result.<path> }}".\n\n'
        "Replanning:\n"
        "  * If the task Details JSON contains `_replan_context` with "
        "`succeeded_steps` (each with a result summary) and one or a few "
        "`failed_steps`: produce a MINIMAL plan — redo only the failed "
        "step(s); do NOT regenerate steps that already succeeded, reuse their "
        "outputs by referencing the prior result. Regenerate the whole plan "
        "only when the failure is structural (the original DAG itself was "
        "wrong, not just one step's output).\n\n"
        "Use submit_plan tool to submit the final plan as JSON.\n"
        f"Plan JSON shape:\n{schema_hint}\n"
    )


def runtime_planner_predecessor_outputs(dep_outputs: Any) -> str:
    """Render predecessor task outputs for Planner task prompts."""

    if not isinstance(dep_outputs, list) or not dep_outputs:
        return ""
    lines: list[str] = []
    for index, dep in enumerate(dep_outputs[:6], start=1):
        if not isinstance(dep, dict):
            continue
        title = dep.get("task_title") or dep.get("task_id") or f"Predecessor {index}"
        lines.append(f"## {title}")
        if dep.get("result_summary"):
            lines.append(f"Summary: {dep['result_summary']}")
        files = dep.get("files")
        if isinstance(files, list) and files:
            lines.append("Files:")
            for file in files[:10]:
                if not isinstance(file, dict):
                    continue
                label = (
                    file.get("name")
                    or file.get("filename")
                    or file.get("fs_path")
                    or file.get("url")
                    or "file"
                )
                ref = file.get("fs_path") or file.get("url") or file.get("document_id") or ""
                lines.append(f"- {label}" + (f" ({ref})" if ref and ref != label else ""))
    return "\n".join(lines)


def runtime_planner_task_prompt(task: Any) -> str:
    """Render the Runtime-owned Planner task/user prompt."""

    title = _runtime_mapping_get(task, "title", "")
    description = _runtime_mapping_get(task, "description")
    details = _runtime_mapping_get(task, "details") or {}
    input_contract = _runtime_mapping_get(task, "input_contract")
    expected_output = _runtime_mapping_get(task, "expected_output")
    owner_service_key = _runtime_mapping_get(task, "owner_service_key")
    delegate_service_keys = _runtime_mapping_get(task, "delegate_service_keys")

    parts = [f"# Task to plan\nTitle: {title}"]
    if description:
        parts.append(f"Description: {description}")
    predecessor_outputs = runtime_planner_predecessor_outputs(
        details.get("dep_outputs") if isinstance(details, dict) else None
    )
    if predecessor_outputs:
        parts.append("# Predecessor task outputs\n" + predecessor_outputs)
    if details:
        parts.append(f"Details JSON: {json.dumps(details, ensure_ascii=False, default=str)}")
    if input_contract:
        parts.append(
            f"Input contract (JSON Schema): {json.dumps(input_contract, ensure_ascii=False)}"
        )
    if expected_output:
        parts.append(
            f"Expected output (JSON Schema): {json.dumps(expected_output, ensure_ascii=False)}"
        )
    if owner_service_key:
        parts.append(f"Owner service: {owner_service_key}")
    if delegate_service_keys:
        parts.append(f"Allowed delegates: {', '.join(delegate_service_keys)}")
    return "\n\n".join(parts)


def runtime_planner_llm_billing_context(
    *,
    entity_id: str,
    workspace_id: str | None = None,
    source: str = RUNTIME_PLANNER_SOURCE,
) -> Any:
    """Build the LLM billing context for Planner generation."""

    return runtime_llm_billing_context(
        entity_id,
        workspace_id=workspace_id,
        source=source,
    )


def runtime_planner_tool_schemas() -> list[dict[str, Any]]:
    """Build the Runtime-owned tool schemas available to the Planner agent."""

    return [
        {
            "type": "function",
            "function": {
                "name": "list_tools",
                "description": (
                    "List available tools/actions for a service in this workspace. "
                    "Call this to discover what actions an agent can perform before planning steps."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service_key": {
                            "type": "string",
                            "description": "The service_key to look up tools for",
                        },
                    },
                    "required": ["service_key"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tool_schema",
                "description": (
                    "Get the input/output schema for a specific tool action. "
                    "Call this before using an action in a step to understand what params it needs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "description": "Provider key"},
                        "action_key": {"type": "string", "description": "Action name"},
                        "service_key": {
                            "type": "string",
                            "description": "Optional service_key to validate the action against.",
                        },
                    },
                    "required": ["provider", "action_key"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_plan",
                "description": (
                    "Submit the final execution plan. Call this when you've finished planning. "
                    "The plan_json must be valid JSON matching the Plan schema with steps array and metadata."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_json": {
                            "type": "string",
                            "description": (
                                "The complete plan as a JSON string with "
                                "{steps: [...], metadata: {...}}"
                            ),
                        },
                    },
                    "required": ["plan_json"],
                },
            },
        },
    ]


def runtime_execute_planner_tool_call(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    context: Any,
    parse_plan: Any | None = None,
    enforce_plan: Any | None = None,
) -> dict[str, Any] | str:
    """Execute a planner-only tool call against already-gathered runtime context."""

    tool_args = args or {}
    subscriptions = list(_runtime_mapping_get(context, "subscriptions", []) or [])
    agents_by_id = _runtime_mapping_get(context, "agents_by_id", {}) or {}
    allowed_service_keys = set(_runtime_mapping_get(context, "allowed_service_keys", []) or [])
    provider_actions = _runtime_mapping_get(context, "provider_actions", {}) or {}
    provider_action_specs = _runtime_mapping_get(context, "provider_action_specs", {}) or {}
    document_groups = _runtime_mapping_get(context, "document_groups", []) or []
    staff = _runtime_mapping_get(context, "staff", []) or []
    agent_tool_names = _runtime_mapping_get(context, "agent_tool_names", {}) or {}
    agent_skill_names = _runtime_mapping_get(context, "agent_skill_names", {}) or {}

    if tool_name == "list_tools":
        service_key = str(tool_args.get("service_key") or "")
        subscription = next(
            (
                sub for sub in subscriptions
                if getattr(sub, "service_key", None) == service_key
            ),
            None,
        )
        if not subscription:
            return {"error": f"No subscription found for service_key '{service_key}'"}

        agent_id = getattr(subscription, "agent_id", None)
        agent = agents_by_id.get(agent_id) if agent_id else None
        agent_name = getattr(agent, "name", "unknown") if agent else "unknown"
        platform_tools = list(agent_tool_names.get(getattr(agent, "id", ""), [])) if agent else []
        agent_skills = list(agent_skill_names.get(getattr(agent, "id", ""), [])) if agent else []
        service_provider_actions = _provider_actions_for_service(context, service_key)
        service_provider_specs = _provider_action_specs_for_service(context, service_key)
        action_bindings = [
            binding.to_dict()
            for binding in runtime_planner_action_bindings(
                service_provider_actions,
                provider_action_specs=service_provider_specs,
            )
        ]
        return {
            "service_key": service_key,
            "agent_name": agent_name,
            "agent_description": (getattr(agent, "system_prompt", "") or "")[:500] if agent else "",
            "capability_catalog": runtime_planner_capability_catalog_dicts(
                provider_actions=service_provider_actions,
                provider_action_specs=service_provider_specs,
                platform_tools=platform_tools,
                skills=agent_skills,
            ),
            "action_bindings": action_bindings,
            "available_actions": action_bindings,
            "platform_tools": platform_tools,
            "skills": agent_skills,
            "step_kinds": ["llm", "action", "sleep", "human", "subagent"],
            "hint": (
                "Plan capability-first: choose capability_id from capability_catalog, "
                "then use action_bindings only when an action step needs provider/action_key. "
                "For subagent steps, the agent can use platform_tools + skills during "
                "multi-turn execution."
            ),
            "available_documents": document_groups,
            "assigned_staff": staff,
            "allowed_services": sorted(allowed_service_keys),
        }

    if tool_name == "get_tool_schema":
        provider = str(tool_args.get("provider") or "")
        action_key = str(tool_args.get("action_key") or "")
        service_key = str(tool_args.get("service_key") or "")
        scoped_provider_actions = (
            _provider_actions_for_service(context, service_key)
            if service_key else provider_actions
        )
        scoped_provider_specs = (
            _provider_action_specs_for_service(context, service_key)
            if service_key else provider_action_specs
        )
        binding = runtime_planner_action_binding_for(
            provider=provider,
            action_key=action_key,
            provider_actions=scoped_provider_actions,
            provider_action_specs=scoped_provider_specs,
        )
        if binding is None:
            actions = scoped_provider_actions.get(provider, [])
            return {
                "error": (
                    f"Action '{action_key}' not found in provider '{provider}'. "
                    f"Available: {actions}"
                )
            }
        payload = binding.to_dict(include_schema=True)
        payload["available"] = True
        payload["hint"] = (
            "Use this binding in a kind='action' step with provider, action_key, "
            "capability_id, and params matching input_schema when present."
        )
        return payload

    if tool_name == "submit_plan":
        if parse_plan is None:
            return {"error": "Plan parser unavailable."}
        plan_json = tool_args.get("plan_json", "")
        plan = parse_plan(plan_json)
        if plan is None:
            return {"error": "Invalid plan JSON. Check the schema and try again."}
        if enforce_plan is not None:
            try:
                enforce_plan(plan)
            except Exception as exc:
                return {"error": f"Plan validation failed: {exc}"}
        return {"message": "Plan accepted and validated.", "_plan": plan}

    return {"error": f"Unknown tool: {tool_name}"}


def runtime_planner_user_message(content: str) -> dict[str, Any]:
    """Build a Runtime-owned Planner user message."""

    return {"role": "user", "content": content}


def runtime_planner_assistant_message(
    result: RuntimePlannerChatTurnResult,
) -> dict[str, Any]:
    """Build the assistant message that carries a Planner turn result forward."""

    payload: dict[str, Any] = {
        "role": "assistant",
        "content": result.content or "",
    }
    if result.tool_calls:
        payload["tool_calls"] = list(result.tool_calls)
    return payload


def runtime_planner_tool_message(content: str, *, tool_call_id: str | None = None) -> dict[str, Any]:
    """Build a Planner tool result message for the next LLM turn."""

    payload: dict[str, Any] = {"role": "tool", "content": content}
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    return payload


RUNTIME_PLAN_SUPERVISOR_VERDICTS = ("completed", "needs_replan", "needs_human", "failed")


def runtime_plan_supervisor_prompt(
    *,
    task_title: str,
    task_description: str,
    done_count: int,
    failed_count: int,
    skipped_count: int,
    step_lines: Iterable[str],
) -> str:
    """Build the Runtime-owned supervisor prompt for executed plans."""

    return (
        f"You are a task supervisor. A plan just finished executing.\n\n"
        f"Task: {task_title}\n"
        f"Description: {task_description}\n\n"
        f"Plan result: {done_count} steps done, {failed_count} failed, "
        f"{skipped_count} skipped\n\n"
        f"Steps:\n" + "\n".join(step_lines) + "\n\n"
        "Before the parent task status is changed, decide whether the "
        "task objective was actually achieved by these outputs. Do not "
        "mark completed only because every execution step is marked done.\n\n"
        "Choose ONE verdict:\n"
        "- completed - the task goal was achieved and any required deliverable exists\n"
        "- needs_replan - the system should try a different plan without asking the user\n"
        "- needs_human - missing input, access, approval, source material, or user decision is required\n"
        "- failed - the task cannot be completed or is permanently invalid\n\n"
        "Respond with ONLY the verdict word, nothing else."
    )


def runtime_plan_supervisor_messages(
    *,
    task_title: str,
    task_description: str,
    done_count: int,
    failed_count: int,
    skipped_count: int,
    step_lines: Iterable[str],
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for plan supervisor verdicts."""

    return [{
        "role": "user",
        "content": runtime_plan_supervisor_prompt(
            task_title=task_title,
            task_description=task_description,
            done_count=done_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            step_lines=step_lines,
        ),
    }]


async def runtime_execute_plan_supervisor_completion(
    *,
    task_title: str,
    task_description: str,
    done_count: int,
    failed_count: int,
    skipped_count: int,
    step_lines: Iterable[str],
    entity_id: str | None,
    workspace_id: str | None = None,
) -> RuntimeTextCompletionResult:
    """Execute the Plan supervisor verdict call with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_plan_supervisor_messages(
            task_title=task_title,
            task_description=task_description,
            done_count=done_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            step_lines=step_lines,
        ),
        entity_id=entity_id,
        source=RUNTIME_PLAN_SUPERVISOR_SOURCE,
        workspace_id=workspace_id,
        temperature=0.1,
        max_tokens=20,
    )


def runtime_parse_plan_supervisor_verdict(raw: str | None) -> str:
    """Parse a Runtime plan supervisor verdict from plain text or JSON."""

    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            text = str(parsed.get("verdict") or parsed.get("status") or "").strip()
    except Exception:
        pass
    verdict = text.lower().split()[0].strip("`'\".,:;")
    if verdict in RUNTIME_PLAN_SUPERVISOR_VERDICTS:
        return verdict
    return ""


async def runtime_execute_planner_chat_turn(
    *,
    messages: Iterable[dict[str, Any]],
    tools: list[dict[str, Any]],
    system_prompt: str,
    entity_id: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    temperature: float = 0.7,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimePlannerChatTurnResult:
    """Run one Planner chat turn with Runtime-owned LLM/tool-call plumbing."""

    from packages.core.ai.llm_client import chat_completion_with_tools

    wire_messages: list[dict[str, Any]] = []
    if system_prompt:
        wire_messages.append({"role": "system", "content": system_prompt})
    wire_messages.extend(dict(message) for message in messages)

    resolved_model, resolved_metadata, _resolved_byok = await runtime_resolve_text_completion_route(
        entity_id=entity_id,
        user_id=user_id,
        source=RUNTIME_PLANNER_SOURCE,
        model=model,
        metadata=metadata,
    )

    content, tool_calls, usage = await chat_completion_with_tools(
        wire_messages,
        tools,
        temperature=temperature,
        model=resolved_model,
        metadata=resolved_metadata,
    )
    return RuntimePlannerChatTurnResult(
        content=content or "",
        tool_calls=list(tool_calls or []),
        usage=dict(usage or {}),
    )


def runtime_planner_action_bindings(
    provider_actions: dict[str, list[str]] | None,
    *,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> tuple[RuntimePlannerActionBinding, ...]:
    bindings: list[RuntimePlannerActionBinding] = []
    for provider, actions in (provider_actions or {}).items():
        provider_key = str(provider or "").strip()
        if not provider_key:
            continue
        action_specs = (provider_action_specs or {}).get(provider_key) or {}
        for action in _unique_clean_strings(actions):
            capability = capability_for_id(
                runtime_capability_id_for_action_key(action, provider=provider_key)
            )
            spec = action_specs.get(action) or {}
            bindings.append(
                RuntimePlannerActionBinding(
                    provider=provider_key,
                    action_key=action,
                    capability_id=capability.id if capability else None,
                    capability_name=capability.name if capability else None,
                    risk_level=capability.risk_level if capability else None,
                    required_approval=capability.required_approval if capability else None,
                    description=_clean_description(spec.get("description")),
                    input_schema=_first_schema(
                        spec.get("input_schema"),
                        spec.get("parameters"),
                    ),
                    output_schema=_first_schema(
                        spec.get("output_schema"),
                        spec.get("result_schema"),
                    ),
                )
            )
    return tuple(sorted(bindings, key=lambda item: (item.capability_id or "", item.provider, item.action_key)))


def runtime_planner_action_binding_for(
    *,
    provider: str,
    action_key: str,
    provider_actions: dict[str, list[str]] | None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> RuntimePlannerActionBinding | None:
    provider_key = str(provider or "").strip()
    action = str(action_key or "").strip()
    if not provider_key or not action:
        return None
    for binding in runtime_planner_action_bindings(
        provider_actions,
        provider_action_specs=provider_action_specs,
    ):
        if binding.provider == provider_key and binding.action_key == action:
            return binding
    return None


def runtime_planner_action_specs_from_tools_cached(
    tools_cached: Any,
) -> dict[str, dict[str, Any]]:
    """Normalize MCP/OpenAI-style cached tool specs into action specs.

    The cache shape is intentionally loose because built-in MCP wrappers,
    remote MCP ``tools/list``, and older seed data do not all use the same
    field names. Runtime owns this normalization so planners can depend on a
    stable action binding catalog instead of re-parsing every provider shape.
    """
    specs: dict[str, dict[str, Any]] = {}
    for raw_tool in _iter_cached_tool_specs(tools_cached):
        spec = _normalize_cached_tool_spec(raw_tool)
        action_key = str(spec.get("action_key") or "").strip()
        if not action_key:
            continue
        specs[action_key] = spec
    return specs


def runtime_apply_action_binding_schemas_to_steps(
    steps: Iterable[Any],
    *,
    provider_actions: dict[str, list[str]] | None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> int:
    """Attach Runtime action binding schemas to mutable plan-like steps.

    Planner output can omit ``expected_input_schema`` / ``expected_output_schema``
    after using ``get_tool_schema`` because the provider/action binding already
    carries the schema. This helper lets callers hydrate those fields from the
    Runtime catalog before materializing ExecutionStep rows.
    """
    attached = 0
    for step in steps or ():
        if str(getattr(step, "kind", "") or "") != "action":
            continue
        binding = runtime_planner_action_binding_for(
            provider=str(getattr(step, "provider", "") or ""),
            action_key=str(getattr(step, "action_key", "") or ""),
            provider_actions=provider_actions,
            provider_action_specs=provider_action_specs,
        )
        if binding is None:
            continue
        if binding.input_schema is not None and not getattr(step, "expected_input_schema", None):
            setattr(step, "expected_input_schema", binding.input_schema)
            attached += 1
        if binding.output_schema is not None and not getattr(step, "expected_output_schema", None):
            setattr(step, "expected_output_schema", binding.output_schema)
            attached += 1
    return attached


def runtime_planner_capability_catalog(
    *,
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    platform_tools: Iterable[str] | None = None,
    skills: Iterable[dict[str, Any]] | None = None,
) -> tuple[RuntimePlannerCapabilityCatalogEntry, ...]:
    grouped_actions: dict[str, list[RuntimePlannerActionBinding]] = {}
    for binding in runtime_planner_action_bindings(
        provider_actions,
        provider_action_specs=provider_action_specs,
    ):
        if binding.capability_id:
            grouped_actions.setdefault(binding.capability_id, []).append(binding)

    grouped_tools: dict[str, set[str]] = {}
    tool_names = set(_unique_clean_strings(platform_tools))
    for capability in capabilities_for_tool_names(tool_names):
        grouped_tools.setdefault(capability.id, set()).update(
            name for name in capability.tool_names if name in tool_names
        )

    skill_list = tuple(_normalized_skill_dicts(skills))
    skill_capability_ids = {"skill.invoke"} if skill_list else set()

    capability_ids = sorted(set(grouped_actions) | set(grouped_tools) | skill_capability_ids)
    entries: list[RuntimePlannerCapabilityCatalogEntry] = []
    for capability_id in capability_ids:
        capability = capability_for_id(capability_id)
        if capability is None:
            continue
        sources: list[str] = []
        if grouped_actions.get(capability_id):
            sources.append("provider_action")
        if grouped_tools.get(capability_id):
            sources.append("platform_tool")
        entry_skills = skill_list if capability_id == "skill.invoke" else ()
        if entry_skills:
            sources.append("skill")
        entries.append(
            RuntimePlannerCapabilityCatalogEntry(
                capability_id=capability.id,
                name=capability.name,
                description=capability.description,
                risk_level=capability.risk_level,
                required_approval=capability.required_approval,
                provider_actions=tuple(grouped_actions.get(capability_id, ())),
                platform_tools=tuple(sorted(grouped_tools.get(capability_id, ()))),
                skills=entry_skills,
                sources=tuple(sources),
            )
        )
    return tuple(entries)


def runtime_planner_capability_catalog_dicts(
    *,
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    platform_tools: Iterable[str] | None = None,
    skills: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        entry.to_dict()
        for entry in runtime_planner_capability_catalog(
            provider_actions=provider_actions,
            provider_action_specs=provider_action_specs,
            platform_tools=platform_tools,
            skills=skills,
        )
    ]


def runtime_planner_capability_catalog_text(
    *,
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    platform_tools: Iterable[str] | None = None,
    skills: Iterable[dict[str, Any]] | None = None,
) -> str:
    entries = runtime_planner_capability_catalog(
        provider_actions=provider_actions,
        provider_action_specs=provider_action_specs,
        platform_tools=platform_tools,
        skills=skills,
    )
    if not entries:
        return "  (no runtime capabilities available)"
    lines: list[str] = []
    for entry in entries:
        approval = "approval" if entry.required_approval else "auto"
        lines.append(f"  - {entry.capability_id} ({entry.risk_level}, {approval}): {entry.name}")
        if entry.provider_actions:
            actions = ", ".join(
                f"{binding.provider}.{binding.action_key}"
                for binding in entry.provider_actions[:8]
            )
            lines.append(f"    actions: {actions}")
        if entry.platform_tools:
            lines.append(f"    platform_tools: {', '.join(entry.platform_tools[:8])}")
        if entry.skills:
            skill_names = ", ".join(str(skill.get("name") or skill.get("slug")) for skill in entry.skills[:8])
            lines.append(f"    skills: {skill_names}")
    return "\n".join(lines)


def _unique_clean_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _iter_cached_tool_specs(tools_cached: Any) -> Iterable[Any]:
    if isinstance(tools_cached, dict):
        for key in ("tools", "items", "data"):
            nested = tools_cached.get(key)
            if isinstance(nested, list):
                yield from nested
                return
        for key, value in tools_cached.items():
            if isinstance(value, dict):
                spec = dict(value)
                spec.setdefault("name", key)
                yield spec
            elif isinstance(value, str):
                yield {"name": key, "description": value}
        return
    if isinstance(tools_cached, list):
        yield from tools_cached
        return
    if isinstance(tools_cached, tuple):
        yield from tools_cached


def _normalize_cached_tool_spec(raw_tool: Any) -> dict[str, Any]:
    if isinstance(raw_tool, str):
        return {"action_key": raw_tool.strip()}
    if not isinstance(raw_tool, dict):
        return {}

    fn = raw_tool.get("function") if isinstance(raw_tool.get("function"), dict) else {}
    action_key = (
        fn.get("name")
        or raw_tool.get("name")
        or raw_tool.get("action_key")
        or raw_tool.get("tool")
    )
    action = str(action_key or "").strip()
    if not action:
        return {}

    input_schema = _first_schema(
        fn.get("parameters"),
        raw_tool.get("parameters"),
        raw_tool.get("inputSchema"),
        raw_tool.get("input_schema"),
        raw_tool.get("schema"),
    )
    output_schema = _first_schema(
        raw_tool.get("outputSchema"),
        raw_tool.get("output_schema"),
        raw_tool.get("resultSchema"),
        raw_tool.get("result_schema"),
        raw_tool.get("returns"),
    )
    return {
        "action_key": action,
        "description": _clean_description(
            fn.get("description") or raw_tool.get("description")
        ),
        "input_schema": input_schema,
        "output_schema": output_schema,
    }


def _first_schema(*values: Any) -> dict[str, Any] | None:
    for value in values:
        schema = _schema_if_dict(value)
        if schema is not None:
            return schema
    return None


def _schema_if_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _schema_parameter_names(schema: dict[str, Any] | None) -> list[str]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return [str(name) for name in properties.keys()]
    return []


def _clean_description(value: Any) -> str | None:
    description = str(value or "").strip()
    return description[:240] if description else None


def _normalized_skill_dicts(values: Iterable[dict[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for value in values or ():
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or value.get("slug") or "").strip()
        if not name:
            continue
        skill = {
            "name": name,
            "slug": str(value.get("slug") or name).strip(),
        }
        description = str(value.get("description") or "").strip()
        if description:
            skill["description"] = description[:180]
        out.append(skill)
    return tuple(out)
