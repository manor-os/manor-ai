import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { officeCompatibleFontFamily } from "../lib/officeFonts";
import { createPortal } from "react-dom";
import {
  useBlocker,
  useLocation,
  useParams,
  useNavigate,
  type BlockerFunction,
} from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { PrismAsync as CodeSyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import { api, ApiError } from "../lib/api";
import { invalidateKnowledgeQueries } from "../lib/knowledgeInvalidation";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import IsolatedHtmlPreviewFrame from "../components/ui/IsolatedHtmlPreviewFrame";
import Select from "../components/ui/Select";
import Dropdown from "../components/ui/Dropdown";
import Button from "../components/ui/Button";
import AiEditButton from "../components/ui/AiEditButton";
import AiEditPreviewControls from "../components/ui/AiEditPreviewControls";
import ResizablePaneGroup, { type ResizablePaneDefinition } from "../components/ui/ResizablePaneGroup";
import { PageHeaderTitle } from "../components/ui/PageHeader";
import EditorLiveInlineDiff from "../components/EditorLiveInlineDiff";
import MediaInsertDialog from "../components/MediaInsertDialog";
import MarkdownTable from "../components/MarkdownTable";
import SpreadsheetChartPreview from "../components/SpreadsheetChartPreview";
import SpreadsheetImageLayer from "../components/SpreadsheetImageLayer";
import PresentationShapeOutline from "../components/PresentationShapeOutline";
import { presentationRoundRectRadius, presentationStrokeDash } from "../lib/presentationShapeStyle";
import {
  presentationPresetClipPath,
  presentationPresetPointsAttribute,
  presentationPresetPolygonPoints,
} from "../lib/presentationPresetGeometry";
import SitePublishAction from "../components/SitePublishAction";
import CodeProjectExplorer from "../components/code/CodeProjectExplorer";
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
  IconInfo,
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
  AiEditPatchStreamEventKind,
  AiEditPreviewStatus,
  AiEditTargetKind,
  createAiEditCommitCoordinator,
  createEditorLiveAdapter,
  mergeEditorLivePreviewDiff,
  nextEditorLiveChangeCount,
  openEditorLiveChat,
  updateEditorLiveChat,
  type EditorLiveApplyMeta,
  type EditorLiveChatDetail,
  type EditorNativeFilePatchResult,
} from "../lib/editorLiveChat";
import {
  buildEditorLiveTextFrames,
  createEditorLiveDelimitedFrameStream,
  type EditorLiveTextFrame,
} from "../lib/editorLiveAnimation";
import {
  EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX,
  serializeEditorLiveSpreadsheetPayload,
} from "../lib/editorLiveSpreadsheet";
import {
  AiEditDisplayMode,
  resolveAiEditDisplayMode,
} from "../lib/aiEditPreferences";
import { authPrincipalKey, getAuthToken } from "../lib/authToken";
import { allocateEditorSaveIntent } from "../lib/auxiliarySaveQueue";
import { codeLanguageForFile, codeLanguageLabel, isCodeLikeFile } from "../lib/codeFiles";
import {
  fileReferenceKind,
  isEditableDiagramReference,
  OfficeEditorFileFactory,
  OfficeEditorFormat,
} from "../lib/fileReferences";
import {
  codeProjectDirectory,
  codeProjectName,
  useCodeProjectWorkspace,
} from "../lib/useCodeProjectWorkspace";
import { useHtmlPreviewDocument } from "../lib/useHtmlPreviewDocument";
import {
  IDENTITY_PRESENTATION_GROUP_TRANSFORM,
  presentationColorWithAlpha,
  presentationGroupContent,
  presentationGroupTransform,
  presentationInverseTransform,
  presentationMediaMime,
  presentationObjectGroups,
  presentationObjectIdsInOrder,
  presentationRelationshipsPart,
  presentationResizeRect,
  presentationShapeTransform,
  presentationShapeFillScope,
  presentationTransformPoint,
  presentationVideoSource,
  resolvePresentationPartTarget,
  type PresentationGroupTransform,
  type PresentationResizeHandle,
} from "../lib/presentationOoxml";
import {
  findPresentationPlaceholderShape,
  presentationInheritedTextStyleLevels,
  presentationPointsToCqh as pptxPointsToCqh,
  type PresentationTextLevelStyle,
} from "../lib/presentationStyleInheritance";
import {
  rebasePresentationTextSourceMap,
  reconcilePresentationTextRuns,
  reconcilePresentationTextSourceMap,
  type PresentationTextEditSpan,
  type PresentationTextSourceMap,
} from "../lib/presentationTextEdits";
import {
  applyPresentationLiveEditContent,
  buildPresentationLiveEditContent,
  createPresentationSlide,
  mediaExtension,
  PresentationSlideLayout,
  presentationImageMime,
  presentationLiveEditTargetShape,
  type PresentationLiveEditTarget,
} from "../lib/presentationLiveEdit";
import { presentationShapesForDuplicateSlide } from "../lib/presentationEditability";
import {
  detectDelimitedTextFormat,
  parseDelimitedText,
  serializeDelimitedText,
  type DelimitedTextFormat,
} from "../lib/delimitedText";
import {
  decodeTextFile,
  encodeTextFile,
  textEncodingLabel,
  textFileFormatForSave,
  type PreservedTextFormat,
} from "../lib/textFilePreservation";
import {
  isValidSpreadsheetWorksheetName,
  nextSpreadsheetSheetName,
  spreadsheetMergeAt,
  spreadsheetSheetsFromFile,
  spreadsheetActiveSheetIndex,
  spreadsheetCellVisualStyle,
  type SpreadsheetCellStyle,
  type SpreadsheetEditorChart,
  type SpreadsheetSheetModel,
  type SpreadsheetSheetSnapshot,
  type SpreadsheetStructureOperation,
} from "../lib/spreadsheetOoxml";
import {
  createSpreadsheetFormulaEvaluationState,
  getSpreadsheetDisplayValue,
  getSpreadsheetNumericValue,
  type SpreadsheetNumberFormatter,
} from "../lib/spreadsheetFormula";
import { useAuthStore } from "../stores/auth";
import { useToastStore } from "../stores/toast";
import { canCommentDocument, canEditDocument } from "../lib/permissions";
import type { Comment, CommentAnchor } from "../lib/types";
import type { InsertableMediaAsset } from "../lib/mediaInsertion";
import { sanitizeDocumentHtml, sanitizeManorDocumentRender } from "../lib/sanitizeDocumentHtml";
import {
  paginateManorDocument,
  renderManorDocument,
  serializeManorDocumentHtml,
  type ManorDocumentRender,
} from "../lib/manorDocumentEngine";
import {
  canRedoPlainTextHistory,
  canUndoPlainTextHistory,
  createPlainTextHistory,
  normalizePlainTextSelection,
  recordPlainTextHistory,
  redoPlainTextHistory,
  resetPlainTextHistory,
  undoPlainTextHistory,
  type PlainTextSelection,
} from "../lib/plain-text-history.mjs";

import { t } from "../lib/i18n";
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type EditorMode = "richtext" | "markdown" | "code" | "text" | "spreadsheet" | "presentation" | "diagram";
type MarkdownViewMode = "source" | "split" | "preview";
type MarkdownEditResult = { next: string; selectionStart: number; selectionEnd?: number };
type EditorLivePreviewState = {
  baseline: string;
  current: string;
  baselineSlides?: PptxSlide[];
  currentSlides?: PptxSlide[];
  mode: EditorMode;
  status: AiEditPreviewStatus;
  phase: EditorLiveTextFrame["phase"] | "format";
  changeCount: number;
  targetId: string;
  targetPath?: string;
  diff?: string;
  modelStreaming?: boolean;
};

function cloneEditorLivePreviewState(
  preview: EditorLivePreviewState | null,
): EditorLivePreviewState | null {
  if (!preview) return null;
  return {
    ...preview,
    baselineSlides: preview.baselineSlides
      ? structuredClone(preview.baselineSlides)
      : undefined,
    currentSlides: preview.currentSlides
      ? structuredClone(preview.currentSlides)
      : undefined,
  };
}

function waitForEditorLiveFrame(delayMs: number) {
  if (delayMs <= 0) return Promise.resolve();
  return new Promise<void>((resolve) => window.setTimeout(resolve, delayMs));
}

function editorTextRange(root: HTMLElement, start: number, end: number) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let current = walker.nextNode();
  let offset = 0;
  let startPoint: { node: Node; offset: number } | null = null;
  let endPoint: { node: Node; offset: number } | null = null;
  let lastTextNode: Node | null = null;

  while (current) {
    lastTextNode = current;
    const length = current.textContent?.length || 0;
    if (!startPoint && start <= offset + length) {
      startPoint = { node: current, offset: Math.max(0, start - offset) };
    }
    if (end <= offset + length) {
      endPoint = { node: current, offset: Math.max(0, end - offset) };
      break;
    }
    offset += length;
    current = walker.nextNode();
  }

  if (!startPoint && lastTextNode) {
    startPoint = { node: lastTextNode, offset: lastTextNode.textContent?.length || 0 };
  }
  if (!endPoint) endPoint = startPoint;
  if (!startPoint || !endPoint) return null;

  const range = document.createRange();
  range.setStart(startPoint.node, startPoint.offset);
  range.setEnd(endPoint.node, endPoint.offset);
  return range;
}

const DOCX_BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,div,blockquote,pre,figure,td,th,hr";
const DOCX_DIRECT_INLINE_BLOCK_SELECTOR = "a[href],img[src]";
const DOCX_PARAGRAPH_BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,div,blockquote,pre";

function docxLeafBlocks(root: ParentNode): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(`${DOCX_BLOCK_SELECTOR},${DOCX_DIRECT_INLINE_BLOCK_SELECTOR}`)).filter((element) => {
    if (element.closest("[data-manor-docx-layout]")) return false;
    if (element.parentElement === root && element.matches(DOCX_DIRECT_INLINE_BLOCK_SELECTOR)) return true;
    return element.matches(DOCX_BLOCK_SELECTOR) && !element.querySelector(DOCX_BLOCK_SELECTOR);
  });
}

type TextFileSaveSnapshot = {
  originalBuffer: ArrayBuffer;
  baseline: string;
  format: PreservedTextFormat;
};

type TextSaveRequest = {
  text: string;
  documentId: string;
  documentName: string | null;
  documentMimeType: string | null;
  documentPath: string | null;
  canEdit: boolean;
  authPrincipalKey: string;
  sessionRevision: number;
  editRevision: number;
  docxOriginalBuffer: ArrayBuffer | null;
  docxBaselineHtml: string | null;
  docxExpectedSourceSha256: string | null;
  isDocx: boolean;
  textOriginalBuffer: ArrayBuffer | null;
  textBaseline: string | null;
  textFormat: PreservedTextFormat | null;
  textFormatLoad: Promise<TextFileSaveSnapshot | null> | null;
};

type SpreadsheetSaveRequest = {
  sheets: SpreadsheetSheetSnapshot[];
  documentId: string;
  documentName: string;
  documentPath: string | null;
  canEdit: boolean;
  authPrincipalKey: string;
  sessionRevision: number;
  revision: number;
  originalBuffer: ArrayBuffer;
  expectedSourceSha256: string;
  baselineSheets: SpreadsheetSheetSnapshot[];
};

type PresentationSaveRequest = {
  slides: PptxSlide[];
  documentId: string;
  documentName: string;
  documentPath: string | null;
  canEdit: boolean;
  authPrincipalKey: string;
  sessionRevision: number;
  revision: number;
  originalBuffer: ArrayBuffer | null;
  expectedSourceSha256: string | null;
  baselineSlides: PptxSlide[] | null;
};

function spreadsheetSheetSnapshot(sheet: SpreadsheetSheetModel): SpreadsheetSheetSnapshot {
  return {
    name: sheet.name,
    sourceName: sheet.sourceName || sheet.name,
    data: structuredClone(sheet.data),
    styles: structuredClone(sheet.styles),
    columnWidths: [...sheet.columnWidths],
    rowHeights: [...sheet.rowHeights],
    merges: structuredClone(sheet.merges),
    editorCharts: structuredClone(sheet.editorCharts || []),
    structureOperations: structuredClone(sheet.structureOperations || []),
    hidden: sheet.hidden,
  };
}

function createEmptySpreadsheetSheet(name: string): SpreadsheetSheetModel {
  return {
    name,
    data: [[""]],
    displayData: [[""]],
    numberFormats: {},
    styles: {},
    columnWidths: [112],
    rowHeights: [32],
    merges: [],
    charts: [],
    images: [],
    editorCharts: [],
    structureOperations: [],
    hidden: false,
  };
}

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

const PLAIN_TEXT_INCREMENTAL_INPUT_TYPES = new Set([
  "insertText",
  "insertLineBreak",
  "insertParagraph",
  "insertFromPaste",
  "insertFromDrop",
  "deleteContentBackward",
  "deleteContentForward",
  "deleteWordBackward",
  "deleteWordForward",
  "deleteSoftLineBackward",
  "deleteSoftLineForward",
  "deleteHardLineBackward",
  "deleteHardLineForward",
  "deleteByCut",
  "deleteByDrag",
]);

function plainTextSelectionFrom(textarea: HTMLTextAreaElement): PlainTextSelection {
  return {
    start: textarea.selectionStart,
    end: textarea.selectionEnd,
    direction: textarea.selectionDirection,
  };
}

function restorePlainTextSelection(textarea: HTMLTextAreaElement, selection: PlainTextSelection) {
  textarea.setSelectionRange(selection.start, selection.end, selection.direction || "none");
}

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
const EMPTY_COMMENTS: Comment[] = [];

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

function surfaceSelectionAnchor(
  surface: HTMLElement | null,
  mode: string,
  source?: string,
): CommentAnchor | null {
  const selection = window.getSelection();
  if (!surface || !selection || selection.rangeCount === 0 || selection.isCollapsed) return null;

  const range = selection.getRangeAt(0);
  return rangeSelectionAnchor(surface, range, mode, source);
}

function rangeSelectionAnchor(
  surface: HTMLElement | null,
  range: Range | null,
  mode: string,
  source?: string,
): CommentAnchor | null {
  if (!surface || !range || range.collapsed) return null;
  if (!surface.contains(range.commonAncestorContainer)) return null;

  const quote = trimCommentQuote(range.toString());
  if (!quote) return null;

  return {
    type: "rendered_text_selection",
    mode,
    source,
    quote,
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

function detectMode(document: {
  name: string;
  file_type?: string | null;
  mime_type?: string | null;
}): EditorMode {
  const officeFile = OfficeEditorFileFactory.create(
    document.name,
    document.mime_type || undefined,
    document.file_type || undefined,
  );
  if (officeFile.kind === "spreadsheet") return "spreadsheet";
  if (officeFile.kind === "presentation") return "presentation";
  if (officeFile.kind === "document") return "richtext";
  const documentKind = fileReferenceKind(
    document.name,
    document.mime_type || undefined,
    document.file_type || undefined,
  );
  if (documentKind === "diagram") {
    if (isEditableDiagramReference(document.name, document.file_type || undefined)) return "diagram";
    return "code";
  }
  if (documentKind === "spreadsheet") return "spreadsheet";
  if (documentKind === "presentation") return "presentation";
  const ext = String(
    document.file_type || document.name.split(".").pop() || "",
  ).toLowerCase().replace(/^\./, "");
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "xlsx" || ext === "xls" || ext === "csv") return "spreadsheet";
  if (ext === "pptx" || ext === "ppt") return "presentation";
  if (isCodeLikeFile(document)) return "code";
  if (["txt", "text", "log"].includes(ext)) return "text";
  if (isPlainTextFile(document.name)) return "text";
  return "richtext"; // docx, doc, and other document-like files use rich text
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

function preservesTextFileBytes(document: {
  name: string;
  file_type?: string | null;
  mime_type?: string | null;
}): boolean {
  const officeFile = OfficeEditorFileFactory.create(
    document.name,
    document.mime_type || undefined,
    document.file_type || undefined,
  );
  if (officeFile.usesBinaryPackage) return false;
  return ["text", "markdown", "code", "spreadsheet", "diagram"].includes(detectMode(document));
}

const DOCUMENT_SAVE_RETRY_DELAYS = [1000, 2000] as const;
const DOCUMENT_SAVE_AUTH_CHANGED_MESSAGE = "Your account or workspace changed. Review your changes before saving again.";
const DOCUMENT_SAVE_RETRYABLE_STATUSES = new Set([408, 423, 429]);

function currentDocumentSaveAuthToken(expectedPrincipalKey: string): string | null {
  const currentToken = getAuthToken();
  if (authPrincipalKey(currentToken) !== expectedPrincipalKey) {
    throw new Error(DOCUMENT_SAVE_AUTH_CHANGED_MESSAGE);
  }
  return currentToken;
}

function isRetryableDocumentSaveError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return DOCUMENT_SAVE_RETRYABLE_STATUSES.has(error.status)
      || (error.status >= 500 && error.status < 600);
  }
  return error instanceof TypeError;
}

async function retryDocumentSave<T>(save: () => Promise<T>): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await save();
    } catch (error) {
      const retryDelay = DOCUMENT_SAVE_RETRY_DELAYS[attempt];
      if (retryDelay == null || !isRetryableDocumentSaveError(error)) throw error;
      await new Promise<void>((resolve) => window.setTimeout(resolve, retryDelay));
    }
  }
}

async function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("Unable to read image data."));
    reader.readAsDataURL(blob);
  });
}

function safeMediaFileName(name: string, fallback: string) {
  const clean = name
    .split(/[\\/]/).pop()
    ?.replace(/[^a-z0-9._-]+/gi, "-")
    .replace(/^-+|-+$/g, "") || fallback;
  return clean.slice(0, 120) || fallback;
}

function relativeFsReference(fromFile: string, targetFile: string) {
  const fromParts = fromFile.replace(/^\/+/, "").split("/").filter(Boolean);
  const targetParts = targetFile.replace(/^\/+/, "").split("/").filter(Boolean);
  fromParts.pop();
  let shared = 0;
  while (shared < fromParts.length && shared < targetParts.length && fromParts[shared] === targetParts[shared]) shared += 1;
  const relative = `${"../".repeat(fromParts.length - shared)}${targetParts.slice(shared).join("/")}`;
  return relative.startsWith(".") ? relative : `./${relative}`;
}

async function presentationReplacementImageDataUrl(imageUrl: string, expectedMime: string): Promise<string> {
  const response = await fetch(imageUrl);
  if (!response.ok) throw new Error(`Unable to read the replacement image (${response.status}).`);
  const sourceBlob = await response.blob();
  if (!sourceBlob.type || sourceBlob.type.toLowerCase() === expectedMime) {
    return blobToDataUrl(new Blob([sourceBlob], { type: expectedMime }));
  }
  if (!["image/png", "image/jpeg", "image/webp"].includes(expectedMime)) {
    throw new Error(`This PPTX image must stay ${expectedMime}; use a matching replacement file.`);
  }
  const bitmap = await createImageBitmap(sourceBlob);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, bitmap.width);
    canvas.height = Math.max(1, bitmap.height);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Unable to prepare the replacement image.");
    context.drawImage(bitmap, 0, 0);
    const converted = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        (blob) => blob ? resolve(blob) : reject(new Error("Unable to convert the replacement image.")),
        expectedMime,
        expectedMime === "image/jpeg" ? 0.94 : undefined,
      );
    });
    return blobToDataUrl(converted);
  } finally {
    bitmap.close();
  }
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
  return normalizeSheetData(parseDelimitedText(text).rows);
}

function parseSpreadsheetPasteText(text: string): string[][] {
  const normalized = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (normalized.includes("\t")) {
    const lines = normalized.endsWith("\n") ? normalized.slice(0, -1).split("\n") : normalized.split("\n");
    return lines.map((line) => line.split("\t"));
  }
  return parseCsvText(text).map((row) => row.map((cell) => String(cell ?? "")));
}

type SheetChartType = SpreadsheetEditorChart["type"];
type SheetChartConfig = SpreadsheetEditorChart;

type SheetTextAlign = "left" | "center" | "right";

type SheetCellStyle = SpreadsheetCellStyle;

type SheetStyleMap = Record<string, SheetCellStyle>;

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
      aria-label={title}
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
        aria-label={title}
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

function sheetDataToCsv(data: any[][]): string {
  return serializeDelimitedText(data, { delimiter: ",", lineEnding: "\n", finalLineEnding: false });
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
    for (const flag of ["underline", "strike", "wrapText"] as const) {
      if (typeof style[flag] === "boolean") next[flag] = style[flag];
    }
    if (style.verticalAlign === "top" || style.verticalAlign === "middle" || style.verticalAlign === "bottom") next.verticalAlign = style.verticalAlign;
    for (const border of ["borderTop", "borderBottom", "borderLeft", "borderRight"] as const) {
      const value = style[border];
      if (typeof value === "string" && /^[123]px (?:solid|dashed|dotted|double) #[a-f\d]{6}$/i.test(value)) next[border] = value;
    }
    if (typeof style.fontFamily === "string" && style.fontFamily.trim()) next.fontFamily = style.fontFamily.trim();
    if (typeof style.color === "string" && style.color.trim()) next.color = style.color.trim();
    if (typeof style.fill === "string" && style.fill.trim()) next.fill = style.fill.trim();
    if (style.align === "left" || style.align === "center" || style.align === "right") next.align = style.align;
    const fontSize = Number(style.fontSize);
    if (Number.isFinite(fontSize) && fontSize > 0) next.fontSize = Math.max(1, Math.min(409, fontSize));
    if (Object.keys(next).length > 0) normalized[key] = next;
  });
  return normalized;
}

function parseSpreadsheetPayload(text: string): { data: any[][]; charts: SheetChartConfig[]; styles: SheetStyleMap } | null {
  if (!text.startsWith(EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX)) return null;
  try {
    const parsed = JSON.parse(text.slice(EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX.length)) as { data?: any[][]; charts?: unknown; styles?: unknown };
    const data = normalizeSheetData(Array.isArray(parsed.data) ? parsed.data : null);
    return { data, charts: normalizeSheetCharts(parsed.charts, data), styles: normalizeSheetStyles(parsed.styles) };
  } catch {
    return null;
  }
}

function serializeSpreadsheetContent(data: any[][], charts: SheetChartConfig[], persistCharts: boolean, styles: SheetStyleMap = {}): string {
  const cleanStyles = normalizeSheetStyles(styles);
  if (!persistCharts) return sheetDataToCsv(data);
  return serializeEditorLiveSpreadsheetPayload(data, charts, cleanStyles);
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

function getDefaultSheetChartColumns(data: any[][], maxCols: number): { labelColumn: number; valueColumn: number } {
  const columns = Array.from({ length: maxCols }, (_, i) => i);
  const numericColumns = columns.filter((col) => data.some((row, rowIdx) => rowIdx > 0 && getSpreadsheetNumericValue(data, rowIdx, col) != null));
  const valueColumn = numericColumns.find((col) => col !== 0) ?? numericColumns[0] ?? Math.min(1, maxCols - 1);
  const labelColumn = columns.find((col) => col !== valueColumn && data.some((row, rowIdx) => {
    if (rowIdx === 0) return false;
    const text = String(row[col] ?? "").trim();
    return text.length > 0 && getSpreadsheetNumericValue(data, rowIdx, col) == null;
  })) ?? (valueColumn === 0 ? Math.min(1, maxCols - 1) : 0);
  return { labelColumn, valueColumn };
}

function buildChartPoints(data: any[][], chart: SheetChartConfig): { label: string; value: number }[] {
  const start = Math.max(0, Math.min(data.length - 1, chart.startRow));
  const end = Math.max(start, Math.min(data.length - 1, chart.endRow));
  const points: { label: string; value: number }[] = [];
  for (let r = start; r <= end; r += 1) {
    const value = getSpreadsheetNumericValue(data, r, chart.valueColumn);
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
  initialDisplayData = [],
  initialNumberFormats = {},
  numberFormatter,
  columnWidths = [],
  rowHeights = [],
  showGridlines = true,
  merges = [],
  nativeCharts = [],
  nativeImages = [],
  sheetTabs = [],
  activeSheetIndex = 0,
  onSelectSheet,
  onAddSheet,
  onRenameSheet,
  persistCharts,
  onChange,
}: {
  initialData: any[][] | null;
  initialCharts: SheetChartConfig[];
  initialStyles: SheetStyleMap;
  initialDisplayData?: string[][];
  initialNumberFormats?: SpreadsheetSheetModel["numberFormats"];
  numberFormatter?: SpreadsheetNumberFormatter;
  columnWidths?: number[];
  rowHeights?: number[];
  showGridlines?: boolean;
  merges?: SpreadsheetSheetModel["merges"];
  nativeCharts?: SpreadsheetSheetModel["charts"];
  nativeImages?: SpreadsheetSheetModel["images"];
  sheetTabs?: Array<{ index: number; name: string }>;
  activeSheetIndex?: number;
  onSelectSheet?: (sheetIndex: number) => void;
  onAddSheet?: () => void;
  onRenameSheet?: (sheetIndex: number, nextName: string) => boolean;
  persistCharts: boolean;
  onChange: (
    data: any[][],
    charts: SheetChartConfig[],
    styles: SheetStyleMap,
    structureOperation?: SpreadsheetStructureOperation,
  ) => void;
}) {
  const [data, setData] = useState<any[][]>(() => normalizeSheetData(initialData));
  const [charts, setCharts] = useState<SheetChartConfig[]>(() => normalizeSheetCharts(initialCharts, normalizeSheetData(initialData)));
  const [styles, setStyles] = useState<SheetStyleMap>(() => normalizeSheetStyles(initialStyles));
  const [selected, setSelected] = useState<SheetCellCoord>({ r: 0, c: 0 });
  const [selectionAnchor, setSelectionAnchor] = useState<SheetCellCoord>({ r: 0, c: 0 });
  const [isSelecting, setIsSelecting] = useState(false);
  const [showChartPanel, setShowChartPanel] = useState(false);
  const [renamingSheetIndex, setRenamingSheetIndex] = useState<number | null>(null);
  const [renamingSheetName, setRenamingSheetName] = useState("");
  const activeInputRef = useRef<HTMLTextAreaElement | null>(null);
  const sourceDataRef = useRef<any[][]>(structuredClone(normalizeSheetData(initialData)));

  useEffect(() => {
    const normalized = normalizeSheetData(initialData);
    sourceDataRef.current = structuredClone(normalized);
    setData(normalized);
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
  const formulaEvaluationState = createSpreadsheetFormulaEvaluationState();
  const imageMaxRow = Math.max(0, ...nativeImages.map((image) => (
    image.end?.r ?? image.anchor.r + Math.ceil((image.height || 0) / 32)
  )));
  const imageMaxColumn = Math.max(0, ...nativeImages.map((image) => (
    image.end?.c ?? image.anchor.c + Math.ceil((image.width || 0) / 112)
  )));
  const visibleRows = Math.max(MIN_VISIBLE_SHEET_ROWS, data.length, selected.r + 1, imageMaxRow + 1);
  const maxCols = Math.max(MIN_VISIBLE_SHEET_COLS, actualMaxCols, selected.c + 1, imageMaxColumn + 1);
  const renderedRowHeights = Array.from({ length: visibleRows }, (_, rowIndex) => {
    const row = data[rowIndex] || [];
    const natural = Math.max(
      32,
      ...row.map((value) => String(value ?? "").split(/\r\n|\r|\n/).length * 18 + 12),
    );
    return rowHeights[rowIndex] || natural;
  });
  const renderedColumnWidths = Array.from({ length: maxCols }, (_, columnIndex) => (
    columnWidths[columnIndex] || 112
  ));
  const selectionRange = useMemo(() => normalizeSheetSelection(selected, selectionAnchor), [selected, selectionAnchor]);
  const selectedRangeLabel = spreadsheetRangeRef(selectionRange);
  const activeCellValue = selected ? String(data[selected.r]?.[selected.c] ?? "") : "";
  const activeStyle = styles[sheetStyleKey(selected.r, selected.c)] || {};
  const columnOptions = Array.from({ length: maxCols }, (_, i) => i);
  const numericColumnOptions = columnOptions.filter((col) => data.some((row, rowIdx) => (
    rowIdx > 0 && getSpreadsheetNumericValue(
      data,
      rowIdx,
      col,
      new Set(),
      undefined,
      formulaEvaluationState,
    ) != null
  )));
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

  const beginSheetRename = useCallback((sheet: { index: number; name: string }) => {
    if (!onRenameSheet) return;
    setRenamingSheetIndex(sheet.index);
    setRenamingSheetName(sheet.name);
  }, [onRenameSheet]);

  const finishSheetRename = useCallback(() => {
    if (renamingSheetIndex == null) return;
    const sheet = sheetTabs.find((candidate) => candidate.index === renamingSheetIndex);
    const nextName = renamingSheetName.trim();
    if (!sheet || nextName === sheet.name) {
      setRenamingSheetIndex(null);
      return;
    }
    if (onRenameSheet?.(renamingSheetIndex, nextName)) {
      setRenamingSheetIndex(null);
    }
  }, [onRenameSheet, renamingSheetIndex, renamingSheetName, sheetTabs]);

  const focusCell = useCallback((targetR: number, targetC: number, extend = false) => {
    const row = Math.max(0, targetR);
    const column = Math.max(0, targetC);
    const mergedRange = merges.find((range) => (
      row >= range.s.r && row <= range.e.r && column >= range.s.c && column <= range.e.c
    ));
    const safe = mergedRange ? { r: mergedRange.s.r, c: mergedRange.s.c } : { r: row, c: column };
    setSelected(safe);
    setSelectionAnchor((current) => extend ? current : safe);
    focusActiveInput();
  }, [focusActiveInput, merges]);

  const addRow = useCallback(() => {
    const range = selectionRange;
    const insertAt = range ? range.r2 + 1 : data.length;
    const next = ensureSheetDimensions(data, Math.max(data.length, insertAt), maxCols);
    next.splice(insertAt, 0, Array(maxCols).fill(""));
    const nextStyles = shiftStylesForRowInsert(styles, insertAt);
    setStyles(nextStyles);
    onChange(next, charts, nextStyles, { axis: "row", index: insertAt, deleteCount: 0, insertCount: 1 });
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
    onChange(next, charts, nextStyles, { axis: "column", index: insertAt, deleteCount: 0, insertCount: 1 });
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
    onChange(normalized, charts, nextStyles, {
      axis: "row",
      index: range.r1,
      deleteCount: range.r2 - range.r1 + 1,
      insertCount: normalized.length === data.length ? 1 : 0,
    });
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
    onChange(normalized, charts, nextStyles, {
      axis: "column",
      index: range.c1,
      deleteCount: range.c2 - range.c1 + 1,
      insertCount: normalized[0]?.length === data[0]?.length ? 1 : 0,
    });
    focusCell(selected.r, Math.min(range.c1, Math.max(0, maxCols - (range.c2 - range.c1 + 1) - 1)));
  }, [charts, data, focusCell, maxCols, onChange, selected.r, selectionRange, styles]);

  const clearSelection = useCallback(() => {
    const range = selectionRange;
    if (!range) return;
    const lastRow = range.r2;
    const lastColumn = range.c2;
    const next = ensureSheetDimensions(data, lastRow + 1, lastColumn + 1);
    for (let r = range.r1; r <= lastRow; r += 1) {
      for (let c = range.c1; c <= lastColumn; c += 1) {
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
      const lastRow = range.r2;
      const lastColumn = range.c2;
      const next = ensureSheetDimensions(data, lastRow + 1, lastColumn + 1);
      for (let r = range.r1; r <= lastRow; r += 1) {
        for (let c = range.c1; c <= lastColumn; c += 1) {
          next[r][c] = matrix[0][0];
        }
      }
      commitData(next);
      focusCell(lastRow, lastColumn, true);
      return;
    }
    const maxPasteCols = Math.max(1, ...matrix.map((row) => row.length));
    const targetRows = startR + matrix.length;
    const targetCols = startC + maxPasteCols;
    if (targetRows <= startR || targetCols <= startC) return;
    const next = ensureSheetDimensions(data, targetRows, targetCols);
    matrix.forEach((row, ri) => {
      row.forEach((cell, ci) => {
        next[startR + ri][startC + ci] = cell;
      });
    });
    commitData(next);
    const end = { r: targetRows - 1, c: targetCols - 1 };
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
      ? rangeColumns.filter((col) => data.some((_row, rowIdx) => rowIdx >= range.r1 && rowIdx <= range.r2 && getSpreadsheetNumericValue(data, rowIdx, col) != null))
      : [];
    const valueColumn = rangeIsUseful
      ? rangeNumericColumns.find((col) => range && col !== range.c1) ?? rangeNumericColumns[0] ?? inferred.valueColumn
      : inferred.valueColumn;
    const labelColumn = rangeIsUseful
      ? rangeColumns.find((col) => col !== valueColumn && range && data.some((row, rowIdx) => {
        if (rowIdx < range.r1 || rowIdx > range.r2) return false;
        const text = String(row[col] ?? "").trim();
        return text.length > 0 && getSpreadsheetNumericValue(data, rowIdx, col) == null;
      })) ?? inferred.labelColumn
      : inferred.labelColumn;
    const headerLooksText = rangeIsUseful && range && range.r2 > range.r1 && getSpreadsheetNumericValue(data, range.r1, valueColumn) == null;
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
    setSelected({
      r: visibleRows - 1,
      c: maxCols - 1,
    });
  }, [maxCols, visibleRows]);

  const handleCellKeyDown = useCallback((event: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>, ri: number, ci: number, rawCellValue: string) => {
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
      if (event.altKey) return;
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
      {(sheetTabs.length > 0 || onAddSheet) && (
        <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "7px 12px", borderBottom: "1px solid var(--border-subtle, #dbe3ef)", background: "var(--surface-panel, #ffffff)", overflowX: "auto", flexShrink: 0 }}>
          {sheetTabs.map((sheet) => {
            const active = sheet.index === activeSheetIndex;
            const tabStyle = {
              padding: "5px 12px",
              borderRadius: 7,
              border: `1px solid ${active ? "var(--editor-active-border, #ccded9)" : "transparent"}`,
              background: active ? "var(--editor-active-bg, #ecfdf8)" : "transparent",
              color: active ? "var(--editor-active-text, #436b65)" : "var(--text-muted, #57534e)",
              fontSize: 12,
              fontWeight: active ? 800 : 650,
              whiteSpace: "nowrap" as const,
            };
            return renamingSheetIndex === sheet.index ? (
              <input
                key={`${sheet.index}:${sheet.name}:rename`}
                value={renamingSheetName}
                autoFocus
                aria-label={t("page.doc_editor.rename_sheet")}
                onChange={(event) => setRenamingSheetName(event.target.value)}
                onBlur={finishSheetRename}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    finishSheetRename();
                  } else if (event.key === "Escape") {
                    event.preventDefault();
                    setRenamingSheetIndex(null);
                  }
                }}
                style={{ ...tabStyle, minWidth: 96, cursor: "text" }}
              />
            ) : (
              <button
                key={`${sheet.index}:${sheet.name}`}
                type="button"
                onClick={() => onSelectSheet?.(sheet.index)}
                onDoubleClick={() => beginSheetRename(sheet)}
                onKeyDown={(event) => {
                  if (event.key === "F2" && onRenameSheet) {
                    event.preventDefault();
                    beginSheetRename(sheet);
                  }
                }}
                aria-pressed={active}
                aria-keyshortcuts={onRenameSheet ? "F2" : undefined}
                title={onRenameSheet ? t("page.doc_editor.rename_sheet") : sheet.name}
                style={{ ...tabStyle, cursor: "pointer" }}
              >
                {sheet.name}
              </button>
            );
          })}
          {onAddSheet && (
            <button
              type="button"
              onClick={onAddSheet}
              title={t("page.doc_editor.add_sheet")}
              aria-label={t("page.doc_editor.add_sheet")}
              style={{
                ...sheetToolbarButtonBase,
                height: 28,
                minWidth: 28,
                padding: "0 8px",
                color: "var(--editor-active-text, #436b65)",
              }}
            >
              <IconPlus size={14} />
              {t("page.doc_editor.new_sheet")}
            </button>
          )}
        </div>
      )}
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
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
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
          </div>
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
      <div className="spreadsheet-editor-workspace" style={{ flex: 1, display: "flex", minHeight: 0, overflow: "hidden" }}>
        <div className="spreadsheet-editor-grid-pane" style={{ flex: 1, overflow: "auto", minWidth: 0 }}>
          <div style={{ position: "relative", width: "max-content", minWidth: "100%" }}>
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
                    style={{ ...shTh, width: renderedColumnWidths[i], minWidth: renderedColumnWidths[i], cursor: "cell" }}
                  >
                    {colLetter(i)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: visibleRows }, (_, ri) => {
                const row = data[ri] || [];
                const renderedRowHeight = renderedRowHeights[ri];
                return (
                <tr key={ri} style={{ height: renderedRowHeight }}>
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
                    const merge = spreadsheetMergeAt(merges, ri, ci);
                    if (merge.covered) return null;
                    const isActive = selected.r === ri && selected.c === ci;
                    const isInRange = sheetCellInRange(ri, ci, selectionRange);
                    const rawCellValue = row[ci] != null ? String(row[ci]) : "";
                    const isFormulaCell = rawCellValue.trim().startsWith("=");
                    const sourceValue = sourceDataRef.current[ri]?.[ci];
                    const currentValue = data[ri]?.[ci];
                    const sourceDisplay = initialDisplayData[ri]?.[ci];
                    const displayValue = !isFormulaCell
                      && Object.is(sourceValue == null ? "" : sourceValue, currentValue == null ? "" : currentValue)
                      && sourceDisplay != null
                      ? sourceDisplay
                      : getSpreadsheetDisplayValue(data, ri, ci, sourceDisplay, {
                          numberFormat: initialNumberFormats[sheetStyleKey(ri, ci)],
                          formatNumber: numberFormatter,
                          evaluationState: formulaEvaluationState,
                        });
                    const cellStyle = styles[sheetStyleKey(ri, ci)] || {};
                    const cellBackground = isInRange
                      ? `linear-gradient(rgba(79,125,117,0.10), rgba(79,125,117,0.10)), ${cellStyle.fill || "#ffffff"}`
                      : cellStyle.fill || "#ffffff";
                    const sharedTextStyle: React.CSSProperties = {
                      ...spreadsheetCellVisualStyle(cellStyle),
                      color: displayValue === "#ERROR" ? "#c14a44" : cellStyle.color || "#1c1917",
                      textAlign: cellStyle.align || "left",
                      border: "none",
                      borderTop: undefined, borderBottom: undefined, borderLeft: undefined, borderRight: undefined,
                    };
                    return (
                      <td
                        key={ci}
                        rowSpan={merge.rowSpan}
                        colSpan={merge.columnSpan}
                        onMouseDown={() => {
                          selectCell(ri, ci);
                          setIsSelecting(true);
                        }}
                        onMouseEnter={() => {
                          if (isSelecting) setSelected({ r: ri, c: ci });
                        }}
                        style={{
                          ...shTd,
                          ...(showGridlines ? {} : { borderTop: "1px solid transparent", borderBottom: "1px solid transparent", borderLeft: "1px solid transparent", borderRight: "1px solid transparent" }),
                          ...spreadsheetCellVisualStyle(cellStyle),
                          padding: 0,
                          background: cellBackground,
                          outline: isActive ? "2px solid #4f7d75" : isInRange ? "1px solid rgba(79,125,117,0.35)" : undefined,
                          outlineOffset: -2,
                          width: renderedColumnWidths[ci],
                          minWidth: renderedColumnWidths[ci],
                          height: renderedRowHeight,
                        }}
                      >
                        {isActive ? (
                          <textarea
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
                              height: renderedRowHeight,
                              padding: "5px 9px",
                              border: "none",
                              outline: "none",
                              background: "transparent",
                              resize: "none",
                              overflow: "hidden",
                              lineHeight: "normal",
                            }}
                          />
                        ) : (
                          <div
                            title={isFormulaCell ? rawCellValue : undefined}
                            style={{ ...sharedTextStyle, padding: "6px 10px", minHeight: renderedRowHeight, cursor: "cell", overflowWrap: "anywhere", overflow: "hidden", display: "flex", flexDirection: "column", justifyContent: cellStyle.verticalAlign === "middle" ? "center" : cellStyle.verticalAlign === "bottom" ? "flex-end" : "flex-start" }}
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
          <SpreadsheetImageLayer
            images={nativeImages}
            columnWidths={renderedColumnWidths}
            rowHeights={renderedRowHeights}
          />
          </div>
        </div>

        {nativeCharts.length > 0 && (
          <aside className="spreadsheet-editor-chart-pane spreadsheet-editor-chart-pane--native" style={{ width: "min(44vw, 520px)", minWidth: 360, maxWidth: "100%", borderLeft: "1px solid var(--border-subtle, rgba(28,25,23,0.06))", background: "var(--surface-muted, #fafaf9)", overflow: "auto", padding: 12, flexShrink: 0 }}>
            <SpreadsheetChartPreview charts={nativeCharts} />
          </aside>
        )}
        {showChartPanel && <aside className="spreadsheet-editor-chart-pane spreadsheet-editor-chart-pane--config" style={{ width: "min(100%, 360px)", maxWidth: "100%", borderLeft: "1px solid var(--border-subtle, rgba(28,25,23,0.06))", background: "var(--surface-muted, #fafaf9)", overflow: "auto", flexShrink: 0 }}>
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
  sourceMap?: PresentationTextSourceMap;
  strikethrough?: boolean;
  fontSize?: number; color?: string; align?: string;
  fontFamily?: string; bullet?: string; indent?: number; indentRight?: number; hanging?: number;
  lineSpacing?: number; // multiplier (1.0 = single)
  spaceBefore?: number; // pt
  spaceAfter?: number; // pt
  baseline?: number; // superscript (+) / subscript (-)
  spacing?: number; // letter spacing in pt
  runs?: { text: string; bold?: boolean; italic?: boolean; underline?: boolean; strikethrough?: boolean; fontSize?: number; color?: string; fontFamily?: string; baseline?: number; spacing?: number }[];
}

type PptxInlineTextRun = NonNullable<PptxTextRun["runs"]>[number];

interface PptxTableCell {
  text: string; bold?: boolean; italic?: boolean; color?: string; fill?: string;
  fontSize?: number; fontFamily?: string;
  sourceMap?: PresentationTextSourceMap;
  gridSpan?: number; vMerge?: boolean;
}

interface PptxShapeSource {
  part: string;
  kind: "sp" | "pic" | "cxnSp" | "graphicFrame";
  objectId: string;
  cloneOfObjectId?: string;
  editable: boolean;
  mediaPart?: string;
  groupTransform?: PresentationGroupTransform;
  groupPath?: string[];
}

interface PptxShapeInheritance {
  masterShape?: string;
  layoutShape?: string;
  textStyles: PresentationTextLevelStyle[];
}

interface PptxShape {
  id: string;
  type?: "shape" | "table" | "image" | "graphic";
  x: number; y: number; w: number; h: number;
  fill?: string;
  gradFill?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  borderRadius?: number;
  opacity?: number;
  rotation?: number;
  stroke?: string;
  strokeWidth?: number;
  strokeDash?: string;
  presetGeom?: string; // oval, triangle, diamond, etc.
  flipH?: boolean;
  flipV?: boolean;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  imgCrop?: { l: number; t: number; r: number; b: number };
  vAlign?: "top" | "middle" | "bottom";
  wordWrap?: boolean;
  padding?: { l: number; t: number; r: number; b: number };
  texts: PptxTextRun[];
  imgUrl?: string;
  altText?: string;
  videoUrl?: string;
  hyperlink?: string;
  imageFit?: "cover" | "contain" | "fill";
  graphicKind?: "chart" | "diagram" | "embedded" | "unknown";
  graphicPreviewUrl?: string;
  graphicPreviewStatus?: "loading" | "failed";
  // Table data
  tableRows?: PptxTableCell[][];
  tableCols?: number;
  tableColWidths?: number[];
  tableRowHeights?: number[];
  source?: PptxShapeSource;
}

interface PptxEditorThemeSnapshot {
  colors: Record<string, string>;
  majorFont?: string;
  minorFont?: string;
}

interface PptxSlide {
  id: string;
  bg?: string;
  bgGrad?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  bgImgUrl?: string;
  aspectRatio?: string;
  heightPoints?: number;
  notes?: string;
  shapes: PptxShape[];
  sourcePart?: string;
  notesPart?: string;
  theme?: PptxEditorThemeSnapshot;
}

function mergePresentationSavedIdentity(
  currentSlides: PptxSlide[],
  savedSlides: PptxSlide[],
  savingSlides: PptxSlide[],
): PptxSlide[] {
  const savedSlideById = new Map(savedSlides.map((slide) => [slide.id, slide]));
  const savingSlideById = new Map(savingSlides.map((slide) => [slide.id, slide]));
  return currentSlides.map((slide) => {
    const savedSlide = savedSlideById.get(slide.id);
    if (!savedSlide) return slide;
    const savedShapeById = new Map(savedSlide.shapes.map((shape) => [shape.id, shape]));
    const savingShapeById = new Map(
      (savingSlideById.get(slide.id)?.shapes || []).map((shape) => [shape.id, shape]),
    );
    return {
      ...slide,
      sourcePart: savedSlide.sourcePart || slide.sourcePart,
      notesPart: savedSlide.notesPart || slide.notesPart,
      shapes: slide.shapes.map((shape) => {
        const savedShape = savedShapeById.get(shape.id);
        if (!savedShape) return shape;
        const savingShape = savingShapeById.get(shape.id);
        const texts = shape.texts.map((paragraph, index) => {
          const savedParagraph = savedShape.texts[index];
          if (!savedParagraph) return paragraph;
          if (savedParagraph.text !== paragraph.text) {
            return {
              ...paragraph,
              sourceMap: rebasePresentationTextSourceMap(
                savedParagraph.text,
                paragraph.text,
                savingShape?.texts[index]?.sourceMap,
                paragraph.sourceMap,
              ),
            };
          }
          const { sourceMap: _savedEditProvenance, ...savedParagraphState } = paragraph;
          return savedParagraphState;
        });
        const tableRows = shape.tableRows?.map((row, rowIndex) => row.map((cell, cellIndex) => {
          const savedCell = savedShape.tableRows?.[rowIndex]?.[cellIndex];
          if (!savedCell) return cell;
          if (savedCell.text !== cell.text) {
            return {
              ...cell,
              sourceMap: rebasePresentationTextSourceMap(
                savedCell.text,
                cell.text,
                savingShape?.tableRows?.[rowIndex]?.[cellIndex]?.sourceMap,
                cell.sourceMap,
              ),
            };
          }
          const { sourceMap: _savedEditProvenance, ...savedCellState } = cell;
          return savedCellState;
        }));
        return {
          ...shape,
          texts,
          ...(tableRows ? { tableRows } : {}),
          ...(savedShape.source ? { source: structuredClone(savedShape.source) } : {}),
        };
      }),
    };
  });
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

function pptxXmlElement(xml: string, tag: string): string | null {
  const m = xml.match(new RegExp(`<${tag}\\b[^>]*(?:\\/>|>[\\s\\S]*?<\\/${tag}>)`, "i"));
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
  _editorLineStyles = [];
  _editorEffectStyles = [];
  try {
    const extractFills = (xml: string) => (xml.match(/<a:(solidFill|gradFill|pattFill|blipFill)[\s>][\s\S]*?<\/a:\1>/g) || []);
    const bgFillLst = pptxXmlInner(themeXml, "a:bgFillStyleLst");
    if (bgFillLst) _editorBgFillStyles = [...extractFills(bgFillLst)];
    const fillLst = pptxXmlInner(themeXml, "a:fillStyleLst");
    if (fillLst) _editorFillStyles = [...extractFills(fillLst)];
    const lineLst = pptxXmlInner(themeXml, "a:lnStyleLst");
    if (lineLst) _editorLineStyles = [...(lineLst.match(/<a:ln[\s>][\s\S]*?<\/a:ln>/g) || [])];
    const effectLst = pptxXmlInner(themeXml, "a:effectStyleLst");
    if (effectLst) _editorEffectStyles = [...(effectLst.match(/<a:effectStyle[\s>][\s\S]*?<\/a:effectStyle>/g) || [])];
  } catch { /* non-fatal */ }

  return colors;
}

let _activeTheme: Record<string, string> = DEFAULT_SCHEME;
let _editorMajorFont = "";
let _editorMinorFont = "";
let _editorBgFillStyles: string[] = [];
let _editorFillStyles: string[] = [];
let _editorLineStyles: string[] = [];
let _editorEffectStyles: string[] = [];

function pptxParseColor(xml: string, phClrOverride?: string): string | null {
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
    const base = m[1] === "phClr" && phClrOverride
      ? phClrOverride
      : _activeTheme[m[1]] || DEFAULT_SCHEME[m[1]] || "#57534e";
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
  if (typeface === "+mj-lt" || typeface === "+mj-ea" || typeface === "+mj-cs") {
    return officeCompatibleFontFamily(_editorMajorFont || undefined);
  }
  if (typeface === "+mn-lt" || typeface === "+mn-ea" || typeface === "+mn-cs") {
    return officeCompatibleFontFamily(_editorMinorFont || undefined);
  }
  return officeCompatibleFontFamily(typeface);
}

function editorShapeStyleReference(
  shapeXml: string,
  kind: "fill" | "ln" | "effect",
): { index: number; color?: string } | undefined {
  const style = pptxXmlInner(shapeXml, "p:style");
  const reference = style ? pptxXmlElement(style, `a:${kind}Ref`) : null;
  if (!reference) return undefined;
  const index = parseInt(pptxXmlAttr(reference, "idx") || "0", 10);
  if (!Number.isFinite(index) || index <= 0) return undefined;
  return { index, color: pptxParseColor(reference) || undefined };
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

function pptxShapeAsTopLevel(shape: PptxShape): PptxShape {
  const groupTransform = shape.source?.groupTransform;
  if (!groupTransform) return shape;
  const sourceRect = {
    x: (shape.x / 100) * EDITOR_SLIDE_W,
    y: (shape.y / 100) * EDITOR_SLIDE_H,
    width: (shape.w / 100) * EDITOR_SLIDE_W,
    height: (shape.h / 100) * EDITOR_SLIDE_H,
    rotation: shape.rotation,
    flipH: shape.flipH,
    flipV: shape.flipV,
  };
  const transform = presentationShapeTransform(groupTransform, sourceRect);
  const horizontalScale = Math.hypot(transform.a, transform.b);
  const verticalScale = Math.hypot(transform.c, transform.d);
  const axisDot = transform.a * transform.c + transform.b * transform.d;
  if (Math.abs(axisDot) > Math.max(1e-9, horizontalScale * verticalScale * 1e-6)) {
    throw new Error("This grouped object cannot be flattened without changing its affine geometry.");
  }
  const center = presentationTransformPoint(
    transform,
    sourceRect.x + sourceRect.width / 2,
    sourceRect.y + sourceRect.height / 2,
  );
  const width = horizontalScale * sourceRect.width;
  const height = verticalScale * sourceRect.height;
  return {
    ...shape,
    x: emu2pctX(center.x - width / 2),
    y: emu2pctY(center.y - height / 2),
    w: emu2pctX(width),
    h: emu2pctY(height),
    rotation: Math.atan2(transform.b, transform.a) * 180 / Math.PI,
    flipH: false,
    flipV: transform.a * transform.d - transform.b * transform.c < 0,
  };
}

function clonePptxShape(shape: PptxShape, offset = 0, preserveEditableSource = true): PptxShape {
  const preserveSource = preserveEditableSource && Boolean(shape.source?.editable);
  const clone = JSON.parse(JSON.stringify(preserveSource ? shape : pptxShapeAsTopLevel(shape))) as PptxShape;
  const id = genId();
  if (preserveSource && clone.source) {
    clone.source.cloneOfObjectId = shape.source?.cloneOfObjectId || shape.source?.objectId;
    clone.source.objectId = `clone-${id}`;
  } else {
    delete clone.source;
  }
  const localOffset = preserveSource ? pptxShapeLocalDelta(shape, offset, offset) : { dx: offset, dy: offset };
  return {
    ...clone,
    id,
    x: preserveSource ? clone.x + localOffset.dx : Math.max(0, Math.min(100 - clone.w, clone.x + offset)),
    y: preserveSource ? clone.y + localOffset.dy : Math.max(0, Math.min(100 - clone.h, clone.y + offset)),
  };
}

/** Parse shape stroke/border */
function pptxParseStroke(xml: string, phClrOverride?: string, inherited: { color?: string; width?: number; dash?: string } = {}): { color?: string; width?: number; dash?: string } {
  const ln = pptxXmlInner(xml, "a:ln");
  if (!ln) return {};
  // Check for noFill (no stroke)
  if (ln.includes("<a:noFill")) return {};
  const parsedColor = pptxParseColor(ln, phClrOverride);
  const color = parsedColor ? presentationColorWithAlpha(parsedColor, ln) : inherited.color;
  const wAttr = pptxXmlAttr(ln, "w");
  const width = wAttr ? parseInt(wAttr, 10) / 12700 : inherited.width ?? 1; // EMU → pt
  return { color: color || undefined, width: color ? width : undefined, dash: presentationStrokeDash(ln) ?? inherited.dash };
}

/** Parse text runs with enhanced properties */
function pptxParseTextRuns(spXml: string, inheritedStyles: PresentationTextLevelStyle[] = []): PptxTextRun[] {
  const texts: PptxTextRun[] = [];
  const paras = spXml.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || [];
  for (const para of paras) {
    const pPr = pptxXmlElement(para, "a:pPr");
    const lvl = pPr ? parseInt(pptxXmlAttr(pPr, "lvl") || "0", 10) : 0;
    const inherited = inheritedStyles[lvl] || inheritedStyles[0] || {};
    const align = pPr ? (pptxXmlAttr(pPr, "algn") || inherited.align) : inherited.align;
    const marL = pPr ? pptxXmlAttr(pPr, "marL") : null;
    const indent = marL != null ? parseInt(marL, 10) / 12700 : inherited.indent ?? (lvl > 0 ? lvl * 18 : undefined);
    const marR = pPr ? pptxXmlAttr(pPr, "marR") : null;
    const indentRight = marR != null ? parseInt(marR, 10) / 12700 : inherited.indentRight;
    const firstLineIndent = pPr ? pptxXmlAttr(pPr, "indent") : null;
    const hanging = firstLineIndent ? parseInt(firstLineIndent, 10) / 12700 : inherited.hanging;

    // Detect bullet
    let bullet: string | undefined = inherited.bullet == null ? undefined : inherited.bullet;
    if (pPr) {
      const buChar = pPr.match(/<a:buChar char="([^"]+)"/);
      if (buChar) bullet = pptxDecodeText(buChar[1]);
      else if (pPr.includes("<a:buAutoNum")) {
        const autoNumType = (pPr.match(/<a:buAutoNum type="([^"]+)"/) || [])[1] || "arabicPeriod";
        if (autoNumType.startsWith("alpha")) bullet = "a.";
        else if (autoNumType.startsWith("roman")) bullet = "i.";
        else bullet = "#.";
      }
      // buNone means explicitly no bullet
      else if (pPr.includes("<a:buNone")) bullet = undefined;
      else if (!bullet && lvl > 0) bullet = "\u2022";
    }

    // Line/paragraph spacing
    let lineSpacing: number | undefined = inherited.lineSpacing;
    let spaceBefore: number | undefined = inherited.spaceBefore;
    let spaceAfter: number | undefined = inherited.spaceAfter;
    if (pPr) {
      const paragraphDefaultRun = pptxXmlElement(pPr, "a:defRPr");
      const paragraphFontSize = paragraphDefaultRun
        ? parseInt(pptxXmlAttr(paragraphDefaultRun, "sz") || "0", 10) / 100 || inherited.fontSize || 12
        : inherited.fontSize || 12;
      const spacingPoints = (spacingXml: string | null): number | undefined => {
        if (!spacingXml) return undefined;
        const points = spacingXml.match(/<a:spcPts val="(\d+)"/);
        if (points) return parseInt(points[1], 10) / 100;
        const percent = spacingXml.match(/<a:spcPct val="(\d+)"/);
        return percent ? paragraphFontSize * (parseInt(percent[1], 10) / 100000) : undefined;
      };
      const lnSpc = pptxXmlInner(pPr, "a:lnSpc");
      if (lnSpc) {
        const spcPct = lnSpc.match(/<a:spcPct val="(\d+)"/);
        if (spcPct) lineSpacing = parseInt(spcPct[1], 10) / 100000;
        const spcPts = lnSpc.match(/<a:spcPts val="(\d+)"/);
        if (spcPts) lineSpacing = parseInt(spcPts[1], 10) / 100 / paragraphFontSize;
      }
      const spcBef = pptxXmlInner(pPr, "a:spcBef");
      if (spcBef) spaceBefore = spacingPoints(spcBef);
      const spcAft = pptxXmlInner(pPr, "a:spcAft");
      if (spcAft) spaceAfter = spacingPoints(spcAft);
    }

    // Parse default paragraph text properties (defRPr and endParaRPr)
    let defFontSize = inherited.fontSize, defColor = inherited.color;
    let defBold = Boolean(inherited.bold), defItalic = Boolean(inherited.italic);
    let defUnderline = Boolean(inherited.underline), defStrike = Boolean(inherited.strikethrough);
    let defBaseline = inherited.baseline, defSpacing = inherited.spacing, defFontFamily = inherited.fontFamily;
    const defRPr = pPr ? pptxXmlElement(pPr, "a:defRPr") : null;
    if (defRPr) {
      const szM = pptxXmlAttr(defRPr, "sz");
      if (szM) defFontSize = parseInt(szM, 10) / 100;
      const c = pptxParseColor(defRPr);
      if (c) defColor = presentationColorWithAlpha(c, defRPr);
      const bold = pptxXmlAttr(defRPr, "b");
      const italic = pptxXmlAttr(defRPr, "i");
      const underline = pptxXmlAttr(defRPr, "u");
      const strike = pptxXmlAttr(defRPr, "strike");
      if (bold != null) defBold = bold === "1";
      if (italic != null) defItalic = italic === "1";
      if (underline != null) defUnderline = underline !== "none";
      if (strike != null) defStrike = strike !== "noStrike";
      const baseline = pptxXmlAttr(defRPr, "baseline");
      if (baseline) defBaseline = parseInt(baseline, 10) / 1000;
      const spacing = pptxXmlAttr(defRPr, "spc");
      if (spacing) defSpacing = parseInt(spacing, 10) / 100;
      const latin = defRPr.match(/<a:latin typeface="([^"]+)"/);
      const ea = defRPr.match(/<a:ea typeface="([^"]+)"/);
      defFontFamily = editorResolveFont(latin?.[1]) || editorResolveFont(ea?.[1]) || defFontFamily;
    }
    const endParaRPr = pptxXmlElement(para, "a:endParaRPr");
    if (endParaRPr) {
      const szM = pptxXmlAttr(endParaRPr, "sz");
      if (szM && !defFontSize) defFontSize = parseInt(szM, 10) / 100;
      const c = pptxParseColor(endParaRPr);
      if (c && !defColor) defColor = presentationColorWithAlpha(c, endParaRPr);
    }

    // Collect runs and line breaks in document order
    const tokens = para.match(/<a:r[\s>][\s\S]*?<\/a:r>|<a:br\s*\/>|<a:br[\s>][\s\S]*?<\/a:br>|<a:fld[\s>][\s\S]*?<\/a:fld>/g) || [];
    const runs: NonNullable<PptxTextRun["runs"]> = [];
    let paraText = "";
    let firstBold = defBold, firstItalic = defItalic, firstUnderline = defUnderline, firstStrike = defStrike;
    let firstBaseline = defBaseline, firstSpacing = defSpacing;
    let firstFontSize = defFontSize, firstColor = defColor, firstFontFamily = defFontFamily;
    let isFirst = true;
    for (const token of tokens) {
      if (token.startsWith("<a:br")) { paraText += "\n"; runs.push({ text: "\n" }); continue; }
      let rBold = defBold, rItalic = defItalic, rUnderline = defUnderline;
      let rFontSize = defFontSize, rColor = defColor, rFontFamily = defFontFamily;
      let rStrike = defStrike, rBaseline = defBaseline, rSpacing = defSpacing;
      const rPr = pptxXmlElement(token, "a:rPr");
      if (rPr) {
        const bold = pptxXmlAttr(rPr, "b");
        const italic = pptxXmlAttr(rPr, "i");
        const underline = pptxXmlAttr(rPr, "u");
        const strike = pptxXmlAttr(rPr, "strike");
        if (bold != null) rBold = bold === "1";
        if (italic != null) rItalic = italic === "1";
        if (underline != null) rUnderline = underline !== "none";
        if (strike != null) rStrike = strike !== "noStrike";
        const szM = pptxXmlAttr(rPr, "sz");
        if (szM) rFontSize = parseInt(szM, 10) / 100;
        const rc = pptxParseColor(rPr);
        if (rc) rColor = presentationColorWithAlpha(rc, rPr);
        const latin = rPr.match(/<a:latin typeface="([^"]+)"/);
        const ea = rPr.match(/<a:ea typeface="([^"]+)"/);
        rFontFamily = editorResolveFont(latin?.[1]) || editorResolveFont(ea?.[1]) || rFontFamily;
        const baselineM = pptxXmlAttr(rPr, "baseline");
        if (baselineM) rBaseline = parseInt(baselineM, 10) / 1000;
        const spcM = pptxXmlAttr(rPr, "spc");
        if (spcM) rSpacing = parseInt(spcM, 10) / 100;
      }
      const tMatch = token.match(/<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/);
      const runText = tMatch ? pptxDecodeText(tMatch[1]) : "";
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
        firstStrike = rStrike; firstBaseline = rBaseline; firstSpacing = rSpacing;
        firstFontSize = rFontSize; firstColor = rColor; firstFontFamily = rFontFamily;
        isFirst = false;
      }
    }

    if (paraText.trim() || paraText.includes("\n")) {
      texts.push({
        text: paraText, bold: firstBold, italic: firstItalic, underline: firstUnderline,
        strikethrough: firstStrike, baseline: firstBaseline, spacing: firstSpacing,
        fontSize: firstFontSize, color: firstColor, fontFamily: firstFontFamily,
        align: align === "ctr" ? "center" : align === "r" ? "right" : align === "just" ? "justify" : align === "l" ? "left" : undefined,
        bullet, indent, indentRight,
        hanging,
        lineSpacing, spaceBefore, spaceAfter,
        runs: runs.length > 1 ? runs : undefined,
      });
    } else {
      texts.push({
        text: "",
        fontSize: firstFontSize || defFontSize || 12,
        fontFamily: firstFontFamily,
        color: firstColor,
        bold: firstBold, italic: firstItalic, underline: firstUnderline,
        strikethrough: firstStrike,
        align: align === "ctr" ? "center" : align === "r" ? "right" : align === "just" ? "justify" : align === "l" ? "left" : undefined,
        bullet, indent, indentRight, hanging,
        lineSpacing,
        spaceBefore,
        spaceAfter,
      });
    }
  }
  return texts;
}

function pptxDecodeText(value: string): string {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_, hex: string) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, decimal: string) => String.fromCodePoint(parseInt(decimal, 10)))
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
function pptxParseTable(xml: string): { rows: PptxTableCell[][]; cols: number; colWidths?: number[]; rowHeights?: number[] } | null {
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
  const rowHeights: number[] = [];
  let maxCols = 0;
  for (const tr of trMatches) {
    rowHeights.push(parseInt((tr.match(/^<a:tr\b[^>]*\bh="(\d+)"/) || [])[1] || "0", 10));
    const tcMatches = tr.match(/<a:tc[\s>][\s\S]*?<\/a:tc>/g) || [];
    const row: PptxTableCell[] = [];
    for (const tc of tcMatches) {
      const text = (tc.match(/<a:p[\s>][\s\S]*?<\/a:p>/g) || []).map((paragraph) => (
        (paragraph.match(/<a:t(?:\s[^>]*)?>[\s\S]*?<\/a:t>|<a:br\b[^>]*(?:\/>|>[\s\S]*?<\/a:br>)/g) || []).map((token) => (
          token.startsWith("<a:br")
            ? "\n"
            : pptxDecodeText(token.replace(/^<a:t(?:\s[^>]*)?>|<\/a:t>$/g, ""))
        )).join("")
      )).join("\n");
      const paragraphs = pptxParseTextRuns(tc);
      const textStyle = paragraphs.find((paragraph) => paragraph.text.length > 0) || paragraphs[0];
      const tcPr = pptxXmlElement(tc, "a:tcPr");
      const fill = tcPr ? pptxParseColor(tcPr) : null;
      const gridSpanM = tc.match(/gridSpan="(\d+)"/);
      const vMerge = tc.includes('vMerge="1"') || tc.includes('hMerge="1"');
      row.push({ text, bold: textStyle?.bold, italic: textStyle?.italic, fontSize: textStyle?.fontSize,
        fontFamily: textStyle?.fontFamily, color: textStyle?.color, fill: fill || undefined,
        gridSpan: gridSpanM ? parseInt(gridSpanM[1], 10) : undefined, vMerge: vMerge || undefined });
    }
    rows.push(row);
    maxCols = Math.max(maxCols, row.length);
  }
  return rows.length > 0 ? { rows, cols: maxCols, colWidths, rowHeights } : null;
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

function editorShapeInheritance(
  shapeXml: string,
  layoutXml?: string,
  masterXml?: string,
): PptxShapeInheritance {
  const layoutShape = findPresentationPlaceholderShape(layoutXml, shapeXml);
  return {
    masterShape: findPresentationPlaceholderShape(masterXml, layoutShape || shapeXml),
    layoutShape,
    textStyles: presentationInheritedTextStyleLevels(shapeXml, layoutXml, masterXml, {
      color: (xml) => {
        const color = pptxParseColor(xml);
        return color ? presentationColorWithAlpha(color, xml) : undefined;
      },
      font: editorResolveFont,
    }),
  };
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

function pptxParseShapeXml(
  sp: string,
  relsMap: Map<string, string>,
  phMap?: Map<string, { x: number; y: number; w: number; h: number }>,
  source?: Omit<PptxShapeSource, "objectId" | "mediaPart"> & { mediaParts?: Map<string, string> },
  inheritance?: PptxShapeInheritance,
): PptxShape | null {
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
  const nonVisualProperties = sp.match(/<p:cNvPr\b[^>]*\/?\s*>/);
  const altText = nonVisualProperties ? pptxXmlAttr(nonVisualProperties[0], "descr") : null;
  if (altText != null) shape.altText = pptxDecodeText(altText);
  if (source) {
    const objectId = nonVisualProperties ? pptxXmlAttr(nonVisualProperties[0], "id") : null;
    if (objectId) {
      shape.source = {
        part: source.part,
        kind: source.kind,
        objectId,
        editable: source.editable,
      };
    }
  }

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
  const shapeFillScope = presentationShapeFillScope(spPr);
  const hasExplicitFill = /<a:(?:noFill|solidFill|gradFill|blipFill|pattFill)\b/i.test(shapeFillScope);
  const hasNoFill = shapeFillScope.includes("<a:noFill");
  if (!hasNoFill) {
    const solidFill = pptxXmlInner(shapeFillScope, "a:solidFill");
    if (solidFill) {
      const fillColor = pptxParseColor(solidFill);
      shape.fill = fillColor ? presentationColorWithAlpha(fillColor, solidFill) : undefined;
    }
    shape.gradFill = pptxParseGradient(shapeFillScope);

    if (!hasExplicitFill) {
      const fillReference = editorShapeStyleReference(sp, "fill");
      const themedFill = fillReference ? _editorFillStyles[fillReference.index - 1] : undefined;
      if (themedFill?.includes("<a:solidFill")) {
        const fillColor = pptxParseColor(themedFill, fillReference?.color);
        shape.fill = fillColor ? presentationColorWithAlpha(fillColor, themedFill) : undefined;
      } else if (themedFill?.includes("<a:gradFill")) {
        shape.gradFill = pptxParseGradient(themedFill, fillReference?.color);
      }
    }

    // Blip fill (texture/image fill on shapes)
    if (!shape.fill && !shape.gradFill) {
      const blipFill = pptxXmlInner(spPr, "a:blipFill") || pptxXmlInner(sp, "p:blipFill");
      if (blipFill) {
        const blipM = blipFill.match(/<a:blip r:embed="([^"]+)"/);
        if (blipM) {
          const imgUrl = relsMap.get(blipM[1]);
          if (imgUrl) shape.imgUrl = imgUrl;
          if (shape.source) shape.source.mediaPart = source?.mediaParts?.get(blipM[1]);
        }
      }
    }
  } else {
    shape.fill = "transparent";
  }

  // Stroke/border
  const explicitLine = pptxXmlInner(spPr, "a:ln");
  const lineReference = editorShapeStyleReference(sp, "ln");
  const themedLine = lineReference ? _editorLineStyles[lineReference.index - 1] : undefined;
  const inheritedStroke = themedLine ? pptxParseStroke(themedLine, lineReference?.color) : {};
  const stroke = explicitLine ? pptxParseStroke(explicitLine, undefined, inheritedStroke) : inheritedStroke;
  if (stroke.color) { shape.stroke = stroke.color; shape.strokeWidth = stroke.width; shape.strokeDash = stroke.dash; }

  // Shadow (outer shadow)
  const explicitShadow = pptxXmlInner(spPr, "a:outerShdw");
  const effectReference = !/<a:(?:effectLst|effectDag)\b/.test(spPr) ? editorShapeStyleReference(sp, "effect") : undefined;
  const themedEffect = effectReference ? _editorEffectStyles[effectReference.index - 1] : undefined;
  const outerShdw = explicitShadow || (themedEffect ? pptxXmlInner(themedEffect, "a:outerShdw") : null);
  if (outerShdw) {
    const shdwBlur = parseInt(pptxXmlAttr(outerShdw, "blurRad") || "0", 10) / 12700;
    const shdwDist = parseInt(pptxXmlAttr(outerShdw, "dist") || "0", 10) / 12700;
    const shdwAng = parseInt(pptxXmlAttr(outerShdw, "dir") || "0", 10) / 60000;
    const shdwColor = pptxParseColor(outerShdw, effectReference?.color) || "#000000";
    const shdwAlphaM = outerShdw.match(/<a:alpha val="(\d+)"/);
    const shdwAlpha = shdwAlphaM ? parseInt(shdwAlphaM[1], 10) / 100000 : 0.4;
    shape.shadow = { blur: shdwBlur, dist: shdwDist, angle: shdwAng, color: shdwColor, alpha: shdwAlpha };
  }

  // Border radius
  if (sp.includes('prst="roundRect"')) {
    const adjM = sp.match(/name="adj" fmla="val (\d+)"/);
    shape.borderRadius = adjM ? Math.min(50, parseInt(adjM[1], 10) / 1000) : 16.667;
  }

  // Text body properties (vertical alignment + insets)
  const bodyProperties = [inheritance?.masterShape, inheritance?.layoutShape, sp]
    .map((shapeXml) => shapeXml ? pptxXmlElement(shapeXml, "a:bodyPr") : null)
    .filter((value): value is string => Boolean(value));
  if (bodyProperties.length > 0) {
    const inheritedBodyAttr = (name: string) => {
      for (let index = bodyProperties.length - 1; index >= 0; index -= 1) {
        const value = pptxXmlAttr(bodyProperties[index], name);
        if (value != null) return value;
      }
      return null;
    };
    const anchor = inheritedBodyAttr("anchor");
    const wrap = inheritedBodyAttr("wrap");
    if (wrap === "none" || wrap === "square") shape.wordWrap = wrap === "square";
    if (anchor === "t") shape.vAlign = "top";
    else if (anchor === "b") shape.vAlign = "bottom";
    else if (anchor === "ctr") shape.vAlign = "middle";
    const lIns = inheritedBodyAttr("lIns");
    const tIns = inheritedBodyAttr("tIns");
    const rIns = inheritedBodyAttr("rIns");
    const bIns = inheritedBodyAttr("bIns");
    shape.padding = {
      l: lIns != null ? parseInt(lIns, 10) / 12700 : 7.2,
      t: tIns != null ? parseInt(tIns, 10) / 12700 : 3.6,
      r: rIns != null ? parseInt(rIns, 10) / 12700 : 7.2,
      b: bIns != null ? parseInt(bIns, 10) / 12700 : 3.6,
    };
  }

  // Text
  shape.texts = pptxParseTextRuns(sp, inheritance?.textStyles);

  // Image (p:pic blip)
  if (!shape.imgUrl) {
    const blipM = sp.match(/<a:blip r:embed="([^"]+)"/);
    if (blipM) {
      const imgUrl = relsMap.get(blipM[1]);
      if (imgUrl) { shape.imgUrl = imgUrl; shape.type = "image"; }
      if (shape.source) shape.source.mediaPart = source?.mediaParts?.get(blipM[1]);
    }
  }
  if (shape.imgUrl && (shape.source?.kind === "pic" || /^\s*<p:pic\b/.test(sp))) {
    shape.type = "image";
  }
  shape.videoUrl = presentationVideoSource(sp, relsMap);
  // Image cropping (srcRect)
  if (shape.imgUrl) {
    const srcRect = sp.match(/<a:srcRect\s+([^/]*)\/>/);
    if (srcRect) {
      const attrs = srcRect[1];
      const l = parseInt((attrs.match(/l="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      const t = parseInt((attrs.match(/t="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      const r = parseInt((attrs.match(/r="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      const b = parseInt((attrs.match(/b="(-?\d+)"/) || [])[1] || "0", 10) / 1000;
      if (l || t || r || b) shape.imgCrop = { l, t, r, b };
    }
    const blip = sp.match(/<a:blip\b[^>]*(?:\/>|>[\s\S]*?<\/a:blip>)/i)?.[0];
    const imageAlpha = blip?.match(/<a:alphaModFix\b[^>]*\bamt="(\d+)"/i)?.[1];
    if (imageAlpha) shape.opacity = Math.max(0, Math.min(1, parseInt(imageAlpha, 10) / 100000));
    shape.imageFit = shape.imgCrop ? "cover" : "fill";
  }

  return shape;
}

interface PptxEditorParseOptions {
  isCancelled?: () => boolean;
}

let pptxEditorParseQueue: Promise<void> = Promise.resolve();

async function parsePptxForEditor(
  buf: ArrayBuffer,
  options: PptxEditorParseOptions = {},
): Promise<PptxSlide[]> {
  const parseRun = pptxEditorParseQueue.then(async () => {
    if (options.isCancelled?.()) return [];
    return parsePptxForEditorUnlocked(buf, options);
  });
  pptxEditorParseQueue = parseRun.then(() => undefined, () => undefined);
  return await parseRun;
}

async function parsePptxForEditorUnlocked(
  buf: ArrayBuffer,
  options: PptxEditorParseOptions,
): Promise<PptxSlide[]> {
  if (options.isCancelled?.()) return [];
  EDITOR_SLIDE_W = 12192000;
  EDITOR_SLIDE_H = 6858000;
  _activeTheme = DEFAULT_SCHEME;
  _editorMajorFont = "";
  _editorMinorFont = "";
  _editorBgFillStyles = [];
  _editorFillStyles = [];
  _editorLineStyles = [];
  _editorEffectStyles = [];

  const JSZip = (await import("jszip")).default;
  const zip = await JSZip.loadAsync(buf);
  if (!zip.file("ppt/presentation.xml")) {
    throw new Error("The PPTX package has no presentation.xml part.");
  }
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
          const resolved = resolvePresentationPartTarget("ppt/presentation.xml", target);
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
        const mime = presentationMediaMime(name);
        mediaCache.set(name, `data:${mime};base64,${data}`);
      } catch { /* skip bad media */ }
    }
  }

  // Pre-parse slide layouts and slide masters (non-fatal)
  const layoutCache = new Map<string, string>();
  const masterCache = new Map<string, string>();
  const layoutToMasterPath = new Map<string, string>();
  const layoutRelsCache = new Map<string, Map<string, string>>();
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
        const lre = zip.file(presentationRelationshipsPart(layoutPath));
        if (lre) {
          const layoutRels = new Map<string, string>();
          for (const rel of ((await lre.async("text")).match(/<Relationship[^>]*\/>/g) || [])) {
            const id = pptxXmlAttr(rel, "Id");
            const target = pptxXmlAttr(rel, "Target");
            const type = pptxXmlAttr(rel, "Type");
            if (target) {
              const resolved = resolvePresentationPartTarget(layoutPath, target);
              if (type?.includes("slideMaster")) layoutToMasterPath.set(layoutPath, resolved);
              if (id && mediaCache.has(resolved)) layoutRels.set(id, mediaCache.get(resolved)!);
            }
          }
          layoutRelsCache.set(layoutPath, layoutRels);
        }
      } catch { /* non-fatal */ }
    }
    // Pre-cache master rels for media
    for (const masterPath of masterCache.keys()) {
      try {
        const mre = zip.file(presentationRelationshipsPart(masterPath));
        if (mre) {
          const masterRels = new Map<string, string>();
          for (const rel of ((await mre.async("text")).match(/<Relationship[^>]*\/>/g) || [])) {
            const id = pptxXmlAttr(rel, "Id");
            const target = pptxXmlAttr(rel, "Target");
            if (id && target) {
              const resolved = resolvePresentationPartTarget(masterPath, target);
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
      const relsMap = new Map<string, string>();
      const mediaParts = new Map<string, string>();
      let layoutPath: string | undefined;
      let notesPath: string | undefined;
      try {
        const relsEntry = zip.file(presentationRelationshipsPart(slidePath));
        if (relsEntry) {
          const relsXml = await relsEntry.async("text");
          for (const rel of (relsXml.match(/<Relationship[^>]*\/>/g) || [])) {
            const id = pptxXmlAttr(rel, "Id");
            const target = pptxXmlAttr(rel, "Target");
            const type = pptxXmlAttr(rel, "Type");
            if (id && target) {
              const resolved = resolvePresentationPartTarget(slidePath, target);
              if (mediaCache.has(resolved)) {
                relsMap.set(id, mediaCache.get(resolved)!);
                mediaParts.set(id, resolved);
              }
              if (type && type.includes("slideLayout")) layoutPath = resolved;
              if (type && type.includes("notesSlide")) notesPath = resolved;
            }
          }
        }
        if (layoutPath) {
          const lre = zip.file(presentationRelationshipsPart(layoutPath));
          if (lre) {
            for (const rel of ((await lre.async("text")).match(/<Relationship[^>]*\/>/g) || [])) {
              const id = pptxXmlAttr(rel, "Id");
              const target = pptxXmlAttr(rel, "Target");
              if (id && target) {
                const resolved = resolvePresentationPartTarget(layoutPath, target);
                if (mediaCache.has(resolved) && !relsMap.has(id)) relsMap.set(id, mediaCache.get(resolved)!);
              }
            }
          }
        }
      } catch { /* rels non-fatal */ }

      const slide: PptxSlide = {
        id: genId(),
        aspectRatio: `${EDITOR_SLIDE_W}/${EDITOR_SLIDE_H}`,
        heightPoints: EDITOR_SLIDE_H / 12700,
        shapes: [],
        sourcePart: slidePath,
        notesPart: notesPath,
        theme: {
          colors: { ..._activeTheme },
          majorFont: officeCompatibleFontFamily(_editorMajorFont || undefined),
          minorFont: officeCompatibleFontFamily(_editorMinorFont || undefined),
        },
      };
      if (notesPath) {
        try {
          const notesEntry = zip.file(notesPath);
          if (notesEntry) slide.notes = pptxParseSpeakerNotes(await notesEntry.async("text"));
        } catch { /* notes are optional */ }
      }

      const slideLayoutXml = layoutPath ? layoutCache.get(layoutPath) : undefined;
      const slideMasterPath = layoutPath ? layoutToMasterPath.get(layoutPath) : undefined;
      const slideMasterXml = slideMasterPath ? masterCache.get(slideMasterPath) : undefined;

      // Build placeholder position map: master → layout (later overrides)
      const phMap = new Map<string, { x: number; y: number; w: number; h: number }>();
      try {
        if (layoutPath) {
          if (slideMasterXml) {
            for (const [k, v] of editorBuildPhMap(slideMasterXml)) phMap.set(k, v);
          }
          if (slideLayoutXml) {
            for (const [k, v] of editorBuildPhMap(slideLayoutXml)) phMap.set(k, v);
          }
        }
      } catch { /* phMap non-fatal */ }

      // Background — slide → layout → slide master fallback (with bgRef resolution)
      try {
        let bgXml = pptxXmlInner(xml, "p:bg");
        let bgRels = relsMap;
        if (!bgXml && layoutPath && layoutCache.has(layoutPath)) {
          bgXml = pptxXmlInner(layoutCache.get(layoutPath)!, "p:bg");
          bgRels = layoutRelsCache.get(layoutPath) || new Map<string, string>();
        }
        if (!bgXml && layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            bgXml = pptxXmlInner(masterCache.get(masterPath)!, "p:bg");
            bgRels = masterRelsCache.get(masterPath) || new Map<string, string>();
          }
        }
        if (bgXml) {
          const bgResult = editorParseBgFromXml(bgXml);
          if (bgResult.color) slide.bg = bgResult.color;
          if (bgResult.grad) slide.bgGrad = bgResult.grad;
          if (bgResult.imgRId && bgRels.has(bgResult.imgRId)) slide.bgImgUrl = bgRels.get(bgResult.imgRId);
        }
      } catch { /* bg non-fatal */ }

      // Parse top-level shapes separately from group children to avoid rendering
      // grouped objects twice.
      const groupMatches = presentationObjectGroups(xml);
      let topLevelXml = xml;
      for (const group of groupMatches) topLevelXml = topLevelXml.replace(group.xml, "");
      const spMatches = topLevelXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || [];
      const picMatches = topLevelXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || [];
      const cxnMatches = topLevelXml.match(/<p:cxnSp[\s>][\s\S]*?<\/p:cxnSp>/g) || [];
      const groupedGraphicFrameMetadata = new Map<string, {
        transform: PresentationGroupTransform;
        groupPath: string[];
      }>();

      // Group shapes with coordinate transforms
      try {
        const applyGroupMetadata = (
          shape: PptxShape,
          transform: PresentationGroupTransform,
          groupPath: string[],
        ) => {
          if (!shape.source) return;
          shape.source.groupTransform = transform;
          shape.source.groupPath = groupPath;
        };
        const parseGroup = (
          grp: string,
          parentTransform: PresentationGroupTransform,
          parentPath: string[],
        ) => {
          const transform = presentationGroupTransform(grp, parentTransform);
          const groupId = grp.match(/<p:nvGrpSpPr\b[^>]*>[\s\S]*?<p:cNvPr\b[^>]*\bid="([^"]+)"/i)?.[1];
          const groupPath = groupId ? [...parentPath, groupId] : parentPath;
          const { directXml, nestedGroups } = presentationGroupContent(grp);
          const children = [
            ...(directXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
            ...(directXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
            ...(directXml.match(/<p:cxnSp[\s>][\s\S]*?<\/p:cxnSp>/g) || []),
          ];
          for (const ch of children) {
            const kind = ch.startsWith("<p:pic") ? "pic" : ch.startsWith("<p:cxnSp") ? "cxnSp" : "sp";
            const s = pptxParseShapeXml(
              ch,
              relsMap,
              phMap,
              { part: slidePath, kind, editable: true, mediaParts },
              editorShapeInheritance(ch, slideLayoutXml, slideMasterXml),
            );
            if (s) {
              applyGroupMetadata(s, transform, groupPath);
              slide.shapes.push(s);
            }
          }
          for (const frame of directXml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || []) {
            const objectId = frame.match(/<p:cNvPr\b[^>]*\bid="([^"]+)"/i)?.[1];
            if (objectId) groupedGraphicFrameMetadata.set(objectId, { transform, groupPath });
          }
          for (const nested of nestedGroups) parseGroup(nested.xml, transform, groupPath);
        };
        for (const group of groupMatches) parseGroup(group.xml, IDENTITY_PRESENTATION_GROUP_TRANSFORM, []);
      } catch { /* group extraction non-fatal */ }

      for (const sp of [...spMatches, ...picMatches, ...cxnMatches]) {
        try {
          const kind = sp.startsWith("<p:pic") ? "pic" : sp.startsWith("<p:cxnSp") ? "cxnSp" : "sp";
          const s = pptxParseShapeXml(
            sp,
            relsMap,
            phMap,
            { part: slidePath, kind, editable: true, mediaParts },
            editorShapeInheritance(sp, slideLayoutXml, slideMasterXml),
          );
          if (s) slide.shapes.push(s);
        } catch { /* individual shape non-fatal */ }
      }

      // Tables and unsupported graphic frames (non-fatal). Charts, diagrams, and
      // embedded objects remain independently movable/resizable and receive an
      // isolated transparent object render after parsing.
      try {
        for (const gf of (xml.match(/<p:graphicFrame[\s>][\s\S]*?<\/p:graphicFrame>/g) || [])) {
          const xfrm = pptxXmlInner(gf, "a:xfrm") || pptxXmlInner(gf, "p:xfrm");
          if (!xfrm) continue;
          const offM = xfrm.match(/<a:off x="(-?\d+)" y="(-?\d+)"/) || xfrm.match(/<p:off x="(-?\d+)" y="(-?\d+)"/);
          const extM = xfrm.match(/<a:ext cx="(\d+)" cy="(\d+)"/) || xfrm.match(/<p:ext cx="(\d+)" cy="(\d+)"/);
          if (!offM || !extM) continue;
          const table = pptxParseTable(gf);
          const cNvPr = gf.match(/<p:cNvPr\b[^>]*\/?\s*>/);
          const objectId = cNvPr ? pptxXmlAttr(cNvPr[0], "id") : null;
          const groupMetadata = objectId ? groupedGraphicFrameMetadata.get(objectId) : undefined;
          const groupTransform = groupMetadata?.transform;
          const xfrmOpening = xfrm.match(/<(?:a|p):xfrm\b[^>]*>/i)?.[0] || "";
          const frameRect = {
            x: parseInt(offM[1], 10),
            y: parseInt(offM[2], 10),
            width: parseInt(extM[1], 10),
            height: parseInt(extM[2], 10),
            rotation: Number(pptxXmlAttr(xfrmOpening, "rot") || 0) / 60_000,
            flipH: ["1", "true"].includes((pptxXmlAttr(xfrmOpening, "flipH") || "").toLowerCase()),
            flipV: ["1", "true"].includes((pptxXmlAttr(xfrmOpening, "flipV") || "").toLowerCase()),
          };
          const x = emu2pctX(frameRect.x);
          const y = emu2pctY(frameRect.y);
          const w = emu2pctX(frameRect.width);
          const h = emu2pctY(frameRect.height);
          const source = objectId
            ? {
                part: slidePath,
                kind: "graphicFrame" as const,
                objectId,
                editable: true,
                groupTransform,
                groupPath: groupMetadata?.groupPath,
              }
            : undefined;
          if (table) {
            slide.shapes.push({
              id: genId(), type: "table", x, y, w, h,
              rotation: frameRect.rotation, flipH: frameRect.flipH, flipV: frameRect.flipV,
              texts: [], tableRows: table.rows, tableCols: table.cols, tableColWidths: table.colWidths,
              tableRowHeights: table.rowHeights,
              source,
            });
            continue;
          }
          const graphicData = gf.match(/<a:graphicData\b[^>]*\buri="([^"]+)"/i)?.[1] || "";
          const graphicKind = /\/chart$/i.test(graphicData)
            ? "chart"
            : /\/diagram$/i.test(graphicData)
              ? "diagram"
              : /oleObject|package/i.test(graphicData)
                ? "embedded"
                : "unknown";
          slide.shapes.push({
            id: genId(), type: "graphic", x, y, w, h, texts: [], graphicKind,
            rotation: frameRect.rotation, flipH: frameRect.flipH, flipV: frameRect.flipV,
            graphicPreviewStatus: "loading",
            source,
          });
        }
      } catch { /* tables non-fatal */ }

      // The sp/pic/connector/graphic parsing passes above are type-specific.
      // Restore the original DrawingML paint order before adding inherited
      // layout/master decorations behind slide-owned objects.
      const sourcePaintOrder = new Map(
        presentationObjectIdsInOrder(xml).map((objectId, index) => [objectId, index]),
      );
      slide.shapes.sort((left, right) => {
        const leftOrder = left.source?.part === slidePath
          ? sourcePaintOrder.get(left.source.objectId)
          : undefined;
        const rightOrder = right.source?.part === slidePath
          ? sourcePaintOrder.get(right.source.objectId)
          : undefined;
        return (leftOrder ?? Number.MAX_SAFE_INTEGER) - (rightOrder ?? Number.MAX_SAFE_INTEGER);
      });

      // Layout decorative shapes (non-fatal)
      try {
        if (layoutPath && layoutCache.has(layoutPath)) {
          const layoutXml = layoutCache.get(layoutPath)!;
          const layoutDecorations: PptxShape[] = [];
          for (const sp of [
            ...(layoutXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
            ...(layoutXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
          ]) {
            if (sp.includes("<p:ph")) continue;
            const kind = sp.startsWith("<p:pic") ? "pic" : "sp";
            const lShape = pptxParseShapeXml(
              sp,
              layoutRelsCache.get(layoutPath) || new Map<string, string>(),
              undefined,
              { part: layoutPath, kind, editable: false },
            );
            if (lShape && (lShape.fill || lShape.gradFill || lShape.imgUrl || lShape.stroke)) {
              layoutDecorations.push(lShape);
            }
          }
          const layoutPaintOrder = new Map(
            presentationObjectIdsInOrder(layoutXml).map((objectId, index) => [objectId, index]),
          );
          layoutDecorations.sort((left, right) => (
            (layoutPaintOrder.get(left.source?.objectId || "") ?? Number.MAX_SAFE_INTEGER)
            - (layoutPaintOrder.get(right.source?.objectId || "") ?? Number.MAX_SAFE_INTEGER)
          ));
          slide.shapes = [...layoutDecorations, ...slide.shapes];
        }
      } catch { /* layout shapes non-fatal */ }

      // Slide master decorative shapes (non-fatal)
      try {
        if (layoutPath) {
          const masterPath = layoutToMasterPath.get(layoutPath);
          if (masterPath && masterCache.has(masterPath)) {
            const masterXml = masterCache.get(masterPath)!;
            const masterRels = masterRelsCache.get(masterPath) || new Map<string, string>();
            const masterDecorations: PptxShape[] = [];
            for (const sp of [
              ...(masterXml.match(/<p:sp[\s>][\s\S]*?<\/p:sp>/g) || []),
              ...(masterXml.match(/<p:pic[\s>][\s\S]*?<\/p:pic>/g) || []),
            ]) {
              if (sp.includes("<p:ph")) continue;
              const kind = sp.startsWith("<p:pic") ? "pic" : "sp";
              const mShape = pptxParseShapeXml(sp, masterRels, undefined, { part: masterPath, kind, editable: false });
              if (mShape && (mShape.fill || mShape.gradFill || mShape.imgUrl || mShape.stroke)) {
                masterDecorations.push(mShape);
              }
            }
            const masterPaintOrder = new Map(
              presentationObjectIdsInOrder(masterXml).map((objectId, index) => [objectId, index]),
            );
            masterDecorations.sort((left, right) => (
              (masterPaintOrder.get(left.source?.objectId || "") ?? Number.MAX_SAFE_INTEGER)
              - (masterPaintOrder.get(right.source?.objectId || "") ?? Number.MAX_SAFE_INTEGER)
            ));
            slide.shapes = [...masterDecorations, ...slide.shapes];
          }
        }
      } catch { /* master shapes non-fatal */ }

      slides.push(slide);
    } catch {
      slides.push({
        id: genId(),
        aspectRatio: `${EDITOR_SLIDE_W}/${EDITOR_SLIDE_H}`,
        heightPoints: EDITOR_SLIDE_H / 12700,
        shapes: [],
        theme: {
          colors: { ..._activeTheme },
          majorFont: officeCompatibleFontFamily(_editorMajorFont || undefined),
          minorFont: officeCompatibleFontFamily(_editorMinorFont || undefined),
        },
      });
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

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Secure file version checks are unavailable in this browser.");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function loadOfficeEditSource(
  documentId: string,
  needsLegacyConversion: boolean,
): Promise<{ buffer: ArrayBuffer; sourceSha256: string }> {
  if (needsLegacyConversion) {
    const response = await api.documents.editableResponse(documentId);
    const sourceSha256 = response.headers.get("X-Manor-Source-SHA256")?.trim().toLowerCase() || "";
    if (!/^[0-9a-f]{64}$/.test(sourceSha256)) {
      throw new Error("The editable Office source is missing its version identifier.");
    }
    return { buffer: await response.arrayBuffer(), sourceSha256 };
  }
  const buffer = await (await api.documents.previewBlob(documentId)).arrayBuffer();
  return { buffer, sourceSha256: await sha256Hex(buffer) };
}

const PRESENTATION_FONT_FAMILIES = [
  "Aptos",
  "Arial",
  "Inter",
  "Georgia",
  "Times New Roman",
  "Verdana",
  "Courier New",
];

function createPptxSlide(layout: PresentationSlideLayout, aspectRatio = "16/9"): PptxSlide {
  return createPresentationSlide({ id: genId(), layout, aspectRatio }) as PptxSlide;
}

function pptxSlideAspectRatio(value?: string): number {
  const [width, height] = (value || "16/9").split("/").map(Number);
  return width > 0 && height > 0 ? width / height : 16 / 9;
}

function pptxColorInputValue(value: string | undefined, fallback: string): string {
  if (value && /^#[\da-f]{6}$/i.test(value)) return value;
  if (value && /^#[\da-f]{3}$/i.test(value)) {
    return `#${value.slice(1).split("").map((character) => character + character).join("")}`;
  }
  const rgb = value?.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (!rgb) return fallback;
  return `#${rgb.slice(1, 4).map((channel) => Math.max(0, Math.min(255, Number(channel))).toString(16).padStart(2, "0")).join("")}`;
}

function pptxImageNaturalSize(url: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve({ width: image.naturalWidth || image.width, height: image.naturalHeight || image.height });
    image.onerror = () => reject(new Error("Unable to read presentation image dimensions."));
    image.src = url;
  });
}

function pptxEditorImageStyle(shape: PptxShape, borderRadius: React.CSSProperties["borderRadius"]): React.CSSProperties {
  const clipPath = pptxShapeClipPath(shape);
  if (!shape.imgCrop) {
    return {
      position: "absolute",
      inset: 0,
      width: "100%",
      height: "100%",
      objectFit: shape.imageFit || "fill",
      borderRadius,
      clipPath,
      opacity: shape.opacity,
    };
  }
  const remainingWidth = Math.max(0.01, 100 - shape.imgCrop.l - shape.imgCrop.r);
  const remainingHeight = Math.max(0.01, 100 - shape.imgCrop.t - shape.imgCrop.b);
  return {
    position: "absolute",
    left: `${-(shape.imgCrop.l / remainingWidth) * 100}%`,
    top: `${-(shape.imgCrop.t / remainingHeight) * 100}%`,
    width: `${10000 / remainingWidth}%`,
    height: `${10000 / remainingHeight}%`,
    objectFit: "fill",
    borderRadius,
    clipPath,
    opacity: shape.opacity,
  };
}

function pptxShapeClipPath(shape: PptxShape): string | undefined {
  return presentationPresetClipPath(shape.presetGeom);
}

function pptxShapeBorderRadius(shape: PptxShape): React.CSSProperties["borderRadius"] {
  if (shape.presetGeom === "ellipse" || shape.presetGeom === "oval") return "50%";
  if (shape.presetGeom === "roundRect") return presentationRoundRectRadius(shape.w, shape.h, shape.borderRadius);
  if (shape.presetGeom === "flowChartTerminator") return "999px";
  if (shape.presetGeom === "wedgeRoundRectCallout") return "8%";
  if (shape.presetGeom === "snip1Rect" || shape.presetGeom === "snip2SameRect") return "0 12% 0 0";
  return shape.borderRadius ? `${shape.borderRadius}%` : 0;
}

function pptxShapeTransform(shape: PptxShape): string | undefined {
  if (shape.source?.groupTransform) {
    const sourceRect = {
      x: (shape.x / 100) * EDITOR_SLIDE_W,
      y: (shape.y / 100) * EDITOR_SLIDE_H,
      width: (shape.w / 100) * EDITOR_SLIDE_W,
      height: (shape.h / 100) * EDITOR_SLIDE_H,
      rotation: shape.rotation,
      flipH: shape.flipH,
      flipV: shape.flipV,
    };
    const transform = presentationShapeTransform(shape.source.groupTransform, sourceRect);
    return `matrix(${transform.a}, ${transform.b}, ${transform.c}, ${transform.d}, 0, 0)`;
  }
  const transforms: string[] = [];
  if (shape.rotation) transforms.push(`rotate(${shape.rotation}deg)`);
  if (shape.flipH) transforms.push("scaleX(-1)");
  if (shape.flipV) transforms.push("scaleY(-1)");
  return transforms.length ? transforms.join(" ") : undefined;
}

function pptxShapeShadow(shape: PptxShape): string | undefined {
  if (!shape.shadow) return undefined;
  const { angle, dist, blur, color, alpha } = shape.shadow;
  const radians = (angle * Math.PI) / 180;
  const red = parseInt(color.slice(1, 3), 16);
  const green = parseInt(color.slice(3, 5), 16);
  const blue = parseInt(color.slice(5, 7), 16);
  return `${pptxPointsToCqh(Math.cos(radians) * dist)} ${pptxPointsToCqh(Math.sin(radians) * dist)} ${pptxPointsToCqh(blur)} rgba(${red},${green},${blue},${alpha})`;
}

function pptxShapeVisualStyle(shape: PptxShape): React.CSSProperties {
  const isLine = shape.presetGeom === "line";
  const isPolygon = Boolean(presentationPresetPolygonPoints(shape.presetGeom));
  const sourceRect = {
    x: (shape.x / 100) * EDITOR_SLIDE_W,
    y: (shape.y / 100) * EDITOR_SLIDE_H,
    width: (shape.w / 100) * EDITOR_SLIDE_W,
    height: (shape.h / 100) * EDITOR_SLIDE_H,
    rotation: shape.rotation,
    flipH: shape.flipH,
    flipV: shape.flipV,
  };
  const groupedTransform = shape.source?.groupTransform
    ? presentationShapeTransform(shape.source.groupTransform, sourceRect)
    : undefined;
  const groupedOrigin = groupedTransform
    ? presentationTransformPoint(groupedTransform, sourceRect.x, sourceRect.y)
    : undefined;
  return {
    position: "absolute",
    left: `${groupedOrigin ? emu2pctX(groupedOrigin.x) : shape.x}%`,
    top: `${groupedOrigin ? emu2pctY(groupedOrigin.y) : shape.y}%`,
    width: `${isLine ? Math.max(0.5, shape.w) : shape.w}%`,
    height: `${isLine ? Math.max(0.5, shape.h) : shape.h}%`,
    boxSizing: "border-box",
    overflow: isLine ? "visible" : "hidden",
    borderRadius: pptxShapeBorderRadius(shape),
    background: isLine || isPolygon ? undefined : shape.gradFill ? pptxGradToCss(shape.gradFill) : shape.fill,
    border: "none",
    transform: pptxShapeTransform(shape),
    transformOrigin: groupedTransform ? "0 0" : undefined,
    boxShadow: isLine || isPolygon ? undefined : pptxShapeShadow(shape),
    display: "flex",
    flexDirection: "column",
    justifyContent: shape.vAlign === "bottom" ? "flex-end" : shape.vAlign === "middle" ? "center" : "flex-start",
    padding: !isLine && shape.padding
      ? `${pptxPointsToCqh(shape.padding.t)} ${pptxPointsToCqh(shape.padding.r)} ${pptxPointsToCqh(shape.padding.b)} ${pptxPointsToCqh(shape.padding.l)}`
      : !isLine && shape.texts.length
        ? `${pptxPointsToCqh(3.6)} ${pptxPointsToCqh(7.2)}`
        : undefined,
  };
}

function pptxShapeLocalDelta(shape: PptxShape, dx: number, dy: number): { dx: number; dy: number } {
  const groupTransform = shape.source?.groupTransform;
  if (!groupTransform) return { dx, dy };
  const inverse = presentationInverseTransform(groupTransform);
  if (!inverse) return { dx: 0, dy: 0 };
  const deltaX = (dx / 100) * EDITOR_SLIDE_W;
  const deltaY = (dy / 100) * EDITOR_SLIDE_H;
  return {
    dx: emu2pctX(inverse.a * deltaX + inverse.c * deltaY),
    dy: emu2pctY(inverse.b * deltaX + inverse.d * deltaY),
  };
}

function PptxShapeGeometry({ shape }: { shape: PptxShape }) {
  if (shape.presetGeom === "line") return <PresentationShapeOutline {...shape} />;
  const points = presentationPresetPolygonPoints(shape.presetGeom);
  if (!points) return <PresentationShapeOutline {...shape} />;
  const pointList = presentationPresetPointsAttribute(shape.presetGeom);
  const clipPath = pptxShapeClipPath(shape);
  const shadow = pptxShapeShadow(shape);
  return (
    <>
      <div
        aria-hidden="true"
        style={{
          position: "absolute",
          inset: 0,
          clipPath,
          background: shape.gradFill ? pptxGradToCss(shape.gradFill) : shape.fill,
          filter: shadow ? `drop-shadow(${shadow})` : undefined,
          pointerEvents: "none",
        }}
      />
      <PresentationShapeOutline {...shape} points={pointList} />
    </>
  );
}

function PptxGraphicFramePreview({ shape }: { shape: PptxShape }) {
  return (
    <div
      role="img"
      aria-label={t("page.doc_editor.object")}
      style={{
        position: "absolute",
        inset: 0,
        overflow: "hidden",
        background: shape.graphicPreviewUrl ? "transparent" : "var(--color-surface-subtle, #f5f5f4)",
        pointerEvents: "none",
      }}
    >
      {shape.graphicPreviewUrl ? (
        <img
          src={shape.graphicPreviewUrl}
          alt=""
          draggable={false}
          style={{
            position: "absolute",
            inset: 0,
            width: "100%",
            height: "100%",
            objectFit: "fill",
          }}
        />
      ) : (
        <span style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "var(--color-text-muted, #78716c)", fontSize: 12 }}>
          {shape.graphicPreviewStatus === "loading"
            ? t("status.loading")
            : t("page.file_viewer.preview_not_available")}
        </span>
      )}
    </div>
  );
}

function pptxGraphicFrameImageUrl(shape: PptxShape): Promise<string> {
  return shape.graphicPreviewUrl
    ? Promise.resolve(shape.graphicPreviewUrl)
    : Promise.reject(new Error("A rendered presentation object is required to copy this object."));
}

async function clonePptxShapeForEditor(
  shape: PptxShape,
  offset = 0,
  preserveEditableSource = true,
): Promise<PptxShape> {
  const clone = clonePptxShape(shape, offset, preserveEditableSource);
  if (shape.type !== "graphic" || clone.source) return clone;
  return {
    ...clone,
    type: "image",
    imgUrl: await pptxGraphicFrameImageUrl(shape),
    imageFit: "fill",
    graphicKind: undefined,
    graphicPreviewUrl: undefined,
    graphicPreviewStatus: undefined,
  };
}

function pptxParagraphVisualStyle(paragraph: PptxTextRun, slide: PptxSlide, shape: PptxShape): React.CSSProperties {
  return {
    position: "relative",
    zIndex: 1,
    marginTop: paragraph.spaceBefore != null ? pptxPointsToCqh(paragraph.spaceBefore) : paragraph.text === "" ? "0.3em" : "0.05em",
    marginBottom: paragraph.spaceAfter != null ? pptxPointsToCqh(paragraph.spaceAfter) : "0.05em",
    paddingLeft: paragraph.indent != null ? pptxPointsToCqh(paragraph.indent) : paragraph.bullet ? pptxPointsToCqh(18) : undefined,
    paddingRight: paragraph.indentRight != null ? pptxPointsToCqh(paragraph.indentRight) : undefined,
    textIndent: !paragraph.bullet && paragraph.hanging != null ? pptxPointsToCqh(paragraph.hanging) : undefined,
    color: paragraph.color || slide.theme?.colors.tx1 || DEFAULT_SCHEME.tx1 || "#000000",
    fontFamily: paragraph.fontFamily
      ? `"${paragraph.fontFamily}", sans-serif`
      : slide.theme?.minorFont
        ? `"${slide.theme.minorFont}", sans-serif`
        : undefined,
    fontSize: pptxPointsToCqh(paragraph.fontSize || 16),
    fontWeight: paragraph.bold ? 700 : 400,
    fontStyle: paragraph.italic ? "italic" : undefined,
    textDecoration: [paragraph.underline ? "underline" : "", paragraph.strikethrough ? "line-through" : ""].filter(Boolean).join(" ") || undefined,
    textAlign: (paragraph.align as React.CSSProperties["textAlign"]) || "left",
    lineHeight: paragraph.lineSpacing || 1.2,
    wordBreak: "normal",
    overflowWrap: shape.wordWrap === false ? "normal" : "break-word",
    whiteSpace: shape.wordWrap === false ? "pre" : "pre-wrap",
    minHeight: "1.2em",
  };
}

function pptxRunVisualStyle(run: NonNullable<PptxTextRun["runs"]>[number]): React.CSSProperties {
  return {
    color: run.color,
    fontFamily: run.fontFamily ? `"${run.fontFamily}", sans-serif` : undefined,
    fontSize: run.fontSize ? pptxPointsToCqh(run.fontSize) : undefined,
    fontWeight: run.bold ? 700 : undefined,
    fontStyle: run.italic ? "italic" : undefined,
    textDecoration: [run.underline ? "underline" : "", run.strikethrough ? "line-through" : ""].filter(Boolean).join(" ") || undefined,
    verticalAlign: run.baseline ? (run.baseline > 0 ? "super" : "sub") : undefined,
    letterSpacing: run.spacing ? pptxPointsToCqh(run.spacing) : undefined,
  };
}

function reconcilePptxTextRuns(
  paragraph: PptxTextRun,
  nextText: string,
  sourceMap?: PresentationTextSourceMap,
): PptxInlineTextRun[] | undefined {
  return reconcilePresentationTextRuns(
    paragraph.runs,
    paragraph.text,
    nextText,
    sourceMap,
    paragraph.sourceMap,
  );
}

function normalizedPptxEditableText(value: string): string {
  return value.replace(/\r\n?/g, "\n");
}

function pptxContentEditablePointOffset(root: HTMLElement, node: Node, offset: number): number | null {
  if (node !== root && !root.contains(node)) return null;
  try {
    const range = document.createRange();
    range.setStart(root, 0);
    range.setEnd(node, offset);
    const measure = document.createElement("div");
    measure.style.cssText = "position:fixed;left:-10000px;top:0;white-space:pre-wrap;visibility:hidden";
    measure.append(range.cloneContents());
    document.body.append(measure);
    const length = Array.from(normalizedPptxEditableText(measure.innerText)).length;
    measure.remove();
    return length;
  } catch {
    return null;
  }
}

function pptxContentEditableSelection(root: HTMLElement): { start: number; end: number } | null {
  const selection = window.getSelection();
  if (!selection?.anchorNode || !selection.focusNode) return null;
  const anchor = pptxContentEditablePointOffset(root, selection.anchorNode, selection.anchorOffset);
  const focus = pptxContentEditablePointOffset(root, selection.focusNode, selection.focusOffset);
  return anchor == null || focus == null
    ? null
    : { start: Math.min(anchor, focus), end: Math.max(anchor, focus) };
}

function pptxDirectInputSpan(
  currentText: string,
  nextText: string,
  selection: { start: number; end: number },
  inputType: string,
  data: string | null,
): PresentationTextEditSpan | null {
  // Some browser/automation input paths dispatch a beforeinput event without
  // InputEvent.inputType. Fall back to the normal text reconciliation path.
  if (!inputType) return null;
  const currentCharacters = Array.from(currentText);
  let originalStart = selection.start;
  let originalEnd = selection.end;
  let inserted = "";
  if (inputType.startsWith("delete")) {
    if (originalStart === originalEnd && inputType.endsWith("Backward")) {
      const nextCharacters = Array.from(nextText);
      const suffix = currentCharacters.slice(originalEnd);
      const prefixLength = nextCharacters.length - suffix.length;
      if (
        prefixLength < 0
        || prefixLength > originalStart
        || nextCharacters.slice(prefixLength).join("") !== suffix.join("")
        || nextCharacters.slice(0, prefixLength).join("") !== currentCharacters.slice(0, prefixLength).join("")
      ) return null;
      originalStart = prefixLength;
    } else if (originalStart === originalEnd && inputType.endsWith("Forward")) {
      const nextCharacters = Array.from(nextText);
      const prefix = currentCharacters.slice(0, originalStart);
      const suffixLength = nextCharacters.length - prefix.length;
      const suffixStart = currentCharacters.length - suffixLength;
      if (
        suffixLength < 0
        || suffixStart < originalEnd
        || nextCharacters.slice(0, prefix.length).join("") !== prefix.join("")
        || nextCharacters.slice(prefix.length).join("") !== currentCharacters.slice(suffixStart).join("")
      ) return null;
      originalEnd = suffixStart;
    }
  } else if (inputType === "insertParagraph" || inputType === "insertLineBreak") inserted = "\n";
  else if (inputType.startsWith("insert") && data != null) inserted = normalizedPptxEditableText(data);
  else if (inputType.startsWith("insert")) {
    const nextCharacters = Array.from(nextText);
    const suffixLength = currentCharacters.length - originalEnd;
    if (
      nextCharacters.slice(0, originalStart).join("") !== currentCharacters.slice(0, originalStart).join("")
      || nextCharacters.slice(nextCharacters.length - suffixLength).join("") !== currentCharacters.slice(originalEnd).join("")
    ) return null;
    inserted = nextCharacters.slice(originalStart, nextCharacters.length - suffixLength).join("");
  } else return null;

  const expected = [
    ...currentCharacters.slice(0, originalStart),
    ...Array.from(inserted),
    ...currentCharacters.slice(originalEnd),
  ].join("");
  if (expected !== nextText) return null;
  return {
    originalStart,
    originalEnd,
    editedStart: originalStart,
    editedEnd: originalStart + Array.from(inserted).length,
  };
}

function PptxRunsContent({ paragraph }: { paragraph: PptxTextRun }) {
  return paragraph.runs?.length
    ? paragraph.runs.map((run, runIndex) => <span key={runIndex} style={pptxRunVisualStyle(run)}>{run.text}</span>)
    : paragraph.text;
}

function PptxBulletMarker({ paragraph }: { paragraph: PptxTextRun }) {
  if (!paragraph.bullet) return null;
  return (
    <span
      contentEditable={false}
      style={{
        position: "absolute",
        left: paragraph.indent != null
          ? pptxPointsToCqh(paragraph.indent + (paragraph.hanging ?? (paragraph.indent === 0 ? 0 : -14)))
          : pptxPointsToCqh(2),
      }}
    >
      {paragraph.bullet}
    </span>
  );
}

function PptxParagraphContent({ paragraph }: { paragraph: PptxTextRun }) {
  return (
    <>
      <PptxBulletMarker paragraph={paragraph} />
      <PptxRunsContent paragraph={paragraph} />
    </>
  );
}

function pptxTableCellVisualStyle(cell: PptxTableCell, rowIndex: number): React.CSSProperties {
  return {
    background: cell.fill || (rowIndex === 0 ? "#f5f5f4" : "#ffffff"),
    color: cell.color || "#292524",
    fontWeight: (cell.bold ?? rowIndex === 0) ? 700 : 400,
    fontStyle: cell.italic ? "italic" : "normal",
    fontFamily: cell.fontFamily || undefined,
    fontSize: cell.fontSize ? pptxPointsToCqh(cell.fontSize) : undefined,
    whiteSpace: "pre-wrap",
  };
}

function pptxTableRowVisualStyle(shape: PptxShape, rowIndex: number): React.CSSProperties | undefined {
  const heights = shape.tableRowHeights;
  if (!heights || heights.length !== shape.tableRows?.length) return undefined;
  const totalHeight = heights.reduce((sum, value) => sum + Math.max(0, value), 0);
  const rowHeight = heights[rowIndex];
  if (!totalHeight || !rowHeight) return undefined;
  return { height: `${(rowHeight / totalHeight) * 100}%` };
}

function PptxReadOnlySlide({
  slide,
  thumbnail = false,
}: {
  slide: PptxSlide;
  thumbnail?: boolean;
}) {
  const background: React.CSSProperties = { backgroundColor: slide.bg || "#ffffff" };
  if (slide.bgGrad) background.backgroundImage = pptxGradToCss(slide.bgGrad);
  if (slide.bgImgUrl) {
    background.backgroundImage = `url(${slide.bgImgUrl})`;
    background.backgroundSize = "cover";
    background.backgroundPosition = "center";
  }

  return (
    <div
      className={thumbnail ? "presentation-editor-thumbnail-slide" : "presentation-editor-present-slide"}
      style={{ ...background, aspectRatio: slide.aspectRatio || "16/9", "--pptx-point-scale": slide.heightPoints ? 540 / slide.heightPoints : undefined } as React.CSSProperties}
    >
      {slide.shapes.map((shape) => {
        const shapeStyle = pptxShapeVisualStyle(shape);
        const radius = shapeStyle.borderRadius;

        if (shape.type === "table" && shape.tableRows) {
          return (
            <div key={shape.id} style={shapeStyle}>
              <table className="presentation-editor-present-table">
                {shape.tableColWidths && (
                  <colgroup>
                    {shape.tableColWidths.map((width, columnIndex) => {
                      const totalWidth = shape.tableColWidths!.reduce((sum, value) => sum + value, 0) || 1;
                      return <col key={columnIndex} style={{ width: `${(width / totalWidth) * 100}%` }} />;
                    })}
                  </colgroup>
                )}
                <tbody>
                  {shape.tableRows.map((row, rowIndex) => (
                    <tr key={rowIndex} style={pptxTableRowVisualStyle(shape, rowIndex)}>
                      {row.map((cell, cellIndex) => cell.vMerge ? null : (
                        <td key={cellIndex} colSpan={cell.gridSpan} style={pptxTableCellVisualStyle(cell, rowIndex)}>
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
            <PptxShapeGeometry shape={shape} />
            {shape.type === "graphic" && <PptxGraphicFramePreview shape={shape} />}
            {shape.videoUrl && !thumbnail ? (
              <video
                className="presentation-editor-native-video presentation-editor-native-video--shape"
                src={shape.videoUrl}
                poster={shape.imgUrl}
                controls
                playsInline
                preload="metadata"
                aria-label="Presentation video"
                style={{ opacity: shape.opacity }}
              />
            ) : shape.imgUrl && (
              <img
                src={shape.imgUrl}
                alt={shape.altText || ""}
                style={pptxEditorImageStyle(shape, radius)}
              />
            )}
            {shape.texts.map((paragraph, paragraphIndex) => (
              <div key={paragraphIndex} style={pptxParagraphVisualStyle(paragraph, slide, shape)}>
                <PptxParagraphContent paragraph={paragraph} />
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
  onLiveEditTargetChange,
  onChange,
}: {
  slides: PptxSlide[];
  onLiveEditTargetChange?: (target: PresentationLiveEditTarget) => void;
  onChange: (slides: PptxSlide[]) => void;
}) {
  const [slides, setSlides] = useState<PptxSlide[]>(initialSlides);
  const [activeIdx, setActiveIdx] = useState(0);
  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState<{ shapeId: string; textIdx: number; initialValue: string } | null>(null);
  const [editingTableCell, setEditingTableCell] = useState<{
    shapeId: string;
    rowIdx: number;
    cellIdx: number;
    value: string;
    initialValue: string;
    sourceMap: PresentationTextSourceMap;
  } | null>(null);
  const [dragging, setDragging] = useState<{ shapeId: string; slideIdx: number; startX: number; startY: number; origX: number; origY: number; baseSlides: PptxSlide[] } | null>(null);
  const [resizing, setResizing] = useState<{ shapeId: string; slideIdx: number; handle: PresentationResizeHandle; startX: number; startY: number; origX: number; origY: number; origW: number; origH: number; baseSlides: PptxSlide[] } | null>(null);
  const [undoStack, setUndoStack] = useState<PptxSlide[][]>([]);
  const [redoStack, setRedoStack] = useState<PptxSlide[][]>([]);
  const [dragThumbIdx, setDragThumbIdx] = useState<number | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);
  const [canvasZoom, setCanvasZoom] = useState(1);
  const [showFormatOptions, setShowFormatOptions] = useState(false);
  const [presentingIdx, setPresentingIdx] = useState<number | null>(null);
  const [notesDraft, setNotesDraft] = useState("");
  const [shapeContextMenu, setShapeContextMenu] = useState<{ shapeId: string; x: number; y: number } | null>(null);
  const [mediaInsertOpen, setMediaInsertOpen] = useState(false);
  const showPresentationError = useToastStore((state) => state.error);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const inlineTextEditorRef = useRef<HTMLSpanElement>(null);
  const inlineTextEditSessionRef = useRef<{
    shapeId: string;
    textIdx: number;
    text: string;
    sourceMap: PresentationTextSourceMap;
  } | null>(null);
  const inlineTextBeforeInputRef = useRef<{
    selection: { start: number; end: number };
    inputType: string;
    data: string | null;
  } | null>(null);
  const tableCellBeforeInputRef = useRef<{
    selection: { start: number; end: number };
    inputType: string;
    data: string | null;
  } | null>(null);
  const replaceImageShapeIdRef = useRef<string | null>(null);
  const imageFitRequestRef = useRef(new Map<string, number>());
  const canvasRef = useRef<HTMLDivElement>(null);
  const canvasViewportRef = useRef<HTMLDivElement>(null);
  const thumbnailRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const slidesRef = useRef(slides);
  const activeIdxRef = useRef(activeIdx);
  activeIdxRef.current = activeIdx;
  const copiedShapeRef = useRef<PptxShape | null>(null);
  const copyShapeRequestRef = useRef(0);
  const [canvasViewportSize, setCanvasViewportSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    slidesRef.current = slides;
  }, [slides]);

  useEffect(() => {
    if (!selectedShapeId) setShowFormatOptions(false);
  }, [selectedShapeId]);

  useEffect(() => {
    if (!editingText || !inlineTextEditorRef.current) return;
    const editor = inlineTextEditorRef.current;
    editor.focus();
    const selection = window.getSelection();
    if (!selection) return;
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
  }, [editingText]);

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
    const previousSlideIds = new Set(slidesRef.current.map((slide) => slide.id));
    let createdSlideIndex = -1;
    initialSlides.forEach((slide, index) => {
      if (slide.id.startsWith("ai-slide-") && !previousSlideIds.has(slide.id)) {
        createdSlideIndex = index;
      }
    });
    slidesRef.current = initialSlides;
    setSlides(initialSlides);
    setActiveIdx((index) => createdSlideIndex >= 0
      ? createdSlideIndex
      : Math.max(0, Math.min(index, initialSlides.length - 1)));
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
    setUndoStack([]);
    setRedoStack([]);
  }, [initialSlides]);

  useEffect(() => {
    onLiveEditTargetChange?.({ activeSlideIndex: activeIdx, selectedShapeId });
  }, [activeIdx, onLiveEditTargetChange, selectedShapeId]);

  useEffect(() => {
    thumbnailRefs.current[activeIdx]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeIdx, slides.length]);

  const selectPresentationSlide = useCallback((slideIndex: number) => {
    setActiveIdx(Math.max(0, Math.min(slides.length - 1, slideIndex)));
    setSelectedShapeId(null);
    setEditingText(null);
    setEditingTableCell(null);
  }, [slides.length]);

  const handlePresentationThumbnailKeyDown = useCallback((
    event: React.KeyboardEvent<HTMLButtonElement>,
    slideIndex: number,
  ) => {
    let nextSlide: number | null = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextSlide = Math.max(0, slideIndex - 1);
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextSlide = Math.min(slides.length - 1, slideIndex + 1);
    if (event.key === "Home") nextSlide = 0;
    if (event.key === "End") nextSlide = slides.length - 1;
    if (nextSlide === null || nextSlide === slideIndex) return;
    event.preventDefault();
    selectPresentationSlide(nextSlide);
    thumbnailRefs.current[nextSlide]?.focus();
  }, [selectPresentationSlide, slides.length]);

  const pushUndo = useCallback((prev: PptxSlide[]) => {
    setUndoStack(s => [...s.slice(-29), prev]);
    setRedoStack([]);
  }, []);

  const updateSlides = useCallback((next: PptxSlide[], skipUndo = false) => {
    if (!skipUndo) pushUndo(slidesRef.current);
    slidesRef.current = next;
    setSlides(next);
    onChange(next);
  }, [onChange, pushUndo]);

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

  const addSlide = useCallback((layout: PresentationSlideLayout = PresentationSlideLayout.TitleBody) => {
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
    setEditingText({ shapeId, textIdx: 0, initialValue: text });
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

  const updateShapeText = useCallback((
    shapeId: string,
    textIdx: number,
    newText: string,
    sourceMap?: PresentationTextSourceMap,
  ) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return {
        ...s,
        shapes: s.shapes.map(sh => {
          if (sh.id !== shapeId) return sh;
          const texts = [...sh.texts];
          const paragraph = texts[textIdx];
          const nextSourceMap = sourceMap
            || reconcilePresentationTextSourceMap(paragraph.text, newText, paragraph.sourceMap);
          texts[textIdx] = {
            ...paragraph,
            text: newText,
            sourceMap: nextSourceMap,
            runs: reconcilePptxTextRuns(paragraph, newText, nextSourceMap),
          };
          return { ...sh, texts };
        }),
      };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const beginTextEditing = useCallback((shapeId: string, textIdx: number, value: string) => {
    setSelectedShapeId(shapeId);
    setEditingTableCell(null);
    const paragraph = slidesRef.current[activeIdxRef.current]?.shapes
      .find((shape) => shape.id === shapeId)?.texts[textIdx];
    inlineTextEditSessionRef.current = {
      shapeId,
      textIdx,
      text: value,
      sourceMap: reconcilePresentationTextSourceMap(value, value, paragraph?.sourceMap),
    };
    inlineTextBeforeInputRef.current = null;
    setEditingText({ shapeId, textIdx, initialValue: value });
  }, []);

  const beginTableCellEditing = useCallback((
    shapeId: string,
    rowIdx: number,
    cellIdx: number,
    cell: PptxTableCell,
  ) => {
    setSelectedShapeId(shapeId);
    setEditingText(null);
    setEditingTableCell({
      shapeId,
      rowIdx,
      cellIdx,
      value: cell.text,
      initialValue: cell.text,
      sourceMap: reconcilePresentationTextSourceMap(cell.text, cell.text, cell.sourceMap),
    });
  }, []);

  const commitTextEditing = useCallback((nextText: string) => {
    if (!editingText) return;
    if (nextText !== editingText.initialValue) {
      const session = inlineTextEditSessionRef.current;
      updateShapeText(
        editingText.shapeId,
        editingText.textIdx,
        nextText,
        session?.shapeId === editingText.shapeId
          && session.textIdx === editingText.textIdx
          && session.text === nextText
          ? session.sourceMap
          : undefined,
      );
    }
    inlineTextEditSessionRef.current = null;
    inlineTextBeforeInputRef.current = null;
    setEditingText(null);
  }, [editingText, updateShapeText]);

  const updateTableCell = useCallback((
    shapeId: string,
    rowIdx: number,
    cellIdx: number,
    value: string,
    sourceMap: PresentationTextSourceMap,
  ) => {
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
              : row.map((cell, currentCellIdx) => currentCellIdx === cellIdx
                ? { ...cell, text: value, sourceMap }
                : cell)),
          };
        }),
      };
    });
    updateSlides(next);
  }, [activeIdx, slides, updateSlides]);

  const commitTableCellEditing = useCallback(() => {
    if (!editingTableCell) return;
    if (editingTableCell.value !== editingTableCell.initialValue) {
      updateTableCell(
        editingTableCell.shapeId,
        editingTableCell.rowIdx,
        editingTableCell.cellIdx,
        editingTableCell.value,
        editingTableCell.sourceMap,
      );
    }
    tableCellBeforeInputRef.current = null;
    setEditingTableCell(null);
  }, [editingTableCell, updateTableCell]);

  const updateSlideBg = useCallback((color: string) => {
    const next = slides.map((s, i) => {
      if (i !== activeIdx) return s;
      return { ...s, bg: color, bgGrad: undefined, bgImgUrl: undefined };
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

  const handleImageFile = useCallback((file: File, replaceShapeId: string | null = null, hyperlink?: string) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      if (!url) return;
      const img = new Image();
      img.onload = () => {
        const aspect = img.width / img.height || 1;
        const slideAspect = pptxSlideAspectRatio(slides[activeIdx]?.aspectRatio);
        let w = 40;
        let h = (w * slideAspect) / aspect;
        if (h > 60) {
          h = 60;
          w = (h * aspect) / slideAspect;
        }
        const shapeId = replaceShapeId || genId();
        const next = slides.map((s, i) => {
          if (i !== activeIdx) return s;
          if (replaceShapeId) {
            return {
              ...s,
              shapes: s.shapes.map((shape) => shape.id === replaceShapeId
                ? { ...shape, type: "image" as const, imgUrl: url, imgCrop: shape.imgCrop, imageFit: shape.imageFit || "fill" as const }
                : shape),
            };
          }
          return { ...s, shapes: [...s.shapes, {
            id: shapeId,
            type: "image" as const,
            x: (100 - w) / 2,
            y: (100 - h) / 2,
            w,
            h,
            imgUrl: url,
            hyperlink,
            imageFit: "fill" as const,
            texts: [],
          }] };
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
    setMediaInsertOpen(true);
  }, []);

  const handleMediaInsert = useCallback(async (asset: InsertableMediaAsset) => {
    if (asset.kind === "image") {
      const blob = await api.documents.downloadBlob(asset.document.id);
      handleImageFile(new File([blob], asset.name, { type: blob.type || asset.document.mime_type || "image/png" }));
      return;
    }

    let posterFile: File;
    try {
      const thumbnailUrl = await api.documents.videoThumbnail(asset.document.id);
      const response = await fetch(thumbnailUrl);
      if (!response.ok) throw new Error("Video poster unavailable");
      const blob = await response.blob();
      posterFile = new File([blob], `${asset.name}-poster.png`, { type: blob.type || "image/png" });
      if (thumbnailUrl.startsWith("blob:")) URL.revokeObjectURL(thumbnailUrl);
    } catch {
      const safeTitle = asset.name.replace(/[<>&"']/g, "").slice(0, 72);
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#171717"/><circle cx="640" cy="330" r="92" fill="#4f7d75"/><path d="M615 275l88 55-88 55z" fill="#fff"/><text x="640" y="505" fill="#fff" font-family="Arial,sans-serif" font-size="38" text-anchor="middle">${safeTitle}</text></svg>`;
      posterFile = new File([svg], `${asset.name}-poster.svg`, { type: "image/svg+xml" });
    }
    handleImageFile(
      posterFile,
      null,
      `${window.location.origin}/viewer/${encodeURIComponent(asset.document.id)}`,
    );
  }, [handleImageFile]);

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
      const groupPath = shapes[idx].source?.groupPath;
      if (groupPath?.length) {
        const groupKey = groupPath.join("/");
        const peerIndexes = shapes.flatMap((shape, index) => (
          shape.source?.part === shapes[idx].source?.part
          && shape.source?.groupPath?.join("/") === groupKey
            ? [index]
            : []
        ));
        const peerShapes = peerIndexes.map((index) => shapes[index]);
        const peerIndex = peerShapes.findIndex((shape) => shape.id === shapeId);
        if (peerIndex < 0) return s;
        const [item] = peerShapes.splice(peerIndex, 1);
        if (dir === "up") peerShapes.splice(Math.min(peerShapes.length, peerIndex + 1), 0, item);
        else if (dir === "down") peerShapes.splice(Math.max(0, peerIndex - 1), 0, item);
        else if (dir === "top") peerShapes.push(item);
        else peerShapes.unshift(item);
        peerIndexes.forEach((shapeIndex, index) => { shapes[shapeIndex] = peerShapes[index]; });
        return { ...s, shapes };
      }
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

  const duplicateSlide = useCallback(async () => {
    const src = slides[activeIdx];
    if (!src) return;
    let duplicatedShapes: PptxShape[];
    try {
      duplicatedShapes = await Promise.all(
        presentationShapesForDuplicateSlide(src.shapes, src.sourcePart)
          .map((shape) => clonePptxShapeForEditor(shape, 0, false)),
      );
    } catch {
      showPresentationError(t("page.doc_editor.presentation_object_copy_failed"));
      return;
    }
    const currentSlides = slidesRef.current;
    const sourceIndex = currentSlides.findIndex((slide) => slide.id === src.id);
    if (sourceIndex < 0) return;
    const dup: PptxSlide = {
      ...(JSON.parse(JSON.stringify(src)) as PptxSlide),
      id: genId(),
      shapes: duplicatedShapes,
    };
    const shouldSelectDuplicate = currentSlides[activeIdxRef.current]?.id === src.id;
    const next = [
      ...currentSlides.slice(0, sourceIndex + 1),
      dup,
      ...currentSlides.slice(sourceIndex + 1),
    ];
    updateSlides(next);
    if (shouldSelectDuplicate) {
      setActiveIdx(sourceIndex + 1);
      setSelectedShapeId(null);
      setEditingText(null);
      setEditingTableCell(null);
    }
  }, [activeIdx, showPresentationError, slides, updateSlides]);

  const duplicateShape = useCallback(async (shapeId: string) => {
    const source = activeSlide.shapes.find((shape) => shape.id === shapeId);
    if (!source) return;
    let duplicate: PptxShape;
    try {
      duplicate = await clonePptxShapeForEditor(source, 2);
    } catch {
      showPresentationError(t("page.doc_editor.presentation_object_copy_failed"));
      return;
    }
    const currentSlides = slidesRef.current;
    const sourceSlideIndex = currentSlides.findIndex((slide) => slide.id === activeSlide.id);
    if (sourceSlideIndex < 0) return;
    const shouldSelectDuplicate = currentSlides[activeIdxRef.current]?.id === activeSlide.id;
    const next = currentSlides.map((slide, index) => index === sourceSlideIndex
      ? { ...slide, shapes: [...slide.shapes, duplicate] }
      : slide);
    updateSlides(next);
    if (shouldSelectDuplicate) {
      setSelectedShapeId(duplicate.id);
      setEditingText(null);
      setEditingTableCell(null);
    }
  }, [activeSlide.id, activeSlide.shapes, showPresentationError, updateSlides]);

  const copyShape = useCallback(async (shapeId: string) => {
    const source = activeSlide.shapes.find((shape) => shape.id === shapeId);
    if (!source) return;
    const requestId = copyShapeRequestRef.current + 1;
    copyShapeRequestRef.current = requestId;
    try {
      const copy = await clonePptxShapeForEditor(source);
      if (copyShapeRequestRef.current === requestId) copiedShapeRef.current = copy;
    } catch {
      if (copyShapeRequestRef.current === requestId) {
        showPresentationError(t("page.doc_editor.presentation_object_copy_failed"));
      }
    }
  }, [activeSlide.shapes, showPresentationError]);

  const pasteShape = useCallback(async () => {
    if (!copiedShapeRef.current) return;
    const preserveSource = copiedShapeRef.current.source?.part === activeSlide.sourcePart;
    let duplicate: PptxShape;
    try {
      duplicate = await clonePptxShapeForEditor(copiedShapeRef.current, 2, preserveSource);
    } catch {
      showPresentationError(t("page.doc_editor.presentation_object_copy_failed"));
      return;
    }
    const next = slides.map((slide, index) => index === activeIdx
      ? { ...slide, shapes: [...slide.shapes, duplicate] }
      : slide);
    updateSlides(next);
    copiedShapeRef.current = JSON.parse(JSON.stringify(duplicate)) as PptxShape;
    setSelectedShapeId(duplicate.id);
    setEditingText(null);
    setEditingTableCell(null);
  }, [activeIdx, activeSlide.sourcePart, showPresentationError, slides, updateSlides]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (presentingIdx !== null) return;
      if (isEditableDomTarget(event.target)) return;
      const command = event.metaKey || event.ctrlKey;
      if (command && event.key.toLowerCase() === "c" && selectedShapeId) {
        event.preventDefault();
        void copyShape(selectedShapeId);
        return;
      }
      if (command && event.key.toLowerCase() === "d" && selectedShapeId) {
        event.preventDefault();
        void duplicateShape(selectedShapeId);
        return;
      }
      if (command && event.key.toLowerCase() === "v" && copiedShapeRef.current) {
        event.preventDefault();
        void pasteShape();
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
          if (merged.source?.groupTransform) {
            merged.w = Math.max(1, merged.w);
            merged.h = Math.max(minHeight, merged.h);
          } else {
            merged.w = Math.max(1, Math.min(100, merged.w));
            merged.h = Math.max(minHeight, Math.min(100, merged.h));
            merged.x = Math.max(0, Math.min(100 - merged.w, merged.x));
            merged.y = Math.max(0, Math.min(100 - merged.h, merged.y));
          }
          if (merged.opacity != null) merged.opacity = Math.max(0, Math.min(1, merged.opacity));
          if (merged.imgUrl && merged.imageFit === "contain" && ("w" in update || "h" in update)) {
            merged.imageFit = "fill";
          }
          return merged;
        }),
      };
    });
    updateSlides(next);
  }, [slides, activeIdx, updateSlides]);

  const updateImageFit = useCallback(async (shapeId: string, imageFit: "cover" | "contain" | "fill") => {
    const requestedSlideId = slidesRef.current[activeIdx]?.id;
    const requestedShape = slidesRef.current[activeIdx]?.shapes.find((shape) => shape.id === shapeId);
    if (!requestedSlideId || !requestedShape?.imgUrl) return;
    const requestedImageUrl = requestedShape.imgUrl;
    const requestRevision = (imageFitRequestRef.current.get(shapeId) || 0) + 1;
    imageFitRequestRef.current.set(shapeId, requestRevision);

    let naturalSize: { width: number; height: number } | null = null;
    if (imageFit !== "fill") {
      try {
        naturalSize = await pptxImageNaturalSize(requestedImageUrl);
      } catch {
        if (imageFitRequestRef.current.get(shapeId) === requestRevision) {
          showPresentationError(t("page.doc_editor.image_fit_failed"));
        }
        return;
      }
    }

    if (imageFitRequestRef.current.get(shapeId) !== requestRevision) return;
    const currentSlides = slidesRef.current;
    const slideIndex = currentSlides.findIndex((slide) => slide.id === requestedSlideId);
    const currentShape = currentSlides[slideIndex]?.shapes.find((shape) => shape.id === shapeId);
    if (slideIndex < 0 || currentShape?.imgUrl !== requestedImageUrl) return;

    let update: Partial<PptxShape> = { imageFit, imgCrop: undefined };
    if (naturalSize) {
      const imageAspect = naturalSize.width / Math.max(1, naturalSize.height);
      const slideAspect = pptxSlideAspectRatio(currentSlides[slideIndex].aspectRatio);
      const frameAspect = (currentShape.w * slideAspect) / Math.max(0.01, currentShape.h);
      if (imageFit === "cover") {
        const horizontalCrop = imageAspect > frameAspect ? ((1 - frameAspect / imageAspect) * 100) / 2 : 0;
        const verticalCrop = imageAspect < frameAspect ? ((1 - imageAspect / frameAspect) * 100) / 2 : 0;
        update = {
          imageFit,
          imgCrop: {
            l: horizontalCrop,
            t: verticalCrop,
            r: horizontalCrop,
            b: verticalCrop,
          },
        };
      } else {
        let width = currentShape.w;
        let height = currentShape.h;
        if (imageAspect > frameAspect) height = (width * slideAspect) / imageAspect;
        else width = (height * imageAspect) / slideAspect;
        width = Math.max(1, Math.min(100, width));
        height = Math.max(1, Math.min(100, height));
        update = {
          imageFit,
          imgCrop: undefined,
          x: Math.max(0, Math.min(100 - width, currentShape.x + (currentShape.w - width) / 2)),
          y: Math.max(0, Math.min(100 - height, currentShape.y + (currentShape.h - height) / 2)),
          w: width,
          h: height,
        };
      }
    }

    const next = currentSlides.map((slide, index) => index === slideIndex
      ? { ...slide, shapes: slide.shapes.map((shape) => shape.id === shapeId ? { ...shape, ...update } : shape) }
      : slide);
    pushUndo(currentSlides);
    slidesRef.current = next;
    setSlides(next);
    onChange(next);
  }, [activeIdx, onChange, pushUndo, showPresentationError]);

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
          shapes: s.shapes.map((sh) => {
            if (sh.id !== dragging.shapeId) return sh;
            const localDelta = pptxShapeLocalDelta(sh, dx, dy);
            const x = dragging.origX + localDelta.dx;
            const y = dragging.origY + localDelta.dy;
            return sh.source?.groupTransform
              ? { ...sh, x, y }
              : { ...sh, x: Math.max(0, Math.min(100 - sh.w, x)), y: Math.max(0, Math.min(100 - sh.h, y)) };
          }),
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
  const handleResizeStart = useCallback((e: React.MouseEvent, shapeId: string, handle: PresentationResizeHandle) => {
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
      const next = resizing.baseSlides.map((s, i) => {
        if (i !== resizing.slideIdx) return s;
        return {
          ...s,
            shapes: s.shapes.map(sh => {
              if (sh.id !== resizing.shapeId) return sh;
              const minW = sh.presetGeom === "line" ? 1 : 3;
              const minH = sh.presetGeom === "line" ? 0.5 : 3;
              const resized = presentationResizeRect(
                {
                  x: (resizing.origX / 100) * EDITOR_SLIDE_W,
                  y: (resizing.origY / 100) * EDITOR_SLIDE_H,
                  width: (resizing.origW / 100) * EDITOR_SLIDE_W,
                  height: (resizing.origH / 100) * EDITOR_SLIDE_H,
                  rotation: sh.rotation,
                  flipH: sh.flipH,
                  flipV: sh.flipV,
                },
                resizing.handle,
                {
                  x: (dx / 100) * EDITOR_SLIDE_W,
                  y: (dy / 100) * EDITOR_SLIDE_H,
                },
                sh.source?.groupTransform,
                {
                  width: (minW / 100) * EDITOR_SLIDE_W,
                  height: (minH / 100) * EDITOR_SLIDE_H,
                },
              );
              return {
                ...sh,
                x: emu2pctX(resized.x),
                y: emu2pctY(resized.y),
                w: emu2pctX(resized.width),
                h: emu2pctY(resized.height),
                imageFit: sh.imgUrl && sh.imageFit === "contain" ? "fill" : sh.imageFit,
              };
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
  const handleThumbDragStart = useCallback((idx: number) => {
    setDragThumbIdx(idx);
  }, []);
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
      {(["n", "s", "e", "w", "ne", "nw", "se", "sw"] as PresentationResizeHandle[]).map((handle) => {
        const pos: React.CSSProperties = {};
        if (handle.includes("n")) pos.top = 0;
        if (handle.includes("s")) pos.bottom = 0;
        if (handle.includes("e")) pos.right = 0;
        if (handle.includes("w")) pos.left = 0;
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
    <div className="presentation-editor" style={{ flex: 1, display: "flex", overflow: "hidden", "--pptx-point-scale": 540 / (slides.find((slide) => slide.heightPoints)?.heightPoints || 540) } as React.CSSProperties}>
      {/* Slide thumbnails */}
      <div className="presentation-editor-sidebar" style={{
        width: 180, flexShrink: 0, borderRight: "1px solid rgba(28,25,23,0.06)",
        background: "rgba(250,250,249,0.8)", overflow: "hidden",
        display: "flex", flexDirection: "column", minHeight: 0,
      }}>
        <div className="presentation-editor-slide-strip" role="tablist" aria-label={t("page.doc_editor.slides")}>
          {slides.map((slide, idx) => {
            return (
              <button
                ref={(button) => { thumbnailRefs.current[idx] = button; }}
                key={slide.id}
                type="button"
                role="tab"
                aria-label={`${t("page.file_viewer.slide")} ${idx + 1}`}
                aria-selected={idx === activeIdx}
                tabIndex={idx === activeIdx ? 0 : -1}
                className={`presentation-editor-thumb${idx === activeIdx ? " is-active" : ""}${dragOverIdx === idx ? " is-drag-over" : ""}`}
                draggable
                onClick={() => selectPresentationSlide(idx)}
                onKeyDown={(event) => handlePresentationThumbnailKeyDown(event, idx)}
                onDragStart={() => handleThumbDragStart(idx)}
                onDragOver={(e) => handleThumbDragOver(e, idx)}
                onDrop={() => handleThumbDrop(idx)}
                onDragEnd={() => { setDragThumbIdx(null); setDragOverIdx(null); }}
                style={{
                  width: "100%",
                  padding: 0,
                  appearance: "none",
                  color: "inherit",
                  background: "transparent",
                  font: "inherit",
                  textAlign: "initial",
                  cursor: "grab",
                  borderRadius: 8,
                  border: idx === activeIdx ? "2px solid #4f7d75" : dragOverIdx === idx ? "2px solid #8aa9d1" : "2px solid transparent",
                  overflow: "hidden",
                  opacity: dragThumbIdx === idx ? 0.5 : 1,
                  transition: "border-color 0.15s, opacity 0.15s",
                }}
              >
                <PptxReadOnlySlide slide={slide} thumbnail />
                <div className="presentation-editor-thumb-label" style={{ fontSize: 10, textAlign: "center", color: "#78716c", padding: "4px 0" }}>
                  {idx + 1}
                </div>
              </button>
            );
          })}
        </div>
        <div className="presentation-editor-add-slide-controls">
          <button type="button" onClick={() => addSlide(PresentationSlideLayout.TitleBody)} className="presentation-editor-add-slide">
            {t("page.doc_editor.plus_add_slide")}
          </button>
          <select
            value=""
            aria-label={t("page.doc_editor.new_slide_layout")}
            onChange={(event) => addSlide(event.target.value as PresentationSlideLayout)}
          >
            <option value="" disabled>{t("page.doc_editor.layout")}</option>
            <option value={PresentationSlideLayout.TitleBody}>{t("page.doc_editor.layout_title_body")}</option>
            <option value={PresentationSlideLayout.TitleOnly}>{t("page.doc_editor.layout_title_only")}</option>
            <option value={PresentationSlideLayout.Section}>{t("page.doc_editor.layout_section")}</option>
            <option value={PresentationSlideLayout.Blank}>{t("page.doc_editor.layout_blank")}</option>
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
            <IconImage size={14} /> {t("component.media_insert.title")}
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
              {selectedShape && selectedShape.type !== "table" && selectedShape.type !== "graphic" && (
                <>
                  {!selectedShape.imgUrl && (
                    <>
                      <span className="presentation-editor-field-label" style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.doc_editor.fill")}</span>
                      <input
                        type="color"
                        aria-label={t("page.doc_editor.fill")}
                        value={pptxColorInputValue(selectedShape.fill, "#ffffff")}
                        onChange={(event) => updateShapeFill(selectedShapeId, event.target.value)}
                        style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
                      />
                    </>
                  )}
                  <span className="presentation-editor-field-label" style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.doc_editor.stroke")}</span>
                  <input
                    type="color"
                    aria-label={t("page.doc_editor.stroke")}
                    value={pptxColorInputValue(selectedShape.stroke, "#1c1917")}
                    onChange={(event) => updateShapeProps(selectedShapeId, { stroke: event.target.value })}
                    style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
                  />
                  <input
                    type="number"
                    min={0}
                    max={20}
                    step={0.5}
                    value={selectedShape.strokeWidth ?? 0}
                    onChange={(event) => updateShapeProps(selectedShapeId, {
                      strokeWidth: Math.max(0, Number(event.target.value) || 0),
                      stroke: selectedShape.stroke || "#1c1917",
                    })}
                    title={t("page.doc_editor.stroke")}
                    style={{ width: 48, fontSize: 12, padding: "2px 4px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4 }}
                  />
                </>
              )}
              {selectedShape?.imgUrl && (
                <select
                  value={selectedShape.imageFit || (selectedShape.imgCrop ? "cover" : "fill")}
                  onChange={(event) => void updateImageFit(selectedShapeId, event.target.value as "cover" | "contain" | "fill")}
                  style={{ fontSize: 12, padding: "3px 8px", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 6, background: "#fff" }}
                >
                  <option value="fill">{t("page.doc_editor.stretch_image")}</option>
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
                    value={pptxColorInputValue(selectedText.color, "#000000")}
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
              value={pptxColorInputValue(activeSlide.bg, "#ffffff")}
              onChange={(e) => updateSlideBg(e.target.value)}
              style={{ width: 24, height: 24, border: "1px solid rgba(28,25,23,0.06)", borderRadius: 4, cursor: "pointer", padding: 0 }}
            />
            {slides.length > 1 && (
              <button onClick={deleteSlide} className="btn-manor-ghost" style={{ fontSize: 12, padding: "4px 12px", color: "#c14a44" }}>
                {t("page.doc_editor.delete_slide")}
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
            ...slideBg,
            borderRadius: 8, boxShadow: "0 8px 32px rgba(0,0,0,0.15)",
            overflow: "hidden",
          }}>
            {activeSlide.shapes.map((shape) => {
              const isSelected = selectedShapeId === shape.id;
              const shapeStyle = pptxShapeVisualStyle(shape);
              const borderRadius = shapeStyle.borderRadius;

              // Table rendering
              if (shape.type === "table" && shape.tableRows) {
                return (
                  <div
                    key={shape.id}
                    onClick={(e) => { e.stopPropagation(); setSelectedShapeId(shape.id); }}
                    onMouseDown={(e) => handleDragStart(e, shape.id)}
                    onContextMenu={(event) => openShapeContextMenu(event, shape.id)}
                    style={{
                      ...shapeStyle,
                      cursor: editingTableCell?.shapeId === shape.id ? "text" : "move",
                      outline: isSelected ? "2px solid #4f7d75" : undefined,
                      outlineOffset: 2,
                    }}
                  >
                    <table className="presentation-editor-present-table">
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
                          <tr key={ri} style={pptxTableRowVisualStyle(shape, ri)}>
                            {row.map((cell, ci) => {
                              if (cell.vMerge) return null;
                              const isEditingCell = editingTableCell?.shapeId === shape.id
                                && editingTableCell.rowIdx === ri
                                && editingTableCell.cellIdx === ci;
                              return (
                                <td
                                  key={ci}
                                  colSpan={cell.gridSpan}
                                  className="presentation-editor-edit-target"
                                  tabIndex={isEditingCell ? -1 : 0}
                                  aria-label={`${t("page.doc_editor.table")} ${ri + 1}, ${ci + 1}`}
                                  aria-keyshortcuts="Enter F2"
                                  onDoubleClick={(event) => {
                                    event.stopPropagation();
                                    beginTableCellEditing(shape.id, ri, ci, cell);
                                  }}
                                  onPointerUp={(event) => {
                                    if (event.pointerType === "mouse" || isEditingCell) return;
                                    event.stopPropagation();
                                    beginTableCellEditing(shape.id, ri, ci, cell);
                                  }}
                                  onKeyDown={(event) => {
                                    if (
                                      event.currentTarget === event.target
                                      && (event.key === "Enter" || event.key === "F2")
                                    ) {
                                      event.preventDefault();
                                      event.stopPropagation();
                                      beginTableCellEditing(shape.id, ri, ci, cell);
                                    }
                                  }}
                                  style={{
                                    ...pptxTableCellVisualStyle(cell, ri),
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                  }}>
                                  {isEditingCell ? (
                                    <textarea
                                      autoFocus
                                      value={editingTableCell.value}
                                      rows={Math.max(1, editingTableCell.value.split("\n").length)}
                                      aria-label={`${t("page.doc_editor.table")} ${ri + 1}, ${ci + 1}`}
                                      onBeforeInput={(event) => {
                                        const inputEvent = event.nativeEvent as InputEvent;
                                        tableCellBeforeInputRef.current = {
                                          selection: {
                                            start: Array.from(event.currentTarget.value.slice(0, event.currentTarget.selectionStart)).length,
                                            end: Array.from(event.currentTarget.value.slice(0, event.currentTarget.selectionEnd)).length,
                                          },
                                          inputType: inputEvent.inputType,
                                          data: inputEvent.data,
                                        };
                                      }}
                                      onChange={(event) => {
                                        const nextValue = normalizedPptxEditableText(event.target.value);
                                        setEditingTableCell((current) => {
                                          if (!current) return current;
                                          const pending = tableCellBeforeInputRef.current;
                                          const directSpan = pending
                                            ? pptxDirectInputSpan(
                                                current.value,
                                                nextValue,
                                                pending.selection,
                                                pending.inputType,
                                                pending.data,
                                              )
                                            : null;
                                          tableCellBeforeInputRef.current = null;
                                          return {
                                            ...current,
                                            value: nextValue,
                                            sourceMap: reconcilePresentationTextSourceMap(
                                              current.value,
                                              nextValue,
                                              current.sourceMap,
                                              directSpan ? [directSpan] : undefined,
                                            ),
                                          };
                                        });
                                      }}
                                      onBlur={commitTableCellEditing}
                                      onMouseDown={(event) => event.stopPropagation()}
                                      onClick={(event) => event.stopPropagation()}
                                      onKeyDown={(event) => {
                                        if (event.key === "Escape") {
                                          event.preventDefault();
                                          tableCellBeforeInputRef.current = null;
                                          setEditingTableCell(null);
                                        } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                                          event.preventDefault();
                                          event.currentTarget.blur();
                                        }
                                      }}
                                      className="presentation-editor-inline-input"
                                      style={{ color: "inherit", background: "inherit" }}
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
                  data-presentation-shape-type={shape.type || "shape"}
                  onClick={(e) => { e.stopPropagation(); setSelectedShapeId(shape.id); }}
                  onMouseDown={(e) => handleDragStart(e, shape.id)}
                  onContextMenu={(event) => openShapeContextMenu(event, shape.id)}
                  style={{
                    ...shapeStyle,
                    cursor: editingText?.shapeId === shape.id ? "text" : "move",
                    outline: isSelected ? "2px solid rgba(79,125,117,0.3)" : undefined,
                    outlineOffset: 2,
                  }}
                >
                  <PptxShapeGeometry shape={shape} />
                  {shape.type === "graphic" && (
                    <PptxGraphicFramePreview shape={shape} />
                  )}
                  {shape.videoUrl ? (
                    <video
                      className="presentation-editor-native-video presentation-editor-native-video--shape"
                      src={shape.videoUrl}
                      poster={shape.imgUrl}
                      controls
                      playsInline
                      preload="metadata"
                      aria-label="Presentation video"
                      onMouseDown={(event) => event.stopPropagation()}
                      style={{ opacity: shape.opacity }}
                    />
                  ) : shape.imgUrl && (
                    shape.imgCrop ? (
                      <div style={{
                        position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
                        overflow: "hidden", zIndex: 0, borderRadius: borderRadius || 0,
                      }}>
                        <img src={shape.imgUrl} alt={shape.altText || ""} style={{
                          ...pptxEditorImageStyle(shape, borderRadius),
                        }} />
                      </div>
                    ) : (
                      <img src={shape.imgUrl} alt={shape.altText || ""} style={pptxEditorImageStyle(shape, borderRadius)} />
                    )
                  )}
                  {shape.texts.map((t, ti) => {
                    const isEditing = editingText?.shapeId === shape.id && editingText?.textIdx === ti;
                    return (
                      <div
                        key={ti}
                        className="presentation-editor-edit-target"
                        tabIndex={isEditing ? -1 : 0}
                        role={isEditing ? undefined : "textbox"}
                        aria-readonly={isEditing ? undefined : true}
                        aria-label={t.text || shape.type || "text"}
                        aria-keyshortcuts="Enter F2"
                        onDoubleClick={(e) => {
                          e.stopPropagation();
                          beginTextEditing(shape.id, ti, t.text);
                        }}
                        onPointerUp={(event) => {
                          if (event.pointerType === "mouse" || isEditing) return;
                          event.stopPropagation();
                          beginTextEditing(shape.id, ti, t.text);
                        }}
                        onKeyDown={(event) => {
                          if (
                            event.currentTarget === event.target
                            && (event.key === "Enter" || event.key === "F2")
                          ) {
                            event.preventDefault();
                            event.stopPropagation();
                            beginTextEditing(shape.id, ti, t.text);
                          }
                        }}
                        style={pptxParagraphVisualStyle(t, activeSlide, shape)}
                      >
                        {isEditing ? (
                          <>
                            <PptxBulletMarker paragraph={t} />
                            <span
                              ref={inlineTextEditorRef}
                              contentEditable
                              suppressContentEditableWarning
                              role="textbox"
                              aria-multiline="true"
                              onBlur={(event) => commitTextEditing(normalizedPptxEditableText(event.currentTarget.innerText))}
                              onMouseDown={(event) => event.stopPropagation()}
                              onClick={(event) => event.stopPropagation()}
                              onBeforeInput={(event) => {
                                const selection = pptxContentEditableSelection(event.currentTarget);
                                const inputEvent = event.nativeEvent as InputEvent;
                                inlineTextBeforeInputRef.current = selection ? {
                                  selection,
                                  inputType: inputEvent.inputType,
                                  data: inputEvent.data,
                                } : null;
                              }}
                              onInput={(event) => {
                                const nextText = normalizedPptxEditableText(event.currentTarget.innerText);
                                const session = inlineTextEditSessionRef.current;
                                if (
                                  !session
                                  || session.shapeId !== editingText.shapeId
                                  || session.textIdx !== editingText.textIdx
                                ) return;
                                const pending = inlineTextBeforeInputRef.current;
                                const directSpan = pending
                                  ? pptxDirectInputSpan(
                                      session.text,
                                      nextText,
                                      pending.selection,
                                      pending.inputType,
                                      pending.data,
                                    )
                                  : null;
                                session.sourceMap = directSpan
                                  ? reconcilePresentationTextSourceMap(session.text, nextText, session.sourceMap, [directSpan])
                                  : reconcilePresentationTextSourceMap(session.text, nextText, session.sourceMap);
                                session.text = nextText;
                                inlineTextBeforeInputRef.current = null;
                              }}
                              onPaste={(event) => {
                                event.preventDefault();
                                document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
                              }}
                              onKeyDown={(event) => {
                                if (event.key === "Escape") {
                                  event.preventDefault();
                                  event.currentTarget.innerText = editingText.initialValue;
                                  event.currentTarget.blur();
                                } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                                  event.preventDefault();
                                  event.currentTarget.blur();
                                }
                              }}
                              style={{ outline: "none", cursor: "text", minWidth: 1 }}
                            >
                              <PptxRunsContent paragraph={t} />
                            </span>
                          </>
                        ) : <PptxParagraphContent paragraph={t} />}
                      </div>
                    );
                  })}
                  {shape.type !== "graphic" && shape.texts.length === 0 && !shape.imgUrl && !shape.tableRows && isSelected && (
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
                {selectedShape.type === "image" && (
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
                )}
              </section>

              {selectedShape.imgUrl && (
                <section className="presentation-editor-format-section">
                  <h3>{t("page.doc_editor.image")}</h3>
                  <label className="presentation-editor-format-row">
                    <span>{t("page.doc_editor.image_fit")}</span>
                    <select
                      value={selectedShape.imageFit || (selectedShape.imgCrop ? "cover" : "fill")}
                      onChange={(event) => void updateImageFit(selectedShape.id, event.target.value as "cover" | "contain" | "fill")}
                    >
                      <option value="fill">{t("page.doc_editor.stretch_image")}</option>
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
            <PptxReadOnlySlide slide={slides[presentingIdx]} />
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
      <MediaInsertDialog
        open={mediaInsertOpen}
        onClose={() => setMediaInsertOpen(false)}
        onInsert={handleMediaInsert}
      />
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
  const { data: aiEditPreferences } = useQuery({
    queryKey: ["preferences"],
    queryFn: () => api.admin.getPreferences(),
    enabled: Boolean(currentUser),
  });
  const aiEditDisplayMode = resolveAiEditDisplayMode(aiEditPreferences);
  const showSaveSuccess = useToastStore((s) => s.success);
  const showSaveError = useToastStore((s) => s.error);
  const [content, setContent] = useState("");
  const [textFileFormat, setTextFileFormat] = useState<PreservedTextFormat | null>(null);
  const [textBytesReadyDocumentId, setTextBytesReadyDocumentId] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<"saved" | "saving" | "unsaved">("saved");
  const [showPreview, setShowPreview] = useState(true);
  const [markdownViewMode, setMarkdownViewMode] = useState<MarkdownViewMode>("split");
  const [showVersions, setShowVersions] = useState(false);
  const [showComments, setShowComments] = useState(false);
  const [commentAnchor, setCommentAnchor] = useState<CommentAnchor | null>(null);
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null);
  const [documentComments, setDocumentComments] = useState<Comment[]>([]);
  const [lineCount, setLineCount] = useState(1);
  const [codeCursor, setCodeCursor] = useState({ line: 1, column: 1 });
  const [richTextBlock, setRichTextBlock] = useState("p");
  const [richTextFont, setRichTextFont] = useState("Inter");
  const [richTextSize, setRichTextSize] = useState("16");
  const [liveDiff, setLiveDiff] = useState<string | null>(null);
  const [liveEditNotice, setLiveEditNotice] = useState<string | null>(null);
  const [liveEditPreview, setLiveEditPreview] = useState<EditorLivePreviewState | null>(null);
  const [liveEditAccepting, setLiveEditAccepting] = useState(false);
  const [documentMediaInsertOpen, setDocumentMediaInsertOpen] = useState(false);
  const [, setPlainTextHistoryRevision] = useState(0);
  const [savedFeedbackVisible, setSavedFeedbackVisible] = useState(false);

  // For DOCX: convert to HTML for editing, track original import
  const [docxHtml, setDocxHtml] = useState<string | null>(null);
  const [docxRender, setDocxRender] = useState<ManorDocumentRender | null>(null);
  const [docxLoading, setDocxLoading] = useState(false);
  const [docxLoadError, setDocxLoadError] = useState<string | null>(null);

  // For XLSX: parsed sheet data
  const [sheetData, setSheetData] = useState<any[][] | null>(null);
  const [sheetCharts, setSheetCharts] = useState<SheetChartConfig[]>([]);
  const [sheetStyles, setSheetStyles] = useState<SheetStyleMap>({});
  const [xlsxLoading, setXlsxLoading] = useState(false);
  const [xlsxLoadError, setXlsxLoadError] = useState<string | null>(null);
  const [xlsxSheets, setXlsxSheets] = useState<SpreadsheetSheetModel[]>([]);
  const [xlsxActiveSheetIndex, setXlsxActiveSheetIndex] = useState(0);

  // For PPTX: parsed structured slides + server-rendered thumbnail references
  const [pptxSlides, setPptxSlides] = useState<PptxSlide[]>([]);
  const [pptxLoading, setPptxLoading] = useState(false);
  const [pptxLoadError, setPptxLoadError] = useState<string | null>(null);
  const [pptxServerUrls, setPptxServerUrls] = useState<string[]>([]);
  const [pptxLiveEditTarget, setPptxLiveEditTarget] = useState<PresentationLiveEditTarget>({
    activeSlideIndex: 0,
    selectedShapeId: null,
  });

  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedFeedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const previousDisplayedSaveStatusRef = useRef<{ scope: string; status: string } | null>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const richSelectionRef = useRef<Range | null>(null);
  const richTextInternalDragRef = useRef(false);
  const markdownRef = useRef<HTMLTextAreaElement>(null);
  const markdownPreviewRef = useRef<HTMLDivElement>(null);
  const markdownPreviewSelectionRef = useRef<Range | null>(null);
  const commentSelectionSurfaceRef = useRef<"editor" | "markdown-preview">("editor");
  const textRef = useRef<HTMLTextAreaElement>(null);
  const plainTextHistoryRef = useRef(createPlainTextHistory());
  const plainTextSelectionRef = useRef<PlainTextSelection>({ start: 0, end: 0, direction: "none" });
  const plainTextGenerationCounterRef = useRef(0);
  const plainTextGenerationRef = useRef(0);
  const plainTextCompositionRef = useRef<{
    text: string;
    selection: PlainTextSelection;
    generation: number;
    ended: boolean;
  } | null>(null);
  const codeRef = useRef<HTMLTextAreaElement>(null);
  const codeGutterRef = useRef<HTMLDivElement>(null);
  const codeHighlightRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef("");
  const liveEditPreviewRef = useRef<EditorLivePreviewState | null>(null);
  const editorLiveTurnPreviewCheckpointRef = useRef<EditorLivePreviewState | null>(null);
  const openedEditorLiveDetailRef = useRef<EditorLiveChatDetail | null>(null);
  const liveEditCommitCoordinator = useRef(createAiEditCommitCoordinator()).current;
  const shouldBlockLiveEditNavigation = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => {
      if (!liveEditPreviewRef.current) return false;
      const currentUrl = `${currentLocation.pathname}${currentLocation.search}${currentLocation.hash}`;
      const nextUrl = `${nextLocation.pathname}${nextLocation.search}${nextLocation.hash}`;
      return currentUrl !== nextUrl;
    },
    [],
  );
  const liveEditNavigationBlocker = useBlocker(shouldBlockLiveEditNavigation);
  const liveEditAnimationRevisionRef = useRef(0);
  const textLikeEditRevisionRef = useRef(0);
  const textSaveSessionRevisionRef = useRef(0);
  const textSaveEditRevisionRef = useRef(0);
  const textSavePersistedRevisionRef = useRef(0);
  const pendingTextSaveRef = useRef<TextSaveRequest | null>(null);
  const textSaveMutateRef = useRef<(request: TextSaveRequest) => void>(() => undefined);
  const textOriginalBufferRef = useRef<ArrayBuffer | null>(null);
  const textBaselineRef = useRef<string | null>(null);
  const textFileFormatRef = useRef<PreservedTextFormat | null>(null);
  const textFormatLoadRef = useRef<{
    documentId: string;
    sessionRevision: number;
    promise: Promise<TextFileSaveSnapshot | null>;
  } | null>(null);
  const textSaveQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const csvFormatRef = useRef<DelimitedTextFormat>({ delimiter: ",", lineEnding: "\n", finalLineEnding: false });
  const docxOriginalBufferRef = useRef<ArrayBuffer | null>(null);
  const docxBaselineHtmlRef = useRef<string | null>(null);
  const docxExpectedSourceSha256Ref = useRef<string | null>(null);
  const docxPaginationFrameRef = useRef<number | null>(null);
  const sheetDataRef = useRef<any[][] | null>(null);
  const sheetChartsRef = useRef<SheetChartConfig[]>([]);
  const sheetStylesRef = useRef<SheetStyleMap>({});
  const xlsxSheetsRef = useRef<SpreadsheetSheetModel[]>([]);
  const xlsxNumberFormatterRef = useRef<SpreadsheetNumberFormatter | null>(null);
  const xlsxOriginalBufferRef = useRef<ArrayBuffer | null>(null);
  const xlsxExpectedSourceSha256Ref = useRef<string | null>(null);
  const xlsxBaselineSheetsRef = useRef<SpreadsheetSheetSnapshot[] | null>(null);
  const xlsxActiveSheetIndexRef = useRef(0);
  const xlsxSaveSessionRevisionRef = useRef(0);
  const xlsxSaveRevisionRef = useRef(0);
  const pendingSpreadsheetSaveRef = useRef<SpreadsheetSaveRequest | null>(null);
  const spreadsheetSaveMutateRef = useRef<(request: SpreadsheetSaveRequest) => void>(() => undefined);
  const xlsxSaveQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const pptxSlidesRef = useRef<PptxSlide[]>([]);
  const pptxOriginalBufferRef = useRef<ArrayBuffer | null>(null);
  const pptxExpectedSourceSha256Ref = useRef<string | null>(null);
  const pptxBaselineSlidesRef = useRef<PptxSlide[] | null>(null);
  const pptxSaveSessionRevisionRef = useRef(0);
  const pptxSaveRevisionRef = useRef(0);
  const pendingPresentationSaveRef = useRef<PresentationSaveRequest | null>(null);
  const presentationSaveMutateRef = useRef<(request: PresentationSaveRequest) => void>(() => undefined);
  const pptxSaveQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const pptxServerObjectUrlsRef = useRef<string[]>([]);
  const pptxRenderRequestRef = useRef(0);
  const pptxGraphicObjectUrlsRef = useRef<string[]>([]);
  const pptxGraphicPreviewRequestRef = useRef(0);
  const pptxGraphicPreviewAbortRef = useRef<AbortController | null>(null);
  const knowledgeReturnTo = getKnowledgeReturnTo(location.state);

  const replacePptxServerUrls = useCallback((nextUrls: string[]) => {
    pptxServerObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    pptxServerObjectUrlsRef.current = nextUrls;
    setPptxServerUrls(nextUrls);
  }, []);

  const replacePptxGraphicObjectUrls = useCallback((nextUrls: string[]) => {
    pptxGraphicObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    pptxGraphicObjectUrlsRef.current = nextUrls;
  }, []);

  const refreshPptxGraphicPreviews = useCallback(async (
    documentId: string,
    slides: PptxSlide[],
  ): Promise<Map<string, string> | null> => {
    pptxGraphicPreviewAbortRef.current?.abort();
    const controller = new AbortController();
    pptxGraphicPreviewAbortRef.current = controller;
    const requestRevision = pptxGraphicPreviewRequestRef.current + 1;
    pptxGraphicPreviewRequestRef.current = requestRevision;
    const tasks = slides.flatMap((slide, slideIndex) => slide.shapes.flatMap((shape) => {
      const source = shape.source;
      if (
        shape.type !== "graphic"
        || !source
        || source.part !== slide.sourcePart
        || source.kind !== "graphicFrame"
      ) return [];
      return [{ slideIndex, shapeId: shape.id, objectId: source.objectId }];
    }));
    if (!tasks.length) {
      if (pptxGraphicPreviewAbortRef.current === controller) {
        pptxGraphicPreviewAbortRef.current = null;
      }
      replacePptxGraphicObjectUrls([]);
      return new Map();
    }
    const previews = new Map<string, string>();
    const createdUrls: string[] = [];
    let cursor = 0;
    const worker = async () => {
      while (
        cursor < tasks.length
        && !controller.signal.aborted
        && requestRevision === pptxGraphicPreviewRequestRef.current
      ) {
        const task = tasks[cursor];
        cursor += 1;
        try {
          const blob = await api.documents.presentationObjectBlob(
            documentId,
            task.slideIndex,
            task.objectId,
            controller.signal,
          );
          if (
            controller.signal.aborted
            || requestRevision !== pptxGraphicPreviewRequestRef.current
          ) break;
          const url = URL.createObjectURL(blob);
          createdUrls.push(url);
          previews.set(task.shapeId, url);
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") break;
          // Keep the object selectable and editable even when its visual renderer
          // cannot represent this specific vendor extension yet.
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(2, tasks.length) }, worker));
    if (pptxGraphicPreviewAbortRef.current === controller) {
      pptxGraphicPreviewAbortRef.current = null;
    }
    if (
      controller.signal.aborted
      || requestRevision !== pptxGraphicPreviewRequestRef.current
    ) {
      createdUrls.forEach((url) => URL.revokeObjectURL(url));
      return null;
    }
    replacePptxGraphicObjectUrls(createdUrls);
    return previews;
  }, [replacePptxGraphicObjectUrls]);

  const refreshPptxServerUrls = useCallback(async (documentId: string) => {
    const requestRevision = pptxRenderRequestRef.current + 1;
    pptxRenderRequestRef.current = requestRevision;
    const createdObjectUrls: string[] = [];
    const fetchWithTimeout = async (input: RequestInfo | URL, init: RequestInit = {}, timeoutMs = 8000) => {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(input, { ...init, cache: "no-store", signal: controller.signal });
      } finally {
        window.clearTimeout(timeout);
      }
    };

    try {
      const token = getAuthToken();
      const headers: Record<string, string> = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const slideRes = await fetchWithTimeout(`/api/v1/documents/${documentId}/slides`, { headers }, 30_000);
      if (!slideRes.ok) throw new Error("Slide rendering failed");
      const slideData = await slideRes.json();
      if (!Array.isArray(slideData.slides) || slideData.slides.length === 0) {
        throw new Error("No rendered slides available");
      }
      const renderedUrls = await Promise.all(slideData.slides.map(async (slide: { url: string }) => {
        try {
          const imageRes = await fetchWithTimeout(`/api/v1${slide.url}`, { headers });
          if (!imageRes.ok) return null;
          const objectUrl = URL.createObjectURL(await imageRes.blob());
          createdObjectUrls.push(objectUrl);
          return objectUrl;
        } catch {
          return null;
        }
      }));
      if (renderedUrls.some((url) => !url)) throw new Error("Slide image fetch failed");
      if (requestRevision !== pptxRenderRequestRef.current) {
        createdObjectUrls.forEach((url) => URL.revokeObjectURL(url));
        return null;
      }
      replacePptxServerUrls(renderedUrls as string[]);
      return renderedUrls as string[];
    } catch {
      createdObjectUrls.forEach((url) => URL.revokeObjectURL(url));
      if (requestRevision === pptxRenderRequestRef.current) replacePptxServerUrls([]);
      return null;
    }
  }, [replacePptxServerUrls]);

  useEffect(() => () => {
    pptxRenderRequestRef.current += 1;
    pptxGraphicPreviewRequestRef.current += 1;
    pptxGraphicPreviewAbortRef.current?.abort();
    pptxGraphicPreviewAbortRef.current = null;
    pptxServerObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    pptxServerObjectUrlsRef.current = [];
    pptxGraphicObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    pptxGraphicObjectUrlsRef.current = [];
  }, []);

  const advancePlainTextGeneration = useCallback(() => {
    const generation = plainTextGenerationCounterRef.current + 1;
    plainTextGenerationCounterRef.current = generation;
    plainTextGenerationRef.current = generation;
    return generation;
  }, []);

  const resetPlainTextEditorHistory = useCallback((selection: PlainTextSelection = { start: 0, end: 0, direction: "none" }) => {
    resetPlainTextHistory(plainTextHistoryRef.current);
    advancePlainTextGeneration();
    plainTextSelectionRef.current = selection;
    plainTextCompositionRef.current = null;
    setPlainTextHistoryRevision((revision) => revision + 1);
  }, [advancePlainTextGeneration]);

  useEffect(() => {
    const pendingTextSave = pendingTextSaveRef.current;
    const pendingSpreadsheetSave = pendingSpreadsheetSaveRef.current;
    const pendingPresentationSave = pendingPresentationSaveRef.current;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = null;
    pendingTextSaveRef.current = null;
    pendingSpreadsheetSaveRef.current = null;
    pendingPresentationSaveRef.current = null;
    if (pendingTextSave) textSaveMutateRef.current(pendingTextSave);
    if (pendingSpreadsheetSave) spreadsheetSaveMutateRef.current(pendingSpreadsheetSave);
    if (pendingPresentationSave) presentationSaveMutateRef.current(pendingPresentationSave);
    textSaveSessionRevisionRef.current += 1;
    textSaveEditRevisionRef.current = 0;
    textSavePersistedRevisionRef.current = 0;
    setSaveStatus("saved");
    setLiveDiff(null);
    setLiveEditNotice(null);
    liveEditAnimationRevisionRef.current += 1;
    liveEditPreviewRef.current = null;
    setLiveEditPreview(null);
    textOriginalBufferRef.current = null;
    textBaselineRef.current = null;
    textFileFormatRef.current = null;
    textFormatLoadRef.current = null;
    setTextBytesReadyDocumentId(null);
    textLikeEditRevisionRef.current = 0;
    resetPlainTextEditorHistory();
    csvFormatRef.current = { delimiter: ",", lineEnding: "\n", finalLineEnding: false };
    setTextFileFormat(null);
    docxOriginalBufferRef.current = null;
    docxBaselineHtmlRef.current = null;
    docxExpectedSourceSha256Ref.current = null;
    if (docxPaginationFrameRef.current != null) cancelAnimationFrame(docxPaginationFrameRef.current);
    docxPaginationFrameRef.current = null;
    setDocxHtml(null);
    setDocxRender(null);
    setDocxLoadError(null);
    xlsxSheetsRef.current = [];
    xlsxNumberFormatterRef.current = null;
    xlsxOriginalBufferRef.current = null;
    xlsxExpectedSourceSha256Ref.current = null;
    xlsxBaselineSheetsRef.current = null;
    xlsxSaveSessionRevisionRef.current += 1;
    xlsxSaveRevisionRef.current += 1;
    setXlsxSheets([]);
    xlsxActiveSheetIndexRef.current = 0;
    setXlsxActiveSheetIndex(0);
    setXlsxLoadError(null);
    pptxOriginalBufferRef.current = null;
    pptxExpectedSourceSha256Ref.current = null;
    pptxBaselineSlidesRef.current = null;
    pptxRenderRequestRef.current += 1;
    pptxGraphicPreviewRequestRef.current += 1;
    pptxGraphicPreviewAbortRef.current?.abort();
    pptxGraphicPreviewAbortRef.current = null;
    replacePptxServerUrls([]);
    replacePptxGraphicObjectUrls([]);
    setPptxLoadError(null);
    setPptxLiveEditTarget({ activeSlideIndex: 0, selectedShapeId: null });
    pptxSaveSessionRevisionRef.current += 1;
    pptxSaveRevisionRef.current += 1;
  }, [docId, replacePptxGraphicObjectUrls, replacePptxServerUrls, resetPlainTextEditorHistory]);
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
  const editorFile = OfficeEditorFileFactory.create(
    doc?.name || "",
    doc?.mime_type || undefined,
    doc?.file_type || undefined,
  );
  const isDocx = editorFile.kind === "document";
  const isXlsx = editorFile.kind === "spreadsheet" && editorFile.format !== OfficeEditorFormat.Csv;
  const isCsv = editorFile.format === OfficeEditorFormat.Csv;
  const isPptx = editorFile.kind === "presentation";
  const needsLegacyOfficeConversion = editorFile.requiresLegacyConversion;
  const canEditCurrentDoc = canEditDocument(currentUser, doc);
  const canCommentCurrentDoc = canCommentDocument(currentUser, doc);

  useEffect(() => {
    if (!doc || !currentUser || canEditCurrentDoc) return;
    navigate(`/viewer/${doc.id}`, { replace: true, state: location.state });
  }, [canEditCurrentDoc, currentUser, doc, location.state, navigate]);

  const { data: contentData, isLoading: contentLoading } = useQuery({
    queryKey: ["document-content", docId],
    queryFn: () => api.documents.getContent(docId!),
    enabled: !!docId && !!doc && !isDocx && !isXlsx && !isPptx,
  });

  const { data: versions, refetch: refetchVersions } = useQuery({
    queryKey: ["document-versions", docId],
    queryFn: () => api.documents.getVersions(docId!),
    enabled: !!docId && showVersions,
  });

  const { data: editorComments = EMPTY_COMMENTS } = useQuery<Comment[]>({
    queryKey: ["comments", "document", docId],
    queryFn: () => api.comments.list("document", docId!),
    enabled: !!docId && !!doc,
    retry: false,
  });

  useEffect(() => {
    setDocumentComments(editorComments);
  }, [editorComments]);

  const saveMutation = useMutation({
    mutationFn: (request: TextSaveRequest) => {
      const requireCurrentAuthToken = () => currentDocumentSaveAuthToken(request.authPrincipalKey);
      const {
        text,
        documentId,
        documentName,
        documentMimeType,
        docxOriginalBuffer,
        docxBaselineHtml,
      } = request;
      const saveIntentPromise = allocateEditorSaveIntent();
      const saveTask = textSaveQueueRef.current.catch(() => undefined).then(async () => {
        const saveIntent = await saveIntentPromise;
        if (!request.canEdit) throw new Error("You do not have edit access to this document");
        if (request.isDocx) {
          if (!documentName || !docxOriginalBuffer || docxBaselineHtml == null) {
            throw new Error("The original Word package is unavailable; reload the document before saving.");
          }
          const { editDocumentFileWithSnapshot } = await import("../lib/documentOoxml");
          const { file, savedBuffer, savedHtml } = await editDocumentFileWithSnapshot(
            docxOriginalBuffer,
            docxBaselineHtml,
            text,
            documentName,
          );
          const expectedSourceSha256 = request.docxExpectedSourceSha256;
          if (!expectedSourceSha256) {
            throw new Error("The original Word source version is unavailable; reload the document before saving.");
          }
          return retryDocumentSave(async () => {
            const updatedDocument = await api.documents.replaceFile(
              documentId,
              file,
              saveIntent,
              requireCurrentAuthToken(),
              expectedSourceSha256,
            );
            return {
              request,
              updatedDocument,
              savedBuffer,
              savedHtml,
              savedTextBuffer: null,
              savedText: null,
              savedTextFormat: null,
            };
          });
        }
        const loadedTextFormat = await request.textFormatLoad;
        const textOriginalBuffer = request.textOriginalBuffer ?? loadedTextFormat?.originalBuffer ?? null;
        const textBaseline = request.textBaseline ?? loadedTextFormat?.baseline ?? null;
        const textFormat = request.textFormat ?? loadedTextFormat?.format ?? null;
        if (documentName && textOriginalBuffer && textBaseline != null && textFormat) {
          const savedTextFormat = textFileFormatForSave(textFormat);
          const bytes = encodeTextFile(text, textBaseline, textFormat);
          const fileBytes = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
          const file = new File([fileBytes], documentName, { type: documentMimeType || "text/plain;charset=utf-8" });
          const expectedSourceSha256 = await sha256Hex(textOriginalBuffer);
          return retryDocumentSave(async () => {
            const updatedDocument = await api.documents.replaceFile(
              documentId,
              file,
              saveIntent,
              requireCurrentAuthToken(),
              expectedSourceSha256,
            );
            return {
              request,
              updatedDocument,
              savedBuffer: null,
              savedHtml: null,
              savedTextBuffer: fileBytes,
              savedText: text,
              savedTextFormat,
            };
          });
        }
        return retryDocumentSave(async () => {
          await api.documents.saveContent(
            documentId,
            text,
            saveIntent,
            requireCurrentAuthToken(),
          );
          return {
            request,
            updatedDocument: null,
            savedBuffer: null,
            savedHtml: null,
            savedTextBuffer: null,
            savedText: null,
            savedTextFormat: null,
          };
        });
      });
      textSaveQueueRef.current = saveTask;
      return saveTask;
    },
    onMutate: (request) => {
      if (
        request.sessionRevision === textSaveSessionRevisionRef.current
        && request.editRevision === textSaveEditRevisionRef.current
      ) setSaveStatus("saving");
    },
    onSuccess: ({ request, updatedDocument, savedBuffer, savedHtml, savedTextBuffer, savedText, savedTextFormat }) => {
      const isCurrentSession = request.sessionRevision === textSaveSessionRevisionRef.current;
      if (updatedDocument) queryClient.setQueryData(["document", request.documentId], updatedDocument);
      if (isCurrentSession) {
        textSavePersistedRevisionRef.current = Math.max(
          textSavePersistedRevisionRef.current,
          request.editRevision,
        );
        if (savedBuffer && savedHtml != null) {
          docxOriginalBufferRef.current = savedBuffer;
          docxBaselineHtmlRef.current = savedHtml;
          if (request.editRevision === textSaveEditRevisionRef.current && editorRef.current) {
            const parsed = new DOMParser().parseFromString(`<div id="docx-saved-root">${savedHtml}</div>`, "text/html");
            const savedRoot = parsed.getElementById("docx-saved-root");
            const savedBlocks = savedRoot ? docxLeafBlocks(savedRoot) : [];
            const editorBlocks = docxLeafBlocks(editorRef.current);
            if (savedBlocks.length === editorBlocks.length) {
              savedBlocks.forEach((savedBlock, index) => {
                const source = savedBlock.getAttribute("data-docx-paragraph-index");
                if (source == null) return;
                editorBlocks[index].setAttribute("data-docx-paragraph-index", source);
                const sourceEditable = savedBlock.getAttribute("data-docx-source-editable");
                if (sourceEditable != null) editorBlocks[index].setAttribute("data-docx-source-editable", sourceEditable);
                editorBlocks[index].removeAttribute("data-docx-insert-after");
              });
            }
          }
        }
        if (savedTextBuffer && savedText != null) {
          textOriginalBufferRef.current = savedTextBuffer;
          textBaselineRef.current = savedText;
          if (savedTextFormat) {
            textFileFormatRef.current = savedTextFormat;
            setTextFileFormat(savedTextFormat);
            if (request.textFormat && !request.textFormat.safeToSave) {
              setLiveEditNotice("Saved as UTF-8 because the original text encoding was not recognized.");
            }
          }
        }
        if (request.editRevision === textSaveEditRevisionRef.current) setSaveStatus("saved");
      }
      invalidateKnowledgeQueries(queryClient);
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", request.documentPath] });
      if (isCurrentSession && showVersions) refetchVersions();
    },
    onError: (error, request) => {
      if (
        request.sessionRevision === textSaveSessionRevisionRef.current
        && request.editRevision === textSaveEditRevisionRef.current
      ) {
        setSaveStatus("unsaved");
      }
      const message = error instanceof Error ? error.message : t("page.blueprint_detail.save_failed");
      showSaveError(request.documentName ? `Failed to save ${request.documentName}: ${message}` : message);
    },
  });
  textSaveMutateRef.current = saveMutation.mutate;

  const spreadsheetSaveMutation = useMutation({
    mutationFn: (request: SpreadsheetSaveRequest) => {
      const requireCurrentAuthToken = () => currentDocumentSaveAuthToken(request.authPrincipalKey);
      const saveIntentPromise = allocateEditorSaveIntent();
      const saveTask = xlsxSaveQueueRef.current.catch(() => undefined).then(async () => {
        const saveIntent = await saveIntentPromise;
        if (!request.canEdit) throw new Error("You do not have edit access to this document");
        const { preserveSpreadsheetFile } = await import("../lib/spreadsheetOoxml");
        const file = await preserveSpreadsheetFile(
          request.originalBuffer,
          request.baselineSheets,
          request.sheets,
          request.documentName,
        );
        const savedBuffer = await file.arrayBuffer();
        const XLSX = await import("xlsx");
        const savedWorkbook = XLSX.read(savedBuffer, {
          type: "array",
          cellFormula: true,
          cellNF: true,
          cellStyles: true,
          cellText: true,
        });
        const savedWorkbookSheets = await spreadsheetSheetsFromFile(XLSX, savedWorkbook, savedBuffer);
        return retryDocumentSave(async () => {
          const updatedDocument = await api.documents.replaceFile(
            request.documentId,
            file,
            saveIntent,
            requireCurrentAuthToken(),
            request.expectedSourceSha256,
          );
          return {
            request,
            updatedDocument,
            savedBuffer,
            savedWorkbookSheets,
          };
        });
      });
      xlsxSaveQueueRef.current = saveTask;
      return saveTask;
    },
    onMutate: (request) => {
      if (
        request.sessionRevision === xlsxSaveSessionRevisionRef.current
        && request.revision === xlsxSaveRevisionRef.current
      ) setSaveStatus("saving");
    },
    onSuccess: ({ request, updatedDocument, savedBuffer, savedWorkbookSheets }) => {
      const isCurrentSession = request.sessionRevision === xlsxSaveSessionRevisionRef.current;
      if (isCurrentSession && request.revision === xlsxSaveRevisionRef.current) {
        const requestedSheets = new Map(request.sheets.map((sheet) => [sheet.name, sheet]));
        const currentSheets = new Map(xlsxSheetsRef.current.map((sheet) => [sheet.name, sheet]));
        const nextSheets = savedWorkbookSheets.map((sheet) => ({
          ...sheet,
          charts: currentSheets.get(sheet.name)?.charts || [],
          editorCharts: structuredClone(requestedSheets.get(sheet.name)?.editorCharts || []),
          structureOperations: [],
        }));
        xlsxOriginalBufferRef.current = savedBuffer;
        xlsxBaselineSheetsRef.current = nextSheets.map(spreadsheetSheetSnapshot);
        xlsxSheetsRef.current = nextSheets;
        setXlsxSheets(nextSheets);
        const nextActiveSheet = nextSheets[xlsxActiveSheetIndexRef.current] || nextSheets[0];
        if (nextActiveSheet) {
          const nextContent = serializeSpreadsheetContent(
            nextActiveSheet.data,
            nextActiveSheet.editorCharts || [],
            true,
            nextActiveSheet.styles as SheetStyleMap,
          );
          setSheetData(nextActiveSheet.data);
          setSheetCharts(nextActiveSheet.editorCharts || []);
          setSheetStyles(nextActiveSheet.styles as SheetStyleMap);
          contentRef.current = nextContent;
          setContent(nextContent);
        }
        setSaveStatus("saved");
      }
      queryClient.setQueryData(["document", request.documentId], updatedDocument);
      invalidateKnowledgeQueries(queryClient);
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", request.documentPath] });
      if (isCurrentSession && showVersions) refetchVersions();
    },
    onError: (error, request) => {
      if (
        request.sessionRevision === xlsxSaveSessionRevisionRef.current
        && request.revision === xlsxSaveRevisionRef.current
      ) setSaveStatus("unsaved");
      const message = error instanceof Error ? error.message : t("page.blueprint_detail.save_failed");
      showSaveError(`Failed to save ${request.documentName}: ${message}`);
    },
  });
  spreadsheetSaveMutateRef.current = spreadsheetSaveMutation.mutate;

  const presentationSaveMutation = useMutation({
    mutationFn: (request: PresentationSaveRequest) => {
      const requireCurrentAuthToken = () => currentDocumentSaveAuthToken(request.authPrincipalKey);
      const saveIntentPromise = allocateEditorSaveIntent();
      const saveTask = pptxSaveQueueRef.current.catch(() => undefined).then(async () => {
        const saveIntent = await saveIntentPromise;
        if (!request.canEdit) throw new Error("You do not have edit access to this document");
        if (!request.originalBuffer || !request.baselineSlides) {
          throw new Error("The original PowerPoint package is unavailable; reload the presentation before saving.");
        }
        const { preservePresentationFileWithSnapshot } = await import("../lib/presentationOoxmlPatch");
        const result = await preservePresentationFileWithSnapshot(
          request.originalBuffer,
          request.baselineSlides,
          request.slides,
          request.documentName,
        );
        const file = result.file;
        const savedSlides = result.slides as PptxSlide[];
        const savedBuffer = await file.arrayBuffer();
        const expectedSourceSha256 = request.expectedSourceSha256;
        if (!expectedSourceSha256) {
          throw new Error("The original PowerPoint source version is unavailable; reload the presentation before saving.");
        }
        return retryDocumentSave(async () => {
          const updatedDocument = await api.documents.replaceFile(
            request.documentId,
            file,
            saveIntent,
            requireCurrentAuthToken(),
            expectedSourceSha256,
          );
          return {
            request,
            updatedDocument,
            savedBuffer,
            savedSlides,
          };
        });
      });
      pptxSaveQueueRef.current = saveTask;
      return saveTask;
    },
    onMutate: (request) => {
      if (
        request.sessionRevision === pptxSaveSessionRevisionRef.current
        && request.revision === pptxSaveRevisionRef.current
      ) setSaveStatus("saving");
    },
    onSuccess: ({ request, updatedDocument, savedBuffer, savedSlides }) => {
      const isCurrentSession = request.sessionRevision === pptxSaveSessionRevisionRef.current;
      if (isCurrentSession) {
        if (savedBuffer && savedSlides) {
          pptxOriginalBufferRef.current = savedBuffer;
          pptxBaselineSlidesRef.current = structuredClone(savedSlides);
        } else {
          pptxOriginalBufferRef.current = null;
          pptxBaselineSlidesRef.current = null;
        }
        if (request.revision === pptxSaveRevisionRef.current) {
          if (savedSlides) {
            const nextSlides = mergePresentationSavedIdentity(
              pptxSlidesRef.current,
              savedSlides,
              request.slides,
            );
            pptxSlidesRef.current = nextSlides;
            setPptxSlides(nextSlides);
          }
          setSaveStatus("saved");
        }
      }
      queryClient.setQueryData(["document", request.documentId], updatedDocument);
      invalidateKnowledgeQueries(queryClient);
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", request.documentPath] });
      if (isCurrentSession && showVersions) refetchVersions();
    },
    onError: (error, request) => {
      if (
        request.sessionRevision === pptxSaveSessionRevisionRef.current
        && request.revision === pptxSaveRevisionRef.current
      ) setSaveStatus("unsaved");
      const message = error instanceof Error ? error.message : t("page.blueprint_detail.save_failed");
      showSaveError(`Failed to save ${request.documentName}: ${message}`);
    },
  });
  presentationSaveMutateRef.current = presentationSaveMutation.mutate;

  const buildTextSaveRequest = useCallback((text: string, editRevision: number): TextSaveRequest | null => {
    if (!docId) return null;
    return {
      text,
      documentId: docId,
      documentName: doc?.name || null,
      documentMimeType: doc?.mime_type || null,
      documentPath: doc?.fs_path || null,
      canEdit: canEditCurrentDoc,
      authPrincipalKey: authPrincipalKey(getAuthToken()),
      sessionRevision: textSaveSessionRevisionRef.current,
      editRevision,
      docxOriginalBuffer: docxOriginalBufferRef.current,
      docxBaselineHtml: docxBaselineHtmlRef.current,
      docxExpectedSourceSha256: docxExpectedSourceSha256Ref.current,
      isDocx,
      textOriginalBuffer: textOriginalBufferRef.current,
      textBaseline: textBaselineRef.current,
      textFormat: textFileFormatRef.current,
      textFormatLoad: textFormatLoadRef.current?.documentId === docId
        && textFormatLoadRef.current.sessionRevision === textSaveSessionRevisionRef.current
        ? textFormatLoadRef.current.promise
        : null,
    };
  }, [canEditCurrentDoc, doc, docId, isDocx]);

  const buildSpreadsheetSaveRequest = useCallback((
    sheets: SpreadsheetSheetSnapshot[],
    revision: number,
  ): SpreadsheetSaveRequest | null => {
    if (
      !docId
      || !doc?.name
      || !xlsxOriginalBufferRef.current
      || !xlsxExpectedSourceSha256Ref.current
      || !xlsxBaselineSheetsRef.current
    ) return null;
    return {
      sheets: structuredClone(sheets),
      documentId: docId,
      documentName: doc.name,
      documentPath: doc.fs_path || null,
      canEdit: canEditCurrentDoc,
      authPrincipalKey: authPrincipalKey(getAuthToken()),
      sessionRevision: xlsxSaveSessionRevisionRef.current,
      revision,
      originalBuffer: xlsxOriginalBufferRef.current,
      expectedSourceSha256: xlsxExpectedSourceSha256Ref.current,
      baselineSheets: xlsxBaselineSheetsRef.current,
    };
  }, [canEditCurrentDoc, doc?.fs_path, doc?.name, docId]);

  const buildPresentationSaveRequest = useCallback((
    slides: PptxSlide[],
    revision: number,
  ): PresentationSaveRequest | null => {
    if (!docId || !doc?.name) return null;
    return {
      slides: structuredClone(slides),
      documentId: docId,
      documentName: doc.name,
      documentPath: doc.fs_path || null,
      canEdit: canEditCurrentDoc,
      authPrincipalKey: authPrincipalKey(getAuthToken()),
      sessionRevision: pptxSaveSessionRevisionRef.current,
      revision,
      originalBuffer: pptxOriginalBufferRef.current,
      expectedSourceSha256: pptxExpectedSourceSha256Ref.current,
      baselineSlides: pptxBaselineSlidesRef.current,
    };
  }, [canEditCurrentDoc, doc?.fs_path, doc?.name, docId]);

  const ensureNoLiveEditPreview = useCallback(() => {
    if (!liveEditPreviewRef.current) return true;
    setLiveEditNotice(t("page.doc_editor.ai_edit_save_blocked"));
    return false;
  }, []);

  useEffect(() => {
    if (liveEditNavigationBlocker.state === "blocked") {
      setLiveEditNotice(t("page.doc_editor.ai_edit_save_blocked"));
    }
  }, [liveEditNavigationBlocker.state]);

  const flushSave = useCallback(async (text = content) => {
    if (!ensureNoLiveEditPreview()) return false;
    if (!docId) return true;
    const hadPendingTimer = !!saveTimerRef.current;
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    if (doc?.name && isXlsx && xlsxOriginalBufferRef.current && xlsxBaselineSheetsRef.current) {
      const pendingSave = pendingSpreadsheetSaveRef.current;
      pendingSpreadsheetSaveRef.current = null;
      if (!pendingSave && saveStatus === "saved" && !spreadsheetSaveMutation.isPending) return true;
      const revision = xlsxSaveRevisionRef.current;
      const sheets = xlsxSheetsRef.current.map(spreadsheetSheetSnapshot);
      const request = pendingSave || buildSpreadsheetSaveRequest(sheets, revision);
      if (!request) return false;
      try {
        await spreadsheetSaveMutation.mutateAsync(request);
        return true;
      } catch {
        return false;
      }
    }
    if (doc?.name && isPptx) {
      const pendingSave = pendingPresentationSaveRef.current;
      pendingPresentationSaveRef.current = null;
      if (!pendingSave && saveStatus === "saved" && !presentationSaveMutation.isPending) return true;
      const revision = pptxSaveRevisionRef.current;
      const request = pendingSave || buildPresentationSaveRequest(pptxSlidesRef.current, revision);
      if (!request) return false;
      try {
        await presentationSaveMutation.mutateAsync(request);
        return true;
      } catch {
        return false;
      }
    }
    if (!hadPendingTimer && saveStatus === "saved" && !saveMutation.isPending) return true;
    pendingTextSaveRef.current = null;
    const request = buildTextSaveRequest(text, textSaveEditRevisionRef.current);
    if (!request) return false;
    try {
      await saveMutation.mutateAsync(request);
      return true;
    } catch {
      return false;
    }
  }, [
    buildPresentationSaveRequest,
    buildSpreadsheetSaveRequest,
    buildTextSaveRequest,
    content,
    doc?.name,
    docId,
    ensureNoLiveEditPreview,
    isPptx,
    isXlsx,
    presentationSaveMutation,
    saveMutation,
    saveStatus,
    spreadsheetSaveMutation,
  ]);

  // Derived
  const mode: EditorMode = doc
    ? detectMode(doc)
    : "richtext";
  const docName = doc?.name || "Document";
  const activeXlsxSheet = xlsxSheets[xlsxActiveSheetIndex];
  const xlsxSheetTabs = xlsxSheets.flatMap((sheet, index) => (
    !sheet.hidden && sheet.name !== SPREADSHEET_CHARTS_SHEET ? [{ index, name: sheet.name }] : []
  ));

  const { data: wikiLinkData } = useQuery({
    queryKey: ["fs-wiki-links", doc?.fs_path],
    queryFn: () => api.fs.wikiLinks(doc!.fs_path!),
    enabled: Boolean(doc?.fs_path && mode === "markdown"),
  });

  useEffect(() => { contentRef.current = content; }, [content]);
  useEffect(() => { sheetDataRef.current = sheetData; }, [sheetData]);
  useEffect(() => { sheetChartsRef.current = sheetCharts; }, [sheetCharts]);
  useEffect(() => { sheetStylesRef.current = sheetStyles; }, [sheetStyles]);
  useEffect(() => { xlsxSheetsRef.current = xlsxSheets; }, [xlsxSheets]);
  useEffect(() => { pptxSlidesRef.current = pptxSlides; }, [pptxSlides]);

  // Load text-like files from their original bytes so the editor and viewer
  // agree on BOM/UTF-16 decoding. Saving can then restore the same encoding
  // and newline convention instead of silently normalizing every file.
  useEffect(() => {
    if (!docId || !doc || !preservesTextFileBytes(doc)) return;
    let cancelled = false;
    const sessionRevision = textSaveSessionRevisionRef.current;
    const loadPromise = (async (): Promise<TextFileSaveSnapshot | null> => {
      try {
        const blob = await api.documents.previewBlob(docId);
        const buffer = await blob.arrayBuffer();
        const decoded = decodeTextFile(buffer);
        const snapshot = {
          originalBuffer: buffer.slice(0),
          baseline: decoded.text,
          format: decoded.format,
        };
        if (!cancelled && sessionRevision === textSaveSessionRevisionRef.current) {
          textOriginalBufferRef.current = snapshot.originalBuffer;
          textBaselineRef.current = snapshot.baseline;
          textFileFormatRef.current = snapshot.format;
          setTextFileFormat(snapshot.format);
          const keepLocalTextChange = textSaveEditRevisionRef.current !== textSavePersistedRevisionRef.current
            && contentRef.current !== decoded.text;
          if (!keepLocalTextChange) {
            if (mode === "text" && contentRef.current !== decoded.text) resetPlainTextEditorHistory();
            setContent(decoded.text);
            contentRef.current = decoded.text;
          }
          if (isCsv) {
            const csvFormat = detectDelimitedTextFormat(decoded.text);
            csvFormatRef.current = csvFormat;
            if (!keepLocalTextChange) {
              setSheetData(parseDelimitedText(decoded.text, csvFormat).rows);
              setSheetCharts([]);
              setSheetStyles({});
            }
          }
          if (!decoded.format.safeToSave) {
            setLiveEditNotice("Unknown text encoding detected. The next save will convert this file to UTF-8.");
          }
          if (!keepLocalTextChange) setSaveStatus("saved");
        }
        return snapshot;
      } catch {
        // The content API remains the fallback for metadata-only documents.
        if (!cancelled && sessionRevision === textSaveSessionRevisionRef.current && doc.fs_path) {
          setLiveEditNotice("The original file bytes could not be loaded. Changes will be saved as UTF-8.");
        }
        return null;
      } finally {
        if (!cancelled && sessionRevision === textSaveSessionRevisionRef.current) {
          setTextBytesReadyDocumentId(docId);
        }
      }
    })();
    textFormatLoadRef.current = { documentId: docId, sessionRevision, promise: loadPromise };
    void loadPromise;
    return () => { cancelled = true; };
  }, [doc?.file_type, doc?.fs_path, doc?.mime_type, doc?.name, docId, isCsv, mode, resetPlainTextEditorHistory]);

  // Load text content
  useEffect(() => {
    if (contentData?.content != null) {
      if (textOriginalBufferRef.current) return;
      const spreadsheetPayload = parseSpreadsheetPayload(contentData.content);
      const loadedContent = spreadsheetPayload
        ? serializeSpreadsheetContent(spreadsheetPayload.data, spreadsheetPayload.charts, isXlsx, spreadsheetPayload.styles)
        : contentData.content;
      const contentChanged = contentRef.current !== loadedContent;
      const hasUnsavedLocalChange = textSaveEditRevisionRef.current !== textSavePersistedRevisionRef.current
        && contentChanged;
      if (hasUnsavedLocalChange) return;
      textSavePersistedRevisionRef.current = textSaveEditRevisionRef.current;
      if (mode === "text" && contentChanged) resetPlainTextEditorHistory();
      contentRef.current = loadedContent;
      if (spreadsheetPayload) {
        setContent(loadedContent);
        setSheetData(spreadsheetPayload.data);
        setSheetCharts(spreadsheetPayload.charts);
        setSheetStyles(spreadsheetPayload.styles);
      } else {
        setContent(loadedContent);
        if (isCsv) {
          const csvFormat = detectDelimitedTextFormat(contentData.content);
          csvFormatRef.current = csvFormat;
          setSheetData(parseDelimitedText(contentData.content, csvFormat).rows);
          setSheetCharts([]);
          setSheetStyles({});
        }
      }
    }
  }, [contentData, isCsv, isXlsx, mode, resetPlainTextEditorHistory]);

  // Load DOCX: download blob → Manor's OOXML renderer. The original package remains the
  // save source so unchanged Word structures and resources stay byte-stable.
  useEffect(() => {
    if (!doc || !docId || !isDocx) return;
    let cancelled = false;
    setDocxLoadError(null);
    setDocxLoading(true);
    (async () => {
      try {
        const source = await loadOfficeEditSource(docId, needsLegacyOfficeConversion);
        if (cancelled) return;
        const { buffer: buf, sourceSha256 } = source;
        if (cancelled) return;
        const bytes = new Uint8Array(buf);
        // Real DOCX starts with PK zip signature (0x50 0x4B)
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const rendered = await renderManorDocument(buf);
          if (cancelled) return;
          const sanitizeOptions = { allowDocxEditorAttributes: true, allowDocxLayoutStyles: true };
          const safeRender = sanitizeManorDocumentRender(rendered, sanitizeOptions);
          docxOriginalBufferRef.current = buf.slice(0);
          docxBaselineHtmlRef.current = safeRender.html;
          docxExpectedSourceSha256Ref.current = sourceSha256;
          setDocxRender(safeRender);
          setDocxHtml(safeRender.html);
          setContent(safeRender.html);
        } else {
          throw new Error("The Word file is not a valid OOXML package.");
        }
      } catch (e) {
        if (cancelled) return;
        console.error("Failed to load DOCX for editing:", e);
        docxOriginalBufferRef.current = null;
        docxBaselineHtmlRef.current = null;
        docxExpectedSourceSha256Ref.current = null;
        setDocxRender(null);
        setDocxHtml(null);
        setDocxLoadError(t("page.doc_editor.failed_to_load_document_for_editing"));
      } finally {
        if (!cancelled) setDocxLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [doc, docId, isDocx, needsLegacyOfficeConversion]);

  // Load XLSX: download blob → xlsx → data[][] (or CSV fallback)
  useEffect(() => {
    if (!doc || !docId || !isXlsx) return;
    let cancelled = false;
    setXlsxLoadError(null);
    setXlsxLoading(true);
    (async () => {
      try {
        const source = await loadOfficeEditSource(docId, needsLegacyOfficeConversion);
        if (cancelled) return;
        const { buffer: buf, sourceSha256 } = source;
        if (cancelled) return;
        const XLSX = await import("xlsx");
        if (cancelled) return;
        xlsxNumberFormatterRef.current = (value, numberFormat) => String(XLSX.SSF.format(numberFormat, value));
        const wb = XLSX.read(buf, {
          type: "array",
          cellFormula: true,
          cellNF: true,
          cellStyles: true,
          cellText: true,
        });
        const parsedSheets = await spreadsheetSheetsFromFile(XLSX, wb, buf);
        if (cancelled) return;
        const firstSheetIndex = spreadsheetActiveSheetIndex(
          wb,
          parsedSheets,
          [SPREADSHEET_CHARTS_SHEET],
        );
        const activeSheet = parsedSheets[firstSheetIndex] || parsedSheets[0];
        if (!activeSheet) throw new Error("The workbook has no worksheets.");
        const metadata = readWorkbookEditorMetadata(wb, activeSheet.data);
        const nextSheets = parsedSheets.map((sheet, index) => index === firstSheetIndex
          ? {
              ...sheet,
              styles: { ...sheet.styles, ...metadata.styles },
              editorCharts: metadata.charts,
            }
          : sheet);
        const nextActiveSheet = nextSheets[firstSheetIndex];
        xlsxOriginalBufferRef.current = buf.slice(0);
        xlsxExpectedSourceSha256Ref.current = sourceSha256;
        xlsxBaselineSheetsRef.current = nextSheets.map(spreadsheetSheetSnapshot);
        xlsxSheetsRef.current = nextSheets;
        setXlsxSheets(nextSheets);
        xlsxActiveSheetIndexRef.current = firstSheetIndex;
        setXlsxActiveSheetIndex(firstSheetIndex);
        setSheetData(nextActiveSheet.data);
        setSheetCharts(metadata.charts);
        setSheetStyles(nextActiveSheet.styles as SheetStyleMap);
      } catch (e) {
        if (cancelled) return;
        console.error("Failed to load XLSX for editing:", e);
        xlsxOriginalBufferRef.current = null;
        xlsxExpectedSourceSha256Ref.current = null;
        xlsxBaselineSheetsRef.current = null;
        xlsxSheetsRef.current = [];
        setXlsxSheets([]);
        setXlsxLoadError(t("page.doc_editor.failed_to_load_document_for_editing"));
      } finally {
        if (!cancelled) setXlsxLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [doc, docId, isXlsx, needsLegacyOfficeConversion]);

  // Load PPTX:
  // Read from its package. Server-rendered slides are requested only when
  // unsupported graphic frames need object-level previews (or later for AI capture).
  useEffect(() => {
    if (!docId || !isPptx) return;
    setPptxLoadError(null);
    setPptxLoading(true);
    let cancelled = false;
    pptxRenderRequestRef.current += 1;
    pptxGraphicPreviewRequestRef.current += 1;
    pptxGraphicPreviewAbortRef.current?.abort();
    pptxGraphicPreviewAbortRef.current = null;
    replacePptxServerUrls([]);
    replacePptxGraphicObjectUrls([]);

    (async () => {
      try {
        const source = await loadOfficeEditSource(docId, needsLegacyOfficeConversion);
        if (cancelled) return;
        const { buffer: buf, sourceSha256 } = source;
        if (cancelled) return;
        const bytes = new Uint8Array(buf);
        if (bytes.length >= 2 && bytes[0] === 0x50 && bytes[1] === 0x4B) {
          const parsed = await parsePptxForEditor(buf, { isCancelled: () => cancelled });
          if (!cancelled) {
            const nextSlides = parsed.length > 0 ? parsed : [{ id: genId(), bg: "#ffffff", shapes: [] }];
            pptxOriginalBufferRef.current = buf.slice(0);
            pptxExpectedSourceSha256Ref.current = sourceSha256;
            pptxBaselineSlidesRef.current = JSON.parse(JSON.stringify(nextSlides)) as PptxSlide[];
            setPptxSlides(nextSlides);
            setContent(slidesToText(nextSlides));
            setSaveStatus("saved");
            if (nextSlides.some((slide) => slide.shapes.some((shape) => shape.type === "graphic"))) {
              void refreshPptxGraphicPreviews(docId, nextSlides).then((previews) => {
                if (cancelled || !previews) return;
                setPptxSlides((currentSlides) => currentSlides.map((slide) => ({
                  ...slide,
                  shapes: slide.shapes.map((shape) => {
                    const graphicPreviewUrl = previews.get(shape.id);
                    if (shape.type !== "graphic") return shape;
                    return graphicPreviewUrl
                      ? { ...shape, graphicPreviewUrl, graphicPreviewStatus: undefined }
                      : { ...shape, graphicPreviewStatus: "failed" };
                  }),
                })));
              });
            }
          }
        } else {
          throw new Error("The PowerPoint file is not a valid OOXML package.");
        }
      } catch (e) {
        if (cancelled) return;
        console.error("Failed to load PPTX for editing:", e);
        pptxOriginalBufferRef.current = null;
        pptxExpectedSourceSha256Ref.current = null;
        pptxBaselineSlidesRef.current = null;
        pptxRenderRequestRef.current += 1;
        pptxGraphicPreviewRequestRef.current += 1;
        pptxGraphicPreviewAbortRef.current?.abort();
        pptxGraphicPreviewAbortRef.current = null;
        replacePptxServerUrls([]);
        replacePptxGraphicObjectUrls([]);
        setPptxSlides([]);
        setPptxLoadError(t("page.doc_editor.failed_to_load_document_for_editing"));
      } finally {
        if (!cancelled) setPptxLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      pptxGraphicPreviewRequestRef.current += 1;
      pptxGraphicPreviewAbortRef.current?.abort();
      pptxGraphicPreviewAbortRef.current = null;
    };
  }, [
    docId,
    isPptx,
    needsLegacyOfficeConversion,
    refreshPptxGraphicPreviews,
    replacePptxGraphicObjectUrls,
    replacePptxServerUrls,
  ]);

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
      if (!ensureNoLiveEditPreview()) return;
      if (!canEditCurrentDoc) return;
      const editRevision = textSaveEditRevisionRef.current + 1;
      textSaveEditRevisionRef.current = editRevision;
      const request = buildTextSaveRequest(text, editRevision);
      if (!request) return;
      setSaveStatus("unsaved");
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      pendingTextSaveRef.current = request;
      const timer = setTimeout(() => {
        if (saveTimerRef.current === timer) saveTimerRef.current = null;
        if (pendingTextSaveRef.current === request) pendingTextSaveRef.current = null;
        saveMutation.mutate(request);
      }, 3000);
      saveTimerRef.current = timer;
    },
    [buildTextSaveRequest, canEditCurrentDoc, ensureNoLiveEditPreview, saveMutation],
  );

  const scheduleSpreadsheetSave = useCallback((sheets: SpreadsheetSheetModel[]) => {
    if (!canEditCurrentDoc) return;
    const revision = xlsxSaveRevisionRef.current + 1;
    xlsxSaveRevisionRef.current = revision;
    const snapshots = sheets.map(spreadsheetSheetSnapshot);
    const request = buildSpreadsheetSaveRequest(snapshots, revision);
    if (!request) return;
    setSaveStatus("unsaved");
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    pendingSpreadsheetSaveRef.current = request;
    const timer = setTimeout(() => {
      if (saveTimerRef.current === timer) saveTimerRef.current = null;
      if (pendingSpreadsheetSaveRef.current === request) pendingSpreadsheetSaveRef.current = null;
      spreadsheetSaveMutation.mutate(request);
    }, 1800);
    saveTimerRef.current = timer;
  }, [buildSpreadsheetSaveRequest, canEditCurrentDoc, spreadsheetSaveMutation]);

  const schedulePresentationSave = useCallback((newSlides: PptxSlide[]) => {
    if (!canEditCurrentDoc) return;
    const revision = pptxSaveRevisionRef.current + 1;
    pptxSaveRevisionRef.current = revision;
    const request = buildPresentationSaveRequest(newSlides, revision);
    if (!request) return;
    setSaveStatus("unsaved");
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    pendingPresentationSaveRef.current = request;
    const timer = setTimeout(() => {
      if (saveTimerRef.current === timer) saveTimerRef.current = null;
      if (pendingPresentationSaveRef.current === request) pendingPresentationSaveRef.current = null;
      presentationSaveMutation.mutate(request);
    }, 1800);
    saveTimerRef.current = timer;
  }, [buildPresentationSaveRequest, canEditCurrentDoc, presentationSaveMutation]);

  const handleContentChange = useCallback(
    (text: string) => {
      textLikeEditRevisionRef.current += 1;
      contentRef.current = text;
      setContent(text);
      scheduleSave(text);
    },
    [scheduleSave],
  );

  const commitPlainTextChange = useCallback((
    text: string,
    options: {
      beforeSelection?: PlainTextSelection;
      afterSelection?: PlainTextSelection;
      input?: boolean;
    } = {},
  ) => {
    const previous = contentRef.current;
    if (text === previous) return;
    const beforeGeneration = plainTextGenerationRef.current;
    const afterGeneration = advancePlainTextGeneration();
    const beforeSelection = normalizePlainTextSelection(
      options.beforeSelection || plainTextSelectionRef.current,
      previous.length,
    );
    const afterSelection = normalizePlainTextSelection(
      options.afterSelection || beforeSelection,
      text.length,
    );
    recordPlainTextHistory(
      plainTextHistoryRef.current,
      previous,
      text,
      beforeSelection,
      afterSelection,
      {
        input: options.input,
        beforeGeneration,
        afterGeneration,
      },
    );
    plainTextSelectionRef.current = afterSelection;
    setPlainTextHistoryRevision((revision) => revision + 1);
    contentRef.current = text;
    handleContentChange(text);
  }, [advancePlainTextGeneration, handleContentChange]);

  const updatePlainTextComposition = useCallback((textarea: HTMLTextAreaElement) => {
    const text = textarea.value;
    const selection = plainTextSelectionFrom(textarea);
    plainTextSelectionRef.current = selection;
    if (text === contentRef.current) return;
    contentRef.current = text;
    handleContentChange(text);
  }, [handleContentChange]);

  const finishPlainTextComposition = useCallback((
    textarea: HTMLTextAreaElement,
    expectedComposition?: typeof plainTextCompositionRef.current,
  ) => {
    const composition = plainTextCompositionRef.current;
    if (!composition || (expectedComposition && composition !== expectedComposition)) return;
    plainTextCompositionRef.current = null;
    const text = textarea.value;
    const selection = plainTextSelectionFrom(textarea);
    plainTextSelectionRef.current = selection;
    if (composition.text !== text) {
      const afterGeneration = advancePlainTextGeneration();
      recordPlainTextHistory(
        plainTextHistoryRef.current,
        composition.text,
        text,
        composition.selection,
        selection,
        {
          beforeGeneration: composition.generation,
          afterGeneration,
        },
      );
      setPlainTextHistoryRevision((revision) => revision + 1);
    }
    if (text !== contentRef.current) handleContentChange(text);
  }, [advancePlainTextGeneration, handleContentChange]);

  const endPlainTextComposition = useCallback((textarea: HTMLTextAreaElement) => {
    const composition = plainTextCompositionRef.current;
    if (!composition) return;
    composition.ended = true;
    // The final committed input can follow compositionend in the same browser
    // task. It finalizes synchronously in onChange; this is the no-input fallback.
    queueMicrotask(() => finishPlainTextComposition(textarea, composition));
  }, [finishPlainTextComposition]);

  const projectRootPath = mode === "code" ? codeProjectDirectory(doc?.fs_path) : null;
  const projectRootName = codeProjectName(projectRootPath);
  const handleAuxiliarySaveError = useCallback((path: string, message: string) => {
    showSaveError(t("page.blueprint_detail.save_failed"), `${path}: ${message}`);
  }, [showSaveError]);
  const codeWorkspace = useCodeProjectWorkspace({
    enabled: mode === "code",
    mainPath: doc?.fs_path,
    mainName: docName,
    mainContent: content,
    mainSaveStatus: saveStatus,
    onMainContentChange: handleContentChange,
    onSaveMain: flushSave,
    onAuxiliarySaveError: handleAuxiliarySaveError,
  });
  const saveCodeWorkspaceForPublish = useCallback(async () => {
    if (!ensureNoLiveEditPreview()) return false;
    return codeWorkspace.saveAll();
  }, [codeWorkspace.saveAll, ensureNoLiveEditPreview]);
  const activeCodeTab = codeWorkspace.activeTab;
  const activeCodePath = activeCodeTab?.path || doc?.fs_path || "";
  const activeCodePathRef = useRef(activeCodePath);
  activeCodePathRef.current = activeCodePath;
  const editorLiveTurnTargetPathRef = useRef<string | undefined>(undefined);
  const activeCodeName = activeCodeTab?.name || docName;
  const activeCodeContent = activeCodeTab?.content ?? content;
  const activeCodeStatus = activeCodeTab?.status || saveStatus;
  const activeCodeIsMain = !activeCodeTab || activeCodeTab.isMain;
  const activeCodeReference = activeCodeIsMain && doc
    ? { name: activeCodeName, file_type: doc.file_type, mime_type: doc.mime_type }
    : activeCodeName;
  const stableHtmlPreviewContentRef = useRef(content);
  const stableHtmlPreviewTextOverridesRef = useRef(codeWorkspace.previewTextOverrides);
  const holdHtmlPreview = liveEditPreview?.mode === "code"
    && liveEditPreview.phase !== "settle"
    && !liveEditPreview.modelStreaming;
  const htmlPreviewContent = holdHtmlPreview
    ? stableHtmlPreviewContentRef.current
    : content;
  const htmlPreviewTextOverrides = holdHtmlPreview
    ? stableHtmlPreviewTextOverridesRef.current
    : codeWorkspace.previewTextOverrides;
  useEffect(() => {
    if (holdHtmlPreview) return;
    stableHtmlPreviewContentRef.current = content;
    stableHtmlPreviewTextOverridesRef.current = codeWorkspace.previewTextOverrides;
  }, [codeWorkspace.previewTextOverrides, content, holdHtmlPreview]);
  const activeCodeLineCount = useMemo(
    () => activeCodeContent.split("\n").length,
    [activeCodeContent],
  );
  const codeLanguage = codeLanguageForFile(activeCodeReference);
  const codeLanguageName = codeLanguageLabel(activeCodeReference);
  const codeOpenPaths = useMemo(
    () => new Set(codeWorkspace.tabs.map((tab) => tab.path)),
    [codeWorkspace.tabs],
  );
  const codeFileStatuses = useMemo(
    () => Object.fromEntries(codeWorkspace.tabs.map((tab) => [tab.path, tab.status])),
    [codeWorkspace.tabs],
  );
  const {
    previewUrl: codePreviewUrl,
    isResolvingAssets: isResolvingCodePreviewAssets,
    isPreparingPreview: isPreparingCodePreview,
    failedAssetCount: codePreviewFailedAssetCount,
    previewError: codePreviewError,
    retryPreview: retryCodePreview,
  } = useHtmlPreviewDocument(
    htmlPreviewContent,
    isHtmlFile(docName) ? doc?.fs_path : null,
    isHtmlFile(docName),
    htmlPreviewTextOverrides,
  );

  useEffect(() => {
    setCodeCursor({ line: 1, column: 1 });
  }, [activeCodePath]);

  useEffect(() => {
    const textarea = codeRef.current;
    if (!textarea) return;
    syncCodeScrollLayers(textarea.scrollTop, textarea.scrollLeft);
  }, [activeCodeContent, activeCodePath, syncCodeScrollLayers]);

  const handleCodeKeyDown = useCallback((event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const selected = activeCodeContent.slice(start, end);
    let nextContent = activeCodeContent;
    let nextStart = start;
    let nextEnd = end;

    if (start !== end) {
      const blockStart = activeCodeContent.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      const nextBreak = activeCodeContent.indexOf("\n", end);
      const blockEnd = nextBreak === -1 ? activeCodeContent.length : nextBreak;
      const block = activeCodeContent.slice(blockStart, blockEnd);
      const lines = block.split("\n");
      if (event.shiftKey) {
        let removedBeforeSelection = 0;
        let removedTotal = 0;
        let offset = 0;
        const replacement = lines.map((line) => {
          const removeCount = line.startsWith("  ") ? 2 : line.startsWith("\t") || line.startsWith(" ") ? 1 : 0;
          if (blockStart + offset < start) removedBeforeSelection += removeCount;
          removedTotal += removeCount;
          offset += line.length + 1;
          return line.slice(removeCount);
        }).join("\n");
        nextContent = activeCodeContent.slice(0, blockStart) + replacement + activeCodeContent.slice(blockEnd);
        nextStart = Math.max(blockStart, start - removedBeforeSelection);
        nextEnd = Math.max(nextStart, end - removedTotal);
      } else {
        const replacement = lines.map((line) => `  ${line}`).join("\n");
        nextContent = activeCodeContent.slice(0, blockStart) + replacement + activeCodeContent.slice(blockEnd);
        nextStart = start + 2;
        nextEnd = end + lines.length * 2;
      }
    } else if (event.shiftKey) {
      const lineStart = activeCodeContent.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      const beforeCursor = activeCodeContent.slice(lineStart, start);
      const removeCount = beforeCursor.endsWith("  ") ? 2 : beforeCursor.endsWith("\t") || beforeCursor.endsWith(" ") ? 1 : 0;
      if (removeCount > 0) {
        nextContent = activeCodeContent.slice(0, start - removeCount) + activeCodeContent.slice(end);
        nextStart = start - removeCount;
        nextEnd = nextStart;
      }
    } else {
      nextContent = activeCodeContent.slice(0, start) + "  " + selected + activeCodeContent.slice(end);
      nextStart = start + 2;
      nextEnd = nextStart;
    }

    if (nextContent === activeCodeContent) return;
    codeWorkspace.changeActiveContent(nextContent);
    requestAnimationFrame(() => {
      textarea.selectionStart = nextStart;
      textarea.selectionEnd = nextEnd;
    });
  }, [activeCodeContent, codeWorkspace.changeActiveContent]);

  const refreshCodeCursor = useCallback((textarea: HTMLTextAreaElement) => {
    const position = textarea.selectionStart;
    const beforeCursor = textarea.value.slice(0, position);
    const lines = beforeCursor.split("\n");
    setCodeCursor({ line: lines.length, column: (lines.at(-1)?.length || 0) + 1 });
  }, []);

  const handleCodeTabKeyDown = useCallback((event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    let targetIndex = index;
    if (event.key === "ArrowLeft") targetIndex = Math.max(0, index - 1);
    else if (event.key === "ArrowRight") targetIndex = Math.min(codeWorkspace.tabs.length - 1, index + 1);
    else if (event.key === "Home") targetIndex = 0;
    else if (event.key === "End") targetIndex = codeWorkspace.tabs.length - 1;
    else return;
    event.preventDefault();
    const target = codeWorkspace.tabs[targetIndex];
    if (!target) return;
    codeWorkspace.setActivePath(target.path);
    const tabButtons = event.currentTarget.closest('[role="tablist"]')?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    requestAnimationFrame(() => tabButtons?.[targetIndex]?.focus());
  }, [codeWorkspace.setActivePath, codeWorkspace.tabs]);

  const handleManualSave = useCallback(async () => {
    if (!ensureNoLiveEditPreview()) return;
    const saved = mode === "code" ? await codeWorkspace.saveActive() : await flushSave(content);
    if (saved) {
      showSaveSuccess(t("page.blueprint_detail.saved"));
      return;
    }
    showSaveError(t("page.blueprint_detail.save_failed"));
  }, [codeWorkspace.saveActive, content, ensureNoLiveEditPreview, flushSave, mode, showSaveError, showSaveSuccess]);

  const goBackFromEditor = useCallback(async () => {
    if (!ensureNoLiveEditPreview()) return;
    const saved = mode === "code" ? await codeWorkspace.saveAll() : await flushSave(content);
    if (saved) navigate(knowledgeReturnTo || "/knowledge");
  }, [codeWorkspace.saveAll, content, ensureNoLiveEditPreview, flushSave, knowledgeReturnTo, mode, navigate]);

  const handleSlidesChange = useCallback(
    (newSlides: PptxSlide[]) => {
      if (liveEditPreviewRef.current) {
        setPptxSlides(structuredClone(pptxSlidesRef.current));
        setLiveEditNotice(t("page.doc_editor.ai_edit_save_blocked"));
        return;
      }
      pptxSlidesRef.current = newSlides;
      setPptxSlides(newSlides);
      const text = slidesToText(newSlides);
      contentRef.current = text;
      setContent(text);
      schedulePresentationSave(newSlides);
    },
    [schedulePresentationSave],
  );

  const handlePresentationLiveEditTargetChange = useCallback((next: PresentationLiveEditTarget) => {
    setPptxLiveEditTarget((current) => (
      current.activeSlideIndex === next.activeSlideIndex && current.selectedShapeId === next.selectedShapeId
        ? current
        : next
    ));
  }, []);

  const handleSelectXlsxSheet = useCallback((sheetIndex: number) => {
    const currentSheets = xlsxSheetsRef.current;
    const sheet = currentSheets[sheetIndex];
    if (!sheet || sheet.hidden || sheet.name === SPREADSHEET_CHARTS_SHEET) return;
    const formulaSheets = currentSheets.map((candidate) => ({
      name: candidate.name,
      data: candidate.data,
    }));
    const formulaEvaluationState = createSpreadsheetFormulaEvaluationState();
    const displayData = sheet.data.map((row, rowIndex) => row.map((value, columnIndex) => (
      typeof value === "string" && value.trim().startsWith("=")
        ? getSpreadsheetDisplayValue(
            sheet.data,
            rowIndex,
            columnIndex,
            sheet.displayData[rowIndex]?.[columnIndex],
            {
              context: { sheets: formulaSheets, currentSheetName: sheet.name },
              numberFormat: sheet.numberFormats[sheetStyleKey(rowIndex, columnIndex)],
              formatNumber: xlsxNumberFormatterRef.current || undefined,
              evaluationState: formulaEvaluationState,
            },
          )
        : sheet.displayData[rowIndex]?.[columnIndex] ?? String(value ?? "")
    )));
    const refreshedSheet = { ...sheet, displayData };
    const nextSheets = currentSheets.map((candidate, index) => (
      index === sheetIndex ? refreshedSheet : candidate
    ));
    xlsxSheetsRef.current = nextSheets;
    setXlsxSheets(nextSheets);
    xlsxActiveSheetIndexRef.current = sheetIndex;
    setXlsxActiveSheetIndex(sheetIndex);
    setSheetData(refreshedSheet.data);
    setSheetCharts(refreshedSheet.editorCharts || []);
    setSheetStyles(refreshedSheet.styles as SheetStyleMap);
    setContent(serializeSpreadsheetContent(
      refreshedSheet.data,
      refreshedSheet.editorCharts || [],
      true,
      refreshedSheet.styles as SheetStyleMap,
    ));
  }, []);

  const handleAddXlsxSheet = useCallback(() => {
    if (!isXlsx) return;
    const currentSheets = xlsxSheetsRef.current;
    const name = nextSpreadsheetSheetName(currentSheets.map((sheet) => sheet.name));
    const newSheet = createEmptySpreadsheetSheet(name);
    const nextSheets = [...currentSheets, newSheet];
    const nextSheetIndex = nextSheets.length - 1;
    const nextContent = serializeSpreadsheetContent(newSheet.data, [], true, {});

    textLikeEditRevisionRef.current += 1;
    xlsxSheetsRef.current = nextSheets;
    setXlsxSheets(nextSheets);
    xlsxActiveSheetIndexRef.current = nextSheetIndex;
    setXlsxActiveSheetIndex(nextSheetIndex);
    setSheetData(newSheet.data);
    setSheetCharts([]);
    setSheetStyles({});
    contentRef.current = nextContent;
    setContent(nextContent);
    scheduleSpreadsheetSave(nextSheets);
  }, [isXlsx, scheduleSpreadsheetSave]);

  const handleRenameXlsxSheet = useCallback((sheetIndex: number, nextName: string) => {
    if (!isXlsx) return false;
    const currentSheets = xlsxSheetsRef.current;
    const sheet = currentSheets[sheetIndex];
    const name = nextName.trim();
    if (!sheet || sheet.hidden || sheet.name === SPREADSHEET_CHARTS_SHEET) return false;
    if (
      !isValidSpreadsheetWorksheetName(name)
      || currentSheets.some((candidate, index) => (
        index !== sheetIndex && candidate.name.trim().toLocaleLowerCase() === name.toLocaleLowerCase()
      ))
    ) {
      showSaveError(t("page.doc_editor.invalid_sheet_name"));
      return false;
    }
    if (name === sheet.name) return true;
    const nextSheets = currentSheets.map((candidate, index) => (
      index === sheetIndex ? { ...candidate, name } : candidate
    ));
    textLikeEditRevisionRef.current += 1;
    xlsxSheetsRef.current = nextSheets;
    setXlsxSheets(nextSheets);
    if (sheetIndex === xlsxActiveSheetIndexRef.current) {
      const renamedSheet = nextSheets[sheetIndex];
      const nextContent = serializeSpreadsheetContent(
        renamedSheet.data,
        renamedSheet.editorCharts || [],
        true,
        renamedSheet.styles as SheetStyleMap,
      );
      contentRef.current = nextContent;
      setContent(nextContent);
    }
    scheduleSpreadsheetSave(nextSheets);
    return true;
  }, [isXlsx, scheduleSpreadsheetSave, showSaveError, t]);

  // Spreadsheet change → preserve the original workbook or save the lightweight CSV model.
  const handleSheetChange = useCallback(
    (
      data: any[][],
      charts: SheetChartConfig[],
      styles: SheetStyleMap,
      structureOperation?: SpreadsheetStructureOperation,
      persist = true,
    ) => {
      // AI Edit can read the next operation before React flushes the state
      // effects below. Keep the imperative snapshot in the same commit so a
      // follow-up checkpoint always includes the last streamed cell change.
      sheetDataRef.current = data;
      sheetChartsRef.current = charts;
      sheetStylesRef.current = styles;
      setSheetData(data);
      setSheetCharts(charts);
      setSheetStyles(styles);
      const content = isXlsx
        ? serializeSpreadsheetContent(data, charts, true, styles)
        : serializeDelimitedText(data, csvFormatRef.current);
      textLikeEditRevisionRef.current += 1;
      contentRef.current = content;
      setContent(content);
      if (isXlsx && xlsxSheetsRef.current.length > 0) {
        const previousSheets = xlsxSheetsRef.current;
        const sheetsWithEdit = previousSheets.map((sheet, index) => (
          index === xlsxActiveSheetIndex
            ? {
                ...sheet,
                data: structuredClone(data),
                styles: structuredClone(styles),
                editorCharts: structuredClone(charts),
                structureOperations: [
                  ...(sheet.structureOperations || []),
                  ...(structureOperation ? [structureOperation] : []),
                ],
              }
            : sheet
        ));
        const formulaSheets = sheetsWithEdit.map((sheet) => ({
          name: sheet.name,
          data: sheet.data,
        }));
        const formulaEvaluationState = createSpreadsheetFormulaEvaluationState();
        const nextSheets = sheetsWithEdit.map((sheet, sheetIndex) => {
          if (sheetIndex !== xlsxActiveSheetIndex) return sheet;
          const previousSheet = previousSheets[sheetIndex];
          const displayData = sheet.data.map((row, rowIndex) => row.map((value, columnIndex) => {
            const sourceDisplay = previousSheet.displayData[rowIndex]?.[columnIndex];
            if (typeof value === "string" && value.trim().startsWith("=")) {
              return getSpreadsheetDisplayValue(
                sheet.data,
                rowIndex,
                columnIndex,
                sourceDisplay,
                {
                  context: { sheets: formulaSheets, currentSheetName: sheet.name },
                  numberFormat: sheet.numberFormats[sheetStyleKey(rowIndex, columnIndex)],
                  formatNumber: xlsxNumberFormatterRef.current || undefined,
                  evaluationState: formulaEvaluationState,
                },
              );
            }
            return Object.is(previousSheet.data[rowIndex]?.[columnIndex] ?? "", value ?? "")
              ? sourceDisplay ?? String(value ?? "")
              : String(value ?? "");
          }));
          return { ...sheet, displayData };
        });
        xlsxSheetsRef.current = nextSheets;
        setXlsxSheets(nextSheets);
        if (persist) scheduleSpreadsheetSave(nextSheets);
        return;
      }
      if (persist) scheduleSave(content);
    },
    [isXlsx, scheduleSave, scheduleSpreadsheetSave, xlsxActiveSheetIndex],
  );

  const previewEditorLiveSheetFrame = useCallback((rows: unknown[][]) => {
    const data = rows as any[][];
    sheetDataRef.current = data;
    setSheetData(data);
  }, []);

  // Manual save (Ctrl+S)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (!ensureNoLiveEditPreview()) return;
        if (mode === "code") void codeWorkspace.saveActive();
        else void flushSave(content);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [codeWorkspace.saveActive, content, ensureNoLiveEditPreview, flushSave, mode]);

  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (
        saveStatus === "saved"
        && !saveMutation.isPending
        && !spreadsheetSaveMutation.isPending
        && !presentationSaveMutation.isPending
        && !codeWorkspace.hasPendingWrites
        && !liveEditPreview
      ) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [codeWorkspace.hasPendingWrites, liveEditPreview, presentationSaveMutation.isPending, saveMutation.isPending, saveStatus, spreadsheetSaveMutation.isPending]);

  useEffect(() => () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    const pendingTextSave = pendingTextSaveRef.current;
    const pendingSpreadsheetSave = pendingSpreadsheetSaveRef.current;
    const pendingPresentationSave = pendingPresentationSaveRef.current;
    pendingTextSaveRef.current = null;
    pendingSpreadsheetSaveRef.current = null;
    pendingPresentationSaveRef.current = null;
    if (pendingTextSave) textSaveMutateRef.current(pendingTextSave);
    if (pendingSpreadsheetSave) spreadsheetSaveMutateRef.current(pendingSpreadsheetSave);
    if (pendingPresentationSave) presentationSaveMutateRef.current(pendingPresentationSave);
  }, []);

  // Rich text: sync contentEditable -> state
  const handleRichTextInput = useCallback(() => {
    if (editorRef.current) {
      const html = isDocx
        ? serializeManorDocumentHtml(editorRef.current)
        : editorRef.current.innerHTML;
      textLikeEditRevisionRef.current += 1;
      contentRef.current = html;
      setContent(html);
      scheduleSave(html);
      if (isDocx && docxRender) {
        if (docxPaginationFrameRef.current != null) cancelAnimationFrame(docxPaginationFrameRef.current);
        docxPaginationFrameRef.current = requestAnimationFrame(() => {
          docxPaginationFrameRef.current = null;
          if (editorRef.current) paginateManorDocument(editorRef.current, docxRender);
        });
      }
    }
  }, [docxRender, isDocx, scheduleSave]);

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
        editorRef.current.innerHTML = sanitizeDocumentHtml(docxHtml, {
          allowDocxEditorAttributes: true,
          allowDocxLayoutStyles: true,
        });
        if (docxRender) {
          if (docxPaginationFrameRef.current != null) cancelAnimationFrame(docxPaginationFrameRef.current);
          docxPaginationFrameRef.current = requestAnimationFrame(() => {
            docxPaginationFrameRef.current = null;
            if (editorRef.current) paginateManorDocument(editorRef.current, docxRender);
          });
        }
      } else if (contentData?.content != null) {
        editorRef.current.innerHTML = sanitizeDocumentHtml(contentData.content);
      }
    }
  }, [mode, contentData, docxRender, isDocx, docxHtml]);

  useEffect(() => {
    if (!isDocx || !docxRender) return undefined;
    const repaginate = () => {
      if (editorRef.current) paginateManorDocument(editorRef.current, docxRender);
    };
    window.addEventListener("resize", repaginate);
    void document.fonts?.ready.then(repaginate);
    return () => window.removeEventListener("resize", repaginate);
  }, [docxRender, isDocx]);

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
    setDocumentMediaInsertOpen(true);
  }, []);

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

  const insertDocumentBreak = useCallback((lineBreak: boolean) => {
    const editor = editorRef.current;
    const selection = window.getSelection();
    if (!editor || !selection || selection.rangeCount === 0) return false;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return false;

    const startElement = range.startContainer instanceof Element
      ? range.startContainer
      : range.startContainer.parentElement;
    const block = startElement?.closest<HTMLElement>(DOCX_PARAGRAPH_BLOCK_SELECTOR);
    if (!block || !editor.contains(block)) return false;

    if (!range.collapsed) {
      const endElement = range.endContainer instanceof Element
        ? range.endContainer
        : range.endContainer.parentElement;
      if (endElement?.closest(DOCX_PARAGRAPH_BLOCK_SELECTOR) !== block) return false;
      range.deleteContents();
    }

    const sourceIndex = block.getAttribute("data-docx-paragraph-index")
      || block.getAttribute("data-docx-insert-after");
    if (sourceIndex == null) return false;

    if (lineBreak) {
      const breakNode = document.createElement("br");
      breakNode.setAttribute("data-docx-line-break", "true");
      range.insertNode(breakNode);
      range.setStartAfter(breakNode);
      range.collapse(true);
    } else {
      const tailRange = document.createRange();
      tailRange.setStart(range.startContainer, range.startOffset);
      tailRange.setEnd(block, block.childNodes.length);
      const tail = tailRange.extractContents();
      const newBlock = block.cloneNode(false) as HTMLElement;
      newBlock.removeAttribute("data-docx-paragraph-index");
      newBlock.setAttribute("data-docx-insert-after", sourceIndex);
      newBlock.appendChild(tail);
      if (!newBlock.textContent && !newBlock.querySelector("br")) {
        const placeholder = document.createElement("br");
        placeholder.setAttribute("data-docx-placeholder", "true");
        newBlock.appendChild(placeholder);
      }
      if (!block.textContent && !block.querySelector("br")) {
        const placeholder = document.createElement("br");
        placeholder.setAttribute("data-docx-placeholder", "true");
        block.appendChild(placeholder);
      }
      block.after(newBlock);
      range.selectNodeContents(newBlock);
      range.collapse(true);
    }

    selection.removeAllRanges();
    selection.addRange(range);
    handleRichTextInput();
    saveRichSelection();
    return true;
  }, [handleRichTextInput, saveRichSelection]);

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

  const handleRichTextBeforeInput = useCallback((event: React.FormEvent<HTMLDivElement>) => {
    if (!isDocx) return;
    const inputType = (event.nativeEvent as InputEvent).inputType || "";
    if (inputType === "insertParagraph" || inputType === "insertLineBreak") {
      if (insertDocumentBreak(inputType === "insertLineBreak")) event.preventDefault();
    }
  }, [insertDocumentBreak, isDocx]);

  const insertRichTextTransfer = useCallback((transfer: DataTransfer) => {
    const plainText = transfer.getData("text/plain");
    const html = transfer.getData("text/html");
    if (html) {
      document.execCommand("insertHTML", false, sanitizeDocumentHtml(html));
      return true;
    }
    if (!plainText) return false;
    document.execCommand("insertText", false, plainText);
    return true;
  }, []);

  const handleRichTextPaste = useCallback((event: React.ClipboardEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (!canEditCurrentDoc) return;
    if (insertRichTextTransfer(event.clipboardData)) focusRichEditorAfterChange();
  }, [canEditCurrentDoc, focusRichEditorAfterChange, insertRichTextTransfer]);

  const handleRichTextDragStart = useCallback(() => {
    richTextInternalDragRef.current = canEditCurrentDoc;
  }, [canEditCurrentDoc]);

  const handleRichTextDragEnd = useCallback(() => {
    richTextInternalDragRef.current = false;
  }, []);

  const handleRichTextDrop = useCallback((event: React.DragEvent<HTMLDivElement>) => {
    if (richTextInternalDragRef.current) {
      richTextInternalDragRef.current = false;
      requestAnimationFrame(() => focusRichEditorAfterChange());
      return;
    }
    event.preventDefault();
    if (!canEditCurrentDoc) return;
    const editor = event.currentTarget;
    const range = typeof document.caretRangeFromPoint === "function"
      ? document.caretRangeFromPoint(event.clientX, event.clientY)
      : null;
    const caret = !range && typeof document.caretPositionFromPoint === "function"
      ? document.caretPositionFromPoint(event.clientX, event.clientY)
      : null;
    const dropRange = range || (caret ? (() => {
      const next = document.createRange();
      next.setStart(caret.offsetNode, caret.offset);
      next.collapse(true);
      return next;
    })() : null);
    editor.focus();
    if (dropRange && editor.contains(dropRange.commonAncestorContainer)) {
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(dropRange);
    }
    if (insertRichTextTransfer(event.dataTransfer)) focusRichEditorAfterChange();
  }, [canEditCurrentDoc, focusRichEditorAfterChange, insertRichTextTransfer]);

  const applyTextareaEdit = useCallback((textarea: HTMLTextAreaElement, next: string, selectionStart: number, selectionEnd = selectionStart) => {
    handleContentChange(next);
    requestAnimationFrame(() => {
      textarea.selectionStart = selectionStart;
      textarea.selectionEnd = selectionEnd;
      textarea.focus();
    });
  }, [handleContentChange]);

  const applyPlainTextTextareaEdit = useCallback((textarea: HTMLTextAreaElement, next: string, selectionStart: number, selectionEnd = selectionStart) => {
    const beforeSelection = plainTextSelectionFrom(textarea);
    const afterSelection = normalizePlainTextSelection(
      { start: selectionStart, end: selectionEnd, direction: "none" },
      next.length,
    );
    commitPlainTextChange(next, { beforeSelection, afterSelection });
    requestAnimationFrame(() => {
      restorePlainTextSelection(textarea, afterSelection);
      plainTextSelectionRef.current = afterSelection;
      textarea.focus();
    });
  }, [commitPlainTextChange]);

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
    setDocumentMediaInsertOpen(true);
  }, []);

  const handleDocumentMediaInsert = useCallback(async (asset: InsertableMediaAsset) => {
    if (mode === "richtext") {
      const escapedName = asset.name.replace(/[<>&"']/g, "");
      const mediaBlob = asset.kind === "image" || !isDocx
        ? await api.documents.downloadBlob(asset.document.id)
        : null;
      if (asset.kind === "image") {
        const dataUrl = await blobToDataUrl(mediaBlob!);
        execCmd("insertHTML", `<figure class="doc-editor-media"><img src="${dataUrl}" alt="${escapedName}"/><figcaption>${escapedName}</figcaption></figure><p><br></p>`);
      } else if (isDocx) {
        const viewerUrl = `${window.location.origin}/viewer/${encodeURIComponent(asset.document.id)}`;
        execCmd("insertHTML", `<figure class="doc-editor-media"><a href="${viewerUrl}">&#9654; ${escapedName}</a><figcaption>${escapedName}</figcaption></figure><p><br></p>`);
      } else {
        const dataUrl = await blobToDataUrl(mediaBlob!);
        execCmd("insertHTML", `<figure class="doc-editor-media"><video src="${dataUrl}" controls playsinline preload="metadata"></video><figcaption>${escapedName}</figcaption></figure><p><br></p>`);
      }
      return;
    }

    if (mode === "markdown") {
      const alt = asset.name.replace(/[\[\]]/g, "");
      if (asset.kind === "image") {
        const blob = await api.documents.downloadBlob(asset.document.id);
        insertMarkdownBlock(`![${alt}](${await blobToDataUrl(blob)})`);
      } else {
        let posterDataUrl = "";
        try {
          const thumbnailUrl = await api.documents.videoThumbnail(asset.document.id);
          const response = await fetch(thumbnailUrl);
          if (response.ok) posterDataUrl = await blobToDataUrl(await response.blob());
          if (thumbnailUrl.startsWith("blob:")) URL.revokeObjectURL(thumbnailUrl);
        } catch {
          // The video link remains usable without a generated poster.
        }
        const viewerUrl = `/viewer/${encodeURIComponent(asset.document.id)}`;
        insertMarkdownBlock(posterDataUrl
          ? `[![Video: ${alt}](${posterDataUrl})](${viewerUrl})`
          : `[▶ ${alt}](${viewerUrl})`);
      }
      return;
    }

    if (mode === "code") {
      if (!activeCodePath) throw new Error("Open a project file before inserting media.");
      const root = projectRootPath || codeProjectDirectory(activeCodePath) || ".";
      const assetDirectory = root === "." ? "assets" : `${root}/assets`;
      try {
        await api.fs.mkdir(assetDirectory);
      } catch {
        // Existing asset directories are reusable.
      }
      const blob = await api.documents.downloadBlob(asset.document.id);
      const fileName = safeMediaFileName(asset.name, asset.kind === "image" ? "image.png" : "video.mp4");
      const targetPath = `${assetDirectory}/${Date.now()}-${fileName}`;
      await api.fs.upload(targetPath, new File([blob], fileName, { type: blob.type || asset.document.mime_type || undefined }));
      const reference = relativeFsReference(activeCodePath, targetPath);
      const escapedName = asset.name.replace(/[<>&"']/g, "");
      const snippet = asset.kind === "image"
        ? `<img src="${reference}" alt="${escapedName}" loading="lazy" />`
        : `<video src="${reference}" controls playsinline preload="metadata"></video>`;
      const textarea = codeRef.current;
      const start = textarea?.selectionStart ?? activeCodeContent.length;
      const end = textarea?.selectionEnd ?? activeCodeContent.length;
      const prefix = start > 0 && activeCodeContent[start - 1] !== "\n" ? "\n" : "";
      const suffix = end < activeCodeContent.length && activeCodeContent[end] !== "\n" ? "\n" : "";
      const insertion = `${prefix}${snippet}${suffix}`;
      codeWorkspace.changeActiveContent(activeCodeContent.slice(0, start) + insertion + activeCodeContent.slice(end));
      requestAnimationFrame(() => {
        if (!textarea) return;
        const position = start + insertion.length;
        textarea.selectionStart = position;
        textarea.selectionEnd = position;
        textarea.focus();
      });
    }
  }, [
    activeCodeContent,
    activeCodePath,
    codeWorkspace.changeActiveContent,
    execCmd,
    insertMarkdownBlock,
    isDocx,
    mode,
    projectRootPath,
  ]);

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
      applyPlainTextTextareaEdit(textarea, result.next, result.selectionStart, result.selectionEnd ?? result.selectionStart);
    } else {
      commitPlainTextChange(result.next, {
        afterSelection: {
          start: result.selectionStart,
          end: result.selectionEnd ?? result.selectionStart,
        },
      });
    }
  }, [applyPlainTextTextareaEdit, commitPlainTextChange, content]);

  const runPlainTextHistoryCommand = useCallback((command: "undo" | "redo") => {
    const textarea = textRef.current;
    const history = plainTextHistoryRef.current;
    const hasEntry = command === "undo"
      ? canUndoPlainTextHistory(history)
      : canRedoPlainTextHistory(history);
    if (!hasEntry) return;
    const result = command === "undo"
      ? undoPlainTextHistory(history, contentRef.current, plainTextGenerationRef.current)
      : redoPlainTextHistory(history, contentRef.current, plainTextGenerationRef.current);
    setPlainTextHistoryRevision((revision) => revision + 1);
    if (!result) return;
    contentRef.current = result.text;
    plainTextGenerationRef.current = result.generation;
    plainTextSelectionRef.current = result.selection;
    handleContentChange(result.text);
    requestAnimationFrame(() => {
      if (!textarea) return;
      restorePlainTextSelection(textarea, result.selection);
      plainTextSelectionRef.current = result.selection;
      textarea.focus();
    });
  }, [handleContentChange]);

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
    plainTextSelectionRef.current = plainTextSelectionFrom(textarea);
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
        textarea.setSelectionRange(index, index + needle.length, "none");
        plainTextSelectionRef.current = plainTextSelectionFrom(textarea);
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
      applyPlainTextTextareaEdit(textarea, next, index, index + replacement.length);
    } else {
      commitPlainTextChange(next);
    }
  }, [applyPlainTextTextareaEdit, commitPlainTextChange, content]);

  const clearPlainTextDocument = useCallback(() => {
    if (!window.confirm("Clear all content in this text file?")) return;
    const textarea = textRef.current;
    if (textarea) {
      applyPlainTextTextareaEdit(textarea, "", 0);
    } else {
      commitPlainTextChange("");
    }
  }, [applyPlainTextTextareaEdit, commitPlainTextChange]);

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

      if (!options.markdown && plainTextCompositionRef.current) return;

      if (!options.markdown && (e.metaKey || e.ctrlKey) && !e.altKey && key === "z") {
        e.preventDefault();
        runPlainTextHistoryCommand(e.shiftKey ? "redo" : "undo");
        return;
      }

      if (!options.markdown && (e.metaKey || e.ctrlKey) && !e.altKey && key === "y") {
        e.preventDefault();
        runPlainTextHistoryCommand("redo");
        return;
      }

      const applyEdit = options.markdown ? applyTextareaEdit : applyPlainTextTextareaEdit;

      if (options.markdown && (e.metaKey || e.ctrlKey) && (key === "b" || key === "i")) {
        e.preventDefault();
        const marker = key === "b" ? "**" : "*";
        const selected = content.slice(start, end);
        const next = content.slice(0, start) + marker + selected + marker + content.slice(end);
        applyEdit(ta, next, start + marker.length, end + marker.length);
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
            applyEdit(ta, next, Math.max(blockStart, start - removedBeforeSelection), Math.max(blockStart, end - removedTotal));
          } else {
            const indented = lines.map((line) => `  ${line}`).join("\n");
            const next = content.slice(0, blockStart) + indented + content.slice(blockEnd);
            applyEdit(ta, next, start + 2, end + lines.length * 2);
          }
          return;
        }

        if (e.shiftKey) {
          const lineStart = content.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
          const beforeCursor = content.slice(lineStart, start);
          const removeCount = beforeCursor.endsWith("  ") ? 2 : beforeCursor.endsWith("\t") || beforeCursor.endsWith(" ") ? 1 : 0;
          if (removeCount > 0) {
            const next = content.slice(0, start - removeCount) + content.slice(start);
            applyEdit(ta, next, start - removeCount);
          }
        } else {
          const next = content.slice(0, start) + "  " + content.slice(end);
          applyEdit(ta, next, start + 2);
        }
      }
    },
    [applyPlainTextTextareaEdit, applyTextareaEdit, content, insertMarkdownLink, runPlainTextHistoryCommand],
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
    if (targetMode === "code") return activeCodeIsMain ? codeRef.current : null;
    return null;
  }, [activeCodeIsMain, mode]);

  const buildCurrentCommentAnchor = useCallback((): CommentAnchor | null => {
    if (mode === "markdown" && commentSelectionSurfaceRef.current === "markdown-preview") {
      const previewAnchor = surfaceSelectionAnchor(markdownPreviewRef.current, mode, docName);
      if (previewAnchor) return previewAnchor;
      const cachedPreviewAnchor = rangeSelectionAnchor(markdownPreviewRef.current, markdownPreviewSelectionRef.current, mode, docName);
      if (cachedPreviewAnchor) return cachedPreviewAnchor;
    }

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

  const refreshEditorCommentAnchor = useCallback(() => {
    commentSelectionSurfaceRef.current = "editor";
    markdownPreviewSelectionRef.current = null;
    refreshCommentAnchor();
  }, [refreshCommentAnchor]);

  const refreshPlainTextSelection = useCallback((event: React.SyntheticEvent<HTMLTextAreaElement>) => {
    plainTextSelectionRef.current = plainTextSelectionFrom(event.currentTarget);
    refreshEditorCommentAnchor();
  }, [refreshEditorCommentAnchor]);

  const refreshMarkdownPreviewCommentAnchor = useCallback(() => {
    const selection = window.getSelection();
    let previewRange: Range | null = null;
    let previewText = "";
    try {
      if (selection && selection.rangeCount > 0 && !selection.isCollapsed) {
        const range = selection.getRangeAt(0);
        if (markdownPreviewRef.current?.contains(range.commonAncestorContainer)) {
          previewRange = range.cloneRange();
          previewText = selection.toString().trim();
        }
      }
    } catch {
      previewRange = null;
      previewText = "";
    }

    commentSelectionSurfaceRef.current = "markdown-preview";
    markdownPreviewSelectionRef.current = previewRange;
    const previewAnchor = rangeSelectionAnchor(markdownPreviewRef.current, previewRange, mode, docName);
    setCommentAnchor(previewAnchor);

    if (!previewRange || !previewText) return;
    requestAnimationFrame(() => {
      try {
        const nextSelection = window.getSelection();
        if (!nextSelection || nextSelection.toString().trim()) return;
        if (!markdownPreviewRef.current?.contains(previewRange.commonAncestorContainer)) return;
        nextSelection.removeAllRanges();
        nextSelection.addRange(previewRange);
      } catch {
        // Preview selections are best-effort UI affordances; stale ranges should not break editing.
      }
    });
  }, [docName, mode]);

  const preserveCommentSelection = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    // Keep the active textarea/contentEditable/preview selection intact until
    // buildCurrentCommentAnchor has captured the exact user-selected text.
    event.preventDefault();
    refreshCommentAnchor();
    const cachedPreviewRange = markdownPreviewSelectionRef.current;
    if (!cachedPreviewRange || commentSelectionSurfaceRef.current !== "markdown-preview") return;
    requestAnimationFrame(() => {
      try {
        const selection = window.getSelection();
        if (!selection || !markdownPreviewRef.current?.contains(cachedPreviewRange.commonAncestorContainer)) return;
        selection.removeAllRanges();
        selection.addRange(cachedPreviewRange);
      } catch {
        // Selection restore is visual only; comments still keep the cached quote.
      }
    });
  }, [refreshCommentAnchor]);

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
    error: { label: t("page.blueprint_detail.save_failed"), type: "red" },
  };
  const displayedSaveStatus = mode === "code" ? activeCodeStatus : saveStatus;
  const statusInfo = statusConfig[displayedSaveStatus];
  const visibleLiveEditNotice = liveEditNotice && liveEditNotice !== statusInfo.label ? liveEditNotice : null;
  const modeLabel = isDocx
    ? t("page.doc_editor.mode_word_document")
    : isPptx
      ? t("page.doc_editor.mode_presentation")
      : t(`page.doc_editor.mode_${mode}`);
  const liveEditPhaseLabel = liveEditPreview?.status === AiEditPreviewStatus.Ready
    ? t(
      liveEditPreview.changeCount === 1
        ? "page.doc_editor.ai_edit_pending_change"
        : "page.doc_editor.ai_edit_pending_changes",
      { count: liveEditPreview.changeCount },
    )
    : liveEditPreview?.phase === "select"
      ? t("page.doc_editor.ai_edit_selecting")
      : liveEditPreview?.phase === "delete"
        ? t("page.doc_editor.ai_edit_deleting")
        : liveEditPreview?.phase === "format"
          ? t("page.doc_editor.ai_edit_formatting")
          : t("page.doc_editor.ai_edit_typing");
  const savedFeedbackScope = mode === "code" ? `${docId || ""}:${activeCodePath}` : docId || "";

  useEffect(() => {
    const previous = previousDisplayedSaveStatusRef.current;
    previousDisplayedSaveStatusRef.current = { scope: savedFeedbackScope, status: displayedSaveStatus };

    if (!previous || previous.scope !== savedFeedbackScope) {
      if (savedFeedbackTimerRef.current) clearTimeout(savedFeedbackTimerRef.current);
      savedFeedbackTimerRef.current = null;
      setSavedFeedbackVisible(false);
      return;
    }

    if (displayedSaveStatus === "saved" && previous.status !== "saved") {
      setSavedFeedbackVisible(true);
      if (savedFeedbackTimerRef.current) clearTimeout(savedFeedbackTimerRef.current);
      savedFeedbackTimerRef.current = setTimeout(() => {
        savedFeedbackTimerRef.current = null;
        setSavedFeedbackVisible(false);
      }, 1600);
      return;
    }

    if (displayedSaveStatus !== "saved") {
      if (savedFeedbackTimerRef.current) clearTimeout(savedFeedbackTimerRef.current);
      savedFeedbackTimerRef.current = null;
      setSavedFeedbackVisible(false);
    }
  }, [displayedSaveStatus, savedFeedbackScope]);

  useEffect(() => () => {
    if (savedFeedbackTimerRef.current) clearTimeout(savedFeedbackTimerRef.current);
  }, []);

  const diagramDoc = useMemo<EditableDiagramDocument | null>(
    () => {
      if (mode !== "diagram") return null;
      try {
        return parseDiagramDocument(content, docName.replace(/\.(diagram\.json|diagram)$/i, ""));
      } catch {
        return null;
      }
    },
    [content, docName, mode],
  );

  const handleDiagramChange = useCallback(
    (nextDiagram: EditableDiagramDocument) => {
      const text = serializeDiagramDocument(nextDiagram);
      textLikeEditRevisionRef.current += 1;
      contentRef.current = text;
      setContent(text);
      scheduleSave(text);
    },
    [scheduleSave],
  );

  const getPresentationLiveEditContent = useCallback(() => buildPresentationLiveEditContent(
    pptxSlidesRef.current,
    {
      documentName: docName,
      activeSlideIndex: pptxLiveEditTarget.activeSlideIndex,
      selectedShapeId: pptxLiveEditTarget.selectedShapeId,
    },
  ), [docName, pptxLiveEditTarget.activeSlideIndex, pptxLiveEditTarget.selectedShapeId]);

  const getPresentationLiveEditAttachmentFiles = useCallback(async () => {
    const slides = pptxSlidesRef.current;
    const shape = presentationLiveEditTargetShape(slides, pptxLiveEditTarget);
    const slideNumber = Math.max(1, Math.min(slides.length, pptxLiveEditTarget.activeSlideIndex + 1));
    let sourceUrl: string | undefined = shape?.imgUrl
      || shape?.graphicPreviewUrl
      || pptxServerUrls[pptxLiveEditTarget.activeSlideIndex];
    if (!sourceUrl && docId) {
      const renderedUrls = await refreshPptxServerUrls(docId);
      sourceUrl = renderedUrls?.[pptxLiveEditTarget.activeSlideIndex];
    }
    if (!sourceUrl) return [];
    const response = await fetch(sourceUrl);
    if (!response.ok) throw new Error(`Unable to capture slide image (${response.status}).`);
    const blob = await response.blob();
    const expectedMime = shape ? presentationImageMime(shape.source?.mediaPart) : (blob.type || "image/png");
    const extension = shape ? mediaExtension(shape.source?.mediaPart) : expectedMime === "image/jpeg" ? ".jpg" : ".png";
    return [new File(
      [blob],
      shape ? `current-slide-${slideNumber}-image${extension}` : `current-slide-${slideNumber}.png`,
      { type: blob.type || expectedMime },
    )];
  }, [docId, pptxLiveEditTarget, pptxServerUrls, refreshPptxServerUrls]);

  const previewPresentationLiveEdit = useCallback((
    nextSlides: PptxSlide[],
    targetId: string,
    meta?: EditorLiveApplyMeta,
  ) => {
    const previousPreview = liveEditPreviewRef.current;
    const previousPresentationPreview = previousPreview?.targetId === targetId
      && previousPreview.mode === "presentation"
      && previousPreview.baselineSlides
      ? previousPreview
      : null;
    const baselineSlides = structuredClone(
      previousPresentationPreview
        ? previousPresentationPreview.baselineSlides!
        : pptxSlidesRef.current,
    );
    const currentSlides = structuredClone(nextSlides);
    const nextPreview: EditorLivePreviewState = {
      baseline: previousPresentationPreview
        ? previousPresentationPreview.baseline
        : slidesToText(baselineSlides),
      current: slidesToText(currentSlides),
      baselineSlides,
      currentSlides,
      mode: "presentation",
      status: AiEditPreviewStatus.Animating,
      phase: "settle",
      changeCount: nextEditorLiveChangeCount(previousPresentationPreview, meta),
      targetId,
      diff: previousPresentationPreview?.diff || meta?.diff || meta?.patch,
      modelStreaming: meta?.streamEvent === AiEditPatchStreamEventKind.Delta,
    };
    pptxSlidesRef.current = currentSlides;
    setPptxSlides(currentSlides);
    contentRef.current = nextPreview.current;
    setContent(nextPreview.current);
    liveEditPreviewRef.current = nextPreview;
    setLiveEditPreview(nextPreview);
    setLiveDiff(null);
    setLiveEditNotice(null);
    return true;
  }, []);

  const applyGeneratedPresentationImage = useCallback(async (imageUrl: string, meta: EditorLiveApplyMeta) => {
    if (!imageUrl || meta.signal?.aborted) return;
    const slides = pptxSlidesRef.current;
    const targetShape = presentationLiveEditTargetShape(slides, pptxLiveEditTarget);
    if (!targetShape?.imgUrl) throw new Error("Select an editable slide image before asking AI to replace it.");
    const expectedMime = presentationImageMime(targetShape.source?.mediaPart);
    const replacement = await presentationReplacementImageDataUrl(imageUrl, expectedMime);
    if (meta.signal?.aborted) return;
    const nextSlides = slides.map((slide, slideIndex) => slideIndex !== pptxLiveEditTarget.activeSlideIndex
      ? slide
      : {
        ...slide,
        shapes: slide.shapes.map((shape) => shape.id === targetShape.id
          ? { ...shape, imgUrl: replacement }
          : shape),
      });
    previewPresentationLiveEdit(nextSlides, `${docId || "presentation"}`, meta);
    setLiveEditNotice(t("page.doc_editor.pptx_ai_image_applied"));
  }, [docId, pptxLiveEditTarget, previewPresentationLiveEdit]);

  const applyNativePresentationFilePatch = useCallback(async (
    result: EditorNativeFilePatchResult,
    meta: EditorLiveApplyMeta,
  ) => {
    if (!docId || !doc?.fs_path || meta.signal?.aborted) return false;
    if (result.document_id && result.document_id !== docId) {
      throw new Error("The native patch result belongs to a different Knowledge document.");
    }
    if (result.path !== doc.fs_path) {
      throw new Error("The native patch result does not match the active presentation path.");
    }

    const blob = await api.documents.previewBlob(docId, {
      cache: false,
      force: true,
      signal: meta.signal,
    });
    if (meta.signal?.aborted) return false;
    const buffer = await blob.arrayBuffer();
    if (meta.signal?.aborted) return false;
    const downloadedSha256 = await sha256Hex(buffer);
    if (result.source_sha256) {
      if (downloadedSha256 !== result.source_sha256.toLowerCase()) {
        throw new Error("The reloaded presentation does not match the persisted native patch.");
      }
    }
    const parsed = await parsePptxForEditor(buffer, {
      isCancelled: () => Boolean(meta.signal?.aborted),
    });
    if (meta.signal?.aborted) return false;
    const nextSlides = parsed.length > 0
      ? parsed
      : [{ id: genId(), bg: "#ffffff", shapes: [] }];

    pendingPresentationSaveRef.current = null;
    pptxSaveSessionRevisionRef.current += 1;
    pptxSaveRevisionRef.current += 1;
    pptxOriginalBufferRef.current = buffer.slice(0);
    pptxExpectedSourceSha256Ref.current = downloadedSha256;
    pptxBaselineSlidesRef.current = structuredClone(nextSlides);
    pptxSlidesRef.current = nextSlides;
    const nextContent = slidesToText(nextSlides);
    contentRef.current = nextContent;
    setPptxSlides(nextSlides);
    setContent(nextContent);
    setSaveStatus("saved");
    setPptxLoadError(null);
    setPptxLiveEditTarget((current) => {
      const activeSlideIndex = Math.max(0, Math.min(current.activeSlideIndex, nextSlides.length - 1));
      const selectedShapeId = nextSlides[activeSlideIndex]?.shapes.some(
        (shape) => shape.id === current.selectedShapeId,
      ) ? current.selectedShapeId : null;
      return { activeSlideIndex, selectedShapeId };
    });
    liveEditAnimationRevisionRef.current += 1;
    liveEditPreviewRef.current = null;
    editorLiveTurnPreviewCheckpointRef.current = null;
    setLiveEditPreview(null);
    setLiveDiff(null);
    setLiveEditNotice(t("page.doc_editor.ai_edit_updated", { mode: "Presentation" }));
    invalidateKnowledgeQueries(queryClient);
    queryClient.invalidateQueries({ queryKey: ["document", docId] });

    pptxRenderRequestRef.current += 1;
    replacePptxServerUrls([]);
    replacePptxGraphicObjectUrls([]);
    if (nextSlides.some((slide) => slide.shapes.some((shape) => shape.type === "graphic"))) {
      void refreshPptxGraphicPreviews(docId, nextSlides).then((previews) => {
        if (!previews || meta.signal?.aborted) return;
        setPptxSlides((currentSlides) => currentSlides.map((slide) => ({
          ...slide,
          shapes: slide.shapes.map((shape) => {
            const graphicPreviewUrl = previews.get(shape.id);
            if (shape.type !== "graphic") return shape;
            return graphicPreviewUrl
              ? { ...shape, graphicPreviewUrl, graphicPreviewStatus: undefined }
              : { ...shape, graphicPreviewStatus: "failed" };
          }),
        })));
      });
    }
    return true;
  }, [
    doc?.fs_path,
    docId,
    queryClient,
    refreshPptxGraphicPreviews,
    replacePptxGraphicObjectUrls,
    replacePptxServerUrls,
  ]);

  const getEditorLiveContent = useCallback((targetPath?: string) => {
    if (mode === "presentation") return getPresentationLiveEditContent();
    if (mode === "spreadsheet") {
      return isXlsx
        ? serializeSpreadsheetContent(
          normalizeSheetData(sheetDataRef.current),
          sheetChartsRef.current,
          true,
          sheetStylesRef.current,
        )
        : serializeDelimitedText(normalizeSheetData(sheetDataRef.current), csvFormatRef.current);
    }
    if (mode === "richtext" && editorRef.current) {
      return editorRef.current.innerHTML;
    }
    if (mode === "code" && targetPath && targetPath !== doc?.fs_path) {
      return codeWorkspace.fileContent(targetPath) || "";
    }
    return contentRef.current;
  }, [codeWorkspace.fileContent, doc?.fs_path, getPresentationLiveEditContent, isXlsx, mode]);

  const animateEditorLiveText = useCallback(async (
    nextText: string,
    targetId: string,
    targetPath?: string,
    meta?: EditorLiveApplyMeta,
  ) => {
    if (meta?.signal?.aborted) return false;
    const auxiliaryCodeTarget = mode === "code"
      && Boolean(targetPath)
      && targetPath !== doc?.fs_path;
    const before = auxiliaryCodeTarget && targetPath
      ? codeWorkspace.fileContent(targetPath) || ""
      : contentRef.current;
    if (before === nextText) return true;
    const revision = liveEditAnimationRevisionRef.current + 1;
    liveEditAnimationRevisionRef.current = revision;
    const previousPreview = liveEditPreviewRef.current?.targetId === targetId
      ? liveEditPreviewRef.current
      : null;
    const initialPreview: EditorLivePreviewState = {
      baseline: previousPreview?.baseline ?? before,
      current: before,
      mode,
      status: AiEditPreviewStatus.Animating,
      phase: "select",
      changeCount: nextEditorLiveChangeCount(previousPreview, meta),
      targetId,
      targetPath,
      diff: previousPreview?.diff || meta?.diff || meta?.patch,
      modelStreaming: meta?.streamEvent === AiEditPatchStreamEventKind.Delta,
    };
    liveEditPreviewRef.current = initialPreview;
    setLiveEditPreview(initialPreview);
    setLiveDiff(null);
    setLiveEditNotice(null);

    if (mode === "code" && targetPath) {
      codeWorkspace.setActivePath(targetPath);
    }
    if (mode === "markdown") {
      setMarkdownViewMode((current) => current === "preview" ? "split" : current);
    }

    if (meta?.streamEvent === AiEditPatchStreamEventKind.Delta) {
      const deltaFrame = buildEditorLiveTextFrames(before, nextText, 1).at(-1);
      if (auxiliaryCodeTarget && targetPath) {
        if (!codeWorkspace.previewFileContent(targetPath, nextText)) return false;
      } else {
        contentRef.current = nextText;
        setContent(nextText);
      }
      const deltaPreview: EditorLivePreviewState = {
        ...initialPreview,
        current: nextText,
        phase: "type",
      };
      liveEditPreviewRef.current = deltaPreview;
      setLiveEditPreview(deltaPreview);
      if (deltaFrame) {
        window.requestAnimationFrame(() => {
          const textarea = mode === "text"
            ? textRef.current
            : mode === "markdown"
              ? markdownRef.current
              : codeRef.current;
          if (!textarea || meta.signal?.aborted) return;
          textarea.focus({ preventScroll: true });
          textarea.setSelectionRange(
            deltaFrame.selectionStart,
            deltaFrame.selectionEnd,
            "forward",
          );
        });
      }
      return true;
    }

    if (aiEditDisplayMode === AiEditDisplayMode.InstantPreview) {
      const instantPreview: EditorLivePreviewState = {
        ...initialPreview,
        current: nextText,
        status: AiEditPreviewStatus.Animating,
        phase: "settle",
      };
      if (auxiliaryCodeTarget && targetPath) {
        if (!codeWorkspace.previewFileContent(targetPath, nextText)) return false;
      } else {
        contentRef.current = nextText;
        setContent(nextText);
      }
      liveEditPreviewRef.current = instantPreview;
      setLiveEditPreview(instantPreview);
      setLiveDiff(null);
      return true;
    }

    const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const allFrames = buildEditorLiveTextFrames(before, nextText, prefersReducedMotion ? 1 : 36);
    const frames = prefersReducedMotion && allFrames.length > 2
      ? [allFrames[0], allFrames[allFrames.length - 1]]
      : allFrames;

    for (const frame of frames) {
      if (
        revision !== liveEditAnimationRevisionRef.current
        || meta?.signal?.aborted
      ) return false;
      if (auxiliaryCodeTarget && targetPath) {
        if (!codeWorkspace.previewFileContent(targetPath, frame.content)) return false;
      } else {
        contentRef.current = frame.content;
        setContent(frame.content);
      }
      const nextPreview: EditorLivePreviewState | null = liveEditPreviewRef.current
        ? { ...liveEditPreviewRef.current, current: frame.content, phase: frame.phase }
        : null;
      if (nextPreview) {
        liveEditPreviewRef.current = nextPreview;
        setLiveEditPreview(nextPreview);
      }

      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      if (revision !== liveEditAnimationRevisionRef.current || meta?.signal?.aborted) return false;
      const textarea = mode === "text"
        ? textRef.current
        : mode === "markdown"
          ? markdownRef.current
          : codeRef.current;
      if (textarea) {
        textarea.focus({ preventScroll: true });
        textarea.setSelectionRange(frame.selectionStart, frame.selectionEnd, "forward");
      }
      await waitForEditorLiveFrame(prefersReducedMotion ? 0 : frame.delayMs);
    }

    if (revision !== liveEditAnimationRevisionRef.current || meta?.signal?.aborted) return false;
    const streamedPreview: EditorLivePreviewState = {
      ...(liveEditPreviewRef.current || initialPreview),
      current: nextText,
      status: AiEditPreviewStatus.Animating,
      phase: "settle",
    };
    if (auxiliaryCodeTarget && targetPath) {
      if (!codeWorkspace.previewFileContent(targetPath, nextText)) return false;
    } else {
      contentRef.current = nextText;
      setContent(nextText);
    }
    liveEditPreviewRef.current = streamedPreview;
    setLiveEditPreview(streamedPreview);
    return true;
  }, [aiEditDisplayMode, codeWorkspace, doc?.fs_path, mode]);

  const animateEditorLiveSpreadsheet = useCallback(async (
    nextText: string,
    targetId: string,
    targetPath: string | undefined,
    meta: EditorLiveApplyMeta,
  ) => {
    if (meta.signal?.aborted) return false;
    const before = getEditorLiveContent(targetPath);
    if (before === nextText) return true;

    const spreadsheetPayload = parseSpreadsheetPayload(nextText);
    if (
      meta.streamEvent === AiEditPatchStreamEventKind.Delta
      && before.startsWith(EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX)
      && !spreadsheetPayload
    ) return true;
    const nextData = spreadsheetPayload
      ? spreadsheetPayload.data
      : parseCsvText(nextText);
    const nextCharts = spreadsheetPayload
      ? spreadsheetPayload.charts
      : sheetChartsRef.current;
    const nextStyles = spreadsheetPayload
      ? spreadsheetPayload.styles
      : sheetStylesRef.current;
    const beforeData = structuredClone(normalizeSheetData(sheetDataRef.current));
    const revision = liveEditAnimationRevisionRef.current + 1;
    liveEditAnimationRevisionRef.current = revision;
    const previousPreview = liveEditPreviewRef.current?.targetId === targetId
      ? liveEditPreviewRef.current
      : null;
    const initialPreview: EditorLivePreviewState = {
      baseline: previousPreview?.baseline ?? before,
      current: before,
      mode,
      status: AiEditPreviewStatus.Animating,
      phase: "select",
      changeCount: nextEditorLiveChangeCount(previousPreview, meta),
      targetId,
      targetPath,
      diff: previousPreview?.diff || meta.diff || meta.patch,
      modelStreaming: meta.streamEvent === AiEditPatchStreamEventKind.Delta,
    };
    liveEditPreviewRef.current = initialPreview;
    setLiveEditPreview(initialPreview);
    setLiveDiff(null);
    setLiveEditNotice(t("page.doc_editor.ai_edit_updated", { mode: modeLabel }));

    if (meta.streamEvent === AiEditPatchStreamEventKind.Delta) {
      handleSheetChange(nextData, nextCharts, nextStyles, undefined, false);
      const deltaPreview: EditorLivePreviewState = {
        ...initialPreview,
        current: getEditorLiveContent(targetPath),
        phase: "type",
      };
      liveEditPreviewRef.current = deltaPreview;
      setLiveEditPreview(deltaPreview);
      return true;
    }

    const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (aiEditDisplayMode !== AiEditDisplayMode.InstantPreview && !prefersReducedMotion) {
      const animationFormat: DelimitedTextFormat = isXlsx
        ? { delimiter: ",", lineEnding: "\n", finalLineEnding: false }
        : csvFormatRef.current;
      const frames = createEditorLiveDelimitedFrameStream(
        beforeData,
        nextData,
        animationFormat,
      );
      for (const frame of frames) {
        if (
          revision !== liveEditAnimationRevisionRef.current
          || meta.signal?.aborted
        ) return false;
        previewEditorLiveSheetFrame(frame.rows);
        const framePreview: EditorLivePreviewState = {
          ...(liveEditPreviewRef.current || initialPreview),
          phase: frame.phase,
        };
        liveEditPreviewRef.current = framePreview;
        setLiveEditPreview(framePreview);
        await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
        if (revision !== liveEditAnimationRevisionRef.current || meta.signal?.aborted) return false;
        await waitForEditorLiveFrame(frame.delayMs);
      }
    }

    if (revision !== liveEditAnimationRevisionRef.current || meta.signal?.aborted) return false;
    handleSheetChange(nextData, nextCharts, nextStyles, undefined, false);
    const current = getEditorLiveContent(targetPath);
    const streamedPreview: EditorLivePreviewState = {
      ...(liveEditPreviewRef.current || initialPreview),
      current,
      status: AiEditPreviewStatus.Animating,
      phase: "settle",
    };
    liveEditPreviewRef.current = streamedPreview;
    setLiveEditPreview(streamedPreview);
    return true;
  }, [aiEditDisplayMode, getEditorLiveContent, handleSheetChange, isXlsx, mode, modeLabel, previewEditorLiveSheetFrame]);

  const animateEditorLiveRichText = useCallback(async (
    nextText: string,
    targetId: string,
    meta?: EditorLiveApplyMeta,
  ) => {
    if (meta?.signal?.aborted) return false;
    const editor = editorRef.current;
    const before = contentRef.current;
    const sanitizedText = sanitizeDocumentHtml(nextText, {
      allowDocxEditorAttributes: isDocx,
      allowDocxLayoutStyles: isDocx,
    });
    if (before === sanitizedText) return true;

    const revision = liveEditAnimationRevisionRef.current + 1;
    liveEditAnimationRevisionRef.current = revision;
    const previousPreview = liveEditPreviewRef.current?.targetId === targetId
      ? liveEditPreviewRef.current
      : null;
    const initialPreview: EditorLivePreviewState = {
      baseline: previousPreview?.baseline ?? before,
      current: before,
      mode,
      status: AiEditPreviewStatus.Animating,
      phase: "select",
      changeCount: nextEditorLiveChangeCount(previousPreview, meta),
      targetId,
      diff: previousPreview?.diff || meta?.diff || meta?.patch,
      modelStreaming: meta?.streamEvent === AiEditPatchStreamEventKind.Delta,
    };
    liveEditPreviewRef.current = initialPreview;
    setLiveEditPreview(initialPreview);
    setLiveDiff(null);
    setLiveEditNotice(null);

    if (meta?.streamEvent === AiEditPatchStreamEventKind.Delta) {
      if (editor) editor.innerHTML = sanitizedText;
      contentRef.current = sanitizedText;
      setContent(sanitizedText);
      if (isDocx) setDocxHtml(sanitizedText);
      const deltaPreview: EditorLivePreviewState = {
        ...initialPreview,
        current: sanitizedText,
        phase: "type",
      };
      liveEditPreviewRef.current = deltaPreview;
      setLiveEditPreview(deltaPreview);
      return true;
    }

    if (aiEditDisplayMode === AiEditDisplayMode.InstantPreview) {
      if (editor) editor.innerHTML = sanitizedText;
      contentRef.current = sanitizedText;
      setContent(sanitizedText);
      if (isDocx) setDocxHtml(sanitizedText);
      const instantPreview: EditorLivePreviewState = {
        ...initialPreview,
        current: sanitizedText,
        status: AiEditPreviewStatus.Animating,
        phase: "settle",
      };
      liveEditPreviewRef.current = instantPreview;
      setLiveEditPreview(instantPreview);
      return true;
    }

    const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!editor || prefersReducedMotion) {
      if (editor) editor.innerHTML = sanitizedText;
      contentRef.current = sanitizedText;
      setContent(sanitizedText);
      if (isDocx) setDocxHtml(sanitizedText);
    } else {
      const nextContainer = document.createElement("div");
      nextContainer.innerHTML = sanitizedText;
      const beforeText = editor.textContent || "";
      const afterText = nextContainer.textContent || "";
      const frames = buildEditorLiveTextFrames(beforeText, afterText, 28);
      const selectionFrame = frames[0];
      let range = editorTextRange(
        editor,
        selectionFrame.selectionStart,
        selectionFrame.selectionEnd,
      );
      if (!range) {
        range = document.createRange();
        range.selectNodeContents(editor);
      }
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      editor.focus({ preventScroll: true });
      await waitForEditorLiveFrame(220);
      if (revision !== liveEditAnimationRevisionRef.current || meta?.signal?.aborted) return false;

      if (beforeText !== afterText) {
        range.deleteContents();
        const insertionNode = document.createTextNode("");
        range.insertNode(insertionNode);
        range.setStartAfter(insertionNode);
        range.collapse(true);
        selection?.removeAllRanges();
        selection?.addRange(range);
        const typeFrames = frames.filter((frame) => frame.phase === "type");
        const start = selectionFrame.selectionStart;
        if (typeFrames.length === 0) {
          const deletingPreview = { ...initialPreview, phase: "delete" as const };
          liveEditPreviewRef.current = deletingPreview;
          setLiveEditPreview(deletingPreview);
          await waitForEditorLiveFrame(70);
        }
        for (const frame of typeFrames) {
          if (revision !== liveEditAnimationRevisionRef.current || meta?.signal?.aborted) return false;
          insertionNode.data = afterText.slice(start, frame.selectionStart);
          const caret = document.createRange();
          caret.setStart(insertionNode, insertionNode.data.length);
          caret.collapse(true);
          selection?.removeAllRanges();
          selection?.addRange(caret);
          const typingPreview = { ...initialPreview, phase: "type" as const };
          liveEditPreviewRef.current = typingPreview;
          setLiveEditPreview(typingPreview);
          await waitForEditorLiveFrame(frame.delayMs);
        }
      } else {
        const formattingPreview = { ...initialPreview, phase: "format" as const };
        liveEditPreviewRef.current = formattingPreview;
        setLiveEditPreview(formattingPreview);
        await waitForEditorLiveFrame(120);
      }

      if (revision !== liveEditAnimationRevisionRef.current || meta?.signal?.aborted) return false;
      editor.innerHTML = sanitizedText;
      contentRef.current = sanitizedText;
      setContent(sanitizedText);
      if (isDocx) setDocxHtml(sanitizedText);
      const finalCaret = editorTextRange(editor, selectionFrame.selectionStart, selectionFrame.selectionStart);
      if (finalCaret) {
        finalCaret.collapse(true);
        selection?.removeAllRanges();
        selection?.addRange(finalCaret);
      }
    }

    const streamedPreview: EditorLivePreviewState = {
      ...(liveEditPreviewRef.current || initialPreview),
      current: sanitizedText,
      status: AiEditPreviewStatus.Animating,
      phase: "settle",
    };
    liveEditPreviewRef.current = streamedPreview;
    setLiveEditPreview(streamedPreview);
    return true;
  }, [aiEditDisplayMode, isDocx, mode]);

  const discardLiveEditPreview = useCallback(() => {
    if (liveEditCommitCoordinator.isCommitting()) return;
    const preview = liveEditPreviewRef.current;
    if (!preview) return;
    liveEditAnimationRevisionRef.current += 1;
    if (preview.mode === "presentation" && preview.baselineSlides) {
      const baselineSlides = structuredClone(preview.baselineSlides);
      pptxSlidesRef.current = baselineSlides;
      setPptxSlides(baselineSlides);
      const baselineText = slidesToText(baselineSlides);
      contentRef.current = baselineText;
      setContent(baselineText);
    } else if (
      preview.mode === "code"
      && preview.targetPath
      && preview.targetPath !== doc?.fs_path
    ) {
      codeWorkspace.previewFileContent(preview.targetPath, preview.baseline);
      codeWorkspace.setActivePath(preview.targetPath);
    } else if (preview.mode === "spreadsheet") {
      const payload = parseSpreadsheetPayload(preview.baseline);
      const data = payload ? payload.data : parseCsvText(preview.baseline);
      handleSheetChange(
        data,
        payload?.charts || [],
        payload?.styles || {},
        undefined,
        false,
      );
    } else {
      contentRef.current = preview.baseline;
      setContent(preview.baseline);
    }
    if (preview.mode === "richtext") {
      if (editorRef.current) editorRef.current.innerHTML = preview.baseline;
      if (isDocx) setDocxHtml(preview.baseline);
    }
    liveEditPreviewRef.current = null;
    editorLiveTurnPreviewCheckpointRef.current = null;
    setLiveEditPreview(null);
    setLiveDiff(null);
    setLiveEditNotice(t("page.doc_editor.ai_edit_discarded"));
    if (liveEditNavigationBlocker.state === "blocked") {
      liveEditNavigationBlocker.proceed();
    }
  }, [codeWorkspace, doc?.fs_path, handleSheetChange, isDocx, liveEditCommitCoordinator, liveEditNavigationBlocker]);

  const acceptLiveEditPreview = useCallback(async () => {
    const preview = liveEditPreviewRef.current;
    if (
      !preview
      || preview.status !== AiEditPreviewStatus.Ready
      || liveEditCommitCoordinator.isCommitting()
    ) return;
    setLiveEditAccepting(true);
    try {
      await liveEditCommitCoordinator.run(async () => {
        const finalContent = preview.current;
        let persisted = false;

        // Prevent an older debounced save from running after the accepted
        // content and overwriting it. In-flight saves remain ordered by each
        // format's save queue, so the accepted revision is still last.
        if (saveTimerRef.current) {
          clearTimeout(saveTimerRef.current);
          saveTimerRef.current = null;
        }
        pendingTextSaveRef.current = null;
        pendingSpreadsheetSaveRef.current = null;
        pendingPresentationSaveRef.current = null;

        if (preview.mode === "presentation" && preview.currentSlides) {
          const acceptedSlides = structuredClone(preview.currentSlides);
          const revision = pptxSaveRevisionRef.current + 1;
          pptxSaveRevisionRef.current = revision;
          const request = buildPresentationSaveRequest(acceptedSlides, revision);
          if (request) {
            await presentationSaveMutation.mutateAsync(request);
            persisted = true;
          }
        } else if (
          preview.mode === "code"
          && preview.targetPath
          && preview.targetPath !== doc?.fs_path
        ) {
          if (codeWorkspace.commitFileContent(preview.targetPath, finalContent)) {
            persisted = await codeWorkspace.saveActive();
          }
        } else if (preview.mode === "spreadsheet" && isXlsx) {
          const revision = xlsxSaveRevisionRef.current + 1;
          xlsxSaveRevisionRef.current = revision;
          const sheets = xlsxSheetsRef.current.map(spreadsheetSheetSnapshot);
          const request = buildSpreadsheetSaveRequest(sheets, revision);
          if (request) {
            await spreadsheetSaveMutation.mutateAsync(request);
            persisted = true;
          }
        } else {
          const editRevision = textSaveEditRevisionRef.current + 1;
          textSaveEditRevisionRef.current = editRevision;
          const request = buildTextSaveRequest(finalContent, editRevision);
          if (request) {
            await saveMutation.mutateAsync(request);
            persisted = true;
          }
        }

        if (!persisted) {
          setLiveEditNotice(t("page.doc_editor.ai_edit_save_blocked"));
          return;
        }

        if (preview.mode === "text" && preview.baseline !== finalContent) {
          const beforeGeneration = plainTextGenerationRef.current;
          const afterGeneration = advancePlainTextGeneration();
          const beforeSelection = normalizePlainTextSelection(plainTextSelectionRef.current, preview.baseline.length);
          const afterSelection = normalizePlainTextSelection({
            start: finalContent.length,
            end: finalContent.length,
            direction: "none",
          }, finalContent.length);
          recordPlainTextHistory(
            plainTextHistoryRef.current,
            preview.baseline,
            finalContent,
            beforeSelection,
            afterSelection,
            { beforeGeneration, afterGeneration },
          );
          plainTextSelectionRef.current = afterSelection;
          setPlainTextHistoryRevision((revision) => revision + 1);
        }

        liveEditAnimationRevisionRef.current += 1;
        if (preview.mode !== "presentation") {
          textLikeEditRevisionRef.current += 1;
        }
        liveEditPreviewRef.current = null;
        editorLiveTurnPreviewCheckpointRef.current = null;
        setLiveEditPreview(null);
        setLiveDiff(null);
        setLiveEditNotice(t("page.doc_editor.ai_edit_accepted", { mode: modeLabel }));
        if (liveEditNavigationBlocker.state === "blocked") {
          liveEditNavigationBlocker.proceed();
        }
      });
    } catch (error) {
      if (!(error instanceof Error && error.message === "AI Edit is already accepting a preview.")) {
        console.warn("AI Edit accept failed", error);
        setLiveEditNotice(t("page.doc_editor.ai_edit_save_blocked"));
      }
    } finally {
      setLiveEditAccepting(false);
    }
  }, [
    advancePlainTextGeneration,
    buildPresentationSaveRequest,
    buildSpreadsheetSaveRequest,
    buildTextSaveRequest,
    codeWorkspace,
    doc?.fs_path,
    isXlsx,
    liveEditCommitCoordinator,
    liveEditNavigationBlocker,
    modeLabel,
    presentationSaveMutation,
    saveMutation,
    spreadsheetSaveMutation,
  ]);

  const applyEditorLiveContent = useCallback(async (
    nextText: string,
    targetId: string,
    targetPath: string | undefined,
    meta: EditorLiveApplyMeta,
  ) => {
    if (meta.signal?.aborted) return false;
    if (
      meta.streamEvent === AiEditPatchStreamEventKind.Delta
      && aiEditDisplayMode === AiEditDisplayMode.InstantPreview
    ) return false;
    if (mode === "text" || mode === "markdown" || mode === "code") {
      return animateEditorLiveText(nextText, targetId, targetPath, meta);
    }
    if (mode === "richtext") {
      return animateEditorLiveRichText(nextText, targetId, meta);
    }
    if (mode === "spreadsheet") {
      return animateEditorLiveSpreadsheet(nextText, targetId, targetPath, meta);
    }

    setLiveDiff(null);
    setLiveEditNotice(t("page.doc_editor.ai_edit_updated", { mode: modeLabel }));

    if (mode === "presentation") {
      const nextSlides = applyPresentationLiveEditContent(pptxSlidesRef.current, nextText);
      if (!nextSlides) {
        setLiveEditNotice(t("page.doc_editor.pptx_ai_invalid_edit"));
        return false;
      }
      return previewPresentationLiveEdit(nextSlides, targetId, meta);
    }

    const previousPreview = liveEditPreviewRef.current?.targetId === targetId
      ? liveEditPreviewRef.current
      : null;
    const baseline = previousPreview?.baseline ?? getEditorLiveContent(targetPath);
    let current = nextText;

    if (mode === "diagram") {
      try {
        const nextDiagram = parseDiagramDocument(
          nextText,
          docName.replace(/\.(diagram\.json|diagram)$/i, ""),
        );
        const serialized = serializeDiagramDocument(nextDiagram);
        textLikeEditRevisionRef.current += 1;
        contentRef.current = serialized;
        setContent(serialized);
        current = serialized;
      } catch (error) {
        setLiveEditNotice(
          error instanceof Error ? error.message : "AI returned an invalid diagram.",
        );
        return false;
      }
    } else {
      textLikeEditRevisionRef.current += 1;
      contentRef.current = nextText;
      setContent(nextText);
    }

    const nextPreview: EditorLivePreviewState = {
      baseline,
      current,
      mode,
      status: AiEditPreviewStatus.Animating,
      phase: "settle",
      changeCount: nextEditorLiveChangeCount(previousPreview, meta),
      targetId,
      targetPath,
      diff: previousPreview?.diff || meta.diff || meta.patch,
      modelStreaming: meta.streamEvent === AiEditPatchStreamEventKind.Delta,
    };
    liveEditPreviewRef.current = nextPreview;
    setLiveEditPreview(nextPreview);
    return true;
  }, [
    aiEditDisplayMode,
    animateEditorLiveRichText,
    animateEditorLiveSpreadsheet,
    animateEditorLiveText,
    docName,
    getEditorLiveContent,
    handleSheetChange,
    mode,
    modeLabel,
    previewPresentationLiveEdit,
  ]);

  const completeEditorLiveContent = useCallback((
    _nextText: string,
    targetId: string,
    meta: EditorLiveApplyMeta,
  ) => {
    if (meta.signal?.aborted) return false;
    const preview = liveEditPreviewRef.current;
    if (!preview || preview.targetId !== targetId) return false;
    const cumulativeDiff = mergeEditorLivePreviewDiff(preview.diff, meta);
    const readyPreview: EditorLivePreviewState = {
      ...preview,
      status: AiEditPreviewStatus.Ready,
      phase: "settle",
      diff: cumulativeDiff,
      modelStreaming: false,
    };
    liveEditPreviewRef.current = readyPreview;
    setLiveEditPreview(readyPreview);
    setLiveDiff(null);
    return true;
  }, []);

  const beginEditorLiveTurn = useCallback((meta: EditorLiveApplyMeta) => {
    if (meta.signal?.aborted) return false;
    const preview = liveEditPreviewRef.current;
    if (!preview) return true;
    const pendingPreview: EditorLivePreviewState = {
      ...preview,
      status: AiEditPreviewStatus.Animating,
      phase: "select",
      modelStreaming: true,
    };
    liveEditPreviewRef.current = pendingPreview;
    setLiveEditPreview(pendingPreview);
    setLiveDiff(null);
    return true;
  }, []);

  const restoreEditorLiveTurnPreview = useCallback((
    targetId: string,
    meta: EditorLiveApplyMeta,
  ) => {
    if (meta.signal?.aborted) return false;
    const checkpoint = cloneEditorLivePreviewState(
      editorLiveTurnPreviewCheckpointRef.current,
    );
    if (
      !checkpoint
      || checkpoint.targetId !== targetId
      || checkpoint.status !== AiEditPreviewStatus.Ready
    ) return false;

    liveEditAnimationRevisionRef.current += 1;
    if (checkpoint.mode === "presentation" && checkpoint.currentSlides) {
      const restoredSlides = structuredClone(checkpoint.currentSlides);
      pptxSlidesRef.current = restoredSlides;
      setPptxSlides(restoredSlides);
      contentRef.current = checkpoint.current;
      setContent(checkpoint.current);
    } else if (
      checkpoint.mode === "code"
      && checkpoint.targetPath
      && checkpoint.targetPath !== doc?.fs_path
    ) {
      if (!codeWorkspace.previewFileContent(checkpoint.targetPath, checkpoint.current)) {
        return false;
      }
      codeWorkspace.setActivePath(checkpoint.targetPath);
    } else if (checkpoint.mode === "spreadsheet") {
      const payload = parseSpreadsheetPayload(checkpoint.current);
      const data = payload ? payload.data : parseCsvText(checkpoint.current);
      handleSheetChange(
        data,
        payload?.charts || [],
        payload?.styles || {},
        undefined,
        false,
      );
    } else {
      contentRef.current = checkpoint.current;
      setContent(checkpoint.current);
    }
    if (checkpoint.mode === "richtext") {
      if (editorRef.current) editorRef.current.innerHTML = checkpoint.current;
      if (isDocx) setDocxHtml(checkpoint.current);
    }

    liveEditPreviewRef.current = checkpoint;
    setLiveEditPreview(checkpoint);
    setLiveDiff(null);
    setLiveEditNotice(null);
    return true;
  }, [codeWorkspace, doc?.fs_path, handleSheetChange, isDocx]);

  const needsTextByteHydration = Boolean(
    docId
    && doc
    && preservesTextFileBytes(doc),
  );
  const isLoadingContent = contentLoading
    || docxLoading
    || xlsxLoading
    || pptxLoading
    || (needsTextByteHydration && textBytesReadyDocumentId !== docId);

  const openLiveEdit = useCallback(() => {
    if (!docId || !doc || isLoadingContent) return;
    const targetKind = mode === "diagram"
      ? AiEditTargetKind.Diagram
      : mode === "code"
        ? AiEditTargetKind.Project
        : AiEditTargetKind.Document;
    // A project session belongs to the project document, not whichever file
    // happened to be active when the panel opened. read() locks the active
    // file for one streamed turn so switching tabs cannot redirect mid-patch.
    const targetId = docId;
    const readTurnContent = () => {
      const pendingPreview = liveEditPreviewRef.current;
      editorLiveTurnPreviewCheckpointRef.current = cloneEditorLivePreviewState(
        pendingPreview,
      );
      const targetPath = mode === "code"
        ? pendingPreview?.mode === "code" && pendingPreview.targetPath
          ? pendingPreview.targetPath
          : activeCodePathRef.current
        : undefined;
      // Follow-up instructions continue the current unaccepted transaction.
      // After Accept/Discard clears it, the next turn may target another tab.
      if (targetPath && pendingPreview?.mode === "code") {
        codeWorkspace.setActivePath(targetPath);
      }
      editorLiveTurnTargetPathRef.current = targetPath;
      return getEditorLiveContent(targetPath);
    };
    const targetDocumentName = mode === "code" ? activeCodeName : docName;
    const adapter = createEditorLiveAdapter({
      target: { kind: targetKind, id: targetId },
      read: readTurnContent,
      getTurnPreviewState: () => ({
        changeCount: liveEditPreviewRef.current?.changeCount || 0,
      }),
      beginTurn: beginEditorLiveTurn,
      preview: (next, meta) => applyEditorLiveContent(
        next,
        targetId,
        mode === "code" ? editorLiveTurnTargetPathRef.current : undefined,
        meta,
      ),
      complete: (next, meta) => completeEditorLiveContent(next, targetId, meta),
      restore: (_next, meta) => restoreEditorLiveTurnPreview(targetId, meta),
      rollback: discardLiveEditPreview,
      commitCoordinator: liveEditCommitCoordinator,
    });
    const baseDetail = {
      documentId: docId,
      documentName: targetDocumentName,
      fileType: doc?.file_type,
      mimeType: doc?.mime_type,
      editorType: modeLabel,
      sourcePath: mode === "code" ? undefined : doc?.fs_path,
      getTurnMetadata: () => {
        const targetPath = mode === "code" ? editorLiveTurnTargetPathRef.current : undefined;
        const currentDocumentName = targetPath?.split("/").pop() || targetDocumentName;
        return {
          documentName: currentDocumentName,
          fileType: mode === "code" ? codeLanguageForFile(currentDocumentName) : doc?.file_type,
          mimeType: mode === "code" ? undefined : doc?.mime_type,
          editorType: modeLabel,
          sourcePath: mode === "code" ? targetPath : doc?.fs_path,
        };
      },
      adapter,
      previewStatus: liveEditPreviewRef.current?.status || null,
      previewChangeCount: liveEditPreviewRef.current?.changeCount || 0,
      previewAccepting: liveEditAccepting,
      acceptPreview: acceptLiveEditPreview,
      discardPreview: discardLiveEditPreview,
    };
    if (mode === "presentation") {
      const presentationDetail = {
        ...baseDetail,
        fileType: "pptx",
        editorType: "Presentation",
        getAttachmentFiles: getPresentationLiveEditAttachmentFiles,
        applyGeneratedImage: applyGeneratedPresentationImage,
        supportsImageGeneration: true,
        supportsNativeFilePatch: Boolean(doc?.fs_path),
        applyNativeFilePatch: applyNativePresentationFilePatch,
        instruction: t("page.doc_editor.pptx_ai_instruction", { name: docName }),
        emptyDescription: t("page.doc_editor.pptx_ai_description"),
        placeholder: t("page.doc_editor.pptx_ai_placeholder"),
        examples: [
          t("page.doc_editor.pptx_ai_example_replace"),
          t("page.doc_editor.pptx_ai_example_margin"),
          t("page.doc_editor.pptx_ai_example_crop"),
          t("page.doc_editor.pptx_ai_example_text"),
        ],
      };
      openedEditorLiveDetailRef.current = presentationDetail;
      openEditorLiveChat(presentationDetail);
      return;
    }
    openedEditorLiveDetailRef.current = baseDetail;
    openEditorLiveChat(baseDetail);
  }, [
    activeCodeName,
    acceptLiveEditPreview,
    applyEditorLiveContent,
    applyGeneratedPresentationImage,
    applyNativePresentationFilePatch,
    beginEditorLiveTurn,
    completeEditorLiveContent,
    codeWorkspace,
    doc,
    doc?.file_type,
    doc?.mime_type,
    docId,
    docName,
    discardLiveEditPreview,
    getEditorLiveContent,
    getPresentationLiveEditAttachmentFiles,
    liveEditAccepting,
    liveEditCommitCoordinator,
    isLoadingContent,
    mode,
    modeLabel,
    restoreEditorLiveTurnPreview,
  ]);

  useEffect(() => {
    const detail = openedEditorLiveDetailRef.current;
    if (!detail) return;
    updateEditorLiveChat({
      ...detail,
      previewStatus: liveEditPreview?.status || null,
      previewChangeCount: liveEditPreview?.changeCount || 0,
      previewAccepting: liveEditAccepting,
      acceptPreview: acceptLiveEditPreview,
      discardPreview: discardLiveEditPreview,
    });
  }, [
    acceptLiveEditPreview,
    discardLiveEditPreview,
    liveEditAccepting,
    liveEditPreview?.changeCount,
    liveEditPreview?.status,
  ]);
  const officeLoadError = isDocx
    ? docxLoadError
    : isXlsx
      ? xlsxLoadError
      : isPptx
        ? pptxLoadError
        : null;

  const handleCodeSelection = useCallback((event: React.SyntheticEvent<HTMLTextAreaElement>) => {
    refreshCodeCursor(event.currentTarget);
    if (activeCodeIsMain) refreshEditorCommentAnchor();
  }, [activeCodeIsMain, refreshCodeCursor, refreshEditorCommentAnchor]);

  const visibleCodeTabs = codeWorkspace.tabs.length > 0 ? codeWorkspace.tabs : [{
    path: activeCodePath || "__main__",
    name: docName,
    content,
    status: saveStatus,
    loading: false,
    error: null,
    readError: false,
    isMain: true,
  }];

  const codeSourcePane = (
    <section
      className={`doc-editor-code-source${liveEditPreview?.mode === "code" ? " is-ai-editing" : ""}`}
      aria-label="Code editor"
      aria-busy={liveEditPreview?.mode === "code" && liveEditPreview.status === AiEditPreviewStatus.Animating}
    >
      <div className="doc-editor-code-titlebar doc-editor-code-tabs">
        <div className="doc-editor-code-tablist" role="tablist" aria-label="Open code files">
          {visibleCodeTabs.map((tab, index) => {
            const selected = tab.path === activeCodePath || (tab.isMain && !activeCodeTab);
            return (
              <div key={tab.path} className={`doc-editor-code-tab-shell${selected ? " is-active" : ""}`}>
                <button
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  tabIndex={selected ? 0 : -1}
                  className="doc-editor-code-tab"
                  title={tab.path}
                  disabled={liveEditPreview?.mode === "code"}
                  onClick={() => codeWorkspace.setActivePath(tab.path)}
                  onKeyDown={(event) => handleCodeTabKeyDown(event, index)}
                >
                  <IconCode size={13} />
                  <span className="doc-editor-code-tab__name">{tab.name}</span>
                  {tab.status !== "saved" && (
                    <span className={`doc-editor-code-tab__status is-${tab.status}`} aria-label={tab.status} />
                  )}
                </button>
                {!tab.isMain && (
                  <button
                    type="button"
                    className="doc-editor-code-tab__close"
                    aria-label={`Close ${tab.name}`}
                    title={`Close ${tab.name}`}
                    disabled={liveEditPreview?.mode === "code"}
                    onClick={() => void codeWorkspace.closeFile(tab.path)}
                  >
                    <IconClose size={12} />
                  </button>
                )}
              </div>
            );
          })}
        </div>
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
              <IconClose size={13} />
            </button>
          )}
        </div>
      </div>
      <div className={`doc-editor-code-body${liveDiff ? " doc-editor-code-body--diff" : ""}`}>
        {activeCodeTab?.loading ? (
          <div className="doc-editor-code-load-state">Loading {activeCodeName}…</div>
        ) : activeCodeTab?.readError ? (
          <div className="doc-editor-code-load-state is-error">
            <span>{activeCodeTab.error}</span>
            <button
              type="button"
              onClick={() => void codeWorkspace.openFile({ path: activeCodeTab.path, name: activeCodeTab.name })}
            >
              Retry
            </button>
          </div>
        ) : liveDiff ? (
          <EditorLiveInlineDiff content={activeCodeContent} diff={liveDiff} variant="code" />
        ) : (
          <>
            <div className="manor-editor-line-gutter doc-editor-code-gutter">
              <div ref={codeGutterRef} className="doc-editor-code-gutter-lines">
                {Array.from({ length: activeCodeLineCount }, (_, index) => {
                  const line = index + 1;
                  const count = activeCodeIsMain ? commentLineCounts.get(line) || 0 : 0;
                  const firstComment = count ? firstCommentForLine(line) : undefined;
                  return (
                    <div key={line} className="doc-editor-code-gutter-line">
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
                  customStyle={{ minHeight: "100%", overflow: "visible" }}
                  codeTagProps={{
                    style: {
                      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                      tabSize: 2,
                    },
                  }}
                >
                  {activeCodeContent || " "}
                </CodeSyntaxHighlighter>
              </div>
              <textarea
                key={activeCodePath}
                ref={codeRef}
                value={activeCodeContent}
                onChange={(event) => codeWorkspace.changeActiveContent(event.target.value)}
                onScroll={(event) => {
                  const { scrollTop, scrollLeft } = event.currentTarget;
                  syncCodeScrollLayers(scrollTop, scrollLeft);
                }}
                onKeyDown={handleCodeKeyDown}
                onSelect={handleCodeSelection}
                onClick={handleCodeSelection}
                onKeyUp={handleCodeSelection}
                className="manor-editor-codearea doc-editor-ide-textarea"
                placeholder={t("page.doc_editor.start_coding")}
                spellCheck={false}
                readOnly={liveEditPreview?.mode === "code"}
                wrap="off"
              />
            </div>
          </>
        )}
      </div>
    </section>
  );

  const codePreviewPane = showPreview && isRenderableCodeFile(docName) ? (
    <section className="doc-editor-code-preview" aria-label={t("page.doc_editor.preview")}>
      <div className="doc-editor-code-preview-bar">
        <span>{t("page.doc_editor.preview")}</span>
        <span>{renderableCodePreviewLabel(docName)}</span>
      </div>
      <IsolatedHtmlPreviewFrame
        preview={{
          previewUrl: codePreviewUrl,
          isPreparingPreview: isResolvingCodePreviewAssets || isPreparingCodePreview,
          previewError: codePreviewError,
          retryPreview: retryCodePreview,
        }}
        className="doc-editor-code-preview-frame"
        title={t("page.doc_editor.preview")}
        data-missing-preview-assets={codePreviewFailedAssetCount || undefined}
      />
    </section>
  ) : null;

  const codePaneDefinitions: ResizablePaneDefinition[] = [
    ...(projectRootPath ? [{
      id: "files",
      label: "Project files",
      initialSize: 14,
      minSize: 160,
      maxSize: 340,
      className: "doc-editor-project-pane",
      children: (
        <CodeProjectExplorer
          rootPath={projectRootPath}
          rootName={projectRootName}
          activePath={activeCodePath}
          openPaths={codeOpenPaths}
          fileStatuses={codeFileStatuses}
          onOpenFile={(file) => {
            if (liveEditPreviewRef.current?.mode === "code") {
              setLiveEditNotice(t("page.doc_editor.ai_edit_save_blocked"));
              return;
            }
            void codeWorkspace.openFile(file);
          }}
        />
      ),
    }] : []),
    {
      id: "source",
      label: "Code editor",
      initialSize: projectRootPath ? 44 : 54,
      minSize: 360,
      className: "doc-editor-code-pane-shell",
      children: codeSourcePane,
    },
    ...(codePreviewPane ? [{
      id: "preview",
      label: "Preview",
      initialSize: 42,
      minSize: 320,
      className: "doc-editor-preview-pane",
      children: codePreviewPane,
    }] : []),
  ];

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="manor-editor-shell">
      {/* Header bar */}
      <div className="manor-editor-header">
        <button
          onClick={() => void goBackFromEditor()}
          className="btn-manor-ghost"
          title={t("page.doc_editor.back_to_knowledge_base")}
          style={{ width: 36, height: 36, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <IconArrowLeft size={18} />
        </button>

        <div className="manor-editor-header-main">
          <PageHeaderTitle variant="editor" title={docName}>{docName}</PageHeaderTitle>
          <StatusBadge type="gray">{modeLabel}</StatusBadge>
        </div>

        {visibleLiveEditNotice && <StatusBadge type="teal" dot>{visibleLiveEditNotice}</StatusBadge>}

        {(displayedSaveStatus !== "saved" || savedFeedbackVisible) && (
          <StatusBadge type={statusInfo.type} dot>{statusInfo.label}</StatusBadge>
        )}

        {!officeLoadError && (
          <AiEditButton
            onClick={openLiveEdit}
            disabled={!doc || isLoadingContent}
          />
        )}

        {!officeLoadError
          && (["richtext", "markdown", "code"] as EditorMode[]).includes(mode)
          && !(mode === "richtext" && isDocx)
          && (
          <button
            type="button"
            onClick={() => setDocumentMediaInsertOpen(true)}
            className="btn-manor-ghost"
            title={t("component.media_insert.title")}
            aria-label={t("component.media_insert.title")}
            style={{ width: 36, height: 36, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
          >
            <IconImage size={18} />
          </button>
        )}

        {doc && (
          <button
            onMouseDown={preserveCommentSelection}
            onClick={() => {
              refreshCommentAnchor();
              setShowComments((open) => {
                const next = !open;
                if (next) setShowVersions(false);
                return next;
              });
            }}
            className={showComments ? "btn-manor-teal-light" : "btn-manor-ghost"}
            title={t("page.tasks.comments")}
            aria-label={t("page.tasks.comments")}
            aria-pressed={showComments}
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

        {canEditCurrentDoc && mode === "code" && isHtmlFile(docName) && (
          <SitePublishAction doc={doc || null} beforePublish={saveCodeWorkspaceForPublish} />
        )}

        <button
          onClick={() => void handleManualSave()}
          disabled={
            saveMutation.isPending
            || spreadsheetSaveMutation.isPending
            || presentationSaveMutation.isPending
            || activeCodeTab?.loading
            || activeCodeTab?.readError
            || displayedSaveStatus === "saving"
            || Boolean(liveEditPreview)
            || !canEditCurrentDoc
            || Boolean(officeLoadError)
          }
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
          aria-label={t("page.doc_editor.version_history")}
          aria-pressed={showVersions}
          style={{ width: 36, height: 36, padding: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <IconClock size={18} />
        </button>
      </div>

      {/* Toolbar (rich text + docx) */}
      {(mode === "richtext" && !officeLoadError) && (
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
                aria-pressed={markdownViewMode === viewMode}
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
            <ToolbarBtn
              title={t("page.doc_editor.undo_ctrl_plus_z")}
              onClick={() => runPlainTextHistoryCommand("undo")}
              icon={<IconUndo size={15} />}
              disabled={!canUndoPlainTextHistory(plainTextHistoryRef.current)}
            />
            <ToolbarBtn
              title={t("page.doc_editor.redo_ctrl_plus_shift_plus_z")}
              onClick={() => runPlainTextHistoryCommand("redo")}
              icon={<IconRedo size={15} />}
              disabled={!canRedoPlainTextHistory(plainTextHistoryRef.current)}
            />
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
          <div role="status" aria-live="polite" style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
            <LoadingSpinner size={28} />
            <span>{t("status.loading")}</span>
          </div>
        ) : officeLoadError ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <EmptyState
              icon={<IconInfo size={28} />}
              title={officeLoadError}
              description={t("page.doc_editor.office_file_load_error_description")}
            />
          </div>
        ) : mode === "presentation" ? (
          /* Presentation editor (PPTX) */
          <PresentationEditor
            key={docId}
            slides={pptxSlides}
            onLiveEditTargetChange={handlePresentationLiveEditTargetChange}
            onChange={handleSlidesChange}
          />
        ) : mode === "diagram" ? (
          diagramDoc ? (
            /* Editable diagram canvas */
            <DiagramCanvas document={diagramDoc} onChange={handleDiagramChange} />
          ) : (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <EmptyState
                icon={<IconInfo size={28} />}
                title={t("page.doc_editor.diagram_cannot_be_edited")}
                description={t("page.doc_editor.diagram_invalid_description")}
              />
            </div>
          )
        ) : mode === "spreadsheet" ? (
          /* Spreadsheet editor */
          <SpreadsheetEditor
            key={`${docId}:${xlsxActiveSheetIndex}`}
            initialData={sheetData}
            initialCharts={sheetCharts}
            initialStyles={sheetStyles}
            initialDisplayData={activeXlsxSheet?.displayData}
            initialNumberFormats={activeXlsxSheet?.numberFormats}
            numberFormatter={xlsxNumberFormatterRef.current || undefined}
            columnWidths={activeXlsxSheet?.columnWidths}
            rowHeights={activeXlsxSheet?.rowHeights}
            showGridlines={activeXlsxSheet?.showGridlines}
            merges={activeXlsxSheet?.merges}
            nativeCharts={activeXlsxSheet?.charts}
            nativeImages={activeXlsxSheet?.images}
            sheetTabs={xlsxSheetTabs}
            activeSheetIndex={xlsxActiveSheetIndex}
            onSelectSheet={handleSelectXlsxSheet}
            onAddSheet={isXlsx ? handleAddXlsxSheet : undefined}
            onRenameSheet={isXlsx ? handleRenameXlsxSheet : undefined}
            persistCharts={isXlsx}
            onChange={handleSheetChange}
          />
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
                  readOnly={liveEditPreview?.mode === "text"}
                  aria-busy={liveEditPreview?.mode === "text" && liveEditPreview.status === AiEditPreviewStatus.Animating}
                  onBeforeInput={(event) => {
                    plainTextSelectionRef.current = plainTextSelectionFrom(event.currentTarget);
                  }}
                  onCompositionStart={(event) => {
                    if (plainTextCompositionRef.current) {
                      finishPlainTextComposition(event.currentTarget, plainTextCompositionRef.current);
                    }
                    plainTextCompositionRef.current = {
                      text: contentRef.current,
                      selection: plainTextSelectionFrom(event.currentTarget),
                      generation: plainTextGenerationRef.current,
                      ended: false,
                    };
                  }}
                  onCompositionEnd={(event) => endPlainTextComposition(event.currentTarget)}
                  onChange={(event) => {
                    const nativeEvent = event.nativeEvent as InputEvent;
                    const composition = plainTextCompositionRef.current;
                    if (composition) {
                      updatePlainTextComposition(event.currentTarget);
                      if (composition.ended && !nativeEvent.isComposing) {
                        finishPlainTextComposition(event.currentTarget, composition);
                      }
                      return;
                    }
                    commitPlainTextChange(event.currentTarget.value, {
                      beforeSelection: plainTextSelectionRef.current,
                      afterSelection: plainTextSelectionFrom(event.currentTarget),
                      input: PLAIN_TEXT_INCREMENTAL_INPUT_TYPES.has(nativeEvent.inputType || ""),
                    });
                  }}
                  onKeyDown={handlePlainTextKeyDown}
                  onSelect={refreshPlainTextSelection}
                  onClick={refreshPlainTextSelection}
                  onKeyUp={refreshPlainTextSelection}
                  rows={Math.max(30, content.split("\n").length + 6)}
                  spellCheck
                  placeholder="Start typing..."
                  className={`manor-editor-document-surface text-editor-page${liveEditPreview?.mode === "text" ? " is-ai-editing" : ""}`}
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
              contentEditable={canEditCurrentDoc && liveEditPreview?.mode !== "richtext"}
              suppressContentEditableWarning
              role="textbox"
              aria-label={`${docName} content`}
              aria-multiline="true"
              aria-busy={liveEditPreview?.mode === "richtext" && liveEditPreview.status === AiEditPreviewStatus.Animating}
              tabIndex={0}
              onInput={handleRichTextInput}
              onBeforeInput={handleRichTextBeforeInput}
              onKeyDown={handleRichTextKeyDown}
              onPaste={handleRichTextPaste}
              onDragStart={handleRichTextDragStart}
              onDragEnd={handleRichTextDragEnd}
              onDrop={handleRichTextDrop}
              onMouseUp={() => {
                saveRichSelection();
                refreshEditorCommentAnchor();
              }}
              onKeyUp={() => {
                saveRichSelection();
                refreshEditorCommentAnchor();
              }}
              onFocus={() => {
                saveRichSelection();
                refreshEditorCommentAnchor();
              }}
              onBlur={saveRichSelection}
              spellCheck
              data-placeholder="Start typing..."
              className={`manor-editor-document-surface richtext-editor-page ${isDocx && docxRender ? "manor-docx-native" : "docx-preview"}${liveEditPreview?.mode === "richtext" ? " is-ai-editing" : ""}`}
              style={isDocx && docxRender ? {
                "--docx-page-width": `${docxRender.layout.pageWidthPx}px`,
                "--docx-page-height": `${docxRender.layout.pageHeightPx}px`,
                "--docx-margin-top": `${docxRender.layout.marginTopPx}px`,
                "--docx-margin-right": `${docxRender.layout.marginRightPx}px`,
                "--docx-margin-bottom": `${docxRender.layout.marginBottomPx}px`,
                "--docx-margin-left": `${docxRender.layout.marginLeftPx}px`,
                "--docx-header-distance": `${docxRender.layout.headerDistancePx}px`,
                "--docx-footer-distance": `${docxRender.layout.footerDistancePx}px`,
              } as React.CSSProperties : undefined}
            />
          </div>
        ) : mode === "markdown" ? (
          /* Markdown editor */
          <div className={`markdown-editor-layout markdown-editor-layout--${markdownViewMode}`}>
            {markdownViewMode !== "preview" && (
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
                    readOnly={liveEditPreview?.mode === "markdown"}
                    aria-busy={liveEditPreview?.mode === "markdown" && liveEditPreview.status === AiEditPreviewStatus.Animating}
                    onChange={(e) => handleContentChange(e.target.value)}
                    onKeyDown={(e) => handlePlainTextKeyDown(e, { markdown: true })}
                    onSelect={refreshEditorCommentAnchor}
                    onClick={refreshEditorCommentAnchor}
                    onKeyUp={refreshEditorCommentAnchor}
                    className={`manor-editor-codearea markdown-codearea${liveEditPreview?.mode === "markdown" ? " is-ai-editing" : ""}`}
                    placeholder={t("page.doc_editor.write_your_markdown_here")}
                    spellCheck
                  />
                )}
                {renderCommentAnchorRail(markdownStats.lines)}
              </div>
            )}
            {markdownViewMode !== "source" && (
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
                  <div
                    ref={markdownPreviewRef}
                    className="md-preview prose prose-slate prose-sm"
                    onMouseUp={refreshMarkdownPreviewCommentAnchor}
                    onPointerUp={refreshMarkdownPreviewCommentAnchor}
                    onKeyUp={refreshMarkdownPreviewCommentAnchor}
                  >
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
                          return <MarkdownTable>{children}</MarkdownTable>;
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
          <div className="doc-editor-ide-shell">
            <ResizablePaneGroup
              panes={codePaneDefinitions}
              storageKey={`doc-editor-code-panes:${projectRootPath || doc?.fs_path || docId || "default"}`}
              className={`doc-editor-ide-workspace${projectRootPath ? " has-files" : " no-files"}${codePreviewPane ? " has-preview" : " no-preview"}`}
            />
            <div className="doc-editor-ide-statusbar" role="status">
              <span className="doc-editor-ide-statusbar__file" title={activeCodePath}>{activeCodePath}</span>
              <span>{codeLanguageName}</span>
              <span>{textEncodingLabel(textFileFormat)}</span>
              <span>Ln {codeCursor.line}, Col {codeCursor.column}</span>
              {codePreviewPane && (
                <span>
                  {isResolvingCodePreviewAssets
                    ? "Preview loading"
                    : codePreviewFailedAssetCount > 0
                      ? `${codePreviewFailedAssetCount} preview asset${codePreviewFailedAssetCount === 1 ? "" : "s"} missing`
                      : "Preview ready"}
                </span>
              )}
            </div>
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
      <div className={`manor-editor-statusbar${liveEditPreview ? " has-ai-edit-preview" : ""}`}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span>{isDocx || isXlsx || isPptx
            ? formatBytes(doc?.file_size || 0)
            : formatBytes(new Blob([mode === "code" ? activeCodeContent : content]).size)}</span>
          {mode !== "spreadsheet" && mode !== "presentation" && (
            <>
              <span>{wordCount(mode === "richtext" ? content.replace(/<[^>]+>/g, " ") : mode === "code" ? activeCodeContent : content)} {t("page.doc_editor.words")}</span>
              <span>{(mode === "richtext" ? content.replace(/<[^>]+>/g, "") : mode === "code" ? activeCodeContent : content).length} {t("page.doc_editor.chars")}</span>
            </>
          )}
          {(mode === "code" || mode === "text") && <span>{mode === "code" ? activeCodeLineCount : lineCount} {t("page.doc_editor.lines")}</span>}
          {mode === "markdown" && (
            <>
              <span>{markdownStats.lines} lines</span>
              <span>{markdownStats.readingMinutes} min read</span>
            </>
          )}
          {mode === "spreadsheet" && sheetData && <span>{sheetData.length} {t("page.doc_editor.rows_2")}</span>}
          {mode === "presentation" && <span>{pptxSlides.length} {t("page.doc_editor.slides")}</span>}
          {mode === "diagram" && diagramDoc && <span>{diagramDoc.elements.length} objects</span>}
          {["text", "markdown", "code", "diagram"].includes(mode) && <span>{textEncodingLabel(textFileFormat)}</span>}
          {isCsv && <span>{textEncodingLabel(textFileFormat)} · {csvFormatRef.current.delimiter === "\t" ? "TSV" : `delimiter ${csvFormatRef.current.delimiter}`}</span>}
        </div>
        {liveEditPreview && (
          <AiEditPreviewControls
            className={`doc-editor-live-preview-bar is-${liveEditPreview.status}`}
            status={liveEditPreview.status}
            changeCount={liveEditPreview.changeCount}
            accepting={liveEditAccepting}
            workingLabel={liveEditPhaseLabel}
            workingDescription={t("page.doc_editor.ai_edit_editing_document")}
            readyDescription={t("page.doc_editor.ai_edit_review_preview")}
            onReview={liveEditPreview.diff && liveEditPreview.mode !== "richtext"
              ? () => setLiveDiff((current) => current ? null : liveEditPreview.diff || null)
              : undefined}
            reviewLabel={liveDiff
              ? t("page.doc_editor.ai_edit_back_to_editor")
              : t("page.doc_editor.ai_edit_review")}
            onDiscard={discardLiveEditPreview}
            onAccept={acceptLiveEditPreview}
          />
        )}
        <div className="doc-editor-footer-status" style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {doc?.created_at && <span>{t("page.dashboard.created")} {new Date(doc.created_at).toLocaleDateString()}</span>}
        </div>
      </div>

      <MediaInsertDialog
        open={documentMediaInsertOpen}
        onClose={() => setDocumentMediaInsertOpen(false)}
        onInsert={handleDocumentMediaInsert}
      />

      {/* Styles */}
      <style>{`
        .manor-editor-shell {
          position: relative;
        }
        .is-ai-editing,
        .is-ai-editing textarea {
          caret-color: #4f7d75;
        }
        .is-ai-editing::selection,
        .is-ai-editing textarea::selection {
          background: rgba(79, 125, 117, 0.34);
          color: inherit;
        }
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
        .md-preview h1 { font-size: 1.75em; font-weight: 700; margin: 0.8em 0 0.4em; }
        .md-preview h2 { font-size: 1.4em; font-weight: 700; margin: 0.7em 0 0.35em; }
        .md-preview h3 { font-size: 1.15em; font-weight: 600; margin: 0.6em 0 0.3em; }
        .md-preview p { margin: 0.5em 0; }
        .md-preview ul { list-style: disc; padding-left: 1.5em; margin: 0.5em 0; }
        .md-preview ol { list-style: decimal; padding-left: 1.5em; margin: 0.5em 0; }
        .md-preview blockquote { border-left: 3px solid #d6d3d1; padding-left: 1em; color: #78716c; margin: 0.5em 0; }
        .md-preview hr { border: none; border-top: 1px solid #e7e5e4; margin: 1em 0; }
        .md-code-block {
          background: #1c1917;
          border: 1px solid rgba(28, 25, 23, 0.08);
          border-radius: 10px;
          color: #f5f5f4;
          font-size: 0.85em;
          line-height: 1.65;
          margin: 0.5em 0;
          overflow-x: auto;
          padding: 1em;
        }
        .md-code-block code {
          background: transparent;
          color: inherit;
          font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
          text-shadow: none;
          white-space: pre;
        }
        .md-inline-code {
          background: #f5f5f4;
          border: 1px solid rgba(28, 25, 23, 0.06);
          border-radius: 4px;
          color: #1c1917;
          font-size: 0.9em;
          padding: 0.15em 0.4em;
        }
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
          .doc-editor-code-source {
            min-height: 0 !important;
            border-right: 0 !important;
            border-bottom: 1px solid rgba(231,229,228,0.6) !important;
          }
          .doc-editor-code-source textarea {
            min-height: 0 !important;
          }
          .doc-editor-code-preview {
            min-height: 0 !important;
          }
          .doc-editor-code-preview-frame {
            min-height: 0 !important;
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
  label, title, onClick, bold, italic, underline, icon, disabled,
}: {
  label?: string; title: string; onClick: () => void;
  bold?: boolean; italic?: boolean; underline?: boolean; icon?: React.ReactNode; disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseDown={(event) => event.preventDefault()}
      title={title}
      disabled={disabled}
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
