#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";
import JSZip from "jszip";

const bundled = await build({
  stdin: {
    contents: 'export { extractDocumentParagraphSources, preserveDocumentFile } from "../src/lib/documentOoxml.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString("base64")}`;
const { extractDocumentParagraphSources, preserveDocumentFile } = await import(moduleUrl);
const sampleDirectory = new URL("../public/assets/samples/artifacts/docs/", import.meta.url);
const sampleFiles = (await readdir(sampleDirectory)).filter((name) => name.endsWith(".docx")).sort();
assert.ok(sampleFiles.length >= 3, "built-in DOCX samples should be available for preservation regression coverage");

for (const sampleFile of sampleFiles) {
  const source = await readFile(new URL(sampleFile, sampleDirectory));
  const paragraphs = await extractDocumentParagraphSources(source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength));
  assert.ok(paragraphs.length >= 5, `${sampleFile}: should expose Word paragraphs`);
  assert.ok(paragraphs.some((paragraph) => paragraph.editable && paragraph.text.trim()), `${sampleFile}: should expose editable text`);
}

const sourceBytes = await readFile(new URL(sampleFiles[0], sampleDirectory));
const sourceBuffer = sourceBytes.buffer.slice(sourceBytes.byteOffset, sourceBytes.byteOffset + sourceBytes.byteLength);
const sourceZip = await JSZip.loadAsync(sourceBuffer);
sourceZip.file("customXml/manor-document-preservation.xml", "<preserve>unknown Word OOXML</preserve>");
const preservationInput = await sourceZip.generateAsync({ type: "arraybuffer" });
const paragraphs = await extractDocumentParagraphSources(preservationInput);
const target = paragraphs.find((paragraph) => paragraph.editable && paragraph.text.trim());
assert.ok(target, "sample should contain an editable paragraph");
const replacement = `${target.text} - preserved edit`;
const preservedFile = await preserveDocumentFile(preservationInput, new Map([[target.index, replacement]]), "preserved.docx");
if (process.env.DOCUMENT_TEST_OUTPUT) {
  await writeFile(process.env.DOCUMENT_TEST_OUTPUT, Buffer.from(await preservedFile.arrayBuffer()));
}
const preservedZip = await JSZip.loadAsync(await preservedFile.arrayBuffer());
assert.equal(
  await preservedZip.file("customXml/manor-document-preservation.xml").async("text"),
  "<preserve>unknown Word OOXML</preserve>",
);
assert.deepEqual(
  Object.keys(preservedZip.files).filter((name) => !preservedZip.files[name].dir).sort(),
  Object.keys(sourceZip.files).filter((name) => !sourceZip.files[name].dir).sort(),
  "incremental DOCX save should retain every original package part",
);
assert.equal(await preservedZip.file("docProps/core.xml").async("text"), await sourceZip.file("docProps/core.xml").async("text"));
const documentXml = await preservedZip.file("word/document.xml").async("text");
assert.match(documentXml, /preserved edit/);
assert.equal(
  (documentXml.match(/<w:p[\s>][\s\S]*?<\/w:p>/g) || []).length,
  paragraphs.length,
  "incremental DOCX save should retain paragraph structure",
);

console.log("document editor OOXML preservation test passed");
