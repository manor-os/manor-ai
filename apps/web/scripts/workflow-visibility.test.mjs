#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const flowsSource = await readFile(
  new URL("../src/pages/Flows.tsx", import.meta.url),
  "utf8",
);
const workspaceWorkflowsSource = await readFile(
  new URL("../src/components/workflows/WorkspaceWorkflows.tsx", import.meta.url),
  "utf8",
);


test("workflow libraries hide internal definitions from operator lists", () => {
  assert.match(flowsSource, /const visibleFlows = .*trigger_type !== "internal"/);
  assert.match(flowsSource, /const filtered = visibleFlows\.filter/);
  assert.match(workspaceWorkflowsSource, /workflow\.trigger_type !== "internal"/);
  assert.match(workspaceWorkflowsSource, /function isOperatorBinding/);
  assert.match(workspaceWorkflowsSource, /binding\.trigger_type === "manual"/);
  assert.match(workspaceWorkflowsSource, /chatEntryPoint\?\.enabled === true/);
  assert.match(workspaceWorkflowsSource, /filter\(isOperatorBinding\)/);
});
