import { useEffect, useRef, useState, type CSSProperties } from "react";
import LoadingSpinner from "./ui/LoadingSpinner";
import { t } from "../lib/i18n";
import { createFilePreviewSource, withFilePreviewTimeout } from "../lib/filePreviewSource";
import { sanitizeDocumentHtml, sanitizeManorDocumentRender } from "../lib/sanitizeDocumentHtml";
import {
  paginateManorDocument,
  renderManorDocument,
  type ManorDocumentRender,
} from "../lib/manorDocumentEngine";

export default function DocxReadOnlyViewer({
  blob,
  url,
  afterRender,
}: {
  blob?: Blob | null;
  url?: string;
  afterRender?: (root: HTMLElement) => void;
}) {
  const [html, setHtml] = useState("");
  const [render, setRender] = useState<ManorDocumentRender | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const previewRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setHtml("");
    setRender(null);

    (async () => {
      try {
        const buffer = await createFilePreviewSource({ blob, url }).getArrayBuffer(controller.signal);
        if (cancelled) return;
        const bytes = new Uint8Array(buffer);
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const rendered = await withFilePreviewTimeout(renderManorDocument(buffer), 30_000, "Word preview timed out. Please retry.");
          if (cancelled) return;
          const sanitizeOptions = { allowDocxEditorAttributes: true, allowDocxLayoutStyles: true };
          const safeRender = sanitizeManorDocumentRender(rendered, sanitizeOptions);
          setRender(safeRender);
          setHtml(safeRender.html);
        } else {
          const legacyHtml = new TextDecoder().decode(buffer);
          if (!/^\s*<(?:!doctype|html|body|div|p|h[1-6]|table)\b/i.test(legacyHtml)) {
            throw new Error(t("page.shared_doc.preview_unavailable"));
          }
          setHtml(sanitizeDocumentHtml(legacyHtml));
        }
      } catch (reason: any) {
        if (!cancelled) setError(reason.message || "Failed to render DOCX");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; controller.abort(); };
  }, [blob, url]);

  useEffect(() => {
    const root = previewRef.current;
    if (!root || !html) return;
    root.innerHTML = html;
    if (render) paginateManorDocument(root, render);
    afterRender?.(root);
  }, [afterRender, html, render]);

  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 64 }}><LoadingSpinner size={28} /></div>;
  if (error) return <p role="alert" style={{ color: "var(--danger, #c14a44)", textAlign: "center", padding: 32 }}>{error}</p>;

  return (
    <div className="docx-viewer-stage">
      <div
        ref={previewRef}
        className={render ? "docx-viewer-page manor-docx-native manor-docx-readonly" : "docx-preview docx-viewer-page"}
        style={render ? {
          "--docx-page-width": `${render.layout.pageWidthPx}px`,
          "--docx-page-height": `${render.layout.pageHeightPx}px`,
          "--docx-margin-top": `${render.layout.marginTopPx}px`,
          "--docx-margin-right": `${render.layout.marginRightPx}px`,
          "--docx-margin-bottom": `${render.layout.marginBottomPx}px`,
          "--docx-margin-left": `${render.layout.marginLeftPx}px`,
          "--docx-header-distance": `${render.layout.headerDistancePx}px`,
          "--docx-footer-distance": `${render.layout.footerDistancePx}px`,
        } as CSSProperties : undefined}
      />
    </div>
  );
}
