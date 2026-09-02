import { useEffect, useState } from "react";
import { IconChevronLeft, IconChevronRight } from "../icons";
import { t } from "../../lib/i18n";
import { createRenderedDiagramArtifactPreview } from "../../lib/diagram/artifactPreview";

export default function DiagramArtifactViewer({
  content,
  title,
  fileType,
}: {
  content: string;
  title: string;
  fileType?: string;
}) {
  const [previewPages, setPreviewPages] = useState<Array<{ title: string; url: string }>>([]);
  const [activePage, setActivePage] = useState(0);
  const [previewError, setPreviewError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    let objectUrls: string[] = [];
    setLoading(true);
    setPreviewPages([]);
    setActivePage(0);
    setPreviewError("");
    void createRenderedDiagramArtifactPreview(content, title, fileType).then((preview) => {
      if (cancelled) return;
      if (preview.error || !preview.svg) {
        setPreviewError(preview.error || "Diagram preview failed");
        setLoading(false);
        return;
      }
      const pages = preview.pages.length
        ? preview.pages
        : [{ title, svg: preview.svg }];
      objectUrls = pages.map((page) => URL.createObjectURL(
        new Blob([page.svg], { type: "image/svg+xml;charset=utf-8" }),
      ));
      setPreviewPages(pages.map((page, index) => ({
        title: page.title,
        url: objectUrls[index],
      })));
      setLoading(false);
    });
    return () => {
      cancelled = true;
      objectUrls.forEach((objectUrl) => URL.revokeObjectURL(objectUrl));
    };
  }, [content, fileType, title]);

  if (previewError) {
    return (
      <div className="chat-output-file-missing">
        <p>{t("component.embedded_chat.file_preview_failed")}</p>
        <span>{previewError}</span>
      </div>
    );
  }

  const currentPage = previewPages[activePage];

  if (loading || !currentPage) {
    return (
      <div className="chat-output-file-loading">
        <span className="chat-tool-spinner" />
        <p>{t("component.embedded_chat.loading_file_preview")}</p>
      </div>
    );
  }

  return (
    <div className="chat-output-diagram-viewer">
      <div className="chat-output-diagram-canvas">
        <img src={currentPage.url} alt={`${title} — ${currentPage.title}`} />
      </div>
      {previewPages.length > 1 && (
        <div className="chat-output-ppt-navigation chat-output-diagram-navigation">
          <button
            type="button"
            onClick={() => setActivePage((page) => Math.max(0, page - 1))}
            disabled={activePage === 0}
            aria-label={t("component.embedded_chat.previous_diagram_page")}
          >
            <IconChevronLeft size={16} />
          </button>
          <span aria-live="polite">
            {t("component.embedded_chat.diagram_page")} {activePage + 1} {t("page.file_viewer.of")} {previewPages.length}
          </span>
          <button
            type="button"
            onClick={() => setActivePage((page) => Math.min(previewPages.length - 1, page + 1))}
            disabled={activePage === previewPages.length - 1}
            aria-label={t("component.embedded_chat.next_diagram_page")}
          >
            <IconChevronRight size={16} />
          </button>
        </div>
      )}
    </div>
  );
}
