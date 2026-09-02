/**
 * ChatInputFooter — shared chat composer footer.
 *
 * Owns: attached files (local + KB), attach menu, voice input
 * (AI transcription), # autocomplete, the textarea, send/stop.
 *
 * Parent owns input value (controlled) so callers can layer extras
 * (e.g., @-mention) by wrapping onChange / onKeyDown and rendering
 * `topSlot` / `beforeTextarea`.
 */
import {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
  useLayoutEffect,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import { createRoot, type Root } from "react-dom/client";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type IntegrationMCPServer } from "../lib/api";
import { integrationCatalogQueryOptions } from "../lib/integrationCatalog";
import { integrationSetupHref } from "../lib/integrationSetupLinks";
import { useChatVoiceInput } from "../lib/useChatVoiceInput";
import type { ChatVoiceScope } from "../lib/chatVoice";
import { VoiceInputButton, VoiceInputStatus } from "./chat/ChatVoiceControls";
import LiveChatCallButton from "./chat/LiveChatCallButton";
import IntegrationLogo from "./IntegrationLogo";
import NangoConnectButton from "./integrations/NangoConnectButton";
import Modal from "./ui/Modal";
import { useAuthStore } from "../stores/auth";
import UserAvatar from "./ui/UserAvatar";
import { type ChatMessage, useDebounced } from "../lib/chatStream";
import { getSkillDescription } from "../pages/skills/skillTypes";
import { shouldHandleComposerEnter } from "../lib/composerKeyboard";
import type { ManualSkillReference } from "../lib/manualSkillRefs";
import { InlineRowsSkeleton } from "./ui/Skeleton";
import {
  IconChecklist,
  IconClose,
  IconConnection,
  IconDocument,
  IconFlow,
  IconPlus,
  IconSkill,
  IconUpload,
} from "./icons";
import Select from "./ui/Select";
import { t } from "../lib/i18n";
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import { connectorAccessState } from "../lib/integration-usability.mjs";


export interface AttachedItem {
  name: string;
  id?: string;
  fsPath?: string;
  type?: "file" | "knowledge";
  file?: File;
  fileType?: string;
  mimeType?: string;
  previewUrl?: string;
}

type ComposerReferenceKind = "image" | "video" | "audio" | "file";
type ComposerReferencePreviewItem = Pick<
  AttachedItem,
  "file" | "id" | "name" | "fileType" | "mimeType"
>;

type ComposerDocumentOption = {
  id: string;
  name: string;
  fs_path?: string | null;
  file_type?: string | null;
  mime_type?: string | null;
};

function composerPreviewItemFromDoc(doc: ComposerDocumentOption): AttachedItem {
  return {
    name: doc.name,
    id: doc.id,
    fsPath: doc.fs_path || undefined,
    type: "knowledge",
    fileType: doc.file_type || undefined,
    mimeType: doc.mime_type || undefined,
  };
}

function attachedItemLooksLikeImage(item: AttachedItem) {
  const mime = (item.mimeType || item.file?.type || "").toLowerCase();
  const ext = (item.fileType || item.name.split(".").pop() || "").toLowerCase();
  return (
    mime.startsWith("image/") ||
    /^(jpe?g|png|webp|gif|avif|heic|heif)$/i.test(ext)
  );
}

export async function createChatMessageAttachmentSnapshot(
  item: AttachedItem,
): Promise<NonNullable<ChatMessage["attachments"]>[number]> {
  let previewUrl = item.previewUrl;
  if (
    !previewUrl &&
    item.type === "file" &&
    item.file &&
    attachedItemLooksLikeImage(item)
  ) {
    previewUrl = await api.documents.localImageThumbnail(item.file).catch(() => undefined);
  }
  return {
    name: item.name,
    document_id: item.id,
    fsPath: item.fsPath,
    type: item.type,
    fileType: item.fileType,
    mimeType: item.mimeType || item.file?.type,
    previewUrl,
  };
}

function inferComposerReferenceKind(item: ComposerReferencePreviewItem): ComposerReferenceKind {
  const mime = (item.mimeType || "").toLowerCase();
  const ext = (item.fileType || item.name.split(".").pop() || "").toLowerCase();
  if (mime.startsWith("image/") || /^(jpe?g|png|webp|gif|avif|heic|heif)$/i.test(ext)) {
    return "image";
  }
  if (mime.startsWith("video/") || /^(mp4|mov|m4v|webm|avi|mkv)$/i.test(ext)) {
    return "video";
  }
  if (mime.startsWith("audio/") || /^(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(ext)) {
    return "audio";
  }
  return "file";
}

function composerReferenceBadge(item: ComposerReferencePreviewItem, kind: ComposerReferenceKind) {
  if (kind === "image") return "IMG";
  if (kind === "video") return "VID";
  if (kind === "audio") return "AUD";
  return (item.fileType || item.name.split(".").pop() || "FILE").toUpperCase().slice(0, 4);
}

function revokeObjectUrl(url: string) {
  if (url.startsWith("blob:")) URL.revokeObjectURL(url);
}

function ComposerReferenceThumbnail({
  item,
  className = "",
}: {
  item: ComposerReferencePreviewItem;
  className?: string;
}) {
  const kind = inferComposerReferenceKind(item);
  const [thumbUrl, setThumbUrl] = useState("");

  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    setThumbUrl("");
    if (kind !== "image" && kind !== "video") {
      return () => {};
    }
    const load = (() => {
      if (item.file && kind === "image") return Promise.resolve(URL.createObjectURL(item.file));
      if (!item.id) return null;
      return kind === "image"
        ? api.documents.imageThumbnail(item.id, { cache: true })
        : api.documents.videoThumbnail(item.id, { cache: true });
    })();
    if (!load) return () => {};
    load
      .then((url) => {
        objectUrl = url;
        if (!cancelled) setThumbUrl(url);
      })
      .catch(() => {
        if (!cancelled) setThumbUrl("");
      });
    return () => {
      cancelled = true;
      if (objectUrl) revokeObjectUrl(objectUrl);
    };
  }, [item.file, item.id, kind]);

  return (
    <span
      className={`chat-composer-reference-thumb chat-composer-reference-thumb--${kind} ${className}`.trim()}
    >
      {thumbUrl ? (
        <img
          src={thumbUrl}
          alt={t("component.chat_input_footer.reference_thumbnail")}
        />
      ) : (
        composerReferenceBadge(item, kind)
      )}
    </span>
  );
}

export interface MentionOption {
  id: string;
  type: "agent" | "user";
  name: string;
  subtitle?: string;
  avatarUrl?: string | null;
  avatarSeed?: string;
}

export interface ManualSkillItem {
  id: string;
  name: string;
  reference?: ManualSkillReference;
  slug?: string | null;
  displayName?: string | null;
  display_name?: string | null;
  description?: string | null;
  description_i18n?: Record<string, string> | null;
  config?: {
    description_i18n?: Record<string, string> | null;
    description?: string | null;
  } | null;
  category?: string | null;
  type?: string | null;
}

export interface WorkflowInvokeItem {
  bindingId: string;
  workflowId: string;
  title: string;
  description?: string | null;
  placeholder?: string | null;
}

export interface ChatComposerSendContext {
  localWorkerId?: string;
  localWorkerName?: string;
}

function slugifySkillToken(value: string) {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff]+/gi, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "skill";
}

export function manualSkillLabel(skill: ManualSkillItem) {
  return (
    skill.displayName ||
    skill.display_name ||
    skill.name ||
    skill.slug ||
    t("component.chat_input_footer.skill")
  );
}

export function manualSkillToken(skill: ManualSkillItem) {
  return `/${skill.slug || slugifySkillToken(skill.name || manualSkillLabel(skill))}`;
}

export function stripManualSkillTokens(
  text: string,
  skills: ManualSkillItem[],
) {
  let next = text;
  skills.forEach((skill) => {
    const escaped = manualSkillToken(skill).replace(
      /[.*+?^${}()|[\]\\]/g,
      "\\$&",
    );
    next = next
      .replace(new RegExp(`(^|\\s)${escaped}(?=\\s|$)`, "gu"), " ")
      .replace(/\s{2,}/g, " ");
  });
  return next.trim();
}

function slugifyWorkflowToken(value: string) {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff]+/gi, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "flow";
}

export function workflowInvokeToken(workflow: WorkflowInvokeItem) {
  return `%${slugifyWorkflowToken(workflow.title)}`;
}

function replaceAutocompleteTriggerRange(
  value: string,
  start: number,
  replaceEnd: number,
  token: string,
  trigger: string,
  cursorPos = value.length,
) {
  const safeStart = Math.max(0, Math.min(value.length, start));
  const safeCursor = Math.max(0, Math.min(value.length, cursorPos));
  const isTrigger = (index: number) =>
    value[index] === trigger && (index === 0 || /\s/.test(value[index - 1]));
  const isLiveTrigger = (index: number) =>
    isTrigger(index) &&
    (index === value.length - 1 || !/\s/.test(value[index + 1]));
  const triggerHasNoWhitespaceBeforeCursor = (index: number) =>
    !/[\s\n]/.test(value.substring(index + 1, safeCursor));
  let recoveredFromLiveValue = false;
  let resolvedStart =
    isTrigger(safeStart) &&
    safeStart <= safeCursor &&
    triggerHasNoWhitespaceBeforeCursor(safeStart)
      ? safeStart
      : -1;

  // A menu click can leave the browser selection before the typed trigger while
  // React has already reset the stored trigger position. Recover the latest
  // whitespace-delimited live trigger from the editor value in that case.
  if (resolvedStart < 0) {
    for (let index = value.length - 1; index >= 0; index -= 1) {
      if (isLiveTrigger(index)) {
        resolvedStart = index;
        recoveredFromLiveValue = true;
        break;
      }
    }
  }
  if (resolvedStart < 0) resolvedStart = safeStart;

  const triggerEnd =
    value[resolvedStart] === "%" ? resolvedStart + 1 : resolvedStart;
  const firstWhitespaceAfterTrigger = value
    .slice(triggerEnd)
    .search(/\s/);
  const queryEnd =
    firstWhitespaceAfterTrigger < 0
      ? value.length
      : triggerEnd + firstWhitespaceAfterTrigger;
  const preferredEnd =
    !recoveredFromLiveValue && resolvedStart === safeStart
      ? Math.max(replaceEnd, safeCursor)
      : recoveredFromLiveValue
        ? value.length
        : safeCursor;
  const safeEnd = Math.max(
    triggerEnd,
    Math.min(queryEnd, preferredEnd),
  );
  const before = value.substring(0, resolvedStart);
  const after = value.substring(safeEnd);
  const prefixSpacer =
    before && !before.endsWith(" ") && !before.endsWith("\n") ? " " : "";
  const spacer = after.startsWith(" ") || after.startsWith("\n") ? "" : " ";
  const text = `${before}${prefixSpacer}${token}${spacer}${after}`;
  return {
    text,
    cursor: before.length + prefixSpacer.length + token.length + spacer.length,
  };
}

export function replaceWorkflowTriggerRange(
  value: string,
  start: number,
  replaceEnd: number,
  token: string,
  cursorPos = value.length,
) {
  return replaceAutocompleteTriggerRange(
    value,
    start,
    replaceEnd,
    token,
    "%",
    cursorPos,
  );
}

export function replaceMentionTriggerRange(
  value: string,
  start: number,
  replaceEnd: number,
  token: string,
  cursorPos = value.length,
) {
  return replaceAutocompleteTriggerRange(
    value,
    start,
    replaceEnd,
    token,
    "@",
    cursorPos,
  );
}

export function workflowInvokeMessage(workflow: WorkflowInvokeItem) {
  const title = workflow.title.trim() || "Flow";
  return /[.!?。！？]$/.test(title) ? title : `${title}.`;
}

export function stripWorkflowInvokeToken(
  text: string,
  workflow?: WorkflowInvokeItem | null,
) {
  if (!workflow) return text.trim();
  const escaped = workflowInvokeToken(workflow).replace(
    /[.*+?^${}()|[\]\\]/g,
    "\\$&",
  );
  return text
    .replace(new RegExp(`(^|\\s)${escaped}(?=\\s|$)`, "gu"), " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function hasSkillEnvVars(skill: any) {
  const envVars = skill?.env_vars ?? skill?.config?.env_vars;
  if (Array.isArray(envVars)) return envVars.length > 0;
  if (envVars && typeof envVars === "object")
    return Object.keys(envVars).length > 0;
  return Boolean(envVars);
}

function canShowManualSkill(skill: any) {
  return !(hasSkillEnvVars(skill) && skill?.credentials_configured === false);
}

// Preserve composer eligibility independently of artwork. Adding a logo must
// not expose internal providers or change connection/authorization behavior.
const COMPOSER_INTEGRATION_PROVIDERS = new Set([
  "gmail",
  "email",
  "google_calendar",
  "google_drive",
  "notion",
  "slack",
  "discord",
  "telegram",
  "wechat_personal",
  "wechat_official",
  "whatsapp",
  "twilio",
  "linkedin",
  "twitter_x",
  "github",
  "webhook",
  "quickbooks",
  "stripe",
  "paypal",
  "facebook",
  "youtube",
  "tiktok",
  "shopify",
  "woocommerce",
  "square",
  "tiktok_shop",
  "amazon",
  "outlook",
  "onedrive",
  "ms_calendar",
  "ms_teams",
  "ms_excel",
]);

function ComposerIntegrationLogo({ server }: { server: any }) {
  return (
    <span className="chat-composer-connector-icon">
      <IntegrationLogo provider={server.server_key} size={16} />
    </span>
  );
}

interface ChatInputFooterProps {
  voiceScope?: ChatVoiceScope;
  onVoiceConversation?: (id: string) => void;
  value: string;
  onChange: (v: string) => void;
  /** Called for every keydown before the footer's own handler. Call
   *  e.preventDefault() to prevent the footer from acting on this key. */
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void;
  /** When true, plain Enter sends and Shift+Enter inserts a newline.
   *  When false, plain Enter keeps the legacy newline behavior.
   *  @deprecated Behavior is now fixed: Enter sends, Cmd/Ctrl+Enter or Shift+Enter inserts a newline. */
  enterToSend?: boolean;
  streaming: boolean;
  /** Fired when the user clicks send / uses an explicit send shortcut.
   *  Receives a snapshot of attachments at send time; the footer clears them after. The
   *  parent is responsible for clearing `value` (call onChange("")). */
  onSend: (
    text: string,
    attachments: AttachedItem[],
    manualSkills: ManualSkillItem[],
    context?: ChatComposerSendContext,
  ) => void;
  onSendWorkflow?: (
    text: string,
    attachments: AttachedItem[],
    manualSkills: ManualSkillItem[],
    workflow: WorkflowInvokeItem,
    context?: ChatComposerSendContext,
  ) => void;
  onStop: () => void;
  placeholder?: string;
  disabled?: boolean;
  showStopButton?: boolean;
  /** Rendered above the input row (e.g., @mention dropdown). */
  topSlot?: React.ReactNode;
  /** Rendered at the beginning of the action row (e.g., chat mode picker). */
  modeSlot?: React.ReactNode;
  /** When true, mode controls replace the default action buttons in the bottom row. */
  replaceActionButtons?: boolean;
  /** Rendered inside the input row before the textarea (e.g., @mention pill). */
  beforeTextarea?: React.ReactNode;
  mentions?: MentionOption[];
  /** Workspace-scoped Flows available through the `%` inline trigger. */
  workflows?: WorkflowInvokeItem[];
  selectedMentions?: MentionOption[];
  onMentionSelect?: (mention: MentionOption) => void;
  onMentionRemove?: (mention: MentionOption) => void;
  textareaRef?: React.RefObject<HTMLTextAreaElement>;
  editorRef?: React.RefObject<HTMLDivElement>;
  seedAttachments?: AttachedItem[];
  seedAttachmentsKey?: string;
  attachmentButtonIcon?: "plus" | "paperclip";
  /** Optional className for the outer footer wrapper. */
  className?: string;
}

function getTokenText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const el = node as HTMLElement;
  if (el.dataset?.token) return el.dataset.token;
  if (el.tagName === "BR") return "\n";
  let text = "";
  el.childNodes.forEach((child) => {
    text += getTokenText(child);
  });
  return text;
}

function getEditorText(root: HTMLElement | null): string {
  if (!root) return "";
  let text = "";
  root.childNodes.forEach((child) => {
    text += getTokenText(child);
  });
  return text.replace(/\u00a0/g, " ");
}

function collectTokenRanges(text: string, tokens: string[]) {
  const ranges: Array<{ start: number; end: number }> = [];
  const uniqueTokens = Array.from(new Set(tokens.filter(Boolean))).sort(
    (a, b) => b.length - a.length,
  );
  uniqueTokens.forEach((token) => {
    let start = text.indexOf(token);
    while (start >= 0) {
      ranges.push({ start, end: start + token.length });
      start = text.indexOf(token, start + token.length);
    }
  });
  return ranges;
}

function isInlineTokenBoundary(text: string, start: number, end: number) {
  const before = start > 0 ? text[start - 1] : "";
  const after = end < text.length ? text[end] : "";
  const isStartBoundary =
    !before || /\s/.test(before) || /[([{（【《"'“‘]/u.test(before);
  const isEndBoundary =
    !after ||
    /\s/.test(after) ||
    /[)\]}）】》"'”’.,!?;:，。！？、；：]/u.test(after);
  return isStartBoundary && isEndBoundary;
}

function findInlineTokenMatches(text: string, token: string) {
  const matches: Array<{ start: number; end: number }> = [];
  if (!token) return matches;
  let start = text.indexOf(token);
  while (start >= 0) {
    const end = start + token.length;
    if (isInlineTokenBoundary(text, start, end)) {
      matches.push({ start, end });
    }
    start = text.indexOf(token, Math.max(end, start + 1));
  }
  return matches;
}

function hasInlineToken(text: string, token: string) {
  return findInlineTokenMatches(text, token).length > 0;
}

function isOffsetInRanges(
  offset: number,
  ranges: Array<{ start: number; end: number }>,
) {
  return ranges.some((range) => offset >= range.start && offset < range.end);
}

type ComposerTrigger = "@" | "#" | "/" | "%";

function findLastTriggerOutsideTokens(
  text: string,
  trigger: ComposerTrigger,
  protectedRanges: Array<{ start: number; end: number }>,
) {
  for (let i = text.length - 1; i >= 0; i -= 1) {
    if (text[i] !== trigger) continue;
    if (isOffsetInRanges(i, protectedRanges)) continue;
    if (i === 0 || /\s/.test(text[i - 1])) return i;
  }
  return -1;
}

function findInsertedTriggerPosition(
  previous: string,
  next: string,
  trigger: ComposerTrigger,
) {
  if (next.length <= previous.length) return null;
  let start = 0;
  while (
    start < previous.length &&
    start < next.length &&
    previous[start] === next[start]
  ) {
    start += 1;
  }

  let previousEnd = previous.length - 1;
  let nextEnd = next.length - 1;
  while (
    previousEnd >= start &&
    nextEnd >= start &&
    previous[previousEnd] === next[nextEnd]
  ) {
    previousEnd -= 1;
    nextEnd -= 1;
  }

  const inserted = next.slice(start, nextEnd + 1);
  const triggerOffset = inserted.lastIndexOf(trigger);
  return triggerOffset >= 0 ? start + triggerOffset : null;
}

function getTextLength(node: Node): number {
  return getTokenText(node).length;
}

function getPlainOffset(root: HTMLElement | null): number {
  if (!root) return 0;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0)
    return getEditorText(root).length;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return getEditorText(root).length;

  let offset = 0;
  let found = false;
  const walk = (node: Node) => {
    if (found) return;
    if (node === range.startContainer) {
      if (node.nodeType === Node.TEXT_NODE) {
        offset += Math.min(range.startOffset, (node.textContent || "").length);
      } else {
        const children = Array.from(node.childNodes).slice(
          0,
          range.startOffset,
        );
        children.forEach((child) => {
          offset += getTextLength(child);
        });
      }
      found = true;
      return;
    }
    if (
      node.nodeType === Node.ELEMENT_NODE &&
      (node as HTMLElement).dataset?.token
    ) {
      offset += getTextLength(node);
      return;
    }
    node.childNodes.forEach(walk);
  };
  root.childNodes.forEach(walk);
  return offset;
}

function getActiveTextRunBeforeCursor(root: HTMLElement | null) {
  if (!root) return null;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return null;

  let node: Node | null = range.startContainer;
  let offset = range.startOffset;
  if (node.nodeType === Node.ELEMENT_NODE) {
    const children = Array.from(node.childNodes);
    const previous = children[Math.max(0, offset - 1)];
    if (previous?.nodeType === Node.TEXT_NODE) {
      node = previous;
      offset = previous.textContent?.length || 0;
    }
  }
  if (!node || node.nodeType !== Node.TEXT_NODE) return null;
  if ((node.parentElement as HTMLElement | null)?.dataset?.token) return null;

  const end = getPlainOffset(root);
  const text = (node.textContent || "")
    .slice(0, offset)
    .replace(/\u00a0/g, " ");
  return {
    text,
    start: Math.max(0, end - text.length),
    end,
  };
}

function setPlainOffset(root: HTMLElement | null, target: number) {
  if (!root) return;
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  let seen = 0;
  let placed = false;

  const placeAfter = (node: Node) => {
    range.setStartAfter(node);
    range.collapse(true);
    placed = true;
  };

  const walk = (node: Node) => {
    if (placed) return;
    const len = getTextLength(node);
    if (node.nodeType === Node.TEXT_NODE) {
      const next = seen + len;
      if (target <= next) {
        range.setStart(node, Math.max(0, target - seen));
        range.collapse(true);
        placed = true;
      } else {
        seen = next;
      }
      return;
    }
    if (
      node.nodeType === Node.ELEMENT_NODE &&
      (node as HTMLElement).dataset?.token
    ) {
      const next = seen + len;
      if (target <= seen) {
        range.setStartBefore(node);
        range.collapse(true);
        placed = true;
      } else if (target <= next) {
        placeAfter(node);
      }
      seen = next;
      return;
    }
    node.childNodes.forEach(walk);
  };

  root.childNodes.forEach(walk);
  if (!placed) {
    range.selectNodeContents(root);
    range.collapse(false);
  }
  selection.removeAllRanges();
  selection.addRange(range);
}

function adjacentInlineTokenForDeletion(
  root: HTMLElement | null,
  direction: "backward" | "forward",
) {
  if (!root) return null;
  const selection = window.getSelection();
  if (!selection || !selection.isCollapsed || selection.rangeCount === 0)
    return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return null;

  const node = range.startContainer;
  const offset = range.startOffset;
  let candidate: Node | null = null;
  if (node.nodeType === Node.TEXT_NODE) {
    const length = node.textContent?.length || 0;
    if (direction === "backward") {
      if (offset > 0) return null;
      candidate = node.previousSibling;
    } else {
      if (offset < length) return null;
      candidate = node.nextSibling;
    }
  } else {
    candidate =
      direction === "backward"
        ? node.childNodes[offset - 1] || null
        : node.childNodes[offset] || null;
  }

  return candidate?.nodeType === Node.ELEMENT_NODE &&
    (candidate as HTMLElement).dataset?.token
    ? (candidate as HTMLElement)
    : null;
}

function extensionFromMimeType(mimeType: string) {
  if (!mimeType) return "file";
  const subtype = mimeType.split("/")[1] || "file";
  return (
    subtype
      .replace(/^x-/, "")
      .replace(/[^a-z0-9]+/gi, "")
      .toLowerCase() || "file"
  );
}

function filenameForPastedFile(file: File, index: number) {
  if (file.name) return file.name;
  const ext = extensionFromMimeType(file.type || "image/png");
  const stamp = new Date()
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\..+$/, "")
    .replace("T", "-");
  return `${file.type.startsWith("image/") ? "screenshot" : "clipboard"}-${stamp}${index > 0 ? `-${index + 1}` : ""}.${ext}`;
}

function normalizePastedFile(file: File, index: number) {
  const name = filenameForPastedFile(file, index);
  if (file.name === name) return file;
  return new File([file], name, {
    type: file.type || "application/octet-stream",
    lastModified: Date.now(),
  });
}

export default function ChatInputFooter({
  voiceScope,
  onVoiceConversation,
  value,
  onChange,
  onKeyDown,
  enterToSend = false,
  streaming,
  onSend,
  onSendWorkflow,
  onStop,
  placeholder,
  disabled = false,
  showStopButton = true,
  topSlot,
  modeSlot,
  replaceActionButtons = false,
  beforeTextarea,
  mentions = [],
  workflows = [],
  selectedMentions = [],
  onMentionSelect,
  onMentionRemove,
  textareaRef: externalTextareaRef,
  editorRef: externalEditorRef,
  seedAttachments,
  seedAttachmentsKey,
  attachmentButtonIcon = "plus",
  className,
}: ChatInputFooterProps) {
  const navigate = useNavigate();
  const flowsAccess = usePreviewFeatureAccess("flows");
  const flowsAvailable = flowsAccess.enabled;
  const flowsComingSoon = flowsAccess.loaded && !flowsAccess.released;
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const internalEditorRef = useRef<HTMLDivElement>(null);
  const editorRef = externalEditorRef || internalEditorRef;
  const internalTextareaRef = useRef<HTMLTextAreaElement>(null);
  const textareaRef = externalTextareaRef || internalTextareaRef;
  const pendingCursorRef = useRef<number | null>(null);
  const pendingHashTriggerPosRef = useRef<number | null>(null);
  const pendingMentionTriggerPosRef = useRef<number | null>(null);
  const pendingSkillTriggerPosRef = useRef<number | null>(null);
  const pendingWorkflowTriggerPosRef = useRef<number | null>(null);
  const appliedSeedAttachmentsKeyRef = useRef<string | undefined>();
  const lastNativeValueRef = useRef(value);
  const sendLockedRef = useRef(false);
  const streamingRef = useRef(streaming);
  const syncingEditorRef = useRef(false);
  const inlineThumbnailUrlsRef = useRef<string[]>([]);
  const inlineAvatarRootsRef = useRef<Root[]>([]);
  const [selectedManualSkills, setSelectedManualSkills] = useState<
    ManualSkillItem[]
  >([]);
  const [selectedWorkflow, setSelectedWorkflow] =
    useState<WorkflowInvokeItem | null>(null);
  const attachedFilesRef = useRef<AttachedItem[]>([]);
  const [attachedFiles, setAttachedFilesState] = useState<AttachedItem[]>([]);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [integrationsMenuOpen, setIntegrationsMenuOpen] = useState(false);
  const [nangoConnector, setNangoConnector] = useState<IntegrationMCPServer | null>(null);
  const [kbPickerOpen, setKbPickerOpen] = useState(false);
  const [kbSearch, setKbSearch] = useState("");
  const closeAttachmentPickers = useCallback(() => {
    setAttachMenuOpen(false);
    setKbPickerOpen(false);
    setKbSearch("");
  }, []);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const integrationsMenuRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const attachMenuButtonRef = useRef<HTMLButtonElement>(null);
  const integrationsMenuButtonRef = useRef<HTMLButtonElement>(null);
  const attachMenuPortalRef = useRef<HTMLDivElement>(null);
  const integrationsMenuPortalRef = useRef<HTMLDivElement>(null);
  const kbPickerPortalRef = useRef<HTMLDivElement>(null);
  const [attachMenuCoords, setAttachMenuCoords] = useState<{
    top: number;
    left: number;
    width: number;
  } | null>(null);
  const [integrationsMenuCoords, setIntegrationsMenuCoords] = useState<{
    top: number;
    left: number;
    width: number;
  } | null>(null);
  const [kbPickerCoords, setKbPickerCoords] = useState<{
    top: number;
    left: number;
    width: number;
  } | null>(null);

  const [hashDropdownOpen, setHashDropdownOpen] = useState(false);
  const [hashQuery, setHashQuery] = useState("");
  const [hashTriggerPos, setHashTriggerPos] = useState(-1);
  const hashReplaceEndRef = useRef(-1);
  const [hashActiveIdx, setHashActiveIdx] = useState(0);
  const [mentionDropdownOpen, setMentionDropdownOpen] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  const [mentionTriggerPos, setMentionTriggerPos] = useState(-1);
  const mentionReplaceEndRef = useRef(-1);
  const [mentionActiveIdx, setMentionActiveIdx] = useState(0);
  const [skillDropdownOpen, setSkillDropdownOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState("");
  const [skillTriggerPos, setSkillTriggerPos] = useState(-1);
  const skillReplaceEndRef = useRef(-1);
  const [skillActiveIdx, setSkillActiveIdx] = useState(0);
  const [workflowDropdownOpen, setWorkflowDropdownOpen] = useState(false);
  const [workflowQuery, setWorkflowQuery] = useState("");
  const [workflowTriggerPos, setWorkflowTriggerPos] = useState(-1);
  const workflowReplaceEndRef = useRef(-1);
  const [workflowActiveIdx, setWorkflowActiveIdx] = useState(0);

  const voice = useChatVoiceInput({
    scope: voiceScope, disabled: streaming || disabled,
    onTranscript: (text) => {
      onChange(value ? `${value.trimEnd()} ${text}` : text);
      editorRef.current?.focus();
    },
  });
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    streamingRef.current = streaming;
    if (!streaming) {
      sendLockedRef.current = false;
    }
  }, [streaming]);
  useEffect(() => {
    return () => {
      inlineThumbnailUrlsRef.current.forEach(revokeObjectUrl);
      inlineThumbnailUrlsRef.current = [];
      const avatarRoots = inlineAvatarRootsRef.current;
      inlineAvatarRootsRef.current = [];
      queueMicrotask(() =>
        avatarRoots.forEach((avatarRoot) => avatarRoot.unmount()),
      );
    };
  }, []);

  const setAttachedFiles = useCallback(
    (next: AttachedItem[] | ((prev: AttachedItem[]) => AttachedItem[])) => {
      const resolved =
        typeof next === "function"
          ? (next as (prev: AttachedItem[]) => AttachedItem[])(
              attachedFilesRef.current,
            )
          : next;
      attachedFilesRef.current = resolved;
      setAttachedFilesState(resolved);
    },
    [],
  );

  useEffect(() => {
    if (
      !seedAttachmentsKey ||
      appliedSeedAttachmentsKeyRef.current === seedAttachmentsKey
    ) {
      return;
    }
    appliedSeedAttachmentsKeyRef.current = seedAttachmentsKey;
    if (!seedAttachments?.length) return;

    setAttachedFiles((prev) => {
      const seen = new Set(
        prev.map((item) =>
          item.id ? `${item.type || "file"}:${item.id}` : `${item.type || "file"}:${item.name}`,
        ),
      );
      const next = [...prev];
      seedAttachments.forEach((item) => {
        const key = item.id
          ? `${item.type || "file"}:${item.id}`
          : `${item.type || "file"}:${item.name}`;
        if (seen.has(key)) return;
        seen.add(key);
        next.push(item);
      });
      return next;
    });
  }, [seedAttachments, seedAttachmentsKey, setAttachedFiles]);

  const getPortalMenuCoords = useCallback(
    (
      anchor: HTMLElement | null,
      width: number,
      align: "left" | "right" = "left",
    ) => {
      if (!anchor || typeof window === "undefined") return null;
      const rect = anchor.getBoundingClientRect();
      const menuWidth = Math.min(width, Math.max(220, window.innerWidth - 24));
      const desiredLeft =
        align === "right" ? rect.right - menuWidth : rect.left;
      const left = Math.min(
        Math.max(12, desiredLeft),
        Math.max(12, window.innerWidth - menuWidth - 12),
      );
      return {
        top: Math.max(12, rect.top - 8),
        left,
        width: menuWidth,
      };
    },
    [],
  );

  const updatePortalMenuCoords = useCallback(() => {
    if (attachMenuOpen) {
      setAttachMenuCoords(
        getPortalMenuCoords(attachMenuButtonRef.current, 224, "left"),
      );
    }
    if (integrationsMenuOpen) {
      setIntegrationsMenuCoords(
        getPortalMenuCoords(integrationsMenuButtonRef.current, 390, "left"),
      );
    }
    if (kbPickerOpen) {
      setKbPickerCoords(
        getPortalMenuCoords(composerRef.current, 360, "left"),
      );
    }
  }, [
    attachMenuOpen,
    getPortalMenuCoords,
    integrationsMenuOpen,
    kbPickerOpen,
  ]);

  useLayoutEffect(() => {
    if (!attachMenuOpen && !integrationsMenuOpen && !kbPickerOpen) return;
    updatePortalMenuCoords();
    window.addEventListener("resize", updatePortalMenuCoords);
    window.addEventListener("scroll", updatePortalMenuCoords, true);
    return () => {
      window.removeEventListener("resize", updatePortalMenuCoords);
      window.removeEventListener("scroll", updatePortalMenuCoords, true);
    };
  }, [
    attachMenuOpen,
    integrationsMenuOpen,
    kbPickerOpen,
    updatePortalMenuCoords,
  ]);

  const portalMenuStyle = useCallback(
    (coords: { top: number; left: number; width: number }): CSSProperties => ({
      position: "fixed",
      top: coords.top,
      left: coords.left,
      width: coords.width,
      transform: "translateY(-100%)",
      zIndex: 100000,
    }),
    [],
  );

  useEffect(() => {
    setSelectedManualSkills((prev) =>
      prev.filter((skill) => hasInlineToken(value, manualSkillToken(skill))),
    );
  }, [value]);

  /* Close composer menus on outside click */
  useEffect(() => {
    if (!attachMenuOpen && !integrationsMenuOpen && !kbPickerOpen) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        attachMenuOpen &&
        attachMenuRef.current &&
        !attachMenuRef.current.contains(target) &&
        !attachMenuPortalRef.current?.contains(target)
      ) {
        setAttachMenuOpen(false);
      }
      if (
        integrationsMenuOpen &&
        integrationsMenuRef.current &&
        !integrationsMenuRef.current.contains(target) &&
        !integrationsMenuPortalRef.current?.contains(target)
      ) {
        setIntegrationsMenuOpen(false);
      }
      if (
        kbPickerOpen &&
        attachMenuRef.current &&
        !attachMenuRef.current.contains(target) &&
        !kbPickerPortalRef.current?.contains(target)
      ) {
        setKbPickerOpen(false);
        setKbSearch("");
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [attachMenuOpen, integrationsMenuOpen, kbPickerOpen]);

  /* # autocomplete */
  const debouncedHashQuery = useDebounced(hashQuery, 250);
  const { data: hashDocs } = useQuery({
    queryKey: ["documents", "hash-autocomplete", debouncedHashQuery],
    queryFn: () =>
      api.documents.list({
        search: debouncedHashQuery || undefined,
        limit: 200,
      }),
    enabled: hashDropdownOpen,
    // Keep the previous results visible while the debounced refetch is in
    // flight — otherwise the list flashes "no matching files" on every
    // keystroke.
    placeholderData: (prev) => prev,
  });
  const attachedKnowledgeIds = new Set(
    attachedFiles
      .filter(
        (item) =>
          item.type === "knowledge" &&
          item.id &&
          hasInlineToken(value, `#${item.name}`),
      )
      .map((item) => item.id),
  );
  // The backend orders by recency; rank name matches first so the file the
  // user is typing isn't buried under newer documents.
  const hashRankQuery = hashQuery.trim().toLowerCase();
  const hashMatchScore = (doc: any) => {
    if (!hashRankQuery) return 0;
    const name = (doc.name || "").toLowerCase();
    if (name.startsWith(hashRankQuery)) return 0;
    if (name.includes(hashRankQuery)) return 1;
    return 2;
  };
  const hashFiltered = (hashDocs?.items || [])
    .filter((doc: any) => !attachedKnowledgeIds.has(doc.id))
    .sort((a: any, b: any) => hashMatchScore(a) - hashMatchScore(b))
    .slice(0, 50);
  const debouncedSkillQuery = useDebounced(skillQuery, 200);
  const { data: skillOptions, isLoading: skillsLoading } = useQuery({
    queryKey: ["skills", "composer-manual"],
    queryFn: () => api.skills.list(),
    enabled: skillDropdownOpen,
  });
  const selectedManualSkillIds = new Set(
    selectedManualSkills.map((skill) => skill.id),
  );
  const skillFiltered = (skillOptions || [])
    .filter((raw: any) => {
      if (!raw?.id || selectedManualSkillIds.has(raw.id)) return false;
      if (!canShowManualSkill(raw)) return false;
      const q = debouncedSkillQuery.trim().toLowerCase();
      if (!q) return true;
      const description = getSkillDescription(raw);
      const haystack = [
        raw.name,
        raw.slug,
        raw.display_name,
        raw.displayName,
        description,
        raw.category,
        ...(Array.isArray(raw.tags) ? raw.tags : []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    })
    .slice(0, 20);
  const selectedWorkflowIsInline = Boolean(
    selectedWorkflow && hasInlineToken(value, workflowInvokeToken(selectedWorkflow)),
  );
  const workflowFiltered = workflows
    .filter((workflow) => {
      if (selectedWorkflowIsInline) return false;
      const q = workflowQuery.trim().toLowerCase();
      if (!q) return true;
      return [
        workflow.title,
        workflow.description,
        workflowInvokeToken(workflow),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(q);
    })
    .slice(0, 20);
  const mentionFiltered = mentions
    .filter((mention) => {
      const q = mentionQuery.trim().toLowerCase();
      if (!q) return true;
      return (
        mention.name.toLowerCase().includes(q) ||
        mention.subtitle?.toLowerCase().includes(q) ||
        mention.type.includes(q)
      );
    })
    .slice(0, 12);

  /* KB picker */
  const { data: kbDocs, isLoading: kbDocsLoading } = useQuery({
    queryKey: ["documents", "kb-picker-shared", kbSearch],
    queryFn: () =>
      api.documents.list({
        search: kbSearch || undefined,
        limit: 20,
      }),
    enabled: kbPickerOpen,
  });

  const {
    data: integrationServers,
    isLoading: integrationsLoading,
    isError: integrationsError,
    refetch: refetchIntegrations,
  } = useQuery(
    integrationCatalogQueryOptions(privateApiEnabled && integrationsMenuOpen),
  );
  const composerIntegrationServers = useMemo(() => {
    return (integrationServers || []).filter((server: any) => {
      const accessState = connectorAccessState(server);
      const readyToAuth = Boolean(
        server.nango_provider_config_key ||
        (server.auth_type === "oauth2" && server.oauth_configured),
      );
      const supportedInComposer = COMPOSER_INTEGRATION_PROVIDERS.has(server.server_key);
      return (
        (accessState !== "connect" || readyToAuth) &&
        !server.coming_soon &&
        supportedInComposer
      );
    });
  }, [integrationServers]);

  const updateAutocompleteState = useCallback(
    (val: string, cursorPos: number) => {
      onChange(val);
      const before = val.substring(0, cursorPos);
      const activeTextRun = getActiveTextRunBeforeCursor(editorRef.current);
      const findActiveTextTrigger = (trigger: ComposerTrigger) => {
        if (!activeTextRun) return null;
        const localIdx = activeTextRun.text.lastIndexOf(trigger);
        if (localIdx < 0) return null;
        const absoluteIdx = activeTextRun.start + localIdx;
        const previousChar = absoluteIdx > 0 ? val[absoluteIdx - 1] : "";
        if (absoluteIdx > 0 && !/\s/.test(previousChar)) return null;
        return {
          index: absoluteIdx,
          query: activeTextRun.text.substring(localIdx + 1),
        };
      };
      const protectedTokens = [
        ...selectedMentions.map((mention) => `@${mention.name}`),
        ...selectedManualSkills.map((skill) => manualSkillToken(skill)),
        ...(selectedWorkflow
          ? [workflowInvokeToken(selectedWorkflow)]
          : []),
      ];
      const protectedTokenRanges = collectTokenRanges(before, protectedTokens);
      const allProtectedTokenRanges = collectTokenRanges(val, protectedTokens);
      const forcedMentionIdx = pendingMentionTriggerPosRef.current;
      pendingMentionTriggerPosRef.current = null;
      const activeMentionTrigger = findActiveTextTrigger("@");
      const atIdx =
        activeMentionTrigger?.index ??
        (forcedMentionIdx != null
          ? val[forcedMentionIdx] === "@"
            ? forcedMentionIdx
            : findLastTriggerOutsideTokens(val, "@", allProtectedTokenRanges)
          : findLastTriggerOutsideTokens(before, "@", protectedTokenRanges));
      if (
        mentions.length > 0 &&
        atIdx >= 0 &&
        (atIdx === 0 || /\s/.test(val[atIdx - 1]))
      ) {
        const mentionCursor = activeMentionTrigger
          ? atIdx + 1 + activeMentionTrigger.query.length
          : forcedMentionIdx != null
            ? Math.max(cursorPos, atIdx + 1)
            : cursorPos;
        const q =
          activeMentionTrigger?.query ??
          val.substring(atIdx + 1, mentionCursor);
        const existingMention = selectedMentions.some((mention) =>
          q.startsWith(mention.name),
        );
        if (existingMention || q.includes(" ") || q.includes("\n")) {
          setMentionDropdownOpen(false);
          mentionReplaceEndRef.current = -1;
        } else {
          setMentionDropdownOpen(true);
          setMentionQuery(q);
          setMentionTriggerPos(atIdx);
          mentionReplaceEndRef.current = mentionCursor;
          setMentionActiveIdx(0);
          setHashDropdownOpen(false);
          hashReplaceEndRef.current = -1;
          setSkillDropdownOpen(false);
          skillReplaceEndRef.current = -1;
          setWorkflowDropdownOpen(false);
          workflowReplaceEndRef.current = -1;
          return;
        }
      } else {
        setMentionDropdownOpen(false);
        mentionReplaceEndRef.current = -1;
      }

      const forcedHashIdx = pendingHashTriggerPosRef.current;
      pendingHashTriggerPosRef.current = null;
      const activeHashTrigger = findActiveTextTrigger("#");
      const hashIdx =
        activeHashTrigger?.index ??
        (forcedHashIdx != null
          ? val[forcedHashIdx] === "#"
            ? forcedHashIdx
            : findLastTriggerOutsideTokens(val, "#", allProtectedTokenRanges)
          : findLastTriggerOutsideTokens(before, "#", protectedTokenRanges));
      if (hashIdx >= 0 && (hashIdx === 0 || /\s/.test(val[hashIdx - 1]))) {
        const hashCursor = activeHashTrigger
          ? hashIdx + 1 + activeHashTrigger.query.length
          : forcedHashIdx != null || cursorPos < hashIdx + 1
            ? Math.max(cursorPos, hashIdx + 1)
            : cursorPos;
        const q =
          activeHashTrigger?.query ?? val.substring(hashIdx + 1, hashCursor);
        if (/\s/.test(q)) {
          setHashDropdownOpen(false);
          hashReplaceEndRef.current = -1;
        } else {
          setHashDropdownOpen(true);
          setHashQuery(q);
          setHashTriggerPos(hashIdx);
          hashReplaceEndRef.current = hashCursor;
          setHashActiveIdx(0);
          setSkillDropdownOpen(false);
          skillReplaceEndRef.current = -1;
          setWorkflowDropdownOpen(false);
          workflowReplaceEndRef.current = -1;
          return;
        }
      } else {
        setHashDropdownOpen(false);
        hashReplaceEndRef.current = -1;
      }

      const forcedSkillIdx = pendingSkillTriggerPosRef.current;
      pendingSkillTriggerPosRef.current = null;
      const activeSkillTrigger = findActiveTextTrigger("/");
      const skillIdx =
        activeSkillTrigger?.index ??
        (forcedSkillIdx != null
          ? val[forcedSkillIdx] === "/"
            ? forcedSkillIdx
            : findLastTriggerOutsideTokens(val, "/", allProtectedTokenRanges)
          : findLastTriggerOutsideTokens(before, "/", protectedTokenRanges));
      if (skillIdx >= 0 && (skillIdx === 0 || /\s/.test(val[skillIdx - 1]))) {
        const skillCursor = activeSkillTrigger
          ? skillIdx + 1 + activeSkillTrigger.query.length
          : forcedSkillIdx != null || cursorPos < skillIdx + 1
            ? Math.max(cursorPos, skillIdx + 1)
            : cursorPos;
        const q =
          activeSkillTrigger?.query ?? val.substring(skillIdx + 1, skillCursor);
        const existingSkill = selectedManualSkills.some((skill) =>
          q.startsWith((skill.slug || skill.name || "").trim()),
        );
        if (existingSkill || q.includes(" ") || q.includes("\n")) {
          setSkillDropdownOpen(false);
          skillReplaceEndRef.current = -1;
        } else {
          setSkillDropdownOpen(true);
          setSkillQuery(q);
          setSkillTriggerPos(skillIdx);
          skillReplaceEndRef.current = skillCursor;
          setSkillActiveIdx(0);
          setMentionDropdownOpen(false);
          mentionReplaceEndRef.current = -1;
          setHashDropdownOpen(false);
          hashReplaceEndRef.current = -1;
          setWorkflowDropdownOpen(false);
          workflowReplaceEndRef.current = -1;
          return;
        }
      } else {
        setSkillDropdownOpen(false);
        skillReplaceEndRef.current = -1;
      }

      const forcedWorkflowIdx = pendingWorkflowTriggerPosRef.current;
      pendingWorkflowTriggerPosRef.current = null;
      const activeWorkflowTrigger = findActiveTextTrigger("%");
      const workflowIdx =
        activeWorkflowTrigger?.index ??
        (forcedWorkflowIdx != null
          ? val[forcedWorkflowIdx] === "%"
            ? forcedWorkflowIdx
            : findLastTriggerOutsideTokens(val, "%", allProtectedTokenRanges)
          : findLastTriggerOutsideTokens(before, "%", protectedTokenRanges));
      if (
        workflows.length > 0 &&
        workflowIdx >= 0 &&
        (workflowIdx === 0 || /\s/.test(val[workflowIdx - 1]))
      ) {
        const workflowCursor = activeWorkflowTrigger
          ? workflowIdx + 1 + activeWorkflowTrigger.query.length
          : forcedWorkflowIdx != null || cursorPos < workflowIdx + 1
            ? Math.max(cursorPos, workflowIdx + 1)
            : cursorPos;
        const q =
          activeWorkflowTrigger?.query ??
          val.substring(workflowIdx + 1, workflowCursor);
        if (selectedWorkflowIsInline || q.includes(" ") || q.includes("\n")) {
          setWorkflowDropdownOpen(false);
          workflowReplaceEndRef.current = -1;
        } else {
          setWorkflowDropdownOpen(true);
          setWorkflowQuery(q);
          setWorkflowTriggerPos(workflowIdx);
          workflowReplaceEndRef.current = workflowCursor;
          setWorkflowActiveIdx(0);
          setMentionDropdownOpen(false);
          mentionReplaceEndRef.current = -1;
          setHashDropdownOpen(false);
          hashReplaceEndRef.current = -1;
          setSkillDropdownOpen(false);
          skillReplaceEndRef.current = -1;
        }
      } else {
        setWorkflowDropdownOpen(false);
        workflowReplaceEndRef.current = -1;
      }
    },
    [
      attachedFiles,
      mentions.length,
      onChange,
      selectedManualSkills,
      selectedMentions,
      selectedWorkflow,
      selectedWorkflowIsInline,
      workflows.length,
    ],
  );

  const handleEditorBeforeInput = useCallback(
    (e: React.FormEvent<HTMLDivElement>) => {
      const nativeEvent = e.nativeEvent as InputEvent;
      if (nativeEvent.inputType !== "insertText") return;
      if (nativeEvent.data === "#") {
        pendingHashTriggerPosRef.current = getPlainOffset(editorRef.current);
      } else if (nativeEvent.data === "@") {
        pendingMentionTriggerPosRef.current = getPlainOffset(editorRef.current);
      } else if (nativeEvent.data === "/") {
        pendingSkillTriggerPosRef.current = getPlainOffset(editorRef.current);
      } else if (nativeEvent.data === "%") {
        pendingWorkflowTriggerPosRef.current = getPlainOffset(editorRef.current);
      }
    },
    [],
  );

  const handleEditorInput = useCallback(() => {
    if (syncingEditorRef.current) return;
    const val = getEditorText(editorRef.current);
    const cursorPos = getPlainOffset(editorRef.current);
    const previousVal = lastNativeValueRef.current;
    if (pendingHashTriggerPosRef.current == null) {
      pendingHashTriggerPosRef.current = findInsertedTriggerPosition(
        previousVal,
        val,
        "#",
      );
    }
    if (pendingMentionTriggerPosRef.current == null) {
      pendingMentionTriggerPosRef.current = findInsertedTriggerPosition(
        previousVal,
        val,
        "@",
      );
    }
    if (pendingSkillTriggerPosRef.current == null) {
      pendingSkillTriggerPosRef.current = findInsertedTriggerPosition(
        previousVal,
        val,
        "/",
      );
    }
    if (pendingWorkflowTriggerPosRef.current == null) {
      pendingWorkflowTriggerPosRef.current = findInsertedTriggerPosition(
        previousVal,
        val,
        "%",
      );
    }
    lastNativeValueRef.current = val;
    updateAutocompleteState(val, cursorPos);
  }, [updateAutocompleteState]);

  const insertPlainTextAtCursor = useCallback(
    (text: string) => {
      const cursorPos = getPlainOffset(editorRef.current);
      const next = `${value.slice(0, cursorPos)}${text}${value.slice(cursorPos)}`;
      const nextCursor = cursorPos + text.length;
      pendingCursorRef.current = nextCursor;
      updateAutocompleteState(next, nextCursor);
    },
    [updateAutocompleteState, value],
  );

  const selectMention = useCallback(
    (mention: MentionOption) => {
      const editorValue = getEditorText(editorRef.current) || value;
      const editorCursor = getPlainOffset(editorRef.current);
      const start = mentionTriggerPos >= 0 ? mentionTriggerPos : editorCursor;
      const replaceEnd =
        mentionReplaceEndRef.current >= start
          ? mentionReplaceEndRef.current
          : Math.max(start, editorCursor);
      const token = `@${mention.name}`;
      const replacement = replaceMentionTriggerRange(
        editorValue,
        start,
        replaceEnd,
        token,
        editorCursor,
      );
      onChange(replacement.text);
      const nextCursor = replacement.cursor;
      pendingCursorRef.current = nextCursor;
      onMentionSelect?.(mention);
      setMentionDropdownOpen(false);
      setMentionQuery("");
      setMentionTriggerPos(-1);
      mentionReplaceEndRef.current = -1;
      setTimeout(() => {
        editorRef.current?.focus();
        setPlainOffset(editorRef.current, nextCursor);
      }, 0);
    },
    [mentionTriggerPos, onChange, onMentionSelect, value],
  );

  const selectManualSkill = useCallback(
    (rawSkill: any) => {
      const skill: ManualSkillItem = {
        id: rawSkill.id,
        name: rawSkill.name || rawSkill.slug || t("component.chat_input_footer.skill"),
        reference: { kind: "id", value: rawSkill.id },
        slug: rawSkill.slug,
        displayName: rawSkill.displayName || rawSkill.display_name,
        display_name: rawSkill.display_name,
        description: rawSkill.description,
        category: rawSkill.category,
        type: rawSkill.type,
      };
      const start =
        skillTriggerPos >= 0
          ? skillTriggerPos
          : getPlainOffset(editorRef.current);
      const before = value.substring(0, start);
      const replaceEnd =
        skillReplaceEndRef.current >= start
          ? skillReplaceEndRef.current
          : Math.max(start, getPlainOffset(editorRef.current));
      const after = value.substring(replaceEnd);
      const token = manualSkillToken(skill);
      const prefixSpacer =
        before && !before.endsWith(" ") && !before.endsWith("\n") ? " " : "";
      const spacer = after.startsWith(" ") || after.startsWith("\n") ? "" : " ";
      onChange(`${before}${prefixSpacer}${token}${spacer}${after}`);
      const nextCursor =
        before.length + prefixSpacer.length + token.length + spacer.length;
      pendingCursorRef.current = nextCursor;
      setSelectedManualSkills((prev) =>
        prev.some((item) => item.id === skill.id) ? prev : [...prev, skill],
      );
      setSkillDropdownOpen(false);
      setSkillQuery("");
      setSkillTriggerPos(-1);
      skillReplaceEndRef.current = -1;
      setSkillActiveIdx(0);
      setTimeout(() => {
        editorRef.current?.focus();
        setPlainOffset(editorRef.current, nextCursor);
      }, 0);
    },
    [onChange, skillTriggerPos, value],
  );

  const selectWorkflow = useCallback(
    (workflow: WorkflowInvokeItem) => {
      if (!flowsAvailable) return;
      // `value` may still be one input event behind when a pointer click picks
      // a Flow. Use the contenteditable value so the typed `%` is in the
      // replacement source instead of being left behind as plain text.
      const editorValue = getEditorText(editorRef.current) || value;
      const editorCursor = getPlainOffset(editorRef.current);
      const start =
        workflowTriggerPos >= 0
          ? workflowTriggerPos
          : editorCursor;
      const replaceEnd =
        workflowReplaceEndRef.current >= start
          ? workflowReplaceEndRef.current
          : Math.max(start, editorCursor);
      const token = workflowInvokeToken(workflow);
      const replacement = replaceWorkflowTriggerRange(
        editorValue,
        start,
        replaceEnd,
        token,
        editorCursor,
      );
      onChange(replacement.text);
      const nextCursor = replacement.cursor;
      pendingCursorRef.current = nextCursor;
      setSelectedWorkflow(workflow);
      setWorkflowDropdownOpen(false);
      setWorkflowQuery("");
      setWorkflowTriggerPos(-1);
      workflowReplaceEndRef.current = -1;
      setWorkflowActiveIdx(0);
      setTimeout(() => {
        editorRef.current?.focus();
        setPlainOffset(editorRef.current, nextCursor);
      }, 0);
    },
    [flowsAvailable, onChange, value, workflowTriggerPos],
  );

  const openSkillPicker = useCallback(() => {
    if (skillDropdownOpen) {
      setSkillDropdownOpen(false);
      skillReplaceEndRef.current = -1;
      return;
    }
    const cursorPos = getPlainOffset(editorRef.current);
    setSkillDropdownOpen(true);
    setSkillQuery("");
    setSkillTriggerPos(cursorPos);
    skillReplaceEndRef.current = cursorPos;
    setSkillActiveIdx(0);
    setMentionDropdownOpen(false);
    mentionReplaceEndRef.current = -1;
    setHashDropdownOpen(false);
    hashReplaceEndRef.current = -1;
    setWorkflowDropdownOpen(false);
    workflowReplaceEndRef.current = -1;
    setAttachMenuOpen(false);
    setIntegrationsMenuOpen(false);
    setTimeout(() => editorRef.current?.focus(), 0);
  }, [skillDropdownOpen]);

  const removeAutocompleteTriggerRange = useCallback(
    (
      trigger: ComposerTrigger,
      start: number,
      replaceEnd: number,
      refocus: boolean,
    ) => {
      if (start < 0 || value[start] !== trigger) return;
      const cursorPos = getPlainOffset(editorRef.current);
      const end = Math.min(
        value.length,
        Math.max(start + 1, replaceEnd > start ? replaceEnd : cursorPos),
      );
      const before = value.substring(0, start);
      let after = value.substring(end);
      if (before && after && /\s$/.test(before) && /^\s/.test(after)) {
        after = after.replace(/^[ \t]+/, "");
      } else if (before && after && !/\s$/.test(before) && !/^\s/.test(after)) {
        after = ` ${after}`;
      }
      const next = `${before}${after}`;
      onChange(next);
      pendingCursorRef.current = before.length;
      if (refocus) {
        setTimeout(() => {
          editorRef.current?.focus();
          setPlainOffset(editorRef.current, before.length);
        }, 0);
      }
    },
    [onChange, value],
  );

  const dismissMentionAutocomplete = useCallback(
    (removeTrigger = false, refocus = true) => {
      const start = mentionTriggerPos;
      const replaceEnd = mentionReplaceEndRef.current;
      setMentionDropdownOpen(false);
      setMentionQuery("");
      setMentionTriggerPos(-1);
      mentionReplaceEndRef.current = -1;
      setMentionActiveIdx(0);
      if (removeTrigger)
        removeAutocompleteTriggerRange("@", start, replaceEnd, refocus);
    },
    [mentionTriggerPos, removeAutocompleteTriggerRange],
  );

  const dismissSkillAutocomplete = useCallback(
    (removeTrigger = false, refocus = true) => {
      const start = skillTriggerPos;
      const replaceEnd = skillReplaceEndRef.current;
      setSkillDropdownOpen(false);
      setSkillQuery("");
      setSkillTriggerPos(-1);
      skillReplaceEndRef.current = -1;
      setSkillActiveIdx(0);
      if (removeTrigger)
        removeAutocompleteTriggerRange("/", start, replaceEnd, refocus);
    },
    [removeAutocompleteTriggerRange, skillTriggerPos],
  );

  const dismissWorkflowAutocomplete = useCallback(
    (removeTrigger = false, refocus = true) => {
      const start = workflowTriggerPos;
      const replaceEnd = workflowReplaceEndRef.current;
      setWorkflowDropdownOpen(false);
      setWorkflowQuery("");
      setWorkflowTriggerPos(-1);
      workflowReplaceEndRef.current = -1;
      setWorkflowActiveIdx(0);
      if (removeTrigger) {
        removeAutocompleteTriggerRange("%", start, replaceEnd, refocus);
      }
    },
    [removeAutocompleteTriggerRange, workflowTriggerPos],
  );

  const dismissHashAutocomplete = useCallback(
    (removeTrigger = false, refocus = true) => {
      const start = hashTriggerPos;
      const replaceEnd = hashReplaceEndRef.current;
      setHashDropdownOpen(false);
      setHashQuery("");
      setHashTriggerPos(-1);
      hashReplaceEndRef.current = -1;
      pendingHashTriggerPosRef.current = null;
      setHashActiveIdx(0);
      if (removeTrigger)
        removeAutocompleteTriggerRange("#", start, replaceEnd, refocus);
    },
    [hashTriggerPos, removeAutocompleteTriggerRange],
  );

  const selectHashDoc = useCallback(
    (doc: ComposerDocumentOption) => {
      dismissHashAutocomplete(true);
      setAttachedFiles((prev) =>
        prev.some((f) => f.id === doc.id)
          ? prev
          : [...prev, composerPreviewItemFromDoc(doc)],
      );
    },
    [dismissHashAutocomplete],
  );

  const removeTokenText = useCallback(
    (token: string) => {
      const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const next = value
        .replace(new RegExp(`(^|\\s)${escaped}(?=\\s|$)`, "u"), " ")
        .replace(/\s{2,}/g, " ")
        .trimStart();
      onChange(next);
      pendingCursorRef.current = next.length;
      setTimeout(() => editorRef.current?.focus(), 0);
    },
    [onChange, value],
  );

  const removeMentionToken = useCallback(
    (mention: MentionOption) => {
      removeTokenText(`@${mention.name}`);
      onMentionRemove?.(mention);
    },
    [onMentionRemove, removeTokenText],
  );

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      closeAttachmentPickers();
      const files = e.target.files;
      if (files) {
        Array.from(files).forEach((file) => {
          setAttachedFiles((prev) => [
            ...prev,
            {
              name: file.name,
              type: "file",
              file,
              mimeType: file.type,
              fileType: extensionFromMimeType(file.type),
            },
          ]);
        });
      }
      e.target.value = "";
    },
    [closeAttachmentPickers],
  );

  const attachLocalFiles = useCallback((files: File[]) => {
    if (files.length === 0) return;
    setAttachedFiles((prev) => [
      ...prev,
      ...files.map((file) => ({
        name: file.name,
        type: "file" as const,
        file,
        mimeType: file.type,
        fileType: extensionFromMimeType(file.type),
      })),
    ]);
  }, []);

  const handleEditorPaste = useCallback(
    (e: React.ClipboardEvent<HTMLDivElement>) => {
      const clipboardItems = Array.from(e.clipboardData.items || []);
      const itemFiles = clipboardItems
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((file): file is File => Boolean(file))
        .map(normalizePastedFile);
      const dataFiles =
        itemFiles.length > 0
          ? []
          : Array.from(e.clipboardData.files || []).map(normalizePastedFile);
      const files = [...itemFiles, ...dataFiles];
      const text = e.clipboardData.getData("text/plain");

      e.preventDefault();
      if (files.length > 0) {
        attachLocalFiles(files);
        if (text) insertPlainTextAtCursor(text);
        return;
      }
      insertPlainTextAtCursor(text);
    },
    [attachLocalFiles, insertPlainTextAtCursor],
  );

  const addKbDoc = (doc: ComposerDocumentOption) => {
    closeAttachmentPickers();
    dismissHashAutocomplete(true, false);
    if (attachedFiles.some((f) => f.id === doc.id)) return;
    setAttachedFiles((prev) => [...prev, composerPreviewItemFromDoc(doc)]);
    setTimeout(() => {
      editorRef.current?.focus();
      setPlainOffset(editorRef.current, getEditorText(editorRef.current).length);
    }, 0);
  };

  const insertComposerHint = useCallback(
    (hint: string) => {
      const next = value.trim() ? `${value.trim()}\n${hint}` : hint;
      onChange(next);
      setAttachMenuOpen(false);
      setIntegrationsMenuOpen(false);
      pendingCursorRef.current = next.length;
      window.requestAnimationFrame(() => {
        const root = editorRef.current;
        if (!root) return;
        root.focus();
        setPlainOffset(root, next.length);
      });
    },
    [value, onChange],
  );

  const handleConnectorClick = useCallback(
    (server: any) => {
      const accessState = connectorAccessState(server);
      if (accessState === "usable") {
        insertComposerHint(`Use ${server.name} to `);
        return;
      }
      setIntegrationsMenuOpen(false);
      // Popup authorization must not unmount the composer and discard its draft/files.
      // Reuse Integrations' authorization controller, including sync and WhatsApp setup.
      if (accessState === "connect" && server.nango_provider_config_key) {
        integrationsMenuButtonRef.current?.focus();
        setNangoConnector(server);
        return;
      }
      // Account repair and non-popup setup still use the full settings surface.
      navigate(integrationSetupHref(server.server_key));
    },
    [insertComposerHint, navigate],
  );

  const removeAttachment = (idx: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const triggerSend = useCallback(() => {
    const text = value.trim();
    const currentAttachments = attachedFilesRef.current;
    const manualSkillSnapshot = selectedManualSkills.filter((skill) =>
      hasInlineToken(value, manualSkillToken(skill)),
    );
    const workflowSnapshot =
      flowsAvailable &&
      selectedWorkflow &&
      hasInlineToken(value, workflowInvokeToken(selectedWorkflow))
        ? selectedWorkflow
        : null;
    if (
      (!text &&
        currentAttachments.length === 0 &&
        manualSkillSnapshot.length === 0 &&
        !workflowSnapshot) ||
      streaming ||
      disabled ||
      sendLockedRef.current ||
      voice.busy
    )
      return;
    sendLockedRef.current = true;
    dismissHashAutocomplete(false, false);
    const snapshot = currentAttachments;
    setAttachedFiles([]);
    setSelectedManualSkills([]);
    setSelectedWorkflow(null);
    if (workflowSnapshot && onSendWorkflow) {
      onSendWorkflow(text, snapshot, manualSkillSnapshot, workflowSnapshot);
    } else {
      onSend(text, snapshot, manualSkillSnapshot);
    }
    window.setTimeout(() => {
      if (!streamingRef.current) {
        sendLockedRef.current = false;
      }
    }, 750);
  }, [
    value,
    streaming,
    selectedManualSkills,
    selectedWorkflow,
    flowsAvailable,
    voice.busy,
    onSend,
    onSendWorkflow,
    disabled,
    setAttachedFiles,
    dismissHashAutocomplete,
  ]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (voice.busy && e.key === "Enter") { e.preventDefault(); return; }
      if (onKeyDown) onKeyDown(e);
      if (e.defaultPrevented) return;

      if (
        (e.key === "Backspace" || e.key === "Delete") &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey
      ) {
        const token = adjacentInlineTokenForDeletion(
          editorRef.current,
          e.key === "Backspace" ? "backward" : "forward",
        );
        const mentionId = token?.dataset.mentionId;
        const mentionType = token?.dataset.mentionType;
        const mention = selectedMentions.find(
          (item) => item.id === mentionId && item.type === mentionType,
        );
        if (mention) {
          e.preventDefault();
          removeMentionToken(mention);
          return;
        }
      }

      if (mentionDropdownOpen) {
        if (e.key === "ArrowDown" && mentionFiltered.length > 0) {
          e.preventDefault();
          setMentionActiveIdx((i) =>
            Math.min(i + 1, mentionFiltered.length - 1),
          );
          return;
        }
        if (e.key === "ArrowUp" && mentionFiltered.length > 0) {
          e.preventDefault();
          setMentionActiveIdx((i) => Math.max(i - 1, 0));
          return;
        }
        if (
          (e.key === "Enter" || e.key === "Tab") &&
          mentionFiltered.length > 0
        ) {
          e.preventDefault();
          selectMention(mentionFiltered[mentionActiveIdx]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          dismissMentionAutocomplete(true);
          return;
        }
      }

      if (skillDropdownOpen) {
        if (e.key === "ArrowDown" && skillFiltered.length > 0) {
          e.preventDefault();
          setSkillActiveIdx((i) => Math.min(i + 1, skillFiltered.length - 1));
          return;
        }
        if (e.key === "ArrowUp" && skillFiltered.length > 0) {
          e.preventDefault();
          setSkillActiveIdx((i) => Math.max(i - 1, 0));
          return;
        }
        if (
          (e.key === "Enter" || e.key === "Tab") &&
          skillFiltered.length > 0
        ) {
          e.preventDefault();
          selectManualSkill(skillFiltered[skillActiveIdx]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          dismissSkillAutocomplete(true);
          return;
        }
      }

      if (workflowDropdownOpen) {
        if (e.key === "ArrowDown" && workflowFiltered.length > 0) {
          e.preventDefault();
          setWorkflowActiveIdx((i) =>
            Math.min(i + 1, workflowFiltered.length - 1),
          );
          return;
        }
        if (e.key === "ArrowUp" && workflowFiltered.length > 0) {
          e.preventDefault();
          setWorkflowActiveIdx((i) => Math.max(i - 1, 0));
          return;
        }
        if (
          (e.key === "Enter" || e.key === "Tab") &&
          flowsAvailable &&
          workflowFiltered.length > 0
        ) {
          e.preventDefault();
          selectWorkflow(workflowFiltered[workflowActiveIdx]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          dismissWorkflowAutocomplete(true);
          return;
        }
      }

      if (hashDropdownOpen) {
        if (e.key === "ArrowDown" && hashFiltered.length > 0) {
          e.preventDefault();
          setHashActiveIdx((i) => Math.min(i + 1, hashFiltered.length - 1));
          return;
        }
        if (e.key === "ArrowUp" && hashFiltered.length > 0) {
          e.preventDefault();
          setHashActiveIdx((i) => Math.max(i - 1, 0));
          return;
        }
        if ((e.key === "Enter" || e.key === "Tab") && hashFiltered.length > 0) {
          e.preventDefault();
          selectHashDoc(hashFiltered[hashActiveIdx]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          dismissHashAutocomplete(true);
          return;
        }
      }
      if (
        shouldHandleComposerEnter(e.nativeEvent as KeyboardEvent) &&
        (e.metaKey || e.ctrlKey)
      ) {
        e.preventDefault();
        insertPlainTextAtCursor("\n");
        return;
      }
      if (shouldHandleComposerEnter(e.nativeEvent as KeyboardEvent)) {
        e.preventDefault();
        if (e.shiftKey) {
          insertPlainTextAtCursor("\n");
        } else {
          triggerSend();
        }
      }
    },
    [
      voice.busy,
      onKeyDown,
      removeMentionToken,
      selectedMentions,
      mentionDropdownOpen,
      mentionFiltered,
      mentionActiveIdx,
      selectMention,
      dismissMentionAutocomplete,
      skillDropdownOpen,
      skillFiltered,
      skillActiveIdx,
      selectManualSkill,
      dismissSkillAutocomplete,
      workflowDropdownOpen,
      workflowFiltered,
      workflowActiveIdx,
      flowsAvailable,
      selectWorkflow,
      dismissWorkflowAutocomplete,
      hashDropdownOpen,
      hashFiltered,
      hashActiveIdx,
      selectHashDoc,
      dismissHashAutocomplete,
      enterToSend,
      insertPlainTextAtCursor,
      triggerSend,
    ],
  );

  const handleEditorBlur = useCallback(() => {
    setFocused(false);
    if (mentionDropdownOpen) dismissMentionAutocomplete(true, false);
    if (skillDropdownOpen) dismissSkillAutocomplete(true, false);
    if (workflowDropdownOpen) dismissWorkflowAutocomplete(true, false);
    if (hashDropdownOpen) dismissHashAutocomplete(true, false);
  }, [
    dismissMentionAutocomplete,
    dismissSkillAutocomplete,
    dismissHashAutocomplete,
    hashDropdownOpen,
    mentionDropdownOpen,
    skillDropdownOpen,
    workflowDropdownOpen,
    dismissWorkflowAutocomplete,
  ]);

  const canSend =
    value.trim().length > 0 ||
    attachedFiles.length > 0 ||
    selectedManualSkills.length > 0 ||
    selectedWorkflowIsInline;
  const inlineKnowledgeRefs = attachedFiles.filter(
    (item) =>
      item.type === "knowledge" && hasInlineToken(value, `#${item.name}`),
  );
  const inlineMentionRefs = selectedMentions.filter((mention) =>
    hasInlineToken(value, `@${mention.name}`),
  );
  const inlineSkillRefs = selectedManualSkills.filter((skill) =>
    hasInlineToken(value, manualSkillToken(skill)),
  );
  const inlineWorkflowRef =
    selectedWorkflow && selectedWorkflowIsInline ? selectedWorkflow : null;
  const inlineCardParts = (() => {
    type InlinePart =
      | { kind: "text"; text: string; key: string }
      | { kind: "mention"; token: string; mention: MentionOption; key: string }
      | { kind: "document"; token: string; item: AttachedItem; key: string }
      | { kind: "skill"; token: string; skill: ManualSkillItem; key: string }
      | { kind: "workflow"; token: string; workflow: WorkflowInvokeItem; key: string };
    const matches: Array<{ start: number; end: number; part: InlinePart }> = [];
    inlineMentionRefs.forEach((mention) => {
      const token = `@${mention.name}`;
      findInlineTokenMatches(value, token).forEach(({ start, end }, count) => {
        matches.push({
          start,
          end,
          part: {
            kind: "mention",
            token,
            mention,
            key: `mention-${mention.type}-${mention.id}-${count}`,
          },
        });
      });
    });
    inlineKnowledgeRefs.forEach((item) => {
      const token = `#${item.name}`;
      findInlineTokenMatches(value, token).forEach(({ start, end }, count) => {
        matches.push({
          start,
          end,
          part: {
            kind: "document",
            token,
            item,
            key: `document-${item.id || item.name}-${count}`,
          },
        });
      });
    });
    inlineSkillRefs.forEach((skill) => {
      const token = manualSkillToken(skill);
      findInlineTokenMatches(value, token).forEach(({ start, end }, count) => {
        matches.push({
          start,
          end,
          part: {
            kind: "skill",
            token,
            skill,
            key: `skill-${skill.id}-${count}`,
          },
        });
      });
    });
    if (inlineWorkflowRef) {
      const token = workflowInvokeToken(inlineWorkflowRef);
      findInlineTokenMatches(value, token).forEach(({ start, end }, count) => {
        matches.push({
          start,
          end,
          part: {
            kind: "workflow",
            token,
            workflow: inlineWorkflowRef,
            key: `workflow-${inlineWorkflowRef.bindingId}-${count}`,
          },
        });
      });
    }

    const parts: InlinePart[] = [];
    let cursor = 0;
    matches
      .sort((a, b) => a.start - b.start || b.end - a.end)
      .forEach((match, index) => {
        if (match.start < cursor) return;
        if (match.start > cursor) {
          parts.push({
            kind: "text",
            text: value.slice(cursor, match.start),
            key: `text-${index}-${cursor}`,
          });
        }
        parts.push(match.part);
        cursor = match.end;
      });
    if (cursor < value.length) {
      parts.push({
        kind: "text",
        text: value.slice(cursor),
        key: `text-tail-${cursor}`,
      });
    }
    return parts;
  })();
  const hasInlineCards = inlineCardParts.some((part) => part.kind !== "text");

  useLayoutEffect(() => {
    const root = editorRef.current;
    if (!root) return;
    const cursor = pendingCursorRef.current;
    if (focused && cursor == null && value === lastNativeValueRef.current)
      return;
    inlineThumbnailUrlsRef.current.forEach(revokeObjectUrl);
    inlineThumbnailUrlsRef.current = [];
    inlineAvatarRootsRef.current.forEach((avatarRoot) => avatarRoot.unmount());
    inlineAvatarRootsRef.current = [];
    let cancelled = false;

    const makeTokenNode = (part: (typeof inlineCardParts)[number]) => {
      if (part.kind === "text") return document.createTextNode(part.text);
      const token = document.createElement("span");
      token.contentEditable = "false";
      token.dataset.token = part.token;
      token.className =
        part.kind === "mention"
          ? `chat-composer-inline-token chat-composer-inline-token--mention chat-composer-inline-token--${part.mention.type}`
          : part.kind === "document"
            ? "chat-composer-inline-token chat-composer-inline-token--document"
            : part.kind === "skill"
              ? "chat-composer-inline-token chat-composer-inline-token--skill"
              : "chat-composer-inline-token chat-composer-inline-token--workflow";

      const badge = document.createElement("span");
      badge.className =
        part.kind === "mention"
          ? "chat-composer-inline-avatar"
          : "chat-composer-inline-file-icon";
      const main = document.createElement("span");
      main.className = "chat-composer-inline-main";
      const strong = document.createElement("strong");
      const small = document.createElement("small");

      if (part.kind === "mention") {
        const prefix = document.createElement("span");
        prefix.className = "chat-composer-inline-mention-prefix";
        prefix.textContent = "@";
        token.dataset.mentionId = part.mention.id;
        token.dataset.mentionType = part.mention.type;
        token.dataset.mentionName = part.mention.name;
        const avatarRoot = createRoot(badge);
        avatarRoot.render(
          <UserAvatar
            name={part.mention.name}
            avatarUrl={part.mention.avatarUrl}
            type={part.mention.type}
            seed={part.mention.avatarSeed || part.mention.id}
            size={18}
          />,
        );
        inlineAvatarRootsRef.current.push(avatarRoot);
        strong.textContent = part.mention.name;
        main.append(strong);
        token.append(prefix, badge, main);
      } else if (part.kind === "document") {
        token.dataset.documentId = part.item.id || "";
        token.dataset.documentName = part.item.name;
        token.dataset.documentFileType = part.item.fileType || "";
        token.dataset.documentMimeType = part.item.mimeType || "";
        const refKind = inferComposerReferenceKind(part.item);
        badge.className = `chat-composer-inline-file-icon chat-composer-inline-file-icon--${refKind}`;
        badge.textContent = composerReferenceBadge(part.item, refKind).slice(0, 5);
        if (refKind === "image" || refKind === "video") {
          const load = (() => {
            if (part.item.file && refKind === "image") return Promise.resolve(URL.createObjectURL(part.item.file));
            if (!part.item.id) return null;
            return refKind === "image"
              ? api.documents.imageThumbnail(part.item.id, { cache: true })
              : api.documents.videoThumbnail(part.item.id, { cache: true });
          })();
          if (!load) return token;
          load
            .then((url) => {
              if (cancelled || !badge.isConnected) {
                revokeObjectUrl(url);
                return;
              }
              inlineThumbnailUrlsRef.current.push(url);
              badge.textContent = "";
              const img = document.createElement("img");
              img.src = url;
              img.alt = "";
              badge.appendChild(img);
            })
            .catch(() => {});
        }
        strong.textContent = `#${part.item.name}`;
        small.textContent =
          part.item.mimeType || part.item.fileType || "knowledge";
      } else if (part.kind === "skill") {
        token.dataset.skillId = part.skill.id;
        token.dataset.skillSlug = part.skill.slug || "";
        token.dataset.skillName = part.skill.name;
        badge.textContent = "SK";
        strong.textContent = part.token;
        small.textContent = part.skill.category || part.skill.type || "skill";
      } else {
        token.dataset.workflowBindingId = part.workflow.bindingId;
        token.dataset.workflowId = part.workflow.workflowId;
        badge.textContent = "FL";
        strong.textContent = part.token;
        small.textContent = t("nav.flows");
      }

      if (part.kind === "mention") return token;
      main.append(strong, small);
      token.append(badge, main);
      return token;
    };

    syncingEditorRef.current = true;
    root.replaceChildren(...inlineCardParts.map(makeTokenNode));
    syncingEditorRef.current = false;
    lastNativeValueRef.current = value;
    if (cursor != null) setPlainOffset(root, cursor);
    pendingCursorRef.current = null;
    return () => {
      cancelled = true;
    };
  }, [focused, inlineCardParts, value]);

  const attachMenuPortal =
    attachMenuOpen && attachMenuCoords && typeof document !== "undefined"
      ? createPortal(
          <div
            ref={attachMenuPortalRef}
            className="chat-composer-menu chat-composer-menu--capabilities chat-composer-menu--portal"
            style={portalMenuStyle(attachMenuCoords)}
          >
            <button
              onClick={() => {
                setAttachMenuOpen(false);
                setKbPickerOpen(true);
              }}
              className="chat-composer-menu-item"
              type="button"
            >
              <IconDocument size={16} style={{ color: "#4869ac" }} />
              <span>{t("component.chat_input_footer.add_from_knowledge_base")}</span>
            </button>
            <button
              onClick={() => {
                setAttachMenuOpen(false);
                fileInputRef.current?.click();
              }}
              className="chat-composer-menu-item"
              type="button"
            >
              <IconUpload size={16} style={{ color: "#78716c" }} />
              <span>{t("component.chat_input_footer.add_from_local_files")}</span>
            </button>
          </div>,
          document.body,
        )
      : null;

  const integrationsMenuPortal =
    integrationsMenuOpen &&
    integrationsMenuCoords &&
    typeof document !== "undefined"
      ? createPortal(
          <div
            ref={integrationsMenuPortalRef}
            className="chat-composer-menu chat-composer-menu--integrations chat-composer-menu--portal"
            style={portalMenuStyle(integrationsMenuCoords)}
          >
            <button
              onClick={() => {
                setIntegrationsMenuOpen(false);
                navigate("/integrations");
              }}
              className="chat-composer-menu-item chat-composer-menu-item--header"
              type="button"
            >
              <IconPlus size={16} />
              <span>{t("component.chat_input_footer.add_connectors")}</span>
            </button>
            <div className="chat-composer-menu-divider" />
            {integrationsLoading && (
              <div className="chat-composer-menu-empty" style={{ textAlign: "left" }}>
                <InlineRowsSkeleton rows={3} dense />
              </div>
            )}
            {integrationsError && (
              <button
                type="button"
                className="chat-composer-menu-item"
                onClick={() => void refetchIntegrations()}
              >
                {t("page.integrations.failed_to_load_integrations")} · {t("page.dashboard.retry")}
              </button>
            )}
            {!integrationsLoading &&
              composerIntegrationServers.slice(0, 8).map((server: any) => (
                <button
                  key={server.server_key}
                  onClick={() => handleConnectorClick(server)}
                  className="chat-composer-connector"
                  type="button"
                >
                  <ComposerIntegrationLogo server={server} />
                  <span className="chat-composer-connector-main">
                    <strong>{server.name}</strong>
                    {server.tagline || server.description ? (
                      <small>{server.tagline || server.description}</small>
                    ) : null}
                  </span>
                  <span className="chat-composer-connector-action">
                    {connectorAccessState(server) === "usable"
                      ? t("component.chat_input_footer.use_connector")
                      : connectorAccessState(server) === "repair"
                      ? t("page.integrations.needs_attention")
                      : t("page.apps.connect")}
                  </span>
                </button>
              ))}
            {!integrationsLoading && !integrationsError &&
              composerIntegrationServers.length === 0 && (
                <div className="chat-composer-menu-empty">
                  {t("component.chat_input_footer.no_ready_auth_connectors")}</div>
              )}
          </div>,
          document.body,
        )
      : null;

  const kbPickerPortal =
    kbPickerOpen && kbPickerCoords && typeof document !== "undefined"
      ? createPortal(
          <div
            ref={kbPickerPortalRef}
            className="chat-composer-menu chat-composer-menu--knowledge chat-composer-menu--portal"
            style={portalMenuStyle(kbPickerCoords)}
            role="dialog"
            aria-label={t("component.chat_input_footer.add_from_knowledge_base")}
          >
            <div className="chat-composer-knowledge-search">
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.7}
                aria-hidden="true"
              >
                <circle cx="11" cy="11" r="8" />
                <path d="M21 21l-4.35-4.35" />
              </svg>
              <input
                value={kbSearch}
                onChange={(e) => setKbSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key !== "Escape") return;
                  setKbPickerOpen(false);
                  setKbSearch("");
                  attachMenuButtonRef.current?.focus();
                }}
                placeholder={t("component.chat_input_footer.search_knowledge_base")}
                aria-label={t("component.chat_input_footer.search_knowledge_base")}
                autoFocus
              />
              <button
                onClick={() => {
                  setKbPickerOpen(false);
                  setKbSearch("");
                  attachMenuButtonRef.current?.focus();
                }}
                className="chat-composer-autocomplete-close"
                type="button"
                aria-label={t("action.close")}
                title={t("action.close")}
              >
                <IconClose size={12} />
              </button>
            </div>
            <div className="chat-composer-knowledge-list">
              {kbDocsLoading ? (
                <div className="chat-composer-hash-empty chat-composer-knowledge-loading">
                  <InlineRowsSkeleton rows={4} dense />
                </div>
              ) : (kbDocs?.items || []).length === 0 ? (
                <div className="chat-composer-hash-empty">
                  {t("component.chat_input_footer.no_documents_found")}
                </div>
              ) : null}
              {!kbDocsLoading &&
                (kbDocs?.items || []).map((doc: ComposerDocumentOption) => {
                  const ext = (
                    doc.file_type ||
                    doc.name?.split(".").pop() ||
                    ""
                  ).toUpperCase();
                  const alreadyAttached = attachedFiles.some(
                    (file) => file.id === doc.id,
                  );
                  return (
                    <button
                      key={doc.id}
                      onClick={() => addKbDoc(doc)}
                      disabled={alreadyAttached}
                      className="chat-composer-hash-item chat-composer-knowledge-item"
                      type="button"
                      aria-label={doc.name}
                    >
                      <ComposerReferenceThumbnail
                        item={composerPreviewItemFromDoc(doc)}
                        className="chat-composer-hash-thumb"
                      />
                      <span className="chat-composer-hash-name">
                        {doc.name}
                      </span>
                      <span className="chat-composer-hash-ext">
                        {ext.slice(0, 4) || "?"}
                      </span>
                    </button>
                  );
                })}
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <>
      {attachMenuPortal}
      {integrationsMenuPortal}
      {kbPickerPortal}

      <div
        className={className || "embedded-chat-footer"}
        style={{ position: "relative" }}
      >
        {topSlot}

        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="*/*"
          style={{ display: "none" }}
          onChange={handleFileSelect}
        />

        <div
          ref={composerRef}
          className={`chat-composer ${focused ? "chat-composer--focused" : ""} ${streaming ? "chat-composer--streaming" : ""}`}
        >
          {attachedFiles.length > 0 && (
            <div className="chat-composer-attachments">
              {attachedFiles.map((f, i) => (
                <span
                  key={i}
                  className="chat-composer-chip chat-composer-chip--file"
                >
                  <svg
                    width="12"
                    height="12"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.8}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32m.009-.01l-.01.01m5.699-9.941l-7.81 7.81a1.5 1.5 0 002.112 2.13"
                    />
                  </svg>
                  <span>
                    {f.name.length > 32 ? f.name.slice(0, 30) + "..." : f.name}
                  </span>
                  <button
                    onClick={() => removeAttachment(i)}
                    type="button"
                    aria-label={`Remove ${f.name}`}
                  >
                    <svg
                      width="8"
                      height="8"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={3}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M6 18L18 6M6 6l12 12"
                      />
                    </svg>
                  </button>
                </span>
              ))}
            </div>
          )}

          {/* Textarea + # autocomplete */}
          <div className="chat-composer-input-wrap">
            {mentionDropdownOpen && (
              <div className="chat-composer-mention-menu">
                <div className="chat-composer-hash-title">
                  <span>{t("component.chat_input_footer.mention")}</span>
                  <button
                    type="button"
                    className="chat-composer-autocomplete-close"
                    aria-label={t("component.chat_input_footer.cancel_mention")}
                    title={t("component.chat_input_footer.cancel_mention")}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => dismissMentionAutocomplete(true)}
                  >
                    <IconClose size={12} />
                  </button>
                </div>
                {mentionFiltered.length === 0 ? (
                  <div className="chat-composer-hash-empty">
                    {t("component.chat_input_footer.no_matching_people_or_agents")}</div>
                ) : (
                  mentionFiltered.map((mention, idx) => (
                    <button
                      key={`${mention.type}:${mention.id}`}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => selectMention(mention)}
                      onMouseEnter={() => setMentionActiveIdx(idx)}
                      className={`chat-composer-mention-item ${idx === mentionActiveIdx ? "active" : ""}`}
                      type="button"
                    >
                      <span
                        className={`chat-composer-mention-avatar chat-composer-mention-avatar--${mention.type}`}
                      >
                        {mention.type === "agent" ? (
                          <UserAvatar
                            name={mention.name}
                            avatarUrl={mention.avatarUrl}
                            type="agent"
                            seed={mention.avatarSeed || mention.id}
                            size={28}
                          />
                        ) : mention.avatarUrl ? (
                          <img src={mention.avatarUrl} alt="" />
                        ) : (
                          mention.name.charAt(0).toUpperCase()
                        )}
                      </span>
                      <span className="chat-composer-mention-main">
                        <strong>{mention.name}</strong>
                        <small>
                          {mention.subtitle ||
                            (mention.type === "agent"
                              ? t("component.chat_input_footer.route_this_message_to_an_agent")
                              : t("component.chat_input_footer.reference_this_teammate"))}
                        </small>
                      </span>
                      <span className="chat-composer-mention-type">
                        {mention.type === "agent" ? t("page.workspace_detail.agent") : t("component.chat_input_footer.person")}
                      </span>
                    </button>
                  ))
                )}
              </div>
            )}
            {skillDropdownOpen && (
              <div className="chat-composer-mention-menu chat-composer-skill-menu">
                <div className="chat-composer-hash-title">
                  <span>{t("nav.skills")}</span>
                  <button
                    type="button"
                    className="chat-composer-autocomplete-close"
                    aria-label={t("component.chat_input_footer.cancel_skill")}
                    title={t("component.chat_input_footer.cancel_skill")}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => dismissSkillAutocomplete(true)}
                  >
                    <IconClose size={12} />
                  </button>
                </div>
                {skillsLoading ? (
                  <div className="chat-composer-hash-empty" style={{ textAlign: "left" }}>
                    <InlineRowsSkeleton rows={4} dense />
                  </div>
                ) : skillFiltered.length === 0 ? (
                  <div className="chat-composer-hash-empty">
                    {skillQuery ? t("component.chat_input_footer.no_matching_skills") : t("component.chat_input_footer.no_skills_available")}
                  </div>
                ) : (
                  skillFiltered.map((skill: any, idx: number) => {
                    const description = getSkillDescription(skill);
                    return (
                      <button
                        key={skill.id}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => selectManualSkill(skill)}
                        onMouseEnter={() => setSkillActiveIdx(idx)}
                        className={`chat-composer-mention-item ${idx === skillActiveIdx ? "active" : ""}`}
                        type="button"
                      >
                        <span className="chat-composer-mention-avatar chat-composer-mention-avatar--skill">
                          <IconSkill size={14} />
                        </span>
                        <span className="chat-composer-mention-main">
                          <strong>
                            {skill.display_name ||
                              skill.displayName ||
                              skill.name ||
                              skill.slug}
                          </strong>
                          <small>
                            {description ||
                              skill.slug ||
                              t("component.chat_input_footer.run_this_skill_for_the_next_message")}
                          </small>
                        </span>
                        <span className="chat-composer-mention-type">
                          {skill.category || t("page.skills.skill")}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
            {workflowDropdownOpen && (
              <div className="chat-composer-mention-menu chat-composer-workflow-menu">
                <div className="chat-composer-hash-title">
                  <span>
                    {t("nav.flows")}
                    {flowsComingSoon
                      ? ` · ${t("component.chat_mode.soon")}`
                      : ""}
                  </span>
                  <button
                    type="button"
                    className="chat-composer-autocomplete-close"
                    aria-label={t("component.chat_input_footer.cancel_flow")}
                    title={t("component.chat_input_footer.cancel_flow")}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => dismissWorkflowAutocomplete(true)}
                  >
                    <IconClose size={12} />
                  </button>
                </div>
                {workflowFiltered.length === 0 ? (
                  <div className="chat-composer-hash-empty">
                    {workflowQuery
                      ? t("component.chat_input_footer.no_matching_flows")
                      : t("component.chat_input_footer.no_flows_available")}
                  </div>
                ) : (
                  workflowFiltered.map((workflow, idx) => (
                    <button
                      key={workflow.bindingId}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => selectWorkflow(workflow)}
                      onMouseEnter={() => setWorkflowActiveIdx(idx)}
                      className={`chat-composer-mention-item ${idx === workflowActiveIdx ? "active" : ""}`}
                      type="button"
                      disabled={!flowsAvailable}
                      aria-disabled={!flowsAvailable}
                      title={
                        flowsComingSoon
                          ? t("component.chat_mode.flows_coming_soon")
                          : undefined
                      }
                    >
                      <span className="chat-composer-mention-avatar chat-composer-mention-avatar--workflow">
                        <IconFlow size={14} />
                      </span>
                      <span className="chat-composer-mention-main">
                        <strong>{workflow.title}</strong>
                        <small>
                          {workflow.description ||
                            t("component.chat_input_footer.run_this_flow_for_the_next_message")}
                        </small>
                      </span>
                      <span className="chat-composer-mention-type">
                        {flowsComingSoon
                          ? t("component.chat_mode.soon")
                          : "%"}
                      </span>
                    </button>
                  ))
                )}
              </div>
            )}
            {hashDropdownOpen && (
              <div className="chat-composer-hash-menu">
                <div className="chat-composer-hash-title">
                  <span>{t("component.chat_input_footer.files_and_documents")}</span>
                  <button
                    type="button"
                    className="chat-composer-autocomplete-close"
                    aria-label={t("action.close")}
                    title={t("action.close")}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => dismissHashAutocomplete(true)}
                  >
                    <IconClose size={12} />
                  </button>
                </div>
                {hashFiltered.length === 0 ? (
                  <div className="chat-composer-hash-empty">
                    {hashQuery
                      ? t("component.chat_input_footer.no_matching_files")
                      : t("component.chat_input_footer.type_to_search_files")}
                  </div>
                ) : (
                  hashFiltered.map((doc: any, idx: number) => {
                    const ext = (doc.file_type || "?")
                      .toUpperCase()
                      .slice(0, 4);
                    const previewItem = composerPreviewItemFromDoc(doc);
                    return (
                      <button
                        key={doc.id}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => selectHashDoc(doc)}
                        onMouseEnter={() => setHashActiveIdx(idx)}
                        className={`chat-composer-hash-item ${idx === hashActiveIdx ? "active" : ""}`}
                        type="button"
                      >
                        <ComposerReferenceThumbnail
                          item={previewItem}
                          className="chat-composer-hash-thumb"
                        />
                        <span className="chat-composer-hash-name">
                          {doc.name}
                        </span>
                        <span className="chat-composer-hash-ext">{ext}</span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
            <div
              ref={editorRef}
              role="textbox"
              aria-multiline="true"
              contentEditable={!streaming && !disabled}
              aria-disabled={disabled || streaming}
              suppressContentEditableWarning
              data-placeholder={
                voice.phase === "recording"
                  ? t("component.chat_input_footer.speak_now")
                  : placeholder || t("component.chat_input_footer.message_placeholder")
              }
              onBeforeInput={handleEditorBeforeInput}
              onInput={handleEditorInput}
              onPaste={handleEditorPaste}
              onKeyDown={handleKeyDown}
              onFocus={() => setFocused(true)}
              onBlur={handleEditorBlur}
              className={`chat-composer-rich-editor ${hasInlineCards ? "chat-composer-rich-editor--has-inline-cards" : ""}`}
            />
          </div>

          <VoiceInputStatus voice={voice} />
          <div
            className={`chat-composer-row ${modeSlot ? "chat-composer-row--mode-aware" : ""}`}
          >
            {modeSlot}

            {!replaceActionButtons ? (
              <>
            {/* Attach */}
            <div style={{ position: "relative" }} ref={attachMenuRef}>
              <button
                ref={attachMenuButtonRef}
                onClick={() => {
                  setKbPickerOpen(false);
                  setKbSearch("");
                  setAttachMenuOpen(!attachMenuOpen);
                }}
                disabled={streaming || disabled}
                title={
                  attachmentButtonIcon === "paperclip"
                    ? t("page.task_detail.attachments")
                    : t("component.chat_input_footer.add_context_or_tools")
                }
                aria-label={
                  attachmentButtonIcon === "paperclip"
                    ? t("page.task_detail.attachments")
                    : t("component.chat_input_footer.add_context_or_tools")
                }
                aria-haspopup="menu"
                aria-expanded={attachMenuOpen || kbPickerOpen}
                className={`chat-composer-icon-btn ${attachMenuOpen ? "chat-composer-icon-btn--active" : ""}`}
                type="button"
              >
                {attachmentButtonIcon === "paperclip" ? (
                  <svg
                    width="18"
                    height="18"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.8}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M21.44 11.05 12.25 20.24a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 1 1-2.83-2.83l8.49-8.48" />
                  </svg>
                ) : (
                  <IconPlus size={18} />
                )}
              </button>
            </div>

            {/* Integrations */}
            <div style={{ position: "relative" }} ref={integrationsMenuRef}>
              <button
                ref={integrationsMenuButtonRef}
                onClick={() => setIntegrationsMenuOpen(!integrationsMenuOpen)}
                disabled={streaming || disabled}
                title={t("component.chat_input_footer.connectors")}
                className={`chat-composer-icon-btn ${integrationsMenuOpen ? "chat-composer-icon-btn--active" : ""}`}
                type="button"
              >
                <IconConnection size={18} />
              </button>
            </div>

            <button
              onClick={() => insertComposerHint(t("component.chat_input_footer.create_task_hint"))}
              disabled={streaming || disabled}
              title={t("component.chat_input_footer.create_task")}
              className="chat-composer-icon-btn"
              type="button"
            >
              <IconChecklist size={17} />
            </button>

            <button
              onClick={openSkillPicker}
              disabled={streaming || disabled}
              title={t("component.chat_input_footer.use_skill")}
              className={`chat-composer-icon-btn ${skillDropdownOpen ? "chat-composer-icon-btn--active" : ""}`}
              type="button"
            >
              <IconSkill size={17} />
            </button>

            <VoiceInputButton voice={voice} disabled={streaming || disabled} />
            {voiceScope && <LiveChatCallButton scope={voiceScope} disabled={streaming || disabled || voice.busy} onConversation={onVoiceConversation} />}
              </>
            ) : null}

            {beforeTextarea}

            {/* Send / Stop */}
            {streaming && showStopButton ? (
              <button
                onClick={onStop}
                disabled={disabled}
                title={t("component.chat_input_footer.stop_generating")}
                className="chat-composer-send chat-composer-send--stop"
                type="button"
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                >
                  <rect x="4" y="4" width="16" height="16" rx="2" />
                </svg>
              </button>
            ) : (
              <button
                onClick={triggerSend}
                disabled={disabled || streaming || voice.busy || !canSend}
                className="chat-composer-send"
                type="button"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M22 2L11 13" />
                  <path d="M22 2l-7 20-4-9-9-4 20-7z" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>

      {nangoConnector?.nango_provider_config_key && (
        <Modal
          open
          onClose={() => setNangoConnector(null)}
          title={nangoConnector.name}
          restoreFocusFallback={() => integrationsMenuButtonRef.current?.focus()}
        >
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-3">
              <IntegrationLogo provider={nangoConnector.server_key} size={32} />
              <p className="m-0 text-sm text-stone-600">
                {nangoConnector.description || nangoConnector.tagline || nangoConnector.setup_hint}
              </p>
            </div>
            <NangoConnectButton
              providerConfigKeys={[nangoConnector.nango_provider_config_key]}
              label={t("page.apps.connect")}
              onConnected={() => setNangoConnector(null)}
            />
          </div>
        </Modal>
      )}
    </>
  );
}
