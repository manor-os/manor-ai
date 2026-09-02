#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const apiSource = await readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const mainSource = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
const appLayoutSource = await readFile(new URL("../src/layouts/AppLayout.tsx", import.meta.url), "utf8");

test("background GET 404s stay inline instead of producing global toast noise", () => {
  assert.match(apiSource, /export function shouldShowRequestErrorToast/);
  assert.match(
    apiSource,
    /normalizedMethod === "GET"[\s\S]*?\(status === 404 \|\| status === 410\)/,
  );
  assert.match(
    apiSource,
    /shouldShowRequestErrorToast\([\s\S]*?useToastStore\.getState\(\)\.error/,
  );
});

test("React Query does not retry terminal API client errors", () => {
  assert.match(apiSource, /export function shouldRetryApiQuery/);
  assert.match(mainSource, /import \{ shouldRetryApiQuery \} from "\.\/lib\/api"/);
  assert.match(mainSource, /retry: shouldRetryApiQuery/);
});

test("chat fails closed when a requested workspace disappeared from the refreshed workspace list", () => {
  assert.match(appLayoutSource, /isSuccess: workspaceListReady/);
  assert.match(
    appLayoutSource,
    /const \[unavailableWorkspaceId, setUnavailableWorkspaceId\] = useState<string \| null>\(null\)/,
  );
  assert.match(
    appLayoutSource,
    /workspaceListReady[\s\S]*?activeConvType !== "operation"[\s\S]*?setUnavailableWorkspaceId\(activeConvId\)[\s\S]*?navigate\("\/chat", \{ replace: true \}\)/,
  );
  assert.match(appLayoutSource, /unavailableWorkspaceId === activeConvId[\s\S]*?page\.workspace_detail\.not_found/);
});
