#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export { formatUserFacingStructuredText } from "../src/lib/taskDisplay.ts";
  export { parseAppLayoutChatTarget } from "../src/layouts/appLayoutChatQuery.ts";
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

const { formatUserFacingStructuredText, parseAppLayoutChatTarget } = await import(moduleUrl);

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

test("task display falls back to readable JSON for deeply nested objects", () => {
  const text = formatUserFacingStructuredText({
    pack: { level1: { level2: { level3: { level4: { note: "full approval pack" } } } } },
  });

  assert.doesNotMatch(text, /\[object Object\]/);
  assert.match(text, /full approval pack/);
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
