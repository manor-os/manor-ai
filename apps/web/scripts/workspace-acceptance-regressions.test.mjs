#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const taskDetail = await readFile(
  new URL("../src/pages/TaskDetail.tsx", import.meta.url),
  "utf8",
);
const tasks = await readFile(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const workspaceDetail = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);
const i18n = await readFile(
  new URL("../src/lib/i18n/en.ts", import.meta.url),
  "utf8",
);
const scheduledJobs = await readFile(
  new URL("../src/pages/ScheduledJobs.tsx", import.meta.url),
  "utf8",
);
const pageHeader = await readFile(
  new URL("../src/components/ui/PageHeader.tsx", import.meta.url),
  "utf8",
);

test("manual task status does not expose recovery without an execution origin", () => {
  assert.match(
    taskDetail,
    /const hasTaskRecoveryOrigin = Boolean\(latestPlan\)[\s\S]*?RECOVERY_EVENT_TYPES/,
  );
  assert.match(
    taskDetail,
    /const showTaskRecoveryPanel = planDecisionStateResolved\s*&& hasTaskRecoveryOrigin/,
  );
  assert.match(
    tasks,
    /const hasTaskRecoveryOrigin = Boolean\(latestPlan\)[\s\S]*?RECOVERY_EVENT_TYPES/,
  );
  assert.match(
    tasks,
    /planDecisionStateResolved\s*&& hasTaskRecoveryOrigin\s*&& \(/,
  );
});

test("terminal timestamp uses the task terminal status as its label", () => {
  assert.match(
    taskDetail,
    /const terminalStatusLabel = t\(\(STATUS_CONFIG\[task\.status\] \|\| STATUS_CONFIG\.pending\)\.labelKey\);/,
  );
  assert.match(
    taskDetail,
    /label: terminalStatusLabel, value: task\.completed_at \? formatDateFull\(task\.completed_at\) : null/,
  );
  assert.doesNotMatch(
    taskDetail,
    /label: t\("status\.completed"\), value: task\.completed_at/,
  );
});

test("workspace table actions remain reachable through local horizontal scrolling", () => {
  assert.equal(
    (workspaceDetail.match(/<div style=\{\{ overflowX: "auto" \}\}>/g) || []).length >= 2,
    true,
  );
  assert.match(workspaceDetail, /<table style=\{\{ width: "100%", minWidth: 497, borderCollapse: "collapse" \}\}>/);
  assert.match(workspaceDetail, /<table style=\{\{ width: "100%", minWidth: 458, borderCollapse: "collapse" \}\}>/);
});

test("workspace agent picker excludes other workspace agents and legacy template duplicates", () => {
  assert.match(workspaceDetail, /function filterWorkspaceAgentMappingOptions\(/);
  assert.match(workspaceDetail, /agent\.workspace_id.*workspaceId/);
  assert.match(workspaceDetail, /agent\.source !== "marketplace_template"/);
  assert.match(workspaceDetail, /options=\{filterWorkspaceAgentMappingOptions\(entityAgents \|\| \[\], workspaceId\)/);
});

test("workspace settings use translated labels and live automation totals", () => {
  assert.match(workspaceDetail, /t\("page\.workspace_detail\.learning_on"\)/);
  assert.match(workspaceDetail, /t\("page\.workspace_detail\.edit_configuration"\)/);
  assert.match(workspaceDetail, /t\("page\.workspace_detail\.services"\)/);
  assert.match(workspaceDetail, /workspaceAutomationSummary\s*\?/);
  assert.match(workspaceDetail, /workspaceAutomationSummary\.summary_total/);
  assert.match(workspaceDetail, /workspaceAutomationBindings \|\| \[\]/);
  for (const key of [
    "page.workspace_detail.learning_on",
    "page.workspace_detail.learning_paused",
    "page.workspace_detail.edit_configuration",
    "page.workspace_detail.services",
  ]) {
    assert.match(i18n, new RegExp(`\\"${key}\\"\\s*:`));
  }
});

test("paused workspace automation controls are fail-closed", () => {
  assert.match(workspaceDetail, /workspaceStatus=\{ws\.status\}/);
  assert.match(scheduledJobs, /workspaceStatus !== "active"/);
});

test("shared page header constrains mobile action groups to the viewport", () => {
  assert.match(pageHeader, /const groupClass =\s*\n\s*"[^\"]*max-w-full/);
  assert.match(pageHeader, /const actionsClass =\s*\n\s*"[^\"]*max-w-full/);
});
