/**
 * An iframe for a preview URL named by agent or tool output. Content that is
 * this app's own origin must never run with both allow-scripts and
 * allow-same-origin: it would share the app's storage and sign-in (and, in
 * the customer-trial frame, reach the provider's own page). Only http(s)
 * URLs are framed at all; javascript:, data: and blob: are refused.
 */
const PREVIEW_SANDBOX = "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox";

export function externalPreviewFrame(href: string | null | undefined): { src: string; sandbox: string } | null {
  if (!href || typeof window === "undefined") return null;
  let url: URL;
  try {
    url = new URL(href, window.location.origin);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const sameOrigin = url.origin === window.location.origin;
  return { src: url.href, sandbox: sameOrigin ? PREVIEW_SANDBOX : `${PREVIEW_SANDBOX} allow-same-origin` };
}
