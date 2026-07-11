/**
 * ChatInputFooter — shared chat composer footer.
 *
 * Owns: attached files (local + KB), attach menu, voice input
 * (SpeechRecognition), # autocomplete, the textarea, send/stop.
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
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useAuthStore } from "../stores/auth";
import UserAvatar from "./ui/UserAvatar";
import { type ChatMessage, useDebounced } from "../lib/chatStream";
import { getSkillDescription } from "../pages/skills/skillTypes";
import { shouldHandleComposerEnter } from "../lib/composerKeyboard";
import {
  IconCalendar,
  IconChat,
  IconChecklist,
  IconClose,
  IconConnection,
  IconDocument,
  IconDollar,
  IconEmail,
  IconFacebook,
  IconYouTube,
  IconTikTok,
  IconShoppingCart,
  IconStore,
  IconBox,
  IconFolder,
  IconGitHub,
  IconLinkedIn,
  IconPayPal,
  IconPlus,
  IconSlack,
  IconSkill,
  IconStripe,
  IconTelegram,
  IconTwilio,
  IconTwitter,
  IconUpload,
  IconWebhook,
  IconWeChat,
  IconWhatsApp,
  IconCloud,
  IconExcelGrid,
  type IconProps,
} from "./icons";
import { t } from "../lib/i18n";


export interface AttachedItem {
  name: string;
  id?: string;
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
  file_type?: string | null;
  mime_type?: string | null;
};

function composerPreviewItemFromDoc(doc: ComposerDocumentOption): AttachedItem {
  return {
    name: doc.name,
    id: doc.id,
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
    id: item.id,
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
}

export interface ManualSkillItem {
  id: string;
  name: string;
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

const COMPOSER_INTEGRATION_LOGO_COLOR: Record<string, string> = {
  gmail: "#EA4335",
  google_calendar: "#4285F4",
  google_drive: "#0F9D58",
  slack: "#4A154B",
  discord: "#5865F2",
  telegram: "#229ED9",
  wechat_personal: "#07C160",
  wechat_official: "#07C160",
  whatsapp: "#25D366",
  twilio: "#F22F46",
  linkedin: "#0A66C2",
  twitter_x: "#111111",
  github: "#181717",
  webhook: "#57534e",
  quickbooks: "#2CA01C",
  stripe: "#635BFF",
  paypal: "#003087",
  facebook: "#1877F2",
  email: "#78716c",
  notion: "#111111",
  // Microsoft 365 — keep aligned with MCP_LOGO_COLOR in Integrations.tsx
  outlook: "#0078D4",
  onedrive: "#0364B8",
  ms_calendar: "#0078D4",
  ms_teams: "#6264A7",
  ms_excel: "#107C41",
};

const COMPOSER_INTEGRATION_ICON: Record<
  string,
  (props: IconProps) => JSX.Element
> = {
  gmail: IconEmail,
  email: IconEmail,
  google_calendar: IconCalendar,
  google_drive: IconFolder,
  notion: IconDocument,
  slack: IconSlack,
  discord: IconChat,
  telegram: IconTelegram,
  wechat_personal: IconWeChat,
  wechat_official: IconWeChat,
  whatsapp: IconWhatsApp,
  twilio: IconTwilio,
  linkedin: IconLinkedIn,
  twitter_x: IconTwitter,
  github: IconGitHub,
  webhook: IconWebhook,
  quickbooks: IconDollar,
  stripe: IconStripe,
  paypal: IconPayPal,
  facebook: IconFacebook,
  youtube: IconYouTube,
  tiktok: IconTikTok,
  shopify: IconShoppingCart,
  woocommerce: IconStore,
  square: IconBox,
  tiktok_shop: IconShoppingCart,
  amazon: IconStore,
  outlook: IconEmail,
  onedrive: IconCloud,
  ms_calendar: IconCalendar,
  ms_teams: IconChat,
  ms_excel: IconExcelGrid,
};

function getComposerIntegrationColor(serverKey?: string | null) {
  return serverKey
    ? COMPOSER_INTEGRATION_LOGO_COLOR[serverKey] || "#78716c"
    : "#78716c";
}

function ComposerIntegrationLogo({ server }: { server: any }) {
  const Icon = COMPOSER_INTEGRATION_ICON[server.server_key];
  const color = getComposerIntegrationColor(server.server_key);
  return (
    <span className="chat-composer-connector-icon" style={{ color }}>
      {Icon ? <Icon size={16} /> : <IconConnection size={15} />}
    </span>
  );
}

interface ChatInputFooterProps {
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
  selectedMentions?: MentionOption[];
  onMentionSelect?: (mention: MentionOption) => void;
  onMentionRemove?: (mention: MentionOption) => void;
  textareaRef?: React.RefObject<HTMLTextAreaElement>;
  seedAttachments?: AttachedItem[];
  seedAttachmentsKey?: string;
  /** Optional className for the outer footer wrapper. */
  className?: string;
}

/* ── SpeechRecognition shim ── */
type SpeechRecognitionInstance = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  onresult: ((e: any) => void) | null;
  onerror: ((e: any) => void) | null;
  onend: (() => void) | null;
};
function getSpeechRecognition(): SpeechRecognitionInstance | null {
  const SR =
    (window as any).SpeechRecognition ||
    (window as any).webkitSpeechRecognition;
  if (!SR) return null;
  return new SR() as SpeechRecognitionInstance;
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

type ComposerTrigger = "@" | "#" | "/";

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
  value,
  onChange,
  onKeyDown,
  enterToSend = false,
  streaming,
  onSend,
  onStop,
  placeholder,
  disabled = false,
  showStopButton = true,
  topSlot,
  modeSlot,
  replaceActionButtons = false,
  beforeTextarea,
  mentions = [],
  selectedMentions = [],
  onMentionSelect,
  onMentionRemove,
  textareaRef: externalTextareaRef,
  seedAttachments,
  seedAttachmentsKey,
  className,
}: ChatInputFooterProps) {
  const queryClient = useQueryClient();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const editorRef = useRef<HTMLDivElement>(null);
  const internalTextareaRef = useRef<HTMLTextAreaElement>(null);
  const textareaRef = externalTextareaRef || internalTextareaRef;
  const pendingCursorRef = useRef<number | null>(null);
  const pendingHashTriggerPosRef = useRef<number | null>(null);
  const pendingMentionTriggerPosRef = useRef<number | null>(null);
  const pendingSkillTriggerPosRef = useRef<number | null>(null);
  const appliedSeedAttachmentsKeyRef = useRef<string | undefined>();
  const lastNativeValueRef = useRef(value);
  const sendLockedRef = useRef(false);
  const streamingRef = useRef(streaming);
  const syncingEditorRef = useRef(false);
  const selectedKnowledgeNamesRef = useRef<Set<string>>(new Set());
  const inlineThumbnailUrlsRef = useRef<string[]>([]);
  const [selectedManualSkills, setSelectedManualSkills] = useState<
    ManualSkillItem[]
  >([]);

  const attachedFilesRef = useRef<AttachedItem[]>([]);
  const [attachedFiles, setAttachedFilesState] = useState<AttachedItem[]>([]);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [integrationsMenuOpen, setIntegrationsMenuOpen] = useState(false);
  const [selectedConnector, setSelectedConnector] = useState<any | null>(null);
  const [connectorConnecting, setConnectorConnecting] = useState(false);
  const [connectorError, setConnectorError] = useState("");
  const [kbPickerOpen, setKbPickerOpen] = useState(false);
  const [kbSearch, setKbSearch] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const integrationsMenuRef = useRef<HTMLDivElement>(null);
  const attachMenuButtonRef = useRef<HTMLButtonElement>(null);
  const integrationsMenuButtonRef = useRef<HTMLButtonElement>(null);
  const attachMenuPortalRef = useRef<HTMLDivElement>(null);
  const integrationsMenuPortalRef = useRef<HTMLDivElement>(null);
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

  const [listening, setListening] = useState(false);
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
    };
  }, []);
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);

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
  }, [attachMenuOpen, getPortalMenuCoords, integrationsMenuOpen]);

  useLayoutEffect(() => {
    if (!attachMenuOpen && !integrationsMenuOpen) return;
    updatePortalMenuCoords();
    window.addEventListener("resize", updatePortalMenuCoords);
    window.addEventListener("scroll", updatePortalMenuCoords, true);
    return () => {
      window.removeEventListener("resize", updatePortalMenuCoords);
      window.removeEventListener("scroll", updatePortalMenuCoords, true);
    };
  }, [attachMenuOpen, integrationsMenuOpen, updatePortalMenuCoords]);

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
    selectedKnowledgeNamesRef.current = new Set(
      attachedFiles
        .filter(
          (item) =>
            item.type === "knowledge" && hasInlineToken(value, `#${item.name}`),
        )
        .map((item) => item.name),
    );
  }, [attachedFiles, value]);

  useEffect(() => {
    setSelectedManualSkills((prev) =>
      prev.filter((skill) => hasInlineToken(value, manualSkillToken(skill))),
    );
  }, [value]);

  /* Close composer menus on outside click */
  useEffect(() => {
    if (!attachMenuOpen && !integrationsMenuOpen) return;
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
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [attachMenuOpen, integrationsMenuOpen]);

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
  const { data: kbDocs } = useQuery({
    queryKey: ["documents", "kb-picker-shared", kbSearch],
    queryFn: () =>
      api.documents.list({
        search: kbSearch || undefined,
        limit: 20,
      }),
    enabled: kbPickerOpen,
  });

  const { data: integrationServers, isLoading: integrationsLoading } = useQuery(
    {
      queryKey: ["composer-integrations"],
      queryFn: () => api.integrations.mcpServers(),
      enabled: privateApiEnabled && integrationsMenuOpen,
    },
  );
  const readyAuthIntegrationServers = useMemo(() => {
    return (integrationServers || []).filter((server: any) => {
      const connected = Boolean(
        server.user_connected ||
        server.entity_connected ||
        server.agent_can_use ||
        server.connections?.length ||
        server.entity_accounts?.length,
      );
      const readyToAuth = Boolean(
        server.nango_provider_config_key ||
        (server.auth_type === "oauth2" && server.oauth_configured),
      );
      const hasKnownLogo = Boolean(
        COMPOSER_INTEGRATION_ICON[server.server_key],
      );
      return !connected && readyToAuth && !server.coming_soon && hasKnownLogo;
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
        ...Array.from(selectedKnowledgeNamesRef.current).map(
          (name) => `#${name}`,
        ),
        ...selectedManualSkills.map((skill) => manualSkillToken(skill)),
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
        }
      } else {
        setSkillDropdownOpen(false);
        skillReplaceEndRef.current = -1;
      }
    },
    [mentions.length, onChange, selectedManualSkills, selectedMentions],
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
      const before = value.substring(0, mentionTriggerPos);
      const replaceEnd =
        mentionReplaceEndRef.current > mentionTriggerPos
          ? mentionReplaceEndRef.current
          : Math.max(mentionTriggerPos + 1, getPlainOffset(editorRef.current));
      const after = value.substring(replaceEnd);
      const token = `@${mention.name}`;
      const spacer = after.startsWith(" ") || after.startsWith("\n") ? "" : " ";
      onChange(`${before}${token}${spacer}${after}`);
      const nextCursor = before.length + token.length + spacer.length;
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

  const selectHashDoc = useCallback(
    (doc: {
      id: string;
      name: string;
      file_type?: string;
      mime_type?: string;
    }) => {
      const before = value.substring(0, hashTriggerPos);
      const replaceEnd =
        hashReplaceEndRef.current > hashTriggerPos
          ? hashReplaceEndRef.current
          : Math.max(hashTriggerPos + 1, getPlainOffset(editorRef.current));
      const after = value.substring(replaceEnd);
      const token = `#${doc.name}`;
      const spacer = after.startsWith(" ") || after.startsWith("\n") ? "" : " ";
      onChange(`${before}${token}${spacer}${after}`);
      const nextCursor = before.length + token.length + spacer.length;
      pendingCursorRef.current = nextCursor;
      selectedKnowledgeNamesRef.current.add(doc.name);
      setAttachedFiles((prev) =>
        prev.some((f) => f.id === doc.id)
          ? prev
          : [
              ...prev,
              {
                name: doc.name,
                id: doc.id,
                type: "knowledge",
                fileType: (doc as any).file_type,
                mimeType: (doc as any).mime_type,
              },
            ],
      );
      setHashDropdownOpen(false);
      setHashQuery("");
      setHashTriggerPos(-1);
      hashReplaceEndRef.current = -1;
      setHashActiveIdx(0);
      setTimeout(() => {
        editorRef.current?.focus();
        setPlainOffset(editorRef.current, nextCursor);
      }, 0);
    },
    [value, hashTriggerPos, onChange],
  );

  const selectManualSkill = useCallback(
    (rawSkill: any) => {
      const skill: ManualSkillItem = {
        id: rawSkill.id,
        name: rawSkill.name || rawSkill.slug || t("component.chat_input_footer.skill"),
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
    setAttachMenuOpen(false);
    setIntegrationsMenuOpen(false);
    setTimeout(() => editorRef.current?.focus(), 0);
  }, [skillDropdownOpen]);

  const removeAutocompleteTriggerRange = useCallback(
    (
      trigger: "@" | "/",
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
    [],
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
    if (attachedFiles.some((f) => f.id === doc.id)) return;
    setAttachedFiles((prev) => [...prev, composerPreviewItemFromDoc(doc)]);
    setKbPickerOpen(false);
    setKbSearch("");
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

  const startConnectorAuth = useCallback(
    async (connectorOverride?: any) => {
      const connector = connectorOverride || selectedConnector;
      if (!connector || connectorConnecting) return;
      setConnectorConnecting(true);
      setConnectorError("");
      try {
        if (connector.nango_provider_config_key) {
          const { nango_connect_url } =
            await api.integrations.nango.startConnect([
              connector.nango_provider_config_key,
            ]);
          const popup = window.open(
            nango_connect_url,
            "nango_connect",
            "popup=yes,width=560,height=720,scrollbars=yes",
          );
          if (!popup)
            throw new Error(
              t("component.chat_input_footer.popup_blocked"),
            );
          const tick = window.setInterval(async () => {
            if (!popup.closed) return;
            window.clearInterval(tick);
            try {
              await api.integrations.nango.sync();
              queryClient.invalidateQueries({
                queryKey: ["composer-integrations"],
              });
              // Refresh the Integrations page too in case the user has
              // it open in another tab — the page query key is
              // ``["mcp-servers"]`` (the older ``["integrations"]`` key
              // was renamed and is no longer registered).
              queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
              setSelectedConnector(null);
            } catch (err: any) {
              setSelectedConnector(connector);
              setConnectorError(err?.message || t("component.chat_input_footer.could_not_sync_connection"));
            } finally {
              setConnectorConnecting(false);
            }
          }, 600);
          return;
        }

        if (connector.auth_type === "oauth2" && connector.oauth_configured) {
          const { authorize_url } = await api.integrations.oauthStart(
            connector.server_key,
          );
          window.location.href = authorize_url;
          return;
        }

        window.location.href = "/integrations";
      } catch (err: any) {
        setSelectedConnector(connector);
        setConnectorError(err?.message || t("component.chat_input_footer.could_not_start_connection"));
        setConnectorConnecting(false);
      }
    },
    [connectorConnecting, queryClient, selectedConnector],
  );

  const handleConnectorClick = useCallback(
    (server: any) => {
      const connected = Boolean(
        server.user_connected ||
        server.entity_connected ||
        server.agent_can_use ||
        server.connections?.length ||
        server.entity_accounts?.length,
      );
      if (connected) {
        insertComposerHint(`Use ${server.name} to `);
        return;
      }
      setIntegrationsMenuOpen(false);
      startConnectorAuth(server);
    },
    [insertComposerHint, startConnectorAuth],
  );

  const removeAttachment = (idx: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const toggleVoice = useCallback(() => {
    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
      return;
    }
    const recognition = getSpeechRecognition();
    if (!recognition) {
      onChange(value || t("component.chat_input_footer.voice_input_not_supported"));
      return;
    }
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = navigator.language || "en-US";

    let finalTranscript = "";
    recognition.onresult = (e: any) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const transcript = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalTranscript += transcript;
        else interim += transcript;
      }
      // Replace stale interim + append final + new interim
      onChange(finalTranscript + interim || value);
    };
    recognition.onerror = () => setListening(false);
    recognition.onend = () => {
      setListening(false);
      if (finalTranscript) onChange(finalTranscript);
    };
    recognitionRef.current = recognition;
    recognition.start();
    setListening(true);
  }, [listening, value, onChange]);

  const triggerSend = useCallback(() => {
    const text = value.trim();
    const currentAttachments = attachedFilesRef.current;
    const manualSkillSnapshot = selectedManualSkills.filter((skill) =>
      hasInlineToken(value, manualSkillToken(skill)),
    );
    if (
      (!text &&
        currentAttachments.length === 0 &&
        manualSkillSnapshot.length === 0) ||
      streaming ||
      disabled ||
      sendLockedRef.current
    )
      return;
    sendLockedRef.current = true;
    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
    }
    const snapshot = currentAttachments.filter(
      (item) =>
        item.type !== "knowledge" || hasInlineToken(value, `#${item.name}`),
    );
    setAttachedFiles([]);
    setSelectedManualSkills([]);
    onSend(text, snapshot, manualSkillSnapshot);
    window.setTimeout(() => {
      if (!streamingRef.current) {
        sendLockedRef.current = false;
      }
    }, 750);
  }, [
    value,
    streaming,
    selectedManualSkills,
    listening,
    onSend,
    disabled,
    setAttachedFiles,
  ]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (onKeyDown) onKeyDown(e);
      if (e.defaultPrevented) return;

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

      if (hashDropdownOpen && hashFiltered.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setHashActiveIdx((i) => Math.min(i + 1, hashFiltered.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setHashActiveIdx((i) => Math.max(i - 1, 0));
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          selectHashDoc(hashFiltered[hashActiveIdx]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setHashDropdownOpen(false);
          hashReplaceEndRef.current = -1;
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
      onKeyDown,
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
      hashDropdownOpen,
      hashFiltered,
      hashActiveIdx,
      selectHashDoc,
      enterToSend,
      insertPlainTextAtCursor,
      triggerSend,
    ],
  );

  const handleEditorBlur = useCallback(() => {
    setFocused(false);
    if (mentionDropdownOpen) dismissMentionAutocomplete(true, false);
    if (skillDropdownOpen) dismissSkillAutocomplete(true, false);
  }, [
    dismissMentionAutocomplete,
    dismissSkillAutocomplete,
    mentionDropdownOpen,
    skillDropdownOpen,
  ]);

  const canSend =
    value.trim().length > 0 ||
    attachedFiles.length > 0 ||
    selectedManualSkills.length > 0;
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
  const inlineCardParts = (() => {
    type InlinePart =
      | { kind: "text"; text: string; key: string }
      | { kind: "mention"; token: string; mention: MentionOption; key: string }
      | { kind: "document"; token: string; item: AttachedItem; key: string }
      | { kind: "skill"; token: string; skill: ManualSkillItem; key: string };
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
            : "chat-composer-inline-token chat-composer-inline-token--skill";

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
        token.dataset.mentionId = part.mention.id;
        token.dataset.mentionType = part.mention.type;
        token.dataset.mentionName = part.mention.name;
        if (part.mention.avatarUrl) {
          const img = document.createElement("img");
          img.src = part.mention.avatarUrl;
          img.alt = "";
          badge.appendChild(img);
        } else {
          badge.textContent = part.mention.name.charAt(0).toUpperCase();
        }
        strong.textContent = `@${part.mention.name}`;
        small.textContent = part.mention.type;
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
      } else {
        token.dataset.skillId = part.skill.id;
        token.dataset.skillSlug = part.skill.slug || "";
        token.dataset.skillName = part.skill.name;
        badge.textContent = "SK";
        strong.textContent = part.token;
        small.textContent = part.skill.category || part.skill.type || "skill";
      }

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
                window.location.href = "/integrations";
              }}
              className="chat-composer-menu-item chat-composer-menu-item--header"
              type="button"
            >
              <IconPlus size={16} />
              <span>{t("component.chat_input_footer.add_connectors")}</span>
            </button>
            <div className="chat-composer-menu-divider" />
            {integrationsLoading && (
              <div className="chat-composer-menu-empty">
                {t("component.chat_input_footer.loading_connectors")}</div>
            )}
            {!integrationsLoading &&
              readyAuthIntegrationServers.slice(0, 8).map((server: any) => (
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
                    {t("page.apps.connect")}</span>
                </button>
              ))}
            {!integrationsLoading &&
              readyAuthIntegrationServers.length === 0 && (
                <div className="chat-composer-menu-empty">
                  {t("component.chat_input_footer.no_ready_auth_connectors")}</div>
              )}
          </div>,
          document.body,
        )
      : null;

  return (
    <>
      {attachMenuPortal}
      {integrationsMenuPortal}

      {/* KB picker — sits above the footer */}
      {kbPickerOpen && (
        <div
          style={{
            maxHeight: 200,
            borderTop: "1px solid rgba(28,25,23,0.06)",
            display: "flex",
            flexDirection: "column",
            flexShrink: 0,
          }}
        >
          <div
            style={{
              padding: "8px 16px",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="#78716c"
              strokeWidth={1.5}
            >
              <circle cx="11" cy="11" r="8" />
              <path d="M21 21l-4.35-4.35" />
            </svg>
            <input
              value={kbSearch}
              onChange={(e) => setKbSearch(e.target.value)}
              placeholder={t("component.chat_input_footer.search_knowledge_base")}
              autoFocus
              style={{
                flex: 1,
                border: "none",
                outline: "none",
                background: "transparent",
                fontSize: 12,
                color: "#292524",
                fontFamily: "inherit",
              }}
            />
            <button
              onClick={() => {
                setKbPickerOpen(false);
                setKbSearch("");
              }}
              style={{
                border: "none",
                background: "transparent",
                cursor: "pointer",
                color: "#a8a29e",
                display: "flex",
                padding: 2,
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2.5}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: "0 12px 8px" }}>
            {(kbDocs?.items || []).length === 0 && (
              <div
                style={{
                  textAlign: "center",
                  padding: 16,
                  fontSize: 12,
                  color: "#a8a29e",
                }}
              >
                {t("component.chat_input_footer.no_documents_found")}</div>
            )}
            {(kbDocs?.items || []).map((doc: any) => {
              const ext = (
                doc.file_type ||
                doc.name?.split(".").pop() ||
                ""
              ).toLowerCase();
              const alreadyAttached = attachedFiles.some(
                (f) => f.id === doc.id,
              );
              return (
                <button
                  key={doc.id}
                  onClick={() => addKbDoc(doc)}
                  disabled={alreadyAttached}
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "6px 8px",
                    borderRadius: 8,
                    border: "none",
                    background: alreadyAttached ? "#f5f5f4" : "transparent",
                    cursor: alreadyAttached ? "default" : "pointer",
                    textAlign: "left",
                    fontFamily: "inherit",
                    transition: "background 0.1s",
                    opacity: alreadyAttached ? 0.5 : 1,
                  }}
                  onMouseEnter={(e) => {
                    if (!alreadyAttached)
                      e.currentTarget.style.background = "#fafaf9";
                  }}
                  onMouseLeave={(e) => {
                    if (!alreadyAttached)
                      e.currentTarget.style.background = "transparent";
                  }}
                >
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="#4869ac"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"
                    />
                  </svg>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: 12,
                        fontWeight: 600,
                        color: "#44403c",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {doc.name}
                    </div>
                  </div>
                  <span
                    style={{
                      fontSize: 9,
                      fontWeight: 700,
                      padding: "1px 4px",
                      borderRadius: 4,
                      background: "#e7e5e4",
                      color: "#78716c",
                      textTransform: "uppercase",
                      flexShrink: 0,
                    }}
                  >
                    {ext.slice(0, 4) || "?"}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

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
          className={`chat-composer ${focused ? "chat-composer--focused" : ""} ${streaming ? "chat-composer--streaming" : ""}`}
        >
          {attachedFiles.some((file) => file.type !== "knowledge") && (
            <div className="chat-composer-attachments">
              {attachedFiles.map((f, i) =>
                f.type === "knowledge" ? null : (
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
                      {f.name.length > 32
                        ? f.name.slice(0, 30) + "..."
                        : f.name}
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
                ),
              )}
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
                            seed={mention.id}
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
                  <div className="chat-composer-hash-empty">
                    {t("page.skills.loading_skills")}</div>
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
            {hashDropdownOpen && (
              <div className="chat-composer-hash-menu">
                <div className="chat-composer-hash-title">
                  {t("component.chat_input_footer.files_and_documents")}</div>
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
                listening
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
                onClick={() => setAttachMenuOpen(!attachMenuOpen)}
                disabled={streaming || disabled}
                title={t("component.chat_input_footer.add_context_or_tools")}
                className={`chat-composer-icon-btn ${attachMenuOpen ? "chat-composer-icon-btn--active" : ""}`}
                type="button"
              >
                <IconPlus size={18} />
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

            {/* Voice */}
            <button
              onClick={toggleVoice}
              disabled={streaming || disabled}
              title={listening ? t("component.chat_input_footer.stop_listening") : t("component.chat_input_footer.voice_input")}
              className={`chat-composer-icon-btn ${listening ? "chat-composer-icon-btn--recording" : ""}`}
              type="button"
            >
              {listening ? (
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                >
                  <rect x="6" y="6" width="12" height="12" rx="2" />
                </svg>
              ) : (
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.8}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z" />
                  <path d="M19 10v2a7 7 0 01-14 0v-2" />
                  <line x1="12" y1="19" x2="12" y2="23" />
                  <line x1="8" y1="23" x2="16" y2="23" />
                </svg>
              )}
            </button>
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
                disabled={disabled || streaming || !canSend}
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

      {selectedConnector && (
        <div
          className="chat-connector-modal-backdrop"
          role="presentation"
          onMouseDown={() => setSelectedConnector(null)}
        >
          <div
            className="chat-connector-modal"
            role="dialog"
            aria-modal="true"
            aria-label={`${selectedConnector.name} connector`}
            onMouseDown={(e) => e.stopPropagation()}
          >
            <button
              className="chat-connector-modal-close"
              onClick={() => setSelectedConnector(null)}
              type="button"
              aria-label={t("component.chat_input_footer.close_connector_details")}
            >
              ×
            </button>
            <div className="chat-connector-logo">
              {selectedConnector.name?.charAt(0) || "C"}
            </div>
            <h3>{selectedConnector.name}</h3>
            <p>
              {selectedConnector.description ||
                selectedConnector.tagline ||
                selectedConnector.setup_hint ||
                t("component.chat_input_footer.connect_this_integration_then_use_it_directly_from_cha")}
            </p>
            {connectorError && (
              <div className="chat-connector-error">{connectorError}</div>
            )}
            <button
              className="chat-connector-connect"
              onClick={startConnectorAuth}
              disabled={connectorConnecting}
              type="button"
            >
              {connectorConnecting
                ? t("component.chat_input_footer.connecting")
                : selectedConnector.auth_type === "browser_session"
                  ? t("component.chat_input_footer.install")
                  : t("component.chat_input_footer.connect")}
            </button>
            <button
              className="chat-connector-details"
              onClick={() => {
                window.location.href = "/integrations";
              }}
              type="button"
            >
              {t("component.chat_input_footer.show_details")}</button>
          </div>
        </div>
      )}
    </>
  );
}
