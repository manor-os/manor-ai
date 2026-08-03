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

test("the workflow AI edit button reuses FloatingChat live edit", async () => {
  const [flowsSource, floatingChatSource, editorLiveSource, guidanceSource] = await Promise.all([
    readFile(new URL("../src/pages/Flows.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/components/FloatingChat.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/lib/editorLiveChat.ts", import.meta.url), "utf8"),
    readFile(new URL("../../../packages/core/ai/runtime/prompt_guidance.py", import.meta.url), "utf8"),
  ]);

  assert.match(flowsSource, /openEditorLiveChat\(\{/);
  assert.match(flowsSource, /fileType: "workflow"/);
  assert.match(flowsSource, /getContent: \(\) => serializeWorkflowLiveEdit/);
  assert.match(flowsSource, /const update = parseWorkflowLiveEdit\(content\)/);
  assert.match(flowsSource, /<AiEditButton[^>]*onClick=\{openWorkflowAiEdit\}/);
  assert.match(flowsSource, /closeEditorLiveChat\(\)/);
  assert.doesNotMatch(flowsSource, /WorkflowAiPanel/);

  assert.match(editorLiveSource, /sessionLabel\?: string \| null/);
  assert.match(editorLiveSource, /emptyDescription\?: string \| null/);
  assert.match(floatingChatSource, /editorLiveInfo\?\.examples\?\.length/);
  assert.match(guidanceSource, /Workflow editor-state requirements:/);
  assert.match(guidanceSource, /manor-workflow-v1/);
});
