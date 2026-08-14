#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);

test("chat presentation preview uses the same rendered slide endpoint as FileViewer", () => {
  assert.match(source, /api\.documents\.getSlides\(doc\.id\)/);
});

test("chat CSV preview shares the quote-aware delimited parser", () => {
  assert.match(source, /parseDelimitedText\(content\)\.rows/);
  assert.doesNotMatch(source, /function parseArtifactCSV/);
});

test("chat XLSX preview shares workbook styles, merges, display values, and native charts", () => {
  assert.match(source, /spreadsheetSheetsFromWorkbook\(XLSX, wb\)/);
  assert.match(source, /spreadsheetChartsFromFile\(buf, XLSX, wb\)/);
  assert.match(source, /spreadsheetMergeAt\(sheet\.merges/);
  assert.match(source, /sheet\.displayData\[rowIndex\]/);
  assert.match(source, /<SpreadsheetChartPreview charts=\{sheet\.charts\}/);
});

test("document-backed HTML artifacts render the file itself instead of nesting FileViewer", () => {
  assert.match(source, /function documentIdFromViewerReference/);
  assert.match(
    source,
    /const viewerDocumentId = \[artifact\.href, artifact\.body, artifact\.meta\]/,
  );
  assert.match(
    source,
    /artifact\.kind === "page"[\s\S]*?Boolean\(artifactDocumentId\(artifact\)\)/,
  );
  assert.match(
    source,
    /category === "html" && content[\s\S]*?chat-output-render-frame--raw[\s\S]*?srcDoc=\{content\}/,
  );
});
