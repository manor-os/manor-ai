import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url), "utf8");
const start = source.indexOf("function renderChannels()");
const channels = source.slice(start, source.indexOf("\n  function ", start + 1));

test("channel mutation affordances use management, not write, authority", () => {
  assert.match(channels, /canManageWs && \(\s*<Button[^>]*onClick=\{openAddChannel\}/);
  assert.match(channels, /action=\{canManageWs \? \(/);
  assert.match(channels, /canManageWs && ch\.channel_binding_id && \(/);
  assert.match(channels, /const openAddChannel = \(\) => \{\s*if \(!canManageWs\) return;/);
  assert.match(channels, /const openEditChannel = \(ch: any\) => \{\s*if \(!canManageWs\) return;/);
});

test("permission loss closes channel mutation surfaces and prevents requests", () => {
  assert.ok(source.includes("enabled: !!workspaceId && canManageWs && showChannelModal"));
  assert.ok(channels.includes("open={canManageWs && showChannelModal}"));
  assert.ok(channels.includes("if (!canManageWs) return;"));
  assert.ok(source.includes("open={canManageWs && !!confirmRemoveChannel}"));
  assert.ok(source.includes("if (canManageWs && confirmRemoveChannel) removeChannel.mutate"));
  assert.match(source, /if \(canManageWs\) return;\s*setShowChannelModal\(false\);\s*setEditingChannel\(null\);\s*setConfirmRemoveChannel\(null\);/);
});
