#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";


const pageSource = readFileSync(
  new URL("../src/pages/Integrations.tsx", import.meta.url),
  "utf8",
);


test("Discord uses the shared OAuth connect flow without per-user bot secrets", () => {
  assert.doesNotMatch(pageSource, /discord:\s*\{\s*fields:\s*\[/s);
  assert.match(pageSource, /const isOAuth = server\.auth_type === "oauth2"/);
  assert.match(pageSource, /onConnect\(\)/);
});
