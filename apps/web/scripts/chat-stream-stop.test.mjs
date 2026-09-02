#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const entryPoint = `
  export {
    shouldIgnoreLocallyStoppedStreamUpdate,
    useChatStreamStore,
  } from "../src/stores/chatStream.ts";
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

const { shouldIgnoreLocallyStoppedStreamUpdate, useChatStreamStore } =
  await import(moduleUrl);

useChatStreamStore.getState().reset();

const pausedMessages = [
  { role: "user", content: "Open Chrome and list files" },
  {
    id: "msg_manual_pause",
    role: "assistant",
    content: "",
    meta: { stream_status: "streaming" },
    tool_calls: [
      {
        name: "invoke_skill",
        status: "pending",
        activeChild: "list_files",
      },
    ],
    assistant_blocks: [
      {
        id: "process_1",
        type: "process",
        status: "running",
        steps: [
          {
            id: "step_1",
            name: "list_files",
            status: "running",
          },
        ],
      },
    ],
  },
];

useChatStreamStore.setState({
  streaming: true,
  streamingConvId: "conv_manual_pause",
  messages: pausedMessages,
  latestSessionKey: "conv_manual_pause",
  sessions: {
    conv_manual_pause: {
      key: "conv_manual_pause",
      convId: "conv_manual_pause",
      streaming: true,
      controllerKey: "conv_manual_pause",
      messages: pausedMessages,
    },
  },
  sessionAliases: {},
});

useChatStreamStore.getState().stopStream("conv_manual_pause");

const state = useChatStreamStore.getState();
const stoppedSession = state.sessions.conv_manual_pause;
const stoppedAssistant = stoppedSession.messages.at(-1);

assert.equal(state.streaming, false, "stopping should release the composer streaming state");
assert.equal(stoppedSession.streaming, false, "stopping should finish the active chat session");
assert.equal(stoppedSession.controllerKey, undefined, "stopping should release the stream controller");
assert.equal(
  stoppedAssistant.tool_calls?.[0]?.status,
  "error",
  "stopping should settle a pending wrapper tool instead of leaving it processing",
);
assert.equal(
  stoppedAssistant.tool_calls?.[0]?.activeChild,
  undefined,
  "stopping should clear the wrapper tool's active child",
);
assert.equal(
  stoppedAssistant.assistant_blocks?.[0]?.status,
  "error",
  "stopping should finish the process block instead of leaving it running",
);
assert.equal(
  stoppedAssistant.assistant_blocks?.[0]?.steps?.[0]?.status,
  "error",
  "stopping should finish the active process step",
);
assert.equal(
  stoppedAssistant.meta?.stream_status,
  "interrupted",
  "stopping should clear the persisted active-stream marker immediately",
);
assert.equal(
  stoppedAssistant.meta?.stream_interrupted,
  true,
  "stopping should mark the local transcript as interrupted",
);
assert.equal(
  shouldIgnoreLocallyStoppedStreamUpdate(
    "conv_manual_pause",
    "msg_manual_pause",
    "streaming",
  ),
  true,
  "late streaming snapshots for the manually stopped message should not resurrect tool calls",
);
assert.equal(
  shouldIgnoreLocallyStoppedStreamUpdate(
    "conv_manual_pause",
    "msg_other_turn",
    "streaming",
  ),
  false,
  "a later assistant message in the same conversation must still be followable",
);
assert.equal(
  shouldIgnoreLocallyStoppedStreamUpdate(
    "conv_manual_pause",
    "msg_manual_pause",
    "interrupted",
  ),
  false,
  "the persisted interrupted terminal state should still be allowed through",
);

const [workspaceChatSource, floatingSource, embeddedSource] = await Promise.all([
  readFile(new URL("../src/components/WorkspaceChat.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/components/FloatingChat.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/components/EmbeddedChat.tsx", import.meta.url), "utf8"),
]);

assert.match(
  workspaceChatSource,
  /const handleStopRequest = useCallback/,
  "workspace chat should coordinate its local stop with the persisted chat turn",
);
assert.match(
  workspaceChatSource,
  /api\.chat\.cancelPendingFileApprovals\(convId, pendingHITLIds\(localMsgs\)\)/,
  "workspace chat should request server-side cancellation before abandoning the SSE stream",
);
assert.match(
  workspaceChatSource,
  /onStop=\{handleStopRequest\}/,
  "the workspace composer should use the coordinated stop handler",
);
for (const source of [floatingSource, embeddedSource]) {
  assert.match(
    source,
    /shouldIgnoreLocallyStoppedStreamUpdate\(\s*currentConvId,\s*snapshot\.message_id,\s*snapshot\.status,/,
    "chat surfaces should ignore late running snapshots for a locally stopped turn",
  );
  assert.match(
    source,
    /shouldIgnoreLocallyStoppedStreamUpdate\(currentConvId, messageId\)/,
    "chat surfaces should ignore late message refreshes for a locally stopped turn",
  );
}

console.log("chat stream stop checks passed");
