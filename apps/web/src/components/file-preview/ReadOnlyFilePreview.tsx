import { lazy, Suspense, useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import MarkdownTable from "../MarkdownTable";
import { getFilePreviewKind } from "../../lib/filePreviewKind";
import { parseDelimitedText } from "../../lib/delimitedText";
import {
  assertDiagramPreviewFileSize,
  readDiagramPreviewText,
} from "../../lib/diagram/previewLimits";
import { t } from "../../lib/i18n";
import OfficePreview, { DocxOfficePreview } from "./OfficePreview";
import type { ReadOnlyFilePreviewProps } from "./types";

const LazyDiagramArtifactViewer = lazy(() => import("../diagram/DiagramArtifactViewer"));
const LazyXlsxViewer = lazy(() => import("../../pages/FileViewer").then(({ XlsxViewer }) => ({ default: XlsxViewer })));
const LazyPptxViewer = lazy(() => import("../../pages/FileViewer").then(({ PptxViewer }) => ({ default: PptxViewer })));

const textKinds = new Set(["text", "markdown", "code", "html", "csv", "json", "diagram"]);

export default function ReadOnlyFilePreview({
  file,
  source,
  content,
  blob,
  canDownload = false,
}: ReadOnlyFilePreviewProps) {
  const kind = getFilePreviewKind(file);
  const [fetchedContent, setFetchedContent] = useState<string | null>(content ?? null);
  const [contentError, setContentError] = useState(false);
  const previewContent = content ?? fetchedContent;

  useEffect(() => {
    let cancelled = false;
    setFetchedContent(content ?? null);
    setContentError(false);
    if (content != null || !textKinds.has(kind)) return () => { cancelled = true; };
    void (async () => {
      try {
        const response = await fetch(source.contentUrl);
        if (!response.ok) throw new Error("Preview content is unavailable");
        const value = kind === "diagram" ? await readDiagramPreviewText(response) : await response.text();
        if (!cancelled) setFetchedContent(value);
      } catch {
        if (!cancelled) setContentError(true);
      }
    })();
    return () => { cancelled = true; };
  }, [content, kind, source.contentUrl]);

  if (kind === "docx") return <DocxOfficePreview name={file.name || "Document"} source={source} />;
  if (kind === "pptx") {
    return (
      <OfficePreview
        kind="pptx"
        name={file.name || "Presentation"}
        source={source}
        fallback={(
          <Suspense fallback={<PreviewLoading />}>
            <LazyPptxViewer url={source.contentUrl} blob={blob} />
          </Suspense>
        )}
      />
    );
  }
  if (kind === "xlsx") {
    return <Suspense fallback={<PreviewLoading />}><LazyXlsxViewer url={source.contentUrl} blob={blob} /></Suspense>;
  }
  if (kind === "pdf") {
    return <iframe title={file.name || "PDF"} src={source.contentUrl} style={{ width: "100%", height: "75vh", border: "1px solid rgba(28,25,23,0.06)" }} />;
  }
  if (kind === "image") {
    return <img src={source.contentUrl} alt={file.name || "Image"} style={{ maxWidth: "100%", display: "block", margin: "0 auto" }} />;
  }
  if (kind === "video") {
    return <video src={source.contentUrl} controls controlsList={canDownload ? undefined : "nodownload"} style={{ width: "100%", maxHeight: "75vh", background: "#000" }} />;
  }
  if (kind === "audio") {
    return <audio src={source.contentUrl} controls controlsList={canDownload ? undefined : "nodownload"} style={{ width: "100%" }} />;
  }
  if (kind === "unsupported") return <PreviewFallback message={t("page.shared_doc.preview_unsupported")} />;
  if (contentError) return <PreviewFallback />;
  if (previewContent == null) return <PreviewLoading />;

  if (kind === "markdown") {
    return <div className="markdown-body" style={{ fontSize: 14, lineHeight: 1.7, color: "#292524" }}><Markdown remarkPlugins={[remarkGfm]} components={{ table: ({ children }) => <MarkdownTable>{children}</MarkdownTable> }}>{previewContent}</Markdown></div>;
  }
  if (kind === "html") {
    return <iframe title={file.name || "HTML preview"} srcDoc={previewContent} sandbox="" referrerPolicy="no-referrer" style={{ width: "100%", minHeight: "75vh", border: "1px solid rgba(28,25,23,0.06)" }} />;
  }
  if (kind === "csv") return <CsvPreview content={previewContent} />;
  if (kind === "json") return <CodePreview content={formatJson(previewContent)} />;
  if (kind === "diagram") {
    try {
      assertDiagramPreviewFileSize(file.file_size);
    } catch {
      return <PreviewFallback />;
    }
    return <Suspense fallback={<PreviewLoading />}><LazyDiagramArtifactViewer content={previewContent} title={file.name || "Diagram"} fileType={file.file_type || undefined} /></Suspense>;
  }
  return <CodePreview content={previewContent} />;
}

function CsvPreview({ content }: { content: string }) {
  const rows = parseDelimitedText(content).rows;
  if (!rows.length) return <PreviewFallback />;
  const columnCount = Math.max(1, ...rows.map((row) => row.length));
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="glass-table">
        <thead><tr>{Array.from({ length: columnCount }, (_, index) => <th key={index}>{rows[0]?.[index] ?? ""}</th>)}</tr></thead>
        <tbody>{rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{Array.from({ length: columnCount }, (_, columnIndex) => <td key={columnIndex} style={{ whiteSpace: "pre-wrap" }}>{row[columnIndex] ?? ""}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function CodePreview({ content }: { content: string }) {
  return <pre style={{ margin: 0, padding: 16, background: "#fafaf9", overflowX: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", color: "#292524" }}><code>{content}</code></pre>;
}

function formatJson(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

function PreviewLoading() {
  return <p style={{ color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.loading")}</p>;
}

function PreviewFallback({ message = t("page.shared_doc.preview_unavailable") }: { message?: string }) {
  return <p style={{ color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>{message}</p>;
}
