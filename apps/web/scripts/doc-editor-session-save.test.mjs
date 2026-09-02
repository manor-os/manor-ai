import assert from "node:assert/strict";
import test from "node:test";

import { readFile } from "node:fs/promises";

const editorSource = await readFile(
  new URL("../src/pages/DocEditor.tsx", import.meta.url),
  "utf8",
);

test("text saves stay scoped to their document session and latest edit", () => {
  assert.match(
    editorSource,
    /type TextSaveRequest = \{[\s\S]*?documentId: string;[\s\S]*?sessionRevision: number;[\s\S]*?editRevision: number;/,
  );
  assert.match(
    editorSource,
    /api\.documents\.replaceFile\(\s*documentId,\s*file,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    editorSource,
    /api\.documents\.saveContent\(\s*documentId,\s*text,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    editorSource,
    /const pendingTextSave = pendingTextSaveRef\.current;[\s\S]*?textSaveMutateRef\.current\(pendingTextSave\);[\s\S]*?textSaveSessionRevisionRef\.current \+= 1/,
  );
  assert.match(
    editorSource,
    /if \(request\.editRevision === textSaveEditRevisionRef\.current\) setSaveStatus\("saved"\)/,
  );
  assert.match(editorSource, /retryDocumentSave\(async \(\) => \{/);
  assert.match(
    editorSource,
    /error instanceof ApiError[\s\S]*?DOCUMENT_SAVE_RETRYABLE_STATUSES\.has\(error\.status\)[\s\S]*?error\.status >= 500 && error\.status < 600/,
  );
  assert.match(editorSource, /return error instanceof TypeError/);
  assert.match(
    editorSource,
    /retryDelay == null \|\| !isRetryableDocumentSaveError\(error\)/,
  );
  assert.match(
    editorSource,
    /const \{ file, savedBuffer, savedHtml \} = await editDocumentFileWithSnapshot\([\s\S]*?return retryDocumentSave\(async \(\) => \{[\s\S]*?replaceFile\(\s*documentId,\s*file,\s*saveIntent,/,
  );
  assert.doesNotMatch(editorSource, /retry: 2/);
  assert.match(editorSource, /showSaveError\(request\.documentName/);
  assert.match(editorSource, /authPrincipalKey: string;/);
  assert.match(editorSource, /authPrincipalKey\(getAuthToken\(\)\)/);
  assert.match(
    editorSource,
    /authPrincipalKey\(currentToken\) !== expectedPrincipalKey/,
  );
  assert.match(
    editorSource,
    /saveContent\(\s*documentId,\s*text,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
});

test("spreadsheet and presentation saves keep their original document identity", () => {
  assert.match(
    editorSource,
    /type SpreadsheetSaveRequest = \{[\s\S]*?documentId: string;[\s\S]*?sessionRevision: number;/,
  );
  assert.match(
    editorSource,
    /type PresentationSaveRequest = \{[\s\S]*?documentId: string;[\s\S]*?sessionRevision: number;/,
  );
  assert.match(
    editorSource,
    /replaceFile\(\s*request\.documentId,\s*file,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    editorSource,
    /file = await preserveSpreadsheetFile\([\s\S]*?return retryDocumentSave\(async \(\) => \{[\s\S]*?replaceFile\(\s*request\.documentId,\s*file,\s*saveIntent,/,
  );
  assert.match(
    editorSource,
    /XLSX\.read\(savedBuffer,[\s\S]*?await spreadsheetSheetsFromFile\(XLSX, savedWorkbook, savedBuffer\)[\s\S]*?savedWorkbookSheets/,
    "spreadsheet saves should reparse the persisted workbook before advancing the editor baseline",
  );
  assert.match(
    editorSource,
    /xlsxBaselineSheetsRef\.current = nextSheets\.map\(spreadsheetSheetSnapshot\)/,
    "the next spreadsheet baseline should use the canonical saved workbook model",
  );
  assert.match(
    editorSource,
    /const pendingSpreadsheetSave = pendingSpreadsheetSaveRef\.current;[\s\S]*?spreadsheetSaveMutateRef\.current\(pendingSpreadsheetSave\)/,
  );
  assert.match(
    editorSource,
    /const pendingPresentationSave = pendingPresentationSaveRef\.current;[\s\S]*?presentationSaveMutateRef\.current\(pendingPresentationSave\)/,
  );
});

test("text saves wait for byte-format hydration and keep a writable fallback", () => {
  assert.match(editorSource, /textFormatLoad: Promise<TextFileSaveSnapshot \| null> \| null;/);
  assert.match(editorSource, /const loadedTextFormat = await request\.textFormatLoad;/);
  assert.match(editorSource, /const savedTextFormat = textFileFormatForSave\(textFormat\)/);
  assert.doesNotMatch(editorSource, /requiresTextFormat|saving is disabled to protect its encoding/);
  assert.match(editorSource, /await api\.documents\.saveContent\(/);
  assert.match(
    editorSource,
    /textFormatLoadRef\.current = \{ documentId: docId, sessionRevision, promise: loadPromise \};/,
  );
  assert.match(editorSource, /needsTextByteHydration[\s\S]*?textBytesReadyDocumentId !== docId/);
});

test("fallback content hydration preserves unsaved local edits", () => {
  assert.match(
    editorSource,
    /const hasUnsavedLocalChange = textSaveEditRevisionRef\.current !== textSavePersistedRevisionRef\.current[\s\S]*?if \(hasUnsavedLocalChange\) return;/,
  );
});
