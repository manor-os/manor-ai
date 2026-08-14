import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const styles = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

test("chat artifact cards use a borderless message-bubble surface", () => {
  assert.match(
    styles,
    /\.chat-artifact-summary\s*\{[^}]*border:\s*0;[^}]*background:\s*var\(--message-bubble-other-bg[^}]*box-shadow:\s*var\(\s*--message-bubble-other-shadow/s,
  );
  assert.match(
    styles,
    /\.chat-artifact-summary \.chat-artifact-thumb,[\s\S]*?\.chat-artifact-summary \.chat-output-artifact-icon\s*\{[^}]*border:\s*0;[^}]*box-shadow:\s*var\(--shadow-sm\)/,
  );
  assert.match(
    styles,
    /\.chat-artifact-summary-download\s*\{[^}]*border:\s*0;/s,
  );
});

test("chat artifact cards provide stronger hover and keyboard focus feedback", () => {
  assert.match(
    styles,
    /\.chat-artifact-summary:hover\s*\{[^}]*box-shadow:\s*var\(--shadow-md\);[^}]*transform:\s*translateY\(-2px\);/s,
  );
  assert.match(
    styles,
    /\.chat-artifact-summary:focus-within\s*\{[^}]*var\(--accent-ring\)[^}]*var\(--shadow-md\)/s,
  );
  assert.match(
    styles,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.chat-artifact-summary:hover\s*\{[^}]*transform:\s*none;/,
  );
});
