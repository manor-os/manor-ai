import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import type {
  AssistantResponseSurfaceBlock,
  ResponseSurfaceSubmission,
  ResponseSurfaceSubmissionReceipt,
  ResponseSurfaceSubmissionResult,
} from "../lib/chatStream";
import { getLocale, t } from "../lib/i18n";
import {
  createResponseSurfaceSubmissionReceipt,
  responseSurfaceDocument,
} from "../lib/responseSurface";
import {
  cloneBoundedResponseSurfacePayload,
  latestUnseenResponseSurfaceReceipt,
  RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT,
  RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT,
  RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
} from "../lib/responseSurfaceState.mjs";
import { useIsolatedHtmlPreview } from "../lib/useIsolatedHtmlPreview";
import ChatMarkdown from "./ChatMarkdown";
import { IconAspectRatio, IconSparkles } from "./icons";
import Button from "./ui/Button";
import IsolatedHtmlPreviewFrame from "./ui/IsolatedHtmlPreviewFrame";
import Modal from "./ui/Modal";
import { useResolvedDocumentTheme } from "./ui/ThemeAwareImage";
import "./InteractiveResponseSurface.css";

interface InteractiveResponseSurfaceProps {
  surface: AssistantResponseSurfaceBlock;
  sourceMessageId: string;
  submissionReceipts?: ResponseSurfaceSubmissionReceipt[];
  chrome?: boolean;
  className?: string;
  frameClassName?: string;
  frameStyle?: CSSProperties;
  submissionDisabled?: boolean;
  onSubmit?: (
    submission: ResponseSurfaceSubmissionReceipt,
  ) => void | boolean | ResponseSurfaceSubmissionResult
    | Promise<void | boolean | ResponseSurfaceSubmissionResult>;
}

type SubmissionStatus =
  | { kind: "idle" }
  | { kind: "sending"; label: string }
  | { kind: "sent"; label: string }
  | { kind: "error"; label: string };

interface PendingViewTransition {
  requestId: string;
  targetFocused: boolean;
  timeoutId: number;
  bridgeNonce: string;
  draftVersion: number;
  sourceWindow: Window;
}

interface LateViewState {
  requestId: string;
  bridgeNonce: string;
  draftVersion: number;
  sourceWindow: Window;
  expiresAt: number;
}

function responseSurfaceSubmissionIntentKey(
  submission: ResponseSurfaceSubmission,
): string {
  return JSON.stringify([
    submission.sourceMessageId,
    submission.surfaceId,
    submission.action,
    submission.payload,
  ]);
}

export default function InteractiveResponseSurface({
  surface,
  sourceMessageId,
  submissionReceipts = [],
  chrome = true,
  className = "",
  frameClassName = "",
  frameStyle,
  submissionDisabled = false,
  onSubmit,
}: InteractiveResponseSurfaceProps) {
  const theme = useResolvedDocumentTheme();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const draftRef = useRef<Record<string, unknown>>({});
  const seenReceiptEventIdsRef = useRef<Set<string>>(new Set());
  const bridgeNonceRef = useRef("");
  const draftVersionRef = useRef(0);
  const lateViewStateRef = useRef<LateViewState | null>(null);
  const submittingRef = useRef(false);
  const pendingSubmissionReceiptRef = useRef<{
    intentKey: string;
    receipt: ResponseSurfaceSubmissionReceipt;
  } | null>(null);
  const durablePendingReceiptRef = useRef<ResponseSurfaceSubmissionReceipt | null>(null);
  const pendingViewTransitionRef = useRef<PendingViewTransition | null>(null);
  const [focused, setFocused] = useState(false);
  const [documentRevision, setDocumentRevision] = useState(0);
  const [frameHeight, setFrameHeight] = useState(surface.display.inline_height);
  const [submissionStatus, setSubmissionStatus] = useState<SubmissionStatus>({ kind: "idle" });
  const interactionReadOnly = !onSubmit;
  const locale = getLocale();
  const relevantReceipts = useMemo(() => submissionReceipts.filter((receipt) => (
    receipt.sourceMessageId === sourceMessageId
    && receipt.surfaceId === surface.id
    && surface.actions.some((action) => action.id === receipt.action)
  )), [sourceMessageId, submissionReceipts, surface.actions, surface.id]);
  const html = useMemo(
    () => `${responseSurfaceDocument(surface, theme, draftRef.current, relevantReceipts, {
      history: t("component.response_surface.history"),
      submitted: t("component.response_surface.submitted"),
      lines: t("component.response_surface.lines"),
      codeExercise: t("component.response_surface.code_exercise"),
      codeEditor: t("component.response_surface.code_editor"),
      language: t("component.response_surface.language"),
      line: t("component.response_surface.line"),
      column: t("component.response_surface.column"),
      spaces: t("component.response_surface.spaces"),
      checks: t("component.response_surface.checks"),
      runCode: t("component.response_surface.run_code"),
      submitAnswer: t("component.response_surface.submit_answer"),
      failed: t("component.response_surface.failed"),
      interrupted: t("component.response_surface.interrupted"),
    }, locale)}\n<!-- response-surface-revision:${documentRevision} -->`,
    [documentRevision, locale, relevantReceipts, surface, theme],
  );
  const preview = useIsolatedHtmlPreview(html);

  const finishViewTransition = useCallback((targetFocused: boolean, preserveLate = false) => {
    const pending = pendingViewTransitionRef.current;
    if (pending) {
      window.clearTimeout(pending.timeoutId);
      pendingViewTransitionRef.current = null;
    }
    if (!preserveLate) lateViewStateRef.current = null;
    bridgeNonceRef.current = "";
    setDocumentRevision((revision) => revision + 1);
    setFocused(targetFocused);
  }, []);

  const requestViewTransition = useCallback((targetFocused: boolean) => {
    if (focused === targetFocused || pendingViewTransitionRef.current) return;
    const frameWindow = iframeRef.current?.contentWindow;
    const bridgeNonce = bridgeNonceRef.current;
    if (!frameWindow || !bridgeNonce) {
      finishViewTransition(targetFocused);
      return;
    }
    const requestId = typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const timeoutId = window.setTimeout(() => {
      const current = pendingViewTransitionRef.current;
      if (current?.requestId === requestId) {
        lateViewStateRef.current = {
          requestId,
          bridgeNonce: current.bridgeNonce,
          draftVersion: current.draftVersion,
          sourceWindow: current.sourceWindow,
          expiresAt: Date.now() + 2_000,
        };
        finishViewTransition(targetFocused, true);
      }
    }, 200);
    pendingViewTransitionRef.current = {
      requestId,
      targetFocused,
      timeoutId,
      bridgeNonce,
      draftVersion: draftVersionRef.current,
      sourceWindow: frameWindow,
    };
    frameWindow.postMessage({
      type: "manor:response-surface:request-state",
      bridgeNonce,
      requestId,
    }, window.location.origin);
  }, [finishViewTransition, focused]);

  useEffect(() => {
    const pending = pendingViewTransitionRef.current;
    if (pending) window.clearTimeout(pending.timeoutId);
    pendingViewTransitionRef.current = null;
    setFrameHeight(surface.display.inline_height);
    draftRef.current = {};
    seenReceiptEventIdsRef.current = new Set();
    draftVersionRef.current = 0;
    lateViewStateRef.current = null;
    bridgeNonceRef.current = "";
    submittingRef.current = false;
    pendingSubmissionReceiptRef.current = null;
    durablePendingReceiptRef.current = null;
    setFocused(false);
    setSubmissionStatus({ kind: "idle" });
    setDocumentRevision((revision) => revision + 1);
  }, [sourceMessageId, surface.id, surface.display.inline_height]);

  useEffect(() => {
    const trackedEventId = pendingSubmissionReceiptRef.current?.receipt.eventId
      || durablePendingReceiptRef.current?.eventId;
    const trackedReceipt = trackedEventId
      ? relevantReceipts.find((receipt) => (
          receipt.eventId === trackedEventId && receipt.durable === true
        ))
      : undefined;
    const durablePendingReceipt = (
      trackedReceipt?.status === "pending"
        ? trackedReceipt
        : !trackedEventId
          ? [...relevantReceipts].reverse().find((receipt) => (
              receipt.durable === true && receipt.status === "pending"
            ))
          : undefined
    );
    if (durablePendingReceipt) {
      durablePendingReceiptRef.current = durablePendingReceipt;
      if (!pendingSubmissionReceiptRef.current) {
        pendingSubmissionReceiptRef.current = {
          intentKey: responseSurfaceSubmissionIntentKey(durablePendingReceipt),
          receipt: durablePendingReceipt,
        };
      }
      if (!submittingRef.current) {
        setSubmissionStatus({
          kind: "sent",
          label: durablePendingReceipt.actionLabel,
        });
      }
    } else if (trackedReceipt?.status && trackedReceipt.status !== "pending") {
      pendingSubmissionReceiptRef.current = null;
      durablePendingReceiptRef.current = null;
      if (!submittingRef.current) {
        setSubmissionStatus({
          kind: trackedReceipt.status === "succeeded" ? "sent" : "error",
          label: trackedReceipt.actionLabel,
        });
      }
    }
    const receipt = latestUnseenResponseSurfaceReceipt(
      relevantReceipts,
      seenReceiptEventIdsRef.current,
    );
    if (!receipt) return;
    draftRef.current = {
      ...draftRef.current,
      ...receipt.payload,
    };
    draftVersionRef.current += 1;
    setDocumentRevision((revision) => revision + 1);
  }, [relevantReceipts]);

  useEffect(() => () => {
    const pending = pendingViewTransitionRef.current;
    if (pending) window.clearTimeout(pending.timeoutId);
  }, []);

  useEffect(() => {
    bridgeNonceRef.current = "";
  }, [html]);

  useEffect(() => {
    const busy = submissionStatus.kind === "sending";
    const disabled = submissionDisabled || busy || interactionReadOnly;
    iframeRef.current?.contentWindow?.postMessage({
      type: "manor:response-surface:set-disabled",
      bridgeNonce: bridgeNonceRef.current,
      disabled,
      busy,
    }, window.location.origin);
  }, [interactionReadOnly, preview.previewUrl, submissionDisabled, submissionStatus.kind]);

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      const message = event.data as Record<string, unknown> | null;
      if (!message || message.surfaceId !== surface.id) return;
      const late = lateViewStateRef.current;
      if (
        late
        && message.type === "manor:response-surface:state"
        && event.source === late.sourceWindow
        && message.bridgeNonce === late.bridgeNonce
        && message.requestId === late.requestId
      ) {
        lateViewStateRef.current = null;
        if (Date.now() <= late.expiresAt && draftVersionRef.current === late.draftVersion) {
          const draft = cloneBoundedResponseSurfacePayload(
            message.payload,
            RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT,
          );
          if (draft) {
            draftRef.current = draft;
            draftVersionRef.current += 1;
            setDocumentRevision((revision) => revision + 1);
          }
        }
        return;
      }
      if (event.source !== iframeRef.current?.contentWindow) return;
      if (message.type === "manor:response-surface:bridge-ready") {
        const nonce = typeof message.bridgeNonce === "string" ? message.bridgeNonce : "";
        if (!bridgeNonceRef.current && nonce.length >= 16 && nonce.length <= 128) {
          bridgeNonceRef.current = nonce;
          iframeRef.current?.contentWindow?.postMessage({
            type: "manor:response-surface:set-disabled",
            bridgeNonce: nonce,
            disabled: submissionDisabled || submittingRef.current || interactionReadOnly,
            busy: submittingRef.current,
          }, window.location.origin);
        }
        return;
      }
      if (!bridgeNonceRef.current || message.bridgeNonce !== bridgeNonceRef.current) return;
      if (message.type === "manor:response-surface:resize") {
        const height = Number(message.height);
        if (Number.isFinite(height)) {
          setFrameHeight(Math.min(720, Math.max(180, height)));
        }
        return;
      }
      if (message.type === "manor:response-surface:state") {
        const draft = cloneBoundedResponseSurfacePayload(
          message.payload,
          RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT,
        );
        if (draft) {
          draftRef.current = draft;
          draftVersionRef.current += 1;
        }
        const requestId = typeof message.requestId === "string" ? message.requestId : "";
        const pending = pendingViewTransitionRef.current;
        if (requestId && pending?.requestId === requestId) {
          finishViewTransition(pending.targetFocused);
        }
        return;
      }
      if (message.type !== "manor:response-surface:submit") return;
      if (submissionDisabled || submittingRef.current || interactionReadOnly) return;
      const actionId = typeof message.action === "string" ? message.action : "";
      const action = surface.actions.find((item) => item.id === actionId);
      if (!action || !onSubmit) return;
      const template = surface.render.kind === "template" ? surface.render : null;
      const payload = cloneBoundedResponseSurfacePayload(
        message.payload,
        template?.template_id === "learning.code_lab"
          ? RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT
          : RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
      );
      if (!payload) {
        setSubmissionStatus({ kind: "error", label: action.label });
        return;
      }
      draftRef.current = { ...draftRef.current, ...payload };
      const templateProps = template?.props || {};
      const submission: ResponseSurfaceSubmission = {
        sourceMessageId,
        surfaceId: surface.id,
        title: surface.title,
        action: action.id,
        actionLabel: action.label,
        payload,
        context: template ? {
          templateId: template.template_id,
          instructions: typeof templateProps.instructions === "string"
            ? templateProps.instructions.slice(0, 4_000)
            : undefined,
          checks: Array.isArray(templateProps.tests)
            ? templateProps.tests
              .filter((item): item is string => typeof item === "string")
              .slice(0, 20)
              .map((item) => item.slice(0, 2_000))
            : undefined,
        } : undefined,
      };
      const intentKey = responseSurfaceSubmissionIntentKey(submission);
      const pending = pendingSubmissionReceiptRef.current;
      const receipt = pending?.intentKey === intentKey
        ? pending.receipt
        : createResponseSurfaceSubmissionReceipt(submission);
      if (durablePendingReceiptRef.current?.eventId !== receipt.eventId) {
        durablePendingReceiptRef.current = null;
      }
      pendingSubmissionReceiptRef.current = { intentKey, receipt };
      submittingRef.current = true;
      setSubmissionStatus({ kind: "sending", label: action.label });
      void (async () => {
        try {
          const result = await onSubmit(receipt);
          const structuredResult = typeof result === "object" && result !== null
            ? result
            : null;
          const durableReceipt = durablePendingReceiptRef.current?.eventId === receipt.eventId
            ? durablePendingReceiptRef.current
            : null;
          const serverAccepted = Boolean(durableReceipt)
            || structuredResult?.serverAccepted === true
            || (structuredResult === null && result !== false);
          const terminalObserved = Boolean(durableReceipt?.outcome)
            || structuredResult?.terminalObserved === true
            || structuredResult === null;
          const completed = !durableReceipt?.outcome && (
            structuredResult
              ? structuredResult.status === "succeeded"
                && serverAccepted
                && terminalObserved
              : result !== false
          );
          if (!completed) {
            const determinateFailure = Boolean(durableReceipt?.outcome)
              || Boolean(
                structuredResult
                && structuredResult.status !== "succeeded"
                && structuredResult.terminalObserved,
              );
            if (
              determinateFailure
              && pendingSubmissionReceiptRef.current?.receipt.eventId === receipt.eventId
            ) {
              pendingSubmissionReceiptRef.current = null;
            }
            setSubmissionStatus({
              kind: serverAccepted && !determinateFailure ? "sent" : "error",
              label: action.label,
            });
            return;
          }
          if (pendingSubmissionReceiptRef.current?.receipt.eventId === receipt.eventId) {
            pendingSubmissionReceiptRef.current = null;
          }
          setSubmissionStatus({ kind: "sent", label: action.label });
          if (focused) finishViewTransition(false);
        } catch {
          const durableReceipt = durablePendingReceiptRef.current?.eventId === receipt.eventId
            ? durablePendingReceiptRef.current
            : null;
          if (durableReceipt && !durableReceipt.outcome) {
            setSubmissionStatus({ kind: "sent", label: action.label });
            if (focused) finishViewTransition(false);
          } else {
            setSubmissionStatus({ kind: "error", label: action.label });
          }
        } finally {
          submittingRef.current = false;
        }
      })();
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [finishViewTransition, focused, interactionReadOnly, onSubmit, sourceMessageId, submissionDisabled, surface]);

  const openFocus = useCallback(() => requestViewTransition(true), [requestViewTransition]);
  const closeFocus = useCallback(() => requestViewTransition(false), [requestViewTransition]);

  const frame = (
    <div
      className={`response-surface-runtime${focused ? " is-focused" : ""}`}
      aria-busy={submissionStatus.kind === "sending"}
      tabIndex={0}
    >
      <IsolatedHtmlPreviewFrame
        ref={iframeRef}
        preview={preview}
        title={surface.title}
        className={["response-surface-frame", frameClassName].filter(Boolean).join(" ")}
        style={focused ? { height: "100%" } : (frameStyle || { height: frameHeight })}
      />
      {preview.previewError && (
        <div className="response-surface-fallback">
          <ChatMarkdown content={surface.fallback_markdown} />
        </div>
      )}
      {(submissionDisabled || submissionStatus.kind === "sending") && (
        <div className="response-surface-disabled" role="status" aria-live="polite">
          {submissionStatus.kind === "sending"
            ? t("component.response_surface.sending")
            : t("component.response_surface.wait")}
        </div>
      )}
      {submissionStatus.kind === "error" && (
        <div className="response-surface-submit-error" role="alert">
          {t("component.response_surface.send_failed")}
        </div>
      )}
    </div>
  );

  if (!chrome) return frame;

  return (
    <section
      className={["response-surface", className].filter(Boolean).join(" ")}
      aria-label={surface.title}
    >
      <header className="response-surface-header">
        <div className="response-surface-heading">
          <span className="response-surface-icon" aria-hidden="true">
            <IconSparkles size={15} />
          </span>
          <div>
            <h3>{surface.title}</h3>
            {surface.description && <p>{surface.description}</p>}
          </div>
        </div>
        {surface.display.focusable && (
          <Button
            variant={surface.display.preferred === "focus" ? "primary" : "outline"}
            size="sm"
            onClick={openFocus}
            ariaLabel={`${t("component.response_surface.open")} ${surface.title}`}
            ariaExpanded={focused}
          >
            <IconAspectRatio size={14} />
            {t("component.response_surface.open")}
          </Button>
        )}
      </header>
      {!focused && frame}
      <Modal
        open={focused}
        onClose={closeFocus}
        title={surface.title}
        className="response-surface-modal"
        overlayClassName="response-surface-modal-overlay"
        bodyClassName="response-surface-modal-body"
        width="calc(100vw - 32px)"
        height="calc(100dvh - 32px)"
        maxWidth="1600px"
      >
        {focused && frame}
      </Modal>
    </section>
  );
}
