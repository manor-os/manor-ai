import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [layoutSource, marqueeSource, cssSource] = await Promise.all([
  readFile(new URL("../src/layouts/AppLayout.tsx", import.meta.url), "utf8"),
  readFile(
    new URL("../src/components/ui/HoverMarqueeText.tsx", import.meta.url),
    "utf8",
  ),
  readFile(new URL("../src/index.css", import.meta.url), "utf8"),
]);

test("workspace sidebar names reveal truncated text on hover", () => {
  assert.match(
    layoutSource,
    /filteredWorkspaces\.map[\s\S]*?<HoverMarqueeText[\s\S]*?className="conv-name"[\s\S]*?text=\{ws\.name\}/,
  );
  assert.match(marqueeSource, /track\.scrollWidth - viewport\.clientWidth/);
  assert.match(marqueeSource, /new ResizeObserver\(measure\)/);
  assert.match(
    cssSource,
    /data-overflowing="true"[\s\S]*?:hover[\s\S]*?translateX\(var\(--hover-marquee-distance\)\)/,
  );
});

test("workspace marquee remains accessible and respects reduced motion", () => {
  assert.match(
    layoutSource,
    /role="button"[\s\S]*?tabIndex=\{0\}[\s\S]*?conv-row--button/,
  );
  assert.match(layoutSource, /event\.key === "Enter"[\s\S]*?event\.key === " "/);
  assert.match(marqueeSource, /title=\{isOverflowing \? text : undefined\}/);
  assert.match(marqueeSource, /aria-hidden="true"/);
  assert.match(cssSource, /@media \(prefers-reduced-motion: reduce\)/);
});

test("workspace marquee swaps layers without an opacity flash", () => {
  assert.doesNotMatch(cssSource, /transition:\s*\n\s*opacity 0\.1s ease,/);
  assert.match(
    cssSource,
    /transition: transform var\(--hover-marquee-duration\) linear 0\.35s;/,
  );
});
