export interface PresentationTextLevelStyle {
  align?: string;
  bullet?: string | null;
  indent?: number;
  hanging?: number;
  lineSpacing?: number;
  spaceBefore?: number;
  spaceAfter?: number;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number;
  color?: string;
  fontFamily?: string;
  baseline?: number;
  spacing?: number;
}

interface PresentationStyleResolvers {
  color: (xml: string) => string | undefined;
  font: (typeface: string | undefined) => string | undefined;
}

/**
 * Convert PowerPoint points to the editor's canonical 540px slide height.
 * The canonical slide is rendered at 75% of a 96-DPI, 7.5-inch-high slide,
 * so the CSS 4/3 point conversion and the 3/4 display scale cancel out.
 */
export function presentationPointsToCqh(points: number): string {
  return `${(points / 5.4).toFixed(3)}cqh`;
}

/** Use a metrically compatible bundled font when Office fonts are unavailable. */
export function presentationCompatibleFontFamily(typeface: string | undefined): string | undefined {
  if (!typeface) return undefined;
  const normalized = typeface.trim().toLowerCase();
  if (normalized === "calibri" || normalized === "calibri light") return "Carlito";
  return typeface;
}

function xmlAttr(xml: string, name: string): string | undefined {
  return xml.match(new RegExp(`\\b${name}="([^"]*)"`, "i"))?.[1];
}

function xmlElement(xml: string, tag: string): string | undefined {
  return xml.match(new RegExp(`<${tag}\\b[^>]*(?:\\/>|>[\\s\\S]*?<\\/${tag}>)`, "i"))?.[0];
}

function decodeXmlText(value: string): string {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_, hex: string) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, decimal: string) => String.fromCodePoint(parseInt(decimal, 10)))
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function booleanAttr(xml: string, name: string): boolean | undefined {
  const value = xmlAttr(xml, name);
  if (value == null) return undefined;
  return value === "1" || value === "true" || value === "on";
}

function runStyle(xml: string | undefined, resolvers: PresentationStyleResolvers): PresentationTextLevelStyle {
  if (!xml) return {};
  const latin = xml.match(/<a:latin\b[^>]*\btypeface="([^"]+)"/i)?.[1];
  const eastAsian = xml.match(/<a:ea\b[^>]*\btypeface="([^"]+)"/i)?.[1];
  const underline = xmlAttr(xml, "u");
  const strike = xmlAttr(xml, "strike");
  const fontSize = xmlAttr(xml, "sz");
  const baseline = xmlAttr(xml, "baseline");
  const spacing = xmlAttr(xml, "spc");
  return {
    bold: booleanAttr(xml, "b"),
    italic: booleanAttr(xml, "i"),
    underline: underline == null ? undefined : underline !== "none",
    strikethrough: strike == null ? undefined : strike !== "noStrike",
    fontSize: fontSize == null ? undefined : parseInt(fontSize, 10) / 100,
    color: resolvers.color(xml),
    fontFamily: resolvers.font(latin) || resolvers.font(eastAsian),
    baseline: baseline == null ? undefined : parseInt(baseline, 10) / 1000,
    spacing: spacing == null ? undefined : parseInt(spacing, 10) / 100,
  };
}

function spacingPoints(xml: string | undefined, fontSize: number | undefined): number | undefined {
  if (!xml) return undefined;
  const points = xml.match(/<a:spcPts\b[^>]*\bval="(\d+)"/i)?.[1];
  if (points) return parseInt(points, 10) / 100;
  const percent = xml.match(/<a:spcPct\b[^>]*\bval="(\d+)"/i)?.[1];
  return percent ? (fontSize || 18) * (parseInt(percent, 10) / 100000) : undefined;
}

function parseTextLevel(xml: string | undefined, resolvers: PresentationStyleResolvers): PresentationTextLevelStyle {
  if (!xml) return {};
  const defaultRun = runStyle(xmlElement(xml, "a:defRPr"), resolvers);
  const lineSpacingXml = xmlElement(xml, "a:lnSpc");
  const linePercent = lineSpacingXml?.match(/<a:spcPct\b[^>]*\bval="(\d+)"/i)?.[1];
  const linePoints = lineSpacingXml?.match(/<a:spcPts\b[^>]*\bval="(\d+)"/i)?.[1];
  const marginLeft = xmlAttr(xml, "marL");
  const firstLineIndent = xmlAttr(xml, "indent");
  let bullet: string | null | undefined;
  if (/<a:buNone\b/i.test(xml)) bullet = null;
  else {
    const bulletCharacter = xml.match(/<a:buChar\b[^>]*\bchar="([^"]+)"/i)?.[1];
    if (bulletCharacter) bullet = decodeXmlText(bulletCharacter);
    else if (/<a:buAutoNum\b/i.test(xml)) bullet = "#.";
  }
  return {
    ...defaultRun,
    align: xmlAttr(xml, "algn"),
    bullet,
    indent: marginLeft == null ? undefined : parseInt(marginLeft, 10) / 12700,
    hanging: firstLineIndent == null ? undefined : parseInt(firstLineIndent, 10) / 12700,
    lineSpacing: linePercent
      ? parseInt(linePercent, 10) / 100000
      : linePoints
        ? parseInt(linePoints, 10) / 100 / (defaultRun.fontSize || 12)
        : undefined,
    spaceBefore: spacingPoints(xmlElement(xml, "a:spcBef"), defaultRun.fontSize),
    spaceAfter: spacingPoints(xmlElement(xml, "a:spcAft"), defaultRun.fontSize),
  };
}

export function parsePresentationTextStyleLevels(
  xml: string | undefined,
  resolvers: PresentationStyleResolvers,
): PresentationTextLevelStyle[] {
  if (!xml) return [];
  const defaultParagraph = parseTextLevel(xmlElement(xml, "a:defPPr"), resolvers);
  return Array.from({ length: 9 }, (_, index) => ({
    ...defaultParagraph,
    ...parseTextLevel(xmlElement(xml, `a:lvl${index + 1}pPr`), resolvers),
  }));
}

export function mergePresentationTextStyleLevels(
  ...sources: PresentationTextLevelStyle[][]
): PresentationTextLevelStyle[] {
  return Array.from({ length: 9 }, (_, index) => Object.assign(
    {},
    ...sources.map((source) => source[index] || {}),
  ));
}

export function presentationPlaceholder(shapeXml: string): { type?: string; idx?: string } | null {
  const placeholder = shapeXml.match(/<p:ph\b[^>]*\/?\s*>/i)?.[0];
  if (!placeholder) return null;
  return { type: xmlAttr(placeholder, "type"), idx: xmlAttr(placeholder, "idx") };
}

function placeholderMatchScore(
  candidate: { type?: string; idx?: string },
  requested: { type?: string; idx?: string },
): number {
  let score = 0;
  if (requested.idx && candidate.idx === requested.idx) score += 4;
  if (requested.type && candidate.type === requested.type) score += 3;
  if (
    requested.type && candidate.type
    && [requested.type, candidate.type].every((type) => type === "title" || type === "ctrTitle")
  ) score += 2;
  if (!requested.type && !candidate.type) score += 1;
  return score;
}

export function findPresentationPlaceholderShape(containerXml: string | undefined, shapeXml: string): string | undefined {
  const requested = presentationPlaceholder(shapeXml);
  if (!containerXml || !requested) return undefined;
  let best: { score: number; xml: string } | undefined;
  for (const candidateXml of containerXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/gi) || []) {
    const candidate = presentationPlaceholder(candidateXml);
    if (!candidate) continue;
    const score = placeholderMatchScore(candidate, requested);
    if (score > 0 && (!best || score > best.score)) best = { score, xml: candidateXml };
  }
  return best?.xml;
}

function placeholderStyleKind(shapeXml: string): "titleStyle" | "bodyStyle" | "otherStyle" {
  const placeholder = presentationPlaceholder(shapeXml);
  if (!placeholder) return "otherStyle";
  const type = placeholder.type;
  if (type === "title" || type === "ctrTitle") return "titleStyle";
  if (!type || ["body", "obj", "subTitle", "pic", "tbl", "chart", "dgm", "media"].includes(type)) {
    return "bodyStyle";
  }
  return "otherStyle";
}

function shapeFontReferenceStyle(
  shapeXml: string,
  resolvers: PresentationStyleResolvers,
): PresentationTextLevelStyle[] {
  const style = xmlElement(shapeXml, "p:style");
  const fontRef = style ? xmlElement(style, "a:fontRef") : undefined;
  if (!fontRef) return [];
  const index = xmlAttr(fontRef, "idx");
  const fontFamily = index === "major"
    ? resolvers.font("+mj-lt")
    : index === "minor"
      ? resolvers.font("+mn-lt")
      : undefined;
  const color = resolvers.color(fontRef);
  return Array.from({ length: 9 }, () => ({ fontFamily, color }));
}

function textStyleList(shapeXml: string | undefined): string | undefined {
  return shapeXml ? xmlElement(shapeXml, "a:lstStyle") : undefined;
}

export function presentationInheritedTextStyleLevels(
  slideShapeXml: string,
  layoutXml: string | undefined,
  masterXml: string | undefined,
  resolvers: PresentationStyleResolvers,
): PresentationTextLevelStyle[] {
  const layoutShape = findPresentationPlaceholderShape(layoutXml, slideShapeXml);
  const masterShape = findPresentationPlaceholderShape(masterXml, layoutShape || slideShapeXml);
  const masterStyle = masterXml
    ? xmlElement(masterXml, `p:${placeholderStyleKind(slideShapeXml)}`)
    : undefined;
  return mergePresentationTextStyleLevels(
    parsePresentationTextStyleLevels(masterStyle, resolvers),
    parsePresentationTextStyleLevels(textStyleList(masterShape), resolvers),
    parsePresentationTextStyleLevels(textStyleList(layoutShape), resolvers),
    shapeFontReferenceStyle(slideShapeXml, resolvers),
    parsePresentationTextStyleLevels(textStyleList(slideShapeXml), resolvers),
  );
}
