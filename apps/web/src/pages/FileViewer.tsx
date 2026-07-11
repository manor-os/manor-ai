import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from "react";
import { useLocation, useParams, useNavigate, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { api } from "../lib/api";
import type { Comment, CommentAnchor, Document, DocumentGrant, DocumentShare, UserSummary } from "../lib/types";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import StatusBadge from "../components/ui/StatusBadge";
import Button from "../components/ui/Button";
import AiEditButton from "../components/ui/AiEditButton";
import { IconArrowLeft, IconEdit, IconDownload, IconClose, IconDocument, IconPlus, IconShare, IconInfo, IconText, IconSignature, IconTrash, IconCheck, IconUndo, IconRedo, IconHighlighter, IconPenLine, IconEraser, IconCopy, IconRefresh, IconComment } from "../components/icons";
import CommentThread from "../components/CommentThread";
import {
  ClassificationBadge,
  VisibilityIcon,
  WatermarkLayer,
  PermissionBanner,
  ShareDialog,
} from "../components/permissions";
import type { NewExternalShareConfig } from "../components/permissions";
import { useAuthStore } from "../stores/auth";
import { canCommentDocument, canEditDocument, canShareDocument } from "../lib/permissions";
import {
  openEditorLiveChat,
  type EditorLiveApplyMeta,
  type EditorLiveChatDetail,
} from "../lib/editorLiveChat";
import { getAuthToken } from "../lib/authToken";
import { isCodeLikeFile } from "../lib/codeFiles";

import { t } from "../lib/i18n";

type FileCategory = "text" | "markdown" | "code" | "html" | "image" | "video" | "audio" | "pdf" | "csv" | "json" | "docx" | "xlsx" | "pptx" | "unsupported";
type ImageStroke = { id: string; color: string; size: number; points: Array<{ x: number; y: number }> };
type CommentTextRange = { id: string; start: number; end: number; quote?: string };
type TaskOutputPreviewState = {
  id?: string;
  name?: string;
  fs_path?: string;
  file_type?: string;
  mime_type?: string;
  content: string;
};

function getKnowledgeReturnTo(state: unknown): string | null {
  if (!state || typeof state !== "object") return null;
  const value = (state as { knowledgeReturnTo?: unknown; returnTo?: unknown }).knowledgeReturnTo
    ?? (state as { returnTo?: unknown }).returnTo;
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") ? value : null;
}

function getTaskOutputPreview(state: unknown): TaskOutputPreviewState | null {
  if (!state || typeof state !== "object") return null;
  const preview = (state as { taskOutputPreview?: unknown }).taskOutputPreview;
  if (!preview || typeof preview !== "object") return null;
  const content = (preview as { content?: unknown }).content;
  if (typeof content !== "string" || !content.trim()) return null;
  return {
    id: typeof (preview as { id?: unknown }).id === "string" ? (preview as { id: string }).id : undefined,
    name: typeof (preview as { name?: unknown }).name === "string" ? (preview as { name: string }).name : undefined,
    fs_path: typeof (preview as { fs_path?: unknown }).fs_path === "string" ? (preview as { fs_path: string }).fs_path : undefined,
    file_type: typeof (preview as { file_type?: unknown }).file_type === "string" ? (preview as { file_type: string }).file_type : undefined,
    mime_type: typeof (preview as { mime_type?: unknown }).mime_type === "string" ? (preview as { mime_type: string }).mime_type : undefined,
    content,
  };
}

function inferTaskOutputPreviewMetadata(preview: TaskOutputPreviewState): { file_type: string; mime_type: string } {
  const name = preview.name || preview.fs_path || "";
  const ext = name.split(".").pop()?.toLowerCase() || "";
  if (preview.file_type && preview.mime_type) return { file_type: preview.file_type, mime_type: preview.mime_type };
  if (ext === "md" || ext === "markdown" || preview.mime_type === "text/markdown") return { file_type: "markdown", mime_type: "text/markdown" };
  if (ext === "html" || ext === "htm" || preview.mime_type === "text/html") return { file_type: "html", mime_type: "text/html" };
  if (ext === "json" || preview.mime_type === "application/json") return { file_type: "json", mime_type: "application/json" };
  if (ext === "csv" || preview.mime_type === "text/csv") return { file_type: "csv", mime_type: "text/csv" };
  return { file_type: preview.file_type || ext || "text", mime_type: preview.mime_type || "text/plain" };
}

function taskOutputPreviewDocument(docId: string | undefined, preview: TaskOutputPreviewState): Document {
  const name = preview.name || preview.fs_path?.split(/[\\/]/).filter(Boolean).pop() || docId || "Generated output";
  const metadata = inferTaskOutputPreviewMetadata(preview);
  return {
    id: docId || preview.id || "task-output-preview",
    entity_id: "",
    name,
    fs_path: preview.fs_path,
    file_size: new Blob([preview.content]).size,
    file_type: metadata.file_type,
    mime_type: metadata.mime_type,
    source: "task_output_preview",
    vector_status: "not_indexed",
  };
}

function detectCategory(doc: Document): FileCategory {
  const ext = (doc.name || "").split(".").pop()?.toLowerCase() || "";
  const mime = (doc.mime_type || "").split(";")[0].trim().toLowerCase();
  const fileType = (doc.file_type || "").toLowerCase();

  if (["docx", "doc", "wps"].includes(ext) || ["docx", "doc"].includes(fileType) || mime === "application/vnd.openxmlformats-officedocument.wordprocessingml.document") return "docx";
  if (["xlsx", "xls", "et"].includes(ext) || ["xlsx", "xls"].includes(fileType) || mime === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") return "xlsx";
  if (["pptx", "ppt", "dps"].includes(ext) || ["pptx", "ppt"].includes(fileType) || mime === "application/vnd.openxmlformats-officedocument.presentationml.presentation") return "pptx";
  if (["pdf"].includes(ext) || fileType === "pdf" || mime === "application/pdf") return "pdf";

  if (["md", "markdown"].includes(ext)) return "markdown";
  if (["html", "htm"].includes(ext) || mime === "text/html") return "html";
  if (["json"].includes(ext) || mime === "application/json") return "json";
  if (["csv"].includes(ext) || mime === "text/csv") return "csv";
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"].includes(ext) || fileType === "image" || mime.startsWith("image/")) return "image";
  if (["mp4", "webm", "mov", "avi", "mkv"].includes(ext) || fileType === "video" || mime.startsWith("video/")) return "video";
  if (["mp3", "wav", "ogg", "aac", "flac", "m4a"].includes(ext) || fileType === "audio" || mime.startsWith("audio/")) return "audio";
  if (isCodeLikeFile(doc)) return "code";
  if (["txt", "log", "env", "gitignore", "dockerignore", "editorconfig"].includes(ext) || mime.startsWith("text/")) return "text";

  return "unsupported";
}

function categoryLabel(cat: FileCategory): string {
  const labels: Record<FileCategory, string> = {
    text: "Text", markdown: "Markdown", code: "Code", html: "HTML", image: "Image",
    video: "Video", audio: "Audio",
    pdf: "PDF", csv: "CSV", json: "JSON", docx: "Word", xlsx: "Spreadsheet", pptx: "Presentation", unsupported: "File",
  };
  return labels[cat];
}

function categoryBadgeType(cat: FileCategory): string {
  const types: Record<FileCategory, string> = {
    text: "gray", markdown: "purple", code: "orange", html: "blue", image: "red",
    video: "pink", audio: "cyan",
    pdf: "red", csv: "green", json: "blue", docx: "blue", xlsx: "green", pptx: "orange", unsupported: "gray",
  };
  return types[cat];
}

function isEditable(cat: FileCategory): boolean {
  return ["text", "markdown", "code", "html", "json", "csv", "docx", "xlsx", "pptx"].includes(cat);
}

const COMMENT_QUOTE_LIMIT = 180;

function trimCommentQuote(value: string): string {
  const clean = value.replace(/\s+/g, " ").trim();
  return clean.length > COMMENT_QUOTE_LIMIT
    ? `${clean.slice(0, COMMENT_QUOTE_LIMIT - 1)}...`
    : clean;
}

function documentCommentAnchor(doc: Document | null): CommentAnchor {
  return { type: "document", label: doc?.name || t("component.comment_thread.document") };
}

function contentTextAnchor(content: string, selectedText: string, mode: string): CommentAnchor | null {
  if (!content || !selectedText) return null;
  const start = content.indexOf(selectedText);
  if (start < 0) return null;
  const end = start + selectedText.length;
  return {
    type: "text_range",
    mode,
    line: content.slice(0, start).split("\n").length,
    line_end: content.slice(0, end).split("\n").length,
    start,
    end,
    quote: trimCommentQuote(selectedText),
  };
}

function viewerSelectionAnchor(
  surface: HTMLElement | null,
  doc: Document | null,
  category: FileCategory,
  content: string,
): CommentAnchor | null {
  const selection = window.getSelection();
  if (!surface || !selection || selection.rangeCount === 0 || selection.isCollapsed) return null;

  const range = selection.getRangeAt(0);
  if (!surface.contains(range.commonAncestorContainer)) return null;

  const rawText = range.toString();
  const quote = trimCommentQuote(rawText);
  if (!quote) return null;

  if (["text", "code", "json", "csv"].includes(category)) {
    return contentTextAnchor(content, rawText, category) || {
      type: "text_selection",
      mode: category,
      label: t("component.comment_thread.selected_text"),
      quote,
    };
  }

  return {
    type: "viewer_selection",
    mode: category,
    source: doc?.name,
    label: t("component.comment_thread.selected_text"),
    quote,
  };
}

function flattenCommentTree(comments: Comment[]): Comment[] {
  const flattened: Comment[] = [];
  const visit = (comment: Comment) => {
    flattened.push(comment);
    comment.replies?.forEach(visit);
  };
  comments.forEach(visit);
  return flattened;
}

function rootAnchoredComments(comments: Comment[]): Comment[] {
  return flattenCommentTree(comments).filter((comment) => (
    !comment.parent_id
    && comment.anchor
    && Object.keys(comment.anchor).length > 0
    && comment.anchor.type !== "document"
  ));
}

function anchorBelongsToTextCategory(anchor: CommentAnchor | null | undefined, category: FileCategory) {
  if (!anchor) return false;
  const mode = String(anchor.mode || "").toLowerCase();
  if (mode) return mode === category;
  return category === "text" && (anchor.type === "text_range" || anchor.type === "text_selection");
}

function commentRangesForContent(comments: Comment[], category: FileCategory, content: string): CommentTextRange[] {
  if (!content) return [];
  const ranges: CommentTextRange[] = [];
  for (const comment of comments) {
    const anchor = comment.anchor;
    if (!anchorBelongsToTextCategory(anchor, category)) continue;

    const rawStart = Number(anchor?.start);
    const rawEnd = Number(anchor?.end);
    if (Number.isFinite(rawStart) && Number.isFinite(rawEnd) && rawEnd > rawStart) {
      const start = Math.max(0, Math.min(content.length, rawStart));
      const end = Math.max(start, Math.min(content.length, rawEnd));
      if (end > start) {
        ranges.push({ id: comment.id, start, end, quote: anchor?.quote });
        continue;
      }
    }

    const quote = trimCommentQuote(anchor?.quote || "");
    if (quote) {
      const start = content.indexOf(quote);
      if (start >= 0) ranges.push({ id: comment.id, start, end: start + quote.length, quote });
    }
  }

  const sorted = ranges
    .filter((range) => range.end > range.start)
    .sort((a, b) => a.start - b.start || b.end - a.end);

  const nonOverlapping: CommentTextRange[] = [];
  let cursor = -1;
  for (const range of sorted) {
    if (range.start < cursor) continue;
    nonOverlapping.push(range);
    cursor = range.end;
  }
  return nonOverlapping;
}

function renderCommentMarkedText(
  content: string,
  ranges: CommentTextRange[],
  activeCommentId: string | null,
  onSelectCommentId: (commentId: string) => void,
): ReactNode {
  if (!ranges.length) return content || "\u00a0";
  const nodes: ReactNode[] = [];
  let cursor = 0;
  ranges.forEach((range) => {
    if (range.start > cursor) nodes.push(content.slice(cursor, range.start));
    const text = content.slice(range.start, range.end);
    nodes.push(
      <span
        key={`${range.id}-${range.start}-${range.end}`}
        className={`document-comment-mark${activeCommentId === range.id ? " is-active" : ""}`}
        data-comment-id={range.id}
        title={range.quote || t("page.tasks.comments")}
        onClick={(event) => {
          event.stopPropagation();
          onSelectCommentId(range.id);
        }}
      >
        {text}
      </span>,
    );
    cursor = range.end;
  });
  if (cursor < content.length) nodes.push(content.slice(cursor));
  return nodes.length ? nodes : "\u00a0";
}

function quoteAnchoredComments(comments: Comment[]): Comment[] {
  return comments.filter((comment) => {
    const anchor = comment.anchor;
    const mode = String(anchor?.mode || "").toLowerCase();
    return Boolean(anchor?.quote && (
      !mode
      || mode === "docx"
      || mode === "markdown"
      || mode === "richtext"
      || anchor?.type === "viewer_selection"
      || anchor?.type === "richtext_selection"
    ));
  });
}

function unwrapDocumentCommentMarks(root: HTMLElement) {
  root.querySelectorAll(".document-comment-mark").forEach((mark) => {
    mark.replaceWith(document.createTextNode(mark.textContent || ""));
  });
  root.normalize();
}

function commentSearchParts(value: string): string[] {
  const parts: string[] = [];
  for (let offset = 0; offset < value.length;) {
    const codePoint = value.codePointAt(offset);
    if (codePoint === undefined) break;
    const rawChar = String.fromCodePoint(codePoint);
    const endOffset = offset + rawChar.length;
    const normalized = rawChar.normalize("NFKC").toLocaleLowerCase();
    if (normalized.trim()) {
      for (const char of normalized) {
        if (char.trim()) parts.push(char);
      }
    }
    offset = endOffset;
  }
  return parts;
}

function commentSearchKey(value: string): string {
  return commentSearchParts(value).join("");
}

function searchableCommentQuote(value: string): string {
  return trimCommentQuote(value).replace(/\.{3}$/, "");
}

function collectCommentTextNodes(root: HTMLElement): Text[] {
  const nodes: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || parent.closest(".document-comment-mark, script, style")) return NodeFilter.FILTER_REJECT;
      return node.textContent?.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    },
  });
  let node = walker.nextNode();
  while (node) {
    if (node instanceof Text) nodes.push(node);
    node = walker.nextNode();
  }
  return nodes;
}

function markQuoteCommentsInElement(root: HTMLElement, comments: Comment[], activeCommentId?: string | null) {
  type IndexedPosition = { node: Text; offset: number; endOffset: number };
  type MarkSegment = { node: Text; start: number; end: number; commentId: string; quote: string; active: boolean };

  const textNodes = collectCommentTextNodes(root);
  const positions: IndexedPosition[] = [];
  const searchParts: string[] = [];

  textNodes.forEach((textNode) => {
    const text = textNode.textContent || "";
    for (let offset = 0; offset < text.length;) {
      const codePoint = text.codePointAt(offset);
      if (codePoint === undefined) break;
      const rawChar = String.fromCodePoint(codePoint);
      const endOffset = offset + rawChar.length;
      const normalized = rawChar.normalize("NFKC").toLocaleLowerCase();
      if (normalized.trim()) {
        for (const char of normalized) {
          if (!char.trim()) continue;
          searchParts.push(char);
          positions.push({ node: textNode, offset, endOffset });
        }
      }
      offset = endOffset;
    }
  });

  const searchableText = searchParts.join("");
  const nodeOrder = new Map<Text, number>();
  textNodes.forEach((node, index) => nodeOrder.set(node, index));
  const occupied = new WeakMap<Text, Array<{ start: number; end: number }>>();
  const segments: MarkSegment[] = [];

  for (const comment of comments) {
    const quote = searchableCommentQuote(comment.anchor?.quote || "");
    if (!quote) continue;
    const key = commentSearchKey(quote);
    if (!key) continue;

    const searchIndex = searchableText.indexOf(key);
    if (searchIndex < 0) continue;
    const startPosition = positions[searchIndex];
    const endPosition = positions[searchIndex + key.length - 1];
    if (!startPosition || !endPosition) continue;

    const startNodeIndex = nodeOrder.get(startPosition.node);
    const endNodeIndex = nodeOrder.get(endPosition.node);
    if (startNodeIndex === undefined || endNodeIndex === undefined) continue;

    const candidateSegments: MarkSegment[] = [];
    for (let index = startNodeIndex; index <= endNodeIndex; index += 1) {
      const node = textNodes[index];
      const text = node.textContent || "";
      const start = node === startPosition.node ? startPosition.offset : 0;
      const end = node === endPosition.node ? endPosition.endOffset : text.length;
      if (end <= start) continue;
      const ranges = occupied.get(node) || [];
      if (ranges.some((range) => start < range.end && end > range.start)) {
        candidateSegments.length = 0;
        break;
      }
      candidateSegments.push({
        node,
        start,
        end,
        commentId: comment.id,
        quote,
        active: activeCommentId === comment.id,
      });
    }

    candidateSegments.forEach((segment) => {
      const ranges = occupied.get(segment.node) || [];
      ranges.push({ start: segment.start, end: segment.end });
      occupied.set(segment.node, ranges);
      segments.push(segment);
    });
  }

  const segmentsByNode = new Map<Text, MarkSegment[]>();
  segments.forEach((segment) => {
    const nodeSegments = segmentsByNode.get(segment.node) || [];
    nodeSegments.push(segment);
    segmentsByNode.set(segment.node, nodeSegments);
  });

  segmentsByNode.forEach((nodeSegments, node) => {
    const text = node.textContent || "";
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    nodeSegments
      .sort((a, b) => a.start - b.start)
      .forEach((segment) => {
        if (segment.start > cursor) fragment.append(document.createTextNode(text.slice(cursor, segment.start)));
        const mark = document.createElement("span");
        mark.className = `document-comment-mark${segment.active ? " is-active" : ""}`;
        mark.dataset.commentId = segment.commentId;
        mark.title = segment.quote;
        mark.textContent = text.slice(segment.start, segment.end);
        fragment.append(mark);
        cursor = segment.end;
      });
    if (cursor < text.length) fragment.append(document.createTextNode(text.slice(cursor)));
    node.replaceWith(fragment);
  });
}

function encodeFsPath(path: string): string {
  return path
    .replace(/\\/g, "/")
    .replace(/^\/+/, "")
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent)
    .join("/");
}

function getHtmlPreviewBaseHref(doc: Document | null): string | null {
  if (!doc?.entity_id || !doc.fs_path) return null;
  const normalized = doc.fs_path.replace(/\\/g, "/").replace(/^\/+/, "");
  const lastSlash = normalized.lastIndexOf("/");
  const directory = lastSlash >= 0 ? normalized.slice(0, lastSlash) : "";
  const encodedDirectory = encodeFsPath(directory);
  return encodedDirectory
    ? `/api/v1/fs/${encodeURIComponent(doc.entity_id)}/${encodedDirectory}/`
    : `/api/v1/fs/${encodeURIComponent(doc.entity_id)}/`;
}

function escapeHtmlAttribute(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function injectHtmlBase(content: string, baseHref: string | null): string {
  if (!baseHref || /<base\b/i.test(content)) return content;
  const baseTag = `<base href="${escapeHtmlAttribute(baseHref)}">`;
  if (/<head(\s[^>]*)?>/i.test(content)) {
    return content.replace(/<head(\s[^>]*)?>/i, (match) => `${match}\n${baseTag}`);
  }
  if (/<html(\s[^>]*)?>/i.test(content)) {
    return content.replace(/<html(\s[^>]*)?>/i, (match) => `${match}\n<head>${baseTag}</head>`);
  }
  return `<!doctype html><html><head>${baseTag}</head><body>${content}</body></html>`;
}

function HtmlViewer({ content, doc }: { content: string; doc: Document | null }) {
  const srcDoc = useMemo(() => injectHtmlBase(content, getHtmlPreviewBaseHref(doc)), [content, doc]);
  return (
    <div className="html-viewer-stage">
      <iframe
        title={doc?.name || "HTML preview"}
        className="html-viewer-frame"
        srcDoc={srcDoc}
        sandbox="allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-scripts"
      />
    </div>
  );
}

function imageExportType(name?: string, mimeType?: string | null): { mime: string; extension: string } {
  const cleanMime = (mimeType || "").split(";")[0].trim().toLowerCase();
  if (cleanMime === "image/jpeg" || cleanMime === "image/jpg") return { mime: "image/jpeg", extension: "jpg" };
  if (cleanMime === "image/webp") return { mime: "image/webp", extension: "webp" };
  if (cleanMime === "image/png") return { mime: "image/png", extension: "png" };

  const ext = (name || "").split(".").pop()?.toLowerCase();
  if (ext === "jpg" || ext === "jpeg") return { mime: "image/jpeg", extension: "jpg" };
  if (ext === "webp") return { mime: "image/webp", extension: "webp" };
  return { mime: "image/png", extension: "png" };
}

function imageEditFileName(name?: string, extension = "png") {
  const base = (name || "edited-image").replace(/\.[^.]+$/, "");
  return `${base}.${extension}`;
}

async function imageUrlToObjectUrl(imageUrl: string): Promise<string> {
  if (imageUrl.startsWith("blob:") || imageUrl.startsWith("data:")) return imageUrl;
  const token = getAuthToken();
  const response = await fetch(imageUrl, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!response.ok) {
    throw new Error(`Generated image could not be loaded (${response.status}).`);
  }
  return URL.createObjectURL(await response.blob());
}

function extractReplacementImageUrl(raw: unknown): string | null {
  const source = raw && typeof raw === "object" && "edits" in raw
    ? (raw as { edits?: unknown }).edits
    : raw;
  if (!source || typeof source !== "object") return null;
  const edits = source as Record<string, unknown>;
  for (const key of ["replacementImageUrl", "replacement_image_url", "imageUrl", "image_url", "url"]) {
    const candidate = edits[key];
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return null;
}

function looksLikeScriptMarkdown(text: string): boolean {
  const lines = text.split(/\r?\n/).map((line) => line.trimEnd()).filter(Boolean);
  if (lines.length < 8 || /```/.test(text)) return false;
  const codeSignals = lines.filter((line) => {
    const trimmed = line.trim();
    return (
      /^#!\//.test(trimmed) ||
      /^(import|from|def|class|try:|except|finally:|for |while |if |elif |else:|with )/.test(trimmed) ||
      /^[A-Z_][A-Z0-9_]*\s*=/.test(trimmed) ||
      /^[a-zA-Z_][\w.]*\s*=/.test(trimmed) ||
      /\b(subprocess|os\.|json\.|Image\.|print\(|return |lambda\b|=>)\b/.test(trimmed)
    );
  }).length;
  const markdownSignals = lines.filter((line) => /^(#{1,6}\s+|[-*]\s+|\d+\.\s+|>\s+)/.test(line.trim())).length;
  return codeSignals >= 8 && codeSignals > markdownSignals * 1.5 && codeSignals / lines.length > 0.22;
}

function parseCSV(text: string): string[][] {
  const rows: string[][] = [];
  for (const line of text.split("\n")) {
    if (line.trim() === "") continue;
    const cells: string[] = [];
    let current = "";
    let inQuotes = false;
    for (const ch of line) {
      if (ch === '"') { inQuotes = !inQuotes; continue; }
      if (ch === "," && !inQuotes) { cells.push(current.trim()); current = ""; continue; }
      current += ch;
    }
    cells.push(current.trim());
    rows.push(cells);
  }
  return rows;
}

function colorizeJSON(text: string): string {
  try {
    const obj = JSON.parse(text);
    const pretty = JSON.stringify(obj, null, 2);
    const colored = pretty
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"([^"]+)":/g, '<span style="color:#93c5fd">"$1"</span>:')
      .replace(/: "([^"]*)"/g, ': <span style="color:#86efac">"$1"</span>')
      .replace(/: (\d+\.?\d*)/g, ': <span style="color:#fcd34d">$1</span>')
      .replace(/: (true|false)/g, ': <span style="color:#c4b5fd">$1</span>')
      .replace(/: (null)/g, ': <span style="color:#a8a29e">$1</span>');
    return colored;
  } catch {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
}

// ── DOCX viewer ──
function DocxViewer({
  url,
  commentAnchors = [],
  activeCommentId,
  onSelectCommentId,
}: {
  url: string;
  commentAnchors?: Comment[];
  activeCommentId?: string | null;
  onSelectCommentId?: (commentId: string) => void;
}) {
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const previewRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const bytes = new Uint8Array(buf);
        // Real DOCX starts with PK zip signature (0x50 0x4B)
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const mammoth = await import("mammoth");
          const result = await mammoth.convertToHtml({ arrayBuffer: buf });
          setHtml(result.value);
        } else {
          // Previously saved as HTML text
          setHtml(new TextDecoder().decode(buf));
        }
      } catch (e: any) {
        setError(e.message || "Failed to render DOCX");
      } finally {
        setLoading(false);
      }
    })();
  }, [url]);

  useEffect(() => {
    const root = previewRef.current;
    if (!root || !html) return;
    root.innerHTML = html;
    markQuoteCommentsInElement(root, quoteAnchoredComments(commentAnchors), activeCommentId);
  }, [activeCommentId, commentAnchors, html]);

  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 64 }}><LoadingSpinner size={28} /></div>;
  if (error) return <p style={{ color: "#c14a44", textAlign: "center", padding: 32 }}>{error}</p>;

  return (
    <div className="docx-viewer-stage">
      <div
        ref={previewRef}
        className="docx-preview docx-viewer-page"
        onClick={(event) => {
          const target = event.target instanceof HTMLElement
            ? event.target.closest<HTMLElement>(".document-comment-mark")
            : null;
          const commentId = target?.dataset.commentId;
          if (commentId) onSelectCommentId?.(commentId);
        }}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}

// ── XLSX viewer ──
function XlsxViewer({ url }: { url: string }) {
  const [sheets, setSheets] = useState<{ name: string; data: any[][] }[]>([]);
  const [activeSheet, setActiveSheet] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const bytes = new Uint8Array(buf);
        // Real XLSX starts with PK zip signature (0x50 0x4B)
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const XLSX = await import("xlsx");
          const wb = XLSX.read(buf, { type: "array" });
          const parsed = wb.SheetNames.map((name) => ({
            name,
            data: XLSX.utils.sheet_to_json<any[]>(wb.Sheets[name], { header: 1 }) as any[][],
          }));
          setSheets(parsed);
        } else {
          // Previously saved as CSV text — parse manually
          const text = new TextDecoder().decode(buf);
          const rows = text.split("\n").map(line => line.split(","));
          setSheets([{ name: "Sheet1", data: rows }]);
        }
      } catch (e: any) {
        setError(e.message || "Failed to render spreadsheet");
      } finally {
        setLoading(false);
      }
    })();
  }, [url]);

  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 64 }}><LoadingSpinner size={28} /></div>;
  if (error) return <p style={{ color: "#c14a44", textAlign: "center", padding: 32 }}>{error}</p>;
  if (sheets.length === 0) return <p style={{ color: "#78716c", textAlign: "center", padding: 32 }}>{t("page.file_viewer.empty_spreadsheet")}</p>;

  const sheet = sheets[activeSheet];

  return (
    <div>
      {/* Sheet tabs */}
      {sheets.length > 1 && (
        <div style={{ display: "flex", gap: 4, marginBottom: 16, flexWrap: "wrap" }}>
          {sheets.map((s, i) => (
            <button
              key={s.name}
              onClick={() => setActiveSheet(i)}
              style={{
                padding: "6px 16px",
                fontSize: 13,
                fontWeight: i === activeSheet ? 700 : 500,
                color: i === activeSheet ? "#436b65" : "#78716c",
                background: i === activeSheet ? "rgba(67,107,101,0.08)" : "transparent",
                border: "1px solid " + (i === activeSheet ? "rgba(67,107,101,0.2)" : "rgba(231,229,228,0.6)"),
                borderRadius: 8,
                cursor: "pointer",
                transition: "all 0.15s",
              }}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}

      {/* Table */}
      <div style={{ overflow: "auto", maxHeight: 600, borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          {sheet.data.length > 0 && (
            <thead>
              <tr>
                <th style={thStyle}>#</th>
                {sheet.data[0].map((_: any, ci: number) => (
                  <th key={ci} style={thStyle}>{colLetter(ci)}</th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {sheet.data.map((row, ri) => (
              <tr key={ri}>
                <td style={{ ...tdStyle, color: "#a8a29e", fontWeight: 600, background: "#fafaf9", textAlign: "center", width: 48 }}>{ri + 1}</td>
                {row.map((cell: any, ci: number) => (
                  <td key={ci} style={tdStyle}>{cell != null ? String(cell) : ""}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ fontSize: 12, color: "#a8a29e", marginTop: 8 }}>
        {sheet.data.length} {t("page.file_viewer.rows_sheet")} {sheet.name}
      </p>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  padding: "8px 12px",
  fontSize: 11,
  fontWeight: 700,
  color: "#78716c",
  background: "#fafaf9",
  borderBottom: "2px solid rgba(28,25,23,0.06)",
  textAlign: "left",
  position: "sticky",
  top: 0,
  whiteSpace: "nowrap",
};

const tdStyle: React.CSSProperties = {
  padding: "6px 12px",
  borderBottom: "1px solid #f5f5f4",
  whiteSpace: "nowrap",
  maxWidth: 300,
  overflow: "hidden",
  textOverflow: "ellipsis",
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

// ── PPTX types & helpers ──

/** EMU → percentage of slide (defaults 12192000 × 6858000 EMU for 16:9, overridden per-file) */
let SLIDE_W = 12192000;
let SLIDE_H = 6858000;
const emu2pctX = (v: number) => (v / SLIDE_W) * 100;
const emu2pctY = (v: number) => (v / SLIDE_H) * 100;
/** EMU to pt (for font sizes: 1pt = 12700 EMU) */
const emu2pt = (v: number) => Math.round(v / 12700);

interface PptxTextRun {
  text: string;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number;
  color?: string;
  fontFamily?: string;
  baseline?: number; // superscript (+) / subscript (-)
  spacing?: number; // letter spacing in pt
}

interface PptxParagraph {
  text: string; // concatenated plain text (backward compat)
  bold?: boolean; italic?: boolean; underline?: boolean;
  fontSize?: number; color?: string; align?: string; fontFamily?: string;
  bullet?: string; indent?: number;
  lineSpacing?: number; // multiplier (1.0 = single)
  spaceBefore?: number; // pt
  spaceAfter?: number; // pt
  runs?: PptxTextRun[]; // per-run formatting for rich rendering
}

interface PptxShape {
  x: number; y: number; w: number; h: number; // percentages
  type?: "shape" | "table" | "image" | "line";
  fill?: string;
  gradFill?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  borderRadius?: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
  stroke?: string;
  strokeWidth?: number;
  presetGeom?: string;
  texts: PptxParagraph[];
  imgUrl?: string;
  imgCrop?: { l: number; t: number; r: number; b: number }; // percentages
  opacity?: number;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  vAlign?: "top" | "middle" | "bottom"; // text vertical alignment
  padding?: { l: number; t: number; r: number; b: number }; // text insets in %
  tableRows?: { text: string; bold?: boolean; color?: string; fill?: string }[][];
  tableColWidths?: number[]; // column widths in EMU
  tableCols?: number;
}

interface PptxSlide {
  bg?: string;
  bgGrad?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  bgImgUrl?: string;
  shapes: PptxShape[];
  aspectRatio?: string;
}

function xmlAttr(el: string, attr: string): string | null {
  const re = new RegExp(`${attr}="([^"]*)"`, "i");
  const m = el.match(re);
  return m ? m[1] : null;
}

/** Find content between an XML open/close tag (non-greedy, first match) */
function xmlInner(xml: string, tag: string): string | null {
  // handle <tag ... > ... </tag> or self-closing
  const re = new RegExp(`<${tag}[\\s>][\\s\\S]*?</${tag}>`, "i");
  const m = xml.match(re);
  return m ? m[0] : null;
}

const _viewerDefaultScheme: Record<string, string> = {
  dk1: "#000000", dk2: "#292524", lt1: "#ffffff", lt2: "#fafaf9",
  accent1: "#4472c4", accent2: "#ed7d31", accent3: "#a5a5a5",
  accent4: "#ffc000", accent5: "#5b9bd5", accent6: "#70ad47",
  tx1: "#000000", tx2: "#57534e", bg1: "#ffffff", bg2: "#f5f5f4",
  hlink: "#0563c1", folHlink: "#954f72",
};
let _viewerTheme: Record<string, string> = _viewerDefaultScheme;
// Theme fill styles from <a:fmtScheme>
let _viewerBgFillStyles: string[] = [];  // <a:bgFillStyleLst> — for bgRef idx 1001+
let _viewerFillStyles: string[] = [];    // <a:fillStyleLst> — for bgRef idx 1-3
// Theme fonts
let _viewerMajorFont = "";  // heading font (for +mj-lt)
let _viewerMinorFont = "";  // body font (for +mn-lt)

function parseViewerTheme(themeXml: string) {
  const colors: Record<string, string> = { ..._viewerDefaultScheme };
  const clrScheme = xmlInner(themeXml, "a:clrScheme");
  if (!clrScheme) return;
  for (const tag of ["dk1", "dk2", "lt1", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]) {
    const inner = xmlInner(clrScheme, `a:${tag}`);
    if (inner) {
      let m = inner.match(/<a:srgbClr val="([A-Fa-f0-9]{6})"/);
      if (m) { colors[tag] = `#${m[1]}`; continue; }
      m = inner.match(/<a:sysClr[^>]*lastClr="([A-Fa-f0-9]{6})"/);
      if (m) { colors[tag] = `#${m[1]}`; }
    }
  }
  colors.tx1 = colors.dk1; colors.tx2 = colors.dk2;
  colors.bg1 = colors.lt1; colors.bg2 = colors.lt2;
  _viewerTheme = colors;

  // Parse font scheme
  _viewerMajorFont = "";
  _viewerMinorFont = "";
  try {
    const fontScheme = xmlInner(themeXml, "a:fontScheme");
    if (fontScheme) {
      const majorFont = xmlInner(fontScheme, "a:majorFont");
      const minorFont = xmlInner(fontScheme, "a:minorFont");
      if (majorFont) {
        const m = majorFont.match(/<a:latin typeface="([^"]+)"/);
        if (m) _viewerMajorFont = m[1];
      }
      if (minorFont) {
        const m = minorFont.match(/<a:latin typeface="([^"]+)"/);
        if (m) _viewerMinorFont = m[1];
      }
    }
  } catch { /* non-fatal */ }

  // Parse fill styles from theme fmtScheme
  _viewerBgFillStyles = [];
  _viewerFillStyles = [];
  try {
    const extractFills = (xml: string) => {
      const fills = xml.match(/<a:(solidFill|gradFill|pattFill|blipFill)[\s>][\s\S]*?<\/a:\1>/g) || [];
      return [...fills];
    };
    const bgFillLst = xmlInner(themeXml, "a:bgFillStyleLst");
    if (bgFillLst) _viewerBgFillStyles = extractFills(bgFillLst);
    const fillLst = xmlInner(themeXml, "a:fillStyleLst");
    if (fillLst) _viewerFillStyles = extractFills(fillLst);
  } catch { /* non-fatal */ }
}

function viewerApplyLum(hex: string, mod: number, off: number, tint?: number, shade?: number): string {
  let r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  if (tint !== undefined) { r = Math.round(r + (255 - r) * (1 - tint)); g = Math.round(g + (255 - g) * (1 - tint)); b = Math.round(b + (255 - b) * (1 - tint)); }
  if (shade !== undefined) { r = Math.round(r * shade); g = Math.round(g * shade); b = Math.round(b * shade); }
  if (mod !== 1 || off !== 0) { r = Math.round(Math.min(255, Math.max(0, r * mod + 255 * off))); g = Math.round(Math.min(255, Math.max(0, g * mod + 255 * off))); b = Math.round(Math.min(255, Math.max(0, b * mod + 255 * off))); }
  return [r, g, b].map(v => Math.min(255, Math.max(0, v)).toString(16).padStart(2, "0")).join("");
}

/** Resolve font typeface — handles theme font references +mj-lt, +mn-lt */
function resolveFont(typeface: string | null): string | undefined {
  if (!typeface) return undefined;
  if (typeface === "+mj-lt" || typeface === "+mj-ea" || typeface === "+mj-cs") return _viewerMajorFont || undefined;
  if (typeface === "+mn-lt" || typeface === "+mn-ea" || typeface === "+mn-cs") return _viewerMinorFont || undefined;
  return typeface;
}

function parseColor(xml: string): string | null {
  let m = xml.match(/<a:srgbClr val="([A-Fa-f0-9]{6})"/);
  if (m) {
    const lumMod = xml.match(/<a:lumMod val="(\d+)"/);
    const lumOff = xml.match(/<a:lumOff val="(\d+)"/);
    let hex = m[1];
    if (lumMod || lumOff) hex = viewerApplyLum(hex, lumMod ? parseInt(lumMod[1], 10) / 100000 : 1, lumOff ? parseInt(lumOff[1], 10) / 100000 : 0);
    return `#${hex}`;
  }
  // System color
  m = xml.match(/<a:sysClr[^>]*lastClr="([A-Fa-f0-9]{6})"/);
  if (m) return `#${m[1]}`;
  m = xml.match(/<a:sysClr val="([^"]+)"/);
  if (m) {
    const sysColors: Record<string, string> = { windowText: "#000000", window: "#ffffff", highlight: "#0078d4", highlightText: "#ffffff" };
    return sysColors[m[1]] || "#000000";
  }
  m = xml.match(/<a:schemeClr val="([^"]+)"/);
  if (m) {
    const base = _viewerTheme[m[1]] || _viewerDefaultScheme[m[1]] || "#57534e";
    const lumMod = xml.match(/<a:lumMod val="(\d+)"/);
    const lumOff = xml.match(/<a:lumOff val="(\d+)"/);
    const tint = xml.match(/<a:tint val="(\d+)"/);
    const shade = xml.match(/<a:shade val="(\d+)"/);
    if (lumMod || lumOff || tint || shade) {
      return `#${viewerApplyLum(base.replace("#", ""),
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

/** Resolve a <p:bgRef> or <p:bg> to background color/gradient. */
function parseBgFromXml(bgXml: string): { color?: string; grad?: PptxSlide["bgGrad"]; imgRId?: string } {
  const result: { color?: string; grad?: PptxSlide["bgGrad"]; imgRId?: string } = {};

  // Check for <p:bgPr> with explicit fill
  const bgPr = xmlInner(bgXml, "p:bgPr");
  if (bgPr) {
    result.color = parseColor(bgPr) || undefined;
    result.grad = parseGradient(bgPr);
    const blip = bgPr.match(/<a:blip r:embed="([^"]+)"/);
    if (blip) result.imgRId = blip[1];
    if (result.color || result.grad || result.imgRId) return result;
  }

  // Check for <p:bgRef idx="N"> — references theme bgFillStyleLst
  const bgRef = xmlInner(bgXml, "p:bgRef");
  if (bgRef) {
    const idxMatch = bgRef.match(/idx="(\d+)"/);
    const idx = idxMatch ? parseInt(idxMatch[1], 10) : 0;

    // The <a:schemeClr> inside bgRef provides the color to apply to the fill style
    const refColor = parseColor(bgRef);

    // idx 1001+ → bgFillStyleLst (1001→0, 1002→1, ...)
    // idx 1-999 → fillStyleLst (1→0, 2→1, ...)
    const styleList = idx >= 1001 ? _viewerBgFillStyles : _viewerFillStyles;
    const fillIdx = idx >= 1001 ? idx - 1001 : idx - 1;

    if (fillIdx >= 0 && fillIdx < styleList.length) {
      const fillXml = styleList[fillIdx];
      if (fillXml.includes("<a:solidFill")) {
        result.color = refColor || parseColor(fillXml) || undefined;
      } else if (fillXml.includes("<a:gradFill")) {
        result.grad = parseGradient(fillXml, refColor || undefined);
        if (!result.grad && refColor) result.color = refColor;
      }
    }
    if (!result.color && !result.grad && refColor) {
      result.color = refColor;
    }
  }

  // Fallback: try direct color/gradient in the <p:bg> block
  if (!result.color && !result.grad) {
    result.color = parseColor(bgXml) || undefined;
    result.grad = parseGradient(bgXml);
    const blip = bgXml.match(/<a:blip r:embed="([^"]+)"/);
    if (blip) result.imgRId = blip[1];
  }

  return result;
}

function parseGradient(xml: string, phClrOverride?: string): PptxShape["gradFill"] | undefined {
  const gradXml = xmlInner(xml, "a:gradFill");
  if (!gradXml) return undefined;
  const stops: { pos: number; color: string; alpha: number }[] = [];
  const gsMatches = gradXml.match(/<a:gs[\s>][\s\S]*?<\/a:gs>/g) || [];
  for (const gs of gsMatches) {
    const pos = parseInt(xmlAttr(gs, "pos") || "0", 10) / 1000;
    // If this stop uses phClr (placeholder color), substitute with the override
    const usesPhClr = /schemeClr val="phClr"/.test(gs);
    let color: string;
    if (usesPhClr && phClrOverride) {
      // Apply tint/shade/lumMod/lumOff modifiers to the override color
      const lumMod = gs.match(/<a:lumMod val="(\d+)"/);
      const lumOff = gs.match(/<a:lumOff val="(\d+)"/);
      const tint = gs.match(/<a:tint val="(\d+)"/);
      const shade = gs.match(/<a:shade val="(\d+)"/);
      if (lumMod || lumOff || tint || shade) {
        color = `#${viewerApplyLum(
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
      color = parseColor(gs) || "#000000";
    }
    const alphaM = gs.match(/<a:alpha val="(\d+)"/);
    const alpha = alphaM ? parseInt(alphaM[1], 10) / 100000 : 1;
    stops.push({ pos, color, alpha });
  }
  const angMatch = gradXml.match(/<a:lin ang="(\d+)"/);
  const angle = angMatch ? parseInt(angMatch[1], 10) / 60000 : 0;
  // Detect radial/path gradient
  const isRadial = /<a:path\s/.test(gradXml);
  return stops.length > 0 ? { angle: isRadial ? -1 : angle, stops } : undefined;
}

function gradientToCss(g: { angle: number; stops: { pos: number; color: string; alpha: number }[] }): string {
  const stopStr = g.stops
    .map(s => {
      const r = parseInt(s.color.slice(1, 3), 16);
      const gv = parseInt(s.color.slice(3, 5), 16);
      const b = parseInt(s.color.slice(5, 7), 16);
      return `rgba(${r},${gv},${b},${s.alpha}) ${s.pos}%`;
    })
    .join(", ");
  // Radial gradient (angle === -1 sentinel)
  if (g.angle === -1) return `radial-gradient(ellipse at center, ${stopStr})`;
  // PPTX: 0°=right, CSS: 0°=up → add 90°
  return `linear-gradient(${g.angle + 90}deg, ${stopStr})`;
}

function parseXfrm(spXml: string): { x: number; y: number; w: number; h: number } | null {
  const xfrm = xmlInner(spXml, "a:xfrm");
  if (!xfrm) return null;
  const offM = xfrm.match(/<a:off x="(\d+)" y="(\d+)"/);
  const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/);
  if (!offM || !extM) return null;
  return {
    x: emu2pctX(parseInt(offM[1], 10)),
    y: emu2pctY(parseInt(offM[2], 10)),
    w: emu2pctX(parseInt(extM[1], 10)),
    h: emu2pctY(parseInt(extM[2], 10)),
  };
}

function parseTextRuns(spXml: string): PptxShape["texts"] {
  const texts: PptxShape["texts"] = [];
  const paras = spXml.match(/<a:p>[\s\S]*?<\/a:p>/g) || spXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [];
  for (const para of paras) {
    const pPr = xmlInner(para, "a:pPr");
    const align = pPr ? (xmlAttr(pPr, "algn") || undefined) : undefined;
    const lvl = pPr ? parseInt(xmlAttr(pPr, "lvl") || "0", 10) : 0;
    const marL = pPr ? xmlAttr(pPr, "marL") : null;
    const indent = marL ? parseInt(marL, 10) / 12700 : lvl * 18;

    let bullet: string | undefined;
    if (pPr) {
      const buChar = pPr.match(/<a:buChar char="([^"]+)"/);
      if (buChar) bullet = buChar[1];
      else if (pPr.includes("<a:buAutoNum")) {
        // Determine numbering type — we'll use a placeholder; actual number is per-slide counter
        const autoNumType = (pPr.match(/<a:buAutoNum type="([^"]+)"/) || [])[1] || "arabicPeriod";
        if (autoNumType.startsWith("alpha")) bullet = "a.";
        else if (autoNumType.startsWith("roman")) bullet = "i.";
        else bullet = "#."; // resolved to actual number during rendering
      }
      else if (!pPr.includes("<a:buNone") && lvl > 0) bullet = "\u2022";
    }

    // Line/paragraph spacing
    let lineSpacing: number | undefined, spaceBefore: number | undefined, spaceAfter: number | undefined;
    if (pPr) {
      const lnSpc = xmlInner(pPr, "a:lnSpc");
      if (lnSpc) {
        const spcPct = lnSpc.match(/<a:spcPct val="(\d+)"/);
        if (spcPct) lineSpacing = parseInt(spcPct[1], 10) / 100000;
        const spcPts = lnSpc.match(/<a:spcPts val="(\d+)"/);
        if (spcPts) lineSpacing = parseInt(spcPts[1], 10) / 100 / 12; // approximate pt → line multiplier
      }
      const spcBef = xmlInner(pPr, "a:spcBef");
      if (spcBef) {
        const pts = spcBef.match(/<a:spcPts val="(\d+)"/);
        if (pts) spaceBefore = parseInt(pts[1], 10) / 100;
      }
      const spcAft = xmlInner(pPr, "a:spcAft");
      if (spcAft) {
        const pts = spcAft.match(/<a:spcPts val="(\d+)"/);
        if (pts) spaceAfter = parseInt(pts[1], 10) / 100;
      }
    }

    // Parse default paragraph-level text properties (defRPr and endParaRPr)
    let defFontSize: number | undefined, defColor: string | undefined;
    let defBold = false, defItalic = false, defFontFamily: string | undefined;
    const defRPr = pPr ? xmlInner(pPr, "a:defRPr") : null;
    if (defRPr) {
      const szM = xmlAttr(defRPr, "sz");
      if (szM) defFontSize = parseInt(szM, 10) / 100;
      const c = parseColor(defRPr);
      if (c) defColor = c;
      defBold = defRPr.includes('b="1"');
      defItalic = defRPr.includes('i="1"');
      const latin = defRPr.match(/<a:latin typeface="([^"]+)"/);
      const ea = defRPr.match(/<a:ea typeface="([^"]+)"/);
      defFontFamily = resolveFont(latin?.[1] ?? null) || resolveFont(ea?.[1] ?? null) || defFontFamily;
    }
    const endParaRPr = xmlInner(para, "a:endParaRPr");
    if (endParaRPr) {
      const szM = xmlAttr(endParaRPr, "sz");
      if (szM && !defFontSize) defFontSize = parseInt(szM, 10) / 100;
      const c = parseColor(endParaRPr);
      if (c && !defColor) defColor = c;
    }

    // Collect runs with individual formatting
    const tokens = para.match(/<a:r[\s>][\s\S]*?<\/a:r>|<a:br\s*\/>|<a:br[\s>][\s\S]*?<\/a:br>|<a:fld[\s>][\s\S]*?<\/a:fld>/g) || [];
    const runs: PptxTextRun[] = [];
    let paraText = "";
    // Track first run's style for backward-compat paragraph-level props
    let firstBold = defBold, firstItalic = defItalic, firstUnderline = false;
    let firstFontSize = defFontSize, firstColor = defColor, firstFontFamily = defFontFamily;
    let isFirst = true;

    for (const token of tokens) {
      if (token.startsWith("<a:br")) {
        paraText += "\n";
        runs.push({ text: "\n" });
        continue;
      }
      let rBold = defBold, rItalic = defItalic, rUnderline = false;
      let rFontSize = defFontSize, rColor = defColor, rFontFamily = defFontFamily;

      let rStrike = false, rBaseline: number | undefined, rSpacing: number | undefined;
      const rPr = xmlInner(token, "a:rPr");
      if (rPr) {
        rBold = rPr.includes('b="1"');
        rItalic = rPr.includes('i="1"');
        rUnderline = rPr.includes('u="sng"') || rPr.includes('u="dbl"') || rPr.includes('u="heavy"');
        rStrike = rPr.includes('strike="sngStrike"') || rPr.includes('strike="dblStrike"');
        const szM = xmlAttr(rPr, "sz");
        if (szM) rFontSize = parseInt(szM, 10) / 100;
        const runColor = parseColor(rPr);
        if (runColor) rColor = runColor;
        const latin = rPr.match(/<a:latin typeface="([^"]+)"/);
        const ea = rPr.match(/<a:ea typeface="([^"]+)"/);
        rFontFamily = resolveFont(latin?.[1] ?? null) || resolveFont(ea?.[1] ?? null) || rFontFamily;
        const baselineM = xmlAttr(rPr, "baseline");
        if (baselineM) rBaseline = parseInt(baselineM, 10) / 1000;
        const spcM = xmlAttr(rPr, "spc");
        if (spcM) rSpacing = parseInt(spcM, 10) / 100;
      }
      const tMatch = token.match(/<a:t>([^<]*)<\/a:t>/);
      const runText = tMatch ? tMatch[1] : "";
      if (!runText) continue;
      paraText += runText;
      runs.push({
        text: runText,
        bold: rBold || undefined,
        italic: rItalic || undefined,
        underline: rUnderline || undefined,
        strikethrough: rStrike || undefined,
        fontSize: rFontSize,
        color: rColor,
        fontFamily: rFontFamily,
        baseline: rBaseline,
        spacing: rSpacing,
      });
      if (isFirst) {
        firstBold = rBold; firstItalic = rItalic; firstUnderline = rUnderline;
        firstFontSize = rFontSize; firstColor = rColor; firstFontFamily = rFontFamily;
        isFirst = false;
      }
    }

    if (paraText.trim() || paraText.includes("\n")) {
      texts.push({
        text: paraText,
        bold: firstBold, italic: firstItalic, underline: firstUnderline,
        fontSize: firstFontSize, color: firstColor, fontFamily: firstFontFamily,
        align: align === "ctr" ? "center" : align === "r" ? "right" : align === "just" ? "justify" : undefined,
        bullet, indent: indent > 0 ? indent : undefined,
        lineSpacing, spaceBefore, spaceAfter,
        runs: runs.length > 1 ? runs : undefined, // only include runs if mixed formatting
      });
    } else {
      texts.push({ text: "", fontSize: firstFontSize || defFontSize || 12 });
    }
  }
  return texts;
}

function viewerParseStroke(xml: string): { color?: string; width?: number } {
  const ln = xmlInner(xml, "a:ln");
  if (!ln || ln.includes("<a:noFill")) return {};
  const color = parseColor(ln);
  const wAttr = xmlAttr(ln, "w");
  const width = wAttr ? parseInt(wAttr, 10) / 12700 : 1;
  return { color: color || undefined, width: color ? width : undefined };
}

function viewerParseTable(xml: string): { rows: { text: string; bold?: boolean; color?: string; fill?: string; gridSpan?: number; vMerge?: boolean }[][]; cols: number; colWidths?: number[] } | null {
  const tbl = xmlInner(xml, "a:tbl");
  if (!tbl) return null;

  // Parse column widths from <a:tblGrid>
  const tblGrid = xmlInner(tbl, "a:tblGrid");
  let colWidths: number[] | undefined;
  if (tblGrid) {
    const gridCols = tblGrid.match(/<a:gridCol[^>]*\/>/g) || [];
    colWidths = gridCols.map(gc => parseInt((gc.match(/w="(\d+)"/) || [])[1] || "0", 10));
  }

  const trMatches = tbl.match(/<a:tr[\s>][\s\S]*?<\/a:tr>/g) || [];
  const rows: { text: string; bold?: boolean; color?: string; fill?: string; gridSpan?: number; vMerge?: boolean }[][] = [];
  let maxCols = 0;
  for (const tr of trMatches) {
    const tcMatches = tr.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/g) || [];
    const row: { text: string; bold?: boolean; color?: string; fill?: string; gridSpan?: number; vMerge?: boolean }[] = [];
    for (const tc of tcMatches) {
      const text = (tc.match(/<a:t>([^<]*)<\/a:t>/g) || []).map(m => m.replace(/<\/?a:t>/g, "")).join(" ");
      const rPr = xmlInner(tc, "a:rPr");
      const tcPr = xmlInner(tc, "a:tcPr");
      const gridSpanM = tc.match(/gridSpan="(\d+)"/);
      const vMerge = tc.includes('vMerge="1"') || tc.includes("hMerge=\"1\"");
      row.push({
        text, bold: rPr?.includes('b="1"'),
        color: rPr ? parseColor(rPr) || undefined : undefined,
        fill: tcPr ? parseColor(tcPr) || undefined : undefined,
        gridSpan: gridSpanM ? parseInt(gridSpanM[1], 10) : undefined,
        vMerge: vMerge || undefined,
      });
    }
    rows.push(row);
    maxCols = Math.max(maxCols, row.length);
  }
  return rows.length > 0 ? { rows, cols: maxCols, colWidths } : null;
}

/** Extract placeholder type and idx from a shape XML */
function parsePlaceholder(spXml: string): { type?: string; idx?: string } | null {
  const phM = spXml.match(/<p:ph([^/>]*)\/?>/);
  if (!phM) return null;
  const type = xmlAttr(phM[0], "type") || undefined;
  const idx = xmlAttr(phM[0], "idx") || undefined;
  return { type, idx };
}

/** Build a map of placeholder key → position from layout/master XML */
function buildPlaceholderMap(xmlStr: string): Map<string, { x: number; y: number; w: number; h: number }> {
  const map = new Map<string, { x: number; y: number; w: number; h: number }>();
  const shapes = xmlStr.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [];
  for (const sp of shapes) {
    const ph = parsePlaceholder(sp);
    if (!ph) continue;
    const pos = parseXfrm(sp);
    if (!pos) continue;
    const key = ph.type || `idx:${ph.idx}`;
    map.set(key, pos);
    if (ph.idx) map.set(`idx:${ph.idx}`, pos);
  }
  return map;
}

function parseShape(spXml: string, relsMap: Map<string, string>, phMap?: Map<string, { x: number; y: number; w: number; h: number }>): PptxShape | null {
  let pos = parseXfrm(spXml);
  // Fallback: resolve position from placeholder map (layout/master inheritance)
  if (!pos && phMap) {
    const ph = parsePlaceholder(spXml);
    if (ph) {
      pos = phMap.get(ph.type || "") || phMap.get(`idx:${ph.idx}`) || null;
      // For body placeholder, also try "body" key from master
      if (!pos && ph.idx === "1") pos = phMap.get("body") || null;
    }
  }
  if (!pos) return null;

  const shape: PptxShape = { ...pos, type: "shape", texts: [] };

  // Rotation + flip
  const xfrm = xmlInner(spXml, "a:xfrm");
  if (xfrm) {
    const rotAttr = xmlAttr(xfrm, "rot");
    if (rotAttr) shape.rotation = parseInt(rotAttr, 10) / 60000;
    if (xfrm.includes('flipH="1"')) shape.flipH = true;
    if (xfrm.includes('flipV="1"')) shape.flipV = true;
  }

  // Preset geometry
  const geomM = spXml.match(/<a:prstGeom prst="([^"]+)"/);
  if (geomM) shape.presetGeom = geomM[1];

  // Fill — search only in spPr to avoid matching text color fills
  const spPr = xmlInner(spXml, "p:spPr");
  if (spPr && !spPr.includes("<a:noFill")) {
    const solidFill = xmlInner(spPr, "a:solidFill");
    if (solidFill) shape.fill = parseColor(solidFill) || undefined;
    shape.gradFill = parseGradient(spPr);

    // Blip fill (texture/image fill on shapes)
    if (!shape.fill && !shape.gradFill) {
      const blipFill = xmlInner(spPr, "a:blipFill") || xmlInner(spXml, "p:blipFill");
      if (blipFill) {
        const blipM = blipFill.match(/<a:blip r:embed="([^"]+)"/);
        if (blipM) { const u = relsMap.get(blipM[1]); if (u) shape.imgUrl = u; }
      }
    }
  }

  // Stroke/border — scope to spPr
  const stroke = viewerParseStroke(spPr || spXml);
  if (stroke.color) { shape.stroke = stroke.color; shape.strokeWidth = stroke.width; }

  // Alpha
  const alphaM = spXml.match(/<a:alpha val="(\d+)"/);
  if (alphaM) shape.opacity = parseInt(alphaM[1], 10) / 100000;

  // Shadow (outer shadow)
  const outerShdw = xmlInner(spXml, "a:outerShdw");
  if (outerShdw) {
    const shdwBlur = parseInt(xmlAttr(outerShdw, "blurRad") || "0", 10) / 12700;
    const shdwDist = parseInt(xmlAttr(outerShdw, "dist") || "0", 10) / 12700;
    const shdwAng = parseInt(xmlAttr(outerShdw, "dir") || "0", 10) / 60000;
    const shdwColor = parseColor(outerShdw) || "#000000";
    const shdwAlpha = outerShdw.match(/<a:alpha val="(\d+)"/) ? parseInt(outerShdw.match(/<a:alpha val="(\d+)"/)![1], 10) / 100000 : 0.4;
    shape.shadow = { blur: shdwBlur, dist: shdwDist, angle: shdwAng, color: shdwColor, alpha: shdwAlpha };
  }

  // Border radius (roundRect preset)
  if (spXml.includes('prst="roundRect"')) {
    const adjM = spXml.match(/name="adj" fmla="val (\d+)"/);
    shape.borderRadius = adjM ? Math.min(50, parseInt(adjM[1], 10) / 1000) : 8;
  }

  // Text body properties (vertical alignment + insets)
  const bodyPr = xmlInner(spXml, "a:bodyPr");
  if (bodyPr) {
    const anchor = xmlAttr(bodyPr, "anchor");
    if (anchor === "t") shape.vAlign = "top";
    else if (anchor === "b") shape.vAlign = "bottom";
    else if (anchor === "ctr") shape.vAlign = "middle";
    // Text insets (EMU → % of slide)
    const lIns = xmlAttr(bodyPr, "lIns");
    const tIns = xmlAttr(bodyPr, "tIns");
    const rIns = xmlAttr(bodyPr, "rIns");
    const bIns = xmlAttr(bodyPr, "bIns");
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
  shape.texts = parseTextRuns(spXml);

  // Line / connector detection
  if (spXml.startsWith("<p:cxnSp") || shape.presetGeom === "line" || shape.presetGeom === "straightConnector1") {
    shape.type = "line";
  }

  // Image (p:pic)
  if (!shape.imgUrl) {
    const blipM = spXml.match(/<a:blip r:embed="([^"]+)"/);
    if (blipM) {
      const imgUrl = relsMap.get(blipM[1]);
      if (imgUrl) { shape.imgUrl = imgUrl; shape.type = "image"; }
    }
  }
  // Image cropping (srcRect)
  if (shape.imgUrl) {
    const srcRect = spXml.match(/<a:srcRect\s+([^/]*)\/>/);
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

async function parsePptx(buf: ArrayBuffer): Promise<PptxSlide[]> {
  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(buf);
  const slides: PptxSlide[] = [];

  // Read slide size and slide order from presentation.xml
  let orderedSlideRIds: string[] = [];
  let presRelsMap = new Map<string, string>(); // rId → ppt/slides/slideN.xml
  try {
    const presEntry = zip.file("ppt/presentation.xml");
    if (presEntry) {
      const presXml = await presEntry.async("text");
      const sldSz = presXml.match(/<p:sldSz[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
      if (sldSz) {
        SLIDE_W = parseInt(sldSz[1], 10);
        SLIDE_H = parseInt(sldSz[2], 10);
      }
      // Extract slide order from <p:sldIdLst>
      const sldIdLst = presXml.match(/<p:sldId[^/]*\/>/g) || [];
      orderedSlideRIds = sldIdLst.map(s => (s.match(/r:id="([^"]+)"/) || [])[1]).filter(Boolean);
    }
    // Map rId → slide path from presentation.xml.rels
    const presRelsEntry = zip.file("ppt/_rels/presentation.xml.rels");
    if (presRelsEntry) {
      const presRelsXml = await presRelsEntry.async("text");
      for (const rel of (presRelsXml.match(/<Relationship[^>]*\/>/g) || [])) {
        const id = xmlAttr(rel, "Id");
        const target = xmlAttr(rel, "Target");
        if (id && target) {
          const resolved = target.startsWith("../") ? target.slice(3) : target.startsWith("/") ? target.slice(1) : `ppt/${target}`;
          presRelsMap.set(id, resolved);
        }
      }
    }
  } catch { /* use default 16:9, file-number order */ }

  // Parse theme (non-fatal)
  try {
    const themeEntry = zip.file("ppt/theme/theme1.xml");
    if (themeEntry) parseViewerTheme(await themeEntry.async("text"));
    else _viewerTheme = _viewerDefaultScheme;
  } catch { _viewerTheme = _viewerDefaultScheme; }

  // Determine slide file order: prefer presentation.xml order, fall back to filename sort
  let slideFiles: string[];
  if (orderedSlideRIds.length > 0 && presRelsMap.size > 0) {
    slideFiles = orderedSlideRIds.map(rId => presRelsMap.get(rId)).filter((p): p is string => !!p && /slide\d+\.xml$/.test(p));
  } else {
    slideFiles = Object.keys(zip.files)
      .filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n));
  }
  if (slideFiles.length === 0) {
    slideFiles = Object.keys(zip.files).filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n));
  }
  slideFiles.sort((a, b) => {
      const na = parseInt(a.match(/slide(\d+)/)?.[1] || "0", 10);
      const nb = parseInt(b.match(/slide(\d+)/)?.[1] || "0", 10);
      return na - nb;
    });

  // Extract all media as blob URLs
  const mediaCache = new Map<string, string>();
  for (const name of Object.keys(zip.files)) {
    if (name.startsWith("ppt/media/") && !zip.files[name].dir) {
      try {
        const entry = zip.file(name);
        if (!entry) continue;
        const data = await entry.async("blob");
        const ext = name.split(".").pop()?.toLowerCase() || "";
        const mime = ext === "png" ? "image/png" : ext === "svg" ? "image/svg+xml" : `image/${ext}`;
        mediaCache.set(name, URL.createObjectURL(new Blob([data], { type: mime })));
      } catch { /* skip bad media */ }
    }
  }

  // Pre-cache layouts and slide masters (non-fatal)
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
    // Build layout → master mapping from layout rels
    for (const layoutPath of layoutCache.keys()) {
      try {
        const layoutNum = layoutPath.match(/slideLayout(\d+)/)?.[1] || "1";
        const lrp = `ppt/slideLayouts/_rels/slideLayout${layoutNum}.xml.rels`;
        const lre = zip.file(lrp);
        if (lre) {
          const lrXml = await lre.async("text");
          for (const rel of (lrXml.match(/<Relationship[^>]*\/>/g) || [])) {
            const target = xmlAttr(rel, "Target");
            const type = xmlAttr(rel, "Type");
            if (target && type && type.includes("slideMaster")) {
              const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
              layoutToMasterPath.set(layoutPath, resolved);
            }
          }
        }
      } catch { /* non-fatal */ }
    }
    // Pre-cache master rels for media resolution
    for (const masterPath of masterCache.keys()) {
      try {
        const masterNum = masterPath.match(/slideMaster(\d+)/)?.[1] || "1";
        const mrp = `ppt/slideMasters/_rels/slideMaster${masterNum}.xml.rels`;
        const mre = zip.file(mrp);
        if (mre) {
          const mrXml = await mre.async("text");
          const masterRels = new Map<string, string>();
          for (const rel of (mrXml.match(/<Relationship[^>]*\/>/g) || [])) {
            const id = xmlAttr(rel, "Id");
            const target = xmlAttr(rel, "Target");
            if (id && target) {
              const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
              if (mediaCache.has(resolved)) masterRels.set(id, mediaCache.get(resolved)!);
            }
          }
          masterRelsCache.set(masterPath, masterRels);
        }
      } catch { /* non-fatal */ }
    }
  } catch { /* layouts/masters are optional enhancement */ }

  for (const slidePath of slideFiles) {
    try {
      const entry = zip.file(slidePath);
      if (!entry) continue;
      let xml = await entry.async("text");
      // Unwrap <mc:AlternateContent> — prefer <mc:Choice> (modern), fall back to <mc:Fallback>
      xml = xml.replace(/<mc:AlternateContent[\s>][\s\S]*?<\/mc:AlternateContent>/g, (block) => {
        const choice = block.match(/<mc:Choice[\s>]([\s\S]*?)<\/mc:Choice>/);
        if (choice && choice[1].trim()) return choice[1];
        const fallback = block.match(/<mc:Fallback[\s>]([\s\S]*?)<\/mc:Fallback>/);
        return fallback ? fallback[1] : "";
      });
      const slideNum = slidePath.match(/slide(\d+)/)?.[1] || "1";

      const relsMap = new Map<string, string>();
      let layoutPath: string | undefined;
      try {
        const relsEntry = zip.file(`ppt/slides/_rels/slide${slideNum}.xml.rels`);
        if (relsEntry) {
          const relsXml = await relsEntry.async("text");
          const relMatches = relsXml.match(/<Relationship[^>]*\/>/g) || [];
          for (const rel of relMatches) {
            const id = xmlAttr(rel, "Id");
            const target = xmlAttr(rel, "Target");
            const type = xmlAttr(rel, "Type");
            if (id && target) {
              const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
              if (mediaCache.has(resolved)) relsMap.set(id, mediaCache.get(resolved)!);
              if (type && type.includes("slideLayout")) layoutPath = resolved;
            }
          }
        }
        // Also resolve media from layout rels
        if (layoutPath) {
          const layoutRelsPath = layoutPath.replace(/slideLayouts\//, "slideLayouts/_rels/") + ".rels";
          const lre = zip.file(layoutRelsPath);
          if (lre) {
            const lrXml = await lre.async("text");
            for (const rel of (lrXml.match(/<Relationship[^>]*\/>/g) || [])) {
              const id = xmlAttr(rel, "Id");
              const target = xmlAttr(rel, "Target");
              if (id && target) {
                const resolved = target.startsWith("../") ? "ppt/" + target.slice(3) : target;
                if (mediaCache.has(resolved) && !relsMap.has(id)) relsMap.set(id, mediaCache.get(resolved)!);
              }
            }
          }
        }
      } catch { /* rels parsing non-fatal */ }

      const slide: PptxSlide = { shapes: [] };

      // Merge master rels into relsMap so master media (bg images, etc.) resolves
      if (layoutPath) {
        const masterPath = layoutToMasterPath.get(layoutPath);
        if (masterPath && masterRelsCache.has(masterPath)) {
          for (const [id, url] of masterRelsCache.get(masterPath)!) {
            if (!relsMap.has(id)) relsMap.set(id, url);
          }
        }
      }

      // Build placeholder position map: master → layout → slide (later overrides earlier)
      const phMap = new Map<string, { x: number; y: number; w: number; h: number }>();
      try {
        if (layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            for (const [k, v] of buildPlaceholderMap(masterCache.get(masterPath)!)) phMap.set(k, v);
          }
          if (layoutCache.has(layoutPath)) {
            for (const [k, v] of buildPlaceholderMap(layoutCache.get(layoutPath)!)) phMap.set(k, v);
          }
        }
      } catch { /* placeholder map non-fatal */ }

      // Background — slide → layout → slide master fallback
      try {
        let bgXml = xmlInner(xml, "p:bg");
        if (!bgXml && layoutPath && layoutCache.has(layoutPath)) {
          bgXml = xmlInner(layoutCache.get(layoutPath)!, "p:bg");
        }
        if (!bgXml && layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            bgXml = xmlInner(masterCache.get(masterPath)!, "p:bg");
          }
        }
        if (bgXml) {
          const bgResult = parseBgFromXml(bgXml);
          if (bgResult.color) slide.bg = bgResult.color;
          if (bgResult.grad) slide.bgGrad = bgResult.grad;
          if (bgResult.imgRId && relsMap.has(bgResult.imgRId)) slide.bgImgUrl = relsMap.get(bgResult.imgRId);
        }
      } catch { /* bg parsing non-fatal */ }

      // Parse all shape types + group children
      const spMatches = xml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [];
      const picMatches = xml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || [];
      const cxnMatches = xml.match(/<p:cxnSp[\s>][\s\S]*?<\/p:cxnSp>/g) || [];
      try {
        const grpMatches = xml.match(/<p:grpSp[\s>][\s\S]*?<\/p:grpSp>/g) || [];
        for (const grp of grpMatches) {
          // Parse group transform for coordinate mapping
          const grpSpPr = xmlInner(grp, "p:grpSpPr");
          const grpXfrm = grpSpPr ? xmlInner(grpSpPr, "a:xfrm") : null;
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
            const s = parseShape(ch, relsMap, phMap);
            if (s && grpXfrm && chExtCx > 0 && chExtCy > 0) {
              // Transform child coords from group space to slide space
              const childXEmu = (s.x / 100) * SLIDE_W;
              const childYEmu = (s.y / 100) * SLIDE_H;
              const childWEmu = (s.w / 100) * SLIDE_W;
              const childHEmu = (s.h / 100) * SLIDE_H;
              const mappedX = grpOffX + (childXEmu - chOffX) * (grpExtCx / chExtCx);
              const mappedY = grpOffY + (childYEmu - chOffY) * (grpExtCy / chExtCy);
              const mappedW = childWEmu * (grpExtCx / chExtCx);
              const mappedH = childHEmu * (grpExtCy / chExtCy);
              s.x = emu2pctX(mappedX); s.y = emu2pctY(mappedY);
              s.w = emu2pctX(mappedW); s.h = emu2pctY(mappedH);
            }
            if (s) slide.shapes.push(s);
          }
        }
      } catch { /* group extraction non-fatal */ }

      for (const sp of [...spMatches, ...picMatches, ...cxnMatches]) {
        try {
          const s = parseShape(sp, relsMap, phMap);
          if (s) slide.shapes.push(s);
        } catch { /* individual shape parse non-fatal */ }
      }

      // Tables (non-fatal)
      try {
        const gfMatches = xml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [];
        for (const gf of gfMatches) {
          const xfrm = xmlInner(gf, "a:xfrm") || xmlInner(gf, "p:xfrm");
          if (!xfrm) continue;
          const offM = xfrm.match(/<a:off x="(\d+)" y="(\d+)"/) || xfrm.match(/<p:off x="(\d+)" y="(\d+)"/);
          const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/) || xfrm.match(/<p:ext cx="(\d+)" cy="(\d+)"/);
          if (!offM || !extM) continue;
          const table = viewerParseTable(gf);
          if (table) {
            slide.shapes.push({
              type: "table",
              x: emu2pctX(parseInt(offM[1], 10)), y: emu2pctY(parseInt(offM[2], 10)),
              w: emu2pctX(parseInt(extM[1], 10)), h: emu2pctY(parseInt(extM[2], 10)),
              texts: [], tableRows: table.rows, tableCols: table.cols, tableColWidths: table.colWidths,
            });
          }
        }
      } catch { /* table parsing non-fatal */ }

      // Layout decorative shapes (non-fatal)
      try {
        if (layoutPath && layoutCache.has(layoutPath)) {
          const layoutXml = layoutCache.get(layoutPath)!;
          for (const sp of [
            ...(layoutXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
            ...(layoutXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
          ]) {
            if (sp.includes("<p:ph")) continue;
            const lShape = parseShape(sp, relsMap);
            if (lShape && (lShape.fill || lShape.gradFill || lShape.imgUrl || lShape.stroke)) {
              slide.shapes.unshift(lShape);
            }
          }
        }
      } catch { /* layout shapes non-fatal */ }

      // Slide master decorative shapes (non-fatal) — rendered behind layout shapes
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
              const mShape = parseShape(sp, relsMap);
              if (mShape && (mShape.fill || mShape.gradFill || mShape.imgUrl || mShape.stroke)) {
                slide.shapes.unshift(mShape);
              }
            }
          }
        }
      } catch { /* master shapes non-fatal */ }

      slide.aspectRatio = `${SLIDE_W}/${SLIDE_H}`;
      slides.push(slide);
    } catch {
      // If a single slide fails, add empty slide placeholder and continue
      slides.push({ shapes: [] });
    }
  }

  return slides;
}

// Slide accent colors for text-based slides
const SLIDE_ACCENTS = ["#4f7d75", "#4869ac", "#6f4ba8", "#c14a44", "#b27c34", "#437f6b"];

/** Parse AI-generated text content into styled slides */
function parseTextSlides(text: string): PptxSlide[] {
  const slides: PptxSlide[] = [];
  // Try "--- Slide N ---" format first (editor round-trip)
  const slideMarker = /---\s*Slide\s+\d+\s*---/;
  // Try "## Heading" format (markdown-like)
  const lines = text.split("\n");

  if (slideMarker.test(text)) {
    // Editor text format
    const blocks = text.split(slideMarker).filter(b => b.trim());
    for (const block of blocks) {
      const bLines = block.trim().split("\n").filter(l => l.trim() && l.trim() !== "(empty slide)");
      const accent = SLIDE_ACCENTS[slides.length % SLIDE_ACCENTS.length];
      const texts: PptxShape["texts"] = bLines.map(line => {
        if (line.startsWith("## ")) return { text: line.slice(3), bold: true, fontSize: 32, color: "#1c1917", align: "center" as const };
        return { text: line.replace(/^[-*]\s+/, ""), fontSize: 18, color: "#57534e", bullet: line.match(/^[-*]\s/) ? "\u2022" : undefined };
      });
      slides.push({
        bg: "#ffffff",
        shapes: [
          // Accent bar at top
          { x: 0, y: 0, w: 100, h: 1.5, fill: accent, texts: [] },
          // Content
          { x: 8, y: 8, w: 84, h: 84, texts },
        ],
      });
    }
  } else {
    // Markdown format: split by ## headings
    let title = "";
    let currentSlide: { title: string; items: string[] } | null = null;
    const slideData: { title: string; items: string[] }[] = [];

    for (const line of lines) {
      if (line.match(/^#\s+/) && !title) {
        title = line.replace(/^#\s+/, "").trim();
        continue;
      }
      if (line.match(/^##\s+/)) {
        if (currentSlide) slideData.push(currentSlide);
        currentSlide = { title: line.replace(/^##\s+/, "").trim(), items: [] };
        continue;
      }
      if (currentSlide && line.trim()) {
        currentSlide.items.push(line.trim());
      } else if (!currentSlide && line.trim() && !line.startsWith("#")) {
        // Content before first ## — add to overview
        if (!slideData.length && !currentSlide) {
          currentSlide = { title: t("page.agent_detail.overview"), items: [] };
        }
        if (currentSlide) currentSlide.items.push(line.trim());
      }
    }
    if (currentSlide) slideData.push(currentSlide);

    // Title slide
    if (title || slideData.length === 0) {
      slides.push({
        bg: "#1c1917",
        shapes: [
          { x: 0, y: 0, w: 100, h: 100, gradFill: { angle: 135, stops: [{ pos: 0, color: "#1c1917", alpha: 1 }, { pos: 100, color: "#1e3a5f", alpha: 1 }] }, texts: [] },
          { x: 10, y: 30, w: 80, h: 40, texts: [
            { text: title || "Presentation", bold: true, fontSize: 44, color: "#ffffff", align: "center" },
          ]},
        ],
      });
    }

    // Content slides
    for (let i = 0; i < slideData.length; i++) {
      const sd = slideData[i];
      const accent = SLIDE_ACCENTS[i % SLIDE_ACCENTS.length];
      const items: PptxShape["texts"] = sd.items.map(item => ({
        text: item.replace(/^[-*•]\s*/, "").replace(/^\d+\.\s*/, ""),
        fontSize: 20,
        color: "#44403c",
        bullet: item.match(/^[-*•]/) ? "\u2022" : item.match(/^\d+\./) ? `${item.match(/^(\d+)\./)?.[1]}.` : undefined,
        indent: (item.match(/^[-*•]/) || item.match(/^\d+\./)) ? 24 : undefined,
      }));
      slides.push({
        bg: "#ffffff",
        shapes: [
          // Accent bar
          { x: 0, y: 0, w: 100, h: 1.2, fill: accent, texts: [] },
          // Title
          { x: 6, y: 5, w: 88, h: 15, texts: [
            { text: sd.title, bold: true, fontSize: 32, color: "#1c1917" },
          ]},
          // Content
          { x: 8, y: 22, w: 84, h: 72, texts: items },
        ],
      });
    }
  }

  // Fallback: if nothing parsed, single slide with all text
  if (slides.length === 0) {
    const allLines = lines.filter(l => l.trim());
    slides.push({
      bg: "#ffffff",
      shapes: [{
        x: 5, y: 5, w: 90, h: 90,
        texts: allLines.map(l => ({
          text: l.replace(/^#+\s*/, ""),
          bold: l.startsWith("#"),
          fontSize: l.startsWith("#") ? 28 : 16,
          color: l.startsWith("#") ? "#1c1917" : "#57534e",
        })),
      }],
    });
  }

  return slides;
}

/** Map preset geometry names to CSS clip-path polygons */
function _presetClipPath(geom?: string): string | undefined {
  switch (geom) {
    case "triangle": return "polygon(50% 0%, 0% 100%, 100% 100%)";
    case "rtTriangle": return "polygon(0% 0%, 0% 100%, 100% 100%)";
    case "diamond": return "polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)";
    case "pentagon": return "polygon(50% 0%, 100% 38%, 82% 100%, 18% 100%, 0% 38%)";
    case "hexagon": return "polygon(25% 0%, 75% 0%, 100% 50%, 75% 100%, 25% 100%, 0% 50%)";
    case "parallelogram": return "polygon(15% 0%, 100% 0%, 85% 100%, 0% 100%)";
    case "trapezoid": return "polygon(20% 0%, 80% 0%, 100% 100%, 0% 100%)";
    case "chevron": return "polygon(0% 0%, 85% 0%, 100% 50%, 85% 100%, 0% 100%, 15% 50%)";
    case "rightArrow": return "polygon(0% 20%, 70% 20%, 70% 0%, 100% 50%, 70% 100%, 70% 80%, 0% 80%)";
    case "leftArrow": return "polygon(30% 0%, 30% 20%, 100% 20%, 100% 80%, 30% 80%, 30% 100%, 0% 50%)";
    case "upArrow": return "polygon(50% 0%, 100% 30%, 80% 30%, 80% 100%, 20% 100%, 20% 30%, 0% 30%)";
    case "downArrow": return "polygon(20% 0%, 80% 0%, 80% 70%, 100% 70%, 50% 100%, 0% 70%, 20% 70%)";
    case "star5": return "polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%)";
    case "ribbon": case "ribbon2": return undefined; // too complex for simple clip-path
    default: return undefined;
  }
}

function PreviewDownloadFallback({ message, onDownload }: { message: string; onDownload: () => void }) {
  return (
    <div style={{
      textAlign: "center", padding: "64px 24px", borderRadius: 12,
      border: "1px solid rgba(28,25,23,0.06)", background: "rgba(250,250,249,0.75)",
    }}>
      <IconDocument size={32} className="text-stone-400" />
      <p style={{ color: "#78716c", margin: "16px 0" }}>{message}</p>
      <Button variant="primary" onClick={onDownload} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <IconDownload size={16} />
        {t("page.file_viewer.download_to_view")}
      </Button>
    </div>
  );
}

function withPreviewTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

type PdfEditorTool = "select" | "text" | "signature" | "highlight" | "whiteout" | "draw";

interface PdfPoint {
  x: number;
  y: number;
}

interface PdfAnnotationBase {
  id: string;
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

interface PdfTextAnnotation extends PdfAnnotationBase {
  kind: "text";
  text: string;
  fontSize: number;
  color: string;
}

interface PdfSignatureAnnotation extends PdfAnnotationBase {
  kind: "signature";
  dataUrl: string;
}

interface PdfHighlightAnnotation extends PdfAnnotationBase {
  kind: "highlight";
  color: string;
  opacity: number;
}

interface PdfWhiteoutAnnotation extends PdfAnnotationBase {
  kind: "whiteout";
}

interface PdfDrawAnnotation extends PdfAnnotationBase {
  kind: "draw";
  points: PdfPoint[];
  color: string;
  strokeWidth: number;
}

type PdfAnnotation = PdfTextAnnotation | PdfSignatureAnnotation | PdfHighlightAnnotation | PdfWhiteoutAnnotation | PdfDrawAnnotation;
type PdfRectPlacementTool = Extract<PdfEditorTool, "text" | "signature" | "highlight" | "whiteout">;

interface PdfPageTextHint {
  page: number;
  items: Array<{
    text: string;
    x: number;
    y: number;
    width: number;
    height: number;
  }>;
}

interface PdfRectDraft {
  page: number;
  kind: PdfRectPlacementTool;
  origin: PdfPoint;
  current: PdfPoint;
}

interface PdfLiveEditBridge {
  getContent: () => string;
  applyContent: NonNullable<EditorLiveChatDetail["applyContent"]>;
  localEditContent: NonNullable<EditorLiveChatDetail["localEditContent"]>;
}

interface ImageLiveEditBridge {
  getContent: () => string;
  applyContent: NonNullable<EditorLiveChatDetail["applyContent"]>;
  localEditContent: NonNullable<EditorLiveChatDetail["localEditContent"]>;
  getAttachmentFiles: NonNullable<EditorLiveChatDetail["getAttachmentFiles"]>;
  applyGeneratedImage: NonNullable<EditorLiveChatDetail["applyGeneratedImage"]>;
  supportsImageGeneration: true;
}

function normalizeImageLiveEditState(raw: unknown) {
  const source = raw && typeof raw === "object" && "edits" in raw
    ? (raw as { edits?: unknown }).edits
    : raw;
  const edits = source && typeof source === "object" ? source as Record<string, unknown> : {};
  const clampPercent = (value: unknown, fallback: number) => {
    const next = Number(value);
    return Number.isFinite(next) ? Math.min(220, Math.max(0, next)) : fallback;
  };
  const normalizedRotation = Number(edits.rotation);
  return {
    rotation: Number.isFinite(normalizedRotation) ? (((normalizedRotation % 360) + 360) % 360) : 0,
    flipX: Boolean(edits.flipX),
    flipY: Boolean(edits.flipY),
    brightness: clampPercent(edits.brightness, 100),
    contrast: clampPercent(edits.contrast, 100),
    saturation: clampPercent(edits.saturation, 100),
    hue: Math.min(180, Math.max(-180, Number(edits.hue) || 0)),
    brushColor: typeof edits.brushColor === "string" && edits.brushColor ? edits.brushColor : "#0f8f84",
    brushSize: Math.min(28, Math.max(1, Number(edits.brushSize) || 6)),
    strokes: Array.isArray(edits.strokes) ? edits.strokes.filter((stroke): stroke is ImageStroke => {
      if (!stroke || typeof stroke !== "object") return false;
      const item = stroke as ImageStroke;
      return typeof item.color === "string" && Number.isFinite(item.size) && Array.isArray(item.points);
    }) : [] as ImageStroke[],
  };
}

const PDF_EDITOR_COPY = {
  select: t("page.file_viewer.pdf_editor.select"),
  addText: t("page.file_viewer.pdf_editor.add_text"),
  eSign: t("page.file_viewer.pdf_editor.e_sign"),
  highlight: t("page.file_viewer.pdf_editor.highlight"),
  whiteout: t("page.file_viewer.pdf_editor.whiteout"),
  draw: t("page.file_viewer.pdf_editor.draw"),
  undo: t("page.file_viewer.pdf_editor.undo"),
  redo: t("page.file_viewer.pdf_editor.redo"),
  drawSignature: t("page.file_viewer.pdf_editor.draw_signature"),
  save: t("page.file_viewer.pdf_editor.save_pdf"),
  download: t("page.file_viewer.pdf_editor.download_edited"),
  clear: t("page.file_viewer.pdf_editor.clear_edits"),
  delete: t("page.file_viewer.pdf_editor.delete_selected"),
  clickToPlaceText: t("page.file_viewer.pdf_editor.click_to_place_text"),
  clickToPlaceSignature: t("page.file_viewer.pdf_editor.click_to_place_signature"),
  clickToPlaceHighlight: t("page.file_viewer.pdf_editor.click_to_place_highlight"),
  clickToPlaceWhiteout: t("page.file_viewer.pdf_editor.click_to_place_whiteout"),
  dragToDraw: t("page.file_viewer.pdf_editor.drag_to_draw"),
  noEdits: t("page.file_viewer.pdf_editor.no_edits"),
  saved: t("page.file_viewer.pdf_editor.saved"),
  exported: t("page.file_viewer.pdf_editor.exported"),
  saveFailed: t("page.file_viewer.pdf_editor.save_failed"),
  exportFailed: t("page.file_viewer.pdf_editor.export_failed"),
  signatureTitle: t("page.file_viewer.pdf_editor.signature_title"),
  useSignature: t("page.file_viewer.pdf_editor.use_signature"),
  clearSignature: t("page.file_viewer.pdf_editor.clear_signature"),
  signatureSaved: t("page.file_viewer.pdf_editor.signature_saved"),
  savedSignature: t("page.file_viewer.pdf_editor.saved_signature"),
  redrawSignature: t("page.file_viewer.pdf_editor.redraw_signature"),
  forgetSignature: t("page.file_viewer.pdf_editor.forget_signature"),
};

const PDF_TEXT_COLORS = ["#1c1917", "#c14a44", "#436b65", "#4869ac"];
const PDF_HIGHLIGHT_COLORS = ["#fde047", "#86efac", "#93c5fd", "#f9a8d4"];
const PDF_DRAW_COLORS = ["#1c1917", "#c14a44", "#436b65", "#4869ac"];
const PDF_DEFAULT_ZOOM = 140;
const PDF_SIGNATURE_STORAGE_KEY = "manor.pdfEditor.signature";

function editorToolButtonClass(options: { active?: boolean; icon?: boolean; primary?: boolean; danger?: boolean } = {}) {
  return [
    "manor-editor-tool-button",
    options.icon ? "manor-editor-icon-button" : "",
    options.active ? "manor-editor-tool-button--active" : "",
    options.primary ? "manor-editor-tool-button--primary" : "",
    options.danger ? "manor-editor-tool-button--danger" : "",
  ].filter(Boolean).join(" ");
}

function editorSwatchClass(active: boolean) {
  return `manor-editor-swatch${active ? " manor-editor-swatch--active" : ""}`;
}

function createPdfAnnotationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `pdf-ann-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function clampRatio(value: number, max = 1): number {
  return Math.min(max, Math.max(0, value));
}

function isPdfRectPlacementTool(tool: PdfEditorTool): tool is PdfRectPlacementTool {
  return tool === "text" || tool === "signature" || tool === "highlight" || tool === "whiteout";
}

function normalizePdfRect(origin: PdfPoint, current: PdfPoint) {
  const x = Math.min(origin.x, current.x);
  const y = Math.min(origin.y, current.y);
  return {
    x,
    y,
    width: Math.max(0, Math.max(origin.x, current.x) - x),
    height: Math.max(0, Math.max(origin.y, current.y) - y),
  };
}

function clampPdfAnnotationGeometry<T extends PdfAnnotation>(annotation: T): T {
  const width = Math.max(0.001, Math.min(1, annotation.width));
  const height = Math.max(0.001, Math.min(1, annotation.height));
  const x = clampRatio(annotation.x, Math.max(0, 1 - width));
  const y = clampRatio(annotation.y, Math.max(0, 1 - height));
  return {
    ...annotation,
    x,
    y,
    width: Math.min(width, 1 - x),
    height: Math.min(height, 1 - y),
  };
}

function getPdfAnnotationMinRatio(kind: PdfAnnotation["kind"], pageWidth: number, pageHeight: number) {
  const safeWidth = Math.max(1, pageWidth);
  const safeHeight = Math.max(1, pageHeight);
  const minPixels = kind === "signature"
    ? { width: 72, height: 28 }
    : kind === "text"
      ? { width: 54, height: 26 }
      : kind === "highlight"
        ? { width: 12, height: 6 }
        : { width: 18, height: 14 };
  return {
    width: Math.min(0.92, minPixels.width / safeWidth),
    height: Math.min(0.92, minPixels.height / safeHeight),
  };
}

function roundPdfRatio(value: number): number {
  return Number(clampRatio(value).toFixed(4));
}

function isValidPdfHexColor(value: unknown): value is string {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}

function coercePdfNumber(value: unknown, fallback: number): number {
  const next = typeof value === "number" ? value : Number(value);
  return Number.isFinite(next) ? next : fallback;
}

function parsePdfLiveEditJson(content: string): any {
  const trimmed = content.trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .trim();
  try {
    return JSON.parse(trimmed);
  } catch {
    const objectStart = trimmed.indexOf("{");
    const objectEnd = trimmed.lastIndexOf("}");
    if (objectStart >= 0 && objectEnd > objectStart) {
      return JSON.parse(trimmed.slice(objectStart, objectEnd + 1));
    }
    const arrayStart = trimmed.indexOf("[");
    const arrayEnd = trimmed.lastIndexOf("]");
    if (arrayStart >= 0 && arrayEnd > arrayStart) {
      return JSON.parse(trimmed.slice(arrayStart, arrayEnd + 1));
    }
    throw new Error("Invalid PDF edit JSON");
  }
}

function serializePdfAnnotationForLiveEdit(annotation: PdfAnnotation) {
  const base = {
    id: annotation.id,
    kind: annotation.kind,
    page: annotation.page,
    x: roundPdfRatio(annotation.x),
    y: roundPdfRatio(annotation.y),
    width: roundPdfRatio(annotation.width),
    height: roundPdfRatio(annotation.height),
  };
  if (annotation.kind === "text") {
    return {
      ...base,
      text: annotation.text,
      fontSize: annotation.fontSize,
      color: annotation.color,
    };
  }
  if (annotation.kind === "highlight") {
    return {
      ...base,
      color: annotation.color,
      opacity: annotation.opacity,
    };
  }
  if (annotation.kind === "signature") {
    return {
      ...base,
      hasSignatureImage: true,
    };
  }
  if (annotation.kind === "draw") {
    return {
      ...base,
      points: annotation.points.map((point) => ({
        x: roundPdfRatio(point.x),
        y: roundPdfRatio(point.y),
      })),
      color: annotation.color,
      strokeWidth: annotation.strokeWidth,
    };
  }
  return base;
}

function parsePdfLiveEditAnnotations(
  content: string,
  pageCount: number,
  currentAnnotations: PdfAnnotation[],
  signatureDataUrl: string | null,
): PdfAnnotation[] {
  const parsed = parsePdfLiveEditJson(content);
  const rawAnnotations = Array.isArray(parsed)
    ? parsed
    : parsed?.annotations || parsed?.editableAnnotations || parsed?.pdfAnnotations;
  if (!Array.isArray(rawAnnotations)) throw new Error("PDF edit JSON must include an annotations array");

  const currentById = new Map(currentAnnotations.map((annotation) => [annotation.id, annotation]));
  const safePageCount = Math.max(1, pageCount || 1);

  return rawAnnotations.flatMap((raw: any, index: number): PdfAnnotation[] => {
    const kind = String(raw?.kind || raw?.type || "text").toLowerCase();
    const existing = typeof raw?.id === "string" ? currentById.get(raw.id) : undefined;
    const id = typeof raw?.id === "string" && raw.id.trim() ? raw.id : createPdfAnnotationId();
    const page = Math.min(safePageCount, Math.max(1, Math.round(coercePdfNumber(raw?.page, existing?.page || 1))));
    const widthFallback = existing?.width || (kind === "highlight" ? 0.28 : kind === "whiteout" ? 0.22 : 0.34);
    const heightFallback = existing?.height || (kind === "highlight" ? 0.035 : kind === "whiteout" ? 0.055 : 0.065);
    const width = Math.min(0.95, Math.max(0.006, coercePdfNumber(raw?.width, widthFallback)));
    const height = Math.min(0.95, Math.max(0.006, coercePdfNumber(raw?.height, heightFallback)));
    const x = clampRatio(coercePdfNumber(raw?.x, existing?.x ?? 0.08), Math.max(0, 1 - width));
    const yFallback = existing?.y ?? Math.min(0.86, 0.08 + index * 0.075);
    const y = clampRatio(coercePdfNumber(raw?.y, yFallback), Math.max(0, 1 - height));
    const base = { id, page, x, y, width, height };

    if (kind === "highlight") {
      return [clampPdfAnnotationGeometry({
        ...base,
        kind: "highlight",
        color: isValidPdfHexColor(raw?.color) ? raw.color : existing?.kind === "highlight" ? existing.color : "#fde047",
        opacity: Math.min(0.85, Math.max(0.12, coercePdfNumber(raw?.opacity, existing?.kind === "highlight" ? existing.opacity : 0.45))),
      })];
    }

    if (kind === "whiteout" || kind === "white-out" || kind === "erase") {
      return [clampPdfAnnotationGeometry({ ...base, kind: "whiteout" })];
    }

    if (kind === "draw" || kind === "line") {
      const rawPoints = Array.isArray(raw?.points) ? raw.points : existing?.kind === "draw" ? existing.points : [];
      const points = rawPoints
        .map((point: any) => ({
          x: clampRatio(coercePdfNumber(point?.x, 0)),
          y: clampRatio(coercePdfNumber(point?.y, 0)),
        }))
        .filter((point: PdfPoint) => Number.isFinite(point.x) && Number.isFinite(point.y));
      if (points.length < 2) return [];
      return [clampPdfAnnotationGeometry({
        ...base,
        kind: "draw",
        points,
        color: isValidPdfHexColor(raw?.color) ? raw.color : existing?.kind === "draw" ? existing.color : "#1c1917",
        strokeWidth: Math.min(10, Math.max(1, coercePdfNumber(raw?.strokeWidth, existing?.kind === "draw" ? existing.strokeWidth : 2))),
      })];
    }

    if (kind === "signature") {
      const dataUrl = typeof raw?.dataUrl === "string" && raw.dataUrl.startsWith("data:")
        ? raw.dataUrl
        : existing?.kind === "signature"
          ? existing.dataUrl
          : signatureDataUrl;
      if (!dataUrl) return [];
      return [clampPdfAnnotationGeometry({ ...base, kind: "signature", dataUrl })];
    }

    return [clampPdfAnnotationGeometry({
      ...base,
      kind: "text",
      text: typeof raw?.text === "string" ? raw.text : existing?.kind === "text" ? existing.text : "",
      fontSize: Math.min(48, Math.max(8, coercePdfNumber(raw?.fontSize, existing?.kind === "text" ? existing.fontSize : 14))),
      color: isValidPdfHexColor(raw?.color) ? raw.color : existing?.kind === "text" ? existing.color : "#1c1917",
    })];
  });
}

function ensurePdfName(name: string | undefined, suffix = ""): string {
  const fallback = "document.pdf";
  const safeName = (name || fallback).trim() || fallback;
  if (/\.pdf$/i.test(safeName)) {
    return suffix ? safeName.replace(/\.pdf$/i, `${suffix}.pdf`) : safeName;
  }
  return `${safeName}${suffix}.pdf`;
}

function parseHexColor(hex: string): [number, number, number] {
  const normalized = hex.replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(normalized)) return [0, 0, 0];
  return [
    parseInt(normalized.slice(0, 2), 16) / 255,
    parseInt(normalized.slice(2, 4), 16) / 255,
    parseInt(normalized.slice(4, 6), 16) / 255,
  ];
}

function hexToRgba(hex: string, alpha: number): string {
  const [r, g, b] = parseHexColor(hex).map((value) => Math.round(value * 255));
  return `rgba(${r}, ${g}, ${b}, ${Math.min(1, Math.max(0, alpha))})`;
}

function getSavedPdfSignature(): string | null {
  try {
    return window.localStorage.getItem(PDF_SIGNATURE_STORAGE_KEY);
  } catch {
    return null;
  }
}

function savePdfSignature(dataUrl: string) {
  try {
    window.localStorage.setItem(PDF_SIGNATURE_STORAGE_KEY, dataUrl);
  } catch {
    // localStorage can be unavailable in private or restricted browser contexts.
  }
}

function removeSavedPdfSignature() {
  try {
    window.localStorage.removeItem(PDF_SIGNATURE_STORAGE_KEY);
  } catch {
    // localStorage can be unavailable in private or restricted browser contexts.
  }
}

async function dataUrlToUint8Array(dataUrl: string): Promise<Uint8Array> {
  const response = await fetch(dataUrl);
  const buffer = await response.arrayBuffer();
  return new Uint8Array(buffer);
}

function wrapPdfText(font: any, text: string, fontSize: number, maxWidth: number): string[] {
  const paragraphs = (text || "").split(/\r?\n/);
  const lines: string[] = [];
  for (const paragraph of paragraphs) {
    const words = paragraph.split(/\s+/).filter(Boolean);
    if (words.length === 0) {
      lines.push("");
      continue;
    }
    let line = "";
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (font.widthOfTextAtSize(candidate, fontSize) <= maxWidth || !line) {
        line = candidate;
      } else {
        lines.push(line);
        line = word;
      }
    }
    if (line) lines.push(line);
  }
  return lines.length ? lines : [""];
}

function PdfSignatureDialog({
  onClose,
  onSave,
}: {
  onClose: () => void;
  onSave: (dataUrl: string) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawingRef = useRef(false);
  const lastPointRef = useRef<{ x: number; y: number } | null>(null);
  const [hasInk, setHasInk] = useState(false);

  const prepareCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const width = 520;
    const height = 180;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = "100%";
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#1c1917";
  }, []);

  useEffect(() => {
    prepareCanvas();
  }, [prepareCanvas]);

  const getPoint = useCallback((event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / rect.width) * 520,
      y: ((event.clientY - rect.top) / rect.height) * 180,
    };
  }, []);

  const handlePointerDown = useCallback((event: React.PointerEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drawingRef.current = true;
    lastPointRef.current = getPoint(event);
  }, [getPoint]);

  const handlePointerMove = useCallback((event: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawingRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    const last = lastPointRef.current;
    if (!ctx || !last) return;
    const point = getPoint(event);
    ctx.beginPath();
    ctx.moveTo(last.x, last.y);
    ctx.lineTo(point.x, point.y);
    ctx.stroke();
    lastPointRef.current = point;
    if (!hasInk) setHasInk(true);
  }, [getPoint, hasInk]);

  const stopDrawing = useCallback(() => {
    drawingRef.current = false;
    lastPointRef.current = null;
  }, []);

  const clearSignature = useCallback(() => {
    prepareCanvas();
    setHasInk(false);
  }, [prepareCanvas]);

  const saveSignature = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !hasInk) return;
    onSave(canvas.toDataURL("image/png"));
  }, [hasInk, onSave]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
        background: "rgba(28,25,23,0.42)",
      }}
      onClick={onClose}
    >
      <div
        className="glass-panel"
        style={{ width: "min(560px, 100%)", padding: 18, background: "rgba(255,255,255,0.96)" }}
        onClick={(event) => event.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 15, fontWeight: 700, color: "#292524" }}>
            <IconSignature size={18} />
            {PDF_EDITOR_COPY.signatureTitle}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("action.close")}
            style={{ width: 32, height: 32, borderRadius: 8, border: "none", background: "transparent", color: "#78716c", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}
          >
            <IconClose size={17} />
          </button>
        </div>
        <canvas
          ref={canvasRef}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={stopDrawing}
          onPointerCancel={stopDrawing}
          style={{
            display: "block",
            borderRadius: 10,
            border: "1px solid rgba(28,25,23,0.06)",
            background: "white",
            touchAction: "none",
          }}
        />
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          <button type="button" className="btn-manor-outline" onClick={clearSignature} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, padding: "7px 12px" }}>
            <IconTrash size={15} />
            {PDF_EDITOR_COPY.clearSignature}
          </button>
          <button type="button" className="btn-manor" onClick={saveSignature} disabled={!hasInk} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, padding: "7px 12px", opacity: hasInk ? 1 : 0.55 }}>
            <IconCheck size={15} />
            {PDF_EDITOR_COPY.useSignature}
          </button>
        </div>
      </div>
    </div>
  );
}

function PdfJsViewer({
  url,
  docId,
  docName,
  onDownload,
  onSaved,
  onLiveEditBridgeChange,
  canEdit = true,
}: {
  url: string;
  docId?: string;
  docName?: string;
  onDownload: () => void;
  onSaved?: (savedDoc: Document, savedBlob: Blob) => void | Promise<void>;
  onLiveEditBridgeChange?: (bridge: PdfLiveEditBridge | null) => void;
  canEdit?: boolean;
}) {
  const [pdfDoc, setPdfDoc] = useState<any>(null);
  const [pageCount, setPageCount] = useState(0);
  const [pageTextHints, setPageTextHints] = useState<PdfPageTextHint[]>([]);
  const [zoom, setZoom] = useState(PDF_DEFAULT_ZOOM);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState("");
  const [annotations, setAnnotationsRaw] = useState<PdfAnnotation[]>([]);
  const [annotationPast, setAnnotationPast] = useState<PdfAnnotation[][]>([]);
  const [annotationFuture, setAnnotationFuture] = useState<PdfAnnotation[][]>([]);
  const [activeTool, setActiveTool] = useState<PdfEditorTool>("select");
  const [selectedAnnotationId, setSelectedAnnotationId] = useState<string | null>(null);
  const [signatureDataUrl, setSignatureDataUrl] = useState<string | null>(null);
  const [showSignatureDialog, setShowSignatureDialog] = useState(false);
  const [exporting, setExporting] = useState<"download" | "save" | null>(null);
  const [editorMessage, setEditorMessage] = useState("");
  const [drawingDraft, setDrawingDraft] = useState<{ page: number; points: PdfPoint[]; color: string; strokeWidth: number } | null>(null);
    const [rectDraft, setRectDraft] = useState<PdfRectDraft | null>(null);
    const annotationsRef = useRef<PdfAnnotation[]>([]);
    const drawingDraftRef = useRef<{ page: number; points: PdfPoint[]; color: string; strokeWidth: number } | null>(null);
    const rectDraftRef = useRef<PdfRectDraft | null>(null);
    const textAreaRefs = useRef<Map<string, HTMLTextAreaElement>>(new Map());
    const annotationEditHistoryRef = useRef<Set<string>>(new Set());
    const canvasesRef = useRef<Map<number, HTMLCanvasElement>>(new Map());
  const dragRef = useRef<{
    mode: "move" | "resize";
    id: string;
    startX: number;
    startY: number;
    startAnnX: number;
    startAnnY: number;
    startAnnWidth: number;
    startAnnHeight: number;
    pageWidth: number;
    pageHeight: number;
  } | null>(null);

    const selectedAnnotation = annotations.find((annotation) => annotation.id === selectedAnnotationId) || null;
    const hasAnnotations = annotations.length > 0;
    const busy = exporting !== null;

  useEffect(() => {
    const savedSignature = getSavedPdfSignature();
    if (savedSignature) setSignatureDataUrl(savedSignature);
  }, []);

  useEffect(() => {
    if (selectedAnnotation?.kind !== "text") return;
    const textArea = textAreaRefs.current.get(selectedAnnotation.id);
    if (!textArea) return;
    window.requestAnimationFrame(() => {
      textArea.focus();
      if (!selectedAnnotation.text) return;
      textArea.select();
    });
  }, [selectedAnnotation?.id, selectedAnnotation?.kind]);

  const setAnnotations = useCallback((updater: PdfAnnotation[] | ((current: PdfAnnotation[]) => PdfAnnotation[])) => {
    setAnnotationsRaw((current) => {
      const next = typeof updater === "function" ? (updater as (value: PdfAnnotation[]) => PdfAnnotation[])(current) : updater;
      annotationsRef.current = next;
      return next;
    });
  }, []);

  const pushAnnotationHistory = useCallback((snapshot = annotationsRef.current) => {
    setAnnotationPast((past) => [...past.slice(-24), snapshot]);
    setAnnotationFuture([]);
  }, []);

  const commitAnnotations = useCallback((updater: PdfAnnotation[] | ((current: PdfAnnotation[]) => PdfAnnotation[])) => {
    const current = annotationsRef.current;
    const next = typeof updater === "function" ? (updater as (value: PdfAnnotation[]) => PdfAnnotation[])(current) : updater;
    if (next === current) return;
    setAnnotationPast((past) => [...past.slice(-24), current]);
    setAnnotationFuture([]);
    annotationsRef.current = next;
    setAnnotationsRaw(next);
  }, []);

  const undoAnnotations = useCallback(() => {
    setAnnotationPast((past) => {
      const previous = past[past.length - 1];
      if (!previous) return past;
      const current = annotationsRef.current;
      setAnnotationFuture((future) => [current, ...future].slice(0, 25));
      annotationsRef.current = previous;
      setAnnotationsRaw(previous);
      setSelectedAnnotationId(null);
      return past.slice(0, -1);
    });
  }, []);

  const redoAnnotations = useCallback(() => {
    setAnnotationFuture((future) => {
      const next = future[0];
      if (!next) return future;
      const current = annotationsRef.current;
      setAnnotationPast((past) => [...past.slice(-24), current]);
      annotationsRef.current = next;
      setAnnotationsRaw(next);
      setSelectedAnnotationId(null);
      return future.slice(1);
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: any = null;

    setLoading(true);
    setError("");
    setPdfDoc(null);
    setPageCount(0);
    setPageTextHints([]);

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        if (cancelled) return;
        pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = pdfjs.getDocument({
          url,
          cMapUrl: `${import.meta.env.BASE_URL}pdfjs/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${import.meta.env.BASE_URL}pdfjs/standard_fonts/`,
          disableFontFace: false,
          useSystemFonts: true,
        });
        const loadedPdf = await loadingTask.promise;
        if (cancelled) {
          loadedPdf.destroy();
          return;
        }
        setPdfDoc(loadedPdf);
        setPageCount(loadedPdf.numPages);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || t("page.file_viewer.pdf_preview_requires_downloading_the_file"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      loadingTask?.destroy?.();
    };
  }, [url]);

  useEffect(() => {
    annotationsRef.current = [];
    setAnnotationsRaw([]);
    setAnnotationPast([]);
    setAnnotationFuture([]);
    setSelectedAnnotationId(null);
    setActiveTool("select");
    setEditorMessage("");
    setPageTextHints([]);
    setDrawingDraft(null);
    drawingDraftRef.current = null;
    setRectDraft(null);
    rectDraftRef.current = null;
    setZoom(PDF_DEFAULT_ZOOM);
  }, [url]);

  useEffect(() => {
    if (!pdfDoc || pageCount === 0) return;
    let cancelled = false;
    const renderTasks: any[] = [];

    (async () => {
      setRendering(true);
      try {
        for (let pageNumber = 1; pageNumber <= pageCount; pageNumber++) {
          if (cancelled) break;
          const page = await pdfDoc.getPage(pageNumber);
          const canvas = canvasesRef.current.get(pageNumber);
          if (!canvas) continue;

          const viewport = page.getViewport({ scale: Math.max(0.25, zoom / 100) });
          const outputScale = window.devicePixelRatio || 1;
          canvas.width = Math.floor(viewport.width * outputScale);
          canvas.height = Math.floor(viewport.height * outputScale);
          canvas.style.width = `${viewport.width}px`;
          canvas.style.height = `${viewport.height}px`;

          const context = canvas.getContext("2d");
          if (!context) continue;
          context.setTransform(outputScale, 0, 0, outputScale, 0, 0);
          const task = page.render({ canvasContext: context, viewport });
          renderTasks.push(task);
          await task.promise;
          page.cleanup();
        }
      } catch (e: any) {
        if (!cancelled && e?.name !== "RenderingCancelledException") {
          setError(e?.message || t("page.file_viewer.pdf_preview_requires_downloading_the_file"));
        }
      } finally {
        if (!cancelled) setRendering(false);
      }
    })();

    return () => {
      cancelled = true;
      renderTasks.forEach((task) => task.cancel?.());
    };
  }, [pageCount, pdfDoc, zoom]);

  useEffect(() => {
    if (!pdfDoc || pageCount === 0) return;
    let cancelled = false;

    (async () => {
      const hints: PdfPageTextHint[] = [];
      for (let pageNumber = 1; pageNumber <= pageCount; pageNumber++) {
        if (cancelled) break;
        try {
          const page = await pdfDoc.getPage(pageNumber);
          const viewport = page.getViewport({ scale: 1 });
          const textContent = await page.getTextContent();
          const items = (textContent.items || [])
            .map((item: any) => {
              const text = String(item.str || "").replace(/\s+/g, " ").trim();
              if (!text) return null;
              const transform = Array.isArray(item.transform) ? item.transform : [];
              const x = coercePdfNumber(transform[4], 0);
              const y = coercePdfNumber(transform[5], 0);
              const height = Math.max(4, Math.abs(coercePdfNumber(transform[3], item.height || 10)));
              const width = Math.max(2, coercePdfNumber(item.width, text.length * height * 0.45));
              return {
                text,
                x: roundPdfRatio(x / Math.max(1, viewport.width)),
                y: roundPdfRatio((viewport.height - y - height) / Math.max(1, viewport.height)),
                width: roundPdfRatio(width / Math.max(1, viewport.width)),
                height: roundPdfRatio(height / Math.max(1, viewport.height)),
              };
            })
            .filter(Boolean)
            .slice(0, 120);
          hints.push({ page: pageNumber, items });
          page.cleanup();
        } catch {
          hints.push({ page: pageNumber, items: [] });
        }
      }
      if (!cancelled) setPageTextHints(hints);
    })();

    return () => {
      cancelled = true;
    };
  }, [pageCount, pdfDoc]);

  useEffect(() => () => {
    pdfDoc?.destroy?.();
  }, [pdfDoc]);

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = (event.clientX - drag.startX) / drag.pageWidth;
      const dy = (event.clientY - drag.startY) / drag.pageHeight;
      setAnnotations((current) => current.map((annotation) => {
        if (annotation.id !== drag.id) return annotation;
        if (drag.mode === "resize") {
          const minSize = getPdfAnnotationMinRatio(annotation.kind, drag.pageWidth, drag.pageHeight);
          return {
            ...annotation,
            width: Math.min(Math.max(minSize.width, drag.startAnnWidth + dx), Math.max(minSize.width, 1 - annotation.x)),
            height: Math.min(Math.max(minSize.height, drag.startAnnHeight + dy), Math.max(minSize.height, 1 - annotation.y)),
          };
        }
        return {
          ...annotation,
          x: clampRatio(drag.startAnnX + dx, Math.max(0, 1 - annotation.width)),
          y: clampRatio(drag.startAnnY + dy, Math.max(0, 1 - annotation.height)),
        };
      }));
    };
    const handlePointerUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };
  }, [setAnnotations]);

    const updateAnnotation = useCallback((id: string, patch: Partial<PdfAnnotation>) => {
      setAnnotations((current) => current.map((annotation) => (
        annotation.id === id ? clampPdfAnnotationGeometry({ ...annotation, ...patch } as PdfAnnotation) : annotation
      )));
    }, []);

    const commitAnnotationPatch = useCallback((id: string, patch: Partial<PdfAnnotation>) => {
      commitAnnotations((current) => current.map((annotation) => (
        annotation.id === id ? clampPdfAnnotationGeometry({ ...annotation, ...patch } as PdfAnnotation) : annotation
      )));
    }, [commitAnnotations]);

    const beginAnnotationEdit = useCallback((id: string) => {
      if (annotationEditHistoryRef.current.has(id)) return;
      annotationEditHistoryRef.current.add(id);
      pushAnnotationHistory();
    }, [pushAnnotationHistory]);

    const endAnnotationEdit = useCallback((id: string) => {
      annotationEditHistoryRef.current.delete(id);
    }, []);

	  const deleteSelectedAnnotation = useCallback(() => {
	    if (!selectedAnnotationId) return;
	    commitAnnotations((current) => current.filter((annotation) => annotation.id !== selectedAnnotationId));
	    setSelectedAnnotationId(null);
	  }, [commitAnnotations, selectedAnnotationId]);

	  const duplicateSelectedAnnotation = useCallback(() => {
	    if (!selectedAnnotation) return;
	    const clone = clampPdfAnnotationGeometry({
	      ...selectedAnnotation,
	      id: createPdfAnnotationId(),
	      x: selectedAnnotation.x + 0.02,
	      y: selectedAnnotation.y + 0.02,
	      ...(selectedAnnotation.kind === "draw" ? { points: selectedAnnotation.points.map((point) => ({ ...point })) } : {}),
	    } as PdfAnnotation);
	    commitAnnotations((current) => [...current, clone]);
	    setSelectedAnnotationId(clone.id);
	  }, [commitAnnotations, selectedAnnotation]);

    useEffect(() => {
      const handler = (event: KeyboardEvent) => {
        if (!canEdit) return;
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (target?.closest("input, textarea, select, [contenteditable='true']")) return;

        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
          event.preventDefault();
          if (event.shiftKey) redoAnnotations();
          else undoAnnotations();
          return;
        }

        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "y") {
          event.preventDefault();
          redoAnnotations();
          return;
        }

	      if (!selectedAnnotationId) {
	        if (event.key === "Escape") setActiveTool("select");
	        return;
	      }

	      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "d") {
	        event.preventDefault();
	        duplicateSelectedAnnotation();
	        return;
	      }

	      if (event.key === "Escape") {
	        event.preventDefault();
	        setSelectedAnnotationId(null);
	        setActiveTool("select");
	        return;
	      }

	      if (event.key === "Delete" || event.key === "Backspace") {
	        event.preventDefault();
	        deleteSelectedAnnotation();
	        return;
	      }

	      const step = event.shiftKey ? 0.01 : 0.0025;
	      const movement: Record<string, { dx: number; dy: number }> = {
	        ArrowLeft: { dx: -step, dy: 0 },
	        ArrowRight: { dx: step, dy: 0 },
	        ArrowUp: { dx: 0, dy: -step },
	        ArrowDown: { dx: 0, dy: step },
	      };
	      const move = movement[event.key];
	      if (!move) return;
	      event.preventDefault();
	      commitAnnotations((current) => current.map((annotation) => {
	        if (annotation.id !== selectedAnnotationId) return annotation;
	        return {
	          ...annotation,
	          x: clampRatio(annotation.x + move.dx, Math.max(0, 1 - annotation.width)),
	          y: clampRatio(annotation.y + move.dy, Math.max(0, 1 - annotation.height)),
	        } as PdfAnnotation;
	      }));
      };
      window.addEventListener("keydown", handler);
      return () => window.removeEventListener("keydown", handler);
	  }, [canEdit, commitAnnotations, deleteSelectedAnnotation, duplicateSelectedAnnotation, redoAnnotations, selectedAnnotationId, undoAnnotations]);

  const handlePageClick = useCallback(() => {
    if (activeTool === "select") {
      setSelectedAnnotationId(null);
    }
  }, [activeTool]);

  const startAnnotationDrag = useCallback((event: React.PointerEvent<HTMLDivElement>, annotation: PdfAnnotation) => {
    if (!canEdit) return;
    const target = event.target as HTMLElement;
    if (["TEXTAREA", "INPUT", "BUTTON"].includes(target.tagName)) {
      event.stopPropagation();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const pageEl = (event.currentTarget as HTMLElement).closest("[data-pdf-page]") as HTMLElement | null;
    if (!pageEl) return;
    const rect = pageEl.getBoundingClientRect();
    pushAnnotationHistory();
    dragRef.current = {
      mode: "move",
      id: annotation.id,
      startX: event.clientX,
      startY: event.clientY,
      startAnnX: annotation.x,
      startAnnY: annotation.y,
      startAnnWidth: annotation.width,
      startAnnHeight: annotation.height,
      pageWidth: rect.width,
      pageHeight: rect.height,
    };
    setSelectedAnnotationId(annotation.id);
  }, [canEdit, pushAnnotationHistory]);

  const startAnnotationResize = useCallback((event: React.PointerEvent<HTMLDivElement>, annotation: PdfAnnotation) => {
    if (!canEdit) return;
    event.preventDefault();
    event.stopPropagation();
    const pageEl = (event.currentTarget as HTMLElement).closest("[data-pdf-page]") as HTMLElement | null;
    if (!pageEl) return;
    const rect = pageEl.getBoundingClientRect();
    pushAnnotationHistory();
    dragRef.current = {
      mode: "resize",
      id: annotation.id,
      startX: event.clientX,
      startY: event.clientY,
      startAnnX: annotation.x,
      startAnnY: annotation.y,
      startAnnWidth: annotation.width,
      startAnnHeight: annotation.height,
      pageWidth: rect.width,
      pageHeight: rect.height,
    };
    setSelectedAnnotationId(annotation.id);
  }, [canEdit, pushAnnotationHistory]);

  const getPagePoint = useCallback((event: React.PointerEvent<HTMLDivElement>): PdfPoint => {
    const rect = event.currentTarget.getBoundingClientRect();
    return {
      x: clampRatio((event.clientX - rect.left) / rect.width),
      y: clampRatio((event.clientY - rect.top) / rect.height),
    };
  }, []);

  const handlePagePointerDown = useCallback((pageNumber: number, event: React.PointerEvent<HTMLDivElement>) => {
    if (!canEdit) return;
    if (activeTool === "signature" && !signatureDataUrl) {
      event.preventDefault();
      setShowSignatureDialog(true);
      return;
    }
    if (isPdfRectPlacementTool(activeTool)) {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      const point = getPagePoint(event);
      const draft = { page: pageNumber, kind: activeTool, origin: point, current: point };
      rectDraftRef.current = draft;
      setRectDraft(draft);
      setSelectedAnnotationId(null);
      return;
    }
    if (activeTool !== "draw") return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const point = getPagePoint(event);
    const draft = { page: pageNumber, points: [point], color: "#c14a44", strokeWidth: 2 };
    drawingDraftRef.current = draft;
    setDrawingDraft(draft);
    setSelectedAnnotationId(null);
  }, [activeTool, canEdit, getPagePoint, signatureDataUrl]);

  const handlePagePointerMove = useCallback((pageNumber: number, event: React.PointerEvent<HTMLDivElement>) => {
    if (!canEdit) return;
    const placement = rectDraftRef.current;
    if (isPdfRectPlacementTool(activeTool) && placement && placement.page === pageNumber) {
      const next = { ...placement, current: getPagePoint(event) };
      rectDraftRef.current = next;
      setRectDraft(next);
      return;
    }
    const draft = drawingDraftRef.current;
    if (activeTool !== "draw" || !draft || draft.page !== pageNumber) return;
    const point = getPagePoint(event);
    const last = draft.points[draft.points.length - 1];
    if (last && Math.hypot(point.x - last.x, point.y - last.y) < 0.0025) return;
    const next = { ...draft, points: [...draft.points, point] };
    drawingDraftRef.current = next;
    setDrawingDraft(next);
  }, [activeTool, canEdit, getPagePoint]);

  const handlePagePointerUp = useCallback((pageNumber: number, event?: React.PointerEvent<HTMLDivElement>) => {
    if (!canEdit) return;
    const placement = rectDraftRef.current;
    if (isPdfRectPlacementTool(activeTool) && placement && placement.page === pageNumber) {
      if (event) {
        try {
          event.currentTarget.releasePointerCapture(event.pointerId);
        } catch {
          // The browser may have already released capture after cancellation.
        }
      }
      rectDraftRef.current = null;
      setRectDraft(null);

      const finalPoint = event ? getPagePoint(event) : placement.current;
      const rect = normalizePdfRect(placement.origin, finalPoint);
      const pageRect = event?.currentTarget.getBoundingClientRect();
      const minSize = getPdfAnnotationMinRatio(placement.kind, pageRect?.width || 1, pageRect?.height || 1);
      if (rect.width < minSize.width || rect.height < minSize.height) {
        setEditorMessage(placement.kind === "text"
          ? PDF_EDITOR_COPY.clickToPlaceText
          : placement.kind === "signature"
            ? PDF_EDITOR_COPY.clickToPlaceSignature
          : placement.kind === "highlight"
            ? PDF_EDITOR_COPY.clickToPlaceHighlight
            : PDF_EDITOR_COPY.clickToPlaceWhiteout);
        return;
      }

      const id = createPdfAnnotationId();
      if (placement.kind === "text") {
        const annotation: PdfTextAnnotation = {
          id,
          kind: "text",
          page: pageNumber,
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          text: "",
          fontSize: 14,
          color: "#1c1917",
        };
        commitAnnotations((current) => [...current, annotation]);
        setActiveTool("select");
      } else if (placement.kind === "signature") {
        if (!signatureDataUrl) return;
        const annotation: PdfSignatureAnnotation = {
          id,
          kind: "signature",
          page: pageNumber,
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          dataUrl: signatureDataUrl,
        };
        commitAnnotations((current) => [...current, annotation]);
        setActiveTool("select");
      } else if (placement.kind === "highlight") {
        const annotation: PdfHighlightAnnotation = {
          id,
          kind: "highlight",
          page: pageNumber,
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          color: "#fde047",
          opacity: 0.45,
        };
        commitAnnotations((current) => [...current, annotation]);
      } else {
        const annotation: PdfWhiteoutAnnotation = {
          id,
          kind: "whiteout",
          page: pageNumber,
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
        };
        commitAnnotations((current) => [...current, annotation]);
      }
      setSelectedAnnotationId(id);
      setEditorMessage("");
      return;
    }

    const draft = drawingDraftRef.current;
    if (activeTool !== "draw" || !draft || draft.page !== pageNumber) return;
    if (event) {
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch {
        // The browser may have already released capture after cancellation.
      }
    }
    drawingDraftRef.current = null;
    setDrawingDraft(null);
    if (draft.points.length < 2) return;

    const xs = draft.points.map((point) => point.x);
    const ys = draft.points.map((point) => point.y);
    const minX = Math.max(0, Math.min(...xs));
    const minY = Math.max(0, Math.min(...ys));
    const maxX = Math.min(1, Math.max(...xs));
    const maxY = Math.min(1, Math.max(...ys));
    const width = Math.max(0.003, maxX - minX);
    const height = Math.max(0.003, maxY - minY);
    const id = createPdfAnnotationId();
    const annotation: PdfDrawAnnotation = {
      id,
      kind: "draw",
      page: pageNumber,
      x: minX,
      y: minY,
      width,
      height,
      color: draft.color,
      strokeWidth: draft.strokeWidth,
      points: draft.points.map((point) => ({
        x: clampRatio((point.x - minX) / width),
        y: clampRatio((point.y - minY) / height),
      })),
    };
    commitAnnotations((current) => [...current, annotation]);
    setSelectedAnnotationId(id);
    setActiveTool("select");
  }, [activeTool, canEdit, commitAnnotations, getPagePoint, signatureDataUrl]);

  const buildEditedPdfBlob = useCallback(async () => {
    if (annotations.length === 0) throw new Error(PDF_EDITOR_COPY.noEdits);
    const source = await fetch(url);
    if (!source.ok) throw new Error("PDF fetch failed");
    const sourceBytes = await source.arrayBuffer();
    const { PDFDocument, StandardFonts, rgb } = await import("pdf-lib");
    const editedPdf = await PDFDocument.load(sourceBytes);
    const font = await editedPdf.embedFont(StandardFonts.Helvetica);
    const pages = editedPdf.getPages();

    for (const annotation of annotations) {
      const page = pages[annotation.page - 1];
      if (!page) continue;
      const { width: pageWidth, height: pageHeight } = page.getSize();
      const x = annotation.x * pageWidth;
      const top = annotation.y * pageHeight;
      const boxWidth = annotation.width * pageWidth;
      const boxHeight = annotation.height * pageHeight;

      if (annotation.kind === "text") {
        const [r, g, b] = parseHexColor(annotation.color);
        const lineHeight = annotation.fontSize * 1.25;
        const lines = wrapPdfText(font, annotation.text, annotation.fontSize, Math.max(20, boxWidth));
        const maxLines = Math.max(1, Math.floor(boxHeight / lineHeight));
        lines.slice(0, maxLines).forEach((line, index) => {
          page.drawText(line, {
            x,
            y: pageHeight - top - annotation.fontSize - (index * lineHeight),
            size: annotation.fontSize,
            font,
            color: rgb(r, g, b),
          });
        });
      } else if (annotation.kind === "signature") {
        const signatureImage = await editedPdf.embedPng(await dataUrlToUint8Array(annotation.dataUrl));
        page.drawImage(signatureImage, {
          x,
          y: pageHeight - top - boxHeight,
          width: boxWidth,
          height: boxHeight,
        });
      } else if (annotation.kind === "highlight") {
        const [r, g, b] = parseHexColor(annotation.color);
        page.drawRectangle({
          x,
          y: pageHeight - top - boxHeight,
          width: boxWidth,
          height: boxHeight,
          color: rgb(r, g, b),
          opacity: annotation.opacity,
        });
      } else if (annotation.kind === "whiteout") {
        page.drawRectangle({
          x,
          y: pageHeight - top - boxHeight,
          width: boxWidth,
          height: boxHeight,
          color: rgb(1, 1, 1),
          opacity: 1,
        });
      } else {
        const [r, g, b] = parseHexColor(annotation.color);
        for (let i = 1; i < annotation.points.length; i++) {
          const prev = annotation.points[i - 1];
          const next = annotation.points[i];
          page.drawLine({
            start: {
              x: (annotation.x + prev.x * annotation.width) * pageWidth,
              y: pageHeight - ((annotation.y + prev.y * annotation.height) * pageHeight),
            },
            end: {
              x: (annotation.x + next.x * annotation.width) * pageWidth,
              y: pageHeight - ((annotation.y + next.y * annotation.height) * pageHeight),
            },
            thickness: annotation.strokeWidth,
            color: rgb(r, g, b),
            opacity: 0.95,
          });
        }
      }
    }

    const bytes = await editedPdf.save();
    const pdfBytes = new Uint8Array(bytes.length);
    pdfBytes.set(bytes);
    return new Blob([pdfBytes], { type: "application/pdf" });
  }, [annotations, url]);

  const handleDownloadEdited = useCallback(async () => {
    setExporting("download");
    setEditorMessage("");
    try {
      const blob = await buildEditedPdfBlob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = ensurePdfName(docName, " edited");
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
      setEditorMessage(PDF_EDITOR_COPY.exported);
    } catch (e: any) {
      setEditorMessage(e?.message || PDF_EDITOR_COPY.exportFailed);
    } finally {
      setExporting(null);
    }
  }, [buildEditedPdfBlob, docName]);

	  const handleSaveEdited = useCallback(async () => {
	    if (!docId || !canEdit) return;
	    setExporting("save");
	    setEditorMessage("");
	    try {
	      const blob = await buildEditedPdfBlob();
	      const file = new File([blob], ensurePdfName(docName), { type: "application/pdf" });
	      const savedDoc = await api.documents.replaceFile(docId, file);
	      annotationsRef.current = [];
	      setAnnotationsRaw([]);
	      setAnnotationPast([]);
	      setAnnotationFuture([]);
	      setSelectedAnnotationId(null);
	      setActiveTool("select");
	      setEditorMessage(PDF_EDITOR_COPY.saved);
	      await onSaved?.(savedDoc, blob);
	    } catch (e: any) {
	      setEditorMessage(e?.message || PDF_EDITOR_COPY.saveFailed);
	    } finally {
	      setExporting(null);
	    }
	  }, [buildEditedPdfBlob, canEdit, docId, docName, onSaved]);

  const buildPdfLiveEditContent = useCallback(() => {
    return JSON.stringify({
      format: "manor-pdf-overlay-v1",
      documentName: docName || "document.pdf",
      pageCount,
      coordinateSystem: "Normalized page coordinates from top-left: page is 1-based; x/y/width/height are 0..1.",
      annotations: annotationsRef.current.map(serializePdfAnnotationForLiveEdit),
      pageText: pageTextHints,
    }, null, 2);
  }, [docName, pageCount, pageTextHints]);

  const localPdfEditContent = useCallback((userRequest: string) => {
    const request = userRequest.trim();
    if (!request) return null;
    const lower = request.toLowerCase();
    const currentAnnotations = annotationsRef.current.map(serializePdfAnnotationForLiveEdit);
    const base = {
      format: "manor-pdf-overlay-v1",
      documentName: docName || "document.pdf",
      annotations: currentAnnotations,
    };
    const serialize = (annotations: unknown[]) => JSON.stringify({
      ...base,
      annotations,
    }, null, 2);

    if (
      /(clear|remove|delete|reset|清除|删除|移除|重置)/.test(lower) &&
      /(annotation|overlay|edit|mark|批注|标注|编辑|全部|所有|all)/.test(lower)
    ) {
      return serialize([]);
    }

    const quoted = request.match(/["'“”‘’]([^"'“”‘’]{1,180})["'“”‘’]/)?.[1]?.trim();
    const afterAdd = request.match(/(?:add|insert|write|type|label|添加|加入|写上|输入|标注)\s*[:：]?\s*(.{1,120})/i)?.[1]?.trim();
    const requestedText = (quoted || afterAdd || "").replace(/[。.!?？]+$/, "").trim();

    const findHint = (needle: string) => {
      const normalized = needle.trim().toLowerCase();
      if (!normalized) return null;
      for (const page of pageTextHints) {
        const item = page.items.find((candidate) =>
          candidate.text.toLowerCase().includes(normalized) ||
          normalized.includes(candidate.text.toLowerCase()),
        );
        if (item) return { page: page.page, item };
      }
      return null;
    };

    if (/(highlight|mark|高亮|标记)/.test(lower)) {
      const hint = findHint(quoted || requestedText);
      const annotation = {
        id: createPdfAnnotationId(),
        kind: "highlight",
        page: hint?.page || 1,
        x: hint?.item.x ?? 0.1,
        y: hint?.item.y ?? 0.12,
        width: Math.max(hint?.item.width ?? 0.34, 0.08),
        height: Math.max(hint?.item.height ?? 0.035, 0.025),
        color: "#fde047",
        opacity: 0.45,
      };
      return serialize([...currentAnnotations, annotation]);
    }

    if (/(whiteout|white out|cover|redact|遮盖|涂白|抹掉|打码)/.test(lower)) {
      const hint = findHint(quoted || requestedText);
      const annotation = {
        id: createPdfAnnotationId(),
        kind: "whiteout",
        page: hint?.page || 1,
        x: hint?.item.x ?? 0.1,
        y: hint?.item.y ?? 0.12,
        width: Math.max(hint?.item.width ?? 0.34, 0.1),
        height: Math.max(hint?.item.height ?? 0.05, 0.035),
      };
      return serialize([...currentAnnotations, annotation]);
    }

    if (requestedText && /(add|insert|write|type|text|label|添加|加入|写上|输入|文字|文本|标注)/.test(lower)) {
      const annotation = {
        id: createPdfAnnotationId(),
        kind: "text",
        page: 1,
        x: 0.1,
        y: 0.1 + Math.min(0.6, currentAnnotations.length * 0.075),
        width: 0.72,
        height: 0.06,
        text: requestedText,
        fontSize: 14,
        color: "#1c1917",
      };
      return serialize([...currentAnnotations, annotation]);
    }

    return null;
  }, [docName, pageTextHints]);

  useEffect(() => {
    if (!onLiveEditBridgeChange || !canEdit || pageCount === 0) return;
    onLiveEditBridgeChange({
      getContent: buildPdfLiveEditContent,
      applyContent: (next, meta) => {
        if (!meta.complete) return;
        try {
          const parsedAnnotations = parsePdfLiveEditAnnotations(
            next,
            pageCount,
            annotationsRef.current,
            signatureDataUrl,
          );
          commitAnnotations(parsedAnnotations);
          const last = parsedAnnotations[parsedAnnotations.length - 1];
          setSelectedAnnotationId(last?.id || null);
          setActiveTool("select");
          setEditorMessage("AI edit applied. Review it, then Save PDF to write the changes.");
        } catch (error) {
          setEditorMessage(error instanceof Error ? error.message : "AI edit returned invalid PDF annotations.");
        }
      },
      localEditContent: localPdfEditContent,
    });
    return () => onLiveEditBridgeChange(null);
  }, [buildPdfLiveEditContent, canEdit, commitAnnotations, localPdfEditContent, onLiveEditBridgeChange, pageCount, signatureDataUrl]);

    useEffect(() => {
      const handler = (event: KeyboardEvent) => {
        if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
        event.preventDefault();
        if (!canEdit || !hasAnnotations || busy) return;
        if (event.shiftKey) void handleDownloadEdited();
        else if (docId) void handleSaveEdited();
      };
      window.addEventListener("keydown", handler);
      return () => window.removeEventListener("keydown", handler);
    }, [busy, canEdit, docId, handleDownloadEdited, handleSaveEdited, hasAnnotations]);

  const renderAnnotation = (annotation: PdfAnnotation) => {
    const selected = annotation.id === selectedAnnotationId;
    const annotationFill = annotation.kind === "highlight"
      ? hexToRgba(annotation.color, annotation.opacity)
      : annotation.kind === "whiteout"
        ? "#ffffff"
        : annotation.kind === "text"
          ? "transparent"
          : "rgba(255,255,255,0.18)";
    const annotationBorder = selected
      ? "1px solid #436b65"
      : annotation.kind === "text" || annotation.kind === "highlight" || annotation.kind === "whiteout"
        ? "1px solid transparent"
        : "1px solid rgba(67,107,101,0.18)";
    const commonStyle: React.CSSProperties = {
      position: "absolute",
      left: `${annotation.x * 100}%`,
      top: `${annotation.y * 100}%`,
      width: `${annotation.width * 100}%`,
      height: `${annotation.height * 100}%`,
      zIndex: selected ? 4 : 3,
      border: annotationBorder,
      borderRadius: 6,
      background: annotation.kind === "draw"
        ? "transparent"
        : annotation.kind === "text" && selected
          ? "rgba(255,255,255,0.78)"
          : annotationFill,
      boxShadow: selected ? "0 0 0 2px rgba(67,107,101,0.16)" : "none",
      cursor: "move",
      overflow: annotation.kind === "draw" ? "visible" : "hidden",
      touchAction: "none",
    };

    return (
      <div
        key={annotation.id}
        onPointerDown={(event) => startAnnotationDrag(event, annotation)}
        onClick={(event) => {
          event.stopPropagation();
          setSelectedAnnotationId(annotation.id);
        }}
        style={commonStyle}
      >
        {annotation.kind === "text" ? (
          <textarea
            ref={(element) => {
              if (element) textAreaRefs.current.set(annotation.id, element);
              else textAreaRefs.current.delete(annotation.id);
              }}
              value={annotation.text}
              placeholder={t("page.file_viewer.pdf_editor.type_text")}
              onChange={(event) => updateAnnotation(annotation.id, { text: event.target.value } as Partial<PdfAnnotation>)}
              onFocus={() => {
                beginAnnotationEdit(annotation.id);
                setSelectedAnnotationId(annotation.id);
              }}
              onBlur={() => endAnnotationEdit(annotation.id)}
            onPointerDown={(event) => {
              event.stopPropagation();
              setSelectedAnnotationId(annotation.id);
            }}
            onClick={(event) => event.stopPropagation()}
            style={{
              width: "100%",
              height: "100%",
              resize: "none",
              border: "none",
              outline: "none",
              background: "transparent",
              color: annotation.color,
              fontSize: Math.max(8, annotation.fontSize * (zoom / 100)),
              lineHeight: 1.25,
              fontFamily: "Helvetica, Arial, sans-serif",
              padding: 4,
              cursor: "text",
              pointerEvents: "auto",
            }}
          />
        ) : annotation.kind === "signature" ? (
          <img
            src={annotation.dataUrl}
            alt=""
            draggable={false}
            style={{ width: "100%", height: "100%", objectFit: "contain", display: "block", pointerEvents: "none" }}
          />
        ) : annotation.kind === "draw" ? (
          <svg viewBox="0 0 1 1" preserveAspectRatio="none" style={{ width: "100%", height: "100%", display: "block", overflow: "visible", pointerEvents: "none" }}>
            <polyline
              points={annotation.points.map((point) => `${point.x},${point.y}`).join(" ")}
              fill="none"
              stroke={annotation.color}
              strokeWidth={Math.max(0.003, annotation.strokeWidth / 500)}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        ) : (
          <div style={{ width: "100%", height: "100%", pointerEvents: "none" }} />
        )}
        {selected && (
          <div
            title={t("page.file_viewer.pdf_editor.move")}
            onPointerDown={(event) => startAnnotationDrag(event, annotation)}
            style={{
              position: "absolute",
              top: 2,
              right: 2,
              width: 14,
              height: 14,
              zIndex: 2,
              borderRadius: 4,
              background: "#436b65",
              boxShadow: "0 1px 4px rgba(28,25,23,0.18)",
              cursor: "move",
            }}
          />
        )}
        {selected && annotation.kind !== "draw" && (
          <div
            title={t("page.file_viewer.pdf_editor.resize")}
            onPointerDown={(event) => startAnnotationResize(event, annotation)}
            style={{
              position: "absolute",
              right: 2,
              bottom: 2,
              width: 12,
              height: 12,
              zIndex: 2,
              borderRadius: 4,
              border: "2px solid #ffffff",
              background: "#436b65",
              boxShadow: "0 1px 4px rgba(28,25,23,0.2)",
              cursor: "nwse-resize",
            }}
          />
        )}
      </div>
    );
  };

  const renderRectDraft = (draft: PdfRectDraft) => {
    const rect = normalizePdfRect(draft.origin, draft.current);
    if (rect.width === 0 && rect.height === 0) return null;
    const background = draft.kind === "highlight"
      ? hexToRgba("#fde047", 0.45)
      : draft.kind === "whiteout"
        ? "rgba(255,255,255,0.92)"
        : "transparent";
    return (
      <div
        style={{
          position: "absolute",
          left: `${rect.x * 100}%`,
          top: `${rect.y * 100}%`,
          width: `${rect.width * 100}%`,
          height: `${rect.height * 100}%`,
          zIndex: 5,
          border: "1px dashed #436b65",
          borderRadius: 6,
          background,
          pointerEvents: "none",
        }}
      />
    );
  };

  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 64 }}><LoadingSpinner size={28} /></div>;
  if (error) return <PreviewDownloadFallback message={error} onDownload={onDownload} />;
  if (pageCount === 0) return <PreviewDownloadFallback message={t("page.file_viewer.pdf_preview_requires_downloading_the_file")} onDownload={onDownload} />;

    const hint = activeTool === "text"
    ? PDF_EDITOR_COPY.clickToPlaceText
    : activeTool === "signature"
      ? PDF_EDITOR_COPY.clickToPlaceSignature
      : activeTool === "highlight"
        ? PDF_EDITOR_COPY.clickToPlaceHighlight
        : activeTool === "whiteout"
          ? PDF_EDITOR_COPY.clickToPlaceWhiteout
          : activeTool === "draw"
            ? PDF_EDITOR_COPY.dragToDraw
      : editorMessage;
  const showInspector = canEdit && Boolean(selectedAnnotation || (signatureDataUrl && activeTool === "signature"));

  return (
    <div className="pdf-editor-shell">
      <div className="manor-editor-toolbar pdf-editor-toolbar">
        <button onClick={() => setZoom(Math.max(50, zoom - 25))} className={editorToolButtonClass({ icon: true })}>
          <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 12h-15" /></svg>
        </button>
        <span className="manor-editor-toolbar-muted" style={{ width: 52, textAlign: "center" }}>{zoom}%</span>
        <button onClick={() => setZoom(Math.min(200, zoom + 25))} className={editorToolButtonClass({ icon: true })}>
          <IconPlus size={16} />
        </button>
        {canEdit && (
          <>
            <span className="manor-editor-toolbar-divider" />
            <button
              type="button"
              title={PDF_EDITOR_COPY.undo}
              onClick={undoAnnotations}
              disabled={annotationPast.length === 0 || busy}
              className={editorToolButtonClass({ icon: true })}
            >
              <IconUndo size={15} />
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.redo}
              onClick={redoAnnotations}
              disabled={annotationFuture.length === 0 || busy}
              className={editorToolButtonClass({ icon: true })}
            >
              <IconRedo size={15} />
            </button>
            <span className="manor-editor-toolbar-divider" />
            <button
              type="button"
              title={PDF_EDITOR_COPY.select}
              onClick={() => setActiveTool("select")}
              className={editorToolButtonClass({ active: activeTool === "select" })}
            >
              <IconEdit size={15} />
              {PDF_EDITOR_COPY.select}
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.addText}
              onClick={() => setActiveTool("text")}
              className={editorToolButtonClass({ active: activeTool === "text" })}
            >
              <IconText size={15} />
              {PDF_EDITOR_COPY.addText}
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.eSign}
              onClick={() => {
                if (!signatureDataUrl) setShowSignatureDialog(true);
                setActiveTool("signature");
              }}
              className={editorToolButtonClass({ active: activeTool === "signature" })}
            >
              <IconSignature size={15} />
              {PDF_EDITOR_COPY.eSign}
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.highlight}
              onClick={() => setActiveTool("highlight")}
              className={editorToolButtonClass({ active: activeTool === "highlight" })}
            >
              <IconHighlighter size={15} />
              {PDF_EDITOR_COPY.highlight}
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.whiteout}
              onClick={() => setActiveTool("whiteout")}
              className={editorToolButtonClass({ active: activeTool === "whiteout" })}
            >
              <IconEraser size={15} />
              {PDF_EDITOR_COPY.whiteout}
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.draw}
              onClick={() => setActiveTool("draw")}
              className={editorToolButtonClass({ active: activeTool === "draw" })}
            >
              <IconPenLine size={15} />
              {PDF_EDITOR_COPY.draw}
            </button>
            <span className="manor-editor-toolbar-divider" />
            <button
              type="button"
              title={PDF_EDITOR_COPY.download}
              onClick={handleDownloadEdited}
              disabled={!hasAnnotations || busy}
              className={editorToolButtonClass()}
            >
              <IconDownload size={15} />
              {exporting === "download" ? t("status.loading") : PDF_EDITOR_COPY.download}
            </button>
            <button
              type="button"
              title={PDF_EDITOR_COPY.save}
              onClick={handleSaveEdited}
              disabled={!hasAnnotations || busy || !docId}
              className={editorToolButtonClass({ primary: true })}
            >
              <IconCheck size={15} />
              {exporting === "save" ? t("status.loading") : PDF_EDITOR_COPY.save}
            </button>
            {hasAnnotations && (
              <button
                type="button"
                title={PDF_EDITOR_COPY.clear}
                onClick={() => {
                  commitAnnotations([]);
                  setSelectedAnnotationId(null);
                  setEditorMessage("");
                }}
                disabled={busy}
                className={editorToolButtonClass({ icon: true })}
              >
                <IconTrash size={15} />
              </button>
            )}
          </>
        )}
        {rendering && <span className="manor-editor-toolbar-muted">{t("status.loading")}</span>}
        {hint && !showInspector && (
          <span className="pdf-editor-toolbar-hint" style={{ color: hint.includes("failed") || hint.includes("Could not") ? "#c14a44" : undefined }}>
            {hint}
          </span>
        )}
      </div>
      <div className={`pdf-editor-body${showInspector ? " pdf-editor-body--inspector-open" : ""}`}>
        {showInspector && (
        <aside className="pdf-editor-inspector">
          <div className="pdf-editor-inspector-title">
            <div>
              <h3>PDF editor</h3>
              <p>{annotations.length} editable layer{annotations.length === 1 ? "" : "s"}</p>
            </div>
            <StatusBadge type={hasAnnotations ? "orange" : "gray"}>
              {hasAnnotations ? "Unsaved" : "Clean"}
            </StatusBadge>
          </div>
          {hint ? (
            <p className="pdf-editor-inspector-hint" style={{ color: hint.includes("failed") || hint.includes("Could not") ? "#c14a44" : undefined }}>{hint}</p>
          ) : (
            <p className="pdf-editor-inspector-hint">Choose a tool, then draw or select an editable layer on the PDF.</p>
          )}
          {signatureDataUrl && (activeTool === "signature" || selectedAnnotation?.kind === "signature") && (
            <div className="pdf-editor-inspector-section">
              <span className="manor-editor-toolbar-muted">{PDF_EDITOR_COPY.savedSignature}</span>
              <span style={{ width: 82, height: 30, borderRadius: 8, border: "1px solid rgba(28,25,23,0.06)", background: "white", display: "inline-flex", alignItems: "center", justifyContent: "center", padding: 3 }}>
                <img src={signatureDataUrl} alt="" style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }} />
              </span>
              <button
                type="button"
                onClick={() => setShowSignatureDialog(true)}
                className={editorToolButtonClass()}
              >
                {PDF_EDITOR_COPY.redrawSignature}
              </button>
              <button
                type="button"
                onClick={() => {
                  removeSavedPdfSignature();
                  setSignatureDataUrl(null);
                  setActiveTool("select");
                  setEditorMessage("");
                }}
                className={editorToolButtonClass({ danger: true })}
              >
                {PDF_EDITOR_COPY.forgetSignature}
              </button>
            </div>
          )}
          {selectedAnnotation?.kind === "text" && (
            <div className="pdf-editor-inspector-section">
              <label className="manor-editor-toolbar-label">
                Size
                <input
                  type="number"
                    min={8}
                    max={48}
                    value={selectedAnnotation.fontSize}
                    onFocus={() => beginAnnotationEdit(selectedAnnotation.id)}
                    onBlur={() => endAnnotationEdit(selectedAnnotation.id)}
                    onChange={(event) => updateAnnotation(selectedAnnotation.id, { fontSize: Math.min(48, Math.max(8, Number(event.target.value) || 14)) } as Partial<PdfAnnotation>)}
                  className="manor-editor-number-input"
                />
              </label>
              <div className="manor-editor-swatch-row">
                {PDF_TEXT_COLORS.map((color) => (
                  <button
                      key={color}
                      type="button"
                      title={color}
                      onClick={() => commitAnnotationPatch(selectedAnnotation.id, { color } as Partial<PdfAnnotation>)}
                    className={editorSwatchClass(selectedAnnotation.color === color)}
                    style={{ background: color }}
                  />
                ))}
              </div>
            </div>
          )}
          {selectedAnnotation?.kind === "highlight" && (
            <div className="pdf-editor-inspector-section">
              <span className="manor-editor-toolbar-muted">Highlight color</span>
              <div className="manor-editor-swatch-row">
                {PDF_HIGHLIGHT_COLORS.map((color) => (
                  <button
                      key={color}
                      type="button"
                      title={color}
                      onClick={() => commitAnnotationPatch(selectedAnnotation.id, { color } as Partial<PdfAnnotation>)}
                    className={editorSwatchClass(selectedAnnotation.color === color)}
                    style={{ background: color }}
                  />
                ))}
              </div>
            </div>
          )}
          {selectedAnnotation?.kind === "draw" && (
            <div className="pdf-editor-inspector-section">
              <label className="manor-editor-toolbar-label">
                Stroke
                <input
                  type="range"
                    min={1}
                    max={8}
                    value={selectedAnnotation.strokeWidth}
                    onFocus={() => beginAnnotationEdit(selectedAnnotation.id)}
                    onBlur={() => endAnnotationEdit(selectedAnnotation.id)}
                    onChange={(event) => updateAnnotation(selectedAnnotation.id, { strokeWidth: Number(event.target.value) } as Partial<PdfAnnotation>)}
                  className="manor-editor-range"
                />
              </label>
              <div className="manor-editor-swatch-row">
                {PDF_DRAW_COLORS.map((color) => (
                  <button
                      key={color}
                      type="button"
                      title={color}
                      onClick={() => commitAnnotationPatch(selectedAnnotation.id, { color } as Partial<PdfAnnotation>)}
                    className={editorSwatchClass(selectedAnnotation.color === color)}
                    style={{ background: color }}
                  />
                ))}
              </div>
            </div>
          )}
          {selectedAnnotation && selectedAnnotation.kind !== "draw" && (
            <div className="pdf-editor-inspector-section">
              <label className="manor-editor-toolbar-label">
                W
                <input
                  type="range"
                  min={8}
                    max={80}
                    value={Math.round(selectedAnnotation.width * 100)}
                    onFocus={() => beginAnnotationEdit(selectedAnnotation.id)}
                    onBlur={() => endAnnotationEdit(selectedAnnotation.id)}
                    onChange={(event) => updateAnnotation(selectedAnnotation.id, { width: Number(event.target.value) / 100 } as Partial<PdfAnnotation>)}
                  className="manor-editor-range"
                />
              </label>
              <label className="manor-editor-toolbar-label">
                H
                <input
                  type="range"
                  min={3}
                    max={35}
                    value={Math.round(selectedAnnotation.height * 100)}
                    onFocus={() => beginAnnotationEdit(selectedAnnotation.id)}
                    onBlur={() => endAnnotationEdit(selectedAnnotation.id)}
                    onChange={(event) => updateAnnotation(selectedAnnotation.id, { height: Number(event.target.value) / 100 } as Partial<PdfAnnotation>)}
                  className="manor-editor-range"
                />
              </label>
            </div>
          )}
	          {selectedAnnotation && (
	            <div className="pdf-editor-inspector-section">
	              <button
	                type="button"
	                onClick={duplicateSelectedAnnotation}
	                title={`${t("action.copy")} (Ctrl+D)`}
	                className={editorToolButtonClass()}
	              >
	                <IconCopy size={14} />
	                {t("action.copy")}
	              </button>
	              <button
	                type="button"
	                onClick={deleteSelectedAnnotation}
	                className={editorToolButtonClass({ danger: true })}
	              >
	                <IconTrash size={14} />
	                {PDF_EDITOR_COPY.delete}
	              </button>
	            </div>
	          )}
        </aside>
        )}
        <div className="pdf-editor-pages">
        {Array.from({ length: pageCount }, (_, i) => i + 1).map((pageNumber) => (
          <div
            key={pageNumber}
            data-pdf-page="true"
            className="pdf-editor-page"
            onClick={handlePageClick}
            onPointerDown={(event) => handlePagePointerDown(pageNumber, event)}
            onPointerMove={(event) => handlePagePointerMove(pageNumber, event)}
            onPointerUp={(event) => handlePagePointerUp(pageNumber, event)}
            onPointerCancel={(event) => handlePagePointerUp(pageNumber, event)}
            style={{
              cursor: activeTool === "select" ? "default" : "crosshair",
              touchAction: activeTool === "draw" || isPdfRectPlacementTool(activeTool) ? "none" : undefined,
            }}
          >
            <canvas
              ref={(canvas) => {
                if (canvas) canvasesRef.current.set(pageNumber, canvas);
                else canvasesRef.current.delete(pageNumber);
              }}
            />
            {annotations.filter((annotation) => annotation.page === pageNumber).map(renderAnnotation)}
            {rectDraft?.page === pageNumber && renderRectDraft(rectDraft)}
            {drawingDraft?.page === pageNumber && drawingDraft.points.length > 1 && (
              <svg
                viewBox="0 0 1 1"
                preserveAspectRatio="none"
                style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none", zIndex: 5, overflow: "visible" }}
              >
                <polyline
                  points={drawingDraft.points.map((point) => `${point.x},${point.y}`).join(" ")}
                  fill="none"
                  stroke={drawingDraft.color}
                  strokeWidth={Math.max(0.003, drawingDraft.strokeWidth / 500)}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </div>
        ))}
        </div>
      </div>
      {showSignatureDialog && (
        <PdfSignatureDialog
          onClose={() => setShowSignatureDialog(false)}
          onSave={(dataUrl) => {
            savePdfSignature(dataUrl);
            setSignatureDataUrl(dataUrl);
            setActiveTool("signature");
            setShowSignatureDialog(false);
            setEditorMessage(PDF_EDITOR_COPY.signatureSaved);
          }}
        />
      )}
    </div>
  );
}

function PptxViewJsViewer({ url, onDownload }: { url: string; onDownload: () => void }) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const viewerRef = useRef<any>(null);
  const slideAspectRef = useRef(16 / 9);
  const [slideCount, setSlideCount] = useState(0);
  const [activeSlide, setActiveSlide] = useState(0);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState("");

  const prepareCanvas = useCallback((aspect = slideAspectRef.current) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const stageWidth = stageRef.current?.clientWidth || 980;
    const stageHeight = stageRef.current?.clientHeight || Math.round(stageWidth / aspect);
    const gutter = 2;
    const maxWidth = Math.max(240, stageWidth - gutter);
    const maxHeight = Math.max(135, stageHeight - gutter);
    const fitByWidth = maxWidth;
    const fitByHeight = maxHeight * aspect;
    const width = Math.max(240, Math.floor(Math.min(fitByWidth, fitByHeight)));
    const height = Math.max(135, Math.round(width / aspect));
    const pixelRatio = window.devicePixelRatio || 1;

    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    return canvas;
  }, []);

  const getViewerAspect = useCallback((viewer: any) => {
    const slideSize = viewer?.processor?.getSlideDimensions?.() || viewer?.presentation?.slideSize;
    const aspect = slideSize?.cx && slideSize?.cy ? slideSize.cx / slideSize.cy : 16 / 9;
    return Number.isFinite(aspect) ? Math.min(4, Math.max(0.25, aspect)) : 16 / 9;
  }, []);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!canvasRef.current) return;
      setLoading(true);
      setError("");
      setSlideCount(0);
      setActiveSlide(0);
      prepareCanvas(16 / 9);

      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error("Presentation fetch failed");
        const buf = await res.arrayBuffer();
        if (cancelled || !canvasRef.current) return;

        const { PPTXViewer } = await import("pptxviewjs");
        if (cancelled || !canvasRef.current) return;
        const viewer = new PPTXViewer({
          canvas: canvasRef.current,
          backgroundColor: "#ffffff",
          slideSizeMode: "fit",
        });
        await withPreviewTimeout(viewer.loadFile(buf), 20000, "Presentation preview timed out");
        if (cancelled) {
          viewer.destroy();
          return;
        }

        viewerRef.current = viewer;
        const total = viewer.getSlideCount();
        const aspect = getViewerAspect(viewer);
        slideAspectRef.current = aspect;
        const renderCanvas = prepareCanvas(aspect);
        setSlideCount(total);
        if (total > 0 && renderCanvas) {
          await withPreviewTimeout(
            viewer.render(renderCanvas, { slideIndex: 0, quality: "high" }),
            20000,
            "Presentation preview timed out",
          );
        }
      } catch (e: any) {
        if (!cancelled) setError(e?.message || t("page.file_viewer.this_file_type_cannot_be_previewed_download_to_v"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      viewerRef.current?.destroy();
      viewerRef.current = null;
    };
  }, [getViewerAspect, prepareCanvas, url]);

  const renderSlide = useCallback(async (slideIndex: number) => {
    const renderCanvas = prepareCanvas();
    if (!viewerRef.current || !renderCanvas) return;
    setRendering(true);
    try {
      await withPreviewTimeout(
        viewerRef.current.renderSlide(slideIndex, renderCanvas, { quality: "high" }),
        20000,
        "Presentation preview timed out",
      );
      setActiveSlide(slideIndex);
    } catch (e: any) {
      setError(e?.message || t("page.file_viewer.this_file_type_cannot_be_previewed_download_to_v"));
    } finally {
      setRendering(false);
    }
  }, [prepareCanvas]);

  if (error) return <PreviewDownloadFallback message={error} onDownload={onDownload} />;

  return (
    <div className="pptx-viewer-shell">
      <div ref={stageRef} className="pptx-viewer-stage">
        <canvas ref={canvasRef} style={{ display: "block", background: "white" }} />
        {loading && (
          <div style={{
            position: "absolute", inset: 0, display: "flex", alignItems: "center",
            justifyContent: "center", background: "rgba(250,250,249,0.82)",
          }}>
            <LoadingSpinner size={28} />
          </div>
        )}
      </div>
      {slideCount > 1 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 12, marginTop: 14 }}>
          <button
            onClick={() => renderSlide(Math.max(0, activeSlide - 1))}
            disabled={activeSlide === 0 || rendering}
            style={{
              width: 36, height: 36, borderRadius: 10,
              background: activeSlide === 0 || rendering ? "#f5f5f4" : "white",
              border: "1px solid rgba(28,25,23,0.06)",
              cursor: activeSlide === 0 || rendering ? "default" : "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: activeSlide === 0 || rendering ? "#d6d3d1" : "#57534e",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d="M15 18l-6-6 6-6" /></svg>
          </button>
          <span style={{ fontSize: 13, fontWeight: 600, color: "#78716c" }}>
            {t("page.file_viewer.slide")} {activeSlide + 1} {t("page.file_viewer.of")} {slideCount}
          </span>
          <button
            onClick={() => renderSlide(Math.min(slideCount - 1, activeSlide + 1))}
            disabled={activeSlide === slideCount - 1 || rendering}
            style={{
              width: 36, height: 36, borderRadius: 10,
              background: activeSlide === slideCount - 1 || rendering ? "#f5f5f4" : "white",
              border: "1px solid rgba(28,25,23,0.06)",
              cursor: activeSlide === slideCount - 1 || rendering ? "default" : "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: activeSlide === slideCount - 1 || rendering ? "#d6d3d1" : "#57534e",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d="M9 18l6-6-6-6" /></svg>
          </button>
        </div>
      )}
    </div>
  );
}

// ── PPTX viewer ──
function PptxViewer({ url, docId }: { url: string; docId?: string }) {
  const [slides, setSlides] = useState<PptxSlide[]>([]);
  const [slideImageUrls, setSlideImageUrls] = useState<string[]>([]);
  const [activeSlide, setActiveSlide] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [renderMode, setRenderMode] = useState<"server" | "css">("css");

  useEffect(() => {
    let cancelled = false;

    (async () => {
      // Try server-rendered slides first (pixel-perfect via LibreOffice)
      // Use raw fetch to avoid error toasts when server rendering is unavailable
      if (docId) {
        try {
          const token = getAuthToken();
          const headers: Record<string, string> = {};
          if (token) headers["Authorization"] = `Bearer ${token}`;
          const slideRes = await fetch(`/api/v1/documents/${docId}/slides`, { headers });
          if (slideRes.ok) {
            const slideData = await slideRes.json();
            if (!cancelled && slideData.slides?.length > 0) {
              const blobUrls = await Promise.all(
                slideData.slides.map(async (s: { url: string }) => {
                  const res = await fetch(`/api/v1${s.url}`, { headers });
                  if (!res.ok) throw new Error("Slide fetch failed");
                  const blob = await res.blob();
                  return URL.createObjectURL(blob);
                })
              );
              if (!cancelled) {
                setSlideImageUrls(blobUrls);
                setRenderMode("server");
                setLoading(false);
              }
              return;
            }
          }
        } catch {
          // Server rendering unavailable — fall back to CSS parsing
        }
      }

      // Fallback: client-side CSS-based parsing
      try {
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const bytes = new Uint8Array(buf);

        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const parsed = await parsePptx(buf);
          if (!cancelled) setSlides(parsed.length > 0 ? parsed : [{ shapes: [] }]);
        } else {
          const text = new TextDecoder().decode(bytes);
          const parsedSlides = parseTextSlides(text);
          if (!cancelled) setSlides(parsedSlides.length > 0 ? parsedSlides : [{ shapes: [] }]);
        }
        if (!cancelled) setRenderMode("css");
      } catch (e: any) {
        if (!cancelled) setError(e.message || "Failed to parse presentation");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      // Revoke blob URLs to free memory
      setSlideImageUrls((prev) => { prev.forEach((u) => URL.revokeObjectURL(u)); return []; });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, docId]);

  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 64 }}><LoadingSpinner size={28} /></div>;
  if (error) return <p style={{ color: "#c14a44", textAlign: "center", padding: 32 }}>{error}</p>;

  const totalSlides = renderMode === "server" ? slideImageUrls.length : slides.length;
  if (totalSlides === 0) return <p style={{ color: "#78716c", textAlign: "center", padding: 32 }}>{t("page.file_viewer.empty_presentation")}</p>;

  // Server-rendered mode: show slide images (blob URLs fetched with auth)
  if (renderMode === "server" && slideImageUrls.length > 0) {
    return (
      <div>
        <div style={{
          maxWidth: 900, margin: "0 auto", borderRadius: 12,
          border: "1px solid rgba(28,25,23,0.06)",
          boxShadow: "0 4px 24px rgba(0,0,0,0.08)",
          overflow: "hidden", background: "#000",
        }}>
          <img
            src={slideImageUrls[activeSlide]}
            alt={`Slide ${activeSlide + 1}`}
            style={{ width: "100%", display: "block" }}
          />
        </div>

        {/* Thumbnail strip */}
        {slideImageUrls.length > 1 && (
          <div style={{
            display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap",
            marginTop: 16, padding: "0 8px",
          }}>
            {slideImageUrls.map((blobUrl, i) => (
              <button
                key={i}
                onClick={() => setActiveSlide(i)}
                style={{
                  width: 96, height: 54, borderRadius: 6,
                  border: i === activeSlide ? "2px solid #4f7d75" : "1px solid #e7e5e4",
                  boxShadow: i === activeSlide ? "0 0 0 3px rgba(79,125,117,0.15)" : "none",
                  cursor: "pointer", overflow: "hidden", position: "relative",
                  flexShrink: 0, transition: "all 0.15s", padding: 0, background: "#000",
                }}
                title={`Slide ${i + 1}`}
              >
                <img src={blobUrl} alt={`Slide ${i + 1}`} style={{ width: "100%", height: "100%", objectFit: "contain" }} />
                <span style={{
                  position: "absolute", bottom: 2, right: 4, fontSize: 9,
                  fontWeight: 700, color: "rgba(255,255,255,0.9)", background: "rgba(0,0,0,0.5)",
                  borderRadius: 3, padding: "0 3px",
                }}>{i + 1}</span>
              </button>
            ))}
          </div>
        )}

        {/* Nav */}
        {slideImageUrls.length > 1 && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 12, marginTop: 12 }}>
            <button
              onClick={() => setActiveSlide(Math.max(0, activeSlide - 1))}
              disabled={activeSlide === 0}
              style={{
                width: 36, height: 36, borderRadius: 10,
                background: activeSlide === 0 ? "#f5f5f4" : "white",
                border: "1px solid rgba(28,25,23,0.06)",
                cursor: activeSlide === 0 ? "default" : "pointer",
                display: "flex", alignItems: "center", justifyContent: "center",
                color: activeSlide === 0 ? "#d6d3d1" : "#57534e",
              }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d="M15 18l-6-6 6-6" /></svg>
            </button>
            <span style={{ fontSize: 13, fontWeight: 600, color: "#78716c" }}>
              {t("page.file_viewer.slide")} {activeSlide + 1} {t("page.file_viewer.of")} {slideImageUrls.length}
            </span>
            <button
              onClick={() => setActiveSlide(Math.min(slideImageUrls.length - 1, activeSlide + 1))}
              disabled={activeSlide === slideImageUrls.length - 1}
              style={{
                width: 36, height: 36, borderRadius: 10,
                background: activeSlide === slideImageUrls.length - 1 ? "#f5f5f4" : "white",
                border: "1px solid rgba(28,25,23,0.06)",
                cursor: activeSlide === slideImageUrls.length - 1 ? "default" : "pointer",
                display: "flex", alignItems: "center", justifyContent: "center",
                color: activeSlide === slideImageUrls.length - 1 ? "#d6d3d1" : "#57534e",
              }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d="M9 18l6-6-6-6" /></svg>
            </button>
          </div>
        )}
      </div>
    );
  }

  // CSS fallback mode

  const slide = slides[activeSlide];

  // Build slide background style — layer: solid color < gradient < image
  const slideBg: React.CSSProperties = { backgroundColor: slide.bg || "#ffffff" };
  if (slide.bgGrad) slideBg.backgroundImage = gradientToCss(slide.bgGrad);
  if (slide.bgImgUrl) {
    slideBg.backgroundImage = `url(${slide.bgImgUrl})`;
    slideBg.backgroundSize = "cover";
    slideBg.backgroundPosition = "center";
    slideBg.backgroundRepeat = "no-repeat";
  }

  return (
    <div>
      {/* Slide canvas */}
      <div style={{
        position: "relative",
        aspectRatio: slide.aspectRatio || "16/9",
        maxWidth: 900,
        margin: "0 auto",
        borderRadius: 12,
        border: "1px solid rgba(28,25,23,0.06)",
        boxShadow: "0 4px 24px rgba(0,0,0,0.08)",
        overflow: "hidden",
        ...slideBg,
      }}>
        {slide.shapes.map((shape, si) => {
          let br: string | number | undefined = shape.borderRadius ? `${shape.borderRadius}%` : undefined;
          if (shape.presetGeom === "ellipse" || shape.presetGeom === "oval") br = "50%";
          else if (shape.presetGeom === "roundRect" && !br) br = "8%";
          const borderStyle = shape.stroke ? `${Math.max(0.5, shape.strokeWidth || 1)}px solid ${shape.stroke}` : undefined;

          // Table rendering
          if (shape.type === "table" && shape.tableRows) {
            const totalColW = shape.tableColWidths?.reduce((a, b) => a + b, 0) || 1;
            return (
              <div key={si} style={{
                position: "absolute", left: `${shape.x}%`, top: `${shape.y}%`,
                width: `${shape.w}%`, height: `${shape.h}%`, overflow: "auto",
              }}>
                <table style={{ width: "100%", height: "100%", borderCollapse: "collapse", fontSize: "clamp(8px, 1.2vw, 14px)", tableLayout: "fixed" }}>
                  {shape.tableColWidths && (
                    <colgroup>
                      {shape.tableColWidths.map((w, ci) => (
                        <col key={ci} style={{ width: `${(w / totalColW) * 100}%` }} />
                      ))}
                    </colgroup>
                  )}
                  <tbody>
                    {shape.tableRows.map((row, ri) => (
                      <tr key={ri}>
                        {row.map((cell, ci) => {
                          if ((cell as any).vMerge) return null; // skip merged continuation cells
                          return (
                            <td key={ci} colSpan={(cell as any).gridSpan || undefined} style={{
                              border: "1px solid rgba(28,25,23,0.06)", padding: "2px 4px",
                              background: cell.fill || (ri === 0 ? "#f5f5f4" : "white"),
                              color: cell.color || "#292524",
                              fontWeight: cell.bold || ri === 0 ? 700 : 400,
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", lineHeight: 1.3,
                            }}>{cell.text}</td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }

          // Line / connector rendering
          if (shape.type === "line") {
            const lineColor = shape.stroke || "#57534e";
            const lineWidth = Math.max(1, shape.strokeWidth || 1);
            return (
              <svg key={si} style={{
                position: "absolute", left: `${shape.x}%`, top: `${shape.y}%`,
                width: `${shape.w}%`, height: `${shape.h}%`,
                overflow: "visible", opacity: shape.opacity,
                transform: shape.rotation ? `rotate(${shape.rotation}deg)` : undefined,
              }}>
                <line x1="0" y1={shape.h > shape.w ? "0" : "50%"} x2="100%" y2={shape.h > shape.w ? "100%" : "50%"}
                  stroke={lineColor} strokeWidth={lineWidth} />
              </svg>
            );
          }

          // Clip-path for preset geometries
          const clipPath = _presetClipPath(shape.presetGeom);

          // Vertical alignment
          const vJustify = shape.vAlign === "top" ? "flex-start" : shape.vAlign === "bottom" ? "flex-end" : shape.texts.length > 0 ? "center" : undefined;

          // Text insets
          const pad = shape.padding;
          const padStyle = pad ? `${pad.t}px ${pad.r}px ${pad.b}px ${pad.l}px` : shape.texts.length > 0 ? "2% 3%" : undefined;

          // Build transform (rotation + flip)
          const transforms: string[] = [];
          if (shape.rotation) transforms.push(`rotate(${shape.rotation}deg)`);
          if (shape.flipH) transforms.push("scaleX(-1)");
          if (shape.flipV) transforms.push("scaleY(-1)");

          // Shadow
          let boxShadow: string | undefined;
          if (shape.shadow) {
            const s = shape.shadow;
            const rad = (s.angle * Math.PI) / 180;
            const dx = Math.round(s.dist * Math.cos(rad));
            const dy = Math.round(s.dist * Math.sin(rad));
            const r = parseInt(s.color.slice(1, 3), 16);
            const g = parseInt(s.color.slice(3, 5), 16);
            const bv = parseInt(s.color.slice(5, 7), 16);
            boxShadow = `${dx}px ${dy}px ${Math.round(s.blur)}px rgba(${r},${g},${bv},${s.alpha})`;
          }

          // Image cropping style
          let imgStyle: React.CSSProperties = {
            position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
            objectFit: "fill", borderRadius: br, zIndex: 0,
          };
          if (shape.imgCrop) {
            const c = shape.imgCrop;
            // Use object-position + object-fit to simulate cropping
            const scaleX = 100 / (100 - c.l - c.r);
            const scaleY = 100 / (100 - c.t - c.b);
            imgStyle = {
              position: "absolute",
              top: `-${c.t * scaleY}%`, left: `-${c.l * scaleX}%`,
              width: `${100 * scaleX}%`, height: `${100 * scaleY}%`,
              objectFit: "cover", borderRadius: br, zIndex: 0,
            };
          }

          // Numbered bullet counter
          let bulletNum = 0;
          if (shape.texts.some(t => t.bullet === "#.")) {
            let counter = 0;
            for (const t of shape.texts) {
              if (t.bullet === "#.") counter++;
            }
          }

          return (
            <div
              key={si}
              style={{
                position: "absolute",
                left: `${shape.x}%`,
                top: `${shape.y}%`,
                width: `${shape.w}%`,
                height: `${shape.h}%`,
                background: shape.gradFill ? gradientToCss(shape.gradFill) : shape.fill || undefined,
                borderRadius: br,
                border: borderStyle,
                opacity: shape.opacity,
                overflow: "hidden",
                display: "flex",
                flexDirection: "column",
                justifyContent: vJustify,
                padding: padStyle,
                transform: transforms.length > 0 ? transforms.join(" ") : undefined,
                boxSizing: "border-box",
                clipPath: clipPath,
                boxShadow,
              }}
            >
              {shape.imgUrl && <img src={shape.imgUrl} alt="" style={imgStyle} />}
              {(() => {
                let autoNumCounter = 0;
                return shape.texts.map((t, ti) => {
                  // Resolve auto-numbered bullets
                  let displayBullet = t.bullet;
                  if (t.bullet === "#.") {
                    autoNumCounter++;
                    displayBullet = `${autoNumCounter}.`;
                  } else if (t.bullet === "a.") {
                    autoNumCounter++;
                    displayBullet = `${String.fromCharCode(96 + autoNumCounter)}.`;
                  } else if (t.bullet === "i.") {
                    autoNumCounter++;
                    const romans = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"];
                    displayBullet = `${romans[autoNumCounter - 1] || autoNumCounter}.`;
                  } else if (!t.bullet) {
                    autoNumCounter = 0; // reset on non-bulleted paragraph
                  }
                  return (
                    <p
                      key={ti}
                      style={{
                        position: "relative",
                        margin: 0,
                        marginTop: t.spaceBefore ? `${t.spaceBefore}pt` : t.text === "" ? "0.3em" : "0.05em",
                        marginBottom: t.spaceAfter ? `${t.spaceAfter}pt` : "0.05em",
                        minHeight: t.text === "" ? "0.5em" : undefined,
                        paddingLeft: t.indent ? `${t.indent}px` : displayBullet ? "18px" : undefined,
                        fontSize: t.fontSize ? `${Math.max(8, t.fontSize * 0.85)}px` : "14px",
                        fontWeight: t.bold ? 700 : 400,
                        fontStyle: t.italic ? "italic" : undefined,
                        textDecoration: t.underline ? "underline" : undefined,
                        color: t.color || _viewerTheme.tx1 || "#000000",
                        textAlign: (t.align as any) || undefined,
                        lineHeight: t.lineSpacing || 1.35,
                        wordBreak: "break-word",
                        whiteSpace: "pre-wrap",
                        zIndex: 1,
                        fontFamily: t.fontFamily ? `"${t.fontFamily}", sans-serif` : (_viewerMinorFont ? `"${_viewerMinorFont}", sans-serif` : undefined),
                      }}
                    >
                      {displayBullet && <span style={{ position: "absolute", left: t.indent ? `${t.indent - 14}px` : "2px" }}>{displayBullet}</span>}
                      {t.runs ? t.runs.map((run, ri) => (
                        <span key={ri} style={{
                          fontWeight: run.bold ? 700 : undefined,
                          fontStyle: run.italic ? "italic" : undefined,
                          textDecoration: [run.underline ? "underline" : "", run.strikethrough ? "line-through" : ""].filter(Boolean).join(" ") || undefined,
                          fontSize: run.fontSize && run.fontSize !== t.fontSize ? `${Math.max(8, run.fontSize * 0.85)}px` : undefined,
                          color: run.color && run.color !== t.color ? run.color : undefined,
                          fontFamily: run.fontFamily && run.fontFamily !== t.fontFamily ? `"${run.fontFamily}", sans-serif` : undefined,
                          verticalAlign: run.baseline ? (run.baseline > 0 ? "super" : "sub") : undefined,
                          letterSpacing: run.spacing ? `${run.spacing}pt` : undefined,
                        }}>{run.text}</span>
                      )) : t.text}
                    </p>
                  );
                });
              })()}
            </div>
          );
        })}
      </div>

      {/* Thumbnail strip */}
      {slides.length > 1 && (
        <div style={{
          display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap",
          marginTop: 16, padding: "0 8px",
        }}>
          {slides.map((s, i) => {
            const thumbBg: React.CSSProperties = { backgroundColor: s.bg || "#ffffff" };
            if (s.bgGrad) thumbBg.backgroundImage = gradientToCss(s.bgGrad);
            if (s.bgImgUrl) { thumbBg.backgroundImage = `url(${s.bgImgUrl})`; thumbBg.backgroundSize = "cover"; thumbBg.backgroundRepeat = "no-repeat"; }
            return (
              <button
                key={i}
                onClick={() => setActiveSlide(i)}
                style={{
                  width: 96, height: 54, borderRadius: 6,
                  border: i === activeSlide ? "2px solid #4f7d75" : "1px solid #e7e5e4",
                  boxShadow: i === activeSlide ? "0 0 0 3px rgba(79,125,117,0.15)" : "none",
                  cursor: "pointer", overflow: "hidden", position: "relative",
                  flexShrink: 0, transition: "all 0.15s",
                  ...thumbBg,
                }}
                title={`Slide ${i + 1}`}
              >
                <span style={{
                  position: "absolute", bottom: 2, right: 4, fontSize: 9,
                  fontWeight: 700, color: "rgba(0,0,0,0.5)", background: "rgba(255,255,255,0.7)",
                  borderRadius: 3, padding: "0 3px",
                }}>{i + 1}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* Nav + info */}
      {slides.length > 1 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 12, marginTop: 12 }}>
          <button
            onClick={() => setActiveSlide(Math.max(0, activeSlide - 1))}
            disabled={activeSlide === 0}
            style={{
              width: 36, height: 36, borderRadius: 10,
              background: activeSlide === 0 ? "#f5f5f4" : "white",
              border: "1px solid rgba(28,25,23,0.06)",
              cursor: activeSlide === 0 ? "default" : "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: activeSlide === 0 ? "#d6d3d1" : "#57534e",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d="M15 18l-6-6 6-6" /></svg>
          </button>
          <span style={{ fontSize: 13, fontWeight: 600, color: "#78716c" }}>
            {t("page.file_viewer.slide")} {activeSlide + 1} {t("page.file_viewer.of")} {slides.length}
          </span>
          <button
            onClick={() => setActiveSlide(Math.min(slides.length - 1, activeSlide + 1))}
            disabled={activeSlide === slides.length - 1}
            style={{
              width: 36, height: 36, borderRadius: 10,
              background: activeSlide === slides.length - 1 ? "#f5f5f4" : "white",
              border: "1px solid rgba(28,25,23,0.06)",
              cursor: activeSlide === slides.length - 1 ? "default" : "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: activeSlide === slides.length - 1 ? "#d6d3d1" : "#57534e",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path d="M9 18l6-6-6-6" /></svg>
          </button>
        </div>
      )}
    </div>
  );
}

// ── Details drawer helper ──
function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
      <span style={{ fontSize: 11, color: "#a8a29e", flexShrink: 0 }}>{label}</span>
      <div style={{ textAlign: "right", minWidth: 0 }}>{children}</div>
    </div>
  );
}

// ── Adapters: backend types -> ShareDialog component-local types ──
//
// ShareDialog uses a UI-friendly InternalGrant shape (role enum, email-keyed)
// while the backend stores ResourceGrant rows with capability arrays + opaque
// subject_id. Map between the two here so the dialog stays UI-agnostic.

type ShareRole = "viewer" | "commenter" | "editor" | "curator";

function _capabilitiesToRole(caps: string[]): ShareRole {
  const set = new Set(caps);
  if (set.has("manage_metadata") || set.has("grant_access")) return "curator";
  if (set.has("edit")) return "editor";
  if (set.has("comment")) return "commenter";
  return "viewer";
}

function _roleToCapabilities(role: ShareRole): string[] {
  switch (role) {
    case "viewer":    return ["view"];
    case "commenter": return ["view", "comment"];
    case "editor":    return ["view", "comment", "edit"];
    case "curator":   return ["view", "comment", "edit", "manage_metadata", "grant_access", "share_internal"];
  }
}

function _grantToInternalGrant(
  g: DocumentGrant,
  userById: Map<string, UserSummary>,
) {
  let user_email = g.subject_email || g.subject_id;
  let user_name: string | undefined = g.subject_display_name || undefined;
  let avatar_url: string | undefined = g.subject_avatar_url || undefined;
  if (g.subject_type === "user") {
    const u = userById.get(g.subject_user_id || g.subject_id);
    if (u) {
      user_email = g.subject_email || u.email;
      user_name = g.subject_display_name || u.display_name;
      avatar_url = g.subject_avatar_url || u.avatar_url;
    }
  } else {
    user_email = g.subject_display_name || g.subject_email || `${g.subject_type}: ${g.subject_id}`;
  }
  return {
    id: g.id,
    user_email,
    user_name,
    avatar_url,
    role: _capabilitiesToRole(g.capabilities),
    expires_at: g.expires_at,
    source: "explicit" as const,
  };
}

function _shareToExternalShare(s: DocumentShare) {
  return {
    id: s.id,
    audience: s.audience || "anonymous",
    capabilities: s.capabilities,
    expires_at: s.expires_at,
    watermark: s.watermark,
    require_otp: s.require_otp,
    use_count: s.use_count,
    last_used_at: s.last_used_at,
  };
}

/** Derive a likely entity domain from the current user's email for the
 *  GDrive-style "Anyone @yourcompany.com" option. Best-effort: when the
 *  user's email is a personal address (gmail/outlook/etc.), returns undefined
 *  so the option is hidden. A future commit can replace this with an
 *  explicit Entity.primary_domain field surfaced from the backend.
 */
function _entityDomain(email: string | undefined): string | undefined {
  if (!email) return undefined;
  const at = email.indexOf("@");
  if (at <= 0) return undefined;
  const domain = email.slice(at + 1).trim().toLowerCase();
  const personal = new Set([
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "icloud.com", "qq.com", "163.com", "126.com", "foxmail.com",
    "protonmail.com", "test.com",
  ]);
  if (!domain || personal.has(domain)) return undefined;
  return domain;
}

// ── Access log panel (drawer) ──
//
// Owner self-service per RFC §13.8. Uses 403 to silently hide for non-owners
// (the API enforces; here we just drop the section so the drawer doesn't
// look broken).
function AccessLogSection({ docId }: { docId: string }) {
  const q = useQuery({
    queryKey: ["doc-access-log", docId],
    queryFn: () => api.docPermissions.accessLog(docId, 10),
    retry: false, // 403 = "not your doc"; don't keep retrying
  });
  if (q.isError) return null;
  const rows = q.data || [];
  return (
    <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 12 }}>
      <p style={{ fontSize: 11, fontWeight: 600, color: "#57534e", margin: "0 0 8px" }}>
        {t("page.file_viewer.access_log.title")}
      </p>
      {q.isLoading ? (
        <p style={{ fontSize: 11, color: "#a8a29e", textAlign: "center", margin: 0 }}>
          {t("page.file_viewer.access_log.loading")}
        </p>
      ) : rows.length === 0 ? (
        <p style={{ fontSize: 11, color: "#d6d3d1", textAlign: "center", margin: 0 }}>
          {t("page.file_viewer.access_log.empty")}
        </p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 4 }}>
          {rows.map((r, i) => (
            <li
              key={`${r.ts}-${i}`}
              style={{
                fontSize: 11, color: "#57534e",
                padding: "4px 6px", borderRadius: 4,
                background: "rgba(245,245,244,0.5)",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 6 }}>
                <span style={{ fontWeight: 600 }}>{r.action}</span>
                <span style={{ color: "#a8a29e" }}>
                  {new Date(r.ts).toLocaleString(undefined, {
                    month: "short", day: "numeric",
                    hour: "2-digit", minute: "2-digit",
                  })}
                </span>
              </div>
              <div style={{ color: "#a8a29e" }}>
                {r.actor_type}: {r.actor_id || "—"}
                {r.share_id && ` · ${t("page.file_viewer.access_log.via_share")}`}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function MarkdownViewer({
  content,
  commentRanges = [],
  commentAnchors = [],
  activeCommentId,
  onSelectCommentId,
}: {
  content: string;
  commentRanges?: CommentTextRange[];
  commentAnchors?: Comment[];
  activeCommentId?: string | null;
  onSelectCommentId?: (commentId: string) => void;
}) {
  const sourceLike = useMemo(() => looksLikeScriptMarkdown(content), [content]);
  const previewRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (sourceLike) return;
    const root = previewRef.current;
    if (!root) return;
    unwrapDocumentCommentMarks(root);
    markQuoteCommentsInElement(root, quoteAnchoredComments(commentAnchors), activeCommentId);
  }, [activeCommentId, commentAnchors, content, sourceLike]);

  if (sourceLike) {
    return (
      <div className="markdown-viewer-stage">
        <article className="md-preview markdown-viewer-page markdown-viewer-page--source">
          <pre className="md-code-block markdown-viewer-source-code">
            <code>
              {renderCommentMarkedText(content, commentRanges, activeCommentId || null, onSelectCommentId || (() => {}))}
            </code>
          </pre>
        </article>
      </div>
    );
  }

  return (
    <div className="markdown-viewer-stage">
      <article
        ref={previewRef}
        className="md-preview markdown-viewer-page"
        onClick={(event) => {
          const target = event.target instanceof HTMLElement
            ? event.target.closest<HTMLElement>(".document-comment-mark")
            : null;
          const commentId = target?.dataset.commentId;
          if (commentId) onSelectCommentId?.(commentId);
        }}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkBreaks]}
          components={{
            a({ href, children, ...props }: any) {
              return (
                <a {...props} href={href} target="_blank" rel="noopener noreferrer" className="md-link">
                  {children}
                </a>
              );
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
            img({ alt, ...props }: any) {
              return <img {...props} alt={alt || ""} className="md-image" loading="lazy" />;
            },
            input({ ...props }: any) {
              return <input {...props} disabled className="md-task-checkbox" />;
            },
          }}
        >
          {content}
        </ReactMarkdown>
      </article>
    </div>
  );
}

function TextViewer({
  content,
  commentRanges = [],
  activeCommentId,
  onSelectCommentId,
}: {
  content: string;
  commentRanges?: CommentTextRange[];
  activeCommentId?: string | null;
  onSelectCommentId?: (commentId: string) => void;
}) {
  return (
    <div className="text-viewer-stage">
      <article className="text-viewer-page">
        <pre className="text-viewer-pre">
          {renderCommentMarkedText(content, commentRanges, activeCommentId || null, onSelectCommentId || (() => {}))}
        </pre>
      </article>
    </div>
  );
}

function ImageEditor({
  url,
  docId,
  docName,
  mimeType,
  onSaved,
  onLiveEditBridgeChange,
  canEdit = true,
}: {
  url: string;
  docId?: string;
  docName?: string;
  mimeType?: string | null;
  onSaved: (savedDoc: Document, savedBlob: Blob) => void;
  onLiveEditBridgeChange?: (bridge: ImageLiveEditBridge | null) => void;
  canEdit?: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const generatedPreviewUrlRef = useRef<string | null>(null);
  const drawingRef = useRef(false);
  const [imageReady, setImageReady] = useState(false);
  const [sourceUrl, setSourceUrl] = useState(url);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(100);
  const canvasViewportRef = useRef<HTMLDivElement | null>(null);
  const [rotation, setRotation] = useState(0);
  const [flipX, setFlipX] = useState(false);
  const [flipY, setFlipY] = useState(false);
  const [brightness, setBrightness] = useState(100);
  const [contrast, setContrast] = useState(100);
  const [saturation, setSaturation] = useState(100);
  const [hue, setHue] = useState(0);
  const [tool, setTool] = useState<"select" | "draw">("select");
  const [brushColor, setBrushColor] = useState("#0f8f84");
  const [brushSize, setBrushSize] = useState(6);
  const [strokes, setStrokes] = useState<ImageStroke[]>([]);
  const [activeStroke, setActiveStroke] = useState<ImageStroke | null>(null);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [openMenu, setOpenMenu] = useState<"adjust" | "transform" | "brush" | null>(null);

  const outputType = useMemo(() => imageExportType(docName, mimeType), [docName, mimeType]);
  const revokeGeneratedPreviewUrl = useCallback(() => {
    if (generatedPreviewUrlRef.current) {
      URL.revokeObjectURL(generatedPreviewUrlRef.current);
      generatedPreviewUrlRef.current = null;
    }
  }, []);
  const imageStateRef = useRef({
    rotation,
    flipX,
    flipY,
    brightness,
    contrast,
    saturation,
    hue,
    brushColor,
    brushSize,
    strokes,
  });

  useEffect(() => {
    imageStateRef.current = {
      rotation,
      flipX,
      flipY,
      brightness,
      contrast,
      saturation,
      hue,
      brushColor,
      brushSize,
      strokes,
    };
  }, [brightness, brushColor, brushSize, contrast, flipX, flipY, hue, rotation, saturation, strokes]);

  useEffect(() => {
    revokeGeneratedPreviewUrl();
    setSourceUrl(url);
    setOpenMenu(null);
  }, [revokeGeneratedPreviewUrl, url]);

  useEffect(() => revokeGeneratedPreviewUrl, [revokeGeneratedPreviewUrl]);

  useEffect(() => {
    const element = canvasViewportRef.current;
    if (!element) return undefined;
    const update = () => {
      setViewportSize((size) => {
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
    let cancelled = false;
    const img = new Image();
    img.onload = () => {
      if (cancelled) return;
      imageRef.current = img;
      setImageReady(true);
      setStatus((current) => current.startsWith("AI generated image") ? current : "");
    };
    img.onerror = () => {
      if (!cancelled) setStatus("Image could not be loaded.");
    };
    img.src = sourceUrl;
    return () => {
      cancelled = true;
      imageRef.current = null;
      setImageReady(false);
    };
  }, [sourceUrl]);

  const renderCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    const image = imageRef.current;
    if (!canvas || !image) return;

    const normalizedRotation = ((rotation % 360) + 360) % 360;
    const swapSize = normalizedRotation === 90 || normalizedRotation === 270;
    const width = swapSize ? image.naturalHeight : image.naturalWidth;
    const height = swapSize ? image.naturalWidth : image.naturalHeight;
    const pixelRatio = window.devicePixelRatio || 1;

    canvas.width = Math.max(1, Math.round(width * pixelRatio));
    canvas.height = Math.max(1, Math.round(height * pixelRatio));
    setCanvasSize((size) => (
      size.width === width && size.height === height ? size : { width, height }
    ));

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.translate(width / 2, height / 2);
    ctx.rotate((normalizedRotation * Math.PI) / 180);
    ctx.scale(flipX ? -1 : 1, flipY ? -1 : 1);
    ctx.filter = `brightness(${brightness}%) contrast(${contrast}%) saturate(${saturation}%) hue-rotate(${hue}deg)`;
    ctx.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2);
    ctx.restore();
    ctx.filter = "none";

    for (const stroke of [...strokes, ...(activeStroke ? [activeStroke] : [])]) {
      if (stroke.points.length < 2) continue;
      ctx.save();
      ctx.strokeStyle = stroke.color;
      ctx.lineWidth = stroke.size;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.beginPath();
      stroke.points.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.restore();
    }
  }, [activeStroke, brightness, contrast, flipX, flipY, hue, rotation, saturation, strokes]);

  useEffect(() => {
    if (imageReady) renderCanvas();
  }, [imageReady, renderCanvas]);

  const canvasPoint = useCallback((event: any) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / rect.width) * (canvas.width / (window.devicePixelRatio || 1)),
      y: ((event.clientY - rect.top) / rect.height) * (canvas.height / (window.devicePixelRatio || 1)),
    };
  }, []);

  const handlePointerDown = (event: any) => {
    if (!canEdit || tool !== "draw" || !imageReady) return;
    event.preventDefault();
    const point = canvasPoint(event);
    const stroke = {
      id: `stroke-${Date.now()}`,
      color: brushColor,
      size: brushSize,
      points: [point],
    };
    drawingRef.current = true;
    setActiveStroke(stroke);
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const handlePointerMove = (event: any) => {
    if (!canEdit || !drawingRef.current || tool !== "draw") return;
    const point = canvasPoint(event);
    setActiveStroke((stroke) => stroke ? { ...stroke, points: [...stroke.points, point] } : stroke);
  };

  const finishStroke = (event: any) => {
    if (!drawingRef.current) return;
    drawingRef.current = false;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    if (activeStroke && activeStroke.points.length > 1) {
      setStrokes((items) => [...items, activeStroke]);
    }
    setActiveStroke(null);
  };

  const resetEdits = () => {
    setRotation(0);
    setFlipX(false);
    setFlipY(false);
    setBrightness(100);
    setContrast(100);
    setSaturation(100);
    setHue(0);
    setStrokes([]);
    setActiveStroke(null);
    setStatus("");
  };

  const canvasToBlob = useCallback(() => new Promise<Blob>((resolve, reject) => {
    const canvas = canvasRef.current;
    if (!canvas) {
      reject(new Error("Canvas is not ready"));
      return;
    }
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("Image export failed"));
    }, outputType.mime, 0.94);
  }), [outputType.mime]);

  const buildImageLiveEditContent = useCallback(() => {
    return JSON.stringify({
      format: "manor-image-edit-v1",
      documentName: docName || "image",
      image: {
        width: imageRef.current?.naturalWidth || canvasSize.width || 0,
        height: imageRef.current?.naturalHeight || canvasSize.height || 0,
      },
      capabilities: {
        nonDestructiveAdjustments: true,
        imageRegenerationFromAttachedCurrentImage: true,
      },
      edits: imageStateRef.current,
    }, null, 2);
  }, [canvasSize.height, canvasSize.width, docName]);

  const applyImageEditState = useCallback((state: ReturnType<typeof normalizeImageLiveEditState>) => {
    setRotation(state.rotation);
    setFlipX(state.flipX);
    setFlipY(state.flipY);
    setBrightness(state.brightness);
    setContrast(state.contrast);
    setSaturation(state.saturation);
    setHue(state.hue);
    setBrushColor(state.brushColor);
    setBrushSize(state.brushSize);
    setStrokes(state.strokes);
    setActiveStroke(null);
    setTool("select");
  }, []);

  const localImageEditContent = useCallback((userRequest: string, currentContent: string) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(currentContent);
    } catch {
      return null;
    }
    const currentState = normalizeImageLiveEditState(parsed);
    const nextState = { ...currentState, strokes: [...currentState.strokes] };
    const request = userRequest.toLowerCase();
    let changed = false;
    const setField = <K extends keyof typeof nextState>(key: K, value: (typeof nextState)[K]) => {
      if (JSON.stringify(nextState[key]) === JSON.stringify(value)) return;
      nextState[key] = value;
      changed = true;
    };

    if (/reset|restore|original|清除|还原|恢复|重置/.test(request)) {
      setField("rotation", 0);
      setField("flipX", false);
      setField("flipY", false);
      setField("brightness", 100);
      setField("contrast", 100);
      setField("saturation", 100);
      setField("strokes", []);
    }
    if (/left|counterclockwise|逆时针|向左/.test(request)) setField("rotation", (nextState.rotation + 270) % 360);
    else if (/rotate|right|clockwise|旋转|顺时针|向右/.test(request)) setField("rotation", (nextState.rotation + 90) % 360);
    if (/horizontal|flip h|左右|水平/.test(request)) setField("flipX", !nextState.flipX);
    if (/vertical|flip v|上下|垂直/.test(request)) setField("flipY", !nextState.flipY);
    if (/black.?white|grayscale|grey|gray|黑白|灰度/.test(request)) setField("saturation", 0);
    if (/bright|lighter|曝光|变亮|亮一点|更亮/.test(request)) setField("brightness", Math.min(180, nextState.brightness + 18));
    if (/dark|darker|变暗|暗一点|更暗/.test(request)) setField("brightness", Math.max(40, nextState.brightness - 18));
    if (/contrast|对比/.test(request)) setField("contrast", Math.min(180, nextState.contrast + 18));
    if (/saturat|colorful|vivid|鲜艳|饱和/.test(request)) setField("saturation", Math.min(200, nextState.saturation + 24));
    if (/warm|warmer|暖|偏黄|yellow/.test(request)) setField("hue", Math.min(180, nextState.hue + 16));
    if (/cool|cooler|冷|偏蓝|blue/.test(request)) setField("hue", Math.max(-180, nextState.hue - 16));
    if (/color|颜色|色彩|调色/.test(request) && !/black.?white|grayscale|grey|gray|黑白|灰度/.test(request)) {
      setField("saturation", Math.min(200, nextState.saturation + 20));
      setField("hue", nextState.hue === 0 ? 10 : nextState.hue);
    }
    if (/enhance|improve|polish|better|美化|优化|好看|清晰|质感/.test(request)) {
      setField("brightness", Math.max(nextState.brightness, 108));
      setField("contrast", Math.max(nextState.contrast, 116));
      setField("saturation", Math.max(nextState.saturation, 112));
    }
    if (/remove.*draw|clear.*draw|erase.*draw|清除涂鸦|删除涂鸦|去掉涂鸦/.test(request)) {
      setField("strokes", []);
    }

    if (!changed) return null;
    return JSON.stringify({
      format: "manor-image-edit-v1",
      documentName: docName || "image",
      edits: nextState,
    }, null, 2);
  }, [docName]);

  const applyGeneratedImagePreview = useCallback(async (imageUrl: string, meta: EditorLiveApplyMeta) => {
    if (!meta.complete || !imageUrl) return;
    setStatus("Loading AI generated image...");
    const objectUrl = await imageUrlToObjectUrl(imageUrl);
    revokeGeneratedPreviewUrl();
    if (objectUrl.startsWith("blob:")) generatedPreviewUrlRef.current = objectUrl;
    setSourceUrl(objectUrl);
    applyImageEditState(normalizeImageLiveEditState({}));
    setStatus("AI generated image applied. Review it, then Save image to write the changes.");
  }, [applyImageEditState, revokeGeneratedPreviewUrl]);

  const getImageLiveEditAttachmentFiles = useCallback(async () => {
    const blob = await canvasToBlob();
    const extension = outputType.extension || "png";
    return [
      new File(
        [blob],
        `current-editor-image.${extension}`,
        { type: outputType.mime },
      ),
    ];
  }, [canvasToBlob, outputType.extension, outputType.mime]);

  const applyImageLiveEditContent = useCallback((next: string, meta: EditorLiveApplyMeta) => {
    if (!meta.complete) return;
    try {
      const parsed = JSON.parse(next);
      const replacementImageUrl = extractReplacementImageUrl(parsed);
      if (replacementImageUrl) {
        void applyGeneratedImagePreview(replacementImageUrl, meta);
        return;
      }
      const nextState = normalizeImageLiveEditState(parsed);
      applyImageEditState(nextState);
      setStatus("AI edit applied. Review it, then Save image to write the changes.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "AI edit returned invalid image edits.");
    }
  }, [applyGeneratedImagePreview, applyImageEditState]);

  useEffect(() => {
    if (!onLiveEditBridgeChange || !canEdit || !imageReady) return undefined;
    onLiveEditBridgeChange({
      getContent: buildImageLiveEditContent,
      applyContent: applyImageLiveEditContent,
      localEditContent: localImageEditContent,
      getAttachmentFiles: getImageLiveEditAttachmentFiles,
      applyGeneratedImage: applyGeneratedImagePreview,
      supportsImageGeneration: true,
    });
    return () => onLiveEditBridgeChange(null);
  }, [
    applyGeneratedImagePreview,
    applyImageLiveEditContent,
    buildImageLiveEditContent,
    canEdit,
    getImageLiveEditAttachmentFiles,
    imageReady,
    localImageEditContent,
    onLiveEditBridgeChange,
  ]);

  const downloadEditedImage = async () => {
    try {
      const blob = await canvasToBlob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = imageEditFileName(docName, outputType.extension);
      a.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (err: any) {
      setStatus(err?.message || "Download failed");
    }
  };

  const saveEditedImage = async () => {
    if (!docId || !canEdit) return;
    setSaving(true);
    setStatus("Saving image...");
    try {
      const blob = await canvasToBlob();
      const file = new File([blob], imageEditFileName(docName, outputType.extension), { type: outputType.mime });
      const savedDoc = await api.documents.replaceFile(docId, file);
      onSaved(savedDoc, blob);
      resetEdits();
      setStatus("Saved to current image.");
    } catch (err: any) {
      setStatus(err?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const renderMenu = (
    key: "adjust" | "transform" | "brush",
    label: string,
    content: ReactNode,
  ) => (
    <div className="image-editor-toolbar-menu">
      <button
        type="button"
        className={editorToolButtonClass({ active: openMenu === key })}
        onClick={() => setOpenMenu((current) => current === key ? null : key)}
      >
        {label}
      </button>
      {openMenu === key && (
        <div className="image-editor-menu-panel">
          {content}
        </div>
      )}
    </div>
  );

  const imageFitScale = useMemo(() => {
    if (!canvasSize.width || !canvasSize.height || !viewportSize.width || !viewportSize.height) return 1;
    const horizontalPadding = 56;
    const verticalPadding = 56;
    const fit = Math.min(
      (viewportSize.width - horizontalPadding) / canvasSize.width,
      (viewportSize.height - verticalPadding) / canvasSize.height,
      1,
    );
    return Number.isFinite(fit) && fit > 0 ? fit : 1;
  }, [canvasSize.height, canvasSize.width, viewportSize.height, viewportSize.width]);

  const displayScale = imageFitScale * (zoom / 100);

  return (
    <div className="manor-editor-media-stage image-editor-stage">
      <div className="manor-editor-toolbar image-editor-toolbar">
        {canEdit && (
          <>
            <button
              type="button"
              className={editorToolButtonClass({ active: tool === "select" })}
              onClick={() => setTool("select")}
            >
              <IconEdit size={15} /> Select
            </button>
            <button
              type="button"
              className={editorToolButtonClass({ active: tool === "draw" })}
              onClick={() => setTool("draw")}
            >
              <IconPenLine size={15} /> Draw
            </button>
            <span className="manor-editor-toolbar-divider" />
          </>
        )}
        <button
          type="button"
          className={editorToolButtonClass({ icon: true })}
          onClick={() => setZoom((value) => Math.max(25, value - 25))}
          title="Zoom out"
        >
          <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 12h-15" /></svg>
        </button>
        <span className="manor-editor-toolbar-muted" style={{ width: 52, textAlign: "center" }}>{zoom}%</span>
        <button
          type="button"
          className={editorToolButtonClass({ icon: true })}
          onClick={() => setZoom((value) => Math.min(400, value + 25))}
          title="Zoom in"
        >
          <IconPlus size={16} />
        </button>
        {canEdit && (
          <>
            <span className="manor-editor-toolbar-divider" />
            {renderMenu("transform", "Transform", (
              <>
                <button type="button" className={editorToolButtonClass()} onClick={() => setRotation((value) => value - 90)}>
                  <IconUndo size={14} /> Rotate left
                </button>
                <button type="button" className={editorToolButtonClass()} onClick={() => setRotation((value) => value + 90)}>
                  <IconRedo size={14} /> Rotate right
                </button>
                <button type="button" className={editorToolButtonClass()} onClick={() => setFlipX((value) => !value)}>Flip H</button>
                <button type="button" className={editorToolButtonClass()} onClick={() => setFlipY((value) => !value)}>Flip V</button>
                <button type="button" className={editorToolButtonClass()} onClick={resetEdits}>
                  <IconRefresh size={14} /> Reset
                </button>
              </>
            ))}
            {renderMenu("adjust", "Adjust", (
              <>
                <label className="manor-editor-toolbar-label image-editor-slider-label">
                  Brightness
                  <input className="manor-editor-range" type="range" min={40} max={180} value={brightness} onChange={(event) => setBrightness(Number(event.target.value))} />
                </label>
                <label className="manor-editor-toolbar-label image-editor-slider-label">
                  Contrast
                  <input className="manor-editor-range" type="range" min={40} max={180} value={contrast} onChange={(event) => setContrast(Number(event.target.value))} />
                </label>
                <label className="manor-editor-toolbar-label image-editor-slider-label">
                  Saturation
                  <input className="manor-editor-range" type="range" min={0} max={220} value={saturation} onChange={(event) => setSaturation(Number(event.target.value))} />
                </label>
                <label className="manor-editor-toolbar-label image-editor-slider-label">
                  Hue
                  <input className="manor-editor-range" type="range" min={-180} max={180} value={hue} onChange={(event) => setHue(Number(event.target.value))} />
                </label>
              </>
            ))}
            {renderMenu("brush", "Brush", (
              <>
                <label className="manor-editor-toolbar-label">
                  Color
                  <input type="color" value={brushColor} onChange={(event) => setBrushColor(event.target.value)} className="image-editor-color-input" />
                </label>
                <label className="manor-editor-toolbar-label image-editor-slider-label">
                  Size
                  <input className="manor-editor-range" type="range" min={1} max={28} value={brushSize} onChange={(event) => setBrushSize(Number(event.target.value))} />
                </label>
                <button type="button" className={editorToolButtonClass()} onClick={() => setStrokes((items) => items.slice(0, -1))} disabled={strokes.length === 0}>
                  <IconUndo size={14} /> Undo draw
                </button>
              </>
            ))}
          </>
        )}
        <span className="manor-editor-toolbar-divider" />
        <button
          type="button"
          className={editorToolButtonClass()}
          onClick={downloadEditedImage}
          disabled={!imageReady}
        >
          <IconDownload size={15} /> Download edited
        </button>
        {canEdit && (
          <button
            type="button"
            className={editorToolButtonClass({ primary: true })}
            onClick={saveEditedImage}
            disabled={!imageReady || saving}
          >
            <IconCheck size={15} /> {saving ? "Saving" : "Save image"}
          </button>
        )}
        {status && (
          <span className="image-editor-toolbar-status" style={{ color: status.toLowerCase().includes("fail") || status.toLowerCase().includes("could not") ? "#c14a44" : "#436b65" }}>
            {status}
          </span>
        )}
      </div>

      <div ref={canvasViewportRef} className="image-editor-canvas-viewport">
        <canvas
          ref={canvasRef}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={finishStroke}
          onPointerCancel={finishStroke}
          style={{
            display: "block",
            margin: "0 auto",
            width: imageReady ? `${canvasSize.width * displayScale}px` : undefined,
            height: imageReady ? `${canvasSize.height * displayScale}px` : undefined,
            maxWidth: "none",
            background: "#fff",
            borderRadius: 10,
            boxShadow: "0 20px 45px rgba(28, 25, 23, 0.16)",
            cursor: canEdit && tool === "draw" ? "crosshair" : "default",
            touchAction: canEdit && tool === "draw" ? "none" : "auto",
          }}
        />
      </div>
    </div>
  );
}

// ── Main component ──
export default function FileViewer() {
  const { docId } = useParams<{ docId: string }>();
  const navigate = useNavigate();
  const currentUser = useAuthStore((s) => s.user);
  const location = useLocation();
  const taskOutputPreview = useMemo(() => getTaskOutputPreview(location.state), [location.state]);
  const viewerSurfaceRef = useRef<HTMLDivElement | null>(null);

  const queryClient = useQueryClient();
  const [doc, setDoc] = useState<Document | null>(null);
  const [content, setContent] = useState<string>("");
  const [downloadUrl, setDownloadUrl] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [commentAnchor, setCommentAnchor] = useState<CommentAnchor | null>(null);
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null);
  const [shareDialogOpen, setShareDialogOpen] = useState(false);
  const [pdfLiveEditBridge, setPdfLiveEditBridge] = useState<PdfLiveEditBridge | null>(null);
  const [imageLiveEditBridge, setImageLiveEditBridge] = useState<ImageLiveEditBridge | null>(null);
  const canEditCurrentDoc = canEditDocument(currentUser, doc);
  const canCommentCurrentDoc = canCommentDocument(currentUser, doc);
  const canShareCurrentDoc = canShareDocument(currentUser, doc);

  // Live grants + shares — only fetched when the dialog opens (saves a
  // round-trip on most viewer loads).
  const grantsQuery = useQuery({
    queryKey: ["doc-grants", docId],
    queryFn: () => api.docPermissions.listGrants(docId!),
    enabled: !!docId && shareDialogOpen && canShareCurrentDoc,
  });
  const sharesQuery = useQuery({
    queryKey: ["doc-shares", docId],
    queryFn: () => api.docPermissions.listShares(docId!),
    enabled: !!docId && shareDialogOpen && canShareCurrentDoc,
  });

  // Resolve grant subject_id (user uuid) -> email/display_name for the
  // dialog UI. Single batch request, gated by the grants query so it only
  // fires when there's actually something to look up.
  const grantUserIds = useMemo(() => {
    return Array.from(
      new Set(
        (grantsQuery.data || [])
          .filter((g) => g.subject_type === "user")
          .map((g) => g.subject_user_id || g.subject_id)
          .filter((id): id is string => Boolean(id)),
      ),
    );
  }, [grantsQuery.data]);

  const grantUsersQuery = useQuery({
    queryKey: ["users-batch", grantUserIds.sort().join(",")],
    queryFn: () => api.users.batchByIds(grantUserIds),
    enabled: grantUserIds.length > 0,
  });

  const userById = useMemo(() => {
    const map = new Map<string, UserSummary>();
    for (const u of grantUsersQuery.data || []) map.set(u.id, u);
    return map;
  }, [grantUsersQuery.data]);

  const category = doc ? detectCategory(doc) : "unsupported";
  const knowledgeReturnTo = getKnowledgeReturnTo(location.state);
  const isTaskOutputPreview = doc?.source === "task_output_preview";
  const { data: viewerComments = [] } = useQuery<Comment[]>({
    queryKey: ["comments", "document", doc?.id],
    queryFn: () => api.comments.list("document", doc!.id),
    enabled: !!doc?.id && !isTaskOutputPreview,
    retry: false,
  });

  useEffect(() => {
    if (category !== "pdf") setPdfLiveEditBridge(null);
    if (category !== "image") setImageLiveEditBridge(null);
  }, [category, docId]);

  const goBack = useCallback(() => {
    navigate(knowledgeReturnTo || "/knowledge");
  }, [knowledgeReturnTo, navigate]);

  // ── Permission-v1: derive flags from doc metadata ──────────────────────
  const requiresWatermark =
    doc?.classification === "confidential" || doc?.classification === "restricted";
  const watermarkDensity: "normal" | "dense" =
    doc?.classification === "restricted" ? "dense" : "normal";
  const restrictDownload =
    doc?.classification === "restricted" ||
    (doc?.quarantine_status && doc.quarantine_status !== "clean");
  const bannerReason: "legal_hold" | "quarantine" | "pii" | null =
    doc?.legal_hold
      ? "legal_hold"
      : doc?.quarantine_status && doc.quarantine_status !== "clean"
        ? "quarantine"
        : doc?.pii_detected && doc?.classification === "confidential"
          ? "pii"
          : null;

  const fetchDoc = useCallback(async () => {
    if (!docId) return;
    setLoading(true);
    setError("");
    try {
      const meta = await api.documents.get(docId);
      setDoc(meta);

      const cat = detectCategory(meta);
      if (["text", "markdown", "code", "html", "csv", "json"].includes(cat)) {
        const res = await api.documents.getContent(docId);
        setContent(typeof res === "string" ? res : res.content);
      }

      if (["image", "video", "audio", "pdf", "docx", "xlsx", "pptx"].includes(cat)) {
        // For video/audio, prefer streaming URL (avoids downloading entire file into memory)
        if (["video", "audio"].includes(cat)) {
          const streamUrl = api.documents.streamUrl(meta);
          if (streamUrl) {
            setDownloadUrl(streamUrl);
          } else {
            const url = await api.documents.download(docId);
            setDownloadUrl(url);
          }
        } else {
          const url = await api.documents.download(docId);
          setDownloadUrl(url);
        }
      }
    } catch (err: any) {
      if (taskOutputPreview) {
        setDoc(taskOutputPreviewDocument(docId, taskOutputPreview));
        setContent(taskOutputPreview.content);
        setDownloadUrl("");
        setError("");
        return;
      }
      setError(err.message || "Failed to load document");
    } finally {
      setLoading(false);
    }
  }, [docId, taskOutputPreview]);

  useEffect(() => { fetchDoc(); }, [fetchDoc]);

  useEffect(() => {
    setCommentAnchor(documentCommentAnchor(doc));
    setActiveCommentId(null);
  }, [doc?.id, doc?.name]);

  const viewerAnchoredComments = useMemo(
    () => rootAnchoredComments(viewerComments),
    [viewerComments],
  );

  const viewerCommentRanges = useMemo(
    () => commentRangesForContent(viewerAnchoredComments, category, content),
    [category, content, viewerAnchoredComments],
  );

  const scrollToCommentMark = useCallback((commentId: string) => {
    const surface = viewerSurfaceRef.current;
    if (!surface) return;
    const mark = Array.from(surface.querySelectorAll<HTMLElement>(".document-comment-mark"))
      .find((element) => element.dataset.commentId === commentId);
    mark?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, []);

  const handleSelectViewerCommentId = useCallback((commentId: string) => {
    setActiveCommentId(commentId);
    setCommentsOpen(true);
    setDetailsOpen(false);
    window.requestAnimationFrame(() => scrollToCommentMark(commentId));
  }, [scrollToCommentMark]);

  const handleSelectViewerComment = useCallback((comment: Comment) => {
    handleSelectViewerCommentId(comment.id);
  }, [handleSelectViewerCommentId]);

  const handlePdfSaved = useCallback((savedDoc: Document, savedBlob: Blob) => {
    const savedUrl = URL.createObjectURL(savedBlob);
    setDoc(savedDoc);
    setDownloadUrl((current) => {
      if (current.startsWith("blob:")) URL.revokeObjectURL(current);
      return savedUrl;
    });
  }, []);

  const handleImageSaved = useCallback((savedDoc: Document, savedBlob: Blob) => {
    const savedUrl = URL.createObjectURL(savedBlob);
    setDoc(savedDoc);
    api.documents.clearDownloadCache(savedDoc.id);
    setDownloadUrl((current) => {
      if (current.startsWith("blob:")) URL.revokeObjectURL(current);
      return savedUrl;
    });
  }, []);

  const handleDownload = async () => {
    if (!docId) return;
    try {
      if (isTaskOutputPreview && taskOutputPreview) {
        const url = URL.createObjectURL(new Blob([taskOutputPreview.content], { type: doc?.mime_type || "text/plain;charset=utf-8" }));
        const a = document.createElement("a");
        a.href = url;
        a.download = doc?.name || taskOutputPreview.name || "generated-output.txt";
        a.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
        return;
      }
      const url = downloadUrl || await api.documents.download(docId);
      const a = document.createElement("a");
      a.href = url;
      a.download = doc?.name || "download";
      a.click();
    } catch {
      // silently fail
    }
  };

  const captureCommentSelection = useCallback(() => {
    const anchor = viewerSelectionAnchor(viewerSurfaceRef.current, doc, category, content);
    setCommentAnchor(anchor || documentCommentAnchor(doc));
    if (anchor) setActiveCommentId(null);
    return anchor;
  }, [category, content, doc]);

  const handleToggleComments = useCallback(() => {
    const anchor = viewerSelectionAnchor(viewerSurfaceRef.current, doc, category, content);
    setCommentAnchor(anchor || commentAnchor || documentCommentAnchor(doc));
    setCommentsOpen((open) => {
      const next = !open;
      if (next) setDetailsOpen(false);
      return next;
    });
  }, [category, commentAnchor, content, doc]);

  const handleOpenAiEdit = useCallback(() => {
    if (category !== "pdf" && category !== "image") return;

    const baseDetail: EditorLiveChatDetail = {
      documentId: docId,
      documentName: doc?.name,
      fileType: doc?.file_type || category,
      mimeType: doc?.mime_type,
      editorType: categoryLabel(category),
    };

    if (category === "pdf" && pdfLiveEditBridge) {
      openEditorLiveChat({
        ...baseDetail,
        fileType: "pdf",
        editorType: "PDF",
        getContent: pdfLiveEditBridge.getContent,
        applyContent: (next, meta) => {
          pdfLiveEditBridge.applyContent(next, meta);
        },
        localEditContent: pdfLiveEditBridge.localEditContent,
      });
      return;
    }

    if (category === "image" && imageLiveEditBridge) {
      openEditorLiveChat({
        ...baseDetail,
        fileType: "image",
        editorType: "Image",
        getContent: imageLiveEditBridge.getContent,
        applyContent: (next, meta) => {
          imageLiveEditBridge.applyContent(next, meta);
        },
        localEditContent: imageLiveEditBridge.localEditContent,
        getAttachmentFiles: imageLiveEditBridge.getAttachmentFiles,
        applyGeneratedImage: (imageUrl, meta) => {
          return imageLiveEditBridge.applyGeneratedImage(imageUrl, meta);
        },
        supportsImageGeneration: imageLiveEditBridge.supportsImageGeneration,
        instruction: `Tell me what to change in ${doc?.name || "this image"}. Try: make it clearer, brighten it, increase contrast, replace text inside the image, regenerate it from this image, rotate right, flip horizontal, or reset edits.`,
      });
    }
  }, [category, doc?.file_type, doc?.mime_type, doc?.name, docId, imageLiveEditBridge, pdfLiveEditBridge]);

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", padding: "128px 0" }}>
        <LoadingSpinner size={32} />
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", padding: "128px 0" }}>
        <div className="glass-panel" style={{ padding: 32, textAlign: "center", maxWidth: 480 }}>
          <p style={{ color: "#c14a44", marginBottom: 16 }}>{error}</p>
          <Button variant="outline" onClick={goBack}>{t("page.file_viewer.go_back")}</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="manor-editor-shell manor-editor-viewer-shell animate-fade-in">
      {/* Header */}
      <div className="manor-editor-header" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
        <div className="manor-editor-header-main">
          <button
            onClick={goBack}
            style={{
              flexShrink: 0, width: 36, height: 36, borderRadius: 12,
              background: "rgba(255,255,255,0.6)", border: "1px solid rgba(28,25,23,0.06)",
              display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer",
            }}
          >
            <IconArrowLeft size={16} className="text-stone-600" />
          </button>
          <div style={{ minWidth: 0 }}>
            <h1 className="manor-editor-title">
              {doc?.name || t("page.file_viewer.document")}
            </h1>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}>
              <StatusBadge type={categoryBadgeType(category)}>{categoryLabel(category)}</StatusBadge>
              {doc?.file_size != null && (
                <span style={{ fontSize: 12, color: "#a8a29e" }}>
                  {doc.file_size < 1024 ? `${doc.file_size} B` :
                   doc.file_size < 1048576 ? `${(doc.file_size / 1024).toFixed(1)} KB` :
                   `${(doc.file_size / 1048576).toFixed(1)} MB`}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="manor-editor-actions">
          {!isTaskOutputPreview && canEditCurrentDoc && (category === "pdf" || category === "image") && (
            <AiEditButton
              onClick={handleOpenAiEdit}
              disabled={category === "pdf" ? !pdfLiveEditBridge : !imageLiveEditBridge}
              title={(category === "pdf" ? !pdfLiveEditBridge : !imageLiveEditBridge) ? t("status.loading") : undefined}
              iconSize={16}
            />
          )}
          {isEditable(category) && !isTaskOutputPreview && canEditCurrentDoc && (
            <Link
              to={`/editor/${docId}`}
              state={location.state}
              className={editorToolButtonClass({ icon: true })}
              aria-label={t("action.edit")}
              title={t("action.edit")}
            >
              <IconEdit size={16} />
            </Link>
          )}
          {!isTaskOutputPreview && canEditCurrentDoc && (category === "video" || doc?.name.toLowerCase().endsWith(".video-edit.json")) && (
            <Link
              to={`/video-editor/${docId}`}
              state={location.state}
              className={editorToolButtonClass({ icon: true })}
              aria-label="Edit video"
              title="Edit video"
            >
              <IconEdit size={16} />
            </Link>
          )}
          {!isTaskOutputPreview && canShareCurrentDoc && (
            <button
              onClick={() => setShareDialogOpen(true)}
              className={editorToolButtonClass({ icon: true })}
              aria-label={t("page.file_viewer.share")}
              title={t("page.file_viewer.share")}
            >
              <IconShare size={16} />
            </button>
          )}
          {!isTaskOutputPreview && doc && (
            <button
              onClick={handleToggleComments}
              className={editorToolButtonClass({ icon: true })}
              style={commentsOpen ? {
                background: "#f5f5f4",
                borderColor: "#d6d3d1",
                color: "#44403c",
              } : undefined}
              aria-label={t("page.tasks.comments")}
              title={t("page.tasks.comments")}
            >
              <IconComment size={16} />
            </button>
          )}
          <button
            onClick={() => {
              setDetailsOpen((open) => {
                const next = !open;
                if (next) setCommentsOpen(false);
                return next;
              });
            }}
            aria-label={detailsOpen ? t("page.file_viewer.details_panel.toggle_close") : t("page.file_viewer.details_panel.toggle_open")}
            title={detailsOpen ? t("page.file_viewer.details_panel.toggle_close") : t("page.file_viewer.details_panel.toggle_open")}
            className={editorToolButtonClass({ icon: true, active: detailsOpen })}
          >
            <IconInfo size={16} />
          </button>
          <button
            onClick={restrictDownload ? undefined : handleDownload}
            disabled={!!restrictDownload}
            aria-label={t("page.file_viewer.download")}
            title={restrictDownload ? (doc?.classification === "restricted" ? t("page.file_viewer.download_blocked.restricted") : t("page.file_viewer.download_blocked.quarantine")) : t("page.file_viewer.download")}
            className={editorToolButtonClass({ icon: true })}
            style={{
              opacity: restrictDownload ? 0.45 : 1,
              cursor: restrictDownload ? "not-allowed" : "pointer",
            }}
          >
            <IconDownload size={16} />
          </button>
          <button
            onClick={goBack}
            style={{
              width: 36, height: 36, borderRadius: 10, background: "transparent",
              border: "none", cursor: "pointer", display: "flex", alignItems: "center",
              justifyContent: "center", color: "#a8a29e", transition: "color 0.15s",
            }}
          >
            <IconClose size={18} />
          </button>
        </div>
      </div>

      {/* Permission banner — legal hold / quarantine / DLP auto-upgrade.
          For legal hold with a reason we use the dedicated i18n key; otherwise
          let PermissionBanner pick its own default message per reason. */}
      {bannerReason && (
        <PermissionBanner
          reason={bannerReason}
          message={
            bannerReason === "legal_hold" && doc?.legal_hold_reason
              ? t("permissions.banner.legal_hold_with_reason", { reason: doc.legal_hold_reason })
              : undefined
          }
        />
      )}

      {/* Content + Details drawer */}
      <div className="manor-editor-viewer-layout">
        <div
          ref={viewerSurfaceRef}
          className="glass-panel manor-editor-viewer-surface"
          onMouseUp={captureCommentSelection}
          onKeyUp={captureCommentSelection}
          onContextMenu={(e) => {
            if (doc?.classification === "restricted") {
              e.preventDefault();
            }
          }}
        >
        {requiresWatermark && currentUser && (
          <WatermarkLayer
            viewerEmail={currentUser.email}
            viewerLabel={currentUser.display_name || currentUser.email}
            entitySlug={currentUser.entity_id}
            density={watermarkDensity}
          />
        )}
        {category === "text" && (
          <TextViewer
            content={content}
            commentRanges={viewerCommentRanges}
            activeCommentId={activeCommentId}
            onSelectCommentId={handleSelectViewerCommentId}
          />
        )}

        {category === "markdown" && (
          <MarkdownViewer
            content={content}
            commentRanges={viewerCommentRanges}
            commentAnchors={viewerAnchoredComments}
            activeCommentId={activeCommentId}
            onSelectCommentId={handleSelectViewerCommentId}
          />
        )}

        {category === "html" && (
          <HtmlViewer content={content} doc={doc} />
        )}

        {category === "code" && (
          <pre className="file-viewer-plain-code">
            <code>
              {renderCommentMarkedText(content, viewerCommentRanges, activeCommentId, handleSelectViewerCommentId)}
            </code>
          </pre>
        )}

        {category === "image" && downloadUrl && (
          <ImageEditor
            url={downloadUrl}
            docId={docId}
            docName={doc?.name}
            mimeType={doc?.mime_type}
            onSaved={handleImageSaved}
            onLiveEditBridgeChange={setImageLiveEditBridge}
            canEdit={canEditCurrentDoc}
          />
        )}

        {category === "video" && downloadUrl && (
          <div className="manor-editor-media-stage">
            <video
              src={downloadUrl}
              controls
              className="manor-editor-video-preview"
            />
          </div>
        )}

        {category === "audio" && downloadUrl && (
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 24, padding: "48px 0" }}>
            <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ color: "#9079c2" }}><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></svg>
            <audio src={downloadUrl} controls style={{ width: "100%", maxWidth: 480 }} />
            <p style={{ fontSize: 13, color: "#a8a29e" }}>{doc?.name}</p>
          </div>
        )}

        {category === "pdf" && (
          <div className="manor-editor-pdf-viewer-host">
            {downloadUrl ? (
              <PdfJsViewer
                url={downloadUrl}
                docId={docId}
                docName={doc?.name}
                onDownload={handleDownload}
                onSaved={handlePdfSaved}
                onLiveEditBridgeChange={setPdfLiveEditBridge}
                canEdit={canEditCurrentDoc}
              />
            ) : (
              <div style={{ textAlign: "center", padding: "64px 0" }}>
                <p style={{ color: "#78716c", marginBottom: 16 }}>{t("page.file_viewer.pdf_preview_requires_downloading_the_file")}</p>
                <button onClick={handleDownload} className="btn-manor">{t("page.file_viewer.download_to_view")}</button>
              </div>
            )}
          </div>
        )}

        {category === "docx" && downloadUrl && (
          <DocxViewer
            url={downloadUrl}
            commentAnchors={viewerAnchoredComments}
            activeCommentId={activeCommentId}
            onSelectCommentId={handleSelectViewerCommentId}
          />
        )}

        {category === "xlsx" && downloadUrl && <XlsxViewer url={downloadUrl} />}

        {category === "pptx" && (
          downloadUrl && <PptxViewJsViewer url={downloadUrl} onDownload={handleDownload} />
        )}

        {category === "csv" && content && (() => {
          const rows = parseCSV(content);
          if (rows.length === 0) return <p style={{ color: "#78716c" }}>{t("page.file_viewer.empty_csv_file")}</p>;
          const header = rows[0];
          const body = rows.slice(1);
          return (
            <div style={{ overflow: "hidden", borderRadius: 12 }}>
              <table className="glass-table">
                <thead>
                  <tr>{header.map((cell, i) => <th key={i}>{cell}</th>)}</tr>
                </thead>
                <tbody>
                  {body.map((row, ri) => (
                    <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{cell}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        })()}

        {category === "json" && (
          <pre className="file-viewer-plain-code">
            <code>
              {renderCommentMarkedText(content, viewerCommentRanges, activeCommentId, handleSelectViewerCommentId)}
            </code>
          </pre>
        )}

        {category === "unsupported" && (
          <div style={{ padding: "64px 0" }}>
            <EmptyState
              icon={<IconDocument size={32} className="text-stone-400" />}
              title={t("page.file_viewer.preview_not_available")}
              description={t("page.file_viewer.this_file_type_cannot_be_previewed_download_to_v")}
              action={<Button variant="primary" onClick={handleDownload}>{t("page.file_viewer.download_file")}</Button>}
            />
          </div>
        )}
        </div>

      {/* Comments drawer */}
      {commentsOpen && doc && !isTaskOutputPreview && (
        <aside className="glass-panel manor-editor-viewer-details manor-editor-comments-panel">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <h2 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0 }}>
              {t("page.tasks.comments")}
            </h2>
            <button
              onClick={() => setCommentsOpen(false)}
              aria-label={t("page.file_viewer.details_panel.close")}
              style={{
                width: 24, height: 24, borderRadius: 6, background: "transparent",
                border: "none", cursor: "pointer", color: "#a8a29e",
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
            >
              <IconClose size={14} />
            </button>
          </div>
          <CommentThread
            resourceType="document"
            resourceId={doc.id}
            canComment={canCommentCurrentDoc}
            anchor={commentAnchor || documentCommentAnchor(doc)}
            activeCommentId={activeCommentId}
            onSelectComment={handleSelectViewerComment}
          />
        </aside>
      )}

      {/* Details drawer (right side) — one of 3 sanctioned places for badges */}
      {detailsOpen && doc && (
        <aside
          className="glass-panel manor-editor-viewer-details"
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <h2 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0 }}>
              {t("page.file_viewer.details_panel.title")}
            </h2>
            <button
              onClick={() => setDetailsOpen(false)}
              aria-label={t("page.file_viewer.details_panel.close")}
              style={{
                width: 24, height: 24, borderRadius: 6, background: "transparent",
                border: "none", cursor: "pointer", color: "#a8a29e",
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
            >
              <IconClose size={14} />
            </button>
          </div>

          <DetailRow label={t("page.file_viewer.details.classification")}>
            <ClassificationBadge level={doc.classification} size="sm" />
          </DetailRow>
          <DetailRow label={t("page.file_viewer.details.visibility")}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "#57534e" }}>
              <VisibilityIcon visibility={doc.visibility} size={13} />
              {doc.visibility ?? "—"}
            </span>
          </DetailRow>
          <DetailRow label={t("page.file_viewer.details.owner")}>
            <span style={{ fontSize: 12, color: "#57534e" }}>{doc.owner_id ?? "—"}</span>
          </DetailRow>
          {doc.file_size != null && (
            <DetailRow label={t("page.file_viewer.details.size")}>
              <span style={{ fontSize: 12, color: "#57534e" }}>
                {doc.file_size < 1024 ? `${doc.file_size} B`
                  : doc.file_size < 1048576 ? `${(doc.file_size / 1024).toFixed(1)} KB`
                  : `${(doc.file_size / 1048576).toFixed(1)} MB`}
              </span>
            </DetailRow>
          )}
          {doc.created_at && (
            <DetailRow label={t("page.file_viewer.details.uploaded")}>
              <span style={{ fontSize: 12, color: "#57534e" }}>
                {new Date(doc.created_at).toLocaleString()}
              </span>
            </DetailRow>
          )}
          {doc.client_visible && (
            <DetailRow label={t("page.file_viewer.details.client_portal")}>
              <span style={{ fontSize: 12, color: "#436b65" }}>
                {t("page.file_viewer.details.client_visible_value")}
              </span>
            </DetailRow>
          )}
          {doc.pii_detected && (
            <DetailRow label={t("page.file_viewer.details.pii_check")}>
              <span style={{ fontSize: 12, color: "#9a5630" }}>
                {t("page.file_viewer.details.pii_detected_value")}
              </span>
            </DetailRow>
          )}
          {doc.quarantine_status && doc.quarantine_status !== "clean" && (
            <DetailRow label={t("page.file_viewer.details.quarantine")}>
              <span style={{ fontSize: 12, color: "#7c4a2e" }}>{doc.quarantine_status}</span>
            </DetailRow>
          )}
          {doc.legal_hold && (
            <DetailRow label={t("page.file_viewer.details.legal_hold")}>
              <span style={{ fontSize: 12, color: "#76502c" }}>
                {t("page.file_viewer.details.legal_hold_value")}{doc.legal_hold_reason ? ` · ${doc.legal_hold_reason}` : ""}
              </span>
            </DetailRow>
          )}

          {canShareCurrentDoc && (
            <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 12 }}>
              <button
                onClick={() => setShareDialogOpen(true)}
                className="btn-manor-outline"
                style={{ width: "100%", fontSize: 12, padding: "6px 10px" }}
              >
                <IconShare size={14} style={{ marginRight: 6 }} />
                {t("page.file_viewer.details.manage_access")}
              </button>
            </div>
          )}

          {/* Access log — owner self-service (RFC §13.8) */}
          <AccessLogSection docId={doc.id} />
        </aside>
      )}
      </div>

      {/* Share dialog */}
      {doc && canShareCurrentDoc && (
        <ShareDialog
          open={shareDialogOpen}
          onClose={() => setShareDialogOpen(false)}
          resourceType="document"
          resourceId={doc.id}
          resourceName={doc.name}
          classification={doc.classification}
          visibility={doc.visibility}
          externalShareNeedsApproval={doc.classification === "confidential"}
          internalGrants={(grantsQuery.data || []).map((g) => _grantToInternalGrant(g, userById))}
          externalShares={(sharesQuery.data || []).map(_shareToExternalShare)}
          entityDomain={_entityDomain(currentUser?.email)}
          onAddInternal={async (pick, role, opts) => {
            // Two paths:
            //   pick.kind="staff"          -> use staff.user_id directly
            //                                 (we already have the linked user)
            //   pick.kind="external_email" -> lookup by email; if not found,
            //                                 surface a clear error.
            let user_id: string;
            if (pick.kind === "staff") {
              if (!pick.staff.user_id) {
                // Staff with no user account — common for vendors / pending
                // invites. Until /staff/{id}/create-account is wired into
                // the share flow, fall back to email lookup which will
                // 404 and tell the user to create the account.
                if (!pick.staff.email) {
                  throw new Error(t("permissions.error.staff_no_account"));
                }
                try {
                  const resolved: UserSummary = await api.users.lookupByEmail(pick.staff.email);
                  user_id = resolved.id;
                } catch {
                  throw new Error(t("permissions.error.staff_no_account_named", { name: pick.staff.name }));
                }
              } else {
                user_id = pick.staff.user_id;
              }
            } else {
              // External email path
              let resolved: UserSummary;
              try {
                resolved = await api.users.lookupByEmail(pick.email);
              } catch (err: any) {
                if (err?.status === 404) {
                  throw new Error(t("permissions.error.user_not_in_org", { email: pick.email }));
                }
                throw err;
              }
              user_id = resolved.id;
            }

            await api.docPermissions.createGrant(doc.id, {
              subject_type: "user",
              subject_id: user_id,
              capabilities: _roleToCapabilities(role),
              expires_at: opts.expiresAt,
              // TODO: backend doesn't send notify-on-add email yet (Phase C).
            });
            void opts.notify;
            void opts.message;
            await queryClient.invalidateQueries({ queryKey: ["doc-grants", doc.id] });
          }}
          onUpdateInternalRole={async (grantId, role) => {
            // Find the existing grant — its subject_id is the resolved user_id
            // so we don't need another email lookup. Just resubmit the grant
            // POST (idempotent upsert by subject) with the new capabilities.
            const existing = (grantsQuery.data || []).find((g) => g.id === grantId);
            if (!existing) throw new Error("Grant not found");
            await api.docPermissions.createGrant(doc.id, {
              subject_type: "user",
              subject_id: existing.subject_user_id || existing.subject_id,
              capabilities: _roleToCapabilities(role),
            });
            await queryClient.invalidateQueries({ queryKey: ["doc-grants", doc.id] });
          }}
          onRemoveInternal={async (grantId) => {
            await api.docPermissions.revokeGrant(doc.id, grantId);
            await queryClient.invalidateQueries({ queryKey: ["doc-grants", doc.id] });
          }}
          onCreateExternal={async (config: NewExternalShareConfig) => {
            // Confidential -> submit for admin approval; never returns a URL
            // until an admin decides via the inbox.
            if (doc.classification === "confidential") {
              if (!config.approval_reason || !config.approval_reason.trim()) {
                throw new Error(t("permissions.error.confidential_approval_required"));
              }
              await api.docPermissions.requestShareApproval(doc.id, {
                audience_type: config.audience_type,
                audience_value: config.audience_value,
                capabilities: config.capabilities,
                expires_in_days: config.expires_in_days,
                watermark: config.watermark,
                require_otp: config.require_otp,
                allow_download: config.capabilities.includes("download"),
                reason: config.approval_reason.trim(),
              });
              await queryClient.invalidateQueries({ queryKey: ["doc-share-approvals", doc.id] });
              return { pending: true };
            }
            const result = await api.docPermissions.createShare(doc.id, {
              audience_type: config.audience_type,
              audience_value: config.audience_value,
              capabilities: config.capabilities,
              expires_in_days: config.expires_in_days,
              watermark: config.watermark,
              require_otp: config.require_otp,
              allow_download: config.capabilities.includes("download"),
            });
            await queryClient.invalidateQueries({ queryKey: ["doc-shares", doc.id] });
            const url = result.url
              || (result.token
                ? `${window.location.origin}/shared-doc/${result.token}`
                : undefined);
            return { url };
          }}
          onRevokeExternal={async (shareId) => {
            await api.docPermissions.revokeShare(doc.id, shareId);
            await queryClient.invalidateQueries({ queryKey: ["doc-shares", doc.id] });
          }}
        />
      )}

      {/* DOCX preview styles */}
      <style>{`
        .docx-viewer-stage {
          flex: 1;
          min-height: 0;
          overflow: auto;
          display: flex;
          justify-content: center;
          align-items: flex-start;
          padding: clamp(48px, 8vh, 84px) clamp(20px, 5vw, 72px) 60px;
          box-sizing: border-box;
        }
        .docx-viewer-page {
          width: min(100%, 816px);
          min-height: min(1056px, calc(100dvh - 168px));
          box-sizing: border-box;
          padding: 64px 72px;
          background: #ffffff;
          border: 1px solid rgba(231, 229, 228, 0.74);
          border-radius: 10px;
          box-shadow: 0 18px 50px rgba(28, 25, 23, 0.09);
          color: #292524;
          font-size: 15px;
          line-height: 1.68;
        }
        .docx-preview { overflow-wrap: anywhere; }
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
        .docx-preview blockquote {
          margin: 14px 0;
          padding: 10px 14px;
          border-left: 3px solid #ccded9;
          color: #57534e;
          background: #fafaf9;
        }
        .docx-preview hr {
          border: 0;
          border-top: 1px solid #e7e5e4;
          margin: 22px 0;
        }
        @media (max-width: 760px) {
          .docx-viewer-stage { padding: 16px; }
          .docx-viewer-page {
            min-height: 70vh;
            padding: 34px 26px;
            border-radius: 12px;
          }
        }
      `}</style>
    </div>
  );
}
