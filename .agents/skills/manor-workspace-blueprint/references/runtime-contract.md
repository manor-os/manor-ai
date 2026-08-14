# Workspace Blueprint Runtime Contract

This is the review matrix for every Blueprint. It describes what must be
portable, what is currently supported by the v1.1 pipeline, and what must not
be mistaken for portable configuration.

This reference covers only the `WS-14` portability boundary. Apply
`manor-workspace-architecture` first for the owning runtime node, end-to-end
flow, state machine, artifact, approval, credit, and lifecycle implications.

## Lifecycle parity

The source and installed Workspace must have equivalent runtime-projected
values after normalizing generated IDs and install metadata. Some Blueprint
fields are one-shot install declarations and cannot be reconstructed from the
installed Workspace; verify those through install preview, todos, and check
results instead of feeding them into the runtime round-trip comparison.

| Area | Portable contract | Current v1.1 path |
| --- | --- | --- |
| Workspace shell | kind, operating context, primary work, operating model, safe settings | Exported from Workspace; installed into Workspace columns/settings |
| Agents | the executable agent definition: prompt/config, tool bindings, and external requirements | Exported and installed when embedded or declared as a requirement |
| Subscriptions | a Workspace deployment binding from a service key to an Agent; it is not a user account, Task owner, or provider credential | Exported and installed by service key/agent slug; source subscription IDs are replaced |
| Skills | embedded skill content and external skill requirements | Exported and installed |
| Knowledge | safe packs, folders, optional inline Markdown | Exported and installed |
| Workflow | trigger, variables, ordered graph, step config, binding config | Exported and installed; compare graph exactly |
| Automation | scheduled jobs, workflow triggers, goal/stat install schedules | Scheduled jobs and workflow bindings are installed; verify target resolution |
| Goal/stat | definition, metric, cadence, baseline, collection config | Exported and installed |
| Task configuration | entity-level categories, SLA policies, and escalation rules | Not part of a Workspace Blueprint install/export. Configure them through the entity task-policy admin surface; Proposal-generated Tasks remain runtime-created |
| Governance | never-allow, HITL, auto-approve, risk and budget limits | Exported and installed through policy preset |
| Integrations | channel/session requirements without credentials | Exported as requirements and surfaced as install todos |
| Deliverables | the output contract of a Proposal Task or Workflow: artifact type/format, producer step, dependency, and completion evidence | Must be encoded in `expected_output`, Plan/Workflow step output contracts, and artifact projection; prose alone never creates a deliverable |
| Install-only declarations | prerequisites and verification instructions evaluated during install: variables, unresolved channel/session requirements, post-install checks, and expected baseline | Used by install preview/todos/check results. They are not persisted as runtime behavior and are not reconstructed by `export_workspace()` |

`contract.variables` currently describes install settings in the Blueprint
preview. The install API has no variable-values input and performs no template
substitution, so a variable must not be described as applied configuration.
Built-in declarations that are not referenced are descriptive only. Adding
real substitution requires an explicit values API, validation, persistence,
and round-trip rules.

## Task taxonomy

Use these distinctions when reviewing a Blueprint:

- **Task configuration**: entity-level category, SLA policy, and escalation
  rows managed outside the Workspace Blueprint. These do not define what a
  Proposal will propose and are not portable here.
- **Task instance**: a user's concrete task, its status, plan, artifacts, and
  owner. This is runtime state and is not portable by default.
- **Task execution state**: task logs, plan steps, leases, retries, token
  usage, agent executions, and workflow runs. Never copy it into a Blueprint.

Task categories, SLA policies, and escalation rules are entity-scoped database
resources (they do not have a `workspace_id` column). They are therefore not
safe Workspace Blueprint content: installing one Blueprint could overwrite
another Workspace's shared policy, and several source fields do not have a
lossless Blueprint mapping. Keep these sections empty and configure shared task
policy through the entity-level administration path. A condition-only
`recipe.escalation_rules` entry is rejected if it claims an SLA reference; use
`operating_model.blueprint_governance_rules` for descriptive guidance and
`policy.governance` for enforced allow/deny/HITL behavior.
`recipe.task_templates` must be empty/absent: TaskTemplate is a separate
manual/legacy task-generation feature and is not the source of Proposal Tasks.
`recipe.prompts` is workspace operating guidance, not a task template. It is
stored under `operating_model.blueprint_prompts` and exported again. Every
prompt entry must be an object; malformed entries fail installation instead of
being silently discarded.

## Workflow contract

For every workflow compare:

- stable portable slug;
- trigger type, trigger reference, variables and defaults;
- every step ID exactly once;
- step type/kind, config, tool/service ownership, and output contract;
- dependency edges and execution order;
- binding configuration and enabled/status behavior;
- scheduled jobs that target the workflow.

Generated workflow IDs and Workspace binding IDs may differ. Graph semantics,
variables, target slug, and binding behavior may not.

## Automation contract

Count all automation entry points, not just `recipe.scheduled_jobs`:

- scheduled agent jobs;
- scheduled workflow jobs;
- event/manual workflow triggers and their bindings;
- goal measurement schedules;
- stat collection cadence;
- Proposal/Strategist review cadence and any scheduled workflow/agent jobs.
  A recurring TaskTemplate is not a Workspace Blueprint automation entry.

Recurring TaskTemplate jobs are outside the Workspace Blueprint automation
contract. Use a Proposal cadence, scheduled agent job, or workflow trigger
when the Workspace should create business-context-dependent Tasks.

For each entry, verify schedule/timezone, enabled state, target type, target
slug/service, payload/input, delivery mode, and governance boundary. Exclude
last-run timestamps, run history, errors, counters, and generated IDs.

## Install modes and upgrade boundaries

Simulation may add sandbox and simulation-experience metadata; live install may
not retain those simulation-only fields. Compare the common semantic contract,
not these mode-specific fields. Blueprint upgrades must preserve Workspace edits
and only update content proven to be blueprint-owned; reverts restore the last
explicit upgrade restore point.
