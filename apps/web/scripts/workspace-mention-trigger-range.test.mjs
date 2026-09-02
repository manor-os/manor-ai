import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { transform } from "esbuild";

const composerSource = await readFile(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);

async function loadReplaceMentionTriggerRange() {
  assert.match(
    composerSource,
    /export function replaceMentionTriggerRange/,
    "Chat composer must expose its mention trigger replacement helper",
  );
  const match = composerSource.match(
    /function replaceAutocompleteTriggerRange[\s\S]*?\n}\n\nexport function workflowInvokeMessage/,
  );
  assert.ok(match, "Mention trigger helper must share the composer range recovery implementation");
  const source = match[0].replace(/\n\nexport function workflowInvokeMessage$/, "");
  const compiled = await transform(source, {
    loader: "ts",
    format: "esm",
    target: "es2022",
  });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString("base64")}`);
}

test("mention selection recovers the trailing at-sign when a menu click leaves the native cursor at the start", async () => {
  const { replaceMentionTriggerRange } = await loadReplaceMentionTriggerRange();

  const result = replaceMentionTriggerRange(
    "QA-WS02-MENTION @",
    0,
    1,
    "@Agent A",
    1,
  );

  assert.equal(result.text, "QA-WS02-MENTION @Agent A ");
  assert.equal(result.cursor, result.text.length);
});

test("mention selection preserves text after the stale mention query", async () => {
  const { replaceMentionTriggerRange } = await loadReplaceMentionTriggerRange();

  const result = replaceMentionTriggerRange(
    "draft @age follow-up",
    0,
    1,
    "@Agent A",
    1,
  );

  assert.equal(result.text, "draft @Agent A follow-up");
  assert.equal(result.cursor, "draft @Agent A".length);
});
