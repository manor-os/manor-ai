/**
 * Script-level guards of the customer-trial frame.
 *
 * The frame is the provider's own origin: script running in it can reach the
 * parent page (window.parent / top) and so the provider's own session in the
 * browser's real storage. The memory storage only keeps the frame's own
 * writes out of it. What keeps customer content out of the frame's script
 * context is:
 *
 * 1. The server boundary (collaborator_session tokens pinned to one
 *    Workspace, live grants) is the access control; nothing here replaces it.
 * 2. Customer-authored HTML, SVG or script is never rendered unsanitized or
 *    unsandboxed in a document of this origin: HTML runs only inside a sandbox
 *    without allow-same-origin (IsolatedHtmlPreviewFrame, externalPreviewFrame),
 *    and markup inserted into the page is DOMPurify-sanitized. This is
 *    mandatory; scripts/embedded-html-sinks.test.mjs fails on any new sink
 *    reachable from the embedded router that is not reviewed.
 * 3. These guards: a link or window.open never navigates to a script URL
 *    (javascript:, data:, a blob: document), and a same-tab app link stays
 *    below the frame's basename (every other app route refuses framing).
 */
import { EMBED } from "./embedContext";

const NAVIGABLE = new Set(["http:", "https:", "mailto:", "tel:"]);

function resolve(href: string): URL | null {
  try {
    return new URL(href, window.location.href);
  } catch {
    return null;
  }
}

/** Whether a document may be opened at this URL from the frame. */
export function frameMayOpen(href: string, { download = false } = {}): boolean {
  if (href === "about:blank") return true;
  const url = resolve(href);
  if (!url) return false;
  // A blob: URL carries this origin: only a download, never a document.
  if (url.protocol === "blob:") return download;
  return NAVIGABLE.has(url.protocol);
}

let installed = false;

export function installFrameGuards(): void {
  if (!EMBED || installed) return;
  installed = true;
  const basename = EMBED.basename;

  const anchor = (event: MouseEvent) => {
    const target = event.target instanceof Element ? event.target.closest("a[href]") : null;
    return target instanceof HTMLAnchorElement ? target : null;
  };
  // Capture, before any handler: a script URL is never followed.
  const blockScriptLinks = (event: MouseEvent) => {
    const target = anchor(event);
    if (target && !frameMayOpen(target.getAttribute("href") || "", { download: target.hasAttribute("download") })) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };
  // Bubble, after the router and page handlers: an unhandled same-tab app
  // path outside the frame would load an unframeable page.
  const keepAppLinksInFrame = (event: MouseEvent) => {
    const target = anchor(event);
    if (!target || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const url = resolve(target.getAttribute("href") || "");
    const sameTab = !target.target || target.target === "_self";
    if (!url || !sameTab || target.hasAttribute("download") || url.origin !== window.location.origin) return;
    if (url.pathname.startsWith(`${basename}/`) || url.pathname === basename || url.pathname.startsWith("/api/")) return;
    event.preventDefault();
    window.location.assign(`${basename}${url.pathname}${url.search}${url.hash}`);
  };
  document.addEventListener("click", blockScriptLinks, true);
  document.addEventListener("auxclick", blockScriptLinks, true);
  document.addEventListener("click", keepAppLinksInFrame);

  const open = window.open.bind(window);
  window.open = (url?: string | URL, target?: string, features?: string) => {
    const href = url === undefined ? "about:blank" : String(url);
    return frameMayOpen(href || "about:blank") ? open(url, target, features) : null;
  };
}
