import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const templates = readFileSync(
  new URL("../src/components/workflows/WorkflowTemplates.tsx", import.meta.url),
  "utf8",
);
const flows = readFileSync(new URL("../src/pages/Flows.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");

test("Flow templates are loaded and installed by stable server identity", () => {
  assert.match(templates, /api\.workflows\.templates\(\)/);
  assert.doesNotMatch(templates, /export const TEMPLATES/);
  assert.match(api, /\/workflows\/templates\/\$\{encodeURIComponent\(templateId\)\}\/install/);
  assert.match(flows, /api\.workflows\.installTemplate\(tpl\.id\)/);
  assert.match(flows, /installingId=\{templateMutation\.isPending/);
  assert.doesNotMatch(flows, /steps:\s*tpl\.steps/);
});

test("Flow template cards explain catalogue and runtime identity", () => {
  assert.match(templates, /catalogue ID/);
  assert.match(templates, /Workflow ID/);
  assert.match(templates, /Graph verified/);
  assert.match(templates, /Connect it to any Workspace|connect to any Workspace/);
});
