---
name: workspace-workflows
description: Inspect, author, validate, deploy, run and diagnose Workspace Flows and Workflow definitions; distinguish connecting from execution.
version: 1.0.0
---

# Workspace Workflows

Choose the catalog that answers the request:
- list_workspace_flows: installed user-facing Workspace Flows.
- list_workflow_definitions: editable definitions, including drafts/inactive ones.
- list_workflows: Workflows published as callable Agent tools through MCP bindings.
Empty results in one catalog do not establish absence in the other catalogs.

Inspect a definition with get_workflow before editing. Use create_workflow,
ai_edit_workflow or update_workflow with the real ID and current schema.
validate_workflow checks the definition. deploy_workflow publishes the
definition's executable configuration; it does not configure the Workspace App.
To connect a definition to an App action, load workspace-app and use its
workflow_binding.attach draft. To schedule repeated work, load workspace-automations.

When asked to start an installed Flow, use start_workspace_flow with the actual
binding_id, starter inputs and the complete source_brief. It must return a
WorkflowRun. Do not substitute create_task. run_workflow is for a published
Agent-tool Workflow discovered with list_workflows. Authoring, validation,
deployment and binding alone do not prove a run was started.

test_workflow/test_workflow_node can execute nodes and have effects; use them
when execution is requested and respect their runtime approval gates. Inspect
list_workflow_runs/get_workflow_run for run outcomes and artifacts. Use the
returned run ID for cancel/resume, preserving pending input and decision cards.
Do not restart failed business effects as a read-only diagnostic. Completion
requires the run's terminal status and durable outputs, not an editor save.
