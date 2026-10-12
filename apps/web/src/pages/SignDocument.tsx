import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import type { PDFDocumentProxy } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import ShareOtpGate from "../components/permissions/ShareOtpGate";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { PageHeaderTitle } from "../components/ui/PageHeader";
import { IconCheckCircle, IconChevronLeft, IconChevronRight, IconDownload, IconManorLogo, IconTrash } from "../components/icons";
import { t } from "../lib/i18n";
import { normalizeSignatureFields, type DocumentSignatureField } from "../lib/signatureFields";
import "../components/permissions/signature.css";

interface PublicSignatureRequest {
  id: string;
  document_name: string;
  recipient_name: string;
  recipient_email: string;
  sender_name: string;
  message?: string | null;
  status: "pending" | "completed";
  placement: {
    page: number;
    x: number;
    y: number;
    width: number;
    height: number;
  };
  fields?: DocumentSignatureField[];
  expires_at?: string | null;
  signed_at?: string | null;
  group_size?: number;
  group_signed_count?: number;
  group_finalized?: boolean;
}

type PageState =
  | { kind: "loading" }
  | { kind: "verification" }
  | { kind: "ok"; data: PublicSignatureRequest }
  | { kind: "error"; message: string };

function responseMessage(status: number, detail: any) {
  if (typeof detail === "object" && detail !== null && typeof detail.code === "string") {
    const translated = t(detail.code, detail.vars);
    return translated !== detail.code ? translated : (detail.message || detail.code);
  }
  if (typeof detail === "string") return detail;
  if (status === 410) return t("sign.error.expired");
  if (status === 404) return t("sign.error.not_found");
  return t("sign.error.generic");
}

export default function SignDocument() {
  const { token } = useParams<{ token: string }>();
  const [state, setState] = useState<PageState>({ kind: "loading" });
  const [verificationVersion, setVerificationVersion] = useState(0);
  const [pdfVersion, setPdfVersion] = useState(0);
  const [pdfError, setPdfError] = useState<string>();
  const [pdfDocument, setPdfDocument] = useState<PDFDocumentProxy | null>(null);
  const [previewPage, setPreviewPage] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [renderedPage, setRenderedPage] = useState<number>();
  const [signatureDataUrl, setSignatureDataUrl] = useState<string>();
  const [hasInk, setHasInk] = useState(false);
  const [signatureDraftDirty, setSignatureDraftDirty] = useState(false);
  const [fieldValues, setFieldValues] = useState<Record<string, string | boolean>>({});
  const [activeFieldId, setActiveFieldId] = useState<string>();
  const [consent, setConsent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string>();
  const [previewWidth, setPreviewWidth] = useState(720);
  const previewHostRef = useRef<HTMLDivElement>(null);
  const previewCanvasRef = useRef<HTMLCanvasElement>(null);
  const signatureCanvasRef = useRef<HTMLCanvasElement>(null);
  const fieldEditorRef = useRef<HTMLDivElement>(null);
  const completionRef = useRef<HTMLElement>(null);
  const drawingRef = useRef<number | null>(null);
  const padVersionRef = useRef(0);
  const requestVersionRef = useRef(0);

  const endpoint = token ? `/api/v1/shared-doc/${encodeURIComponent(token)}` : "";
  const endpointRef = useRef(endpoint);
  endpointRef.current = endpoint;
  const fields = useMemo(
    () => state.kind === "ok" ? normalizeSignatureFields(state.data).map((field) => ({
      ...field, label: field.label || t(`signature.field.${field.type}`),
    })) : [],
    [state],
  );
  const activeField = fields.find((field) => field.id === activeFieldId);
  const signaturePage = fields.find((field) => field.type === "signature")?.page ?? 1;
  const requiredFields = fields.filter((field) => field.required);
  const isComplete = (field: DocumentSignatureField) => {
    const value = fieldValues[field.id];
    if (field.type === "checkbox") return value === true;
    if (typeof value !== "string" || !value.trim()) return false;
    if (field.type === "signature") return value.startsWith("data:image/png;base64,");
    if (field.type === "date") return /^\d{4}-\d{2}-\d{2}$/.test(value);
    return true;
  };
  const completedCount = requiredFields.filter(isComplete).length;
  const allRequiredComplete = requiredFields.length > 0 && completedCount === requiredFields.length;
  const hasInvalidText = fields.some((field) => field.type === "text" && String(fieldValues[field.id] ?? "").length > 2000);
  const pdfReady = Boolean(pdfDocument && renderedPage === previewPage && !pdfError);

  const loadRequest = useCallback(async (signal: AbortSignal) => {
    if (!endpoint) return;
    setState({ kind: "loading" });
    setSubmitting(false);
    setSignatureDataUrl(undefined);
    setHasInk(false);
    setSignatureDraftDirty(false);
    try {
      const response = await fetch(`${endpoint}/signature-request`, {
        headers: { Accept: "application/json" },
        signal,
      });
      const body = await response.json().catch(() => ({}));
      if (signal.aborted) return;
      if (!response.ok) {
        if (response.status === 401 && body?.detail?.code === "permissions.error.share.verification_required") {
          setState({ kind: "verification" });
          return;
        }
        setState({ kind: "error", message: responseMessage(response.status, body?.detail) });
        return;
      }
      const requestFields = normalizeSignatureFields(body as PublicSignatureRequest);
      setPreviewPage(requestFields.find((field) => field.type === "signature")?.page ?? body.placement.page);
      setFieldValues({});
      setActiveFieldId(undefined);
      setConsent(false);
      setState({ kind: "ok", data: body as PublicSignatureRequest });
    } catch (error: any) {
      if (!signal.aborted) setState({ kind: "error", message: error?.message || t("sign.error.network") });
    }
  }, [endpoint]);

  useEffect(() => {
    const controller = new AbortController();
    requestVersionRef.current += 1;
    void loadRequest(controller.signal);
    return () => { controller.abort(); requestVersionRef.current += 1; };
  }, [loadRequest, verificationVersion]);

  useEffect(() => {
    if (state.kind !== "ok") return;
    const host = previewHostRef.current;
    if (!host) return;
    const updateWidth = () => {
      const style = window.getComputedStyle(host);
      const width = host.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      if (width > 0) setPreviewWidth((current) => Math.abs(current - width) >= 2 ? width : current);
    };
    updateWidth();
    const resizeObserver = new ResizeObserver(updateWidth);
    resizeObserver.observe(host);
    return () => resizeObserver.disconnect();
  }, [state.kind]);

  useEffect(() => {
    if (state.kind !== "ok" || !endpoint) return;
    let cancelled = false;
    let loadingTask: any;
    setPdfDocument(null);
    setPageCount(0);
    setRenderedPage(undefined);
    setPdfError(undefined);

    const loadPdf = async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        if (cancelled) return;
        pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = pdfjs.getDocument({
          url: `${endpoint}/content?v=${pdfVersion}`,
          withCredentials: true,
        });
        const pdf = await loadingTask.promise;
        if (cancelled) return;
        if (signaturePage > pdf.numPages) {
          throw new Error(t("sign.error.page"));
        }
        setPageCount(pdf.numPages);
        setPdfDocument(pdf);
      } catch (error: any) {
        if (!cancelled) setPdfError(error?.message || t("sign.error.preview"));
      }
    };

    void loadPdf();
    return () => {
      cancelled = true;
      void loadingTask?.destroy?.();
    };
  }, [endpoint, pdfVersion, signaturePage, state.kind]);

  useEffect(() => {
    if (!pdfDocument || state.kind !== "ok") return;
    let cancelled = false;
    let renderTask: any;
    setRenderedPage(undefined);
    setPdfError(undefined);

    const render = async () => {
      try {
        const page = await pdfDocument.getPage(previewPage);
        if (cancelled) return;
        const baseViewport = page.getViewport({ scale: 1 });
        const scale = Math.min(1.5, Math.max(0.01, previewWidth / baseViewport.width));
        const viewport = page.getViewport({ scale });
        const canvas = previewCanvasRef.current;
        if (!canvas || cancelled) return;
        const outputScale = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.floor(viewport.width * outputScale);
        canvas.height = Math.floor(viewport.height * outputScale);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        const context = canvas.getContext("2d");
        if (!context) throw new Error(t("sign.error.preview"));
        renderTask = page.render({
          canvas,
          canvasContext: context,
          viewport,
          transform: outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0],
        });
        await renderTask.promise;
        if (!cancelled) setRenderedPage(previewPage);
      } catch (error: any) {
        if (!cancelled) setPdfError(error?.message || t("sign.error.preview"));
      }
    };

    void render();
    return () => {
      cancelled = true;
      renderTask?.cancel?.();
    };
  }, [pdfDocument, previewPage, previewWidth, state.kind]);

  useEffect(() => {
    const padVersion = ++padVersionRef.current;
    drawingRef.current = null;
    const canvas = signatureCanvasRef.current;
    if (!canvas) return;
    canvas.width = 800;
    canvas.height = 240;
    const context = canvas.getContext("2d");
    if (context) {
      context.lineCap = "round";
      context.lineJoin = "round";
      context.strokeStyle = "#1c1917";
      context.lineWidth = 5;
      const saved = activeFieldId ? fieldValues[activeFieldId] : undefined;
      if (typeof saved === "string" && saved.startsWith("data:image/png;base64,")) {
        const image = new Image();
        let cancelled = false;
        image.onload = () => { if (!cancelled && padVersionRef.current === padVersion) context.drawImage(image, 0, 0, canvas.width, canvas.height); };
        image.src = saved;
        return () => { cancelled = true; };
      }
    }
  // The pad is a draft for the selected field; typing into other fields must not reset it.
  }, [activeFieldId, state.kind]);

  useEffect(() => {
    if (state.kind !== "ok" || state.data.status !== "completed") return;
    completionRef.current?.focus({ preventScroll: true });
    scrollMobileElement(completionRef.current);
  }, [state]);

  function scrollMobileElement(element: HTMLElement | null) {
    if (window.innerWidth >= 1024) return;
    element?.scrollIntoView({
      block: "center",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  }

  function setFieldValue(id: string, value: string | boolean) {
    setFieldValues((current) => ({ ...current, [id]: value }));
    setSubmitError(undefined);
  }

  function selectField(field: DocumentSignatureField) {
    if (field.id !== activeFieldId) {
      if (signatureDraftDirty && activeField?.type === "signature" && signatureDataUrl && hasInk) {
        setFieldValue(activeField.id, signatureDataUrl);
      }
      setActiveFieldId(field.id);
      const value = fieldValues[field.id];
      setSignatureDataUrl(field.type === "signature" && typeof value === "string" ? value : undefined);
      setHasInk(field.type === "signature" && typeof value === "string" && value.startsWith("data:image/png;base64,"));
      setSignatureDraftDirty(false);
    }
    setPreviewPage(field.page);
    requestAnimationFrame(() => {
      const editor = fieldEditorRef.current;
      scrollMobileElement(editor);
      editor?.querySelector<HTMLInputElement | HTMLTextAreaElement>("input, textarea")?.focus({ preventScroll: true });
    });
  }

  function selectNextRequiredField() {
    const next = requiredFields.find((field) => !isComplete(field));
    if (next) selectField(next);
  }

  function applySignature() {
    if (!activeField || activeField.type !== "signature" || !hasInk || !signatureDataUrl) return;
    setFieldValue(activeField.id, signatureDataUrl);
    setSignatureDraftDirty(false);
    scrollMobileElement(previewHostRef.current);
  }

  function canvasPoint(event: React.PointerEvent<HTMLCanvasElement>) {
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * (canvas.width / rect.width),
      y: (event.clientY - rect.top) * (canvas.height / rect.height),
    };
  }

  function startDrawing(event: React.PointerEvent<HTMLCanvasElement>) {
    if (submitting || drawingRef.current !== null || !event.isPrimary || event.button !== 0) return;
    padVersionRef.current += 1;
    const canvas = event.currentTarget;
    const point = canvasPoint(event);
    const context = canvas.getContext("2d");
    if (!context) return;
    canvas.setPointerCapture(event.pointerId);
    drawingRef.current = event.pointerId;
    context.beginPath();
    context.moveTo(point.x, point.y);
  }

  function draw(event: React.PointerEvent<HTMLCanvasElement>) {
    if (drawingRef.current !== event.pointerId) return;
    const context = event.currentTarget.getContext("2d");
    if (!context) return;
    const point = canvasPoint(event);
    context.lineTo(point.x, point.y);
    context.stroke();
    setHasInk(true);
    setSignatureDraftDirty(true);
  }

  function finishDrawing(event: React.PointerEvent<HTMLCanvasElement>) {
    if (drawingRef.current !== event.pointerId) return;
    drawingRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setSignatureDataUrl(event.currentTarget.toDataURL("image/png"));
  }

  function clearSignature() {
    padVersionRef.current += 1;
    const canvas = signatureCanvasRef.current;
    canvas?.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
    setSignatureDataUrl(undefined);
    setHasInk(false);
    setSignatureDraftDirty(false);
    if (activeField?.type === "signature") setFieldValue(activeField.id, "");
  }

  async function submitSignature() {
    if (!endpoint || state.kind !== "ok" || !allRequiredComplete || hasInvalidText || signatureDraftDirty || !consent || !pdfReady || submitting) return;
    const requestVersion = requestVersionRef.current;
    const isCurrentRequest = () => endpointRef.current === endpoint && requestVersionRef.current === requestVersion;
    setSubmitting(true);
    setSubmitError(undefined);
    try {
      const response = await fetch(`${endpoint}/signature`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(state.data.fields?.length
          ? { field_values: fieldValues, consent }
          : { signature_data_url: fieldValues.signature, consent }),
      });
      const body = await response.json().catch(() => ({}));
      if (!isCurrentRequest()) return;
      if (!response.ok) throw new Error(responseMessage(response.status, body?.detail));
      setState({
        kind: "ok",
        data: {
          ...state.data, status: "completed", signed_at: body.signed_at,
          group_finalized: body.group_finalized,
          group_signed_count: (state.data.group_signed_count || 0) + 1,
        },
      });
      setPdfVersion((version) => version + 1);
    } catch (error: any) {
      if (isCurrentRequest()) setSubmitError(error?.message || t("sign.error.submit"));
    } finally {
      if (isCurrentRequest()) setSubmitting(false);
    }
  }

  if (!token) return null;

  return (
    <main className="signature-flow signature-customer min-h-screen bg-[var(--surface-app)] text-[var(--text-default)]">
      <header className="signature-brandbar px-4 py-4 sm:px-6">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-3 gap-y-1">
          <span role="img" aria-label="Manor AI" className="signature-brand flex shrink-0 items-center gap-2 text-sm font-semibold tracking-tight text-[var(--text-strong)]">
            <IconManorLogo size={24} />
            <span aria-hidden="true">Manor AI</span>
          </span>
          <p className="text-xs text-[var(--text-muted)]">{t("sign.secure_signature")}</p>
        </div>
      </header>

      <div className="signature-customer-body mx-auto max-w-6xl px-4 pb-8 pt-3 sm:px-6 sm:pb-12 sm:pt-5">
        {state.kind === "loading" && (
          <p role="status" className="py-20 text-center text-sm text-stone-500">{t("status.loading")}</p>
        )}
        {state.kind === "verification" && (
          <section className="signature-verification mx-auto max-w-md py-10 sm:py-16">
            <h1 className="text-xl font-semibold">{t("sign.verify_title")}</h1>
            <p className="mt-2 text-sm leading-relaxed text-stone-500">{t("sign.verify_hint")}</p>
            <ShareOtpGate
              minimal
              endpoint={endpoint}
              onVerified={() => setVerificationVersion((version) => version + 1)}
            />
          </section>
        )}
        {state.kind === "error" && (
          <section className="signature-verification mx-auto max-w-md py-10 sm:py-16">
            <h1 className="text-xl font-semibold">{t("sign.unavailable")}</h1>
            <p role="alert" className="mt-3 text-sm text-red-700">{state.message}</p>
          </section>
        )}
        {state.kind === "ok" && (
          <div className="signature-workspace grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_320px] lg:gap-8">
            {state.data.status === "completed" && (
              <section ref={completionRef} tabIndex={-1} aria-labelledby="signature-completed-title" className="signature-completion min-w-0 break-words lg:sticky lg:top-6 lg:col-start-2 lg:row-start-1">
                <h2 id="signature-completed-title" className="flex items-center gap-2 text-base font-semibold">
                  <IconCheckCircle size={18} className="shrink-0 text-stone-700" />
                  {state.data.group_finalized === false ? t("sign.group.signed_waiting") : t("sign.completed")}
                </h2>
                <p className="mt-2 text-sm leading-relaxed text-stone-500">
                  {state.data.group_finalized === false
                    ? t("sign.group.waiting_hint", { signed: state.data.group_signed_count || 0, total: state.data.group_size || 1 })
                    : t("sign.completed_hint", { email: state.data.recipient_email })}
                </p>
                {state.data.signed_at && (
                  <p className="mt-2 font-mono text-xs text-stone-500">{new Date(state.data.signed_at).toLocaleString()}</p>
                )}
                {state.data.group_finalized === false ? (
                  <Button variant="outline" className="mt-5" onClick={() => setVerificationVersion((version) => version + 1)}>
                    {t("sign.group.refresh")}
                  </Button>
                ) : (
                  <a href={`${endpoint}/download`} className="btn-manor mt-5 inline-flex items-center gap-2">
                    <IconDownload size={16} />{t("sign.download_signed")}
                  </a>
                )}
              </section>
            )}
            <section className="min-w-0 overflow-hidden lg:col-start-1 lg:row-start-1">
              <div className="signature-document-toolbar pb-4">
                <PageHeaderTitle variant="editor" title={state.data.document_name}>{state.data.document_name}</PageHeaderTitle>
                {state.data.status === "pending" && (
                  <p className="mt-1 text-xs text-stone-500">{t("sign.fill_fields")}</p>
                )}
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                  <nav aria-label={t("sign.page_navigation")} className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      ariaLabel={t("sign.previous_page")}
                      disabled={previewPage <= 1 || !pageCount || submitting}
                      onClick={() => setPreviewPage((page) => page - 1)}
                    >
                      <IconChevronLeft size={16} />
                    </Button>
                    <span aria-live="polite" className="min-w-20 text-center font-mono text-xs tabular-nums text-stone-600">
                      {pageCount ? t("sign.page_count", { page: previewPage, total: pageCount }) : t("status.loading")}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      ariaLabel={t("sign.next_page")}
                      disabled={previewPage >= pageCount || !pageCount || submitting}
                      onClick={() => setPreviewPage((page) => page + 1)}
                    >
                      <IconChevronRight size={16} />
                    </Button>
                  </nav>
                  {state.data.status === "pending" && (
                    <a href={`${endpoint}/download`} className="btn-manor-ghost inline-flex items-center gap-1.5 text-xs">
                      <IconDownload size={14} />{t("sign.download_original")}
                    </a>
                  )}
                </div>
                {pageCount > 1 && previewPage !== signaturePage && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mt-2"
                    disabled={submitting}
                    onClick={() => setPreviewPage(signaturePage)}
                  >
                    {t("sign.go_to_signature")}
                  </Button>
                )}
                {state.data.status === "pending" && (
                  <div className="signature-mobile-progress mt-3 flex flex-wrap items-center justify-between gap-2 lg:hidden">
                    <p className="text-xs text-[var(--text-muted)]">{t("sign.fields_progress", { completed: completedCount, total: requiredFields.length })}</p>
                    <Button variant="outline" size="sm" disabled={allRequiredComplete || submitting || !pdfReady} onClick={selectNextRequiredField}>
                      {t("sign.next_required")}
                    </Button>
                  </div>
                )}
              </div>
              <div ref={previewHostRef} className="signature-document-preview overflow-hidden bg-[var(--surface-sunken)] p-2 sm:p-4" aria-busy={!pdfReady && !pdfError}>
                {pdfError ? (
                  <p role="alert" className="py-20 text-center text-sm text-red-700">{pdfError}</p>
                ) : !pdfReady && (
                  <p role="status" className="py-20 text-center text-sm text-stone-500">{t("status.loading")}</p>
                )}
                <div hidden={!pdfReady} className="signature-document-sheet relative mx-auto w-fit max-w-full overflow-hidden bg-white">
                  <canvas
                    ref={previewCanvasRef}
                    className="block max-w-full"
                    role="img"
                    aria-label={t("sign.page_count", { page: previewPage, total: pageCount })}
                  />
                  {state.data.status === "pending" && fields.filter((field) => field.page === previewPage).map((field) => (
                    <button
                      key={field.id}
                      type="button"
                      data-testid={`signing-field-${field.id}`}
                      data-field-type={field.type}
                      data-completed={isComplete(field)}
                      aria-label={t("sign.edit_field", { label: field.label })}
                      aria-pressed={activeFieldId === field.id}
                      disabled={submitting}
                      title={`${field.label}${field.required ? " *" : ""}`}
                      onClick={() => selectField(field)}
                      className={`signature-field-overlay absolute flex items-center justify-center overflow-hidden rounded-sm border border-dashed text-stone-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)] ${activeFieldId === field.id ? "border-stone-800 bg-stone-100/90" : "border-stone-500 bg-stone-50/85"}`}
                      style={{
                        left: `${field.x * 100}%`, top: `${field.y * 100}%`,
                        width: `${field.width * 100}%`, height: `${field.height * 100}%`,
                        fontSize: "clamp(7px, 1.1vw, 12px)",
                      }}
                    >
                      {field.type === "signature" && typeof fieldValues[field.id] === "string" && fieldValues[field.id]
                        ? <img src={fieldValues[field.id] as string} alt={field.label} className="h-full w-full object-contain" />
                        : <span className="truncate px-1 font-medium">{field.type === "checkbox" && fieldValues[field.id] === true ? "✓" : String(fieldValues[field.id] || field.label)}{!isComplete(field) && field.required ? " *" : ""}</span>}
                    </button>
                  ))}
                </div>
              </div>
            </section>

            {state.data.status === "pending" && (
              <aside className="signature-sidebar min-w-0 break-words lg:sticky lg:top-6">
                  <div className="signature-request-meta flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs">
                    <span className="text-stone-500">{t("sign.requested_by")}</span>
                    <span className="font-medium text-stone-800">{state.data.sender_name}</span>
                  </div>
                  <p className="mt-3 text-sm leading-relaxed text-stone-600">
                    {t("sign.greeting", { name: state.data.recipient_name })}
                  </p>
                  {state.data.message && (
                    <div className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-stone-600">
                      {state.data.message}
                    </div>
                  )}
                  <div className="mt-5 space-y-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p aria-live="polite" className="text-xs font-medium">{t("sign.fields_progress", { completed: completedCount, total: requiredFields.length })}</p>
                      <Button variant="ghost" size="sm" disabled={allRequiredComplete || submitting} onClick={selectNextRequiredField}>{t("sign.next_required")}</Button>
                    </div>
                    <div className="signature-field-list max-h-48 space-y-1 overflow-y-auto" role="group" aria-label={t("sign.fill_fields")}>
                      {fields.map((field) => (
                        <button key={field.id} type="button" disabled={submitting}
                          className={`signature-field-row flex min-h-9 w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] ${activeFieldId === field.id ? "bg-stone-100 text-stone-900" : "text-stone-600 hover:bg-stone-50"}`}
                          onClick={() => selectField(field)} aria-pressed={activeFieldId === field.id}>
                          <span aria-hidden="true">{isComplete(field) ? "✓" : "○"}</span>
                          <span className="min-w-0 flex-1 truncate">{field.label}{field.required ? " *" : ""}</span>
                          <span className="font-mono text-stone-400">{field.page}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                  <div ref={fieldEditorRef} data-testid="signer-field-editor" className="signature-field-edit mt-5">
                    {!activeField ? <p className="text-xs leading-relaxed text-stone-500">{t("sign.field_hint")}</p> : (
                      <>
                        <p className="signature-section-title mb-2">{activeField.label}{activeField.required ? " *" : ""}</p>
                        {activeField.type === "signature" ? (
                          <>
                            <canvas ref={signatureCanvasRef} className="signature-drawing-pad h-32 w-full touch-none rounded-lg bg-[var(--surface-muted)]"
                              aria-label={t("sign.signature_pad")} onPointerDown={startDrawing} onPointerMove={draw}
                              onPointerUp={finishDrawing} onPointerCancel={finishDrawing} />
                            <div className="mt-2 flex flex-wrap justify-between gap-2">
                              <Button variant="ghost" size="sm" onClick={clearSignature} disabled={!hasInk || submitting}>
                                <span className="inline-flex items-center gap-1.5"><IconTrash size={14} />{t("sign.clear")}</span>
                              </Button>
                              <Button variant="outline" size="sm" onClick={applySignature} disabled={!hasInk || !signatureDataUrl || submitting}>{t("sign.apply_signature")}</Button>
                            </div>
                          </>
                        ) : activeField.type === "checkbox" ? (
                          <label className="flex min-h-10 cursor-pointer items-center gap-3 text-sm">
                            <input type="checkbox" checked={fieldValues[activeField.id] === true} disabled={submitting}
                              onChange={(event) => setFieldValue(activeField.id, event.target.checked)} className="h-4 w-4 accent-[var(--accent)]" />
                            {activeField.label}
                          </label>
                        ) : activeField.type === "text" ? (
                          <>
                            <Textarea ariaLabel={activeField.label} value={String(fieldValues[activeField.id] ?? "")} rows={3} disabled={submitting}
                              error={String(fieldValues[activeField.id] ?? "").length > 2000 ? t("sign.text_limit") : undefined}
                              onChange={(event) => setFieldValue(activeField.id, event.target.value)} />
                            <p className="mt-1 text-right font-mono text-[11px] text-stone-500">{String(fieldValues[activeField.id] ?? "").length} / 2000</p>
                          </>
                        ) : (
                          <Input ariaLabel={activeField.label} type={activeField.type === "date" ? "date" : "text"}
                            autoComplete={activeField.type === "name" ? "name" : "off"} maxLength={activeField.type === "name" ? 120 : 10}
                            value={String(fieldValues[activeField.id] ?? "")} disabled={submitting}
                            onChange={(event) => setFieldValue(activeField.id, event.target.value)} />
                        )}
                      </>
                    )}
                  </div>
                  <label className="signature-consent mt-5 flex cursor-pointer items-start gap-3 text-xs leading-relaxed text-stone-600">
                    <input
                      type="checkbox"
                      className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]"
                      checked={consent}
                      disabled={submitting}
                      onChange={(event) => setConsent(event.target.checked)}
                    />
                    <span>{t("sign.consent", { email: state.data.recipient_email })}</span>
                  </label>
                  {submitError && <p role="alert" className="mt-3 text-sm text-red-700">{submitError}</p>}
                  <Button
                    className="mt-5 w-full justify-center"
                    loading={submitting}
                    disabled={!allRequiredComplete || hasInvalidText || signatureDraftDirty || !consent || !pdfReady}
                    onClick={() => void submitSignature()}
                  >
                    {t("sign.submit")}
                  </Button>
                  <p className="mt-3 text-[11px] leading-relaxed text-stone-500">{t("sign.audit_notice")}</p>
              </aside>
            )}
          </div>
        )}
      </div>
    </main>
  );
}
