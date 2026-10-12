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
| Usage guide | optional creator-authored instructions for the installed Workspace | Exported in `manifest.usage_guide`; installed as a Workspace-local snapshot under `settings._blueprint.usage_guide`. It opens after installation and remains available from the header only when the author provides text. It is presentation copy and does not change runtime execution. |
| Agents | the executable agent definition: prompt/config, tool bindings, and external requirements | Exported and installed when embedded or declared as a requirement |
| Subscriptions | a Workspace deployment binding from a service key to an Agent; it is not a user account, Task owner, or provider credential | Exported and installed by service key/agent slug; source subscription IDs are replaced |
| Skills | embedded skill content and external skill requirements | Exported and installed |
| Knowledge | safe packs, relative folder declarations, optional inline Markdown/CSV | Actual starters install into parent folders with basename names and, with filesystem storage enabled, native readable files; declarations alone do not create empty folders |
| Workflow | trigger, variables, ordered graph, step config, binding config, chat projection and Skill dependencies | Exported and installed; nested Skill arguments resolve by embedded component or exact Marketplace identity before resource locks; compare graph exactly |
| Automation | scheduled jobs, workflow triggers, goal/stat install schedules | Enabled and disabled scheduled definitions are exported; installed jobs follow the installer activation policy and must resolve portable targets |
| Goal/stat | definition, metric, cadence, baseline, collection config | Exported and installed |
| Task configuration | entity-level categories, SLA policies, and escalation rules | Not part of a Workspace Blueprint install/export. Configure them through the entity task-policy admin surface; Proposal-generated Tasks remain runtime-created |
| Governance | source rules, HITL, auto-approve, risk and budget limits; explicit hard-block opt-in | Exported configuration is materialized through the preset and approval-first default projection |
| Integrations | channel/session requirements without credentials | Exported as requirements; required external channels are selected from accessible accounts and bound in the Workspace creation transaction, while sessions remain live preflight requirements |
| Deliverables | the output contract of a Proposal Task or Workflow: artifact type/format, producer step, dependency, and completion evidence | Must be encoded in `expected_output`, Plan/Workflow step output contracts, and artifact projection; prose alone never creates a deliverable |
| Setup declarations | prerequisites and verification instructions evaluated during install: variables, channel/session requirements, post-install checks, and expected baseline | Variable values are validated and substituted before materialization and again when a later version is reviewed; install failures persist as todo results while every required declaration persists separately for live readiness. Runtime rows alone cannot reconstruct these declarations. |

System/author defaults must not prohibit operations. Install, Blueprint-to-Draft
and reviewed upgrade normalize both source rules and policy: hard scopes become
approval requirements, lower default risk ceilings become review, and mixed
author auto-grants become review rules to prevent overlap or later rebuild from
bypassing that gate. Safe reviews every action. Published payloads and their
fingerprints remain unchanged; re-export describes the installed projection.
Explicit operator hard-block opt-in and upgrade conflict choices remain intact.
Existing live manual policy is not globally rewritten. Exact-rule draft
confirmation receipts and unattended-run consent are runtime state, never
portable author authority. Access, setup, lifecycle and financial gates are
independent of this default policy projection.

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
explicitly allowlisted human setup requests, and Planner, Executor and
Dispatcher must reject ordinary Tasks. Only the exact child
of a running, Blueprint-declared setup Automation may proceed after verifying
the protected setup fingerprint, current job revision, active Agent binding
and server-owned ScheduledJobRun child receipt; ordinary approval, credits
and artifact contracts still apply. A model-authored Task key is never setup
authorization. Prompt
instructions are not a substitute for these runtime checks.

Legacy installs that predate the durable live contract fail closed rather
than falling back to a cleared install-time todo list. A reviewed Blueprint
upgrade/re-sync rebuilds that contract from the pinned payload and installed
local identities before normal work can resume.

Exact Skill requirements retain both Marketplace source and ID. Recovery and
live readiness share one read-only matcher over active bindings and eligible
same-account or public platform Skills. Typed same-account `installed_from`
links and the exporter's established `marketplace_id`/`source_skill_id` fields
must agree; conflicting links or multiple matching local rows remain blocked.
A platform copy's explicit `source=marketplace` and `source_skill_id` identify
its current source; inherited upstream Marketplace hints are ancestry only.
An existing `installed_skill_id` never bypasses exact source validation. Older
receipts without a source can recover only a unique bound provenance identity
for the exact ID, without inferring a source from ID or slug syntax. Built-in
references still require the trusted packaged identity. These checks do not
install Skills, change bindings, or grant runtime authority.

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

Knowledge starter identity uses the stable component key and full relative
starter path, separately from basename `Document.name`. The installer projects
actual content through `folder_id` into `Workspaces/<Workspace name>/`; two
starters named `guide.md` in different directories remain distinct across
reinstallation and upgrade. `folder_structure` is descriptive pack metadata,
not an instruction to provision empty directories.

Blueprint and user directories are open names. Only the shared runtime layout
is standardized: Workspace root for ordinary/shared files, existing ledger
names, `workflow/<lineage_root_run_id>/` for real Workflow output and
`tasks/<task_id>/<type>/` for Task output. An ordinary Skill call does not create
a Workflow archive. Author portable relative output paths; do not encode
deployment-local `_by_id` roots or runtime IDs. Legacy `production/` defaults
are translated by the runtime, while existing canonical artifact references
remain usable. Full layout and projection-repair ownership belong to WS-10.

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

Inline starters use `.md` or `.csv` paths and UTF-8 text in the legacy
`body_md` field. A Workflow's fixed `document_id` becomes a
`knowledge_document_key` referencing an explicitly included starter. Installation
resolves it only inside the new Workspace. Missing or excluded dependencies
block export/install rather than silently removing the file locator. Runtime
output templates such as `{{delivery.document_id}}` stay unchanged. Fixed
references carrying both a local path and ID must be reduced to the exact ID
before export. Starter file creation never overwrites an occupied path; ordinary
transaction rollback removes only its exact new inode, while an uncertain commit
retains bytes for reconciliation. Existing operator edits are never reset.

## Packaged built-in Skill identity

A portable `builtin:<slug>` resolves only to the unique active, public,
platform-owned row matching the repository-controlled bundle's source digest
and execution fields: prompt, tools, input schema, output format, complete
configuration and version. The expected bundle path comes from the packaged
catalog, never a row's declared path. Seeder and resolver share one pure
specification builder. Missing, stale, spoofed or equally matching duplicate
rows fail closed. Historical duplicate rows, exact-ID runtime invocation and
Agent bindings are not mutated by this resolution.

Export checks this same identity for Agent bindings, Workflow calls and
scheduled Skill targets. A behavior-distinct legacy row cannot be relabelled
as the current built-in. Live setup reconstruction requires the canonical ID
to be bound; a stale same-slug binding or a claimed `source_skill_id` cannot
satisfy it. This compatibility path repairs portable identity without replaying
catalog migrations or deleting historical bindings.

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

App install baselines and configuration upgrades resolve Workflow presentation
keys through the same scoped identity rows as export, without exporting the
Workflow bodies. Capturing App presentation must not require selecting starter
content; an actual Blueprint export still requires every fixed file dependency.
