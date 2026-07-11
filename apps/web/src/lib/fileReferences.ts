const FILE_REFERENCE_EXTENSIONS = [
  "ppt", "pptx", "pdf", "doc", "docx", "xls", "xlsx", "csv", "tsv",
  "md", "markdown", "txt", "rtf", "html", "htm", "css", "js", "jsx", "ts", "tsx",
  "py", "sql", "json", "yaml", "yml", "png", "jpg", "jpeg", "webp", "gif", "svg",
  "mmd", "mermaid", "drawio", "diagram", "mp4", "mov", "webm", "m4v", "mp3", "wav", "m4a", "aac", "ogg", "flac",
];

const FILE_EXT_PATTERN = FILE_REFERENCE_EXTENSIONS.join("|");
const FILE_REF_SCHEME = "manor-file:";

const FILE_REF_RE = new RegExp(
  String.raw`(^|[\s([{<'"“‘，。；：、])((?:\/)?(?:(?:[A-Za-z0-9_.~\-\u4e00-\u9fff]+|[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ]*[A-Za-z0-9_.~\-\u4e00-\u9fff])\/)*(?:[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ()（）\[\]【】+&,'’]*\.(${FILE_EXT_PATTERN})))(?=$|[\s)\]}>'"“”’。，、；:：!?！？|])`,
  "giu",
);

const STANDALONE_FILE_REF_RE = new RegExp(
  String.raw`^(?:\/)?(?:(?:[A-Za-z0-9_.~\-\u4e00-\u9fff]+|[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ]*[A-Za-z0-9_.~\-\u4e00-\u9fff])\/)*(?:[A-Za-z0-9_.~\-\u4e00-\u9fff][A-Za-z0-9_.~\-\u4e00-\u9fff ()（）\[\]【】+&,'’]*\.(${FILE_EXT_PATTERN}))(?:[?#].*)?$`,
  "iu",
);

const FILE_LIKE_RE = new RegExp(
  String.raw`\.(${FILE_EXT_PATTERN})(?=$|[?#\s)\]}>'"“”’。，、；:：!?！？|` + "`" + String.raw`])`,
  "iu",
);
const MARKDOWN_LINK_OR_IMAGE_RE = /!?\[[^\]]*\]\([^)]*\)/g;
const FENCED_CODE_RE = /(```[\s\S]*?```|~~~[\s\S]*?~~~)/g;
const INLINE_CODE_RE = /(`[^`\n]+`)/g;

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
  if (!reference || !looksLikeFileReference(reference)) return null;

  if (decodeFileReferenceHref(reference)) return reference;

  try {
    const url = new URL(reference);
    if (/^https?:$/i.test(url.protocol) && FILE_LIKE_RE.test(url.pathname)) {
      return reference;
    }
  } catch {
    // Not an absolute URL; fall through to local/API path handling.
  }

  const hasPathMarker =
    /^(?:\/|\.{1,2}\/|[A-Za-z]:[\\/])/.test(reference) ||
    /[\\/]/.test(reference);
  if (/\s/.test(reference) && !hasPathMarker) return null;

  return STANDALONE_FILE_REF_RE.test(reference) ? reference : null;
}

function linkifyPlainFileReferencesSegment(segment: string): string {
  return segment.replace(FILE_REF_RE, (full, prefix: string, candidate: string) => {
    const reference = trimReferenceCandidate(candidate);
    if (!looksLikeFileReference(reference)) return full;
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

function processOutsideMarkdownLinks(segment: string): string {
  let cursor = 0;
  let output = "";
  segment.replace(MARKDOWN_LINK_OR_IMAGE_RE, (match, offset: number) => {
    output += processOutsideInlineCode(segment.slice(cursor, offset));
    output += match;
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
