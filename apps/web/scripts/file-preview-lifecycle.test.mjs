#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [fileViewerSource, knowledgeSource, embeddedChatSource, docEditorSource] = await Promise.all([
  readFile(new URL("../src/pages/FileViewer.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/Knowledge.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/components/EmbeddedChat.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8"),
]);

test("file viewer URLs are revoked when async loads lose ownership", () => {
  const docxViewerSource = fileViewerSource
    .split("function DocxViewer({")[1]
    .split("// ── XLSX viewer ──")[0];
  assert.match(fileViewerSource, /fileViewerMountedRef\.current = false/);
  assert.match(fileViewerSource, /fetchGenerationRef\.current \+= 1/);
  assert.match(fileViewerSource, /await api\.documents\.download\(currentDocumentId, \{ cache: false \}\)/);
  assert.doesNotMatch(fileViewerSource, /downloadUrl \|\| await api\.documents\.download/);
  assert.match(fileViewerSource, /!isTaskOutputPreview && !doc\?\.current_user_capabilities\?\.includes\("download"\)/);
  assert.match(fileViewerSource, /catch \(err: any\) \{\s*if \(!isCurrentRequest\(\)\) return;\s*let resolvedPreview/);
  assert.match(fileViewerSource, /const rendered = await renderManorDocument\(buf\);\s*if \(cancelled\) return;[\s\S]*?setRender\(safeRender\);\s*setHtml\(safeRender\.html\)/);
  assert.doesNotMatch(fileViewerSource, /import\("mammoth"\)/);
  assert.match(fileViewerSource, /return \(\) => \{\s*cancelled = true;\s*abortController\.abort\(\)/);
  assert.match(fileViewerSource, /className="docx-comment-anchor-bar"[\s\S]*?onSelectCommentId\?\.\(comment\.id\)/);
  assert.match(fileViewerSource, /const serverRenderAbortController = new AbortController\(\)/);
  assert.match(fileViewerSource, /serverRenderAbortController\.abort\(\)/);
  assert.match(docxViewerSource, /\bdocId,/);
  assert.match(docxViewerSource, /api\.documents\.getPages\(\s*docId,/);
  assert.match(docxViewerSource, /!preferSelectablePreview/);
  assert.match(docxViewerSource, /withPreviewTimeout\([\s\S]{0,80}fetch\(`/);
  assert.match(docxViewerSource, /15_000,\s*"Word preview timed out"/);
  assert.match(docxViewerSource, /new IntersectionObserver\(/);
  assert.match(docxViewerSource, /URL\.revokeObjectURL\(url\)/);
  assert.match(docxViewerSource, /renderManorDocument\(buf\)/);
  assert.match(fileViewerSource, /preferSelectablePreview=\{commentsOpen\}/);
  assert.match(knowledgeSource, /if \(cancelled\) \{[\s\S]*?URL\.revokeObjectURL\(url\)/);
  assert.match(knowledgeSource, /\.catch\(\(\) => \{\s*if \(!cancelled\) setError\(true\)/);
  assert.match(knowledgeSource, /let objectUrl: string \| null = null/);
});

test("artifact and editor previews revoke late and partial slide URLs", () => {
  assert.match(embeddedChatSource, /const previewAbortController = new AbortController\(\)/);
  assert.match(embeddedChatSource, /setLoading\(true\);\s*setSlideUrls\(\[\]\);\s*setCanvasUrl\(null\)/);
  assert.match(embeddedChatSource, /Presentation preview timed out/);
  assert.match(embeddedChatSource, /previewAbortController\.abort\(\)/);
  assert.match(embeddedChatSource, /const trackObjectUrl = \(url: string\) => \{\s*if \(cancelled\) URL\.revokeObjectURL\(url\)/);
  assert.match(embeddedChatSource, /const slideResults = await Promise\.allSettled/);
  assert.match(embeddedChatSource, /revokeTrackedObjectUrls\(urls\)/);
  assert.match(embeddedChatSource, /const url = trackObjectUrl\(await api\.documents\.preview\(doc\.id\)\)/);
  assert.match(embeddedChatSource, /return \(\) => \{\s*cancelled = true;\s*previewAbortController\.abort\(\);[\s\S]*?objectUrls\.forEach\(\(url\) => URL\.revokeObjectURL\(url\)\)/);
  assert.match(embeddedChatSource, /const docxPageAbortController = new AbortController\(\)/);
  assert.match(embeddedChatSource, /api\.documents\.getPages\(\s*doc\.id,\s*docxPageAbortController\.signal/);
  const docxManifestSource = embeddedChatSource
    .split('if (nextCategory === "docx") {')[1]
    .split("const render = await loadDocxFallbackRender")[0];
  assert.doesNotMatch(docxManifestSource, /withArtifactPreviewTimeout|Word preview timed out/);
  assert.match(embeddedChatSource, /abortController\.abort\(\);\s*docxPageAbortController\.abort\(\)/);
  assert.match(docEditorSource, /const requestRevision = pptxRenderRequestRef\.current \+ 1/);
  assert.match(docEditorSource, /if \(requestRevision !== pptxRenderRequestRef\.current\) \{\s*createdObjectUrls\.forEach\(\(url\) => URL\.revokeObjectURL\(url\)\)/);
  assert.match(docEditorSource, /catch \{\s*createdObjectUrls\.forEach\(\(url\) => URL\.revokeObjectURL\(url\)\)/);
  assert.match(docEditorSource, /pptxServerObjectUrlsRef\.current\.forEach\(\(url\) => URL\.revokeObjectURL\(url\)\)/);
  assert.match(docEditorSource, /useEffect\(\(\) => \(\) => \{\s*pptxRenderRequestRef\.current \+= 1/);
});
