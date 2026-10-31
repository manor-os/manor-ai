// No imports: this decides, as the entry's first import, whether the page
// boots as the embedded customer-trial frame. The memory storage itself is
// installed earlier still, by the classic script public/embed-isolate.js,
// which runs before any module chunk evaluates.

/** The frame's URL kind and the session kind (`cs.kind`) its token must carry. */
export const EMBED_SESSION_KINDS = { "client-trial": "client_trial", collaboration: "grant" } as const;
export type EmbedKind = keyof typeof EMBED_SESSION_KINDS;

export interface EmbedContext {
  kind: EmbedKind;
  /** The delivery (client-trial) or grant (collaboration) the session must name in `cs.ref`. */
  refId: string;
  sessionKind: (typeof EMBED_SESSION_KINDS)[EmbedKind];
  /** Router basename: every in-frame route lives below it. */
  basename: string;
}

/** Left by public/embed-isolate.js once memory storage replaced Web Storage. */
interface EmbedIsolationMarker {
  kind: string;
  refId: string;
  basename: string;
  localStorage: Storage;
  sessionStorage: Storage;
}

declare global {
  interface Window { __manorEmbed?: EmbedIsolationMarker }
}

const EMBED_PATH = /^\/embed\/(client-trial|collaboration)\/([0-9A-Za-z]{26})(?=\/|$)/;

export function parseEmbedPath(pathname: string): Omit<EmbedContext, "sessionKind"> | null {
  const match = EMBED_PATH.exec(pathname);
  if (!match) return null;
  return { kind: match[1] as EmbedKind, refId: match[2], basename: match[0] };
}

/** Framed, and by a page of this same origin (a cross-origin parent throws). */
export function framedBySameOrigin(win: Window): boolean {
  try {
    return win.parent !== win && win.parent.location.origin === win.location.origin;
  } catch {
    return false;
  }
}

function isolated(path: Omit<EmbedContext, "sessionKind">): boolean {
  const marker = window.__manorEmbed;
  if (!marker || marker.basename !== path.basename) return false;
  try {
    return globalThis.localStorage === marker.localStorage && globalThis.sessionStorage === marker.sessionStorage;
  } catch {
    return false;
  }
}

function detect(): { context: EmbedContext | null; refused: boolean } {
  if (typeof window === "undefined" || !window.location) return { context: null, refused: false };
  const path = parseEmbedPath(window.location.pathname);
  if (!path) return { context: null, refused: false };
  // An embed URL opened on its own, framed elsewhere, or without its memory
  // storage never boots the app (fail closed).
  if (!framedBySameOrigin(window) || !isolated(path)) return { context: null, refused: true };
  return { context: { ...path, sessionKind: EMBED_SESSION_KINDS[path.kind] }, refused: false };
}

const detected = detect();

/** Set when this page is the embedded customer-trial frame. */
export const EMBED: EmbedContext | null = detected.context;
/** An embed URL that may not boot: show the static notice only. */
export const EMBED_REFUSED = detected.refused;

/**
 * A document URL for an app path. Router navigation already stays below the
 * frame's basename; a plain anchor or a location change must too, because
 * every other app route refuses to be framed.
 */
export function appPath(path: string): string {
  return EMBED && path.startsWith("/") && !path.startsWith("//") ? `${EMBED.basename}${path}` : path;
}
