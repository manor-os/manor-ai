#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  chatFeedbackSubjectKey,
  createChatFeedbackCoordinator,
} from "../src/lib/chat-feedback-queue.mjs";
import { chatMessageActionText } from "../src/lib/chat-message-action-text.mjs";

const USER_SCOPE = "user-1";

const workspaceChat = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const embeddedChat = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const floatingChat = await readFile(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const messageActions = await readFile(
  new URL("../src/components/chat/ChatMessageActions.tsx", import.meta.url),
  "utf8",
);
const chatCss = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function createCoordinator() {
  return createChatFeedbackCoordinator({ channel: null });
}

function currentValue(coordinator, key = "message-1", scope = USER_SCOPE) {
  return coordinator.snapshot(scope)[key] || null;
}

function linkedChannels() {
  const listeners = [new Set(), new Set()];
  return listeners.map((ownListeners, index) => ({
    addEventListener(_type, listener) {
      ownListeners.add(listener);
    },
    removeEventListener(_type, listener) {
      ownListeners.delete(listener);
    },
    postMessage(data) {
      listeners[1 - index].forEach((listener) => listener({ data }));
    },
    close() {},
  }));
}

async function waitForCallCount(calls, count) {
  for (let attempt = 0; attempt < 20 && calls.length < count; attempt += 1) {
    await Promise.resolve();
  }
  assert.equal(calls.length, count);
}

test("workspace chat reuses the shared copy and rating actions", () => {
  assert.match(
    workspaceChat,
    /import ChatMessageActions,[\s\S]*?from "\.\/chat\/ChatMessageActions"/,
  );
  assert.match(workspaceChat, /api\.chat\.feedback\(/);
  assert.match(
    workspaceChat,
    /<ChatMessageActions[\s\S]*?copyText=\{actionCopyText\}[\s\S]*?onFeedback=/,
  );
  assert.match(
    workspaceChat,
    /const canRateMessage = Boolean\([\s\S]*?messageFeedbackTarget === ChatFeedbackTargetKind\.RESPONSE[\s\S]*?onMessageFeedback/,
  );
  assert.match(
    workspaceChat,
    /onFeedback=\{[\s\S]*?canRateCompletion[\s\S]*?: canRateMessage[\s\S]*?: undefined/,
  );
  assert.match(
    workspaceChat,
    /function completionFeedbackTargetKind[\s\S]*?ChatFeedbackTargetKind\.TASK_COMPLETION[\s\S]*?ChatFeedbackTargetKind\.PLAN_COMPLETION/,
  );
  assert.match(
    workspaceChat,
    /enum ChatFeedbackTargetKind[\s\S]*?RESPONSE = "response"[\s\S]*?NONE = "none"/,
  );
  assert.doesNotMatch(workspaceChat, /const feedbackMutation = useMutation/);
  assert.match(
    workspaceChat,
    /const handleTaskCompletionFeedback = useCallback\([\s\S]*?submitMessageFeedback\([\s\S]*?api\.workspaces\.chat\.feedback/,
  );
  const rowSource = workspaceChat.slice(
    workspaceChat.indexOf("export function WsMessageRow"),
    workspaceChat.indexOf("function WorkflowActivityContent"),
  );
  assert.equal(
    (rowSource.match(/<ChatMessageActions/g) || []).length,
    1,
    "each Workspace message must render one shared copy/rating action row",
  );
  assert.doesNotMatch(rowSource, /task-completion-feedback/);
});

test("persisted and streaming workspace rows expose shared hover and focus actions", () => {
  const sharedShells = workspaceChat.match(/chat-message-row chat-message-shell/g) || [];
  assert.ok(sharedShells.length >= 2, "both workspace message row paths must use chat-message-shell");
  assert.match(workspaceChat, /<ChatTimestamp timestamp=\{msg\.timestamp\} \/>/);
  assert.match(workspaceChat, /<ChatTimestamp timestamp=\{msg\.created_at\} \/>/);
});

test("touch-only devices keep shared message actions visible and interactive", () => {
  assert.match(
    chatCss,
    /@media \(hover: none\) \{[\s\S]*?\.chat-message-actions \{[\s\S]*?opacity: 0\.72;[\s\S]*?pointer-events: auto;/,
  );
});

test("feedback selection is exposed to assistive technology", () => {
  assert.match(messageActions, /aria-pressed=\{feedbackValue === "up"\}/);
  assert.match(messageActions, /aria-pressed=\{feedbackValue === "down"\}/);
});

test("every chat surface uses the shared serialized feedback hook", () => {
  for (const source of [workspaceChat, embeddedChat, floatingChat]) {
    assert.match(source, /useChatMessageFeedback\([^)]+\)/);
    assert.match(source, /hydrateMessageFeedback/);
    assert.match(source, /submitMessageFeedback\(/);
  }
});

test("structured assistant finals provide one shared copy and feedback text", () => {
  const structuredMessage = {
    role: "assistant",
    content: "",
    assistant_blocks: [
      { type: "text", phase: "opening", text: "Working" },
      { type: "text", phase: "final", text: "Final answer" },
    ],
  };

  assert.equal(chatMessageActionText(structuredMessage, ""), "Final answer");
  assert.equal(
    chatMessageActionText(
      { ...structuredMessage, content: "Context\n\nFinal answer" },
      "Context\n\nFinal answer",
    ),
    "Context\n\nFinal answer",
  );
  for (const source of [workspaceChat, embeddedChat, floatingChat]) {
    assert.match(source, /chatMessageActionText\(/);
  }
});

test("multiple structured finals retain visible paragraph boundaries", () => {
  const structuredMessage = {
    role: "assistant",
    content: "",
    assistant_blocks: [
      { type: "text", phase: "final", text: "First paragraph" },
      { type: "text", phase: "final", text: "Second paragraph" },
    ],
  };

  assert.equal(
    chatMessageActionText(structuredMessage, ""),
    "First paragraph\n\nSecond paragraph",
  );
  assert.equal(
    chatMessageActionText(
      {
        ...structuredMessage,
        content: "Context\n\nFirst paragraphSecond paragraph",
      },
      "Context\n\nFirst paragraphSecond paragraph",
    ),
    "Context\n\nFirst paragraphSecond paragraph",
  );
});

test("copy and feedback text exclude hidden assistant protocol", () => {
  const hidden = [
    '<manor-live-edit>{"operation":"replace","content":"SECRET_INTERNAL_PATCH"}</manor-live-edit>',
    "<manor-final-response>Final answer</manor-final-response>",
  ].join("\n");
  const structuredMessage = {
    role: "assistant",
    content: hidden,
    assistant_blocks: [
      { type: "text", phase: "final", text: hidden },
    ],
  };

  assert.equal(chatMessageActionText(structuredMessage, hidden), "Final answer");
  assert.doesNotMatch(
    chatMessageActionText(structuredMessage, hidden),
    /SECRET_INTERNAL_PATCH|manor-live-edit|manor-final-response/,
  );

  const nestedMarker = [
    '<manor-live-edit>{"operation":"replace","content":"<manor-final-response>SECRET_INTERNAL_PATCH"}</manor-live-edit>',
    "Visible answer",
  ].join("\n");
  assert.equal(
    chatMessageActionText(
      {
        role: "assistant",
        content: nestedMarker,
        assistant_blocks: [
          { type: "text", phase: "final", text: nestedMarker },
        ],
      },
      nestedMarker,
    ),
    "Visible answer",
  );
});

test("workspace hydrates ratings for every visible conversation", () => {
  assert.match(workspaceChat, /const feedbackConversationScope = useMemo/);
  assert.match(workspaceChat, /ids\.add\(message\.conversation_id\)/);
  assert.match(
    workspaceChat,
    /conversationIds\.map\(\(id\) => hydrateMessageFeedback\(id\)\)/,
  );
  assert.doesNotMatch(workspaceChat, /request_preview:\s*String\(previousUserMessage/);
});

test("feedback requests for one message are serialized in click order", async () => {
  const calls = [];
  const firstRequest = deferred();
  const secondRequest = deferred();
  const coordinator = createCoordinator();

  const first = coordinator.submit(USER_SCOPE, "message-1", "up", async (rating) => {
    calls.push(rating);
    await firstRequest.promise;
    return { rating, mutation_sequence: 1 };
  });
  const second = coordinator.submit(USER_SCOPE, "message-1", "down", async (rating) => {
    calls.push(rating);
    await secondRequest.promise;
    return { rating, mutation_sequence: 2 };
  });

  await Promise.resolve();
  await Promise.resolve();
  assert.equal(currentValue(coordinator), "down", "the UI should immediately reflect the latest click");
  assert.deepEqual(calls, ["up"], "the second request must wait for the first");

  firstRequest.resolve();
  await first;
  await waitForCallCount(calls, 2);
  assert.deepEqual(calls, ["up", "down"]);

  secondRequest.resolve();
  await second;
  assert.equal(currentValue(coordinator), "down");
});

test("a failed latest request rolls back to the last confirmed rating", async () => {
  const coordinator = createCoordinator();

  await coordinator.submit(USER_SCOPE, "message-1", "up", async (rating) => ({
    rating,
    mutation_sequence: 1,
  }));
  await assert.rejects(
    coordinator.submit(USER_SCOPE, "message-1", "down", async () => {
      throw new Error("network failure");
    }),
    /network failure/,
  );
  assert.equal(currentValue(coordinator), "up");
});

test("an older failure cannot roll back a newer queued rating", async () => {
  const firstRequest = deferred();
  const secondRequest = deferred();
  const coordinator = createCoordinator();

  const first = coordinator
    .submit(USER_SCOPE, "message-1", "up", async () => firstRequest.promise)
    .catch((error) => error);
  const second = coordinator.submit(
    USER_SCOPE,
    "message-1",
    "down",
    async (rating) => {
      await secondRequest.promise;
      return { rating, mutation_sequence: 1 };
    },
  );

  firstRequest.reject(new Error("older failure"));
  assert.match(String(await first), /older failure/);
  assert.equal(currentValue(coordinator), "down");

  secondRequest.resolve();
  await second;
  assert.equal(currentValue(coordinator), "down");
});

test("unmounting one subscriber keeps per-message ordering for the next surface", async () => {
  const firstRequest = deferred();
  const secondRequest = deferred();
  const calls = [];
  const coordinator = createCoordinator();
  const unsubscribe = coordinator.subscribe(USER_SCOPE, () => {});
  const first = coordinator.submit(USER_SCOPE, "message-1", "up", async (rating) => {
    calls.push(rating);
    await firstRequest.promise;
    return { rating, mutation_sequence: 1 };
  });
  await Promise.resolve();
  await Promise.resolve();
  unsubscribe();
  coordinator.subscribe(USER_SCOPE, () => {});
  const second = coordinator.submit(USER_SCOPE, "message-1", "down", async (rating) => {
    calls.push(rating);
    await secondRequest.promise;
    return { rating, mutation_sequence: 2 };
  });

  assert.deepEqual(calls, ["up"]);
  firstRequest.resolve();
  await first;
  await waitForCallCount(calls, 2);
  assert.deepEqual(calls, ["up", "down"]);
  secondRequest.resolve();
  await second;
  assert.equal(currentValue(coordinator), "down");
});

test("server hydration restores ratings without leaking them between users", () => {
  const coordinator = createCoordinator();
  coordinator.hydrate(USER_SCOPE, [
    { message_id: "message-1", rating: "up", mutation_sequence: 4 },
  ]);

  assert.equal(currentValue(coordinator), "up");
  assert.equal(currentValue(coordinator, "message-1", "user-2"), null);
});

test("server hydration ignores rows without a canonical revision", () => {
  const coordinator = createCoordinator();
  coordinator.hydrate(USER_SCOPE, [
    { message_id: "message-1", rating: "up" },
  ]);

  assert.equal(currentValue(coordinator), null);
});

test("completion projections hydrate one canonical subject key", () => {
  const coordinator = createCoordinator();
  coordinator.hydrate(USER_SCOPE, [
    {
      message_id: "thread-receipt",
      rating: "down",
      mutation_sequence: 8,
      target_kind: "task_completion",
      target_id: "plan-1",
    },
  ]);

  assert.equal(
    currentValue(coordinator, "subject:completion:plan-1"),
    "down",
  );
  assert.equal(currentValue(coordinator, "thread-receipt"), "down");
  assert.equal(
    chatFeedbackSubjectKey("plan_completion", "plan-1"),
    "subject:completion:plan-1",
  );
});

test("a persisted rating is synchronized across mounted chat surfaces", async () => {
  const [firstChannel, secondChannel] = linkedChannels();
  const firstRequest = deferred();
  const first = createChatFeedbackCoordinator({ channel: firstChannel });
  const second = createChatFeedbackCoordinator({ channel: secondChannel });

  const older = first.submit(USER_SCOPE, "message-1", "up", async () => {
    await firstRequest.promise;
    return { rating: "up", mutation_sequence: 1 };
  });
  await Promise.resolve();
  await second.submit(USER_SCOPE, "message-1", "down", async (rating) => ({
    rating,
    mutation_sequence: 2,
  }));

  assert.equal(
    currentValue(first),
    "up",
    "an in-flight local click stays optimistic until its revision is known",
  );
  assert.equal(currentValue(second), "down");
  firstRequest.resolve();
  await older;
  assert.equal(currentValue(first), "down");
});

test("a feedback response without a server revision fails closed", async () => {
  const [firstChannel, secondChannel] = linkedChannels();
  const first = createChatFeedbackCoordinator({ channel: firstChannel });
  const second = createChatFeedbackCoordinator({ channel: secondChannel });

  await assert.rejects(
    first.submit(USER_SCOPE, "message-1", "up", async (rating) => ({ rating })),
    /missing its server revision/,
  );

  assert.equal(currentValue(first), null);
  assert.equal(currentValue(second), null);
});
