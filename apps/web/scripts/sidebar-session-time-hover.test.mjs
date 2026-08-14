import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appLayout = readFileSync(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

test("sidebar session timestamps are hidden until row hover or keyboard focus", () => {
  assert.match(appLayout, /<span className="manor-session-time">/);
  assert.match(
    css,
    /\.manor-session-time\s*\{[\s\S]*?max-width:\s*0;[\s\S]*?opacity:\s*0;/,
  );
  assert.match(
    css,
    /\.manor-session-row:hover \.manor-session-time,[\s\S]*?\.manor-session-row:focus-within \.manor-session-time\s*\{[\s\S]*?max-width:\s*12em;[\s\S]*?opacity:\s*1;/,
  );
});

test("touch and reduced-motion users retain an accessible timestamp state", () => {
  assert.match(
    css,
    /@media \(hover: none\)[\s\S]*?\.manor-session-row--active \.manor-session-time[\s\S]*?opacity:\s*1;/,
  );
  assert.match(
    css,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.manor-session-time[\s\S]*?transition:\s*none;/,
  );
});
