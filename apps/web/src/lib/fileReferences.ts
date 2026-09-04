import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";
import { markdownContentWithRenderedAssistantFinalText } from "./assistantTextProjection";
import {
  decodeRouteReferenceHref,
  linkifyChatRouteReferencesInMarkdown,
} from "./chatRouteReferences";

export { markdownContentWithRenderedAssistantFinalText };

export type FileReferenceKind =
  | "presentation"
  | "pdf"
  | "spreadsheet"
  | "diagram"
  | "code"
  | "page"
  | "image"
  | "video"
  | "audio"
  | "archive"
  | "document"
  | "file";

const FILE_REFERENCE_EXTENSIONS_BY_KIND: Record<Exclude<FileReferenceKind, "file">, readonly string[]> = {
  presentation: ["ppt", "pptx", "dps", "key", "odp"],
  pdf: ["pdf"],
  spreadsheet: ["xls", "xlsx", "xlsm", "et", "csv", "tsv", "ods", "numbers"],
  diagram: ["mmd", "mermaid", "drawio", "diagram"],
  code: [
    "css", "js", "jsx", "mjs", "cjs", "ts", "tsx", "py", "sql", "json", "jsonl",
    "yaml", "yml", "xml", "toml", "ini", "conf", "sh", "bash", "zsh", "fish", "go",
    "rs", "java", "kt", "kts", "rb", "php", "swift", "c", "cc", "cpp", "cxx", "h",
    "hpp", "cs", "scala", "vue", "svelte", "ipynb", "log",
  ],
  page: ["html", "htm", "xhtml"],
  image: [
    "png", "jpg", "jpeg", "webp", "gif", "svg", "avif", "heic", "heif", "bmp", "tif",
    "tiff", "ico",
  ],
  video: ["mp4", "mov", "webm", "m4v", "avi", "mkv", "mpeg", "mpg"],
  audio: ["mp3", "wav", "m4a", "aac", "ogg", "flac", "opus"],
  archive: ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "dmg", "pkg"],
  document: ["doc", "docx", "wps", "odt", "md", "markdown", "txt", "rtf", "tex", "epub", "mobi"],
};

const FILE_REFERENCE_EXTENSIONS = Array.from(
  new Set(Object.values(FILE_REFERENCE_EXTENSIONS_BY_KIND).flat()),
);
const FILE_REFERENCE_KIND_BY_EXTENSION = new Map<string, FileReferenceKind>(
  Object.entries(FILE_REFERENCE_EXTENSIONS_BY_KIND).flatMap(([kind, extensions]) => (
    extensions.map((extension) => [extension, kind as FileReferenceKind])
  )),
);
const FILE_REFERENCE_KIND_BY_TYPE_ALIAS = new Map<string, FileReferenceKind>([
  ["presentation", "presentation"],
  ["spreadsheet", "spreadsheet"],
  ["diagram", "diagram"],
  ["code", "code"],
  ["page", "page"],
  ["image", "image"],
  ["video", "video"],
  ["audio", "audio"],
  ["archive", "archive"],
  ["document", "document"],
]);

const FILE_EXT_PATTERN = FILE_REFERENCE_EXTENSIONS.join("|");
const FILE_REF_SCHEME = "manor-file:";
const FILE_REF_PATH_COMPONENT = String.raw`(?:[A-Za-z0-9_.~%+\-\u4e00-\u9fff]+|[A-Za-z0-9_.~%+\-\u4e00-\u9fff][A-Za-z0-9_.~%+\-\u4e00-\u9fff ()（）\[\]【】+&,'’]*[A-Za-z0-9_.~%+\-\u4e00-\u9fff])`;

const FILE_REF_RE = new RegExp(
  String.raw`(^|[\s([{<'"“‘，。；：、])((?:\/)?(?:(?:[A-Za-z0-9_.~\-\u4e00-\u9fff]+|[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ]*[A-Za-z0-9_.~\-\u4e00-\u9fff])\/)*(?:[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ()（）\[\]【】+&,'’]*\.(${FILE_EXT_PATTERN})))(?=$|[\s)\]}>'"“”’。，、；:：!?！？|])`,
  "giu",
);

const PLATFORM_FS_FILE_REF_RE = new RegExp(
  String.raw`((?:https?:\/\/[^/\s<>"'` + "`" + String.raw`]+)?\/api\/v1\/fs\/(?:${FILE_REF_PATH_COMPONENT}\/)+(?:${FILE_REF_PATH_COMPONENT})\.(${FILE_EXT_PATTERN}))(?=$|[?#\s)\]}>'"“”’.,。，、；:：!?！？|` + "`" + String.raw`])`,
  "giu",
);

const FILE_LIKE_RE = new RegExp(
  String.raw`\.(${FILE_EXT_PATTERN})(?=$|[?#\s)\]}>'"“”’。，、；:：!?！？|` + "`" + String.raw`])`,
  "iu",
);
const MARKDOWN_LINK_OR_IMAGE_RE = /!?\[[^\]]*\]\([^)]*\)/g;
const MARKDOWN_LINK_PARTS_RE = /^\[([^\]]*)\]\(([^)]*)\)$/;
// Only a quoted Markdown title is excluded. Treating any parenthesized suffix
// as a title would eat real path segments such as `report (final)/index.html`.
const MARKDOWN_LINK_TITLE_RE = /\s+(?:"[^"]*"|'[^']*')\s*$/;
const ENTITY_FS_PATH_RE = /^\/api\/v1\/fs\/[^/]+\/.+/;
const FENCED_CODE_RE = /(```[\s\S]*?```|~~~[\s\S]*?~~~)/g;
const INLINE_CODE_RE = /(`[^`\n]+`)/g;

/** Extract only concrete Manor filesystem file URLs from assistant prose. */
export function extractPlatformFileReferences(content: string): string[] {
  return Array.from(
    String(content || "").matchAll(PLATFORM_FS_FILE_REF_RE),
    (match) => match[1],
  );
}

/**
 * Build the one canonical in-app destination for a known Document record.
 *
 * Callers that already have a structured document id must never downgrade it
 * to a filename and search again: exact ids are both faster and unambiguous.
 */
export function viewerPathForDocumentId(documentId: unknown): string | null {
  const value = String(documentId ?? "").trim();
  // Document keys are opaque identifiers, never filenames, paths or URLs.
  return /^[A-Za-z0-9_-]+$/.test(value) ? `/viewer/${value}` : null;
}

type GeneratedFileRecord = Record<string, unknown>;

const GENERATED_FILE_PATH_KEYS = ["fs_path", "fsPath", "path", "file_path", "output_path", "saved_to", "local_path"] as const;
const GENERATED_FILE_URL_KEYS = [
  "open_url", "openUrl", "viewer_url", "viewerUrl", "result_url", "file_url",
  "download_url", "document_url", "artifact_url", "output_url", "public_url", "url",
] as const;

function generatedFileText(record: GeneratedFileRecord, keys: readonly string[]): string {
  for (const key of keys) {
    const value = String(record[key] ?? "").trim();
    if (value) return value;
  }
  return "";
}

function decodePathPart(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** Historical viewer links encoded a filename instead of a Document key.
 * Keep both encoding interpretations so a lookup can reject ambiguous names.
 * This is a lookup hint, never permission to read an arbitrary filesystem path.
 */
export function legacyViewerFileCandidates(reference: string): string[] {
  const match = reference.match(/^\/viewer\/([^?#]+)(?:[?#].*)?$/);
  if (!match || viewerPathForDocumentId(decodePathPart(match[1]))) return [];
  const candidates = new Set<string>();
  let target = match[1];
  for (let pass = 0; pass <= 2; pass += 1) {
    if (/^[\\/]|[\\\x00-\x1f]|^[A-Za-z]:/.test(target)
      || target.split("/").some((part) => part === "." || part === "..")) return [];
    if (looksLikeFileReference(target)) candidates.add(target);
    try {
      const decoded = decodeURIComponent(target);
      if (decoded === target) break;
      target = decoded;
    } catch {
      return [];
    }
  }
  return [...candidates];
}

function platformFsPath(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  try {
    const url = new URL(text, "http://manor.local");
    const match = url.pathname.match(/^\/api\/v1\/fs\/[^/]+\/(.+)$/);
    return match?.[1] ? decodePathPart(match[1]).replace(/^\/+/, "") : "";
  } catch {
    return "";
  }
}

function canonicalGeneratedFilePath(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  const platformPath = platformFsPath(text);
  if (platformPath) return platformPath;
  if (/^(?:https?:|data:|blob:)/i.test(text)) return "";
  const rawPath = decodePathPart(text.split(/[?#]/, 1)[0] || text).replace(/\\/g, "/");
  if (/^(?:~\/|\/(?:Users|Volumes|private|tmp|var|etc)\/|[A-Za-z]:\/)/i.test(rawPath)) return "";
  const isAbsoluteRoute = rawPath.startsWith("/");
  const decoded = rawPath.replace(/^\/+/, "");
  if (!decoded || decoded === "." || decoded === ".." || decoded.startsWith("../")) return "";
  if (isAbsoluteRoute && /^(?:viewer|api\/v1|documents)\//i.test(decoded)) return "";
  return decoded.replace(/\/\.\//g, "/");
}

export function generatedFileDocumentId(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const record = value as GeneratedFileRecord;
  const id = String(record.document_id ?? "").trim();
  return viewerPathForDocumentId(id) ? id : "";
}

export function generatedFileFsPath(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const record = value as GeneratedFileRecord;
  for (const key of GENERATED_FILE_PATH_KEYS) {
    const path = canonicalGeneratedFilePath(record[key]);
    if (path) return path;
  }
  for (const key of GENERATED_FILE_URL_KEYS) {
    const path = platformFsPath(record[key]);
    if (path) return path;
  }
  return "";
}

/** Return the exact address a generated-file card should open. */
export function generatedFileOpenReference(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const record = value as GeneratedFileRecord;
  const documentId = generatedFileDocumentId(record);
  const viewerPath = viewerPathForDocumentId(documentId);
  if (viewerPath) return viewerPath;
  // Use an exact storage path as the legacy lookup hint when available, but
  // do not turn an invalid Document reference into permission to read raw FS.
  const invalidDocumentId = String(record.document_id ?? "").trim();
  const legacyFsPath = invalidDocumentId ? generatedFileFsPath(record) : "";
  if (legacyFsPath) return `/viewer/${encodeURIComponent(legacyFsPath)}`;
  const explicit = generatedFileText(record, ["open_url", "openUrl", "viewer_url", "viewerUrl"]);
  if (explicit) {
    const legacyViewer = explicit.match(/^\/viewer\/([^/?#]+)(?:[?#].*)?$/i);
    if (legacyViewer) {
      const decodedTarget = decodePathPart(legacyViewer[1]);
      if (decodedTarget.includes("/")) {
        const fsPath = canonicalGeneratedFilePath(decodedTarget);
        if (fsPath) return fsPath;
      }
    }
    return explicit;
  }
  for (const key of GENERATED_FILE_URL_KEYS) {
    const url = String(record[key] ?? "").trim();
    if (platformFsPath(url)) return url;
  }
  const fsPath = generatedFileFsPath(record);
  if (fsPath) return fsPath;
  for (const key of GENERATED_FILE_URL_KEYS) {
    const url = String(record[key] ?? "").trim();
    if (/^(?:https?:|data:|blob:)/i.test(url)) return url;
  }
  if (invalidDocumentId) {
    const legacy = `/viewer/${encodeURIComponent(invalidDocumentId)}`;
    if (legacyViewerFileCandidates(legacy).length) return legacy;
  }
  return "";
}

type MarkdownNode = {
  type: string;
  url?: string;
  identifier?: string;
  children?: MarkdownNode[];
};

/** Collect only links the same CommonMark/GFM syntax used by ChatMarkdown renders. */
function renderedMarkdownLinkDestinations(source: string): string[] {
  const root = fromMarkdown(source, {
    extensions: [gfm()],
    mdastExtensions: [gfmFromMarkdown()],
  }) as MarkdownNode;
  const definitions = new Map<string, string>();
  const referenceIdentifiers: string[] = [];
  const destinations: string[] = [];

  const visit = (node: MarkdownNode) => {
    if (node.type === "definition" && node.identifier && node.url) {
      if (!definitions.has(node.identifier)) definitions.set(node.identifier, node.url);
    } else if (node.type === "link" && node.url) {
      destinations.push(node.url);
    } else if (
      node.type === "linkReference"
      && node.identifier
    ) {
      referenceIdentifiers.push(node.identifier);
    }
    node.children?.forEach(visit);
  };

  visit(root);
  referenceIdentifiers.forEach((identifier) => {
    const destination = definitions.get(identifier);
    if (destination) destinations.push(destination);
  });
  return destinations;
}

function normalizedLinkedDestination(value: string): string {
  const unescaped = value.replace(/\\([\\()])/g, "$1").trim();
  if (!unescaped) return "";
  try {
    const parsed = new URL(unescaped, "https://manor.invalid");
    return parsed.origin === "https://manor.invalid"
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : parsed.href;
  } catch {
    return unescaped;
  }
}

function linkedDestinationAliases(value: string): string[] {
  const reference = String(value || "").trim();
  const decodedReference = decodeFileReferenceHref(reference)
    || decodeRouteReferenceHref(reference);
  const normalized = normalizedLinkedDestination(decodedReference || value);
  if (!normalized) return [];

  const aliases = [`open:${normalized}`];
  try {
    const parsed = new URL(normalized, "https://manor.invalid");
    const viewerMatch = parsed.pathname.match(/^\/viewer\/([^/]+)\/?$/i);
    if (viewerMatch?.[1]) {
      aliases.push(`document:${decodePathPart(viewerMatch[1])}`);
    }
  } catch {
    // The exact normalized address remains a usable alias below.
  }

  const fsPath = platformFsPath(normalized)
    || canonicalGeneratedFilePath(normalized);
  if (fsPath) aliases.push(`fs:${fsPath}`);
  return Array.from(new Set(aliases));
}

function generatedFileLinkAliases(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const record = value as GeneratedFileRecord;
  const aliases: string[] = [];
  const documentId = generatedFileDocumentId(record);
  const fsPath = generatedFileFsPath(record);
  if (documentId) aliases.push(`document:${documentId}`);
  if (fsPath) aliases.push(`fs:${fsPath}`);

  for (const key of GENERATED_FILE_URL_KEYS) {
    aliases.push(...linkedDestinationAliases(String(record[key] ?? "")));
  }
  for (const key of GENERATED_FILE_PATH_KEYS) {
    const path = canonicalGeneratedFilePath(record[key]);
    if (path) aliases.push(`fs:${path}`);
  }
  aliases.push(...linkedDestinationAliases(generatedFileOpenReference(record)));
  return Array.from(new Set(aliases));
}

/**
 * A generated file can be present both in message Markdown and structured
 * attachments. Keep the Markdown card as the inline source of truth and hide
 * only the redundant attachment card with the exact same destination.
 */
export function filterGeneratedFileRecordsAlreadyLinkedInMarkdown<T>(
  content: unknown,
  records: T[],
): T[] {
  if (!records.length) return records;
  const linkedAliases = new Set<string>();
  const source = typeof content === "string" ? content : "";
  if (!source) return records;
  const linkedSource = linkifyFileReferencesInMarkdown(
    linkifyChatRouteReferencesInMarkdown(source),
  );
  const destinations = renderedMarkdownLinkDestinations(linkedSource);
  for (const destination of destinations) {
    linkedDestinationAliases(destination).forEach((alias) => linkedAliases.add(alias));
  }
  if (!linkedAliases.size) return records;

  return records.filter((record) => {
    const aliases = generatedFileLinkAliases(record);
    return !aliases.some((alias) => linkedAliases.has(alias));
  });
}

/** Hide records already owned by another structured file surface. */
export function filterGeneratedFileRecordsAlreadyRepresented<T>(
  records: T[],
  representedRecords: unknown[],
): T[] {
  const representedAliases = new Set(
    representedRecords.flatMap((record) => generatedFileLinkAliases(record)),
  );
  if (!representedAliases.size) return records;
  return records.filter((record) =>
    !generatedFileLinkAliases(record).some((alias) => representedAliases.has(alias)),
  );
}

export function generatedFileLabel(value: unknown, fallback = "File"): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return String(value || fallback);
  }
  const record = value as GeneratedFileRecord;
  const explicit = generatedFileText(record, ["name", "filename", "file_name", "original_name", "title"]);
  if (explicit) return explicit;
  const reference = generatedFileFsPath(record) || generatedFileOpenReference(record) || generatedFileDocumentId(record);
  return reference ? fileNameFromReference(reference) : fallback;
}

function generatedFileAliases(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const record = value as GeneratedFileRecord;
  const aliases: string[] = [];
  const documentId = generatedFileDocumentId(record);
  const fsPath = generatedFileFsPath(record);
  const openReference = generatedFileOpenReference(record);
  if (documentId) aliases.push(`document:${documentId}`);
  if (fsPath) aliases.push(`fs:${fsPath}`);
  if (openReference && !openReference.startsWith("/viewer/")) aliases.push(`open:${openReference}`);
  if (!aliases.length) {
    const name = generatedFileLabel(record, "");
    if (name) aliases.push(`name:${name.toLowerCase()}`);
  }
  return Array.from(new Set(aliases));
}

export function generatedFileIdentity(value: unknown): string {
  return generatedFileAliases(value)[0] || String(value || "");
}

export function dedupeGeneratedFileRecords<T>(values: T[]): T[] {
  const output: T[] = [];
  const merge = (currentValue: T, incomingValue: T): T => {
    const current = currentValue as GeneratedFileRecord;
    const incoming = incomingValue as GeneratedFileRecord;
    return Object.fromEntries(
      Array.from(new Set([...Object.keys(current), ...Object.keys(incoming)])).map((key) => [
        key,
        current[key] === undefined || current[key] === null || current[key] === ""
          ? incoming[key]
          : current[key],
      ]),
    ) as T;
  };
  for (const value of Array.isArray(values) ? values : []) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const aliases = generatedFileAliases(value);
    const matchingIndexes = output.flatMap((existing, index) => (
      generatedFileAliases(existing).some((alias) => aliases.includes(alias)) ? [index] : []
    ));
    if (!matchingIndexes.length) {
      output.push(value);
      continue;
    }
    const target = matchingIndexes[0];
    output[target] = merge(output[target], value);
    for (const duplicate of matchingIndexes.slice(1).reverse()) {
      output[target] = merge(output[target], output[duplicate]);
      output.splice(duplicate, 1);
    }
  }
  return output;
}

export function fileReferenceHref(reference: string): string {
  return `${FILE_REF_SCHEME}${encodeURIComponent(reference)}`;
}

export function decodeFileReferenceHref(href: string): string | null {
  if (!href.startsWith(FILE_REF_SCHEME)) return null;
  try {
    return decodeURIComponent(href.slice(FILE_REF_SCHEME.length));
  } catch {
    return href.slice(FILE_REF_SCHEME.length);
  }
}

export function fileNameFromReference(reference: string): string {
  const withoutQuery = reference.split(/[?#]/)[0] || reference;
  const trimmed = withoutQuery.replace(/[\\/]+$/g, "");
  const name = trimmed.split(/[\\/]/).filter(Boolean).pop();
  return name || reference;
}

export function fileExtensionFromReference(reference: string): string {
  const decoded = decodeFileReferenceHref(reference) || reference;
  const fileName = fileNameFromReference(decoded);
  const match = fileName.match(/\.([a-z0-9]+)$/i);
  return match?.[1]?.toLowerCase() || "";
}

function isDiagramJsonReference(reference: string): boolean {
  const decoded = decodeFileReferenceHref(reference) || reference;
  return fileNameFromReference(decoded).toLowerCase().endsWith(".diagram.json");
}

function fileReferenceKindForExplicitType(fileType: string): FileReferenceKind | null {
  if (fileType === "diagram.json") return "diagram";
  return FILE_REFERENCE_KIND_BY_EXTENSION.get(fileType)
    || FILE_REFERENCE_KIND_BY_TYPE_ALIAS.get(fileType)
    || null;
}

export function isEditableDiagramReference(
  reference: string,
  fileType?: string,
): boolean {
  const decoded = decodeFileReferenceHref(reference) || reference;
  const fileName = fileNameFromReference(decoded).toLowerCase();
  const explicitType = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
  if (explicitType === "diagram.json" || explicitType === "diagram") return true;
  if (["mmd", "mermaid", "drawio"].includes(explicitType)) return false;
  if (explicitType === "json" && fileName.endsWith(".diagram.json")) return true;
  if (fileReferenceKindForExplicitType(explicitType)) return false;
  return fileName.endsWith(".diagram.json") || fileName.endsWith(".diagram");
}

export function fileReferenceKind(
  reference: string,
  mimeType?: string,
  fileType?: string,
): FileReferenceKind {
  const explicitType = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
  const referenceExtension = fileExtensionFromReference(reference);
  const explicitKind = fileReferenceKindForExplicitType(explicitType);
  if (explicitKind) {
    // Older editable diagrams were stored as JSON before diagram.json became
    // a durable file type. Keep that one compound-suffix compatibility case.
    if (explicitType === "json" && isDiagramJsonReference(reference)) return "diagram";
    return explicitKind;
  }
  if (
    isDiagramJsonReference(reference)
    || FILE_REFERENCE_KIND_BY_EXTENSION.get(referenceExtension) === "diagram"
  ) return "diagram";
  const mime = String(mimeType || "").split(";", 1)[0].trim().toLowerCase();
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  if (mime === "application/pdf") return "pdf";
  if (mime.includes("presentation") || mime.includes("powerpoint")) return "presentation";
  if (mime.includes("spreadsheet") || mime.includes("excel")) return "spreadsheet";
  if (mime.includes("wordprocessing") || mime === "application/msword") return "document";
  if (mime.startsWith("text/html")) return "page";
  if (mime.startsWith("text/") || mime.includes("json") || mime.includes("xml")) return "code";
  const extension = FILE_REFERENCE_KIND_BY_EXTENSION.has(explicitType)
    ? explicitType
    : referenceExtension;
  return FILE_REFERENCE_KIND_BY_EXTENSION.get(extension) || "file";
}

export function explicitDiagramIdentityOverridesGenericJson(
  fileType: unknown,
  explicitKind: unknown,
): boolean {
  const normalizedFileType = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
  return normalizedFileType === "json" && explicitKind === "diagram";
}

export enum OfficeEditorFormat {
  Unknown = "",
  Docx = "docx",
  Doc = "doc",
  Xlsx = "xlsx",
  Xls = "xls",
  Csv = "csv",
  Pptx = "pptx",
  Ppt = "ppt",
}

export type OfficeEditorFileFormat = OfficeEditorFormat;

export interface OfficeEditorFileDescriptor {
  format: OfficeEditorFormat;
  kind: Extract<FileReferenceKind, "document" | "spreadsheet" | "presentation" | "file">;
  requiresLegacyConversion: boolean;
  usesBinaryPackage: boolean;
}

const OFFICE_EDITOR_FORMAT_BY_VALUE = new Map<string, OfficeEditorFormat>([
  [OfficeEditorFormat.Docx, OfficeEditorFormat.Docx],
  [OfficeEditorFormat.Doc, OfficeEditorFormat.Doc],
  [OfficeEditorFormat.Xlsx, OfficeEditorFormat.Xlsx],
  [OfficeEditorFormat.Xls, OfficeEditorFormat.Xls],
  [OfficeEditorFormat.Csv, OfficeEditorFormat.Csv],
  [OfficeEditorFormat.Pptx, OfficeEditorFormat.Pptx],
  [OfficeEditorFormat.Ppt, OfficeEditorFormat.Ppt],
]);
const LEGACY_OFFICE_ALIAS_FORMATS = new Map<string, OfficeEditorFormat>([
  ["wps", OfficeEditorFormat.Doc],
  ["et", OfficeEditorFormat.Xls],
  ["dps", OfficeEditorFormat.Ppt],
]);
const LEGACY_OFFICE_FORMATS = new Set<OfficeEditorFormat>([
  OfficeEditorFormat.Doc,
  OfficeEditorFormat.Xls,
  OfficeEditorFormat.Ppt,
]);
const GENERIC_OFFICE_FILE_TYPES = new Set(["document", "spreadsheet", "presentation", "file"]);

function officeEditorFormatFromMime(value?: string): OfficeEditorFormat {
  const mime = String(value || "").split(";", 1)[0].trim().toLowerCase();
  if (mime === "application/vnd.openxmlformats-officedocument.wordprocessingml.document") return OfficeEditorFormat.Docx;
  if (mime === "application/msword") return OfficeEditorFormat.Doc;
  if (mime === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") return OfficeEditorFormat.Xlsx;
  if (mime === "application/vnd.ms-excel") return OfficeEditorFormat.Xls;
  if (mime === "text/csv") return OfficeEditorFormat.Csv;
  if (mime === "application/vnd.openxmlformats-officedocument.presentationml.presentation") return OfficeEditorFormat.Pptx;
  if (mime === "application/vnd.ms-powerpoint") return OfficeEditorFormat.Ppt;
  return OfficeEditorFormat.Unknown;
}

function officeEditorFormatFromValue(value?: string): OfficeEditorFormat {
  const normalized = String(value || "").trim().toLowerCase().replace(/^\./, "");
  return LEGACY_OFFICE_ALIAS_FORMATS.get(normalized)
    || OFFICE_EDITOR_FORMAT_BY_VALUE.get(normalized)
    || officeEditorFormatFromMime(normalized);
}

function officeEditorKind(format: OfficeEditorFormat): OfficeEditorFileDescriptor["kind"] {
  if (format === OfficeEditorFormat.Docx || format === OfficeEditorFormat.Doc) return "document";
  if ([OfficeEditorFormat.Xlsx, OfficeEditorFormat.Xls, OfficeEditorFormat.Csv].includes(format)) return "spreadsheet";
  if (format === OfficeEditorFormat.Pptx || format === OfficeEditorFormat.Ppt) return "presentation";
  return "file";
}

function officeEditorDescriptor(format: OfficeEditorFormat): OfficeEditorFileDescriptor {
  return {
    format,
    kind: officeEditorKind(format),
    requiresLegacyConversion: LEGACY_OFFICE_FORMATS.has(format),
    usesBinaryPackage: format !== OfficeEditorFormat.Unknown && format !== OfficeEditorFormat.Csv,
  };
}

/** One canonical factory for Office parser and legacy-conversion decisions. */
export class OfficeEditorFileFactory {
  static create(
    reference: string,
    mimeType?: string,
    fileType?: string,
  ): OfficeEditorFileDescriptor {
    const explicitType = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
    const explicitFormat = officeEditorFormatFromValue(explicitType);
    if (explicitFormat !== OfficeEditorFormat.Unknown) return officeEditorDescriptor(explicitFormat);

    const mimeFormat = officeEditorFormatFromMime(mimeType);
    const referenceFormat = officeEditorFormatFromValue(fileExtensionFromReference(reference));
    if (!explicitType) return officeEditorDescriptor(mimeFormat || referenceFormat);
    if (!GENERIC_OFFICE_FILE_TYPES.has(explicitType)) {
      return officeEditorDescriptor(OfficeEditorFormat.Unknown);
    }

    const persistedKind = fileReferenceKind("", undefined, explicitType);
    if (persistedKind === "file") return officeEditorDescriptor(mimeFormat || referenceFormat);
    if (mimeFormat !== OfficeEditorFormat.Unknown && officeEditorKind(mimeFormat) === persistedKind) {
      return officeEditorDescriptor(mimeFormat);
    }
    if (referenceFormat !== OfficeEditorFormat.Unknown && officeEditorKind(referenceFormat) === persistedKind) {
      return officeEditorDescriptor(referenceFormat);
    }
    return officeEditorDescriptor(OfficeEditorFormat.Unknown);
  }
}

/** Resolve the concrete Office parser from durable metadata, with name/MIME fallback. */
export function officeEditorFileFormat(
  reference: string,
  mimeType?: string,
  fileType?: string,
): OfficeEditorFileFormat {
  return OfficeEditorFileFactory.create(reference, mimeType, fileType).format;
}

export function fileReferenceTypeLabel(
  reference: string,
  mimeType?: string,
  fileType?: string,
): string {
  if (fileReferenceKind(reference, mimeType, fileType) === "diagram") return "DIA";
  const explicitType = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
  const extension = FILE_REFERENCE_KIND_BY_EXTENSION.has(explicitType)
    ? explicitType
    : fileExtensionFromReference(reference);
  if (!extension) {
    const kind = fileReferenceKind(reference, mimeType, fileType);
    if (kind === "pdf") return "PDF";
    if (kind === "presentation") return "PPT";
    if (kind === "spreadsheet") return "XLS";
    if (kind === "image") return "IMG";
    if (kind === "video") return "VIDEO";
    if (kind === "audio") return "AUDIO";
    if (kind === "document") return "DOC";
    if (kind === "code") return "CODE";
    if (kind === "page") return "WEB";
    return "FILE";
  }
  if (extension === "jpeg") return "JPG";
  if (extension === "markdown") return "MD";
  if (extension.length <= 5) return extension.toUpperCase();
  const kind = fileReferenceKind(reference, mimeType, fileType);
  if (kind === "presentation") return "PPT";
  if (kind === "spreadsheet") return "XLS";
  if (kind === "diagram") return "DIA";
  if (kind === "code") return "CODE";
  if (kind === "document") return "DOC";
  return "FILE";
}

/**
 * A reference we can actually open, as opposed to a filename someone typed.
 *
 * The prose linkifier used to accept anything matching `FILE_LIKE_RE` — a
 * known extension anywhere in the token. So "完全搜不到这个视频
 * two_minute_start_method_openable.mp4" became a file card: the user's own
 * complaint that a file was missing, rendered as a button to open it. Clicking
 * fell through to a document search that found nothing and dumped them on the
 * Knowledge page.
 *
 * A card is a promise that clicking opens the file, so it needs an address:
 *
 *   - `manor-file:` — already an encoded reference
 *   - `/viewer/<id>` — a document route
 *   - `/api/v1/fs/<entity>/<path>` — an entity filesystem path
 *   - an absolute http(s) URL whose path ends in a file
 *   - a bare 26-char ULID document id
 *
 * A bare `report.pdf`, or a relative `daily/report.pdf`, is a NAME. It may not
 * exist, may be one of several, and cannot be resolved without guessing.
 */
export function isOpenableFileReference(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  if (!trimmed) return false;

  if (decodeFileReferenceHref(trimmed)) return true;
  if (/^\/viewer\/[^/]+/.test(trimmed)) return true;
  if (/^\/api\/v1\/fs\/[^/]+\/.+/.test(trimmed)) return true;
  if (/^[0-9A-HJKMNP-TV-Z]{26}$/i.test(trimmed)) return true;

  if (/^https?:\/\//i.test(trimmed)) {
    try {
      const url = new URL(trimmed);
      return FILE_LIKE_RE.test(url.pathname);
    } catch {
      return false;
    }
  }
  return false;
}

export function looksLikeFileReference(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (decodeFileReferenceHref(trimmed)) return true;
  return FILE_LIKE_RE.test(trimmed.split(/[?#]/)[0] || trimmed);
}

function escapeMarkdownLabel(value: string): string {
  return value.replace(/([\\\]\[])/g, "\\$1");
}

function trimReferenceCandidate(value: string): string {
  return value.replace(/[.,;:!?，。；：！？、)\]}>'"“”’]+$/g, "").trim();
}

function standaloneInlineFileReference(value: string): string | null {
  const reference = trimReferenceCandidate(value);
  // Backticks change presentation, not identity. A filename such as
  // `report.pdf` is still only a name; without a viewer route, document id, or
  // returned file URL, turning it into a card would promise a destination the
  // message does not actually contain.
  return reference && isOpenableFileReference(reference) ? reference : null;
}

function linkifyPlainFileReferencesSegment(segment: string): string {
  return segment.replace(FILE_REF_RE, (full, prefix: string, candidate: string) => {
    const reference = trimReferenceCandidate(candidate);
    if (!isOpenableFileReference(reference)) return full;
    const label = escapeMarkdownLabel(fileNameFromReference(reference));
    return `${prefix}[${label}](${fileReferenceHref(reference)})`;
  });
}

function processOutsideInlineCode(segment: string): string {
  return segment
    .split(INLINE_CODE_RE)
    .map((part) => {
      if (!part.startsWith("`") || !part.endsWith("`")) {
        return linkifyPlainFileReferencesSegment(part);
      }
      const reference = standaloneInlineFileReference(part.slice(1, -1));
      if (!reference) return part;
      const label = escapeMarkdownLabel(fileNameFromReference(reference));
      return `[${label}](${fileReferenceHref(reference)})`;
    })
    .join("");
}

/**
 * Normalize a Markdown link whose destination is an entity filesystem path.
 *
 * Assistants often return an exact workspace URL such as
 * `[Open report.md](/api/v1/fs/.../project files/report.md)`. CommonMark does
 * not accept unescaped spaces. Even without spaces, a raw filesystem link
 * bypasses the file-card renderer. Convert both forms to the canonical
 * `manor-file:` handoff, keep a real human label, and replace an echoed raw
 * path label with the basename.
 */
function normalizeOpenableMarkdownFileLink(markdown: string): string {
  if (markdown.startsWith("!")) return markdown;
  const match = MARKDOWN_LINK_PARTS_RE.exec(markdown);
  if (!match) return markdown;
  const [, rawLabel, rawDestination] = match;
  if (MARKDOWN_LINK_TITLE_RE.test(rawDestination)) return markdown;
  const unwrapped = rawDestination.trim().replace(/^<([\s\S]*)>$/, "$1");
  const reference = unwrapped.replace(/\s+/gu, " ").trim();
  if (!ENTITY_FS_PATH_RE.test(reference) || !isOpenableFileReference(reference)) {
    return markdown;
  }
  const label = rawLabel.trim();
  const keepsLabel = Boolean(label)
    && !label.startsWith("/api/v1/fs/")
    && label !== reference;
  const finalLabel = keepsLabel ? label : fileNameFromReference(reference);
  return `[${escapeMarkdownLabel(finalLabel)}](${fileReferenceHref(reference)})`;
}

function processOutsideMarkdownLinks(segment: string): string {
  let cursor = 0;
  let output = "";
  segment.replace(MARKDOWN_LINK_OR_IMAGE_RE, (match, offset: number) => {
    output += processOutsideInlineCode(segment.slice(cursor, offset));
    output += normalizeOpenableMarkdownFileLink(match);
    cursor = offset + match.length;
    return match;
  });
  output += processOutsideInlineCode(segment.slice(cursor));
  return output;
}

export function linkifyFileReferencesInMarkdown(source: string): string {
  if (!source || !FILE_LIKE_RE.test(source)) return source;
  return source
    .split(FENCED_CODE_RE)
    .map((part) => {
      const isFence = part.startsWith("```") || part.startsWith("~~~");
      return isFence ? part : processOutsideMarkdownLinks(part);
    })
    .join("");
}
