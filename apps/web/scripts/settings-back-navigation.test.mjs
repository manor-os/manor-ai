#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const settings = await readFile(
  new URL("../src/pages/Settings.tsx", import.meta.url),
  "utf8",
);

test("settings back navigation uses the shared arrow icon", () => {
  assert.match(settings, /IconArrowLeft,/);
  assert.match(settings, /<IconArrowLeft size=\{16\} aria-hidden \/>/);
  assert.doesNotMatch(settings, /<span aria-hidden>\{"<"\}<\/span>/);
});
