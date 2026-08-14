#!/usr/bin/env node
/*
 * Following a turn this tab is not streaming.
 *
 * A personal conversation streams over the SSE body of the POST that started
 * it, so reloading mid-reply leaves the page with no connection to a turn that
 * is still running. `chat_stream_snapshot` is the only thing that moves the
 * page after that reload.
 */
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => readFile(path.join(webRoot, relativePath), "utf8");

const bundled = await build({
  stdin: {
    contents: `
      export {
        mergeChatStreamSnapshot,
        isTerminalStreamSnapshot,
        streamSnapshotNeedsHistory,
      } from "../src/lib/chatStream.ts";
    `,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  define: { "import.meta.env": JSON.stringify({ DEV: false }) },
  write: false,
  logLevel: "silent",
});

const {
  mergeChatStreamSnapshot,
  isTerminalStreamSnapshot,
  streamSnapshotNeedsHistory,
} = await import(
  `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString("base64")}`
);

const userRow = { role: "user", content: "build it", timestamp: "t0" };
const runningRow = {
  id: "msg_1",
  role: "assistant",
  content: "The assistant started this response and is still working.",
  timestamp: "t1",
};

const snapshot = (over = {}) => ({
  conversation_id: "conv_1",
  message_id: "msg_1",
  seq: 1,
  status: "streaming",
  content: "Reading the repo",
  tool_calls: [{ name: "bash", arguments: { command: "ls" }, status: "pending" }],
  assistant_blocks: [{ type: "process", status: "running", steps: [] }],
  ...over,
});

test("a snapshot replaces the in-progress row instead of appending a second one", () => {
  const merged = mergeChatStreamSnapshot([userRow, runningRow], snapshot());
  // The history endpoint already returns the running placeholder — appending
  // would show the same reply twice after a reload.
  assert.equal(merged.length, 2);
  assert.equal(merged[0], userRow);
  assert.equal(merged[1].content, "Reading the repo");
  assert.equal(merged[1].id, "msg_1");
  assert.equal(merged[1].role, "assistant");
});

test("tool cards and blocks arrive in the shape the transcript renders", () => {
  const merged = mergeChatStreamSnapshot([userRow, runningRow], snapshot());
  const [tool] = merged[1].tool_calls;
  assert.equal(tool.name, "bash");
  // A running tool must stay pending or it renders as a finished step with no
  // spinner (inferToolStatus treats an absent result as success).
  assert.equal(tool.status, "pending");
  assert.equal(merged[1].assistant_blocks[0].type, "process");
});

test("the in-progress row stays last", () => {
  const merged = mergeChatStreamSnapshot([userRow, runningRow], snapshot());
  // The typing cursor, the activity orb and the streaming block renderer all
  // key off `index === messages.length - 1`.
  assert.equal(merged[merged.length - 1].id, "msg_1");
});

test("an untagged trailing row is adopted — it can only be this turn", () => {
  const merged = mergeChatStreamSnapshot(
    [userRow, { ...runningRow, id: undefined }],
    snapshot(),
  );
  assert.equal(merged.length, 2);
  assert.equal(merged[1].content, "Reading the repo");
  assert.equal(merged[1].id, "msg_1");
});

test("a snapshot for a turn this transcript never saw appends, never overwrites", () => {
  // Reader has tab A open on a finished conversation; the next message is sent
  // from tab B. Its snapshots carry a message id this transcript has never
  // seen. Falling back to "the last assistant row" here destroys the previous
  // answer and drags its attachments and approval card onto the live reply.
  const finished = {
    id: "msg_1",
    role: "assistant",
    content: "FIRST ANSWER",
    attachments: [{ name: "report.pdf" }],
    hitl_requests: [{ id: "h1", type: "approval", resolved: true }],
  };
  const merged = mergeChatStreamSnapshot(
    [userRow, finished],
    snapshot({ message_id: "msg_2", content: "second answer in progress" }),
  );
  assert.equal(merged.length, 3);
  assert.equal(merged[1].content, "FIRST ANSWER");
  assert.deepEqual(merged[1].attachments, [{ name: "report.pdf" }]);
  assert.equal(merged[2].id, "msg_2");
  assert.equal(merged[2].content, "second answer in progress");
  assert.equal(merged[2].attachments, undefined);
  assert.equal(merged[2].hitl_requests, undefined);
  assert.ok(
    streamSnapshotNeedsHistory([userRow, finished], snapshot({ message_id: "msg_2" })),
  );
});

test("a row identified by id is followed even after another row lands behind it", () => {
  // A workflow projection row can be appended while the turn runs. Refusing to
  // update anything but the tail would silently freeze the follower for the
  // rest of that turn while the spinner kept running.
  const projected = { id: "wf_1", role: "assistant", content: "Workflow started" };
  const merged = mergeChatStreamSnapshot([userRow, runningRow, projected], snapshot());
  assert.equal(merged.length, 3);
  assert.equal(merged[1].content, "Reading the repo");
  assert.equal(merged[2], projected);
});

test("a transcript with no assistant row yet gets one", () => {
  const merged = mergeChatStreamSnapshot([userRow], snapshot());
  assert.equal(merged.length, 2);
  assert.equal(merged[1].role, "assistant");
  assert.equal(merged[1].content, "Reading the repo");
});

test("stream_status marks running and clears when the turn ends", () => {
  const running = mergeChatStreamSnapshot([userRow, runningRow], snapshot());
  assert.equal(running[1].meta.stream_status, "streaming");

  const done = mergeChatStreamSnapshot(
    [userRow, runningRow],
    snapshot({ status: "done", seq: 2, content: "Done." }),
  );
  assert.equal(done[1].meta.stream_status, undefined);
  assert.equal(done[1].content, "Done.");

  assert.equal(isTerminalStreamSnapshot(snapshot()), false);
  assert.equal(isTerminalStreamSnapshot(snapshot({ status: "done" })), true);
  assert.equal(isTerminalStreamSnapshot(snapshot({ status: "error" })), true);
});

/* ---- wiring ---- */

const [websocketSource, layoutSource, embeddedSource, floatingSource, storeSource, mobileSource] =
  await Promise.all([
    read("src/lib/websocket.ts"),
    read("src/layouts/AppLayout.tsx"),
    read("src/components/EmbeddedChat.tsx"),
    read("src/components/FloatingChat.tsx"),
    read("src/stores/chatStream.ts"),
    readFile(
      path.join(webRoot, "../mobile/src/lib/useRealtime.ts"),
      "utf8",
    ),
  ]);

test("the socket dispatches the event at all", () => {
  // The switch has no default branch: an event name missing from it is dropped
  // in silence, which looks exactly like a broken backend.
  assert.match(websocketSource, /case "chat_stream_snapshot":/);
  assert.match(websocketSource, /onChatStreamSnapshot\?: \(data: Record<string, any>\) => void/);
});

test("snapshots do not trigger a refetch storm", () => {
  const start = layoutSource.indexOf("const onChatStreamSnapshot");
  assert.ok(start >= 0, "AppLayout no longer handles the snapshot event");
  // Bound the slice by the callback's own terminator rather than by whatever
  // happens to be declared next, so an unrelated addition to AppLayout cannot
  // pull foreign code into this assertion.
  const handler = layoutSource.slice(
    start,
    layoutSource.indexOf("}, []);", start) + "}, []);".length,
  );
  assert.ok(handler.includes('new CustomEvent("manor:chat-stream-snapshot"'));
  // ~1 event/second for the length of a turn, and a refetch of active
  // observers happens immediately regardless of staleTime. Matches the call,
  // not the word, so the comment explaining this does not satisfy the check.
  assert.doesNotMatch(handler, /invalidateQueries\s*\(/);

  // Position-independent: the handler must be passed to useWebSocket, wherever
  // it sits in the options object.
  const options = layoutSource.slice(
    layoutSource.indexOf("useWebSocket({"),
    layoutSource.indexOf("});", layoutSource.indexOf("useWebSocket({")),
  );
  assert.match(options, /\bonChatStreamSnapshot\b/);
});

test("the streaming tab ignores its own snapshots", () => {
  for (const source of [embeddedSource, floatingSource]) {
    // Its SSE reducer appends onto the same row, so an outside write of that
    // row duplicates text on the next token.
    assert.ok(
      source.includes("if (liveKey && streamState.sessions[liveKey]?.streaming) return;"),
      "the live-stream owner must be identified from the store, not a lagging ref",
    );
    assert.ok(source.includes("if (hasLocallyStreamedConversation(currentConvId)) return;"));
    assert.ok(source.includes('snapshot.conversation_id !== currentConvId'));
    assert.ok(source.includes("lastSnapshotSeqRef"));
    // A snapshot has no attachments, approval card or message kind — those only
    // settle when the turn ends, so the terminal edge must refetch.
    assert.ok(
      source.includes(
        "if ((terminal || needsHistory) && snapshotRefetchedRef.current !== refetchKey)",
      ),
    );
  }
  assert.ok(storeSource.includes("export function hasLocallyStreamedConversation"));
  assert.ok(storeSource.includes("_locallyStreamedConversations.clear()"));
});

test("a followed run never masquerades as a locally owned stream", () => {
  // `streaming` gates the stop button, the send guard and every REST reload —
  // none of which are true for a run this tab only watches.
  assert.ok(embeddedSource.includes("const assistantWorking = streaming || remoteRunInFlight;"));
  assert.ok(!embeddedSource.includes("streaming: true"));
  assert.ok(embeddedSource.includes("setFollowedRunActive"));
});

test("background refetches cannot clobber a live stream or a switched view", () => {
  // A refetch started while following can resolve after the user starts their
  // own turn — replacing the transcript then glues the next SSE token onto the
  // previous reply. The guard must run at RESOLUTION time, not dispatch time.
  const floatingGuards = floatingSource.split(
    "isConversationStreamingNow(currentConvId)",
  ).length - 1;
  assert.ok(
    floatingGuards >= 2,
    "both FloatingChat refetch sites must re-check streaming when the fetch resolves",
  );
  // EmbeddedChat routes refetches through its loader; background callers must
  // not let a stale response yank the view back to an old conversation.
  assert.match(embeddedSource, /onlyIfStillCurrent: true/);
  assert.match(embeddedSource, /staleForCurrentView\(\)/);
  // A transient network failure is the request's fault, not the data's: the
  // loader may only clear the conversation on a definitive 404.
  assert.match(embeddedSource, /if \(!isMissing\) return;/);
});

test("a followed run cannot spin forever when a snapshot is lost", () => {
  // The transport has no replay: anything published during a reconnect is gone,
  // including the terminal edge. Both surfaces must fall back to asking the API.
  for (const source of [embeddedSource, floatingSource]) {
    assert.match(source, /FOLLOWED_RUN_SILENCE_MS/);
    assert.match(source, /window\.setTimeout\(/);
    assert.ok(source.includes("setFollowedRunSilenceKey"));
    assert.ok(
      source.includes("setFollowedRunActive(false)"),
      "the watchdog must be able to clear the working indicator",
    );
  }
  // A silence window longer than a minute reads as "broken" to a person
  // watching; shorter than the snapshot cadence would refetch constantly.
  const [, ms] = embeddedSource.match(/FOLLOWED_RUN_SILENCE_MS = ([0-9_]+)/);
  const value = Number(ms.replace(/_/g, ""));
  assert.ok(value >= 5_000 && value <= 120_000, `implausible watchdog: ${value}ms`);
  // A zombie row (API process hard-killed mid-turn) claims "streaming" until
  // the startup sweeper runs — the poll budget keeps that bounded.
  for (const source of [embeddedSource, floatingSource]) {
    assert.match(source, /FOLLOWED_RUN_MAX_POLLS/);
    assert.match(source, /followedRunPollsRef.current >= FOLLOWED_RUN_MAX_POLLS/);
  }
});

test("mobile does not turn a per-second event into a poller", () => {
  // Mentioning the event name is not the invariant — returning before the
  // invalidation cascade is. Take the early-return statement itself.
  const managerStart = mobileSource.indexOf("const mgr = new WsManager");
  const earlyReturn = mobileSource.slice(
    mobileSource.indexOf("if (", managerStart),
    mobileSource.indexOf("return;", managerStart),
  );
  assert.match(earlyReturn, /event === "chat_stream_snapshot"/);
  assert.ok(
    !earlyReturn.includes("invalidateQueries"),
    "the early return must precede every invalidation",
  );
});
