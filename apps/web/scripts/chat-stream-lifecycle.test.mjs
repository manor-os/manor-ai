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
  export { processSSEStream } from "../src/lib/chatStream.ts";
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

const { processSSEStream } = await import(moduleUrl);

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
    },
    undefined,
  );
  return { messages, result, currentConvId };
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

console.log("chat stream lifecycle checks passed");
