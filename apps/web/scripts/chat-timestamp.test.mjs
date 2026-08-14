#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

const [
  timestampSource,
  floatingSource,
  embeddedSource,
  workspaceSource,
  cssSource,
  enSource,
  zhSource,
  esSource,
] = await Promise.all([
  read("../src/components/chat/ChatTimestamp.tsx"),
  read("../src/components/FloatingChat.tsx"),
  read("../src/components/EmbeddedChat.tsx"),
  read("../src/components/WorkspaceChat.tsx"),
  read("../src/index.css"),
  read("../src/lib/i18n/en.ts"),
  read("../src/lib/i18n/zh.ts"),
  read("../src/lib/i18n/es.ts"),
]);

test("chat timestamps keep the compact time and reveal the full recorded date", () => {
  assert.match(timestampSource, /const \[expanded, setExpanded\] = useState\(false\)/);
  assert.match(timestampSource, /year: "numeric"/);
  assert.match(timestampSource, /month: "short"/);
  assert.match(timestampSource, /day: "numeric"/);
  assert.match(timestampSource, /aria-expanded=\{expanded\}/);
  assert.match(timestampSource, /expanded \? fullLabel : shortLabel/);
  assert.match(cssSource, /\.chat-timestamp-button:focus-visible/);
});

test("all chat surfaces share the expandable timestamp", () => {
  assert.match(floatingSource, /<ChatTimestamp timestamp=\{msg\.timestamp\} \/>/);
  assert.match(embeddedSource, /<ChatTimestamp timestamp=\{msg\.timestamp\} \/>/);
  assert.match(workspaceSource, /<ChatTimestamp timestamp=\{msg\.created_at\} className="chat-message-time" \/>/);
});

test("expandable timestamp controls are localized", () => {
  for (const source of [enSource, zhSource, esSource]) {
    assert.match(source, /"component\.chat_timestamp\.show_full_date"/);
    assert.match(source, /"component\.chat_timestamp\.show_time_only"/);
  }
});
