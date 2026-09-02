import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { transform } from "esbuild";

const composerSource = await readFile(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);

async function loadReplaceWorkflowTriggerRange() {
  const match = composerSource.match(
    /function replaceAutocompleteTriggerRange[\s\S]*?\n}\n\nexport function workflowInvokeMessage/,
  );
  assert.ok(match, "Chat composer must expose its Flow trigger replacement helper");
  const source = match[0].replace(/\n\nexport function workflowInvokeMessage$/, "");
  const compiled = await transform(source, {
    loader: "ts",
    format: "esm",
    target: "es2022",
  });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString("base64")}`);
}

test("Flow selection consumes the typed percent when its stored trigger position is stale", async () => {
  const { replaceWorkflowTriggerRange } = await loadReplaceWorkflowTriggerRange();

  // Clicking a menu item can move the native selection just after the `%`.
  // The controlled draft still needs to replace that trigger, not append a
  // second token and leave the original percent behind.
  const result = replaceWorkflowTriggerRange("draft %", 7, 7, "%plan-product-video-v1", 7);

  assert.equal(result.text, "draft %plan-product-video-v1 ");
  assert.equal(result.cursor, result.text.length);
});

test("Flow selection recovers the trailing trigger when a menu click leaves the native cursor at the start", async () => {
  const { replaceWorkflowTriggerRange } = await loadReplaceWorkflowTriggerRange();

  const result = replaceWorkflowTriggerRange(
    "QA token probe %",
    0,
    1,
    "%plan-product-video-v1",
    1,
  );

  assert.equal(result.text, "QA token probe %plan-product-video-v1 ");
  assert.equal(result.cursor, result.text.length);
});

test("Flow selection replaces only the stale trigger token and preserves following text", async () => {
  const { replaceWorkflowTriggerRange } = await loadReplaceWorkflowTriggerRange();

  const result = replaceWorkflowTriggerRange(
    "draft %plan follow-up",
    0,
    1,
    "%plan-product-video-v1",
    1,
  );

  assert.equal(result.text, "draft %plan-product-video-v1 follow-up");
  assert.equal(result.cursor, "draft %plan-product-video-v1".length);
});
