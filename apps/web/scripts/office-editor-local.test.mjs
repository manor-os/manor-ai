import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const read = (relativePath) => readFile(new URL(`../${relativePath}`, import.meta.url), "utf8");

const [editor, viewer, api] = await Promise.all([
  read("src/pages/DocEditor.tsx"),
  read("src/pages/FileViewer.tsx"),
  read("src/lib/api.ts"),
]);

for (const source of [editor, viewer, api]) {
  assert.doesNotMatch(source, /OnlyOffice|onlyoffice|office-session/i);
}

assert.match(editor, /editDocumentFileWithSnapshot/);
assert.match(editor, /preserveSpreadsheetFile/);
assert.match(editor, /preservePresentationFile/);
assert.match(viewer, /<DocxViewer/);
assert.match(viewer, /<XlsxViewer/);
assert.match(viewer, /<PptxViewer/);
