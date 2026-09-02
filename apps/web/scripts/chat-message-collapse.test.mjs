import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const [shared, workspace, floating, embedded, blocks, css] = await Promise.all([
  read("../src/components/chat/CollapsibleSentMessage.tsx"),
  read("../src/components/WorkspaceChat.tsx"),
  read("../src/components/FloatingChat.tsx"),
  read("../src/components/EmbeddedChat.tsx"),
  read("../src/components/AssistantMessageBlocks.tsx"),
  read("../src/index.css"),
]);

test("only rendered overflow creates a message expansion control", () => {
  assert.match(shared, /body\.scrollHeight > body\.clientHeight \+ 1/);
  assert.match(shared, /new ResizeObserver\(measure\)/);
  assert.match(shared, /observer\.observe\(body\)/);
  assert.match(shared, /observer\.observe\(content\)/);
  assert.match(shared, /observer\.disconnect\(\)/);
  assert.match(shared, /overflowing && !expanded/);
  assert.match(shared, /\(overflowing \|\| expanded\) &&/);
  assert.doesNotMatch(shared + workspace + blocks, /COLLAPSIBLE_(?:FINAL|MESSAGE)_MAX_|LONG_SENT_MESSAGE_|isLongWorkspaceMessage/);
});

test("expansion is reversible, accessible, and resets for a different message", () => {
  assert.match(shared, /setExpanded\(false\);\s*\}, \[text, enabled\]\)/);
  assert.match(shared, /setExpanded\(\(value\) => !value\)/);
  assert.match(shared, /aria-expanded=\{expanded\}/);
  assert.match(shared, /aria-controls=\{bodyId\}/);
  assert.match(shared, /id=\{bodyId\}/);
  assert.match(shared, /expanded \? "Show less" : "Show all"/);
  assert.match(css, /chat-sent-message-collapse__toggle:focus-visible/);
  assert.match(css, /chat-sent-message-collapse--assistant/);
});

test("ordinary Workspace messages share collapse eligibility, while action cards remain visible", () => {
  const body = workspace.match(/function shouldCollapseWorkspaceMessage\([^)]*\) \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(body);
  const eligible = new Function("msg", "isUser", body);
  for (const authorIsUser of [false, true]) {
    assert.equal(eligible({ body: "短消息", message_kind: "text" }, authorIsUser), true);
    assert.equal(eligible({ body: "", message_kind: "text", assistant_blocks: [{}] }, authorIsUser), true);
  }
  assert.equal(eligible({ message_kind: "proposal" }, false), false);
  assert.equal(eligible({ message_kind: "text", pending_action: { kind: "approval" } }, false), false);
});

test("local, persisted, and structured final messages use the shared component without folding streams", () => {
  assert.match(workspace, /streaming=\{isStreamingAssistant\}\s+minimal\s+collapseLongFinal/);
  assert.match(workspace, /<ExpandableWorkspaceMarkdown[\s\S]*?collapsible\s+streaming=\{isStreamingAssistant\}/);
  assert.match(workspace, /<CollapsibleSentMessage text=\{content\} enabled=\{collapsible && !streaming\}/);
  assert.match(blocks, /<CollapsibleSentMessage text=\{content\} enabled=\{enabled && !streaming\}/);
  assert.doesNotMatch(workspace + blocks, /workspace-markdown-expand-button|workspace-markdown-clamp/);
});

test("assistant process status stays truthful across every chat surface", () => {
  for (const source of [workspace, floating, embedded]) {
    assert.match(
      source,
      /pendingActionKind=\{assistantPendingActionKindForMessage\(msg\)\}/,
    );
  }
  assert.match(
    blocks,
    /pendingActionKind === "approval"[\s\S]*?process_waiting_approval[\s\S]*?pendingActionKind === "input"[\s\S]*?process_waiting_input[\s\S]*?hasRunning[\s\S]*?processing[\s\S]*?hasError[\s\S]*?process_error[\s\S]*?processed/,
  );
  assert.doesNotMatch(blocks, /hasError[\s\S]{0,160}process_recovered/);
  assert.match(blocks, /PendingActionKind\.TASK_RECOVERY/);
  assert.match(blocks, /PendingActionKind\.WORKFLOW_RETRY/);
  assert.match(blocks, /hitlType === "error" \|\| hitlType === "failure"/);
  assert.doesNotMatch(blocks, /hasFinalOutput=\{hasFinalOutput\}/);
});
