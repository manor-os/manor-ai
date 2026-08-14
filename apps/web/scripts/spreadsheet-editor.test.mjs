#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const bundled = await build({
  stdin: {
    contents: 'export { preserveSpreadsheetFile, resolveSpreadsheetPartTarget, spreadsheetChartsFromFile, spreadsheetSheetsFromWorkbook } from "../src/lib/spreadsheetOoxml.ts";',
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
  preserveSpreadsheetFile,
  resolveSpreadsheetPartTarget,
  spreadsheetChartsFromFile,
  spreadsheetSheetsFromWorkbook,
} = await import(moduleUrl);

assert.equal(resolveSpreadsheetPartTarget("xl/workbook.xml", "worksheets/sheet1.xml"), "xl/worksheets/sheet1.xml");
assert.equal(resolveSpreadsheetPartTarget("xl/workbook.xml", "/xl/worksheets/sheet2.xml"), "xl/worksheets/sheet2.xml");

const directories = [
  new URL("../public/assets/samples/artifacts/sheets/", import.meta.url),
  new URL("../public/assets/samples/candidates/sheets/", import.meta.url),
];
const samples = [];
for (const directory of directories) {
  for (const name of (await readdir(directory)).filter((value) => value.endsWith(".xlsx")).sort()) {
    samples.push(new URL(name, directory));
  }
}
assert.ok(samples.length >= 4, "built-in XLSX samples should cover workbook preservation");

for (const sample of samples) {
  const source = await readFile(sample);
  const workbook = XLSX.read(source, {
    type: "buffer",
    cellFormula: true,
    cellNF: true,
    cellStyles: true,
    cellText: true,
  });
  const sheets = spreadsheetSheetsFromWorkbook(XLSX, workbook);
  const charts = await spreadsheetChartsFromFile(
    source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength),
    XLSX,
    workbook,
  );
  assert.equal(sheets.length, workbook.SheetNames.length, `${sample.pathname}: every worksheet should be modeled`);
  assert.ok(sheets.every((sheet) => sheet.data.length > 0 && sheet.data[0].length > 0));
  assert.ok(sheets.some((sheet) => Object.keys(sheet.styles).length > 0), `${sample.pathname}: styles should reach the shared grid model`);
  assert.ok([...charts.values()].some((sheetCharts) => sheetCharts.length > 0), `${sample.pathname}: native charts should resolve from OOXML`);
}

const preservationSample = samples.find((sample) => sample.pathname.endsWith("side-hustle-profit-planner.xlsx"));
assert.ok(preservationSample, "multi-sheet formula sample should be available");
const sourceBytes = await readFile(preservationSample);
const sourceZip = await JSZip.loadAsync(sourceBytes);
sourceZip.file("customXml/manor-spreadsheet-preservation.xml", "<preserve>unknown SpreadsheetML</preserve>");
const preservationInput = await sourceZip.generateAsync({ type: "arraybuffer" });
const workbook = XLSX.read(preservationInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const sheetModels = spreadsheetSheetsFromWorkbook(XLSX, workbook);
assert.equal(sheetModels.length, 3, "regression workbook should retain all three sheets");
const baseline = sheetModels.map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const edited = structuredClone(baseline);
const revenue = edited.find((sheet) => sheet.name === "Revenue Model");
const pipeline = edited.find((sheet) => sheet.name === "Content Pipeline");
assert.ok(revenue && pipeline);
revenue.data[3][1] = 219;
revenue.data[3][3] = "=B4*C4+1";
pipeline.data[3][4] = "edited without rebuilding workbook";

const preservedFile = await preserveSpreadsheetFile(preservationInput, baseline, edited, "preserved.xlsx");
if (process.env.SPREADSHEET_TEST_OUTPUT) {
  await writeFile(process.env.SPREADSHEET_TEST_OUTPUT, Buffer.from(await preservedFile.arrayBuffer()));
}
const preservedZip = await JSZip.loadAsync(await preservedFile.arrayBuffer());
assert.equal(
  await preservedZip.file("customXml/manor-spreadsheet-preservation.xml").async("text"),
  "<preserve>unknown SpreadsheetML</preserve>",
);
assert.deepEqual(
  Object.keys(preservedZip.files).filter((name) => !preservedZip.files[name].dir).sort(),
  Object.keys(sourceZip.files).filter((name) => !sourceZip.files[name].dir).sort(),
  "incremental XLSX save should retain every package part",
);
for (const preservedPart of ["xl/styles.xml", "xl/sharedStrings.xml", "xl/drawings/charts/chart1.xml"]) {
  assert.deepEqual(
    Buffer.from(await preservedZip.file(preservedPart).async("uint8array")),
    Buffer.from(await sourceZip.file(preservedPart).async("uint8array")),
    `${preservedPart} should be byte-for-byte preserved`,
  );
}
const preservedWorkbook = XLSX.read(await preservedFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
assert.deepEqual(preservedWorkbook.SheetNames, ["Dashboard", "Revenue Model", "Content Pipeline"]);
assert.equal(preservedWorkbook.Sheets["Revenue Model"].B4.v, 219);
assert.equal(preservedWorkbook.Sheets["Revenue Model"].D4.f, "B4*C4+1");
assert.equal(preservedWorkbook.Sheets["Content Pipeline"].E4.v, "edited without rebuilding workbook");
assert.ok(preservedWorkbook.Sheets.Dashboard.B4.f, "cross-sheet dashboard formulas should remain formulas");

const structurallyEdited = structuredClone(edited);
structurallyEdited[0].data.push(Array(structurallyEdited[0].data[0].length).fill(null));
await assert.rejects(
  preserveSpreadsheetFile(preservationInput, baseline, structurallyEdited, "invalid.xlsx"),
  /Adding, deleting, or resizing rows and columns/,
);

console.log("spreadsheet editor OOXML preservation test passed");
