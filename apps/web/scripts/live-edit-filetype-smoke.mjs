#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { build } from "esbuild";

const entryPoint = `
  export {
    applyEditorLivePatch,
    buildEditorLiveEditFallbackContent,
    buildEditorLiveEditRequest,
    extractEditorLivePatchPayloads,
    stripEditorLiveEditBlocks,
  } from "../src/lib/editorLiveChat.ts";
`;

const bundled = await build({
  stdin: {
    contents: entryPoint,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;

const {
  applyEditorLivePatch,
  buildEditorLiveEditFallbackContent,
  buildEditorLiveEditRequest,
  extractEditorLivePatchPayloads,
  stripEditorLiveEditBlocks,
} = await import(moduleUrl);

function patchReplace(current, find, replace) {
  return JSON.stringify([{ op: "replace", find, replace }]);
}

function assertValidJson(value, label) {
  assert.doesNotThrow(() => JSON.parse(value), `${label} should remain valid JSON`);
}

function assertPatchCase(testCase) {
  const patch = patchReplace(testCase.content, testCase.find, testCase.replace);
  const result = applyEditorLivePatch(testCase.content, patch);
  assert.deepEqual(result.failed, [], `${testCase.label} patch should apply`);
  assert.equal(result.applied, 1, `${testCase.label} patch count`);
  assert.match(result.content, testCase.expected, `${testCase.label} content changed`);
  if (testCase.validateJson) assertValidJson(result.content, testCase.label);

  const wrapped = `intro<manor-live-patch>${patch}</manor-live-patch>done`;
  assert.equal(extractEditorLivePatchPayloads(wrapped).length, 1, `${testCase.label} extracts patch`);
  assert.equal(stripEditorLiveEditBlocks(wrapped), "introdone", `${testCase.label} strips patch from chat`);

  const prompt = buildEditorLiveEditRequest(
    testCase.detail,
    testCase.request,
    testCase.content,
  );
  assert.equal(prompt, testCase.request.trim(), `${testCase.label} sends only user request`);
  assert.doesNotMatch(prompt, /<manor-current-document>/, `${testCase.label} omits current document`);
  assert.doesNotMatch(
    prompt,
    /Runtime will enforce patch-only file-editor output rules/,
    `${testCase.label} omits frontend runtime guidance`,
  );
  assert.doesNotMatch(prompt, /<manor-live-patch>/, `${testCase.label} omits frontend patch protocol`);
  assert.doesNotMatch(prompt, /Image-generation protocol/, `${testCase.label} omits frontend image protocol`);
  if (testCase.detail.supportsImageGeneration) {
    assert.doesNotMatch(prompt, /hidden current-image attachment/, `${testCase.label} omits image attachment hint`);
  }
}

function assertFallbackCase(testCase) {
  const next = buildEditorLiveEditFallbackContent(
    testCase.detail,
    testCase.request,
    testCase.content,
  );
  assert.equal(typeof next, "string", `${testCase.label} fallback should produce content`);
  assert.notEqual(next, testCase.content, `${testCase.label} fallback should change content`);
  assert.match(next, testCase.expected, `${testCase.label} fallback content`);
  if (testCase.validateJson) assertValidJson(next, testCase.label);
}

function assertStrictPatchProtocolSchema() {
  const current = [
    "| # | 名称 | 类型 | 金额 |",
    "|---|---|---|---|",
    "| 1 | Vanguard 401k | 401k | $106,000 |",
    "| 2 | Coinbase | 股票 | $100,000 |",
    "| 3 | Fidelity | 股票 | $58,000 |",
    "| 4 | Webull | 股票 | $60,000 |",
    "| 5 | Robinhood | 股票 | $190,000 |",
    "",
    "| 分类 | 金额 |",
    "|---|---|",
    "| 401k 合计 | $106,000 |",
    "| 股票/投资合计 | $408,000 |",
    "| **总计** | **$514,000** |",
    "",
  ].join("\n");
  const patch = JSON.stringify([
    {
      op: "insert_after",
      find: "| 5 | Robinhood | 股票 | $190,000 |",
      text: "\n| 6 | Mini | 车辆 | $8,000 |\n| 7 | BMW | 车辆 | $20,000 |",
    },
    {
      op: "replace",
      find: "| 401k 合计 | $106,000 |\n| 股票/投资合计 | $408,000 |\n| **总计** | **$514,000** |",
      replace: "| 401k 合计 | $106,000 |\n| 股票/投资合计 | $408,000 |\n| 车辆合计 | $28,000 |\n| **总计** | **$542,000** |",
    },
  ]);
  const wrapped = `已更新资产表：\n<manor-live-patch>${patch}</manor-live-patch>\n完成`;
  const payloads = extractEditorLivePatchPayloads(wrapped);
  assert.equal(payloads.length, 1, "canonical manor-live-patch tag extracts");
  assert.equal(stripEditorLiveEditBlocks(wrapped), "已更新资产表：\n\n完成", "canonical patch tag is hidden from chat");
  const result = applyEditorLivePatch(current, payloads[0]);
  assert.deepEqual(result.failed, [], "canonical patch schema should apply");
  assert.equal(result.applied, 2, "canonical patch applies both operations");
  assert.match(result.content, /\| 6 \| Mini \| 车辆 \| \$8,000 \|/);
  assert.match(result.content, /\| 7 \| BMW \| 车辆 \| \$20,000 \|/);
  assert.match(result.content, /\| 车辆合计 \| \$28,000 \|/);
  assert.match(result.content, /\| \*\*总计\*\* \| \*\*\$542,000\*\* \|/);

  const nonCanonicalWrapped = `已更新资产表：\n<manor live patch>${patch}</manor live patch>\n完成`;
  assert.equal(
    extractEditorLivePatchPayloads(nonCanonicalWrapped).length,
    0,
    "space-separated patch tag is not part of the schema",
  );

  const invalidPatch = JSON.stringify([
    {
      op: "insert after",
      find: "| 5 | Robinhood | 股票 | $190,000 |",
      replace: "| 6 | Mini | 车辆 | $8,000 |",
    },
  ]);
  const invalidResult = applyEditorLivePatch(current, invalidPatch);
  assert.equal(invalidResult.applied, 0, "human-readable op names are rejected");
  assert.match(invalidResult.failed[0]?.reason || "", /Unsupported patch operation/);
}

const diagramJson = JSON.stringify({
  version: "editable_diagram_v1",
  id: "diagram-1",
  title: "Old title",
  canvas: { width: 1200, height: 800, unit: "px", originX: 0, originY: 0 },
  theme: { background: "#ffffff" },
  elements: [],
}, null, 2);

const liveEditCases = [
  {
    label: "text .txt",
    detail: { documentName: "notes.txt", fileType: "text", editorType: "Text" },
    content: "Old title\nstatus: draft\n",
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "markdown .md",
    detail: { documentName: "brief.md", fileType: "markdown", editorType: "Markdown" },
    content: "# Old title\n\n- item\n",
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "code .tsx",
    detail: { documentName: "Widget.tsx", fileType: "code", editorType: "Code" },
    content: 'export const title = "Old title";\n',
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "html .html",
    detail: { documentName: "index.html", fileType: "html", editorType: "Code" },
    content: "<!doctype html><html><head></head><body><h1>Old title</h1></body></html>",
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "json .json",
    detail: { documentName: "config.json", fileType: "json", editorType: "Code" },
    content: '{ "title": "Old title", "items": [] }\n',
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
    validateJson: true,
  },
  {
    label: "csv .csv",
    detail: { documentName: "data.csv", fileType: "csv", editorType: "Spreadsheet" },
    content: "name,status\nOld title,draft\n",
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "docx richtext",
    detail: { documentName: "proposal.docx", fileType: "docx", editorType: "Word" },
    content: "<p>Old title</p><p>Body</p>",
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "xlsx spreadsheet",
    detail: { documentName: "budget.xlsx", fileType: "xlsx", editorType: "Spreadsheet" },
    content: '{ "format": "manor-spreadsheet-v1", "data": [["Old title"]] }\n',
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
    validateJson: true,
  },
  {
    label: "pptx presentation",
    detail: { documentName: "deck.pptx", fileType: "pptx", editorType: "Presentation" },
    content: "--- Slide 1 ---\nOld title\n",
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "diagram",
    detail: { documentName: "flow.diagram.json", fileType: "diagram", editorType: "Diagram" },
    content: diagramJson,
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
    validateJson: true,
  },
  {
    label: "pdf overlay",
    detail: { documentName: "contract.pdf", fileType: "pdf", editorType: "PDF" },
    content: '{ "format": "manor-pdf-overlay-v1", "annotations": [{ "kind": "text", "text": "Old title" }] }\n',
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
    validateJson: true,
  },
  {
    label: "image edit state",
    detail: {
      documentName: "photo.png",
      fileType: "image",
      editorType: "Image",
      supportsImageGeneration: true,
    },
    content: '{ "format": "manor-image-edit-v1", "edits": { "brightness": 100, "label": "Old title" } }\n',
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
    validateJson: true,
  },
  {
    label: "video recipe",
    detail: { documentName: "story.video-edit.json", fileType: "video", editorType: "Video" },
    content: '{ "version": 1, "title": "Old title", "clips": [] }\n',
    find: "Old title",
    replace: "New title",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
    validateJson: true,
  },
];

const fallbackCases = [
  {
    label: "generic quoted text fallback",
    detail: { documentName: "notes.txt", fileType: "text", editorType: "Text" },
    content: "Old title\nstatus: draft\n",
    request: 'replace "Old title" with "New title"',
    expected: /New title/,
  },
  {
    label: "html visual fallback",
    detail: { documentName: "index.html", fileType: "html", editorType: "Code" },
    content: "<!doctype html><html><head></head><body><h1>Hello</h1></body></html>",
    request: "make this page beautiful",
    expected: /manor-ai-polish/,
  },
  {
    label: "pdf local edit fallback",
    detail: {
      documentName: "contract.pdf",
      fileType: "pdf",
      editorType: "PDF",
      localEditContent: () => JSON.stringify({
        format: "manor-pdf-overlay-v1",
        annotations: [{ kind: "text", text: "Reviewed" }],
      }),
    },
    content: '{ "format": "manor-pdf-overlay-v1", "annotations": [] }',
    request: 'add text "Reviewed"',
    expected: /Reviewed/,
    validateJson: true,
  },
  {
    label: "image local edit fallback",
    detail: {
      documentName: "photo.png",
      fileType: "image",
      editorType: "Image",
      supportsImageGeneration: true,
      localEditContent: () => JSON.stringify({
        format: "manor-image-edit-v1",
        edits: { brightness: 118 },
      }),
    },
    content: '{ "format": "manor-image-edit-v1", "edits": { "brightness": 100 } }',
    request: "brighten the image",
    expected: /118/,
    validateJson: true,
  },
];

for (const testCase of liveEditCases) {
  assertPatchCase(testCase);
}

for (const testCase of fallbackCases) {
  assertFallbackCase(testCase);
}

assertStrictPatchProtocolSchema();

const unsupported = ["audio", "unsupported"];

console.log(
  JSON.stringify(
    {
      ok: true,
      patchedFileTypes: liveEditCases.map((testCase) => testCase.label),
      fallbackPaths: fallbackCases.map((testCase) => testCase.label),
      noAiEditSurface: unsupported,
    },
    null,
    2,
  ),
);
