import JSZip from "jszip";

export type SpreadsheetCellValue = string | number | boolean | null;

export interface SpreadsheetCellStyle {
  bold?: boolean;
  italic?: boolean;
  fontSize?: number;
  fontFamily?: string;
  color?: string;
  fill?: string;
  align?: "left" | "center" | "right";
}

export interface SpreadsheetRange {
  s: { r: number; c: number };
  e: { r: number; c: number };
}

export interface SpreadsheetChartSeries {
  name: string;
  categories: string[];
  values: number[];
}

export interface SpreadsheetChartModel {
  id: string;
  type: "bar" | "line" | "pie";
  title: string;
  series: SpreadsheetChartSeries[];
  anchor?: SpreadsheetRange;
}

export interface SpreadsheetSheetModel {
  name: string;
  data: SpreadsheetCellValue[][];
  displayData: string[][];
  styles: Record<string, SpreadsheetCellStyle>;
  columnWidths: number[];
  rowHeights: number[];
  merges: SpreadsheetRange[];
  charts: SpreadsheetChartModel[];
  hidden: boolean;
}

export interface SpreadsheetSheetSnapshot {
  name: string;
  data: SpreadsheetCellValue[][];
}

export function spreadsheetMergeAt(
  merges: SpreadsheetRange[],
  row: number,
  column: number,
): { covered: boolean; rowSpan: number; columnSpan: number } {
  const merge = merges.find((range) => (
    row >= range.s.r && row <= range.e.r && column >= range.s.c && column <= range.e.c
  ));
  if (!merge) return { covered: false, rowSpan: 1, columnSpan: 1 };
  if (merge.s.r !== row || merge.s.c !== column) return { covered: true, rowSpan: 1, columnSpan: 1 };
  return {
    covered: false,
    rowSpan: merge.e.r - merge.s.r + 1,
    columnSpan: merge.e.c - merge.s.c + 1,
  };
}

export class SpreadsheetPreservationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SpreadsheetPreservationError";
  }
}

function styleKey(row: number, column: number): string {
  return `${row}:${column}`;
}

function colorHex(value: unknown): string | undefined {
  if (!value || typeof value !== "object") return undefined;
  const color = value as { rgb?: unknown; indexed?: unknown };
  if (typeof color.rgb === "string" && /^[0-9a-f]{6,8}$/i.test(color.rgb)) {
    return `#${color.rgb.slice(-6)}`;
  }
  return undefined;
}

function isDarkColor(value: string): boolean {
  const match = value.match(/^#([0-9a-f]{6})$/i);
  if (!match) return false;
  const red = Number.parseInt(match[1].slice(0, 2), 16);
  const green = Number.parseInt(match[1].slice(2, 4), 16);
  const blue = Number.parseInt(match[1].slice(4, 6), 16);
  return (red * 0.299 + green * 0.587 + blue * 0.114) < 135;
}

function sheetBounds(XLSX: any, worksheet: any): { rows: number; columns: number } {
  const reference = typeof worksheet?.["!ref"] === "string" ? worksheet["!ref"] : "A1";
  const decoded = XLSX.utils.decode_range(reference);
  const rows = Math.min(10_000, Math.max(1, Number(decoded?.e?.r || 0) + 1));
  const columns = Math.min(512, Math.max(1, Number(decoded?.e?.c || 0) + 1));
  return { rows, columns };
}

function cellStyle(cell: any): SpreadsheetCellStyle {
  const source = cell?.s && typeof cell.s === "object" ? cell.s : {};
  const font = source.font && typeof source.font === "object" ? source.font : {};
  const fill = source.patternType === "solid" ? colorHex(source.fgColor) : undefined;
  const color = colorHex(font.color) || (fill && isDarkColor(fill) ? "#ffffff" : undefined);
  const horizontal = source.alignment?.horizontal;
  const align = horizontal === "center" || horizontal === "right" || horizontal === "left"
    ? horizontal
    : undefined;
  return {
    ...(font.bold ? { bold: true } : {}),
    ...(font.italic ? { italic: true } : {}),
    ...(Number.isFinite(Number(font.sz)) ? { fontSize: Number(font.sz) } : {}),
    ...(typeof font.name === "string" && font.name ? { fontFamily: font.name } : {}),
    ...(color ? { color } : {}),
    ...(fill ? { fill } : {}),
    ...(align ? { align } : {}),
  };
}

/**
 * Build the shared lightweight browser model used by both the viewer and the
 * editor. SheetJS is passed in by callers so this module adds no dependency or
 * second workbook parser to the bundle.
 */
export function spreadsheetSheetsFromWorkbook(XLSX: any, workbook: any): SpreadsheetSheetModel[] {
  const workbookSheets = Array.isArray(workbook?.Workbook?.Sheets) ? workbook.Workbook.Sheets : [];
  return (workbook?.SheetNames || []).map((name: string, sheetIndex: number) => {
    const worksheet = workbook.Sheets[name];
    const { rows, columns } = sheetBounds(XLSX, worksheet);
    const data: SpreadsheetCellValue[][] = Array.from({ length: rows }, () => Array(columns).fill(null));
    const displayData: string[][] = Array.from({ length: rows }, () => Array(columns).fill(""));
    const styles: Record<string, SpreadsheetCellStyle> = {};

    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < columns; column += 1) {
        const ref = XLSX.utils.encode_cell({ r: row, c: column });
        const cell = worksheet?.[ref];
        if (!cell) continue;
        data[row][column] = typeof cell.f === "string" ? `=${cell.f}` : (cell.v ?? null);
        displayData[row][column] = cell.w != null
          ? String(cell.w)
          : cell.v != null
            ? String(cell.v)
            : "";
        const visual = cellStyle(cell);
        if (Object.keys(visual).length > 0) styles[styleKey(row, column)] = visual;
      }
    }

    const columnWidths = Array.from({ length: columns }, (_, column) => {
      const source = worksheet?.["!cols"]?.[column];
      const width = Number(source?.wpx || (Number(source?.wch) + 1) * 7 || 112);
      return Math.max(48, Math.min(420, Number.isFinite(width) ? width : 112));
    });
    const rowHeights = Array.from({ length: rows }, (_, row) => {
      const source = worksheet?.["!rows"]?.[row];
      const height = Number(source?.hpx || (Number(source?.hpt) * 96) / 72 || 32);
      return Math.max(22, Math.min(240, Number.isFinite(height) ? height : 32));
    });
    const merges = Array.isArray(worksheet?.["!merges"])
      ? worksheet["!merges"].map((merge: SpreadsheetRange) => ({
          s: { r: Number(merge.s.r), c: Number(merge.s.c) },
          e: { r: Number(merge.e.r), c: Number(merge.e.c) },
        }))
      : [];

    return {
      name,
      data,
      displayData,
      styles,
      columnWidths,
      rowHeights,
      merges,
      charts: [],
      hidden: Number(workbookSheets[sheetIndex]?.Hidden || 0) !== 0,
    };
  });
}

function decodeXml(value: string): string {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&#(\d+);/g, (_match, decimal) => String.fromCodePoint(Number(decimal)))
    .replace(/&#x([0-9a-f]+);/gi, (_match, hex) => String.fromCodePoint(Number.parseInt(hex, 16)));
}

function escapeXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function normalizePartPath(value: string): string {
  const output: string[] = [];
  for (const part of value.replace(/\\/g, "/").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") output.pop();
    else output.push(part);
  }
  return output.join("/");
}

export function resolveSpreadsheetPartTarget(sourcePart: string, target: string): string {
  const decodedTarget = decodeXml(target);
  if (decodedTarget.startsWith("/")) return normalizePartPath(decodedTarget.slice(1));
  const sourceDirectory = sourcePart.includes("/") ? sourcePart.slice(0, sourcePart.lastIndexOf("/") + 1) : "";
  return normalizePartPath(`${sourceDirectory}${decodedTarget}`);
}

function relationshipMap(xml: string): Map<string, string> {
  const relationships = new Map<string, string>();
  for (const item of xml.match(/<Relationship\b[^>]*\/?\s*>/g) || []) {
    if (/\bTargetMode="External"/i.test(item)) continue;
    const id = item.match(/\bId="([^"]+)"/i)?.[1];
    const target = item.match(/\bTarget="([^"]+)"/i)?.[1];
    if (id && target) relationships.set(id, target);
  }
  return relationships;
}

function relationshipsPart(sourcePart: string): string {
  const slash = sourcePart.lastIndexOf("/");
  const directory = slash >= 0 ? sourcePart.slice(0, slash + 1) : "";
  const filename = slash >= 0 ? sourcePart.slice(slash + 1) : sourcePart;
  return `${directory}_rels/${filename}.rels`;
}

async function workbookSheetParts(zip: JSZip): Promise<Map<string, string>> {
  const workbookPart = "xl/workbook.xml";
  const workbookXmlFile = zip.file(workbookPart);
  const relationshipsFile = zip.file("xl/_rels/workbook.xml.rels");
  if (!workbookXmlFile || !relationshipsFile) {
    throw new SpreadsheetPreservationError("This workbook is missing its workbook relationships.");
  }
  const [workbookXml, relationshipsXml] = await Promise.all([
    workbookXmlFile.async("text"),
    relationshipsFile.async("text"),
  ]);
  const relationships = relationshipMap(relationshipsXml);
  const parts = new Map<string, string>();
  for (const sheet of workbookXml.match(/<(?:[A-Za-z_][\w.-]*:)?sheet\b[^>]*\/?\s*>/g) || []) {
    const name = sheet.match(/\bname="([^"]*)"/i)?.[1];
    const relationshipId = sheet.match(/\br:id="([^"]+)"/i)?.[1];
    const target = relationshipId ? relationships.get(relationshipId) : undefined;
    if (name != null && target) parts.set(decodeXml(name), resolveSpreadsheetPartTarget(workbookPart, target));
  }
  return parts;
}

function elementBlock(xml: string, localName: string): string | null {
  const pattern = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${localName}\\b[^>]*>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?${localName}>`,
    "i",
  );
  return xml.match(pattern)?.[0] || null;
}

function elementBlocks(xml: string, localName: string): string[] {
  const pattern = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${localName}\\b[^>]*>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?${localName}>`,
    "gi",
  );
  return xml.match(pattern) || [];
}

function elementText(xml: string | null, localName: string): string {
  if (!xml) return "";
  const pattern = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${localName}\\b[^>]*>([\\s\\S]*?)<\\/(?:[A-Za-z_][\\w.-]*:)?${localName}>`,
    "i",
  );
  return decodeXml(xml.match(pattern)?.[1] || "");
}

function formulaRangeValues(
  XLSX: any,
  workbook: any,
  formula: string,
  fallbackSheetName: string,
  display: boolean,
): Array<string | number> {
  const normalized = decodeXml(formula).replace(/^=/, "");
  const match = normalized.match(/^(?:'((?:''|[^'])+)'|([^!]+))!(\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)$/i);
  const sheetName = (match?.[1]?.replace(/''/g, "'") || match?.[2] || fallbackSheetName).trim();
  const rangeText = (match?.[3] || normalized).replace(/\$/g, "");
  const worksheet = workbook?.Sheets?.[sheetName];
  if (!worksheet) return [];
  let range: { s: { r: number; c: number }; e: { r: number; c: number } };
  try {
    range = XLSX.utils.decode_range(rangeText);
  } catch {
    return [];
  }
  const values: Array<string | number> = [];
  for (let row = range.s.r; row <= range.e.r; row += 1) {
    for (let column = range.s.c; column <= range.e.c; column += 1) {
      const cell = worksheet[XLSX.utils.encode_cell({ r: row, c: column })];
      if (display) values.push(cell?.w != null ? String(cell.w) : cell?.v != null ? String(cell.v) : "");
      else values.push(Number(cell?.v));
    }
  }
  return values;
}

function chartAnchor(block: string): SpreadsheetRange | undefined {
  const from = elementBlock(block, "from");
  const to = elementBlock(block, "to");
  if (!from || !to) return undefined;
  return {
    s: { r: Number(elementText(from, "row") || 0), c: Number(elementText(from, "col") || 0) },
    e: { r: Number(elementText(to, "row") || 0), c: Number(elementText(to, "col") || 0) },
  };
}

function parseChartModel(
  XLSX: any,
  workbook: any,
  chartXml: string,
  sheetName: string,
  id: string,
  anchor?: SpreadsheetRange,
): SpreadsheetChartModel | null {
  const chartTypes = ["barChart", "lineChart", "pieChart"] as const;
  const matchedType = chartTypes.find((name) => elementBlock(chartXml, name));
  if (!matchedType) return null;
  const chartBlock = elementBlock(chartXml, matchedType)!;
  const type: SpreadsheetChartModel["type"] = matchedType === "lineChart" ? "line" : matchedType === "pieChart" ? "pie" : "bar";
  const titleBlock = elementBlock(elementBlock(chartXml, "title") || "", "rich");
  const title = elementBlocks(titleBlock || "", "t").map((block) => elementText(block, "t")).join("") || "Chart";
  const series = elementBlocks(chartBlock, "ser").flatMap((seriesBlock, seriesIndex) => {
    const tx = elementBlock(seriesBlock, "tx");
    const txFormula = elementText(elementBlock(tx || "", "strRef"), "f");
    const formulaName = txFormula
      ? formulaRangeValues(XLSX, workbook, txFormula, sheetName, true)[0]
      : undefined;
    const name = String(formulaName ?? (elementText(tx, "v") || `Series ${seriesIndex + 1}`));
    const categoryFormula = elementText(elementBlock(seriesBlock, "cat"), "f");
    const valueFormula = elementText(elementBlock(seriesBlock, "val"), "f");
    const categories = formulaRangeValues(XLSX, workbook, categoryFormula, sheetName, true).map(String);
    const values = formulaRangeValues(XLSX, workbook, valueFormula, sheetName, false)
      .map((value) => Number.isFinite(Number(value)) ? Number(value) : 0);
    return values.length > 0 || categories.length > 0 ? [{ name, categories, values }] : [];
  });
  if (series.length === 0) return null;
  return { id, type, title, series, anchor };
}

/** Resolve and read native SpreadsheetML chart parts without converting them. */
export async function spreadsheetChartsFromFile(
  source: ArrayBuffer,
  XLSX: any,
  workbook: any,
): Promise<Map<string, SpreadsheetChartModel[]>> {
  const zip = await JSZip.loadAsync(source);
  const sheetParts = await workbookSheetParts(zip);
  const chartsBySheet = new Map<string, SpreadsheetChartModel[]>();
  for (const [sheetName, sheetPart] of sheetParts) {
    const sheetFile = zip.file(sheetPart);
    const sheetRelationshipsFile = zip.file(relationshipsPart(sheetPart));
    if (!sheetFile || !sheetRelationshipsFile) continue;
    const [sheetXml, sheetRelationshipsXml] = await Promise.all([
      sheetFile.async("text"),
      sheetRelationshipsFile.async("text"),
    ]);
    const sheetRelationships = relationshipMap(sheetRelationshipsXml);
    const drawingIds = [...sheetXml.matchAll(/<(?:[A-Za-z_][\w.-]*:)?drawing\b[^>]*\br:id="([^"]+)"[^>]*\/?\s*>/gi)]
      .map((match) => match[1]);
    const sheetCharts: SpreadsheetChartModel[] = [];
    for (const drawingId of drawingIds) {
      const drawingTarget = sheetRelationships.get(drawingId);
      if (!drawingTarget) continue;
      const drawingPart = resolveSpreadsheetPartTarget(sheetPart, drawingTarget);
      const drawingFile = zip.file(drawingPart);
      const drawingRelationshipsFile = zip.file(relationshipsPart(drawingPart));
      if (!drawingFile || !drawingRelationshipsFile) continue;
      const [drawingXml, drawingRelationshipsXml] = await Promise.all([
        drawingFile.async("text"),
        drawingRelationshipsFile.async("text"),
      ]);
      const drawingRelationships = relationshipMap(drawingRelationshipsXml);
      const anchors = [
        ...elementBlocks(drawingXml, "twoCellAnchor"),
        ...elementBlocks(drawingXml, "oneCellAnchor"),
        ...elementBlocks(drawingXml, "absoluteAnchor"),
      ];
      for (let anchorIndex = 0; anchorIndex < anchors.length; anchorIndex += 1) {
        const anchorBlock = anchors[anchorIndex];
        const chartRelationshipId = anchorBlock.match(/<(?:[A-Za-z_][\w.-]*:)?chart\b[^>]*\br:id="([^"]+)"/i)?.[1];
        const chartTarget = chartRelationshipId ? drawingRelationships.get(chartRelationshipId) : undefined;
        if (!chartTarget) continue;
        const chartPart = resolveSpreadsheetPartTarget(drawingPart, chartTarget);
        const chartFile = zip.file(chartPart);
        if (!chartFile) continue;
        const chart = parseChartModel(
          XLSX,
          workbook,
          await chartFile.async("text"),
          sheetName,
          `${drawingPart}:${anchorIndex}`,
          chartAnchor(anchorBlock),
        );
        if (chart) sheetCharts.push(chart);
      }
    }
    chartsBySheet.set(sheetName, sheetCharts);
  }
  return chartsBySheet;
}

function cellReference(row: number, column: number): string {
  let label = "";
  let index = column;
  while (index >= 0) {
    label = String.fromCharCode(65 + (index % 26)) + label;
    index = Math.floor(index / 26) - 1;
  }
  return `${label}${row + 1}`;
}

function normalizeComparable(value: SpreadsheetCellValue | undefined): SpreadsheetCellValue {
  return value == null || value === "" ? "" : value;
}

function sameCellValue(left: SpreadsheetCellValue | undefined, right: SpreadsheetCellValue | undefined): boolean {
  return Object.is(normalizeComparable(left), normalizeComparable(right));
}

function sheetDimensions(data: SpreadsheetCellValue[][]): { rows: number; columns: number } {
  return {
    rows: data.length,
    columns: Math.max(1, ...data.map((row) => row.length)),
  };
}

function openingCellAttributes(cellXml: string | undefined, reference: string): string {
  const opening = cellXml?.match(/^<(?:[A-Za-z_][\w.-]*:)?c\b([^>]*)/i)?.[1] || ` r="${reference}"`;
  const kept = (opening.match(/\s+[A-Za-z_:][\w:.-]*="[^"]*"/g) || [])
    .filter((attribute) => !/^\s+t=/i.test(attribute) && !/^\s+r=/i.test(attribute));
  return ` r="${reference}"${kept.join("")}`;
}

function strictNumber(value: string): number | null {
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function replacementCellXml(
  reference: string,
  value: SpreadsheetCellValue,
  originalCellXml?: string,
  fallbackPrefix = "",
): string {
  const prefix = originalCellXml?.match(/^<([A-Za-z_][\w.-]*:)?c\b/i)?.[1] || fallbackPrefix;
  const attributes = openingCellAttributes(originalCellXml, reference);
  if (value == null || value === "") return `<${prefix}c${attributes}/>`;
  if (typeof value === "string" && value.startsWith("=")) {
    return `<${prefix}c${attributes}><${prefix}f>${escapeXml(value.slice(1))}</${prefix}f></${prefix}c>`;
  }
  if (typeof value === "boolean") {
    return `<${prefix}c${attributes} t="b"><${prefix}v>${value ? 1 : 0}</${prefix}v></${prefix}c>`;
  }
  if (typeof value === "number") {
    return `<${prefix}c${attributes}><${prefix}v>${String(value)}</${prefix}v></${prefix}c>`;
  }

  const originalHasNumericValue = Boolean(originalCellXml && !/\bt="(?:s|str|inlineStr|b|e)"/i.test(originalCellXml));
  const numericValue = originalHasNumericValue ? strictNumber(value) : null;
  if (numericValue != null) return `<${prefix}c${attributes}><${prefix}v>${String(numericValue)}</${prefix}v></${prefix}c>`;
  const space = /^\s|\s$/.test(value) ? ' xml:space="preserve"' : "";
  return `<${prefix}c${attributes} t="inlineStr"><${prefix}is><${prefix}t${space}>${escapeXml(value)}</${prefix}t></${prefix}is></${prefix}c>`;
}

function cellColumn(reference: string): number {
  const letters = reference.match(/^[A-Z]+/i)?.[0]?.toUpperCase() || "A";
  let value = 0;
  for (const letter of letters) value = value * 26 + letter.charCodeAt(0) - 64;
  return value - 1;
}

function patchRowXml(rowXml: string, changes: Map<string, SpreadsheetCellValue>): string {
  const rowPrefix = rowXml.match(/^<([A-Za-z_][\w.-]*:)?row\b/i)?.[1] || "";
  const cellPattern = /<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="([A-Z]+\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>)/gi;
  const existing = new Map<string, string>();
  let match: RegExpExecArray | null;
  while ((match = cellPattern.exec(rowXml)) != null) existing.set(match[1].toUpperCase(), match[0]);

  const replacements = new Map<string, string>();
  for (const [reference, value] of changes) {
    replacements.set(reference, replacementCellXml(reference, value, existing.get(reference), rowPrefix));
  }
  let patched = rowXml.replace(cellPattern, (cellXml, reference: string) => replacements.get(reference.toUpperCase()) || cellXml);
  const newCells = [...replacements.entries()]
    .filter(([reference]) => !existing.has(reference))
    .sort((left, right) => cellColumn(left[0]) - cellColumn(right[0]));
  if (newCells.length === 0) return patched;

  const closingMatch = patched.match(/<\/(?:[A-Za-z_][\w.-]*:)?row>\s*$/i);
  if (!closingMatch || closingMatch.index == null) return patched;
  const closingIndex = closingMatch.index;
  const cells = [...existing.entries()]
    .map(([reference, xml]) => [reference, replacements.get(reference) || xml] as const)
    .concat(newCells)
    .sort((left, right) => cellColumn(left[0]) - cellColumn(right[0]))
    .map((entry) => entry[1])
    .join("");
  const withoutCells = patched.slice(0, closingIndex).replace(cellPattern, "");
  return `${withoutCells}${cells}${closingMatch[0]}`;
}

function patchSheetXml(xml: string, changes: Map<string, SpreadsheetCellValue>): string {
  if (changes.size === 0) return xml;
  const changesByRow = new Map<number, Map<string, SpreadsheetCellValue>>();
  for (const [reference, value] of changes) {
    const row = Number(reference.match(/\d+$/)?.[0] || 0);
    if (!changesByRow.has(row)) changesByRow.set(row, new Map());
    changesByRow.get(row)!.set(reference, value);
  }

  const sheetDataMatch = xml.match(/<(?:[A-Za-z_][\w.-]*:)?sheetData\b[^>]*>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?sheetData>/i);
  if (!sheetDataMatch || sheetDataMatch.index == null) {
    throw new SpreadsheetPreservationError("The worksheet has no editable sheetData section.");
  }
  const sheetData = sheetDataMatch[0];
  const sheetPrefix = sheetData.match(/^<([A-Za-z_][\w.-]*:)?sheetData\b/i)?.[1] || "";
  const foundRows = new Set<number>();
  let patchedSheetData = sheetData.replace(/<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*\br="(\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?row>)/gi, (rowXml, rowText: string) => {
    const row = Number(rowText);
    const rowChanges = changesByRow.get(row);
    if (!rowChanges) return rowXml;
    foundRows.add(row);
    if (/\/>\s*$/.test(rowXml)) {
      const open = rowXml.replace(/\/>\s*$/, ">");
      const prefix = rowXml.match(/^<([A-Za-z_][\w.-]*:)?row\b/i)?.[1] || sheetPrefix;
      return patchRowXml(`${open}</${prefix}row>`, rowChanges);
    }
    return patchRowXml(rowXml, rowChanges);
  });

  const missingRows = [...changesByRow.entries()]
    .filter(([row]) => !foundRows.has(row))
    .sort((left, right) => left[0] - right[0])
    .map(([row, rowChanges]) => {
      const cells = [...rowChanges.entries()]
        .sort((left, right) => cellColumn(left[0]) - cellColumn(right[0]))
        .map(([reference, value]) => replacementCellXml(reference, value, undefined, sheetPrefix))
        .join("");
      return `<${sheetPrefix}row r="${row}">${cells}</${sheetPrefix}row>`;
    })
    .join("");
  if (missingRows) {
    patchedSheetData = patchedSheetData.replace(
      /<\/(?:[A-Za-z_][\w.-]*:)?sheetData>/i,
      `${missingRows}</${sheetPrefix}sheetData>`,
    );
  }
  return xml.slice(0, sheetDataMatch.index) + patchedSheetData + xml.slice(sheetDataMatch.index + sheetData.length);
}

function forceWorkbookRecalculation(xml: string): string {
  const workbookPrefix = xml.match(/<([A-Za-z_][\w.-]*:)?workbook\b/i)?.[1] || "";
  const calcPr = xml.match(/<(?:[A-Za-z_][\w.-]*:)?calcPr\b[^>]*\/?\s*>/i)?.[0];
  if (!calcPr) {
    return xml.replace(
      /<\/(?:[A-Za-z_][\w.-]*:)?workbook>/i,
      `<${workbookPrefix}calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></${workbookPrefix}workbook>`,
    );
  }
  let replacement = calcPr;
  const setAttribute = (name: string, value: string) => {
    const pattern = new RegExp(`\\s${name}="[^"]*"`, "i");
    if (pattern.test(replacement)) replacement = replacement.replace(pattern, ` ${name}="${value}"`);
    else replacement = replacement.replace(/\s*\/?\s*>$/, ` ${name}="${value}"/>`);
  };
  setAttribute("calcMode", "auto");
  setAttribute("fullCalcOnLoad", "1");
  setAttribute("forceFullCalc", "1");
  return xml.replace(calcPr, replacement);
}

export async function preserveSpreadsheetFile(
  original: ArrayBuffer,
  baselineSheets: SpreadsheetSheetSnapshot[],
  editedSheets: SpreadsheetSheetSnapshot[],
  filename: string,
): Promise<File> {
  if (baselineSheets.length !== editedSheets.length) {
    throw new SpreadsheetPreservationError("Adding or removing worksheets is not available in OOXML fidelity mode.");
  }
  const zip = await JSZip.loadAsync(original);
  const sheetParts = await workbookSheetParts(zip);
  let changedCellCount = 0;

  for (let sheetIndex = 0; sheetIndex < baselineSheets.length; sheetIndex += 1) {
    const baseline = baselineSheets[sheetIndex];
    const edited = editedSheets[sheetIndex];
    if (!edited || edited.name !== baseline.name) {
      throw new SpreadsheetPreservationError("Renaming or reordering worksheets is not available in OOXML fidelity mode.");
    }
    const baselineDimensions = sheetDimensions(baseline.data);
    const editedDimensions = sheetDimensions(edited.data);
    if (baselineDimensions.rows !== editedDimensions.rows || baselineDimensions.columns !== editedDimensions.columns) {
      throw new SpreadsheetPreservationError("Adding, deleting, or resizing rows and columns is not available in OOXML fidelity mode.");
    }

    const changes = new Map<string, SpreadsheetCellValue>();
    for (let row = 0; row < baselineDimensions.rows; row += 1) {
      for (let column = 0; column < baselineDimensions.columns; column += 1) {
        if (!sameCellValue(baseline.data[row]?.[column], edited.data[row]?.[column])) {
          changes.set(cellReference(row, column), edited.data[row]?.[column] ?? "");
        }
      }
    }
    if (changes.size === 0) continue;
    const sheetPart = sheetParts.get(baseline.name);
    const sheetFile = sheetPart ? zip.file(sheetPart) : null;
    if (!sheetPart || !sheetFile) {
      throw new SpreadsheetPreservationError(`Worksheet “${baseline.name}” could not be resolved in the original file.`);
    }
    zip.file(sheetPart, patchSheetXml(await sheetFile.async("text"), changes));
    changedCellCount += changes.size;
  }

  if (changedCellCount === 0) {
    return new File([original], filename, {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
  }
  const workbookFile = zip.file("xl/workbook.xml");
  if (workbookFile) zip.file("xl/workbook.xml", forceWorkbookRecalculation(await workbookFile.async("text")));
  const output = await zip.generateAsync({ type: "arraybuffer", compression: "DEFLATE" });
  return new File([output], filename, {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}
