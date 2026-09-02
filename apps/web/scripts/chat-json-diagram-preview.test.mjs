#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const [
  chatSource,
  diagramCanvasSource,
  diagramViewerSource,
  diagramPreviewSource,
  drawioPreviewSource,
  fileViewerSource,
  docEditorSource,
  sharedDocumentSource,
  filePreviewKindSource,
  readOnlyFilePreviewSource,
  diagramSchemaSource,
  cssSource,
] = await Promise.all([
  readFile(new URL("../src/components/EmbeddedChat.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/components/diagram/DiagramCanvas.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/components/diagram/DiagramArtifactViewer.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/diagram/artifactPreview.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/diagram/drawioPreview.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/FileViewer.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/SharedDocument.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/filePreviewKind.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/components/file-preview/ReadOnlyFilePreview.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/diagram/schema.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/index.css", import.meta.url), "utf8"),
]);

const diagramPreviewBundle = await build({
  loader: { ".png": "dataurl", ".webp": "dataurl", ".svg": "dataurl" },
  stdin: {
    contents: `
      export { createDiagramArtifactPreview } from "../src/lib/diagram/artifactPreview.ts";
      export {
        createDiagramDocumentFromMermaidSource,
        MAX_DIAGRAM_ELEMENTS,
        parseDiagramDocument,
      } from "../src/lib/diagram/schema.ts";
      export { codeLanguageForFile, isCodeLikeFile } from "../src/lib/codeFiles.ts";
      export { isEditableDiagramReference } from "../src/lib/fileReferences.ts";
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  external: ["mermaid"],
  write: false,
  logLevel: "silent",
});
const diagramPreviewModuleUrl = `data:text/javascript;base64,${Buffer.from(
  diagramPreviewBundle.outputFiles[0].text,
).toString("base64")}`;
const {
  createDiagramArtifactPreview,
  createDiagramDocumentFromMermaidSource,
  codeLanguageForFile,
  isCodeLikeFile,
  isEditableDiagramReference,
  MAX_DIAGRAM_ELEMENTS,
  parseDiagramDocument,
} = await import(diagramPreviewModuleUrl);

const renderedPreviewBundle = await build({
  loader: { ".png": "dataurl", ".webp": "dataurl", ".svg": "dataurl" },
  stdin: {
    contents: `
      export {
        createRenderedDiagramArtifactPreview,
        MAX_DIAGRAM_PREVIEW_BYTES,
      } from "../src/lib/diagram/artifactPreview.ts";
      export {
        assertDiagramPreviewFileSize,
        diagramPreviewTextFromFsRead,
        readDiagramPreviewText,
      } from "../src/lib/diagram/previewLimits.ts";
      export { drawioUnsupportedVertexStyle } from "../src/lib/diagram/drawioPreview.ts";
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  plugins: [{
    name: "fake-mermaid-renderer",
    setup(build) {
      build.onResolve({ filter: /^mermaid$/ }, () => ({ path: "mermaid", namespace: "fake-mermaid" }));
      build.onLoad({ filter: /.*/, namespace: "fake-mermaid" }, () => ({
        contents: `
          export default {
            initialize() {},
            async render(_id, source) {
              return { svg: '<svg data-official-mermaid="true"><text>' + source.length + '</text></svg>' };
            },
          };
        `,
        loader: "js",
      }));
    },
  }],
  write: false,
  logLevel: "silent",
});
const renderedPreviewModuleUrl = `data:text/javascript;base64,${Buffer.from(
  renderedPreviewBundle.outputFiles[0].text,
).toString("base64")}`;
const {
  assertDiagramPreviewFileSize,
  createRenderedDiagramArtifactPreview,
  diagramPreviewTextFromFsRead,
  drawioUnsupportedVertexStyle,
  MAX_DIAGRAM_PREVIEW_BYTES,
  readDiagramPreviewText,
} = await import(renderedPreviewModuleUrl);

test("viewer-backed JSON artifacts load their document instead of rendering the viewer route", () => {
  const artifactViewer = chatSource.match(
    /export function ArtifactViewer[\s\S]*?function OutputPanel/,
  )?.[0];
  assert.ok(artifactViewer);
  assert.match(
    artifactViewer,
    /artifact\.kind === "code"[\s\S]*?Boolean\(artifactDocumentId\(artifact\)\)/,
  );
  assert.doesNotMatch(
    artifactViewer,
    /artifact\.kind === "code"[\s\S]*?looksLikeFileReference\(artifact\.title\)/,
  );
  assert.match(chatSource, /category === "json"[\s\S]*?formatJsonArtifactContent\(content\)/);

  const lookupStart = chatSource.indexOf("async function findDocumentForArtifact");
  const lookupEnd = chatSource.indexOf("\nasync function downloadArtifact", lookupStart);
  assert.ok(lookupStart >= 0 && lookupEnd > lookupStart);
  const lookupSource = chatSource.slice(lookupStart, lookupEnd);
  assert.match(
    lookupSource,
    /const lookup = api\.documents\.get\(documentId\)\.catch\(\(\) => null\)/,
  );
  assert.doesNotMatch(
    lookupSource,
    /documents\.list/,
  );
  const editActionSource = chatSource.match(
    /function ArtifactEditAction[\s\S]*?export function ArtifactViewer/,
  )?.[0];
  assert.ok(editActionSource);
  assert.match(
    editActionSource,
    /try \{[\s\S]*?await findDocumentForArtifact\(artifact\)[\s\S]*?\} catch \{[\s\S]*?setDoc\(null\)/,
  );
});

test("artifact download errors are handled and surfaced", () => {
  const downloadStart = chatSource.indexOf("async function downloadArtifact");
  const downloadEnd = chatSource.indexOf("\nfunction withArtifactPreviewTimeout", downloadStart);
  assert.ok(downloadStart >= 0 && downloadEnd > downloadStart);
  const downloadSource = chatSource.slice(downloadStart, downloadEnd);
  assert.match(
    downloadSource,
    /generatedFileFsPath\(artifact\.data\)[\s\S]*?useAuthStore\.getState\(\)\.user\?\.entity_id[\s\S]*?fetchProtectedFsResponse\([\s\S]*?response\.blob\(\)[\s\S]*?URL\.createObjectURL/,
  );
  assert.doesNotMatch(downloadSource, /if \(!directUrl\) return/);

  const summaryCardsSource = chatSource.match(
    /export function ArtifactSummaryCards[\s\S]*?export default function EmbeddedChat/,
  )?.[0];
  assert.ok(summaryCardsSource);
  assert.match(
    summaryCardsSource,
    /await downloadArtifact\(artifact\);[\s\S]*?catch \(error\) \{[\s\S]*?toast\.error\([\s\S]*?artifact_download_failed/,
  );
});

test("diagram JSON artifacts keep diagram identity and render a diagram preview", () => {
  assert.match(chatSource, /function isDiagramArtifactReference/);
  assert.match(chatSource, /endsWith\("\.diagram\.json"\)/);
  assert.match(
    chatSource,
    /function detectArtifactFileCategory[\s\S]*?fileReferenceKind\(doc\.name \|\| "", doc\.mime_type, doc\.file_type\) === "diagram"[\s\S]*?return "diagram"[\s\S]*?return "json"/,
  );
  assert.match(
    chatSource,
    /function artifactKindFromRecord[\s\S]*?diagramReferences\.some\(isDiagramArtifactReference\)[\s\S]*?return "diagram"/,
  );
  assert.match(chatSource, /lazy\(\(\) => import\("\.\/diagram\/DiagramArtifactViewer"\)\)/);
  assert.match(
    chatSource,
    /<LazyDiagramArtifactViewer[\s\S]*?content=\{content\}[\s\S]*?title=\{artifact\.title\}/,
  );
  assert.doesNotMatch(
    chatSource,
    /category === "text" \|\|[\s\S]*?category === "diagram"[\s\S]*?<pre/,
  );
  assert.match(diagramCanvasSource, /export function buildDiagramSvg/);
  assert.match(diagramViewerSource, /createRenderedDiagramArtifactPreview/);
  assert.match(
    diagramViewerSource,
    /createRenderedDiagramArtifactPreview\(content, title, fileType\)/,
  );
  assert.match(diagramPreviewSource, /createDiagramDocumentFromMermaidSource/);
  assert.match(diagramPreviewSource, /buildDiagramSvg\(document, \{ background: false \}\)/);
  assert.match(diagramViewerSource, /className="chat-output-diagram-canvas"/);
  assert.match(diagramSchemaSource, /export function createDiagramDocumentFromMermaidSource/);
  assert.match(
    docEditorSource,
    /function detectMode\(document:[\s\S]*?isEditableDiagramReference\(document\.name, document\.file_type \|\| undefined\)/,
  );
  assert.match(docEditorSource, /detectMode\(doc\)/);
  assert.match(cssSource, /\.chat-output-diagram-canvas[\s\S]*?background-size: 24px 24px/);
  assert.match(cssSource, /\.chat-output-diagram-canvas[\s\S]*?background-color: #ffffff/);
  assert.doesNotMatch(
    cssSource,
    /\.chat-output-diagram-canvas[\s\S]*?background-color: var\(--surface-muted\)/,
  );
  assert.match(
    chatSource,
    /if \(category === "diagram"\) \{[\s\S]*?<LazyDiagramArtifactViewer[\s\S]*?content=\{content\}[\s\S]*?fileType=/,
  );
  assert.doesNotMatch(chatSource, /category === "diagram" && content/);
});

test("explicit diagram identity survives legacy JSON metadata after rename", () => {
  assert.match(
    chatSource,
    /explicitDiagramIdentityOverridesGenericJson\(persistedFileType, explicit\)/,
  );
  assert.match(
    chatSource,
    /file_type: attachment\.fileType,[\s\S]*?type: attachment\.type/,
  );
});

test("blank native diagrams use native initialization instead of Mermaid", async () => {
  for (const title of ["blank.diagram", "blank.diagram.json"]) {
    const preview = await createRenderedDiagramArtifactPreview(" \n\t", title);
    assert.match(preview.svg, /Editable system diagram/);
    assert.doesNotMatch(preview.svg, /data-official-mermaid/);
    assert.equal(preview.error, "");
  }

  const renamedPreview = await createRenderedDiagramArtifactPreview(
    " \n\t",
    "Renamed Canvas.json",
    "diagram.json",
  );
  assert.match(renamedPreview.svg, /Editable system diagram/);
  assert.doesNotMatch(renamedPreview.svg, /data-official-mermaid/);
  assert.equal(renamedPreview.error, "");

  const mermaidPreview = await createRenderedDiagramArtifactPreview("", "blank.mmd");
  assert.match(mermaidPreview.svg, /data-official-mermaid/);
  const drawioPreview = await createRenderedDiagramArtifactPreview("", "blank.drawio");
  assert.equal(drawioPreview.svg, "");
  assert.ok(drawioPreview.error);
});

test("generic diagram identity still detects Mermaid and Draw.io syntax", async () => {
  const mermaidPreview = await createRenderedDiagramArtifactPreview(
    "flowchart LR\n  A --> B",
    "flow.mmd",
    "diagram",
  );
  assert.match(mermaidPreview.svg, /data-official-mermaid/);
  assert.equal(mermaidPreview.error, "");

  const drawioPreview = await createRenderedDiagramArtifactPreview(
    '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>',
    "flow.drawio",
    "diagram",
  );
  const concreteDrawioPreview = await createRenderedDiagramArtifactPreview(
    '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>',
    "flow.drawio",
    "drawio",
  );
  assert.equal(drawioPreview.svg, concreteDrawioPreview.svg);
  assert.equal(drawioPreview.error, concreteDrawioPreview.error);
  assert.doesNotMatch(drawioPreview.error, /JSON is invalid/i);
});

test("the canonical file viewer renders diagram JSON as a diagram", () => {
  assert.match(
    filePreviewKindSource,
    /fileReferenceKind\(file\.name \|\| "", file\.mime_type \|\| file\.mimeType \|\| undefined, file\.file_type \|\| file\.fileType \|\| undefined\) === "diagram"[\s\S]*?return "diagram"[\s\S]*?return "json"/,
  );
  assert.match(
    fileViewerSource,
    /const cat = getFilePreviewKind\(meta\);[\s\S]*?if \(cat === "diagram"\)[\s\S]*?previewResponse\(resolvedDocumentId\)[\s\S]*?readDiagramPreviewText\(response\)/,
  );
  assert.match(
    readOnlyFilePreviewSource,
    /kind === "diagram"[\s\S]*?<LazyDiagramArtifactViewer[\s\S]*?content=\{previewContent\}[\s\S]*?fileType=\{file\.file_type/,
  );
  assert.match(
    sharedDocumentSource,
    /<ReadOnlyFilePreview file=\{data\} source=\{source\} canDownload=\{canDownload\}/,
  );
});

test("FS-backed chat diagrams load bounded text without a Document row", () => {
  assert.match(
    chatSource,
    /if \(!doc\)[\s\S]*?artifact\.kind === "diagram"[\s\S]*?fallbackCategory === "diagram"[\s\S]*?fetchProtectedFsResponse\([\s\S]*?readDiagramPreviewText\(response\)[\s\S]*?api\.fs\.info\([\s\S]*?assertDiagramPreviewFileSize\(info\?\.size\)[\s\S]*?api\.fs\.read\([\s\S]*?diagramPreviewTextFromFsRead\([\s\S]*?setContent\(/,
  );
  const xml = "<mxGraphModel />";
  assert.equal(diagramPreviewTextFromFsRead({
    content: Buffer.from(xml).toString("base64"),
    encoding: "base64",
    size: Buffer.byteLength(xml),
  }), xml);
  assert.throws(
    () => diagramPreviewTextFromFsRead({
      content: "A".repeat(Math.ceil(MAX_DIAGRAM_PREVIEW_BYTES * 4 / 3) + 8),
      encoding: "base64",
    }),
    /too large/i,
  );
  assert.match(
    fileViewerSource,
    /normalizedBase64[\s\S]*?fileSize[\s\S]*?file_size: fileSize/,
  );
  assert.match(
    fileViewerSource,
    /getFilePreviewKind\(previewDoc\) === "diagram"[\s\S]*?diagramPreviewTextFromFsRead\([\s\S]*?setContent\(diagramContent\)/,
  );
  assert.doesNotMatch(
    fileViewerSource,
    /api\.fs\.read\(legacyFsPath\)/,
  );
});

test("file artifact reloads when its structured identity changes", () => {
  const viewerStart = chatSource.indexOf("function FileArtifactViewer");
  const viewerEnd = chatSource.indexOf("\nfunction ArtifactEditAction", viewerStart);
  assert.ok(viewerStart >= 0 && viewerEnd > viewerStart);
  const viewerSource = chatSource.slice(viewerStart, viewerEnd);
  assert.match(viewerSource, /artifact\.data/);
  assert.match(viewerSource, /artifact\.id/);
  assert.match(viewerSource, /artifact\.kind/);
});

test("raw Mermaid diagrams preserve branch topology", () => {
  const source = `flowchart TD
    A((Start)) --> B{Left}
    A --> C[Right]`;
  const document = createDiagramDocumentFromMermaidSource(source, "Branch flow");
  assert.ok(document);
  const nodeIdByText = new Map(
    document.elements
      .filter((element) => element.kind === "shape")
      .map((element) => [element.text, element.id]),
  );
  const links = document.elements
    .filter((element) => element.kind === "connector")
    .map((element) => [element.from.bind?.elementId, element.to.bind?.elementId]);
  assert.deepEqual(links, [
    [nodeIdByText.get("Start"), nodeIdByText.get("Left")],
    [nodeIdByText.get("Start"), nodeIdByText.get("Right")],
  ]);
  assert.equal(document.elements.find((element) => element.text === "Start")?.shape, "ellipse");
  assert.equal(document.elements.find((element) => element.text === "Left")?.shape, "diamond");

  const preview = createDiagramArtifactPreview(source, "branch.mmd");
  assert.equal(preview.error, "");
  assert.match(preview.svg, /<svg/);
});

test("raw Mermaid sources use the official renderer without a dialect allowlist", async () => {
  for (const source of [
    "flowchart LR\nA --> B & C",
    "C4Container\nContainer(api, API, Service)",
    "swimlane-beta\na: First lane",
    "radar-beta\naxis a, b",
  ]) {
    const preview = await createRenderedDiagramArtifactPreview(source, "diagram.diagram");
    assert.equal(preview.error, "");
    assert.match(preview.svg, /data-official-mermaid="true"/);
  }
  assert.match(diagramPreviewSource, /import\("mermaid"\)/);
  assert.match(diagramPreviewSource, /createMermaidArtifactPreview/);
  assert.doesNotMatch(diagramPreviewSource, /function isMermaidSource/);
  assert.match(diagramViewerSource, /createRenderedDiagramArtifactPreview/);
});

test("editable Mermaid conversion preserves fan-out topology", () => {
  const document = createDiagramDocumentFromMermaidSource(
    "flowchart LR\nA --> B & C",
    "Fan out",
  );
  assert.ok(document);
  const nodes = new Map(
    document.elements
      .filter((element) => element.kind === "shape")
      .map((element) => [element.text, element.id]),
  );
  const links = document.elements
    .filter((element) => element.kind === "connector")
    .map((element) => [element.from.bind?.elementId, element.to.bind?.elementId]);
  assert.deepEqual(Array.from(nodes.keys()), ["A", "B", "C"]);
  assert.deepEqual(links, [
    [nodes.get("A"), nodes.get("B")],
    [nodes.get("A"), nodes.get("C")],
  ]);
});

test("editable Mermaid conversion enforces its budget during fan-out expansion", () => {
  const sources = Array.from({ length: 100 }, (_, index) => `A${index}`).join(" & ");
  const targets = Array.from({ length: 100 }, (_, index) => `B${index}`).join(" & ");
  assert.throws(
    () => createDiagramDocumentFromMermaidSource(
      `flowchart TD\n${sources} --> ${targets}`,
      "Bounded fan out",
    ),
    /too many elements/i,
  );
  assert.match(
    diagramSchemaSource,
    /const edgeKeys = new Set<string>\(\)/,
  );
  assert.match(
    diagramSchemaSource,
    /if \(!edgeKeys\.has\(edgeKey\)\) \{[\s\S]*?ensureElementBudget\(1\)[\s\S]*?edgeKeys\.add\(edgeKey\)[\s\S]*?edges\.push/,
  );
  assert.doesNotMatch(
    diagramSchemaSource,
    /edges\.filter\(\(edge, index\) => \([\s\S]*?edges\.findIndex/,
  );
});

test("the real Mermaid viewer rejects fan-out before calling the renderer", async () => {
  const sources = Array.from({ length: 100 }, (_, index) => `A${index}`).join(" & ");
  const targets = Array.from({ length: 100 }, (_, index) => `B${index}`).join(" & ");
  const preview = await createRenderedDiagramArtifactPreview(
    `flowchart TD\n${sources} --> ${targets}`,
    "bounded-fan-out.mmd",
    "mmd",
  );
  assert.equal(preview.svg, "");
  assert.match(preview.error, /too many elements/i);
});

test("editable Mermaid conversion preserves isolated nodes outside subgraphs", () => {
  const document = createDiagramDocumentFromMermaidSource(`flowchart TD
    A --> B
    C[Isolated]
    E
    subgraph GROUP[Stage]
      D[Internal detail]
    end`, "Mixed flow");
  assert.ok(document);
  assert.deepEqual(
    new Set(document.elements
      .filter((element) => element.kind === "shape")
      .map((element) => element.text)),
    new Set(["A", "B", "Isolated", "E", "Stage", "Internal detail"]),
  );
});

test("invalid or over-limit native diagrams never become a blank editable canvas", () => {
  assert.throws(() => parseDiagramDocument("{"), /JSON is invalid/i);
  assert.throws(
    () => parseDiagramDocument(JSON.stringify({ version: "editable_diagram_v1", elements: [] })),
    /not an editable diagram/i,
  );
  assert.throws(
    () => parseDiagramDocument(JSON.stringify({
      version: "editable_diagram_v1",
      id: "over-limit",
      title: "Over limit",
      canvas: { width: 1200, height: 675, unit: "px" },
      elements: Array.from({ length: MAX_DIAGRAM_ELEMENTS + 1 }, (_, index) => ({
        id: `node_${index}`,
        kind: "shape",
        shape: "rect",
        x: index,
        y: 0,
        w: 24,
        h: 24,
      })),
    })),
    /too many elements/i,
  );
  assert.match(
    docEditorSource,
    /const diagramDoc = useMemo[\s\S]*?parseDiagramDocument\(content[\s\S]*?catch \{[\s\S]*?return null/,
  );
  assert.match(
    docEditorSource,
    /mode === "diagram" \?[\s\S]*?diagramDoc \?[\s\S]*?<DiagramCanvas[\s\S]*?<EmptyState/,
  );
  assert.match(
    docEditorSource,
    /title=\{t\("page\.doc_editor\.diagram_cannot_be_edited"\)\}[\s\S]*?description=\{t\("page\.doc_editor\.diagram_invalid_description"\)\}/,
  );
});

test("Mermaid edge labels do not become nodes and undirected links stay undirected", () => {
  const document = createDiagramDocumentFromMermaidSource(`flowchart TD
    A -- Yes --> B
    A -- No --> C
    D --- E`, "Decision flow");
  assert.ok(document);
  const shapes = document.elements.filter((element) => element.kind === "shape");
  assert.deepEqual(new Set(shapes.map((element) => element.text)), new Set(["A", "B", "C", "D", "E"]));
  const connectors = document.elements.filter((element) => element.kind === "connector");
  assert.deepEqual(connectors.map((element) => [element.label, element.arrowEnd]), [
    ["Yes", true],
    ["No", true],
    [undefined, false],
  ]);
});

test("Mermaid endpoint markers remain bidirectional, circular, or crossed", () => {
  const document = createDiagramDocumentFromMermaidSource(`flowchart LR
    A <--> B
    C o--o D
    E x--x F
    G --o H`, "Marker flow");
  assert.ok(document);
  const connectors = document.elements.filter((element) => element.kind === "connector");
  assert.deepEqual(
    connectors.map((connector) => [connector.markerStart, connector.markerEnd]),
    [
      ["arrow", "arrow"],
      ["circle", "circle"],
      ["cross", "cross"],
      [undefined, "circle"],
    ],
  );
  assert.deepEqual(
    connectors.map((connector) => [connector.arrowStart, connector.arrowEnd]),
    [
      [true, true],
      [true, true],
      [true, true],
      [false, true],
    ],
  );
  const preview = createDiagramArtifactPreview(
    "flowchart LR\nA <--> B\nC o--o D\nE x--x F",
    "markers.mmd",
  );
  assert.equal(preview.error, "");
  assert.match(preview.svg, /marker-start="url\(#diagram-arrow-start\)"/);
  assert.match(preview.svg, /id="diagram-arrow-start"[^>]+orient="auto"/);
  assert.match(preview.svg, /marker-end="url\(#diagram-circle-end\)"/);
  assert.match(preview.svg, /marker-end="url\(#diagram-cross-end\)"/);
  assert.match(diagramCanvasSource, /<Field label="Start marker">[\s\S]*?<option value="circle">Circle<\/option>[\s\S]*?<option value="cross">Cross<\/option>/);
  assert.match(diagramCanvasSource, /<Field label="End marker">[\s\S]*?<option value="circle">Circle<\/option>[\s\S]*?<option value="cross">Cross<\/option>/);
  assert.doesNotMatch(diagramCanvasSource, /Arrow start[\s\S]*?type="checkbox"/);
});

test("single-line Mermaid keeps punctuation and respects horizontal direction", () => {
  const source = `flowchart LR; A["Call API (v2); now"] --> B[Done]; B --> C["Users'"]; C --> D[Owner's queue]`;
  const document = createDiagramDocumentFromMermaidSource(source, "Horizontal flow");
  assert.ok(document);
  const nodes = new Map(
    document.elements
      .filter((element) => element.kind === "shape")
      .map((element) => [element.text, element]),
  );
  assert.deepEqual(Array.from(nodes.keys()), ["Call API (v2); now", "Done", "Users'", "Owner's queue"]);
  assert.ok(nodes.get("Call API (v2); now").x < nodes.get("Done").x);
  assert.ok(nodes.get("Done").x < nodes.get("Users'").x);
  assert.ok(nodes.get("Users'").x < nodes.get("Owner's queue").x);
  assert.equal(nodes.get("Call API (v2); now").y, nodes.get("Done").y);
  assert.equal(nodes.get("Done").y, nodes.get("Users'").y);
  assert.equal(nodes.get("Users'").y, nodes.get("Owner's queue").y);
});

test("Mermaid labels decode named and numeric HTML entities consistently", () => {
  const document = createDiagramDocumentFromMermaidSource(
    "flowchart LR; A[Space&nbsp;here] --> B[Dash&#x2014;here]",
    "Entities",
  );
  assert.ok(document);
  assert.deepEqual(
    document.elements.filter((element) => element.kind === "shape").map((element) => element.text),
    ["Space here", "Dash—here"],
  );
});

test("Mermaid reverse directions invert the flow axis and connector anchors", () => {
  const bottomUp = createDiagramDocumentFromMermaidSource("flowchart BT; A --> B", "Bottom up");
  const rightToLeft = createDiagramDocumentFromMermaidSource("flowchart RL; A --> B", "Right to left");
  assert.ok(bottomUp);
  assert.ok(rightToLeft);

  const btNodes = bottomUp.elements.filter((element) => element.kind === "shape");
  const btConnector = bottomUp.elements.find((element) => element.kind === "connector");
  assert.ok(btNodes[0].y > btNodes[1].y);
  assert.equal(btConnector?.from.bind?.anchor, "top");
  assert.equal(btConnector?.to.bind?.anchor, "bottom");

  const rlNodes = rightToLeft.elements.filter((element) => element.kind === "shape");
  const rlConnector = rightToLeft.elements.find((element) => element.kind === "connector");
  assert.ok(rlNodes[0].x > rlNodes[1].x);
  assert.equal(rlConnector?.from.bind?.anchor, "left");
  assert.equal(rlConnector?.to.bind?.anchor, "right");
});

test("Mermaid DAG layout uses the longest forward path", () => {
  const document = createDiagramDocumentFromMermaidSource(`flowchart TD
    A --> D
    A --> B
    B --> C
    C --> D
    D --> B`, "DAG flow");
  assert.ok(document);
  const nodes = new Map(
    document.elements
      .filter((element) => element.kind === "shape")
      .map((element) => [element.text, element]),
  );
  assert.ok(nodes.get("A").y < nodes.get("B").y);
  assert.ok(nodes.get("B").y < nodes.get("C").y);
  assert.ok(nodes.get("C").y < nodes.get("D").y);
  const cToD = document.elements.find((element) => (
    element.kind === "connector"
    && element.from.bind?.elementId === nodes.get("C").id
    && element.to.bind?.elementId === nodes.get("D").id
  ));
  assert.equal(cToD?.routing, "straight");
  const dToB = document.elements.find((element) => (
    element.kind === "connector"
    && element.from.bind?.elementId === nodes.get("D").id
    && element.to.bind?.elementId === nodes.get("B").id
  ));
  assert.equal(dToB?.routing, "curve");
});

test("Mermaid preview wraps long node labels for the SVG viewer", () => {
  const document = createDiagramDocumentFromMermaidSource(
    "flowchart TD\nA[Portal leads from multiple listing services] --> B[Workspace]",
    "Readable flow",
  );
  assert.ok(document);
  const longNode = document.elements.find(
    (element) => element.kind === "shape" && element.text.includes("Portal leads"),
  );
  assert.match(longNode?.text || "", /\n/);

  const preview = createDiagramArtifactPreview(
    "flowchart TD\nA[Portal leads from multiple listing services] --> B[Workspace]",
    "readable.mmd",
  );
  assert.equal(preview.error, "");
  assert.match(preview.svg, /<tspan[^>]+>Portal leads from<\/tspan>/);
});

test("Mermaid preview wraps CJK labels and long unbroken tokens", () => {
  const source = "flowchart TD\nA[用户输入房源目标客户和品牌资料Workspace生成短视频脚本] --> B[https://example.com/a/very/long/path]";
  const document = createDiagramDocumentFromMermaidSource(source, "中文流程");
  assert.ok(document);
  const labels = document.elements
    .filter((element) => element.kind === "shape")
    .map((element) => element.text);
  assert.ok(labels.every((label) => label.includes("\n")));
  const preview = createDiagramArtifactPreview(source, "readable.mermaid");
  assert.equal(preview.error, "");
  assert.match(preview.svg, /<tspan[^>]+>用户输入房源目标客户[^<]*<\/tspan><tspan/);
});

test("Mermaid and draw.io sources use the safe code editor path", () => {
  assert.equal(isCodeLikeFile("flow.mmd"), true);
  assert.equal(isCodeLikeFile("flow.mermaid"), true);
  assert.equal(isCodeLikeFile("flow.drawio"), true);
  assert.equal(isCodeLikeFile({ name: "styles.css", file_type: ".css" }), true);
  assert.equal(codeLanguageForFile("flow.mermaid"), "mermaid");
  assert.equal(codeLanguageForFile("flow.drawio"), "markup");
  assert.equal(isCodeLikeFile({ name: "Renamed flow.bin", file_type: "mmd" }), true);
  assert.equal(codeLanguageForFile({ name: "Renamed flow.bin", file_type: "mmd" }), "mermaid");
  assert.equal(codeLanguageForFile({ name: "Renamed flow.json", file_type: "mmd" }), "mermaid");
  assert.equal(codeLanguageForFile({ name: "Renamed flow.mmd", file_type: "drawio" }), "markup");
  assert.equal(isCodeLikeFile({ name: "Renamed deck.mmd", file_type: "pptx" }), false);
  assert.equal(codeLanguageForFile({ name: "Renamed deck.mmd", file_type: "pptx" }), "text");
  assert.equal(isEditableDiagramReference("flow.mmd", "mmd"), false);
  assert.equal(isEditableDiagramReference("flow.mermaid", "mermaid"), false);
  assert.equal(isEditableDiagramReference("flow.drawio", "drawio"), false);
  assert.equal(isEditableDiagramReference("flow.diagram.json", "diagram.json"), true);
  assert.equal(isEditableDiagramReference("Renamed Canvas.bin", "diagram.json"), true);
  assert.equal(isEditableDiagramReference("Renamed deck.diagram", "pptx"), false);
  assert.match(
    docEditorSource,
    /isEditableDiagramReference\(document\.name, document\.file_type \|\| undefined\)[\s\S]*?isCodeLikeFile\(document\)/,
  );
  assert.match(
    docEditorSource,
    /const activeCodeReference = activeCodeIsMain && doc[\s\S]*?file_type: doc\.file_type[\s\S]*?codeLanguageForFile\(activeCodeReference\)/,
  );
  assert.match(fileViewerSource, /isEditable\(category, doc\)/);
  assert.match(
    fileViewerSource,
    /function isEditable[\s\S]*?isEditableDiagramReference\(doc\.name, doc\.file_type\)[\s\S]*?isCodeLikeFile\(doc\)/,
  );
});

test("persisted diagram identity wins over conflicting renamed extensions", () => {
  const chatDetector = chatSource.slice(
    chatSource.indexOf("function detectArtifactFileCategory"),
    chatSource.indexOf("const EDITABLE_ARTIFACT_CATEGORIES"),
  );
  const filePreviewKindDetector = filePreviewKindSource;
  const editorDetector = docEditorSource.slice(
    docEditorSource.indexOf("function detectMode"),
    docEditorSource.indexOf("function isOfficeDoc"),
  );

  assert.ok(chatDetector.indexOf("fileReferenceKind") < chatDetector.indexOf("[\"md\", \"markdown\"]"));
  assert.ok(filePreviewKindDetector.indexOf("fileReferenceKind") < filePreviewKindDetector.indexOf("[\"docx\", \"doc\", \"wps\"]"));
  assert.match(
    editorDetector,
    /fileReferenceKind\([\s\S]*?=== "diagram"[\s\S]*?isEditableDiagramReference[\s\S]*?return "code"[\s\S]*?const ext/,
  );
  assert.match(
    editorDetector,
    /const documentKind = fileReferenceKind\([\s\S]*?documentKind === "presentation"[\s\S]*?return "presentation"/,
  );

  const artifactClassifier = chatSource.slice(
    chatSource.indexOf("function artifactKindFromRecord"),
    chatSource.indexOf("function isTerminalArtifactRecord"),
  );
  assert.ok(artifactClassifier.indexOf("fileReferenceKind") >= 0);
  assert.ok(
    artifactClassifier.indexOf("fileReferenceKind")
      < artifactClassifier.indexOf("diagramReferences.some"),
  );
});

test("legacy diagram records classify from their filename before a viewer route", () => {
  const artifactClassifier = chatSource.slice(
    chatSource.indexOf("function artifactKindFromRecord"),
    chatSource.indexOf("function isTerminalArtifactRecord"),
  );
  assert.match(
    artifactClassifier,
    /const diagramReferences = \[\s*record\.name,\s*record\.filename,[\s\S]*?reference,\s*\]/,
  );
});

test("missing diagram sources fail instead of rendering a default canvas", () => {
  const viewerSource = chatSource.slice(
    chatSource.indexOf("function FileArtifactViewer"),
    chatSource.indexOf("function ArtifactEditAction"),
  );
  assert.match(
    viewerSource,
    /let diagramSourceLoaded = false;[\s\S]*?diagramSourceLoaded = true;[\s\S]*?if \(!diagramSourceLoaded\) \{[\s\S]*?throw new Error\("Artifact preview source is unavailable"\)/,
  );
  assert.ok(
    viewerSource.indexOf("if (!diagramSourceLoaded)")
      < viewerSource.indexOf('setCategory("diagram")'),
  );
});

test("chat and canonical viewers subtype from persisted file_type before the renamed filename", () => {
  const chatDetector = chatSource.slice(
    chatSource.indexOf("function detectArtifactFileCategory"),
    chatSource.indexOf("const EDITABLE_ARTIFACT_CATEGORIES"),
  );
  for (const detector of [chatDetector, filePreviewKindSource]) {
    assert.match(
      detector,
      /const persistedKind = fileType\s*\? fileReferenceKind\("", undefined, fileType\)\s*:\s*"file";/,
    );
    assert.match(
      detector,
      /const ext = persistedKind !== "file"\s*\? fileType\s*:/,
    );
    assert.match(
      detector,
      /const mime = persistedKind !== "file"\s*\? ""\s*:/,
    );
  }
});

test("editable diagrams are never rebuilt from a stale Mermaid prompt", () => {
  const prompt = `flowchart TD
    A((Start)) --> B{Left}
    A --> C[Right]`;
  const parsed = parseDiagramDocument(JSON.stringify({
    version: "editable_diagram_v1",
    id: "edited",
    title: "Flowchart sales plan",
    prompt,
    canvas: { width: 800, height: 600, unit: "px" },
    elements: [
      {
        id: "edited-node",
        kind: "shape",
        shape: "roundRect",
        x: 40,
        y: 60,
        w: 180,
        h: 80,
        text: "User edit",
      },
      {
        id: "legitimate-label",
        kind: "shape",
        shape: "rect",
        x: 280,
        y: 60,
        w: 180,
        h: 80,
        text: "subgraph",
      },
    ],
  }), "Branch flow");
  const shapeTexts = parsed.elements
    .filter((element) => element.kind === "shape")
    .map((element) => element.text);
  assert.equal(parsed.id, "edited");
  assert.deepEqual(shapeTexts, ["User edit", "subgraph"]);
});

test("draw.io renders mxGraph content and shared links reuse diagram rendering", () => {
  assert.match(diagramPreviewSource, /createDiagramDocumentsFromDrawioSource/);
  assert.match(drawioPreviewSource, /querySelectorAll\("mxCell"\)/);
  assert.match(drawioPreviewSource, /new Inflate\(\{ raw: true/);
  assert.doesNotMatch(diagramViewerSource, /<pre/);
  assert.match(sharedDocumentSource, /<ReadOnlyFilePreview file=\{data\} source=\{source\} canDownload=\{canDownload\}/);
  assert.match(readOnlyFilePreviewSource, /kind === "diagram"[\s\S]*?<LazyDiagramArtifactViewer/);
});

test("draw.io previews expose every page and the viewer provides page controls", () => {
  assert.match(drawioPreviewSource, /querySelectorAll\("diagram"\)/);
  assert.match(drawioPreviewSource, /createDiagramDocumentsFromDrawioSource/);
  assert.match(diagramPreviewSource, /const pages = documents\.map/);
  assert.match(diagramViewerSource, /preview\.pages/);
  assert.match(diagramViewerSource, /chat-output-diagram-navigation/);
  assert.match(diagramViewerSource, /previous_diagram_page/);
  assert.match(diagramViewerSource, /next_diagram_page/);
});

test("draw.io detection accepts an XML declaration for diagram-classified XML", async () => {
  const preview = await createRenderedDiagramArtifactPreview(
    '<?xml version="1.0"?><mxfile><diagram name="Page 1"><mxGraphModel /></diagram></mxfile>',
    "diagram.xml",
  );
  assert.equal(preview.svg, "");
  assert.doesNotMatch(preview.error, /Mermaid/i);
});

test("draw.io blank pages remain previewable without hiding valid pages", () => {
  assert.doesNotMatch(drawioPreviewSource, /Draw\.io diagram has no visible shapes/);
  assert.match(drawioPreviewSource, /const visibleElements = elements\.filter/);
  assert.match(drawioPreviewSource, /groups: elementIdByCellId\.size \?/);
});

test("draw.io rejects features that the internal renderer cannot preserve", () => {
  assert.equal(drawioUnsupportedVertexStyle("shape=image;image=data:image/png;base64,AA"), "embedded images");
  assert.equal(drawioUnsupportedVertexStyle("swimlane=1"), "swimlanes");
  assert.equal(drawioUnsupportedVertexStyle("shape=mxgraph.aws4.lambda_function"), 'shape "mxgraph.aws4.lambda_function"');
  assert.equal(drawioUnsupportedVertexStyle("rounded=1;whiteSpace=wrap;html=1"), null);
  assert.match(drawioPreviewSource, /does not support connector waypoints/);
  assert.match(drawioPreviewSource, /drawioMarker\(style\.endArrow, "arrow"\)/);
});

test("draw.io group cells position children without rendering container shapes", () => {
  assert.match(drawioPreviewSource, /const isGroup = style\.group === "1"/);
  assert.match(
    drawioPreviewSource,
    /Array\.from\(cellsById\.values\(\)\)\.filter\(\(cell\) => !cell\.isGroup\)/,
  );
});

test("draw.io applies inherited visibility and bounds structural complexity", () => {
  assert.match(drawioPreviewSource, /function drawioCellIsVisible/);
  assert.match(drawioPreviewSource, /visibilityByCellId/);
  assert.match(
    drawioPreviewSource,
    /drawioCellAttribute\(cell, "vertex"\) === "1"[\s\S]*?drawioCellIsVisible\(cell, cellElementsById, visibilityByCellId\)/,
  );
  assert.match(
    drawioPreviewSource,
    /drawioCellAttribute\(cell, "edge"\) !== "1"[\s\S]*?drawioCellIsVisible\(cell, cellElementsById, visibilityByCellId\)/,
  );
  assert.match(drawioPreviewSource, /function drawioCellWrapper/);
  assert.match(drawioPreviewSource, /wrapper\.getAttribute\("label"\)/);
  assert.match(drawioPreviewSource, /MAX_DRAWIO_PAGES/);
  assert.match(drawioPreviewSource, /MAX_DRAWIO_CELLS/);
  assert.match(drawioPreviewSource, /diagrams\.length > MAX_DRAWIO_PAGES/);
  assert.match(drawioPreviewSource, /budget\.cellCount > MAX_DRAWIO_CELLS/);
});

test("compressed draw.io input has encoded and inflated size limits", () => {
  assert.match(drawioPreviewSource, /MAX_DRAWIO_COMPRESSED_BYTES/);
  assert.match(drawioPreviewSource, /MAX_DRAWIO_INFLATED_BYTES/);
  assert.match(drawioPreviewSource, /new Inflate\(\{ raw: true/);
  assert.match(drawioPreviewSource, /inflatedBytes > MAX_DRAWIO_INFLATED_BYTES/);
});

test("diagram preview rejects oversized text before parsing or rendering", async () => {
  const preview = await createRenderedDiagramArtifactPreview(
    "x".repeat(MAX_DIAGRAM_PREVIEW_BYTES + 1),
    "oversized.mmd",
  );
  assert.equal(preview.svg, "");
  assert.match(preview.error, /too large/i);
  const multibytePreview = await createRenderedDiagramArtifactPreview(
    "图".repeat(Math.ceil(MAX_DIAGRAM_PREVIEW_BYTES / 2)),
    "oversized.mmd",
  );
  assert.equal(multibytePreview.svg, "");
  assert.match(multibytePreview.error, /too large/i);
  assert.throws(
    () => assertDiagramPreviewFileSize(MAX_DIAGRAM_PREVIEW_BYTES + 1),
    /too large/i,
  );
  const oversizedResponse = new Response("x", {
    headers: { "content-length": String(MAX_DIAGRAM_PREVIEW_BYTES + 1) },
  });
  await assert.rejects(readDiagramPreviewText(oversizedResponse), /too large/i);
  let streamCancelled = false;
  const chunkedOversizedResponse = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(MAX_DIAGRAM_PREVIEW_BYTES));
      controller.enqueue(new Uint8Array(1));
    },
    cancel() {
      streamCancelled = true;
    },
  }));
  await assert.rejects(readDiagramPreviewText(chunkedOversizedResponse), /too large/i);
  assert.equal(streamCancelled, true);
  assert.match(chatSource, /previewResponse\(doc\.id[\s\S]*?readDiagramPreviewText\(response\)/);
  assert.match(fileViewerSource, /previewResponse\(resolvedDocumentId\)[\s\S]*?readDiagramPreviewText\(response\)/);
  assert.match(readOnlyFilePreviewSource, /assertDiagramPreviewFileSize\(file\.file_size\)/);
  assert.doesNotMatch(sharedDocumentSource, /diagram\/artifactPreview/);
});

test("native diagram previews bound element count and index connector endpoints", () => {
  const elements = Array.from({ length: MAX_DIAGRAM_ELEMENTS + 1 }, (_, index) => ({
    id: `node_${index}`,
    kind: "shape",
    shape: "rect",
    x: index,
    y: 0,
    w: 24,
    h: 24,
  }));
  const preview = createDiagramArtifactPreview(JSON.stringify({
    version: "editable_diagram_v1",
    id: "oversized",
    title: "Oversized",
    canvas: { width: 1200, height: 675, unit: "px" },
    elements,
  }), "oversized.diagram.json");
  assert.equal(preview.svg, "");
  assert.match(preview.error, /too many elements/i);
  assert.match(diagramCanvasSource, /function diagramElementBoundsById/);
  assert.match(diagramCanvasSource, /connectorToSvg\([^\n]+elementBoundsById/);
});

test("malformed diagram elements are rejected before the editor renders them", () => {
  const malformedConnector = JSON.stringify({
    version: "editable_diagram_v1",
    id: "broken",
    title: "Broken diagram",
    canvas: { width: 800, height: 600, unit: "px" },
    elements: [{ id: "broken-connector", kind: "connector" }],
  });
  const malformedText = JSON.stringify({
    version: "editable_diagram_v1",
    id: "broken-text",
    title: "Broken text",
    canvas: { width: 800, height: 600, unit: "px" },
    elements: [{
      id: "broken-shape",
      kind: "shape",
      shape: "rect",
      x: 0,
      y: 0,
      w: 100,
      h: 60,
      text: { bad: true },
    }],
  });

  assert.throws(
    () => parseDiagramDocument(malformedConnector, "Broken"),
    /editable diagram document/i,
  );
  assert.throws(
    () => parseDiagramDocument(malformedText, "Broken"),
    /editable diagram document/i,
  );
  const preview = createDiagramArtifactPreview(malformedConnector, "broken.diagram.json");
  assert.equal(preview.svg, "");
  assert.ok(preview.error);
  assert.match(docEditorSource, /try \{[\s\S]*?parseDiagramDocument\(content[\s\S]*?catch \(error\)/);
});

test("diagram documents reject duplicate ids and dangling connector bindings", () => {
  const baseDocument = {
    version: "editable_diagram_v1",
    canvas: { width: 800, height: 600, unit: "px" },
    theme: { fontFamily: "Inter", palette: {} },
  };
  const shape = {
    id: "node-a",
    kind: "shape",
    shape: "rect",
    x: 40,
    y: 40,
    w: 160,
    h: 80,
    text: "A",
  };

  assert.throws(
    () => parseDiagramDocument(JSON.stringify({
      ...baseDocument,
      elements: [shape, { ...shape, text: "Duplicate" }],
    })),
    /editable diagram document/i,
  );
  assert.throws(
    () => parseDiagramDocument(JSON.stringify({
      ...baseDocument,
      elements: [
        shape,
        {
          id: "edge",
          kind: "connector",
          from: { bind: { elementId: "missing", anchor: "right" } },
          to: { bind: { elementId: "node-a", anchor: "left" } },
        },
      ],
    })),
    /editable diagram document/i,
  );

  const normalized = parseDiagramDocument(JSON.stringify({
    ...baseDocument,
    elements: [{ ...shape, id: undefined }],
  }));
  assert.ok(normalized.elements[0].id);
});

test("wide native diagram previews keep every element inside the SVG viewBox", () => {
  const preview = createDiagramArtifactPreview(JSON.stringify({
    version: "editable_diagram_v1",
    canvas: { width: 20_000, height: 500, unit: "px" },
    theme: { fontFamily: "Inter", palette: {} },
    elements: [
      { id: "left", kind: "shape", shape: "rect", x: 0, y: 0, w: 100, h: 50, text: "Left" },
      { id: "right", kind: "shape", shape: "rect", x: 15_000, y: 0, w: 100, h: 50, text: "Right" },
    ],
  }), "wide.diagram.json");
  assert.equal(preview.error, "");
  const svgTag = preview.svg.match(/^<svg[^>]+>/)?.[0] || "";
  const viewBox = svgTag.match(/viewBox="([^"]+)"/)?.[1].split(/\s+/).map(Number) || [];
  const intrinsicWidth = Number(svgTag.match(/\bwidth="([^"]+)"/)?.[1]);
  assert.ok(viewBox[2] >= 15_292, `unexpected viewBox: ${viewBox.join(" ")}`);
  assert.ok(intrinsicWidth <= 10_000, `unexpected intrinsic width: ${intrinsicWidth}`);
  assert.match(preview.svg, /x="15000"/);
});
