import { lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import CollapsibleSentMessage from "./chat/CollapsibleSentMessage";
import { CsvPreview } from "./file-preview/CsvPreview";

// Markdown rendering loads only when a written result is shown.
const ChatMarkdown = lazy(() => import("./ChatMarkdown"));

// Bounded so a very large result never stalls the page; the file card opens the rest.
const INLINE_TEXT_LIMIT = 60_000;

export type InlineDocumentTextKind = "markdown" | "table";

/** Written results (Markdown, plain text) and tables (CSV, TSV) read in place. */
export function inlineDocumentTextKind(name?: string | null, fileType?: string | null): InlineDocumentTextKind | null {
  const extension = String(fileType || String(name || "").split(".").pop() || "").toLowerCase().replace(/^\./, "");
  if (["md", "markdown", "txt"].includes(extension)) return "markdown";
  if (["csv", "tsv"].includes(extension)) return "table";
  return null;
}

/**
 * A Knowledge document's content shown inline: a short document or table in
 * full, a long one from its first lines until expanded (see ``contentFold``).
 * ``live`` refetches a document that is edited in place (a pinned list);
 * delivered results are immutable and stay cached.
 */
export default function InlineDocumentText({ documentId, kind, label, live = false, open = false }: {
  documentId: string;
  kind: InlineDocumentTextKind;
  /** Names the content as a region; omit it when a file header around it already does. */
  label?: string;
  live?: boolean;
  /** Shown whole, as where the result was opened to be read. */
  open?: boolean;
}) {
  const text = useQuery({
    queryKey: ["inline-document-text", documentId, live],
    queryFn: async () => (await api.documents.previewBlob(documentId, live ? { force: true } : { cache: true })).text(),
    staleTime: live ? 0 : Infinity,
    retry: false,
  });
  if (text.data === undefined) {
    return <p className="inline-document-text__state" role={text.isError ? "alert" : "status"}>
      {text.isError ? t("component.result_files.preview_failed") : t("component.result_files.loading_preview")}
    </p>;
  }
  const content = text.data.length > INLINE_TEXT_LIMIT ? text.data.slice(0, INLINE_TEXT_LIMIT) : text.data;
  const Region = label ? "section" : "div";
  return (
    <Region className={`inline-document-text inline-document-text--${kind}`} aria-label={label}>
      {kind === "table"
        ? <CsvPreview content={content} compact />
        : <div className="inline-document-text__body">
          <CollapsibleSentMessage text={content} tone="assistant" kind="document" enabled={!open}>
            <Suspense fallback={<pre className="inline-document-text__raw">{content}</pre>}>
              <ChatMarkdown content={content} enableFileCards={false} preserveReferences />
            </Suspense>
          </CollapsibleSentMessage>
        </div>}
    </Region>
  );
}
