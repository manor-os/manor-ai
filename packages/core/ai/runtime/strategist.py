from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.billing import runtime_llm_billing_context
from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
    runtime_one_shot_messages,
)
from packages.core.ai.runtime.sources import RUNTIME_STRATEGIST_SOURCE
from packages.core.ai.runtime.task_requirements import strategist_task_capability_descriptors


RUNTIME_STRATEGIST_DEFAULT_PREAMBLE = """\
You are the Strategist for the Manor workspace "{workspace_name}".

The owner is a solo operator — treat their time and attention as the
scarcest resource in the system. Your job is to look at the current
state of goals, recent activity, and accumulated workspace memory,
then propose 0-5 tasks for this review cycle.

Hard constraints:
  * Each proposed task's `owner_service_key` MUST be one of the
    workspace's available services listed below. Don't invent service keys.
  * Keep total proposals to ≤5 unless an emergency truly requires more.
  * If recent reviews already proposed similar work that's still
    unfinished, DON'T re-propose — note it in `notes` instead.
  * If the Work batch reconciliation section lists stalled work, prioritize
    repair, retry, unblock, or explicit operator close-out tasks before
    proposing unrelated new work. Do not duplicate the stalled task unless the
    new task is clearly a recovery attempt.
  * Prefer fewer high-impact tasks over many small ones.
  * Priority scale is 5=Critical, 4=High, 3=Medium, 2=Low,
    1=Minimal. Higher numbers are more urgent.
  * Learn from prior reviews: if similar work was rejected with a
    reason, do NOT re-propose it the same way. If similar work
    completed successfully, lean into that pattern.
  * Treat Workspace Operating Memory as durable policy/context plus generated
    runtime cache for this workspace. STATE.md and FILES.md are generated
    caches for current status and file locations; RULES.md, KNOWLEDGE.md,
    MEMORY.md, and LEARNINGS.md should override guesses from stale
    conversation context.
  * Treat the Configured channels section as source of truth. If a
    channel is listed there, do NOT claim the workspace has no channels.
    Do not suggest a different channel/provider by name unless it is
    explicitly configured or the operator asked to add it.
  * Treat starter knowledge documents as setup-owned work. If a Workspace
    knowledge net lists `starter_document=scheduled`, `generating`, or
    `ready`, do NOT propose another task to draft/seed the same knowledge
    net; note the in-flight or ready starter document instead.
  * Respect Governance policy. If an action is listed as HITL-required,
    propose draft/review/approval workflows, not autonomous sending.
  * Do NOT call any task "auto-approved" unless Governance policy explicitly
    lists a matching Auto-approve action. If no Auto-approve actions are
    listed, describe safe read/draft tasks as low-risk internal work instead.
  * Never infer that a user-visible artifact exists from a text summary
    alone. Treat prior work as "text-only" unless the context explicitly
    lists file/media/document evidence.
  * When a proposed task's deliverable is a user-visible artifact (file,
    image, PDF, document, deck, spreadsheet, video, export, attachment,
    or domain-specific file), make `expected_output` require artifact
    evidence such as `image_url`, `file_url`, `document_url`, `fs_path`,
    or `files`.
    If the available agents can only provide text parameters, say so in
    `notes` instead of claiming drawings already exist.

Time horizon — pick the right owner for the timeline:
  * Agent-driven services (AI handles the work) typically complete
    in hours to 1 day. Use these for tasks the owner shouldn't have
    to touch.
  * Human-driven services need 1-14 days because a person has to do
    them. Reserve these for judgement calls, physical work, or things
    the AI genuinely can't do — and write the task so a human can
    pick it up cold.

Task description quality — **this is critical**:
  * Write descriptions as if briefing a new hire who has never seen
    this workspace. Include: what exactly to do, what data/inputs
    to use, what the expected deliverable looks like, and any
    constraints or quality standards.
  * Bad: "Create a social media post"
  * Good: "Draft a Twitter thread (3-5 tweets) about our Q1 growth
    metrics. Use data from the analytics dashboard. Tone: confident
    but not boastful. Include one data visualization. Post during
    peak hours (9-11am EST)."
  * The Planner and executing agent see ONLY the title + description
    + expected_output. They cannot ask you clarifying questions.
    Everything they need must be in the description.
  * If one proposed task needs the deliverable from another task in the
    same review cycle, assign both stable `task_key` values and set the
    downstream task's `depends_on_task_keys`. Do not rely on prose such
    as "after the previous task" to express dependencies.
  * Use `required_capabilities` to describe what runtime business
    capabilities the task needs. Choose only ids from the capability catalog
    below. Do not put tool names, MCP tool names, or provider-specific action
    strings in `required_capabilities`.
"""


RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT = {
    "review_id": "<provided in user prompt — copy verbatim>",
    "summary": "One paragraph: this cycle's framing.",
    "tasks": [
        {
            "task_key": "stable_snake_case_key_unique_in_this_review",
            "title": "Short imperative title (≤80 chars).",
            "description": (
                "Detailed execution brief (3-8 sentences). Include: "
                "what exactly to do, what inputs/data to use, "
                "expected deliverable format, quality standards, "
                "and any constraints. The executing agent sees ONLY "
                "this description — make it self-contained."
            ),
            "owner_service_key": "<service from allowed list>",
            "delegate_service_keys": ["<other allowed service>"],
            "depends_on_task_keys": ["task_key_that_must_finish_first"],
            "required_capabilities": ["workspace.search", "web.safe_search"],
            "priority": 3,
            "estimated_impact": {
                "goal_id": "<Goal id this is meant to move, or null>",
                "metric_delta": 50.0,
                "rationale": "Why we expect that delta.",
            },
            "rationale": "Why now, why this approach.",
            "expected_output": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "What the output looks like",
                    },
                },
            },
            "deliverables": [
                {
                    "name": "short_identifier_for_this_output",
                    "kind": "value | file",
                    "shape": (
                        "one of: ArtifactResult, TextResult, DocumentResult, "
                        "ListResult, PublishResult, CountResult, DraftPack"
                    ),
                    "acceptance": "How to tell this deliverable is complete and acceptable.",
                    "usage": "How this deliverable is consumed by downstream steps.",
                },
            ],
        }
    ],
    "notes": "Optional observations not actioned this cycle.",
}


def runtime_strategist_review_billing_context(
    *,
    entity_id: str,
    workspace_id: str | None = None,
    source: str = RUNTIME_STRATEGIST_SOURCE,
) -> Any:
    """Build the LLM billing context for Strategist review generation."""

    return runtime_llm_billing_context(
        entity_id,
        workspace_id=workspace_id,
        source=source,
    )


async def runtime_execute_strategist_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
) -> RuntimeTextCompletionResult:
    """Execute a Strategist review completion with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_one_shot_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ),
        entity_id=entity_id,
        workspace_id=workspace_id,
        source=RUNTIME_STRATEGIST_SOURCE,
        max_tokens=4096,
    )


def runtime_strategist_system_prompt(
    ctx: Any,
    *,
    preamble: str | None = None,
) -> str:
    """Build the stable Strategist system prompt for a review cycle."""

    services_block = runtime_strategist_services_block(ctx)
    capabilities_block = runtime_strategist_task_capabilities_block()
    schema_hint = json.dumps(RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT, indent=2)

    base = preamble or RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    try:
        head = base.format(workspace_name=ctx.workspace.name)
    except (KeyError, IndexError):
        # Skill-authored preambles may contain stray braces; keep the run alive.
        head = base
    template_block = runtime_strategist_template_block(ctx)

    parts = [head]
    if template_block:
        parts.append(template_block)
    parts.append(
        "Available services (use these for owner_service_key + delegate_service_keys):\n"
        f"{services_block}"
    )
    parts.append(
        "Available task runtime capabilities (use only these ids in required_capabilities):\n"
        f"{capabilities_block}"
    )
    parts.append(
        "Every task MUST list at least one deliverable in `deliverables`. "
        "For each deliverable pick a `shape` from this exact vocabulary: "
        "ArtifactResult, TextResult, DocumentResult, ListResult, "
        "PublishResult, CountResult, DraftPack."
    )
    parts.append(
        "Output valid JSON matching this exact shape. No prose, no markdown:\n"
        f"{schema_hint}"
    )
    return "\n\n".join(parts) + "\n"


def runtime_strategist_template_block(ctx: Any) -> str:
    """Render operator-authored Strategist template constraints."""
    tpl = getattr(ctx, "strategist_template", None) or {}
    if not isinstance(tpl, dict) or not tpl:
        return ""

    lines: list[str] = []

    business_model = tpl.get("business_model")
    if isinstance(business_model, dict) and business_model:
        lines.append("# Business model for this workspace")
        if business_model.get("model_type"):
            lines.append(f"- Type: {business_model['model_type']}")
        if business_model.get("primary_signal"):
            lines.append(f"- Primary signal: {business_model['primary_signal']}")
        secondary = business_model.get("secondary_signals") or []
        if secondary:
            lines.append("- Secondary signals: " + ", ".join(str(item) for item in secondary))
        anti = business_model.get("anti_signals") or []
        if anti:
            lines.append("- Anti-signals: " + ", ".join(str(item) for item in anti))
        if business_model.get("decision_window"):
            lines.append(f"- Decision window: {business_model['decision_window']}")

    proposal_shape = tpl.get("proposal_shape")
    if isinstance(proposal_shape, dict) and proposal_shape:
        if lines:
            lines.append("")
        lines.append("# Proposal shape - hard constraints")
        if "max_tasks_per_cycle" in proposal_shape:
            lines.append(f"- Propose at most {proposal_shape['max_tasks_per_cycle']} task(s) per review cycle")
        owner_mix = proposal_shape.get("preferred_owner_mix")
        if isinstance(owner_mix, dict):
            lines.append("- Preferred owner mix: " + ", ".join(f"{key}~{value}" for key, value in owner_mix.items()))
        preferred = proposal_shape.get("preferred_categories") or []
        if preferred:
            lines.append("- Prefer these task categories: " + ", ".join(str(item) for item in preferred))
        horizon = proposal_shape.get("task_horizon_hours")
        if isinstance(horizon, list) and len(horizon) == 2:
            lines.append(f"- Each task should be doable in {horizon[0]}-{horizon[1]} hours")
        must_weekly = proposal_shape.get("must_include_categories_per_week") or []
        if must_weekly:
            lines.append("- Must include at least one task per week in: " + ", ".join(str(item) for item in must_weekly))

    do_not = tpl.get("do_not_propose") or []
    if do_not:
        if lines:
            lines.append("")
        lines.append("# Do not propose")
        for item in do_not:
            lines.append(f"- {item}")

    voice = tpl.get("voice")
    if isinstance(voice, dict) and voice:
        if lines:
            lines.append("")
        lines.append("# Voice for proposal card text")
        if voice.get("style"):
            lines.append(f"- Style: {voice['style']}")
        examples = voice.get("examples") or []
        if examples:
            lines.append("- Examples of well-shaped task titles:")
            for example in examples:
                lines.append(f"  - {example}")

    rubric = tpl.get("evaluation_rubric")
    if isinstance(rubric, dict) and rubric.get("weights"):
        if lines:
            lines.append("")
        lines.append("# Self-evaluation rubric")
        weights = rubric["weights"]
        if isinstance(weights, dict):
            for key, value in weights.items():
                lines.append(f"- {key}: weight={value}")
        if "passing_score" in rubric:
            lines.append(f"- Passing score: {rubric['passing_score']}")

    return "\n".join(lines)


def runtime_strategist_user_prompt(ctx: Any, *, review_id: str) -> str:
    """Build the review-cycle user prompt from gathered Strategist context."""

    sections = [f"# Review trigger\n{ctx.trigger}\n\nreview_id to use: `{review_id}`"]
    readiness_section = _format_workspace_readiness(ctx)
    if readiness_section:
        sections.append("# Workspace readiness\n" + readiness_section)

    if ctx.missing_setup:
        missing_labels = {
            "no_agents": "No agents are mapped to services yet",
            "no_goals": "No goals are defined yet",
            "no_channels": "Required workspace channel declarations are not configured yet",
            "no_integrations": "No external integrations are configured yet",
        }
        missing_text = "\n".join(
            f"- {missing_labels.get(item, item)}" for item in ctx.missing_setup
        )
        sections.append(
            "# Workspace setup incomplete\n"
            f"The following are NOT ready:\n{missing_text}\n\n"
            "DO NOT propose work tasks that depend on missing setup. "
            "Instead, propose generic setup/configuration tasks (e.g. "
            "'Configure the declared channel' or 'Define workspace goals') "
            "or propose 0 tasks with a note explaining "
            "what the operator needs to configure first."
        )
        missing_channels = getattr(ctx, "missing_channel_requirements", []) or []
        if missing_channels:
            sections.append("# Missing channel requirements\n" + _format_missing_channels(missing_channels))

    if ctx.configured_integrations:
        sections.append(
            "# Workspace-scoped external integrations\n"
            f"Available: {', '.join(ctx.configured_integrations)}\n"
            "Only propose tasks that use these workspace-scoped integrations. "
            "Do NOT propose tasks requiring unconfigured services. "
            "Do NOT propose tasks for other social platforms merely because "
            "the company account has them connected elsewhere."
        )
    else:
        sections.append(
            "# Workspace-scoped external integrations\n"
            "_(none configured)_ — Built-in workspace channels may still be available, "
            "but do not propose tasks that require third-party credentials."
        )

    sections.append("# Configured channels\n" + (_format_channels(ctx) or "_(none configured)_"))
    sections.append("# Workspace knowledge nets\n" + (_format_knowledge_nets(ctx) or "_(none)_"))
    governance_section = _format_governance_policy(ctx)
    if governance_section:
        sections.append("# Governance policy\n" + governance_section)
    sections.append("# Active goals + pace\n" + (_format_goals(ctx) or "_(none)_"))
    evaluation_section = _format_workspace_evaluation(ctx)
    if evaluation_section:
        sections.append("# Workspace evaluation scorecard\n" + evaluation_section)
    sections.append("# Recent tasks (last 30d)\n" + (_format_tasks(ctx) or "_(none)_"))
    sections.append("# Recent plans\n" + (_format_plans(ctx) or "_(none)_"))
    reconciliation_section = _format_work_batch_reconciliation(ctx)
    if reconciliation_section:
        sections.append("# Work batch reconciliation\n" + reconciliation_section)
    sections.append("# Recent workspace activity\n" + (_format_activity(ctx) or "_(none)_"))
    runtime_section = _format_runtime_learning(ctx)
    if runtime_section:
        sections.append("# Runtime evidence + agent learning candidates\n" + runtime_section)
    if ctx.operating_memory:
        sections.append("# Workspace operating memory (canonical docs)\n" + ctx.operating_memory)
    sections.append("# Workspace memory (most relevant)\n" + (_format_memory(ctx) or "_(none indexed yet)_"))
    if ctx.open_proposed_tasks:
        sections.append("# Already-proposed tasks not yet approved\n" + _format_open_proposed(ctx))
    outcomes_section = _format_proposal_outcomes(ctx)
    if outcomes_section:
        sections.append("# Recent proposal outcomes (last 30d)\n" + outcomes_section)
    calibration_section = _format_calibration(ctx)
    if calibration_section:
        sections.append("# Your calibration so far\n" + calibration_section)

    sections.append(
        "Now produce the JSON Proposal. review_id MUST equal "
        f"`{review_id}` exactly."
    )
    return "\n\n".join(sections)


def runtime_strategist_services_block(ctx: Any) -> str:
    """Render the service owner options available to the Strategist."""

    lines: list[str] = []
    for subscription in ctx.subscriptions:
        if subscription.service_key not in ctx.allowed_service_keys:
            continue
        agent = ctx.agents_by_id.get(subscription.agent_id) if subscription.agent_id else None
        kind, horizon = runtime_strategist_assignee_classification(
            subscription,
            agent,
        )
        agent_name = agent.name if agent else "?"
        lines.append(
            f"  - {subscription.service_key} ({kind} · {horizon}, agent={agent_name})"
        )
    return "\n".join(lines) or "  (no services available — propose 0 tasks)"


def runtime_strategist_task_capabilities_block() -> str:
    """Render the business capability catalog available to proposed tasks."""

    lines: list[str] = []
    for capability in strategist_task_capability_descriptors():
        approval = "approval-gated" if capability.get("required_approval") else "read/safe"
        lines.append(
            f"  - {capability['id']} ({capability['risk_level']}, {approval}): "
            f"{capability['description']}"
        )
    return "\n".join(lines) or "  (no task runtime capabilities available)"


def runtime_strategist_assignee_classification(
    subscription: Any,
    agent: Any,
) -> tuple[str, str]:
    """Return (kind, horizon) for a subscription's assignee."""

    cfg_kind = (subscription.config or {}).get("assignee_kind") if subscription else None
    if cfg_kind == "human":
        return ("human-driven", "1-14d")
    if cfg_kind == "agent":
        return ("agent-driven", "hours-1d")

    if agent is None:
        return ("human-driven", "1-14d")

    category = (agent.category or "").lower()
    tags = {(tag or "").lower() for tag in (agent.tags or [])}
    if (
        category in {"human", "operator", "staff", "contractor"}
        or "human" in tags
        or not (agent.system_prompt or "").strip()
    ):
        return ("human-driven", "1-14d")
    return ("agent-driven", "hours-1d")


def runtime_strategist_tasks_text(ctx: Any) -> str:
    """Render recent task summaries for Strategist review context."""

    return _format_tasks(ctx)


def _format_goals(ctx: Any) -> str:
    lines = []
    for goal in ctx.goals:
        cur = float(goal.current_value) if goal.current_value is not None else None
        tgt = float(goal.target_value) if goal.target_value is not None else None
        progress = f"{cur:g} / {tgt:g}" if cur is not None and tgt is not None else "?"
        lines.append(
            f"- **{goal.title}** (id=`{goal.id}`)  metric={goal.metric_key}  "
            f"progress={progress}  pace={goal.pace_status or 'unknown'}  "
            f"deadline={goal.deadline.isoformat() if goal.deadline else 'none'}"
        )
    return "\n".join(lines)


def _format_workspace_evaluation(ctx: Any) -> str:
    try:
        from packages.core.services.workspace_evaluation import (
            format_workspace_evaluation_for_prompt,
        )

        return format_workspace_evaluation_for_prompt(
            getattr(ctx, "workspace_evaluation", None)
        )
    except Exception:
        return ""


def _format_channels(ctx: Any) -> str:
    lines: list[str] = []
    for channel in getattr(ctx, "configured_channels", []) or []:
        role = channel.get("role") or "channel"
        channel_type = channel.get("channel_type") or "unknown"
        linked = channel.get("linked_service_key") or "unassigned"
        built_in = "built-in" if channel.get("built_in") else "integration"
        purpose = channel.get("purpose") or ""
        line = f"- {role}: {channel_type} ({built_in}, linked_service={linked})"
        if purpose:
            line += f" — {purpose[:220]}"
        lines.append(line)
    return "\n".join(lines)


def _format_workspace_readiness(ctx: Any) -> str:
    readiness = getattr(ctx, "workspace_readiness", None)
    if not isinstance(readiness, dict):
        return ""
    parts = readiness.get("parts")
    if not isinstance(parts, list):
        return ""
    lines: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        name = part.get("name") or part.get("key") or "part"
        status = part.get("status") or "unknown"
        summary = part.get("summary") or ""
        role = part.get("role") or ""
        check = part.get("check") or ""
        line = f"- {name}: {status}"
        if summary:
            line += f" — {summary}"
        if role:
            line += f"\n  Role: {role}"
        if check:
            line += f"\n  Check: {check}"
        missing = part.get("missing_setup_key")
        if missing:
            line += f"\n  Missing setup key: {missing}"
        lines.append(line)
    return "\n".join(lines)


def _format_missing_channels(channels: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for channel in channels:
        role = channel.get("role") or "channel"
        channel_type = channel.get("channel_type") or "unknown"
        provider = channel.get("provider") or channel_type
        purpose = channel.get("purpose") or ""
        line = f"- {role}: {channel_type} provider={provider}"
        if purpose:
            line += f" — {purpose}"
        lines.append(line)
    return "\n".join(lines)


def _format_knowledge_nets(ctx: Any) -> str:
    lines: list[str] = []
    for net in getattr(ctx, "knowledge_nets", []) or []:
        name = net.get("name") or "(untitled)"
        doc_count = int(net.get("document_count") or 0)
        linked = ", ".join(net.get("linked_service_keys") or []) or "all"
        starter_status = net.get("starter_document_status")
        starter_task_key = net.get("starter_task_key")
        purpose = net.get("purpose") or ""
        line = f"- {name}: {doc_count} document(s), linked_service_keys={linked}"
        if starter_status:
            line += f", starter_document={starter_status}"
            if starter_task_key:
                line += f", starter_task_key={starter_task_key}"
        if purpose:
            line += f" — {purpose[:220]}"
        lines.append(line)
    return "\n".join(lines)


def _format_governance_policy(ctx: Any) -> str:
    policy = getattr(ctx, "governance_policy", None) or {}
    if not policy:
        return ""
    lines: list[str] = []
    hitl = policy.get("hitl_required_actions") or []
    blocked = policy.get("never_allow_actions") or []
    auto = policy.get("auto_approve_actions") or []
    if hitl:
        lines.append("HITL required actions: " + ", ".join(str(value) for value in hitl))
    if blocked:
        lines.append("Never allow actions: " + ", ".join(str(value) for value in blocked))
    if auto:
        lines.append("Auto-approve actions: " + ", ".join(str(value) for value in auto))
    else:
        lines.append("Auto-approve actions: _(none configured)_")
    if policy.get("max_risk_level"):
        lines.append(f"Max risk level: {policy['max_risk_level']}")
    return "\n".join(lines)


def _format_tasks(ctx: Any) -> str:
    lines = []
    for task in ctx.recent_tasks:
        bits = [f"- [{task.status}] {task.title}"]
        if task.owner_service_key:
            bits.append(f"(owner={task.owner_service_key})")
        if task.completed_at:
            bits.append(f"completed={task.completed_at.date().isoformat()}")
        output = task.actual_output or {}
        files = _output_artifact_refs(output)
        if files:
            bits.append(f"artifacts={len(files)}")
        elif task.status == "completed" and _looks_artifact_task(task):
            bits.append("artifacts=0 (text-only; do not describe as files/artifacts)")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _format_plans(ctx: Any) -> str:
    lines = []
    for plan in ctx.recent_plans:
        cost = (plan.cost_tracking or {}).get("usd")
        cost_part = f" cost=${cost:.3f}" if cost else ""
        lines.append(
            f"- {plan.id} status={plan.status} mode={plan.execution_mode}{cost_part}"
        )
    return "\n".join(lines)


def _format_work_batch_reconciliation(ctx: Any) -> str:
    rows = getattr(ctx, "work_batch_reconciliation", []) or []
    lines: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        batch_id = item.get("batch_id") or "unknown"
        status = item.get("status") or "unknown"
        open_count = len(item.get("open_task_ids") or [])
        stale_count = len(item.get("stale_task_ids") or [])
        missing_count = len(item.get("missing_task_ids") or [])
        summary = item.get("summary") or item.get("source_kind") or "work batch"
        lines.append(
            f"- batch={batch_id} status={status} summary={summary} "
            f"open={open_count} stale={stale_count} missing={missing_count}"
        )
        stale_tasks = item.get("stale_tasks") or []
        for task in stale_tasks[:5]:
            if not isinstance(task, dict):
                continue
            title = task.get("title") or task.get("task_id") or "task"
            task_status = task.get("status") or "unknown"
            age = task.get("age_hours")
            owner = task.get("owner_service_key") or "unassigned"
            lines.append(
                f"  - [{task_status}] {title} owner={owner} age_hours={age}"
            )
        missing = item.get("missing_task_ids") or []
        if missing:
            lines.append("  - Missing task rows: " + ", ".join(str(value) for value in missing[:8]))
    return "\n".join(lines)


def _format_activity(ctx: Any) -> str:
    lines = []
    for event in getattr(ctx, "recent_activity", []) or []:
        created = getattr(event, "created_at", None)
        created_part = created.isoformat() if created else "unknown_time"
        event_type = getattr(event, "event_type", None) or "activity"
        summary = (getattr(event, "summary", None) or "").strip()
        details = getattr(event, "details", None) or {}
        detail_bits: list[str] = []
        if isinstance(details, dict):
            if details.get("choice"):
                detail_bits.append(f"choice={details['choice']}")
            if details.get("review_id"):
                detail_bits.append(f"review_id={details['review_id']}")
            if details.get("learnings_written"):
                detail_bits.append(f"learnings_written={details['learnings_written']}")
        line = f"- {created_part} [{event_type}] {summary[:220] or '(no summary)'}"
        if detail_bits:
            line += " (" + ", ".join(str(bit) for bit in detail_bits) + ")"
        lines.append(line)
    return "\n".join(lines)


def _format_runtime_learning(ctx: Any) -> str:
    try:
        from packages.core.services.runtime_learning import format_runtime_learning_context

        return format_runtime_learning_context(
            evidence=ctx.recent_runtime_evidence,
            candidates=ctx.learning_candidates,
            max_items=8,
        )
    except Exception:
        return ""


def _format_memory(ctx: Any) -> str:
    lines = []
    for memory in ctx.relevant_memory:
        title = memory.get("title") or "(untitled)"
        scope = memory.get("scope") or "?"
        confidence = memory.get("confidence", 1.0)
        body = (memory.get("content") or "").strip()
        excerpt = (body[:200] + "…") if len(body) > 200 else body
        lines.append(f"- **[{scope}] {title}** (conf={confidence:.2f})\n  {excerpt}")
    return "\n".join(lines)


def _format_open_proposed(ctx: Any) -> str:
    lines = []
    for task in ctx.open_proposed_tasks:
        review = (task.details or {}).get("strategist_review_id", "?")
        lines.append(f"- {task.title} (review={review})")
    return "\n".join(lines)


_OUTCOME_LABELS = {
    "completed": "✅ Approved + completed (lean into these patterns)",
    "rejected": "❌ Rejected by operator (don't re-propose the same way)",
    "abandoned": "⚠️ Abandoned / failed (similar work stalled — rethink approach)",
    "in_progress": "⏳ Approved + still running (informational only)",
}


def _format_calibration(ctx: Any) -> str:
    cal = ctx.calibration or {}
    if not cal:
        return ""
    lines: list[str] = []
    narrative = cal.get("narrative")
    if narrative:
        lines.append(narrative)

    sample_size = cal.get("sample_size", 0)
    if sample_size:
        bits = [f"sample={sample_size}"]
        if cal.get("mean_ratio") is not None:
            bits.append(f"mean(actual/predicted)={cal['mean_ratio']:.2f}x")
        if cal.get("approval_rate") is not None:
            bits.append(f"approval={cal['approval_rate']:.0%}")
        if cal.get("win_rate") is not None:
            bits.append(f"win={cal['win_rate']:.0%}")
        if cal.get("harmed_rate") is not None:
            bits.append(f"harmed={cal['harmed_rate']:.0%}")
        lines.append("Numbers: " + " · ".join(bits))
        breakdown_keys = ("won", "washed", "lost", "harmed")
        if any(key in cal for key in breakdown_keys):
            breakdown = " · ".join(
                f"{key}={cal.get(key, 0)}" for key in breakdown_keys
            )
            lines.append(f"Breakdown: {breakdown}")
    elif cal.get("approval_rate") is not None:
        lines.append(
            f"Numbers: approval={cal['approval_rate']:.0%} "
            "(no labeled outcomes yet)"
        )
    return "\n".join(lines)


def _format_proposal_outcomes(ctx: Any) -> str:
    if not ctx.recent_proposal_outcomes:
        return ""
    blocks: list[str] = []
    for bucket in ("completed", "rejected", "abandoned", "in_progress"):
        rows = ctx.recent_proposal_outcomes.get(bucket) or []
        if not rows:
            continue
        lines = [f"## {_OUTCOME_LABELS[bucket]}"]
        for task in rows:
            owner = task.owner_service_key or "?"
            details = task.details or {}
            output = task.actual_output or {}
            artifacts = _output_artifact_refs(output)

            line = f"- [{owner}] {task.title}"

            reason = details.get("rejection_reason")
            if bucket == "rejected" and reason:
                line += f"\n  Reason: {reason}"

            plan_steps = output.get("steps")
            if isinstance(plan_steps, list) and plan_steps:
                done = sum(1 for step in plan_steps if step.get("status") == "done")
                failed = [step for step in plan_steps if step.get("status") == "failed"]
                line += f"\n  Plan: {done}/{len(plan_steps)} steps completed"
                for failed_step in failed[:2]:
                    err = failed_step.get("error", {})
                    line += (
                        f"\n  ✗ Step '{failed_step.get('key')}' failed: "
                        f"{err.get('type', '?')} — {err.get('message', '')[:150]}"
                    )

            if artifacts:
                line += f"\n  Artifacts: {len(artifacts)} file/image/document reference(s) recorded"
            elif bucket == "completed" and _looks_artifact_task(task):
                line += "\n  Artifacts: none recorded — treat this as text-only, not a generated file/artifact"

            if bucket == "completed":
                response = output.get("response")
                if response:
                    line += f"\n  Output: {response[:300]}"
                elif plan_steps:
                    last_done = [
                        step
                        for step in plan_steps
                        if step.get("status") == "done" and step.get("result_summary")
                    ]
                    if last_done:
                        line += f"\n  Result: {last_done[-1]['result_summary'][:300]}"

            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_ARTIFACT_TASK_TERMS = (
    "文件", "文档", "附件", "下载", "交付物", "产物", "资料包", "压缩包",
    "pdf", "docx", "word", "ppt", "pptx", "slides", "deck", "xlsx", "excel",
    "csv", "表格", "视频", "音频", "海报", "封面",
    "图纸", "设计图", "效果图", "渲染图", "图片", "图像", "三视图", "多视角",
    "尺寸标注", "标注图", "出图", "cad", "solidworks", "pdf图纸",
    "image", "visual", "drawing", "render", "mockup", "sketch", "diagram",
    "file", "artifact", "attachment", "download", "document", "spreadsheet",
    "video", "audio",
)
_ARTIFACT_OUTPUT_KEYS = {
    "files", "artifacts", "file_url", "document_url", "image_url",
    "video_url", "result_url", "url", "fs_path", "document_id", "image_urls",
}
_ARTIFACT_NEGATION_TERMS = (
    "不需要图片", "无需图片", "不要图片", "不生成图片", "不用图片",
    "不需要图纸", "无需图纸", "不要图纸", "不生成图纸",
    "不需要文件", "无需文件", "不要文件", "不生成文件", "不用文件",
    "只需要文字", "文字即可", "文字方案", "text only", "no image",
    "no images", "no file", "no files", "no attachment",
)


def _looks_artifact_task(task: Any) -> bool:
    expected = task.expected_output or {}
    if isinstance(expected, dict):
        if expected.get("requires_artifact") is True or expected.get("artifact_required") is True:
            return True
        if _schema_has_artifact_key(expected):
            return True
    text = (
        f"{task.title or ''}\n{task.description or ''}\n"
        f"{json.dumps(expected, ensure_ascii=False, default=str)}"
    ).lower()
    if any(term in text for term in _ARTIFACT_NEGATION_TERMS):
        return False
    return any(term in text for term in _ARTIFACT_TASK_TERMS)


def _schema_has_artifact_key(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if {str(key).lower() for key in value} & _ARTIFACT_OUTPUT_KEYS:
        return True
    props = value.get("properties")
    if isinstance(props, dict):
        if {str(key).lower() for key in props} & _ARTIFACT_OUTPUT_KEYS:
            return True
        return any(_schema_has_artifact_key(prop) for prop in props.values())
    items = value.get("items")
    return isinstance(items, dict) and _schema_has_artifact_key(items)


def _output_artifact_refs(output: Any) -> list:
    if not isinstance(output, dict):
        return []
    refs = []
    files = output.get("files")
    if isinstance(files, list):
        refs.extend([file for file in files if file])
    for step in output.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_files = step.get("files")
        if isinstance(step_files, list):
            refs.extend([file for file in step_files if file])
        for key in _ARTIFACT_OUTPUT_KEYS - {"files", "artifacts"}:
            if step.get(key):
                refs.append({key: step[key]})
    return refs
