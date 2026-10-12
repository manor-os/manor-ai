import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { clampSignatureField, createSignatureField, parseSignatureFieldPercentage, SIGNATURE_FIELD_TYPES, type DocumentSignatureField, type SignatureFieldType } from "../../lib/signatureFields";
import { IconChevronLeft, IconChevronRight, IconTrash } from "../icons";
import Button from "../ui/Button";
import Input from "../ui/Input";

interface Props {
  documentId: string;
  fields: DocumentSignatureField[];
  onChange: (fields: DocumentSignatureField[]) => void;
  onReadyChange: (ready: boolean) => void;
  readOnly?: boolean;
  sidebar?: ReactNode;
}

function PercentageInput({ label, value, minimum, onCommit }: { label: string; value: number; minimum: number; onCommit: (value: number) => void }) {
  const format = (number: number) => String(Number((number * 100).toFixed(4)));
  const [draft, setDraft] = useState(() => format(value));
  const focusedRef = useRef(false);
  useEffect(() => { if (!focusedRef.current) setDraft(format(value)); }, [value]);

  function commit() {
    const next = parseSignatureFieldPercentage(draft, minimum);
    setDraft(format(next ?? value));
    if (next !== undefined) onCommit(next);
  }

  return <div onBlur={() => { focusedRef.current = false; commit(); }}>
    <Input label={label} type="number" min={minimum * 100} max={100} step="any" value={draft}
      onFocus={() => { focusedRef.current = true; }}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") { event.preventDefault(); commit(); }
        if (event.key === "ArrowUp" || event.key === "ArrowDown") {
          event.preventDefault();
          const current = parseSignatureFieldPercentage(draft, minimum) ?? value;
          const next = Math.max(minimum, Math.min(1, current + (event.key === "ArrowUp" ? 1 : -1) * (event.shiftKey ? 0.01 : 0.001)));
          setDraft(format(next));
          onCommit(next);
        }
      }} />
  </div>;
}

export default function SignatureFieldEditor({ documentId, fields, onChange, onReadyChange, readOnly = false, sidebar }: Props) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const [pageWidth, setPageWidth] = useState(640);
  const [pageRatio, setPageRatio] = useState(1.414);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string>();
  const [reload, setReload] = useState(0);
  const [selectedId, setSelectedId] = useState<string>();
  const [placingType, setPlacingType] = useState<SignatureFieldType>();
  const hostRef = useRef<HTMLDivElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const onReadyRef = useRef(onReadyChange);
  onReadyRef.current = onReadyChange;
  const dragRef = useRef<{ field: DocumentSignatureField; x: number; y: number; width: number; height: number; resize: boolean }>();
  const selected = fields.find((field) => field.id === selectedId);

  useEffect(() => { onReadyRef.current(ready); }, [ready]);
  useEffect(() => { setPageInput(String(page)); }, [page]);
  useEffect(() => { if (readOnly) setPlacingType(undefined); }, [readOnly]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const measure = () => setPageWidth(Math.max(180, Math.min(850, host.clientWidth - 32)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: ReturnType<typeof import("pdfjs-dist").getDocument> | undefined;
    const abort = new AbortController();
    setPdf(null);
    setReady(false);
    setError(undefined);
    setPage(1);
    void (async () => {
      try {
        const [pdfjs, blob] = await Promise.all([
          import("pdfjs-dist"),
          api.documents.downloadBlob(documentId, { signal: abort.signal, force: true, cache: false }),
        ]);
        if (cancelled) return;
        const data = await blob.arrayBuffer();
        if (cancelled) return;
        pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = pdfjs.getDocument({ data });
        const document = await loadingTask.promise;
        if (!cancelled) setPdf(document);
      } catch (caught: any) {
        if (!cancelled) setError(caught?.message || t("sign.error.preview"));
      }
    })();
    return () => {
      cancelled = true;
      abort.abort();
      void loadingTask?.destroy().catch(() => undefined);
    };
  }, [documentId, reload]);

  useEffect(() => {
    if (!pdf) return;
    let cancelled = false;
    let renderTask: RenderTask | undefined;
    setReady(false);
    setError(undefined);
    void (async () => {
      try {
        const pdfPage = await pdf.getPage(page);
        if (cancelled) return;
        const original = pdfPage.getViewport({ scale: 1 });
        const viewport = pdfPage.getViewport({ scale: pageWidth / original.width });
        const canvas = canvasRef.current;
        if (!canvas) return;
        const context = canvas.getContext("2d");
        if (!context) throw new Error(t("sign.error.preview"));
        const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.ceil(viewport.width * pixelRatio);
        canvas.height = Math.ceil(viewport.height * pixelRatio);
        setPageRatio(viewport.height / viewport.width);
        renderTask = pdfPage.render({ canvas, canvasContext: context, viewport, transform: [pixelRatio, 0, 0, pixelRatio, 0, 0] });
        await renderTask.promise;
        if (!cancelled) setReady(true);
      } catch (caught: any) {
        if (!cancelled && caught?.name !== "RenderingCancelledException") setError(caught?.message || t("sign.error.preview"));
      }
    })();
    return () => { cancelled = true; renderTask?.cancel(); };
  }, [pdf, page, pageWidth]);

  function updateField(field: DocumentSignatureField) {
    onChange(fields.map((current) => current.id === field.id ? clampSignatureField(field) : current));
  }

  function addField(x: number, y: number) {
    if (!placingType || !ready || readOnly || fields.length >= 40) return;
    const field = createSignatureField(placingType, page, t(`signature.field.${placingType}`), x, y);
    onChange([...fields, field]);
    setSelectedId(field.id);
    setPlacingType(undefined);
  }

  function beginDrag(event: PointerEvent<HTMLElement>, field: DocumentSignatureField, resize = false) {
    if (readOnly || !ready || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    setSelectedId(field.id);
    setPlacingType(undefined);
    const bounds = pageRef.current?.getBoundingClientRect();
    if (!bounds) return;
    dragRef.current = { field, x: event.clientX, y: event.clientY, width: bounds.width, height: bounds.height, resize };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag || readOnly) return;
    const dx = (event.clientX - drag.x) / drag.width;
    const dy = (event.clientY - drag.y) / drag.height;
    updateField(drag.resize
      ? { ...drag.field, width: Math.min(1 - drag.field.x, drag.field.width + dx), height: Math.min(1 - drag.field.y, drag.field.height + dy) }
      : { ...drag.field, x: drag.field.x + dx, y: drag.field.y + dy });
  }

  function handleFieldKey(event: KeyboardEvent<HTMLButtonElement>, field: DocumentSignatureField) {
    if (readOnly) return;
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onChange(fields.filter((current) => current.id !== field.id));
      setSelectedId(undefined);
      return;
    }
    const delta = event.shiftKey ? 0.01 : 0.002;
    const dx = event.key === "ArrowLeft" ? -delta : event.key === "ArrowRight" ? delta : 0;
    const dy = event.key === "ArrowUp" ? -delta : event.key === "ArrowDown" ? delta : 0;
    if (dx || dy) {
      event.preventDefault();
      updateField({ ...field, x: field.x + dx, y: field.y + dy });
    }
  }

  function goToPage(value: string) {
    const target = Number(value);
    if (pdf && Number.isInteger(target) && target >= 1 && target <= pdf.numPages) {
      if (target !== page) { setReady(false); setPage(target); setSelectedId(undefined); }
      setPlacingType(undefined);
    } else setPageInput(String(page));
  }

  return (
    <div className="grid min-h-0 flex-1 gap-4 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_260px] lg:gap-5 lg:overflow-hidden" data-testid="signature-field-editor">
      <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-[var(--radius-control)] bg-[var(--surface-sunken)] max-lg:min-h-[50dvh] max-lg:max-h-[60dvh]" aria-label={t("signature.fields.document")}>
        <div className="flex flex-wrap items-center justify-between gap-2 p-3">
          <p className="min-w-0 text-xs text-stone-500">{readOnly ? t("signature.fields.preview_hint") : placingType ? t("signature.fields.place_hint", { type: t(`signature.field.${placingType}`) }) : t("signature.fields.drag_hint")}</p>
          <div className="flex shrink-0 items-center gap-2">
            <Button variant="ghost" size="sm" ariaLabel={t("sign.previous_page")} disabled={!pdf || page <= 1} onClick={() => goToPage(String(page - 1))}><IconChevronLeft size={16} /></Button>
            <input className="manor-input !h-8 !w-14 !px-2 text-center" aria-label={t("signature.fields.page_number")} type="number" min={1} max={pdf?.numPages || 1} value={pageInput} disabled={!pdf || !ready} onChange={(event) => setPageInput(event.target.value)} onBlur={() => goToPage(pageInput)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); goToPage(pageInput); } }} />
            <span className="font-mono text-xs text-stone-500">/ {pdf?.numPages || "—"}</span>
            <Button variant="ghost" size="sm" ariaLabel={t("sign.next_page")} disabled={!pdf || page >= pdf.numPages} onClick={() => goToPage(String(page + 1))}><IconChevronRight size={16} /></Button>
          </div>
        </div>
        <div ref={hostRef} className="min-h-[260px] flex-1 overflow-auto px-4 pb-4 lg:min-h-0">
          {error ? <div role="alert" className="p-5 text-sm text-[var(--chip-danger-fg)]"><p>{error}</p><Button variant="ghost" className="mt-3" onClick={() => setReload((value) => value + 1)}>{t("signature.fields.retry")}</Button></div> : (
            <div ref={pageRef} className={`relative mx-auto bg-white ${placingType && ready ? "cursor-crosshair" : ""}`} style={{ width: pageWidth, height: pageWidth * pageRatio }} data-testid="signature-editor-page" data-page-number={page} onClick={(event) => {
              if (event.target !== event.currentTarget && event.target !== canvasRef.current) return;
              const bounds = event.currentTarget.getBoundingClientRect();
              if (placingType) addField((event.clientX - bounds.left) / bounds.width, (event.clientY - bounds.top) / bounds.height);
              else setSelectedId(undefined);
            }}>
              <canvas ref={canvasRef} className="block h-full w-full" aria-label={t("signature.fields.document")} style={{ visibility: ready ? "visible" : "hidden" }} />
              {!ready && <div role="status" className="absolute inset-0 flex items-center justify-center text-sm text-stone-500">{t("status.loading")}</div>}
              {ready && fields.filter((field) => field.page === page).map((field) => (
                <div key={field.id} className="absolute" style={{ left: `${field.x * 100}%`, top: `${field.y * 100}%`, width: `${field.width * 100}%`, height: `${field.height * 100}%` }} data-testid="signature-editor-field" data-field-id={field.id} data-field-type={field.type}>
                  <button type="button" className={`signature-field-box flex h-full w-full items-center justify-center overflow-hidden rounded-sm bg-stone-100/90 px-1 text-[10px] font-medium text-stone-800 outline outline-1 ${selectedId === field.id && !readOnly ? "outline-2 outline-stone-800" : "outline-dashed outline-stone-400"}`} style={{ touchAction: readOnly ? "auto" : "none", cursor: readOnly ? "default" : "move" }} aria-label={t("signature.fields.select", { label: field.label })} aria-pressed={selectedId === field.id} onClick={(event) => { event.stopPropagation(); setSelectedId(field.id); }} onPointerDown={(event) => beginDrag(event, field)} onPointerMove={moveDrag} onPointerUp={() => { dragRef.current = undefined; }} onPointerCancel={() => { dragRef.current = undefined; }} onLostPointerCapture={() => { dragRef.current = undefined; }} onKeyDown={(event) => handleFieldKey(event, field)}>
                    <span className="truncate">{field.type === "checkbox" ? "☐" : field.label}{field.required ? " *" : ""}</span>
                  </button>
                  {!readOnly && selectedId === field.id && <button type="button" className="signature-field-resize absolute -bottom-2 -right-2 flex h-6 w-6 cursor-nwse-resize items-center justify-center rounded-sm bg-stone-800 text-xs text-white" style={{ touchAction: "none" }} aria-label={t("signature.fields.resize", { label: field.label })} onPointerDown={(event) => beginDrag(event, field, true)} onPointerMove={moveDrag} onPointerUp={() => { dragRef.current = undefined; }} onPointerCancel={() => { dragRef.current = undefined; }} onLostPointerCapture={() => { dragRef.current = undefined; }}>↘</button>}
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
      <aside className="space-y-6 p-1 lg:min-h-0 lg:overflow-y-auto">
        {sidebar || <>
          <div>
            <h3 className="signature-section-title">{t("signature.fields.add")}</h3>
            <p className="mt-1 text-xs leading-relaxed text-stone-500">{t("signature.fields.add_hint")}</p>
            <div className="mt-3 grid grid-cols-2 gap-1">
              {SIGNATURE_FIELD_TYPES.map((type) => <Button key={type} variant="ghost" className="signature-field-tool" disabled={!ready || fields.length >= 40} ariaPressed={placingType === type} ariaLabel={t("signature.fields.add_type", { type: t(`signature.field.${type}`) })} onClick={() => { setPlacingType(placingType === type ? undefined : type); setSelectedId(undefined); }} style={placingType === type ? { background: "var(--surface-sunken)", boxShadow: "inset 0 0 0 1px var(--text-muted)" } : undefined}>{t(`signature.field.${type}`)}</Button>)}
            </div>
            {placingType && <Button variant="ghost" size="sm" className="mt-2" onClick={() => addField(0.35, 0.4)}>{t("signature.fields.add_center")}</Button>}
          </div>
          {selected && <div className="space-y-3">
            <h3 className="signature-section-title">{t("signature.fields.properties")}</h3>
            <Input label={t("signature.fields.label")} value={selected.label} maxLength={120} onChange={(event) => updateField({ ...selected, label: event.target.value })} />
            <label className="flex min-h-8 items-center gap-2 text-xs text-stone-700"><input type="checkbox" checked={selected.required} onChange={(event) => updateField({ ...selected, required: event.target.checked })} className="h-4 w-4 accent-[var(--accent)]" />{t("signature.field.required")}</label>
            <div className="grid grid-cols-2 gap-2">
              {(["x", "y", "width", "height"] as const).map((key) => <PercentageInput key={`${selected.id}-${key}`} label={t(`signature.fields.${key}`)} value={selected[key]} minimum={key === "width" ? 0.02 : key === "height" ? 0.012 : 0} onCommit={(value) => updateField({ ...selected, [key]: value })} />)}
            </div>
            <p className="text-[11px] leading-relaxed text-stone-500">{t("signature.fields.keyboard_hint")}</p>
            <Button variant="ghost" size="sm" onClick={() => { onChange(fields.filter((field) => field.id !== selected.id)); setSelectedId(undefined); }}><span className="inline-flex items-center gap-1.5"><IconTrash size={14} />{t("signature.fields.remove")}</span></Button>
          </div>}
          <div>
            <h3 className="signature-section-title">{t("signature.fields.count", { count: fields.length })}</h3>
            {!fields.length ? <p className="mt-2 text-xs leading-relaxed text-stone-500">{t("signature.fields.empty")}</p> : <div className="mt-2 space-y-1">{fields.map((field) => <button type="button" key={field.id} aria-pressed={selectedId === field.id} className={`signature-field-choice flex min-h-9 w-full items-center justify-between gap-2 rounded-[var(--radius-control)] px-2 py-2 text-left text-xs text-[var(--text-default)] hover:bg-[var(--surface-sunken)] ${selectedId === field.id ? "bg-[var(--surface-sunken)] font-medium" : ""}`} onClick={() => { goToPage(String(field.page)); setSelectedId(field.id); setPlacingType(undefined); }}><span className="truncate">{field.label || t(`signature.field.${field.type}`)}{field.required ? " *" : ""}</span><span className="shrink-0 font-mono text-[var(--text-muted)]">{t("signature.fields.page_short", { page: field.page })}</span></button>)}</div>}
          </div>
        </>}
      </aside>
    </div>
  );
}
