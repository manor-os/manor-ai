#!/usr/bin/env node
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { build } from "esbuild";
import JSZip from "jszip";
import { chromium } from "@playwright/test";

const sampleDirectory = new URL("../public/assets/samples/artifacts/docs/", import.meta.url);
const sampleFile = (await readdir(sampleDirectory)).find((name) => name.endsWith(".docx"));
assert.ok(sampleFile, "a DOCX package is required for structural preservation fixtures");
const sourcePackage = await readFile(new URL(sampleFile, sampleDirectory));

const fixtureXml = {
  nested: '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tblPr><w:tblStyle w:val="OuterStyle"/></w:tblPr><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>OUTER</w:t></w:r></w:p><w:tbl><w:tblPr><w:tblStyle w:val="NestedStyle"/></w:tblPr><w:tblGrid><w:gridCol w:w="2500"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>NESTED</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p/></w:tc><w:tc><w:tcPr/><w:p><w:r><w:t>SECOND</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>',
  contentControl: '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:sdt><w:sdtPr><w:alias w:val="Protected table"/></w:sdtPr><w:sdtContent><w:tbl><w:tblPr><w:tblStyle w:val="CustomTable"/></w:tblPr><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>LEFT</w:t></w:r></w:p></w:tc><w:tc><w:tcPr/><w:p><w:r><w:t>RIGHT</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:sdtContent></w:sdt><w:sectPr/></w:body></w:document>',
  blockWrapper: '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:customXml><w:p><w:r><w:t>WRAPPED</w:t></w:r></w:p></w:customXml><w:p><w:r><w:t>VISIBLE</w:t></w:r></w:p></w:tc><w:tc><w:tcPr/><w:p><w:r><w:t>SECOND</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>',
  altChunk: '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:altChunk r:id="rId99"/><w:p><w:r><w:t>AFTER</w:t></w:r></w:p></w:tc><w:tc><w:tcPr/><w:p><w:r><w:t>SECOND</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>',
  revision: '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:tcPr/><w:del><w:p><w:r><w:delText>REMOVED BLOCK</w:delText></w:r></w:p></w:del><w:moveFrom><w:p><w:r><w:delText>MOVED FROM</w:delText></w:r></w:p></w:moveFrom><w:ins><w:p><w:r><w:t>INSERTED BLOCK</w:t></w:r></w:p></w:ins><w:moveTo><w:p><w:r><w:t>MOVED FROM</w:t></w:r></w:p></w:moveTo><w:p><w:r><w:t>ACTIVE </w:t></w:r><w:del><w:r><w:delText>INLINE REMOVED</w:delText></w:r></w:del><w:ins><w:r><w:t>INLINE INSERTED</w:t></w:r></w:ins></w:p></w:tc><w:tc><w:tcPr/><w:p><w:r><w:t>SECOND</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>',
};

const fixtures = {};
for (const [name, documentXml] of Object.entries(fixtureXml)) {
  const zip = await JSZip.loadAsync(sourcePackage);
  zip.file("word/document.xml", documentXml);
  fixtures[name] = Array.from(await zip.generateAsync({ type: "uint8array" }));
}

const bundle = await build({
  stdin: {
    contents: `
      export { editDocumentFile } from "../src/lib/documentOoxml.ts";
      export { renderManorDocument } from "../src/lib/manorDocumentEngine.ts";
      export { sanitizeDocumentHtml } from "../src/lib/sanitizeDocumentHtml.ts";
    `,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "iife",
  globalName: "ManorDocumentPreservation",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
});
try {
  const page = await browser.newPage();
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  const savedPackages = await page.evaluate(async (fixtureBytes) => {
    const output = {};
    for (const [name, bytes] of Object.entries(fixtureBytes)) {
      const input = new Uint8Array(bytes).buffer;
      const rendered = await window.ManorDocumentPreservation.renderManorDocument(input);
      const root = document.createElement("div");
      root.innerHTML = window.ManorDocumentPreservation.sanitizeDocumentHtml(rendered.html, {
        allowDocxEditorAttributes: true,
        allowDocxLayoutStyles: true,
      });
      if (name === "altChunk" && !root.querySelector("[data-docx-alt-chunk-id='rId99']")) {
        throw new Error("altChunk identity must survive the real editor sanitizer");
      }
      if (name === "revision") {
        const deletedBlocks = root.querySelectorAll("td > del");
        if (deletedBlocks.length !== 2) throw new Error("block deletions must retain revision markup");
        if (root.textContent.match(/MOVED FROM/g)?.length !== 2) throw new Error("revision rendering must retain both tracked move states");
        if (Array.from(deletedBlocks).some((block) => block.querySelector('[data-docx-source-editable="true"]'))) {
          throw new Error("deleted revision blocks must not be editable");
        }
        const revisionBlocks = Array.from(root.querySelectorAll("p")).filter((block) => (
          ["REMOVED BLOCK", "MOVED FROM", "INSERTED BLOCK"].includes(block.textContent.trim())
        ));
        if (revisionBlocks.length !== 4 || revisionBlocks.some((block) => (
          block.getAttribute("data-docx-source-editable") !== "false"
          || block.getAttribute("contenteditable") !== "false"
        ))) {
          throw new Error("all block-level revision paragraphs must be protected from destructive edits");
        }
        const inlineRevision = Array.from(root.querySelectorAll("p")).find((block) => block.textContent.includes("INLINE REMOVED"));
        if (inlineRevision?.getAttribute("data-docx-source-editable") !== "false"
          || inlineRevision.getAttribute("contenteditable") !== "false") {
          throw new Error(`inline revision paragraphs must be protected from destructive edits: ${inlineRevision?.outerHTML || "missing"}`);
        }
      }
      const table = root.querySelector("table");
      const firstCell = table?.rows[0]?.cells[0];
      const secondCell = table?.rows[0]?.cells[1];
      if (!firstCell || !secondCell) throw new Error(`${name}: fixture table did not render`);
      firstCell.colSpan = 2;
      while (secondCell.firstChild) firstCell.appendChild(secondCell.firstChild);
      secondCell.remove();
      const file = await window.ManorDocumentPreservation.editDocumentFile(
        input,
        rendered.html,
        root.innerHTML,
        `${name}.docx`,
      );
      output[name] = Array.from(new Uint8Array(await file.arrayBuffer()));
    }
    return output;
  }, fixtures);

  const nestedZip = await JSZip.loadAsync(new Uint8Array(savedPackages.nested));
  const nestedXml = await nestedZip.file("word/document.xml").async("text");
  assert.match(nestedXml, /<w:t>NESTED<\/w:t>/, "outer-cell merges must retain nested table content");
  assert.match(nestedXml, /<w:tblStyle w:val="NestedStyle"\/>/, "nested table styling must remain intact");
  assert.equal((nestedXml.match(/<w:tbl[\s>]/g) || []).length, 2, "nested table structure must remain intact");

  const contentControlZip = await JSZip.loadAsync(new Uint8Array(savedPackages.contentControl));
  const contentControlXml = await contentControlZip.file("word/document.xml").async("text");
  assert.match(contentControlXml, /<w:sdt>/, "table content controls must survive structural edits");
  assert.match(contentControlXml, /<w:alias w:val="Protected table"\/>/, "content-control metadata must survive structural edits");
  assert.match(contentControlXml, /<w:tblStyle w:val="CustomTable"\/>/, "wrapped table styling must survive structural edits");

  const blockWrapperZip = await JSZip.loadAsync(new Uint8Array(savedPackages.blockWrapper));
  const blockWrapperXml = await blockWrapperZip.file("word/document.xml").async("text");
  assert.match(blockWrapperXml, /<w:customXml>/, "block-level custom XML wrappers must survive structural edits");
  assert.match(blockWrapperXml, /<w:t>WRAPPED<\/w:t>/, "wrapped paragraphs must survive structural edits");
  assert.match(blockWrapperXml, /<w:t>VISIBLE<\/w:t>/, "paragraphs after block wrappers must keep their source mapping");
  assert.match(blockWrapperXml, /<w:t>SECOND<\/w:t>/, "merged-cell content after block wrappers must not be dropped");

  const altChunkZip = await JSZip.loadAsync(new Uint8Array(savedPackages.altChunk));
  const altChunkXml = await altChunkZip.file("word/document.xml").async("text");
  assert.equal((altChunkXml.match(/<w:altChunk\b/g) || []).length, 1, "embedded document blocks must be retained once");
  assert.doesNotMatch(altChunkXml, /Embedded document content/, "editor placeholders must not become Word paragraphs");

  const revisionZip = await JSZip.loadAsync(new Uint8Array(savedPackages.revision));
  const revisionXml = await revisionZip.file("word/document.xml").async("text");
  assert.match(revisionXml, /<w:del>/, "block deletion revisions must survive table structure edits");
  assert.match(revisionXml, /<w:moveFrom>/, "move-source revisions must survive table structure edits");
  assert.match(revisionXml, /<w:ins><w:p><w:r><w:t>INSERTED BLOCK<\/w:t>/, "block insertion revisions must survive table structure edits");
  assert.match(revisionXml, /<w:moveTo>/, "move-target revisions must survive table structure edits");
  assert.match(revisionXml, /<w:del><w:r><w:delText>INLINE REMOVED<\/w:delText>/, "inline deletion revisions must survive table structure edits");
  assert.match(revisionXml, /<w:ins><w:r><w:t>INLINE INSERTED<\/w:t>/, "inline insertion revisions must survive table structure edits");
  console.log("Document table preservation browser smoke passed.");
} finally {
  await browser.close();
}
