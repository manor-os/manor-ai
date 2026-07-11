#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const floatingSource = await readFile(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const embeddedSource = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const retrySource = await readFile(
  new URL("../src/lib/chatRetry.ts", import.meta.url),
  "utf8",
);

test("floating chat resets explicit chat mode after a successful turn", () => {
  assert.ok(
    /const resetChatModeAfterTurn = useCallback/.test(floatingSource),
    "FloatingChat should define a per-turn chat-mode reset helper",
  );
  assert.ok(
    /await startStream\([\s\S]*?\);\s*if \(requestChatMode\) resetChatModeAfterTurn\(\);/.test(floatingSource),
    "FloatingChat should reset non-auto chat mode after the stream finishes",
  );
});

test("embedded chat resets explicit chat mode after a successful turn", () => {
  assert.ok(
    /const resetChatModeAfterTurn = useCallback/.test(embeddedSource),
    "EmbeddedChat should define a per-turn chat-mode reset helper",
  );
  assert.ok(
    /await startStream\([\s\S]*?\);\s*if \(requestChatMode\) resetChatModeAfterTurn\(\);/.test(embeddedSource),
    "EmbeddedChat should reset non-auto chat mode after the stream finishes",
  );
});

test("successful chat streams clear saved retry payloads so old requests cannot replay", () => {
  assert.ok(
    /export function clearPendingChatRetry\(\)/.test(retrySource),
    "chatRetry should expose a clear helper",
  );
  assert.ok(
    /clearPendingChatRetry\(\);/.test(floatingSource),
    "FloatingChat should clear the saved retry after a completed stream",
  );
  assert.ok(
    /clearPendingChatRetry\(\);/.test(embeddedSource),
    "EmbeddedChat should clear the saved retry after a completed stream",
  );
});
