#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const agentsSource = await readFile(
  new URL("../src/pages/Agents.tsx", import.meta.url),
  "utf8",
);
const avatarSource = await readFile(
  new URL("../src/components/ui/AgentAvatar.tsx", import.meta.url),
  "utf8",
);
const composerSource = await readFile(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);

test("workspace chat resolves member identity from workspace mappings", () => {
  assert.match(apiSource, /request<WorkspaceAgentMapping\[\]>\(`\/workspaces\/\$\{wsId\}\/agents`\)/);
  assert.match(workspaceChatSource, /if \(m\.agent\) \{/);
  assert.match(workspaceChatSource, /name: m\.agent\.name/);
  assert.match(workspaceChatSource, /avatar_url: m\.agent\.avatar_url \|\| undefined/);
  assert.match(workspaceChatSource, /avatar_seed: m\.agent\.avatar_seed/);
  assert.match(workspaceChatSource, /seed=\{agentAvatarSeed\(agent\)\}/);
  assert.doesNotMatch(workspaceChatSource, /queryFn: \(\) => api\.agents\.list\(\)/);
  assert.match(workspaceChatSource, /const readOnlyTemplate = full\.is_template && !full\.entity_id/);
  assert.match(workspaceChatSource, /primaryAction: readOnlyTemplate/);
});


test("failed unsubscribe clears the pending unsubscribe state", () => {
  assert.match(
    agentsSource,
    /onError: \(\) => \{[\s\S]*?deleteIsUnsubscribeRef\.current = false;[\s\S]*?\}/,
  );
});
