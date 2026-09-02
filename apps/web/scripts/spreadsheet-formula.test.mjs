#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { build } from "esbuild";
import * as XLSX from "xlsx";

const bundled = await build({
  stdin: {
    contents: 'export { createSpreadsheetFormulaEvaluationState, evaluateSpreadsheetFormula, getSpreadsheetDisplayValue, getSpreadsheetNumericValue, parseSpreadsheetNumber } from "../src/lib/spreadsheetFormula.ts";',
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
  createSpreadsheetFormulaEvaluationState,
  evaluateSpreadsheetFormula,
  getSpreadsheetDisplayValue,
  getSpreadsheetNumericValue,
  parseSpreadsheetNumber,
} = await import(moduleUrl);

assert.equal(parseSpreadsheetNumber("10%"), 0.1, "cell percentages should use Excel's fractional value");
assert.equal(parseSpreadsheetNumber("－１２．５％"), -0.125, "localized full-width percentages should be normalized");
assert.equal(parseSpreadsheetNumber("$1,250.50"), 1250.5, "formatted currency should remain formula-compatible");
assert.equal(parseSpreadsheetNumber("=A1*2"), null, "formulas should be evaluated by the formula parser");
assert.equal(parseSpreadsheetNumber("10%%"), null, "malformed percentages must fail closed");

const sheet = [
  ["10%", "=A1*2", "=SUM(A1:B1)"],
  ["=B2", "=A2", null],
];
assert.equal(evaluateSpreadsheetFormula(sheet, "=A1*2"), 0.2, "cell percentages should stay fractional in formulas");
assert.equal(getSpreadsheetDisplayValue(sheet, 0, 1), "0.2");
assert.equal(getSpreadsheetDisplayValue(sheet, 0, 1, "0"), "0.2", "stale formula caches must not hide recalculated values");
assert.equal(getSpreadsheetDisplayValue(sheet, 0, 1, "20.0%"), "20.0%", "matching cached values should retain number formatting");
const numberFormatter = (value, numberFormat) => String(XLSX.SSF.format(numberFormat, value));
assert.equal(
  getSpreadsheetDisplayValue([[1000.4, "=A1"]], 0, 1, "$1,000.00", {
    numberFormat: "$#,##0.00",
    formatNumber: numberFormatter,
  }),
  "$1,000.40",
  "visible currency changes must not be hidden by a relative cache tolerance",
);
assert.equal(
  getSpreadsheetDisplayValue([[0.2, "=A1"]], 0, 1, "10.0%", {
    numberFormat: "0.0%",
    formatNumber: numberFormatter,
  }),
  "20.0%",
  "recalculated formulas should retain their Excel number format",
);
assert.equal(evaluateSpreadsheetFormula(sheet, "=-2^2"), 4, "Excel evaluates unary negation before exponentiation");
assert.equal(evaluateSpreadsheetFormula(sheet, "=2^3^2"), 64, "Excel evaluates same-precedence operators left to right");
assert.equal(evaluateSpreadsheetFormula(sheet, "=A2"), null, "circular references must fail closed");
assert.equal(
  evaluateSpreadsheetFormula([["=IF(1,1,0)"], [2], ["=SUM(A1:A2)"]], "=SUM(A1:A2)"),
  null,
  "aggregate formulas must fail closed when a formula dependency cannot be evaluated",
);

const workbookSheets = [
  { name: "Dashboard", data: [["='Revenue Model'!A1", "='Content Pipeline'!A1"]] },
  { name: "Revenue Model", data: [[12, 8], ["=SUM(A1:B1)", null]] },
  { name: "Content Pipeline", data: [["XHS"]] },
];
const dashboardContext = { sheets: workbookSheets, currentSheetName: "Dashboard" };
const revenueContext = { sheets: workbookSheets, currentSheetName: "Revenue Model" };
assert.equal(
  evaluateSpreadsheetFormula(workbookSheets[0].data, "='Revenue Model'!A1*2", new Set(), dashboardContext),
  24,
  "quoted cross-sheet references should participate in arithmetic",
);
assert.equal(
  evaluateSpreadsheetFormula(workbookSheets[1].data, "=SUM('Revenue Model'!A1:B1)", new Set(), revenueContext),
  20,
  "cross-sheet ranges should resolve against the named worksheet",
);
assert.equal(
  getSpreadsheetDisplayValue(workbookSheets[0].data, 0, 0, "11", { context: dashboardContext }),
  "12",
  "cross-sheet numeric display should replace a stale cached value",
);
assert.equal(
  getSpreadsheetDisplayValue(workbookSheets[0].data, 0, 1, "old", { context: dashboardContext }),
  "XHS",
  "direct cross-sheet text references should refresh their display",
);

const unicodeWorkbook = [
  { name: "仪表盘", data: [["=数据!A1*2"]] },
  { name: "数据", data: [[21]] },
];
assert.equal(
  getSpreadsheetDisplayValue(unicodeWorkbook[0].data, 0, 0, "stale", {
    context: { sheets: unicodeWorkbook, currentSheetName: "仪表盘" },
  }),
  "42",
  "unquoted Unicode worksheet references should evaluate like ASCII sheet names",
);

const circularWorkbook = [
  { name: "First", data: [["='Second'!A1"]] },
  { name: "Second", data: [["='First'!A1"]] },
];
assert.equal(
  getSpreadsheetDisplayValue(
    circularWorkbook[0].data,
    0,
    0,
    undefined,
    { context: { sheets: circularWorkbook, currentSheetName: "First" } },
  ),
  "#ERROR",
  "cross-sheet circular references must fail closed",
);

const deepFormulaChain = [[1]];
for (let row = 1; row < 1_000; row += 1) deepFormulaChain.push([`=A${row}+1`]);
assert.equal(
  evaluateSpreadsheetFormula(deepFormulaChain, "=A1000"),
  null,
  "a deep uncached formula chain must stop safely instead of overflowing the JavaScript stack",
);
const deepEvaluationState = createSpreadsheetFormulaEvaluationState();
let deepFormulaValue = null;
for (let row = 0; row < deepFormulaChain.length; row += 1) {
  deepFormulaValue = getSpreadsheetNumericValue(
    deepFormulaChain,
    row,
    0,
    new Set(),
    undefined,
    deepEvaluationState,
  );
}
assert.equal(deepFormulaValue, 1_000, "shared formula evaluation should reuse prior row results");

console.log("spreadsheet formula numeric coercion test passed");
