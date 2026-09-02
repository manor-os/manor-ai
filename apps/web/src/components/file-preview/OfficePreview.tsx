import { useEffect, useState, type ReactNode } from "react";
import DocxReadOnlyViewer from "../DocxReadOnlyViewer";
import { t } from "../../lib/i18n";
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
    void load()
      .then((result) => {
        if (cancelled || !("pages" in result ? result.pages : result.slides).length) return;
        setPages("pages" in result ? result.pages : result.slides);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => { cancelled = true; };
  }, [kind, source]);

  if (pages == null && !error) return <PreviewLoading />;
  if (!pages?.length) {
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
          style={{ display: "block", width: "100%", height: "auto", margin: "0 auto", border: "1px solid rgba(28,25,23,0.06)" }}
        />
      ))}
    </div>
  );
}

export function DocxOfficePreview({ name, source }: Pick<OfficePreviewProps, "name" | "source">) {
  return <OfficePreview kind="docx" name={name} source={source} fallback={<DocxReadOnlyViewer url={source.contentUrl} />} />;
}

function PreviewLoading() {
  return <p style={{ color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.loading")}</p>;
}

function PreviewFallback() {
  return <p style={{ color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>{t("page.shared_doc.preview_unavailable")}</p>;
}
