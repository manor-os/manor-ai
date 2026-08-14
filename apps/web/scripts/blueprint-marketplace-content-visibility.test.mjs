import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

test("marketplace blueprint metadata stays visible without hover or a first touch", async () => {
  const source = await readFile(
    new URL("../src/pages/BlueprintList.tsx", import.meta.url),
    "utf8",
  );

  // The bug this guards: the card used to reveal its title, price and author
  // only once the pointer was over it, which on a touch screen meant the first
  // tap was spent uncovering the text instead of opening the blueprint.
  assert.doesNotMatch(source, /showDetails/);
  assert.doesNotMatch(source, /matchMedia\("\(hover: none\)"\)/);
  assert.doesNotMatch(source, /opacity: hasMedia && !showDetails \? 0 : 1/);

  // Hover is allowed to drive cover media (autoplay, scrubbing between
  // assets). It may never reach a visibility property — that is the whole
  // difference between "the cover animates" and "the copy is hidden".
  assert.doesNotMatch(source, /(opacity|visibility|display)[^;\n]*\bhovered\b/);

  // The copy itself is unconditional markup, not something a state flag emits.
  assert.match(source, /blueprint-marketplace-card-body/);
  assert.match(source, /blueprint-marketplace-card-title-row/);
  assert.match(source, /blueprint-marketplace-card-intro/);
  assert.match(source, /blueprint-marketplace-card-meta/);
  assert.match(source, /aria-hidden="true"/);
});
