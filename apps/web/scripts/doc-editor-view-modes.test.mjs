import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const editorSource = await readFile(
  new URL("../src/pages/DocEditor.tsx", import.meta.url),
  "utf8",
);

test("markdown preview mode renders preview without an implicit split override", () => {
  assert.doesNotMatch(
    editorSource,
    /liveDiff\s*&&\s*markdownViewMode\s*===\s*["']preview["']\s*\?\s*["']split["']/,
  );
  assert.match(
    editorSource,
    /markdown-editor-layout--\$\{markdownViewMode\}[\s\S]*?markdownViewMode !== "preview"[\s\S]*?markdownViewMode !== "source"/,
  );
});

test("document header toggles share the same accessible neutral selected state", () => {
  assert.match(
    editorSource,
    /className=\{showComments \? "btn-manor-teal-light" : "btn-manor-ghost"\}[\s\S]*?aria-pressed=\{showComments\}/,
  );
  assert.match(
    editorSource,
    /className=\{showVersions \? "btn-manor-teal-light" : "btn-manor-ghost"\}[\s\S]*?aria-pressed=\{showVersions\}/,
  );
  assert.doesNotMatch(editorSource, /btn-manor-neutral-light/);
});
