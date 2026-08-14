#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);

test("workspace chat exposes a permission-gated pause and resume control", () => {
  assert.match(workspaceChatSource, /canManageWorkspace\(currentUser, workspaceStaff \|\| \[\]\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.staff\.list\(workspaceId\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.pause\(workspaceId\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.resume\(workspaceId\)/);
  assert.match(workspaceChatSource, /className="workspace-chat-lifecycle-trigger"/);
  assert.match(workspaceChatSource, /ariaLabel=\{workspaceLifecycleActionLabel\}/);
  assert.match(workspaceChatSource, /<IconPause size=\{17\} \/>/);
  assert.match(workspaceChatSource, /<IconPlay size=\{17\} \/>/);
});

test("workspace chat lifecycle changes refresh shared workspace state", () => {
  assert.match(workspaceChatSource, /setQueryData<Workspace\[\]>\(\["workspaces"\]/);
  assert.match(workspaceChatSource, /invalidateQueries\(\{ queryKey: \["workspace-heartbeat", workspaceId\] \}\)/);
  assert.match(workspaceChatSource, /invalidateQueries\(\{ queryKey: \["workspace-activity", workspaceId\] \}\)/);
});

test("workspace chat lifecycle icon has hover and keyboard focus states", () => {
  assert.match(stylesSource, /\.workspace-chat-lifecycle-trigger\.btn-manor-ghost:hover:not\(:disabled\)/);
  assert.match(stylesSource, /\.workspace-chat-lifecycle-trigger\.btn-manor-ghost:focus-visible \{[\s\S]*var\(--accent-ring\)/);
});
