#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

function readSource(path) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const boundary = readSource("../src/components/AuthSessionBoundary.tsx");
const authStore = readSource("../src/stores/auth.ts");
const authToken = readSource("../src/lib/authToken.ts");
const api = readSource("../src/lib/api.ts");

test("real user activity slides the short-lived session without background heartbeats", () => {
  assert.match(boundary, /ACTIVE_SESSION_RENEW_INTERVAL_MS = 5 \* 60 \* 1000/);
  assert.match(boundary, /\["pointerdown", "keydown", "touchstart", "wheel"\]/);
  assert.match(boundary, /useAuthStore\.getState\(\)\.renewSession\(\)/);
  assert.doesNotMatch(boundary, /mousemove/);
  assert.doesNotMatch(boundary, /setInterval/);
});

test("renewal rotates the token without clearing identity-bound UI state", () => {
  assert.match(authStore, /renewSession: async \(\) =>/);
  assert.match(authStore, /api\.auth\.renew\(\)/);
  assert.match(api, /request<[^>]+>\("\/auth\/renew"/);
  assert.match(api, /path !== "\/auth\/renew"/);
  assert.match(authToken, /export function isSameAuthIdentity/);
  assert.match(boundary, /!isSameAuthIdentity\(previousTokenRef\.current, token\)/);
});

test("chat streams share session-expiry recovery and retry a concurrently rotated token", () => {
  assert.match(api, /async function requestStreamResponse\(/);
  assert.match(
    api,
    /response\.status === 401[\s\S]*?latestToken !== token[\s\S]*?token = latestToken;[\s\S]*?continue;/,
  );
  assert.match(api, /handleSessionExpired\(path\)/);
  assert.match(api, /return requestStreamResponse\("\/chat\/stream", form, opts\?\.signal\)/);
  assert.match(
    api,
    /return requestStreamResponse\([\s\S]*?\/chat\/flow-entrypoints\/\$\{encodeURIComponent\(bindingId\)\}\/stream/,
  );
});

test("support impersonation keeps its separate hard expiry", () => {
  assert.match(boundary, /claims\.typ === "impersonation"/);
});
