import { useEffect, useRef, useState } from "react";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import "./ImagePreview.css";

/** Enlarges the same local image URL without downloading another copy. */
export default function ImagePreview({ src, alt, imageClassName, onError }: {
  src: string;
  alt: string;
  imageClassName?: string;
  onError?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className="image-preview-trigger" aria-label={`${t("component.image_preview.enlarge")} ${alt}`} onClick={() => setOpen(true)}>
      <img src={src} alt={alt} className={imageClassName} onError={onError} />
      <span className="image-preview-hint">{t("component.image_preview.enlarge")}</span>
    </button>
    {open && <ImagePreviewDialog key={src} src={src} alt={alt} onClose={() => setOpen(false)} onError={onError} />}
  </>;
}

function ImagePreviewDialog({ src, alt, onClose, onError }: {
  src: string; alt: string; onClose: () => void; onError?: () => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [area, setArea] = useState({ width: 0, height: 0 });
  const [scale, setScale] = useState<number | "fit">("fit");
  const fit = size.width && size.height && area.width && area.height
    ? Math.min(1, Math.max(1, area.width - 32) / size.width, Math.max(1, area.height - 32) / size.height) : 1;
  const zoom = scale === "fit" ? fit : scale;
  const minimum = Math.min(fit, 0.1);
  const ready = size.width > 0 && area.width > 0;

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setArea({ width: element.clientWidth, height: element.clientHeight }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  function changeScale(next: number | "fit") {
    setScale(next);
    // Start at the top so screenshot headings remain reachable at every scale.
    viewport.current?.scrollTo(0, 0);
  }

  return <Modal open title={alt} onClose={onClose} maxWidth="none" height="calc(100dvh - 48px)" className="image-preview-dialog" bodyClassName="image-preview-dialog-body">
    <div className="image-preview-toolbar">
      <Button variant="ghost" ariaLabel={t("page.doc_editor.zoom_out")} disabled={!ready || zoom <= minimum} onClick={() => changeScale(Math.max(minimum, zoom / 1.25))}>−</Button>
      <output className="image-preview-scale" aria-live="polite">{Math.round(zoom * 100)}%</output>
      <Button variant="ghost" ariaLabel={t("page.doc_editor.zoom_in")} disabled={!ready || zoom >= 4} onClick={() => changeScale(Math.min(4, zoom * 1.25))}>+</Button>
      <Button variant="ghost" disabled={!ready} ariaPressed={scale === 1} ariaLabel={t("component.image_preview.actual_size")} title={t("component.image_preview.actual_size")} onClick={() => changeScale(1)}>100%</Button>
      <Button variant="ghost" disabled={!ready} ariaPressed={scale === "fit"} onClick={() => changeScale("fit")}>{t("component.image_preview.fit")}</Button>
    </div>
    <div ref={viewport} className={`image-preview-viewport${dragging ? " is-dragging" : ""}`} role="region" aria-label={alt} tabIndex={0}
      onPointerDown={(event) => {
        if (event.pointerType !== "mouse" || event.button !== 0) return;
        const element = event.currentTarget;
        if (element.scrollWidth <= element.clientWidth && element.scrollHeight <= element.clientHeight) return;
        event.preventDefault();
        element.focus();
        element.setPointerCapture(event.pointerId);
        drag.current = { x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop };
        setDragging(true);
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        event.currentTarget.scrollLeft = drag.current.left + drag.current.x - event.clientX;
        event.currentTarget.scrollTop = drag.current.top + drag.current.y - event.clientY;
      }}
      onPointerUp={(event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
        drag.current = null;
        setDragging(false);
      }}
      onLostPointerCapture={() => { drag.current = null; setDragging(false); }}
    >
      <div className="image-preview-canvas" style={{ width: Math.max(area.width, size.width * zoom + 32), height: Math.max(area.height, size.height * zoom + 32) }}>
        <img src={src} alt={alt} draggable={false} onError={onError} onLoad={(event) => setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
          style={{ width: size.width ? size.width * zoom : "auto", height: size.height ? size.height * zoom : "auto", visibility: ready ? "visible" : "hidden" }} />
      </div>
    </div>
    <p className="image-preview-help">{t("component.image_preview.pan_hint")}</p>
  </Modal>;
}
