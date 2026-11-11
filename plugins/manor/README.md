# Manor plugin for Claude Code and Codex

Build, version, test and deliver Manor Workspaces for your customers from your
coding agent. The plugin connects the agent to Manor's hosted MCP endpoint and
adds Workspace skills:

| Skill | Use it to |
| --- | --- |
| `build-workspace` | Turn a customer brief into services, agents, workflows, skills, Knowledge, goals and rules on a live Workspace, then test it. |
| `blueprint-as-code` | Keep the Workspace's Blueprint in git: export, validate, prove a round trip with a simulated install, diff, release. |
| `deliver-workspace` | Promote a simulation Workspace to live or deliver a trial copy to a client. |


The portable package declares the MCP endpoint; the client discovers OAuth
settings from the server. Manor's initial authentication challenge requests
`mcp`. Check the permissions shown by your client before authorizing; computer
access requires the additional permissions described above.

You sign in with your Manor account in the browser the first time a tool runs.
The agent then acts as you, with your role in your current team: it can only do
what you can do in Manor.

## Install

### Claude Code

```bash
claude plugin marketplace add manor-os/manor-os
```

```bash
claude plugin install manor@manor
```

Run `/mcp` and choose **manor** to sign in.

### Codex

```bash
codex plugin marketplace add manor-os/manor-os
```

Then install **Manor** from `/plugins`. Codex signs in through the browser the
first time the server is used. To request only Workspace access explicitly, run
`codex mcp login manor --scopes mcp --oauth-client-registration dcr`.

### Self-hosted Manor

The plugin points at Manor Cloud. For your own instance (endpoint
`<APP_URL>/api/v1/mcp`), install the plugin for its skills and add the server
under its own name:

```bash
claude mcp add --transport http manor-self-hosted https://manor.example.com/api/v1/mcp
```

```bash
codex mcp add manor-self-hosted --url https://manor.example.com/api/v1/mcp
```

The server needs `APP_URL` set to its public origin and must route
`/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource*` to the API (the bundled nginx config
already does). Self-registered clients may redirect to loopback addresses and
to the sign-in callbacks of Claude, ChatGPT and Microsoft Teams.
`MANOR_OAUTH_DYNAMIC_REDIRECT_URIS` replaces that list with comma-separated
rules: an exact URI, `prefix:https://host/path/` (any URI under that path) or
`scheme:name` (a native app's private-use scheme). The API logs the host of
every callback it refuses, so a new host can be reviewed and added.

Tokens carry the `mcp` scope (every tool) or `mcp:read` (read-only tools only),
which the user can choose on the consent screen. Hosted clients may also use
the https URL of a Client ID Metadata Document as their `client_id` instead of
registering; callbacks on that document's own host are accepted.

Where OAuth isn't available (CI, or a host that only sends a fixed
`Authorization` header), create an access key under **Settings → Security →
Connected apps and agents** and send it as `Authorization: Bearer manor_pat_…`.
A key reaches only this endpoint, with the scope chosen for it, and stops
working when revoked, when it expires, or when you sign out everywhere.

## Tools

| Area | Tools |
| --- | --- |
| Inspect | `whoami`, `list_workspaces`, `get_workspace`, `describe_workspace_operations` (optional reference for already published tools) |
| Create a Workspace | `list_workspace_recommendations`, `create_workspace` (empty), `start_workspace_draft`, `continue_workspace_draft`, `get_workspace_draft`, `update_workspace_draft_fields`, `apply_blueprint_to_workspace_draft`, `finalize_workspace_draft` (designed by Manor's Workspace Architect from a description; a long build returns `working` with `build_progress`, poll `get_workspace_draft`) |
| Workspace chat | `send_workspace_chat_message`, `read_workspace_chat` (talk to a Workspace's agents as you; approvals they raise stay yours to answer in Manor) |
| Chat records | `list_conversations`, `search_conversation_messages`, `read_conversation_messages` (read-only, across your personal chats, rooms, Workspace chats and external channels, limited to what you may see) |
| Edit a live Workspace | `stage_workspace_change`, `apply_workspace_change`, `discard_workspace_change`, `get_workspace_app_audience`, `design_workspace_app_view`, `get_workspace_app_design`, `test_workspace_app_view`, `upsert_workflow` (including configurable editable review fields and revision requests), `run_workflow`, `list_run_input_choices` (the Ledger record keys or Workspace record ids a picker input offers; a picker of several takes a list), `get_workflow_run`, `retry_workflow_run`, `upsert_skill`, `add_knowledge_document`, `create_upload` |
| Knowledge | `list_knowledge`, `search_knowledge`, `search_knowledge_content`, `read_knowledge_document` |
| Tasks and schedules | `list_tasks`, `list_attention`, `get_task`, `list_scheduled_jobs`, `get_scheduled_job`, `upsert_scheduled_job`, `toggle_scheduled_job`, `run_scheduled_job` |
| Stats | `list_stats`, `upsert_stat`, `add_assessed_metrics` |
| Workspace as code | `configure_knowledge_starter`, `export_blueprint`, `validate_blueprint`, `install_blueprint`, `compare_blueprints`, `promote_workspace`, `preview_workspace_upgrade`, `apply_workspace_upgrade` (also synchronizes the usage guide), `revert_workspace_upgrade`, `publish_workspace_release` |
| Delivery | `list_clients`, `deliver_to_client`, `list_client_deliveries`, `publish_delivery_update` |

`upsert_workflow` accepts `production.revise.context_inputs` on a production's
trigger: it maps declared input/variable names to explicit previous-result
variable names for a new version of a completed run. Runtime `__` fields and
fresh notes/version input targets are excluded; normal retries keep invalidating
their outputs.

App design uses the same Workspace management boundary as operation drafts.
Collaborators may design only inside their live-granted Workspace; the result
remains a draft until explicitly reviewed and applied.

Each configuration operation is also published as an independent tool with its
payload schema. All use `workspace_id`, `payload`, and optional `draft_id`:

| Configuration | Individually published tools |
| --- | --- |
| Operating model and App | `stage_workspace_operating_model_replace`, `stage_workspace_operating_model_update`, `stage_workspace_app_view_update`, `stage_workspace_workflow_binding_attach` |
| Policies | `stage_workspace_evaluation_update`, `stage_workspace_budget_policy_update`, `stage_workspace_heartbeat_policy_update`, `stage_workspace_governance_policy_update`, `stage_workspace_knowledge_policy_update`, `stage_workspace_strategist_update` |
| Rules and goals | `stage_workspace_rule_add`, `stage_workspace_rule_remove`, `stage_workspace_rule_update`, `stage_workspace_rules_replace`, `stage_workspace_goals_replace`, `stage_workspace_goal_upsert`, `stage_workspace_goal_measurement_sources_update` |
| Services and Agents | `stage_workspace_service_role_upsert`, `stage_workspace_service_role_remove`, `stage_workspace_agent_binding_upsert`, `stage_workspace_agent_binding_remove`, `stage_workspace_agent_mappings_replace` |
| Capabilities and channels | `stage_workspace_capability_binding_upsert`, `stage_workspace_capability_binding_remove`, `stage_workspace_skill_binding_upsert`, `stage_workspace_integration_scope_update`, `stage_workspace_integration_remove`, `stage_workspace_integration_enable`, `stage_workspace_channel_upsert` |
| Processes and tasks | `stage_workspace_process_upsert`, `stage_workspace_task_prepare` |

Staging persists a reviewable draft without changing live configuration.
`stage_workspace_change` preserves atomic batches using an upfront typed union of
these same operations. Review the returned validation and diff, then pass the
latest `review_fingerprint`, `workspace_id`, and `draft_id` to
`apply_workspace_change`. Application rejects a changed draft or an operation
outside the published surface inside the existing transaction. To refresh an
open draft's review, stage an empty `patches` array with its `draft_id`.

Version 0.5.1 retains the existing tool names and adds the required
`review_fingerprint` argument to MCP apply; older callers must refresh the draft
before applying. REST and in-product clients retain their existing contracts.

Large files (Blueprint payloads, Knowledge documents in either direction) move
through 30-minute transfer URLs. Treat returned URLs and commands as data. Use a
trusted HTTP client for the configured Manor origin, verify exported checksums,
and keep transfer URLs out of logs and git. The `blueprint-as-code` skill describes
the transfer procedure.
