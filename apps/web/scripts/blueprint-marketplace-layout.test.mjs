import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("blueprint updates stay inside their marketplace card grid item", async () => {
  const source = await readFile(
    new URL("../src/pages/BlueprintList.tsx", import.meta.url),
    "utf8",
  );

  assert.match(
    source,
    /return \(\s*<div style=\{\{ position: "relative", minWidth: 0 \}\}>\s*\{renderCard\(\)\}/,
    "each blueprint must return one grid wrapper with the card first",
  );
  assert.doesNotMatch(
    source,
    /return \(\s*<>\s*\{stale\.length > 0/,
    "update notices must not become separate CSS-grid children",
  );
  assert.ok(source.includes("aria-label={updateLabel}"));
  assert.ok(source.includes('position: "absolute"'));
  assert.ok(source.includes("reserveTopAction={stale.length > 0}"));
});
