import { lazy, Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
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
 * A Knowledge document's content shown inline, clamped until expanded.
 * ``live`` refetches a document that is edited in place (a pinned list);
 * delivered results are immutable and stay cached.
 */
export default function InlineDocumentText({ documentId, kind, label, live = false }: {
  documentId: string;
  kind: InlineDocumentTextKind;
  label: string;
  live?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const text = useQuery({
    queryKey: ["inline-document-text", documentId, live],
    queryFn: async () => (await api.documents.previewBlob(documentId, live ? { force: true } : { cache: true })).text(),
    staleTime: live ? 0 : Infinity,
    retry: false,
  });
  if (!text.data) {
    return <p className="inline-document-text__state" role={text.isError ? "alert" : "status"}>
      {text.isError ? t("component.result_files.preview_failed") : t("component.result_files.loading_preview")}
    </p>;
  }
  const content = text.data.length > INLINE_TEXT_LIMIT ? text.data.slice(0, INLINE_TEXT_LIMIT) : text.data;
  return (
    <section className={`inline-document-text${expanded ? " is-expanded" : ""}`} aria-label={label}>
      <div className="inline-document-text__body">
        {kind === "table"
          ? <CsvPreview content={content} />
          : <Suspense fallback={<pre className="inline-document-text__raw">{content}</pre>}>
            <ChatMarkdown content={content} enableFileCards={false} />
          </Suspense>}
      </div>
      <button type="button" className="inline-document-text__toggle" onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}>
        {expanded ? t("component.result_files.show_less") : t("component.result_files.show_all")}
      </button>
    </section>
  );
}
