#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export {
    WORKFLOW_LIVE_EDIT_FORMAT,
    parseWorkflowLiveEdit,
    serializeWorkflowLiveEdit,
  } from "../src/lib/workflowLiveEdit.ts";
`;

const bundled = await build({
  stdin: {
    contents: entryPoint,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;
const {
  WORKFLOW_LIVE_EDIT_FORMAT,
  parseWorkflowLiveEdit,
  serializeWorkflowLiveEdit,
} = await import(moduleUrl);

const flow = {
  name: "Customer intake",
  description: "Classify a request",
  trigger_type: "manual",
  trigger_config: {},
  variables: { locale: "en" },
  category: "support",
  tags: ["intake"],
  steps: [
    { id: "start", type: "trigger", name: "Start", config: {}, next: ["classify"] },
    { id: "classify", type: "llm", name: "Classify", config: {}, next: [] },
  ],
};

test("workflow live edit serializes a complete editor document", () => {
  const serialized = serializeWorkflowLiveEdit(flow);
  const document = JSON.parse(serialized);
  assert.equal(document.format, WORKFLOW_LIVE_EDIT_FORMAT);
  assert.equal(document.name, flow.name);
  assert.deepEqual(document.variables, flow.variables);
  assert.deepEqual(document.steps, flow.steps);
});

test("workflow live edit validates and returns API update fields", () => {
  const update = parseWorkflowLiveEdit(serializeWorkflowLiveEdit(flow));
  assert.deepEqual(update, flow);
});

test("workflow live edit rejects malformed and duplicate-node graphs", () => {
  assert.throws(
    () => parseWorkflowLiveEdit("{}"),
    /must preserve format/,
  );
  const duplicate = JSON.parse(serializeWorkflowLiveEdit(flow));
  duplicate.steps[1].id = "start";
  assert.throws(
    () => parseWorkflowLiveEdit(JSON.stringify(duplicate)),
    /duplicated/,
  );
});

