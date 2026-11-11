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

- App runner entries (WS-01 -> WS-02/09): run
  `tests/test_workspace_app_direct_entries.py` and
  `tests/test_workspace_app_workflow_configuration.py`, plus frontend
  `workspace-app-targets.test.mjs` and browser `workspace-app-direct-entries.spec.ts`.
  Verify a Workspace with no Services can add multiple Agent commands, connect
  a saved Workflow in the same reviewed draft, switch to Automation, reload and
  remove an entry while preserving its runner. Missing and unauthorized targets
  fail validation; an invalid saved Workflow is absent from the catalog. Check
  desktop and mobile layouts and legacy Service keys containing dots.

- Initial review regression (WS-01/14 -> WS-04 through WS-07): run
  `tests/test_workspace_initial_review.py`, `tests/test_blueprint_startup_service.py`,
  `tests/test_workspace_creation_modes.py`, `tests/test_workspace_setup_gates.py`,
  `tests/test_review_trigger_kind.py`, `tests/test_automated_run_approvals.py`,
  `tests/test_human_request_items.py`, `tests/test_scheduler.py`, and
  `tests/test_scheduler_tick.py` on an isolated PostgreSQL database.
  Integration: assert a transactional startup receipt, post-commit publication,
  rollback, independent installs, broker failure recovery, execution claim and
  terminal replay suppression, and setup input without business Tasks. A first
  readiness sweep must not duplicate installation; later count changes still
  wake Strategist. Manual/simulated installs, explicit ready jobs, pause/delete,
  and disabled jobs keep their boundaries. Export excludes runtime receipts.
  Manual: install an automatic LIVE Blueprint with a required user input;
  inspect the first ReviewRun and Input Needed in Chat without waiting for the
  readiness sweep, then provide input through its ordinary continuation.
  Pass criteria: healthy installation publishes immediately after commit;
  model latency is separate, approval/credits stay on the normal review path,
  and failed publication remains recoverable by the existing scheduler.


- Declared service capability coverage: new `propose_service` calls explicitly
  provide canonical `required_capabilities` (possibly empty). In
  `tests/test_workspace_draft_tool_bindings.py`, verify native tool, Skill and
  exact MCP action coverage, bounded repair for missing custom Agent bindings,
  and compatibility for older drafts without declared requirements. A declared
  requirement is never an execution grant.

- Agent binding recovery: run `tests/test_workspace_creation_binding_repair.py`,
  `tests/test_workspace_draft_tool_bindings.py`, `tests/test_workspace_draft_guards.py`,
  `tests/test_workspace_creation_modes.py`, and the concurrent-finalize case in
  `tests/test_workspace_mechanism_regressions.py` with an isolated test database.
  Verify expanded bindings, bounded repair, preserved valid capabilities and
  settings, actor and service scope, quota rejection, failed validation,
  concurrent edits/finalization, and quiet-stream keepalives. Run the shared
  draft frontend source checks and build; inspect repair progress at desktop
  and mobile widths. Blueprint and missing-user-choice failures remain outside
  automatic repair, and no Workspace may materialize before verified readiness.

- Integration selection regression: run `tests/test_workspace_draft_tool_bindings.py`
  and `tests/test_workspace_operation_domain_validation.py`. Verify a new
  conversational draft cannot select an unconnected MCP provider or external
  channel, and a reviewed Workspace Chat add cannot apply after its connection
  becomes unavailable. Blueprint dependency todos retain their install contract.
- Approval-first defaults regression: run `tests/test_governance_approval_defaults.py`,
  `tests/test_workspace_draft_approval_defaults.py`, and
  `tests/test_blueprint_approval_defaults.py` with an isolated test database.
  Cover policy-only/rule-only/capability-only bans, all three presets,
  resource-qualified and wildcard overlap, source rebuild, no-identifier calls,
  model receipt forgery, genuine typed manual bans, and empty default grants.
  Exercise install, Draft finalize, reviewed upgrade with keep-yours, simulation
  promotion, export and reinstall. Published payload/fingerprint and existing
  operator choices must remain unchanged; unattended consent must not import.
  Browser QA uses a new paused synthetic Workspace with no external bindings;
  inspect approval-required rules and probe real policy decisions without
  publishing, scheduling or altering an existing operator Workspace.
- Conditional rule regression: run `tests/test_workspace_hard_block_requires_user_request.py`
  and `tests/test_workspace_rule_enforcement_conflicts.py`. Missing/conditional
  enforcement scope cannot create a new hard block; explicit all-action bans
  retain the user quote check, and existing stored rules remain unchanged.
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

- Context checkpoint recovery: run `tests/test_context_checkpoint_recovery.py`
  with context compaction scenarios, runtime context management, cancellation,
  and durable Chat compaction suites. Verify actual packed-request admission,
  one shorter retry, hard-window fallback boundaries, unchanged source history,
  next-batch output headroom and usage/cancellation on the configured route.
  Provider mocks prove these control paths, not live summary quality.

- Workspace operation guides: run `tests/test_workspace_operation_skills.py`,
  `tests/test_builtin_ledger_skills.py`, `tests/test_skill_execution_kind.py`,
  `tests/test_prompt_builder_routing.py`, `tests/test_ai_runtime_harness.py`,
  and LLM cache/native-adapter regressions. Verify all Workspace actions and
  canonical configuration patches are covered; App links load the App guide;
  guide caches invalidate on source/dependent-tool changes; instructions-only
  loading starts no child model; revocation/manual selection/external surfaces
  preserve their scope. Compare shared prefixes across actors without sharing
  live state. Provider cache-hit rates and real model route accuracy require
  separate live observation; model stubs do not prove them.
  Run `tests/test_workspace_skill_integration_contract.py` plus integration
  scope/removal/domain/review-fingerprint regressions. Verify public schemas and
  guide constraints match registered handlers, legacy metadata edits cannot
  bypass review, and connected-provider add/remove/restore persists through
  the Chat gateway and durable review. Revoked connections fail at apply.

- Group `@Agent` automated/integration: run `tests/test_chat_rooms.py` on an
  isolated PostgreSQL database and the human chat UI source test.
  Verify an active exact-email-verified Manor user receives an in-app invitation
  and has no group membership before explicit acceptance, including across
  Entities. Unknown and unverified addresses receive branded email (or an
  explicit manual-link fallback when SMTP is unavailable) and no membership
  before acceptance. Owner/admin cancellation and reissue invalidate old links
  and notifications. A removed member rejoins as an ordinary member with a
  new visibility floor. None of these paths adds Entity or Workspace membership.
  Open the link signed out and confirm registration, email proof, and explicit
  group acceptance retain the exact invited address. Read cursors count only
  other senders' post-join messages, advance no further than a displayed
  message, and cannot be changed by an outsider or a member of another room.
  Retracting a sender's message retains its audited original row but returns a
  tombstone, removes it from search/unread, redacts reply previews, and blocks
  new reactions, replies, and Agent source admission. Members can leave; the
  owner cannot abandon room custody without a transfer or closure. Test a
  100-member room with 20 parallel human sends and no lost/duplicated messages.
  An explicit room binding ID creates one durable `ChatRoomAgentRun`, not a
  name-parsed invocation; plain `@` text remains human chat. Stub the enqueue
  path to prove concurrent sends retain distinct source IDs and
  sender IDs, duplicate deliveries claim a run once, overdue pending runs
  are redriven with bounded dispatch, and stale running runs interrupt without
  replaying a possibly completed file action. Verify the `room-agents` queue
  has a consumer and its recovery sweep remains off long-running work slots.
  Stub the model
  and credit gate to prove external guests cannot select unbound or cross-owner
  Agents, a repeated worker cannot bill twice, and AI reply attribution remains
  after binding removal. Assert `PUBLIC_CUSTOMER_CHAT`, no file tools when the
  owner-controlled file setting is off, group-common visible history only,
  shared minimal Chat output/tool guidance without personal, Entity, Workspace,
  or Agent-file prompt content,
  owner-Entity billing, and no provider call
  after sender, audience, binding, authorization or Workspace revocation.
  The owner-only, revisioned AI execution setting defaults off and must reject
  admin, member, outsider, DM, closed-room, and stale conflicting writes.
  Historical enabled rooms must not silently opt in to direct mutations;
  owner opt-in advances the revision and retires old waiting requests without
  executing them. With the setting enabled, every active member turn receives the room file
  allowlist. Read/list accept room attachment IDs only; reject cross-room,
  pre-join, tombstoned, changed-hash, and private Knowledge targets. File
  create/replace/rename/remove in legacy mode must wait for a live owner/admin
  to approve exact arguments. In direct mode they need a durable exact-call
  claim before side effects, but no per-call review. Reject stale setting,
  membership, audience, binding, or execution actor. A claimed operation must
  not run twice after ambiguous retry.
  Every PDF/DOCX/PPTX/XLSX request, including one with model draft content, must hand off the existing built-in
  file Skill to its exact run-bound sandbox. Verify no sandbox is callable
  before handoff or after the file grant is revoked; reject generic create,
  foreign sandbox IDs, Workspace imports, external URL saves, wrong output
  formats, and paths outside the Skill. Save a final quality-gated file to the
  group folder under the claimed receipt, then publish only its projected
  Document. Code text or an unsaved sandbox file is not completion; timeout
  and failed saves leave the receipt failed and cannot replay the operation.
  Verify a member's current/quoted attachments, visible history, selected room
  files and actual public results enter as original bytes with provenance and
  hash, while over-limit, inaccessible or changed sources fail closed.
  Upload a file over the former 100 MiB ceiling, accept up to 1 GiB, reject
  larger files, and serialize uploads at the 10 GiB room total. Keep inline
  storage through 100 MiB; larger uploads and Knowledge shares use bounded
  4 MiB database chunks with exact size/hash checks. Count both inline and
  chunked copies from local upload and Knowledge share against the owning Entity's Knowledge
  storage plan across personal and Entity rooms; retain closed/removed bytes
  and avoid double-counting generated Documents.
  Batch downloads accept up to 100 files totaling 5 GiB, recheck each file's
  current room authority, and stream a server-prepared ZIP without accumulating
  its contents in browser memory. Verify cancellation and failure release
  private snapshots and shared disk reservations.
  All active members may request web search; the model decides whether to use
  the tool, and its query must be copied from the sender's latest message.
  Test that search never derives a query from group
  history, another member's message, or an attachment; uses the exact current
  topic with the intended search backend; and rechecks membership, binding,
  and audience before external I/O. Ordinary members still cannot read
  arbitrary URLs or use file tools.
  Generated files must be private Documents under the room-id-suffixed
  `Group chat files` folder and appear in the group's Files tab only after
  a succeeded claim receipt; test immutable hash/size downloads,
  idempotent projection, group rename, group close, and replacement/version
  behavior. Keep unsupported media and cross-Entity personal-admin writes
  fail-closed until their explicit policy is implemented.
  Browser/manual: mention Manor AI and one bound Agent from a multi-member
  group, observe exactly one attributed reply each, then change membership or
  remove the binding during a delayed turn and verify no late reply appears.
  A Workspace Agent with `external_message.send` set to approval-required must
  remain addable, freeze its reply without publishing, expose the exact draft
  only to a room owner/admin with Workspace external-publish authority, and
  publish it once after approval without repeating model work. Wrong digest,
  unauthorized approver, duplicate decision, rejection, hard policy block,
  and revoked approver/binding/Workspace scope must publish nothing.

- Unified discovery and operation evidence: run `tests/test_discovery_evidence_boundaries.py`,
  `tests/test_manor_search_bridge.py`, `tests/test_capability_semantic_contract.py`,
  `tests/test_chat_capability_evidence.py`, `tests/test_workspace_response_contract.py`,
  and `tests/test_tool_surface_executable.py`. Prove both discovery entrypoints
  retain scoped native/composite capabilities, matcher failure preserves unknown
  status and catalog descriptors, and aliases load schemas in the same loop.
  Unknown inventory categories must not become empty results; evidence for one
  operation cannot prove a different operation blocked/completed. Checkpoint
  evidence is current-run only. Use provider stubs for regression; report live
  discovery separately and never create a business schedule as a read-only probe.
- Chat action selection: run `tests/test_general_chat_intent.py`,
  `tests/test_prompt_builder_routing.py`, `tests/test_chat_mode_video_routing.py`,
  `tests/test_runtime_chat_context_byok.py`, and the Chrome local browser
  contract tests. Vary wording without changing structured mode, attachment,
  surface or authorization; tool and Skill scope must stay available. Past
  keyword-overlap tool paths must not inject next-action hints.
- Tool success versus turn completion: run `tests/test_video_auto_wait.py`,
  `tests/test_workspace_response_contract.py`, `tests/test_skill_tools.py`,
  `tests/test_media_job_warning_receipts.py`,
  `tests/test_skill_execution_observations.py`, and
  `tests/test_nested_media_completion.py`, plus
  `tests/test_child_execution_projection.py`. A successful asset generation must allow the next
  assembly/inspection call in normal, forced and resumed loops, preserve batch
  siblings and reach the caller's final/output contract. Parent handoffs retain
  child stop reasons and bounded tool observations, including warnings from
  completed provider jobs. Keep raw business output intact; compaction belongs
  only to the model-context projection. Preserve explicit terminal policies, HITL, errors,
  no-effect proof and tenant/run isolation. Model-stub success proves control
  flow only, not narration, subtitle quality or a real rendered artifact; live
  media acceptance is a separate check and must not replay paid generation.
- Shared foreground completion: run the web `chat-stream-lifecycle`,
  `chat-stream-store-lifecycle`, `chat-stream-stop`, `chat-stream-resume`, and
  `assistant-process-presentation` tests. Hold the HTTP body open after a scoped
  successful `stream_end` with queued text: the full answer must be visible and
  the live indicator stopped immediately in ordinary and Workspace Chat.
  Preserve same-chunk terminal metadata, ignore foreign terminal and late text
  frames, avoid recovery polling after completion, and retain HITL, cancellation,
  and newer-turn isolation.
- Process lifecycle and clock: run `tests/test_assistant_blocks.py`,
  `tests/test_assistant_blocks_final_text.py`, and `tests/test_assistant_blocks_lifecycle.py`.
  Verify a new
  reply cannot restart historical clocks, equal prose cannot merge distinct
  message IDs, a long nested tool/remount preserves the server start time, and
  terminal timestamps stay frozen. Real failures and waiting remain distinct;
  completing a turn never deletes failed or unverified attempt evidence.
- Nested Chat outcome evidence: run `tests/test_chat_nested_outcome_contract.py`,
  `tests/test_chat_capability_evidence.py`, `tests/test_discovery_evidence_boundaries.py`,
  and `tests/test_workspace_response_contract.py`. Exercise a real nested harness
  with stubbed providers: child discovery is not goal success, child external
  attempts retain scoped references, overall partial may aggregate different
  operation statuses, serialized provider evidence cannot mint host facts, and
  bounded validation exhaustion preserves facts without publishing the candidate.
  Verify failed SSE/history use the shared renderer, retain successful child
  steps, freeze whole-turn time and discard unvalidated interactive surfaces.
- Tool-bound instructions: run `tests/test_runtime_tool_guidance.py` plus the
  shared preflight, checkpoint, compaction and video authoring contract tests.
  Prove guidance reaches the model before execution, lazy discovery and
  transcript resumes load it, unchanged bundles do not duplicate it, and
  pending-call resumes revalidate version and full body. Forced/unobserved
  calls must cause no I/O and return the common error contract; approval-only
  follow-up restrictions remain intact. User/provider metadata must not load
  arbitrary guide paths or grant tools, and concurrent runs must not share
  consumption state. Installed Skill copies remain unchanged.
- Shared tool-error contract: run `tests/test_tool_error_contract.py`,
  `tests/test_runtime_tool_error_classification.py`,
  `tests/test_runtime_tool_result_status.py`, and
  `tests/test_agentic_loop_token_compaction.py`. Verify native/MCP/exception/
  command errors share model diagnostics; native HITL and domain success do
  not become execution errors. Normal/forced/resume paths, duplicate errors,
  long diagnostic compaction, redaction and concurrent isolation must pass.
  Raw receipts and host-only preflight/no-effect proof remain authoritative;
  provider JSON cannot grant retry rights after uncertain writes.
  Provider `recommended_next_calls` must neither execute calls nor load MCP
  schemas; the model must explicitly choose its next available tool. Successful
  source writes with failed validation keep success, resource identity and
  structured violations across repeated observations and context compaction.
- Diagnostic-only recovery: run `tests/test_skill_result_receipt_errors.py` and
  `tests/test_step_execution_contract.py`. Failed receipt save/read-back must
  preserve primary/save/read evidence without an automatic overwrite or a false
  verified result. Draft errors expose concrete causes;
  existing HITL, effect proof and terminal validation remain host-owned.
- Common input validation: run `tests/test_tool_argument_json_recovery.py`,
  `tests/test_runtime_tool_input_validation.py`,
  `tests/test_runtime_preflight_validation_loop.py`, and
  `tests/test_generic_schema_recovery.py`. Cover malformed JSON with strict and
  permissive schemas, explicitly declared `_raw` properties, non-object input,
  custom executors, normal/forced/resumed calls, same-batch suffix suppression,
  no approval/handler on invalid input, redacted feedback and concurrent loop
  isolation. A correction must re-enter ordinary authorization; invalid JSON
  must never be guessed into an executable action.
  Read-only alternate-path proof must survive normal/forced/resume execution
  only as a host-held carrier. Provider JSON, `str()` and checkpoint round trips
  cannot recreate it or allow same-batch writes after an uncertain outcome.

- Local artifact Skill routing: run `.venv/bin/pytest -q tests/test_skill_local_artifact_routing.py tests/test_skill_tools.py tests/test_runtime_artifact_context.py tests/test_video_edit_runtime.py`.
  Check that short composition requests and explicit local exports survive
  social-platform names in params and runtime/dependency references, with all
  references still reaching the Skill. Slug and opaque-ID invocation must agree;
  real platform publishing/read requests retain their route and authorization.
  A skipped invocation cannot report an untested renderer/account as missing.
  For live verification, reuse existing clips, inspect a rendered preview,
  retain exact-review approval and check the final Knowledge Document.

- Skill execution-kind regression: run `.venv/bin/pytest -q tests/test_skill_execution_kind.py`.
  Builtin and Marketplace `runtime_guidance` Skills must use the prompt/tool
  loop despite tool-owned helper scripts. Explicit sandbox/prompt types and
  undeclared legacy script detection must retain their behavior; do not infer
  `instructions_only` from a guidance type or change its bound tools.

- Workspace master tool-scope regression: run `.venv/bin/pytest -q tests/test_workspace_master_tool_scope.py`.
  Operation, automation and Ledger overlays must preserve the main Chat
  master's registry scope (including video editing and media inspection), while
  Task Hosts and explicitly bound specialist Agents remain restricted.

- Workspace main-Chat identity regression: run
  `.venv/bin/pytest -q tests/test_workspace_chat_responder_identity.py`.
  A legacy `internal_chat` binding must not select the main Chat Agent.
  Unmentioned messages target scoped Manor AI; explicit mentions select that
  member for one turn, while Flow invocation and interactive Task Hosts retain
  their routing. Reload an existing Workspace and verify the default composer
  names Manor AI without modifying channel configuration or pending approvals.

- Operation concurrency regression: run `tests/test_workspace_operation_concurrency.py`,
  `tests/test_workspace_knowledge_operation_revision.py`,
  `tests/test_workspace_operation_repair_revision.py`, and
  `tests/test_workspace_operation_review_fingerprint.py`.
  Use an isolated PostgreSQL database and competing transactions:
  two successful edits must both survive; apply, discard, and edit must preserve
  the winning terminal receipt; changed/legacy review cards cannot approve new
  contents. A Knowledge configuration change must invalidate an older draft,
  while identical retries and independent Workspace edits remain usable.

- Workspace file-tool contract: run `tests/test_workspace_file_tool_contract.py`
  and `tests/test_workspace_file_maintenance_scope.py`. Verify that provisioning
  persists protected source readers with file writers; historical create-only
  bindings do not acquire read access; disabled definitions and explicit
  revocations remain denied; Workspace/service grants do not leak to other
  deployments; stale execution snapshots cannot preserve a removed grant.

- Saved AI language regression: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_auth_locale.py tests/test_ai_response_language.py tests/test_runtime_response_language.py tests/test_ai_generation_actor_scope.py`.
  Verify account persistence, current preference on a new turn/resume, nested
  Agent and helper inheritance, concurrent actor isolation, generated artifact
  text, preserved structured output, and external recipient language. These
  request tests stub providers; they do not prove actual model compliance.

- Restriction repair regression: run `tests/test_workspace_rules_search_provenance.py`,
  `tests/test_workspace_operation_effective_policy.py`,
  `tests/test_workspace_operation_review_diff.py`, and
  `tests/test_workspace_operation_tool_surface.py`. Search by action/capability
  retains each source rule. Preview, validation and apply agree; same-value
  rule updates are no-ops even with unrelated live grants, and remaining HITL
  requirements survive removal of a hard block. Legacy cards must not invent
  removals without evidence. Browser QA uses a paused synthetic workspace:
  approve the exact rule change, reload, repeat the request, then probe the
  real policy decision. Expect human approval, not hard denial or auto-execution,
  with no Task, schedule or publication created.
- Direct upload conversion regression: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_chat_attachment_read_access.py tests/test_assistant_conversion_retry.py tests/test_file_conversion.py tests/test_sandbox_cleanup_control.py`.
  Verify exact attachment reads, follow-up turns, cross-user/conversation/entity
  denial, deleted/restricted sources, unchanged input digests and normal output
  approval. Retry success retains error history without reporting an unresolved
  failure; unrelated/differently scoped successes must not clear it. Browser QA
  must upload distinct source bytes directly, without Knowledge fallback.


- Automated Voice HITL: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_voice_approvals.py tests/test_voice_hitl_api.py tests/test_voice_hitl_transport.py tests/test_voice_hitl_integration.py tests/test_voice_work_actor.py`.
  Use an isolated `TEST_DATABASE_URL` for the integration and actor suites; their fixtures
  recreate schema and clear data. The other suites use synthetic provider
  events and mock database boundaries, not microphones or live models.
  Verify Main pending data survives both Live transports, final ASR overrides
  model arguments, and a pending confirmation cannot disappear into silent
  wait or combine with an earlier instruction. Input captured before a prompt
  is presented cannot answer it. Exact approve/reject responses preserve the
  original Chat user text and use the existing resolver; repeating a response,
  multiple cards, a changed/deleted card, and unauthorized users cannot execute
  another operation. Login/MFA/unsupported requests retain UI handoff.
  Bare agreement cannot approve one of multiple cards through a model-selected
  ID; explicit numbered replies use the exact captured list order, including
  when the newly presented list differs from the earlier order. Recovery
  keeps the exact approval snapshot and presents through Live; no raw Main
  result, fingerprint, token, or execution arguments become a spoken command.
  In a shared conversation, caller B cannot recover or claim caller A's pending
  approval, or interrupt A's running receipt; Main rejects a foreign origin
  before invoking the approval resolver. A can still claim their own pending
  work, and public/channel sessions retain their existing scope contract.
- Automated native Browser admission/status: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_browser_voice_admission.py tests/test_browser_voice_pending_decisions.py tests/test_browser_voice_status_history.py tests/test_voice_work_sources.py`.
  The source-promotion suite requires an isolated test PostgreSQL database;
  the Browser suites use synthetic events and callbacks, not live models or microphones.
- Automated: `.venv/bin/pytest -q tests/test_task_session.py tests/test_response_surfaces.py tests/test_workspace_chat_pending_count.py tests/test_workspace_chat_pending_visibility.py tests/test_workspace_authz_holes.py tests/test_workspace_write_authz.py tests/test_runtime_permissions_roles.py tests/test_workspace_mechanism_regressions.py -k 'chat or feedback'`; plus `.venv/bin/pytest -q tests/test_ai_runtime_harness.py tests/test_agent_provisioning_marketplace_skills.py -k 'agent_provisioning_tool or runtime_requester or runtime_search or progressively_loads_'`, `.venv/bin/pytest -q tests/test_runtime_default_tool_authorization.py tests/test_agentic_loop_token_compaction.py tests/test_runtime_tool_input_validation.py -k 'runtime or search_tools or tool_pool'`, `.venv/bin/pytest -q tests/test_capability_search.py tests/test_builtin_ledger_skills.py tests/test_chrome_skill_run_contract.py`, `.venv/bin/pytest -q tests/test_webchat_page.py`, `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_whatsapp_webhook.py tests/test_channel_link.py`, `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_voice_work_router.py tests/test_voice_work_queue.py tests/test_browser_voice.py tests/test_gateway_voice.py tests/test_twilio_realtime_voice.py tests/test_twilio_voice_runtime.py tests/test_channel_voice_cancellation.py`, and `.venv/bin/pytest -q tests/test_generic_email_mcp.py tests/test_generic_channel_callback.py tests/test_email_attachment_bridge.py`
- Manor AI tool inheritance regression: `.venv/bin/pytest -q tests/test_ai_runtime_harness.py -k 'workspace_manor_ai_inherits or workspace_tool_surface_policy'`. Verify Workspace Manor AI retains every ordinary owner-Chat capability and visible tool, while non-master Workspace Agents do not acquire unbound Manor, CLI, file-write, or integration permissions.
- Automated failure-control contract: `.venv/bin/pytest -q tests/test_ai_engine.py tests/test_video_auto_wait.py tests/test_skill_tools.py -k 'stop_parent_error or terminal_policy_cannot_turn_failed or forced_media_error or local_coding_skill_dispatch or approved_forced_call_aborts or structured_skill_output or nested_tool_error_control or invent_setup_link or keeps_nested_tool_error or manual_web_skill_tool_error'`
- Tool-error self-repair: run `tests/test_agent_tool_error_self_repair.py`
  alongside the failure-control tests. Verify ordinary/forced/resumed errors
  retain detailed diagnostics and tools, nested failure controls do not end the
  parent, the model can edit, check and render after an error, repeated error
  rounds stay within the total round budget, unexecuted batch suffixes are
  discarded, and every new call keeps its authorization, cancellation,
  successful terminal-tool and strict persistence boundaries. Mocked model/render
  tests prove control flow, not actual model self-repair or rendered delivery;
  live QA must inspect the real tool-call records and output before claiming either.
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
  unlock input. Workspace Manor AI has the ordinary owner-Chat permission
  baseline plus Workspace-specific capabilities; custom/service Agents remain
  binding-scoped. Deleting a Task leaves no rateable completion projection or
  completion-feedback evidence for that Task. Runtime code neither guesses a
  completion target from prose nor repairs an old-worker row. Response surfaces
  survive refresh, keep metadata events anchored to the correct assistant
  message, degrade to their text fallback on render failure, and cannot bypass
  normal Chat runtime scope or approval boundaries.
  WhatsApp phone routing resolves one exact active binding, rejects ambiguity,
  and never accepts a contact-level subscription override.
  Browser Voice upserts input-transcription deltas into one visible partial
  user line, replaces it with the completed transcript, preserves committed
  input items across later speech starts, and starts one Realtime response from
  committed audio without waiting for final ASR or creating another response
  when ASR completes. The Call inherits the current bound Agent prompt and the
  ordinary Chat context manager's normal compacted Conversation history,
  answers no-tool conversation directly, and delegates tool/private-state/
  approval/persistence work exactly once to the ordinary scoped Chat Agent.
  Native Browser retains the unhandled committed-input group when a newer
  speech generation cancels its response. A full instruction followed by an
  empty or nonempty tail must still admit one request containing all finalized
  authoritative parts in capture order, never the model's utterance argument.
  Admission waits even when the first part's final arrives after the tail and
  bridge decision; duplicate finals cannot erase accepted text or repeat work.
  Save every nonempty part once, validate original source IDs/text/ownership,
  and promote a saved row with the ordered source snapshot instead of creating
  a combined duplicate user message. Exact retries reuse that work, while
  overlap, foreign sources, reordered text, and terminal restart are rejected.
  Two pending work decisions for one group must admit only the newest combined
  request in either final-ASR order. Status and its spoken answer must preserve
  earlier decisions awaiting final ASR. Explicit cancel consumes the pending
  group; completed direct answers consume only inputs without pending decisions.
  Silent wait preserves unfinished input. A later empty item cannot revive a
  consumed request. An entirely empty group returns structured
  `input_unavailable` for Live recovery with `work_started: false`, not silent
  discard or Main execution. Clear only that finalized-empty group immediately,
  even if recovery speech is interrupted; repeated empty groups cannot exhaust
  the pending-input bound or consume other pending requests.
  These native admission changes leave the Gateway
  fallback, ordinary scope/approval/credit gates, and Blueprint contracts
  unchanged and add no inference step or schema migration. Final assistant text
  includes the response-completed tail even if delta delivery was incomplete.
  Interrupting an assistant response retains the final user transcript exactly
  once in durable Chat whether ASR completes before or after interruption,
  without recording an unheard assistant answer or executing an abandoned tool.
  Model-specific startup budgets retain Agent identity and compacted summaries;
  excess ordinary history is pruned without an extra inference hop.
  Run `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_voice_context_budget.py
  tests/test_voice_message_limits.py tests/test_voice_wire_startup.py
  tests/test_voice_startup.py` for the wire-budget boundary. Long ASCII, CJK,
  emoji, and escaped history that fits model tokens must still fit the complete
  Vercel UTF-8 JSON message. Protected identity/summary cannot be silently cut;
  native OpenAI retains its own token budget. Exact-limit messages succeed,
  oversized batches send nothing and preserve the accepted session, and
  deterministic size errors do not spend another unchanged setup attempt.
  Loopback WebSocket tests enforce the real receive-size boundary and prove
  long-history Browser startup plus result/foreground config changes stay
  native; they do not claim live-provider or microphone end-to-end coverage.
  Diagnostic logs contain byte counts, never private prompt/result content.
  Live is instructed to delegate without a spoken preamble; its final structured function output is
  rendered once by the same Live Agent, while raw Main Agent output is never
  spoken directly. A valid native late bridge decision allows buffered
  preamble audio to drain without a handoff interrupt, overrides direct-answer
  completion, waits for the finalized pending input group, and is admitted
  exactly once. Real user barge-in must still interrupt and acknowledge the
  actual played position. Cancelled, superseded, mismatched,
  and duplicate responses cannot execute stale work. Live delegates capability
  checks rather than treating its three bridge tools as the full Agent's
  capability limit; missing access claims require authoritative evidence and
  delegation cannot expand scope. Native and Gateway prompts include the same
  generic Main platform capability overview even without inherited tool context;
  the overview is not a whitelist or an account-readiness claim. Context
  resolution still disables execution-tool/account/Skill catalogs, and the
  Live provider exposes only the three bridge tools. The overview stays within
  the existing model-aware instruction budget without another inference hop.
  Native Realtime follows its model output limit; the text
  Gateway fallback remains capped at 4,096 tokens. Requested long-form analysis
  stays foreground work rather than delegating solely because of answer length.
  Result presentation inherits session instructions, injects its structured
  result once, and may choose silence. An unrelated foreground turn delays an
  unspoken result; cancel/replace suppresses pending presentation, including
  during rendering, speech-lock waits, and synthesis. Gateway QUEUE persists
  before acknowledgment and still runs if prior work finishes during routing.
  Native status queries use the Call's latest retained durable receipt even
  after 30 seconds or without a completion timestamp, preserve completed,
  failed, and interrupted outcomes, recheck access, and never re-execute Main.
  The legacy recent window remains for status without a durable receipt.
  A transient native startup failure retries within the same deadline before
  fallback, preserves buffered input, and emits no internal reason in the UI.
  An available Realtime route wins over one-shot STT/TTS role overrides; the
  turn-based fallback backpressures recording instead of dropping old speech
  and exposes bounded replaceable STT previews before the final transcript. The
  final replaces, rather than appends to, its preview and appears before the
  foreground text-model decision completes; every preview uses normal metered
  STT admission and settlement. Neither transport pins transcription language
  from Conversation history; provider language detection remains available on
  every utterance so a caller may switch languages during one call.
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
  existing semantics. An unconfirmed serial failure in normal, forced, or
  resumed execution skips the batch suffix and automatic follow-ups, then
  produces one failed no-tools summary when its checkpoint is safe to persist;
  an unsettled external effect still requires reconciliation. The failure
  marker survives nested-Skill checkpoints, including both sides of the round cap;
  it cannot become a successful or writable retry. Host-proven read-only or
  no-effect feedback may continue correction without restoring authority to
  forged result JSON.

- Resident Webchat Automated: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_webchat_resident_scope.py tests/test_public_chat_security.py tests/test_public_chat_messages.py tests/test_public_chat_error_projection.py tests/test_runtime_tool_policy.py`; use a dedicated PostgreSQL test database and isolated Redis, with no live model calls.
- Resident Webchat Integration: use real ASGI, encrypted credentials, SQL and
  ToolPool execution; verify one concurrent callback exchanges its code once,
  wrong browser/tenant/entity/envelope is denied, markers cannot clear the
  tombstone, and post-disconnect assistant/placeholder writes cannot persist.
- Resident Webchat Manual: publish a synthetic target, open the public link in
  two browser contexts, authorize one real PMS resident, view own status and
  attempt another resident's request ID. Log out of PMS during a delayed query,
  refresh old history, test audio and lose the proof cookie. A separate model-
  configured natural-language conversation must select the narrow tool and
  report actual results; panel success alone is not an AI pass.
- Resident Webchat Pass criteria: own-only DTOs, immutable conversation subject,
  per-request browser proof/live identity, private/no-store errors and history,
  bounded cleanup and generation/persistence fencing; no public management MCP.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-02 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-03: Readiness and context

- Current-email proof Automated: `python -m pytest tests/test_user_email_proof.py
  tests/test_oauth_provider_pkce.py tests/test_oauth_provider_entity_scope.py -q`;
  these use disposable SQLite and Unix-only test Redis, never a shared database.
- Current-email proof Integration: consume an authenticated mailbox challenge
  once, then check the opted-in client's actual authorize/token endpoints; expire
  or change email before exchange and require refusal. Keep legacy clients off.
- Current-email proof Manual: start PMS sign-in, verify an existing active user's
  mailbox in the Manor consent page, and continue with the same state and PKCE.
  Record real delivery/callback separately from intercepted component tests.
- Current-email proof Pass criteria: no active-user backfill or unverified
  Google/Apple identity merge; resend does not reset attempts; mailbox/session
  changes fail closed; no new login/MFA/approval/credit authority is created.
  User proof and OTP state are identity runtime data, excluded from Blueprints.

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py`; plus `.venv/bin/pytest -q tests/test_workspace_readiness.py tests/test_strategist_briefing_integration.py tests/test_workspace_knowledge_visibility.py tests/test_knowledge_visibility.py tests/test_workspace_mechanism_regressions.py tests/test_remote_mcp.py tests/test_integration_operation_catalog.py tests/test_runtime_integration_account_registry.py -k 'strategist_provider_scope_ignores_internal_and_manual_measurement_sources or live_remote or account_tool_catalog or provider_catalog_merge or integration_account'`, and `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_integration_health_credential_errors.py tests/test_whatsapp_business_provisioning.py`
- App materials intake: run `tests/test_workspace_user_materials_setup.py` and
  `tests/test_workspaces.py::test_workspace_owner_can_answer_materials_setup_without_bypassing_file_check`.
  Confirm a selected/uploaded Document is attached through Knowledge permissions,
  a text answer applies only to its declared check, and Workflow launch remains
  blocked until the live setup status is ready.
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

- Resident Webchat Automated: run `tests/test_webchat_resident_scope.py` owner
  authority/target lifecycle cases with the WS-02 admission suite.
- Resident Webchat Integration: live userinfo must provide a strict integer
  remote Entity and match the selected owner's Manor identity; revoke local
  membership or provider authorization and retry without a prior health click.
- Resident Webchat Manual: configure a connected target without an LLM key and
  distinguish working resident verification from blocked natural-language Chat.
- Resident Webchat Pass criteria: only owner-selectable credentials form private
  target state; target readiness never implies model, credit or approval readiness.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-03 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-04: Strategist and Proposal generation

- Skill prerequisite context: run `tests/test_strategist_skill_binding.py` and
  `tests/test_required_skill_recovery.py`; only active owner-bound scoped Skills
  appear, required inputs survive install/export, and exact Task blockers remain
  available to the briefing path without copying schema defaults.

- Rejection/failure recovery: run `tests/test_strategist_generation_failures.py`,
  `tests/test_strategist_scheduled_generation_failure.py`,
  `tests/test_strategist_decision_loop_e2e.py`,
  `tests/test_strategist_meta_learning_suppression.py`, and
  `tests/test_monitor_strategist_fallback_retry.py`. Invalid JSON/schema output
  receives detailed shared-contract feedback once, then fails without Proposal,
  Task, or successful watermark; a later explicit review can recover the same
  evidence window. Missing/stale/wrong-scale measurements do not inject scorer
  Tasks. Repeated monitor ticks do not retry legacy fallback reviews, while real
  readiness changes retain their normal dispatch.

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py tests/test_strategist_template.py tests/test_strategist_deliverables.py tests/test_strategist_artifact_evidence.py tests/test_strategist_decision_loop_e2e.py tests/test_review_trigger_kind.py tests/test_change_kinds.py tests/test_automated_run_approvals.py`
- Integration: run one human-requested review against fixed Goal configuration
  and measurements; assert typed Proposal validation, Task ownership,
  dependencies, deliverables, stable Goal-change identity, and one chat card.
- Manual: run a Workspace until a Proposal appears; inspect Summary, Task titles,
  owners, expected artifacts, and absence of duplicate open proposals.
- Pass criteria: Proposal is context-derived, schema-valid, bounded, and stored
  once; credit failure or invalid output creates no partial Task cohort.

## WS-05: Proposal governance

- Code-only refusal: verify `WRONG_DIRECTION` without a comment reaches both
  prompt paths with the Task title, is classified as rejected rather than
  abandoned, and persists through the ledger into the next review briefing.
  Old cached briefings without title/reason-code fields remain readable. A
  rejected task is never regenerated merely because a score is missing.
- Scheduling/approval separation: run `tests/test_proposal_approval_migration.py`
  and `tests/test_workspace_creation_modes.py` with an isolated test database.
  Automatic reviews must retain pending Proposal actions absent a real grant;
  implicit scheduling grants do not survive migration, explicit grants and
  unrelated policy do, and rerunning migration is idempotent. Verify both prompt
  paths share cohort governance, concurrent Settings edits do not lose grants,
  and simulation promotion/export never leaks temporary consent. Auto-approved
  cards display a typed status/reason, not renewed buttons for historical work.
- Automated: `.venv/bin/pytest -q tests/test_proposal_items.py tests/test_proposal_card_item_cohorts.py tests/test_proposal_reject_reason_api.py tests/test_approval_matrix_api.py tests/test_standing_grants_api.py tests/test_automated_run_approvals.py`
- Integration: exercise allow, needs-human, deny, partial selection, and an
  external-action cohort; assert exact Task/workflow dispatch behavior.
- Manual: approve and reject separate proposals from Workspace chat; refresh and
  verify card state and Task state remain consistent.
- Pass criteria: one auditable decision per cohort; rejected items never run;
  approval cannot authorize changed arguments or another cohort.

## WS-06: Task lifecycle and planning

- Blueprint setup admission: run `tests/test_workspace_setup_task_admission.py`,
  `tests/test_workspace_setup_gates.py` and `tests/test_blueprint_startup_service.py`.
  Verify that the exact active setup run's child stays on the coordinated
  runtime and reaches the ordinary billing gate, while forged child markers,
  changed contract/job revision, cancelled runs, paused/deleted Workspaces and
  inactive Agent subscriptions stop execution. App
  status must use the latest actual occurrence and a Workspace-scoped Task link,
  even when the ScheduledJob summary is empty. Normal Tasks remain blocked.

- Task agent-loop control: run `tests/test_task_coordinator_wakeup.py` and
  `tests/test_task_agent_loop.py`. Verify normal Workspace Task admission
  creates/reuses one durable TaskCoordinatorRun pinned to its factory identity
  and Workspace scope, a stale generation or claim cannot claim, checkpoint or
  ACK a newer wake, and retries/wakes do not duplicate billing, delegation, or
  terminal effects. Include `tests/test_planner_mcp_runtime_scope.py` and
  `tests/test_agent_effective_capabilities.py`: cached actions respect exact
  names and Workspace disabled-provider exclusions without dropping dynamic
  wildcard bindings, and capabilities never transfer between services or
  subscriptions.
  Blocker projections must preserve system audit identity
  while naming the actual affected Agent; old logs are not current authority.

- Runtime selection: run `tests/test_robust_planner_coordinator.py`; the
  canonical selection fingerprint must be independent of binding ordering.
  Include `tests/test_task_owner_recovery.py`: all background/custom Task
  business types must select the closed coordinated runtime factory;
  interactive and approval Tasks must remain on their explicit foreground/HITL
  adapters.

- Durable Task admission: run `tests/test_task_execution_outbox.py`. Verify Task lifecycle generations fold
  concurrent starts, broker acceptance does not ACK either delivery hop, and
  duplicate start consumers admit one Task run and one wake. Delayed
  CONTROL and HEAVY messages must not cross a future same-generation due time.
  Include Kanban and both bulk mutation surfaces plus cross-Workspace bulk
  authorization. Production business paths
  must stage a durable generation and may issue only its exact CONTROL hint;
  generationless direct wake helpers must not exist, and HEAVY delivery without
  the exact event/generation pair must fail before claiming or running a Task.

- Platform-tool scope: run `tests/test_planner_service_tool_attribution.py`
  and `tests/test_worker_proposal_runtime_metadata.py`. Verify exact service/runtime
  scope, Workspace/Task overlays, distinct deployments of a reused Agent,
  ambiguous-service rejection, and unchanged
  provider-action lookup. Treat Task required capabilities as demands against
  the selected root/service Agents' aggregate callable scope, not grants;
  missing capabilities fail before provider/billing work without falsely
  requiring the root Agent to own every service capability.

- Recovery regression: `tests/test_task_owner_recovery.py` covers Task API and
  Workspace Chat retries, identical first/retry
  service scope with/without owners, duplicate retry rejection, unavailable
  and foreign teams, deleted Workspaces, commit/rollback dispatch fencing.
  `tests/test_task_agent_loop.py` covers a retry continuing the same transcript
  and `tests/test_task_coordinator_manual_retry.py` a duplicate manual retry
  that cannot renew a running Task. `tests/test_plan_dag_retirement_migration.py`
  covers the one-time close-out of retired plan_dag work.

- Automated: `.venv/bin/pytest -q tests/test_workspace_setup_gates.py tests/test_task_session.py tests/test_task_state_machine.py tests/test_task_dependencies.py tests/test_plan_schema.py tests/test_task_output_step_contract.py tests/test_task_coordinator_wakeup.py tests/test_task_agent_loop.py`
- Integration: approve a Task with a predecessor and required artifact; assert
  pending dependency, release, and terminal producer bind.
  Attempt ordinary update, Kanban, and bulk starts for a Workspace Task whose
  factory result is `MANUAL`; each must reject before the status write and
  create no execution intent.
  Delete an interactive Task with queued and running chat work; assert its
  Runtime run tree is cancelled and its Conversation can no longer resolve.
- Manual: inspect the Task page; verify owner,
  delegates, dependencies, expected output, and approval gates are visible.
- Pass criteria: only legal state transitions occur; each Task owns at most one
  live Task run, and duplicate starts cannot duplicate provider spend;
  required output has one unambiguous producer; blocked, interactive, and
  terminal Tasks do not dispatch, and manual service keys stay within the Task
  owner/delegate scope.
  Deleted interactive Tasks leave no runnable or fallback Task Session turn.
  Task provider actions come only from the resolved Task requester's callable accounts.

## WS-07: Task execution, Dispatcher, and worker lease

- Required Skill recovery: run `tests/test_skill_tools.py` and
  `tests/test_required_skill_recovery.py`. Completed domain verdicts are accepted
  as invocation evidence; failure/wait results are not. Internal missing
  prerequisites do not blind-retry; human input retains its own gate.

- Internal file continuation: run `tests/test_runtime_file_effect_recovery.py`,
  `tests/test_runtime_file_effect_receipt_db.py`,
  `tests/test_runtime_file_effect_loop_recovery.py`,
  `tests/test_runtime_file_effect_migration.py`,
  `tests/test_dispatcher_file_recovery.py`, `tests/test_nested_skill_durability.py`,
  `tests/test_effect_checkpoint_proof.py`,
  and `tests/test_duplicate_tool_call_identity.py` against isolated PostgreSQL.
  Cover ordinary/nested result persistence before round checkpoints, lost
  return recovery from exact committed journals, no replay, bounded submission
  correction, exhausted recovery without user verification, stale owner/call/
  arguments/generation/lease rejection, concurrent scope isolation, index-backed
  receipt lookup, and no retry-budget or approval refund after a proven write.
  Cover multiple successful child writes followed by parent output failure:
  exact server-owned handoffs survive child-frame folding and resume without
  invoking the Skill or writing again. A failed second child, forged/foreign
  handoff, changed receipt or recursive witness never supplies execution proof.
  Reused model call IDs reject the whole new batch before I/O and return
  correctable diagnostics; compacted history cannot borrow an earlier committed
  transaction, and an exact pending resume remains valid.
  Run real additive migration upgrade/retry/downgrade in a disposable schema;
  legacy rows must remain unknown rather than receiving inferred identities.
  Absence, mixed or historical proof never means not-applied; database outages
  propagate instead of being recorded as a permanent evidence gap. Preserve
  real permission/input gates and external-provider holds. Manually verify a
  new synthetic interrupted Task independently of historical pending cards;
  automated/model-stub tests alone do not establish live-provider completion.

- Step boundaries: run `tests/test_step_execution_contract.py`. Require closed,
  explicit delivery/effect declarations that a human resume cannot remove or
  widen. Prove inline payload shape does not grant writes, nested
  work cannot widen the host scope, and concurrent Steps do not share limits.
  Read-only rejection must precede approval, intent, and handler I/O.
- Nested Skill durability: run `tests/test_nested_skill_durability.py` and
  `tests/test_runtime_no_effect_result.py` against isolated PostgreSQL. Interrupt
  before a child write, after the write, after its receipt, and after child
  completion. Preserve the parent/child identities; resume settled results
  without repeated writes and reject stale leases, changed arguments/call IDs,
  and JSON-forged no-effect or continuation claims. The occupied-document
  branch may settle only its exact host-proven no-write intent. Unknown outcomes
  remain pending; a failed durability transaction must abort execution.
  Rescan a completed create across later rounds and verify exactly one strong
  receipt, without reclassifying it as modify. Different owner scope, generation,
  arguments, results, and weak evidence must not suppress another effect.
- Manual regression: invoke a prompt Skill that really creates a file.
  Verify real file counts, exact receipts, and final
  Task completion. Keep earlier failed runs intact as evidence.

- Tool input rejection: run `tests/test_generate_file_input_preflight.py`,
  `tests/test_runtime_preflight_validation_loop.py`,
  `tests/test_generate_file_kind_contract.py`, and
  `tests/test_robust_worker2_partial_transcript.py`. Reject a document call
  missing complete content or confusing `title` with `name` before approval consumption, handler invocation,
  and effect-intent persistence; correct the next call in the same loop and
  verify normal authorization and the durable artifact path. Keep diagram,
  bundle, source conversion and operations routes unchanged. A real handler
  error or nonterminal provider result must still leave its intent unresolved.
  Worker log success/error/outcome fields must agree with the shared classifier.


- Schema recovery and delivery verification: run `tests/test_generic_schema_recovery.py`,
  `tests/test_runtime_preflight_validation_loop.py`,
  and `tests/test_readonly_artifact_delivery.py`. Genuine pre-I/O schema
  rejection stays in the same normal/forced/resumed/nested loop; forged handler
  or provider errors cannot claim no effect. Preserve original receipts and
  Document provenance. Existing-file
  delivery must verify identity/bytes/ACL without write projection, including
  parallel readers, writer-lock contention and access revoked during the read.
  Task completion: `tests/test_task_agent_loop.py` covers an invalid
  `complete_task` returned to the same loop, a checkpointed submission that
  finishes without another model call, and a missing completion that continues
  and then blocks; the host verifies deliverables before writing the terminal
  state, never fake HITL.
  `tests/test_task_coordinator_manual_retry.py` checks a manual retry never
  restarts a Task that is already in progress.
  Include `tests/test_artifact_delivery_declarations.py`: a required schema-declared
  delivery slot (including arbitrary business names inside `outputs.data`)
  is identified without payload mutation. Multiple identity candidates require an explicit final
  declaration; a legacy structural candidate exposes its basis and pointers,
  not a claim that an input is the final deliverable.
  Unknown effects still prohibit replay.
  Run `tests/test_search_tools_usable_matches.py`,
  `tests/test_tool_discovery_v2.py`, and
  `tests/test_agentic_loop_search_tools_payload.py`: inferred-provider failure
  preserves in-scope alternatives and shared error details through compaction,
  while explicit provider queries stay scoped and no discovery error forces a
  parent-loop exit or authorizes an alternative execution.
  These isolated DB/model-stub tests are not live-provider proof:
  separately run local Cloud browser Tasks and inspect actual
  output content, artifacts and terminal states.
  Include `tests/test_patch_file_no_effect.py` and
  `tests/test_native_artifact_output_contract.py`: missing-target patch rejection
  must persist exact no-effect proof before continuing; post-write exceptions
  and forged errors remain unknown. A valid native files[] contract (including
  open_url and business summary fields) must survive host coercion unchanged;
  invalid const/required/additionalProperties cannot be normalized into success.
  Run web source
  `task-output-delivery-fields.test.mjs` and verify the actual Task page at desktop
  and narrow widths: custom business summaries must remain visible beside files.

- Shared Agent-loop checkpoint and delegation regression: run
  `tests/test_robust_worker_coordinator.py`,
  `tests/test_human_input_resume_db.py`,
  `tests/test_robust_worker2_partial_transcript.py`,
  `tests/test_robust_worker2_round_cap.py`,
  `tests/test_task_agent_durable_delegation.py`, and
  `tests/test_task_agent_child_runs.py`. Verify per-round durable transcript
  checkpoints, shared Chat Agent-loop resume, bounded rounds, typed
  delegation receipts derived only from successful tool results,
  free-form/structured answer replacement, and that a Workspace Task's input
  card cannot resume through the legacy loop. Ambiguous
  effects and missing checkpoints must fail closed without fresh delegation.

- External-worker fencing and concurrency: run
  `tests/test_external_worker_attempt_fencing.py`,
  `tests/test_worker_heartbeat_lock_order.py`, and
  `tests/test_worker_sdk_protocol_v2.py`. Verify per-generation attempt tokens,
  authenticated worker ownership (including legacy null-token leases),
  monotonic capability merge under stale concurrent heartbeats, persisted
  max-concurrency admission, retained terminal outbox batches after transport
  failure, and no handler replay while a terminal report awaits ACK. Expire a
  tokenized live action without a receipt and assert it parks for reconciliation
  with the claim retained instead of retrying.

- Step result contract: `tests/test_dispatcher_envelope_status.py` covers the
  StepResult envelope status as the control signal. Contract failures retain
  receipts and
  manual recovery reconciles them rather than replaying external effects.
  `tests/test_plan_output_contract_e2e.py` verifies business payloads survive
  the full lease boundary and an invalid payload never completes the Step.


- Automated: `.venv/bin/pytest -q tests/test_orchestration_hardening.py tests/test_plan_retries.py tests/test_dispatcher_ref_resolution.py tests/test_dispatcher_envelope_status.py tests/test_lease_heartbeat.py tests/test_scheduler.py tests/test_scheduler_tick.py tests/test_skill_creation_matching.py tests/test_event_emitter.py`; plus `.venv/bin/pytest -q tests/test_workflow_runner_nodes.py tests/test_workflows.py -k 'claim or terminal_commit'`
- Automation Agent-selection regression: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_tool_surface_consolidation.py tests/test_ai_runtime_harness.py tests/test_tool_surface_executable.py tests/test_structured_runtime_queries.py -k 'scheduled_job or cron or automation'`. Verify the tool tells the model to query real Agents before supplying `agent_id`; a selected Agent must be active with exactly one current scoped deployment; valid selections persist, omitted or invalid/stale/unavailable selections fall back to the trusted caller, Manor AI falls back to `manor-master`, and the creation receipt exposes the final stored Agent ID. Trusted management API callers retain their separate explicit selection contract.
- Automated Sandbox lifecycle: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_sandbox_runner_lifecycle.py tests/test_sandbox_sdk.py`
- Integration: execute a two-step external worker-job Plan with ref passing, then simulate duplicate
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
  Crash a Task agent loop around a durable delegation park, lose the parent
  wake, and leave the child batch unsettled; assert recovery settles the batch
  and wakes the parent without orphan children
  (`tests/test_task_agent_durable_delegation.py`), and a stale claim aborts
  without writing (`tests/test_task_agent_loop.py`).
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
- Manual: follow a Task on its page; verify one live Task run,
  visible retries, and one terminal Task state.
- Pass criteria: no duplicate completion/side effect, references resolve from
  accepted upstream results, retry bounds hold, and unsupported success claims
  cannot complete the Task. A denied Agent Task claim is acknowledged only
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
  Cooperative provider rate limits release the active Lease into bounded,
  Retry-After-aware retry without consuming a business attempt; provider
  failures retain their real status, and exhausted credits do not blind-retry.
  Worker event persistence finishes before its local
  loop closes, while external delivery has a bounded shutdown wait and resumes
  through durable retry state rather than replaying committed Task work. Runtime
  schema hydration uses the same actor account boundary as execution and cannot
  import another tenant's discovered action. Scheduled Skill provider work
  holds neither the request transaction nor a worker DB connection; its final
  write revalidates Job and Skill revisions under lifecycle -> Job -> Skill
  lock order, and a failed final fence leaves any new file directory
  unreachable without changing the previously committed Skill.

- Resident Webchat Automated: run `tests/test_webchat_resident_scope.py` real
  ToolPool/envelope cases plus `tests/test_runtime_tool_policy.py` external MCP denial.
- Resident Webchat Integration: reuse the request scope with another conversation,
  contact or worker surface and assert refusal before resident provider I/O.
- Resident Webchat Manual: the public Agent exposes only the narrow self-read
  tool during verification; no manager Task or worker identity is substituted.
- Resident Webchat Pass criteria: existing Task/worker scopes remain unchanged;
  a resident read token cannot become generic managed integration authority.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-07 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-08: HITL and resume

- Conversation review scope: run `tests/test_conversation_approval_holds.py`.
  Historical approval acknowledgements and conditional review instructions must
  not stop a new internal file; explicit file holds and existing ask/deny policy
  remain authoritative. Verify non-openable creation targets, readable existing
  files, and independent Workflow review artifact links.

- Browser Chat continuation: run `tests/test_chat_input_continuation.py`,
  `tests/test_runtime_human_input.py`, `tests/test_runtime_request_input_tool.py`,
  `tests/test_chat_runtime_runs.py`, and `tests/test_runtime_capability_refresh.py`.
  Cover inline/durable
  with local/external Sandbox, persisted question/checkpoint, same-call answer
  injection, concurrent answer idempotency, stale question/owner rejection,
  cache-expired/reloaded form recovery, and newer-turn fencing. Non-resumable
  hosts and later capability refresh must not advertise a waiting capability
  they cannot execute. Input expiry and lease recovery cannot depend on Sandbox.

- Natural-language input resolution: run `tests/test_workspace_resolve_hitl_preflight.py`
  and `tests/test_workspace_input_resolution.py` with the existing input resume
  and Human queue suites. Verify malformed `decision`/empty response recovers
  in the same loop, exact actor/conversation/message targeting, text/structured
  answers/attachment preservation, and one winner across card/queue/tool
  submissions. Ordinary answers must not consume authorization or reconciliation
  requests, resume a stale origin, or overwrite another response.

- Input attachments: run `tests/test_human_input_resume_paths.py`.
  Verify Knowledge selection and local files reach only their original card,
  attachment-only answers resume the same checkpoint, canonical references
  retain current Document access checks, and existing Knowledge keeps its ACL
  and location. Partial upload or resolve failure must preserve the note and
  files, reuse successful receipts, and retry with the same upload key. In the
  browser, check search, select/remove, local file selection, keyboard access,
  and mobile layout without resolving a real pending request with test data.


- Task approval and input resume: run `tests/test_task_agent_loop_hitl.py`,
  `tests/test_task_agent_child_hitl.py`,
  `tests/test_robust_coordinator_approval_bridge.py`, and
  `tests/test_human_input_resume_db.py`. Verify the Task card binds one exact
  parked Runtime tool request, tampered or stale cards cannot resume a newer
  park, the approved token cannot run a different call, and approval causes a
  server-forced continuation of the saved call rather than model reconstruction.
  A delegated child's question or approval parks on its own card, and each
  answer wakes only its asker. For `request_input`, each new response replaces
  the other answer channel.

- Effect reconciliation: run `tests/test_effect_reconciliation.py`,
  `tests/test_coordinator_approval_receipt_binding.py`, and
  `tests/test_dispatcher_artifact_projection_transaction.py`. Verify exact
  effect identity/scope, immutable request snapshots, per-effect decisions,
  stale-card refresh without execution, one compare-and-swap winner, canonical
  Workspace -> Task -> Plan -> Lease -> Step -> request locking, safe
  `NOT_APPLIED` only after quiescence, and artifact/card projection in the same
  transaction as the lease transition. Unknown enum/provider states and
  malformed evidence must fail closed.

- Autonomous recovery vs. human input: run
  `tests/test_task_coordinator_manual_retry.py`,
  `tests/test_runtime_request_input_tool.py`, `tests/test_runtime_human_input.py`, and the
  dispatcher timeout and retry suites. Verify missing/invalid model
  justifications return a no-effect diagnostic in the same loop; genuine human
  facts and exact-file reviews can still suspend and resume; historical questions
  remain readable. Missing file receipts, validation failure and exhausted retry
  counts must not mint an input card. Busy delivery reads must preserve completed
  Steps and allow further observation without replay; terminal internal recovery
  must report failure, not waiting-for-user or success. Keep unknown external
  effects, exact approval identity, credits, cancellation and stale claims closed.

- Task input surfaces: park a Task agent loop on `request_input`, answer
  through Task comments, Task input, blocker tool and Workspace card, then
  consume its host result in the same run without asking again. Reject
  stale/missing request IDs, expired/cancelled requests, changed checkpoints
  and revoked file access. At desktop and mobile widths, actual TaskDetail must expose file links,
  current cards and bounded progress; reload and stale drafts cannot replay
  or retarget input.

- Generated-media contract: run `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_generated_media_authorization_contract.py tests/test_media_output_approval_resume.py tests/test_media_policy_resources.py`.
  Integration must persist and consume the real approval, write actual bytes,
  and register Knowledge. Cover explicit-token and tokenless resume, a collision
  affecting only a non-primary encoding, and deny/HITL rules for every physical,
  logical, and original requested path. A stale grant must remain unconsumed.

- TikTok video/photo review automated: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_tiktok_publish_review.py tests/test_tiktok_publish_review_api.py tests/test_tiktok_mcp.py tests/test_tiktok_publish_approval.py tests/test_tiktok_attachment_upload.py tests/test_youtube_tiktok_mcp.py`; `npm --prefix apps/web run test:tiktok-publish-review-browser`, plus the web build. Photo coverage includes exact ordered images, actual JPEG/WebP bytes, all previews loaded, selected cover, and replay protection.
- TikTok integration: use a mocked creator/provider and real HITL persistence.
  Refresh and submit one exact review; reject stale review IDs, unexpected
  fields, missing declarations, changed source/account, and repeated approval.
  Verify standing and unattended consent cannot skip the review, only one
  consumed grant reaches initialization, and provider errors never claim a post.
- TikTok manual: open the form, preview the original video, select privacy,
  toggle interactions and commercial disclosure, edit the caption, and confirm.
  Reopen it to refresh creator limits; verify a stale second window cannot
  approve and failed validation leaves the request pending. Use a test provider
  unless a separately authorized real publication is intended.
- TikTok pass criteria: actual human settings reach the exact call, no
  default privacy or enabled interactions, protected preview and source digest,
  single-use consent, and truthful processing/error status. Audit materials
  may describe only deployed, demonstrated behavior; code cannot waive the
  provider's independent Direct Post audit.

- Task-comment origin: run `tests/test_task_comment_approval_origin.py`,
  `tests/test_task_comment_provider_approval.py`, `tests/test_task_runtime_approvals.py`,
  and `tests/test_runtime_guard_unified_store.py`, plus the Workspace
  `test_workspace_task_rule_scope_e2e_through_runtime_tool_gate` regression.
  A normal Task created from main Chat must reach its Task rules from the
  comment thread; approval resumes the exact call once without rebinding or
  dispatching the parent Task. Cover a canonical conversation already equal
  to the thread, paused Workspace, rejection, changed payload, terminal Task,
  invalid/retargeted thread, and background token/dedup isolation. Real comment
  context and the Chat-card resume context must agree, while background
  surfaces remain background. Use an isolated test database and mocked model/
  provider execution; no live external action is needed.
- Automated: `.venv/bin/pytest -q tests/test_dispatcher_unified_approval_gate.py tests/test_lease_needs_human_request.py tests/test_pending_action_resolve.py tests/test_approval_card_lifecycle.py tests/test_workspace_hitl_channel_ack.py tests/test_channel_approved_reply_routes.py tests/test_hitl_card_surface.py tests/test_workspace_chat_tool_hitl_surface.py tests/test_runtime_guard_unified_store.py tests/test_runtime_tool_input_validation.py tests/test_automated_run_approvals.py tests/test_governance_policy_update_patch.py`; plus `.venv/bin/pytest -q tests/test_ai_engine.py -k 'approved_forced_call'`.
- Integration: pause one Step for approval/input, resolve once, resend the same
  response/token, and assert only the original origin resumes once. Pause a
  parent on subworkflow and foreach-subworkflow children, attempt a human resume,
  and assert it remains paused until the exact durable child receipt arrives.
  Resolve two HITL requests stored on the same Message concurrently and assert
  both resolution markers survive commit and refresh.
  Pause a delegated child run on a write tool, approve its card, and assert the
  call runs exactly once. Repeat with denial, an approval racing a re-park, and
  a cancelled child; none may execute twice or mint a new continuation.
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
  Basic Chrome approval resumes execute the exact call once and retain the
  canonical Chrome task tools for observation, drafting and finalization.
  `tests/test_chrome_basic_approval.py` and
  `tests/test_chrome_basic_approval_resume.py` cover default no-approval behavior,
  exact-rule overrides, read-only effects, one-time token consumption and a
  separately gated final submission. Unknown results are observed, not replayed.
  Cover Chrome-only provider and exact-name discovery with legacy read rules;
  generic, mixed-provider and invalid searches keep their read policy. Original
  discovery action, browser resource and browser category ask/deny rules still
  apply, and read-only effect and current discovery permission remain intact.
  `tests/test_chrome_basic_approval_policy_proof.py` covers classification
  restoration, missing/changed proof under a broader ordinary-action allow rule,
  exact target display, connection changes and a separately gated Luma submit.
  `tests/test_runtime_semantic_target_preflight.py` checks that read and resolve
  ask/deny decisions stop each internal observation before browser dispatch.
  Extension tests cover cold session restoration and user-only exact handoff
  focus, including released claims without model or cleanup authority.
  After other exact forced calls or a terminal provider confirm-and-retry pair,
  the model receives no callable tools for its final summary, so one approval
  cannot produce a duplicate side effect/card. Intermediate `file_upload` and
  `youtube_upload_start` keep an allowlisted followup set after the approved
  retry so the same origin can continue wait/read_page and remaining
  authorized page actions for the current task; upload, computer,
  inject_script, and navigation stay out. Public, Unlisted, and Schedule
  remain separately gated. Missing policy_category still disables followup
  tools. Pending
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

- Resident Webchat Automated: run the WS-02 resident suite and
  `tests/test_dispatcher_unified_approval_gate.py`.
- Resident Webchat Integration: external management MCP remains denied despite
  a verified resident session; no read verification resolves a pending approval.
- Resident Webchat Manual: confirm verification UI promises only self-status
  reads and does not represent maintenance creation or manager consent.
- Resident Webchat Pass criteria: read verification adds no write or approval
  bypass; later submission work requires its own explicit tested consent contract.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-08 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-09: Workspace Flow and Workflow runtime

- Direct-launch scope: run
  `tests/test_workflows.py::test_start_workflow_inherits_definition_workspace_and_rejects_conflicts`
  and `tests/test_workflows.py::test_workspace_owned_workflow_binding_cannot_cross_workspace`.
  Workspace-owned definitions must keep their scope even without a binding or
  explicit caller Workspace, and an explicit conflicting scope must fail before
  run creation. Binding creation, updates (including changing the definition)
  and legacy binding execution must enforce the same boundary. Retain
  shared-definition and lifecycle admission coverage.
- Parallel retry and node deadline regressions: run
  `.venv/bin/pytest -q tests/test_workflow_parallel_retry.py tests/test_workflow_node_timeouts.py`.
  Prove successful siblings execute once, all active checks precede fan-in,
  inactive conditional routes stay inactive, explicit rewinds invalidate
  dependent output, and legacy-chain recovery keeps Agent effect evidence.
  Keep stage retry and native approval suites alongside these tests. Missing
  node timeout must not imply 300 seconds; explicit deadlines still fail and
  invalid configs cannot call tools or reuse cached results. Blueprint config
  projection must preserve null/positive values. Local projection checks are
  not evidence of a live source Workspace upgrade or production deployment.

- Approval policy regression: run `tests/test_workspace_workflow_automatic_policy.py`
  and `tests/test_automated_run_approvals.py`. Cross automatic/confirm-changes/risk
  modes with allow/ask/deny/no rule. Prove running Workflows and Chat produce the
  same decision, current rule edits take effect, Task restrictions remain additive,
  and provider confirmation uses the same policy. No conversation must produce a
  concrete blocker, never implicit approval. Verify native resume separately.

- Native tool approval regression: run
  `tests/test_workflow_native_approval_nodes.py` and
  `tests/test_workflow_native_approval_integration.py` with the existing Agent
  checkpoint, Workflow approval, orchestration and chat-origin suites. Prove
  real File Engine approval parks without writing an output variable; App and
  Chat expose the same native request; approval resumes only its saved call;
  rejection, duplicate decisions, wrong actors, lifecycle/definition drift and
  generic Resume cannot execute. Rollback must publish no wake, and broker
  failure must retain the durable continuation. Earlier effects execute once.
  Public API/Blueprint payloads must not contain private checkpoint data.
  Run `tests/test_workflows.py::test_workflow_status_settlement_survives_chat_projection_failure`
  to prove both approval-message and status-message failures preserve
  authoritative settlement and roll back only the Chat savepoint.
  Browser-check App/Chat approval, rejection, visible errors, keyboard input
  and duplicate submission. Mocked browser transport is not production proof.

- Expired claim recovery: run `tests/test_workflow_expired_claim_recovery.py`
  on a dedicated test database plus Workflow claim/terminal-commit tests.
  Race renewal, replacement and deletion against exact-token takeover; select
  only running runs with known expired ownership. Preserve completed evidence,
  settle normal terminal effects once, and prove late delivery cannot replay
  the graph. Verify the periodic task has a control consumer and bounded expiry.
  Browser-check explicit Retry, status-unavailable and close-App progress states;
  do not start paid generation merely to verify UI projections.

- Automated: `.venv/bin/pytest -q tests/test_workspace_workflow_entrypoints.py tests/test_workspace_flow_tools.py tests/test_workflow_runner_nodes.py tests/test_workflow_orchestration_nodes.py tests/test_workflow_publication_receipts.py tests/test_workflow_chat_origin.py tests/test_workflows.py -k 'terminal_effect or continuation'`; plus `.venv/bin/pytest -q tests/test_webchat_page.py tests/test_automated_run_approvals.py`
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

- Scoped file acceptance: run `tests/test_file_deliverable_contract.py`
  and `tests/test_strategist_deliverables.py`.
  Require the declared logical folder and canonical Document plus actual
  write/readback of the final bytes. Reject another project/Workspace/Entity,
  trash, missing IDs, old-attempt receipts, read-before-write, model-only claims
  and later changed files.

- Video composition output contract: run `tests/test_compose_video_output_contract.py`, `tests/test_media_output_approval_resume.py`, and `tests/test_media_policy_resources.py` with isolated test storage. Verify exact MP4/recipe pair approval, sidecar-only collisions, stale consent, cross-Workspace rejection and concurrent CREATE protection. Force each post-render projection failure and confirm the model-facing error retains committed output identities without claiming complete delivery or replaying the render.
- Video authoring recovery: run `tests/test_video_edit_runtime.py` and
  `tests/test_video_edit_authoring_contract.py` and
  `tests/test_video_review_continuation.py`. Verify a completed negative check
  feeds precise diagnostics into review -> edit -> re-review in one Agent loop,
  settles only the check's effect and never grants delivery or render approval.
  Crashes, incomplete reports and uncertain write/render outcomes remain errors.
  Verify the
  same contract at start/status/recovery/sync, structured exact-file diagnostics
  after source writes and failed review without a host-selected repair strategy,
  accepted local GSAP path equivalents and unchanged strict review/hash gates.
  Run the opt-in pinned renderer proof with
  `MANOR_VIDEO_RUNTIME_SMOKE_IMAGE=sandbox-skill:<local-tag> .venv/bin/pytest -q tests/test_video_authoring_pinned_runtime.py`.
  It uses a temporary, network-free container and synthetic content, never user
  projects or provider generation. Static/unit checks alone do not prove a real
  user composition or final MP4.

- Video render isolation/resource regression: run `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_video_render_sandbox_isolation.py tests/test_video_runtime_resources.py` plus the Sandbox backend process-init tests. Verify a render uses fresh capacity and releases only its own execution Sandbox on success, failure, transport loss and cancellation; review preserves the editable session. PID/CPU/memory pressure must reduce worker concurrency or return structured diagnostics before starting Chromium, and timeout must reap descendants without killing another execution. HyperFrames' FFmpeg runs through `media_governor.py`: concurrent processes and their CPU windows come from the sandbox budget, a PID/OOM/thread-spawn failure retries once at one FFmpeg on one CPU, then returns `render_resource_limit` (reason, step, attempts). Browser steps (check, snapshot, render) get a navigation budget sized from `MANOR_VIDEO_NAVIGATION_TIMEOUT_MS` (default 60 s) plus the project's media, retry once with doubled timeouts on a page timeout, then return `snapshot_timeout`/`render_timeout` (reason, step, attempts, `retryable: true`). Multi-layer extraction is proven by `MANOR_VIDEO_DOCKER_TEST=1 MANOR_VIDEO_DOCKER_IMAGE=<sandbox-skill tag> pytest tests/test_video_runtime_docker.py -k montage`. Run a real bounded render in a new test Sandbox; inspect nonempty MP4/duration and process cleanup. Do not replay an existing user's paid generation to prove this contract.
- Video Sandbox lease regression: run `.venv/bin/pytest -q tests/test_sandbox_runner_touch.py tests/test_video_edit_runtime.py`.
  Verify authenticated heartbeat 200 with refreshed expiry; missing 404,
  unavailable 409 and unauthenticated 401 without lease mutation; no command or
  capacity grant. Exercise renewal, busy transition and replacement while an
  earlier prune candidate is awaiting cleanup. Live verification must proceed
  from start through write_files and review in the same recoverable project.

- Folder-layout regression: run `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_generated_media_naming.py tests/test_generated_media_authorization_contract.py tests/test_task_scoped_artifacts.py tests/test_knowledge_workspace_binding.py tests/test_blueprint_installer_embedded.py tests/test_human_request_items.py tests/test_recording_uploads.py tests/test_chrome_recording_uploads.py tests/test_email_attachment_bridge.py tests/test_youtube_publication_receipts.py tests/test_knowledge_visibility.py`.
  Also run `tests/test_conversation_generated_files.py`: default ordinary Chat
  outputs from every producing tool share one private title-named folder,
  same-title sessions stay separate, a rename preserves the folder and document
  IDs, and explicit destinations or Workspace sessions keep their existing routing.
  Cover root files without implicit type buckets; Task buckets; Workflow lineage
  precedence and legacy retries; exact approval/save agreement; same-basename
  starters with distinct portable paths; idempotent logical repair and later
  reconcile; unchanged source IDs/bytes during Need Input binding; hidden storage
  folders with visible Documents; capture root permissions and explicit folders;
  latest-receipt fallback. Run recording/creative Blueprint round trips and
  `tests/test_ai_runtime_harness.py` for the Skill/runtime edge. Apply the recording
  nullable-folder migration in a disposable database and run migration graph
  checks; never execute a projection repair against production as part of tests.
  Also run `tests/test_knowledge_starter_layout.py`,
  `tests/test_file_tools_doc_visibility.py`
  and `tests/test_bash_output_paths.py`. Empty Workspace listings must not fall
  back to Knowledge root. Bash approval, execution and projection must agree on
  the resolved path while existing source operands stay usable. Document-ID-only file reads must
  resolve the canonical physical path but reject foreign, private, or mismatched
  Document references through the same access boundary. Denied captures
  must not create explicit destination folders. The recording migration test
  must exercise both root sessions and the guarded downgrade on PostgreSQL.
  Need Input must preserve an existing user folder and a foreign Workspace
  origin. Bash tests must include numeric echo output, escaped names, comments,
  trailing directory slashes and ephemeral sandbox commands.
- Delegation receipts: include `tests/test_robust_worker_coordinator.py` when
  changing delegated output capture. Verify only successful tool receipts feed
  typed evidence/artifact/Knowledge refs, and assistant prose cannot manufacture evidence.

- Generated-media contract: also run `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_media_output_job_contract.py`.
  Integration covers Chinese/case/underscore names, initial and late collisions,
  supported MIME encodings, Workspace/Task roots, and video completion/recovery.
  Manually generate a named image and inspect its saved Knowledge file; approval
  and final output must identify the same normalized target. Audio and video
  retain the same identity contract, with explicit legacy video compatibility.
  Pass only when malformed or cross-job contracts fail before writes and late
  collisions neither overwrite a file nor choose an unapproved suffix.

- Automated: `.venv/bin/pytest -q tests/test_artifact_knowledge_projection.py tests/test_workspace_artifacts.py tests/test_workspace_task_artifacts_are_visible.py tests/test_task_scoped_artifacts.py tests/test_knowledge_file_consistency.py tests/test_entity_fs_write_guard.py tests/test_generate_file_tool.py tests/test_workspace_artifact_purge.py tests/test_event_emitter.py tests/test_email_attachment_bridge.py tests/test_documents.py`; `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_document_permissions.py -k 'signature_request'`; `npm --prefix apps/web exec playwright test e2e/knowledge-upload-lifecycle.spec.ts e2e/knowledge-upload-page.spec.ts`; `npm --prefix apps/web run build`
  PDF fields additionally require `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_document_signature_fields.py tests/test_document_signature_lock.py tests/test_pdf_signature_stamping.py`.
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
  Create one PDF signature request, verify the exact recipient by OTP, view the
  pinned page/placement, sign it, and assert the source bytes/version remain
  unchanged while one adjacent readable `-signed` Document with one initial
  version contains the visual signature, signer caption, document/signature
  digests, access-log events, and immutable signed-document marker. Occupy the
  default signed-copy name and assert a deduped name is used without overwriting
  the existing file. Fail projection after signed bytes are written but before
  commit and assert the file, Document, Version, Share completion, and upload
  event all roll back. Race two completion attempts; only one may commit. Edit
  the source before signing and assert completion fails closed.
  Force initial email failure, resend, assert token rotation, then revoke and
  confirm both old and current tokens are unusable.
  Run `E2E_PDF_SIGNATURE=1 E2E_PORT=5185 E2E_API=http://127.0.0.1:8000 npm --prefix apps/web run test:e2e -- pdf-signature-live.spec.ts --workers=1`
  against the dedicated local signature server described in `apps/web/e2e/README.md`.
  Validate desktop and mobile two-page review, real loopback SMTP/OTP delivery,
  drawing and consent, browser PDF download with matching digest, owner status,
  delivery failure/resend/revoke, and invalid-page rejection before invitation.
  Run `pdf-signature-fields-live.spec.ts` using the optional local PDF/DOCX
  fixtures and independent ports documented in `apps/web/e2e/README.md`. Place,
  drag, resize, label, and preview signature/name/date/text/checkbox fields on
  multiple pages; fill them with desktop and mobile touch input after OTP.
  Assert missing required values cannot submit, final PDF pages/content survive,
  the download digest matches the completion receipt, and the owner sees one
  completed request. API tests reject duplicate IDs, out-of-page rectangles,
  unknown values, invalid types/dates, blank signatures, unsupported glyphs,
  and text that cannot fit. Verify normalized `field_values`, their canonical
  digest, and signature hashes agree with the rendered output; no raw signature
  image is stored in Share metadata. Legacy requests accept both old and new
  submission shapes.
- Manual: open the attachment from Task, chat, and Knowledge. Rename the
  Workspace and confirm durable identity/path behavior remains coherent.
  From a PDF viewer, send a request to a real mailbox, complete the OTP and
  drawing flow on desktop and mobile, then confirm the sender sees sent,
  viewed, and signed timestamps and both parties can download the signed PDF.
  On a test copy, confirm each field aligns with the intended location at
  desktop/mobile widths and after PDF page rotation; inspect the downloaded
  result for readable complete text, preserved original terms, and no clipping.
- Pass criteria: for the Task/Workflow paths covered by the listed tests, a
  concrete readable file has a Document ID; no phantom filename, cross-entity
  path, empty Knowledge entry, or completed-required-artifact gap. A new
  producer must prove projection separately; rolled-back multi-file projection
  emits no upload event, committed event delivery happens afterward, interrupted
  delivery resumes from its EventLog lease without replaying the artifact
  transaction, and failed backup cleanup retains a durable retry intent. This
  checklist is not a blanket claim for every external provider.
  PDF signing exposes no plaintext token after create/resend, accepts only the
  verified audience, validates bounded PNG/PDF input server-side, pins the
  source revision and exact field snapshot, leaves the source version unchanged,
  commits one separately projected signed copy/version with all required values,
  and rejects duplicate completion or later binary/text saves. Multi-party
  signing keeps partial bytes hidden without a Document projection and publishes
  the copy only after every signer completes. Delivery failure remains retryable,
  document/Workspace/classification lifecycle still applies, and the runtime is
  absent from Blueprints.


## WS-11: Configurable Goals, measurements, events, evaluation, and learning

- Evidence-assessment automated: include `tests/test_stat_assessment_contract.py`.
  Cover bound Goal writes,
  current-run receipts, unknown versus zero, source version and actor scope,
  stale definitions, duplicate/conflicting idempotency keys, late observations,
  null-safe migration rollback, and runtime-derived UI states.
- Evidence-assessment integration: configure five real Stats plus one assessment
  Workflow/Automation, export that Workspace and reinstall into another; assert
  service keys and local bindings resolve with no copied runtime observations.
  Preserve a paused Automation when rerunning the setup command.
- Evidence-assessment manual: execute the configured Workflow with missing and
  sufficient evidence; inspect real observation/Goal receipts, gaps and history.
  Verify the current Workspace separately from the bundled Blueprint. A build
  or template update alone is not proof that the live Workspace was migrated.
- Evidence-assessment pass: no fabricated backfill, no unknown-to-zero coercion,
  no text-only completion, no duplicate timers, and no loss of last-known data.

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

- Delegation accounting: include `tests/test_task_agent_durable_delegation.py`.
  Verify delegated child-run usage is attributed to the parent Task and billed
  once per child call.

- Apify automated: `tests/test_apify_client.py`, `tests/test_apify_mcp.py`,
  `tests/test_apify_integration.py`; Cloud source also runs
  `tests/test_apify_dispatch.py`, `tests/test_apify_collections.py`, and
  `tests/test_platform_managed_mcp.py`. Run migration graph, OSS boundary and
  Celery queue-registry checks when changing its persistence or recovery task.
- Apify integration: quote without vendor work; start twice; poll terminal cost
  concurrently; exercise insufficient credits, zero/missing/over-budget cost,
  failed and canceled runs, lost start response, revoked account, deleted
  Workspace, and settlement failure. Use isolated DB and mocked provider;
  connection health must never start an Actor.
- Apify official MCP transport: verify initialization, the fixed endpoint and
  tool allowlist, async `call-actor` budget options, structured run identity,
  cost metadata including genuine zero versus missing values, dataset reads,
  and cancellation confirmation. A malformed or lost start response must not
  retry the paid call or release its reservation. Run remote JSON/SSE protocol
  tests alongside the client contract tests when changing this transport.
- Apify manual: connect a personal key or configure the Cloud platform key,
  request a profile collection, review the maximum Credits, start it, then
  inspect terminal usage and reference coverage. Download/transcript add-ons
  require a selected Reel; temporary URLs do not count as Knowledge delivery.
- Apify pass criteria: pricing is frozen and calculated with Decimal; at most
  one start and debit per quote; explicit BYOK cannot switch to platform debit;
  unknown startup/missing cost remain auditable and reserved; unresolved work
  blocks purge, recovery never restarts it, and runtime state is not exported.

- Native source-promotion/status regression: run the WS-02 Browser admission/status
  and `tests/test_voice_work_sources.py` suites alongside the existing voice billing
  gates; reused user messages must not bypass scoped/public admission or start duplicate work.
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
  reservation lifetime and authorization boundaries. Native input grouping
  and source promotion add no inference or billing exception; empty-input
  recovery and durable status lookup do not start or replay Main work. The
  Gateway fallback and Blueprint/schema contracts remain unchanged. Adding a
  provider or entry surface requires its own gate test.

- Resident Webchat Automated: run `tests/test_credit_reservations.py` and the
  WS-02 resident suite; the latter makes no model/provider-inference calls.
- Resident Webchat Integration: a private read uses the same runtime envelope
  without rewriting the normal channel owner used by Chat credit admission.
- Resident Webchat Manual: missing LLM configuration remains a Chat blocker even
  if resident status HTTP succeeds; record these outcomes separately.
- Resident Webchat Pass criteria: no resident token or panel action creates a
  model key, a credit grant, a different billing actor or a free inference path.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-12 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-13: Pause, delete, restore, and purge

- Pause semantics: run `tests/test_workspace_manual_autonomy.py`,
  `tests/test_runtime_manual_execution_origin.py`, `tests/test_strategist_runtime_manual_origin.py`,
  `tests/test_task_manual_dispatch_admission.py`, `tests/test_agent_initiative_paused_workspace.py`,
  `tests/test_workspace_paused_files.py`, `tests/test_paused_workspace_manual_tasks.py`,
  `tests/test_workspace_manual_scheduler.py`, `tests/test_review_trigger_kind.py`,
  `tests/test_workspace_setup_gates.py`, `tests/test_workspace_workflow_entrypoints.py`,
  `tests/test_scheduler.py`, and `tests/test_unified_approvals.py`. Verify manual
  file writes, Task start/retry/approval, Flow runs, collection, and human
  Strategist requests work without resuming autonomy. Scheduled/event reviews
  and automatic Tasks must remain stopped; paused review proposals require
  explicit approval. Preserve ACL, setup, credit, deletion, and provenance fences.
  Background Task/Flow/Strategist/run-now tool calls must not acquire manual
  authority from attribution or source labels. Race pause/delete with candidate
  discovery and lease commit. Confirm manual child lineage remains scoped,
  generated ownerless Goal schedules retain the run-now requester, and paused
  manual Initiative delivery passes only its exact current producer authority.
  Independently enabled Workflow schedules and standalone Stat collectors must
  execute automatically while Strategist remains paused. Disabled jobs,
  disabled bindings, wrong scheduled origins, deleted Workspaces and blocked
  setup must still refuse new execution. Verify a Workflow-only live Blueprint
  installs paused with its declared automations enabled.
- Automated: `.venv/bin/pytest -q tests/test_workspace_lifecycle.py tests/test_document_permissions.py tests/test_folder_permissions.py tests/test_document_access_batch.py tests/test_entity_fs_write_guard.py tests/test_reusable_resource_locking.py tests/test_deletion_tasks.py tests/test_workspace_mechanism_regressions.py tests/test_user_lifecycle.py tests/test_workspace_purge_owned_resources.py tests/test_twilio_voice_runtime.py tests/test_blueprint_channel_revert.py tests/test_integrations.py -k 'delete or restore or purge or paused or payload or disconnect or userless or twilio_binding or blueprint_twilio'`; plus `.venv/bin/pytest -q tests/test_sharing_permission_boundaries.py -k 'rechecks or auth_read or read_gate or folder_list_gate or revoked_bearer or can_manage or primary_entity or actor_resolution'` and `DEPLOYMENT_MODE=cloud .venv/bin/pytest -q tests/test_admin_impersonation.py -k 'ended_impersonation or owner_can_start_and_end'`
  PDF signature boundaries additionally require `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_document_signature_fields.py tests/test_document_signature_lock.py tests/test_document_permissions.py -k 'signature'`.
  Knowledge retention additionally covers `tests/test_workspace_artifact_purge.py`,
  `tests/test_embedding.py`, and `tests/test_embedding_recovery.py`: preserve
  existing file/version paths, protect legacy restricted audiences, discard
  obsolete cleanup intents, and stop indexing publication after Entity deletion.
  Retained-directory tests must cover renamed folder roots, nested inherited
  grants, mixed logical/storage aliases, revocation, foreign Entities, and
  unprojected paths. Public Document/folder share concurrency tests must prove
  Entity deletion waits for an admitted read, while deletion that wins first
  denies the next read; Entity locks precede Workspace/folder locks.
- Integration: create Workspace Task/Plan/Lease/Workflow run/job and job-run/chat/artifact,
  notification/outbox/delivery and Goal/Stat collection schedules, soft-delete
  it, verify immediate execution blocking and automation cleanup. Assert retained
  Knowledge keeps its IDs, paths, versions, explicit document/folder ACLs and
  public shares. Deleted source Workspace roles must not confer access; an exact
  link in another active Workspace admits only the linked file, and revocation
  denies the next read. Low-level mutations and userless agents must still reject
  deleted executing Workspaces and unprojected bytes;
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
  Pause PDF field stamping, then revoke its Share or change its field snapshot;
  assert final admission rejects the stale submission without changing source
  bytes, completion evidence, or Document versions. Exercise folder
  reclassification during stamping through the shared signature regression,
  and verify public signature reads obey Entity lifecycle and current share policy.
- Manual: delete and restore within grace period; verify restored data is
  accessible but removed automations are not unexpectedly recreated.
- Pass criteria: no new automatic work after pause and no work after delete; no stale polling, and restore
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
  successful Workspace purge. Workspace purge retains Entity Knowledge and
  removes only its own groups/memberships. Terminal Entity deletion removes
  retained Documents, versions, folders, grants, shares and exact source/cache
  files. Old cleanup jobs must preserve targets referenced by any surviving
  Document or DocumentVersion. Physical deletion starts only after the owning
  database deletion commits; a disabled, missing, unmounted, or failing filesystem
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
  PDF signature field submissions cross the same final policy/lifecycle locks
  as legacy signing. A revoked, expired, changed-source, or changed-field
  request cannot complete, and restoring Workspace access cannot reopen a
  completed signature or bypass the signed-document content lock.

- Resident Webchat Automated: run `tests/test_webchat_resident_scope.py`,
  `tests/test_user_lifecycle.py` and `tests/test_workspace_lifecycle.py`.
- Resident Webchat Integration: race callback/identity/read response with
  disconnect or disable; assert tombstones survive marker loss, expired proof
  can clear local authority, and real assistant persistence rejects stale generation.
- Resident Webchat Manual: log out of PMS, refresh, clear browser proof, change
  the target, and start a new public conversation without exposing old history.
- Resident Webchat Pass criteria: private content fails closed through history,
  chat/audio and UI reads; purge removes only owned grants and does not recreate
  anonymous authority. Remote revoke failure remains explicit and retryable.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-13 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-14: Blueprint portability

- Knowledge dependency regression: `tests/test_blueprint_knowledge_workflow_refs.py`
  covers Markdown/CSV locators, native reads, simulation/live install/re-export,
  excluded and foreign starters, and file commit/rollback/savepoint ownership.
  Explicit `initial_file` templates must export frozen seed content after live
  edits, reject invalid/missing seed metadata without a current-content fallback,
  and leave ordinary artifacts excluded in both the picker and exporter.
  `tests/test_blueprint_configuration_upgrade.py` verifies initial template
  upgrade/revert preserves current bytes and rejects destructive mode changes;
  `tests/test_blueprint_v11_e2e.py` covers the authorized configuration API.

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
  artifact/credit state or unattended-run consent; deployment-local Webchat
  document and Workflow-binding references are excluded; and prose matches
  executable configuration.

- Resident Webchat Automated: run `tests/test_webchat_resident_scope.py`,
  `tests/test_blueprint_exporter_embedded.py`, `tests/test_blueprint_v11_e2e.py`
  and `tests/test_manor_workspace_architecture_skill.py`.
- Resident Webchat Integration: export a configured channel and nested stale
  target metadata; assert no `resident_pms`, `resident_private`, resident
  binding ID, token/proof/state or original subject appears in the payload.
- Resident Webchat Manual: install the safe public channel and explicitly select
  a new target account; no resident session or private history is restored.
- Resident Webchat Pass criteria: a public channel is portable only through its
  existing safe contract; resident connection/identity/credentials are runtime-only.

- Resident Create Automated: run `tests/test_webchat_resident_create.py`, `tests/test_webchat_resident_scope.py`, `tests/test_webchat_resident_gateway.py`, `tests/test_webchat_resident_migration.py` and `tests/test_manor_workspace_architecture_skill.py` on a dedicated test database; also run all ten cross-cutting files required below for WS-14 changes.
- Resident Create Integration: use real PostgreSQL row locks and formal migration DDL, distinct encrypted read/create grants, real ASGI/ToolPool with mocked PMS transport; prove no confirmation means zero external writes, one durable key under double-confirm/edit races, same-key recovery after timeout/receipt404, lifecycle/proof/actor rejection and purge/Blueprint exclusion. Do not label mocked provider I/O a live PMS test.
- Resident Create Manual: in isolated browsers authorize both purposes for one synthetic resident, ask real Chat to prepare a draft, edit/address-select and explicitly confirm it, then verify one PMS request as the resident and manager. Repeat with expiry, logout, two residents, double click and interrupted response; compare actual PMS DB receipts. Keep API-only and missing-model outcomes distinct.
- Resident Create Pass criteria: local draft authority never becomes generic management, confirmation or billing authority; only reviewed revision/proof/Origin can submit, unknown never means failed or not-written, no private state migrates in Blueprint and no stale session can read or publish it.

## WS-15: Notification persistence and delivery

- Native group/DM email regression: `tests/test_chat_email_notifications.py`
  exercises the default unread threshold, custom delay, no repeat range, bounded
  escaped excerpts using the common template, live read/mute/membership/owner
  and verified-address checks, deferred quiet hours/disabled SMTP, provider
  retry and concurrent discovery/delivery locks. Mock SMTP; never use actual
  users as test receivers. Personal preferences and receipts are not exported.

- Group mention/reply regression: active post-join recipients receive one
  idempotent in-app alert for an explicit @ or direct reply, even when both
  target the same person. Outsiders, the sender, and muted members receive
  none. Message content is absent from notification bodies; retraction
  cancels the related alert. Muting never changes unread counts.

- Automated: `.venv/bin/pytest -q tests/test_notifications.py tests/test_notify_multichannel.py tests/test_notification_scheduler.py tests/test_notification_delivery_capacity.py tests/test_external_event_sweep_capacity.py tests/test_notification_callbacks.py tests/test_task_event_notifications.py tests/test_workspace_notification_knowledge_access.py tests/test_workspace_hitl_channel_ack.py tests/test_realtime_task_updates.py tests/test_migration_graph.py`
  Capacity regression: hold a provider on one transaction slot and verify
  unstarted notifications remain pending; event and notification outboxes share
  admission; larger budgets retain a foreground slot. Expire attempt/sweep
  budgets, cancel queued work and interrupt a receipt checkpoint; verify bounded
  retry, durable target receipts, no leaked connection/permit and no repeated
  claim of one event within the same sweep. For event sweeps, fill the shared
  gate, expire the new-claim budget during an active send and during admission,
  and fail one claim while a sibling is sending. Verify deferred rows keep
  attempt zero, fast batches drain through at most two dispatchers, and both
  failure and outer cancellation wait for all sessions/permits to be released.
  The adjacent PDF email path is checked by `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_document_permissions.py tests/test_document_signature_fields.py -k 'signature'` and the live signature specs in `apps/web/e2e/README.md`.
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
  For PDF fields, reject invalid definitions before any invitation. Fail the
  invitation, retry through explicit resend, and confirm the same field
  definitions and request identity survive while the URL rotates. After a
  successful signature, completion notice failure must not replay signing.
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
  PDF signature email delivery remains represented by Share state, not by a
  Notification outbox receipt. Field editing adds no notification producer or
  automatic completion retry; invitation failure remains recoverable and
  signed-document completion remains authoritative after mail delivery.


## WS-18: Agent Initiative and proactive delivery

- Automated: `PYTHONPATH=.:tests .venv/bin/pytest -q tests/test_agent_initiative_models.py tests/test_agent_initiatives.py tests/test_agent_initiative_runtime_tools.py tests/test_agent_initiative_execution_runtime.py tests/test_agent_initiative_delivery_recovery.py tests/test_notification_scheduler.py tests/test_ai_runtime_harness.py tests/test_migration_graph.py`; plus the Agent Initiative frontend source test and web build.
- Integration: create one personal custom-Agent Initiative, one lazy Manor
  master Initiative, and seed one pre-boundary Workspace Initiative. Reject
  new Workspace creation through both Chat tooling and REST before an
  Initiative or derived driver exists. Exercise notify, silent,
  muted, paused owner-confirmed run-now, duplicate occurrence, cooldown/rate
  suppression, invalid JSON repair/failure, quiet hours, and one scheduled
  one-shot. Assert exact owner/Subscription/Conversation lineage, normal credit
  attribution, Chat-before-Notification delivery, stable idempotency keys, no
  duplicate model execution, and no movement of the recurring clock on manual
  run-now. Exercise due/future/live/stale delivery leases, concurrent claims,
  broker/worker failure, bounded backoff and exhaustion, malformed context,
  stale-token fencing, and a batch with more poison rows than the sweep limit;
  assert Notification attempts start only after Chat reaches a terminal state
  and later healthy work is not starved. Fail broker publication and assert it
  does not consume a Delivery attempt; assert the recovery task keeps the
  one-argument Run-id worker contract. Resume a stale deferred Chat worker and
  a stale Notification worker after a successor changes durable state, and
  assert neither can reclaim/quarantine or revive the successor's result.
  Soft-delete a Workspace with an active Initiative, assert `needs_setup` plus
  no driver, restore without auto-start, then explicitly resume and assert one
  enabled replacement driver. For Workspace main pagination with pinned thread
  cards, assert the Initiative header uses the explicit resolved Conversation
  id rather than inferring a destination from message rows.
  For each external target, assert active delivery succeeds; mute, forged
  Notification metadata, or a missing/mismatched exact Chat Message cancels
  external fan-out without retracting the in-app parent; and a stale Outbox
  claim cannot overwrite its successor.
- Manual: from an Agent conversation ask for a daily update, change its time in
  the same conversation, mute it with “不要打扰我”, resume it, and inspect the
  Initiative card and Run history. Repeat in Manor main Chat; confirm each
  update lands only in its selected private conversation and that quiet hours
  retain Chat/in-app history without an external push. In Workspace Chat ask
  for a recurring deliverable and confirm it creates a Workspace-scoped
  Automation rather than an Initiative. Verify a Workspace viewer cannot see
  the management tools or create an Automation, even through a stale call;
  historical Workspace Initiative cards remain readable and manageable without
  allowing a new one.
- Pass criteria: proactive behavior belongs to one concrete
  `AgentSubscription`, not an Agent definition or mandatory Workspace; owner and
  Workspace permissions are revalidated before cost and delivery; only strict
  notify decisions become messages; silent/muted/duplicate/cooldown/rate cases
  remain auditable without delivery; pause produces no automatic cost; delivery
  retry never reruns the Agent and cannot continue beyond the persisted attempt
  limit; one-shot completion is scheduled-only; generic Scheduler, Experiment,
  Proposal, derived Automation index, and Blueprint surfaces cannot expose or
  mutate derived Initiative runtime.

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

## Delivery evidence and file commit regression boundary

- Capability discovery and Chat continuation: run `test_capability_semantic_contract.py`,
  `test_capability_search.py`, `test_chat_capability_evidence.py`,
  `test_runtime_capability_preparation.py`, `test_runtime_chat_capabilities.py`,
  `test_runtime_capability_refresh.py` and `test_runtime_capability_gate.py`.
  Exercise native retrieval alongside a disconnected inferred provider, irrelevant
  writing Skills, matcher failure, pagination and current-turn evidence across
  checkpoints. A rejected unsupported answer must cause actual tool execution in
  the same loop, not a fabricated Task or a forced integration connection.
  In isolated PostgreSQL, verify exact Chat leases, stale actors/conversations,
  normal approval and denial, policy changes after preflight, revoked catalog
  entries, applied receipt replay and no global Agent binding mutation. Manually
  check durable internal Chat loads refreshed tools and resumes its existing
  message after approval. Pass only with ordinary authorization intact; catalog
  relevance and successful transport never establish semantic task completion.

- Automated: `test_runtime_file_journal.py`, `test_runtime_file_journal_recovery.py`,
  `test_merge_video_output_contract.py`, `test_merge_video_projection.py`,
  `test_workspace_response_contract.py` and
  the Task agent-loop and delivery-verification suites cover WS-02/07/08/10/13.
- Integration: use an isolated PostgreSQL database for lost-ACK recovery,
  projection rollback, concurrent target collisions and current-run evidence;
  verify the Alembic journal migration and unique unresolved-path fence.
- Manual: observe Chat
  progress against durable records, not unrelated background work.
- Pass: no later file is overwritten by rollback; no settled write is repeated;
  no final Task summary claims execution without
  its scoped current evidence. Runtime journal/manifest/event rows are absent
  from Blueprint exports. Crash-safe deletion and all multi-file renderers are
  outside this write/copy and single-MP4 boundary.

## New node rule

If a feature introduces a new persisted runtime, entry surface, approval
origin, billable provider path, artifact type, or deletion-owned resource:

1. add or extend a `WS-xx` architecture node;
2. add Automated, Integration, Manual, and Pass criteria entries here;
3. add it to the cross-node contract table;
4. decide its Blueprint portability explicitly;
5. add a structural regression test so later documentation cannot omit it.


## Private client delivery — WS-01/02/08/13/14

- Automated: `tests/test_client_workspace_delivery.py`, `tests/test_people.py`, `tests/test_workspace_access_defaults.py`.
- Integration: receive in a different owned organization, verify direct Owner, no Staff or Marketplace record and no provider access; repeat acceptance returns the same Workspace. Select Markdown/CSV starters with fixed Workflow references and an App, accept, read the native files and compare the re-export. Confirm unselected Knowledge stays excluded and missing, foreign or unsafe dependencies fail before invitation persistence.
- Initial-file delivery: use both a mutable CSV queue and an unrelated Markdown
  handoff. Select their explicitly configured initial templates, verify recipient
  files contain only the seed, and compare source IDs, metadata and bytes before
  and after delivery. Edit the recipient copy and confirm re-export still uses
  the seed. Picker labels must distinguish initial templates from current content
  and identify required Workflow dependencies without exposing excluded files.
- Manual: Company client detail → contact → optional Workspace invitation; recipient verifies email and accepts. Check expiry, rotation, revoke, narrow layout and keyboard access.
- Pass criteria: source remains intact, customer install commits paused, no automatic sharing or execution, and deleted/revoked source authority prevents pending acceptance.


## CRM tools — WS-02/08/13/14

- Automated: `tests/test_crm_tools.py`, `tests/test_crm.py`, `tests/test_client_workspace_delivery.py`, `tests/test_relationship_ledger.py`, and the scoped Manor read/approval tests in `test_runtime_tool_policy.py`.
- Integration: use an isolated company/database; create and patch contacts, deals and follow-ups through Manor and read them through HTTP. Verify anonymous, external, foreign and revoked actors fail; ordinary members retain CRM access without Staff authority; source managers alone prepare invitations and recipients confirm acceptance separately.
- Association: link an installed current-Workspace subject to an existing Client, repeat without duplication, reject a competing client, update the CRM profile and read it freshly through the ledger tool, then unlink without deleting either history. Model-selected Workspace IDs cannot override the runtime scope; metadata CRUD cannot forge links.
- Manual: refresh the deployed company CRM and make a read-only Manor Chat request for the company CRM overview; confirm genuine tool evidence and no customer writes or outbound messages.
- Pass: Agent and HTTP use the same validation/state services, bindings/approval/credits/lifecycle remain canonical, and no company CRM/history/association enters a Blueprint.


## Customer trial and Group collaboration — WS-01/02/13/14

- Automated: `tests/test_workspace_collaboration.py`, private delivery/CRM, native `tests/test_chat_rooms.py`, Workspace lifecycle and Blueprint v1.1/export regressions.
- Integration: isolated PostgreSQL; trial acceptance retains customer ownership/pause; exact-email authorization does not create Staff/company/Workspace membership. Existing Group association requires current membership and live room/owner; removed members, closed rooms, deleted owners, DM/foreign/missing ids fail closed. Nonmembers receive no room identifiers or automatic invitation. Maintenance revocation leaves independent Group membership intact. Removed submission/review endpoints create no entry, draft or run; Group navigation remains absent from Blueprint even when declared portable.
- Manual: customer delivery stages, collaboration authorization and existing Group selector; accepted guest sees Open Group only with active membership. Check unlinked/unavailable, load/save failure retaining selection, retry, keyboard and desktop/mobile states. Use isolated browser fixtures for mutations, native Group for messages/files.
- Pass: no duplicate submission queue, Group membership grants no company/Workspace authority, no automatic execution or file sharing, current permissions gate native chat/Agent/files, runtime association stays outside export, and expiry/deletion continue to fence portal access.
