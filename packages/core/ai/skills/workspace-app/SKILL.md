---
name: workspace-app
description: Configure the Workspace App layout, main action, forms and internal links to Agent Chat, Workflow or Automation.
version: 1.0.2
---

# Workspace App

First call `manor(action="workspace", params={"action":"operation", "params":{"action":"get_current"}})`.
Read `app_view`, `available_actions`, `workflow_options`, `agent_options`,
`automation_options` and `attachable_workflow_options`. Empty Knowledge results
do not prove an App or Workflow is missing. Unattached saved Workflows can still
be available to connect. Do not select an unrelated Workflow merely because it exists.

For titles, descriptions, visible/order settings, primary action, input forms
and internal execution targets, create a configuration draft using `app_view.update`.
Use returned action keys for existing entries and catalog IDs for runner references.
New entries do not require a Workspace Service: add
`entries:{"action:<unique id>":{"title":"Entry name"}}`, with a typed target in
`targets`. Agent command text goes in `prompts:{"action:<id>":"Instruction"}`;
input questions go in `forms`. Multiple entries can use the same Agent with
different commands. Remove a custom entry with `entries:{"action:<id>":null}`;
this removes only its App display settings and preserves the runner.
To target a saved Workflow, add the entry and `workflow_binding.attach` with
its `action_key` in the same draft. Do not invent or create a Service for an App entry. Internal targets use `agent:<id>`,
`agent:default`, `workflow:<binding_id>` or `automation:<job_id>`. Switching
Run with must update the action's target and matching configuration together.
Preserve other actions and the user's existing layout. Read the tool's live
patch contract for collection replacement semantics before supplying arrays.

To connect an unattached Workflow, use `workflow_binding.attach` with its actual
`workflow_id`, optional existing `action_key` (or `service_key`, never both), and
`primary:true` when it should be the main action. The reviewed apply creates
the binding and App action; it does not deploy or run the Workflow.

Use create_draft with nonempty patches, then validate_draft/preview_diff when
needed. The returned review card owns approval; apply_draft only after approval.
After apply, re-read get_current and verify the target, primary action and layout.
If multiple workflows fit and the intended one is unclear, ask for the choice
before drafting a connection. An unavailable target requires explaining the
specific missing binding or configuration, not declaring that the whole App is absent.
