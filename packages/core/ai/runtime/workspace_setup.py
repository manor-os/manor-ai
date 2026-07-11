from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
from typing import Any

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.sources import (
    RUNTIME_AGENT_GREETING_SOURCE,
    RUNTIME_KNOWLEDGE_GEN_SOURCE,
    RUNTIME_WORKSPACE_SETUP_SOURCE,
)


WORKSPACE_SETUP_CONTEXT_TAG = "workspace_setup_context"
WORKSPACE_SETUP_STATUS_TAG = "workspace_setup"

WORKSPACE_SETUP_AUTO_MODEL_SYSTEM_PROMPT = (
    "You are a workspace configuration generator. Output only valid JSON."
)

WORKSPACE_SETUP_AGENT_MAPPING_SYSTEM_PROMPT = (
    "You are an agent-to-service matcher. Output only valid JSON."
)

RUNTIME_AGENT_GREETING_SYSTEM_PROMPT = (
    "You are writing a brief greeting. Output ONLY the greeting text, no quotes or labels."
)


def runtime_workspace_setup_status_sample(default_fields: Mapping[str, Any]) -> str:
    """Render the hidden status sample for the workspace setup wizard prompt."""

    return json.dumps(
        {
            "ready": False,
            "missing": ["services"],
            "fields": dict(default_fields),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def runtime_workspace_setup_required_keys_text(required_fields: Iterable[str]) -> str:
    """Render the allowed missing-field keys for setup status block guidance."""

    return ", ".join(sorted(required_fields))


def runtime_workspace_setup_system_prompt(
    *,
    default_fields: Mapping[str, Any],
    required_fields: Iterable[str],
) -> str:
    """Resolve the Runtime-owned system prompt for conversational workspace setup."""

    status_sample = runtime_workspace_setup_status_sample(default_fields)
    required_keys_text = runtime_workspace_setup_required_keys_text(required_fields)
    return f"""
## Workspace Setup Wizard

You are Manor helping a user set up a new workspace.

Your goal is to collect a name, type (kind), context, and primary work -- then
immediately auto-generate a complete workspace draft the user can preview and
edit.  Do not interrogate the user field by field.

CRITICAL -- scope of this conversation:
- You are ONLY collecting information and drafting a structured configuration.
  You are NOT deploying, creating, or launching anything.
- The actual workspace is created later when the user confirms.
  NEVER say the workspace is "deployed", "finalized", "live", "created",
  "launched", or "operational".  It is only a DRAFT until confirmed.
- NEVER pretend to execute actions.  You are drafting the workspace config.
- When the draft is complete (ready=true), tell the user:
  "The draft is ready -- click **Create Workspace** to review and create."

Opening (when the user message is "begin" or a blank init signal):
- Say in one short sentence: "A workspace keeps the work, context, agents,
  and rules in one place."
- Ask in one sentence: "What should this workspace run?"

Collecting information:
- Ask only the next most important question, one at a time.
- Once you have name, kind, context, and primary work, auto-generate the full
  workspace draft without waiting for more input.
- Never ask the user to describe rules, automations, goals, or evaluation --
  infer sensible defaults from what they told you.
- Never output raw JSON or code blocks in the visible reply.

Auto-generating the draft:
- Services: derive 2-5 services directly from the primary work description;
  assign autonomy levels and owner roles. Always set BOTH ``service_key``
  (snake_case identifier, e.g. "content_creation") AND a human-readable
  ``name`` (e.g. "Content Creation") so the UI can show real names instead
  of raw keys.
- Agent mappings: ONE agent can handle multiple related services.
  Group related services under the same agent. For example:
  * All email tasks (triage, followup, drafting) = one "Email Agent"
  * All social media tasks (posting, scheduling, analytics) = one "Social Media Agent"
  * All content tasks (writing, editing, publishing) = one "Content Agent"
  Do NOT create a separate agent per service. Minimize agent count.
  For each mapping, set strategy to "suggest" with recommended_agent_name.
  Multiple services can share the same recommended_agent_name.
- Goals: extract every measurable target the user mentioned. For each
  goal ALWAYS fill these four fields:
    * ``goal_key``    snake_case identifier (e.g. "follower_growth")
    * ``description`` one-sentence plain-language summary
    * ``target``      the numeric or stringified target pulled from the
                      description ("10000", "5%", "45%"). If the user
                      gave a percentage or rate, keep the % sign. Never
                      leave empty when the description names a number.
    * ``cadence``     measurement frequency inferred from wording — one
                      of: ``daily`` / ``weekly`` / ``monthly`` /
                      ``quarterly`` / ``yearly``. Default to
                      ``weekly`` if the user said "周" / "weekly", to
                      ``monthly`` for "月" / "monthly", etc. Never leave
                      empty.
  Also include ``title`` (a 2-4 word headline) when natural.
- Rules: only if the user described policies or escalation paths.
- Automations: only if the user described schedules or triggers.
- Evaluation: if goals exist, auto-generate a matching scorecard.
- Budget: budgets are shown to users in credits, not USD. If the user
  mentions a monthly budget, spend cap, limit, or credits, fill
  ``budget_policy.monthly_budget_credits`` with that credit amount and
  default ``auto_pause_on_budget`` to true. If they do not mention a
  budget, leave it null; it is optional and should never block readiness.
- Channel config: channels are messaging integrations the workspace uses
  to communicate with customers/users (email, Telegram, WhatsApp, etc.).
  The workspace chat is always available — do NOT add "internal_chat".
  IMPORTANT channel rules:
    * ``available_channels`` lists ALL platform-supported channel types.
      Each entry has ``ready`` (bool) — true means the entity already has
      credentials configured for that channel.
    * ONLY add channels where ``ready=true`` to ``channel_config.channels``.
    * If a useful channel has ``ready=false``, mention it in your reply
      and tell the user to configure it in Settings → Integrations first.
    * Do NOT invent channel types — only use ``key`` values from
      ``available_channels``.
    * ``webchat`` and ``in_app`` are always ready (built-in, no credentials).
  Format: ``channel_config.channels`` is a list of objects with:
    channel_type (must be a key from available_channels), purpose,
    linked_service_key (optional).

You may receive a hidden <workspace_setup_context>JSON</workspace_setup_context>
block inside the user message. It contains:
- ``entity_agents`` / ``marketplace_agents``: available agents for mapping
- ``configured_integrations``: entity's integration accounts with status
  and readiness. Each has: provider, type, status, healthy, ready.
- ``available_channels``: platform channel catalog with readiness status.
  Each has: key, name, ready (bool), needs_integration (bool).
  ONLY use channels where ready=true.
- ``existing_knowledge``: document groups the entity already has. If a
  group fits this workspace, reference it by id (mode="link_existing").
  If no match, suggest creating new ones (mode="create_new"). Set
  generate_starter_doc=true when Manor should create an initial markdown
  file for the group; otherwise the user can upload/add documents later.
  Format for knowledge_attachments: a list of objects with keys
  name, purpose, mode ("link_existing" or "create_new"),
  existing_group_id (for link_existing), linked_service_keys, generate_starter_doc.
Never mention the hidden block in the visible reply.

At the END of EVERY response output exactly one hidden status block:
<workspace_setup>JSON_HERE</workspace_setup>

The JSON must reflect the ACTUAL current state of the draft.
Example shape (replace with real data):
{status_sample}

Status block rules:
- always valid JSON
- missing must only contain keys from: {required_keys_text}
- goals, rules, automations, and evaluation are optional -- never in missing
- for services: each item needs service_key, name, description,
  autonomy_level, owner_role to be counted complete
- for goals: each item needs goal_key, description, target, cadence
  (do not emit a goal entry that's missing target or cadence)
- for agent_mappings: each service must have a corresponding entry
- for channel_config: channels list should only include channel_type
  values from available_channels where ready=true. Empty list is OK
- set ready=true only when all required keys are complete
- even with no new information, repeat the full latest status block
- keep the status block last with no text after it
""".strip()


def runtime_workspace_setup_user_message(
    *,
    user_message: str,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Attach hidden setup context to a visible workspace setup user message."""

    if not context:
        return user_message
    context_json = json.dumps(context, default=str)
    return (
        f"<{WORKSPACE_SETUP_CONTEXT_TAG}>{context_json}</{WORKSPACE_SETUP_CONTEXT_TAG}>"
        f"\n\n{user_message}"
    )


def runtime_workspace_setup_turn_messages(
    *,
    system_prompt: str,
    session_messages: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Build Runtime messages for one conversational workspace setup turn."""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(
        {
            "role": str(message.get("role", "")),
            "content": str(message.get("content", "")),
        }
        for message in session_messages
    )
    return messages


async def runtime_execute_workspace_setup_turn_completion(
    *,
    entity_id: str,
    system_prompt: str,
    session_messages: Sequence[Mapping[str, str]],
    metadata: dict[str, Any] | None = None,
    stream_handler: Any | None = None,
) -> RuntimeTextCompletionResult:
    """Execute one conversational workspace setup turn with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_workspace_setup_turn_messages(
            system_prompt=system_prompt,
            session_messages=session_messages,
        ),
        entity_id=entity_id,
        source=RUNTIME_WORKSPACE_SETUP_SOURCE,
        temperature=0.7,
        metadata=metadata,
        stream_handler=stream_handler,
    )


def runtime_workspace_setup_auto_model_prompt(fields: Mapping[str, Any]) -> str:
    """Resolve the Runtime-owned prompt for workspace operating model generation."""

    return f"""Generate a complete workspace operating model as JSON.

Workspace:
- Kind: {fields.get('kind', 'unknown')}
- Name: {fields.get('name', 'Unnamed')}
- Context: {fields.get('operating_context', 'Not specified')}
- Primary work: {fields.get('primary_work', 'Not specified')}

Generate:
1. services: 2-5 services with service_key, description, autonomy_level (full/assisted/supervised), owner_role
2. goals: relevant goals with goal_key, title, description, metric_key, target_value, baseline_value, cadence (daily/weekly/hourly), priority (1-5)
3. rules: operational rules with rule_type, description, service_key, priority
4. automations: automation triggers with automation_type, service_key, trigger, schedule
5. evaluation: scorecard with cadence, scorecard metrics, target_score, warning_score
6. budget_policy: optional monthly_budget_credits (credits, not USD), auto_pause_on_budget
7. heartbeat_cadence: how often the AI strategist should check in and propose tasks. Choose based on workspace needs:
   - "*/30 * * * *" (every 30 min) for real-time ops like social media, customer support
   - "0 */4 * * *" (every 4 hours) for active workspaces with daily goals
   - "0 9 * * *" (daily at 9am) for standard workspaces
   - "0 9 * * 1" (weekly Monday 9am) for slower-paced operations like quarterly reporting

Return ONLY valid JSON with keys: services, goals, rules, automations, evaluation, budget_policy, heartbeat_cadence.
Do not wrap in markdown fences."""


def runtime_workspace_setup_auto_model_messages(
    fields: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for workspace operating model generation."""

    return [
        {"role": "system", "content": WORKSPACE_SETUP_AUTO_MODEL_SYSTEM_PROMPT},
        {"role": "user", "content": runtime_workspace_setup_auto_model_prompt(fields)},
    ]


async def runtime_execute_workspace_setup_auto_model_completion(
    *,
    entity_id: str,
    fields: Mapping[str, Any],
    metadata: dict[str, Any] | None = None,
) -> RuntimeTextCompletionResult:
    """Execute workspace operating-model generation with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_workspace_setup_auto_model_messages(fields),
        entity_id=entity_id,
        source=RUNTIME_WORKSPACE_SETUP_SOURCE,
        temperature=0.5,
        metadata=metadata,
    )


def runtime_workspace_setup_agent_mapping_prompt(
    *,
    service_descriptions: Sequence[Mapping[str, Any]],
    agent_descriptions: Sequence[Mapping[str, Any]],
) -> str:
    """Resolve the Runtime-owned prompt for matching workspace services to agents."""

    return f"""Assign agents to workspace services. Think in TWO steps:

STEP 1 — Group services by domain:
Look at all services and group them by shared domain/platform/integration.
e.g. email_triage + email_followup + email_drafting = "email domain"
e.g. content_creation + content_scheduling + analytics = "social media domain"
Services that share the same integration or platform belong together.

STEP 2 — Assign ONE agent per group:
For each group, find the BEST single agent that covers all services in that group.
One agent handles the entire group. Multiple services get the SAME agent_id.

Services needed:
{json.dumps(list(service_descriptions), indent=2)}

Available agents (with their integrations/tools):
{json.dumps(list(agent_descriptions), indent=2)}

Assignment rules:
- One agent per domain group (NOT one agent per service)
- Match by integrations first: agent must have tools for the domain
- Entity agents (source="entity") preferred over marketplace
- create_custom ONLY when no existing agent covers that domain at all
- For create_custom: suggest ONE general agent name for the whole group
  (e.g. "Email Agent" not "Email Triage Agent" + "Email Followup Agent")

Return a JSON array with one entry per service:
- service_key: the service key
- agent_id: matched agent ID (SAME id for services in the same group)
- strategy: "entity_match" | "marketplace_match" | "create_custom"
- reason: brief explanation

Return ONLY a JSON array. No markdown fences."""


def runtime_workspace_setup_agent_mapping_messages(
    *,
    service_descriptions: Sequence[Mapping[str, Any]],
    agent_descriptions: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for workspace service-agent matching."""

    return [
        {"role": "system", "content": WORKSPACE_SETUP_AGENT_MAPPING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": runtime_workspace_setup_agent_mapping_prompt(
                service_descriptions=service_descriptions,
                agent_descriptions=agent_descriptions,
            ),
        },
    ]


async def runtime_execute_workspace_setup_agent_mapping_completion(
    *,
    entity_id: str,
    service_descriptions: Sequence[Mapping[str, Any]],
    agent_descriptions: Sequence[Mapping[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> RuntimeTextCompletionResult:
    """Execute workspace service-agent matching with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_workspace_setup_agent_mapping_messages(
            service_descriptions=service_descriptions,
            agent_descriptions=agent_descriptions,
        ),
        entity_id=entity_id,
        source=RUNTIME_WORKSPACE_SETUP_SOURCE,
        temperature=0.3,
        metadata=metadata,
    )


def runtime_agent_greeting_user_message(
    *,
    agent_name: str,
    service_key: str,
    workspace_name: str,
    workspace_kind: str,
    system_prompt: str,
    max_capability_chars: int = 300,
) -> str:
    """Build the Runtime-owned user message for setup agent greetings."""

    prompt_snippet = (system_prompt or "")[:max_capability_chars]
    return (
        f"You are {agent_name}, an AI agent assigned to the \"{service_key}\" role "
        f"in the \"{workspace_name}\" workspace ({workspace_kind or 'general'}).\n\n"
        f"Your capabilities: {prompt_snippet}\n\n"
        f"Write a friendly greeting (1-2 sentences) introducing yourself "
        f"to the workspace team. Mention your role and what you can help with. "
        f"Be concise and warm."
    )


def runtime_agent_greeting_messages(
    *,
    agent_name: str,
    service_key: str,
    workspace_name: str,
    workspace_kind: str,
    system_prompt: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for workspace setup agent greetings."""

    return [
        {"role": "system", "content": RUNTIME_AGENT_GREETING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": runtime_agent_greeting_user_message(
                agent_name=agent_name,
                service_key=service_key,
                workspace_name=workspace_name,
                workspace_kind=workspace_kind,
                system_prompt=system_prompt,
            ),
        },
    ]


async def runtime_execute_agent_greeting_completion(
    *,
    entity_id: str,
    workspace_id: str,
    agent_name: str,
    service_key: str,
    workspace_name: str,
    workspace_kind: str,
    system_prompt: str,
) -> RuntimeTextCompletionResult:
    """Execute workspace setup agent greeting generation with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_agent_greeting_messages(
            agent_name=agent_name,
            service_key=service_key,
            workspace_name=workspace_name,
            workspace_kind=workspace_kind,
            system_prompt=system_prompt,
        ),
        entity_id=entity_id,
        source=RUNTIME_AGENT_GREETING_SOURCE,
        workspace_id=workspace_id,
        temperature=0.7,
        max_tokens=150,
    )


def runtime_knowledge_starter_document_prompt(
    *,
    group_name: str,
    purpose: str,
    workspace_name: str,
    workspace_kind: str,
    primary_work: str,
) -> str:
    """Build the Runtime-owned prompt for starter knowledge base documents."""

    return (
        f"Generate a comprehensive starter document for a knowledge base.\n\n"
        f"Knowledge base: {group_name}\n"
        f"Purpose: {purpose}\n"
        f"Workspace: {workspace_name} ({workspace_kind})\n"
        f"Primary work: {primary_work}\n\n"
        f"Write a well-structured markdown document (800-1500 words) as a "
        f"useful starting point. Include sections, guidelines, examples, "
        f"templates. Be specific to the workspace context, not generic.\n\n"
        f"Output ONLY markdown content, no code fences."
    )


def runtime_knowledge_starter_document_messages(
    *,
    group_name: str,
    purpose: str,
    workspace_name: str,
    workspace_kind: str,
    primary_work: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for starter knowledge base documents."""

    return [{
        "role": "user",
        "content": runtime_knowledge_starter_document_prompt(
            group_name=group_name,
            purpose=purpose,
            workspace_name=workspace_name,
            workspace_kind=workspace_kind,
            primary_work=primary_work,
        ),
    }]


async def runtime_execute_knowledge_starter_document_completion(
    *,
    entity_id: str,
    workspace_id: str,
    group_name: str,
    purpose: str,
    workspace_name: str,
    workspace_kind: str,
    primary_work: str,
) -> RuntimeTextCompletionResult:
    """Execute starter knowledge document generation with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_knowledge_starter_document_messages(
            group_name=group_name,
            purpose=purpose,
            workspace_name=workspace_name,
            workspace_kind=workspace_kind,
            primary_work=primary_work,
        ),
        entity_id=entity_id,
        source=RUNTIME_KNOWLEDGE_GEN_SOURCE,
        workspace_id=workspace_id,
        temperature=0.7,
        max_tokens=3000,
    )
