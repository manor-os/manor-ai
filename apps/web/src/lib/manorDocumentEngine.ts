import type JSZipArchive from "jszip";

const WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

type CssProperties = Record<string, string>;

interface ParagraphProperties {
  css: CssProperties;
  numId?: string;
  level?: number;
  pageBreakBefore?: boolean;
  keepNext?: boolean;
  keepLines?: boolean;
  widowControl?: boolean;
}

interface WordStyle {
  id: string;
  type: string;
  name: string;
  basedOn?: string;
  paragraph: ParagraphProperties;
  run: CssProperties;
}

interface NumberingLevel {
  format: string;
  text: string;
  start: number;
  paragraph: ParagraphProperties;
  run: CssProperties;
}

interface Relationship {
  target: string;
  type: string;
}

interface FontDefinition {
  alternate?: string;
  generic: "serif" | "sans-serif" | "monospace";
}

type DynamicFieldKind = "page" | "num-pages";

interface FieldState {
  instruction: string;
  resultVisible: boolean;
  resultRendered: boolean;
  kind?: DynamicFieldKind;
}

interface RenderContext {
  partPath: string;
  paragraphIndex: number;
  revisionBlockDepth: number;
  styles: Map<string, WordStyle>;
  resolvedStyles: Map<string, WordStyle>;
  defaultParagraph: ParagraphProperties;
  defaultRun: CssProperties;
  numbering: Map<string, Map<number, NumberingLevel>>;
  counters: Map<string, number[]>;
  relationships: Map<string, Relationship>;
  media: Map<string, string>;
  themeColors: Map<string, string>;
  themeFonts: Map<string, string>;
  fontDefinitions: Map<string, FontDefinition>;
  footnotes: Map<string, string>;
  fieldStack: FieldState[];
  trackParagraphs: boolean;
}

export interface ManorDocumentLayout {
  pageWidthPx: number;
  pageHeightPx: number;
  marginTopPx: number;
  marginRightPx: number;
  marginBottomPx: number;
  marginLeftPx: number;
  headerDistancePx: number;
  footerDistancePx: number;
}

export interface ManorDocumentRender {
  html: string;
  headerHtml: string;
  footerHtml: string;
  firstHeaderHtml: string;
  firstFooterHtml: string;
  evenHeaderHtml: string;
  evenFooterHtml: string;
  differentFirstPage: boolean;
  differentEvenPages: boolean;
  layout: ManorDocumentLayout;
  fonts: string[];
}

function parseXml(value: string, label: string): XMLDocument {
  const parsed = new DOMParser().parseFromString(value, "application/xml");
  if (parsed.querySelector("parsererror")) throw new Error(`The DOCX ${label} part is malformed.`);
  return parsed;
}

function elementChildren(node: ParentNode | null | undefined, name?: string): Element[] {
  if (!node) return [];
  return Array.from(node.childNodes).filter((child): child is Element => (
    child.nodeType === Node.ELEMENT_NODE && (!name || (child as Element).localName === name)
  ));
}

function firstChild(node: ParentNode | null | undefined, name: string): Element | undefined {
  return node ? elementChildren(node, name)[0] : undefined;
}

function descendants(node: ParentNode | null | undefined, name: string): Element[] {
  if (!node) return [];
  return Array.from((node as Document | Element).getElementsByTagNameNS("*", name));
}

function wordAttribute(element: Element | null | undefined, name: string): string | undefined {
  if (!element) return undefined;
  return element.getAttributeNS(WORD_NS, name)
    || element.getAttribute(`w:${name}`)
    || element.getAttribute(name)
    || undefined;
}

function relationshipAttribute(element: Element | null | undefined, name: string): string | undefined {
  return element?.getAttribute(name) || element?.getAttribute(`r:${name}`) || undefined;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function styleText(properties: CssProperties): string {
  return Object.entries(properties)
    .filter(([, value]) => Boolean(value))
    .map(([property, value]) => `${property}:${value}`)
    .join(";");
}

function mergeParagraphProperties(...values: Array<ParagraphProperties | undefined>): ParagraphProperties {
  return values.reduce<ParagraphProperties>((merged, value) => ({
    css: { ...merged.css, ...(value?.css || {}) },
    numId: value?.numId ?? merged.numId,
    level: value?.level ?? merged.level,
    pageBreakBefore: value?.pageBreakBefore ?? merged.pageBreakBefore,
    keepNext: value?.keepNext ?? merged.keepNext,
    keepLines: value?.keepLines ?? merged.keepLines,
    widowControl: value?.widowControl ?? merged.widowControl,
  }), { css: {} });
}

function wordBoolean(element: Element | undefined): boolean | undefined {
  if (!element) return undefined;
  const value = (wordAttribute(element, "val") || "true").toLowerCase();
  return !["0", "false", "off", "none"].includes(value);
}

function twipsToPixels(value: string | undefined): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed / 15 : undefined;
}

function halfPointsToPixels(value: string | undefined): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed * 2 / 3 : undefined;
}

function emuToPixels(value: string | null | undefined): number | undefined {
  if (value == null || value === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed / 9525 : undefined;
}

function tableMeasure(element: Element | undefined, fallback = "auto"): string {
  const value = Number(wordAttribute(element, "w"));
  if (!Number.isFinite(value) || value < 0) return fallback;
  const type = wordAttribute(element, "type") || "dxa";
  if (type === "pct") return `${value / 50}%`;
  if (type === "auto" || type === "nil") return fallback;
  return `${value / 15}px`;
}

function safeHex(value: string | undefined, fallback = ""): string {
  if (!value || value === "auto") return fallback;
  return /^[0-9a-f]{6}$/i.test(value) ? `#${value.toUpperCase()}` : fallback;
}

function themeColor(element: Element | undefined, context: RenderContext): string {
  const direct = safeHex(wordAttribute(element, "val"));
  if (direct) return direct;
  const key = wordAttribute(element, "themeColor") || "";
  return context.themeColors.get(key) || "";
}

function highlightColor(value: string | undefined): string {
  const colors: Record<string, string> = {
    black: "#000000", blue: "#0000FF", cyan: "#00FFFF", darkBlue: "#000080",
    darkCyan: "#008080", darkGray: "#808080", darkGreen: "#008000", darkMagenta: "#800080",
    darkRed: "#800000", darkYellow: "#808000", green: "#00FF00", lightGray: "#C0C0C0",
    magenta: "#FF00FF", red: "#FF0000", white: "#FFFFFF", yellow: "#FFFF00",
  };
  return colors[value || ""] || "";
}

function borderCss(element: Element | undefined, context: RenderContext): string {
  if (!element) return "";
  const value = wordAttribute(element, "val") || "single";
  if (["nil", "none"].includes(value)) return "none";
  const size = Math.max(0.5, Number(wordAttribute(element, "sz") || 4) / 8);
  const style = value.includes("dash") ? "dashed" : value.includes("dot") ? "dotted" : "solid";
  return `${size}pt ${style} ${themeColor(element, context) || "#A8A29E"}`;
}

function paragraphProperties(element: Element | undefined, context: RenderContext): ParagraphProperties {
  if (!element) return { css: {} };
  const css: CssProperties = {};
  const alignment = wordAttribute(firstChild(element, "jc"), "val");
  if (alignment) css["text-align"] = alignment === "both" ? "justify" : alignment;

  const spacing = firstChild(element, "spacing");
  const before = twipsToPixels(wordAttribute(spacing, "before"));
  const after = twipsToPixels(wordAttribute(spacing, "after"));
  if (before != null) css["margin-top"] = `${before}px`;
  if (after != null) css["margin-bottom"] = `${after}px`;
  const line = Number(wordAttribute(spacing, "line"));
  const lineRule = wordAttribute(spacing, "lineRule") || "auto";
  if (Number.isFinite(line) && line > 0) {
    if (lineRule === "auto") css["line-height"] = String(line / 240);
    else if (lineRule === "exact") css["line-height"] = `${line / 15}px`;
  }

  const indent = firstChild(element, "ind");
  const left = twipsToPixels(wordAttribute(indent, "left") || wordAttribute(indent, "start"));
  const right = twipsToPixels(wordAttribute(indent, "right") || wordAttribute(indent, "end"));
  const firstLine = twipsToPixels(wordAttribute(indent, "firstLine"));
  const hanging = twipsToPixels(wordAttribute(indent, "hanging"));
  if (left != null) css["margin-left"] = `${left}px`;
  if (right != null) css["margin-right"] = `${right}px`;
  if (firstLine != null) css["text-indent"] = `${firstLine}px`;
  if (hanging != null) {
    css["text-indent"] = `${-hanging}px`;
    css["--docx-hanging"] = `${hanging}px`;
  }

  const shading = firstChild(element, "shd");
  const fill = themeColor(shading, context) || safeHex(wordAttribute(shading, "fill"));
  if (fill) css["background-color"] = fill;

  const borders = firstChild(element, "pBdr");
  for (const side of ["top", "right", "bottom", "left"] as const) {
    const border = borderCss(firstChild(borders, side), context);
    if (border) css[`border-${side}`] = border;
  }
  const numPr = firstChild(element, "numPr");
  const numId = wordAttribute(firstChild(numPr, "numId"), "val");
  const level = Number(wordAttribute(firstChild(numPr, "ilvl"), "val") || 0);
  return {
    css,
    numId,
    level: Number.isFinite(level) ? level : 0,
    pageBreakBefore: wordBoolean(firstChild(element, "pageBreakBefore")),
    keepNext: wordBoolean(firstChild(element, "keepNext")),
    keepLines: wordBoolean(firstChild(element, "keepLines")),
    widowControl: wordBoolean(firstChild(element, "widowControl")),
  };
}

function runProperties(element: Element | undefined, context: RenderContext): CssProperties {
  if (!element) return {};
  const css: CssProperties = {};
  const bold = wordBoolean(firstChild(element, "b"));
  const italic = wordBoolean(firstChild(element, "i"));
  if (bold != null) css["font-weight"] = bold ? "700" : "400";
  if (italic != null) css["font-style"] = italic ? "italic" : "normal";
  const underline = firstChild(element, "u");
  const strikeElement = firstChild(element, "strike") || firstChild(element, "dstrike");
  const strike = wordBoolean(strikeElement);
  const underlineEnabled = underline ? wordAttribute(underline, "val") !== "none" : undefined;
  const decorations = [underlineEnabled ? "underline" : "", strike ? "line-through" : ""].filter(Boolean);
  if (decorations.length) css["text-decoration"] = decorations.join(" ");
  else if (underlineEnabled === false || strike === false) css["text-decoration"] = "none";
  const color = themeColor(firstChild(element, "color"), context);
  if (color) css.color = color;
  const shading = firstChild(element, "shd");
  const background = themeColor(shading, context) || safeHex(wordAttribute(shading, "fill")) || highlightColor(wordAttribute(firstChild(element, "highlight"), "val"));
  if (background) css["background-color"] = background;
  const size = halfPointsToPixels(wordAttribute(firstChild(element, "sz"), "val"));
  if (size != null) css["font-size"] = `${size}px`;
  const fonts = firstChild(element, "rFonts");
  const themeFont = wordAttribute(fonts, "asciiTheme") || wordAttribute(fonts, "hAnsiTheme") || "";
  const font = wordAttribute(fonts, "ascii") || wordAttribute(fonts, "hAnsi") || wordAttribute(fonts, "eastAsia") || context.themeFonts.get(themeFont);
  if (font) {
    const definition = context.fontDefinitions.get(font);
    const families = [font, definition?.alternate]
      .filter((value): value is string => Boolean(value))
      .map((value) => `'${value.replace(/'/g, "\\'")}'`);
    css["font-family"] = [...families, definition?.generic || "sans-serif"].join(", ");
  }
  const spacing = twipsToPixels(wordAttribute(firstChild(element, "spacing"), "val"));
  if (spacing != null) css["letter-spacing"] = `${spacing}px`;
  const position = halfPointsToPixels(wordAttribute(firstChild(element, "position"), "val"));
  if (position != null) css["vertical-align"] = `${position}px`;
  const vertical = wordAttribute(firstChild(element, "vertAlign"), "val");
  if (vertical === "superscript") css["vertical-align"] = "super";
  if (vertical === "subscript") css["vertical-align"] = "sub";
  if (vertical === "superscript" || vertical === "subscript") css["font-size"] ||= "0.75em";
  const caps = wordBoolean(firstChild(element, "caps"));
  const smallCaps = wordBoolean(firstChild(element, "smallCaps"));
  const hidden = wordBoolean(firstChild(element, "vanish"));
  if (caps != null) css["text-transform"] = caps ? "uppercase" : "none";
  if (smallCaps != null) css["font-variant"] = smallCaps ? "small-caps" : "normal";
  if (hidden != null) css.display = hidden ? "none" : "inline";
  return css;
}

function parseTheme(xml: string | undefined): { colors: Map<string, string>; fonts: Map<string, string> } {
  const colors = new Map<string, string>();
  const fonts = new Map<string, string>();
  if (!xml) return { colors, fonts };
  const document = parseXml(xml, "theme");
  const scheme = descendants(document, "clrScheme")[0];
  for (const entry of elementChildren(scheme)) {
    const color = elementChildren(entry)[0];
    const value = color?.getAttribute("val") || color?.getAttribute("lastClr") || "";
    if (/^[0-9a-f]{6}$/i.test(value)) colors.set(entry.localName, `#${value.toUpperCase()}`);
  }
  const fontScheme = descendants(document, "fontScheme")[0];
  const major = firstChild(fontScheme, "majorFont");
  const minor = firstChild(fontScheme, "minorFont");
  const typeface = (node: Element | undefined, name: string) => firstChild(node, name)?.getAttribute("typeface") || "";
  fonts.set("majorHAnsi", typeface(major, "latin"));
  fonts.set("majorAscii", typeface(major, "latin"));
  fonts.set("majorEastAsia", typeface(major, "ea"));
  fonts.set("minorHAnsi", typeface(minor, "latin"));
  fonts.set("minorAscii", typeface(minor, "latin"));
  fonts.set("minorEastAsia", typeface(minor, "ea"));
  return { colors, fonts };
}

function parseFontDefinitions(xml: string | undefined): Map<string, FontDefinition> {
  const definitions = new Map<string, FontDefinition>();
  if (!xml) return definitions;
  const document = parseXml(xml, "font table");
  for (const font of descendants(document, "font")) {
    const name = wordAttribute(font, "name");
    if (!name) continue;
    const family = wordAttribute(firstChild(font, "family"), "val");
    definitions.set(name, {
      alternate: wordAttribute(firstChild(font, "altName"), "val"),
      generic: family === "roman" ? "serif" : family === "modern" ? "monospace" : "sans-serif",
    });
  }
  return definitions;
}

function parseStyles(xml: string | undefined, context: RenderContext): void {
  if (!xml) return;
  const document = parseXml(xml, "styles");
  const defaults = firstChild(document.documentElement, "docDefaults");
  context.defaultParagraph = paragraphProperties(firstChild(firstChild(defaults, "pPrDefault"), "pPr"), context);
  context.defaultRun = runProperties(firstChild(firstChild(defaults, "rPrDefault"), "rPr"), context);
  for (const styleElement of descendants(document, "style")) {
    const id = wordAttribute(styleElement, "styleId");
    if (!id) continue;
    context.styles.set(id, {
      id,
      type: wordAttribute(styleElement, "type") || "paragraph",
      name: wordAttribute(firstChild(styleElement, "name"), "val") || id,
      basedOn: wordAttribute(firstChild(styleElement, "basedOn"), "val"),
      paragraph: paragraphProperties(firstChild(styleElement, "pPr"), context),
      run: runProperties(firstChild(styleElement, "rPr"), context),
    });
  }
}

function resolveStyle(id: string | undefined, context: RenderContext, visiting = new Set<string>()): WordStyle | undefined {
  if (!id) return undefined;
  const cached = context.resolvedStyles.get(id);
  if (cached) return cached;
  const style = context.styles.get(id);
  if (!style || visiting.has(id)) return style;
  visiting.add(id);
  const parent = resolveStyle(style.basedOn, context, visiting);
  const resolved: WordStyle = {
    ...style,
    paragraph: mergeParagraphProperties(parent?.paragraph, style.paragraph),
    run: { ...(parent?.run || {}), ...style.run },
  };
  context.resolvedStyles.set(id, resolved);
  return resolved;
}

function parseNumbering(xml: string | undefined, context: RenderContext): void {
  if (!xml) return;
  const document = parseXml(xml, "numbering");
  const abstracts = new Map<string, Map<number, NumberingLevel>>();
  const parseLevel = (levelElement: Element): NumberingLevel => {
    const level = Number(wordAttribute(levelElement, "ilvl") || 0);
    return {
      format: wordAttribute(firstChild(levelElement, "numFmt"), "val") || "decimal",
      text: wordAttribute(firstChild(levelElement, "lvlText"), "val") || `%${level + 1}.`,
      start: Number(wordAttribute(firstChild(levelElement, "start"), "val") || 1),
      paragraph: paragraphProperties(firstChild(levelElement, "pPr"), context),
      run: runProperties(firstChild(levelElement, "rPr"), context),
    };
  };
  for (const abstract of descendants(document, "abstractNum")) {
    const id = wordAttribute(abstract, "abstractNumId");
    if (!id) continue;
    const levels = new Map<number, NumberingLevel>();
    for (const levelElement of elementChildren(abstract, "lvl")) {
      const level = Number(wordAttribute(levelElement, "ilvl") || 0);
      levels.set(level, parseLevel(levelElement));
    }
    abstracts.set(id, levels);
  }
  for (const number of descendants(document, "num")) {
    const id = wordAttribute(number, "numId");
    const abstractId = wordAttribute(firstChild(number, "abstractNumId"), "val");
    const baseLevels = abstractId ? abstracts.get(abstractId) : undefined;
    if (!id || !baseLevels) continue;
    const levels = new Map(Array.from(baseLevels, ([level, definition]) => [level, {
      ...definition,
      paragraph: mergeParagraphProperties(definition.paragraph),
      run: { ...definition.run },
    }]));
    for (const override of elementChildren(number, "lvlOverride")) {
      const level = Number(wordAttribute(override, "ilvl") || 0);
      const replacement = firstChild(override, "lvl");
      if (replacement) levels.set(level, parseLevel(replacement));
      const start = Number(wordAttribute(firstChild(override, "startOverride"), "val"));
      const current = levels.get(level);
      if (current && Number.isFinite(start)) levels.set(level, { ...current, start });
    }
    context.numbering.set(id, levels);
  }
}

function formatNumber(value: number, format: string): string {
  if (format === "bullet") return "•";
  if (format === "decimalZero") return String(value).padStart(2, "0");
  if (format === "lowerLetter" || format === "upperLetter") {
    let result = "";
    let current = Math.max(1, value);
    while (current > 0) {
      current -= 1;
      result = String.fromCharCode(97 + current % 26) + result;
      current = Math.floor(current / 26);
    }
    return format === "upperLetter" ? result.toUpperCase() : result;
  }
  if (format === "lowerRoman" || format === "upperRoman") {
    const values: Array<[number, string]> = [[1000, "m"], [900, "cm"], [500, "d"], [400, "cd"], [100, "c"], [90, "xc"], [50, "l"], [40, "xl"], [10, "x"], [9, "ix"], [5, "v"], [4, "iv"], [1, "i"]];
    let current = value;
    let result = "";
    values.forEach(([amount, token]) => { while (current >= amount) { result += token; current -= amount; } });
    return format === "upperRoman" ? result.toUpperCase() : result;
  }
  return String(value);
}

function listLabel(numId: string, level: number, context: RenderContext): { label: string; paragraph?: ParagraphProperties; run?: CssProperties } {
  const levels = context.numbering.get(numId);
  const definition = levels?.get(level);
  if (!definition) return { label: "•" };
  const counters = context.counters.get(numId) || [];
  counters[level] = (counters[level] ?? definition.start - 1) + 1;
  counters.length = level + 1;
  context.counters.set(numId, counters);
  const bullet = definition.text
    .replace(/\uF0B7/gi, "•")
    .replace(/\uF0A7/gi, "▪")
    .replace(/\uF0D8/gi, "➢");
  const label = (definition.format === "bullet" ? bullet : definition.text).replace(/%(\d+)/g, (_, raw: string) => {
    const target = Number(raw) - 1;
    const targetDefinition = levels?.get(target) || definition;
    return formatNumber(counters[target] ?? targetDefinition.start, targetDefinition.format);
  });
  return { label, paragraph: definition.paragraph, run: definition.run };
}

function parseRelationships(xml: string | undefined): Map<string, Relationship> {
  const relationships = new Map<string, Relationship>();
  if (!xml) return relationships;
  const document = parseXml(xml, "relationships");
  for (const relationship of descendants(document, "Relationship")) {
    const id = relationshipAttribute(relationship, "Id");
    const target = relationshipAttribute(relationship, "Target");
    if (id && target) relationships.set(id, { target, type: relationshipAttribute(relationship, "Type") || "" });
  }
  return relationships;
}

function normalizePartTarget(partPath: string, target: string): string {
  const base = target.startsWith("/") ? "" : partPath.slice(0, partPath.lastIndexOf("/") + 1);
  const parts = `${base}${target}`.replace(/\\/g, "/").split("/");
  const normalized: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") normalized.pop();
    else normalized.push(part);
  }
  return normalized.join("/");
}

function mediaType(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase();
  const types: Record<string, string> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", svg: "image/svg+xml", webp: "image/webp", bmp: "image/bmp", tif: "image/tiff", tiff: "image/tiff" };
  return types[extension || ""] || "application/octet-stream";
}

function bytesToDataUrl(bytes: Uint8Array, mimeType: string): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return `data:${mimeType};base64,${btoa(binary)}`;
}

function renderDrawing(element: Element, context: RenderContext): string {
  const blip = descendants(element, "blip")[0] || descendants(element, "imagedata")[0];
  const id = blip?.getAttribute("r:embed") || blip?.getAttribute("r:id") || blip?.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "embed") || "";
  const relationship = context.relationships.get(id);
  const source = relationship ? context.media.get(normalizePartTarget(context.partPath, relationship.target)) : undefined;
  if (!source) return "";
  const extent = descendants(element, "extent")[0] || descendants(element, "ext")[0];
  const width = emuToPixels(extent?.getAttribute("cx"));
  const height = emuToPixels(extent?.getAttribute("cy"));
  const style = styleText({
    width: width ? `${width}px` : "auto",
    height: height ? `${height}px` : "auto",
    "max-width": "100%",
  });
  const title = descendants(element, "docPr")[0]?.getAttribute("descr") || descendants(element, "docPr")[0]?.getAttribute("name") || "";
  return `<img src="${source}" alt="${escapeHtml(title)}" style="${style}">`;
}

function renderHorizontalRule(element: Element, context: RenderContext): string {
  const rect = descendants(element, "rect").find((entry) => entry.getAttribute("o:hr") === "t");
  const vmlStyle = rect?.getAttribute("style") || "";
  const height = /(?:^|;)\s*height\s*:\s*([^;]+)/i.exec(vmlStyle)?.[1]?.trim();
  const standardRule = rect?.getAttribute("o:hrstd") === "t";
  const stroke = firstChild(rect, "stroke");
  const color = safeHex(stroke?.getAttribute("color") || rect?.getAttribute("strokecolor") || undefined)
    || themeColor(stroke, context);
  return `<span class="manor-docx-horizontal-rule" contenteditable="false" style="${styleText({
    "border-top-width": standardRule ? "3px" : height || "1.5pt",
    "border-top-style": standardRule ? "double" : "solid",
    "border-top-color": color,
  })}"></span>`;
}

function dynamicFieldKind(instruction: string | undefined): DynamicFieldKind | undefined {
  const command = instruction?.trim().split(/\s+/)[0]?.toUpperCase();
  if (command === "PAGE") return "page";
  if (command === "NUMPAGES" || command === "SECTIONPAGES") return "num-pages";
  return undefined;
}

function renderRun(element: Element, inherited: CssProperties, context: RenderContext): string {
  const rPr = firstChild(element, "rPr");
  const characterStyle = resolveStyle(wordAttribute(firstChild(rPr, "rStyle"), "val"), context);
  const style = { ...inherited, ...(characterStyle?.run || {}), ...runProperties(rPr, context) };
  let content = "";
  for (const child of elementChildren(element)) {
    if (["rPr", "proofErr", "bookmarkStart", "bookmarkEnd"].includes(child.localName)) continue;
    if (child.localName === "fldChar") {
      const type = wordAttribute(child, "fldCharType");
      if (type === "begin") context.fieldStack.push({ instruction: "", resultVisible: false, resultRendered: false });
      else if (type === "separate" && context.fieldStack.length) {
        const field = context.fieldStack[context.fieldStack.length - 1];
        field.resultVisible = true;
        field.kind = dynamicFieldKind(field.instruction);
      }
      else if (type === "end") context.fieldStack.pop();
      continue;
    }
    const activeField = context.fieldStack.at(-1);
    if (child.localName === "instrText") {
      if (activeField) activeField.instruction += child.textContent || "";
      continue;
    }
    const fieldResultVisible = !activeField || activeField.resultVisible;
    if (!fieldResultVisible) continue;
    if (["t", "delText"].includes(child.localName)) {
      if (activeField?.kind) {
        if (!activeField.resultRendered) {
          content += `<span data-docx-field="${activeField.kind}">${escapeHtml(child.textContent || "")}</span>`;
          activeField.resultRendered = true;
        }
      } else content += escapeHtml(child.textContent || "");
    }
    else if (child.localName === "tab") content += "\t";
    else if (child.localName === "noBreakHyphen") content += "&#8209;";
    else if (child.localName === "softHyphen") content += "&shy;";
    else if (child.localName === "br" || child.localName === "cr") {
      const page = wordAttribute(child, "type") === "page";
      content += `<br${page ? ' data-docx-page-break="true"' : ""}>`;
    } else if (child.localName === "drawing" || child.localName === "pict" || child.localName === "object") {
      if (child.localName === "pict" && descendants(child, "rect").some((rect) => rect.getAttribute("o:hr") === "t")) {
        content += renderHorizontalRule(child, context);
      } else content += renderDrawing(child, context);
    } else if (child.localName === "footnoteReference") {
      const id = wordAttribute(child, "id") || "";
      content += `<sup class="manor-docx-footnote" title="${escapeHtml(context.footnotes.get(id) || "")}">${escapeHtml(id)}</sup>`;
    }
  }
  if (!content) return "";
  const styles = styleText(style);
  return styles ? `<span style="${styles}">${content}</span>` : content;
}

function renderInline(element: Element, inherited: CssProperties, context: RenderContext): string {
  if (element.localName === "r") return renderRun(element, inherited, context);
  if (element.localName === "bookmarkStart") {
    const name = wordAttribute(element, "name");
    return context.trackParagraphs && name
      ? `<span id="${escapeHtml(name)}" class="manor-docx-bookmark" contenteditable="false"></span>`
      : "";
  }
  if (element.localName === "bookmarkEnd") return "";
  if (element.localName === "hyperlink") {
    const id = element.getAttribute("r:id") || element.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id") || "";
    const relationship = context.relationships.get(id);
    const href = relationship?.target || `#${wordAttribute(element, "anchor") || ""}`;
    const safeHref = /^(?:https?:|mailto:|#)/i.test(href) ? href : "#";
    return `<a href="${escapeHtml(safeHref)}">${elementChildren(element).map((child) => renderInline(child, inherited, context)).join("")}</a>`;
  }
  if (["ins", "moveTo", "smartTag", "sdtContent", "customXml"].includes(element.localName)) {
    return elementChildren(element).map((child) => renderInline(child, inherited, context)).join("");
  }
  if (["del", "moveFrom"].includes(element.localName)) {
    return `<del>${elementChildren(element).map((child) => renderInline(child, inherited, context)).join("")}</del>`;
  }
  if (element.localName === "fldSimple") {
    const kind = dynamicFieldKind(wordAttribute(element, "instr"));
    const field = kind ? ` data-docx-field="${kind}"` : "";
    return `<span class="manor-docx-field"${field}>${elementChildren(element).map((child) => renderInline(child, inherited, context)).join("")}</span>`;
  }
  if (element.localName === "sdt") {
    return elementChildren(firstChild(element, "sdtContent") || element).map((child) => renderInline(child, inherited, context)).join("");
  }
  return "";
}

function renderParagraph(element: Element, context: RenderContext): string {
  const pPr = firstChild(element, "pPr");
  const styleId = wordAttribute(firstChild(pPr, "pStyle"), "val");
  const style = resolveStyle(styleId, context);
  let properties = mergeParagraphProperties(context.defaultParagraph, style?.paragraph, paragraphProperties(pPr, context));
  const runStyle = {
    ...context.defaultRun,
    ...(style?.run || {}),
    ...runProperties(firstChild(pPr, "rPr"), context),
  };
  let marker = "";
  if (properties.numId && properties.numId !== "0") {
    const list = listLabel(properties.numId, properties.level || 0, context);
    marker = list.label;
    properties = mergeParagraphProperties(properties, list.paragraph);
    for (const property of ["font-family", "font-size", "font-weight", "font-style", "color"] as const) {
      if (list.run?.[property]) properties.css[`--docx-marker-${property}`] = list.run[property];
    }
  }
  const index = context.paragraphIndex++;
  const content = elementChildren(element)
    .filter((child) => child.localName !== "pPr")
    .map((child) => renderInline(child, runStyle, context))
    .join("");
  const markerIsBullet = marker === "•";
  const sourceEditable = context.revisionBlockDepth === 0
    && !["ins", "del", "moveFrom", "moveTo"].some((name) => descendants(element, name).length > 0);
  const classes = [
    "manor-docx-paragraph",
    marker ? "manor-docx-list-paragraph" : "",
    markerIsBullet ? "manor-docx-list-bullet" : "",
  ].filter(Boolean).join(" ");
  const attributes = [
    `class="${classes}"`,
    context.trackParagraphs ? `data-docx-paragraph-index="${index}" data-docx-source-editable="${sourceEditable ? "true" : "false"}"` : 'data-manor-docx-layout="true" contenteditable="false"',
    context.trackParagraphs && !sourceEditable ? 'contenteditable="false"' : "",
    marker ? `data-docx-list-label="${markerIsBullet ? "bullet" : escapeHtml(marker)}"` : "",
    properties.pageBreakBefore ? 'data-docx-page-break-before="true"' : "",
    properties.keepNext ? 'data-docx-keep-next="true"' : "",
    properties.keepLines ? 'data-docx-keep-lines="true"' : "",
    properties.widowControl === false ? 'data-docx-widow-control="false"' : "",
    style?.name ? `data-docx-style-name="${escapeHtml(style.name)}"` : "",
    `style="${styleText(properties.css)}"`,
  ].filter(Boolean).join(" ");
  return `<p ${attributes}>${content || '<br data-docx-placeholder="true">'}</p>`;
}

interface RenderedCell {
  html: string;
  colspan: number;
  rowspan: number;
  skip?: boolean;
  column: number;
}

function renderTable(element: Element, context: RenderContext): string {
  const tblPr = firstChild(element, "tblPr");
  const grid = firstChild(element, "tblGrid");
  const gridWidths = elementChildren(grid, "gridCol").map((column) => twipsToPixels(wordAttribute(column, "w")) || 0);
  const tableWidth = tableMeasure(firstChild(tblPr, "tblW"), "100%");
  const tableCss: CssProperties = {
    "border-collapse": "collapse",
    "table-layout": wordAttribute(firstChild(tblPr, "tblLayout"), "type") === "autofit" ? "auto" : "fixed",
    width: tableWidth,
  };
  const tableAlignment = wordAttribute(firstChild(tblPr, "jc"), "val");
  if (tableAlignment === "center") tableCss.margin = "0 auto";
  else if (tableAlignment === "right" || tableAlignment === "end") tableCss["margin-left"] = "auto";
  const tableIndent = twipsToPixels(wordAttribute(firstChild(tblPr, "tblInd"), "w"));
  if (tableIndent != null && !["center", "right", "end"].includes(tableAlignment || "")) tableCss["margin-left"] = `${tableIndent}px`;
  const tableBorders = firstChild(tblPr, "tblBorders");
  const tableCellMargins = firstChild(tblPr, "tblCellMar");
  const defaultPadding = ["top", "right", "bottom", "left"].map((side) => twipsToPixels(wordAttribute(firstChild(tableCellMargins, side), "w")) ?? 7.5);
  const activeMerges = new Map<number, RenderedCell>();
  const rows: Array<{ cells: RenderedCell[]; attributes: string }> = [];
  const rowElements = elementChildren(element, "tr");
  for (const [rowIndex, rowElement] of rowElements.entries()) {
    const cells: RenderedCell[] = [];
    const rowProperties = firstChild(rowElement, "trPr");
    const rowHeight = firstChild(rowProperties, "trHeight");
    const rowHeightPx = twipsToPixels(wordAttribute(rowHeight, "val"));
    const rowCss: CssProperties = {};
    if (rowHeightPx != null) {
      rowCss[wordAttribute(rowHeight, "hRule") === "exact" ? "height" : "min-height"] = `${rowHeightPx}px`;
    }
    let column = 0;
    for (const cellElement of elementChildren(rowElement, "tc")) {
      const tcPr = firstChild(cellElement, "tcPr");
      const colspan = Math.max(1, Number(wordAttribute(firstChild(tcPr, "gridSpan"), "val") || 1));
      const vMerge = firstChild(tcPr, "vMerge");
      const mergeValue = wordAttribute(vMerge, "val");
      if (vMerge && mergeValue !== "restart" && activeMerges.has(column)) {
        activeMerges.get(column)!.rowspan += 1;
        context.paragraphIndex += descendants(cellElement, "p").length;
        column += colspan;
        continue;
      }
      const gridWidth = gridWidths.slice(column, column + colspan).reduce((sum, value) => sum + value, 0);
      const width = tableMeasure(firstChild(tcPr, "tcW"), gridWidth ? `${gridWidth}px` : "auto");
      const shading = firstChild(tcPr, "shd");
      const fill = themeColor(shading, context) || safeHex(wordAttribute(shading, "fill"));
      const vertical = wordAttribute(firstChild(tcPr, "vAlign"), "val");
      const borders = firstChild(tcPr, "tcBorders");
      const cellMargins = firstChild(tcPr, "tcMar");
      const padding = ["top", "right", "bottom", "left"].map((side, index) => (
        twipsToPixels(wordAttribute(firstChild(cellMargins, side), "w")) ?? defaultPadding[index]
      ));
      const css: CssProperties = {
        width,
        padding: padding.map((value) => `${value}px`).join(" "),
        "vertical-align": vertical === "center" ? "middle" : vertical === "bottom" ? "bottom" : "top",
        "background-color": fill,
      };
      const textDirection = wordAttribute(firstChild(tcPr, "textDirection"), "val");
      if (textDirection === "tbRl" || textDirection === "tbRlV") css["writing-mode"] = "vertical-rl";
      if (textDirection === "btLr") css["writing-mode"] = "vertical-lr";
      if (firstChild(tcPr, "noWrap")) css["white-space"] = "nowrap";
      for (const side of ["top", "right", "bottom", "left"] as const) {
        const tableBorder = side === "top"
          ? firstChild(tableBorders, rowIndex === 0 ? "top" : "insideH")
          : side === "bottom"
            ? firstChild(tableBorders, rowIndex === rowElements.length - 1 ? "bottom" : "insideH")
            : side === "left"
              ? firstChild(tableBorders, column === 0 ? "left" : "insideV")
              : firstChild(tableBorders, column + colspan >= gridWidths.length ? "right" : "insideV");
        const border = borderCss(firstChild(borders, side), context) || borderCss(tableBorder, context);
        if (border) css[`border-${side}`] = border;
      }
      const cellContent = elementChildren(cellElement)
        .filter((child) => child.localName !== "tcPr")
        .map((child) => renderBlock(child, context))
        .join("");
      const cell: RenderedCell = { html: `<td style="${styleText(css)}">${cellContent || "<p><br></p>"}</td>`, colspan, rowspan: 1, column };
      cells.push(cell);
      if (vMerge && mergeValue === "restart") activeMerges.set(column, cell);
      else for (let offset = 0; offset < colspan; offset += 1) activeMerges.delete(column + offset);
      column += colspan;
    }
    rows.push({
      cells,
      attributes: `${firstChild(rowProperties, "tblHeader") ? ' data-docx-table-header="true"' : ""}${firstChild(rowProperties, "cantSplit") ? ' data-docx-table-row-keep="true"' : ""}${Object.keys(rowCss).length ? ` style="${styleText(rowCss)}"` : ""}`,
    });
  }
  const body = rows.map(({ cells, attributes }) => `<tr${attributes}>${cells.map((cell) => cell.html.replace("<td ", `<td${cell.colspan > 1 ? ` colspan="${cell.colspan}"` : ""}${cell.rowspan > 1 ? ` rowspan="${cell.rowspan}"` : ""} `)).join("")}</tr>`).join("");
  const columns = gridWidths.length
    ? `<colgroup>${gridWidths.map((width) => `<col style="width:${width}px">`).join("")}</colgroup>`
    : "";
  return `<table class="manor-docx-table" style="${styleText(tableCss)}">${columns}<tbody>${body}</tbody></table>`;
}

function renderBlock(element: Element, context: RenderContext): string {
  if (element.localName === "p") return renderParagraph(element, context);
  if (element.localName === "tbl") return renderTable(element, context);
  if (["del", "moveFrom"].includes(element.localName)) {
    context.revisionBlockDepth += 1;
    let content: string;
    try {
      content = elementChildren(element).map((child) => renderBlock(child, context)).join("");
    } finally {
      context.revisionBlockDepth -= 1;
    }
    return `<del data-docx-revision="deleted" contenteditable="false" style="display:block">${content}</del>`;
  }
  if (["ins", "moveTo"].includes(element.localName)) {
    context.revisionBlockDepth += 1;
    try {
      return elementChildren(element).map((child) => renderBlock(child, context)).join("");
    } finally {
      context.revisionBlockDepth -= 1;
    }
  }
  if (["sdt", "customXml", "smartTag"].includes(element.localName)) {
    const content = element.localName === "sdt" ? firstChild(element, "sdtContent") || element : element;
    return elementChildren(content).map((child) => renderBlock(child, context)).join("");
  }
  if (element.localName === "altChunk") {
    const id = element.getAttribute("r:id")
      || element.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id")
      || "";
    return `<p class="manor-docx-unsupported" data-docx-alt-chunk-id="${escapeHtml(id)}" contenteditable="false">Embedded document content</p>`;
  }
  return "";
}

function sectionLayout(document: XMLDocument): { layout: ManorDocumentLayout; section: Element | undefined } {
  const sections = descendants(document, "sectPr");
  const section = sections.at(-1);
  const size = firstChild(section, "pgSz");
  const margins = firstChild(section, "pgMar");
  const width = twipsToPixels(wordAttribute(size, "w")) || 816;
  const height = twipsToPixels(wordAttribute(size, "h")) || 1056;
  const landscape = wordAttribute(size, "orient") === "landscape";
  return {
    section,
    layout: {
      pageWidthPx: landscape ? Math.max(width, height) : width,
      pageHeightPx: landscape ? Math.min(width, height) : height,
      marginTopPx: twipsToPixels(wordAttribute(margins, "top")) || 96,
      marginRightPx: twipsToPixels(wordAttribute(margins, "right")) || 96,
      marginBottomPx: twipsToPixels(wordAttribute(margins, "bottom")) || 96,
      marginLeftPx: twipsToPixels(wordAttribute(margins, "left")) || 96,
      headerDistancePx: twipsToPixels(wordAttribute(margins, "header")) || 48,
      footerDistancePx: twipsToPixels(wordAttribute(margins, "footer")) || 48,
    },
  };
}

async function renderRelatedPart(
  zip: JSZipArchive,
  reference: Element | undefined,
  context: RenderContext,
): Promise<string> {
  const id = reference?.getAttribute("r:id") || reference?.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id") || "";
  const relationship = context.relationships.get(id);
  if (!relationship) return "";
  const part = normalizePartTarget(context.partPath, relationship.target);
  const xml = await zip.file(part)?.async("text");
  if (!xml) return "";
  const document = parseXml(xml, part);
  const slash = part.lastIndexOf("/");
  const relationshipsPart = `${part.slice(0, slash)}/_rels/${part.slice(slash + 1)}.rels`;
  const partRelationshipsXml = await zip.file(relationshipsPart)?.async("text");
  const relatedContext: RenderContext = {
    ...context,
    partPath: part,
    paragraphIndex: 0,
    revisionBlockDepth: 0,
    trackParagraphs: false,
    counters: new Map(),
    relationships: parseRelationships(partRelationshipsXml),
    fieldStack: [],
  };
  return elementChildren(document.documentElement).map((child) => renderBlock(child, relatedContext)).join("");
}

function parseFootnotes(xml: string | undefined): Map<string, string> {
  const footnotes = new Map<string, string>();
  if (!xml) return footnotes;
  const document = parseXml(xml, "footnotes");
  for (const footnote of descendants(document, "footnote")) {
    const id = wordAttribute(footnote, "id");
    const text = descendants(footnote, "t").map((node) => node.textContent || "").join("").trim();
    if (id && text) footnotes.set(id, text);
  }
  return footnotes;
}

export async function renderManorDocument(buffer: ArrayBuffer): Promise<ManorDocumentRender> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(buffer);
  const documentXml = await zip.file("word/document.xml")?.async("text");
  if (!documentXml) throw new Error("The DOCX package has no word/document.xml part.");
  const [stylesXml, numberingXml, themeXml, relationshipsXml, footnotesXml, settingsXml, fontTableXml] = await Promise.all([
    zip.file("word/styles.xml")?.async("text"),
    zip.file("word/numbering.xml")?.async("text"),
    zip.file("word/theme/theme1.xml")?.async("text"),
    zip.file("word/_rels/document.xml.rels")?.async("text"),
    zip.file("word/footnotes.xml")?.async("text"),
    zip.file("word/settings.xml")?.async("text"),
    zip.file("word/fontTable.xml")?.async("text"),
  ]);
  const theme = parseTheme(themeXml);
  const relationships = parseRelationships(relationshipsXml);
  const media = new Map<string, string>();
  await Promise.all(Object.keys(zip.files).filter((path) => path.startsWith("word/media/") && !zip.files[path].dir).map(async (path) => {
    const bytes = await zip.file(path)?.async("uint8array");
    if (bytes) media.set(path, bytesToDataUrl(bytes, mediaType(path)));
  }));
  const context: RenderContext = {
    partPath: "word/document.xml",
    paragraphIndex: 0,
    revisionBlockDepth: 0,
    styles: new Map(),
    resolvedStyles: new Map(),
    defaultParagraph: { css: {} },
    defaultRun: {},
    numbering: new Map(),
    counters: new Map(),
    relationships,
    media,
    themeColors: theme.colors,
    themeFonts: theme.fonts,
    fontDefinitions: parseFontDefinitions(fontTableXml),
    footnotes: parseFootnotes(footnotesXml),
    fieldStack: [],
    trackParagraphs: true,
  };
  parseStyles(stylesXml, context);
  parseNumbering(numberingXml, context);
  const document = parseXml(documentXml, "document");
  const body = descendants(document, "body")[0];
  if (!body) throw new Error("The DOCX package has no document body.");
  const { layout, section } = sectionLayout(document);
  const html = elementChildren(body).filter((child) => child.localName !== "sectPr").map((child) => renderBlock(child, context)).join("");
  const reference = (name: "headerReference" | "footerReference", type: string) => (
    elementChildren(section, name).find((entry) => wordAttribute(entry, "type") === type)
  );
  const defaultHeaderReference = reference("headerReference", "default") || firstChild(section, "headerReference");
  const defaultFooterReference = reference("footerReference", "default") || firstChild(section, "footerReference");
  const settings = settingsXml ? parseXml(settingsXml, "settings") : undefined;
  const differentFirstPage = wordBoolean(firstChild(section, "titlePg")) === true;
  const evenAndOddHeaders = settings ? descendants(settings, "evenAndOddHeaders")[0] : undefined;
  const differentEvenPages = wordBoolean(evenAndOddHeaders) === true;
  const [headerHtml, footerHtml, firstHeaderHtml, firstFooterHtml, evenHeaderHtml, evenFooterHtml] = await Promise.all([
    renderRelatedPart(zip, defaultHeaderReference, context),
    renderRelatedPart(zip, defaultFooterReference, context),
    renderRelatedPart(zip, reference("headerReference", "first"), context),
    renderRelatedPart(zip, reference("footerReference", "first"), context),
    renderRelatedPart(zip, reference("headerReference", "even"), context),
    renderRelatedPart(zip, reference("footerReference", "even"), context),
  ]);
  const fonts = Array.from(new Set([
    context.defaultRun["font-family"],
    ...Array.from(context.styles.values()).map((style) => style.run["font-family"]),
  ].filter((value): value is string => Boolean(value)).map((value) => value.split(",")[0].replace(/^['"]|['"]$/g, ""))));
  return {
    html,
    headerHtml,
    footerHtml,
    firstHeaderHtml,
    firstFooterHtml,
    evenHeaderHtml,
    evenFooterHtml,
    differentFirstPage,
    differentEvenPages,
    layout,
    fonts,
  };
}

const PARAGRAPH_FRAGMENT_ATTRIBUTE = "data-manor-docx-paragraph-fragment";
const PARAGRAPH_FRAGMENT_STYLE_ATTRIBUTE = "data-manor-docx-paragraph-style";
const NO_INLINE_STYLE = "__manor_none__";

function restoreParagraphFragments(blocks: HTMLElement[]): HTMLElement[] {
  const firstFragment = new Map<string, HTMLElement>();
  const restored = blocks.filter((block) => {
    const id = block.getAttribute(PARAGRAPH_FRAGMENT_ATTRIBUTE);
    if (!id) return true;
    const first = firstFragment.get(id);
    if (!first) {
      firstFragment.set(id, block);
      return true;
    }
    if (block.getAttribute("data-docx-keep-next") === "true") first.setAttribute("data-docx-keep-next", "true");
    while (block.firstChild) first.appendChild(block.firstChild);
    block.remove();
    return false;
  });
  firstFragment.forEach((block) => {
    const originalStyle = block.getAttribute(PARAGRAPH_FRAGMENT_STYLE_ATTRIBUTE);
    if (originalStyle === NO_INLINE_STYLE) block.removeAttribute("style");
    else if (originalStyle != null) block.setAttribute("style", originalStyle);
    block.removeAttribute(PARAGRAPH_FRAGMENT_ATTRIBUTE);
    block.removeAttribute(PARAGRAPH_FRAGMENT_STYLE_ATTRIBUTE);
  });
  return restored;
}

function directPageContent(root: HTMLElement): HTMLElement[] {
  const pageBlocks = Array.from(root.children).flatMap((child) => {
    if (!(child instanceof HTMLElement)) return [];
    if (!child.matches("[data-manor-docx-page]")) return [child];
    const content = child.querySelector<HTMLElement>(":scope > .manor-docx-page__content");
    return content ? Array.from(content.children).filter((item): item is HTMLElement => item instanceof HTMLElement) : [];
  });
  const blocks = restoreParagraphFragments(pageBlocks);
  const firstFragment = new Map<string, HTMLTableElement>();
  return blocks.filter((block) => {
    if (!(block instanceof HTMLTableElement)) return true;
    const id = block.dataset.manorDocxTableFragment;
    if (!id) return true;
    const first = firstFragment.get(id);
    if (!first) {
      firstFragment.set(id, block);
      block.removeAttribute("data-manor-docx-table-fragment");
      return true;
    }
    block.querySelectorAll('tr[data-manor-docx-layout="true"]').forEach((row) => row.remove());
    const firstBody = first.tBodies[0] || first.createTBody();
    Array.from(block.tBodies).flatMap((body) => Array.from(body.rows)).forEach((row) => firstBody.appendChild(row));
    return false;
  });
}

function cloneElementRange(block: HTMLElement, range: Range): HTMLElement {
  const clone = block.cloneNode(false) as HTMLElement;
  clone.appendChild(range.cloneContents());
  return clone;
}

function markParagraphSplit(source: HTMLElement, head: HTMLElement, tail: HTMLElement, id: string): void {
  const originalStyle = source.getAttribute(PARAGRAPH_FRAGMENT_STYLE_ATTRIBUTE)
    ?? source.getAttribute("style")
    ?? NO_INLINE_STYLE;
  for (const fragment of [head, tail]) {
    fragment.setAttribute(PARAGRAPH_FRAGMENT_ATTRIBUTE, id);
    fragment.setAttribute(PARAGRAPH_FRAGMENT_STYLE_ATTRIBUTE, originalStyle);
  }
  head.style.marginBottom = "0px";
  head.removeAttribute("data-docx-keep-next");
  tail.style.marginTop = "0px";
  tail.style.textIndent = "0px";
  tail.removeAttribute("data-docx-page-break-before");
  tail.removeAttribute("data-docx-list-label");
  tail.classList.remove("manor-docx-list-paragraph", "manor-docx-list-bullet");
}

function splitAtBoundary(block: HTMLElement, node: Node, offset: number, id: string): [HTMLElement, HTMLElement] {
  const headRange = document.createRange();
  headRange.selectNodeContents(block);
  headRange.setEnd(node, offset);
  const tailRange = document.createRange();
  tailRange.selectNodeContents(block);
  tailRange.setStart(node, offset);
  const head = cloneElementRange(block, headRange);
  const tail = cloneElementRange(block, tailRange);
  markParagraphSplit(block, head, tail, id);
  return [head, tail];
}

function textPosition(block: HTMLElement, requestedOffset: number): { node: Node; offset: number } | null {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  let remaining = requestedOffset;
  let node = walker.nextNode();
  while (node) {
    const length = node.textContent?.length || 0;
    if (remaining <= length) return { node, offset: remaining };
    remaining -= length;
    node = walker.nextNode();
  }
  return null;
}

function splitParagraphAtText(block: HTMLElement, offset: number, id: string): [HTMLElement, HTMLElement] | null {
  const position = textPosition(block, offset);
  return position ? splitAtBoundary(block, position.node, position.offset, id) : null;
}

function splitParagraphAtPageBreaks(block: HTMLElement, id: string): HTMLElement[] {
  const fragments: HTMLElement[] = [];
  let remainder = block;
  let marker = remainder.querySelector<HTMLElement>('br[data-docx-page-break="true"]');
  while (marker) {
    const range = document.createRange();
    range.selectNodeContents(remainder);
    range.setEndAfter(marker);
    const head = cloneElementRange(remainder, range);
    const tailRange = document.createRange();
    tailRange.selectNodeContents(remainder);
    tailRange.setStartAfter(marker);
    const tail = cloneElementRange(remainder, tailRange);
    markParagraphSplit(remainder, head, tail, id);
    fragments.push(head);
    remainder = tail;
    marker = remainder.querySelector<HTMLElement>('br[data-docx-page-break="true"]');
  }
  if (remainder.textContent?.length || remainder.querySelector("img,br,.manor-docx-horizontal-rule")) fragments.push(remainder);
  else if (remainder.getAttribute("data-docx-keep-next") === "true") fragments.at(-1)?.setAttribute("data-docx-keep-next", "true");
  return fragments.length ? fragments : [block];
}

function pageOverflows(pageContent: HTMLElement): boolean {
  return pageContent.scrollHeight > pageContent.clientHeight + 2;
}

function renderedLineCount(block: HTMLElement): number {
  const range = document.createRange();
  range.selectNodeContents(block);
  const lineTops: number[] = [];
  for (const rect of Array.from(range.getClientRects())) {
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (!lineTops.some((top) => Math.abs(top - rect.top) < 1)) lineTops.push(rect.top);
  }
  return lineTops.length;
}

function measureParagraphSplit(
  block: HTMLElement,
  split: [HTMLElement, HTMLElement],
  pageContent: HTMLElement,
): { fits: boolean; headLines: number; tailLines: number } {
  const [head, tail] = split;
  block.replaceWith(head);
  const fits = !pageOverflows(pageContent);
  const headLines = renderedLineCount(head);
  head.replaceWith(block);
  block.replaceWith(tail);
  const tailLines = renderedLineCount(tail);
  tail.replaceWith(block);
  return { fits, headLines, tailLines };
}

function splitParagraphToFit(
  block: HTMLElement,
  pageContent: HTMLElement,
  fragmentId: string,
): [HTMLElement, HTMLElement] | null {
  const text = block.textContent || "";
  if (text.length < 2) return null;
  const preserveWidows = block.getAttribute("data-docx-widow-control") !== "false";
  let low = 1;
  let high = text.length - 1;
  let best = 0;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const split = splitParagraphAtText(block, middle, fragmentId);
    if (!split) break;
    const { fits } = measureParagraphSplit(block, split, pageContent);
    if (fits) {
      best = middle;
      low = middle + 1;
    } else high = middle - 1;
  }
  if (!best) return null;
  const wordOffsets = Array.from(text.matchAll(/[\s-]+/g), (match) => (match.index || 0) + match[0].length)
    .filter((offset) => offset > 0 && offset <= best)
    .reverse();
  const candidates = wordOffsets.length ? wordOffsets : Array.from({ length: best }, (_, index) => best - index);
  if (!candidates.includes(best)) candidates.unshift(best);
  for (const offset of candidates) {
    const result = splitParagraphAtText(block, offset, fragmentId);
    if (!result || !result[0].textContent?.length || !result[1].textContent?.length) continue;
    const measurement = measureParagraphSplit(block, result, pageContent);
    if (!measurement.fits) continue;
    if (preserveWidows && (measurement.headLines < 2 || measurement.tailLines < 2)) continue;
    return result;
  }
  return null;
}

function selectionOffset(root: HTMLElement, node: Node | null, offset: number): number | null {
  if (!node || !root.contains(node)) return null;
  const range = document.createRange();
  range.selectNodeContents(root);
  try { range.setEnd(node, offset); } catch { return null; }
  const fragment = range.cloneContents();
  fragment.querySelectorAll("[data-manor-docx-layout]").forEach((element) => element.remove());
  return fragment.textContent?.length || 0;
}

function restoreSelection(root: HTMLElement, start: number | null, end: number | null): void {
  if (start == null || end == null) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => (node.parentElement?.closest("[data-manor-docx-layout]") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const range = document.createRange();
  let cursor = 0;
  let startSet = false;
  let node = walker.nextNode();
  while (node) {
    const length = node.textContent?.length || 0;
    if (!startSet && start <= cursor + length) {
      range.setStart(node, Math.max(0, start - cursor));
      startSet = true;
    }
    if (startSet && end <= cursor + length) {
      range.setEnd(node, Math.max(0, end - cursor));
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      return;
    }
    cursor += length;
    node = walker.nextNode();
  }
  if (startSet) {
    range.setEnd(root, root.childNodes.length);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }
}

function pageFurniture(render: ManorDocumentRender, pageNumber: number): { header: string; footer: string } {
  if (pageNumber === 1 && render.differentFirstPage) {
    return {
      header: render.firstHeaderHtml,
      footer: render.firstFooterHtml,
    };
  }
  if (pageNumber % 2 === 0 && render.differentEvenPages) {
    return {
      header: render.evenHeaderHtml || render.headerHtml,
      footer: render.evenFooterHtml || render.footerHtml,
    };
  }
  return { header: render.headerHtml, footer: render.footerHtml };
}

function applyPageFurniture(page: HTMLElement, render: ManorDocumentRender, pageNumber: number, pageCount?: number): void {
  const furniture = pageFurniture(render, pageNumber);
  page.setAttribute("aria-label", `Document page ${pageNumber}`);
  const header = page.querySelector<HTMLElement>(":scope > .manor-docx-page__header");
  const footer = page.querySelector<HTMLElement>(":scope > .manor-docx-page__footer");
  if (header) header.innerHTML = furniture.header;
  if (footer) footer.innerHTML = furniture.footer;
  page.querySelectorAll<HTMLElement>('[data-docx-field="page"]').forEach((field) => {
    field.textContent = String(pageNumber);
  });
  if (pageCount != null) {
    page.querySelectorAll<HTMLElement>('[data-docx-field="num-pages"]').forEach((field) => {
      field.textContent = String(pageCount);
    });
  }
}

function createPage(root: HTMLElement, render: ManorDocumentRender, pageNumber: number): HTMLElement {
  const page = document.createElement("article");
  page.className = "manor-docx-page";
  page.setAttribute("data-manor-docx-page", "true");
  const header = document.createElement("header");
  header.className = "manor-docx-page__header";
  header.setAttribute("data-manor-docx-layout", "true");
  header.contentEditable = "false";
  const content = document.createElement("div");
  content.className = "manor-docx-page__content";
  const footer = document.createElement("footer");
  footer.className = "manor-docx-page__footer";
  footer.setAttribute("data-manor-docx-layout", "true");
  footer.contentEditable = "false";
  page.append(header, content, footer);
  applyPageFurniture(page, render, pageNumber);
  root.appendChild(page);
  return content;
}

function createTableFragment(
  source: HTMLTableElement,
  id: string,
  repeatedHeaders: HTMLTableRowElement[] = [],
): HTMLTableElement {
  const fragment = source.cloneNode(false) as HTMLTableElement;
  fragment.dataset.manorDocxTableFragment = id;
  Array.from(source.children).forEach((child) => {
    if (child.tagName !== "TBODY") fragment.appendChild(child.cloneNode(true));
  });
  const body = document.createElement("tbody");
  repeatedHeaders.forEach((row) => {
    const repeated = row.cloneNode(true) as HTMLTableRowElement;
    repeated.setAttribute("data-manor-docx-layout", "true");
    repeated.contentEditable = "false";
    repeated.querySelectorAll("[data-docx-paragraph-index]").forEach((paragraph) => {
      paragraph.removeAttribute("data-docx-paragraph-index");
      paragraph.removeAttribute("data-docx-source-editable");
    });
    body.appendChild(repeated);
  });
  fragment.appendChild(body);
  return fragment;
}

export function paginateManorDocument(root: HTMLElement, render: ManorDocumentRender): number {
  const selection = window.getSelection();
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  const start = range ? selectionOffset(root, range.startContainer, range.startOffset) : null;
  const end = range ? selectionOffset(root, range.endContainer, range.endOffset) : null;
  const blocks = directPageContent(root);
  root.replaceChildren();
  let pages = 0;
  let pageContent!: HTMLElement;
  const nextPage = () => {
    pages += 1;
    pageContent = createPage(root, render, pages);
    return pageContent;
  };
  nextPage();
  let tableIndex = 0;
  let paragraphFragmentIndex = 0;
  const placeParagraph = (block: HTMLElement, fragmentId: string): void => {
    pageContent.appendChild(block);
    if (!pageOverflows(pageContent)) return;

    const previous = block.previousElementSibling as HTMLElement | null;
    let movedKeepChain = false;
    if (previous?.getAttribute("data-docx-keep-next") === "true") {
      const keepChain: HTMLElement[] = [previous];
      let cursor = previous.previousElementSibling as HTMLElement | null;
      while (cursor?.getAttribute("data-docx-keep-next") === "true") {
        keepChain.unshift(cursor);
        cursor = cursor.previousElementSibling as HTMLElement | null;
      }
      block.remove();
      nextPage();
      keepChain.forEach((item) => pageContent.appendChild(item));
      pageContent.appendChild(block);
      if (!pageOverflows(pageContent)) return;
      movedKeepChain = true;
    }

    const hasEarlierContent = block.previousElementSibling != null;
    const keepLines = block.getAttribute("data-docx-keep-lines") === "true";
    if (block.matches("p") && (!keepLines || !hasEarlierContent || movedKeepChain)) {
      const split = splitParagraphToFit(block, pageContent, fragmentId);
      if (split) {
        const [head, tail] = split;
        block.replaceWith(head);
        nextPage();
        placeParagraph(tail, fragmentId);
        return;
      }
    }

    if (hasEarlierContent) {
      block.remove();
      nextPage();
      placeParagraph(block, fragmentId);
    }
  };
  for (const block of blocks) {
    const breakBefore = block.getAttribute("data-docx-page-break-before") === "true";
    if (breakBefore && pageContent.childElementCount > 0) {
      nextPage();
    }
    if (block instanceof HTMLTableElement && block.rows.length > 0) {
      const rows = Array.from(block.tBodies).flatMap((body) => Array.from(body.rows));
      const headerRows = rows.filter((row) => row.getAttribute("data-docx-table-header") === "true");
      const fragmentId = `table-${tableIndex++}`;
      let fragment = createTableFragment(block, fragmentId);
      pageContent.appendChild(fragment);
      for (const row of rows) {
        fragment.tBodies[0].appendChild(row);
        const overflowed = pageOverflows(pageContent);
        const hasEarlierContent = fragment.previousElementSibling != null;
        const headerRow = row.getAttribute("data-docx-table-header") === "true";
        if (overflowed && (fragment.rows.length > 1 || hasEarlierContent)) {
          if (headerRow && hasEarlierContent) {
            fragment.remove();
            nextPage();
            pageContent.appendChild(fragment);
            continue;
          }
          row.remove();
          if (fragment.rows.length === 0) fragment.remove();
          nextPage();
          fragment = createTableFragment(block, fragmentId, headerRows);
          pageContent.appendChild(fragment);
          fragment.tBodies[0].appendChild(row);
        }
      }
      continue;
    }
    const fragmentId = `paragraph-${paragraphFragmentIndex++}`;
    for (const fragment of splitParagraphAtPageBreaks(block, fragmentId)) {
      placeParagraph(fragment, fragmentId);
      if (fragment.querySelector('[data-docx-page-break="true"]')) nextPage();
    }
  }
  Array.from(root.querySelectorAll<HTMLElement>(":scope > [data-manor-docx-page]")).forEach((page) => {
    if (root.childElementCount > 1 && page.querySelector(".manor-docx-page__content")?.childElementCount === 0) page.remove();
  });
  const renderedPages = Array.from(root.querySelectorAll<HTMLElement>(":scope > [data-manor-docx-page]"));
  renderedPages.forEach((page, index) => applyPageFurniture(page, render, index + 1, renderedPages.length));
  pages = renderedPages.length;
  root.dataset.manorDocxPageCount = String(pages);
  restoreSelection(root, start, end);
  return pages;
}

export function serializeManorDocumentHtml(root: HTMLElement): string {
  const pages = Array.from(root.querySelectorAll<HTMLElement>(":scope > [data-manor-docx-page]"));
  if (!pages.length) return root.innerHTML;
  const container = document.createElement("div");
  pages.forEach((page) => {
    const content = page.querySelector<HTMLElement>(":scope > .manor-docx-page__content");
    if (!content) return;
    Array.from(content.childNodes).forEach((child) => container.appendChild(child.cloneNode(true)));
  });
  restoreParagraphFragments(Array.from(container.children).filter((child): child is HTMLElement => child instanceof HTMLElement));
  const firstFragment = new Map<string, HTMLTableElement>();
  container.querySelectorAll('tr[data-manor-docx-layout="true"]').forEach((row) => row.remove());
  Array.from(container.querySelectorAll<HTMLTableElement>("table[data-manor-docx-table-fragment]")).forEach((table) => {
    const id = table.dataset.manorDocxTableFragment;
    if (!id) return;
    const first = firstFragment.get(id);
    if (!first) {
      firstFragment.set(id, table);
      table.removeAttribute("data-manor-docx-table-fragment");
      return;
    }
    const firstBody = first.tBodies[0] || first.createTBody();
    Array.from(table.tBodies).flatMap((body) => Array.from(body.rows)).forEach((row) => firstBody.appendChild(row));
    table.remove();
  });
  return container.innerHTML;
}
