const ROUTE_REF_SCHEME = "manor-route:";
const ULID_PATTERN = "[0-9A-HJKMNP-TV-Z]{26}";
const ROUTE_REF_RE = new RegExp(
  String.raw`(^|[\s([{<'"“‘，。；：、])((?:https?:\/\/[^\s)\]}>'"“”’]+|\/(?:tasks|viewer)\/${ULID_PATTERN}[^\s)\]}>'"“”’]*))(?=$|[\s)\]}>'"“”’。，、；:：!?！？|])`,
  "giu",
);
const MARKDOWN_LINK_OR_IMAGE_RE = /!?\[[^\]]*\]\([^)]*\)/g;
const FENCED_CODE_RE = /(```[\s\S]*?```|~~~[\s\S]*?~~~)/g;
const INLINE_CODE_RE = /(`[^`\n]+`)/g;

export type ChatRouteReferenceKind = "task" | "viewer";

export interface ChatRouteReference {
  kind: ChatRouteReferenceKind;
  id: string;
  path: string;
}

export function routeReferenceHref(reference: string): string {
  return `${ROUTE_REF_SCHEME}${encodeURIComponent(reference)}`;
}

export function decodeRouteReferenceHref(href: string): string | null {
  if (!href.startsWith(ROUTE_REF_SCHEME)) return null;
  try {
    return decodeURIComponent(href.slice(ROUTE_REF_SCHEME.length));
  } catch {
    return href.slice(ROUTE_REF_SCHEME.length);
  }
}

function trimReferenceCandidate(value: string): string {
  return value.replace(/[.,;:!?，。；：！？、)\]}>'"“”’]+$/g, "").trim();
}

function escapeMarkdownLabel(value: string): string {
  return value.replace(/([\\\]\[])/g, "\\$1");
}

function normalizeCandidate(reference: string): string {
  return trimReferenceCandidate(decodeRouteReferenceHref(reference) || reference);
}

export function parseChatRouteReference(reference: string): ChatRouteReference | null {
  const normalized = normalizeCandidate(reference);
  if (!normalized) return null;
  try {
    const url = new URL(normalized, typeof window === "undefined" ? "http://localhost" : window.location.origin);
    const match = url.pathname.match(new RegExp(`^/(tasks|viewer)/(${ULID_PATTERN})(?:/)?$`, "i"));
    if (!match) return null;
    return {
      kind: match[1].toLowerCase() === "tasks" ? "task" : "viewer",
      id: match[2],
      path: `${url.pathname}${url.search}${url.hash}`,
    };
  } catch {
    return null;
  }
}

export function looksLikeTaskRouteReference(reference: unknown): boolean {
  return typeof reference === "string" && parseChatRouteReference(reference)?.kind === "task";
}

export function looksLikeViewerRouteReference(reference: unknown): boolean {
  return typeof reference === "string" && parseChatRouteReference(reference)?.kind === "viewer";
}

export function chatRouteReferenceLabel(route: ChatRouteReference): string {
  const shortId = route.id.slice(-6);
  return route.kind === "task" ? `Task ${shortId}` : `File ${shortId}`;
}

function linkifyPlainRouteReferencesSegment(segment: string): string {
  return segment.replace(ROUTE_REF_RE, (full, prefix: string, candidate: string) => {
    const reference = trimReferenceCandidate(candidate);
    const route = parseChatRouteReference(reference);
    if (!route) return full;
    return `${prefix}[${escapeMarkdownLabel(chatRouteReferenceLabel(route))}](${routeReferenceHref(reference)})`;
  });
}

function processOutsideInlineCode(segment: string): string {
  return segment
    .split(INLINE_CODE_RE)
    .map((part) => (part.startsWith("`") && part.endsWith("`") ? part : linkifyPlainRouteReferencesSegment(part)))
    .join("");
}

function processOutsideMarkdownLinks(segment: string): string {
  let cursor = 0;
  let output = "";
  segment.replace(MARKDOWN_LINK_OR_IMAGE_RE, (match, offset: number) => {
    output += processOutsideInlineCode(segment.slice(cursor, offset));
    output += match;
    cursor = offset + match.length;
    return match;
  });
  output += processOutsideInlineCode(segment.slice(cursor));
  return output;
}

export function linkifyChatRouteReferencesInMarkdown(source: string): string {
  if (!source || !/(\/tasks\/|\/viewer\/)/i.test(source)) return source;
  return source
    .split(FENCED_CODE_RE)
    .map((part) => {
      const isFence = part.startsWith("```") || part.startsWith("~~~");
      return isFence ? part : processOutsideMarkdownLinks(part);
    })
    .join("");
}

export function preserveReturnToInHistory(returnTo?: string) {
  if (!returnTo || typeof window === "undefined") return;
  if (!returnTo.startsWith("/") || returnTo.startsWith("//")) return;
  try {
    const target = new URL(returnTo, window.location.origin);
    if (
      target.origin !== window.location.origin ||
      target.pathname !== window.location.pathname ||
      target.search !== window.location.search
    ) {
      return;
    }
    const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const next = `${target.pathname}${target.search}${target.hash}`;
    if (current !== next) {
      window.history.replaceState(window.history.state, "", next);
    }
  } catch {
    // If the return target cannot be parsed, leave the current history entry untouched.
  }
}
