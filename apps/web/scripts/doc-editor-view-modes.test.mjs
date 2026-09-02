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

test("plain text edits connect native input and accepted AI previews to bounded history", () => {
  assert.match(editorSource, /const plainTextHistoryRef = useRef\(createPlainTextHistory\(\)\);/);
  assert.match(
    editorSource,
    /recordPlainTextHistory\([\s\S]*?input: options\.input,[\s\S]*?beforeGeneration,[\s\S]*?afterGeneration,/,
  );
  assert.match(editorSource, /onBeforeInput=\{\(event\) => \{/);
  assert.match(editorSource, /preview\.mode === "text"[\s\S]*?recordPlainTextHistory\([\s\S]*?preview\.baseline,[\s\S]*?finalContent/);
  assert.match(editorSource, /runPlainTextHistoryCommand\(e\.shiftKey \? "redo" : "undo"\)/);
  assert.doesNotMatch(editorSource, /document\.execCommand\(command\)/);
});

test("late text-byte decoding preserves edits in every text-like editor", () => {
  assert.match(
    editorSource,
    /const keepLocalTextChange = textSaveEditRevisionRef\.current !== textSavePersistedRevisionRef\.current[\s\S]*?contentRef\.current !== decoded\.text[\s\S]*?if \(!keepLocalTextChange\)/,
  );
  assert.doesNotMatch(editorSource, /const keepLocalTextChange = mode === "text"/);
  assert.match(editorSource, /needsTextByteHydration[\s\S]*?textBytesReadyDocumentId !== docId/);
  assert.match(
    editorSource,
    /if \(isCsv\)[\s\S]*?if \(!keepLocalTextChange\) \{[\s\S]*?setSheetData\(/,
  );
});
