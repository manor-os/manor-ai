/**
 * Resolve an OOXML relationship target to the package-relative part name used
 * by JSZip. Relationship targets are relative to the part that owns the .rels
 * file; a leading slash denotes an absolute path inside the package.
 */
export function resolvePresentationPartTarget(sourcePart: string, target: string): string {
  const normalizedTarget = target.replace(/\\/g, "/");
  const withoutFragment = normalizedTarget.split("#", 1)[0].split("?", 1)[0];
  const sourceDirectory = sourcePart.replace(/\\/g, "/").replace(/^\/+/, "").split("/").slice(0, -1);
  const segments = withoutFragment.startsWith("/")
    ? withoutFragment.split("/")
    : [...sourceDirectory, ...withoutFragment.split("/")];
  const resolved: string[] = [];

  for (const segment of segments) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      resolved.pop();
      continue;
    }
    resolved.push(segment);
  }

  return resolved.join("/");
}

/** Return the package-relative relationship part for an OOXML source part. */
export function presentationRelationshipsPart(sourcePart: string): string {
  const normalized = sourcePart.replace(/\\/g, "/").replace(/^\/+/, "");
  const slash = normalized.lastIndexOf("/");
  const directory = slash >= 0 ? normalized.slice(0, slash + 1) : "";
  const fileName = slash >= 0 ? normalized.slice(slash + 1) : normalized;
  return `${directory}_rels/${fileName}.rels`;
}

/** Return DrawingML object ids in their source paint order. */
export function presentationObjectIdsInOrder(slideXml: string): string[] {
  const ids: string[] = [];
  for (const match of slideXml.matchAll(/<p:cNvPr\b[^>]*\bid="([^"]+)"/gi)) {
    if (!ids.includes(match[1])) ids.push(match[1]);
  }
  return ids;
}

export interface PresentationObjectGroup {
  xml: string;
  objectIds: Set<string>;
}

/** Return complete outermost group elements without truncating nested groups. */
export function presentationObjectGroups(slideXml: string): PresentationObjectGroup[] {
  const groups: PresentationObjectGroup[] = [];
  let depth = 0;
  let groupStart: number | undefined;
  for (const match of slideXml.matchAll(/<p:grpSp\b[^>]*>|<\/p:grpSp>/gi)) {
    const matchIndex = match.index;
    if (matchIndex == null) continue;
    if (!match[0].startsWith("</")) {
      if (depth === 0) groupStart = matchIndex;
      if (/\/\s*>$/.test(match[0])) {
        if (depth === 0) {
          const xml = match[0];
          groups.push({ xml, objectIds: new Set(presentationObjectIdsInOrder(xml)) });
          groupStart = undefined;
        }
      } else depth += 1;
      continue;
    }
    if (depth === 0) continue;
    depth -= 1;
    if (depth !== 0 || groupStart == null) continue;
    const xml = slideXml.slice(groupStart, matchIndex + match[0].length);
    groups.push({ xml, objectIds: new Set(presentationObjectIdsInOrder(xml)) });
    groupStart = undefined;
  }
  return groups;
}

export function presentationGroupContent(groupXml: string): {
  directXml: string;
  nestedGroups: PresentationObjectGroup[];
} {
  const openingEnd = groupXml.indexOf(">");
  const closingStart = groupXml.toLowerCase().lastIndexOf("</p:grpsp>");
  const content = openingEnd >= 0 && closingStart > openingEnd
    ? groupXml.slice(openingEnd + 1, closingStart)
    : "";
  const nestedGroups = presentationObjectGroups(content);
  let directXml = content;
  for (const group of nestedGroups) directXml = directXml.replace(group.xml, "");
  return { directXml, nestedGroups };
}

export interface PresentationGroupTransform {
  a: number;
  b: number;
  c: number;
  d: number;
  e: number;
  f: number;
}

export const IDENTITY_PRESENTATION_GROUP_TRANSFORM: PresentationGroupTransform = {
  a: 1,
  b: 0,
  c: 0,
  d: 1,
  e: 0,
  f: 0,
};

function composePresentationTransforms(
  parent: PresentationGroupTransform,
  child: PresentationGroupTransform,
): PresentationGroupTransform {
  return {
    a: parent.a * child.a + parent.c * child.b,
    b: parent.b * child.a + parent.d * child.b,
    c: parent.a * child.c + parent.c * child.d,
    d: parent.b * child.c + parent.d * child.d,
    e: parent.a * child.e + parent.c * child.f + parent.e,
    f: parent.b * child.e + parent.d * child.f + parent.f,
  };
}

export function presentationInverseTransform(
  transform: PresentationGroupTransform,
): PresentationGroupTransform | undefined {
  const determinant = transform.a * transform.d - transform.b * transform.c;
  if (Math.abs(determinant) < 1e-12) return undefined;
  return {
    a: transform.d / determinant,
    b: -transform.b / determinant,
    c: -transform.c / determinant,
    d: transform.a / determinant,
    e: (transform.c * transform.f - transform.d * transform.e) / determinant,
    f: (transform.b * transform.e - transform.a * transform.f) / determinant,
  };
}

export interface PresentationTransformRect {
  x: number;
  y: number;
  width: number;
  height: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
}

export function presentationTransformPoint(
  transform: PresentationGroupTransform,
  x: number,
  y: number,
): { x: number; y: number } {
  return {
    x: transform.a * x + transform.c * y + transform.e,
    y: transform.b * x + transform.d * y + transform.f,
  };
}

/** Return the complete source-to-slide matrix for a rotated/flipped shape. */
export function presentationShapeTransform(
  groupTransform: PresentationGroupTransform,
  rect: PresentationTransformRect,
): PresentationGroupTransform {
  const radians = (rect.rotation || 0) * Math.PI / 180;
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  const horizontalSign = rect.flipH ? -1 : 1;
  const verticalSign = rect.flipV ? -1 : 1;
  const centerX = rect.x + rect.width / 2;
  const centerY = rect.y + rect.height / 2;
  const orientation: PresentationGroupTransform = {
    a: cosine * horizontalSign,
    b: sine * horizontalSign,
    c: -sine * verticalSign,
    d: cosine * verticalSign,
    e: 0,
    f: 0,
  };
  orientation.e = centerX - orientation.a * centerX - orientation.c * centerY;
  orientation.f = centerY - orientation.b * centerX - orientation.d * centerY;
  return composePresentationTransforms(groupTransform, orientation);
}

export type PresentationResizeHandle = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

/** Resize through the visible shape axes while keeping the opposite handle fixed. */
export function presentationResizeRect(
  rect: PresentationTransformRect,
  handle: PresentationResizeHandle,
  slideDelta: { x: number; y: number },
  groupTransform: PresentationGroupTransform = IDENTITY_PRESENTATION_GROUP_TRANSFORM,
  minimum: { width: number; height: number } = { width: 1, height: 1 },
): PresentationTransformRect {
  const shapeTransform = presentationShapeTransform(groupTransform, rect);
  const inverseShape = presentationInverseTransform(shapeTransform);
  const inverseGroup = presentationInverseTransform(groupTransform);
  if (!inverseShape || !inverseGroup) return { ...rect };

  const localDeltaX = inverseShape.a * slideDelta.x + inverseShape.c * slideDelta.y;
  const localDeltaY = inverseShape.b * slideDelta.x + inverseShape.d * slideDelta.y;
  let horizontalDelta = 0;
  let verticalDelta = 0;
  let width = rect.width;
  let height = rect.height;

  if (handle.includes("e")) {
    horizontalDelta = Math.max(minimum.width - rect.width, localDeltaX);
    width = rect.width + horizontalDelta;
  } else if (handle.includes("w")) {
    horizontalDelta = Math.min(rect.width - minimum.width, localDeltaX);
    width = rect.width - horizontalDelta;
  }
  if (handle.includes("s")) {
    verticalDelta = Math.max(minimum.height - rect.height, localDeltaY);
    height = rect.height + verticalDelta;
  } else if (handle.includes("n")) {
    verticalDelta = Math.min(rect.height - minimum.height, localDeltaY);
    height = rect.height - verticalDelta;
  }

  const center = {
    x: rect.x + rect.width / 2,
    y: rect.y + rect.height / 2,
  };
  const centerOnSlide = presentationTransformPoint(groupTransform, center.x, center.y);
  const centerShiftX = horizontalDelta / 2;
  const centerShiftY = verticalDelta / 2;
  const shiftedCenterOnSlide = {
    x: centerOnSlide.x + shapeTransform.a * centerShiftX + shapeTransform.c * centerShiftY,
    y: centerOnSlide.y + shapeTransform.b * centerShiftX + shapeTransform.d * centerShiftY,
  };
  const shiftedCenter = presentationTransformPoint(
    inverseGroup,
    shiftedCenterOnSlide.x,
    shiftedCenterOnSlide.y,
  );
  return {
    ...rect,
    x: shiftedCenter.x - width / 2,
    y: shiftedCenter.y - height / 2,
    width,
    height,
  };
}

/** Return the axis-aligned slide bounds of a fully transformed source rectangle. */
export function presentationTransformBounds(
  transform: PresentationGroupTransform,
  rect: Pick<PresentationTransformRect, "x" | "y" | "width" | "height">,
): Pick<PresentationTransformRect, "x" | "y" | "width" | "height"> {
  const corners = [
    presentationTransformPoint(transform, rect.x, rect.y),
    presentationTransformPoint(transform, rect.x + rect.width, rect.y),
    presentationTransformPoint(transform, rect.x, rect.y + rect.height),
    presentationTransformPoint(transform, rect.x + rect.width, rect.y + rect.height),
  ];
  const xs = corners.map((point) => point.x);
  const ys = corners.map((point) => point.y);
  const left = Math.min(...xs);
  const top = Math.min(...ys);
  return {
    x: left,
    y: top,
    width: Math.max(...xs) - left,
    height: Math.max(...ys) - top,
  };
}

/** Compose one group's child-coordinate transform with its parent transform. */
export function presentationGroupTransform(
  groupXml: string,
  parent: PresentationGroupTransform = IDENTITY_PRESENTATION_GROUP_TRANSFORM,
): PresentationGroupTransform {
  const properties = groupXml.match(/<p:grpSpPr\b[^>]*>[\s\S]*?<\/p:grpSpPr>/i)?.[0];
  const transform = properties?.match(/<a:xfrm\b[^>]*>[\s\S]*?<\/a:xfrm>/i)?.[0];
  if (!transform) return { ...parent };
  const point = (tag: "off" | "ext" | "chOff" | "chExt", x: "x" | "cx", y: "y" | "cy") => {
    const opening = transform.match(new RegExp(`<a:${tag}\\b[^>]*>`, "i"))?.[0] || "";
    const value = (name: string) => {
      const match = opening.match(new RegExp(`\\b${name}="(-?\\d+)"`, "i"));
      return match ? Number(match[1]) : undefined;
    };
    return { x: value(x), y: value(y) };
  };
  const offset = point("off", "x", "y");
  const extent = point("ext", "cx", "cy");
  const childOffset = point("chOff", "x", "y");
  const childExtent = point("chExt", "cx", "cy");
  if (
    offset.x == null || offset.y == null
    || extent.x == null || extent.y == null
    || childOffset.x == null || childOffset.y == null
    || childExtent.x == null || childExtent.y == null
    || childExtent.x === 0 || childExtent.y === 0
  ) return { ...parent };
  const scaleX = extent.x / childExtent.x;
  const scaleY = extent.y / childExtent.y;
  const translateX = offset.x - childOffset.x * scaleX;
  const translateY = offset.y - childOffset.y * scaleY;
  const opening = transform.match(/<a:xfrm\b[^>]*>/i)?.[0] || "";
  const rotation = Number(opening.match(/\brot="(-?\d+)"/i)?.[1] || 0) / 60_000 * Math.PI / 180;
  const flipH = /\bflipH="(?:1|true)"/i.test(opening) ? -1 : 1;
  const flipV = /\bflipV="(?:1|true)"/i.test(opening) ? -1 : 1;
  const cosine = Math.cos(rotation);
  const sine = Math.sin(rotation);
  const centerX = offset.x + extent.x / 2;
  const centerY = offset.y + extent.y / 2;
  const orientation: PresentationGroupTransform = {
    a: cosine * flipH,
    b: sine * flipH,
    c: -sine * flipV,
    d: cosine * flipV,
    e: 0,
    f: 0,
  };
  orientation.e = centerX - orientation.a * centerX - orientation.c * centerY;
  orientation.f = centerY - orientation.b * centerX - orientation.d * centerY;
  return composePresentationTransforms(parent, composePresentationTransforms(orientation, {
    a: scaleX,
    b: 0,
    c: 0,
    d: scaleY,
    e: translateX,
    f: translateY,
  }));
}

/**
 * Return only the shape-level fill portion of `<p:spPr>`.
 *
 * A very common PowerPoint shape has a solid fill and a line with
 * `<a:noFill/>`. Looking for `noFill` across the whole properties block makes
 * that line setting erase the shape background in browser renderers. Strip
 * line/effect children before interpreting the shape fill.
 */
export function presentationShapeFillScope(shapePropertiesXml: string): string {
  return shapePropertiesXml
    .replace(/<a:ln[\s>][\s\S]*?<\/a:ln>/gi, "")
    .replace(/<a:effectLst[\s>][\s\S]*?<\/a:effectLst>/gi, "")
    .replace(/<a:effectDag[\s>][\s\S]*?<\/a:effectDag>/gi, "");
}

/** Apply a DrawingML color alpha value to one browser-rendered color. */
export function presentationColorWithAlpha(color: string, colorXml: string): string {
  const alphaMatch = colorXml.match(/<a:alpha\b[^>]*\bval="(\d+)"/i);
  if (!alphaMatch) return color;
  const alpha = Math.max(0, Math.min(1, Number(alphaMatch[1]) / 100000));
  if (alpha <= 0) return "transparent";
  if (alpha >= 1 || !/^#[\da-f]{6}$/i.test(color)) return color;
  const hex = color.slice(1);
  const red = Number.parseInt(hex.slice(0, 2), 16);
  const green = Number.parseInt(hex.slice(2, 4), 16);
  const blue = Number.parseInt(hex.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${Number(alpha.toFixed(4))})`;
}

/** Infer a browser-safe MIME type for an embedded presentation media part. */
export function presentationMediaMime(partName: string): string {
  const extension = partName.split(".").pop()?.toLowerCase();
  if (extension === "jpg" || extension === "jpeg") return "image/jpeg";
  if (extension === "png") return "image/png";
  if (extension === "gif") return "image/gif";
  if (extension === "webp") return "image/webp";
  if (extension === "svg") return "image/svg+xml";
  if (extension === "mp4" || extension === "m4v" || extension === "mov") return "video/mp4";
  if (extension === "webm") return "video/webm";
  if (extension === "ogv" || extension === "ogg") return "video/ogg";
  if (extension === "mp3") return "audio/mpeg";
  if (extension === "m4a") return "audio/mp4";
  if (extension === "wav") return "audio/wav";
  return "application/octet-stream";
}

/**
 * Extract relationship IDs used by native PowerPoint video objects.
 * Modern Office writes both `a:videoFile` and the PowerPoint 2010 `p14:media`
 * extension; accept either form and preserve document order.
 */
export function presentationVideoRelationshipIds(shapeXml: string): string[] {
  const ids: string[] = [];
  const patterns = [
    /<a:videoFile\b[^>]*\br:(?:link|embed)="([^"]+)"[^>]*\/?\s*>/gi,
    /<p14:media\b[^>]*\br:embed="([^"]+)"[^>]*\/?\s*>/gi,
  ];
  for (const pattern of patterns) {
    for (const match of shapeXml.matchAll(pattern)) {
      if (match[1] && !ids.includes(match[1])) ids.push(match[1]);
    }
  }
  return ids;
}

/** Resolve the first playable native video referenced by a presentation shape. */
export function presentationVideoSource(
  shapeXml: string,
  relationshipUrls: ReadonlyMap<string, string>,
): string | undefined {
  for (const relationshipId of presentationVideoRelationshipIds(shapeXml)) {
    const url = relationshipUrls.get(relationshipId);
    if (url) return url;
  }
  return undefined;
}
