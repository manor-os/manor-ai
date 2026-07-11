"""LLM-driven Planner.

Inputs: a Task (already created with owner_service_key + delegates) +
its workspace context (active subscriptions, available providers /
actions, relevant memories).

Output: a Pydantic ``Plan`` validated against the canonical schema.

Flow:
  1. Resolve workspace context — subscriptions, allowed actions per
     subscription, recent relevant memory.
  2. Build the system + user prompt.
  3. Single Claude call returning JSON.
  4. ``Plan.model_validate_json`` — on failure, one re-prompt with
     the validation error attached.
  5. Cross-check against workspace allowlists (service_keys, actions)
     so the Planner can't conjure capabilities the workspace lacks.
  6. Persist via ``plans.service.create_plan_from_dag``.

If the LLM is unreachable / no API key, falls back to a tiny
rule-based stub so Demo A v0 still demonstrates end-to-end execution
during dev / CI.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_apply_action_binding_schemas_to_steps,
    runtime_capability_id_for_action_key,
    runtime_execute_planner_chat_turn,
    runtime_execute_planner_tool_call,
    runtime_planner_assistant_message,
    runtime_planner_action_specs_from_tools_cached,
    runtime_planner_llm_billing_context,
    runtime_planner_system_prompt,
    runtime_planner_task_prompt,
    runtime_planner_tool_schemas,
    runtime_planner_tool_message,
    runtime_planner_user_message,
)
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.service import (
    PlanContractError,
    create_plan_from_dag,
    plan_contract_gaps,
)

logger = logging.getLogger(__name__)


PLANNER_VERSION = "v0.1-demo-a"


class PlannerError(Exception):
    """Planner couldn't produce a valid plan after retries."""


class CapabilityError(Exception):
    """Planner produced a plan referencing capabilities not in the
    workspace's allowlists. Should never happen if the prompt was
    followed; we re-validate as a safety net."""


# ── Public entry point ────────────────────────────────────────────────

async def plan_task(
    db: AsyncSession,
    task_id: str,
    *,
    execution_mode: Optional[str] = None,
) -> ExecutionPlan:
    """Generate + persist a Plan for ``task_id``. Raises if the task
    doesn't exist or the Planner can't produce a valid plan.

    ``execution_mode`` defaults to whatever the task's workspace asks
    for — sandbox workspaces get ``sandbox`` automatically, regular
    workspaces get ``live``. Callers can still force a specific mode
    (eg. UI "Run as dry-run preview" button)."""
    task = (await db.execute(
        select(Task).where(Task.id == task_id)
    )).scalar_one_or_none()
    if not task:
        raise PlannerError(f"task {task_id} not found")

    context = await _gather_context(db, task)

    if execution_mode is None:
        from packages.core.workspaces import default_execution_mode
        execution_mode = (
            default_execution_mode(context.workspace)
            if context.workspace else "live"
        )

    async with runtime_planner_llm_billing_context(
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
    ):
        plan = await _generate_plan(task, context)
    _enforce_allowlists(plan, context)

    gaps = plan_contract_gaps(plan.topo_order())
    if gaps:
        # Re-plan once, feeding the gaps back to the Planner via task.details
        # (the prompt dumps Details JSON, so _replan_context reaches the LLM).
        # MERGE into any existing _replan_context — a runtime replan
        # (executor._maybe_replan) may have populated prior_plan_id /
        # succeeded_steps / failed_steps that lineage (_replan_parent_plan_id)
        # and the minimal-replan guidance depend on; don't clobber them.
        existing_ctx = (task.details or {}).get("_replan_context")
        replan_ctx = dict(existing_ctx) if isinstance(existing_ctx, dict) else {}
        replan_ctx.setdefault("reason", "contract_gaps")
        replan_ctx["contract_gaps"] = "; ".join(
            f"{g.step_key}: {g.detail}" for g in gaps
        )
        task.details = {**(task.details or {}), "_replan_context": replan_ctx}
        async with runtime_planner_llm_billing_context(
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
        ):
            plan = await _generate_plan(task, context)
        _enforce_allowlists(plan, context)
        gaps = plan_contract_gaps(plan.topo_order())
        if gaps:
            raise PlanContractError(gaps)

    return await create_plan_from_dag(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        agent_subscription_id=task.owner_subscription_id,
        plan=plan,
        planner_version=PLANNER_VERSION,
        parent_plan_id=_replan_parent_plan_id(task),
        execution_mode=execution_mode,
    )


def _replan_parent_plan_id(task: Task) -> str | None:
    """Preserve the failed-plan lineage when Planner is called for a replan."""
    details = task.details if isinstance(task.details, dict) else {}
    context = details.get("_replan_context")
    if not isinstance(context, dict):
        return None
    prior_plan_id = context.get("prior_plan_id")
    if isinstance(prior_plan_id, str) and prior_plan_id.strip():
        return prior_plan_id.strip()
    return None


# ── Context gathering ─────────────────────────────────────────────────

class _Context:
    """All the workspace state the Planner is allowed to draw on."""

    def __init__(
        self,
        workspace: Optional[Workspace],
        subscriptions: list[AgentSubscription],
        agents_by_id: dict[str, Agent],
        allowed_service_keys: set[str],
        provider_actions: dict[str, list[str]],
        provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
        service_provider_actions: dict[str, dict[str, list[str]]] | None = None,
        service_provider_action_specs: (
            dict[str, dict[str, dict[str, dict[str, Any]]]] | None
        ) = None,
        document_groups: list[dict] | None = None,
        staff: list[dict] | None = None,
        agent_tool_names: dict[str, list[str]] | None = None,
        agent_skill_names: dict[str, list[dict]] | None = None,
    ):
        self.workspace = workspace
        self.subscriptions = subscriptions
        self.agents_by_id = agents_by_id
        self.allowed_service_keys = allowed_service_keys
        self.provider_actions = provider_actions
        self.provider_action_specs = provider_action_specs or {}
        self.service_provider_actions = service_provider_actions or {}
        self.service_provider_action_specs = service_provider_action_specs or {}
        self.document_groups = document_groups or []
        self.staff = staff or []
        self.agent_tool_names = agent_tool_names or {}
        self.agent_skill_names = agent_skill_names or {}


async def _gather_context(db: AsyncSession, task: Task) -> _Context:
    workspace: Optional[Workspace] = None
    if task.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == task.workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()

    subs: list[AgentSubscription] = []
    if task.workspace_id:
        subs = list((await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == task.entity_id,
                AgentSubscription.workspace_id == task.workspace_id,
                AgentSubscription.status == "active",
            )
        )).scalars().all())

    # Allowlist of service_keys = task.owner_service_key + delegates.
    # Falls back to "all subscriptions in the workspace" if the task
    # didn't pin an owner (legacy tasks during the migration window).
    explicit = set(task.delegate_service_keys or [])
    if task.owner_service_key:
        explicit.add(task.owner_service_key)
    allowed_service_keys = explicit if explicit else {
        s.service_key for s in subs if s.service_key
    }
    subs = [
        s for s in subs
        if getattr(s, "service_key", None) in allowed_service_keys
    ]

    agent_ids = [s.agent_id for s in subs if s.agent_id]
    agents_by_id: dict[str, Agent] = {}
    if agent_ids:
        agent_rows = list((await db.execute(
            select(Agent).where(
                Agent.id.in_(agent_ids),
                or_(Agent.entity_id == task.entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all())
        agents_by_id = {a.id: a for a in agent_rows}

    # Provider/action map — derived from agent_mcp_bindings + mcp_servers.
    # Keep both a per-service scope for routing correctness and a union for
    # backward-compatible planner prompt summaries.
    service_provider_actions, service_provider_action_specs = await _compute_service_provider_actions(
        db,
        subscriptions=subs,
        agents_by_id=agents_by_id,
    )
    provider_actions = _union_provider_actions(service_provider_actions)
    provider_action_specs = _union_provider_action_specs(service_provider_action_specs)

    # Workspace-scoped documents + staff — so Planner knows what's available
    doc_groups: list[dict] = []
    staff_list: list[dict] = []
    if task.workspace_id:
        from packages.core.models.document import DocumentGroup
        from packages.core.models.workspace import WorkspaceStaff
        doc_rows = list((await db.execute(
            select(DocumentGroup).where(DocumentGroup.workspace_id == task.workspace_id)
        )).scalars().all())
        doc_groups = [{"id": d.id, "name": d.name} for d in doc_rows]

        staff_rows = list((await db.execute(
            select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == task.workspace_id)
        )).scalars().all())
        staff_list = [{"staff_id": s.staff_id, "role": s.role} for s in staff_rows]

    # Per-agent platform tool bindings + skill bindings — so the Planner
    # knows each agent's capabilities beyond MCP actions (e.g. write_file,
    # generate_document_file, invoke_skill, web_search).
    agent_tool_names: dict[str, list[str]] = {}
    agent_skill_names: dict[str, list[dict]] = {}
    if agent_ids:
        from packages.core.models.workspace import AgentToolBinding, ToolDefinition
        from packages.core.models.skill import AgentSkillBinding, Skill

        # Tool bindings — join to ToolDefinition to get human-readable names
        tool_binding_rows = list((await db.execute(
            select(AgentToolBinding.agent_id, ToolDefinition.name).join(
                ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id,
            ).where(AgentToolBinding.agent_id.in_(agent_ids))
        )).all())
        for agent_id_val, tool_name in tool_binding_rows:
            agent_tool_names.setdefault(agent_id_val, []).append(tool_name)

        # Skill bindings
        skill_binding_rows = list((await db.execute(
            select(AgentSkillBinding).where(AgentSkillBinding.agent_id.in_(agent_ids))
        )).scalars().all())
        skill_ids = [sb.skill_id for sb in skill_binding_rows]
        skill_map: dict[str, Skill] = {}
        if skill_ids:
            skill_rows = list((await db.execute(
                select(Skill).where(Skill.id.in_(skill_ids))
            )).scalars().all())
            skill_map = {s.id: s for s in skill_rows}
        for sb in skill_binding_rows:
            sk = skill_map.get(sb.skill_id)
            if sk:
                agent_skill_names.setdefault(sb.agent_id, []).append({
                    "slug": sk.slug or sk.name,
                    "name": sk.name,
                    "description": (sk.description or "")[:150],
                })

    return _Context(
        workspace=workspace,
        subscriptions=subs,
        agents_by_id=agents_by_id,
        allowed_service_keys=allowed_service_keys,
        provider_actions=provider_actions,
        provider_action_specs=provider_action_specs,
        service_provider_actions=service_provider_actions,
        service_provider_action_specs=service_provider_action_specs,
        document_groups=doc_groups,
        staff=staff_list,
        agent_tool_names=agent_tool_names,
        agent_skill_names=agent_skill_names,
    )


async def _compute_service_provider_actions(
    db: AsyncSession,
    *,
    subscriptions: list[AgentSubscription],
    agents_by_id: dict[str, Agent],
) -> tuple[
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
    """Derive {service_key: {provider_key: [allowed_action_keys, ...]}}.

    Reads agent_mcp_bindings + mcp_servers.tools_cached. Empty when no
    bindings exist — Planner gets an empty action menu for that service and
    produces a pure-LLM/subagent plan, which is the right behaviour."""
    if not agents_by_id or not subscriptions:
        return {}, {}

    service_keys_by_agent: dict[str, set[str]] = {}
    for subscription in subscriptions:
        agent_id = getattr(subscription, "agent_id", None)
        service_key = str(getattr(subscription, "service_key", "") or "").strip()
        if agent_id in agents_by_id and service_key:
            service_keys_by_agent.setdefault(agent_id, set()).add(service_key)
    if not service_keys_by_agent:
        return {}, {}

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    bindings = list((await db.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id.in_(list(service_keys_by_agent.keys())),
            AgentMCPBinding.status == "active",
        )
    )).scalars().all())
    if not bindings:
        return {}, {}

    server_ids = {b.mcp_server_id for b in bindings}
    servers = {
        s.id: s for s in (await db.execute(
            select(MCPServer).where(
                MCPServer.id.in_(server_ids),
                MCPServer.status == "active",
            )
        )).scalars().all()
    }

    out: dict[str, dict[str, list[str]]] = {}
    spec_out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for b in bindings:
        srv = servers.get(b.mcp_server_id)
        if not srv:
            continue
        tool_specs = runtime_planner_action_specs_from_tools_cached(srv.tools_cached or [])
        all_tool_names = list(tool_specs)
        configured_allowed = b.allowed_tools if b.allowed_tools is not None else srv.default_allowed_tools
        if configured_allowed is not None:
            allowed_set = {str(n) for n in configured_allowed if str(n or "").strip()}
            allowed = [n for n in all_tool_names if n in allowed_set or f"mcp__{srv.server_key}__{n}" in allowed_set]
        else:
            allowed = all_tool_names
        if not allowed:
            continue
        for service_key in service_keys_by_agent.get(b.agent_id, set()):
            service_actions = out.setdefault(service_key, {}).setdefault(srv.server_key, [])
            service_specs = spec_out.setdefault(service_key, {}).setdefault(srv.server_key, {})
            for n in allowed:
                if n not in service_actions:
                    service_actions.append(n)
                service_specs.setdefault(n, tool_specs.get(n, {}))
    from packages.core.services.agent_permission_service import resolve_agent_direct_mcp_actions

    direct_by_provider: dict[str, set[str]] = {}
    for agent_id in service_keys_by_agent:
        direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
        for provider, actions in direct_actions.items():
            direct_by_provider.setdefault(provider, set()).update(actions)
    if direct_by_provider:
        direct_servers = {
            s.server_key: s for s in (await db.execute(
                select(MCPServer).where(
                    MCPServer.server_key.in_(list(direct_by_provider)),
                    MCPServer.status == "active",
                )
            )).scalars().all()
        }
        for agent_id, service_keys in service_keys_by_agent.items():
            direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
            for provider, actions in direct_actions.items():
                srv = direct_servers.get(provider)
                if not srv:
                    continue
                tool_specs = runtime_planner_action_specs_from_tools_cached(srv.tools_cached or [])
                allowed = [action for action in sorted(actions) if action in tool_specs]
                if not allowed:
                    continue
                for service_key in service_keys:
                    service_actions = out.setdefault(service_key, {}).setdefault(provider, [])
                    service_specs = spec_out.setdefault(service_key, {}).setdefault(provider, {})
                    for action in allowed:
                        if action not in service_actions:
                            service_actions.append(action)
                        service_specs.setdefault(action, tool_specs.get(action, {}))
    return out, spec_out


def _union_provider_actions(
    service_provider_actions: dict[str, dict[str, list[str]]],
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for provider_actions in service_provider_actions.values():
        for provider, actions in provider_actions.items():
            bucket = out.setdefault(provider, [])
            for action in actions:
                if action not in bucket:
                    bucket.append(action)
    return out


def _union_provider_action_specs(
    service_provider_action_specs: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for provider_specs in service_provider_action_specs.values():
        for provider, action_specs in provider_specs.items():
            bucket = out.setdefault(provider, {})
            for action, spec in action_specs.items():
                bucket.setdefault(action, spec)
    return out


# ── LLM call + validation ─────────────────────────────────────────────

async def _generate_plan(task: Task, ctx: _Context) -> Plan:
    """Multi-turn agent loop — the Planner researches context and tools
    before committing to a plan. Up to MAX_PLANNER_TURNS turns.

    Available tools:
      list_tools(service_key)   — see what actions an agent can perform
      get_tool_schema(provider, action_key) — input/output schema for a tool
      submit_plan(plan_json)    — finalize and validate the plan DAG
    """
    MAX_PLANNER_TURNS = 8

    system_prompt = runtime_planner_system_prompt(
        subscriptions=ctx.subscriptions,
        agents_by_id=ctx.agents_by_id,
        allowed_service_keys=ctx.allowed_service_keys,
        provider_actions=ctx.provider_actions,
        provider_action_specs=ctx.provider_action_specs,
        document_groups=ctx.document_groups,
        staff=ctx.staff,
        agent_tool_names=ctx.agent_tool_names,
        agent_skill_names=ctx.agent_skill_names,
    )
    user_prompt = runtime_planner_task_prompt(task)
    tools = runtime_planner_tool_schemas()

    messages: list[dict[str, Any]] = [runtime_planner_user_message(user_prompt)]

    submitted_plan: Optional[Plan] = None

    for turn in range(MAX_PLANNER_TURNS):
        try:
            response = await runtime_execute_planner_chat_turn(
                messages=messages,
                tools=tools,
                system_prompt=system_prompt,
                entity_id=getattr(task, "entity_id", None),
                workspace_id=getattr(task, "workspace_id", None),
            )
        except Exception as exc:
            logger.warning("Planner LLM call failed on turn %d: %s", turn, exc)
            if turn == 0:
                logger.warning("Planner: LLM unavailable, using fallback stub")
                return _fallback_plan(task, ctx)
            break

        # Handle tool calls
        if response.tool_calls:
            messages.append(runtime_planner_assistant_message(response))
            for tc in response.tool_calls:
                tool_name = tc.get("name") or (tc.get("function") or {}).get("name", "")
                raw_args = tc.get("arguments") or (tc.get("function") or {}).get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, TypeError):
                    args = {}

                result = runtime_execute_planner_tool_call(
                    tool_name,
                    args,
                    context=ctx,
                    parse_plan=_parse_plan,
                    enforce_plan=lambda plan: _enforce_allowlists(plan, ctx),
                )

                # Check if submit_plan returned a valid plan
                if tool_name == "submit_plan" and isinstance(result, dict) and result.get("_plan"):
                    submitted_plan = _normalize_plan_for_task(task, result["_plan"])
                    result_text = result.get("message", "Plan accepted.")
                else:
                    result_text = json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)

                messages.append(runtime_planner_tool_message(
                    result_text,
                    tool_call_id=tc.get("id"),
                ))

            if submitted_plan:
                return submitted_plan
            continue

        # No tool calls — try to parse as direct plan JSON (fallback for simple tasks)
        if response.content:
            plan = _parse_plan(response.content)
            if plan:
                return _normalize_plan_for_task(task, plan)
            # Ask to use submit_plan tool
            messages.append(runtime_planner_assistant_message(response))
            messages.append(runtime_planner_user_message(
                "Please use the submit_plan tool to submit your plan as valid JSON."
            ))
            continue

        break

    raise PlannerError(f"Planner failed to produce a valid plan after {MAX_PLANNER_TURNS} turns")


_TEXT_REPORT_DELIVERABLE_TERMS = (
    "structured text report",
    "text report",
    "plain text report",
    "internal memo",
    "plain text",
    "text-only",
    "text only",
    "文字报告",
    "纯文本",
)
_EXPLICIT_SAVED_ARTIFACT_TERMS = (
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md",
    "saved file", "file link", "file path", "report file", "text file",
    "document file", "markdown file", "as a file", "download", "attachment",
    "save as", "export as",
    "报告文件", "文件链接", "文件路径", "附件", "下载", "保存为", "导出为",
)


def _normalize_plan_for_task(task: Task, plan: Plan) -> Plan:
    """Normalize planner overreach before materializing executable steps."""
    plan = _normalize_planner_hard_approval_steps(plan)
    if not _task_requests_text_report_only(task):
        return plan

    depended_on = {dep for step in plan.steps for dep in step.depends_on}
    removable_keys = {
        step.key
        for step in plan.steps
        if step.key not in depended_on
        and _is_unrequested_text_report_file_write_step(step)
    }
    if not removable_keys or len(removable_keys) >= len(plan.steps):
        return plan

    normalized = Plan(
        steps=[step for step in plan.steps if step.key not in removable_keys],
        metadata=plan.metadata,
    )
    try:
        normalized.metadata.normalized_removed_steps = sorted(removable_keys)
        normalized.metadata.normalization_reason = "unrequested_text_report_file_write"
    except Exception:
        pass
    return normalized


def _normalize_planner_hard_approval_steps(plan: Plan) -> Plan:
    """Keep approval authority in workspace policy, not planner guesses.

    Plans produced here come from the LLM planner. The planner may decide what
    work should happen, but it must not create approval rules by setting
    ``requires_approval``. Runtime approvals, always-allow, and denies are
    evaluated later by workspace/task governance policy in the dispatcher.
    """
    changed: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if step.requires_approval:
            steps.append(step.model_copy(update={"requires_approval": False}))
            changed.append(step.key)
        else:
            steps.append(step)
    if not changed:
        return plan

    normalized = Plan(steps=steps, metadata=plan.metadata)
    try:
        existing = list(getattr(normalized.metadata, "normalized_removed_step_approvals", []) or [])
        normalized.metadata.normalized_removed_step_approvals = existing + changed
        normalized.metadata.normalization_reason = "planner_hard_approval_policy_owned"
    except Exception:
        pass
    return normalized


def _task_requests_text_report_only(task: Task) -> bool:
    title = str(getattr(task, "title", "") or "")
    description = str(getattr(task, "description", "") or "")
    expected_output = getattr(task, "expected_output", None)
    details = getattr(task, "details", None)
    text = "\n".join([
        title,
        description,
        json.dumps(expected_output, ensure_ascii=False, default=str) if expected_output else "",
        json.dumps(details, ensure_ascii=False, default=str) if details else "",
    ]).lower()
    return (
        any(term in text for term in _TEXT_REPORT_DELIVERABLE_TERMS)
        and not any(term in text for term in _EXPLICIT_SAVED_ARTIFACT_TERMS)
    )


def _is_unrequested_text_report_file_write_step(step: PlanStep) -> bool:
    prompt = json.dumps(step.params or {}, ensure_ascii=False, default=str).lower()
    return (
        step.capability_id == "file.write"
        or step.requires_approval and "generate_file" in prompt
        or "file_url" in prompt
        or "fs_path" in prompt
    ) and (
        "generate_file" in prompt
        or "save the file" in prompt
        or "file_url" in prompt
        or "fs_path" in prompt
        or "保存" in prompt
    )


def _parse_plan(text: str) -> Optional[Plan]:
    """Try to extract + validate a Plan from raw LLM text. Tolerates
    fenced code blocks (```json … ```) and stray prose."""
    if not text:
        return None
    candidate = _strip_code_fence(text).strip()
    try:
        return Plan.model_validate_json(candidate)
    except ValidationError as exc:
        logger.debug("Planner Plan validation failed: %s", exc)
        return None
    except ValueError:
        # Not JSON at all.
        return None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # ```json or ```\n
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# ── Allowlist enforcement ─────────────────────────────────────────────

def _ctx_provider_actions_for_step(ctx: _Context, service_key: str | None) -> dict[str, list[str]]:
    if ctx.service_provider_actions:
        return dict(ctx.service_provider_actions.get(str(service_key or ""), {}) or {})
    return ctx.provider_actions


def _ctx_provider_action_specs_for_step(
    ctx: _Context,
    service_key: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    if ctx.service_provider_action_specs:
        return dict(ctx.service_provider_action_specs.get(str(service_key or ""), {}) or {})
    return ctx.provider_action_specs


def _enforce_allowlists(plan: Plan, ctx: _Context) -> None:
    for s in plan.steps:
        if s.service_key and s.service_key not in ctx.allowed_service_keys:
            raise CapabilityError(
                f"step {s.key}: service_key={s.service_key!r} is not in "
                f"the workspace's allowlist {sorted(ctx.allowed_service_keys)}"
            )
        if s.kind == "action":
            provider_actions = _ctx_provider_actions_for_step(ctx, s.service_key)
            provider_action_specs = _ctx_provider_action_specs_for_step(ctx, s.service_key)
            runtime_apply_action_binding_schemas_to_steps(
                [s],
                provider_actions=provider_actions,
                provider_action_specs=provider_action_specs,
            )
            available = provider_actions.get(s.provider or "")
            if available is None:
                raise CapabilityError(
                    f"step {s.key}: provider={s.provider!r} not available "
                    f"for service_key={s.service_key!r}"
                )
            if s.action_key not in available:
                raise CapabilityError(
                    f"step {s.key}: action {s.action_key!r} not in "
                    f"allowed actions for service_key={s.service_key!r} "
                    f"provider {s.provider}: {available}"
                )
            inferred_capability_id = runtime_capability_id_for_action_key(
                s.action_key,
                provider=s.provider,
            )
            if inferred_capability_id and s.capability_id != inferred_capability_id:
                raise CapabilityError(
                    f"step {s.key}: capability_id={s.capability_id!r} does not "
                    f"match provider/action capability {inferred_capability_id!r}"
                )


# ── Fallback for dev / CI without LLM ─────────────────────────────────

def _fallback_plan(task: Task, ctx: _Context) -> Plan:
    """Two-step generic plan: think, then summarise.

    Used when AIEngine.chat raises (no API key, network down). Lets
    Demo A v0 smoke tests run end-to-end without hitting the LLM.
    """
    primary_service = task.owner_service_key or next(
        iter(ctx.allowed_service_keys), None
    )
    if not primary_service:
        # No services at all — emit a single human step so the user
        # sees something instead of nothing.
        return Plan(steps=[
            PlanStep(
                key="manual_only",
                kind="human",
                params={"prompt": f"No services configured to plan task: {task.title}"},
                description="Manual fallback (no service_key available).",
            )
        ])
    task_prompt = runtime_planner_task_prompt(task)
    return Plan(steps=[
        PlanStep(
            key="think",
            kind="llm",
            service_key=primary_service,
            params={"prompt": f"Think out loud about how to do this task:\n\n{task_prompt}"},
            description="Reason about the task.",
        ),
        PlanStep(
            key="summarize",
            kind="llm",
            service_key=primary_service,
            params={"prompt": "Summarise the conclusion and any runtime requirements used in 3 bullet points: ${{ steps.think.result.text }}"},
            depends_on=["think"],
            description="Summarise into a concrete plan.",
        ),
    ])
