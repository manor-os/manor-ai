#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { build } from "esbuild";

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const entryPoint = `
  export {
    formatPersistedStreamErrorMessage,
    formatPublicToolResult,
    formatRuntimeQueueStatus,
    parseToolCalls,
    processSSEStream,
  } from "../src/lib/chatStream.ts";
`;

const bundled = await build({
  stdin: {
    contents: entryPoint,
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
  formatPersistedStreamErrorMessage,
  formatPublicToolResult,
  formatRuntimeQueueStatus,
  parseToolCalls,
  processSSEStream,
} = await import(moduleUrl);

const internalToolFailure =
  "Tool key mcp__private_server__private_action is not registered";
assert.equal(
  formatPublicToolResult(internalToolFailure),
  "Sorry, the request failed. Please try again.",
);
assert.equal(
  formatPersistedStreamErrorMessage(internalToolFailure),
  "Sorry, the request failed. Please try again.",
);
assert.equal(
  formatPersistedStreamErrorMessage(
    "Tool error (mcp__calendar__create_event): Permission denied. Reconnect your Google account.",
  ),
  "Sorry, the request failed. Please try again.\n\nError detail: Permission denied. Reconnect your Google account.",
);
assert.equal(
  formatPersistedStreamErrorMessage("consecutive_tool_errors"),
  "Sorry, the request failed. Please try again.",
);
const redactedPersistedError = formatPersistedStreamErrorMessage(
  "Provider rejected Authorization: Bearer sk-live-1234567890abcdef",
);
assert.equal(redactedPersistedError.includes("sk-live-1234567890abcdef"), false);
assert.equal(redactedPersistedError, "Sorry, the request failed. Please try again.");
for (const privateError of [
  "[Errno 2] No such file or directory: '/var/lib/manor/private/customer.csv'",
  "Connection refused to redis://internal-cache:6379/0",
  "Validation failed for tenant ent_01SECRET on /srv/manor/private.json",
]) {
  assert.equal(
    formatPersistedStreamErrorMessage(privateError),
    "Sorry, the request failed. Please try again.",
  );
}
const projectedToolCalls = parseToolCalls([{
  name: "mcp__private_server__private_action",
  result: internalToolFailure,
  raw_result: internalToolFailure,
  status: "error",
}]);
assert.equal(
  projectedToolCalls[0].result,
  "Sorry, the request failed. Please try again.",
);
assert.equal(projectedToolCalls[0].rawResult, undefined);
assert.equal(
  formatPublicToolResult("Tool error (private.execute): KeyError('handler')"),
  "Sorry, the request failed. Please try again.",
);
assert.equal(
  formatPublicToolResult(
    "Tool error (private.execute): RuntimeError('Authorization: Bearer sk-secret')",
  ),
  "Sorry, the request failed. Please try again.",
);
assert.equal(
  formatPublicToolResult(
    "Tool error (private.execute): ValueError('/srv/private/config.yaml')",
  ),
  "Sorry, the request failed. Please try again.",
);
assert.equal(
  formatPublicToolResult(
    "Tool error (mcp__calendar__create_event): Permission denied. Reconnect your Google account.",
  ),
  "Permission denied. Reconnect your Google account.",
);
assert.equal(
  formatPublicToolResult(
    "Tool error (mcp__calendar__list_events): Rate limit exceeded. Try again later.",
  ),
  "Rate limit exceeded. Try again later.",
);
assert.equal(
  formatPublicToolResult(
    "Tool error (mcp__media__generate): Quota exceeded. Upgrade your plan or try again later.",
  ),
  "Quota exceeded. Upgrade your plan or try again later.",
);
assert.equal(
  formatPublicToolResult(
    "Tool error (mcp__media__generate): Credits exhausted. Upgrade your plan.",
  ),
  "Credits exhausted. Upgrade your plan.",
);
assert.equal(
  formatPublicToolResult("Your Python code raises TypeError when the input is None."),
  "Your Python code raises TypeError when the input is None.",
);
assert.equal(
  formatPublicToolResult("Use SQLAlchemy's select() API for this query."),
  "Use SQLAlchemy's select() API for this query.",
);
assert.equal(
  formatPublicToolResult("No matching handler for mcp__private_server__private_action"),
  "Sorry, the request failed. Please try again.",
);
const redactedToolResult = formatPublicToolResult(
  "Retry at https://example.invalid/run?api_key=sk-live-1234567890abcdef",
);
assert.equal(redactedToolResult.includes("sk-live-1234567890abcdef"), false);
assert.equal(redactedToolResult.includes("api_key=<redacted>"), true);
assert.equal(
  formatPublicToolResult(
    "This operation is temporarily unavailable. Please try again.",
  ),
  "Sorry, the request failed. Please try again.",
);
const publicDraftCalls = parseToolCalls([{
  name: "continue_workspace_draft",
  result: '{"artifact_kind":"workspace_draft","draft_id":"draft-1","status":"ready"}',
  raw_result: JSON.stringify({
    artifact_kind: "workspace_draft",
    draft_id: "draft-1",
    status: "ready",
    ready: true,
    fields: { name: "Public draft" },
    private_debug: { tool_key: "mcp__private__draft" },
  }),
  status: "success",
}]);
assert.deepEqual(JSON.parse(publicDraftCalls[0].rawResult), {
  artifact_kind: "workspace_draft",
  draft_id: "draft-1",
  status: "ready",
  ready: true,
  fields: { name: "Public draft" },
});
assert.equal(parseToolCalls([{
  name: "continue_workspace_draft",
  result: '{"artifact_kind":"workspace_draft","status":"ready"}',
  raw_result: '{"artifact_kind":"workspace_draft","private_debug":true}',
  status: "success",
}])[0].rawResult, undefined);

assert.equal(
  formatRuntimeQueueStatus({
    ticket: "queue_ticket_ui",
    position: 3,
    etaSeconds: 75,
    pollAfterSeconds: 5,
  }),
  "Waiting for execution capacity · queue position 3 · about 2 min",
);

function sseFrame(event, payload) {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function responseFromFrames(frames) {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(frames.join("")));
      controller.close();
    },
  });
  return new Response(body);
}

async function runStream(frames) {
  let messages = [
    { role: "user", content: "List documents" },
    { role: "assistant", content: "" },
  ];
  let currentConvId;
  const runtimeStates = [];
  const result = await processSSEStream(
    responseFromFrames(frames),
    {
      setMessages(updater) {
        messages = typeof updater === "function" ? updater(messages) : updater;
      },
      setCurrentConvId(updater) {
        currentConvId =
          typeof updater === "function" ? updater(currentConvId) : updater;
      },
      onRuntimeState(state) {
        runtimeStates.push(state);
      },
    },
    undefined,
  );
  return { messages, result, currentConvId, runtimeStates };
}

const lifecycleFrames = [
  sseFrame("stream_start", {
    conversation_id: "conv_1",
    message_id: "msg_assistant_1",
  }),
  sseFrame("text_delta", { content: "I will inspect the workspace first. " }),
  sseFrame("tool_start", {
    tool_call: { name: "workspace_list_knowledge", status: "pending" },
    assistant_blocks: [
      {
        id: "blk_process_1",
        type: "process",
        status: "running",
        steps: [
          {
            id: "step_1",
            seq: 1,
            kind: "tool",
            name: "workspace_list_knowledge",
            status: "running",
          },
        ],
      },
    ],
  }),
  sseFrame("tool_end", {
    tool_call: {
      name: "workspace_list_knowledge",
      status: "success",
      result: '{"count":3}',
    },
    assistant_blocks: [
      {
        id: "blk_process_1",
        type: "process",
        status: "completed",
        steps: [
          {
            id: "step_1",
            seq: 1,
            kind: "tool",
            name: "workspace_list_knowledge",
            status: "success",
          },
        ],
      },
    ],
  }),
  sseFrame("summary_start", {}),
  sseFrame("text_delta", { content: "Found 3 workspace documents." }),
  sseFrame("stream_end", {
    conversation_id: "conv_1",
    message_id: "msg_assistant_1",
    persisted: true,
    assistant_blocks: [
      {
        id: "blk_process_1",
        type: "process",
        status: "error",
        default_collapsed: false,
        steps: [
          {
            id: "step_1",
            seq: 1,
            kind: "tool",
            name: "workspace_list_knowledge",
            status: "error",
          },
        ],
      },
    ],
  }),
];

const { messages, result, currentConvId } = await runStream(lifecycleFrames);
const assistant = messages.at(-1);

assert.equal(currentConvId, "conv_1", "stream_start should set conversation id");
assert.equal(result.messageId, "msg_assistant_1", "stream result should expose message id");
assert.equal(
  assistant.content,
  "Found 3 workspace documents.",
  "summary_start should reset visible assistant content before final text",
);
assert.equal(
  assistant.id,
  "msg_assistant_1",
  "stream_end message_id should tag the local assistant message for persisted dedupe",
);
assert.equal(
  assistant.assistant_blocks?.[0]?.default_collapsed,
  true,
  "assistant blocks should stay collapsed after summary_start even if a later error payload is expanded",
);

const crossConversationFrames = [
  sseFrame("stream_start", {
    conversation_id: "conv_current",
    message_id: "msg_current",
  }),
  sseFrame("text_delta", {
    conversation_id: "conv_current",
    message_id: "msg_current",
    content: "Current reply. ",
  }),
  sseFrame("tool_start", {
    conversation_id: "conv_old",
    message_id: "msg_old",
    tool_call: { name: "web_search", status: "pending", arguments: { q: "old job" } },
    assistant_blocks: [
      {
        id: "blk_old",
        type: "process",
        status: "running",
        steps: [
          {
            id: "old_step_1",
            seq: 1,
            kind: "tool",
            name: "web_search",
            status: "running",
            arguments_preview: '{"q":"old job"}',
          },
        ],
      },
    ],
  }),
  sseFrame("text_delta", {
    conversation_id: "conv_old",
    message_id: "msg_old",
    content: "This belongs to an older chat.",
  }),
  sseFrame("stream_end", {
    conversation_id: "conv_current",
    message_id: "msg_current",
    persisted: true,
  }),
];

const { messages: guardedMessages } = await runStream(crossConversationFrames);
const guardedAssistant = guardedMessages.at(-1);

assert.equal(
  guardedAssistant.content,
  "Current reply. ",
  "events tagged with another conversation/message should not append text to this turn",
);
assert.equal(
  guardedAssistant.assistant_blocks,
  undefined,
  "events tagged with another conversation/message should not replace this turn's process blocks",
);
assert.deepEqual(
  guardedAssistant.tool_calls || [],
  [],
  "events tagged with another conversation/message should not add tool calls to this turn",
);

const runtimeFrames = [
  sseFrame("runtime_run", {
    run_id: "run_durable_1",
    conversation_id: "conv_durable_1",
    message_id: "msg_durable_1",
    status: "queued",
    poll_after_seconds: 5,
  }),
  "id: 42-0\n" + sseFrame("runtime_waiting", {
    run_id: "run_durable_1",
    conversation_id: "conv_durable_1",
    message_id: "msg_durable_1",
    status: "waiting_resource",
    queue: {
      ticket: "queue_ticket_1",
      position: 3,
      eta_seconds: 40,
      poll_after_seconds: 5,
      deadline_at: "2026-08-01T12:05:00Z",
    },
  }),
  "id: 43-0\n" + sseFrame("runtime_status", {
    run_id: "run_durable_1",
    status: "running",
  }),
];

const runtimeResult = await runStream(runtimeFrames);
assert.equal(runtimeResult.result.runtimeRunId, "run_durable_1");
assert.equal(runtimeResult.result.lastEventId, "43-0");
assert.equal(runtimeResult.runtimeStates[1].status, "waiting_resource");
assert.equal(runtimeResult.runtimeStates[1].queue.position, 3);
assert.equal(runtimeResult.runtimeStates[1].pollAfterSeconds, 5);

const replayCursorResult = await runStream([
  sseFrame("runtime_run", {
    run_id: "run_replay_1",
    status: "running",
  }),
  "id: 51-0\n" + sseFrame("text_delta", { content: "Recovered" }),
  "id: 52-0\n" + sseFrame("stream_end", {
    message_id: "msg_replay_1",
    persisted: true,
  }),
]);
assert.equal(
  replayCursorResult.result.lastEventId,
  "52-0",
  "the replay cursor must advance for content events, not only runtime status events",
);

const explicitPersistenceFailure = await runStream([
  sseFrame("tool_start", {
    assistant_blocks: [{
      id: "surface-before-error",
      type: "surface",
      surface: { kind: "generated_html", html: "<button>Submit</button>" },
    }],
  }),
  sseFrame("error", {
    message: "Sorry, the request failed. Please try again.",
    message_id: "msg_not_persisted",
    persisted: false,
  }),
]);
assert.equal(
  explicitPersistenceFailure.result.error?.persisted,
  false,
  "an error message id must not override explicit persistence failure",
);
assert.equal(
  explicitPersistenceFailure.messages.at(-1)?.assistant_blocks,
  undefined,
  "a failed turn must remove an interactive response surface",
);
assert.equal(
  explicitPersistenceFailure.messages.at(-1)?.content,
  "Sorry, the request failed. Please try again.",
  "a server-sanitized error must not be wrapped a second time",
);

const legacyInternalFailure = await runStream([
  sseFrame("error", {
    message: "Internal error: (sqlalchemy.dialects.postgresql.asyncpg.Error) [SQL: SELECT secret]",
    message_id: "msg_legacy_internal_error",
    persisted: true,
  }),
]);
assert.equal(
  legacyInternalFailure.messages.at(-1)?.content,
  "Sorry, the request failed. Please try again.",
  "legacy runtime events must not expose internal database details",
);
assert.doesNotMatch(
  legacyInternalFailure.messages.at(-1)?.content || "",
  /sqlalchemy|SELECT secret/i,
);

const explicitStreamEndPersistenceFailure = await runStream([
  sseFrame("stream_end", {
    message_id: "msg_end_not_persisted",
    persisted: false,
    error: "Assistant message could not be saved",
  }),
]);
assert.equal(explicitStreamEndPersistenceFailure.result.persisted, false);
assert.equal(
  explicitStreamEndPersistenceFailure.result.error?.persisted,
  false,
  "a stream_end message id must not override explicit persistence failure",
);

const redactedFailedTool = await runStream([
  sseFrame("tool_start", {
    tool_call: {
      name: "mcp__private_server__private_action",
      status: "pending",
    },
  }),
  sseFrame("tool_end", {
    tool_call: {
      name: "operation",
      status: "error",
      result: "This operation is temporarily unavailable. Please try again.",
    },
  }),
]);
assert.deepEqual(
  redactedFailedTool.messages.at(-1)?.tool_calls?.map(({ name, status }) => ({
    name,
    status,
  })),
  [{ name: "operation", status: "error" }],
  "a redacted failure must settle and replace its pending private tool card",
);

const concurrentRedactedFailure = await runStream([
  sseFrame("tool_start", {
    tool_call: {
      name: "mcp__private__first",
      arguments: { request_id: "first" },
      status: "pending",
    },
  }),
  sseFrame("tool_start", {
    tool_call: {
      name: "mcp__private__second",
      arguments: { request_id: "second" },
      status: "pending",
    },
  }),
  sseFrame("tool_end", {
    tool_call: {
      name: "operation",
      arguments: { request_id: "first" },
      status: "error",
      result: "This operation is temporarily unavailable. Please try again.",
    },
  }),
]);
const concurrentCalls = concurrentRedactedFailure.messages.at(-1)?.tool_calls || [];
assert.deepEqual(
  concurrentCalls.map(({ name, status, arguments: serializedArguments }) => ({
    name,
    status,
    arguments: JSON.parse(serializedArguments),
  })),
  [
    {
      name: "operation",
      status: "error",
      arguments: { request_id: "first" },
    },
    {
      name: "mcp__private__second",
      status: "pending",
      arguments: { request_id: "second" },
    },
  ],
  "a redacted failure must use its argument fingerprint when tools overlap",
);

console.log("chat stream lifecycle checks passed");
