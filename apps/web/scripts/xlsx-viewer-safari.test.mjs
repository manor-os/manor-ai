#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [viewerSource, apiSource, embeddedChatSource, docEditorSource] = await Promise.all([
  readFile(new URL("../src/pages/FileViewer.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/components/EmbeddedChat.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8"),
]);

function sourceBetween(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(start, -1, `Missing source marker: ${startMarker}`);
  assert.notEqual(end, -1, `Missing source marker: ${endMarker}`);
  return source.slice(start, end);
}

test("XLSX preview parses the protected Blob directly for Safari", () => {
  assert.match(viewerSource, /function XlsxViewer\(\{ url, blob \}/);
  assert.match(viewerSource, /blob\s*\?\s*await blob\.arrayBuffer\(\)/);
  assert.match(viewerSource, /<XlsxViewer url=\{downloadUrl\} blob=\{downloadBlob\} \/>/);
  assert.match(viewerSource, /setDownloadBlob\(blob\)/);
});

test("binary viewers keep preview state aligned with the downloaded Blob", () => {
  assert.match(viewerSource, /function DocxViewer\(\{[\s\S]*?blob,[\s\S]*?\}: \{/);
  assert.match(viewerSource, /function PdfJsViewer\(\{[\s\S]*?blob,[\s\S]*?}: \{/);
  assert.match(viewerSource, /function PptxViewJsViewer\(\{ url, blob,/);
  assert.match(viewerSource, /function PptxViewer\(\{ url, blob,/);
  assert.match(viewerSource, /<PdfJsViewer\s+url=\{downloadUrl\}\s+blob=\{downloadBlob\}/);
  assert.match(viewerSource, /<DocxViewer\s+url=\{downloadUrl\}\s+blob=\{downloadBlob\}/);
  assert.match(viewerSource, /<PptxViewer\s+url=\{downloadUrl\}\s+blob=\{downloadBlob\}/);
  assert.match(viewerSource, /const replaceDownloadUrl = useCallback\(\(nextUrl: string\)/);
  assert.match(viewerSource, /const downloadUrlRef = useRef\(""\)/);
  assert.match(viewerSource, /const fetchGenerationRef = useRef\(0\)/);
  assert.match(viewerSource, /const isCurrentRequest = \(\) => fetchGenerationRef\.current === requestId/);
  assert.match(viewerSource, /fetchGenerationRef\.current \+= 1/);
  assert.match(viewerSource, /fileViewerMountedRef\.current = false/);
});

test("DOCX preview prefers faithful server pages and keeps the native comment fallback", () => {
  const docxViewerSource = sourceBetween(
    viewerSource,
    "// ── DOCX viewer ──",
    "// ── XLSX viewer ──",
  );
  assert.match(docxViewerSource, /const rendered = await renderManorDocument\(buf\)/);
  assert.match(docxViewerSource, /allowDocxEditorAttributes: true, allowDocxLayoutStyles: true/);
  assert.match(docxViewerSource, /paginateManorDocument\(root, render\)/);
  assert.match(docxViewerSource, /manor-docx-native manor-docx-readonly/);
  assert.match(docxViewerSource, /className="docx-comment-anchor-bar"/);
  assert.match(docxViewerSource, /markQuoteCommentsInElement\(root, visibleCommentAnchors\)/);
  assert.match(docxViewerSource, /updateCommentMarkActiveState\(root, activeCommentId\)/);
  assert.match(docxViewerSource, /onSelectCommentId\?\.\(comment\.id\)/);
  assert.doesNotMatch(docxViewerSource, /import\("mammoth"\)/);
  assert.match(docxViewerSource, /api\.documents\.getPages\(\s*docId,/);
  assert.match(docxViewerSource, /withPreviewTimeout\([\s\S]{0,80}fetch\(`/);
  assert.match(docxViewerSource, /15_000,\s*"Word preview timed out"/);
  assert.match(docxViewerSource, /!preferSelectablePreview/);
  assert.match(docxViewerSource, /new IntersectionObserver\(/);
});

test("PPTX preview revokes server and parser-created object URLs", () => {
  assert.match(viewerSource, /let serverRenderActive = true/);
  assert.match(viewerSource, /if \(cancelled \|\| !serverRenderActive\)/);
  assert.match(viewerSource, /URL\.revokeObjectURL\(blobUrl\)/);
  assert.match(viewerSource, /parsePptx\(buf, \{[\s\S]*?isCancelled: \(\) => cancelled/);
  assert.match(viewerSource, /const parserBlobUrls: string\[\] = \[\]/);
  assert.match(viewerSource, /onObjectUrl: \(objectUrl\) => \{[\s\S]*?parserBlobUrls\.push\(objectUrl\);[\s\S]*?createdBlobUrls\.push\(objectUrl\)/);
  assert.match(viewerSource, /parsedResult\.status === "rejected"[\s\S]*?revokeTrackedBlobUrls\(parserBlobUrls\)/);
  assert.match(viewerSource, /const mediaUrl = URL\.createObjectURL\(/);
  assert.match(viewerSource, /URL\.revokeObjectURL\(mediaUrl\)/);
  assert.match(viewerSource, /revokeCreatedBlobUrls\(\);[\s\S]*?setError\(e\.message/);
  assert.match(viewerSource, /let pptxParseQueue: Promise<void> = Promise\.resolve\(\)/);
  assert.match(viewerSource, /return await parsePptxUnlocked\(buf, options\)/);
  assert.match(viewerSource, /parsePptxUnlocked[\s\S]*?SLIDE_W = 12192000;[\s\S]*?_viewerTheme = \{ \.\.\._viewerDefaultScheme \};[\s\S]*?_viewerMajorFont = "";[\s\S]*?_viewerBgFillStyles = \[\]/);
  assert.match(viewerSource, /slideTheme\?\.colors\.tx1/);
  assert.match(viewerSource, /slideTheme\?\.minorFont/);
});

test("document API exposes the cached protected Blob", () => {
  assert.match(apiSource, /downloadBlob: \(id: string, options\?: DocumentBlobOptions\): Promise<Blob>/);
});

test("caller-owned and stale document downloads cannot replace shared cache state", () => {
  const protectedBlobSource = sourceBetween(
    apiSource,
    "async function fetchProtectedBlob(",
    "function cacheBustUrl(",
  );
  assert.match(protectedBlobSource, /if \(!useCache\) return request/);
  assert.match(protectedBlobSource, /documentDownloadInflight\.set\(cacheKey, request\)/);
  assert.match(
    protectedBlobSource,
    /documentDownloadInflight\.get\(cacheKey\) === request[\s\S]{0,100}documentDownloadInflight\.delete\(cacheKey\)/,
  );
  assert.match(
    protectedBlobSource,
    /useCache\s*&&\s*documentDownloadInflight\.get\(cacheKey\) === request[\s\S]{0,160}documentDownloadCache\.set\(cacheKey/,
  );
});

test("embedded chat XLSX artifacts parse the protected Blob without fetching its object URL", () => {
  const xlsxArtifactSource = sourceBetween(
    embeddedChatSource,
    'if (nextCategory === "xlsx") {',
    'if (nextCategory === "docx") {',
  );
  assert.match(xlsxArtifactSource, /const blob = await api\.documents\.previewBlob\(doc\.id\)/);
  assert.match(xlsxArtifactSource, /const buf = await blob\.arrayBuffer\(\)/);
  assert.match(xlsxArtifactSource, /if \(cancelled\) return/);
  assert.doesNotMatch(xlsxArtifactSource, /fetch\(/);
  assert.doesNotMatch(xlsxArtifactSource, /URL\.createObjectURL/);
});

test("document editor XLSX preview parses the protected Blob and cancels stale loads", () => {
  const xlsxEditorSource = sourceBetween(
    docEditorSource,
    "// Load XLSX: download blob",
    "// Load PPTX:",
  );
  assert.match(xlsxEditorSource, /let cancelled = false/);
  assert.match(xlsxEditorSource, /const source = await loadOfficeEditSource\(docId, needsLegacyOfficeConversion\)/);
  assert.match(xlsxEditorSource, /const \{ buffer: buf, sourceSha256 \} = source/);
  assert.match(xlsxEditorSource, /return \(\) => \{\s*cancelled = true/);
  assert.doesNotMatch(xlsxEditorSource, /api\.documents\.download\(/);
  assert.doesNotMatch(xlsxEditorSource, /fetch\(/);
});

test("DOCX artifact previews prefer faithful rendered pages and retain the protected Blob fallback", () => {
  const docxPageViewerSource = sourceBetween(
    embeddedChatSource,
    "function DocumentPageArtifactViewer",
    "function FileArtifactViewer",
  );
  const docxFallbackSource = sourceBetween(
    embeddedChatSource,
    "async function loadDocxFallbackRender",
    "function DocumentPageArtifactViewer",
  );
  const docxArtifactSource = sourceBetween(
    embeddedChatSource,
    'if (nextCategory === "docx") {',
    "const downloadedUrl = await api.documents.preview(doc.id);",
  );
  assert.match(docxArtifactSource, /api\.documents\.getPages\(/);
  assert.match(docxPageViewerSource, /fetch\(`\/api\/v1\$\{page\.url\}`/);
  assert.match(docxPageViewerSource, /URL\.createObjectURL\(await response\.blob\(\)\)/);
  assert.match(docxPageViewerSource, /new IntersectionObserver/);
  assert.match(docxPageViewerSource, /activeFetches < DOCX_PAGE_FETCH_CONCURRENCY/);
  assert.match(docxArtifactSource, /setDocxPages\(pages\)/);
  assert.match(docxArtifactSource, /abortController\.signal/);
  assert.match(docxArtifactSource, /loadDocxFallbackRender\(/);
  assert.match(docxFallbackSource, /api\.documents\.previewResponse\(documentId, \{ signal \}\)/);
  assert.match(docxFallbackSource, /const blob = await response\.blob\(\)/);
  assert.match(docxFallbackSource, /const buf = await blob\.arrayBuffer\(\)/);
  assert.match(docxFallbackSource, /renderManorDocument\(buf\)/);
  assert.match(embeddedChatSource, /paginateManorDocument\(root, render\)/);
  assert.doesNotMatch(docxFallbackSource, /import\("mammoth"\)/);
  assert.match(docxArtifactSource, /if \(cancelled\) return/);
  assert.doesNotMatch(docxArtifactSource, /api\.documents\.download\(/);
});

test("document editor DOCX preview parses the protected Blob and cancels stale loads", () => {
  const docxEditorSource = sourceBetween(
    docEditorSource,
    "// Load DOCX: download blob",
    "// Load XLSX:",
  );
  assert.match(docxEditorSource, /let cancelled = false/);
  assert.match(docxEditorSource, /const source = await loadOfficeEditSource\(docId, needsLegacyOfficeConversion\)/);
  assert.match(docxEditorSource, /const \{ buffer: buf, sourceSha256 \} = source/);
  assert.match(docxEditorSource, /return \(\) => \{\s*cancelled = true/);
  assert.doesNotMatch(docxEditorSource, /api\.documents\.download\(/);
  assert.doesNotMatch(docxEditorSource, /fetch\(/);
});

test("document editor PPTX parser uses the protected Blob while object preview requests stay independent", () => {
  const pptxEditorSource = sourceBetween(
    docEditorSource,
    "// Load PPTX:",
    "// Track line count",
  );
  assert.match(pptxEditorSource, /const source = await loadOfficeEditSource\(docId, needsLegacyOfficeConversion\)/);
  assert.match(pptxEditorSource, /const \{ buffer: buf, sourceSha256 \} = source/);
  assert.match(pptxEditorSource, /parsePptxForEditor\(buf, \{ isCancelled: \(\) => cancelled \}\)/);
  assert.match(pptxEditorSource, /if \(cancelled\) return/);
  assert.match(pptxEditorSource, /void refreshPptxGraphicPreviews\(docId, nextSlides\)\.then/);
  assert.match(pptxEditorSource, /pptxRenderRequestRef\.current \+= 1;\s*pptxGraphicPreviewRequestRef\.current \+= 1;\s*pptxGraphicPreviewAbortRef\.current\?\.abort\(\);\s*pptxGraphicPreviewAbortRef\.current = null;\s*replacePptxServerUrls\(\[\]\);\s*replacePptxGraphicObjectUrls\(\[\]\)/);
  assert.doesNotMatch(pptxEditorSource, /api\.documents\.download\(/);
  assert.doesNotMatch(pptxEditorSource, /let downloadUrl/);
  assert.doesNotMatch(pptxEditorSource, /fetch\(/);
});

test("document editor serializes PPTX parsing and resets shared parser state", () => {
  const parserSource = sourceBetween(
    docEditorSource,
    "interface PptxEditorParseOptions",
    "/** Convert slides to saveable text",
  );
  assert.match(parserSource, /let pptxEditorParseQueue: Promise<void> = Promise\.resolve\(\)/);
  assert.match(parserSource, /const parseRun = pptxEditorParseQueue\.then\(async \(\) => \{/);
  assert.match(parserSource, /if \(options\.isCancelled\?\.\(\)\) return \[\]/);
  assert.match(parserSource, /return parsePptxForEditorUnlocked\(buf, options\)/);
  assert.match(parserSource, /pptxEditorParseQueue = parseRun\.then\(\(\) => undefined, \(\) => undefined\)/);
  assert.match(parserSource, /EDITOR_SLIDE_W = 12192000;\s*EDITOR_SLIDE_H = 6858000/);
  assert.match(parserSource, /_activeTheme = DEFAULT_SCHEME;\s*_editorMajorFont = "";\s*_editorMinorFont = ""/);
  assert.match(parserSource, /_editorBgFillStyles = \[\];\s*_editorFillStyles = \[\]/);
  assert.match(parserSource, /theme: \{\s*colors: \{ \.\.\._activeTheme \},\s*majorFont: officeCompatibleFontFamily\(_editorMajorFont \|\| undefined\),\s*minorFont: officeCompatibleFontFamily\(_editorMinorFont \|\| undefined\)/);
  assert.match(docEditorSource, /slide\.theme\?\.colors\.tx1/);
  assert.match(docEditorSource, /slide\.theme\?\.minorFont/);
});

test("document editor media insertion consumes protected Blobs directly", () => {
  const slideMediaSource = sourceBetween(
    docEditorSource,
    "const handleMediaInsert = useCallback",
    "let posterFile:",
  );
  assert.match(slideMediaSource, /api\.documents\.downloadBlob\(asset\.document\.id\)/);
  assert.doesNotMatch(slideMediaSource, /api\.documents\.download\(/);
  assert.doesNotMatch(slideMediaSource, /fetch\(/);

  const documentMediaSource = sourceBetween(
    docEditorSource,
    "const handleDocumentMediaInsert = useCallback",
    "const insertMarkdownWikiLink = useCallback",
  );
  assert.equal(
    [...documentMediaSource.matchAll(/api\.documents\.downloadBlob\(asset\.document\.id\)/g)].length,
    3,
  );
  assert.doesNotMatch(documentMediaSource, /api\.documents\.download\(/);
});

test("document editor text decoding also avoids a Safari object URL refetch", () => {
  const textEditorSource = sourceBetween(
    docEditorSource,
    "// Load text-like files from their original bytes",
    "// Load text content",
  );
  assert.match(textEditorSource, /const blob = await api\.documents\.previewBlob\(docId\)/);
  assert.match(textEditorSource, /const buffer = await blob\.arrayBuffer\(\)/);
  assert.match(textEditorSource, /if \(!cancelled && sessionRevision === textSaveSessionRevisionRef\.current\)/);
  assert.doesNotMatch(textEditorSource, /editableBlob|editableResponse|editable-file/);
  assert.doesNotMatch(textEditorSource, /api\.documents\.download\(/);
  assert.doesNotMatch(textEditorSource, /fetch\(/);
});
