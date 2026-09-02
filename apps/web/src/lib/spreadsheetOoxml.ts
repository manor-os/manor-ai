import JSZip from "jszip";
import type { CSSProperties } from "react";
import { officeCompatibleFontFamily } from "./officeFonts";
import {
  createSpreadsheetFormulaEvaluationState,
  evaluateSpreadsheetFormulaValue,
  type SpreadsheetFormulaContext,
  type SpreadsheetFormulaValue,
} from "./spreadsheetFormula";

interface SpreadsheetFormulaErrorValue {
  type: "error";
  value: "#N/A";
}

type SpreadsheetFormulaCacheValue = SpreadsheetFormulaValue | SpreadsheetFormulaErrorValue;

const UNCALCULATED_FORMULA_VALUE: SpreadsheetFormulaErrorValue = { type: "error", value: "#N/A" };
const INVALID_WORKSHEET_NAME_CHARACTER = /[\\/\[\]:*?]/;
const INVALID_XML_CONTROL_CHARACTER = /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/;
const SPREADSHEET_CHART_MAX_POINTS = 10_000;

export function isValidSpreadsheetWorksheetName(name: string): boolean {
  return Boolean(
    name.trim()
    && name.length <= 31
    && !INVALID_WORKSHEET_NAME_CHARACTER.test(name)
    && !INVALID_XML_CONTROL_CHARACTER.test(name)
    && !name.startsWith("'")
    && !name.endsWith("'")
    && name.toLocaleLowerCase() !== "history"
  );
}

export type SpreadsheetCellValue = string | number | boolean | null;

export interface SpreadsheetCellStyle {
  bold?: boolean;
  italic?: boolean;
  fontSize?: number;
  fontFamily?: string;
  color?: string;
  fill?: string;
  align?: "left" | "center" | "right";
  verticalAlign?: "top" | "middle" | "bottom";
  underline?: boolean;
  strike?: boolean;
  wrapText?: boolean;
  borderTop?: string;
  borderBottom?: string;
  borderLeft?: string;
  borderRight?: string;
}

export interface SpreadsheetRange {
  s: { r: number; c: number };
  e: { r: number; c: number };
}

export interface SpreadsheetChartSeries {
  name: string;
  categories: Array<string | number>;
  values: number[];
  color?: string;
  pointColors?: string[];
  showMarkers?: boolean;
  plotType?: "column" | "bar" | "line" | "area" | "pie" | "doughnut" | "scatter" | "stock" | "volume";
}

export interface SpreadsheetChartModel {
  id: string;
  type: "column" | "bar" | "line" | "area" | "pie" | "doughnut" | "scatter" | "combo_column_line" | "stock_hlc" | "stock_ohlc" | "stock_vhlc" | "stock_vohlc";
  grouping?: "clustered" | "standard" | "stacked" | "percentStacked";
  scatterStyle?: "marker" | "line" | "lineMarker" | "smooth" | "smoothMarker";
  title: string;
  series: SpreadsheetChartSeries[];
  anchor?: SpreadsheetRange;
}

export interface SpreadsheetImageModel {
  id: string;
  src: string;
  name: string;
  altText: string;
  anchor: { r: number; c: number };
  end?: { r: number; c: number };
  offsetX: number;
  offsetY: number;
  endOffsetX?: number;
  endOffsetY?: number;
  width?: number;
  height?: number;
}

export interface SpreadsheetEditorChart {
  id: string;
  type: "bar" | "line" | "pie";
  title: string;
  labelColumn: number;
  valueColumn: number;
  startRow: number;
  endRow: number;
}

export interface SpreadsheetSheetModel {
  name: string;
  sourceName?: string;
  data: SpreadsheetCellValue[][];
  displayData: string[][];
  numberFormats: Record<string, string>;
  styles: Record<string, SpreadsheetCellStyle>;
  columnWidths: number[];
  rowHeights: number[];
  merges: SpreadsheetRange[];
  charts: SpreadsheetChartModel[];
  images: SpreadsheetImageModel[];
  editorCharts?: SpreadsheetEditorChart[];
  structureOperations?: SpreadsheetStructureOperation[];
  hidden: boolean;
  showGridlines?: boolean;
}

export interface SpreadsheetSheetSnapshot {
  name: string;
  sourceName?: string;
  data: SpreadsheetCellValue[][];
  styles?: Record<string, SpreadsheetCellStyle>;
  columnWidths?: number[];
  rowHeights?: number[];
  merges?: SpreadsheetRange[];
  editorCharts?: SpreadsheetEditorChart[];
  structureOperations?: SpreadsheetStructureOperation[];
  hidden?: boolean;
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

export function nextSpreadsheetSheetName(existingNames: string[]): string {
  const usedNames = new Set(existingNames.map((name) => name.toLocaleLowerCase()));
  for (let index = 1; ; index += 1) {
    const candidate = `Sheet${index}`;
    if (!usedNames.has(candidate.toLocaleLowerCase())) return candidate;
  }
}

export function spreadsheetActiveSheetIndex(
  workbook: any,
  sheets: SpreadsheetSheetModel[],
  excludedNames: string[] = [],
): number {
  const excluded = new Set(excludedNames.map((name) => name.toLocaleLowerCase()));
  const selectable = (index: number) => Boolean(
    sheets[index]
    && !sheets[index].hidden
    && !excluded.has(sheets[index].name.toLocaleLowerCase()),
  );
  const activeTab = Number(workbook?.Workbook?.WBView?.[0]?.activeTab);
  if (Number.isInteger(activeTab) && activeTab >= 0 && activeTab < sheets.length && selectable(activeTab)) {
    return activeTab;
  }
  const firstSelectable = sheets.findIndex((_sheet, index) => selectable(index));
  return Math.max(0, firstSelectable);
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
    const numberFormats: Record<string, string> = {};
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
        if (typeof cell.z === "string") numberFormats[styleKey(row, column)] = cell.z;
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
      sourceName: name,
      data,
      displayData,
      numberFormats,
      styles,
      columnWidths,
      rowHeights,
      merges,
      charts: [],
      images: [],
      hidden: Number(workbookSheets[sheetIndex]?.Hidden || 0) !== 0,
    };
  });
}

/** Native style tables supplement SheetJS CE's fill-only cell.s projection. */
export async function spreadsheetSheetsFromFile(XLSX: any, workbook: any, source: ArrayBuffer): Promise<SpreadsheetSheetModel[]> {
  const sheets = spreadsheetSheetsFromWorkbook(XLSX, workbook);
  const bytes = new Uint8Array(source);
  if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) return sheets;
  const [zip, charts, images] = await Promise.all([
    JSZip.loadAsync(source),
    spreadsheetChartsFromFile(source, XLSX, workbook),
    spreadsheetImagesFromFile(source),
  ]);
  const parts = await workbookSheetParts(zip);
  const stylesXml = await zip.file("xl/styles.xml")?.async("text") || "";
  const fonts = spreadsheetXmlItems(stylesXml, "fonts", "font");
  const fills = spreadsheetXmlItems(stylesXml, "fills", "fill");
  const borders = spreadsheetXmlItems(stylesXml, "borders", "border");
  const attr = (xml: string, name: string) => xml.match(new RegExp(`\\b${name}="([^"]*)"`, "i"))?.[1];
  const child = (xml: string, name: string) => xml.match(new RegExp(`<(?:[A-Za-z_][\\w.-]*:)?${name}\\b[^>]*?(?:\\/>|>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?${name}>)`, "i"))?.[0] || "";
  const flag = (xml: string, name: string) => {
    const tag = child(xml, name);
    return Boolean(tag) && !["0", "false", "off", "none"].includes(attr(tag, "val") || "1");
  };
  const resolvedColor = (color: string) => {
    const direct = attr(color, "rgb");
    const theme = attr(color, "theme");
    const resolved = theme != null ? workbook.Themes?.themeElements?.clrScheme?.[Number(theme)]?.rgb : undefined;
    const value = direct && /^[a-f\d]{6,8}$/i.test(direct)
      ? direct.slice(-6)
      : typeof resolved === "string" && /^[a-f\d]{6}$/i.test(resolved) ? resolved : undefined;
    if (!value) return undefined;
    const tint = Number(attr(color, "tint"));
    if (!Number.isFinite(tint) || tint === 0) return `#${value}`;
    const channels = value.match(/../g)!.map((channel) => {
      const current = Number.parseInt(channel, 16);
      return Math.max(0, Math.min(255, Math.round(
        tint < 0 ? current * (1 + tint) : current + (255 - current) * tint,
      )));
    });
    return `#${channels.map((channel) => channel.toString(16).padStart(2, "0")).join("").toUpperCase()}`;
  };
  const rgb = (xml: string) => resolvedColor(child(xml, "color"));
  const nativeStyles = spreadsheetXmlItems(stylesXml, "cellXfs", "xf").map((xf): SpreadsheetCellStyle => {
    const font = fonts[Number(attr(xf, "fontId") || 0)] || "";
    const fill = fills[Number(attr(xf, "fillId") || 0)] || "";
    const patternFill = child(fill, "patternFill");
    const fillColor = attr(patternFill, "patternType") === "solid"
      ? resolvedColor(child(patternFill, "fgColor"))
      : undefined;
    const alignment = child(xf, "alignment");
    const horizontal = attr(alignment, "horizontal");
    const vertical = attr(alignment, "vertical");
    const size = Number(attr(child(font, "sz"), "val"));
    const name = attr(child(font, "name"), "val");
    const border = borders[Number(attr(xf, "borderId") || 0)] || "";
    const visual: SpreadsheetCellStyle = {
      bold: flag(font, "b"), italic: flag(font, "i"), underline: flag(font, "u"), strike: flag(font, "strike"),
      ...(Number.isFinite(size) && size > 0 ? { fontSize: size } : {}),
      ...(name ? { fontFamily: decodeXml(name) } : {}),
      ...(rgb(font) ? { color: rgb(font) } : {}),
      ...(fillColor ? { fill: fillColor } : {}),
      ...(horizontal === "left" || horizontal === "center" || horizontal === "right" ? { align: horizontal } : {}),
      ...(vertical === "center" ? { verticalAlign: "middle" } : vertical === "top" || vertical === "bottom" ? { verticalAlign: vertical } : {}),
      ...(attr(alignment, "wrapText") != null ? { wrapText: ["1", "true"].includes(attr(alignment, "wrapText")!) } : {}),
    };
    for (const edge of ["Top", "Bottom", "Left", "Right"] as const) {
      const side = child(border, edge.toLowerCase());
      const style = attr(side, "style");
      if (!style) continue;
      const width = style === "double" || style === "thick" ? 3 : style.startsWith("medium") ? 2 : 1;
      const line = style === "double" ? "double" : /dash/i.test(style) ? "dashed" : style === "dotted" || style === "hair" ? "dotted" : "solid";
      visual[`border${edge}`] = `${width}px ${line} ${rgb(side) || "#000000"}`;
    }
    return visual;
  });
  for (const sheet of sheets) {
    sheet.charts = charts.get(sheet.name) || [];
    sheet.images = images.get(sheet.name) || [];
    const part = parts.get(sheet.name);
    const xml = part ? await zip.file(part)?.async("text") || "" : "";
    const view = child(xml, "sheetView");
    sheet.showGridlines = !["0", "false"].includes(attr(view, "showGridLines") || "1");
    for (const row of xml.match(/<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*>/gi) || []) {
      const index = Number(attr(row, "r")) - 1;
      const points = Number(attr(row, "ht"));
      if (index >= 0 && index < sheet.rowHeights.length && Number.isFinite(points) && points > 0) sheet.rowHeights[index] = points * 96 / 72;
    }
    for (const cell of xml.match(/<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*>/gi) || []) {
      const reference = attr(cell, "r");
      if (!reference || !/^[A-Z]+\d+$/i.test(reference)) continue;
      const { r, c } = XLSX.utils.decode_cell(reference);
      if (r >= sheet.data.length || c >= sheet.columnWidths.length) continue;
      const visual = nativeStyles[Number(attr(cell, "s") || 0)];
      if (visual) sheet.styles[styleKey(r, c)] = { ...sheet.styles[styleKey(r, c)], ...visual };
    }
  }
  return sheets;
}

export function spreadsheetCellVisualStyle(style: SpreadsheetCellStyle): CSSProperties {
  return {
    color: style.color, fontFamily: style.fontFamily ? `"${officeCompatibleFontFamily(style.fontFamily)}", sans-serif` : undefined,
    fontSize: style.fontSize != null ? `${style.fontSize}pt` : undefined,
    fontWeight: style.bold ? 700 : 400, fontStyle: style.italic ? "italic" : "normal",
    textDecoration: [style.underline ? "underline" : "", style.strike ? "line-through" : ""].filter(Boolean).join(" ") || undefined,
    textAlign: style.align, verticalAlign: style.verticalAlign,
    ...(style.borderTop ? { borderTop: style.borderTop } : {}),
    ...(style.borderBottom ? { borderBottom: style.borderBottom } : {}),
    ...(style.borderLeft ? { borderLeft: style.borderLeft } : {}),
    ...(style.borderRight ? { borderRight: style.borderRight } : {}),
    whiteSpace: style.wrapText === false ? "pre" : "pre-wrap",
  };
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

function drawingAnchorBlocks(xml: string): string[] {
  return [...xml.matchAll(new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?(twoCellAnchor|oneCellAnchor|absoluteAnchor)\\b[^>]*>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?\\1>`,
    "gi",
  ))].map((match) => match[0]);
}

function elementText(xml: string | null, localName: string): string {
  if (!xml) return "";
  const pattern = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${localName}\\b[^>]*>([\\s\\S]*?)<\\/(?:[A-Za-z_][\\w.-]*:)?${localName}>`,
    "i",
  );
  return decodeXml(xml.match(pattern)?.[1] || "");
}

function elementAttribute(xml: string | null, localName: string, attribute: string): string {
  if (!xml) return "";
  const opening = xml.match(new RegExp(`<(?:[A-Za-z_][\\w.-]*:)?${localName}\\b[^>]*>`, "i"))?.[0] || "";
  return decodeXml(opening.match(new RegExp(`\\b${attribute}="([^"]*)"`, "i"))?.[1] || "");
}

function spreadsheetThemeColors(xml: string): Map<string, string> {
  const colors = new Map<string, string>();
  const scheme = elementBlock(xml, "clrScheme");
  if (!scheme) return colors;
  for (const name of ["dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]) {
    const entry = elementBlock(scheme, name);
    const value = elementAttribute(entry, "srgbClr", "val") || elementAttribute(entry, "sysClr", "lastClr");
    if (/^[0-9a-f]{6}$/i.test(value)) colors.set(name, `#${value.toUpperCase()}`);
  }
  return colors;
}

function chartRgbColor(xml: string | null, themeColors: Map<string, string>): string | undefined {
  if (!xml) return undefined;
  const direct = elementAttribute(xml, "srgbClr", "val");
  const scheme = elementAttribute(xml, "schemeClr", "val");
  const value = /^[0-9a-f]{6}$/i.test(direct) ? direct : themeColors.get(scheme)?.slice(1);
  if (!value || !/^[0-9a-f]{6}$/i.test(value)) return undefined;
  const lumModAttribute = elementAttribute(xml, "lumMod", "val");
  const lumOffAttribute = elementAttribute(xml, "lumOff", "val");
  const lumModValue = Number(lumModAttribute);
  const lumOffValue = Number(lumOffAttribute);
  const lumMod = lumModAttribute && Number.isFinite(lumModValue) ? lumModValue / 100_000 : 1;
  const lumOff = lumOffAttribute && Number.isFinite(lumOffValue) ? lumOffValue / 100_000 : 0;
  const channels = value.match(/../g)!.map((channel) => (
    Math.max(0, Math.min(255, Math.round(Number.parseInt(channel, 16) * lumMod + 255 * lumOff)))
  ));
  return `#${channels.map((channel) => channel.toString(16).padStart(2, "0")).join("").toUpperCase()}`;
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
  if (!/^[A-Z]+\d+(?::[A-Z]+\d+)?$/i.test(rangeText)) return [];
  const worksheet = workbook?.Sheets?.[sheetName];
  if (!worksheet) return [];
  let range: { s: { r: number; c: number }; e: { r: number; c: number } };
  try {
    range = XLSX.utils.decode_range(rangeText);
    const usedRange = typeof worksheet["!ref"] === "string"
      ? XLSX.utils.decode_range(worksheet["!ref"])
      : range;
    range = {
      s: { r: Math.max(range.s.r, usedRange.s.r), c: Math.max(range.s.c, usedRange.s.c) },
      e: { r: Math.min(range.e.r, usedRange.e.r), c: Math.min(range.e.c, usedRange.e.c) },
    };
  } catch {
    return [];
  }
  if (range.s.r > range.e.r || range.s.c > range.e.c) return [];
  const pointCount = (range.e.r - range.s.r + 1) * (range.e.c - range.s.c + 1);
  if (pointCount > SPREADSHEET_CHART_MAX_POINTS) return [];
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

function chartCachedValues(source: string | null, display: boolean): Array<string | number> {
  if (!source) return [];
  const cache = ["strCache", "numCache", "strLit", "numLit"]
    .map((localName) => elementBlock(source, localName))
    .find(Boolean);
  if (!cache) return [];
  const points = new Map<number, string | number>();
  let lastPointIndex = -1;
  for (const point of elementBlocks(cache, "pt")) {
    const index = Number(elementAttribute(point, "pt", "idx"));
    const rawValue = elementText(point, "v");
    const value = display ? rawValue : Number(rawValue);
    if (
      Number.isInteger(index) && index >= 0 && index < SPREADSHEET_CHART_MAX_POINTS
      && (display || Number.isFinite(value))
    ) {
      points.set(index, value);
      lastPointIndex = Math.max(lastPointIndex, index);
    }
  }
  const declaredCount = Number(elementAttribute(cache, "ptCount", "val"));
  const lastIndex = Math.min(SPREADSHEET_CHART_MAX_POINTS - 1, Math.max(
    Number.isInteger(declaredCount) && declaredCount > 0 ? declaredCount - 1 : -1,
    lastPointIndex,
  ));
  if (lastIndex < 0) return [];
  return Array.from(
    { length: lastIndex + 1 },
    (_, index) => points.get(index) ?? (display ? "" : 0),
  );
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
  themeColors: Map<string, string>,
  anchor?: SpreadsheetRange,
): SpreadsheetChartModel | null {
  const chartTypes = ["barChart", "lineChart", "areaChart", "pieChart", "doughnutChart", "scatterChart", "stockChart"] as const;
  const chartBlocks = chartTypes.flatMap((name) => (
    elementBlocks(chartXml, name).map((block) => ({ name, block }))
  ));
  if (chartBlocks.length === 0) return null;
  const primary = chartBlocks[0];
  type NativePlotType = Exclude<NonNullable<SpreadsheetChartSeries["plotType"]>, "volume">;
  const nativePlotType = (
    name: typeof chartTypes[number], block: string,
  ): NativePlotType => name === "barChart"
    ? elementAttribute(block, "barDir", "val") === "bar" ? "bar" : "column"
    : name === "lineChart" ? "line"
      : name === "areaChart" ? "area"
        : name === "doughnutChart" ? "doughnut"
          : name === "scatterChart" ? "scatter"
            : name === "stockChart" ? "stock" : "pie";
  const plotTypes = chartBlocks.map(({ name, block }) => nativePlotType(name, block)!);
  const isColumnLineCombo = plotTypes.length === 2
    && plotTypes.includes("column")
    && plotTypes.includes("line")
    && plotTypes.every((plotType) => plotType === "column" || plotType === "line");
  const isVolumeStockCandidate = plotTypes.length === 2
    && plotTypes.includes("column")
    && plotTypes.includes("stock")
    && plotTypes.every((plotType) => plotType === "column" || plotType === "stock");
  if (plotTypes.length !== 1 && !isColumnLineCombo && !isVolumeStockCandidate) return null;
  let type: SpreadsheetChartModel["type"] = isColumnLineCombo
    ? "combo_column_line"
    : plotTypes[0] === "stock" ? "stock_hlc" : plotTypes[0];
  const chartBlock = chartBlocks.find(({ name, block }) => nativePlotType(name, block) === "column")?.block
    || primary.block;
  const scatterStyleValue = elementAttribute(chartBlock, "scatterStyle", "val");
  const scatterStyle = ["marker", "line", "lineMarker", "smooth", "smoothMarker"].includes(scatterStyleValue)
    ? scatterStyleValue as SpreadsheetChartModel["scatterStyle"]
    : undefined;
  const groupingValue = elementAttribute(chartBlock, "grouping", "val");
  const grouping = ["clustered", "standard", "stacked", "percentStacked"].includes(groupingValue)
    ? groupingValue as SpreadsheetChartModel["grouping"]
    : undefined;
  const plotAreaStart = chartXml.search(/<(?:[A-Za-z_][\w.-]*:)?plotArea\b/i);
  const chartHeader = plotAreaStart >= 0 ? chartXml.slice(0, plotAreaStart) : chartXml;
  const titleContainer = elementBlock(chartHeader, "title");
  const titleText = elementBlock(titleContainer || "", "tx");
  const titleBlock = elementBlock(titleText || "", "rich");
  const titleReference = elementBlock(titleText || "", "strRef");
  const titleFormula = elementText(titleReference, "f");
  const formulaTitle = titleFormula
    ? formulaRangeValues(XLSX, workbook, titleFormula, sheetName, true)[0]
    : undefined;
  const cachedTitle = chartCachedValues(titleReference, true)[0];
  const title = elementBlocks(titleBlock || "", "t").map((block) => elementText(block, "t")).join("")
    || String(formulaTitle ?? cachedTitle ?? (elementText(titleText, "v") || "Chart"));
  let seriesIndex = 0;
  let series: SpreadsheetChartSeries[] = chartBlocks.flatMap(({ name: plotName, block: plotBlock }) => {
    const plotType = nativePlotType(plotName, plotBlock)!;
    return elementBlocks(plotBlock, "ser").flatMap((seriesBlock) => {
      const currentSeriesIndex = seriesIndex;
      seriesIndex += 1;
      const tx = elementBlock(seriesBlock, "tx");
      const txReference = elementBlock(tx || "", "strRef");
      const txFormula = elementText(txReference, "f");
      const formulaName = txFormula
        ? formulaRangeValues(XLSX, workbook, txFormula, sheetName, true)[0]
        : undefined;
      const cachedName = chartCachedValues(txReference, true)[0];
      const name = String(formulaName ?? cachedName ?? (elementText(tx, "v") || `Series ${currentSeriesIndex + 1}`));
      const categorySource = elementBlock(seriesBlock, plotType === "scatter" ? "xVal" : "cat");
      const valueSource = elementBlock(seriesBlock, plotType === "scatter" ? "yVal" : "val");
      const categoryFormula = elementText(categorySource, "f");
      const valueFormula = elementText(valueSource, "f");
      const formulaCategories = formulaRangeValues(XLSX, workbook, categoryFormula, sheetName, plotType !== "scatter");
      const formulaValues = formulaRangeValues(XLSX, workbook, valueFormula, sheetName, false);
      const categories = (formulaCategories.length > 0
        ? formulaCategories
        : chartCachedValues(categorySource, plotType !== "scatter"))
        .map((value) => plotType === "scatter" ? Number(value) : String(value));
      const values = (formulaValues.length > 0 ? formulaValues : chartCachedValues(valueSource, false))
        .map((value) => Number.isFinite(Number(value)) ? Number(value) : 0);
      const seriesProperties = elementBlock(seriesBlock, "spPr");
      const points = elementBlocks(seriesBlock, "dPt");
      const pointColors: string[] = [];
      for (const point of points) {
        const index = Number(elementAttribute(point, "idx", "val"));
        const color = chartRgbColor(elementBlock(point, "spPr"), themeColors);
        if (Number.isInteger(index) && index >= 0 && color) pointColors[index] = color;
      }
      return values.length > 0 || categories.length > 0 ? [{
        name,
        categories,
        values,
        ...(chartRgbColor(seriesProperties, themeColors) ? { color: chartRgbColor(seriesProperties, themeColors) } : {}),
        ...(pointColors.some(Boolean) ? { pointColors } : {}),
        plotType,
        ...(plotType === "line" || plotType === "scatter" ? {
          showMarkers: !["", "none"].includes(elementAttribute(seriesBlock, "symbol", "val")),
        } : {}),
      }] : [];
    });
  });
  if (series.length === 0) return null;
  const columnSeriesCount = series.filter((item) => item.plotType === "column").length;
  const stockSeriesCount = series.filter((item) => item.plotType === "stock").length;
  if (isVolumeStockCandidate) {
    const columnPlotCount = plotTypes.filter((plotType) => plotType === "column").length;
    const stockPlotCount = plotTypes.filter((plotType) => plotType === "stock").length;
    if (
      columnPlotCount !== 1 || stockPlotCount !== 1
      || columnSeriesCount !== 1 || ![3, 4].includes(stockSeriesCount)
    ) return null;
    series = series.map((item) => item.plotType === "column" ? { ...item, plotType: "volume" } : item);
    type = stockSeriesCount === 4 ? "stock_vohlc" : "stock_vhlc";
  } else if (plotTypes[0] === "stock") {
    if (plotTypes.length !== 1 || ![3, 4].includes(stockSeriesCount)) return null;
    type = stockSeriesCount === 4 ? "stock_ohlc" : "stock_hlc";
  }
  return {
    id,
    type,
    ...(grouping ? { grouping } : {}),
    ...(scatterStyle ? { scatterStyle } : {}),
    title,
    series,
    anchor,
  };
}

/** Resolve and read native SpreadsheetML chart parts without converting them. */
export async function spreadsheetChartsFromFile(
  source: ArrayBuffer,
  XLSX: any,
  workbook: any,
): Promise<Map<string, SpreadsheetChartModel[]>> {
  const zip = await JSZip.loadAsync(source);
  const sheetParts = await workbookSheetParts(zip);
  const themeColors = spreadsheetThemeColors(await zip.file("xl/theme/theme1.xml")?.async("text") || "");
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
      const anchors = drawingAnchorBlocks(drawingXml);
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
          themeColors,
          chartAnchor(anchorBlock),
        );
        if (chart) sheetCharts.push(chart);
      }
    }
    chartsBySheet.set(sheetName, sheetCharts);
  }
  return chartsBySheet;
}

function spreadsheetImageMime(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase();
  return ({
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
    svg: "image/svg+xml", webp: "image/webp", bmp: "image/bmp", avif: "image/avif",
  } as Record<string, string>)[extension || ""] || "application/octet-stream";
}

function drawingMarker(block: string, name: "from" | "to"): { r: number; c: number; x: number; y: number } | undefined {
  const marker = elementBlock(block, name);
  if (!marker) return undefined;
  return {
    r: Number(elementText(marker, "row") || 0),
    c: Number(elementText(marker, "col") || 0),
    x: Number(elementText(marker, "colOff") || 0) / 9525,
    y: Number(elementText(marker, "rowOff") || 0) / 9525,
  };
}

/** Resolve native SpreadsheetML image relationships without rasterizing the worksheet. */
export async function spreadsheetImagesFromFile(source: ArrayBuffer): Promise<Map<string, SpreadsheetImageModel[]>> {
  const zip = await JSZip.loadAsync(source);
  const sheetParts = await workbookSheetParts(zip);
  const imagesBySheet = new Map<string, SpreadsheetImageModel[]>();
  for (const [sheetName, sheetPart] of sheetParts) {
    const sheetFile = zip.file(sheetPart);
    const sheetRelationshipsFile = zip.file(relationshipsPart(sheetPart));
    if (!sheetFile || !sheetRelationshipsFile) continue;
    const [sheetXml, sheetRelationshipsXml] = await Promise.all([
      sheetFile.async("text"), sheetRelationshipsFile.async("text"),
    ]);
    const sheetRelationships = relationshipMap(sheetRelationshipsXml);
    const drawingIds = [...sheetXml.matchAll(/<(?:[A-Za-z_][\w.-]*:)?drawing\b[^>]*\br:id="([^"]+)"[^>]*\/?\s*>/gi)]
      .map((match) => match[1]);
    const sheetImages: SpreadsheetImageModel[] = [];
    for (const drawingId of drawingIds) {
      const drawingTarget = sheetRelationships.get(drawingId);
      if (!drawingTarget) continue;
      const drawingPart = resolveSpreadsheetPartTarget(sheetPart, drawingTarget);
      const drawingFile = zip.file(drawingPart);
      const drawingRelationshipsFile = zip.file(relationshipsPart(drawingPart));
      if (!drawingFile || !drawingRelationshipsFile) continue;
      const [drawingXml, drawingRelationshipsXml] = await Promise.all([
        drawingFile.async("text"), drawingRelationshipsFile.async("text"),
      ]);
      const drawingRelationships = relationshipMap(drawingRelationshipsXml);
      const anchors = drawingAnchorBlocks(drawingXml);
      for (let anchorIndex = 0; anchorIndex < anchors.length; anchorIndex += 1) {
        const block = anchors[anchorIndex];
        const picture = elementBlock(block, "pic");
        const relationshipId = picture?.match(/<(?:[A-Za-z_][\w.-]*:)?blip\b[^>]*\br:embed="([^"]+)"/i)?.[1];
        const target = relationshipId ? drawingRelationships.get(relationshipId) : undefined;
        if (!picture || !target) continue;
        const mediaPart = resolveSpreadsheetPartTarget(drawingPart, target);
        const media = zip.file(mediaPart);
        if (!media) continue;
        const start = drawingMarker(block, "from");
        const end = drawingMarker(block, "to");
        const x = Number(elementAttribute(block, "pos", "x") || 0) / 9525;
        const y = Number(elementAttribute(block, "pos", "y") || 0) / 9525;
        const width = Number(elementAttribute(block, "ext", "cx") || 0) / 9525;
        const height = Number(elementAttribute(block, "ext", "cy") || 0) / 9525;
        const name = elementAttribute(picture, "cNvPr", "name") || `Picture ${sheetImages.length + 1}`;
        const altText = elementAttribute(picture, "cNvPr", "descr") || name;
        sheetImages.push({
          id: `${drawingPart}:${anchorIndex}`,
          src: `data:${spreadsheetImageMime(mediaPart)};base64,${await media.async("base64")}`,
          name,
          altText,
          anchor: start ? { r: start.r, c: start.c } : { r: 0, c: 0 },
          ...(end ? { end: { r: end.r, c: end.c } } : {}),
          offsetX: start?.x ?? x,
          offsetY: start?.y ?? y,
          ...(end ? { endOffsetX: end.x, endOffsetY: end.y } : {}),
          ...(width > 0 ? { width } : {}),
          ...(height > 0 ? { height } : {}),
        });
      }
    }
    imagesBySheet.set(sheetName, sheetImages);
  }
  return imagesBySheet;
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

interface SpreadsheetFormulaDependencyRange {
  sheetName: string;
  rowStart: number;
  rowEnd: number;
  columnStart: number;
  columnEnd: number;
}

interface SpreadsheetFormulaDependencies {
  ranges: SpreadsheetFormulaDependencyRange[];
  dynamic: boolean;
}

interface SpreadsheetCellPosition {
  sheetName: string;
  row: number;
  column: number;
}

function spreadsheetCellPosition(sheetName: string, reference: string): SpreadsheetCellPosition | null {
  const normalized = reference.replace(/\$/g, "").toUpperCase();
  const row = Number(normalized.match(/\d+$/)?.[0] || 0) - 1;
  const column = cellColumn(normalized);
  return row >= 0 && column >= 0
    ? { sheetName: sheetName.toLocaleLowerCase(), row, column }
    : null;
}

function spreadsheetFormulaDependencies(
  formula: string,
  currentSheetName: string,
): SpreadsheetFormulaDependencies {
  const ranges: SpreadsheetFormulaDependencyRange[] = [];
  const searchableFormula = formula.replace(/"(?:[^"]|"")*"/g, (value) => " ".repeat(value.length));
  const maskedFormula = searchableFormula.split("");
  const pattern = /(?:(?:'((?:[^']|'')+)'|([^'!+\-*/^(),:<>=\s]+))!)?(\$?[A-Z]+\$?\d+)(?:\s*:\s*(?:(?:'((?:[^']|'')+)'|([^'!+\-*/^(),:<>=\s]+))!)?(\$?[A-Z]+\$?\d+))?/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(searchableFormula)) != null) {
    const previous = searchableFormula[match.index - 1] || "";
    const next = searchableFormula[pattern.lastIndex] || "";
    if (/^[\p{L}\p{M}\p{N}_.]$/u.test(previous) || /^[\p{L}\p{M}\p{N}_]$/u.test(next)) continue;
    const startSheetName = (match[1]?.replace(/''/g, "'") || match[2] || currentSheetName).toLocaleLowerCase();
    const endSheetName = (match[4]?.replace(/''/g, "'") || match[5] || startSheetName).toLocaleLowerCase();
    if (startSheetName !== endSheetName) continue;
    const start = spreadsheetCellPosition(startSheetName, match[3]);
    const end = spreadsheetCellPosition(endSheetName, match[6] || match[3]);
    if (!start || !end) continue;
    ranges.push({
      sheetName: startSheetName,
      rowStart: Math.min(start.row, end.row),
      rowEnd: Math.max(start.row, end.row),
      columnStart: Math.min(start.column, end.column),
      columnEnd: Math.max(start.column, end.column),
    });
    for (let index = match.index; index < pattern.lastIndex; index += 1) maskedFormula[index] = " ";
  }
  const functionPattern = /[\p{L}_\\][\p{L}\p{M}\p{N}_.\\]*\s*(?=\()/gu;
  while ((match = functionPattern.exec(maskedFormula.join(""))) != null) {
    for (let index = match.index; index < functionPattern.lastIndex; index += 1) maskedFormula[index] = " ";
  }
  const unresolvedIdentifier = maskedFormula
    .join("")
    .match(/[\p{L}_\\][\p{L}\p{M}\p{N}_.\\]*/gu)
    ?.some((identifier) => !/^(?:TRUE|FALSE)$/i.test(identifier));
  return {
    ranges,
    dynamic: ranges.length === 0
      || Boolean(unresolvedIdentifier)
      || /\b(?:INDIRECT|OFFSET)\s*\(/i.test(formula),
  };
}

function spreadsheetDependencyContains(
  dependency: SpreadsheetFormulaDependencyRange,
  cell: SpreadsheetCellPosition,
): boolean {
  return dependency.sheetName === cell.sheetName
    && cell.row >= dependency.rowStart
    && cell.row <= dependency.rowEnd
    && cell.column >= dependency.columnStart
    && cell.column <= dependency.columnEnd;
}

function normalizeComparable(value: SpreadsheetCellValue | undefined): SpreadsheetCellValue {
  return value == null || value === "" ? "" : value;
}

function sameCellValue(left: SpreadsheetCellValue | undefined, right: SpreadsheetCellValue | undefined): boolean {
  return Object.is(normalizeComparable(left), normalizeComparable(right));
}

function sameSpreadsheetObject(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

function sheetDimensions(data: SpreadsheetCellValue[][]): { rows: number; columns: number } {
  return {
    rows: Math.max(1, data.length),
    columns: Math.max(1, ...data.map((row) => row.length)),
  };
}

export type SpreadsheetStructureOperation = {
  axis: "row" | "column";
  index: number;
  deleteCount: number;
  insertCount: number;
};

function transformSpreadsheetIndex(index: number, operation: SpreadsheetStructureOperation): number | null {
  if (index < operation.index) return index;
  if (index < operation.index + operation.deleteCount) return null;
  return index - operation.deleteCount + operation.insertCount;
}

function transformSpreadsheetPosition(
  row: number,
  column: number,
  operations: SpreadsheetStructureOperation[],
): { row: number; column: number } | null {
  let nextRow: number | null = row;
  let nextColumn: number | null = column;
  for (const operation of operations) {
    if (operation.axis === "row" && nextRow != null) nextRow = transformSpreadsheetIndex(nextRow, operation);
    if (operation.axis === "column" && nextColumn != null) nextColumn = transformSpreadsheetIndex(nextColumn, operation);
  }
  return nextRow == null || nextColumn == null ? null : { row: nextRow, column: nextColumn };
}

function applySpreadsheetStructureToData(
  data: SpreadsheetCellValue[][],
  operations: SpreadsheetStructureOperation[],
): SpreadsheetCellValue[][] {
  let output = data.map((row) => [...row]);
  for (const operation of operations) {
    if (operation.axis === "row") {
      output.splice(
        operation.index,
        operation.deleteCount,
        ...Array.from({ length: operation.insertCount }, () => Array(sheetDimensions(output).columns).fill("")),
      );
    } else {
      const width = sheetDimensions(output).columns;
      output = output.map((row) => {
        const next = Array.from({ length: width }, (_unused, index) => row[index] ?? "");
        next.splice(operation.index, operation.deleteCount, ...Array(operation.insertCount).fill(""));
        return next;
      });
    }
  }
  return output.length > 0 ? output : [[""]];
}

function transformSpreadsheetStyleMap(
  styles: Record<string, SpreadsheetCellStyle> | undefined,
  operations: SpreadsheetStructureOperation[],
): Record<string, SpreadsheetCellStyle> {
  const output: Record<string, SpreadsheetCellStyle> = {};
  for (const [key, style] of Object.entries(styles || {})) {
    const [rowText, columnText] = key.split(":");
    const position = transformSpreadsheetPosition(Number(rowText), Number(columnText), operations);
    if (position) output[styleKey(position.row, position.column)] = style;
  }
  return output;
}

function spreadsheetColumnName(column: number): string {
  let value = column + 1;
  let output = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    output = String.fromCharCode(65 + remainder) + output;
    value = Math.floor((value - 1) / 26);
  }
  return output || "A";
}

type SpreadsheetReferenceParts = {
  absoluteColumn: string;
  column: number;
  absoluteRow: string;
  row: number;
};

function spreadsheetReferenceParts(reference: string): SpreadsheetReferenceParts | null {
  const match = reference.match(/^(\$?)([A-Z]{1,3})(\$?)(\d+)$/i);
  if (!match) return null;
  return {
    absoluteColumn: match[1],
    column: cellColumn(match[2]),
    absoluteRow: match[3],
    row: Number(match[4]) - 1,
  };
}

function transformSpreadsheetInterval(
  start: number,
  end: number,
  operation: SpreadsheetStructureOperation,
): [number, number] | null {
  const low = Math.min(start, end);
  const high = Math.max(start, end);
  if (operation.deleteCount === 0) {
    const nextLow = transformSpreadsheetIndex(low, operation);
    const nextHigh = transformSpreadsheetIndex(high, operation);
    return nextLow == null || nextHigh == null ? null : [nextLow, nextHigh];
  }

  const deletedEnd = operation.index + operation.deleteCount;
  if (high < operation.index || low >= deletedEnd) {
    const nextLow = transformSpreadsheetIndex(low, operation);
    const nextHigh = transformSpreadsheetIndex(high, operation);
    return nextLow == null || nextHigh == null ? null : [nextLow, nextHigh];
  }
  const survivesBefore = low < operation.index;
  const survivesAfter = high >= deletedEnd;
  if (!survivesBefore && !survivesAfter) {
    return operation.insertCount > 0
      ? [operation.index, operation.index + operation.insertCount - 1]
      : null;
  }
  const firstSurvivor = survivesBefore ? low : deletedEnd;
  const lastSurvivor = survivesAfter ? high : operation.index - 1;
  const nextLow = transformSpreadsheetIndex(firstSurvivor, operation);
  const nextHigh = transformSpreadsheetIndex(lastSurvivor, operation);
  return nextLow == null || nextHigh == null ? null : [nextLow, nextHigh];
}

export function transformSpreadsheetRange(
  reference: string,
  operations: SpreadsheetStructureOperation[],
): string | null {
  const [start, end] = reference.split(":");
  const startParts = spreadsheetReferenceParts(start);
  const endParts = spreadsheetReferenceParts(end || start);
  if (!startParts || !endParts) return reference;
  let rowRange: [number, number] = [startParts.row, endParts.row];
  let columnRange: [number, number] = [startParts.column, endParts.column];
  for (const operation of operations) {
    const nextRange = transformSpreadsheetInterval(
      ...(operation.axis === "row" ? rowRange : columnRange),
      operation,
    );
    if (!nextRange) return null;
    if (operation.axis === "row") rowRange = nextRange;
    else columnRange = nextRange;
  }
  const nextStart = `${startParts.absoluteColumn}${spreadsheetColumnName(columnRange[0])}${startParts.absoluteRow}${rowRange[0] + 1}`;
  const nextEnd = `${endParts.absoluteColumn}${spreadsheetColumnName(columnRange[1])}${endParts.absoluteRow}${rowRange[1] + 1}`;
  return end ? `${nextStart}:${nextEnd}` : nextStart;
}

function transformSpreadsheetFormula(
  formula: string,
  operations: SpreadsheetStructureOperation[],
): string {
  return formula.split(/("(?:[^"]|"")*")/g).map((segment, index) => {
    if (index % 2 === 1) return segment;
    return segment.replace(/(^|[^A-Za-z0-9_.!])((?:\$?[A-Z]{1,3}\$?\d+)(?::\$?[A-Z]{1,3}\$?\d+)?)/g, (full, prefix, reference) => {
      const transformed = transformSpreadsheetRange(reference, operations);
      return `${prefix}${transformed || "#REF!"}`;
    });
  }).join("");
}

function spreadsheetOutputName(filename: string): string {
  return filename.toLowerCase().endsWith(".xlsx")
    ? filename
    : `${filename.replace(/\.(?:xls|et)$/i, "")}.xlsx`;
}

function sheetJsCellStyle(style: SpreadsheetCellStyle): Record<string, unknown> {
  return {
    ...(style.bold || style.italic || style.fontSize || style.fontFamily || style.color
      ? {
          font: {
            ...(style.bold ? { bold: true } : {}),
            ...(style.italic ? { italic: true } : {}),
            ...(style.fontSize ? { sz: style.fontSize } : {}),
            ...(style.fontFamily ? { name: style.fontFamily } : {}),
            ...(style.color ? { color: { rgb: style.color.replace(/^#/, "") } } : {}),
          },
        }
      : {}),
    ...(style.fill
      ? { patternType: "solid", fgColor: { rgb: style.fill.replace(/^#/, "") } }
      : {}),
    ...(style.align ? { alignment: { horizontal: style.align } } : {}),
  };
}

/**
 * Rebuild a valid editable workbook when an edit changes workbook structure
 * beyond what the incremental OOXML patcher can represent. Normal cell edits
 * continue to use the original package-preserving path below.
 */
export async function buildSpreadsheetFile(
  sheets: SpreadsheetSheetSnapshot[],
  filename: string,
): Promise<File> {
  const XLSX = await import("xlsx");
  const workbook = XLSX.utils.book_new();
  const workbookSheets: Array<{ Hidden: 0 | 1 | 2 }> = [];
  const contentSheets = sheets.filter((sheet) => sheet.name !== "_manor_charts");

  for (const [sheetIndex, sheet] of contentSheets.entries()) {
    const values = sheet.data.map((row) => row.map((value) => (
      typeof value === "string" && value.trim().startsWith("=") ? null : value
    )));
    const worksheet = XLSX.utils.aoa_to_sheet(values);
    sheet.data.forEach((row, rowIndex) => row.forEach((value, columnIndex) => {
      const reference = XLSX.utils.encode_cell({ r: rowIndex, c: columnIndex });
      if (typeof value === "string" && value.trim().startsWith("=")) {
        worksheet[reference] = { t: "n", f: value.trim().slice(1) };
      }
      const style = sheet.styles?.[styleKey(rowIndex, columnIndex)];
      if (style && worksheet[reference]) worksheet[reference].s = sheetJsCellStyle(style);
    }));
    const dimensions = sheetDimensions(sheet.data);
    worksheet["!ref"] = XLSX.utils.encode_range({
      s: { r: 0, c: 0 },
      e: { r: dimensions.rows - 1, c: dimensions.columns - 1 },
    });
    if (sheet.columnWidths?.length) {
      worksheet["!cols"] = sheet.columnWidths.map((width) => ({ wpx: width }));
    }
    if (sheet.rowHeights?.length) {
      worksheet["!rows"] = sheet.rowHeights.map((height) => ({ hpx: height }));
    }
    if (sheet.merges?.length) worksheet["!merges"] = structuredClone(sheet.merges);
    XLSX.utils.book_append_sheet(workbook, worksheet, sheet.name || `Sheet${sheetIndex + 1}`);
    workbookSheets.push({ Hidden: sheet.hidden ? 1 : 0 });
  }
  const editorMetadataSheet = contentSheets.find((sheet) => sheet.editorCharts?.length);
  if (editorMetadataSheet) {
    const metadata = JSON.stringify({
      charts: editorMetadataSheet.editorCharts,
      styles: editorMetadataSheet.styles || {},
    });
    const metadataRows = Array.from(
      { length: Math.max(1, Math.ceil(metadata.length / 30_000)) },
      (_unused, index) => [metadata.slice(index * 30_000, (index + 1) * 30_000)],
    );
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(metadataRows), "_manor_charts");
    workbookSheets.push({ Hidden: 1 });
  }
  const workbookProperties = (workbook.Workbook || {}) as Record<string, unknown>;
  workbook.Workbook = {
    ...workbookProperties,
    Sheets: workbookSheets,
    CalcPr: {
      ...((workbookProperties.CalcPr as Record<string, unknown> | undefined) || {}),
      calcMode: "auto",
      fullCalcOnLoad: true,
      forceFullCalc: true,
    },
  } as typeof workbook.Workbook;
  const output = XLSX.write(workbook, {
    type: "array",
    bookType: "xlsx",
    cellStyles: true,
    compression: true,
  });
  return new File([output], spreadsheetOutputName(filename), {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
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
  cachedFormulaValue?: SpreadsheetFormulaCacheValue,
  preserveFormulaXml = false,
): string {
  const prefix = originalCellXml?.match(/^<([A-Za-z_][\w.-]*:)?c\b/i)?.[1] || fallbackPrefix;
  const attributes = openingCellAttributes(originalCellXml, reference);
  if (value == null || value === "") return `<${prefix}c${attributes}/>`;
  if (typeof value === "string" && value.startsWith("=")) {
    const cachedText = cachedFormulaValue == null
      ? undefined
      : typeof cachedFormulaValue === "object"
        ? cachedFormulaValue.value
        : typeof cachedFormulaValue === "boolean"
          ? cachedFormulaValue ? "1" : "0"
          : String(cachedFormulaValue);
    const cachedValue = cachedText == null
      ? ""
      : `<${prefix}v>${escapeXml(cachedText)}</${prefix}v>`;
    const cachedType = cachedFormulaValue == null
      ? undefined
      : typeof cachedFormulaValue === "object"
        ? "e"
        : typeof cachedFormulaValue === "string"
          ? "str"
          : typeof cachedFormulaValue === "boolean"
            ? "b"
            : "n";
    if (preserveFormulaXml && originalCellXml) {
      const valuePattern = new RegExp(
        `<(?:[A-Za-z_][\\w.-]*:)?v\\b[^>]*(?:\\/>|>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?v>)`,
        "i",
      );
      let preserved = valuePattern.test(originalCellXml)
        ? originalCellXml.replace(valuePattern, cachedValue)
        : originalCellXml.replace(
            new RegExp(`(<\\/(?:[A-Za-z_][\\w.-]*:)?f>)`, "i"),
            `$1${cachedValue}`,
          );
      preserved = preserved.replace(/^<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*>/i, (opening) => {
        const withoutType = opening.replace(/\s+t="[^"]*"/i, "");
        return cachedType
          ? withoutType.replace(/>$/, ` t="${cachedType}">`)
          : withoutType;
      });
      return preserved;
    }
    const typeAttribute = cachedType ? ` t="${cachedType}"` : "";
    return `<${prefix}c${attributes}${typeAttribute}><${prefix}f>${escapeXml(value.slice(1))}</${prefix}f>${cachedValue}</${prefix}c>`;
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

function patchRowXml(
  rowXml: string,
  changes: Map<string, SpreadsheetCellValue>,
  formulaCache: Map<string, SpreadsheetFormulaCacheValue>,
  directChanges: Set<string>,
): string {
  const rowPrefix = rowXml.match(/^<([A-Za-z_][\w.-]*:)?row\b/i)?.[1] || "";
  const cellPattern = /<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="([A-Z]+\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>)/gi;
  const existing = new Map<string, string>();
  let match: RegExpExecArray | null;
  while ((match = cellPattern.exec(rowXml)) != null) existing.set(match[1].toUpperCase(), match[0]);

  const replacements = new Map<string, string>();
  for (const [reference, value] of changes) {
    replacements.set(reference, replacementCellXml(
      reference,
      value,
      existing.get(reference),
      rowPrefix,
      formulaCache.get(reference),
      !directChanges.has(reference),
    ));
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
  // CT_Row allows an optional extension list only after all cell elements.
  // Keep that ordering when adding a previously missing cell; appending after
  // extLst produces a package that Excel/LibreOffice may refuse to open.
  const extensionIndex = withoutCells.search(/<(?:[A-Za-z_][\w.-]*:)?extLst\b/i);
  if (extensionIndex >= 0) {
    return `${withoutCells.slice(0, extensionIndex)}${cells}${withoutCells.slice(extensionIndex)}${closingMatch[0]}`;
  }
  return `${withoutCells}${cells}${closingMatch[0]}`;
}

function patchSheetXml(
  xml: string,
  changes: Map<string, SpreadsheetCellValue>,
  formulaCache: Map<string, SpreadsheetFormulaCacheValue>,
  directChanges: Set<string>,
): string {
  if (changes.size === 0) return xml;
  const changesByRow = new Map<number, Map<string, SpreadsheetCellValue>>();
  for (const [reference, value] of changes) {
    const row = Number(reference.match(/\d+$/)?.[0] || 0);
    if (!changesByRow.has(row)) changesByRow.set(row, new Map());
    changesByRow.get(row)!.set(reference, value);
  }

  const sheetDataMatch = xml.match(/<(?:[A-Za-z_][\w.-]*:)?sheetData\b[^>]*>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?sheetData>/i);
  if (!sheetDataMatch || sheetDataMatch.index == null) {
    const emptySheetData = xml.match(/<(?:[A-Za-z_][\w.-]*:)?sheetData\b[^>]*\/\s*>/i);
    if (emptySheetData?.index != null) {
      const prefix = emptySheetData[0].match(/^<([A-Za-z_][\w.-]*:)?sheetData\b/i)?.[1] || "";
      const expanded = emptySheetData[0].replace(/\/\s*>$/, `></${prefix}sheetData>`);
      const normalizedXml = xml.slice(0, emptySheetData.index)
        + expanded
        + xml.slice(emptySheetData.index + emptySheetData[0].length);
      return patchSheetXml(normalizedXml, changes, formulaCache, directChanges);
    }
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
      return patchRowXml(`${open}</${prefix}row>`, rowChanges, formulaCache, directChanges);
    }
    return patchRowXml(rowXml, rowChanges, formulaCache, directChanges);
  });

  const missingRows = [...changesByRow.entries()]
    .filter(([row]) => !foundRows.has(row))
    .sort((left, right) => left[0] - right[0])
    .map(([row, rowChanges]) => {
      const cells = [...rowChanges.entries()]
        .sort((left, right) => cellColumn(left[0]) - cellColumn(right[0]))
        .map(([reference, value]) => replacementCellXml(
          reference,
          value,
          undefined,
          sheetPrefix,
          formulaCache.get(reference),
          !directChanges.has(reference),
        ))
        .join("");
      return { row, xml: `<${sheetPrefix}row r="${row}">${cells}</${sheetPrefix}row>` };
    });
  for (const missingRow of missingRows) {
    let inserted = false;
    patchedSheetData = patchedSheetData.replace(
      /<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*\br="(\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?row>)/gi,
      (rowXml, rowText: string) => {
        if (!inserted && Number(rowText) > missingRow.row) {
          inserted = true;
          return `${missingRow.xml}${rowXml}`;
        }
        return rowXml;
      },
    );
    if (!inserted) {
      patchedSheetData = patchedSheetData.replace(
        /<\/(?:[A-Za-z_][\w.-]*:)?sheetData>/i,
        `${missingRow.xml}</${sheetPrefix}sheetData>`,
      );
    }
  }
  return xml.slice(0, sheetDataMatch.index) + patchedSheetData + xml.slice(sheetDataMatch.index + sheetData.length);
}

function materializeSharedSpreadsheetFormulas(
  xml: string,
  baselineData: SpreadsheetCellValue[][],
  directChanges: Set<string>,
  structureChanged: boolean,
): string {
  const cellPattern = /<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="([A-Z]+\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>)/gi;
  const groups = new Map<string, Set<string>>();
  for (const match of xml.matchAll(cellPattern)) {
    const formula = match[0].match(/<(?:[A-Za-z_][\w.-]*:)?f\b([^>]*\bt="shared"[^>]*)\/?\s*>(?:[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?f>)?/i);
    const sharedIndex = formula?.[1].match(/\bsi="([^"]+)"/i)?.[1];
    if (!sharedIndex) continue;
    const references = groups.get(sharedIndex) || new Set<string>();
    references.add(match[1].toUpperCase());
    groups.set(sharedIndex, references);
  }
  const materializedGroups = new Set(
    [...groups.entries()]
      .filter(([, references]) => structureChanged || [...references].some((reference) => directChanges.has(reference)))
      .map(([sharedIndex]) => sharedIndex),
  );
  if (materializedGroups.size === 0) return xml;

  return xml.replace(cellPattern, (cellXml, referenceText: string) => {
    const formula = cellXml.match(/<([A-Za-z_][\w.-]*:)?f\b([^>]*\bt="shared"[^>]*)\/?\s*>(?:[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?f>)?/i);
    const sharedIndex = formula?.[2].match(/\bsi="([^"]+)"/i)?.[1];
    if (!formula || !sharedIndex || !materializedGroups.has(sharedIndex)) return cellXml;
    const reference = referenceText.toUpperCase();
    const row = Number(reference.match(/\d+$/)?.[0] || 0) - 1;
    const column = cellColumn(reference);
    const value = baselineData[row]?.[column];
    if (typeof value !== "string" || !value.trim().startsWith("=")) {
      throw new SpreadsheetPreservationError(`Shared formula ${reference} is outside the editable worksheet range.`);
    }
    const prefix = formula[1] || "";
    return cellXml.replace(formula[0], `<${prefix}f>${escapeXml(value.trim().slice(1))}</${prefix}f>`);
  });
}

function transformWorksheetStructureXml(
  xml: string,
  operations: SpreadsheetStructureOperation[],
  dimensions: { rows: number; columns: number },
): string {
  if (operations.length === 0) return xml;
  const cellPattern = /<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="([A-Z]+\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>)/gi;
  let output = xml.replace(
    /<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*\br="(\d+)"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?row>)/gi,
    (rowXml, rowText: string) => {
      let transformedRow: number | null = Number(rowText) - 1;
      for (const operation of operations) {
        if (operation.axis === "row" && transformedRow != null) {
          transformedRow = transformSpreadsheetIndex(transformedRow, operation);
        }
      }
      if (transformedRow == null) return "";
      const nextRow = transformedRow + 1;
      let transformed = rowXml.replace(/\br="\d+"/i, `r="${nextRow}"`);
      transformed = transformed.replace(cellPattern, (cellXml, reference: string) => {
        const cellMatch = reference.match(/^([A-Z]+)(\d+)$/i);
        if (!cellMatch) return cellXml;
        const position = transformSpreadsheetPosition(Number(cellMatch[2]) - 1, cellColumn(cellMatch[1]), operations);
        if (!position) return "";
        const nextReference = `${spreadsheetColumnName(position.column)}${position.row + 1}`;
        let nextCell = cellXml.replace(/\br="[A-Z]+\d+"/i, `r="${nextReference}"`);
        nextCell = nextCell.replace(
          /(<(?:[A-Za-z_][\w.-]*:)?f\b[^>]*>)([\s\S]*?)(<\/(?:[A-Za-z_][\w.-]*:)?f>)/gi,
          (_formulaXml, opening, formula, closing) => {
            const transformedOpening = String(opening).replace(
              /\bref="([^"]+)"/i,
              (attribute, reference) => {
                const transformed = transformSpreadsheetRange(reference, operations);
                return transformed ? `ref="${transformed}"` : attribute;
              },
            );
            return `${transformedOpening}${transformSpreadsheetFormula(formula, operations)}${closing}`;
          },
        );
        return nextCell;
      });
      return transformed;
    },
  );

  const lastReference = `${spreadsheetColumnName(Math.max(0, dimensions.columns - 1))}${Math.max(1, dimensions.rows)}`;
  output = output.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?dimension\b[^>]*\bref=")([^"]+)(")/i,
    `$1A1:${lastReference}$3`,
  );
  output = output.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?(?:mergeCell|autoFilter|hyperlink)\b[^>]*\bref=")([^"]+)(")/gi,
    (full, opening, reference, closing) => {
      const transformed = transformSpreadsheetRange(reference, operations);
      return transformed ? `${opening}${transformed}${closing}` : "";
    },
  );
  output = output.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?(?:conditionalFormatting|dataValidation)\b[^>]*\bsqref=")([^"]+)(")/gi,
    (full, opening, references, closing) => {
      const transformed = String(references).split(/\s+/)
        .map((reference) => transformSpreadsheetRange(reference, operations))
        .filter(Boolean)
        .join(" ");
      return transformed ? `${opening}${transformed}${closing}` : full;
    },
  );
  output = output.replace(
    /<(?:[A-Za-z_][\w.-]*:)?col\b[^>]*\bmin="(\d+)"[^>]*\bmax="(\d+)"[^>]*\/>/gi,
    (columnXml, minText: string, maxText: string) => {
      let min: number | null = Number(minText) - 1;
      let max: number | null = Number(maxText) - 1;
      for (const operation of operations) {
        if (operation.axis !== "column") continue;
        const nextMin: number | null = min == null ? null : transformSpreadsheetIndex(min, operation);
        const nextMax: number | null = max == null ? null : transformSpreadsheetIndex(max, operation);
        if (nextMin == null && nextMax == null) return "";
        min = nextMin ?? nextMax;
        max = nextMax ?? nextMin;
        if (operation.insertCount > 0 && operation.index > (min ?? 0) && operation.index <= (max ?? 0)) {
          max = (max ?? 0) + operation.insertCount;
        }
      }
      if (min == null || max == null) return "";
      return columnXml
        .replace(/\bmin="\d+"/i, `min="${min + 1}"`)
        .replace(/\bmax="\d+"/i, `max="${max + 1}"`);
    },
  );
  return output;
}

interface SpreadsheetTableTransformResult {
  xml: string;
  headerChanges: Map<string, SpreadsheetCellValue>;
  columnTransforms: Map<string, string | null>;
  tableName: string;
  reference: string;
}

function spreadsheetStructuredCharacterEscaped(value: string, index: number): boolean {
  let apostrophes = 0;
  for (let cursor = index - 1; cursor >= 0 && value[cursor] === "'"; cursor -= 1) apostrophes += 1;
  return apostrophes % 2 === 1;
}

function spreadsheetStructuredReferenceEnd(formula: string, start: number): number | null {
  let depth = 0;
  for (let index = start; index < formula.length; index += 1) {
    const escaped = spreadsheetStructuredCharacterEscaped(formula, index);
    if (formula[index] === "[" && !escaped) depth += 1;
    else if (formula[index] === "]" && !escaped) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return null;
}

function spreadsheetFormulaStringEnd(formula: string, start: number): number | null {
  for (let index = start + 1; index < formula.length; index += 1) {
    if (formula[index] !== '"') continue;
    if (formula[index + 1] === '"') {
      index += 1;
      continue;
    }
    return index;
  }
  return null;
}

function escapeSpreadsheetStructuredColumnName(name: string): string {
  return name.replace(/([\[\]#'@])/g, "'$1");
}

function spreadsheetStructuredColumnNeedsOuterSpecifier(name: string): boolean {
  return /[\t\n\r,:."{}$^&*+=<>\/\\!()%?`;~_-]/.test(name);
}

function spreadsheetStructuredColumnSpecifier(name: string, nested: boolean): string {
  const specifier = `[${escapeSpreadsheetStructuredColumnName(name)}]`;
  return nested || !spreadsheetStructuredColumnNeedsOuterSpecifier(name) ? specifier : `[${specifier}]`;
}

function spreadsheetStructuredCurrentRowSpecifier(name: string): string {
  const escaped = escapeSpreadsheetStructuredColumnName(name);
  return /^[A-Za-z0-9_\u0080-\uFFFF]+$/.test(name) ? `[@${escaped}]` : `[@[${escaped}]]`;
}

function replaceSpreadsheetStructuredColumnTokens(
  reference: string,
  columnTransforms: Map<string, string | null>,
): string | null {
  const candidates = Array.from(columnTransforms, ([name, replacement]) => {
    const escaped = escapeSpreadsheetStructuredColumnName(name);
    return [
      { source: `[@[${escaped}]]`, replacement, currentRow: true, standalone: false },
      { source: `[@${escaped}]`, replacement, currentRow: true, standalone: false },
      { source: `[[${escaped}]]`, replacement, currentRow: false, standalone: true },
      { source: `[${escaped}]`, replacement, currentRow: false, standalone: false },
    ];
  }).flat().sort((left, right) => right.source.length - left.source.length);
  const normalizedReference = reference.toLowerCase();
  let output = "";
  let depth = 0;
  let index = 0;
  while (index < reference.length) {
    const candidate = candidates.find((item) => (
      normalizedReference.slice(index, index + item.source.length) === item.source.toLowerCase()
    ));
    if (candidate) {
      if (candidate.replacement == null) return null;
      output += candidate.currentRow
        ? spreadsheetStructuredCurrentRowSpecifier(candidate.replacement)
        : spreadsheetStructuredColumnSpecifier(candidate.replacement, !candidate.standalone && depth > 0);
      index += candidate.source.length;
      continue;
    }
    const escaped = spreadsheetStructuredCharacterEscaped(reference, index);
    if (reference[index] === "[" && !escaped) depth += 1;
    else if (reference[index] === "]" && !escaped) depth -= 1;
    output += reference[index];
    index += 1;
  }
  return output;
}

function transformSpreadsheetStructuredReferenceFormula(
  formula: string,
  tableName: string,
  columnTransforms: Map<string, string | null>,
  includeUnqualified: boolean,
  includeQualified = true,
): string {
  if (!tableName || columnTransforms.size === 0) return formula;
  const normalizedTableName = tableName.toLowerCase();
  const identifierCharacter = /[A-Za-z0-9_.\u0080-\uFFFF]/;
  let output = "";
  let index = 0;
  while (index < formula.length) {
    if (formula[index] === '"') {
      const stringEnd = spreadsheetFormulaStringEnd(formula, index);
      if (stringEnd == null) {
        output += formula.slice(index);
        break;
      }
      output += formula.slice(index, stringEnd + 1);
      index = stringEnd + 1;
      continue;
    }
    if (identifierCharacter.test(formula[index]) && (index === 0 || !identifierCharacter.test(formula[index - 1]))) {
      let identifierEnd = index + 1;
      while (identifierEnd < formula.length && identifierCharacter.test(formula[identifierEnd])) identifierEnd += 1;
      let referenceStart = identifierEnd;
      while (/\s/.test(formula[referenceStart] || "")) referenceStart += 1;
      if (formula[referenceStart] === "[") {
        const referenceEnd = spreadsheetStructuredReferenceEnd(formula, referenceStart);
        if (referenceEnd != null) {
          const reference = formula.slice(referenceStart, referenceEnd + 1);
          const currentTable = formula.slice(index, identifierEnd).toLowerCase() === normalizedTableName;
          if (currentTable && includeQualified) {
            const transformed = replaceSpreadsheetStructuredColumnTokens(reference, columnTransforms);
            output += transformed == null ? "#REF!" : `${formula.slice(index, referenceStart)}${transformed}`;
          } else {
            output += formula.slice(index, referenceEnd + 1);
          }
          index = referenceEnd + 1;
          continue;
        }
      }
      output += formula.slice(index, identifierEnd);
      index = identifierEnd;
      continue;
    }
    if (includeUnqualified && formula[index] === "[") {
      const referenceEnd = spreadsheetStructuredReferenceEnd(formula, index);
      if (referenceEnd != null) {
        const transformed = replaceSpreadsheetStructuredColumnTokens(
          formula.slice(index, referenceEnd + 1),
          columnTransforms,
        );
        output += transformed ?? "#REF!";
        index = referenceEnd + 1;
        continue;
      }
    }
    output += formula[index];
    index += 1;
  }
  return output;
}

function transformSpreadsheetStructuredReferenceXml(
  xml: string,
  tableName: string,
  columnTransforms: Map<string, string | null>,
  includeUnqualified: boolean,
  includeQualified = true,
): string {
  return xml.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula|formula1|formula2|formula|definedName|f)\b[^>]*>)([\s\S]*?)(<\/(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula|formula1|formula2|formula|definedName|f)>)/gi,
    (full, opening, formula, closing) => {
      const decodedFormula = decodeXml(formula);
      const transformedFormula = transformSpreadsheetStructuredReferenceFormula(
        decodedFormula,
        tableName,
        columnTransforms,
        includeUnqualified,
        includeQualified,
      );
      return transformedFormula === decodedFormula
        ? full
        : `${opening}${escapeXml(transformedFormula)}${closing}`;
    },
  );
}

function transformSpreadsheetWorksheetStructuredReferenceXml(
  xml: string,
  tableName: string,
  columnTransforms: Map<string, string | null>,
  tableReference: string,
): string {
  const [startText, endText] = tableReference.split(":");
  const start = spreadsheetReferenceParts(startText);
  const end = spreadsheetReferenceParts(endText || startText);
  if (!start || !end) return xml;
  return xml.replace(
    /<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\/>|<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>/gi,
    (cellXml) => {
      const reference = spreadsheetReferenceParts(cellXml.match(/\br="([A-Z]+\d+)"/i)?.[1] || "");
      if (
        !reference
        || reference.row < start.row
        || reference.row > end.row
        || reference.column < start.column
        || reference.column > end.column
      ) return cellXml;
      return transformSpreadsheetStructuredReferenceXml(
        cellXml,
        tableName,
        columnTransforms,
        true,
        false,
      );
    },
  );
}

function transformSpreadsheetTableXml(
  xml: string,
  operations: SpreadsheetStructureOperation[],
  editedData: SpreadsheetCellValue[][],
): SpreadsheetTableTransformResult {
  const headerChanges = new Map<string, SpreadsheetCellValue>();
  const columnTransforms = new Map<string, string | null>();
  const tableTag = xml.match(/<(?:[A-Za-z_][\w.-]*:)?table\b[^>]*>/i)?.[0] || "";
  const tableName = decodeXml(
    tableTag.match(/\bdisplayName="([^"]+)"/i)?.[1]
      || tableTag.match(/\bname="([^"]+)"/i)?.[1]
      || "",
  );
  const tableReference = xml.match(/<(?:[A-Za-z_][\w.-]*:)?table\b[^>]*\bref="([^"]+)"/i)?.[1];
  if (!tableReference) return { xml, headerChanges, columnTransforms, tableName, reference: "" };
  const transformedReference = operations.length > 0
    ? transformSpreadsheetRange(tableReference, operations)
    : tableReference;
  if (!transformedReference) {
    throw new SpreadsheetPreservationError("This structural edit removes an entire Excel table.");
  }
  let output = xml.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?(?:table|autoFilter)\b[^>]*\bref=")([^"]+)(")/gi,
    (full, opening, reference, closing) => {
      const transformed = transformSpreadsheetRange(reference, operations);
      return transformed ? `${opening}${transformed}${closing}` : full;
    },
  );

  const originalStart = spreadsheetReferenceParts(tableReference.split(":")[0]);
  const transformedStart = spreadsheetReferenceParts(transformedReference.split(":")[0]);
  const transformedEnd = spreadsheetReferenceParts(transformedReference.split(":")[1] || transformedReference);
  const hasHeaderRow = !/<(?:[A-Za-z_][\w.-]*:)?table\b[^>]*\bheaderRowCount="0"/i.test(xml);
  const tableColumns = output.match(/<((?:[A-Za-z_][\w.-]*:)?tableColumns)\b([^>]*)>([\s\S]*?)<\/\1>/i);
  if (originalStart && transformedStart && transformedEnd && tableColumns) {
    const columnXml = tableColumns[3].match(
      /<(?:[A-Za-z_][\w.-]*:)?tableColumn\b[^>]*\/>|<(?:[A-Za-z_][\w.-]*:)?tableColumn\b[^>]*>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?tableColumn>/gi,
    ) || [];
    const columnByPosition = new Map<number, string>();
    columnXml.forEach((column, index) => {
      let position: number | null = originalStart.column + index;
      for (const operation of operations) {
        if (operation.axis === "column" && position != null) {
          position = transformSpreadsheetIndex(position, operation);
        }
      }
      if (position != null) {
        columnByPosition.set(position, column);
      } else {
        const deletedName = decodeXml(column.match(/\bname="([^"]*)"/i)?.[1] || "");
        if (deletedName) columnTransforms.set(deletedName, null);
      }
    });
    let nextColumnId = Math.max(0, ...columnXml.map((column) => Number(column.match(/\bid="(\d+)"/i)?.[1] || 0))) + 1;
    const usedNames = new Set<string>();
    const nextColumns: string[] = [];
    for (let column = transformedStart.column; column <= transformedEnd.column; column += 1) {
      const existing = columnByPosition.get(column);
      const fallback = `Column${column - transformedStart.column + 1}`;
      const existingName = existing?.match(/\bname="([^"]*)"/i)?.[1];
      const decodedExistingName = existingName ? decodeXml(existingName) : "";
      const editedHeaderValue = hasHeaderRow ? editedData[transformedStart.row]?.[column] ?? "" : "";
      const editedHeader = hasHeaderRow ? String(editedHeaderValue).trim() : "";
      const header = editedHeader || decodedExistingName || fallback;
      let name = header;
      let suffix = 2;
      while (usedNames.has(name.toLowerCase())) name = `${header}${suffix++}`;
      usedNames.add(name.toLowerCase());
      if (decodedExistingName && decodedExistingName !== name) columnTransforms.set(decodedExistingName, name);
      if (hasHeaderRow && (typeof editedHeaderValue !== "string" || editedHeaderValue !== name)) {
        headerChanges.set(cellReference(transformedStart.row, column), name);
      }
      if (existing) {
        nextColumns.push(/\bname="[^"]*"/i.test(existing)
          ? existing.replace(/\bname="[^"]*"/i, `name="${escapeXml(name)}"`)
          : existing.replace(/^(<(?:[A-Za-z_][\w.-]*:)?tableColumn\b)/i, `$1 name="${escapeXml(name)}"`));
      } else {
        nextColumns.push(`<tableColumn id="${nextColumnId++}" name="${escapeXml(name)}"/>`);
      }
    }
    const opening = `<${tableColumns[1]}${tableColumns[2]}>`.replace(
      /\bcount="\d+"/i,
      `count="${nextColumns.length}"`,
    );
    output = output.replace(tableColumns[0], `${opening}${nextColumns.join("")}</${tableColumns[1]}>`);
  }

  if (operations.length > 0) {
    output = output.replace(
      /(<(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula)\b[^>]*>)([\s\S]*?)(<\/(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula)>)/gi,
      (_full, opening, formula, closing) => `${opening}${escapeXml(transformSpreadsheetFormula(decodeXml(formula), operations))}${closing}`,
    );
  }
  return { xml: output, headerChanges, columnTransforms, tableName, reference: transformedReference };
}

function transformWorkbookFormulaReferences(
  xml: string,
  sheetName: string,
  operations: SpreadsheetStructureOperation[],
): string {
  if (operations.length === 0) return xml;
  const escapedSheet = sheetName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const quotedSheet = sheetName.replace(/'/g, "''");
  const referencePattern = new RegExp(
    `((?:'${quotedSheet}'|${escapedSheet})!)((?:\\$?[A-Z]{1,3}\\$?\\d+)(?::\\$?[A-Z]{1,3}\\$?\\d+)?)`,
    "gi",
  );
  const transformFormulaCode = (formula: string) => {
    const transformReferences = (code: string) => code.replace(referencePattern, (_full, prefix, reference) => {
      const transformed = transformSpreadsheetRange(reference, operations);
      return transformed ? `${prefix}${transformed}` : `${prefix}#REF!`;
    });
    let output = "";
    let codeStart = 0;
    for (let index = 0; index < formula.length; index += 1) {
      if (formula[index] !== '"') continue;
      output += transformReferences(formula.slice(codeStart, index));
      const literalStart = index;
      index += 1;
      while (index < formula.length) {
        if (formula[index] !== '"') {
          index += 1;
          continue;
        }
        if (formula[index + 1] === '"') {
          index += 2;
          continue;
        }
        index += 1;
        break;
      }
      output += formula.slice(literalStart, index);
      codeStart = index;
      index -= 1;
    }
    return output + transformReferences(formula.slice(codeStart));
  };
  return xml.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula|definedName|formula1|formula2|formula|f)\b[^>]*>)([\s\S]*?)(<\/(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula|definedName|formula1|formula2|formula|f)>)/gi,
    (full, opening, formula, closing) => {
      const decodedFormula = decodeXml(formula);
      const transformedFormula = transformFormulaCode(decodedFormula);
      return transformedFormula === decodedFormula
        ? full
        : `${opening}${escapeXml(transformedFormula)}${closing}`;
    },
  );
}

function transformWorkbookSheetNameReferences(
  xml: string,
  renames: Map<string, string>,
): string {
  if (renames.size === 0) return xml;
  const renameByLowerName = new Map([...renames].map(([oldName, newName]) => [
    oldName.toLocaleLowerCase(),
    newName,
  ]));
  const quoteSheetName = (name: string) => `'${name.replace(/'/g, "''")}'`;
  const transformFormulaCode = (formula: string) => {
    let output = "";
    let codeStart = 0;
    const transformReferences = (code: string) => code.replace(
      /(^|[^A-Za-z0-9_.$\]])(?:(?:'((?:[^']|'')+)'|([^'!+\-*/^&=<>%,(){}\[\]\s:]+)):)?(?:'((?:[^']|'')+)'|([^'!+\-*/^&=<>%,(){}\[\]\s:]+))!/g,
      (
        full,
        prefix: string,
        quotedRangeStart: string | undefined,
        unquotedRangeStart: string | undefined,
        quotedName: string | undefined,
        unquotedName: string | undefined,
      ) => {
        const currentName = (quotedName || unquotedName || "").replace(/''/g, "'");
        if (quotedName && currentName.includes(":") && !currentName.startsWith("[")) {
          const [rangeStart, rangeEnd] = currentName.split(":");
          const nextRangeStart = renameByLowerName.get(rangeStart.toLocaleLowerCase()) || rangeStart;
          const nextRangeEnd = renameByLowerName.get(rangeEnd.toLocaleLowerCase()) || rangeEnd;
          return nextRangeStart !== rangeStart || nextRangeEnd !== rangeEnd
            ? `${prefix}${quoteSheetName(`${nextRangeStart}:${nextRangeEnd}`)}!`
            : full;
        }
        // [Book]Sheet!A1 and '[Book]Sheet'!A1 are external workbook
        // references, not references to a local worksheet being renamed.
        if (currentName.startsWith("[")) return full;
        const nextName = renameByLowerName.get(currentName.toLocaleLowerCase());
        const rangeStart = quotedRangeStart || unquotedRangeStart;
        if (rangeStart != null) {
          const currentRangeStart = rangeStart.replace(/''/g, "'");
          if (currentRangeStart.startsWith("[")) return full;
          const nextRangeStart = renameByLowerName.get(currentRangeStart.toLocaleLowerCase()) || currentRangeStart;
          const nextRangeEnd = nextName || currentName;
          return nextName || nextRangeStart !== currentRangeStart
            ? `${prefix}${quoteSheetName(`${nextRangeStart}:${nextRangeEnd}`)}!`
            : full;
        }
        return nextName ? `${prefix}${quoteSheetName(nextName)}!` : full;
      },
    );
    for (let index = 0; index < formula.length; index += 1) {
      if (formula[index] !== '"') continue;
      output += transformReferences(formula.slice(codeStart, index));
      const literalStart = index;
      index += 1;
      while (index < formula.length) {
        if (formula[index] !== '"') {
          index += 1;
          continue;
        }
        if (formula[index + 1] === '"') {
          index += 2;
          continue;
        }
        index += 1;
        break;
      }
      output += formula.slice(literalStart, index);
      codeStart = index;
      index -= 1;
    }
    return output + transformReferences(formula.slice(codeStart));
  };
  let output = xml.replace(
    /(<(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula|definedName|formula1|formula2|formula|f)\b[^>]*>)([\s\S]*?)(<\/(?:[A-Za-z_][\w.-]*:)?(?:calculatedColumnFormula|totalsRowFormula|definedName|formula1|formula2|formula|f)>)/gi,
    (full, opening, formula, closing) => {
      const decodedFormula = decodeXml(formula);
      const transformedFormula = transformFormulaCode(decodedFormula);
      return transformedFormula === decodedFormula
        ? full
        : `${opening}${escapeXml(transformedFormula)}${closing}`;
    },
  );
  output = output.replace(
    /<(?:(?:[A-Za-z_][\w.-]*):)?hyperlink\b[^>]*\blocation="[^"]*"[^>]*\/?\s*>/gi,
    (tag) => {
      if (/\b[A-Za-z_][\w.-]*:id="[^"]*"/i.test(tag)) return tag;
      const encodedLocation = tag.match(/\blocation="([^"]*)"/i)?.[1];
      if (encodedLocation == null) return tag;
      const location = decodeXml(encodedLocation);
      const transformed = transformFormulaCode(location);
      return transformed === location
        ? tag
        : spreadsheetSetXmlAttribute(tag, "location", escapeXml(transformed));
    },
  );
  output = output.replace(
    /<(?:(?:[A-Za-z_][\w.-]*):)?worksheetSource\b[^>]*\bsheet="[^"]*"[^>]*\/?\s*>/gi,
    (tag) => {
      if (/\b[A-Za-z_][\w.-]*:id="[^"]*"/i.test(tag)) return tag;
      const encodedName = tag.match(/\bsheet="([^"]*)"/i)?.[1];
      if (encodedName == null) return tag;
      const currentName = decodeXml(encodedName);
      const nextName = renameByLowerName.get(currentName.toLocaleLowerCase());
      return nextName
        ? spreadsheetSetXmlAttribute(tag, "sheet", escapeXml(nextName))
        : tag;
    },
  );
  return output;
}

function renameSpreadsheetWorkbookSheets(xml: string, renames: Map<string, string>): string {
  if (renames.size === 0) return xml;
  return xml.replace(/<(?:(?:[A-Za-z_][\w.-]*):)?sheet\b[^>]*\/?\s*>/g, (sheetTag) => {
    const currentName = sheetTag.match(/\bname="([^"]*)"/i)?.[1];
    if (currentName == null) return sheetTag;
    const nextName = renames.get(decodeXml(currentName));
    return nextName ? spreadsheetSetXmlAttribute(sheetTag, "name", escapeXml(nextName)) : sheetTag;
  });
}

function forceWorkbookRecalculation(xml: string): string {
  const workbookPrefix = xml.match(/<([A-Za-z_][\w.-]*:)?workbook\b/i)?.[1] || "";
  const calcPr = xml.match(
    /<(?:[A-Za-z_][\w.-]*:)?calcPr\b[^>]*(?:\/\s*>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?calcPr\s*>)/i,
  )?.[0];
  if (!calcPr) {
    return xml.replace(
      /<\/(?:[A-Za-z_][\w.-]*:)?workbook>/i,
      `<${workbookPrefix}calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></${workbookPrefix}workbook>`,
    );
  }
  let replacement = calcPr.match(/^<(?:[A-Za-z_][\w.-]*:)?calcPr\b[^>]*>/i)?.[0] || calcPr;
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

function spreadsheetSetXmlAttribute(openTag: string, name: string, value: string | undefined): string {
  const attribute = new RegExp(`\\s${name}="[^"]*"`, "i");
  // Callers may pass a whole xf/row container, not just its opening tag.
  return openTag.replace(/^<[^>]+>/, (opening) => {
    if (value == null) return opening.replace(attribute, "");
    if (attribute.test(opening)) return opening.replace(attribute, ` ${name}="${value}"`);
    return opening.replace(/\s*\/?\s*>$/, (ending) => ` ${name}="${value}"${ending}`);
  });
}

function spreadsheetXmlItems(xml: string, collection: string, item: string): string[] {
  const body = xml.match(new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${collection}\\b[^>]*>([\\s\\S]*?)<\\/(?:[A-Za-z_][\\w.-]*:)?${collection}>`,
    "i",
  ))?.[1] || "";
  return body.match(new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${item}\\b[^>]*?(?:\\/>|>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?${item}>)`,
    "gi",
  )) || [];
}

function spreadsheetXmlWithPrefix(xml: string, prefix: string): string {
  if (!prefix) return xml;
  return xml.replace(
    /<(\/?)((?![A-Za-z_][\w.-]*:)[A-Za-z_][\w.-]*)(?=[\s/>])/g,
    `<$1${prefix}$2`,
  );
}

function appendSpreadsheetXmlItem(xml: string, collection: string, itemXml: string): string {
  const pattern = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${collection}\\b[^>]*>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?${collection}>`,
    "i",
  );
  const existing = xml.match(pattern)?.[0];
  if (!existing) throw new SpreadsheetPreservationError(`The workbook style table has no ${collection} collection.`);
  const itemName = collection === "cellXfs" ? "xf" : collection === "fonts" ? "font" : "fill";
  const count = spreadsheetXmlItems(xml, collection, itemName).length + 1;
  const prefix = existing.match(/^<([A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*\b/i)?.[1] || "";
  const opening = existing.match(new RegExp(`^<${prefix}${collection}\\b[^>]*>`, "i"))?.[0] || `<${prefix}${collection}>`;
  const nextOpening = spreadsheetSetXmlAttribute(opening, "count", String(count));
  const next = existing
    .replace(opening, nextOpening)
    .replace(
      new RegExp(`<\\/${prefix}${collection}>$`, "i"),
      `${spreadsheetXmlWithPrefix(itemXml, prefix)}</${prefix}${collection}>`,
    );
  return xml.replace(existing, next);
}

function patchSpreadsheetContainerChild(container: string, tag: string, child: string | null): string {
  const prefix = container.match(/^<([A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*\b/i)?.[1] || "";
  const pattern = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${tag}\\b[^>]*(?:\\/>|>[\\s\\S]*?<\\/(?:[A-Za-z_][\\w.-]*:)?${tag}>)`,
    "i",
  );
  const nextChild = child == null ? null : spreadsheetXmlWithPrefix(child, prefix);
  if (pattern.test(container)) return nextChild == null ? container.replace(pattern, "") : container.replace(pattern, nextChild);
  if (child == null) return container;
  const containerName = container.match(/^<([A-Za-z_][\w:.-]*)\b/i)?.[1];
  if (!containerName) return container;
  if (/\/>\s*$/i.test(container)) return container.replace(/\/>\s*$/i, `>${nextChild}</${containerName}>`);
  return container.replace(new RegExp(`<\\/${containerName}>\\s*$`, "i"), `${nextChild}</${containerName}>`);
}

function patchSpreadsheetFont(
  fontXml: string,
  baseline: SpreadsheetCellStyle,
  edited: SpreadsheetCellStyle,
): string {
  let output = fontXml || "<font/>";
  if (baseline.bold !== edited.bold) output = patchSpreadsheetContainerChild(output, "b", edited.bold ? "<b/>" : null);
  if (baseline.italic !== edited.italic) output = patchSpreadsheetContainerChild(output, "i", edited.italic ? "<i/>" : null);
  if (baseline.fontSize !== edited.fontSize) output = patchSpreadsheetContainerChild(output, "sz", edited.fontSize ? `<sz val="${edited.fontSize}"/>` : null);
  if (baseline.fontFamily !== edited.fontFamily) output = patchSpreadsheetContainerChild(output, "name", edited.fontFamily ? `<name val="${escapeXml(edited.fontFamily)}"/>` : null);
  if (baseline.color !== edited.color) output = patchSpreadsheetContainerChild(output, "color", edited.color ? `<color rgb="FF${edited.color.replace(/^#/, "").toUpperCase()}"/>` : null);
  return output;
}

function patchSpreadsheetAlignment(xfXml: string, align: SpreadsheetCellStyle["align"]): string {
  const current = xfXml.match(/<(?:[A-Za-z_][\w.-]*:)?alignment\b[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?alignment>)/i)?.[0];
  if (!align) return current ? xfXml.replace(current, "") : xfXml;
  const next = spreadsheetSetXmlAttribute(current || "<alignment/>", "horizontal", align);
  if (current) return xfXml.replace(current, next);
  return patchSpreadsheetContainerChild(xfXml, "alignment", next);
}

function createSpreadsheetStyleRegistry(stylesXml: string) {
  let xml = stylesXml;
  const styleIndex = (baseIndex: number, baseline: SpreadsheetCellStyle, edited: SpreadsheetCellStyle): number => {
    const fonts = spreadsheetXmlItems(xml, "fonts", "font");
    const fills = spreadsheetXmlItems(xml, "fills", "fill");
    const xfs = spreadsheetXmlItems(xml, "cellXfs", "xf");
    let xf = xfs[baseIndex] || xfs[0] || '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>';
    if (!sameSpreadsheetObject(
      [baseline.bold, baseline.italic, baseline.fontSize, baseline.fontFamily, baseline.color],
      [edited.bold, edited.italic, edited.fontSize, edited.fontFamily, edited.color],
    )) {
      const fontId = Number(xf.match(/\bfontId="(\d+)"/i)?.[1] || 0);
      const font = patchSpreadsheetFont(fonts[fontId] || fonts[0] || "<font/>", baseline, edited);
      let nextFontId = fonts.indexOf(font);
      if (nextFontId < 0) {
        nextFontId = fonts.length;
        xml = appendSpreadsheetXmlItem(xml, "fonts", font);
      }
      xf = spreadsheetSetXmlAttribute(xf, "fontId", String(nextFontId));
      xf = spreadsheetSetXmlAttribute(xf, "applyFont", "1");
    }
    if (baseline.fill !== edited.fill) {
      const fill = edited.fill
        ? `<fill><patternFill patternType="solid"><fgColor rgb="FF${edited.fill.replace(/^#/, "").toUpperCase()}"/><bgColor indexed="64"/></patternFill></fill>`
        : '<fill><patternFill patternType="none"/></fill>';
      let fillId = fills.indexOf(fill);
      if (fillId < 0) {
        fillId = fills.length;
        xml = appendSpreadsheetXmlItem(xml, "fills", fill);
      }
      xf = spreadsheetSetXmlAttribute(xf, "fillId", String(fillId));
      xf = spreadsheetSetXmlAttribute(xf, "applyFill", "1");
    }
    if (baseline.align !== edited.align) {
      xf = patchSpreadsheetAlignment(xf, edited.align);
      xf = spreadsheetSetXmlAttribute(xf, "applyAlignment", edited.align ? "1" : undefined);
    }
    const updatedXfs = spreadsheetXmlItems(xml, "cellXfs", "xf");
    let nextXfId = updatedXfs.indexOf(xf);
    if (nextXfId < 0) {
      nextXfId = updatedXfs.length;
      xml = appendSpreadsheetXmlItem(xml, "cellXfs", xf);
    }
    return nextXfId;
  };
  return { styleIndex, xml: () => xml };
}

function worksheetCellStyleIndex(xml: string, reference: string): number {
  const cell = (xml.match(/<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="[A-Z]+\d+"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>)/gi) || [])
    .find((item) => item.match(/\br="([A-Z]+\d+)"/i)?.[1].toUpperCase() === reference.toUpperCase());
  return Number(cell?.match(/\bs="(\d+)"/i)?.[1] || 0);
}

function patchWorksheetCellStyle(xml: string, reference: string, styleIndexValue: number): string {
  let found = false;
  const output = xml.replace(
    /<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*\br="[A-Z]+\d+"[^>]*(?:\/>|>[\s\S]*?<\/(?:[A-Za-z_][\w.-]*:)?c>)/gi,
    (cellXml) => {
      if (cellXml.match(/\br="([A-Z]+\d+)"/i)?.[1].toUpperCase() !== reference.toUpperCase()) return cellXml;
      found = true;
      return cellXml.replace(/^<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*>/i, (opening) => spreadsheetSetXmlAttribute(opening, "s", String(styleIndexValue)));
    },
  );
  if (!found) throw new SpreadsheetPreservationError(`Unable to apply the style to cell ${reference}.`);
  return output;
}

function spreadsheetMetadataSheetXml(metadata: string): string {
  const chunks = Array.from(
    { length: Math.max(1, Math.ceil(metadata.length / 30_000)) },
    (_unused, index) => metadata.slice(index * 30_000, (index + 1) * 30_000),
  );
  const rows = chunks.map((chunk, index) => (
    `<row r="${index + 1}"><c r="A${index + 1}" t="inlineStr"><is><t${/^\s|\s$/.test(chunk) ? ' xml:space="preserve"' : ""}>${escapeXml(chunk)}</t></is></c></row>`
  )).join("");
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    + `<dimension ref="A1:A${chunks.length}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="15"/><sheetData>${rows}</sheetData>`
    + '</worksheet>';
}

function spreadsheetEmptySheetXml(sheet: SpreadsheetSheetSnapshot): string {
  const dimensions = sheetDimensions(sheet.data);
  const lastReference = cellReference(dimensions.rows - 1, dimensions.columns - 1);
  const dimension = lastReference === "A1" ? "A1" : `A1:${lastReference}`;
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    + `<dimension ref="${dimension}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="15"/><sheetData/>`
    + '</worksheet>';
}

async function appendSpreadsheetWorksheet(
  zip: JSZip,
  sheet: SpreadsheetSheetSnapshot,
): Promise<string> {
  const workbookEntry = zip.file("xl/workbook.xml");
  const relationshipsEntry = zip.file("xl/_rels/workbook.xml.rels");
  const contentTypesEntry = zip.file("[Content_Types].xml");
  if (!workbookEntry || !relationshipsEntry || !contentTypesEntry) {
    throw new SpreadsheetPreservationError("The workbook package cannot add another worksheet.");
  }

  let workbookXml = await workbookEntry.async("text");
  let relationshipsXml = await relationshipsEntry.async("text");
  let contentTypesXml = await contentTypesEntry.async("text");
  const worksheetIndexes = Object.keys(zip.files)
    .map((path) => Number(path.match(/^xl\/worksheets\/sheet(\d+)\.xml$/i)?.[1] || 0));
  const sheetPart = `xl/worksheets/sheet${Math.max(0, ...worksheetIndexes) + 1}.xml`;
  const relationshipIds = Array.from(relationshipsXml.matchAll(/\bId="rId(\d+)"/g), (match) => Number(match[1]));
  const relationshipId = `rId${Math.max(0, ...relationshipIds) + 1}`;
  const sheetIds = Array.from(
    workbookXml.matchAll(/<(?:(?:[A-Za-z_][\w.-]*):)?sheet\b[^>]*\bsheetId="(\d+)"/g),
    (match) => Number(match[1]),
  );
  const sheetId = Math.max(0, ...sheetIds) + 1;
  const workbookPrefix = workbookXml.match(/<([A-Za-z_][\w.-]*:)?workbook\b/i)?.[1] || "";
  const sheetXml = `<${workbookPrefix}sheet name="${escapeXml(sheet.name)}" sheetId="${sheetId}"${sheet.hidden ? ' state="hidden"' : ""} r:id="${relationshipId}"/>`;
  const sheetsBlock = new RegExp(`<${workbookPrefix}sheets\\b[^>]*>[\\s\\S]*?<\\/${workbookPrefix}sheets>`, "i");
  if (sheetsBlock.test(workbookXml)) {
    workbookXml = workbookXml.replace(new RegExp(`<\\/${workbookPrefix}sheets>`, "i"), `${sheetXml}</${workbookPrefix}sheets>`);
  } else {
    const emptySheets = new RegExp(`<${workbookPrefix}sheets\\b[^>]*/\\s*>`, "i");
    if (!emptySheets.test(workbookXml)) {
      throw new SpreadsheetPreservationError("The workbook has no worksheet list.");
    }
    workbookXml = workbookXml.replace(emptySheets, `<${workbookPrefix}sheets>${sheetXml}</${workbookPrefix}sheets>`);
  }

  const relationshipsPrefix = relationshipsXml.match(/<([A-Za-z_][\w.-]*:)?Relationships\b/i)?.[1] || "";
  const relationship = `<${relationshipsPrefix}Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/${sheetPart.split("/").pop()}"/>`;
  const relationshipsClosing = new RegExp(`<\\/${relationshipsPrefix}Relationships>\\s*$`, "i");
  if (!relationshipsClosing.test(relationshipsXml)) {
    throw new SpreadsheetPreservationError("The workbook has no relationship list.");
  }
  relationshipsXml = relationshipsXml.replace(
    relationshipsClosing,
    `${relationship}</${relationshipsPrefix}Relationships>`,
  );

  const contentTypesPrefix = contentTypesXml.match(/<([A-Za-z_][\w.-]*:)?Types\b/i)?.[1] || "";
  const contentType = `<${contentTypesPrefix}Override PartName="/${sheetPart}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`;
  const contentTypesClosing = new RegExp(`<\\/${contentTypesPrefix}Types>\\s*$`, "i");
  if (!contentTypesClosing.test(contentTypesXml)) {
    throw new SpreadsheetPreservationError("The workbook has no content type list.");
  }
  contentTypesXml = contentTypesXml.replace(
    contentTypesClosing,
    `${contentType}</${contentTypesPrefix}Types>`,
  );

  zip.file(sheetPart, spreadsheetEmptySheetXml(sheet));
  zip.file("xl/workbook.xml", workbookXml);
  zip.file("xl/_rels/workbook.xml.rels", relationshipsXml);
  zip.file("[Content_Types].xml", contentTypesXml);
  return sheetPart;
}

async function writeSpreadsheetEditorMetadata(
  zip: JSZip,
  sheetParts: Map<string, string>,
  metadata: string,
): Promise<void> {
  const existingPart = sheetParts.get("_manor_charts");
  if (existingPart) {
    zip.file(existingPart, spreadsheetMetadataSheetXml(metadata));
    return;
  }
  const workbookEntry = zip.file("xl/workbook.xml");
  const relationshipsEntry = zip.file("xl/_rels/workbook.xml.rels");
  const contentTypesEntry = zip.file("[Content_Types].xml");
  if (!workbookEntry || !relationshipsEntry || !contentTypesEntry) {
    throw new SpreadsheetPreservationError("The workbook package cannot store editor chart metadata.");
  }
  let workbookXml = await workbookEntry.async("text");
  let relationshipsXml = await relationshipsEntry.async("text");
  let contentTypesXml = await contentTypesEntry.async("text");
  const worksheetIndexes = Object.keys(zip.files)
    .map((path) => Number(path.match(/^xl\/worksheets\/sheet(\d+)\.xml$/i)?.[1] || 0));
  const sheetPart = `xl/worksheets/sheet${Math.max(0, ...worksheetIndexes) + 1}.xml`;
  const relationshipIds = Array.from(relationshipsXml.matchAll(/\bId="rId(\d+)"/g), (match) => Number(match[1]));
  const relationshipId = `rId${Math.max(0, ...relationshipIds) + 1}`;
  const sheetIds = Array.from(workbookXml.matchAll(/<(?:(?:[A-Za-z_][\w.-]*):)?sheet\b[^>]*\bsheetId="(\d+)"/g), (match) => Number(match[1]));
  const sheetId = Math.max(0, ...sheetIds) + 1;
  const workbookPrefix = workbookXml.match(/<([A-Za-z_][\w.-]*:)?workbook\b/i)?.[1] || "";
  const sheetXml = `<${workbookPrefix}sheet name="_manor_charts" sheetId="${sheetId}" state="hidden" r:id="${relationshipId}"/>`;
  if (new RegExp(`<${workbookPrefix}sheets\\b[^>]*>[\\s\\S]*?<\\/${workbookPrefix}sheets>`, "i").test(workbookXml)) {
    workbookXml = workbookXml.replace(new RegExp(`<\\/${workbookPrefix}sheets>`, "i"), `${sheetXml}</${workbookPrefix}sheets>`);
  } else {
    throw new SpreadsheetPreservationError("The workbook has no worksheet list.");
  }
  const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/${sheetPart.split("/").pop()}"/>`;
  relationshipsXml = relationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
  contentTypesXml = contentTypesXml.replace(
    /<\/Types>\s*$/i,
    `<Override PartName="/${sheetPart}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>`,
  );
  zip.file(sheetPart, spreadsheetMetadataSheetXml(metadata));
  zip.file("xl/workbook.xml", workbookXml);
  zip.file("xl/_rels/workbook.xml.rels", relationshipsXml);
  zip.file("[Content_Types].xml", contentTypesXml);
}

function transformSpreadsheetDrawingXml(xml: string, operations: SpreadsheetStructureOperation[]): string {
  let output = xml;
  for (const operation of operations) {
    const tag = operation.axis === "row" ? "row" : "col";
    output = output.replace(new RegExp(`(<(?:[A-Za-z_][\\w.-]*:)?${tag}>)(\\d+)(<\\/(?:[A-Za-z_][\\w.-]*:)?${tag}>)`, "gi"),
      (_full, opening, indexText, closing) => {
        const next = transformSpreadsheetIndex(Number(indexText), operation);
        return `${opening}${next ?? operation.index}${closing}`;
      });
  }
  return output;
}

export async function preserveSpreadsheetFile(
  original: ArrayBuffer,
  baselineSheets: SpreadsheetSheetSnapshot[],
  editedSheets: SpreadsheetSheetSnapshot[],
  filename: string,
): Promise<File> {
  const normalizedSheetNames = editedSheets.map((sheet) => sheet.name.trim().toLocaleLowerCase());
  if (
    editedSheets.some((sheet) => !isValidSpreadsheetWorksheetName(sheet.name))
    || new Set(normalizedSheetNames).size !== normalizedSheetNames.length
  ) {
    throw new SpreadsheetPreservationError(
      "Worksheet names must be unique and Excel-compatible (1–31 characters; not History; no leading or trailing apostrophe, control characters, or \\ / ? * [ ] :).",
    );
  }

  let zip: JSZip;
  try {
    zip = await JSZip.loadAsync(original);
  } catch {
    return buildSpreadsheetFile(editedSheets, filename);
  }

  if (
    editedSheets.length < baselineSheets.length
  ) {
    throw new SpreadsheetPreservationError("Removing or reordering worksheets is not available in this editor yet.");
  }
  const baselineSheetNames = baselineSheets.map((sheet) => sheet.name.trim().toLocaleLowerCase());
  const editedExistingSheetNames = normalizedSheetNames.slice(0, baselineSheets.length);
  if (baselineSheets.some((baseline, sheetIndex) => (
    baseline.sourceName != null && editedSheets[sheetIndex]?.sourceName !== baseline.sourceName
  ))) {
    throw new SpreadsheetPreservationError("Removing or reordering worksheets is not available in this editor yet.");
  }
  const hasSameExistingSheetSet = baselineSheetNames.length === editedExistingSheetNames.length
    && baselineSheetNames.every((name) => editedExistingSheetNames.includes(name));
  if (
    hasSameExistingSheetSet
    && baselineSheetNames.some((name, sheetIndex) => editedExistingSheetNames[sheetIndex] !== name)
  ) {
    throw new SpreadsheetPreservationError("Removing or reordering worksheets is not available in this editor yet.");
  }
  const renamedSheets = new Map<string, string>();
  baselineSheets.forEach((baseline, sheetIndex) => {
    const edited = editedSheets[sheetIndex];
    if (edited && edited.name !== baseline.name) renamedSheets.set(baseline.name, edited.name);
  });
  const addedSheets = editedSheets.slice(baselineSheets.length);
  for (const sheet of addedSheets) await appendSpreadsheetWorksheet(zip, sheet);
  const effectiveBaselineSheets = [
    ...baselineSheets,
    ...addedSheets.map((sheet): SpreadsheetSheetSnapshot => ({
      name: sheet.name,
      data: [[null]],
      styles: {},
      editorCharts: [],
      structureOperations: [],
      hidden: sheet.hidden,
    })),
  ];
  const sheetParts = await workbookSheetParts(zip);
  const metadataSourceSheet = editedSheets.find((sheet) => (sheet.editorCharts?.length || 0) > 0)
    || editedSheets.find((sheet) => !sheet.hidden && sheet.name !== "_manor_charts")
    || editedSheets[0];
  const baselineMetadataSourceSheet = metadataSourceSheet
    ? effectiveBaselineSheets.find((sheet) => sheet.name === metadataSourceSheet.name)
    : undefined;
  const editorChartsChanged = !sameSpreadsheetObject(
    baselineMetadataSourceSheet?.editorCharts || [],
    metadataSourceSheet?.editorCharts || [],
  );
  const existingEditorMetadataChanged = sheetParts.has("_manor_charts") && !sameSpreadsheetObject(
    baselineMetadataSourceSheet?.styles || {},
    metadataSourceSheet?.styles || {},
  );
  const sheetChanges: Array<{
    baseline: SpreadsheetSheetSnapshot;
    edited: SpreadsheetSheetSnapshot;
    structuredBaselineData: SpreadsheetCellValue[][];
    structuredBaselineStyles: Record<string, SpreadsheetCellStyle>;
    operations: SpreadsheetStructureOperation[];
    changes: Map<string, SpreadsheetCellValue>;
    directChanges: Set<string>;
    styleChanges: Map<string, { baseline: SpreadsheetCellStyle; edited: SpreadsheetCellStyle }>;
    dimensions: { rows: number; columns: number };
  }> = [];
  let mutationCount = addedSheets.length;

  for (let sheetIndex = 0; sheetIndex < effectiveBaselineSheets.length; sheetIndex += 1) {
    const baseline = effectiveBaselineSheets[sheetIndex];
    const edited = editedSheets[sheetIndex];
    if (!edited) continue;
    const operations = structuredClone(edited.structureOperations || []);
    const structuredBaselineData = applySpreadsheetStructureToData(baseline.data, operations);
    const structuredBaselineStyles = transformSpreadsheetStyleMap(baseline.styles, operations);
    const editedDimensions = sheetDimensions(edited.data);

    const changes = new Map<string, SpreadsheetCellValue>();
    for (let row = 0; row < editedDimensions.rows; row += 1) {
      for (let column = 0; column < editedDimensions.columns; column += 1) {
        if (!sameCellValue(structuredBaselineData[row]?.[column], edited.data[row]?.[column])) {
          changes.set(cellReference(row, column), edited.data[row]?.[column] ?? "");
        }
      }
    }
    const directChanges = new Set(changes.keys());
    const styleChanges = new Map<string, { baseline: SpreadsheetCellStyle; edited: SpreadsheetCellStyle }>();
    const styleKeys = new Set([...Object.keys(structuredBaselineStyles), ...Object.keys(edited.styles || {})]);
    for (const key of styleKeys) {
      const baselineStyle = structuredBaselineStyles[key] || {};
      const editedStyle = edited.styles?.[key] || {};
      if (!sameSpreadsheetObject(baselineStyle, editedStyle)) {
        const [rowText, columnText] = key.split(":");
        const reference = cellReference(Number(rowText), Number(columnText));
        styleChanges.set(reference, { baseline: baselineStyle, edited: editedStyle });
        if (!changes.has(reference)) changes.set(reference, edited.data[Number(rowText)]?.[Number(columnText)] ?? "");
      }
    }
    mutationCount += directChanges.size + styleChanges.size + operations.length;
    sheetChanges.push({
      baseline,
      edited,
      structuredBaselineData,
      structuredBaselineStyles,
      operations,
      changes,
      directChanges,
      styleChanges,
      dimensions: editedDimensions,
    });
  }
  if (editorChartsChanged || existingEditorMetadataChanged) mutationCount += 1;
  mutationCount += renamedSheets.size;

  if (mutationCount === 0) {
    return new File([original], filename, {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
  }

  const baselineContextSheets = sheetChanges.map((plan) => ({ name: plan.baseline.name, data: plan.structuredBaselineData }));
  const editedContextSheets = editedSheets.map((sheet) => ({ name: sheet.name, data: sheet.data }));
  const baselineEvaluationState = createSpreadsheetFormulaEvaluationState();
  const editedEvaluationState = createSpreadsheetFormulaEvaluationState();
  const formulaUpdates: Array<{
    plan: (typeof sheetChanges)[number];
    reference: string;
    formula: string;
    baselineValue: SpreadsheetFormulaValue | null;
    editedValue: SpreadsheetFormulaValue | null;
    dependencies: SpreadsheetFormulaDependencies;
  }> = [];
  const affectedKeys = new Set<string>();
  const affectedCells: SpreadsheetCellPosition[] = [];
  const markAffected = (sheetName: string, reference: string) => {
    const cell = spreadsheetCellPosition(sheetName, reference);
    if (!cell) return;
    const key = `${cell.sheetName}!${cell.row}:${cell.column}`;
    if (affectedKeys.has(key)) return;
    affectedKeys.add(key);
    affectedCells.push(cell);
  };

  const hasStyleChanges = sheetChanges.some((plan) => plan.styleChanges.size > 0);
  const stylesFile = hasStyleChanges ? zip.file("xl/styles.xml") : null;
  if (hasStyleChanges && !stylesFile) {
    throw new SpreadsheetPreservationError("The workbook has no editable style table.");
  }
  const styleRegistry = stylesFile
    ? createSpreadsheetStyleRegistry(await stylesFile.async("text"))
    : null;

  for (const plan of sheetChanges) {
    const { baseline, edited, directChanges, dimensions } = plan;
    for (const reference of directChanges) markAffected(edited.name, reference);
    const baselineContext: SpreadsheetFormulaContext = {
      sheets: baselineContextSheets,
      currentSheetName: baseline.name,
    };
    const editedContext: SpreadsheetFormulaContext = {
      sheets: editedContextSheets,
      currentSheetName: edited.name,
    };
    for (let row = 0; row < dimensions.rows; row += 1) {
      for (let column = 0; column < dimensions.columns; column += 1) {
        const formula = edited.data[row]?.[column];
        if (typeof formula !== "string" || !formula.trim().startsWith("=")) continue;
        const reference = cellReference(row, column);
        const baselineFormula = plan.structuredBaselineData[row]?.[column];
        const baselineValue = typeof baselineFormula === "string" && baselineFormula.trim().startsWith("=")
          ? evaluateSpreadsheetFormulaValue(
              plan.structuredBaselineData,
              baselineFormula,
              new Set(),
              baselineContext,
              baselineEvaluationState,
            )
          : null;
        const editedValue = evaluateSpreadsheetFormulaValue(
          edited.data,
          formula,
          new Set(),
          editedContext,
          editedEvaluationState,
        );
        formulaUpdates.push({
          plan,
          reference,
          formula,
          baselineValue,
          editedValue,
          dependencies: spreadsheetFormulaDependencies(formula, edited.name),
        });
        if (directChanges.has(reference) || (editedValue != null && !Object.is(baselineValue, editedValue))) {
          markAffected(edited.name, reference);
        }
      }
    }
  }

  for (let affectedIndex = 0; affectedIndex < affectedCells.length; affectedIndex += 1) {
    const affected = affectedCells[affectedIndex];
    for (const update of formulaUpdates) {
      if (update.editedValue != null || update.plan.directChanges.has(update.reference)) continue;
      const formulaCell = spreadsheetCellPosition(update.plan.edited.name, update.reference);
      if (!formulaCell) continue;
      const key = `${formulaCell.sheetName}!${formulaCell.row}:${formulaCell.column}`;
      if (affectedKeys.has(key)) continue;
      if (
        update.dependencies.dynamic
        || update.dependencies.ranges.some((dependency) => spreadsheetDependencyContains(dependency, affected))
      ) markAffected(update.plan.edited.name, update.reference);
    }
  }

  for (const plan of sheetChanges) {
    const formulaCache = new Map<string, SpreadsheetFormulaCacheValue>();
    for (const update of formulaUpdates) {
      if (update.plan !== plan) continue;
      const formulaCell = spreadsheetCellPosition(plan.edited.name, update.reference);
      if (!formulaCell) continue;
      const key = `${formulaCell.sheetName}!${formulaCell.row}:${formulaCell.column}`;
      const directlyChanged = plan.directChanges.has(update.reference);
      const calculatedValueChanged = update.editedValue != null
        && !Object.is(update.baselineValue, update.editedValue);
      if (!directlyChanged && !calculatedValueChanged && !affectedKeys.has(key)) continue;
      plan.changes.set(update.reference, update.formula);
      formulaCache.set(update.reference, update.editedValue ?? UNCALCULATED_FORMULA_VALUE);
    }
    const sheetPart = sheetParts.get(plan.baseline.name);
    const sheetFile = sheetPart ? zip.file(sheetPart) : null;
    if (!sheetPart || !sheetFile) {
      throw new SpreadsheetPreservationError(`Worksheet “${plan.baseline.name}” could not be resolved in the original file.`);
    }
    const originalSheetXml = await sheetFile.async("text");
    let sheetXml = transformWorksheetStructureXml(
      materializeSharedSpreadsheetFormulas(
        originalSheetXml,
        plan.baseline.data,
        plan.directChanges,
        plan.operations.length > 0,
      ),
      plan.operations,
      plan.dimensions,
    );
    const baseStyleIndexes = new Map(
      Array.from(plan.styleChanges.keys(), (reference) => [reference, worksheetCellStyleIndex(sheetXml, reference)]),
    );
    sheetXml = patchSheetXml(
      sheetXml,
      plan.changes,
      formulaCache,
      plan.directChanges,
    );
    if (styleRegistry) {
      for (const [reference, styles] of plan.styleChanges) {
        const styleIndexValue = styleRegistry.styleIndex(baseStyleIndexes.get(reference) || 0, styles.baseline, styles.edited);
        sheetXml = patchWorksheetCellStyle(sheetXml, reference, styleIndexValue);
      }
    }
    zip.file(sheetPart, sheetXml);
  }
  if (styleRegistry) zip.file("xl/styles.xml", styleRegistry.xml());

  const tableColumnTransforms: Array<{
    part: string;
    sheetPart: string;
    tableName: string;
    reference: string;
    columnTransforms: Map<string, string | null>;
  }> = [];
  for (const plan of sheetChanges) {
    const sheetPart = sheetParts.get(plan.baseline.name);
    const sheetRelationshipsPart = sheetPart
      ? `${sheetPart.slice(0, sheetPart.lastIndexOf("/") + 1)}_rels/${sheetPart.split("/").pop()}.rels`
      : undefined;
    const sheetRelationshipsEntry = sheetRelationshipsPart ? zip.file(sheetRelationshipsPart) : null;
    if (sheetPart && sheetRelationshipsEntry) {
      const sheetRelationshipsXml = await sheetRelationshipsEntry.async("text");
      for (const relationship of sheetRelationshipsXml.match(/<Relationship\b[^>]*\/>/g) || []) {
        const type = relationship.match(/\bType="([^"]+)"/i)?.[1] || "";
        const target = relationship.match(/\bTarget="([^"]+)"/i)?.[1];
        if (!target) continue;
        const relatedPart = resolveSpreadsheetPartTarget(sheetPart, target);
        const relatedEntry = zip.file(relatedPart);
        if (!relatedEntry) continue;
        if (type.endsWith("/drawing") && plan.operations.length > 0) {
          zip.file(relatedPart, transformSpreadsheetDrawingXml(await relatedEntry.async("text"), plan.operations));
        } else if (type.endsWith("/table")) {
          const table = transformSpreadsheetTableXml(
            await relatedEntry.async("text"),
            plan.operations,
            plan.edited.data,
          );
          zip.file(relatedPart, table.xml);
          if (table.headerChanges.size > 0) {
            const worksheetEntry = zip.file(sheetPart);
            if (!worksheetEntry) {
              throw new SpreadsheetPreservationError(`Worksheet “${plan.baseline.name}” could not be updated with its table headers.`);
            }
            zip.file(sheetPart, patchSheetXml(
              await worksheetEntry.async("text"),
              table.headerChanges,
              new Map(),
              new Set(table.headerChanges.keys()),
            ));
          }
          if (table.columnTransforms.size > 0 && table.tableName && table.reference) {
            tableColumnTransforms.push({
              part: relatedPart,
              sheetPart,
              tableName: table.tableName,
              reference: table.reference,
              columnTransforms: table.columnTransforms,
            });
          }
        }
      }
    }
    if (plan.operations.length === 0) continue;
    const formulaPartPaths = Object.keys(zip.files).filter((path) => (
      /^xl\/(?:worksheets|charts|tables)\/.*\.xml$/i.test(path) || path === "xl/workbook.xml"
    ));
    for (const partPath of formulaPartPaths) {
      const part = zip.file(partPath);
      if (!part) continue;
      zip.file(partPath, transformWorkbookFormulaReferences(await part.async("text"), plan.baseline.name, plan.operations));
    }
  }
  if (renamedSheets.size > 0) {
    const formulaPartPaths = Object.keys(zip.files).filter((path) => /^xl\/.*\.xml$/i.test(path));
    for (const partPath of formulaPartPaths) {
      const part = zip.file(partPath);
      if (!part) continue;
      zip.file(partPath, transformWorkbookSheetNameReferences(await part.async("text"), renamedSheets));
    }
  }
  if (tableColumnTransforms.length > 0) {
    const formulaPartPaths = Object.keys(zip.files).filter((path) => /^xl\/.*\.xml$/i.test(path));
    for (const partPath of formulaPartPaths) {
      const part = zip.file(partPath);
      if (!part) continue;
      let partXml = await part.async("text");
      for (const table of tableColumnTransforms) {
        partXml = transformSpreadsheetStructuredReferenceXml(
          partXml,
          table.tableName,
          table.columnTransforms,
          partPath === table.part,
        );
        if (partPath === table.sheetPart) {
          partXml = transformSpreadsheetWorksheetStructuredReferenceXml(
            partXml,
            table.tableName,
            table.columnTransforms,
            table.reference,
          );
        }
      }
      zip.file(partPath, partXml);
    }
  }
  if ((editorChartsChanged || existingEditorMetadataChanged) && metadataSourceSheet) {
    await writeSpreadsheetEditorMetadata(zip, sheetParts, JSON.stringify({
      charts: metadataSourceSheet.editorCharts || [],
      styles: metadataSourceSheet.styles || {},
    }));
  }
  const workbookFile = zip.file("xl/workbook.xml");
  if (workbookFile) {
    const workbookXml = renameSpreadsheetWorkbookSheets(await workbookFile.async("text"), renamedSheets);
    zip.file("xl/workbook.xml", forceWorkbookRecalculation(workbookXml));
  }
  const output = await zip.generateAsync({ type: "arraybuffer", compression: "DEFLATE" });
  return new File([output], spreadsheetOutputName(filename), {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}
