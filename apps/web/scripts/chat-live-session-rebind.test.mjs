#!/usr/bin/env node
/*
 * A streaming session owns its transcript.
 *
 * Both chat surfaces rebind their visible conversation by clearing the store
 * and refetching from the API. That is right when switching to some other
 * conversation and wrong for the one currently streaming — and the streaming
 * one lands here too: the first `stream_start` frame mints the conversation id,
 * AppLayout navigates to /chat?conversation=<id>, and the prop change arrives
 * mid-stream. Clearing then drops the user's own message plus everything
 * streamed so far, and `loadConversationMessages` refuses to overwrite a live
 * stream, so nothing refills it until the next SSE frame — the chat sits on the
 * empty welcome state for as long as the agent takes to produce its first
 * visible event.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => readFile(path.join(webRoot, relativePath), "utf8");

const [embeddedSource, floatingSource] = await Promise.all([
  read("src/components/EmbeddedChat.tsx"),
  read("src/components/FloatingChat.tsx"),
]);

test("EmbeddedChat does not clear or refetch the conversation it is streaming", () => {
  assert.ok(
    embeddedSource.includes(
      "const isLiveConversation = Boolean(streamingSession?.streaming)",
    ),
  );
  assert.ok(
    embeddedSource.includes("if (cid && !isLiveConversation) setSessionMessages(cid, [])"),
  );
  // The unguarded form is the bug.
  assert.ok(!embeddedSource.includes("if (cid) setSessionMessages(cid, []);"));
});

test("FloatingChat session switching leaves a live stream's transcript alone", () => {
  const fn = floatingSource.slice(
    floatingSource.indexOf("const handleSwitchSession = (convId: string) => {"),
    floatingSource.indexOf("/* ---- HITL action handler ---- */"),
  );
  assert.ok(fn.includes("streamState.getSessionKeyForConversation(convId)"));

  // The assertion that matters is ORDER: the clear has to sit behind the
  // early return, not merely coexist with it. A regex that only proves an
  // `if (isLiveConversation)` block exists passes with the clear moved inside.
  const guardIndex = fn.indexOf("if (isLiveConversation) {");
  const returnIndex = fn.indexOf("return;", guardIndex);
  const clearIndex = fn.indexOf("setSessionMessages(convId, [])");
  assert.ok(guardIndex >= 0, "the live-conversation guard is gone");
  assert.ok(returnIndex > guardIndex, "the guard no longer returns early");
  assert.ok(
    clearIndex > returnIndex,
    "the transcript clear must be unreachable for a live conversation",
  );
  const loadIndex = fn.indexOf("loadRecentMessages(convId)");
  assert.ok(
    loadIndex > returnIndex,
    "the API reload must be unreachable for a live conversation",
  );
});

test("both surfaces resolve the live session through the store, not a local guess", () => {
  for (const source of [embeddedSource, floatingSource]) {
    assert.ok(source.includes("useChatStreamStore.getState()"));
    assert.ok(source.includes("getSessionKeyForConversation"));
  }
});
