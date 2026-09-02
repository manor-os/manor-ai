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
  assert.match(floatingPanel, /className="floating-panel"/);
  assert.doesNotMatch(floatingPanel, /width\?:|height\?:|style\?:|className\?:/);
  assertDeclarations(".floating-panel", [
    "width:\\s*min\\(380px, calc\\(100% - 48px\\)\\)",
    "height:\\s*min\\(560px, calc\\(100vh - 48px\\)\\)",
    "height:\\s*min\\(560px, calc\\(100dvh - 48px\\)\\)",
    "max-width:\\s*380px",
    "pointer-events:\\s*none",
    "transform:\\s*translateY\\(16px\\) scale\\(0\\.95\\)",
  ]);
  assert.doesNotMatch(css, /@media \(min-width: 641px\) and \(max-width: 1000px\)/);
  assert.match(
    css,
    /@media \(max-width: 640px\) \{[\s\S]*?\.floating-panel \{[\s\S]*?width:\s*min\([\s\S]*?calc\(100% - 24px\)/,
  );
  assert.match(
    css,
    /@media \(max-width: 640px\) \{[\s\S]*?\.floating-panel \{[\s\S]*?100dvh[\s\S]*?safe-area-inset-bottom/,
  );
  assert.match(messageRow, /alignItems: "flex-start"/);
  assertDeclarations(".chat-message-row", ["min-width:\\s*0"]);
  assertDeclarations(".chat-bubble", [
    "box-sizing:\\s*border-box",
    "max-width:\\s*100%",
    "min-width:\\s*0",
    "overflow-wrap:\\s*anywhere",
  ]);
});

test("floating panel width remains continuous across responsive breakpoints", () => {
  const panelWidth = (viewportWidth) => {
    const gutter = viewportWidth <= 640 ? 24 : 48;
    return Math.min(viewportWidth - gutter, 380);
  };

  assert.ok(Math.abs(panelWidth(640) - panelWidth(641)) < 1);
  assert.equal(panelWidth(800), 380);
  assert.equal(panelWidth(1000), 380);
  for (let width = 320; width < 1440; width += 1) {
    assert.ok(
      Math.abs(panelWidth(width + 1) - panelWidth(width)) <= 1,
      `panel width should not jump between ${width}px and ${width + 1}px`,
    );
  }
});

test("floating panel and launcher animations honor reduced motion", () => {
  assert.match(
    css,
    /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.floating-panel,[\s\S]*?\.float-chat-btn,[\s\S]*?\.floating-chat-suggestion \{[\s\S]*?animation:\s*none;[\s\S]*?transition:\s*none;/,
  );
  assert.doesNotMatch(floatingPanel, /transition:\s*"all|transform:\s*open|opacity:\s*open/);
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

test("light user bubbles keep attachment card labels readable", () => {
  assertDeclarations(
    'html:not([data-theme="dark"]) .chat-bubble--user .chat-message-reference-strip .inline-file-reference-card',
    ["color:\\s*var\\(--text-default\\)"],
  );
  assert.match(
    css,
    /html:not\(\[data-theme="dark"\]\) \.chat-bubble--user \.chat-message-reference-strip \.inline-file-reference-card__type,[\s\S]*?\.inline-file-reference-card__unresolved\s*\{\s*color:\s*var\(--text-muted\);/,
  );
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
