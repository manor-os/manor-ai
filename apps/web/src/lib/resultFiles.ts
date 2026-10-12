import { decodeFileReferenceHref, fileReferenceKind, viewerPathForDocumentId } from "./fileReferences";

export type ResultFileMetadata = {
  name: string;
  fileType?: string;
  mimeType?: string;
  /** When the run declares final refs, intermediate step files are excluded. */
  isFinal?: boolean;
};

export function isVideoResultFile(file: ResultFileMetadata): boolean {
  return fileReferenceKind(file.name, file.mimeType, file.fileType) === "video";
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

/** Legacy output bundles include production evidence alongside deliverables. */
export function isSupportingResultFile(file: ResultFileMetadata): boolean {
  return isVideoPreviewFile(file)
    || /(?:^|[\s_.\/-])(?:qa|canvas|storyboard|manifest|timeline|ledger|reference|thumbnail|preview|draft|source|raw|editable|subtitles?)(?:$|[\s_.\/-])|quality[-_ ]?review|internal[-_ ]?review|(?:first|last)[-_ ]?frames?|frame[-_ ]?samples?|contact[-_ ]?sheet|\.video-edit\.json$|\.(?:srt|vtt)$|分镜|字幕|质检|预览|草稿|素材/i.test(file.name);
}

export function partitionGeneratedResultFiles<T extends ResultFileMetadata>(files: readonly T[]): { primary: T[]; supporting: T[] } {
  const ordered = prioritizeVideoResultFiles(files);
  const deliverables = ordered.filter((file) => file.isFinal !== false && !isSupportingResultFile(file));
  const videos = deliverables.filter(isVideoResultFile);
  const images = deliverables.filter((file) => fileReferenceKind(file.name, file.mimeType, file.fileType) === "image");
  const primary = videos.length ? videos : images.length ? images : deliverables;
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
