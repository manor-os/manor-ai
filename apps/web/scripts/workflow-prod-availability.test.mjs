import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const configSource = await readFile(
  new URL("../src/stores/config.ts", import.meta.url),
  "utf8",
);
const layoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const workspaceDetailSource = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);
const scheduledJobsSource = await readFile(
  new URL("../src/pages/ScheduledJobs.tsx", import.meta.url),
  "utf8",
);

test("flows default to coming soon in production like apps", () => {
  assert.match(configSource, /flows_available: boolean/);
  assert.match(configSource, /flows_available: import\.meta\.env\.DEV/);
  assert.match(layoutSource, /const flowsAvailable = useConfigStore\(\(s\) => s\.flows_available\)/);
  assert.match(layoutSource, /const flowsConfigurationItem = \(enabled: boolean\): NavItem/);
  assert.match(layoutSource, /disabled: !enabled/);
  assert.match(layoutSource, /badge: enabled \? undefined : "Soon"/);
  assert.match(layoutSource, /items\.splice\(1, 0, flowsConfigurationItem\(flowsAvailable\)\)/);
});

test("workspace configuration hides workflow surfaces when flows are coming soon", () => {
  assert.match(workspaceDetailSource, /SETUP_TAB_ITEMS\.filter\(\(item\) => flowsAvailable \|\| item\.key !== "workflows"\)/);
  assert.match(workspaceDetailSource, /workflowsEnabled=\{flowsAvailable\}/);
  assert.match(workspaceDetailSource, /normalizedTab === "workflows" && !flowsAvailable \? "overview"/);
  assert.match(scheduledJobsSource, /showWorkflowAutomations \? allJobs : allJobs\.filter\(\(job\) => job\.execution_type !== "workflow"\)/);
  assert.match(scheduledJobsSource, /AUTOMATION_KIND_TABS\.filter\(\(tab\) => tab\.key === "agent_schedule"\)/);
});
