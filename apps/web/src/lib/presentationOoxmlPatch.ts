import {
  presentationTextEditSpans,
  presentationTextEditSpansFromSourceMap,
  presentationTextEditSpansForSource,
  type PresentationTextSourceMap,
} from "./presentationTextEdits";
import {
  presentationObjectGroups,
  type PresentationGroupTransform,
} from "./presentationOoxml";

const PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

type PresentationSourceKind = "sp" | "pic" | "cxnSp" | "graphicFrame";

interface PresentationShapeSource {
  part: string;
  kind: PresentationSourceKind;
  objectId: string;
  cloneOfObjectId?: string;
  editable: boolean;
  mediaPart?: string;
  groupTransform?: PresentationGroupTransform;
  groupPath?: string[];
}

interface PresentationText {
  text: string;
  sourceMap?: PresentationTextSourceMap;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number;
  color?: string;
  align?: string;
  fontFamily?: string;
  bullet?: string;
  indent?: number;
  lineSpacing?: number;
  spaceBefore?: number;
  spaceAfter?: number;
  baseline?: number;
  spacing?: number;
  runs?: Array<{
    text: string;
    bold?: boolean;
    italic?: boolean;
    underline?: boolean;
    strikethrough?: boolean;
    fontSize?: number;
    color?: string;
    fontFamily?: string;
    baseline?: number;
    spacing?: number;
  }>;
}

interface PresentationTableCell {
  text: string;
  sourceMap?: PresentationTextSourceMap;
  bold?: boolean;
  color?: string;
  fill?: string;
  gridSpan?: number;
  vMerge?: boolean;
}

export interface PreservePresentationShape {
  id: string;
  type?: string;
  x: number;
  y: number;
  w: number;
  h: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
  fill?: string;
  gradFill?: { angle: number; stops: Array<{ pos: number; color: string; alpha: number }> };
  opacity?: number;
  stroke?: string;
  strokeWidth?: number;
  presetGeom?: string;
  borderRadius?: number;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  vAlign?: "top" | "middle" | "bottom";
  padding?: { l: number; t: number; r: number; b: number };
  imgCrop?: { l: number; t: number; r: number; b: number };
  texts: PresentationText[];
  tableRows?: PresentationTableCell[][];
  tableColWidths?: number[];
  imgUrl?: string;
  hyperlink?: string;
  imageFit?: "cover" | "contain" | "fill";
  source?: PresentationShapeSource;
}

export interface PreservePresentationSlide {
  id: string;
  bg?: string;
  bgGrad?: { angle: number; stops: Array<{ pos: number; color: string; alpha: number }> };
  bgImgUrl?: string;
  aspectRatio?: string;
  notes?: string;
  shapes: PreservePresentationShape[];
  sourcePart?: string;
  notesPart?: string;
}

export interface PreservePresentationResult {
  file: File;
  slides: PreservePresentationSlide[];
}

export class PresentationPreservationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PresentationPreservationError";
  }
}

function escapeXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]),
  );
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(stableValue(left)) === JSON.stringify(stableValue(right));
}

function slideUnsupportedProperties(slide: PreservePresentationSlide): Record<string, unknown> {
  const unsupported = { ...slide } as Record<string, unknown>;
  for (const key of ["id", "bg", "bgGrad", "bgImgUrl", "notes", "shapes", "sourcePart", "notesPart"]) delete unsupported[key];
  return unsupported;
}

function setXmlAttribute(openTag: string, name: string, value: string | undefined): string {
  const attribute = new RegExp(`\\s${name}\\s*=\\s*(?:"[^"]*"|'[^']*')`, "i");
  if (value == null) return openTag.replace(attribute, "");
  if (attribute.test(openTag)) return openTag.replace(attribute, ` ${name}="${value}"`);
  return openTag.replace(/\s*\/?\s*>$/, (ending) => ` ${name}="${value}"${ending}`);
}

function newPresentationShapeCreationId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return `{${globalThis.crypto.randomUUID().toUpperCase()}}`;
  }
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0").toUpperCase());
  return `{${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}}`;
}

const PRESENTATION_SHAPE_CREATION_NAMESPACE = "http://schemas.microsoft.com/office/drawing/2014/main";

function refreshClonedShapeCreationIds(element: string, namespaceContext = element): string {
  const namespaces = new Map<string, string>();
  for (const match of `${namespaceContext}\n${element}`.matchAll(
    /\bxmlns(?::([A-Za-z_][\w.-]*))?\s*=\s*(["'])(.*?)\2/gi,
  )) {
    namespaces.set(match[1] || "", match[3]);
  }
  return element.replace(
    /<(?:(?:([A-Za-z_][\w.-]*)):)?creationId\b[^>]*>/gi,
    (openTag, prefix: string | undefined) => (
      namespaces.get(prefix || "") === PRESENTATION_SHAPE_CREATION_NAMESPACE
        ? setXmlAttribute(openTag, "id", newPresentationShapeCreationId())
        : openTag
    ),
  );
}

function presentationColorValue(value: string | undefined, fallback = "000000"): { hex: string; alpha: number } {
  const source = (value || "").trim();
  const hex = source.replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(hex)) {
    return { hex: hex.split("").map((character) => character + character).join("").toUpperCase(), alpha: 1 };
  }
  if (/^[0-9a-f]{6}$/i.test(hex)) return { hex: hex.toUpperCase(), alpha: 1 };
  const rgb = source.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)$/i);
  if (rgb) {
    const channels = rgb.slice(1, 4).map((channel) => Math.max(0, Math.min(255, Number(channel))));
    return {
      hex: channels.map((channel) => Math.round(channel).toString(16).padStart(2, "0")).join("").toUpperCase(),
      alpha: Math.max(0, Math.min(1, rgb[4] == null ? 1 : Number(rgb[4]))),
    };
  }
  return { hex: fallback, alpha: source === "transparent" ? 0 : 1 };
}

function presentationAlpha(value: number | undefined): number {
  return Math.round(Math.max(0, Math.min(1, value ?? 1)) * 100_000);
}

function presentationColorXml(color: string | undefined, alpha = 1): string {
  const parsed = presentationColorValue(color);
  const combinedAlpha = parsed.alpha * Math.max(0, Math.min(1, alpha));
  return `<a:srgbClr val="${parsed.hex}">${combinedAlpha < 1 ? `<a:alpha val="${presentationAlpha(combinedAlpha)}"/>` : ""}</a:srgbClr>`;
}

function presentationGeometryXml(shape: PreservePresentationShape): string {
  const adjustment = shape.presetGeom === "roundRect" && shape.borderRadius != null
    ? `<a:gd name="adj" fmla="val ${Math.round(Math.max(0, Math.min(50, shape.borderRadius)) * 1000)}"/>`
    : "";
  return `<a:prstGeom prst="${escapeXml(shape.presetGeom || "rect")}"><a:avLst>${adjustment}</a:avLst></a:prstGeom>`;
}

function presentationFillXml(shape: PreservePresentationShape): string {
  if (shape.gradFill?.stops?.length) {
    const stops = shape.gradFill.stops
      .map((stop) => `<a:gs pos="${Math.round(Math.max(0, Math.min(100, stop.pos)) * 1000)}">${presentationColorXml(stop.color, stop.alpha)}</a:gs>`)
      .join("");
    const angle = Math.round((((shape.gradFill.angle || 0) % 360) + 360) % 360 * 60_000);
    return `<a:gradFill rotWithShape="1"><a:gsLst>${stops}</a:gsLst><a:lin ang="${angle}" scaled="1"/></a:gradFill>`;
  }
  if (!shape.fill || shape.fill === "transparent" || shape.fill === "none") return "<a:noFill/>";
  return `<a:solidFill>${presentationColorXml(shape.fill)}</a:solidFill>`;
}

function presentationLineXml(shape: PreservePresentationShape): string {
  if (!shape.stroke || (shape.strokeWidth ?? 0) <= 0) return "<a:ln><a:noFill/></a:ln>";
  return `<a:ln w="${Math.max(1, Math.round((shape.strokeWidth || 1) * 12_700))}"><a:solidFill>${presentationColorXml(shape.stroke)}</a:solidFill><a:prstDash val="solid"/></a:ln>`;
}

function patchDirectShapeProperty(
  shapeProperties: string,
  pattern: RegExp,
  replacement: string,
): string {
  if (pattern.test(shapeProperties)) return shapeProperties.replace(pattern, replacement);
  return shapeProperties.replace(/<\/p:spPr>\s*$/i, `${replacement}</p:spPr>`);
}

function patchDirectShapeFill(shapeProperties: string, replacement: string): string {
  const fillPattern = /<a:(?:noFill|solidFill|gradFill|pattFill)\b[^>]*(?:\/>|>[\s\S]*?<\/a:(?:solidFill|gradFill|pattFill)>)/ig;
  const lineIndex = shapeProperties.search(/<a:ln\b/i);
  for (const match of shapeProperties.matchAll(fillPattern)) {
    if (lineIndex < 0 || match.index! < lineIndex) {
      return `${shapeProperties.slice(0, match.index)}${replacement}${shapeProperties.slice(match.index! + match[0].length)}`;
    }
  }
  if (lineIndex >= 0) return `${shapeProperties.slice(0, lineIndex)}${replacement}${shapeProperties.slice(lineIndex)}`;
  return shapeProperties.replace(/<\/p:spPr>\s*$/i, `${replacement}</p:spPr>`);
}

function patchShapeFormatting(element: string, shape: PreservePresentationShape): string {
  const shapeProperties = element.match(/<p:spPr\b[^>]*(?:\/>|>[\s\S]*?<\/p:spPr>)/i)?.[0];
  if (!shapeProperties) return element;
  let patched = shapeProperties;
  patched = patchDirectShapeFill(patched, presentationFillXml(shape));
  patched = patchDirectShapeProperty(
    patched,
    /<a:ln\b[^>]*(?:\/>|>[\s\S]*?<\/a:ln>)/i,
    presentationLineXml(shape),
  );
  const geometry = presentationGeometryXml(shape);
  patched = patchDirectShapeProperty(
    patched,
    /<a:prstGeom\b[^>]*(?:\/>|>[\s\S]*?<\/a:prstGeom>)/i,
    geometry,
  );
  if (shape.shadow) {
    const shadow = `<a:effectLst><a:outerShdw blurRad="${Math.round(Math.max(0, shape.shadow.blur) * 12_700)}" dist="${Math.round(Math.max(0, shape.shadow.dist) * 12_700)}" dir="${Math.round((((shape.shadow.angle || 0) % 360) + 360) % 360 * 60_000)}">${presentationColorXml(shape.shadow.color, shape.shadow.alpha)}</a:outerShdw></a:effectLst>`;
    patched = patchDirectShapeProperty(
      patched,
      /<a:effectLst\b[^>]*(?:\/>|>[\s\S]*?<\/a:effectLst>)/i,
      shadow,
    );
  } else {
    patched = patched.replace(/<a:effectLst\b[^>]*(?:\/>|>[\s\S]*?<\/a:effectLst>)/i, "");
  }
  let output = element.replace(shapeProperties, patched);
  output = output.replace(/<a:bodyPr\b[^>]*\/?\s*>/i, (bodyPr) => {
    const anchor = shape.vAlign === "middle" ? "ctr" : shape.vAlign === "bottom" ? "b" : "t";
    let next = setXmlAttribute(bodyPr, "anchor", anchor);
    const padding = shape.padding;
    if (padding) {
      next = setXmlAttribute(next, "lIns", String(Math.round(padding.l * 12_700)));
      next = setXmlAttribute(next, "tIns", String(Math.round(padding.t * 12_700)));
      next = setXmlAttribute(next, "rIns", String(Math.round(padding.r * 12_700)));
      next = setXmlAttribute(next, "bIns", String(Math.round(padding.b * 12_700)));
    }
    return next;
  });
  return output;
}

function presentationRunProperties(text: PresentationText, run: NonNullable<PresentationText["runs"]>[number]): string {
  const style = { ...text, ...run };
  const attributes = [
    'lang="en-US"',
    style.fontSize ? `sz="${Math.max(100, Math.round(style.fontSize * 100))}"` : "",
    style.bold ? 'b="1"' : "",
    style.italic ? 'i="1"' : "",
    style.underline ? 'u="sng"' : "",
    style.strikethrough ? 'strike="sngStrike"' : "",
    style.baseline ? `baseline="${Math.round(style.baseline * 1000)}"` : "",
    style.spacing ? `spc="${Math.round(style.spacing * 100)}"` : "",
  ].filter(Boolean).join(" ");
  const children = [
    style.color ? `<a:solidFill>${presentationColorXml(style.color)}</a:solidFill>` : "",
    style.fontFamily ? `<a:latin typeface="${escapeXml(style.fontFamily)}"/>` : "",
  ].join("");
  return `<a:rPr ${attributes}>${children}</a:rPr>`;
}

function presentationParagraphXml(text: PresentationText): string {
  const alignment = text.align === "center" ? "ctr" : text.align === "right" ? "r" : text.align === "justify" ? "just" : "l";
  const paragraphAttributes = [
    `algn="${alignment}"`,
    text.indent != null ? `marL="${Math.round(Math.max(0, text.indent) * 12_700)}"` : "",
  ].filter(Boolean).join(" ");
  const bullet = text.bullet
    ? text.bullet === "#." || text.bullet === "a." || text.bullet === "i."
      ? `<a:buAutoNum type="${text.bullet === "a." ? "alphaLcPeriod" : text.bullet === "i." ? "romanLcPeriod" : "arabicPeriod"}"/>`
      : `<a:buChar char="${escapeXml(text.bullet)}"/>`
    : "<a:buNone/>";
  const spacing = [
    text.lineSpacing != null ? `<a:lnSpc><a:spcPct val="${Math.round(Math.max(0.1, text.lineSpacing) * 100_000)}"/></a:lnSpc>` : "",
    text.spaceBefore != null ? `<a:spcBef><a:spcPts val="${Math.round(Math.max(0, text.spaceBefore) * 100)}"/></a:spcBef>` : "",
    text.spaceAfter != null ? `<a:spcAft><a:spcPts val="${Math.round(Math.max(0, text.spaceAfter) * 100)}"/></a:spcAft>` : "",
  ].join("");
  const runs = text.runs?.length ? text.runs : [{ text: text.text }];
  const runXml = runs.map((run) => run.text.replace(/\r\n?/g, "\n").split("\n").map((segment, index) => (
    `${index > 0 ? "<a:br/>" : ""}<a:r>${presentationRunProperties(text, run)}<a:t${/^\s|\s$/.test(segment) ? ' xml:space="preserve"' : ""}>${escapeXml(segment)}</a:t></a:r>`
  )).join("")).join("");
  return `<a:p><a:pPr ${paragraphAttributes}>${spacing}${bullet}</a:pPr>${runXml}<a:endParaRPr lang="en-US"/></a:p>`;
}

function patchTransform(element: string, shape: PreservePresentationShape, slideWidth: number, slideHeight: number): string {
  const x = Math.round((shape.x / 100) * slideWidth);
  const y = Math.round((shape.y / 100) * slideHeight);
  const cx = Math.max(1, Math.round((shape.w / 100) * slideWidth));
  const cy = Math.max(1, Math.round((shape.h / 100) * slideHeight));
  const rotation = shape.rotation ? String(Math.round(shape.rotation * 60000)) : undefined;

  const patchXfrm = (xfrm: string, prefix: string, coordinatePrefix = prefix) => {
    let next = xfrm.replace(new RegExp(`<${prefix}:xfrm\\b[^>]*>`, "i"), (openTag) => {
      let patched = setXmlAttribute(openTag, "rot", rotation);
      patched = setXmlAttribute(patched, "flipH", shape.flipH ? "1" : undefined);
      return setXmlAttribute(patched, "flipV", shape.flipV ? "1" : undefined);
    });
    next = next.replace(new RegExp(`<${coordinatePrefix}:off\\b[^>]*/>`, "i"), `<${coordinatePrefix}:off x="${x}" y="${y}"/>`);
    next = next.replace(new RegExp(`<${coordinatePrefix}:ext\\b[^>]*/>`, "i"), `<${coordinatePrefix}:ext cx="${cx}" cy="${cy}"/>`);
    return next;
  };

  const aXfrm = element.match(/<a:xfrm\b[^>]*>[\s\S]*?<\/a:xfrm>/i)?.[0];
  if (aXfrm) return element.replace(aXfrm, patchXfrm(aXfrm, "a"));
  const pXfrm = element.match(/<p:xfrm\b[^>]*>[\s\S]*?<\/p:xfrm>/i)?.[0];
  if (pXfrm) return element.replace(pXfrm, patchXfrm(pXfrm, "p", /<a:off\b/i.test(pXfrm) ? "a" : "p"));

  const attributes = `${rotation ? ` rot="${rotation}"` : ""}${shape.flipH ? ' flipH="1"' : ""}${shape.flipV ? ' flipV="1"' : ""}`;
  const xfrm = `<a:xfrm${attributes}><a:off x="${x}" y="${y}"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm>`;
  if (/<p:spPr\b[^>]*\/>/i.test(element)) {
    return element.replace(/<p:spPr\b([^>]*)\/>/i, `<p:spPr$1>${xfrm}</p:spPr>`);
  }
  if (/<p:spPr\b[^>]*>/i.test(element)) {
    return element.replace(/<p:spPr\b[^>]*>/i, (openTag) => `${openTag}${xfrm}`);
  }
  throw new PresentationPreservationError("This object has no editable OOXML transform.");
}

function patchImageCrop(element: string, crop: PreservePresentationShape["imgCrop"]): string {
  const existing = /<a:srcRect\b[^>]*\/?\s*>/i;
  if (!crop || !(crop.l || crop.t || crop.r || crop.b)) {
    return element.replace(existing, "");
  }
  const attributes = (["l", "t", "r", "b"] as const)
    .map((key) => `${key}="${Math.round(Math.max(0, Math.min(95, crop[key])) * 1000)}"`)
    .join(" ");
  const sourceRect = `<a:srcRect ${attributes}/>`;
  if (existing.test(element)) return element.replace(existing, sourceRect);
  const blip = element.match(/<a:blip\b[^>]*\/>|<a:blip\b[^>]*>[\s\S]*?<\/a:blip>/i)?.[0];
  if (!blip) throw new PresentationPreservationError("This image has no editable OOXML blip.");
  return element.replace(blip, `${blip}${sourceRect}`);
}

function patchImageOpacity(element: string, opacity: number | undefined): string {
  const alpha = Math.round(Math.max(0, Math.min(1, opacity ?? 1)) * 100_000);
  const blip = element.match(/<a:blip\b[^>]*(?:\/>|>[\s\S]*?<\/a:blip>)/i)?.[0];
  if (!blip) return element;
  let patched = blip.replace(/<a:alphaModFix\b[^>]*\/>/i, "");
  if (alpha < 100_000) {
    if (/\/>\s*$/i.test(patched)) patched = patched.replace(/\/>\s*$/i, `><a:alphaModFix amt="${alpha}"/></a:blip>`);
    else patched = patched.replace(/<\/a:blip>\s*$/i, `<a:alphaModFix amt="${alpha}"/></a:blip>`);
  }
  return element.replace(blip, patched);
}

function decodePresentationText(value: string): string {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_match, hex: string) => String.fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_match, decimal: string) => String.fromCodePoint(Number.parseInt(decimal, 10)))
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function presentationTextXml(templateTag: string, value: string): string {
  return value.split(/(\n)/).map((token) => {
    if (token === "\n") return "<a:br/>";
    let opening = templateTag.match(/^<a:t(?:\s[^>]*)?>/)?.[0] || "<a:t>";
    if (/^\s|\s$/.test(token) && !/xml:space=/i.test(opening)) {
      opening = opening.replace(/>$/, ' xml:space="preserve">');
    }
    return `${opening}${escapeXml(token)}</a:t>`;
  }).join("");
}

interface PresentationTextToken {
  end: number;
  kind: "break" | "field" | "run" | "text";
  raw: string;
  start: number;
  text: string;
}

function presentationTextLength(value: string): number {
  return Array.from(value).length;
}

function presentationTextSlice(value: string, start: number, end?: number): string {
  return Array.from(value).slice(start, end).join("");
}

function presentationTextTokens(paragraphXml: string): PresentationTextToken[] {
  const pattern = /<a:fld[\s>][\s\S]*?<\/a:fld>|<a:r[\s>][\s\S]*?<\/a:r>|<a:br\b[^>]*(?:\/>|>[\s\S]*?<\/a:br>)|<a:t(?:\s[^>]*)?>[\s\S]*?<\/a:t>/g;
  let textOffset = 0;
  return Array.from(paragraphXml.matchAll(pattern), (match) => {
    const raw = match[0];
    const kind = /^<a:fld(?:\s|>)/.test(raw)
      ? "field"
      : /^<a:r(?:\s|>)/.test(raw)
        ? "run"
        : /^<a:br(?:\s|\/|>)/.test(raw)
          ? "break"
          : "text";
    const text = kind === "break"
      ? "\n"
      : Array.from(raw.matchAll(/<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g), (textMatch) => (
          decodePresentationText(textMatch[1] || "")
        )).join("");
    const token: PresentationTextToken = {
      raw,
      text,
      kind,
      start: textOffset,
      end: textOffset + presentationTextLength(text),
    };
    textOffset = token.end;
    return token;
  });
}

function presentationRunTemplate(token: PresentationTextToken): string {
  if (token.kind === "run") return token.raw;
  const runProperties = token.raw.match(/<a:rPr\b[^>]*(?:\/>|>[\s\S]*?<\/a:rPr>)/i)?.[0]
    || '<a:rPr lang="en-US"/>';
  return `<a:r>${runProperties}<a:t></a:t></a:r>`;
}

function presentationRunFragments(template: string, value: string): string {
  return value.split("\n").map((segment, index) => {
    const lineBreak = index > 0 ? "<a:br/>" : "";
    if (!segment) return lineBreak;
    const textTag = template.match(/<a:t(?:\s[^>]*)?>[\s\S]*?<\/a:t>/i)?.[0];
    if (!textTag) {
      return `${lineBreak}<a:r><a:rPr lang="en-US"/>${presentationTextXml("<a:t>", segment)}</a:r>`;
    }
    return `${lineBreak}${template.replace(textTag, presentationTextXml(textTag, segment))}`;
  }).join("");
}

function presentationTextTokenReplacement(
  token: PresentationTextToken,
  value: string,
  styleTemplate: string,
): string {
  if (token.kind === "text") return presentationTextXml(token.raw, value);
  const template = token.kind === "run" ? token.raw : styleTemplate;
  return presentationRunFragments(template, value);
}

function presentationEditedRunTemplate(
  edited: PresentationText | undefined,
  position: number,
  fallback: PresentationTextToken,
): string {
  if (!edited?.runs?.length) return presentationRunTemplate(fallback);
  let offset = 0;
  let selected = edited.runs[edited.runs.length - 1];
  for (const run of edited.runs) {
    const end = offset + presentationTextLength(run.text);
    if (position < end) {
      selected = run;
      break;
    }
    offset = end;
  }
  return `<a:r>${presentationRunProperties(edited, selected)}<a:t></a:t></a:r>`;
}

function patchPresentationParagraphTextSpan(
  paragraphXml: string,
  originalStart: number,
  originalEnd: number,
  inserted: string,
  edited: PresentationText | undefined,
  editedStart: number,
): string {
  const tokens = presentationTextTokens(paragraphXml);
  if (!tokens.length) {
    if (!inserted) return paragraphXml;
    const run = presentationRunFragments('<a:r><a:rPr lang="en-US"/><a:t></a:t></a:r>', inserted);
    return /<a:endParaRPr\b/i.test(paragraphXml)
      ? paragraphXml.replace(/<a:endParaRPr\b/i, `${run}<a:endParaRPr`)
      : paragraphXml.replace(/<\/a:p>\s*$/i, `${run}</a:p>`);
  }

  const insertionOnly = originalStart === originalEnd;
  const anchor = insertionOnly
    ? Math.max(0, tokens.findIndex((token) => originalStart <= token.end))
    : Math.max(0, tokens.findIndex((token) => token.end > originalStart && token.start < originalEnd));
  const styleToken = tokens.slice(0, anchor).reverse().find((token) => token.kind === "run" || token.kind === "field")
    || tokens.slice(anchor).find((token) => token.kind === "run" || token.kind === "field")
    || tokens[anchor];
  const styleTemplate = presentationEditedRunTemplate(edited, editedStart, styleToken);
  let tokenIndex = 0;
  return paragraphXml.replace(
    /<a:fld[\s>][\s\S]*?<\/a:fld>|<a:r[\s>][\s\S]*?<\/a:r>|<a:br\b[^>]*(?:\/>|>[\s\S]*?<\/a:br>)|<a:t(?:\s[^>]*)?>[\s\S]*?<\/a:t>/g,
    (raw) => {
      const index = tokenIndex++;
      const token = tokens[index];
      if (insertionOnly) {
        if (index !== anchor) return raw;
        const localOffset = Math.max(0, Math.min(presentationTextLength(token.text), originalStart - token.start));
        if (token.kind === "field" || token.kind === "break") {
          const insertedXml = presentationRunFragments(styleTemplate, inserted);
          if (localOffset === 0) return `${insertedXml}${raw}`;
          if (localOffset === presentationTextLength(token.text)) return `${raw}${insertedXml}`;
        }
        return presentationTextTokenReplacement(
          token,
          `${presentationTextSlice(token.text, 0, localOffset)}${inserted}${presentationTextSlice(token.text, localOffset)}`,
          styleTemplate,
        );
      }
      if (token.end <= originalStart || token.start >= originalEnd) return raw;
      const before = token.start < originalStart
        ? presentationTextSlice(token.text, 0, originalStart - token.start)
        : "";
      const after = token.end > originalEnd
        ? presentationTextSlice(token.text, originalEnd - token.start)
        : "";
      const nextText = `${before}${index === anchor ? inserted : ""}${after}`;
      return presentationTextTokenReplacement(token, nextText, styleTemplate);
    },
  );
}

function patchPresentationParagraphText(
  paragraphXml: string,
  value: string,
  edited?: PresentationText,
): string {
  const original = presentationTextTokens(paragraphXml).map((token) => token.text).join("");
  if (original === value) return paragraphXml;
  const editedCharacters = Array.from(value);
  return presentationTextEditSpansForSource(original, value, edited?.sourceMap).reverse().reduce((patched, span) => (
    patchPresentationParagraphTextSpan(
      patched,
      span.originalStart,
      span.originalEnd,
      editedCharacters.slice(span.editedStart, span.editedEnd).join(""),
      edited,
      span.editedStart,
    )
  ), paragraphXml);
}

function presentationTextFormatting(text: PresentationText): unknown {
  const { text: _text, runs: _runs, sourceMap: _sourceMap, ...paragraphFormatting } = text;
  return paragraphFormatting;
}

function presentationRunsFollowTextEdit(baseline: PresentationText, edited: PresentationText): boolean {
  if (!baseline.runs?.length && !edited.runs?.length) return true;
  if (!baseline.runs?.length || (!edited.runs?.length && edited.text.length > 0)) return false;
  if (!edited.runs?.length) return edited.text.length === 0;
  const baselineCharacters = baseline.runs.flatMap((run) => {
    const { text, ...formatting } = run;
    return Array.from(text, (character) => ({ character, formatting }));
  });
  const editedCharacters = edited.runs.flatMap((run) => {
    const { text, ...formatting } = run;
    return Array.from(text, (character) => ({ character, formatting }));
  });
  const baselineText = baselineCharacters.map(({ character }) => character).join("");
  const editedText = editedCharacters.map(({ character }) => character).join("");
  if (baselineText !== baseline.text || editedText !== edited.text) return false;

  let baselineCursor = 0;
  let editedCursor = 0;
  for (const span of presentationTextEditSpansForSource(baseline.text, edited.text, edited.sourceMap)) {
    const unchangedLength = span.originalStart - baselineCursor;
    for (let offset = 0; offset < unchangedLength; offset += 1) {
      if (!sameValue(
        editedCharacters[editedCursor + offset]?.formatting,
        baselineCharacters[baselineCursor + offset]?.formatting,
      )) return false;
    }
    const insertedFormatting = baselineCharacters[Math.max(0, span.originalStart - 1)]?.formatting
      || baselineCharacters[span.originalStart]?.formatting
      || {};
    for (let index = span.editedStart; index < span.editedEnd; index += 1) {
      if (!sameValue(editedCharacters[index]?.formatting, insertedFormatting)) return false;
    }
    baselineCursor = span.originalEnd;
    editedCursor = span.editedEnd;
  }
  for (let offset = 0; baselineCursor + offset < baselineCharacters.length; offset += 1) {
    if (!sameValue(
      editedCharacters[editedCursor + offset]?.formatting,
      baselineCharacters[baselineCursor + offset]?.formatting,
    )) return false;
  }
  return true;
}

function patchTextParagraphs(
  element: string,
  baselineTexts: PresentationText[],
  texts: PresentationText[],
): string {
  const textBody = element.match(/<p:txBody\b[^>]*>[\s\S]*?<\/p:txBody>/i)?.[0];
  if (!textBody) {
    if (texts.length === 0) return element;
    if (/<\/p:sp>\s*$/i.test(element)) {
      const paragraphs = texts.map(presentationParagraphXml).join("");
      return element.replace(/<\/p:sp>\s*$/i, `<p:txBody><a:bodyPr/><a:lstStyle/>${paragraphs}</p:txBody></p:sp>`);
    }
    throw new PresentationPreservationError("This object cannot contain editable text.");
  }
  const openTag = textBody.match(/^<p:txBody\b[^>]*>/i)?.[0] || "<p:txBody>";
  const bodyPr = textBody.match(/<a:bodyPr\b[^>]*(?:\/>|>[\s\S]*?<\/a:bodyPr>)/i)?.[0] || "<a:bodyPr/>";
  const listStyle = textBody.match(/<a:lstStyle\b[^>]*(?:\/>|>[\s\S]*?<\/a:lstStyle>)/i)?.[0] || "<a:lstStyle/>";
  const sourceParagraphs = textBody.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [];
  const paragraphs = sourceParagraphs.length === baselineTexts.length && texts.length === baselineTexts.length
    ? sourceParagraphs.map((paragraph, index) => {
        const baseline = baselineTexts[index];
        const edited = texts[index];
        if (sameValue(baseline, edited)) return paragraph;
        if (
          sameValue(presentationTextFormatting(baseline), presentationTextFormatting(edited))
          && presentationRunsFollowTextEdit(baseline, edited)
        ) {
          return patchPresentationParagraphText(paragraph, edited.text, edited);
        }
        return presentationParagraphXml(edited);
      }).join("")
    : (texts.length ? texts : [{ text: "" }]).map(presentationParagraphXml).join("");
  return element.replace(textBody, `${openTag}${bodyPr}${listStyle}${paragraphs}</p:txBody>`);
}

interface PresentationParagraphRecord {
  text: string;
  xml: string;
}

function presentationParagraphPosition(
  paragraphs: PresentationParagraphRecord[],
  position: number,
): { index: number; offset: number } {
  let paragraphOffset = 0;
  for (let index = 0; index < paragraphs.length; index += 1) {
    const paragraphEnd = paragraphOffset + presentationTextLength(paragraphs[index].text);
    if (position >= paragraphOffset && position <= paragraphEnd) {
      return { index, offset: position - paragraphOffset };
    }
    paragraphOffset = paragraphEnd + 1;
  }
  throw new PresentationPreservationError("Unable to map the edited table text to its source paragraphs.");
}

function presentationParagraphXmlParts(paragraphXml: string): {
  content: string;
  endProperties: string;
  open: string;
  properties: string;
} {
  const open = paragraphXml.match(/^<a:p\b[^>]*>/i)?.[0] || "<a:p>";
  const properties = paragraphXml.match(/<a:pPr\b[^>]*(?:\/>|>[\s\S]*?<\/a:pPr>)/i)?.[0] || "";
  const contentStart = properties ? paragraphXml.indexOf(properties) + properties.length : open.length;
  const endProperties = paragraphXml.match(/<a:endParaRPr\b[^>]*(?:\/>|>[\s\S]*?<\/a:endParaRPr>)/i)?.[0] || "";
  const contentEnd = endProperties ? paragraphXml.indexOf(endProperties) : paragraphXml.lastIndexOf("</a:p>");
  return { content: paragraphXml.slice(contentStart, contentEnd), endProperties, open, properties };
}

function mergePresentationParagraphXml(firstXml: string, lastXml: string): string {
  const first = presentationParagraphXmlParts(firstXml);
  const last = presentationParagraphXmlParts(lastXml);
  return `${first.open}${first.properties}${first.content}${last.content}${last.endProperties || first.endProperties}</a:p>`;
}

function patchTableCellTextBody(
  textBody: string,
  baselineText: string,
  editedText: string,
  sourceMap?: PresentationTextSourceMap,
): string {
  if (baselineText === editedText) return textBody;
  const paragraphMatches = Array.from(textBody.matchAll(/<a:p[\s>][\s\S]*?<\/a:p>/g));
  const paragraphs = paragraphMatches.map((match) => ({
    xml: match[0],
    text: presentationTextTokens(match[0]).map((token) => token.text).join(""),
  }));
  const sourceText = paragraphs.map((paragraph) => paragraph.text).join("\n");
  if (sourceText !== baselineText) {
    throw new PresentationPreservationError("This table cell no longer matches its editable OOXML text.");
  }

  if (!paragraphs.length) {
    const paragraph = presentationParagraphXml({ text: editedText });
    return textBody.replace(/<\/a:txBody>\s*$/i, `${paragraph}</a:txBody>`);
  }

  const editedCharacters = Array.from(editedText);
  const sourceMappedEditSpans = presentationTextEditSpansFromSourceMap(sourceText, editedText, sourceMap);
  let editSpans = sourceMappedEditSpans || presentationTextEditSpans(sourceText, editedText);
  const preservedCharacters = presentationTextLength(sourceText)
    - editSpans.reduce((count, span) => count + span.originalEnd - span.originalStart, 0);
  const removedParagraphBoundary = (sourceText.match(/\n/g) || []).length > (editedText.match(/\n/g) || []).length;
  if (
    !sourceMappedEditSpans
    && removedParagraphBoundary
    && preservedCharacters * 2 < Math.min(presentationTextLength(sourceText), presentationTextLength(editedText))
  ) {
    editSpans = [{
      originalStart: 0,
      originalEnd: presentationTextLength(sourceText),
      editedStart: 0,
      editedEnd: presentationTextLength(editedText),
    }];
  }
  for (const span of editSpans.reverse()) {
    const start = presentationParagraphPosition(paragraphs, span.originalStart);
    const end = presentationParagraphPosition(paragraphs, span.originalEnd);

    const inserted = editedCharacters.slice(span.editedStart, span.editedEnd).join("");
    if (start.index === end.index) {
      const paragraph = paragraphs[start.index];
      const nextText = `${presentationTextSlice(paragraph.text, 0, start.offset)}${inserted}${presentationTextSlice(paragraph.text, end.offset)}`;
      paragraph.xml = patchPresentationParagraphTextSpan(
        paragraph.xml,
        start.offset,
        end.offset,
        inserted,
        undefined,
        span.editedStart,
      );
      paragraph.text = nextText;
      continue;
    }

    const first = paragraphs[start.index];
    const last = paragraphs[end.index];
    const prefix = presentationTextSlice(first.text, 0, start.offset);
    const suffix = presentationTextSlice(last.text, end.offset);
    const insertedParagraphs = inserted.split("\n");
    const firstText = `${prefix}${insertedParagraphs[0]}`;
    const lastText = insertedParagraphs.length === 1
      ? suffix
      : `${insertedParagraphs[insertedParagraphs.length - 1]}${suffix}`;
    const firstXml = patchPresentationParagraphTextSpan(
      first.xml,
      start.offset,
      presentationTextLength(first.text),
      insertedParagraphs[0],
      undefined,
      span.editedStart,
    );
    const lastXml = patchPresentationParagraphTextSpan(
      last.xml,
      0,
      end.offset,
      insertedParagraphs.length === 1 ? "" : insertedParagraphs[insertedParagraphs.length - 1],
      undefined,
      0,
    );
    if (insertedParagraphs.length === 1) {
      paragraphs.splice(start.index, end.index - start.index + 1, {
        xml: mergePresentationParagraphXml(firstXml, lastXml),
        text: `${firstText}${lastText}`,
      });
      continue;
    }

    const replacement = [
      { xml: firstXml, text: firstText },
      ...insertedParagraphs.slice(1, -1).map((text) => ({ xml: presentationParagraphXml({ text }), text })),
      { xml: lastXml, text: lastText },
    ];
    paragraphs.splice(start.index, end.index - start.index + 1, ...replacement);
  }

  const firstParagraphStart = paragraphMatches[0].index;
  const lastParagraph = paragraphMatches[paragraphMatches.length - 1];
  const lastParagraphEnd = lastParagraph.index + lastParagraph[0].length;
  return `${textBody.slice(0, firstParagraphStart)}${paragraphs.map((paragraph) => paragraph.xml).join("")}${textBody.slice(lastParagraphEnd)}`;
}

function patchTableCells(
  element: string,
  baselineRows: PresentationTableCell[][],
  rows: PresentationTableCell[][],
): string {
  const baselineCells = baselineRows.flat();
  const cells = rows.flat();
  const xmlCells = element.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/g) || [];
  if (xmlCells.length !== cells.length || baselineCells.length !== cells.length) {
    throw new PresentationPreservationError("Changing this presentation table structure is not supported yet.");
  }
  let cellIndex = 0;
  return element.replace(/<a:tc[\s>][\s\S]*?<\/a:tc>/g, (cellXml) => {
    const index = cellIndex++;
    const cell = cells[index];
    if (sameValue(baselineCells[index], cell)) return cellXml;
    const textBody = cellXml.match(/<a:txBody\b[^>]*>[\s\S]*?<\/a:txBody>/i)?.[0];
    if (!textBody) throw new PresentationPreservationError("This table cell has no editable OOXML text body.");
    const baselineCell = baselineCells[index];
    let patched = cellXml.replace(
      textBody,
      patchTableCellTextBody(textBody, baselineCell.text, cell.text, cell.sourceMap),
    );
    if (baselineCell.fill !== cell.fill) {
      const tcPr = patched.match(/<a:tcPr\b[^>]*(?:\/>|>[\s\S]*?<\/a:tcPr>)/i)?.[0];
      if (tcPr) {
        const fill = cell.fill ? `<a:solidFill>${presentationColorXml(cell.fill)}</a:solidFill>` : "<a:noFill/>";
        patched = patched.replace(tcPr, patchDirectTableCellFill(tcPr, fill));
      }
    }
    return patched;
  });
}

function patchDirectTableCellFill(tableCellProperties: string, replacement: string): string {
  const pattern = /<a:(?:noFill|solidFill|gradFill|pattFill)\b[^>]*(?:\/>|>[\s\S]*?<\/a:(?:solidFill|gradFill|pattFill)>)/i;
  if (pattern.test(tableCellProperties)) return tableCellProperties.replace(pattern, replacement);
  if (/\/>\s*$/i.test(tableCellProperties)) {
    return tableCellProperties.replace(/\/>\s*$/i, `>${replacement}</a:tcPr>`);
  }
  return tableCellProperties.replace(/<\/a:tcPr>\s*$/i, `${replacement}</a:tcPr>`);
}

function replaceSourceObject(
  slideXml: string,
  source: PresentationShapeSource,
  update: (element: string) => string,
): string {
  const pattern = new RegExp(`<p:${source.kind}\\b[\\s\\S]*?<\\/p:${source.kind}>`, "g");
  let found = false;
  const patched = slideXml.replace(pattern, (element) => {
    const cNvPr = element.match(/<p:cNvPr\b[^>]*\/?\s*>/)?.[0];
    const objectId = cNvPr?.match(/\bid="([^"]+)"/i)?.[1];
    if (objectId !== source.objectId) return element;
    found = true;
    return update(element);
  });
  if (!found) throw new PresentationPreservationError(`Unable to find source object ${source.objectId}.`);
  return patched;
}

function imageExtensionForMime(mime: string): string {
  if (mime === "image/jpeg") return "jpg";
  if (mime === "image/svg+xml") return "svg";
  if (mime === "image/webp") return "webp";
  if (mime === "image/gif") return "gif";
  return "png";
}

async function newImagePayload(url: string): Promise<{ bytes: Uint8Array; mime: string; extension: string }> {
  let blob: Blob;
  if (url.startsWith("data:")) {
    const match = url.match(/^data:([^;,]+)(;base64)?,(.*)$/s);
    if (!match) throw new PresentationPreservationError("The inserted image data is invalid.");
    const mime = match[1].toLowerCase();
    if (!mime.startsWith("image/")) throw new PresentationPreservationError("The inserted object is not an image.");
    const binary = match[2] ? atob(match[3]) : decodeURIComponent(match[3]);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return { bytes, mime, extension: imageExtensionForMime(mime) };
  }
  const response = await fetch(url);
  if (!response.ok) throw new PresentationPreservationError(`Unable to read the inserted image (${response.status}).`);
  blob = await response.blob();
  const mime = blob.type.toLowerCase();
  if (!mime.startsWith("image/")) throw new PresentationPreservationError("The inserted object is not an image.");
  return {
    bytes: new Uint8Array(await blob.arrayBuffer()),
    mime,
    extension: imageExtensionForMime(mime),
  };
}

function relationshipPartForSlide(slidePart: string): string {
  const separator = slidePart.lastIndexOf("/");
  const directory = separator >= 0 ? slidePart.slice(0, separator) : "";
  const fileName = separator >= 0 ? slidePart.slice(separator + 1) : slidePart;
  return `${directory}/_rels/${fileName}.rels`;
}

function nextRelationshipId(relationshipsXml: string): string {
  const ids = Array.from(relationshipsXml.matchAll(/\bId="rId(\d+)"/g), (match) => Number(match[1]));
  return `rId${Math.max(0, ...ids) + 1}`;
}

function nextMediaPart(zip: { files: Record<string, unknown> }, extension: string): string {
  const indexes = Object.keys(zip.files)
    .map((path) => path.match(/^ppt\/media\/image(\d+)\.[^.]+$/i)?.[1])
    .filter(Boolean)
    .map(Number);
  return `ppt/media/image${Math.max(0, ...indexes) + 1}.${extension}`;
}

function ensureImageContentType(contentTypesXml: string, extension: string, mime: string): string {
  const existing = new RegExp(`<Default\\b[^>]*\\bExtension="${extension}"`, "i");
  if (existing.test(contentTypesXml)) return contentTypesXml;
  return contentTypesXml.replace(
    /<\/Types>\s*$/i,
    `<Default Extension="${escapeXml(extension)}" ContentType="${escapeXml(mime)}"/></Types>`,
  );
}

function appendImageRelationship(relationshipsXml: string, relationshipId: string, mediaPart: string): string {
  const target = `../media/${mediaPart.split("/").pop()}`;
  const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="${escapeXml(target)}"/>`;
  return relationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
}

function appendHyperlinkRelationship(relationshipsXml: string, relationshipId: string, url: string): string {
  const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="${escapeXml(url)}" TargetMode="External"/>`;
  return relationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
}

function pictureObjectXml(
  shape: PreservePresentationShape,
  objectId: number,
  relationshipId: string,
  hyperlinkRelationshipId: string | undefined,
  slideWidth: number,
  slideHeight: number,
): string {
  const x = Math.round((shape.x / 100) * slideWidth);
  const y = Math.round((shape.y / 100) * slideHeight);
  const cx = Math.max(1, Math.round((shape.w / 100) * slideWidth));
  const cy = Math.max(1, Math.round((shape.h / 100) * slideHeight));
  const rotation = shape.rotation ? ` rot="${Math.round(shape.rotation * 60000)}"` : "";
  const flips = `${shape.flipH ? ' flipH="1"' : ""}${shape.flipV ? ' flipV="1"' : ""}`;
  const crop = shape.imgCrop && (shape.imgCrop.l || shape.imgCrop.t || shape.imgCrop.r || shape.imgCrop.b)
    ? `<a:srcRect ${(["l", "t", "r", "b"] as const).map((key) => `${key}="${Math.round(Math.max(0, Math.min(95, shape.imgCrop![key])) * 1000)}"`).join(" ")}/>`
    : "";
  const alpha = Math.round(Math.max(0, Math.min(1, shape.opacity ?? 1)) * 100_000);
  const blip = alpha < 100_000
    ? `<a:blip r:embed="${relationshipId}"><a:alphaModFix amt="${alpha}"/></a:blip>`
    : `<a:blip r:embed="${relationshipId}"/>`;
  return [
    "<p:pic>",
    `<p:nvPicPr><p:cNvPr id="${objectId}" name="Inserted image ${objectId}">${hyperlinkRelationshipId ? `<a:hlinkClick r:id="${hyperlinkRelationshipId}"/>` : ""}</p:cNvPr><p:cNvPicPr/><p:nvPr/></p:nvPicPr>`,
    `<p:blipFill>${blip}${crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill>`,
    `<p:spPr><a:xfrm${rotation}${flips}><a:off x="${x}" y="${y}"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>${presentationLineXml(shape)}</p:spPr>`,
    "</p:pic>",
  ].join("");
}

function nextObjectId(slideXml: string): number {
  const ids = Array.from(slideXml.matchAll(/<p:cNvPr\b[^>]*\bid="(\d+)"/g), (match) => Number(match[1]));
  return Math.max(1, ...ids) + 1;
}

function transformXml(shape: PreservePresentationShape, slideWidth: number, slideHeight: number, prefix = "a"): string {
  const x = Math.round((shape.x / 100) * slideWidth);
  const y = Math.round((shape.y / 100) * slideHeight);
  const cx = Math.max(1, Math.round((shape.w / 100) * slideWidth));
  const cy = Math.max(1, Math.round((shape.h / 100) * slideHeight));
  const attributes = `${shape.rotation ? ` rot="${Math.round(shape.rotation * 60000)}"` : ""}${shape.flipH ? ' flipH="1"' : ""}${shape.flipV ? ' flipV="1"' : ""}`;
  return `<${prefix}:xfrm${attributes}><${prefix}:off x="${x}" y="${y}"/><${prefix}:ext cx="${cx}" cy="${cy}"/></${prefix}:xfrm>`;
}

function bodyPropertiesXml(shape: PreservePresentationShape): string {
  const anchor = shape.vAlign === "middle" ? "ctr" : shape.vAlign === "bottom" ? "b" : "t";
  const padding = shape.padding;
  return `<a:bodyPr wrap="square" anchor="${anchor}"${padding ? ` lIns="${Math.round(padding.l * 12_700)}" tIns="${Math.round(padding.t * 12_700)}" rIns="${Math.round(padding.r * 12_700)}" bIns="${Math.round(padding.b * 12_700)}"` : ""}/>`;
}

function shapeObjectXml(
  shape: PreservePresentationShape,
  objectId: number,
  slideWidth: number,
  slideHeight: number,
  hyperlinkRelationshipId?: string,
): string {
  const geometry = presentationGeometryXml(shape);
  const effect = shape.shadow
    ? `<a:effectLst><a:outerShdw blurRad="${Math.round(Math.max(0, shape.shadow.blur) * 12_700)}" dist="${Math.round(Math.max(0, shape.shadow.dist) * 12_700)}" dir="${Math.round((((shape.shadow.angle || 0) % 360) + 360) % 360 * 60_000)}">${presentationColorXml(shape.shadow.color, shape.shadow.alpha)}</a:outerShdw></a:effectLst>`
    : "";
  const paragraphs = (shape.texts.length ? shape.texts : [{ text: "" }]).map(presentationParagraphXml).join("");
  return [
    "<p:sp>",
    `<p:nvSpPr><p:cNvPr id="${objectId}" name="Manor object ${objectId}">${hyperlinkRelationshipId ? `<a:hlinkClick r:id="${hyperlinkRelationshipId}"/>` : ""}</p:cNvPr><p:cNvSpPr/><p:nvPr/></p:nvSpPr>`,
    `<p:spPr>${transformXml(shape, slideWidth, slideHeight)}${geometry}${presentationFillXml(shape)}${presentationLineXml(shape)}${effect}</p:spPr>`,
    `<p:txBody>${bodyPropertiesXml(shape)}<a:lstStyle/>${paragraphs}</p:txBody>`,
    "</p:sp>",
  ].join("");
}

function tableCellXml(cell: PresentationTableCell): string {
  const attributes = `${cell.gridSpan && cell.gridSpan > 1 ? ` gridSpan="${cell.gridSpan}"` : ""}${cell.vMerge ? ' vMerge="1"' : ""}`;
  const fill = cell.fill ? `<a:solidFill>${presentationColorXml(cell.fill)}</a:solidFill>` : "<a:noFill/>";
  return `<a:tc${attributes}><a:txBody><a:bodyPr/><a:lstStyle/>${presentationParagraphXml({ text: cell.text, bold: cell.bold, color: cell.color })}</a:txBody><a:tcPr>${fill}</a:tcPr></a:tc>`;
}

function tableObjectXml(
  shape: PreservePresentationShape,
  objectId: number,
  slideWidth: number,
  slideHeight: number,
): string {
  const rows = shape.tableRows || [];
  const columnCount = Math.max(1, ...rows.map((row) => row.reduce((count, cell) => count + Math.max(1, cell.gridSpan || 1), 0)));
  const width = Math.max(1, Math.round((shape.w / 100) * slideWidth));
  const height = Math.max(1, Math.round((shape.h / 100) * slideHeight));
  const columnWidth = Math.max(1, Math.round(width / columnCount));
  const rowHeight = Math.max(1, Math.round(height / Math.max(1, rows.length)));
  const sourceColumnWidths = shape.tableColWidths || [];
  const totalSourceColumnWidth = sourceColumnWidths.reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0);
  const gridWidths = sourceColumnWidths.length === columnCount && totalSourceColumnWidth > 0
    ? sourceColumnWidths.map((value) => Math.max(1, Math.round((Math.max(0, Number(value) || 0) / totalSourceColumnWidth) * width)))
    : Array.from({ length: columnCount }, () => columnWidth);
  const grid = gridWidths.map((gridWidth) => `<a:gridCol w="${gridWidth}"/>`).join("");
  const rowXml = (rows.length ? rows : [[{ text: "" }]])
    .map((row) => `<a:tr h="${rowHeight}">${row.map(tableCellXml).join("")}</a:tr>`)
    .join("");
  return [
    "<p:graphicFrame>",
    `<p:nvGraphicFramePr><p:cNvPr id="${objectId}" name="Manor table ${objectId}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>`,
    transformXml(shape, slideWidth, slideHeight, "p"),
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">',
    `<a:tbl><a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>${grid}</a:tblGrid>${rowXml}</a:tbl>`,
    "</a:graphicData></a:graphic></p:graphicFrame>",
  ].join("");
}

function sourceObjectPattern(source: PresentationShapeSource): RegExp {
  return new RegExp(`<p:${source.kind}\\b[\\s\\S]*?<\\/p:${source.kind}>`, "g");
}

function takeSourceObject(slideXml: string, source: PresentationShapeSource): { xml: string; object?: string } {
  let object: string | undefined;
  const xml = slideXml.replace(sourceObjectPattern(source), (element) => {
    const id = element.match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/i)?.[1];
    if (id !== source.objectId || object) return element;
    object = element;
    return "";
  });
  return { xml, object };
}

function appendObjectsToSlide(slideXml: string, objects: string[]): string {
  if (!objects.length) return slideXml;
  if (!/<\/p:spTree>/i.test(slideXml)) throw new PresentationPreservationError("This slide has no editable shape tree.");
  return slideXml.replace(/<\/p:spTree>/i, `${objects.join("")}</p:spTree>`);
}

function patchSlideBackground(slideXml: string, slide: PreservePresentationSlide): string {
  const fill = slide.bgGrad?.stops?.length
    ? presentationFillXml({ id: "slide-background", x: 0, y: 0, w: 100, h: 100, texts: [], gradFill: slide.bgGrad })
    : `<a:solidFill>${presentationColorXml(slide.bg || "#ffffff")}</a:solidFill>`;
  const background = `<p:bg><p:bgPr>${fill}<a:effectLst/></p:bgPr></p:bg>`;
  if (/<p:bg\b[^>]*(?:\/>|>[\s\S]*?<\/p:bg>)/i.test(slideXml)) {
    return slideXml.replace(/<p:bg\b[^>]*(?:\/>|>[\s\S]*?<\/p:bg>)/i, background);
  }
  return slideXml.replace(/<p:cSld\b[^>]*>/i, (openTag) => `${openTag}${background}`);
}

function patchSlideImageBackground(slideXml: string, relationshipId: string): string {
  const background = `<p:bg><p:bgPr><a:blipFill><a:blip r:embed="${escapeXml(relationshipId)}"/><a:stretch><a:fillRect/></a:stretch></a:blipFill><a:effectLst/></p:bgPr></p:bg>`;
  if (/<p:bg\b[^>]*(?:\/>|>[\s\S]*?<\/p:bg>)/i.test(slideXml)) {
    return slideXml.replace(/<p:bg\b[^>]*(?:\/>|>[\s\S]*?<\/p:bg>)/i, background);
  }
  return slideXml.replace(/<p:cSld\b[^>]*>/i, (openTag) => `${openTag}${background}`);
}

function materializeInheritedSlideObjects(
  slideXml: string,
  slidePart: string,
  baselineLockedShapes: PreservePresentationShape[],
): string {
  const groupedObjectIds = new Set(
    baselineLockedShapes
      .filter((shape) => shape.source?.part === slidePart)
      .map((shape) => shape.source!.objectId),
  );
  let output = slideXml;
  for (const group of presentationObjectGroups(slideXml)) {
    if ([...group.objectIds].some((objectId) => groupedObjectIds.has(objectId))) {
      output = output.replace(group.xml, "");
    }
  }
  if (baselineLockedShapes.some((shape) => shape.source?.part !== slidePart)) {
    output = output.replace(/<p:sld\b[^>]*>/i, (openTag) => setXmlAttribute(openTag, "showMasterSp", "0"));
  }
  return output;
}

function presentationGroupContainerObjectIds(groupXml: string): Set<string> {
  return new Set(Array.from(
    groupXml.matchAll(/<p:nvGrpSpPr\b[^>]*>[\s\S]*?<\/p:nvGrpSpPr>/gi),
    (properties) => properties[0].match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/i)?.[1],
  ).filter((id): id is string => Boolean(id)));
}

function reorderPresentationGroupMembers(groupXml: string, orderByObjectId: Map<string, number>): string {
  const openingEnd = groupXml.indexOf(">");
  const closingStart = groupXml.toLowerCase().lastIndexOf("</p:grpsp>");
  if (openingEnd < 0 || closingStart <= openingEnd) return groupXml;
  const content = groupXml.slice(openingEnd + 1, closingStart);
  const nestedGroups = presentationObjectGroups(content);
  const nestedItems: Array<{ start: number; end: number; xml: string; order?: number }> = [];
  let searchCursor = 0;
  for (const nested of nestedGroups) {
    const start = content.indexOf(nested.xml, searchCursor);
    if (start < 0) continue;
    const orders = [...nested.objectIds].flatMap((objectId) => {
      const order = orderByObjectId.get(objectId);
      return order == null ? [] : [order];
    });
    nestedItems.push({
      start,
      end: start + nested.xml.length,
      xml: reorderPresentationGroupMembers(nested.xml, orderByObjectId),
      order: orders.length ? Math.min(...orders) : undefined,
    });
    searchCursor = start + nested.xml.length;
  }
  let directContent = content;
  for (const nested of [...nestedItems].reverse()) {
    directContent = `${directContent.slice(0, nested.start)}${" ".repeat(nested.end - nested.start)}${directContent.slice(nested.end)}`;
  }
  const directItems = Array.from(
    directContent.matchAll(/<p:(sp|pic|cxnSp|graphicFrame)\b[\s\S]*?<\/p:\1>/gi),
    (match) => {
      const start = match.index || 0;
      const xml = content.slice(start, start + match[0].length);
      const objectId = xml.match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/i)?.[1];
      return {
        start,
        end: start + match[0].length,
        xml,
        order: objectId ? orderByObjectId.get(objectId) : undefined,
      };
    },
  );
  const slots = [...directItems, ...nestedItems]
    .filter((item): item is typeof item & { order: number } => item.order != null)
    .sort((left, right) => left.start - right.start);
  if (slots.length < 2) {
    if (!nestedItems.length) return groupXml;
    let nestedOutput = content;
    for (const nested of [...nestedItems].reverse()) {
      nestedOutput = `${nestedOutput.slice(0, nested.start)}${nested.xml}${nestedOutput.slice(nested.end)}`;
    }
    return `${groupXml.slice(0, openingEnd + 1)}${nestedOutput}${groupXml.slice(closingStart)}`;
  }
  const orderedXml = [...slots]
    .sort((left, right) => left.order - right.order || left.start - right.start)
    .map((item) => item.xml);
  let output = "";
  let cursor = 0;
  slots.forEach((slot, index) => {
    output += content.slice(cursor, slot.start);
    output += orderedXml[index];
    cursor = slot.end;
  });
  output += content.slice(cursor);
  return `${groupXml.slice(0, openingEnd + 1)}${output}${groupXml.slice(closingStart)}`;
}

function parseSlideSize(presentationXml: string): { width: number; height: number } {
  const match = presentationXml.match(/<p:sldSz\b[^>]*\bcx="(\d+)"[^>]*\bcy="(\d+)"/i);
  return match
    ? { width: Number(match[1]), height: Number(match[2]) }
    : { width: 12192000, height: 6858000 };
}

function patchSpeakerNotes(notesXml: string, notes: string): string {
  const bodyShape = (notesXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [])
    .find((shape) => /<p:ph\b[^>]*type="body"/i.test(shape));
  if (!bodyShape) throw new PresentationPreservationError("This slide has no editable speaker-notes body.");
  const textBody = bodyShape.match(/<p:txBody[\s>][\s\S]*?<\/p:txBody>/i)?.[0];
  if (!textBody) throw new PresentationPreservationError("This slide has no editable speaker-notes text body.");
  const bodyPr = textBody.match(/<a:bodyPr\b[^>]*\/?\s*>/i)?.[0] || "<a:bodyPr/>";
  const listStyle = textBody.match(/<a:lstStyle\b[^>]*>[\s\S]*?<\/a:lstStyle>|<a:lstStyle\b[^>]*\/>/i)?.[0] || "<a:lstStyle/>";
  const paragraphs = (notes.split(/\r?\n/) || [""])
    .map((line) => `<a:p><a:r><a:rPr lang="en-US"/><a:t>${escapeXml(line)}</a:t></a:r><a:endParaRPr lang="en-US"/></a:p>`)
    .join("");
  const nextTextBody = `<p:txBody>${bodyPr}${listStyle}${paragraphs}</p:txBody>`;
  return notesXml.replace(bodyShape, bodyShape.replace(textBody, nextTextBody));
}

function resolvePackageTarget(basePart: string, target: string): string {
  if (target.startsWith("/")) return target.slice(1);
  const parts = `${basePart.slice(0, basePart.lastIndexOf("/") + 1)}${target}`.split("/");
  const normalized: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") normalized.pop();
    else normalized.push(part);
  }
  return normalized.join("/");
}

function relationships(relationshipsXml: string): string[] {
  return relationshipsXml.match(/<Relationship\b[^>]*\/>/g) || [];
}

function relationshipAttribute(relationshipXml: string, name: string): string | undefined {
  return relationshipXml.match(new RegExp(`\\b${name}="([^"]+)"`, "i"))?.[1];
}

function pruneUnusedSlideObjectRelationships(
  slidePart: string,
  slideXml: string,
  relationshipsXml: string,
): { relationshipsXml: string; orphanedParts: string[] } {
  const referencedIds = new Set(
    Array.from(slideXml.matchAll(/\br:(?:id|embed|link)="([^"]+)"/gi), (match) => match[1]),
  );
  const orphanedParts: string[] = [];
  const nextRelationshipsXml = relationshipsXml.replace(/<Relationship\b[^>]*\/>/g, (relationship) => {
    const type = relationshipAttribute(relationship, "Type") || "";
    if (!type.endsWith("/image") && !type.endsWith("/hyperlink")) return relationship;
    const id = relationshipAttribute(relationship, "Id");
    if (!id || referencedIds.has(id)) return relationship;
    const target = relationshipAttribute(relationship, "Target");
    if (target && !/\bTargetMode="External"/i.test(relationship)) {
      orphanedParts.push(resolvePackageTarget(slidePart, target));
    }
    return "";
  });
  return { relationshipsXml: nextRelationshipsXml, orphanedParts };
}

type PresentationPackageReader = {
  files: Record<string, unknown>;
  file(path: string): { async(type: "text"): Promise<string> } | null;
};

async function presentationDependencyClosure(
  zip: PresentationPackageReader,
  roots: Iterable<string>,
): Promise<Set<string>> {
  const discovered = new Set<string>();
  const pending = Array.from(roots);
  while (pending.length > 0) {
    const part = pending.shift()!;
    if (!part || discovered.has(part)) continue;
    discovered.add(part);
    const relationshipEntry = zip.file(relationshipPartForSlide(part));
    if (!relationshipEntry) continue;
    for (const relationship of relationships(await relationshipEntry.async("text"))) {
      if (/\bTargetMode="External"/i.test(relationship)) continue;
      const target = relationshipAttribute(relationship, "Target");
      if (!target) continue;
      const targetPart = resolvePackageTarget(part, target);
      if (!discovered.has(targetPart)) pending.push(targetPart);
    }
  }
  return discovered;
}

async function reachablePresentationParts(zip: PresentationPackageReader): Promise<Set<string>> {
  const roots = new Set(["ppt/presentation.xml"]);
  const packageRelationships = zip.file("_rels/.rels");
  if (packageRelationships) {
    for (const relationship of relationships(await packageRelationships.async("text"))) {
      if (/\bTargetMode="External"/i.test(relationship)) continue;
      const target = relationshipAttribute(relationship, "Target");
      if (target) roots.add(resolvePackageTarget("", target));
    }
  }
  return presentationDependencyClosure(zip, roots);
}

function ensurePartOverride(contentTypesXml: string, part: string, contentType: string): string {
  const partName = `/${part}`;
  if (new RegExp(`<Override\\b[^>]*\\bPartName="${partName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`, "i").test(contentTypesXml)) {
    return contentTypesXml;
  }
  return contentTypesXml.replace(
    /<\/Types>\s*$/i,
    `<Override PartName="${escapeXml(partName)}" ContentType="${escapeXml(contentType)}"/></Types>`,
  );
}

function nextNumberedPart(paths: string[], pattern: RegExp, prefix: string, suffix: string): string {
  const indexes = paths.map((path) => Number(path.match(pattern)?.[1] || 0));
  return `${prefix}${Math.max(0, ...indexes) + 1}${suffix}`;
}

function emptySlideXml(): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    + '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>'
    + '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>';
}

function emptyRelationshipsXml(layoutTarget: string): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="${escapeXml(layoutTarget)}"/>`
    + '</Relationships>';
}

function emptyNotesSlideXml(): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    + '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder 1"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>'
    + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>';
}

function emptyNotesMasterXml(): string {
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<p:notesMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    + '<p:cSld name=""><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder 1"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>'
    + '</p:spTree></p:cSld>'
    + '<p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/>'
    + '<p:hf hdr="1" ftr="1" dt="1" sldNum="1"/><p:notesStyle><a:lvl1pPr marL="0" algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1"><a:defRPr sz="1200" kern="1200"/></a:lvl1pPr></p:notesStyle>'
    + '</p:notesMaster>';
}

function removePartOverride(contentTypesXml: string, part: string): string {
  const escapedPart = `/${part}`.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return contentTypesXml.replace(new RegExp(`<Override\\b[^>]*\\bPartName="${escapedPart}"[^>]*/>`, "gi"), "");
}

function ensureCompatibleShapeChange(baseline: PreservePresentationShape, edited: PreservePresentationShape) {
  const baselineCells = baseline.tableRows?.flat() || [];
  const editedCells = edited.tableRows?.flat() || [];
  if (baselineCells.length !== editedCells.length) {
    throw new PresentationPreservationError("Changing this presentation table structure is not supported yet.");
  }
  for (let index = 0; index < baselineCells.length; index += 1) {
    const baselineTopology = { gridSpan: baselineCells[index].gridSpan, vMerge: baselineCells[index].vMerge };
    const editedTopology = { gridSpan: editedCells[index].gridSpan, vMerge: editedCells[index].vMerge };
    if (!sameValue(baselineTopology, editedTopology)) {
      throw new PresentationPreservationError("Changing this presentation table structure is not supported yet.");
    }
  }
}

export async function preservePresentationFileWithSnapshot(
  original: ArrayBuffer,
  baselineSlides: PreservePresentationSlide[],
  editedSlides: PreservePresentationSlide[],
  fileName: string,
): Promise<PreservePresentationResult> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(original);
  const presentationEntry = zip.file("ppt/presentation.xml");
  if (!presentationEntry) throw new PresentationPreservationError("The PPTX package has no presentation.xml part.");
  let presentationXml = await presentationEntry.async("text");
  const size = parseSlideSize(presentationXml);
  const presentationRelationshipsEntry = zip.file("ppt/_rels/presentation.xml.rels");
  if (!presentationRelationshipsEntry) throw new PresentationPreservationError("The PPTX package has no presentation relationships part.");
  let presentationRelationshipsXml = await presentationRelationshipsEntry.async("text");
  const contentTypesEntry = zip.file("[Content_Types].xml");
  if (!contentTypesEntry) throw new PresentationPreservationError("The PPTX package has no content-types part.");
  let contentTypesXml = await contentTypesEntry.async("text");
  const savedSlides = structuredClone(editedSlides);
  const baselineById = new Map(baselineSlides.map((slide) => [slide.id, slide]));
  const sourceKey = (shape: PreservePresentationShape) => shape.source
    ? `${shape.source.part}:${shape.source.kind}:${shape.source.objectId}`
    : "";
  const slideRelationshipByPart = new Map<string, { id: string; xml: string }>();
  for (const relationship of relationships(presentationRelationshipsXml)) {
    const type = relationshipAttribute(relationship, "Type") || "";
    const target = relationshipAttribute(relationship, "Target");
    const id = relationshipAttribute(relationship, "Id");
    if (!type.endsWith("/slide") || !target || !id) continue;
    slideRelationshipByPart.set(resolvePackageTarget("ppt/presentation.xml", target), { id, xml: relationship });
  }
  const slideIdByRelationship = new Map<string, string>();
  for (const slideIdTag of presentationXml.match(/<p:sldId\b[^>]*\/>/g) || []) {
    const relationshipId = relationshipAttribute(slideIdTag, "r:id");
    const slideId = relationshipAttribute(slideIdTag, "id");
    if (relationshipId && slideId) slideIdByRelationship.set(relationshipId, slideId);
  }
  let nextPresentationSlideId = Math.max(255, ...Array.from(slideIdByRelationship.values(), Number)) + 1;
  const outputPartBySlideId = new Map<string, string>();
  const outputRelationshipBySlideId = new Map<string, string>();
  const potentiallyOrphanedParts = new Set<string>();

  const layoutTargetFor = async (templatePart?: string): Promise<string> => {
    const candidates = [templatePart, ...baselineSlides.map((slide) => slide.sourcePart)].filter(Boolean) as string[];
    for (const candidate of candidates) {
      const entry = zip.file(relationshipPartForSlide(candidate));
      if (!entry) continue;
      const xml = await entry.async("text");
      const layout = relationships(xml).find((item) => (relationshipAttribute(item, "Type") || "").endsWith("/slideLayout"));
      const target = layout && relationshipAttribute(layout, "Target");
      if (target) return target;
    }
    return "../slideLayouts/slideLayout1.xml";
  };

  const ensureNotesMaster = async (): Promise<string> => {
    const existingRelationship = relationships(presentationRelationshipsXml).find((relationship) => (
      (relationshipAttribute(relationship, "Type") || "").endsWith("/notesMaster")
    ));
    const existingTarget = existingRelationship && relationshipAttribute(existingRelationship, "Target");
    if (existingTarget) return resolvePackageTarget("ppt/presentation.xml", existingTarget);

    const existingPart = Object.keys(zip.files).find((path) => /^ppt\/notesMasters\/notesMaster\d+\.xml$/i.test(path));
    const notesMasterPart = existingPart || nextNumberedPart(
      Object.keys(zip.files),
      /^ppt\/notesMasters\/notesMaster(\d+)\.xml$/i,
      "ppt/notesMasters/notesMaster",
      ".xml",
    );
    if (!existingPart) {
      const themePart = Object.keys(zip.files).find((path) => /^ppt\/theme\/theme\d+\.xml$/i.test(path));
      const themeRelationship = themePart
        ? `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/${themePart.split("/").pop()}"/>`
        : "";
      zip.file(notesMasterPart, emptyNotesMasterXml());
      zip.file(
        relationshipPartForSlide(notesMasterPart),
        `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${themeRelationship}</Relationships>`,
      );
      contentTypesXml = ensurePartOverride(
        contentTypesXml,
        notesMasterPart,
        "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml",
      );
    }

    const relationshipId = nextRelationshipId(presentationRelationshipsXml);
    const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" Target="notesMasters/${notesMasterPart.split("/").pop()}"/>`;
    presentationRelationshipsXml = presentationRelationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
    const notesMasterId = `<p:notesMasterIdLst><p:notesMasterId r:id="${relationshipId}"/></p:notesMasterIdLst>`;
    if (/<p:sldIdLst\b/i.test(presentationXml)) {
      presentationXml = presentationXml.replace(/<p:sldIdLst\b/i, `${notesMasterId}<p:sldIdLst`);
    } else {
      presentationXml = presentationXml.replace(/<\/p:presentation>\s*$/i, `${notesMasterId}</p:presentation>`);
    }
    return notesMasterPart;
  };

  const attachNotesToSlide = async (
    slidePart: string,
    slideRelationshipsXml: string,
    edited: PreservePresentationSlide,
  ): Promise<string> => {
    if (!(edited.notes || "").trim() && !edited.notesPart) return slideRelationshipsXml;
    const templateNotesPart = edited.notesPart
      || Object.keys(zip.files).find((path) => /^ppt\/notesSlides\/notesSlide\d+\.xml$/i.test(path));
    const notesPart = nextNumberedPart(
      Object.keys(zip.files),
      /^ppt\/notesSlides\/notesSlide(\d+)\.xml$/i,
      "ppt/notesSlides/notesSlide",
      ".xml",
    );
    let notesXml: string;
    let notesRelationshipsXml: string;
    const templateEntry = templateNotesPart ? zip.file(templateNotesPart) : null;
    const templateRelationshipsEntry = templateNotesPart ? zip.file(relationshipPartForSlide(templateNotesPart)) : null;
    if (templateEntry && templateRelationshipsEntry) {
      notesXml = patchSpeakerNotes(await templateEntry.async("text"), edited.notes || "");
      notesRelationshipsXml = (await templateRelationshipsEntry.async("text")).replace(/<Relationship\b[^>]*\/>/g, (relationship) => {
        const type = relationshipAttribute(relationship, "Type") || "";
        if (!type.endsWith("/slide")) return relationship;
        return setXmlAttribute(relationship, "Target", `../slides/${slidePart.split("/").pop()}`);
      });
    } else {
      const notesMasterPart = await ensureNotesMaster();
      notesXml = patchSpeakerNotes(emptyNotesSlideXml(), edited.notes || "");
      notesRelationshipsXml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" Target="../notesMasters/${notesMasterPart.split("/").pop()}"/>`
        + `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="../slides/${slidePart.split("/").pop()}"/>`
        + '</Relationships>';
    }
    zip.file(notesPart, notesXml);
    zip.file(relationshipPartForSlide(notesPart), notesRelationshipsXml);
    contentTypesXml = ensurePartOverride(
      contentTypesXml,
      notesPart,
      "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml",
    );
    const relationshipId = nextRelationshipId(slideRelationshipsXml);
    const notesRelationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/${notesPart.split("/").pop()}"/>`;
    edited.notesPart = notesPart;
    return slideRelationshipsXml.replace(/<\/Relationships>\s*$/i, `${notesRelationship}</Relationships>`);
  };

  for (const edited of savedSlides) {
    const baseline = baselineById.get(edited.id);
    let slidePart: string;
    let slideXml: string;
    let slideRelationshipsXml: string;
    if (baseline) {
      if (!baseline.sourcePart) throw new PresentationPreservationError("This slide has no editable source part.");
      edited.sourcePart = baseline.sourcePart;
      if (!sameValue(slideUnsupportedProperties(baseline), slideUnsupportedProperties(edited))) {
        throw new PresentationPreservationError("Changing this slide layout is not supported yet.");
      }
      slidePart = baseline.sourcePart;
      const slideEntry = zip.file(slidePart);
      if (!slideEntry) throw new PresentationPreservationError(`Missing slide part ${slidePart}.`);
      slideXml = await slideEntry.async("text");
      const relationshipEntry = zip.file(relationshipPartForSlide(slidePart));
      if (!relationshipEntry) throw new PresentationPreservationError(`Missing slide relationships part ${relationshipPartForSlide(slidePart)}.`);
      slideRelationshipsXml = await relationshipEntry.async("text");
    } else {
      const templatePart = edited.sourcePart;
      slidePart = nextNumberedPart(Object.keys(zip.files), /^ppt\/slides\/slide(\d+)\.xml$/i, "ppt/slides/slide", ".xml");
      edited.sourcePart = slidePart;
      slideXml = emptySlideXml();
      slideRelationshipsXml = emptyRelationshipsXml(await layoutTargetFor(templatePart));
      contentTypesXml = ensurePartOverride(
        contentTypesXml,
        slidePart,
        "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
      );
    }

    const baselineShapeById = new Map((baseline?.shapes || []).map((shape) => [shape.id, shape]));
    for (const editedShape of edited.shapes) {
      if (editedShape.source) continue;
      const persistedSource = baselineShapeById.get(editedShape.id)?.source;
      if (persistedSource) editedShape.source = structuredClone(persistedSource);
    }
    const clonedSourceShapes = edited.shapes.filter((shape) => shape.source?.editable && shape.source.cloneOfObjectId);
    const clonedShapeIds = new Set(clonedSourceShapes.map((shape) => shape.id));
    let clonedObjectId = nextObjectId(slideXml);
    for (const clonedShape of clonedSourceShapes) {
      const clonedSource = clonedShape.source!;
      const templateShape = baseline?.shapes.find((shape) => (
        shape.source?.part === clonedSource.part
        && shape.source.kind === clonedSource.kind
        && shape.source.objectId === clonedSource.cloneOfObjectId
      ));
      if (!templateShape?.source || templateShape.source.part !== slidePart) {
        throw new PresentationPreservationError("This copied object no longer matches its source slide.");
      }
      ensureCompatibleShapeChange(templateShape, clonedShape);
      const assignedObjectId = String(clonedObjectId);
      let clonedObject = "";
      slideXml = replaceSourceObject(slideXml, templateShape.source, (element) => {
        clonedObject = element.replace(/<p:cNvPr\b[^>]*\/?\s*>/i, (openTag) => {
          const currentName = openTag.match(/\bname="([^"]*)"/i)?.[1] || "Object";
          return setXmlAttribute(setXmlAttribute(openTag, "id", assignedObjectId), "name", `${currentName} Copy`);
        });
        clonedObject = refreshClonedShapeCreationIds(clonedObject, slideXml);
        clonedObject = patchTransform(clonedObject, clonedShape, size.width, size.height);
        if (!sameValue(templateShape.imgCrop, clonedShape.imgCrop)) {
          clonedObject = patchImageCrop(clonedObject, clonedShape.imgCrop);
        }
        if (templateShape.opacity !== clonedShape.opacity && clonedSource.kind === "pic") {
          clonedObject = patchImageOpacity(clonedObject, clonedShape.opacity);
        }
        if (!sameValue(
          [templateShape.fill, templateShape.gradFill, templateShape.stroke, templateShape.strokeWidth, templateShape.presetGeom, templateShape.borderRadius, templateShape.shadow, templateShape.vAlign, templateShape.padding],
          [clonedShape.fill, clonedShape.gradFill, clonedShape.stroke, clonedShape.strokeWidth, clonedShape.presetGeom, clonedShape.borderRadius, clonedShape.shadow, clonedShape.vAlign, clonedShape.padding],
        )) clonedObject = patchShapeFormatting(clonedObject, clonedShape);
        if (!sameValue(templateShape.texts, clonedShape.texts)) {
          clonedObject = patchTextParagraphs(clonedObject, templateShape.texts, clonedShape.texts);
        }
        if (!sameValue(templateShape.tableRows, clonedShape.tableRows) && clonedShape.tableRows) {
          clonedObject = patchTableCells(clonedObject, templateShape.tableRows || [], clonedShape.tableRows);
        }
        return `${element}${clonedObject}`;
      });
      if (templateShape.imgUrl !== clonedShape.imgUrl) {
        if (!clonedSource.mediaPart || !clonedShape.imgUrl) {
          throw new PresentationPreservationError("This copied object cannot be converted to or from an image in place.");
        }
        const image = await newImagePayload(clonedShape.imgUrl);
        const mediaPart = nextMediaPart(zip, image.extension);
        const relationshipId = nextRelationshipId(slideRelationshipsXml);
        zip.file(mediaPart, image.bytes);
        contentTypesXml = ensureImageContentType(contentTypesXml, image.extension, image.mime);
        slideRelationshipsXml = appendImageRelationship(slideRelationshipsXml, relationshipId, mediaPart);
        slideXml = replaceSourceObject(slideXml, {
          ...clonedSource,
          objectId: assignedObjectId,
        }, (element) => element.replace(
          /<a:blip\b[^>]*\br:embed="[^"]+"/i,
          (openTag) => setXmlAttribute(openTag, "r:embed", relationshipId),
        ));
        clonedSource.mediaPart = mediaPart;
      }
      clonedSource.objectId = assignedObjectId;
      delete clonedSource.cloneOfObjectId;
      clonedObjectId += 1;
    }
    const baselineEditableShapes = baseline?.shapes.filter((shape) => shape.source?.editable) || [];
    const baselineShapeBySource = new Map(baselineEditableShapes.map((shape) => [sourceKey(shape), shape]));
    const editedSourceShapes = edited.shapes.filter((shape) => shape.source?.editable && !clonedShapeIds.has(shape.id));
    const addedShapes = edited.shapes.filter((shape) => !shape.source);
    const baselineLockedShapes = baseline?.shapes.filter((shape) => shape.source && !shape.source.editable) || [];
    const editedLockedShapes = edited.shapes.filter((shape) => shape.source && !shape.source.editable);
    const baselineSlideLockedShapes = baselineLockedShapes.filter((shape) => shape.source?.part === slidePart);
    const editedSlideLockedShapes = editedLockedShapes.filter((shape) => shape.source?.part === slidePart);
    const baselineInheritedLockedShapes = baselineLockedShapes.filter((shape) => shape.source?.part !== slidePart);
    const editedInheritedLockedShapes = editedLockedShapes.filter((shape) => shape.source?.part !== slidePart);
    const orderedShapeState = (shapes: PreservePresentationShape[]) => [...shapes]
      .sort((left, right) => sourceKey(left).localeCompare(sourceKey(right)));
    const inheritedLockedShapesChanged = !sameValue(
      orderedShapeState(baselineInheritedLockedShapes),
      orderedShapeState(editedInheritedLockedShapes),
    );
    const groupedObjectIds = new Set(presentationObjectGroups(slideXml).flatMap((group) => [...group.objectIds]));
    const baselineSlideSourceShapes = baseline?.shapes.filter((shape) => shape.source?.part === slidePart) || [];
    const modeledSlideSourceShapes = [...baselineSlideSourceShapes, ...clonedSourceShapes];
    const editedSlideSourceShapes = edited.shapes.filter((shape) => shape.source?.part === slidePart);
    const baselineSlideOrder = baselineSlideSourceShapes.map(sourceKey);
    const editedSlideOrder = editedSlideSourceShapes.map(sourceKey);
    if (inheritedLockedShapesChanged) {
      slideXml = materializeInheritedSlideObjects(slideXml, slidePart, baselineInheritedLockedShapes);
    }

    const baselineSlideLockedBySource = new Map(baselineSlideLockedShapes.map((shape) => [sourceKey(shape), shape]));
    const regeneratedSlideLockedShapes = editedSlideLockedShapes.filter((shape) => {
      if (groupedObjectIds.has(shape.source!.objectId)) return false;
      const original = baselineSlideLockedBySource.get(sourceKey(shape));
      return !original || !sameValue(original, shape);
    });
    const regeneratedSlideLockedKeys = new Set(regeneratedSlideLockedShapes.map(sourceKey));

    for (const editedShape of editedSourceShapes) {
      const originalShape = baselineShapeBySource.get(sourceKey(editedShape));
      if (!originalShape?.source) throw new PresentationPreservationError("This object no longer matches its source slide.");
      const source = originalShape.source;
      ensureCompatibleShapeChange(originalShape, editedShape);
      const transformChanged = !sameValue(
        [originalShape.x, originalShape.y, originalShape.w, originalShape.h, originalShape.rotation, originalShape.flipH, originalShape.flipV],
        [editedShape.x, editedShape.y, editedShape.w, editedShape.h, editedShape.rotation, editedShape.flipH, editedShape.flipV],
      );
      const cropChanged = !sameValue(originalShape.imgCrop, editedShape.imgCrop);
      const opacityChanged = originalShape.opacity !== editedShape.opacity;
      const formattingChanged = !sameValue(
        [originalShape.fill, originalShape.gradFill, originalShape.stroke, originalShape.strokeWidth, originalShape.presetGeom, originalShape.borderRadius, originalShape.shadow, originalShape.vAlign, originalShape.padding],
        [editedShape.fill, editedShape.gradFill, editedShape.stroke, editedShape.strokeWidth, editedShape.presetGeom, editedShape.borderRadius, editedShape.shadow, editedShape.vAlign, editedShape.padding],
      );
      const textChanged = !sameValue(originalShape.texts, editedShape.texts);
      const tableChanged = !sameValue(originalShape.tableRows, editedShape.tableRows);
      if (transformChanged || cropChanged || opacityChanged || formattingChanged || textChanged || tableChanged) {
        slideXml = replaceSourceObject(slideXml, source, (element) => {
          let patched = transformChanged
            ? patchTransform(element, editedShape, size.width, size.height)
            : element;
          if (cropChanged) patched = patchImageCrop(patched, editedShape.imgCrop);
          if (opacityChanged && source.kind === "pic") patched = patchImageOpacity(patched, editedShape.opacity);
          if (formattingChanged) patched = patchShapeFormatting(patched, editedShape);
          if (textChanged) patched = patchTextParagraphs(patched, originalShape.texts, editedShape.texts);
          if (tableChanged && editedShape.tableRows) {
            patched = patchTableCells(patched, originalShape.tableRows || [], editedShape.tableRows);
          }
          return patched;
        });
      }

      if (originalShape.imgUrl !== editedShape.imgUrl) {
        if (!source.mediaPart || !editedShape.imgUrl) {
          throw new PresentationPreservationError("This object cannot be converted to or from an image in place.");
        }
        const image = await newImagePayload(editedShape.imgUrl);
        const mediaPart = nextMediaPart(zip, image.extension);
        const relationshipId = nextRelationshipId(slideRelationshipsXml);
        zip.file(mediaPart, image.bytes);
        contentTypesXml = ensureImageContentType(contentTypesXml, image.extension, image.mime);
        slideRelationshipsXml = appendImageRelationship(slideRelationshipsXml, relationshipId, mediaPart);
        slideXml = replaceSourceObject(slideXml, source, (element) => element.replace(
          /<a:blip\b[^>]*\br:embed="[^"]+"/i,
          (openTag) => setXmlAttribute(openTag, "r:embed", relationshipId),
        ));
        if (editedShape.source) editedShape.source.mediaPart = mediaPart;
      }
    }

    const editableBackgroundChanged = !baseline || !sameValue([baseline.bg, baseline.bgGrad], [edited.bg, edited.bgGrad]);
    const backgroundImageChanged = !sameValue(baseline?.bgImgUrl, edited.bgImgUrl);
    if (edited.bgImgUrl && (!baseline || backgroundImageChanged)) {
      const image = await newImagePayload(edited.bgImgUrl);
      const mediaPart = nextMediaPart(zip, image.extension);
      const relationshipId = nextRelationshipId(slideRelationshipsXml);
      zip.file(mediaPart, image.bytes);
      contentTypesXml = ensureImageContentType(contentTypesXml, image.extension, image.mime);
      slideRelationshipsXml = appendImageRelationship(slideRelationshipsXml, relationshipId, mediaPart);
      slideXml = patchSlideImageBackground(slideXml, relationshipId);
    } else if (!edited.bgImgUrl && (editableBackgroundChanged || backgroundImageChanged)) {
      slideXml = patchSlideBackground(slideXml, edited);
    }

    const generatedShapes = [
      ...addedShapes,
      ...regeneratedSlideLockedShapes,
      ...(inheritedLockedShapesChanged ? editedInheritedLockedShapes : []),
    ];
    const structureChanged = !baseline
      || generatedShapes.length > 0
      || !sameValue(baselineSlideOrder, editedSlideOrder);
    if (structureChanged) {
      const nextGeneratedObjectId = nextObjectId(slideXml);
      const sourceObjects = new Map<string, string>();
      const groupedSourceKey = new Map<string, string>();
      const editedMemberKeys = new Set(editedSlideSourceShapes.map(sourceKey));
      const editedOrderByObjectId = new Map(editedSlideSourceShapes.map((shape, index) => (
        [shape.source!.objectId, index]
      )));
      for (const group of presentationObjectGroups(slideXml)) {
        const members = modeledSlideSourceShapes.filter((shape) => group.objectIds.has(shape.source!.objectId));
        if (!members.length) continue;
        const modeledObjectIds = new Set(members.map((shape) => shape.source!.objectId));
        const groupContainerObjectIds = presentationGroupContainerObjectIds(group.xml);
        const hasUnmodeledMembers = [...group.objectIds].some((objectId) => (
          !modeledObjectIds.has(objectId) && !groupContainerObjectIds.has(objectId)
        ));
        const remainingMembers = members.filter((shape) => editedMemberKeys.has(sourceKey(shape)));
        const primary = remainingMembers[0] && sourceKey(remainingMembers[0]);
        let groupXml = group.xml;
        for (const removedMember of members.filter((shape) => !editedMemberKeys.has(sourceKey(shape)))) {
          groupXml = takeSourceObject(groupXml, removedMember.source!).xml;
        }
        groupXml = reorderPresentationGroupMembers(groupXml, editedOrderByObjectId);
        if (!primary && hasUnmodeledMembers) {
          slideXml = slideXml.replace(group.xml, groupXml);
          continue;
        }
        slideXml = slideXml.replace(group.xml, "");
        if (!primary) continue;
        sourceObjects.set(primary, groupXml);
        remainingMembers.forEach((shape) => groupedSourceKey.set(sourceKey(shape), primary));
      }
      const baselineExtractedShapes = [
        ...baselineEditableShapes.filter((shape) => (
          shape.source?.part === slidePart && !groupedObjectIds.has(shape.source.objectId)
        )),
        ...baselineSlideLockedShapes.filter((shape) => (
          !groupedObjectIds.has(shape.source!.objectId)
        )),
        ...clonedSourceShapes.filter((shape) => (
          shape.source?.part === slidePart && !groupedObjectIds.has(shape.source.objectId)
        )),
      ];
      for (const originalShape of baselineExtractedShapes) {
        const taken = takeSourceObject(slideXml, originalShape.source!);
        slideXml = taken.xml;
        if (taken.object) sourceObjects.set(sourceKey(originalShape), taken.object);
      }
      const addedObjects = new Map<string, string>();
      const addedSources = new Map<string, PresentationShapeSource>();
      let objectId = nextGeneratedObjectId;
      for (const addedShape of generatedShapes) {
        const sourceObjectId = String(objectId);
        let hyperlinkRelationshipId: string | undefined;
        if (addedShape.hyperlink) {
          hyperlinkRelationshipId = nextRelationshipId(slideRelationshipsXml);
          slideRelationshipsXml = appendHyperlinkRelationship(slideRelationshipsXml, hyperlinkRelationshipId, addedShape.hyperlink);
        }
        if (addedShape.imgUrl) {
          const image = await newImagePayload(addedShape.imgUrl);
          const mediaPart = nextMediaPart(zip, image.extension);
          const relationshipId = nextRelationshipId(slideRelationshipsXml);
          zip.file(mediaPart, image.bytes);
          contentTypesXml = ensureImageContentType(contentTypesXml, image.extension, image.mime);
          slideRelationshipsXml = appendImageRelationship(slideRelationshipsXml, relationshipId, mediaPart);
          addedObjects.set(addedShape.id, pictureObjectXml(addedShape, objectId, relationshipId, hyperlinkRelationshipId, size.width, size.height));
          addedSources.set(addedShape.id, {
            part: slidePart,
            kind: "pic",
            objectId: sourceObjectId,
            editable: addedShape.source?.editable ?? true,
            mediaPart,
          });
        } else if (addedShape.type === "table" || addedShape.tableRows) {
          addedObjects.set(addedShape.id, tableObjectXml(addedShape, objectId, size.width, size.height));
          addedSources.set(addedShape.id, {
            part: slidePart,
            kind: "graphicFrame",
            objectId: sourceObjectId,
            editable: addedShape.source?.editable ?? true,
          });
        } else {
          addedObjects.set(addedShape.id, shapeObjectXml(addedShape, objectId, size.width, size.height, hyperlinkRelationshipId));
          addedSources.set(addedShape.id, {
            part: slidePart,
            kind: "sp",
            objectId: sourceObjectId,
            editable: addedShape.source?.editable ?? true,
          });
        }
        objectId += 1;
      }
      const emittedGroupedSources = new Set<string>();
      const orderedObjects = edited.shapes.flatMap((shape) => {
        const groupedKey = shape.source?.part === slidePart ? groupedSourceKey.get(sourceKey(shape)) : undefined;
        if (groupedKey) {
          if (emittedGroupedSources.has(groupedKey)) return [];
          emittedGroupedSources.add(groupedKey);
          const object = sourceObjects.get(groupedKey);
          return object ? [object] : [];
        }
        if (shape.source?.part === slidePart && regeneratedSlideLockedKeys.has(sourceKey(shape))) {
          const object = addedObjects.get(shape.id);
          return object ? [object] : [];
        }
        if (shape.source?.part === slidePart) {
          const object = sourceObjects.get(sourceKey(shape));
          return object ? [object] : [];
        }
        if (!shape.source) {
          const object = addedObjects.get(shape.id);
          return object ? [object] : [];
        }
        if (inheritedLockedShapesChanged && shape.source && !shape.source.editable) {
          const object = addedObjects.get(shape.id);
          return object ? [object] : [];
        }
        return [];
      });
      slideXml = appendObjectsToSlide(slideXml, orderedObjects);
      for (const shape of edited.shapes) {
        const source = addedSources.get(shape.id);
        if (source) shape.source = source;
      }
    }

    if (!baseline) slideRelationshipsXml = await attachNotesToSlide(slidePart, slideRelationshipsXml, edited);
    if (baseline && (baseline.notes || "") !== (edited.notes || "")) {
      if (baseline.notesPart) {
        const notesEntry = zip.file(baseline.notesPart);
        if (!notesEntry) throw new PresentationPreservationError(`Missing notes part ${baseline.notesPart}.`);
        zip.file(baseline.notesPart, patchSpeakerNotes(await notesEntry.async("text"), edited.notes || ""));
        edited.notesPart = baseline.notesPart;
      } else {
        slideRelationshipsXml = await attachNotesToSlide(slidePart, slideRelationshipsXml, edited);
      }
    }
    const prunedRelationships = pruneUnusedSlideObjectRelationships(slidePart, slideXml, slideRelationshipsXml);
    slideRelationshipsXml = prunedRelationships.relationshipsXml;
    prunedRelationships.orphanedParts.forEach((part) => potentiallyOrphanedParts.add(part));
    zip.file(slidePart, slideXml);
    zip.file(relationshipPartForSlide(slidePart), slideRelationshipsXml);

    outputPartBySlideId.set(edited.id, slidePart);
    const existingRelationship = slideRelationshipByPart.get(slidePart);
    if (existingRelationship && baseline) {
      outputRelationshipBySlideId.set(edited.id, existingRelationship.id);
    } else {
      const relationshipId = nextRelationshipId(presentationRelationshipsXml);
      const relationship = `<Relationship Id="${relationshipId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/${slidePart.split("/").pop()}"/>`;
      presentationRelationshipsXml = presentationRelationshipsXml.replace(/<\/Relationships>\s*$/i, `${relationship}</Relationships>`);
      outputRelationshipBySlideId.set(edited.id, relationshipId);
    }
  }

  const slideIdEntries = savedSlides.map((slide) => {
    const relationshipId = outputRelationshipBySlideId.get(slide.id);
    const part = outputPartBySlideId.get(slide.id);
    if (!relationshipId || !part) throw new PresentationPreservationError("Unable to save the slide order.");
    const existingSlideId = slideIdByRelationship.get(relationshipId);
    return `<p:sldId id="${existingSlideId || nextPresentationSlideId++}" r:id="${relationshipId}"/>`;
  }).join("");
  const keptSlideParts = new Set(outputPartBySlideId.values());
  const deletedDependencyCandidates = new Set(potentiallyOrphanedParts);
  const deletedSlideParts = new Set(
    baselineSlides.flatMap((slide) => (
      slide.sourcePart && !keptSlideParts.has(slide.sourcePart) ? [slide.sourcePart] : []
    )),
  );
  for (const slidePart of deletedSlideParts) {
    const dependencies = await presentationDependencyClosure(zip, [slidePart]);
    dependencies.forEach((part) => deletedDependencyCandidates.add(part));
    const relationship = slideRelationshipByPart.get(slidePart);
    if (!relationship) continue;
    presentationRelationshipsXml = presentationRelationshipsXml.replace(relationship.xml, "");
  }
  if (/<p:sldIdLst\b[^>]*>[\s\S]*?<\/p:sldIdLst>/i.test(presentationXml)) {
    presentationXml = presentationXml.replace(/<p:sldIdLst\b[^>]*>[\s\S]*?<\/p:sldIdLst>/i, `<p:sldIdLst>${slideIdEntries}</p:sldIdLst>`);
  } else {
    presentationXml = presentationXml.replace(/<p:presentation\b[^>]*>/i, (openTag) => `${openTag}<p:sldIdLst>${slideIdEntries}</p:sldIdLst>`);
  }
  zip.file("ppt/presentation.xml", presentationXml);
  zip.file("ppt/_rels/presentation.xml.rels", presentationRelationshipsXml);
  const reachableParts = await reachablePresentationParts(zip);
  for (const part of deletedDependencyCandidates) {
    if (reachableParts.has(part)) continue;
    zip.remove(part);
    zip.remove(relationshipPartForSlide(part));
    contentTypesXml = removePartOverride(contentTypesXml, part);
  }
  zip.file("[Content_Types].xml", contentTypesXml);

  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE", mimeType: PPTX_MIME });
  const safeName = fileName.toLowerCase().endsWith(".pptx") ? fileName : `${fileName.replace(/\.ppt$/i, "")}.pptx`;
  for (const slide of savedSlides) {
    for (const shape of slide.shapes) {
      for (const paragraph of shape.texts) delete paragraph.sourceMap;
      for (const row of shape.tableRows || []) {
        for (const cell of row) delete cell.sourceMap;
      }
    }
  }
  return {
    file: new File([blob], safeName, { type: PPTX_MIME, lastModified: Date.now() }),
    slides: savedSlides,
  };
}

export async function preservePresentationFile(
  original: ArrayBuffer,
  baselineSlides: PreservePresentationSlide[],
  editedSlides: PreservePresentationSlide[],
  fileName: string,
): Promise<File> {
  return (await preservePresentationFileWithSnapshot(original, baselineSlides, editedSlides, fileName)).file;
}
