#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

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
