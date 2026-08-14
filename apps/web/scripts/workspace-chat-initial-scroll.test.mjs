#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const workspaceChat = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);

test("workspace chat keeps the initial view at the latest message", () => {
  assert.match(
    workspaceChat,
    /const initialPendingActionIdsRef = useRef<Set<string>>\(new Set\(\)\);/,
  );
  assert.match(
    workspaceChat,
    /if \(initialPendingActionIdsRef\.current\.has\(latestPendingActionId\)\) return;/,
  );
});

test("workspace chat sets the initial scroll position without an animation", () => {
  assert.match(
    workspaceChat,
    /const previousScrollBehavior = container\.style\.scrollBehavior;\s+container\.style\.scrollBehavior = "auto";\s+container\.scrollTop = container\.scrollHeight;\s+container\.style\.scrollBehavior = previousScrollBehavior;/,
  );
});
