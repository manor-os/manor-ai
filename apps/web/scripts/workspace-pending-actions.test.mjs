#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const workspaceChat = readFileSync(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const localeSources = ["en", "zh", "es"].map((locale) =>
  readFileSync(
    new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url),
    "utf8",
  ),
);

test("pending-action summaries describe the request instead of exposing its enum", () => {
  assert.match(workspaceChat, /function pendingActionLabel\(msg: WsMessage\)/);
  assert.match(workspaceChat, /taskRef\?\.title \|\| taskRef\?\.name/);
  assert.match(workspaceChat, /action\?\.review_title/);
  assert.match(workspaceChat, /payload\?\.action_description/);
  assert.match(workspaceChat, /\.map\(\(msg\) => pendingActionLabel\(msg\)\)/);
  assert.doesNotMatch(
    workspaceChat,
    /formatUserFacingText\(kind\.replace\(\/_\/g, ["'] ["']\)\)/,
  );
});

test("pending-action banner only appears while autonomous mode is running", () => {
  assert.match(
    workspaceChat,
    /const showPendingActionsBanner =\s*autonomousRunning && openActionCount > 0 && pendingActions\.length > 0;/,
  );
  assert.match(workspaceChat, /\{showPendingActionsBanner && \(/);
});

test("every shipped locale has friendly generic and named-retry fallbacks", () => {
  for (const source of localeSources) {
    assert.match(source, /component\.workspace_chat\.pending_action_governance_approval/);
    assert.match(source, /component\.workspace_chat\.pending_action_retry_named/);
    assert.match(source, /component\.workspace_chat\.pending_action_review_requested/);
  }
});
