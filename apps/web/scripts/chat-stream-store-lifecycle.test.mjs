#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { build } from "esbuild";

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const bundled = await build({
  stdin: {
    contents: `
      export {
        ChatStreamCompletionStatus,
        useChatStreamStore,
      } from "../src/stores/chatStream.ts";
      export { rollbackResponseSurfaceSubmissionMessages } from "../src/lib/responseSurface.ts";
    `,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  define: {
    "import.meta.env": JSON.stringify({ DEV: false }),
  },
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;
const {
  ChatStreamCompletionStatus,
  rollbackResponseSurfaceSubmissionMessages,
  useChatStreamStore,
} = await import(moduleUrl);

function sseFrame(event, payload, id) {
  const prefix = id ? `id: ${id}\n` : "";
  return `${prefix}event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function responseFromFrames(frames, headers = {}) {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(frames.join("")));
      controller.close();
    },
  });
  return new Response(body, {
    headers: { "content-type": "text/event-stream", ...headers },
  });
}

function heldResponseFromFrames(frames, headers = {}) {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(frames.join("")));
    },
  });
  return new Response(body, {
    headers: { "content-type": "text/event-stream", ...headers },
  });
}

function controlledResponseFromFrames(frames, headers = {}) {
  let streamController;
  const body = new ReadableStream({
    start(controller) {
      streamController = controller;
      controller.enqueue(new TextEncoder().encode(frames.join("")));
    },
  });
  return {
    response: new Response(body, {
      headers: { "content-type": "text/event-stream", ...headers },
    }),
    enqueue(frame) {
      streamController.enqueue(new TextEncoder().encode(frame));
    },
    close() {
      streamController.close();
    },
  };
}

async function waitUntil(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("Timed out waiting for store state");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

useChatStreamStore.getState().reset();
let replayRequests = 0;
globalThis.fetch = async (url) => {
  const value = String(url);
  if (value.endsWith("/api/v1/chat/runs/run_recover_1")) {
    return Response.json({
      id: "run_recover_1",
      status: "completed",
      poll_after_seconds: 1,
    });
  }
  if (value.includes("/api/v1/chat/runs/run_recover_1/events")) {
    replayRequests += 1;
    return responseFromFrames([
      sseFrame("runtime_run", { run_id: "run_recover_1", status: "completed" }),
      sseFrame("text_delta", { content: "Recovered final answer" }, "2-0"),
      sseFrame(
        "stream_end",
        { message_id: "msg_recover_1", persisted: true },
        "3-0",
      ),
      sseFrame("runtime_status", { run_id: "run_recover_1", status: "completed" }),
    ]);
  }
  throw new Error(`Unexpected fetch: ${value}`);
};

let recoveredStatus;
let recoveredDetails;
await useChatStreamStore.getState().startStream(
  async () =>
    responseFromFrames([
      sseFrame("runtime_run", {
        run_id: "run_recover_1",
        status: "queued",
        poll_after_seconds: 1,
      }),
      sseFrame(
        "runtime_waiting",
        {
          run_id: "run_recover_1",
          status: "waiting_resource",
          queue: { ticket: "ticket_recover_1", position: 1, poll_after_seconds: 1 },
        },
        "1-0",
      ),
    ]),
  "conv_recover_1",
  [
    { role: "user", content: "Run a skill" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_recover_1",
  (status, details) => {
    recoveredStatus = status;
    recoveredDetails = details;
  },
);

const recovered = useChatStreamStore.getState().sessions.conv_recover_1;
assert.equal(replayRequests, 1, "terminal status still requires one final event replay");
assert.equal(recovered.messages.at(-1)?.content, "Recovered final answer");
assert.equal(recovered.runtimeLastEventId, "3-0");
assert.equal(recoveredStatus, ChatStreamCompletionStatus.Succeeded);
assert.equal(recoveredDetails.serverAccepted, true);
assert.equal(recoveredDetails.terminalObserved, true);
assert.equal(recoveredDetails.messageId, "msg_recover_1");

useChatStreamStore.getState().reset();
let releaseCancel;
let cancelRequests = 0;
globalThis.fetch = async (url, init) => {
  const value = String(url);
  assert.equal(value, "/api/v1/chat/runs/run_cancel_1/cancel");
  assert.equal(init?.method, "POST");
  cancelRequests += 1;
  return new Promise((resolve) => {
    releaseCancel = () => resolve(Response.json({
      id: "run_cancel_1",
      status: "cancel_requested",
      poll_after_seconds: 1,
    }));
  });
};

let cancelledStatus;
const controlledCancelResponse = controlledResponseFromFrames([
  sseFrame("runtime_run", {
    run_id: "run_cancel_1",
    status: "queued",
    poll_after_seconds: 60,
  }),
]);
const cancelStream = useChatStreamStore.getState().startStream(
  async () => controlledCancelResponse.response,
  "conv_cancel_1",
  [
    { role: "user", content: "Run another skill" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_cancel_1",
  (status) => {
    cancelledStatus = status;
  },
);

await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_1?.runtimeRunId === "run_cancel_1",
);
const acceptedCancel = useChatStreamStore.getState().stopStream("conv_cancel_1");
assert.equal(cancelRequests, 1);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_1.streaming,
  true,
  "Stop must keep the composer blocked until durable cancellation is confirmed",
);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_1.runtimeStatus,
  "cancel_requested",
);
controlledCancelResponse.enqueue(sseFrame("runtime_waiting", {
  run_id: "run_cancel_1",
  status: "waiting_resource",
  queue: { ticket: "ticket_cancel_1", position: 2, poll_after_seconds: 60 },
}, "2-0"));
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_1?.runtimeLastEventId === "2-0",
);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_1.runtimeStatus,
  "cancel_requested",
  "a nonterminal runtime update must not erase an in-flight cancellation request",
);
releaseCancel();
assert.equal(await acceptedCancel, true, "a durable cancel request must release close waiters");
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_1?.runtimePollAfterSeconds === 1,
);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_1.streaming,
  true,
  "an accepted cancellation request must keep observing the run until terminal evidence",
);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_1.runtimeStatus,
  "cancel_requested",
);
assert.equal(
  await useChatStreamStore.getState().stopStream("conv_cancel_1"),
  true,
  "a later close must reuse the already confirmed cancellation",
);
assert.equal(cancelRequests, 1, "confirmed cancellation must remain idempotent");
controlledCancelResponse.enqueue(sseFrame("runtime_cancelled", {
  run_id: "run_cancel_1",
  status: "cancelled",
}, "3-0"));
controlledCancelResponse.close();
await cancelStream;
assert.equal(useChatStreamStore.getState().sessions.conv_cancel_1.streaming, false);
assert.equal(cancelledStatus, ChatStreamCompletionStatus.Cancelled);

useChatStreamStore.getState().reset();
let releaseRacedCancel;
globalThis.fetch = async (url, init) => {
  assert.equal(String(url), "/api/v1/chat/runs/run_cancel_race/cancel");
  assert.equal(init?.method, "POST");
  return new Promise((resolve) => {
    releaseRacedCancel = () => resolve(Response.json({
      id: "run_cancel_race",
      status: "cancelled",
    }));
  });
};
let racedCancelStatus;
let racedCancelDetails;
const racedCancelResponse = controlledResponseFromFrames([
  sseFrame("runtime_run", {
    run_id: "run_cancel_race",
    status: "running",
  }),
]);
const racedCancelStream = useChatStreamStore.getState().startStream(
  async () => racedCancelResponse.response,
  "conv_cancel_race",
  [{ role: "assistant", content: "" }],
  () => {},
  "conv_cancel_race",
  (status, details) => {
    racedCancelStatus = status;
    racedCancelDetails = details;
  },
);
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_race?.runtimeRunId
    === "run_cancel_race",
);
const racedCancel = useChatStreamStore.getState().stopStream("conv_cancel_race");
await waitUntil(() => typeof releaseRacedCancel === "function");
racedCancelResponse.enqueue(sseFrame("runtime_cancelled", {
  run_id: "run_cancel_race",
  status: "cancelled",
}, "2-0"));
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_race?.runtimeStatus === "cancelled",
);
releaseRacedCancel();
assert.equal(await racedCancel, true);
await racedCancelStream;
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_race.streaming,
  false,
  "a terminal event racing ahead of the cancel response must still release the held stream",
);
assert.equal(racedCancelStatus, ChatStreamCompletionStatus.Cancelled);
assert.equal(racedCancelDetails.serverAccepted, true);
assert.equal(racedCancelDetails.terminalObserved, true);

useChatStreamStore.getState().reset();
let cancelFailureRequests = 0;
globalThis.fetch = async (url, init) => {
  const value = String(url);
  assert.equal(value, "/api/v1/chat/runs/run_cancel_failure/cancel");
  assert.equal(init?.method, "POST");
  cancelFailureRequests += 1;
  if (cancelFailureRequests === 1) {
    return new Response("cancel unavailable", { status: 503 });
  }
  return Response.json({ id: "run_cancel_failure", status: "cancelled" });
};

let cancelFailureStatus;
let cancelFailureDetails;
const cancelFailureStream = useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("runtime_run", {
      run_id: "run_cancel_failure",
      status: "queued",
      poll_after_seconds: 60,
    }),
    sseFrame("runtime_waiting", {
      run_id: "run_cancel_failure",
      status: "waiting_resource",
      queue: { ticket: "ticket_cancel_failure", position: 2, poll_after_seconds: 60 },
    }),
  ]),
  "conv_cancel_failure",
  [
    { role: "user", content: "Run another skill" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_cancel_failure",
  (status, details) => {
    cancelFailureStatus = status;
    cancelFailureDetails = details;
  },
);

await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_failure?.runtimeRunId
    === "run_cancel_failure",
);
const rejectedCancel = useChatStreamStore.getState().stopStream("conv_cancel_failure");
assert.equal(await rejectedCancel, false);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_failure.streaming,
  true,
  "a rejected cancellation must keep the active run attached and the composer blocked",
);
const retriedCancel = useChatStreamStore.getState().stopStream("conv_cancel_failure");
assert.equal(await retriedCancel, true);
await cancelFailureStream;
assert.equal(cancelFailureRequests, 2);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_failure.streaming,
  false,
  "a later confirmed cancellation may release the composer",
);
assert.equal(cancelFailureStatus, ChatStreamCompletionStatus.Cancelled);
assert.equal(cancelFailureDetails.serverAccepted, true);
assert.equal(
  cancelFailureDetails.terminalObserved,
  true,
  "a confirmed runtime cancellation is a determinate terminal outcome",
);

useChatStreamStore.getState().reset();
const staleCancelResolvers = [];
globalThis.fetch = async (url, init) => {
  assert.equal(String(url), "/api/v1/chat/runs/run_cancel_replaced/cancel");
  assert.equal(init?.method, "POST");
  return new Promise((resolve) => staleCancelResolvers.push(resolve));
};
const replacedStream = useChatStreamStore.getState().startStream(
  async () => heldResponseFromFrames([
    sseFrame("runtime_run", { run_id: "run_cancel_replaced", status: "running" }),
  ]),
  "conv_cancel_replaced",
  [{ role: "assistant", content: "" }],
  () => {},
  "conv_cancel_replaced",
);
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_replaced?.runtimeRunId
    === "run_cancel_replaced",
);
const staleCancel = useChatStreamStore.getState().stopStream("conv_cancel_replaced");
await waitUntil(() => staleCancelResolvers.length === 1);

const replacementStream = useChatStreamStore.getState().startStream(
  async () => heldResponseFromFrames([
    sseFrame("runtime_run", { run_id: "run_cancel_replaced", status: "running" }),
  ]),
  "conv_cancel_replaced",
  [{ role: "assistant", content: "Replacement run" }],
  () => {},
  "conv_cancel_replaced",
);
assert.equal(await staleCancel, false, "a replacement stream must release the old cancel waiter");
await replacedStream;
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_replaced?.runtimeStatus === "running",
);
staleCancelResolvers[0](Response.json({
  id: "run_cancel_replaced",
  status: "cancelled",
}));
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_replaced.streaming,
  true,
  "a stale cancellation response must not stop a replacement stream with the same run id",
);
const replacementCancel = useChatStreamStore.getState().stopStream("conv_cancel_replaced");
await waitUntil(() => staleCancelResolvers.length === 2);
staleCancelResolvers[1](Response.json({
  id: "run_cancel_replaced",
  status: "cancelled",
}));
assert.equal(await replacementCancel, true);
await replacementStream;
assert.equal(useChatStreamStore.getState().sessions.conv_cancel_replaced.streaming, false);

useChatStreamStore.getState().reset();
const originalSetTimeout = globalThis.setTimeout;
let timedOutCancelSignal;
globalThis.setTimeout = (callback, delay, ...args) => originalSetTimeout(
  callback,
  delay === 15_000 ? 0 : delay,
  ...args,
);
globalThis.fetch = async (_url, init) => new Promise((_resolve, reject) => {
  timedOutCancelSignal = init?.signal;
  init?.signal?.addEventListener(
    "abort",
    () => reject(new DOMException("Aborted", "AbortError")),
    { once: true },
  );
});
const timeoutStream = useChatStreamStore.getState().startStream(
  async () => heldResponseFromFrames([
    sseFrame("runtime_run", { run_id: "run_cancel_timeout", status: "running" }),
  ]),
  "conv_cancel_timeout",
  [{ role: "assistant", content: "" }],
  () => {},
  "conv_cancel_timeout",
);
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_timeout?.runtimeRunId
    === "run_cancel_timeout",
);
const timedOutCancel = useChatStreamStore.getState().stopStream("conv_cancel_timeout");
assert.equal(await timedOutCancel, false);
await waitUntil(
  () => useChatStreamStore.getState().sessions.conv_cancel_timeout?.runtimeStatus === "running",
);
globalThis.setTimeout = originalSetTimeout;
assert.equal(timedOutCancelSignal?.aborted, true);
assert.equal(
  useChatStreamStore.getState().sessions.conv_cancel_timeout.streaming,
  true,
  "a timed-out cancellation must restore retryable streaming state",
);
globalThis.fetch = async () => Response.json({
  id: "run_cancel_timeout",
  status: "cancelled",
});
assert.equal(
  await useChatStreamStore.getState().stopStream("conv_cancel_timeout"),
  true,
);
await timeoutStream;

useChatStreamStore.getState().reset();
let failedStatus;
let failedDetails;
const failedSessionKey = await useChatStreamStore.getState().startStream(
  async () => {
    throw new Error("Response surface submission requires a conversation");
  },
  "conv_failed_1",
  [
    {
      role: "user",
      content: "Run code",
      meta: {
        response_surface_submission: { eventId: "surface-event-1" },
        response_surface_submission_pending: true,
      },
    },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_failed_1",
  (status, details) => {
    failedStatus = status;
    failedDetails = details;
  },
);

assert.equal(failedSessionKey, "conv_failed_1");
assert.equal(
  failedStatus,
  ChatStreamCompletionStatus.Failed,
  "terminal stream failures must be reported to response-surface callers",
);
assert.equal(
  failedDetails.serverAccepted,
  false,
  "transport failures without durable evidence must reuse the submission event",
);
assert.equal(
  useChatStreamStore.getState().sessions.conv_failed_1.messages.at(-1)?.content,
  "Error: Response surface submission requires a conversation",
);
assert.equal(useChatStreamStore.getState().sessions.conv_failed_1.streaming, false);

useChatStreamStore.getState().reset();
let truncatedStatus;
let truncatedDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("text_delta", { content: "Partial answer" }),
  ]),
  "conv_truncated_1",
  [
    { role: "user", content: "Submit an answer" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_truncated_1",
  (status, details) => {
    truncatedStatus = status;
    truncatedDetails = details;
  },
);
assert.equal(
  truncatedStatus,
  ChatStreamCompletionStatus.Failed,
  "a clean transport EOF without stream_end must not report success",
);
assert.equal(
  truncatedDetails.serverAccepted,
  false,
  "a truncated non-runtime stream has no durable acceptance evidence",
);
assert.equal(
  useChatStreamStore.getState().sessions.conv_truncated_1.messages.at(-1)?.content,
  "Partial answer",
  "partial assistant text remains visible for diagnosis without acknowledging the action",
);

useChatStreamStore.getState().reset();
let terminalReplayRequests = 0;
globalThis.fetch = async (url) => {
  const value = String(url);
  assert.match(value, /\/api\/v1\/chat\/runs\/run_terminal_cut\/events/);
  terminalReplayRequests += 1;
  return responseFromFrames([
    sseFrame("text_delta", { content: "Recovered terminal answer" }, "2-0"),
    sseFrame(
      "stream_end",
      { message_id: "msg_terminal_cut", persisted: true },
      "3-0",
    ),
    sseFrame(
      "runtime_status",
      { run_id: "run_terminal_cut", status: "completed" },
      "4-0",
    ),
  ]);
};
let terminalCutStatus;
let terminalCutDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("runtime_run", { run_id: "run_terminal_cut", status: "completed" }),
  ]),
  "conv_terminal_cut",
  [
    { role: "user", content: "Submit a durable answer" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_terminal_cut",
  (status, details) => {
    terminalCutStatus = status;
    terminalCutDetails = details;
  },
);
assert.equal(
  terminalReplayRequests,
  1,
  "a terminal runtime without stream_end must replay its final events",
);
assert.equal(terminalCutStatus, ChatStreamCompletionStatus.Succeeded);
assert.equal(terminalCutDetails.serverAccepted, true);
assert.equal(
  useChatStreamStore.getState().sessions.conv_terminal_cut.messages.at(-1)?.content,
  "Recovered terminal answer",
);

useChatStreamStore.getState().reset();
let incompleteTerminalReplayRequests = 0;
globalThis.fetch = async (url) => {
  const value = String(url);
  assert.match(value, /\/api\/v1\/chat\/runs\/run_terminal_incomplete\/events/);
  incompleteTerminalReplayRequests += 1;
  return responseFromFrames([
    sseFrame("runtime_status", {
      run_id: "run_terminal_incomplete",
      status: "completed",
    }, "2-0"),
  ]);
};
let incompleteTerminalStatus;
let incompleteTerminalDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("runtime_run", {
      run_id: "run_terminal_incomplete",
      status: "completed",
    }),
  ]),
  "conv_terminal_incomplete",
  [
    { role: "user", content: "Submit another durable answer" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_terminal_incomplete",
  (status, details) => {
    incompleteTerminalStatus = status;
    incompleteTerminalDetails = details;
  },
);
assert.equal(incompleteTerminalReplayRequests, 1);
assert.equal(
  incompleteTerminalStatus,
  ChatStreamCompletionStatus.Failed,
  "a completed runtime whose replay still lacks stream_end must fail closed",
);
assert.equal(
  incompleteTerminalDetails.serverAccepted,
  true,
  "the failed completion still reports that the durable run accepted the event",
);
assert.equal(
  incompleteTerminalDetails.terminalObserved,
  false,
  "a completed runtime without stream_end remains an unknown terminal outcome",
);

useChatStreamStore.getState().reset();
let messageIdAcceptedStatus;
let messageIdAcceptedDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("stream_end", { message_id: "msg_accepted_without_flag" }),
  ]),
  "conv_message_id_accepted",
  [
    { role: "user", content: "Submit another answer" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_message_id_accepted",
  (status, details) => {
    messageIdAcceptedStatus = status;
    messageIdAcceptedDetails = details;
  },
);
assert.equal(messageIdAcceptedStatus, ChatStreamCompletionStatus.Succeeded);
assert.equal(
  messageIdAcceptedDetails.serverAccepted,
  true,
  "a persisted assistant message id is durable acceptance evidence even on older stream_end payloads",
);

useChatStreamStore.getState().reset();
let explicitPersistenceFailureStatus;
let explicitPersistenceFailureDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("stream_end", {
      message_id: "msg_explicitly_not_persisted",
      persisted: false,
    }),
  ]),
  "conv_explicitly_not_persisted",
  [
    { role: "user", content: "Submit another answer" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_explicitly_not_persisted",
  (status, details) => {
    explicitPersistenceFailureStatus = status;
    explicitPersistenceFailureDetails = details;
  },
);
assert.equal(
  explicitPersistenceFailureStatus,
  ChatStreamCompletionStatus.Failed,
  "an explicit persistence failure must never complete the turn successfully",
);
assert.equal(
  explicitPersistenceFailureDetails.serverAccepted,
  false,
  "message_id is legacy acceptance evidence only when persisted is absent",
);
assert.equal(explicitPersistenceFailureDetails.terminalObserved, true);

useChatStreamStore.getState().reset();
let acceptedPersistenceFailureStatus;
let acceptedPersistenceFailureDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames(
    [sseFrame("stream_end", {
      message_id: "msg_surface_placeholder",
      persisted: false,
    })],
    { "X-Response-Surface-Event-ID": "surface-event-accepted" },
  ),
  "conv_surface_persistence_failure",
  [
    {
      role: "user",
      content: "Run code",
      meta: {
        response_surface_submission: { eventId: "surface-event-accepted" },
        response_surface_submission_pending: true,
      },
    },
    { role: "assistant", content: "The answer could not be saved" },
  ],
  () => {},
  "conv_surface_persistence_failure",
  (status, details) => {
    acceptedPersistenceFailureStatus = status;
    acceptedPersistenceFailureDetails = details;
  },
);
assert.equal(acceptedPersistenceFailureStatus, ChatStreamCompletionStatus.Failed);
assert.equal(
  acceptedPersistenceFailureDetails.serverAccepted,
  true,
  "a committed response-surface receipt stays accepted when only the assistant reply fails",
);
assert.equal(
  acceptedPersistenceFailureDetails.terminalObserved,
  false,
  "assistant persistence failure must retain the accepted event for idempotent replay",
);

useChatStreamStore.getState().reset();
let acceptedErrorPersistenceFailureStatus;
let acceptedErrorPersistenceFailureDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames(
    [
      sseFrame("error", {
        message: "Sorry, the request failed. Please try again.",
        message_id: null,
        persisted: false,
      }),
      sseFrame("stream_end", {
        message_id: null,
        persisted: false,
        stop_reason: "error",
        error: "Sorry, the request failed. Please try again.",
      }),
    ],
    { "X-Response-Surface-Event-ID": "surface-event-error-not-persisted" },
  ),
  "conv_surface_error_persistence_failure",
  [
    {
      role: "user",
      content: "Run code",
      meta: {
        response_surface_submission: {
          eventId: "surface-event-error-not-persisted",
        },
        response_surface_submission_pending: true,
      },
    },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_surface_error_persistence_failure",
  (status, details) => {
    acceptedErrorPersistenceFailureStatus = status;
    acceptedErrorPersistenceFailureDetails = details;
  },
);
assert.equal(
  acceptedErrorPersistenceFailureStatus,
  ChatStreamCompletionStatus.Failed,
);
assert.equal(acceptedErrorPersistenceFailureDetails.serverAccepted, true);
assert.equal(
  acceptedErrorPersistenceFailureDetails.terminalObserved,
  false,
  "an unpersisted error must preserve the accepted event id for same-event replay",
);

for (const [runtimeStatus, expectedStatus, expectedTerminal] of [
  ["completed", ChatStreamCompletionStatus.Failed, false],
  ["failed", ChatStreamCompletionStatus.Failed, true],
]) {
  useChatStreamStore.getState().reset();
  const suffix = runtimeStatus;
  const runId = `run_cancel_terminal_${suffix}`;
  const convId = `conv_cancel_terminal_${suffix}`;
  globalThis.fetch = async (url, init) => {
    assert.equal(String(url), `/api/v1/chat/runs/${runId}/cancel`);
    assert.equal(init?.method, "POST");
    return Response.json({ id: runId, status: runtimeStatus });
  };
  let completionStatus;
  let completionDetails;
  const stream = useChatStreamStore.getState().startStream(
    async () => heldResponseFromFrames([
      sseFrame("runtime_run", { run_id: runId, status: "running" }),
    ]),
    convId,
    [{ role: "assistant", content: "" }],
    () => {},
    convId,
    (status, details) => {
      completionStatus = status;
      completionDetails = details;
    },
  );
  await waitUntil(
    () => useChatStreamStore.getState().sessions[convId]?.runtimeRunId === runId,
  );
  assert.equal(await useChatStreamStore.getState().stopStream(convId), true);
  await stream;
  assert.equal(useChatStreamStore.getState().sessions[convId].streaming, false);
  assert.equal(completionStatus, expectedStatus);
  assert.equal(completionDetails.serverAccepted, true);
  assert.equal(
    completionDetails.terminalObserved,
    expectedTerminal,
    runtimeStatus === "completed"
      ? "completed without stream_end must replay before the event rotates"
      : "a failed runtime is a determinate terminal outcome",
  );
}

useChatStreamStore.getState().reset();
let creditStopStatusRequests = 0;
let creditStopEventRequests = 0;
globalThis.fetch = async (url) => {
  const value = String(url);
  if (value.endsWith("/api/v1/chat/runs/run_credit_stop")) {
    creditStopStatusRequests += 1;
    return Response.json({
      id: "run_credit_stop",
      status: "completed",
      poll_after_seconds: 1,
    });
  }
  if (value.includes("/api/v1/chat/runs/run_credit_stop/events")) {
    creditStopEventRequests += 1;
    return responseFromFrames([]);
  }
  throw new Error(`Unexpected fetch: ${value}`);
};
let creditStopStatus;
let creditStopDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("runtime_run", {
      run_id: "run_credit_stop",
      status: "running",
      poll_after_seconds: 1,
    }, "1-0"),
    sseFrame("stream_end", {
      message_id: "msg_credit_stop",
      persisted: true,
      stop_reason: "credit_exhausted",
      limit_detail: {
        reason: "credit_exhausted",
        message: "No credits",
      },
    }, "2-0"),
  ]),
  "conv_credit_stop",
  [
    { role: "user", content: "Continue the exercise" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_credit_stop",
  (status, details) => {
    creditStopStatus = status;
    creditStopDetails = details;
  },
);
assert.equal(creditStopStatusRequests, 1);
assert.equal(creditStopEventRequests, 1);
assert.equal(
  creditStopStatus,
  ChatStreamCompletionStatus.Failed,
  "a determinate credit stop must stay failed after durable recovery reports completed",
);
assert.equal(creditStopDetails.serverAccepted, true);
assert.equal(creditStopDetails.terminalObserved, true);
assert.equal(creditStopDetails.messageId, "msg_credit_stop");

useChatStreamStore.getState().reset();
let persistedStopErrorStatus;
let persistedStopErrorDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("stream_end", {
      message_id: "msg_stop_error",
      persisted: true,
      stop_reason: "error",
      error: "sandbox failed",
    }),
  ]),
  "conv_stop_error",
  [
    { role: "user", content: "Run code" },
    { role: "assistant", content: "Sandbox failed" },
  ],
  () => {},
  "conv_stop_error",
  (status, details) => {
    persistedStopErrorStatus = status;
    persistedStopErrorDetails = details;
  },
);
assert.equal(persistedStopErrorStatus, ChatStreamCompletionStatus.Failed);
assert.equal(persistedStopErrorDetails.serverAccepted, true);
assert.equal(persistedStopErrorDetails.terminalObserved, true);

useChatStreamStore.getState().reset();
let durableFailureStatus;
let durableFailureDetails;
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("runtime_run", { run_id: "run_failed_1", status: "failed" }),
    sseFrame("stream_end", { message_id: "msg_failed_1", persisted: true }),
  ]),
  "conv_failed_2",
  [
    { role: "user", content: "Run a durable action" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_failed_2",
  (status, details) => {
    durableFailureStatus = status;
    durableFailureDetails = details;
  },
);
assert.equal(
  durableFailureDetails.serverAccepted,
  true,
  "persisted terminal failures must rotate the event on retry",
);
assert.equal(durableFailureDetails.terminalObserved, true);
assert.equal(
  durableFailureStatus,
  ChatStreamCompletionStatus.Failed,
  "a terminal failed Runtime run must not be acknowledged as a successful submission",
);

useChatStreamStore.getState().reset();
let failedWithoutEndStatus;
let failedWithoutEndDetails;
globalThis.fetch = async (url) => {
  throw new Error(`A terminal failed run must not poll again: ${String(url)}`);
};
await useChatStreamStore.getState().startStream(
  async () => responseFromFrames([
    sseFrame("runtime_run", { run_id: "run_failed_without_end", status: "failed" }),
  ]),
  "conv_failed_without_end",
  [
    { role: "user", content: "Run a durable action" },
    { role: "assistant", content: "" },
  ],
  () => {},
  "conv_failed_without_end",
  (status, details) => {
    failedWithoutEndStatus = status;
    failedWithoutEndDetails = details;
  },
);
assert.equal(failedWithoutEndStatus, ChatStreamCompletionStatus.Failed);
assert.equal(failedWithoutEndDetails.serverAccepted, true);
assert.equal(
  failedWithoutEndDetails.terminalObserved,
  true,
  "a terminal failed runtime_status is determinate even when stream_end is missing",
);

useChatStreamStore.getState().reset();
const conversationIds = [];
const assignedSessionKey = await useChatStreamStore.getState().startStream(
  async () => responseFromFrames(
    [
      sseFrame("stream_start", { conversation_id: "conv_ai_edit_header" }),
      sseFrame("text_delta", { content: "streamed" }),
      sseFrame("stream_end", { message_id: "msg_ai_edit_header", persisted: true }),
    ],
    { "X-Conversation-ID": "conv_ai_edit_header" },
  ),
  undefined,
  [
    { role: "user", content: "Edit this file" },
    { role: "assistant", content: "" },
  ],
  (conversationId) => conversationIds.push(conversationId),
  "draft:ai-edit-header",
);
assert.equal(assignedSessionKey, "conv_ai_edit_header");
assert.deepEqual(
  conversationIds,
  ["conv_ai_edit_header"],
  "the response header and first SSE frame must publish one stable conversation identity",
);
assert.equal(useChatStreamStore.getState().sessions["draft:ai-edit-header"], undefined);
assert.equal(
  useChatStreamStore.getState().sessions.conv_ai_edit_header.messages.at(-1)?.content,
  "streamed",
);

const rolledBackMessages = rollbackResponseSurfaceSubmissionMessages(
  [
    { role: "assistant", content: "Earlier answer" },
    {
      role: "user",
      content: "Run code",
      meta: {
        response_surface_submission: { eventId: "surface-event-1" },
        response_surface_submission_pending: true,
      },
    },
    {
      role: "assistant",
      content: "Error: Response surface submission requires a conversation",
    },
  ],
  "surface-event-1",
);
assert.deepEqual(
  rolledBackMessages,
  [{ role: "assistant", content: "Earlier answer" }],
  "a failed surface action must remove only its receipt and error placeholder",
);

const rolledBackPersistedStreamError = rollbackResponseSurfaceSubmissionMessages(
  [
    { role: "assistant", content: "Earlier answer" },
    {
      role: "user",
      content: "Run code",
      meta: {
        response_surface_submission: { eventId: "surface-event-2" },
        response_surface_submission_pending: true,
      },
    },
    {
      role: "assistant",
      content: "Sorry, the request failed. Please try again.\n\nError detail: sandbox failed",
      stream_error: true,
      meta: { stream_status: "error" },
    },
  ],
  "surface-event-2",
);
assert.deepEqual(
  rolledBackPersistedStreamError,
  [{ role: "assistant", content: "Earlier answer" }],
  "a failed surface action must remove the persisted stream error bubble",
);

const durableReceiptMessages = [
  { role: "assistant", content: "Earlier answer" },
  {
    id: "persisted-user-message",
    role: "user",
    content: "Run code",
    meta: {
      response_surface_submission: { eventId: "surface-event-durable" },
    },
  },
  {
    role: "assistant",
    content: "Error: transport disconnected",
    stream_error: true,
  },
];
assert.deepEqual(
  rollbackResponseSurfaceSubmissionMessages(
    durableReceiptMessages,
    "surface-event-durable",
  ),
  durableReceiptMessages,
  "an ambiguous transport failure must never delete a durable HTML activity receipt",
);

useChatStreamStore.getState().reset();
console.log("chat stream store lifecycle checks passed");
