import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const flowsSource = await readFile(
  new URL("../src/pages/Flows.tsx", import.meta.url),
  "utf8",
);

test("new flows persist a visible trigger node", () => {
  assert.match(flowsSource, /trigger_type:\s*formTrigger/);
  assert.match(flowsSource, /type:\s*"trigger"/);
  assert.doesNotMatch(flowsSource, /createMutation\.mutate\(\{\s*name:\s*formName,\s*description:\s*formDesc,\s*trigger:/);
});

test("every flow launch surface blocks explicit-entry errors", () => {
  assert.match(flowsSource, /disabled=\{streaming \|\| errorCount > 0\}/);
  assert.match(flowsSource, /disabled=\{streaming \|\| flowErrorCount > 0\}/);
  assert.match(flowsSource, /validateWorkflow\(flow\.steps \|\| \[\]\).*level === "error"/);
  assert.match(flowsSource, /if \(flowErrorCount === 0\) requestWorkflowRun\(flow\)/);
  assert.match(flowsSource, /if \(errorCount > 0\) \{[\s\S]*?Workflow isn't deployable/);
});

test("worker-resumed runs poll and replace the initial paused result", () => {
  assert.match(flowsSource, /latest\.status === "running" \|\| latest\.status === "paused"/);
  assert.match(flowsSource, /return latest .*\? 2_000 : false/);
  assert.match(flowsSource, /setRunResult\(refreshed\)/);
});
