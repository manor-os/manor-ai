# Workspace Node Test Checklist

Use this as a selectable test matrix. Run the changed node, every crossed-edge
node, and the smoke set at the end. Commands assume the repository root and
`.venv` are available. Test names are intentionally independent so a node can
be verified without running the entire suite.

For each node record four results: **Automated**, **Integration**, **Manual**,
and **Pass criteria**. A skipped category needs a reason.

## WS-01: Create, install, and configure

- Automated: `.venv/bin/pytest -q tests/test_workspaces.py tests/test_workspace_access_defaults.py tests/test_blueprint_installer_embedded.py tests/test_blueprint_installer_workflows.py`
- Integration: install a minimal Blueprint in simulate mode and assert the
  Workspace, subscriptions, workflow bindings, jobs, policy, and todos all use
  the new Workspace/entity identities.
- Manual: open the new Workspace and inspect setup/readiness; unresolved
  integrations must appear as blockers rather than silently disappearing.
- Pass criteria: no source IDs/secrets copied, no partially installed Workspace
  reported ready, and install is idempotent for Blueprint-owned components.

## WS-02: Runtime scope and chat

- Automated: `.venv/bin/pytest -q tests/test_workspace_chat_pending_count.py tests/test_workspace_chat_pending_visibility.py tests/test_workspace_authz_holes.py tests/test_workspace_write_authz.py tests/test_runtime_permissions_roles.py`
- Integration: send one message in Workspace main chat and one Task thread;
  assert their runtime envelopes carry the correct Workspace/Task and tools.
- Manual: switch between two Workspaces and confirm history, pending HITL,
  attachments, agent mentions, and input enabled state never cross scopes.
- Pass criteria: no cross-Workspace data/tool leakage; deleted/inaccessible
  Workspace chat fails closed; terminal or user-paused turns unlock input.

## WS-03: Readiness and context

- Automated: `.venv/bin/pytest -q tests/test_workspace_readiness.py tests/test_strategist_briefing_integration.py tests/test_workspace_knowledge_visibility.py tests/test_knowledge_visibility.py`
- Integration: remove one required subscription/integration/Knowledge input and
  compare readiness plus Strategist context before and after restoration.
- Manual: inspect setup blockers in UI and verify the same missing component is
  named in the Strategist behavior or prevented before execution.
- Pass criteria: context includes only installed Workspace resources; missing
  setup is explicit; unrelated entity integrations do not appear.

## WS-04: Strategist and Proposal generation

- Automated: `.venv/bin/pytest -q tests/test_strategist_template.py tests/test_strategist_deliverables.py tests/test_strategist_artifact_evidence.py tests/test_strategist_decision_loop_e2e.py tests/test_review_trigger_kind.py`
- Integration: run one human-requested review against fixed goals/context and
  assert typed Proposal validation, Task ownership, dependencies, deliverables,
  and one chat card.
- Manual: run a Workspace until a Proposal appears; inspect Summary, Task titles,
  owners, expected artifacts, and absence of duplicate open proposals.
- Pass criteria: Proposal is context-derived, schema-valid, bounded, and stored
  once; credit failure or invalid output creates no partial Task cohort.

## WS-05: Proposal governance

- Automated: `.venv/bin/pytest -q tests/test_proposal_items.py tests/test_proposal_card_item_cohorts.py tests/test_proposal_reject_reason_api.py tests/test_approval_matrix_api.py tests/test_standing_grants_api.py`
- Integration: exercise allow, needs-human, deny, partial selection, and an
  external-action cohort; assert exact Task/workflow dispatch behavior.
- Manual: approve and reject separate proposals from Workspace chat; refresh and
  verify card state and Task state remain consistent.
- Pass criteria: one auditable decision per cohort; rejected items never run;
  approval cannot authorize changed arguments or another cohort.

## WS-06: Task lifecycle and planning

- Automated: `.venv/bin/pytest -q tests/test_task_state_machine.py tests/test_task_dependencies.py tests/test_plan_schema.py tests/test_plan_materialization_linker.py tests/test_task_output_step_contract.py tests/test_plan_contract_enforcement.py`
- Integration: approve a Task with a predecessor and required artifact; assert
  pending dependency, release, Plan materialization, and terminal producer bind.
- Manual: inspect Task timeline and Plan steps before execution; verify owner,
  delegates, dependencies, expected output, and approval gates are visible.
- Pass criteria: only legal state transitions occur; Plan is acyclic and scoped;
  required output has one unambiguous producer; blocked Tasks do not dispatch.

## WS-07: Executor, Dispatcher, and worker lease

- Automated: `.venv/bin/pytest -q tests/test_orchestration_hardening.py tests/test_plan_retries.py tests/test_dispatcher_ref_resolution.py tests/test_dispatcher_envelope_status.py tests/test_lease_heartbeat.py tests/test_task_runner_terminal_guard.py`
- Integration: execute a two-step Plan with ref passing, then simulate duplicate
  delivery, lease expiry, retryable failure, and terminal failure.
- Manual: follow Task execution timeline; verify one active worker per Step,
  stable attempts, visible retries, and one terminal Task reconciliation.
- Pass criteria: no duplicate completion/side effect, references resolve from
  accepted upstream results, retry bounds hold, and unsupported success claims
  cannot complete the Plan.

## WS-08: HITL and resume

- Automated: `.venv/bin/pytest -q tests/test_dispatcher_unified_approval_gate.py tests/test_lease_needs_human_request.py tests/test_pending_action_resolve.py tests/test_approval_card_lifecycle.py tests/test_workspace_hitl_channel_ack.py tests/test_hitl_card_surface.py`
- Integration: pause one Step for approval/input, resolve once, resend the same
  response/token, and assert only the original origin resumes once.
- Manual: exercise approve, reject, user takeover, login blocker, manual pause,
  cancel, and refresh; ensure cards resolve and chat input becomes usable.
- Pass criteria: exact payload binding, single consumption, correct resume
  origin, no duplicate task, no stale unresolved UI after terminal state.

## WS-09: Workspace Flow and Workflow runtime

- Automated: `.venv/bin/pytest -q tests/test_workspace_workflow_entrypoints.py tests/test_workspace_flow_tools.py tests/test_workflow_runner_nodes.py tests/test_workflow_orchestration_nodes.py tests/test_workflow_publication_receipts.py tests/test_workflow_chat_origin.py`
- Integration: launch the same bound Flow from chat, Proposal, and schedule;
  assert validated inputs, idempotent launch key, snapshot, trace, and lineage.
- Manual: start a Flow from Workspace chat, satisfy starter input/HITL, refresh
  during execution, and inspect terminal outputs without 404 polling loops.
- Pass criteria: runtime executes installed definition through active binding;
  partial/waiting stays nonterminal; retry preserves accepted outputs/effects.

## WS-10: Artifact and Knowledge projection

- Automated: `.venv/bin/pytest -q tests/test_artifact_knowledge_projection.py tests/test_workspace_artifacts.py tests/test_workspace_task_artifacts_are_visible.py tests/test_task_scoped_artifacts.py tests/test_knowledge_file_consistency.py tests/test_internal_worker_artifact_capture.py`
- Integration: produce one real file from a Task and one from a Workflow; assert
  entity-relative path, Document row, provenance, viewer URL, Task/chat card,
  and Knowledge visibility all identify the same artifact.
- Manual: open the attachment from Task, chat, and Knowledge. Rename the
  Workspace and confirm durable identity/path behavior remains coherent.
- Pass criteria: for the Task/Workflow paths covered by the listed tests, a
  concrete readable file has a Document ID; no phantom filename, cross-entity
  path, empty Knowledge entry, or completed-required-artifact gap. A new
  producer must prove projection separately; this checklist is not a blanket
  claim for every external provider.

## WS-11: Events, goals, evaluation, and learning

- Automated: `.venv/bin/pytest -q tests/test_ledger_adapters.py tests/test_workspace_event_ledger.py tests/test_workspace_event_triggers.py tests/test_workspace_evaluation.py tests/test_strategist_decision_loop_e2e.py`
- Integration: complete a goal-linked Task, record measurement/evaluation, then
  start another review and assert the prior evidence is present once.
- Manual: inspect observability timeline, goal progress, evaluation, and next
  Proposal rationale after a completed and a failed run.
- Pass criteria: the listed ledger adapters produce idempotent correlated
  events and sourced measurements, with no invented impact; direct or legacy
  event writers require separate coverage.

## WS-12: Credits and usage

- Automated: `.venv/bin/pytest -q tests/test_credit_reservations.py tests/test_chat_byok_credit_gate.py tests/test_byok_plan_gate.py tests/test_scheduler_tick.py tests/test_billing_balance.py`
- Integration: exhaust credits, then invoke direct chat, scheduler, Strategist,
  Task, Workflow, nested skill, TTS/media paths; assert no provider call or
  duplicate transaction. Repeat with BYOK.
- Manual: compare transaction/usage screens before and after one known call,
  one blocked call, one failed settlement retry, and one BYOK call.
- Pass criteria: every surface covered by the listed tests gates before
  fan-out; one reservation/settlement per covered billable call; blocked calls
  do not debit; BYOK follows configured policy. Adding a provider or entry
  surface requires its own gate test.

## WS-13: Pause, delete, restore, and purge

- Automated: `.venv/bin/pytest -q tests/test_workspace_lifecycle.py tests/test_workspace_mechanism_regressions.py -k 'delete or restore or purge or paused'`
- Integration: create Workspace Task/Plan/Lease/Workflow run/job/chat/artifact,
  soft-delete it, verify immediate blocking and automation cleanup, backdate it,
  purge, and assert Workspace-scoped rows are gone.
- Manual: delete and restore within grace period; verify restored data is
  accessible but removed automations are not unexpectedly recreated.
- Pass criteria: no new work after pause/delete, no stale polling, and restore
  is bounded. Purge is only complete for the rows explicitly covered by the
  current implementation and tests; the architecture reference lists known
  uncovered workspace-owned tables that remain a follow-up.

## WS-14: Blueprint portability

- Automated: `.venv/bin/pytest -q tests/test_blueprint_payload_v11.py tests/test_blueprint_exporter_embedded.py tests/test_blueprint_installer_embedded.py tests/test_blueprint_installer_workflows.py tests/test_blueprint_v11_e2e.py tests/test_manor_workspace_blueprint_checker.py tests/test_manor_workspace_blueprint_roundtrip.py`
- Integration: export a configured Workspace, install simulate/live in an
  isolated entity, export again, normalize generated IDs only, and compare.
- Manual: inspect marketplace copy, install preview/todos, installed operating
  loop, subscriptions, Flows, automations, approval scope, and sample outputs.
- Pass criteria: semantic round-trip, no secret/source ID/runtime Task/Plan/run/
  artifact/credit state, and prose matches executable configuration.

## Cross-cutting smoke set

Run after any change that crosses three or more nodes:

```bash
.venv/bin/pytest -q \
  tests/test_strategist_decision_loop_e2e.py \
  tests/test_task_output_step_contract.py \
  tests/test_dispatcher_unified_approval_gate.py \
  tests/test_workspace_workflow_entrypoints.py \
  tests/test_artifact_knowledge_projection.py \
  tests/test_credit_reservations.py \
  tests/test_workspace_lifecycle.py \
  tests/test_blueprint_v11_e2e.py
```

Also run:

```bash
.venv/bin/ruff check <changed-python-files>
git diff --check
```

## New node rule

If a feature introduces a new persisted runtime, entry surface, approval
origin, billable provider path, artifact type, or deletion-owned resource:

1. add or extend a `WS-xx` architecture node;
2. add Automated, Integration, Manual, and Pass criteria entries here;
3. add it to the cross-node contract table;
4. decide its Blueprint portability explicitly;
5. add a structural regression test so later documentation cannot omit it.
