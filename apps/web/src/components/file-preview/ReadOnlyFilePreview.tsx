import { lazy, Suspense, useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import MarkdownTable from "../MarkdownTable";
import { getFilePreviewKind } from "../../lib/filePreviewKind";
import { createFilePreviewSource, filePreviewMediaBlob } from "../../lib/filePreviewSource";
import { fetchFileBlob } from "../../lib/fileTransfer";
import { CsvPreview } from "./CsvPreview";
import {
  assertDiagramPreviewFileSize,
  readDiagramPreviewText,
} from "../../lib/diagram/previewLimits";
import { t } from "../../lib/i18n";
import OfficePreview, { DocxOfficePreview } from "./OfficePreview";
import type { ReadOnlyFilePreviewProps } from "./types";
import Button from "../ui/Button";
import ImagePreview from "./ImagePreview";

const LazyDiagramArtifactViewer = lazy(() => import("../diagram/DiagramArtifactViewer"));
const LazyXlsxViewer = lazy(() => import("../../pages/FileViewer").then(({ XlsxViewer }) => ({ default: XlsxViewer })));
const LazyPptxViewer = lazy(() => import("../../pages/FileViewer").then(({ PptxViewer }) => ({ default: PptxViewer })));
const LazyPdfJsViewer = lazy(() => import("../../pages/FileViewer").then(({ PdfJsViewer }) => ({ default: PdfJsViewer })));

const textKinds = new Set(["text", "markdown", "code", "html", "csv", "json", "diagram"]);

export default function ReadOnlyFilePreview({
  file,
  source,
  content,
  blob,
  canDownload = false,
  onDownload,
}: ReadOnlyFilePreviewProps) {
  const kind = getFilePreviewKind(file);
  const [fetchedContent, setFetchedContent] = useState<string | null>(content ?? null);
  const [contentError, setContentError] = useState("");
  const [loadedBlob, setLoadedBlob] = useState<Blob | null>(null);
  const [mediaUrl, setMediaUrl] = useState("");
  const [retryVersion, setRetryVersion] = useState(0);
  const previewContent = content ?? fetchedContent;

  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    const controller = new AbortController();
    setFetchedContent(content ?? null);
    setContentError("");
    setLoadedBlob(null);
    setMediaUrl("");
    if (kind === "unsupported" || (content != null && textKinds.has(kind))) return;
    void (async () => {
      try {
        if (kind === "diagram") assertDiagramPreviewFileSize(file.file_size);
        const binary = await createFilePreviewSource({
          blob, url: source.contentUrl,
          fetchBinary: source.fetchBinary || (kind === "diagram" ? async (signal) => {
            return (await fetchFileBlob(source.contentUrl, { signal }, fetch,
              async (response) => new Blob([await readDiagramPreviewText(response)]))).blob;
          } : undefined),
        }).getBlob(controller.signal);
        if (cancelled) return;
        setLoadedBlob(binary);
        if (textKinds.has(kind)) {
          if (kind === "diagram") assertDiagramPreviewFileSize(binary.size);
          const value = await binary.text();
          if (!cancelled) setFetchedContent(value);
        } else if (["image", "video", "audio"].includes(kind)) {
          objectUrl = URL.createObjectURL(filePreviewMediaBlob(binary, file));
          setMediaUrl(objectUrl);
        }
      } catch (cause) {
        if (!cancelled) setContentError(cause instanceof Error ? cause.message : t("page.shared_doc.preview_unavailable"));
      }
    })();
    return () => { cancelled = true; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [blob, content, kind, source.contentUrl, source.fetchBinary, file.name, file.mime_type, file.file_size, retryVersion]);

  if (kind === "unsupported") return <PreviewFallback message={t(canDownload ? "page.shared_doc.preview_unsupported_downloadable" : "page.shared_doc.preview_unsupported")} />;
  if (contentError) return <div role="alert"><PreviewFallback message={contentError} /><Button variant="outline" onClick={() => setRetryVersion((value) => value + 1)}>{t("component.workflow_run.action.retry")}</Button></div>;
  if (!textKinds.has(kind) && !loadedBlob) return <PreviewLoading />;

  if (kind === "docx") return <DocxOfficePreview name={file.name || "Document"} source={source} blob={loadedBlob} />;
  if (kind === "pptx") {
    return (
      <OfficePreview
        kind="pptx"
        name={file.name || "Presentation"}
        source={source}
        fallback={(
          <Suspense fallback={<PreviewLoading />}>
            <LazyPptxViewer url="" blob={loadedBlob} />
          </Suspense>
        )}
      />
    );
  }
  if (kind === "xlsx") {
    return <Suspense fallback={<PreviewLoading />}><LazyXlsxViewer url="" blob={loadedBlob} /></Suspense>;
  }
  if (kind === "pdf") {
    return <Suspense fallback={<PreviewLoading />}><LazyPdfJsViewer url="" blob={loadedBlob} docName={file.name || "PDF"} canEdit={false} onDownload={canDownload ? onDownload : undefined} /></Suspense>;
  }
  if (kind === "image") {
    return <ImagePreview src={mediaUrl} alt={file.name || "Image"} onError={() => setContentError(t("page.shared_doc.preview_unavailable"))} />;
  }
  if (kind === "video") {
    return <video src={mediaUrl} controls onError={() => setContentError(t("page.shared_doc.preview_unavailable"))} controlsList={canDownload ? undefined : "nodownload"} style={{ width: "100%", maxHeight: "75vh", background: "#000" }} />;
  }
  if (kind === "audio") {
    return <audio src={mediaUrl} controls onError={() => setContentError(t("page.shared_doc.preview_unavailable"))} controlsList={canDownload ? undefined : "nodownload"} style={{ width: "100%" }} />;
  }
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
