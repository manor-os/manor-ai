import type { CSSProperties } from "react";

export interface WikiLinkInfo {
  target: string;
  display?: string | null;
  resolved_path?: string | null;
  exists: boolean;
  document_id?: string | null;
  document_name?: string | null;
  file_type?: string | null;
  vector_status?: string | null;
}

const WIKI_LINK_RE = /\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g;

export function wikiLinkKey(target: string): string {
  return target.trim().replace(/\.md$/i, "").toLowerCase();
}

export function wikiLinkMap(links: WikiLinkInfo[] | undefined): Map<string, WikiLinkInfo> {
  const out = new Map<string, WikiLinkInfo>();
  for (const link of links || []) out.set(wikiLinkKey(link.target), link);
  return out;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(text: string): string {
  return escapeHtml(text).replace(/'/g, "&#39;");
}

export function renderWikiLinksHtml(src: string, links: WikiLinkInfo[] | undefined): string {
  const byTarget = wikiLinkMap(links);
  return src.replace(WIKI_LINK_RE, (_match, rawTarget: string, rawDisplay?: string) => {
    const target = String(rawTarget || "").trim();
    const display = String(rawDisplay || target).trim();
    const link = byTarget.get(wikiLinkKey(target));
    const docId = link?.document_id || "";
    const exists = Boolean(link?.exists && docId);
    return [
      `<a href="#" class="md-wiki-link${exists ? "" : " md-wiki-link-missing"}"`,
      ` data-wiki-target="${escapeAttr(target)}"`,
      docId ? ` data-wiki-doc-id="${escapeAttr(docId)}"` : "",
      ` title="${escapeAttr(exists ? (link?.document_name || target) : `Missing wiki page: ${target}`)}">`,
      escapeHtml(display),
      "</a>",
    ].join("");
  });
}

export function WikiLinkedText({
  content,
  links,
  onOpenLink,
  className,
  style,
}: {
  content: string;
  links?: WikiLinkInfo[];
  onOpenLink: (link: WikiLinkInfo, target: string) => void;
  className?: string;
  style?: CSSProperties;
}) {
  const byTarget = wikiLinkMap(links);
  const parts: Array<{ text: string; target?: string; display?: string; link?: WikiLinkInfo }> = [];
  let lastIndex = 0;
  for (const match of content.matchAll(WIKI_LINK_RE)) {
    const matchText = match[0] || "";
    const index = match.index ?? 0;
    if (index > lastIndex) parts.push({ text: content.slice(lastIndex, index) });
    const target = String(match[1] || "").trim();
    const display = String(match[2] || target).trim();
    parts.push({
      text: matchText,
      target,
      display,
      link: byTarget.get(wikiLinkKey(target)),
    });
    lastIndex = index + matchText.length;
  }
  if (lastIndex < content.length) parts.push({ text: content.slice(lastIndex) });

  return (
    <div className={className} style={style}>
      {parts.map((part, index) => {
        if (!part.target) return <span key={index}>{part.text}</span>;
        const link = part.link || {
          target: part.target,
          exists: false,
          display: part.display,
        };
        const exists = Boolean(link.exists && link.document_id);
        return (
          <button
            key={`${part.target}-${index}`}
            type="button"
            onClick={() => onOpenLink(link, part.target!)}
            className={`wiki-inline-link${exists ? "" : " wiki-inline-link-missing"}`}
            title={exists ? (link.document_name || part.target) : `Missing wiki page: ${part.target}`}
          >
            {part.display}
          </button>
        );
      })}
    </div>
  );
}
