#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";
import JSZip from "jszip";

const bundled = await build({
  stdin: {
    contents: 'export { extractDocumentParagraphSources, matchDocumentParagraphSourceIndexes, preserveDocumentFile, recoverDocumentParagraphSourceIndexes, reorderDocumentBodyUnitXml } from "../src/lib/documentOoxml.ts";',
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
const {
  extractDocumentParagraphSources,
  matchDocumentParagraphSourceIndexes,
  preserveDocumentFile,
  recoverDocumentParagraphSourceIndexes,
  reorderDocumentBodyUnitXml,
} = await import(moduleUrl);

assert.deepEqual(
  matchDocumentParagraphSourceIndexes(
    [
      { text: "Quarter total" },
      { text: "Quarter total" },
    ],
    [
      { index: 0, text: "Quarter total", editable: false },
      { index: 1, text: "Quarter total", editable: true },
    ],
  ),
  [0, 1],
  "duplicate visible text must retain the original Word paragraph order",
);
assert.deepEqual(
  matchDocumentParagraphSourceIndexes(
    [{ text: "", containsMedia: true }],
    [
      { index: 0, text: "", editable: true },
      { index: 1, text: "", editable: false, containsMedia: true },
    ],
  ),
  [1],
  "media blocks should map to media-bearing Word paragraphs instead of earlier blanks",
);
assert.deepEqual(
  recoverDocumentParagraphSourceIndexes(
    [
      { sourceIndex: 0, tagName: "P" },
      { sourceIndex: 1, tagName: "P" },
    ],
    [
      { sourceIndex: null, tagName: "P" },
      { sourceIndex: 0, tagName: "P" },
      { sourceIndex: null, tagName: "P" },
    ],
  ),
  [null, 0, 1],
  "a new paragraph before an exact source match must not steal that source identity",
);
assert.deepEqual(
  reorderDocumentBodyUnitXml(
    ["<w:p>A</w:p>", "<w:p>B</w:p>", "<w:sectPr/>"],
    [[0], [1]],
    ["<w:p>B</w:p>", "<w:p>A</w:p>"],
  ),
  ["<w:p>B</w:p>", "<w:p>A</w:p>", "<w:sectPr/>"],
  "reordered Word source units should follow edited top-level order while section properties stay last",
);
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

const styledZip = await JSZip.loadAsync(sourceBuffer);
styledZip.file(
  "word/document.xml",
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr><w:pBdr><w:bottom w:val="single" w:sz="4"/></w:pBdr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>Bold</w:t></w:r><w:r><w:t xml:space="preserve"> normal</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
);
const styledInput = await styledZip.generateAsync({ type: "arraybuffer" });
const styledFile = await preserveDocumentFile(styledInput, new Map([[0, "Bolded normal"]]), "styled.docx");
const styledOutput = await JSZip.loadAsync(await styledFile.arrayBuffer());
const styledXml = await styledOutput.file("word/document.xml").async("text");
assert.match(
  styledXml,
  /<w:r><w:rPr><w:b\/><\/w:rPr><w:t>Bolded<\/w:t><\/w:r><w:r><w:t xml:space="preserve"> normal<\/w:t><\/w:r>/,
  "a text-only edit should stay in its original Word run instead of collapsing later run styles",
);
const formattedContentFile = await preserveDocumentFile(styledInput, {
  replacements: new Map(),
  insertions: new Map(),
  formattedContentReplacements: new Map([[0, '<w:p><w:pPr><w:spacing w:after="160"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>Bold normal</w:t></w:r></w:p>']]),
}, "styled-content.docx");
const formattedContentOutput = await JSZip.loadAsync(await formattedContentFile.arrayBuffer());
const formattedContentXml = await formattedContentOutput.file("word/document.xml").async("text");
assert.match(
  formattedContentXml,
  /<w:p><w:pPr><w:pStyle w:val="ListParagraph"\/><w:numPr><w:ilvl w:val="0"\/><w:numId w:val="7"\/><\/w:numPr><w:pBdr><w:bottom w:val="single" w:sz="4"\/><\/w:pBdr><w:jc w:val="center"\/><\/w:pPr><w:r><w:rPr><w:b\/><\/w:rPr><w:t>Bold normal<\/w:t><\/w:r><\/w:p>/,
  "inline formatting should replace runs without importing generated paragraph defaults",
);
assert.doesNotMatch(formattedContentXml, /<w:spacing w:after="160"\/>/);
const formattedBlockFile = await preserveDocumentFile(styledInput, {
  replacements: new Map(),
  insertions: new Map(),
  formattedReplacements: new Map([[0, '<w:p><w:pPr><w:spacing w:after="80"/><w:jc w:val="right"/></w:pPr><w:r><w:t>Bold normal</w:t></w:r></w:p>']]),
}, "styled-block.docx");
const formattedBlockOutput = await JSZip.loadAsync(await formattedBlockFile.arrayBuffer());
const formattedBlockXml = await formattedBlockOutput.file("word/document.xml").async("text");
assert.match(
  formattedBlockXml,
  /<w:pPr><w:pStyle w:val="ListParagraph"\/><w:numPr><w:ilvl w:val="0"\/><w:numId w:val="7"\/><\/w:numPr><w:pBdr><w:bottom w:val="single" w:sz="4"\/><\/w:pBdr><w:spacing w:after="80"\/><w:jc w:val="right"\/><\/w:pPr>/,
  "paragraph formatting should retain the source style and numbering while updating controlled properties",
);
assert.doesNotMatch(formattedBlockXml, /<w:jc w:val="center"\/>/);

const breakText = `${target.text}\nSecond line`;
const insertedParagraphs = ["New paragraph", ""];
const structuralFile = await preserveDocumentFile(preservationInput, {
  replacements: new Map([[target.index, breakText]]),
  insertions: new Map([[target.index, insertedParagraphs]]),
}, "structural-edit.docx");
const structuralBuffer = await structuralFile.arrayBuffer();
const structuralZip = await JSZip.loadAsync(structuralBuffer);
assert.equal(
  await structuralZip.file("customXml/manor-document-preservation.xml").async("text"),
  "<preserve>unknown Word OOXML</preserve>",
  "structural edits should retain unknown package parts",
);
const structuralParagraphs = await extractDocumentParagraphSources(structuralBuffer);
assert.equal(structuralParagraphs.length, paragraphs.length + insertedParagraphs.length);
assert.equal(structuralParagraphs[target.index].text, breakText, "Shift+Enter should persist as a Word line break");
assert.equal(structuralParagraphs[target.index + 1].text, insertedParagraphs[0], "Enter should persist a new Word paragraph");
assert.equal(structuralParagraphs[target.index + 2].text, insertedParagraphs[1], "blank Word paragraphs should persist");

const reeditedFile = await preserveDocumentFile(
  structuralBuffer,
  new Map([[target.index + 1, "Re-edited paragraph"]]),
  "structural-edit.docx",
);
const reeditedParagraphs = await extractDocumentParagraphSources(await reeditedFile.arrayBuffer());
assert.equal(
  reeditedParagraphs[target.index + 1].text,
  "Re-edited paragraph",
  "a paragraph inserted by Enter should remain editable after reload",
);

const editorSource = await readFile(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8");
assert.ok(editorSource.includes('if (!isDocx) return;'), "non-Word rich text should keep native paragraph editing");
assert.ok(
  editorSource.includes('if (insertDocumentBreak(inputType === "insertLineBreak")) event.preventDefault();'),
  "Word beforeinput should prevent the native edit only after the structural break handler succeeds",
);
assert.match(
  editorSource,
  /const DOCX_PARAGRAPH_BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,div,blockquote,pre"/,
  "Enter should keep quotes and code blocks as editable Word paragraphs",
);
const ooxmlSource = await readFile(new URL("../src/lib/documentOoxml.ts", import.meta.url), "utf8");
assert.ok(
  ooxmlSource.includes('clone.querySelectorAll("br").forEach((node) => node.remove())'),
  "loaded Word line breaks should be treated as editable text rather than formatting",
);
assert.doesNotMatch(
  editorSource,
  /Embedded media is not supported in the Word editor yet/,
  "Word image insertion should use the package editing engine instead of a UI restriction",
);
assert.match(ooxmlSource, /relationships\/image/, "Word image edits should create OOXML image relationships");
assert.match(ooxmlSource, /relationships\/hyperlink/, "Word links should create OOXML hyperlink relationships");
assert.match(ooxmlSource, /<w:drawing/, "Word images should serialize as DrawingML rather than placeholder text");
assert.match(
  ooxmlSource,
  /mergeDocumentBodyXml\(\s*patchedMaskedDocumentXml, baselineHtml, html, resources\.context/,
  "structural Word edits should merge changed body units while native text-box contents are masked",
);
assert.match(
  ooxmlSource,
  /restoreWordTextBoxes\(mergedMaskedDocumentXml, patchedTextBoxContents\)/,
  "structural Word edits should restore native text-box contents after the body merge",
);
assert.match(
  ooxmlSource,
  /const source = sourceDocumentTable\(sourceXml, sourceParagraphIndexes\)/,
  "structural table edits should rebuild against the original Word table instead of a generic table",
);
assert.match(
  ooxmlSource,
  /const gridXml = patchTableGrid\(source\.gridXml, targetGridColumns\)/,
  "structural table edits should preserve and resize the source table grid",
);
assert.match(
  ooxmlSource,
  /baselineCell\?\.outerHTML === editedCell\.outerHTML[\s\S]*?return sourceCell\.xml/,
  "unchanged Word cells should retain their complete original OOXML properties and content",
);
const documentEngineSource = await readFile(new URL("../src/lib/manorDocumentEngine.ts", import.meta.url), "utf8");
assert.match(
  documentEngineSource,
  /context\.paragraphIndex \+= descendants\(cellElement, "p"\)\.length/,
  "vertical-merge continuation cells must still advance paragraph identity for later editable content",
);
assert.doesNotMatch(
  ooxmlSource,
  /const nextBody = `\$\{opening\}\$\{documentBodyXml\(html/,
  "structural Word edits must not replace the complete original body from editor HTML",
);
assert.match(
  ooxmlSource,
  /pruneUnusedGeneratedDocumentResources/,
  "repeated structural saves should remove superseded generated relationships and media",
);
assert.match(
  ooxmlSource,
  /const BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,div,blockquote,pre,figure,td,th,hr"/,
  "every rich-text block created by the editor should retain Word paragraph identity across repeated saves",
);
assert.match(
  ooxmlSource,
  /const DIRECT_INLINE_BLOCK_SELECTOR = "a\[href\],img\[src\]"/,
  "root-level pasted links and images should be tracked as Word paragraphs",
);
assert.match(
  editorSource,
  /const DOCX_BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,div,blockquote,pre,figure,td,th,hr"/,
  "the live editor should recognize the same saved Word blocks as the package engine",
);
assert.match(
  editorSource,
  /const savedBlocks = savedRoot \? docxLeafBlocks\(savedRoot\) : \[\];[\s\S]*?const editorBlocks = docxLeafBlocks\(editorRef\.current\);/,
  "the live editor should copy saved table-cell identities back into the editable DOM",
);

console.log("document editor OOXML preservation test passed");
