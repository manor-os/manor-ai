import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

/**
 * Re-fetches the list response that issued signed media URLs (an App
 * component's data, a Knowledge page), so expired URLs are signed again.
 */
export const SignedMediaRefreshContext = createContext<(() => void) | null>(null);

/** One refetch for every tile that finds its URL expired in the same moment. */
export function useSignedMediaRefresh(refetch: () => unknown): () => void {
  const pending = useRef(false);
  return useCallback(() => {
    if (pending.current) return;
    pending.current = true;
    window.setTimeout(() => {
      pending.current = false;
      void refetch();
    }, 20);
  }, [refetch]);
}

/** An <img> error does not say why; the origin answers 403 when expired or tampered, 404 when missing. */
async function signedMediaStatus(url: string): Promise<number> {
  try {
    return (await fetch(url, { method: "HEAD", credentials: "omit", cache: "no-store" })).status;
  } catch {
    return 0;
  }
}

/**
 * A signed manor-media URL to put straight into `<img src loading="lazy">`,
 * or null when the caller should load the image through the API as before.
 * A 403 asks the issuing list to sign again, once (`waiting` meanwhile); a
 * second failure, a 404, or no URL at all falls back to the API path.
 */
export function useSignedMediaUrl(url?: string | null): { src: string | null; waiting: boolean; onError: () => void } {
  const refresh = useContext(SignedMediaRefreshContext);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const [fellBack, setFellBack] = useState(false);
  const resigned = useRef(false);
  const latestUrl = useRef(url);
  latestUrl.current = url;
  useEffect(() => {
    // A fresh signature from the refetch gets its own try.
    setFailedUrl(null);
  }, [url]);
  const onError = useCallback(() => {
    if (!url) return;
    setFailedUrl(url);
    void signedMediaStatus(url).then((status) => {
      if (status === 403 && refresh && !resigned.current) {
        resigned.current = true;
        refresh();
        // The list answered with the same URL (or not at all): use the API path.
        window.setTimeout(() => { if (latestUrl.current === url) setFellBack(true); }, 5_000);
        return;
      }
      setFellBack(true);
    });
  }, [refresh, url]);
  const waiting = Boolean(url) && !fellBack && failedUrl === url;
  return { src: url && !fellBack && failedUrl !== url ? url : null, waiting, onError };
}
