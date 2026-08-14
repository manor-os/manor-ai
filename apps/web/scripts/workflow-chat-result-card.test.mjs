import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

const [
  cardSource,
  embeddedSource,
  floatingSource,
  workspaceSource,
  flowsSource,
  chatStreamSource,
  apiSchemaSource,
  stylesSource,
] = await Promise.all([
  read("../src/components/WorkflowResultCard.tsx"),
  read("../src/components/EmbeddedChat.tsx"),
  read("../src/components/FloatingChat.tsx"),
  read("../src/components/WorkspaceChat.tsx"),
  read("../src/pages/Flows.tsx"),
  read("../src/lib/chatStream.ts"),
  read("../../../packages/core/schemas/chat.py"),
  read("../src/index.css"),
]);

test("workflow results use the shared compact card and preserve chat return context", () => {
  assert.match(cardSource, /import CompactCard from "\.\/ui\/CompactCard"/);
  assert.match(cardSource, /import IconTile from "\.\/ui\/IconTile"/);
  assert.match(cardSource, /preserveReturnToInHistory\(currentReturnTo\)/);
  assert.match(cardSource, /query\.set\("run", result\.run_id\)/);
  assert.match(cardSource, /chatReturnTo: currentReturnTo/);
});

test("chat workflow result cards use the message bubble surface without an outline", () => {
  assert.match(
    stylesSource,
    /\.chat-workflow-result-card__surface\.compact-card\s*\{[^}]*border:\s*0;[^}]*background:\s*var\(--message-bubble-other-bg[^}]*box-shadow:\s*var\(\s*--message-bubble-other-shadow/s,
  );
  assert.match(
    stylesSource,
    /\.chat-workflow-result-card__surface\.compact-card:hover\s*\{[^}]*border-color:\s*transparent;[^}]*box-shadow:\s*var\(\s*--message-bubble-other-shadow/s,
  );
});

test("personal, floating, and workspace chat render workflow result cards", () => {
  assert.match(chatStreamSource, /workflow_result\?: WorkflowResultReference/);
  assert.match(apiSchemaSource, /workflow_result: dict \| None = None/);
  assert.match(embeddedSource, /<WorkflowResultCard[\s\S]*result=\{msg\.workflow_result\}/);
  assert.match(floatingSource, /<WorkflowResultCard[\s\S]*result=\{msg\.workflow_result\}/);
  assert.match(workspaceSource, /msg\.meta\?\.workflow_result/);
  assert.match(workspaceSource, /#workspace-chat-message-\$\{msg\.id\}/);
});

test("workflow result links hydrate the exact run and reveal its final result", () => {
  assert.match(flowsSource, /searchParams\.get\("run"\) \|\| searchParams\.get\("workflow_run"\)/);
  assert.match(flowsSource, /api\.workflows\.getRun\(requestedRunId\)/);
  assert.match(flowsSource, /setShowFinalResult\(true\)/);
  assert.match(flowsSource, /next\.set\("run", run\.id\)/);
  assert.match(flowsSource, /workflowReturnTo \? "Back to chat" : "Back to flows"/);
});
