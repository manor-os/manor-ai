#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const [orb, floating, embedded, workspace, runtime, css] = await Promise.all([
  read("../src/components/ui/AgentActivityOrb.tsx"),
  read("../src/components/FloatingChat.tsx"),
  read("../src/components/EmbeddedChat.tsx"),
  read("../src/components/WorkspaceChat.tsx"),
  read("../src/components/WorkspaceSimulationRuntime.tsx"),
  read("../src/index.css"),
]);

test("chat surfaces use one semantic single-color activity orb", () => {
  assert.match(orb, /theme="light"/);
  assert.match(floating, /<AgentActivityOrb activity=\{activeAgentActivity\}/);
  assert.match(embedded, /<AgentActivityOrb activity=\{activeAgentActivity\}/);
  assert.match(workspace, /<AgentActivityOrb activity=\{activeWorkspaceActivity\}/);
  assert.match(runtime, /activityForRuntimeStage\(runtime\.stageLabel\)/);
  assert.match(css, /\.agent-activity-orb__canvas\s*\{[\s\S]*?filter: brightness\(0\);/);
  assert.match(css, /html\[data-theme="dark"\] \.agent-activity-orb__canvas\s*\{\s*filter: brightness\(0\) invert\(1\);/);
});

test("idle headers do not claim a decorative online state", () => {
  assert.doesNotMatch(floating, /component\.floating_chat\.online/);
  assert.doesNotMatch(embedded, /component\.floating_chat\.online/);
  assert.doesNotMatch(embedded, /<span className="chat-model-badge">AI<\/span>/);
});
