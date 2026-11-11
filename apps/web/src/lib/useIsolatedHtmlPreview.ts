import { useCallback, useEffect, useRef, useState } from "react";

const PREVIEW_SHELL_URL = "/html-preview-shell.html";
const PREVIEW_MESSAGE_TIMEOUT_MS = 10_000;

function previewInstanceId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export interface IsolatedHtmlPreviewResult {
  previewUrl: string | null;
  isPreparingPreview: boolean;
  previewError: string | null;
  /** With lockNavigation: the document left its first load, so the frame must not stay mounted. */
  navigatedAway?: boolean;
  retryPreview: () => void;
}

export interface IsolatedHtmlPreviewOptions {
  /** For documents that run generated code against data the host passes them. */
  lockNavigation?: boolean;
}

export function useIsolatedHtmlPreview(
  html: string,
  enabled = true,
  { lockNavigation = false }: IsolatedHtmlPreviewOptions = {},
): IsolatedHtmlPreviewResult {
  const instanceIdRef = useRef<string | null>(null);
  if (!instanceIdRef.current) instanceIdRef.current = previewInstanceId();
  const revisionRef = useRef(0);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [isPreparingPreview, setIsPreparingPreview] = useState(Boolean(enabled));
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [navigatedAway, setNavigatedAway] = useState(false);
  const [retryRevision, setRetryRevision] = useState(0);
  const retryPreview = useCallback(() => {
    setRetryRevision((revision) => revision + 1);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setPreviewUrl(null);
      setIsPreparingPreview(false);
      setPreviewError(null);
      setNavigatedAway(false);
      return;
    }

    const revision = revisionRef.current + 1;
    revisionRef.current = revision;
    const token = `${instanceIdRef.current}-${revision}`;
    const url = `${PREVIEW_SHELL_URL}?preview=${encodeURIComponent(token)}${lockNavigation ? "&navigation=locked" : ""}`;
    let rendered = false;
    let awaitingRender = false;

    setPreviewUrl(url);
    setIsPreparingPreview(true);
    setPreviewError(null);
    setNavigatedAway(false);

    const timeout = window.setTimeout(() => {
      if (rendered) return;
      setIsPreparingPreview(false);
      setPreviewError("HTML preview preparation timed out.");
    }, PREVIEW_MESSAGE_TIMEOUT_MS);

    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.data?.token !== token) return;
      if (event.data?.type === "manor-html-preview:shell-ready") {
        awaitingRender = true;
        const target = event.source as WindowProxy | null;
        target?.postMessage({
          type: "manor-html-preview:render",
          token,
          html,
        }, window.location.origin);
        return;
      }
      if (event.data?.type === "manor-html-preview:rendered") {
        // Each render request has one load, so a further one is the document
        // navigating or reloading itself. A navigation at boot or one that gets
        // a 204 adds no load: the lock stops those, this only cleans up after it.
        if (lockNavigation && !awaitingRender) setNavigatedAway(true);
        awaitingRender = false;
        rendered = true;
        window.clearTimeout(timeout);
        setIsPreparingPreview(false);
        setPreviewError(null);
      } else if (event.data?.type === "manor-html-preview:error") {
        window.clearTimeout(timeout);
        setIsPreparingPreview(false);
        setPreviewError(String(event.data.error || "HTML preview preparation failed."));
      }
    };

    window.addEventListener("message", onMessage);
    return () => {
      window.clearTimeout(timeout);
      window.removeEventListener("message", onMessage);
    };
  }, [enabled, html, lockNavigation, retryRevision]);

  return { previewUrl, isPreparingPreview, previewError, navigatedAway, retryPreview };
}
