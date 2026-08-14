#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const messageBubble = readFileSync(
  new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
  "utf8",
);
const floatingPanel = readFileSync(
  new URL("../src/components/FloatingPanel.tsx", import.meta.url),
  "utf8",
);
const messageRow = readFileSync(
  new URL("../src/components/chat/MessageRow.tsx", import.meta.url),
  "utf8",
);

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`));
  assert.ok(match, `${selector} rule should exist`);
  return match[1];
}

function assertDeclarations(selector, declarations) {
  const body = ruleBody(selector);
  for (const declaration of declarations) {
    assert.match(body, new RegExp(declaration), `${selector} should include ${declaration}`);
  }
}

test("chat rows and bubbles can shrink inside narrow floating panels", () => {
  assert.match(floatingPanel, /width: "calc\(100% - 48px\)"/);
  assert.match(floatingPanel, /maxWidth: width/);
  assert.match(messageRow, /alignItems: "flex-start"/);
  assertDeclarations(".chat-message-row", ["min-width:\\s*0"]);
  assertDeclarations(".chat-bubble", [
    "box-sizing:\\s*border-box",
    "max-width:\\s*100%",
    "min-width:\\s*0",
    "overflow-wrap:\\s*anywhere",
  ]);
});

test("markdown content inside chat bubbles cannot force horizontal clipping", () => {
  assertDeclarations(".chat-md", [
    "max-width:\\s*100%",
    "min-width:\\s*0",
    "overflow-wrap:\\s*anywhere",
  ]);
  assertDeclarations(".inline-file-reference-card", [
    "box-sizing:\\s*border-box",
    "max-width:\\s*min\\(100%,\\s*360px\\)",
    "min-width:\\s*0",
  ]);
  assertDeclarations(".inline-task-reference-card", ["min-width:\\s*0"]);
  assertDeclarations(".chat-code-block", [
    "max-width:\\s*100%",
    "min-width:\\s*0",
    "overflow-x:\\s*auto",
  ]);
  assertDeclarations(".chat-md-table-scroll", [
    "max-width:\\s*100%",
    "min-width:\\s*0",
    "overflow-x:\\s*auto",
  ]);
});

test("floating chat bubbles honor the shared chat surface treatment", () => {
  assert.match(messageBubble, /var\(--message-bubble-other-bg,/);
  assert.match(messageBubble, /var\(--message-bubble-other-border,/);
  assert.match(messageBubble, /var\(--message-bubble-other-shadow,/);
  assert.match(messageBubble, /var\(--message-bubble-user-radius,/);
  assert.match(messageBubble, /var\(--message-bubble-user-radius, 16px 0 16px 16px\)/);
  assert.match(messageBubble, /var\(--message-bubble-other-radius, 0 16px 16px 16px\)/);
  assertDeclarations(".chat-bubble--bot", [
    "--message-bubble-other-border:\\s*0",
    "--message-bubble-other-radius:\\s*0 16px 16px 16px",
    "--message-bubble-other-shadow:",
  ]);
  assertDeclarations(".chat-bubble--user", [
    "--message-bubble-user-radius:\\s*16px 0 16px 16px",
    "--message-bubble-user-shadow:",
  ]);
});
