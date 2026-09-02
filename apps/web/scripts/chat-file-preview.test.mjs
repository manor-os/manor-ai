#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const workspaceSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const css = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const splitterSource = await readFile(
  new URL("../src/components/ui/ResizablePaneGroup.tsx", import.meta.url),
  "utf8",
);

test("chat presentation preview uses the same rendered slide endpoint as FileViewer", () => {
  assert.match(source, /api\.documents\.getSlides\(doc\.id\)/);
});

test("chat presentation preview falls back to a pure PPT canvas instead of nesting FileViewer", () => {
  const presentationCanvas = source.match(
    /function PresentationArtifactCanvas[\s\S]*?function PresentationArtifactViewer/,
  )?.[0];
  const presentationViewer = source.match(
    /function PresentationArtifactViewer[\s\S]*?function FileArtifactViewer/,
  )?.[0];
  assert.ok(presentationCanvas);
  assert.ok(presentationViewer);
  assert.match(source, /new PPTXViewer\(/);
  assert.match(
    presentationCanvas,
    /viewerRef\.current = viewer;[\s\S]*?viewer\.loadFile\(buffer\)/,
  );
  assert.match(presentationCanvas, /renderQueueRef/);
  assert.match(presentationCanvas, /renderGenerationRef/);
  assert.match(presentationViewer, /<PresentationArtifactCanvas/);
  assert.match(
    presentationViewer,
    /\}, \[[\s\S]*?artifact\.body,[\s\S]*?artifact\.data,[\s\S]*?artifact\.href,[\s\S]*?artifact\.id,[\s\S]*?artifact\.meta,[\s\S]*?artifact\.title,[\s\S]*?\]\);/,
  );
  assert.doesNotMatch(presentationViewer, /<iframe/);
});

test("chat artifact output uses the shared persisted splitter with a one-third default", () => {
  assert.match(source, /import ResizablePaneGroup from "\.\/ui\/ResizablePaneGroup"/);
  assert.match(
    source,
    /id: "chat"[\s\S]*?initialSize: 2[\s\S]*?minSize: outputOpen \? 360 : undefined/,
  );
  assert.match(source, /id: "output"[\s\S]*?initialSize: 1[\s\S]*?minSize: 360/);
  assert.match(source, /storageKey="embedded-chat-output-panes"/);
  assert.match(css, /\.embedded-chat-output-panes/);
  assert.doesNotMatch(source, /chat-output-resize-handle/);
  assert.doesNotMatch(source, /key=\{outputOpen \? "output-open" : "output-closed"\}/);
  assert.match(splitterSource, /layout\.paneKey === paneKey \? layout\.sizes : initialSizes/);
});

test("workspace chat artifact output uses the same adjustable right-side third", () => {
  assert.match(workspaceSource, /import ResizablePaneGroup from "\.\/ui\/ResizablePaneGroup"/);
  assert.match(
    workspaceSource,
    /id: "chat"[\s\S]*?initialSize: 2[\s\S]*?minSize: outputOpen \? 420 : undefined/,
  );
  assert.match(
    workspaceSource,
    /id: "output"[\s\S]*?initialSize: 1[\s\S]*?minSize: 360/,
  );
  assert.match(workspaceSource, /storageKey="workspace-chat-output-panes"/);
  assert.match(workspaceSource, /<OutputPanel[\s\S]*?artifact=\{activeOutputArtifact\}/);
  assert.match(workspaceSource, /artifactDedupKey\(selectedArtifact\)/);
  assert.match(
    workspaceSource,
    /if \(!selectedArtifact \|\| !selectedArtifactAnchor\) return selectedArtifact/,
  );
  assert.match(
    workspaceSource,
    /const itemAnchor =[\s\S]*?if \(itemAnchor !== selectedArtifactAnchor\) continue/,
  );
  assert.match(workspaceSource, /onArtifactOpen=\{openWorkspaceArtifact\}/);
  assert.doesNotMatch(workspaceSource, /openWorkspaceArtifactDetail/);
});

test("chat artifact splitter stacks before pane minimum widths would clip", () => {
  assert.match(css, /container: chat-workbench \/ inline-size/);
  const containerRuleIndex = css.indexOf("@container chat-workbench (max-width: 720px)");
  assert.notEqual(containerRuleIndex, -1);
  const outputRuleIndex = css.indexOf(
    "  .embedded-chat-output-panes.resizable-pane-group {",
    containerRuleIndex,
  );
  assert.notEqual(outputRuleIndex, -1);
  assert.ok(outputRuleIndex > containerRuleIndex);
});

test("chat presentation preview revokes partial and late slide object URLs", () => {
  const presentationViewer = source.match(
    /function PresentationArtifactViewer[\s\S]*?function FileArtifactViewer/,
  )?.[0];
  assert.ok(presentationViewer);
  assert.match(presentationViewer, /const trackObjectUrl/);
  assert.match(presentationViewer, /Promise\.allSettled/);
  assert.match(presentationViewer, /revokeTrackedObjectUrls\(urls\)/);
  assert.match(presentationViewer, /if \(cancelled\) URL\.revokeObjectURL\(url\)/);
  assert.match(presentationViewer, /previewAbortController\.abort\(\)/);
});

test("chat CSV preview shares the quote-aware delimited parser", () => {
  assert.match(source, /parseDelimitedText\(content\)\.rows/);
  assert.doesNotMatch(source, /function parseArtifactCSV/);
});

test("chat Word artifacts use faithful page renders with the Manor document fallback", () => {
  const documentPageViewer = source.match(
    /function DocumentPageArtifactViewer[\s\S]*?function FileArtifactViewer/,
  )?.[0];
  const fileViewer = source.match(
    /function FileArtifactViewer[\s\S]*?export function ArtifactViewer/,
  )?.[0];
  assert.ok(documentPageViewer);
  assert.ok(fileViewer);
  assert.match(fileViewer, /api\.documents\.getPages\(/);
  assert.match(fileViewer, /setDocxPages\(pages\)/);
  assert.match(source, /renderManorDocument\(buf\)/);
  assert.match(source, /paginateManorDocument\(root, render\)/);
  assert.doesNotMatch(source, /import\("mammoth"\)/);
  assert.match(
    source,
    /className="chat-output-docx-pages chat-output-docx-pages--native"/,
  );
  assert.match(
    css,
    /\.chat-output-docx-pages--native\s*\{[\s\S]*?align-items: stretch/,
  );
  assert.match(
    css,
    /\.chat-output-docx-pages--native > \.manor-docx-native\s*\{[\s\S]*?align-self: flex-start;[\s\S]*?margin-inline: auto/,
  );
  assert.match(documentPageViewer, /className="chat-output-docx-pages"/);
  assert.match(documentPageViewer, /new IntersectionObserver/);
  assert.match(documentPageViewer, /activeFetches < DOCX_PAGE_FETCH_CONCURRENCY/);
  assert.match(documentPageViewer, /URL\.revokeObjectURL\(url\)/);
  assert.doesNotMatch(documentPageViewer, /Promise\.allSettled/);
  assert.match(css, /\.chat-output-docx-pages\s*\{[\s\S]*?background: var\(--surface-sunken\)/);
  assert.match(css, /\.chat-output-docx-page img\s*\{[\s\S]*?object-fit: contain/);
});

test("chat XLSX preview shares workbook styles, merges, display values, native pictures, and charts", () => {
  assert.match(source, /spreadsheetSheetsFromFile\(XLSX, wb, buf\)/);
  assert.match(source, /spreadsheetCellVisualStyle\(style\)/);
  assert.match(source, /spreadsheetMergeAt\(sheet\.merges/);
  assert.match(source, /sheet\.displayData\[rowIndex\]/);
  assert.match(source, /<SpreadsheetImageLayer[\s\S]*?images=\{sheet\.images\}/);
  assert.match(source, /<SpreadsheetChartPreview charts=\{sheet\.charts\}/);
});

test("document-backed HTML artifacts render the file itself instead of nesting FileViewer", () => {
  const artifactViewer = source.match(
    /export function ArtifactViewer[\s\S]*?export function OutputPanel/,
  )?.[0];
  assert.ok(artifactViewer);
  assert.match(source, /function artifactDocumentId/);
  assert.match(
    source,
    /artifact\.kind === "page"[\s\S]*?Boolean\(artifactDocumentId\(artifact\)\)/,
  );
  assert.match(
    source,
    /category === "html" && content[\s\S]*?chat-output-render-frame--raw[\s\S]*?<IsolatedHtmlPreviewFrame[\s\S]*?preview=\{htmlPreview\}/,
  );
  assert.match(source, /useIsolatedHtmlPreview/);
  assert.doesNotMatch(
    artifactViewer,
    /artifact\.href\?\.startsWith\("\/viewer\/"\)/,
  );
  assert.match(
    artifactViewer,
    /<iframe title=\{artifact\.title\} src=\{artifact\.href\}/,
  );
  assert.doesNotMatch(source, /\bsrcDoc=/);
  assert.doesNotMatch(source, /sandbox="allow-scripts allow-same-origin"/);
});

test("workspace local artifacts preserve a real message anchor for editor return", () => {
  assert.match(
    workspaceSource,
    /const messageAnchorId = `workspace-chat-message-\$\{msg\.id \|\| item\.key\}`/,
  );
  assert.match(workspaceSource, /id=\{messageAnchorId\}/);
  assert.match(
    workspaceSource,
    /onOpen=\{\(artifact\) =>[\s\S]*?openWorkspaceArtifact\(artifact, messageAnchorId\)[\s\S]*?\}/,
  );
});

test("chat artifacts resolve Documents only by a canonical document id", () => {
  const idResolver = source.match(
    /function artifactDocumentId[\s\S]*?function artifactDownloadName/,
  )?.[0];
  const documentResolver = source.match(
    /async function findDocumentForArtifact[\s\S]*?async function downloadArtifact/,
  )?.[0];
  assert.ok(idResolver);
  assert.ok(documentResolver);
  assert.match(idResolver, /artifact\.data\?\.document_id/);
  assert.doesNotMatch(idResolver, /nestedDocument/);
  assert.doesNotMatch(idResolver, /viewerDocumentId/);
  assert.doesNotMatch(idResolver, /artifact\.href/);
  assert.doesNotMatch(idResolver, /data\.id/);
  assert.match(documentResolver, /const documentId = artifactDocumentId\(artifact\)/);
  assert.match(documentResolver, /api\.documents\.get\(documentId\)/);
  assert.doesNotMatch(documentResolver, /api\.documents\.list/);
});
