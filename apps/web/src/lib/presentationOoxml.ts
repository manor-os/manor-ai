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
