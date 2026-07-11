"""Workspace Architect — typed-tool orchestrator that builds a workspace
draft via incremental tool calls instead of single-shot JSON.

This replaces the legacy ``workspace_setup_service.process_setup_turn``
flow (which asked the LLM to emit a full ``<workspace_setup>JSON</...>``
status block on every turn -- prone to missing fields and hallucinated
agent ids). Each construction step is now a function call with a JSON
Schema that the model-provider enforces:

  * ``ws_commit_basics``           name + kind + context + primary_work
  * ``ws_propose_service``         service decomposition (2-5x)
  * ``ws_propose_goal``            target/cadence required, no defaults
  * ``ws_propose_agent_mapping``   ULID-checked + entity-scoped
  * ``ws_request_custom_agent``    fallback when no entity agent fits
  * ``ws_propose_channel``         primary external / internal / etc.
  * ``ws_propose_rule``            policy / escalation
  * ``ws_propose_automation``      schedules
  * ``ws_set_evaluation``          scorecard
  * ``ws_set_budget``              optional monthly credit cap
  * ``ws_search_entity_agents``    (read) get real agent ids to bind
  * ``ws_search_blueprints``       (read) marketplace lookup
  * ``ws_get_draft``               (read) recover state
  * ``ws_lint_draft``              self-check
  * ``ws_mark_ready``              flip ready=true after lint passes

The two entry points -- ``architect_run_turn`` (used by the live chat
draft) and ``architect_invoke_via_skill`` (registered Skill the master
agent can call from any chat) -- both end up running the same Runtime
Harness loop adapter with the same tools, so behaviour stays identical
across surfaces.
"""
from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Awaitable, Callable, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    ChatSurface,
    runtime_execute_workspace_architect_loop,
    runtime_prepare_local_tool_surface_for_turn,
    runtime_request_for_surface_turn,
    runtime_wrap_tool_executor,
    runtime_workspace_architect_tool_executor,
    runtime_workspace_architect_tool_schemas,
)

logger = logging.getLogger(__name__)


_USAGE_SOURCE = "workspace_architect"


# ---------------------------------------------------------------------------
# System prompt -- the brain of the architect
# ---------------------------------------------------------------------------

ARCHITECT_SYSTEM_PROMPT = """\
## Workspace Architect

You are Manor's Workspace Architect. You build a structured workspace
configuration by calling typed tools. You **never** output JSON in your
visible reply -- the structure lives in tool calls, the visible reply
is plain conversational text for the user.

The user is creating a workspace -- a semi-autonomous unit (a property,
project, support desk, campaign, client account, etc.) with its own
agents, channels, goals, and automations. You help them describe their
intent, then commit each part of the configuration to the draft using
function calls.

═══════════════════════════════════════════════════════════════════════
HARD RULES
═══════════════════════════════════════════════════════════════════════

1. Pass ``draft_id`` to **every** tool call. The current draft id is
   given to you at the start of every turn inside a
   ``<draft_id>...</draft_id>`` block. Read it once and reuse.
2. Never invent agent ids. Always call ``ws_search_entity_agents``
   first to get the real list, then use those exact ids in
   ``ws_propose_agent_mapping``. If no good match exists, call
   ``ws_request_custom_agent`` instead.
3. Goals must always have ``target`` and ``cadence``. If the user did
   not say a number, infer a reasonable one and set
   ``rationale="inferred"`` -- do NOT skip the field.
4. Service / goal / rule / automation keys are snake_case
   (``content_creation``, ``follower_growth``).
5. Do NOT claim the workspace is created. You are drafting -- the user
   clicks "Create Workspace" later. Never say "deployed" / "live" /
   "operational".
6. Do NOT ask the user to author rules / automations / scorecards
   field-by-field. Infer reasonable defaults from what they've said.
7. When ``ws_lint_draft`` returns no P0 issues, call ``ws_mark_ready``
   and tell the user "The draft is ready -- click **Create Workspace**
   to review and create."
8. Budget is always described to users in credits, not USD. If the user
   mentions budget, spend, cap, limits, or credits, call
   ``ws_set_budget`` with ``monthly_budget_credits``. If they do not
   mention a budget, leave it uncapped; the draft UI lets them fill it.
9. **MCP server preference.** When binding ``mcp_bindings`` for a
   custom agent, ALWAYS prefer the per-platform MCP server over the
   generic ``nango`` aggregator. Examples:
   - For Twitter posting → bind ``twitter_x`` (NOT ``nango`` with
     ``nango_proxy``).
   - For Slack messaging → bind ``slack`` (NOT ``nango``).
   - For Linear / Notion / GitHub → bind ``linear`` / ``notion`` /
     ``github`` directly.
   Only bind the ``nango`` server when (a) the platform has no
   first-class MCP entry in ``ws_search_capabilities.integrations``
   AND (b) the entity has authorized that provider via Nango (visible
   under ``providers_connected`` in the nango integration row).
   Per-platform servers expose typed tools that agents can call directly.
   Binding the server is enough; tool-level allowlists are only needed for
   deliberately narrowed scopes. ``nango_proxy`` is a generic HTTP passthrough
   and should be a last resort.

═══════════════════════════════════════════════════════════════════════
TURN-BY-TURN FLOW
═══════════════════════════════════════════════════════════════════════

Opening turn (user message is "begin" or empty):
  - One short sentence: "A workspace keeps the work, context, agents,
    and rules in one place."
  - One open question: "What should this workspace run?"
  - No tool calls yet.

Discovery turns (you don't yet have name + kind + operating_context +
primary_work):
  - Ask the next single most important question. Don't interrogate.
  - You MAY call ``ws_search_blueprints`` if the user's intent
    obviously matches a marketplace category -- if you find a strong
    match, mention it and ask "want to start from this template?".

Once you have name + kind + operating_context + primary_work, do
ALL of the following in this turn before replying to the user:

  STEP A. ``ws_commit_basics`` (one call)
  STEP B. Decompose into 2-5 services with ``ws_propose_service``,
          one call per service. Always set both ``service_key`` and
          ``name`` (Title Case).
  STEP C. ``ws_search_entity_agents`` once to load real agent ids.
          ``ws_search_capabilities`` once to load the available tools,
          skills, and integrations (with their backing MCP server keys).
  STEP D. For each service, decide:
            (a) best entity-agent match → ``ws_propose_agent_mapping``,
                or
            (b) custom agent → ``ws_request_custom_agent`` with a FULL
                spec (not just a name + seed prompt):
                  - ``system_prompt`` -- 5-10 sentences. CRITICAL: the
                    Agent you're describing is a **general worker**
                    owned by the entity and reusable across workspaces;
                    the workspace's primary_work / operating_context
                    is layered on later via the subscription's
                    custom_prompt. So write the system_prompt to
                    describe the agent's CAPABILITY (e.g. "an expert
                    Twitter content creator who writes hooks, threads,
                    and replies"), NOT a specific workspace ("the
                    Twitter Growth workspace's content agent"). Mention
                    the agent's name, list its core skills, end with a
                    scope guard ("Stay in this capability; defer
                    cross-capability requests").
                  - ``tool_bindings`` -- pick from
                    ws_search_capabilities.tools that the service truly
                    needs (don't over-bind; no shotgun).
                  - ``business_capabilities`` -- for workspace operation
                    scope changes, prefer these ids over raw tool names
                    so runtime can expand and audit the capability.
                  - ``skill_bindings`` -- if an existing skill matches a
                    sub-task (e.g. "twitter_post"), bind it.
                  - ``mcp_bindings`` -- only servers whose
                    ``active_integration`` is true.
                  - ``missing_skill_specs`` -- if the service needs a
                    capability NOT covered by any existing tool/skill,
                    request a brand-new skill (the platform creates it
                    + binds it for you). Each spec needs name +
                    system_prompt.
                  - ``missing_integrations`` -- if the service needs an
                    integration the entity hasn't set up yet, list it
                    here. The agent is still created; the workspace
                    surfaces a "needs setup" warning.
          IMPORTANT: a custom agent without any tool / skill / mcp
          binding cannot do real work. Always bind something.
  STEP E. Extract goals -- ``ws_propose_goal``, one per measurable
          target the user mentioned. Always supply target + cadence.
  STEP F. Pick a primary external channel based on what the user
          described (``ws_propose_channel`` role=primary_external),
          plus an internal channel
          (``ws_propose_channel`` role=internal channel_type=internal_chat).
  STEP G. ASSIGN STAFF -- ``ws_search_capabilities`` exposes the
          entity's staff roster. For workspaces where humans are
          clearly involved (review queues, escalations, content
          approval), pick 1-3 plausible staff members and call
          ``ws_assign_staff(staff_id, role, service_key?)`` per pick.
          Use sensible roles like ``owner``, ``editor``, ``reviewer``,
          ``analyst``. If the roster is empty or the workspace is
          fully autonomous, skip staff entirely -- don't invent ids.
  STEP H. ATTACH KNOWLEDGE -- for any service that benefits from
          retrievable context (brand voice, playbooks, SOPs, FAQ,
          past examples), call ``ws_attach_knowledge`` to spin up a
          Knowledge Net (DocumentGroup) the operator can drop documents into
          or seed with a starter doc. One call per logical network (don't create
          a group per service if they share content). Pick mode='create_new'
          unless a template group from ws_search_capabilities.knowledge
          clearly fits, in which case mode='clone_template'. Use
          generate_starter_doc=true when the workspace needs a fresh
          generated markdown doc in addition to any template files.
  STEP I. If the user described policies → ``ws_propose_rule``;
          schedules → ``ws_propose_automation``. Skip if absent.
          For external-action guardrails, set enforceable fields:
          - "approval before posting/sending" →
            rule_type="approval_required",
            action_patterns=["social_post.publish"] / ["email.send"] /
            ["external_message.send"] as appropriate.
          - "draft only" / "never publish/send/delete" →
            rule_type="draft_only" or "deny" with matching action_patterns.
          - "do not modify workspace files; only add" →
            rule_type="deny",
            action_patterns=["workspace.file.modify", "workspace.file.delete", "workspace.file.write"].
          Keep conditional policies (for example, "only on Sundays") in the
          description even if they cannot be fully encoded as action patterns.
  STEP J. If at least one goal exists → ``ws_set_evaluation`` with a
          scorecard mapping each goal to its metric.
  STEP K. If the user gave a monthly credit budget or asked to control
          spend → ``ws_set_budget``. Use credits. Do not invent a cap
          from nothing.
  STEP L. ``ws_lint_draft`` and inspect issues.
          - If P0 issues exist, fix them via more tool calls then
            re-lint. Do not give up.
          - When P0=0, ``ws_mark_ready``.
  STEP M. Reply to the user with: a one-paragraph summary of what
          you drafted (services + agents + key goals + staff assigned
          + knowledge buckets created) and the "click Create
          Workspace" line.

Subsequent turns (user wants to tweak after draft is ready):
  IMPORTANT: Do NOT regenerate the whole draft. Only change what the
  user asked for. The draft already has services, agents, goals, channels,
  knowledge — leave everything the user didn't mention UNTOUCHED.

  - ``ws_propose_*`` to add or replace a single item
  - ``ws_remove`` to delete a single item, including
    kind=integration when the user wants a provider removed from the
    "needs setup" warnings
  - Re-run ``ws_lint_draft`` + ``ws_mark_ready`` after the change
  - Reply with ONE short sentence describing what you changed

  NEVER re-run Steps A through L on a subsequent turn. Only call the
  specific tools needed for the user's requested change.

═══════════════════════════════════════════════════════════════════════
QUALITY BAR
═══════════════════════════════════════════════════════════════════════

* Every service has a real ``name`` (not just service_key).
* Every service has an agent_mapping (real or custom).
* Every goal has target + cadence.
* primary_external_channel has channel_type and purpose.
* The visible reply is short, plain, and human -- no JSON, no XML,
  no enumerated tool names.
"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

ToolEventCallback = Callable[[str, dict], Awaitable[None] | None]


async def architect_run_turn(
    db: AsyncSession,
    *,
    draft_id: str,
    entity_id: str,
    user_id: Optional[str],
    user_message: str,
    history: Optional[List[dict]] = None,
    stream_handler: Optional[Any] = None,
    on_tool_start: Optional[Callable[[str, dict], Any]] = None,
    on_tool_end: Optional[Callable[[str, str], Any]] = None,
) -> str:
    """Run one architect turn for the given draft.

    Mutations land directly on the draft row via the typed tools, so
    the caller just needs to commit the session afterwards. Returns the
    visible (non-tool) text the LLM produced for the user.

    ``history`` -- prior visible turns ([{role,content}, ...]) so the
    architect remembers the conversation. Hidden tool round-trips are
    NOT included; they live in agentic_loop's internal state.
    """
    initial_messages: List[dict] = []
    if history:
        for m in history:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and content:
                initial_messages.append({"role": role, "content": str(content)})

    # Inject current draft state so the LLM knows what exists
    # before making targeted edits. Without this, it would have to
    # call ws_lint_draft just to see the current configuration.
    draft_snapshot = ""
    try:
        from packages.core.services.workspace_draft_service import get_draft

        _draft = await get_draft(db, draft_id, entity_id)
        if _draft and _draft.fields:
            f = _draft.fields
            summary_parts = []
            if f.get("name"):
                summary_parts.append(f"name: {f['name']}")
            if f.get("kind"):
                summary_parts.append(f"kind: {f['kind']}")
            services = f.get("services") or []
            if services:
                svc_keys = [s.get("service_key", "?") for s in services]
                summary_parts.append(f"services: {', '.join(svc_keys)}")
            mappings = f.get("agent_mappings") or []
            if mappings:
                map_strs = [f"{m.get('service_key','?')}→{m.get('agent_name') or m.get('recommended_agent_name','?')}" for m in mappings]
                summary_parts.append(f"agents: {', '.join(map_strs)}")
            goals = f.get("goals") or []
            if goals:
                goal_strs = [g.get("title") or g.get("goal_key", "?") for g in goals]
                summary_parts.append(f"goals: {', '.join(goal_strs)}")
            channels = (f.get("channel_config") or {}).get("channels") or []
            if channels:
                ch_strs = [c.get("channel_type", "?") for c in channels]
                summary_parts.append(f"channels: {', '.join(ch_strs)}")
            knowledge = f.get("knowledge_attachments") or []
            if knowledge:
                kb_strs = [k.get("name", "?") for k in knowledge]
                summary_parts.append(f"knowledge: {', '.join(kb_strs)}")
            if summary_parts:
                draft_snapshot = "<current_draft>\n" + "\n".join(summary_parts) + "\n</current_draft>\n"
    except Exception:
        pass

    framed_user_message = (
        f"<draft_id>{draft_id}</draft_id>\n"
        f"<entity_id>{entity_id}</entity_id>\n"
        "Pass the draft_id above to every tool call.\n\n"
        f"{draft_snapshot}"
        f"{user_message}"
    )

    runtime_request = runtime_request_for_surface_turn(
        surface=ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=None,
        message=user_message,
        legacy_path="services.workspace_architect",
    )
    runtime_surface_result = runtime_prepare_local_tool_surface_for_turn(
        runtime_request,
        tool_schemas=runtime_workspace_architect_tool_schemas(),
    )
    runtime_executor = runtime_wrap_tool_executor(
        runtime_surface_result.envelope,
        runtime_workspace_architect_tool_executor(
            db,
            draft_id=draft_id,
            entity_id=entity_id,
            user_id=user_id,
        ),
    )
    schemas_by_name = {
        str(schema.get("function", {}).get("name") or ""): schema
        for schema in runtime_surface_result.tool_schemas
        if isinstance(schema, dict)
    }

    started = time.monotonic()
    result = await runtime_execute_workspace_architect_loop(
        runtime_envelope=runtime_surface_result.envelope,
        system_prompt=ARCHITECT_SYSTEM_PROMPT,
        user_message=framed_user_message,
        tools=runtime_surface_result.tool_schemas,
        entity_id=entity_id,
        agent_id=None,
        user_id=user_id,
        active_user_message=user_message,
        allowed_tool_names=runtime_surface_result.allowed_tool_names,
        tool_executor=runtime_executor,
        tool_schema_resolver=lambda name: schemas_by_name.get(str(name or "")),
        max_rounds=40,
        initial_messages=initial_messages,
        stream_handler=stream_handler,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    # Token usage is auto-recorded per LLM call via the runtime billing context.
    # We only need the aggregated totals here for the stream meta event so the
    # frontend can display token count + latency next to the construction log.
    usage = result.usage or {}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("prompt") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("completion") or 0)
    total_tokens = int(usage.get("total_tokens") or usage.get("total") or (prompt_tokens + completion_tokens))
    model_name = str(usage.get("model") or "")

    if stream_handler is not None:
        meta_payload = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "duration_ms": duration_ms,
            "rounds": result.rounds,
            "tool_calls": len(result.tool_calls_made or []),
            "model": model_name,
        }
        try:
            maybe = stream_handler("turn_meta", meta_payload)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            pass

    return result.content or ""


# ---------------------------------------------------------------------------
# Skill-style invocation -- lets a master agent call the architect from
# any chat via ``invoke_skill('workspace_architect', input_text)``.
# ---------------------------------------------------------------------------

WORKSPACE_ARCHITECT_SKILL_SLUG = "workspace_architect"


async def architect_invoke_via_skill(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: Optional[str],
    input_text: str,
) -> dict:
    """Skill-shaped wrapper.

    The chat agent invokes us via ``invoke_skill('workspace_architect',
    '<draft_id>X</draft_id> user message...')``. We parse the framing
    out of input_text, then delegate to ``architect_run_turn``.
    """
    import re
    m = re.search(r"<draft_id>([A-Z0-9]{26})</draft_id>", input_text)
    if not m:
        return {"error": "input_text missing <draft_id>...</draft_id> framing"}
    draft_id = m.group(1)
    user_msg = re.sub(r"<draft_id>[A-Z0-9]{26}</draft_id>\s*", "", input_text).strip()

    content = await architect_run_turn(
        db,
        draft_id=draft_id,
        entity_id=entity_id,
        user_id=user_id,
        user_message=user_msg,
    )
    return {"content": content, "draft_id": draft_id}
