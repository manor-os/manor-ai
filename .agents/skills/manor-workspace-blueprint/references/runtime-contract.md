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
fields are setup declarations rather than runtime rows; verify those through
install/upgrade preview, persisted provenance, todos, and check results instead
of feeding them into the runtime round-trip comparison.

| Area | Portable contract | Current v1.1 path |
| --- | --- | --- |
| Workspace shell | kind, operating context, primary work, operating model, safe settings | Exported from Workspace; installed into Workspace columns/settings |
| Agents | the executable agent definition: prompt/config, tool bindings, and external requirements | Exported and installed when embedded or declared as a requirement |
| Subscriptions | a Workspace deployment binding from a service key to an Agent; it is not a user account, Task owner, or provider credential | Exported and installed by service key/agent slug; source subscription IDs are replaced |
| Skills | embedded skill content and external skill requirements | Exported and installed |
| Knowledge | safe packs, folders, optional inline Markdown | Exported and installed |
| Workflow | trigger, variables, ordered graph, step config, binding config | Exported and installed; compare graph exactly |
| Automation | scheduled jobs, workflow triggers, goal/stat install schedules | Enabled and disabled scheduled definitions are exported; installed jobs follow the installer activation policy and must resolve portable targets |
| Goal/stat | definition, metric, cadence, baseline, collection config | Exported and installed |
| Task configuration | entity-level categories, SLA policies, and escalation rules | Not part of a Workspace Blueprint install/export. Configure them through the entity task-policy admin surface; Proposal-generated Tasks remain runtime-created |
| Governance | never-allow, HITL, auto-approve, risk and budget limits | Exported and installed through policy preset |
| Integrations | channel/session requirements without credentials | Exported as requirements; required external channels are selected from accessible accounts and bound in the Workspace creation transaction, while sessions remain live preflight requirements |
| Deliverables | the output contract of a Proposal Task or Workflow: artifact type/format, producer step, dependency, and completion evidence | Must be encoded in `expected_output`, Plan/Workflow step output contracts, and artifact projection; prose alone never creates a deliverable |
| Setup declarations | prerequisites and verification instructions evaluated during install: variables, channel/session requirements, post-install checks, and expected baseline | Variable values are validated and substituted before materialization and again when a later version is reviewed; install failures persist as todo results while every required declaration persists separately for live readiness. Runtime rows alone cannot reconstruct these declarations. |

Required setup declarations are live guards, not install-screen warnings.
Passing installation does not erase them. Readiness
must re-evaluate the exact Agent identity plus worker binding, enabled scheduled
job, active Workflow binding/definition, and declared MCP binding fields. An
optional MCP requirement may remain visible as guidance but must not activate
the normal-work gate. Credential-shaped MCP setup fields belong to the
integration/credential flow; only safe allowlisted fields may be required in an
`AgentMCPBinding.config_override`.

While any required setup check is incomplete, scheduler dispatch may run only
the matching Workspace-scoped setup job, Strategist persistence may retain only
explicitly allowlisted human setup requests, and Planner must reject every
Task. A model-authored Task key is never setup authorization. Prompt
instructions are not a substitute for these runtime checks.

Legacy installs that predate the durable live contract fail closed rather
than falling back to a cleared install-time todo list. A reviewed Blueprint
upgrade/re-sync rebuilds that contract from the pinned payload and installed
local identities before normal work can resume.

`contract.variables` is an install and version-upgrade personalization
contract. The APIs accept `variable_values`, apply declared defaults, reject
missing required or unknown values, and substitute only declared
`{{variable_key}}` references before materialization. A non-null default
defines the variable's JSON kind; the server rejects an override with a
different kind even when a client omits its own validation. Values marked
`materialize` are retained in `Workspace.settings.blueprint_personalization`
outside Blueprint provenance and reused when planning later versions; other
values must be supplied again. Upgrade fingerprints and compare-and-swap use
the raw published payload, while component diffs and explicit conflict choices
use the resolved payload. A newly required value blocks planning/application
until supplied. Variable keys must not be credential-shaped, because
Blueprint variables are not a secret-input channel. The installed record also
retains a fingerprint of resolved portable content outside the surgical upgrade
boundary, so changing a saved value cannot mark an unchanged Workspace shell or
Knowledge body fully synchronized.

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

For each entry, verify schedule/timezone, install-time activation policy, target
type, target slug/service, payload/input, delivery mode, and governance boundary.
A scheduled Skill must use an embedded component key or exact Marketplace
identity; its source Entity Skill ID and copied prompt are not portable. Exclude
last-run timestamps, run history, generation clocks/errors/attempt counters,
and generated IDs.

## Install modes and upgrade boundaries

Simulation may add sandbox and simulation-experience metadata; live install may
not retain those simulation-only fields. Compare the common semantic contract,
not these mode-specific fields. Blueprint upgrades must preserve Workspace edits
and only update content proven to be blueprint-owned; reverts restore the last
explicit upgrade restore point. When other portable configuration changed outside
the surgical upgrader's scope, safe component updates may apply as a partial
upgrade, but the installed Blueprint version and fingerprints remain at the last
fully synchronized baseline until that configuration is reconciled.
Upgrade preview and apply resolve current variable declarations against saved
materialized personalization plus explicit operator overrides. The reviewed
concurrency fingerprint remains the raw Marketplace payload fingerprint; the
materialized payload is used only to preview and apply runtime content.
