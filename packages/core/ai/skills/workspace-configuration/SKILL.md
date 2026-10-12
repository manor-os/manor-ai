---
name: workspace-configuration
description: Adjust Workspace services, Agent assignments, tool and Skill bindings, integrations, rules, budget and governance through configuration drafts.
version: 1.0.1
---

# Workspace Configuration

Read operation get_current before proposing a persistent change. It returns
the operating model, services, agent mappings, bindings and effective policy.
The generated operation_parameters schema is the inner params of
manor(action="workspace", params={"action":"operation", "params":{...}}).
The compatibility workspace_operation name is not a separately exposed tool
for internal master Chat. Use real schemas and catalog IDs, not inferred payloads.
For name, description, category, primary_work or operating_context, read
manor(action="get_workspace") and then
manor(action="update_workspace", params={"name":..., "description":..., "category":...}).
The active Workspace is supplied by the runtime. Framing fields refresh Agent
subscription identities. Operating model, budget and heartbeat changes require
operation drafts.
Use the operation's current patch catalog; arrays replace their entire
collections unless the named upsert operation says otherwise.

Adding an Agent requires an Agent mapping. service_role.upsert defines service
metadata only. For an additional service, use a new service_key and include
service_role.upsert plus agent_mapping.upsert in one draft. Reusing a service_key
replaces its Agent. Discover existing Agent IDs or prepare a complete
create_agent_draft with system_prompt, tool_bindings and skill_bindings.
Discover real tool and Skill identities before binding them. Workspace-only
Skill bindings use skill_binding.upsert with owner_scope and owner_service_key;
tool/MCP bindings use capability_binding.upsert with their actual transport.

Inspect the actor's connections with manor(action="list_integrations",
params={"ready_only":true}); use the returned provider/server_key. Read existing
capability_bindings and integration_scope.disabled_providers from get_current.
To add a connected provider, draft:
{"op":"capability_binding.upsert","payload":{"binding":{"owner_scope":"workspace_agent","capability_type":"mcp","integration_key":"<provider>"}}}.
Use owner_scope="service" plus owner_service_key only for a specific service.
Include allowed_tools only when narrowing the server's actions; use exact
tool names or action names from the actor's live provider catalog.
To stop using a provider throughout this Workspace, draft:
{"op":"integration.remove","payload":{"provider":"<provider>"}}.
To restore a disabled provider, use integration.enable with the same payload.
Restoration alone reuses inherited bindings/requirements; it does not add a new
binding. If a newly requested provider is also disabled, enable it and upsert
the intended binding in one draft. integration_scope.update accepts only
disabled_providers:string[] and replaces that list, so retain other exclusions.
capability_binding.remove with an existing binding_key removes one explicit
binding; it does not disable inherited provider access throughout the Workspace.
Connection readiness is checked both while drafting and again on apply. A
disconnected provider needs the actor to connect it in Integration settings;
never ask for credentials in Chat or use another member's private account.
These Workspace edits preserve account credentials and shared Agent definitions.
After approval, re-read the bindings and disabled providers. Do not test a
connection by sending messages, publishing, spending money or starting work.

For guardrail changes, identify source rule_key and rule_type in get_current.
Patch rule.update/remove as requested; changing governance_policy alone does
not remove a rule-derived ban. Preserve approval requirements the user keeps.
Task-only requirements belong to update_task_runtime, not Workspace rules.
For service removal, use service_role.remove and the intended mapping/binding
removal ops. Rules, agent_mappings and operating_model replace operations replace
collections, so retain all unaffected rows. process.upsert records descriptive
process metadata, not a runnable Workflow or schedule. Evaluation, budget,
heartbeat, strategist and channel settings use their corresponding patch ops.

Create a nonempty draft, inspect validation/diff, and apply only after its review
is approved. A rejection, stale version or missing dependency is a failed apply,
not permission to bypass the draft path. Re-read get_current after application;
verify actual mappings and bindings, not only service names. Guides and bindings
do not grant execution authority. Blueprint exports portable configuration,
not this conversation's approvals, Task runs or cached runtime state.
