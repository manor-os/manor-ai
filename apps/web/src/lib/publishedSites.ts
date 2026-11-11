/**
 * Published websites attached to a chat reply.
 *
 * The backend (packages/core/services/chat_artifacts.py) turns a successful
 * publish_site call into one attachment per site:
 * `{ type: "site", name, url, site_id, revision }`. It is not a file: it has
 * no FileRef and must never be rendered or deduplicated as one.
 */
export interface PublishedSiteAttachment {
  name: string;
  url: string;
  siteId: string;
  revision?: number;
}

export function isSiteAttachment(value: unknown): boolean {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
    && String((value as Record<string, unknown>).type || "").trim().toLowerCase() === "site";
}

function httpUrl(value: unknown): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) return "";
  try {
    const url = new URL(text);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

/** Site attachments with a public http(s) address, one per site (the last wins). */
export function publishedSitesFromAttachments(attachments: unknown): PublishedSiteAttachment[] {
  if (!Array.isArray(attachments)) return [];
  const sites = new Map<string, PublishedSiteAttachment>();
  for (const value of attachments) {
    if (!isSiteAttachment(value)) continue;
    const record = value as Record<string, unknown>;
    const url = httpUrl(record.url);
    if (!url) continue;
    const siteId = String(record.site_id || record.siteId || url).trim();
    const revision = typeof record.revision === "number" && Number.isFinite(record.revision)
      ? record.revision
      : undefined;
    sites.delete(siteId);
    sites.set(siteId, {
      name: String(record.name || "").trim() || new URL(url).host,
      url,
      siteId,
      ...(revision === undefined ? {} : { revision }),
    });
  }
  return [...sites.values()];
}

/** The address as people read it: host and path, without the scheme or a bare trailing slash. */
export function publishedSiteDisplayUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === "/" ? "" : parsed.pathname;
    return `${parsed.host}${path}${parsed.search}`;
  } catch {
    return url;
  }
}
