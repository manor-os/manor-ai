import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readSource = (path) => readFile(new URL(path, import.meta.url), "utf8");
const [engine, editor, viewer, sanitizer, writer] = await Promise.all([
  readSource("../src/lib/manorDocumentEngine.ts"),
  readSource("../src/pages/DocEditor.tsx"),
  readSource("../src/pages/FileViewer.tsx"),
  readSource("../src/lib/sanitizeDocumentHtml.ts"),
  readSource("../src/lib/documentOoxml.ts"),
]);

test("Knowledge Word rendering uses Manor's OOXML engine", () => {
  assert.match(editor, /const rendered = await renderManorDocument\(buf\)/);
  assert.match(viewer, /const rendered = await renderManorDocument\(buf\)/);
  assert.doesNotMatch(editor, /import\("mammoth"\)/);
  assert.doesNotMatch(viewer, /import\("mammoth"\)/);
  assert.match(engine, /zip\.file\("word\/document\.xml"\)/);
  assert.match(engine, /zip\.file\("word\/styles\.xml"\)/);
  assert.match(engine, /zip\.file\("word\/numbering\.xml"\)/);
  assert.match(engine, /zip\.file\("word\/theme\/theme1\.xml"\)/);
  assert.match(engine, /zip\.file\("word\/footnotes\.xml"\)/);
  assert.match(engine, /zip\.file\("word\/fontTable\.xml"\)/);
  assert.match(engine, /zip\.file\("word\/settings\.xml"\)/);
  assert.match(engine, /parseFontDefinitions\(fontTableXml\)/);
});

test("native Word pages retain editable structure and measured pagination", () => {
  assert.match(engine, /data-docx-paragraph-index/);
  assert.match(engine, /data-docx-list-label/);
  assert.match(engine, /data-manor-docx-page/);
  assert.match(engine, /pageContent\.scrollHeight > pageContent\.clientHeight/);
  assert.match(engine, /data-manor-docx-table-fragment/);
  assert.match(engine, /data-manor-docx-paragraph-fragment/);
  assert.match(engine, /splitParagraphToFit/);
  assert.match(engine, /renderedLineCount/);
  assert.match(engine, /data-docx-widow-control/);
  assert.match(engine, /serializeManorDocumentHtml/);
  assert.match(editor, /serializeManorDocumentHtml\(editorRef\.current\)/);
  assert.match(editor, /paginateManorDocument\(editorRef\.current, docxRender\)/);
  assert.match(writer, /existingIndexes\.every\(\(value\): value is number => value != null\)/);
  assert.match(writer, /new Set\(existingIndexes\)\.size === existingIndexes\.length/);
});

test("native Word layout honors numbering overrides, table geometry, and page furniture", () => {
  assert.match(engine, /startOverride/);
  assert.match(engine, /firstHeaderHtml/);
  assert.match(engine, /evenHeaderHtml/);
  assert.match(engine, /differentFirstPage/);
  assert.match(engine, /differentEvenPages/);
  assert.match(engine, /<colgroup>/);
  assert.match(engine, /tableMeasure/);
  assert.match(engine, /data-docx-table-header/);
  assert.match(engine, /dynamicFieldKind/);
  assert.match(engine, /data-docx-field/);
  assert.match(engine, /field\.textContent = String\(pageCount\)/);
  assert.match(engine, /manor-docx-floating-picture/);
  assert.match(engine, /data-docx-anchor-page-scope/);
  assert.match(engine, /positionH/);
  assert.match(engine, /positionV/);
  assert.match(sanitizer, /data-docx-anchor-page-scope/);
  assert.match(engine, /data-docx-section-after/);
  assert.match(engine, /docxSectionIndex/);
  assert.match(engine, /render\.sections\.map\(\(section\) => section\.layout\.pageWidthPx\)/);
  assert.match(engine, /breakType !== "continuous"/);
  assert.match(sanitizer, /sanitizeManorDocumentRender/);
});

test("native Word grouped text boxes use member geometry without flattening the group", () => {
  assert.match(engine, /drawingGroupMemberGeometry/);
  assert.match(engine, /ancestor\.localName === "wgp" \|\| ancestor\.localName === "grpSp"/);
  assert.match(engine, /groupedGeometry\?\.width \?\? emuToPixels/);
  assert.match(engine, /addPixelOffset\(css\.left, groupedGeometry\.left\)/);
  assert.match(engine, /groupedGeometry\?\.rotation/);
  assert.match(engine, /defaultInsets = \{ tIns: 45720, rIns: 91440, bIns: 45720, lIns: 91440 \}/);
  assert.match(engine, /const geometricInset = 0\.146447/);
  assert.match(
    writer,
    /async function rebuildDocumentBodyInPackage[\s\S]*?isolateWordTextBoxes\(documentXml\)[\s\S]*?patchWordTextBoxContents[\s\S]*?restoreWordTextBoxes/,
  );
  assert.match(writer, /documentBodyMergeEquivalent/);
  assert.match(writer, /!element\.closest\("\.manor-docx-text-box"\)/);
  assert.match(writer, /addedTextBoxResource/);
});

test("only renderer-owned DOCX markup can retain layout styles", () => {
  assert.match(sanitizer, /allowDocxLayoutStyles\?: boolean/);
  assert.match(sanitizer, /allowDocxLayoutStyles \? \["srcset"\] : \["style", "srcset"\]/);
  assert.match(editor, /sanitizeDocumentHtml\(html\)/, "pasted HTML should keep the strict sanitizer path");
});
