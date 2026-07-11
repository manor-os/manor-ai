#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { build } from "esbuild";
import JSZip from "jszip";

const bundled = await build({
  stdin: {
    contents: 'export { buildPresentationBlob } from "../src/lib/presentationPptx.ts";',
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
const { buildPresentationBlob } = await import(moduleUrl);

const pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
const blob = await buildPresentationBlob([
  {
    id: "slide-1",
    bg: "#ffffff",
    aspectRatio: "16/9",
    notes: "Explain the saved editor workflow.",
    shapes: [
      {
        id: "title",
        type: "shape",
        x: 8,
        y: 8,
        w: 84,
        h: 18,
        fill: "#f5f5f4",
        stroke: "#4f7d75",
        strokeWidth: 1,
        presetGeom: "roundRect",
        texts: [{ text: "Editable title", fontSize: 28, bold: true, color: "#1c1917" }],
      },
      {
        id: "image",
        type: "image",
        x: 8,
        y: 32,
        w: 36,
        h: 48,
        imgUrl: pixel,
        imageFit: "cover",
        texts: [],
      },
      {
        id: "table",
        type: "table",
        x: 50,
        y: 32,
        w: 42,
        h: 48,
        texts: [],
        tableRows: [
          [{ text: "Metric", bold: true, fill: "#e7e5e4" }, { text: "Value", bold: true, fill: "#e7e5e4" }],
          [{ text: "Saved" }, { text: "Yes" }],
        ],
        tableColWidths: [1, 1],
      },
    ],
  },
], "Editor smoke test.pptx");

assert.equal(blob.type, "application/vnd.openxmlformats-officedocument.presentationml.presentation");
assert.ok(blob.size > 5_000, "generated presentation should contain an OOXML package");

const zip = await JSZip.loadAsync(await blob.arrayBuffer());
assert.ok(zip.file("ppt/presentation.xml"), "presentation.xml should exist");
assert.ok(zip.file("ppt/slides/slide1.xml"), "first slide should exist");
assert.ok(Object.keys(zip.files).some((name) => name.startsWith("ppt/media/")), "image media should be embedded");

const slideXml = await zip.file("ppt/slides/slide1.xml").async("text");
assert.match(slideXml, /Editable title/);
assert.match(slideXml, /Metric/);
assert.match(slideXml, /Saved/);
assert.ok(Object.keys(zip.files).some((name) => name.startsWith("ppt/notesSlides/notesSlide")), "speaker notes should be embedded");

console.log("presentation editor export smoke test passed");
