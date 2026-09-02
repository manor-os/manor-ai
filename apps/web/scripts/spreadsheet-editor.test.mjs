#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const bundled = await build({
  stdin: {
    contents: `
      export { isValidSpreadsheetWorksheetName, nextSpreadsheetSheetName, preserveSpreadsheetFile, resolveSpreadsheetPartTarget, spreadsheetActiveSheetIndex, spreadsheetChartsFromFile, spreadsheetImagesFromFile, spreadsheetSheetsFromWorkbook, spreadsheetSheetsFromFile, spreadsheetCellVisualStyle, transformSpreadsheetRange } from "../src/lib/spreadsheetOoxml.ts";
      export { EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX, serializeEditorLiveSpreadsheetPayload } from "../src/lib/editorLiveSpreadsheet.ts";
    `,
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
  EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX,
  isValidSpreadsheetWorksheetName,
  nextSpreadsheetSheetName,
  preserveSpreadsheetFile,
  resolveSpreadsheetPartTarget,
  spreadsheetActiveSheetIndex,
  spreadsheetChartsFromFile,
  spreadsheetImagesFromFile,
  spreadsheetSheetsFromWorkbook,
  spreadsheetSheetsFromFile,
  spreadsheetCellVisualStyle,
  serializeEditorLiveSpreadsheetPayload,
  transformSpreadsheetRange,
} = await import(moduleUrl);

assert.equal(nextSpreadsheetSheetName(["Sheet1", "sheet2", "Data"]), "Sheet3");
const activeSheetModels = [
  { name: "First", hidden: false },
  { name: "Hidden", hidden: true },
  { name: "Selected", hidden: false },
];
assert.equal(spreadsheetActiveSheetIndex({ Workbook: { WBView: [{ activeTab: 2 }] } }, activeSheetModels), 2);
assert.equal(spreadsheetActiveSheetIndex({ Workbook: { WBView: [{ activeTab: 1 }] } }, activeSheetModels), 0);
assert.equal(spreadsheetActiveSheetIndex({ Workbook: { WBView: [{ activeTab: 2 }] } }, activeSheetModels, ["Selected"]), 0);
assert.equal(spreadsheetActiveSheetIndex({ Workbook: { WBView: [{ activeTab: 99 }] } }, activeSheetModels), 0);
assert.equal(isValidSpreadsheetWorksheetName("Rates 2026"), true);
for (const invalidName of ["'Rates", "Rates'", "History", "Bad\u0001Name"]) {
  assert.equal(isValidSpreadsheetWorksheetName(invalidName), false);
}

assert.equal(resolveSpreadsheetPartTarget("xl/workbook.xml", "worksheets/sheet1.xml"), "xl/worksheets/sheet1.xml");
assert.equal(resolveSpreadsheetPartTarget("xl/workbook.xml", "/xl/worksheets/sheet2.xml"), "xl/worksheets/sheet2.xml");

async function nativeChartPackage(chartBlock, titleBlock = '<title><tx><rich><p><r><t>Native chart</t></r></p></rich></tx></title>') {
  const zip = new JSZip();
  zip.file("xl/workbook.xml", '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet" r:id="rId1"/></sheets></workbook>');
  zip.file("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>');
  zip.file("xl/worksheets/sheet1.xml", '<worksheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><drawing r:id="rId1"/></worksheet>');
  zip.file("xl/worksheets/_rels/sheet1.xml.rels", '<Relationships><Relationship Id="rId1" Target="../drawings/drawing1.xml"/></Relationships>');
  zip.file("xl/drawings/drawing1.xml", '<wsDr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><oneCellAnchor><from><col>2</col><colOff>9525</colOff><row>3</row><rowOff>19050</rowOff></from><ext cx="2286000" cy="1143000"/><pic><nvPicPr><cNvPr id="2" name="Logo" descr="Revenue logo"/></nvPicPr><blipFill><a:blip r:embed="rId2"/></blipFill></pic><clientData/></oneCellAnchor><twoCellAnchor><from><col>4</col><colOff>0</colOff><row>2</row><rowOff>0</rowOff></from><to><col>10</col><colOff>0</colOff><row>16</row><rowOff>0</rowOff></to><graphicFrame><graphic><graphicData><c:chart r:id="rId1"/></graphicData></graphic></graphicFrame><clientData/></twoCellAnchor></wsDr>');
  zip.file("xl/drawings/_rels/drawing1.xml.rels", '<Relationships><Relationship Id="rId1" Target="../charts/chart1.xml"/><Relationship Id="rId2" Target="../media/image1.png"/></Relationships>');
  zip.file("xl/charts/chart1.xml", `<chartSpace><chart>${titleBlock}<plotArea>${chartBlock}</plotArea></chart></chartSpace>`);
  zip.file("xl/theme/theme1.xml", '<theme><themeElements><clrScheme><dk1><sysClr val="windowText" lastClr="000000"/></dk1><lt1><sysClr val="window" lastClr="FFFFFF"/></lt1><accent1><srgbClr val="4472C4"/></accent1></clrScheme></themeElements></theme>');
  zip.file("xl/media/image1.png", Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mNkYPj/n4GBgYGJAQoAHgQCAZV+9a0AAAAASUVORK5CYII=", "base64"));
  return zip.generateAsync({ type: "arraybuffer" });
}

const imagePackage = await nativeChartPackage('<barChart><barDir val="col"/><ser><tx><v>Revenue</v></tx><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser></barChart>');
const images = await spreadsheetImagesFromFile(imagePackage);
assert.equal(images.get("Sheet").length, 1);
assert.deepEqual(images.get("Sheet")[0], {
  id: "xl/drawings/drawing1.xml:0",
  src: images.get("Sheet")[0].src,
  name: "Logo",
  altText: "Revenue logo",
  anchor: { r: 3, c: 2 },
  offsetX: 1,
  offsetY: 2,
  width: 240,
  height: 120,
});
assert.match(images.get("Sheet")[0].src, /^data:image\/png;base64,/);

const chartWorkbook = {
  SheetNames: ["Sheet"],
  Sheets: { Sheet: XLSX.utils.aoa_to_sheet([["Jan", 10], ["Feb", 20]]) },
};
for (const [block, expectedType, expectedGrouping] of [
  ['<barChart><barDir val="col"/><grouping val="stacked"/>', "column", "stacked"],
  ['<barChart><barDir val="bar"/><grouping val="percentStacked"/>', "bar", "percentStacked"],
  ["<lineChart><grouping val=\"standard\"/>", "line", "standard"],
  ["<areaChart><grouping val=\"stacked\"/>", "area", "stacked"],
  ["<pieChart>", "pie", undefined],
  ["<doughnutChart>", "doughnut", undefined],
]) {
  const series = '<ser><tx><v>Revenue</v></tx><spPr><solidFill><srgbClr val="174C46"/></solidFill></spPr><marker><symbol val="circle"/></marker><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser>';
  const closing = block.match(/^<([A-Za-z]+Chart)/)[1];
  const packageBytes = await nativeChartPackage(`${block}${series}</${closing}>`);
  const charts = await spreadsheetChartsFromFile(packageBytes, XLSX, chartWorkbook);
  const chart = charts.get("Sheet")[0];
  assert.equal(chart.type, expectedType);
  assert.equal(chart.grouping, expectedGrouping);
  assert.equal(chart.title, "Native chart");
  assert.equal(chart.series[0].color, "#174C46");
  if (expectedType === "line") assert.equal(chart.series[0].showMarkers, true);
}

const comboColumnSeries = '<ser><tx><v>Revenue</v></tx><spPr><solidFill><srgbClr val="174C46"/></solidFill></spPr><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser>';
const comboLineSeries = '<ser><tx><v>Margin</v></tx><spPr><solidFill><srgbClr val="CC6600"/></solidFill></spPr><marker><symbol val="circle"/></marker><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser>';
const comboPackage = await nativeChartPackage(`<lineChart>${comboLineSeries}</lineChart><barChart><barDir val="col"/>${comboColumnSeries}</barChart>`);
const comboCharts = await spreadsheetChartsFromFile(comboPackage, XLSX, chartWorkbook);
const combo = comboCharts.get("Sheet")[0];
assert.equal(combo.type, "combo_column_line");
assert.deepEqual(combo.series.map((series) => series.name), ["Revenue", "Margin"]);
assert.deepEqual(combo.series.map((series) => series.plotType), ["column", "line"]);
assert.equal(combo.series[1].showMarkers, true);

const stockSeries = ["Open", "High", "Low", "Close"].map((name, index) => (
  `<ser><tx><v>${name}</v></tx><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val><idx val="${index}"/></ser>`
)).join("");
const stockPackage = await nativeChartPackage(`<stockChart>${stockSeries}<hiLowLines/><upDownBars/></stockChart>`);
const stockCharts = await spreadsheetChartsFromFile(stockPackage, XLSX, chartWorkbook);
const stock = stockCharts.get("Sheet")[0];
assert.equal(stock.type, "stock_ohlc");
assert.deepEqual(stock.series.map((series) => series.name), ["Open", "High", "Low", "Close"]);
assert.ok(stock.series.every((series) => series.plotType === "stock"));

const volumeSeries = '<ser><tx><v>Volume</v></tx><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser>';
const volumeStockPackage = await nativeChartPackage(`<barChart><barDir val="col"/>${volumeSeries}</barChart><stockChart>${stockSeries}<hiLowLines/><upDownBars/></stockChart>`);
const volumeStockCharts = await spreadsheetChartsFromFile(volumeStockPackage, XLSX, chartWorkbook);
const volumeStock = volumeStockCharts.get("Sheet")[0];
assert.equal(volumeStock.type, "stock_vohlc");
assert.deepEqual(volumeStock.series.map((series) => series.name), ["Volume", "Open", "High", "Low", "Close"]);
assert.deepEqual(volumeStock.series.map((series) => series.plotType), ["volume", "stock", "stock", "stock", "stock"]);

const extraVolumeSeries = volumeSeries.replace("Volume", "Volume 2");
const ambiguousVolumeStockPackage = await nativeChartPackage(`<barChart><barDir val="col"/>${volumeSeries}${extraVolumeSeries}</barChart><stockChart>${stockSeries}<hiLowLines/><upDownBars/></stockChart>`);
const ambiguousVolumeStockCharts = await spreadsheetChartsFromFile(ambiguousVolumeStockPackage, XLSX, chartWorkbook);
assert.equal(ambiguousVolumeStockCharts.get("Sheet").length, 0);

const duplicateStockPlotPackage = await nativeChartPackage(`<barChart><barDir val="col"/>${volumeSeries}</barChart><stockChart>${stockSeries}<hiLowLines/><upDownBars/></stockChart><stockChart><hiLowLines/></stockChart>`);
const duplicateStockPlotCharts = await spreadsheetChartsFromFile(duplicateStockPlotPackage, XLSX, chartWorkbook);
assert.equal(duplicateStockPlotCharts.get("Sheet").length, 0);

const invalidSimpleStockPackage = await nativeChartPackage(`<stockChart>${stockSeries}${volumeSeries}<hiLowLines/><upDownBars/></stockChart>`);
const invalidSimpleStockCharts = await spreadsheetChartsFromFile(invalidSimpleStockPackage, XLSX, chartWorkbook);
assert.equal(invalidSimpleStockCharts.get("Sheet").length, 0);

const unsupportedStockComboPackage = await nativeChartPackage(`<lineChart>${comboLineSeries}</lineChart><stockChart>${stockSeries}</stockChart>`);
const unsupportedStockComboCharts = await spreadsheetChartsFromFile(unsupportedStockComboPackage, XLSX, chartWorkbook);
assert.equal(unsupportedStockComboCharts.get("Sheet").length, 0);

const unsupportedLineAreaPackage = await nativeChartPackage(`<lineChart>${comboLineSeries}</lineChart><areaChart>${comboColumnSeries}</areaChart>`);
const unsupportedLineAreaCharts = await spreadsheetChartsFromFile(unsupportedLineAreaPackage, XLSX, chartWorkbook);
assert.equal(unsupportedLineAreaCharts.get("Sheet").length, 0);

const cachedChartPackage = await nativeChartPackage(`
  <lineChart><ser>
    <tx><strRef><f>Missing!$B$1</f><strCache><ptCount val="1"/><pt idx="0"><v>Cached revenue</v></pt></strCache></strRef></tx>
    <cat><strRef><f>Missing!$A$1:$A$2</f><strCache><ptCount val="2"/><pt idx="0"><v>Jan</v></pt><pt idx="1"><v>Feb</v></pt></strCache></strRef></cat>
    <val><numRef><f>Missing!$B$1:$B$2</f><numCache><ptCount val="2"/><pt idx="0"><v>12</v></pt><pt idx="1"><v>18.5</v></pt></numCache></numRef></val>
  </ser></lineChart>
`);
const cachedCharts = await spreadsheetChartsFromFile(cachedChartPackage, XLSX, chartWorkbook);
assert.equal(cachedCharts.get("Sheet").length, 1);
assert.equal(cachedCharts.get("Sheet")[0].series[0].name, "Cached revenue");
assert.deepEqual(cachedCharts.get("Sheet")[0].series[0].categories, ["Jan", "Feb"]);
assert.deepEqual(cachedCharts.get("Sheet")[0].series[0].values, [12, 18.5]);

const literalChartPackage = await nativeChartPackage(`
  <barChart><barDir val="col"/><ser>
    <tx><v>Literal revenue</v></tx>
    <cat><strLit><ptCount val="2"/><pt idx="0"><v>Mar</v></pt><pt idx="1"><v>Apr</v></pt></strLit></cat>
    <val><numLit><ptCount val="2"/><pt idx="0"><v>21</v></pt><pt idx="1"><v>34</v></pt></numLit></val>
  </ser></barChart>
`);
const literalCharts = await spreadsheetChartsFromFile(literalChartPackage, XLSX, chartWorkbook);
assert.equal(literalCharts.get("Sheet").length, 1);
assert.equal(literalCharts.get("Sheet")[0].series[0].name, "Literal revenue");
assert.deepEqual(literalCharts.get("Sheet")[0].series[0].categories, ["Mar", "Apr"]);
assert.deepEqual(literalCharts.get("Sheet")[0].series[0].values, [21, 34]);

const fullColumnChartPackage = await nativeChartPackage('<lineChart><ser><tx><v>Full column</v></tx><cat><strRef><f>Sheet!$A$1:$A$1048576</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$1048576</f></numRef></val></ser></lineChart>');
const fullColumnCharts = await spreadsheetChartsFromFile(fullColumnChartPackage, XLSX, chartWorkbook);
assert.deepEqual(fullColumnCharts.get("Sheet")[0].series[0].categories, ["Jan", "Feb"]);
assert.deepEqual(fullColumnCharts.get("Sheet")[0].series[0].values, [10, 20]);

const linkedTitlePackage = await nativeChartPackage(
  '<lineChart><ser><tx><v>Revenue</v></tx><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser></lineChart>',
  '<title><tx><strRef><f>Missing!$D$1</f><strCache><ptCount val="1"/><pt idx="0"><v>Linked revenue</v></pt></strCache></strRef></tx></title>',
);
const linkedTitleCharts = await spreadsheetChartsFromFile(linkedTitlePackage, XLSX, chartWorkbook);
assert.equal(linkedTitleCharts.get("Sheet")[0].title, "Linked revenue");

const axisOnlyTitlePackage = await nativeChartPackage(
  '<lineChart><ser><tx><v>Revenue</v></tx><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser></lineChart><valAx><title><tx><rich><p><r><t>Revenue axis</t></r></p></rich></tx></title></valAx>',
  '',
);
const axisOnlyTitleCharts = await spreadsheetChartsFromFile(axisOnlyTitlePackage, XLSX, chartWorkbook);
assert.equal(axisOnlyTitleCharts.get("Sheet")[0].title, "Chart");

const themedSeries = '<ser><tx><v>Themed</v></tx><spPr><solidFill><schemeClr val="accent1"><lumMod val="60000"/><lumOff val="40000"/></schemeClr></solidFill></spPr><cat><strRef><f>Sheet!$A$1:$A$2</f></strRef></cat><val><numRef><f>Sheet!$B$1:$B$2</f></numRef></val></ser>';
const themedPackage = await nativeChartPackage(`<lineChart>${themedSeries}</lineChart>`);
const themedCharts = await spreadsheetChartsFromFile(themedPackage, XLSX, chartWorkbook);
assert.equal(themedCharts.get("Sheet")[0].series[0].color, "#8FAADC");

const scatterSeries = '<ser><tx><v>Observed</v></tx><spPr><solidFill><srgbClr val="336699"/></solidFill></spPr><marker><symbol val="circle"/></marker><xVal><numRef><f>Sheet!$B$1:$B$2</f></numRef></xVal><yVal><numRef><f>Sheet!$B$1:$B$2</f></numRef></yVal></ser>';
const scatterPackage = await nativeChartPackage(`<scatterChart><scatterStyle val="smoothMarker"/>${scatterSeries}</scatterChart>`);
const scatterCharts = await spreadsheetChartsFromFile(scatterPackage, XLSX, chartWorkbook);
const scatter = scatterCharts.get("Sheet")[0];
assert.equal(scatter.type, "scatter");
assert.equal(scatter.scatterStyle, "smoothMarker");
assert.deepEqual(scatter.series[0].categories, [10, 20]);
assert.deepEqual(scatter.series[0].values, [10, 20]);
assert.equal(scatter.series[0].showMarkers, true);

assert.equal(
  transformSpreadsheetRange("C4:C5", [{ axis: "row", index: 1, deleteCount: 1, insertCount: 0 }]),
  "C3:C4",
  "a deletion before a formula range should shift without expanding it",
);
assert.equal(
  transformSpreadsheetRange("C2:C3", [{ axis: "row", index: 4, deleteCount: 1, insertCount: 0 }]),
  "C2:C3",
  "a deletion after a formula range should leave it unchanged",
);

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
  assert.ok(
    sheets.some((sheet) => Object.keys(sheet.numberFormats).length > 0),
    `${sample.pathname}: number formats should reach the shared grid model`,
  );
  assert.ok([...charts.values()].some((sheetCharts) => sheetCharts.length > 0), `${sample.pathname}: native charts should resolve from OOXML`);
}

const preservationSample = samples.find((sample) => sample.pathname.endsWith("side-hustle-profit-planner.xlsx"));
assert.ok(preservationSample, "multi-sheet formula sample should be available");
const sourceBytes = await readFile(preservationSample);
const sourceZip = await JSZip.loadAsync(sourceBytes);
sourceZip.file("customXml/manor-spreadsheet-preservation.xml", "<preserve>unknown SpreadsheetML</preserve>");
const revenueSheetPart = "xl/worksheets/sheet2.xml";
const revenueSheetXml = await sourceZip.file(revenueSheetPart).async("text");
sourceZip.file(
  revenueSheetPart,
  revenueSheetXml.replace(
    "<x:f>D4-E4*C4</x:f>",
    '<x:f t="array" ref="F4">D4-E4*C4</x:f>',
  ),
);
const workbookXml = await sourceZip.file("xl/workbook.xml").async("text");
sourceZip.file(
  "xl/workbook.xml",
  workbookXml.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?calcPr\b[^>]*)\s*\/>/i,
    "$1></x:calcPr>",
  ),
);
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
const preservedRevenueSheetXml = await preservedZip.file(revenueSheetPart).async("text");
assert.match(
  preservedRevenueSheetXml,
  /<x:f t="array" ref="F4">D4-E4\*C4<\/x:f><x:v>1561<\/x:v>/,
  "refreshing a dependent formula cache must preserve its original formula attributes",
);
const preservedWorkbookXml = await preservedZip.file("xl/workbook.xml").async("text");
assert.match(preservedWorkbookXml, /<x:calcPr\b[^>]*calcMode="auto"[^>]*\/>/);
assert.doesNotMatch(preservedWorkbookXml, /<\/x:calcPr>/, "recalculation settings must remain valid when calcPr was not self-closing");
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
assert.equal(preservedWorkbook.Sheets["Revenue Model"].D4.v, 1753, "edited formulas should persist a fresh cached value");
assert.equal(preservedWorkbook.Sheets["Revenue Model"].F4.v, 1561, "dependent formulas should refresh their cached value");
assert.equal(preservedWorkbook.Sheets["Content Pipeline"].E4.v, "edited without rebuilding workbook");
assert.ok(preservedWorkbook.Sheets.Dashboard.B4.f, "cross-sheet dashboard formulas should remain formulas");
assert.equal(preservedWorkbook.Sheets.Dashboard.B4.v, 11194, "cross-sheet numeric formula caches should refresh");
assert.equal(
  preservedWorkbook.Sheets.Dashboard.H4.v,
  "edited without rebuilding workbook",
  "cross-sheet text formula caches should refresh",
);

const sheetsWithAddition = structuredClone(baseline);
sheetsWithAddition.push({
  name: nextSpreadsheetSheetName(sheetsWithAddition.map((sheet) => sheet.name)),
  data: [["Item", "Amount"], ["Launch", 12], ["Total", "=SUM(B2:B2)"]],
  styles: { "0:0": { bold: true, fill: "#dbeafe" } },
});
const addedSheetFile = await preserveSpreadsheetFile(
  preservationInput,
  baseline,
  sheetsWithAddition,
  "added-sheet.xlsx",
);
const addedSheetBuffer = await addedSheetFile.arrayBuffer();
const addedSheetZip = await JSZip.loadAsync(addedSheetBuffer);
assert.equal(
  await addedSheetZip.file("customXml/manor-spreadsheet-preservation.xml").async("text"),
  "<preserve>unknown SpreadsheetML</preserve>",
  "adding a worksheet should preserve unrelated workbook package parts",
);
const addedSheetWorkbook = XLSX.read(addedSheetBuffer, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
assert.deepEqual(
  addedSheetWorkbook.SheetNames,
  ["Dashboard", "Revenue Model", "Content Pipeline", "Sheet1"],
  "new worksheets should be appended to the same workbook",
);
assert.equal(addedSheetWorkbook.Sheets.Sheet1.A2.v, "Launch");
assert.equal(addedSheetWorkbook.Sheets.Sheet1.B3.f, "SUM(B2:B2)");
assert.equal(addedSheetWorkbook.Sheets.Sheet1.B3.v, 12, "new worksheet formulas should save a usable cache");
assert.equal(
  spreadsheetSheetsFromWorkbook(XLSX, addedSheetWorkbook).find((sheet) => sheet.name === "Sheet1").styles["0:0"].fill.toLowerCase(),
  "#dbeafe",
  "new worksheet styles should remain editable in namespaced workbooks",
);

const addedSheetBaseline = spreadsheetSheetsFromWorkbook(XLSX, addedSheetWorkbook).map((sheet) => ({
  name: sheet.name,
  data: structuredClone(sheet.data),
  styles: structuredClone(sheet.styles),
}));
const addedSheetEditedAgain = structuredClone(addedSheetBaseline);
addedSheetEditedAgain.find((sheet) => sheet.name === "Sheet1").data[1][1] = 18;
const editedAddedSheetFile = await preserveSpreadsheetFile(
  addedSheetBuffer,
  addedSheetBaseline,
  addedSheetEditedAgain,
  "added-sheet-edited-again.xlsx",
);
const editedAddedSheetWorkbook = XLSX.read(await editedAddedSheetFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.equal(editedAddedSheetWorkbook.Sheets.Sheet1.B2.v, 18);
assert.equal(
  editedAddedSheetWorkbook.Sheets.Sheet1.B3.v,
  18,
  "a newly added worksheet should remain editable after it is saved and reloaded",
);

const sharedFormulaWorkbook = XLSX.utils.book_new();
const sharedFormulaSheet = XLSX.utils.aoa_to_sheet([[1, null], [2, null], [3, null]]);
sharedFormulaSheet.B1 = { t: "n", f: "A1*2", v: 2 };
sharedFormulaSheet.B2 = { t: "n", f: "A2*2", v: 4 };
sharedFormulaSheet.B3 = { t: "n", f: "A3*2", v: 6 };
sharedFormulaSheet["!ref"] = "A1:B3";
XLSX.utils.book_append_sheet(sharedFormulaWorkbook, sharedFormulaSheet, "Shared");
const sharedFormulaBytes = XLSX.write(sharedFormulaWorkbook, { type: "array", bookType: "xlsx" });
const sharedFormulaZip = await JSZip.loadAsync(sharedFormulaBytes);
const sharedFormulaPart = "xl/worksheets/sheet1.xml";
const sharedFormulaXml = await sharedFormulaZip.file(sharedFormulaPart).async("text");
sharedFormulaZip.file(
  sharedFormulaPart,
  sharedFormulaXml
    .replace('<f>A1*2</f>', '<f t="shared" si="0" ref="B1:B3">A1*2</f>')
    .replace('<f>A2*2</f>', '<f t="shared" si="0"/>')
    .replace('<f>A3*2</f>', '<f t="shared" si="0"/>'),
);
const sharedFormulaInput = await sharedFormulaZip.generateAsync({ type: "arraybuffer" });
const sharedFormulaParsed = XLSX.read(sharedFormulaInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const sharedFormulaBaseline = spreadsheetSheetsFromWorkbook(XLSX, sharedFormulaParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const sharedFormulaEdited = structuredClone(sharedFormulaBaseline);
sharedFormulaEdited[0].data[0][1] = 20;
const sharedFormulaFile = await preserveSpreadsheetFile(
  sharedFormulaInput,
  sharedFormulaBaseline,
  sharedFormulaEdited,
  "shared-formula.xlsx",
);
const sharedFormulaSavedZip = await JSZip.loadAsync(await sharedFormulaFile.arrayBuffer());
const sharedFormulaSavedXml = await sharedFormulaSavedZip.file(sharedFormulaPart).async("text");
assert.doesNotMatch(
  sharedFormulaSavedXml,
  /<f\b[^>]*\bt="shared"/,
  "editing one member of a shared-formula group should materialize the remaining formulas",
);
const sharedFormulaSavedWorkbook = XLSX.read(await sharedFormulaFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.equal(sharedFormulaSavedWorkbook.Sheets.Shared.B1.v, 20);
assert.equal(sharedFormulaSavedWorkbook.Sheets.Shared.B2.f, "A2*2");
assert.equal(sharedFormulaSavedWorkbook.Sheets.Shared.B3.f, "A3*2");

const unsupportedWorkbook = XLSX.utils.book_new();
const unsupportedSheet = XLSX.utils.aoa_to_sheet([[1, null, 10, null], [2, 3], [null, null]]);
unsupportedSheet.B1 = { t: "n", f: "IF(A1>0,A1,0)", v: 1 };
unsupportedSheet.D1 = { t: "n", f: "IF(C1>0,C1,0)", v: 10 };
unsupportedSheet.A3 = { t: "n", f: "SUM(B1:B2)", v: 4 };
unsupportedSheet["!ref"] = "A1:D3";
XLSX.utils.book_append_sheet(unsupportedWorkbook, unsupportedSheet, "Sheet1");
const unsupportedInput = XLSX.write(unsupportedWorkbook, { type: "array", bookType: "xlsx" });
const unsupportedParsed = XLSX.read(unsupportedInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const unsupportedBaseline = spreadsheetSheetsFromWorkbook(XLSX, unsupportedParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const unsupportedEdited = structuredClone(unsupportedBaseline);
unsupportedEdited[0].data[0][0] = 2;
unsupportedEdited[0].data[1][0] = 4;
const unsupportedFile = await preserveSpreadsheetFile(
  unsupportedInput,
  unsupportedBaseline,
  unsupportedEdited,
  "unsupported.xlsx",
);
const unsupportedResult = XLSX.read(await unsupportedFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.equal(unsupportedResult.Sheets.Sheet1.B1.f, "IF(A1>0,A1,0)");
assert.equal(unsupportedResult.Sheets.Sheet1.B1.w, "#N/A", "unsupported formulas must invalidate stale caches");
assert.equal(unsupportedResult.Sheets.Sheet1.A3.f, "SUM(B1:B2)");
assert.equal(unsupportedResult.Sheets.Sheet1.A3.w, "#N/A", "dependent aggregates must not persist partial caches");
assert.equal(unsupportedResult.Sheets.Sheet1.D1.v, 10, "unrelated unsupported formula caches should remain intact");

const unicodeWorkbook = XLSX.utils.book_new();
XLSX.utils.book_append_sheet(unicodeWorkbook, XLSX.utils.aoa_to_sheet([[1]]), "数据");
const unicodeDashboard = XLSX.utils.aoa_to_sheet([[null]]);
unicodeDashboard.A1 = { t: "n", f: "IF(数据!A1>0,数据!A1,0)", v: 1 };
unicodeDashboard["!ref"] = "A1";
XLSX.utils.book_append_sheet(unicodeWorkbook, unicodeDashboard, "仪表盘");
const unicodeInput = XLSX.write(unicodeWorkbook, { type: "array", bookType: "xlsx" });
const unicodeParsed = XLSX.read(unicodeInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const unicodeBaseline = spreadsheetSheetsFromWorkbook(XLSX, unicodeParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const unicodeEdited = structuredClone(unicodeBaseline);
unicodeEdited.find((sheet) => sheet.name === "数据").data[0][0] = 2;
const unicodeFile = await preserveSpreadsheetFile(
  unicodeInput,
  unicodeBaseline,
  unicodeEdited,
  "unicode-reference.xlsx",
);
const unicodeResult = XLSX.read(await unicodeFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.equal(unicodeResult.Sheets["仪表盘"].A1.f, "IF(数据!A1>0,数据!A1,0)");
assert.equal(
  unicodeResult.Sheets["仪表盘"].A1.w,
  "#N/A",
  "unsupported formulas with Unicode worksheet dependencies must not retain stale caches",
);

const namedRangeWorkbook = XLSX.utils.book_new();
const namedRangeSheet = XLSX.utils.aoa_to_sheet([[1, 0.1, null]]);
namedRangeSheet.C1 = { t: "n", f: "IF(A1>0,TaxRate,0)", v: 0.1 };
namedRangeSheet["!ref"] = "A1:C1";
XLSX.utils.book_append_sheet(namedRangeWorkbook, namedRangeSheet, "Sheet1");
namedRangeWorkbook.Workbook = {
  ...(namedRangeWorkbook.Workbook || {}),
  Names: [{ Name: "TaxRate", Ref: "Sheet1!$B$1" }],
};
const namedRangeInput = XLSX.write(namedRangeWorkbook, { type: "array", bookType: "xlsx" });
const namedRangeParsed = XLSX.read(namedRangeInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const namedRangeBaseline = spreadsheetSheetsFromWorkbook(XLSX, namedRangeParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const namedRangeEdited = structuredClone(namedRangeBaseline);
namedRangeEdited[0].data[0][1] = 0.2;
const namedRangeFile = await preserveSpreadsheetFile(
  namedRangeInput,
  namedRangeBaseline,
  namedRangeEdited,
  "named-range.xlsx",
);
const namedRangeResult = XLSX.read(await namedRangeFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.equal(namedRangeResult.Sheets.Sheet1.C1.f, "IF(A1>0,TaxRate,0)");
assert.equal(
  namedRangeResult.Sheets.Sheet1.C1.w,
  "#N/A",
  "unsupported formulas with named references must not retain stale caches",
);

const renameWorkbook = XLSX.utils.book_new();
const renameMainSheet = XLSX.utils.aoa_to_sheet([["Total"], [null]]);
renameMainSheet.A2 = { t: "n", f: "'Source Data'!A1*2", v: 4 };
renameMainSheet["!ref"] = "A1:A2";
XLSX.utils.book_append_sheet(renameWorkbook, renameMainSheet, "Summary");
XLSX.utils.book_append_sheet(renameWorkbook, XLSX.utils.aoa_to_sheet([[2]]), "Source Data");
const renameInput = XLSX.write(renameWorkbook, { type: "array", bookType: "xlsx" });
const renameParsed = XLSX.read(renameInput, {
  type: "array",
  cellFormula: true,
  cellText: true,
});
const renameBaseline = spreadsheetSheetsFromWorkbook(XLSX, renameParsed)
  .map((sheet) => ({ name: sheet.name, sourceName: sheet.sourceName, data: structuredClone(sheet.data) }));
for (const invalidName of ["'Rates", "Rates'", "History", "Bad\u0001Name"]) {
  const invalidRename = structuredClone(renameBaseline);
  invalidRename[1].name = invalidName;
  await assert.rejects(
    preserveSpreadsheetFile(renameInput, renameBaseline, invalidRename, "invalid-sheet.xlsx"),
    /Worksheet names must be unique and Excel-compatible/,
  );
}
const invalidFallbackRename = structuredClone(renameBaseline);
invalidFallbackRename[1].name = "History";
await assert.rejects(
  preserveSpreadsheetFile(
    new ArrayBuffer(0),
    renameBaseline,
    invalidFallbackRename,
    "invalid-fallback-sheet.xlsx",
  ),
  /Worksheet names must be unique and Excel-compatible/,
);
const renameEdited = structuredClone(renameBaseline);
renameEdited[1].name = "Rates 2026";
const renamedFile = await preserveSpreadsheetFile(
  renameInput,
  renameBaseline,
  renameEdited,
  "renamed-sheet.xlsx",
);
const renamedResult = XLSX.read(await renamedFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.deepEqual(renamedResult.SheetNames, ["Summary", "Rates 2026"]);
assert.equal(renamedResult.Sheets.Summary.A2.f, "'Rates 2026'!A1*2");

const reorderedSheets = [structuredClone(renameBaseline[1]), structuredClone(renameBaseline[0])];
await assert.rejects(
  preserveSpreadsheetFile(renameInput, renameBaseline, reorderedSheets, "reordered-sheets.xlsx"),
  /Removing or reordering worksheets/,
);
const reorderedAndRenamedSheets = [structuredClone(renameBaseline[1]), structuredClone(renameBaseline[0])];
reorderedAndRenamedSheets[0].name = "North";
reorderedAndRenamedSheets[1].name = "South";
await assert.rejects(
  preserveSpreadsheetFile(renameInput, renameBaseline, reorderedAndRenamedSheets, "reordered-and-renamed-sheets.xlsx"),
  /Removing or reordering worksheets/,
);

const renamedAndInserted = structuredClone(renameBaseline);
renamedAndInserted[1].name = "Rates 2026";
renamedAndInserted[1].data.splice(0, 0, ["Inserted"]);
renamedAndInserted[1].structureOperations = [{
  axis: "row",
  index: 0,
  deleteCount: 0,
  insertCount: 1,
}];
const renamedAndInsertedFile = await preserveSpreadsheetFile(
  renameInput,
  renameBaseline,
  renamedAndInserted,
  "renamed-and-inserted-sheet.xlsx",
);
const renamedAndInsertedResult = XLSX.read(
  await renamedAndInsertedFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellText: true },
);
assert.equal(
  renamedAndInsertedResult.Sheets.Summary.A2.f,
  "'Rates 2026'!A2*2",
  "structural references must be shifted under the original sheet name before it is renamed",
);

const linkedRenameZip = await JSZip.loadAsync(renameInput);
const linkedSummaryXml = await linkedRenameZip.file("xl/worksheets/sheet1.xml").async("text");
linkedRenameZip.file(
  "xl/worksheets/sheet1.xml",
  linkedSummaryXml.replace(
    /<\/worksheet>$/,
    '<hyperlinks><hyperlink ref="A1" location="#\'Source Data\'!A1" display="Jump"/><hyperlink ref="A2" location="#\'Source Data\'!A1" r:id="rId99" display="External jump"/></hyperlinks></worksheet>',
  ),
);
linkedRenameZip.file(
  "xl/pivotCache/pivotCacheDefinition99.xml",
  '<pivotCacheDefinition><cacheSource><worksheetSource ref="A1:A2" sheet="Source Data"/></cacheSource></pivotCacheDefinition>',
);
linkedRenameZip.file(
  "xl/pivotCache/pivotCacheDefinition100.xml",
  '<pivotCacheDefinition><cacheSource><worksheetSource r:id="rId88" ref="A1:A2" sheet="Source Data"/></cacheSource></pivotCacheDefinition>',
);
const linkedRenameInput = await linkedRenameZip.generateAsync({ type: "arraybuffer" });
const linkedRenamedFile = await preserveSpreadsheetFile(
  linkedRenameInput,
  renameBaseline,
  renameEdited,
  "renamed-linked-sheet.xlsx",
);
const linkedRenamedZip = await JSZip.loadAsync(await linkedRenamedFile.arrayBuffer());
assert.match(
  await linkedRenamedZip.file("xl/worksheets/sheet1.xml").async("text"),
  /location="#&apos;Rates 2026&apos;!A1"/,
  "internal worksheet hyperlinks must follow a renamed sheet",
);
assert.match(
  await linkedRenamedZip.file("xl/pivotCache/pivotCacheDefinition99.xml").async("text"),
  /worksheetSource\b[^>]*sheet="Rates 2026"/,
  "pivot worksheet sources must follow a renamed sheet",
);
assert.match(
  await linkedRenamedZip.file("xl/worksheets/sheet1.xml").async("text"),
  /location="#'Source Data'!A1" r:id="rId99"/,
  "external hyperlinks must retain their external worksheet location",
);
assert.match(
  await linkedRenamedZip.file("xl/pivotCache/pivotCacheDefinition100.xml").async("text"),
  /r:id="rId88"[^>]*sheet="Source Data"/,
  "external pivot sources must retain their external worksheet name",
);

const chainedRenameWorkbook = XLSX.utils.book_new();
const chainedRenameMainSheet = XLSX.utils.aoa_to_sheet([["Total"], [null]]);
chainedRenameMainSheet.A2 = { t: "n", f: 'A!A1+B!A1+Data!A1+MyData!A1+SUM(A:B!A1)+[1]Data!A1+\'[1]Data\'!A1+IF("Data!A1"="Data!A1",1,0)', v: 16 };
chainedRenameMainSheet["!ref"] = "A1:A2";
XLSX.utils.book_append_sheet(chainedRenameWorkbook, chainedRenameMainSheet, "Summary");
XLSX.utils.book_append_sheet(chainedRenameWorkbook, XLSX.utils.aoa_to_sheet([[2]]), "A");
XLSX.utils.book_append_sheet(chainedRenameWorkbook, XLSX.utils.aoa_to_sheet([[3]]), "B");
XLSX.utils.book_append_sheet(chainedRenameWorkbook, XLSX.utils.aoa_to_sheet([[5]]), "Data");
XLSX.utils.book_append_sheet(chainedRenameWorkbook, XLSX.utils.aoa_to_sheet([[7]]), "MyData");
const chainedRenameInput = XLSX.write(chainedRenameWorkbook, { type: "array", bookType: "xlsx" });
const chainedRenameParsed = XLSX.read(chainedRenameInput, {
  type: "array",
  cellFormula: true,
  cellText: true,
});
const chainedRenameBaseline = spreadsheetSheetsFromWorkbook(XLSX, chainedRenameParsed)
  .map((sheet) => ({ name: sheet.name, sourceName: sheet.sourceName, data: structuredClone(sheet.data) }));
const chainedRenameEdited = structuredClone(chainedRenameBaseline);
chainedRenameEdited[1].name = "B";
chainedRenameEdited[2].name = "C";
chainedRenameEdited[3].name = "Revenue$2026";
chainedRenameEdited[4].name = "Model";
const chainedRenamedFile = await preserveSpreadsheetFile(
  chainedRenameInput,
  chainedRenameBaseline,
  chainedRenameEdited,
  "chained-renamed-sheets.xlsx",
);
const chainedRenamedResult = XLSX.read(await chainedRenamedFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.deepEqual(chainedRenamedResult.SheetNames, ["Summary", "B", "C", "Revenue$2026", "Model"]);
assert.equal(
  chainedRenamedResult.Sheets.Summary.A2.f,
  '\'B\'!A1+\'C\'!A1+\'Revenue$2026\'!A1+\'Model\'!A1+SUM(\'B:C\'!A1)+[1]Data!A1+\'[1]Data\'!A1+IF("Data!A1"="Data!A1",1,0)',
  "batched sheet renames must not chain, match external books or suffixes, break 3D ranges, or rewrite string literals",
);

const secondRenameBaseline = spreadsheetSheetsFromWorkbook(XLSX, chainedRenamedResult)
  .map((sheet) => ({ name: sheet.name, sourceName: sheet.sourceName, data: structuredClone(sheet.data) }));
const secondRenameEdited = structuredClone(secondRenameBaseline);
secondRenameEdited[2].name = "Final";
const secondRenamedFile = await preserveSpreadsheetFile(
  await chainedRenamedFile.arrayBuffer(),
  secondRenameBaseline,
  secondRenameEdited,
  "second-renamed-sheets.xlsx",
);
const secondRenamedResult = XLSX.read(await secondRenamedFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
assert.equal(
  secondRenamedResult.Sheets.Summary.A2.f,
  '\'B\'!A1+\'Final\'!A1+\'Revenue$2026\'!A1+\'Model\'!A1+SUM(\'B:Final\'!A1)+[1]Data!A1+\'[1]Data\'!A1+IF("Data!A1"="Data!A1",1,0)',
  "quoted 3D sheet ranges from a prior save must keep following endpoint renames",
);

const structurallyEdited = structuredClone(edited);
structurallyEdited[0].data.push(Array(structurallyEdited[0].data[0].length).fill(null));
structurallyEdited[0].data.at(-1)[0] = "compatibility row";
const structurallyEditedFile = await preserveSpreadsheetFile(
  preservationInput,
  baseline,
  structurallyEdited,
  "resized.xlsx",
);
const structurallyEditedWorkbook = XLSX.read(await structurallyEditedFile.arrayBuffer(), {
  type: "array",
  cellFormula: true,
  cellText: true,
});
const structurallyEditedSheet = structurallyEditedWorkbook.Sheets[structurallyEdited[0].name];
assert.ok(structurallyEditedSheet, "compatibility save should retain the edited worksheet");
assert.equal(
  structurallyEditedSheet[`A${structurallyEdited[0].data.length}`].v,
  "compatibility row",
  "structural edits should save through the editable compatibility path",
);

const blankWorkbook = XLSX.utils.book_new();
XLSX.utils.book_append_sheet(blankWorkbook, XLSX.utils.aoa_to_sheet([]), "Blank");
const blankInput = XLSX.write(blankWorkbook, { type: "array", bookType: "xlsx" });
const blankParsed = XLSX.read(blankInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const blankBaseline = spreadsheetSheetsFromWorkbook(XLSX, blankParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const blankEdited = structuredClone(blankBaseline);
blankEdited[0].data[0][0] = "first value";
const blankFile = await preserveSpreadsheetFile(blankInput, blankBaseline, blankEdited, "blank.xlsx");
const blankResult = XLSX.read(await blankFile.arrayBuffer(), { type: "array", cellText: true });
assert.equal(
  blankResult.Sheets.Blank.A1.v,
  "first value",
  "editing a workbook whose worksheet uses self-closing sheetData must preserve the package",
);

const liveEditWorkbook = XLSX.utils.book_new();
XLSX.utils.book_append_sheet(
  liveEditWorkbook,
  XLSX.utils.aoa_to_sheet([["Name", "Value"], ["Alpha", 10]]),
  "Live edit",
);
const liveEditInput = XLSX.write(liveEditWorkbook, { type: "array", bookType: "xlsx" });
const liveEditParsed = XLSX.read(liveEditInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const liveEditBaseline = spreadsheetSheetsFromWorkbook(XLSX, liveEditParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const liveEditPayload = serializeEditorLiveSpreadsheetPayload(
  [["Name", "Value"], ["Alpha", 10], ["Delta", 40]],
  [],
  {},
);
const liveEditPayloadData = JSON.parse(
  liveEditPayload.slice(EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX.length),
).data;
const liveEditSheets = structuredClone(liveEditBaseline);
liveEditSheets[0].data = liveEditPayloadData;
const liveEditFile = await preserveSpreadsheetFile(
  liveEditInput,
  liveEditBaseline,
  liveEditSheets,
  "live-edit.xlsx",
);
const liveEditResult = XLSX.read(await liveEditFile.arrayBuffer(), { type: "array", cellText: true });
assert.equal(liveEditResult.Sheets["Live edit"].B3.v, 40);
assert.equal(
  liveEditResult.Sheets["Live edit"].B3.t,
  "n",
  "AI Edit must keep newly added XLSX numbers numeric after Accept and reopen",
);

const sparseWorkbook = XLSX.utils.book_new();
const sparseSheet = XLSX.utils.aoa_to_sheet([["top"]]);
sparseSheet.A10 = { t: "s", v: "bottom" };
sparseSheet["!ref"] = "A1:A10";
XLSX.utils.book_append_sheet(sparseWorkbook, sparseSheet, "Sparse");
const sparseInput = XLSX.write(sparseWorkbook, { type: "array", bookType: "xlsx" });
const sparseParsed = XLSX.read(sparseInput, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const sparseBaseline = spreadsheetSheetsFromWorkbook(XLSX, sparseParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const sparseEdited = structuredClone(sparseBaseline);
sparseEdited[0].data[4][0] = "middle";
const sparseFile = await preserveSpreadsheetFile(sparseInput, sparseBaseline, sparseEdited, "sparse.xlsx");
const sparseZip = await JSZip.loadAsync(await sparseFile.arrayBuffer());
const sparseSheetXml = await sparseZip.file("xl/worksheets/sheet1.xml").async("text");
assert.deepEqual(
  [...sparseSheetXml.matchAll(/<row\b[^>]*\br="(\d+)"/g)].map((match) => Number(match[1])),
  [1, 5, 10],
  "new rows must be inserted in numeric worksheet order",
);

// A worksheet row may carry an extension list after its cells. Adding a cell
// must keep the OOXML sequence valid (c* before extLst), otherwise Excel and
// LibreOffice can reject the otherwise-preserved package.
const extensionWorkbook = XLSX.utils.book_new();
const extensionSheet = XLSX.utils.aoa_to_sheet([["before"]]);
XLSX.utils.book_append_sheet(extensionWorkbook, extensionSheet, "Extension");
const extensionInput = XLSX.write(extensionWorkbook, { type: "array", bookType: "xlsx" });
const extensionInputZip = await JSZip.loadAsync(extensionInput);
const extensionSheetXml = await extensionInputZip.file("xl/worksheets/sheet1.xml").async("text");
extensionInputZip.file(
  "xl/worksheets/sheet1.xml",
  extensionSheetXml.replace(
    /(<row\b[^>]*>)([\s\S]*?)(<\/row>)/i,
    "$1$2<extLst><ext uri=\"{manor-test}\"/></extLst>$3",
  ),
);
const extensionInputBytes = await extensionInputZip.generateAsync({ type: "arraybuffer" });
const extensionWorkbookParsed = XLSX.read(extensionInputBytes, {
  type: "array",
  cellFormula: true,
  cellNF: true,
  cellStyles: true,
  cellText: true,
});
const extensionBaseline = spreadsheetSheetsFromWorkbook(XLSX, extensionWorkbookParsed)
  .map((sheet) => ({ name: sheet.name, data: structuredClone(sheet.data) }));
const extensionEdited = structuredClone(extensionBaseline);
extensionEdited[0].data[0][1] = "after";
const extensionFile = await preserveSpreadsheetFile(
  extensionInputBytes,
  extensionBaseline,
  extensionEdited,
  "extension.xlsx",
);
const extensionOutputZip = await JSZip.loadAsync(await extensionFile.arrayBuffer());
const extensionOutputXml = await extensionOutputZip.file("xl/worksheets/sheet1.xml").async("text");
assert.match(extensionOutputXml, /<c[^>]*r="B1"[\s\S]*?<\/c><extLst>/, "new cells must stay before row extensions");
assert.equal(
  XLSX.read(await extensionFile.arrayBuffer(), { type: "array", cellText: true }).Sheets.Extension.B1.v,
  "after",
  "rows with extension lists should remain readable after editing",
);

const structuralWorkbook = XLSX.utils.book_new();
const structuralDataSheet = XLSX.utils.aoa_to_sheet([
  ["Name", "Value", "Double"],
  ["A", 10, null],
  ["B", 20, null],
]);
structuralDataSheet.C2 = { t: "n", f: "B2*2", v: 20 };
structuralDataSheet.C3 = { t: "n", f: "B3*2", v: 40 };
structuralDataSheet["!ref"] = "A1:C3";
XLSX.utils.book_append_sheet(structuralWorkbook, structuralDataSheet, "Data");
const structuralSummary = XLSX.utils.aoa_to_sheet([[null]]);
structuralSummary.A1 = { t: "n", f: "Data!B2", v: 10 };
structuralSummary.A2 = { t: "n", f: "SUM(DataTable[Name])", v: 0 };
structuralSummary.A3 = { t: "n", f: "SUM(DataTable[Name])-SUM(DataTable[Value])", v: 0 };
structuralSummary.A4 = { t: "n", f: "SUM(DataTable[[#Data],[Name]])", v: 0 };
structuralSummary.A5 = { t: "n", f: 'IF(1=1,"DataTable[Name]",SUM(DataTable[Name]))', v: 0 };
structuralSummary.A6 = { t: "s", v: "Data!B2" };
structuralSummary.A7 = { t: "n", f: 'IF(Data!B2>0,"Data!B2",Data!B2)', v: 10 };
structuralSummary["!ref"] = "A1:A7";
XLSX.utils.book_append_sheet(structuralWorkbook, structuralSummary, "Summary");
structuralWorkbook.Workbook = {
  ...(structuralWorkbook.Workbook || {}),
  Names: [{ Name: "PrimaryValue", Ref: "Data!$B$2" }],
};
const structuralInputBytes = XLSX.write(structuralWorkbook, { type: "array", bookType: "xlsx", cellStyles: true });
const structuralInputZip = await JSZip.loadAsync(structuralInputBytes);
const structuralSheetPart = "xl/worksheets/sheet1.xml";
const structuralSheetXml = await structuralInputZip.file(structuralSheetPart).async("text");
const structuralSheetWithArrayFormula = structuralSheetXml.replace(
  '<c r="C2"><f>B2*2</f>',
  '<c r="C2"><f t="array" ref="C2:C3">B2*2</f>',
);
assert.notEqual(structuralSheetWithArrayFormula, structuralSheetXml, "fixture should contain an array formula range");
const structuralSheetWithTableNamespace = /\bxmlns:r=/.test(structuralSheetWithArrayFormula)
  ? structuralSheetWithArrayFormula
  : structuralSheetWithArrayFormula.replace(
      /<worksheet\b/,
      '<worksheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"',
    );
structuralInputZip.file(
  structuralSheetPart,
  structuralSheetWithTableNamespace.replace(
    /<\/worksheet>\s*$/,
    '<tableParts count="1"><tablePart r:id="rIdTable1"/></tableParts></worksheet>',
  ),
);
structuralInputZip.file(
  "xl/worksheets/_rels/sheet1.xml.rels",
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdTable1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/></Relationships>',
);
structuralInputZip.file(
  "xl/tables/table1.xml",
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="1" name="DataTable" displayName="DataTable" ref="A1:C3" totalsRowShown="0"><autoFilter ref="A1:C3"/><tableColumns count="3"><tableColumn id="1" name="Name"/><tableColumn id="2" name="Value"/><tableColumn id="3" name="Double"><calculatedColumnFormula>[@Value]*2</calculatedColumnFormula></tableColumn></tableColumns><tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" showRowStripes="1" showColumnStripes="0"/></table>',
);
const structuralContentTypes = await structuralInputZip.file("[Content_Types].xml").async("text");
structuralInputZip.file(
  "[Content_Types].xml",
  structuralContentTypes.replace(
    /<\/Types>\s*$/,
    '<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/></Types>',
  ),
);
structuralInputZip.file("customXml/structure-preservation.xml", "<keep>structure</keep>");
const structuralInput = await structuralInputZip.generateAsync({ type: "arraybuffer" });
const structuralParsed = XLSX.read(structuralInput, { type: "array", cellFormula: true, cellStyles: true, cellText: true });
const structuralBaseline = spreadsheetSheetsFromWorkbook(XLSX, structuralParsed).map((sheet) => ({
  name: sheet.name,
  data: structuredClone(sheet.data),
  styles: structuredClone(sheet.styles),
  columnWidths: [...sheet.columnWidths],
  rowHeights: [...sheet.rowHeights],
  merges: structuredClone(sheet.merges),
  editorCharts: [],
  hidden: sheet.hidden,
}));

const renamedTableHeaderSheets = structuredClone(structuralBaseline);
renamedTableHeaderSheets[0].data[0][0] = "Customer";
const renamedTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  renamedTableHeaderSheets,
  "renamed-table-header.xlsx",
);
const renamedTableHeaderZip = await JSZip.loadAsync(await renamedTableHeaderFile.arrayBuffer());
const renamedTableHeaderXml = await renamedTableHeaderZip.file("xl/tables/table1.xml").async("text");
assert.match(
  renamedTableHeaderXml,
  /<tableColumn\b[^>]*\bname="Customer"/,
  "editing a table header cell should update its structured-reference name",
);
const renamedTableHeaderWorkbook = XLSX.read(
  await renamedTableHeaderFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  renamedTableHeaderWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Customer])",
  "renaming a table header should update structured references in other worksheets",
);
assert.equal(
  renamedTableHeaderWorkbook.Sheets.Summary.A3.f,
  "SUM(DataTable[Customer])-SUM(DataTable[Value])",
  "table header renames should leave unrelated structured columns unchanged",
);
assert.equal(
  renamedTableHeaderWorkbook.Sheets.Summary.A4.f,
  "SUM(DataTable[[#Data],[Customer]])",
  "table header renames should update nested structured references",
);
assert.equal(
  renamedTableHeaderWorkbook.Sheets.Summary.A5.f,
  'IF(1=1,"DataTable[Name]",SUM(DataTable[Customer]))',
  "table header renames should not modify text literals that resemble structured references",
);

const renamedTableHeaderBaseline = spreadsheetSheetsFromWorkbook(XLSX, renamedTableHeaderWorkbook);
const formulaEditedAfterHeaderRename = structuredClone(renamedTableHeaderBaseline);
formulaEditedAfterHeaderRename[1].data[1][0] = "=SUM(DataTable[Customer])+1";
const formulaEditedAfterHeaderRenameFile = await preserveSpreadsheetFile(
  await renamedTableHeaderFile.arrayBuffer(),
  renamedTableHeaderBaseline,
  formulaEditedAfterHeaderRename,
  "formula-edited-after-table-header.xlsx",
);
const formulaEditedAfterHeaderRenameWorkbook = XLSX.read(
  await formulaEditedAfterHeaderRenameFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  formulaEditedAfterHeaderRenameWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Customer])+1",
  "editing a formula after a header save should use the formula parsed from the saved workbook",
);

const reorderedTableAttributesInputZip = await JSZip.loadAsync(structuralInput);
const reorderedTableAttributesXml = (await reorderedTableAttributesInputZip.file("xl/tables/table1.xml").async("text"))
  .replace('name="DataTable" displayName="DataTable"', 'displayName="DataTable" name="InternalTable"');
reorderedTableAttributesInputZip.file("xl/tables/table1.xml", reorderedTableAttributesXml);
const reorderedTableAttributesInput = await reorderedTableAttributesInputZip.generateAsync({ type: "arraybuffer" });
const reorderedTableAttributesWorkbook = XLSX.read(
  reorderedTableAttributesInput,
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
const reorderedTableAttributesBaseline = spreadsheetSheetsFromWorkbook(XLSX, reorderedTableAttributesWorkbook);
const reorderedTableAttributesEdited = structuredClone(reorderedTableAttributesBaseline);
reorderedTableAttributesEdited[0].data[0][0] = "Customer";
const reorderedTableAttributesFile = await preserveSpreadsheetFile(
  reorderedTableAttributesInput,
  reorderedTableAttributesBaseline,
  reorderedTableAttributesEdited,
  "reordered-table-attributes.xlsx",
);
const reorderedTableAttributesSavedWorkbook = XLSX.read(
  await reorderedTableAttributesFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  reorderedTableAttributesSavedWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Customer])",
  "structured formulas should use displayName regardless of table attribute order",
);

const worksheetStructuredFormulaInputZip = await JSZip.loadAsync(structuralInput);
const worksheetStructuredFormulaXml = await worksheetStructuredFormulaInputZip.file(structuralSheetPart).async("text");
const worksheetStructuredFormulaFixture = worksheetStructuredFormulaXml.replace(
  /<c\b([^>]*\br="B2"[^>]*)><v>10<\/v><\/c>/,
  '<c$1><f>LEN([@Name])</f><v>1</v></c>',
);
assert.notEqual(
  worksheetStructuredFormulaFixture,
  worksheetStructuredFormulaXml,
  "fixture should contain an unqualified structured formula inside the table range",
);
worksheetStructuredFormulaInputZip.file(structuralSheetPart, worksheetStructuredFormulaFixture);
const worksheetStructuredFormulaInput = await worksheetStructuredFormulaInputZip.generateAsync({ type: "arraybuffer" });
const worksheetStructuredFormulaParsed = XLSX.read(
  worksheetStructuredFormulaInput,
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
const worksheetStructuredFormulaBaseline = spreadsheetSheetsFromWorkbook(XLSX, worksheetStructuredFormulaParsed);
const worksheetStructuredFormulaEdited = structuredClone(worksheetStructuredFormulaBaseline);
worksheetStructuredFormulaEdited[0].data[0][0] = "Customer";
const worksheetStructuredFormulaFile = await preserveSpreadsheetFile(
  worksheetStructuredFormulaInput,
  worksheetStructuredFormulaBaseline,
  worksheetStructuredFormulaEdited,
  "worksheet-structured-formula.xlsx",
);
const worksheetStructuredFormulaWorkbook = XLSX.read(
  await worksheetStructuredFormulaFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  worksheetStructuredFormulaWorkbook.Sheets.Data.B2.f,
  "LEN([@Customer])",
  "renaming a header should update unqualified formulas stored in table worksheet cells",
);

const escapedTableHeaderSheets = structuredClone(structuralBaseline);
escapedTableHeaderSheets[0].data[0][0] = "#Items";
const escapedTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  escapedTableHeaderSheets,
  "escaped-table-header.xlsx",
);
const escapedTableHeaderBuffer = await escapedTableHeaderFile.arrayBuffer();
const escapedTableHeaderWorkbook = XLSX.read(
  escapedTableHeaderBuffer,
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  escapedTableHeaderWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable['#Items])",
  "structured references should escape special column-name characters",
);
const escapedTableHeaderBaseline = spreadsheetSheetsFromWorkbook(XLSX, escapedTableHeaderWorkbook);
const escapedTableHeaderRenamed = structuredClone(escapedTableHeaderBaseline);
escapedTableHeaderRenamed[0].data[0][0] = "Count";
const escapedTableHeaderRenamedFile = await preserveSpreadsheetFile(
  escapedTableHeaderBuffer,
  escapedTableHeaderBaseline,
  escapedTableHeaderRenamed,
  "renamed-escaped-table-header.xlsx",
);
const escapedTableHeaderRenamedWorkbook = XLSX.read(
  await escapedTableHeaderRenamedFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  escapedTableHeaderRenamedWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Count])",
  "a previously escaped structured column name should remain editable",
);

const apostropheTableHeaderSheets = structuredClone(structuralBaseline);
apostropheTableHeaderSheets[0].data[0][0] = "Owner'";
const apostropheTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  apostropheTableHeaderSheets,
  "apostrophe-table-header.xlsx",
);
const apostropheTableHeaderBuffer = await apostropheTableHeaderFile.arrayBuffer();
const apostropheTableHeaderWorkbook = XLSX.read(
  apostropheTableHeaderBuffer,
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  apostropheTableHeaderWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Owner''])",
  "structured references should escape an apostrophe at the end of a column name",
);
const apostropheTableHeaderBaseline = spreadsheetSheetsFromWorkbook(XLSX, apostropheTableHeaderWorkbook);
const apostropheTableHeaderRenamed = structuredClone(apostropheTableHeaderBaseline);
apostropheTableHeaderRenamed[0].data[0][0] = "Owner";
const apostropheTableHeaderRenamedFile = await preserveSpreadsheetFile(
  apostropheTableHeaderBuffer,
  apostropheTableHeaderBaseline,
  apostropheTableHeaderRenamed,
  "renamed-apostrophe-table-header.xlsx",
);
const apostropheTableHeaderRenamedWorkbook = XLSX.read(
  await apostropheTableHeaderRenamedFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  apostropheTableHeaderRenamedWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Owner])",
  "a structured column name ending in an escaped apostrophe should remain editable",
);

const quotedTableHeaderSheets = structuredClone(structuralBaseline);
quotedTableHeaderSheets[0].data[0][0] = 'He said "Hi"';
const quotedTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  quotedTableHeaderSheets,
  "quoted-table-header.xlsx",
);
const quotedTableHeaderBuffer = await quotedTableHeaderFile.arrayBuffer();
const quotedTableHeaderWorkbook = XLSX.read(
  quotedTableHeaderBuffer,
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  quotedTableHeaderWorkbook.Sheets.Summary.A2.f,
  'SUM(DataTable[[He said "Hi"]])',
  "structured references should preserve quotes in a column name",
);
const quotedTableHeaderBaseline = spreadsheetSheetsFromWorkbook(XLSX, quotedTableHeaderWorkbook);
const quotedTableHeaderRenamed = structuredClone(quotedTableHeaderBaseline);
quotedTableHeaderRenamed[0].data[0][0] = "Count";
const quotedTableHeaderRenamedFile = await preserveSpreadsheetFile(
  quotedTableHeaderBuffer,
  quotedTableHeaderBaseline,
  quotedTableHeaderRenamed,
  "renamed-quoted-table-header.xlsx",
);
const quotedTableHeaderRenamedWorkbook = XLSX.read(
  await quotedTableHeaderRenamedFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  quotedTableHeaderRenamedWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[Count])",
  "a structured column name containing quotes should remain editable",
);

const nestedTableHeaderSheets = structuredClone(structuralBaseline);
nestedTableHeaderSheets[0].data[0][0] = "Total $";
const nestedTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  nestedTableHeaderSheets,
  "nested-table-header.xlsx",
);
const nestedTableHeaderWorkbook = XLSX.read(
  await nestedTableHeaderFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  nestedTableHeaderWorkbook.Sheets.Summary.A2.f,
  "SUM(DataTable[[Total $]])",
  "structured references should add an outer specifier for punctuation in column names",
);

const duplicateTableHeaderSheets = structuredClone(structuralBaseline);
duplicateTableHeaderSheets[0].data[0][1] = "Name";
const duplicateTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  duplicateTableHeaderSheets,
  "duplicate-table-header.xlsx",
);
const duplicateTableHeaderZip = await JSZip.loadAsync(await duplicateTableHeaderFile.arrayBuffer());
const duplicateTableHeaderWorkbook = XLSX.read(
  await duplicateTableHeaderFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  duplicateTableHeaderWorkbook.Sheets.Data.B1.v,
  "Name2",
  "deduplicated table column names should be written back to the visible header cell",
);
assert.equal(
  duplicateTableHeaderWorkbook.Sheets.Summary.A3.f,
  "SUM(DataTable[Name])-SUM(DataTable[Name2])",
  "deduplicating a table header should update structured references",
);
assert.match(
  await duplicateTableHeaderZip.file("xl/tables/table1.xml").async("text"),
  /<calculatedColumnFormula>\[@Name2\]\*2<\/calculatedColumnFormula>/,
  "deduplicating a table header should update unqualified references inside the table",
);

const blankTableHeaderSheets = structuredClone(structuralBaseline);
blankTableHeaderSheets[0].data[0][0] = "";
const blankTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  blankTableHeaderSheets,
  "blank-table-header.xlsx",
);
const blankTableHeaderWorkbook = XLSX.read(
  await blankTableHeaderFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  blankTableHeaderWorkbook.Sheets.Data.A1.v,
  "Name",
  "an empty table header should write its fallback name back to the visible header cell",
);

const whitespaceTableHeaderSheets = structuredClone(structuralBaseline);
whitespaceTableHeaderSheets[0].data[0][0] = " Customer ";
const whitespaceTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  whitespaceTableHeaderSheets,
  "whitespace-table-header.xlsx",
);
const whitespaceTableHeaderWorkbook = XLSX.read(
  await whitespaceTableHeaderFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  whitespaceTableHeaderWorkbook.Sheets.Data.A1.v,
  "Customer",
  "trimmed table column names should be written back to their visible header cells",
);

const swappedTableHeaderSheets = structuredClone(structuralBaseline);
swappedTableHeaderSheets[0].data[0][0] = "Value";
swappedTableHeaderSheets[0].data[0][1] = "Name";
const swappedTableHeaderFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  swappedTableHeaderSheets,
  "swapped-table-headers.xlsx",
);
const swappedTableHeaderWorkbook = XLSX.read(
  await swappedTableHeaderFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  swappedTableHeaderWorkbook.Sheets.Summary.A3.f,
  "SUM(DataTable[Value])-SUM(DataTable[Name])",
  "simultaneous table header renames should not cascade through each other",
);
const headerlessTableInputZip = await JSZip.loadAsync(structuralInput);
const headerlessTableXml = (await headerlessTableInputZip.file("xl/tables/table1.xml").async("text"))
  .replace(/<table\b/, '<table headerRowCount="0"')
  .replace(/name="Name"/, 'name="Column1"')
  .replace(/name="Value"/, 'name="Column2"')
  .replace(/name="Double"/, 'name="Column3"');
headerlessTableInputZip.file("xl/tables/table1.xml", headerlessTableXml);
const headerlessTableInput = await headerlessTableInputZip.generateAsync({ type: "arraybuffer" });
const headerlessEditedSheets = structuredClone(structuralBaseline);
headerlessEditedSheets[0].data[1][0] = "Edited data";
const headerlessTableFile = await preserveSpreadsheetFile(
  headerlessTableInput,
  structuralBaseline,
  headerlessEditedSheets,
  "headerless-table.xlsx",
);
const headerlessTableOutputZip = await JSZip.loadAsync(await headerlessTableFile.arrayBuffer());
assert.match(
  await headerlessTableOutputZip.file("xl/tables/table1.xml").async("text"),
  /<tableColumn\b[^>]*\bname="Column1"/,
  "editing table data must not treat the first row of a headerless table as column names",
);

const insertedRowSheets = structuredClone(structuralBaseline);
insertedRowSheets[0].data.splice(1, 0, ["Inserted row", "", ""]);
insertedRowSheets[0].structureOperations = [{ axis: "row", index: 1, deleteCount: 0, insertCount: 1 }];
const insertedRowFile = await preserveSpreadsheetFile(structuralInput, structuralBaseline, insertedRowSheets, "inserted-row.xlsx");
const insertedRowZip = await JSZip.loadAsync(await insertedRowFile.arrayBuffer());
const insertedRowTableXml = await insertedRowZip.file("xl/tables/table1.xml").async("text");
assert.equal(await insertedRowZip.file("customXml/structure-preservation.xml").async("text"), "<keep>structure</keep>");
assert.match(insertedRowTableXml, /<table\b[^>]*\bref="A1:C4"/);
assert.match(insertedRowTableXml, /<autoFilter\b[^>]*\bref="A1:C4"/);
assert.match(
  await insertedRowZip.file(structuralSheetPart).async("text"),
  /<f\b(?=[^>]*\bt="array")(?=[^>]*\bref="C3:C4")[^>]*>B3\*2<\/f>/,
  "row insertion should update the array formula declaration range",
);
const insertedRowWorkbook = XLSX.read(await insertedRowFile.arrayBuffer(), { type: "array", cellFormula: true, cellStyles: true, cellText: true });
assert.equal(insertedRowWorkbook.Sheets.Data.A2.v, "Inserted row");
assert.equal(insertedRowWorkbook.Sheets.Data.C3.f, "B3*2", "row insertion should update same-sheet formulas");
assert.equal(insertedRowWorkbook.Sheets.Summary.A1.f, "Data!B3", "row insertion should update cross-sheet formulas");
assert.equal(insertedRowWorkbook.Sheets.Summary.A6.v, "Data!B2", "row insertion must not rewrite ordinary cell text that resembles a formula reference");
assert.equal(
  insertedRowWorkbook.Sheets.Summary.A7.f,
  'IF(Data!B3>0,"Data!B2",Data!B3)',
  "row insertion must update formula references without rewriting string literals",
);
assert.equal(insertedRowWorkbook.Workbook.Names.find((name) => name.Name === "PrimaryValue").Ref, "Data!$B$3");

const insertedColumnSheets = structuredClone(structuralBaseline);
insertedColumnSheets[0].data.forEach((row) => row.splice(1, 0, ""));
insertedColumnSheets[0].data[0][1] = "Inserted column";
insertedColumnSheets[0].structureOperations = [{ axis: "column", index: 1, deleteCount: 0, insertCount: 1 }];
const insertedColumnFile = await preserveSpreadsheetFile(structuralInput, structuralBaseline, insertedColumnSheets, "inserted-column.xlsx");
const insertedColumnZip = await JSZip.loadAsync(await insertedColumnFile.arrayBuffer());
const insertedColumnTableXml = await insertedColumnZip.file("xl/tables/table1.xml").async("text");
assert.match(
  await insertedColumnZip.file(structuralSheetPart).async("text"),
  /<f\b(?=[^>]*\bt="array")(?=[^>]*\bref="D2:D3")[^>]*>C2\*2<\/f>/,
  "column insertion should update the array formula declaration range",
);
assert.match(insertedColumnTableXml, /<table\b[^>]*\bref="A1:D3"/);
assert.match(insertedColumnTableXml, /<tableColumns\b[^>]*\bcount="4"/);
assert.match(insertedColumnTableXml, /<tableColumn\b[^>]*\bname="Inserted column"/);
assert.equal(await insertedColumnZip.file("customXml/structure-preservation.xml").async("text"), "<keep>structure</keep>");
const insertedColumnWorkbook = XLSX.read(await insertedColumnFile.arrayBuffer(), { type: "array", cellFormula: true, cellStyles: true, cellText: true });
assert.equal(insertedColumnWorkbook.Sheets.Data.B1.v, "Inserted column");
assert.equal(insertedColumnWorkbook.Sheets.Data.D2.f, "C2*2", "column insertion should update same-sheet formulas");
assert.equal(insertedColumnWorkbook.Sheets.Summary.A1.f, "Data!C2", "column insertion should update cross-sheet formulas");
assert.equal(insertedColumnWorkbook.Workbook.Names.find((name) => name.Name === "PrimaryValue").Ref, "Data!$C$2");

const deletedRowSheets = structuredClone(structuralBaseline);
deletedRowSheets[0].data.splice(1, 1);
deletedRowSheets[0].structureOperations = [{ axis: "row", index: 1, deleteCount: 1, insertCount: 0 }];
const deletedRowFile = await preserveSpreadsheetFile(structuralInput, structuralBaseline, deletedRowSheets, "deleted-row.xlsx");
const deletedRowWorkbook = XLSX.read(await deletedRowFile.arrayBuffer(), { type: "array", cellFormula: true, cellStyles: true, cellText: true });
assert.equal(deletedRowWorkbook.Sheets.Data.A2.v, "B");
assert.equal(deletedRowWorkbook.Sheets.Data.C2.f, "B2*2", "row deletion should move and update same-sheet formulas");
assert.match(deletedRowWorkbook.Sheets.Summary.A1.f, /#REF!/, "references into a deleted row should become reference errors");

const editedThenDeletedRowSheets = structuredClone(structuralBaseline);
editedThenDeletedRowSheets[0].data[1][0] = "A edited";
editedThenDeletedRowSheets[0].data.splice(2, 1);
editedThenDeletedRowSheets[0].structureOperations = [{
  axis: "row",
  index: 2,
  deleteCount: 1,
  insertCount: 0,
}];
const editedThenDeletedRowFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  editedThenDeletedRowSheets,
  "edited-then-deleted-row.xlsx",
);
const editedThenDeletedRowZip = await JSZip.loadAsync(await editedThenDeletedRowFile.arrayBuffer());
assert.match(
  await editedThenDeletedRowZip.file(structuralSheetPart).async("text"),
  /<f\b(?=[^>]*\bt="array")(?=[^>]*\bref="C2:C2")[^>]*>B2\*2<\/f>/,
  "deleting the end of an array formula range should shrink its declaration",
);
const editedThenDeletedRowWorkbook = XLSX.read(
  await editedThenDeletedRowFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(editedThenDeletedRowWorkbook.Sheets.Data.A2.v, "A edited");
assert.equal(
  editedThenDeletedRowWorkbook.Sheets.Data.C2.f,
  "B2*2",
  "explicit row deletion should not move formulas from an earlier edited row",
);
assert.equal(
  editedThenDeletedRowWorkbook.Sheets.Summary.A1.f,
  "Data!B2",
  "editing row 2 before deleting row 3 must not invalidate the row 2 reference",
);

const pastedAndExtendedSheets = structuredClone(structuralBaseline);
pastedAndExtendedSheets[0].structureOperations = [];
pastedAndExtendedSheets[0].data[1][0] = "A pasted";
pastedAndExtendedSheets[0].data[1][1] = 11;
pastedAndExtendedSheets[0].data.push(["C", 30, ""]);
const pastedAndExtendedFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  pastedAndExtendedSheets,
  "pasted-and-extended.xlsx",
);
const pastedAndExtendedWorkbook = XLSX.read(
  await pastedAndExtendedFile.arrayBuffer(),
  { type: "array", cellFormula: true, cellStyles: true, cellText: true },
);
assert.equal(
  pastedAndExtendedWorkbook.Sheets.Summary.A1.f,
  "Data!B2",
  "ordinary paste growth must not be inferred as a structural row insertion",
);

const deletedColumnSheets = structuredClone(structuralBaseline);
deletedColumnSheets[0].data.forEach((row) => row.splice(0, 1));
deletedColumnSheets[0].structureOperations = [{ axis: "column", index: 0, deleteCount: 1, insertCount: 0 }];
const deletedColumnFile = await preserveSpreadsheetFile(structuralInput, structuralBaseline, deletedColumnSheets, "deleted-column.xlsx");
const deletedColumnWorkbook = XLSX.read(await deletedColumnFile.arrayBuffer(), { type: "array", cellFormula: true, cellStyles: true, cellText: true });
assert.equal(deletedColumnWorkbook.Sheets.Data.A2.v, 10);
assert.equal(deletedColumnWorkbook.Sheets.Data.B2.f, "A2*2", "column deletion should move and update same-sheet formulas");
assert.equal(deletedColumnWorkbook.Sheets.Summary.A1.f, "Data!A2", "column deletion should update cross-sheet formulas");
assert.equal(
  deletedColumnWorkbook.Sheets.Summary.A2.f,
  "SUM(#REF!)",
  "deleting a table column should invalidate qualified structured references to it",
);

const deletedCalculatedColumnSourceSheets = structuredClone(structuralBaseline);
deletedCalculatedColumnSourceSheets[0].data.forEach((row) => row.splice(1, 1));
deletedCalculatedColumnSourceSheets[0].structureOperations = [{ axis: "column", index: 1, deleteCount: 1, insertCount: 0 }];
const deletedCalculatedColumnSourceFile = await preserveSpreadsheetFile(
  structuralInput,
  structuralBaseline,
  deletedCalculatedColumnSourceSheets,
  "deleted-calculated-column-source.xlsx",
);
const deletedCalculatedColumnSourceZip = await JSZip.loadAsync(await deletedCalculatedColumnSourceFile.arrayBuffer());
assert.match(
  await deletedCalculatedColumnSourceZip.file("xl/tables/table1.xml").async("text"),
  /<calculatedColumnFormula>#REF!\*2<\/calculatedColumnFormula>/,
  "deleting a table column should invalidate unqualified references inside calculated-column formulas",
);

const styledSheets = structuredClone(structuralBaseline);
styledSheets[0].styles["0:0"] = {
  ...(styledSheets[0].styles["0:0"] || {}),
  bold: true,
  italic: true,
  fontFamily: "Arial",
  fontSize: 18,
  color: "#1d4ed8",
  fill: "#dbeafe",
  align: "center",
};
styledSheets[0].editorCharts = [{
  id: "editor-chart-1",
  type: "bar",
  title: "Values",
  labelColumn: 0,
  valueColumn: 1,
  startRow: 1,
  endRow: 2,
}];
const styledFile = await preserveSpreadsheetFile(structuralInput, structuralBaseline, styledSheets, "styled.xlsx");
const styledZip = await JSZip.loadAsync(await styledFile.arrayBuffer());
assert.equal(await styledZip.file("customXml/structure-preservation.xml").async("text"), "<keep>structure</keep>");
assert.match(await styledZip.file("xl/styles.xml").async("text"), /rgb="FF1D4ED8"/);
assert.doesNotMatch(await styledZip.file("xl/styles.xml").async("text"), /<\/xf\s+/, "attributes belong to the opening xf tag, never its closing tag");
const styledWorkbook = XLSX.read(await styledFile.arrayBuffer(), { type: "array", cellFormula: true, cellStyles: true, cellText: true });
assert.ok(styledWorkbook.SheetNames.includes("_manor_charts"), "editor charts should be stored without rebuilding the workbook");
assert.equal(styledWorkbook.Workbook.Sheets.find((sheet) => sheet.name === "_manor_charts").Hidden, 1);
const metadataText = Object.values(styledWorkbook.Sheets._manor_charts)
  .filter((cell) => cell && typeof cell === "object" && "v" in cell)
  .map((cell) => String(cell.v))
  .join("");
assert.equal(JSON.parse(metadataText).charts[0].title, "Values");
assert.equal(spreadsheetSheetsFromWorkbook(XLSX, styledWorkbook)[0].styles["0:0"].fill.toLowerCase(), "#dbeafe");

const nativeStyleBytes = await styledFile.arrayBuffer();
const nativeStyleSheets = await spreadsheetSheetsFromFile(XLSX, styledWorkbook, nativeStyleBytes);
assert.equal(nativeStyleSheets[0].styles["0:0"].bold, true, "import native font properties rather than only SheetJS fill styles");
assert.equal(nativeStyleSheets[0].styles["0:0"].fontFamily, "Arial");
assert.equal(nativeStyleSheets[0].styles["0:0"].fontSize, 18);
assert.equal(nativeStyleSheets[0].styles["0:0"].align, "center");
assert.equal(spreadsheetCellVisualStyle(nativeStyleSheets[0].styles["0:0"]).fontSize, "18pt");
assert.equal(spreadsheetCellVisualStyle({ fontFamily: "Calibri" }).fontFamily, '"Carlito", sans-serif');
const layoutZip = await JSZip.loadAsync(nativeStyleBytes);
const nativeLayoutXml = await layoutZip.file("xl/worksheets/sheet1.xml").async("text");
layoutZip.file("xl/worksheets/sheet1.xml", nativeLayoutXml.replace(/<((?:[A-Za-z_][\w.-]*:)?sheetView)\b/, '<$1 showGridLines="0"'));
const nativeStylesXml = await layoutZip.file("xl/styles.xml").async("text");
layoutZip.file("xl/styles.xml", nativeStylesXml
  .replace(/<patternFill patternType="solid">[\s\S]*?<\/patternFill>/, '<patternFill patternType="solid"><fgColor theme="4" tint="0.4"/><bgColor indexed="64"/></patternFill>')
  .replace(/<((?:[A-Za-z_][\w.-]*:)?border)\b[^>]*>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?border>/, '<border><left/><right/><top/><bottom style="medium"><color theme="4" tint="0.4"/></bottom></border>'));
const nativeThemeXml = await layoutZip.file("xl/theme/theme1.xml").async("text");
layoutZip.file("xl/theme/theme1.xml", nativeThemeXml.replace(/<a:accent1>[\s\S]*?<\/a:accent1>/, '<a:accent1><a:srgbClr val="4472C4"/></a:accent1>'));
const layoutBytes = await layoutZip.generateAsync({ type: "arraybuffer" });
const layoutWorkbook = XLSX.read(layoutBytes, { type: "array", cellFormula: true, cellStyles: true });
const layoutSheets = await spreadsheetSheetsFromFile(XLSX, layoutWorkbook, layoutBytes);
assert.equal(layoutSheets[0].showGridlines, false);
assert.equal(layoutSheets[0].styles["0:0"].fill, "#8FAADC");
assert.equal(layoutSheets[0].styles["0:0"].borderBottom, "2px solid #8FAADC");
const layoutEdited = structuredClone(layoutSheets);
layoutEdited[0].data[0][0] = "Edited template title";
const layoutSaved = await preserveSpreadsheetFile(layoutBytes, layoutSheets, layoutEdited, "layout.xlsx");
const savedLayoutZip = await JSZip.loadAsync(await layoutSaved.arrayBuffer());
assert.equal(await savedLayoutZip.file("xl/styles.xml").async("text"), await layoutZip.file("xl/styles.xml").async("text"), "content-only edit retains the original native style table");
assert.match(await savedLayoutZip.file("xl/worksheets/sheet1.xml").async("text"), /showGridLines="0"/);

console.log("spreadsheet editor OOXML preservation test passed");
