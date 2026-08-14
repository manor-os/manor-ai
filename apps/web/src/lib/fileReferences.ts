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
  presentation: ["ppt", "pptx", "key", "odp"],
  pdf: ["pdf"],
  spreadsheet: ["xls", "xlsx", "xlsm", "csv", "tsv", "ods", "numbers"],
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
  document: ["doc", "docx", "odt", "md", "markdown", "txt", "rtf", "tex", "epub", "mobi"],
};

const FILE_REFERENCE_EXTENSIONS = Array.from(
  new Set(Object.values(FILE_REFERENCE_EXTENSIONS_BY_KIND).flat()),
);
const FILE_REFERENCE_KIND_BY_EXTENSION = new Map<string, FileReferenceKind>(
  Object.entries(FILE_REFERENCE_EXTENSIONS_BY_KIND).flatMap(([kind, extensions]) => (
    extensions.map((extension) => [extension, kind as FileReferenceKind])
  )),
);

const FILE_EXT_PATTERN = FILE_REFERENCE_EXTENSIONS.join("|");
const FILE_REF_SCHEME = "manor-file:";

const FILE_REF_RE = new RegExp(
  String.raw`(^|[\s([{<'"“‘，。；：、])((?:\/)?(?:(?:[A-Za-z0-9_.~\-\u4e00-\u9fff]+|[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ]*[A-Za-z0-9_.~\-\u4e00-\u9fff])\/)*(?:[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ()（）\[\]【】+&,'’]*\.(${FILE_EXT_PATTERN})))(?=$|[\s)\]}>'"“”’。，、；:：!?！？|])`,
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

/**
 * Build the one canonical in-app destination for a known Document record.
 *
 * Callers that already have a structured document id must never downgrade it
 * to a filename and search again: exact ids are both faster and unambiguous.
 */
export function viewerPathForDocumentId(documentId: unknown): string | null {
  const value = String(documentId ?? "").trim();
  return value ? `/viewer/${encodeURIComponent(value)}` : null;
}

type GeneratedFileRecord = Record<string, unknown>;

const GENERATED_FILE_DOCUMENT_ID_KEYS = ["document_id", "documentId", "doc_id"] as const;
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
  const documentId = generatedFileText(record, GENERATED_FILE_DOCUMENT_ID_KEYS);
  if (documentId) return documentId;
  const id = String(record.id ?? "").trim();
  return id && (record.mime_type || record.mimeType || record.file_type || record.fileType)
    ? id
    : "";
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
  const explicit = generatedFileText(record, ["open_url", "openUrl", "viewer_url", "viewerUrl"]);
  if (explicit) {
    const legacyViewer = explicit.match(/^\/viewer\/([^/?#]+)/);
    if (legacyViewer?.[1] && !documentId) {
      const decoded = decodePathPart(legacyViewer[1]);
      const legacyPath = canonicalGeneratedFilePath(decoded);
      if (legacyPath && /[/.]/.test(decoded)) return legacyPath;
    }
    return explicit;
  }
  const viewerPath = viewerPathForDocumentId(documentId);
  if (viewerPath) return viewerPath;
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
  return "";
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

export function fileReferenceKind(
  reference: string,
  mimeType?: string,
  fileType?: string,
): FileReferenceKind {
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
  const explicitType = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
  const extension = FILE_REFERENCE_KIND_BY_EXTENSION.has(explicitType)
    ? explicitType
    : fileExtensionFromReference(reference);
  return FILE_REFERENCE_KIND_BY_EXTENSION.get(extension) || "file";
}

export function fileReferenceTypeLabel(
  reference: string,
  mimeType?: string,
  fileType?: string,
): string {
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
