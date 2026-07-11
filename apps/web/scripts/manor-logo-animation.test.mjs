#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const layoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const cssSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);

test("sidebar Manor logo exposes independently animated SVG segments", () => {
  assert.match(layoutSource, /className="manor-brand-logo-cap"/);
  assert.match(layoutSource, /manor-brand-logo-leg--left/);
  assert.match(layoutSource, /manor-brand-logo-leg--right/);
  assert.match(cssSource, /@keyframes manor-brand-cap-hover/);
  assert.match(cssSource, /@keyframes manor-brand-left-hover/);
  assert.match(cssSource, /@keyframes manor-brand-right-hover/);
});

test("sidebar Manor logo animation respects reduced-motion preferences", () => {
  assert.match(cssSource, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(cssSource, /\.manor-brand-mark:hover \.manor-brand-logo path\s*\{\s*animation: none;/);
});
