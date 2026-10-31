from __future__ import annotations

import json
from typing import Any

from packages.core.constants.task import TaskStatus
from packages.core.ai.runtime.billing import runtime_llm_billing_context
from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
    runtime_one_shot_messages,
)
from packages.core.ai.runtime.sources import RUNTIME_STRATEGIST_SOURCE


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
  * Plan status `replanned` is terminal history, not in-flight work.
    Do not treat a `replanned`/`cancelled`/`failed`/`completed` plan as
    unfinished live work. Missing Goal current_value is a measurement
    gap, not missing materials. If scores predate newer Goal-driving work,
    explain that measurement gap; do not automatically create a scoring task.
    Only propose measurement work when evidence and the operator's direction
    justify it. Missing evidence is unknown, not an invented zero.
    That gap alone is not a human_request. Do not re-propose recently completed
    work.
    If Goals are still incomplete and there is no live work batch or
    evidenced active execution, consider the next high-impact Goal-driving task.
    A non-terminal Plan label alone is not active execution; distinguish queued
    dependencies and blocked recovery from work that is actually running.
    Agents may produce new Goal-driving artifacts or repair an existing
    not-ready artifact in place. A not-ready existing artifact is not
    leftover for the operator. Leftover for the operator is only login,
    pay, sign, send, or inventing facts only the user can supply.
    Forbidding another copy of the same artifact is not forbidding
    in-place repair of the current one.
  * If the Work batch reconciliation section lists stalled work, deal with it
    on the original Task before proposing unrelated new work. Repair, retry
    and unblock are actions on that Task, never a new Task: never propose a
    task or human_request that recovers, retries, repairs, unblocks, resumes,
    chases or verifies an open Task; such items are discarded.
    For a blocked Task worth another attempt, put its task_id in
    `retry_task_ids`; Manor retries that Task itself. A technical failure is
    the platform's to recover: never ask the operator to fix or retry it.
    waiting_configuration is a recovery request on the original Task: resolve
    its configuration review and continue that Task; do not create another
    Task requiring the same missing capability.
    A Task marked waiting_on=person is not stalled agent work. Its request
    card is already open for the operator; leave it there.
  * Do not wait for the user to send a workspace chat message.
    Chat is not the collection surface. If core facts only the user
    has are missing, emit `human_requests`. Otherwise propose
    Goal-driving tasks and keep unknowns explicit.
  * Prefer fewer high-impact tasks over many small ones.
  * Priority scale is 5=Critical, 4=High, 3=Medium, 2=Low,
    1=Minimal. Higher numbers are more urgent.
  * Learn from prior reviews: a rejection reason_code is feedback even without
    a comment. Do NOT repeat rejected work under a new title. WRONG_DIRECTION
    requires a different approach; BAD_TIMING requires changed timing or state.
    Reconsider only when new evidence or an explicit operator request addresses
    the rejection, and explain what changed in `notes`. A measurement gap does
    not override a rejection. If similar work completed successfully, lean into
    that pattern.
  * Never propose meta/bookkeeping tasks — writing or updating
    learnings, memory, LEARNINGS.md/MEMORY.md/STATE.md, "document
    what we learned", consolidating notes, or process retrospectives.
    The runtime records learnings automatically through the learning
    pipeline; pending learning candidates are for the operator to
    resolve, not tasks. If learning candidates are piling up, mention
    it in `notes`. Proposing 0 tasks is better than proposing
    bookkeeping.
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
  * Make every structured `expected_output` a precise JSON Schema, not a prose
    hint: list all mandatory fields in `required`; when the task asks for
    exactly N records, set both `minItems` and `maxItems` to N on the relevant
    array. The Task agent works to this contract and checks its result against
    it before completing, so do not omit constraints stated in the task
    description.
  * For files with a project-specific Knowledge destination, set each file
    deliverable's `document_id_path` (JSON Pointer into the Task result),
    `workspace_folder` (exact relative logical folder, no storage root), and
    `require_readback=true`. Require that Document ID in `expected_output`.
    The Task agent must report the saved Document as its deliverable; the host
    accepts a reported file only if it exists and the agent read it back, so a
    path or saved=true claim alone cannot satisfy this contract.

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


RUNTIME_STRATEGIST_EMPTY_OK = """\
Empty proposals are a first-class good outcome. When the domain reports in
the briefing are healthy and no new evidence demands action, return an empty
`tasks` array and explain the no-action reasoning in `notes` — "reviewed,
nothing needs to change" is a legitimate, valuable conclusion. Never
fabricate tasks to fill a quota; an empty proposal is strictly better than a
padded one.

Do not treat plan status `replanned` as in-flight work; it is terminal
history. Missing Goal current_value is a measurement gap, not missing
materials. A missing or stale score does not automatically require a task.
Only propose measurement work when evidence and the operator's direction
justify it. Missing evidence is unknown, not an invented zero.
A measurement gap does not override a rejection: reason_code is feedback even
without a comment. Do not repeat rejected work under a new title; reconsider
only when new evidence, changed timing/state, or an explicit operator request
addresses the rejection, and explain what changed in `notes`.
That gap alone is not a human_request. Do not re-propose
recently completed work. If Goals are still incomplete and there is no live work batch or
evidenced active execution, consider the next high-impact Goal-driving task.
A non-terminal Plan label alone is not active execution; queued work and blocked
recovery still need a decision, not an assumption that a worker is running.
Agents may produce new Goal-driving artifacts or repair an existing
not-ready artifact in place. A not-ready existing artifact is not
leftover for the operator. Leftover for the operator is only login,
pay, sign, send, or inventing facts only the user can supply.
Forbidding another copy of the same artifact is not forbidding
in-place repair of the current one. An empty proposal is valid when
remaining Goals are done, on track, or blocked on an open Need Input
item. Do not wait for a user chat message.

Do not propose high-risk changes for any domain listed under "Coverage gaps"
in the briefing: data for those domains is unavailable or stale this cycle,
so there is no evidence basis for risky changes to them."""


RUNTIME_STRATEGIST_BASIS_GUIDANCE = """\
Every task SHOULD cite its evidence basis. Add a `basis` object to each
task: set `basis.report_refs` to the briefing domain names (e.g. "goal",
"execution") or report ids the task responds to, and `basis.evidence_refs`
to the ids shown in [evidence: ...] suffixes of the observations you relied
on. A task you cannot ground in any report or evidence is a task you should
reconsider proposing. Optionally set `correlation_key` (stable snake_case)
on recurring work so duplicate proposals can be detected across cycles."""


RUNTIME_STRATEGIST_EXTERNAL_ACTION_GUIDANCE = """\
`external_action` is optional and must be omitted for ordinary tasks. The only
currently supported value is a one-use public YouTube publish scope with this
exact shape: provider=youtube, action=publish_video,
destination=studio.youtube.com, visibility=public, intended_channel set to the
channel named by the task (or paired_chrome_signed_in_channel),
predecessor_task_key naming a task in depends_on_task_keys, max_executions=1,
and expires_in_hours between 1 and 168. Such a task must include
external.social in required_capabilities. Never use this field for a different
provider, host, visibility, action, or unbounded/repeating execution."""


RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE = """\
Use the optional `human_requests` array (max 3) when the workspace needs a
HUMAN decision, review, input, or manual work that is NOT an executable work
task — e.g. a direction confirmation before committing budget, sign-off that
is blocking progress, information only the operator has, or an offline action
no agent can perform. Each entry needs: `request_key` (stable snake_case,
unique in this proposal), `request_kind` (decision | review | input |
manual_work), `question` (the concrete question or request, self-contained),
and optionally `role_required` (workspace role expected to answer),
`expected_by_hours` (soft deadline in hours), and `context`. Human requests
are created automatically without approval and appear as Need Input;
the addressee may decline. Never ask for this in workspace chat.
Never use a human_request for work an agent service could do — propose a
task instead. Never use a human_request for missing Goal current_value.
Omit the field or return [] when no
human input is needed."""


RUNTIME_STRATEGIST_EXPERIMENTS_GUIDANCE = """\
Use the optional `experiments` array (max 1) when the briefing shows repeated
failures or degraded results for a specific automation and a CONFIG change
might fix it. Prefer a bounded experiment over proposing a direct
automation_change: the experiment applies a temporary `overlay_patch` to one
target (target_kind: scheduled_job | workflow_binding — never a task), runs at
most `max_runs` runs for at most `duration_days` days, auto-rolls-back after
consecutive failures, and is evaluated deterministically against the declared
`success_metrics` (v1 metric names: success_rate, run_count — each with a
numeric `target`). Set `guardrails.max_cost` honestly (≤ $20 keeps it medium
risk). Always state the evidence basis in `hypothesis` (cite the briefing
observations that motivated it). An experiment never becomes permanent by
itself — if it wins, propose the formal change in a later cycle citing the
experiment's evaluation. Omit the field or return [] when no config
experiment is warranted."""


RUNTIME_STRATEGIST_CHANGE_GUIDANCE = """\
Three optional arrays let you change the workspace's CONFIGURATION directly:
`automation_changes` (max 3, targets scheduled_job | workflow_binding),
`workflow_changes` (max 2, targets workflow_definition | workflow_binding), and
`goal_changes` (max 2, targets goal).

Pick the right instrument:
  * **config change** — the evidence ALREADY shows what the right setting is.
    A schedule that reliably misses its window, an automation that has failed
    every run this window and should be paused, a stale binding nobody
    triggers, a goal whose target was met and should be raised, a deadline
    that has passed. You are not testing anything; you are correcting a
    setting you can justify from the briefing.
  * **experiment** — you believe a config change might help but are NOT sure.
    Run the bounded trial first; propose the formal change later, citing its
    evaluation.
  * **task** — one-off work a service performs. Never model recurring config
    drift as a task.

Every change entry needs:
  * `change_key` — stable snake_case, unique within its own array.
  * `target_kind` + `operation`. automation/workflow operations are
    create | update | pause | resume | delete; goal operations are
    create | update_target | update_deadline | pause | archive.
  * `target_id` — required for every operation except `create`; `create` must
    NOT carry a target_id or an expected_revision.
  * `expected_revision` — REQUIRED for update / pause / resume / delete /
    archive / update_target / update_deadline. Copy the `revision` number
    shown for that row in the briefing's automation_portfolio digest (for
    automations and bindings) or goal digest (for goals). If the row changed
    since the briefing was frozen, the change is rejected as STALE_REVISION
    instead of overwriting someone else's edit — so copy it exactly, never
    guess or invent a number.
  * `patch` — field-level only, and only these keys:
      - scheduled_job: enabled, cron_expr, every_seconds, schedule_kind,
        timezone, name, execution_target, payload_message
      - workflow_binding: enabled, status, trigger_type, trigger_ref,
        variables, name
      - workflow_definition: name, steps, variables, description
      - goal: target_value, deadline, baseline_value, priority,
        measurement_cadence, status
    `pause` / `resume` / `delete` / `archive` need no patch (the operation
    says everything); `update`, `update_target` and `update_deadline` need a
    non-empty one. Anything outside the lists above is rejected.
  * `rationale` (>= 16 chars) and `basis` — cite the briefing reports and
    the [evidence: ...] ids the change rests on. A change you cannot ground
    in the briefing is a change you should not propose.

Changes to a row that another open proposal already targets are rejected as
duplicates, so do not re-propose a change that is still awaiting approval.
Omit these arrays or return [] when the configuration is fine as it is."""


RUNTIME_STRATEGIST_WORKFLOW_RUN_GUIDANCE = """\
Use the optional `workflow_runs` array only for a Flow listed under Installed
Workspace Flows. Reference its exact blueprint_slug and workflow_slug. Prefer
workflow_run over decomposing an installed end-to-end Flow into ordinary tasks.
Preserve the complete request in source_brief and use only declared input keys.
Treat the briefing's Workflow runs as execution evidence:
  * Never propose a fresh run of a Flow that already has a pending, running,
    or paused run. It is work already in flight, even when it needs input.
  * Never duplicate a completed scheduled Flow from the current review window
    unless the evidence explicitly calls for an intentional rerun.
  * For a failed or blocked run, address the recorded blocker with a human
    request, configuration change, or recovery task; do not hide the failure
    by launching an indistinguishable fresh run.
  * A needs_input run is already asking a person. Do not re-ask its question
    or propose recovery work for it; mention the open request in notes.
Omit the field or return [] when no installed Flow matches."""


RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT = {
    "review_id": "<provided in user prompt — copy verbatim>",
    "summary": "One or two short sentences from Manor AI to the owner, in plain words.",
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
            "existing_task_id": "<task_id of the existing Task this builds on, or null>",
            "required_capabilities": ["workspace.search", "web.safe_search"],
            "external_action": None,
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
    "notes": "Optional private reasoning for the review record; never shown to the owner.",
    "retry_task_ids": ["<task_id of a blocked Task Manor should retry; usually []>"],
}


# v2 (briefing_mode) variant — identical to the legacy hint plus the
# non-task item kinds (human_requests / experiments / the three change
# arrays). Execution evidence is added to either hint when a snapshot is supplied.
RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT_V2 = {
    **RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT,
    "tasks": [{
        **RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT["tasks"][0],
        "required_skills": ["<bound Skill slug for owner_service_key, or omit>"],
    }],
    "human_requests": [
        {
            "request_key": "stable_snake_case_key_unique_in_this_review",
            "request_kind": "decision | review | input | manual_work",
            "role_required": "workspace role expected to answer, or null",
            "question": (
                "The concrete question or request for the human "
                "(self-contained, >= 8 chars)."
            ),
            "expected_by_hours": 48,
            "context": "Optional short context the human needs to answer.",
        }
    ],
    "experiments": [
        {
            "experiment_key": "stable_snake_case_key",
            "hypothesis": (
                "What the config change should improve and the briefing "
                "evidence it rests on (>= 16 chars)."
            ),
            "target_kind": "scheduled_job | workflow_binding",
            "target_id": "<id of the automation to experiment on>",
            "overlay_patch": {"payload_message": "temporary config override"},
            "max_runs": 5,
            "duration_days": 7,
            "success_metrics": {
                "success_rate": {"baseline": 0.6, "target": 0.9},
            },
            "guardrails": {
                "max_cost": 20,
                "rollback_on_consecutive_failures": 2,
            },
        }
    ],
    "workflow_runs": [
        {
            "run_key": "stable_snake_case_key",
            "workflow_ref": {
                "blueprint_slug": "exact installed blueprint slug",
                "workflow_slug": "exact installed Flow slug",
            },
            "inputs": {"declared_input_key": "value"},
            "source_brief": "Complete request and constraints for the Flow.",
            "rationale": "Why this installed Flow is the right execution path.",
            "basis": {"report_refs": [], "evidence_refs": []},
        }
    ],
    "automation_changes": [
        {
            "change_key": "stable_snake_case_key",
            "target_kind": "scheduled_job | workflow_binding",
            "operation": "create | update | pause | resume | delete",
            "target_id": "<row id; omit only for create>",
            "expected_revision": 3,
            "patch": {"cron_expr": "0 9 * * 1"},
            "rationale": (
                "Why the evidence already settles this (>= 16 chars)."
            ),
            "basis": {"report_refs": ["automation_portfolio"], "evidence_refs": []},
        }
    ],
    "workflow_changes": [
        {
            "change_key": "stable_snake_case_key",
            "target_kind": "workflow_definition | workflow_binding",
            "operation": "create | update | pause | resume | delete",
            "target_id": "<row id; omit only for create>",
            "expected_revision": 1,
            "patch": {"enabled": False},
            "rationale": "Why this workflow config must change (>= 16 chars).",
            "basis": {"report_refs": ["execution"], "evidence_refs": []},
        }
    ],
    "goal_changes": [
        {
            "change_key": "stable_snake_case_key",
            "target_kind": "goal",
            "operation": "create | update_target | update_deadline | pause | archive",
            "target_id": "<goal id; omit only for create>",
            "expected_revision": 2,
            "patch": {"target_value": 2000},
            "rationale": "Why the goal itself should change (>= 16 chars).",
            "basis": {"report_refs": ["goal"], "evidence_refs": []},
        }
    ],
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
    user_id: str | None = None,
) -> RuntimeTextCompletionResult:
    """Execute a Strategist review completion with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_one_shot_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ),
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        source=RUNTIME_STRATEGIST_SOURCE,
        max_tokens=4096,
    )


def runtime_strategist_system_prompt(
    ctx: Any,
    *,
    preamble: str | None = None,
    briefing_mode: bool = False,
) -> str:
    """Build the stable Strategist system prompt for a review cycle.

    ``briefing_mode=True`` (strategist_review_v2 path only) appends the
    empty-proposal legitimization + coverage-gap restriction paragraph.
    Both paths retain the same execution evidence contract.
    """

    services_block = runtime_strategist_services_block(ctx)
    capabilities_block = runtime_strategist_task_capabilities_block(ctx)
    hint = dict(RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT_V2 if briefing_mode else RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT)
    if getattr(ctx, "execution_snapshot", None):
        hint["execution_review"] = {
            "revision": "<copy the execution snapshot revision exactly>",
            "references": [{
                "task_id": "<exact task_id>", "plan_id": "<its plan_id or null>",
                "observed_state": "<copy its observed_state exactly>",
                "blocker_ref": "<its current_blocker.ref or null>",
            }],
        }
    schema_hint = json.dumps(hint, indent=2)

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
        "Available task runtime capabilities (resolved requirements, never tool grants):\n"
        f"{capabilities_block}"
    )
    parts.append(
        "Assign each task only to owner/delegate services whose resolved capability_ids "
        "cover its required_capabilities. A capability on another service is not inherited. "
        "Unknown or unavailable scope is not proof of access; propose a configuration "
        "request instead of retrying unchanged work. Resolved scope does not prove live "
        "provider health and does not bypass operation approval."
    )
    parts.append(
        "Every task MUST list at least one deliverable in `deliverables`. "
        "For each deliverable pick a `shape` from this exact vocabulary: "
        "ArtifactResult, TextResult, DocumentResult, ListResult, "
        "PublishResult, CountResult, DraftPack."
    )
    if briefing_mode:
        parts.append(
            "If a task must run a bound Skill, list its exact owner-service slug in "
            "`required_skills`; the Task agent is told to run it with invoke_skill. "
            "Do not name a Skill unavailable to that owner service. "
            "Check its required inputs before proposing execution. Cite existing canonical "
            "inputs, or propose the missing upstream deliverable and declare depends_on_task_keys. "
            "If that prerequisite cannot fit this cohort, defer its consumer to the next review. "
            "Missing internal artifacts call for upstream work, not a user questionnaire. "
            "Retry a blocked Skill only after the named prerequisite or capability has changed."
        )
    parts.append(RUNTIME_STRATEGIST_EXTERNAL_ACTION_GUIDANCE)
    parts.append(
        "Current execution evidence is a server observation at observed_at, not a tool grant. "
        "It outranks historical plans, reports, proposal outcomes and memory for execution state. "
        "When a snapshot is supplied, return execution_review with its exact revision, and "
        "reference every existing Task whose execution or blocker informs your decision (max 8; "
        "use [] if none). Copy task_id, plan_id and observed_state from the SAME observation. "
        "A blocker_ref must belong to that Task, never another Task or Plan. "
        "pending/not_started means queued, NOT running; a raw running Plan is not active-worker proof. "
        "Completed Steps do not prove accepted delivery. Old failures describe past attempts, "
        "not current provider health. configuration_changed requires recheck, not another claim "
        "of missing access. unavailable means unknown. A business capability ID is not a setting "
        "or credential, and access on another Agent is not inherited. "
        "Keep summary/notes about your proposed strategy; do not rewrite execution status or "
        "diagnose blockers in free prose. The server records referenced execution facts separately. "
        "These observations do not dispatch, retry, repair, approve or close Tasks."
    )
    parts.append(
        "A Task with waiting_on_human=true or waiting_on=person is waiting on a person, and its "
        "request card is already open for them. Only their answer unblocks it; no agent can. "
        "Never propose a task or human_request that recovers, chases, relays, re-asks, verifies "
        "or follows up that Task. "
        "Leave it. Repair, retry and unblock of any open Task (in progress, blocked, on hold or "
        "waiting) happen on that Task, never as a new Task: a task or human_request that "
        "recovers, retries, repairs, unblocks, resumes or verifies an open Task is discarded. "
        "To retry a blocked Task, list its exact task_id in retry_task_ids; Manor retries that "
        "Task itself. Never ask the owner to fix, retry or work around a technical failure. "
        "Set existing_task_id when a proposed task builds on an "
        "existing Task."
    )
    parts.append(
        "`summary` is the only text the workspace owner reads. Write it as Manor AI talking to "
        "the owner (\"I\" and \"you\"), in one or two short sentences of plain language, in the "
        "owner's language. Say what you propose and how it helps. When you propose nothing, say "
        "so in one short friendly line. Never use internal terms such as review cycle, briefing, "
        "execution evidence, Task acceptance, measurement gap or recovery pass, and do not explain "
        "what you chose not to do. Requests already waiting on the owner have their own cards; do "
        "not repeat them. `notes` is private reasoning for the review record and is never shown "
        "to the owner."
    )
    if briefing_mode:
        parts.append(RUNTIME_STRATEGIST_EMPTY_OK)
        parts.append(RUNTIME_STRATEGIST_BASIS_GUIDANCE)
        parts.append(RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE)
        parts.append(RUNTIME_STRATEGIST_EXPERIMENTS_GUIDANCE)
        parts.append(RUNTIME_STRATEGIST_CHANGE_GUIDANCE)
        parts.append(RUNTIME_STRATEGIST_WORKFLOW_RUN_GUIDANCE)
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
        primary_signal = (
            business_model.get("primary_signal")
            or business_model.get("primary_metric")
        )
        if primary_signal:
            lines.append(f"- Primary signal: {primary_signal}")
        secondary = (
            business_model.get("secondary_signals")
            or business_model.get("secondary_metrics")
            or []
        )
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

    priors = tpl.get("priors") or []
    if isinstance(priors, (list, tuple)) and priors:
        if lines:
            lines.append("")
        lines.append("# Workspace-specific proposal requirements - hard constraints")
        for item in priors:
            if str(item).strip():
                lines.append(f"- {str(item).strip()}")

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
    workspace_settings = getattr(getattr(ctx, "workspace", None), "settings", None)
    personalization = (
        workspace_settings.get("blueprint_personalization")
        if isinstance(workspace_settings, dict)
        else None
    )
    if isinstance(personalization, dict) and personalization:
        sections.append(
            "# Blueprint personalization\n"
            + json.dumps(
                personalization,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )[:4000]
        )
    readiness_section = _format_workspace_readiness(ctx)
    if readiness_section:
        sections.append("# Workspace readiness\n" + readiness_section)

    sections.extend(_missing_setup_sections(ctx))

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
    failure_evidence = _format_failed_task_evidence(ctx)
    if failure_evidence:
        sections.append("# Task failure evidence\n" + failure_evidence)
    sections.append("# Recent plans\n" + (_format_plans(ctx) or "_(none)_"))
    reconciliation_section = _format_work_batch_reconciliation(ctx)
    if reconciliation_section:
        sections.append("# Work batch reconciliation\n" + reconciliation_section)
    sections.append("# Recent workspace activity\n" + (_format_activity(ctx) or "_(none)_"))
    declined_section = _format_declined_human_requests(ctx)
    if declined_section:
        sections.append("# Declined human requests\n" + declined_section)
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

    sections.extend(_execution_snapshot_sections(ctx))
    sections.append(
        "Now produce the JSON Proposal. review_id MUST equal "
        f"`{review_id}` exactly."
    )
    return "\n\n".join(sections)


def runtime_strategist_user_prompt_v2(
    briefing_markdown: str,
    ctx: Any,
    *,
    review_id: str,
) -> str:
    """Briefing-driven review user prompt (strategist_review_v2 path, M6).

    The deterministic ReviewBriefing markdown is the data core. Only the
    cheap operational sections survive from the legacy prompt: workspace
    readiness / missing setup, the open-proposals dedupe list, and
    operating + relevant memory. Everything else (recent tasks, plans,
    activity, outcomes, calibration, evaluation scorecard) is superseded by
    the briefing's domain reports.
    """
    sections = [f"# Review trigger\n{ctx.trigger}\n\nreview_id to use: `{review_id}`"]

    readiness_section = _format_workspace_readiness(ctx)
    if readiness_section:
        sections.append("# Workspace readiness\n" + readiness_section)

    sections.extend(_missing_setup_sections(ctx))

    declined_section = _format_declined_human_requests(ctx)
    if declined_section:
        sections.append("# Declined human requests\n" + declined_section)

    if getattr(ctx, "open_proposed_tasks", None):
        sections.append(
            "# Already-proposed tasks not yet approved\n" + _format_open_proposed(ctx)
        )

    installed_flows = getattr(ctx, "installed_flows", None) or []
    if installed_flows:
        sections.append(
            "# Installed Workspace Flows\n"
            + json.dumps(installed_flows, ensure_ascii=False, indent=2)
        )

    if getattr(ctx, "operating_memory", ""):
        sections.append(
            "# Workspace operating memory (canonical docs)\n" + ctx.operating_memory
        )
    sections.append(
        "# Workspace memory (most relevant)\n"
        + (_format_memory(ctx) or "_(none indexed yet)_")
    )

    sections.append(
        "# Review briefing (deterministic domain reports)\n"
        + briefing_markdown.strip()
    )
    failure_evidence = _format_failed_task_evidence(ctx)
    if failure_evidence:
        sections.append("# Task failure evidence\n" + failure_evidence)

    sections.extend(_execution_snapshot_sections(ctx))
    sections.append(
        "Now produce the JSON Proposal. review_id MUST equal "
        f"`{review_id}` exactly."
    )
    return "\n\n".join(sections)


def _execution_snapshot_sections(ctx: Any) -> list[str]:
    snapshot = getattr(ctx, "execution_snapshot", None)
    if not snapshot:
        return []
    return ["# Current execution evidence (authoritative as of observed_at)\n"
            + json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)]


def _missing_setup_sections(ctx: Any) -> list[str]:
    """Shared "setup incomplete" sections (legacy + v2 user prompts)."""
    sections: list[str] = []
    if ctx.missing_setup:
        missing_labels = {
            "no_agents": "No agents are mapped to services yet",
            "no_goals": "No goals are defined yet",
            "no_channels": "Required workspace channel declarations are not configured yet",
            "no_integrations": "No external integrations are configured yet",
            "blocking_setup_incomplete": "Blueprint-defined blocking Workspace setup is incomplete",
        }
        missing_text = "\n".join(
            f"- {missing_labels.get(item, item)}" for item in ctx.missing_setup
        )
        sections.append(
            "# Workspace setup incomplete\n"
            f"The following are NOT ready:\n{missing_text}\n\n"
            "DO NOT propose Tasks while blocking setup is incomplete. "
            "Automated setup runs only through the Blueprint's authorized setup job. "
            "Use a human request for an operator configuration step, or propose "
            "0 items with a note explaining what must be configured first."
        )
        missing_channels = getattr(ctx, "missing_channel_requirements", []) or []
        if missing_channels:
            sections.append("# Missing channel requirements\n" + _format_missing_channels(missing_channels))
        blocking_setup = _blocking_setup_prompt_details(ctx)
        if blocking_setup:
            sections.append(blocking_setup)
    return sections


def _blocking_setup_prompt_details(ctx: Any) -> str:
    readiness = getattr(ctx, "workspace_readiness", None)
    if not isinstance(readiness, dict):
        return ""
    part = next((
        item
        for item in readiness.get("parts") or []
        if isinstance(item, dict) and item.get("key") == "blocking_setup"
    ), None)
    if not isinstance(part, dict) or part.get("status") != "missing":
        return ""
    details = part.get("details") if isinstance(part.get("details"), dict) else {}
    incomplete_lines = []
    for result in details.get("incomplete_checks") or []:
        if not isinstance(result, dict):
            continue
        key = result.get("key") or "setup"
        reason = result.get("reason") or "not ready"
        incomplete_lines.append(f"- {key}: {reason}")
    allowed = [
        str(value or "").strip()
        for value in details.get("allowed_setup_task_keys") or []
        if str(value or "").strip()
    ]
    lines = [
        "# Blocking setup gate (authoritative)",
        *(incomplete_lines or ["- Blocking setup is not ready."]),
        "Until every check is ready, do not propose any Tasks, workflow_runs, experiments, "
        "or configuration changes. Automated setup is owned by the authorized setup job.",
    ]
    if allowed:
        lines.append(
            "You may emit only human requests whose request_key is exactly one of: "
            + ", ".join(allowed)
            + "."
        )
    else:
        lines.append("Propose zero work items and explain the operator blocker in notes.")
    lines.append("Never attach an external_action to a setup proposal.")
    return "\n".join(lines)


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
        bound_skills = (getattr(ctx, "bound_skills_by_service", None) or {}).get(subscription.service_key, [])
        skill_note = f", bound_skills={','.join(bound_skills)}" if bound_skills else ""
        lines.append(
            f"  - {subscription.service_key} ({kind} · {horizon}, agent={agent_name}{skill_note})"
        )
        contracts = (getattr(ctx, "bound_skill_contracts_by_service", None) or {}).get(subscription.service_key, [])
        for contract in contracts:
            lines.append("    Skill contract: " + json.dumps(contract, ensure_ascii=False))
        scope = (getattr(ctx, "service_capabilities", None) or {}).get(subscription.service_key)
        if scope is None:
            lines.append('    {"scope_status":"unknown","capability_ids":[]}')
        else:
            lines.append("    " + json.dumps(scope.prompt_dict(), ensure_ascii=False))
    return "\n".join(lines) or "  (no services available — propose 0 tasks)"


def runtime_strategist_task_capabilities_block(ctx: Any = None) -> str:
    """Describe only capabilities present on this review's scheduled services."""

    lines: list[str] = []
    capabilities = {}
    for key, scope in (getattr(ctx, "service_capabilities", None) or {}).items():
        if key in getattr(ctx, "allowed_service_keys", ()) and scope.available:
            for capability in scope.capabilities:
                capabilities.setdefault(capability.capability_id, capability)
    for capability in sorted(capabilities.values(), key=lambda entry: entry.capability_id):
        approval = "approval-gated" if capability.required_approval else "runtime-governed"
        lines.append(
            f"  - {capability.capability_id} ({capability.risk_level}, {approval}): "
            f"{capability.description[:220]}"
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
    from packages.core.governance.approval_projection import approval_summary

    lines = approval_summary(policy)
    if policy.get("max_risk_level"):
        lines.append(f"Max risk level: {policy['max_risk_level']}")
    return "\n".join(lines)


def _format_failed_task_evidence(ctx: Any) -> str:
    """Keep concrete blockers visible even when the briefing compresses outcomes."""
    failures = []
    for task in getattr(ctx, "recent_tasks", []) or []:
        if task.status != TaskStatus.FAILED:
            continue
        output = task.actual_output if isinstance(task.actual_output, dict) else {}
        errors = [
            {"type": str(step["error"].get("type") or "")[:100],
             "message": str(step["error"].get("message") or "")[:1200]}
            for step in (output.get("steps") or [])
            if isinstance(step, dict) and isinstance(step.get("error"), dict)
        ][:3]
        failures.append({
            "task_id": task.id, "title": str(task.title)[:200],
            "supervisor_evidence": str(output.get("supervisor_evidence") or "")[:600],
            "errors": errors,
        })
        if len(failures) >= 5:
            break
    return json.dumps(failures, ensure_ascii=False) if failures else ""


def _format_tasks(ctx: Any) -> str:
    lines = []
    for task in ctx.recent_tasks:
        bits = [f"- [{task.status}] {task.title} task_id={getattr(task, 'id', 'unknown')}"]
        if task.owner_service_key:
            bits.append(f"(owner={task.owner_service_key})")
        if task.completed_at:
            bits.append(f"completed={task.completed_at.date().isoformat()}")
        output = task.actual_output or {}
        files = _output_artifact_refs(output)
        if files:
            bits.append(f"artifacts={len(files)}")
        elif task.status == TaskStatus.COMPLETED and _looks_artifact_task(task):
            bits.append("artifacts=0 (text-only; do not describe as files/artifacts)")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _format_plans(ctx: Any) -> str:
    from packages.core.constants.execution import PLAN_TERMINAL_STATUSES

    task_status_by_id = {
        str(getattr(task, "id", "") or ""): str(getattr(task, "status", "") or "")
        for task in getattr(ctx, "recent_tasks", []) or []
        if getattr(task, "id", None)
    }
    terminal_plan = {status.value for status in PLAN_TERMINAL_STATUSES}
    observations = {
        row["task_id"]: row
        for row in (getattr(ctx, "execution_snapshot", None) or {}).get("tasks", [])
    }
    live_lines: list[str] = []
    inactive_lines: list[str] = []
    history_lines: list[str] = []
    for plan in ctx.recent_plans:
        cost = (plan.cost_tracking or {}).get("usd")
        cost_part = f" cost=${cost:.3f}" if cost else ""
        plan_status = str(getattr(plan, "status", "") or "")
        task_id = str(getattr(plan, "task_id", None) or "none")
        fact = observations.get(task_id, {})
        task_status = fact.get("task_status") or task_status_by_id.get(task_id, "unknown")
        observed = fact.get("observed_state", "unknown") if fact.get("plan_id") == plan.id else "unknown"
        is_live = plan_status not in terminal_plan and observed in {"running", "planning"}
        history = " (terminal history)" if plan_status in terminal_plan else ""
        line = (
            f"- {plan.id} status={plan_status}{history} "
            f"task={task_id} task_status={task_status} "
            f"observed_state={observed} mode={plan.execution_mode}{cost_part}"
        )
        (history_lines if history else live_lines if is_live else inactive_lines).append(line)
    if not live_lines and not history_lines and not inactive_lines:
        return ""
    parts = []
    if live_lines:
        parts.append("Live plans:")
        parts.extend(live_lines)
    else:
        parts.append("Live plans: none. `replanned` is terminal history, not in-flight work.")
    if history_lines:
        parts.append("Historical plans (not in-flight):")
        parts.extend(history_lines)
    if inactive_lines:
        parts.append("Other recorded plans (not proof of active execution):")
        parts.extend(inactive_lines)
    return "\n".join(parts)


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
            waiting = (
                " waiting_on=person (request card already open; not agent work)"
                if task.get("waiting_on_human") else ""
            )
            lines.append(
                f"  - [{task_status}] {title} task_id={task.get('task_id')} "
                f"owner={owner} age_hours={age}{waiting}"
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


def _format_declined_human_requests(ctx: Any) -> str:
    questions = getattr(ctx, "declined_human_requests", None) or []
    if not questions:
        return ""
    return (
        "These inputs were declined or skipped. Do not repeat the same request "
        "solely because its answer is absent. Check actual readiness and continue "
        "independent work; keep genuinely required missing inputs blocked.\n"
        + "\n".join(f"- [declined] {question}" for question in questions)
    )


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
    "pending": "Queued / not started (not running)",
    "blocked": "Blocked / paused (not running)",
    "in_progress": "Recorded in_progress (consult execution evidence for activity)",
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
    for bucket in ("completed", "rejected", "abandoned", "pending", "blocked", "in_progress"):
        rows = ctx.recent_proposal_outcomes.get(bucket) or []
        if not rows:
            continue
        lines = [f"## {_OUTCOME_LABELS[bucket]}"]
        for task in rows:
            owner = task.owner_service_key or "?"
            details = task.details or {}
            output = task.actual_output or {}
            artifacts = _output_artifact_refs(output)

            line = (f"- [{owner}] {task.title} task_id={getattr(task, 'id', 'unknown')} "
                    f"status={getattr(task, 'status', 'unknown')}")

            reason = details.get("rejection_reason")
            reason_code = details.get("rejection_reason_code")
            if bucket == "rejected" and reason_code:
                line += f"\n  Reason code: {str(reason_code)[:64]}"
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
