#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { writeFile } from "node:fs/promises";
import { build } from "esbuild";
import { degrees, PDFDocument, rgb } from "pdf-lib";
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";

const bundled = await build({
  stdin: {
    contents: 'export { offsetPdfPlacement, pdfOverlayPlacement, pdfOverlayPoint } from "../src/lib/pdfOverlayGeometry.ts";',
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
const { offsetPdfPlacement, pdfOverlayPlacement, pdfOverlayPoint } = await import(moduleUrl);

const unrotatedViewport = {
  width: 100,
  height: 200,
  convertToPdfPoint(x, y) {
    return [10 + x, 20 + 200 - y];
  },
};
const unrotated = pdfOverlayPlacement(unrotatedViewport, { x: 0.1, y: 0.2, width: 0.3, height: 0.4 });
assert.ok(Math.abs(unrotated.origin.x - 20) < 1e-9 && Math.abs(unrotated.origin.y - 100) < 1e-9);
assert.ok(Math.abs(unrotated.topLeft.x - 20) < 1e-9 && Math.abs(unrotated.topLeft.y - 180) < 1e-9);
assert.ok(Math.abs(unrotated.width - 30) < 1e-9);
assert.ok(Math.abs(unrotated.height - 80) < 1e-9);
assert.ok(Math.abs(unrotated.rotation) < 1e-9);
const offset = offsetPdfPlacement(unrotated, 5, 7);
assert.ok(Math.abs(offset.x - 25) < 1e-9 && Math.abs(offset.y - 107) < 1e-9);
const convertedPoint = pdfOverlayPoint(unrotatedViewport, { x: 0.25, y: 0.5 });
assert.ok(Math.abs(convertedPoint.x - 35) < 1e-9 && Math.abs(convertedPoint.y - 120) < 1e-9);

const source = await PDFDocument.create();
const page = source.addPage([420, 560]);
page.setCropBox(40, 70, 240, 320);
page.setRotation(degrees(90));
const sourceBytes = await source.save();
const loadingTask = pdfjs.getDocument({
  data: new Uint8Array(sourceBytes),
  disableWorker: true,
});
const loaded = await loadingTask.promise;
const pdfJsPage = await loaded.getPage(1);
const viewport = pdfJsPage.getViewport({ scale: 1 });
assert.equal(viewport.width, 320);
assert.equal(viewport.height, 240);

const normalizedRect = { x: 0.1, y: 0.2, width: 0.35, height: 0.25 };
const rotated = pdfOverlayPlacement(viewport, normalizedRect);
assert.ok(Math.abs(rotated.width - viewport.width * normalizedRect.width) < 1e-6);
assert.ok(Math.abs(rotated.height - viewport.height * normalizedRect.height) < 1e-6);
assert.ok(Math.abs(Math.abs(rotated.rotation) - 90) < 1e-6);

const edited = await PDFDocument.load(sourceBytes);
edited.getPage(0).drawRectangle({
  x: rotated.origin.x,
  y: rotated.origin.y,
  width: rotated.width,
  height: rotated.height,
  rotate: degrees(rotated.rotation),
  color: rgb(0.85, 0.08, 0.08),
});
const editedBytes = await edited.save();
if (process.env.PDF_TEST_OUTPUT) await writeFile(process.env.PDF_TEST_OUTPUT, editedBytes);
await loadingTask.destroy();

console.log("PDF editor overlay geometry test passed");
