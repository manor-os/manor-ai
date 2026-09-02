#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const cssSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const appLayoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([\\s\\S]*?)\\}`).exec(cssSource);
  return match?.[1] || "";
}

test("embedded chat header owns a stacking layer above scrollable messages", () => {
  const header = ruleBody(".embedded-chat-header");
  const workbench = ruleBody(".embedded-chat-workbench");

  assert.match(header, /position:\s*relative;/);
  assert.match(header, /z-index:\s*(?:[1-9]\d{1,}|[3-9]\d);/);
  assert.match(workbench, /position:\s*relative;/);
  assert.match(workbench, /z-index:\s*0;/);
});

test("chat composer and app shells use shared borderless elevation", () => {
  const composer = ruleBody(".chat-composer");
  const embeddedComposer = ruleBody(
    ".embedded-chat-footer > .chat-composer:not(.chat-composer--focused)",
  );

  assert.match(composer, /border:\s*0;/);
  assert.match(composer, /box-shadow:\s*var\(--glass-highlight\), var\(--shadow-md\);/);
  assert.match(
    embeddedComposer,
    /box-shadow:\s*var\(--glass-highlight\), var\(--shadow-ambient\);/,
  );
  assert.match(
    cssSource,
    /html:not\(\[data-theme="dark"\]\) \.app-shell-sidebar,\s*\.app-chat-shell,\s*\.app-content-panel:not\(\.app-content-panel--flush\)\s*\{[\s\S]*?border:\s*0 !important;[\s\S]*?var\(--shadow-md\)/,
  );
  assert.match(appLayoutSource, /className="app-chat-shell"/);
  assert.doesNotMatch(
    appLayoutSource,
    /className="app-chat-shell"[\s\S]{0,600}border:\s*"1px solid var\(--chrome-border\)"/,
  );
});
