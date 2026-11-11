import { useEffect, useState, type ReactNode } from "react";
import DocxReadOnlyViewer from "../DocxReadOnlyViewer";
import { t } from "../../lib/i18n";
import { withFilePreviewTimeout } from "../../lib/filePreviewSource";
import type { PreviewPage, ReadOnlyPreviewSource } from "./types";

interface OfficePreviewProps {
  kind: "docx" | "pptx";
  name: string;
  source: ReadOnlyPreviewSource;
  fallback?: ReactNode;
}

export default function OfficePreview({ kind, name, source, fallback }: OfficePreviewProps) {
  const [pages, setPages] = useState<PreviewPage[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setPages(null);
    setError(false);
    const load = kind === "docx" ? source.getPages : source.getSlides;
    if (!load) {
      setError(true);
      return () => { cancelled = true; };
    }
    void withFilePreviewTimeout<{ pages: PreviewPage[] } | { slides: PreviewPage[] }>(load(), 30_000, "Office preview timed out")
      .then((result) => {
        if (cancelled) return;
        if (!("pages" in result ? result.pages : result.slides).length) { setError(true); return; }
        setPages("pages" in result ? result.pages : result.slides);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => { cancelled = true; };
  }, [kind, source]);

  if (pages == null && !error) return <PreviewLoading />;
  if (error || !pages?.length) {
    if (fallback) return <>{fallback}</>;
    return <PreviewFallback />;
  }

  const label = kind === "docx" ? "page" : "slide";
  return (
    <div style={{ display: "grid", gap: 16 }}>
      {pages.map((page) => (
        <img
          key={page.index}
          src={page.url}
          alt={`${name}, ${label} ${page.index + 1}`}
          width={page.width || undefined}
          height={page.height || undefined}
          loading={page.index === 0 ? "eager" : "lazy"}
          onError={() => setError(true)}
          style={{ display: "block", width: "100%", height: "auto", margin: "0 auto", border: "0.5px solid rgba(29, 29, 31,0.06)" }}
        />
      ))}
    </div>
  );
}

export function DocxOfficePreview({ name, source, blob }: Pick<OfficePreviewProps, "name" | "source"> & { blob?: Blob | null }) {
  return <OfficePreview kind="docx" name={name} source={source} fallback={<DocxReadOnlyViewer blob={blob} url={source.contentUrl} />} />;
}

function PreviewLoading() {
  return <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.loading")}</p>;
}

function PreviewFallback() {
  return <p style={{ color: "var(--text-muted)", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.preview_unavailable")}</p>;
}
