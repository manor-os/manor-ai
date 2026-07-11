import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
import { useLocation, useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { PrismAsync as CodeSyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import { api } from "../lib/api";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import Select from "../components/ui/Select";
import Dropdown from "../components/ui/Dropdown";
import AiEditButton from "../components/ui/AiEditButton";
import EditorLiveInlineDiff from "../components/EditorLiveInlineDiff";
import {
  IconArrowLeft,
  IconArrowDown,
  IconArrowUp,
  IconArrowRight,
  IconClock,
  IconCheck,
  IconClose,
  IconCode,
  IconComment,
  IconCopy,
  IconEraser,
  IconEye,
  IconHighlighter,
  IconImage,
  IconLayers,
  IconLink,
  IconList,
  IconPalette,
  IconPlay,
  IconPlus,
  IconText,
  IconTrash,
  IconTrendingUp,
  IconUndo,
  IconRedo,
  IconSearch,
  IconSettings,
} from "../components/icons";
import CommentThread from "../components/CommentThread";
import { wikiLinkKey, wikiLinkMap, type WikiLinkInfo } from "../components/WikiLinkedText";
import DiagramCanvas from "../components/diagram/DiagramCanvas";
import {
  parseDiagramDocument,
  serializeDiagramDocument,
  type EditableDiagramDocument,
} from "../lib/diagram/schema";
import {
  openEditorLiveChat,
  type EditorLiveApplyMeta,
} from "../lib/editorLiveChat";
import { getAuthToken } from "../lib/authToken";
import { codeLanguageForFile, codeLanguageLabel, isCodeLikeFile } from "../lib/codeFiles";
import { useAuthStore } from "../stores/auth";
import { canCommentDocument, canEditDocument } from "../lib/permissions";
import type { Comment, CommentAnchor } from "../lib/types";

import { t } from "../lib/i18n";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type EditorMode = "richtext" | "markdown" | "code" | "text" | "spreadsheet" | "presentation" | "diagram";
type MarkdownViewMode = "source" | "split" | "preview";
type MarkdownEditResult = { next: string; selectionStart: number; selectionEnd?: number };

const RICH_TEXT_FONTS = [
  "Inter",
  "Arial",
  "Georgia",
  "Times New Roman",
  "Courier New",
  "Verdana",
];

const RICH_TEXT_BLOCK_OPTIONS = [
  { value: "p", label: "Normal" },
  { value: "h1", label: "Heading 1" },
  { value: "h2", label: "Heading 2" },
  { value: "h3", label: "Heading 3" },
  { value: "blockquote", label: "Quote" },
  { value: "pre", label: "Code block" },
];

const RICH_TEXT_FONT_SIZES = [
  { label: "12", value: "12" },
  { label: "14", value: "14" },
  { label: "16", value: "16" },
  { label: "18", value: "18" },
  { label: "24", value: "24" },
  { label: "32", value: "32" },
];

const ideEditorTheme: Record<string, React.CSSProperties> = {
  ...vscDarkPlus,
  'pre[class*="language-"]': {
    ...(vscDarkPlus['pre[class*="language-"]'] as React.CSSProperties),
    margin: 0,
    padding: "20px 24px 48px",
    overflow: "visible",
    background: "transparent",
    fontSize: 13,
    lineHeight: "1.65rem",
  },
  'code[class*="language-"]': {
    ...(vscDarkPlus['code[class*="language-"]'] as React.CSSProperties),
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    fontSize: 13,
    lineHeight: "1.65rem",
    textShadow: "none",
    whiteSpace: "pre",
  },
};

const richTextSelectButtonStyle: React.CSSProperties = {
  height: 32,
  minHeight: 32,
  borderRadius: 8,
  border: "1px solid var(--editor-control-border, #e2dfdc)",
  background: "var(--editor-control-bg, #ffffff)",
  color: "var(--editor-control-text, #44403c)",
  padding: "0 28px 0 10px",
  fontSize: 12,
  fontWeight: 750,
  boxShadow: "var(--editor-control-shadow, none)",
};

const WIKI_MARKDOWN_LINK_RE = /\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g;

function escapeMarkdownLabel(text: string): string {
  return text.replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}

function markdownWithWikiLinks(src: string): string {
  return src.replace(WIKI_MARKDOWN_LINK_RE, (_match, rawTarget: string, rawDisplay?: string) => {
    const target = String(rawTarget || "").trim();
    const display = String(rawDisplay || target).trim();
    return `[${escapeMarkdownLabel(display)}](#wiki:${encodeURIComponent(target)})`;
  });
}

function markdownWordCount(text: string): number {
  return wordCount(
    text
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/!\[[^\]]*]\([^)]+\)/g, " ")
      .replace(/\[[^\]]+]\([^)]+\)/g, " ")
      .replace(/[#>*_`~|[\]()-]/g, " "),
  );
}

function getKnowledgeReturnTo(state: unknown): string | null {
  if (!state || typeof state !== "object") return null;
  const value = (state as { chatReturnTo?: unknown; knowledgeReturnTo?: unknown; returnTo?: unknown }).chatReturnTo
    ?? (state as { knowledgeReturnTo?: unknown; returnTo?: unknown }).knowledgeReturnTo
    ?? (state as { returnTo?: unknown }).returnTo;
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : null;
}

function isEditableDomTarget(target: EventTarget | null): boolean {
  const element = target instanceof HTMLElement ? target : null;
  if (!element) return false;
  return Boolean(element.closest("input, textarea, select, [contenteditable='true']"));
}

const COMMENT_QUOTE_LIMIT = 180;

function trimCommentQuote(value: string): string {
  const clean = value.replace(/\s+/g, " ").trim();
  return clean.length > COMMENT_QUOTE_LIMIT
    ? `${clean.slice(0, COMMENT_QUOTE_LIMIT - 1)}...`
    : clean;
}

function textLineAnchor(text: string, start: number, end: number, mode: string): CommentAnchor {
  const normalizedStart = Math.max(0, Math.min(start, text.length));
  const normalizedEnd = Math.max(normalizedStart, Math.min(end, text.length));
  const line = text.slice(0, normalizedStart).split("\n").length;
  const lineEnd = text.slice(0, normalizedEnd).split("\n").length;
  const quote = trimCommentQuote(text.slice(normalizedStart, normalizedEnd));
  return {
    type: "text_range",
    mode,
    line,
    line_end: lineEnd,
    start: normalizedStart,
    end: normalizedEnd,
    quote: quote || undefined,
  };
}

function flattenComments(comments: Comment[]): Comment[] {
  const out: Comment[] = [];
  const visit = (comment: Comment) => {
    out.push(comment);
    comment.replies?.forEach(visit);
  };
  comments.forEach(visit);
  return out;
}

function detectMode(name: string): EditorMode {
  if (isDiagramFile(name)) return "diagram";
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "xlsx" || ext === "xls" || ext === "csv") return "spreadsheet";
  if (ext === "pptx" || ext === "ppt") return "presentation";
  if (isCodeLikeFile(name)) return "code";
  if (isPlainTextFile(name)) return "text";
  return "richtext"; // docx, doc, and other document-like files use rich text
}

function isDiagramFile(name: string): boolean {
  const lower = name.toLowerCase();
  return lower.endsWith(".diagram.json") || lower.endsWith(".diagram");
}

function isOfficeDoc(name: string): boolean {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return ["docx", "doc"].includes(ext);
}

function isPlainTextFile(name: string): boolean {
  const lower = name.toLowerCase();
  const base = lower.split(/[\\/]/).pop() || lower;
  const ext = (lower.split(".").pop() || "").toLowerCase();
  if ([".gitignore", ".dockerignore", ".editorconfig"].includes(base)) return true;
  return ["txt", "text", "log"].includes(ext);
}

function isHtmlFile(name: string): boolean {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return ["html", "htm"].includes(ext);
}

function isSvgFile(name: string): boolean {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return ext === "svg";
}

function isRenderableCodeFile(name: string): boolean {
  return isHtmlFile(name) || isSvgFile(name);
}

function renderableCodePreviewLabel(name: string): string {
  return isSvgFile(name) ? "SVG" : "HTML";
}

function isLocalPreviewAssetUrl(url: string): boolean {
  const trimmed = url.trim();
  if (!trimmed || trimmed.startsWith("#")) return false;
  return !/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(trimmed);
}

type PreviewAssetReplacement =
  | { kind: "url"; value: string }
  | { kind: "style"; value: string }
  | { kind: "script"; value: string };

function stripUrlSuffix(url: string): string {
  return url.split(/[?#]/, 1)[0] || "";
}

function normalizePreviewPath(path: string): string {
  const parts: string[] = [];
  for (const rawPart of path.replace(/\\/g, "/").split("/")) {
    const part = rawPart.trim();
    if (!part || part === ".") continue;
    if (part === "..") {
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  return parts.join("/");
}

function dirname(path: string): string {
  const normalized = normalizePreviewPath(path);
  const idx = normalized.lastIndexOf("/");
  return idx >= 0 ? normalized.slice(0, idx) : "";
}

function resolvePreviewAssetPath(currentFsPath: string | undefined | null, rawUrl: string): string | null {
  if (!currentFsPath || !isLocalPreviewAssetUrl(rawUrl)) return null;
  const cleanUrl = stripUrlSuffix(rawUrl).replace(/^\/+/, "");
  if (!cleanUrl) return null;
  const baseDir = dirname(currentFsPath);
  return normalizePreviewPath(`${baseDir}/${cleanUrl}`);
}

function extractLocalHtmlAssetRefs(html: string): string[] {
  const refs = new Set<string>();
  const attrRe = /\b(?:src|href|poster)\s*=\s*(["'])(.*?)\1/gi;
  let attrMatch: RegExpExecArray | null;
  while ((attrMatch = attrRe.exec(html))) {
    const url = attrMatch[2]?.trim();
    if (url && isLocalPreviewAssetUrl(url)) refs.add(url);
  }

  const cssUrlRe = /url\(\s*(["']?)(.*?)\1\s*\)/gi;
  let cssMatch: RegExpExecArray | null;
  while ((cssMatch = cssUrlRe.exec(html))) {
    const url = cssMatch[2]?.trim();
    if (url && isLocalPreviewAssetUrl(url)) refs.add(url);
  }
  return [...refs];
}

function escapePreviewAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function escapeRawElementText(value: string, tagName: "script" | "style"): string {
  const closingTag = new RegExp(`</${tagName}`, "gi");
  return value.replace(closingTag, `<\\/${tagName}`);
}

function previewAssetKind(ref: string, result: { encoding: string; mime_type?: string }): PreviewAssetReplacement["kind"] | null {
  if (result.encoding !== "utf-8") return null;
  const cleanRef = stripUrlSuffix(ref).toLowerCase();
  const mime = (result.mime_type || "").toLowerCase();
  if (cleanRef.endsWith(".css") || mime === "text/css") return "style";
  if (
    cleanRef.endsWith(".js") ||
    cleanRef.endsWith(".mjs") ||
    cleanRef.endsWith(".cjs") ||
    mime.includes("javascript") ||
    mime === "text/ecmascript"
  ) {
    return "script";
  }
  return null;
}

function rewriteHtmlLocalAssetUrls(html: string, assets: Record<string, PreviewAssetReplacement>): string {
  if (!Object.keys(assets).length) return html;
  return html
    .replace(/<link\b([^>]*?)\bhref\s*=\s*(["'])(.*?)\2([^>]*)>/gi, (match, before, _quote, url, after) => {
      const asset = assets[String(url).trim()];
      const attrs = `${before || ""} ${after || ""}`;
      if (asset?.kind === "style" && /\brel\s*=\s*(["'])?[^"'>\s]*stylesheet/i.test(attrs)) {
        return `<style data-manor-preview-src="${escapePreviewAttr(String(url).trim())}">\n${escapeRawElementText(asset.value, "style")}\n</style>`;
      }
      if (asset?.kind === "url") {
        return match.replace(String(url), asset.value);
      }
      return match;
    })
    .replace(/<script\b([^>]*?)\bsrc\s*=\s*(["'])(.*?)\2([^>]*)>([\s\S]*?)<\/script>/gi, (match, before, _quote, url, after) => {
      const asset = assets[String(url).trim()];
      if (asset?.kind === "script") {
        const attrs = `${before || ""}${after || ""}`.replace(
          /\s+\b(?:async|defer|crossorigin|integrity|referrerpolicy)\b(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?/gi,
          "",
        );
        return `<script${attrs} data-manor-preview-src="${escapePreviewAttr(String(url).trim())}">\n${escapeRawElementText(asset.value, "script")}\n</script>`;
      }
      if (asset?.kind === "url") {
        return match.replace(String(url), asset.value);
      }
      return match;
    })
    .replace(/\b(src|href|poster)\s*=\s*(["'])(.*?)\2/gi, (match, attr, quote, url) => {
      const replacement = assets[String(url).trim()];
      return replacement?.kind === "url" ? `${attr}=${quote}${replacement.value}${quote}` : match;
    })
    .replace(/url\(\s*(["']?)(.*?)\1\s*\)/gi, (match, quote, url) => {
      const replacement = assets[String(url).trim()];
      return replacement?.kind === "url" ? `url(${quote || ""}${replacement.value}${quote || ""})` : match;
    });
}

const HTML_PREVIEW_NAVIGATION_GUARD = [
  '<base href="about:blank">',
  "<script>",
  "(() => {",
  "  const isNavigationHref = (href) => {",
  "    const value = String(href || '').trim();",
  "    return Boolean(value) && !value.startsWith('#') && !/^(?:javascript:|mailto:|tel:|data:|blob:)/i.test(value);",
  "  };",
  "  const block = (event) => {",
  "    event.preventDefault();",
  "    event.stopImmediatePropagation();",
  "  };",
  "  document.addEventListener('click', (event) => {",
  "    const target = event.target instanceof Element ? event.target : null;",
  "    if (!target) return;",
  "    const link = target.closest('a[href], area[href]');",
  "    if (link && isNavigationHref(link.getAttribute('href'))) {",
  "      block(event);",
  "      return;",
  "    }",
  "    if (target.closest('button[formaction], input[formaction], button[type=\"submit\"], input[type=\"submit\"]')) {",
  "      block(event);",
  "    }",
  "  }, true);",
  "  document.addEventListener('submit', block, true);",
  "  window.open = () => null;",
  "})();",
  "</script>",
].join("");

function injectHtmlPreviewNavigationGuard(html: string): string {
  if (/<head(?:\s|>)/i.test(html)) {
    return html.replace(/<head([^>]*)>/i, `<head$1>${HTML_PREVIEW_NAVIGATION_GUARD}`);
  }
  if (/<html(?:\s|>)/i.test(html)) {
    return html.replace(/<html([^>]*)>/i, `<html$1><head>${HTML_PREVIEW_NAVIGATION_GUARD}</head>`);
  }
  return `<!doctype html><html><head>${HTML_PREVIEW_NAVIGATION_GUARD}</head><body>${html}</body></html>`;
}

function blobFromFsReadResult(result: { content: string; encoding: string; mime_type?: string }): Blob {
  const mimeType = result.mime_type || "application/octet-stream";
  if (result.encoding === "base64") {
    const binary = window.atob(result.content);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: mimeType });
  }
  return new Blob([result.content], { type: mimeType });
}

function isSpreadsheetFile(name: string): boolean {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return ["xlsx", "xls"].includes(ext);
}

function isCsvFile(name: string): boolean {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return ext === "csv";
}

function isPptxFile(name: string): boolean {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return ["pptx", "ppt"].includes(ext);
}

function extLabel(name: string): string {
  return (name.split(".").pop() || "").toUpperCase();
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function wordCount(text: string): number {
  return text.trim() ? text.trim().split(/\s+/).length : 0;
}

// ---------------------------------------------------------------------------
// Spreadsheet Editor sub-component
// ---------------------------------------------------------------------------

function normalizeSheetData(value: any[][] | null): any[][] {
  return value && value.length > 0 ? value : [[""]];
}

function parseCsvText(text: string): any[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];

    if (ch === '"') {
      if (inQuotes && next === '"') {
        cell += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }

    if (ch === "," && !inQuotes) {
      row.push(cell);
      cell = "";
      continue;
    }

    if ((ch === "\n" || ch === "\r") && !inQuotes) {
      if (ch === "\r" && next === "\n") i += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      continue;
    }

    cell += ch;
  }

  row.push(cell);
  if (row.some((value) => value !== "") || rows.length === 0) rows.push(row);
  return normalizeSheetData(rows);
}

function parseSpreadsheetPasteText(text: string): string[][] {
  const normalized = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (normalized.includes("\t")) {
    const lines = normalized.endsWith("\n") ? normalized.slice(0, -1).split("\n") : normalized.split("\n");
    return lines.map((line) => line.split("\t"));
  }
  return parseCsvText(text).map((row) => row.map((cell) => String(cell ?? "")));
}

type SheetChartType = "bar" | "line" | "pie";

interface SheetChartConfig {
  id: string;
  type: SheetChartType;
  title: string;
  labelColumn: number;
  valueColumn: number;
  startRow: number;
  endRow: number;
}

type SheetTextAlign = "left" | "center" | "right";

interface SheetCellStyle {
  bold?: boolean;
  italic?: boolean;
  fontSize?: number;
  fontFamily?: string;
  color?: string;
  fill?: string;
  align?: SheetTextAlign;
}

type SheetStyleMap = Record<string, SheetCellStyle>;

const SPREADSHEET_EDITOR_PAYLOAD_PREFIX = "__MANOR_SPREADSHEET_EDITOR_V1__\n";
const SPREADSHEET_CHARTS_SHEET = "_manor_charts";
const SHEET_CHART_COLORS = ["#4869ac", "#4f7d75", "#d3873f", "#6f4ba8", "#c14a44", "#44895f"];
const MIN_VISIBLE_SHEET_ROWS = 32;
const MIN_VISIBLE_SHEET_COLS = 12;

type SheetCellCoord = { r: number; c: number };
type SheetRange = { r1: number; c1: number; r2: number; c2: number };

const sheetToolbarButtonBase: React.CSSProperties = {
  height: 30,
  minWidth: 30,
  border: "1px solid transparent",
  borderRadius: 8,
  background: "transparent",
  color: "var(--editor-control-text, #57534e)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  padding: "0 9px",
  fontSize: 12,
  fontWeight: 800,
  lineHeight: 1,
  cursor: "pointer",
  whiteSpace: "nowrap",
};

const sheetToolbarSelectStyle: React.CSSProperties = {
  height: 30,
  minWidth: 0,
  border: "1px solid var(--editor-control-border, rgba(28,25,23,0.06))",
  borderRadius: 8,
  background: "var(--editor-control-bg, #ffffff)",
  color: "var(--editor-control-text, #44403c)",
  padding: "0 28px 0 9px",
  fontSize: 12,
  fontWeight: 750,
  boxShadow: "var(--editor-control-shadow, none)",
};

function SheetToolbarGroup({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: 0,
        border: "none",
        borderRadius: 0,
        background: "transparent",
        minWidth: 0,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

function SheetToolbarButton({
  children,
  icon,
  title,
  active,
  danger,
  disabled,
  onClick,
  role,
  style,
}: {
  children?: React.ReactNode;
  icon?: React.ReactNode;
  title: string;
  active?: boolean;
  danger?: boolean;
  disabled?: boolean;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  role?: string;
  style?: React.CSSProperties;
}) {
  const borderColor = active
    ? "var(--editor-active-border, #ccded9)"
    : danger
      ? "var(--editor-danger-border, #ecc8c5)"
      : "transparent";
  const background = active
    ? "var(--editor-active-bg, #ecfdf8)"
    : danger
      ? "var(--editor-danger-bg, #fff7f7)"
      : "transparent";
  const color = active
    ? "var(--editor-active-text, #436b65)"
    : danger
      ? "var(--editor-danger-text, #a23e38)"
      : "var(--editor-control-text, #44403c)";
  return (
    <button
      type="button"
      title={title}
      role={role}
      aria-pressed={active || undefined}
      disabled={disabled}
      onClick={onClick}
      style={{
        ...sheetToolbarButtonBase,
        borderColor,
        background,
        color,
        opacity: disabled ? 0.45 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
        ...style,
      }}
    >
      {icon && <span style={{ display: "inline-flex", alignItems: "center", color: "currentColor" }}>{icon}</span>}
      {children && <span>{children}</span>}
    </button>
  );
}

function SheetColorControl({
  icon,
  label,
  title,
  value,
  onChange,
}: {
  icon: React.ReactNode;
  label: string;
  title: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label
      title={title}
      style={{
        ...sheetToolbarButtonBase,
        position: "relative",
        padding: "0 8px",
        overflow: "hidden",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center" }}>{icon}</span>
      <span style={{ fontSize: 11 }}>{label}</span>
      <span
        aria-hidden="true"
        style={{
          width: 14,
          height: 14,
          borderRadius: 4,
          border: "1px solid rgba(28,25,23,0.18)",
          background: value,
          boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.55)",
        }}
      />
      <input
        type="color"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        style={{ position: "absolute", inset: 0, opacity: 0, cursor: "pointer" }}
      />
    </label>
  );
}

function sheetStyleKey(row: number, col: number): string {
  return `${row}:${col}`;
}

function csvEscape(value: unknown): string {
  const s = value != null ? String(value) : "";
  return s.includes(",") || s.includes('"') || s.includes("\n") ? `"${s.replace(/"/g, '""')}"` : s;
}

function sheetDataToCsv(data: any[][]): string {
  return data.map((row) => row.map(csvEscape).join(",")).join("\n");
}

function normalizeSheetCharts(charts: unknown, data: any[][]): SheetChartConfig[] {
  if (!Array.isArray(charts)) return [];
  const rowCount = Math.max(1, data.length);
  const colCount = Math.max(1, ...data.map((row) => row.length));
  const clampIndex = (value: unknown, fallback: number, max: number) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(0, Math.min(max, Math.trunc(parsed)));
  };
  return charts.flatMap((chart) => {
    if (!chart || typeof chart !== "object") return [];
    const raw = chart as Partial<SheetChartConfig>;
    const type: SheetChartType = raw.type === "line" || raw.type === "pie" ? raw.type : "bar";
    const labelColumn = clampIndex(raw.labelColumn, 0, colCount - 1);
    const valueColumn = clampIndex(raw.valueColumn, Math.min(1, colCount - 1), colCount - 1);
    const startRow = clampIndex(raw.startRow, rowCount > 1 ? 1 : 0, rowCount - 1);
    const endRow = Math.max(startRow, clampIndex(raw.endRow, rowCount - 1, rowCount - 1));
    return [{
      id: typeof raw.id === "string" && raw.id ? raw.id : genId(),
      type,
      title: typeof raw.title === "string" && raw.title ? raw.title : `${type.toUpperCase()} Chart`,
      labelColumn,
      valueColumn,
      startRow,
      endRow,
    }];
  });
}

function normalizeSheetStyles(styles: unknown): SheetStyleMap {
  if (!styles || typeof styles !== "object" || Array.isArray(styles)) return {};
  const normalized: SheetStyleMap = {};
  Object.entries(styles as Record<string, unknown>).forEach(([key, raw]) => {
    if (!/^\d+:\d+$/.test(key) || !raw || typeof raw !== "object" || Array.isArray(raw)) return;
    const style = raw as Partial<SheetCellStyle>;
    const next: SheetCellStyle = {};
    if (typeof style.bold === "boolean") next.bold = style.bold;
    if (typeof style.italic === "boolean") next.italic = style.italic;
    if (typeof style.fontFamily === "string" && style.fontFamily.trim()) next.fontFamily = style.fontFamily.trim();
    if (typeof style.color === "string" && style.color.trim()) next.color = style.color.trim();
    if (typeof style.fill === "string" && style.fill.trim()) next.fill = style.fill.trim();
    if (style.align === "left" || style.align === "center" || style.align === "right") next.align = style.align;
    const fontSize = Number(style.fontSize);
    if (Number.isFinite(fontSize)) next.fontSize = Math.max(8, Math.min(72, Math.trunc(fontSize)));
    if (Object.keys(next).length > 0) normalized[key] = next;
  });
  return normalized;
}

function parseSpreadsheetPayload(text: string): { data: any[][]; charts: SheetChartConfig[]; styles: SheetStyleMap } | null {
  if (!text.startsWith(SPREADSHEET_EDITOR_PAYLOAD_PREFIX)) return null;
  try {
    const parsed = JSON.parse(text.slice(SPREADSHEET_EDITOR_PAYLOAD_PREFIX.length)) as { data?: any[][]; charts?: unknown; styles?: unknown };
    const data = normalizeSheetData(Array.isArray(parsed.data) ? parsed.data : null);
    return { data, charts: normalizeSheetCharts(parsed.charts, data), styles: normalizeSheetStyles(parsed.styles) };
  } catch {
    return null;
  }
}

function serializeSpreadsheetContent(data: any[][], charts: SheetChartConfig[], persistCharts: boolean, styles: SheetStyleMap = {}): string {
  const cleanStyles = normalizeSheetStyles(styles);
  if (!persistCharts || (charts.length === 0 && Object.keys(cleanStyles).length === 0)) return sheetDataToCsv(data);
  return `${SPREADSHEET_EDITOR_PAYLOAD_PREFIX}${JSON.stringify({ data, charts, styles: cleanStyles })}`;
}

function readWorkbookEditorMetadata(workbook: any, data: any[][]): { charts: SheetChartConfig[]; styles: SheetStyleMap } {
  const sheet = workbook?.Sheets?.[SPREADSHEET_CHARTS_SHEET];
  if (!sheet) return { charts: [], styles: {} };
  try {
    const rows = (Object.values(sheet) as any[])
      .filter((cell) => cell && typeof cell === "object" && "v" in cell)
      .map((cell) => String(cell.v))
      .join("");
    const parsed = JSON.parse(rows) as { charts?: unknown; styles?: unknown };
    return {
      charts: normalizeSheetCharts(parsed.charts, data),
      styles: normalizeSheetStyles(parsed.styles),
    };
  } catch {
    return { charts: [], styles: {} };
  }
}

function spreadsheetCellLabel(data: any[][], column: number): string {
  const header = data[0]?.[column];
  const text = header != null && String(header).trim() ? String(header).trim() : colLetter(column);
  return `${colLetter(column)} · ${text}`;
}

function spreadsheetCellRef(row: number, col: number): string {
  return `${colLetter(Math.max(0, col))}${Math.max(0, row) + 1}`;
}

function spreadsheetRangeRef(range: SheetRange | null): string {
  if (!range) return "";
  const start = spreadsheetCellRef(range.r1, range.c1);
  const end = spreadsheetCellRef(range.r2, range.c2);
  return start === end ? start : `${start}:${end}`;
}

function normalizeSheetSelection(selected: SheetCellCoord | null, anchor: SheetCellCoord | null): SheetRange | null {
  if (!selected) return null;
  const start = anchor || selected;
  return {
    r1: Math.min(start.r, selected.r),
    c1: Math.min(start.c, selected.c),
    r2: Math.max(start.r, selected.r),
    c2: Math.max(start.c, selected.c),
  };
}

function sheetRangeSize(range: SheetRange | null): number {
  if (!range) return 0;
  return (range.r2 - range.r1 + 1) * (range.c2 - range.c1 + 1);
}

function sheetCellInRange(row: number, col: number, range: SheetRange | null): boolean {
  return Boolean(range && row >= range.r1 && row <= range.r2 && col >= range.c1 && col <= range.c2);
}

function ensureSheetDimensions(data: any[][], rows: number, cols: number): any[][] {
  const next = data.length ? data.map((row) => [...row]) : [[""]];
  while (next.length < rows) next.push([]);
  for (const row of next) {
    while (row.length < cols) row.push("");
  }
  return next;
}

function sheetRangeToTsv(data: any[][], range: SheetRange): string {
  const rows: string[] = [];
  for (let r = range.r1; r <= range.r2; r += 1) {
    const cells: string[] = [];
    for (let c = range.c1; c <= range.c2; c += 1) {
      cells.push(String(data[r]?.[c] ?? ""));
    }
    rows.push(cells.join("\t"));
  }
  return rows.join("\n");
}

function styleMapForRange(styles: SheetStyleMap, range: SheetRange | null, patch: SheetCellStyle): SheetStyleMap {
  if (!range) return styles;
  const next: SheetStyleMap = { ...styles };
  for (let r = range.r1; r <= range.r2; r += 1) {
    for (let c = range.c1; c <= range.c2; c += 1) {
      const key = sheetStyleKey(r, c);
      const merged: SheetCellStyle = { ...(next[key] || {}), ...patch };
      Object.keys(merged).forEach((styleKey) => {
        const typedKey = styleKey as keyof SheetCellStyle;
        if (merged[typedKey] === undefined || merged[typedKey] === false || merged[typedKey] === "") {
          delete merged[typedKey];
        }
      });
      if (Object.keys(merged).length > 0) next[key] = merged;
      else delete next[key];
    }
  }
  return next;
}

function shiftStylesForRowInsert(styles: SheetStyleMap, rowIndex: number): SheetStyleMap {
  const next: SheetStyleMap = {};
  Object.entries(styles).forEach(([key, style]) => {
    const [row, col] = key.split(":").map(Number);
    next[sheetStyleKey(row >= rowIndex ? row + 1 : row, col)] = style;
  });
  return next;
}

function shiftStylesForColumnInsert(styles: SheetStyleMap, colIndex: number): SheetStyleMap {
  const next: SheetStyleMap = {};
  Object.entries(styles).forEach(([key, style]) => {
    const [row, col] = key.split(":").map(Number);
    next[sheetStyleKey(row, col >= colIndex ? col + 1 : col)] = style;
  });
  return next;
}

function shiftStylesForRowDelete(styles: SheetStyleMap, range: SheetRange): SheetStyleMap {
  const count = range.r2 - range.r1 + 1;
  const next: SheetStyleMap = {};
  Object.entries(styles).forEach(([key, style]) => {
    const [row, col] = key.split(":").map(Number);
    if (row >= range.r1 && row <= range.r2) return;
    next[sheetStyleKey(row > range.r2 ? row - count : row, col)] = style;
  });
  return next;
}

function shiftStylesForColumnDelete(styles: SheetStyleMap, range: SheetRange): SheetStyleMap {
  const count = range.c2 - range.c1 + 1;
  const next: SheetStyleMap = {};
  Object.entries(styles).forEach(([key, style]) => {
    const [row, col] = key.split(":").map(Number);
    if (col >= range.c1 && col <= range.c2) return;
    next[sheetStyleKey(row, col > range.c2 ? col - count : col)] = style;
  });
  return next;
}

function parseSheetNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const text = String(value ?? "").normalize("NFKC").trim();
  if (!text || text.startsWith("=")) return null;
  const normalized = text
    .replace(/[$¥€£₹₩₽₺₫₴₪₦₱฿₡₲₵₭₮₸₼₾₿,\s]/g, "")
    .replace(/%$/, "");
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeSheetFormulaExpression(formula: string): string {
  return formula
    .trim()
    .replace(/^=/, "")
    .normalize("NFKC")
    .replace(/[；;]/g, ",")
    .replace(/[×]/g, "*")
    .replace(/[÷]/g, "/");
}

interface SheetFormulaToken {
  type: "number" | "ref" | "ident" | "op" | "lparen" | "rparen" | "comma" | "colon";
  value: string;
}

function sheetColumnIndex(label: string): number {
  let value = 0;
  for (const ch of label.toUpperCase()) {
    value = value * 26 + (ch.charCodeAt(0) - 64);
  }
  return value - 1;
}

function parseSheetRef(ref: string): { row: number; col: number } | null {
  const match = ref.match(/^\$?([A-Z]+)\$?(\d+)$/i);
  if (!match) return null;
  const row = Number(match[2]) - 1;
  const col = sheetColumnIndex(match[1]);
  return row >= 0 && col >= 0 ? { row, col } : null;
}

function tokenizeSheetFormula(expression: string): SheetFormulaToken[] | null {
  const tokens: SheetFormulaToken[] = [];
  let i = 0;
  while (i < expression.length) {
    const ch = expression[i];
    if (/\s/.test(ch)) {
      i += 1;
      continue;
    }
    if ("+-*/^".includes(ch)) {
      tokens.push({ type: "op", value: ch });
      i += 1;
      continue;
    }
    if (ch === "(") { tokens.push({ type: "lparen", value: ch }); i += 1; continue; }
    if (ch === ")") { tokens.push({ type: "rparen", value: ch }); i += 1; continue; }
    if (ch === ",") { tokens.push({ type: "comma", value: ch }); i += 1; continue; }
    if (ch === ":") { tokens.push({ type: "colon", value: ch }); i += 1; continue; }

    if (/\d|\./.test(ch)) {
      let j = i;
      while (j < expression.length && /[\d.]/.test(expression[j])) j += 1;
      const isPercent = expression[j] === "%";
      if (isPercent) j += 1;
      const raw = expression.slice(i, isPercent ? j - 1 : j);
      const parsed = Number(raw);
      if (!Number.isFinite(parsed)) return null;
      tokens.push({ type: "number", value: String(isPercent ? parsed / 100 : parsed) });
      i = j;
      continue;
    }

    if (/[A-Z_$]/i.test(ch)) {
      let j = i;
      while (j < expression.length && /[A-Z_$\d]/i.test(expression[j])) j += 1;
      const raw = expression.slice(i, j);
      tokens.push({ type: parseSheetRef(raw) ? "ref" : "ident", value: raw.toUpperCase() });
      i = j;
      continue;
    }

    return null;
  }
  return tokens;
}

function getSheetNumericValue(data: any[][], row: number, col: number, seen = new Set<string>()): number | null {
  const key = `${row}:${col}`;
  if (seen.has(key)) return null;
  const value = data[row]?.[col];
  if (typeof value === "string" && value.trim().startsWith("=")) {
    const nextSeen = new Set(seen);
    nextSeen.add(key);
    return evaluateSheetFormula(data, value, nextSeen);
  }
  return parseSheetNumber(value);
}

function evaluateSheetFormula(data: any[][], formula: string, seen = new Set<string>()): number | null {
  const expression = normalizeSheetFormulaExpression(formula);
  const tokens = tokenizeSheetFormula(expression);
  if (!tokens || tokens.length === 0) return null;
  let pos = 0;

  const peek = () => tokens[pos];
  const consume = () => tokens[pos++];

  const parseRangeValues = (firstRef: string): number[] | null => {
    const start = parseSheetRef(firstRef);
    if (!start || peek()?.type !== "colon") return null;
    consume();
    const endToken = consume();
    const end = endToken?.type === "ref" ? parseSheetRef(endToken.value) : null;
    if (!end) return null;
    const values: number[] = [];
    const rowStart = Math.min(start.row, end.row);
    const rowEnd = Math.max(start.row, end.row);
    const colStart = Math.min(start.col, end.col);
    const colEnd = Math.max(start.col, end.col);
    for (let row = rowStart; row <= rowEnd; row += 1) {
      for (let col = colStart; col <= colEnd; col += 1) {
        const value = getSheetNumericValue(data, row, col, seen);
        if (value != null) values.push(value);
      }
    }
    return values;
  };

  const parseExpression = (): number | null => parseAddSub();

  const parseFunctionArgs = (): number[] | null => {
    const values: number[] = [];
    if (peek()?.type === "rparen") {
      consume();
      return values;
    }
    while (pos < tokens.length) {
      const token = peek();
      if (token?.type === "ref" && tokens[pos + 1]?.type === "colon") {
        consume();
        const rangeValues = parseRangeValues(token.value);
        if (!rangeValues) return null;
        values.push(...rangeValues);
      } else {
        const value = parseExpression();
        if (value == null) return null;
        values.push(value);
      }
      if (peek()?.type === "comma") {
        consume();
        continue;
      }
      if (peek()?.type === "rparen") {
        consume();
        return values;
      }
      return null;
    }
    return null;
  };

  function parsePrimary(): number | null {
    const token = consume();
    if (!token) return null;
    if (token.type === "number") return Number(token.value);
    if (token.type === "ref") {
      if (peek()?.type === "colon") return null;
      const ref = parseSheetRef(token.value);
      return ref ? getSheetNumericValue(data, ref.row, ref.col, seen) : null;
    }
    if (token.type === "ident" && peek()?.type === "lparen") {
      consume();
      const values = parseFunctionArgs();
      if (!values) return null;
      if (token.value === "SUM") return values.reduce((sum, value) => sum + value, 0);
      if (token.value === "AVERAGE") return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
      if (token.value === "MIN") return values.length ? Math.min(...values) : 0;
      if (token.value === "MAX") return values.length ? Math.max(...values) : 0;
      if (token.value === "COUNT") return values.length;
      return null;
    }
    if (token.type === "lparen") {
      const value = parseExpression();
      if (peek()?.type !== "rparen") return null;
      consume();
      return value;
    }
    return null;
  }

  function parseUnary(): number | null {
    if (peek()?.type === "op" && (peek().value === "-" || peek().value === "+")) {
      const op = consume().value;
      const value = parseUnary();
      return value == null ? null : op === "-" ? -value : value;
    }
    return parsePrimary();
  }

  function parsePower(): number | null {
    let left = parseUnary();
    while (left != null && peek()?.type === "op" && peek().value === "^") {
      consume();
      const right = parseUnary();
      left = right == null ? null : Math.pow(left, right);
    }
    return left;
  }

  function parseMulDiv(): number | null {
    let left = parsePower();
    while (left != null && peek()?.type === "op" && (peek().value === "*" || peek().value === "/")) {
      const op = consume().value;
      const right = parsePower();
      if (right == null) return null;
      left = op === "*" ? left * right : right === 0 ? null : left / right;
    }
    return left;
  }

  function parseAddSub(): number | null {
    let left = parseMulDiv();
    while (left != null && peek()?.type === "op" && (peek().value === "+" || peek().value === "-")) {
      const op = consume().value;
      const right = parseMulDiv();
      if (right == null) return null;
      left = op === "+" ? left + right : left - right;
    }
    return left;
  }

  const result = parseExpression();
  return result != null && pos === tokens.length && Number.isFinite(result) ? result : null;
}

function getSheetDisplayValue(data: any[][], row: number, col: number): string {
  const value = data[row]?.[col];
  if (typeof value === "string" && value.trim().startsWith("=")) {
    const evaluated = getSheetNumericValue(data, row, col);
    return evaluated == null ? "#ERROR" : Number.isInteger(evaluated) ? String(evaluated) : String(Number(evaluated.toFixed(6)));
  }
  return value != null ? String(value) : "";
}

function getDefaultSheetChartColumns(data: any[][], maxCols: number): { labelColumn: number; valueColumn: number } {
  const columns = Array.from({ length: maxCols }, (_, i) => i);
  const numericColumns = columns.filter((col) => data.some((row, rowIdx) => rowIdx > 0 && getSheetNumericValue(data, rowIdx, col) != null));
  const valueColumn = numericColumns.find((col) => col !== 0) ?? numericColumns[0] ?? Math.min(1, maxCols - 1);
  const labelColumn = columns.find((col) => col !== valueColumn && data.some((row, rowIdx) => {
    if (rowIdx === 0) return false;
    const text = String(row[col] ?? "").trim();
    return text.length > 0 && getSheetNumericValue(data, rowIdx, col) == null;
  })) ?? (valueColumn === 0 ? Math.min(1, maxCols - 1) : 0);
  return { labelColumn, valueColumn };
}

function buildChartPoints(data: any[][], chart: SheetChartConfig): { label: string; value: number }[] {
  const start = Math.max(0, Math.min(data.length - 1, chart.startRow));
  const end = Math.max(start, Math.min(data.length - 1, chart.endRow));
  const points: { label: string; value: number }[] = [];
  for (let r = start; r <= end; r += 1) {
    const value = getSheetNumericValue(data, r, chart.valueColumn);
    if (value == null) continue;
    points.push({
      label: String(data[r]?.[chart.labelColumn] ?? r + 1),
      value,
    });
  }
  return points;
}

function ChartPreview({ chart, data }: { chart: SheetChartConfig; data: any[][] }) {
  const points = buildChartPoints(data, chart);
  const width = 320;
  const height = 180;
  const pad = { top: 18, right: 18, bottom: 34, left: 42 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const maxValue = Math.max(1, ...points.map((p) => Math.abs(p.value)));

  if (points.length === 0) {
    return (
      <div style={{ height, display: "flex", alignItems: "center", justifyContent: "center", color: "#a8a29e", fontSize: 12 }}>
        {t("page.doc_editor.no_chart_data")}
      </div>
    );
  }

  if (chart.type === "pie") {
    const total = points.reduce((sum, p) => sum + Math.max(0, p.value), 0);
    let angle = -90;
    const cx = width / 2;
    const cy = 88;
    const radius = 56;
    const slices = total > 0 ? points.map((p, i) => {
      const slice = (Math.max(0, p.value) / total) * 360;
      const start = angle;
      const end = angle + slice;
      angle = end;
      const startRad = (Math.PI / 180) * start;
      const endRad = (Math.PI / 180) * end;
      const x1 = cx + radius * Math.cos(startRad);
      const y1 = cy + radius * Math.sin(startRad);
      const x2 = cx + radius * Math.cos(endRad);
      const y2 = cy + radius * Math.sin(endRad);
      const large = slice > 180 ? 1 : 0;
      return (
        <path
          key={`${p.label}-${i}`}
          d={`M ${cx} ${cy} L ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2} Z`}
          fill={SHEET_CHART_COLORS[i % SHEET_CHART_COLORS.length]}
          stroke="white"
          strokeWidth="2"
        />
      );
    }) : [];
    return (
      <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
        {slices}
        {points.slice(0, 4).map((p, i) => (
          <g key={p.label} transform={`translate(${16 + (i % 2) * 145}, ${150 + Math.floor(i / 2) * 16})`}>
            <rect width="8" height="8" rx="2" fill={SHEET_CHART_COLORS[i % SHEET_CHART_COLORS.length]} />
            <text x="12" y="8" fontSize="10" fill="#78716c">{p.label}</text>
          </g>
        ))}
      </svg>
    );
  }

  const xStep = plotW / Math.max(1, points.length);
  const yFor = (value: number) => pad.top + plotH - (Math.max(0, value) / maxValue) * plotH;
  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"} ${pad.left + xStep * i + xStep / 2} ${yFor(p.value)}`).join(" ");

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
      <line x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + plotH} stroke="#d6d3d1" />
      <line x1={pad.left} y1={pad.top + plotH} x2={pad.left + plotW} y2={pad.top + plotH} stroke="#d6d3d1" />
      <text x={8} y={pad.top + 4} fontSize="10" fill="#a8a29e">{maxValue.toLocaleString()}</text>
      {chart.type === "line" ? (
        <>
          <path d={linePath} fill="none" stroke="#4869ac" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
          {points.map((p, i) => (
            <circle key={`${p.label}-${i}`} cx={pad.left + xStep * i + xStep / 2} cy={yFor(p.value)} r="3.5" fill="#4869ac" />
          ))}
        </>
      ) : (
        points.map((p, i) => {
          const barW = Math.max(8, xStep * 0.58);
          const x = pad.left + xStep * i + (xStep - barW) / 2;
          const y = yFor(p.value);
          return (
            <rect
              key={`${p.label}-${i}`}
              x={x}
              y={y}
              width={barW}
              height={pad.top + plotH - y}
              rx="3"
              fill={SHEET_CHART_COLORS[i % SHEET_CHART_COLORS.length]}
            />
          );
        })
      )}
      {points.map((p, i) => i % Math.ceil(points.length / 5) === 0 && (
        <text key={`${p.label}-label`} x={pad.left + xStep * i + xStep / 2} y={height - 12} textAnchor="middle" fontSize="10" fill="#78716c">
          {p.label.slice(0, 8)}
        </text>
      ))}
    </svg>
  );
}

function SpreadsheetEditor({
  initialData,
  initialCharts,
  initialStyles,
  persistCharts,
  onChange,
}: {
  initialData: any[][] | null;
  initialCharts: SheetChartConfig[];
  initialStyles: SheetStyleMap;
  persistCharts: boolean;
  onChange: (data: any[][], charts: SheetChartConfig[], styles: SheetStyleMap) => void;
}) {
  const [data, setData] = useState<any[][]>(() => normalizeSheetData(initialData));
  const [charts, setCharts] = useState<SheetChartConfig[]>(() => normalizeSheetCharts(initialCharts, normalizeSheetData(initialData)));
  const [styles, setStyles] = useState<SheetStyleMap>(() => normalizeSheetStyles(initialStyles));
  const [selected, setSelected] = useState<SheetCellCoord>({ r: 0, c: 0 });
  const [selectionAnchor, setSelectionAnchor] = useState<SheetCellCoord>({ r: 0, c: 0 });
  const [isSelecting, setIsSelecting] = useState(false);
  const [showChartPanel, setShowChartPanel] = useState(true);
  const activeInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setData(normalizeSheetData(initialData));
  }, [initialData]);

  useEffect(() => {
    setCharts(normalizeSheetCharts(initialCharts, normalizeSheetData(initialData)));
  }, [initialCharts, initialData]);

  useEffect(() => {
    setStyles(normalizeSheetStyles(initialStyles));
  }, [initialStyles]);

  useEffect(() => {
    const stopSelecting = () => setIsSelecting(false);
    window.addEventListener("mouseup", stopSelecting);
    return () => window.removeEventListener("mouseup", stopSelecting);
  }, []);

  const focusActiveInput = useCallback(() => {
    window.requestAnimationFrame(() => activeInputRef.current?.focus());
  }, []);

  const commitData = useCallback((nextData: any[][]) => {
    const normalized = normalizeSheetData(nextData);
    setData(normalized);
    onChange(normalized, charts, styles);
  }, [charts, onChange, styles]);

  const updateCell = useCallback((r: number, c: number, value: string) => {
    const next = ensureSheetDimensions(data, r + 1, c + 1);
    next[r][c] = value;
    commitData(next);
  }, [commitData, data]);

  const actualMaxCols = Math.max(1, ...data.map((r) => r.length));
  const visibleRows = Math.max(MIN_VISIBLE_SHEET_ROWS, data.length, selected.r + 1);
  const maxCols = Math.max(MIN_VISIBLE_SHEET_COLS, actualMaxCols, selected.c + 1);
  const selectionRange = useMemo(() => normalizeSheetSelection(selected, selectionAnchor), [selected, selectionAnchor]);
  const selectedRangeLabel = spreadsheetRangeRef(selectionRange);
  const activeCellValue = selected ? String(data[selected.r]?.[selected.c] ?? "") : "";
  const activeStyle = styles[sheetStyleKey(selected.r, selected.c)] || {};
  const columnOptions = Array.from({ length: maxCols }, (_, i) => i);
  const numericColumnOptions = columnOptions.filter((col) => data.some((row, rowIdx) => rowIdx > 0 && getSheetNumericValue(data, rowIdx, col) != null));
  const valueColumnOptions = numericColumnOptions.length > 0 ? numericColumnOptions : columnOptions;
  const fontSelectOptions = [
    { value: "", label: "Default font" },
    { value: "Inter, ui-sans-serif, system-ui, sans-serif", label: "Inter" },
    { value: "Arial, Helvetica, sans-serif", label: "Arial" },
    { value: "Georgia, serif", label: "Georgia" },
    { value: "'Times New Roman', Times, serif", label: "Times" },
    { value: "'SF Mono', Menlo, Consolas, monospace", label: "Mono" },
  ];
  const chartTypeOptions: { value: SheetChartType; label: string }[] = [
    { value: "bar", label: t("page.doc_editor.bar") },
    { value: "line", label: t("page.doc_editor.line_chart") },
    { value: "pie", label: t("page.doc_editor.pie") },
  ];
  const chartDropdownItems = chartTypeOptions.map((option) => ({
    key: option.value,
    label: option.label,
    icon: <IconTrendingUp size={14} />,
  }));
  const columnSelectOptions = columnOptions.map((col) => ({
    value: String(col),
    label: spreadsheetCellLabel(data, col),
  }));
  const valueColumnSelectOptions = valueColumnOptions.map((col) => ({
    value: String(col),
    label: spreadsheetCellLabel(data, col),
  }));

  const focusCell = useCallback((targetR: number, targetC: number, extend = false) => {
    const safe = { r: Math.max(0, targetR), c: Math.max(0, targetC) };
    setSelected(safe);
    setSelectionAnchor((current) => extend ? current : safe);
    focusActiveInput();
  }, [focusActiveInput]);

  const addRow = useCallback(() => {
    const range = selectionRange;
    const insertAt = range ? range.r2 + 1 : data.length;
    const next = ensureSheetDimensions(data, Math.max(data.length, insertAt), maxCols);
    next.splice(insertAt, 0, Array(maxCols).fill(""));
    const nextStyles = shiftStylesForRowInsert(styles, insertAt);
    setStyles(nextStyles);
    onChange(next, charts, nextStyles);
    setData(next);
    focusCell(insertAt, selected.c);
  }, [charts, data, focusCell, maxCols, onChange, selected.c, selectionRange, styles]);

  const addCol = useCallback(() => {
    const range = selectionRange;
    const insertAt = range ? range.c2 + 1 : maxCols;
    const next = ensureSheetDimensions(data, data.length, Math.max(maxCols, insertAt));
    next.forEach((row) => row.splice(insertAt, 0, ""));
    const nextStyles = shiftStylesForColumnInsert(styles, insertAt);
    setStyles(nextStyles);
    onChange(next, charts, nextStyles);
    setData(next);
    focusCell(selected.r, insertAt);
  }, [charts, data, focusCell, maxCols, onChange, selected.r, selectionRange, styles]);

  const deleteRows = useCallback(() => {
    const range = selectionRange;
    if (!range) return;
    const next = data.filter((_row, index) => index < range.r1 || index > range.r2);
    const normalized = next.length ? next : [[""]];
    const nextStyles = shiftStylesForRowDelete(styles, range);
    setData(normalized);
    setStyles(nextStyles);
    onChange(normalized, charts, nextStyles);
    focusCell(Math.min(range.r1, normalized.length - 1), selected.c);
  }, [charts, data, focusCell, onChange, selected.c, selectionRange, styles]);

  const deleteCols = useCallback(() => {
    const range = selectionRange;
    if (!range) return;
    const next = ensureSheetDimensions(data, data.length, maxCols)
      .map((row) => row.filter((_cell, index) => index < range.c1 || index > range.c2));
    const normalized = next.map((row) => row.length ? row : [""]);
    const nextStyles = shiftStylesForColumnDelete(styles, range);
    setData(normalized);
    setStyles(nextStyles);
    onChange(normalized, charts, nextStyles);
    focusCell(selected.r, Math.min(range.c1, Math.max(0, maxCols - (range.c2 - range.c1 + 1) - 1)));
  }, [charts, data, focusCell, maxCols, onChange, selected.r, selectionRange, styles]);

  const clearSelection = useCallback(() => {
    const range = selectionRange;
    if (!range) return;
    const next = ensureSheetDimensions(data, range.r2 + 1, range.c2 + 1);
    for (let r = range.r1; r <= range.r2; r += 1) {
      for (let c = range.c1; c <= range.c2; c += 1) {
        next[r][c] = "";
      }
    }
    commitData(next);
    focusCell(range.r1, range.c1);
  }, [commitData, data, focusCell, selectionRange]);

  const commitStyles = useCallback((nextStyles: SheetStyleMap) => {
    const normalized = normalizeSheetStyles(nextStyles);
    setStyles(normalized);
    onChange(data, charts, normalized);
  }, [charts, data, onChange]);

  const applyStyle = useCallback((patch: SheetCellStyle) => {
    commitStyles(styleMapForRange(styles, selectionRange, patch));
    focusActiveInput();
  }, [commitStyles, focusActiveInput, selectionRange, styles]);

  const makeHeader = useCallback(() => {
    applyStyle({
      bold: true,
      fontSize: Math.max(13, activeStyle.fontSize || 13),
      color: "#1c1917",
      fill: "#e8eff4",
      align: "center",
    });
  }, [activeStyle.fontSize, applyStyle]);

  const copySelection = useCallback(async () => {
    const range = selectionRange;
    if (!range) return;
    const text = sheetRangeToTsv(data, range);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard can be unavailable in some embedded browsers; keyboard paste still works.
    }
  }, [data, selectionRange]);

  const pasteCells = useCallback((startR: number, startC: number, text: string) => {
    const matrix = parseSpreadsheetPasteText(text);
    if (matrix.length === 0 || matrix.every((row) => row.length === 0)) return;
    const range = selectionRange;
    if (range && matrix.length === 1 && matrix[0].length === 1 && sheetRangeSize(range) > 1) {
      const next = ensureSheetDimensions(data, range.r2 + 1, range.c2 + 1);
      for (let r = range.r1; r <= range.r2; r += 1) {
        for (let c = range.c1; c <= range.c2; c += 1) {
          next[r][c] = matrix[0][0];
        }
      }
      commitData(next);
      focusCell(range.r2, range.c2, true);
      return;
    }
    const maxPasteCols = Math.max(1, ...matrix.map((row) => row.length));
    const targetRows = startR + matrix.length;
    const targetCols = startC + maxPasteCols;
    const next = ensureSheetDimensions(data, targetRows, targetCols);
    matrix.forEach((row, ri) => {
      row.forEach((cell, ci) => {
        next[startR + ri][startC + ci] = cell;
      });
    });
    commitData(next);
    const end = { r: startR + matrix.length - 1, c: startC + maxPasteCols - 1 };
    setSelectionAnchor({ r: startR, c: startC });
    setSelected(end);
  }, [commitData, data, focusCell, selectionRange]);

  const commitCharts = useCallback((nextCharts: SheetChartConfig[]) => {
    const normalized = normalizeSheetCharts(nextCharts, data);
    setCharts(normalized);
    onChange(data, normalized, styles);
  }, [data, onChange, styles]);

  const addChart = useCallback((type: SheetChartType) => {
    const range = selectionRange;
    const inferred = getDefaultSheetChartColumns(data, maxCols);
    const rangeIsUseful = Boolean(range && sheetRangeSize(range) > 1);
    const rangeColumns = rangeIsUseful && range
      ? Array.from({ length: range.c2 - range.c1 + 1 }, (_v, index) => range.c1 + index)
      : [];
    const rangeNumericColumns = rangeIsUseful && range
      ? rangeColumns.filter((col) => data.some((_row, rowIdx) => rowIdx >= range.r1 && rowIdx <= range.r2 && getSheetNumericValue(data, rowIdx, col) != null))
      : [];
    const valueColumn = rangeIsUseful
      ? rangeNumericColumns.find((col) => range && col !== range.c1) ?? rangeNumericColumns[0] ?? inferred.valueColumn
      : inferred.valueColumn;
    const labelColumn = rangeIsUseful
      ? rangeColumns.find((col) => col !== valueColumn && range && data.some((row, rowIdx) => {
        if (rowIdx < range.r1 || rowIdx > range.r2) return false;
        const text = String(row[col] ?? "").trim();
        return text.length > 0 && getSheetNumericValue(data, rowIdx, col) == null;
      })) ?? inferred.labelColumn
      : inferred.labelColumn;
    const headerLooksText = rangeIsUseful && range && range.r2 > range.r1 && getSheetNumericValue(data, range.r1, valueColumn) == null;
    const startRow = rangeIsUseful && range ? Math.min(range.r2, range.r1 + (headerLooksText ? 1 : 0)) : data.length > 1 ? 1 : 0;
    const endRow = rangeIsUseful && range ? range.r2 : Math.max(startRow, data.length - 1);
    const header = data[0]?.[valueColumn];
    const title = `${spreadsheetCellLabel(data, valueColumn).split(" · ").slice(1).join(" · ") || colLetter(valueColumn)} ${t("page.doc_editor.chart")}`;
    commitCharts([...charts, {
      id: genId(),
      type,
      title: typeof header === "string" && header.trim() ? `${header.trim()} ${t("page.doc_editor.chart")}` : title,
      labelColumn,
      valueColumn,
      startRow,
      endRow,
    }]);
    setShowChartPanel(true);
  }, [charts, commitCharts, data, maxCols, selectionRange]);

  const updateChart = useCallback((chartId: string, update: Partial<SheetChartConfig>) => {
    commitCharts(charts.map((chart) => chart.id === chartId ? { ...chart, ...update } : chart));
  }, [charts, commitCharts]);

  const deleteChart = useCallback((chartId: string) => {
    commitCharts(charts.filter((chart) => chart.id !== chartId));
  }, [charts, commitCharts]);

  const selectCell = useCallback((r: number, c: number, extend = false) => {
    const cell = { r, c };
    setSelected(cell);
    setSelectionAnchor((current) => extend ? current : cell);
    focusActiveInput();
  }, [focusActiveInput]);

  const selectAllVisible = useCallback(() => {
    setSelectionAnchor({ r: 0, c: 0 });
    setSelected({ r: visibleRows - 1, c: maxCols - 1 });
  }, [maxCols, visibleRows]);

  const handleCellKeyDown = useCallback((event: React.KeyboardEvent<HTMLInputElement>, ri: number, ci: number, rawCellValue: string) => {
    const meta = event.metaKey || event.ctrlKey;
    if (meta && event.key.toLowerCase() === "a") {
      event.preventDefault();
      selectAllVisible();
      return;
    }
    if (meta && event.key.toLowerCase() === "c") {
      event.preventDefault();
      void copySelection();
      return;
    }
    if (meta && event.key.toLowerCase() === "x") {
      event.preventDefault();
      void copySelection();
      clearSelection();
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && sheetRangeSize(selectionRange) > 1) {
      event.preventDefault();
      clearSelection();
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      const nextCol = event.shiftKey ? ci - 1 : ci + 1;
      if (nextCol < 0) focusCell(Math.max(0, ri - 1), maxCols - 1);
      else if (nextCol >= maxCols) focusCell(ri + 1, 0);
      else focusCell(ri, nextCol);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      focusCell(event.shiftKey ? ri - 1 : ri + 1, ci);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setSelectionAnchor({ r: ri, c: ci });
      setSelected({ r: ri, c: ci });
      return;
    }
    if (!event.metaKey && !event.ctrlKey && !event.altKey && event.key.startsWith("Arrow")) {
      const input = event.currentTarget;
      const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
      const atEnd = input.selectionStart === rawCellValue.length && input.selectionEnd === rawCellValue.length;
      const shouldMove = event.shiftKey || event.key === "ArrowUp" || event.key === "ArrowDown" || (event.key === "ArrowLeft" && atStart) || (event.key === "ArrowRight" && atEnd);
      if (!shouldMove) return;
      event.preventDefault();
      const next =
        event.key === "ArrowUp" ? { r: ri - 1, c: ci }
          : event.key === "ArrowDown" ? { r: ri + 1, c: ci }
            : event.key === "ArrowLeft" ? { r: ri, c: ci - 1 }
              : { r: ri, c: ci + 1 };
      focusCell(next.r, next.c, event.shiftKey);
    }
  }, [clearSelection, copySelection, focusCell, maxCols, selectAllVisible, selectionRange]);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          borderBottom: "1px solid var(--border-subtle, #dbe3ef)",
          background: "var(--surface-muted, #fafaf9)",
          flexShrink: 0,
          flexWrap: "wrap",
        }}
      >
        <SheetToolbarGroup style={{ flex: "1 1 420px" }}>
          <div style={{ minWidth: 72, height: 30, border: "1px solid var(--editor-control-border, #dbe3ef)", borderRadius: 8, background: "var(--editor-control-bg, #ffffff)", display: "flex", alignItems: "center", padding: "0 9px", fontSize: 12, fontWeight: 850, color: "var(--text-strong, #1c1917)" }}>
            {selectedRangeLabel || "A1"}
          </div>
          <div style={{ height: 30, width: 34, border: "1px solid var(--editor-control-border, #dbe3ef)", borderRadius: 8, background: "var(--editor-control-bg, #ffffff)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faint, #78716c)", fontWeight: 900, fontSize: 12 }}>
            fx
          </div>
          <input
            value={activeCellValue}
            onChange={(event) => updateCell(selected.r, selected.c, event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                focusCell(selected.r + 1, selected.c);
              }
            }}
            onPaste={(event) => {
              const pasted = event.clipboardData.getData("text/plain");
              if (!pasted || (!pasted.includes("\t") && !/[\r\n]/.test(pasted))) return;
              event.preventDefault();
              pasteCells(selected.r, selected.c, pasted);
            }}
            style={{ flex: 1, minWidth: 180, height: 30, border: "1px solid var(--editor-control-border, #dbe3ef)", borderRadius: 8, padding: "0 10px", fontSize: 13, color: "var(--text-strong, #1c1917)", background: "var(--editor-control-bg, #ffffff)" }}
          />
        </SheetToolbarGroup>
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginLeft: "auto" }}>
          <SheetToolbarGroup>
          <Select
            value={activeStyle.fontFamily || ""}
            onChange={(value) => applyStyle({ fontFamily: value || undefined })}
            options={fontSelectOptions}
            style={{ width: 136 }}
            buttonStyle={{ ...sheetToolbarSelectStyle, width: "100%", boxShadow: "none" }}
          />
          <input
            type="number"
            min={8}
            max={72}
            value={activeStyle.fontSize || 13}
            onChange={(event) => applyStyle({ fontSize: Number(event.target.value) || 13 })}
            style={{ ...sheetToolbarSelectStyle, width: 58, padding: "0 7px" }}
            title="Font size"
          />
          <SheetToolbarButton
            onClick={() => applyStyle({ bold: !activeStyle.bold })}
            active={Boolean(activeStyle.bold)}
            title="Bold"
            style={{ fontSize: 13, fontWeight: 950, padding: "0 10px" }}
          >
            B
          </SheetToolbarButton>
          <SheetToolbarButton
            onClick={() => applyStyle({ italic: !activeStyle.italic })}
            active={Boolean(activeStyle.italic)}
            title="Italic"
            style={{ fontSize: 13, fontStyle: "italic", padding: "0 10px" }}
          >
            I
          </SheetToolbarButton>
          <SheetColorControl
            icon={<IconPalette size={14} />}
            label="A"
            title="Text color"
            value={activeStyle.color || "#1c1917"}
            onChange={(value) => applyStyle({ color: value })}
          />
          <SheetColorControl
            icon={<IconHighlighter size={14} />}
            label="Fill"
            title="Fill color"
            value={activeStyle.fill || "#ffffff"}
            onChange={(value) => applyStyle({ fill: value })}
          />
          </SheetToolbarGroup>
          <SheetToolbarGroup>
          {(["left", "center", "right"] as SheetTextAlign[]).map((align) => (
            <SheetToolbarButton
              key={align}
              onClick={() => applyStyle({ align })}
              active={activeStyle.align === align}
              title={`Align ${align}`}
              style={{ padding: "0 9px" }}
            >
              {align === "left" ? "L" : align === "center" ? "C" : "R"}
            </SheetToolbarButton>
          ))}
          <SheetToolbarButton onClick={makeHeader} icon={<IconText size={14} />} title="Header">
            Header
          </SheetToolbarButton>
          <Dropdown
            align="right"
            trigger={(
              <SheetToolbarButton icon={<IconTrendingUp size={14} />} title={t("page.doc_editor.chart")}>
                {t("page.doc_editor.chart")}
              </SheetToolbarButton>
            )}
            items={chartDropdownItems}
            onSelect={(key) => addChart(key as SheetChartType)}
          />
          </SheetToolbarGroup>
          <SheetToolbarGroup>
          <SheetToolbarButton onClick={() => void copySelection()} icon={<IconCopy size={14} />} title={t("action.copy")}>
            {t("action.copy")}
          </SheetToolbarButton>
          <SheetToolbarButton onClick={clearSelection} icon={<IconEraser size={14} />} title="Clear">
            Clear
          </SheetToolbarButton>
          <SheetToolbarButton onClick={addRow} icon={<IconPlus size={14} />} title={t("page.doc_editor.plus_row")}>
            Row
          </SheetToolbarButton>
          <SheetToolbarButton onClick={addCol} icon={<IconPlus size={14} />} title={t("page.doc_editor.plus_column")}>
            Col
          </SheetToolbarButton>
          <SheetToolbarButton onClick={deleteRows} danger icon={<IconTrash size={14} />} title="Delete row">
            Row
          </SheetToolbarButton>
          <SheetToolbarButton onClick={deleteCols} danger icon={<IconTrash size={14} />} title="Delete col">
            Col
          </SheetToolbarButton>
          <SheetToolbarButton
            onClick={() => setShowChartPanel((value) => !value)}
            active={showChartPanel}
            icon={<IconEye size={14} />}
            title={t("page.doc_editor.charts")}
          >
            Charts {charts.length ? `(${charts.length})` : ""}
          </SheetToolbarButton>
          </SheetToolbarGroup>
        </div>
      </div>
      <div style={{ flex: 1, display: "flex", minHeight: 0, overflow: "hidden" }}>
        <div style={{ flex: 1, overflow: "auto", minWidth: 0 }}>
          <table style={{ borderCollapse: "separate", borderSpacing: 0, fontSize: 13, minWidth: "100%", userSelect: isSelecting ? "none" : undefined }}>
            <thead>
              <tr>
                <th
                  onMouseDown={(event) => { event.preventDefault(); selectAllVisible(); }}
                  style={{ ...shTh, width: 48, minWidth: 48, background: "var(--surface-sunken, #eef2f7)", left: 0, zIndex: 6, cursor: "cell" }}
                >
                  #
                </th>
                {Array.from({ length: maxCols }, (_, i) => (
                  <th
                    key={i}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      setSelectionAnchor({ r: 0, c: i });
                      setSelected({ r: visibleRows - 1, c: i });
                      setIsSelecting(true);
                    }}
                    style={{ ...shTh, minWidth: 112, cursor: "cell" }}
                  >
                    {colLetter(i)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: visibleRows }, (_, ri) => {
                const row = data[ri] || [];
                return (
                <tr key={ri}>
                  <td
                    onMouseDown={(event) => {
                      event.preventDefault();
                      setSelectionAnchor({ r: ri, c: 0 });
                      setSelected({ r: ri, c: maxCols - 1 });
                      setIsSelecting(true);
                    }}
                    style={{ ...shTd, background: "#fafaf9", color: "#78716c", fontWeight: 700, textAlign: "center", width: 48, minWidth: 48, position: "sticky", left: 0, zIndex: 2, cursor: "cell" }}
                  >
                    {ri + 1}
                  </td>
                  {Array.from({ length: maxCols }, (_, ci) => {
                    const isActive = selected.r === ri && selected.c === ci;
                    const isInRange = sheetCellInRange(ri, ci, selectionRange);
                    const rawCellValue = row[ci] != null ? String(row[ci]) : "";
                    const isFormulaCell = rawCellValue.trim().startsWith("=");
                    const displayValue = getSheetDisplayValue(data, ri, ci);
                    const cellStyle = styles[sheetStyleKey(ri, ci)] || {};
                    const cellBackground = isInRange
                      ? `linear-gradient(rgba(79,125,117,0.10), rgba(79,125,117,0.10)), ${cellStyle.fill || "#ffffff"}`
                      : cellStyle.fill || "#ffffff";
                    const sharedTextStyle: React.CSSProperties = {
                      color: displayValue === "#ERROR" ? "#c14a44" : cellStyle.color || "#1c1917",
                      fontFamily: cellStyle.fontFamily || "inherit",
                      fontSize: cellStyle.fontSize || 13,
                      fontWeight: cellStyle.bold ? 700 : 400,
                      fontStyle: cellStyle.italic ? "italic" : "normal",
                      textAlign: cellStyle.align || "left",
                    };
                    return (
                      <td
                        key={ci}
                        onMouseDown={() => {
                          selectCell(ri, ci);
                          setIsSelecting(true);
                        }}
                        onMouseEnter={() => {
                          if (isSelecting) setSelected({ r: ri, c: ci });
                        }}
                        style={{
                          ...shTd,
                          padding: 0,
                          background: cellBackground,
                          outline: isActive ? "2px solid #4f7d75" : isInRange ? "1px solid rgba(79,125,117,0.35)" : undefined,
                          outlineOffset: -2,
                          minWidth: 112,
                          height: 32,
                        }}
                      >
                        {isActive ? (
                          <input
                            ref={activeInputRef}
                            autoFocus
                            value={rawCellValue}
                            onChange={(e) => updateCell(ri, ci, e.target.value)}
                            onKeyDown={(e) => handleCellKeyDown(e, ri, ci, rawCellValue)}
                            onPaste={(e) => {
                              const pasted = e.clipboardData.getData("text/plain");
                              if (!pasted || (!pasted.includes("\t") && !/[\r\n]/.test(pasted))) return;
                              e.preventDefault();
                              pasteCells(ri, ci, pasted);
                            }}
                            style={{
                              ...sharedTextStyle,
                              width: "100%",
                              height: 32,
                              padding: "5px 9px",
                              border: "none",
                              outline: "none",
                              background: "transparent",
                            }}
                          />
                        ) : (
                          <div
                            title={isFormulaCell ? rawCellValue : undefined}
                            style={{ ...sharedTextStyle, padding: "6px 10px", minHeight: 32, cursor: "cell", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                          >
                            {displayValue}
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );})}
            </tbody>
          </table>
        </div>

        {showChartPanel && <aside style={{ width: "min(100%, 360px)", maxWidth: "100%", borderLeft: "1px solid var(--border-subtle, rgba(28,25,23,0.06))", background: "var(--surface-muted, #fafaf9)", overflow: "auto", flexShrink: 0 }}>
          <div style={{ padding: 14, borderBottom: "1px solid var(--border-subtle, rgba(28,25,23,0.06))", display: "flex", alignItems: "center", gap: 8 }}>
            <strong style={{ fontSize: 13, color: "var(--text-strong, #1c1917)" }}>{t("page.doc_editor.charts")}</strong>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-faint, #a8a29e)" }}>
              {persistCharts ? t("page.doc_editor.saved_to_xlsx") : t("page.doc_editor.preview_only")}
            </span>
          </div>
          <div style={{ padding: 12, display: "flex", gap: 8, flexWrap: "wrap", borderBottom: "1px solid var(--border-subtle, rgba(28,25,23,0.06))" }}>
            <Dropdown
              align="left"
              style={{ width: "100%" }}
              trigger={(
                <button
                  type="button"
                  className="btn-manor-ghost"
                  style={{ width: "100%", justifyContent: "space-between", fontSize: 12, padding: "7px 10px", display: "flex", alignItems: "center" }}
                >
                  <span>{t("page.doc_editor.chart")}</span>
                  <span>▾</span>
                </button>
              )}
              items={chartDropdownItems}
              onSelect={(key) => addChart(key as SheetChartType)}
            />
          </div>
          <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 12 }}>
            {charts.length === 0 ? (
              <div style={{ padding: 20, textAlign: "center", color: "var(--text-faint, #a8a29e)", fontSize: 12 }}>
                {t("page.doc_editor.no_charts_yet")}
              </div>
            ) : charts.map((chart) => (
              <div key={chart.id} style={{ border: "1px solid var(--border-subtle, rgba(28,25,23,0.06))", borderRadius: 8, background: "var(--surface-panel, white)", overflow: "hidden" }}>
                <div style={{ padding: "10px 12px", borderBottom: "1px solid #f5f5f4", display: "flex", gap: 8, alignItems: "center" }}>
                  <input
                    value={chart.title}
                    onChange={(e) => updateChart(chart.id, { title: e.target.value })}
                    style={{ flex: 1, minWidth: 0, border: "none", outline: "none", fontSize: 13, fontWeight: 700, color: "#1c1917" }}
                  />
                  <button onClick={() => deleteChart(chart.id)} className="btn-manor-ghost" style={{ fontSize: 11, padding: "3px 8px", color: "#c14a44" }}>
                    {t("action.delete")}
                  </button>
                </div>
                <div style={{ padding: 10 }}>
                  <ChartPreview chart={chart} data={data} />
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 8 }}>
                    <div style={sheetFieldStyle}>
                      <span>{t("page.doc_editor.type")}</span>
                      <Select
                        value={chart.type}
                        onChange={(value) => updateChart(chart.id, { type: value as SheetChartType })}
                        options={chartTypeOptions}
                      />
                    </div>
                    <div style={sheetFieldStyle}>
                      <span>{t("page.doc_editor.labels")}</span>
                      <Select
                        value={String(chart.labelColumn)}
                        onChange={(value) => updateChart(chart.id, { labelColumn: Number(value) })}
                        options={columnSelectOptions}
                      />
                    </div>
                    <div style={sheetFieldStyle}>
                      <span>{t("page.doc_editor.values")}</span>
                      <Select
                        value={String(chart.valueColumn)}
                        onChange={(value) => updateChart(chart.id, { valueColumn: Number(value) })}
                        options={valueColumnSelectOptions}
                      />
                    </div>
                    <label style={sheetFieldStyle}>
                      {t("page.doc_editor.rows_2")}
                      <div style={{ display: "flex", gap: 4 }}>
                        <input
                          type="number"
                          min={1}
                          max={data.length}
                          value={chart.startRow + 1}
                          onChange={(e) => updateChart(chart.id, { startRow: Number(e.target.value) - 1 })}
                          style={sheetControlStyle}
                        />
                        <input
                          type="number"
                          min={1}
                          max={data.length}
                          value={chart.endRow + 1}
                          onChange={(e) => updateChart(chart.id, { endRow: Number(e.target.value) - 1 })}
                          style={sheetControlStyle}
                        />
                      </div>
                    </label>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </aside>}
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "center", padding: "6px 12px", borderTop: "1px solid var(--border-subtle, #dbe3ef)", background: "var(--surface-muted, #fafaf9)", flexShrink: 0, fontSize: 12, color: "var(--text-faint, #78716c)" }}>
        <span>{data.length} {t("page.doc_editor.rows")} · {actualMaxCols} {t("page.doc_editor.cols")}</span>
        <span>{selectedRangeLabel}</span>
        <span style={{ marginLeft: "auto" }}>{persistCharts ? t("page.doc_editor.saved_to_xlsx") : t("page.doc_editor.preview_only")}</span>
      </div>
    </div>
  );
}

const shTh: React.CSSProperties = {
  padding: "6px 10px", fontSize: 11, fontWeight: 700, color: "var(--text-faint, #78716c)",
  background: "var(--surface-muted, #fafaf9)", borderBottom: "2px solid var(--border-subtle, rgba(28,25,23,0.06))", borderRight: "1px solid var(--border-subtle, rgba(28,25,23,0.06))",
  textAlign: "center", position: "sticky", top: 0, whiteSpace: "nowrap",
};
const shTd: React.CSSProperties = {
  borderBottom: "1px solid var(--border-subtle, #f5f5f4)", borderRight: "1px solid var(--border-subtle, #f5f5f4)",
};
const sheetFieldStyle: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 4, fontSize: 11, fontWeight: 700, color: "var(--text-faint, #78716c)",
};
const sheetControlStyle: React.CSSProperties = {
  width: "100%", minWidth: 0, border: "1px solid var(--border-subtle, rgba(28,25,23,0.06))", borderRadius: 6, padding: "5px 7px",
  fontSize: 12, color: "var(--text-strong, #1c1917)", background: "var(--surface-panel, white)",
};

function colLetter(i: number): string {
  let s = "";
  let n = i;
  while (n >= 0) {
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26) - 1;
  }
  return s;
}

// ---------------------------------------------------------------------------
// PPTX types & parsing (mirrors FileViewer logic)
// ---------------------------------------------------------------------------

interface PptxTextRun {
  text: string; bold?: boolean; italic?: boolean; underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number; color?: string; align?: string;
  fontFamily?: string; bullet?: string; indent?: number;
  lineSpacing?: number; // multiplier (1.0 = single)
  spaceBefore?: number; // pt
  spaceAfter?: number; // pt
  baseline?: number; // superscript (+) / subscript (-)
  spacing?: number; // letter spacing in pt
  runs?: { text: string; bold?: boolean; italic?: boolean; underline?: boolean; strikethrough?: boolean; fontSize?: number; color?: string; fontFamily?: string; baseline?: number; spacing?: number }[];
}

interface PptxTableCell {
  text: string; bold?: boolean; color?: string; fill?: string;
  gridSpan?: number; vMerge?: boolean;
}

interface PptxShape {
  id: string;
  type?: "shape" | "table" | "image";
  x: number; y: number; w: number; h: number;
  fill?: string;
  gradFill?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  borderRadius?: number;
  opacity?: number;
  rotation?: number;
  stroke?: string;
  strokeWidth?: number;
  presetGeom?: string; // oval, triangle, diamond, etc.
  flipH?: boolean;
  flipV?: boolean;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  imgCrop?: { l: number; t: number; r: number; b: number };
  vAlign?: "top" | "middle" | "bottom";
  padding?: { l: number; t: number; r: number; b: number };
  texts: PptxTextRun[];
  imgUrl?: string;
  imageFit?: "cover" | "contain";
  // Table data
  tableRows?: PptxTableCell[][];
  tableCols?: number;
  tableColWidths?: number[];
}

interface PptxSlide {
  id: string;
  bg?: string;
  bgGrad?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  bgImgUrl?: string;
  aspectRatio?: string;
  notes?: string;
  shapes: PptxShape[];
}

let EDITOR_SLIDE_W = 12192000;
let EDITOR_SLIDE_H = 6858000;
const emu2pctX = (v: number) => (v / EDITOR_SLIDE_W) * 100;
const emu2pctY = (v: number) => (v / EDITOR_SLIDE_H) * 100;

function pptxXmlAttr(el: string, attr: string): string | null {
  const m = el.match(new RegExp(`${attr}="([^"]*)"`));
  return m ? m[1] : null;
}

function pptxXmlInner(xml: string, tag: string): string | null {
  const m = xml.match(new RegExp(`<${tag}[\\s>][\\s\\S]*?</${tag}>`, "i"));
  return m ? m[0] : null;
}

// Default fallback scheme colors
const DEFAULT_SCHEME: Record<string, string> = {
  dk1: "#000000", dk2: "#292524", lt1: "#ffffff", lt2: "#fafaf9",
  accent1: "#4472c4", accent2: "#ed7d31", accent3: "#a5a5a5",
  accent4: "#ffc000", accent5: "#5b9bd5", accent6: "#70ad47",
  tx1: "#000000", tx2: "#57534e", bg1: "#ffffff", bg2: "#f5f5f4",
  hlink: "#0563c1", folHlink: "#954f72",
};

/** Parse theme1.xml and extract actual scheme colors */
function parseThemeColors(themeXml: string): Record<string, string> {
  const colors: Record<string, string> = { ...DEFAULT_SCHEME };
  // Extract from <a:clrScheme> — each child tag name is the color name
  const clrScheme = pptxXmlInner(themeXml, "a:clrScheme");
  if (!clrScheme) return colors;
  const tags = ["dk1", "dk2", "lt1", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"];
  for (const tag of tags) {
    const inner = pptxXmlInner(clrScheme, `a:${tag}`);
    if (inner) {
      let m = inner.match(/<a:srgbClr val="([A-Fa-f0-9]{6})"/);
      if (m) { colors[tag] = `#${m[1]}`; continue; }
      m = inner.match(/<a:sysClr[^>]*lastClr="([A-Fa-f0-9]{6})"/);
      if (m) { colors[tag] = `#${m[1]}`; continue; }
    }
  }
  // Map tx1→dk1, tx2→dk2, bg1→lt1, bg2→lt2
  colors.tx1 = colors.dk1;
  colors.tx2 = colors.dk2;
  colors.bg1 = colors.lt1;
  colors.bg2 = colors.lt2;

  // Parse font scheme
  _editorMajorFont = "";
  _editorMinorFont = "";
  try {
    const fontScheme = pptxXmlInner(themeXml, "a:fontScheme");
    if (fontScheme) {
      const majorFont = pptxXmlInner(fontScheme, "a:majorFont");
      const minorFont = pptxXmlInner(fontScheme, "a:minorFont");
      if (majorFont) { const m = majorFont.match(/<a:latin typeface="([^"]+)"/); if (m) _editorMajorFont = m[1]; }
      if (minorFont) { const m = minorFont.match(/<a:latin typeface="([^"]+)"/); if (m) _editorMinorFont = m[1]; }
    }
  } catch { /* non-fatal */ }

  // Parse fill styles from fmtScheme
  _editorBgFillStyles = [];
  _editorFillStyles = [];
  try {
    const extractFills = (xml: string) => (xml.match(/<a:(solidFill|gradFill|pattFill|blipFill)[\s>][\s\S]*?<\/a:\1>/g) || []);
    const bgFillLst = pptxXmlInner(themeXml, "a:bgFillStyleLst");
    if (bgFillLst) _editorBgFillStyles = [...extractFills(bgFillLst)];
    const fillLst = pptxXmlInner(themeXml, "a:fillStyleLst");
    if (fillLst) _editorFillStyles = [...extractFills(fillLst)];
  } catch { /* non-fatal */ }

  return colors;
}

let _activeTheme: Record<string, string> = DEFAULT_SCHEME;
let _editorMajorFont = "";
let _editorMinorFont = "";
let _editorBgFillStyles: string[] = [];
let _editorFillStyles: string[] = [];

function pptxParseColor(xml: string): string | null {
  let m = xml.match(/<a:srgbClr val="([A-Fa-f0-9]{6})"/);
  if (m) {
    // Check for lumMod/lumOff transforms
    const lumMod = xml.match(/<a:lumMod val="(\d+)"/);
    const lumOff = xml.match(/<a:lumOff val="(\d+)"/);
    let hex = m[1];
    if (lumMod || lumOff) {
      hex = applyLumTransform(hex, lumMod ? parseInt(lumMod[1], 10) / 100000 : 1, lumOff ? parseInt(lumOff[1], 10) / 100000 : 0);
    }
    return `#${hex}`;
  }
  // System color (e.g. windowText, window)
  m = xml.match(/<a:sysClr[^>]*lastClr="([A-Fa-f0-9]{6})"/);
  if (m) return `#${m[1]}`;
  m = xml.match(/<a:sysClr val="([^"]+)"/);
  if (m) {
    const sysColors: Record<string, string> = { windowText: "#000000", window: "#ffffff", highlight: "#0078d4", highlightText: "#ffffff" };
    return sysColors[m[1]] || "#000000";
  }
  m = xml.match(/<a:schemeClr val="([^"]+)"/);
  if (m) {
    const base = _activeTheme[m[1]] || DEFAULT_SCHEME[m[1]] || "#57534e";
    // Apply luminance transforms (tints/shades)
    const lumMod = xml.match(/<a:lumMod val="(\d+)"/);
    const lumOff = xml.match(/<a:lumOff val="(\d+)"/);
    const tint = xml.match(/<a:tint val="(\d+)"/);
    const shade = xml.match(/<a:shade val="(\d+)"/);
    if (lumMod || lumOff || tint || shade) {
      const hex = base.replace("#", "");
      return `#${applyLumTransform(hex,
        lumMod ? parseInt(lumMod[1], 10) / 100000 : 1,
        lumOff ? parseInt(lumOff[1], 10) / 100000 : 0,
        tint ? parseInt(tint[1], 10) / 100000 : undefined,
        shade ? parseInt(shade[1], 10) / 100000 : undefined,
      )}`;
    }
    return base;
  }
  return null;
}

function editorResolveFont(typeface: string | null | undefined): string | undefined {
  if (!typeface) return undefined;
  if (typeface === "+mj-lt" || typeface === "+mj-ea" || typeface === "+mj-cs") return _editorMajorFont || undefined;
  if (typeface === "+mn-lt" || typeface === "+mn-ea" || typeface === "+mn-cs") return _editorMinorFont || undefined;
  return typeface;
}

/** Apply OOXML luminance transforms to a hex color */
function applyLumTransform(hex: string, mod: number, off: number, tint?: number, shade?: number): string {
  let r = parseInt(hex.slice(0, 2), 16);
  let g = parseInt(hex.slice(2, 4), 16);
  let b = parseInt(hex.slice(4, 6), 16);
  if (tint !== undefined) {
    r = Math.round(r + (255 - r) * (1 - tint));
    g = Math.round(g + (255 - g) * (1 - tint));
    b = Math.round(b + (255 - b) * (1 - tint));
  }
  if (shade !== undefined) {
    r = Math.round(r * shade);
    g = Math.round(g * shade);
    b = Math.round(b * shade);
  }
  // lumMod + lumOff (applied in HSL space approximately)
  if (mod !== 1 || off !== 0) {
    r = Math.round(Math.min(255, Math.max(0, r * mod + 255 * off)));
    g = Math.round(Math.min(255, Math.max(0, g * mod + 255 * off)));
    b = Math.round(Math.min(255, Math.max(0, b * mod + 255 * off)));
  }
  return [r, g, b].map(v => Math.min(255, Math.max(0, v)).toString(16).padStart(2, "0")).join("");
}

function pptxGradToCss(g: { angle: number; stops: { pos: number; color: string; alpha: number }[] }): string {
  const stops = g.stops.map(s => {
    const r = parseInt(s.color.slice(1, 3), 16);
    const gv = parseInt(s.color.slice(3, 5), 16);
    const b = parseInt(s.color.slice(5, 7), 16);
    return `rgba(${r},${gv},${b},${s.alpha}) ${s.pos}%`;
  }).join(", ");
  if (g.angle === -1) return `radial-gradient(ellipse at center, ${stops})`;
  // PPTX: 0°=right, CSS: 0°=up → add 90° for CSS conversion
  return `linear-gradient(${g.angle + 90}deg, ${stops})`;
}

function pptxParseGradient(xml: string, phClrOverride?: string): PptxShape["gradFill"] | undefined {
  const gradXml = pptxXmlInner(xml, "a:gradFill");
  if (!gradXml) return undefined;
  const stops: { pos: number; color: string; alpha: number }[] = [];
  const gsMatches = gradXml.match(/<a:gs[\s>][\s\S]*?<\/a:gs>/g) || [];
  for (const gs of gsMatches) {
    const pos = parseInt(pptxXmlAttr(gs, "pos") || "0", 10) / 1000;
    const usesPhClr = /schemeClr val="phClr"/.test(gs);
    let color: string;
    if (usesPhClr && phClrOverride) {
      const lumMod = gs.match(/<a:lumMod val="(\d+)"/);
      const lumOff = gs.match(/<a:lumOff val="(\d+)"/);
      const tint = gs.match(/<a:tint val="(\d+)"/);
      const shade = gs.match(/<a:shade val="(\d+)"/);
      if (lumMod || lumOff || tint || shade) {
        color = `#${applyLumTransform(
          phClrOverride.replace("#", ""),
          lumMod ? parseInt(lumMod[1], 10) / 100000 : 1,
          lumOff ? parseInt(lumOff[1], 10) / 100000 : 0,
          tint ? parseInt(tint[1], 10) / 100000 : undefined,
          shade ? parseInt(shade[1], 10) / 100000 : undefined,
        )}`;
      } else {
        color = phClrOverride;
      }
    } else {
      color = pptxParseColor(gs) || "#000000";
    }
    const alphaM = gs.match(/<a:alpha val="(\d+)"/);
    const alpha = alphaM ? parseInt(alphaM[1], 10) / 100000 : 1;
    stops.push({ pos, color, alpha });
  }
  const angMatch = gradXml.match(/<a:lin ang="(\d+)"/);
  const angle = angMatch ? parseInt(angMatch[1], 10) / 60000 : 0;
  const isRadial = /<a:path\s/.test(gradXml);
  return stops.length > 0 ? { angle: isRadial ? -1 : angle, stops } : undefined;
}

let _shapeCounter = 0;
function genId() { return `s${++_shapeCounter}_${Date.now()}`; }

function clonePptxShape(shape: PptxShape, offset = 0): PptxShape {
  const clone = JSON.parse(JSON.stringify(shape)) as PptxShape;
  return {
    ...clone,
    id: genId(),
    x: Math.max(0, Math.min(100 - clone.w, clone.x + offset)),
    y: Math.max(0, Math.min(100 - clone.h, clone.y + offset)),
  };
}

/** Parse shape stroke/border */
function pptxParseStroke(xml: string): { color?: string; width?: number } {
  const ln = pptxXmlInner(xml, "a:ln");
  if (!ln) return {};
  // Check for noFill (no stroke)
  if (ln.includes("<a:noFill")) return {};
  const color = pptxParseColor(ln);
  const wAttr = pptxXmlAttr(ln, "w");
  const width = wAttr ? parseInt(wAttr, 10) / 12700 : 1; // EMU → pt
  return { color: color || undefined, width: color ? width : undefined };
}

/** Parse text runs with enhanced properties */
function pptxParseTextRuns(spXml: string): PptxTextRun[] {
  const texts: PptxTextRun[] = [];
  const paras = spXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [];
  for (const para of paras) {
    const pPr = pptxXmlInner(para, "a:pPr");
    const align = pPr ? (pptxXmlAttr(pPr, "algn") || undefined) : undefined;
    const lvl = pPr ? parseInt(pptxXmlAttr(pPr, "lvl") || "0", 10) : 0;
    const marL = pPr ? pptxXmlAttr(pPr, "marL") : null;
    const indent = marL ? parseInt(marL, 10) / 12700 : lvl * 18;

    // Detect bullet
    let bullet: string | undefined;
    if (pPr) {
      const buChar = pPr.match(/<a:buChar char="([^"]+)"/);
      if (buChar) bullet = buChar[1];
      else if (pPr.includes("<a:buAutoNum")) {
        const autoNumType = (pPr.match(/<a:buAutoNum type="([^"]+)"/) || [])[1] || "arabicPeriod";
        if (autoNumType.startsWith("alpha")) bullet = "a.";
        else if (autoNumType.startsWith("roman")) bullet = "i.";
        else bullet = "#.";
      }
      // buNone means explicitly no bullet
      else if (!pPr.includes("<a:buNone") && lvl > 0) bullet = "\u2022";
    }

    // Line/paragraph spacing
    let lineSpacing: number | undefined, spaceBefore: number | undefined, spaceAfter: number | undefined;
    if (pPr) {
      const lnSpc = pptxXmlInner(pPr, "a:lnSpc");
      if (lnSpc) {
        const spcPct = lnSpc.match(/<a:spcPct val="(\d+)"/);
        if (spcPct) lineSpacing = parseInt(spcPct[1], 10) / 100000;
        const spcPts = lnSpc.match(/<a:spcPts val="(\d+)"/);
        if (spcPts) lineSpacing = parseInt(spcPts[1], 10) / 100 / 12;
      }
      const spcBef = pptxXmlInner(pPr, "a:spcBef");
      if (spcBef) { const pts = spcBef.match(/<a:spcPts val="(\d+)"/); if (pts) spaceBefore = parseInt(pts[1], 10) / 100; }
      const spcAft = pptxXmlInner(pPr, "a:spcAft");
      if (spcAft) { const pts = spcAft.match(/<a:spcPts val="(\d+)"/); if (pts) spaceAfter = parseInt(pts[1], 10) / 100; }
    }

    // Parse default paragraph text properties (defRPr and endParaRPr)
    let defFontSize: number | undefined, defColor: string | undefined;
    let defBold = false, defItalic = false, defFontFamily: string | undefined;
    const defRPr = pPr ? pptxXmlInner(pPr, "a:defRPr") : null;
    if (defRPr) {
      const szM = pptxXmlAttr(defRPr, "sz");
      if (szM) defFontSize = parseInt(szM, 10) / 100;
      const c = pptxParseColor(defRPr);
      if (c) defColor = c;
      defBold = defRPr.includes('b="1"');
      defItalic = defRPr.includes('i="1"');
      const latin = defRPr.match(/<a:latin typeface="([^"]+)"/);
      const ea = defRPr.match(/<a:ea typeface="([^"]+)"/);
      defFontFamily = editorResolveFont(latin?.[1]) || editorResolveFont(ea?.[1]) || defFontFamily;
    }
    const endParaRPr = pptxXmlInner(para, "a:endParaRPr");
    if (endParaRPr) {
      const szM = pptxXmlAttr(endParaRPr, "sz");
      if (szM && !defFontSize) defFontSize = parseInt(szM, 10) / 100;
      const c = pptxParseColor(endParaRPr);
      if (c && !defColor) defColor = c;
    }

    // Collect runs and line breaks in document order
    const tokens = para.match(/<a:r[\s>][\s\S]*?<\/a:r>|<a:br\s*\/>|<a:br[\s>][\s\S]*?<\/a:br>|<a:fld[\s>][\s\S]*?<\/a:fld>/g) || [];
    const runs: NonNullable<PptxTextRun["runs"]> = [];
    let paraText = "";
    let firstBold = defBold, firstItalic = defItalic, firstUnderline = false;
    let firstFontSize = defFontSize, firstColor = defColor, firstFontFamily = defFontFamily;
    let isFirst = true;
    for (const token of tokens) {
      if (token.startsWith("<a:br")) { paraText += "\n"; runs.push({ text: "\n" }); continue; }
      let rBold = defBold, rItalic = defItalic, rUnderline = false;
      let rFontSize = defFontSize, rColor = defColor, rFontFamily = defFontFamily;
      let rStrike = false, rBaseline: number | undefined, rSpacing: number | undefined;
      const rPr = pptxXmlInner(token, "a:rPr");
      if (rPr) {
        rBold = rPr.includes('b="1"');
        rItalic = rPr.includes('i="1"');
        rUnderline = rPr.includes('u="sng"') || rPr.includes('u="dbl"') || rPr.includes('u="heavy"');
        rStrike = rPr.includes('strike="sngStrike"') || rPr.includes('strike="dblStrike"');
        const szM = pptxXmlAttr(rPr, "sz");
        if (szM) rFontSize = parseInt(szM, 10) / 100;
        const rc = pptxParseColor(rPr);
        if (rc) rColor = rc;
        const latin = rPr.match(/<a:latin typeface="([^"]+)"/);
        const ea = rPr.match(/<a:ea typeface="([^"]+)"/);
        rFontFamily = editorResolveFont(latin?.[1]) || editorResolveFont(ea?.[1]) || rFontFamily;
        const baselineM = pptxXmlAttr(rPr, "baseline");
        if (baselineM) rBaseline = parseInt(baselineM, 10) / 1000;
        const spcM = pptxXmlAttr(rPr, "spc");
        if (spcM) rSpacing = parseInt(spcM, 10) / 100;
      }
      const tMatch = token.match(/<a:t>([^<]*)<\/a:t>/);
      const runText = tMatch ? tMatch[1] : "";
      if (!runText) continue;
      paraText += runText;
      runs.push({
        text: runText,
        bold: rBold || undefined, italic: rItalic || undefined,
        underline: rUnderline || undefined, strikethrough: rStrike || undefined,
        fontSize: rFontSize, color: rColor, fontFamily: rFontFamily,
        baseline: rBaseline, spacing: rSpacing,
      });
      if (isFirst) {
        firstBold = rBold; firstItalic = rItalic; firstUnderline = rUnderline;
        firstFontSize = rFontSize; firstColor = rColor; firstFontFamily = rFontFamily;
        isFirst = false;
      }
    }

    if (paraText.trim() || paraText.includes("\n")) {
      texts.push({
        text: paraText, bold: firstBold, italic: firstItalic, underline: firstUnderline,
        fontSize: firstFontSize, color: firstColor, fontFamily: firstFontFamily,
        align: align === "ctr" ? "center" : align === "r" ? "right" : align === "just" ? "justify" : undefined,
        bullet, indent: indent > 0 ? indent : undefined,
        lineSpacing, spaceBefore, spaceAfter,
        runs: runs.length > 1 ? runs : undefined,
      });
    } else {
      texts.push({ text: "", fontSize: firstFontSize || defFontSize || 12 });
    }
  }
  return texts;
}

function pptxDecodeText(value: string): string {
  return value
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function pptxParseSpeakerNotes(xml: string): string {
  const bodyShape = (xml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [])
    .find((shape) => /<p:ph[^>]*type="body"/.test(shape));
  if (!bodyShape) return "";
  return (bodyShape.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [])
    .map((paragraph) => {
      const tokens = paragraph.match(/<a:t>[^<]*<\/a:t>|<a:br\s*\/>/g) || [];
      return tokens.map((token) => {
        if (token.startsWith("<a:br")) return "\n";
        return pptxDecodeText(token.replace(/^<a:t>|<\/a:t>$/g, ""));
      }).join("");
    })
    .join("\n")
    .trim();
}

/** Parse a table from graphicFrame */
function pptxParseTable(xml: string): { rows: PptxTableCell[][]; cols: number; colWidths?: number[] } | null {
  const tbl = pptxXmlInner(xml, "a:tbl");
  if (!tbl) return null;
  // Parse column widths from <a:tblGrid>
  const tblGrid = pptxXmlInner(tbl, "a:tblGrid");
  let colWidths: number[] | undefined;
  if (tblGrid) {
    const gridCols = tblGrid.match(/<a:gridCol[^>]*\/>/g) || [];
    colWidths = gridCols.map(gc => parseInt((gc.match(/w="(\d+)"/) || [])[1] || "0", 10));
  }
  const trMatches = tbl.match(/<a:tr[\s>][\s\S]*?<\/a:tr>/g) || [];
  const rows: PptxTableCell[][] = [];
  let maxCols = 0;
  for (const tr of trMatches) {
    const tcMatches = tr.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/g) || [];
    const row: PptxTableCell[] = [];
    for (const tc of tcMatches) {
      const text = (tc.match(/<a:t>([^<]*)<\/a:t>/g) || []).map(m => m.replace(/<\/?a:t>/g, "")).join(" ");
      const rPr = pptxXmlInner(tc, "a:rPr");
      const bold = rPr ? rPr.includes('b="1"') : false;
      const color = rPr ? pptxParseColor(rPr) : null;
      const tcPr = pptxXmlInner(tc, "a:tcPr");
      const fill = tcPr ? pptxParseColor(tcPr) : null;
      const gridSpanM = tc.match(/gridSpan="(\d+)"/);
      const vMerge = tc.includes('vMerge="1"') || tc.includes('hMerge="1"');
      row.push({ text, bold, color: color || undefined, fill: fill || undefined, gridSpan: gridSpanM ? parseInt(gridSpanM[1], 10) : undefined, vMerge: vMerge || undefined });
    }
    rows.push(row);
    maxCols = Math.max(maxCols, row.length);
  }
  return rows.length > 0 ? { rows, cols: maxCols, colWidths } : null;
}

/** Parse a single shape XML element into a PptxShape */
/** Extract placeholder type/idx from shape XML (editor) */
function editorParsePlaceholder(spXml: string): { type?: string; idx?: string } | null {
  const phM = spXml.match(/<p:ph([^/>]*)\/?>/);
  if (!phM) return null;
  const type = pptxXmlAttr(phM[0], "type") || undefined;
  const idx = pptxXmlAttr(phM[0], "idx") || undefined;
  return { type, idx };
}

/** Build placeholder key→position map from layout/master XML (editor) */
function editorBuildPhMap(xmlStr: string): Map<string, { x: number; y: number; w: number; h: number }> {
  const map = new Map<string, { x: number; y: number; w: number; h: number }>();
  const shapes = xmlStr.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [];
  for (const sp of shapes) {
    const ph = editorParsePlaceholder(sp);
    if (!ph) continue;
    const xfrm = pptxXmlInner(sp, "a:xfrm");
    if (!xfrm) continue;
    const offM = xfrm.match(/<a:off x="(\d+)" y="(\d+)"/);
    const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/);
    if (!offM || !extM) continue;
    const pos = {
      x: emu2pctX(parseInt(offM[1], 10)),
      y: emu2pctY(parseInt(offM[2], 10)),
      w: emu2pctX(parseInt(extM[1], 10)),
      h: emu2pctY(parseInt(extM[2], 10)),
    };
    const key = ph.type || `idx:${ph.idx}`;
    map.set(key, pos);
    if (ph.idx) map.set(`idx:${ph.idx}`, pos);
  }
  return map;
}

function editorParseBgFromXml(bgXml: string): { color?: string; grad?: PptxSlide["bgGrad"]; imgRId?: string } {
  const result: { color?: string; grad?: PptxSlide["bgGrad"]; imgRId?: string } = {};
  const bgPr = pptxXmlInner(bgXml, "p:bgPr");
  if (bgPr) {
    result.color = pptxParseColor(bgPr) || undefined;
    result.grad = pptxParseGradient(bgPr);
    const blip = bgPr.match(/<a:blip r:embed="([^"]+)"/);
    if (blip) result.imgRId = blip[1];
    if (result.color || result.grad || result.imgRId) return result;
  }
  const bgRef = pptxXmlInner(bgXml, "p:bgRef");
  if (bgRef) {
    const idxMatch = bgRef.match(/idx="(\d+)"/);
    const idx = idxMatch ? parseInt(idxMatch[1], 10) : 0;
    const refColor = pptxParseColor(bgRef);
    const styleList = idx >= 1001 ? _editorBgFillStyles : _editorFillStyles;
    const fillIdx = idx >= 1001 ? idx - 1001 : idx - 1;
    if (fillIdx >= 0 && fillIdx < styleList.length) {
      const fillXml = styleList[fillIdx];
      if (fillXml.includes("<a:solidFill")) {
        result.color = refColor || pptxParseColor(fillXml) || undefined;
      } else if (fillXml.includes("<a:gradFill")) {
        result.grad = pptxParseGradient(fillXml, refColor || undefined);
        if (!result.grad && refColor) result.color = refColor;
      }
    }
    if (!result.color && !result.grad && refColor) result.color = refColor;
  }
  if (!result.color && !result.grad) {
    result.color = pptxParseColor(bgXml) || undefined;
    result.grad = pptxParseGradient(bgXml);
    const blip = bgXml.match(/<a:blip r:embed="([^"]+)"/);
    if (blip) result.imgRId = blip[1];
  }
  return result;
}

function pptxParseShapeXml(sp: string, relsMap: Map<string, string>, phMap?: Map<string, { x: number; y: number; w: number; h: number }>): PptxShape | null {
  let x: number, y: number, w: number, h: number;

  const xfrm = pptxXmlInner(sp, "a:xfrm");
  if (xfrm) {
    const offM = xfrm.match(/<a:off x="(\d+)" y="(\d+)"/);
    const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/);
    if (!offM || !extM) return null;
    x = emu2pctX(parseInt(offM[1], 10));
    y = emu2pctY(parseInt(offM[2], 10));
    w = emu2pctX(parseInt(extM[1], 10));
    h = emu2pctY(parseInt(extM[2], 10));
  } else if (phMap) {
    // Resolve from placeholder map (layout/master inheritance)
    const ph = editorParsePlaceholder(sp);
    if (!ph) return null;
    const pos = phMap.get(ph.type || "") || phMap.get(`idx:${ph.idx}`) || (ph.idx === "1" ? phMap.get("body") : null);
    if (!pos) return null;
    x = pos.x; y = pos.y; w = pos.w; h = pos.h;
  } else {
    return null;
  }

  const shape: PptxShape = {
    id: genId(),
    type: "shape",
    x, y, w, h,
    texts: [],
  };

  // Rotation + flip (in 60000ths of a degree)
  if (xfrm) {
    const rotAttr = pptxXmlAttr(xfrm, "rot");
    if (rotAttr) shape.rotation = parseInt(rotAttr, 10) / 60000;
    if (xfrm.includes('flipH="1"')) shape.flipH = true;
    if (xfrm.includes('flipV="1"')) shape.flipV = true;
  }

  // Preset geometry
  const geomM = sp.match(/<a:prstGeom prst="([^"]+)"/);
  if (geomM) shape.presetGeom = geomM[1];

  // Fill — parse within spPr to avoid picking up text fills
  const spPr = pptxXmlInner(sp, "p:spPr") || pptxXmlInner(sp, "xdr:spPr") || sp;
  const hasNoFill = spPr.includes("<a:noFill");
  if (!hasNoFill) {
    const solidFill = pptxXmlInner(spPr, "a:solidFill");
    if (solidFill) shape.fill = pptxParseColor(solidFill) || undefined;
    shape.gradFill = pptxParseGradient(spPr);

    // Blip fill (texture/image fill on shapes)
    if (!shape.fill && !shape.gradFill) {
      const blipFill = pptxXmlInner(spPr, "a:blipFill") || pptxXmlInner(sp, "p:blipFill");
      if (blipFill) {
        const blipM = blipFill.match(/<a:blip r:embed="([^"]+)"/);
        if (blipM) {
          const imgUrl = relsMap.get(blipM[1]);
          if (imgUrl) shape.imgUrl = imgUrl;
        }
      }
    }
  } else {
    shape.fill = "transparent";
  }

  // Stroke/border
  const stroke = pptxParseStroke(sp);
  if (stroke.color) { shape.stroke = stroke.color; shape.strokeWidth = stroke.width; }

  // Opacity
  const alphaM = sp.match(/<a:alpha val="(\d+)"/);
  if (alphaM) shape.opacity = parseInt(alphaM[1], 10) / 100000;

  // Shadow (outer shadow)
  const outerShdw = pptxXmlInner(sp, "a:outerShdw");
  if (outerShdw) {
    const shdwBlur = parseInt(pptxXmlAttr(outerShdw, "blurRad") || "0", 10) / 12700;
    const shdwDist = parseInt(pptxXmlAttr(outerShdw, "dist") || "0", 10) / 12700;
    const shdwAng = parseInt(pptxXmlAttr(outerShdw, "dir") || "0", 10) / 60000;
    const shdwColor = pptxParseColor(outerShdw) || "#000000";
    const shdwAlphaM = outerShdw.match(/<a:alpha val="(\d+)"/);
    const shdwAlpha = shdwAlphaM ? parseInt(shdwAlphaM[1], 10) / 100000 : 0.4;
    shape.shadow = { blur: shdwBlur, dist: shdwDist, angle: shdwAng, color: shdwColor, alpha: shdwAlpha };
  }

  // Border radius
  if (sp.includes('prst="roundRect"')) {
    const adjM = sp.match(/name="adj" fmla="val (\d+)"/);
    shape.borderRadius = adjM ? Math.min(50, parseInt(adjM[1], 10) / 1000) : 8;
  }

  // Text body properties (vertical alignment + insets)
  const bodyPr = pptxXmlInner(sp, "a:bodyPr");
  if (bodyPr) {
    const anchor = pptxXmlAttr(bodyPr, "anchor");
    if (anchor === "t") shape.vAlign = "top";
    else if (anchor === "b") shape.vAlign = "bottom";
    else if (anchor === "ctr") shape.vAlign = "middle";
    const lIns = pptxXmlAttr(bodyPr, "lIns");
    const tIns = pptxXmlAttr(bodyPr, "tIns");
    const rIns = pptxXmlAttr(bodyPr, "rIns");
    const bIns = pptxXmlAttr(bodyPr, "bIns");
    if (lIns || tIns || rIns || bIns) {
      shape.padding = {
        l: lIns ? parseInt(lIns, 10) / 12700 : 7,
        t: tIns ? parseInt(tIns, 10) / 12700 : 4,
        r: rIns ? parseInt(rIns, 10) / 12700 : 7,
        b: bIns ? parseInt(bIns, 10) / 12700 : 4,
      };
    }
  }

  // Text
  shape.texts = pptxParseTextRuns(sp);

  // Image (p:pic blip)
  if (!shape.imgUrl) {
    const blipM = sp.match(/<a:blip r:embed="([^"]+)"/);
    if (blipM) {
      const imgUrl = relsMap.get(blipM[1]);
      if (imgUrl) { shape.imgUrl = imgUrl; shape.type = "image"; }
    }
  }
  // Image cropping (srcRect)
  if (shape.imgUrl) {
    const srcRect = sp.match(/<a:srcRect\s+([^/]*)\/>/);
    if (srcRect) {
      const attrs = srcRect[1];
      const l = parseInt((attrs.match(/l="(\d+)"/) || [])[1] || "0", 10) / 1000;
      const t = parseInt((attrs.match(/t="(\d+)"/) || [])[1] || "0", 10) / 1000;
      const r = parseInt((attrs.match(/r="(\d+)"/) || [])[1] || "0", 10) / 1000;
      const b = parseInt((attrs.match(/b="(\d+)"/) || [])[1] || "0", 10) / 1000;
      if (l || t || r || b) shape.imgCrop = { l, t, r, b };
    }
  }

  return shape;
}

async function parsePptxForEditor(buf: ArrayBuffer): Promise<PptxSlide[]> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(buf);
  const slides: PptxSlide[] = [];

  // Read slide size and slide order from presentation.xml
  let orderedSlideRIds: string[] = [];
  let presRelsMap = new Map<string, string>();
  try {
    const presEntry = zip.file("ppt/presentation.xml");
    if (presEntry) {
      const presXml = await presEntry.async("text");
      const sldSz = presXml.match(/<p:sldSz[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
      if (sldSz) {
        EDITOR_SLIDE_W = parseInt(sldSz[1], 10);
        EDITOR_SLIDE_H = parseInt(sldSz[2], 10);
      }
      const sldIdLst = presXml.match(/<p:sldId[^/]*\/>/g) || [];
      orderedSlideRIds = sldIdLst.map(s => (s.match(/r:id="([^"]+)"/) || [])[1]).filter(Boolean);
    }
    const presRelsEntry = zip.file("ppt/_rels/presentation.xml.rels");
    if (presRelsEntry) {
      const presRelsXml = await presRelsEntry.async("text");
      for (const rel of (presRelsXml.match(/<Relationship[^>]*\/>/g) || [])) {
        const id = pptxXmlAttr(rel, "Id");
        const target = pptxXmlAttr(rel, "Target");
        if (id && target) {
          const resolved = target.startsWith("../") ? target.slice(3) : target.startsWith("/") ? target.slice(1) : `ppt/${target}`;
          presRelsMap.set(id, resolved);
        }
      }
    }
  } catch { /* default 16:9 */ }

  // Parse theme colors (non-fatal)
  try {
    const themeEntry = zip.file("ppt/theme/theme1.xml");
    if (themeEntry) {
      _activeTheme = parseThemeColors(await themeEntry.async("text"));
    } else {
      _activeTheme = DEFAULT_SCHEME;
    }
  } catch { _activeTheme = DEFAULT_SCHEME; }

  let slideFiles: string[];
  let hasExplicitSlideOrder = orderedSlideRIds.length > 0 && presRelsMap.size > 0;
  if (hasExplicitSlideOrder) {
    slideFiles = orderedSlideRIds.map(rId => presRelsMap.get(rId)).filter((p): p is string => !!p && /slide\d+\.xml$/.test(p));
  } else {
    slideFiles = Object.keys(zip.files).filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n));
  }
  if (slideFiles.length === 0) {
    slideFiles = Object.keys(zip.files).filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n));
    hasExplicitSlideOrder = false;
  }
  if (!hasExplicitSlideOrder) {
    slideFiles.sort((a, b) => {
      const na = parseInt(a.match(/slide(\d+)/)?.[1] || "0", 10);
      const nb = parseInt(b.match(/slide(\d+)/)?.[1] || "0", 10);
      return na - nb;
    });
  }

  // Extract media as data URLs so images survive editor saves and reloads.
  const mediaCache = new Map<string, string>();
  for (const name of Object.keys(zip.files)) {
    if (name.startsWith("ppt/media/") && !zip.files[name].dir) {
      try {
        const entry = zip.file(name);
        if (!entry) continue;
        const data = await entry.async("base64");
        const ext = name.split(".").pop()?.toLowerCase() || "";
        const mime = ext === "jpg" || ext === "jpeg"
          ? "image/jpeg"
          : ext === "svg"
            ? "image/svg+xml"
            : ext === "webp"
              ? "image/webp"
              : ext === "gif"
                ? "image/gif"
                : "image/png";
        mediaCache.set(name, `data:${mime};base64,${data}`);
      } catch { /* skip bad media */ }
    }
  }

  // Pre-parse slide layouts and slide masters (non-fatal)
  const layoutCache = new Map<string, string>();
  const masterCache = new Map<string, string>();
  const layoutToMasterPath = new Map<string, string>();
  const masterRelsCache = new Map<string, Map<string, string>>();
  try {
    for (const name of Object.keys(zip.files)) {
      if (/^ppt\/slideLayouts\/slideLayout\d+\.xml$/.test(name)) {
        const e = zip.file(name);
        if (e) layoutCache.set(name, await e.async("text"));
      }
      if (/^ppt\/slideMasters\/slideMaster\d+\.xml$/.test(name)) {
        const e = zip.file(name);
        if (e) masterCache.set(name, await e.async("text"));
      }
    }
    // Build layout → master mapping
    for (const layoutPath of layoutCache.keys()) {
      try {
        const layoutNum = layoutPath.match(/slideLayout(\d+)/)?.[1] || "1";
        const lre = zip.file(`ppt/slideLayouts/_rels/slideLayout${layoutNum}.xml.rels`);
        if (lre) {
          for (const rel of ((await lre.async("text")).match(/<Relationship[^>]*\/>/g) || [])) {
            const target = pptxXmlAttr(rel, "Target");
            const type = pptxXmlAttr(rel, "Type");
            if (target && type?.includes("slideMaster")) {
              layoutToMasterPath.set(layoutPath, target.startsWith("../") ? "ppt/" + target.slice(3) : target);
            }
          }
        }
      } catch { /* non-fatal */ }
    }
    // Pre-cache master rels for media
    for (const masterPath of masterCache.keys()) {
      try {
        const masterNum = masterPath.match(/slideMaster(\d+)/)?.[1] || "1";
        const mre = zip.file(`ppt/slideMasters/_rels/slideMaster${masterNum}.xml.rels`);
        if (mre) {
          const masterRels = new Map<string, string>();
          for (const rel of ((await mre.async("text")).match(/<Relationship[^>]*\/>/g) || [])) {
            const id = pptxXmlAttr(rel, "Id");
            const target = pptxXmlAttr(rel, "Target");
            if (id && target) {
              const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
              if (mediaCache.has(resolved)) masterRels.set(id, mediaCache.get(resolved)!);
            }
          }
          masterRelsCache.set(masterPath, masterRels);
        }
      } catch { /* non-fatal */ }
    }
  } catch { /* layouts/masters optional */ }

  for (const slidePath of slideFiles) {
    try {
      const entry = zip.file(slidePath);
      if (!entry) continue;
      // Unwrap <mc:AlternateContent> — prefer <mc:Choice> (modern), fall back to <mc:Fallback>
      let xml = await entry.async("text");
      xml = xml.replace(/<mc:AlternateContent[\s>][\s\S]*?<\/mc:AlternateContent>/g, (block) => {
        const choice = block.match(/<mc:Choice[\s>]([\s\S]*?)<\/mc:Choice>/);
        if (choice && choice[1].trim()) return choice[1];
        const fallback = block.match(/<mc:Fallback[\s>]([\s\S]*?)<\/mc:Fallback>/);
        return fallback ? fallback[1] : "";
      });
      const slideNum = slidePath.match(/slide(\d+)/)?.[1] || "1";

      const relsMap = new Map<string, string>();
      let layoutPath: string | undefined;
      let notesPath: string | undefined;
      try {
        const relsEntry = zip.file(`ppt/slides/_rels/slide${slideNum}.xml.rels`);
        if (relsEntry) {
          const relsXml = await relsEntry.async("text");
          for (const rel of (relsXml.match(/<Relationship[^>]*\/>/g) || [])) {
            const id = pptxXmlAttr(rel, "Id");
            const target = pptxXmlAttr(rel, "Target");
            const type = pptxXmlAttr(rel, "Type");
            if (id && target) {
              const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
              if (mediaCache.has(resolved)) relsMap.set(id, mediaCache.get(resolved)!);
              if (type && type.includes("slideLayout")) layoutPath = resolved;
              if (type && type.includes("notesSlide")) notesPath = resolved;
            }
          }
        }
        if (layoutPath) {
          const lre = zip.file(layoutPath.replace(/slideLayouts\//, "slideLayouts/_rels/") + ".rels");
          if (lre) {
            for (const rel of ((await lre.async("text")).match(/<Relationship[^>]*\/>/g) || [])) {
              const id = pptxXmlAttr(rel, "Id");
              const target = pptxXmlAttr(rel, "Target");
              if (id && target) {
                const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
                if (mediaCache.has(resolved) && !relsMap.has(id)) relsMap.set(id, mediaCache.get(resolved)!);
              }
            }
          }
        }
      } catch { /* rels non-fatal */ }

      // Merge master rels into relsMap
      if (layoutPath) {
        const masterPath = layoutToMasterPath.get(layoutPath);
        if (masterPath && masterRelsCache.has(masterPath)) {
          for (const [id, url] of masterRelsCache.get(masterPath)!) {
            if (!relsMap.has(id)) relsMap.set(id, url);
          }
        }
      }

      const slide: PptxSlide = { id: genId(), aspectRatio: `${EDITOR_SLIDE_W}/${EDITOR_SLIDE_H}`, shapes: [] };
      if (notesPath) {
        try {
          const notesEntry = zip.file(notesPath);
          if (notesEntry) slide.notes = pptxParseSpeakerNotes(await notesEntry.async("text"));
        } catch { /* notes are optional */ }
      }

      // Build placeholder position map: master → layout (later overrides)
      const phMap = new Map<string, { x: number; y: number; w: number; h: number }>();
      try {
        if (layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            for (const [k, v] of editorBuildPhMap(masterCache.get(masterPath)!)) phMap.set(k, v);
          }
          if (layoutCache.has(layoutPath)) {
            for (const [k, v] of editorBuildPhMap(layoutCache.get(layoutPath)!)) phMap.set(k, v);
          }
        }
      } catch { /* phMap non-fatal */ }

      // Background — slide → layout → slide master fallback (with bgRef resolution)
      try {
        let bgXml = pptxXmlInner(xml, "p:bg");
        if (!bgXml && layoutPath && layoutCache.has(layoutPath)) {
          bgXml = pptxXmlInner(layoutCache.get(layoutPath)!, "p:bg");
        }
        if (!bgXml && layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            bgXml = pptxXmlInner(masterCache.get(masterPath)!, "p:bg");
          }
        }
        if (bgXml) {
          const bgResult = editorParseBgFromXml(bgXml);
          if (bgResult.color) slide.bg = bgResult.color;
          if (bgResult.grad) slide.bgGrad = bgResult.grad;
          if (bgResult.imgRId && relsMap.has(bgResult.imgRId)) slide.bgImgUrl = relsMap.get(bgResult.imgRId);
        }
      } catch { /* bg non-fatal */ }

      // Parse top-level shapes separately from group children to avoid rendering
      // grouped objects twice.
      const groupMatches = xml.match(/<p:grpSp[\s>][\s\S]*?<\/p:grpSp>/g) || [];
      const topLevelXml = xml.replace(/<p:grpSp[\s>][\s\S]*?<\/p:grpSp>/g, "");
      const spMatches = topLevelXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [];
      const picMatches = topLevelXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || [];
      const cxnMatches = topLevelXml.match(/<p:cxnSp[\s>][\s\S]*?<\/p:cxnSp>/g) || [];

      // Group shapes with coordinate transforms
      try {
        for (const grp of groupMatches) {
          const grpSpPr = pptxXmlInner(grp, "p:grpSpPr");
          const grpXfrm = grpSpPr ? pptxXmlInner(grpSpPr, "a:xfrm") : null;
          let grpOffX = 0, grpOffY = 0, grpExtCx = 1, grpExtCy = 1;
          let chOffX = 0, chOffY = 0, chExtCx = 1, chExtCy = 1;
          if (grpXfrm) {
            const off = grpXfrm.match(/<a:off x="(\d+)" y="(\d+)"/);
            const ext = grpXfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/);
            const chOff = grpXfrm.match(/<a:chOff x="(\d+)" y="(\d+)"/);
            const chExt = grpXfrm.match(/<a:chExt cx="(\d+)" cy="(\d+)"/);
            if (off) { grpOffX = parseInt(off[1], 10); grpOffY = parseInt(off[2], 10); }
            if (ext) { grpExtCx = parseInt(ext[1], 10); grpExtCy = parseInt(ext[2], 10); }
            if (chOff) { chOffX = parseInt(chOff[1], 10); chOffY = parseInt(chOff[2], 10); }
            if (chExt) { chExtCx = parseInt(chExt[1], 10); chExtCy = parseInt(chExt[2], 10); }
          }
          const children = [
            ...(grp.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
            ...(grp.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
            ...(grp.match(/<p:cxnSp[\s>][\s\S]*?<\/p:cxnSp>/g) || []),
          ];
          for (const ch of children) {
            const s = pptxParseShapeXml(ch, relsMap, phMap);
            if (s && grpXfrm && chExtCx > 0 && chExtCy > 0) {
              const cxE = (s.x / 100) * EDITOR_SLIDE_W, cyE = (s.y / 100) * EDITOR_SLIDE_H;
              const cwE = (s.w / 100) * EDITOR_SLIDE_W, chE = (s.h / 100) * EDITOR_SLIDE_H;
              s.x = emu2pctX(grpOffX + (cxE - chOffX) * (grpExtCx / chExtCx));
              s.y = emu2pctY(grpOffY + (cyE - chOffY) * (grpExtCy / chExtCy));
              s.w = emu2pctX(cwE * (grpExtCx / chExtCx));
              s.h = emu2pctY(chE * (grpExtCy / chExtCy));
            }
            if (s) slide.shapes.push(s);
          }
        }
      } catch { /* group extraction non-fatal */ }

      for (const sp of [...spMatches, ...picMatches, ...cxnMatches]) {
        try {
          const s = pptxParseShapeXml(sp, relsMap, phMap);
          if (s) slide.shapes.push(s);
        } catch { /* individual shape non-fatal */ }
      }

      // Tables (non-fatal)
      try {
        for (const gf of (xml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [])) {
          const xfrm = pptxXmlInner(gf, "a:xfrm") || pptxXmlInner(gf, "p:xfrm");
          if (!xfrm) continue;
          const offM = xfrm.match(/<a:off x="(\d+)" y="(\d+)"/) || xfrm.match(/<p:off x="(\d+)" y="(\d+)"/);
          const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/) || xfrm.match(/<p:ext cx="(\d+)" cy="(\d+)"/);
          if (!offM || !extM) continue;
          const table = pptxParseTable(gf);
          if (table) {
            slide.shapes.push({
              id: genId(), type: "table",
              x: emu2pctX(parseInt(offM[1], 10)), y: emu2pctY(parseInt(offM[2], 10)),
              w: emu2pctX(parseInt(extM[1], 10)), h: emu2pctY(parseInt(extM[2], 10)),
              texts: [], tableRows: table.rows, tableCols: table.cols, tableColWidths: table.colWidths,
            });
          }
        }
      } catch { /* tables non-fatal */ }

      // Layout decorative shapes (non-fatal)
      try {
        if (layoutPath && layoutCache.has(layoutPath)) {
          const layoutXml = layoutCache.get(layoutPath)!;
          for (const sp of [
            ...(layoutXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
            ...(layoutXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
          ]) {
            if (sp.includes("<p:ph")) continue;
            const lShape = pptxParseShapeXml(sp, relsMap);
            if (lShape && (lShape.fill || lShape.gradFill || lShape.imgUrl || lShape.stroke)) {
              slide.shapes.unshift(lShape);
            }
          }
        }
      } catch { /* layout shapes non-fatal */ }

      // Slide master decorative shapes (non-fatal)
      try {
        if (layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            const masterXml = masterCache.get(masterPath)!;
            for (const sp of [
              ...(masterXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
              ...(masterXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
            ]) {
              if (sp.includes("<p:ph")) continue;
              const mShape = pptxParseShapeXml(sp, relsMap);
              if (mShape && (mShape.fill || mShape.gradFill || mShape.imgUrl || mShape.stroke)) {
                slide.shapes.unshift(mShape);
              }
            }
          }
        }
      } catch { /* master shapes non-fatal */ }

      slides.push(slide);
    } catch {
      slides.push({ id: genId(), aspectRatio: `${EDITOR_SLIDE_W}/${EDITOR_SLIDE_H}`, shapes: [] });
    }
  }

  return slides;
}

/** Convert slides to saveable text (markdown-ish) */
function slidesToText(slides: PptxSlide[]): string {
  return slides.map((slide, i) => {
    const header = `--- Slide ${i + 1} ---`;
    const texts = slide.shapes
      .flatMap(s => s.texts.map(t => {
        const prefix = t.bold ? "## " : "";
        return prefix + t.text;
      }))
      .filter(Boolean);
    return header + "\n" + (texts.length > 0 ? texts.join("\n") : "(empty slide)");
  }).join("\n\n");
}

/** Parse text back into slide structures for basic round-trip */
const _EDITOR_ACCENTS = ["#4f7d75", "#4869ac", "#6f4ba8", "#c14a44", "#b27c34", "#437f6b"];

function textToSlides(text: string): PptxSlide[] {
  const slideMarker = /---\s*Slide\s+\d+\s*---/;
  const lines = text.split("\n");

  if (slideMarker.test(text)) {
    // "--- Slide N ---" format (editor round-trip)
    const blocks = text.split(slideMarker).filter(b => b.trim());
    if (blocks.length === 0) return [{ id: genId(), bg: "#ffffff", shapes: [] }];
    return blocks.map((block, i) => {
      const bLines = block.trim().split("\n").filter(l => l.trim() && l.trim() !== "(empty slide)");
      const accent = _EDITOR_ACCENTS[i % _EDITOR_ACCENTS.length];
      const texts: PptxTextRun[] = bLines.map(line => {
        if (line.startsWith("## ")) return { text: line.slice(3), bold: true, fontSize: 32, color: "#1c1917", align: "center" };
        return { text: line.replace(/^[-*]\s+/, ""), fontSize: 18, color: "#57534e", bullet: line.match(/^[-*]\s/) ? "\u2022" : undefined };
      });
      return {
        id: genId(), bg: "#ffffff",
        shapes: [
          { id: genId(), x: 0, y: 0, w: 100, h: 1.5, fill: accent, texts: [] },
          { id: genId(), x: 8, y: 8, w: 84, h: 84, texts },
        ],
      };
    });
  }

  // Markdown format: ## headings split into slides
  let title = "";
  let currentSlide: { title: string; items: string[] } | null = null;
  const slideData: { title: string; items: string[] }[] = [];

  for (const line of lines) {
    if (line.match(/^#\s+/) && !title) { title = line.replace(/^#\s+/, "").trim(); continue; }
    if (line.match(/^##\s+/)) {
      if (currentSlide) slideData.push(currentSlide);
      currentSlide = { title: line.replace(/^##\s+/, "").trim(), items: [] };
      continue;
    }
    if (currentSlide && line.trim()) currentSlide.items.push(line.trim());
    else if (!currentSlide && line.trim() && !line.startsWith("#")) {
      if (!currentSlide) currentSlide = { title: t("page.agent_detail.overview"), items: [] };
      currentSlide.items.push(line.trim());
    }
  }
  if (currentSlide) slideData.push(currentSlide);

  const result: PptxSlide[] = [];

  // Title slide
  if (title || slideData.length === 0) {
    result.push({
      id: genId(), bg: "#1c1917",
      shapes: [
        { id: genId(), x: 0, y: 0, w: 100, h: 100, gradFill: { angle: 135, stops: [{ pos: 0, color: "#1c1917", alpha: 1 }, { pos: 100, color: "#1e3a5f", alpha: 1 }] }, texts: [] },
        { id: genId(), x: 10, y: 30, w: 80, h: 40, texts: [
          { text: title || "Presentation", bold: true, fontSize: 44, color: "#ffffff", align: "center" },
        ]},
      ],
    });
  }

  // Content slides
  for (let i = 0; i < slideData.length; i++) {
    const sd = slideData[i];
    const accent = _EDITOR_ACCENTS[i % _EDITOR_ACCENTS.length];
    const items: PptxTextRun[] = sd.items.map(item => ({
      text: item.replace(/^[-*•]\s*/, "").replace(/^\d+\.\s*/, ""),
      fontSize: 20, color: "#44403c",
      bullet: item.match(/^[-*•]/) ? "\u2022" : undefined,
      indent: item.match(/^[-*•]/) ? 24 : undefined,
    }));
    result.push({
      id: genId(), bg: "#ffffff",
      shapes: [
        { id: genId(), x: 0, y: 0, w: 100, h: 1.2, fill: accent, texts: [] },
        { id: genId(), x: 6, y: 5, w: 88, h: 15, texts: [{ text: sd.title, bold: true, fontSize: 32, color: "#1c1917" }] },
        { id: genId(), x: 8, y: 22, w: 84, h: 72, texts: items },
      ],
    });
  }

  if (result.length === 0) {
    result.push({ id: genId(), bg: "#ffffff", shapes: [{ id: genId(), x: 5, y: 5, w: 90, h: 90, texts: lines.filter(l => l.trim()).map(l => ({ text: l, fontSize: 16, color: "#57534e" })) }] });
  }
  return result;
}

type PptxSlideLayout = "title-body" | "title-only" | "section" | "blank";

const PRESENTATION_FONT_FAMILIES = [
  "Aptos",
  "Arial",
  "Inter",
  "Georgia",
  "Times New Roman",
  "Verdana",
  "Courier New",
];

function createPptxSlide(layout: PptxSlideLayout, aspectRatio = "16/9"): PptxSlide {
  const base = { id: genId(), bg: "#ffffff", aspectRatio };
  if (layout === "blank") return { ...base, shapes: [] };
  if (layout === "title-only") {
    return {
      ...base,
      shapes: [{
        id: genId(), x: 10, y: 28, w: 80, h: 28,
        texts: [{ text: "New Slide Title", bold: true, fontSize: 36, color: "#1c1917", align: "center" }],
      }],
    };
  }
  if (layout === "section") {
    return {
      ...base,
      shapes: [{
        id: genId(), x: 10, y: 28, w: 80, h: 22,
        texts: [{ text: "Section Title", bold: true, fontSize: 38, color: "#1c1917", align: "center" }],
      }, {
        id: genId(), x: 18, y: 54, w: 64, h: 14,
        texts: [{ text: "Section description", fontSize: 18, color: "#78716c", align: "center" }],
      }],
    };
  }
  return {
    ...base,
    shapes: [{
      id: genId(), x: 10, y: 12, w: 80, h: 20,
      texts: [{ text: "New Slide Title", bold: true, fontSize: 32, color: "#1c1917", align: "center" }],
    }, {
      id: genId(), x: 10, y: 38, w: 80, h: 46,
      texts: [{ text: "Click to edit text", fontSize: 18, color: "#57534e" }],
    }],
  };
}

function PptxReadOnlySlide({ slide, serverUrl }: { slide: PptxSlide; serverUrl?: string }) {
  const background: React.CSSProperties = { backgroundColor: slide.bg || "#ffffff" };
  if (slide.bgGrad) background.backgroundImage = pptxGradToCss(slide.bgGrad);
  if (slide.bgImgUrl) {
    background.backgroundImage = `url(${slide.bgImgUrl})`;
    background.backgroundSize = "cover";
    background.backgroundPosition = "center";
  }

  return (
    <div className="presentation-editor-present-slide" style={{ ...background, aspectRatio: slide.aspectRatio || "16/9" }}>
      {serverUrl ? (
        <img src={serverUrl} alt="" className="presentation-editor-present-server-image" />
      ) : slide.shapes.map((shape) => {
        const radius = shape.presetGeom === "ellipse" || shape.presetGeom === "oval"
          ? "50%"
          : shape.presetGeom === "roundRect"
            ? "8%"
            : shape.borderRadius
              ? `${shape.borderRadius}%`
              : 0;
        const transforms: string[] = [];
        if (shape.rotation) transforms.push(`rotate(${shape.rotation}deg)`);
        if (shape.flipH) transforms.push("scaleX(-1)");
        if (shape.flipV) transforms.push("scaleY(-1)");
        const shapeStyle: React.CSSProperties = {
          position: "absolute",
          left: `${shape.x}%`, top: `${shape.y}%`,
          width: `${shape.w}%`, height: `${shape.h}%`,
          boxSizing: "border-box",
          overflow: "hidden",
          opacity: shape.opacity,
          borderRadius: radius,
          background: shape.gradFill ? pptxGradToCss(shape.gradFill) : shape.fill,
          border: shape.stroke ? `${Math.max(0.5, shape.strokeWidth || 1)}px solid ${shape.stroke}` : undefined,
          transform: transforms.length ? transforms.join(" ") : undefined,
          display: "flex",
          flexDirection: "column",
          justifyContent: shape.vAlign === "bottom" ? "flex-end" : shape.vAlign === "middle" ? "center" : "flex-start",
          padding: shape.padding ? `${shape.padding.t}pt ${shape.padding.r}pt ${shape.padding.b}pt ${shape.padding.l}pt` : shape.texts.length ? "2% 3%" : undefined,
        };

        if (shape.type === "table" && shape.tableRows) {
          return (
            <div key={shape.id} style={shapeStyle}>
              <table className="presentation-editor-present-table">
                <tbody>
                  {shape.tableRows.map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      {row.map((cell, cellIndex) => cell.vMerge ? null : (
                        <td key={cellIndex} colSpan={cell.gridSpan} style={{ background: cell.fill, color: cell.color, fontWeight: cell.bold || rowIndex === 0 ? 700 : 400 }}>
                          {cell.text}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }

        return (
          <div key={shape.id} style={shapeStyle}>
            {shape.imgUrl && (
              <img
                src={shape.imgUrl}
                alt=""
                style={{
                  position: "absolute", inset: 0, width: "100%", height: "100%",
                  objectFit: shape.imgCrop ? "fill" : shape.imageFit || "cover",
                  borderRadius: radius,
                }}
              />
            )}
            {shape.texts.map((paragraph, paragraphIndex) => (
              <div key={paragraphIndex} style={{
                position: "relative", zIndex: 1,
                color: paragraph.color || "#000000",
                fontFamily: paragraph.fontFamily ? `"${paragraph.fontFamily}", sans-serif` : undefined,
                fontSize: `${((paragraph.fontSize || 16) / 5.4).toFixed(3)}cqh`,
                fontWeight: paragraph.bold ? 700 : 400,
                fontStyle: paragraph.italic ? "italic" : undefined,
                textDecoration: paragraph.underline ? "underline" : paragraph.strikethrough ? "line-through" : undefined,
                textAlign: (paragraph.align as React.CSSProperties["textAlign"]) || "left",
                lineHeight: paragraph.lineSpacing || 1.2,
                paddingLeft: paragraph.indent ? `${paragraph.indent}px` : paragraph.bullet ? "1.1em" : undefined,
                whiteSpace: "pre-wrap",
              }}>
                {paragraph.bullet && <span className="presentation-editor-present-bullet">{paragraph.bullet}</span>}
                {paragraph.runs?.length ? paragraph.runs.map((run, runIndex) => (
                  <span key={runIndex} style={{
                    color: run.color,
                    fontFamily: run.fontFamily ? `"${run.fontFamily}", sans-serif` : undefined,
                    fontSize: run.fontSize ? `${(run.fontSize / 5.4).toFixed(3)}cqh` : undefined,
                    fontWeight: run.bold ? 700 : undefined,
                    fontStyle: run.italic ? "italic" : undefined,
                    textDecoration: run.underline ? "underline" : run.strikethrough ? "line-through" : undefined,
                  }}>{run.text}</span>
                )) : paragraph.text}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Presentation Editor sub-component
// ---------------------------------------------------------------------------

function PresentationEditor({
  slides: initialSlides,
  serverSlideUrls,
  onChange,
}: {
  slides: PptxSlide[];
  serverSlideUrls?: string[];
  onChange: (slides: PptxSlide[]) => void;
}) {
  const [slides, setSlides] = useState<PptxSlide[]>(initialSlides);
  const [activeIdx, setActiveIdx] = useState(0);
  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState<{ shapeId: string; textIdx: number; value: string; initialValue: string } | null>(null);
  const [editingTableCell, setEditingTableCell] = useState<{ shapeId: string; rowIdx: number; cellIdx: number; value: string; initialValue: string } | null>(null);
  const [dragging, setDragging] = useState<{ shapeId: string; slideIdx: number; startX: number; startY: number; origX: number; origY: number; baseSlides: PptxSlide[] } | null>(null);
  const [resizing, setResizing] = useState<{ shapeId: string; slideIdx: number; handle: string; startX: number; startY: number; origX: number; origY: number; origW: number; origH: number; baseSlides: PptxSlide[] } | null>(null);
  const [undoStack, setUndoStack] = useState<PptxSlide[][]>([]);
  const [redoStack, setRedoStack] = useState<PptxSlide[][]>([]);
  const [dragThumbIdx, setDragThumbIdx] = useState<number | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);
  const [useServerBg, setUseServerBg] = useState(false);
  const [canvasZoom, setCanvasZoom] = useState(1);
  const [showFormatOptions, setShowFormatOptions] = useState(false);
  const [presentingIdx, setPresentingIdx] = useState<number | null>(null);
  const [notesDraft, setNotesDraft] = useState("");
  const [shapeContextMenu, setShapeContextMenu] = useState<{ shapeId: string; x: number; y: number } | null>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const replaceImageShapeIdRef = useRef<string | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const canvasViewportRef = useRef<HTMLDivElement>(null);
  const slidesRef = useRef(slides);
  const copiedShapeRef = useRef<PptxShape | null>(null);
  const [canvasViewportSize, setCanvasViewportSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    slidesRef.current = slides;
  }, [slides]);

  useEffect(() => {
    if (!selectedShapeId) setShowFormatOptions(false);
  }, [selectedShapeId]);

  useEffect(() => {
    const element = canvasViewportRef.current;
    if (!element) return undefined;
    const update = () => {
      setCanvasViewportSize((size) => {
        const next = { width: element.clientWidth, height: element.clientHeight };
        return size.width === next.width && size.height === next.height ? size : next;
      });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (initialSlides === slidesRef.current) return;
    slidesRef.current = initialSlides;
    setSlides(initialSlides);
    setActiveIdx((index) => Math.max(0, Math.min(index, initialSlides.length - 1)));
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
    setUndoStack([]);
    setRedoStack([]);
  }, [initialSlides]);

  const pushUndo = useCallback((prev: PptxSlide[]) => {
    setUndoStack(s => [...s.slice(-29), prev]);
    setRedoStack([]);
  }, []);

  const updateSlides = useCallback((next: PptxSlide[], skipUndo = false) => {
    if (!skipUndo) pushUndo(slides);
    setSlides(next);
    onChange(next);
  }, [onChange, slides, pushUndo]);

  const undo = useCallback(() => {
    if (undoStack.length === 0) return;
    const prev = undoStack[undoStack.length - 1];
    setRedoStack(s => [...s, slides]);
    setUndoStack(s => s.slice(0, -1));
    setSlides(prev);
    onChange(prev);
  }, [undoStack, slides, onChange]);

  const redo = useCallback(() => {
    if (redoStack.length === 0) return;
    const next = redoStack[redoStack.length - 1];
    setUndoStack(s => [...s, slides]);
    setRedoStack(s => s.slice(0, -1));
    setSlides(next);
    onChange(next);
  }, [redoStack, slides, onChange]);

  const activeSlide = slides[activeIdx] || { id: "empty", bg: "#ffffff", aspectRatio: "16/9", shapes: [] };
  useEffect(() => {
    setNotesDraft(activeSlide.notes || "");
  }, [activeSlide.id, activeSlide.notes]);

  const commitSpeakerNotes = useCallback(() => {
    if ((activeSlide.notes || "") === notesDraft) return;
    const next = slides.map((slide, index) => index === activeIdx ? { ...slide, notes: notesDraft } : slide);
    updateSlides(next);
  }, [activeIdx, activeSlide.notes, notesDraft, slides, updateSlides]);

  useEffect(() => {
    if (!shapeContextMenu) return undefined;
    const close = () => setShapeContextMenu(null);
    window.addEventListener("mousedown", close);
    window.addEventListener("blur", close);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("blur", close);
      window.removeEventListener("resize", close);
    };
  }, [shapeContextMenu]);

  useEffect(() => {
    if (presentingIdx === null) return undefined;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setPresentingIdx(null);
        return;
      }
      if (event.key === "ArrowRight" || event.key === "ArrowDown" || event.key === " " || event.key === "PageDown") {
        event.preventDefault();
        setPresentingIdx((index) => index === null ? 0 : Math.min(slides.length - 1, index + 1));
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp" || event.key === "PageUp") {
        event.preventDefault();
        setPresentingIdx((index) => index === null ? 0 : Math.max(0, index - 1));
      } else if (event.key === "Home") {
        event.preventDefault();
        setPresentingIdx(0);
      } else if (event.key === "End") {
        event.preventDefault();
        setPresentingIdx(Math.max(0, slides.length - 1));
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [presentingIdx, slides.length]);

  const activeSlideAspect = useMemo(() => {
    const [rawW, rawH] = (activeSlide.aspectRatio || "16/9").split("/").map((part) => Number(part.trim()));
    const ratio = rawW > 0 && rawH > 0 ? rawW / rawH : 16 / 9;
    return Number.isFinite(ratio) && ratio > 0 ? ratio : 16 / 9;
  }, [activeSlide.aspectRatio]);
  const slideFrameSize = useMemo(() => {
    if (!canvasViewportSize.width || !canvasViewportSize.height) {
      return { width: 960, height: Math.round(960 / activeSlideAspect) };
    }
    const horizontalPadding = 48;
    const verticalPadding = 48;
    const availableWidth = Math.max(240, canvasViewportSize.width - horizontalPadding);
    const availableHeight = Math.max(135, canvasViewportSize.height - verticalPadding);
    const width = Math.max(240, Math.floor(Math.min(availableWidth, availableHeight * activeSlideAspect)));
    return { width, height: Math.max(135, Math.round(width / activeSlideAspect)) };
  }, [activeSlideAspect, canvasViewportSize.height, canvasViewportSize.width]);

  const addSlide = useCallback((layout: PptxSlideLayout = "title-body") => {
    const newSlide = createPptxSlide(layout, activeSlide.aspectRatio || "16/9");
    const next = [...slides, newSlide];
    updateSlides(next);
    setActiveIdx(next.length - 1);
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
  }, [activeSlide.aspectRatio, slides, updateSlides]);

  const deleteSlide = useCallback(() => {
    if (slides.length <= 1) return;
    const next = slides.filter((_, i) => i !== activeIdx);
    updateSlides(next);
    setActiveIdx(Math.min(activeIdx, next.length - 1));
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
  }, [slides, activeIdx, updateSlides]);

  const addTextBox = useCallback(() => {
    const shapeId = genId();
    const text = "New text box";
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return {
        ...s,
        shapes: [...s.shapes, {
          id: shapeId,
          x: 15, y: 50, w: 70, h: 15,
          texts: [{ text, fontSize: 16, color: "#57534e" }],
        }],
      };
    });
    updateSlides(next);
    setSelectedShapeId(shapeId);
    setEditingText({ shapeId, textIdx: 0, value: text, initialValue: text });
  }, [slides, activeIdx, updateSlides]);

  const deleteShape = useCallback((shapeId: string) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return { ...s, shapes: s.shapes.filter(sh => sh.id !== shapeId) };
    });
    updateSlides(next);
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
  }, [slides, activeIdx, updateSlides]);

  // Keyboard shortcuts: undo/redo, delete shape, and nudge selected shapes.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (presentingIdx !== null) return;
      if (isEditableDomTarget(e.target)) return;
      if ((e.metaKey || e.ctrlKey) && e.key === "z" && !e.shiftKey) { e.preventDefault(); undo(); return; }
      if ((e.metaKey || e.ctrlKey) && e.key === "z" && e.shiftKey) { e.preventDefault(); redo(); return; }
      if (e.key === "Escape") {
        setSelectedShapeId(null);
        setEditingText(null);
        setEditingTableCell(null);
        return;
      }
      if (!selectedShapeId || editingText || editingTableCell) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteShape(selectedShapeId);
        return;
      }
      const delta = e.shiftKey ? 2 : 0.5;
      const movement: Record<string, { dx: number; dy: number }> = {
        ArrowLeft: { dx: -delta, dy: 0 },
        ArrowRight: { dx: delta, dy: 0 },
        ArrowUp: { dx: 0, dy: -delta },
        ArrowDown: { dx: 0, dy: delta },
      };
      const move = movement[e.key];
      if (!move) return;
      e.preventDefault();
      const next = slides.map((slide, index) => {
        if (index !== activeIdx) return slide;
        return {
          ...slide,
          shapes: slide.shapes.map((shape) => {
            if (shape.id !== selectedShapeId) return shape;
            return {
              ...shape,
              x: Math.max(0, Math.min(100 - shape.w, shape.x + move.dx)),
              y: Math.max(0, Math.min(100 - shape.h, shape.y + move.dy)),
            };
          }),
        };
      });
      updateSlides(next);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [activeIdx, deleteShape, editingTableCell, editingText, presentingIdx, redo, selectedShapeId, slides, undo, updateSlides]);

  const updateShapeText = useCallback((shapeId: string, textIdx: number, newText: string) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return {
        ...s,
        shapes: s.shapes.map(sh => {
          if (sh.id !== shapeId) return sh;
          const texts = [...sh.texts];
          texts[textIdx] = { ...texts[textIdx], text: newText };
          return { ...sh, texts };
        }),
      };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const beginTextEditing = useCallback((shapeId: string, textIdx: number, value: string) => {
    setSelectedShapeId(shapeId);
    setEditingTableCell(null);
    setEditingText({ shapeId, textIdx, value, initialValue: value });
  }, []);

  const commitTextEditing = useCallback(() => {
    if (!editingText) return;
    if (editingText.value !== editingText.initialValue) {
      updateShapeText(editingText.shapeId, editingText.textIdx, editingText.value);
    }
    setEditingText(null);
  }, [editingText, updateShapeText]);

  const cancelTextEditing = useCallback(() => setEditingText(null), []);

  const updateTableCell = useCallback((shapeId: string, rowIdx: number, cellIdx: number, value: string) => {
    const next = slides.map((slide, slideIdx) => {
      if (slideIdx !== activeIdx) return slide;
      return {
        ...slide,
        shapes: slide.shapes.map((shape) => {
          if (shape.id !== shapeId || !shape.tableRows) return shape;
          return {
            ...shape,
            tableRows: shape.tableRows.map((row, currentRowIdx) => currentRowIdx !== rowIdx
              ? row
              : row.map((cell, currentCellIdx) => currentCellIdx === cellIdx ? { ...cell, text: value } : cell)),
          };
        }),
      };
    });
    updateSlides(next);
  }, [activeIdx, slides, updateSlides]);

  const commitTableCellEditing = useCallback(() => {
    if (!editingTableCell) return;
    if (editingTableCell.value !== editingTableCell.initialValue) {
      updateTableCell(editingTableCell.shapeId, editingTableCell.rowIdx, editingTableCell.cellIdx, editingTableCell.value);
    }
    setEditingTableCell(null);
  }, [editingTableCell, updateTableCell]);

  const updateSlideBg = useCallback((color: string) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return { ...s, bg: color, bgGrad: undefined };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const addShape = useCallback((preset: string) => {
    const shapeMap: Record<string, Partial<PptxShape>> = {
      rect: { x: 20, y: 30, w: 25, h: 20, fill: "#4472c4", presetGeom: "rect" },
      roundRect: { x: 20, y: 30, w: 25, h: 20, fill: "#ed7d31", presetGeom: "roundRect", borderRadius: 8 },
      ellipse: { x: 25, y: 30, w: 20, h: 25, fill: "#a5a5a5", presetGeom: "ellipse" },
      triangle: { x: 25, y: 30, w: 20, h: 20, fill: "#ffc000", presetGeom: "triangle" },
      line: { x: 15, y: 50, w: 70, h: 0.5, fill: "#57534e", presetGeom: "line", stroke: "#57534e", strokeWidth: 2 },
    };
    const base = shapeMap[preset] || shapeMap.rect;
    const shapeId = genId();
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return { ...s, shapes: [...s.shapes, { id: shapeId, type: "shape" as const, texts: [], ...base } as PptxShape] };
    });
    updateSlides(next);
    setSelectedShapeId(shapeId);
    setEditingText(null);
    setEditingTableCell(null);
  }, [slides, activeIdx, updateSlides]);

  const handleImageFile = useCallback((file: File, replaceShapeId: string | null = null) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      if (!url) return;
      const img = new Image();
      img.onload = () => {
        const aspect = img.width / img.height || 1;
        const w = 40;
        const h = Math.min(w / aspect, 60);
        const shapeId = replaceShapeId || genId();
        const next = slides.map((s, i) => {
          if (i !== activeIdx) return s;
          if (replaceShapeId) {
            return {
              ...s,
              shapes: s.shapes.map((shape) => shape.id === replaceShapeId
                ? { ...shape, type: "image" as const, imgUrl: url, imgCrop: undefined, imageFit: shape.imageFit || "contain" as const }
                : shape),
            };
          }
          return { ...s, shapes: [...s.shapes, { id: shapeId, type: "image" as const, x: 30, y: 20, w, h, imgUrl: url, imageFit: "contain" as const, texts: [] }] };
        });
        updateSlides(next);
        setSelectedShapeId(shapeId);
        setEditingText(null);
        setEditingTableCell(null);
      };
      img.src = url;
    };
    reader.readAsDataURL(file);
  }, [slides, activeIdx, updateSlides]);

  const requestImageInsert = useCallback(() => {
    replaceImageShapeIdRef.current = null;
    imageInputRef.current?.click();
  }, []);

  const requestImageReplacement = useCallback((shapeId: string) => {
    replaceImageShapeIdRef.current = shapeId;
    imageInputRef.current?.click();
  }, []);

  const moveShapeZ = useCallback((shapeId: string, dir: "up" | "down" | "top" | "bottom") => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      const shapes = [...s.shapes];
      const idx = shapes.findIndex(sh => sh.id === shapeId);
      if (idx < 0) return s;
      const [item] = shapes.splice(idx, 1);
      if (dir === "up" && idx < shapes.length) shapes.splice(idx + 1, 0, item);
      else if (dir === "down" && idx > 0) shapes.splice(idx - 1, 0, item);
      else if (dir === "top") shapes.push(item);
      else if (dir === "bottom") shapes.unshift(item);
      else shapes.splice(idx, 0, item);
      return { ...s, shapes };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const duplicateSlide = useCallback(() => {
    const src = slides[activeIdx];
    if (!src) return;
    const dup: PptxSlide = {
      ...(JSON.parse(JSON.stringify(src)) as PptxSlide),
      id: genId(),
      shapes: src.shapes.map((shape) => clonePptxShape(shape)),
    };
    const next = [...slides.slice(0, activeIdx + 1), dup, ...slides.slice(activeIdx + 1)];
    updateSlides(next);
    setActiveIdx(activeIdx + 1);
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
  }, [slides, activeIdx, updateSlides]);

  const duplicateShape = useCallback((shapeId: string) => {
    const source = activeSlide.shapes.find((shape) => shape.id === shapeId);
    if (!source) return;
    const duplicate = clonePptxShape(source, 2);
    const next = slides.map((slide, index) => index === activeIdx
      ? { ...slide, shapes: [...slide.shapes, duplicate] }
      : slide);
    updateSlides(next);
    setSelectedShapeId(duplicate.id);
    setEditingText(null);
    setEditingTableCell(null);
  }, [activeIdx, activeSlide.shapes, slides, updateSlides]);

  const copyShape = useCallback((shapeId: string) => {
    const source = activeSlide.shapes.find((shape) => shape.id === shapeId);
    if (source) copiedShapeRef.current = JSON.parse(JSON.stringify(source)) as PptxShape;
  }, [activeSlide.shapes]);

  const pasteShape = useCallback(() => {
    if (!copiedShapeRef.current) return;
    const duplicate = clonePptxShape(copiedShapeRef.current, 2);
    const next = slides.map((slide, index) => index === activeIdx
      ? { ...slide, shapes: [...slide.shapes, duplicate] }
      : slide);
    updateSlides(next);
    copiedShapeRef.current = JSON.parse(JSON.stringify(duplicate)) as PptxShape;
    setSelectedShapeId(duplicate.id);
    setEditingText(null);
    setEditingTableCell(null);
  }, [activeIdx, slides, updateSlides]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (presentingIdx !== null) return;
      if (isEditableDomTarget(event.target)) return;
      const command = event.metaKey || event.ctrlKey;
      if (command && event.key.toLowerCase() === "c" && selectedShapeId) {
        event.preventDefault();
        copyShape(selectedShapeId);
        return;
      }
      if (command && event.key.toLowerCase() === "d" && selectedShapeId) {
        event.preventDefault();
        duplicateShape(selectedShapeId);
        return;
      }
      if (command && event.key.toLowerCase() === "v" && copiedShapeRef.current) {
        event.preventDefault();
        pasteShape();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [copyShape, duplicateShape, pasteShape, presentingIdx, selectedShapeId]);

  const updateShapeFill = useCallback((shapeId: string, fill: string) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return { ...s, shapes: s.shapes.map(sh => sh.id === shapeId ? { ...sh, fill, gradFill: undefined } : sh) };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const updateShapeProps = useCallback((shapeId: string, update: Partial<PptxShape>) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return {
        ...s,
        shapes: s.shapes.map((shape) => {
          if (shape.id !== shapeId) return shape;
          const merged = { ...shape, ...update };
          const minHeight = merged.presetGeom === "line" ? 0.5 : 1;
          merged.w = Math.max(1, Math.min(100, merged.w));
          merged.h = Math.max(minHeight, Math.min(100, merged.h));
          merged.x = Math.max(0, Math.min(100 - merged.w, merged.x));
          merged.y = Math.max(0, Math.min(100 - merged.h, merged.y));
          if (merged.opacity != null) merged.opacity = Math.max(0, Math.min(1, merged.opacity));
          return merged;
        }),
      };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const updateTextStyle = useCallback((shapeId: string, textIdx: number, update: Partial<PptxTextRun>) => {
    const inlineKeys = ["bold", "italic", "underline", "strikethrough", "fontSize", "color", "fontFamily"] as const;
    const runUpdate = inlineKeys.reduce((result, key) => {
      if (key in update) result[key] = update[key] as never;
      return result;
    }, {} as Partial<NonNullable<PptxTextRun["runs"]>[number]>);
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return {
        ...s,
        shapes: s.shapes.map(sh => {
          if (sh.id !== shapeId) return sh;
          const texts = sh.texts.map((text, ti) => {
            if (ti !== textIdx) return text;
            return {
              ...text,
              ...update,
              runs: text.runs?.length && Object.keys(runUpdate).length
                ? text.runs.map((run) => ({ ...run, ...runUpdate }))
                : text.runs,
            };
          });
          return { ...sh, texts };
        }),
      };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const handleDragStart = useCallback((e: React.MouseEvent, shapeId: string) => {
    if (editingText || editingTableCell) return;
    e.stopPropagation();
    const shape = activeSlide.shapes.find(s => s.id === shapeId);
    if (!shape) return;
    setDragging({ shapeId, slideIdx: activeIdx, startX: e.clientX, startY: e.clientY, origX: shape.x, origY: shape.y, baseSlides: slides });
    setSelectedShapeId(shapeId);
  }, [activeIdx, activeSlide, editingTableCell, editingText, slides]);

  useEffect(() => {
    if (!dragging) return;
    const handleMove = (e: MouseEvent) => {
      if (!canvasRef.current) return;
      const rect = canvasRef.current.getBoundingClientRect();
      const dx = ((e.clientX - dragging.startX) / rect.width) * 100;
      const dy = ((e.clientY - dragging.startY) / rect.height) * 100;
      const next = dragging.baseSlides.map((s, i) => {
        if (i !== dragging.slideIdx) return s;
        return {
          ...s,
          shapes: s.shapes.map(sh => sh.id === dragging.shapeId
            ? { ...sh, x: Math.max(0, Math.min(100 - sh.w, dragging.origX + dx)), y: Math.max(0, Math.min(100 - sh.h, dragging.origY + dy)) }
            : sh),
        };
      });
      slidesRef.current = next;
      setSlides(next);
    };
    const handleUp = () => {
      if (slidesRef.current !== dragging.baseSlides) {
        pushUndo(dragging.baseSlides);
        onChange(slidesRef.current);
      }
      setDragging(null);
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => { window.removeEventListener("mousemove", handleMove); window.removeEventListener("mouseup", handleUp); };
  }, [dragging, onChange, pushUndo]);

  // Resize handler
  const handleResizeStart = useCallback((e: React.MouseEvent, shapeId: string, handle: string) => {
    e.stopPropagation();
    e.preventDefault();
    const shape = activeSlide.shapes.find(s => s.id === shapeId);
    if (!shape) return;
    setResizing({ shapeId, slideIdx: activeIdx, handle, startX: e.clientX, startY: e.clientY, origX: shape.x, origY: shape.y, origW: shape.w, origH: shape.h, baseSlides: slides });
  }, [activeIdx, activeSlide, slides]);

  useEffect(() => {
    if (!resizing) return;
    const handleMove = (e: MouseEvent) => {
      if (!canvasRef.current) return;
      const rect = canvasRef.current.getBoundingClientRect();
      const dx = ((e.clientX - resizing.startX) / rect.width) * 100;
      const dy = ((e.clientY - resizing.startY) / rect.height) * 100;
      const h = resizing.handle;
      const next = resizing.baseSlides.map((s, i) => {
        if (i !== resizing.slideIdx) return s;
        return {
          ...s,
            shapes: s.shapes.map(sh => {
              if (sh.id !== resizing.shapeId) return sh;
              const minW = sh.presetGeom === "line" ? 1 : 3;
              const minH = sh.presetGeom === "line" ? 0.5 : 3;
              let { x, y, w, h: height } = { x: resizing.origX, y: resizing.origY, w: resizing.origW, h: resizing.origH };
              if (h.includes("e")) w = resizing.origW + dx;
              if (h.includes("w")) {
                const right = resizing.origX + resizing.origW;
                x = Math.min(right - minW, resizing.origX + dx);
                w = right - x;
              }
              if (h.includes("s")) height = resizing.origH + dy;
              if (h.includes("n")) {
                const bottom = resizing.origY + resizing.origH;
                y = Math.min(bottom - minH, resizing.origY + dy);
                height = bottom - y;
              }
              x = Math.max(0, Math.min(100 - minW, x));
              y = Math.max(0, Math.min(100 - minH, y));
              w = Math.max(minW, Math.min(w, 100 - x));
              height = Math.max(minH, Math.min(height, 100 - y));
              return { ...sh, x, y, w, h: height };
            }),
          };
        });
      slidesRef.current = next;
      setSlides(next);
    };
    const handleUp = () => {
      if (slidesRef.current !== resizing.baseSlides) {
        pushUndo(resizing.baseSlides);
        onChange(slidesRef.current);
      }
      setResizing(null);
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => { window.removeEventListener("mousemove", handleMove); window.removeEventListener("mouseup", handleUp); };
  }, [resizing, onChange, pushUndo]);

  // Slide reorder via drag
  const handleThumbDragStart = useCallback((idx: number) => setDragThumbIdx(idx), []);
  const handleThumbDragOver = useCallback((e: React.DragEvent, idx: number) => { e.preventDefault(); setDragOverIdx(idx); }, []);
  const handleThumbDrop = useCallback((idx: number) => {
    if (dragThumbIdx === null || dragThumbIdx === idx) { setDragThumbIdx(null); setDragOverIdx(null); return; }
    const next = [...slides];
    const [moved] = next.splice(dragThumbIdx, 1);
    next.splice(idx, 0, moved);
    updateSlides(next);
    setActiveIdx(idx);
    setDragThumbIdx(null);
    setDragOverIdx(null);
  }, [dragThumbIdx, slides, updateSlides]);

  const openShapeContextMenu = useCallback((event: React.MouseEvent, shapeId: string) => {
    event.preventDefault();
    event.stopPropagation();
    setSelectedShapeId(shapeId);
    setEditingText(null);
    setEditingTableCell(null);
    setShapeContextMenu({
      shapeId,
      x: Math.max(8, Math.min(window.innerWidth - 220, event.clientX)),
      y: Math.max(8, Math.min(window.innerHeight - 300, event.clientY)),
    });
  }, []);

  const selectedShape = activeSlide.shapes.find(s => s.id === selectedShapeId);
  const contextMenuShape = shapeContextMenu
    ? activeSlide.shapes.find((shape) => shape.id === shapeContextMenu.shapeId)
    : undefined;
  const selectedTextIndex = selectedShape
    ? (editingText?.shapeId === selectedShape.id ? editingText.textIdx : selectedShape.texts.length > 0 ? 0 : null)
    : null;
  const selectedText = selectedShape && selectedTextIndex !== null ? selectedShape.texts[selectedTextIndex] : undefined;
  const updateSelectedTextStyle = useCallback((update: Partial<PptxTextRun>) => {
    if (!selectedShape || selectedTextIndex === null) return;
    updateTextStyle(selectedShape.id, selectedTextIndex, update);
  }, [selectedShape, selectedTextIndex, updateTextStyle]);

  // Build slide background style — layer: solid color < gradient < image
  const hasServerBg = useServerBg && serverSlideUrls && serverSlideUrls.length > activeIdx;
  const slideBg: React.CSSProperties = { backgroundColor: activeSlide.bg || "#ffffff" };
  if (activeSlide.bgGrad) slideBg.backgroundImage = pptxGradToCss(activeSlide.bgGrad);
  if (activeSlide.bgImgUrl) {
    slideBg.backgroundImage = `url(${activeSlide.bgImgUrl})`;
    slideBg.backgroundSize = "cover";
    slideBg.backgroundPosition = "center";
    slideBg.backgroundRepeat = "no-repeat";
  }
  const renderedSlideFrameSize = {
    width: Math.max(160, Math.round(slideFrameSize.width * canvasZoom)),
    height: Math.max(90, Math.round(slideFrameSize.height * canvasZoom)),
  };

  const renderResizeHandles = (shapeId: string) => (
    <>
      {["n", "s", "e", "w", "ne", "nw", "se", "sw"].map((handle) => {
        const pos: React.CSSProperties = {};
        if (handle.includes("n")) pos.top = -5;
        if (handle.includes("s")) pos.bottom = -5;
        if (handle.includes("e")) pos.right = -5;
        if (handle.includes("w")) pos.left = -5;
        if (handle === "n" || handle === "s") { pos.left = "50%"; pos.marginLeft = -5; }
        if (handle === "e" || handle === "w") { pos.top = "50%"; pos.marginTop = -5; }
        const cursors: Record<string, string> = { n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize", ne: "nesw-resize", sw: "nesw-resize", nw: "nwse-resize", se: "nwse-resize" };
        return (
          <div
            key={handle}
            className="presentation-editor-resize-handle"
            onMouseDown={(event) => handleResizeStart(event, shapeId, handle)}
            style={{ cursor: cursors[handle], ...pos }}
          />
        );
      })}
    </>
  );

  return (
    <div className="presentation-editor" style={{ flex: 1, display: "flex", overflow: "hidden" }}>
      {/* Slide thumbnails */}
      <div className="presentation-editor-sidebar" style={{
        width: 180, flexShrink: 0, borderRight: "1px solid rgba(28,25,23,0.06)",
        background: "rgba(250,250,249,0.8)", overflowY: "auto", padding: 12,
        display: "flex", flexDirection: "column", gap: 8,
      }}>
        {slides.map((slide, idx) => {
          const hasThumbServer = useServerBg && serverSlideUrls && serverSlideUrls.length > idx;
          const thumbBg: React.CSSProperties = { backgroundColor: slide.bg || "#ffffff" };
          if (slide.bgGrad) thumbBg.backgroundImage = pptxGradToCss(slide.bgGrad);
          if (slide.bgImgUrl) {
            thumbBg.backgroundImage = `url(${slide.bgImgUrl})`;
            thumbBg.backgroundSize = "cover";
            thumbBg.backgroundRepeat = "no-repeat";
          }
          return (
            <div
              key={slide.id}
              className={`presentation-editor-thumb${idx === activeIdx ? " is-active" : ""}${dragOverIdx === idx ? " is-drag-over" : ""}`}
              draggable
              onClick={() => { setActiveIdx(idx); setSelectedShapeId(null); setEditingText(null); setEditingTableCell(null); }}
              onDragStart={() => handleThumbDragStart(idx)}
              onDragOver={(e) => handleThumbDragOver(e, idx)}
              onDrop={() => handleThumbDrop(idx)}
              onDragEnd={() => { setDragThumbIdx(null); setDragOverIdx(null); }}
              style={{
                cursor: "grab",
                borderRadius: 8,
                border: idx === activeIdx ? "2px solid #4f7d75" : dragOverIdx === idx ? "2px solid #8aa9d1" : "2px solid transparent",
                overflow: "hidden",
                opacity: dragThumbIdx === idx ? 0.5 : 1,
                transition: "border-color 0.15s, opacity 0.15s",
              }}
            >
              <div style={{
                aspectRatio: slide.aspectRatio || "16/9", position: "relative", ...thumbBg,
                borderRadius: 6, overflow: "hidden",
              }}>
                {hasThumbServer && (
                  <img src={serverSlideUrls![idx]} alt="" style={{
                    position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
                    objectFit: "contain", zIndex: 0,
                  }} />
                )}
                {!hasThumbServer && slide.shapes.map((shape) => {
                  const sBg: React.CSSProperties = {};
                  if (shape.gradFill) sBg.background = pptxGradToCss(shape.gradFill);
                  else if (shape.fill) sBg.background = shape.fill;
                  let tBr: string | number | undefined = shape.borderRadius ? `${shape.borderRadius}%` : undefined;
                  if (shape.presetGeom === "ellipse" || shape.presetGeom === "oval") tBr = "50%";
                  return (
                    <div key={shape.id} style={{
                      position: "absolute",
                      left: `${shape.x}%`, top: `${shape.y}%`,
                      width: `${shape.w}%`, height: `${shape.h}%`,
                      overflow: "hidden",
                      ...sBg,
                      borderRadius: tBr,
                      opacity: shape.opacity,
                      border: shape.stroke ? `1px solid ${shape.stroke}` : undefined,
                      transform: shape.rotation ? `rotate(${shape.rotation}deg)` : undefined,
                    }}>
                      {shape.imgUrl && <img src={shape.imgUrl} alt="" style={{ width: "100%", height: "100%", objectFit: shape.imageFit || "cover", position: "absolute", top: 0, left: 0, zIndex: 0 }} />}
                      {shape.type === "table" && shape.tableRows && (
                        <div style={{ width: "100%", height: "100%", background: "#fafaf9", display: "flex", alignItems: "center", justifyContent: "center" }}>
                          <span style={{ fontSize: 4, color: "#a8a29e" }}>{t("page.doc_editor.table")}</span>
                        </div>
                      )}
                      {shape.texts.map((t, ti) => (
                        <div key={ti} style={{
                          position: "relative",
                          fontSize: Math.max(4, (t.fontSize || 16) * 0.2),
                          fontWeight: t.bold ? 700 : 400,
                          color: t.color || "#000",
                          textAlign: (t.align as any) || "left",
                          lineHeight: 1.2,
                          overflow: "hidden",
                          whiteSpace: "nowrap",
                          textOverflow: "ellipsis",
                        }}>
                          {t.text}
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>
              <div className="presentation-editor-thumb-label" style={{ fontSize: 10, textAlign: "center", color: "#78716c", padding: "4px 0" }}>
                {idx + 1}
              </div>
            </div>
          );
        })}
        <div className="presentation-editor-add-slide-controls">
          <button type="button" onClick={() => addSlide("title-body")} className="presentation-editor-add-slide">
            {t("page.doc_editor.plus_add_slide")}
          </button>
          <select
            value=""
            aria-label={t("page.doc_editor.new_slide_layout")}
            onChange={(event) => addSlide(event.target.value as PptxSlideLayout)}
          >
            <option value="" disabled>{t("page.doc_editor.layout")}</option>
            <option value="title-body">{t("page.doc_editor.layout_title_body")}</option>
            <option value="title-only">{t("page.doc_editor.layout_title_only")}</option>
            <option value="section">{t("page.doc_editor.layout_section")}</option>
            <option value="blank">{t("page.doc_editor.layout_blank")}</option>
          </select>
        </div>
      </div>

      {/* Main slide canvas */}
      <div className="presentation-editor-main" style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* Toolbar */}
        <div className="presentation-editor-toolbar" style={{
          display: "flex", alignItems: "center", gap: 8, padding: "8px 16px",
          borderBottom: "1px solid rgba(28,25,23,0.06)", background: "rgba(255,255,255,0.5)",
          flexShrink: 0, flexWrap: "nowrap", minHeight: 46,
        }}>
          {/* Undo / Redo */}
          <button onClick={undo} disabled={undoStack.length === 0} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 8px", opacity: undoStack.length === 0 ? 0.3 : 1 }} title={t("page.doc_editor.undo_ctrl_plus_z")}>
            <IconUndo size={14} />
          </button>
          <button onClick={redo} disabled={redoStack.length === 0} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 8px", opacity: redoStack.length === 0 ? 0.3 : 1 }} title={t("page.doc_editor.redo_ctrl_plus_shift_plus_z")}>
            <IconRedo size={14} />
          </button>
          <div className="presentation-editor-separator" style={{ width: 1, height: 20, background: "#e7e5e4" }} />
          {/* Insert tools */}
          <button onClick={addTextBox} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 12px" }}>
            {t("page.doc_editor.plus_text")}
          </button>
          <button onClick={() => addShape("rect")} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 8px" }} title={t("page.doc_editor.rectangle")}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><rect x="3" y="3" width="18" height="18" rx="2"/></svg>
          </button>
          <button onClick={() => addShape("ellipse")} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 8px" }} title={t("page.doc_editor.circle")}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><circle cx="12" cy="12" r="9"/></svg>
          </button>
          <button onClick={() => addShape("line")} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 8px" }} title={t("page.doc_editor.line")}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><line x1="4" y1="20" x2="20" y2="4"/></svg>
          </button>
          <button onClick={requestImageInsert} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 12px" }}>
            <IconImage size={14} /> {t("page.doc_editor.plus_image").replace(/^\+\s*/, "")}
          </button>
          <input
            ref={imageInputRef}
            type="file"
            accept="image/*"
            style={{ display: "none" }}
            onChange={(event) => {
              const file = event.target.files?.[0];
              const replaceShapeId = replaceImageShapeIdRef.current;
              replaceImageShapeIdRef.current = null;
              if (file) handleImageFile(file, replaceShapeId);
              event.target.value = "";
            }}
          />
          <button onClick={duplicateSlide} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 12px" }} title={t("page.doc_editor.duplicate_slide")}>
            {t("page.doc_editor.duplicate")}
          </button>
          {selectedShapeId && (
            <div className="presentation-editor-context-tools">
              <span className="presentation-editor-context-title" title={t("page.doc_editor.object")}><IconLayers size={14} /></span>
              <span className="presentation-editor-field-label" style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.doc_editor.fill")}</span>
              <input
                type="color"
                value={selectedShape?.fill || "#ffffff"}
                onChange={(e) => updateShapeFill(selectedShapeId, e.target.value)}
                style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
              />
              <span className="presentation-editor-field-label" style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.doc_editor.stroke")}</span>
              <input
                type="color"
                value={selectedShape?.stroke || "#1c1917"}
                onChange={(e) => updateShapeProps(selectedShapeId, { stroke: e.target.value })}
                style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
              />
              <input
                type="number"
                min={0}
                max={20}
                step={0.5}
                value={selectedShape?.strokeWidth ?? 0}
                onChange={(e) => updateShapeProps(selectedShapeId, { strokeWidth: Math.max(0, Number(e.target.value) || 0), stroke: selectedShape?.stroke || "#1c1917" })}
                title={t("page.doc_editor.stroke")}
                style={{ width: 48, fontSize: 12, padding: "2px 4px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4 }}
              />
              {selectedShape?.imgUrl && (
                <select
                  value={selectedShape.imageFit || "cover"}
                  onChange={(e) => updateShapeProps(selectedShapeId, { imageFit: e.target.value as "cover" | "contain" })}
                  style={{ fontSize: 12, padding: "3px 8px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 6, background: "#fff" }}
                >
                  <option value="contain">{t("page.doc_editor.fit_image")}</option>
                  <option value="cover">{t("page.doc_editor.fill_crop")}</option>
                </select>
              )}
              {/* Z-order */}
              <button onClick={() => moveShapeZ(selectedShapeId, "up")} className="btn-manor-ghost" style={{ fontSize: 12, padding: "2px 6px" }} title={t("page.doc_editor.bring_forward")}>
                <IconArrowUp size={13} />
              </button>
              <button onClick={() => moveShapeZ(selectedShapeId, "down")} className="btn-manor-ghost" style={{ fontSize: 12, padding: "2px 6px" }} title={t("page.doc_editor.send_backward")}>
                <IconArrowDown size={13} />
              </button>
              {selectedText && (
                <>
                  <div className="presentation-editor-separator" style={{ width: 1, height: 20, background: "#e7e5e4" }} />
                  <select
                    value={selectedText.fontFamily || "Aptos"}
                    aria-label={t("page.doc_editor.font")}
                    title={t("page.doc_editor.font")}
                    onChange={(event) => updateSelectedTextStyle({ fontFamily: event.target.value })}
                    style={{ width: 108, fontSize: 12, padding: "3px 7px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 6, background: "#fff" }}
                  >
                    {selectedText.fontFamily && !PRESENTATION_FONT_FAMILIES.includes(selectedText.fontFamily) && (
                      <option value={selectedText.fontFamily}>{selectedText.fontFamily}</option>
                    )}
                    {PRESENTATION_FONT_FAMILIES.map((font) => <option key={font} value={font}>{font}</option>)}
                  </select>
                  <input
                    type="number"
                    min={8}
                    max={120}
                    value={selectedText.fontSize || 16}
                    aria-label={t("page.doc_editor.size")}
                    title={t("page.doc_editor.size")}
                    onChange={(e) => updateSelectedTextStyle({ fontSize: parseInt(e.target.value, 10) || 16 })}
                    style={{ width: 48, fontSize: 12, padding: "2px 4px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4 }}
                  />
                  <button
                    onClick={() => updateSelectedTextStyle({ bold: !selectedText.bold })}
                    className="btn-manor-ghost"
                    style={{ fontSize: 12, padding: "2px 8px", fontWeight: 700, opacity: selectedText.bold ? 1 : 0.4 }}
                  >{t("page.doc_editor.b")}</button>
                  <button
                    onClick={() => updateSelectedTextStyle({ italic: !selectedText.italic })}
                    className="btn-manor-ghost"
                    style={{ fontSize: 12, padding: "2px 8px", fontStyle: "italic", opacity: selectedText.italic ? 1 : 0.4 }}
                  >{t("page.doc_editor.i")}</button>
                  <button
                    onClick={() => updateSelectedTextStyle({ underline: !selectedText.underline })}
                    className="btn-manor-ghost"
                    style={{ fontSize: 12, padding: "2px 8px", textDecoration: "underline", opacity: selectedText.underline ? 1 : 0.4 }}
                  >U</button>
                  <button
                    type="button"
                    onClick={() => updateSelectedTextStyle({ bullet: selectedText.bullet ? undefined : "\u2022", indent: selectedText.bullet ? undefined : selectedText.indent || 24 })}
                    className="btn-manor-ghost"
                    title={t("page.doc_editor.bulleted_list")}
                    aria-pressed={Boolean(selectedText.bullet)}
                    style={{ fontSize: 12, padding: "2px 8px", opacity: selectedText.bullet ? 1 : 0.5 }}
                  >
                    <IconList size={14} />
                  </button>
                  <input
                    type="color"
                    value={selectedText.color || "#000000"}
                    aria-label={t("page.doc_editor.color")}
                    title={t("page.doc_editor.color")}
                    onChange={(e) => updateSelectedTextStyle({ color: e.target.value })}
                    style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
                  />
                  <select
                    value={selectedText.align || "left"}
                    onChange={(e) => updateSelectedTextStyle({ align: e.target.value })}
                    style={{ fontSize: 12, padding: "3px 8px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 6, background: "#fff" }}
                  >
                    <option value="left">Left</option>
                    <option value="center">Center</option>
                    <option value="right">Right</option>
                  </select>
                  <select
                    value={selectedText.lineSpacing || 1.2}
                    aria-label={t("page.doc_editor.line_spacing")}
                    title={t("page.doc_editor.line_spacing")}
                    onChange={(event) => updateSelectedTextStyle({ lineSpacing: Number(event.target.value) })}
                    style={{ width: 64, fontSize: 12, padding: "3px 6px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 6, background: "#fff" }}
                  >
                    {selectedText.lineSpacing && ![1, 1.15, 1.2, 1.5, 2].includes(selectedText.lineSpacing) && (
                      <option value={selectedText.lineSpacing}>{Number(selectedText.lineSpacing.toFixed(2))}</option>
                    )}
                    <option value={1}>1.0</option>
                    <option value={1.15}>1.15</option>
                    <option value={1.2}>1.2</option>
                    <option value={1.5}>1.5</option>
                    <option value={2}>2.0</option>
                  </select>
                </>
              )}
              <button
                type="button"
                onClick={() => setShowFormatOptions((value) => !value)}
                className="btn-manor-ghost presentation-editor-format-toggle"
                aria-pressed={showFormatOptions}
                title={t("page.doc_editor.format_options")}
              >
                <IconSettings size={14} />
                <span>{t("page.doc_editor.format_options")}</span>
              </button>
              <button onClick={() => deleteShape(selectedShapeId)} className="btn-manor-ghost presentation-editor-danger-icon" title={t("action.delete")} aria-label={t("action.delete")}>
                <IconTrash size={14} />
              </button>
            </div>
          )}
          <div className="presentation-editor-toolbar-side" style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
            <button type="button" onClick={() => setPresentingIdx(activeIdx)} className="btn-manor-ghost presentation-editor-present-button" title={t("page.doc_editor.present")}>
              <IconPlay size={14} /> <span>{t("page.doc_editor.present")}</span>
            </button>
            <div className="presentation-editor-zoom-controls">
              <button type="button" onClick={() => setCanvasZoom((value) => Math.max(0.5, Number((value - 0.1).toFixed(1))))} disabled={canvasZoom <= 0.5} title={t("page.doc_editor.zoom_out")}>−</button>
              <button type="button" onClick={() => setCanvasZoom(1)} title={t("page.doc_editor.fit_slide")}>{Math.round(canvasZoom * 100)}%</button>
              <button type="button" onClick={() => setCanvasZoom((value) => Math.min(1.6, Number((value + 0.1).toFixed(1))))} disabled={canvasZoom >= 1.6} title={t("page.doc_editor.zoom_in")}>+</button>
            </div>
            <span className="presentation-editor-field-label" style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.doc_editor.bg")}</span>
            <input
              type="color"
              value={activeSlide.bg || "#ffffff"}
              onChange={(e) => updateSlideBg(e.target.value)}
              style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
            />
            {slides.length > 1 && (
              <button onClick={deleteSlide} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 12px", color: "#c14a44" }}>
                {t("page.doc_editor.delete_slide")}
              </button>
            )}
            {serverSlideUrls && serverSlideUrls.length > 0 && (
              <button
                onClick={() => {
                  setUseServerBg((value) => !value);
                  setSelectedShapeId(null);
                  setEditingText(null);
                  setEditingTableCell(null);
                }}
                className="btn-manor-ghost"
                style={{
                  fontSize: 11, padding: "4px 10px",
                  background: useServerBg ? "rgba(79,125,117,0.1)" : undefined,
                  color: useServerBg ? "#4f7d75" : "#78716c",
                  border: useServerBg ? "1px solid rgba(79,125,117,0.3)" : "1px solid #e7e5e4",
                  borderRadius: 6,
                }}
              >
                {useServerBg ? t("action.edit") : t("page.doc_editor.preview")}
              </button>
            )}
            <span className="presentation-editor-page-count" style={{ fontSize: 11, color: "#a8a29e" }}>
              {activeIdx + 1}/{slides.length}
            </span>
          </div>
        </div>

        <div className="presentation-editor-workspace">
          {/* Canvas */}
          <div
            ref={canvasViewportRef}
            className="presentation-editor-canvas-viewport"
            style={{
              flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
              padding: 24, background: "#e7e5e4", overflow: "auto", boxSizing: "border-box",
            }}
            onClick={() => { setSelectedShapeId(null); setEditingText(null); setEditingTableCell(null); }}
          >
          <div ref={canvasRef} className="presentation-editor-slide-frame" style={{
            width: renderedSlideFrameSize.width,
            height: renderedSlideFrameSize.height,
            flexShrink: 0,
            aspectRatio: activeSlide.aspectRatio || "16/9",
            position: "relative",
            ...(hasServerBg ? { backgroundColor: "#ffffff" } : slideBg),
            borderRadius: 8, boxShadow: "0 8px 32px rgba(0,0,0,0.15)",
            overflow: "hidden",
          }}>
            {/* Server-rendered slide image as pixel-perfect background */}
            {hasServerBg && (
              <img
                src={serverSlideUrls![activeIdx]}
                alt=""
                style={{
                  position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
                  objectFit: "contain", zIndex: 0, pointerEvents: "none",
                }}
              />
            )}
            {!hasServerBg && activeSlide.shapes.map((shape) => {
              const isSelected = selectedShapeId === shape.id;
              const shapeBgStyle: React.CSSProperties = {};
              if (shape.gradFill) {
                shapeBgStyle.background = pptxGradToCss(shape.gradFill);
              } else if (shape.fill) {
                shapeBgStyle.background = shape.fill;
              }

              // Compute border radius from preset geometry
              let borderRadius: string | number | undefined = shape.borderRadius ? `${shape.borderRadius}%` : undefined;
              const geom = shape.presetGeom;
              if (geom === "ellipse" || geom === "oval") borderRadius = "50%";
              else if (geom === "roundRect" && !borderRadius) borderRadius = "8%";
              else if (geom === "snip1Rect" || geom === "snip2SameRect") borderRadius = "0 12% 0 0";

              // Compute stroke border
              let borderStyle: string | undefined;
              if (shape.stroke) {
                borderStyle = `${Math.max(0.5, shape.strokeWidth || 1)}px solid ${shape.stroke}`;
              }

              // Rotation + flip
              const transforms: string[] = [];
              if (shape.rotation) transforms.push(`rotate(${shape.rotation}deg)`);
              if (shape.flipH) transforms.push("scaleX(-1)");
              if (shape.flipV) transforms.push("scaleY(-1)");
              const transform = transforms.length > 0 ? transforms.join(" ") : undefined;

              // Shadow
              let boxShadow: string | undefined;
              if (shape.shadow) {
                const s = shape.shadow;
                const rad = (s.angle * Math.PI) / 180;
                const sx = Math.round(Math.cos(rad) * s.dist);
                const sy = Math.round(Math.sin(rad) * s.dist);
                const sr = parseInt(s.color.slice(1, 3), 16);
                const sg = parseInt(s.color.slice(3, 5), 16);
                const sb = parseInt(s.color.slice(5, 7), 16);
                boxShadow = `${sx}px ${sy}px ${s.blur}px rgba(${sr},${sg},${sb},${s.alpha})`;
              }

              // Table rendering
              if (shape.type === "table" && shape.tableRows) {
                return (
                  <div
                    key={shape.id}
                    onClick={(e) => { e.stopPropagation(); setSelectedShapeId(shape.id); }}
                    onMouseDown={(e) => handleDragStart(e, shape.id)}
                    onContextMenu={(event) => openShapeContextMenu(event, shape.id)}
                    style={{
                      position: "absolute",
                      left: `${shape.x}%`, top: `${shape.y}%`,
                      width: `${shape.w}%`, height: `${shape.h}%`,
                      overflow: isSelected ? "visible" : "auto",
                      cursor: editingTableCell?.shapeId === shape.id ? "text" : "move",
                      outline: isSelected ? "2px solid #4f7d75" : undefined,
                      outlineOffset: 2,
                      transform,
                    }}
                  >
                    <table style={{
                      width: "100%", height: "100%", borderCollapse: "collapse",
                      fontSize: "clamp(8px, 1.2vw, 14px)", tableLayout: "fixed",
                    }}>
                      {shape.tableColWidths && (
                        <colgroup>
                          {shape.tableColWidths.map((w, ci) => {
                            const totalW = shape.tableColWidths!.reduce((a, b) => a + b, 0) || 1;
                            return <col key={ci} style={{ width: `${(w / totalW) * 100}%` }} />;
                          })}
                        </colgroup>
                      )}
                      <tbody>
                        {shape.tableRows.map((row, ri) => (
                          <tr key={ri}>
                            {row.map((cell, ci) => {
                              if (cell.vMerge) return null;
                              return (
                                <td
                                  key={ci}
                                  colSpan={cell.gridSpan}
                                  onDoubleClick={(event) => {
                                    event.stopPropagation();
                                    setSelectedShapeId(shape.id);
                                    setEditingText(null);
                                    setEditingTableCell({ shapeId: shape.id, rowIdx: ri, cellIdx: ci, value: cell.text, initialValue: cell.text });
                                  }}
                                  style={{
                                  border: "1px solid rgba(28,25,23,0.06)",
                                  padding: "2px 4px",
                                  background: cell.fill || (ri === 0 ? "#f5f5f4" : "white"),
                                  color: cell.color || "#292524",
                                  fontWeight: cell.bold || ri === 0 ? 700 : 400,
                                  overflow: "hidden", textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                  lineHeight: 1.3,
                                }}>
                                  {editingTableCell?.shapeId === shape.id && editingTableCell.rowIdx === ri && editingTableCell.cellIdx === ci ? (
                                    <input
                                      autoFocus
                                      value={editingTableCell.value}
                                      onChange={(event) => setEditingTableCell((current) => current ? { ...current, value: event.target.value } : current)}
                                      onBlur={commitTableCellEditing}
                                      onMouseDown={(event) => event.stopPropagation()}
                                      onClick={(event) => event.stopPropagation()}
                                      onKeyDown={(event) => {
                                        if (event.key === "Escape") setEditingTableCell(null);
                                        if (event.key === "Enter") event.currentTarget.blur();
                                      }}
                                      className="presentation-editor-inline-input"
                                    />
                                  ) : cell.text}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {isSelected && !editingTableCell && renderResizeHandles(shape.id)}
                  </div>
                );
              }

              return (
                <div
                  key={shape.id}
                  onClick={(e) => { e.stopPropagation(); setSelectedShapeId(shape.id); }}
                  onMouseDown={(e) => handleDragStart(e, shape.id)}
                  onContextMenu={(event) => openShapeContextMenu(event, shape.id)}
                  style={{
                    position: "absolute",
                    left: `${shape.x}%`, top: `${shape.y}%`,
                    width: `${shape.w}%`, height: `${shape.h}%`,
                    ...shapeBgStyle,
                    border: isSelected ? "2px solid #4f7d75" : borderStyle || "none",
                    borderRadius: borderRadius || 0,
                    opacity: shape.opacity,
                    cursor: editingText?.shapeId === shape.id ? "text" : "move",
                    outline: isSelected ? "2px solid rgba(79,125,117,0.3)" : undefined,
                    outlineOffset: 2,
                    display: "flex", flexDirection: "column",
                    justifyContent: shape.vAlign === "bottom" ? "flex-end" : shape.vAlign === "middle" ? "center" : "flex-start",
                    overflow: isSelected ? "visible" : "hidden",
                    boxSizing: "border-box",
                    padding: shape.padding ? `${shape.padding.t}pt ${shape.padding.r}pt ${shape.padding.b}pt ${shape.padding.l}pt` : shape.texts.length > 0 ? "2% 3%" : undefined,
                    transform,
                    boxShadow: boxShadow,
                  }}
                >
                  {shape.imgUrl && (
                    shape.imgCrop ? (
                      <div style={{
                        position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
                        overflow: "hidden", zIndex: 0, borderRadius: borderRadius || 0,
                      }}>
                        <img src={shape.imgUrl} alt="" style={{
                          position: "absolute",
                          left: `${-shape.imgCrop.l}%`, top: `${-shape.imgCrop.t}%`,
                          width: `${100 + shape.imgCrop.l + shape.imgCrop.r}%`,
                          height: `${100 + shape.imgCrop.t + shape.imgCrop.b}%`,
                          objectFit: "fill",
                        }} />
                      </div>
                    ) : (
                      <img src={shape.imgUrl} alt="" style={{
                        width: "100%", height: "100%", objectFit: shape.imageFit || "cover",
                        position: "absolute", top: 0, left: 0, zIndex: 0,
                        borderRadius: borderRadius || 0,
                      }} />
                    )
                  )}
                  {shape.texts.map((t, ti) => {
                    const isEditing = editingText?.shapeId === shape.id && editingText?.textIdx === ti;
                    return (
                      <div
                        key={ti}
                        onDoubleClick={(e) => {
                          e.stopPropagation();
                          beginTextEditing(shape.id, ti, t.text);
                        }}
                        style={{
                          position: "relative",
                          zIndex: 1,
                          marginTop: t.spaceBefore ? `${t.spaceBefore}pt` : t.text === "" ? "0.3em" : "0.05em",
                          marginBottom: t.spaceAfter ? `${t.spaceAfter}pt` : "0.05em",
                          paddingLeft: t.indent ? `${t.indent}px` : t.bullet ? "18px" : undefined,
                          fontSize: t.fontSize ? `${Math.max(8, t.fontSize * 0.85)}px` : "14px",
                          fontWeight: t.bold ? 700 : 400,
                          fontStyle: t.italic ? "italic" : undefined,
                          textDecoration: [t.underline ? "underline" : "", t.strikethrough ? "line-through" : ""].filter(Boolean).join(" ") || undefined,
                          color: t.color || _activeTheme.tx1 || "#000000",
                          textAlign: (t.align as any) || undefined,
                          lineHeight: t.lineSpacing || 1.35,
                          wordBreak: "break-word",
                          whiteSpace: "pre-wrap",
                          minHeight: "1.2em",
                          fontFamily: t.fontFamily ? `"${t.fontFamily}", sans-serif` : (_editorMinorFont ? `"${_editorMinorFont}", sans-serif` : undefined),
                        }}
                      >
                        {isEditing ? (
                          <textarea
                            autoFocus
                            value={editingText.value}
                            onChange={(e) => setEditingText((current) => current ? { ...current, value: e.target.value } : current)}
                            onBlur={commitTextEditing}
                            onMouseDown={(e) => e.stopPropagation()}
                            onClick={(e) => e.stopPropagation()}
                            onKeyDown={(e) => {
                              if (e.key === "Escape") {
                                e.preventDefault();
                                cancelTextEditing();
                              } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                                e.preventDefault();
                                e.currentTarget.blur();
                              }
                            }}
                            rows={Math.max(1, editingText.value.split("\n").length)}
                            style={{
                              width: "100%", minHeight: "100%", border: "none", outline: "none", resize: "none",
                              background: "rgba(255,255,255,0.9)", borderRadius: 4,
                              fontSize: "inherit", fontWeight: "inherit", fontStyle: "inherit",
                              color: "inherit", textAlign: "inherit", lineHeight: "inherit",
                              padding: "2px 4px",
                              fontFamily: "inherit",
                            }}
                          />
                        ) : (
                          <>
                            {t.bullet && <span style={{ position: "absolute", left: t.indent ? `${t.indent - 14}px` : "2px" }}>{t.bullet}</span>}
                            {t.runs && t.runs.length > 1 ? t.runs.map((run, ri) => (
                              <span key={ri} style={{
                                fontWeight: run.bold ? 700 : undefined,
                                fontStyle: run.italic ? "italic" : undefined,
                                textDecoration: [run.underline ? "underline" : "", run.strikethrough ? "line-through" : ""].filter(Boolean).join(" ") || undefined,
                                fontSize: run.fontSize ? `${Math.max(8, run.fontSize * 0.85)}px` : undefined,
                                color: run.color || undefined,
                                fontFamily: run.fontFamily ? `"${run.fontFamily}", sans-serif` : undefined,
                                verticalAlign: run.baseline ? (run.baseline > 0 ? "super" : "sub") : undefined,
                                letterSpacing: run.spacing ? `${run.spacing}pt` : undefined,
                              }}>{run.text}</span>
                            )) : t.text}
                          </>
                        )}
                      </div>
                    );
                  })}
                  {shape.texts.length === 0 && !shape.imgUrl && !shape.tableRows && isSelected && (
                    <div style={{ color: "#a8a29e", fontSize: 12, textAlign: "center" }}>{t("page.doc_editor.empty_shape")}</div>
                  )}
                  {isSelected && !editingText && renderResizeHandles(shape.id)}
                </div>
              );
            })}
          </div>
          </div>
          {showFormatOptions && selectedShape && (
            <aside className="presentation-editor-format-panel" onClick={(event) => event.stopPropagation()}>
              <div className="presentation-editor-format-panel-header">
                <div>
                  <strong>{t("page.doc_editor.format_options")}</strong>
                  <span>{t("page.doc_editor.object")}</span>
                </div>
                <button type="button" onClick={() => setShowFormatOptions(false)} title={t("action.close")} aria-label={t("action.close")}>
                  <IconClose size={16} />
                </button>
              </div>

              <section className="presentation-editor-format-section">
                <h3>{t("page.doc_editor.size_position")}</h3>
                <div className="presentation-editor-format-grid">
                  <label>
                    <span>X</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={0.5}
                      value={Number(selectedShape.x.toFixed(1))}
                      onChange={(event) => updateShapeProps(selectedShape.id, { x: Number(event.target.value) || 0 })}
                    />
                  </label>
                  <label>
                    <span>Y</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={0.5}
                      value={Number(selectedShape.y.toFixed(1))}
                      onChange={(event) => updateShapeProps(selectedShape.id, { y: Number(event.target.value) || 0 })}
                    />
                  </label>
                  <label>
                    <span>W</span>
                    <input
                      type="number"
                      min={1}
                      max={100}
                      step={0.5}
                      value={Number(selectedShape.w.toFixed(1))}
                      onChange={(event) => updateShapeProps(selectedShape.id, { w: Math.max(1, Number(event.target.value) || 1) })}
                    />
                  </label>
                  <label>
                    <span>H</span>
                    <input
                      type="number"
                      min={0.5}
                      max={100}
                      step={0.5}
                      value={Number(selectedShape.h.toFixed(1))}
                      onChange={(event) => updateShapeProps(selectedShape.id, { h: Math.max(0.5, Number(event.target.value) || 0.5) })}
                    />
                  </label>
                </div>
                <label className="presentation-editor-format-row">
                  <span>{t("page.doc_editor.rotate")}</span>
                  <input
                    type="number"
                    min={-360}
                    max={360}
                    step={1}
                    value={Math.round(selectedShape.rotation ?? 0)}
                    onChange={(event) => updateShapeProps(selectedShape.id, { rotation: Number(event.target.value) || 0 })}
                  />
                </label>
                <label className="presentation-editor-format-slider">
                  <span>
                    <span>{t("page.doc_editor.opacity")}</span>
                    <output>{Math.round((selectedShape.opacity ?? 1) * 100)}%</output>
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={5}
                    value={Math.round((selectedShape.opacity ?? 1) * 100)}
                    onChange={(event) => updateShapeProps(selectedShape.id, { opacity: Number(event.target.value) / 100 })}
                  />
                </label>
              </section>

              {selectedShape.imgUrl && (
                <section className="presentation-editor-format-section">
                  <h3>{t("page.doc_editor.image")}</h3>
                  <label className="presentation-editor-format-row">
                    <span>{t("page.doc_editor.image_fit")}</span>
                    <select
                      value={selectedShape.imageFit || "cover"}
                      onChange={(event) => updateShapeProps(selectedShape.id, { imageFit: event.target.value as "cover" | "contain" })}
                    >
                      <option value="contain">{t("page.doc_editor.fit_image")}</option>
                      <option value="cover">{t("page.doc_editor.fill_crop")}</option>
                    </select>
                  </label>
                  <button type="button" className="presentation-editor-format-command" onClick={() => requestImageReplacement(selectedShape.id)}>
                    <IconImage size={14} /> {t("page.doc_editor.replace_image")}
                  </button>
                </section>
              )}

              <section className="presentation-editor-format-section">
                <h3>{t("page.doc_editor.arrange")}</h3>
                <div className="presentation-editor-format-actions">
                  <button type="button" onClick={() => moveShapeZ(selectedShape.id, "up")}>
                    <IconArrowUp size={14} /> {t("page.doc_editor.bring_forward")}
                  </button>
                  <button type="button" onClick={() => moveShapeZ(selectedShape.id, "down")}>
                    <IconArrowDown size={14} /> {t("page.doc_editor.send_backward")}
                  </button>
                </div>
              </section>

              <section className="presentation-editor-format-section presentation-editor-format-actions-section">
                <button type="button" onClick={() => copyShape(selectedShape.id)}>
                  <IconCopy size={14} /> {t("action.copy")}
                </button>
                <button type="button" onClick={() => duplicateShape(selectedShape.id)}>
                  <IconCopy size={14} /> {t("page.doc_editor.duplicate_object")}
                </button>
                <button type="button" className="is-danger" onClick={() => deleteShape(selectedShape.id)}>
                  <IconTrash size={14} /> {t("action.delete")}
                </button>
              </section>
            </aside>
          )}
        </div>
        <div className="presentation-editor-speaker-notes">
          <label htmlFor="presentation-speaker-notes">{t("page.doc_editor.speaker_notes")}</label>
          <textarea
            id="presentation-speaker-notes"
            value={notesDraft}
            placeholder={t("page.doc_editor.speaker_notes_placeholder")}
            onChange={(event) => setNotesDraft(event.target.value)}
            onBlur={commitSpeakerNotes}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) event.currentTarget.blur();
            }}
          />
        </div>
      </div>

      {shapeContextMenu && contextMenuShape && (
        <div
          className="presentation-editor-context-menu"
          style={{ left: shapeContextMenu.x, top: shapeContextMenu.y }}
          onMouseDown={(event) => event.stopPropagation()}
        >
          {contextMenuShape.texts.length > 0 && (
            <button type="button" onClick={() => {
              beginTextEditing(contextMenuShape.id, 0, contextMenuShape.texts[0].text);
              setShapeContextMenu(null);
            }}>
              <IconText size={15} /> {t("action.edit")}
            </button>
          )}
          {contextMenuShape.imgUrl && (
            <button type="button" onClick={() => {
              requestImageReplacement(contextMenuShape.id);
              setShapeContextMenu(null);
            }}>
              <IconImage size={15} /> {t("page.doc_editor.replace_image")}
            </button>
          )}
          <button type="button" onClick={() => { copyShape(contextMenuShape.id); setShapeContextMenu(null); }}>
            <IconCopy size={15} /> {t("action.copy")}
          </button>
          <button type="button" onClick={() => { duplicateShape(contextMenuShape.id); setShapeContextMenu(null); }}>
            <IconCopy size={15} /> {t("page.doc_editor.duplicate_object")}
          </button>
          <div className="presentation-editor-context-menu-separator" />
          <button type="button" onClick={() => { moveShapeZ(contextMenuShape.id, "top"); setShapeContextMenu(null); }}>
            <IconArrowUp size={15} /> {t("page.doc_editor.bring_to_front")}
          </button>
          <button type="button" onClick={() => { moveShapeZ(contextMenuShape.id, "bottom"); setShapeContextMenu(null); }}>
            <IconArrowDown size={15} /> {t("page.doc_editor.send_to_back")}
          </button>
          <button type="button" onClick={() => {
            setSelectedShapeId(contextMenuShape.id);
            setShowFormatOptions(true);
            setShapeContextMenu(null);
          }}>
            <IconSettings size={15} /> {t("page.doc_editor.format_options")}
          </button>
          <div className="presentation-editor-context-menu-separator" />
          <button type="button" className="is-danger" onClick={() => { deleteShape(contextMenuShape.id); setShapeContextMenu(null); }}>
            <IconTrash size={15} /> {t("action.delete")}
          </button>
        </div>
      )}

      {presentingIdx !== null && slides[presentingIdx] && createPortal((
        <div className="presentation-editor-present-overlay" role="dialog" aria-modal="true" aria-label={t("page.doc_editor.presentation_mode")}>
          <div className="presentation-editor-present-header">
            <span>{presentingIdx + 1} / {slides.length}</span>
            <button type="button" onClick={() => setPresentingIdx(null)} title={t("action.close")} aria-label={t("action.close")}>
              <IconClose size={18} />
            </button>
          </div>
          <div
            className="presentation-editor-present-stage"
            onClick={() => setPresentingIdx((index) => index === null ? 0 : Math.min(slides.length - 1, index + 1))}
          >
            <PptxReadOnlySlide
              slide={slides[presentingIdx]}
              serverUrl={useServerBg ? serverSlideUrls?.[presentingIdx] : undefined}
            />
          </div>
          <div className="presentation-editor-present-controls">
            <button type="button" disabled={presentingIdx === 0} onClick={() => setPresentingIdx((index) => index === null ? 0 : Math.max(0, index - 1))}>
              <IconArrowLeft size={16} /> {t("page.doc_editor.previous_slide")}
            </button>
            <span>{t("page.doc_editor.presentation_hint")}</span>
            <button type="button" disabled={presentingIdx >= slides.length - 1} onClick={() => setPresentingIdx((index) => index === null ? 0 : Math.min(slides.length - 1, index + 1))}>
              {t("page.doc_editor.next_slide")} <IconArrowRight size={16} />
            </button>
          </div>
        </div>
      ), document.body)}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function DocEditor() {
  const { docId } = useParams<{ docId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const currentUser = useAuthStore((s) => s.user);
  const queryClient = useQueryClient();
  const [content, setContent] = useState("");
  const [saveStatus, setSaveStatus] = useState<"saved" | "saving" | "unsaved">("saved");
  const [showPreview, setShowPreview] = useState(true);
  const [codePreviewAssetReplacements, setCodePreviewAssetReplacements] = useState<Record<string, PreviewAssetReplacement>>({});
  const [markdownViewMode, setMarkdownViewMode] = useState<MarkdownViewMode>("split");
  const [showVersions, setShowVersions] = useState(false);
  const [showComments, setShowComments] = useState(false);
  const [commentAnchor, setCommentAnchor] = useState<CommentAnchor | null>(null);
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null);
  const [documentComments, setDocumentComments] = useState<Comment[]>([]);
  const [lineCount, setLineCount] = useState(1);
  const [richTextBlock, setRichTextBlock] = useState("p");
  const [richTextFont, setRichTextFont] = useState("Inter");
  const [richTextSize, setRichTextSize] = useState("16");
  const [liveDiff, setLiveDiff] = useState<string | null>(null);
  const [liveEditNotice, setLiveEditNotice] = useState<string | null>(null);

  // For DOCX: convert to HTML for editing, track original import
  const [docxHtml, setDocxHtml] = useState<string | null>(null);
  const [docxLoading, setDocxLoading] = useState(false);

  // For XLSX: parsed sheet data
  const [sheetData, setSheetData] = useState<any[][] | null>(null);
  const [sheetCharts, setSheetCharts] = useState<SheetChartConfig[]>([]);
  const [sheetStyles, setSheetStyles] = useState<SheetStyleMap>({});
  const [xlsxLoading, setXlsxLoading] = useState(false);

  // For PPTX: parsed slides + server-rendered images
  const [pptxSlides, setPptxSlides] = useState<PptxSlide[]>([]);
  const [pptxLoading, setPptxLoading] = useState(false);
  const [pptxServerUrls, setPptxServerUrls] = useState<string[]>([]);

  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const richSelectionRef = useRef<Range | null>(null);
  const markdownRef = useRef<HTMLTextAreaElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const codeRef = useRef<HTMLTextAreaElement>(null);
  const codeGutterRef = useRef<HTMLDivElement>(null);
  const codeHighlightRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef("");
  const sheetDataRef = useRef<any[][] | null>(null);
  const sheetChartsRef = useRef<SheetChartConfig[]>([]);
  const sheetStylesRef = useRef<SheetStyleMap>({});
  const pptxSlidesRef = useRef<PptxSlide[]>([]);
  const pptxSaveRevisionRef = useRef(0);
  const pptxSaveQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const codePreviewObjectUrlsRef = useRef<string[]>([]);
  const knowledgeReturnTo = getKnowledgeReturnTo(location.state);

  useEffect(() => {
    setLiveDiff(null);
    setLiveEditNotice(null);
    pptxSaveRevisionRef.current += 1;
  }, [docId]);
  useEffect(() => {
    if (!liveEditNotice) return undefined;
    const timer = window.setTimeout(() => setLiveEditNotice(null), 8000);
    return () => window.clearTimeout(timer);
  }, [liveEditNotice]);

  const syncCodeScrollLayers = useCallback((scrollTop: number, scrollLeft: number) => {
    if (codeGutterRef.current) {
      codeGutterRef.current.style.transform = `translateY(-${scrollTop}px)`;
    }
    if (codeHighlightRef.current) {
      codeHighlightRef.current.style.transform = `translate(${-scrollLeft}px, -${scrollTop}px)`;
    }
  }, []);

  useEffect(() => {
    const textarea = codeRef.current;
    if (!textarea) return;
    syncCodeScrollLayers(textarea.scrollTop, textarea.scrollLeft);
  }, [content, liveDiff, syncCodeScrollLayers]);

  // Queries
  const { data: doc } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => api.documents.get(docId!),
    enabled: !!docId,
  });
  const canEditCurrentDoc = canEditDocument(currentUser, doc);
  const canCommentCurrentDoc = canCommentDocument(currentUser, doc);

  useEffect(() => {
    if (!doc || !currentUser || canEditCurrentDoc) return;
    navigate(`/viewer/${doc.id}`, { replace: true, state: location.state });
  }, [canEditCurrentDoc, currentUser, doc, location.state, navigate]);

  const { data: contentData, isLoading: contentLoading } = useQuery({
    queryKey: ["document-content", docId],
    queryFn: () => api.documents.getContent(docId!),
    enabled: !!docId && !!doc && !isOfficeDoc(doc.name) && !isSpreadsheetFile(doc.name) && !isPptxFile(doc.name),
  });

  const { data: versions, refetch: refetchVersions } = useQuery({
    queryKey: ["document-versions", docId],
    queryFn: () => api.documents.getVersions(docId!),
    enabled: !!docId && showVersions,
  });

  const { data: editorComments = [] } = useQuery<Comment[]>({
    queryKey: ["comments", "document", docId],
    queryFn: () => api.comments.list("document", docId!),
    enabled: !!docId && !!doc,
    retry: false,
  });

  useEffect(() => {
    setDocumentComments(editorComments);
  }, [editorComments]);

  const saveMutation = useMutation({
    mutationFn: (text: string) => {
      if (!canEditCurrentDoc) throw new Error("You do not have edit access to this document");
      return api.documents.saveContent(docId!, text);
    },
    onMutate: () => setSaveStatus("saving"),
    onSuccess: () => {
      setSaveStatus("saved");
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", doc?.fs_path] });
      if (showVersions) refetchVersions();
    },
    onError: () => setSaveStatus("unsaved"),
  });

  const presentationSaveMutation = useMutation({
    mutationFn: ({ slides, revision }: { slides: PptxSlide[]; revision: number }) => {
      const saveTask = pptxSaveQueueRef.current.catch(() => undefined).then(async () => {
        if (!canEditCurrentDoc) throw new Error("You do not have edit access to this document");
        if (revision !== pptxSaveRevisionRef.current) return { updatedDocument: null, revision };
        const { buildPresentationFile } = await import("../lib/presentationPptx");
        const file = await buildPresentationFile(slides, doc?.name || "Presentation.pptx");
        if (revision !== pptxSaveRevisionRef.current) return { updatedDocument: null, revision };
        const updatedDocument = await api.documents.replaceFile(docId!, file);
        return { updatedDocument, revision };
      });
      pptxSaveQueueRef.current = saveTask;
      return saveTask;
    },
    onMutate: ({ revision }) => {
      if (revision === pptxSaveRevisionRef.current) setSaveStatus("saving");
    },
    onSuccess: ({ updatedDocument, revision }) => {
      if (revision === pptxSaveRevisionRef.current) setSaveStatus("saved");
      if (!updatedDocument) return;
      queryClient.setQueryData(["document", docId], updatedDocument);
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", doc?.fs_path] });
      setPptxServerUrls([]);
      if (showVersions) refetchVersions();
    },
    onError: (_error, { revision }) => {
      if (revision === pptxSaveRevisionRef.current) setSaveStatus("unsaved");
    },
  });

  const flushSave = useCallback(async (text = content) => {
    if (!docId) return true;
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    if (doc?.name && isPptxFile(doc.name)) {
      if (saveStatus === "saved" && !presentationSaveMutation.isPending) return true;
      const revision = pptxSaveRevisionRef.current;
      try {
        await presentationSaveMutation.mutateAsync({ slides: pptxSlidesRef.current, revision });
        return true;
      } catch {
        return false;
      }
    }
    if (saveStatus === "saved" && !saveMutation.isPending) return true;
    try {
      await saveMutation.mutateAsync(text);
      return true;
    } catch {
      return false;
    }
  }, [content, doc?.name, docId, presentationSaveMutation, saveMutation, saveStatus]);

  const goBackToKnowledge = useCallback(async () => {
    const saved = await flushSave(content);
    if (saved) navigate(knowledgeReturnTo || "/knowledge");
  }, [content, flushSave, knowledgeReturnTo, navigate]);

  // Derived
  const mode: EditorMode = doc ? detectMode(doc.name) : "richtext";
  const docName = doc?.name || "Document";
  const isDocx = doc ? isOfficeDoc(doc.name) : false;
  const isXlsx = doc ? isSpreadsheetFile(doc.name) : false;
  const isCsv = doc ? isCsvFile(doc.name) : false;
  const isPptx = doc ? isPptxFile(doc.name) : false;
  const codeLanguage = useMemo(() => codeLanguageForFile(docName), [docName]);
  const codeLanguageName = useMemo(() => codeLanguageLabel(docName), [docName]);
  const htmlPreviewAssetRefs = useMemo(
    () => (isHtmlFile(docName) ? extractLocalHtmlAssetRefs(content) : []),
    [content, docName],
  );
  const htmlPreviewAssetRefKey = useMemo(
    () => htmlPreviewAssetRefs.slice().sort().join("\n"),
    [htmlPreviewAssetRefs],
  );
  const codePreviewContent = useMemo(
    () => (isHtmlFile(docName) ? injectHtmlPreviewNavigationGuard(rewriteHtmlLocalAssetUrls(content, codePreviewAssetReplacements)) : content),
    [codePreviewAssetReplacements, content, docName],
  );

  const { data: wikiLinkData } = useQuery({
    queryKey: ["fs-wiki-links", doc?.fs_path],
    queryFn: () => api.fs.wikiLinks(doc!.fs_path!),
    enabled: Boolean(doc?.fs_path && mode === "markdown"),
  });

  useEffect(() => {
    const revokePreviewUrls = () => {
      codePreviewObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
      codePreviewObjectUrlsRef.current = [];
    };

    if (!doc?.fs_path || !isHtmlFile(docName) || !htmlPreviewAssetRefs.length) {
      revokePreviewUrls();
      setCodePreviewAssetReplacements({});
      return;
    }

    let cancelled = false;
    const currentFsPath = doc.fs_path;

    async function loadPreviewAssets() {
      const nextAssetReplacements: Record<string, PreviewAssetReplacement> = {};
      const nextObjectUrls: string[] = [];

      await Promise.all(
        htmlPreviewAssetRefs.map(async (ref) => {
          const path = resolvePreviewAssetPath(currentFsPath, ref);
          if (!path) return;
          try {
            const result = await api.fs.read(path);
            const inlineKind = previewAssetKind(ref, result);
            if (inlineKind === "style" || inlineKind === "script") {
              nextAssetReplacements[ref] = { kind: inlineKind, value: result.content };
              return;
            }
            const objectUrl = URL.createObjectURL(blobFromFsReadResult(result));
            nextAssetReplacements[ref] = { kind: "url", value: objectUrl };
            nextObjectUrls.push(objectUrl);
          } catch {
            // Keep the original URL in the preview. The browser console will
            // surface the missing asset, but the editor itself should remain usable.
          }
        }),
      );

      if (cancelled) {
        nextObjectUrls.forEach((url) => URL.revokeObjectURL(url));
        return;
      }

      revokePreviewUrls();
      codePreviewObjectUrlsRef.current = nextObjectUrls;
      setCodePreviewAssetReplacements(nextAssetReplacements);
    }

    loadPreviewAssets();

    return () => {
      cancelled = true;
    };
  }, [doc?.fs_path, docName, htmlPreviewAssetRefKey]);

  useEffect(() => () => {
    codePreviewObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    codePreviewObjectUrlsRef.current = [];
  }, []);

  useEffect(() => { contentRef.current = content; }, [content]);
  useEffect(() => { sheetDataRef.current = sheetData; }, [sheetData]);
  useEffect(() => { sheetChartsRef.current = sheetCharts; }, [sheetCharts]);
  useEffect(() => { sheetStylesRef.current = sheetStyles; }, [sheetStyles]);
  useEffect(() => { pptxSlidesRef.current = pptxSlides; }, [pptxSlides]);

  // Load text content
  useEffect(() => {
    if (contentData?.content != null) {
      const spreadsheetPayload = parseSpreadsheetPayload(contentData.content);
      if (spreadsheetPayload) {
        setContent(serializeSpreadsheetContent(spreadsheetPayload.data, spreadsheetPayload.charts, isXlsx, spreadsheetPayload.styles));
        setSheetData(spreadsheetPayload.data);
        setSheetCharts(spreadsheetPayload.charts);
        setSheetStyles(spreadsheetPayload.styles);
      } else {
        setContent(contentData.content);
        if (isCsv) {
          setSheetData(parseCsvText(contentData.content));
          setSheetCharts([]);
          setSheetStyles({});
        }
      }
    }
  }, [contentData, isCsv, isXlsx]);

  // Load DOCX: download blob → mammoth → HTML (or load saved HTML directly)
  useEffect(() => {
    if (!doc || !docId || !isDocx) return;
    setDocxLoading(true);
    (async () => {
      try {
        const url = await api.documents.download(docId);
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const bytes = new Uint8Array(buf);
        // Real DOCX starts with PK zip signature (0x50 0x4B)
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const mammoth = await import("mammoth");
          const result = await mammoth.convertToHtml({ arrayBuffer: buf });
          setDocxHtml(result.value);
          setContent(result.value);
        } else {
          // Previously saved as HTML text — load directly
          const html = new TextDecoder().decode(buf);
          setDocxHtml(html);
          setContent(html);
        }
      } catch (e) {
        console.error("Failed to load DOCX for editing:", e);
        // Try loading as plain text content (fallback)
        try {
          const textRes = await api.documents.getContent(docId);
          const html = typeof textRes === "string" ? textRes : textRes.content;
          if (html) { setDocxHtml(html); setContent(html); return; }
        } catch { /* ignore */ }
        setDocxHtml(`<p>${t("page.doc_editor.failed_to_load_document_for_editing")}</p>`);
      } finally {
        setDocxLoading(false);
      }
    })();
  }, [doc, docId, isDocx]);

  // Load XLSX: download blob → xlsx → data[][] (or CSV fallback)
  useEffect(() => {
    if (!doc || !docId || !isXlsx) return;
    setXlsxLoading(true);
    (async () => {
      try {
        const url = await api.documents.download(docId);
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const bytes = new Uint8Array(buf);
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const XLSX = await import("xlsx");
          const wb = XLSX.read(buf, { type: "array" });
          const firstSheet = wb.SheetNames.find((name: string) => name !== SPREADSHEET_CHARTS_SHEET) || wb.SheetNames[0];
          const data = normalizeSheetData(XLSX.utils.sheet_to_json<any[]>(wb.Sheets[firstSheet], { header: 1 }) as any[][]);
          const metadata = readWorkbookEditorMetadata(wb, data);
          setSheetData(data);
          setSheetCharts(metadata.charts);
          setSheetStyles(metadata.styles);
        } else {
          // Previously saved as CSV — parse directly
          const text = new TextDecoder().decode(buf);
          const spreadsheetPayload = parseSpreadsheetPayload(text);
          if (spreadsheetPayload) {
            setSheetData(spreadsheetPayload.data);
            setSheetCharts(spreadsheetPayload.charts);
            setSheetStyles(spreadsheetPayload.styles);
          } else {
            setSheetData(parseCsvText(text));
            setSheetCharts([]);
            setSheetStyles({});
          }
        }
      } catch (e) {
        console.error("Failed to load XLSX for editing:", e);
      } finally {
        setXlsxLoading(false);
      }
    })();
  }, [doc, docId, isXlsx]);

  // Load PPTX: fetch server-rendered slides + download blob → parse slides (or text fallback)
  useEffect(() => {
    if (!doc || !docId || !isPptx) return;
    setPptxLoading(true);
    setPptxServerUrls([]);
    let cancelled = false;
    const objectUrls: string[] = [];

    const fetchWithTimeout = async (input: RequestInfo | URL, init: RequestInit = {}, timeoutMs = 8000) => {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(input, { ...init, signal: controller.signal });
      } finally {
        window.clearTimeout(timeout);
      }
    };

    (async () => {
      // Server-rendered slide images are useful previews, but they must not block
      // the editable fallback. Keep this work in the background.
      void (async () => {
        try {
          const token = getAuthToken();
          const headers: Record<string, string> = {};
          if (token) headers["Authorization"] = `Bearer ${token}`;
          const slideRes = await fetchWithTimeout(`/api/v1/documents/${docId}/slides`, { headers });
          if (!slideRes.ok || cancelled) return;
          const slideData = await slideRes.json();
          if (cancelled || !slideData.slides?.length) return;
          const blobUrls = await Promise.all(
            slideData.slides.map(async (s: { url: string }) => {
              const res = await fetchWithTimeout(`/api/v1${s.url}`, { headers });
              if (!res.ok) throw new Error("Slide image fetch failed");
              const blob = await res.blob();
              const blobUrl = URL.createObjectURL(blob);
              objectUrls.push(blobUrl);
              return blobUrl;
            })
          );
          if (!cancelled) setPptxServerUrls(blobUrls);
        } catch { /* server rendering unavailable — CSS fallback is fine */ }
      })();

      try {
        const url = await api.documents.download(docId);
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const bytes = new Uint8Array(buf);
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const parsed = await parsePptxForEditor(buf);
          if (!cancelled) {
            const nextSlides = parsed.length > 0 ? parsed : [{ id: genId(), bg: "#ffffff", shapes: [] }];
            setPptxSlides(nextSlides);
            setContent(slidesToText(nextSlides));
            setSaveStatus("saved");
          }
        } else {
          // AI-drafted text — parse into slide structures
          const text = new TextDecoder().decode(bytes);
          if (!cancelled) {
            setPptxSlides(textToSlides(text));
            setContent(text);
            setSaveStatus("saved");
          }
        }
      } catch (e) {
        console.error("Failed to load PPTX for editing:", e);
        // Try plain text fallback
        try {
          const textRes = await api.documents.getContent(docId);
          const text = typeof textRes === "string" ? textRes : textRes.content;
          if (text && !cancelled) {
            setPptxSlides(textToSlides(text));
            setContent(text);
            setSaveStatus("saved");
            return;
          }
        } catch { /* ignore */ }
        if (!cancelled) {
          const emptySlides = [{ id: genId(), bg: "#ffffff", shapes: [] }];
          setPptxSlides(emptySlides);
          setContent(slidesToText(emptySlides));
        }
      } finally {
        if (!cancelled) setPptxLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [docId, isPptx]);

  // Track line count for plain text and code modes
  useEffect(() => {
    if (mode === "code" || mode === "text") setLineCount(content.split("\n").length);
  }, [content, mode]);

  useEffect(() => {
    if (mode !== "text") return;
    const textarea = textRef.current;
    if (!textarea) return;

    const resizeTextPage = () => {
      textarea.style.height = "auto";
      textarea.style.height = `${textarea.scrollHeight + 2}px`;
    };

    resizeTextPage();
    const frame = window.requestAnimationFrame(resizeTextPage);
    window.addEventListener("resize", resizeTextPage);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", resizeTextPage);
    };
  }, [content, mode]);

  // Debounced auto-save
  const scheduleSave = useCallback(
    (text: string) => {
      if (!canEditCurrentDoc) return;
      setSaveStatus("unsaved");
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      saveTimerRef.current = setTimeout(() => {
        saveTimerRef.current = null;
        saveMutation.mutate(text);
      }, 3000);
    },
    [canEditCurrentDoc, saveMutation],
  );

  const schedulePresentationSave = useCallback((newSlides: PptxSlide[]) => {
    if (!canEditCurrentDoc) return;
    const revision = pptxSaveRevisionRef.current + 1;
    pptxSaveRevisionRef.current = revision;
    setSaveStatus("unsaved");
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      saveTimerRef.current = null;
      presentationSaveMutation.mutate({ slides: newSlides, revision });
    }, 1800);
  }, [canEditCurrentDoc, presentationSaveMutation]);

  const handleContentChange = useCallback(
    (text: string) => {
      setContent(text);
      scheduleSave(text);
    },
    [scheduleSave],
  );

  const handleSlidesChange = useCallback(
    (newSlides: PptxSlide[]) => {
      setPptxSlides(newSlides);
      const text = slidesToText(newSlides);
      setContent(text);
      schedulePresentationSave(newSlides);
    },
    [schedulePresentationSave],
  );

  // Spreadsheet change → convert to CSV and save
  const handleSheetChange = useCallback(
    (data: any[][], charts: SheetChartConfig[], styles: SheetStyleMap) => {
      setSheetData(data);
      setSheetCharts(charts);
      setSheetStyles(styles);
      const content = serializeSpreadsheetContent(data, charts, isXlsx, styles);
      setContent(content);
      scheduleSave(content);
    },
    [isXlsx, scheduleSave],
  );

  // Manual save (Ctrl+S)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        void flushSave(content);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [content, flushSave]);

  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (saveStatus === "saved" && !saveMutation.isPending && !presentationSaveMutation.isPending) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [presentationSaveMutation.isPending, saveMutation.isPending, saveStatus]);

  useEffect(() => () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
  }, []);

  // Rich text: sync contentEditable -> state
  const handleRichTextInput = useCallback(() => {
    if (editorRef.current) {
      const html = editorRef.current.innerHTML;
      setContent(html);
      scheduleSave(html);
    }
  }, [scheduleSave]);

  const saveRichSelection = useCallback(() => {
    const editor = editorRef.current;
    const selection = window.getSelection();
    if (!editor || !selection || selection.rangeCount === 0) return;
    const anchor = selection.anchorNode;
    if (anchor && editor.contains(anchor)) {
      richSelectionRef.current = selection.getRangeAt(0).cloneRange();
    }
  }, []);

  const restoreRichSelection = useCallback(() => {
    const editor = editorRef.current;
    const range = richSelectionRef.current;
    if (!editor || !range) return;
    const selection = window.getSelection();
    if (!selection) return;
    selection.removeAllRanges();
    selection.addRange(range);
  }, []);

  const getRichRange = useCallback(() => {
    restoreRichSelection();
    const editor = editorRef.current;
    const selection = window.getSelection();
    if (!editor || !selection || selection.rangeCount === 0) return null;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return null;
    return range;
  }, [restoreRichSelection]);

  const focusRichEditorAfterChange = useCallback(() => {
    editorRef.current?.focus();
    handleRichTextInput();
    saveRichSelection();
  }, [handleRichTextInput, saveRichSelection]);

  const getActiveRichTableCell = useCallback(() => {
    restoreRichSelection();
    const editor = editorRef.current;
    const selection = window.getSelection();
    const node = selection?.anchorNode;
    const element = node instanceof Element ? node : node?.parentElement;
    if (!editor || !element || !editor.contains(element)) return null;
    return element.closest("td, th") as HTMLTableCellElement | null;
  }, [restoreRichSelection]);

  const getRichSelectionBlocks = useCallback(() => {
    const editor = editorRef.current;
    if (!editor) return [];
    const range = getRichRange();
    const blockSelector = "p, div, li, h1, h2, h3, h4, h5, h6, blockquote, pre, td, th";

    if (!range || range.collapsed) {
      const selection = window.getSelection();
      const node = selection?.anchorNode;
      const element = node instanceof Element ? node : node?.parentElement;
      const block = element?.closest(blockSelector);
      return block && editor.contains(block) ? [block as HTMLElement] : [editor];
    }

    const blocks = Array.from(editor.querySelectorAll<HTMLElement>(blockSelector))
      .filter((block) => {
        try {
          return range.intersectsNode(block);
        } catch {
          return false;
        }
      });
    return blocks.length > 0 ? blocks : [editor];
  }, [getRichRange]);

  const applyRichTextBlockStyles = useCallback((styles: Record<string, string>) => {
    const blocks = getRichSelectionBlocks();
    blocks.forEach((block) => {
      Object.entries(styles).forEach(([property, value]) => {
        block.style.setProperty(property, value);
      });
    });
    focusRichEditorAfterChange();
  }, [focusRichEditorAfterChange, getRichSelectionBlocks]);

  // Set initial HTML for rich text mode (including DOCX)
  useEffect(() => {
    if (mode === "richtext" && editorRef.current) {
      if (isDocx && docxHtml != null) {
        editorRef.current.innerHTML = docxHtml;
      } else if (contentData?.content != null) {
        editorRef.current.innerHTML = contentData.content;
      }
    }
  }, [mode, contentData, isDocx, docxHtml]);

  // Rich text toolbar command
  const execCmd = useCallback((cmd: string, value?: string) => {
    restoreRichSelection();
    document.execCommand(cmd, false, value);
    focusRichEditorAfterChange();
  }, [focusRichEditorAfterChange, restoreRichSelection]);

  const applyRichTextBlock = useCallback((value: string) => {
    setRichTextBlock(value);
    execCmd("formatBlock", value);
  }, [execCmd]);

  const applyRichTextFont = useCallback((value: string) => {
    setRichTextFont(value);
    execCmd("fontName", value);
  }, [execCmd]);

  const applyRichTextFontSize = useCallback((value: string) => {
    setRichTextSize(value);
    restoreRichSelection();
    document.execCommand("fontSize", false, "7");
    editorRef.current?.querySelectorAll('font[size="7"]').forEach((node) => {
      const span = document.createElement("span");
      span.style.fontSize = `${value}px`;
      span.innerHTML = (node as HTMLElement).innerHTML;
      node.replaceWith(span);
    });
    editorRef.current?.focus();
    handleRichTextInput();
    saveRichSelection();
  }, [handleRichTextInput, restoreRichSelection, saveRichSelection]);

  const insertRichTextTable = useCallback((rows = 3, cols = 3) => {
    const headerCells = Array.from({ length: cols }, () => "<th>Header</th>").join("");
    const bodyRows = Array.from({ length: Math.max(1, rows - 1) }, () => (
      `<tr>${Array.from({ length: cols }, () => "<td><br></td>").join("")}</tr>`
    )).join("");
    execCmd(
      "insertHTML",
      `<table><tbody><tr>${headerCells}</tr>${bodyRows}</tbody></table><p><br></p>`,
    );
  }, [execCmd]);

  const insertRichTextImage = useCallback(() => {
    const url = window.prompt("Image URL", "https://");
    if (!url) return;
    execCmd("insertImage", url);
  }, [execCmd]);

  const insertRichTextCustomTable = useCallback(() => {
    const rows = Number(window.prompt("Rows", "3"));
    const cols = Number(window.prompt("Columns", "3"));
    if (!Number.isFinite(rows) || !Number.isFinite(cols)) return;
    insertRichTextTable(Math.max(1, Math.min(20, Math.floor(rows))), Math.max(1, Math.min(12, Math.floor(cols))));
  }, [insertRichTextTable]);

  const insertRichTextPageBreak = useCallback(() => {
    execCmd(
      "insertHTML",
      '<div class="doc-editor-page-break" data-docx-page-break="true" contenteditable="false"><span>Page break</span></div><p><br></p>',
    );
  }, [execCmd]);

  const insertRichTextCallout = useCallback(() => {
    execCmd(
      "insertHTML",
      '<blockquote class="doc-editor-callout"><strong>Note</strong><br><br></blockquote><p><br></p>',
    );
  }, [execCmd]);

  const insertRichTextChecklist = useCallback(() => {
    execCmd(
      "insertHTML",
      '<ul class="doc-editor-checklist"><li><span class="doc-editor-checkbox">&#9744;</span> Item</li></ul><p><br></p>',
    );
  }, [execCmd]);

  const applyRichTextLink = useCallback(() => {
    const url = window.prompt(t("page.doc_editor.enter_url"), "https://");
    if (url) execCmd("createLink", url);
  }, [execCmd]);

  const handleRichTextInsert = useCallback((key: string) => {
    if (key === "table-2") insertRichTextTable(2, 2);
    if (key === "table-3") insertRichTextTable(3, 3);
    if (key === "table") insertRichTextTable(3, 3);
    if (key === "table-custom") insertRichTextCustomTable();
    if (key === "image") insertRichTextImage();
    if (key === "divider") execCmd("insertHorizontalRule");
    if (key === "page-break") insertRichTextPageBreak();
    if (key === "date") execCmd("insertText", new Date().toLocaleDateString());
    if (key === "callout") insertRichTextCallout();
    if (key === "checklist") insertRichTextChecklist();
    if (key === "clear") execCmd("removeFormat");
  }, [
    execCmd,
    insertRichTextCallout,
    insertRichTextChecklist,
    insertRichTextCustomTable,
    insertRichTextImage,
    insertRichTextPageBreak,
    insertRichTextTable,
  ]);

  const handleRichTextTableAction = useCallback((key: string) => {
    const cell = getActiveRichTableCell();
    const row = cell?.parentElement as HTMLTableRowElement | null;
    const table = cell?.closest("table");
    if (!cell || !row || !table) {
      window.alert("Place the cursor inside a table first.");
      return;
    }

    if (key === "row-below") {
      const nextRow = row.cloneNode(true) as HTMLTableRowElement;
      Array.from(nextRow.cells).forEach((nextCell) => { nextCell.innerHTML = "<br>"; });
      row.after(nextRow);
      focusRichEditorAfterChange();
      return;
    }

    if (key === "row-above") {
      const nextRow = row.cloneNode(true) as HTMLTableRowElement;
      Array.from(nextRow.cells).forEach((nextCell) => { nextCell.innerHTML = "<br>"; });
      row.before(nextRow);
      focusRichEditorAfterChange();
      return;
    }

    if (key === "column-right" || key === "column-left") {
      const columnIndex = cell.cellIndex + (key === "column-right" ? 1 : 0);
      Array.from(table.rows).forEach((tableRow) => {
        const reference = tableRow.cells[Math.max(0, Math.min(cell.cellIndex, tableRow.cells.length - 1))];
        const tagName = reference?.tagName.toLowerCase() === "th" ? "th" : "td";
        const nextCell = document.createElement(tagName);
        nextCell.innerHTML = "<br>";
        tableRow.insertBefore(nextCell, tableRow.cells[columnIndex] || null);
      });
      focusRichEditorAfterChange();
      return;
    }

    if (key === "header-row") {
      Array.from(table.rows).forEach((tableRow, rowIndex) => {
        Array.from(tableRow.cells).forEach((tableCell) => {
          const tagName = rowIndex === 0 ? "th" : "td";
          if (tableCell.tagName.toLowerCase() === tagName) return;
          const nextCell = document.createElement(tagName);
          nextCell.innerHTML = tableCell.innerHTML;
          nextCell.colSpan = tableCell.colSpan;
          nextCell.rowSpan = tableCell.rowSpan;
          nextCell.style.cssText = (tableCell as HTMLElement).style.cssText;
          tableCell.replaceWith(nextCell);
        });
      });
      focusRichEditorAfterChange();
      return;
    }

    if (key === "merge-right") {
      const rightCell = row.cells[cell.cellIndex + 1];
      if (!rightCell) return;
      cell.colSpan = (cell.colSpan || 1) + (rightCell.colSpan || 1);
      const separator = cell.textContent?.trim() && rightCell.textContent?.trim() ? " " : "";
      cell.innerHTML = `${cell.innerHTML}${separator}${rightCell.innerHTML}`;
      rightCell.remove();
      focusRichEditorAfterChange();
      return;
    }

    if (key === "split-cell") {
      if (cell.colSpan > 1) {
        cell.colSpan -= 1;
      }
      const nextCell = document.createElement(cell.tagName.toLowerCase() === "th" ? "th" : "td");
      nextCell.innerHTML = "<br>";
      cell.after(nextCell);
      focusRichEditorAfterChange();
      return;
    }

    if (key === "delete-row") {
      if (table.rows.length <= 1) {
        table.remove();
      } else {
        row.remove();
      }
      focusRichEditorAfterChange();
      return;
    }

    if (key === "delete-column") {
      const columnIndex = cell.cellIndex;
      Array.from(table.rows).forEach((tableRow) => {
        if (tableRow.cells.length <= 1) {
          tableRow.remove();
        } else {
          tableRow.cells[columnIndex]?.remove();
        }
      });
      if (table.rows.length === 0) table.remove();
      focusRichEditorAfterChange();
      return;
    }

    if (key === "delete-table") {
      table.remove();
      focusRichEditorAfterChange();
    }
  }, [focusRichEditorAfterChange, getActiveRichTableCell]);

  const handleRichTextLayoutAction = useCallback((key: string) => {
    if (key === "align-left") execCmd("justifyLeft");
    if (key === "align-center") execCmd("justifyCenter");
    if (key === "align-right") execCmd("justifyRight");
    if (key === "align-justify") execCmd("justifyFull");
    if (key === "bullet") execCmd("insertUnorderedList");
    if (key === "numbered") execCmd("insertOrderedList");
    if (key === "indent") execCmd("indent");
    if (key === "outdent") execCmd("outdent");
    if (key === "line-1") applyRichTextBlockStyles({ "line-height": "1.25" });
    if (key === "line-15") applyRichTextBlockStyles({ "line-height": "1.5" });
    if (key === "line-2") applyRichTextBlockStyles({ "line-height": "2" });
    if (key === "space-tight") applyRichTextBlockStyles({ "margin-bottom": "6px" });
    if (key === "space-normal") applyRichTextBlockStyles({ "margin-bottom": "12px" });
    if (key === "space-loose") applyRichTextBlockStyles({ "margin-bottom": "20px" });
  }, [applyRichTextBlockStyles, execCmd]);

  const selectRichTextMatch = useCallback((query: string) => {
    const editor = editorRef.current;
    const needle = query.trim();
    if (!editor || !needle) return false;
    const haystack = editor.textContent || "";
    const matchIndex = haystack.toLowerCase().indexOf(needle.toLowerCase());
    if (matchIndex < 0) return false;

    const range = document.createRange();
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let offset = 0;
    let startSet = false;
    let current = walker.nextNode();

    while (current) {
      const text = current.textContent || "";
      const nextOffset = offset + text.length;
      if (!startSet && matchIndex >= offset && matchIndex <= nextOffset) {
        range.setStart(current, matchIndex - offset);
        startSet = true;
      }
      if (startSet && matchIndex + needle.length >= offset && matchIndex + needle.length <= nextOffset) {
        range.setEnd(current, matchIndex + needle.length - offset);
        break;
      }
      offset = nextOffset;
      current = walker.nextNode();
    }

    const selection = window.getSelection();
    if (!selection || range.collapsed) return false;
    selection.removeAllRanges();
    selection.addRange(range);
    richSelectionRef.current = range.cloneRange();
    editor.focus();
    return true;
  }, []);

  const findRichText = useCallback(() => {
    const query = window.prompt("Find text");
    if (!query) return;
    if (!selectRichTextMatch(query)) window.alert("No match found.");
  }, [selectRichTextMatch]);

  const replaceFirstRichText = useCallback(() => {
    const query = window.prompt("Find text");
    if (!query) return;
    const replacement = window.prompt("Replace with", "");
    if (replacement == null) return;
    if (!selectRichTextMatch(query)) {
      window.alert("No match found.");
      return;
    }
    document.execCommand("insertText", false, replacement);
    focusRichEditorAfterChange();
  }, [focusRichEditorAfterChange, selectRichTextMatch]);

  const transformRichTextSelection = useCallback((transform: (value: string) => string) => {
    const range = getRichRange();
    const selectedText = range?.toString() || "";
    if (!range || !selectedText) return;
    document.execCommand("insertText", false, transform(selectedText));
    focusRichEditorAfterChange();
  }, [focusRichEditorAfterChange, getRichRange]);

  const selectAllRichText = useCallback(() => {
    const editor = editorRef.current;
    if (!editor) return;
    const range = document.createRange();
    range.selectNodeContents(editor);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    richSelectionRef.current = range.cloneRange();
    editor.focus();
  }, []);

  const clearRichTextDocument = useCallback(() => {
    if (!editorRef.current) return;
    if (!window.confirm("Clear all content in this document?")) return;
    editorRef.current.innerHTML = "<p><br></p>";
    focusRichEditorAfterChange();
  }, [focusRichEditorAfterChange]);

  const handleRichTextToolsAction = useCallback((key: string) => {
    if (key === "find") findRichText();
    if (key === "replace") replaceFirstRichText();
    if (key === "select-all") selectAllRichText();
    if (key === "upper") transformRichTextSelection((value) => value.toUpperCase());
    if (key === "lower") transformRichTextSelection((value) => value.toLowerCase());
    if (key === "title") {
      transformRichTextSelection((value) => value.replace(/\S+/g, (word) => (
        word.slice(0, 1).toUpperCase() + word.slice(1).toLowerCase()
      )));
    }
    if (key === "clear-format") execCmd("removeFormat");
    if (key === "clear-document") clearRichTextDocument();
  }, [clearRichTextDocument, execCmd, findRichText, replaceFirstRichText, selectAllRichText, transformRichTextSelection]);

  const handleRichTextKeyDown = useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Tab") {
      event.preventDefault();
      execCmd(event.shiftKey ? "outdent" : "indent");
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      applyRichTextLink();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      findRichText();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "x") {
      event.preventDefault();
      execCmd("strikeThrough");
    }
  }, [applyRichTextLink, execCmd, findRichText]);

  const applyTextareaEdit = useCallback((textarea: HTMLTextAreaElement, next: string, selectionStart: number, selectionEnd = selectionStart) => {
    handleContentChange(next);
    requestAnimationFrame(() => {
      textarea.selectionStart = selectionStart;
      textarea.selectionEnd = selectionEnd;
      textarea.focus();
    });
  }, [handleContentChange]);

  const applyMarkdownEdit = useCallback((build: (selection: { start: number; end: number; selected: string }) => MarkdownEditResult) => {
    const textarea = markdownRef.current;
    const start = textarea?.selectionStart ?? content.length;
    const end = textarea?.selectionEnd ?? content.length;
    const result = build({ start, end, selected: content.slice(start, end) });
    if (textarea) {
      applyTextareaEdit(textarea, result.next, result.selectionStart, result.selectionEnd ?? result.selectionStart);
    } else {
      handleContentChange(result.next);
    }
  }, [applyTextareaEdit, content, handleContentChange]);

  const wrapMarkdownSelection = useCallback((before: string, after = before, placeholder = "text") => {
    applyMarkdownEdit(({ start, end, selected }) => {
      const value = selected || placeholder;
      const next = `${content.slice(0, start)}${before}${value}${after}${content.slice(end)}`;
      return {
        next,
        selectionStart: start + before.length,
        selectionEnd: start + before.length + value.length,
      };
    });
  }, [applyMarkdownEdit, content]);

  const editMarkdownLines = useCallback((transform: (line: string, index: number) => string) => {
    applyMarkdownEdit(({ start, end }) => {
      const blockStart = content.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      const nextBreak = content.indexOf("\n", end);
      const blockEnd = nextBreak === -1 ? content.length : nextBreak;
      const lines = content.slice(blockStart, blockEnd).split("\n");
      const replacement = lines.map(transform).join("\n");
      const next = content.slice(0, blockStart) + replacement + content.slice(blockEnd);
      return {
        next,
        selectionStart: blockStart,
        selectionEnd: blockStart + replacement.length,
      };
    });
  }, [applyMarkdownEdit, content]);

  const applyMarkdownHeading = useCallback((level: 1 | 2 | 3) => {
    const prefix = `${"#".repeat(level)} `;
    editMarkdownLines((line) => `${prefix}${line.replace(/^#{1,6}\s+/, "") || "Heading"}`);
  }, [editMarkdownLines]);

  const prefixMarkdownLines = useCallback((prefix: string) => {
    editMarkdownLines((line, index) => {
      const clean = line.replace(/^\s*(?:[-*+]|\d+\.|- \[[ xX]\]|>)\s+/, "");
      return `${prefix.replace("{n}", String(index + 1))}${clean || "List item"}`;
    });
  }, [editMarkdownLines]);

  const insertMarkdownBlock = useCallback((block: string, cursorOffset?: number) => {
    applyMarkdownEdit(({ start, end }) => {
      const needsLeadingBreak = start > 0 && content[start - 1] !== "\n";
      const needsTrailingBreak = end < content.length && content[end] !== "\n";
      const insert = `${needsLeadingBreak ? "\n\n" : ""}${block}${needsTrailingBreak ? "\n\n" : ""}`;
      const next = content.slice(0, start) + insert + content.slice(end);
      return {
        next,
        selectionStart: start + (cursorOffset == null ? insert.length : (needsLeadingBreak ? 2 : 0) + cursorOffset),
      };
    });
  }, [applyMarkdownEdit, content]);

  const insertMarkdownLink = useCallback(() => {
    const textarea = markdownRef.current;
    const start = textarea?.selectionStart ?? content.length;
    const end = textarea?.selectionEnd ?? content.length;
    const selected = content.slice(start, end) || "link text";
    const url = window.prompt("Link URL", "https://");
    if (!url) return;
    applyMarkdownEdit(() => {
      const snippet = `[${selected}](${url})`;
      const next = content.slice(0, start) + snippet + content.slice(end);
      return { next, selectionStart: start + 1, selectionEnd: start + 1 + selected.length };
    });
  }, [applyMarkdownEdit, content]);

  const insertMarkdownImage = useCallback(() => {
    const url = window.prompt("Image URL", "https://");
    if (!url) return;
    const alt = window.prompt("Alt text", "image") || "image";
    insertMarkdownBlock(`![${alt}](${url})`);
  }, [insertMarkdownBlock]);

  const insertMarkdownWikiLink = useCallback(() => {
    const textarea = markdownRef.current;
    const start = textarea?.selectionStart ?? content.length;
    const end = textarea?.selectionEnd ?? content.length;
    const selected = content.slice(start, end) || "Page name";
    applyMarkdownEdit(() => {
      const snippet = `[[${selected}]]`;
      const next = content.slice(0, start) + snippet + content.slice(end);
      return { next, selectionStart: start + 2, selectionEnd: start + 2 + selected.length };
    });
  }, [applyMarkdownEdit, content]);

  const applyPlainTextEdit = useCallback((build: (selection: { start: number; end: number; selected: string }) => MarkdownEditResult) => {
    const textarea = textRef.current;
    const start = textarea?.selectionStart ?? content.length;
    const end = textarea?.selectionEnd ?? content.length;
    const result = build({ start, end, selected: content.slice(start, end) });
    if (textarea) {
      applyTextareaEdit(textarea, result.next, result.selectionStart, result.selectionEnd ?? result.selectionStart);
    } else {
      handleContentChange(result.next);
    }
  }, [applyTextareaEdit, content, handleContentChange]);

  const runPlainTextNativeCommand = useCallback((command: "undo" | "redo") => {
    const textarea = textRef.current;
    if (!textarea) return;
    textarea.focus();
    document.execCommand(command);
    requestAnimationFrame(() => {
      if (textarea.value !== content) handleContentChange(textarea.value);
    });
  }, [content, handleContentChange]);

  const insertPlainTextBlock = useCallback((block: string) => {
    applyPlainTextEdit(({ start, end }) => {
      const needsLeadingBreak = start > 0 && content[start - 1] !== "\n";
      const needsTrailingBreak = end < content.length && content[end] !== "\n";
      const insert = `${needsLeadingBreak ? "\n" : ""}${block}${needsTrailingBreak ? "\n" : ""}`;
      const next = content.slice(0, start) + insert + content.slice(end);
      return { next, selectionStart: start + insert.length };
    });
  }, [applyPlainTextEdit, content]);

  const editPlainTextLines = useCallback((transform: (lines: string[]) => string[]) => {
    applyPlainTextEdit(({ start, end }) => {
      const hasSelection = start !== end;
      const blockStart = hasSelection ? content.lastIndexOf("\n", Math.max(0, start - 1)) + 1 : 0;
      const nextBreak = hasSelection ? content.indexOf("\n", end) : -1;
      const blockEnd = hasSelection ? (nextBreak === -1 ? content.length : nextBreak) : content.length;
      const replacement = transform(content.slice(blockStart, blockEnd).split("\n")).join("\n");
      const next = content.slice(0, blockStart) + replacement + content.slice(blockEnd);
      return { next, selectionStart: blockStart, selectionEnd: blockStart + replacement.length };
    });
  }, [applyPlainTextEdit, content]);

  const prefixPlainTextLines = useCallback((prefix: string) => {
    editPlainTextLines((lines) => lines.map((line, index) => `${prefix.replace("{n}", String(index + 1))}${line}`));
  }, [editPlainTextLines]);

  const transformPlainTextSelection = useCallback((transform: (value: string) => string) => {
    applyPlainTextEdit(({ start, end, selected }) => {
      const source = selected || content;
      const offset = selected ? start : 0;
      const replacement = transform(source);
      const next = selected
        ? content.slice(0, start) + replacement + content.slice(end)
        : replacement;
      return { next, selectionStart: offset, selectionEnd: offset + replacement.length };
    });
  }, [applyPlainTextEdit, content]);

  const selectAllPlainText = useCallback(() => {
    const textarea = textRef.current;
    if (!textarea) return;
    textarea.focus();
    textarea.select();
  }, []);

  const copyPlainTextSelection = useCallback(() => {
    const textarea = textRef.current;
    const text = textarea
      ? textarea.value.slice(textarea.selectionStart, textarea.selectionEnd) || textarea.value
      : content;
    void navigator.clipboard?.writeText(text);
  }, [content]);

  const findPlainText = useCallback(() => {
    const needle = window.prompt("Find text");
    if (!needle) return;
    const textarea = textRef.current;
    const from = textarea?.selectionEnd ?? 0;
    let index = content.indexOf(needle, from);
    if (index === -1 && from > 0) index = content.indexOf(needle);
    if (index === -1) return;
    requestAnimationFrame(() => {
      textarea?.focus();
      if (textarea) {
        textarea.selectionStart = index;
        textarea.selectionEnd = index + needle.length;
      }
    });
  }, [content]);

  const replaceFirstPlainText = useCallback(() => {
    const needle = window.prompt("Find text to replace");
    if (!needle) return;
    const replacement = window.prompt("Replace with", "") ?? "";
    const index = content.indexOf(needle);
    if (index === -1) return;
    const next = content.slice(0, index) + replacement + content.slice(index + needle.length);
    const textarea = textRef.current;
    if (textarea) {
      applyTextareaEdit(textarea, next, index, index + replacement.length);
    } else {
      handleContentChange(next);
    }
  }, [applyTextareaEdit, content, handleContentChange]);

  const clearPlainTextDocument = useCallback(() => {
    if (!window.confirm("Clear all content in this text file?")) return;
    const textarea = textRef.current;
    if (textarea) {
      applyTextareaEdit(textarea, "", 0);
    } else {
      handleContentChange("");
    }
  }, [applyTextareaEdit, handleContentChange]);

  const handlePlainTextInsertAction = useCallback((key: string) => {
    if (key === "date") insertPlainTextBlock(new Date().toLocaleDateString());
    if (key === "time") insertPlainTextBlock(new Date().toLocaleString());
    if (key === "divider") insertPlainTextBlock("------------------------------------------------------------");
    if (key === "bullet") prefixPlainTextLines("- ");
    if (key === "numbered") prefixPlainTextLines("{n}. ");
  }, [insertPlainTextBlock, prefixPlainTextLines]);

  const handlePlainTextToolsAction = useCallback((key: string) => {
    if (key === "find") findPlainText();
    if (key === "replace") replaceFirstPlainText();
    if (key === "select-all") selectAllPlainText();
    if (key === "upper") transformPlainTextSelection((value) => value.toUpperCase());
    if (key === "lower") transformPlainTextSelection((value) => value.toLowerCase());
    if (key === "title") {
      transformPlainTextSelection((value) => value.replace(/\S+/g, (word) => (
        word.slice(0, 1).toUpperCase() + word.slice(1).toLowerCase()
      )));
    }
    if (key === "sort") editPlainTextLines((lines) => [...lines].sort((a, b) => a.localeCompare(b)));
    if (key === "dedupe") editPlainTextLines((lines) => Array.from(new Set(lines)));
    if (key === "trim") editPlainTextLines((lines) => lines.map((line) => line.replace(/[ \t]+$/g, "")));
    if (key === "clear-document") clearPlainTextDocument();
  }, [clearPlainTextDocument, editPlainTextLines, findPlainText, replaceFirstPlainText, selectAllPlainText, transformPlainTextSelection]);

  // Plain text editors: Tab indents selections, Shift+Tab outdents, Markdown supports bold/italic shortcuts.
  const handlePlainTextKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>, options: { markdown?: boolean } = {}) => {
      const ta = e.currentTarget;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const key = e.key.toLowerCase();

      if (options.markdown && (e.metaKey || e.ctrlKey) && (key === "b" || key === "i")) {
        e.preventDefault();
        const marker = key === "b" ? "**" : "*";
        const selected = content.slice(start, end);
        const next = content.slice(0, start) + marker + selected + marker + content.slice(end);
        applyTextareaEdit(ta, next, start + marker.length, end + marker.length);
        return;
      }

      if (options.markdown && (e.metaKey || e.ctrlKey) && key === "k") {
        e.preventDefault();
        insertMarkdownLink();
        return;
      }

      if (e.key === "Tab") {
        e.preventDefault();
        if (start !== end) {
          const blockStart = content.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
          const nextBreak = content.indexOf("\n", end);
          const blockEnd = nextBreak === -1 ? content.length : nextBreak;
          const block = content.slice(blockStart, blockEnd);
          const lines = block.split("\n");

          if (e.shiftKey) {
            let removedBeforeSelection = 0;
            let removedTotal = 0;
            let offset = 0;
            const outdented = lines.map((line) => {
              const removeCount = line.startsWith("  ") ? 2 : line.startsWith("\t") || line.startsWith(" ") ? 1 : 0;
              const absoluteLineStart = blockStart + offset;
              if (absoluteLineStart < start) removedBeforeSelection += removeCount;
              removedTotal += removeCount;
              offset += line.length + 1;
              return removeCount > 0 ? line.slice(removeCount) : line;
            }).join("\n");
            const next = content.slice(0, blockStart) + outdented + content.slice(blockEnd);
            applyTextareaEdit(ta, next, Math.max(blockStart, start - removedBeforeSelection), Math.max(blockStart, end - removedTotal));
          } else {
            const indented = lines.map((line) => `  ${line}`).join("\n");
            const next = content.slice(0, blockStart) + indented + content.slice(blockEnd);
            applyTextareaEdit(ta, next, start + 2, end + lines.length * 2);
          }
          return;
        }

        if (e.shiftKey) {
          const lineStart = content.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
          const beforeCursor = content.slice(lineStart, start);
          const removeCount = beforeCursor.endsWith("  ") ? 2 : beforeCursor.endsWith("\t") || beforeCursor.endsWith(" ") ? 1 : 0;
          if (removeCount > 0) {
            const next = content.slice(0, start - removeCount) + content.slice(start);
            applyTextareaEdit(ta, next, start - removeCount);
          }
        } else {
          const next = content.slice(0, start) + "  " + content.slice(end);
          applyTextareaEdit(ta, next, start + 2);
        }
      }
    },
    [applyTextareaEdit, content, insertMarkdownLink],
  );

  // Markdown preview data
  const markdownPreviewSource = useMemo(() => (
    mode === "markdown" ? markdownWithWikiLinks(content) : ""
  ), [content, mode]);
  const markdownWikiLinksByTarget = useMemo(
    () => wikiLinkMap((wikiLinkData?.links || []) as WikiLinkInfo[]),
    [wikiLinkData?.links],
  );
  const markdownHeadings = useMemo(() => (
    content
      .split("\n")
      .map((line, index) => {
        const match = /^(#{1,3})\s+(.+)$/.exec(line);
        if (!match) return null;
        return {
          id: `${index}-${match[2].toLowerCase().replace(/[^\w\u4e00-\u9fff]+/g, "-")}`,
          level: match[1].length,
          text: match[2].replace(/[*_`[\]()#]/g, "").trim(),
          line: index + 1,
        };
      })
      .filter(Boolean) as Array<{ id: string; level: number; text: string; line: number }>
  ), [content]);
  const markdownStats = useMemo(() => {
    const words = markdownWordCount(content);
    return {
      lines: content ? content.split("\n").length : 1,
      headings: markdownHeadings.length,
      readingMinutes: Math.max(1, Math.ceil(words / 220)),
    };
  }, [content, markdownHeadings.length]);

  const textareaForAnchorMode = useCallback((anchorMode?: string) => {
    const targetMode = anchorMode || mode;
    if (targetMode === "markdown") return markdownRef.current;
    if (targetMode === "text") return textRef.current;
    if (targetMode === "code") return codeRef.current;
    return null;
  }, [mode]);

  const buildCurrentCommentAnchor = useCallback((): CommentAnchor | null => {
    const textarea = textareaForAnchorMode(mode);
    if (textarea) {
      return textLineAnchor(content, textarea.selectionStart, textarea.selectionEnd, mode);
    }

    if (mode === "richtext") {
      const range = getRichRange();
      const quote = trimCommentQuote(range?.toString() || "");
      return quote
        ? { type: "richtext_selection", mode, quote }
        : { type: "document", mode, label: docName };
    }

    return { type: "document", mode, label: docName };
  }, [content, docName, getRichRange, mode, textareaForAnchorMode]);

  const refreshCommentAnchor = useCallback(() => {
    setCommentAnchor(buildCurrentCommentAnchor());
  }, [buildCurrentCommentAnchor]);

  const handleCommentsLoaded = useCallback((comments: Comment[]) => {
    setDocumentComments(comments);
  }, []);

  const anchoredDocumentComments = useMemo(
    () => flattenComments(documentComments).filter((comment) => !comment.parent_id && comment.anchor && Object.keys(comment.anchor).length > 0),
    [documentComments],
  );

  const commentLineCounts = useMemo(() => {
    const counts = new Map<number, number>();
    for (const comment of anchoredDocumentComments) {
      const line = Number(comment.anchor?.line || 0);
      if (line > 0) counts.set(line, (counts.get(line) || 0) + 1);
    }
    return counts;
  }, [anchoredDocumentComments]);

  const firstCommentForLine = useCallback(
    (line: number) => anchoredDocumentComments.find((comment) => Number(comment.anchor?.line || 0) === line),
    [anchoredDocumentComments],
  );

  const selectTextareaAnchor = useCallback((textarea: HTMLTextAreaElement, anchor: CommentAnchor) => {
    const start = Number.isFinite(anchor.start) ? Math.max(0, Number(anchor.start)) : 0;
    const end = Number.isFinite(anchor.end) ? Math.max(start, Number(anchor.end)) : start;
    const line = Number(anchor.line || 1);
    textarea.focus();
    textarea.selectionStart = Math.min(start, textarea.value.length);
    textarea.selectionEnd = Math.min(end, textarea.value.length);
    textarea.scrollTop = Math.max(0, (line - 3) * 24);
  }, []);

  const handleSelectDocumentComment = useCallback((comment: Comment) => {
    setActiveCommentId(comment.id);
    setShowComments(true);
    const anchor = comment.anchor;
    if (!anchor) return;

    if (anchor.mode === "markdown") {
      setMarkdownViewMode((current) => current === "preview" ? "split" : current);
    }

    requestAnimationFrame(() => {
      const textarea = textareaForAnchorMode(anchor.mode);
      if (textarea && (anchor.start != null || anchor.line != null)) {
        selectTextareaAnchor(textarea, anchor);
        return;
      }
      if (anchor.mode === "richtext" && anchor.quote) {
        selectRichTextMatch(anchor.quote);
      }
    });
  }, [selectRichTextMatch, selectTextareaAnchor, textareaForAnchorMode]);

  const renderCommentAnchorRail = (totalLines: number) => {
    const lineComments = anchoredDocumentComments.filter((comment) => Number(comment.anchor?.line || 0) > 0);
    if (lineComments.length === 0) return null;
    const denominator = Math.max(1, totalLines - 1);
    return (
      <div className="doc-comment-anchor-rail" aria-label={t("page.tasks.comments")}>
        {lineComments.map((comment) => {
          const line = Math.max(1, Number(comment.anchor?.line || 1));
          const top = Math.min(92, Math.max(8, ((line - 1) / denominator) * 84 + 8));
          return (
            <button
              key={comment.id}
              type="button"
              className={activeCommentId === comment.id ? "doc-comment-anchor-dot is-active" : "doc-comment-anchor-dot"}
              style={{ top: `${top}%` }}
              title={
                comment.anchor?.line_end && comment.anchor.line_end !== line
                  ? t("component.comment_thread.lines_range", { start: line, end: comment.anchor.line_end })
                  : t("component.comment_thread.line_number", { line })
              }
              onClick={() => handleSelectDocumentComment(comment)}
            >
              <IconComment size={11} />
            </button>
          );
        })}
      </div>
    );
  };

  const openMarkdownWikiTarget = useCallback(async (target: string) => {
    const link = markdownWikiLinksByTarget.get(wikiLinkKey(target));
    const docId = link?.document_id;
    if (!docId) return;
    const saved = await flushSave(content);
    if (saved) navigate(`/editor/${docId}`, { state: knowledgeReturnTo ? { knowledgeReturnTo } : undefined });
  }, [content, flushSave, knowledgeReturnTo, markdownWikiLinksByTarget, navigate]);

  // Status indicator
  const statusConfig: Record<string, { label: string; type: string }> = {
    saved: { label: t("page.blueprint_detail.saved"), type: "success" },
    saving: { label: t("page.task_collections.saving"), type: "warning" },
    unsaved: { label: t("page.doc_editor.unsaved_changes"), type: "orange" },
  };
  const statusInfo = statusConfig[saveStatus];
  const modeBadgeType = mode === "richtext" ? "purple" : mode === "markdown" ? "blue" : mode === "text" ? "gray" : mode === "spreadsheet" ? "green" : mode === "presentation" ? "orange" : mode === "diagram" ? "teal" : "teal";
  const modeLabel = isDocx ? "Word" : isPptx ? "Presentation" : mode === "richtext" ? "Rich Text" : mode === "markdown" ? "Markdown" : mode === "text" ? "Text" : mode === "spreadsheet" ? "Spreadsheet" : mode === "presentation" ? "Presentation" : mode === "diagram" ? "Diagram" : "Code";
  const usesInlineLiveDiff = mode === "text" || mode === "markdown" || mode === "code";

  const diagramDoc = useMemo(
    () => mode === "diagram" ? parseDiagramDocument(content, docName.replace(/\.(diagram\.json|diagram)$/i, "")) : null,
    [content, docName, mode],
  );

  const handleDiagramChange = useCallback(
    (nextDiagram: EditableDiagramDocument) => {
      const text = serializeDiagramDocument(nextDiagram);
      setContent(text);
      scheduleSave(text);
    },
    [scheduleSave],
  );

  const getEditorLiveContent = useCallback(() => {
    if (mode === "presentation") return slidesToText(pptxSlidesRef.current);
    if (mode === "spreadsheet") {
      return serializeSpreadsheetContent(
        normalizeSheetData(sheetDataRef.current),
        sheetChartsRef.current,
        isXlsx,
        sheetStylesRef.current,
      );
    }
    if (mode === "richtext" && editorRef.current) {
      return editorRef.current.innerHTML;
    }
    return contentRef.current;
  }, [isXlsx, mode]);

  const applyEditorLiveContent = useCallback((nextText: string, meta?: EditorLiveApplyMeta) => {
    const diff = meta?.diff || meta?.patch;
    if (diff && usesInlineLiveDiff) {
      setLiveDiff(diff);
      setLiveEditNotice(null);
    } else {
      setLiveDiff(null);
      setLiveEditNotice(`AI updated ${modeLabel}`);
    }

    if (mode === "presentation") {
      const nextSlides = textToSlides(nextText);
      handleSlidesChange(nextSlides);
      return;
    }

    if (mode === "spreadsheet") {
      const spreadsheetPayload = parseSpreadsheetPayload(nextText);
      const nextData = spreadsheetPayload
        ? spreadsheetPayload.data
        : parseCsvText(nextText);
      const nextCharts = spreadsheetPayload
        ? spreadsheetPayload.charts
        : sheetChartsRef.current;
      const nextStyles = spreadsheetPayload
        ? spreadsheetPayload.styles
        : sheetStylesRef.current;
      const serialized = serializeSpreadsheetContent(nextData, nextCharts, isXlsx, nextStyles);
      setSheetData(nextData);
      setSheetCharts(nextCharts);
      setSheetStyles(nextStyles);
      setContent(serialized);
      scheduleSave(serialized);
      return;
    }

    if (mode === "diagram") {
      try {
        JSON.parse(nextText);
      } catch {
        return;
      }
      const nextDiagram = parseDiagramDocument(
        nextText,
        docName.replace(/\.(diagram\.json|diagram)$/i, ""),
      );
      const serialized = serializeDiagramDocument(nextDiagram);
      setContent(serialized);
      scheduleSave(serialized);
      return;
    }

    if (mode === "richtext") {
      setContent(nextText);
      if (isDocx) setDocxHtml(nextText);
      if (editorRef.current) editorRef.current.innerHTML = nextText;
      scheduleSave(nextText);
      return;
    }

    setContent(nextText);
    scheduleSave(nextText);
  }, [docName, handleSlidesChange, isDocx, isXlsx, mode, modeLabel, scheduleSave, usesInlineLiveDiff]);

  const openLiveEdit = useCallback(() => {
    openEditorLiveChat({
      documentId: docId,
      documentName: docName,
      fileType: doc?.file_type,
      mimeType: doc?.mime_type,
      editorType: modeLabel,
      getContent: getEditorLiveContent,
      applyContent: (next, meta) => applyEditorLiveContent(next, meta),
    });
  }, [applyEditorLiveContent, doc?.file_type, doc?.mime_type, docId, docName, getEditorLiveContent, modeLabel]);

  const isLoadingContent = contentLoading || docxLoading || xlsxLoading || pptxLoading;
  const markdownEditorLayoutMode = liveDiff && markdownViewMode === "preview"
    ? "split"
    : markdownViewMode;

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="manor-editor-shell">
      {/* Header bar */}
      <div className="manor-editor-header">
        <button
          onClick={goBackToKnowledge}
          className="btn-manor-ghost"
          title={t("page.doc_editor.back_to_knowledge_base")}
          style={{ width: 36, height: 36, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <IconArrowLeft size={18} />
        </button>

        <div className="manor-editor-header-main">
          <h1 className="manor-editor-title">
            {docName}
          </h1>
          <StatusBadge type="gray">{extLabel(docName)}</StatusBadge>
          <StatusBadge type={modeBadgeType}>{modeLabel}</StatusBadge>
        </div>

        {liveEditNotice && <StatusBadge type="teal" dot>{liveEditNotice}</StatusBadge>}

        <StatusBadge type={statusInfo.type} dot>{statusInfo.label}</StatusBadge>

        <AiEditButton onClick={openLiveEdit} />

        {doc && (
          <button
            onClick={() => {
              setCommentAnchor(buildCurrentCommentAnchor());
              setShowComments((open) => {
                const next = !open;
                if (next) setShowVersions(false);
                return next;
              });
            }}
            className={showComments ? "btn-manor-neutral-light" : "btn-manor-ghost"}
            title={t("page.tasks.comments")}
            style={{ width: 36, height: 36, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
          >
            <IconComment size={18} />
          </button>
        )}

        {(mode === "code" && isRenderableCodeFile(docName)) && (
          <button
            onClick={() => setShowPreview((p) => !p)}
            className={showPreview ? "btn-manor-teal-light" : "btn-manor-ghost"}
            style={{ fontSize: 12, fontWeight: 600, padding: "6px 14px" }}
          >
            {t("page.doc_editor.preview")}
          </button>
        )}

        <button
          onClick={() => void flushSave(content)}
          disabled={saveMutation.isPending || presentationSaveMutation.isPending || !canEditCurrentDoc}
          className="btn-manor"
          style={{ fontSize: 12, padding: "6px 16px" }}
        >
          {t("action.save")}
        </button>

        <button
          onClick={() => {
            setShowVersions((open) => {
              const next = !open;
              if (next) setShowComments(false);
              return next;
            });
          }}
          className={showVersions ? "btn-manor-teal-light" : "btn-manor-ghost"}
          title={t("page.doc_editor.version_history")}
          style={{ width: 36, height: 36, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <IconClock size={18} />
        </button>
      </div>

      {/* Toolbar (rich text + docx) */}
      {(mode === "richtext") && (
        <div className="manor-editor-toolbar richtext-editor-toolbar">
          <ToolbarGroup>
            <ToolbarBtn title={t("page.doc_editor.undo_ctrl_plus_z")} onClick={() => execCmd("undo")} icon={<IconUndo size={15} />} />
            <ToolbarBtn title={t("page.doc_editor.redo_ctrl_plus_shift_plus_z")} onClick={() => execCmd("redo")} icon={<IconRedo size={15} />} />
          </ToolbarGroup>
          <ToolbarSep />
          <ToolbarGroup style={{ gap: 6 }}>
            <Select
              value={richTextBlock}
              onChange={applyRichTextBlock}
              options={RICH_TEXT_BLOCK_OPTIONS}
              style={{ width: 132 }}
              buttonStyle={richTextSelectButtonStyle}
            />
            <Select
              value={richTextFont}
              onChange={applyRichTextFont}
              options={RICH_TEXT_FONTS}
              style={{ width: 138 }}
              buttonStyle={richTextSelectButtonStyle}
            />
            <Select
              value={richTextSize}
              onChange={applyRichTextFontSize}
              options={RICH_TEXT_FONT_SIZES}
              style={{ width: 76 }}
              buttonStyle={richTextSelectButtonStyle}
            />
          </ToolbarGroup>
          <ToolbarSep />
          <ToolbarGroup>
            <ToolbarBtn label={t("page.doc_editor.b")} title={t("page.doc_editor.bold_ctrl_plus_b")} bold onClick={() => execCmd("bold")} />
            <ToolbarBtn label={t("page.doc_editor.i")} title={t("page.doc_editor.italic_ctrl_plus_i")} italic onClick={() => execCmd("italic")} />
            <ToolbarBtn label={t("page.doc_editor.u")} title={t("page.doc_editor.underline_ctrl_plus_u")} underline onClick={() => execCmd("underline")} />
            <ToolbarBtn label="S" title="Strikethrough" onClick={() => execCmd("strikeThrough")} />
            <ToolbarColor title="Text color" value="#1c1917" onChange={(value) => execCmd("foreColor", value)} icon={<IconText size={14} />} />
            <ToolbarColor title="Highlight" value="#fef08a" onChange={(value) => execCmd("hiliteColor", value)} icon={<IconHighlighter size={14} />} />
          </ToolbarGroup>
          <ToolbarSep />
          <ToolbarGroup>
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <IconList size={15} />
                  Paragraph
                </button>
              )}
              items={[
                { key: "align-left", label: "Align left" },
                { key: "align-center", label: "Align center" },
                { key: "align-right", label: "Align right" },
                { key: "align-justify", label: "Justify" },
                { key: "bullet", label: "Bullet list" },
                { key: "numbered", label: "Numbered list" },
                { key: "indent", label: "Indent" },
                { key: "outdent", label: "Outdent" },
                { key: "line-1", label: "Line spacing 1.0" },
                { key: "line-15", label: "Line spacing 1.5" },
                { key: "line-2", label: "Line spacing 2.0" },
                { key: "space-tight", label: "Tight paragraph spacing" },
                { key: "space-normal", label: "Normal paragraph spacing" },
                { key: "space-loose", label: "Loose paragraph spacing" },
              ]}
              onSelect={handleRichTextLayoutAction}
            />
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <IconLink size={15} />
                  Link
                </button>
              )}
              items={[
                { key: "add", label: "Add link" },
                { key: "remove", label: "Remove link" },
              ]}
              onSelect={(key) => {
                if (key === "add") applyRichTextLink();
                if (key === "remove") execCmd("unlink");
              }}
            />
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <IconPlus size={15} />
                  Insert
                </button>
              )}
              items={[
                { key: "table-2", label: "2 x 2 table" },
                { key: "table-3", label: "3 x 3 table" },
                { key: "table-custom", label: "Custom table" },
                { key: "image", label: "Image" },
                { key: "checklist", label: "Checklist" },
                { key: "callout", label: "Callout" },
                { key: "date", label: "Date" },
                { key: "page-break", label: "Page break" },
                { key: "divider", label: "Horizontal line" },
              ]}
              onSelect={handleRichTextInsert}
            />
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  Table
                </button>
              )}
              items={[
                { key: "row-above", label: "Insert row above" },
                { key: "row-below", label: "Insert row below" },
                { key: "column-left", label: "Insert column left" },
                { key: "column-right", label: "Insert column right" },
                { key: "header-row", label: "Make first row header" },
                { key: "merge-right", label: "Merge with cell right" },
                { key: "split-cell", label: "Split cell" },
                { key: "delete-row", label: "Delete row", danger: true },
                { key: "delete-column", label: "Delete column", danger: true },
                { key: "delete-table", label: "Delete table", danger: true },
              ]}
              onSelect={handleRichTextTableAction}
            />
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <IconSearch size={15} />
                  Tools
                </button>
              )}
              items={[
                { key: "find", label: "Find" },
                { key: "replace", label: "Replace first match" },
                { key: "select-all", label: "Select all" },
                { key: "upper", label: "Uppercase selection" },
                { key: "lower", label: "Lowercase selection" },
                { key: "title", label: "Title case selection" },
                { key: "clear-format", label: "Clear formatting" },
                { key: "clear-document", label: "Clear document", danger: true },
              ]}
              onSelect={handleRichTextToolsAction}
            />
          </ToolbarGroup>
          {isDocx && (
            <>
              <ToolbarSep />
              <span className="richtext-toolbar-note">{t("page.doc_editor.docx_imported_as_html_saves_as_text")}</span>
            </>
          )}
        </div>
      )}

      {mode === "markdown" && (
        <div className="manor-editor-toolbar markdown-editor-toolbar">
          <div className="markdown-toolbar-group">
            <button type="button" className="manor-editor-tool-button manor-editor-icon-button" title="Bold (Cmd/Ctrl+B)" onClick={() => wrapMarkdownSelection("**", "**", "bold text")}>
              <strong>B</strong>
            </button>
            <button type="button" className="manor-editor-tool-button manor-editor-icon-button" title="Italic (Cmd/Ctrl+I)" onClick={() => wrapMarkdownSelection("*", "*", "italic text")}>
              <em>I</em>
            </button>
            <button type="button" className="manor-editor-tool-button manor-editor-icon-button" title="Inline code" onClick={() => wrapMarkdownSelection("`", "`", "code")}>
              <IconCode size={15} />
            </button>
            <button type="button" className="manor-editor-tool-button manor-editor-icon-button" title="Link (Cmd/Ctrl+K)" onClick={insertMarkdownLink}>
              <IconLink size={15} />
            </button>
          </div>
          <div className="manor-editor-toolbar-divider" />
          <div className="markdown-toolbar-group">
            <button type="button" className="manor-editor-tool-button" onClick={() => applyMarkdownHeading(1)}>H1</button>
            <button type="button" className="manor-editor-tool-button" onClick={() => applyMarkdownHeading(2)}>H2</button>
            <button type="button" className="manor-editor-tool-button" onClick={() => applyMarkdownHeading(3)}>H3</button>
          </div>
          <div className="manor-editor-toolbar-divider" />
          <div className="markdown-toolbar-group">
            <button type="button" className="manor-editor-tool-button" title="Bullet list" onClick={() => prefixMarkdownLines("- ")}>
              <IconList size={15} /> Bullet
            </button>
            <button type="button" className="manor-editor-tool-button" title="Numbered list" onClick={() => prefixMarkdownLines("{n}. ")}>
              1. List
            </button>
            <button type="button" className="manor-editor-tool-button" title="Task list" onClick={() => prefixMarkdownLines("- [ ] ")}>
              <IconCheck size={15} /> Task
            </button>
            <button type="button" className="manor-editor-tool-button" title="Quote" onClick={() => prefixMarkdownLines("> ")}>
              Quote
            </button>
          </div>
          <div className="manor-editor-toolbar-divider" />
          <div className="markdown-toolbar-group">
            <button type="button" className="manor-editor-tool-button" title="Code block" onClick={() => insertMarkdownBlock("```ts\n\n```", "```ts\n".length)}>
              <IconCode size={15} /> Block
            </button>
            <button type="button" className="manor-editor-tool-button" title="Table" onClick={() => insertMarkdownBlock("| Name | Value |\n| --- | --- |\n| Item | 100 |")}>
              Table
            </button>
            <button type="button" className="manor-editor-tool-button" title="Image" onClick={insertMarkdownImage}>
              Image
            </button>
            <button type="button" className="manor-editor-tool-button" title="Wiki link" onClick={insertMarkdownWikiLink}>
              [[Wiki]]
            </button>
            <button type="button" className="manor-editor-tool-button" title="Divider" onClick={() => insertMarkdownBlock("---")}>
              HR
            </button>
          </div>
          <div className="markdown-view-switch" aria-label="Markdown view mode">
            {(["source", "split", "preview"] as MarkdownViewMode[]).map((viewMode) => (
              <button
                key={viewMode}
                type="button"
                className={markdownViewMode === viewMode ? "is-active" : ""}
                onClick={() => setMarkdownViewMode(viewMode)}
              >
                {viewMode === "source" ? "Edit" : viewMode === "split" ? "Split" : "Preview"}
              </button>
            ))}
          </div>
        </div>
      )}

      {mode === "text" && (
        <div className="manor-editor-toolbar richtext-editor-toolbar text-editor-toolbar">
          <ToolbarGroup>
            <ToolbarBtn title={t("page.doc_editor.undo_ctrl_plus_z")} onClick={() => runPlainTextNativeCommand("undo")} icon={<IconUndo size={15} />} />
            <ToolbarBtn title={t("page.doc_editor.redo_ctrl_plus_shift_plus_z")} onClick={() => runPlainTextNativeCommand("redo")} icon={<IconRedo size={15} />} />
          </ToolbarGroup>
          <ToolbarSep />
          <ToolbarGroup>
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <IconPlus size={15} />
                  Insert
                </button>
              )}
              items={[
                { key: "date", label: "Date" },
                { key: "time", label: "Date and time" },
                { key: "divider", label: "Divider" },
                { key: "bullet", label: "Bullet lines" },
                { key: "numbered", label: "Numbered lines" },
              ]}
              onSelect={handlePlainTextInsertAction}
            />
            <Dropdown
              align="left"
              trigger={(
                <button
                  type="button"
                  className="manor-editor-tool-button richtext-toolbar-button"
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <IconSearch size={15} />
                  Tools
                </button>
              )}
              items={[
                { key: "find", label: "Find" },
                { key: "replace", label: "Replace first match" },
                { key: "select-all", label: "Select all" },
                { key: "upper", label: "Uppercase selection" },
                { key: "lower", label: "Lowercase selection" },
                { key: "title", label: "Title case selection" },
                { key: "sort", label: "Sort selected lines" },
                { key: "dedupe", label: "Remove duplicate lines" },
                { key: "trim", label: "Trim trailing spaces" },
                { key: "clear-document", label: "Clear document", danger: true },
              ]}
              onSelect={handlePlainTextToolsAction}
            />
          </ToolbarGroup>
          <ToolbarSep />
          <ToolbarGroup>
            <ToolbarBtn title="Find" onClick={findPlainText} icon={<IconSearch size={15} />} />
            <ToolbarBtn title="Select all" onClick={selectAllPlainText} icon={<IconText size={15} />} />
            <ToolbarBtn title="Copy selection" onClick={copyPlainTextSelection} icon={<IconCopy size={15} />} />
          </ToolbarGroup>
        </div>
      )}

      {/* Main editor area */}
      <div className="manor-editor-main">
        {isLoadingContent ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <LoadingSpinner size={28} />
          </div>
        ) : mode === "presentation" ? (
          /* Presentation editor (PPTX) */
          <PresentationEditor key={docId} slides={pptxSlides} serverSlideUrls={pptxServerUrls} onChange={handleSlidesChange} />
        ) : mode === "diagram" && diagramDoc ? (
          /* Editable diagram canvas */
          <DiagramCanvas document={diagramDoc} onChange={handleDiagramChange} />
        ) : mode === "spreadsheet" ? (
          /* Spreadsheet editor */
          <SpreadsheetEditor key={docId} initialData={sheetData} initialCharts={sheetCharts} initialStyles={sheetStyles} persistCharts={isXlsx} onChange={handleSheetChange} />
        ) : mode === "text" ? (
          /* Plain text editor with the same page-centered document surface as Word */
          <div className="manor-editor-workspace richtext-editor-workspace text-editor-workspace">
            {liveDiff ? (
              <EditorLiveInlineDiff
                content={content}
                diff={liveDiff}
                variant="document"
                title={`AI edit diff · ${docName}`}
                showHeader
                onClose={() => setLiveDiff(null)}
              />
            ) : (
              <>
                <textarea
                  ref={textRef}
                  value={content}
                  onChange={(e) => handleContentChange(e.target.value)}
                  onKeyDown={handlePlainTextKeyDown}
                  onSelect={refreshCommentAnchor}
                  onClick={refreshCommentAnchor}
                  onKeyUp={refreshCommentAnchor}
                  rows={Math.max(30, content.split("\n").length + 6)}
                  spellCheck
                  placeholder="Start typing..."
                  className="manor-editor-document-surface text-editor-page"
                />
                {renderCommentAnchorRail(lineCount)}
              </>
            )}
          </div>
        ) : mode === "richtext" ? (
          /* Rich text (contentEditable) — also used for DOCX */
          <div className="manor-editor-workspace richtext-editor-workspace">
            <div
              ref={editorRef}
              contentEditable
              suppressContentEditableWarning
              onInput={handleRichTextInput}
              onKeyDown={handleRichTextKeyDown}
              onMouseUp={() => {
                saveRichSelection();
                refreshCommentAnchor();
              }}
              onKeyUp={() => {
                saveRichSelection();
                refreshCommentAnchor();
              }}
              onFocus={() => {
                saveRichSelection();
                refreshCommentAnchor();
              }}
              onBlur={saveRichSelection}
              spellCheck
              data-placeholder="Start typing..."
              className="docx-preview manor-editor-document-surface richtext-editor-page"
            />
          </div>
        ) : mode === "markdown" ? (
          /* Markdown editor */
          <div className={`markdown-editor-layout markdown-editor-layout--${markdownEditorLayoutMode}`}>
            {markdownEditorLayoutMode !== "preview" && (
              <div className="markdown-source-pane">
                <div className="markdown-pane-header">
                  <span>Markdown</span>
                  <span>{markdownStats.lines} lines</span>
                  {liveDiff && (
                    <button
                      type="button"
                      className="doc-editor-code-diff-close"
                      aria-label="Close inline diff"
                      title="Close inline diff"
                      onClick={() => setLiveDiff(null)}
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true">
                        <path
                          d="M6 6l12 12M18 6L6 18"
                          fill="none"
                          stroke="currentColor"
                          strokeLinecap="round"
                          strokeWidth="2"
                        />
                      </svg>
                    </button>
                  )}
                </div>
                {liveDiff ? (
                  <EditorLiveInlineDiff
                    content={content}
                    diff={liveDiff}
                    variant="markdown"
                    title={`AI edit diff · ${docName}`}
                  />
                ) : (
                  <textarea
                    ref={markdownRef}
                    value={content}
                    onChange={(e) => handleContentChange(e.target.value)}
                    onKeyDown={(e) => handlePlainTextKeyDown(e, { markdown: true })}
                    onSelect={refreshCommentAnchor}
                    onClick={refreshCommentAnchor}
                    onKeyUp={refreshCommentAnchor}
                    className="manor-editor-codearea markdown-codearea"
                    placeholder={t("page.doc_editor.write_your_markdown_here")}
                    spellCheck
                  />
                )}
                {renderCommentAnchorRail(markdownStats.lines)}
              </div>
            )}
            {markdownEditorLayoutMode !== "source" && (
              <div className="markdown-preview-pane">
                <div className="markdown-pane-header">
                  <span>Preview</span>
                  <span>{markdownStats.readingMinutes} min read · {markdownStats.headings} headings</span>
                </div>
                <div className="markdown-preview-body">
                  {markdownHeadings.length > 0 && (
                    <aside className="markdown-outline">
                      <strong>Outline</strong>
                      {markdownHeadings.slice(0, 12).map((heading) => (
                        <button
                          key={`${heading.id}-${heading.line}`}
                          type="button"
                          style={{ paddingLeft: 8 + (heading.level - 1) * 10 }}
                          onClick={() => {
                            const lineStart = content.split("\n").slice(0, heading.line - 1).join("\n").length + (heading.line > 1 ? 1 : 0);
                            markdownRef.current?.focus();
                            if (markdownRef.current) {
                              markdownRef.current.selectionStart = lineStart;
                              markdownRef.current.selectionEnd = lineStart;
                            }
                            setMarkdownViewMode((current) => current === "preview" ? "split" : current);
                          }}
                        >
                          {heading.text}
                        </button>
                      ))}
                    </aside>
                  )}
                  <div className="md-preview prose prose-slate prose-sm">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, remarkBreaks]}
                      components={{
                        a({ href, children, ...props }: any) {
                          const hrefString = String(href || "");
                          if (hrefString.startsWith("#wiki:")) {
                            const target = decodeURIComponent(hrefString.slice("#wiki:".length));
                            const link = markdownWikiLinksByTarget.get(wikiLinkKey(target));
                            const exists = Boolean(link?.exists && link.document_id);
                            return (
                              <a
                                href="#"
                                className={`md-wiki-link${exists ? "" : " md-wiki-link-missing"}`}
                                title={exists ? (link?.document_name || target) : `Missing wiki page: ${target}`}
                                onClick={(event) => {
                                  event.preventDefault();
                                  void openMarkdownWikiTarget(target);
                                }}
                              >
                                {children}
                              </a>
                            );
                          }
                          return <a {...props} href={href} target="_blank" rel="noopener noreferrer" className="md-link">{children}</a>;
                        },
                        pre({ children }: any) {
                          return <pre className="md-code-block">{children}</pre>;
                        },
                        code({ className, children, ...props }: any) {
                          return <code {...props} className={className || "md-inline-code"}>{children}</code>;
                        },
                        table({ children }: any) {
                          return <table className="md-table">{children}</table>;
                        },
                        input({ ...props }: any) {
                          return <input {...props} disabled className="md-task-checkbox" />;
                        },
                      }}
                    >
                      {markdownPreviewSource}
                    </ReactMarkdown>
                  </div>
                </div>
              </div>
            )}
          </div>
        ) : (
          /* Code editor */
          <div className={`doc-editor-code-layout${showPreview && isRenderableCodeFile(docName) ? " doc-editor-code-layout--split" : ""}`}>
            <section className="doc-editor-code-source" aria-label="Code editor">
              <div className="doc-editor-code-titlebar">
                <span className="doc-editor-code-filename" title={docName}>{docName}</span>
                <div className="doc-editor-code-title-actions">
                  <span className="doc-editor-code-language">{codeLanguageName}</span>
                  {liveDiff && (
                    <button
                      type="button"
                      className="doc-editor-code-diff-close"
                      aria-label="Close inline diff"
                      title="Close inline diff"
                      onClick={() => setLiveDiff(null)}
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true">
                        <path
                          d="M6 6l12 12M18 6L6 18"
                          fill="none"
                          stroke="currentColor"
                          strokeLinecap="round"
                          strokeWidth="2"
                        />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
              <div className={`doc-editor-code-body${liveDiff ? " doc-editor-code-body--diff" : ""}`}>
                {liveDiff ? (
                  <EditorLiveInlineDiff content={content} diff={liveDiff} variant="code" />
                ) : (
                  <>
                    <div className="manor-editor-line-gutter doc-editor-code-gutter">
                      <div ref={codeGutterRef} className="doc-editor-code-gutter-lines">
                        {Array.from({ length: lineCount }, (_, i) => {
                          const line = i + 1;
                          const count = commentLineCounts.get(line) || 0;
                          const firstComment = count ? firstCommentForLine(line) : undefined;
                          return (
                            <div key={i} className="doc-editor-code-gutter-line">
                              <span>{line}</span>
                              {firstComment && (
                                <button
                                  type="button"
                                  className={activeCommentId === firstComment.id ? "doc-editor-code-comment-marker is-active" : "doc-editor-code-comment-marker"}
                                  title={`${count} ${t("page.tasks.comments")}`}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    handleSelectDocumentComment(firstComment);
                                  }}
                                >
                                  <IconComment size={10} />
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                    <div className="doc-editor-code-stack">
                      <div ref={codeHighlightRef} className="doc-editor-code-highlight" aria-hidden="true">
                        <CodeSyntaxHighlighter
                          language={codeLanguage}
                          style={ideEditorTheme}
                          wrapLongLines={false}
                          customStyle={{
                            minHeight: "100%",
                            overflow: "visible",
                          }}
                          codeTagProps={{
                            style: {
                              fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                              tabSize: 2,
                            },
                          }}
                        >
                          {content || " "}
                        </CodeSyntaxHighlighter>
                      </div>
                      <textarea
                        ref={codeRef}
                        value={content}
                        onChange={(e) => handleContentChange(e.target.value)}
                        onScroll={(event) => {
                          const { scrollTop, scrollLeft } = event.currentTarget;
                          syncCodeScrollLayers(scrollTop, scrollLeft);
                        }}
                        onKeyDown={handlePlainTextKeyDown}
                        onSelect={refreshCommentAnchor}
                        onClick={refreshCommentAnchor}
                        onKeyUp={refreshCommentAnchor}
                        className="manor-editor-codearea doc-editor-ide-textarea"
                        placeholder={t("page.doc_editor.start_coding")}
                        spellCheck={false}
                        wrap="off"
                      />
                    </div>
                  </>
                )}
              </div>
            </section>
            {showPreview && isRenderableCodeFile(docName) && (
              <section className="doc-editor-code-preview" aria-label={t("page.doc_editor.preview")}>
                <div className="doc-editor-code-preview-bar">
                  <span>{t("page.doc_editor.preview")}</span>
                  <span>{renderableCodePreviewLabel(docName)}</span>
                </div>
                <iframe
                  srcDoc={codePreviewContent}
                  sandbox="allow-scripts"
                  className="doc-editor-code-preview-frame"
                  title={t("page.doc_editor.preview")}
                />
              </section>
            )}
          </div>
        )}

        {/* Comments */}
        {showComments && doc && (
          <div className="manor-editor-sidebar manor-editor-comments-panel" style={{ width: 320, overflowY: "auto" }}>
            <div className="manor-editor-sidebar-header" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.tasks.comments")}</h3>
              <button
                type="button"
                onClick={() => setShowComments(false)}
                aria-label={t("page.file_viewer.details_panel.close")}
                className="btn-manor-ghost"
                style={{ width: 28, height: 28, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
              >
                <IconClose size={14} />
              </button>
            </div>
            <div className="manor-editor-sidebar-section">
              <CommentThread
                resourceType="document"
                resourceId={doc.id}
                canComment={canCommentCurrentDoc}
                anchor={commentAnchor}
                activeCommentId={activeCommentId}
                onCommentsLoaded={handleCommentsLoaded}
                onSelectComment={handleSelectDocumentComment}
              />
            </div>
          </div>
        )}

        {/* Version history */}
        {showVersions && (
          <div className="manor-editor-sidebar" style={{ width: 280, overflowY: "auto" }}>
            <div className="manor-editor-sidebar-header">
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.doc_editor.version_history_2")}</h3>
            </div>
            <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 6 }}>
              {!versions || versions.length === 0 ? (
                <p style={{ fontSize: 12, color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>{t("page.doc_editor.no_versions_yet")}</p>
              ) : (
                (versions as any[]).map((v: any) => (
                  <div key={v.id} className="glass-card-sm" style={{ padding: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <span style={{ fontSize: 12, fontWeight: 700, color: "#44403c" }}>{t("page.doc_editor.v")}{v.version_number}</span>
                      <span style={{ fontSize: 10, color: "#a8a29e" }}>
                        {v.created_at ? new Date(v.created_at).toLocaleString() : ""}
                      </span>
                    </div>
                    {v.change_summary && <p style={{ fontSize: 11, color: "#78716c", marginTop: 4, marginBottom: 0 }}>{v.change_summary}</p>}
                    {v.created_by && <p style={{ fontSize: 10, color: "#a8a29e", marginTop: 2, marginBottom: 0 }}>{t("page.skills.by")} {v.created_by}</p>}
                    {v.file_size != null && <p style={{ fontSize: 10, color: "#a8a29e", marginTop: 2, marginBottom: 0 }}>{formatBytes(v.file_size)}</p>}
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="manor-editor-statusbar">
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span>{mode === "presentation" ? formatBytes(doc?.file_size || 0) : formatBytes(new Blob([content]).size)}</span>
          {mode !== "spreadsheet" && mode !== "presentation" && (
            <>
              <span>{wordCount(mode === "richtext" ? content.replace(/<[^>]+>/g, " ") : content)} {t("page.doc_editor.words")}</span>
              <span>{(mode === "richtext" ? content.replace(/<[^>]+>/g, "") : content).length} {t("page.doc_editor.chars")}</span>
            </>
          )}
          {(mode === "code" || mode === "text") && <span>{lineCount} {t("page.doc_editor.lines")}</span>}
          {mode === "markdown" && (
            <>
              <span>{markdownStats.lines} lines</span>
              <span>{markdownStats.readingMinutes} min read</span>
            </>
          )}
          {mode === "spreadsheet" && sheetData && <span>{sheetData.length} {t("page.doc_editor.rows_2")}</span>}
          {mode === "presentation" && <span>{pptxSlides.length} {t("page.doc_editor.slides")}</span>}
          {mode === "diagram" && diagramDoc && <span>{diagramDoc.elements.length} objects</span>}
        </div>
        <div className="doc-editor-footer-status" style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {saveStatus === "saved" && (
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#5f928a", display: "inline-block" }} />
              {t("page.blueprint_detail.saved")}
            </span>
          )}
          {doc?.created_at && <span>{t("page.dashboard.created")} {new Date(doc.created_at).toLocaleDateString()}</span>}
        </div>
      </div>

      {/* Styles */}
      <style>{`
        .doc-comment-anchor-rail {
          position: absolute;
          top: 0;
          right: 10px;
          bottom: 0;
          width: 22px;
          pointer-events: none;
          z-index: 12;
        }
        .text-editor-workspace,
        .markdown-source-pane {
          position: relative;
        }
        .doc-comment-anchor-dot {
          position: absolute;
          right: 0;
          width: 20px;
          height: 20px;
          border-radius: 999px;
          border: 1px solid rgba(120, 113, 108, 0.55);
          background: #fafaf9;
          color: #57534e;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          box-shadow: 0 4px 12px rgba(28, 25, 23, 0.12);
          pointer-events: auto;
          cursor: pointer;
        }
        .doc-comment-anchor-dot.is-active,
        .doc-comment-anchor-dot:hover {
          background: #44403c;
          color: #fff;
          border-color: #44403c;
        }
        .doc-editor-code-gutter-line {
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 4px;
          min-height: 1.65rem;
        }
        .doc-editor-code-comment-marker {
          width: 17px;
          height: 17px;
          border-radius: 999px;
          border: 1px solid rgba(120, 113, 108, 0.55);
          background: rgba(250, 250, 249, 0.96);
          color: #57534e;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          padding: 0;
        }
        .doc-editor-code-comment-marker.is-active,
        .doc-editor-code-comment-marker:hover {
          background: #44403c;
          color: white;
          border-color: #44403c;
        }
        .btn-manor-neutral-light {
          background: #f5f5f4;
          border: 1px solid #d6d3d1;
          color: #44403c;
        }
        .btn-manor-neutral-light:hover {
          background: #e7e5e4;
          border-color: #a8a29e;
        }
        .md-preview h1 { font-size: 1.75em; font-weight: 700; margin: 0.8em 0 0.4em; }
        .md-preview h2 { font-size: 1.4em; font-weight: 700; margin: 0.7em 0 0.35em; }
        .md-preview h3 { font-size: 1.15em; font-weight: 600; margin: 0.6em 0 0.3em; }
        .md-preview p { margin: 0.5em 0; }
        .md-preview ul { list-style: disc; padding-left: 1.5em; margin: 0.5em 0; }
        .md-preview ol { list-style: decimal; padding-left: 1.5em; margin: 0.5em 0; }
        .md-preview blockquote { border-left: 3px solid #d6d3d1; padding-left: 1em; color: #78716c; margin: 0.5em 0; }
        .md-preview hr { border: none; border-top: 1px solid #e7e5e4; margin: 1em 0; }
        .md-code-block { background: #f5f5f4; border-radius: 10px; padding: 1em; overflow-x: auto; font-size: 0.85em; margin: 0.5em 0; }
        .md-inline-code { background: #f5f5f4; padding: 0.15em 0.4em; border-radius: 4px; font-size: 0.9em; }
        .md-link { color: #436b65; text-decoration: underline; }
        .md-wiki-link {
          display: inline-flex;
          align-items: center;
          border: 1px solid rgba(79, 125, 117, 0.24);
          border-radius: 999px;
          background: rgba(242, 246, 245, 0.74);
          color: #436b65;
          padding: 0 0.45em;
          margin: 0 0.05em;
          font-weight: 700;
          text-decoration: none;
          cursor: pointer;
          transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
        }
        .md-wiki-link:hover {
          background: rgba(229, 238, 235, 0.72);
          border-color: rgba(79, 125, 117, 0.38);
          color: #395a54;
        }
        .md-wiki-link-missing {
          border-color: rgba(168, 162, 158, 0.34);
          background: rgba(250, 250, 249, 0.82);
          color: #78716c;
          border-style: dashed;
        }
        .md-img { max-width: 100%; border-radius: 10px; margin: 0.5em 0; }
        .docx-preview { overflow-wrap: anywhere; }
        .richtext-editor-page {
          box-sizing: border-box;
          display: block;
          flex: 0 0 auto;
          height: auto !important;
          min-width: 0;
          overflow: visible;
          white-space: normal;
          word-break: normal;
        }
        .richtext-editor-page > * {
          max-width: 100%;
        }
        .richtext-editor-page table {
          max-width: 100%;
        }
        .docx-preview table { border-collapse: collapse; width: 100%; margin: 12px 0; }
        .docx-preview td, .docx-preview th { border: 1px solid #e7e5e4; padding: 8px 12px; font-size: 14px; }
        .docx-preview th { background: #fafaf9; font-weight: 600; }
        .docx-preview img { max-width: 100%; border-radius: 8px; margin: 8px 0; }
        .docx-preview p { margin: 0 0 12px; }
        .docx-preview h1, .docx-preview h2, .docx-preview h3 { margin: 20px 0 10px; color: #1c1917; line-height: 1.25; }
        .docx-preview h1 { font-size: 28px; }
        .docx-preview h2 { font-size: 22px; }
        .docx-preview h3 { font-size: 18px; }
        .docx-preview ul, .docx-preview ol { padding-left: 1.5em; margin: 8px 0 14px; }
        .richtext-editor-page:empty::before {
          content: attr(data-placeholder);
          color: #a8a29e;
          pointer-events: none;
        }
        .docx-preview blockquote {
          margin: 14px 0;
          padding: 10px 14px;
          border-left: 3px solid #ccded9;
          color: #57534e;
          background: #fafaf9;
        }
        .docx-preview .doc-editor-callout {
          border-color: #4f7d75;
          background: #f2f6f5;
        }
        .docx-preview .doc-editor-checklist {
          list-style: none;
          padding-left: 0;
        }
        .docx-preview .doc-editor-checkbox {
          margin-right: 8px;
          color: #436b65;
          font-weight: 800;
        }
        .docx-preview .doc-editor-page-break {
          position: relative;
          height: 28px;
          margin: 28px 0;
          border-top: 1px dashed #d6d3d1;
          color: #a8a29e;
          font-size: 11px;
          font-weight: 800;
          letter-spacing: 0.08em;
          text-align: center;
          text-transform: uppercase;
          user-select: none;
        }
        .docx-preview .doc-editor-page-break span {
          position: relative;
          top: -9px;
          display: inline-flex;
          padding: 0 10px;
          background: #ffffff;
        }
        .docx-preview hr {
          border: 0;
          border-top: 1px solid #e7e5e4;
          margin: 22px 0;
        }
        @media (max-width: 640px) {
          .doc-editor-shell {
            margin: -16px !important;
            border-radius: 28px !important;
          }
          .doc-editor-header {
            gap: 8px !important;
            padding: 10px 12px !important;
            flex-wrap: wrap !important;
            align-items: center !important;
            overflow: visible !important;
          }
          .doc-editor-header > .btn-manor-ghost:first-child {
            width: 34px !important;
            height: 34px !important;
            flex: 0 0 auto !important;
          }
          .doc-editor-title-row {
            flex: 1 1 calc(100% - 48px) !important;
            min-width: 0 !important;
          }
          .doc-editor-title-row h1 {
            font-size: 14px !important;
          }
          .doc-editor-header .btn-manor,
          .doc-editor-header .btn-manor-ghost,
          .doc-editor-header .btn-manor-neutral-light,
          .doc-editor-header .btn-manor-teal-light {
            flex: 0 0 auto;
          }
          .doc-editor-main {
            overflow: auto !important;
          }
          .doc-editor-richtext-wrap {
            padding: 14px !important;
          }
          .doc-editor-richtext-wrap .docx-preview {
            padding: 18px !important;
            border-radius: 18px !important;
          }
          .doc-editor-code-layout {
            grid-template-columns: minmax(0, 1fr) !important;
            overflow: auto !important;
          }
          .doc-editor-code-source {
            min-height: 44vh !important;
            flex: 0 0 auto !important;
            border-right: 0 !important;
            border-bottom: 1px solid rgba(231,229,228,0.6) !important;
          }
          .doc-editor-code-source textarea {
            min-height: 44vh !important;
            padding: 18px !important;
          }
          .doc-editor-code-preview {
            min-height: 42vh !important;
            flex: 0 0 auto !important;
          }
          .doc-editor-code-preview-frame {
            min-height: 42vh !important;
          }
          .doc-editor-footer {
            align-items: flex-start !important;
            gap: 4px 12px !important;
            justify-content: flex-start !important;
            padding: 8px 12px !important;
            flex-wrap: wrap !important;
          }
          .doc-editor-footer-stats,
          .doc-editor-footer-status {
            gap: 8px 12px !important;
            flex-wrap: wrap !important;
          }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toolbar sub-components
// ---------------------------------------------------------------------------

function ToolbarBtn({
  label, title, onClick, bold, italic, underline, icon,
}: {
  label?: string; title: string; onClick: () => void;
  bold?: boolean; italic?: boolean; underline?: boolean; icon?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseDown={(event) => event.preventDefault()}
      title={title}
      className="manor-editor-tool-button richtext-toolbar-button"
      style={{ fontWeight: bold ? 800 : undefined, fontStyle: italic ? "italic" : undefined, textDecoration: underline ? "underline" : undefined }}
    >
      {icon || label}
    </button>
  );
}

function ToolbarGroup({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return <div className="richtext-toolbar-group" style={style}>{children}</div>;
}

function ToolbarColor({
  title,
  value,
  icon,
  onChange,
}: {
  title: string;
  value: string;
  icon: React.ReactNode;
  onChange: (value: string) => void;
}) {
  return (
    <label className="manor-editor-tool-button richtext-toolbar-button richtext-toolbar-color" title={title}>
      {icon}
      <span className="richtext-toolbar-color-swatch" style={{ background: value }} />
      <input type="color" defaultValue={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function ToolbarSep() {
  return <div className="manor-editor-toolbar-divider" />;
}
