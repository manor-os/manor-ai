# Workspace Node Test Checklist

Use this as a selectable test matrix. Run the changed node, every crossed-edge
node, and the smoke set at the end. Commands assume the repository root and
`.venv` are available. Prefix every pytest command with `PYTHONPATH=.:tests`
so legacy test helpers imported as `auth_helpers` resolve consistently with
CI. Test names are intentionally independent so a node can be verified without
running the entire suite.

For each node record four results: **Automated**, **Integration**, **Manual**,
and **Pass criteria**. A skipped category needs a reason.

## WS-01: Create, install, and configure

- Automated: `.venv/bin/pytest -q tests/test_workspaces.py tests/test_workspace_access_defaults.py tests/test_blueprint_installer_embedded.py tests/test_blueprint_installer_workflows.py tests/test_workspace_stats.py tests/test_blueprint_payload_v11.py tests/test_webchat_page.py`; plus `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_nango_oauth.py tests/test_integration_channel_ownership.py -k 'whatsapp'`
- Integration: install a minimal Blueprint in simulate mode and assert the
  Workspace, subscriptions, workflow bindings, jobs, Goal/Stat definitions,
  policy, and todos all use the new Workspace/entity identities.
- Manual: open the new Workspace and inspect setup/readiness; unresolved
  integrations must appear as blockers rather than silently disappearing.
  Connect WhatsApp Business without selecting an Agent, then use the existing
  Agent Binding surface to select one exact AgentSubscription/Workspace.
- Pass criteria: no source IDs/secrets copied, no partially installed Workspace
  reported ready, and install is idempotent for Blueprint-owned components.

## WS-02: Runtime scope and chat

- Automated: `.venv/bin/pytest -q tests/test_task_session.py tests/test_response_surfaces.py tests/test_workspace_chat_pending_count.py tests/test_workspace_chat_pending_visibility.py tests/test_workspace_authz_holes.py tests/test_workspace_write_authz.py tests/test_runtime_permissions_roles.py tests/test_workspace_mechanism_regressions.py -k 'chat or feedback'`; plus `.venv/bin/pytest -q tests/test_ai_runtime_harness.py tests/test_agent_provisioning_marketplace_skills.py -k 'agent_provisioning_tool or runtime_requester or runtime_search or progressively_loads_'`, `.venv/bin/pytest -q tests/test_runtime_default_tool_authorization.py tests/test_agentic_loop_token_compaction.py tests/test_runtime_tool_input_validation.py -k 'runtime or search_tools or tool_pool'`, `.venv/bin/pytest -q tests/test_capability_search.py tests/test_builtin_ledger_skills.py tests/test_chrome_skill_run_contract.py`, `.venv/bin/pytest -q tests/test_webchat_page.py`, `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_whatsapp_webhook.py tests/test_channel_link.py`, `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_voice_work_router.py tests/test_voice_work_queue.py tests/test_browser_voice.py tests/test_gateway_voice.py tests/test_twilio_realtime_voice.py tests/test_twilio_voice_runtime.py tests/test_channel_voice_cancellation.py`, and `.venv/bin/pytest -q tests/test_generic_email_mcp.py tests/test_generic_channel_callback.py tests/test_email_attachment_bridge.py`
- Automated failure-control contract: `.venv/bin/pytest -q tests/test_ai_engine.py tests/test_video_auto_wait.py tests/test_skill_tools.py -k 'stop_parent_error or terminal_policy_cannot_turn_failed or consecutive_tool_errors or forced_media_error or local_coding_skill_dispatch or approved_forced_call_aborts or structured_skill_output or nested_tool_error_control or invent_setup_link or keeps_nested_tool_error or manual_web_skill_tool_error'`
- Automated Sandbox interaction contract: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_sandbox_tools_capacity.py tests/test_sandbox_sdk.py tests/test_tool_surface_consolidation.py`
- Integration: send one message in Workspace main chat and one Task thread;
  assert their runtime envelopes carry the correct Workspace/Task and tools.
  For an interactive Task, request a different Agent and assert the Task Host
  still owns prompt, tools, billing attribution, and the assistant message.
  Rate duplicate main/thread projections of one completed Plan and assert one
  normalized rating/revision; repeat with a taskless completed Plan. Delete the
  owning Task and assert its message projection is no longer rateable and its
  canonical feedback plus reviewer evidence are removed atomically, then invoke
  a delayed Plan-completion producer and assert it cannot recreate the marker.
  Upgrade legacy headline/metadata feedback and assert one typed main-chat
  projection, one canonical subject/evidence pair, stripped legacy metadata,
  and non-null target/revision columns; assert a post-upgrade old-worker insert
  that omits those fields is rejected. Deliver a signed WhatsApp webhook for
  one bound phone ID and assert the exact AgentSubscription/Workspace handles
  it; duplicate the active binding or set a conflicting contact pin and assert
  no Agent run or provider reply occurs. Start one Twilio Voice Call, replace
  its work twice while the first Agent exits cooperatively, and assert only the
  latest replacement runs on the Call's frozen binding and Conversation.
- Manual: switch between two Workspaces and confirm history, pending HITL,
  attachments, agent mentions, and input enabled state never cross scopes.
  Open an interactive Task and confirm its reused Chat panel is open, names the
  Host, and rejects starting when that Host is unavailable.
  In both main Chat and a Task Session, request a choice or code-lab surface,
  open its focus view, submit a value, and confirm the Agent consumes it as the
  next turn without a separate user bubble. Submit again, refresh, and confirm
  both operations remain in the originating HTML with the latest choice/code
  restored. Confirm generated HTML has no network, host API, or parent-page access.
  From a member-owned Chat, request another member's private Skill by exact ID
  and slug and assert both discovery and invocation fail; repeat with a Skill ID
  from another Entity. Directly invoke a built-in Ledger Skill in a Workspace
  without its declared contract and assert execution is rejected.
  From a Workspace-bound generic mailbox, save one received text/document
  attachment, ask the Agent about its contents, refresh, and attach the returned
  `document_id` to a reviewed draft. Confirm another Workspace cannot reuse it.
  Start one background Sandbox command that emits `need_tool`, resolve that
  capability through the ordinary Runtime gate, respond once, and confirm the
  same execution completes. Repeat status with `after_sequence` and confirm no
  earlier event is replayed; attempt the response from another Conversation and
  confirm it is denied before Sandbox I/O.
- Pass criteria: no cross-Workspace data/tool leakage; a deleted/inaccessible
  Conversation or Workspace chat fails closed; terminal or user-paused turns
  unlock input; deleting a Task leaves no rateable completion projection or
  completion-feedback evidence for that Task. Runtime code neither guesses a
  completion target from prose nor repairs an old-worker row. Response surfaces
  survive refresh, keep metadata events anchored to the correct assistant
  message, degrade to their text fallback on render failure, and cannot bypass
  normal Chat runtime scope or approval boundaries.
  WhatsApp phone routing resolves one exact active binding, rejects ambiguity,
  and never accepts a contact-level subscription override.
  Twilio Voice acknowledges durable work before the Agent finishes, keeps
  foreground control responsive, serializes spoken responses, and never
  executes or speaks an interrupted receipt or a result from a changed binding.
  Admitted work drains durably after Media Stream closure without another
  Realtime provider write.
  A wide Agent searches only
  its effective bound/contextual tools; an unbound registry tool stays hidden,
  and a loaded schema cannot bypass later revocation.
  A provider wildcard survives Workspace/Workflow prompt assembly and can load
  a newly discovered action; an explicit action allowlist, hard external/editor
  profile, or revoked binding still denies it, and message metadata cannot replay
  the provider scope on a later turn. Capability companions appear only beside
  matched available capabilities, remain ordinary `invoke_skill` targets, and
  cannot grant or execute the paired Tool/MCP capability. Every static
  Integration resolves to a complete child Skill; its MCP discovery/companion
  prefix comes from the route registry, and private hosted child Skills are absent
  from the sanitized OSS catalog and tree. Invalid nested public arguments fail
  with a stable schema path before authorization/HITL and never reach the handler.
  Tool failures remain visible to the model and UI but cannot directly stop the
  Agent loop; bounded repeated failures end through a no-tools model summary,
  while successful terminal tools and strict governance controls retain their
  existing semantics.

## WS-03: Readiness and context

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py`; plus `.venv/bin/pytest -q tests/test_workspace_readiness.py tests/test_strategist_briefing_integration.py tests/test_workspace_knowledge_visibility.py tests/test_knowledge_visibility.py tests/test_workspace_mechanism_regressions.py tests/test_remote_mcp.py tests/test_integration_operation_catalog.py tests/test_runtime_integration_account_registry.py -k 'strategist_provider_scope_ignores_internal_and_manual_measurement_sources or live_remote or account_tool_catalog or provider_catalog_merge or integration_account'`, and `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_integration_health_credential_errors.py tests/test_whatsapp_business_provisioning.py`
- Integration: remove one required subscription/integration/Knowledge input and
  compare readiness plus Strategist context before and after restoration. Repeat
  with manual, `workspace_internal`, and one configured external Goal/Stat
  source; only the last may require its named provider.
- Manual: inspect setup blockers in UI and verify the same missing component is
  named in the Strategist behavior or prevented before execution. Run
  WhatsApp **Test connection** before and after Agent Binding changes and
  confirm it reports the same account readiness rather than routing status.
- Pass criteria: context includes only installed Workspace resources; missing
  setup is explicit; unrelated entity integrations do not appear. Live MCP
  actions cannot widen an explicit Agent allowlist, and stale/wrong-endpoint or
  other-actor account catalogs do not enter context. Cached dynamic bindings
  are re-discovered when account identities, default ordering, registry load
  completeness, remote endpoint, or token transport changes.
  WhatsApp account Ready proves OAuth, assets, phone registration, exact App
  subscription, and callback only; it makes no routing claim.

## WS-04: Strategist and Proposal generation

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py tests/test_strategist_template.py tests/test_strategist_deliverables.py tests/test_strategist_artifact_evidence.py tests/test_strategist_decision_loop_e2e.py tests/test_review_trigger_kind.py tests/test_change_kinds.py`
- Integration: run one human-requested review against fixed Goal configuration
  and measurements; assert typed Proposal validation, Task ownership,
  dependencies, deliverables, stable Goal-change identity, and one chat card.
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

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py tests/test_task_session.py tests/test_task_state_machine.py tests/test_task_dependencies.py tests/test_plan_schema.py tests/test_plan_materialization_linker.py tests/test_task_output_step_contract.py tests/test_plan_contract_enforcement.py`; plus `.venv/bin/pytest -q tests/test_workspace_write_authz.py -k 'plan'`
- Integration: approve a Task with a predecessor and required artifact; assert
  pending dependency, release, Plan materialization, and terminal producer bind.
  Race two Plan persistence callers for one Task and assert the second waits,
  then observes the first non-terminal Plan without writing another. Edit the
  Task while provider generation is in flight and assert the stale Plan is
  discarded without overwriting the newer Task governance context.
  Race API and worker planning while the provider is blocked; assert one
  renewable Task claim admits only one billing/provider path through the fenced
  Plan commit. Crash after Plan commit but before queue publication and assert a
  redelivery dispatches that exact Plan. Race a stale planning-failure writer
  with an uncommitted Plan and assert it waits, then preserves the Task and Plan.
  Race Plan admission with an uncommitted Workspace delete and assert it waits,
  then rejects before Planner/provider work or Plan persistence.
  Repeat the delete race against Plan approve/cancel/retry and assert each waits
  for the Workspace lock and rejects without changing or dispatching the Plan.
  Change a Task to terminal after background planning resolves its scope but
  before admission locks complete; assert no context/provider work or Plan row.
  Delete an interactive Task with queued and running chat work; assert its
  Runtime run tree is cancelled and its Conversation can no longer resolve.
- Manual: inspect Task timeline and Plan steps before execution; verify owner,
  delegates, dependencies, expected output, and approval gates are visible.
- Pass criteria: only legal state transitions occur; Plan is acyclic and scoped;
  each Task owns at most one non-terminal Plan and stale Task snapshots never
  reach persistence; duplicate API/worker planning cannot duplicate provider
  spend, and a stale failure cannot overwrite a committed active Plan;
  required output has one unambiguous producer; blocked, interactive, and
  terminal Tasks do not dispatch, and manual service keys stay within the Task
  owner/delegate scope.
  Deleted interactive Tasks leave no runnable or fallback Task Session turn.
  Planner provider actions come only from the resolved Task requester's callable accounts.

## WS-07: Executor, Dispatcher, and worker lease

- Automated: `.venv/bin/pytest -q tests/test_orchestration_hardening.py tests/test_plan_retries.py tests/test_dispatcher_ref_resolution.py tests/test_dispatcher_envelope_status.py tests/test_lease_heartbeat.py tests/test_task_runner_terminal_guard.py tests/test_scheduler.py tests/test_scheduler_tick.py tests/test_skill_creation_matching.py tests/test_event_emitter.py`; plus `.venv/bin/pytest -q tests/test_workflow_runner_nodes.py tests/test_workflows.py -k 'claim or terminal_commit'`
- Automated Sandbox lifecycle: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_sandbox_runner_lifecycle.py tests/test_sandbox_sdk.py`
- Integration: execute a two-step Plan with ref passing, then simulate duplicate
  delivery, lease expiry, retryable failure, and terminal failure. Duplicate a
  background Agent Task while its renewable claim is live, then simulate owner
  crash and assert the delayed recheck resumes after claim expiry. Race two
  PostgreSQL row-lease claimants and assert only one token is admitted; expire
  that lease and assert a successor can claim without deleting the successor
  during stale-owner cleanup. Replace the token immediately before the old
  owner's terminal commit and assert the business transaction cannot commit.
  Lose an Agent Task claim after possible external effects, fail its first
  recovery publication, and assert a durable Task intent remains sweepable;
  assert every recovered delivery runs only fenced settlement on `recovery-v2`,
  keeps the previous business-task signature, and never enters `TaskRunner`.
  Put an undue intent ahead of a due intent and assert the due row is still
  claimed. Assert a published recovery is not republished before its complete
  retry-chain deadline, and that broker failure releases only the exact unique
  publish token without shortening a newer claim.
  Run a real isolated command that emits one structured input request, wait for
  the exact response file, and then exits. Assert queued/running state, stable
  sequence, one idempotent response, same-process completion, bounded cleanup,
  and rejection of conflicting, oversized, secret-bearing, or unknown-event
  responses.
  Run overlapping occurrences of one Agent
  schedule and assert distinct Tasks retain exact run lineage. Race the
  scheduler tick with a reschedule or A-to-B-to-A config update; assert the old
  revision key is rejected, the stale tick cannot overwrite the replacement
  clock, and explicit delete-after resume creates a new occurrence only after
  the prior run is terminal. Submit the same ScheduledJob prompt update
  concurrently and assert one revision diff, one queued Skill-generation claim,
  one durable generation intent, and one provider path. When that Job already
  references a Skill, assert generation creates a new definition/directory and
  atomically rebinds the Job without changing the old Skill even when ownership
  metadata names that Job; fail broker publication
  and assert the generation sweep republishes that exact revision. Disable a
  pending Job and assert neither the sweep nor a delivered worker crosses the
  credit/provider boundary; re-enable and assert the same intent resumes. Fail
  provider generation through its durable attempt budget, assert the active
  intent closes with a visible error, then edit the prompt or disable-enable and
  assert a fresh bounded attempt window. Advance a rolling-upgrade delivery's
  revision after its preliminary read and assert it exits before credits/provider
  work. Force persistence failure after object-store write and assert the
  uncommitted Skill prefix is removed. Execute a scheduled sandbox Skill and
  assert canonical `invoke_skill` runs its complete bundle before the Agent's
  first model response. Hold an old Beat dispatch-clock transaction past the
  generic retry budget and assert the new worker continues its dedicated
  rolling-upgrade wait, then safely consumes the revalidated legacy occurrence
  rather than losing it. Fail the
  broker handoff before published projection and assert the durable `PREPARED`
  sweep repairs it. Crash an Agent/Workflow child after `PUBLISHED`, wait past
  the child claim lease, and assert the same durable child is redelivered
  without duplicate business completion. Repeat with an admitted generic body
  and assert it reaches an inspectable ambiguous terminal error without replay.
  Delay that body in the broker past its publication deadline, admit it under a
  live occurrence claim, and assert admission refreshes the deadline and the
  sweep defers rather than terminalizing healthy work. Assert Workflow recovery
  uses a header accepted by the previous worker signature, while an upgraded
  worker treats that header as a non-recursive claim recheck;
  after a fast
  child reaches terminal, assert recovery is projection-only, and after a
  settlement fence is written, assert recovery retries settlement only. Fail
  the fence write while holding the occurrence claim and assert the stale owner
  does not publish directly; without an execution claim, fail both the fence
  write and initial dedicated settlement publish. In both cases assert the
  completed task replaces itself with a typed settlement task on the
  version-isolated recovery queue, retains the previous business-task signature,
  and never re-enters its business body; verify pre-upgrade worker queue bindings
  cannot consume that payload. Replace the occurrence token after a finalizer error and assert
  the stale owner neither persists nor directly enqueues settlement, while the
  settlement-only redelivery reacquires the occurrence claim. Exhaust each
  settlement chain and assert retries remain finite while the periodic sweep can
  start a later bounded chain. Lose an Agent claim immediately before terminal commit and
  assert rollback; lose it after terminal commit and assert post-commit cleanup
  cannot overwrite the terminal Task. Race child
  admission with an uncommitted Job pause/delete; assert it waits, then rejects
  and terminalizes only the exact pristine prepared Task/WorkflowRun. Fail one
  scheduled Agent Workspace Chat projection, assert the terminal run retains a
  typed pending intent, recover one Message without rerunning the Agent, and
  verify realtime publication occurs only after the Message transaction commits.
  Replace its Task id with a cross-Workspace Task and assert quarantine without
  creating or publishing a Message.
  Fill one oldest recovery batch with malformed payloads and assert they enter
  terminal quarantine rather than starving a later valid handoff. Include an
  unknown execution state, a non-terminal settlement outcome, a valid orphan
  handoff, a legacy ISO backoff, and an
  invalid high calendar value; assert none republishes completed business or
  remains permanently hidden from the due scan.
- Manual: follow Task execution timeline; verify one active worker per Step,
  stable attempts, visible retries, and one terminal Task reconciliation.
- Pass criteria: no duplicate completion/side effect, references resolve from
  accepted upstream results, retry bounds hold, and unsupported success claims
  cannot complete the Plan. A denied Agent Task claim is acknowledged only
  after one delayed recovery check is accepted by the broker; a completed Task
  makes that recovery delivery a no-op. A lost Agent claim is acknowledged only
  after terminal evidence, a durable Task recovery intent, or a broker-accepted
  versioned settlement delivery; broker loss is repaired without replaying the
  Agent business body. Execution uses one PostgreSQL claim
  domain with short transactions and does not pin a connection for the
  duration of provider work. Scheduled recovery completes both published state
  and ledger projection for the original run; settlement-only retries never
  call the business runner, retry chains are finite, and terminal Task, Workflow, manual-step, and
  scheduled-settlement commits close the claim fence before auxiliary
  post-commit awaits. The same database transaction validates and locks the
  current durable token before each claimed terminal commit. Scheduler polling
  uses the indexed typed due clock, and generation intent remains due until its
  exact revision installs a Skill. Scheduled Workspace Chat output
  has one durable Message, a terminal projection state, and no pre-commit ghost
  event; malformed recovery rows are inspectable and leave the active scan.
  A live setup regression leaves materialized pending
  Steps unleased and resumes them only after the trusted setup contract is ready.
  Cooperative provider rate limits release the active Lease into bounded,
  Retry-After-aware retry without consuming a business attempt; provider
  failures retain their real status, and exhausted credits do not blind-retry.
  A queue failure after Plan commit redispatches the same Plan ID and surfaces
  a durable recovery action on exhaustion instead of creating a second Plan.
  Worker event persistence finishes before its local
  loop closes, while external delivery has a bounded shutdown wait and resumes
  through durable retry state rather than replaying committed Task work. Runtime
  schema hydration uses the same actor account boundary as planning and cannot
  import another tenant's discovered action. Scheduled Skill provider work
  holds neither the request transaction nor a worker DB connection; its final
  write revalidates Job and Skill revisions under lifecycle -> Job -> Skill
  lock order, and a failed final fence leaves any new file directory
  unreachable without changing the previously committed Skill.

## WS-08: HITL and resume

- Automated: `.venv/bin/pytest -q tests/test_dispatcher_unified_approval_gate.py tests/test_lease_needs_human_request.py tests/test_pending_action_resolve.py tests/test_approval_card_lifecycle.py tests/test_workspace_hitl_channel_ack.py tests/test_channel_approved_reply_routes.py tests/test_hitl_card_surface.py tests/test_workspace_chat_tool_hitl_surface.py tests/test_runtime_guard_unified_store.py tests/test_runtime_tool_input_validation.py`; plus `.venv/bin/pytest -q tests/test_ai_engine.py -k 'approved_forced_call'` and `node --test apps/web/scripts/chat-message-collapse.test.mjs`
- Integration: pause one Step for approval/input, resolve once, resend the same
  response/token, and assert only the original origin resumes once. Pause a
  parent on subworkflow and foreach-subworkflow children, attempt a human resume,
  and assert it remains paused until the exact durable child receipt arrives.
  Resolve two HITL requests stored on the same Message concurrently and assert
  both resolution markers survive commit and refresh.
  Fail an approved chat-tool continuation before provider I/O, both as a
  returned preflight error and as an unavailable settlement store. Assert the
  Agent loop keeps explicitly read-only tools, can execute one alternate read,
  and excludes the failed write plus every other write/unclassified tool.
  Repeat with a stale Runtime abort and a successful or currently executing
  provider result; assert those paths remain hard-stop/tool-free and never
  repeat the effect. Separately, present a durable claim from an earlier
  ambiguous attempt before current provider I/O; assert only read-only
  reconciliation tools remain available and the claimed write is not repeated.
  Attempt a delayed WhatsApp free-form reply outside the customer-service
  window, assert it remains actionable, then select an explicit approved
  template and assert only that template operation is sent.
- Manual: exercise approve, reject, user takeover, login blocker, manual pause,
  cancel, and refresh; ensure cards resolve and chat input becomes usable.
- Pass criteria: exact payload binding, single consumption, correct resume
  origin, no duplicate task, no stale unresolved UI after terminal state.
  Concurrent resolutions sharing one Message preserve every card terminal state.
  Proven pre-provider failures remain inside the bounded Agent loop without
  reusing the approval token; only read-only alternate tools remain callable.
  Every nested JSON value retains its exact type through approval (including
  arrays, objects, booleans, numbers, strings, and null); an old malformed
  continuation expires before a grant, token consumption, or handler execution.
  After the exact forced call or provider confirm-and-retry pair, the model
  receives no callable tools for its final summary, so one approval cannot
  produce a duplicate side effect/card. Pending
  actions render as waiting and any failed tool remains an error, never a false
  recovered/completed label.
  Determinate external-reply failure leaves the approval actionable; adapters
  with provider idempotency retain a same-key-retry-only claim after ambiguous
  failure, allow immediate approval retry with the stable key, and keep
  rejection blocked; at-least-once adapters quarantine the ambiguous outcome
  without an automatic duplicate send. Retry classification comes from the
  claim/attempt snapshot frozen before provider I/O, not the adapter registry
  loaded by a later deployment.
  WhatsApp outside-window free-form replies fail determinately with
  `whatsapp_template_required`; the approval remains actionable until an
  explicit approved template is selected.
  Sandbox requests never mint authority: any requested Runtime tool or external
  effect still crosses its normal approval gate, and credential exchange uses
  references rather than plaintext values.

## WS-09: Workspace Flow and Workflow runtime

- Automated: `.venv/bin/pytest -q tests/test_workspace_workflow_entrypoints.py tests/test_workspace_flow_tools.py tests/test_workflow_runner_nodes.py tests/test_workflow_orchestration_nodes.py tests/test_workflow_publication_receipts.py tests/test_workflow_chat_origin.py tests/test_workflows.py -k 'terminal_effect or continuation'`; plus `.venv/bin/pytest -q tests/test_webchat_page.py`
- Integration: launch the same bound Flow from chat, Proposal, and schedule;
  assert validated inputs, idempotent launch key, snapshot, trace, and lineage.
  Crash after terminal state commits but before ledger/error-handler/subworkflow
  effects finish; assert the terminal-effect sweep redelivers the same run and
  records completion only after all idempotent effects drain. Fail the immediate
  terminal-effect drain from API cancellation, workflow tools, and manual step
  execution; assert each returns the already-committed business outcome while
  leaving recovery intent claimable.
- Manual: start a Flow from Workspace chat, satisfy starter input/HITL, refresh
  during execution, and inspect terminal outputs without 404 polling loops.
  Publish a manual Workflow as a Webchat action, submit it twice with the same
  client submission key, and confirm only one run exists; disable the binding
  and confirm the public form disappears. Fail the first broker dispatch,
  confirm Webchat reports a retryable error, then retry with the same key and
  confirm the original run is queued. Compare a document module in Review and
  on the public page, including a document that falls back to chunk text. Create
  more than one document scan budget of mostly-private resources, page through
  the picker cursor to a later public document, and confirm a previously selected
  reference still appears through server Review; fail the picker and Review
  requests and confirm only the picker becomes unavailable while publication
  stays blocked.
  Rotate anonymous visitor sessions from one IP and confirm the new-submission
  quota does not reset; confirm a pending submission uses its separate retry
  quota and the configured shared rate-limit backend. Confirm a read-only member
  cannot fetch the resource catalog or submit a candidate page, but can Review
  the exact page already saved on an active Webchat binding.
- Pass criteria: runtime executes installed definition through active binding;
  partial/waiting stays nonterminal; retry preserves accepted outputs/effects.
  Multi-account operation catalogs retain per-action account provenance;
  partial registries require an exact account and read-only `all` mode fans out
  only to accounts that exposed that action. Terminal state is authoritative
  even while recoverable effects are pending, and the typed completion marker
  prevents further sweep claims after those effects finish.

## WS-10: Artifact and Knowledge projection

- Automated: `.venv/bin/pytest -q tests/test_artifact_knowledge_projection.py tests/test_workspace_artifacts.py tests/test_workspace_task_artifacts_are_visible.py tests/test_task_scoped_artifacts.py tests/test_knowledge_file_consistency.py tests/test_internal_worker_artifact_capture.py tests/test_entity_fs_write_guard.py tests/test_generate_file_tool.py tests/test_workspace_artifact_purge.py tests/test_event_emitter.py tests/test_email_attachment_bridge.py tests/test_documents.py`; `npm --prefix apps/web exec playwright test e2e/knowledge-upload-lifecycle.spec.ts e2e/knowledge-upload-page.spec.ts`
- Integration: produce one real file from a Task and one from a Workflow; assert
  entity-relative path, Document row, provenance, viewer URL, Task/chat card,
  and Knowledge visibility all identify the same artifact. Import one bounded
  Email attachment and assert its original bytes, Workspace folder, Document,
  provenance, extracted text, and viewer URL resolve to the same identity.
  Force a browser-upload commit acknowledgement loss with delayed receipt
  visibility, then retry the same key and assert one readable source file and
  one Document. Reload during processing and assert receipt polling resumes
  without an automatic second upload; force a persistent negative receipt,
  explicitly reselect the matching original file, and assert the same key is
  reused. Expire both keyed and cleanup-only recovery intents, then assert
  unreferenced bytes are removed while a committed Document's source survives.
  Switch principals and assert no retry, toast, or cache mutation crosses the
  identity boundary.
- Manual: open the attachment from Task, chat, and Knowledge. Rename the
  Workspace and confirm durable identity/path behavior remains coherent.
- Pass criteria: for the Task/Workflow paths covered by the listed tests, a
  concrete readable file has a Document ID; no phantom filename, cross-entity
  path, empty Knowledge entry, or completed-required-artifact gap. A new
  producer must prove projection separately; rolled-back multi-file projection
  emits no upload event, committed event delivery happens afterward, interrupted
  delivery resumes from its EventLog lease without replaying the artifact
  transaction, and failed backup cleanup retains a durable retry intent. This
  checklist is not a blanket claim for every external provider.

## WS-11: Configurable Goals, measurements, events, evaluation, and learning

- Automated: `.venv/bin/pytest -q tests/test_goals.py tests/test_goal_scheduling.py tests/test_goal_measurement_direction.py tests/test_workspace_operation_goal_measurement_sources.py tests/test_workspace_stats.py tests/test_migration_graph.py tests/test_ledger_adapters.py tests/test_workspace_event_ledger.py tests/test_workspace_event_triggers.py tests/test_workspace_evaluation.py tests/test_strategist_decision_loop_e2e.py tests/test_workspace_mechanism_regressions.py`
- Integration: create manual, `workspace_internal`, and integration-backed
  WorkspaceStats; bind a Goal by local `stat_id`; repeat the same observation
  idempotency key; change the Goal source/cadence and verify stale collection
  does not apply. Complete a goal-linked Task, record measurement/evaluation,
  then start another review and assert the prior evidence is present once.
  Change a completion rating through another receipt projection and assert the
  canonical feedback row and reviewer evidence carry the same latest revision.
  Remove or invalidate a scheduled Goal/Stat definition and assert the derived
  Job disables through the shared mutation factory with updated timestamp,
  revision, audit patch, and run causation.
- Manual: configure a Goal target/baseline/deadline and Stat collector in the
  Workspace UI; inspect identity, cadence, source and freshness, then inspect
  the observation, goal progress, evaluation, and next Proposal rationale after
  a completed and a failed run.
- Pass criteria: Goal identity remains stable when metric configuration changes;
  WorkspaceStat collection and Goal outcome configuration are independently
  valid and local to the Workspace; a repeated observation produces no extra
  measurement. Invalid lifecycle/cadence/source/Stat scope fails closed;
  manual/internal sources do not demand a provider. The listed ledger adapters
  produce idempotent correlated events and sourced measurements, with no
  invented impact; direct or legacy event writers require separate coverage.

## WS-12: Credits and usage

- Automated: `DEPLOYMENT_MODE=cloud .venv/bin/pytest -q tests/test_credit_reservations.py tests/test_chat_byok_credit_gate.py tests/test_byok_plan_gate.py tests/test_scheduler_tick.py tests/test_skill_creation_matching.py tests/test_billing_balance.py tests/test_browser_voice.py tests/test_gateway_voice.py tests/test_voice_startup.py tests/test_twilio_realtime_voice.py tests/test_twilio_voice_runtime.py`
- Integration: exhaust credits, then invoke direct chat, scheduler, Strategist,
  Task, Workflow, nested skill, media, Browser Realtime Voice, and Twilio Voice
  Realtime paths; assert no provider call or duplicate transaction. Repeat both
  Voice entries with platform-managed
  Realtime and native OpenAI BYOK, confirming platform calls reserve before
  provider I/O and BYOK skips the platform media reservation/debit.
- Manual: compare transaction/usage screens before and after one known call,
  one blocked call, one failed strict response settlement, and one BYOK
  Realtime call; confirm Browser usage is attributed to its authenticated Chat
  scope and Twilio usage to the persisted connection owner and exact
  Workspace/Agent binding recorded on the call session.
- Pass criteria: every surface covered by the listed tests gates before
  fan-out; one reservation/settlement per covered billable call; blocked calls
  do not debit; BYOK follows configured policy. The shared Realtime engine
  checks platform credit before every generated response, records every
  provider response with a stable operation ID and correct provider
  attribution, prices cached audio exactly once, and never swallows a strict
  settlement failure. Browser/Twilio media adapters retain their independent
  reservation lifetime and authorization boundaries. Adding a provider or
  entry surface requires its own gate test.

## WS-13: Pause, delete, restore, and purge

- Automated: `.venv/bin/pytest -q tests/test_workspace_lifecycle.py tests/test_document_permissions.py tests/test_folder_permissions.py tests/test_document_access_batch.py tests/test_entity_fs_write_guard.py tests/test_reusable_resource_locking.py tests/test_deletion_tasks.py tests/test_workspace_mechanism_regressions.py tests/test_user_lifecycle.py tests/test_workspace_purge_owned_resources.py tests/test_twilio_voice_runtime.py tests/test_blueprint_channel_revert.py tests/test_integrations.py -k 'delete or restore or purge or paused or payload or disconnect or userless or twilio_binding or blueprint_twilio'`; plus `.venv/bin/pytest -q tests/test_sharing_permission_boundaries.py -k 'rechecks or auth_read or read_gate or folder_list_gate or revoked_bearer or can_manage or primary_entity or actor_resolution'` and `DEPLOYMENT_MODE=cloud .venv/bin/pytest -q tests/test_admin_impersonation.py -k 'ended_impersonation or owner_can_start_and_end'`
- Integration: create Workspace Task/Plan/Lease/Workflow run/job and job-run/chat/artifact,
  notification/outbox/delivery and Goal/Stat collection schedules, soft-delete
  it, verify immediate blocking and automation cleanup, and assert its
  document/folder ACL routes and public share tokens fail closed until restore;
  assert low-level filesystem mutations and userless agent listings cannot
  bypass that lifecycle boundary, and a shared parent filters deleted-Workspace
  direct children;
  deploy its
  Agent/Skill/Workflow resources into a second Workspace, backdate it, purge,
  and assert the covered Workspace-scoped rows are gone while reused resources
  and the second Workspace's bindings survive. Assert Goal/Stat definitions,
  observations, Goal measurements, and Task links are removed for the purged
  Workspace; links from an entity Goal to a purged Task are removed without
  deleting that entity Goal; scheduled-job runs are removed with their
  definition at soft delete and the restored schedule can reclaim the same
  occurrence key; Workflow action grants are deleted at soft delete while
  WorkflowProject state remains restorable; Workflow runs and projects are
  removed at hard purge; the second Workspace's corresponding rows survive.
  Delete a conversation containing Task/Plan-completion feedback and assert
  both its feedback row and derived RuntimeEvidence are removed. Disconnect a
  Twilio Integration and assert only pending/connecting Voice sessions are
  canceled while terminal history remains. Rebind and remove Twilio through
  Integration binding and legacy Channel CRUD, Workspace attach/update/remove,
  Workspace Operation apply, and Blueprint upgrade/revert; assert each cancels
  only unconnected calls frozen to the changed binding.
  Hard-purge a user, Workspace, and
  final Entity and assert only their owned Voice runtime rows are removed.
  Reconnect WhatsApp through a fully provisioned replacement and assert the
  existing ChannelConfig, Channel, AgentSubscription, and Workspace route IDs
  survive; disconnect it and assert exact App/Nango cleanup completes while the
  customer WABA and phone remain intact.
  Hard-purge a user with acquisition attribution and assert that personal
  attribution row is removed.
- Manual: delete and restore within grace period; verify restored data is
  accessible but removed automations are not unexpectedly recreated.
- Pass criteria: no new work after pause/delete, no stale polling, and restore
  is bounded. Parent-first Job/occurrence locks make child admission linearizable
  with pause/delete; an already-admitted child may finish, while an unstarted
  prepared child reaches an explicit cancelled terminal state. Purge deletes
  only exclusively owned Agent/Skill/Workflow
  definitions. Reused definitions lose the deleted home without losing source
  provenance or surviving deployments; entity-wide reuse remains entity-visible,
  while scoped reuse is limited by explicit Workspace/user view grants. Purge is
  only complete for the rows explicitly covered by the current implementation
  and tests; action grants never survive restore, while WorkflowProject state
  remains until hard purge; the architecture reference lists known uncovered
  workspace-owned tables that remain a follow-up. Restore and purge serialize on the same
  entity lifecycle lock, stale `deleted_at` candidates are rejected, direct
  Task/Conversation/Channel/Runtime/Automation/Flow configuration writers use
  the same lock, and one failed nightly candidate cannot roll back an earlier
  successful Workspace purge. Physical artifact tree, document source file,
  and derived preview-cache deletion starts only after the owning database
  deletion commits; a disabled, missing, unmounted, or failing filesystem
  retains one durable cleanup job with bounded backoff, while later cleanup
  jobs continue without starvation. Physical cleanup shares the entity mutation
  lock even when the entity root is initially missing; final symlinks are
  atomically quarantined and unlinked without touching their destinations,
  symlink parents fail closed, a source path reused by a surviving Document is
  preserved, and a target replaced by an unexpected regular file is restored
  while the cleanup job is retained. Twilio Voice hard purge follows explicit
  user/Workspace/Entity ownership and does not depend on a deleted
  ChannelConfig foreign key. Every Twilio binding writer invalidates only
  `pending/connecting` calls for that exact binding before its route mutation;
  connected and terminal history is preserved. Revoking a client-visible Webchat document removes
  its public module, and pausing/deleting the Workspace prevents its published
  action from creating another Workflow run.
  WhatsApp reconnect preserves ChannelConfig, Channel, AgentSubscription, and
  Workspace identities until the replacement is Ready. Disconnect commits
  fail-closed state before provider I/O, unsubscribes the exact App, retires the
  exact Nango connection with bounded retry, and leaves the customer WABA and
  phone intact.

## WS-14: Blueprint portability

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py tests/test_blueprint_payload_v11.py tests/test_blueprint_exporter_embedded.py tests/test_blueprint_installer_embedded.py tests/test_blueprint_installer_workflows.py tests/test_blueprint_v11_e2e.py tests/test_workspace_stats.py tests/test_manor_workspace_blueprint_checker.py tests/test_manor_workspace_blueprint_roundtrip.py`
- Integration: export a configured Workspace, install simulate/live in an
  isolated entity, export again, normalize generated IDs only, and compare.
  Confirm Goal `goal_key` and Stat `key` relations plus safe source/cadence and
  baseline survive, while measurements, observations, current values, pace,
  task links and schedule runs do not. Pause the source Workspace and confirm a
  disabled scheduled Skill definition still round-trips through a portable
  component/Marketplace identity to a new local Skill ID, while its generation
  recovery clocks and local source ID do not.
- Manual: inspect marketplace copy, install preview/todos, installed operating
  loop, subscriptions, Flows, automations, approval scope, and sample outputs.
- Pass criteria: semantic round-trip; no secret-shaped keys or credential values
  embedded in free-form portable text; no source ID/runtime Task/Plan/run/
  artifact/credit state; deployment-local Webchat document and Workflow-binding
  references are excluded; and prose matches executable configuration.

## WS-15: Notification persistence and delivery

- Automated: `.venv/bin/pytest -q tests/test_notifications.py tests/test_notify_multichannel.py tests/test_notification_scheduler.py tests/test_notification_callbacks.py tests/test_task_event_notifications.py tests/test_workspace_notification_knowledge_access.py tests/test_workspace_hitl_channel_ack.py tests/test_realtime_task_updates.py tests/test_migration_graph.py`
- Integration: create one immediate and one scheduled Workspace notification
  from a supported Task/HITL/media producer, retry the same producer key,
  reject a changed delivery intent, run two
  sweepers, expire and reclaim one processing lease, let the stale worker try
  to finish, simulate an authorization-store error, revoke Workspace access,
  revoke entity role/membership and Workspace membership while a provider send
  is waiting, pause the Workspace between two configured external targets, and assert one
  parent row plus the expected delivered/retried/canceled outbox states and no
  second provider send. Request an explicit channel without a target and assert
  retry/failure rather than false delivery. Upgrade a database already stamped
  at the prior notification revision and assert the claim token appears.
  Hard-purge the Workspace and assert parent/outbox/delivery removal. Process a
  WhatsApp inbound event and assert any staff alert is in-app only; request a
  personal WhatsApp notification target and assert it is rejected without a
  provider send.
- Manual: observe a supported producer's in-app, email/channel, refresh,
  reconnect, and permission-revocation behavior while a notification is pending
  and while Redis is restarted. An evaluation snapshot is not a notification
  producer unless product code explicitly routes it through WS-15.
- Pass criteria: rollback emits no realtime event; commit creates one visible
  fact; scheduled delivery creates no duplicate parent; stale leases recover;
  stale claim owners cannot commit; provider and worker-interruption retries
  are bounded and inspectable; authorization errors retry; inaccessible
  recipients receive no Workspace content through API, Runtime, or WebSocket;
  entity-role, membership, user-state, Workspace-membership, lifecycle, and
  access-mode changes serialize with delivery and access is rechecked in the
  same locked transaction before every external target; cancellation follows
  parent-before-outbox lock order; unresolved explicit channels never report
  delivered; Workspace purge removes notification runtime state. External
  channels are explicitly at-least-once.
  WhatsApp Business may produce an in-app staff notification for inbound work,
  but is not eligible as a personal external notification delivery target.


## Cross-cutting smoke set

Run after any change that crosses three or more nodes:

```bash
PYTHONPATH=.:tests .venv/bin/pytest -q \
  tests/test_strategist_decision_loop_e2e.py \
  tests/test_task_output_step_contract.py \
  tests/test_dispatcher_unified_approval_gate.py \
  tests/test_workspace_workflow_entrypoints.py \
  tests/test_artifact_knowledge_projection.py \
  tests/test_workspace_stats.py \
  tests/test_goals.py \
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
