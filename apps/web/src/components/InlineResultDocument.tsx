import { useMemo } from "react";
import { api } from "../lib/api";
import { getFilePreviewKind } from "../lib/filePreviewKind";
import ReadOnlyFilePreview from "./file-preview/ReadOnlyFilePreview";
import type { ReadOnlyPreviewSource } from "./file-preview/types";

// What reads or plays in place in full: a deck, a page document, a sheet, a web page, text or a recording.
const INLINE_KINDS = new Set(["pptx", "pdf", "docx", "xlsx", "html", "markdown", "csv", "text", "audio"]);

/** Whether a result file is a document that can be shown whole where the result is. */
export function isInlineResultDocument(file: { name?: string | null; mimeType?: string | null; fileType?: string | null }): boolean {
  return INLINE_KINDS.has(getFilePreviewKind({ name: file.name, mime_type: file.mimeType, file_type: file.fileType }));
}

/**
 * A result's document, the whole of it, where the result is shown: every slide
 * of a deck, every page of a PDF, the web page it built, the text it wrote. It
 * loads through the access-checked preview endpoint, the way the viewer does.
 */
export default function InlineResultDocument({ documentId, name, mimeType, fileType }: {
  documentId: string;
  name: string;
  mimeType?: string | null;
  fileType?: string | null;
}) {
  const source = useMemo<ReadOnlyPreviewSource>(() => ({
    contentUrl: "",
    fetchBinary: (signal) => api.documents.previewBlob(documentId, { signal, cache: true }),
  }), [documentId]);
  return <div className="inline-result-document" aria-label={name}>
    <ReadOnlyFilePreview file={{ name, mime_type: mimeType, file_type: fileType }} source={source} />
  </div>;
}
