import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const scheduledJobsSource = await readFile(
  new URL("../src/pages/ScheduledJobs.tsx", import.meta.url),
  "utf8",
);
const workspaceWorkflowsSource = await readFile(
  new URL("../src/components/workflows/WorkspaceWorkflows.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const flowsSource = await readFile(new URL("../src/pages/Flows.tsx", import.meta.url), "utf8");
const compactCardSource = await readFile(new URL("../src/components/ui/CompactCard.tsx", import.meta.url), "utf8");
const stylesSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

test("workspace automations configure scheduled and event-triggered workflows", () => {
  assert.match(scheduledJobsSource, /"workflow_schedule"/);
  assert.match(scheduledJobsSource, /"workflow_event"/);
  assert.match(scheduledJobsSource, /execution_type = "workflow"/);
  assert.match(scheduledJobsSource, /trigger_type: "workspace_event"/);
  assert.match(scheduledJobsSource, /trigger_config: \{ event: fEvent\.trim\(\) \}/);
  assert.match(scheduledJobsSource, /workspace_workflow_binding_id = fWorkspaceBinding/);
  assert.match(scheduledJobsSource, /binding_id: fWorkspaceBinding/);
  assert.match(scheduledJobsSource, /Select an attached workflow/);
});

test("workspace workflows are attached independently from their automations", () => {
  assert.match(workspaceWorkflowsSource, /trigger_type: "manual"/);
  assert.match(workspaceWorkflowsSource, /workspace_attached: true/);
  assert.match(workspaceWorkflowsSource, /listRuns\(\{ workspace_id: workspaceId/);
  assert.match(workspaceWorkflowsSource, /runBinding\(bindingId/);
  assert.match(workspaceWorkflowsSource, /Attach workflow/);
  assert.match(workspaceWorkflowsSource, /Detach workflow/);
  assert.match(workspaceWorkflowsSource, /navigate\(`\/flows\?workflow=\$\{encodeURIComponent\(binding\.workflow_id\)\}`\)/);
  assert.match(flowsSource, /searchParams\.get\("workflow"\)/);
  assert.match(flowsSource, /const requested = \(flows as Flow\[\]\)\.find/);
  assert.match(flowsSource, /next\.delete\("workflow"\)/);
});

test("workspace workflow bindings can be created, updated, run, paused, and deleted", () => {
  assert.match(apiSource, /createBinding: \(data: any\)/);
  assert.match(apiSource, /updateBinding: \(bindingId: string, data: any\)/);
  assert.match(apiSource, /deleteBinding: \(bindingId: string\)/);
  assert.match(apiSource, /runBinding: \(bindingId: string, data\?: any\)/);
  assert.match(apiSource, /runBinding:[\s\S]*?"X-Silent-Error": "1"/);
  assert.match(scheduledJobsSource, /toggleBindingMut/);
  assert.match(scheduledJobsSource, /runBindingMut/);
  assert.match(scheduledJobsSource, /deleteBindingMut/);
});

test("workspace workflow automation controls remain responsive and accessible", () => {
  assert.match(scheduledJobsSource, /aria-label="Workspace event name"/);
  assert.match(scheduledJobsSource, /aria-label=\{`\$\{enabled \? "Pause" : "Enable"\}/);
  assert.match(scheduledJobsSource, /aria-label=\{`\$\{job\.enabled \? "Pause" : "Enable"\}/);
  assert.match(scheduledJobsSource, /aria-label=\{`Run \$\{/);
  assert.match(scheduledJobsSource, /aria-label=\{`Edit \$\{/);
  assert.match(scheduledJobsSource, /aria-label=\{`Delete \$\{/);
  assert.match(stylesSource, /@media \(max-width: 720px\) \{[\s\S]*?\.scheduled-job-actions/);
  assert.match(stylesSource, /\.workspace-workflow-automation-action:focus-visible/);
  assert.match(compactCardSource, /e\.target !== e\.currentTarget/);
});
