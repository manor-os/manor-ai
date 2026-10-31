import { saveFileResponse } from "./fileDownload";
import { AUTH_TOKEN_CHANGED_EVENT, getAuthToken, isSameAuthIdentity } from "./authToken";

/** The API signs one exact, authorized selection; large bytes never enter the cache. */
export async function saveRoomFileSelection(
  name: string,
  prepare: (signal: AbortSignal) => Promise<{ download_url: string }>,
  options: { signal: AbortSignal; onTransferStarted?: () => void },
) {
  const { onTransferStarted } = options;
  options.signal.throwIfAborted();
  const identity = getAuthToken();
  const controller = new AbortController();
  const signal = controller.signal;
  const abort = () => controller.abort(options.signal.reason);
  const checkIdentity = () => {
    if (!isSameAuthIdentity(identity, getAuthToken())) controller.abort(new DOMException("Session changed", "AbortError"));
  };
  options.signal.addEventListener("abort", abort, { once: true });
  window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, checkIdentity);
  window.addEventListener("storage", checkIdentity);
  const prepareUrl = async () => {
    checkIdentity();
    signal.throwIfAborted();
    const ticket = await prepare(signal);
    checkIdentity();
    signal.throwIfAborted();
    const url = new URL(ticket.download_url, window.location.origin);
    if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) {
      throw new Error("The file download URL is invalid.");
    }
    return url.href;
  };
  try {
    return await saveFileResponse(name, async () => {
      const url = await prepareUrl();
      const response = await fetch(url, { signal, cache: "no-store", credentials: "same-origin", redirect: "error" });
      if (!response.ok) {
        const problem = await response.json().catch(() => null);
        throw new Error(typeof problem?.detail === "string" ? problem.detail : `File download failed (HTTP ${response.status}). Please retry.`);
      }
      signal.throwIfAborted();
      onTransferStarted?.();
      return response;
    }, signal, async () => {
      const url = await prepareUrl();
      signal.throwIfAborted();
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      anchor.hidden = true;
      anchor.referrerPolicy = "no-referrer";
      document.body.appendChild(anchor);
      try { anchor.click(); }
      finally { anchor.remove(); }
    });
  } finally {
    options.signal.removeEventListener("abort", abort);
    window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, checkIdentity);
    window.removeEventListener("storage", checkIdentity);
  }
}
