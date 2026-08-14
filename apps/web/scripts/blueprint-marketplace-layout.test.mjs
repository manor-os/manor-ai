import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("blueprint updates stay inside their gallery card grid item", async () => {
  const source = await readFile(
    new URL("../src/pages/BlueprintList.tsx", import.meta.url),
    "utf8",
  );

  assert.match(
    source,
    /return \(\s*<div className="blueprint-marketplace-card-item">\s*<Link/,
    "each blueprint must return one gallery card grid item",
  );
  assert.match(source, /<WorkspaceAppCard/);
  assert.match(source, /\{stale\.length > 0 && \(/);
  assert.ok(source.includes("aria-label={updateLabel}"));
  assert.match(source, /className="blueprint-marketplace-update"/);
});
