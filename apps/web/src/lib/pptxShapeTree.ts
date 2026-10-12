/**
 * Document-order walk of a PresentationML shape tree for the read-only viewer.
 *
 * Regex matching like `<p:grpSp…?</p:grpSp>` stops at the first closing tag
 * and therefore splits nested groups. This scanner balances the drawable
 * element tags instead, keeps their document order (which is their z-order),
 * and composes nested group transforms so every leaf maps into slide EMUs.
 */

export type PptxTreeElementKind = "sp" | "pic" | "cxnSp" | "graphicFrame" | "grpSp";

export interface PptxTreeElement {
  kind: PptxTreeElementKind;
  xml: string;
}

/** Rectangle in EMU. */
export interface PptxEmuRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type PptxEmuMapper = (rect: PptxEmuRect) => PptxEmuRect;

export interface PptxGroupTransform {
  offX: number;
  offY: number;
  extCx: number;
  extCy: number;
  chOffX: number;
  chOffY: number;
  chExtCx: number;
  chExtCy: number;
}

export interface PptxFlatElement {
  kind: Exclude<PptxTreeElementKind, "grpSp">;
  xml: string;
  /** Maps the element's own xfrm rectangle (EMU) into slide EMU. */
  toSlide: PptxEmuMapper;
  /** Nesting depth; 0 for direct spTree children. */
  depth: number;
}

export type PptxGraphicFrameKind = "table" | "chart" | "smartart" | "ole" | "other";

const DRAWABLE_TAG_SOURCE = "<(\\/?)p:(sp|pic|cxnSp|graphicFrame|grpSp)(?=[\\s/>])[^>]*?(\\/?)>";

export const identityPptxEmuMapper: PptxEmuMapper = (rect) => rect;

/** Direct drawable children of `xml`, in document order, with balanced tags. */
export function scanPptxShapeTree(xml: string): PptxTreeElement[] {
  const elements: PptxTreeElement[] = [];
  const stack: Array<{ kind: PptxTreeElementKind; start: number }> = [];
  const drawableTag = new RegExp(DRAWABLE_TAG_SOURCE, "g");
  let match: RegExpExecArray | null;
  while ((match = drawableTag.exec(xml)) !== null) {
    const closing = match[1] === "/";
    const kind = match[2] as PptxTreeElementKind;
    const selfClosing = match[3] === "/";
    if (!closing) {
      if (selfClosing) {
        if (stack.length === 0) elements.push({ kind, xml: match[0] });
        continue;
      }
      stack.push({ kind, start: match.index });
      continue;
    }
    // Pop to the matching opener; tolerate malformed nesting by unwinding.
    let openerIndex = -1;
    for (let index = stack.length - 1; index >= 0; index -= 1) {
      if (stack[index].kind === kind) { openerIndex = index; break; }
    }
    if (openerIndex < 0) continue;
    const opener = stack[openerIndex];
    stack.length = openerIndex;
    if (stack.length === 0) {
      elements.push({ kind, xml: xml.slice(opener.start, match.index + match[0].length) });
    }
  }
  return elements;
}

/** The markup between a group's own open and close tags. */
export function pptxGroupInnerXml(groupXml: string): string {
  const openEnd = groupXml.indexOf(">");
  const closeStart = groupXml.lastIndexOf("</p:grpSp>");
  if (openEnd < 0 || closeStart <= openEnd) return "";
  return groupXml.slice(openEnd + 1, closeStart);
}

function intAttr(tag: string | undefined, name: string): number | null {
  if (!tag) return null;
  const match = tag.match(new RegExp(`\\b${name}="(-?\\d+)"`));
  return match ? parseInt(match[1], 10) : null;
}

/** Parse `<p:grpSpPr><a:xfrm>` (the group's own transform, not a child's). */
export function parsePptxGroupTransform(groupXml: string): PptxGroupTransform | null {
  const inner = pptxGroupInnerXml(groupXml);
  // The group's own properties precede its first drawable child.
  const firstChild = inner.search(/<p:(sp|pic|cxnSp|graphicFrame|grpSp)(?=[\s/>])/);
  const head = firstChild >= 0 ? inner.slice(0, firstChild) : inner;
  const xfrm = head.match(/<a:xfrm\b[\s\S]*?<\/a:xfrm>/)?.[0];
  if (!xfrm) return null;
  const off = xfrm.match(/<a:off\b[^>]*>/)?.[0];
  const ext = xfrm.match(/<a:ext\b[^>]*>/)?.[0];
  const chOff = xfrm.match(/<a:chOff\b[^>]*>/)?.[0];
  const chExt = xfrm.match(/<a:chExt\b[^>]*>/)?.[0];
  const offX = intAttr(off, "x") ?? 0;
  const offY = intAttr(off, "y") ?? 0;
  const extCx = intAttr(ext, "cx");
  const extCy = intAttr(ext, "cy");
  if (extCx == null || extCy == null) return null;
  return {
    offX,
    offY,
    extCx,
    extCy,
    chOffX: intAttr(chOff, "x") ?? offX,
    chOffY: intAttr(chOff, "y") ?? offY,
    chExtCx: intAttr(chExt, "cx") ?? extCx,
    chExtCy: intAttr(chExt, "cy") ?? extCy,
  };
}

/** Compose a group's child-space → parent-space transform onto `parent`. */
export function composePptxGroupMapper(
  parent: PptxEmuMapper,
  transform: PptxGroupTransform | null,
): PptxEmuMapper {
  if (!transform) return parent;
  const scaleX = transform.chExtCx > 0 ? transform.extCx / transform.chExtCx : 1;
  const scaleY = transform.chExtCy > 0 ? transform.extCy / transform.chExtCy : 1;
  return (rect) => parent({
    x: transform.offX + (rect.x - transform.chOffX) * scaleX,
    y: transform.offY + (rect.y - transform.chOffY) * scaleY,
    w: rect.w * scaleX,
    h: rect.h * scaleY,
  });
}

/**
 * Flatten a shape tree into leaf elements in document (z) order. Groups are
 * expanded recursively; each leaf carries the composed mapper of its groups.
 */
export function flattenPptxShapeTree(
  xml: string,
  parent: PptxEmuMapper = identityPptxEmuMapper,
  depth = 0,
  maxDepth = 32,
): PptxFlatElement[] {
  const flat: PptxFlatElement[] = [];
  for (const element of scanPptxShapeTree(xml)) {
    if (element.kind === "grpSp") {
      if (depth >= maxDepth) continue;
      const mapper = composePptxGroupMapper(parent, parsePptxGroupTransform(element.xml));
      flat.push(...flattenPptxShapeTree(pptxGroupInnerXml(element.xml), mapper, depth + 1, maxDepth));
      continue;
    }
    flat.push({ kind: element.kind, xml: element.xml, toSlide: parent, depth });
  }
  return flat;
}

/** Classify a graphic frame by its `<a:graphicData uri>`. */
export function pptxGraphicFrameKind(frameXml: string): PptxGraphicFrameKind {
  const uri = frameXml.match(/<a:graphicData\b[^>]*\buri="([^"]*)"/)?.[1] || "";
  if (/\/drawingml\/2006\/table$/.test(uri) || /<a:tbl[\s>]/.test(frameXml)) return "table";
  if (/\/drawingml\/2006\/chart$/.test(uri) || /chartex|\/chart$/i.test(uri)) return "chart";
  if (/\/drawingml\/2006\/diagram$/.test(uri)) return "smartart";
  if (/\/presentationml\/2006\/ole$/.test(uri) || /<p:oleObj[\s>]/.test(frameXml)) return "ole";
  return "other";
}

/** Rectangle of a graphic frame's `<p:xfrm>` in its parent's coordinate space. */
export function pptxGraphicFrameRect(frameXml: string): PptxEmuRect | null {
  const xfrm = frameXml.match(/<p:xfrm\b[\s\S]*?<\/p:xfrm>/)?.[0]
    || frameXml.match(/<a:xfrm\b[\s\S]*?<\/a:xfrm>/)?.[0];
  if (!xfrm) return null;
  const off = xfrm.match(/<a:off\b[^>]*>/)?.[0];
  const ext = xfrm.match(/<a:ext\b[^>]*>/)?.[0];
  const x = intAttr(off, "x");
  const y = intAttr(off, "y");
  const w = intAttr(ext, "cx");
  const h = intAttr(ext, "cy");
  if (x == null || y == null || w == null || h == null) return null;
  return { x, y, w, h };
}
