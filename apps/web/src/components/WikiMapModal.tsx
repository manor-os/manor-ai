import { useEffect, useMemo, useRef, useState } from "react";
import Modal from "./ui/Modal";
import {
  IconArrowRight,
  IconEdit,
  IconExternalLink,
  IconLink,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconTrash,
} from "./icons";
import { t } from "../lib/i18n";

export interface WikiMapPage {
  path: string;
  title: string;
  document_id?: string | null;
  document_name?: string | null;
  links?: Array<{
    target: string;
    display?: string | null;
    resolved_path?: string | null;
    exists?: boolean;
    document_id?: string | null;
  }>;
  backlinks?: Array<{ source_path: string; source_title: string }>;
}

export interface WikiMapMissingLink {
  target: string;
  count?: number;
  sources?: Array<{ path: string; title: string }>;
}

const CANVAS_W = 1160;
const CANVAS_H = 680;
const NODE_W = 188;
const NODE_H = 70;
const POS_KEY = "manor.wikiMap.positions.v2";

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function defaultPosition(index: number, total: number): { x: number; y: number } {
  const radius = Math.min(260, Math.max(120, 42 * Math.sqrt(Math.max(total, 1))));
  const angle = total <= 1 ? 0 : (index / total) * Math.PI * 2 - Math.PI / 2;
  return {
    x: CANVAS_W / 2 - NODE_W / 2 + Math.cos(angle) * radius,
    y: CANVAS_H / 2 - NODE_H / 2 + Math.sin(angle) * radius,
  };
}

function nodeCenter(pos: { x: number; y: number }) {
  return { x: pos.x + NODE_W / 2, y: pos.y + NODE_H / 2 };
}

function edgeAnchors(fromPos: { x: number; y: number }, toPos: { x: number; y: number }) {
  const a = nodeCenter(fromPos);
  const b = nodeCenter(toPos);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.max(1, Math.sqrt(dx * dx + dy * dy));
  return {
    a: {
      x: a.x + (dx / len) * (NODE_W * 0.43),
      y: a.y + (dy / len) * (NODE_H * 0.43),
    },
    b: {
      x: b.x - (dx / len) * (NODE_W * 0.43),
      y: b.y - (dy / len) * (NODE_H * 0.43),
    },
  };
}

function includesText(value: string | undefined | null, query: string): boolean {
  return String(value || "").toLowerCase().includes(query);
}

function linkTitle(link: NonNullable<WikiMapPage["links"]>[number], fallback = ""): string {
  return String(link.display || link.target || fallback || "").replace(/\.md$/i, "");
}

function shortWikiPath(path: string): string {
  const parts = String(path || "").split("/").filter(Boolean);
  if (parts.length <= 2) return parts.join("/");
  return `${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
}

export default function WikiMapModal({
  open,
  pages,
  missingLinks,
  onClose,
  onCreatePage,
  onOpenPage,
  onConnect,
  onRemoveLink,
  isConnecting = false,
}: {
  open: boolean;
  pages: WikiMapPage[];
  missingLinks: WikiMapMissingLink[];
  onClose: () => void;
  onCreatePage: (name?: string) => void;
  onOpenPage: (page: WikiMapPage) => void;
  onConnect: (source: WikiMapPage, target: WikiMapPage) => void;
  onRemoveLink?: (source: WikiMapPage, link: NonNullable<WikiMapPage["links"]>[number]) => void;
  isConnecting?: boolean;
}) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const pageByPath = useMemo(() => new Map(pages.map((page) => [page.path, page])), [pages]);
  const pageKey = useMemo(() => pages.map((page) => page.path).sort().join("|"), [pages]);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [dragging, setDragging] = useState<{ path: string; dx: number; dy: number } | null>(null);
  const [linkDraft, setLinkDraft] = useState<{ fromPath: string; x: number; y: number } | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [zoom, setZoom] = useState(1);

  const selectedPage = selectedPath ? pageByPath.get(selectedPath) || null : null;

  useEffect(() => {
    if (!open) return;
    let saved: Record<string, { x: number; y: number }> = {};
    try {
      saved = JSON.parse(localStorage.getItem(POS_KEY) || "{}");
    } catch {
      saved = {};
    }
    const next: Record<string, { x: number; y: number }> = {};
    pages.forEach((page, index) => {
      const pos = saved[page.path] || defaultPosition(index, pages.length);
      next[page.path] = {
        x: clamp(pos.x, 18, CANVAS_W - NODE_W - 18),
        y: clamp(pos.y, 18, CANVAS_H - NODE_H - 18),
      };
    });
    setPositions(next);
    setSelectedPath((prev) => (prev && pages.some((page) => page.path === prev) ? prev : pages[0]?.path || null));
  }, [open, pageKey, pages]);

  useEffect(() => {
    if (!open || Object.keys(positions).length === 0) return;
    localStorage.setItem(POS_KEY, JSON.stringify(positions));
  }, [open, positions]);

  const edges = useMemo(() => {
    const out: Array<{ from: WikiMapPage; to: WikiMapPage; link: NonNullable<WikiMapPage["links"]>[number] }> = [];
    for (const page of pages) {
      for (const link of page.links || []) {
        const to = link.resolved_path ? pageByPath.get(link.resolved_path) : undefined;
        if (to && to.path !== page.path) out.push({ from: page, to, link });
      }
    }
    return out;
  }, [pageByPath, pages]);

  const selectedOutgoingLinks = useMemo(() => {
    if (!selectedPage) return [];
    return (selectedPage.links || []).map((link) => ({
      link,
      targetPage: link.resolved_path ? pageByPath.get(link.resolved_path) || null : null,
    }));
  }, [pageByPath, selectedPage]);

  const selectedIncomingLinks = useMemo(() => {
    if (!selectedPage) return [];
    return (selectedPage.backlinks || []).map((backlink) => ({
      backlink,
      sourcePage: pageByPath.get(backlink.source_path) || null,
    }));
  }, [pageByPath, selectedPage]);

  const normalizedQuery = query.trim().toLowerCase();
  const matchingPaths = useMemo(() => {
    if (!normalizedQuery) return new Set<string>();
    return new Set(
      pages
        .filter((page) => {
          const linkedTargets = (page.links || []).map((link) => link.target).join(" ");
          return includesText(page.title, normalizedQuery)
            || includesText(page.path, normalizedQuery)
            || includesText(linkedTargets, normalizedQuery);
        })
        .map((page) => page.path),
    );
  }, [normalizedQuery, pages]);

  const connectedToSelected = useMemo(() => {
    const out = new Set<string>();
    if (!selectedPath) return out;
    out.add(selectedPath);
    for (const edge of edges) {
      if (edge.from.path === selectedPath) out.add(edge.to.path);
      if (edge.to.path === selectedPath) out.add(edge.from.path);
    }
    return out;
  }, [edges, selectedPath]);

  const pointFromEvent = (event: React.PointerEvent) => {
    const el = canvasRef.current;
    const rect = el?.getBoundingClientRect();
    if (!rect || !el) return { x: 0, y: 0 };
    return {
      x: clamp((event.clientX - rect.left + el.scrollLeft) / zoom, 0, CANVAS_W),
      y: clamp((event.clientY - rect.top + el.scrollTop) / zoom, 0, CANVAS_H),
    };
  };

  const resetLayout = () => {
    const next: Record<string, { x: number; y: number }> = {};
    pages.forEach((page, index) => {
      const pos = defaultPosition(index, pages.length);
      next[page.path] = {
        x: clamp(pos.x, 18, CANVAS_W - NODE_W - 18),
        y: clamp(pos.y, 18, CANVAS_H - NODE_H - 18),
      };
    });
    setPositions(next);
  };

  const finishLink = (event: React.PointerEvent) => {
    if (!linkDraft) return;
    const nodeEl = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest<HTMLElement>("[data-wiki-node-path]");
    const targetPath = nodeEl?.dataset.wikiNodePath;
    if (targetPath && targetPath !== linkDraft.fromPath) {
      const source = pageByPath.get(linkDraft.fromPath);
      const target = pageByPath.get(targetPath);
      if (source && target) onConnect(source, target);
    }
    setLinkDraft(null);
  };

  return (
    <Modal open={open} onClose={onClose} title={t("page.knowledge.wiki_map")} maxWidth="1320px">
      <style>{`
        .wiki-map-shell { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 14px; }
        .wiki-map-canvas { position: relative; height: 660px; overflow: auto; border: 1px solid rgba(28,25,23,.06); border-radius: 24px; background: radial-gradient(circle at 18% 16%, rgba(95,146,138,.06), transparent 34%), radial-gradient(circle at 82% 78%, rgba(178,124,52,.035), transparent 32%), linear-gradient(135deg, rgba(255,255,255,.9), rgba(250,250,249,.92) 46%, rgba(246,245,243,.86)); box-shadow: inset 0 1px 0 rgba(255,255,255,.72), 0 18px 44px rgba(28,25,23,.08); }
        .wiki-map-stage { position: relative; width: ${CANVAS_W}px; height: ${CANVAS_H}px; transform-origin: top left; }
        .wiki-map-stage:before { content: ""; position: absolute; inset: 0; background-image: radial-gradient(rgba(120,113,108,.22) 1px, transparent 1px); background-size: 24px 24px; opacity: .46; }
        .wiki-map-svg { position: absolute; inset: 0; pointer-events: none; z-index: 1; }
        .wiki-map-edge { filter: drop-shadow(0 1px 1px rgba(28,25,23,.06)); transition: stroke .16s ease, opacity .16s ease, stroke-width .16s ease; }
        .wiki-map-edge.is-flowing { stroke-dasharray: 7 12; animation: wikiMapFlow 1.25s linear infinite; }
        .wiki-map-edge-label { fill: #57534e; font-size: 10px; font-weight: 800; letter-spacing: .04em; paint-order: stroke; stroke: rgba(250,250,249,.94); stroke-width: 5px; }
        @keyframes wikiMapFlow { to { stroke-dashoffset: -38; } }
        .wiki-map-node { position: absolute; z-index: 2; width: ${NODE_W}px; min-height: ${NODE_H}px; border: 1px solid rgba(28,25,23,.07); border-radius: 999px; background: rgba(255,255,255,.86); box-shadow: 0 1px 2px rgba(28,25,23,.04), 0 12px 28px rgba(28,25,23,.08), inset 0 1px 0 rgba(255,255,255,.76); cursor: grab; user-select: none; touch-action: none; transition: opacity .16s ease, box-shadow .16s ease, border-color .16s ease, background .16s ease, transform .16s ease; }
        .wiki-map-node:active { cursor: grabbing; }
        .wiki-map-node.is-dim { opacity: .32; }
        .wiki-map-node.is-highlighted { border-color: rgba(67,107,101,.28); box-shadow: 0 0 0 4px rgba(67,107,101,.08), 0 14px 30px rgba(28,25,23,.09); background: rgba(242,246,245,.96); }
        .wiki-map-node.is-selected { border-color: rgba(67,107,101,.42); box-shadow: 0 0 0 5px rgba(67,107,101,.1), 0 16px 34px rgba(28,25,23,.1); background: rgba(255,255,255,.96); }
        .wiki-map-node-inner { padding: 9px 34px 9px 14px; }
        .wiki-map-title { margin: 0; color: #1c1917; font-size: 12px; font-weight: 800; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .wiki-map-path { margin: 3px 0 0; color: #78716c; font-size: 9.5px; font-weight: 750; line-height: 1.1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .wiki-map-meta { margin: 4px 0 0; color: #a8a29e; font-size: 10px; font-weight: 700; }
        .wiki-map-handle { position: absolute; right: 9px; top: 50%; width: 15px; height: 15px; border-radius: 50%; transform: translateY(-50%); border: 2px solid rgba(255,255,255,.94); background: #436b65; box-shadow: 0 0 0 1px rgba(67,107,101,.18), 0 4px 12px rgba(67,107,101,.24); cursor: crosshair; }
        .wiki-map-handle:hover { background: #395a54; }
        .wiki-map-open { position: absolute; left: 13px; bottom: -18px; border: none; background: transparent; color: #436b65; font-size: 10px; font-weight: 800; cursor: pointer; padding: 0; opacity: 0; transition: opacity .16s ease; }
        .wiki-map-node:hover .wiki-map-open, .wiki-map-node.is-selected .wiki-map-open { opacity: 1; }
        .wiki-map-toolbar { position: sticky; top: 12px; left: 12px; z-index: 4; display: inline-flex; align-items: center; gap: 8px; padding: 8px; border: none; border-radius: 18px; background: rgba(255,255,255,.76); backdrop-filter: blur(16px) saturate(135%); box-shadow: 0 1px 2px rgba(28,25,23,.04), 0 12px 30px rgba(28,25,23,.1); }
        .wiki-map-search { display: flex; align-items: center; gap: 6px; min-width: 210px; height: 30px; border-radius: 999px; border: none; background: rgba(245,245,244,.9); color: #57534e; padding: 0 10px; }
        .wiki-map-search input { width: 100%; border: none; outline: none; background: transparent; color: #1c1917; font-size: 12px; }
        .wiki-map-search input::placeholder { color: #a8a29e; }
        .wiki-map-tool-btn { width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; border: none; border-radius: 999px; background: rgba(245,245,244,.92); color: #57534e; cursor: pointer; font-size: 13px; font-weight: 900; }
        .wiki-map-tool-btn:hover { background: rgba(242,246,245,.96); color: #1c1917; }
        .wiki-map-zoom { color: #78716c; font-size: 11px; font-weight: 800; min-width: 38px; text-align: center; }
        .wiki-map-side { border: 1px solid rgba(231,229,228,.74); border-radius: 22px; padding: 15px; background: linear-gradient(180deg, rgba(255,255,255,.86), rgba(250,250,249,.72)); }
        .wiki-map-side-title { margin: 0 0 8px; color: #44403c; font-size: 12px; font-weight: 900; }
        .wiki-map-help { color: #78716c; font-size: 12px; line-height: 1.5; margin: 0 0 12px; }
        .wiki-map-button { display: inline-flex; align-items: center; gap: 6px; border: 1px solid rgba(79,125,117,.24); border-radius: 999px; background: rgba(242,246,245,.76); color: #436b65; padding: 7px 10px; font-size: 12px; font-weight: 800; cursor: pointer; }
        .wiki-map-button:hover { background: rgba(229,238,235,.78); }
        .wiki-map-detail { border: 1px solid rgba(231,229,228,.78); border-radius: 16px; padding: 11px; margin: 13px 0; background: rgba(255,255,255,.72); }
        .wiki-map-detail-name { margin: 0; color: #292524; font-size: 13px; font-weight: 900; line-height: 1.25; }
        .wiki-map-detail-path { margin: 5px 0 0; color: #a8a29e; font-size: 11px; word-break: break-word; }
        .wiki-map-stats { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 10px; }
        .wiki-map-stat { border: 1px solid rgba(214,211,209,.76); border-radius: 999px; background: rgba(250,250,249,.8); color: #78716c; font-size: 11px; font-weight: 800; padding: 5px 8px; }
        .wiki-map-link-section { margin-top: 14px; }
        .wiki-map-link-list { display: grid; gap: 8px; }
        .wiki-map-link-row { display: grid; gap: 7px; border: 1px solid rgba(231,229,228,.86); border-radius: 15px; background: rgba(255,255,255,.78); padding: 9px; }
        .wiki-map-link-row.is-missing { border-style: dashed; background: rgba(250,247,239,.72); }
        .wiki-map-link-main { display: flex; align-items: center; gap: 7px; min-width: 0; color: #44403c; font-size: 12px; font-weight: 850; }
        .wiki-map-link-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .wiki-map-link-direction { color: #436b65; flex: 0 0 auto; }
        .wiki-map-link-subtle { color: #a8a29e; font-size: 10px; font-weight: 750; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .wiki-map-link-actions { display: flex; flex-wrap: wrap; gap: 6px; }
        .wiki-map-link-action { display: inline-flex; align-items: center; gap: 4px; border: 1px solid rgba(214,211,209,.74); border-radius: 999px; background: rgba(250,250,249,.88); color: #57534e; padding: 5px 7px; font-size: 10px; font-weight: 850; cursor: pointer; }
        .wiki-map-link-action:hover { border-color: rgba(95,146,138,.44); color: #436b65; }
        .wiki-map-link-action.is-danger:hover { border-color: rgba(209,139,134,.46); color: #a23e38; }
        .wiki-map-empty-copy { color: #a8a29e; font-size: 11px; line-height: 1.45; margin: 0; }
        .wiki-map-missing { width: 100%; text-align: left; margin-top: 7px; border: 1px dashed rgba(168,162,158,.42); border-radius: 12px; background: rgba(250,250,249,.86); color: #57534e; padding: 8px 9px; cursor: pointer; font-size: 12px; font-weight: 750; }
        .wiki-map-empty { position: absolute; inset: 0; display: grid; place-items: center; z-index: 2; color: #78716c; }
        html[data-theme="dark"] .wiki-map-canvas { border-color: var(--modal-border); background: radial-gradient(circle at 18% 16%, rgba(125,218,205,.08), transparent 34%), radial-gradient(circle at 82% 78%, rgba(178,124,52,.05), transparent 32%), linear-gradient(135deg, rgba(17,17,17,.96), rgba(13,13,13,.98)); box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 18px 44px rgba(0,0,0,.32); }
        html[data-theme="dark"] .wiki-map-stage:before { background-image: radial-gradient(rgba(255,255,255,.16) 1px, transparent 1px); opacity: .34; }
        html[data-theme="dark"] .wiki-map-edge { filter: drop-shadow(0 1px 1px rgba(0,0,0,.36)); }
        html[data-theme="dark"] .wiki-map-edge-label { fill: var(--text-muted); stroke: rgba(17,17,17,.96); }
        html[data-theme="dark"] .wiki-map-node { border-color: var(--modal-border); background: rgba(27,27,27,.92); box-shadow: 0 12px 28px rgba(0,0,0,.24), inset 0 1px 0 rgba(255,255,255,.05); }
        html[data-theme="dark"] .wiki-map-node.is-highlighted,
        html[data-theme="dark"] .wiki-map-node.is-selected { border-color: rgba(139,186,176,.42); background: rgba(35,46,44,.95); box-shadow: 0 0 0 4px rgba(139,186,176,.12), 0 16px 34px rgba(0,0,0,.26); }
        html[data-theme="dark"] .wiki-map-title,
        html[data-theme="dark"] .wiki-map-detail-name,
        html[data-theme="dark"] .wiki-map-side-title,
        html[data-theme="dark"] .wiki-map-link-main { color: var(--text-strong); }
        html[data-theme="dark"] .wiki-map-path,
        html[data-theme="dark"] .wiki-map-help,
        html[data-theme="dark"] .wiki-map-zoom,
        html[data-theme="dark"] .wiki-map-empty,
        html[data-theme="dark"] .wiki-map-link-action { color: var(--text-muted); }
        html[data-theme="dark"] .wiki-map-meta,
        html[data-theme="dark"] .wiki-map-detail-path,
        html[data-theme="dark"] .wiki-map-link-subtle,
        html[data-theme="dark"] .wiki-map-empty-copy { color: var(--text-faint); }
        html[data-theme="dark"] .wiki-map-toolbar,
        html[data-theme="dark"] .wiki-map-side,
        html[data-theme="dark"] .wiki-map-detail,
        html[data-theme="dark"] .wiki-map-link-row,
        html[data-theme="dark"] .wiki-map-link-row.is-missing,
        html[data-theme="dark"] .wiki-map-stat,
        html[data-theme="dark"] .wiki-map-link-action,
        html[data-theme="dark"] .wiki-map-missing { background: var(--modal-muted-bg); border-color: var(--modal-border); }
        html[data-theme="dark"] .wiki-map-search,
        html[data-theme="dark"] .wiki-map-tool-btn { background: var(--modal-control-bg); color: var(--text-default); }
        html[data-theme="dark"] .wiki-map-search input { color: var(--text-strong); }
        html[data-theme="dark"] .wiki-map-search input::placeholder { color: var(--text-faint); }
        html[data-theme="dark"] .wiki-map-tool-btn:hover { background: var(--modal-control-hover-bg); color: var(--text-strong); }
        html[data-theme="dark"] .wiki-map-open,
        html[data-theme="dark"] .wiki-map-link-direction,
        html[data-theme="dark"] .wiki-map-link-action:hover { color: var(--accent); }
        @media (max-width: 860px) { .wiki-map-shell { grid-template-columns: 1fr; } .wiki-map-side { order: -1; } .wiki-map-canvas { height: 560px; } .wiki-map-toolbar { position: relative; top: 10px; left: 10px; flex-wrap: wrap; } }
      `}</style>
      <div className="wiki-map-shell">
        <div
          ref={canvasRef}
          className="wiki-map-canvas"
          onPointerMove={(event) => {
            const point = pointFromEvent(event);
            if (dragging) {
              setPositions((prev) => ({
                ...prev,
                [dragging.path]: {
                  x: clamp(point.x - dragging.dx, 18, CANVAS_W - NODE_W - 18),
                  y: clamp(point.y - dragging.dy, 18, CANVAS_H - NODE_H - 18),
                },
              }));
            }
            if (linkDraft) setLinkDraft((prev) => prev ? { ...prev, ...point } : prev);
          }}
          onPointerUp={(event) => {
            setDragging(null);
            finishLink(event);
          }}
          onPointerLeave={() => setDragging(null)}
        >
          <div className="wiki-map-toolbar">
            <label className="wiki-map-search">
              <IconSearch size={13} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("page.knowledge.wiki_map_search_placeholder")}
              />
            </label>
            <button type="button" className="wiki-map-tool-btn" onClick={() => setZoom((z) => clamp(Number((z - 0.1).toFixed(2)), 0.65, 1.45))}>-</button>
            <span className="wiki-map-zoom">{Math.round(zoom * 100)}%</span>
            <button type="button" className="wiki-map-tool-btn" onClick={() => setZoom((z) => clamp(Number((z + 0.1).toFixed(2)), 0.65, 1.45))}>+</button>
            <button type="button" className="wiki-map-tool-btn" onClick={resetLayout} title={t("page.knowledge.wiki_map_reset")}> <IconRefresh size={13} /> </button>
          </div>

          <div className="wiki-map-stage" style={{ transform: `scale(${zoom})` }}>
            <svg className="wiki-map-svg" viewBox={`0 0 ${CANVAS_W} ${CANVAS_H}`}>
              <defs>
                <marker id="wiki-map-arrow-default" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(120,113,108,.44)" />
                </marker>
                <marker id="wiki-map-arrow-out" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(67,107,101,.76)" />
                </marker>
                <marker id="wiki-map-arrow-in" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(66,108,135,.72)" />
                </marker>
                <marker id="wiki-map-arrow-search" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(178,124,52,.72)" />
                </marker>
              </defs>
              {edges.map((edge, edgeIndex) => {
                const from = positions[edge.from.path];
                const to = positions[edge.to.path];
                if (!from || !to) return null;
                const { a, b } = edgeAnchors(from, to);
                const mid = Math.max(28, Math.abs(b.x - a.x) * 0.34);
                const edgeDirection = selectedPath && edge.from.path === selectedPath ? "out" : selectedPath && edge.to.path === selectedPath ? "in" : null;
                const isSelectedEdge = edgeDirection !== null;
                const isSearchEdge = normalizedQuery && (matchingPaths.has(edge.from.path) || matchingPaths.has(edge.to.path));
                const color = edgeDirection === "out"
                  ? "rgba(67,107,101,.72)"
                  : edgeDirection === "in"
                    ? "rgba(66,108,135,.7)"
                    : isSearchEdge
                      ? "rgba(178,124,52,.68)"
                      : "rgba(120,113,108,.28)";
                const marker = edgeDirection === "out"
                  ? "url(#wiki-map-arrow-out)"
                  : edgeDirection === "in"
                    ? "url(#wiki-map-arrow-in)"
                    : isSearchEdge
                      ? "url(#wiki-map-arrow-search)"
                      : "url(#wiki-map-arrow-default)";
                const pathId = `wiki-map-edge-${edgeIndex}-${edge.from.path.replace(/[^a-z0-9]/gi, "_")}-${edge.to.path.replace(/[^a-z0-9]/gi, "_")}`;
                return (
                  <g key={`${edge.from.path}->${edge.to.path}->${edge.link.target}`}>
                    <path
                      id={pathId}
                      className={`wiki-map-edge${isSelectedEdge || isSearchEdge ? " is-flowing" : ""}`}
                      d={`M ${a.x} ${a.y} C ${a.x + mid} ${a.y}, ${b.x - mid} ${b.y}, ${b.x} ${b.y}`}
                      fill="none"
                      markerEnd={marker}
                      opacity={selectedPath && !isSelectedEdge ? 0.2 : 1}
                      stroke={color}
                      strokeWidth={isSelectedEdge || isSearchEdge ? "2.8" : "1.6"}
                    />
                    {isSelectedEdge && (
                      <text className="wiki-map-edge-label">
                        <textPath href={`#${pathId}`} startOffset="50%" textAnchor="middle">
                          {edgeDirection === "out" ? t("page.knowledge.wiki_map_outgoing") : t("page.knowledge.wiki_map_incoming")}
                        </textPath>
                      </text>
                    )}
                  </g>
                );
              })}
              {linkDraft && (() => {
                const from = positions[linkDraft.fromPath];
                if (!from) return null;
                const a = nodeCenter(from);
                return (
                  <path
                    d={`M ${a.x} ${a.y} C ${a.x + 90} ${a.y}, ${linkDraft.x - 90} ${linkDraft.y}, ${linkDraft.x} ${linkDraft.y}`}
                    fill="none"
                    stroke="rgba(67,107,101,.72)"
                    strokeDasharray="7 6"
                    strokeWidth="2.8"
                  />
                );
              })()}
            </svg>

            {pages.length === 0 ? (
              <div className="wiki-map-empty">
                <button type="button" className="wiki-map-button" onClick={() => onCreatePage()}>
                  <IconPlus size={13} /> {t("page.knowledge.wiki_map_create_first_page")}
                </button>
              </div>
            ) : pages.map((page) => {
              const pos = positions[page.path] || defaultPosition(0, pages.length);
              const isSelected = selectedPath === page.path;
              const isMatch = normalizedQuery ? matchingPaths.has(page.path) : false;
              const isDim = normalizedQuery ? !isMatch : selectedPath ? !connectedToSelected.has(page.path) : false;
              return (
                <div
                  key={page.path}
                  data-wiki-node-path={page.path}
                  className={`wiki-map-node${isDim ? " is-dim" : ""}${isMatch ? " is-highlighted" : ""}${isSelected ? " is-selected" : ""}`}
                  style={{ left: pos.x, top: pos.y }}
                  onPointerDown={(event) => {
                    if ((event.target as HTMLElement).closest(".wiki-map-handle, .wiki-map-open")) return;
                    const point = pointFromEvent(event);
                    setSelectedPath(page.path);
                    setDragging({ path: page.path, dx: point.x - pos.x, dy: point.y - pos.y });
                  }}
                  onDoubleClick={() => onOpenPage(page)}
                >
                  <div className="wiki-map-node-inner">
                    <p className="wiki-map-title">{page.title}</p>
                    <p className="wiki-map-path" title={page.path}>{shortWikiPath(page.path)}</p>
                    <p className="wiki-map-meta">
                      {(page.links?.length || 0)} {t("page.knowledge.wiki_map_out")} - {(page.backlinks?.length || 0)} {t("page.knowledge.wiki_map_in")}
                    </p>
                    <button type="button" className="wiki-map-open" onClick={() => onOpenPage(page)}>{t("page.knowledge.wiki_map_open")}</button>
                  </div>
                  <button
                    type="button"
                    className="wiki-map-handle"
                    title={t("page.knowledge.wiki_map_drag_handle")}
                    disabled={isConnecting}
                    onPointerDown={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      setSelectedPath(page.path);
                      setLinkDraft({ fromPath: page.path, ...pointFromEvent(event) });
                    }}
                  />
                </div>
              );
            })}
          </div>
        </div>

        <aside className="wiki-map-side">
          <p className="wiki-map-side-title">{t("page.knowledge.wiki_map_manual_linking")}</p>
          <p className="wiki-map-help">{t("page.knowledge.wiki_map_help")}</p>
          <button type="button" className="wiki-map-button" onClick={() => onCreatePage()}>
            <IconPlus size={13} /> {t("page.knowledge.new_wiki_page")}
          </button>

          {selectedPage && (
            <div className="wiki-map-detail">
              <p className="wiki-map-detail-name">{selectedPage.title}</p>
              <p className="wiki-map-detail-path">{selectedPage.path}</p>
              <div className="wiki-map-stats">
                <span className="wiki-map-stat">{selectedPage.links?.length || 0} {t("page.knowledge.wiki_map_outgoing")}</span>
                <span className="wiki-map-stat">{selectedPage.backlinks?.length || 0} {t("page.knowledge.wiki_map_backlinks")}</span>
              </div>
              <button type="button" className="wiki-map-button" style={{ marginTop: 10 }} onClick={() => onOpenPage(selectedPage)}>
                <IconEdit size={12} /> {t("page.knowledge.wiki_map_edit_page")}
              </button>
            </div>
          )}

          {selectedPage && (
            <div className="wiki-map-link-section">
              <p className="wiki-map-side-title">{t("page.knowledge.wiki_map_outgoing_links")}</p>
              <div className="wiki-map-link-list">
                {selectedOutgoingLinks.length === 0 && (
                  <p className="wiki-map-empty-copy">{t("page.knowledge.wiki_map_no_outgoing")}</p>
                )}
                {selectedOutgoingLinks.map(({ link, targetPage }, linkIndex) => (
                  <div key={`${selectedPage.path}->${link.target}-${linkIndex}`} className={`wiki-map-link-row${targetPage ? "" : " is-missing"}`}>
                    <div className="wiki-map-link-main">
                      <span className="wiki-map-link-name">{selectedPage.title}</span>
                      <IconArrowRight size={13} className="wiki-map-link-direction" />
                      <span className="wiki-map-link-name">{targetPage?.title || linkTitle(link)}</span>
                    </div>
                    <div className="wiki-map-link-subtle">
                      {targetPage ? targetPage.path : t("page.knowledge.wiki_map_missing_page")}
                    </div>
                    <div className="wiki-map-link-actions">
                      {targetPage ? (
                        <>
                          <button type="button" className="wiki-map-link-action" onClick={() => onOpenPage(targetPage)}>
                            <IconExternalLink size={11} /> {t("page.knowledge.wiki_map_open_target")}
                          </button>
                          <button type="button" className="wiki-map-link-action" disabled={isConnecting} onClick={() => onConnect(targetPage, selectedPage)}>
                            <IconLink size={11} /> {t("page.knowledge.wiki_map_add_reverse_link")}
                          </button>
                        </>
                      ) : (
                        <button type="button" className="wiki-map-link-action" onClick={() => onCreatePage(link.target)}>
                          <IconPlus size={11} /> {t("page.knowledge.wiki_map_create_missing_page")}
                        </button>
                      )}
                      {onRemoveLink && (
                        <button type="button" className="wiki-map-link-action is-danger" disabled={isConnecting} onClick={() => onRemoveLink(selectedPage, link)}>
                          <IconTrash size={11} /> {t("page.knowledge.wiki_map_remove_link")}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {selectedPage && (
            <div className="wiki-map-link-section">
              <p className="wiki-map-side-title">{t("page.knowledge.wiki_map_incoming_links")}</p>
              <div className="wiki-map-link-list">
                {selectedIncomingLinks.length === 0 && (
                  <p className="wiki-map-empty-copy">{t("page.knowledge.wiki_map_no_incoming")}</p>
                )}
                {selectedIncomingLinks.map(({ backlink, sourcePage }) => (
                  <div key={`${backlink.source_path}->${selectedPage.path}`} className="wiki-map-link-row">
                    <div className="wiki-map-link-main">
                      <span className="wiki-map-link-name">{backlink.source_title}</span>
                      <IconArrowRight size={13} className="wiki-map-link-direction" />
                      <span className="wiki-map-link-name">{selectedPage.title}</span>
                    </div>
                    <div className="wiki-map-link-subtle">{backlink.source_path}</div>
                    <div className="wiki-map-link-actions">
                      {sourcePage && (
                        <button type="button" className="wiki-map-link-action" onClick={() => onOpenPage(sourcePage)}>
                          <IconExternalLink size={11} /> {t("page.knowledge.wiki_map_open_source")}
                        </button>
                      )}
                      {sourcePage && (
                        <button type="button" className="wiki-map-link-action" disabled={isConnecting} onClick={() => onConnect(selectedPage, sourcePage)}>
                          <IconLink size={11} /> {t("page.knowledge.wiki_map_add_reverse_link")}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {missingLinks.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <p className="wiki-map-side-title">{t("page.knowledge.wiki_map_pages_to_create")}</p>
              {missingLinks.slice(0, 8).map((item) => (
                <button key={item.target} type="button" className="wiki-map-missing" onClick={() => onCreatePage(item.target)}>
                  <IconLink size={12} /> {item.target}
                </button>
              ))}
            </div>
          )}
        </aside>
      </div>
    </Modal>
  );
}
