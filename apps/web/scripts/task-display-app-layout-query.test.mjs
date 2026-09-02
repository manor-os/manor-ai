#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export { formatTaskOutputSummary, formatUserFacingStructuredText } from "../src/lib/taskDisplay.ts";
  export { parseAppLayoutChatTarget } from "../src/layouts/appLayoutChatQuery.ts";
  export { redactInternalAssistantErrorDetails } from "../src/lib/assistant-visible-text.mjs";
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
  formatTaskOutputSummary,
  formatUserFacingStructuredText,
  parseAppLayoutChatTarget,
  redactInternalAssistantErrorDetails,
} = await import(moduleUrl);
const appLayoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const tasksPageSource = await readFile(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const taskPropertiesSource = await readFile(
  new URL("../src/components/task/TaskPropertiesPanel.tsx", import.meta.url),
  "utf8",
);

test("task display renders nested plain objects without [object Object]", () => {
  const text = formatUserFacingStructuredText({
    approval_pack: {
      recipient: { name: "Alex Rivera", email: "alex@example.com" },
      proposal: { subject: "Lease renewal approval", amount: 1250 },
    },
    status: "pending_review",
  });

  assert.doesNotMatch(text, /\[object Object\]/);
  assert.match(text, /Approval Pack/);
  assert.match(text, /Alex Rivera/);
  assert.match(text, /Lease renewal approval/);
});

test("assistant display hides internal database details but keeps actionable errors", () => {
  const internalError = [
    "Sorry, the request failed. Please try again.",
    "",
    "Error detail: Internal error: (sqlalchemy.dialects.postgresql.asyncpg.Error)",
    "[SQL: SELECT messages.id FROM messages]",
    "[parameters: ('private-value',)]",
  ].join("\n");
  const actionableError = [
    "Sorry, the request failed. Please try again.",
    "",
    "Error detail: calendar provider timed out",
  ].join("\n");
  const diagnosticAnswer = [
    "Here is the diagnosis.",
    "",
    "Error detail: sqlalchemy could not execute the query.",
  ].join("\n");

  assert.equal(
    redactInternalAssistantErrorDetails(internalError),
    "Sorry, the request failed. Please try again.",
  );
  assert.equal(redactInternalAssistantErrorDetails(actionableError), actionableError);
  assert.equal(redactInternalAssistantErrorDetails(diagnosticAnswer), diagnosticAnswer);
});

test("sidebar workspace selection persists the workspace chat query", () => {
  assert.match(
    appLayoutSource,
    /const openWorkspace = \(\) => \{[\s\S]*?navigate\(`\/chat\?workspace=\$\{encodeURIComponent\(ws\.id\)\}`\);/,
  );
});

test("tasks board only shows Running now for an active execution", () => {
  assert.match(tasksPageSource, /task\.execution_active === true/);
  assert.doesNotMatch(tasksPageSource, /const isProcessingGlow = \(isAI \|\| isWorkspaceTask\) && task\.status === "in_progress"/);
});

test("workspace assignee picker filters agents to active workspace subscriptions", () => {
  assert.match(taskPropertiesSource, /workspaceAgentIds/);
  assert.match(taskPropertiesSource, /filterWorkspaceScopedAgents/);
});

test("new workspace task form filters agents to active workspace subscriptions", () => {
  assert.match(tasksPageSource, /workspace-assignable-agents", formWorkspace/);
  assert.match(tasksPageSource, /const formAssignableAgents = useMemo/);
  assert.match(tasksPageSource, /formAssignableAgents\.map\(\(agent: any\) =>/);
});

test("task display falls back to readable JSON for deeply nested objects", () => {
  const text = formatUserFacingStructuredText({
    pack: { level1: { level2: { level3: { level4: { note: "full approval pack" } } } } },
  });

  assert.doesNotMatch(text, /\[object Object\]/);
  assert.match(text, /full approval pack/);
});

test("task display preserves canonical file links byte-for-byte", () => {
  const fsUrl = "/api/v1/fs/01KXVW5YZRHMDSB9MN4VV6KRV3/Videos/manor-video-contract-e2e-20260810/snapshots/manor-review/frame-05-at-16s.png";
  const viewerUrl = "/viewer/01KZQBAVMH7DZE4Q3CD8GTPT34";
  const content = [
    `[frame-05-at-16s.png](${fsUrl})`,
    `[final-review.mp4](${viewerUrl})`,
  ].join("\n");

  assert.equal(formatUserFacingStructuredText(content), content);
});

test("task display never humanizes addresses or code while still cleaning prose", () => {
  const url = "https://cdn.example.test/manor-video-contract-e2e/final-review.mp4?run=video-edit-v1";
  const code = "`Videos/manor-video-contract-e2e/snapshots/manor-review`";
  const content = `workspace_agent: ${url}\nPath: ${code}`;
  const formatted = formatUserFacingStructuredText(content);

  assert.match(formatted, /^Workspace AI /);
  assert.ok(formatted.includes(url));
  assert.ok(formatted.includes(code));
});

test("task display hides standalone document IDs but keeps them inside viewer links", () => {
  const documentId = "01KZQBAVMH7DZE4Q3CD8GTPT34";
  const link = `[video-edit-recipe.json](/viewer/${documentId})`;
  const content = [
    "Editable recipe:",
    link,
    `Recipe document ID: \`${documentId}\``,
  ].join("\n");
  const formatted = formatUserFacingStructuredText(content);

  assert.ok(formatted.includes(link));
  assert.doesNotMatch(formatted, /Recipe document ID/i);
  assert.equal(formatted.match(new RegExp(documentId, "g"))?.length, 1);
});

test("task display does not use a document ID as a file label", () => {
  const text = formatUserFacingStructuredText({
    documents: [{ document_id: "01KZQBAVMH7DZE4Q3CD8GTPT34" }],
  });

  assert.doesNotMatch(text, /01KZQBAVMH7DZE4Q3CD8GTPT34/);
  assert.match(text, /File 1/);
});

test("task output summary renders a structured result when no prose summary was saved", () => {
  const text = formatTaskOutputSummary({
    plan_status: "completed",
    result: {
      topic_title: "The Two-Minute Reset for a Stuck Task",
      core_promise: "Turn an avoided task into one visible next move.",
      visual_beats: ["Face the wall", "Choose one corner", "Take the next action"],
    },
  });

  assert.match(text, /Two-Minute Reset/);
  assert.match(text, /Core Promise/);
  assert.match(text, /Take the next action/);
});

test("app layout chat query parser gives conversation precedence over workspace", () => {
  assert.deepEqual(
    parseAppLayoutChatTarget("?workspace=workspace_1&conversation=conv%2Fneeds%20encoding"),
    { type: "conversation", conversationId: "conv/needs encoding" },
  );
});

test("app layout chat query parser supports conversationId alias", () => {
  assert.deepEqual(
    parseAppLayoutChatTarget("?conversationId=conv_alias"),
    { type: "conversation", conversationId: "conv_alias" },
  );
});

test("app layout chat query parser keeps workspace fallback behavior", () => {
  assert.deepEqual(
    parseAppLayoutChatTarget("?workspaceId=workspace_2"),
    { type: "workspace", workspaceId: "workspace_2" },
  );
});
