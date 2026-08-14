import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const configSource = await readFile(
  new URL("../src/stores/config.ts", import.meta.url),
  "utf8",
);
const healthSource = await readFile(
  new URL("../../api/routers/health.py", import.meta.url),
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
const previewAccessSource = await readFile(
  new URL("../src/lib/previewFeatureAccess.ts", import.meta.url),
  "utf8",
);
const routerSource = await readFile(
  new URL("../src/router.tsx", import.meta.url),
  "utf8",
);
const adminFlagsSource = await readFile(
  new URL("../src/admin/pages/Flags.tsx", import.meta.url),
  "utf8",
);

test("flows are released locally but default to coming soon in production", () => {
  assert.match(configSource, /flows_available: boolean/);
  assert.match(configSource, /flows_available: import\.meta\.env\.DEV/);
  assert.match(configSource, /flows_released: boolean/);
  assert.match(configSource, /flows_released: import\.meta\.env\.DEV/);
  assert.match(healthSource, /"flows_released": _feature_available\("FLOWS_RELEASED", environment\)/);
  assert.match(layoutSource, /usePreviewFeatureAccess\("flows"\)/);
  assert.match(layoutSource, /const flowsConfigurationItem = \(/);
  assert.match(layoutSource, /disabled: !enabled/);
  assert.match(layoutSource, /badge: comingSoon \? "Soon" : undefined/);
  assert.match(layoutSource, /items\.push\(flowsConfigurationItem\(flowsAvailable, !flowsReleased\)\)/);
  assert.match(previewAccessSource, /flows: "flows_preview_access"/);
  assert.match(previewAccessSource, /apps: "apps_preview_access"/);
  assert.match(previewAccessSource, /enabled: configured \|\| previewEnabled/);
  assert.match(previewAccessSource, /if \(feature === "flows"\) return state\.flows_released/);
  assert.match(previewAccessSource, /released,/);
  assert.match(routerSource, /<PreviewFeatureRoute feature="flows">/);
  assert.match(routerSource, /<PreviewFeatureRoute feature="apps">/);
});

test("workspace configuration hides workflow surfaces when flows are coming soon", () => {
  assert.match(workspaceDetailSource, /usePreviewFeatureAccess\("flows"\)/);
  assert.match(workspaceDetailSource, /SETUP_TAB_ITEMS\.filter\(\(item\) => flowsAvailable \|\| item\.key !== "workflows"\)/);
  assert.match(workspaceDetailSource, /workflowsEnabled=\{flowsAvailable\}/);
  assert.match(workspaceDetailSource, /normalizedTab === "workflows" && !flowsAvailable \? "overview"/);
  assert.match(scheduledJobsSource, /usePreviewFeatureAccess\("flows"\)/);
  assert.match(scheduledJobsSource, /workflowsEnabled \?\? flowsAccess\.enabled/);
  assert.match(scheduledJobsSource, /showWorkflowAutomations \? allJobs : allJobs\.filter\(\(job\) => job\.execution_type !== "workflow"\)/);
  assert.match(scheduledJobsSource, /AUTOMATION_KIND_TABS\.filter\(\(tab\) => tab\.key === "agent_schedule"\)/);
});

test("configuration navigation orders integrations, agents, skills, then flows", () => {
  const integrations = layoutSource.indexOf('path: "/integrations"');
  const agents = layoutSource.indexOf('path: "/agents"');
  const skills = layoutSource.indexOf('path: "/skills"');
  const flows = layoutSource.indexOf("items.push(flowsConfigurationItem(flowsAvailable, !flowsReleased))");
  assert.ok(integrations >= 0 && integrations < agents);
  assert.ok(agents < skills);
  assert.ok(skills < flows);
});

test("admin preview controls select real accounts instead of requiring user ids", () => {
  assert.match(adminFlagsSource, /flows_preview_access: "Flows preview"/);
  assert.match(adminFlagsSource, /apps_preview_access: "Apps preview"/);
  assert.match(adminFlagsSource, /adminApi\.flags\.accountTargets\(deferredAccountSearch\)/);
  assert.match(adminFlagsSource, /aria-label="Search accounts"/);
  assert.match(adminFlagsSource, /aria-label="Account"/);
});
