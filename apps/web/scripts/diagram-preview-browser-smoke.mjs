#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";
import { chromium } from "@playwright/test";
import { deflateRaw } from "pako";

const bundle = await build({
  stdin: {
    contents: `
      import { createDiagramDocumentsFromDrawioSource } from "../src/lib/diagram/drawioPreview.ts";
      window.__drawioDocuments = createDiagramDocumentsFromDrawioSource;
    `,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "iife",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const compressedModel = `<mxGraphModel><root>
  <mxCell id="0"/><mxCell id="1" parent="0"/>
  <mxCell id="visible" value="Compressed" vertex="1" parent="1"><mxGeometry x="10" y="20" width="120" height="60" as="geometry"/></mxCell>
</root></mxGraphModel>`;
const compressedPayload = Buffer.from(deflateRaw(encodeURIComponent(compressedModel))).toString("base64");
const appCss = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
});
try {
  const page = await browser.newPage();
  await page.setContent("<!doctype html><html><body></body></html>");
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  const result = await page.evaluate(async ({ compressedPayload }) => {
    const hiddenSource = `<?xml version="1.0" encoding="UTF-8"?>
      <mxfile><diagram name="Visibility"><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="hidden-group" value="Hidden" vertex="1" parent="1" visible="0" style="group=1"><mxGeometry width="200" height="100" as="geometry"/></mxCell>
        <mxCell id="hidden-child" value="Secret" vertex="1" parent="hidden-group"><mxGeometry width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="visible" value="Visible" vertex="1" parent="1"><mxGeometry x="300" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="hidden-edge" edge="1" source="hidden-child" target="visible" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>
      </root></mxGraphModel></diagram></mxfile>`;
    const hiddenDocuments = await window.__drawioDocuments(hiddenSource, "Visibility");
    const compressedSource = `<mxfile><diagram name="Compressed">${compressedPayload}</diagram></mxfile>`;
    const compressedDocuments = await window.__drawioDocuments(compressedSource, "Compressed");
    const wrappedSource = `<mxfile><diagram name="Wrapped"><mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <object id="wrapped-source" label="Wrapped source"><mxCell vertex="1" parent="1"><mxGeometry x="20" y="20" width="120" height="60" as="geometry"/></mxCell></object>
      <UserObject id="wrapped-target" label="Wrapped target"><mxCell vertex="1" parent="1"><mxGeometry x="240" y="20" width="120" height="60" as="geometry"/></mxCell></UserObject>
      <object id="wrapped-edge" label="Wrapped link"><mxCell edge="1" source="wrapped-source" target="wrapped-target" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell></object>
    </root></mxGraphModel></diagram></mxfile>`;
    const wrappedDocuments = await window.__drawioDocuments(wrappedSource, "Wrapped");
    return {
      hiddenTexts: hiddenDocuments[0].elements.map((element) => element.text || element.label || "").filter(Boolean),
      hiddenKinds: hiddenDocuments[0].elements.map((element) => element.kind),
      compressedTexts: compressedDocuments[0].elements.map((element) => element.text || "").filter(Boolean),
      wrappedTexts: wrappedDocuments[0].elements.map((element) => element.text || element.label || "").filter(Boolean),
      wrappedKinds: wrappedDocuments[0].elements.map((element) => element.kind),
    };
  }, { compressedPayload });
  assert.deepEqual(result.hiddenTexts, ["Visible"]);
  assert.deepEqual(result.hiddenKinds, ["shape"]);
  assert.deepEqual(result.compressedTexts, ["Compressed"]);
  assert.deepEqual(result.wrappedTexts, ["Wrapped source", "Wrapped target", "Wrapped link"]);
  assert.deepEqual(result.wrappedKinds, ["shape", "shape", "connector"]);

  const tooManyCells = `<mxGraphModel><root>${Array.from(
    { length: 10_001 },
    (_, index) => `<mxCell id="${index}"/>`,
  ).join("")}</root></mxGraphModel>`;
  const complexityError = await page.evaluate(async (source) => {
    try {
      await window.__drawioDocuments(source, "Too many");
      return "";
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  }, tooManyCells);
  assert.match(complexityError, /too many elements/i);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.setContent(`<!doctype html><html data-theme="dark"><head><style>${appCss}</style></head><body><div class="chat-output-diagram-canvas" style="height: 400px"><svg width="320" height="180"><text x="20" y="40" fill="#1c1917">Readable</text></svg></div></body></html>`);
  const canvasStyle = await page.locator(".chat-output-diagram-canvas").evaluate((element) => ({
    backgroundColor: getComputedStyle(element).backgroundColor,
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  assert.equal(canvasStyle.backgroundColor, "rgb(255, 255, 255)");
  assert.ok(canvasStyle.scrollWidth <= canvasStyle.clientWidth);
  console.log("Diagram preview browser smoke passed.");
} finally {
  await browser.close();
}
