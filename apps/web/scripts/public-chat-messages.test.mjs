import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: {
    contents: 'export * from "../src/lib/publicChatMessages.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
  logLevel: "silent",
});
const { mergePublicChatMessages, publicChatMessageNeedsRefresh } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`
);
const reply = (content, extra = {}) => ({ id: "assistant", role: "assistant", content, created_at: null, ...extra });
const placeholder = "The assistant started this response and is still working. If this remains after a reload, the stream was interrupted before it could finish.";

test("poll repairs a partial SSE reply using the existing message identity", () => {
  const local = [reply("Partial", { local_status: "pending" })];
  const completed = reply("Complete answer", { stream_status: "completed" });
  assert.deepEqual(mergePublicChatMessages(local, [completed]), [completed]);
});

test("poll repairs a saved placeholder after reload", () => {
  const saved = reply(placeholder, { stream_status: "running" });
  assert.equal(publicChatMessageNeedsRefresh(saved), true);
  const completed = reply("Complete answer", { stream_status: "completed" });
  const result = mergePublicChatMessages([saved], [completed]);
  assert.deepEqual(result, [completed]);
  assert.equal(publicChatMessageNeedsRefresh(result[0]), false);
});

test("a delayed running checkpoint does not truncate already streamed text", () => {
  const local = [reply("Partial streamed answer")];
  assert.equal(mergePublicChatMessages(local, [reply("Partial", { stream_status: "streaming" })]), local);
  assert.equal(mergePublicChatMessages(local, [reply(placeholder, { stream_status: "running" })]), local);
  const final = reply("Short corrected answer", { stream_status: "completed" });
  assert.deepEqual(mergePublicChatMessages(local, [final]), [final]);
});

for (const [content, stream_status] of [[placeholder, "running"], ["Send", "streaming"]]) {
  test(`a ${stream_status} reply replaces its own transport failure after its SSE ID arrived`, () => {
    const failed = reply("Send failed", { local_status: "notice", client_turn_id: "turn" });
    const otherFailure = reply("Other send failed", { id: "tmp-other", local_status: "notice" });
    const running = reply(content, { stream_status, client_turn_id: "turn" });
    assert.deepEqual(mergePublicChatMessages([failed, otherFailure], [running]), [running, otherFailure]);
  });
}

test("replayed final messages do not duplicate history or trigger activity", () => {
  const local = [reply("Complete answer", { stream_status: "completed" })];
  assert.equal(mergePublicChatMessages(local, [{ ...local[0] }]), local);
});

test("server persistence replaces optimistic user messages without duplication", () => {
  const local = [{ id: "tmp-user-1", role: "user", content: "Hello", created_at: null }];
  const saved = { ...local[0], id: "user-1", created_at: "2026-08-31T00:00:00Z" };
  assert.deepEqual(mergePublicChatMessages(local, [saved]), [saved]);
});

test("distinct persisted messages with identical text remain distinct", () => {
  const first = reply("Thank you");
  const second = reply("Thank you", { id: "assistant-2" });
  assert.deepEqual(mergePublicChatMessages([first], [second]), [first, second]);
});

test("legacy placeholders refresh, terminal failures stop refreshing", () => {
  assert.equal(publicChatMessageNeedsRefresh(reply(placeholder)), true);
  assert.equal(publicChatMessageNeedsRefresh(reply("Partial", { stream_status: "streaming" })), true);
  assert.equal(publicChatMessageNeedsRefresh(reply("Failed", { stream_status: "error" })), false);
  assert.equal(publicChatMessageNeedsRefresh(reply("Plain non-streamed reply")), false);
});

test("refreshing an older reply preserves a later send failure", () => {
  const older = reply("Earlier partial answer", { stream_status: "streaming" });
  const failedUser = { id: "tmp-user-new", role: "user", content: "New question", created_at: null };
  const failure = reply("Send failed", { id: "tmp-assistant-new", local_status: "notice" });
  const completed = { ...older, content: "Earlier completed answer", stream_status: "completed" };
  assert.deepEqual(mergePublicChatMessages([older, failedUser, failure], [completed]), [completed, failedUser, failure]);
});

test("unrelated replies with matching text cannot consume a temporary failure", () => {
  const failure = reply("Please try again", { id: "tmp-assistant-new", local_status: "notice" });
  const unrelated = reply("Please try again", { id: "another-reply", stream_status: "completed" });
  assert.deepEqual(mergePublicChatMessages([failure], [unrelated]), [failure, unrelated]);
});

test("a recovered reply clears only its own failure state", () => {
  const failed = reply("Send failed", { local_status: "notice" });
  const otherFailure = reply("Send failed", { id: "tmp-assistant-other", local_status: "notice" });
  const recovered = reply("Recovered answer", { stream_status: "completed" });
  assert.deepEqual(mergePublicChatMessages([failed, otherFailure], [recovered]), [recovered, otherFailure]);
});

for (const localStatus of ["pending", "notice"]) {
  test(`poll repairs a ${localStatus} reply when no SSE message ID arrived`, () => {
    const user = { id: "tmp-user-turn", role: "user", content: "Question", created_at: null, client_turn_id: "turn" };
    const temporary = reply(localStatus === "notice" ? "Send failed" : "", {
      id: "tmp-assistant-turn", client_turn_id: "turn", local_status: localStatus,
    });
    const savedUser = { ...user, id: "saved-user" };
    const savedReply = reply("Recovered answer", { client_turn_id: "turn", stream_status: "completed" });
    assert.deepEqual(mergePublicChatMessages([user, temporary], [savedUser, savedReply]), [savedUser, savedReply]);
  });
}

test("a running reply gains its real ID and can later finish after SSE loss", () => {
  const temporary = reply("", { id: "tmp-assistant-turn", client_turn_id: "turn", local_status: "pending" });
  const running = reply(placeholder, { client_turn_id: "turn", stream_status: "running" });
  const intermediate = mergePublicChatMessages([temporary], [running]);
  assert.deepEqual(intermediate, [running]);
  assert.equal(publicChatMessageNeedsRefresh(intermediate[0]), true);
  const completed = { ...running, content: "Recovered answer", stream_status: "completed" };
  assert.deepEqual(mergePublicChatMessages(intermediate, [completed]), [completed]);
});

test("identical questions and failures in different turns stay separate", () => {
  const failedUser = { id: "tmp-user-failed", role: "user", content: "Question", created_at: null, client_turn_id: "failed-turn" };
  const failure = reply("Send failed", { id: "tmp-assistant-failed", client_turn_id: "failed-turn", local_status: "notice" });
  const retryUser = { ...failedUser, id: "tmp-user-retry", client_turn_id: "retry-turn" };
  const retryReply = reply("", { id: "tmp-assistant-retry", client_turn_id: "retry-turn", local_status: "pending" });
  const savedUser = { ...retryUser, id: "saved-user" };
  const savedReply = reply("Reply", { client_turn_id: "retry-turn", stream_status: "completed" });
  assert.deepEqual(
    mergePublicChatMessages([failedUser, failure, retryUser, retryReply], [savedUser, savedReply]),
    [failedUser, failure, savedUser, savedReply],
  );
});
