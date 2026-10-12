// PPTX → slide model parser for the read-only viewer's browser renderer (the
// interim view while the server render runs, and the fallback when it fails).
// Pure string/zip work with no DOM access, so it can be tested in Node.
import {
  flattenPptxShapeTree,
  pptxGraphicFrameKind,
  pptxGraphicFrameRect,
  type PptxEmuMapper,
} from "./pptxShapeTree";
import {
  presentationColorWithAlpha,
  presentationMediaMime,
  presentationRelationshipsPart,
  presentationShapeFillScope,
  presentationVideoSource,
  resolvePresentationPartTarget,
} from "./presentationOoxml";
import {
  presentationInheritedTextStyleLevels,
  type PresentationTextLevelStyle,
} from "./presentationStyleInheritance";
import { presentationStrokeDash } from "./presentationShapeStyle";

let SLIDE_W = 12192000;
let SLIDE_H = 6858000;
const emu2pctX = (v: number) => (v / SLIDE_W) * 100;
const emu2pctY = (v: number) => (v / SLIDE_H) * 100;
/** EMU to pt (for font sizes: 1pt = 12700 EMU) */
const emu2pt = (v: number) => Math.round(v / 12700);

export interface PptxTextRun {
  text: string;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number;
  color?: string;
  fontFamily?: string;
  baseline?: number; // superscript (+) / subscript (-)
  spacing?: number; // letter spacing in pt
}

export interface PptxParagraph {
  text: string; // concatenated plain text (backward compat)
  bold?: boolean; italic?: boolean; underline?: boolean;
  fontSize?: number; color?: string; align?: string; fontFamily?: string;
  bullet?: string; indent?: number; indentRight?: number; hanging?: number;
  lineSpacing?: number; // multiplier (1.0 = single)
  spaceBefore?: number; // pt
  spaceAfter?: number; // pt
  runs?: PptxTextRun[]; // per-run formatting for rich rendering
}

export interface PptxShape {
  x: number; y: number; w: number; h: number; // percentages
  type?: "shape" | "table" | "image" | "line" | "placeholder";
  /** Labeled stand-in for graphic frames the CSS renderer cannot draw. */
  placeholderKind?: "chart" | "smartart" | "object";
  fill?: string;
  gradFill?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  borderRadius?: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
  stroke?: string;
  strokeWidth?: number;
  strokeDash?: string;
  presetGeom?: string;
  texts: PptxParagraph[];
  imgUrl?: string;
  altText?: string;
  videoUrl?: string;
  imgCrop?: { l: number; t: number; r: number; b: number }; // percentages
  opacity?: number;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  vAlign?: "top" | "middle" | "bottom"; // text vertical alignment
  wordWrap?: boolean;
  padding?: { l: number; t: number; r: number; b: number }; // text insets in %
  tableRows?: { text: string; bold?: boolean; color?: string; fill?: string }[][];
  tableColWidths?: number[]; // column widths in EMU
  tableCols?: number;
}

export interface PptxSlide {
  bg?: string;
  bgGrad?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  bgImgUrl?: string;
  shapes: PptxShape[];
  aspectRatio?: string;
  heightPoints?: number;
  theme?: PptxThemeSnapshot;
}

export interface PptxParseOptions {
  isCancelled?: () => boolean;
  onObjectUrl?: (url: string) => void;
}

interface PptxThemeSnapshot {
  colors: Record<string, string>;
  majorFont?: string;
  minorFont?: string;
}

function decodePptxText(value: string): string {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_, hex: string) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, decimal: string) => String.fromCodePoint(parseInt(decimal, 10)))
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function xmlAttr(el: string, attr: string): string | null {
  const re = new RegExp(`${attr}="([^"]*)"`, "i");
  const m = el.match(re);
  return m ? m[1] : null;
}

/** First `<tag>` element, self-closing (`<a:pPr lvl="1"/>`) or with content. */
function xmlElementAny(xml: string, tag: string): string | null {
  const re = new RegExp(`<${tag}(?=[\\s/>])[^>]*?(?:\\/>|>[\\s\\S]*?</${tag}>)`);
  return xml.match(re)?.[0] ?? null;
}

/** An attribute of the element's own start tag, matched by exact name. */
function xmlStartTagAttr(element: string, attr: string): string | null {
  const startTag = element.match(/^<[^>]*>/)?.[0] ?? element;
  return startTag.match(new RegExp(`\\s${attr}="([^"]*)"`))?.[1] ?? null;
}

/** Find content between an XML open/close tag (non-greedy, first match) */
function xmlInner(xml: string, tag: string): string | null {
  // handle <tag ... > ... </tag> or self-closing
  const re = new RegExp(`<${tag}[\\s>][\\s\\S]*?</${tag}>`, "i");
  const m = xml.match(re);
  return m ? m[0] : null;
}

const _viewerDefaultScheme: Record<string, string> = {
  dk1: "#000000", dk2: "#292524", lt1: "#ffffff", lt2: "#fafaf9",
  accent1: "#4472c4", accent2: "#ed7d31", accent3: "#a5a5a5",
  accent4: "#ffc000", accent5: "#5b9bd5", accent6: "#70ad47",
  tx1: "#000000", tx2: "#57534e", bg1: "#ffffff", bg2: "#f5f5f4",
  hlink: "#0563c1", folHlink: "#954f72",
};
let _viewerTheme: Record<string, string> = _viewerDefaultScheme;
// Theme fill styles from <a:fmtScheme>
let _viewerBgFillStyles: string[] = [];  // <a:bgFillStyleLst> — for bgRef idx 1001+
let _viewerFillStyles: string[] = [];    // <a:fillStyleLst> — for bgRef/fillRef idx 1-3
let _viewerLineStyles: string[] = [];    // <a:lnStyleLst> — for lnRef idx 1-3
// Theme fonts
let _viewerMajorFont = "";  // heading font (for +mj-lt)
let _viewerMinorFont = "";  // body font (for +mn-lt)

function parseViewerTheme(themeXml: string) {
  const colors: Record<string, string> = { ..._viewerDefaultScheme };
  const clrScheme = xmlInner(themeXml, "a:clrScheme");
  if (!clrScheme) return;
  for (const tag of ["dk1", "dk2", "lt1", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]) {
    const inner = xmlInner(clrScheme, `a:${tag}`);
    if (inner) {
      let m = inner.match(/<a:srgbClr val="([A-Fa-f0-9]{6})"/);
      if (m) { colors[tag] = `#${m[1]}`; continue; }
      m = inner.match(/<a:sysClr[^>]*lastClr="([A-Fa-f0-9]{6})"/);
      if (m) { colors[tag] = `#${m[1]}`; }
    }
  }
  colors.tx1 = colors.dk1; colors.tx2 = colors.dk2;
  colors.bg1 = colors.lt1; colors.bg2 = colors.lt2;
  _viewerTheme = colors;

  // Parse font scheme
  _viewerMajorFont = "";
  _viewerMinorFont = "";
  try {
    const fontScheme = xmlInner(themeXml, "a:fontScheme");
    if (fontScheme) {
      const majorFont = xmlInner(fontScheme, "a:majorFont");
      const minorFont = xmlInner(fontScheme, "a:minorFont");
      if (majorFont) {
        const m = majorFont.match(/<a:latin typeface="([^"]+)"/);
        if (m) _viewerMajorFont = m[1];
      }
      if (minorFont) {
        const m = minorFont.match(/<a:latin typeface="([^"]+)"/);
        if (m) _viewerMinorFont = m[1];
      }
    }
  } catch { /* non-fatal */ }

  // Parse fill styles from theme fmtScheme
  _viewerBgFillStyles = [];
  _viewerFillStyles = [];
  _viewerLineStyles = [];
  try {
    const extractFills = (xml: string) => {
      const fills = xml.match(/<a:(solidFill|gradFill|pattFill|blipFill)[\s>][\s\S]*?<\/a:\1>/g) || [];
      return [...fills];
    };
    const bgFillLst = xmlInner(themeXml, "a:bgFillStyleLst");
    if (bgFillLst) _viewerBgFillStyles = extractFills(bgFillLst);
    const fillLst = xmlInner(themeXml, "a:fillStyleLst");
    if (fillLst) _viewerFillStyles = extractFills(fillLst);
    const lineLst = xmlInner(themeXml, "a:lnStyleLst");
    if (lineLst) _viewerLineStyles = lineLst.match(/<a:ln\b[^>]*(?:\/>|>[\s\S]*?<\/a:ln>)/g) || [];
  } catch { /* non-fatal */ }
}

function viewerApplyLum(hex: string, mod: number, off: number, tint?: number, shade?: number): string {
  let r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  if (tint !== undefined) { r = Math.round(r + (255 - r) * (1 - tint)); g = Math.round(g + (255 - g) * (1 - tint)); b = Math.round(b + (255 - b) * (1 - tint)); }
  if (shade !== undefined) { r = Math.round(r * shade); g = Math.round(g * shade); b = Math.round(b * shade); }
  if (mod !== 1 || off !== 0) { r = Math.round(Math.min(255, Math.max(0, r * mod + 255 * off))); g = Math.round(Math.min(255, Math.max(0, g * mod + 255 * off))); b = Math.round(Math.min(255, Math.max(0, b * mod + 255 * off))); }
  return [r, g, b].map(v => Math.min(255, Math.max(0, v)).toString(16).padStart(2, "0")).join("");
}

/** Resolve font typeface — handles theme font references +mj-lt, +mn-lt */
function resolveFont(typeface: string | null): string | undefined {
  if (!typeface) return undefined;
  if (typeface === "+mj-lt" || typeface === "+mj-ea" || typeface === "+mj-cs") return _viewerMajorFont || undefined;
  if (typeface === "+mn-lt" || typeface === "+mn-ea" || typeface === "+mn-cs") return _viewerMinorFont || undefined;
  return typeface;
}

function parseColor(xml: string): string | null {
  let m = xml.match(/<a:srgbClr val="([A-Fa-f0-9]{6})"/);
  if (m) {
    const lumMod = xml.match(/<a:lumMod val="(\d+)"/);
    const lumOff = xml.match(/<a:lumOff val="(\d+)"/);
    let hex = m[1];
    if (lumMod || lumOff) hex = viewerApplyLum(hex, lumMod ? parseInt(lumMod[1], 10) / 100000 : 1, lumOff ? parseInt(lumOff[1], 10) / 100000 : 0);
    return `#${hex}`;
  }
  // System color
  m = xml.match(/<a:sysClr[^>]*lastClr="([A-Fa-f0-9]{6})"/);
  if (m) return `#${m[1]}`;
  m = xml.match(/<a:sysClr val="([^"]+)"/);
  if (m) {
    const sysColors: Record<string, string> = { windowText: "#000000", window: "#ffffff", highlight: "#0078d4", highlightText: "#ffffff" };
    return sysColors[m[1]] || "#000000";
  }
  m = xml.match(/<a:schemeClr val="([^"]+)"/);
  if (m) {
    const base = _viewerTheme[m[1]] || _viewerDefaultScheme[m[1]] || "#57534e";
    const lumMod = xml.match(/<a:lumMod val="(\d+)"/);
    const lumOff = xml.match(/<a:lumOff val="(\d+)"/);
    const tint = xml.match(/<a:tint val="(\d+)"/);
    const shade = xml.match(/<a:shade val="(\d+)"/);
    if (lumMod || lumOff || tint || shade) {
      return `#${viewerApplyLum(base.replace("#", ""),
        lumMod ? parseInt(lumMod[1], 10) / 100000 : 1,
        lumOff ? parseInt(lumOff[1], 10) / 100000 : 0,
        tint ? parseInt(tint[1], 10) / 100000 : undefined,
        shade ? parseInt(shade[1], 10) / 100000 : undefined,
      )}`;
    }
    return base;
  }
  return null;
}

/** Resolve a <p:bgRef> or <p:bg> to background color/gradient. */
function parseBgFromXml(bgXml: string): { color?: string; grad?: PptxSlide["bgGrad"]; imgRId?: string } {
  const result: { color?: string; grad?: PptxSlide["bgGrad"]; imgRId?: string } = {};

  // Check for <p:bgPr> with explicit fill
  const bgPr = xmlInner(bgXml, "p:bgPr");
  if (bgPr) {
    result.color = parseColor(bgPr) || undefined;
    result.grad = parseGradient(bgPr);
    const blip = bgPr.match(/<a:blip r:embed="([^"]+)"/);
    if (blip) result.imgRId = blip[1];
    if (result.color || result.grad || result.imgRId) return result;
  }

  // Check for <p:bgRef idx="N"> — references theme bgFillStyleLst
  const bgRef = xmlInner(bgXml, "p:bgRef");
  if (bgRef) {
    const idxMatch = bgRef.match(/idx="(\d+)"/);
    const idx = idxMatch ? parseInt(idxMatch[1], 10) : 0;

    // The <a:schemeClr> inside bgRef provides the color to apply to the fill style
    const refColor = parseColor(bgRef);

    // idx 1001+ → bgFillStyleLst (1001→0, 1002→1, ...)
    // idx 1-999 → fillStyleLst (1→0, 2→1, ...)
    const styleList = idx >= 1001 ? _viewerBgFillStyles : _viewerFillStyles;
    const fillIdx = idx >= 1001 ? idx - 1001 : idx - 1;

    if (fillIdx >= 0 && fillIdx < styleList.length) {
      const fillXml = styleList[fillIdx];
      if (fillXml.includes("<a:solidFill")) {
        result.color = refColor || parseColor(fillXml) || undefined;
      } else if (fillXml.includes("<a:gradFill")) {
        result.grad = parseGradient(fillXml, refColor || undefined);
        if (!result.grad && refColor) result.color = refColor;
      }
    }
    if (!result.color && !result.grad && refColor) {
      result.color = refColor;
    }
  }

  // Fallback: try direct color/gradient in the <p:bg> block
  if (!result.color && !result.grad) {
    result.color = parseColor(bgXml) || undefined;
    result.grad = parseGradient(bgXml);
    const blip = bgXml.match(/<a:blip r:embed="([^"]+)"/);
    if (blip) result.imgRId = blip[1];
  }

  return result;
}

function parseGradient(xml: string, phClrOverride?: string): PptxShape["gradFill"] | undefined {
  const gradXml = xmlInner(xml, "a:gradFill");
  if (!gradXml) return undefined;
  const stops: { pos: number; color: string; alpha: number }[] = [];
  const gsMatches = gradXml.match(/<a:gs[\s>][\s\S]*?<\/a:gs>/g) || [];
  for (const gs of gsMatches) {
    const pos = parseInt(xmlAttr(gs, "pos") || "0", 10) / 1000;
    // If this stop uses phClr (placeholder color), substitute with the override
    const usesPhClr = /schemeClr val="phClr"/.test(gs);
    let color: string;
    if (usesPhClr && phClrOverride) {
      // Apply tint/shade/lumMod/lumOff modifiers to the override color
      const lumMod = gs.match(/<a:lumMod val="(\d+)"/);
      const lumOff = gs.match(/<a:lumOff val="(\d+)"/);
      const tint = gs.match(/<a:tint val="(\d+)"/);
      const shade = gs.match(/<a:shade val="(\d+)"/);
      if (lumMod || lumOff || tint || shade) {
        color = `#${viewerApplyLum(
          phClrOverride.replace("#", ""),
          lumMod ? parseInt(lumMod[1], 10) / 100000 : 1,
          lumOff ? parseInt(lumOff[1], 10) / 100000 : 0,
          tint ? parseInt(tint[1], 10) / 100000 : undefined,
          shade ? parseInt(shade[1], 10) / 100000 : undefined,
        )}`;
      } else {
        color = phClrOverride;
      }
    } else {
      color = parseColor(gs) || "#000000";
    }
    const alphaM = gs.match(/<a:alpha val="(\d+)"/);
    const alpha = alphaM ? parseInt(alphaM[1], 10) / 100000 : 1;
    stops.push({ pos, color, alpha });
  }
  const angMatch = gradXml.match(/<a:lin ang="(\d+)"/);
  const angle = angMatch ? parseInt(angMatch[1], 10) / 60000 : 0;
  // Detect radial/path gradient
  const isRadial = /<a:path\s/.test(gradXml);
  return stops.length > 0 ? { angle: isRadial ? -1 : angle, stops } : undefined;
}

export function gradientToCss(g: { angle: number; stops: { pos: number; color: string; alpha: number }[] }): string {
  const stopStr = g.stops
    .map(s => {
      const r = parseInt(s.color.slice(1, 3), 16);
      const gv = parseInt(s.color.slice(3, 5), 16);
      const b = parseInt(s.color.slice(5, 7), 16);
      return `rgba(${r},${gv},${b},${s.alpha}) ${s.pos}%`;
    })
    .join(", ");
  // Radial gradient (angle === -1 sentinel)
  if (g.angle === -1) return `radial-gradient(ellipse at center, ${stopStr})`;
  // PPTX: 0°=right, CSS: 0°=up → add 90°
  return `linear-gradient(${g.angle + 90}deg, ${stopStr})`;
}

function parseXfrm(spXml: string): { x: number; y: number; w: number; h: number } | null {
  const xfrm = xmlInner(spXml, "a:xfrm");
  if (!xfrm) return null;
  const offM = xfrm.match(/<a:off x="(\d+)" y="(\d+)"/);
  const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/);
  if (!offM || !extM) return null;
  return {
    x: emu2pctX(parseInt(offM[1], 10)),
    y: emu2pctY(parseInt(offM[2], 10)),
    w: emu2pctX(parseInt(extM[1], 10)),
    h: emu2pctY(parseInt(extM[2], 10)),
  };
}

function parseTextRuns(spXml: string, inheritedLevels?: PresentationTextLevelStyle[]): PptxShape["texts"] {
  const texts: PptxShape["texts"] = [];
  const paras = spXml.match(/<a:p>[\s\S]*?<\/a:p>/g) || spXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [];
  for (const para of paras) {
    const pPr = xmlElementAny(para, "a:pPr");
    const lvl = pPr ? parseInt(xmlStartTagAttr(pPr, "lvl") || "0", 10) : 0;
    const inherited: PresentationTextLevelStyle = inheritedLevels?.[Math.max(0, Math.min(8, lvl))] || {};
    const align = (pPr ? (xmlStartTagAttr(pPr, "algn") || undefined) : undefined) ?? inherited.align;
    const marL = pPr ? xmlStartTagAttr(pPr, "marL") : null;
    const indent = marL != null ? parseInt(marL, 10) / 12700 : inherited.indent ?? (lvl > 0 ? lvl * 18 : undefined);
    const marR = pPr ? xmlStartTagAttr(pPr, "marR") : null;
    const indentRight = marR != null ? parseInt(marR, 10) / 12700 : inherited.indentRight;
    const firstLineIndent = pPr ? xmlStartTagAttr(pPr, "indent") : null;
    const hanging = firstLineIndent != null ? parseInt(firstLineIndent, 10) / 12700 : inherited.hanging;

    let bullet: string | undefined;
    const ownBullet = Boolean(pPr && /<a:bu(?:Char|AutoNum|None)\b/.test(pPr));
    const hasText = /<a:t[\s>][^<]/.test(para);
    if (!ownBullet && inheritedLevels && hasText && inherited.bullet !== undefined) {
      // Inherited bullets (e.g. the master's body style) apply to text lines;
      // null is an inherited buNone.
      bullet = inherited.bullet || undefined;
    } else if (pPr) {
      const buChar = pPr.match(/<a:buChar char="([^"]+)"/);
      if (buChar) bullet = buChar[1];
      else if (pPr.includes("<a:buAutoNum")) {
        // Determine numbering type — we'll use a placeholder; actual number is per-slide counter
        const autoNumType = (pPr.match(/<a:buAutoNum type="([^"]+)"/) || [])[1] || "arabicPeriod";
        if (autoNumType.startsWith("alpha")) bullet = "a.";
        else if (autoNumType.startsWith("roman")) bullet = "i.";
        else bullet = "#."; // resolved to actual number during rendering
      }
      else if (!pPr.includes("<a:buNone") && lvl > 0) bullet = "\u2022";
    }

    // Line/paragraph spacing
    let lineSpacing: number | undefined, spaceBefore: number | undefined, spaceAfter: number | undefined;
    if (pPr) {
      const lnSpc = xmlInner(pPr, "a:lnSpc");
      if (lnSpc) {
        const spcPct = lnSpc.match(/<a:spcPct val="(\d+)"/);
        if (spcPct) lineSpacing = parseInt(spcPct[1], 10) / 100000;
        const spcPts = lnSpc.match(/<a:spcPts val="(\d+)"/);
        if (spcPts) lineSpacing = parseInt(spcPts[1], 10) / 100 / 12; // approximate pt → line multiplier
      }
      const spcBef = xmlInner(pPr, "a:spcBef");
      if (spcBef) {
        const pts = spcBef.match(/<a:spcPts val="(\d+)"/);
        if (pts) spaceBefore = parseInt(pts[1], 10) / 100;
      }
      const spcAft = xmlInner(pPr, "a:spcAft");
      if (spcAft) {
        const pts = spcAft.match(/<a:spcPts val="(\d+)"/);
        if (pts) spaceAfter = parseInt(pts[1], 10) / 100;
      }
    }

    // Parse default paragraph-level text properties (defRPr and endParaRPr)
    let defFontSize: number | undefined, defColor: string | undefined;
    let defBold = false, defItalic = false, defFontFamily: string | undefined;
    const defRPr = pPr ? xmlElementAny(pPr, "a:defRPr") : null;
    if (defRPr) {
      const szM = xmlAttr(defRPr, "sz");
      if (szM) defFontSize = parseInt(szM, 10) / 100;
      const c = parseColor(defRPr);
      if (c) defColor = c;
      defBold = ["1", "true"].includes(xmlStartTagAttr(defRPr, "b") || "");
      defItalic = ["1", "true"].includes(xmlStartTagAttr(defRPr, "i") || "");
      const latin = defRPr.match(/<a:latin typeface="([^"]+)"/);
      const ea = defRPr.match(/<a:ea typeface="([^"]+)"/);
      defFontFamily = resolveFont(latin?.[1] ?? null) || resolveFont(ea?.[1] ?? null) || defFontFamily;
    }
    const endParaRPr = xmlElementAny(para, "a:endParaRPr");
    if (endParaRPr) {
      const szM = xmlAttr(endParaRPr, "sz");
      if (szM && !defFontSize) defFontSize = parseInt(szM, 10) / 100;
      const c = parseColor(endParaRPr);
      if (c && !defColor) defColor = c;
    }
    // Anything this paragraph leaves unset comes from the inherited level.
    defFontSize ??= inherited.fontSize;
    defColor ??= inherited.color;
    defFontFamily ??= inherited.fontFamily;
    if (!defRPr || xmlStartTagAttr(defRPr, "b") == null) defBold ||= Boolean(inherited.bold);
    if (!defRPr || xmlStartTagAttr(defRPr, "i") == null) defItalic ||= Boolean(inherited.italic);
    lineSpacing ??= inherited.lineSpacing;
    spaceBefore ??= inherited.spaceBefore;
    spaceAfter ??= inherited.spaceAfter;

    // Collect runs with individual formatting
    const tokens = para.match(/<a:r[\s>][\s\S]*?<\/a:r>|<a:br\s*\/>|<a:br[\s>][\s\S]*?<\/a:br>|<a:fld[\s>][\s\S]*?<\/a:fld>/g) || [];
    const runs: PptxTextRun[] = [];
    let paraText = "";
    // Track first run's style for backward-compat paragraph-level props
    let firstBold = defBold, firstItalic = defItalic, firstUnderline = false;
    let firstFontSize = defFontSize, firstColor = defColor, firstFontFamily = defFontFamily;
    let isFirst = true;

    for (const token of tokens) {
      if (token.startsWith("<a:br")) {
        paraText += "\n";
        runs.push({ text: "\n" });
        continue;
      }
      let rBold = defBold, rItalic = defItalic, rUnderline = false;
      let rFontSize = defFontSize, rColor = defColor, rFontFamily = defFontFamily;

      let rStrike = false, rBaseline: number | undefined, rSpacing: number | undefined;
      const rPr = xmlElementAny(token, "a:rPr");
      if (rPr) {
        // Only an explicit b/i attribute overrides the paragraph default.
        const boldAttr = xmlStartTagAttr(rPr, "b");
        const italicAttr = xmlStartTagAttr(rPr, "i");
        if (boldAttr != null) rBold = boldAttr === "1" || boldAttr === "true";
        if (italicAttr != null) rItalic = italicAttr === "1" || italicAttr === "true";
        rUnderline = rPr.includes('u="sng"') || rPr.includes('u="dbl"') || rPr.includes('u="heavy"');
        rStrike = rPr.includes('strike="sngStrike"') || rPr.includes('strike="dblStrike"');
        const szM = xmlAttr(rPr, "sz");
        if (szM) rFontSize = parseInt(szM, 10) / 100;
        const runColor = parseColor(rPr);
        if (runColor) rColor = runColor;
        const latin = rPr.match(/<a:latin typeface="([^"]+)"/);
        const ea = rPr.match(/<a:ea typeface="([^"]+)"/);
        rFontFamily = resolveFont(latin?.[1] ?? null) || resolveFont(ea?.[1] ?? null) || rFontFamily;
        const baselineM = xmlAttr(rPr, "baseline");
        if (baselineM) rBaseline = parseInt(baselineM, 10) / 1000;
        const spcM = xmlAttr(rPr, "spc");
        if (spcM) rSpacing = parseInt(spcM, 10) / 100;
      }
      const tMatch = token.match(/<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/);
      const runText = tMatch ? decodePptxText(tMatch[1]) : "";
      if (!runText) continue;
      paraText += runText;
      runs.push({
        text: runText,
        bold: rBold || undefined,
        italic: rItalic || undefined,
        underline: rUnderline || undefined,
        strikethrough: rStrike || undefined,
        fontSize: rFontSize,
        color: rColor,
        fontFamily: rFontFamily,
        baseline: rBaseline,
        spacing: rSpacing,
      });
      if (isFirst) {
        firstBold = rBold; firstItalic = rItalic; firstUnderline = rUnderline;
        firstFontSize = rFontSize; firstColor = rColor; firstFontFamily = rFontFamily;
        isFirst = false;
      }
    }

    if (paraText.trim() || paraText.includes("\n")) {
      texts.push({
        text: paraText,
        bold: firstBold, italic: firstItalic, underline: firstUnderline,
        fontSize: firstFontSize, color: firstColor, fontFamily: firstFontFamily,
        align: align === "ctr" ? "center" : align === "r" ? "right" : align === "just" ? "justify" : undefined,
        bullet, indent, indentRight, hanging,
        lineSpacing, spaceBefore, spaceAfter,
        runs: runs.length > 1 ? runs : undefined, // only include runs if mixed formatting
      });
    } else {
      texts.push({
        text: "", fontSize: firstFontSize || defFontSize || 12,
        bold: firstBold, italic: firstItalic, underline: firstUnderline,
        color: firstColor, fontFamily: firstFontFamily,
        align: align === "ctr" ? "center" : align === "r" ? "right" : align === "just" ? "justify" : undefined,
        bullet, indent, indentRight, hanging, lineSpacing, spaceBefore, spaceAfter,
      });
    }
  }
  return texts;
}

function viewerParseStroke(xml: string): { color?: string; width?: number; dash?: string } {
  const ln = xmlInner(xml, "a:ln");
  if (!ln || ln.includes("<a:noFill")) return {};
  const color = parseColor(ln);
  const wAttr = xmlAttr(ln, "w");
  const width = wAttr ? parseInt(wAttr, 10) / 12700 : 1;
  return { color: color ? presentationColorWithAlpha(color, ln) : undefined, width: color ? width : undefined, dash: presentationStrokeDash(ln) };
}

function viewerParseTable(xml: string): { rows: { text: string; bold?: boolean; color?: string; fill?: string; gridSpan?: number; vMerge?: boolean }[][]; cols: number; colWidths?: number[] } | null {
  const tbl = xmlInner(xml, "a:tbl");
  if (!tbl) return null;

  // Parse column widths from <a:tblGrid>
  const tblGrid = xmlInner(tbl, "a:tblGrid");
  let colWidths: number[] | undefined;
  if (tblGrid) {
    const gridCols = tblGrid.match(/<a:gridCol[^>]*\/>/g) || [];
    colWidths = gridCols.map(gc => parseInt((gc.match(/w="(\d+)"/) || [])[1] || "0", 10));
  }

  const trMatches = tbl.match(/<a:tr[\s>][\s\S]*?<\/a:tr>/g) || [];
  const rows: { text: string; bold?: boolean; color?: string; fill?: string; gridSpan?: number; vMerge?: boolean }[][] = [];
  let maxCols = 0;
  for (const tr of trMatches) {
    const tcMatches = tr.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/g) || [];
    const row: { text: string; bold?: boolean; color?: string; fill?: string; gridSpan?: number; vMerge?: boolean }[] = [];
    for (const tc of tcMatches) {
      const text = (tc.match(/<a:t>([^<]*)<\/a:t>/g) || []).map(m => m.replace(/<\/?a:t>/g, "")).join(" ");
      const rPr = xmlInner(tc, "a:rPr");
      const tcPr = xmlInner(tc, "a:tcPr");
      const gridSpanM = tc.match(/gridSpan="(\d+)"/);
      const vMerge = tc.includes('vMerge="1"') || tc.includes("hMerge=\"1\"");
      row.push({
        text, bold: rPr?.includes('b="1"'),
        color: rPr ? parseColor(rPr) || undefined : undefined,
        fill: tcPr ? parseColor(tcPr) || undefined : undefined,
        gridSpan: gridSpanM ? parseInt(gridSpanM[1], 10) : undefined,
        vMerge: vMerge || undefined,
      });
    }
    rows.push(row);
    maxCols = Math.max(maxCols, row.length);
  }
  return rows.length > 0 ? { rows, cols: maxCols, colWidths } : null;
}

/** Extract placeholder type and idx from a shape XML */
function parsePlaceholder(spXml: string): { type?: string; idx?: string } | null {
  const phM = spXml.match(/<p:ph([^/>]*)\/?>/);
  if (!phM) return null;
  const type = xmlAttr(phM[0], "type") || undefined;
  const idx = xmlAttr(phM[0], "idx") || undefined;
  return { type, idx };
}

/** Build a map of placeholder key → position from layout/master XML */
function buildPlaceholderMap(xmlStr: string): Map<string, { x: number; y: number; w: number; h: number }> {
  const map = new Map<string, { x: number; y: number; w: number; h: number }>();
  const shapes = xmlStr.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [];
  for (const sp of shapes) {
    const ph = parsePlaceholder(sp);
    if (!ph) continue;
    const pos = parseXfrm(sp);
    if (!pos) continue;
    const key = ph.type || `idx:${ph.idx}`;
    map.set(key, pos);
    if (ph.idx) map.set(`idx:${ph.idx}`, pos);
  }
  return map;
}

interface PptxShapeInheritance {
  layoutXml?: string;
  masterXml?: string;
}

/** `<p:style>` reference (fillRef/lnRef/fontRef): theme list index + its color. */
function viewerShapeStyleReference(spXml: string, kind: "fill" | "ln"): { index: number; color: string | null } | null {
  const style = xmlInner(spXml, "p:style");
  if (!style) return null;
  const reference = style.match(new RegExp(`<a:${kind}Ref\\b[^>]*(?:\\/>|>[\\s\\S]*?<\\/a:${kind}Ref>)`))?.[0];
  if (!reference) return null;
  const index = parseInt(xmlAttr(reference, "idx") || "0", 10);
  return { index, color: parseColor(reference) };
}

const viewerTextStyleResolvers = {
  color: (xml: string) => parseColor(xml) || undefined,
  font: (typeface: string | undefined) => resolveFont(typeface ?? null),
};

function parseShape(
  spXml: string,
  relsMap: Map<string, string>,
  phMap?: Map<string, { x: number; y: number; w: number; h: number }>,
  inheritance?: PptxShapeInheritance,
): PptxShape | null {
  let pos = parseXfrm(spXml);
  // Fallback: resolve position from placeholder map (layout/master inheritance)
  if (!pos && phMap) {
    const ph = parsePlaceholder(spXml);
    if (ph) {
      pos = phMap.get(ph.type || "") || phMap.get(`idx:${ph.idx}`) || null;
      // For body placeholder, also try "body" key from master
      if (!pos && ph.idx === "1") pos = phMap.get("body") || null;
    }
  }
  if (!pos) return null;

  const shape: PptxShape = { ...pos, type: "shape", texts: [] };
  const nonVisualProperties = spXml.match(/<p:cNvPr\b[^>]*\/?\s*>/);
  const altText = nonVisualProperties ? xmlAttr(nonVisualProperties[0], "descr") : null;
  if (altText != null) shape.altText = decodePptxText(altText);

  // Rotation + flip
  const xfrm = xmlInner(spXml, "a:xfrm");
  if (xfrm) {
    const rotAttr = xmlAttr(xfrm, "rot");
    if (rotAttr) shape.rotation = parseInt(rotAttr, 10) / 60000;
    if (xfrm.includes('flipH="1"')) shape.flipH = true;
    if (xfrm.includes('flipV="1"')) shape.flipV = true;
  }

  // Preset geometry
  const geomM = spXml.match(/<a:prstGeom prst="([^"]+)"/);
  if (geomM) shape.presetGeom = geomM[1];

  // Fill — search only in spPr to avoid matching text color fills
  const spPr = xmlInner(spXml, "p:spPr");
  const shapeFillScope = spPr ? presentationShapeFillScope(spPr) : "";
  if (spPr && !shapeFillScope.includes("<a:noFill")) {
    const solidFill = xmlInner(shapeFillScope, "a:solidFill");
    if (solidFill) {
      const color = parseColor(solidFill);
      if (color) shape.fill = presentationColorWithAlpha(color, solidFill);
    }
    shape.gradFill = parseGradient(shapeFillScope);

    // No fill of its own: the shape style's fillRef picks a theme fill,
    // recolored with the reference's color (phClr), like PowerPoint.
    const hasExplicitFill = /<a:(?:solidFill|gradFill|blipFill|pattFill)\b/.test(shapeFillScope);
    if (!hasExplicitFill && !shape.fill && !shape.gradFill) {
      const fillReference = viewerShapeStyleReference(spXml, "fill");
      const themedFill = fillReference && fillReference.index > 0 ? _viewerFillStyles[fillReference.index - 1] : undefined;
      if (themedFill?.includes("<a:solidFill")) {
        const color = fillReference?.color || parseColor(themedFill);
        if (color) shape.fill = color;
      } else if (themedFill?.includes("<a:gradFill")) {
        shape.gradFill = parseGradient(themedFill, fillReference?.color || undefined);
        if (!shape.gradFill && fillReference?.color) shape.fill = fillReference.color;
      }
    }

    // Blip fill (texture/image fill on shapes)
    if (!shape.fill && !shape.gradFill) {
      const blipFill = xmlInner(spPr, "a:blipFill") || xmlInner(spXml, "p:blipFill");
      if (blipFill) {
        const blipM = blipFill.match(/<a:blip r:embed="([^"]+)"/);
        if (blipM) { const u = relsMap.get(blipM[1]); if (u) shape.imgUrl = u; }
      }
    }
  }

  // Stroke/border — scope to spPr
  const stroke = viewerParseStroke(spPr || spXml);
  if (stroke.color) { shape.stroke = stroke.color; shape.strokeWidth = stroke.width; shape.strokeDash = stroke.dash; }
  else {
    // Outline from the shape style's lnRef unless the shape turns it off.
    const ownLine = xmlInner(spPr || "", "a:ln");
    const lineReference = ownLine?.includes("<a:noFill") ? null : viewerShapeStyleReference(spXml, "ln");
    const themedLine = lineReference && lineReference.index > 0 ? _viewerLineStyles[lineReference.index - 1] : undefined;
    const lineColor = lineReference?.color || (themedLine ? parseColor(themedLine) : null);
    if (lineReference && lineColor && !themedLine?.includes("<a:noFill")) {
      const ownWidth = ownLine ? xmlAttr(ownLine, "w") : null;
      const themedWidth = themedLine ? xmlAttr(themedLine, "w") : null;
      shape.stroke = lineColor;
      shape.strokeWidth = parseInt(ownWidth || themedWidth || "9525", 10) / 12700;
      shape.strokeDash = presentationStrokeDash(ownLine || themedLine || "");
    }
  }

  // Fill, stroke and shadow alpha belong to their own colors, never the text.
  const alphaM = spXml.match(/<a:alphaModFix\b[^>]*\bamt="(\d+)"/);
  if (alphaM) shape.opacity = parseInt(alphaM[1], 10) / 100000;

  // Shadow (outer shadow)
  const outerShdw = xmlInner(spXml, "a:outerShdw");
  if (outerShdw) {
    const shdwBlur = parseInt(xmlAttr(outerShdw, "blurRad") || "0", 10) / 12700;
    const shdwDist = parseInt(xmlAttr(outerShdw, "dist") || "0", 10) / 12700;
    const shdwAng = parseInt(xmlAttr(outerShdw, "dir") || "0", 10) / 60000;
    const shdwColor = parseColor(outerShdw) || "#000000";
    const shdwAlpha = outerShdw.match(/<a:alpha val="(\d+)"/) ? parseInt(outerShdw.match(/<a:alpha val="(\d+)"/)![1], 10) / 100000 : 0.4;
    shape.shadow = { blur: shdwBlur, dist: shdwDist, angle: shdwAng, color: shdwColor, alpha: shdwAlpha };
  }

  // Border radius (roundRect preset)
  if (spXml.includes('prst="roundRect"')) {
    const adjM = spXml.match(/name="adj" fmla="val (\d+)"/);
    shape.borderRadius = adjM ? Math.min(50, parseInt(adjM[1], 10) / 1000) : 16.667;
  }

  // Text body properties (vertical alignment + insets)
  const bodyPr = xmlInner(spXml, "a:bodyPr");
  if (bodyPr) {
    const anchor = xmlAttr(bodyPr, "anchor");
    const wrap = xmlAttr(bodyPr, "wrap");
    if (wrap === "none" || wrap === "square") shape.wordWrap = wrap === "square";
    if (anchor === "t") shape.vAlign = "top";
    else if (anchor === "b") shape.vAlign = "bottom";
    else if (anchor === "ctr") shape.vAlign = "middle";
    // Text insets (EMU → % of slide)
    const lIns = xmlAttr(bodyPr, "lIns");
    const tIns = xmlAttr(bodyPr, "tIns");
    const rIns = xmlAttr(bodyPr, "rIns");
    const bIns = xmlAttr(bodyPr, "bIns");
    if (lIns || tIns || rIns || bIns) {
      shape.padding = {
        l: lIns ? parseInt(lIns, 10) / 12700 : 7,
        t: tIns ? parseInt(tIns, 10) / 12700 : 4,
        r: rIns ? parseInt(rIns, 10) / 12700 : 7,
        b: bIns ? parseInt(bIns, 10) / 12700 : 4,
      };
    }
  }

  // Text
  // Placeholder text inherits size/alignment/bullets from the layout and
  // master (lstStyle, title/body/other styles); fontRef supplies color/font.
  let inheritedLevels: PresentationTextLevelStyle[] | undefined;
  if (inheritance && /<a:t[\s>]/.test(spXml)) {
    try {
      inheritedLevels = presentationInheritedTextStyleLevels(
        spXml,
        inheritance.layoutXml,
        inheritance.masterXml,
        viewerTextStyleResolvers,
      );
    } catch { /* inheritance is best effort */ }
  }
  shape.texts = parseTextRuns(spXml, inheritedLevels);

  // Line / connector detection
  if (spXml.startsWith("<p:cxnSp") || shape.presetGeom === "line" || shape.presetGeom === "straightConnector1") {
    shape.type = "line";
  }

  // Image (p:pic)
  if (!shape.imgUrl) {
    const blipM = spXml.match(/<a:blip r:embed="([^"]+)"/);
    if (blipM) {
      const imgUrl = relsMap.get(blipM[1]);
      if (imgUrl) { shape.imgUrl = imgUrl; shape.type = "image"; }
    }
  }
  if (shape.imgUrl && /^\s*<p:pic\b/.test(spXml)) shape.type = "image";
  shape.videoUrl = presentationVideoSource(spXml, relsMap);
  // Image cropping (srcRect)
  if (shape.imgUrl) {
    const srcRect = spXml.match(/<a:srcRect\s+([^/]*)\/>/);
    if (srcRect) {
      const attrs = srcRect[1];
      const l = parseInt((attrs.match(/l="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      const t = parseInt((attrs.match(/t="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      const r = parseInt((attrs.match(/r="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      const b = parseInt((attrs.match(/b="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      if (l || t || r || b) shape.imgCrop = { l, t, r, b };
    }
  }

  return shape;
}

function mapPptxShapeToSlide(shape: PptxShape, toSlide: PptxEmuMapper) {
  const mapped = toSlide({
    x: (shape.x / 100) * SLIDE_W,
    y: (shape.y / 100) * SLIDE_H,
    w: (shape.w / 100) * SLIDE_W,
    h: (shape.h / 100) * SLIDE_H,
  });
  shape.x = emu2pctX(mapped.x);
  shape.y = emu2pctY(mapped.y);
  shape.w = emu2pctX(mapped.w);
  shape.h = emu2pctY(mapped.h);
}

/** Tables render natively; charts, SmartArt and OLE get a labeled placeholder. */
function parseGraphicFrameShape(frameXml: string, relsMap: Map<string, string>): PptxShape | null {
  const rect = pptxGraphicFrameRect(frameXml);
  if (!rect || rect.w <= 0 || rect.h <= 0) return null;
  const base: PptxShape = {
    x: emu2pctX(rect.x), y: emu2pctY(rect.y),
    w: emu2pctX(rect.w), h: emu2pctY(rect.h),
    texts: [],
  };
  const kind = pptxGraphicFrameKind(frameXml);
  if (kind === "table") {
    const table = viewerParseTable(frameXml);
    return table
      ? { ...base, type: "table", tableRows: table.rows, tableCols: table.cols, tableColWidths: table.colWidths }
      : null;
  }
  if (kind === "ole") {
    // OLE objects usually carry a preview picture; show it when resolvable.
    const blip = frameXml.match(/<a:blip\b[^>]*\br:embed="([^"]+)"/);
    const imgUrl = blip ? relsMap.get(blip[1]) : undefined;
    if (imgUrl) return { ...base, type: "image", imgUrl };
  }
  return {
    ...base,
    type: "placeholder",
    placeholderKind: kind === "chart" ? "chart" : kind === "smartart" ? "smartart" : "object",
  };
}

const PPTX_MEDIA_REF_PREFIX = "pptx-media:";

// The PPTX parser helpers keep slide dimensions and theme data in module
// state. Serialize parses so navigating between presentations cannot mix the
// state of an older, still-awaiting parse into the current one.
let pptxParseQueue: Promise<void> = Promise.resolve();

export async function parsePptx(buf: ArrayBuffer, options?: PptxParseOptions): Promise<PptxSlide[]> {
  const previousParse = pptxParseQueue;
  let releaseParse!: () => void;
  pptxParseQueue = new Promise<void>((resolve) => { releaseParse = resolve; });
  await previousParse;
  try {
    if (options?.isCancelled?.()) return [];
    return await parsePptxUnlocked(buf, options);
  } finally {
    releaseParse();
  }
}

async function parsePptxUnlocked(buf: ArrayBuffer, options?: PptxParseOptions): Promise<PptxSlide[]> {
  // Every parse owns a fresh set of module-scoped parser defaults. The queue
  // prevents concurrent mutation; this reset prevents a theme-less file from
  // inheriting slide size, colors, fonts, or fill styles from the prior file.
  SLIDE_W = 12192000;
  SLIDE_H = 6858000;
  _viewerTheme = { ..._viewerDefaultScheme };
  _viewerMajorFont = "";
  _viewerMinorFont = "";
  _viewerBgFillStyles = [];
  _viewerFillStyles = [];
  _viewerLineStyles = [];

  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(buf);
  const slides: PptxSlide[] = [];

  // Read slide size and slide order from presentation.xml
  let orderedSlideRIds: string[] = [];
  let presRelsMap = new Map<string, string>(); // rId → ppt/slides/slideN.xml
  try {
    const presEntry = zip.file("ppt/presentation.xml");
    if (presEntry) {
      const presXml = await presEntry.async("text");
      const sldSz = presXml.match(/<p:sldSz[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
      if (sldSz) {
        SLIDE_W = parseInt(sldSz[1], 10);
        SLIDE_H = parseInt(sldSz[2], 10);
      }
      // Extract slide order from <p:sldIdLst>
      const sldIdLst = presXml.match(/<p:sldId[^/]*\/>/g) || [];
      orderedSlideRIds = sldIdLst.map(s => (s.match(/r:id="([^"]+)"/) || [])[1]).filter(Boolean);
    }
    // Map rId → slide path from presentation.xml.rels
    const presRelsEntry = zip.file("ppt/_rels/presentation.xml.rels");
    if (presRelsEntry) {
      const presRelsXml = await presRelsEntry.async("text");
      for (const rel of (presRelsXml.match(/<Relationship[^>]*\/>/g) || [])) {
        const id = xmlAttr(rel, "Id");
        const target = xmlAttr(rel, "Target");
        if (id && target) {
          const resolved = resolvePresentationPartTarget("ppt/presentation.xml", target);
          presRelsMap.set(id, resolved);
        }
      }
    }
  } catch { /* use default 16:9, file-number order */ }

  // Parse theme (non-fatal)
  try {
    const themeEntry = zip.file("ppt/theme/theme1.xml");
    if (themeEntry) parseViewerTheme(await themeEntry.async("text"));
    else _viewerTheme = _viewerDefaultScheme;
  } catch { _viewerTheme = _viewerDefaultScheme; }

  // Determine slide file order: prefer presentation.xml order, fall back to filename sort
  let slideFiles: string[];
  let hasExplicitSlideOrder = orderedSlideRIds.length > 0 && presRelsMap.size > 0;
  if (hasExplicitSlideOrder) {
    slideFiles = orderedSlideRIds.map(rId => presRelsMap.get(rId)).filter((p): p is string => !!p && /slide\d+\.xml$/.test(p));
  } else {
    slideFiles = Object.keys(zip.files)
      .filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n));
  }
  if (slideFiles.length === 0) {
    slideFiles = Object.keys(zip.files).filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n));
    hasExplicitSlideOrder = false;
  }
  if (!hasExplicitSlideOrder) slideFiles.sort((a, b) => {
      const na = parseInt(a.match(/slide(\d+)/)?.[1] || "0", 10);
      const nb = parseInt(b.match(/slide(\d+)/)?.[1] || "0", 10);
      return na - nb;
    });

  const isCancelled = () => Boolean(options?.isCancelled?.());
  if (isCancelled()) return [];

  // Media is extracted lazily: relationships resolve to part references and
  // only parts that a parsed slide actually uses become blob URLs (videos
  // included), instead of decompressing every ppt/media/* entry up front.
  const mediaParts = new Set(
    Object.keys(zip.files).filter((name) => name.startsWith("ppt/media/") && !zip.files[name].dir),
  );
  const mediaRef = (part: string) => `${PPTX_MEDIA_REF_PREFIX}${part}`;
  const mediaUrls = new Map<string, Promise<string | undefined>>();
  const resolveMedia = (reference: string | undefined): Promise<string | undefined> => {
    if (!reference || !reference.startsWith(PPTX_MEDIA_REF_PREFIX)) return Promise.resolve(reference);
    const part = reference.slice(PPTX_MEDIA_REF_PREFIX.length);
    let pending = mediaUrls.get(part);
    if (!pending) {
      pending = (async () => {
        if (isCancelled()) return undefined;
        try {
          const entry = zip.file(part);
          if (!entry) return undefined;
          const data = await entry.async("blob");
          if (isCancelled()) return undefined;
          const mediaUrl = URL.createObjectURL(new Blob([data], { type: presentationMediaMime(part) }));
          options?.onObjectUrl?.(mediaUrl);
          return mediaUrl;
        } catch {
          return undefined; // skip bad media
        }
      })();
      mediaUrls.set(part, pending);
    }
    return pending;
  };
  const resolveSlideMedia = async (slide: PptxSlide) => {
    if (slide.bgImgUrl) slide.bgImgUrl = await resolveMedia(slide.bgImgUrl);
    for (const shape of slide.shapes) {
      if (isCancelled()) return;
      if (shape.imgUrl) shape.imgUrl = await resolveMedia(shape.imgUrl);
      if (shape.videoUrl) shape.videoUrl = await resolveMedia(shape.videoUrl);
    }
  };

  // Pre-cache layouts and slide masters (non-fatal)
  const layoutCache = new Map<string, string>();
  const masterCache = new Map<string, string>();
  const layoutToMasterPath = new Map<string, string>();
  const layoutRelsCache = new Map<string, Map<string, string>>();
  const masterRelsCache = new Map<string, Map<string, string>>();
  try {
    for (const name of Object.keys(zip.files)) {
      if (isCancelled()) return [];
      if (/^ppt\/slideLayouts\/slideLayout\d+\.xml$/.test(name)) {
        const e = zip.file(name);
        if (e) layoutCache.set(name, await e.async("text"));
      }
      if (/^ppt\/slideMasters\/slideMaster\d+\.xml$/.test(name)) {
        const e = zip.file(name);
        if (e) masterCache.set(name, await e.async("text"));
      }
    }
    // Build layout → master mapping from layout rels
    for (const layoutPath of layoutCache.keys()) {
      if (isCancelled()) return [];
      try {
        const lrp = presentationRelationshipsPart(layoutPath);
        const lre = zip.file(lrp);
        if (lre) {
          const lrXml = await lre.async("text");
          const layoutRels = new Map<string, string>();
          for (const rel of (lrXml.match(/<Relationship[^>]*\/>/g) || [])) {
            const id = xmlAttr(rel, "Id");
            const target = xmlAttr(rel, "Target");
            const type = xmlAttr(rel, "Type");
            if (target) {
              const resolved = resolvePresentationPartTarget(layoutPath, target);
              if (type?.includes("slideMaster")) layoutToMasterPath.set(layoutPath, resolved);
              if (id && mediaParts.has(resolved)) layoutRels.set(id, mediaRef(resolved));
            }
          }
          layoutRelsCache.set(layoutPath, layoutRels);
        }
      } catch { /* non-fatal */ }
    }
    // Pre-cache master rels for media resolution
    for (const masterPath of masterCache.keys()) {
      if (isCancelled()) return [];
      try {
        const mrp = presentationRelationshipsPart(masterPath);
        const mre = zip.file(mrp);
        if (mre) {
          const mrXml = await mre.async("text");
          const masterRels = new Map<string, string>();
          for (const rel of (mrXml.match(/<Relationship[^>]*\/>/g) || [])) {
            const id = xmlAttr(rel, "Id");
            const target = xmlAttr(rel, "Target");
            if (id && target) {
              const resolved = resolvePresentationPartTarget(masterPath, target);
              if (mediaParts.has(resolved)) masterRels.set(id, mediaRef(resolved));
            }
          }
          masterRelsCache.set(masterPath, masterRels);
        }
      } catch { /* non-fatal */ }
    }
  } catch { /* layouts/masters are optional enhancement */ }

  for (const slidePath of slideFiles) {
    if (isCancelled()) return [];
    try {
      const entry = zip.file(slidePath);
      if (!entry) continue;
      let xml = await entry.async("text");
      // Unwrap <mc:AlternateContent> — prefer <mc:Choice> (modern), fall back to <mc:Fallback>
      xml = xml.replace(/<mc:AlternateContent[\s>][\s\S]*?<\/mc:AlternateContent>/g, (block) => {
        const choice = block.match(/<mc:Choice[\s>]([\s\S]*?)<\/mc:Choice>/);
        if (choice && choice[1].trim()) return choice[1];
        const fallback = block.match(/<mc:Fallback[\s>]([\s\S]*?)<\/mc:Fallback>/);
        return fallback ? fallback[1] : "";
      });
      const relsMap = new Map<string, string>();
      let layoutPath: string | undefined;
      try {
        const relsEntry = zip.file(presentationRelationshipsPart(slidePath));
        if (relsEntry) {
          const relsXml = await relsEntry.async("text");
          const relMatches = relsXml.match(/<Relationship[^>]*\/>/g) || [];
          for (const rel of relMatches) {
            const id = xmlAttr(rel, "Id");
            const target = xmlAttr(rel, "Target");
            const type = xmlAttr(rel, "Type");
            if (id && target) {
              const resolved = resolvePresentationPartTarget(slidePath, target);
              if (mediaParts.has(resolved)) relsMap.set(id, mediaRef(resolved));
              if (type && type.includes("slideLayout")) layoutPath = resolved;
            }
          }
        }
        // Also resolve media from layout rels
        if (layoutPath) {
          const layoutRelsPath = presentationRelationshipsPart(layoutPath);
          const lre = zip.file(layoutRelsPath);
          if (lre) {
            const lrXml = await lre.async("text");
            for (const rel of (lrXml.match(/<Relationship[^>]*\/>/g) || [])) {
              const id = xmlAttr(rel, "Id");
              const target = xmlAttr(rel, "Target");
              if (id && target) {
                const resolved = resolvePresentationPartTarget(layoutPath, target);
                if (mediaParts.has(resolved) && !relsMap.has(id)) relsMap.set(id, mediaRef(resolved));
              }
            }
          }
        }
      } catch { /* rels parsing non-fatal */ }

      const slide: PptxSlide = { shapes: [] };

      // Build placeholder position map: master → layout → slide (later overrides earlier)
      const phMap = new Map<string, { x: number; y: number; w: number; h: number }>();
      try {
        if (layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            for (const [k, v] of buildPlaceholderMap(masterCache.get(masterPath)!)) phMap.set(k, v);
          }
          if (layoutCache.has(layoutPath)) {
            for (const [k, v] of buildPlaceholderMap(layoutCache.get(layoutPath)!)) phMap.set(k, v);
          }
        }
      } catch { /* placeholder map non-fatal */ }

      // Background — slide → layout → slide master fallback
      try {
        let bgXml = xmlInner(xml, "p:bg");
        let bgRels = relsMap;
        if (!bgXml && layoutPath && layoutCache.has(layoutPath)) {
          bgXml = xmlInner(layoutCache.get(layoutPath)!, "p:bg");
          bgRels = layoutRelsCache.get(layoutPath) || new Map<string, string>();
        }
        if (!bgXml && layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            bgXml = xmlInner(masterCache.get(masterPath)!, "p:bg");
            bgRels = masterRelsCache.get(masterPath) || new Map<string, string>();
          }
        }
        if (bgXml) {
          const bgResult = parseBgFromXml(bgXml);
          if (bgResult.color) slide.bg = bgResult.color;
          if (bgResult.grad) slide.bgGrad = bgResult.grad;
          if (bgResult.imgRId && bgRels.has(bgResult.imgRId)) slide.bgImgUrl = bgRels.get(bgResult.imgRId);
        }
      } catch { /* bg parsing non-fatal */ }

      // Layout/master parts the slide's text styles inherit from.
      const slideMasterPath = layoutPath ? layoutToMasterPath.get(layoutPath) : undefined;
      const shapeInheritance: PptxShapeInheritance = {
        layoutXml: layoutPath ? layoutCache.get(layoutPath) : undefined,
        masterXml: slideMasterPath ? masterCache.get(slideMasterPath) : undefined,
      };

      // Walk the shape tree in document order (= z-order). Groups are
      // expanded recursively with their composed transforms, and graphic
      // frames (tables, charts, SmartArt, OLE) keep their stacking position.
      try {
        const spTree = xml.match(/<p:spTree[\s>][\s\S]*<\/p:spTree>/)?.[0] || xml;
        for (const element of flattenPptxShapeTree(spTree)) {
          try {
            const shape = element.kind === "graphicFrame"
              ? parseGraphicFrameShape(element.xml, relsMap)
              : parseShape(element.xml, relsMap, phMap, shapeInheritance);
            if (!shape) continue;
            if (element.depth > 0) mapPptxShapeToSlide(shape, element.toSlide);
            slide.shapes.push(shape);
          } catch { /* individual shape parse non-fatal */ }
        }
      } catch { /* shape tree extraction non-fatal */ }

      // Layout and master decorative shapes render behind slide content,
      // master first, each in its own document order.
      const inheritedDecorations = (partXml: string, partRels: Map<string, string>) => {
        const decorations: PptxShape[] = [];
        const tree = partXml.match(/<p:spTree[\s>][\s\S]*<\/p:spTree>/)?.[0] || partXml;
        for (const element of flattenPptxShapeTree(tree)) {
          if (element.kind !== "sp" && element.kind !== "pic") continue;
          if (element.xml.includes("<p:ph")) continue;
          try {
            const shape = parseShape(element.xml, partRels);
            if (!shape || !(shape.fill || shape.gradFill || shape.imgUrl || shape.stroke)) continue;
            if (element.depth > 0) mapPptxShapeToSlide(shape, element.toSlide);
            decorations.push(shape);
          } catch { /* decorative shape non-fatal */ }
        }
        return decorations;
      };
      let layoutDecorations: PptxShape[] = [];
      let masterDecorations: PptxShape[] = [];
      try {
        if (layoutPath && layoutCache.has(layoutPath)) {
          layoutDecorations = inheritedDecorations(
            layoutCache.get(layoutPath)!,
            layoutRelsCache.get(layoutPath) || new Map<string, string>(),
          );
        }
      } catch { /* layout shapes non-fatal */ }
      try {
        if (layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            masterDecorations = inheritedDecorations(
              masterCache.get(masterPath)!,
              masterRelsCache.get(masterPath) || new Map<string, string>(),
            );
          }
        }
      } catch { /* master shapes non-fatal */ }
      slide.shapes = [...masterDecorations, ...layoutDecorations, ...slide.shapes];

      slide.aspectRatio = `${SLIDE_W}/${SLIDE_H}`;
      slide.heightPoints = SLIDE_H / 12700;
      slide.theme = {
        colors: { ..._viewerTheme },
        majorFont: _viewerMajorFont || undefined,
        minorFont: _viewerMinorFont || undefined,
      };
      await resolveSlideMedia(slide);
      if (isCancelled()) return [];
      slides.push(slide);
    } catch {
      // If a single slide fails, add empty slide placeholder and continue
      slides.push({ shapes: [] });
    }
  }

  return slides;
}
