import {
  decodeFileReferenceHref,
  fileExtensionFromReference,
  fileNameFromReference,
  fileReferenceKind,
  isInternalFileTarget,
  viewerPathForDocumentId,
} from "./fileReferences";

export type ResultFileMetadata = {
  name: string;
  fileType?: string;
  mimeType?: string;
  /** When the run declares final refs, intermediate step files are excluded. */
  isFinal?: boolean;
};

export function isVideoResultFile(file: ResultFileMetadata): boolean {
  return isMediaResultFile(file, "video");
}

export function isMediaResultFile(file: ResultFileMetadata, media: "video" | "audio" | "image"): boolean {
  return fileReferenceKind(file.name, file.mimeType, file.fileType) === media;
}

export function isVideoPreviewFile(file: ResultFileMetadata): boolean {
  return isVideoResultFile(file) && /(?:^|[\s_.\/-])(?:preview|draft|raw|source|clean[-_ ]?master|clip|segment|shot|s\d{2})(?:$|[\s_.\/-])|预览|草稿|片段|素材/i.test(file.name);
}

/** Stable presentation order only; never infer completion or change identities. */
export function prioritizeVideoResultFiles<T extends ResultFileMetadata>(files: readonly T[]): T[] {
  const videos = files.filter((file) => isVideoResultFile(file) && !isVideoPreviewFile(file));
  const previews = files.filter(isVideoPreviewFile);
  const other = files.filter((file) => !isVideoResultFile(file));
  return [...videos, ...previews, ...other];
}

const PAGED_SOURCE = /\.(?:pptx?|docx?|xlsx?|key|pages)$/i;
const READ_IN_PLACE = /\.(?:md|markdown|txt|csv|tsv)$/i;
const RESULT_SHOWN_IN_PLACE = /\.(?:png|jpe?g|gif|webp|bmp|avif|mp4|mov|webm|m4v|mkv|pptx?|pdf|docx?|xlsx?|md|markdown|txt|csv|tsv)$/i;

/** A PDF made from an editable file of the same name in the same result (deck.pptx → deck.pdf). */
export function isRenderedCopy(file: ResultFileMetadata, files: readonly ResultFileMetadata[]): boolean {
  const match = file.name.match(/^(.*)\.pdf$/i);
  if (!match) return false;
  const stem = match[1].toLowerCase();
  return files.some((other) => other !== file && PAGED_SOURCE.test(other.name)
    && other.name.replace(/\.[^.]+$/, "").toLowerCase() === stem);
}

/**
 * Which final files are shown as themselves (a picture, a frame, a first page,
 * a written result or table read in place) and which as a plain file link:
 * other formats, and the PDF copy of a deck or document already shown by its
 * own first page.
 */
export function partitionPreviewedResultFiles<T extends ResultFileMetadata>(files: readonly T[]): { previewed: T[]; listed: T[] } {
  const previewed = files.filter((file) => RESULT_SHOWN_IN_PLACE.test(file.name) && !isRenderedCopy(file, files));
  return { previewed, listed: files.filter((file) => !previewed.includes(file)) };
}

/**
 * A finished Workflow result in Workspace chat is a sentence and rows that
 * expand, so scheduled runs do not fill the conversation. A file someone still
 * has to decide on stays open: it is what the decision is about.
 */
export function workspaceResultStartsFolded(message: {
  meta?: { workflow_result?: unknown } | null;
  pending_action?: unknown;
  resolved_at?: unknown;
}): boolean {
  if (message.pending_action && !message.resolved_at) return false;
  return Boolean(message.meta?.workflow_result);
}

/** Legacy output bundles include production evidence alongside deliverables. */
/** A record ID or a content hash in a file name: a working file a run named for itself, not for a person. */
const MACHINE_NAMED = /(?:^|[^0-9A-Za-z])(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{24,})(?:[^0-9A-Za-z]|$)/;

export function isSupportingResultFile(file: ResultFileMetadata): boolean {
  return isVideoPreviewFile(file)
    || MACHINE_NAMED.test(file.name)
    || /(?:^|[\s_.\/-])(?:qa|canvas|storyboard|manifest|timeline|ledger|reference|thumbnail|preview|draft|source|raw|editable|subtitles?)(?:$|[\s_.\/-])|quality[-_ ]?review|internal[-_ ]?review|(?:first|last)[-_ ]?frames?|frame[-_ ]?samples?|contact[-_ ]?sheet|\.video-edit\.json$|\.(?:srt|vtt)$|分镜|字幕|质检|预览|草稿|素材/i.test(file.name);
}

export function partitionGeneratedResultFiles<T extends ResultFileMetadata>(files: readonly T[]): { primary: T[]; supporting: T[] } {
  const ordered = prioritizeVideoResultFiles(files);
  const deliverables = ordered.filter((file) => file.isFinal !== false && !isSupportingResultFile(file));
  const videos = deliverables.filter(isVideoResultFile);
  const images = deliverables.filter((file) => fileReferenceKind(file.name, file.mimeType, file.fileType) === "image");
  const media = videos.length ? videos : images;
  // A table or written result is read, not a production asset: it stays beside the media.
  const readable = deliverables.filter((file) => READ_IN_PLACE.test(file.name) || READ_IN_PLACE.test(`.${file.fileType || ""}`));
  const primary = media.length ? deliverables.filter((file) => media.includes(file) || readable.includes(file)) : deliverables;
  return { primary, supporting: ordered.filter((file) => !primary.includes(file)) };
}

export function prioritizeGeneratedResultFiles<T extends ResultFileMetadata>(files: readonly T[]): T[] {
  const { primary, supporting } = partitionGeneratedResultFiles(files);
  return [...primary, ...supporting];
}

/** Preview the canonical Document through its access-checked endpoint. */
export function resultFilePreviewTarget(reference: string, origin: string): { documentId: string } | { url: string } | null {
  const decoded = decodeFileReferenceHref(reference) || reference;
  try {
    const url = new URL(decoded, origin);
    const viewer = url.pathname.match(/^\/viewer\/([^/]+)$/);
    const document = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/(?:download|preview\/content)$/);
    if (viewer || document) {
      const id = decodeURIComponent((viewer || document)![1]);
      return url.origin === origin && viewerPathForDocumentId(id) ? { documentId: id } : null;
    }
    if (url.origin === origin && /^\/api\/v1\/fs\/[^/]+\/.+/.test(url.pathname)) {
      return { url: `${url.pathname}${url.search}` };
    }
    if (url.origin !== origin && /^(?:https?):$/.test(url.protocol)) return { url: decoded };
  } catch { /* An unresolved file address stays a file card. */ }
  return null;
}

/**
 * The file a reply's Markdown link hands over, when it is one a chat shows as
 * itself (a table, a written result, a picture, a page) and this Manor can
 * preview it. Anything else stays a link.
 */
export function inPlaceResultFileForLink(
  href: string | undefined,
  label: string,
  origin: string,
  { image = false }: { image?: boolean } = {},
): (ResultFileMetadata & { key: string; reference: string }) | null {
  if (!href?.trim() || isInternalFileTarget(href)) return null;
  const reference = decodeFileReferenceHref(href) || href;
  if (!resultFilePreviewTarget(reference, origin)) return null;
  const cleanLabel = label.trim();
  const named = /\.[a-z0-9]{1,8}$/i.test(cleanLabel) ? cleanLabel : fileNameFromReference(reference);
  const extension = /\.[a-z0-9]{1,8}$/i.test(named) ? fileExtensionFromReference(named) : "";
  // ``![...](...)`` is a picture whatever its name says; a link must name a shown kind.
  if (image) return { key: reference, name: extension ? named : cleanLabel || "image", reference, fileType: extension || "png" };
  if (!named || !partitionPreviewedResultFiles([{ name: named }]).previewed.length) return null;
  return { key: reference, name: named, reference, fileType: extension || undefined };
}
