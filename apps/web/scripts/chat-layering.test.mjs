#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const cssSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`).exec(cssSource);
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
